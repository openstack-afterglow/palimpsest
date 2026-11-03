"""Durable image publications, personal staging and image-scoped graph access."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BIGINT, CHAR, JSON, ForeignKey, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from palimpsest_hub.models import Base, _now

# Pre-publication-era image jobs were project-wide, not personal/private.
LEGACY_JOB_VISIBILITY = "internal"


class PalimpsestHubBuildContext(Base):
    __tablename__ = "palimpsest_hub_build_contexts"

    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    digest: Mapped[str] = mapped_column(VARCHAR(71), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    created_by: Mapped[str] = mapped_column(VARCHAR(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)


class PalimpsestHubImageJob(Base):
    __tablename__ = "palimpsest_hub_image_jobs"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128))
    request_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    kind: Mapped[str] = mapped_column(VARCHAR(16), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    visibility: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="private")
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


class PalimpsestHubLayerNode(Base):
    """Project-visible logical occurrence; physical payload rows remain parentless."""

    __tablename__ = "palimpsest_hub_layer_nodes"

    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    id: Mapped[str] = mapped_column(VARCHAR(71), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(VARCHAR(71))
    blob_digest: Mapped[str] = mapped_column(
        VARCHAR(71), ForeignKey("palimpsest_hub_layers.blob_digest", ondelete="RESTRICT"), nullable=False
    )
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    compatibility_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "parent_id"],
            ["palimpsest_hub_layer_nodes.project_id", "palimpsest_hub_layer_nodes.id"],
            ondelete="RESTRICT",
        ),
        Index("idx_layer_nodes_project_parent", "project_id", "parent_id"),
    )


class PalimpsestHubPublishedImage(Base):
    __tablename__ = "palimpsest_hub_published_images"

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    created_by: Mapped[str | None] = mapped_column(VARCHAR(128))
    request_id: Mapped[str] = mapped_column(CHAR(36), nullable=False)
    name: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    visibility: Mapped[str] = mapped_column(VARCHAR(16), nullable=False, default="private")
    manifest_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    leaf_id: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    storage_binding_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "leaf_id"],
            ["palimpsest_hub_layer_nodes.project_id", "palimpsest_hub_layer_nodes.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("project_id", "request_id", name="uq_published_image_project_request"),
        Index("idx_published_images_project_created", "project_id", "created_at"),
    )


class PalimpsestHubPublicationPayload(Base):
    """Bytes staged by an exact user and project, not a project-wide CAS grant."""

    __tablename__ = "palimpsest_hub_publication_payloads"

    project_id: Mapped[str] = mapped_column(VARCHAR(64), primary_key=True)
    created_by: Mapped[str] = mapped_column(VARCHAR(128), primary_key=True)
    blob_digest: Mapped[str] = mapped_column(VARCHAR(71), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=_now)


class PalimpsestHubImageMembership(Base):
    """Publication references, not CAS ownership, authorize each logical node."""

    __tablename__ = "palimpsest_hub_image_memberships"

    image_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("palimpsest_hub_published_images.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    project_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
    node_id: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    blob_digest: Mapped[str] = mapped_column(VARCHAR(71), nullable=False)
    # Empty means independently supplied/unrestricted. Otherwise every origin
    # project ceiling survives later publications, even by an image's creator.
    reuse_projects_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "node_id"],
            ["palimpsest_hub_layer_nodes.project_id", "palimpsest_hub_layer_nodes.id"],
            ondelete="RESTRICT",
        ),
        Index("idx_image_membership_node", "project_id", "node_id"),
        Index("idx_image_membership_payload", "blob_digest"),
    )
