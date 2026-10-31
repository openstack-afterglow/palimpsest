"""Durable, caller-scoped instance intents and immutable CAS reuse bindings."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from palimpsest_hub.models import Base


class PalimpsestHubInstance(Base):
    __tablename__ = "palimpsest_hub_instances"
    __table_args__ = (UniqueConstraint("project_id", "request_id", name="uq_instance_project_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Never credentials: includes the sealed ordered layer_digests/runtime/provenance snapshot.
    spec_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_instance_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    server_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    boot_volume_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_recorded_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    capability_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capability_expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=lambda: datetime.now(UTC))
