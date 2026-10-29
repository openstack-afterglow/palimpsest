"""Human-authorized OCI intake and lease-fenced HTTPS image workers."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from palimpsest_hub.api.builds import require_build_lease, require_build_worker
from palimpsest_hub.api.hub import _factory_or_503, _legacy_inventory, _legacy_writer
from palimpsest_hub.auth import package_target_project_id
from palimpsest_hub.config import get_settings
from palimpsest_hub.image_models import PalimpsestHubImageJob
from palimpsest_hub.models import exact_identity
from palimpsest_hub.services import image_jobs as service
from palimpsest_hub.services.digest import normalize_digest

router = APIRouter()


class RegistryImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,63}$")
    reference: str = Field(min_length=1, max_length=512)
    platform: Literal["linux/amd64"] = "linux/amd64"


class DockerfileBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,63}$")
    dockerfile: str = Field(min_length=1, max_length=1024 * 1024)
    context_digest: str | None = None
    platform: Literal["linux/amd64"] = "linux/amd64"
    target: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    build_args: dict[str, str] = Field(default_factory=dict, max_length=64)

    @field_validator("context_digest")
    @classmethod
    def context(cls, value):
        if value is not None and normalize_digest(value) is None:
            raise ValueError("context_digest must be sha256:<64hex>")
        return normalize_digest(value) if value else None

    @field_validator("build_args")
    @classmethod
    def args(cls, value):
        import re

        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) or len(item.encode()) > 4096
            for key, item in value.items()
        ):
            raise ValueError("invalid or oversized build argument")
        return value


class OutputLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str
    size_bytes: int = Field(gt=0, strict=True)

    @field_validator("digest")
    @classmethod
    def digest_value(cls, value):
        normalized = normalize_digest(value)
        if normalized is None:
            raise ValueError("invalid SHA256 digest")
        return normalized


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_digest: str
    manifest_digest: str
    config_digest: str

    @field_validator("root_digest", "manifest_digest", "config_digest")
    @classmethod
    def digest_value(cls, value):
        return OutputLayer.digest_value(value)


class Runtime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entrypoint: list[str] = Field(default_factory=list, max_length=256)
    cmd: list[str] = Field(default_factory=list, max_length=256)
    env: list[str] = Field(default_factory=list, max_length=1024)
    working_dir: str = Field(default="", max_length=4096)
    user: str = Field(default="", max_length=256)

    @field_validator("entrypoint", "cmd", "env")
    @classmethod
    def strings(cls, value):
        if any("\x00" in item or len(item.encode()) > 16384 for item in value):
            raise ValueError("invalid runtime value")
        return value

    @field_validator("working_dir", "user")
    @classmethod
    def no_nul(cls, value):
        if "\x00" in value:
            raise ValueError("invalid runtime value")
        return value


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layers: list[OutputLayer] = Field(min_length=1, max_length=128)
    source: Source
    runtime: Runtime


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error_code: Literal["build_failed", "build_timeout", "cleanup_failed"]


def request_body(model=None, media_type="application/json"):
    schema = model.model_json_schema() if model is not None else {"type": "string", "format": "binary"}
    definitions = schema.pop("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(item) for key, item in value.items()}
        return value

    return {"requestBody": {"required": True, "content": {media_type: {"schema": inline(schema)}}}}


async def bounded_json(request: Request, model, maximum: int):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise HTTPException(status_code=413, detail="request body exceeds byte limit")
        body.extend(chunk)
    try:
        return model.model_validate(json.loads(body))
    except (ValueError, ValidationError, TypeError, RecursionError):
        # Do not reflect Dockerfile, environment, arguments or submitted credentials.
        raise HTTPException(status_code=422, detail="invalid image job descriptor") from None


def content_descriptor(request: Request, maximum: int, digest: str | None = None):
    expected = normalize_digest(digest or request.headers.get("X-Content-SHA256", ""))
    length = request.headers.get("Content-Length", "")
    if expected is None or len(length) > 20 or not length.isascii() or not length.isdecimal():
        raise HTTPException(status_code=422, detail="SHA256 digest and Content-Length are required")
    size = int(length)
    if size < 1 or size > maximum:
        raise HTTPException(status_code=413, detail="content exceeds byte limit")
    return expected, size


@router.post(
    "/build-contexts",
    status_code=201,
    operation_id="upload_image_build_context",
    openapi_extra=request_body(media_type="application/x-tar"),
)
async def upload_context(request: Request, token_info: dict = Depends(_legacy_writer)):
    if request.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-tar":
        raise HTTPException(status_code=415, detail="build context must be application/x-tar")
    digest, size = content_descriptor(request, get_settings().palimpsest_hub_max_context_bytes)
    return await service.upload_context(request, token_info, digest, size)


@router.post(
    "/registry-imports",
    status_code=202,
    operation_id="create_registry_import",
    openapi_extra=request_body(RegistryImportRequest),
)
async def registry_import(request: Request, token_info: dict = Depends(_legacy_writer)):
    req = await bounded_json(request, RegistryImportRequest, 8192)
    payload = req.model_dump(mode="json")
    payload["reference"] = service.normalize_registry_reference(req.reference)
    return service.serialize_job(await service.admit("registry", payload, token_info))


@router.post(
    "/dockerfile-builds",
    status_code=202,
    operation_id="create_dockerfile_build",
    openapi_extra=request_body(DockerfileBuildRequest),
)
async def dockerfile_build(request: Request, token_info: dict = Depends(_legacy_writer)):
    req = await bounded_json(request, DockerfileBuildRequest, 2 * 1024 * 1024)
    if len(req.dockerfile.encode()) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Dockerfile exceeds byte limit")
    return service.serialize_job(await service.admit("dockerfile", req.model_dump(mode="json"), token_info))


@router.get("/image-jobs", operation_id="list_image_jobs")
async def list_jobs(limit: int = Query(50, ge=1, le=100), token_info: dict = Depends(_legacy_inventory)):
    async with _factory_or_503()() as session:
        rows = (
            await session.scalars(
                select(PalimpsestHubImageJob)
                .where(exact_identity(PalimpsestHubImageJob.project_id, package_target_project_id(token_info)))
                .order_by(PalimpsestHubImageJob.created_at.desc(), PalimpsestHubImageJob.id.desc())
                .limit(limit)
            )
        ).all()
        return [service.serialize_job(row) for row in rows]


@router.get("/image-jobs/{job_id}", operation_id="get_image_job")
async def get_job(job_id: UUID, token_info: dict = Depends(_legacy_inventory)):
    async with _factory_or_503()() as session:
        row = await session.get(PalimpsestHubImageJob, str(job_id))
        if row is None or row.project_id != package_target_project_id(token_info):
            raise HTTPException(status_code=404, detail="image job not found")
        return service.serialize_job(row)


@router.post(
    "/image-worker/claim", status_code=202, dependencies=[Depends(require_build_worker)], operation_id="claim_image_job"
)
async def claim():
    result = await service.claim()
    return result if result is not None else Response(status_code=204)


@router.get("/image-worker/jobs/{job_id}", operation_id="image_worker_job_status")
async def status(job_id: UUID, lease: str = Depends(require_build_lease)):
    return service.serialize_job(await service.worker_status(str(job_id), lease), worker=True)


@router.get("/image-worker/jobs/{job_id}/inputs/{digest}", operation_id="download_image_job_input")
async def download_input(job_id: UUID, digest: str, lease: str = Depends(require_build_lease)):
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=404, detail="image job input not found")
    return await service.download_context(str(job_id), lease, normalized)


@router.put(
    "/image-worker/jobs/{job_id}/outputs/{digest}",
    operation_id="stage_image_job_output",
    openapi_extra=request_body(media_type="application/octet-stream"),
)
async def stage_output(job_id: UUID, digest: str, request: Request, lease: str = Depends(require_build_lease)):
    expected, size = content_descriptor(request, get_settings().palimpsest_hub_max_blob_bytes, digest)
    return service.serialize_job(await service.stage_output(str(job_id), lease, request, expected, size))


@router.post(
    "/image-worker/jobs/{job_id}/complete",
    operation_id="complete_image_job",
    openapi_extra=request_body(CompleteRequest),
)
async def complete(job_id: UUID, request: Request, lease: str = Depends(require_build_lease)):
    req = await bounded_json(request, CompleteRequest, 512 * 1024)
    return service.serialize_job(await service.complete(str(job_id), lease, req.model_dump(mode="json")), worker=True)


@router.post("/image-worker/jobs/{job_id}/fail", operation_id="fail_image_job", openapi_extra=request_body(Failure))
async def fail(job_id: UUID, request: Request, lease: str = Depends(require_build_lease)):
    req = await bounded_json(request, Failure, 1024)
    return service.serialize_job(await service.fail(str(job_id), lease, req.error_code))
