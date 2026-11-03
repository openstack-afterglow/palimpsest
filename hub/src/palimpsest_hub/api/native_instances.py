"""Opt-in native API; never dispatches to the conventional HTTPS instance lane."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from palimpsest_hub.api.hub import _factory_or_503, _legacy_inventory, _legacy_writer, _store_or_503
from palimpsest_hub.auth import get_os_conn
from palimpsest_hub.config import get_settings
from palimpsest_hub.services import native_instances as service

router = APIRouter()


class NativeInstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    image_id: UUID
    profile: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    phase: Literal["cpu", "gpu"] = "cpu"
    timeout: int = Field(ge=1, le=3600, strict=True)
    command: list[str] = Field(min_length=1, max_length=128)
    keep: bool = Field(default=True, strict=True)

    @field_validator("command")
    @classmethod
    def bounded_command(cls, value):
        if not value[0] or any("\x00" in item or len(item.encode()) > 4096 for item in value):
            raise ValueError("invalid native command argument")
        if sum(len(item.encode()) for item in value) > 16384:
            raise ValueError("native command exceeds byte bound")
        return value


@router.post("/native-instances", status_code=202, operation_id="create_native_instance")
async def create(
    req: NativeInstanceRequest,
    background: BackgroundTasks,
    actor: dict = Depends(_legacy_writer),
    conn=Depends(get_os_conn),
):
    return await service.create(
        _factory_or_503(),
        _store_or_503(),
        get_settings(),
        actor,
        conn,
        req.model_dump(mode="json"),
        background=background,
    )


@router.get("/native-instances", operation_id="list_native_instances")
async def list_instances(limit: int = Query(50, ge=1, le=100), actor: dict = Depends(_legacy_inventory)):
    return await service.list_instances(_factory_or_503(), actor, limit)


@router.get("/native-instances/{instance_id}", operation_id="get_native_instance")
async def get(instance_id: UUID, actor: dict = Depends(_legacy_inventory)):
    return await service.get(_factory_or_503(), actor, str(instance_id))


@router.delete("/native-instances/{instance_id}", status_code=202, operation_id="delete_native_instance")
async def delete(
    instance_id: UUID,
    background: BackgroundTasks,
    actor: dict = Depends(_legacy_writer),
    conn=Depends(get_os_conn),
):
    return await service.delete(
        _factory_or_503(),
        get_settings(),
        actor,
        conn,
        str(instance_id),
        background=background,
    )
