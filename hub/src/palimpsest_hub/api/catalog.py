"""Separate operator inventory from caller-visible named bundle discovery."""

from fastapi import APIRouter, Depends, Query, Response

from palimpsest_hub.api.hub import _legacy_inventory
from palimpsest_hub.catalog_auth import require_global_admin
from palimpsest_hub.catalog_dto import AssetPage, CatalogLayerDetail, CatalogLayerPage
from palimpsest_hub.services import catalog

router = APIRouter(tags=["catalog"])


@router.get("/catalog/assets", response_model=AssetPage, operation_id="list_catalog_assets")
async def assets(
    response: Response,
    q: str | None = Query(None, max_length=128),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    token_info: dict = Depends(_legacy_inventory),
):
    response.headers["Cache-Control"] = "no-store"
    return await catalog.assets(token_info, q, offset, limit)


@router.get("/admin/catalog/layers", response_model=CatalogLayerPage, operation_id="list_admin_catalog_layers")
async def layers(
    response: Response,
    q: str | None = Query(None, max_length=128),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    token_info: dict = Depends(require_global_admin),
):
    response.headers["Cache-Control"] = "no-store"
    return await catalog.layers(token_info, q, offset, limit)


@router.get("/admin/catalog/layers/{digest}", response_model=CatalogLayerDetail, operation_id="get_admin_catalog_layer")
async def detail(digest: str, response: Response, token_info: dict = Depends(require_global_admin)):
    response.headers["Cache-Control"] = "no-store"
    return await catalog.detail(token_info, digest)
