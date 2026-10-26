"""Catalog-only original human/global authority; existing project/auth contracts stay unchanged."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Header, HTTPException, Request, Security
from keystoneauth1 import access as ks_access
from keystoneauth1 import exceptions as ks_auth_exceptions
from keystoneclient import exceptions as ks_exceptions

from palimpsest_hub.auth import _assignment_user, _get_reader_ks_client, _role_metadata, _value, keystone_token_header
from palimpsest_hub.catalog_policy import global_admin_actions
from palimpsest_hub.config import get_settings, validate_keystone_id

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


def _global_admin_context(token_info: dict[str, Any]) -> dict[str, Any]:
    """Fresh human validation; only explicit global catalog reads omit a target."""
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
    target = validate_keystone_id(selected_target) if selected_target else None
    client = _get_reader_ks_client()
    if not _is_system_admin(user_id):
        raise HTTPException(status_code=403, detail="Current system administrator assignment is required")
    if user_id == client._palimpsest_reader_user_id:
        raise HTTPException(status_code=403, detail="Validator identity cannot use human administrative authority")
    try:
        user = client.users.get(user_id)
        project = client.projects.get(target) if target else None
        if _value(user, "id") != user_id or (project is not None and _value(project, "id") != target):
            raise HTTPException(status_code=503, detail="Keystone identity response ownership is invalid")
        resources = (
            ((user, "Administrator"), (project, "Target project"))
            if project is not None
            else ((user, "Administrator"),)
        )
        for resource, label in resources:
            enabled = _value(resource, "enabled")
            if enabled is False:
                raise HTTPException(status_code=403, detail=label + " is disabled")
            if enabled is not True:
                raise HTTPException(status_code=503, detail=label + " enabled state is unavailable")
        home_domain_id = validate_keystone_id(_value(user, "domain_id"))
        target_domain_id = validate_keystone_id(_value(project, "domain_id")) if project is not None else None
        if home_domain_id != validate_keystone_id(token_info.get("user_domain_id")):
            raise HTTPException(status_code=503, detail="Current identity domain does not match the original subject")
        for domain_id in dict.fromkeys(domain for domain in (home_domain_id, target_domain_id) if domain is not None):
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
    actions = global_admin_actions(user_id)
    return {
        **token_info,
        "is_package_system_admin": True,
        "target_project_id": target,
        "package_actions": actions,
        "target_project_name": (_value(project, "name") or "") if project is not None else "",
    }


async def require_global_admin(
    request: Request,
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
    _token_scheme: str | None = Security(keystone_token_header),
) -> dict[str, Any]:
    """Global catalog only: original human system authority, no fabricated target.

    Target-bound require_token/require_admin contracts intentionally stay intact.
    """
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="X-Auth-Token header is required")
    try:
        original = await asyncio.to_thread(validate_token, x_auth_token, x_project_id or "")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone token validation is unavailable") from None
    context = await asyncio.to_thread(_global_admin_context, original)
    if "system:manage" not in context["package_actions"]:
        raise HTTPException(status_code=403, detail="System management policy denied")
    request.state.token_info = context
    return context
