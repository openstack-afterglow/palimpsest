"""Durable image intake attempts and project-bound build contexts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BIGINT, CHAR, JSON, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from palimpsest_hub.models import Base, _now


class PalimpsestHubBuildContext(Base):
    __tablename__ = "palimpsest_hub_build_contexts"

    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    digest: Mapped[str] = mapped_column(VARCHAR(71), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)


class PalimpsestHubImageJob(Base):
    __tablename__ = "palimpsest_hub_image_jobs"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128))
    request_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    context_digest: Mapped[str | None] = mapped_column(VARCHAR(71))
    status: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="queued")
    staged_outputs: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    output_digests: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(32))
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (
        UniqueConstraint("project_id", "request_id", name="uq_image_job_project_request"),
        Index("idx_image_jobs_project_created", "project_id", "created_at"),
    )
