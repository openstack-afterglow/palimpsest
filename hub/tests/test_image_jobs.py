"""Tenant, byte-integrity and atomic multi-output image-job boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.api import builds, hub, image_jobs
from palimpsest_hub.image_models import PalimpsestHubBuildContext, PalimpsestHubImageJob
from palimpsest_hub.models import (
    Base,
    PalimpsestHubLayer,
    PalimpsestHubLayerAccess,
    RuntimeBlobReference,
)
from palimpsest_hub.services import image_jobs as service
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import LocalPathBlobStore

KEY = "disposable-image-worker-key-01234567890123456789"
WORKER = {"X-Palimpsest-Build-Worker-Key": KEY}
HUMAN = {"X-Identity": "human"}


def digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def context_tar(name="hello.txt", payload=b"hello context"):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return result.getvalue()


@pytest.fixture
async def images(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'images.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "cas")
    settings = SimpleNamespace(
        palimpsest_hub_build_worker_key=SecretStr(KEY),
        palimpsest_hub_build_timeout_seconds=60,
        palimpsest_hub_max_blob_bytes=1024 * 1024,
        palimpsest_hub_registry_hosts=("docker.io", "ghcr.io", "quay.io", "registry-1.docker.io"),
        palimpsest_hub_max_context_bytes=16 * 1024 * 1024,
        palimpsest_hub_max_context_expanded_bytes=256 * 1024 * 1024,
        palimpsest_hub_max_context_members=10000,
    )
    monkeypatch.setattr(hub, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub, "get_blob_store", lambda: store)
    for module in (hub, builds, image_jobs, service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(image_jobs.router, prefix="/v1")

    async def identity(request: Request):
        if request.headers.get("X-Identity") != "human":
            raise HTTPException(status_code=403, detail="human native authority required")
        return {
            "project_id": request.headers.get("X-Project", "alpha"),
            "user_id": "caller",
            "package_actions": {"packages:read", "packages:write", "packages:inventory"},
            "is_package_system_admin": False,
            "token": "disposable-native-token",
        }

    app.dependency_overrides[hub._legacy_writer] = identity
    app.dependency_overrides[hub._legacy_inventory] = identity

    async def refresh(token_info):
        return token_info

    monkeypatch.setattr(service, "get_package_member_info", refresh)
    monkeypatch.setattr(
        service,
        "validate_token",
        lambda token, project: {
            "project_id": project,
            "user_id": "caller",
            "token": token,
            "package_actions": {"packages:read", "packages:write", "packages:inventory"},
        },
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://image.test") as client:
        yield SimpleNamespace(client=client, factory=factory, store=store, settings=settings)
    await engine.dispose()


async def enqueue(h, *, request_id=None, headers=None, **values):
    return await h.client.post(
        "/v1/registry-imports",
        headers=headers or HUMAN,
        json={"request_id": request_id or str(uuid4()), "name": "image", "reference": "alpine:3", **values},
    )


def claim_headers(channel=None, sequence=1):
    return {
        **WORKER,
        "X-Palimpsest-Worker-Channel": channel or str(uuid4()),
        "X-Palimpsest-Claim-Sequence": str(sequence),
    }


async def claim(h):
    response = await h.client.post("/v1/image-worker/claim", headers=claim_headers())
    assert response.status_code == 202, response.text
    value = response.json()
    return value, {**WORKER, "X-Palimpsest-Build-Lease": value["lease"]}


def path(job):
    return f"/v1/image-worker/jobs/{job['id']}"


async def stage(h, job, headers, payload, *, expected=None, size=None):
    return await h.client.put(
        path(job) + "/outputs/" + (expected or digest(payload)),
        content=payload,
        headers={**headers, "Content-Length": str(len(payload) if size is None else size)},
    )


def descriptor(*payloads):
    return {
        "layers": [{"digest": digest(value), "size_bytes": len(value)} for value in payloads],
        "source": {
            "root_digest": digest(b"root"),
            "manifest_digest": digest(b"manifest"),
            "config_digest": digest(b"config"),
        },
        "runtime": {
            "entrypoint": ["/bin/sh"],
            "cmd": ["-c", "echo actual"],
            "env": ["SECRET=not-in-public-get"],
            "working_dir": "/work",
            "user": "1000",
        },
    }


@pytest.mark.asyncio
async def test_authority_request_binding_visibility_and_sanitization(images):
    h = images
    assert (await enqueue(h, headers=WORKER)).status_code == 403
    for headers in ({}, HUMAN, {"X-Palimpsest-Build-Worker-Key": "wrong"}):
        assert (await h.client.post("/v1/image-worker/claim", headers=headers)).status_code == 401
    request_id = str(uuid4())
    first = await enqueue(h, request_id=request_id)
    repeat = await enqueue(h, request_id=request_id)
    assert first.status_code == repeat.status_code == 202
    assert first.json()["id"] == repeat.json()["id"]
    assert (await enqueue(h, request_id=request_id, reference="ubuntu:24.04")).status_code == 409
    assert (
        await h.client.get("/v1/image-jobs/" + first.json()["id"], headers={**HUMAN, "X-Project": "beta"})
    ).status_code == 404
    assert (await h.client.get("/v1/image-jobs", headers={**HUMAN, "X-Project": "beta"})).json() == []
    assert "reference" not in first.text and "lease" not in first.text and "project_id" not in first.text
    job, headers = await claim(h)
    assert job["request"]["reference"] == "docker.io/library/alpine:3"
    assert job["context"] is None and job["timeout_seconds"] <= 60
    assert (await h.client.post("/v1/image-worker/claim", headers=claim_headers())).status_code == 204
    assert (await h.client.get(path(job), headers={**WORKER, "X-Palimpsest-Build-Lease": "wrong"})).status_code == 409


@pytest.mark.asyncio
async def test_fresh_authority_revocation_never_admits(images, monkeypatch):
    async def revoked(token_info):
        raise HTTPException(status_code=403, detail="fresh human authority revoked")

    monkeypatch.setattr(service, "get_package_member_info", revoked)
    assert (await enqueue(images)).status_code == 403
    async with images.factory() as session:
        assert (await session.scalars(select(PalimpsestHubImageJob))).all() == []


@pytest.mark.parametrize(
    "reference",
    [
        "http://127.0.0.1/a",
        "localhost/a",
        "evil.example/a",
        "ghcr.io:444/a",
        "ghcr.io/a\n",
        "user:password@ghcr.io/a",
        "/etc/passwd",
        "ghcr.io/a?secret=x",
    ],
)
@pytest.mark.asyncio
async def test_registry_policy_rejects_unapproved_sources(images, reference):
    response = await enqueue(images, reference=reference)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_context_copy_descriptor_project_bounds_and_hash(images):
    h = images
    payload = context_tar()
    uploaded = await h.client.post(
        "/v1/build-contexts",
        headers={**HUMAN, "Content-Type": "application/x-tar", "X-Content-SHA256": digest(payload)},
        content=payload,
    )
    assert uploaded.status_code == 201, uploaded.text
    req = {
        "request_id": str(uuid4()),
        "name": "copy-image",
        "dockerfile": "FROM scratch\nCOPY hello.txt /hello.txt\n",
        "context_digest": digest(payload),
        "build_args": {"VALUE": "do-not-echo"},
    }
    denied = await h.client.post("/v1/dockerfile-builds", json=req, headers={**HUMAN, "X-Project": "beta"})
    assert denied.status_code == 404
    response = await h.client.post("/v1/dockerfile-builds", json=req, headers=HUMAN)
    assert response.status_code == 202 and "do-not-echo" not in response.text and "COPY" not in response.text
    job, headers = await claim(h)
    assert job["context"] == {"digest": digest(payload), "size_bytes": len(payload)}
    downloaded = await h.client.get(path(job) + "/inputs/" + digest(payload), headers=headers)
    assert downloaded.status_code == 200 and downloaded.content == payload
    assert (await h.client.get(path(job) + "/inputs/" + digest(b"other"), headers=headers)).status_code == 404
    async with h.factory() as session:
        assert await session.get(PalimpsestHubBuildContext, ("alpha", digest(payload)))
        refs = (
            await session.scalars(select(RuntimeBlobReference).where(RuntimeBlobReference.digest == digest(payload)))
        ).all()
        assert len(refs) == 2
    for bad, declared in [(context_tar("../escape"), None), (payload, digest(b"wrong"))]:
        failure = await h.client.post(
            "/v1/build-contexts",
            headers={**HUMAN, "Content-Type": "application/x-tar", "X-Content-SHA256": declared or digest(bad)},
            content=bad,
        )
        assert failure.status_code == 422
    too_large = await h.client.post(
        "/v1/build-contexts",
        headers={
            **HUMAN,
            "Content-Type": "application/x-tar",
            "X-Content-SHA256": digest(payload),
            "Content-Length": str(h.settings.palimpsest_hub_max_context_bytes + 1),
        },
        content=payload,
    )
    assert too_large.status_code == 413
    assert not list(h.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_multioutput_atomicity_integrity_terminal_retry_and_retention(images):
    h = images
    await enqueue(h)
    job, headers = await claim(h)
    a, b = b"hsqs-layer-a", b"hsqs-layer-b"
    assert (await stage(h, job, headers, a, expected=digest(b))).status_code == 422
    assert (await stage(h, job, headers, a, size=len(a) + 1)).status_code == 422
    assert (await stage(h, job, headers, b"not squashfs")).status_code == 422
    assert (await stage(h, job, headers, a)).status_code == 200
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubLayer))).all() == []
        assert await session.scalar(select(RuntimeBlobReference).where(RuntimeBlobReference.digest == digest(a)))
    incomplete = await h.client.post(path(job) + "/complete", json=descriptor(a, b), headers=headers)
    assert incomplete.status_code == 409
    assert (await stage(h, job, headers, b)).status_code == 200
    result = await h.client.post(path(job) + "/complete", json=descriptor(a, b), headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["result"] == descriptor(a, b)
    assert result.json()["output_digests"] == [digest(a), digest(b)]
    assert (
        await h.client.post(path(job) + "/complete", json=descriptor(a, b), headers=headers)
    ).json() == result.json()
    assert (await h.client.post(path(job) + "/complete", json=descriptor(b, a), headers=headers)).status_code == 409
    assert (await stage(h, job, headers, a)).status_code == 200
    assert (await stage(h, job, headers, a, expected=digest(b))).status_code == 422
    assert (await h.client.post(path(job) + "/fail", json={"error_code": "build_failed"}, headers=headers)).json()[
        "status"
    ] == "complete"
    public = await h.client.get("/v1/image-jobs/" + job["id"], headers=HUMAN)
    assert "not-in-public-get" not in public.text
    assert public.json()["result"]["runtime"]["env_names"] == ["SECRET"]
    async with h.factory() as session:
        first = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(a)))
        second = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(b)))
        assert first.parent_digest is None and second.parent_digest is None
        assert not first.is_published and first.config_json["base_image_digest"] is None
        assert second.config_json["instance_mode"] == "oci-layer"
    assert not list(h.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_expired_attempt_never_replays_and_terminal_fail_is_immutable(images):
    h = images
    await enqueue(h)
    job, headers = await claim(h)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubImageJob, job["id"])
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert (await stage(h, job, headers, b"hsqs-expired")).status_code == 409
    status = await h.client.get(path(job), headers=headers)
    assert status.json()["status"] == "error" and status.json()["error_code"] == "worker_interrupted"
    assert (await h.client.post("/v1/image-worker/claim", headers=claim_headers())).status_code == 204
    assert (await h.client.post(path(job) + "/fail", json={"error_code": "build_failed"}, headers=headers)).json()[
        "error_code"
    ] == "worker_interrupted"
    assert (
        await h.client.post(path(job) + "/complete", json=descriptor(b"hsqs-expired"), headers=headers)
    ).status_code == 409


@pytest.mark.asyncio
async def test_crossproject_compatible_bytes_receive_readonly_grant_not_metadata_overwrite(images):
    h = images
    a = b"hsqs-common"
    await enqueue(h)
    job, headers = await claim(h)
    assert (await stage(h, job, headers, a)).status_code == 200
    assert (await h.client.post(path(job) + "/complete", json=descriptor(a), headers=headers)).status_code == 200
    async with h.factory() as session:
        row = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(a)))
        row.name = "original-owner-name"
        row.config_json = {**row.config_json, "name": row.name}
        row.config_digest = compute_config_digest(row.config_json)
        original = row.config_digest
        await session.commit()
    await enqueue(h, headers={**HUMAN, "X-Project": "beta"}, name="foreign-request-name")
    other, other_headers = await claim(h)
    assert (await stage(h, other, other_headers, a)).status_code == 200
    assert (
        await h.client.post(path(other) + "/complete", json=descriptor(a), headers=other_headers)
    ).status_code == 200
    async with h.factory() as session:
        row = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(a)))
        assert row.project_id == "alpha" and row.name == "original-owner-name" and row.config_digest == original
        assert await session.get(PalimpsestHubLayerAccess, (digest(a), "beta"))


@pytest.mark.asyncio
async def test_descriptor_conflict_rolls_back_every_layer(images):
    h = images
    a, b = b"hsqs-first", b"hsqs-conflict"
    await enqueue(h)
    first, first_headers = await claim(h)
    await stage(h, first, first_headers, b)
    await h.client.post(path(first) + "/complete", json=descriptor(b), headers=first_headers)
    async with h.factory() as session:
        existing = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(b)))
        existing.arch = "aarch64"
        await session.commit()
    await enqueue(h)
    job, headers = await claim(h)
    await stage(h, job, headers, a)
    await stage(h, job, headers, b)
    response = await h.client.post(path(job) + "/complete", json=descriptor(a, b), headers=headers)
    assert response.status_code == 409
    async with h.factory() as session:
        assert (
            await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(a))) is None
        )
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert row.status == "building" and row.output_digests == []


@pytest.mark.asyncio
async def test_repeated_occurrences_and_shared_layers_preserve_contextual_graph(images):
    h = images
    a, b = b"hsqs-shared", b"hsqs-next"
    await enqueue(h)
    job, headers = await claim(h)
    await stage(h, job, headers, a)
    first = await h.client.post(path(job) + "/complete", json=descriptor(a, a), headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["output_digests"] == [digest(a), digest(a)]
    await enqueue(h)
    second, second_headers = await claim(h)
    await stage(h, second, second_headers, b)
    await stage(h, second, second_headers, a)
    result = await h.client.post(path(second) + "/complete", json=descriptor(b, a), headers=second_headers)
    assert result.status_code == 200, result.text
    assert result.json()["result"]["layers"] == descriptor(b, a)["layers"]
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubLayer))).all()
        assert len(rows) == 2 and all(row.parent_digest is None for row in rows)


@pytest.mark.asyncio
async def test_original_token_subject_change_is_rejected(images, monkeypatch):
    monkeypatch.setattr(
        service, "validate_token", lambda token, project: {"project_id": project, "user_id": "different-human"}
    )
    response = await enqueue(images)
    assert response.status_code == 403
    async with images.factory() as session:
        assert (await session.scalars(select(PalimpsestHubImageJob))).all() == []


@pytest.mark.asyncio
async def test_oversized_json_is_bounded_without_echoing_build_inputs(images):
    secret = "private-build-arg-" + "x" * (2 * 1024 * 1024)
    response = await images.client.post("/v1/dockerfile-builds", content=secret, headers=HUMAN)
    assert response.status_code == 413 and "private-build-arg" not in response.text
    response = await images.client.post(
        "/v1/dockerfile-builds",
        json={
            "request_id": str(uuid4()),
            "name": "invalid",
            "dockerfile": "private-dockerfile",
            "build_args": {"BAD ARG": "private-value"},
        },
        headers=HUMAN,
    )
    assert response.status_code == 422
    assert "private-dockerfile" not in response.text and "private-value" not in response.text


def symlink_context(links, *, write_through=False):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        info = tarfile.TarInfo("physical.txt")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"real"))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
        if write_through:
            info = tarfile.TarInfo("alias/injected.txt")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"evil"))
    return result.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "links,write_through,expected",
    [
        ([("alias", "physical.txt")], False, 201),
        ([("nested/alias", "../physical.txt")], False, 201),
        ([("alias", "../../outside")], False, 422),
        ([("alias", "/absolute")], False, 422),
        ([("alias", "physical.txt")], True, 422),
        ([("alias", "other"), ("other", "alias")], False, 422),
        ([("alias", "root/../outside"), ("root", ".")], False, 422),
    ],
)
async def test_context_symlinks_are_internal_and_never_write_ancestors(images, links, write_through, expected):
    payload = symlink_context(links, write_through=write_through)
    response = await images.client.post(
        "/v1/build-contexts",
        content=payload,
        headers={**HUMAN, "Content-Type": "application/x-tar", "X-Content-SHA256": digest(payload)},
    )
    assert response.status_code == expected, response.text
    assert not list(images.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_openapi_describes_bounded_actual_request_shapes(images):
    response = await images.client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    registry = paths["/v1/registry-imports"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert set(registry["required"]) == {"request_id", "name", "reference"}
    docker = paths["/v1/dockerfile-builds"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "context_digest" in docker["properties"] and "build_args" in docker["properties"]
    complete = paths["/v1/image-worker/jobs/{job_id}/complete"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert "root_digest" in complete["properties"]["source"]["properties"]
    assert "entrypoint" in complete["properties"]["runtime"]["properties"]
    context = paths["/v1/build-contexts"]["post"]["requestBody"]["content"]["application/x-tar"]["schema"]
    assert context == {"type": "string", "format": "binary"}


@pytest.mark.asyncio
async def test_deadline_during_completion_rolls_back_partial_registrations(images, monkeypatch):
    h = images
    a, b = b"hsqs-time-first", b"hsqs-time-second"
    await enqueue(h)
    job, headers = await claim(h)
    await stage(h, job, headers, a)
    await stage(h, job, headers, b)
    actual_run = service._run_blocking
    future = datetime.now(UTC) + timedelta(hours=2)

    async def slow_verification(function, *args, **kwargs):
        result = await actual_run(function, *args, **kwargs)
        if function == h.store.inspect_file and args[0] == h.store.blob_path(digest(b)):
            monkeypatch.setattr(service, "now", lambda: future)
        return result

    monkeypatch.setattr(service, "_run_blocking", slow_verification)
    response = await h.client.post(path(job) + "/complete", json=descriptor(a, b), headers=headers)
    assert response.status_code == 409
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubLayer))).all() == []
    status = await h.client.get(path(job), headers=headers)
    assert status.json()["status"] == "error" and status.json()["output_digests"] == []
    assert (await h.client.post("/v1/image-worker/claim", headers=claim_headers())).status_code == 204


@pytest.mark.asyncio
async def test_image_lost_claim_responses_and_delayed_post_are_authoritatively_settled(images):
    h = images
    channel = str(uuid4())
    idle = claim_headers(channel)
    assert (await h.client.post("/v1/image-worker/claim", headers=idle)).status_code == 204
    queued = await enqueue(h)
    recovery = await h.client.get("/v1/image-worker/claim", headers=idle)
    assert recovery.status_code == 200 and recovery.headers["Cache-Control"] == "no-store"
    assert recovery.json() == {"channel": channel, "protocol": "image", "sequence": 1, "claim": None}
    assert (await h.client.post("/v1/image-worker/claim", headers=idle)).status_code == 204
    fenced = claim_headers(channel, 2)
    assert (await h.client.get("/v1/image-worker/claim", headers=fenced)).json()["claim"] is None
    assert (await h.client.post("/v1/image-worker/claim", headers=fenced)).status_code == 204
    next_headers = claim_headers(channel, 3)
    accepted = await h.client.post("/v1/image-worker/claim", headers=next_headers)
    job = accepted.json()
    assert accepted.status_code == 202 and job["id"] == queued.json()["id"]
    recovery = await h.client.get("/v1/image-worker/claim", headers=next_headers)
    assert recovery.json()["claim"] == {"id": job["id"], "lease": job["lease"]}
    assert (await h.client.post("/v1/image-worker/claim", headers=next_headers)).json() == job
    h.settings.palimpsest_hub_build_timeout_seconds = 3600
    async with h.factory() as session, session.begin():
        row = await session.get(PalimpsestHubImageJob, job["id"])
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    recovered = await h.client.get("/v1/image-worker/claim", headers=next_headers)
    assert recovered.json()["claim"]["lease"] == job["lease"]
    status = await h.client.get(path(job), headers={**WORKER, "X-Palimpsest-Build-Lease": job["lease"]})
    assert status.json()["error_code"] == "worker_interrupted"
    for sequence in range(4, 9):
        assert (
            await h.client.post("/v1/image-worker/claim", headers=claim_headers(channel, sequence))
        ).status_code == 204
    from palimpsest_hub.models import PalimpsestWorkerClaim

    async with h.factory() as session:
        cursors = (await session.scalars(select(PalimpsestWorkerClaim))).all()
        assert len(cursors) == 1 and cursors[0].sequence == 8 and cursors[0].result_json is None
    assert (await h.client.post("/v1/image-worker/claim", headers=next_headers)).status_code == 409
    assert (await h.client.post("/v1/image-worker/claim", headers=claim_headers())).status_code == 204


@pytest.mark.asyncio
async def test_recovery_waits_for_inflight_image_claim_transaction(images, monkeypatch):
    h = images
    await enqueue(h)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.context_for

    async def delayed(session, job):
        entered.set()
        await release.wait()
        return await original(session, job)

    monkeypatch.setattr(service, "context_for", delayed)
    headers = claim_headers()
    posting = asyncio.create_task(h.client.post("/v1/image-worker/claim", headers=headers))
    recovering = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        recovering = asyncio.create_task(h.client.get("/v1/image-worker/claim", headers=headers))
        await asyncio.sleep(0.05)
        assert not recovering.done()
        release.set()
        accepted, recovered = await asyncio.gather(posting, recovering)
        assert accepted.status_code == 202 and recovered.status_code == 200
        assert recovered.json()["claim"] == {key: accepted.json()[key] for key in ("id", "lease")}
    finally:
        release.set()
        await asyncio.gather(posting, *([recovering] if recovering else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_image_claim_recovery_requires_worker_key_and_identity(images):
    for method in (images.client.get, images.client.post):
        assert (await method("/v1/image-worker/claim", headers=HUMAN)).status_code == 401
        assert (await method("/v1/image-worker/claim", headers=WORKER)).status_code == 422


@pytest.mark.asyncio
async def test_publication_intent_binds_existing_stages_without_exposing_pending_runtime(images):
    h = images
    await enqueue(h)
    job, headers = await claim(h)
    a, b = b"hsqs-publication-a", b"hsqs-publication-b"
    assert (await stage(h, job, headers, a)).status_code == 200
    initial = await h.client.get(path(job), headers=headers)
    assert initial.headers["cache-control"] == "no-store"
    receipt = initial.json()["publication"]
    assert receipt["result_digest"] is None
    assert receipt["staged_outputs"] == [{"digest": digest(a), "size_bytes": len(a)}]
    expiry = receipt["deadline"]
    planned = descriptor(a, b)
    bound = await h.client.post(path(job) + "/publication-intent", headers=headers, json=planned)
    assert bound.status_code == 200, bound.text
    assert bound.json()["result"] is None
    assert bound.json()["publication"]["result_digest"] == compute_config_digest(planned)
    assert bound.json()["publication"]["deadline"] == expiry
    assert (
        await h.client.post(path(job) + "/publication-intent", headers=headers, json=planned)
    ).json() == bound.json()
    changed = descriptor(b, a)
    assert (await h.client.post(path(job) + "/publication-intent", headers=headers, json=changed)).status_code == 409
    assert (await h.client.post(path(job) + "/complete", headers=headers, json=changed)).status_code == 409
    assert (await stage(h, job, headers, b"hsqs-unbound-extra")).status_code == 409
    public = await h.client.get("/v1/image-jobs/" + job["id"], headers=HUMAN)
    assert public.json()["result"] is None
    assert "publication" not in public.json() and "not-in-public-get" not in public.text
    assert (await stage(h, job, headers, b)).status_code == 200
    ready = (await h.client.get(path(job), headers=headers)).json()["publication"]
    assert ready["deadline"] == expiry
    assert ready["staged_outputs"] == planned["layers"]
    completed = await h.client.post(path(job) + "/complete", headers=headers, json=planned)
    assert completed.status_code == 200, completed.text
    assert completed.json()["publication"]["result_digest"] == compute_config_digest(planned)
    assert completed.json()["result"] == planned


@pytest.mark.asyncio
async def test_publication_receipt_waits_for_inflight_output_commit(images):
    h = images
    await enqueue(h)
    job, headers = await claim(h)
    payload = b"hsqs-receipt-fence"
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_body():
        yield payload[:4]
        entered.set()
        await release.wait()
        yield payload[4:]

    upload = asyncio.create_task(
        h.client.put(
            path(job) + "/outputs/" + digest(payload),
            content=slow_body(),
            headers={**headers, "Content-Length": str(len(payload))},
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    status = asyncio.create_task(h.client.get(path(job), headers=headers))
    try:
        await asyncio.sleep(0.05)
        assert not status.done(), "absence receipt escaped the in-flight upload lock"
    finally:
        release.set()
    assert (await asyncio.wait_for(upload, 5)).status_code == 200
    receipt = (await asyncio.wait_for(status, 5)).json()["publication"]
    assert receipt["staged_outputs"] == [{"digest": digest(payload), "size_bytes": len(payload)}]


@pytest.mark.asyncio
async def test_expired_publication_intent_never_extends_lease_or_publishes(images):
    h = images
    await enqueue(h)
    job, headers = await claim(h)
    payload = b"hsqs-expiring-publication"
    planned = descriptor(payload)
    assert (await h.client.post(path(job) + "/publication-intent", headers=headers, json=planned)).status_code == 200
    async with h.factory() as session:
        row = await session.get(PalimpsestHubImageJob, job["id"])
        expired = datetime.now(UTC) - timedelta(seconds=1)
        row.lease_expires_at = expired
        await session.commit()
    assert (await h.client.post(path(job) + "/publication-intent", headers=headers, json=planned)).status_code == 409
    receipt = (await h.client.get(path(job), headers=headers)).json()
    assert receipt["status"] == "error" and receipt["result"] is None
    async with h.factory() as session:
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert row.lease_expires_at.replace(tzinfo=UTC) == expired
        assert row.output_digests == []
        assert (await session.scalars(select(PalimpsestHubLayer))).all() == []
