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
from palimpsest_hub.config import configured_image_source_policy, get_settings
from palimpsest_hub.image_models import LEGACY_JOB_VISIBILITY, PalimpsestHubBuildContext, PalimpsestHubImageJob
from palimpsest_hub.models import exact_identity
from palimpsest_hub.services import claim_recovery
from palimpsest_hub.services.blob_references import retain_blob
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.git_context import ContextError
from palimpsest_hub.services.git_source import GitSourceError
from palimpsest_hub.services.git_staging import GitStagingUnavailable, stage_git_context
from palimpsest_hub.services.hub_store import HubStoreError
from palimpsest_hub.services.published_images import IMAGE_MUTEX
from palimpsest_hub.services.worker_leases import sql_mutex


def now():
    return datetime.now(UTC)


def _valid_os_declarations(payload):
    # Match manifest byte/NUL bounds and the worker's nonblank requirement.
    for name in ("os_family", "os_version"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            return False
        try:
            if len(value.encode("utf-8")) > 128:
                return False
        except UnicodeError:
            return False
    return True


def _has_publication_declarations(job):
    # Historical jobs cannot borrow an actor or invented OS declarations.
    return isinstance(job.created_by, str) and bool(job.created_by.strip()) and _valid_os_declarations(job.request_json)


def iso(value):
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def serialize_job(row, *, worker=False):
    result = row.result_json if row.status == "complete" else None
    # Pre-publication-era completed jobs are historical execution receipts, not
    # catalog images. Do not manufacture a reusable image ID for those rows.
    published_image_id = row.id if isinstance((result or {}).get("manifest"), dict) else None
    if result and not worker:
        runtime = result["runtime"]
        result = {
            "source": result["source"],
            "runtime": {
                "entrypoint": runtime["entrypoint"],
                "cmd": runtime["cmd"],
                "working_dir": runtime["working_dir"],
                "user": runtime["user"],
                "env_names": [
                    item.partition("=")[0]
                    for item in runtime["env"]
                    if "=" in item and item.partition("=")[0].isascii() and item.partition("=")[0].isidentifier()
                ],
            },
        }
    value = {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "visibility": row.visibility,
        "status": row.status,
        "request_id": row.request_id,
        "request_digest": row.request_digest,
        "output_digests": row.output_digests,
        "published_image_id": published_image_id,
        "result": result,
        "error_code": row.error_code,
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "completed_at": iso(row.completed_at),
    }
    if worker:
        value["publication"] = {
            "version": 1,
            "deadline": iso(row.lease_expires_at),
            "result_digest": compute_config_digest(row.result_json) if row.result_json else None,
            "staged_outputs": [
                {"digest": item["digest"], "size_bytes": item["size_bytes"]} for item in row.staged_outputs
            ],
        }
    return value


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


def require_configured_policy(policy):
    if not policy.rules:
        raise HTTPException(status_code=503, detail="image source policy is not configured")


def admit_sources(kind, payload, policy, hosts):
    """Early-check literal image inputs; BuildKit fences resolved dynamic inputs."""

    from palimpsest_local.registry import RegistryError, default_registry_config, resolve_image_reference

    require_configured_policy(policy)

    def admit(reference):
        canonical = normalize_registry_reference(reference, hosts)
        try:
            policy.admit(resolve_image_reference(canonical, default_registry_config(), environment={}))
        except RegistryError:
            raise HTTPException(status_code=422, detail="image source is not allowed by operator policy") from None
        return canonical

    if kind == "registry":
        return {**payload, "reference": admit(payload["reference"])}
    # This is deliberately not a Dockerfile interpreter. Literal FROM and
    # external COPY/ADD --from inputs can fail before queue/context effects;
    # ARG expansion, stage aliases and all actual resolutions are enforced by
    # the exact BuildKit source-policy transport at solve time.
    from palimpsest_local.buildkit import (
        _FROM_RE,
        _PARSER_DIRECTIVE_RE,
        _copy_from_sources,
        _instruction_leading_options,
        _is_instruction,
        _logical_dockerfile_text_lines,
    )
    from palimpsest_local.errors import ArtifactValidationError

    lines = _logical_dockerfile_text_lines(payload["dockerfile"])
    stages = {match[2].lower() for line in lines if (match := _FROM_RE.fullmatch(line)) is not None and match[2]}

    def literal(source):
        if "$" not in source and not source.isdecimal() and source.lower() not in stages:
            admit(source)

    for line in lines:
        directive = _PARSER_DIRECTIVE_RE.fullmatch(line)
        if directive:
            if directive[1].lower() == "escape":
                break  # Non-default parser semantics belong to the actual solve.
            # The fixed built-in frontend uses cmdline= and does not resolve a
            # syntax image. Do not impose permission for an unused header.
            continue
        match = _FROM_RE.fullmatch(line)
        if match:
            if match[1].lower() != "scratch":
                literal(match[1])
            continue
        if "<<" in line:
            break  # Do not mistake heredoc payload text for image instructions.
        try:
            if _is_instruction(line, "COPY") or _is_instruction(line, "ADD"):
                for source in _copy_from_sources(line):
                    literal(source)
            if _is_instruction(line, "RUN"):
                for option in _instruction_leading_options(line, "RUN"):
                    if not option.lower().startswith("--mount="):
                        continue
                    for field in option.split("=", 1)[1].strip("\"'").split(","):
                        key, separator, source = field.partition("=")
                        if separator and key.strip().lower() == "from":
                            literal(source.strip().strip("\"'"))
        except ArtifactValidationError:
            continue  # Ambiguous quoted flags are fenced by BuildKit, not guessed.
    return payload


def bound_source_policy(payload, settings):
    """Verify persisted operator provenance without widening a queued request."""
    from palimpsest_local.image_source_policy import image_source_policy_from_dict
    from palimpsest_local.registry import RegistryError

    try:
        policy = image_source_policy_from_dict(payload.get("source_policy"))
        if (
            not policy.rules
            or payload.get("source_policy_digest") != policy.digest
            or policy != configured_image_source_policy(settings)
        ):
            raise ValueError("image source policy changed")
    except (RegistryError, ValueError):
        raise HTTPException(status_code=409, detail="image source policy binding changed") from None
    return policy


def require_result_policy(job, descriptor):
    metadata = descriptor.get("manifest", {}).get("source", {}).get("metadata", {})
    if (
        metadata.get("source_policy") != job.request_json.get("source_policy")
        or metadata.get("source_policy_digest") != job.request_json.get("source_policy_digest")
        or job.request_json.get("source_policy") is None
    ):
        raise HTTPException(status_code=409, detail="image result source policy binding changed")


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


def _preflight(kind, payload):
    """Cheap gates that must pass before any context, Git fetch or queue effect."""
    if not _valid_os_declarations(payload):
        raise HTTPException(
            status_code=422, detail="OS declarations must be nonblank, NUL-free and at most 128 UTF-8 bytes"
        )
    settings = get_settings()
    if not settings.palimpsest_hub_build_worker_key.get_secret_value():
        raise HTTPException(status_code=503, detail="isolated image worker is not configured")
    if "source_policy" in payload or "source_policy_digest" in payload:
        raise HTTPException(status_code=422, detail="image source policy is operator-controlled")
    return settings, configured_image_source_policy(settings)


async def admit(kind, payload, token_info):
    settings, policy = _preflight(kind, payload)
    if kind == "registry":
        payload = {**payload, "reference": normalize_registry_reference(payload["reference"])}
    project = package_target_project_id(token_info)
    payload = {**payload, "visibility": payload.get("visibility", "private")}
    from palimpsest_hub.services.published_images import visibility_value

    visibility_value(payload["visibility"])

    caller_descriptor = {"kind": kind, **payload}
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
            stored_descriptor = {"kind": existing.kind, **existing.request_json}
            stored_caller_descriptor = {
                key: value
                for key, value in stored_descriptor.items()
                if key not in ("source_policy", "source_policy_digest")
            }
            if (
                existing.created_by != token_info["user_id"]
                or existing.request_digest != compute_config_digest(stored_descriptor)
                or stored_caller_descriptor != caller_descriptor
            ):
                raise HTTPException(status_code=409, detail="request_id is already bound to another descriptor")
            return existing
        payload = admit_sources(kind, payload, policy, settings.palimpsest_hub_registry_hosts)
        payload = {**payload, "source_policy": policy.to_dict(), "source_policy_digest": policy.digest}
        fingerprint = compute_config_digest({"kind": kind, **payload})
        if context_digest:
            context = await session.get(
                PalimpsestHubBuildContext,
                {"project_id": project, "digest": context_digest, "created_by": token_info["user_id"]},
            )
            if (
                context is None
                or context.project_id != project
                or context.created_by != token_info["user_id"]
                or not store.exists(context_digest)
            ):
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
            visibility=payload["visibility"],
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


async def receive(store, request, digest, size, deadline, *, context=False, squashfs=True):
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
        elif squashfs:

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


async def register_context_blob(store, token_info, project, digest, size, *, created):
    """Record ownership of a verified context blob; discard it again if registration never committed."""
    commit_started = False
    try:
        async with sql_mutex(_factory_or_503(), "build-api:image:project:" + project) as session:
            context = await session.get(
                PalimpsestHubBuildContext,
                {"project_id": project, "digest": digest, "created_by": token_info["user_id"]},
            )
            if context is not None and (
                context.project_id != project
                or context.created_by != token_info["user_id"]
                or context.size_bytes != size
            ):
                raise HTTPException(status_code=409, detail="build context descriptor conflicts")
            await fresh_writer(token_info)
            if context is None:
                session.add(
                    PalimpsestHubBuildContext(
                        project_id=project, digest=digest, size_bytes=size, created_by=token_info["user_id"]
                    )
                )
            await retain_blob(session, digest, "context", digest)
            commit_started = True
            await session.commit()
    except BaseException:
        if created and not commit_started:
            await _discard_unregistered_blob(store, _factory_or_503(), digest)
        raise


async def upload_context(request, token_info, digest, size):
    store = _store_or_503()
    project = package_target_project_id(token_info)
    async with (
        _locked_file(store, lambda: store.acquire_package_lock("image-context:" + project, blocking=False)),
        _receive(store, request, digest, size, now() + timedelta(seconds=300), context=True) as (path, verified),
        _locked_blob(store, digest),
    ):
        created = await _run_blocking(store.publish_verified, path, verified)
        await register_context_blob(store, token_info, project, digest, size, created=created)
    return {"digest": digest, "size_bytes": size}


async def _git_receipt_replay(req, token_info, project):
    """Return the job already admitted for this request_id, or refuse a reused id; None if new.

    Runs before any fetch, so an identical retry costs no network or DNS work.
    """
    async with _factory_or_503()() as session:
        existing = await session.scalar(
            select(PalimpsestHubImageJob).where(
                exact_identity(PalimpsestHubImageJob.project_id, project),
                PalimpsestHubImageJob.request_id == str(req.request_id),
            )
        )
    if existing is None:
        return None
    stored = existing.request_json or {}
    git = stored.get("git") or {}
    same = (
        existing.kind == "dockerfile"
        and existing.created_by == token_info["user_id"]
        and git.get("repo_url") == req.repo_url
        and git.get("revision") == req.revision
        and git.get("context_subdir") == req.context_subdir
        and git.get("dockerfile_path") == req.dockerfile_path
        and stored.get("name") == req.name
        and stored.get("os_family") == req.os_family
        and stored.get("os_version") == req.os_version
        and stored.get("visibility") == req.visibility
    )
    if not same:
        raise HTTPException(status_code=409, detail="request_id is already bound to another descriptor")
    return existing


async def admit_git_dockerfile(req, token_info):
    """Stage a pinned Git checkout as an owned context, then admit a Dockerfile job for it."""
    _, policy = _preflight("dockerfile", {"os_family": req.os_family, "os_version": req.os_version})
    # Git fetch cannot be replayed from a receipt without a context digest, so refuse here, before
    # any network effect, when the operator has no source policy (fail closed).
    require_configured_policy(policy)
    store = _store_or_503()
    project = package_target_project_id(token_info)
    replayed = await _git_receipt_replay(req, token_info, project)
    if replayed is not None:
        return replayed
    async with _locked_file(store, lambda: store.acquire_package_lock("image-context:" + project, blocking=False)):
        try:
            staged = await stage_git_context(
                req,
                allowed_hosts=get_settings().palimpsest_hub_git_allowed_hosts,
                store=store,
                max_context_bytes=get_settings().palimpsest_hub_max_context_bytes,
                max_members=get_settings().palimpsest_hub_max_context_members,
                max_expanded_bytes=get_settings().palimpsest_hub_max_context_expanded_bytes,
            )
        except GitStagingUnavailable:
            raise HTTPException(status_code=503, detail="git builds are not enabled on this Hub") from None
        except GitSourceError as exc:
            raise HTTPException(status_code=422, detail=f"git source rejected: {exc.code}") from None
        except ContextError as exc:
            raise HTTPException(status_code=422, detail=f"build context rejected: {exc}") from None
        except HubStoreError:
            raise HTTPException(status_code=422, detail="build context exceeds the byte limit") from None
        async with _locked_blob(store, staged.blob_digest):
            await register_context_blob(store, token_info, project, staged.blob_digest, staged.size_bytes, created=True)
    payload = {
        "request_id": str(req.request_id),
        "name": req.name,
        "dockerfile": staged.dockerfile_text,
        "context_digest": staged.blob_digest,
        "platform": "linux/amd64",
        "os_family": req.os_family,
        "os_version": req.os_version,
        "metadata": req.metadata,
        "visibility": req.visibility,
        "git": {
            "repo_url": req.repo_url,
            "revision": staged.revision,
            "context_subdir": req.context_subdir,
            "dockerfile_path": req.dockerfile_path,
        },
    }
    return await admit("dockerfile", payload, token_info)


@asynccontextmanager
async def locked_jobs():
    async with sql_mutex(_factory_or_503(), IMAGE_MUTEX) as session:
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
    if live:
        bound_source_policy(job.request_json, get_settings())
    return job


async def context_for(session, job):
    if not job.context_digest:
        return None
    context = await session.get(
        PalimpsestHubBuildContext,
        {"project_id": job.project_id, "digest": job.context_digest, "created_by": job.created_by},
    )
    if context is None or context.project_id != job.project_id or context.created_by != job.created_by:
        raise HTTPException(status_code=409, detail="build context unavailable")
    return {"digest": context.digest, "size_bytes": context.size_bytes}


async def claim(channel: str, sequence: int, *, recover: bool = False):
    settings = get_settings()
    async with locked_jobs() as session:
        cursor, settled = await claim_recovery.prepare(session, "image", channel, sequence)
        if recover or settled:
            await session.commit()
            return claim_recovery.settlement(cursor) if recover else cursor.result_json
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
            if not _has_publication_declarations(job):
                job.status, job.error_code, job.completed_at = "error", "publication_metadata_required", now()
                continue
            try:
                if compute_config_digest({"kind": job.kind, **job.request_json}) != job.request_digest:
                    raise HTTPException(status_code=409)
                if job.request_json.get("visibility", LEGACY_JOB_VISIBILITY) != job.visibility:
                    raise HTTPException(status_code=409)
                policy = bound_source_policy(job.request_json, settings)
                admit_sources(job.kind, job.request_json, policy, settings.palimpsest_hub_registry_hosts)
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
                    if key not in ("request_id", "name", "platform", "source_policy", "source_policy_digest")
                },
                "context": context,
                "source_policy": policy.to_dict(),
                "source_policy_digest": policy.digest,
                "lease": job.lease_owner,
                "deadline": deadline(job).isoformat(),
                "timeout_seconds": max(1, math.ceil((deadline(job) - now()).total_seconds())),
                "max_output_bytes": settings.palimpsest_hub_max_blob_bytes,
            }
            cursor.result_json = result
            await session.commit()
            return result
        await session.commit()
        return None


async def worker_status(job_id, lease):
    store = _store_or_503()
    async with (
        _locked_file(store, lambda: store.acquire_package_lock("image-output:" + job_id, blocking=False)),
        locked_jobs() as session,
    ):
        job = await owned(session, job_id, lease)
        await session.commit()
        return job


async def publication_intent(job_id, lease, descriptor):
    """Bind a result without publishing; exact retries do not extend the lease."""
    store = _store_or_503()
    if sum(item["size_bytes"] for item in descriptor["layers"]) > get_settings().palimpsest_hub_max_blob_bytes:
        raise HTTPException(status_code=413, detail="image output stack exceeds byte limit")
    async with (
        _locked_file(store, lambda: store.acquire_package_lock("image-output:" + job_id, blocking=False)),
        locked_jobs() as session,
    ):
        job = await owned(session, job_id, lease)
        require_result_policy(job, descriptor)
        if job.result_json is not None and job.result_json != descriptor:
            raise HTTPException(status_code=409, detail="image publication descriptor conflicts")
        if job.status != "complete":
            await owned(session, job_id, lease, live=True)
            expected = {item["digest"]: item["size_bytes"] for item in descriptor["layers"]}
            if any(expected.get(item["digest"]) != item["size_bytes"] for item in job.staged_outputs):
                raise HTTPException(status_code=409, detail="publication intent conflicts with staged output")
            job.result_json = descriptor
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
                if (
                    job.result_json is not None
                    and {"digest": digest, "size_bytes": size} not in job.result_json["layers"]
                ):
                    raise HTTPException(status_code=409, detail="image output differs from publication intent")
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
            commit_started = False
            try:
                job = await owned(session, job_id, lease, live=True)
                if not any(item["digest"] == digest for item in job.staged_outputs):
                    job.staged_outputs = [
                        *job.staged_outputs,
                        {"digest": digest, "size_bytes": size, "blob_md5": verified.blob_md5},
                    ]
                await retain_blob(session, digest, "image-job", job.id)
                commit_started = True
                await session.commit()
            except BaseException:
                if not commit_started:
                    await session.rollback()
                    if created:
                        await _discard_unregistered_blob(store, _factory_or_503(), digest)
                raise
            return job


async def complete(job_id, lease, descriptor):
    store = _store_or_503()
    layers = descriptor["layers"]
    digests = [item["digest"] for item in layers]
    if sum(item["size_bytes"] for item in layers) > get_settings().palimpsest_hub_max_blob_bytes:
        raise HTTPException(status_code=413, detail="image output stack exceeds byte limit")
    async with (
        _locked_file(store, lambda: store.acquire_package_lock("image-output:" + job_id, blocking=False)),
        _locked_blobs(store, digests),
        locked_jobs() as session,
    ):
        job = await owned(session, job_id, lease)
        if not _has_publication_declarations(job):
            raise HTTPException(status_code=409, detail="image publication requires proven creator and OS declarations")
        if job.visibility != job.request_json.get("visibility", LEGACY_JOB_VISIBILITY) or (
            compute_config_digest({"kind": job.kind, **job.request_json}) != job.request_digest
        ):
            raise HTTPException(status_code=409, detail="image publication visibility or request binding changed")
        if job.status == "complete":
            if job.result_json != descriptor:
                raise HTTPException(status_code=409, detail="completed image descriptor conflicts")
            return job
        job = await owned(session, job_id, lease, live=True)
        require_result_policy(job, descriptor)
        if job.result_json is not None and job.result_json != descriptor:
            raise HTTPException(status_code=409, detail="image publication descriptor conflicts")
        stages = {item["digest"]: item for item in job.staged_outputs}
        if set(stages) != set(digests) or any(
            stages[item["digest"]]["size_bytes"] != item["size_bytes"] for item in layers
        ):
            raise HTTPException(status_code=409, detail="complete descriptor does not match staged outputs")
        from palimpsest_hub.services.image_quota import QUOTA_ERROR_CODES
        from palimpsest_hub.services.published_images import normalized, register_image

        canonical = descriptor["manifest"]
        canonical = normalized(canonical)
        if (
            canonical["os_family"] != job.request_json["os_family"]
            or canonical["os_version"] != job.request_json["os_version"]
            or canonical["mode"] != "oci-root"
            or canonical["platform"] != job.request_json.get("platform", "linux/amd64")
            or canonical["source"]["kind"] != ("registry" if job.kind == "registry" else "local")
            or canonical["metadata"] != job.request_json.get("metadata", {})
            or canonical["runtime"] != descriptor["runtime"]
            or [{key: item[key] for key in ("digest", "size_bytes")} for item in canonical["layers"]] != layers
            or any(canonical["source"].get(key) != value for key, value in descriptor["source"].items())
        ):
            raise HTTPException(status_code=409, detail="image manifest contradicts admitted output")
        try:
            await register_image(
                session,
                store,
                {"project_id": job.project_id, "user_id": job.created_by},
                job.request_id,
                job.name,
                canonical,
                image_id=job.id,
                visibility=job.visibility,
            )
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
            if code not in QUOTA_ERROR_CODES:
                raise
            quota_code = code
            # Discard every pending registration; the output locks above stay held.
            await session.rollback()
        else:
            quota_code = None
        if quota_code is not None:
            # A tenant quota is a definite job outcome. Answering with the terminal
            # error receipt lets the worker settle instead of halting as uncertain.
            async with locked_jobs() as fresh:
                job = await owned(fresh, job_id, lease)
                if job.status == "building":
                    job.status, job.error_code, job.completed_at = "error", quota_code, now()
                await fresh.commit()
                return job
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
