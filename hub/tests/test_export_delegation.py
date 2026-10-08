"""Deferred Glance export delegation boundary.

Real keystoneauth/keystoneclient/openstacksdk objects talk HTTP to a synthetic
Keystone that implements token validation, the current role graph, OS-TRUST
creation/deletion and trust-scoped password authentication. Glance byte I/O is
stubbed at the Hub's own `get_image`/`download_image` boundary, and qemu-img at
`_run_subprocess`; no real cloud, tenant image or credential is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import hub as hub_api
from palimpsest_hub.auth import _reader_client_for
from palimpsest_hub.config import get_settings
from palimpsest_hub.models import Base, PalimpsestImageExport, PalimpsestImageExportDelegation
from palimpsest_hub.rate_limit import limiter
from palimpsest_hub.services import image_exports
from palimpsest_hub.services.hub_store import LocalPathBlobStore

USER = "f" * 64
OTHER_USER = "7" * 32
PROJECT = "a" * 32
OTHER_PROJECT = "b" * 32
SERVICE_USER = "5" * 32
SERVICE_PROJECT = "9" * 32
VALIDATOR = "c" * 32
ORIGINAL = "original-requester-token"
IMAGE_ID = "11111111-2222-4333-8444-555555555555"
SOURCE = b"synthetic image bytes"
EDGES = {"member": ["reader"], "palimpsest-publish_editor": ["palimpsest-inventory_reader"]}
ROLE_NAMES = {
    "admin",
    "manager",
    "service",
    "member",
    "reader",
    "palimpsest-publish_editor",
    "palimpsest-inventory_reader",
    "palimpsest-download_user",
}


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch):
    values = {
        "DATABASE_URL": "mysql+asyncmy://user:pass@db/palimpsest",
        "REDIS_URL": "redis://redis/0",
        "PALIMPSEST_HUB_LOCAL_PATH": "/var/lib/palimpsest",
        "OS_AUTH_URL": "https://keystone.invalid/v3",
        "OS_USERNAME": "palimpsest",
        "OS_PASSWORD": "synthetic-service-secret",
        "OS_PROJECT_NAME": "palimpsest-service",
        "OS_READER_USERNAME": "read-only-validator",
        "OS_READER_PASSWORD": "synthetic-read-secret",
        "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS": json.dumps(["e" * 32]),
        "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS": json.dumps(["d" * 32]),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(limiter, "enabled", False)
    get_settings.cache_clear()
    _reader_client_for.cache_clear()
    yield
    get_settings.cache_clear()
    _reader_client_for.cache_clear()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _effective(names):
    result, pending = set(), list(names)
    while pending:
        name = pending.pop()
        if name not in result:
            result.add(name)
            pending.extend(EDGES.get(name, []))
    return result


@pytest.fixture
def keystone(monkeypatch: pytest.MonkeyPatch):
    state = {
        "project_roles": ["member", "palimpsest-publish_editor"],
        "user_enabled": True,
        "trusts": {},
        "auth": [],
        "trust_creates": [],
        "deletes": [],
        "create_mode": "normal",
        "trust_token_mode": "normal",
        "delete_unavailable": 0,
    }

    def role(name):
        return {"id": "role-" + name, "name": name}

    def user_token():
        now = datetime.now(UTC)
        return {
            "methods": ["password"],
            "expires_at": _iso(now + timedelta(hours=1)),
            "issued_at": _iso(now),
            "user": {"id": USER, "name": "requester", "domain": {"id": "default", "name": "Default"}},
            "roles": [role(name) for name in state["project_roles"]],
            "project": {"id": PROJECT, "name": "tenant", "domain": {"id": "default", "name": "Default"}},
            "catalog": [],
        }

    class Identity(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, body=None, subject=None):
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if subject:
                self.send_header("X-Subject-Token", subject)
            self.end_headers()
            self.wfile.write(payload)

        def body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

        def trust_for_token(self):
            token = self.headers.get("X-Auth-Token") or ""
            return state["trusts"].get(token.removeprefix("trust-token-")) if token.startswith("trust-token-") else None

        def do_POST(self):
            path = urlsplit(self.path).path
            body = self.body()
            if path == "/v3/auth/tokens":
                self.authenticate(body["auth"])
            elif path == "/v3/OS-TRUST/trusts":
                self.create_trust(body["trust"])
            else:
                self.send_json(404, {"error": {"message": "unknown"}})

        def authenticate(self, auth):
            password = auth["identity"]["password"]["user"]
            scope = auth.get("scope")
            state["auth"].append({"user": {k: v for k, v in password.items() if k != "password"}, "scope": scope})
            now = datetime.now(UTC)
            if password.get("name") == "read-only-validator":
                token = {
                    "methods": ["password"],
                    "expires_at": _iso(now + timedelta(hours=1)),
                    "issued_at": _iso(now),
                    "user": {
                        "id": VALIDATOR,
                        "name": "read-only-validator",
                        "domain": {"id": "default", "name": "Default"},
                    },
                    "roles": [role("reader")],
                    "system": {"all": True},
                    "catalog": [],
                }
                self.send_json(201, {"token": token}, "validator-subject")
                return
            service = password.get("name") == "palimpsest" or password.get("id") == SERVICE_USER
            if not service or password.get("password") != "synthetic-service-secret":
                self.send_json(401, {"error": {"message": "invalid credentials"}})
                return
            if scope == {"project": {"name": "palimpsest-service", "domain": {"name": "Default"}}}:
                token = {
                    "methods": ["password"],
                    "expires_at": _iso(now + timedelta(hours=1)),
                    "issued_at": _iso(now),
                    "user": {"id": SERVICE_USER, "name": "palimpsest", "domain": {"id": "default", "name": "Default"}},
                    "roles": [role("admin")],
                    "project": {
                        "id": SERVICE_PROJECT,
                        "name": "palimpsest-service",
                        "domain": {"id": "default", "name": "Default"},
                    },
                }
                self.send_json(201, {"token": token}, "service-project-token")
                return
            trust_id = (scope or {}).get("OS-TRUST:trust", {}).get("id")
            trust = state["trusts"].get(trust_id)
            if (
                password.get("id") != SERVICE_USER
                or trust is None
                or trust["deleted"]
                or trust["expires"] <= now
                or not state["user_enabled"]
                or not set(trust["roles"]) <= _effective(state["project_roles"])
            ):
                self.send_json(401, {"error": {"message": "trust cannot be used"}})
                return
            mode = state["trust_token_mode"]
            token = {
                "methods": ["password"],
                "expires_at": _iso(min(now + timedelta(hours=1), trust["expires"])),
                "issued_at": _iso(now),
                "user": {"id": USER, "name": "requester", "domain": {"id": "default", "name": "Default"}},
                # Keystone adds implied roles to trust tokens (member -> reader).
                "roles": [
                    role(name)
                    for name in sorted(_effective(trust["roles"]))
                    + {
                        "extra_role": ["admin"],
                        "extra_unrelated": ["palimpsest-download_user"],
                    }.get(mode, [])
                ],
                "project": {
                    "id": OTHER_PROJECT if mode == "swap_project" else PROJECT,
                    "name": "tenant",
                    "domain": {"id": "default", "name": "Default"},
                },
                "OS-TRUST:trust": {
                    "id": trust_id,
                    "impersonation": True,
                    "trustor_user": {"id": USER},
                    "trustee_user": {"id": SERVICE_USER},
                },
                "catalog": [],
            }
            self.send_json(201, {"token": token}, "trust-token-" + trust_id)

        def create_trust(self, trust):
            state["trust_creates"].append({"token": self.headers.get("X-Auth-Token"), "trust": trust})
            names = [item["name"] for item in trust.get("roles", [])]
            if (
                self.headers.get("X-Auth-Token") != ORIGINAL
                or trust.get("trustor_user_id") != USER
                or trust.get("trustee_user_id") != SERVICE_USER
                or not names
                or not set(names) <= _effective(state["project_roles"])
            ):
                self.send_json(403, {"error": {"message": "trust refused"}})
                return
            requested = datetime.fromisoformat(trust["expires_at"])
            expires = requested + timedelta(hours=1) if state["create_mode"] == "widen_expiry" else requested
            trust_id = uuid.uuid4().hex
            state["trusts"][trust_id] = {"roles": names, "expires": expires, "deleted": False, "trust": trust}
            self.send_json(
                201,
                {
                    "trust": {
                        "id": trust_id,
                        "trustor_user_id": USER,
                        "trustee_user_id": SERVICE_USER,
                        "project_id": trust["project_id"],
                        "impersonation": trust["impersonation"],
                        "roles": [role(name) for name in names],
                        "expires_at": _iso(expires),
                        "remaining_uses": None,
                    }
                },
            )

        def do_DELETE(self):
            path = urlsplit(self.path).path
            trust_id = path.rsplit("/", 1)[-1]
            trust = state["trusts"].get(trust_id)
            state["deletes"].append({"trust_id": trust_id, "token": self.headers.get("X-Auth-Token")})
            if not path.startswith("/v3/OS-TRUST/trusts/") or trust is None or trust["deleted"]:
                self.send_json(404, {"error": {"message": "missing trust"}})
                return
            if state["delete_unavailable"]:
                state["delete_unavailable"] -= 1
                self.send_json(503, {"error": {"message": "identity unavailable"}})
                return
            token_trust = self.trust_for_token()
            if self.headers.get("X-Auth-Token") != ORIGINAL and token_trust is not trust:
                self.send_json(403, {"error": {"message": "only the trustor can delete"}})
                return
            trust["deleted"] = True
            self.send_json(204)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if self.headers.get("X-Auth-Token") != "validator-subject":
                self.send_json(401, {"error": {"message": "validator required"}})
                return
            if parsed.path == "/v3/auth/tokens":
                if self.headers.get("X-Subject-Token") == ORIGINAL:
                    self.send_json(200, {"token": user_token()}, ORIGINAL)
                else:
                    self.send_json(404, {"error": {"message": "invalid subject"}})
            elif parsed.path == "/v3/roles":
                self.send_json(
                    200,
                    {
                        "roles": [
                            {"id": "role-" + name, "name": name, "domain_id": None} for name in sorted(ROLE_NAMES)
                        ],
                        "links": {"next": None},
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
                            for prior, implied in EDGES.items()
                            if implied
                        ],
                        "links": {"next": None},
                    },
                )
            elif parsed.path == f"/v3/users/{USER}":
                self.send_json(200, {"user": {"id": USER, "name": "requester", "enabled": state["user_enabled"]}})
            elif parsed.path == f"/v3/projects/{PROJECT}":
                self.send_json(
                    200, {"project": {"id": PROJECT, "name": "tenant", "enabled": True, "domain_id": "default"}}
                )
            elif parsed.path == "/v3/role_assignments":
                query = parse_qs(parsed.query)
                assignments = (
                    []
                    if query.get("scope.system")
                    else [
                        {"user": {"id": USER}, "scope": {"project": {"id": PROJECT}}, "role": role(name)}
                        for name in state["project_roles"]
                    ]
                )
                self.send_json(200, {"role_assignments": assignments})
            else:
                self.send_json(404, {"error": {"message": "unknown"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Identity)
    monkeypatch.setenv("OS_AUTH_URL", f"http://127.0.0.1:{server.server_port}/v3")
    get_settings.cache_clear()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _image():
    return SimpleNamespace(
        id=IMAGE_ID,
        name="tenant-image",
        status="active",
        disk_format="raw",
        size=len(SOURCE),
        virtual_size=len(SOURCE),
        checksum=None,
        os_hash_algo="sha256",
        os_hash_value=hashlib.sha256(SOURCE).hexdigest(),
        updated_at="2026-10-07T00:00:00Z",
        owner=PROJECT,
        visibility="private",
    )


@pytest.fixture
async def hub(keystone, tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    glance = []

    def get_image(conn, image_id):
        glance.append(("get", conn, image_id))
        return _image()

    class Download:
        def iter_content(self, chunk_size):
            yield SOURCE

        def close(self):
            pass

    def download_image(conn, image_id):
        glance.append(("download", conn, image_id))
        return Download()

    async def qemu(argv, **kwargs):
        if argv[1] == "measure":
            return 0, json.dumps({"required": len(SOURCE)}), ""
        return 0, json.dumps({"format": "raw", "virtual-size": len(SOURCE)}), ""

    for module in (hub_api, image_exports):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
        monkeypatch.setattr(module, "get_blob_store", lambda: store)
    monkeypatch.setattr(image_exports, "get_image", get_image)
    monkeypatch.setattr(image_exports, "download_image", download_image)
    monkeypatch.setattr(image_exports, "_run_subprocess", qemu)
    app = FastAPI()
    app.include_router(hub_api.router, prefix="/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
        yield SimpleNamespace(client=client, identity=keystone, factory=factory, store=store, glance=glance)
    await engine.dispose()


async def _admit(h, expected=202):
    response = await h.client.post(
        "/v1/image-exports",
        headers={"X-Auth-Token": ORIGINAL},
        json={"image_id": IMAGE_ID, "disk_format": "raw"},
    )
    assert response.status_code == expected, response.text
    return response.json()


async def _rows(h):
    async with h.factory() as session:
        exports = list((await session.execute(select(PalimpsestImageExport))).scalars())
        delegations = list((await session.execute(select(PalimpsestImageExportDelegation))).scalars())
    return exports, delegations


def _worker_glance(h):
    return [call for call in h.glance if getattr(call[1].session.auth, "trust_id", None)]


def _assert_no_tenant_service_password(identity):
    """The service password is used only for its own project or as a trust trustee."""
    service_project = {"project": {"name": "palimpsest-service", "domain": {"name": "Default"}}}
    for event in identity["auth"]:
        user = event["user"]
        if user.get("name") == "palimpsest" or user.get("id") == SERVICE_USER:
            scope = event["scope"] or {}
            assert scope == service_project or set(scope) == {"OS-TRUST:trust"}


async def test_admission_creates_bounded_impersonating_least_role_trust_and_worker_uses_only_it(hub):
    h = hub
    created = await _admit(h)
    assert created["status"] == "queued"
    [create] = h.identity["trust_creates"]
    trust = create["trust"]
    assert create["token"] == ORIGINAL
    assert (trust["trustor_user_id"], trust["trustee_user_id"], trust["project_id"]) == (USER, SERVICE_USER, PROJECT)
    assert trust["impersonation"] is True and [r["name"] for r in trust["roles"]] == ["member"]
    requested = datetime.fromisoformat(trust["expires_at"])
    assert timedelta(0) < requested - datetime.now(UTC) <= timedelta(seconds=21600)
    exports, [delegation] = await _rows(h)
    assert delegation.state == image_exports.DELEGATION_ACTIVE and delegation.export_id == exports[0].id
    assert (delegation.project_id, delegation.trustor_user_id, delegation.trustee_user_id) == (
        PROJECT,
        USER,
        SERVICE_USER,
    )
    assert delegation.role_names == ["member"]
    persisted = {column.name: getattr(delegation, column.name) for column in delegation.__table__.columns}
    assert ORIGINAL not in json.dumps(persisted, default=str)
    assert "synthetic-service-secret" not in json.dumps(persisted, default=str)

    assert await image_exports.process_one_image_export(owner="worker-1") is True
    exports, [delegation] = await _rows(h)
    assert exports[0].status == image_exports.STATUS_COMPLETE
    worker_calls = _worker_glance(h)
    assert [call[0] for call in worker_calls] == ["get", "download"]
    for _, conn, _ in worker_calls:
        assert conn.session.auth.trust_id == delegation.trust_id
        assert conn.session.get_token() == "trust-token-" + delegation.trust_id
    trust_auth = [e for e in h.identity["auth"] if e["scope"] and "OS-TRUST:trust" in e["scope"]]
    assert trust_auth and all(e["user"] == {"id": SERVICE_USER} for e in trust_auth)
    _assert_no_tenant_service_password(h.identity)
    # Keystone's implied `reader` on the trust token is accepted; only the closure is allowed.
    assert set(worker_calls[0][1].session.auth.get_access(worker_calls[0][1].session).role_names) == {
        "member",
        "reader",
    }
    # Terminal success retires and deletes the requester's Trust through its own impersonating token.
    assert delegation.state == image_exports.DELEGATION_DELETED
    assert h.identity["trusts"][delegation.trust_id]["deleted"] is True
    assert h.identity["deletes"][-1]["token"] == "trust-token-" + delegation.trust_id


@pytest.mark.parametrize(
    "change,code",
    [
        ("role_removed", "authorization_revoked"),
        ("publish_removed", "authorization_revoked"),
        ("user_disabled", "authorization_revoked"),
        ("trust_revoked", "delegation_revoked"),
        ("swap_project", "delegation_scope_mismatch"),
        ("extra_role", "delegation_scope_mismatch"),
        ("extra_unrelated", "delegation_scope_mismatch"),
        ("expired", "delegation_expired"),
        ("row_swapped", "delegation_scope_mismatch"),
        ("legacy_without_delegation", "delegation_required"),
    ],
)
async def test_revoked_expired_or_swapped_scope_fails_before_glance_without_fallback(hub, change, code):
    h = hub
    await _admit(h)
    _, [delegation] = await _rows(h)
    if change == "role_removed":
        h.identity["project_roles"] = []
    elif change == "publish_removed":
        h.identity["project_roles"] = ["member"]
    elif change == "user_disabled":
        h.identity["user_enabled"] = False
    elif change == "trust_revoked":
        h.identity["trusts"][delegation.trust_id]["deleted"] = True
    elif change in ("swap_project", "extra_role", "extra_unrelated"):
        h.identity["trust_token_mode"] = change
    else:
        async with h.factory() as session, session.begin():
            table = PalimpsestImageExportDelegation
            target = update(table).where(table.trust_id == delegation.trust_id)
            if change == "expired":
                await session.execute(target.values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
            elif change == "row_swapped":
                await session.execute(target.values(project_id=OTHER_PROJECT))
            else:
                await session.execute(target.values(state=image_exports.DELEGATION_DELETED))

    assert await image_exports.process_one_image_export(owner="worker-1") is True
    [job], [stored] = await _rows(h)
    assert (job.status, job.error_code) == (image_exports.STATUS_ERROR, code)
    assert _worker_glance(h) == []
    _assert_no_tenant_service_password(h.identity)
    assert stored.state != image_exports.DELEGATION_ACTIVE
    # A failed authorization is terminal; nothing reclaims and reruns the export.
    assert await image_exports.process_one_image_export(owner="worker-2") is False


async def test_cleanup_failure_is_retried_without_repeating_the_export(hub):
    h = hub
    await _admit(h)
    h.identity["delete_unavailable"] = 1
    assert await image_exports.process_one_image_export(owner="worker-1") is True
    [job], [delegation] = await _rows(h)
    assert job.status == image_exports.STATUS_COMPLETE
    assert delegation.state == image_exports.DELEGATION_CLEANUP and delegation.cleanup_attempts == 1
    downloads = len([c for c in _worker_glance(h) if c[0] == "download"])
    async with h.factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExportDelegation).values(cleanup_next_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await image_exports.run_delegation_cleanup() == 1
    [job], [delegation] = await _rows(h)
    assert job.status == image_exports.STATUS_COMPLETE
    assert delegation.state == image_exports.DELEGATION_DELETED
    assert h.identity["trusts"][delegation.trust_id]["deleted"] is True
    assert await image_exports.process_one_image_export(owner="worker-2") is False
    assert len([c for c in _worker_glance(h) if c[0] == "download"]) == downloads


async def test_unusable_trust_cleanup_waits_for_finite_expiry(hub):
    h = hub
    await _admit(h)
    h.identity["project_roles"] = []
    assert await image_exports.process_one_image_export(owner="worker-1") is True
    _, [delegation] = await _rows(h)
    assert delegation.state == image_exports.DELEGATION_CLEANUP
    assert h.identity["trusts"][delegation.trust_id]["deleted"] is False
    async with h.factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExportDelegation).values(
                cleanup_next_at=datetime.now(UTC) - timedelta(seconds=1),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    assert await image_exports.run_delegation_cleanup() == 1
    _, [delegation] = await _rows(h)
    assert delegation.state == image_exports.DELEGATION_EXPIRED


@pytest.mark.parametrize("trustor_delete_available", [True, False])
async def test_abandoned_admission_trust_is_deleted_or_durably_queued(hub, monkeypatch, trustor_delete_available):
    h = hub

    async def fail_bind(*_args, **_kwargs):
        raise image_exports.ImageExportError(503, "Export delegation is unavailable", code="delegation_unavailable")

    monkeypatch.setattr(image_exports, "_bind_delegation", fail_bind)
    if not trustor_delete_available:
        h.identity["delete_unavailable"] = 1
    await _admit(h, expected=503)
    exports, [delegation] = await _rows(h)
    assert exports == [] and delegation.export_id is None
    if trustor_delete_available:
        assert delegation.state == image_exports.DELEGATION_DELETED
        assert h.identity["deletes"][-1]["token"] == ORIGINAL
        return
    assert delegation.state == image_exports.DELEGATION_CLEANUP
    assert await image_exports.run_delegation_cleanup() == 1
    _, [delegation] = await _rows(h)
    assert delegation.state == image_exports.DELEGATION_DELETED
    assert h.identity["deletes"][-1]["token"] == "trust-token-" + delegation.trust_id


async def test_crashed_admission_pending_trust_is_swept_after_grace(hub):
    h = hub
    await _admit(h)
    _, [delegation] = await _rows(h)
    async with h.factory() as session, session.begin():
        await session.execute(update(PalimpsestImageExport).values(status=image_exports.STATUS_ERROR))
        await session.execute(
            update(PalimpsestImageExportDelegation).values(
                state=image_exports.DELEGATION_PENDING,
                export_id=None,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    assert await image_exports.run_delegation_cleanup() == 1
    _, [delegation] = await _rows(h)
    assert delegation.state == image_exports.DELEGATION_DELETED


async def test_byte_reuse_creates_no_delegation(hub, tmp_path):
    h = hub
    image = _image()
    fingerprint = image_exports.compute_source_fingerprint(
        image_id=image.id,
        disk_format=image.disk_format,
        size_bytes=image.size,
        virtual_size_bytes=image.virtual_size,
        checksum=image.checksum,
        hash_algo=image.os_hash_algo,
        hash_value=image.os_hash_value,
        updated_at=image.updated_at,
    )
    source = tmp_path / "blob"
    source.write_bytes(SOURCE)
    finalized = h.store.inspect_file(source, max_bytes=1024)
    h.store.publish_verified(source, finalized)
    now = datetime.now(UTC)
    async with h.factory() as session, session.begin():
        session.add(
            PalimpsestImageExport(
                id=str(uuid.uuid4()),
                project_id=OTHER_PROJECT,
                created_by=OTHER_USER,
                source_image_id=IMAGE_ID,
                source_name="tenant-image",
                source_disk_format="raw",
                source_size_bytes=len(SOURCE),
                source_fingerprint=fingerprint,
                artifact_key=image_exports.compute_artifact_key(fingerprint, "raw"),
                target_disk_format="raw",
                result_blob_digest=finalized.blob_digest,
                result_size_bytes=len(SOURCE),
                status=image_exports.STATUS_COMPLETE,
                progress_pct=100,
                attempts=0,
                next_at=now,
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
    created = await _admit(h)
    assert created["status"] == image_exports.STATUS_COMPLETE
    assert h.identity["trust_creates"] == []
    assert (await _rows(h))[1] == []


@pytest.mark.parametrize("reset_reason", ["error", "deleted", "missing_blob"])
async def test_new_export_never_reuses_previous_creator_delegation(hub, reset_reason):
    h = hub
    await _admit(h)
    _, [first] = await _rows(h)
    # Every reset replaces an earlier creator's authority, not just an error retry.
    async with h.factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExport).values(
                status=image_exports.STATUS_ERROR if reset_reason == "error" else image_exports.STATUS_COMPLETE,
                created_by=OTHER_USER,
                deleted_at=datetime.now(UTC) if reset_reason == "deleted" else None,
                result_blob_digest="sha256:" + "0" * 64 if reset_reason == "missing_blob" else None,
            )
        )
        await session.execute(update(PalimpsestImageExportDelegation).values(trustor_user_id=OTHER_USER))
    await _admit(h)
    [job], delegations = await _rows(h)
    current = next(d for d in delegations if d.trust_id != first.trust_id)
    previous = next(d for d in delegations if d.trust_id == first.trust_id)
    assert job.status == image_exports.STATUS_QUEUED and job.created_by == USER
    assert current.state == image_exports.DELEGATION_ACTIVE and current.trustor_user_id == USER
    assert previous.state == image_exports.DELEGATION_CLEANUP
    assert await image_exports.process_one_image_export(owner="worker-1") is True
    assert all(call[1].session.auth.trust_id == current.trust_id for call in _worker_glance(h))


async def test_delegated_role_must_be_currently_possessed(hub, monkeypatch):
    h = hub
    saved = {name: list(implied) for name, implied in EDGES.items()}
    monkeypatch.setenv("PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES", '["reader"]')
    get_settings.cache_clear()
    try:
        EDGES["member"] = []
        await _admit(h, expected=403)
    finally:
        EDGES.clear()
        EDGES.update(saved)
    assert h.identity["trust_creates"] == []
    assert await _rows(h) == ([], [])


async def test_trust_wider_than_requested_is_deleted_and_refused(hub):
    h = hub
    h.identity["create_mode"] = "widen_expiry"
    await _admit(h, expected=403)
    [trust] = h.identity["trusts"].values()
    assert trust["deleted"] is True
    assert await _rows(h) == ([], [])


async def test_soft_deleted_queued_export_retires_delegation(hub):
    h = hub
    created = await _admit(h)
    deleted = await h.client.delete(f"/v1/image-exports/{created['id']}", headers={"X-Auth-Token": ORIGINAL})
    assert deleted.status_code == 204
    _, [delegation] = await _rows(h)
    assert delegation.state == image_exports.DELEGATION_CLEANUP
    assert await image_exports.run_delegation_cleanup() == 1
    assert await image_exports.process_one_image_export(owner="worker-1") is False
    assert _worker_glance(h) == []


@pytest.mark.parametrize("lease_seconds,expected", [(-1, 204), (3600, 409)])
async def test_soft_delete_claimed_export_normalizes_database_utc_lease(hub, lease_seconds, expected):
    h = hub
    created = await _admit(h)
    async with h.factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExport).values(
                status=image_exports.STATUS_DOWNLOADING,
                lease_owner="worker-1",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
            )
        )
    deleted = await h.client.delete(f"/v1/image-exports/{created['id']}", headers={"X-Auth-Token": ORIGINAL})
    assert deleted.status_code == expected
    [job], [delegation] = await _rows(h)
    if expected == 204:
        assert job.deleted_at is not None
        assert delegation.state == image_exports.DELEGATION_CLEANUP
        assert await image_exports.run_delegation_cleanup() == 1
    else:
        assert job.deleted_at is None
        assert delegation.state == image_exports.DELEGATION_ACTIVE
        assert deleted.json()["detail"] == "A currently leased export job cannot be deleted"
    assert _worker_glance(h) == []


async def test_attempt_exhaustion_retires_delegation_without_glance(hub):
    h = hub
    await _admit(h)
    async with h.factory() as session, session.begin():
        await session.execute(update(PalimpsestImageExport).values(attempts=3))
    assert await image_exports.process_one_image_export(owner="worker-1") is False
    [job], [delegation] = await _rows(h)
    assert (job.status, job.error_code) == (image_exports.STATUS_ERROR, "attempts_exhausted")
    assert delegation.state == image_exports.DELEGATION_CLEANUP
    assert await image_exports.run_delegation_cleanup() == 1
    assert _worker_glance(h) == []


async def test_stale_cleanup_cannot_overwrite_reclaimed_cleanup_lease(hub, monkeypatch):
    h = hub
    created = await _admit(h)
    await image_exports.soft_delete_project_export(PROJECT, created["id"])
    started, release = threading.Event(), threading.Event()
    now = datetime.now(UTC)
    monkeypatch.setattr(image_exports, "_now", lambda: now)

    def slow_first_delete(_delegation):
        if not started.is_set():
            started.set()
            assert release.wait(timeout=10)
        return False

    monkeypatch.setattr(image_exports, "delete_export_trust", slow_first_delete)
    first = asyncio.create_task(image_exports.run_delegation_cleanup())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        # The first worker outlives its five-minute lease; a second worker reclaims it.
        now += timedelta(minutes=6)
        assert await image_exports.run_delegation_cleanup() == 0
        _, [newer] = await _rows(h)
        assert newer.cleanup_attempts == 2
        now += timedelta(seconds=30)
    finally:
        release.set()
        await first
    _, [stored] = await _rows(h)
    assert stored.state == image_exports.DELEGATION_CLEANUP
    assert stored.cleanup_attempts == 2
    assert stored.cleanup_next_at == newer.cleanup_next_at
    assert stored.updated_at == newer.updated_at
    assert _worker_glance(h) == []


@pytest.mark.parametrize(
    "name,value",
    [
        ("PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES", "[]"),
        ("PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES", '["Admin"]'),
        ("PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES", '["reader", "manager"]'),
        ("PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES", '["service"]'),
        ("PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES", '[" reader"]'),
        ("PALIMPSEST_HUB_EXPORT_DELEGATION_TTL_SECONDS", "0"),
        ("PALIMPSEST_HUB_EXPORT_DELEGATION_TTL_SECONDS", "86401"),
    ],
)
def test_administrative_empty_or_unbounded_delegation_settings_fail_startup(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()
