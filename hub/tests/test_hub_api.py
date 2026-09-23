"""Palimpsest Hub API, blob store, OCI bundle, and upload-offset regression tests."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException, Response, UploadFile
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
from palimpsest_hub.auth import get_token_info
from palimpsest_hub.main import app
from palimpsest_hub.models import (
    Base,
    PalimpsestHubLayer,
    PalimpsestHubLayerAccess,
    PalimpsestHubUpload,
)
from palimpsest_hub.services.digest import compute_config_digest
from palimpsest_hub.services.hub_bundle import (
    ANNOTATION_CONFIG_DIGEST,
    BundleError,
    BundleLayer,
    BundleLimitError,
    ParsedBundle,
    build_manifest,
    extract_blob,
    iter_bundle_tar,
    materialize_plain_tar,
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
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalPathBlobStore:
    monkeypatch.setattr(
        hub_api,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_max_blob_bytes=1024 * 1024,
            palimpsest_hub_max_bundle_expanded_bytes=1024 * 1024,
        ),
    )
    return LocalPathBlobStore(tmp_path / "hub")


def _put_blob(store: LocalPathBlobStore, payload: bytes) -> str:
    session_id = "a" * 32
    store.start_upload(session_id)
    with store.upload_path(session_id).open("wb") as handle:
        handle.write(payload)
    finalized = store.finalize_upload(session_id, None)
    store.abort_upload(session_id)
    return finalized.blob_digest


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


def test_blob_promotion_does_not_report_success_without_durable_directory(store: LocalPathBlobStore, monkeypatch):
    session_id = "a" * 32
    payload = b"layer awaiting a durable directory entry"
    store.start_upload(session_id)
    store.upload_path(session_id).write_bytes(payload)

    def failed_sync():
        raise OSError("blob directory fsync failed")

    monkeypatch.setattr(store, "_sync_blob_dir", failed_sync)
    with pytest.raises(OSError, match="fsync failed"):
        store.finalize_upload(session_id, _sha256(payload))
    assert store.upload_path(session_id).read_bytes() == payload


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
async def test_blob_response_returns_suffix_bytes_and_rejects_empty_suffix(store: LocalPathBlobStore):
    payload = bytes(range(100))
    digest = _put_blob(store, payload)
    response = hub_api._blob_response(
        store=store,
        digest=digest,
        total=len(payload),
        media_type="application/octet-stream",
        filename="layer.sqsh",
        range_header="bytes=-25",
    )
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 75-99/100"
    assert b"".join([chunk async for chunk in response.body_iterator]) == payload[-25:]
    with pytest.raises(HTTPException) as denied:
        hub_api._blob_response(
            store=store,
            digest=digest,
            total=len(payload),
            media_type="application/octet-stream",
            filename="layer.sqsh",
            range_header="bytes=-0",
        )
    assert denied.value.status_code == 416


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


def _chain(store: LocalPathBlobStore, *, leaf_media_type: str = MEDIA_TYPE_LAYER_SQUASHFS) -> list[BundleLayer]:
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
            media_type=leaf_media_type,
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

    parsed = parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)
    leaf = chain[-1]
    staged = tmp_path / "staged.sqsh"

    extract_blob(bundle_path, parsed.blob_members[leaf.blob_digest], staged, max_blob_bytes=1024 * 1024)
    assert _sha256(staged.read_bytes()) == leaf.blob_digest


def test_parse_bundle_reconstructs_parent_chain_from_manifest_order(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store)
    bundle_path = tmp_path / "bundle.tar"
    with bundle_path.open("wb") as out:
        for chunk in iter_bundle_tar(store, [chain]):
            out.write(chunk)

    parsed = parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)
    parents = [entry["parent_digest"] for entry in parsed.layers]
    assert parents == [None, chain[0].blob_digest, chain[1].blob_digest]


def test_parse_bundle_attaches_each_layer_annotated_config(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store)
    bundle_path = tmp_path / "bundle.tar"
    bundle_path.write_bytes(b"".join(iter_bundle_tar(store, [chain])))

    parsed = parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)

    assert [entry["config"] for entry in parsed.layers] == [layer.config for layer in chain]


def test_parse_bundle_rejects_annotated_config_parent_that_contradicts_manifest_order(
    store: LocalPathBlobStore, tmp_path: Path
):
    chain = _chain(store)
    child = chain[1]
    chain[1] = BundleLayer(
        blob_digest=child.blob_digest,
        size_bytes=child.size_bytes,
        name=child.name,
        config={**child.config, "parent_digest": chain[-1].blob_digest},
    )
    bundle_path = tmp_path / "contradictory.tar"
    bundle_path.write_bytes(b"".join(iter_bundle_tar(store, [chain])))

    with pytest.raises(BundleError, match="parent_digest.*manifest"):
        parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)


def test_parse_bundle_rejects_leaf_config_that_disagrees_with_annotation(tmp_path: Path):
    payload = b"layer bytes"
    annotated = b'{"name":"annotated"}'
    leaf = b'{"name":"manifest"}'
    manifest = {
        "layers": [
            {
                "digest": _sha256(payload),
                "size": len(payload),
                "annotations": {ANNOTATION_CONFIG_DIGEST: _sha256(annotated)},
            }
        ],
        "config": {"digest": _sha256(leaf), "size": len(leaf)},
    }
    manifest_bytes = json.dumps(manifest).encode()
    index_bytes = json.dumps({"manifests": [{"digest": _sha256(manifest_bytes), "size": len(manifest_bytes)}]}).encode()
    bundle_path = tmp_path / "contradictory-leaf.tar"
    with tarfile.open(bundle_path, mode="w") as archive:
        for name, data in (
            ("index.json", index_bytes),
            (f"blobs/sha256/{_sha256(manifest_bytes)[7:]}", manifest_bytes),
            (f"blobs/sha256/{_sha256(annotated)[7:]}", annotated),
            (f"blobs/sha256/{_sha256(leaf)[7:]}", leaf),
            (f"blobs/sha256/{_sha256(payload)[7:]}", payload),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    with pytest.raises(BundleError, match="leaf config.*모순"):
        parse_bundle(bundle_path, max_blob_bytes=1024, max_expanded_bytes=32 * 1024)


def test_parse_bundle_deduplicates_consistent_shared_parent(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store)
    bundle_path = tmp_path / "shared-parent.tar"
    bundle_path.write_bytes(b"".join(iter_bundle_tar(store, [chain, chain[:2]])))

    parsed = parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)

    assert [entry["blob_digest"] for entry in parsed.layers] == [layer.blob_digest for layer in chain]


@pytest.mark.parametrize("conflict", ["media_type", "config"])
def test_parse_bundle_rejects_conflicting_descriptors_for_shared_blob(
    store: LocalPathBlobStore, tmp_path: Path, conflict: str
):
    chain = _chain(store)
    root = chain[0]
    duplicate = BundleLayer(
        blob_digest=root.blob_digest,
        size_bytes=root.size_bytes,
        name=root.name,
        config={"name": "different"} if conflict == "config" else root.config,
        media_type=MEDIA_TYPE_IMAGE_QCOW2 if conflict == "media_type" else root.media_type,
    )
    bundle_path = tmp_path / "contradictory-shared-blob.tar"
    bundle_path.write_bytes(b"".join(iter_bundle_tar(store, [chain, [duplicate]])))

    with pytest.raises(BundleError, match="모순"):
        parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)


@pytest.mark.asyncio
async def test_bundle_digest_mismatch_rejects_before_cas_publication(
    store: LocalPathBlobStore, monkeypatch: pytest.MonkeyPatch
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(hub_api, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub_api, "get_blob_store", lambda: store)
    payload = b"existing downloadable layer"
    monkeypatch.setattr(
        hub_api,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_max_blob_bytes=1024 * 1024, palimpsest_hub_max_bundle_expanded_bytes=1024 * 1024
        ),
    )
    existing_digest = _put_blob(store, payload)
    declared = "sha256:" + "b" * 64
    member = f"blobs/sha256/{declared[7:]}"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    monkeypatch.setattr(
        hub_api,
        "parse_bundle",
        lambda _, **__: ParsedBundle(
            layers=[{"blob_digest": declared, "size_bytes": len(payload)}], blob_members={declared: member}
        ),
    )
    try:
        with pytest.raises(HTTPException) as rejected:
            await hub_api.import_bundle(UploadFile(file=io.BytesIO(raw.getvalue())), {"project_id": "alpha"})
        assert rejected.value.status_code == 422
        assert b"".join(store.iter_blob(existing_digest)) == payload
        assert not store.exists(declared)
    finally:
        await engine.dispose()


def _write_single_layer_bundle(
    bundle_path: Path,
    payload: bytes,
    *,
    descriptor_size: int | None = None,
    layer_media_type: str | None = None,
) -> str:
    digest = _sha256(payload)
    config = b'{"name":"layer"}'
    config_digest = _sha256(config)
    layer = {"digest": digest, "size": len(payload) if descriptor_size is None else descriptor_size}
    if layer_media_type is not None:
        layer["mediaType"] = layer_media_type
    manifest = {
        "schemaVersion": 2,
        "config": {"digest": config_digest, "size": len(config)},
        "layers": [layer],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_digest = _sha256(manifest_bytes)
    index_bytes = json.dumps(
        {"manifests": [{"digest": manifest_digest, "size": len(manifest_bytes)}]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with tarfile.open(bundle_path, mode="w:gz") as archive:
        for name, value in (
            ("index.json", index_bytes),
            (f"blobs/sha256/{manifest_digest[7:]}", manifest_bytes),
            (f"blobs/sha256/{config_digest[7:]}", config),
            (f"blobs/sha256/{digest[7:]}", payload),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    return digest


def test_bundle_streaming_parser_rejects_4097th_member_without_getmembers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    compressed = tmp_path / "too-many.tar.gz"
    with tarfile.open(compressed, mode="w:gz") as archive:
        for index in range(4097):
            info = tarfile.TarInfo(f"entries/{index}")
            info.size = 0
            archive.addfile(info)
    bundle_path = materialize_plain_tar(compressed, tmp_path / "too-many.tar", max_expanded_bytes=4 * 1024 * 1024)
    monkeypatch.setattr(tarfile.TarFile, "getmembers", lambda _: (_ for _ in ()).throw(AssertionError("unbounded")))
    with pytest.raises(BundleLimitError, match="4096"):
        parse_bundle(bundle_path, max_blob_bytes=1024, max_expanded_bytes=4 * 1024 * 1024)


def test_compressed_bundle_blob_limit_and_descriptor_mismatch_leave_no_staging(tmp_path: Path):
    oversized = tmp_path / "oversized.tar.gz"
    _write_single_layer_bundle(oversized, b"\0" * 1024)
    oversized_plain = materialize_plain_tar(oversized, tmp_path / "oversized.tar", max_expanded_bytes=32 * 1024)
    with pytest.raises(BundleLimitError):
        parse_bundle(oversized_plain, max_blob_bytes=128, max_expanded_bytes=32 * 1024)

    mismatched = tmp_path / "mismatched.tar.gz"
    _write_single_layer_bundle(mismatched, b"payload", descriptor_size=8)
    mismatched_plain = materialize_plain_tar(mismatched, tmp_path / "mismatched.tar", max_expanded_bytes=32 * 1024)
    with pytest.raises(BundleError, match="선언 크기"):
        parse_bundle(mismatched_plain, max_blob_bytes=1024, max_expanded_bytes=32 * 1024)

    truncated = tmp_path / "truncated.tar"
    info = tarfile.TarInfo("blob")
    info.size = 16
    truncated.write_bytes(info.tobuf() + b"short")
    destination = tmp_path / "partial"
    with pytest.raises(BundleError):
        extract_blob(truncated, "blob", destination, expected_size=16, max_blob_bytes=1024)
    assert not destination.exists()


@pytest.mark.parametrize("mode", ["w:gz", "w:bz2", "w:xz"])
def test_materialize_supports_each_compressed_tar_format(tmp_path: Path, mode: str):
    compressed = tmp_path / f"bundle-{mode[-2:]}.tar"
    with tarfile.open(compressed, mode=mode) as archive:
        info = tarfile.TarInfo("payload")
        info.size = len(b"payload")
        archive.addfile(info, io.BytesIO(b"payload"))

    plain = tmp_path / "bundle.tar"
    assert materialize_plain_tar(compressed, plain, max_expanded_bytes=32 * 1024) == plain
    with tarfile.open(plain, mode="r:") as archive:
        assert archive.getnames() == ["payload"]


def test_materialize_rejects_gzip_expansion_and_removes_partial_spool(tmp_path: Path):
    compressed = tmp_path / "expands.tar.gz"
    with tarfile.open(compressed, mode="w:gz") as archive:
        info = tarfile.TarInfo("payload")
        info.size = 64 * 1024
        archive.addfile(info, io.BytesIO(b"x" * info.size))

    spool = tmp_path / "expands.tar"
    with pytest.raises(BundleLimitError):
        materialize_plain_tar(compressed, spool, max_expanded_bytes=1024)
    assert not spool.exists()


def test_physical_scan_bounds_oversized_pax_payload_before_tarfile_reads_it(tmp_path: Path):
    bundle_path = tmp_path / "oversized-pax.tar"
    payload = b"x" * (4 * 1024 * 1024 + 1)
    with tarfile.open(bundle_path, mode="w") as archive:
        info = tarfile.TarInfo("pax-header")
        info.type = tarfile.XHDTYPE
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(BundleLimitError, match="확장 멤버"):
        parse_bundle(bundle_path, max_blob_bytes=8 * 1024 * 1024, max_expanded_bytes=8 * 1024 * 1024)


def test_parse_bundle_requires_a_materialized_multilayer_tar(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store)
    compressed = tmp_path / "bundle.tar.gz"
    with tarfile.open(compressed, mode="w:gz") as archive:
        for chunk in iter_bundle_tar(store, [chain]):
            archive.fileobj.write(chunk)

    with pytest.raises(BundleError):
        parse_bundle(compressed, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)
    bundle_path = materialize_plain_tar(compressed, tmp_path / "bundle.tar", max_expanded_bytes=1024 * 1024)
    parsed = parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)
    assert [layer["blob_digest"] for layer in parsed.layers] == [layer.blob_digest for layer in chain]


def test_nonregular_tar_payloads_consume_expansion_budget_and_leave_no_outputs(tmp_path: Path):
    compressed = tmp_path / "nonregular.tar.gz"
    with tarfile.open(compressed, mode="w:gz") as archive:
        for index in range(3):
            info = tarfile.TarInfo(f"directories/{index}")
            info.type = tarfile.DIRTYPE
            info.size = 512
            archive.addfile(info, io.BytesIO(b"x" * info.size))

    plain = materialize_plain_tar(compressed, tmp_path / "nonregular.tar", max_expanded_bytes=32 * 1024)
    staged = tmp_path / "staged"
    with pytest.raises(BundleLimitError):
        parse_bundle(plain, max_blob_bytes=1024, max_expanded_bytes=1024)
    assert not staged.exists()

    rejected_spool = tmp_path / "rejected.tar"
    with pytest.raises(BundleLimitError):
        materialize_plain_tar(compressed, rejected_spool, max_expanded_bytes=1024)
    assert not rejected_spool.exists()


def test_bundle_round_trip_preserves_disk_layer_media_type(store: LocalPathBlobStore, tmp_path: Path):
    chain = _chain(store, leaf_media_type=MEDIA_TYPE_IMAGE_QCOW2)
    bundle_path = tmp_path / "disk-layer.tar"
    bundle_path.write_bytes(b"".join(iter_bundle_tar(store, [chain])))

    parsed = parse_bundle(bundle_path, max_blob_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024)
    assert parsed.layers[-1]["media_type"] == MEDIA_TYPE_IMAGE_QCOW2


def test_parse_bundle_defaults_and_validates_layer_media_type(tmp_path: Path):
    default_compressed = tmp_path / "default-media.tar.gz"
    _write_single_layer_bundle(default_compressed, b"payload")
    default_plain = materialize_plain_tar(
        default_compressed, tmp_path / "default-media.tar", max_expanded_bytes=32 * 1024
    )
    parsed = parse_bundle(default_plain, max_blob_bytes=1024, max_expanded_bytes=32 * 1024)
    assert parsed.layers[0]["media_type"] == MEDIA_TYPE_LAYER_SQUASHFS

    invalid_compressed = tmp_path / "invalid-media.tar.gz"
    _write_single_layer_bundle(invalid_compressed, b"payload", layer_media_type="application/octet-stream")
    invalid_plain = materialize_plain_tar(
        invalid_compressed, tmp_path / "invalid-media.tar", max_expanded_bytes=32 * 1024
    )
    with pytest.raises(BundleError, match="mediaType"):
        parse_bundle(invalid_plain, max_blob_bytes=1024, max_expanded_bytes=32 * 1024)


def test_build_manifest_rejects_empty_chain():
    with pytest.raises(BundleError):
        build_manifest([], {})


async def _import_bundle_over_http(payload: bytes):
    async def identity(request: Request):
        return {"project_id": "alpha", "user_id": "member"}

    app.dependency_overrides[get_token_info] = identity
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/v1/bundles/import", files={"file": ("bundle.tar.gz", payload)})
    finally:
        app.dependency_overrides.clear()


async def _prepared_hub(tmp_path: Path, name: str, store: LocalPathBlobStore, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(hub_api, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub_api, "get_blob_store", lambda: store)
    return engine, factory


def _gzip_bundle(store: LocalPathBlobStore, chain: list[BundleLayer]) -> bytes:
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
        for chunk in iter_bundle_tar(store, [chain]):
            compressed.write(chunk)
    return raw.getvalue()


@pytest.mark.asyncio
async def test_import_rejects_conflicting_shared_descriptor_without_publishing(
    store: LocalPathBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    destination = LocalPathBlobStore(tmp_path / "destination")
    chain = _chain(store)
    root = chain[0]
    duplicate = BundleLayer(root.blob_digest, root.size_bytes, root.name, {"name": "different"})
    payload = b"".join(iter_bundle_tar(store, [chain, [duplicate]]))
    engine, factory = await _prepared_hub(tmp_path, "conflict.sqlite", destination, monkeypatch)
    try:
        response = await _import_bundle_over_http(payload)
        assert response.status_code == 422, response.text
        async with factory() as session:
            assert (await session.execute(select(PalimpsestHubLayer))).scalars().all() == []
        assert not destination.exists(root.blob_digest)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_registers_the_whole_chain_with_its_declared_parents(
    store: LocalPathBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    chain = _chain(store)
    engine, factory = await _prepared_hub(tmp_path, "import.sqlite", store, monkeypatch)
    try:
        response = await _import_bundle_over_http(_gzip_bundle(store, chain))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["skipped"] == []
        assert body["imported"] == [layer.blob_digest for layer in chain]
        async with factory() as session:
            rows = {row.blob_digest: row for row in (await session.execute(select(PalimpsestHubLayer))).scalars().all()}
        assert [rows[layer.blob_digest].media_type for layer in chain] == [MEDIA_TYPE_LAYER_SQUASHFS] * 3
        assert rows[chain[-1].blob_digest].parent_digest == chain[-2].blob_digest
        assert rows[chain[0].blob_digest].parent_digest is None
        assert all(store.exists(digest) for digest in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_preserves_each_config_in_a_cloud_image_parent_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_store = LocalPathBlobStore(tmp_path / "source")
    destination_store = LocalPathBlobStore(tmp_path / "destination")
    base_payload = b"qcow2 base image bytes"
    runtime_payload = b"runtime squashfs bytes"
    app_payload = b"application squashfs bytes"
    base_digest = _put_blob(source_store, base_payload)
    runtime_digest = _put_blob(source_store, runtime_payload)
    app_digest = _put_blob(source_store, app_payload)
    chain_id = _sha256(b"runtime chain")
    chain = [
        BundleLayer(
            blob_digest=base_digest,
            size_bytes=len(base_payload),
            name="jammy",
            config={
                "name": "jammy",
                "kind": "cloud-image",
                "disk_format": "qcow2",
                "arch": "x86_64",
                "blob_digest": base_digest,
            },
            media_type=MEDIA_TYPE_IMAGE_QCOW2,
        ),
        BundleLayer(
            blob_digest=runtime_digest,
            size_bytes=len(runtime_payload),
            name="runtime",
            config={
                "name": "runtime",
                "blob_digest": runtime_digest,
                "parent_digest": base_digest,
                "chain_id": chain_id,
                "ubuntu_base": "24.04",
            },
        ),
        BundleLayer(
            blob_digest=app_digest,
            size_bytes=len(app_payload),
            name="application",
            config={
                "name": "application",
                "blob_digest": app_digest,
                "parent_digest": runtime_digest,
                "chain_id": chain_id,
                "python_version": "3.12",
            },
        ),
    ]
    monkeypatch.setattr(
        hub_api,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_max_blob_bytes=1024 * 1024,
            palimpsest_hub_max_bundle_expanded_bytes=1024 * 1024,
        ),
    )
    engine, factory = await _prepared_hub(tmp_path, "full-chain.sqlite", destination_store, monkeypatch)
    try:
        response = await _import_bundle_over_http(_gzip_bundle(source_store, chain))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["skipped"] == []
        assert body["imported"] == [layer.blob_digest for layer in chain]
        async with factory() as session:
            rows = {row.blob_digest: row for row in (await session.execute(select(PalimpsestHubLayer))).scalars().all()}
        assert set(rows) == {layer.blob_digest for layer in chain}
        assert all(destination_store.exists(layer.blob_digest) for layer in chain)
        assert (
            rows[base_digest].kind,
            rows[base_digest].disk_format,
            rows[base_digest].arch,
            rows[base_digest].media_type,
            rows[base_digest].parent_digest,
        ) == ("cloud-image", "qcow2", "x86_64", MEDIA_TYPE_IMAGE_QCOW2, None)
        assert (
            rows[runtime_digest].chain_id,
            rows[runtime_digest].ubuntu_base,
            rows[runtime_digest].python_version,
            rows[runtime_digest].parent_digest,
        ) == (chain_id, "24.04", None, base_digest)
        assert (
            rows[app_digest].chain_id,
            rows[app_digest].ubuntu_base,
            rows[app_digest].python_version,
            rows[app_digest].parent_digest,
        ) == (chain_id, None, "3.12", runtime_digest)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_preserves_cloud_image_descriptor_fields(
    store: LocalPathBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = b"qcow2 base image bytes"
    digest = _put_blob(store, payload)
    base = BundleLayer(
        blob_digest=digest,
        size_bytes=len(payload),
        name="jammy",
        config={"name": "jammy", "kind": "cloud-image", "disk_format": "qcow2", "arch": "x86_64"},
        media_type=MEDIA_TYPE_IMAGE_QCOW2,
    )
    engine, factory = await _prepared_hub(tmp_path, "cloud.sqlite", store, monkeypatch)
    try:
        response = await _import_bundle_over_http(_gzip_bundle(store, [base]))
        assert response.status_code == 200, response.text
        assert response.json()["imported"] == [digest]
        async with factory() as session:
            row = (await session.execute(select(PalimpsestHubLayer))).scalar_one()
        assert (row.media_type, row.kind, row.disk_format, row.arch) == (
            MEDIA_TYPE_IMAGE_QCOW2,
            "cloud-image",
            "qcow2",
            "x86_64",
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_skips_a_descriptor_whose_media_type_contradicts_its_config(
    store: LocalPathBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    payload = b"mislabelled layer bytes"
    digest = _put_blob(store, payload)
    mislabelled = BundleLayer(
        blob_digest=digest,
        size_bytes=len(payload),
        name="mislabelled",
        config={"name": "mislabelled", "kind": "squashfs"},
        media_type=MEDIA_TYPE_IMAGE_QCOW2,
    )
    engine, factory = await _prepared_hub(tmp_path, "mismatch.sqlite", store, monkeypatch)
    try:
        response = await _import_bundle_over_http(_gzip_bundle(store, [mislabelled]))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["imported"] == []
        assert [item["digest"] for item in body["skipped"]] == [digest]
        async with factory() as session:
            assert (await session.execute(select(PalimpsestHubLayer))).scalars().all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_over_expansion_limit_returns_413_without_registering_anything(
    store: LocalPathBlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    chain = _chain(store)
    payload = _gzip_bundle(store, chain)
    engine, factory = await _prepared_hub(tmp_path, "rejected.sqlite", store, monkeypatch)
    monkeypatch.setattr(
        hub_api,
        "get_settings",
        lambda: SimpleNamespace(
            palimpsest_hub_max_blob_bytes=1024 * 1024,
            palimpsest_hub_max_bundle_expanded_bytes=64,
        ),
    )
    try:
        response = await _import_bundle_over_http(payload)
        assert response.status_code == 413, response.text
        async with factory() as session:
            assert (await session.execute(select(PalimpsestHubLayer))).scalars().all() == []
    finally:
        await engine.dispose()


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


def test_layer_dict_exposes_public_base_descriptor_but_redacts_foreign_ownership():
    base_image_digest = "sha256:" + "b" * 64
    row = PalimpsestHubLayer(
        blob_digest="sha256:" + "a" * 64,
        blob_md5=None,
        size_bytes=1024,
        media_type=MEDIA_TYPE_LAYER_SQUASHFS,
        config_digest="sha256:" + "c" * 64,
        name="runtime-layer",
        kind="squashfs",
        config_json={"base_image_digest": base_image_digest, "private_legacy_field": "do-not-return"},
        project_id="owner-project",
        created_by="owner-user",
        is_published=True,
    )

    data = hub_api._layer_dict(row)

    assert data["base_image_digest"] == base_image_digest
    assert data["config_json"]["base_image_digest"] == base_image_digest
    foreign = hub_api._layer_dict(row, {"project_id": "another-project"})
    assert foreign["project_id"] is None and foreign["created_by"] is None
    assert foreign["base_image_digest"] == base_image_digest
    assert "private_legacy_field" not in foreign["config_json"]
    owner = hub_api._layer_dict(row, {"project_id": "owner-project"})
    assert owner["project_id"] == "owner-project" and owner["created_by"] == "owner-user"


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
async def test_upload_remains_retryable_after_blob_promotion_precedes_registration(
    store: LocalPathBlobStore, monkeypatch: pytest.MonkeyPatch
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(hub_api, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub_api, "get_blob_store", lambda: store)
    identity = {"project_id": "alpha", "user_id": "member"}
    payload = b"retryable layer bytes"
    digest = _sha256(payload)
    session_id = (await start_upload(HubUploadStartRequest(digest=digest), identity))["session_id"]
    store.upload_path(session_id).write_bytes(payload)
    async with factory() as session:
        upload = await session.get(PalimpsestHubUpload, session_id)
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
    descriptor = HubLayerMeta(name="retryable", kind="squashfs")
    compute = hub_api.compute_config_digest

    def registration_fails(config):
        raise RuntimeError("database registration interrupted")

    monkeypatch.setattr(hub_api, "compute_config_digest", registration_fails)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            await finalize_upload(session_id, descriptor, request, identity)
        assert store.upload_path(session_id).read_bytes() == payload
        monkeypatch.setattr(hub_api, "compute_config_digest", compute)
        registered = await finalize_upload(session_id, descriptor, request, identity)
        assert registered["blob_digest"] == digest
        assert b"".join(store.iter_blob(digest)) == payload
        assert not store.upload_path(session_id).exists()
        async with factory() as session:
            assert await session.get(PalimpsestHubUpload, session_id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_uploads_register_one_blob_and_both_projects(
    store: LocalPathBlobStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(hub_api, "get_session_factory", lambda: factory)
    monkeypatch.setattr(hub_api, "get_blob_store", lambda: store)
    payload = b"same bytes for two projects"
    digest = _sha256(payload)
    requests = []
    for project in ("alpha", "beta"):
        identity = {"project_id": project, "user_id": "member"}
        session_id = (await start_upload(HubUploadStartRequest(digest=digest), identity))["session_id"]
        store.upload_path(session_id).write_bytes(payload)
        async with factory() as session:
            upload = await session.get(PalimpsestHubUpload, session_id)
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
        requests.append((session_id, request, identity))
    try:
        results = await asyncio.gather(
            *(
                finalize_upload(session_id, HubLayerMeta(name="shared", kind="squashfs"), request, identity)
                for session_id, request, identity in requests
            )
        )
        assert [result["blob_digest"] for result in results] == [digest, digest]
        async with factory() as session:
            assert len((await session.execute(select(PalimpsestHubLayer))).scalars().all()) == 1
            for project in ("alpha", "beta"):
                assert await hub_api._load_visible(session, digest, {"project_id": project}) is not None
        assert b"".join(store.iter_blob(digest)) == payload
    finally:
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
async def test_unscoped_upload_session_is_not_claimable_by_project():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(PalimpsestHubUpload(id="b" * 32, received_bytes=0, project_id=None))
            await session.commit()
            with pytest.raises(HTTPException) as denied:
                await hub_api._owned_upload(session, "b" * 32, {"project_id": "project-b"})
            assert denied.value.status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_export_download_ticket_supports_range_resume(monkeypatch: pytest.MonkeyPatch):
    export_id = UUID("11111111-1111-1111-1111-111111111111")
    digest = "sha256:" + "b" * 64
    token = "t" * 32
    token_key = f"afterglow:export-dl-token:{token}"

    class FakeRedis:
        async def get(self, key: str):
            assert key == token_key
            return json.dumps(
                {
                    "export_id": str(export_id),
                    "project_id": "project-1",
                    "digest": digest,
                    "expires_at": int(time.time()) + 60,
                }
            )

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
    assert captured["range_header"] == "bytes=1024-2047"
    assert captured["cache_control"] == "no-store"


@pytest.mark.asyncio
async def test_export_ticket_deadline_is_fixed_and_wrong_export_does_not_refresh(monkeypatch: pytest.MonkeyPatch):
    export_id = UUID("11111111-1111-1111-1111-111111111111")
    wrong_export_id = UUID("22222222-2222-2222-2222-222222222222")
    digest = "sha256:" + "c" * 64

    class FakeRedis:
        async def get(self, key: str):
            return json.dumps(
                {
                    "export_id": str(export_id),
                    "project_id": "project-1",
                    "digest": digest,
                    "expires_at": int(time.time()) + 60,
                }
            )

    async def fake_redis():
        return FakeRedis()

    monkeypatch.setattr(hub_api, "get_redis", fake_redis)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    with pytest.raises(HTTPException) as rejected:
        await hub_api.download_image_export_with_token(wrong_export_id, request, "t" * 32)
    assert rejected.value.status_code == 404

    class ExpiredRedis:
        async def get(self, key: str):
            return json.dumps(
                {"export_id": str(export_id), "project_id": "project-1", "digest": digest, "expires_at": 0}
            )

    async def expired_redis():
        return ExpiredRedis()

    monkeypatch.setattr(hub_api, "get_redis", expired_redis)
    with pytest.raises(HTTPException) as expired:
        await hub_api.download_image_export_with_token(export_id, request, "t" * 32)
    assert expired.value.status_code == 404


@pytest.mark.asyncio
async def test_export_ticket_records_original_absolute_deadline(monkeypatch: pytest.MonkeyPatch):
    export_id = UUID("33333333-3333-3333-3333-333333333333")
    digest = "sha256:" + "d" * 64
    captured: dict[str, object] = {}

    class FakeRedis:
        async def setex(self, key: str, ttl: int, payload: str):
            captured.update(key=key, ttl=ttl, payload=json.loads(payload))

    class Export:
        id = str(export_id)

    async def fake_redis():
        return FakeRedis()

    async def fake_export(project_id: str, requested_export_id: str):
        assert (project_id, requested_export_id) == ("project-1", str(export_id))
        return Export()

    monkeypatch.setattr(hub_api, "get_redis", fake_redis)
    monkeypatch.setattr(hub_api, "get_project_export", fake_export)
    monkeypatch.setattr(hub_api, "_store_or_503", lambda: object())
    monkeypatch.setattr(
        hub_api, "_complete_export_blob", lambda row, store: (digest, 1, "x", "application/octet-stream")
    )
    before = int(time.time())
    issued = Response()
    result = await hub_api.create_image_export_download_token(export_id, issued, {"project_id": "project-1"})
    assert result["expires_in"] == 60
    assert issued.headers["cache-control"] == "no-store"
    assert captured["ttl"] == 60
    assert before + 60 <= captured["payload"]["expires_at"] <= int(time.time()) + 60
