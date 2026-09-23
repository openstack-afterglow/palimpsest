"""Project-bound, administrator-only requests for isolated Palimpsestfile builds."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from palimpsest_hub.api.hub import _factory_or_503, _load_visible, _locked_file, _store_or_503
from palimpsest_hub.auth import require_admin
from palimpsest_hub.config import get_settings
from palimpsest_hub.models import PalimpsestHubBuild
from palimpsest_hub.rate_limit import limiter
from palimpsest_hub.services.digest import normalize_digest
from palimpsest_hub.services.hub_store import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS

router = APIRouter()


class BuildRequest(BaseModel):
    """Build RUN commands in a disposable network-none VM from visible Hub inputs."""

    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9.+-]{0,63}$")
    recipe: str = Field(..., min_length=1, max_length=1024 * 1024)
    base_digest: str
    layer_digests: list[str] = Field(default_factory=list, max_length=25)

    @field_validator("base_digest")
    @classmethod
    def _check_base(cls, value: str) -> str:
        digest = normalize_digest(value)
        if digest is None:
            raise ValueError("base_digest must be sha256:<64hex>")
        return digest

    @field_validator("layer_digests")
    @classmethod
    def _check_layers(cls, values: list[str]) -> list[str]:
        digests = [normalize_digest(value) for value in values]
        if any(digest is None for digest in digests) or len(set(digests)) != len(digests):
            raise ValueError("layer_digests must be distinct sha256:<64hex> digests")
        return [digest for digest in digests if digest is not None]

    @field_validator("recipe")
    @classmethod
    def _check_recipe(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024 * 1024:
            raise ValueError("recipe exceeds 1 MiB")
        return value


class BuildResponse(BaseModel):
    id: str
    name: str
    recipe_digest: str
    base_digest: str
    layer_digests: list[str]
    status: str
    output_digest: str | None
    output_size_bytes: int | None
    error_code: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


def serialize_build(row: PalimpsestHubBuild) -> dict[str, Any]:
    """Never return raw RUN commands, identities or worker-local paths."""
    return {
        "id": row.id,
        "name": row.name,
        "recipe_digest": row.recipe_digest,
        "base_digest": row.base_digest,
        "layer_digests": row.layer_digests,
        "status": row.status,
        "output_digest": row.output_digest,
        "output_size_bytes": row.output_size_bytes,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


@router.post("/builds", status_code=202, response_model=BuildResponse, operation_id="create_hub_build")
@limiter.limit("6/hour")
async def create_hub_build(
    request: Request,
    req: BuildRequest,
    token_info: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Persist a job only after checking every source against the caller's project."""
    settings = get_settings()
    if not settings.palimpsest_hub_builder_python or not Path(settings.palimpsest_hub_builder_python).is_absolute():
        raise HTTPException(status_code=503, detail="isolated build worker is not configured")
    store = _store_or_503()
    factory = _factory_or_503()
    project_id = token_info["project_id"]
    async with (
        _locked_file(store, lambda: store.acquire_project_build_lock(project_id, blocking=False)),
        factory() as session,
    ):
        queued = await session.scalar(
            select(func.count())
            .select_from(PalimpsestHubBuild)
            .where(PalimpsestHubBuild.project_id == project_id, PalimpsestHubBuild.status.in_(("queued", "building")))
        )
        if queued and queued >= 4:
            raise HTTPException(status_code=429, detail="project build queue is full")
        base = await _load_visible(session, req.base_digest, token_info)
        if base.kind != KIND_CLOUD_IMAGE or base.arch != "x86_64" or base.disk_format not in ("raw", "qcow2"):
            raise HTTPException(status_code=422, detail="build base must be a visible Linux x86_64 cloud image")
        previous: str | None = None
        for digest in req.layer_digests:
            layer = await _load_visible(session, digest, token_info)
            if layer.kind != "squashfs" or layer.media_type != MEDIA_TYPE_LAYER_SQUASHFS:
                raise HTTPException(status_code=422, detail="build inputs must be SquashFS layers")
            if layer.parent_digest != previous:
                raise HTTPException(status_code=422, detail="build layer parent chain is incomplete")
            if previous is None and (layer.config_json or {}).get("base_image_digest") != req.base_digest:
                raise HTTPException(status_code=422, detail="build layer does not belong to the selected base")
            previous = digest
        for digest in (req.base_digest, *req.layer_digests):
            if not store.exists(digest):
                raise HTTPException(status_code=409, detail="a build input blob is not available")
        row = PalimpsestHubBuild(
            id=str(uuid.uuid4()),
            project_id=project_id,
            created_by=token_info.get("user_id"),
            name=req.name,
            recipe=req.recipe,
            recipe_digest="sha256:" + hashlib.sha256(req.recipe.encode("utf-8")).hexdigest(),
            base_digest=req.base_digest,
            layer_digests=req.layer_digests,
            status="queued",
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        return serialize_build(row)


@router.get("/builds", response_model=list[BuildResponse], operation_id="list_hub_builds")
async def list_hub_builds(
    limit: int = Query(50, ge=1, le=100), token_info: dict = Depends(require_admin)
) -> list[dict[str, Any]]:
    factory = _factory_or_503()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(PalimpsestHubBuild)
                    .where(PalimpsestHubBuild.project_id == token_info["project_id"])
                    .order_by(PalimpsestHubBuild.created_at.desc(), PalimpsestHubBuild.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [serialize_build(row) for row in rows]


@router.get("/builds/{build_id}", response_model=BuildResponse, operation_id="get_hub_build")
async def get_hub_build(build_id: UUID, token_info: dict = Depends(require_admin)) -> dict[str, Any]:
    factory = _factory_or_503()
    async with factory() as session:
        row = await session.get(PalimpsestHubBuild, str(build_id))
        if row is None or row.project_id != token_info["project_id"]:
            raise HTTPException(status_code=404, detail="build job not found")
        return serialize_build(row)
