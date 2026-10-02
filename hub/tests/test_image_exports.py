"""Palimpsest image export regression tests."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.models import Base, PalimpsestImageExport
from palimpsest_hub.services import image_exports
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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "error", "lease_lost"])
async def test_claimed_export_logs_lifecycle_without_sensitive_data(tmp_path, monkeypatch, caplog, outcome):
    secret = "sensitive-openstack-token-and-stderr"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hub.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "store")
    source = b"image content"
    checksum = hashlib.sha256(source).hexdigest()
    job = PalimpsestImageExport(
        id="export-secret-id",
        project_id="project-secret-id",
        source_image_id="image-secret-id",
        source_name=secret,
        source_disk_format="raw",
        source_size_bytes=len(source),
        source_virtual_size_bytes=len(source),
        source_hash_algo="sha256",
        source_hash_value=checksum,
        source_fingerprint="a" * 64,
        artifact_key="b" * 64,
        target_disk_format="raw",
        status="queued",
        progress_pct=0,
    )
    async with factory() as session:
        session.add(job)
        await session.commit()

    class Response:
        def iter_content(self, chunk_size):
            yield source

        def close(self):
            pass

    openstack_image = SimpleNamespace(
        status="active",
        owner=job.project_id,
        disk_format="raw",
        size=len(source),
        virtual_size=len(source),
        checksum=None,
        os_hash_algo="sha256",
        os_hash_value=checksum,
        updated_at=None,
    )
    admin_conn = SimpleNamespace(
        image=SimpleNamespace(download_image=lambda *args, **kwargs: Response()), close=lambda: None
    )
    monkeypatch.setattr(image_exports, "get_session_factory", lambda: factory)
    monkeypatch.setattr(image_exports, "get_blob_store", lambda: store)
    monkeypatch.setattr(image_exports, "get_settings", lambda: SimpleNamespace(palimpsest_hub_max_blob_bytes=1024))
    monkeypatch.setattr(image_exports, "get_admin_connection_for_project", lambda _project: admin_conn)
    monkeypatch.setattr(image_exports, "get_image", lambda *_args: openstack_image)

    async def subprocess_result(argv, **kwargs):
        if outcome == "error":
            raise RuntimeError(secret + " /private/secret/path")
        if outcome == "lease_lost":
            raise image_exports.ImageExportLeaseLost(secret)
        if argv[1] == "measure":
            return 0, json.dumps({"required": len(source)}), ""
        return 0, json.dumps({"format": "raw", "virtual-size": len(source)}), ""

    monkeypatch.setattr(image_exports, "_run_subprocess", subprocess_result)
    try:
        with caplog.at_level(logging.DEBUG, logger="palimpsest_hub.services.image_exports"):
            assert await image_exports.process_one_image_export(owner="worker-secret-id") is True
        async with factory() as session:
            stored = await session.get(PalimpsestImageExport, job.id)
            expected = "downloading" if outcome == "lease_lost" else outcome
            assert stored.status == expected
            if outcome == "complete":
                assert stored.result_size_bytes == len(source)
            elif outcome == "error":
                assert stored.error_code == "export_failed"
            else:
                assert stored.lease_owner == "worker-secret-id"
        records = [r for r in caplog.records if r.name == image_exports.__name__]
        info = [r.getMessage() for r in records if r.levelno == logging.INFO]
        assert info == ["Export task started status=downloading", f"Export task ended status={outcome}"]
        debug = [r.getMessage() for r in records if r.levelno == logging.DEBUG]
        assert len(debug) == 1
        assert f"status={outcome}" in debug[0] and "elapsed_ms=" in debug[0]
        assert "downloaded_bytes=" in debug[0] and "output_bytes=" in debug[0]
        for record in records:
            assert record.exc_info is None
        emitted = "\n".join(r.getMessage() for r in records)
        for forbidden in (secret, "secret-id", "/private/secret/path", checksum):
            assert forbidden not in emitted
    finally:
        await engine.dispose()
