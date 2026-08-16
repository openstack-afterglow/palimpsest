"""Palimpsest image export regression tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from palimpsest_hub.models import PalimpsestImageExport
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.image_exports import (
    CONVERTER_CONTRACT,
    STATUS_COMPLETE,
    _has_external_reference,
    build_qemu_img_convert_command,
    compute_artifact_key,
    compute_source_fingerprint,
    serialize_export,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_compute_source_fingerprint_properties():
    common = {
        "image_id": "img-1",
        "size_bytes": 1024,
        "virtual_size_bytes": 2048,
        "disk_format": "qcow2",
        "updated_at": "2026-08-01T00:00:00Z",
        "checksum": "abc",
        "hash_algo": "sha512",
        "hash_value": "def",
    }
    fp1 = compute_source_fingerprint(**common)
    fp2 = compute_source_fingerprint(**common)
    assert len(fp1) == 64
    assert fp1 == fp2

    fp_diff = compute_source_fingerprint(**(common | {"size_bytes": 2048}))
    assert fp1 != fp_diff


def test_compute_artifact_key_properties():
    fp = "a" * 64
    key1 = compute_artifact_key(fp, "qcow2")
    key2 = compute_artifact_key(fp, "qcow2")
    assert len(key1) == 64
    assert key1 == key2

    payload = {
        "converter_contract": CONVERTER_CONTRACT,
        "source_fingerprint": fp,
        "target_disk_format": "qcow2",
    }
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert key1 == expected


def test_serialize_export_complete_and_pending():
    row_pending = PalimpsestImageExport(
        id="export-1",
        project_id="proj-1",
        created_by="user-1",
        source_image_id="img-1",
        source_name="ubuntu-24.04",
        source_disk_format="raw",
        source_size_bytes=1024,
        source_virtual_size_bytes=2048,
        source_fingerprint="a" * 64,
        artifact_key="b" * 64,
        target_disk_format="qcow2",
        status="queued",
        progress_pct=0,
        created_at=_now(),
    )
    data_pending = serialize_export(row_pending)
    assert data_pending["id"] == "export-1"
    assert data_pending["status"] == "queued"
    assert data_pending["blob_digest"] is None

    row_complete = PalimpsestImageExport(
        id="export-2",
        project_id="proj-1",
        created_by="user-1",
        source_image_id="img-1",
        source_name="ubuntu-24.04",
        source_disk_format="raw",
        source_size_bytes=1024,
        source_virtual_size_bytes=2048,
        source_fingerprint="a" * 64,
        artifact_key="b" * 64,
        target_disk_format="qcow2",
        status=STATUS_COMPLETE,
        progress_pct=100,
        result_blob_digest="sha256:" + "c" * 64,
        result_size_bytes=2048,
        created_at=_now(),
        started_at=_now(),
        completed_at=_now(),
    )
    data_complete = serialize_export(row_complete)
    assert data_complete["status"] == STATUS_COMPLETE
    assert data_complete["blob_digest"] == "sha256:" + "c" * 64
    assert data_complete["size_bytes"] == 2048


def test_has_external_reference():
    assert _has_external_reference({"backing-filename": "/etc/passwd"}) is True
    assert _has_external_reference({"data-file": "/tmp/evil"}) is True
    assert _has_external_reference({"format": "qcow2", "virtual-size": 100}) is False


def test_build_qemu_img_convert_command_vhd_maps_to_vpc():
    cmd = build_qemu_img_convert_command(Path("/tmp/source.raw"), Path("/tmp/target.vhd"), "raw", "vhd")
    assert cmd == [
        "qemu-img",
        "convert",
        "-f",
        "raw",
        "-O",
        "vpc",
        "/tmp/source.raw",
        "/tmp/target.vhd",
    ]


def test_promote_file_symlink_and_traversal_safety(tmp_path: Path):
    store = LocalPathBlobStore(tmp_path / "hub")
    scratch_dir = store.exports_dir / "job-scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    fake_source = scratch_dir / "converted.qcow2"
    payload = b"converted image bytes"
    fake_source.write_bytes(payload)

    promoted = store.promote_file(fake_source, max_bytes=1024)
    assert promoted.size_bytes == len(payload)
    assert store.blob_path(promoted.blob_digest).is_file()


def test_promote_file_refreshes_existing_blob_gc_age(tmp_path: Path):
    store = LocalPathBlobStore(tmp_path / "hub")
    scratch_dir = store.exports_dir / "job-scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    payload = b"duplicate image payload"
    first_file = scratch_dir / "first.qcow2"
    first_file.write_bytes(payload)
    promoted1 = store.promote_file(first_file, max_bytes=1024)

    second_file = scratch_dir / "second.qcow2"
    second_file.write_bytes(payload)
    promoted2 = store.promote_file(second_file, max_bytes=1024)

    assert promoted1.blob_digest == promoted2.blob_digest
    assert not second_file.exists()
