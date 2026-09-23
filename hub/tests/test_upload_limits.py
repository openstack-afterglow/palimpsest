"""HTTP-visible upload quota, worker admission, lock, and rollback retention contracts."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import hub as hub_api
from palimpsest_hub.auth import get_token_info
from palimpsest_hub.main import app
from palimpsest_hub.models import Base, PalimpsestHubLayer, PalimpsestHubUpload
from palimpsest_hub.services.hub_store import LocalPathBlobStore


@pytest.mark.asyncio
async def test_cancelled_upload_lock_waiter_cannot_strand_session(tmp_path: Path, monkeypatch):
    store = LocalPathBlobStore(tmp_path / "store")
    session_id = "a" * 32
    held = store.acquire_upload_lock(session_id)
    attempted = threading.Event()
    acquire = store.acquire_upload_lock

    def notified(value: str, *, blocking: bool = True) -> int | None:
        attempted.set()
        return acquire(value, blocking=blocking)

    monkeypatch.setattr(store, "acquire_upload_lock", notified)

    async def waiting_request() -> None:
        async with hub_api._locked_upload(store, session_id):
            raise AssertionError("cancelled request must not enter the critical section")

    try:
        waiter = asyncio.create_task(waiting_request())
        assert await asyncio.to_thread(attempted.wait, 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        store.release_blob_lock(held)
    probe = await asyncio.wait_for(asyncio.to_thread(acquire, session_id), timeout=2)
    store.release_blob_lock(probe)


@pytest.mark.asyncio
async def test_same_lock_contenders_beyond_the_thread_pool_still_converge(tmp_path: Path):
    """A lock waiter must never occupy a worker thread the owner needs."""
    store = LocalPathBlobStore(tmp_path / "store")
    session_id = "e" * 32
    digest = "sha256:" + "e" * 64
    bounded = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(bounded)
    entered = 0

    async def contender() -> None:
        nonlocal entered
        async with hub_api._locked_upload(store, session_id):
            entered += 1
            # The owner needs both a second lock and a worker thread while holding
            # the first one; saturated waiters must not be able to starve either.
            async with hub_api._locked_blob(store, digest):
                await hub_api._run_blocking(lambda: None)

    try:
        await asyncio.wait_for(asyncio.gather(*(contender() for _ in range(40))), timeout=60)
    finally:
        bounded.shutdown(wait=False)
    assert entered == 40


@pytest.mark.asyncio
async def test_blocking_hub_workers_are_bounded_without_stalling_heartbeat(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hub_api, "_blocking_work_slots", asyncio.Semaphore(2))
    entered = threading.Event()
    release = threading.Event()
    active_lock = threading.Lock()
    active = 0
    maximum = 0

    def blocking_worker() -> None:
        nonlocal active, maximum
        with active_lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                entered.set()
        release.wait(2)
        with active_lock:
            active -= 1

    tasks = [asyncio.create_task(hub_api._run_blocking(blocking_worker)) for _ in range(3)]
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    assert maximum == 2
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_bounded_workers_admit_more_requests_than_the_thread_pool(monkeypatch: pytest.MonkeyPatch):
    """Capacity waits must not occupy the same executor the admitted work needs."""
    monkeypatch.setattr(hub_api, "_blocking_work_slots", asyncio.Semaphore(2))
    contenders = 64

    def worker() -> int:
        return 1

    completed = await asyncio.wait_for(
        asyncio.gather(*(hub_api._run_blocking(worker) for _ in range(contenders))), timeout=30
    )
    assert sum(completed) == contenders


@pytest.mark.asyncio
async def test_rolled_back_blob_is_kept_whenever_a_layer_row_references_it(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    registered = "sha256:" + "a" * 64
    orphan = "sha256:" + "b" * 64
    for digest in (registered, orphan):
        path = store.blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"published bytes")
    async with factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=registered,
                size_bytes=15,
                media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
                config_digest="sha256:" + "c" * 64,
                name="kept",
                kind="squashfs",
                config_json={"blob_digest": registered},
                project_id="alpha",
                is_published=False,
            )
        )
        await session.commit()
    try:
        await hub_api._discard_unregistered_blob(store, factory, registered)
        assert store.exists(registered)

        await hub_api._discard_unregistered_blob(store, factory, orphan)
        assert not store.exists(orphan)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rolled_back_blob_is_kept_when_registration_state_cannot_be_read(tmp_path: Path):
    store = LocalPathBlobStore(tmp_path / "store")
    digest = "sha256:" + "d" * 64
    path = store.blob_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ambiguous commit bytes")

    def unreadable():
        raise RuntimeError("database unavailable")

    await hub_api._discard_unregistered_blob(store, unreadable, digest)
    assert store.exists(digest)


@pytest.mark.asyncio
async def test_cancelled_finalizer_worker_retains_upload_lock_until_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = LocalPathBlobStore(tmp_path / "store")
    session_id = "d" * 32
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(hub_api, "get_blob_store", lambda: store)

    def blocking_worker() -> None:
        started.set()
        release.wait(2)

    @hub_api._serialize_upload
    async def guarded(value: str) -> None:
        await hub_api._run_blocking(blocking_worker)

    task = asyncio.create_task(guarded(session_id))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    contender = asyncio.create_task(asyncio.to_thread(store.acquire_upload_lock, session_id, blocking=False))
    await asyncio.sleep(0.05)
    assert await contender is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    fd = await asyncio.wait_for(asyncio.to_thread(store.acquire_upload_lock, session_id), timeout=1)
    store.release_blob_lock(fd)


@pytest.mark.asyncio
async def test_sorted_bundle_digest_locks_exclude_gc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = LocalPathBlobStore(tmp_path / "store")
    first = "sha256:" + "a" * 64
    second = "sha256:" + "b" * 64
    order: list[str] = []
    acquire = store.acquire_blob_lock

    def recorded(digest: str, *, blocking: bool = True) -> int | None:
        order.append(digest)
        return acquire(digest, blocking=blocking)

    monkeypatch.setattr(store, "acquire_blob_lock", recorded)
    async with hub_api._locked_blobs(store, [second, first, second]):
        gc_waiter = asyncio.create_task(asyncio.to_thread(acquire, first))
        await asyncio.sleep(0.05)
        assert not gc_waiter.done()
    gc_fd = await asyncio.wait_for(gc_waiter, timeout=1)
    store.release_blob_lock(gc_fd)
    assert order == [first, second]


@pytest.mark.asyncio
async def test_upload_sessions_are_project_bounded_and_released_on_abort(tmp_path: Path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    monkeypatch.setattr("palimpsest_hub.api.hub.get_session_factory", lambda: factory)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_blob_store", lambda: store)

    async def identity(request: Request):
        return {"project_id": request.headers.get("x-project-id", "alpha"), "user_id": "member"}

    app.dependency_overrides[get_token_info] = identity
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            uploads = []
            for _ in range(4):
                response = await client.post("/v1/uploads", json={})
                assert response.status_code == 200
                uploads.append(response.json()["session_id"])
            assert (await client.post("/v1/uploads", json={})).status_code == 429
            foreign = {"X-Project-Id": "beta"}
            assert (await client.get(f"/v1/uploads/{uploads[0]}", headers=foreign)).status_code == 404
            assert (await client.delete(f"/v1/uploads/{uploads[0]}", headers=foreign)).status_code == 404
            assert (await client.post("/v1/uploads", json={}, headers=foreign)).status_code == 200
            async with factory() as session:
                abandoned = await session.get(PalimpsestHubUpload, uploads[0])
                abandoned.updated_at = datetime.now(UTC) - timedelta(days=2)
                await session.commit()
            recovered = await client.post("/v1/uploads", json={})
            assert recovered.status_code == 200
            assert not store.upload_path(uploads[0]).exists()
            assert (await client.get(f"/v1/uploads/{uploads[0]}")).status_code == 404
            assert (await client.delete(f"/v1/uploads/{uploads[1]}")).status_code == 204
            assert (await client.post("/v1/uploads", json={})).status_code == 200
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
