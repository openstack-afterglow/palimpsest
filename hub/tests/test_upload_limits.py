"""HTTP-visible project upload quota and ownership contract."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import hub as hub_api
from palimpsest_hub.auth import get_token_info
from palimpsest_hub.main import app
from palimpsest_hub.models import Base, PalimpsestHubUpload
from palimpsest_hub.services.hub_store import LocalPathBlobStore


@pytest.mark.asyncio
async def test_cancelled_upload_lock_waiter_cannot_strand_session(tmp_path: Path, monkeypatch):
    store = LocalPathBlobStore(tmp_path / "store")
    session_id = "a" * 32
    held = store.acquire_upload_lock(session_id)
    entered = threading.Event()
    acquire = store.acquire_upload_lock

    def notified(value: str) -> int:
        entered.set()
        return acquire(value)

    monkeypatch.setattr(store, "acquire_upload_lock", notified)

    async def waiting_request() -> None:
        async with hub_api._locked_upload(store, session_id):
            raise AssertionError("cancelled request must not enter the critical section")

    try:
        waiter = asyncio.create_task(waiting_request())
        assert await asyncio.to_thread(entered.wait, 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        store.release_blob_lock(held)
    probe = await asyncio.wait_for(asyncio.to_thread(acquire, session_id), timeout=2)
    store.release_blob_lock(probe)


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
