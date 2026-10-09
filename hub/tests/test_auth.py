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
    values.update(
        {
            "OS_READER_USERNAME": "read-only-validator",
            "OS_READER_PASSWORD": "synthetic-read-secret",
            "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS": json.dumps(["e" * 32]),
            "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS": json.dumps(["d" * 32]),
            "PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS": "{}",
            "PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN": "https://packages.example",
        }
    )
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


# Synthetic Keystone owns these initial links; tests mutate them to prove revocation.
SERVICE_EDGES = {
    "palimpsest_admin": ["palimpsest_editor", "palimpsest-keys_admin"],
    "palimpsest_editor": ["palimpsest_user", "palimpsest-publish_editor", "palimpsest-keys_editor"],
    "palimpsest_user": ["palimpsest_reader", "palimpsest-download_user"],
    "palimpsest_reader": ["palimpsest-inventory_reader"],
    "palimpsest-download_user": ["palimpsest_reader"],
    "palimpsest-publish_editor": ["palimpsest_reader"],
    "palimpsest-keys_editor": ["palimpsest_reader"],
    "palimpsest-keys_admin": ["palimpsest_reader"],
    "member": ["reader"],
}


@pytest.fixture
def keystone_http(monkeypatch: pytest.MonkeyPatch):
    """Real SDK requests against an isolated subject-validation identity boundary."""
    state = {
        "user_id": FEDERATED_OWNER,
        "project_id": PROJECT_A,
        "user_enabled": True,
        "project_enabled": True,
        "validator_roles": ["reader"],
        "token_roles": ["member", "palimpsest_admin"],
        "project_roles": ["member", "palimpsest_admin"],
        "other_roles": [],
        "system_assignments": [],
        "unavailable": False,
        "requests": [],
    }
    state["edges"] = {name: list(implied) for name, implied in SERVICE_EDGES.items()}
    names = {"admin", "manager", "service", "project_owner", "project_admin", "project_member", "project_reader"}
    names.update(SERVICE_EDGES)
    names.update(name for implied in SERVICE_EDGES.values() for name in implied)
    state["directory"] = [{"id": "role-" + name, "name": name, "domain_id": None} for name in sorted(names)]
    state["directory_unavailable"] = False
    state["directory_truncated"] = False

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
            "roles": [
                {"id": f"role-{name}", "name": name} for name in state["validator_roles" if reader else "token_roles"]
            ],
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
            state["requests"].append(
                {
                    "method": "GET",
                    "path": parsed.path,
                    "actor_token": self.headers.get("X-Auth-Token"),
                    "subject_token": self.headers.get("X-Subject-Token"),
                }
            )
            if parsed.path == "/compute/ping":
                self.send_json(
                    200,
                    {"project_id": PROJECT_A, "original_subject": self.headers.get("X-Auth-Token") == ORIGINAL_SUBJECT},
                )
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
            elif parsed.path == "/v3/roles":
                if state["directory_unavailable"]:
                    self.send_json(503, {"error": {"message": "Role directory unavailable"}})
                else:
                    self.send_json(
                        200,
                        {
                            "roles": [role for role in state["directory"] if role.get("domain_id") is None],
                            "links": {"next": None},
                            "truncated": state["directory_truncated"],
                        },
                    )
            elif parsed.path == "/v3/role_inferences":
                self.send_json(
                    200,
                    {
                        "role_inferences": [
                            {
                                "prior_role": {"id": "role-" + prior},
                                "implies": [{"id": "role-" + name} for name in implied],
                            }
                            for prior, implied in state["edges"].items()
                        ],
                        "links": {"next": None},
                    },
                )
            elif parsed.path.startswith("/v3/roles/"):
                role_id = parsed.path.rsplit("/", 1)[-1]
                role = next((role for role in state["directory"] if role["id"] == role_id), None)
                self.send_json(200, {"role": role}) if role else self.send_json(
                    404, {"error": {"message": "Missing role"}}
                )
            elif parsed.path == f"/v3/users/{state['user_id']}":
                self.send_json(
                    200,
                    {"user": {"id": state["user_id"], "name": "federated-member", "enabled": state["user_enabled"]}},
                )
            elif parsed.path == f"/v3/projects/{state['project_id']}":
                self.send_json(
                    200,
                    {
                        "project": {
                            "id": state["project_id"],
                            "name": "selected-project",
                            "enabled": state["project_enabled"],
                            "domain_id": "default",
                        }
                    },
                )
            elif parsed.path == "/v3/role_assignments":
                query = parse_qs(parsed.query)
                assignments = []
                if not query.get("scope.system"):
                    assignments.extend(
                        {
                            "user": {"id": state["user_id"]},
                            "scope": {"project": {"id": state["project_id"]}},
                            "role": {"id": "role-" + name, "name": name},
                        }
                        for name in state["project_roles"]
                    )
                    assignments.extend(
                        {
                            "user": {"id": state["user_id"]},
                            "scope": {"project": {"id": PROJECT_B}},
                            "role": {"id": "role-" + name, "name": name},
                        }
                        for name in state["other_roles"]
                    )
                elif not query.get("effective"):
                    assignments.extend(state["system_assignments"])
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


def test_direct_system_admin_recognition_and_revocation(keystone_http):
    keystone_http["token_roles"] = ["admin"]
    keystone_http["system_assignments"] = [
        {
            "user": {"id": FEDERATED_OWNER},
            "scope": {"system": {"all": True}},
            "role": {"id": "role-admin", "name": "admin"},
        }
    ]
    info = validate_token(ORIGINAL_SUBJECT, PROJECT_A)
    assert info["is_system_admin"] is True

    keystone_http["system_assignments"] = []
    info = validate_token(ORIGINAL_SUBJECT, PROJECT_A)
    assert info["is_system_admin"] is False
    with pytest.raises(HTTPException) as exc_info:
        require_admin(token_info=info)
    assert exc_info.value.status_code == 403


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


@pytest.mark.parametrize("elevated_role", ["admin", "manager", "service"])
def test_admin_validator_is_not_accepted_as_reader(keystone_http, elevated_role):
    keystone_http["validator_roles"] = ["reader", elevated_role]
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,expected",
    [
        ("palimpsest_reader", {"palimpsest-inventory_reader"}),
        ("palimpsest_user", {"palimpsest-inventory_reader", "palimpsest-download_user"}),
        (
            "palimpsest_editor",
            {
                "palimpsest-inventory_reader",
                "palimpsest-download_user",
                "palimpsest-publish_editor",
                "palimpsest-keys_editor",
            },
        ),
        (
            "palimpsest_admin",
            {
                "palimpsest-inventory_reader",
                "palimpsest-download_user",
                "palimpsest-publish_editor",
                "palimpsest-keys_editor",
                "palimpsest-keys_admin",
            },
        ),
        *[
            (leaf, {leaf, "palimpsest-inventory_reader"})
            for leaf in (
                "palimpsest-inventory_reader",
                "palimpsest-download_user",
                "palimpsest-publish_editor",
                "palimpsest-keys_editor",
                "palimpsest-keys_admin",
            )
        ],
    ],
)
async def test_current_role_id_graph_resolves_parents_and_exact_leaves(keystone_http, role, expected):
    keystone_http["token_roles"] = keystone_http["project_roles"] = ["member", role]
    original = validate_token(ORIGINAL_SUBJECT)
    member = await get_package_member_info(original)
    assert member["package_capabilities"] == expected
    assert member["token"] == ORIGINAL_SUBJECT and member["user_id"] == FEDERATED_OWNER
    assert member["auth_ref"] is original["auth_ref"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roles",
    [
        ["member"],
        ["reader"],
        ["member", "project_admin"],
        ["member", "project_owner"],
        ["palimpsest_admin"],
        ["reader", "palimpsest-publish_editor"],
        ["reader", "palimpsest-download_user"],
        ["member", "manager", "palimpsest_admin"],
    ],
)
async def test_baseline_and_service_authority_are_both_required(keystone_http, roles):
    keystone_http["token_roles"] = keystone_http["project_roles"] = roles
    with pytest.raises(HTTPException) as raised:
        await get_package_member_info(validate_token(ORIGINAL_SUBJECT))
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_removed_keystone_implication_revokes_retained_parent_token(keystone_http):
    original = validate_token(ORIGINAL_SUBJECT)
    assert (await get_package_member_info(original))["can_write"]
    keystone_http["edges"]["palimpsest_editor"].remove("palimpsest-publish_editor")
    assert not (await get_package_member_info(original))["can_write"]
    assert "palimpsest-publish_editor" not in validate_package_owner(FEDERATED_OWNER, PROJECT_A)["roles"]


@pytest.mark.asyncio
async def test_current_assignment_and_token_authority_intersect(keystone_http):
    keystone_http["token_roles"] = ["reader", "palimpsest_reader"]
    original = validate_token(ORIGINAL_SUBJECT)
    assert (await get_package_member_info(original))["package_capabilities"] == {"palimpsest-inventory_reader"}
    keystone_http["token_roles"] = ["member", "palimpsest_admin"]
    original = validate_token(ORIGINAL_SUBJECT)
    keystone_http["project_roles"] = ["reader", "palimpsest_reader"]
    assert (await get_package_member_info(original))["package_capabilities"] == {"palimpsest-inventory_reader"}


@pytest.mark.parametrize("change", ["domain", "duplicate", "unknown", "cycle", "unavailable", "truncated"])
def test_role_directory_cannot_be_replaced_by_name_claims(keystone_http, change):
    if change == "domain":
        next(role for role in keystone_http["directory"] if role["name"] == "palimpsest_admin")["domain_id"] = "tenant"
    elif change == "duplicate":
        keystone_http["directory"].append({"id": "alias", "name": "palimpsest_admin", "domain_id": None})
    elif change == "unknown":
        keystone_http["edges"]["palimpsest_admin"].append("missing")
    elif change == "cycle":
        keystone_http["edges"]["palimpsest_reader"].append("palimpsest_admin")
    elif change == "truncated":
        keystone_http["directory_truncated"] = True
    else:
        keystone_http["directory_unavailable"] = True
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == (403 if change == "domain" else 503)


def test_transitive_admin_alias_is_not_service_authority(keystone_http):
    keystone_http["edges"]["palimpsest_admin"].append("admin")
    with pytest.raises(HTTPException) as raised:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert raised.value.status_code == 403


@pytest.fixture
async def native_http(keystone_http, tmp_path, monkeypatch):
    """Real loopback Hub + Keystone HTTP; fresh SQLite/CAS, no production app lifespan."""
    import asyncio
    import socket
    import time
    from types import SimpleNamespace

    import httpx
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from palimpsest_hub.api import builds, hub, packages
    from palimpsest_hub.models import Base
    from palimpsest_hub.services import image_exports, package_registry
    from palimpsest_hub.services.hub_store import LocalPathBlobStore

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'http-smoke.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    for module in (hub, image_exports, package_registry):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
        monkeypatch.setattr(module, "get_blob_store", lambda: store)
    app = FastAPI()
    for router in (packages.router, hub.router, builds.router):
        app.include_router(router, prefix="/v1")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert server.started, "isolated Hub did not start"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=20) as client:
            yield SimpleNamespace(client=client, identity=keystone_http, factory=factory, store=store)
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        listener.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,inventory,download,publish,issue,revoke",
    [
        ("palimpsest_reader", True, False, False, False, False),
        ("palimpsest_user", True, True, False, False, False),
        ("palimpsest_editor", True, True, True, True, False),
        ("palimpsest_admin", True, True, True, True, True),
        ("palimpsest-inventory_reader", True, False, False, False, False),
        ("palimpsest-download_user", True, True, False, False, False),
        ("palimpsest-publish_editor", True, False, True, False, False),
        ("palimpsest-keys_editor", True, False, False, True, False),
        ("palimpsest-keys_admin", True, False, False, False, True),
    ],
)
async def test_native_http_service_matrix(native_http, role, inventory, download, publish, issue, revoke):
    from test_packages import image_archive
    from test_packages import publish as publish_package

    h = native_http
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    namespace = default_project_namespace(PROJECT_A)
    base = f"/v1/projects/{namespace}"
    registered = await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})
    assert registered.status_code == 201
    actions = ["packages:inventory", "packages:read", "packages:write", "cache:read", "cache:write"]
    key_request = {"name": "http smoke", "scope": {"packages": ["test"]}, "actions": actions}
    issued = await h.client.post(base + "/keys", headers=token, json=key_request)
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    archive, body, graph = image_archive()
    assert (await publish_package(h, namespace, credential, archive, body))[0].status_code == 201
    # The same previously issued key is attenuated by the current graph/assignments.
    h.identity["project_roles"] = h.identity["token_roles"] = ["member", role]
    version = base + "/versions/" + body["root_digest"]
    for headers in (token, credential):
        result = await h.client.get(base + "/packages", headers=headers)
        assert result.status_code == (200 if inventory else 403)
        for suffix in ("", "/download", "/blobs/" + next(iter(graph))):
            response = await h.client.get(version + suffix, headers=headers, params={"package": "test"})
            assert response.status_code == (200 if (inventory if not suffix else download) else 403)
            if suffix == "/download" and download:
                assert response.content == archive
        other = await h.client.get(version, headers=headers, params={"package": "other"})
        assert other.status_code in {403, 404}
    started = await h.client.post(base + "/uploads", headers=credential, params={"package": "test"}, json=body)
    assert started.status_code == (201 if publish else 403)
    cache_body = {
        "build_key": body["root_digest"],
        "cache_scope": "default",
        "platform": "linux/amd64",
        "builder_fingerprint": body["root_digest"],
        "archive_digest": body["archive_digest"],
        "archive_size_bytes": len(archive),
    }
    cache_started = await h.client.post(
        base + "/cache/uploads",
        headers=credential,
        params={"package": "test"},
        json=cache_body,
    )
    assert cache_started.status_code == (201 if publish else 403)
    legacy_write = await h.client.post("/v1/uploads", headers=token, json={})
    assert legacy_write.status_code == (200 if publish else 403)
    legacy_inventory = await h.client.get("/v1/layers", headers=token)
    assert legacy_inventory.status_code == (200 if inventory else 403)
    # Authorization precedes missing resource lookup, including download credential issuance.
    missing = "11111111-1111-1111-1111-111111111111"
    ticket = await h.client.post(f"/v1/image-exports/{missing}/download-token", headers=token)
    assert ticket.status_code == (404 if download else 403)
    subset = {**key_request, "actions": ["packages:inventory"]}
    created = await h.client.post(base + "/keys", headers=token, json=subset)
    assert created.status_code == (201 if issue and inventory else 403)
    rejected = await h.client.post(base + "/keys", headers=token, json={**key_request, "actions": ["vm:launch"]})
    assert rejected.status_code == 422
    revoked = await h.client.delete(base + "/keys/" + issued.json()["key"]["key_id"], headers=token)
    assert revoked.status_code == (204 if revoke else 403)
    # A service admin is not the verified system admin; a package key is not a VM credential.
    build = {"name": "forbidden", "recipe": "FROM fixture", "base_digest": body["root_digest"]}
    assert (await h.client.post("/v1/builds", headers=token, json=build)).status_code == 403
    assert (await h.client.post("/v1/builds", headers=credential, json=build)).status_code == 401
    assert (await h.client.delete("/v1/layers/" + body["root_digest"], headers=token)).status_code == 403
    assert not any(event.get("methods") == ["token"] for event in h.identity["requests"])


@pytest.mark.asyncio
async def test_native_http_key_graph_downgrade_and_namespace_denial(native_http):
    from test_packages import image_archive, staged

    h = native_http
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    namespace = default_project_namespace(PROJECT_A)
    base = f"/v1/projects/{namespace}"
    assert (await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})).status_code == 201
    request = {"name": "narrow writer", "scope": {"packages": ["test"]}, "actions": ["packages:write"]}
    h.identity["project_roles"] = h.identity["token_roles"] = [
        "member",
        "palimpsest-publish_editor",
        "palimpsest-keys_editor",
    ]
    issued = await h.client.post(base + "/keys", headers=token, json=request)
    assert issued.status_code == 201
    # Publish-only is not forced to request download and cannot delegate it.
    assert (
        await h.client.post(base + "/keys", headers=token, json={**request, "actions": ["packages:read"]})
    ).status_code == 403
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    archive, body, _ = image_archive()
    path, _ = await staged(h, namespace, credential, archive, body)
    h.identity["project_roles"] = ["member", "palimpsest_user"]
    assert (await h.client.put(path, headers=credential, params={"package": "test"}, json={})).status_code == 403
    assert (
        await h.client.patch(
            path,
            headers={**credential, "Upload-Offset": str(len(archive)), "Content-Type": "application/octet-stream"},
            params={"package": "test"},
            content=b"x",
        )
    ).status_code == 403
    assert (await h.client.post(base + "/keys", headers=token, json=request)).status_code == 403
    assert (await h.client.get("/v1/projects/foreign/packages", headers=credential)).status_code == 403
    assert (await h.client.get(base + "/packages", headers={**token, "X-Project-Id": PROJECT_B})).status_code == 403
    h.identity["project_roles"] = h.identity["token_roles"] = ["member", "palimpsest_admin"]
    h.identity["edges"]["palimpsest_editor"].remove("palimpsest-publish_editor")
    assert (await h.client.put(path, headers=credential, params={"package": "test"}, json={})).status_code == 403
    h.identity["directory_unavailable"] = True
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 503


@pytest.mark.asyncio
async def test_fine_grained_inventory_dependency_is_actual_not_hardcoded(keystone_http):
    keystone_http["project_roles"] = keystone_http["token_roles"] = ["member", "palimpsest-publish_editor"]
    original = validate_token(ORIGINAL_SUBJECT)
    assert (await get_package_member_info(original))["package_capabilities"] == {
        "palimpsest-publish_editor",
        "palimpsest-inventory_reader",
    }
    keystone_http["edges"]["palimpsest-publish_editor"] = []
    assert (await get_package_member_info(original))["package_capabilities"] == {"palimpsest-publish_editor"}


def test_unrelated_domain_role_inferences_do_not_disable_global_package_authority(keystone_http):
    keystone_http["directory"].extend(
        [
            {"id": "role-domain-parent", "name": "domain-parent", "domain_id": "tenant"},
            {"id": "role-domain-leaf", "name": "palimpsest-publish_editor", "domain_id": "tenant"},
        ]
    )
    keystone_http["edges"]["domain-parent"] = ["domain-leaf"]
    assert validate_package_owner(FEDERATED_OWNER, PROJECT_A)["can_write"] is True


def test_mixed_case_unrelated_assignment_uses_exact_directory_name(keystone_http):
    keystone_http["directory"].append(
        {"id": "role-UnrelatedMixedCase", "name": "UnrelatedMixedCase", "domain_id": None}
    )
    keystone_http["project_roles"].append("UnrelatedMixedCase")
    assert validate_package_owner(FEDERATED_OWNER, PROJECT_A)["can_write"] is True
