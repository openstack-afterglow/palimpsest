"""Unit tests for palimpsest_local.oci_layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from palimpsest_local.digest import digest_file
from palimpsest_local.errors import ArtifactValidationError, DigestMismatchError
from palimpsest_local.oci_layout import (
    ANNOTATION_CHAIN_ID,
    ANNOTATION_NAME,
    MEDIA_TYPE_IMAGE_QCOW2,
    MEDIA_TYPE_INDEX,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
    MEDIA_TYPE_MANIFEST,
    OCI_LAYOUT_VERSION,
    ContentStore,
    build_bundle_tar_bytes,
    canonical_json,
    config_digest,
    extract_bundle_tar,
    verify_layout_dir,
)


def create_tar_bundle(members: dict[str, bytes]) -> bytes:
    return build_bundle_tar_bytes(members)


def test_deterministic_tar_bytes():
    members = {
        "index.json": b'{"hello":"world"}',
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
    }
    tar1 = build_bundle_tar_bytes(members)
    tar2 = build_bundle_tar_bytes(members)
    assert tar1 == tar2
    assert len(tar1) > 0


def test_canonical_json():
    data = {"b": 2, "a": "한글"}
    encoded = canonical_json(data)
    assert encoded == '{"a":"한글","b":2}'.encode()


def test_content_store_operations(tmp_path: Path):
    store = ContentStore(tmp_path)
    payload = b"test blob content"
    expected_digest = f"sha256:{config_digest(payload)[len('sha256:') :]}"

    # Write stream
    path = store.write_stream([payload], expected_digest=expected_digest)
    assert path.is_file()
    assert store.exists(expected_digest)
    assert store.size(expected_digest) == len(payload)
    assert (path.stat().st_mode & 0o777) == 0o444

    # Digest mismatch
    with pytest.raises(DigestMismatchError):
        store.write_stream([payload], expected_digest="sha256:" + "0" * 64)

    # Ingest file
    sample_file = tmp_path / "sample.bin"
    sample_file.write_bytes(b"another blob")
    sample_digest = digest_file(sample_file)
    store.ingest_file(sample_file)
    assert store.exists(sample_digest)

    # Delete
    store.delete(sample_digest, retention_guard=lambda: None)
    assert not store.exists(sample_digest)


def test_content_store_delete_requires_retention_guard(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    digest = store.write_stream([b"guarded"]).name

    with pytest.raises(ArtifactValidationError, match="durable-reference guard"):
        store.delete(f"sha256:{digest}")


def test_content_store_reuses_valid_digest_inode(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    payload = b"stable inode"
    digest = config_digest(payload)
    first = store.write_stream([payload], expected_digest=digest)
    identity = (first.stat().st_dev, first.stat().st_ino)

    second = store.write_stream([payload], expected_digest=digest)

    assert (second.stat().st_dev, second.stat().st_ino) == identity


def test_content_store_replaces_poisoned_existing_digest_target(tmp_path: Path):
    store = ContentStore(tmp_path)
    payload = b"verified bytes"
    digest = config_digest(payload)
    target = store.blob_path(digest)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"poisoned bytes")
    target.chmod(0o600)

    result = store.write_stream([payload], expected_digest=digest)

    assert result == target
    assert result.read_bytes() == payload
    assert digest_file(result) == digest
    assert result.stat().st_mode & 0o777 == 0o444


def test_extract_and_verify_bundle(tmp_path: Path):
    layer1_bytes = b"layer1-squashfs-bytes"
    layer1_digest = f"sha256:{config_digest(layer1_bytes)[len('sha256:') :]}"

    layer2_bytes = b"layer2-squashfs-bytes"
    layer2_digest = f"sha256:{config_digest(layer2_bytes)[len('sha256:') :]}"

    cfg_bytes = canonical_json({"chain_id": "test-chain"})
    cfg_digest = f"sha256:{config_digest(cfg_bytes)[len('sha256:') :]}"

    manifest_dict = {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_MANIFEST,
        "config": {"mediaType": MEDIA_TYPE_LAYER_CONFIG, "digest": cfg_digest, "size": len(cfg_bytes)},
        "layers": [
            {
                "mediaType": MEDIA_TYPE_IMAGE_QCOW2,
                "digest": layer1_digest,
                "size": len(layer1_bytes),
                "annotations": {ANNOTATION_NAME: "base-image"},
            },
            {
                "mediaType": MEDIA_TYPE_LAYER_SQUASHFS,
                "digest": layer2_digest,
                "size": len(layer2_bytes),
                "annotations": {ANNOTATION_NAME: "app-layer"},
            },
        ],
        "annotations": {ANNOTATION_NAME: "app-layer", ANNOTATION_CHAIN_ID: "test-chain"},
    }
    manifest_bytes = canonical_json(manifest_dict)
    manifest_digest = f"sha256:{config_digest(manifest_bytes)[len('sha256:') :]}"

    index_dict = {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_INDEX,
        "manifests": [
            {
                "mediaType": MEDIA_TYPE_MANIFEST,
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "annotations": {ANNOTATION_NAME: "app-layer"},
            }
        ],
    }
    index_bytes = canonical_json(index_dict)
    layout_bytes = canonical_json({"imageLayoutVersion": OCI_LAYOUT_VERSION})

    bundle_members = {
        "oci-layout": layout_bytes,
        "index.json": index_bytes,
        f"blobs/sha256/{manifest_digest[len('sha256:') :]}": manifest_bytes,
        f"blobs/sha256/{cfg_digest[len('sha256:') :]}": cfg_bytes,
        f"blobs/sha256/{layer1_digest[len('sha256:') :]}": layer1_bytes,
        f"blobs/sha256/{layer2_digest[len('sha256:') :]}": layer2_bytes,
    }

    tar_bytes = create_tar_bundle(bundle_members)
    tar_path = tmp_path / "bundle.tar"
    tar_path.write_bytes(tar_bytes)

    dest_dir = tmp_path / "extracted_layout"
    verified_layout = extract_bundle_tar(tar_path, dest_dir)

    assert len(verified_layout.manifests) == 1
    vm = verified_layout.manifests[0]
    assert vm.manifest_digest == manifest_digest
    assert vm.config_digest == cfg_digest
    assert len(vm.entries) == 2
    assert vm.entries[0].digest == layer1_digest
    assert vm.entries[0].media_type == MEDIA_TYPE_IMAGE_QCOW2
    assert vm.entries[0].parent_digest is None
    assert vm.entries[1].digest == layer2_digest
    assert vm.entries[1].media_type == MEDIA_TYPE_LAYER_SQUASHFS
    assert vm.entries[1].parent_digest == layer1_digest

    # Now verify directory in place
    verified_dir = verify_layout_dir(dest_dir)
    assert verified_dir == verified_layout


def test_extract_bundle_security_rejections(tmp_path: Path):
    # 1. Traversal member name
    members_traversal = {
        "oci-layout": canonical_json({"imageLayoutVersion": OCI_LAYOUT_VERSION}),
        "index.json": b"{}",
        "../etc/passwd": b"hacked",
    }
    tar1 = tmp_path / "traversal.tar"
    tar1.write_bytes(create_tar_bundle(members_traversal))
    dest1 = tmp_path / "dest1"

    with pytest.raises(ArtifactValidationError, match="unsafe path traversal"):
        extract_bundle_tar(tar1, dest1)
    assert not dest1.exists()

    # 2. Blob digest mismatch
    fake_layer_bytes = b"wrong content"
    real_layer_bytes = b"correct content"
    real_digest = f"sha256:{config_digest(real_layer_bytes)[len('sha256:') :]}"

    cfg_bytes = canonical_json({})
    cfg_digest = f"sha256:{config_digest(cfg_bytes)[len('sha256:') :]}"

    manifest_bytes = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_MANIFEST,
            "config": {"mediaType": MEDIA_TYPE_LAYER_CONFIG, "digest": cfg_digest, "size": len(cfg_bytes)},
            "layers": [
                {
                    "mediaType": MEDIA_TYPE_LAYER_SQUASHFS,
                    "digest": real_digest,
                    "size": len(fake_layer_bytes),
                }
            ],
        }
    )
    manifest_digest = f"sha256:{config_digest(manifest_bytes)[len('sha256:') :]}"

    index_bytes = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_INDEX,
            "manifests": [{"mediaType": MEDIA_TYPE_MANIFEST, "digest": manifest_digest, "size": len(manifest_bytes)}],
        }
    )

    members_mismatch = {
        "oci-layout": canonical_json({"imageLayoutVersion": OCI_LAYOUT_VERSION}),
        "index.json": index_bytes,
        f"blobs/sha256/{manifest_digest[len('sha256:') :]}": manifest_bytes,
        f"blobs/sha256/{cfg_digest[len('sha256:') :]}": cfg_bytes,
        f"blobs/sha256/{real_digest[len('sha256:') :]}": fake_layer_bytes,  # byte digest mismatch!
    }

    tar2 = tmp_path / "mismatch.tar"
    tar2.write_bytes(create_tar_bundle(members_mismatch))
    dest2 = tmp_path / "dest2"

    with pytest.raises(DigestMismatchError):
        extract_bundle_tar(tar2, dest2)
    # Ensure atomic cleanup leaves no files at dest2!
    assert not dest2.exists()


@pytest.mark.parametrize("wrong_field", ["manifest", "config", "layer"])
def test_extract_rejects_descriptor_size_mismatch_without_promoting(tmp_path: Path, wrong_field: str):
    layer_bytes = b"layer"
    layer_digest = config_digest(layer_bytes)
    config_bytes = canonical_json({"name": "layer"})
    config_digest_value = config_digest(config_bytes)
    manifest = {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_MANIFEST,
        "config": {"mediaType": MEDIA_TYPE_LAYER_CONFIG, "digest": config_digest_value, "size": len(config_bytes)},
        "layers": [{"mediaType": MEDIA_TYPE_LAYER_SQUASHFS, "digest": layer_digest, "size": len(layer_bytes)}],
    }
    if wrong_field == "config":
        manifest["config"]["size"] += 1
    if wrong_field == "layer":
        manifest["layers"][0]["size"] += 1
    manifest_bytes = canonical_json(manifest)
    manifest_digest = config_digest(manifest_bytes)
    manifest_size = len(manifest_bytes) + (1 if wrong_field == "manifest" else 0)
    index_bytes = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_INDEX,
            "manifests": [{"mediaType": MEDIA_TYPE_MANIFEST, "digest": manifest_digest, "size": manifest_size}],
        }
    )
    archive = tmp_path / f"{wrong_field}.tar"
    archive.write_bytes(
        create_tar_bundle(
            {
                "oci-layout": canonical_json({"imageLayoutVersion": OCI_LAYOUT_VERSION}),
                "index.json": index_bytes,
                f"blobs/sha256/{manifest_digest.split(':', 1)[1]}": manifest_bytes,
                f"blobs/sha256/{config_digest_value.split(':', 1)[1]}": config_bytes,
                f"blobs/sha256/{layer_digest.split(':', 1)[1]}": layer_bytes,
            }
        )
    )
    destination = tmp_path / f"{wrong_field}-layout"
    with pytest.raises(ArtifactValidationError, match="descriptor size mismatch"):
        extract_bundle_tar(archive, destination)
    assert not destination.exists()
