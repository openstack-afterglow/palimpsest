"""Palimpsest Glance image export service.

Provides durable Glance-to-hub image export enqueueing, status query,
soft deletion, worker claim/execution with lease fencing, qemu conversion,
and deferred blob store garbage collection.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    import openstack

from palimpsest_hub.config import get_settings
from palimpsest_hub.database import get_session_factory
from palimpsest_hub.models import PalimpsestHubLayer, PalimpsestImageExport
from palimpsest_hub.openstack import get_admin_connection_for_project, get_image
from palimpsest_hub.services.hub_store import (
    IMAGE_FORMAT_SPECS,
    get_blob_store,
)

_logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_CONVERTING = "converting"
STATUS_FINALIZING = "finalizing"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"
EXPORT_STATUSES = (
    STATUS_QUEUED,
    STATUS_DOWNLOADING,
    STATUS_CONVERTING,
    STATUS_FINALIZING,
    STATUS_COMPLETE,
    STATUS_ERROR,
)

PROGRESS_QUEUED = 0
PROGRESS_DOWNLOADING = 10
PROGRESS_CONVERTING = 50
PROGRESS_FINALIZING = 90
PROGRESS_COMPLETE = 100

CONVERTER_CONTRACT = "palimpsest-qemu-convert-v1"
SUPPORTED_FORMATS = ("raw", "qcow2", "vmdk", "vdi", "vhd", "vhdx")
QEMU_MEASURE_DRIVERS = frozenset({"raw", "qcow2"})


def _now() -> datetime:
    return datetime.now(UTC)


class ImageExportError(Exception):
    """Domain exception for Palimpsest image export errors."""

    def __init__(self, status_code: int, detail: str, code: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code or "image_export_error"


class ImageExportLeaseLost(RuntimeError):
    """The worker no longer owns the job and must not publish any state."""


class ImageExportNotFound(ImageExportError):
    """Requested export job was not found."""

    def __init__(self, detail: str = "Image export job not found"):
        super().__init__(status_code=404, detail=detail, code="export_not_found")


def _is_retryable_transaction_error(exc: OperationalError) -> bool:
    """Recognize database deadlock/serialization failures safe to retry."""
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    code = args[0] if args else None
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return code in {1205, 1213, "40001", "40P01"} or sqlstate in {"40001", "40P01"}


def compute_source_fingerprint(
    image_id: str,
    disk_format: str,
    size_bytes: int,
    virtual_size_bytes: int | None,
    checksum: str | None,
    hash_algo: str | None,
    hash_value: str | None,
    updated_at: str | None,
) -> str:
    """Compute canonical SHA-256 fingerprint for Glance source image snapshot."""
    payload = {
        "checksum": checksum,
        "disk_format": disk_format,
        "hash_algo": hash_algo,
        "hash_value": hash_value,
        "id": image_id,
        "size_bytes": size_bytes,
        "updated_at": updated_at,
        "virtual_size_bytes": virtual_size_bytes,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compute_artifact_key(source_fingerprint: str, target_disk_format: str) -> str:
    """Compute canonical SHA-256 key for target disk conversion output."""
    payload = {
        "converter_contract": CONVERTER_CONTRACT,
        "source_fingerprint": source_fingerprint,
        "target_disk_format": target_disk_format,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def serialize_export(row: PalimpsestImageExport) -> dict[str, Any]:
    """Serialize PalimpsestImageExport row into the public API contract."""
    complete = row.status == STATUS_COMPLETE and row.result_blob_digest is not None
    source_prefix = row.source_image_id.replace("-", "")[:12]
    spec = IMAGE_FORMAT_SPECS.get(row.target_disk_format)
    filename = (
        f"palimpsest-{source_prefix}-{row.artifact_key[:12]}.{spec.extension}"
        if complete and spec is not None
        else None
    )
    return {
        "id": row.id,
        "source_image_id": row.source_image_id,
        "source_name": row.source_name,
        "source_disk_format": row.source_disk_format,
        "source_size_bytes": row.source_size_bytes,
        "target_disk_format": row.target_disk_format,
        "status": row.status,
        "progress_pct": row.progress_pct,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "blob_digest": row.result_blob_digest,
        "size_bytes": row.result_size_bytes,
        "filename": filename,
        "download_path": f"/v1/image-exports/{row.id}/blob" if complete else None,
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


async def enqueue_image_export(
    conn: openstack.connection.Connection,
    token_info: dict[str, Any],
    image_id: str,
    target_disk_format: str,
) -> PalimpsestImageExport:
    """Enqueue a new Glance image export job for the requester's project."""
    if target_disk_format not in IMAGE_FORMAT_SPECS:
        raise ImageExportError(
            status_code=422,
            detail=f"Unsupported target disk format: {target_disk_format!r}",
            code="invalid_target_format",
        )

    try:
        img = await asyncio.to_thread(get_image, conn, image_id)
    except Exception as exc:
        raise ImageExportNotFound(f"Glance image {image_id!r} not found or inaccessible") from exc

    if not getattr(img, "id", None) or not getattr(img, "name", None):
        raise ImageExportError(
            status_code=422,
            detail="Glance image must have a non-empty ID and name",
            code="invalid_image_metadata",
        )

    if getattr(img, "status", None) != "active":
        raise ImageExportError(
            status_code=409,
            detail=f"Glance image {image_id!r} is not active (status={getattr(img, 'status', None)!r})",
            code="image_not_active",
        )

    if img.disk_format not in IMAGE_FORMAT_SPECS:
        raise ImageExportError(
            status_code=422,
            detail=f"Unsupported source disk format: {img.disk_format!r}",
            code="unsupported_source_format",
        )

    if not img.size or img.size <= 0:
        raise ImageExportError(
            status_code=422,
            detail="Source image size must be greater than zero",
            code="invalid_image_size",
        )

    settings = get_settings()
    max_bytes = settings.palimpsest_hub_max_blob_bytes
    if img.size > max_bytes:
        raise ImageExportError(
            status_code=413,
            detail=f"Source image size ({img.size} bytes) exceeds limit ({max_bytes} bytes)",
            code="image_too_large",
        )

    project_id = token_info.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ImageExportError(status_code=401, detail="Project scope is required", code="project_scope_required")
    user_id = token_info.get("user_id")

    source_fingerprint = compute_source_fingerprint(
        image_id=img.id,
        disk_format=img.disk_format,
        size_bytes=img.size,
        virtual_size_bytes=getattr(img, "virtual_size", None),
        checksum=getattr(img, "checksum", None),
        hash_algo=getattr(img, "os_hash_algo", None),
        hash_value=getattr(img, "os_hash_value", None),
        updated_at=getattr(img, "updated_at", None),
    )
    artifact_key = compute_artifact_key(source_fingerprint, target_disk_format)

    factory = get_session_factory()
    if factory is None:
        raise ImageExportError(status_code=503, detail="Database connection unavailable", code="db_unavailable")

    blob_store = get_blob_store()

    # Pre-flight query to identify digests needing file liveness checks outside DB transaction
    valid_same_digest: str | None = None
    valid_global_digest: str | None = None
    global_result_size: int | None = None

    async with factory() as session:
        stmt_same = select(PalimpsestImageExport).where(
            PalimpsestImageExport.project_id == project_id,
            PalimpsestImageExport.artifact_key == artifact_key,
        )
        same_pre = (await session.execute(stmt_same)).scalar_one_or_none()
        if same_pre is not None and same_pre.status == STATUS_COMPLETE and same_pre.deleted_at is None:
            # The requester has proved current Glance visibility via get_image(),
            # and this completed row already belongs to the same project.
            valid_same_digest = same_pre.result_blob_digest
        elif same_pre is None:
            # Requester-scoped Glance visibility authorizes reuse of bytes only;
            # the other project's row and authorization are never reused.
            stmt_global = (
                select(PalimpsestImageExport)
                .where(
                    PalimpsestImageExport.artifact_key == artifact_key,
                    PalimpsestImageExport.deleted_at.is_(None),
                    PalimpsestImageExport.status == STATUS_COMPLETE,
                    PalimpsestImageExport.result_blob_digest.isnot(None),
                )
                .limit(1)
            )
            global_pre = (await session.execute(stmt_global)).scalar_one_or_none()
            if global_pre is not None:
                valid_global_digest = global_pre.result_blob_digest
                global_result_size = global_pre.result_size_bytes

    # Filesystem liveness checks performed completely OUTSIDE DB transaction
    same_blob_present = blob_store.exists(valid_same_digest) if valid_same_digest else False
    global_blob_present = blob_store.exists(valid_global_digest) if valid_global_digest else False
    if valid_global_digest and global_blob_present:
        global_result_size = blob_store.size(valid_global_digest)

    async def _write_transaction() -> PalimpsestImageExport:
        async with factory() as session, session.begin():
            # Lock the project's indexed key range so concurrent requests for
            # different artifacts cannot both create nonterminal work.
            await session.execute(
                select(PalimpsestImageExport.id).where(PalimpsestImageExport.project_id == project_id).with_for_update()
            )
            # 1. Enforce at most one nonterminal job per project
            stmt_active = select(PalimpsestImageExport).where(
                PalimpsestImageExport.project_id == project_id,
                PalimpsestImageExport.deleted_at.is_(None),
                PalimpsestImageExport.status.notin_([STATUS_COMPLETE, STATUS_ERROR]),
            )
            active_job = (await session.execute(stmt_active)).scalars().first()
            if active_job is not None:
                raise ImageExportError(
                    status_code=409,
                    detail="Project already has an active export job in progress",
                    code="active_export_exists",
                )

            # 2. Check for existing same-project job for (project_id, artifact_key)
            stmt_same = select(PalimpsestImageExport).where(
                PalimpsestImageExport.project_id == project_id,
                PalimpsestImageExport.artifact_key == artifact_key,
            )
            same_row = (await session.execute(stmt_same)).scalar_one_or_none()
            if same_row is not None:
                if (
                    same_row.status == STATUS_COMPLETE
                    and same_row.deleted_at is None
                    and same_row.result_blob_digest == valid_same_digest
                    and same_blob_present
                ):
                    return same_row

                # Reset soft-deleted, error, or missing-blob row to queued
                now = _now()
                same_row.status = STATUS_QUEUED
                same_row.progress_pct = PROGRESS_QUEUED
                same_row.error_code = None
                same_row.error_message = None
                same_row.attempts = 0
                same_row.next_at = now
                same_row.lease_owner = None
                same_row.lease_expires_at = None
                same_row.started_at = None
                same_row.completed_at = None
                same_row.result_blob_digest = None
                same_row.result_size_bytes = None
                same_row.deleted_at = None
                same_row.created_by = user_id
                same_row.source_image_id = img.id
                same_row.source_name = img.name
                same_row.source_disk_format = img.disk_format
                same_row.source_size_bytes = img.size
                same_row.source_virtual_size_bytes = getattr(img, "virtual_size", None)
                same_row.source_checksum = getattr(img, "checksum", None)
                same_row.source_hash_algo = getattr(img, "os_hash_algo", None)
                same_row.source_hash_value = getattr(img, "os_hash_value", None)
                same_row.source_updated_at = getattr(img, "updated_at", None)
                same_row.updated_at = now
                return same_row

            # 3. Check for global completed artifact with present blob
            if valid_global_digest and global_blob_present:
                now = _now()
                new_row = PalimpsestImageExport(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    created_by=user_id,
                    source_image_id=img.id,
                    source_name=img.name,
                    source_disk_format=img.disk_format,
                    source_size_bytes=img.size,
                    source_virtual_size_bytes=getattr(img, "virtual_size", None),
                    source_checksum=getattr(img, "checksum", None),
                    source_hash_algo=getattr(img, "os_hash_algo", None),
                    source_hash_value=getattr(img, "os_hash_value", None),
                    source_updated_at=getattr(img, "updated_at", None),
                    source_fingerprint=source_fingerprint,
                    artifact_key=artifact_key,
                    target_disk_format=target_disk_format,
                    result_blob_digest=valid_global_digest,
                    result_size_bytes=global_result_size,
                    status=STATUS_COMPLETE,
                    progress_pct=PROGRESS_COMPLETE,
                    attempts=0,
                    next_at=now,
                    created_at=now,
                    updated_at=now,
                    started_at=now,
                    completed_at=now,
                )
                session.add(new_row)
                return new_row

            # 4. Insert new queued job
            now = _now()
            new_row = PalimpsestImageExport(
                id=str(uuid.uuid4()),
                project_id=project_id,
                created_by=user_id,
                source_image_id=img.id,
                source_name=img.name,
                source_disk_format=img.disk_format,
                source_size_bytes=img.size,
                source_virtual_size_bytes=getattr(img, "virtual_size", None),
                source_checksum=getattr(img, "checksum", None),
                source_hash_algo=getattr(img, "os_hash_algo", None),
                source_hash_value=getattr(img, "os_hash_value", None),
                source_updated_at=getattr(img, "updated_at", None),
                source_fingerprint=source_fingerprint,
                artifact_key=artifact_key,
                target_disk_format=target_disk_format,
                status=STATUS_QUEUED,
                progress_pct=PROGRESS_QUEUED,
                attempts=0,
                next_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(new_row)
            return new_row

    reuse_digest = valid_same_digest or valid_global_digest
    reuse_lock_fd: int | None = None
    try:
        if reuse_digest:
            reuse_lock_fd = await asyncio.to_thread(blob_store.acquire_blob_lock, reuse_digest)
            if valid_same_digest:
                same_blob_present = blob_store.exists(valid_same_digest)
            if valid_global_digest:
                global_blob_present = blob_store.exists(valid_global_digest)
                if global_blob_present:
                    global_result_size = blob_store.size(valid_global_digest)
        for attempt in range(3):
            try:
                return await _write_transaction()
            except OperationalError as exc:
                if not _is_retryable_transaction_error(exc) or attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeError("Unreachable transaction retry state")
    except IntegrityError:
        # Explicit race recovery preflight outside transaction
        async with factory() as session:
            stmt_same = select(PalimpsestImageExport).where(
                PalimpsestImageExport.project_id == project_id,
                PalimpsestImageExport.artifact_key == artifact_key,
            )
            same_race = (await session.execute(stmt_same)).scalar_one_or_none()

        if same_race is None:
            raise

        race_digest = same_race.result_blob_digest if same_race.status == STATUS_COMPLETE else None
        race_present = blob_store.exists(race_digest) if race_digest else False

        async with factory() as session, session.begin():
            target = (await session.execute(stmt_same.with_for_update())).scalar_one()
            if (
                target.status == STATUS_COMPLETE
                and target.deleted_at is None
                and target.result_blob_digest == race_digest
                and race_present
            ):
                return target
            if target.status not in (STATUS_COMPLETE, STATUS_ERROR) and target.deleted_at is None:
                # The winner may already be claimed. Preserve its lease and
                # attempts instead of resetting live work after a duplicate insert.
                return target

            now = _now()
            target.status = STATUS_QUEUED
            target.progress_pct = PROGRESS_QUEUED
            target.error_code = None
            target.error_message = None
            target.attempts = 0
            target.next_at = now
            target.lease_owner = None
            target.lease_expires_at = None
            target.started_at = None
            target.completed_at = None
            target.result_blob_digest = None
            target.result_size_bytes = None
            target.deleted_at = None
            target.created_by = user_id
            target.source_image_id = img.id
            target.source_name = img.name
            target.source_disk_format = img.disk_format
            target.source_size_bytes = img.size
            target.source_virtual_size_bytes = getattr(img, "virtual_size", None)
            target.source_checksum = getattr(img, "checksum", None)
            target.source_hash_algo = getattr(img, "os_hash_algo", None)
            target.source_hash_value = getattr(img, "os_hash_value", None)
            target.source_updated_at = getattr(img, "updated_at", None)
            target.updated_at = now
            return target
    finally:
        if reuse_lock_fd is not None:
            await asyncio.to_thread(blob_store.release_blob_lock, reuse_lock_fd)


async def list_project_exports(
    project_id: str,
    *,
    source_image_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[PalimpsestImageExport]:
    """List non-deleted export jobs for a project."""
    factory = get_session_factory()
    if factory is None:
        raise ImageExportError(status_code=503, detail="Database connection unavailable", code="db_unavailable")
    async with factory() as session:
        stmt = select(PalimpsestImageExport).where(
            PalimpsestImageExport.project_id == project_id,
            PalimpsestImageExport.deleted_at.is_(None),
        )
        if source_image_id:
            stmt = stmt.where(PalimpsestImageExport.source_image_id == source_image_id)
        if status:
            stmt = stmt.where(PalimpsestImageExport.status == status)
        stmt = stmt.order_by(desc(PalimpsestImageExport.created_at)).limit(min(limit, 100))
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_project_export(project_id: str, export_id: str) -> PalimpsestImageExport:
    """Get a specific non-deleted export job for a project."""
    factory = get_session_factory()
    if factory is None:
        raise ImageExportError(status_code=503, detail="Database connection unavailable", code="db_unavailable")
    async with factory() as session:
        stmt = select(PalimpsestImageExport).where(
            PalimpsestImageExport.project_id == project_id,
            PalimpsestImageExport.id == export_id,
            PalimpsestImageExport.deleted_at.is_(None),
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ImageExportNotFound(f"Image export {export_id!r} not found for project {project_id!r}")
        return row


async def soft_delete_project_export(project_id: str, export_id: str) -> PalimpsestImageExport:
    """Soft delete a terminal, unclaimed queued, or expired-lease export job."""
    factory = get_session_factory()
    if factory is None:
        raise ImageExportError(status_code=503, detail="Database connection unavailable", code="db_unavailable")
    async with factory() as session, session.begin():
        stmt = (
            select(PalimpsestImageExport)
            .where(
                PalimpsestImageExport.project_id == project_id,
                PalimpsestImageExport.id == export_id,
                PalimpsestImageExport.deleted_at.is_(None),
            )
            .with_for_update()
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ImageExportNotFound(f"Image export {export_id!r} not found for project {project_id!r}")
        now = _now()
        terminal = row.status in (STATUS_COMPLETE, STATUS_ERROR)
        unclaimed = row.status == STATUS_QUEUED and row.lease_owner is None
        expired = row.lease_expires_at is not None and row.lease_expires_at <= now
        if not (terminal or unclaimed or expired):
            raise ImageExportError(
                status_code=409,
                detail="A currently leased export job cannot be deleted",
                code="cannot_delete_active_job",
            )
        row.deleted_at = now
        row.updated_at = now
        return row


def build_qemu_img_convert_command(
    source: Path,
    target: Path,
    source_format: str,
    target_format: str,
) -> list[str]:
    """Build qemu-img convert command line arguments."""
    src_driver = IMAGE_FORMAT_SPECS[source_format].qemu_driver
    tgt_driver = IMAGE_FORMAT_SPECS[target_format].qemu_driver
    return ["qemu-img", "convert", "-f", src_driver, "-O", tgt_driver, str(source), str(target)]


async def _update_job_cas(
    session: AsyncSession,
    job_id: str,
    owner: str,
    *,
    status: str | None = None,
    progress_pct: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    result_blob_digest: str | None = None,
    result_size_bytes: int | None = None,
    completed_at: datetime | None = None,
    clear_lease: bool = False,
    extend_lease_seconds: int = 120,
) -> bool:
    """Perform a Compare-And-Swap database update conditioned on job ID + lease_owner."""
    now = _now()
    values: dict[str, Any] = {"updated_at": now}
    if status is not None:
        values["status"] = status
    if progress_pct is not None:
        values["progress_pct"] = progress_pct
    if error_code is not None:
        values["error_code"] = error_code
    if error_message is not None:
        values["error_message"] = error_message
    if result_blob_digest is not None:
        values["result_blob_digest"] = result_blob_digest
    if result_size_bytes is not None:
        values["result_size_bytes"] = result_size_bytes
    if completed_at is not None:
        values["completed_at"] = completed_at

    if clear_lease:
        values["lease_owner"] = None
        values["lease_expires_at"] = None
    else:
        values["lease_expires_at"] = now + timedelta(seconds=extend_lease_seconds)
        values["next_at"] = now + timedelta(seconds=extend_lease_seconds)

    stmt = (
        update(PalimpsestImageExport)
        .where(
            PalimpsestImageExport.id == job_id,
            PalimpsestImageExport.lease_owner == owner,
            PalimpsestImageExport.deleted_at.is_(None),
        )
        .values(**values)
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def claim_next_image_export(*, owner: str) -> PalimpsestImageExport | None:
    """Claim due queued job or expired lease using SELECT FOR UPDATE SKIP LOCKED."""
    factory = get_session_factory()
    if factory is None:
        return None

    now = _now()
    lease_ttl = timedelta(seconds=120)
    async with factory() as session:
        candidate: PalimpsestImageExport | None = None
        async with session.begin():
            stmt = (
                select(PalimpsestImageExport)
                .where(
                    PalimpsestImageExport.deleted_at.is_(None),
                    or_(
                        and_(
                            PalimpsestImageExport.status == STATUS_QUEUED,
                            PalimpsestImageExport.next_at <= now,
                        ),
                        and_(
                            PalimpsestImageExport.status.notin_([STATUS_COMPLETE, STATUS_ERROR]),
                            PalimpsestImageExport.lease_expires_at.isnot(None),
                            PalimpsestImageExport.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(PalimpsestImageExport.next_at.asc(), PalimpsestImageExport.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            candidate = (await session.execute(stmt)).scalar_one_or_none()
            if candidate is None:
                return None

            # Attempt limit check: 4th claim triggers terminal error
            if candidate.attempts >= 3:
                candidate.status = STATUS_ERROR
                candidate.error_code = "attempts_exhausted"
                candidate.error_message = "Export job exceeded maximum retry attempts"
                candidate.lease_owner = None
                candidate.lease_expires_at = None
                candidate.completed_at = now
                candidate.updated_at = now
                return None

            candidate.attempts += 1
            candidate.lease_owner = owner
            candidate.lease_expires_at = now + lease_ttl
            candidate.next_at = now + lease_ttl
            candidate.updated_at = now
            if candidate.started_at is None:
                candidate.started_at = now
            candidate.status = STATUS_DOWNLOADING
            candidate.progress_pct = PROGRESS_DOWNLOADING

        return candidate


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int = 1024 * 1024) -> bytes:
    if stream is None:
        return b""
    captured = bytearray()
    while chunk := await stream.read(64 * 1024):
        if len(captured) < limit:
            captured.extend(chunk[: limit - len(captured)])
    return bytes(captured)


async def _communicate_bounded(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    async with asyncio.TaskGroup() as group:
        stdout_task = group.create_task(_read_bounded(proc.stdout))
        stderr_task = group.create_task(_read_bounded(proc.stderr))
        group.create_task(proc.wait())
    return stdout_task.result(), stderr_task.result()


async def _run_subprocess(
    cmd: list[str],
    *,
    timeout: float,
    scratch_dir: Path,
    lease_lost: asyncio.Event,
) -> tuple[int, str, str]:
    """Execute argv with a minimal environment and abort immediately on lease loss."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TMPDIR": str(scratch_dir)},
    )
    communicate_task = asyncio.create_task(_communicate_bounded(proc))
    lease_task = asyncio.create_task(lease_lost.wait())
    try:
        done, _ = await asyncio.wait(
            {communicate_task, lease_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await _terminate_process(proc)
            raise TimeoutError
        if lease_lost.is_set():
            if communicate_task not in done:
                await _terminate_process(proc)
            raise ImageExportLeaseLost
        stdout_b, stderr_b = await communicate_task
        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        for task in (communicate_task, lease_task):
            if not task.done():
                task.cancel()
        for task in (communicate_task, lease_task):
            with suppress(asyncio.CancelledError):
                await task


def _has_external_reference(payload: Any) -> bool:
    if isinstance(payload, dict):
        if {"backing-filename", "full-backing-filename", "data-file"} & payload.keys():
            return True
        return any(_has_external_reference(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_has_external_reference(value) for value in payload)
    return False


def _remove_scratch_dir(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _prepare_scratch_dir(path: Path, exports_dir: Path) -> None:
    exports_dir.mkdir(parents=True, exist_ok=True)
    _remove_scratch_dir(path)
    path.mkdir(mode=0o700)
    if path.resolve().parent != exports_dir.resolve():
        _remove_scratch_dir(path)
        raise RuntimeError("Export scratch path escaped the configured hub directory")


def _scratch_dir_for(exports_dir: Path, job: PalimpsestImageExport, owner: str) -> Path:
    owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:12]
    return exports_dir / f"{job.id}-{job.attempts}-{owner_key}"


async def process_one_image_export(*, owner: str) -> bool:
    """Claim and execute one pending image export job. Returns True if a job was processed."""
    job = await claim_next_image_export(owner=owner)
    if job is None:
        return False

    settings = get_settings()
    factory = get_session_factory()
    blob_store = get_blob_store()
    scratch_dir = _scratch_dir_for(blob_store.exports_dir, job, owner)

    # Every claim gets an owner/attempt-specific directory. A worker that loses
    # its lease may clean only its own files, never the reclaimer's active work.
    await asyncio.to_thread(_prepare_scratch_dir, scratch_dir, blob_store.exports_dir)

    heartbeat_stop = asyncio.Event()
    lease_lost = asyncio.Event()

    async def _heartbeat():
        while not heartbeat_stop.is_set():
            try:
                await asyncio.sleep(30)
                if heartbeat_stop.is_set():
                    break
                if factory is None:
                    lease_lost.set()
                    break
                async with factory() as session, session.begin():
                    ok = await _update_job_cas(session, job.id, owner, extend_lease_seconds=120)
                    if not ok:
                        _logger.warning("Heartbeat CAS lost lease for export %s", job.id)
                        lease_lost.set()
                        break
            except Exception:
                _logger.warning("Heartbeat failed for export %s", job.id, exc_info=True)
                lease_lost.set()
                break

    heartbeat_task = asyncio.create_task(_heartbeat())

    admin_conn: openstack.connection.Connection | None = None
    err_code = "export_failed"
    err_msg = "An unexpected error occurred during image export"

    try:
        # 1. Obtain project-scoped OpenStack connection & recheck Glance image authorization/revision
        try:
            admin_conn = await asyncio.to_thread(get_admin_connection_for_project, job.project_id)
        except Exception as exc:
            err_code = "access_denied"
            err_msg = "Unable to establish project-scoped OpenStack access"
            raise ImageExportError(403, err_msg, code=err_code) from exc

        try:
            img = await asyncio.to_thread(get_image, admin_conn, job.source_image_id)
        except Exception as exc:
            err_code = "image_unavailable"
            err_msg = f"Source Glance image {job.source_image_id!r} is unavailable"
            raise ImageExportError(404, err_msg, code=err_code) from exc

        if getattr(img, "status", None) != "active":
            err_code = "image_not_active"
            err_msg = f"Source Glance image status is {getattr(img, 'status', None)!r}, expected active"
            raise ImageExportError(409, err_msg, code=err_code)

        # Check authorization
        img_owner = getattr(img, "owner", None) or getattr(img, "project_id", None)
        img_visibility = getattr(img, "visibility", None)
        authorized = False

        if img_owner == job.project_id or img_visibility in ("public", "community"):
            authorized = True
        elif img_visibility == "shared":
            try:
                members = await asyncio.to_thread(lambda: list(admin_conn.image.members(img.id)))
                for m in members:
                    m_id = getattr(m, "member_id", None) or getattr(m, "id", None)
                    m_status = getattr(m, "status", None)
                    if m_id == job.project_id and m_status == "accepted":
                        authorized = True
                        break
            except Exception:
                _logger.warning("Failed to verify Glance image membership for export %s", job.id, exc_info=True)

        if not authorized:
            err_code = "access_denied"
            err_msg = "Image authorization revoked or invalid for project"
            raise ImageExportError(403, err_msg, code=err_code)

        # Compare immutable fields
        if (
            img.disk_format != job.source_disk_format
            or img.size != job.source_size_bytes
            or getattr(img, "virtual_size", None) != job.source_virtual_size_bytes
            or getattr(img, "checksum", None) != job.source_checksum
            or getattr(img, "os_hash_algo", None) != job.source_hash_algo
            or getattr(img, "os_hash_value", None) != job.source_hash_value
            or str(getattr(img, "updated_at", None)) != str(job.source_updated_at)
        ):
            err_code = "source_image_modified"
            err_msg = "Source image metadata or content has changed since export was queued"
            raise ImageExportError(409, err_msg, code=err_code)

        # 2. Check disk space for download (source + 1 GiB)
        free_bytes = shutil.disk_usage(scratch_dir).free
        required_download_space = job.source_size_bytes + (1 * 1024 * 1024 * 1024)
        if free_bytes < required_download_space:
            _logger.warning(
                "Insufficient export download storage for %s (free=%s required=%s)",
                job.id,
                free_bytes,
                required_download_space,
            )
            err_code = "insufficient_disk_space"
            err_msg = "Insufficient storage capacity for image export"
            raise ImageExportError(507, err_msg, code=err_code)
        # 3. Stream download into exclusive scratch file
        src_spec = IMAGE_FORMAT_SPECS[job.source_disk_format]
        source_file = scratch_dir / f"source.{src_spec.extension}"
        fd = os.open(source_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

        sha256 = hashlib.sha256()
        sha512 = hashlib.sha512()
        md5 = hashlib.md5()  # noqa: S324 — Glance MD5 checksum verification
        total_downloaded = 0
        max_allowed = settings.palimpsest_hub_max_blob_bytes

        def _do_download():
            nonlocal total_downloaded
            with os.fdopen(fd, "wb") as out_f:
                resp = admin_conn.image.download_image(job.source_image_id, stream=True)
                try:
                    for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                        if not chunk:
                            continue
                        total_downloaded += len(chunk)
                        if total_downloaded > max_allowed:
                            raise ImageExportError(
                                status_code=413,
                                detail="Downloaded image bytes exceed maximum allowed size",
                                code="image_too_large",
                            )
                        sha256.update(chunk)
                        sha512.update(chunk)
                        md5.update(chunk)
                        out_f.write(chunk)
                finally:
                    if hasattr(resp, "close"):
                        with suppress(Exception):
                            resp.close()

        await asyncio.to_thread(_do_download)
        if lease_lost.is_set():
            raise ImageExportLeaseLost

        if total_downloaded != job.source_size_bytes:
            err_code = "image_size_mismatch"
            err_msg = f"Downloaded size ({total_downloaded}) does not match expected size ({job.source_size_bytes})"
            raise ImageExportError(400, err_msg, code=err_code)

        # Verify the strongest supported Glance hash, falling back to checksum.
        if job.source_hash_algo in {"sha256", "sha512"}:
            digest = sha256.hexdigest() if job.source_hash_algo == "sha256" else sha512.hexdigest()
            if not job.source_hash_value or not hmac.compare_digest(digest, job.source_hash_value):
                err_code = "checksum_mismatch"
                err_msg = f"Downloaded image {job.source_hash_algo} digest mismatch"
                raise ImageExportError(400, err_msg, code=err_code)
        elif job.source_checksum:
            if not hmac.compare_digest(md5.hexdigest(), job.source_checksum):
                err_code = "checksum_mismatch"
                err_msg = "Downloaded image checksum mismatch"
                raise ImageExportError(400, err_msg, code=err_code)
        else:
            err_code = "checksum_unavailable"
            err_msg = "Glance image does not provide a supported integrity digest"
            raise ImageExportError(409, err_msg, code=err_code)

        # 4. Source qemu-img info inspection
        src_driver = IMAGE_FORMAT_SPECS[job.source_disk_format].qemu_driver
        tgt_driver = IMAGE_FORMAT_SPECS[job.target_disk_format].qemu_driver

        cmd_info = ["qemu-img", "info", "--output=json", "-f", src_driver, str(source_file)]
        try:
            code, stdout_str, stderr_str = await _run_subprocess(
                cmd_info, timeout=30.0, scratch_dir=scratch_dir, lease_lost=lease_lost
            )
        except TimeoutError as exc:
            err_code = "inspection_timeout"
            err_msg = "Source image inspection timed out"
            raise ImageExportError(504, err_msg, code=err_code) from exc
        if code != 0:
            _logger.warning("qemu-img source inspection failed for export %s: %s", job.id, stderr_str[-1000:])
            err_code = "invalid_image_format"
            err_msg = "Source image format inspection failed"
            raise ImageExportError(400, err_msg, code=err_code)

        try:
            info_data = json.loads(stdout_str)
        except Exception as exc:
            err_code = "invalid_image_format"
            err_msg = "Source image inspection returned invalid metadata"
            raise ImageExportError(400, err_msg, code=err_code) from exc

        if info_data.get("format") != src_driver:
            err_code = "invalid_image_format"
            err_msg = f"qemu-img format mismatch (reported {info_data.get('format')!r}, expected {src_driver!r})"
            raise ImageExportError(400, err_msg, code=err_code)

        if _has_external_reference(info_data):
            err_code = "unsafe_backing_file"
            err_msg = "Source image contains a prohibited external file reference"
            raise ImageExportError(400, err_msg, code=err_code)

        # 5. Measure target allocation where qemu supports it. Other public
        # drivers reject `measure`, so reserve the full virtual size instead.
        # The additional 1 GiB below covers container metadata and filesystem
        # allocation overhead while keeping the fallback fail-closed.
        virtual_size = info_data.get("virtual-size")
        if not isinstance(virtual_size, int) or isinstance(virtual_size, bool) or virtual_size < 0:
            err_code = "invalid_image_format"
            err_msg = "Source image inspection returned an invalid virtual size"
            raise ImageExportError(400, err_msg, code=err_code)

        required_val = virtual_size
        if tgt_driver in QEMU_MEASURE_DRIVERS:
            cmd_measure = [
                "qemu-img",
                "measure",
                "--output=json",
                "-f",
                src_driver,
                "-O",
                tgt_driver,
                str(source_file),
            ]
            try:
                code, stdout_str, stderr_str = await _run_subprocess(
                    cmd_measure, timeout=30.0, scratch_dir=scratch_dir, lease_lost=lease_lost
                )
            except TimeoutError as exc:
                err_code = "measurement_timeout"
                err_msg = "Conversion storage measurement timed out"
                raise ImageExportError(504, err_msg, code=err_code) from exc
            if code != 0:
                _logger.warning("qemu-img measure failed for export %s: %s", job.id, stderr_str[-1000:])
                err_code = "measurement_failed"
                err_msg = "Unable to measure required conversion storage"
                raise ImageExportError(400, err_msg, code=err_code)

            try:
                m_data = json.loads(stdout_str)
            except Exception as exc:
                err_code = "measurement_failed"
                err_msg = "Conversion measurement returned invalid metadata"
                raise ImageExportError(400, err_msg, code=err_code) from exc

            required_val = m_data.get("required")
            if required_val is None:
                required_val = m_data.get("required-size")

            if not isinstance(required_val, int) or isinstance(required_val, bool) or required_val < 0:
                err_code = "measurement_failed"
                err_msg = f"Invalid required size in qemu-img measure output ({required_val!r})"
                raise ImageExportError(400, err_msg, code=err_code)

        free_bytes = shutil.disk_usage(scratch_dir).free
        required_space = required_val + (1 * 1024 * 1024 * 1024)
        if free_bytes < required_space:
            _logger.warning(
                "Insufficient export conversion storage for %s (free=%s required=%s)",
                job.id,
                free_bytes,
                required_space,
            )
            err_code = "insufficient_disk_space"
            err_msg = "Insufficient storage capacity for image export"
            raise ImageExportError(507, err_msg, code=err_code)

        tgt_spec = IMAGE_FORMAT_SPECS[job.target_disk_format]
        target_file = source_file if src_driver == tgt_driver else scratch_dir / f"target.{tgt_spec.extension}"

        # 6. Perform conversion only when qemu drivers differ.
        if src_driver != tgt_driver:
            if factory:
                async with factory() as session, session.begin():
                    ok = await _update_job_cas(
                        session, job.id, owner, status=STATUS_CONVERTING, progress_pct=PROGRESS_CONVERTING
                    )
                    if not ok:
                        lease_lost.set()
                        raise ImageExportLeaseLost

            cmd_convert = build_qemu_img_convert_command(
                source_file, target_file, job.source_disk_format, job.target_disk_format
            )
            try:
                code, _, stderr_str = await _run_subprocess(
                    cmd_convert, timeout=3600.0, scratch_dir=scratch_dir, lease_lost=lease_lost
                )
                if code != 0:
                    _logger.warning("qemu-img conversion failed for export %s: %s", job.id, stderr_str[-1000:])
                    err_code = "conversion_failed"
                    err_msg = "Image conversion failed"
                    raise ImageExportError(500, err_msg, code=err_code)
            except TimeoutError as exc:
                err_code = "conversion_timeout"
                err_msg = "qemu-img convert operation timed out after 3600 seconds"
                raise ImageExportError(504, err_msg, code=err_code) from exc

        # 7. Output Validation
        if factory:
            async with factory() as session, session.begin():
                ok = await _update_job_cas(
                    session, job.id, owner, status=STATUS_FINALIZING, progress_pct=PROGRESS_FINALIZING
                )
                if not ok:
                    lease_lost.set()
                    raise ImageExportLeaseLost

        cmd_out_info = ["qemu-img", "info", "--output=json", "-f", tgt_driver, str(target_file)]
        try:
            code, stdout_str, stderr_str = await _run_subprocess(
                cmd_out_info, timeout=30.0, scratch_dir=scratch_dir, lease_lost=lease_lost
            )
        except TimeoutError as exc:
            err_code = "inspection_timeout"
            err_msg = "Converted image inspection timed out"
            raise ImageExportError(504, err_msg, code=err_code) from exc
        if code != 0:
            _logger.warning("qemu-img output inspection failed for export %s: %s", job.id, stderr_str[-1000:])
            err_code = "conversion_failed"
            err_msg = "Converted image validation failed"
            raise ImageExportError(500, err_msg, code=err_code)

        try:
            out_info_data = json.loads(stdout_str)
        except Exception as exc:
            err_code = "conversion_failed"
            err_msg = "Converted image validation returned invalid metadata"
            raise ImageExportError(500, err_msg, code=err_code) from exc

        if out_info_data.get("format") != tgt_driver:
            err_code = "conversion_failed"
            err_msg = f"Converted format mismatch (reported {out_info_data.get('format')!r}, expected {tgt_driver!r})"
            raise ImageExportError(500, err_msg, code=err_code)

        if _has_external_reference(out_info_data):
            err_code = "unsafe_backing_file"
            err_msg = "Converted image contains a prohibited external file reference"
            raise ImageExportError(500, err_msg, code=err_code)

        output_size = target_file.stat().st_size
        if output_size > settings.palimpsest_hub_max_blob_bytes:
            err_code = "image_too_large"
            err_msg = f"Converted image size ({output_size} bytes) exceeds maximum limit ({settings.palimpsest_hub_max_blob_bytes} bytes)"
            raise ImageExportError(413, err_msg, code=err_code)

        # 8. Promotion & Final Completion
        if lease_lost.is_set():
            raise ImageExportLeaseLost
        if factory:
            async with factory() as session:
                now = _now()
                stmt = select(PalimpsestImageExport).where(
                    PalimpsestImageExport.id == job.id,
                    PalimpsestImageExport.lease_owner == owner,
                    PalimpsestImageExport.lease_expires_at > now,
                    PalimpsestImageExport.deleted_at.is_(None),
                )
                held = (await session.execute(stmt)).scalar_one_or_none()
                if held is None:
                    lease_lost.set()
                    raise ImageExportLeaseLost

        finalized = await asyncio.to_thread(
            blob_store.promote_file, target_file, max_bytes=settings.palimpsest_hub_max_blob_bytes
        )

        if factory:
            async with factory() as session, session.begin():
                ok = await _update_job_cas(
                    session,
                    job.id,
                    owner,
                    status=STATUS_COMPLETE,
                    progress_pct=PROGRESS_COMPLETE,
                    result_blob_digest=finalized.blob_digest,
                    result_size_bytes=finalized.size_bytes,
                    completed_at=_now(),
                    clear_lease=True,
                )
                if not ok:
                    _logger.error("Final CAS failed to publish completion for export %s", job.id)

        return True

    except ImageExportLeaseLost:
        _logger.warning("Palimpsest export job %s lost its lease; leaving it for reclaim", job.id)
        return True

    except Exception as exc:
        _logger.warning("Palimpsest export job %s failed: %s", job.id, exc, exc_info=True)
        if isinstance(exc, ImageExportError):
            err_code = exc.code
            err_msg = exc.detail

        if factory:
            try:
                async with factory() as session, session.begin():
                    # Retain prior progress_pct on error
                    await _update_job_cas(
                        session,
                        job.id,
                        owner,
                        status=STATUS_ERROR,
                        error_code=err_code,
                        error_message=err_msg,
                        clear_lease=True,
                    )
            except Exception:
                _logger.error("Failed to write error status for job %s", job.id, exc_info=True)

        return True

    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        if admin_conn is not None:
            try:
                await asyncio.to_thread(admin_conn.close)
            except Exception:
                _logger.debug("Failed to close export OpenStack connection", exc_info=True)
        await asyncio.to_thread(_remove_scratch_dir, scratch_dir)


async def validate_qemu_img_support() -> None:
    """Fail worker startup unless qemu-img advertises every mapped driver."""
    proc = await asyncio.create_subprocess_exec(
        "qemu-img",
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TMPDIR": "/tmp"},
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except TimeoutError:
        await _terminate_process(proc)
        raise RuntimeError("qemu-img capability check timed out") from None
    output = f"{stdout_b.decode(errors='replace')}\n{stderr_b.decode(errors='replace')}"
    missing = sorted({spec.qemu_driver for spec in IMAGE_FORMAT_SPECS.values()} - set(output.split()))
    if proc.returncode != 0 or missing:
        raise RuntimeError(f"qemu-img does not advertise required formats: {', '.join(missing)}")


async def run_image_export_worker(*, owner: str | None = None) -> bool:
    """Runner alias for process_one_image_export."""
    if owner is None:
        owner = f"worker-anon-{uuid.uuid4().hex[:8]}"
    return await process_one_image_export(owner=owner)


async def run_export_maintenance(max_age_seconds: int = 86400) -> None:
    """Clean stale scratch and delete only locked, freshly rechecked unreferenced blobs."""
    try:
        blob_store = get_blob_store()
    except Exception:
        return

    now_epoch = time.time()

    if blob_store.exports_dir.is_dir():
        for entry in blob_store.exports_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                try:
                    if now_epoch - entry.stat().st_mtime > max_age_seconds:
                        shutil.rmtree(entry, ignore_errors=True)
                except Exception:
                    pass

    factory = get_session_factory()
    if factory is None or not blob_store.blobs_dir.is_dir():
        return

    deleted_after = _now() - timedelta(seconds=max_age_seconds)
    for entry in blob_store.blobs_dir.iterdir():
        if not entry.is_file() or entry.is_symlink() or len(entry.name) != 64:
            continue
        try:
            int(entry.name, 16)
            if now_epoch - entry.stat().st_mtime <= max_age_seconds:
                continue
        except (OSError, ValueError):
            continue

        digest = f"sha256:{entry.name}"
        lock_fd: int | None = None
        try:
            lock_fd = await asyncio.to_thread(blob_store.acquire_blob_lock, digest)
            if not entry.is_file() or entry.is_symlink():
                continue
            if now_epoch - entry.stat().st_mtime <= max_age_seconds:
                continue

            async with factory() as session:
                export_ref = (
                    await session.execute(
                        select(PalimpsestImageExport.id)
                        .where(
                            PalimpsestImageExport.result_blob_digest == digest,
                            or_(
                                PalimpsestImageExport.deleted_at.is_(None),
                                PalimpsestImageExport.deleted_at > deleted_after,
                            ),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if export_ref is not None:
                    continue

                layer_ref = (
                    await session.execute(
                        select(PalimpsestHubLayer.id).where(PalimpsestHubLayer.blob_digest == digest).limit(1)
                    )
                ).scalar_one_or_none()
                if layer_ref is not None:
                    continue

            entry.unlink(missing_ok=True)
        except Exception:
            _logger.warning("Failed Palimpsest blob GC for %s", digest, exc_info=True)
        finally:
            if lock_fd is not None:
                await asyncio.to_thread(blob_store.release_blob_lock, lock_fd)
