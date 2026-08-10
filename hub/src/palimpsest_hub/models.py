from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BIGINT, BOOLEAN, CHAR, INT, JSON, TEXT, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class PalimpsestHubLayer(Base):
    __tablename__ = "palimpsest_hub_layers"

    id: Mapped[int] = mapped_column(INT, primary_key=True, autoincrement=True)
    blob_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False, unique=True)
    blob_md5: Mapped[str | None] = mapped_column(CHAR(32), nullable=True, index=True)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    media_type: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    disk_format: Mapped[str | None] = mapped_column(VARCHAR(16), nullable=True)
    arch: Mapped[str | None] = mapped_column(VARCHAR(16), nullable=True)
    os_variant: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    config_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    chain_id: Mapped[str | None] = mapped_column(VARCHAR(71), nullable=True, index=True)
    parent_digest: Mapped[str | None] = mapped_column(VARCHAR(71), nullable=True, index=True)
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, index=True)
    ubuntu_base: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    python_version: Mapped[str | None] = mapped_column(VARCHAR(16), nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True, index=True)
    is_published: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False, server_default="0")
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)


class PalimpsestHubUpload(Base):
    __tablename__ = "palimpsest_hub_uploads"

    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    declared_digest: Mapped[str | None] = mapped_column(VARCHAR(71), nullable=True)
    received_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    project_id: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now, onupdate=_now)


class PalimpsestImageExport(Base):
    __tablename__ = "palimpsest_image_exports"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    source_image_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    source_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    source_disk_format: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    source_virtual_size_bytes: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    source_checksum: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    source_hash_algo: Mapped[str | None] = mapped_column(VARCHAR(16), nullable=True)
    source_hash_value: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    source_updated_at: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    source_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    artifact_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    target_disk_format: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    result_blob_digest: Mapped[str | None] = mapped_column(VARCHAR(71), nullable=True)
    result_size_bytes: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    status: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="queued", server_default="queued")
    progress_pct: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    attempts: Mapped[int] = mapped_column(INT, nullable=False, default=0, server_default="0")
    next_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now, onupdate=_now)
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (
        UniqueConstraint("project_id", "artifact_key", name="uq_palimpsest_exports_project_artifact"),
        Index("idx_palimpsest_exports_artifact", "artifact_key"),
        Index("idx_palimpsest_exports_digest", "result_blob_digest"),
        Index("idx_palimpsest_exports_claim", "status", "next_at"),
        Index("idx_palimpsest_exports_project_created", "project_id", "deleted_at", "created_at"),
    )
