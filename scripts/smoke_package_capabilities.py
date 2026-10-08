#!/usr/bin/env python3
"""Docker-backed scoped package capability acceptance for Palimpsest Hub.

The host driver (``run``) never builds the API or worker images. It derives one
smoke-only image from the supplied API image by unpacking the ``hub/uv.lock``
pinned, hash-verified aiosqlite wheel into its venv, because the production
image intentionally ships only the MySQL driver. That derived image is never a
publication artifact; the report records both image IDs.

The API container serves the unmodified registered ``palimpsest_hub.main:app``
(routes, middleware and exception handlers) through uvicorn with lifespan off.
``hub-serve`` replicates the lifespan except ``init_db``: production
``init_db`` passes MySQL ``connect_timeout`` to every driver, which SQLite
rejects, so the harness installs its own SQLite engine/session factory and
records the canonical ``palimpsest-hub-bootstrap`` failure as an observation.
This is isolated acceptance of the HTTP contract, not proof of the canonical
database, lifespan or deployment.

Identity is a synthetic Keystone HTTP directory (``keystone-serve``) with
unique global role IDs, mutable implication edges and live assignments. The
Hub authenticates its read-only validator with its real keystoneauth/
keystoneclient stack; driver tokens are minted only on a separate control port.
Redis is a real container, blobs and SQLite live in retained named volumes.
No KVM, Glance, Nova or other cloud service is contacted.

Top-level imports are stdlib only: the same file runs as the host driver and,
read-only mounted, as the in-container helper subcommands.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

SCRIPT_IN_CONTAINER = "/smoke/smoke_package_capabilities.py"
SQL_MOUNT = "/var/lib/palimpsest-sql"
BLOB_MOUNT = "/var/lib/palimpsest"
DATABASE_URL = f"sqlite+aiosqlite:///{SQL_MOUNT}/hub.sqlite"
HUB_PORT = 8020
IDENTITY_PORT = 5000
CONTROL_PORT = 5001
LABEL = "org.palimpsest.smoke"
LABEL_VALUE = "scoped-package-capabilities"
PUBLIC_ORIGIN = "https://packages.smoke.invalid"

# Keystone defaults plus the Palimpsest service graph used by hub/tests/test_auth.py.
ROLE_EDGES = (
    ("admin", "member"),
    ("member", "reader"),
    ("palimpsest_admin", "palimpsest_editor"),
    ("palimpsest_admin", "palimpsest-keys_admin"),
    ("palimpsest_editor", "palimpsest_user"),
    ("palimpsest_editor", "palimpsest-publish_editor"),
    ("palimpsest_editor", "palimpsest-keys_editor"),
    ("palimpsest_user", "palimpsest_reader"),
    ("palimpsest_user", "palimpsest-download_user"),
    ("palimpsest_reader", "palimpsest-inventory_reader"),
    ("palimpsest-download_user", "palimpsest_reader"),
    ("palimpsest-publish_editor", "palimpsest_reader"),
    ("palimpsest-keys_editor", "palimpsest_reader"),
    ("palimpsest-keys_admin", "palimpsest_reader"),
)
ROLE_NAMES = sorted(
    {"admin", "manager", "service", "member", "reader", "project_admin", "project_member"}
    | {name for edge in ROLE_EDGES for name in edge}
)
ALL_ACTIONS = ["packages:inventory", "packages:read", "packages:write", "cache:read", "cache:write"]
PLATFORM_MACHINE = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}


# ---------------------------------------------------------------------------
# Shared fixtures (host driver and in-container helpers)
# ---------------------------------------------------------------------------


def sha256_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def synthetic_bytes(label: str, size: int) -> bytes:
    """Deterministic, label-bound bytes the driver can recompute independently."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{label}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def _canonical_json(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _tar(files) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in files:
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    return result.getvalue()


def oci_archive(marker: bytes, architecture: str):
    """One-layer OCI image layout, same shape as hub/tests/test_packages.py image_archive."""
    layer = _tar([("marker", marker)])
    config = _canonical_json(
        {
            "architecture": architecture,
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [sha256_digest(layer)]},
            "config": {"Cmd": ["/bin/sh"]},
        }
    )
    root = _canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "digest": sha256_digest(config),
                "size": len(config),
                "mediaType": "application/vnd.oci.image.config.v1+json",
            },
            "layers": [
                {
                    "digest": sha256_digest(layer),
                    "size": len(layer),
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                }
            ],
        }
    )
    blobs = {sha256_digest(layer): layer, sha256_digest(config): config, sha256_digest(root): root}
    index = _canonical_json(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": sha256_digest(root),
                    "size": len(root),
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                }
            ],
        }
    )
    archive = _tar(
        [
            ("oci-layout", _canonical_json({"imageLayoutVersion": "1.0.0"})),
            ("index.json", index),
            *[("blobs/sha256/" + key[7:], value) for key, value in blobs.items()],
        ]
    )
    return archive, sha256_digest(root), sha256_digest(layer), blobs


# ---------------------------------------------------------------------------
# Synthetic Keystone (runs inside a container on the private network)
# ---------------------------------------------------------------------------


class SyntheticKeystone:
    """Current, mutable identity directory; every response is computed from live state."""

    def __init__(self, seed: dict):
        self.lock = threading.RLock()
        self.validator = seed["validator"]
        self.roles = {role["id"]: {"id": role["id"], "name": role["name"], "domain_id": None} for role in seed["roles"]}
        self.ids = {role["name"]: role["id"] for role in seed["roles"]}
        self.edges = {role_id: set() for role_id in self.roles}
        for prior, implied in seed["edges"]:
            self.edges[self.ids[prior]].add(self.ids[implied])
        self.users = {user["id"]: dict(user) for user in seed["users"]}
        self.users[self.validator["id"]] = {
            "id": self.validator["id"],
            "name": self.validator["name"],
            "label": "validator",
            "enabled": True,
        }
        self.projects = {project["id"]: dict(project) for project in seed["projects"]}
        self.assignments: set[tuple[str, str, str, str]] = set()
        for assignment in seed["assignments"]:
            self.assign(assignment, True)
        self.tokens: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.extra_roles: dict[str, str] = {}

    # -- state mutation (control port only) --------------------------------
    def assign(self, spec: dict, present: bool) -> None:
        if spec.get("system"):
            item = (spec["user"], "system", "all", self.ids[spec["role"]])
        else:
            item = (spec["user"], "project", spec["project"], self.ids[spec["role"]])
        with self.lock:
            if present:
                self.assignments.add(item)
            else:
                self.assignments.discard(item)

    def edge(self, prior: str, implied_id: str, present: bool) -> None:
        with self.lock:
            target = self.edges[self.ids[prior]]
            if present:
                target.add(implied_id)
            else:
                target.discard(implied_id)

    def duplicate_role(self, name: str, present: bool) -> None:
        with self.lock:
            if present:
                role_id = uuid.uuid4().hex
                self.extra_roles[name] = role_id
                self.roles[role_id] = {"id": role_id, "name": name, "domain_id": None}
                self.edges[role_id] = set()
            else:
                role_id = self.extra_roles.pop(name)
                del self.roles[role_id]
                del self.edges[role_id]

    def expand(self, role_ids) -> list[str]:
        seen: list[str] = []
        queue = list(role_ids)
        while queue:
            role_id = queue.pop(0)
            if role_id in seen or role_id not in self.roles:
                continue
            seen.append(role_id)
            queue.extend(sorted(self.edges.get(role_id, ())))
        return seen

    def _issue(self, user_id: str, *, project_id: str | None, role_ids: list[str]) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        self.tokens[token] = {
            "user_id": user_id,
            "project_id": project_id,
            "role_ids": role_ids,
            "issued_at": now,
            "expires_at": now + timedelta(hours=4),
            "label": self.users[user_id].get("label", "unknown"),
        }
        return token

    def mint(self, user_id: str, project_id: str) -> str:
        """Project token carrying the issue-time role snapshot (Keystone expands implied roles)."""
        with self.lock:
            direct = sorted(
                role
                for user, kind, target, role in self.assignments
                if user == user_id and kind == "project" and target == project_id
            )
            if not direct or not self.users[user_id].get("enabled") or not self.projects[project_id].get("enabled"):
                raise PermissionError("no current project role assignment")
            return self._issue(user_id, project_id=project_id, role_ids=self.expand(direct))

    # -- identity API ------------------------------------------------------
    def _role_ref(self, role_id: str) -> dict:
        return {"id": role_id, "name": self.roles[role_id]["name"]}

    def token_body(self, token: str) -> dict:
        record = self.tokens[token]
        user = self.users[record["user_id"]]
        body = {
            "methods": ["password"],
            "audit_ids": [hashlib.sha256(token.encode()).hexdigest()[:22]],
            "expires_at": record["expires_at"].strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "issued_at": record["issued_at"].strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "user": {"id": user["id"], "name": user["name"], "domain": {"id": "default", "name": "Default"}},
            "roles": [self._role_ref(role_id) for role_id in record["role_ids"] if role_id in self.roles],
            "catalog": [],
            "is_domain": False,
        }
        if record["project_id"] is None:
            body["system"] = {"all": True}
        else:
            project = self.projects[record["project_id"]]
            body["project"] = {
                "id": project["id"],
                "name": project["name"],
                "domain": {"id": "default", "name": "Default"},
            }
        return {"token": body}

    def _valid(self, token: str | None) -> dict | None:
        record = self.tokens.get(token or "")
        if record is None or record["expires_at"] <= datetime.now(UTC):
            return None
        return record

    def _label(self, token: str | None) -> str:
        if not token:
            return "none"
        record = self._valid(token)
        return record["label"] if record else "unknown"

    def authenticate(self, body: dict) -> tuple[int, dict, str | None]:
        with self.lock:
            identity = body.get("auth", {}).get("identity", {})
            methods = identity.get("methods")
            password = identity.get("password", {}).get("user", {})
            scope = body.get("auth", {}).get("scope", {})
            entry = {
                "method": "POST",
                "path": "/v3/auth/tokens",
                "methods": methods,
                "scope": sorted(scope) if isinstance(scope, dict) else "invalid",
            }
            if (
                methods == ["password"]
                and password.get("name") == self.validator["name"]
                and secrets.compare_digest(str(password.get("password", "")), self.validator["password"])
                and scope == {"system": {"all": True}}
            ):
                direct = [
                    role
                    for user, kind, _, role in self.assignments
                    if user == self.validator["id"] and kind == "system"
                ]
                token = self._issue(self.validator["id"], project_id=None, role_ids=self.expand(direct))
                entry.update(principal="validator", status=201)
                self.requests.append(entry)
                return 201, self.token_body(token), token
            entry.update(principal="rejected", status=401)
            self.requests.append(entry)
            return 401, {"error": {"code": 401, "message": "Only the read-only validator may authenticate"}}, None

    def get(self, path: str, query: dict, actor: str | None, subject: str | None) -> tuple[int, dict, str | None]:
        with self.lock:
            entry = {"method": "GET", "path": self._template(path), "actor": self._label(actor)}
            if subject is not None:
                entry["subject"] = self._label(subject)
            status, body, header = self._get(path, query, actor, subject)
            entry["status"] = status
            self.requests.append(entry)
            return status, body, header

    @staticmethod
    def _template(path: str) -> str:
        for prefix in ("/v3/roles/", "/v3/users/", "/v3/projects/"):
            if path.startswith(prefix) and len(path) > len(prefix):
                return prefix + "{id}"
        return path

    def _get(self, path, query, actor, subject):
        if path.rstrip("/") in ("", "/v3"):
            version = {
                "id": "v3.14",
                "status": "stable",
                "updated": "2026-01-01T00:00:00Z",
                "links": [{"rel": "self", "href": "http://keystone:5000/v3/"}],
                "media-types": [{"base": "application/json", "type": "application/vnd.openstack.identity-v3+json"}],
            }
            return (
                (200, {"version": version}, None)
                if path.rstrip("/") == "/v3"
                else (300, {"versions": {"values": [version]}}, None)
            )
        record = self._valid(actor)
        if record is None or record["user_id"] != self.validator["id"]:
            return 401, {"error": {"code": 401, "message": "Read-only validator token required"}}, None
        if path == "/v3/auth/tokens":
            if self._valid(subject) is None:
                return 404, {"error": {"code": 404, "message": "Could not find token"}}, None
            return 200, self.token_body(subject), subject
        if path == "/v3/roles":
            roles = [dict(role) for role in self.roles.values() if role["domain_id"] is None]
            return 200, {"roles": roles, "links": {"self": None, "previous": None, "next": None}}, None
        if path == "/v3/role_inferences":
            inferences = []
            for prior, implied in sorted(self.edges.items()):
                if implied and prior in self.roles:
                    inferences.append(
                        {
                            "prior_role": self._role_ref(prior),
                            "implies": [
                                self._role_ref(role_id) if role_id in self.roles else {"id": role_id}
                                for role_id in sorted(implied)
                            ],
                        }
                    )
            return 200, {"role_inferences": inferences, "links": {"self": None, "previous": None, "next": None}}, None
        if path.startswith("/v3/roles/"):
            role = self.roles.get(path.rsplit("/", 1)[-1])
            return (200, {"role": dict(role)}, None) if role else (404, {"error": {"code": 404}}, None)
        if path.startswith("/v3/users/"):
            user = self.users.get(path.rsplit("/", 1)[-1])
            if user is None:
                return 404, {"error": {"code": 404}}, None
            return (
                200,
                {"user": {"id": user["id"], "name": user["name"], "enabled": user["enabled"], "domain_id": "default"}},
                None,
            )
        if path.startswith("/v3/projects/"):
            project = self.projects.get(path.rsplit("/", 1)[-1])
            if project is None:
                return 404, {"error": {"code": 404}}, None
            return (
                200,
                {
                    "project": {
                        "id": project["id"],
                        "name": project["name"],
                        "enabled": project["enabled"],
                        "domain_id": "default",
                        "is_domain": False,
                    }
                },
                None,
            )
        if path == "/v3/role_assignments":
            return (
                200,
                {
                    "role_assignments": self.role_assignments(query),
                    "links": {"self": None, "previous": None, "next": None},
                },
                None,
            )
        return 404, {"error": {"code": 404, "message": "Unknown identity resource"}}, None

    def role_assignments(self, query: dict) -> list[dict]:
        user_id = (query.get("user.id") or [None])[0]
        system_only = bool(query.get("scope.system"))
        project_only = (query.get("scope.project.id") or [None])[0]
        effective = "effective" in query
        result, emitted = [], set()
        for user, kind, target, role in sorted(self.assignments):
            if user_id is not None and user != user_id:
                continue
            if system_only and kind != "system":
                continue
            if project_only is not None and (kind != "project" or target != project_only):
                continue
            for role_id in self.expand([role]) if effective else [role]:
                key = (user, kind, target, role_id)
                if key in emitted or role_id not in self.roles:
                    continue
                emitted.add(key)
                scope = (
                    {"system": {"all": True}}
                    if kind == "system"
                    else {
                        "project": {
                            "id": target,
                            "name": self.projects[target]["name"],
                            "domain": {"id": "default", "name": "Default"},
                        }
                    }
                )
                result.append(
                    {
                        "user": {
                            "id": user,
                            "name": self.users[user]["name"],
                            "domain": {"id": "default", "name": "Default"},
                        },
                        "scope": scope,
                        "role": self._role_ref(role_id),
                        "links": {"assignment": "synthetic"},
                    }
                )
        return result

    def control(self, request: dict) -> dict:
        operation = request.get("op")
        if operation == "ping":
            return {"ok": True}
        if operation == "mint":
            return {"token": self.mint(request["user"], request["project"])}
        if operation in ("assign", "unassign"):
            self.assign(request, operation == "assign")
            return {"ok": True}
        if operation == "edge":
            self.edge(request["prior"], self.ids[request["implied"]], request["present"])
            return {"ok": True}
        if operation == "dangling_edge":
            self.edge(request["prior"], "smoke-missing-role-id", request["present"])
            return {"ok": True}
        if operation == "duplicate_role":
            self.duplicate_role(request["name"], request["present"])
            return {"ok": True}
        if operation == "log":
            with self.lock:
                return {"requests": list(self.requests)}
        raise ValueError("unknown control operation")


def keystone_serve(_args) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    directory = SyntheticKeystone(json.loads(os.environ["SMOKE_KEYSTONE_SEED"]))
    control_secret = os.environ["SMOKE_KEYSTONE_CONTROL_SECRET"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # never log tokens or paths
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

        def read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

    class IdentityHandler(Handler):
        def do_POST(self):
            if urlsplit(self.path).path.rstrip("/") != "/v3/auth/tokens":
                self.send_json(404, {"error": {"code": 404}})
                return
            self.send_json(*directory.authenticate(self.read_json()))

        def do_GET(self):
            parsed = urlsplit(self.path)
            self.send_json(
                *directory.get(
                    parsed.path,
                    parse_qs(parsed.query, keep_blank_values=True),
                    self.headers.get("X-Auth-Token"),
                    self.headers.get("X-Subject-Token"),
                )
            )

    class ControlHandler(Handler):
        def do_POST(self):
            if urlsplit(self.path).path != "/control" or not secrets.compare_digest(
                self.headers.get("X-Smoke-Control", ""), control_secret
            ):
                self.send_json(403, {"error": "control secret required"})
                return
            try:
                self.send_json(200, directory.control(self.read_json()))
            except PermissionError as exc:
                self.send_json(409, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                self.send_json(400, {"error": type(exc).__name__})

    servers = [
        ThreadingHTTPServer(("0.0.0.0", IDENTITY_PORT), IdentityHandler),
        ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler),
    ]
    for server in servers:
        server.daemon_threads = True
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    print("synthetic keystone ready", flush=True)
    for thread in threads:
        thread.join()
    return 0


# ---------------------------------------------------------------------------
# In-container Hub helpers (derived smoke image / worker image)
# ---------------------------------------------------------------------------


def hub_serve(_args) -> int:
    """Serve unmodified main:app; replicate lifespan except the MySQL-only init_db."""
    import asyncio

    import uvicorn
    from palimpsest_hub import database
    from palimpsest_hub.api.hub import configure_blocking_operations
    from palimpsest_hub.cache import close_redis
    from palimpsest_hub.config import get_settings
    from palimpsest_hub.logging import configure_logging
    from palimpsest_hub.main import app
    from palimpsest_hub.models import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def main():
        configure_logging()
        settings = get_settings()
        if not settings.database_url.startswith("sqlite+aiosqlite:///"):
            raise SystemExit("hub-serve is the isolated SQLite harness only")
        configure_blocking_operations(settings.palimpsest_hub_max_blocking_operations)
        engine = create_async_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        database._engine = engine
        database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=HUB_PORT,
                lifespan="off",
                access_log=False,
                log_level="info",
            )
        )
        try:
            await server.serve()
        finally:
            await close_redis()
            await database.close_db()

    asyncio.run(main())
    return 0


def seed_export(args) -> int:
    """Seed one completed export row whose bytes are published through the real CAS store."""
    import asyncio

    from palimpsest_hub.config import get_settings
    from palimpsest_hub.models import Base, PalimpsestImageExport
    from palimpsest_hub.services.hub_store import get_blob_store
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def main():
        settings = get_settings()
        store = get_blob_store(settings)
        payload = synthetic_bytes(args.label, args.size)
        store.exports_dir.mkdir(parents=True, exist_ok=True)
        scratch = store.exports_dir / f"smoke-seed-{uuid.uuid4().hex}.raw"
        scratch.write_bytes(payload)
        finalized = store.promote_file(scratch, max_bytes=settings.palimpsest_hub_max_blob_bytes)
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            now = datetime.now(UTC)
            row = PalimpsestImageExport(
                id=str(uuid.uuid4()),
                project_id=args.project,
                created_by=args.user,
                source_image_id=str(uuid.uuid4()),
                source_name="smoke-export",
                source_disk_format="qcow2",
                source_size_bytes=len(payload),
                source_fingerprint=hashlib.sha256(f"{args.label}:source".encode()).hexdigest(),
                artifact_key=hashlib.sha256(f"{args.label}:artifact".encode()).hexdigest(),
                target_disk_format="raw",
                result_blob_digest=finalized.blob_digest,
                result_size_bytes=finalized.size_bytes,
                status="complete",
                progress_pct=100,
                attempts=1,
                started_at=now,
                completed_at=now,
            )
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add(row)
                await session.commit()
        finally:
            await engine.dispose()
        print(json.dumps({"export_id": row.id, "digest": finalized.blob_digest, "size_bytes": finalized.size_bytes}))

    asyncio.run(main())
    return 0


def sql_inspect(_args) -> int:
    """Reread persisted SQL rows and verify every CAS file against its digest name."""
    import asyncio

    from palimpsest_hub.config import get_settings
    from palimpsest_hub.models import (
        Base,
        PackageKey,
        PackageNamespace,
        PackageTag,
        PackageVersion,
        PalimpsestHubLayer,
        PalimpsestImageExport,
        RegistryPackage,
    )
    from palimpsest_hub.services.hub_store import get_blob_store
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine

    async def main():
        settings = get_settings()
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as connection:
                counts = {
                    table.name: int(await connection.scalar(select(func.count()).select_from(table)))
                    for table in Base.metadata.sorted_tables
                }
                namespaces = [
                    list(row)
                    for row in await connection.execute(
                        select(PackageNamespace.project_id, PackageNamespace.namespace).order_by(
                            PackageNamespace.namespace
                        )
                    )
                ]
                versions = [
                    list(row)
                    for row in await connection.execute(
                        select(
                            RegistryPackage.name,
                            PackageVersion.root_digest,
                            PackageVersion.archive_digest,
                            PackageVersion.archive_size_bytes,
                            PackageVersion.pushed_by,
                        )
                        .select_from(PackageVersion)
                        .join(RegistryPackage, RegistryPackage.id == PackageVersion.package_id)
                        .order_by(RegistryPackage.name, PackageVersion.root_digest)
                    )
                ]
                tags = [
                    list(row)
                    for row in await connection.execute(
                        select(RegistryPackage.name, PackageTag.tag, PackageTag.root_digest)
                        .select_from(PackageTag)
                        .join(RegistryPackage, RegistryPackage.id == PackageTag.package_id)
                        .order_by(RegistryPackage.name, PackageTag.tag)
                    )
                ]
                layers = [
                    list(row)
                    for row in await connection.execute(
                        select(
                            PalimpsestHubLayer.blob_digest,
                            PalimpsestHubLayer.name,
                            PalimpsestHubLayer.size_bytes,
                            PalimpsestHubLayer.project_id,
                        ).order_by(PalimpsestHubLayer.blob_digest)
                    )
                ]
                exports = [
                    list(row)
                    for row in await connection.execute(
                        select(
                            PalimpsestImageExport.id,
                            PalimpsestImageExport.status,
                            PalimpsestImageExport.result_blob_digest,
                        ).order_by(PalimpsestImageExport.id)
                    )
                ]
                keys = [
                    [row[0], row[1] is not None, row[2]]
                    for row in await connection.execute(
                        select(PackageKey.id, PackageKey.revoked_at, PackageKey.actions).order_by(PackageKey.id)
                    )
                ]
        finally:
            await engine.dispose()
        store = get_blob_store(settings)
        cas, mismatched = [], []
        if store.blobs_dir.is_dir():
            for entry in sorted(store.blobs_dir.iterdir()):
                cas.append("sha256:" + entry.name)
                digest = hashlib.sha256()
                with entry.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry.name:
                    mismatched.append(entry.name)
        print(
            json.dumps(
                {
                    "counts": counts,
                    "namespaces": namespaces,
                    "versions": versions,
                    "tags": tags,
                    "layers": layers,
                    "exports": exports,
                    "keys": keys,
                    "cas_digests": cas,
                    "cas_mismatched": mismatched,
                },
                sort_keys=True,
            )
        )

    asyncio.run(main())
    return 0


def worker_probe(_args) -> int:
    """Safe worker runtime command: entrypoint imports, platform and qemu-img capability only."""
    import asyncio
    import platform

    import palimpsest_hub.build_worker as build_worker
    import palimpsest_hub.worker as worker
    from palimpsest_hub.services.image_exports import SUPPORTED_FORMATS, validate_qemu_img_support

    asyncio.run(validate_qemu_img_support())
    version = subprocess.run(["qemu-img", "--version"], capture_output=True, text=True, check=True)
    print(
        json.dumps(
            {
                "machine": platform.machine(),
                "python": platform.python_version(),
                "worker_entrypoint": callable(worker.run),
                "build_worker_entrypoint": callable(build_worker.run),
                "qemu_img_formats_validated": list(SUPPORTED_FORMATS),
                "qemu_img_version": version.stdout.splitlines()[0] if version.stdout else "",
            }
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Host driver
# ---------------------------------------------------------------------------


class SmokeAbort(RuntimeError):
    pass


def ensure(condition, message: str, evidence=None):
    """Response validator helper: return evidence or fail the check with a non-secret message."""
    if not condition:
        raise AssertionError(message)
    return evidence


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body or b"null")

    def error_code(self) -> str | None:
        try:
            value = self.json()
        except ValueError:
            return None
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, dict):
                return error.get("code")
            detail = value.get("detail")
            if isinstance(detail, dict):
                return detail.get("code")
            if isinstance(detail, str):
                return detail[:120]
        return None


def http_call(port: int, method: str, path: str, *, headers=None, body: bytes | None = None, timeout=120) -> Response:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return Response(response.status, {key.lower(): value for key, value in response.getheaders()}, data)
    finally:
        connection.close()


class Docker:
    def __init__(self, binary: str, run_id: str):
        self.binary, self.run_id = binary, run_id
        self.containers: list[str] = []

    def labels(self) -> list[str]:
        return ["--label", f"{LABEL}={LABEL_VALUE}", "--label", f"{LABEL}.run={self.run_id}"]

    def __call__(self, *args, check=True, timeout=900, input_bytes=None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            timeout=timeout,
            input=input_bytes,
            check=False,
        )
        if check and result.returncode != 0:
            lines = result.stderr.decode(errors="replace").strip().splitlines()
            raise SmokeAbort(f"docker {args[0]} failed ({result.returncode}): {lines[-1] if lines else ''}"[:400])
        return result

    def text(self, *args, **kwargs) -> str:
        return self(*args, **kwargs).stdout.decode().strip()

    def track(self, name: str) -> str:
        self.containers.append(name)
        return name

    def inspect_image(self, reference: str) -> dict:
        data = json.loads(self.text("image", "inspect", reference))[0]
        return {
            "reference": reference,
            "id": data["Id"],
            "repo_digests": data.get("RepoDigests") or [],
            "os": data.get("Os"),
            "architecture": data.get("Architecture"),
            "variant": data.get("Variant"),
            "cmd": (data.get("Config") or {}).get("Cmd"),
            "user": (data.get("Config") or {}).get("User"),
        }

    def published_port(self, container: str, port: int) -> int:
        for line in self.text("port", container, f"{port}/tcp").splitlines():
            if line.startswith("127.0.0.1:"):
                return int(line.rsplit(":", 1)[1])
        raise SmokeAbort(f"no loopback port published for {container}:{port}")


class Secrets:
    """Every bearer-equivalent value the run handles; reports/logs are scrubbed and verified."""

    def __init__(self):
        self.values: set[str] = set()

    def add(self, value: str) -> str:
        if value:
            self.values.add(value)
        return value

    def scrub(self, text: str) -> tuple[str, int]:
        hits = 0
        for value in sorted(self.values, key=len, reverse=True):
            if value in text:
                hits += text.count(value)
                text = text.replace(value, "[REDACTED]")
        return text, hits


def locked_wheel(lock_path: Path, package: str) -> tuple[str, str, str]:
    text = lock_path.read_text()
    block = re.search(
        r'\[\[package\]\]\nname = "' + re.escape(package) + r'"\nversion = "([^"]+)"\n(.*?)(?=\n\[\[package\]\]|\Z)',
        text,
        re.S,
    )
    if block is None:
        raise SmokeAbort(f"{package} is not pinned in {lock_path}")
    wheel = re.search(r'\{ url = "([^"]+-py3-none-any\.whl)", hash = "sha256:([0-9a-f]{64})"', block[2])
    if wheel is None:
        raise SmokeAbort(f"{package} has no locked pure-Python wheel")
    return block[1], wheel[1], wheel[2]


class Smoke:
    def __init__(self, args):
        self.args = args
        self.repo = Path(__file__).resolve().parents[1]
        self.run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ").lower() + "-" + secrets.token_hex(3)
        self.prefix = f"palimpsest-scope-smoke-{self.run_id}"
        evidence = (
            Path(args.evidence_dir)
            if args.evidence_dir
            else (self.repo / "build" / "scoped-package-capabilities-smoke" / self.run_id)
        )
        self.evidence = evidence.resolve()
        if not self.evidence.is_relative_to(self.repo):
            raise SystemExit(f"--evidence-dir must be inside {self.repo}")
        if self.evidence.exists() and any(self.evidence.iterdir()):
            raise SystemExit("--evidence-dir must be new or empty")
        self.docker = Docker(args.docker, self.run_id)
        self.secrets = Secrets()
        self.checks: list[dict] = []
        self.observations: dict = {}
        self.names = {
            "network": f"{self.prefix}-net",
            "sql_volume": f"{self.prefix}-sql",
            "blob_volume": f"{self.prefix}-blobs",
            "keystone": f"{self.prefix}-keystone",
            "redis": f"{self.prefix}-redis",
            "api": f"{self.prefix}-api",
            "derived_image": f"palimpsest-scope-smoke-api:{self.run_id}",
        }
        self.hub_port = 0
        self.control_port = 0
        self.machine = PLATFORM_MACHINE[args.platform]
        self.architecture = args.platform.split("/", 1)[1]

    # -- recording ---------------------------------------------------------
    def record(self, name, *, ok, request=None, actor=None, expected=None, status=None, code=None, evidence=None):
        entry = {"id": len(self.checks) + 1, "name": name, "ok": bool(ok)}
        for key, value in (
            ("request", request),
            ("actor", actor),
            ("expected", expected),
            ("status", status),
            ("error_code", code),
            ("evidence", evidence),
        ):
            if value is not None:
                entry[key] = value
        self.checks.append(entry)
        print(
            ("PASS " if ok else "FAIL ") + f"{entry['id']:03d} {name}" + (f" -> {status}" if status else ""), flush=True
        )
        return ok

    def truth(self, name, condition, evidence=None, *, require=False):
        self.record(name, ok=condition, evidence=evidence)
        if require and not condition:
            raise SmokeAbort(name)

    def http(
        self,
        name,
        actor,
        method,
        path,
        *,
        expect,
        auth=None,
        headers=None,
        params=None,
        json_body=None,
        body=None,
        validate=None,
        require=False,
    ) -> Response:
        expected = {expect} if isinstance(expect, int) else set(expect)
        request_headers = dict(auth or {})
        request_headers.update(headers or {})
        payload = body
        if json_body is not None:
            payload = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
        target = path + ("?" + urlencode(params) if params else "")
        response = http_call(self.hub_port, method, target, headers=request_headers, body=payload)
        ok = response.status in expected
        evidence = None
        if ok and validate is not None:
            try:
                evidence = validate(response)
            except (AssertionError, KeyError, TypeError, ValueError) as exc:
                ok, evidence = False, {"assertion": str(exc)[:300] or type(exc).__name__}
        summary = method + " " + path.split("?", 1)[0]
        if params:
            summary += " " + json.dumps(params, sort_keys=True)
        if "Range" in request_headers:
            summary += " Range:" + request_headers["Range"]
        self.record(
            name,
            ok=ok,
            request=summary,
            actor=actor,
            expected=sorted(expected),
            status=response.status,
            code=None if response.status < 300 else response.error_code(),
            evidence=evidence,
        )
        if require and not ok:
            raise SmokeAbort(name)
        return response

    def bytes_equal(self, original: bytes):
        def check(response: Response):
            assert response.body == original, "downloaded bytes differ from original"
            return {"bytes": len(response.body), "sha256": hashlib.sha256(response.body).hexdigest()}

        return check

    def resume(self, name, actor, path, *, auth, original: bytes, params=None):
        """Two owned Range requests (first half, then resume from offset) reproduce the original."""
        half = len(original) // 2
        total = len(original)

        def part(expected: bytes, start: int, end: int):
            def check(response: Response):
                assert response.headers.get("content-range") == f"bytes {start}-{end}/{total}", "content-range"
                assert response.body == expected, "range bytes differ"
                return {"bytes": len(response.body)}

            return check

        first = self.http(
            name + ": first range",
            actor,
            "GET",
            path,
            expect=206,
            auth=auth,
            params=params,
            headers={"Range": f"bytes=0-{half - 1}"},
            validate=part(original[:half], 0, half - 1),
        )
        second = self.http(
            name + ": resumed range",
            actor,
            "GET",
            path,
            expect=206,
            auth=auth,
            params=params,
            headers={"Range": f"bytes={half}-"},
            validate=part(original[half:], half, total - 1),
        )
        self.truth(
            name + ": resumed bytes equal original",
            first.body + second.body == original,
            {"sha256": hashlib.sha256(first.body + second.body).hexdigest(), "bytes": total},
        )

    # -- identity control --------------------------------------------------
    def control(self, operation: str, **values) -> dict:
        response = http_call(
            self.control_port,
            "POST",
            "/control",
            headers={"X-Smoke-Control": self.control_secret, "Content-Type": "application/json"},
            body=json.dumps({"op": operation, **values}).encode(),
        )
        if response.status != 200:
            raise SmokeAbort(f"identity control {operation} returned {response.status}")
        return response.json()

    def mint(self, label: str, project: str | None = None) -> dict:
        user = self.users[label]
        token = self.secrets.add(self.control("mint", user=user["id"], project=project or user["project"])["token"])
        return {"X-Auth-Token": token}

    def assignment(self, present: bool, label: str, role: str, *, system=False):
        user = self.users[label]
        spec = {"user": user["id"], "role": role}
        spec.update({"system": True} if system else {"project": user["project"]})
        self.control("assign" if present else "unassign", **spec)

    # -- docker lifecycle --------------------------------------------------
    def prepare_driver_wheel(self, workdir: Path) -> Path:
        version, url, expected = locked_wheel(self.repo / "hub" / "uv.lock", "aiosqlite")
        name = url.rsplit("/", 1)[1]
        if self.args.aiosqlite_wheel:
            payload = Path(self.args.aiosqlite_wheel).read_bytes()
            source = "provided"
        else:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - pinned lock URL
                payload = response.read()
            source = "downloaded"
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise SmokeAbort("aiosqlite wheel hash differs from hub/uv.lock")
        target = workdir / name
        target.write_bytes(payload)
        self.observations["test_only_driver"] = {
            "package": "aiosqlite",
            "version": version,
            "wheel": name,
            "sha256": actual,
            "source": source,
            "lock": "hub/uv.lock",
            "installed_into": "/app/hub/.venv purelib of the derived smoke image only",
        }
        return target

    def build_derived_image(self, workdir: Path, wheel: Path, version: str) -> None:
        dockerfile = (
            f"FROM {self.args.api_image}\n"
            "USER root\n"
            f"COPY {wheel.name} /tmp/{wheel.name}\n"
            'RUN /app/hub/.venv/bin/python -c "import sys, sysconfig, zipfile; '
            "zipfile.ZipFile(sys.argv[1]).extractall(sysconfig.get_paths()['purelib'])\" "
            f"/tmp/{wheel.name} && rm -f /tmp/{wheel.name} "
            f"&& /app/hub/.venv/bin/python -c \"import aiosqlite; assert aiosqlite.__version__ == '{version}'\"\n"
            "USER palimpsest\n"
            f"LABEL {LABEL}={LABEL_VALUE} {LABEL}.run={self.run_id} {LABEL}.purpose=smoke-only-not-for-publication\n"
        )
        (workdir / "Dockerfile").write_text(dockerfile)
        self.docker(
            "build",
            "--platform",
            self.args.platform,
            "--pull=false",
            "-t",
            self.names["derived_image"],
            str(workdir),
            timeout=1800,
        )

    def env_file(self, workdir: Path, name: str, values: dict[str, str]) -> Path:
        path = workdir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            for key, value in values.items():
                if "\n" in value:
                    raise SmokeAbort("environment values must be single-line")
                handle.write(f"{key}={value}\n")
        return path

    def run_once(
        self,
        label: str,
        image: str,
        *command,
        env_file: Path | None = None,
        platform=True,
        user: str | None = None,
        check=True,
    ) -> subprocess.CompletedProcess:
        name = self.docker.track(f"{self.prefix}-{label}")
        args = ["run", "--rm", "--name", name, *self.docker.labels(), "--network", self.names["network"]]
        if platform:
            args += ["--platform", self.args.platform]
        if user:
            args += ["--user", user]
        if env_file:
            args += ["--env-file", str(env_file)]
        args += [
            "-v",
            f"{self.names['sql_volume']}:{SQL_MOUNT}",
            "-v",
            f"{self.names['blob_volume']}:{BLOB_MOUNT}",
            "-v",
            f"{Path(__file__).resolve()}:{SCRIPT_IN_CONTAINER}:ro",
            image,
            *command,
        ]
        return self.docker(*args, check=check)

    def start_api(self) -> None:
        name = self.names["api"]
        if name not in self.docker.containers:
            self.docker.track(name)
        self.docker(
            "run",
            "-d",
            "--name",
            name,
            *self.docker.labels(),
            "--platform",
            self.args.platform,
            "--network",
            self.names["network"],
            "--network-alias",
            "hub",
            "--env-file",
            str(self.hub_env),
            "-v",
            f"{self.names['sql_volume']}:{SQL_MOUNT}",
            "-v",
            f"{self.names['blob_volume']}:{BLOB_MOUNT}",
            "-v",
            f"{Path(__file__).resolve()}:{SCRIPT_IN_CONTAINER}:ro",
            "-p",
            f"127.0.0.1::{HUB_PORT}",
            self.names["derived_image"],
            "python",
            SCRIPT_IN_CONTAINER,
            "hub-serve",
        )
        self.wait_api()

    def wait_api(self) -> None:
        deadline = time.monotonic() + self.args.startup_timeout
        while time.monotonic() < deadline:
            state = self.docker.text("inspect", "-f", "{{.State.Running}}", self.names["api"], check=False)
            if state == "false":
                break
            try:
                self.hub_port = self.docker.published_port(self.names["api"], HUB_PORT)
                if http_call(self.hub_port, "GET", "/health", timeout=5).status == 200:
                    return
            except (OSError, SmokeAbort, http.client.HTTPException):
                pass
            time.sleep(1)
        raise SmokeAbort("Hub API did not become healthy")

    def save_logs(self, container: str, filename: str) -> None:
        result = self.docker("logs", container, check=False)
        text = (result.stdout + result.stderr).decode(errors="replace")
        text, hits = self.secrets.scrub(text)
        (self.evidence / filename).write_text(text)
        if hits:
            self.record(f"{filename} contained no handled secret values", ok=False, evidence={"redacted": hits})

    def setup(self, workdir: Path) -> None:
        d = self.docker
        images = self.images = {
            "api": d.inspect_image(self.args.api_image),
            "worker": d.inspect_image(self.args.worker_image),
        }
        for role, image in images.items():
            self.truth(
                f"{role} image platform is {self.args.platform}",
                (image["os"], image["architecture"]) == ("linux", self.architecture),
                {"os": image["os"], "architecture": image["architecture"]},
                require=True,
            )
        self.truth(
            "api image default command is canonical main:app",
            images["api"]["cmd"] == ["uvicorn", "palimpsest_hub.main:app", "--host", "0.0.0.0", "--port", "8020"],
            {"cmd": images["api"]["cmd"]},
        )
        self.truth(
            "worker image default command is the export worker",
            images["worker"]["cmd"] == ["python", "-m", "palimpsest_hub.worker"],
            {"cmd": images["worker"]["cmd"]},
        )
        context = workdir / "derived-image"
        context.mkdir()
        wheel = self.prepare_driver_wheel(context)
        self.build_derived_image(context, wheel, self.observations["test_only_driver"]["version"])
        images["derived_smoke_api"] = d.inspect_image(self.names["derived_image"])
        images["derived_smoke_api"]["publication"] = False
        self.truth(
            "derived smoke image platform matches",
            images["derived_smoke_api"]["architecture"] == self.architecture,
            {"architecture": images["derived_smoke_api"]["architecture"]},
            require=True,
        )
        if d("image", "inspect", self.args.redis_image, check=False).returncode != 0:
            d("pull", "--quiet", self.args.redis_image)
        images["redis"] = d.inspect_image(self.args.redis_image)

        d("network", "create", *d.labels(), self.names["network"])
        self.network_created = True
        for volume in ("sql_volume", "blob_volume"):
            d("volume", "create", *d.labels(), "--label", f"{LABEL}.retain=evidence", self.names[volume])
        self.run_once(
            "volume-init",
            self.args.api_image,
            "sh",
            "-c",
            f"chown palimpsest:palimpsest {SQL_MOUNT} {BLOB_MOUNT} && chmod 0700 {SQL_MOUNT} {BLOB_MOUNT}",
            user="0",
        )

        self.control_secret = self.secrets.add(secrets.token_urlsafe(32))
        validator_password = self.secrets.add(secrets.token_urlsafe(24))
        glance_password = self.secrets.add(secrets.token_urlsafe(24))
        project_a, project_b, service_project = (secrets.token_hex(16) for _ in range(3))
        self.project_a = project_a
        self.users = {
            # 64-hex federated-style owner and 32-hex local users.
            "owner": {"id": secrets.token_hex(32), "project": project_a},
            "inventory": {"id": secrets.token_hex(16), "project": project_a},
            "writer": {"id": secrets.token_hex(16), "project": project_a},
            "tenant_admin": {"id": secrets.token_hex(16), "project": project_a},
            "system_admin": {"id": secrets.token_hex(16), "project": project_a},
            "protected": {"id": secrets.token_hex(16), "project": project_a},
            "service_member": {"id": secrets.token_hex(16), "project": service_project},
            "foreign": {"id": secrets.token_hex(16), "project": project_b},
        }
        assignments = {
            "owner": ["member", "palimpsest_admin"],
            "inventory": ["member", "palimpsest_reader"],
            "writer": ["member", "palimpsest-publish_editor", "palimpsest-keys_editor"],
            "tenant_admin": ["member", "admin"],
            "system_admin": ["member"],
            "protected": ["member", "palimpsest_admin"],
            "service_member": ["member", "palimpsest_admin"],
            "foreign": ["member", "palimpsest_admin"],
        }
        validator = {"id": secrets.token_hex(16), "name": "palimpsest-smoke-validator", "password": validator_password}
        seed = {
            "validator": validator,
            "roles": [{"id": uuid.uuid4().hex, "name": name} for name in ROLE_NAMES],
            "edges": [list(edge) for edge in ROLE_EDGES],
            "users": [
                {"id": user["id"], "name": f"smoke-{label}", "label": label, "enabled": True}
                for label, user in self.users.items()
            ],
            "projects": [
                {"id": project, "name": name, "enabled": True}
                for project, name in (
                    (project_a, "smoke-project-a"),
                    (project_b, "smoke-project-b"),
                    (service_project, "service"),
                )
            ],
            "assignments": [
                *(
                    {"user": self.users[label]["id"], "project": self.users[label]["project"], "role": role}
                    for label, roles in assignments.items()
                    for role in roles
                ),
                {"user": self.users["system_admin"]["id"], "system": True, "role": "admin"},
                {"user": validator["id"], "system": True, "role": "reader"},
            ],
        }
        keystone_env = self.env_file(
            workdir,
            "keystone.env",
            {
                "SMOKE_KEYSTONE_SEED": json.dumps(seed, separators=(",", ":")),
                "SMOKE_KEYSTONE_CONTROL_SECRET": self.control_secret,
            },
        )
        d(
            "run",
            "-d",
            "--name",
            d.track(self.names["keystone"]),
            *d.labels(),
            "--platform",
            self.args.platform,
            "--network",
            self.names["network"],
            "--network-alias",
            "keystone",
            "--env-file",
            str(keystone_env),
            "-v",
            f"{Path(__file__).resolve()}:{SCRIPT_IN_CONTAINER}:ro",
            "-p",
            f"127.0.0.1::{CONTROL_PORT}",
            self.args.api_image,
            "python",
            SCRIPT_IN_CONTAINER,
            "keystone-serve",
        )
        d(
            "run",
            "-d",
            "--name",
            d.track(self.names["redis"]),
            *d.labels(),
            "--network",
            self.names["network"],
            "--network-alias",
            "redis",
            self.args.redis_image,
        )
        deadline = time.monotonic() + self.args.startup_timeout
        while True:
            try:
                self.control_port = d.published_port(self.names["keystone"], CONTROL_PORT)
                self.control("ping")
                if d.text("exec", self.names["redis"], "redis-cli", "ping", check=False) == "PONG":
                    break
            except (OSError, SmokeAbort, http.client.HTTPException):
                pass
            if time.monotonic() > deadline:
                raise SmokeAbort("synthetic Keystone or Redis did not become ready")
            time.sleep(1)

        hub_values = {
            "DATABASE_URL": DATABASE_URL,
            "REDIS_URL": "redis://redis:6379/0",
            "PALIMPSEST_HUB_LOCAL_PATH": BLOB_MOUNT,
            "OS_AUTH_URL": f"http://keystone:{IDENTITY_PORT}/v3",
            "OS_USERNAME": "palimpsest-glance-unused",
            "OS_PASSWORD": glance_password,
            "OS_PROJECT_NAME": "service",
            "OS_READER_USERNAME": validator["name"],
            "OS_READER_PASSWORD": validator_password,
            "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS": json.dumps([service_project]),
            "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS": json.dumps([self.users["protected"]["id"]]),
            "PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS": "{}",
            "PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN": PUBLIC_ORIGIN,
        }
        self.hub_env = self.env_file(workdir, "hub.env", hub_values)
        probe_env = self.env_file(
            workdir,
            "canonical-probe.env",
            {
                **hub_values,
                "DATABASE_URL": f"sqlite+aiosqlite:///{SQL_MOUNT}/canonical-bootstrap-probe.sqlite",
            },
        )
        probe = self.run_once(
            "canonical-bootstrap-probe",
            self.names["derived_image"],
            "palimpsest-hub-bootstrap",
            env_file=probe_env,
            check=False,
        )
        lines = [line for line in probe.stderr.decode(errors="replace").splitlines() if line.strip()]
        self.observations["canonical_sqlite_bootstrap"] = {
            "command": "palimpsest-hub-bootstrap (production init_db + create_schema) with sqlite+aiosqlite URL",
            "exit_code": probe.returncode,
            "final_error_line": self.secrets.scrub(lines[-1])[0][:300] if lines else "",
            "interpretation": (
                "canonical lifespan/bootstrap cannot open SQLite: init_db passes MySQL connect_timeout"
                if probe.returncode
                else "canonical bootstrap unexpectedly succeeded with SQLite"
            ),
        }
        self.start_api()
        machine = d.text("exec", self.names["api"], "python", "-c", "import platform; print(platform.machine())")
        self.truth("API container runtime machine matches platform", machine == self.machine, {"machine": machine})
        routes = d.text(
            "exec",
            self.names["api"],
            "python",
            "-c",
            (
                "import json; from palimpsest_hub.main import app; "
                "print(json.dumps(sorted({getattr(r, 'path', '') for r in app.routes})))"
            ),
        )
        route_list = json.loads(routes)
        self.truth(
            "served app exposes registered native and legacy routes",
            all(
                path in route_list
                for path in (
                    "/v1/projects/{namespace}/versions/{digest}/download",
                    "/v1/image-exports/{export_id}/download-token",
                    "/v1/layers/{digest}/blob",
                    "/v1/builds",
                    "/v1/auth/me",
                )
            ),
            {"route_count": len(route_list)},
        )

    # -- acceptance scenario -----------------------------------------------
    def scenario(self) -> None:  # noqa: C901 - one linear, readable acceptance narrative
        owner = self.mint("owner")
        inventory = self.mint("inventory")
        writer = self.mint("writer")
        tenant_admin = self.mint("tenant_admin")
        system_admin = self.mint("system_admin")
        protected = self.mint("protected")
        service_member = self.mint("service_member")
        foreign = self.mint("foreign")
        project = self.project_a
        archive, root, layer, blobs = oci_archive(synthetic_bytes(f"{self.run_id}:app", 96 * 1024), self.architecture)
        writer_archive, writer_root, writer_layer, _ = oci_archive(
            synthetic_bytes(f"{self.run_id}:writer", 48 * 1024), self.architecture
        )
        pending_archive, pending_root, _, _ = oci_archive(
            synthetic_bytes(f"{self.run_id}:pending", 16 * 1024), self.architecture
        )

        def body(payload, root_digest, tag):
            return {
                "package_type": "oci-image",
                "tag": tag,
                "root_digest": root_digest,
                "archive_digest": sha256_digest(payload),
                "archive_size_bytes": len(payload),
                "expected_tag_digest": None,
                "provenance": {"source_revision": f"smoke-{self.run_id}"},
            }

        def bearer(secret):
            return {"Authorization": "Bearer " + secret}

        def no_secret(response):
            text = response.body.decode(errors="replace")
            assert not any(value in text for value in self.secrets.values), "response echoed a handled secret"
            return None

        # Control plane and protected identities.
        self.http(
            "owner project context",
            "owner",
            "GET",
            "/v1/projects/current",
            expect=200,
            auth=owner,
            validate=lambda r: ensure(
                all(r.json()["capabilities"].values()), "owner capabilities incomplete", r.json()["capabilities"]
            ),
            require=True,
        )
        registered = self.http(
            "owner registers project namespace",
            "owner",
            "PUT",
            f"/v1/projects/{project}/namespace",
            expect=201,
            auth=owner,
            json_body={},
            require=True,
        )
        namespace = registered.json()["namespace"]
        base = f"/v1/projects/{namespace}"
        self.http(
            "namespace registration is idempotent",
            "owner",
            "PUT",
            f"/v1/projects/{project}/namespace",
            expect=200,
            auth=owner,
            json_body={},
        )
        self.http(
            "project header is an assertion, not a rescope",
            "owner",
            "GET",
            "/v1/projects/current",
            expect=403,
            auth={**owner, "X-Project-Id": self.users["foreign"]["project"]},
        )
        for label, token in (
            ("system_admin", system_admin),
            ("protected", protected),
            ("service_member", service_member),
            ("tenant_admin", tenant_admin),
        ):
            self.http(
                f"{label} cannot use package control", label, "GET", "/v1/projects/current", expect=403, auth=token
            )
            self.http(
                f"{label} cannot become package key owner",
                label,
                "POST",
                base + "/keys",
                expect=403,
                auth=token,
                json_body={"name": "denied", "scope": {"packages": ["smoke/app"]}, "actions": ["packages:inventory"]},
            )
        self.http(
            "foreign project cannot read namespace", "foreign", "GET", base + "/packages", expect=403, auth=foreign
        )

        # Key issuance: exact subset delegation.
        def issue(label, token, name, actions, packages, expect=201):
            response = self.http(
                f"{label} issues key '{name}' {actions}",
                label,
                "POST",
                base + "/keys",
                expect=expect,
                auth=token,
                require=expect == 201,
                json_body={"name": name, "scope": {"packages": packages}, "actions": actions, "expires_in_days": 1},
            )
            if response.status == 201:
                value = response.json()
                self.secrets.add(value["secret"])
                return value["secret"], value["key"]["key_id"]
            return None, None

        full_secret, full_id = issue("owner", owner, "full", ALL_ACTIONS, ["smoke/app"])
        if not full_secret:
            raise SmokeAbort("owner key issuance failed")
        full = bearer(full_secret)
        self.http(
            "unknown delegated action is rejected",
            "owner",
            "POST",
            base + "/keys",
            expect=422,
            auth=owner,
            json_body={"name": "bad", "scope": {"packages": ["smoke/app"]}, "actions": ["vm:launch"]},
        )
        sub_secret, _ = issue("owner", owner, "inventory-subset", ["packages:inventory"], ["smoke/app"])
        writer_secret, _ = issue(
            "writer", writer, "write-no-read", ["packages:inventory", "packages:write"], ["smoke/writer", "smoke/app"]
        )
        issue("writer", writer, "undelegated-read", ["packages:read"], ["smoke/writer"], expect=403)
        issue("inventory", inventory, "no-key-authority", ["packages:inventory"], ["smoke/app"], expect=403)
        sub, writer_key = bearer(sub_secret), bearer(writer_secret)
        self.http(
            "key metadata never returns secrets",
            "owner",
            "GET",
            base + "/keys",
            expect=200,
            auth=owner,
            validate=no_secret,
        )

        # Native resumable upload, owned session, finalize.
        app = {"package": "smoke/app"}
        started = self.http(
            "native upload start",
            "key:full",
            "POST",
            base + "/uploads",
            expect=201,
            auth=full,
            params=app,
            json_body=body(archive, root, "v1"),
            require=True,
        )
        upload = base + "/uploads/" + started.json()["upload_id"]
        split = len(archive) // 3
        octet = {"Content-Type": "application/octet-stream"}
        self.http(
            "native upload first chunk",
            "key:full",
            "PATCH",
            upload,
            expect=204,
            auth=full,
            params=app,
            headers={**octet, "Upload-Offset": "0"},
            body=archive[:split],
            require=True,
            validate=lambda r: {"offset": int(r.headers["upload-offset"])},
        )
        self.http(
            "upload session is owned by its key", "key:writer", "GET", upload, expect=404, auth=writer_key, params=app
        )
        self.http(
            "upload status reports resume offset",
            "key:full",
            "GET",
            upload,
            expect=200,
            auth=full,
            params=app,
            validate=lambda r: ensure(
                r.json()["received_bytes"] == split,
                "unexpected resume offset",
                {"received_bytes": r.json()["received_bytes"]},
            ),
        )
        self.http(
            "stale offset is rejected with server offset",
            "key:full",
            "PATCH",
            upload,
            expect=409,
            auth=full,
            params=app,
            headers={**octet, "Upload-Offset": "0"},
            body=archive[:split],
            validate=lambda r: ensure(
                r.headers.get("upload-offset") == str(split),
                "missing Upload-Offset",
                {"upload_offset": r.headers.get("upload-offset")},
            ),
        )
        self.http(
            "native upload resumed chunk",
            "key:full",
            "PATCH",
            upload,
            expect=204,
            auth=full,
            params=app,
            headers={**octet, "Upload-Offset": str(split)},
            body=archive[split:],
            require=True,
        )
        self.http(
            "native finalize publishes",
            "key:full",
            "PUT",
            upload,
            expect=201,
            auth=full,
            params=app,
            json_body={},
            require=True,
            validate=lambda r: ensure(r.json()["digest"] == root, "root digest", {"digest": r.json()["digest"]}),
        )
        self.http(
            "native finalize is idempotent", "key:full", "PUT", upload, expect=200, auth=full, params=app, json_body={}
        )

        version = f"{base}/versions/{root}"
        self.http(
            "version metadata names the exact graph",
            "key:full",
            "GET",
            version,
            expect=200,
            auth=full,
            params=app,
            validate=lambda r: ensure(
                set(r.json()["graph"]) == set(blobs) and r.json()["archive_digest"] == sha256_digest(archive),
                "graph mismatch",
                {"graph": sorted(r.json()["graph"])},
            ),
        )
        self.http(
            "key downloads original archive",
            "key:full",
            "GET",
            version + "/download",
            expect=200,
            auth=full,
            params=app,
            validate=self.bytes_equal(archive),
        )
        self.resume("key archive download", "key:full", version + "/download", auth=full, original=archive, params=app)
        self.http(
            "key downloads layer blob",
            "key:full",
            "GET",
            f"{version}/blobs/{layer}",
            expect=200,
            auth=full,
            params=app,
            validate=self.bytes_equal(blobs[layer]),
        )
        self.resume(
            "key layer blob", "key:full", f"{version}/blobs/{layer}", auth=full, original=blobs[layer], params=app
        )
        self.http(
            "owner token downloads original archive",
            "owner",
            "GET",
            version + "/download",
            expect=200,
            auth=owner,
            params=app,
            validate=self.bytes_equal(archive),
        )

        # Inventory denies downloads.
        self.http(
            "inventory role lists packages",
            "inventory",
            "GET",
            base + "/packages",
            expect=200,
            auth=inventory,
            validate=lambda r: {"names": [i["name"] for i in r.json()["items"]]},
        )
        self.http(
            "inventory role reads version metadata", "inventory", "GET", version, expect=200, auth=inventory, params=app
        )
        self.http(
            "inventory role cannot download archive",
            "inventory",
            "GET",
            version + "/download",
            expect=403,
            auth=inventory,
            params=app,
        )
        self.http(
            "inventory role cannot range-download archive",
            "inventory",
            "GET",
            version + "/download",
            expect=403,
            auth=inventory,
            params=app,
            headers={"Range": "bytes=0-9"},
        )
        self.http(
            "inventory role cannot download blob",
            "inventory",
            "GET",
            f"{version}/blobs/{layer}",
            expect=403,
            auth=inventory,
            params=app,
        )
        self.http(
            "inventory-subset key lists packages",
            "key:inventory-subset",
            "GET",
            base + "/packages",
            expect=200,
            auth=sub,
        )
        self.http(
            "inventory-subset key cannot download",
            "key:inventory-subset",
            "GET",
            version + "/download",
            expect=403,
            auth=sub,
            params=app,
        )
        self.http(
            "inventory-subset key cannot publish",
            "key:inventory-subset",
            "POST",
            base + "/uploads",
            expect=403,
            auth=sub,
            params=app,
            json_body=body(archive, root, "v9"),
        )

        # Inventory + write without read publishes.
        writer_pkg = {"package": "smoke/writer"}
        started = self.http(
            "write-no-read key starts upload",
            "key:writer",
            "POST",
            base + "/uploads",
            expect=201,
            auth=writer_key,
            params=writer_pkg,
            json_body=body(writer_archive, writer_root, "v1"),
            require=True,
        )
        writer_upload = base + "/uploads/" + started.json()["upload_id"]
        self.http(
            "write-no-read key appends",
            "key:writer",
            "PATCH",
            writer_upload,
            expect=204,
            auth=writer_key,
            params=writer_pkg,
            headers={**octet, "Upload-Offset": "0"},
            body=writer_archive,
        )
        self.http(
            "write-no-read key publishes",
            "key:writer",
            "PUT",
            writer_upload,
            expect=201,
            auth=writer_key,
            params=writer_pkg,
            json_body={},
        )
        writer_version = f"{base}/versions/{writer_root}"
        self.http(
            "write-no-read key lists inventory", "key:writer", "GET", base + "/packages", expect=200, auth=writer_key
        )
        self.http(
            "write-no-read key cannot download",
            "key:writer",
            "GET",
            writer_version + "/download",
            expect=403,
            auth=writer_key,
            params=writer_pkg,
        )
        self.http(
            "write-no-read key cannot download blob",
            "key:writer",
            "GET",
            f"{writer_version}/blobs/{writer_layer}",
            expect=403,
            auth=writer_key,
            params=writer_pkg,
        )
        self.http(
            "published write-no-read bytes are the original",
            "owner",
            "GET",
            writer_version + "/download",
            expect=200,
            auth=owner,
            params=writer_pkg,
            validate=self.bytes_equal(writer_archive),
        )
        self.http(
            "scoped key inventory excludes undelegated package",
            "key:full",
            "GET",
            base + "/packages",
            expect=200,
            auth=full,
            validate=lambda r: ensure(
                [i["name"] for i in r.json()["items"]] == ["smoke/app"],
                "scope leak",
                {"names": [i["name"] for i in r.json()["items"]]},
            ),
        )
        self.http(
            "scoped key cannot read undelegated package",
            "key:full",
            "GET",
            writer_version,
            expect=403,
            auth=full,
            params=writer_pkg,
        )

        # Own revocation.
        revoke_secret, revoke_id = issue("owner", owner, "revocable", ["packages:inventory"], ["smoke/app"])
        revocable = bearer(revoke_secret)
        self.http("revocable key authenticates", "key:revocable", "GET", "/v1/auth/me", expect=200, auth=revocable)
        self.http(
            "non-key-admin cannot revoke", "writer", "DELETE", f"{base}/keys/{revoke_id}", expect=403, auth=writer
        )
        self.http("owner revokes own key", "owner", "DELETE", f"{base}/keys/{revoke_id}", expect=204, auth=owner)
        self.http("revoked key is rejected", "key:revocable", "GET", "/v1/auth/me", expect=401, auth=revocable)

        # Legacy artifacts: resumable upload, inventory/download split.
        legacy = synthetic_bytes(f"{self.run_id}:legacy", 80 * 1024)
        legacy_digest = sha256_digest(legacy)

        def legacy_upload(label, payload, name, require=False):
            started = self.http(
                f"legacy upload start ({name})",
                label,
                "POST",
                "/v1/uploads",
                expect=200,
                auth=owner,
                json_body={"digest": sha256_digest(payload)},
                require=require,
            )
            session = "/v1/uploads/" + started.json()["session_id"]
            half = len(payload) // 2
            self.http(
                f"legacy first chunk ({name})",
                label,
                "PATCH",
                session,
                expect=200,
                auth=owner,
                headers={"Upload-Offset": "0"},
                body=payload[:half],
                require=require,
            )
            return session, half

        session, half = legacy_upload("owner", legacy, "smoke-legacy-layer", require=True)
        self.http("legacy session is owned by uploader", "writer", "GET", session, expect=404, auth=writer)
        self.http(
            "legacy status reports resume offset",
            "owner",
            "GET",
            session,
            expect=200,
            auth=owner,
            validate=lambda r: ensure(
                r.json()["received_bytes"] == half, "offset", {"received_bytes": r.json()["received_bytes"]}
            ),
        )
        self.http(
            "legacy resumed chunk",
            "owner",
            "PATCH",
            session,
            expect=200,
            auth=owner,
            headers={"Upload-Offset": str(half)},
            body=legacy[half:],
            require=True,
        )
        self.http(
            "legacy finalize registers layer",
            "owner",
            "PUT",
            session,
            expect=200,
            auth=owner,
            headers={"Upload-Offset": str(len(legacy))},
            json_body={"name": "smoke-legacy-layer", "kind": "squashfs"},
            require=True,
            validate=lambda r: ensure(
                r.json()["blob_digest"] == legacy_digest, "digest", {"blob_digest": r.json()["blob_digest"]}
            ),
        )
        layer_path = f"/v1/layers/{legacy_digest}"
        self.http(
            "legacy inventory lists owned layer",
            "owner",
            "GET",
            "/v1/layers",
            expect=200,
            auth=owner,
            params={"digest": legacy_digest},
            validate=lambda r: ensure(len(r.json()) == 1, "layer missing", {"count": len(r.json())}),
        )
        self.http(
            "legacy download returns original bytes",
            "owner",
            "GET",
            layer_path + "/blob",
            expect=200,
            auth=owner,
            validate=self.bytes_equal(legacy),
        )
        self.resume("legacy layer blob", "owner", layer_path + "/blob", auth=owner, original=legacy)
        self.http("inventory role sees legacy layer", "inventory", "GET", layer_path, expect=200, auth=inventory)
        self.http(
            "inventory role cannot download legacy blob",
            "inventory",
            "GET",
            layer_path + "/blob",
            expect=403,
            auth=inventory,
        )
        self.http(
            "write-no-read role cannot download legacy blob",
            "writer",
            "GET",
            layer_path + "/blob",
            expect=403,
            auth=writer,
        )
        self.http("foreign project cannot see legacy layer", "foreign", "GET", layer_path, expect=404, auth=foreign)
        self.http(
            "foreign project cannot download legacy blob",
            "foreign",
            "GET",
            layer_path + "/blob",
            expect=404,
            auth=foreign,
        )
        gc_payload = synthetic_bytes(f"{self.run_id}:gc", 8 * 1024)
        gc_session, gc_half = legacy_upload("owner", gc_payload, "smoke-gc-layer")
        self.http(
            "legacy GC proof layer resumed chunk",
            "owner",
            "PATCH",
            gc_session,
            expect=200,
            auth=owner,
            headers={"Upload-Offset": str(gc_half)},
            body=gc_payload[gc_half:],
        )
        self.http(
            "legacy GC proof layer registers",
            "owner",
            "PUT",
            gc_session,
            expect=200,
            auth=owner,
            headers={"Upload-Offset": str(len(gc_payload))},
            json_body={"name": "smoke-gc-layer", "kind": "squashfs"},
        )

        # Existing export resource (seeded SQL row + real CAS bytes) and Redis tickets.
        seeded = self.run_once(
            "seed-export",
            self.names["derived_image"],
            "python",
            SCRIPT_IN_CONTAINER,
            "seed-export",
            "--project",
            project,
            "--user",
            self.users["owner"]["id"],
            "--label",
            f"{self.run_id}:export",
            "--size",
            str(64 * 1024),
            env_file=self.hub_env,
        )
        export = json.loads(seeded.stdout.decode().strip().splitlines()[-1])
        export_bytes = synthetic_bytes(f"{self.run_id}:export", 64 * 1024)
        self.truth(
            "seeded export bytes are content addressed",
            export["digest"] == sha256_digest(export_bytes),
            {"export_id": export["export_id"], "digest": export["digest"]},
            require=True,
        )
        export_path = f"/v1/image-exports/{export['export_id']}"
        self.http(
            "owner lists existing export",
            "owner",
            "GET",
            "/v1/image-exports",
            expect=200,
            auth=owner,
            validate=lambda r: ensure(
                export["export_id"] in [item["id"] for item in r.json()], "export missing", {"count": len(r.json())}
            ),
        )
        self.http("owner reads existing export", "owner", "GET", export_path, expect=200, auth=owner)
        self.http(
            "owner downloads export blob",
            "owner",
            "GET",
            export_path + "/blob",
            expect=200,
            auth=owner,
            validate=self.bytes_equal(export_bytes),
        )
        self.resume("owner export blob", "owner", export_path + "/blob", auth=owner, original=export_bytes)

        def ticket(label, token, expect=200):
            response = self.http(
                f"{label} requests export download ticket",
                label,
                "POST",
                export_path + "/download-token",
                expect=expect,
                auth=token,
            )
            if response.status != 200:
                return None
            url = response.json()["url"]
            self.secrets.add(url.split("dl_token=", 1)[1])
            return url

        url = ticket("owner", owner)
        if url:
            self.http(
                "ticket redeems original export bytes",
                "ticket:owner",
                "GET",
                url,
                expect=200,
                validate=self.bytes_equal(export_bytes),
            )
            self.resume("ticket export download", "ticket:owner", url, auth={}, original=export_bytes)
        self.http("inventory role reads existing export", "inventory", "GET", export_path, expect=200, auth=inventory)
        ticket("inventory", inventory, expect=403)
        self.http(
            "inventory role cannot download export blob",
            "inventory",
            "GET",
            export_path + "/blob",
            expect=403,
            auth=inventory,
        )
        ticket("foreign", foreign, expect=404)
        self.http("foreign project cannot read export", "foreign", "GET", export_path, expect=404, auth=foreign)
        missing = "/v1/image-exports/11111111-1111-1111-1111-111111111111/download-token"
        self.http(
            "missing export ticket is 404 for download authority", "owner", "POST", missing, expect=404, auth=owner
        )
        self.http(
            "authorization precedes missing export lookup", "inventory", "POST", missing, expect=403, auth=inventory
        )

        # Global builder/GC: only verified system admin passes the auth boundary.
        build = {"name": "smoke-build", "recipe": "RUN true", "base_digest": legacy_digest}
        self.http("service-admin tenant cannot list builds", "owner", "GET", "/v1/builds", expect=403, auth=owner)
        self.http(
            "service-admin tenant cannot build", "owner", "POST", "/v1/builds", expect=403, auth=owner, json_body=build
        )
        self.http(
            "tenant Keystone admin cannot build",
            "tenant_admin",
            "POST",
            "/v1/builds",
            expect=403,
            auth=tenant_admin,
            json_body=build,
        )
        self.http(
            "package key is not a builder credential",
            "key:full",
            "POST",
            "/v1/builds",
            expect=401,
            auth=full,
            json_body=build,
        )
        self.http("system admin lists builds", "system_admin", "GET", "/v1/builds", expect=200, auth=system_admin)
        self.http(
            "system admin passes build auth; builder unconfigured",
            "system_admin",
            "POST",
            "/v1/builds",
            expect=503,
            auth=system_admin,
            json_body=build,
        )
        gc_path = f"/v1/layers/{sha256_digest(gc_payload)}"
        self.http("service-admin tenant cannot GC layer", "owner", "DELETE", gc_path, expect=403, auth=owner)
        self.http("inventory role cannot GC layer", "inventory", "DELETE", gc_path, expect=403, auth=inventory)
        self.http("package key cannot GC layer", "key:full", "DELETE", gc_path, expect=401, auth=full)
        self.http(
            "system admin GCs proof-owned layer", "system_admin", "DELETE", gc_path, expect=204, auth=system_admin
        )
        self.http("GC'd proof layer is gone", "owner", "GET", gc_path, expect=404, auth=owner)
        self.http("retained legacy layer survives GC", "owner", "GET", layer_path, expect=200, auth=owner)

        # A key owner who becomes a system admin is no longer a valid package owner.
        self.assignment(True, "owner", "admin", system=True)
        self.http(
            "owner granted system admin: key fails closed", "key:full", "GET", "/v1/auth/me", expect=403, auth=full
        )
        self.http(
            "owner granted system admin: token fails closed",
            "owner",
            "GET",
            "/v1/projects/current",
            expect=403,
            auth=owner,
        )
        self.assignment(False, "owner", "admin", system=True)
        self.http("system admin removed: key restored", "key:full", "GET", "/v1/auth/me", expect=200, auth=full)

        # Current assignment downgrade attenuates retained token, keys, uploads and tickets.
        started = self.http(
            "pending upload before downgrade",
            "key:full",
            "POST",
            base + "/uploads",
            expect=201,
            auth=full,
            params=app,
            json_body=body(pending_archive, pending_root, "v2"),
        )
        pending = base + "/uploads/" + started.json()["upload_id"] if started.status == 201 else None
        if pending:
            self.http(
                "pending upload receives all bytes",
                "key:full",
                "PATCH",
                pending,
                expect=204,
                auth=full,
                params=app,
                headers={**octet, "Upload-Offset": "0"},
                body=pending_archive,
            )
        pre_ticket = ticket("owner (pre-downgrade)", owner)
        self.assignment(False, "owner", "palimpsest_admin")
        self.assignment(True, "owner", "palimpsest_reader")
        if pre_ticket:
            self.http("downgrade: issued ticket no longer redeems", "ticket:owner", "GET", pre_ticket, expect=403)
        if pending:
            self.http(
                "downgrade: pending upload cannot finalize",
                "key:full",
                "PUT",
                pending,
                expect=403,
                auth=full,
                params=app,
                json_body={},
            )
        self.http(
            "downgrade: key cannot download",
            "key:full",
            "GET",
            version + "/download",
            expect=403,
            auth=full,
            params=app,
        )
        self.http(
            "downgrade: key cannot range-download blob",
            "key:full",
            "GET",
            f"{version}/blobs/{layer}",
            expect=403,
            auth=full,
            params=app,
            headers={"Range": "bytes=0-9"},
        )
        self.http("downgrade: key keeps inventory", "key:full", "GET", base + "/packages", expect=200, auth=full)
        self.http(
            "downgrade: retained token cannot issue keys",
            "owner",
            "POST",
            base + "/keys",
            expect=403,
            auth=owner,
            json_body={"name": "late", "scope": {"packages": ["smoke/app"]}, "actions": ["packages:inventory"]},
        )
        self.http(
            "downgrade: retained token cannot revoke keys",
            "owner",
            "DELETE",
            f"{base}/keys/{full_id}",
            expect=403,
            auth=owner,
        )
        self.http(
            "downgrade: retained token cannot download legacy blob",
            "owner",
            "GET",
            layer_path + "/blob",
            expect=403,
            auth=owner,
        )
        self.http(
            "downgrade: retained token keeps legacy inventory",
            "owner",
            "GET",
            "/v1/layers",
            expect=200,
            auth=owner,
            params={"digest": legacy_digest},
        )
        ticket("owner (downgraded)", owner, expect=403)
        self.http(
            "downgrade: retained token cannot legacy-write",
            "owner",
            "POST",
            "/v1/uploads",
            expect=403,
            auth=owner,
            json_body={},
        )
        self.assignment(False, "owner", "palimpsest_reader")
        self.assignment(True, "owner", "palimpsest_admin")
        self.http(
            "restored assignment: key downloads again",
            "key:full",
            "GET",
            version + "/download",
            expect=200,
            auth=full,
            params=app,
            validate=self.bytes_equal(archive),
        )
        post_ticket = ticket("owner (restored)", owner)
        if post_ticket:
            self.http(
                "restored assignment: new ticket redeems",
                "ticket:owner",
                "GET",
                post_ticket,
                expect=200,
                validate=self.bytes_equal(export_bytes),
            )
        if pending:
            self.http(
                "restored assignment: pending upload aborted",
                "key:full",
                "DELETE",
                pending,
                expect=204,
                auth=full,
                params=app,
            )

        # Removing a Keystone implication edge revokes the retained parent token/key.
        self.control("edge", prior="palimpsest_editor", implied="palimpsest-publish_editor", present=False)
        self.http(
            "edge removed: key cannot start upload",
            "key:full",
            "POST",
            base + "/uploads",
            expect=403,
            auth=full,
            params=app,
            json_body=body(pending_archive, pending_root, "v3"),
        )
        self.http(
            "edge removed: retained token cannot legacy-write",
            "owner",
            "POST",
            "/v1/uploads",
            expect=403,
            auth=owner,
            json_body={},
        )
        self.http(
            "edge removed: retained token cannot delegate write",
            "owner",
            "POST",
            base + "/keys",
            expect=403,
            auth=owner,
            json_body={"name": "w", "scope": {"packages": ["smoke/app"]}, "actions": ["packages:write"]},
        )
        self.http(
            "edge removed: retained token cannot register namespace",
            "owner",
            "PUT",
            f"/v1/projects/{project}/namespace",
            expect=403,
            auth=owner,
            json_body={},
        )
        self.http(
            "edge removed: context reports no write",
            "owner",
            "GET",
            "/v1/projects/current",
            expect=200,
            auth=owner,
            validate=lambda r: ensure(
                not r.json()["capabilities"]["packages_write"] and r.json()["capabilities"]["packages_download"],
                "write still granted",
                r.json()["capabilities"],
            ),
        )
        self.http(
            "edge removed: unrelated download remains",
            "key:full",
            "GET",
            version + "/download",
            expect=200,
            auth=full,
            params=app,
            validate=self.bytes_equal(archive),
        )
        self.control("edge", prior="palimpsest_editor", implied="palimpsest-publish_editor", present=True)
        started = self.http(
            "edge restored: key can start upload",
            "key:full",
            "POST",
            base + "/uploads",
            expect=201,
            auth=full,
            params=app,
            json_body=body(pending_archive, pending_root, "v3"),
        )
        if started.status == 201:
            self.http(
                "edge restored: probe upload aborted",
                "key:full",
                "DELETE",
                base + "/uploads/" + started.json()["upload_id"],
                expect=204,
                auth=full,
                params=app,
            )

        # Current role directory integrity: ambiguous IDs and dangling metadata fail closed (503).
        self.control("duplicate_role", name="palimpsest_admin", present=True)
        self.http(
            "ambiguous global role name fails closed (key)", "key:full", "GET", "/v1/auth/me", expect=503, auth=full
        )
        self.http(
            "ambiguous global role name fails closed (token)",
            "owner",
            "GET",
            "/v1/projects/current",
            expect=503,
            auth=owner,
        )
        self.control("duplicate_role", name="palimpsest_admin", present=False)
        self.control("dangling_edge", prior="palimpsest_reader", present=True)
        self.http(
            "dangling inference role metadata 404 fails closed 503",
            "key:full",
            "GET",
            "/v1/auth/me",
            expect=503,
            auth=full,
        )
        self.control("dangling_edge", prior="palimpsest_reader", present=False)
        self.http("directory repaired: key authenticates", "key:full", "GET", "/v1/auth/me", expect=200, auth=full)

        self.persistence(
            base=base,
            version=version,
            app=app,
            layer=layer,
            archive=archive,
            blobs=blobs,
            legacy=legacy,
            layer_path=layer_path,
            export_path=export_path,
            export_bytes=export_bytes,
            full=full,
            owner=owner,
            writer_key=writer_key,
            writer_version=writer_version,
            writer_pkg=writer_pkg,
            revocable=revocable,
            ticket=ticket,
        )

    def sql_snapshot(self, label: str) -> dict:
        result = self.run_once(
            f"sql-inspect-{label}",
            self.names["derived_image"],
            "python",
            SCRIPT_IN_CONTAINER,
            "sql-inspect",
            env_file=self.hub_env,
        )
        return json.loads(result.stdout.decode().strip().splitlines()[-1])

    def reread(
        self,
        phase: str,
        *,
        version,
        app,
        layer,
        archive,
        blobs,
        legacy,
        layer_path,
        export_path,
        export_bytes,
        full,
        owner,
        writer_key,
        writer_version,
        writer_pkg,
        revocable,
        ticket,
    ):
        self.http(
            f"{phase}: key rereads original archive",
            "key:full",
            "GET",
            version + "/download",
            expect=200,
            auth=full,
            params=app,
            validate=self.bytes_equal(archive),
        )
        self.resume(f"{phase}: key archive", "key:full", version + "/download", auth=full, original=archive, params=app)
        self.http(
            f"{phase}: key rereads layer blob",
            "key:full",
            "GET",
            f"{version}/blobs/{layer}",
            expect=200,
            auth=full,
            params=app,
            validate=self.bytes_equal(blobs[layer]),
        )
        self.http(
            f"{phase}: legacy layer bytes",
            "owner",
            "GET",
            layer_path + "/blob",
            expect=200,
            auth=owner,
            validate=self.bytes_equal(legacy),
        )
        self.http(
            f"{phase}: export blob bytes",
            "owner",
            "GET",
            export_path + "/blob",
            expect=200,
            auth=owner,
            validate=self.bytes_equal(export_bytes),
        )
        url = ticket(f"owner ({phase})", owner)
        if url:
            self.http(
                f"{phase}: Redis ticket redeems export",
                "ticket:owner",
                "GET",
                url,
                expect=200,
                validate=self.bytes_equal(export_bytes),
            )
        self.http(
            f"{phase}: write-no-read key still cannot download",
            "key:writer",
            "GET",
            writer_version + "/download",
            expect=403,
            auth=writer_key,
            params=writer_pkg,
        )
        self.http(
            f"{phase}: revoked key stays revoked", "key:revocable", "GET", "/v1/auth/me", expect=401, auth=revocable
        )

    def persistence(self, **context) -> None:
        context.pop("base")
        before = self.sql_snapshot("before-restart")
        self.truth(
            "CAS files verify against digest names",
            not before["cas_mismatched"],
            {"cas_files": len(before["cas_digests"])},
        )
        self.save_logs(self.names["api"], "api-initial.log")
        self.docker("restart", self.names["api"], timeout=300)
        self.wait_api()
        self.reread("after container restart", **context)
        after_restart = self.sql_snapshot("after-restart")
        comparable = ("namespaces", "versions", "tags", "layers", "exports", "keys", "cas_digests")
        self.truth(
            "SQL and CAS reread identically after restart",
            all(before[key] == after_restart[key] for key in comparable),
            {"counts": after_restart["counts"]},
        )
        self.save_logs(self.names["api"], "api-restarted.log")
        self.docker("rm", "-f", self.names["api"], timeout=300)
        self.start_api()
        self.reread("after container recreation", **context)
        after_recreate = self.sql_snapshot("after-recreate")
        self.truth(
            "SQL and CAS reread identically after recreation",
            all(before[key] == after_recreate[key] for key in comparable),
            {"counts": after_recreate["counts"]},
        )
        self.observations["persistence"] = {
            "sql_counts": before["counts"],
            "cas_files": len(before["cas_digests"]),
            "phases": ["docker restart (same container)", "docker rm + run (new container, same volumes)"],
        }

    def identity_audit(self) -> None:
        log = self.control("log")["requests"]
        posts = [entry for entry in log if entry["method"] == "POST"]
        validations = [entry for entry in log if entry["path"] == "/v3/auth/tokens" and entry["method"] == "GET"]
        user_actor = [
            entry for entry in log if entry["method"] == "GET" and entry["actor"] not in ("validator", "none")
        ]
        self.truth(
            "Hub authenticated only the read-only validator by password",
            bool(posts)
            and all(entry["methods"] == ["password"] and entry["principal"] == "validator" for entry in posts),
            {"posts": len(posts)},
        )
        self.truth(
            "Hub never exchanged or rescoped an original subject token",
            not any(entry["methods"] != ["password"] for entry in posts),
            {"token_method_posts": 0},
        )
        self.truth(
            "every subject validation used the validator as actor",
            bool(validations) and all(entry["actor"] == "validator" for entry in validations),
            {"validations": len(validations)},
        )
        self.truth(
            "no identity call used a caller token as actor",
            not user_actor,
            {"non_validator_actor_calls": len(user_actor)},
        )
        paths = sorted({entry["path"] for entry in log})
        self.observations["identity_calls"] = {
            "total": len(log),
            "paths": paths,
            "subjects_validated": sorted({entry.get("subject", "") for entry in validations}),
        }

    def worker(self) -> None:
        name = self.docker.track(f"{self.prefix}-worker-probe")
        result = self.docker(
            "run",
            "--rm",
            "--name",
            name,
            *self.docker.labels(),
            "--platform",
            self.args.platform,
            "--network",
            "none",
            "-v",
            f"{Path(__file__).resolve()}:{SCRIPT_IN_CONTAINER}:ro",
            self.args.worker_image,
            "python",
            SCRIPT_IN_CONTAINER,
            "worker-probe",
            check=False,
        )
        if result.returncode:
            self.record("worker runtime probe", ok=False, evidence={"exit_code": result.returncode})
            return
        probe = json.loads(result.stdout.decode().strip().splitlines()[-1])
        self.truth(
            "worker image executes worker/build-worker imports and qemu-img validation",
            probe["worker_entrypoint"] and probe["build_worker_entrypoint"],
            probe,
        )
        self.truth(
            "worker runtime machine matches platform", probe["machine"] == self.machine, {"machine": probe["machine"]}
        )

    # -- orchestration -----------------------------------------------------
    def cleanup(self) -> None:
        for container in reversed(self.docker.containers):
            self.docker("rm", "-f", container, check=False, timeout=300)
        if getattr(self, "network_created", False):
            self.docker("network", "rm", self.names["network"], check=False)
        if not self.args.keep_derived_image:
            self.docker("image", "rm", self.names["derived_image"], check=False)

    def write_report(self, failure: str | None) -> Path:
        failed = [check["name"] for check in self.checks if not check["ok"]]
        report = {
            "schema": "palimpsest-scoped-package-capabilities-smoke-v1",
            "run_id": self.run_id,
            "platform": self.args.platform,
            "verdict": "passed" if not failure and not failed and self.checks else "failed",
            "aborted_at": failure,
            "failed_checks": failed,
            "check_count": len(self.checks),
            "images": getattr(self, "images", {}),
            "retained_volumes": {
                "sql": {"name": self.names["sql_volume"], "mount": SQL_MOUNT, "database": DATABASE_URL},
                "blobs": {"name": self.names["blob_volume"], "mount": BLOB_MOUNT},
            },
            "removed_resources": {
                "containers": self.docker.containers,
                "network": self.names["network"],
                "derived_image": None if self.args.keep_derived_image else self.names["derived_image"],
            },
            "runtime": {
                "app": "palimpsest_hub.main:app (unmodified registered routes/middleware/handlers)",
                "server": "uvicorn, lifespan=off, plain HTTP on loopback-published port",
                "harness": "hub-serve: configure_logging + configure_blocking_operations + SQLite engine/"
                "session factory installed in palimpsest_hub.database; no dependency or auth overrides",
                "identity": "synthetic Keystone HTTP directory: unique global role IDs, live implication edges and "
                "assignments; Hub uses its real keystoneauth/keystoneclient validator",
                "redis": self.args.redis_image,
            },
            "limitations": [
                "Isolated acceptance only: not proof of canonical MySQL/MariaDB, production lifespan, Redis "
                "credentials or deployment.",
                "Canonical lifespan/bootstrap cannot use SQLite (see observations.canonical_sqlite_bootstrap); "
                "production database code was not changed.",
                "MariaDB/MySQL persistence was not exercised by this script.",
                "No real KVM, Glance, Nova or OpenStack cloud was contacted; the export row is seeded with real CAS "
                "bytes and no conversion ran; the builder is unconfigured (503 after admin auth).",
                "Synthetic Keystone is not a Keystone implementation; it mirrors only the endpoints the Hub calls.",
            ],
            "real_cloud": False,
            "kvm": False,
            "observations": self.observations,
            "checks": self.checks,
        }
        text = json.dumps(report, indent=2, sort_keys=False, default=str)
        text, hits = self.secrets.scrub(text)
        if hits:
            report["verdict"] = "failed"
            report["failed_checks"].append("report contained handled secret values (redacted)")
            text = self.secrets.scrub(json.dumps(report, indent=2, default=str))[0]
        path = self.evidence / "report.json"
        path.write_text(text + "\n")
        return path

    def execute(self) -> int:
        self.evidence.mkdir(parents=True, exist_ok=True)
        failure = None
        workdir = Path(tempfile.mkdtemp(prefix="palimpsest-scope-smoke-"))
        try:
            self.setup(workdir)
            self.scenario()
            self.identity_audit()
            self.worker()
        except SmokeAbort as exc:
            failure = str(exc)
            print(f"ABORT {failure}", flush=True)
        except Exception as exc:  # report and clean up on unexpected harness errors
            failure = f"{type(exc).__name__}: {self.secrets.scrub(str(exc))[0][:300]}"
            print(f"ERROR {failure}", flush=True)
        finally:
            try:
                for name, filename in ((self.names["api"], "api-final.log"), (self.names["keystone"], "keystone.log")):
                    if name in self.docker.containers:
                        self.save_logs(name, filename)
            finally:
                self.cleanup()
                shutil.rmtree(workdir, ignore_errors=True)
        report = self.write_report(failure)
        failed = sum(not check["ok"] for check in self.checks)
        print(f"checks={len(self.checks)} failed={failed} aborted={bool(failure)} report={report}")
        print(f"retained volumes: {self.names['sql_volume']} {self.names['blob_volume']}")
        return 0 if not failure and not failed else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="host driver: Docker-backed acceptance run")
    run.add_argument("--api-image", required=True, help="locally built Hub API image (single platform)")
    run.add_argument("--worker-image", required=True, help="locally built Hub export-worker image")
    run.add_argument("--platform", required=True, choices=sorted(PLATFORM_MACHINE))
    run.add_argument(
        "--evidence-dir", help="inside the repository; default build/scoped-package-capabilities-smoke/RUN"
    )
    run.add_argument("--redis-image", default="docker.io/library/redis:7.4-alpine")
    run.add_argument("--aiosqlite-wheel", help="pre-downloaded wheel; must match the hub/uv.lock hash")
    run.add_argument("--docker", default="docker")
    run.add_argument("--startup-timeout", type=int, default=180)
    run.add_argument("--keep-derived-image", action="store_true")
    for name in ("keystone-serve", "hub-serve", "sql-inspect", "worker-probe"):
        commands.add_parser(name, help="in-container helper")
    seed = commands.add_parser("seed-export", help="in-container helper")
    seed.add_argument("--project", required=True)
    seed.add_argument("--user", required=True)
    seed.add_argument("--label", required=True)
    seed.add_argument("--size", type=int, required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.command == "run":
        return Smoke(args).execute()
    return {
        "keystone-serve": keystone_serve,
        "hub-serve": hub_serve,
        "seed-export": seed_export,
        "sql-inspect": sql_inspect,
        "worker-probe": worker_probe,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
