"""Original-caller native intents; SQL uncertainty never authorizes replay."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CHAR, JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from palimpsest_hub.models import Base


class PalimpsestNativeInstance(Base):
    __tablename__ = "palimpsest_native_instances"
    __table_args__ = (UniqueConstraint("project_id", "request_id", name="uq_native_project_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    environment_digest: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    image_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("palimpsest_hub_published_images.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    run_id: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    spec_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    journal_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    server_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=lambda: datetime.now(UTC))
