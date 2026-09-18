from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from palimpsest_hub.auth import require_admin, require_token
from palimpsest_hub.config import get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch):
    values = {
        "DATABASE_URL": "mysql+asyncmy://user:pass@db/palimpsest",
        "REDIS_URL": "redis://redis/0",
        "PALIMPSEST_HUB_LOCAL_PATH": "/var/lib/palimpsest",
        "OS_AUTH_URL": "https://keystone.example/v3",
        "OS_USERNAME": "palimpsest",
        "OS_PASSWORD": "password",
        "OS_PROJECT_NAME": "palimpsest-service",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_admin_client_uses_configured_project_name():
    password_plugin = object()
    session = object()
    client = object()
    with (
        patch("palimpsest_hub.auth.v3.Password", return_value=password_plugin) as password,
        patch("palimpsest_hub.auth.ks_session.Session", return_value=session) as session_factory,
        patch("keystoneclient.v3.client.Client", return_value=client) as client_factory,
    ):
        from palimpsest_hub.auth import _get_admin_ks_client

        result = _get_admin_ks_client()

    assert result is client
    assert password.call_args.kwargs["project_name"] == "palimpsest-service"
    session_factory.assert_called_once_with(auth=password_plugin, timeout=15, verify=True)
    client_factory.assert_called_once_with(session=session)


def make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/v1/layers", "headers": raw_headers})


@pytest.mark.asyncio
async def test_require_token_missing_header_raises_401():
    req = make_request()
    with pytest.raises(HTTPException) as exc_info:
        await require_token(req, x_auth_token=None, x_project_id=None)
    assert exc_info.value.status_code == 401
    assert "X-Auth-Token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_token_validates_and_stores_token_info():
    req = make_request({"X-Auth-Token": "valid-token", "X-Project-Id": "proj-1"})
    valid_info = {
        "token": "valid-token",
        "project_id": "proj-1",
        "project_name": "demo",
        "user_id": "user-123",
        "username": "user",
        "expires_at": "2026-08-01T12:00:00Z",
        "roles": ["member"],
        "is_system_admin": False,
    }
    with patch("palimpsest_hub.auth.validate_token", return_value=valid_info):
        info = await require_token(req, x_auth_token="valid-token", x_project_id="proj-1")
    assert info["project_id"] == "proj-1"
    assert info["user_id"] == "user-123"
    assert req.state.token_info == info


@pytest.mark.asyncio
async def test_require_token_rejects_missing_project_scope():
    req = make_request({"X-Auth-Token": "unscoped-token"})
    unscoped_info = {
        "token": "unscoped-token",
        "project_id": "",
        "user_id": "user-123",
        "roles": [],
        "is_system_admin": False,
    }
    with (
        patch("palimpsest_hub.auth.validate_token", return_value=unscoped_info),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_token(req, x_auth_token="unscoped-token", x_project_id=None)
    assert exc_info.value.status_code == 401
    assert "project-scoped" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_allows_system_admin_and_rejects_non_admin():
    admin_info = {"project_id": "proj-1", "is_system_admin": True}
    non_admin_info = {"project_id": "proj-1", "is_system_admin": False}

    assert require_admin(token_info=admin_info) == admin_info

    with pytest.raises(HTTPException) as exc_info:
        require_admin(token_info=non_admin_info)
    assert exc_info.value.status_code == 403
