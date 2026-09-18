"""Palimpsest Hub API, blob store, OCI bundle, and upload-offset regression tests."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from palimpsest_hub.api import hub as hub_api
from palimpsest_hub.api.hub import (
    HubLayerMeta,
    HubUploadStartRequest,
    finalize_upload,
    start_upload,
)
from palimpsest_hub.main import app
from palimpsest_hub.models import (
    Base,
    PalimpsestHubLayer,
    PalimpsestHubLayerAccess,
    PalimpsestHubUpload,
)
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_bundle import (
    BundleError,
    BundleLayer,
    build_manifest,
    iter_bundle_tar,
    parse_bundle,
)
from palimpsest_hub.services.hub_store import (
    KIND_BUILDKIT_CACHE,
    MEDIA_TYPE_BUILDKIT_CACHE,
    MEDIA_TYPE_IMAGE_QCOW2,
    MEDIA_TYPE_LAYER_SQUASHFS,
    HubDigestMismatch,
    HubStoreError,
    LocalPathBlobStore,
    write_upload_stream,
)


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> LocalPathBlobStore:
    return LocalPathBlobStore(tmp_path / "hub")


def _put_blob(store: LocalPathBlobStore, payload: bytes) -> str:
    session_id = "a" * 32
    store.start_upload(session_id)
    with store.upload_path(session_id).open("wb") as handle:
        handle.write(payload)
    return store.finalize_upload(session_id, None).blob_digest


# ---------------------------------------------------------------------------
# 1. Blob Store Invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_digest", ["../escape", "sha256:nothex", "sha256:123", "sha256:" + "g" * 64])
def test_blob_path_rejects_traversal_and_malformed_digest(store: LocalPathBlobStore, bad_digest: str):
    with pytest.raises(HubStoreError):
        store.blob_path(bad_digest)


@pytest.mark.parametrize("bad_session", ["../escape", "gg" * 16, "short", "a" * 33])
def test_upload_path_rejects_malformed_session_id(store: LocalPathBlobStore, bad_session: str):
    with pytest.raises(HubStoreError):
        store.upload_path(bad_session)


def test_upload_finalize_places_blob_at_content_addressed_path(store: LocalPathBlobStore):
    payload = b"palimpsest layer bytes"
    digest = _put_blob(store, payload)
    expected = store.root / "blobs" / "sha256" / digest[len("sha256:") :]
    assert expected.is_file()
    assert expected.read_bytes() == payload


def test_finalize_rejects_declared_digest_mismatch_and_discards_bytes(store: LocalPathBlobStore):
    session_id = "a" * 32
    store.start_upload(session_id)
    with store.upload_path(session_id).open("wb") as handle:
        handle.write(b"actual payload")
    wrong_digest = "sha256:" + "b" * 64
    with pytest.raises(HubDigestMismatch):
        store.finalize_upload(session_id, wrong_digest)
    assert not store.upload_path(session_id).exists()
    assert not (store.root / "blobs" / "sha256" / ("b" * 64)).exists()


def test_finalize_is_idempotent_for_identical_content(store: LocalPathBlobStore):
    payload = b"same content"
    first = _put_blob(store, payload)
    second = _put_blob(store, payload)
    assert first == second
    assert store.exists(first)
    assert store.size(first) == len(payload)


def test_iter_blob_supports_range_reads(store: LocalPathBlobStore):
    payload = bytes(range(256)) * 8
    digest = _put_blob(store, payload)

    full = b"".join(store.iter_blob(digest))
    assert full == payload

    middle = b"".join(store.iter_blob(digest, start=100, length=50))
    assert middle == payload[100:150]


@pytest.mark.asyncio
async def test_write_upload_stream_aborts_when_exceeding_limit(store: LocalPathBlobStore):
    session_id = "e" * 32
    store.start_upload(session_id)

    async def _oversized_stream():
        yield b"x" * (1024 * 1024)

    with pytest.raises(HubStoreError, match="상한을 초과"):
        await write_upload_stream(store, session_id, _oversized_stream(), already_received=0, max_bytes=100)
    assert not store.upload_path(session_id).exists()


# ---------------------------------------------------------------------------
# 2. OCI Bundle Invariants
# ---------------------------------------------------------------------------


def _chain(store: LocalPathBlobStore) -> list[BundleLayer]:
    root_bytes = b"root layer"
    child_bytes = b"child layer"
    leaf_bytes = b"leaf layer"

    root_digest = _put_blob(store, root_bytes)
    child_digest = _put_blob(store, child_bytes)
    leaf_digest = _put_blob(store, leaf_bytes)

    return [
        BundleLayer(blob_digest=root_digest, size_bytes=len(root_bytes), name="root", config={"name": "root"}),
        BundleLayer(
            blob_digest=child_digest,
            size_bytes=len(child_bytes),
            name="child",
            config={"name": "child", "parent_digest": root_digest},
        ),
        BundleLayer(
            blob_digest=leaf_digest,
            size_bytes=len(leaf_bytes),
            name="leaf",
            config={"name": "leaf", "parent_digest": child_digest},
        ),
    ]


def test_bundle_is_a_valid_oci_image_layout(store: LocalPathBlobStore):
    chain = _chain(store)
    chunks = iter_bundle_tar(store, [chain])

    raw = b"".join(chunks)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        members = {member.name for member in tf.getmembers()}

    assert "oci-layout" in members
    assert "index.json" in members
    assert all(name.startswith("blobs/sha256/") for name in members if name not in {"oci-layout", "index.json"})


def test_bundle_round_trips_through_parse(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store)
    bundle_path = tmp_path / "bundle.tar"
    with bundle_path.open("wb") as out:
        for chunk in iter_bundle_tar(store, [chain]):
            out.write(chunk)

    parsed = parse_bundle(bundle_path)
    leaf = chain[-1]
    staged = tmp_path / "staged.sqsh"

    from palimpsest_hub.services.hub_bundle import extract_blob

    extract_blob(bundle_path, parsed.blob_members[leaf.blob_digest], staged)
    assert _sha256(staged.read_bytes()) == leaf.blob_digest


def test_parse_bundle_reconstructs_parent_chain_from_manifest_order(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store)
    bundle_path = tmp_path / "bundle.tar"
    with bundle_path.open("wb") as out:
        for chunk in iter_bundle_tar(store, [chain]):
            out.write(chunk)

    parsed = parse_bundle(bundle_path)
    parents = [entry["parent_digest"] for entry in parsed.layers]
    assert parents == [None, chain[0].blob_digest, chain[1].blob_digest]


def test_build_manifest_rejects_empty_chain():
    with pytest.raises(BundleError):
        build_manifest([], {})


# ---------------------------------------------------------------------------
# 3. Discovery & Health Routes
# ---------------------------------------------------------------------------


def test_discovery_and_health_endpoints():
    client = TestClient(app)

    root_resp = client.get("/")
    assert root_resp.status_code == 200
    root_data = root_resp.json()
    assert "versions" in root_data
    assert root_data["versions"][0]["id"] == "v1.0"

    v1_resp = client.get("/v1/")
    assert v1_resp.status_code == 200
    v1_data = v1_resp.json()
    assert "version" in v1_data
    assert v1_data["version"]["id"] == "v1.0"

    h1_resp = client.get("/v1/health")
    assert h1_resp.status_code == 200
    assert h1_resp.json() == {"status": "ok"}

    h_resp = client.get("/health")
    assert h_resp.status_code == 200
    assert h_resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 4. Schema Models & Validation
# ---------------------------------------------------------------------------


def test_cloud_image_meta_resolves_media_type_by_disk_format():
    assert HubLayerMeta(name="torch", kind="squashfs").resolved_media_type() == MEDIA_TYPE_LAYER_SQUASHFS
    assert (
        HubLayerMeta(name="ubuntu", kind="cloud-image", disk_format="qcow2", arch="x86_64").resolved_media_type()
        == MEDIA_TYPE_IMAGE_QCOW2
    )
    assert (
        HubLayerMeta(
            name="ubuntu",
            kind="cloud-image",
            disk_format="qcow2",
            arch="x86_64",
            media_type=MEDIA_TYPE_IMAGE_QCOW2,
        ).resolved_media_type()
        == MEDIA_TYPE_IMAGE_QCOW2
    )


def test_buildkit_cache_meta_resolves_dedicated_media_type():
    chain_id = "sha256:" + "d" * 64
    assert (
        HubLayerMeta(name="dockerfile-cache", kind=KIND_BUILDKIT_CACHE, chain_id=chain_id).resolved_media_type()
        == MEDIA_TYPE_BUILDKIT_CACHE
    )
    assert (
        HubLayerMeta(
            name="dockerfile-cache",
            kind=KIND_BUILDKIT_CACHE,
            chain_id=chain_id,
            media_type=MEDIA_TYPE_BUILDKIT_CACHE,
        ).resolved_media_type()
        == MEDIA_TYPE_BUILDKIT_CACHE
    )


def test_buildkit_cache_requires_key_and_rejects_runtime_chain_fields():
    with pytest.raises(ValueError, match="chain_id"):
        HubLayerMeta(name="dockerfile-cache", kind=KIND_BUILDKIT_CACHE)
    with pytest.raises(ValueError, match="runtime parent/base"):
        HubLayerMeta(
            name="dockerfile-cache",
            kind=KIND_BUILDKIT_CACHE,
            chain_id="sha256:" + "d" * 64,
            base_image_digest="sha256:" + "e" * 64,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "name": "dockerfile-cache",
            "kind": KIND_BUILDKIT_CACHE,
            "chain_id": "sha256:" + "d" * 64,
            "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
        },
        {
            "name": "runtime-layer",
            "kind": "squashfs",
            "media_type": MEDIA_TYPE_BUILDKIT_CACHE,
        },
        {
            "name": "ubuntu",
            "kind": "cloud-image",
            "disk_format": "qcow2",
            "arch": "x86_64",
            "media_type": MEDIA_TYPE_BUILDKIT_CACHE,
        },
        {
            "name": "dockerfile-cache",
            "kind": KIND_BUILDKIT_CACHE,
            "chain_id": "sha256:" + "d" * 64,
            "media_type": "application/octet-stream",
        },
    ],
)
def test_layer_meta_rejects_unsupported_or_kind_inconsistent_media_type(kwargs: dict):
    with pytest.raises(ValueError, match="media_type"):
        HubLayerMeta(**kwargs)


def test_cloud_image_requires_disk_format():
    with pytest.raises(ValueError):
        HubLayerMeta(name="ubuntu", kind="cloud-image", disk_format=None)


def test_cloud_image_requires_arch_but_generic_legacy_layer_keeps_null_arch():
    with pytest.raises(ValueError, match="arch"):
        HubLayerMeta(name="ubuntu", kind="cloud-image", disk_format="qcow2")
    assert HubLayerMeta(name="legacy-layer", kind="squashfs").arch is None


def test_layer_cannot_declare_disk_format():
    with pytest.raises(ValueError):
        HubLayerMeta(name="torch", kind="squashfs", disk_format="qcow2")


def test_layer_dict_exposes_base_image_digest_from_config_json():
    base_image_digest = "sha256:" + "b" * 64
    row = PalimpsestHubLayer(
        blob_digest="sha256:" + "a" * 64,
        blob_md5=None,
        size_bytes=1024,
        media_type=MEDIA_TYPE_LAYER_SQUASHFS,
        config_digest="sha256:" + "c" * 64,
        name="runtime-layer",
        kind="squashfs",
        config_json={"base_image_digest": base_image_digest},
        is_published=False,
    )

    data = hub_api._layer_dict(row)

    assert data["base_image_digest"] == base_image_digest
    assert data["config_json"]["base_image_digest"] == base_image_digest


def test_buildkit_cache_download_filename_uses_tar_extension():
    row = PalimpsestHubLayer(
        blob_digest="sha256:" + "a" * 64,
        blob_md5=None,
        size_bytes=1024,
        media_type=MEDIA_TYPE_BUILDKIT_CACHE,
        config_digest="sha256:" + "c" * 64,
        name="dockerfile-cache",
        kind=KIND_BUILDKIT_CACHE,
        config_json={},
        is_published=False,
    )

    assert hub_api._hub_blob_filename(row) == "dockerfile-cache.tar"


@pytest.mark.asyncio
async def test_finalize_buildkit_cache_stores_dedicated_media_type(
    store: LocalPathBlobStore, monkeypatch: pytest.MonkeyPatch
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_session_factory", lambda: factory)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_blob_store", lambda: store)

    payload = b"portable buildkit local cache tar"
    digest = _sha256(payload)
    token_info = {
        "project_id": "project-1",
        "user_id": "user-1",
        "is_system_admin": False,
    }
    started = await start_upload(HubUploadStartRequest(digest=digest), token_info)
    session_id = started["session_id"]
    assert session_id is not None

    store.upload_path(session_id).write_bytes(payload)
    async with factory() as session:
        upload = await session.get(PalimpsestHubUpload, session_id)
        assert upload is not None
        upload.received_bytes = len(payload)
        await session.commit()

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": f"/v1/uploads/{session_id}",
            "headers": [(b"upload-offset", str(len(payload)).encode())],
        }
    )
    chain_id = "sha256:" + "d" * 64
    await finalize_upload(
        session_id,
        HubLayerMeta(name="dockerfile-cache", kind=KIND_BUILDKIT_CACHE, chain_id=chain_id),
        request,
        token_info,
    )

    async with factory() as session:
        row = (await session.execute(select(PalimpsestHubLayer))).scalar_one()
        assert row.kind == KIND_BUILDKIT_CACHE
        assert row.media_type == MEDIA_TYPE_BUILDKIT_CACHE
        assert row.chain_id == chain_id
        assert row.config_json["kind"] == KIND_BUILDKIT_CACHE
        assert hub_api._hub_blob_filename(row) == "dockerfile-cache.tar"
    await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_rejects_non_admin_publication():
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/v1/uploads/" + "a" * 32,
            "headers": [],
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        await finalize_upload(
            "a" * 32,
            HubLayerMeta(name="torch", kind="squashfs", is_published=True),
            request,
            {"project_id": "project-1", "is_system_admin": False},
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_private_blob_can_be_registered_by_another_project(
    store: LocalPathBlobStore, monkeypatch: pytest.MonkeyPatch
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_session_factory", lambda: factory)
    monkeypatch.setattr("palimpsest_hub.api.hub.get_blob_store", lambda: store)

    payload = b"shared private layer bytes"
    digest = _put_blob(store, payload)
    meta = HubLayerMeta(name="shared-layer", kind="squashfs")
    config = {
        "name": meta.name,
        "kind": meta.kind,
        "ubuntu_base": meta.ubuntu_base,
        "python_version": meta.python_version,
        "parent_digest": meta.parent_digest,
        "chain_id": meta.chain_id,
        "blob_digest": digest,
        "disk_format": meta.disk_format,
        "arch": None,
        "os_variant": meta.os_variant,
        "base_image_digest": meta.base_image_digest,
    }
    async with factory() as session:
        session.add(
            PalimpsestHubLayer(
                blob_digest=digest,
                blob_md5=None,
                size_bytes=len(payload),
                media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                arch=None,
                config_digest=compute_config_digest(config),
                name=meta.name,
                kind=meta.kind,
                config_json=config,
                project_id="project-a",
                is_published=False,
                created_by="user-a",
            )
        )
        await session.commit()

    project_b = {
        "project_id": "project-b",
        "user_id": "user-b",
        "is_system_admin": False,
    }
    started = await start_upload(HubUploadStartRequest(digest=digest), project_b)
    assert started["completed"] is False
    session_id = started["session_id"]
    assert session_id

    store.upload_path(session_id).write_bytes(payload)
    async with factory() as session:
        upload = await session.get(PalimpsestHubUpload, session_id)
        assert upload is not None
        upload.received_bytes = len(payload)
        await session.commit()

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": f"/v1/uploads/{session_id}",
            "headers": [(b"upload-offset", str(len(payload)).encode())],
        }
    )
    finalized = await finalize_upload(session_id, meta, request, project_b)
    assert finalized["already_present"] is True

    async with factory() as session:
        layers = (await session.execute(select(PalimpsestHubLayer))).scalars().all()
        access = await session.get(PalimpsestHubLayerAccess, (digest, "project-b"))
    assert len(layers) == 1
    assert access is not None
    assert access.created_by == "user-b"

    project_c = {
        "project_id": "project-c",
        "user_id": "user-c",
        "is_system_admin": False,
    }
    incompatible = HubLayerMeta(
        name="runtime-pack",
        kind="squashfs",
        chain_id="sha256:" + "c" * 64,
        base_image_digest="sha256:" + "d" * 64,
        arch="x86_64",
    )
    retry = await start_upload(HubUploadStartRequest(digest=digest), project_c)
    retry_id = retry["session_id"]
    assert retry_id
    store.upload_path(retry_id).write_bytes(payload)
    async with factory() as session:
        upload = await session.get(PalimpsestHubUpload, retry_id)
        assert upload is not None
        upload.received_bytes = len(payload)
        await session.commit()
    retry_request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": f"/v1/uploads/{retry_id}",
            "headers": [(b"upload-offset", str(len(payload)).encode())],
        }
    )
    with pytest.raises(HTTPException) as conflict:
        await finalize_upload(retry_id, incompatible, retry_request, project_c)
    assert conflict.value.status_code == 409
    assert "incompatible descriptor fields" in str(conflict.value.detail)
    async with factory() as session:
        assert await session.get(PalimpsestHubLayerAccess, (digest, "project-c")) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_export_download_ticket_supports_range_resume(monkeypatch: pytest.MonkeyPatch):
    export_id = UUID("11111111-1111-1111-1111-111111111111")
    digest = "sha256:" + "b" * 64
    token = "t" * 32
    token_key = f"afterglow:export-dl-token:{token}"

    class FakeRedis:
        def __init__(self):
            self.expirations: list[tuple[str, int]] = []

        async def get(self, key: str):
            assert key == token_key
            return f'{{"export_id":"{export_id}","project_id":"project-1","digest":"{digest}"}}'

        async def expire(self, key: str, ttl: int):
            self.expirations.append((key, ttl))

    redis = FakeRedis()

    async def fake_get_redis():
        return redis

    async def fake_get_project_export(project_id: str, requested_export_id: str):
        assert project_id == "project-1"
        assert requested_export_id == str(export_id)
        return object()

    captured: dict = {}

    def fake_blob_response(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(hub_api, "get_redis", fake_get_redis)
    monkeypatch.setattr(hub_api, "get_project_export", fake_get_project_export)
    monkeypatch.setattr(
        hub_api,
        "_complete_export_blob",
        lambda row, store: (digest, 4096, "export.qcow2", "application/octet-stream"),
    )
    monkeypatch.setattr(hub_api, "_store_or_503", lambda: object())
    monkeypatch.setattr(hub_api, "_blob_response", fake_blob_response)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/v1/image-exports/{export_id}/download",
            "headers": [(b"range", b"bytes=1024-2047")],
        }
    )
    result = await hub_api.download_image_export_with_token(export_id, request, token)

    assert result is not None
    assert redis.expirations == [(token_key, 60)]
    assert captured["range_header"] == "bytes=1024-2047"
