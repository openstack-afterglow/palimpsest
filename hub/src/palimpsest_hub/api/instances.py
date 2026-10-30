"""Original-human-token instance API and narrow fixed-expiry guest capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from palimpsest_hub.api.hub import (
    _factory_or_503,
    _legacy_inventory,
    _legacy_writer,
    _store_or_503,
)
from palimpsest_hub.auth import get_os_conn, package_target_project_id
from palimpsest_hub.config import get_settings
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.models import exact_identity
from palimpsest_hub.services import instances as service
from palimpsest_hub.services.digest import normalize_digest
from palimpsest_hub.services.instance_cluster_runtime import (
    ClusterErrorCode,
    cluster_capability,
    cluster_join_token,
    validate_cluster_result,
)
from palimpsest_hub.services.instance_gpu_runtime import GpuErrorCode
from palimpsest_hub.services.worker_leases import run_blocking, sql_mutex

router = APIRouter()


class InstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    image_id: UUID
    flavor_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    gpu_profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    cluster_profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    cluster_server_id: UUID | None = None
    network_id: UUID
    security_group_ids: list[UUID] = Field(default_factory=list, max_length=16)
    layer_digests: list[str] = Field(default_factory=list, max_length=128)
    source_instance_id: UUID | None = None
    image_job_id: UUID | None = None
    command: list[str] = Field(default_factory=list, max_length=128)
    readiness_argv: list[str] = Field(default_factory=list, max_length=128)
    boot_gib: int = Field(ge=1, le=4096, strict=True)
    writable_gib: int = Field(
        default=8,
        ge=1,
        le=1024,
        strict=True,
        description="Guest-private disk-backed ext4 OverlayFS capacity; smaller than boot_gib",
    )

    @field_validator("layer_digests")
    @classmethod
    def layers(cls, values):
        digests = [normalize_digest(item) for item in values]
        if any(item is None for item in digests):
            raise ValueError("layer_digests must be SHA256 digests")
        return digests

    @field_validator("command", "readiness_argv")
    @classmethod
    def argv(cls, values):
        if (
            any("\x00" in value or len(value.encode()) > 4096 for value in values)
            or sum(len(value.encode()) for value in values) > 16384
        ):
            raise ValueError("command arguments exceed instance limits")
        if values and not values[0]:
            raise ValueError("executable cannot be empty")
        return values

    @model_validator(mode="after")
    def stack(self):
        if self.source_instance_id and self.layer_digests:
            raise ValueError("source_instance_id already selects the exact retained layer stack")
        if not (self.layer_digests or self.source_instance_id or self.image_job_id):
            raise ValueError("select layer_digests, image_job_id, or source_instance_id")
        if len(set(self.security_group_ids)) != len(self.security_group_ids):
            raise ValueError("security_group_ids must be distinct")
        if self.source_instance_id and self.image_job_id:
            raise ValueError("source_instance_id already selects immutable image provenance")
        if self.writable_gib >= self.boot_gib:
            raise ValueError("writable_gib must be smaller than boot_gib")
        if self.cluster_server_id and not self.cluster_profile:
            raise ValueError("cluster_server_id requires cluster_profile")
        if self.cluster_profile and (self.gpu_profile or self.command or self.readiness_argv):
            raise ValueError("cluster_profile excludes GPU and caller-selected execution")
        return self


@router.post("/instances", status_code=202, operation_id="create_hub_instance")
async def create_instance(req: InstanceRequest, actor: dict = Depends(_legacy_writer), conn=Depends(get_os_conn)):
    settings = get_settings()
    origin = urlsplit(settings.palimpsest_hub_package_public_origin)
    if (
        origin.scheme != "https"
        or not origin.netloc
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in ("", "/")
    ):
        raise HTTPException(503, "A canonical HTTPS public origin is required for instance bootstrap")
    return await service.create(_factory_or_503(), _store_or_503(), settings, actor, conn, req.model_dump(mode="json"))


@router.get("/instances", operation_id="list_hub_instances")
async def list_instances(limit: int = Query(50, ge=1, le=100), actor: dict = Depends(_legacy_inventory)):
    async with _factory_or_503()() as session:
        rows = (
            await session.scalars(
                select(PalimpsestHubInstance)
                .where(exact_identity(PalimpsestHubInstance.project_id, package_target_project_id(actor)))
                .order_by(PalimpsestHubInstance.created_at.desc(), PalimpsestHubInstance.id.desc())
                .limit(limit)
            )
        ).all()
        return [service.projection(row) for row in rows]


@router.get("/instances/{instance_id}", operation_id="get_hub_instance")
async def get_instance(instance_id: UUID, actor: dict = Depends(_legacy_inventory), conn=Depends(get_os_conn)):
    return await service.observe(_factory_or_503(), actor, conn, str(instance_id))


@router.post("/instances/{instance_id}/stop", status_code=202, operation_id="stop_hub_instance")
async def stop_instance(instance_id: UUID, actor: dict = Depends(_legacy_writer), conn=Depends(get_os_conn)):
    return await service.action(_factory_or_503(), actor, conn, str(instance_id), "stop")


@router.delete("/instances/{instance_id}", status_code=202, operation_id="delete_hub_instance")
async def delete_instance(instance_id: UUID, actor: dict = Depends(_legacy_writer), conn=Depends(get_os_conn)):
    return await service.action(_factory_or_503(), actor, conn, str(instance_id), "delete")


def bearer(request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not 32 <= len(header[7:]) <= 128:
        raise HTTPException(404, "Bootstrap capability not found or expired")
    return header[7:]


@router.get("/instance-bootstrap/{instance_id}/layers/{digest}", operation_id="get_instance_bootstrap_layer")
async def bootstrap_layer(instance_id: UUID, digest: str, request: Request):
    digest = normalize_digest(digest)
    if digest is None:
        raise HTTPException(404, "Layer not found")
    factory = _factory_or_503()
    async with factory() as session:
        row = await session.get(PalimpsestHubInstance, str(instance_id))
        if row is None:
            raise HTTPException(404, "Bootstrap capability not found or expired")
        service.capability(row, bearer(request))
        if digest not in row.spec_json["layer_digests"]:
            raise HTTPException(404, "Layer not found")
        try:
            stream = await run_blocking(_store_or_503().open_read, digest)
        except Exception:
            raise HTTPException(409, "Bound layer is unavailable") from None
        expected_size = next(layer["size_bytes"] for layer in row.spec_json["layers"] if layer["digest"] == digest)

    async def chunks():
        sent = 0
        try:
            while sent < expected_size:
                # Revocation/expiry applies even to an already-open long download.
                async with factory() as session:
                    current = await session.get(PalimpsestHubInstance, str(instance_id))
                    if current is None:
                        return
                    service.capability(current, bearer(request))
                chunk = await run_blocking(stream.read, min(1024 * 1024, expected_size - sent))
                if not chunk:
                    raise RuntimeError("Bound layer length changed")
                sent += len(chunk)
                yield chunk
            if await run_blocking(stream.read, 1):
                raise RuntimeError("Bound layer length changed")
        finally:
            await run_blocking(stream.close)

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "Content-Length": str(expected_size)},
    )


@router.get("/instance-bootstrap/{instance_id}/cluster", operation_id="get_instance_bootstrap_cluster")
async def bootstrap_cluster(instance_id: UUID, request: Request, response: Response):
    async with _factory_or_503()() as session:
        row = await session.get(PalimpsestHubInstance, str(instance_id))
        if row is None:
            raise HTTPException(404, "Bootstrap capability not found or expired")
        service.capability(row, bearer(request))
        result = await cluster_capability(session, row, get_settings())
    response.headers["Cache-Control"] = "no-store"
    return result


class InstanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: UUID
    status: Literal["ready", "error"]
    exit_code: int | None = Field(None, ge=-255, le=255)
    stdout: str = Field(default="", max_length=8192)
    stderr: str = Field(default="", max_length=8192)
    error_code: Literal["guest_execution_failed", "command_failed"] | GpuErrorCode | ClusterErrorCode | None = None

    @model_validator(mode="after")
    def outcome(self):
        if len(self.stdout.encode()) > 32768 or len(self.stderr.encode()) > 32768:
            raise ValueError("guest output exceeds byte limit")
        if self.status == "ready" and (self.error_code is not None or self.exit_code not in (0, None)):
            raise ValueError("ready result contradicts command outcome")
        if self.status == "error" and self.error_code is None:
            raise ValueError("error result requires a bounded error code")
        return self


@router.put(
    "/instance-bootstrap/{instance_id}/result",
    status_code=204,
    operation_id="put_instance_bootstrap_result",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": InstanceResult.model_json_schema()}},
        }
    },
)
async def bootstrap_result(instance_id: UUID, request: Request):
    # Bound before JSON parsing, including chunked request bodies.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 70000:
            raise HTTPException(413, "Guest result exceeds byte limit")
    try:
        result = InstanceResult.model_validate_json(bytes(body))
    except ValueError:
        raise HTTPException(422, "Invalid bounded guest result") from None
    if str(result.instance_id) != str(instance_id):
        raise HTTPException(409, "Guest result identity does not match capability")
    async with sql_mutex(_factory_or_503(), "build-api:instance:" + str(instance_id)) as session:
        row = await session.get(PalimpsestHubInstance, str(instance_id))
        if row is None:
            raise HTTPException(404, "Bootstrap capability not found or expired")
        service.capability(row, bearer(request))
        if row.status in ("stopping", "stopped"):
            raise HTTPException(409, "Stopped instance cannot report new readiness")
        payload = result.model_dump(mode="json")
        for channel in ("stdout", "stderr"):
            payload[channel] = payload[channel].replace(bearer(request), "[redacted bootstrap capability]")
        if row.spec_json.get("cluster"):
            settings = get_settings()
            token = cluster_join_token(row, settings)
            for channel in ("stdout", "stderr"):
                payload[channel] = payload[channel].replace(token, "[redacted cluster token]")
            if payload["status"] == "ready":
                validate_cluster_result(payload, await cluster_capability(session, row, settings))
        if (
            payload["status"] == "ready"
            and payload["exit_code"] is None
            and not row.spec_json["readiness_argv"]
            and not row.spec_json.get("cluster")
        ):
            raise HTTPException(422, "A running command requires an explicit readiness probe")
        if row.result_json is not None and row.result_json != payload:
            raise HTTPException(409, "Instance guest result is already sealed")
        row.result_json = payload
        if row.result_recorded_at is None:
            row.result_recorded_at = datetime.now(UTC)
        row.status = payload["status"]
        row.error_code = payload["error_code"]
        row.updated_at = datetime.now(UTC)
        await session.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
