"""Project-scoped immutable image publication and logical-layer discovery."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from palimpsest_hub.api import hub
from palimpsest_hub.api.hub import (
    _factory_or_503,
    _legacy_inventory,
    _legacy_writer,
    _locked_blob,
    _run_blocking,
    _store_or_503,
)
from palimpsest_hub.api.image_jobs import bounded_json, content_descriptor
from palimpsest_hub.auth import package_target_project_id
from palimpsest_hub.config import get_settings
from palimpsest_hub.image_models import (
    PalimpsestHubLayerNode,
    PalimpsestHubPublicationPayload,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import PalimpsestHubUpload, exact_identity
from palimpsest_hub.services import published_images as service
from palimpsest_hub.services.blob_references import retain_blob
from palimpsest_hub.services.image_jobs import _receive, fresh_writer, now
from palimpsest_hub.services.worker_leases import sql_mutex

router = APIRouter()


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,63}$")
    manifest: dict
    visibility: Literal["private", "internal", "public"] = "private"


class ExtensionLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    size_bytes: int = Field(gt=0, strict=True)
    source: dict | None = None
    id: str | None = None
    parent_id: str | None = None


class ExtensionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,63}$")
    base_image_id: UUID
    layers: list[ExtensionLayer] = Field(min_length=1, max_length=128)
    visibility: Literal["private", "internal", "public"] = "private"
    metadata: dict | None = None


@asynccontextmanager
async def _upload_admission(factory, store, project, actor):
    """Reserve the legacy/package shared project capacity before reading bytes."""
    from palimpsest_hub.models import PackageUpload

    upload_id = uuid4().hex
    try:
        async with hub._locked_file(store, lambda: store.acquire_project_upload_lock(project, blocking=False)):
            await hub._expire_project_uploads(factory, store, project)
            async with factory() as session:
                active = await session.scalar(
                    select(func.count())
                    .select_from(PalimpsestHubUpload)
                    .where(exact_identity(PalimpsestHubUpload.project_id, project))
                )
                active += await session.scalar(
                    select(func.count())
                    .select_from(PackageUpload)
                    .where(
                        PackageUpload.project_id == project,
                        PackageUpload.status.in_(("uploading", "validating")),
                        PackageUpload.expires_at > datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                if active >= hub._MAX_ACTIVE_UPLOADS:
                    raise HTTPException(status_code=429, detail="project upload session limit reached")
                await fresh_writer(actor)
                session.add(
                    PalimpsestHubUpload(
                        id=upload_id, received_bytes=0, project_id=project, created_by=actor.get("user_id")
                    )
                )
                await session.commit()
        async with hub._locked_upload(store, upload_id):
            yield
    finally:
        # The receive context drains writes and removes its tempfile before the
        # shared admission row is released, including cancellation/error paths.
        async def release():
            async with factory() as session:
                row = await session.get(PalimpsestHubUpload, upload_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()

        # Repeated cancellation must not strand a shared slot until idle expiry.
        cleanup = asyncio.create_task(release())
        _, cancelled = await hub._wait_without_releasing(cleanup)
        if cancelled:
            raise asyncio.CancelledError


@router.put("/published-images/blobs/{digest}", operation_id="stage_published_image_payload")
async def stage_payload(digest: str, request: Request, actor: dict = Depends(_legacy_writer)):
    digest, size = content_descriptor(request, get_settings().palimpsest_hub_max_blob_bytes, digest)
    if request.headers.get("Content-Type", "").split(";", 1)[0] != "application/octet-stream":
        raise HTTPException(status_code=415, detail="publication payload must be octet-stream")
    store, factory = _store_or_503(), _factory_or_503()
    project = package_target_project_id(actor)
    # Even existing bytes are verified from this caller's stream; presence is
    # not permission to adopt another project's private payload.
    async with (
        _upload_admission(factory, store, project, actor),
        _receive(store, request, digest, size, now() + timedelta(seconds=300)) as (path, verified),
        _locked_blob(store, digest),
        sql_mutex(factory, "build-api:image:single-flight") as session,
    ):
        await fresh_writer(actor)
        await _run_blocking(store.publish_verified, path, verified)
        row = await session.get(
            PalimpsestHubPublicationPayload,
            {"project_id": project, "created_by": actor["user_id"], "blob_digest": digest},
        )
        if row is None:
            session.add(
                PalimpsestHubPublicationPayload(
                    project_id=project, created_by=actor["user_id"], blob_digest=digest, size_bytes=size
                )
            )
        elif row.project_id != project or row.created_by != actor["user_id"] or row.size_bytes != size:
            raise HTTPException(status_code=409, detail="publication payload size conflicts")
        await retain_blob(
            session,
            digest,
            "image-stage",
            hashlib.sha256((project + "\0" + actor["user_id"]).encode("utf-8")).hexdigest(),
        )
        await session.commit()
    return {"digest": digest, "size_bytes": size}


@router.post("/published-images", status_code=201, operation_id="publish_image")
async def publish(request: Request, actor: dict = Depends(_legacy_writer)):
    payload = await bounded_json(request, PublicationRequest, 1024 * 1024)
    manifest = service.normalized(payload.manifest)
    if sum(item["size_bytes"] for item in manifest["layers"]) > get_settings().palimpsest_hub_max_blob_bytes:
        raise HTTPException(status_code=413, detail="image stack exceeds byte limit")
    current = await fresh_writer(actor)
    current = {**current, "project_id": package_target_project_id(current)}
    return await service.publish_image(
        _factory_or_503(),
        _store_or_503(),
        current,
        str(payload.request_id),
        payload.name,
        manifest,
        visibility=payload.visibility,
    )


@router.get("/published-images", operation_id="list_published_images")
async def list_images(
    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), actor: dict = Depends(_legacy_inventory)
):
    async with _factory_or_503()() as session:
        rows = (
            await session.scalars(
                select(PalimpsestHubPublishedImage)
                .where(service.visible_images(actor))
                .order_by(PalimpsestHubPublishedImage.created_at, PalimpsestHubPublishedImage.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {"items": [await service.image_value(session, row, actor=actor) for row in rows]}


@router.post("/published-images/extensions", status_code=201, operation_id="publish_image_extension")
async def extension(request: Request, actor: dict = Depends(_legacy_writer)):
    payload = await bounded_json(request, ExtensionRequest, 1024 * 1024)
    current = await fresh_writer(actor)
    current = {**current, "project_id": package_target_project_id(current)}
    return await service.extend_image(
        _factory_or_503(),
        _store_or_503(),
        current,
        str(payload.request_id),
        payload.name,
        str(payload.base_image_id),
        [layer.model_dump(exclude_none=True, exclude={"id", "parent_id"}) for layer in payload.layers],
        visibility=payload.visibility,
        metadata=payload.metadata,
    )


@router.get("/published-images/{image_id}", operation_id="get_published_image")
async def get_image(image_id: UUID, actor: dict = Depends(_legacy_inventory)):
    async with _factory_or_503()() as session:
        row = await session.get(PalimpsestHubPublishedImage, str(image_id))
        if row is None or not service.image_readable(row, actor):
            raise HTTPException(status_code=404, detail="published image not found")
        return await service.image_value(session, row, actor=actor)


async def _node(session, node_id, actor, *, project_id=None, image_id=None):
    rows = (
        await session.scalars(
            service.visible_nodes(actor, project_id=project_id, image_id=image_id)
            .where(PalimpsestHubLayerNode.id == node_id)
            .order_by(PalimpsestHubLayerNode.project_id)
            .limit(1)
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="logical layer not found")
    return rows[0]


@router.get("/layer-nodes/{node_id}", operation_id="get_layer_node")
async def get_node(
    node_id: str, project_id: str | None = None, image_id: UUID | None = None, actor: dict = Depends(_legacy_inventory)
):
    async with _factory_or_503()() as session:
        return service.serialize_node(
            await _node(session, node_id, actor, project_id=project_id, image_id=str(image_id) if image_id else None)
        )


@router.get("/layer-nodes/{node_id}/children", operation_id="get_layer_children")
async def children(
    node_id: str,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_id: str | None = None,
    image_id: UUID | None = None,
    actor: dict = Depends(_legacy_inventory),
):
    image_id = str(image_id) if image_id else None
    async with _factory_or_503()() as session:
        await _node(session, node_id, actor, project_id=project_id, image_id=image_id)
        rows = (
            await session.scalars(
                service.visible_nodes(actor, project_id=project_id, image_id=image_id)
                .where(PalimpsestHubLayerNode.parent_id == node_id)
                .order_by(PalimpsestHubLayerNode.id, PalimpsestHubLayerNode.project_id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {"items": [service.serialize_node(row) for row in rows]}


@router.get("/layer-nodes/{node_id}/ancestors", operation_id="get_layer_ancestors")
async def ancestors(
    node_id: str, project_id: str | None = None, image_id: UUID | None = None, actor: dict = Depends(_legacy_inventory)
):
    image_id = str(image_id) if image_id else None
    async with _factory_or_503()() as session:
        row = await _node(session, node_id, actor, project_id=project_id, image_id=image_id)
        result, seen, project = [], set(), row.project_id
        while row.parent_id is not None:
            if row.id in seen or len(result) >= 128:
                raise HTTPException(status_code=409, detail="logical layer graph is invalid")
            seen.add(row.id)
            row = await _node(session, row.parent_id, actor, project_id=project, image_id=image_id)
            result.append(service.serialize_node(row))
        return {"items": list(reversed(result))}


@router.get("/published-images/{image_id}/blobs/{digest}", operation_id="download_published_image_payload")
async def image_blob(image_id: UUID, digest: str, request: Request, actor: dict = Depends(hub._legacy_download)):
    async with _factory_or_503()() as session:
        row = await session.get(PalimpsestHubPublishedImage, str(image_id))
        if row is None or not service.image_readable(row, actor):
            raise HTTPException(status_code=404, detail="published image not found")
        manifest = service.normalized(row.manifest_json)
        item = next((item for item in manifest["layers"] if item["digest"] == digest), None)
        if item is None:
            raise HTTPException(status_code=404, detail="publication payload not found")
        await service.image_value(session, row)
    store = _store_or_503()
    if not store.exists(digest):
        raise HTTPException(status_code=404, detail="publication payload not found")
    return hub._blob_response(
        store=store,
        digest=digest,
        total=item["size_bytes"],
        media_type="application/vnd.palimpsest.layer.squashfs",
        filename=digest[7:] + ".sqfs",
        range_header=request.headers.get("range"),
    )
