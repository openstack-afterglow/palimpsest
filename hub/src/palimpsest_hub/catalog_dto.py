"""Explicit read-only catalog projections; never raw stored configuration."""

from typing import Literal

from pydantic import BaseModel

Visibility = Literal["public", "site", "project", "shared", "private"]


class CatalogOrigin(BaseModel):
    kind: Literal["build", "image-export", "registered", "unknown"]
    source: Literal[
        "hub-build", "dockerfile-inline", "github-dockerfile", "recipe", "glance", "upload-or-import", "unknown"
    ]
    evidence: Literal["recorded", "unrecorded"]
    build_id: str | None = None
    job_id: str | None = None
    revision: str | None = None


class CatalogLayer(BaseModel):
    id: str
    name: str
    blob_digest: str | None
    parent_id: str | None
    parent_digest: str | None
    kind: str
    arch: str | None
    ubuntu_base: str | None
    size_bytes: int | None
    created_at: str | None
    project_id: str | None
    visibility: Visibility
    chain_complete: bool
    origin: CatalogOrigin


class CatalogLayerPage(BaseModel):
    source: Literal["hub"] = "hub"
    items: list[CatalogLayer]
    offset: int
    has_more: bool
    next_offset: int | None


class CatalogLayerDetail(CatalogLayer):
    ancestors: list[CatalogLayer]
    children: list[CatalogLayer]
    children_has_more: bool
    bundles: list[str]


class Asset(BaseModel):
    id: str
    name: str
    kind: Literal["layer-bundle", "cloud-image"]
    blob_digest: str
    chain_id: str | None
    arch: str | None
    ubuntu_base: str | None
    size_bytes: int | None
    created_at: str | None
    visibility: Literal["public", "site", "project", "shared"]
    layer_count: int
    chain_complete: bool
    download_allowed: bool
    base_image_digest: str | None
    local_supported: bool
    local_reason: (
        Literal["base-image-required", "base-image-format-unsupported", "architecture-unknown", "architecture-mismatch"]
        | None
    )


class AssetPage(BaseModel):
    items: list[Asset]
    offset: int
    has_more: bool
    next_offset: int | None
