"""Keystone token validation and service-scoped OpenStack connections for Palimpsest Hub."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from keystoneauth1 import session as ks_session
from keystoneauth1.identity import v3

from palimpsest_hub.config import get_settings

_logger = logging.getLogger(__name__)
_admin_role_id_cache: str | None = None

keystone_token_header = APIKeyHeader(
    name="X-Auth-Token",
    scheme_name="KeystoneToken",
    auto_error=False,
    description="Keystone authentication token",
)


def _get_admin_ks_client():
    from keystoneclient.v3 import client as ks_client

    settings = get_settings()
    auth = v3.Password(
        auth_url=settings.os_auth_url,
        username=settings.os_username,
        password=settings.os_password.get_secret_value(),
        project_name=settings.os_project_name,
        user_domain_name=settings.os_user_domain_name,
        project_domain_name=settings.os_project_domain_name,
    )
    session = ks_session.Session(auth=auth, timeout=15, verify=settings.ssl_verify)
    return ks_client.Client(session=session)


def _resolve_admin_role_id() -> str | None:
    global _admin_role_id_cache
    if _admin_role_id_cache:
        return _admin_role_id_cache
    try:
        roles = _get_admin_ks_client().roles.list(name="admin")
        if roles:
            _admin_role_id_cache = roles[0].id
    except Exception:
        _logger.warning("Failed to resolve Keystone admin role", exc_info=True)
    return _admin_role_id_cache


def _is_system_admin(user_id: str) -> bool:
    """Fail closed unless the user has admin on Keystone system scope."""
    if not user_id:
        return False
    try:
        role_id = _resolve_admin_role_id()
        if not role_id:
            return False
        assignments = _get_admin_ks_client().role_assignments.list(
            user=user_id,
            role=role_id,
            system="all",
        )
        return bool(assignments)
    except Exception:
        _logger.warning("Keystone system-admin check failed", exc_info=True)
        return False


def validate_token(token: str, project_id: str = "") -> dict[str, Any]:
    settings = get_settings()
    kwargs: dict[str, Any] = {"auth_url": settings.os_auth_url, "token": token}
    if project_id:
        kwargs["project_id"] = project_id
    auth_plugin = v3.Token(**kwargs)
    session = ks_session.Session(auth=auth_plugin, timeout=30, verify=settings.ssl_verify)
    access = auth_plugin.get_access(session)
    roles = list(access.role_names) if access.role_names else []
    return {
        "token": access.auth_token,
        "project_id": access.project_id or "",
        "project_name": access.project_name or "",
        "user_id": access.user_id or "",
        "username": access.username or "",
        "expires_at": access.expires.isoformat() if access.expires else "",
        "roles": roles,
        "is_system_admin": _is_system_admin(access.user_id or ""),
    }


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
    except Exception:
        _logger.info("Keystone token validation failed", exc_info=True)
        raise HTTPException(status_code=401, detail="Invalid or expired Keystone token") from None
    if not info.get("project_id"):
        raise HTTPException(status_code=401, detail="A project-scoped Keystone token is required")
    request.state.token_info = info
    return info


def get_token_info(token_info: dict[str, Any] = Depends(require_token)) -> dict[str, Any]:
    return token_info


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
    try:
        conn = openstack.connect(
            load_envvars=False,
            load_yaml_config=False,
            auth_url=settings.os_auth_url,
            auth_type="token",
            token=scoped_token,
            project_id=project_id,
            region_name=settings.os_region_name,
            interface=settings.os_interface,
            api_timeout=30,
            verify=settings.ssl_verify,
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
