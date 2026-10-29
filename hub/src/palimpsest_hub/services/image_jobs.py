"""SQL-fenced, private, atomic multi-layer OCI intake publication.

CAS stages are durable references, not public layer records. Failed attempts retain
references for conservative GC; temporary upload files are always removed.
"""

from __future__ import annotations

import asyncio
import hmac
import math
import tarfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from starlette.responses import StreamingResponse

from palimpsest_hub.api.hub import (
    _discard_unregistered_blob,
    _factory_or_503,
    _grant_layer_access,
    _legacy_writer,
    _locked_blob,
    _locked_blobs,
    _locked_file,
    _run_blocking,
    _store_or_503,
)
from palimpsest_hub.auth import (
    get_package_member_info,
    package_target_project_id,
    revalidate_package_admin,
    validate_token,
)
from palimpsest_hub.config import get_settings
from palimpsest_hub.image_models import PalimpsestHubBuildContext, PalimpsestHubImageJob
from palimpsest_hub.models import PalimpsestHubLayer, exact_identity
from palimpsest_hub.services.blob_references import retain_blob
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, HubStoreError
from palimpsest_hub.services.worker_leases import sql_mutex


def now():
    return datetime.now(UTC)


def iso(value):
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def serialize_job(row, *, worker=False):
    result = row.result_json
    if result and not worker:
        runtime = result["runtime"]
        result = {
            "source": result["source"],
            "runtime": {
                "entrypoint": runtime["entrypoint"],
                "cmd": runtime["cmd"],
                "working_dir": runtime["working_dir"],
                "user": runtime["user"],
                "env_names": [item.partition("=")[0] for item in runtime["env"]],
            },
        }
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "request_id": row.request_id,
        "request_digest": row.request_digest,
        "output_digests": row.output_digests,
        "result": result,
        "error_code": row.error_code,
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "completed_at": iso(row.completed_at),
    }


def normalize_registry_reference(value: str, hosts=None) -> str:
    """Bound the transport authority without importing worker/client executors.

    The worker independently resolves this canonical reference using the existing
    anonymous OCI resolver. Controller images intentionally contain only Hub.
    """
    import re

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or not value.isascii()
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or "://" in value
        or "\\" in value
        or value.count("@") > 1
    ):
        raise HTTPException(status_code=422, detail="invalid registry reference")
    name_tag, separator, pin = value.partition("@")
    if separator and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", pin):
        raise HTTPException(status_code=422, detail="invalid registry digest")
    tag = None
    name = name_tag
    if name_tag.rfind(":") > name_tag.rfind("/"):
        name, tag = name_tag.rsplit(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise HTTPException(status_code=422, detail="invalid registry tag")
    first = name.partition("/")[0]
    explicit = "/" in name and ("." in first or ":" in first or first == "localhost" or first.startswith("["))
    authority, repository = name.split("/", 1) if explicit else ("docker.io", name)
    authority = authority.lower()
    allowed = hosts if hosts is not None else get_settings().palimpsest_hub_registry_hosts
    if authority not in allowed:
        raise HTTPException(status_code=422, detail="registry host is not allowed")
    if (
        not repository
        or len(repository) > 255
        or any(not re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", part) for part in repository.split("/"))
    ):
        raise HTTPException(status_code=422, detail="invalid registry repository")
    if authority == "docker.io" and "/" not in repository:
        repository = "library/" + repository
    if tag is None and not separator:
        tag = "latest"
    return (
        authority
        + "/"
        + repository
        + ((":" + tag) if tag is not None else "")
        + (("@" + pin.lower()) if separator else "")
    )


async def fresh_writer(token_info):
    if token_info.get("is_package_system_admin"):
        current = await revalidate_package_admin(token_info)
    else:
        original = await asyncio.to_thread(validate_token, token_info["token"], token_info["project_id"])
        if original.get("user_id") != token_info["user_id"] or original.get("project_id") != token_info["project_id"]:
            raise HTTPException(status_code=403, detail="image job subject identity changed")
        current = await get_package_member_info(token_info=original)
    await _legacy_writer(current)
    if (
        package_target_project_id(current) != package_target_project_id(token_info)
        or current["user_id"] != token_info["user_id"]
    ):
        raise HTTPException(status_code=403, detail="image job authority changed")
    return current


async def admit(kind, payload, token_info):
    settings = get_settings()
    if not settings.palimpsest_hub_build_worker_key.get_secret_value():
        raise HTTPException(status_code=503, detail="isolated image worker is not configured")
    project = package_target_project_id(token_info)
    descriptor = {"kind": kind, **payload}
    fingerprint = compute_config_digest(descriptor)
    context_digest = payload.get("context_digest")
    store = _store_or_503()
    async with (
        _locked_blobs(store, [context_digest] if context_digest else []),
        sql_mutex(_factory_or_503(), "build-api:image:project:" + project) as session,
    ):
        existing = await session.scalar(
            select(PalimpsestHubImageJob).where(
                exact_identity(PalimpsestHubImageJob.project_id, project),
                PalimpsestHubImageJob.request_id == payload["request_id"],
            )
        )
        if existing is not None:
            if existing.request_digest != fingerprint:
                raise HTTPException(status_code=409, detail="request_id is already bound to another descriptor")
            return existing
        if context_digest:
            context = await session.get(PalimpsestHubBuildContext, (project, context_digest))
            if context is None or context.project_id != project or not store.exists(context_digest):
                raise HTTPException(status_code=404, detail="build context not found")
        count = await session.scalar(
            select(func.count())
            .select_from(PalimpsestHubImageJob)
            .where(
                exact_identity(PalimpsestHubImageJob.project_id, project),
                PalimpsestHubImageJob.status.in_(("queued", "building")),
            )
        )
        if count >= 4:
            raise HTTPException(status_code=429, detail="project image queue is full")
        await fresh_writer(token_info)
        row = PalimpsestHubImageJob(
            id=str(uuid4()),
            project_id=project,
            created_by=token_info["user_id"],
            request_id=payload["request_id"],
            kind=kind,
            name=payload["name"],
            request_digest=fingerprint,
            request_json=payload,
            context_digest=context_digest,
            status="queued",
            staged_outputs=[],
            output_digests=[],
            created_at=now(),
        )
        session.add(row)
        if context_digest:
            await retain_blob(session, context_digest, "image-job", row.id)
        await session.commit()
        return row


def validate_context(path):
    settings = get_settings()
    members = 0
    expanded = 0
    paths = {}
    links = {}

    def target_parts(parent, target):
        if not target or target.startswith("/") or "\\" in target or "\x00" in target:
            raise ValueError("unsafe context symlink")
        parts = list(parent)
        for part in target.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise ValueError("escaping context symlink")
                parts.pop()
            else:
                parts.append(part)
        return tuple(parts)

    try:
        with tarfile.open(path, mode="r:") as archive:
            for member in archive:
                members += 1
                raw = member.name
                parsed = PurePosixPath(raw)
                if members > settings.palimpsest_hub_max_context_members:
                    raise ValueError("too many context members")
                if raw in (".", "./") and member.isdir():
                    continue
                name = str(parsed)
                if (
                    not raw
                    or len(raw) > 4096
                    or "\\" in raw
                    or "\x00" in raw
                    or parsed.is_absolute()
                    or ".." in parsed.parts
                    or name in paths
                    or member.size < 0
                    or not (member.isfile() or member.isdir() or member.issym())
                    or member.issparse()
                ):
                    raise ValueError("unsafe context member")
                paths[name] = "directory" if member.isdir() else "symlink" if member.issym() else "file"
                if member.issym():
                    target_parts(parsed.parts[:-1], member.linkname)
                    if len(member.linkname) > 4096:
                        raise ValueError("oversized context symlink")
                    links[parsed.parts] = member.linkname.split("/")
                expanded += member.size
                if expanded > settings.palimpsest_hub_max_context_expanded_bytes:
                    raise ValueError("expanded context too large")
        # Extraction is physical files/directories first, then links. No archive
        # member may write through a file/symlink ancestor, regardless of order.
        for name in paths:
            for ancestor in PurePosixPath(name).parents:
                if str(ancestor) != "." and paths.get(str(ancestor), "directory") != "directory":
                    raise ValueError("non-directory context ancestor")
        # Resolve contextual link chains without touching the controller filesystem.
        for name in links:
            seen = set()
            pending = list(name)
            resolved = []
            while pending:
                part, *pending = pending
                if part in ("", "."):
                    continue
                if part == "..":
                    if not resolved:
                        raise ValueError("escaping context symlink")
                    resolved.pop()
                    continue
                resolved.append(part)
                key = tuple(resolved)
                if key in links:
                    if key in seen or len(seen) >= 40:
                        raise ValueError("context symlink cycle or excessive depth")
                    seen.add(key)
                    resolved.pop()
                    pending = links[key] + pending
    except (tarfile.TarError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="invalid or unsafe build context tar") from None


async def receive(store, request, digest, size, deadline, *, context=False):
    staging = uuid4().hex
    try:
        await _run_blocking(store.start_upload, staging)
        try:
            async with asyncio.timeout(max(0, (deadline - now()).total_seconds())):
                count = 0
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > size:
                        raise HTTPException(status_code=413, detail="content exceeds declared size")
                    if chunk:
                        await _run_blocking(store.append_upload, staging, chunk)
        except TimeoutError:
            raise HTTPException(status_code=409, detail="content transfer deadline expired") from None
        if count != size:
            raise HTTPException(status_code=422, detail="content size mismatch")
        path = store.upload_path(staging)
        verified = await _run_blocking(store.inspect_file, path, max_bytes=size)
        if verified.blob_digest != digest:
            raise HTTPException(status_code=422, detail="content digest mismatch")
        if context:
            await _run_blocking(validate_context, path)
        else:

            def magic():
                with path.open("rb") as handle:
                    return handle.read(4) == b"hsqs"

            if not await _run_blocking(magic):
                raise HTTPException(status_code=422, detail="output is not SquashFS")
        yield path, verified
    except HubStoreError:
        raise HTTPException(status_code=422, detail="content could not be verified") from None
    finally:
        await _run_blocking(store.abort_upload, staging)


_receive = asynccontextmanager(receive)


async def upload_context(request, token_info, digest, size):
    store = _store_or_503()
    project = package_target_project_id(token_info)
    async with (
        _locked_file(store, lambda: store.acquire_package_lock("image-context:" + project, blocking=False)),
        _receive(store, request, digest, size, now() + timedelta(seconds=300), context=True) as (path, verified),
        _locked_blob(store, digest),
    ):
        created = await _run_blocking(store.publish_verified, path, verified)
        try:
            async with sql_mutex(_factory_or_503(), "build-api:image:project:" + project) as session:
                context = await session.get(PalimpsestHubBuildContext, (project, digest))
                if context is not None and (context.project_id != project or context.size_bytes != size):
                    raise HTTPException(status_code=409, detail="build context descriptor conflicts")
                await fresh_writer(token_info)
                if context is None:
                    session.add(
                        PalimpsestHubBuildContext(
                            project_id=project, digest=digest, size_bytes=size, created_by=token_info["user_id"]
                        )
                    )
                await retain_blob(session, digest, "context", digest)
                await session.commit()
        except BaseException:
            if created:
                await _discard_unregistered_blob(store, _factory_or_503(), digest)
            raise
    return {"digest": digest, "size_bytes": size}


@asynccontextmanager
async def locked_jobs():
    async with sql_mutex(_factory_or_503(), "build-api:image:single-flight") as session:
        yield session


def deadline(job):
    value = job.lease_expires_at
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


def expire(job):
    if job.status == "building" and (deadline(job) is None or deadline(job) <= now()):
        job.status, job.error_code, job.completed_at = "error", "worker_interrupted", now()


async def owned(session, job_id, lease, *, live=False):
    job = await session.get(PalimpsestHubImageJob, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(status_code=404, detail="image job not found")
    if not job.lease_owner or not hmac.compare_digest(job.lease_owner.encode(), lease.encode()):
        raise HTTPException(status_code=409, detail="image lease does not match")
    expire(job)
    if job.status == "error":
        await session.commit()
    if live and job.status != "building":
        raise HTTPException(status_code=409, detail="image attempt is not active")
    return job


async def context_for(session, job):
    if not job.context_digest:
        return None
    context = await session.get(PalimpsestHubBuildContext, (job.project_id, job.context_digest))
    if context is None or context.project_id != job.project_id:
        raise HTTPException(status_code=409, detail="build context unavailable")
    return {"digest": context.digest, "size_bytes": context.size_bytes}


async def claim():
    settings = get_settings()
    async with locked_jobs() as session:
        active = (
            await session.scalars(
                select(PalimpsestHubImageJob).where(PalimpsestHubImageJob.status == "building").with_for_update()
            )
        ).all()
        for job in active:
            expire(job)
        if any(job.status == "building" for job in active):
            await session.commit()
            return None
        jobs = (
            await session.scalars(
                select(PalimpsestHubImageJob)
                .where(PalimpsestHubImageJob.status == "queued")
                .order_by(PalimpsestHubImageJob.created_at, PalimpsestHubImageJob.id)
                .with_for_update()
            )
        ).all()
        for job in jobs:
            try:
                if compute_config_digest({"kind": job.kind, **job.request_json}) != job.request_digest:
                    raise HTTPException(status_code=409)
                if job.kind == "registry":
                    normalize_registry_reference(job.request_json["reference"])
                context = await context_for(session, job)
                if context and not _store_or_503().exists(context["digest"]):
                    raise HTTPException(status_code=409)
            except HTTPException:
                job.status, job.error_code, job.completed_at = "error", "build_failed", now()
                continue
            job.status, job.started_at, job.lease_owner = "building", now(), str(uuid4())
            timeout = min(3600, settings.palimpsest_hub_build_timeout_seconds)
            job.lease_expires_at = job.started_at + timedelta(seconds=timeout)
            result = {
                "id": job.id,
                "kind": job.kind,
                "name": job.name,
                "platform": job.request_json["platform"],
                "request": {
                    key: value
                    for key, value in job.request_json.items()
                    if key not in ("request_id", "name", "platform")
                },
                "context": context,
                "lease": job.lease_owner,
                "deadline": deadline(job).isoformat(),
                "timeout_seconds": max(1, math.ceil((deadline(job) - now()).total_seconds())),
                "max_output_bytes": settings.palimpsest_hub_max_blob_bytes,
            }
            await session.commit()
            return result
        await session.commit()
        return None


async def worker_status(job_id, lease):
    async with locked_jobs() as session:
        job = await owned(session, job_id, lease)
        await session.commit()
        return job


async def download_context(job_id, lease, digest):
    store = _store_or_503()
    async with _locked_blob(store, digest):
        async with locked_jobs() as session:
            job = await owned(session, job_id, lease, live=True)
            if job.context_digest != digest:
                raise HTTPException(status_code=404, detail="image job input not found")
            context = await context_for(session, job)
            size, expiry = context["size_bytes"], deadline(job)
        try:
            verified = await _run_blocking(store.inspect_file, store.blob_path(digest), max_bytes=size)
        except HubStoreError:
            raise HTTPException(status_code=409, detail="build context bytes unavailable") from None
        if verified.blob_digest != digest or verified.size_bytes != size:
            raise HTTPException(status_code=409, detail="build context bytes changed")
        async with locked_jobs() as session:
            await owned(session, job_id, lease, live=True)
        handle = store.open_read(digest)

    async def chunks():
        try:
            remaining = size
            while remaining:
                if now() >= expiry:
                    raise RuntimeError("image input deadline expired")
                chunk = await _run_blocking(handle.read, min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("image input truncated")
                remaining -= len(chunk)
                yield chunk
        finally:
            handle.close()

    return StreamingResponse(
        chunks(), media_type="application/x-tar", headers={"Content-Length": str(size), "X-Content-SHA256": digest}
    )


async def stage_output(job_id, lease, request: Request, digest, size):
    store = _store_or_503()
    async with _locked_file(store, lambda: store.acquire_package_lock("image-output:" + job_id, blocking=False)):
        async with locked_jobs() as session:
            job = await owned(session, job_id, lease)
            if job.status == "complete":
                if {"digest": digest, "size_bytes": size} not in job.result_json["layers"]:
                    raise HTTPException(status_code=409, detail="completed output conflicts")
                expiry = now() + timedelta(seconds=60)
            else:
                job = await owned(session, job_id, lease, live=True)
                entries = job.staged_outputs
                old = next((item for item in entries if item["digest"] == digest), None)
                if old and old["size_bytes"] != size:
                    raise HTTPException(status_code=409, detail="staged output descriptor conflicts")
                if not old and (
                    len(entries) >= 128
                    or sum(item["size_bytes"] for item in entries) + size > get_settings().palimpsest_hub_max_blob_bytes
                ):
                    raise HTTPException(status_code=413, detail="image output set exceeds byte limit")
                expiry = deadline(job)
        async with (
            _receive(store, request, digest, size, expiry) as (path, verified),
            _locked_blob(store, digest),
            locked_jobs() as session,
        ):
            job = await owned(session, job_id, lease)
            if job.status == "complete":
                if {"digest": digest, "size_bytes": size} not in job.result_json["layers"]:
                    raise HTTPException(status_code=409, detail="completed output conflicts")
                return job
            job = await owned(session, job_id, lease, live=True)
            created = await _run_blocking(store.publish_verified, path, verified)
            try:
                job = await owned(session, job_id, lease, live=True)
                if not any(item["digest"] == digest for item in job.staged_outputs):
                    job.staged_outputs = [
                        *job.staged_outputs,
                        {"digest": digest, "size_bytes": size, "blob_md5": verified.blob_md5},
                    ]
                await retain_blob(session, digest, "image-job", job.id)
                await session.commit()
            except BaseException:
                await session.rollback()
                if created:
                    await _discard_unregistered_blob(store, _factory_or_503(), digest)
                raise
            return job


def compatible(row, size):
    config = row.config_json or {}
    return (
        row.size_bytes == size
        and row.kind == "squashfs"
        and row.media_type == MEDIA_TYPE_LAYER_SQUASHFS
        and row.arch == "x86_64"
        and row.parent_digest is None
        and row.chain_id is None
        and row.disk_format is None
        and config.get("base_image_digest") is None
    )


async def complete(job_id, lease, descriptor):
    store = _store_or_503()
    layers = descriptor["layers"]
    digests = [item["digest"] for item in layers]
    if sum(item["size_bytes"] for item in layers) > get_settings().palimpsest_hub_max_blob_bytes:
        raise HTTPException(status_code=413, detail="image output stack exceeds byte limit")
    async with _locked_blobs(store, digests), locked_jobs() as session:
        job = await owned(session, job_id, lease)
        if job.status == "complete":
            if job.result_json != descriptor:
                raise HTTPException(status_code=409, detail="completed image descriptor conflicts")
            return job
        job = await owned(session, job_id, lease, live=True)
        stages = {item["digest"]: item for item in job.staged_outputs}
        if set(stages) != set(digests) or any(
            stages[item["digest"]]["size_bytes"] != item["size_bytes"] for item in layers
        ):
            raise HTTPException(status_code=409, detail="complete descriptor does not match staged outputs")
        # The job stores the contextual OCI occurrence graph. Global blob rows
        # stay parentless, so shared/repeated bytes are not assigned fake parents.
        for item in {item["digest"]: item for item in layers}.values():
            digest, size = item["digest"], item["size_bytes"]
            try:
                verified = await _run_blocking(store.inspect_file, store.blob_path(digest), max_bytes=size)
            except HubStoreError:
                raise HTTPException(status_code=409, detail="staged output bytes unavailable") from None
            if verified.blob_digest != digest or verified.size_bytes != size:
                raise HTTPException(status_code=409, detail="staged output bytes changed")
            existing = await session.scalar(
                select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest).with_for_update()
            )
            if existing is not None:
                if not compatible(existing, size):
                    raise HTTPException(status_code=409, detail="output conflicts with an existing layer structure")
                await _grant_layer_access(session, digest, {"project_id": job.project_id, "user_id": job.created_by})
            else:
                # Names and image runtime are NOT global blob identities. A source image's
                # runtime is bound to this completed job, not overwritten onto shared bytes.
                name = "oci-" + digest[7:]
                name = name[:64]
                config = {
                    "name": name,
                    "kind": "squashfs",
                    "ubuntu_base": None,
                    "python_version": None,
                    "parent_digest": None,
                    "chain_id": None,
                    "blob_digest": digest,
                    "disk_format": None,
                    "arch": "x86_64",
                    "os_variant": None,
                    "base_image_digest": None,
                    "instance_mode": "oci-layer",
                }
                session.add(
                    PalimpsestHubLayer(
                        blob_digest=digest,
                        blob_md5=verified.blob_md5,
                        size_bytes=size,
                        media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                        arch="x86_64",
                        config_digest=compute_config_digest(config),
                        parent_digest=None,
                        name=name,
                        kind="squashfs",
                        config_json=config,
                        project_id=job.project_id,
                        is_published=False,
                        created_by=job.created_by,
                    )
                )
        # Disk verification and SQL flush must not extend the immutable execution lease.
        await session.flush()
        if now() >= deadline(job):
            # Roll back ALL pending layer registrations; the next lease-status
            # transaction records expiry without committing a partial output set.
            raise HTTPException(status_code=409, detail="image attempt deadline expired")
        job.output_digests, job.result_json = digests, descriptor
        job.status, job.completed_at = "complete", now()
        await session.commit()
        return job


async def fail(job_id, lease, error_code):
    async with locked_jobs() as session:
        job = await owned(session, job_id, lease)
        if job.status == "building":
            job.status, job.error_code, job.completed_at = "error", error_code, now()
        await session.commit()
        return job
