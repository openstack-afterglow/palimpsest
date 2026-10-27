#!/usr/bin/env python3
"""Isolated loopback system-admin acceptance; no Docker, cloud, Redis or pytest.

Run only inside the coordinator's network/filesystem/resource sandbox, with an
empty safe environment and installed Hub dependencies. --scratch is an owned
writable directory. The real main:app, HTTP transport, Keystone SDK, SQLite and
CAS run locally; only Keystone is synthetic. No build worker/VM is executed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from smoke_package_capabilities import (
    ROLE_EDGES,
    ROLE_NAMES,
    SyntheticKeystone,
    oci_archive,
)


async def smoke(scratch: Path) -> dict:
    import httpx
    import uvicorn
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    root = Path(tempfile.mkdtemp(prefix="palimpsest-system-admin-", dir=scratch))
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    caller, target, protected = "a" * 32, "b" * 32, "e" * 32
    user, validator_id = "f" * 64, "c" * 32
    validator = {
        "id": validator_id,
        "name": "synthetic-reader",
        "password": "synthetic-reader-only",
        "domain_id": "default",
    }
    alias, group_id = "smoke-system-operator", "smoke-native-admin-group"
    directory = SyntheticKeystone(
        {
            "validator": validator,
            "roles": [{"id": "role-" + name, "name": name} for name in [*ROLE_NAMES, alias]],
            "edges": [*ROLE_EDGES, (alias, "admin")],
            "domains": [{"id": "default", "name": "Default", "enabled": True}],
            "users": [
                {"id": user, "name": "human-operator", "label": "operator", "enabled": True, "domain_id": "default"},
                {"id": "9" * 32, "name": "unused-trustee", "enabled": True, "domain_id": "default"},
            ],
            "groups": [{"id": group_id, "name": "native administration"}],
            "projects": [
                {"id": value, "name": name, "enabled": True, "domain_id": "default"}
                for value, name in (
                    (caller, "caller-home"),
                    (target, "selected-target"),
                    (protected, "protected-service"),
                )
            ],
            "assignments": [
                {"user": user, "system": True, "role": "admin"},
                {"user": validator_id, "system": True, "role": "reader"},
            ],
        }
    )
    # Deliberately no project roles in the original subject or target membership.
    # Subject validation is the boundary under test, not Keystone token issuance.
    token = directory._issue(user, project_id=caller, role_ids=[])
    system_token = directory._issue(user, project_id=None, role_ids=[])

    class IdentityHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body, subject=None):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if subject:
                self.send_header("X-Subject-Token", subject)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if urlsplit(self.path).path != "/v3/auth/tokens":
                self.respond(404, {"error": {"code": 404}})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.respond(*directory.authenticate(body))

        def do_GET(self):
            parsed = urlsplit(self.path)
            self.respond(
                *directory.get(
                    parsed.path,
                    parse_qs(parsed.query, keep_blank_values=True),
                    self.headers.get("X-Auth-Token"),
                    self.headers.get("X-Subject-Token"),
                )
            )

    identity = ThreadingHTTPServer(("127.0.0.1", 0), IdentityHandler)
    identity.daemon_threads = True
    identity_thread = threading.Thread(target=identity.serve_forever, daemon=True)
    identity_thread.start()
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'hub.sqlite'}",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "PALIMPSEST_HUB_LOCAL_PATH": str(root / "cas"),
            "OS_AUTH_URL": f"http://127.0.0.1:{identity.server_port}/v3",
            "OS_USERNAME": "unused-trustee",
            "OS_PASSWORD": "unused-synthetic",
            "OS_PROJECT_NAME": "unused-service",
            "OS_USER_DOMAIN_NAME": "Default",
            "OS_PROJECT_DOMAIN_NAME": "Default",
            "OS_READER_USERNAME": validator["name"],
            "OS_READER_PASSWORD": validator["password"],
            "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_PROJECT_IDS": json.dumps([protected]),
            "PALIMPSEST_HUB_PACKAGE_FORBIDDEN_USER_IDS": json.dumps(["d" * 32]),
            "PALIMPSEST_HUB_PACKAGE_NAMESPACE_BINDINGS": "{}",
            "PALIMPSEST_HUB_PACKAGE_PUBLIC_ORIGIN": "https://packages.synthetic.invalid",
            "PALIMPSEST_HUB_BUILD_WORKER_KEY": "synthetic-worker-admission-only-1234567890",
        }
    )
    from palimpsest_hub import database
    from palimpsest_hub.api.hub import configure_blocking_operations
    from palimpsest_hub.config import get_settings
    from palimpsest_hub.main import app
    from palimpsest_hub.models import (
        Base,
        PackageKey,
        PackageTag,
        PackageUpload,
        PackageVersion,
        PalimpsestHubBuild,
        PalimpsestHubLayer,
    )
    from palimpsest_hub.services.hub_store import get_blob_store

    get_settings.cache_clear()
    settings = get_settings()
    configure_blocking_operations(settings.palimpsest_hub_max_blocking_operations)
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    database._engine = engine
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    store = get_blob_store(settings)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    observations = []
    try:
        for _ in range(1000):
            if server.started:
                break
            if serving.done():
                await serving
                raise AssertionError("Hub stopped before readiness")
            await asyncio.sleep(0.01)
        assert server.started, "Hub readiness deadline exceeded"
        headers = {"X-Auth-Token": token, "X-Project-Id": caller, "X-Target-Project-Id": target}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=20) as client:

            async def call(method, path, status, **kwargs):
                supplied_headers = kwargs.pop("headers", headers)
                result = await client.request(method, path, headers=supplied_headers, **kwargs)
                observations.append({"method": method, "status": result.status_code, "expected": status})
                assert result.status_code == status, f"{method} expected {status}, received {result.status_code}"
                return result

            context = await call("PUT", f"/v1/projects/{target}/namespace", 201, json={})
            assert context.json()["project_id"] == target
            namespace = context.json()["namespace"]
            base = f"/v1/projects/{namespace}"
            system_headers = {"X-Auth-Token": system_token, "X-Project-Id": "", "X-Target-Project-Id": target}
            original_system = await call("GET", "/v1/me", 200, headers=system_headers)
            assert original_system.json()["project_id"] == "" and original_system.json()["target_project_id"] == target
            await call("PUT", f"/v1/projects/{target}/namespace", 200, headers=system_headers, json={})
            archive, root_digest, _, _ = oci_archive(b"synthetic admin OCI contents", "amd64")
            body = {
                "package_type": "oci-image",
                "tag": "proof",
                "root_digest": root_digest,
                "archive_digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                "archive_size_bytes": len(archive),
                "expected_tag_digest": None,
            }
            query = {"package": "proof/app"}
            started = await call("POST", base + "/uploads", 201, params=query, json=body)
            upload = base + "/uploads/" + started.json()["upload_id"]
            await call(
                "PATCH",
                upload,
                204,
                params=query,
                content=archive,
                headers={**headers, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
            )
            published = await call("PUT", upload, 201, params=query, json={})
            assert published.json()["pushed_by"] == user and published.json()["pushed_key_id"] is None
            version = base + "/versions/" + root_digest
            await call("GET", base + "/packages", 200)
            downloaded = await call("GET", version + "/download", 200, params=query)
            assert downloaded.content == archive
            key_request = {
                "name": "bounded human-issued proof",
                "scope": {"packages": ["proof/app"]},
                "actions": ["packages:read"],
            }
            issued = await call("POST", base + "/keys", 201, headers=system_headers, json=key_request)
            key_id = issued.json()["key"]["key_id"]
            key_headers = {"Authorization": "Bearer " + issued.json()["secret"]}
            effective = await call("GET", "/v1/auth/me", 200, headers=key_headers)
            assert effective.json()["actions"] == ["packages:inventory", "packages:read"]
            key_download = await call("GET", version + "/download", 200, headers=key_headers, params=query)
            assert key_download.content == archive
            await call("GET", base + "/packages", 403, headers={**key_headers, "X-Target-Project-Id": target})
            await call("POST", base + "/uploads", 403, headers=key_headers, params=query, json=body)
            await call("DELETE", base + "/package", 403, headers=key_headers, params=query)
            await call("POST", base + "/keys", 401, headers=key_headers, json=key_request)
            await call("GET", "/v1/builds", 401, headers=key_headers)
            await call("GET", base + "/package", 403, headers=key_headers, params={"package": "proof/foreign"})
            listed = await call("GET", base + "/keys", 200, headers=system_headers)
            assert [item["key_id"] for item in listed.json()["items"]] == [key_id]
            assert issued.json()["secret"] not in listed.text
            await call("DELETE", base + "/keys/" + key_id, 204, headers=system_headers)
            await call("GET", "/v1/auth/me", 401, headers=key_headers)
            pending_key = await call("POST", base + "/keys", 201, headers=system_headers, json=key_request)
            pending_key_headers = {"Authorization": "Bearer " + pending_key.json()["secret"]}
            # The effective-system endpoint is deliberately empty. All native
            # management below must derive authority from raw IDs and membership.
            validator_token = next(key for key, item in directory.tokens.items() if item["user_id"] == validator_id)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{identity.server_port}", timeout=20
            ) as identity_client:
                omitted = await identity_client.get(
                    "/v3/role_assignments",
                    headers={"X-Auth-Token": validator_token},
                    params={
                        "user.id": user,
                        "scope.system": "all",
                        "effective": "true",
                    },
                )
            assert omitted.status_code == 200 and omitted.json()["role_assignments"] == []
            directory.assign({"user": user, "system": True, "role": "admin"}, False)
            directory.assign({"group": group_id, "system": True, "role": alias}, True)
            directory.memberships.add((user, group_id))
            await call("GET", "/v1/me", 200, headers=system_headers)
            await call("GET", "/v1/auth/me", 200, headers=pending_key_headers)
            directory.memberships.discard((user, group_id))
            directory.assign({"user": user, "project": caller, "role": "admin"}, True)
            await call("GET", "/v1/auth/me", 403, headers=pending_key_headers)
            await call("GET", base + "/keys", 401, headers=system_headers)
            directory.assign({"user": user, "project": caller, "role": "admin"}, False)
            directory.memberships.add((user, group_id))
            directory.edge(alias, directory.ids["admin"], False)
            await call("GET", "/v1/auth/me", 403, headers=pending_key_headers)
            directory.edge(alias, directory.ids["admin"], True)
            await call("GET", "/v1/auth/me", 200, headers=pending_key_headers)
            await call("GET", base + "/packages", 403, headers={"X-Auth-Token": token, "X-Project-Id": target})
            await call("DELETE", base + "/package", 204, params=query)
            await call("GET", version, 404, params=query)

            async def layer(payload, name, **metadata):
                opened = await call("POST", "/v1/uploads", 200, json={})
                path = "/v1/uploads/" + opened.json()["session_id"]
                await call(
                    "PATCH",
                    path,
                    200,
                    content=payload,
                    headers={**headers, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
                )
                result = await call(
                    "PUT",
                    path,
                    200,
                    headers={**headers, "Upload-Offset": str(len(payload))},
                    json={"name": name, "arch": "x86_64", **metadata},
                )
                return result.json()["blob_digest"]

            image = await layer(b"synthetic nonbootable raw image", "proof-base", kind="cloud-image", disk_format="raw")
            digest = await layer(
                b"synthetic squashfs metadata proof", "proof-layer", base_image_digest=image, is_published=True
            )
            await call("GET", f"/v1/layers/{digest}", 200)
            blob = await call("GET", f"/v1/layers/{digest}/blob", 200)
            assert blob.content == b"synthetic squashfs metadata proof"
            await call("POST", "/v1/bundles", 200, json={"refs": [digest]})
            job = await call(
                "POST",
                "/v1/builds",
                202,
                json={
                    "name": "proof-build",
                    "recipe": f"FROM {image}\nRUN true",
                    "base_digest": image,
                },
            )
            await call("GET", "/v1/builds/" + job.json()["id"], 200)
            await call("GET", "/v1/builds", 200)
            async with database._session_factory() as session:
                row = await session.get(PalimpsestHubBuild, job.json()["id"])
                assert row.project_id == target and row.created_by == user and row.status == "queued"
                artifact = await session.scalar(
                    select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest)
                )
                assert artifact.project_id == target and artifact.created_by == user and artifact.is_published
                assert await session.scalar(select(func.count()).select_from(PackageKey)) == 2
                key_row = await session.get(PackageKey, key_id.replace("-", ""))
                assert key_row.owner_user_id == user and key_row.project_id == target
                assert key_row.admin_issuer_context["project_id"] == "" and key_row.revoked_at is not None
            await call("DELETE", f"/v1/layers/{digest}", 204)
            await call("GET", f"/v1/layers/{digest}", 404)
            pending = await call("POST", base + "/uploads", 201, params=query, json=body)
            pending_path = base + "/uploads/" + pending.json()["upload_id"]
            directory.memberships.discard((user, group_id))
            await call(
                "PATCH",
                pending_path,
                403,
                params=query,
                content=archive,
                headers={**headers, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
            )
            await call("POST", "/v1/uploads", 403, json={})
            await call("GET", "/v1/builds", 403)
            await call("GET", "/v1/auth/me", 403, headers=pending_key_headers)
            async with database._session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(PackageTag)) == 0
                assert await session.scalar(select(func.count()).select_from(PackageVersion)) == 0
                upload_row = await session.get(PackageUpload, pending.json()["upload_id"])
                assert (
                    upload_row.received_bytes == 0
                    and upload_row.owner_user_id == user
                    and upload_row.project_id == target
                )
            assert store.upload_path(pending.json()["upload_id"]).stat().st_size == 0
        assert not any(entry.get("methods") == ["token"] for entry in directory.requests)
        return {
            "scope": "actual registered app + HTTP + installed SDK + isolated SQLite/CAS + synthetic Keystone",
            "checks": observations,
            "caller_project_preserved": True,
            "target_membership": False,
            "requester_audit_verified": True,
            "revocation_before_io_denied": True,
            "effective_system_view_empty": True,
            "raw_direct_and_group_implied_authority": True,
            "cloud_contacted": False,
            "build_worker_executed": False,
            "scratch": str(root),
        }
    finally:
        server.should_exit = True
        await serving
        listener.close()
        await database.close_db()
        await asyncio.to_thread(identity.shutdown)
        identity.server_close()
        await asyncio.to_thread(identity_thread.join, 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    if not args.scratch.is_dir():
        parser.error("--scratch must be an existing owned writable directory")
    print(json.dumps(asyncio.run(smoke(args.scratch)), sort_keys=True))


if __name__ == "__main__":
    main()
