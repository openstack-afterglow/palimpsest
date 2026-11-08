"""Bounded lifetime and size of staged-but-unregistered publication payloads.

A staged payload is *registered for its stager* once an image published by the same
project and user uses its digest (an image layer membership or a source-closure blob). Registration by
another project never counts: otherwise the cap would reveal whether foreign bytes exist.
Expired unregistered rows are removed together with their ``image-stage`` blob reference,
so the existing export maintenance may reclaim the bytes; CAS bytes are never deleted here.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, select

from palimpsest_hub.image_models import (
    PalimpsestHubImageMembership,
    PalimpsestHubImageSourceBlob,
    PalimpsestHubPublicationPayload,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import RuntimeBlobReference, exact_identity

DEFAULT_MAX_BYTES = 200 * 1024**3
DEFAULT_TTL_SECONDS = 24 * 3600
_SWEEP_BATCH = 500


def reference_id(project: str, user_id: str) -> str:
    return hashlib.sha256((project + "\0" + user_id).encode("utf-8")).hexdigest()


def _registered(row=PalimpsestHubPublicationPayload):
    """Correlated condition: the row's digest is used by an image published by the same stager.

    The physical layer row is deliberately not consulted: it is unique per digest and records
    only the first registrant, so it would reveal (and depend on) other tenants' bytes.
    """
    own_image = and_(
        PalimpsestHubPublishedImage.project_id == row.project_id,
        PalimpsestHubPublishedImage.created_by == row.created_by,
    )
    layer = exists().where(
        PalimpsestHubImageMembership.blob_digest == row.blob_digest,
        PalimpsestHubImageMembership.image_id == PalimpsestHubPublishedImage.id,
        own_image,
    )
    source = exists().where(
        PalimpsestHubImageSourceBlob.blob_digest == row.blob_digest,
        PalimpsestHubImageSourceBlob.image_id == PalimpsestHubPublishedImage.id,
        own_image,
    )
    return layer | source


def _cutoff(ttl_seconds: int) -> datetime:
    return (datetime.now(UTC) - timedelta(seconds=ttl_seconds)).replace(tzinfo=None)


async def _remove(session, rows) -> int:
    for row in rows:
        reference = await session.get(
            RuntimeBlobReference, ("image-stage", reference_id(row.project_id, row.created_by), row.blob_digest)
        )
        if reference is not None:
            await session.delete(reference)
        await session.delete(row)
    if rows:
        await session.flush()
    return len(rows)


async def expire_staged_payloads(session, *, ttl_seconds: int, project: str | None = None, user_id: str | None = None):
    """Remove unregistered staged rows older than the TTL (optionally for one stager)."""
    conditions = [~_registered(), PalimpsestHubPublicationPayload.created_at < _cutoff(ttl_seconds)]
    if project is not None and user_id is not None:
        conditions += [
            exact_identity(PalimpsestHubPublicationPayload.project_id, project),
            exact_identity(PalimpsestHubPublicationPayload.created_by, user_id),
        ]
    rows = (await session.scalars(select(PalimpsestHubPublicationPayload).where(*conditions).limit(_SWEEP_BATCH))).all()
    return await _remove(session, rows)


async def enforce_staging_limits(
    session, project: str, user_id: str, digest: str, size: int, *, max_bytes: int, ttl_seconds: int
) -> bool:
    """Apply expiry then the aggregate cap. Returns True when expired rows were removed.

    Re-staging a payload the caller already holds consumes no new quota. The caller must
    commit the returned removals even when this raises (see ``stage_payload``).
    """
    removed = await expire_staged_payloads(session, ttl_seconds=ttl_seconds, project=project, user_id=user_id)
    own = (
        exact_identity(PalimpsestHubPublicationPayload.project_id, project),
        exact_identity(PalimpsestHubPublicationPayload.created_by, user_id),
    )
    if await session.get(PalimpsestHubPublicationPayload, (project, user_id, digest)) is not None:
        return bool(removed)
    held = await session.scalar(
        select(func.coalesce(func.sum(PalimpsestHubPublicationPayload.size_bytes), 0)).where(
            *own, ~_registered(), PalimpsestHubPublicationPayload.created_at >= _cutoff(ttl_seconds)
        )
    )
    if int(held) + size > max_bytes:
        raise StagedQuotaExceeded(removed=bool(removed), limit=max_bytes, staged=int(held))
    return bool(removed)


class StagedQuotaExceeded(HTTPException):
    def __init__(self, *, removed: bool, limit: int, staged: int):
        super().__init__(
            status_code=413,
            detail={"code": "staged_payload_limit", "limit_bytes": limit, "staged_bytes": staged},
        )
        self.removed = removed
