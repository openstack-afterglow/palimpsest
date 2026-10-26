"""Isolated real SQL/router fixture, with synthetic identity only; no production lifespan."""

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import APIRouter, FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub import auth, catalog_auth
from palimpsest_hub import catalog_policy as native_policy
from palimpsest_hub.api import catalog as catalog_api
from palimpsest_hub.api import hub as hub_api
from palimpsest_hub.api import packages
from palimpsest_hub.models import Base, PalimpsestHubBuild, PalimpsestHubLayer, PalimpsestHubLayerAccess
from palimpsest_hub.services import package_registry
from palimpsest_hub.services.hub_store import MEDIA_TYPE_IMAGE_QCOW2, MEDIA_TYPE_LAYER_SQUASHFS, LocalPathBlobStore


def digest(number):
    return "sha256:" + hashlib.sha256(payload(number)).hexdigest()


def payload(number):
    """Synthetic layer bytes, not a runtime-qualified SquashFS/cloud image."""
    return b"\0" * number + b"\n"


def layer(number, name, *, project="Project-A", published=False, parent=None, config=None):
    return PalimpsestHubLayer(
        blob_digest=digest(number),
        size_bytes=number + 1,
        media_type=MEDIA_TYPE_LAYER_SQUASHFS,
        config_digest=digest(999),
        name=name,
        kind="squashfs",
        arch="x86_64",
        parent_digest=digest(parent) if parent is not None else None,
        config_json=config or {},
        project_id=project,
        is_published=published,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )


@asynccontextmanager
async def isolated_catalog(root, *, include_packages=False):
    """root is caller-owned temporary storage; all authority lookups are synthetic.

    Actual require_global_admin executes on every request, including subject and policy
    fences. Only its external Keystone validation/context boundaries are replaced.
    Real CAS bytes are seeded for export transport only, not runtime qualification.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'catalog.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(root / "cas")
    for number in range(13):
        upload_id = f"{number:032x}"
        store.start_upload(upload_id)
        store.upload_path(upload_id).write_bytes(payload(number))
        finalized = store.finalize_upload(upload_id, digest(number))
        assert finalized.blob_digest == digest(number)
        store.abort_upload(upload_id)
    policy_file = root / "policy.yaml"
    policy_file.write_text("{}\n")
    settings = SimpleNamespace(oslo_policy_policy_file=str(policy_file), oslo_policy_policy_dirs=())
    state = SimpleNamespace(factory=factory, store=store, policy_file=policy_file, revoked=False, deny_policy=False)
    async with factory() as session:
        session.add_all(
            [
                layer(0, "base", published=True),
                layer(1, "torch-root", published=True, config={"base_image_digest": digest(0)}),
                layer(2, "torch-leaf", published=True, parent=1),
                layer(3, "private-child", project="Project-B", parent=2),
                layer(4, "private-root", project="Project-B"),
                layer(5, "public-hidden-parent", published=True, parent=4),
                layer(6, "shared-tensorflow", project="Project-B"),
                layer(7, "site-bundle", project=None),
                layer(8, "project-bundle"),
                layer(9, "cycle-a", published=True, parent=10),
                layer(10, "cycle-b", published=True, parent=9),
                layer(11, "missing-parent", published=True, parent=999),
                layer(12, "fake-build", published=True, config={"build_id": "caller-forged", "source": "hub-build"}),
            ]
        )
        base = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(0)))
        base.kind = "cloud-image"
        base.disk_format = "qcow2"
        base.media_type = MEDIA_TYPE_IMAGE_QCOW2
        await session.flush()
        session.add(PalimpsestHubLayerAccess(blob_digest=digest(6), project_id="Project-A"))
        session.add(
            PalimpsestHubBuild(
                id="00000000-0000-0000-0000-000000000001",
                project_id="Project-A",
                name="torch-leaf",
                recipe="private recipe must not escape",
                recipe_digest=digest(998),
                base_digest=digest(0),
                layer_digests=[digest(1)],
                status="complete",
                output_digest=digest(2),
                output_size_bytes=3,
            )
        )
        await session.commit()

    async def original(request: Request):
        token = request.headers.get("x-auth-token")
        if token not in {"human-admin", "member", "service-admin", "reader", "no-inventory"}:
            raise HTTPException(401, "Synthetic token required")
        return {
            "token": token,
            "user_id": token,
            "project_id": "Project-A",
            "is_system_admin": token in {"human-admin", "service-admin"},
        }

    def validate(token, project):
        if token not in {"human-admin", "member", "service-admin", "reader"}:
            raise HTTPException(401, "Synthetic token required")
        return {
            "token": token,
            "user_id": token,
            "project_id": project,
            "is_system_admin": token == "human-admin" and not state.revoked,
        }

    def admin_context(value, *, global_read=False):
        if not value.get("is_system_admin"):
            raise HTTPException(403, "Fresh human system administrator required")
        policy_file.write_text('{"system:manage": "!"}\n' if state.deny_policy else "{}\n")
        return {**value, "package_actions": native_policy.global_admin_actions(value["user_id"])}

    async def synthetic_member(token_info: dict):
        capabilities = set()
        if token_info["token"] != "no-inventory":
            capabilities.add("palimpsest-inventory_reader")
        if token_info["token"] == "member":
            capabilities.add("palimpsest-download_user")
        return {**token_info, "package_capabilities": frozenset(capabilities)}

    app = FastAPI()
    app.include_router(catalog_api.router, prefix="/v1")
    # Only the actual legacy read/export routes needed for transport proof; never
    # expose upload/delete/build admission in this synthetic-identity service.
    downloads = APIRouter()
    safe_reads = {"/layers/{digest}", "/layers/{digest}/ancestors", "/layers/{digest}/blob"}
    for route in hub_api.router.routes:
        if (route.path in safe_reads and route.methods == {"GET"}) or (
            route.path == "/bundles" and route.methods == {"POST"}
        ):
            downloads.routes.append(route)
    app.include_router(downloads, prefix="/v1")
    if include_packages:
        app.include_router(packages.router, prefix="/v1")
    app.dependency_overrides[auth.require_token] = original
    app.dependency_overrides[auth.get_token_info] = original
    state.app = app
    try:
        with (
            patch.object(hub_api, "get_session_factory", lambda: factory),
            patch.object(hub_api, "get_blob_store", lambda: store),
            patch.object(hub_api, "get_package_member_info", synthetic_member),
            patch.object(native_policy, "get_settings", lambda: settings),
            patch.object(package_registry, "get_session_factory", lambda: factory),
            patch.object(catalog_auth, "validate_token", validate),
            patch.object(catalog_auth, "_global_admin_context", admin_context),
        ):
            yield state
    finally:
        await engine.dispose()
