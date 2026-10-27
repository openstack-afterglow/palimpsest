"""Keystone token validation, current-authority checks and caller-token OpenStack connections for Palimpsest Hub."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from keystoneauth1 import access as ks_access
from keystoneauth1 import exceptions as ks_auth_exceptions
from keystoneauth1 import session as ks_session
from keystoneauth1.identity import v3
from keystoneauth1.identity.access import AccessInfoPlugin
from keystoneclient import exceptions as ks_exceptions
from pydantic import SecretStr

from palimpsest_hub.config import get_settings, validate_keystone_id
from palimpsest_hub.policy import (
    allowed_actions,
    capabilities_for,
)

_logger = logging.getLogger(__name__)

_HUMAN_AUTH_METHODS = frozenset({"password", "token", "totp", "oidc", "saml2", "mapped"})
_REDUCED_TOKEN_MARKERS = frozenset(
    {
        "OS-TRUST:trust",
        "application_credential",
        "OS-OAUTH1",
        "OS-OAUTH2",
        "oauth1",
        "oauth2",
        "bind",
        "access_rules",
    }
)


def _role_metadata(client, path):
    try:
        return client.get(path)[1]
    except ks_exceptions.NotFound:
        raise HTTPException(status_code=503, detail="Keystone role metadata is unavailable") from None


def _role_directory(client):
    """Read one fresh, complete role-ID graph. Missing/ambiguous data never grants authority."""
    directory = _role_metadata(client, "/roles")
    inferences = _role_metadata(client, "/role_inferences")
    _complete_identity_collection(directory, "roles")
    _complete_identity_collection(inferences, "role_inferences")
    by_id, global_names = {}, {}
    for role in directory["roles"]:
        role_id, name = role["id"], role["name"]
        if not isinstance(role_id, str) or not role_id or not isinstance(name, str) or not name or role_id in by_id:
            raise ValueError("Invalid Keystone role directory")
        by_id[role_id] = role
        if role.get("domain_id") is None:
            global_names.setdefault(name.casefold(), []).append(role_id)

    def referenced_role(role_id):
        if role_id not in by_id:
            # /roles normally lists globals only, while inferences can mention domain roles.
            validate_keystone_id(role_id)
            response = _role_metadata(client, f"/roles/{role_id}")
            role = response["role"]
            if role.get("id") != role_id or not isinstance(role.get("name"), str) or not role["name"]:
                raise ValueError("Invalid referenced role")
            if role.get("domain_id") is None:
                raise ValueError("Global role missing from complete directory")
            by_id[role_id] = role
        return by_id[role_id]

    edges = {role_id: set() for role_id in by_id}
    for inference in inferences["role_inferences"]:
        prior = inference["prior_role"]["id"]
        prior_role = referenced_role(prior)
        for implied in inference["implies"]:
            implied_role = referenced_role(implied["id"])
            # Domain/custom aliases never assert global builtin capabilities through inference.
            if prior_role.get("domain_id") is None and implied_role.get("domain_id") is None:
                edges[prior].add(implied["id"])
    return by_id, global_names, edges


def _effective_role_names(role_ids, directory):
    by_id, global_names, edges = directory
    names, visited, visiting = set(), set(), set()

    def visit(role_id):
        if role_id in visiting or role_id not in by_id:
            raise ValueError("Invalid Keystone role graph")
        if role_id in visited:
            return
        role = by_id[role_id]
        name = role["name"]
        if role.get("domain_id") is not None:
            return
        if global_names.get(name.casefold()) != [role_id]:
            raise ValueError("Role is not a unique global binding")
        visiting.add(role_id)
        names.add(name)
        for implied in edges[role_id]:
            visit(implied)
        visiting.remove(role_id)
        visited.add(role_id)

    for role_id in role_ids:
        visit(role_id)
    return names


def role_name_closure(role_names, directory) -> frozenset[str]:
    """Exact unique global role names plus their current Keystone implications; ambiguity grants nothing."""
    by_id, global_names, _ = directory
    role_ids = []
    for name in role_names:
        bound = global_names.get(name.casefold())
        if not bound or len(bound) != 1 or by_id[bound[0]]["name"] != name:
            raise ValueError("Delegated role is not a unique global Keystone role")
        role_ids.append(bound[0])
    return frozenset(_effective_role_names(role_ids, directory))


keystone_token_header = APIKeyHeader(
    name="X-Auth-Token",
    scheme_name="KeystoneToken",
    auto_error=False,
    description="Keystone authentication token",
)


@lru_cache(maxsize=1)
def _reader_client_for(auth_url: str, username: str, password: SecretStr, user_domain_name: str, verify: bool):
    from keystoneclient.v3 import client as ks_client

    auth = v3.Password(
        auth_url=auth_url,
        username=username,
        password=password.get_secret_value(),
        user_domain_name=user_domain_name,
        system_scope="all",
    )
    session = ks_session.Session(auth=auth, timeout=15, verify=verify)
    return ks_client.Client(session=session, endpoint_override=auth_url)


def _get_reader_ks_client():
    settings = get_settings()
    if not settings.os_reader_username or not settings.os_reader_password.get_secret_value():
        raise HTTPException(status_code=503, detail="Read-only Keystone validator credentials are required")
    try:
        client = _reader_client_for(
            settings.os_auth_url,
            settings.os_reader_username,
            settings.os_reader_password,
            settings.os_reader_user_domain_name,
            settings.ssl_verify,
        )
        access = client.session.auth.get_access(client.session)
        roles = {role.casefold() for role in access.role_names}
        if not access.system_scoped or "reader" not in roles or roles & {"admin", "manager", "service"}:
            raise HTTPException(status_code=503, detail="A read-only Keystone validator identity is required")
        client._palimpsest_reader_user_id = validate_keystone_id(access.user_id)
        return client
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone validator identity is unavailable") from None


def _value(resource: Any, name: str) -> Any:
    return resource.get(name) if isinstance(resource, dict) else getattr(resource, name, None)


def _assignment_user(assignment: Any) -> str:
    user = _value(assignment, "user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        return ""
    try:
        return validate_keystone_id(user["id"])
    except ValueError:
        return ""


def _complete_identity_collection(payload, key: str) -> list[dict[str, Any]]:
    """Only absent/null next and absent/null/literal-false truncation are complete."""
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get(key), list)
        or not isinstance(payload.get("links", {}), dict)
        or payload.get("links", {}).get("next") is not None
        or (payload.get("truncated") is not None and payload.get("truncated") is not False)
        or any(not isinstance(item, dict) for item in payload[key])
    ):
        raise ValueError("Incomplete Keystone identity collection")
    return payload[key]


def _identity_collection(client, path: str, key: str) -> list[dict[str, Any]]:
    """Read one complete native collection; partial metadata never grants authority."""
    try:
        payload = client.get(path)[1]
    except ks_exceptions.NotFound:
        raise HTTPException(status_code=503, detail="Keystone identity collection is unavailable") from None
    return _complete_identity_collection(payload, key)


def _system_role_names(client, user_id: str, directory) -> frozenset[str]:
    """Raw exact-system grants plus current native groups and trusted role-ID inference.

    Keystone's effective assignment view may omit system grants. It is never
    used as either positive or negative evidence for this boundary.
    """
    user_id = validate_keystone_id(user_id)
    by_id, global_names, _ = directory
    if len(global_names.get("admin", ())) != 1:
        raise ValueError("Canonical global admin role is unavailable")

    def roles_from(records, principal, identity):
        result = set()
        for assignment in records:
            assigned_principal = assignment.get(principal)
            scope = assignment.get("scope")
            if (
                not isinstance(assigned_principal, dict)
                or assigned_principal.get("id") != identity
                or not isinstance(scope, dict)
                or set(scope) != {"system"}
                or not (
                    scope["system"] == "all"
                    or isinstance(scope["system"], dict)
                    and set(scope["system"]) == {"all"}
                    and scope["system"]["all"] is True
                )
                or (principal == "user" and "group" in assignment)
                or (principal == "group" and "user" in assignment)
            ):
                continue
            role = assignment.get("role")
            if not isinstance(role, dict):
                raise ValueError("Native assignment role ID is unavailable")
            role_id = validate_keystone_id(role.get("id"))
            native_role = by_id.get(role_id)
            if native_role is None:
                native_role = client.get("/roles/" + role_id)[1]["role"]
                if native_role.get("id") != role_id or native_role.get("domain_id") is None:
                    raise ValueError("Global assignment role missing from complete directory")
                # Known domain roles cannot imply global system authority.
                continue
            if role.get("name") is not None and role["name"] != native_role["name"]:
                raise ValueError("Native assignment role does not match its ID")
            result.update(_effective_role_names([role_id], directory))
        return result

    query = urlencode({"user.id": user_id, "scope.system": "all", "include_names": "true"})
    direct = _identity_collection(client, "/role_assignments?" + query, "role_assignments")
    names = roles_from(direct, "user", user_id)
    groups = _identity_collection(client, "/users/" + user_id + "/groups", "groups")
    group_ids = [validate_keystone_id(group.get("id")) for group in groups]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("Native group membership is ambiguous")
    for group_id in group_ids:
        query = urlencode({"group.id": group_id, "scope.system": "all", "include_names": "true"})
        assignments = _identity_collection(client, "/role_assignments?" + query, "role_assignments")
        names.update(roles_from(assignments, "group", group_id))
    return frozenset(names)


def _is_system_admin(user_id: str) -> bool:
    """Fresh native raw user/group system authority; unavailable is not a denial."""
    if not user_id:
        return False
    try:
        client = _get_reader_ks_client()
        roles = _system_role_names(client, user_id, _role_directory(client))
        return "admin" in roles and "service" not in roles
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone system authority is unavailable") from None


def validate_token(token: str, project_id: str = "") -> dict[str, Any]:
    client = _get_reader_ks_client()
    try:
        access = client.tokens.validate(token, include_catalog=True)
    except (
        ks_exceptions.Unauthorized,
        ks_exceptions.NotFound,
        ks_auth_exceptions.Unauthorized,
        ks_auth_exceptions.NotFound,
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired Keystone token") from None
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone token validation is unavailable") from None
    try:
        original_project = validate_keystone_id(access.project_id) if access.project_id else ""
        original_user = validate_keystone_id(access.user_id)
        expected_project = validate_keystone_id(project_id) if project_id else ""
        if access.expires is None or access.expires <= datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Invalid or expired Keystone token")
        if expected_project and original_project != expected_project:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PROJECT_SCOPE_MISMATCH",
                    "message": "Project header does not match the original token scope",
                },
            )
        auth_ref = ks_access.create(auth_token=token, body={"token": dict(access)})
        methods = tuple(access.get("methods") or ())
        human = bool(methods) and set(methods) <= _HUMAN_AUTH_METHODS
        # Scope exchange is a standard human method, not proof that the original
        # subject's explicit delegation/binding/access-rule restrictions vanished.
        delegated = any(marker in access for marker in _REDUCED_TOKEN_MARKERS)
        return {
            "token": token,
            "auth_ref": auth_ref,
            "project_id": original_project,
            "project_name": access.project_name or "",
            "project_domain_id": (access.get("project") or {}).get("domain", {}).get("id", ""),
            "user_id": original_user,
            "username": access.username or "",
            "user_domain_name": (access.get("user") or {}).get("domain", {}).get("name", ""),
            "user_domain_id": (access.get("user") or {}).get("domain", {}).get("id", ""),
            "expires_at": access.expires.isoformat(),
            "roles": list(access.role_names or []),
            "role_ids": [role["id"] for role in access.get("roles", [])],
            "system_scope": bool(access.get("system")),
            "domain_scope": bool(access.get("domain")),
            "auth_methods": methods,
            "is_delegated": delegated,
            "is_system_admin": human and not delegated and _is_system_admin(original_user),
        }
    except HTTPException:
        raise
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="Invalid scoped Keystone token") from None


async def require_token(
    request: Request,
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
    _token_scheme: str | None = Security(keystone_token_header),
) -> dict[str, Any]:
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="X-Auth-Token header is required")
    try:
        info = await asyncio.to_thread(validate_token, x_auth_token, x_project_id or "")
    except HTTPException:
        raise
    except Exception:
        _logger.info("Keystone token validation failed")
        raise HTTPException(status_code=503, detail="Keystone token validation is unavailable") from None
    target = request.headers.get("x-target-project-id")
    if not info.get("project_id") and not (info.get("is_system_admin") and target is not None):
        raise HTTPException(
            status_code=401, detail="A project token or human system administrator with an explicit target is required"
        )
    if target is not None:
        try:
            info["target_project_id"] = validate_keystone_id(target)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=422, detail="Invalid logical target project ID") from None
        info = await asyncio.to_thread(_package_admin_context, info)
    request.state.token_info = info
    return info


def get_token_info(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    return token_info


def _personal_project_owner(client, user_id, project_id, user, project, directory, assignments):
    """Resolve personal authority from fresh native extras, domains and provenance."""
    if _value(project, "afterglow_personal_user_id") != user_id:
        return False
    domains = {}
    for resource in (user, project):
        domain_id = validate_keystone_id(_value(resource, "domain_id"))
        if domain_id not in domains:
            domain = client.domains.get(domain_id)
            if _value(domain, "id") != domain_id or _value(domain, "enabled") is not True:
                return False
            domains[domain_id] = domain
    domain = domains[_value(project, "domain_id")]
    kind = (
        domain.get("afterglow_domain_kind", "personal")
        if isinstance(domain, dict)
        else getattr(domain, "afterglow_domain_kind", "personal")
    )
    if _value(domain, "name") != "Personal" or kind != "personal":
        return False
    members = directory[1].get("member", ())
    if len(members) != 1 or directory[0][members[0]]["name"] != "member":
        return False
    member = members[0]
    if not _effective_role_names([member], directory) <= {"member", "reader"}:
        return False
    permitted = {member, *directory[1].get("reader", ())}
    raw = _identity_collection(
        client,
        "/role_assignments?"
        + urlencode(
            {
                "scope.project.id": project_id,
                "include_names": "true",
            }
        ),
        "role_assignments",
    )

    def direct(row, role_ids):
        scope, links = row.get("scope"), row.get("links", {})
        return (
            isinstance(scope, dict)
            and set(scope) == {"project"}
            and scope["project"].get("id") == project_id
            and row.get("user", {}).get("id") == user_id
            and "group" not in row
            and row.get("role", {}).get("id") in role_ids
            and isinstance(links, dict)
            and "membership" not in links
            and "OS-INHERIT:inherited_to" not in row
            and (
                not links.get("assignment")
                or links["assignment"].rstrip("/").endswith(f"/projects/{project_id}/users/{user_id}/roles/{member}")
            )
        )

    effective = [row for row in assignments if row.get("scope", {}).get("project", {}).get("id") == project_id]
    return (
        bool(raw)
        and all(direct(row, {member}) for row in raw)
        and bool(effective)
        and all(direct(row, permitted) for row in effective)
        and any(row.get("role", {}).get("id") == member for row in effective)
    )


def validate_package_owner(user_id: str, project_id: str, *, allow_empty: bool = False) -> dict[str, Any]:
    """Recheck current owner authority; neither a token nor cached membership is minted."""
    settings = get_settings()
    if (
        not settings.palimpsest_hub_package_forbidden_project_ids
        or not settings.palimpsest_hub_package_forbidden_user_ids
    ):
        raise HTTPException(status_code=503, detail="Protected project and principal policy is required")
    try:
        user_id, project_id = validate_keystone_id(user_id), validate_keystone_id(project_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail="A verified project member is required") from None
    if (
        user_id in settings.palimpsest_hub_package_forbidden_user_ids
        or project_id in settings.palimpsest_hub_package_forbidden_project_ids
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_CREDENTIAL_FORBIDDEN",
                "message": "Protected identities cannot authorize package access",
            },
        )
    client = _get_reader_ks_client()
    if user_id == client._palimpsest_reader_user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "ADMIN_CREDENTIAL_FORBIDDEN", "message": "The validator identity cannot own package keys"},
        )
    try:
        user = client.users.get(user_id)
        project = client.projects.get(project_id)
        if _value(user, "id") != user_id or _value(project, "id") != project_id:
            raise HTTPException(status_code=503, detail="Keystone identity response ownership is invalid")
        if _value(user, "enabled") is not True or _value(project, "enabled") is not True:
            raise HTTPException(status_code=403, detail="Package owner or project is disabled")
        roles: set[str] = set()
        directory = _role_directory(client)
        if _system_role_names(client, user_id, directory) & {"admin", "manager", "service"}:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "ADMIN_CREDENTIAL_FORBIDDEN",
                    "message": "System principals cannot authorize ordinary member keys",
                },
            )
        query = urlencode({"user.id": user_id, "effective": "true", "include_names": "true"})
        assignments = _identity_collection(client, "/role_assignments?" + query, "role_assignments")
        for assignment in assignments:
            if _assignment_user(assignment) != user_id:
                continue
            assigned = _value(assignment, "role")
            effective = _effective_role_names([assigned["id"]], directory)
            if assigned.get("name") != directory[0][assigned["id"]]["name"]:
                raise ValueError("Assignment role does not match directory")
            if {role.casefold() for role in effective} & {"service"}:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "ADMIN_CREDENTIAL_FORBIDDEN",
                        "message": "Administrative or service identities cannot authorize package access",
                    },
                )
            scope = _value(assignment, "scope")
            target = scope.get("project", {}).get("id") if isinstance(scope, dict) else None
            if target == project_id:
                roles.update(effective)
        if not roles:
            raise HTTPException(status_code=403, detail="Current native project membership is required")
        personal_owner = _personal_project_owner(client, user_id, project_id, user, project, directory, assignments)
        actions = allowed_actions(roles, project_id, user_id, personal_project_owner=personal_owner)
        capabilities = capabilities_for(actions)
        if not actions and not allow_empty:
            raise HTTPException(status_code=403, detail="Current Palimpsest service authority is required")
        return {
            "user_id": user_id,
            "username": _value(user, "name") or "",
            "project_id": project_id,
            "project_name": _value(project, "name") or "",
            "project_domain_id": _value(project, "domain_id") or "",
            "roles": sorted(roles),
            "package_capabilities": capabilities,
            "package_actions": actions,
            "can_write": "palimpsest-publish_editor" in capabilities,
            "role_directory": directory,
            "personal_project_owner": personal_owner,
        }
    except HTTPException:
        raise
    except ks_exceptions.NotFound:
        raise HTTPException(status_code=403, detail="Package owner or project is unavailable") from None
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone membership validation is unavailable") from None


async def get_package_member_info(
    token_info: dict[str, Any] = Depends(require_token), *, allow_empty: bool = False
) -> dict[str, Any]:
    roles = {str(role).casefold() for role in token_info.get("roles", [])}
    if (
        roles & {"service"}
        or token_info.get("is_system_admin")
        or token_info.get("system_scope")
        or token_info.get("domain_scope")
        or token_info.get("is_delegated")
        or not token_info.get("auth_methods")
        or not set(token_info["auth_methods"]) <= _HUMAN_AUTH_METHODS
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ADMIN_CREDENTIAL_FORBIDDEN",
                "message": "Administrative or service tokens cannot authorize package access",
            },
        )
    owner = await asyncio.to_thread(
        validate_package_owner, token_info["user_id"], token_info["project_id"], allow_empty=allow_empty
    )
    try:
        token_roles = _effective_role_names(token_info["role_ids"], owner["role_directory"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Current Keystone role bindings are unavailable") from None
    token_actions = allowed_actions(
        token_roles,
        token_info["project_id"],
        token_info["user_id"],
        personal_project_owner=owner.get("personal_project_owner") is True and "member" in token_roles,
    )
    if owner["user_id"] != token_info["user_id"] or owner["project_id"] != token_info["project_id"]:
        raise HTTPException(status_code=403, detail="Package owner does not match original token identity")
    actions = token_actions & owner["package_actions"]
    capabilities = capabilities_for(actions)
    if not actions and not allow_empty:
        raise HTTPException(status_code=403, detail="Current Palimpsest service authority is required")
    return {
        **token_info,
        "current_roles": owner["roles"],
        "package_capabilities": capabilities,
        "package_actions": actions,
        "personal_project_owner": owner.get("personal_project_owner") is True and "member" in token_roles,
        "can_write": "palimpsest-publish_editor" in capabilities,
    }


def package_target_project_id(token_info: dict[str, Any]) -> str:
    """Logical Hub target; never a replacement OpenStack token scope."""
    if token_info.get("is_package_system_admin"):
        return token_info.get("target_project_id") or token_info["project_id"]
    return token_info["project_id"]


def _package_admin_context(token_info: dict[str, Any]) -> dict[str, Any]:
    """Fresh human authority for an explicit logical target."""
    methods = token_info.get("auth_methods", ())
    if (
        not token_info.get("is_system_admin")
        or not methods
        or not set(methods) <= _HUMAN_AUTH_METHODS
        or token_info.get("is_delegated")
        or token_info.get("domain_scope")
        or {str(role).casefold() for role in token_info.get("roles", ())} & {"service"}
    ):
        raise HTTPException(status_code=403, detail="Current human system administrator authority is required")
    settings = get_settings()
    user_id = validate_keystone_id(token_info["user_id"])
    selected_target = token_info.get("target_project_id") or token_info.get("project_id")
    if not selected_target:
        raise HTTPException(
            status_code=403, detail="An explicit logical target is required for an unscoped administrator"
        )
    target = validate_keystone_id(selected_target)
    client = _get_reader_ks_client()
    if not _is_system_admin(user_id):
        raise HTTPException(status_code=403, detail="Current system administrator assignment is required")
    if user_id == client._palimpsest_reader_user_id:
        raise HTTPException(status_code=403, detail="Validator identity cannot use human administrative authority")
    try:
        user = client.users.get(user_id)
        project = client.projects.get(target)
        if _value(user, "id") != user_id or _value(project, "id") != target:
            raise HTTPException(status_code=503, detail="Keystone identity response ownership is invalid")
        for resource, label in ((user, "Administrator"), (project, "Target project")):
            enabled = _value(resource, "enabled")
            if enabled is False:
                raise HTTPException(status_code=403, detail=label + " is disabled")
            if enabled is not True:
                raise HTTPException(status_code=503, detail=label + " enabled state is unavailable")
        home_domain_id = validate_keystone_id(_value(user, "domain_id"))
        target_domain_id = validate_keystone_id(_value(project, "domain_id"))
        if home_domain_id != validate_keystone_id(token_info.get("user_domain_id")):
            raise HTTPException(status_code=503, detail="Current identity domain does not match the original subject")
        for domain_id in dict.fromkeys((home_domain_id, target_domain_id)):
            try:
                domain = client.domains.get(domain_id)
            except ks_exceptions.NotFound:
                raise HTTPException(status_code=503, detail="Native identity domain is unavailable") from None
            if _value(domain, "id") != domain_id:
                raise HTTPException(status_code=503, detail="Native identity domain ownership is invalid")
            enabled = _value(domain, "enabled")
            if enabled is False:
                raise HTTPException(status_code=403, detail="Administrator or target domain is disabled")
            if enabled is not True:
                raise HTTPException(status_code=503, detail="Native identity domain enabled state is unavailable")
        # The configured trustee is resolved independently of caller token names.
        # A renamed/mismatched token domain cannot suppress its exact-ID exclusion.
        configured_domains = _identity_collection(
            client,
            "/domains?" + urlencode({"name": settings.os_user_domain_name}),
            "domains",
        )
        if len(configured_domains) != 1:
            raise ValueError("Configured service domain is missing or ambiguous")
        service_domain = configured_domains[0]
        service_domain_id = validate_keystone_id(service_domain.get("id"))
        if (
            service_domain.get("name") != settings.os_user_domain_name
            or type(service_domain.get("enabled")) is not bool
        ):
            raise ValueError("Configured service domain ownership or state is invalid")
        if service_domain_id in (home_domain_id, target_domain_id) and service_domain["enabled"] is False:
            raise HTTPException(status_code=403, detail="Administrator or target domain is disabled")
        configured_users = _identity_collection(
            client,
            "/users?" + urlencode({"name": settings.os_username, "domain_id": service_domain_id}),
            "users",
        )
        if len(configured_users) != 1:
            raise ValueError("Configured service identity is missing or ambiguous")
        service_user = configured_users[0]
        service_user_id = validate_keystone_id(service_user.get("id"))
        if (
            service_user.get("name") != settings.os_username
            or service_user.get("domain_id") != service_domain_id
            or type(service_user.get("enabled")) is not bool
        ):
            raise ValueError("Configured service identity ownership or state is invalid")
        if user_id == service_user_id:
            raise HTTPException(
                status_code=403, detail="Export service identity cannot use human administrative authority"
            )
        directory = _role_directory(client)
        query = urlencode({"user.id": user_id, "effective": "true", "include_names": "true"})
        for assignment in _identity_collection(client, "/role_assignments?" + query, "role_assignments"):
            if _assignment_user(assignment) != user_id:
                continue
            role = _value(assignment, "role")
            effective = _effective_role_names([role["id"]], directory)
            if role.get("name") != directory[0][role["id"]]["name"]:
                raise ValueError("Assignment role does not match directory")
            if "service" in effective:
                raise HTTPException(
                    status_code=403, detail="Service principals cannot use human administrative authority"
                )
    except HTTPException:
        raise
    except ks_exceptions.NotFound:
        raise HTTPException(status_code=403, detail="Administrator or target project is unavailable") from None
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone administrative validation is unavailable") from None
    actions = allowed_actions({"admin"}, target, user_id, system_admin=True)
    return {
        **token_info,
        "is_package_system_admin": True,
        "target_project_id": target,
        "package_actions": actions,
        "package_capabilities": capabilities_for(actions),
        "target_project_name": _value(project, "name") or "",
    }


async def revalidate_package_admin(token_info: dict[str, Any]) -> dict[str, Any]:
    """Revalidate the same subject at publication boundaries; never reuse a stale flag."""
    original = await asyncio.to_thread(validate_token, token_info["token"], token_info["project_id"])
    if original["user_id"] != token_info["user_id"]:
        raise HTTPException(status_code=403, detail="Administrator subject identity changed")
    original["target_project_id"] = token_info.get("target_project_id") or token_info["project_id"]
    return await asyncio.to_thread(_package_admin_context, original)


async def get_package_manager_info(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    if token_info.get("is_system_admin"):
        return await asyncio.to_thread(_package_admin_context, token_info)
    return await get_package_member_info(token_info=token_info)


def require_admin(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    if not token_info.get("is_system_admin"):
        raise HTTPException(status_code=403, detail="Current human system administrator authority is required")
    original = validate_token(token_info["token"], token_info["project_id"])
    if original["user_id"] != token_info["user_id"]:
        raise HTTPException(status_code=403, detail="Administrator subject identity changed")
    original["target_project_id"] = token_info.get("target_project_id") or token_info.get("project_id")
    context = _package_admin_context(original)
    if "system:manage" not in context["package_actions"]:
        raise HTTPException(status_code=403, detail="System management policy denied")
    return context


def validate_admin_key_owner(user_id: str, target_project_id: str, issuer_context: dict[str, Any]) -> dict[str, Any]:
    """Recheck an explicitly human-admin-issued bounded key, never upgrade an old member key."""
    if not isinstance(issuer_context, dict) or set(issuer_context) != {
        "project_id",
        "auth_methods",
        "user_domain_id",
        "user_domain_name",
    }:
        raise HTTPException(status_code=503, detail="Administrative key issuer provenance is invalid")
    # Provenance is server-owned admission data, not a token or machine-supplied
    # authority flag. The key still carries only its declared package/cache scope.
    return _package_admin_context(
        {
            **issuer_context,
            "user_id": validate_keystone_id(user_id),
            "target_project_id": validate_keystone_id(target_project_id),
            "is_system_admin": _is_system_admin(user_id),
            "is_delegated": False,
            "domain_scope": False,
            "roles": (),
        }
    )


async def get_os_conn(
    token_info: dict[str, Any] = Depends(require_token),
) -> AsyncGenerator[object, None]:
    """Yield a caller-token-scoped OpenStack connection and close it."""
    import openstack

    settings = get_settings()
    project_id = token_info["project_id"]
    scoped_token = token_info["token"]
    auth_ref = token_info.get("auth_ref")
    if auth_ref is None or auth_ref.auth_token != scoped_token or (auth_ref.project_id or "") != project_id:
        raise HTTPException(status_code=401, detail="Validated original Keystone token is required")
    try:
        session = ks_session.Session(
            auth=AccessInfoPlugin(auth_ref=auth_ref, auth_url=settings.os_auth_url),
            timeout=30,
            verify=settings.ssl_verify,
        )
        conn = openstack.connection.Connection(
            session=session,
            region_name=settings.os_region_name,
            interface=settings.os_interface,
            api_timeout=30,
        )
        conn._afterglow_token = scoped_token
        conn._afterglow_project_id = project_id
        conn._afterglow_user_id = token_info.get("user_id", "")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid scoped Keystone token") from None

    try:
        yield conn
    finally:
        await asyncio.to_thread(conn.close)
