from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BIGINT,
    BOOLEAN,
    CHAR,
    INT,
    JSON,
    TEXT,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    cast,
    false,
)
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


def exact_identity(column, value: str | None):
    """Retain indexed narrowing while enforcing bytes on legacy CI-collated SQL."""
    if value is None:
        return false()
    return (column == value) & (cast(column, LargeBinary) == value.encode("ascii"))


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


class PalimpsestHubLayerAccess(Base):
    __tablename__ = "palimpsest_hub_layer_access"

    blob_digest: Mapped[str] = mapped_column(
        VARCHAR(71),
        ForeignKey("palimpsest_hub_layers.blob_digest", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
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


class PalimpsestHubBuild(Base):
    __tablename__ = "palimpsest_hub_builds"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128))
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    recipe: Mapped[str] = mapped_column(TEXT, nullable=False)
    recipe_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    base_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    layer_digests: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="queued")
    output_digest: Mapped[str | None] = mapped_column(VARCHAR(71))
    output_size_bytes: Mapped[int | None] = mapped_column(BIGINT)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(32))
    lease_owner: Mapped[str | None] = mapped_column(VARCHAR(128))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))

    __table_args__ = (Index("idx_palimpsest_builds_project_created", "project_id", "created_at"),)


def _binary_name(length: int):
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_bin"), "mysql")


class PackageNamespace(Base):
    __tablename__ = "palimpsest_package_namespaces"
    project_id: Mapped[str] = mapped_column(_binary_name(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(_binary_name(63), nullable=False, unique=True)
    project_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)


class RegistryPackage(Base):
    __tablename__ = "palimpsest_packages"
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("palimpsest_package_namespaces.project_id"), nullable=False)
    name: Mapped[str] = mapped_column(_binary_name(255), nullable=False)
    package_type: Mapped[str] = mapped_column(VARCHAR(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    __table_args__ = (UniqueConstraint("project_id", "name"),)


class PackageKey(Base):
    __tablename__ = "palimpsest_package_keys"
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("palimpsest_package_namespaces.project_id"), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(_binary_name(64), nullable=False, index=True)
    secret_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    scope: Mapped[dict] = mapped_column(JSON, nullable=False)
    actions: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)


class PackageVersion(Base):
    __tablename__ = "palimpsest_package_versions"
    package_id: Mapped[str] = mapped_column(ForeignKey("palimpsest_packages.id"), primary_key=True)
    root_digest: Mapped[str] = mapped_column(CHAR(71), primary_key=True)
    root_media_type: Mapped[str] = mapped_column(VARCHAR(128), nullable=False)
    graph: Mapped[dict] = mapped_column(JSON, nullable=False)
    platforms: Mapped[list] = mapped_column(JSON, nullable=False)
    archive_digest: Mapped[str] = mapped_column(CHAR(71), nullable=False)
    archive_size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False)
    pushed_by: Mapped[str] = mapped_column(_binary_name(64), nullable=False)
    pushed_key_id: Mapped[str] = mapped_column(ForeignKey("palimpsest_package_keys.id"), nullable=False)
    pushed_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)


class PackageBlobReference(Base):
    __tablename__ = "palimpsest_package_blob_references"
    package_id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    root_digest: Mapped[str] = mapped_column(CHAR(71), primary_key=True)
    blob_digest: Mapped[str] = mapped_column(CHAR(71), primary_key=True, index=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["package_id", "root_digest"],
            ["palimpsest_package_versions.package_id", "palimpsest_package_versions.root_digest"],
        ),
    )


class PackageTag(Base):
    __tablename__ = "palimpsest_package_tags"
    package_id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    tag: Mapped[str] = mapped_column(_binary_name(128), primary_key=True)
    root_digest: Mapped[str] = mapped_column(CHAR(71), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    updated_by: Mapped[str] = mapped_column(_binary_name(64), nullable=False)
    revision: Mapped[int] = mapped_column(BIGINT, nullable=False, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["package_id", "root_digest"],
            ["palimpsest_package_versions.package_id", "palimpsest_package_versions.root_digest"],
        ),
    )


class PackageUpload(Base):
    __tablename__ = "palimpsest_package_uploads"
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("palimpsest_package_namespaces.project_id"), nullable=False, index=True
    )
    package: Mapped[str] = mapped_column(_binary_name(255), nullable=False)
    key_id: Mapped[str] = mapped_column(ForeignKey("palimpsest_package_keys.id"), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(_binary_name(64), nullable=False)
    resource: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    status: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="uploading")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class PackageCache(Base):
    __tablename__ = "palimpsest_package_caches"
    id: Mapped[str] = mapped_column(CHAR(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("palimpsest_package_namespaces.project_id"), nullable=False)
    package: Mapped[str] = mapped_column(_binary_name(255), nullable=False)
    build_key: Mapped[str] = mapped_column(CHAR(71), nullable=False)
    cache_scope: Mapped[str] = mapped_column(VARCHAR(48), nullable=False)
    platform: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    builder_fingerprint: Mapped[str] = mapped_column(_binary_name(128), nullable=False)
    archive_digest: Mapped[str] = mapped_column(CHAR(71), nullable=False, index=True)
    archive_size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)
    created_by: Mapped[str] = mapped_column(_binary_name(64), nullable=False)
    __table_args__ = (
        Index("idx_package_cache_partition", "project_id", "package", "cache_scope", "platform", "builder_fingerprint"),
    )
