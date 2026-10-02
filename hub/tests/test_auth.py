from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from palimpsest_hub.auth import (
    _reader_client_for,
    get_os_conn,
    get_package_member_info,
    require_admin,
    require_token,
    validate_package_owner,
    validate_token,
)
from palimpsest_hub.config import default_project_namespace, get_settings


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
    values.update({
        "OS_READER_USERNAME": "read-only-validator",
        "OS_READER_PASSWORD": "synthetic-read-secret",
        "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS": json.dumps(["e" * 32]),
        "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS": json.dumps(["d" * 32]),
        "PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS": "{}",
        "PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN": "https://packages.example",
    })
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    _reader_client_for.cache_clear()
    yield
    get_settings.cache_clear()
    _reader_client_for.cache_clear()




def make_request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/v1/layers", "headers": raw_headers})


@pytest.mark.asyncio
async def test_require_token_missing_header_raises_401():
    req = make_request()
    with pytest.raises(HTTPException) as exc_info:
        await require_token(req, x_auth_token=None, x_project_id=None)
    assert exc_info.value.status_code == 401




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


def test_require_admin_rejects_non_system_admin():
    non_admin_info = {"project_id": "proj-1", "is_system_admin": False}
    with pytest.raises(HTTPException) as exc_info:
        require_admin(token_info=non_admin_info)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_token_validation_error_never_logs_token_or_exception():
    from palimpsest_hub.auth import _logger

    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())
            assert record.exc_info is None

    handler = Capture()
    _logger.addHandler(handler)
    original_level = _logger.level
    _logger.setLevel(logging.INFO)
    try:
        with (
            patch("palimpsest_hub.auth.validate_token", side_effect=RuntimeError("secret-auth-value")),
            pytest.raises(HTTPException) as exc_info,
        ):
            await require_token(make_request(), x_auth_token="secret-header-value", x_project_id=None)
    finally:
        _logger.removeHandler(handler)
        _logger.setLevel(original_level)
    assert exc_info.value.status_code == 503
    assert "secret-auth-value" not in "\n".join(messages)
    assert "secret-header-value" not in "\n".join(messages)


PROJECT_A = "a" * 32
PROJECT_B = "b" * 32
FEDERATED_OWNER = "f" * 64
ORIGINAL_SUBJECT = "original-project-subject"


@pytest.fixture
def keystone_http(monkeypatch: pytest.MonkeyPatch):
    """Real SDK requests against an isolated subject-validation identity boundary."""
    state = {
        "user_id": FEDERATED_OWNER,
        "project_id": PROJECT_A,
        "user_enabled": True,
        "project_enabled": True,
        "validator_roles": ["reader"],
        "token_roles": ["member"],
        "project_roles": ["member"],
        "other_roles": [],
        "unavailable": False,
        "requests": [],
    }

    def token_data(*, reader=False, project_id=None):
        now = datetime.now(UTC)
        data = {
            "methods": ["password" if reader else "oidc"],
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "issued_at": now.isoformat(),
            "user": {
                "id": "c" * 32 if reader else state["user_id"],
                "name": "read-only-validator" if reader else "federated-member",
                "domain": {"id": "default", "name": "Default"},
            },
            "roles": [{"id": f"role-{name}", "name": name} for name in state["validator_roles" if reader else "token_roles"]],
            "catalog": [],
        }
        if reader:
            data["system"] = {"all": True}
        elif project_id is not None or state["project_id"]:
            data["project"] = {
                "id": project_id or state["project_id"],
                "name": "selected-project" if project_id != PROJECT_B else "default-project",
                "domain": {"id": "default", "name": "Default"},
            }
        return data

    class IdentityServer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, body, subject=None):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if subject:
                self.send_header("X-Subject-Token", subject)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            identity = body["auth"]["identity"]
            state["requests"].append({"method": "POST", "methods": identity["methods"]})
            if identity["methods"] == ["token"]:
                self.send_json(201, {"token": token_data(project_id=PROJECT_B)}, "replacement-default-project-token")
            elif identity.get("password", {}).get("user", {}).get("name") == "read-only-validator":
                self.send_json(201, {"token": token_data(reader=True)}, "validator-subject")
            else:
                self.send_json(403, {"error": {"message": "Administrator validator authentication is forbidden"}})

        def do_GET(self):
            parsed = urlsplit(self.path)
            state["requests"].append({
                "method": "GET", "path": parsed.path,
                "actor_token": self.headers.get("X-Auth-Token"),
                "subject_token": self.headers.get("X-Subject-Token"),
            })
            if parsed.path == "/compute/ping":
                self.send_json(200, {"project_id": PROJECT_A, "original_subject": self.headers.get("X-Auth-Token") == ORIGINAL_SUBJECT})
                return
            if state["unavailable"]:
                self.send_json(503, {"error": {"message": "Identity temporarily unavailable"}})
                return
            if self.headers.get("X-Auth-Token") != "validator-subject":
                self.send_json(401, {"error": {"message": "Read-only validator required"}})
                return
            if parsed.path == "/v3/auth/tokens":
                if self.headers.get("X-Subject-Token") == ORIGINAL_SUBJECT:
                    self.send_json(200, {"token": token_data()}, ORIGINAL_SUBJECT)
                else:
                    self.send_json(401, {"error": {"message": "Invalid subject"}})
            elif parsed.path == f"/v3/users/{state['user_id']}":
                self.send_json(200, {"user": {"id": state["user_id"], "name": "federated-member", "enabled": state["user_enabled"]}})
            elif parsed.path == f"/v3/projects/{state['project_id']}":
                self.send_json(200, {"project": {"id": state["project_id"], "name": "selected-project", "enabled": state["project_enabled"], "domain_id": "default"}})
            elif parsed.path == "/v3/role_assignments":
                query = parse_qs(parsed.query)
                assignments = []
                if not query.get("scope.system"):
                    assignments.extend({"user": {"id": state["user_id"]}, "scope": {"project": {"id": state["project_id"]}}, "role": {"id": name, "name": name}} for name in state["project_roles"])
                    assignments.extend({"user": {"id": state["user_id"]}, "scope": {"project": {"id": PROJECT_B}}, "role": {"id": name, "name": name}} for name in state["other_roles"])
                self.send_json(200, {"role_assignments": assignments})
            else:
                self.send_json(404, {"error": {"message": "Unknown identity resource"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), IdentityServer)
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("OS_AUTH_URL", state["url"] + "/v3")
    get_settings.cache_clear()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("project_header", ["", PROJECT_A])
def test_original_scope_and_federated_owner_survive_subject_validation(keystone_http, project_header):
    info = validate_token(ORIGINAL_SUBJECT, project_header)
    assert (info["project_id"], info["user_id"], info["token"]) == (PROJECT_A, FEDERATED_OWNER, ORIGINAL_SUBJECT)
    assert info["auth_ref"].auth_token == ORIGINAL_SUBJECT
    assert not any(event.get("methods") == ["token"] for event in keystone_http["requests"])
    validation = next(event for event in keystone_http["requests"] if event.get("path") == "/v3/auth/tokens")
    assert validation["actor_token"] == "validator-subject"
    assert validation["subject_token"] == ORIGINAL_SUBJECT


def test_project_header_is_assertion_not_rescope(keystone_http):
    with pytest.raises(HTTPException) as raised:
        validate_token(ORIGINAL_SUBJECT, PROJECT_B)
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "PROJECT_SCOPE_MISMATCH"
    assert not any(event.get("methods") == ["token"] for event in keystone_http["requests"])


def test_invalid_subject_is_not_identity_outage(keystone_http):
    with pytest.raises(HTTPException) as raised:
        validate_token("invalid-subject")
    assert raised.value.status_code == 401


def test_admin_validator_is_not_accepted_as_reader(keystone_http):
    keystone_http["validator_roles"] = ["reader", "admin"]
    with pytest.raises(HTTPException) as raised:
        validate_token(ORIGINAL_SUBJECT)
    assert raised.value.status_code == 503
    assert not any(event.get("path") == "/v3/auth/tokens" for event in keystone_http["requests"])


@pytest.mark.asyncio
async def test_openstack_consumer_keeps_original_subject_without_exchange(keystone_http):
    info = validate_token(ORIGINAL_SUBJECT)
    connections = get_os_conn(token_info=info)
    conn = await anext(connections)
    try:
        result = conn.session.get(keystone_http["url"] + "/compute/ping").json()
        assert result == {"project_id": PROJECT_A, "original_subject": True}
    finally:
        await connections.aclose()
    assert not any(event.get("methods") == ["token"] for event in keystone_http["requests"])


@pytest.mark.asyncio
async def test_membership_removal_blocks_same_valid_token(keystone_http):
    info = validate_token(ORIGINAL_SUBJECT)
    member = await get_package_member_info(token_info=info)
    assert member["user_id"] == FEDERATED_OWNER and member["can_write"] is True
    keystone_http["project_roles"] = []
    with pytest.raises(HTTPException) as raised:
        await get_package_member_info(token_info=info)
    assert raised.value.status_code == 403


@pytest.mark.parametrize("field", ["user_enabled", "project_enabled"])
def test_disabled_owner_or_project_cannot_authorize_key(keystone_http, field):
    keystone_http[field] = False
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 403


def test_admin_assignment_elsewhere_blocks_member_only_subject(keystone_http):
    keystone_http["other_roles"] = ["admin"]
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "ADMIN_CREDENTIAL_FORBIDDEN"


def test_role_from_other_project_never_grants_current_membership(keystone_http):
    keystone_http["project_roles"] = []
    keystone_http["other_roles"] = ["member"]
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 403


def test_identity_outage_cannot_use_previous_owner_approval(keystone_http):
    assert validate_package_owner(FEDERATED_OWNER, PROJECT_A)["can_write"] is True
    keystone_http["unavailable"] = True
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 503


def test_missing_protected_policy_disables_package_authority(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS", "[]")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 503


def test_protected_federated_owner_is_exact_and_supported(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS", json.dumps([FEDERATED_OWNER]))
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 403
    assert raised.value.detail["code"] == "ADMIN_CREDENTIAL_FORBIDDEN"


def test_case_only_project_assertion_is_not_same_identity(keystone_http):
    with pytest.raises(HTTPException) as raised:
        validate_token(ORIGINAL_SUBJECT, PROJECT_A.upper())
    assert raised.value.status_code == 403


def test_reserved_namespace_cannot_bind_foreign_project(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS", json.dumps({f"p-{PROJECT_B}": PROJECT_A}))
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()


def test_opaque_project_namespace_is_stable_and_case_sensitive():
    assert default_project_namespace(PROJECT_A) == f"p-{PROJECT_A}"
    assert default_project_namespace("f" * 64) != default_project_namespace("F" * 64)
    assert len(default_project_namespace("f" * 64)) <= 63
