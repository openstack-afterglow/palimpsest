from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import openstack
from fastapi import HTTPException, Request

from palimpsest_hub.config import get_settings

_CONTEXT_HEADER = "x-afterglow-context"
_SIGNATURE_HEADER = "x-afterglow-signature"
_MAX_CONTEXT_TTL_SECONDS = 60


def _decode_context(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Afterglow service context") from exc


def _verified_context(request: Request) -> dict[str, Any]:
    encoded = request.headers.get(_CONTEXT_HEADER)
    signature = request.headers.get(_SIGNATURE_HEADER)
    if not encoded or not signature:
        raise HTTPException(status_code=401, detail="Afterglow service authentication is required")

    raw = _decode_context(encoded)
    expected = hmac.new(
        get_settings().palimpsest_hub_signing_secret.get_secret_value().encode(), raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Afterglow service signature")

    try:
        context = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Afterglow service context") from exc
    if not isinstance(context, dict):
        raise HTTPException(status_code=401, detail="Invalid Afterglow service context")

    expires_at = context.get("exp")
    if (
        not isinstance(expires_at, int)
        or expires_at < time.time()
        or expires_at > time.time() + _MAX_CONTEXT_TTL_SECONDS
    ):
        raise HTTPException(status_code=401, detail="Expired Afterglow service context")
    if context.get("method") != request.method or context.get("path") != request.url.path:
        raise HTTPException(status_code=401, detail="Afterglow service context does not match this request")

    for field in ("token", "user_id", "project_id"):
        if not isinstance(context.get(field), str) or not context[field]:
            raise HTTPException(status_code=401, detail="Incomplete Afterglow service context")
    roles = context.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise HTTPException(status_code=401, detail="Invalid Afterglow service roles")
    if not isinstance(context.get("is_system_admin", False), bool):
        raise HTTPException(status_code=401, detail="Invalid Afterglow service context")
    return context


async def get_token_info(request: Request) -> dict[str, Any]:
    return _verified_context(request)


async def require_admin(request: Request) -> dict[str, Any]:
    context = _verified_context(request)
    if not context["is_system_admin"]:
        raise HTTPException(status_code=403, detail="System administrator role is required")
    return context


def get_os_conn(token_info: dict[str, Any]) -> openstack.connection.Connection:
    settings = get_settings()
    return openstack.connect(
        load_envvars=False,
        load_yaml_config=False,
        auth_url=settings.os_auth_url,
        auth_type="token",
        token=token_info["token"],
        project_id=token_info["project_id"],
        region_name=settings.os_region_name,
        interface=settings.os_interface,
        api_timeout=30,
        verify=settings.ssl_verify,
    )
