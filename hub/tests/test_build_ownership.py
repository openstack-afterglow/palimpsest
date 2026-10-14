"""API-side ownership with real disposable SQL and disjoint local CAS locks.

Portable SQLite cases define the contract; the opt-in MariaDB/process smoke in
 test_database_pool exercises actual cross-process SQL exclusion. No recipe or
 cloud resource is executed by these tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from palimpsest_hub.models import Base, PalimpsestHubBuild, PalimpsestHubLayer
from palimpsest_hub.services import builds
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.worker_leases import role_lease, sql_mutex


def digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.fixture
async def ownership(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    stores = [LocalPathBlobStore(tmp_path / f"api-{index}") for index in range(3)]
    selected = ContextVar("api_store", default=stores[0])
    base = b"raw visible ownership input"
    base_digest = digest(base)
    for store in stores:
        store.blobs_dir.mkdir(parents=True, exist_ok=True)
        store.blob_path(base_digest).write_bytes(base)
    async with factory() as session, session.begin():
        session.add(
            PalimpsestHubLayer(
                blob_digest=base_digest,
                size_bytes=len(base),
                media_type="application/octet-stream",
                disk_format="raw",
                arch="x86_64",
                config_digest=base_digest,
                name="base",
                kind="cloud-image",
                config_json={},
                project_id=None,
                is_published=True,
            )
        )
    settings = SimpleNamespace(palimpsest_hub_build_timeout_seconds=60, palimpsest_hub_max_blob_bytes=1024)
    monkeypatch.setattr(builds, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(builds, "_store_or_503", selected.get)
    monkeypatch.setattr(builds, "get_settings", lambda: settings)

    async def enqueue(project="alpha"):
        job_id = str(uuid4())
        recipe = f"FROM {base_digest}\nRUN private-command"
        async with factory() as session, session.begin():
            session.add(
                PalimpsestHubBuild(
                    id=job_id,
                    project_id=project,
                    created_by="requester",
                    name="private-output",
                    recipe=recipe,
                    recipe_digest=digest(recipe.encode()),
                    base_digest=base_digest,
                    layer_digests=[],
                    status="queued",
                    created_at=datetime.now(UTC),
                )
            )
        return job_id

    try:
        yield SimpleNamespace(factory=factory, stores=stores, selected=selected, settings=settings, enqueue=enqueue)
    finally:
        await engine.dispose()


async def submit(job, payload):
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "method": "PUT", "path": "/output", "headers": []}, receive)
    return await builds.upload_output(job["id"], job["lease"], request, digest(payload), len(payload))


@pytest.mark.asyncio
async def test_api_single_flight_uses_sql_not_shared_cas_or_worker_identity(ownership):
    h = ownership
    ids = [await h.enqueue(project) for project in ("alpha", "beta", "gamma")]

    async def claim_from(store):
        token = h.selected.set(store)
        try:
            return await builds.claim_build()
        finally:
            h.selected.reset(token)

    claims = await asyncio.gather(*(claim_from(store) for store in h.stores))
    assert sum(job is not None for job in claims) == 1
    first = next(job for job in claims if job is not None)
    assert first["id"] == ids[0]
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubBuild))).all()
        assert sum(row.status == "building" for row in rows) == 1
        assert sum(row.status == "queued" for row in rows) == 2
    # Explicit failure releases single-flight, but retains an immutable receipt.
    failed = await builds.fail_build(first["id"], first["lease"], "cleanup_failed")
    second = await claim_from(h.stores[1])
    assert failed.status == "error" and second["id"] == ids[1]
    assert second["lease"] != first["lease"]
    assert (await builds.worker_status(first["id"], first["lease"])).error_code == "cleanup_failed"
    with pytest.raises(HTTPException) as error:
        await submit(first, b"hsqsstale-result")
    assert error.value.status_code == 409
    assert all(not store.exists(digest(b"hsqsstale-result")) for store in h.stores)


@pytest.mark.asyncio
async def test_persisted_deadline_survives_api_timeout_changes_without_replay(ownership):
    h = ownership
    job_id = await h.enqueue()
    claim = await builds.claim_build()
    async with h.factory() as session:
        original = await session.get(PalimpsestHubBuild, job_id)
        deadline = original.lease_expires_at.replace(tzinfo=UTC)
    h.settings.palimpsest_hub_build_timeout_seconds = 3600
    status = await builds.worker_status(job_id, claim["lease"])
    assert builds._deadline(status) == deadline
    assert status.lease_owner == claim["lease"]
    # API expiry never authorizes private-tree cleanup or a second recipe attempt.
    async with h.factory() as session, session.begin():
        row = await session.get(PalimpsestHubBuild, job_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await builds.claim_build() is None
    status = await builds.worker_status(job_id, claim["lease"])
    assert status.status == "error" and status.error_code == "worker_interrupted"
    assert status.lease_owner == claim["lease"] and status.output_digest is None
    assert await builds.claim_build() is None


@pytest.mark.asyncio
async def test_publication_is_private_and_old_nonce_has_no_effect(ownership):
    h = ownership
    await h.enqueue("beta")
    claim = await builds.claim_build()
    stale = {**claim, "lease": str(uuid4())}
    payload = b"hsqsprivate-publication"
    with pytest.raises(HTTPException) as error:
        await submit(stale, payload)
    assert error.value.status_code == 409
    assert not h.stores[0].exists(digest(payload))
    completed = await submit(claim, payload)
    async with h.factory() as session:
        layer = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(payload))
        )
        assert layer.project_id == "beta" and layer.created_by == "requester" and not layer.is_published
    assert completed.status == "complete" and completed.lease_owner == claim["lease"]
    repeated = await submit(claim, payload)
    assert repeated.output_digest == completed.output_digest
    assert (await builds.fail_build(claim["id"], claim["lease"], "build_failed")).status == "complete"


@pytest.mark.asyncio
async def test_cancelled_publication_drains_before_disjoint_api_can_claim(ownership, monkeypatch):
    h = ownership
    await h.enqueue()
    claim = await builds.claim_build()
    started, release, drained = threading.Event(), threading.Event(), threading.Event()
    original = h.stores[0].publish_verified

    def publish(*args):
        started.set()
        try:
            assert release.wait(5)
            return original(*args)
        finally:
            drained.set()

    monkeypatch.setattr(h.stores[0], "publish_verified", publish)
    publishing = asyncio.create_task(submit(claim, b"hsqscancelled-publication"))
    peer = None

    async def claim_from_peer():
        token = h.selected.set(h.stores[1])
        try:
            return await builds.claim_build()
        finally:
            h.selected.reset(token)

    try:
        assert await asyncio.to_thread(started.wait, 3)
        publishing.cancel()
        publishing.cancel()
        peer = asyncio.create_task(claim_from_peer())
        await asyncio.sleep(0.05)
        assert not publishing.done() and not peer.done() and not drained.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await publishing
        assert drained.is_set() and await peer is None
        status = await builds.worker_status(claim["id"], claim["lease"])
        assert status.status == "building" and status.output_digest is None
    finally:
        release.set()
        await asyncio.gather(publishing, *([peer] if peer is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_api_mutex_namespace_cannot_be_adopted_as_a_worker_role(ownership):
    with pytest.raises(ValueError, match="not renewable"):
        async with role_lease(ownership.factory, "build-api:single-flight"):
            pytest.fail("worker role adopted API mutex")
    with pytest.raises(ValueError, match="build-api namespace"):
        async with sql_mutex(ownership.factory, "export-maintenance"):
            pytest.fail("API mutex adopted export role")
