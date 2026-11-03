"""Palimpsest image export regression tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from palimpsest_hub.models import (
    Base,
    PalimpsestImageExport,
    PalimpsestImageExportDelegation,
)
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
from palimpsest_hub.services.worker_leases import LeaseLost, role_lease, run_blocking


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
        created_by="requester-secret-id",
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
    delegation = PalimpsestImageExportDelegation(
        trust_id="trust-secret-id",
        export_id=job.id,
        project_id=job.project_id,
        trustor_user_id=job.created_by,
        trustee_user_id="trustee-secret-id",
        role_names=["reader"],
        expires_at=_now() + timedelta(hours=1),
        state=image_exports.DELEGATION_ACTIVE,
    )
    async with factory() as session:
        session.add_all([job, delegation])
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
    export_conn = SimpleNamespace(close=lambda: None)
    opened = []

    def authorized(project_id, verified):
        opened.append((project_id, verified.trust_id, verified.trustor_user_id))
        return export_conn

    deleted = []
    monkeypatch.setattr(image_exports, "get_session_factory", lambda: factory)
    monkeypatch.setattr(image_exports, "get_blob_store", lambda: store)
    monkeypatch.setattr(image_exports, "get_settings", lambda: SimpleNamespace(palimpsest_hub_max_blob_bytes=1024))
    monkeypatch.setattr(image_exports, "_authorized_export_connection", authorized)
    monkeypatch.setattr(image_exports, "get_image", lambda *_args: openstack_image)
    monkeypatch.setattr(image_exports, "download_image", lambda conn, _image: Response())
    monkeypatch.setattr(
        image_exports, "delete_export_trust", lambda verified: deleted.append(verified.trust_id) or True
    )

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
            retained = await session.get(PalimpsestImageExportDelegation, delegation.trust_id)
            expected = "downloading" if outcome == "lease_lost" else outcome
            assert stored.status == expected
            assert opened == [(job.project_id, delegation.trust_id, job.created_by)]
            if outcome == "complete":
                assert stored.result_size_bytes == len(source)
            elif outcome == "error":
                assert stored.error_code == "export_failed"
            else:
                assert stored.lease_owner.startswith("worker-secret-id:")
            # Terminal outcomes delete the Trust; a lost lease leaves it to the new owner.
            if outcome == "lease_lost":
                assert (retained.state, deleted) == (image_exports.DELEGATION_ACTIVE, [])
            else:
                assert (retained.state, deleted) == (image_exports.DELEGATION_DELETED, [delegation.trust_id])
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


@pytest.fixture
async def export_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalPathBlobStore(tmp_path / "ownership-store")
    monkeypatch.setattr(image_exports, "get_session_factory", lambda: factory)
    monkeypatch.setattr(image_exports, "get_blob_store", lambda: store)
    try:
        yield SimpleNamespace(factory=factory, store=store)
    finally:
        await engine.dispose()


async def _queued_export(factory):
    job = PalimpsestImageExport(
        id=str(uuid.uuid4()),
        project_id="project",
        created_by="requester",
        source_image_id=str(uuid.uuid4()),
        source_name="source",
        source_disk_format="raw",
        source_size_bytes=1,
        source_virtual_size_bytes=1,
        source_hash_algo="sha256",
        source_hash_value="a" * 64,
        source_fingerprint=uuid.uuid4().hex * 2,
        artifact_key=uuid.uuid4().hex * 2,
        target_disk_format="raw",
        status="queued",
        progress_pct=0,
    )
    async with factory() as session, session.begin():
        session.add(job)
    return job


@pytest.mark.asyncio
async def test_export_expiry_rejects_heartbeat_and_completion_and_reclaim_has_unique_token(export_db):
    factory = export_db.factory
    job = await _queued_export(factory)
    first = await image_exports.claim_next_image_export(owner="same-process")
    async with factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExport)
            .where(
                PalimpsestImageExport.id == job.id,
            )
            .values(lease_expires_at=_now() - timedelta(seconds=1))
        )
        assert not await image_exports._update_job_cas(session, job.id, first.lease_owner)
        assert not await image_exports._update_job_cas(
            session,
            job.id,
            first.lease_owner,
            status="complete",
            clear_lease=True,
        )
    second = await image_exports.claim_next_image_export(owner="same-process")
    assert second.id == first.id and second.attempts == first.attempts + 1
    assert second.lease_owner != first.lease_owner
    async with factory() as session, session.begin():
        assert not await image_exports._update_job_cas(
            session,
            job.id,
            first.lease_owner,
            status="error",
            clear_lease=True,
        )
        assert await image_exports._update_job_cas(session, job.id, second.lease_owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [None, "complete", "error"])
async def test_export_cas_synchronizes_reloaded_naive_timestamp_owner(export_db, terminal_status):
    factory = export_db.factory
    job = await _queued_export(factory)
    claimed = await image_exports.claim_next_image_export(owner="timestamp-owner")
    async with factory() as session, session.begin():
        loaded = await session.get(PalimpsestImageExport, job.id)
        # SQLite, like MySQL DATETIME, returns naive storage timestamps. Keep
        # this object in the identity map to exercise ORM bulk synchronization.
        assert loaded.lease_expires_at.tzinfo is None
        previous_expiry = image_exports._aware(loaded.lease_expires_at)
        assert await image_exports._update_job_cas(
            session,
            job.id,
            claimed.lease_owner,
            status=terminal_status,
            progress_pct=17,
            clear_lease=terminal_status is not None,
            extend_lease_seconds=300,
        )
        assert await session.get(PalimpsestImageExport, job.id) is loaded
        assert loaded.status == (terminal_status or "downloading")
        assert loaded.progress_pct == 17
        if terminal_status is None:
            assert loaded.lease_owner == claimed.lease_owner
            assert image_exports._aware(loaded.lease_expires_at) > previous_expiry
        else:
            assert loaded.lease_owner is None and loaded.lease_expires_at is None
    async with factory() as session:
        persisted = await session.get(PalimpsestImageExport, job.id)
        assert persisted.status == (terminal_status or "downloading")
        assert persisted.progress_pct == 17
        assert persisted.lease_owner == (claimed.lease_owner if terminal_status is None else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_soft_delete_checks_reloaded_naive_export_lease_without_adopting_live_owner(export_db, expired):
    factory = export_db.factory
    job = await _queued_export(factory)
    claimed = await image_exports.claim_next_image_export(owner="delete-owner")
    async with factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExport)
            .where(
                PalimpsestImageExport.id == job.id,
            )
            .values(lease_expires_at=_now() + timedelta(seconds=-1 if expired else 120))
        )
    if expired:
        deleted = await image_exports.soft_delete_project_export(job.project_id, job.id)
        assert deleted.deleted_at is not None
    else:
        with pytest.raises(image_exports.ImageExportError) as error:
            await image_exports.soft_delete_project_export(job.project_id, job.id)
        assert error.value.status_code == 409 and error.value.code == "cannot_delete_active_job"
    async with factory() as session:
        persisted = await session.get(PalimpsestImageExport, job.id)
        assert (persisted.deleted_at is not None) is expired
        assert persisted.lease_owner == claimed.lease_owner and persisted.status == "downloading"


@pytest.mark.asyncio
async def test_maintenance_keeps_live_unknown_and_current_attempt_scratch(export_db):
    factory, store = export_db.factory, export_db.store
    await _queued_export(factory)
    old = await image_exports.claim_next_image_export(owner="process")
    old_path = image_exports._scratch_dir_for(store.exports_dir, old, old.lease_owner)
    async with factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExport)
            .where(
                PalimpsestImageExport.id == old.id,
            )
            .values(lease_expires_at=_now() - timedelta(seconds=1))
        )
    current = await image_exports.claim_next_image_export(owner="process")
    current_path = image_exports._scratch_dir_for(store.exports_dir, current, current.lease_owner)
    unknown_owner = image_exports._scratch_dir_for(store.exports_dir, current, "unrecognized-attempt")
    unknown_job = store.exports_dir / f"{uuid.uuid4()}-1-{'a' * 12}"
    legacy = store.exports_dir / "legacy-name"
    for path in (old_path, current_path, unknown_owner, unknown_job, legacy):
        path.mkdir(parents=True)
        (path / "output.raw").write_bytes(b"owned bytes")
        os.utime(path, (1, 1))
    await image_exports.run_export_maintenance(max_age_seconds=1)
    assert not old_path.exists()
    for path in (current_path, unknown_owner, unknown_job, legacy):
        assert (path / "output.raw").read_bytes() == b"owned bytes"
    async with factory() as session, session.begin():
        await session.execute(
            update(PalimpsestImageExport)
            .where(
                PalimpsestImageExport.id == current.id,
            )
            .values(lease_expires_at=_now() - timedelta(seconds=1))
        )
    await image_exports.run_export_maintenance(max_age_seconds=1)
    assert not current_path.exists()
    assert unknown_owner.exists() and unknown_job.exists() and legacy.exists()


@pytest.mark.asyncio
async def test_competing_maintenance_role_skips_shared_blob_removal(export_db):
    store = export_db.store
    store.blobs_dir.mkdir(parents=True, exist_ok=True)
    blob = store.blobs_dir / ("a" * 64)
    blob.write_bytes(b"old unreferenced bytes")
    os.utime(blob, (1, 1))
    async with role_lease(export_db.factory, "export-maintenance") as lease:
        assert lease is not None
        await image_exports.run_export_maintenance(max_age_seconds=1)
        assert blob.exists()
    await image_exports.run_export_maintenance(max_age_seconds=1)
    assert not blob.exists()


@pytest.mark.asyncio
async def test_maintenance_preserves_retained_image_and_deleted_instance_sources(export_db):
    from palimpsest_hub.services.blob_references import retain_blob

    store = export_db.store
    payloads = {
        "context": b"retained Dockerfile context",
        "image-job": b"staged OCI output",
        "instance": b"deleted instance reusable lower",
    }
    retained = {}
    for index, (kind, payload) in enumerate(payloads.items()):
        identity = "sha256:" + hashlib.sha256(payload).hexdigest()
        store.blobs_dir.mkdir(parents=True, exist_ok=True)
        path = store.blob_path(identity)
        path.write_bytes(payload)
        os.utime(path, (1, 1))
        async with export_db.factory() as session:
            await retain_blob(session, identity, kind, str(index))
            await session.commit()
        retained[identity] = payload
    garbage = store.blobs_dir / ("f" * 64)
    garbage.write_bytes(b"unreferenced expired staging")
    os.utime(garbage, (1, 1))
    await image_exports.run_export_maintenance(max_age_seconds=1)
    assert not garbage.exists()
    for identity, payload in retained.items():
        assert store.blob_path(identity).read_bytes() == payload


@pytest.mark.asyncio
async def test_role_lease_renews_and_excludes_competitors(export_db):
    async with role_lease(export_db.factory, "renewal", ttl_seconds=0.6) as first:
        assert first is not None
        await asyncio.sleep(1.4)
        assert not first.lost.is_set()
        async with role_lease(export_db.factory, "renewal") as contender:
            assert contender is None
    async with role_lease(export_db.factory, "renewal") as successor:
        assert successor is not None and successor.token != first.token
        await first.release()
        assert await successor.renew()


@pytest.mark.asyncio
async def test_expired_role_cannot_renew_fence_or_release_successor(export_db):
    from palimpsest_hub.models import PalimpsestWorkerLease

    async with role_lease(export_db.factory, "expiry") as first:
        async with export_db.factory() as session, session.begin():
            await session.execute(
                update(PalimpsestWorkerLease)
                .where(
                    PalimpsestWorkerLease.name == "expiry",
                )
                .values(expires_at=_now() - timedelta(seconds=1))
            )
        assert not await first.renew()
        with pytest.raises(LeaseLost):
            async with first.fence():
                pytest.fail("expired lease entered effect fence")
        async with role_lease(export_db.factory, "expiry") as successor:
            assert successor is not None and successor.token != first.token
            await first.release()
            async with export_db.factory() as session:
                stored = await session.scalar(
                    select(PalimpsestWorkerLease).where(
                        PalimpsestWorkerLease.name == "expiry",
                    )
                )
                assert stored.token == successor.token


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_fails", [False, True])
async def test_blocking_effect_drains_repeated_cancellation_without_replacing_it(thread_fails):
    started, release, drained = threading.Event(), threading.Event(), threading.Event()

    def effect():
        started.set()
        try:
            assert release.wait(5), "test did not release the blocking effect"
            if thread_fails:
                raise RuntimeError("late provider failure")
            return "completed"
        finally:
            drained.set()

    task = asyncio.create_task(run_blocking(effect))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not drained.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
