"""Project-scoped immutable image publication and logical-layer discovery."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from palimpsest_hub.api import hub
from palimpsest_hub.api.hub import (
    _factory_or_503,
    _locked_blob,
    _run_blocking,
    _store_or_503,
)
from palimpsest_hub.api.image_jobs import bounded_json, content_descriptor
from palimpsest_hub.auth import (
    credential_headers,
    get_package_manager_info,
    keystone_token_header,
    original_token_info,
    package_target_project_id,
)
from palimpsest_hub.config import get_settings
from palimpsest_hub.image_models import (
    PalimpsestHubLayerNode,
    PalimpsestHubPublicationPayload,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import PalimpsestHubUpload, exact_identity
from palimpsest_hub.services import image_keys, public_images
from palimpsest_hub.services import published_images as service
from palimpsest_hub.services.blob_references import retain_blob
from palimpsest_hub.services.image_jobs import _receive, fresh_writer, now
from palimpsest_hub.services.worker_leases import sql_mutex

router = APIRouter()

# Documentation-only declarations: credential selection below reads the raw headers.
image_key_bearer = HTTPBearer(
    auto_error=False, scheme_name="ImageKey", description="ppi_v1_ image API key (Authorization: Bearer)"
)

_HUMAN_DENIALS = {
    "packages:inventory": "Palimpsest inventory authority required",
    "packages:read": "Palimpsest download authority required",
    "packages:write": "Palimpsest publish authority required",
}


def _bearer_credential(authorization: str) -> str:
    """RFC 7235: the auth scheme is case-insensitive; only Bearer carries an image key."""
    scheme, _, credential = authorization.partition(" ")
    credential = credential.strip(" ")
    if scheme.casefold() != "bearer" or not credential:
        raise HTTPException(status_code=401, detail="image key bearer credential required")
    return credential


async def _image_actor(request: Request, *, human_action: str, key_action: str, anonymous: bool) -> dict:
    """Select exactly one credential: image key, original Keystone token, or (reads only) none.

    A present but invalid credential is always rejected; it is never treated as anonymous.
    `ppk_v1` package keys and every other bearer format fail image-key decoding with 401.
    """
    authorization, token = credential_headers(request)
    if authorization is not None:
        if "x-target-project-id" in request.headers:
            raise HTTPException(status_code=403, detail="image keys cannot select a logical target")
        actor = await image_keys.authenticate(_bearer_credential(authorization))
        assertion = request.headers.get("x-project-id")
        if assertion is not None and assertion != actor["project_id"]:
            raise HTTPException(status_code=403, detail="project assertion does not match image key")
        if key_action not in actor["image_actions"]:
            raise HTTPException(status_code=403, detail="image key action is not delegated")
        return actor
    if token is None:
        if not anonymous:
            raise HTTPException(status_code=401, detail="X-Auth-Token header or image key is required")
        public_images.admit_anonymous(request)
        return public_images.anonymous_actor()
    member = await get_package_manager_info(token_info=await original_token_info(request))
    if human_action not in member.get("package_actions", ()):
        raise HTTPException(status_code=403, detail=_HUMAN_DENIALS[human_action])
    return member


_TokenScheme = Security(keystone_token_header)
_KeyScheme = Security(image_key_bearer)


async def image_inventory(
    request: Request, _token: str | None = _TokenScheme, _key: HTTPAuthorizationCredentials | None = _KeyScheme
) -> dict:
    return await _image_actor(request, human_action="packages:inventory", key_action="images:read", anonymous=True)


async def image_download(
    request: Request, _token: str | None = _TokenScheme, _key: HTTPAuthorizationCredentials | None = _KeyScheme
) -> dict:
    return await _image_actor(request, human_action="packages:read", key_action="images:read", anonymous=True)


async def image_graph_reader(
    request: Request, _token: str | None = _TokenScheme, _key: HTTPAuthorizationCredentials | None = _KeyScheme
) -> dict:
    return await _image_actor(request, human_action="packages:inventory", key_action="images:read", anonymous=False)


async def image_writer(
    request: Request, _token: str | None = _TokenScheme, _key: HTTPAuthorizationCredentials | None = _KeyScheme
) -> dict:
    return await _image_actor(request, human_action="packages:write", key_action="images:publish", anonymous=False)


def _publish_only_key(actor: dict) -> bool:
    return actor.get("actor_type") == "image-key" and "images:read" not in actor["image_actions"]


async def _fresh_writer(actor: dict) -> dict:
    """Mutation-boundary recheck for the same credential kind that admitted the request."""
    if actor.get("actor_type") == "image-key":
        return await image_keys.fresh_actor(actor, "images:publish")
    return await fresh_writer(actor)


async def _require_own_staged_payloads(actor: dict, manifest: dict) -> None:
    """A publish-only key cannot probe payloads it did not stage (no reuse of visible images)."""
    digests = {item["digest"] for item in manifest["layers"]}
    async with _factory_or_503()() as session:
        staged = set(
            (
                await session.scalars(
                    select(PalimpsestHubPublicationPayload.blob_digest).where(
                        exact_identity(PalimpsestHubPublicationPayload.project_id, actor["project_id"]),
                        exact_identity(PalimpsestHubPublicationPayload.created_by, actor["user_id"]),
                        PalimpsestHubPublicationPayload.blob_digest.in_(digests),
                    )
                )
            ).all()
        )
    if staged != digests:
        raise HTTPException(status_code=404, detail="publication payload not found")


def _readable(row, actor: dict) -> bool:
    if public_images.is_anonymous(actor):
        return public_images.anonymous_readable(row)
    return service.image_readable(row, actor)


class SourceClosureItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["root", "manifest", "config", "layer"]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, strict=True)
    ordinal: int = Field(ge=0, strict=True)


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,63}$")
    manifest: dict
    visibility: Literal["private", "internal", "public"] = "private"
    source_closure: list[SourceClosureItem] | None = Field(default=None, min_length=1, max_length=300)


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
                await _fresh_writer(actor)
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
async def stage_payload(digest: str, request: Request, actor: dict = Depends(image_writer)):
    digest, size = content_descriptor(request, get_settings().palimpsest_hub_max_blob_bytes, digest)
    if request.headers.get("Content-Type", "").split(";", 1)[0] != "application/octet-stream":
        raise HTTPException(status_code=415, detail="publication payload must be octet-stream")
    store, factory = _store_or_503(), _factory_or_503()
    project = package_target_project_id(actor)
    # Even existing bytes are verified from this caller's stream; presence is
    # not permission to adopt another project's private payload.
    async with (
        _upload_admission(factory, store, project, actor),
        _receive(store, request, digest, size, now() + timedelta(seconds=300), squashfs=False) as (path, verified),
        _locked_blob(store, digest),
        sql_mutex(factory, service.IMAGE_MUTEX) as session,
    ):
        await _fresh_writer(actor)
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
async def publish(request: Request, actor: dict = Depends(image_writer)):
    payload = await bounded_json(request, PublicationRequest, 1024 * 1024)
    manifest = service.normalized(payload.manifest)
    if sum(item["size_bytes"] for item in manifest["layers"]) > get_settings().palimpsest_hub_max_blob_bytes:
        raise HTTPException(status_code=413, detail="image stack exceeds byte limit")
    current = await _fresh_writer(actor)
    current = {**current, "project_id": package_target_project_id(current)}
    if _publish_only_key(current):
        await _require_own_staged_payloads(current, manifest)
    return await service.publish_image(
        _factory_or_503(),
        _store_or_503(),
        current,
        str(payload.request_id),
        payload.name,
        manifest,
        visibility=payload.visibility,
        source_closure=(
            [item.model_dump() for item in payload.source_closure] if payload.source_closure is not None else None
        ),
    )


@router.get("/published-images", operation_id="list_published_images")
async def list_images(
    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), actor: dict = Depends(image_inventory)
):
    anonymous = public_images.is_anonymous(actor)
    if anonymous:
        limit = min(limit, public_images.ANONYMOUS_LIST_LIMIT)
    predicate = public_images.anonymous_predicate() if anonymous else service.visible_images(actor)
    async with _factory_or_503()() as session:
        rows = (
            await session.scalars(
                select(PalimpsestHubPublishedImage)
                .where(predicate)
                .order_by(PalimpsestHubPublishedImage.created_at, PalimpsestHubPublishedImage.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        if anonymous:
            return {"items": [await public_images.anonymous_image_value(session, row) for row in rows]}
        return {"items": [await service.image_value(session, row, actor=actor) for row in rows]}


@router.post("/published-images/extensions", status_code=201, operation_id="publish_image_extension")
async def extension(request: Request, actor: dict = Depends(image_writer)):
    payload = await bounded_json(request, ExtensionRequest, 1024 * 1024)
    current = await _fresh_writer(actor)
    if _publish_only_key(current):
        # Extension reads the base image's full manifest and graph.
        raise HTTPException(status_code=403, detail="image extension also requires images:read")
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
async def get_image(image_id: UUID, actor: dict = Depends(image_inventory)):
    anonymous = public_images.is_anonymous(actor)
    async with _factory_or_503()() as session:
        row = await session.get(PalimpsestHubPublishedImage, str(image_id))
        if row is None or not _readable(row, actor):
            raise HTTPException(status_code=404, detail="published image not found")
        if anonymous:
            return await public_images.anonymous_image_value(session, row)
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
    node_id: str,
    project_id: str | None = None,
    image_id: UUID | None = None,
    actor: dict = Depends(image_graph_reader),
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
    actor: dict = Depends(image_graph_reader),
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
    node_id: str,
    project_id: str | None = None,
    image_id: UUID | None = None,
    actor: dict = Depends(image_graph_reader),
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
async def image_blob(image_id: UUID, digest: str, request: Request, actor: dict = Depends(image_download)):
    async with _factory_or_503()() as session:
        row = await session.get(PalimpsestHubPublishedImage, str(image_id))
        if row is None or not _readable(row, actor):
            raise HTTPException(status_code=404, detail="published image not found")
        roles, size = await service.image_blob_roles(session, row, digest)
        if not roles:
            raise HTTPException(status_code=404, detail="publication payload not found")
        await service.image_value(session, row)
    store = _store_or_503()
    if not store.exists(digest):
        raise HTTPException(status_code=404, detail="publication payload not found")
    payload = "payload" in roles
    response = hub._blob_response(
        store=store,
        digest=digest,
        total=size,
        media_type="application/vnd.palimpsest.layer.squashfs" if payload else "application/octet-stream",
        filename=digest[7:] + (".sqfs" if payload else ""),
        range_header=request.headers.get("range"),
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
