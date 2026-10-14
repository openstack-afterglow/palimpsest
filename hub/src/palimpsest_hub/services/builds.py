"""Hub-owned remote build admission, fixed-deadline fencing and CAS publication.

Only authenticated HTTP workers execute recipes. SQL, visibility decisions and
private artifact registration stay in the API; a terminal attempt is never replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import select
from starlette.responses import StreamingResponse

from palimpsest_hub.api.hub import (
    HubLayerMeta,
    _factory_or_503,
    _grant_layer_access,
    _locked_blob,
    _locked_file,
    _registration_conflicts,
    _run_blocking,
    _store_or_503,
    _visible_filter,
)
from palimpsest_hub.config import get_settings
from palimpsest_hub.models import PalimpsestHubBuild, PalimpsestHubLayer
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, HubStoreError
from palimpsest_hub.services.worker_leases import sql_mutex


def _now() -> datetime:
    return datetime.now(UTC)


def _deadline(job: PalimpsestHubBuild) -> datetime:
    if job.started_at is None:
        return datetime.min.replace(tzinfo=UTC)
    if job.lease_expires_at is not None:
        expiry = job.lease_expires_at
        return expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry.astimezone(UTC)
    started = job.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started + timedelta(seconds=get_settings().palimpsest_hub_build_timeout_seconds)


def _expire(job: PalimpsestHubBuild) -> bool:
    if job.status == "building" and _deadline(job) <= _now():
        job.status = "error"
        job.error_code = "worker_interrupted"
        job.completed_at = _now()
        # Keep the nonce so the original worker can settle an ambiguous response.
        return True
    return False


@asynccontextmanager
async def _global_lock(store):
    # CAS locks remain useful locally, but SQL owns the deployment-wide singleton.
    async with (
        _locked_file(store, lambda: store.acquire_package_lock("build-worker-global", blocking=False)),
        sql_mutex(_factory_or_503(), "build-api:single-flight") as session,
    ):
        yield session


async def _owned(session, build_id: str, lease: str) -> PalimpsestHubBuild:
    job = await session.get(PalimpsestHubBuild, build_id, with_for_update=True)
    if job is None:
        raise HTTPException(status_code=404, detail="build job not found")
    if not job.lease_owner or not hmac.compare_digest(job.lease_owner.encode(), lease.encode()):
        raise HTTPException(status_code=409, detail="build lease does not match")
    return job


async def _live(session, build_id: str, lease: str) -> PalimpsestHubBuild:
    job = await _owned(session, build_id, lease)
    if _expire(job):
        await session.commit()
    if job.status != "building":
        raise HTTPException(status_code=409, detail="build attempt is not active")
    return job


async def _inputs(session, store, job: PalimpsestHubBuild) -> list[PalimpsestHubLayer]:
    rows = []
    input_bytes = 0
    for digest in (job.base_digest, *job.layer_digests):
        row = await session.scalar(
            _visible_filter(
                select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest),
                {"project_id": job.project_id},
            )
        )
        if row is None or not store.exists(digest):
            raise HTTPException(status_code=409, detail="a build input is no longer available")
        if row.size_bytes <= 0 or row.size_bytes > get_settings().palimpsest_hub_max_blob_bytes:
            raise HTTPException(status_code=409, detail="invalid build input size")
        rows.append(row)
        input_bytes += row.size_bytes
        if input_bytes > get_settings().palimpsest_hub_max_blob_bytes:
            raise HTTPException(status_code=409, detail="total build inputs exceed worker byte limit")
    base, *layers = rows
    if base.kind != "cloud-image" or base.arch != "x86_64" or base.disk_format not in ("raw", "qcow2"):
        raise HTTPException(status_code=409, detail="invalid build base")
    previous = None
    for layer in layers:
        if (
            layer.kind != "squashfs"
            or layer.media_type != MEDIA_TYPE_LAYER_SQUASHFS
            or layer.parent_digest != previous
            or (previous is None and (layer.config_json or {}).get("base_image_digest") != base.blob_digest)
            or (
                (layer.config_json or {}).get("base_image_digest") is not None
                and (layer.config_json or {}).get("base_image_digest") != base.blob_digest
            )
        ):
            raise HTTPException(status_code=409, detail="invalid build input chain")
        previous = layer.blob_digest
    if "sha256:" + hashlib.sha256(job.recipe.encode()).hexdigest() != job.recipe_digest:
        raise HTTPException(status_code=409, detail="build recipe changed")
    return rows


async def claim_build() -> dict | None:
    store = _store_or_503()
    async with _global_lock(store) as session:
        active = (
            await session.scalars(
                select(PalimpsestHubBuild).where(PalimpsestHubBuild.status == "building").with_for_update()
            )
        ).all()
        for job in active:
            _expire(job)
        if any(job.status == "building" for job in active):
            await session.commit()
            return None
        queued = (
            await session.scalars(
                select(PalimpsestHubBuild)
                .where(PalimpsestHubBuild.status == "queued")
                .order_by(PalimpsestHubBuild.created_at, PalimpsestHubBuild.id)
                .with_for_update()
            )
        ).all()
        for job in queued:
            try:
                base, *layers = await _inputs(session, store, job)
            except HTTPException:
                job.status = "error"
                job.error_code = "build_failed"
                job.completed_at = _now()
                continue
            job.status = "building"
            job.started_at = _now()
            job.lease_owner = str(uuid4())
            deadline = job.started_at + timedelta(seconds=get_settings().palimpsest_hub_build_timeout_seconds)
            job.lease_expires_at = deadline
            result = {
                "id": job.id,
                "lease": job.lease_owner,
                "deadline": deadline.isoformat(),
                "timeout_seconds": max(1, math.ceil((deadline - _now()).total_seconds())),
                "name": job.name,
                "recipe": job.recipe,
                "recipe_digest": job.recipe_digest,
                "base": {
                    "digest": base.blob_digest,
                    "arch": base.arch,
                    "disk_format": base.disk_format,
                    "size_bytes": base.size_bytes,
                },
                "layers": [
                    {
                        "digest": row.blob_digest,
                        "media_type": row.media_type,
                        "parent_digest": row.parent_digest,
                        "base_image_digest": (row.config_json or {}).get("base_image_digest"),
                        "size_bytes": row.size_bytes,
                    }
                    for row in layers
                ],
                "max_output_bytes": get_settings().palimpsest_hub_max_blob_bytes,
            }
            await session.commit()
            return result
        await session.commit()
        return None


async def worker_status(build_id: str, lease: str) -> PalimpsestHubBuild:
    store = _store_or_503()
    async with _global_lock(store) as session:
        job = await _owned(session, build_id, lease)
        _expire(job)
        await session.commit()
        return job


async def download_input(build_id: str, lease: str, digest: str) -> StreamingResponse:
    store = _store_or_503()
    # No CAS lookup until both attempt authority and exact input membership pass.
    async with _global_lock(store) as session:
        job = await _live(session, build_id, lease)
        if digest not in (job.base_digest, *job.layer_digests):
            raise HTTPException(status_code=404, detail="build input not found")
        rows = await _inputs(session, store, job)
        row = next(row for row in rows if row.blob_digest == digest)
        size, deadline = row.size_bytes, _deadline(job)
    async with _locked_blob(store, digest):
        try:
            verified = await _run_blocking(store.inspect_file, store.blob_path(digest), max_bytes=size)
        except HubStoreError as exc:
            raise HTTPException(status_code=409, detail="build input bytes are unavailable") from exc
        if verified.blob_digest != digest or verified.size_bytes != size:
            raise HTTPException(status_code=409, detail="build input bytes changed")
        # Recheck after potentially lengthy verification, before returning any bytes.
        async with _global_lock(store) as session:
            job = await _live(session, build_id, lease)
            await _inputs(session, store, job)
        handle = store.open_read(digest)

    async def chunks():
        try:
            remaining = size
            while remaining:
                if _now() >= deadline:
                    raise RuntimeError("build input deadline expired")
                chunk = await _run_blocking(handle.read, min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("build input was truncated")
                remaining -= len(chunk)
                yield chunk
        finally:
            handle.close()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(size),
            "X-Content-Digest": digest,
            "Cache-Control": "no-store",
        },
    )


async def fail_build(build_id: str, lease: str, error_code: str) -> PalimpsestHubBuild:
    store = _store_or_503()
    async with _global_lock(store) as session:
        job = await _owned(session, build_id, lease)
        _expire(job)
        if job.status == "building":
            job.status = "error"
            job.error_code = error_code
            job.completed_at = _now()
        # Complete and error are immutable, including their original nonce.
        await session.commit()
        return job


def _squashfs_magic(path) -> bool:
    with path.open("rb") as handle:
        return handle.read(4) == b"hsqs"


async def upload_output(build_id: str, lease: str, request: Request, expected: str, size: int) -> PalimpsestHubBuild:
    store = _store_or_503()
    # One bounded staging file per attempt; duplicates wait rather than multiply disk use.
    async with _locked_file(store, lambda: store.acquire_package_lock("build-output-" + build_id, blocking=False)):
        async with _global_lock(store) as session:
            job = await _owned(session, build_id, lease)
            if job.status != "complete":
                job = await _live(session, build_id, lease)
                deadline = _deadline(job)
            else:
                if (job.output_digest, job.output_size_bytes) != (expected, size):
                    raise HTTPException(status_code=409, detail="completed build output conflicts")
                # Receipt retries cannot hold staging and its lock indefinitely.
                deadline = _now() + timedelta(seconds=get_settings().palimpsest_hub_build_timeout_seconds)
        staging_id = uuid4().hex
        try:
            await _run_blocking(store.start_upload, staging_id)
            # Terminal repeats still verify the submitted bytes, but cannot alter SQL/CAS.
            remaining = (deadline - _now()).total_seconds()
            try:
                async with asyncio.timeout(remaining):
                    total = 0
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > size:
                            raise HTTPException(status_code=413, detail="build output exceeds declared size")
                        # Unlike a bare to_thread, this drains writes on cancellation
                        # before the finally block unlinks private staging.
                        await _run_blocking(store.append_upload, staging_id, chunk)
            except TimeoutError as exc:
                await worker_status(build_id, lease)
                raise HTTPException(status_code=409, detail="build output transfer deadline expired") from exc
            except HubStoreError as exc:
                raise HTTPException(status_code=413, detail="build output exceeds declared size") from exc
            if total != size:
                raise HTTPException(status_code=422, detail="build output size mismatch")
            staged = store.upload_path(staging_id)
            finalized = await _run_blocking(store.inspect_file, staged, max_bytes=size)
            if finalized.blob_digest != expected or not await _run_blocking(_squashfs_magic, staged):
                raise HTTPException(status_code=422, detail="build output digest or SquashFS magic mismatch")
            # Digest lock spans durable CAS publication and the SQL reference commit.
            async with _locked_blob(store, expected), _global_lock(store) as session:
                job = await _owned(session, build_id, lease)
                if job.status == "complete":
                    if (job.output_digest, job.output_size_bytes) != (expected, size):
                        raise HTTPException(status_code=409, detail="completed build output conflicts")
                    return job
                job = await _live(session, build_id, lease)
                parent = job.layer_digests[-1] if job.layer_digests else None
                meta = HubLayerMeta(
                    name=job.name,
                    kind="squashfs",
                    parent_digest=parent,
                    base_image_digest=job.base_digest,
                    arch="x86_64",
                )
                existing = await session.scalar(
                    select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == expected)
                )
                if existing is not None and (
                    _registration_conflicts(existing, meta)
                    or existing.size_bytes != size
                    or existing.is_published
                    or existing.project_id is None
                ):
                    raise HTTPException(status_code=409, detail="output conflicts with an existing layer descriptor")
                await _run_blocking(store.publish_verified, staged, finalized)
                # A slow disk copy cannot extend execution authority.
                job = await _live(session, build_id, lease)
                if existing is not None:
                    await _grant_layer_access(
                        session, expected, {"project_id": job.project_id, "user_id": job.created_by}
                    )
                else:
                    config = {
                        "name": job.name,
                        "kind": "squashfs",
                        "ubuntu_base": None,
                        "python_version": None,
                        "parent_digest": parent,
                        "chain_id": None,
                        "blob_digest": expected,
                        "disk_format": None,
                        "arch": "x86_64",
                        "os_variant": None,
                        "base_image_digest": job.base_digest,
                    }
                    session.add(
                        PalimpsestHubLayer(
                            blob_digest=expected,
                            blob_md5=finalized.blob_md5,
                            size_bytes=size,
                            media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                            arch="x86_64",
                            config_digest=compute_config_digest(config),
                            parent_digest=parent,
                            name=job.name,
                            kind="squashfs",
                            config_json=config,
                            project_id=job.project_id,
                            is_published=False,
                            created_by=job.created_by,
                        )
                    )
                job.output_digest = expected
                job.output_size_bytes = size
                job.status = "complete"
                job.completed_at = _now()
                await session.commit()
                return job
        finally:
            await _run_blocking(store.abort_upload, staging_id)
