from __future__ import annotations

import gc
import hashlib
import io
import json
import multiprocessing
import os
import pickle
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import palimpsest_local.artifact_store as artifact_store_module
import palimpsest_local.oci_store as oci_store_module
from palimpsest_local.artifact_store import ArtifactStore, ArtifactStoreError
from palimpsest_local.oci_converter import (
    DEFAULT_LAYER_CONVERSION_LIMITS,
    LAYER_INTAKE_POLICY_ID,
    LayerIntakeReceipt,
)
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    SQUASHFS_STRUCTURAL_VERIFIER_ID,
    LeasedSquashFS,
    PackedSquashFSReceipt,
    SquashFSToolchainIdentity,
    VerifiedSquashFSToolchain,
)
from palimpsest_local.oci_provenance import OCI_LAYER_MEDIA_TYPE
from palimpsest_local.oci_store import (
    ArtifactLeaseOwner,
    DerivedLayerOccurrence,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStore,
    OCIStoreError,
)
from palimpsest_local.state import StatePaths, init_resolved_roots


def _digest(byte: str) -> str:
    return "sha256:" + byte * 64


def _squashfs(payload: bytes = b"payload" + b"\0" * 57) -> bytes:
    image_size = 96 + len(payload)
    superblock = struct.pack(
        "<5I6H8Q",
        0x73717368,
        1,
        0,
        131072,
        0,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        image_size,
        144,
        2**64 - 1,
        96,
        112,
        2**64 - 1,
        2**64 - 1,
    )
    return superblock + payload


def _store(tmp_path: Path, *, repair_age: float = 0) -> tuple[StatePaths, OCIStore]:
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    return roots, OCIStore(roots, repair_min_age_seconds=repair_age)


def _occurrence(ordinal: int = 0) -> DerivedLayerOccurrence:
    return DerivedLayerOccurrence(
        source_snapshot_binding_digest=_digest("a"),
        source_image_digest=_digest("b"),
        ordinal=ordinal,
        media_type=OCI_LAYER_MEDIA_TYPE,
        compressed_digest=_digest("c"),
        compressed_size=123,
        diff_id=_digest("d"),
    )


def _toolchain() -> SquashFSToolchainIdentity:
    return SquashFSToolchainIdentity("4.7.5", _digest("e"), (_digest("f"),))


def _toolchain_capability() -> VerifiedSquashFSToolchain:
    return VerifiedSquashFSToolchain(_toolchain(), Path("/test/mksquashfs"), ())


def _key(occurrence: DerivedLayerOccurrence) -> DerivedSquashFSKey:
    return DerivedSquashFSKey.for_occurrence(
        occurrence,
        intake_policy_id=LAYER_INTAKE_POLICY_ID,
        intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
        pack_policy_id=SQUASHFS_PACK_POLICY_ID,
        pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
        toolchain=_toolchain_capability(),
    )


def _receipts(occurrence: DerivedLayerOccurrence, image: bytes) -> tuple[LayerIntakeReceipt, PackedSquashFSReceipt]:
    intake = LayerIntakeReceipt(
        policy_id=LAYER_INTAKE_POLICY_ID,
        policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
        ordinal=occurrence.ordinal,
        media_type=occurrence.media_type,
        compressed_digest=occurrence.compressed_digest,
        compressed_size=occurrence.compressed_size,
        diff_id=occurrence.diff_id,
        uncompressed_size=10240,
        physical_headers=1,
        members=1,
        regular_bytes=7,
        xattr_bytes=0,
    )
    toolchain = _toolchain()
    packed = PackedSquashFSReceipt(
        policy_id=SQUASHFS_PACK_POLICY_ID,
        policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
        source_ordinal=occurrence.ordinal,
        source_diff_id=occurrence.diff_id,
        normalized_tar_digest=_digest("1"),
        normalized_tar_size=10240,
        entries=1,
        packer_version=toolchain.version,
        packer_sha256=toolchain.executable_digest.removeprefix("sha256:"),
        image_digest=f"sha256:{hashlib.sha256(image).hexdigest()}",
        image_size=len(image),
        structural_verifier=SQUASHFS_STRUCTURAL_VERIFIER_ID,
        toolchain_fingerprint=toolchain.fingerprint,
        toolchain_dependency_digests=toolchain.dependency_digests,
    )
    return intake, packed


def _producer(occurrence: DerivedLayerOccurrence, calls: list[int], image: bytes):
    @contextmanager
    def produce():
        calls.append(occurrence.ordinal)
        intake, receipt = _receipts(occurrence, image)
        packed = LeasedSquashFS(io.BytesIO(image), receipt)
        try:
            yield intake, packed
        finally:
            failure = packed._close()
            if failure is not None:
                raise failure

    return produce


def _process_materialize(
    config_root: str,
    state_root: str,
    marker: str,
    occurrence: DerivedLayerOccurrence,
    key: DerivedSquashFSKey,
    image: bytes,
) -> None:
    store = OCIStore(StatePaths(Path(config_root), Path(state_root)), repair_min_age_seconds=0)

    @contextmanager
    def produce():
        marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(marker_fd, b"x")
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
        time.sleep(0.03)
        intake, receipt = _receipts(occurrence, image)
        packed = LeasedSquashFS(io.BytesIO(image), receipt)
        try:
            yield intake, packed
        finally:
            packed._close()

    store.materialize(occurrence, key, produce)


def test_cold_materialization_warm_hit_and_repeated_occurrence_share_physical_bytes(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    image = _squashfs()
    first = _occurrence(0)
    second = _occurrence(1)
    calls: list[int] = []

    first_receipt = store.materialize(first, _key(first), _producer(first, calls, image))
    warm_receipt = store.materialize(first, _key(first), lambda: (_ for _ in ()).throw(AssertionError("warm")))
    second_receipt = store.materialize(second, _key(second), lambda: (_ for _ in ()).throw(AssertionError("warm")))

    assert calls == [0]
    assert warm_receipt == first_receipt
    assert first_receipt.image_digest == second_receipt.image_digest
    assert first_receipt.record_digest == second_receipt.record_digest
    assert first_receipt.occurrence_digest != second_receipt.occurrence_digest
    assert len(list((roots.store / "blobs" / "sha256").glob("[0-9a-f]" * 64))) == 1


def test_observed_materialization_reports_invocation_local_cache_result(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    image = _squashfs()
    first = _occurrence(0)
    second = _occurrence(1)
    key = _key(first)

    cold = store.materialize_observed(first, key, _producer(first, [], image))
    warm = store.materialize_observed(
        second,
        _key(second),
        lambda: (_ for _ in ()).throw(AssertionError("warm producer invoked")),
    )

    assert isinstance(cold, MaterializationResult)
    assert cold.cache_result == "cold_miss"
    assert warm.cache_result == "warm_hit"
    assert cold.receipt.record_digest == warm.receipt.record_digest
    assert cold.receipt.occurrence_digest != warm.receipt.occurrence_digest


def test_observed_materialization_reports_validated_repair(tmp_path: Path) -> None:
    roots, store = _store(tmp_path, repair_age=0)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    first = store.materialize(occurrence, key, _producer(occurrence, [], image))
    index = roots.oci_derived_store / "keys" / key.digest.removeprefix("sha256:")
    os.chmod(index, 0o600)
    index.write_bytes(b"x" * index.stat().st_size)
    os.chmod(index, 0o400)

    repaired = store.materialize_observed(occurrence, key, _producer(occurrence, [], image))

    assert repaired.cache_result == "cold_repair"
    assert repaired.receipt == first


def test_materialization_result_rejects_unhashable_cache_result() -> None:
    receipt = oci_store_module.DerivedLayerReceipt(
        store_id="oci-store-v1:" + "0" * 64,
        occurrence_digest=_digest("1"),
        record_digest=_digest("2"),
        key_digest=_digest("3"),
        source_snapshot_binding_digest=_digest("4"),
        source_image_digest=_digest("5"),
        ordinal=0,
        image_digest=_digest("6"),
        image_size=1,
    )

    with pytest.raises(OCIStoreError, match="cache result is invalid"):
        MaterializationResult(receipt, [])  # type: ignore[arg-type]


def test_cold_inconsistent_receipts_publish_nothing(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()

    @contextmanager
    def inconsistent():
        intake, receipt = _receipts(occurrence, image)
        packed = LeasedSquashFS(io.BytesIO(image), replace(receipt, normalized_tar_size=0))
        try:
            yield intake, packed
        finally:
            packed._close()

    with pytest.raises(OCIStoreError, match="internally inconsistent"):
        store.materialize(occurrence, _key(occurrence), inconsistent)

    assert list((roots.store / "blobs" / "sha256").glob("[0-9a-f]" * 64)) == []
    for name in ("records", "keys", "occurrences", "leases"):
        assert list((roots.oci_derived_store / name).iterdir()) == []


def test_warm_canonical_receipt_tamper_is_rebuilt_and_repaired(tmp_path: Path) -> None:
    roots, store = _store(tmp_path, repair_age=0)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    first = store.materialize(occurrence, key, _producer(occurrence, [], image))
    records = roots.oci_derived_store / "records"
    original = json.loads((records / first.record_digest.removeprefix("sha256:")).read_bytes())
    original["packed_receipt"]["normalized_tar_size"] = 0
    tampered_payload = json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    tampered_digest = f"sha256:{hashlib.sha256(tampered_payload).hexdigest()}"
    tampered_record = records / tampered_digest.removeprefix("sha256:")
    tampered_record.write_bytes(tampered_payload)
    tampered_record.chmod(0o400)
    index = roots.oci_derived_store / "keys" / key.digest.removeprefix("sha256:")
    index_payload = json.dumps(
        {
            "key_digest": key.digest,
            "record_digest": tampered_digest,
            "schema": "palimpsest.oci-derived-recipe.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    index.chmod(0o600)
    index.write_bytes(index_payload)
    index.chmod(0o400)
    calls: list[int] = []

    repaired = store.materialize(occurrence, key, _producer(occurrence, calls, image))

    assert repaired == first
    assert calls == [0]
    assert json.loads(index.read_bytes())["record_digest"] == first.record_digest


def test_durable_lease_requires_verified_eof_and_can_be_resumed(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    lease = store.acquire_lease(receipt, owner)

    assert not hasattr(lease, "path") and not hasattr(lease, "fileno")
    with pytest.raises(OCIStoreError, match="verified EOF"):
        lease.close()
    lease_id = lease.lease_id
    lease._abort()

    resumed = store.resume_lease(lease_id, owner, receipt)
    with resumed:
        assert b"".join(resumed.chunks(17)) == image
    with pytest.raises(OCIStoreError, match="missing"):
        store.resume_lease(lease_id, owner, receipt)


def test_durable_lease_is_discoverable_and_public_bindings_are_read_only(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    lease = store.acquire_lease(receipt, owner)

    assert store.list_leases(owner)[0].lease_id == lease.lease_id
    with pytest.raises(AttributeError):
        lease.lease_id = str(uuid.uuid4())
    with pytest.raises(AttributeError):
        lease.owner = ArtifactLeaseOwner(str(uuid.uuid4()), "other", "root-layer")
    with pytest.raises(AttributeError):
        lease.receipt = receipt
    lease._abort()


def test_physical_delete_honors_durable_lease_and_legacy_publish_reuses_inode(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    lease = store.acquire_lease(receipt, owner)
    physical = ArtifactStore(roots.store)
    blob = roots.store / "blobs" / "sha256" / receipt.image_digest.split(":", 1)[1]
    identity = (blob.stat().st_dev, blob.stat().st_ino)

    ContentStore(roots.store).write_stream([image], expected_digest=receipt.image_digest)

    assert (blob.stat().st_dev, blob.stat().st_ino) == identity
    with pytest.raises(OCIStoreError, match="durable OCI lease"):
        physical.delete_blob(
            receipt.image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipt.image_digest),
        )
    assert blob.is_file()

    with lease:
        assert b"".join(lease.chunks(19)) == image
    assert physical.delete_blob(
        receipt.image_digest,
        retention_guard=lambda: store.assert_artifact_unleased(receipt.image_digest),
    ) == len(image)
    assert not blob.exists()


def test_physical_delete_fails_closed_on_malformed_lease_record(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    image = _squashfs()
    occurrence = _occurrence()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    malformed = roots.oci_derived_store / "leases" / str(uuid.uuid4())
    malformed.write_bytes(b"{}")
    malformed.chmod(0o400)
    physical = ArtifactStore(roots.store)

    with pytest.raises(OCIStoreError, match="fields are invalid"):
        physical.delete_blob(
            receipt.image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipt.image_digest),
        )

    assert (roots.store / "blobs" / "sha256" / receipt.image_digest.split(":", 1)[1]).is_file()


def test_lease_acquisition_linearizes_before_physical_delete(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    image = _squashfs()
    occurrence = _occurrence()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    physical = ArtifactStore(roots.store)
    publishing = threading.Event()
    allow_publish = threading.Event()
    acquired = threading.Event()
    release = threading.Event()
    delete_attempted = threading.Event()
    outcomes: list[str] = []
    real_publish = store._publish_file
    real_delete_lock = physical._digest_lock

    @contextmanager
    def observed_delete_lock(authority, digest_hex):
        delete_attempted.set()
        with real_delete_lock(authority, digest_hex):
            yield

    def delayed_publish(authority, directory_fd, name, payload, **kwargs):
        if json.loads(payload).get("schema") == oci_store_module.DERIVED_LEASE_SCHEMA:
            publishing.set()
            assert allow_publish.wait(2)
        return real_publish(authority, directory_fd, name, payload, **kwargs)

    monkeypatch.setattr(store, "_publish_file", delayed_publish)
    monkeypatch.setattr(physical, "_digest_lock", observed_delete_lock)

    def acquire() -> None:
        lease = store.acquire_lease(receipt, owner)
        acquired.set()
        assert release.wait(2)
        lease._abort()

    def delete() -> None:
        try:
            physical.delete_blob(
                receipt.image_digest,
                retention_guard=lambda: store.assert_artifact_unleased(receipt.image_digest),
            )
        except OCIStoreError as exc:
            outcomes.append(exc.code)

    acquire_thread = threading.Thread(target=acquire)
    delete_thread = threading.Thread(target=delete)
    acquire_thread.start()
    assert publishing.wait(2)
    delete_thread.start()
    assert delete_attempted.wait(2)
    assert delete_thread.is_alive() and outcomes == []
    allow_publish.set()
    assert acquired.wait(2)
    delete_thread.join(2)
    release.set()
    acquire_thread.join(2)

    assert not acquire_thread.is_alive() and not delete_thread.is_alive()
    assert outcomes == ["oci-store-in-use"]
    assert (roots.store / "blobs" / "sha256" / receipt.image_digest.split(":", 1)[1]).is_file()


def test_physical_delete_linearizes_before_lease_acquisition(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    image = _squashfs()
    occurrence = _occurrence()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    physical = ArtifactStore(roots.store)
    retention_checked = threading.Event()
    allow_delete = threading.Event()
    acquire_attempted = threading.Event()
    outcomes: list[str] = []
    real_acquire_lock = store._artifacts._digest_lock

    @contextmanager
    def observed_acquire_lock(authority, digest_hex):
        acquire_attempted.set()
        with real_acquire_lock(authority, digest_hex):
            yield

    monkeypatch.setattr(store._artifacts, "_digest_lock", observed_acquire_lock)

    def guard() -> None:
        store.assert_artifact_unleased(receipt.image_digest)
        retention_checked.set()
        assert allow_delete.wait(2)

    def delete() -> None:
        physical.delete_blob(receipt.image_digest, retention_guard=guard)

    def acquire() -> None:
        try:
            store.acquire_lease(receipt, owner)
        except ArtifactStoreError as exc:
            outcomes.append(exc.code)

    delete_thread = threading.Thread(target=delete)
    acquire_thread = threading.Thread(target=acquire)
    delete_thread.start()
    assert retention_checked.wait(2)
    acquire_thread.start()
    assert acquire_attempted.wait(2)
    assert acquire_thread.is_alive() and outcomes == []
    allow_delete.set()
    delete_thread.join(2)
    acquire_thread.join(2)

    assert not delete_thread.is_alive() and not acquire_thread.is_alive()
    assert outcomes == ["artifact-missing"]
    assert list((roots.oci_derived_store / "leases").iterdir()) == []


def test_physical_delete_unlink_fault_keeps_blob_and_reports_no_success(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    image = _squashfs()
    occurrence = _occurrence()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    blob = roots.store / "blobs" / "sha256" / receipt.image_digest.split(":", 1)[1]
    real_unlink = artifact_store_module.os.unlink

    def fail_target_unlink(name, *args, **kwargs):
        if name == receipt.image_digest.split(":", 1)[1]:
            raise OSError("injected unlink failure")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(artifact_store_module.os, "unlink", fail_target_unlink)
    with pytest.raises(ArtifactStoreError, match="artifact deletion failed"):
        ArtifactStore(roots.store).delete_blob(
            receipt.image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipt.image_digest),
        )

    assert blob.is_file()


def test_physical_delete_fsync_fault_reports_no_success(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    image = _squashfs()
    occurrence = _occurrence()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    blob = roots.store / "blobs" / "sha256" / receipt.image_digest.split(":", 1)[1]
    blobs_stat = (blob.parent.stat().st_dev, blob.parent.stat().st_ino)
    real_fsync = artifact_store_module.os.fsync

    def fail_blobs_fsync(fd: int) -> None:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == blobs_stat:
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(artifact_store_module.os, "fsync", fail_blobs_fsync)
    with pytest.raises(ArtifactStoreError, match="artifact deletion failed"):
        ArtifactStore(roots.store).delete_blob(
            receipt.image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipt.image_digest),
        )

    assert not blob.exists()


def test_lease_open_failure_does_not_publish_orphan_record(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))

    @contextmanager
    def fail_open(*_args, **_kwargs):
        raise ArtifactStoreError("artifact-open", "injected open failure")
        yield

    monkeypatch.setattr(store._artifacts, "open_squashfs", fail_open)
    with pytest.raises(ArtifactStoreError, match="injected open failure"):
        store.acquire_lease(receipt, ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer"))

    assert list((roots.oci_derived_store / "leases").iterdir()) == []


def test_duplicate_resume_waits_then_observes_released_record(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    acquired = store.acquire_lease(receipt, owner)
    lease_id = acquired.lease_id
    acquired._abort()
    first = store.resume_lease(lease_id, owner, receipt)
    outcome: list[str] = []

    def resume_again() -> None:
        try:
            store.resume_lease(lease_id, owner, receipt)
        except OCIStoreError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=resume_again)
    thread.start()
    time.sleep(0.03)
    assert outcome == [] and thread.is_alive()
    with first:
        assert b"".join(first.chunks()) == image
    thread.join(2)

    assert outcome == ["oci-store-missing"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_child_gc_does_not_close_reused_lock_fd(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    lease = store.acquire_lease(receipt, ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer"))
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            opened = [os.open(os.devnull, os.O_RDONLY) for _ in range(32)]
            del lease
            gc.collect()
            ok = all(os.fstat(fd).st_mode for fd in opened)
            os.write(write_fd, b"ok" if ok else b"closed")
        except BaseException as exc:
            os.write(write_fd, repr(exc).encode()[:200])
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 200)
    os.close(read_fd)
    os.waitpid(child, 0)
    lease._abort()
    assert result == b"ok"


def test_lease_rejects_copy_pickle_and_foreign_thread(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    lease = store.acquire_lease(receipt, ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer"))

    with pytest.raises(OCIStoreError, match="cannot be copied"):
        copy(lease)
    with pytest.raises(OCIStoreError, match="cannot be copied"):
        deepcopy(lease)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(lease)
    failures: list[BaseException] = []

    def consume() -> None:
        try:
            next(lease.chunks())
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=consume)
    thread.start()
    thread.join()
    assert len(failures) == 1 and isinstance(failures[0], OCIStoreError)


def test_same_size_blob_corruption_is_rebuilt_only_for_requested_recipe(tmp_path: Path) -> None:
    roots, store = _store(tmp_path, repair_age=0)
    occurrence = _occurrence()
    image = _squashfs()
    first = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    blob = roots.store / "blobs" / "sha256" / first.image_digest.removeprefix("sha256:")
    os.chmod(blob, 0o600)
    corrupted = bytearray(blob.read_bytes())
    corrupted[-1] ^= 1
    blob.write_bytes(corrupted)
    os.chmod(blob, 0o400)
    calls: list[int] = []

    result = store.materialize_observed(occurrence, _key(occurrence), _producer(occurrence, calls, image))

    assert calls == [0]
    assert result.cache_result == "cold_repair"
    assert result.receipt == first
    assert blob.read_bytes() == image


def test_same_recipe_thread_contention_invokes_one_producer(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    calls: list[int] = []

    @contextmanager
    def slow_producer():
        calls.append(occurrence.ordinal)
        time.sleep(0.03)
        intake, receipt = _receipts(occurrence, image)
        packed = LeasedSquashFS(io.BytesIO(image), receipt)
        try:
            yield intake, packed
        finally:
            packed._close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _index: store.materialize_observed(occurrence, key, slow_producer), range(4))
        )

    assert calls == [0]
    assert [result.cache_result for result in results].count("cold_miss") == 1
    assert [result.cache_result for result in results].count("warm_hit") == 3
    assert len({result.receipt for result in results}) == 1


def test_same_recipe_spawn_process_contention_invokes_one_producer(tmp_path: Path) -> None:
    roots, _store_value = _store(tmp_path)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    marker = tmp_path / "producer-count"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_process_materialize,
            args=(str(roots.config), str(roots.state), str(marker), occurrence, key, image),
        )
        for _ in range(3)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(15)

    assert [process.exitcode for process in processes] == [0, 0, 0]
    assert marker.read_bytes() == b"x"


def test_fresh_corruption_defers_repair_without_false_hit(tmp_path: Path) -> None:
    roots, store = _store(tmp_path, repair_age=3600)
    occurrence = _occurrence()
    image = _squashfs()
    first = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    blob = roots.store / "blobs" / "sha256" / first.image_digest.removeprefix("sha256:")
    os.chmod(blob, 0o600)
    blob.write_bytes(b"x" * len(image))
    os.chmod(blob, 0o400)

    with pytest.raises(ArtifactStoreError, match="repair-deferred"):
        store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))


def test_corrupt_recipe_index_is_age_gated_and_repaired_atomically(tmp_path: Path) -> None:
    roots, store = _store(tmp_path, repair_age=0)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    first = store.materialize(occurrence, key, _producer(occurrence, [], image))
    index = roots.oci_derived_store / "keys" / key.digest.removeprefix("sha256:")
    os.chmod(index, 0o600)
    index.write_bytes(b"x" * index.stat().st_size)
    os.chmod(index, 0o400)
    calls: list[int] = []

    repaired = store.materialize(occurrence, key, _producer(occurrence, calls, image))

    assert repaired == first
    assert calls == [0]


def test_dangling_canonical_recipe_index_is_repaired(tmp_path: Path) -> None:
    roots, store = _store(tmp_path, repair_age=0)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    first = store.materialize(occurrence, key, _producer(occurrence, [], image))
    index = roots.oci_derived_store / "keys" / key.digest.removeprefix("sha256:")
    dangling = {
        "key_digest": key.digest,
        "record_digest": _digest("9"),
        "schema": "palimpsest.oci-derived-recipe.v1",
    }
    os.chmod(index, 0o600)
    index.write_bytes(json.dumps(dangling, sort_keys=True, separators=(",", ":")).encode())
    os.chmod(index, 0o400)
    calls: list[int] = []

    repaired = store.materialize(occurrence, key, _producer(occurrence, calls, image))

    assert repaired == first
    assert calls == [0]


def test_artifact_reader_clean_exit_requires_complete_bytes(tmp_path: Path) -> None:
    roots, _store_value = _store(tmp_path)
    image = _squashfs()
    digest = f"sha256:{hashlib.sha256(image).hexdigest()}"
    from palimpsest_local.artifact_store import ArtifactStore

    artifacts = ArtifactStore(roots.store, repair_min_age_seconds=0)
    artifacts.publish_squashfs((image,), expected_digest=digest, expected_size=len(image), maximum=len(image))

    with pytest.raises(ArtifactStoreError, match="incomplete"):
        with artifacts.open_squashfs(digest, len(image), maximum=len(image)):
            pass


def test_artifact_cache_hit_cleanup_fault_is_not_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    roots, _store_value = _store(tmp_path)
    image = _squashfs()
    digest = f"sha256:{hashlib.sha256(image).hexdigest()}"
    artifacts = artifact_store_module.ArtifactStore(roots.store, repair_min_age_seconds=0)
    artifacts.publish_squashfs((image,), expected_digest=digest, expected_size=len(image), maximum=len(image))
    real_unlink = artifact_store_module.os.unlink

    def fail_temporary_unlink(name, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".oci-artifact-tmp-"):
            raise OSError("injected unlink fault")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(artifact_store_module.os, "unlink", fail_temporary_unlink)

    with pytest.raises(ArtifactStoreError, match="artifact-cleanup"):
        artifacts.publish_squashfs((image,), expected_digest=digest, expected_size=len(image), maximum=len(image))


def test_artifact_primary_and_cleanup_faults_are_both_preserved(tmp_path: Path, monkeypatch) -> None:
    roots, _store_value = _store(tmp_path)
    image = _squashfs()
    digest = f"sha256:{hashlib.sha256(image).hexdigest()}"
    artifacts = artifact_store_module.ArtifactStore(roots.store, repair_min_age_seconds=0)
    real_unlink = artifact_store_module.os.unlink

    def fail_temporary_unlink(name, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".oci-artifact-tmp-"):
            raise OSError("injected unlink fault")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(artifact_store_module.os, "unlink", fail_temporary_unlink)

    with pytest.raises(BaseExceptionGroup) as captured:
        artifacts.publish_squashfs(
            (b"x" * len(image),),
            expected_digest=digest,
            expected_size=len(image),
            maximum=len(image),
        )

    assert [getattr(error, "code", None) for error in captured.value.exceptions] == [
        "artifact-digest",
        "artifact-cleanup",
    ]


def test_stale_repair_preserves_fresh_then_removes_old_metadata_and_artifact_temps(tmp_path: Path) -> None:
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    now = time.time_ns()
    fresh_store = OCIStore(roots, repair_min_age_seconds=3600, wall_clock_ns=lambda: now)
    temporary_names: list[Path] = []
    for directory in ("records", "keys", "occurrences", "leases"):
        temporary = roots.oci_derived_store / directory / f".oci-derived-tmp-{uuid.uuid4().hex}"
        temporary.write_bytes(b"temporary")
        temporary.chmod(0o400)
        temporary_names.append(temporary)
    image = _squashfs()
    image_digest = f"sha256:{hashlib.sha256(image).hexdigest()}"
    artifact_temp = (
        roots.store
        / "blobs"
        / "sha256"
        / f".oci-artifact-tmp-{image_digest.removeprefix('sha256:')}-{uuid.uuid4().hex}"
    )
    artifact_temp.write_bytes(image)
    artifact_temp.chmod(0o400)
    temporary_names.append(artifact_temp)

    assert fresh_store.repair_stale_temporaries(minimum_age_seconds=0) == 0
    assert all(path.exists() for path in temporary_names)

    old_store = OCIStore(
        roots,
        repair_min_age_seconds=3600,
        wall_clock_ns=lambda: now + 7200 * 1_000_000_000,
    )
    assert old_store.repair_stale_temporaries(minimum_age_seconds=0) == len(temporary_names)
    assert not any(path.exists() for path in temporary_names)


def test_metadata_rename_fault_publishes_no_receipt_and_retry_converges(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    real_replace = oci_store_module.os.replace
    injected = False

    def fail_first_metadata_replace(*args, **kwargs):
        nonlocal injected
        if not injected and "dst_dir_fd" in kwargs and str(args[0]).startswith(".oci-derived-tmp-"):
            injected = True
            raise OSError("injected metadata rename fault")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(oci_store_module.os, "replace", fail_first_metadata_replace)
    with pytest.raises(OCIStoreError, match="publication failed"):
        store.materialize(occurrence, key, _producer(occurrence, [], image))
    assert list((roots.oci_derived_store / "occurrences").iterdir()) == []
    monkeypatch.setattr(oci_store_module.os, "replace", real_replace)

    receipt = store.materialize(occurrence, key, _producer(occurrence, [], image))
    assert receipt.key_digest == key.digest


def test_lock_acquire_fault_leaves_no_false_hit_and_retry_converges(tmp_path: Path, monkeypatch) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    real_flock = oci_store_module.fcntl.flock
    injected = False

    def fail_first_lock(fd, operation):
        nonlocal injected
        if not injected and operation == oci_store_module.fcntl.LOCK_EX:
            injected = True
            raise OSError("injected flock fault")
        return real_flock(fd, operation)

    monkeypatch.setattr(oci_store_module.fcntl, "flock", fail_first_lock)
    with pytest.raises(OCIStoreError, match="lock operation failed"):
        store.materialize(occurrence, key, _producer(occurrence, [], image))
    monkeypatch.setattr(oci_store_module.fcntl, "flock", real_flock)

    assert store.materialize(occurrence, key, _producer(occurrence, [], image)).key_digest == key.digest


def test_occurrence_parent_fsync_fault_returns_no_receipt_and_retry_converges(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    occurrence_directory = (roots.oci_derived_store / "occurrences").stat()
    target_identity = (occurrence_directory.st_dev, occurrence_directory.st_ino)
    real_fsync = oci_store_module.os.fsync
    injected = False

    def fail_occurrence_parent(fd):
        nonlocal injected
        opened = os.fstat(fd)
        if not injected and (opened.st_dev, opened.st_ino) == target_identity:
            injected = True
            raise OSError("injected occurrence parent fsync fault")
        return real_fsync(fd)

    monkeypatch.setattr(oci_store_module.os, "fsync", fail_occurrence_parent)
    with pytest.raises(OCIStoreError, match="publication failed"):
        store.materialize(occurrence, key, _producer(occurrence, [], image))
    monkeypatch.setattr(oci_store_module.os, "fsync", real_fsync)

    assert store.materialize(occurrence, key, _producer(occurrence, [], image)).key_digest == key.digest


def test_lease_release_unlink_fault_keeps_recoverable_record(tmp_path: Path, monkeypatch) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    lease = store.acquire_lease(receipt, owner)
    lease_id = lease.lease_id
    real_unlink = oci_store_module.os.unlink

    def fail_lease_unlink(name, *args, **kwargs):
        if name == lease_id:
            raise OSError("injected lease release fault")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(oci_store_module.os, "unlink", fail_lease_unlink)
    assert b"".join(lease.chunks()) == image
    with pytest.raises(OCIStoreError, match="release failed"):
        lease.close()
    monkeypatch.setattr(oci_store_module.os, "unlink", real_unlink)

    recovered = store.list_leases(owner)
    assert [item.lease_id for item in recovered] == [lease_id]
    resumed = store.resume_lease(lease_id, owner, receipt)
    with resumed:
        assert b"".join(resumed.chunks()) == image


def test_failed_recipe_producer_releases_waiter_for_successful_retry(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    key = _key(occurrence)
    image = _squashfs()
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(2)

    @contextmanager
    def flaky_producer():
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            time.sleep(0.03)
            raise OCIStoreError("oci-store-producer", "injected producer failure")
        intake, receipt = _receipts(occurrence, image)
        packed = LeasedSquashFS(io.BytesIO(image), receipt)
        try:
            yield intake, packed
        finally:
            packed._close()

    def invoke():
        start.wait()
        try:
            return store.materialize(occurrence, key, flaky_producer)
        except OCIStoreError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: invoke(), range(2)))

    assert calls == 2
    assert sorted(isinstance(result, str) for result in results) == [False, True]
