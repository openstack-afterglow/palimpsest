"""Administrator image-quota policy and accounting routes."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from palimpsest_hub.api import hub
from palimpsest_hub.api.hub import _factory_or_503
from palimpsest_hub.auth import require_admin
from palimpsest_hub.config import validate_keystone_id
from palimpsest_hub.services import image_quota as service
from palimpsest_hub.services import published_images
from palimpsest_hub.services.worker_leases import sql_mutex

router = APIRouter()

_Limit = Annotated[int, Field(strict=True, ge=0, le=service.MAX_BIGINT)]
Tier = Literal["free", "paid"]
Visibility = Literal["private", "internal", "public"]
SubjectKind = Literal["user", "project"]


class PolicyUpdate(BaseModel):
    """All three limits are required; explicit null means unlimited."""

    model_config = ConfigDict(extra="forbid")

    max_images: _Limit | None
    max_chain_bytes: _Limit | None
    max_source_bytes: _Limit | None


class TierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Tier


async def _quota_body(request: Request, model, maximum: int):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(status_code=413, detail="image quota request body exceeds byte limit")
        body.extend(chunk)
    try:
        return model.model_validate(json.loads(body))
    except (ValueError, ValidationError, TypeError, RecursionError):
        raise HTTPException(status_code=422, detail="invalid image quota request") from None


def _subject(value):
    try:
        return validate_keystone_id(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid quota subject identifier") from None


@router.get("/admin/image-quota/policies", operation_id="list_image_quota_policies")
async def list_policies(_: dict = Depends(require_admin)):
    async with _factory_or_503()() as session:
        return {"items": list((await service.load_policies(session)).values())}


@router.put("/admin/image-quota/policies/{tier}/{visibility}", operation_id="update_image_quota_policy")
async def update_policy(tier: Tier, visibility: Visibility, request: Request, admin: dict = Depends(require_admin)):
    payload = await _quota_body(request, PolicyUpdate, 4096)
    # Same mutex as every publication: an update is ordered strictly before or after each check-and-insert.
    async with sql_mutex(_factory_or_503(), published_images.IMAGE_MUTEX) as session:
        value = await service.update_policy(session, tier, visibility, payload.model_dump(), admin["user_id"])
        await session.commit()
        return value


@router.get("/admin/image-quota/tiers/{subject_kind}/{subject_id}", operation_id="get_image_quota_tier")
async def get_tier(subject_kind: SubjectKind, subject_id: str, _: dict = Depends(require_admin)):
    subject_id = _subject(subject_id)
    async with _factory_or_503()() as session:
        return await service.subject_tier(session, subject_kind, subject_id)


@router.put("/admin/image-quota/tiers/{subject_kind}/{subject_id}", operation_id="assign_image_quota_tier")
async def put_tier(subject_kind: SubjectKind, subject_id: str, request: Request, admin: dict = Depends(require_admin)):
    subject_id = _subject(subject_id)
    payload = await _quota_body(request, TierUpdate, 1024)
    async with sql_mutex(_factory_or_503(), published_images.IMAGE_MUTEX) as session:
        value = await service.assign_tier(session, subject_kind, subject_id, payload.tier, admin["user_id"])
        await session.commit()
        return value


@router.get("/admin/image-quota/usage", operation_id="get_image_quota_usage")
async def usage(subject_kind: SubjectKind = Query(...), subject_id: str = Query(...), _: dict = Depends(require_admin)):
    subject_id = _subject(subject_id)
    async with _factory_or_503()() as session:
        return await service.subject_usage(session, subject_kind, subject_id)


@router.get("/admin/image-quota/physical", operation_id="get_image_physical_report")
async def physical(_: dict = Depends(require_admin)):
    settings = published_images.get_settings()
    manila = any(
        getattr(settings, "palimpsest_hub_publication_manila_" + key, "") for key in ("share_id", "export", "root")
    )
    try:
        store = hub._store_or_503()
    except HTTPException:
        store = None  # Reported as null capacity, never guessed.
    async with _factory_or_503()() as session:
        return await service.physical_report(session, store, manila=manila)
