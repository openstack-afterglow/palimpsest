"""Consumer boundaries for authenticated streamed remote builds (real loopback HTTP)."""

from __future__ import annotations

import asyncio
import hashlib
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import builds, hub
from palimpsest_hub.auth import require_admin
from palimpsest_hub.build_worker import validate_claim
from palimpsest_hub.config import BuildWorkerSettings, Settings
from palimpsest_hub.models import Base, PalimpsestHubBuild, PalimpsestHubLayer
from palimpsest_hub.rate_limit import limiter
from palimpsest_hub.services import builds as service
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, LocalPathBlobStore

KEY = "worker-key-only-for-disposable-test-1234567890"
WORKER = {"X-Palimpsest-Build-Worker-Key": KEY}
ADMIN = {"X-Role": "admin"}
FOREIGN = {"X-Role": "admin", "X-Project-Id": "beta"}


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


async def streamed(payload: bytes):
    for offset in range(0, len(payload), 7):
        yield payload[offset : offset + 7]
        await asyncio.sleep(0)


@pytest.fixture
async def remote_hub(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builds.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    settings = SimpleNamespace(
        palimpsest_hub_build_worker_key=SecretStr(KEY),
        palimpsest_hub_build_timeout_seconds=60,
        palimpsest_hub_max_blob_bytes=1024 * 1024,
    )
    monkeypatch.setattr(hub, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub, "get_blob_store", lambda: store)
    for module in (hub, builds, service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(limiter, "enabled", False)
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(hub.router, prefix="/v1")
    app.include_router(builds.router, prefix="/v1")

    async def identity(request: Request):
        return {
            "project_id": request.headers.get("X-Project-Id", "alpha"),
            "user_id": "creator",
            "is_system_admin": False,
        }

    async def admin(request: Request):
        if request.headers.get("X-Role") != "admin":
            raise HTTPException(status_code=403, detail="admin required")
        return await identity(request)

    # Only tenant identity is replaced. Worker header authentication is real.
    app.dependency_overrides[require_admin] = admin
    for dependency in (hub._legacy_writer, hub._legacy_inventory, hub._legacy_download):
        app.dependency_overrides[dependency] = identity
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=20) as client:
            yield SimpleNamespace(client=client, factory=factory, store=store, settings=settings, app=app)
    finally:
        server.should_exit = True
        await task
        listener.close()
        await engine.dispose()


async def upload_base(h, payload=b"raw visible cloud image", headers=None):
    started = await h.client.post("/v1/uploads", json={}, headers=headers)
    assert started.status_code == 200, started.text
    upload = started.json()["session_id"]
    appended = await h.client.patch(
        f"/v1/uploads/{upload}", content=streamed(payload), headers={**(headers or {}), "Upload-Offset": "0"}
    )
    assert appended.status_code == 200, appended.text
    registered = await h.client.put(
        f"/v1/uploads/{upload}",
        headers={**(headers or {}), "Upload-Offset": str(len(payload))},
        json={"name": "base-image", "kind": "cloud-image", "arch": "x86_64", "disk_format": "raw"},
    )
    assert registered.status_code == 200, registered.text
    return digest(payload)


async def enqueue(h, base, *, name="private-output", layers=(), headers=None):
    recipe = f"FROM {base}\n" + "".join(f"LAYER {layer}\n" for layer in layers) + "RUN echo ready"
    return await h.client.post(
        "/v1/builds",
        headers=headers or ADMIN,
        json={"name": name, "base_digest": base, "layer_digests": list(layers), "recipe": recipe},
    )


async def claim(h):
    response = await h.client.post("/v1/build-worker/claim", headers=WORKER)
    assert response.status_code == 202, response.text
    return response.json()


def attempt_headers(job):
    return {**WORKER, "X-Palimpsest-Build-Lease": job["lease"]}


def attempt_path(job):
    return f"/v1/build-worker/builds/{job['id']}"


async def output(h, job, payload, *, expected=None, extra_headers=None):
    return await h.client.put(
        attempt_path(job) + "/output",
        content=streamed(payload),
        headers={
            **attempt_headers(job),
            "X-Content-Digest": expected or digest(payload),
            "Content-Length": str(len(payload)),
            **(extra_headers or {}),
        },
    )


@pytest.mark.asyncio
async def test_streamed_claim_download_publication_and_lost_response_settlement(remote_hub):
    h = remote_hub
    base_bytes = b"raw visible cloud image"
    base = await upload_base(h, base_bytes)
    unrelated = await upload_base(h, b"other private cloud image")
    assert (await enqueue(h, base, headers=FOREIGN)).status_code == 404
    assert (await enqueue(h, base, headers=WORKER)).status_code == 403
    queued = await enqueue(h, base)
    assert queued.status_code == 202, queued.text
    assert "recipe" not in queued.json() and "lease" not in queued.json()
    assert (await h.client.get(f"/v1/builds/{queued.json()['id']}", headers=FOREIGN)).status_code == 404
    for headers in ({}, ADMIN, {"X-Auth-Token": KEY}, {"X-Palimpsest-Build-Worker-Key": "wrong"}):
        denied = await h.client.post("/v1/build-worker/claim", headers=headers)
        assert denied.status_code == 401 and KEY not in denied.text
    job = await claim(h)
    assert job["id"] == queued.json()["id"]
    assert job["recipe_digest"] == digest(job["recipe"].encode())
    assert job["base"] == {"digest": base, "arch": "x86_64", "disk_format": "raw", "size_bytes": len(base_bytes)}
    assert job["layers"] == [] and 0 < job["timeout_seconds"] <= 60
    assert set(job) == {
        "id",
        "lease",
        "deadline",
        "timeout_seconds",
        "name",
        "recipe",
        "recipe_digest",
        "base",
        "layers",
        "max_output_bytes",
    }
    admitted = validate_claim(job, h.settings.palimpsest_hub_max_blob_bytes, 60)
    assert admitted["base"] == job["base"] and admitted["input_bytes"] == len(base_bytes)
    headers = attempt_headers(job)
    prefix = attempt_path(job)
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 204
    for target in (base, unrelated):
        assert (await h.client.get(prefix + f"/inputs/{target}", headers=WORKER)).status_code == 409
    assert (await h.client.get(prefix + f"/inputs/{unrelated}", headers=headers)).status_code == 404
    async with h.client.stream("GET", prefix + f"/inputs/{base}", headers=headers) as received:
        assert received.status_code == 200
        assert received.headers["Content-Length"] == str(len(base_bytes))
        assert received.headers["X-Content-Digest"] == base
        assert b"".join([chunk async for chunk in received.aiter_bytes()]) == base_bytes
    payload = b"hsqs\0\0exact-worker-output"
    completed = await output(h, job, payload, extra_headers={"X-Project-Id": "beta", "X-Name": "override"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "complete"
    assert completed.json()["output_digest"] == digest(payload)
    assert completed.json()["output_size_bytes"] == len(payload)
    assert "lease" not in completed.json() and "recipe" not in completed.json()
    assert (await h.client.get(prefix, headers=headers)).json() == completed.json()
    assert (await h.client.get(f"/v1/layers/{digest(payload)}/blob")).content == payload
    assert (await h.client.get(f"/v1/layers/{digest(payload)}/blob", headers=FOREIGN)).status_code == 404
    assert (await h.client.get("/v1/builds", headers=FOREIGN)).json() == []
    async with h.factory() as session:
        durable = await session.get(PalimpsestHubBuild, job["id"])
        layer = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(payload))
        )
        assert durable.status == "complete" and durable.lease_owner == job["lease"]
        assert durable.completed_at is not None and durable.project_id == "alpha"
        assert layer.project_id == "alpha" and layer.created_by == "creator" and not layer.is_published
        assert layer.name == job["name"] and layer.config_json["base_image_digest"] == base
    # The response may have been lost: same nonce is a durable receipt, even after deadline.
    async with h.factory() as session:
        row = await session.get(PalimpsestHubBuild, job["id"])
        row.started_at = datetime.now(UTC) - timedelta(hours=2)
        row.lease_expires_at = row.started_at + timedelta(seconds=60)
        await session.commit()
    repeated = await output(h, job, payload)
    assert repeated.status_code == 200 and repeated.json()["status"] == "complete"
    assert (await output(h, job, b"hsqsconflicting-result")).status_code == 409
    assert (await h.client.post(prefix + "/fail", headers=headers, json={"error_code": "cleanup_failed"})).json()[
        "status"
    ] == "complete"
    assert (
        await h.client.get(prefix, headers={**headers, "X-Palimpsest-Build-Lease": str(uuid4())})
    ).status_code == 409
    assert not list(h.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_output_rejects_wrong_bytes_and_never_registers_them(remote_hub):
    h = remote_hub
    base = await upload_base(h)
    assert (await enqueue(h, base)).status_code == 202
    job = await claim(h)
    payload = b"hsqsactual-output"
    wrong = digest(b"hsqsdifferent-output")
    assert (await output(h, job, payload, expected=wrong)).status_code == 422
    assert not h.store.exists(wrong) and not h.store.exists(digest(payload))
    assert (await output(h, job, b"not-squashfs")).status_code == 422
    prefix = attempt_path(job)
    headers = {**attempt_headers(job), "X-Content-Digest": digest(payload)}
    assert (await h.client.put(prefix + "/output", content=streamed(payload), headers=headers)).status_code == 411
    assert (
        await h.client.put(prefix + "/output", content=payload, headers={**headers, "X-Content-Digest": "bad"})
    ).status_code == 422
    assert (await output(h, job, payload, extra_headers={"X-Palimpsest-Build-Lease": str(uuid4())})).status_code == 409
    # Real HTTP transports enforce framing themselves. Exercise malicious ASGI
    # bodies too: actual streamed bytes must match the declaration at the API boundary.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://fixture") as malicious:
        for declared, code in ((len(payload) - 1, 413), (len(payload) + 1, 422), (0, 413), (1024 * 1024 + 1, 413)):
            response = await malicious.put(
                prefix + "/output", content=streamed(payload), headers={**headers, "Content-Length": str(declared)}
            )
            assert response.status_code == code, response.text
    assert (await h.client.get(prefix, headers=attempt_headers(job))).json()["status"] == "building"
    assert not h.store.exists(digest(payload))
    assert not list(h.store.uploads_dir.iterdir())
    assert (await output(h, job, payload)).status_code == 200
    # A complete receipt cannot be used to bless wrong bytes with matching headers.
    assert (await output(h, job, b"hsqswrong--output", expected=digest(payload))).status_code in (409, 422)


@pytest.mark.asyncio
async def test_global_claim_deadline_nonce_and_terminal_failure_boundaries(remote_hub):
    h = remote_hub
    first_base = await upload_base(h)
    second_base = await upload_base(h, b"beta-cloud-image", FOREIGN)
    first = await enqueue(h, first_base)
    second = await enqueue(h, second_base, headers=FOREIGN)
    assert first.status_code == second.status_code == 202
    responses = await asyncio.gather(*[h.client.post("/v1/build-worker/claim", headers=WORKER) for _ in range(6)])
    assert sorted(response.status_code for response in responses) == [202, 204, 204, 204, 204, 204]
    old = next(response.json() for response in responses if response.status_code == 202)
    assert old["id"] == first.json()["id"]
    old_headers, old_prefix = attempt_headers(old), attempt_path(old)
    observed = (await h.client.get(old_prefix, headers=old_headers)).json()
    assert observed["status"] == "building"
    async with h.factory() as session:
        row = await session.get(PalimpsestHubBuild, old["id"])
        assert row.started_at is not None
        row.started_at = datetime.now(UTC) - timedelta(seconds=61)
        row.lease_expires_at = row.started_at + timedelta(seconds=60)
        await session.commit()
    new = await claim(h)
    assert new["id"] == second.json()["id"] and new["lease"] != old["lease"]
    expired = (await h.client.get(old_prefix, headers=old_headers)).json()
    assert expired["status"] == "error" and expired["error_code"] == "worker_interrupted"
    assert (await output(h, old, b"hsqsexpired-output")).status_code == 409
    assert (await h.client.get(old_prefix + f"/inputs/{first_base}", headers=old_headers)).status_code == 409
    assert (await h.client.get(attempt_path(new), headers=old_headers)).status_code == 409
    assert (
        await h.client.post(old_prefix + "/fail", headers=old_headers, json={"error_code": "build_failed"})
    ).json() == expired
    for body in ({"error_code": "raw exception /secret/path"}, {"error_code": "build_failed", "project_id": "alpha"}):
        assert (
            await h.client.post(attempt_path(new) + "/fail", headers=attempt_headers(new), json=body)
        ).status_code == 422
    failed = await h.client.post(
        attempt_path(new) + "/fail", headers=attempt_headers(new), json={"error_code": "build_timeout"}
    )
    assert failed.status_code == 200 and failed.json()["error_code"] == "build_timeout"
    again = await h.client.post(
        attempt_path(new) + "/fail", headers=attempt_headers(new), json={"error_code": "build_failed"}
    )
    assert again.json() == failed.json()
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 204
    assert (await output(h, new, b"hsqsfailed-output")).status_code == 409
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubBuild))).all()
        assert len(rows) == 2 and all(row.status == "error" and row.lease_owner for row in rows)


@pytest.mark.asyncio
async def test_claim_and_download_recheck_visibility_chain_and_bytes(remote_hub):
    h = remote_hub
    base = await upload_base(h)
    queued = await enqueue(h, base)
    async with h.factory() as session:
        source = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == base))
        source.project_id = "beta"
        await session.commit()
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 204
    async with h.factory() as session:
        row = await session.get(PalimpsestHubBuild, queued.json()["id"])
        assert row.status == "error" and row.lease_owner is None
        source = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == base))
        source.project_id = "alpha"
        await session.commit()
    assert (await enqueue(h, base)).status_code == 202
    job = await claim(h)
    url = attempt_path(job) + f"/inputs/{base}"
    async with h.factory() as session:
        source = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == base))
        source.project_id = "beta"
        await session.commit()
    assert (await h.client.get(url, headers=attempt_headers(job))).status_code == 409
    async with h.factory() as session:
        source = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == base))
        source.project_id = "alpha"
        await session.commit()
    original = h.store.blob_path(base).read_bytes()
    h.store.blob_path(base).write_bytes(b"x" * len(original))
    assert (await h.client.get(url, headers=attempt_headers(job))).status_code == 409
    h.store.blob_path(base).write_bytes(original)
    assert (await h.client.get(url, headers=attempt_headers(job))).content == original
    payload = b"hsqsfirst-parent-layer"
    assert (await output(h, job, payload)).status_code == 200
    parent = digest(payload)
    queued = await enqueue(h, base, name="child-output", layers=[parent])
    assert queued.status_code == 202, queued.text
    child = await claim(h)
    assert child["layers"] == [
        {
            "digest": parent,
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
            "parent_digest": None,
            "base_image_digest": base,
            "size_bytes": len(payload),
        }
    ]
    assert (
        await h.client.get(attempt_path(child) + f"/inputs/{parent}", headers=attempt_headers(child))
    ).content == payload
    async with h.factory() as session:
        layer = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == parent))
        layer.parent_digest = digest(b"missing-parent")
        await session.commit()
    assert (
        await h.client.get(attempt_path(child) + f"/inputs/{parent}", headers=attempt_headers(child))
    ).status_code == 409
    async with h.factory() as session:
        layer = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == parent))
        layer.parent_digest = None
        await session.commit()
    child_payload = b"hsqssecond-child-layer"
    assert (await output(h, child, child_payload)).status_code == 200
    registered = (await h.client.get(f"/v1/layers/{digest(child_payload)}")).json()
    assert registered["parent_digest"] == parent and registered["base_image_digest"] == base
    assert registered["ancestors"] == [parent] and registered["chain_complete"] is True
    assert (await enqueue(h, base, name="broken-chain", layers=[digest(child_payload)])).status_code == 422
    # Explicit contradictory base metadata on a descendant cannot be emitted
    # as a claim that the real executor would reject after consuming authority.
    async with h.factory() as session:
        descendant = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(child_payload))
        )
        descendant.config_json = {**descendant.config_json, "base_image_digest": digest(b"other-base")}
        await session.commit()
    assert (await enqueue(h, base, name="wrong-descendant", layers=[parent, digest(child_payload)])).status_code == 422


@pytest.mark.asyncio
async def test_public_project_queue_cap_is_atomic_and_worker_cannot_read_other_jobs(remote_hub):
    h = remote_hub
    base = await upload_base(h)
    responses = await asyncio.gather(*(enqueue(h, base, name=f"burst-{index}") for index in range(8)))
    assert sorted(response.status_code for response in responses) == [202, 202, 202, 202, 429, 429, 429, 429]
    job = await claim(h)
    other_id = next(
        response.json()["id"]
        for response in responses
        if response.status_code == 202 and response.json()["id"] != job["id"]
    )
    assert (await h.client.get(f"/v1/build-worker/builds/{other_id}", headers=attempt_headers(job))).status_code == 409
    assert (await h.client.get(f"/v1/build-worker/builds/{uuid4()}", headers=attempt_headers(job))).status_code == 404


@pytest.mark.asyncio
async def test_deadline_is_not_extended_by_download_status_or_a_stream_in_progress(remote_hub, monkeypatch):
    h = remote_hub
    base = await upload_base(h)
    assert (await enqueue(h, base)).status_code == 202
    job = await claim(h)
    prefix, headers = attempt_path(job), attempt_headers(job)
    started = (await h.client.get(prefix, headers=headers)).json()["started_at"]
    assert (await h.client.get(prefix + f"/inputs/{base}", headers=headers)).status_code == 200
    assert (await h.client.get(prefix, headers=headers)).json()["started_at"] == started
    payload = b"hsqsstream-crosses-fixed-deadline"

    async def crosses_deadline():
        yield payload[:4]
        # This real HTTP request completes while the output connection remains open.
        assert (await h.client.get(prefix, headers=headers)).json()["status"] == "building"
        expired_now = datetime.now(UTC) + timedelta(seconds=61)
        monkeypatch.setattr(service, "_now", lambda: expired_now)
        yield payload[4:]

    rejected = await h.client.put(
        prefix + "/output",
        content=crosses_deadline(),
        headers={
            **headers,
            "Content-Length": str(len(payload)),
            "X-Content-Digest": digest(payload),
        },
    )
    assert rejected.status_code == 409, rejected.text
    status = (await h.client.get(prefix, headers=headers)).json()
    assert status["status"] == "error" and status["error_code"] == "worker_interrupted"
    assert status["output_digest"] is None and not h.store.exists(digest(payload))
    assert not list(h.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_worker_key_admission_is_disabled_when_unconfigured(remote_hub):
    h = remote_hub
    base = await upload_base(h)
    h.settings.palimpsest_hub_build_worker_key = SecretStr("")
    assert (await enqueue(h, base)).status_code == 503
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 401
    # No configured worker credential is equivalent to a tenant or admin credential.
    assert (await h.client.get("/v1/builds", headers=WORKER)).status_code == 403


@pytest.mark.parametrize(
    "origin", ["http://hub", "https://user:password@hub", "https://hub/v1", "https://hub?x=1", "https://hub#fragment"]
)
def test_worker_settings_reject_non_origin_or_insecure_urls(origin):
    with pytest.raises(ValidationError):
        BuildWorkerSettings(
            palimpsest_hub_url=origin,
            palimpsest_hub_build_worker_key_file="/run/key",
            palimpsest_hub_build_work_path="/scratch",
            _env_file=None,
        )


@pytest.mark.parametrize(
    "key",
    ["too-short", "k" * 4097, "a" * 16 + " " + "b" * 16, "a" * 16 + "\t" + "b" * 16, "a" * 16 + "\x7f" + "b" * 16],
)
def test_api_configuration_rejects_worker_keys_the_executor_cannot_use(key):
    with pytest.raises(ValidationError):
        Settings(
            database_url="sqlite+aiosqlite://",
            redis_url="redis://localhost",
            palimpsest_hub_local_path="/cas",
            os_auth_url="https://identity",
            os_username="exporter",
            os_password="secret",
            os_project_name="service",
            palimpsest_hub_build_worker_key=key,
            _env_file=None,
        )


@pytest.mark.asyncio
async def test_existing_descriptor_or_public_blob_cannot_be_rebranded_as_private_output(remote_hub):
    h = remote_hub
    base = await upload_base(h)
    payload = b"hsqsidentical-content-different-authority"
    assert (await enqueue(h, base, name="original-name")).status_code == 202
    original = await claim(h)
    assert (await output(h, original, payload)).status_code == 200
    assert (await enqueue(h, base, name="different-name")).status_code == 202
    incompatible = await claim(h)
    assert (await output(h, incompatible, payload)).status_code == 409
    await h.client.post(
        attempt_path(incompatible) + "/fail", headers=attempt_headers(incompatible), json={"error_code": "build_failed"}
    )
    async with h.factory() as session:
        layer = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(payload))
        )
        assert layer.name == "original-name" and layer.project_id == "alpha" and not layer.is_published
        layer.is_published = True
        await session.commit()
    assert (await enqueue(h, base, name="original-name")).status_code == 202
    public_collision = await claim(h)
    assert (await output(h, public_collision, payload)).status_code == 409
    async with h.factory() as session:
        job = await session.get(PalimpsestHubBuild, public_collision["id"])
        layer = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(payload))
        )
        assert job.status == "building" and job.output_digest is None
        assert layer.is_published and layer.name == "original-name"


@pytest.mark.asyncio
async def test_aggregate_worker_input_limit_is_enforced_before_admission_and_claim(remote_hub):
    h = remote_hub
    base_bytes = b"raw visible cloud image"
    base = await upload_base(h, base_bytes)
    assert (await enqueue(h, base)).status_code == 202
    first = await claim(h)
    payload = b"hsqsparent-for-total-byte-limit"
    assert (await output(h, first, payload)).status_code == 200
    parent = digest(payload)
    # Each input fits independently; their sum does not.
    limit = max(len(base_bytes), len(payload))
    h.settings.palimpsest_hub_max_blob_bytes = limit
    refused = await enqueue(h, base, name="oversize-inputs", layers=[parent])
    assert refused.status_code == 422 and "total build inputs" in refused.text
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 204
    h.settings.palimpsest_hub_max_blob_bytes = 1024 * 1024
    accepted = await enqueue(h, base, name="changed-limits", layers=[parent])
    assert accepted.status_code == 202
    h.settings.palimpsest_hub_max_blob_bytes = limit
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 204
    async with h.factory() as session:
        row = await session.get(PalimpsestHubBuild, accepted.json()["id"])
        assert row.status == "error" and row.error_code == "build_failed" and row.lease_owner is None


@pytest.mark.asyncio
async def test_https_build_architecture_rechecked_at_admission_claim_input_and_publication(remote_hub):
    h = remote_hub
    base = await upload_base(h)
    payload = b"hsqs architecture parent"
    parent = digest(payload)
    h.store.blob_path(parent).write_bytes(payload)
    async with h.factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=parent,
                size_bytes=len(payload),
                name="parent",
                kind="squashfs",
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch="aarch64",
                project_id="alpha",
                config_digest=digest(b"parent configuration"),
                config_json={"base_image_digest": base},
            )
        )
        await session.commit()
    rejected = await enqueue(h, base, layers=[parent])
    assert rejected.status_code == 422
    async with h.factory() as session:
        row = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == parent))
        row.arch = "x86_64"
        await session.commit()
    queued = await enqueue(h, base, layers=[parent])
    assert queued.status_code == 202
    async with h.factory() as session:
        row = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == parent))
        row.arch = "aarch64"
        await session.commit()
    assert (await h.client.post("/v1/build-worker/claim", headers=WORKER)).status_code == 204
    async with h.factory() as session:
        row = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == parent))
        row.arch = "x86_64"
        await session.commit()
    assert (await enqueue(h, base, layers=[parent])).status_code == 202
    job = await claim(h)
    async with h.factory() as session:
        row = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == parent))
        row.arch = "aarch64"
        await session.commit()
    denied_input = await h.client.get(attempt_path(job) + "/inputs/" + parent, headers=attempt_headers(job))
    assert denied_input.status_code == 422
    result = b"hsqs output from stale architecture"
    denied_output = await output(h, job, result)
    assert denied_output.status_code == 422
    assert not h.store.exists(digest(result))
    async with h.factory() as session:
        assert (
            await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(result)))
            is None
        )
        assert (await session.get(PalimpsestHubBuild, job["id"])).status != "complete"
