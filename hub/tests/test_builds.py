"""Project isolation and observable upload → build → download transitions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub import build_worker
from palimpsest_hub.api.builds import BuildRequest, create_hub_build
from palimpsest_hub.auth import get_token_info, require_admin
from palimpsest_hub.config import BuildWorkerSettings
from palimpsest_hub.main import app
from palimpsest_hub.models import Base, PalimpsestHubBuild
from palimpsest_hub.services import builds as build_service
from palimpsest_hub.services.hub_store import LocalPathBlobStore


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_build_worker_requires_kvm_preflight_before_opening_database(tmp_path: Path, monkeypatch):
    settings = BuildWorkerSettings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}",
        palimpsest_hub_local_path=str(tmp_path / "store"),
        palimpsest_hub_builder_python=sys.executable,
        _env_file=None,
    )
    monkeypatch.setattr(build_worker, "get_build_worker_settings", lambda: settings)
    monkeypatch.setattr(build_worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        build_worker,
        "os",
        SimpleNamespace(access=lambda *_: True, R_OK=os.R_OK, W_OK=os.W_OK, environ={"PATH": os.environ["PATH"]}),
    )
    monkeypatch.setattr(build_worker.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    monkeypatch.setattr(build_worker, "init_db", lambda *args, **kwargs: pytest.fail("DB opened before KVM preflight"))
    with pytest.raises(RuntimeError, match="KVM preflight"):
        await build_worker.main()
    assert not (tmp_path / "hub.sqlite").exists()


@pytest.mark.asyncio
async def test_project_build_consumes_uploaded_base_and_publishes_private_layer(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    settings = SimpleNamespace(
        palimpsest_hub_builder_python="/usr/bin/python3",
        palimpsest_hub_build_timeout_seconds=60,
        palimpsest_hub_max_blob_bytes=1024 * 1024,
    )
    monkeypatch.setattr("palimpsest_hub.api.hub.get_session_factory", lambda: factory)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_blob_store", lambda: store)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_settings", lambda: settings)
    monkeypatch.setattr("palimpsest_hub.api.builds.get_settings", lambda: settings)
    monkeypatch.setattr(build_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(build_service, "get_blob_store", lambda _settings=None: store)
    monkeypatch.setattr(build_service, "get_build_worker_settings", lambda: settings)

    async def identity(request: Request):
        return {
            "project_id": request.headers.get("x-project-id", "alpha"),
            "user_id": "member",
            "is_system_admin": False,
        }

    async def admin(request: Request):
        if request.headers.get("x-role") != "admin":
            raise HTTPException(status_code=403, detail="admin required")
        return await identity(request)

    app.dependency_overrides[get_token_info] = identity
    app.dependency_overrides[require_admin] = admin
    output = b"hsqs\x00\x00test-layer"
    output_digest = digest(output)

    def vm_executor(python, manifest, job_dir, timeout):
        payload = json.loads(manifest.read_text())
        assert payload["recipe"] == f"FROM {base_digest}\nRUN echo ready"
        assert payload["base"]["digest"] == base_digest
        assert payload["base"]["path"] == str(store.blob_path(base_digest))
        assert timeout == 60
        (job_dir / "result.sqsh").write_bytes(output)
        return {"digest": output_digest, "size_bytes": len(output)}

    monkeypatch.setattr(build_service, "_run_guest_build", vm_executor)
    monkeypatch.setattr(build_service, "_cleanup_guest", lambda python, job_dir: None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            base = b"raw cloud image fixture"
            base_digest = digest(base)
            started = await client.post("/v1/uploads", json={})
            assert started.status_code == 200
            upload = started.json()["session_id"]
            first = await client.patch(f"/v1/uploads/{upload}", content=base[:7], headers={"Upload-Offset": "0"})
            assert first.status_code == 200
            assert first.json()["received_bytes"] == 7
            with store.upload_path(upload).open("ab") as handle:
                handle.write(b"unacknowledged bytes from an interrupted request")
            appended = await client.patch(f"/v1/uploads/{upload}", content=base[7:], headers={"Upload-Offset": "7"})
            assert appended.status_code == 200
            assert appended.json()["received_bytes"] == len(base)
            registered = await client.put(
                f"/v1/uploads/{upload}",
                headers={"Upload-Offset": str(len(base))},
                json={"name": "base-image", "kind": "cloud-image", "arch": "x86_64", "disk_format": "raw"},
            )
            assert registered.status_code == 200
            assert registered.json()["blob_digest"] == base_digest
            assert (await client.get(f"/v1/layers/{base_digest}/blob")).content == base

            foreign = {"X-Project-Id": "beta", "X-Role": "admin"}
            assert (await client.get("/v1/layers", headers=foreign)).json() == []
            assert (
                await client.post(
                    "/v1/builds",
                    headers=foreign,
                    json={
                        "name": "private-output",
                        "base_digest": base_digest,
                        "recipe": f"FROM {base_digest}\nRUN echo ready",
                    },
                )
            ).status_code == 404
            build_input = {
                "name": "private-output",
                "base_digest": base_digest,
                "recipe": f"FROM {base_digest}\nRUN echo ready",
            }
            assert (await client.post("/v1/builds", json=build_input)).status_code == 403
            queued = await client.post("/v1/builds", headers={"X-Role": "admin"}, json=build_input)
            assert queued.status_code == 202, queued.text
            job = queued.json()
            assert job["status"] == "queued"
            assert "recipe" not in job and "created_by" not in job
            assert (await client.get(f"/v1/builds/{job['id']}", headers=foreign)).status_code == 404

            assert await build_service.process_one_hub_build("test-worker") is True
            complete = (await client.get(f"/v1/builds/{job['id']}", headers={"X-Role": "admin"})).json()
            assert complete["status"] == "complete"
            assert complete["output_digest"] == output_digest
            assert (await client.get(f"/v1/layers/{output_digest}/blob")).content == output
            assert (await client.get(f"/v1/layers/{output_digest}/blob", headers=foreign)).status_code == 404
            assert (await client.get("/v1/builds", headers=foreign)).json() == []

            wrong_digest = digest(b"different output")

            def corrupted_result(python, manifest, job_dir, timeout):
                (job_dir / "result.sqsh").write_bytes(output)
                return {"digest": wrong_digest, "size_bytes": len(output)}

            monkeypatch.setattr(build_service, "_run_guest_build", corrupted_result)
            bad = await client.post(
                "/v1/builds", headers={"X-Role": "admin"}, json={**build_input, "name": "corrupt-result"}
            )
            assert bad.status_code == 202
            assert await build_service.process_one_hub_build("test-worker") is True
            failed = (await client.get(f"/v1/builds/{bad.json()['id']}", headers={"X-Role": "admin"})).json()
            assert failed["status"] == "error" and failed["output_digest"] is None
            assert (await client.get(f"/v1/layers/{wrong_digest}/blob")).status_code == 404
            assert not store.exists(wrong_digest)

            async def enqueue(index: int):
                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/v1/builds",
                        "headers": [],
                        "client": (f"192.0.2.{index + 1}", 1234),
                    }
                )
                return await create_hub_build(
                    request,
                    BuildRequest(**{**build_input, "name": f"burst-{index}"}),
                    {"project_id": "alpha", "user_id": "member", "is_system_admin": True},
                )

            outcomes = await asyncio.gather(*(enqueue(index) for index in range(8)), return_exceptions=True)
            assert len([result for result in outcomes if isinstance(result, dict)]) == 4
            assert (
                len([result for result in outcomes if isinstance(result, HTTPException) and result.status_code == 429])
                == 4
            )
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_interrupted_builder_with_unverifiable_identity_stops_worker_and_retains_tree(
    tmp_path: Path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    build_id = "00000000-0000-4000-8000-000000000001"
    job_dir = store.root / "builds" / build_id
    job_dir.mkdir(parents=True)
    (job_dir / "builder.pid").write_text("{invalid")
    async with factory() as session:
        session.add(
            PalimpsestHubBuild(
                id=build_id,
                project_id="alpha",
                name="interrupted",
                recipe="RUN true",
                recipe_digest=digest(b"RUN true"),
                base_digest=digest(b"base"),
                layer_digests=[],
                status="building",
                lease_owner="old-worker",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    monkeypatch.setattr(build_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(build_service, "get_blob_store", lambda _settings=None: store)
    monkeypatch.setattr(
        build_service,
        "get_build_worker_settings",
        lambda: SimpleNamespace(palimpsest_hub_builder_python="/usr/bin/python3"),
    )
    try:
        with pytest.raises(build_service.BuildCleanupError):
            await build_service.fail_interrupted_builds()
        async with factory() as session:
            row = await session.get(PalimpsestHubBuild, build_id)
            assert row.status == "error" and row.error_code == "cleanup_failed"
        assert (job_dir / "builder.pid").read_text() == "{invalid"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_build_retries_failed_private_scratch_cleanup(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    build_id = "00000000-0000-4000-8000-000000000002"
    job_dir = store.root / "builds" / build_id
    job_dir.mkdir(parents=True)
    (job_dir / "input.json").write_text("private recipe", encoding="utf-8")
    output_digest = digest(b"published output")
    async with factory() as session:
        session.add(
            PalimpsestHubBuild(
                id=build_id,
                project_id="alpha",
                name="completed",
                recipe="RUN true",
                recipe_digest=digest(b"RUN true"),
                base_digest=digest(b"base"),
                layer_digests=[],
                status="complete",
                output_digest=output_digest,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    monkeypatch.setattr(build_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(build_service, "get_blob_store", lambda _settings=None: store)
    monkeypatch.setattr(
        build_service,
        "get_build_worker_settings",
        lambda: SimpleNamespace(palimpsest_hub_builder_python="/usr/bin/python3"),
    )
    monkeypatch.setattr(build_service, "_cleanup_guest", lambda python, job_dir: None)
    remove = build_service.shutil.rmtree

    def denied_cleanup(path):
        raise OSError("scratch removal failed")

    monkeypatch.setattr(build_service.shutil, "rmtree", denied_cleanup)
    try:
        with pytest.raises(build_service.BuildCleanupError):
            await build_service.fail_interrupted_builds()
        async with factory() as session:
            row = await session.get(PalimpsestHubBuild, build_id)
            assert row.status == "complete" and row.output_digest == output_digest
            assert row.error_code == "cleanup_failed"
        assert job_dir.exists()

        monkeypatch.setattr(build_service.shutil, "rmtree", remove)
        assert await build_service.fail_interrupted_builds() == 1
        async with factory() as session:
            row = await session.get(PalimpsestHubBuild, build_id)
            assert row.status == "complete" and row.output_digest == output_digest
            assert row.error_code is None
        assert not job_dir.exists()
    finally:
        monkeypatch.setattr(build_service.shutil, "rmtree", remove)
        await engine.dispose()


@pytest.mark.asyncio
async def test_published_build_stops_worker_when_guest_cleanup_fails(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    build_id = "00000000-0000-4000-8000-000000000003"
    output_digest = digest(b"verified output")
    async with factory() as session:
        session.add(
            PalimpsestHubBuild(
                id=build_id,
                project_id="alpha",
                name="completed",
                recipe="RUN true",
                recipe_digest=digest(b"RUN true"),
                base_digest=digest(b"base"),
                layer_digests=[],
                status="queued",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    monkeypatch.setattr(build_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(build_service, "get_blob_store", lambda _settings=None: store)
    monkeypatch.setattr(
        build_service,
        "get_build_worker_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_builder_python="/usr/bin/python3", palimpsest_hub_build_timeout_seconds=60
        ),
    )

    async def input_manifest(build_id, job_dir):
        return {"name": "completed"}

    async def publish(build_id, owner, job_dir, result):
        async with factory() as session:
            row = await session.get(PalimpsestHubBuild, build_id)
            row.status = "complete"
            row.output_digest = output_digest
            row.lease_owner = None
            await session.commit()

    def denied_guest_cleanup(python, job_dir):
        raise build_service.BuildCleanupError("guest cleanup failed")

    monkeypatch.setattr(build_service, "_input_manifest", input_manifest)
    monkeypatch.setattr(build_service, "_run_guest_build", lambda *args: {"digest": output_digest})
    monkeypatch.setattr(build_service, "_publish_output", publish)
    monkeypatch.setattr(build_service, "_cleanup_guest", denied_guest_cleanup)
    try:
        with pytest.raises(build_service.BuildCleanupError):
            await build_service.process_one_hub_build("owner")
        async with factory() as session:
            row = await session.get(PalimpsestHubBuild, build_id)
            assert row.status == "complete" and row.output_digest == output_digest
            assert row.error_code == "cleanup_failed"
        assert (store.root / "builds" / build_id / "input.json").exists()
    finally:
        await engine.dispose()


def test_failed_guest_run_never_reclaims_state_before_group_verification(tmp_path: Path, monkeypatch):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    manifest = job_dir / "input.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        build_service,
        "_cleanup_guest",
        lambda python, path: pytest.fail("guest cleanup ran before the recorded group was verified"),
    )

    # The interpreter lacks palimpsest_local, so the builder exits nonzero.
    with pytest.raises(RuntimeError, match="isolated builder failed"):
        build_service._run_guest_build(sys.executable, manifest, job_dir, 60)
    assert (job_dir / "stderr").read_text(encoding="utf-8").strip()


@pytest.mark.asyncio
async def test_unverifiable_builder_group_blocks_guest_cleanup_and_retains_tree(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    build_id = "00000000-0000-4000-8000-000000000004"
    async with factory() as session:
        session.add(
            PalimpsestHubBuild(
                id=build_id,
                project_id="alpha",
                name="timed-out",
                recipe="RUN true",
                recipe_digest=digest(b"RUN true"),
                base_digest=digest(b"base"),
                layer_digests=[],
                status="queued",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    monkeypatch.setattr(build_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(build_service, "get_blob_store", lambda _settings=None: store)
    monkeypatch.setattr(
        build_service,
        "get_build_worker_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_builder_python="/usr/bin/python3", palimpsest_hub_build_timeout_seconds=60
        ),
    )

    async def input_manifest(build_id, job_dir):
        return {"name": "timed-out"}

    def timed_out(*_args):
        raise RuntimeError("build timed out")

    def unverifiable_group(job_dir):
        raise build_service.BuildCleanupError("builder process group could not be reaped")

    monkeypatch.setattr(build_service, "_input_manifest", input_manifest)
    monkeypatch.setattr(build_service, "_run_guest_build", timed_out)
    monkeypatch.setattr(build_service, "_stop_interrupted_builder", unverifiable_group)
    monkeypatch.setattr(
        build_service,
        "_cleanup_guest",
        lambda python, job_dir: pytest.fail("guest cleanup ran without a reaped builder group"),
    )
    try:
        with pytest.raises(build_service.BuildCleanupError):
            await build_service.process_one_hub_build("owner")
        async with factory() as session:
            row = await session.get(PalimpsestHubBuild, build_id)
            assert row.status == "error" and row.error_code == "cleanup_failed"
            assert row.lease_owner is None
        assert (store.root / "builds" / build_id / "input.json").exists()
    finally:
        await engine.dispose()
