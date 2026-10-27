from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from palimpsest_hub.api import hub as hub_api
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
        "token_user_domain_id": "default",
        "token_user_domain_name": "Default",
        "current_user_domain_id": "default",
        "project_domain_id": "default",
        "target_domain_id": "target-domain",
        "domains": {
            "default": {"id": "default", "name": "Default", "enabled": True},
            "target-domain": {"id": "target-domain", "name": "TargetDomain", "enabled": True},
        },
        "domain_get_unavailable": set(),
        "domains_unavailable": False,
        "domains_not_found": False,
        "domains_truncated": False,
        "domains_next": False,
        "service_user_id": "native-export-trustee",
        "service_user_name": "palimpsest",
        "service_domain_id": "default",
        "service_enabled": True,
        "users_unavailable": False,
        "users_not_found": False,
        "users_truncated": False,
        "users_next": False,
        "extra_users": [],
        "validator_roles": ["reader"],
        "token_roles": ["member", "palimpsest_admin"],
        "project_roles": ["member", "palimpsest_admin"],
        "other_roles": [],
        "system_roles": [],
        "groups": [],
        "group_system_roles": {},
        "raw_user_assignments": None,
        "raw_group_assignments": {},
        "groups_unavailable": False,
        "groups_truncated": False,
        "groups_next": False,
        "assignments_unavailable": False,
        "assignments_truncated": False,
        "assignments_next": False,
        "effective_all_next": False,
        "effective_all_truncated": False,
        "extra_effective_assignments": [],
        "group_assignments_unavailable": False,
        "token_methods": ["oidc"],
        "token_markers": {},
        "collection_metadata": {},
        "subject_scope": "project",
        "delegated": False,
        "target_enabled": True,
        "system_role_id": None,
        "unavailable": False,
        "requests": [],
    }
    state["edges"] = {name: list(implied) for name, implied in SERVICE_EDGES.items()}
    names = {
        "admin",
        "manager",
        "service",
        "project_owner",
        "project_admin",
        "project_member",
        "project_reader",
        "waygate_admin",
    }
    names.update(SERVICE_EDGES)
    names.update(name for implied in SERVICE_EDGES.values() for name in implied)
    state["directory"] = [{"id": "role-" + name, "name": name, "domain_id": None} for name in sorted(names)]
    state["directory_unavailable"] = False
    state["directory_truncated"] = False

    def token_data(*, reader=False, project_id=None):
        now = datetime.now(UTC)
        data = {
            "methods": ["password"] if reader else state["token_methods"],
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "issued_at": now.isoformat(),
            "user": {
                "id": "c" * 32 if reader else state["user_id"],
                "name": "read-only-validator" if reader else "federated-member",
                "domain": {"id": "default", "name": "Default"}
                if reader
                else {
                    "id": state["token_user_domain_id"],
                    "name": state["token_user_domain_name"],
                },
            },
            "roles": [
                {"id": f"role-{name}", "name": name} for name in state["validator_roles" if reader else "token_roles"]
            ],
            "catalog": [],
        }
        if reader or state["subject_scope"] == "system":
            data["system"] = {"all": True}
        elif state["subject_scope"] == "project" and (project_id is not None or state["project_id"]):
            data["project"] = {
                "id": project_id or state["project_id"],
                "name": "selected-project" if project_id != PROJECT_B else "default-project",
                "domain": {"id": "default", "name": "Default"},
            }
        if not reader and state["delegated"]:
            data["OS-TRUST:trust"] = {"id": "synthetic-trust", "trustor_user": {"id": state["user_id"]}}
        if not reader:
            data.update(state["token_markers"])
        return data

    def native_users():
        return {
            state["user_id"]: {
                "id": state["user_id"],
                "name": "federated-member",
                "domain_id": state["current_user_domain_id"],
                "enabled": state["user_enabled"],
            },
            "c" * 32: {"id": "c" * 32, "name": "read-only-validator", "domain_id": "default", "enabled": True},
            state["service_user_id"]: {
                "id": state["service_user_id"],
                "name": state["service_user_name"],
                "domain_id": state["service_domain_id"],
                "enabled": state["service_enabled"],
            },
        }

    class IdentityServer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, body, subject=None):
            path = urlsplit(self.path)
            collection = path.path
            if collection == "/v3/role_assignments" and "effective" in parse_qs(path.query):
                collection += "?effective"
            if status == 200 and collection in state["collection_metadata"]:
                body = dict(body)
                body.pop("links", None)
                body.pop("truncated", None)
                body.update(state["collection_metadata"][collection])
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
                if self.headers.get("X-Subject-Token") == ORIGINAL_SUBJECT and not state.get("subject_revoked"):
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
            elif parsed.path == "/v3/domains":
                if state["domains_not_found"]:
                    self.send_json(404, {"error": {"message": "Domain collection unavailable"}})
                    return
                if state["domains_unavailable"]:
                    self.send_json(503, {"error": {"message": "Domain directory unavailable"}})
                    return
                query = parse_qs(parsed.query)
                domains = [
                    dict(domain) for domain in state["domains"].values() if query.get("name") == [domain.get("name")]
                ]
                self.send_json(
                    200,
                    {
                        "domains": domains,
                        "links": {"next": "synthetic-next" if state["domains_next"] else None},
                        "truncated": state["domains_truncated"],
                    },
                )
            elif parsed.path.startswith("/v3/domains/"):
                domain_id = parsed.path.rsplit("/", 1)[-1]
                if domain_id in state["domain_get_unavailable"]:
                    self.send_json(503, {"error": {"message": "Native domain unavailable"}})
                elif domain_id in state["domains"]:
                    self.send_json(200, {"domain": state["domains"][domain_id]})
                else:
                    self.send_json(404, {"error": {"message": "Missing native domain"}})
            elif parsed.path == "/v3/users":
                if state["users_not_found"]:
                    self.send_json(404, {"error": {"message": "User collection unavailable"}})
                    return
                if state["users_unavailable"]:
                    self.send_json(503, {"error": {"message": "User directory unavailable"}})
                    return
                query = parse_qs(parsed.query)
                users = [
                    dict(user)
                    for user in [*native_users().values(), *state["extra_users"]]
                    if (query.get("name") == [user.get("name")] and query.get("domain_id") == [user.get("domain_id")])
                ]
                self.send_json(
                    200,
                    {
                        "users": users,
                        "links": {"next": "synthetic-next" if state["users_next"] else None},
                        "truncated": state["users_truncated"],
                    },
                )
            elif parsed.path == f"/v3/users/{state['user_id']}/groups":
                if state["groups_unavailable"]:
                    self.send_json(503, {"error": {"message": "Group membership unavailable"}})
                else:
                    self.send_json(
                        200,
                        {
                            "groups": [{"id": group_id} for group_id in state["groups"]],
                            "links": {"next": "synthetic-next" if state["groups_next"] else None},
                            "truncated": state["groups_truncated"],
                        },
                    )
            elif parsed.path.startswith("/v3/users/"):
                user = native_users().get(parsed.path.rsplit("/", 1)[-1])
                self.send_json(200, {"user": user}) if user else self.send_json(
                    404, {"error": {"message": "Missing native user"}}
                )
            elif parsed.path == f"/v3/projects/{state['project_id']}":
                self.send_json(
                    200,
                    {
                        "project": {
                            "id": state["project_id"],
                            "name": "selected-project",
                            "enabled": state["project_enabled"],
                            "domain_id": state["project_domain_id"],
                            **state.get("project_extras", {}),
                        }
                    },
                )
            elif parsed.path == f"/v3/projects/{PROJECT_B}":
                self.send_json(
                    200,
                    {
                        "project": {
                            "id": PROJECT_B,
                            "name": "target-project",
                            "enabled": state["target_enabled"],
                            "domain_id": state["target_domain_id"],
                        }
                    },
                )
            elif parsed.path == "/v3/role_assignments":
                query = parse_qs(parsed.query)
                assignments = []
                if state["assignments_unavailable"]:
                    self.send_json(503, {"error": {"message": "Assignment directory unavailable"}})
                    return
                if query.get("scope.system") and "effective" in query:
                    # Real Keystone can omit raw system grants in the effective view.
                    self.send_json(200, {"role_assignments": [], "links": {"next": None}})
                    return
                group_id = (query.get("group.id") or [None])[0]
                if group_id is not None:
                    if state["group_assignments_unavailable"]:
                        self.send_json(503, {"error": {"message": "Raw group grants unavailable"}})
                        return
                    assignments = state["raw_group_assignments"].get(group_id)
                    if assignments is None:
                        assignments = [
                            {
                                "group": {"id": group_id},
                                "scope": {"system": {"all": True}},
                                "role": {"id": "role-" + name, "name": name},
                            }
                            for name in state["group_system_roles"].get(group_id, ())
                        ]
                elif query.get("scope.system"):
                    assignments = state["raw_user_assignments"]
                    if assignments is None:
                        assignments = [
                            {
                                "user": {"id": state["user_id"]},
                                "scope": {"system": {"all": True}},
                                "role": {"id": state["system_role_id"] or "role-" + name, "name": name},
                            }
                            for name in state["system_roles"]
                        ]
                else:
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
                if query.get("scope.project.id"):
                    assignments = state.get(
                        "raw_project_assignments",
                        [
                            row
                            for row in assignments
                            if row.get("scope", {}).get("project", {}).get("id") == query["scope.project.id"][0]
                        ],
                    )
                effective_all = "effective" in query and not query.get("scope.system")
                if effective_all:
                    assignments.extend(state["extra_effective_assignments"])
                partial_all = effective_all and (state["effective_all_next"] or state["effective_all_truncated"])
                if partial_all:
                    # The omitted second page owns the service-principal veto.
                    assignments = [item for item in assignments if item["role"]["name"] != "service"]
                self.send_json(
                    200,
                    {
                        "role_assignments": assignments,
                        "links": {
                            "next": "synthetic-next"
                            if state["assignments_next"] or effective_all and state["effective_all_next"]
                            else None
                        },
                        "truncated": state["assignments_truncated"]
                        or effective_all
                        and state["effective_all_truncated"],
                    },
                )
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


def test_admin_assignment_elsewhere_never_changes_current_project_ceiling(keystone_http):
    keystone_http["project_roles"] = ["palimpsest_reader"]
    keystone_http["other_roles"] = ["admin"]
    owner = validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert owner["package_actions"] == {"packages:inventory"}
    assert "admin" not in owner["roles"]


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
async def test_grade_and_exact_leaf_defaults_need_no_service_edges_or_member(keystone_http, role, expected):
    keystone_http["edges"] = {}
    keystone_http["token_roles"] = keystone_http["project_roles"] = [role]
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
        ["manager"],
        ["waygate_admin"],
        ["service", "palimpsest_admin"],
    ],
)
async def test_unentitled_or_machine_service_roles_cannot_authorize(keystone_http, roles):
    keystone_http["token_roles"] = keystone_http["project_roles"] = roles
    with pytest.raises(HTTPException) as raised:
        await get_package_member_info(validate_token(ORIGINAL_SUBJECT))
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_removed_service_edges_do_not_revoke_locally_evaluated_grade(keystone_http):
    keystone_http["token_roles"] = keystone_http["project_roles"] = ["palimpsest_editor"]
    original = validate_token(ORIGINAL_SUBJECT)
    keystone_http["edges"] = {}
    assert (await get_package_member_info(original))["can_write"]
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


def test_project_admin_implication_never_establishes_system_authority(keystone_http):
    keystone_http["edges"]["palimpsest_admin"].append("admin")
    owner = validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert owner["can_write"] is True
    assert validate_token(ORIGINAL_SUBJECT)["is_system_admin"] is False


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

    from palimpsest_hub.api import builds, catalog, hub, packages
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
    from palimpsest_hub.api import policy as policy_api

    app.include_router(policy_api.router, prefix="/v1")
    for router in (packages.router, hub.router, builds.router, catalog.router):
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
@pytest.mark.parametrize("scope", ["project", "system", "unscoped"])
async def test_global_catalog_real_http_accepts_human_without_fabricated_target(native_http, scope):
    from catalog_fixture import layer

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    h.identity["subject_scope"] = scope
    async with h.factory() as session:
        session.add(layer(400, "global-private-row", project=PROJECT_B))
        await session.commit()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    response = await h.client.get("/v1/admin/catalog/layers", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["name"] == "global-private-row"
    assert response.json()["items"][0]["project_id"] == PROJECT_B
    if scope != "project":
        # The new global read is not permission to broaden target-bound APIs.
        assert (await h.client.get("/v1/layers", headers=headers)).status_code == 401
    h.identity["system_roles"] = []
    assert (await h.client.get("/v1/admin/catalog/layers", headers=headers)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delegated", "service_role", "disabled_user", "disabled_home"])
async def test_global_catalog_projectless_human_vetoes_are_not_weakened(native_http, change):
    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    h.identity["subject_scope"] = "system"
    if change == "delegated":
        h.identity["delegated"] = True
    elif change == "service_role":
        h.identity["token_roles"] = ["service"]
    elif change == "disabled_user":
        h.identity["user_enabled"] = False
    else:
        h.identity["domains"][h.identity["current_user_domain_id"]]["enabled"] = False
    response = await h.client.get("/v1/admin/catalog/layers", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("rule,expected", [("!", 403), ("@", 200)])
async def test_global_catalog_targetless_policy_still_requires_native_human_system_authority(
    native_http, tmp_path, monkeypatch, rule, expected
):
    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    h.identity["subject_scope"] = "system"
    policy_file = tmp_path / "global-catalog-policy.yaml"
    policy_file.write_text(json.dumps({"system:manage": rule}))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    response = await h.client.get("/v1/admin/catalog/layers", headers=headers)
    assert response.status_code == expected, response.text
    h.identity["system_roles"] = []
    assert (await h.client.get("/v1/admin/catalog/layers", headers=headers)).status_code == 403


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
    h.identity["edges"] = {}
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
    h.identity["project_roles"] = h.identity["token_roles"] = [role]
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
    h.identity["project_roles"] = ["palimpsest_reader"]
    assert (await h.client.put(path, headers=credential, params={"package": "test"}, json={})).status_code == 403
    h.identity["directory_unavailable"] = True
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 503


@pytest.mark.asyncio
async def test_fine_grained_inventory_default_is_local_not_inferred(keystone_http):
    keystone_http["project_roles"] = keystone_http["token_roles"] = ["palimpsest-publish_editor"]
    keystone_http["edges"] = {}
    original = validate_token(ORIGINAL_SUBJECT)
    assert (await get_package_member_info(original))["package_capabilities"] == {
        "palimpsest-publish_editor",
        "palimpsest-inventory_reader",
    }


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


@pytest.fixture
async def identity_http(keystone_http):
    application = FastAPI()
    application.include_router(hub_api.router, prefix="/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://fixture") as client:
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer " + ORIGINAL_SUBJECT}, 401),
        ({"X-Auth-Token": "invalid-subject"}, 401),
        ({"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_B}, 403),
        ({"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A.upper()}, 403),
    ],
)
async def test_me_http_requires_original_project_credentials(identity_http, keystone_http, headers, status):
    response = await identity_http.get("/v1/me", headers=headers)
    assert response.status_code == status
    assert not any(event.get("methods") == ["token"] for event in keystone_http["requests"])


@pytest.mark.asyncio
async def test_me_http_exposes_only_immutable_scope_and_fresh_capabilities(identity_http, keystone_http):
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A}
    response = await identity_http.get("/v1/me", headers=headers)
    assert response.status_code == 200
    expected = {
        "user_id": FEDERATED_OWNER,
        "project_id": PROJECT_A,
        "project_name": "selected-project",
        "roles": ["member", "palimpsest_admin"],
        "is_system_admin": False,
        "capabilities": {"inventory": True, "download": True, "upload": True, "delete": False, "build": False},
    }
    assert response.json() == expected
    assert all(
        secret not in response.text
        for secret in (
            ORIGINAL_SUBJECT,
            "validator-subject",
            "synthetic-read-secret",
            "auth_ref",
            "catalog",
            "role_directory",
        )
    )
    # The original token remains valid and unchanged after current authority is reduced.
    keystone_http["project_roles"] = ["reader", "palimpsest_reader"]
    reduced = await identity_http.get("/v1/me", headers=headers)
    expected["capabilities"].update(download=False, upload=False)
    assert reduced.status_code == 200 and reduced.json() == expected
    keystone_http["project_roles"] = []
    removed = await identity_http.get("/v1/me", headers=headers)
    expected["capabilities"]["inventory"] = False
    assert removed.status_code == 200 and removed.json() == expected
    assert not any(event.get("methods") == ["token"] for event in keystone_http["requests"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "roles,expected",
    [
        (["member", "palimpsest-inventory_reader"], (True, False, False)),
        (["member", "palimpsest-download_user"], (True, True, False)),
        (["member", "palimpsest-publish_editor"], (True, False, True)),
        (["reader", "palimpsest-inventory_reader"], (True, False, False)),
        (["reader", "palimpsest-download_user"], (True, True, False)),
        (["member"], (False, False, False)),
        (["admin", "member", "palimpsest_admin"], (True, True, True)),
    ],
)
async def test_me_http_matches_exact_legacy_leaf_policies(identity_http, keystone_http, roles, expected):
    keystone_http["token_roles"] = roles
    keystone_http["project_roles"] = roles
    response = await identity_http.get("/v1/me", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 200
    assert response.json()["roles"] == roles
    assert response.json()["capabilities"] == dict(
        zip(("inventory", "download", "upload", "delete", "build"), (*expected, False, False), strict=True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_enabled", "project_enabled"])
async def test_me_http_disabled_authority_has_no_capabilities(identity_http, keystone_http, field):
    keystone_http[field] = False
    response = await identity_http.get("/v1/me", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 200
    assert not any(response.json()["capabilities"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["directory_unavailable", "directory_truncated"])
async def test_me_http_does_not_mask_current_authority_freshness_failures(identity_http, keystone_http, field):
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    assert (await identity_http.get("/v1/me", headers=headers)).status_code == 200
    keystone_http[field] = True
    assert (await identity_http.get("/v1/me", headers=headers)).status_code == 503


@pytest.mark.asyncio
async def test_me_http_system_admin_can_upload_without_package_membership(identity_http, keystone_http):
    keystone_http["system_roles"] = ["admin"]
    keystone_http["token_roles"] = keystone_http["project_roles"] = []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    response = await identity_http.get("/v1/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["is_system_admin"] is True
    assert response.json()["capabilities"] == {
        "inventory": True,
        "download": True,
        "upload": True,
        "delete": True,
        "build": True,
    }


@pytest.mark.asyncio
async def test_me_http_rejects_unscoped_subject(identity_http, keystone_http):
    keystone_http["project_id"] = ""
    assert (await identity_http.get("/v1/me", headers={"X-Auth-Token": ORIGINAL_SUBJECT})).status_code == 401


@pytest.mark.asyncio
async def test_me_http_protected_identity_has_no_capabilities(identity_http, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS", json.dumps([FEDERATED_OWNER]))
    get_settings.cache_clear()
    response = await identity_http.get("/v1/me", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 200 and response.json()["user_id"] == FEDERATED_OWNER
    assert not any(response.json()["capabilities"].values())


@pytest.mark.asyncio
async def test_me_http_missing_protection_policy_is_unavailable_not_denied(identity_http, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS", "[]")
    get_settings.cache_clear()
    assert (await identity_http.get("/v1/me", headers={"X-Auth-Token": ORIGINAL_SUBJECT})).status_code == 503


@pytest.mark.asyncio
async def test_me_http_other_project_admin_keeps_current_project_ceiling(identity_http, keystone_http):
    keystone_http["project_roles"] = ["palimpsest_reader"]
    keystone_http["other_roles"] = ["admin"]
    response = await identity_http.get("/v1/me", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 200 and response.json()["is_system_admin"] is False
    assert response.json()["capabilities"] == {
        "inventory": True,
        "download": False,
        "upload": False,
        "delete": False,
        "build": False,
    }


@pytest.mark.asyncio
async def test_native_http_system_admin_target_package_crud_without_membership(native_http):
    from sqlalchemy import select
    from test_packages import image_archive
    from test_packages import publish as publish_package

    from palimpsest_hub.models import PackageVersion

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A, "X-Target-Project-Id": PROJECT_B}
    registered = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert registered.status_code == 201, registered.text
    namespace = registered.json()["namespace"]
    assert registered.json()["project_id"] == PROJECT_B
    base = f"/v1/projects/{namespace}"
    archive, body, graph = image_archive()
    published, _, _ = await publish_package(h, namespace, headers, archive, body)
    assert published.status_code == 201, published.text
    assert published.json()["pushed_by"] == FEDERATED_OWNER
    assert published.json()["pushed_key_id"] is None
    async with h.factory() as session:
        row = await session.scalar(select(PackageVersion))
        assert row.pushed_by == FEDERATED_OWNER and row.pushed_key_id is None
    assert (await h.client.get(base + "/packages", headers=headers)).status_code == 200
    version = base + "/versions/" + body["root_digest"]
    for suffix, content in (("", None), ("/download", archive), ("/blobs/" + next(iter(graph)), None)):
        response = await h.client.get(version + suffix, headers=headers, params={"package": "test"})
        assert response.status_code == 200, response.text
        if content is not None:
            assert response.content == content
    # A target is not a scope assertion, and no ordinary key is minted for the operator.
    assert (
        await h.client.get(base + "/packages", headers={"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_B})
    ).status_code == 403
    assert (await h.client.get(base + "/packages", headers={"X-Auth-Token": ORIGINAL_SUBJECT})).status_code == 403
    deleted = await h.client.delete(base + "/package", headers=headers, params={"package": "test"})
    assert deleted.status_code == 204, deleted.text
    assert (await h.client.get(version, headers=headers, params={"package": "test"})).status_code == 404
    assert not any(event.get("methods") == ["token"] for event in h.identity["requests"])


@pytest.mark.asyncio
async def test_native_http_system_admin_revoked_before_finalize_cannot_publish(native_http):
    from sqlalchemy import func, select
    from test_packages import image_archive, staged

    from palimpsest_hub.models import PackageTag

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A, "X-Target-Project-Id": PROJECT_B}
    namespace = default_project_namespace(PROJECT_B)
    assert (await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})).status_code == 201
    archive, body, _ = image_archive()
    path, _ = await staged(h, namespace, headers, archive, body)
    h.identity["system_roles"] = []
    finalized = await h.client.put(path, headers=headers, params={"package": "test"}, json={})
    assert finalized.status_code == 403
    assert (await h.client.get(path, headers=headers, params={"package": "test"})).status_code == 403
    async with h.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PackageTag)) == 0
    assert not h.store.exists(body["archive_digest"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "tenant_admin",
        "forged_role_id",
        "application_credential",
        "trust",
        "disabled_user",
        "disabled_target",
        "configured_service",
        "service_role",
        "validator",
    ],
)
async def test_native_http_system_admin_forgery_and_machine_denial(native_http, monkeypatch, change):
    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["token_roles"] = h.identity["project_roles"] = []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A, "X-Target-Project-Id": PROJECT_B}
    if change == "tenant_admin":
        h.identity["system_roles"] = []
        h.identity["token_roles"] = h.identity["project_roles"] = ["admin"]
    elif change == "forged_role_id":
        h.identity["system_role_id"] = "role-reader"
    elif change == "application_credential":
        h.identity["token_methods"] = ["application_credential"]
    elif change == "trust":
        h.identity["delegated"] = True
    elif change == "disabled_user":
        h.identity["user_enabled"] = False
    elif change == "disabled_target":
        h.identity["target_enabled"] = False
    elif change == "configured_service":
        monkeypatch.setenv("OS_USERNAME", "federated-member")
        get_settings.cache_clear()
    elif change == "service_role":
        h.identity["token_roles"] = ["service"]
    elif change == "validator":
        h.identity["user_id"] = "c" * 32
    status = 503 if change == "forged_role_id" else 403
    assert (await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})).status_code == status
    assert (await h.client.post("/v1/uploads", headers=headers, json={})).status_code == status


def test_require_admin_rechecks_assignment_not_cached_subject_flag(keystone_http):
    keystone_http["system_roles"] = ["admin"]
    keystone_http["token_roles"] = keystone_http["project_roles"] = []
    original = validate_token(ORIGINAL_SUBJECT)
    accepted = require_admin(original)
    assert accepted["is_package_system_admin"] is True
    assert accepted["project_id"] == PROJECT_A and accepted["user_id"] == FEDERATED_OWNER
    assert accepted["roles"] == []
    assert "system:manage" in accepted["package_actions"]
    keystone_http["system_roles"] = []
    with pytest.raises(HTTPException) as rejected:
        require_admin(original)
    assert rejected.value.status_code == 403


@pytest.mark.asyncio
async def test_native_http_human_system_admin_supersedes_protected_owner_policy(native_http, monkeypatch):
    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["token_roles"] = h.identity["project_roles"] = []
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS", json.dumps([FEDERATED_OWNER]))
    monkeypatch.setenv("PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS", json.dumps([PROJECT_B]))
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A, "X-Target-Project-Id": PROJECT_B}
    result = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert result.status_code == 201 and result.json()["project_id"] == PROJECT_B
    # The protected human's ordinary member/key ownership is still forbidden.
    with pytest.raises(HTTPException) as denied:
        await get_package_member_info(validate_token(ORIGINAL_SUBJECT))
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_native_http_admin_revoked_during_archive_validation_has_no_cas_or_tag(native_http, monkeypatch):
    from sqlalchemy import func, select
    from test_packages import image_archive, staged

    from palimpsest_hub.models import PackageTag
    from palimpsest_hub.services import package_registry

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A, "X-Target-Project-Id": PROJECT_B}
    namespace = default_project_namespace(PROJECT_B)
    assert (await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})).status_code == 201
    archive, body, graph = image_archive()
    path, _ = await staged(h, namespace, headers, archive, body)
    run_blocking = package_registry._run_blocking

    async def revoke_after_validation(operation, *args, **kwargs):
        result = await run_blocking(operation, *args, **kwargs)
        if operation is package_registry.validate_package_archive:
            h.identity["system_roles"] = []
        return result

    monkeypatch.setattr(package_registry, "_run_blocking", revoke_after_validation)
    response = await h.client.put(path, headers=headers, params={"package": "test"}, json={})
    assert response.status_code == 403
    assert not h.store.exists(body["archive_digest"])
    assert not any(h.store.exists(digest) for digest in graph)
    async with h.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PackageTag)) == 0


@pytest.mark.asyncio
async def test_native_http_admin_upload_status_abort_and_target_isolation(native_http):
    from test_packages import image_archive

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["token_roles"] = h.identity["project_roles"] = []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A, "X-Target-Project-Id": PROJECT_B}
    namespace = default_project_namespace(PROJECT_B)
    assert (await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})).status_code == 201
    _, body, _ = image_archive()
    base = f"/v1/projects/{namespace}"
    opened = await h.client.post(base + "/uploads", headers=headers, params={"package": "test"}, json=body)
    assert opened.status_code == 201
    assert opened.json()["key_id"] is None and opened.json()["owner_user_id"] == FEDERATED_OWNER
    path = base + "/uploads/" + opened.json()["upload_id"]
    assert (await h.client.get(path, headers=headers, params={"package": "test"})).status_code == 200
    assert (await h.client.get(path, headers=headers, params={"package": "other"})).status_code == 404
    assert (
        await h.client.get(path, headers={**headers, "X-Target-Project-Id": PROJECT_A}, params={"package": "test"})
    ).status_code == 403
    assert (await h.client.delete(path, headers=headers, params={"package": "test"})).status_code == 204
    status = await h.client.get(path, headers=headers, params={"package": "test"})
    assert status.status_code == 200 and status.json()["status"] == "aborted"
    assert (await h.client.put(path, headers=headers, params={"package": "test"}, json={})).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["project", "system", "unscoped"])
@pytest.mark.parametrize("methods", [["password"], ["password", "totp"], ["oidc"], ["token"], ["password", "token"]])
async def test_native_http_human_admin_original_scopes_and_bounded_key_management(native_http, scope, methods):
    from test_packages import image_archive
    from test_packages import publish as publish_package

    from palimpsest_hub.models import PackageKey
    from palimpsest_hub.services import package_registry

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    h.identity["subject_scope"] = scope
    h.identity["token_methods"] = methods
    original_project = PROJECT_A if scope == "project" else ""
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": original_project, "X-Target-Project-Id": PROJECT_B}
    identity = await h.client.get("/v1/me", headers=headers)
    assert identity.status_code == 200, identity.text
    assert identity.json()["project_id"] == original_project and identity.json()["target_project_id"] == PROJECT_B
    context = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert context.status_code == 201, context.text
    assert context.json()["capabilities"]["keys_issue"] and context.json()["capabilities"]["keys_revoke"]
    namespace = context.json()["namespace"]
    base = f"/v1/projects/{namespace}"
    archive, body, _ = image_archive()
    published, _, _ = await publish_package(h, namespace, headers, archive, body)
    assert published.status_code == 201
    request = {
        "name": "explicit narrow admin-issued key",
        "scope": {"packages": ["test"]},
        "actions": ["packages:read"],
    }
    issued = await h.client.post(base + "/keys", headers=headers, json=request)
    assert issued.status_code == 201, issued.text
    assert issued.headers["cache-control"] == "no-store"
    key_id = issued.json()["key"]["key_id"]
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    async with h.factory() as session:
        row = await session.get(PackageKey, key_id.replace("-", ""))
        assert row.owner_user_id == FEDERATED_OWNER and row.project_id == PROJECT_B
        assert row.admin_issuer_context["project_id"] == original_project
        assert set(row.admin_issuer_context) == {"project_id", "auth_methods", "user_domain_id", "user_domain_name"}
        session.add(
            PackageKey(
                id="9" * 32,
                project_id=PROJECT_B,
                owner_user_id="another-human",
                secret_hash="0" * 64,
                name="foreign-owner-key",
                scope={"packages": ["test"]},
                actions=["packages:read"],
                created_at=package_registry.now(),
                expires_at=package_registry.now() + timedelta(days=1),
            )
        )
        await session.commit()
    own = await h.client.get(base + "/keys", headers=headers)
    assert own.status_code == 200 and [item["key_id"] for item in own.json()["items"]] == [key_id]
    assert own.json()["project_id"] == PROJECT_B and issued.json()["secret"] not in own.text
    assert (
        await h.client.delete(base + "/keys/99999999-9999-9999-9999-999999999999", headers=headers)
    ).status_code == 404
    assert (await h.client.get("/v1/auth/me", headers=credential)).json()["actions"] == [
        "packages:inventory",
        "packages:read",
    ]
    version = base + "/versions/" + body["root_digest"]
    downloaded = await h.client.get(version + "/download", headers=credential, params={"package": "test"})
    assert downloaded.status_code == 200 and downloaded.content == archive
    assert (await h.client.get(base + "/package", headers=credential, params={"package": "other"})).status_code == 403
    assert (
        await h.client.post(base + "/uploads", headers=credential, params={"package": "test"}, json=body)
    ).status_code == 403
    assert (
        await h.client.get(base + "/packages", headers={**credential, "X-Target-Project-Id": PROJECT_B})
    ).status_code == 403
    assert (await h.client.delete(base + "/package", headers=credential, params={"package": "test"})).status_code == 403
    assert (await h.client.post(base + "/keys", headers=credential, json=request)).status_code == 401
    assert (await h.client.get("/v1/builds", headers=credential)).status_code == 401
    assert (await h.client.delete(base + "/keys/" + key_id, headers=headers)).status_code == 204
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 401
    second = await h.client.post(base + "/keys", headers=headers, json=request)
    assert second.status_code == 201
    second_credential = {"Authorization": "Bearer " + second.json()["secret"]}
    h.identity["system_roles"] = []
    assert (await h.client.get("/v1/auth/me", headers=second_credential)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "methods", [["application_credential"], ["oauth1"], ["oauth2"], ["password", "unknown-machine"]]
)
async def test_native_http_system_scoped_machine_methods_cannot_gain_admin(native_http, methods):
    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["subject_scope"] = "system"
    h.identity["token_methods"] = methods
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    assert (await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})).status_code in {
        401,
        403,
    }


@pytest.mark.asyncio
async def test_native_http_member_key_never_inherits_later_system_administrator_authority(native_http):
    h = native_http
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    registered = await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=headers, json={})
    assert registered.status_code == 201
    base = "/v1/projects/" + registered.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "old member key",
            "scope": {"packages": ["test"]},
            "actions": ["packages:read"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = []
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["system", "unscoped"])
@pytest.mark.parametrize("grant", ["direct", "direct-implied", "group", "group-implied"])
async def test_native_http_raw_system_grants_when_effective_view_omits_them(native_http, scope, grant):
    h = native_http
    h.identity["subject_scope"] = scope
    h.identity["token_roles"] = h.identity["project_roles"] = []
    alias = "native-system-operator"
    h.identity["directory"].append({"id": "role-" + alias, "name": alias, "domain_id": None})
    h.identity["edges"][alias] = ["admin"]
    role = alias if "implied" in grant else "admin"
    group_id = "native-admin-group"
    if grant.startswith("group"):
        h.identity["groups"] = [group_id]
        h.identity["group_system_roles"][group_id] = [role]
    else:
        h.identity["system_roles"] = [role]
    async with httpx.AsyncClient(base_url=h.identity["url"]) as identity_client:
        omitted = await identity_client.get(
            "/v3/role_assignments",
            headers={"X-Auth-Token": "validator-subject"},
            params={
                "user.id": FEDERATED_OWNER,
                "scope.system": "all",
                "effective": "true",
            },
        )
    assert omitted.status_code == 200 and omitted.json()["role_assignments"] == []
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    original = validate_token(ORIGINAL_SUBJECT)
    assert original["project_id"] == "" and original["is_system_admin"] is True
    context = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert context.status_code == 201, context.text
    base = "/v1/projects/" + context.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "raw native authority key",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201, issued.text
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 200
    if grant == "group-implied":
        h.identity["groups"] = []
    elif grant == "group":
        h.identity["group_system_roles"][group_id] = []
    elif grant == "direct-implied":
        h.identity["edges"][alias] = []
    else:
        h.identity["system_roles"] = []
    assert validate_token(ORIGINAL_SUBJECT)["is_system_admin"] is False
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403
    assert (await h.client.get(base + "/keys", headers=headers)).status_code in {401, 403}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign", ["project", "domain", "foreign-user", "foreign-group", "domain-role", "mixed-scope"]
)
async def test_native_http_raw_grants_never_promote_foreign_scope_or_role_names(native_http, foreign):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    assignment = {
        "user": {"id": FEDERATED_OWNER},
        "scope": {"system": {"all": True}},
        "role": {"id": "role-admin", "name": "admin"},
    }
    if foreign == "project":
        assignment["scope"] = {"project": {"id": PROJECT_B}}
    elif foreign == "domain":
        assignment["scope"] = {"domain": {"id": "default"}}
    elif foreign == "foreign-user":
        assignment["user"] = {"id": "another-user"}
    elif foreign == "mixed-scope":
        assignment["scope"]["project"] = {"id": PROJECT_B}
    elif foreign == "domain-role":
        assignment["role"]["id"] = "domain-admin-id"
        h.identity["directory"].append({"id": "domain-admin-id", "name": "admin", "domain_id": "tenant-domain"})
    if foreign == "foreign-group":
        h.identity["groups"] = ["current-group"]
        assignment.pop("user")
        assignment["group"] = {"id": "foreign-group"}
        h.identity["raw_group_assignments"]["current-group"] = [assignment]
    else:
        h.identity["raw_user_assignments"] = [assignment]
    assert validate_token(ORIGINAL_SUBJECT)["is_system_admin"] is False
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    assert (await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})).status_code in {
        401,
        403,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "groups_unavailable",
        "groups_truncated",
        "groups_next",
        "assignments_unavailable",
        "assignments_truncated",
        "assignments_next",
        "group_assignments_unavailable",
        "directory_unavailable",
        "directory_truncated",
        "ambiguous-admin",
    ],
)
async def test_native_http_system_metadata_outage_is_unavailable_not_a_false_denial(native_http, failure):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["groups"] = ["native-admin-group"]
    h.identity["group_system_roles"]["native-admin-group"] = ["admin"]
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    context = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert context.status_code == 201
    base = "/v1/projects/" + context.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "fresh owner proof",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    if failure == "ambiguous-admin":
        h.identity["directory"].append({"id": "other-global-admin", "name": "admin", "domain_id": None})
    else:
        h.identity[failure] = True
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 503
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 503


@pytest.mark.asyncio
async def test_native_http_hidden_raw_group_service_assignment_blocks_existing_admin_key(native_http):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["groups"] = ["human-admins"]
    h.identity["group_system_roles"]["human-admins"] = ["admin"]
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    registered = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert registered.status_code == 201
    base = "/v1/projects/" + registered.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "bounded before service change",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200
    h.identity["groups"].append("service-principals")
    h.identity["group_system_roles"]["service-principals"] = ["service"]
    assert validate_token(ORIGINAL_SUBJECT)["is_system_admin"] is False
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403
    assert (await h.client.get(base + "/keys", headers=headers)).status_code in {401, 403}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["project", "system", "unscoped"])
@pytest.mark.parametrize("domain", ["home", "target"])
async def test_native_http_admin_key_rechecks_home_and_target_domain_enablement(native_http, scope, domain):
    h = native_http
    h.identity["subject_scope"] = scope
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["system_roles"] = ["admin"]
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    context = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert context.status_code == 201, context.text
    base = "/v1/projects/" + context.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "native-domain-bound key",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201, issued.text
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200
    domain_id = h.identity["current_user_domain_id"] if domain == "home" else h.identity["target_domain_id"]
    h.identity["domains"][domain_id]["enabled"] = False
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 403
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403
    h.identity["domains"][domain_id]["enabled"] = True
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "home-domain-enabled-missing",
        "target-domain-enabled-missing",
        "home-domain-id-mismatch",
        "home-domain-missing",
        "target-domain-missing",
        "home-domain-unavailable",
        "target-domain-unavailable",
        "home-domain-id-missing",
        "target-domain-id-missing",
        "user-enabled-missing",
        "project-enabled-missing",
        "domains_unavailable",
        "domains_truncated",
        "domains_next",
        "domains_not_found",
        "users_unavailable",
        "users_truncated",
        "users_next",
        "users_not_found",
        "configured-domain-ambiguous",
        "configured-user-ambiguous",
        "configured-domain-enabled-missing",
        "configured-user-enabled-missing",
        "configured-domain-missing",
        "configured-user-missing",
    ],
)
async def test_native_http_admin_domain_and_configured_identity_metadata_fail_closed(native_http, monkeypatch, failure):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["system_roles"] = ["admin"]
    service_domain = "independent-service-domain"
    h.identity["domains"][service_domain] = {"id": service_domain, "name": "ServiceDomain", "enabled": True}
    h.identity["service_domain_id"] = service_domain
    monkeypatch.setenv("OS_USER_DOMAIN_NAME", "ServiceDomain")
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    context = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert context.status_code == 201, context.text
    base = "/v1/projects/" + context.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "native metadata proof",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    if failure == "home-domain-enabled-missing":
        h.identity["domains"]["default"].pop("enabled")
    elif failure == "target-domain-enabled-missing":
        h.identity["domains"]["target-domain"]["enabled"] = "true"
    elif failure == "home-domain-id-mismatch":
        h.identity["domains"]["default"]["id"] = "foreign-domain"
    elif failure == "home-domain-missing":
        h.identity["domains"].pop("default")
    elif failure == "target-domain-missing":
        h.identity["domains"].pop("target-domain")
    elif failure == "home-domain-unavailable":
        h.identity["domain_get_unavailable"].add("default")
    elif failure == "target-domain-unavailable":
        h.identity["domain_get_unavailable"].add("target-domain")
    elif failure == "home-domain-id-missing":
        h.identity["current_user_domain_id"] = None
    elif failure == "target-domain-id-missing":
        h.identity["target_domain_id"] = None
    elif failure == "user-enabled-missing":
        h.identity["user_enabled"] = None
    elif failure == "project-enabled-missing":
        h.identity["target_enabled"] = None
    elif failure == "configured-domain-ambiguous":
        h.identity["domains"]["another-service-domain"] = {
            "id": "another-service-domain",
            "name": "ServiceDomain",
            "enabled": True,
        }
    elif failure == "configured-user-ambiguous":
        h.identity["extra_users"].append(
            {"id": "another-service-user", "name": "palimpsest", "domain_id": service_domain, "enabled": True}
        )
    elif failure == "configured-domain-enabled-missing":
        h.identity["domains"][service_domain].pop("enabled")
    elif failure == "configured-user-enabled-missing":
        h.identity["service_enabled"] = None
    elif failure == "configured-domain-missing":
        h.identity["domains"].pop(service_domain)
    elif failure == "configured-user-missing":
        h.identity["service_user_name"] = "renamed-export-trustee"
    else:
        h.identity[failure] = True
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 503
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", [False, True])
async def test_native_http_configured_service_id_is_excluded_despite_token_domain_name(
    native_http, monkeypatch, rename
):
    from sqlalchemy import func, select

    from palimpsest_hub.models import PackageKey, PackageNamespace

    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["system_roles"] = ["admin"]
    h.identity["user_id"] = h.identity["service_user_id"]
    h.identity["token_user_domain_name"] = "StaleOrMismatchedDomain"
    if rename:
        h.identity["domains"]["default"]["name"] = "RenamedDefault"
        monkeypatch.setenv("OS_USER_DOMAIN_NAME", "RenamedDefault")
        get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    result = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert result.status_code == 403
    async with h.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PackageNamespace)) == 0
        assert await session.scalar(select(func.count()).select_from(PackageKey)) == 0


@pytest.mark.asyncio
async def test_native_http_static_key_domain_name_never_skips_current_service_id_exclusion(native_http, monkeypatch):
    h = native_http
    h.identity["subject_scope"] = "unscoped"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["system_roles"] = ["admin"]
    h.identity["token_user_domain_name"] = "OldTokenDomainName"
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    context = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert context.status_code == 201, context.text
    base = "/v1/projects/" + context.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "static domain-name provenance",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200
    # The same native user is now the configured trustee in a renamed domain;
    # old token/provenance names cannot turn off its exact current-ID exclusion.
    h.identity["domains"]["default"]["name"] = "RenamedDefault"
    monkeypatch.setenv("OS_USER_DOMAIN_NAME", "RenamedDefault")
    monkeypatch.setenv("OS_USERNAME", "federated-member")
    get_settings.cache_clear()
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 403
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["effective_all_next", "effective_all_truncated"])
@pytest.mark.parametrize("service_scope", ["project", "domain"])
async def test_native_http_admin_service_veto_requires_complete_effective_all_collection(
    native_http, partial, service_scope
):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["system_roles"] = ["admin"]
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    registered = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert registered.status_code == 201
    base = "/v1/projects/" + registered.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "bounded before all-scope pagination",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 200
    if service_scope == "project":
        h.identity["other_roles"] = ["service"]
    else:
        h.identity["extra_effective_assignments"] = [
            {
                "user": {"id": FEDERATED_OWNER},
                "scope": {"domain": {"id": "default"}},
                "role": {"id": "role-service", "name": "service"},
            }
        ]
    h.identity[partial] = True
    # Raw exact-system grants and every native domain remain complete/valid;
    # ONLY the distinct effective-all service-veto response is partial.
    assert validate_token(ORIGINAL_SUBJECT)["is_system_admin"] is True
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 503
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 503
    h.identity[partial] = False
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 403
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["effective_all_next", "effective_all_truncated"])
async def test_native_http_member_key_service_veto_requires_complete_effective_all_collection(native_http, partial):
    h = native_http
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    registered = await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=headers, json={})
    assert registered.status_code == 201
    base = "/v1/projects/" + registered.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "ordinary key before all-scope pagination",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    h.identity["other_roles"] = ["service"]
    h.identity[partial] = True
    assert (await h.client.get(base + "/keys", headers=headers)).status_code == 503
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 503
    h.identity[partial] = False
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("methods", [["token"], ["password", "token"]])
@pytest.mark.parametrize(
    "marker",
    [
        "OS-TRUST:trust",
        "application_credential",
        "OS-OAUTH1",
        "OS-OAUTH2",
        "oauth1",
        "oauth2",
        "bind",
        "access_rules",
    ],
)
@pytest.mark.parametrize("restriction", [{"id": "original-restriction"}, {}])
async def test_native_http_exchanged_reduced_original_never_gains_broad_admin(
    native_http, methods, marker, restriction
):
    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["token_roles"] = h.identity["project_roles"] = []
    h.identity["token_methods"] = methods
    h.identity["token_markers"] = {marker: restriction}
    info = validate_token(ORIGINAL_SUBJECT)
    assert info["is_delegated"] is True and info["is_system_admin"] is False
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    response = await h.client.put(f"/v1/projects/{PROJECT_B}/namespace", headers=headers, json={})
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True], ids=["ordinary", "admin"])
@pytest.mark.parametrize("collection", ["/v3/roles", "/v3/role_inferences", "/v3/role_assignments?effective"])
@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({}, 200),
        ({"links": {}}, 200),
        ({"links": {"next": None}, "truncated": None}, 200),
        ({"truncated": False}, 200),
        *[({"links": {"next": value}}, 503) for value in [False, 0, "", [], {}, "next-page"]],
        *[({"truncated": value}, 503) for value in [0, 0.0, "", [], {}, True]],
    ],
)
async def test_native_http_raw_collection_completeness_for_tokens_and_keys(
    native_http, admin, collection, metadata, expected
):
    h = native_http
    project = PROJECT_A
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    if admin:
        h.identity["system_roles"] = ["admin"]
        h.identity["token_roles"] = h.identity["project_roles"] = []
        h.identity["token_methods"] = ["token"]
        project = PROJECT_B
        headers["X-Target-Project-Id"] = project
    registered = await h.client.put(f"/v1/projects/{project}/namespace", headers=headers, json={})
    assert registered.status_code == 201, registered.text
    base = "/v1/projects/" + registered.json()["namespace"]
    issued = await h.client.post(
        base + "/keys",
        headers=headers,
        json={
            "name": "raw envelope proof",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory"],
        },
    )
    assert issued.status_code == 201, issued.text
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    # Override the actual HTTP JSON envelope, without fixture truthiness coercion
    # or SDK Resource/list normalization of completeness markers.
    h.identity["collection_metadata"][collection] = metadata
    response = await h.client.get(base + "/keys", headers=headers)
    assert response.status_code == expected, response.text
    response = await h.client.get("/v1/auth/me", headers=credential)
    assert response.status_code == expected, response.text


@pytest.mark.asyncio
async def test_native_policy_http_reload_controls_key_ceiling_without_inventory_leaf(
    native_http, tmp_path, monkeypatch
):
    from test_packages import image_archive, staged

    h = native_http
    h.identity["edges"] = {}
    token = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Project-Id": PROJECT_A}
    namespace = default_project_namespace(PROJECT_A)
    base = f"/v1/projects/{namespace}"
    assert (await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})).status_code == 201
    request = {"name": "policy ceiling", "scope": {"packages": ["test"]}, "actions": ["packages:write", "cache:write"]}
    issued = await h.client.post(base + "/keys", headers=token, json=request)
    assert issued.status_code == 201
    key = {"Authorization": "Bearer " + issued.json()["secret"]}
    path = tmp_path / "policy.yaml"
    path.write_text("{}\n")
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(path))
    get_settings.cache_clear()
    h.identity["token_roles"] = h.identity["project_roles"] = ["palimpsest_reader"]
    archive, body, _ = image_archive()
    assert (
        await h.client.post(base + "/uploads", headers=key, params={"package": "test"}, json=body)
    ).status_code == 403
    path.write_text(
        json.dumps(
            {
                "packages:inventory": "!",
                "packages:write": "role:palimpsest_reader",
                "cache:write": "role:palimpsest_reader",
                "keys:issue": "role:palimpsest_reader",
            }
        )
    )
    projection = await h.client.get("/v1/policy/capabilities", headers=token)
    assert projection.status_code == 200
    assert projection.json() == {
        "service": "palimpsest",
        "capabilities": ["palimpsest-keys_editor", "palimpsest-publish_editor"],
        "actions": ["cache:write", "keys:issue", "packages:write"],
    }
    assert (await h.client.get(base + "/packages", headers=token)).status_code == 403
    upload, _ = await staged(h, namespace, key, archive, body)
    assert (await h.client.put(upload, headers=key, params={"package": "test"}, json={})).status_code == 201
    assert (await h.client.post(base + "/keys", headers=token, json=request)).status_code == 201
    assert (
        await h.client.post(base + "/keys", headers=token, json={**request, "actions": ["packages:read"]})
    ).status_code == 403
    assert (
        await h.client.post(base + "/uploads", headers=key, params={"package": "other"}, json=body)
    ).status_code == 403
    assert (await h.client.get("/v1/projects/foreign/packages", headers=key)).status_code == 403
    assert (
        await h.client.post("/v1/builds", headers=token, json={"name": "denied", "recipe": "FROM fixture"})
    ).status_code == 403
    assert (await h.client.delete("/v1/layers/" + body["root_digest"], headers=token)).status_code == 403
    path.write_text(
        json.dumps(
            {
                "packages:inventory": "!",
                "packages:read": "role:palimpsest_reader",
                "keys:issue": "role:palimpsest_reader",
            }
        )
    )
    reader = await h.client.post(base + "/keys", headers=token, json={**request, "actions": ["packages:read"]})
    assert reader.status_code == 201
    read_key = {"Authorization": "Bearer " + reader.json()["secret"]}
    version_path = base + "/versions/" + body["root_digest"]
    assert (await h.client.get(version_path, headers=read_key, params={"package": "test"})).status_code == 403
    downloaded = await h.client.get(version_path + "/download", headers=read_key, params={"package": "test"})
    assert downloaded.status_code == 200 and downloaded.content == archive
    path.write_text(json.dumps({"packages:write": "!", "cache:write": "role:palimpsest_reader"}))
    assert (
        await h.client.post(base + "/uploads", headers=key, params={"package": "test"}, json=body)
    ).status_code == 403
    assert "packages:write" not in (await h.client.get("/v1/policy/capabilities", headers=token)).json()["actions"]
    cache_body = {
        "build_key": body["root_digest"],
        "cache_scope": "default",
        "platform": "linux/amd64",
        "builder_fingerprint": body["root_digest"],
        "archive_digest": body["archive_digest"],
        "archive_size_bytes": len(archive),
    }
    assert (
        await h.client.post(base + "/cache/uploads", headers=key, params={"package": "test"}, json=cache_body)
    ).status_code == 201
    from palimpsest_hub.policy import ACTIONS

    path.write_text(json.dumps(dict.fromkeys(ACTIONS, "!")))
    empty = await h.client.get("/v1/policy/capabilities", headers=token)
    assert empty.status_code == 200 and empty.json() == {"service": "palimpsest", "actions": [], "capabilities": []}
    assert (await h.client.get("/v1/policy/capabilities")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        '"packages:write": [',
        '"packages:write": null',
        '"packages:write": false',
        '"packages:write": []',
        '"packages:write": "rule:missing"',
        '"": "@"',
        '1: "@"',
        "null",
        "false",
        "[]",
        "",
    ],
)
async def test_native_policy_malformed_reload_never_reuses_allow(native_http, tmp_path, monkeypatch, invalid):
    h = native_http
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    path = tmp_path / "policy.yaml"
    path.write_text("{}\n")
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(path))
    get_settings.cache_clear()
    assert (await h.client.get("/v1/policy/capabilities", headers=token)).status_code == 200
    path.write_text(invalid)
    for _ in range(2):
        assert (await h.client.get("/v1/policy/capabilities", headers=token)).status_code == 503
    path.write_text("{}\n")
    assert (await h.client.get("/v1/policy/capabilities", headers=token)).status_code == 200


@pytest.mark.asyncio
async def test_native_policy_allow_all_does_not_bypass_identity_or_system(native_http, tmp_path, monkeypatch):
    from palimpsest_hub.policy import ACTIONS

    h = native_http
    h.identity["edges"] = {}
    path = tmp_path / "policy.yaml"
    path.write_text(json.dumps(dict.fromkeys(ACTIONS, "@")))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(path))
    get_settings.cache_clear()
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    ordinary = await h.client.get("/v1/policy/capabilities", headers=token)
    assert ordinary.status_code == 200 and "system:manage" not in ordinary.json()["actions"]
    assert (await h.client.get("/v1/builds", headers=token)).status_code == 403
    assert (
        await h.client.get("/v1/policy/capabilities", headers={**token, "X-Target-Project-Id": PROJECT_B})
    ).status_code == 403
    h.identity["token_roles"] = ["admin"]
    h.identity["project_roles"] = ["admin"]
    project_admin = await h.client.get("/v1/policy/capabilities", headers=token)
    assert project_admin.status_code == 200 and "system:manage" not in project_admin.json()["actions"]
    assert (await h.client.get("/v1/builds", headers=token)).status_code == 403
    h.identity["project_roles"] = []
    assert (await h.client.get("/v1/policy/capabilities", headers=token)).status_code == 403
    h.identity["project_roles"] = ["palimpsest_admin"]
    h.identity["token_markers"] = {"application_credential": {"id": "restricted"}}
    assert (await h.client.get("/v1/policy/capabilities", headers=token)).status_code == 403
    h.identity["token_markers"] = {}
    h.identity["token_roles"] = h.identity["project_roles"] = ["palimpsest_admin"]
    h.identity["user_enabled"] = False
    assert (await h.client.get("/v1/policy/capabilities", headers=token)).status_code == 403


@pytest.mark.asyncio
async def test_native_policy_toml_bridge_directory_reload_and_env_precedence(native_http, tmp_path, monkeypatch):
    h = native_http
    h.identity["edges"] = {}
    h.identity["project_roles"] = h.identity["token_roles"] = ["palimpsest_reader"]
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("{}\n")
    policy_dir = tmp_path / "policy.d"
    policy_dir.mkdir()
    override = policy_dir / "reader.yaml"
    override.write_text('"packages:write": "role:palimpsest_reader"\n')
    config = tmp_path / "palimpsest.conf"
    config.write_text(
        "[oslo_policy]\npolicy_file = "
        + json.dumps(str(policy_file))
        + "\npolicy_dirs = "
        + json.dumps([str(policy_dir)])
        + "\n"
    )
    monkeypatch.delenv("OSLO_POLICY_POLICY_FILE", raising=False)
    monkeypatch.delenv("OSLO_POLICY_POLICY_DIRS", raising=False)
    monkeypatch.setenv("PALIMPSEST_CONFIG", str(config))
    get_settings.cache_clear()
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    assert "packages:write" in (await h.client.get("/v1/policy/capabilities", headers=token)).json()["actions"]
    override.write_text('"packages:write": "!"\n')
    assert "packages:write" not in (await h.client.get("/v1/policy/capabilities", headers=token)).json()["actions"]
    other = tmp_path / "env-policy.yaml"
    other.write_text('"packages:inventory": "!"\n')
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(other))
    monkeypatch.setenv("OSLO_POLICY_POLICY_DIRS", "[]")
    get_settings.cache_clear()
    response = await h.client.get("/v1/policy/capabilities", headers=token)
    assert response.status_code == 200 and response.json()["actions"] == []


@pytest.mark.asyncio
async def test_native_policy_casefold_global_collision_denies_before_authority(native_http, tmp_path, monkeypatch):
    h = native_http
    h.identity["edges"] = {}
    h.identity["directory"].append(
        {"id": "role-custom_policy_writer", "name": "custom_policy_writer", "domain_id": None}
    )
    h.identity["token_roles"] = h.identity["project_roles"] = ["custom_policy_writer"]
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text('"packages:write": "role:custom_policy_writer"\n')
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    baseline = await h.client.get("/v1/policy/capabilities", headers=headers)
    assert baseline.status_code == 200 and baseline.json()["actions"] == ["packages:write"]
    h.identity["directory"].append(
        {"id": "role-Custom_Policy_Writer", "name": "Custom_Policy_Writer", "domain_id": None}
    )
    for name in ("custom_policy_writer", "Custom_Policy_Writer"):
        h.identity["token_roles"] = h.identity["project_roles"] = [name]
        assert (await h.client.get("/v1/policy/capabilities", headers=headers)).status_code == 503
        assert (await h.client.post("/v1/uploads", headers=headers, json={})).status_code == 503


def test_default_policy_config_never_reads_unreadable_secret_toml(monkeypatch):
    from pathlib import Path

    monkeypatch.delenv("PALIMPSEST_CONFIG", raising=False)
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", "/etc/palimpsest/policy.yaml")
    monkeypatch.setenv("OSLO_POLICY_POLICY_DIRS", "[]")
    default = Path("/etc/palimpsest/palimpsest.conf")
    original_is_file = Path.is_file
    reads = []

    def is_file(path):
        return True if path in {default, Path("/app/palimpsest.conf")} else original_is_file(path)

    def read_text(path, *args, **kwargs):
        reads.append(path)
        if path == Path("/app/palimpsest.conf"):
            raise PermissionError("root:root 0640 secret TOML is not readable by Hub")
        assert path == default
        return '[oslo_policy]\npolicy_file = "/etc/palimpsest/policy.yaml"\npolicy_dirs = []\n'

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "read_text", read_text)
    get_settings.cache_clear()
    assert get_settings().oslo_policy_policy_file == "/etc/palimpsest/policy.yaml"
    assert reads == [default]


@pytest.mark.asyncio
@pytest.mark.parametrize("expression,expected", [("", True), ("@", True), ("!", False)])
async def test_native_policy_explicit_string_checks_keep_oslo_semantics(
    native_http, tmp_path, monkeypatch, expression, expected
):
    h = native_http
    h.identity["edges"] = {}
    h.identity["project_roles"] = h.identity["token_roles"] = ["palimpsest_reader"]
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(json.dumps({"packages:write": expression}))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()
    response = await h.client.get("/v1/policy/capabilities", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 200
    assert ("packages:write" in response.json()["actions"]) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize(
    "expression", ["not malformed", "role:palimpsest_reader or malformed", "not (role:palimpsest_reader or)"]
)
async def test_native_policy_parse_errors_cannot_become_allow_under_boolean_checks(
    native_http, tmp_path, monkeypatch, directory, expression
):
    from oslo_policy import _parser

    h = native_http
    h.identity["edges"] = {}
    h.identity["project_roles"] = h.identity["token_roles"] = ["palimpsest_reader"]
    main = tmp_path / "policy.yaml"
    main.write_text("{}\n")
    policy_dir = tmp_path / "policy.d"
    policy_dir.mkdir()
    rules = policy_dir / "override.yaml" if directory else main
    rules.write_text(json.dumps({"packages:write": expression}))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(main))
    monkeypatch.setenv("OSLO_POLICY_POLICY_DIRS", json.dumps([str(policy_dir)]) if directory else "[]")
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT}
    logger = _parser.LOG
    assert (await h.client.get("/v1/policy/capabilities", headers=headers)).status_code == 503
    assert (await h.client.post("/v1/uploads", headers=headers, json={})).status_code == 503
    assert _parser.LOG is logger
    rules.write_text(json.dumps({"packages:write": "role:palimpsest_reader"}))
    recovered = await h.client.get("/v1/policy/capabilities", headers=headers)
    assert recovered.status_code == 200 and "packages:write" in recovered.json()["actions"]
    started = await h.client.post("/v1/uploads", headers=headers, json={})
    assert started.status_code == 200
    assert (await h.client.delete("/v1/uploads/" + started.json()["session_id"], headers=headers)).status_code == 204
    assert _parser.LOG is logger


@pytest.mark.asyncio
async def test_native_policy_http_cache_only_archive_roundtrip_and_current_issuer(native_http, tmp_path, monkeypatch):
    from test_packages import cache_archive, digest, staged

    h = native_http
    h.identity["edges"] = {}
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    namespace = default_project_namespace(PROJECT_A)
    base = f"/v1/projects/{namespace}"
    assert (await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})).status_code == 201
    issued = await h.client.post(
        base + "/keys",
        headers=token,
        json={
            "name": "cache-only",
            "scope": {"packages": ["test"]},
            "actions": ["cache:read", "cache:write"],
        },
    )
    assert issued.status_code == 201
    key = {"Authorization": "Bearer " + issued.json()["secret"]}
    h.identity["token_roles"] = h.identity["project_roles"] = ["palimpsest_reader"]
    policy_file = tmp_path / "cache-policy.yaml"
    policy_file.write_text(json.dumps({"packages:inventory": "!", "cache:read": "@", "cache:write": "@"}))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()
    projection = await h.client.get("/v1/policy/capabilities", headers=token)
    assert projection.status_code == 200 and projection.json() == {
        "service": "palimpsest",
        "capabilities": [],
        "actions": ["cache:read", "cache:write"],
    }
    binding = {
        "project_id": PROJECT_A,
        "namespace": namespace,
        "package": "test",
        "build_key": digest(b"http build"),
        "cache_scope": "default",
        "platform": "linux/amd64",
        "builder_fingerprint": digest(b"http builder"),
    }
    archive = cache_archive(binding)
    body = {name: value for name, value in binding.items() if name not in {"project_id", "namespace", "package"}}
    body.update(archive_digest=digest(archive), archive_size_bytes=len(archive))
    upload, _ = await staged(h, namespace, key, archive, body, resource="cache")
    assert (await h.client.put(upload, params={"package": "test"}, headers=key, json={})).status_code == 201
    lookup = {name: value for name, value in body.items() if name not in {"archive_digest", "archive_size_bytes"}}
    resolved = await h.client.get(base + "/cache/resolve", params={"package": "test", **lookup}, headers=key)
    assert resolved.status_code == 200 and resolved.json()["resolution"] == "exact"
    archive_path = base + "/cache/archives/" + body["archive_digest"]
    downloaded = await h.client.get(archive_path, params={"package": "test"}, headers=key)
    assert downloaded.status_code == 200 and downloaded.content == archive
    assert (await h.client.get(base + "/packages", headers=key)).status_code == 403
    assert (await h.client.get(archive_path, params={"package": "other"}, headers=key)).status_code == 403
    policy_file.write_text(json.dumps({"packages:inventory": "!", "cache:read": "@", "cache:write": "!"}))
    assert (
        await h.client.post(base + "/cache/uploads", params={"package": "test"}, headers=key, json=body)
    ).status_code == 403
    assert (await h.client.get(archive_path, params={"package": "test"}, headers=key)).status_code == 200
    h.identity["project_roles"] = []
    assert (await h.client.get(archive_path, params={"package": "test"}, headers=key)).status_code == 403


def test_packaged_policy_sample_exactly_matches_registered_defaults():
    from importlib.resources import files

    import yaml

    from palimpsest_hub.policy import list_rules

    sample = files("palimpsest_hub").joinpath("policy.yaml.sample").read_text()
    assert yaml.safe_load(sample) == {rule.name: rule.check_str for rule in list_rules()}


def _configure_personal_identity(state):
    state["token_roles"] = state["project_roles"] = ["member"]
    state["project_domain_id"] = "personal"
    state["domains"]["personal"] = {"id": "personal", "name": "Personal", "enabled": True}
    state["project_extras"] = {"afterglow_personal_user_id": FEDERATED_OWNER}


@pytest.mark.asyncio
async def test_native_personal_member_gets_only_project_actions_and_policy_deny_wins(
    native_http, tmp_path, monkeypatch
):
    from palimpsest_hub.policy import ACTIONS, SYSTEM_ACTIONS

    h = native_http
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    h.identity["token_roles"] = h.identity["project_roles"] = ["member"]
    ordinary = await h.client.get("/v1/policy/capabilities", headers=token)
    assert ordinary.status_code == 200 and ordinary.json()["actions"] == []
    _configure_personal_identity(h.identity)
    projection = await h.client.get("/v1/policy/capabilities", headers=token)
    assert projection.status_code == 200, projection.text
    assert set(projection.json()["actions"]) == set(ACTIONS) - SYSTEM_ACTIONS
    owner = validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert owner["personal_project_owner"] is True and set(owner["roles"]) <= {"member", "reader"}
    policy_file = tmp_path / "personal-policy.yaml"
    policy_file.write_text(json.dumps({"packages:write": "!", "system:manage": "@", "packages:delete": "!"}))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()
    projected = await h.client.get("/v1/policy/capabilities", headers=token)
    assert projected.status_code == 200
    assert not {"packages:write", "system:manage", "packages:delete"} & set(projected.json()["actions"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing_marker",
        "foreign_marker",
        "malformed_marker",
        "wrong_domain",
        "conflicting_kind",
        "disabled_home",
        "disabled_domain",
        "disabled_user",
        "disabled_project",
        "member_removed",
        "foreign_raw",
        "group_raw",
        "inherited_raw",
        "group_effective",
        "delegated",
    ],
)
async def test_native_personal_identity_provenance_and_original_credential_fail_closed(keystone_http, change):
    state = keystone_http
    _configure_personal_identity(state)
    if change in {"missing_marker", "foreign_marker", "malformed_marker"}:
        state["project_extras"] = {
            "afterglow_personal_user_id": {
                "missing_marker": None,
                "foreign_marker": "another-user",
                "malformed_marker": [FEDERATED_OWNER],
            }[change]
        }
    elif change == "wrong_domain":
        state["domains"]["personal"]["name"] = "personal"
    elif change == "conflicting_kind":
        state["domains"]["personal"]["afterglow_domain_kind"] = "organization"
    elif change in {"disabled_home", "disabled_domain"}:
        state["domains"]["default" if change == "disabled_home" else "personal"]["enabled"] = False
    elif change in {"disabled_user", "disabled_project"}:
        state["user_enabled" if change == "disabled_user" else "project_enabled"] = False
    elif change == "member_removed":
        state["project_roles"] = []
    elif change == "delegated":
        state["token_markers"] = {"application_credential": {"id": "reduced"}}
    else:
        row = {
            "user": {"id": FEDERATED_OWNER},
            "scope": {"project": {"id": PROJECT_A}},
            "role": {"id": "role-member", "name": "member"},
        }
        if change == "foreign_raw":
            row["user"]["id"] = "another-user"
        elif change in {"group_raw", "group_effective"}:
            row["links"] = {"membership": "https://identity/groups/foreign/users/owner"}
        else:
            row["scope"]["OS-INHERIT:inherited_to"] = "projects"
        state["extra_effective_assignments" if change == "group_effective" else "raw_project_assignments"] = [row]
    with pytest.raises(HTTPException) as error:
        await get_package_member_info(validate_token(ORIGINAL_SUBJECT))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_native_personal_package_key_keeps_action_package_and_system_ceilings(native_http):
    from test_packages import cache_archive, digest, image_archive, publish, staged

    h = native_http
    _configure_personal_identity(h.identity)
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    registered = await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})
    assert registered.status_code == 201, registered.text
    namespace = registered.json()["namespace"]
    base = "/v1/projects/" + namespace
    request = {
        "name": "personal package",
        "scope": {"packages": ["test"]},
        "actions": ["packages:read", "packages:write", "cache:read", "cache:write"],
    }
    issued = await h.client.post(base + "/keys", headers=token, json=request)
    assert issued.status_code == 201, issued.text
    key = {"Authorization": "Bearer " + issued.json()["secret"]}
    archive, body, _ = image_archive()
    published, _, _ = await publish(h, namespace, key, archive, body)
    assert published.status_code == 201, published.text
    assert (
        await h.client.get(
            base + "/versions/" + body["root_digest"] + "/download", headers=key, params={"package": "test"}
        )
    ).status_code == 200
    binding = {
        "project_id": PROJECT_A,
        "namespace": namespace,
        "package": "test",
        "build_key": digest(b"personal build"),
        "cache_scope": "default",
        "platform": "linux/amd64",
        "builder_fingerprint": digest(b"personal builder"),
    }
    cache = cache_archive(binding)
    cache_body = {name: value for name, value in binding.items() if name not in {"project_id", "namespace", "package"}}
    cache_body.update(archive_digest=digest(cache), archive_size_bytes=len(cache))
    cache_upload, _ = await staged(h, namespace, key, cache, cache_body, resource="cache")
    assert (await h.client.put(cache_upload, params={"package": "test"}, headers=key, json={})).status_code == 201
    cached = await h.client.get(base + "/cache/archives/" + digest(cache), params={"package": "test"}, headers=key)
    assert cached.status_code == 200 and cached.content == cache
    assert (
        await h.client.post(base + "/uploads", headers=key, params={"package": "other"}, json=body)
    ).status_code == 403
    assert (await h.client.get("/v1/projects/foreign/packages", headers=key)).status_code == 403
    assert (await h.client.post(base + "/keys", headers=key, json=request)).status_code == 401
    assert (await h.client.delete(base + "/package", headers=key, params={"package": "test"})).status_code == 403
    assert (await h.client.get("/v1/builds", headers=token)).status_code == 403
    narrow = await h.client.post(base + "/keys", headers=token, json={**request, "actions": ["packages:read"]})
    assert narrow.status_code == 201
    narrow_key = {"Authorization": "Bearer " + narrow.json()["secret"]}
    assert (
        await h.client.post(base + "/uploads", headers=narrow_key, params={"package": "test"}, json=body)
    ).status_code == 403
    next_archive, next_body, _ = image_archive(b"pending personal publication")
    next_body["expected_tag_digest"] = body["root_digest"]
    upload, _ = await staged(h, namespace, key, next_archive, next_body)
    h.identity["project_extras"]["afterglow_personal_user_id"] = "foreign-owner"
    assert (await h.client.put(upload, headers=key, params={"package": "test"}, json={})).status_code == 403
    assert (await h.client.get("/v1/auth/me", headers=key)).status_code == 403
    assert (
        await h.client.get(base + "/cache/archives/" + digest(cache), params={"package": "test"}, headers=key)
    ).status_code == 403
    h.identity["project_extras"]["afterglow_personal_user_id"] = FEDERATED_OWNER
    assert (await h.client.delete(base + "/keys/" + issued.json()["key"]["key_id"], headers=token)).status_code == 204
    assert (await h.client.get("/v1/auth/me", headers=key)).status_code == 401


@pytest.mark.parametrize("change", ["malformed_home", "missing_home", "escalating_member"])
def test_native_personal_incomplete_home_or_escalating_member_never_establishes_owner(keystone_http, change):
    _configure_personal_identity(keystone_http)
    if change == "malformed_home":
        keystone_http["current_user_domain_id"] = []
    elif change == "missing_home":
        keystone_http["current_user_domain_id"] = "missing-home"
    else:
        keystone_http["edges"]["member"].append("admin")
    try:
        owner = validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    except HTTPException as error:
        assert error.status_code in {403, 503}
    else:
        assert not owner.get("personal_project_owner")


async def _personal_packages_for_deletion(h):
    from test_packages import image_archive, publish

    _configure_personal_identity(h.identity)
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    registered = await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})
    assert registered.status_code == 201, registered.text
    namespace = registered.json()["namespace"]
    base = "/v1/projects/" + namespace
    issued = await h.client.post(
        base + "/keys",
        headers=token,
        json={
            "name": "metadata deletion witness",
            "scope": {"packages": ["test", "other"]},
            "actions": ["packages:read", "packages:write"],
        },
    )
    assert issued.status_code == 201, issued.text
    key = {"Authorization": "Bearer " + issued.json()["secret"]}
    archive, body, blobs = image_archive()
    for package in ("test", "other"):
        response, _, _ = await publish(h, namespace, key, archive, body, package=package)
        assert response.status_code == 201, response.text
    return namespace, base, token, key, body, blobs


@pytest.mark.asyncio
async def test_native_personal_metadata_delete_preserves_cas_other_refs_and_hard_boundaries(
    native_http, tmp_path, monkeypatch
):
    from palimpsest_hub.models import PackageNamespace
    from palimpsest_hub.policy import KEY_ACTIONS

    h = native_http
    namespace, base, token, key, body, blobs = await _personal_packages_for_deletion(h)
    assert "packages:delete" not in KEY_ACTIONS
    assert (
        await h.client.post(
            base + "/keys",
            headers=token,
            json={
                "name": "not delegable",
                "scope": {"packages": ["test"]},
                "actions": ["packages:delete"],
            },
        )
    ).status_code == 422
    assert (await h.client.delete(base + "/package", headers=key, params={"package": "test"})).status_code == 403
    foreign = default_project_namespace(PROJECT_B)
    async with h.factory() as session:
        session.add(PackageNamespace(project_id=PROJECT_B, namespace=foreign, project_name="Foreign"))
        await session.commit()
    assert (
        await h.client.delete("/v1/projects/" + foreign + "/package", headers=token, params={"package": "test"})
    ).status_code == 403
    path = tmp_path / "delete-policy.yaml"
    path.write_text(json.dumps({"packages:delete": "@"}))
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(path))
    get_settings.cache_clear()
    for role in ("member", "palimpsest_admin"):
        h.identity["project_extras"] = {}
        h.identity["token_roles"] = h.identity["project_roles"] = [role]
        assert (await h.client.delete(base + "/package", headers=token, params={"package": "test"})).status_code == 403
    _configure_personal_identity(h.identity)
    h.identity["token_markers"] = {"application_credential": {"id": "reduced"}}
    assert (await h.client.delete(base + "/package", headers=token, params={"package": "test"})).status_code == 403
    h.identity["token_markers"] = {}
    path.write_text(json.dumps({"packages:delete": "!"}))
    assert (await h.client.delete(base + "/package", headers=token, params={"package": "test"})).status_code == 403
    path.write_text(json.dumps({"packages:delete": "@"}))
    response = await h.client.delete(base + "/package", headers=token, params={"package": "test"})
    assert response.status_code == 204, response.text
    version = base + "/versions/" + body["root_digest"]
    assert (await h.client.get(version, headers=key, params={"package": "test"})).status_code == 404
    assert (await h.client.get(version + "/download", headers=key, params={"package": "other"})).status_code == 200
    for digest, expected in blobs.items():
        assert h.store.blob_path(digest).read_bytes() == expected
    assert (await h.client.get("/v1/builds", headers=token)).status_code == 403
    assert (await h.client.delete("/v1/layers/" + body["root_digest"], headers=token)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["owner", "token", "provenance", "policy"])
async def test_native_personal_metadata_delete_revalidates_before_commit_and_rolls_back(
    native_http, tmp_path, monkeypatch, revocation
):
    from palimpsest_hub.services import package_registry as registry

    h = native_http
    namespace, base, token, key, body, blobs = await _personal_packages_for_deletion(h)
    original_member = registry.get_package_member_info
    observations = []
    policy_file = tmp_path / "delete-revocation.yaml"
    policy_file.write_text("{}")
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()

    async def revoke_after_first_observation(*, token_info):
        result = await original_member(token_info=token_info)
        observations.append(result)
        if len(observations) == 1:
            if revocation == "owner":
                h.identity["project_extras"]["afterglow_personal_user_id"] = "foreign"
            elif revocation == "token":
                h.identity["subject_revoked"] = True
            elif revocation == "provenance":
                h.identity["raw_project_assignments"] = [
                    {
                        "group": {"id": "foreign"},
                        "role": {"id": "role-member", "name": "member"},
                        "scope": {"project": {"id": PROJECT_A}},
                    }
                ]
            else:
                policy_file.write_text(json.dumps({"packages:delete": "!"}))
        return result

    monkeypatch.setattr(registry, "get_package_member_info", revoke_after_first_observation)
    response = await h.client.delete(base + "/package", headers=token, params={"package": "test"})
    assert response.status_code == (401 if revocation == "token" else 403), response.text
    assert observations
    _configure_personal_identity(h.identity)
    h.identity["subject_revoked"] = False
    h.identity.pop("raw_project_assignments", None)
    policy_file.write_text("{}")
    version = base + "/versions/" + body["root_digest"]
    for package in ("test", "other"):
        assert (await h.client.get(version + "/download", headers=key, params={"package": package})).status_code == 200
    for digest, expected in blobs.items():
        assert h.store.blob_path(digest).read_bytes() == expected


@pytest.mark.parametrize("kind", [None, False, "organization", ["personal"]])
def test_native_personal_explicit_invalid_domain_kind_is_not_absence(keystone_http, kind):
    _configure_personal_identity(keystone_http)
    keystone_http["domains"]["personal"]["afterglow_domain_kind"] = kind
    owner = validate_package_owner(FEDERATED_OWNER, PROJECT_A, allow_empty=True)
    assert not owner["personal_project_owner"] and not owner["package_actions"]


@pytest.mark.asyncio
async def test_me_rechecks_membership_for_a_retained_token(keystone_http):
    from palimpsest_hub.api.hub import get_hub_identity

    original = validate_token(ORIGINAL_SUBJECT)
    context = await get_hub_identity(original)
    assert context["capabilities"] == {
        "inventory": True,
        "download": True,
        "upload": True,
        "delete": False,
        "build": False,
    }
    keystone_http["project_roles"] = []
    revoked = await get_hub_identity(original)
    assert not any(revoked["capabilities"].values())
    assert revoked["roles"] == original["roles"]
    assert revoked["project_id"] == PROJECT_A and revoked["user_id"] == FEDERATED_OWNER


@pytest.mark.parametrize("elevated", ["admin", "manager", "service"])
def test_existing_native_owner_loses_authority_on_direct_system_grant(keystone_http, elevated):
    assert validate_package_owner(FEDERATED_OWNER, PROJECT_A)["can_write"] is True
    keystone_http["system_roles"] = [elevated]
    with pytest.raises(HTTPException) as revoked:
        validate_package_owner(FEDERATED_OWNER, PROJECT_A)
    assert revoked.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("elevated", ["admin", "manager", "service"])
async def test_native_key_and_finalize_recheck_direct_system_promotion(native_http, elevated):
    from test_packages import image_archive, staged

    h = native_http
    # Issue and stage as the original ordinary owner before its system promotion.
    namespace = default_project_namespace(PROJECT_A)
    token = {"X-Auth-Token": ORIGINAL_SUBJECT}
    base = f"/v1/projects/{namespace}"
    registered = await h.client.put(f"/v1/projects/{PROJECT_A}/namespace", headers=token, json={})
    assert registered.status_code == 201
    issued = await h.client.post(
        base + "/keys",
        headers=token,
        json={
            "name": "before promotion",
            "scope": {"packages": ["test"]},
            "actions": ["packages:inventory", "packages:read", "packages:write"],
        },
    )
    assert issued.status_code == 201
    credential = {"Authorization": "Bearer " + issued.json()["secret"]}
    archive, body, _ = image_archive()
    path, _ = await staged(h, namespace, credential, archive, body)
    h.identity["system_roles"] = [elevated]
    assert (await h.client.get("/v1/auth/me", headers=credential)).status_code == 403
    assert (await h.client.get("/v1/projects/current", headers=token)).status_code == (
        200 if elevated == "admin" else 403
    )
    assert (await h.client.put(path, params={"package": "test"}, headers=credential, json={})).status_code == 403
    h.identity["system_roles"] = []
    assert (await h.client.put(path, params={"package": "test"}, headers=credential, json={})).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["build", "delete"])
async def test_native_http_system_policy_withdrawal_at_final_mutation_boundary(
    native_http, tmp_path, monkeypatch, operation
):
    from sqlalchemy import select

    from palimpsest_hub.api import builds
    from palimpsest_hub.models import PalimpsestHubBuild, PalimpsestHubLayer

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    policy_file = tmp_path / "final-mutation-policy.yaml"
    policy_file.write_text("{}")
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("PALIMPSEST_HUB_BUILD_WORKER_KEY", "synthetic-final-boundary-worker-key-1234567890")
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    payload = b"raw base retained after policy withdrawal"
    opened = await h.client.post("/v1/uploads", headers=headers, json={})
    assert opened.status_code == 200, opened.text
    upload_path = "/v1/uploads/" + opened.json()["session_id"]
    assert (
        await h.client.patch(upload_path, headers={**headers, "Upload-Offset": "0"}, content=payload)
    ).status_code == 200
    finalized = await h.client.put(
        upload_path,
        headers={**headers, "Upload-Offset": str(len(payload))},
        json={
            "name": "policy-base",
            "kind": "cloud-image",
            "arch": "x86_64",
            "disk_format": "raw",
        },
    )
    assert finalized.status_code == 200, finalized.text
    digest = finalized.json()["blob_digest"]
    module = builds if operation == "build" else hub_api
    revalidate = module.revalidate_package_admin
    observed = []

    async def withdraw_before_final_validation(context):
        policy_file.write_text(json.dumps({"system:manage": "!"}))
        current = await revalidate(context)
        observed.append(current)
        return current

    monkeypatch.setattr(module, "revalidate_package_admin", withdraw_before_final_validation)
    if operation == "build":
        response = await h.client.post(
            "/v1/builds",
            headers=headers,
            json={
                "name": "denied-build",
                "base_digest": digest,
                "recipe": f"FROM {digest}\nRUN true",
            },
        )
    else:
        response = await h.client.delete("/v1/layers/" + digest, headers=headers)
    assert response.status_code == 403, response.text
    assert observed and all(current["is_package_system_admin"] for current in observed)
    assert all("system:manage" not in current["package_actions"] for current in observed)
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubBuild))).all() == []
        layer = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest))
        assert layer is not None and layer.project_id == PROJECT_B and layer.created_by == FEDERATED_OWNER
    assert h.store.blob_path(digest).read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,revocation",
    [
        (operation, revocation)
        for operation in ("upload", "bundle")
        for revocation in ("system-role", "original-token", "target-disabled", "write-policy")
    ]
    + [("upload", "system-policy")],
)
@pytest.mark.parametrize("retained_reference", [False, True])
async def test_native_http_admin_publication_revalidates_after_cas_and_compensates(
    native_http,
    tmp_path,
    monkeypatch,
    operation,
    revocation,
    retained_reference,
):
    from sqlalchemy import select
    from test_hub_api import _chain

    from palimpsest_hub.models import PalimpsestHubLayer, PalimpsestHubLayerAccess, PalimpsestHubUpload
    from palimpsest_hub.services.hub_bundle import iter_bundle_tar
    from palimpsest_hub.services.hub_store import LocalPathBlobStore

    h = native_http
    h.identity["system_roles"] = ["admin"]
    h.identity["project_roles"] = h.identity["token_roles"] = []
    policy_file = tmp_path / "after-cas-policy.yaml"
    policy_file.write_text("{}")
    monkeypatch.setenv("OSLO_POLICY_POLICY_FILE", str(policy_file))
    get_settings.cache_clear()
    headers = {"X-Auth-Token": ORIGINAL_SUBJECT, "X-Target-Project-Id": PROJECT_B}
    source = LocalPathBlobStore(tmp_path / "bundle-source")
    chain = _chain(source)
    first = chain[0]
    payload = source.blob_path(first.blob_digest).read_bytes()
    expected = (
        {first.blob_digest: payload}
        if operation == "upload"
        else {item.blob_digest: source.blob_path(item.blob_digest).read_bytes() for item in chain}
    )
    if retained_reference:
        h.store.blobs_dir.mkdir(parents=True, exist_ok=True)
        h.store.blob_path(first.blob_digest).write_bytes(payload)
        async with h.factory() as session:
            session.add(
                PalimpsestHubLayer(
                    blob_digest=first.blob_digest,
                    size_bytes=len(payload),
                    media_type=first.media_type,
                    config_digest="sha256:" + "7" * 64,
                    name="root",
                    kind="squashfs",
                    config_json={"name": "root"},
                    project_id=PROJECT_B if operation == "upload" else PROJECT_A,
                    created_by="retained-owner",
                )
            )
            await session.commit()
    upload_id = None
    if operation == "upload":
        opened = await h.client.post("/v1/uploads", headers=headers, json={})
        assert opened.status_code == 200, opened.text
        upload_id = opened.json()["session_id"]
        assert (
            await h.client.patch("/v1/uploads/" + upload_id, content=payload, headers={**headers, "Upload-Offset": "0"})
        ).status_code == 200
    run_blocking = hub_api._run_blocking
    published = []

    async def revoke_after_publication(function, *args, **kwargs):
        result = await run_blocking(function, *args, **kwargs)
        if (operation == "upload" and function == h.store.publish_verified) or (
            operation == "bundle" and function is hub_api._publish_staged_bundle
        ):
            published.append(True)
            assert all(h.store.blob_path(digest).read_bytes() == content for digest, content in expected.items())
            if revocation == "system-role":
                h.identity["system_roles"] = []
            elif revocation == "original-token":
                h.identity["subject_revoked"] = True
            elif revocation == "target-disabled":
                h.identity["target_enabled"] = False
            else:
                action = "packages:write" if revocation == "write-policy" else "system:manage"
                policy_file.write_text(json.dumps({action: "!"}))
        return result

    monkeypatch.setattr(hub_api, "_run_blocking", revoke_after_publication)
    if operation == "upload":
        response = await h.client.put(
            "/v1/uploads/" + upload_id,
            headers={**headers, "Upload-Offset": str(len(payload))},
            json={"name": "root", "is_published": True},
        )
    else:
        bundle = b"".join(iter_bundle_tar(source, [chain]))
        response = await h.client.post("/v1/bundles/import", headers=headers, files={"file": ("bundle.tar", bundle)})
    assert response.status_code == (401 if revocation == "original-token" else 403), response.text
    assert published
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubLayer))).all()
        assert len(rows) == int(retained_reference)
        if retained_reference:
            assert rows[0].blob_digest == first.blob_digest and rows[0].created_by == "retained-owner"
            assert rows[0].is_published is False
        assert (await session.scalars(select(PalimpsestHubLayerAccess))).all() == []
        if upload_id:
            row = await session.get(PalimpsestHubUpload, upload_id)
            assert row is not None and row.project_id == PROJECT_B and row.created_by == FEDERATED_OWNER
    for digest, content in expected.items():
        if retained_reference and digest == first.blob_digest:
            assert h.store.blob_path(digest).read_bytes() == content
        else:
            assert not h.store.exists(digest)
    if upload_id:
        assert h.store.upload_path(upload_id).read_bytes() == payload
