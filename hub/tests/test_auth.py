from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from palimpsest_hub.auth import get_token_info
from palimpsest_hub.config import get_settings

_SECRET = "a" * 32


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch):
    values = {
        "DATABASE_URL": "mysql+asyncmy://user:pass@db/palimpsest",
        "REDIS_URL": "redis://redis/0",
        "PALIMPSEST_HUB_LOCAL_PATH": "/var/lib/palimpsest",
        "PALIMPSEST_HUB_SIGNING_SECRET": _SECRET,
        "OS_AUTH_URL": "https://keystone.example/v3",
        "OS_USERNAME": "palimpsest",
        "OS_PASSWORD": "password",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def signed_request(*, method: str = "GET", path: str = "/v1/layers", signature: str | None = None) -> Request:
    context = {
        "exp": int(time.time()) + 30,
        "method": method,
        "path": path,
        "token": "keystone-token",
        "user_id": "user-1",
        "project_id": "project-1",
        "roles": ["member"],
        "is_system_admin": False,
    }
    raw = json.dumps(context, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature = signature or hmac.new(_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [
                (b"x-afterglow-context", encoded.encode()),
                (b"x-afterglow-signature", signature.encode()),
            ],
        }
    )


@pytest.mark.asyncio
async def test_signed_afterglow_context_is_accepted():
    context = await get_token_info(signed_request())

    assert context["project_id"] == "project-1"
    assert context["token"] == "keystone-token"


@pytest.mark.asyncio
async def test_context_cannot_be_replayed_to_another_path():
    request = signed_request(path="/v1/layers")
    request.scope["path"] = "/v1/images"

    with pytest.raises(HTTPException, match="does not match") as exc_info:
        await get_token_info(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected():
    request = signed_request(signature="0" * 64)

    with pytest.raises(HTTPException, match="Invalid Afterglow service signature") as exc_info:
        await get_token_info(request)

    assert exc_info.value.status_code == 401
