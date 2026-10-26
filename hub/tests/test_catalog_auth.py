"""Catalog-only native HTTP authority regressions, preserving existing project auth consumers."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from palimpsest_hub.auth import _reader_client_for
from palimpsest_hub.config import get_settings

PROJECT_A = "a" * 32
PROJECT_B = "b" * 32
FEDERATED_OWNER = "f" * 64
ORIGINAL_SUBJECT = "original-project-subject"

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

    from palimpsest_hub.api import catalog, hub, packages
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
    for router in (packages.router, hub.router, catalog.router):
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
@pytest.mark.parametrize("grant", ["direct", "group", "group-implied"])
async def test_catalog_current_raw_system_grants_are_authoritative(native_http, grant):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["project_roles"] = h.identity["token_roles"] = []
    if grant == "direct":
        h.identity["system_roles"] = ["admin"]
    else:
        h.identity["groups"] = ["catalog-operators"]
        h.identity["group_system_roles"] = {"catalog-operators": ["admin" if grant == "group" else "palimpsest_admin"]}
        if grant == "group-implied":
            h.identity["edges"]["palimpsest_admin"].append("admin")
    response = await h.client.get("/v1/admin/catalog/layers", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 200, response.text
    h.identity["system_roles"] = []
    h.identity["groups"] = []
    assert (
        await h.client.get("/v1/admin/catalog/layers", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    ).status_code == 403


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
        "directory_unavailable",
        "directory_truncated",
    ],
)
async def test_catalog_partial_or_unavailable_system_authority_is_503(native_http, failure):
    h = native_http
    h.identity["subject_scope"] = "system"
    h.identity["project_roles"] = h.identity["token_roles"] = []
    h.identity["system_roles"] = ["admin"]
    h.identity[failure] = True
    response = await h.client.get("/v1/admin/catalog/layers", headers={"X-Auth-Token": ORIGINAL_SUBJECT})
    assert response.status_code == 503, response.text
