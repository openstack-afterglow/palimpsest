"""Tenant, byte-integrity and atomic multi-output image-job boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from palimpsest_hub.api import builds, hub, image_jobs, published_images
from palimpsest_hub.image_models import PalimpsestHubBuildContext, PalimpsestHubImageJob
from palimpsest_hub.models import (
    Base,
    PalimpsestHubLayer,
    PalimpsestHubLayerAccess,
    RuntimeBlobReference,
)
from palimpsest_hub.services import image_jobs as service
from palimpsest_hub.services import published_images as publications
from palimpsest_hub.services.blob_references import blob_referenced
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import HubStoreError, LocalPathBlobStore

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
    for module in (hub, builds, image_jobs, service, publications, published_images):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(image_jobs.router, prefix="/v1")
    app.include_router(published_images.router, prefix="/v1")
    app.include_router(hub.router, prefix="/v1")

    async def identity(request: Request):
        if request.headers.get("X-Identity") != "human":
            raise HTTPException(status_code=403, detail="human native authority required")
        admin = request.headers.get("X-Target") is not None
        return {
            "project_id": request.headers.get("X-Project", "alpha"),
            "user_id": request.headers.get("X-User", "caller"),
            "package_actions": {"packages:read", "packages:write", "packages:inventory"},
            "is_package_system_admin": admin,
            **({"target_project_id": request.headers["X-Target"]} if admin else {}),
            "token": "disposable-native-token:" + request.headers.get("X-User", "caller"),
        }

    app.dependency_overrides[hub._legacy_writer] = identity
    app.dependency_overrides[hub._legacy_inventory] = identity
    app.dependency_overrides[hub._legacy_download] = identity
    app.dependency_overrides[hub._project_delete] = identity

    async def refresh(token_info):
        return token_info

    monkeypatch.setattr(service, "get_package_member_info", refresh)
    monkeypatch.setattr(
        service,
        "validate_token",
        lambda token, project: {
            "project_id": project,
            "user_id": token.partition(":")[2] or "caller",
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
        json={
            "request_id": request_id or str(uuid4()),
            "name": "image",
            "reference": "alpine:3",
            "os_family": "alpine",
            "os_version": "3",
            **values,
        },
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
    value = {
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
    from palimpsest_local.image_manifest import manifest_from_result

    value["manifest"] = manifest_from_result(value, os_family="alpine", os_version="3", source_kind="registry")
    return value


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
    assert first.json()["visibility"] == "private"
    assert (await enqueue(h, request_id=request_id, visibility="internal")).status_code == 409
    assert (await enqueue(h, request_id=request_id, headers={**HUMAN, "X-User": "other"})).status_code == 409
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
        "os_family": "unknown",
        "os_version": "unknown",
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
        assert await session.get(PalimpsestHubBuildContext, ("alpha", digest(payload), "caller"))
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
async def test_build_context_unknown_commit_outcome_retains_unregistered_cas(images, monkeypatch):
    """Inject an unknown COMMIT outcome without a real delayed server commit."""
    h = images
    payload = context_tar(payload=b"context with unknown metadata outcome")
    expected = digest(payload)
    failure = OperationalError("COMMIT", {}, TimeoutError("unknown COMMIT outcome"), connection_invalidated=True)

    async def unknown_commit(session):
        # Only the context and its retention-reference transaction may fail.
        context = await session.get(PalimpsestHubBuildContext, ("alpha", expected, "caller"))
        assert context is not None and context.size_bytes == len(payload)
        assert await session.scalar(select(RuntimeBlobReference).where(RuntimeBlobReference.digest == expected))
        raise failure

    monkeypatch.setattr(AsyncSession, "commit", unknown_commit)
    with pytest.raises(OperationalError) as raised:
        await h.client.post(
            "/v1/build-contexts",
            headers={**HUMAN, "Content-Type": "application/x-tar", "X-Content-SHA256": expected},
            content=payload,
        )
    assert raised.value is failure
    async with h.factory() as session:
        # Neither missing context metadata nor missing references proves COMMIT rollback.
        assert await session.get(PalimpsestHubBuildContext, ("alpha", expected, "caller")) is None
        assert not await blob_referenced(session, expected)
    retained = h.store.blob_path(expected).read_bytes()
    assert retained == payload and digest(retained) == expected
    assert not list(h.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_build_context_precommit_failure_discards_published_unreferenced_cas(images, monkeypatch):
    h = images
    payload = context_tar(payload=b"verified context before reference failure")
    expected = digest(payload)

    async def fail_reference(*_args):
        assert h.store.blob_path(expected).read_bytes() == payload
        raise RuntimeError("reference transaction failed before commit")

    monkeypatch.setattr(service, "retain_blob", fail_reference)
    with pytest.raises(RuntimeError, match="reference transaction failed before commit"):
        await h.client.post(
            "/v1/build-contexts",
            headers={**HUMAN, "Content-Type": "application/x-tar", "X-Content-SHA256": expected},
            content=payload,
        )
    assert not h.store.exists(expected)
    assert not list(h.store.uploads_dir.iterdir())
    async with h.factory() as session:
        assert await session.get(PalimpsestHubBuildContext, ("alpha", expected, "caller")) is None
        assert not await blob_referenced(session, expected)


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
async def test_staged_output_unknown_commit_outcome_retains_unregistered_cas(images, monkeypatch):
    """Inject an unknown COMMIT outcome without a real delayed server commit."""
    h = images
    assert (await enqueue(h)).status_code == 202
    job, headers = await claim(h)
    payload = b"hsqs-verified-output-with-unknown-commit"
    expected = digest(payload)
    failure = OperationalError("COMMIT", {}, TimeoutError("unknown COMMIT outcome"), connection_invalidated=True)

    async def unknown_commit(session):
        # Claim/setup is committed already; only the output-reference commit may fail.
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert any(item["digest"] == expected for item in row.staged_outputs)
        assert await session.scalar(select(RuntimeBlobReference).where(RuntimeBlobReference.digest == expected))
        raise failure

    monkeypatch.setattr(AsyncSession, "commit", unknown_commit)
    with pytest.raises(OperationalError) as raised:
        await stage(h, job, headers, payload)
    assert raised.value is failure
    async with h.factory() as session:
        # A fresh empty reference read cannot rule out the in-flight COMMIT landing later.
        assert not await blob_referenced(session, expected)
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert row.status == "building" and row.staged_outputs == []
    retained = h.store.blob_path(expected).read_bytes()
    assert retained == payload and digest(retained) == expected
    # This route's anonymous receive spool is not a resumable upload.
    assert not list(h.store.uploads_dir.iterdir())


@pytest.mark.asyncio
async def test_staged_output_precommit_failure_discards_published_unreferenced_cas(images, monkeypatch):
    h = images
    assert (await enqueue(h)).status_code == 202
    job, headers = await claim(h)
    payload = b"hsqs-published-before-reference-failure"
    expected = digest(payload)

    async def fail_reference(*_args):
        assert h.store.blob_path(expected).read_bytes() == payload
        raise RuntimeError("reference transaction failed before commit")

    monkeypatch.setattr(service, "retain_blob", fail_reference)
    with pytest.raises(RuntimeError, match="reference transaction failed before commit"):
        await stage(h, job, headers, payload)
    assert not h.store.exists(expected)
    assert not list(h.store.uploads_dir.iterdir())
    async with h.factory() as session:
        assert not await blob_referenced(session, expected)
        row = await session.get(PalimpsestHubImageJob, job["id"])
        assert row.staged_outputs == []


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
async def test_crossproject_identical_payloads_keep_separate_private_image_authority(images):
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
        assert await session.get(PalimpsestHubLayerAccess, (digest(a), "beta")) is None
    other_image = await publications.resolve_image(
        h.factory,
        other["id"],
        {"project_id": "beta", "user_id": "caller"},
    )
    assert other_image["project_id"] == "beta" and other_image["visibility"] == "private"
    assert other_image["manifest"]["layers"][0]["digest"] == digest(a)


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
async def test_deadline_during_completion_rolls_back_partial_registrations(images, monkeypatch):
    h = images
    a, b = b"hsqs-time-first", b"hsqs-time-second"
    await enqueue(h)
    job, headers = await claim(h)
    await stage(h, job, headers, a)
    await stage(h, job, headers, b)
    actual_run = publications.run_blocking
    future = datetime.now(UTC) + timedelta(hours=2)
    verified_second = []

    async def slow_verification(function, *args, **kwargs):
        result = await actual_run(function, *args, **kwargs)
        if function == h.store.inspect_file and args[0] == h.store.blob_path(digest(b)):
            verified_second.append(True)
            monkeypatch.setattr(service, "now", lambda: future)
        return result

    monkeypatch.setattr(publications, "run_blocking", slow_verification)
    response = await h.client.post(path(job) + "/complete", json=descriptor(a, b), headers=headers)
    assert verified_second == [True]
    assert response.status_code == 409
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubLayer))).all() == []
        from palimpsest_hub.image_models import PalimpsestHubLayerNode, PalimpsestHubPublishedImage

        assert (await session.scalars(select(PalimpsestHubLayerNode))).all() == []
        assert (await session.scalars(select(PalimpsestHubPublishedImage))).all() == []
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


def publication_manifest(*payloads, source_kind="local"):
    from palimpsest_local.image_manifest import manifest_from_result

    return manifest_from_result(
        descriptor(*payloads),
        os_family="alpine",
        os_version="3",
        source_kind=source_kind,
        metadata={"label": "descriptive-project-value"},
    )


async def upload_publication_payload(h, payload, headers=None):
    return await h.client.put(
        "/v1/published-images/blobs/" + digest(payload),
        content=payload,
        headers={**(headers or HUMAN), "Content-Type": "application/octet-stream", "Content-Length": str(len(payload))},
    )


async def publish_manifest(h, manifest, *, request_id=None, headers=None, name="published", visibility="private"):
    return await h.client.post(
        "/v1/published-images",
        headers=headers or HUMAN,
        json={"request_id": request_id or str(uuid4()), "name": name, "manifest": manifest, "visibility": visibility},
    )


@pytest.mark.asyncio
async def test_publication_registers_single_parent_many_child_and_shared_payload(images):
    a, b, c = b"hsqsroot", b"hsqscommon", b"hsqsother-parent"
    for payload in (a, b, c):
        assert (await upload_publication_payload(images, payload)).status_code == 200
    first = await publish_manifest(images, publication_manifest(a, b))
    branch = await publish_manifest(images, publication_manifest(a, c))
    other_parent = await publish_manifest(images, publication_manifest(c, b))
    assert first.status_code == branch.status_code == other_parent.status_code == 201
    one, two, three = first.json(), branch.json(), other_parent.json()
    assert one["nodes"][0]["id"] == two["nodes"][0]["id"]
    assert one["leaf_id"] != three["leaf_id"]
    assert one["nodes"][-1]["digest"] == three["nodes"][-1]["digest"] == digest(b)
    children = await images.client.get("/v1/layer-nodes/" + one["nodes"][0]["id"] + "/children", headers=HUMAN)
    assert {node["id"] for node in children.json()["items"]} == {one["leaf_id"], two["leaf_id"]}
    ancestors = await images.client.get("/v1/layer-nodes/" + one["leaf_id"] + "/ancestors", headers=HUMAN)
    assert [node["id"] for node in ancestors.json()["items"]] == [one["nodes"][0]["id"]]
    async with images.factory() as session:
        assert len((await session.scalars(select(PalimpsestHubLayer))).all()) == 3
    private = await publications.resolve_image(images.factory, one["id"], {"project_id": "alpha", "user_id": "caller"})
    assert private["manifest"]["runtime"]["env"] == ["SECRET=not-in-public-get"]
    assert "not-in-public-get" not in first.text
    assert private["manifest"]["metadata"] == one["manifest"]["metadata"] == {"label": "descriptive-project-value"}
    assert one["manifest"]["runtime"]["env_names"] == ["SECRET"]
    assert private["manifest_digest"] == one["manifest_digest"]
    assert private["leaf_id"] == one["leaf_id"]
    assert (
        await images.client.get(
            "/v1/published-images/" + one["id"], headers={**HUMAN, "X-Project": "beta", "X-User": "other"}
        )
    ).status_code == 404
    assert (
        await images.client.get(
            "/v1/layer-nodes/" + one["leaf_id"], headers={**HUMAN, "X-Project": "beta", "X-User": "other"}
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_publication_revoked_during_payload_copy_never_commits(images, monkeypatch):
    payload = b"hsqsrevoked-during-copy"
    assert (await upload_publication_payload(images, payload)).status_code == 200
    state = {"revoked": False}
    original_copy = publications._publish_no_replace

    def copy_then_revoke(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        state["revoked"] = True
        return result

    async def current(token_info):
        if state["revoked"]:
            raise HTTPException(status_code=403, detail="membership removed")
        return token_info

    monkeypatch.setattr(publications, "_publish_no_replace", copy_then_revoke)
    monkeypatch.setattr(service, "get_package_member_info", current)
    response = await publish_manifest(images, publication_manifest(payload))
    assert response.status_code == 403
    assert state["revoked"] is True
    async with images.factory() as session:
        assert (await session.scalars(select(publications.PalimpsestHubPublishedImage))).all() == []


@pytest.mark.asyncio
async def test_admin_target_publication_revalidates_original_token_scope(images, monkeypatch):
    admin = {**HUMAN, "X-Target": "beta", "X-User": "operator"}
    seen = []

    async def revalidate(token_info):
        seen.append(token_info["project_id"])
        if token_info["project_id"] != "alpha":
            raise HTTPException(status_code=403, detail="PROJECT_SCOPE_MISMATCH")
        return token_info

    monkeypatch.setattr(service, "revalidate_package_admin", revalidate)
    payload = b"hsqsadmin-target-payload"
    assert (await upload_publication_payload(images, payload, headers=admin)).status_code == 200
    response = await publish_manifest(images, publication_manifest(payload), headers=admin)
    assert response.status_code == 201, response.text
    assert seen and set(seen) == {"alpha"}
    async with images.factory() as session:
        [row] = (await session.scalars(select(publications.PalimpsestHubPublishedImage))).all()
    assert row.project_id == "beta"


@pytest.mark.asyncio
async def test_publication_requires_actual_project_bytes_and_immutable_request(images):
    payload = b"hsqspublication"
    manifest = publication_manifest(payload)
    assert (await publish_manifest(images, manifest)).status_code == 404
    assert (await upload_publication_payload(images, payload)).status_code == 200
    # A CAS hit belonging to alpha cannot be adopted by beta merely by digest.
    assert (await publish_manifest(images, manifest, headers={**HUMAN, "X-Project": "beta"})).status_code == 404
    request_id = str(uuid4())
    first = await publish_manifest(images, manifest, request_id=request_id)
    retry = await publish_manifest(images, manifest, request_id=request_id)
    assert first.status_code == retry.status_code == 201 and first.json()["id"] == retry.json()["id"]
    assert (await publish_manifest(images, manifest, request_id=request_id, name="changed")).status_code == 409
    bad = {**manifest, "layers": [{**manifest["layers"][0], "parent_id": digest(b"false")}]}
    assert (await publish_manifest(images, bad)).status_code == 422
    arbitrary = await images.client.post(
        "/v1/published-images",
        headers=HUMAN,
        json={
            "request_id": str(uuid4()),
            "name": "bad",
            "manifest": manifest,
            "storage_binding": {"path": "/caller/chosen", "kind": "manila"},
        },
    )
    assert arbitrary.status_code == 422


@pytest.mark.asyncio
async def test_worker_completion_and_local_push_use_same_graph_catalog(images):
    payload = b"hsqsshared-worker"
    await enqueue(images)
    job, headers = await claim(images)
    assert (await stage(images, job, headers, payload)).status_code == 200
    completed = await images.client.post(path(job) + "/complete", headers=headers, json=descriptor(payload))
    assert completed.status_code == 200, completed.text
    image_id = completed.json()["published_image_id"]
    worker_image = (await images.client.get("/v1/published-images/" + image_id, headers=HUMAN)).json()
    retained = images.store.blob_path(digest(payload)).stat()
    child = b"hsqslocal-child-only"
    assert (await upload_publication_payload(images, child)).status_code == 200
    local_image = await publish_manifest(images, publication_manifest(payload, child))
    assert local_image.status_code == 201
    assert local_image.json()["nodes"][0]["id"] == worker_image["leaf_id"]
    assert local_image.json()["nodes"][-1]["parent_id"] == worker_image["leaf_id"]
    current = images.store.blob_path(digest(payload)).stat()
    assert (current.st_ino, current.st_mtime_ns) == (retained.st_ino, retained.st_mtime_ns)
    async with images.factory() as session:
        assert len((await session.scalars(select(PalimpsestHubLayer))).all()) == 2


@pytest.mark.asyncio
async def test_manila_publication_staging_accepts_other_authorized_projects(images):
    images.settings.palimpsest_hub_publication_manila_share_id = str(uuid4())
    headers = {**HUMAN, "X-Project": "other-project", "X-User": "other"}
    assert (await upload_publication_payload(images, b"hsqsauthorized", headers=headers)).status_code == 200
    assert images.store.blob_path(digest(b"hsqsauthorized")).read_bytes() == b"hsqsauthorized"


def manila_mount(tmp_path, monkeypatch):
    """Model kernel mount records while retaining real no-follow descriptor IO."""
    root = tmp_path / "share"
    root.mkdir()
    values = SimpleNamespace(
        palimpsest_hub_publication_manila_share_id=str(uuid4()),
        palimpsest_hub_publication_manila_export="192.0.2.10:/volumes/share",
        palimpsest_hub_publication_manila_root=str(root),
    )
    metadata = root.stat()
    device = f"{os.major(metadata.st_dev)}:{os.minor(metadata.st_dev)}"
    kernel = {
        "mountinfo": f"21 1 {device} / {root} rw - ceph 192.0.2.10:/volumes/share rw\n",
        "mnt_id": 21,
    }
    original = Path.read_text

    def read_kernel(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return kernel["mountinfo"]
        if str(path).startswith("/proc/self/fdinfo/"):
            return f"mnt_id:\t{kernel['mnt_id']}\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(publications, "get_settings", lambda: values)
    monkeypatch.setattr(Path, "read_text", read_kernel)
    return root, values, kernel


def test_manila_destination_requires_actual_exact_export_mount(tmp_path, monkeypatch):
    root, values, kernel = manila_mount(tmp_path, monkeypatch)
    store = LocalPathBlobStore(tmp_path / "private-cas")
    destination, binding = publications.storage_destination(store)
    try:
        assert destination.root == root and binding["kind"] == "manila"
        destination.check_identity()
    finally:
        destination.close()
    kernel["mountinfo"] = kernel["mountinfo"].replace(" / ", " /volumes/share ", 1)
    destination, other_binding = publications.storage_destination(store)
    destination.close()
    assert other_binding == binding
    kernel["mountinfo"] = kernel["mountinfo"].replace(" /volumes/share ", " /volumes/share/descendant ")
    with pytest.raises(HTTPException):
        publications.storage_destination(store)
    kernel["mountinfo"] = kernel["mountinfo"].replace(" /volumes/share/descendant ", " /volumes/share ")
    nested = root / "nested"
    nested.mkdir()
    values.palimpsest_hub_publication_manila_root = str(nested)
    with pytest.raises(HTTPException) as rejected:
        publications.storage_destination(store)
    assert rejected.value.status_code == 503
    values.palimpsest_hub_publication_manila_root = str(root)
    kernel["mountinfo"] = kernel["mountinfo"].replace(" - ceph ", " - ext4 ")
    with pytest.raises(HTTPException):
        publications.storage_destination(store)


def test_immutable_publication_copies_bytes_and_never_repairs_conflicts(tmp_path):
    from palimpsest_hub.services import published_images as publication

    source, destination = LocalPathBlobStore(tmp_path / "cas"), LocalPathBlobStore(tmp_path / "share")
    payload = b"hsqsactual-publication"
    source.blobs_dir.mkdir(parents=True)
    source.blob_path(digest(payload)).write_bytes(payload)
    verified = source.inspect_file(source.blob_path(digest(payload)), max_bytes=len(payload))
    publication._publish_no_replace(source.blob_path(digest(payload)), destination, verified)
    target = destination.blob_path(digest(payload))
    assert target.read_bytes() == payload
    publication._publish_no_replace(source.blob_path(digest(payload)), destination, verified)
    target.chmod(0o600)
    target.write_bytes(b"corrupted")
    with pytest.raises(HubStoreError):
        publication._publish_no_replace(source.blob_path(digest(payload)), destination, verified)
    assert target.read_bytes() == b"corrupted"


@pytest.mark.parametrize("overmount", ["stacked", "blobs", "blobs/sha256"])
def test_manila_destination_rejects_stacked_and_child_mounts(tmp_path, monkeypatch, overmount):
    root, _, kernel = manila_mount(tmp_path, monkeypatch)
    target = root if overmount == "stacked" else root / overmount
    kernel["mountinfo"] += f"22 21 8:2 / {target} rw - ext4 /dev/unrelated rw\n"
    with pytest.raises(HTTPException) as rejected:
        publications.storage_destination(LocalPathBlobStore(tmp_path / "private-cas"))
    assert rejected.value.status_code == 503


@pytest.mark.parametrize("mismatch", ["ancestor", "mount-id", "device", "missing-fdinfo"])
def test_manila_mount_record_must_own_open_root(tmp_path, monkeypatch, mismatch):
    root, _, kernel = manila_mount(tmp_path, monkeypatch)
    if mismatch == "ancestor":
        # The textual Ceph child still exists, but a newer ancestor mount hides it.
        kernel["mountinfo"] += f"22 1 8:2 / {root.parent} rw - ext4 /dev/other rw\n"
        kernel["mnt_id"] = 22
    elif mismatch == "mount-id":
        kernel["mnt_id"] = 22
    elif mismatch == "device":
        device = f"{os.major(root.stat().st_dev)}:{os.minor(root.stat().st_dev)}"
        kernel["mountinfo"] = kernel["mountinfo"].replace(device, "8:999")
    else:
        kernel["mnt_id"] = "not-a-mount-id"
    with pytest.raises(HTTPException) as rejected:
        publications.storage_destination(LocalPathBlobStore(tmp_path / "cas"))
    assert rejected.value.status_code == 503
    assert not (root / "blobs").exists()


@pytest.mark.parametrize("drift", ["root", "target", "mount-id"])
def test_manila_publication_pins_write_directories_and_rejects_binding_drift(tmp_path, monkeypatch, drift):
    root, _, kernel = manila_mount(tmp_path, monkeypatch)
    source = LocalPathBlobStore(tmp_path / "cas")
    source.blobs_dir.mkdir(parents=True)
    payload = b"hsqs-pinned-write"
    source_path = source.blob_path(digest(payload))
    source_path.write_bytes(payload)
    verified = source.inspect_file(source_path, max_bytes=len(payload))
    destination, _ = publications.storage_destination(source)
    original = os.open
    changed = False

    def move_binding(path, flags, *args, **kwargs):
        nonlocal changed
        if path == source_path and not changed:
            changed = True
            if drift == "root":
                root.rename(tmp_path / "detached-share")
                root.mkdir()
            elif drift == "target":
                (root / "blobs" / "sha256").rename(root / "blobs" / "detached")
                (root / "blobs" / "sha256").mkdir()
            else:
                kernel["mnt_id"] = 22
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", move_binding)
    try:
        with pytest.raises(HubStoreError):
            publications._publish_no_replace(source_path, destination, verified)
        assert changed
        assert not destination.blob_path(digest(payload)).exists()
        assert not list(root.rglob(".publication-*"))
        assert not list((tmp_path / "detached-share").rglob(".publication-*"))
    finally:
        destination.close()


@pytest.mark.asyncio
async def test_publication_receive_reserves_shared_project_capacity_before_disk(images, monkeypatch):
    from palimpsest_hub.models import PalimpsestHubUpload

    entered = [asyncio.Event() for _ in range(4)]
    release = asyncio.Event()
    payload = b"hsqs-bounded-receive"
    starts = []
    original = images.store.start_upload

    def start_upload(upload_id):
        starts.append(upload_id)
        return original(upload_id)

    monkeypatch.setattr(images.store, "start_upload", start_upload)

    async def body(index):
        entered[index].set()
        await release.wait()
        yield payload

    async def send(index):
        return await images.client.put(
            "/v1/published-images/blobs/" + digest(payload),
            headers={**HUMAN, "Content-Type": "application/octet-stream", "Content-Length": str(len(payload))},
            content=body(index),
        )

    tasks = [asyncio.create_task(send(index)) for index in range(4)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 5)
        assert len(starts) == 4
        async with images.factory() as session:
            assert len((await session.scalars(select(PalimpsestHubUpload))).all()) == 4
        streamed = False

        async def denied_body():
            nonlocal streamed
            streamed = True
            yield payload

        denied = await images.client.put(
            "/v1/published-images/blobs/" + digest(payload),
            headers={**HUMAN, "Content-Type": "application/octet-stream", "Content-Length": str(len(payload))},
            content=denied_body(),
        )
        assert denied.status_code == 429
        assert not streamed and len(starts) == 4
        # Legacy admission observes the same rows, not a separate four-slot pool.
        with pytest.raises(HTTPException) as legacy:
            await hub.start_upload(hub.HubUploadStartRequest(), {"project_id": "alpha", "user_id": "caller"})
        assert legacy.value.status_code == 429
        assert (await upload_publication_payload(images, payload, {**HUMAN, "X-Project": "beta"})).status_code == 200
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(response, httpx.Response) and response.status_code == 200 for response in results)
    async with images.factory() as session:
        assert (await session.scalars(select(PalimpsestHubUpload))).all() == []
    assert not list(images.store.uploads_dir.iterdir())
    assert (await upload_publication_payload(images, payload)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "size", "digest"])
async def test_publication_receive_releases_admission_on_failure(images, failure):
    from palimpsest_hub.models import PalimpsestHubUpload

    payload = b"hsqs-release-admission"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def body():
        entered.set()
        await release.wait()
        yield payload

    expected = digest(b"different") if failure == "digest" else digest(payload)
    size = len(payload) + (1 if failure == "size" else 0)
    task = asyncio.create_task(
        images.client.put(
            "/v1/published-images/blobs/" + expected,
            headers={**HUMAN, "Content-Type": "application/octet-stream", "Content-Length": str(size)},
            content=body(),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        assert (await task).status_code == 422
    async with images.factory() as session:
        assert (await session.scalars(select(PalimpsestHubUpload))).all() == []
    assert not list(images.store.uploads_dir.iterdir())
    assert (await upload_publication_payload(images, payload)).status_code == 200


@pytest.mark.parametrize("worker", [False, True])
def test_historical_complete_jobs_do_not_advertise_unregistered_images(worker):
    legacy = descriptor(b"hsqshistorical")
    legacy.pop("manifest")
    row = SimpleNamespace(
        id=str(uuid4()),
        name="historical-image",
        kind="registry",
        status="complete",
        visibility="private",
        request_id=str(uuid4()),
        request_digest=digest(b"old-request"),
        output_digests=[digest(b"hsqshistorical")],
        result_json=legacy,
        error_code=None,
        created_at=datetime.now(UTC),
        started_at=None,
        completed_at=datetime.now(UTC),
        lease_expires_at=None,
        staged_outputs=[],
    )
    value = service.serialize_job(row, worker=worker)
    assert value["status"] == "complete" and value["published_image_id"] is None
    assert value["output_digests"] == row.output_digests
    assert value["result"]["source"] == legacy["source"]


@pytest.mark.asyncio
async def test_sdk_publication_request_cannot_manufacture_worker_image_alias(images):
    payload = b"hsqsrequest-collision"
    request_id = str(uuid4())
    canonical = descriptor(payload)["manifest"]
    assert (await upload_publication_payload(images, payload)).status_code == 200
    published = await publish_manifest(images, canonical, request_id=request_id, name="image")
    assert published.status_code == 201
    assert (await enqueue(images, request_id=request_id)).status_code == 202
    job, headers = await claim(images)
    assert job["id"] != published.json()["id"]
    assert (await stage(images, job, headers, payload)).status_code == 200
    rejected = await images.client.post(path(job) + "/complete", headers=headers, json=descriptor(payload))
    assert rejected.status_code == 409
    status = await images.client.get(path(job), headers=headers)
    assert status.json()["status"] == "building" and status.json()["published_image_id"] is None
    assert (await images.client.get("/v1/published-images/" + job["id"], headers=HUMAN)).status_code == 404
    assert (await images.client.get("/v1/published-images/" + published.json()["id"], headers=HUMAN)).status_code == 200


@pytest.mark.asyncio
async def test_publication_admission_counts_legacy_and_package_uploads(images):
    from palimpsest_hub.models import PackageNamespace, PackageUpload, PalimpsestHubUpload

    async with images.factory() as session:
        session.add(PackageNamespace(project_id="alpha", namespace="alpha", project_name="alpha"))
        await session.flush()
        for _ in range(2):
            session.add(PalimpsestHubUpload(id=uuid4().hex, project_id="alpha", created_by="caller"))
        for _ in range(2):
            session.add(
                PackageUpload(
                    id=uuid4().hex,
                    project_id="alpha",
                    package="test",
                    owner_user_id="caller",
                    resource="package",
                    request={},
                    status="uploading",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await session.commit()
    streamed = False

    async def body():
        nonlocal streamed
        streamed = True
        yield b"hsqs-shared-capacity"

    rejected = await images.client.put(
        "/v1/published-images/blobs/" + digest(b"hsqs-shared-capacity"),
        headers={**HUMAN, "Content-Type": "application/octet-stream", "Content-Length": "20"},
        content=body(),
    )
    assert rejected.status_code == 429 and not streamed
    assert not images.store.uploads_dir.exists()


def test_manila_pinned_publication_is_immutable_and_descriptor_relative(tmp_path, monkeypatch):
    root, _, _ = manila_mount(tmp_path, monkeypatch)
    source = LocalPathBlobStore(tmp_path / "cas")
    source.blobs_dir.mkdir(parents=True)
    payload = b"hsqs-pinned-actual-bytes"
    source_path = source.blob_path(digest(payload))
    source_path.write_bytes(payload)
    verified = source.inspect_file(source_path, max_bytes=len(payload))
    destination, _ = publications.storage_destination(source)
    try:
        publications._publish_no_replace(source_path, destination, verified)
        target = destination.blob_path(digest(payload))
        assert target.read_bytes() == payload
        identity = target.stat()
        publications._publish_no_replace(source_path, destination, verified)
        assert target.stat().st_ino == identity.st_ino
        target.chmod(0o600)
        target.write_bytes(b"conflicting-bytes")
        with pytest.raises(HubStoreError):
            publications._publish_no_replace(source_path, destination, verified)
        assert target.read_bytes() == b"conflicting-bytes"
        assert not list(root.rglob(".publication-*"))
    finally:
        destination.close()


@pytest.mark.parametrize("component", ["blobs", "sha256"])
def test_manila_pinned_publication_never_writes_through_symlink(tmp_path, monkeypatch, component):
    root, _, _ = manila_mount(tmp_path, monkeypatch)
    source = LocalPathBlobStore(tmp_path / "cas")
    source.blobs_dir.mkdir(parents=True)
    payload = b"hsqs-no-link-redirection"
    source_path = source.blob_path(digest(payload))
    source_path.write_bytes(payload)
    verified = source.inspect_file(source_path, max_bytes=len(payload))
    destination, _ = publications.storage_destination(source)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    if component == "blobs":
        (root / "blobs").symlink_to(outside, target_is_directory=True)
    else:
        (root / "blobs").mkdir()
        (root / "blobs" / "sha256").symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises((OSError, HubStoreError)):
            publications._publish_no_replace(source_path, destination, verified)
        assert list(outside.iterdir()) == []
    finally:
        destination.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("layer_count", [1, 2])
async def test_publication_rejects_target_replacement_between_layer_copies(images, tmp_path, monkeypatch, layer_count):
    from palimpsest_hub.image_models import PalimpsestHubLayerNode, PalimpsestHubPublishedImage

    payloads = (b"hsqs-first-pinned-layer", b"hsqs-second-pinned-layer")[:layer_count]
    for payload in payloads:
        assert (await upload_publication_payload(images, payload)).status_code == 200
    root, _, _ = manila_mount(tmp_path, monkeypatch)
    from palimpsest_hub.services import ceph_scoped_store

    # This existing regression exercises pinned directory custody, independently
    # of the namespace helper's xattr tests.
    def pinned_destination(base, actor, visibility, **kwargs):
        destination, binding = base
        destination.binding = {**binding, "kind": "manila-namespace-v1"}
        return destination

    monkeypatch.setattr(ceph_scoped_store, "scoped_destination", pinned_destination)
    original = publications._publish_no_replace
    copied = []

    def replace_after_first(source, destination, verified):
        original(source, destination, verified)
        copied.append(verified.blob_digest)
        if len(copied) == 1:
            (root / "blobs" / "sha256").rename(root / "blobs" / "detached")
            (root / "blobs" / "sha256").mkdir()

    monkeypatch.setattr(publications, "_publish_no_replace", replace_after_first)
    response = await publish_manifest(images, publication_manifest(*payloads))
    assert response.status_code == 409
    assert copied == [digest(payloads[0])]
    assert list((root / "blobs" / "sha256").iterdir()) == []
    assert (root / "blobs" / "detached" / digest(payloads[0])[7:]).read_bytes() == payloads[0]
    assert not list(root.rglob(".publication-*"))
    async with images.factory() as session:
        assert (await session.scalars(select(PalimpsestHubLayer))).all() == []
        assert (await session.scalars(select(PalimpsestHubLayerNode))).all() == []
        assert (await session.scalars(select(PalimpsestHubPublishedImage))).all() == []


@pytest.mark.asyncio
async def test_publication_admission_drains_sql_release_under_repeated_cancellation(images, monkeypatch):
    from palimpsest_hub.models import PalimpsestHubUpload

    body_entered, body_release = asyncio.Event(), asyncio.Event()
    release_entered, release_allowed = asyncio.Event(), asyncio.Event()
    original_delete = AsyncSession.delete
    release_cancelled = []

    async def blocked_delete(session, row):
        if isinstance(row, PalimpsestHubUpload):
            release_entered.set()
            try:
                await release_allowed.wait()
            except asyncio.CancelledError:
                release_cancelled.append(True)
                raise
        await original_delete(session, row)

    monkeypatch.setattr(AsyncSession, "delete", blocked_delete)
    actor = {
        "project_id": "alpha",
        "user_id": "caller",
        "token": "disposable-native-token",
        "package_actions": {"packages:read", "packages:write", "packages:inventory"},
    }

    async def admitted():
        async with published_images._upload_admission(images.factory, images.store, "alpha", actor):
            body_entered.set()
            await body_release.wait()

    task = asyncio.create_task(admitted())
    try:
        await asyncio.wait_for(body_entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(release_entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and release_cancelled == []
        async with images.factory() as session:
            assert len((await session.scalars(select(PalimpsestHubUpload))).all()) == 1
    finally:
        body_release.set()
        release_allowed.set()
        results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert release_cancelled == []
    async with images.factory() as session:
        assert (await session.scalars(select(PalimpsestHubUpload))).all() == []
    # Capacity is usable immediately, not only after the 24-hour idle timeout.
    async with published_images._upload_admission(images.factory, images.store, "alpha", actor):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("publisher", ["local", "worker"])
@pytest.mark.parametrize("visibility", ["private", "internal", "public"])
@pytest.mark.parametrize(
    "project,user,authenticated",
    [
        ("alpha", "caller", True),
        ("alpha", "other", True),
        ("beta", "caller", True),
        ("beta", "other", True),
        ("alpha", "caller", False),
    ],
)
async def test_image_visibility_access_matrix(images, publisher, visibility, project, user, authenticated):
    payload = b"hsqs-real-matrix-payload"
    if publisher == "local":
        assert (await upload_publication_payload(images, payload)).status_code == 200
        response = await publish_manifest(images, publication_manifest(payload), visibility=visibility)
        assert response.status_code == 201, response.text
        image = response.json()
    else:
        admitted = await enqueue(images, visibility=visibility)
        assert admitted.status_code == 202, admitted.text
        job, headers = await claim(images)
        assert job["request"]["visibility"] == visibility
        assert (await stage(images, job, headers, payload)).status_code == 200
        completed = await images.client.post(path(job) + "/complete", headers=headers, json=descriptor(payload))
        assert completed.status_code == 200, completed.text
        image = (await images.client.get("/v1/published-images/" + job["id"], headers=HUMAN)).json()
    headers = {"X-Project": project, "X-User": user, **(HUMAN if authenticated else {})}
    allowed = authenticated and (
        visibility == "public"
        or (visibility == "private" and user == "caller")
        or (visibility == "internal" and project == "alpha")
    )
    expected = 200 if allowed else (404 if authenticated else 403)
    image_id, node_id = image["id"], image["leaf_id"]
    detail = await images.client.get("/v1/published-images/" + image_id, headers=headers)
    assert detail.status_code == expected
    node = await images.client.get(
        "/v1/layer-nodes/" + node_id, params={"image_id": image_id, "project_id": "alpha"}, headers=headers
    )
    assert node.status_code == expected
    blob = await images.client.get("/v1/published-images/" + image_id + "/blobs/" + digest(payload), headers=headers)
    assert blob.status_code == expected
    listing = await images.client.get("/v1/published-images", headers=headers)
    if authenticated:
        assert [item["id"] for item in listing.json()["items"]] == ([image_id] if allowed else [])
    if allowed:
        assert detail.json()["visibility"] == visibility
        assert ("created_by" in detail.json()) == (user == "caller")
        assert ("created_by" in listing.json()["items"][0]) == (user == "caller")
        assert blob.content == payload and "not-in-public-get" not in detail.text
        resolved = await publications.resolve_image(images.factory, image_id, {"project_id": project, "user_id": user})
        assert resolved["manifest"]["runtime"]["env"] == ["SECRET=not-in-public-get"]
    elif authenticated:
        with pytest.raises(HTTPException) as rejected:
            await publications.resolve_image(images.factory, image_id, {"project_id": project, "user_id": user})
        assert rejected.value.status_code == 404
    # Even obsolete project grants cannot expose new publication payload rows.
    async with images.factory() as session:
        layer = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(payload))
        )
        assert layer.kind == "image-payload" and layer.project_id == "alpha"
        assert (await session.scalars(select(PalimpsestHubLayerAccess))).all() == []
        session.add(PalimpsestHubLayerAccess(blob_digest=digest(payload), project_id=project, created_by=user))
        await session.commit()
    if authenticated:
        assert (await images.client.get("/v1/layers/" + digest(payload), headers=headers)).status_code == 404
        assert (await images.client.get("/v1/layers/" + digest(payload) + "/blob", headers=headers)).status_code == 404
        assert (
            await images.client.patch(
                "/v1/layers/" + digest(payload),
                json={"name": "unauthorized-rename"},
                headers={**headers, "If-Match": layer.config_digest},
            )
        ).status_code == 404
        assert (await images.client.delete("/v1/layers/" + digest(payload), headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_personal_stage_context_and_request_cannot_be_adopted_by_project_peer(images):
    payload = b"hsqs-unpublished-personal-stage"
    other = {**HUMAN, "X-User": "other"}
    assert (await upload_publication_payload(images, payload)).status_code == 200
    assert (await publish_manifest(images, publication_manifest(payload), headers=other)).status_code == 404
    context = context_tar(payload=b"private Dockerfile input")
    uploaded = await images.client.post(
        "/v1/build-contexts",
        content=context,
        headers={
            **HUMAN,
            "Content-Type": "application/x-tar",
            "X-Content-SHA256": digest(context),
            "Content-Length": str(len(context)),
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    build = {
        "request_id": str(uuid4()),
        "name": "personal",
        "dockerfile": "FROM scratch",
        "context_digest": digest(context),
        "os_family": "alpine",
        "os_version": "3",
    }
    assert (await images.client.post("/v1/dockerfile-builds", json=build, headers=other)).status_code == 404
    admitted = await enqueue(images)
    assert (await images.client.get("/v1/image-jobs/" + admitted.json()["id"], headers=other)).status_code == 404
    assert (await images.client.get("/v1/image-jobs", headers=other)).json() == []


@pytest.mark.asyncio
async def test_visible_graph_hides_private_branches_and_blocks_borrowed_declassification(images):
    root, public_child, private_child = b"hsqs-branch-root", b"hsqs-public-child", b"hsqs-private-child"
    for payload in (root, public_child, private_child):
        assert (await upload_publication_payload(images, payload)).status_code == 200
    public = (await publish_manifest(images, publication_manifest(root, public_child), visibility="public")).json()
    private = (await publish_manifest(images, publication_manifest(root, private_child))).json()
    other = {**HUMAN, "X-User": "other"}
    visible = await images.client.get("/v1/layer-nodes/" + public["nodes"][0]["id"] + "/children", headers=other)
    assert [item["id"] for item in visible.json()["items"]] == [public["leaf_id"]]
    assert (await images.client.get("/v1/layer-nodes/" + private["leaf_id"], headers=other)).status_code == 404
    internal_bytes = b"hsqs-foreign-internal"
    assert (await upload_publication_payload(images, internal_bytes)).status_code == 200
    internal = await publish_manifest(images, publication_manifest(internal_bytes), visibility="internal")
    assert internal.status_code == 201
    borrowed = publication_manifest(internal_bytes)
    assert (await publish_manifest(images, borrowed, headers=other, visibility="public")).status_code == 404
    assert (await publish_manifest(images, borrowed, headers=other, visibility="private")).status_code == 404
    assert (await publish_manifest(images, borrowed, headers=other, visibility="internal")).status_code == 201
    # Creating an internal reference does not turn its borrower into the
    # unrestricted owner of the original payload on a later publication.
    for visibility in ("public", "private"):
        assert (await publish_manifest(images, borrowed, headers=other, visibility=visibility)).status_code == 404
    request_id = str(uuid4())
    expanded = await publish_manifest(images, borrowed, request_id=request_id, visibility="public")
    assert expanded.status_code == 201
    assert (await publish_manifest(images, borrowed, request_id=request_id, visibility="private")).status_code == 409


@pytest.mark.asyncio
async def test_extension_preserves_server_private_runtime_and_derives_parent_chain(images):
    root, child = b"hsqs-extension-base", b"hsqs-extension-child"
    assert (await upload_publication_payload(images, root)).status_code == 200
    # Public availability of identical bytes must not erase the scope of the
    # explicitly selected internal base's private runtime/provenance.
    assert (
        await publish_manifest(images, publication_manifest(root), name="public-bytes", visibility="public")
    ).status_code == 201
    base = (await publish_manifest(images, publication_manifest(root), visibility="internal")).json()
    other = {**HUMAN, "X-User": "other"}
    queried = await images.client.get("/v1/published-images/" + base["id"], headers=other)
    assert queried.status_code == 200
    assert queried.json()["manifest"]["runtime"]["env_names"] == ["SECRET"]
    assert "env" not in queried.json()["manifest"]["runtime"]
    assert (await upload_publication_payload(images, child, headers=other)).status_code == 200
    body = {
        "request_id": str(uuid4()),
        "name": "extended",
        "base_image_id": base["id"],
        "layers": [
            {
                "digest": digest(child),
                "size_bytes": len(child),
                "id": digest(b"untrusted-id"),
                "parent_id": digest(b"untrusted-parent"),
            }
        ],
        "visibility": "internal",
        "metadata": {"label": "extension-label"},
    }
    response = await images.client.post("/v1/published-images/extensions", json=body, headers=other)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["nodes"][-1]["parent_id"] == base["leaf_id"]
    assert result["nodes"][-1]["id"] != digest(b"untrusted-id")
    assert "not-in-public-get" not in response.text
    assert result["manifest"]["source"] == {"kind": "local"}
    exact = await publications.resolve_image(images.factory, result["id"], {"project_id": "alpha", "user_id": "other"})
    assert exact["manifest"]["runtime"]["env"] == ["SECRET=not-in-public-get"]
    assert exact["manifest"]["metadata"] == {"label": "extension-label"}
    assert exact["manifest"]["source"] == {
        "kind": "local",
        "metadata": {
            "base_image_id": base["id"],
            "base_manifest_digest": base["manifest_digest"],
        },
    }
    assert (await images.client.post("/v1/published-images/extensions", json=body, headers=other)).json()[
        "id"
    ] == result["id"]
    for visibility in ("private", "public"):
        denied = await images.client.post(
            "/v1/published-images/extensions",
            json={**body, "request_id": str(uuid4()), "visibility": visibility},
            headers=other,
        )
        assert denied.status_code == 404
        chained = await images.client.post(
            "/v1/published-images/extensions",
            json={**body, "base_image_id": result["id"], "request_id": str(uuid4()), "visibility": visibility},
            headers=other,
        )
        assert chained.status_code == 404
    from palimpsest_hub.image_models import PalimpsestHubImageMembership

    async with images.factory() as session:
        members = (
            await session.scalars(
                select(PalimpsestHubImageMembership)
                .where(PalimpsestHubImageMembership.image_id == result["id"])
                .order_by(PalimpsestHubImageMembership.ordinal)
            )
        ).all()
        assert [member.reuse_projects_json for member in members] == [["alpha"], []]
    denied = await images.client.post(
        "/v1/published-images/extensions",
        json={**body, "request_id": str(uuid4())},
        headers={**other, "X-Project": "beta"},
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_worker_publication_cannot_change_accepted_visibility_after_claim(images):
    payload = b"hsqs-worker-visibility-binding"
    assert (await enqueue(images, visibility="private")).status_code == 202
    job, headers = await claim(images)
    assert (await stage(images, job, headers, payload)).status_code == 200
    async with images.factory() as session:
        row = await session.get(PalimpsestHubImageJob, job["id"])
        row.visibility = "public"
        await session.commit()
    response = await images.client.post(path(job) + "/complete", headers=headers, json=descriptor(payload))
    assert response.status_code == 409
    async with images.factory() as session:
        from palimpsest_hub.image_models import PalimpsestHubImageMembership, PalimpsestHubPublishedImage

        assert (await session.scalars(select(PalimpsestHubPublishedImage))).all() == []
        assert (await session.scalars(select(PalimpsestHubImageMembership))).all() == []


def _namespace_base(tmp_path, monkeypatch):
    from palimpsest_hub.services import ceph_scoped_store as scopes

    root = tmp_path / "qualified-ceph-mount"
    root.mkdir(exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)

    def identity(descriptor):
        return 7, os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino

    monkeypatch.setattr(scopes, "_identity", identity)
    monkeypatch.setattr(scopes, "_mount_filesystem", lambda fd, fs_name: None)
    monkeypatch.setattr(publications, "_fd_identity", identity)
    base = SimpleNamespace(
        root=root,
        fd=fd,
        identity=identity(fd),
        exported_path="/volumes/share",
        check_identity=lambda: None,
        close=lambda: os.close(base.fd),
    )
    return (
        scopes,
        base,
        {
            "kind": "manila",
            "share_id": "12345678-1234-1234-1234-123456789abc",
            "export": "192.0.2.10:6789:/volumes/share",
        },
    )


def test_scope_labels_bind_exact_user_project_and_share_without_case_folding():
    from palimpsest_hub.services.ceph_scoped_store import namespace_for, scope_for

    share = "12345678-1234-1234-1234-123456789abc"
    actor = {"user_id": "User-A", "project_id": "Project-A"}
    private = scope_for(actor, "private", share)
    assert private != scope_for({**actor, "user_id": "user-a"}, "private", share)
    assert private == scope_for({**actor, "project_id": "other"}, "private", share)
    assert scope_for(actor, "internal", share) == scope_for({**actor, "user_id": "other"}, "internal", share)
    assert scope_for(actor, "public", share) == "public"
    assert namespace_for(share, private) != namespace_for("12345678-1234-1234-1234-123456789abd", private)


def test_namespace_layout_is_set_on_empty_fresh_scope_before_payloads(tmp_path, monkeypatch):
    import json

    scopes, base, binding = _namespace_base(tmp_path, monkeypatch)
    actor = {"project_id": "project", "user_id": "user"}
    namespace = scopes.namespace_for(binding["share_id"], scopes.scope_for(actor, "private", binding["share_id"]))
    mutations = []

    def set_layout(fd, name, value):
        assert os.listdir(fd) == []
        mutations.append((name, value))

    monkeypatch.setattr(scopes.os, "setxattr", set_layout, raising=False)
    monkeypatch.setattr(
        scopes.os,
        "getxattr",
        lambda fd, name: json.dumps(
            {
                "pool_name": "data",
                "pool_namespace": namespace,
                "inheritance": "@set",
            }
        ).encode(),
        raising=False,
    )
    store = scopes.scoped_destination((base, binding), actor, "private", "cephfs", "data")
    try:
        assert mutations == [("ceph.dir.layout.pool", b"data"), ("ceph.dir.layout.pool_namespace", namespace.encode())]
        assert store.binding["kind"] == "manila-namespace-v1"
        assert store.binding["export"] == binding["export"] + "/cas/" + store.binding["scope_key"]
        assert os.listdir(store.fd) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "wrong_layout",
    [{}, {"pool_name": "data", "pool_namespace": "wrong"}, {"pool_name": "wrong", "pool_namespace": "expected"}],
)
def test_existing_namespace_missing_or_wrong_layout_is_never_repaired(tmp_path, monkeypatch, wrong_layout):
    import json
    from unittest.mock import Mock

    scopes, base, binding = _namespace_base(tmp_path, monkeypatch)
    actor = {"project_id": "project", "user_id": "user"}
    scope = scopes.scope_for(actor, "private", binding["share_id"])
    (base.root / "cas" / scope).mkdir(parents=True)
    mutation = Mock(side_effect=AssertionError("must not rewrite an existing layout"))
    monkeypatch.setattr(scopes.os, "setxattr", mutation, raising=False)
    monkeypatch.setattr(scopes.os, "getxattr", lambda fd, name: json.dumps(wrong_layout).encode(), raising=False)
    try:
        with pytest.raises(HubStoreError, match="layout"):
            scopes.scoped_destination((base, binding), actor, "private", "cephfs", "data", create=False)
        mutation.assert_not_called()
        os.fstat(base.fd)  # Failure retains the caller's base descriptor.
    finally:
        base.close()


def test_existing_scope_lookup_never_creates_missing_directories(tmp_path, monkeypatch):
    from unittest.mock import Mock

    scopes, base, binding = _namespace_base(tmp_path, monkeypatch)
    mutation = Mock(side_effect=AssertionError("existing-only lookup cannot mkdir"))
    monkeypatch.setattr(scopes.os, "mkdir", mutation)
    try:
        with pytest.raises(FileNotFoundError):
            scopes.scoped_destination(
                (base, binding), {"user_id": "user", "project_id": "project"}, "private", "cephfs", "data", create=False
            )
        mutation.assert_not_called()
    finally:
        base.close()


def test_file_layout_is_checked_independently_of_correct_parent_layout(tmp_path, monkeypatch):
    import json
    import stat

    scopes, base, binding = _namespace_base(tmp_path, monkeypatch)
    actor = {"user_id": "user", "project_id": "project"}
    scope = scopes.scope_for(actor, "private", binding["share_id"])
    namespace = scopes.namespace_for(binding["share_id"], scope)
    monkeypatch.setattr(scopes.os, "setxattr", lambda *args: None, raising=False)

    def layout(fd, name):
        return json.dumps(
            {
                "pool_name": "data",
                "pool_namespace": "foreign-namespace" if stat.S_ISREG(os.fstat(fd).st_mode) else namespace,
                "inheritance": "@set",
            }
        ).encode()

    monkeypatch.setattr(scopes.os, "getxattr", layout, raising=False)
    store = scopes.scoped_destination((base, binding), actor, "private", "cephfs", "data")
    try:
        payload = store.root / "blobs" / "sha256" / ("a" * 64)
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"payload in wrong RADOS namespace")
        with pytest.raises(HubStoreError, match="layout"):
            store.check_payload_layout("sha256:" + "a" * 64)
        with pytest.raises(HubStoreError, match="layout"):
            store.check_payload_layout(payload)
    finally:
        store.close()


def test_cross_scope_same_hash_publication_copies_distinct_payload_inodes(tmp_path, monkeypatch):
    import json

    scopes, base, binding = _namespace_base(tmp_path, monkeypatch)
    payload = b"hsqs-one-payload-two-security-domains"
    source = tmp_path / "source"
    source.write_bytes(payload)
    paths = []
    for user in ("first", "second"):
        if paths:
            # Each successful store owns its base descriptor until close.
            base.fd = os.open(base.root, os.O_RDONLY | os.O_DIRECTORY)
            base.identity = scopes._identity(base.fd)
        actor = {"user_id": user, "project_id": "project"}
        namespace = scopes.namespace_for(binding["share_id"], scopes.scope_for(actor, "private", binding["share_id"]))
        monkeypatch.setattr(scopes.os, "setxattr", lambda *args: None, raising=False)
        monkeypatch.setattr(
            scopes.os,
            "getxattr",
            lambda fd, name, ns=namespace: json.dumps(
                {
                    "pool_name": "data",
                    "pool_namespace": ns,
                    "inheritance": "@set",
                }
            ).encode(),
            raising=False,
        )
        store = scopes.scoped_destination((base, binding), actor, "private", "cephfs", "data")
        try:
            publications._publish_pinned(
                source, store, SimpleNamespace(blob_digest=digest(payload), size_bytes=len(payload))
            )
            paths.append(store.blob_path(digest(payload)))
        finally:
            store.close()
    assert paths[0].read_bytes() == paths[1].read_bytes() == payload
    assert paths[0].stat().st_ino != paths[1].stat().st_ino


def test_publication_checks_new_file_namespace_before_writing_confidential_bytes(tmp_path, monkeypatch):
    root, _, _ = manila_mount(tmp_path, monkeypatch)
    source_store = LocalPathBlobStore(tmp_path / "private-stage")
    source = tmp_path / "secret.sqfs"
    source.write_bytes(b"hsqs-confidential-publication-bytes")
    verified = source_store.inspect_file(source, max_bytes=4096)
    destination, _ = publications.storage_destination(source_store)
    observed_sizes = []

    def reject_file_namespace(fd):
        observed_sizes.append(os.fstat(fd).st_size)
        assert observed_sizes == [0]
        raise HubStoreError("unexpected actual file namespace")

    destination.check_payload_layout = reject_file_namespace
    try:
        with pytest.raises(HubStoreError, match="unexpected actual file namespace"):
            publications._publish_no_replace(source, destination, verified)
        assert observed_sizes == [0]
        assert list((root / "blobs" / "sha256").iterdir()) == []
    finally:
        destination.close()


@pytest.mark.parametrize("options", ["rw,mds_namespace=cephfs", "rw,fs=cephfs", "rw,fs=cephfs,mds_namespace=cephfs"])
def test_namespace_mount_filesystem_qualifies_kernel_aliases_by_descriptor_mount_id(monkeypatch, options):
    from palimpsest_hub.services import ceph_scoped_store as scopes

    monkeypatch.setattr(scopes, "_identity", lambda fd: (7, 1, 2))
    # A pathname-similar unrelated mount must not supply the selected fs name.
    mountinfo = (
        "8 1 0:1 / /same-path rw - ceph 192.0.2.10:/share rw,fs=wrong\n"
        f"7 1 0:1 / /actual-path rw - ceph 192.0.2.10:/share {options}\n"
    )
    monkeypatch.setattr(scopes.Path, "read_text", lambda path: mountinfo)
    scopes._mount_filesystem(42, "cephfs")


@pytest.mark.parametrize(
    "options", ["rw", "rw,mds_namespace=wrong", "rw,fs=wrong", "rw,fs=cephfs,mds_namespace=wrong", "rw,mds_namespace="]
)
def test_namespace_mount_missing_wrong_or_contradictory_filesystem_is_rejected(monkeypatch, options):
    from palimpsest_hub.services import ceph_scoped_store as scopes

    monkeypatch.setattr(scopes, "_identity", lambda fd: (7, 1, 2))
    mountinfo = (
        f"7 1 0:1 / /actual rw - ceph 192.0.2.10:/share {options}\n"
        "8 1 0:1 / /misleading rw - ceph 192.0.2.10:/share rw,fs=cephfs\n"
    )
    monkeypatch.setattr(scopes.Path, "read_text", lambda path: mountinfo)
    with pytest.raises(HubStoreError, match="filesystem name"):
        scopes._mount_filesystem(42, "cephfs")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["os_family", "os_version"])
@pytest.mark.parametrize("value,status", [(" \t", 422), ("nul\x00os", 422), ("é" * 65, 422), ("é" * 64, 202)])
async def test_os_declarations_reject_unpublishable_values_before_worker_admission(images, field, value, status):
    response = await enqueue(images, **{field: value})
    assert response.status_code == status, response.text
    async with images.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubImageJob))).all()
        if status == 422:
            assert rows == []
        else:
            assert len(rows) == 1 and rows[0].request_json[field] == value
    offered = await images.client.post("/v1/image-worker/claim", headers=claim_headers())
    assert offered.status_code == (204 if status == 422 else 202), offered.text
