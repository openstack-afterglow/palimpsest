"""Keystone token validation and service-scoped OpenStack connections for Palimpsest Hub."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

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

_logger = logging.getLogger(__name__)

keystone_token_header = APIKeyHeader(
    name="X-Auth-Token",
    scheme_name="KeystoneToken",
    auto_error=False,
    description="Keystone authentication token",
)


@lru_cache(maxsize=1)
def _reader_client_for(
    auth_url: str, username: str, password: SecretStr, user_domain_name: str, verify: bool
):
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
        if not access.system_scoped or "reader" not in roles or roles & {"admin", "service"}:
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


def _assignment_role(assignment: Any) -> str:
    role = _value(assignment, "role")
    if not isinstance(role, dict) or not isinstance(role.get("name"), str) or not role["name"]:
        raise HTTPException(status_code=503, detail="Named effective Keystone roles are unavailable")
    return role["name"].casefold()


def _is_system_admin(user_id: str) -> bool:
    """Retain the separate build-admin gate without an admin validator credential."""
    if not user_id:
        return False
    try:
        user_id = validate_keystone_id(user_id)
        assignments = _get_reader_ks_client().role_assignments.list(
            user=user_id, system="all", effective=True, include_names=True
        )
        for assignment in assignments:
            scope = _value(assignment, "scope")
            if (
                _assignment_user(assignment) == user_id
                and isinstance(scope, dict)
                and scope.get("system", {}).get("all") is True
                and _assignment_role(assignment) == "admin"
            ):
                return True
    except Exception:
        _logger.warning("Keystone system-admin check failed")
    return False


def validate_token(token: str, project_id: str = "") -> dict[str, Any]:
    client = _get_reader_ks_client()
    try:
        access = client.tokens.validate(token, include_catalog=True)
    except (ks_exceptions.Unauthorized, ks_exceptions.NotFound, ks_auth_exceptions.Unauthorized, ks_auth_exceptions.NotFound):
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
            raise HTTPException(status_code=403, detail={"code": "PROJECT_SCOPE_MISMATCH", "message": "Project header does not match the original token scope"})
        auth_ref = ks_access.create(auth_token=token, body={"token": dict(access)})
        return {
            "token": token,
            "auth_ref": auth_ref,
            "project_id": original_project,
            "project_name": access.project_name or "",
            "project_domain_id": (access.get("project") or {}).get("domain", {}).get("id", ""),
            "user_id": original_user,
            "username": access.username or "",
            "expires_at": access.expires.isoformat(),
            "roles": list(access.role_names or []),
            "system_scope": bool(access.get("system")),
            "domain_scope": bool(access.get("domain")),
            "is_system_admin": _is_system_admin(original_user),
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
    if not info.get("project_id"):
        raise HTTPException(status_code=401, detail="A project-scoped Keystone token is required")
    request.state.token_info = info
    return info


def get_token_info(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    return token_info


def validate_package_owner(user_id: str, project_id: str) -> dict[str, Any]:
    """Recheck current owner authority; neither a token nor cached membership is minted."""
    settings = get_settings()
    if not settings.palimpsest_hub_package_forbidden_project_ids or not settings.palimpsest_hub_package_forbidden_user_ids:
        raise HTTPException(status_code=503, detail="Protected project and principal policy is required")
    try:
        user_id, project_id = validate_keystone_id(user_id), validate_keystone_id(project_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=403, detail="A verified project member is required") from None
    if user_id in settings.palimpsest_hub_package_forbidden_user_ids or project_id in settings.palimpsest_hub_package_forbidden_project_ids:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_CREDENTIAL_FORBIDDEN", "message": "Protected identities cannot authorize package access"})
    client = _get_reader_ks_client()
    if user_id == client._palimpsest_reader_user_id:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_CREDENTIAL_FORBIDDEN", "message": "The validator identity cannot own package keys"})
    try:
        user = client.users.get(user_id)
        project = client.projects.get(project_id)
        if _value(user, "id") != user_id or _value(project, "id") != project_id:
            raise HTTPException(status_code=503, detail="Keystone identity response ownership is invalid")
        if _value(user, "enabled") is not True or _value(project, "enabled") is not True:
            raise HTTPException(status_code=403, detail="Package owner or project is disabled")
        roles: set[str] = set()
        for assignment in client.role_assignments.list(user=user_id, effective=True, include_names=True):
            if _assignment_user(assignment) != user_id:
                continue
            role = _assignment_role(assignment)
            if role in {"admin", "service"}:
                raise HTTPException(status_code=403, detail={"code": "ADMIN_CREDENTIAL_FORBIDDEN", "message": "Administrative or service identities cannot authorize package access"})
            scope = _value(assignment, "scope")
            target = scope.get("project", {}).get("id") if isinstance(scope, dict) else None
            if target == project_id:
                roles.add(role)
        if not roles & {"member", "reader"}:
            raise HTTPException(status_code=403, detail="Current project membership is required")
        return {
            "user_id": user_id,
            "username": _value(user, "name") or "",
            "project_id": project_id,
            "project_name": _value(project, "name") or "",
            "project_domain_id": _value(project, "domain_id") or "",
            "roles": sorted(roles),
            "can_write": "member" in roles,
        }
    except HTTPException:
        raise
    except ks_exceptions.NotFound:
        raise HTTPException(status_code=403, detail="Package owner or project is unavailable") from None
    except Exception:
        raise HTTPException(status_code=503, detail="Keystone membership validation is unavailable") from None


async def get_package_member_info(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    roles = {str(role).casefold() for role in token_info.get("roles", [])}
    if (
        roles & {"admin", "service"}
        or token_info.get("is_system_admin")
        or token_info.get("system_scope")
        or token_info.get("domain_scope")
    ):
        raise HTTPException(status_code=403, detail={"code": "ADMIN_CREDENTIAL_FORBIDDEN", "message": "Administrative or service tokens cannot authorize package access"})
    if not roles & {"member", "reader"}:
        raise HTTPException(status_code=403, detail="A project member or reader token is required")
    owner = await asyncio.to_thread(validate_package_owner, token_info["user_id"], token_info["project_id"])
    owner["can_write"] = owner["can_write"] and "member" in roles
    return {**token_info, **owner}


def require_admin(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    if not token_info.get("is_system_admin"):
        raise HTTPException(status_code=403, detail="System administrator role is required")
    return token_info


async def get_os_conn(
    token_info: dict[str, Any] = Depends(require_token),
) -> AsyncGenerator[object, None]:
    """Yield a caller-token-scoped OpenStack connection and close it."""
    import openstack

    settings = get_settings()
    project_id = token_info["project_id"]
    scoped_token = token_info["token"]
    auth_ref = token_info.get("auth_ref")
    if (
        auth_ref is None
        or auth_ref.auth_token != scoped_token
        or auth_ref.project_id != project_id
    ):
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
