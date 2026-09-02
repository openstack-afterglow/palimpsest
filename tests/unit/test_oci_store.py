from __future__ import annotations

import gc
import hashlib
import io
import json
import multiprocessing
import os
import pickle
import struct
import subprocess
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
import palimpsest_local.oci_root_kvm as oci_root_kvm_module
import palimpsest_local.oci_root_prepare as oci_root_prepare_module
import palimpsest_local.oci_store as oci_store_module
import palimpsest_local.platforms as platforms
import palimpsest_local.state as state_module
from palimpsest_local.artifact_store import ArtifactStore, ArtifactStoreError
from palimpsest_local.errors import StateError
from palimpsest_local.oci_boot_plan import (
    OCIBootPlanIntent,
    PreparedOCIBootPlan,
    load_prepared_oci_boot_plan,
    prepare_oci_boot_plan,
    release_oci_boot_plan,
)
from palimpsest_local.oci_converter import (
    DEFAULT_LAYER_CONVERSION_LIMITS,
    LAYER_INTAKE_POLICY_ID,
    LayerIntakeReceipt,
)
from palimpsest_local.oci_guest_filesystems import ext4_primary_superblock_checksum
from palimpsest_local.oci_guest_stage1 import parse_guest_kernel_cmdline, verify_guest_stage1_transport
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.oci_materializer import OCIImageMaterializationReceipt
from palimpsest_local.oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    SQUASHFS_STRUCTURAL_VERIFIER_ID,
    LeasedSquashFS,
    PackedSquashFSReceipt,
    SquashFSToolchainIdentity,
    VerifiedSquashFSToolchain,
)
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
)
from palimpsest_local.oci_root_kvm import (
    build_oci_root_domain_plan,
    commit_oci_root_domain_plan,
    load_oci_root_domain_plan,
    verify_host_boot_artifacts,
)
from palimpsest_local.oci_root_prepare import (
    OCIRootPreparationTransaction,
    prepare_oci_root_run,
    reconcile_oci_root_preparation,
    release_prepared_oci_root_run,
)
from palimpsest_local.oci_root_volume import (
    OCIRootVolumeRecord,
    claim_oci_root_volume,
    load_oci_root_volume,
    oci_root_volume_label,
    release_oci_root_volume,
)
from palimpsest_local.oci_stage1 import OCIStage1Plan
from palimpsest_local.oci_store import (
    ArtifactLeaseOwner,
    DerivedLayerOccurrence,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStore,
    OCIStoreError,
)
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from palimpsest_local.state import StatePaths, init_resolved_roots, read_run_ledger_snapshot, reserve_new_run


def _digest(byte: str) -> str:
    return "sha256:" + byte * 64


def _squashfs(payload: bytes = b"payload" + b"\0" * 57, *, align: bool = True) -> bytes:
    bytes_used = 96 + len(payload)
    image_size = ((bytes_used + 511) // 512) * 512 if align else bytes_used
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
        bytes_used,
        144,
        2**64 - 1,
        96,
        112,
        2**64 - 1,
        2**64 - 1,
    )
    return superblock + payload + b"\0" * (image_size - bytes_used)


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


def test_materialization_rejects_structurally_valid_but_unaligned_squashfs(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs(align=False)
    assert len(image) % 512 != 0

    with pytest.raises(OCIStoreError, match="producer receipts are internally inconsistent"):
        store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))


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
        image_size=512,
    )

    with pytest.raises(OCIStoreError, match="cache result is invalid"):
        MaterializationResult(receipt, [])  # type: ignore[arg-type]

    wire = receipt.to_dict()
    wire["filesystem"] = "ext4"
    with pytest.raises(OCIStoreError, match="derived receipt is invalid"):
        oci_store_module.DerivedLayerReceipt.from_dict(wire)


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


def test_durable_lease_can_detach_and_release_without_streaming(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    occurrence = _occurrence()
    image = _squashfs()
    receipt = store.materialize(occurrence, _key(occurrence), _producer(occurrence, [], image))
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")

    lease = store.acquire_lease(receipt, owner)
    lease_id = lease.detach()

    assert [item.lease_id for item in store.list_leases(owner)] == [lease_id]
    store.release_recoverable_lease(lease_id, owner, receipt)
    assert store.list_leases(owner) == ()


def test_legacy_v1_durable_lease_is_discoverable_and_releasable(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    occurrence = _occurrence()
    receipt = store.materialize(
        occurrence,
        _key(occurrence),
        _producer(occurrence, [], _squashfs()),
    )
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-layer")
    lease = store.acquire_lease(receipt, owner)
    lease_id = lease.detach()
    path = roots.oci_derived_store / "leases" / lease_id
    value = json.loads(path.read_bytes())
    value["schema"] = oci_store_module._DERIVED_LEASE_SCHEMA_V1
    value["receipt"].pop("filesystem")
    path.chmod(0o600)
    path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    path.chmod(0o400)

    recovered = store.list_leases(owner)

    assert len(recovered) == 1
    assert recovered[0].receipt.filesystem == "squashfs"
    store.release_recoverable_lease(lease_id, owner, recovered[0].receipt)
    assert store.list_leases(owner) == ()


def _ordered_receipts(store: OCIStore, count: int = 3):
    image = _squashfs()
    receipts = []
    for ordinal in range(count):
        occurrence = _occurrence(ordinal)
        result = store.materialize_observed(
            occurrence,
            _key(occurrence),
            _producer(occurrence, [], image),
        )
        receipts.append(result.receipt)
    return image, tuple(receipts)


def test_lease_set_is_ordered_idempotent_and_retains_shared_artifact(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    plan_digest = _digest("9")

    first = store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)
    second = store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)

    assert second == first
    assert tuple(member.ordinal for member in first.members) == (0, 1, 2)
    assert tuple(member.receipt for member in first.members) == receipts
    assert len({member.lease_id for member in first.members}) == 3
    assert len(list((roots.oci_derived_store / "leases").iterdir())) == 3
    assert store.load_lease_set(first.lease_set_id, owner, plan_digest=plan_digest) == first
    assert store.list_lease_set_intents(owner)[0].complete
    with pytest.raises(OCIStoreError, match="retained by a durable OCI lease"):
        ArtifactStore(roots.store).delete_blob(
            receipts[0].image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipts[0].image_digest),
        )

    store.release_lease_set(first)

    assert list((roots.oci_derived_store / "leases").iterdir()) == []
    assert list((roots.oci_derived_store / "lease-sets").iterdir()) == []
    assert ArtifactStore(roots.store).delete_blob(
        receipts[0].image_digest,
        retention_guard=lambda: store.assert_artifact_unleased(receipts[0].image_digest),
    ) == len(image)


def test_legacy_v1_lease_set_is_recoverable_and_releasable(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    plan_digest = _digest("9")
    legacy_schema = oci_store_module._DERIVED_LEASE_SET_SCHEMA_V1
    intent = store._lease_set_intent_value(receipts, owner, plan_digest, schema=legacy_schema)
    set_path = roots.oci_derived_store / "lease-sets" / intent["lease_set_id"].removeprefix("sha256:")
    set_path.write_bytes(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())
    set_path.chmod(0o400)
    for member, receipt in zip(intent["members"][:1], receipts[:1], strict=True):
        lease_value = store._lease_record_value(member["lease_id"], owner, receipt, 1)
        lease_value["schema"] = oci_store_module._DERIVED_LEASE_SCHEMA_V1
        lease_value["receipt"].pop("filesystem")
        lease_path = roots.oci_derived_store / "leases" / member["lease_id"]
        lease_path.write_bytes(json.dumps(lease_value, sort_keys=True, separators=(",", ":")).encode())
        lease_path.chmod(0o400)

    recoverable = store.list_lease_set_intents(owner)
    reacquired = store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)
    loaded = store.load_lease_set(intent["lease_set_id"], owner, plan_digest=plan_digest)

    assert len(recoverable) == 1 and not recoverable[0].complete
    assert reacquired == loaded
    assert reacquired.lease_set_id == intent["lease_set_id"]
    assert len(list((roots.oci_derived_store / "lease-sets").iterdir())) == 1
    assert len(list((roots.oci_derived_store / "leases").iterdir())) == len(receipts)
    assert {member.receipt.filesystem for member in loaded.members} == {"squashfs"}
    store.release_lease_set(loaded)
    assert list((roots.oci_derived_store / "leases").iterdir()) == []
    assert list((roots.oci_derived_store / "lease-sets").iterdir()) == []


def test_partial_lease_set_publication_retry_converges(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    plan_digest = _digest("9")
    expected_id = store.lease_set_id(receipts, owner, plan_digest=plan_digest)
    real_publish = store._publish_file
    lease_publications = 0

    def fail_second_member(authority, directory_fd, name, payload, **kwargs):
        nonlocal lease_publications
        if json.loads(payload).get("schema") == oci_store_module.DERIVED_LEASE_SCHEMA:
            lease_publications += 1
            if lease_publications == 2:
                raise OCIStoreError("injected-crash", "partial lease-set publication")
        return real_publish(authority, directory_fd, name, payload, **kwargs)

    monkeypatch.setattr(store, "_publish_file", fail_second_member)
    with pytest.raises(OCIStoreError, match="partial lease-set"):
        store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)

    assert len(list((roots.oci_derived_store / "lease-sets").iterdir())) == 1
    assert len(list((roots.oci_derived_store / "leases").iterdir())) == 1
    monkeypatch.setattr(store, "_publish_file", real_publish)

    recovered = store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)
    assert recovered.lease_set_id == expected_id
    assert len(recovered.members) == 3
    store.release_lease_set(recovered)


def test_intent_only_crash_still_retains_every_planned_artifact(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    plan_digest = _digest("9")
    lease_set_id = store.lease_set_id(receipts, owner, plan_digest=plan_digest)
    real_publish = store._publish_file

    def fail_first_member(authority, directory_fd, name, payload, **kwargs):
        if json.loads(payload).get("schema") == oci_store_module.DERIVED_LEASE_SCHEMA:
            raise OCIStoreError("injected-crash", "intent-only lease-set publication")
        return real_publish(authority, directory_fd, name, payload, **kwargs)

    monkeypatch.setattr(store, "_publish_file", fail_first_member)
    with pytest.raises(OCIStoreError, match="intent-only"):
        store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)
    assert len(list((roots.oci_derived_store / "leases").iterdir())) == 0
    with pytest.raises(OCIStoreError, match="durable OCI lease set"):
        ArtifactStore(roots.store).delete_blob(
            receipts[0].image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipts[0].image_digest),
        )

    monkeypatch.setattr(store, "_publish_file", real_publish)
    store.rollback_lease_set(lease_set_id, owner, plan_digest=plan_digest)


def test_lease_set_acquisition_linearizes_before_physical_delete(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store, 2)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    physical = ArtifactStore(roots.store)
    publishing_intent = threading.Event()
    allow_publish = threading.Event()
    delete_attempted = threading.Event()
    outcomes: list[str] = []
    acquired = []
    real_publish = store._publish_file
    real_delete_lock = physical._digest_lock

    def delayed_publish(authority, directory_fd, name, payload, **kwargs):
        if json.loads(payload).get("schema") == oci_store_module.DERIVED_LEASE_SET_SCHEMA:
            publishing_intent.set()
            assert allow_publish.wait(2)
        return real_publish(authority, directory_fd, name, payload, **kwargs)

    @contextmanager
    def observed_delete_lock(authority, digest_hex):
        delete_attempted.set()
        with real_delete_lock(authority, digest_hex):
            yield

    monkeypatch.setattr(store, "_publish_file", delayed_publish)
    monkeypatch.setattr(physical, "_digest_lock", observed_delete_lock)

    acquire_thread = threading.Thread(
        target=lambda: acquired.append(store.acquire_lease_set(receipts, owner, plan_digest=_digest("9")))
    )

    def delete() -> None:
        try:
            physical.delete_blob(
                receipts[0].image_digest,
                retention_guard=lambda: store.assert_artifact_unleased(receipts[0].image_digest),
            )
        except OCIStoreError as exc:
            outcomes.append(exc.code)

    delete_thread = threading.Thread(target=delete)
    acquire_thread.start()
    assert publishing_intent.wait(2)
    delete_thread.start()
    assert delete_attempted.wait(2)
    assert delete_thread.is_alive()
    allow_publish.set()
    acquire_thread.join(2)
    delete_thread.join(2)

    assert outcomes == ["oci-store-in-use"]
    assert len(acquired) == 1
    store.release_lease_set(acquired[0])


def test_partial_lease_set_can_be_rolled_back_from_durable_intent(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    plan_digest = _digest("9")
    lease_set_id = store.lease_set_id(receipts, owner, plan_digest=plan_digest)
    real_publish = store._publish_file
    lease_publications = 0

    def fail_second_member(authority, directory_fd, name, payload, **kwargs):
        nonlocal lease_publications
        if json.loads(payload).get("schema") == oci_store_module.DERIVED_LEASE_SCHEMA:
            lease_publications += 1
            if lease_publications == 2:
                raise OCIStoreError("injected-crash", "partial lease-set publication")
        return real_publish(authority, directory_fd, name, payload, **kwargs)

    monkeypatch.setattr(store, "_publish_file", fail_second_member)
    with pytest.raises(OCIStoreError, match="partial lease-set"):
        store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)
    monkeypatch.setattr(store, "_publish_file", real_publish)

    recoverable = store.list_lease_set_intents(owner)
    assert len(recoverable) == 1
    assert recoverable[0].lease_set_id == lease_set_id
    assert recoverable[0].present_lease_ids == recoverable[0].member_lease_ids[:1]
    assert not recoverable[0].complete

    store.rollback_lease_set(lease_set_id, owner, plan_digest=plan_digest)

    assert list((roots.oci_derived_store / "leases").iterdir()) == []
    assert list((roots.oci_derived_store / "lease-sets").iterdir()) == []


def test_interrupted_lease_set_release_retry_converges(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    lease_set = store.acquire_lease_set(receipts, owner, plan_digest=_digest("9"))
    fail_id = lease_set.members[1].lease_id
    real_unlink = oci_store_module.os.unlink
    injected = False

    def fail_one_member(name, *args, **kwargs):
        nonlocal injected
        if name == fail_id and not injected:
            injected = True
            raise OSError("injected lease-set release fault")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(oci_store_module.os, "unlink", fail_one_member)
    with pytest.raises(OCIStoreError, match="member release failed"):
        store.release_lease_set(lease_set)
    assert len(list((roots.oci_derived_store / "lease-sets").iterdir())) == 1
    monkeypatch.setattr(oci_store_module.os, "unlink", real_unlink)

    store.release_lease_set(lease_set)
    assert list((roots.oci_derived_store / "leases").iterdir()) == []
    assert list((roots.oci_derived_store / "lease-sets").iterdir()) == []


def test_intent_unlink_fsync_failure_is_terminally_idempotent(tmp_path: Path, monkeypatch) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    lease_set = store.acquire_lease_set(receipts, owner, plan_digest=_digest("9"))
    lease_sets_stat = (roots.oci_derived_store / "lease-sets").stat()
    target_identity = (lease_sets_stat.st_dev, lease_sets_stat.st_ino)
    real_fsync = oci_store_module.os.fsync
    injected = False

    def fail_final_intent_fsync(fd):
        nonlocal injected
        opened = os.fstat(fd)
        if not injected and (opened.st_dev, opened.st_ino) == target_identity:
            injected = True
            raise OSError("injected final intent fsync fault")
        return real_fsync(fd)

    monkeypatch.setattr(oci_store_module.os, "fsync", fail_final_intent_fsync)
    with pytest.raises(OCIStoreError, match="intent release failed"):
        store.release_lease_set(lease_set)
    monkeypatch.setattr(oci_store_module.os, "fsync", real_fsync)

    store.release_lease_set(lease_set)
    store.release_lease_set(lease_set)
    assert store.list_lease_set_intents(owner) == ()


def test_malformed_lease_set_intent_fails_closed(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    _image, receipts = _ordered_receipts(store)
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), "demo", "root-lower")
    plan_digest = _digest("9")
    lease_set = store.acquire_lease_set(receipts, owner, plan_digest=plan_digest)
    intent_path = roots.oci_derived_store / "lease-sets" / lease_set.lease_set_id.removeprefix("sha256:")
    original = intent_path.read_bytes()
    malformed = json.loads(original)
    malformed["members"][0]["ordinal"] = 1
    intent_path.chmod(0o600)
    intent_path.write_bytes(json.dumps(malformed, sort_keys=True, separators=(",", ":")).encode())
    intent_path.chmod(0o400)

    with pytest.raises(OCIStoreError, match="member order is invalid"):
        store.load_lease_set(lease_set.lease_set_id, owner, plan_digest=plan_digest)
    with pytest.raises(OCIStoreError, match="lease-set retention is malformed"):
        ArtifactStore(roots.store).delete_blob(
            receipts[0].image_digest,
            retention_guard=lambda: store.assert_artifact_unleased(receipts[0].image_digest),
        )
    assert len(list((roots.oci_derived_store / "leases").iterdir())) == 3

    intent_path.chmod(0o600)
    intent_path.write_bytes(original)
    intent_path.chmod(0o400)
    store.release_lease_set(lease_set)


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
    for directory in ("records", "keys", "occurrences", "leases", "lease-sets"):
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


def _image_materialization(store: OCIStore) -> OCIImageMaterializationReceipt:
    _image, receipts = _ordered_receipts(store)
    layers = tuple(Descriptor(OCI_LAYER_MEDIA_TYPE, _digest("c"), 123) for _receipt in receipts)
    return OCIImageMaterializationReceipt(
        source_snapshot_binding_digest=receipts[0].source_snapshot_binding_digest,
        source_image_digest=receipts[0].source_image_digest,
        root_descriptor=Descriptor(OCI_IMAGE_MANIFEST_MEDIA_TYPE, _digest("2"), 512),
        manifest_digest=_digest("2"),
        config_descriptor=Descriptor(OCI_IMAGE_CONFIG_MEDIA_TYPE, _digest("3"), 256),
        platform_os="linux",
        platform_architecture="amd64",
        layer_descriptors=layers,
        layer_diff_ids=tuple(_digest("d") for _receipt in receipts),
        process=OCIProcessSpec(
            ("/sbin/init",), (("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),), "/", OCIUserSpec("0", "0"), 15
        ),
        results=tuple(MaterializationResult(receipt, "warm_hit") for receipt in receipts),
    )


def test_boot_plan_is_path_free_ordered_and_recoverable(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    run_id = str(uuid.uuid4())

    prepared = prepare_oci_boot_plan(materialization, run_id=run_id, run_name="demo", store=store)
    recovered = load_prepared_oci_boot_plan(prepared.intent, store)
    encoded = json.dumps(prepared.to_dict(), sort_keys=True)

    assert recovered == prepared
    assert prepared.intent.owner == ArtifactLeaseOwner(run_id, "demo", "root-lower")
    assert tuple(member.ordinal for member in prepared.lower_leases.members) == (0, 1, 2)
    assert prepared.intent.to_dict()["phase"] == "lower-reserved"
    assert prepared.intent.to_dict()["writable_root_policy"] == "vm-specific"
    assert str(tmp_path) not in encoded
    assert "/blobs/" not in encoded and "/lease-sets/" not in encoded

    release_oci_boot_plan(prepared, store)
    assert list((roots.oci_derived_store / "leases").iterdir()) == []
    assert list((roots.oci_derived_store / "lease-sets").iterdir()) == []


def test_boot_plan_digest_and_lease_set_are_deterministic(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    run_id = str(uuid.uuid4())
    first_intent = OCIBootPlanIntent(run_id, "demo", materialization)
    second_intent = OCIBootPlanIntent(run_id, "demo", materialization)

    first = prepare_oci_boot_plan(materialization, run_id=run_id, run_name="demo", store=store)
    second = prepare_oci_boot_plan(materialization, run_id=run_id, run_name="demo", store=store)

    assert first_intent.digest == second_intent.digest == first.intent.digest
    assert first == second
    release_oci_boot_plan(first, store)


def test_lower_graph_digest_ignores_invocation_local_cache_results(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    changed_results = tuple(
        replace(result, cache_result="cold_miss" if result.cache_result == "warm_hit" else "warm_hit")
        for result in materialization.results
    )
    changed = replace(materialization, results=changed_results)

    first = OCIBootPlanIntent(str(uuid.uuid4()), "first", materialization)
    second = OCIBootPlanIntent(str(uuid.uuid4()), "second", changed)

    assert first.lower_graph_dict() == second.lower_graph_dict()
    assert first.lower_graph_digest == second.lower_graph_digest
    assert first.digest != second.digest


def test_boot_plan_rejects_image_without_a_process(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    materialization = replace(_image_materialization(store), process=OCIProcessSpec.empty())

    with pytest.raises(OCIStoreError, match="process is not bootable"):
        OCIBootPlanIntent(str(uuid.uuid4()), "demo", materialization)


def test_prepared_boot_plan_rejects_owner_rebinding(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    prepared = prepare_oci_boot_plan(
        materialization,
        run_id=str(uuid.uuid4()),
        run_name="demo",
        store=store,
    )
    foreign_owner = ArtifactLeaseOwner(str(uuid.uuid4()), "other", "root-lower")

    with pytest.raises(OCIStoreError, match="lease binding is invalid"):
        PreparedOCIBootPlan(prepared.intent, replace(prepared.lower_leases, owner=foreign_owner))

    release_oci_boot_plan(prepared, store)


def test_boot_plan_rejects_materialization_metadata_rebinding(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    forged = replace(
        materialization,
        layer_diff_ids=(_digest("e"), *materialization.layer_diff_ids[1:]),
    )

    with pytest.raises(OCIStoreError, match="receipt occurrence binding is invalid"):
        prepare_oci_boot_plan(
            forged,
            run_id=str(uuid.uuid4()),
            run_name="demo",
            store=store,
        )


_ROOT_VOLUME_SIZE = 16 * 1024 * 1024


class _RootVolumeTools:
    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv == ["qemu-img", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "qemu-img 9.2\n", "")
        if argv == ["mkfs.ext4", "-V"]:
            return subprocess.CompletedProcess(argv, 0, "mke2fs 1.47\n", "")
        if argv[0] == "mkfs.ext4":
            path = Path(argv[-1])
            label = argv[argv.index("-L") + 1]
            filesystem_uuid = argv[argv.index("-U") + 1] if "-U" in argv else str(uuid.UUID(int=0))
            superblock = bytearray(1024)
            superblock[0:4] = (4096).to_bytes(4, "little")
            superblock[4:8] = (_ROOT_VOLUME_SIZE // 4096).to_bytes(4, "little")
            superblock[24:28] = (2).to_bytes(4, "little")
            superblock[32:36] = (32768).to_bytes(4, "little")
            superblock[40:44] = (4096).to_bytes(4, "little")
            superblock[56:58] = b"\x53\xef"
            superblock[76:80] = (1).to_bytes(4, "little")
            superblock[88:90] = (256).to_bytes(2, "little")
            superblock[96:100] = (0x42).to_bytes(4, "little")
            superblock[100:104] = (0x400).to_bytes(4, "little")
            superblock[104:120] = uuid.UUID(filesystem_uuid).bytes
            superblock[120:136] = label.encode("ascii").ljust(16, b"\0")
            superblock[0x175] = 1
            superblock[1020:1024] = ext4_primary_superblock_checksum(bytes(superblock)).to_bytes(4, "little")
            with path.open("r+b") as stream:
                stream.seek(1024)
                stream.write(superblock)
                stream.flush()
                os.fsync(stream.fileno())
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["qemu-img", "info", "--output=json"]:
            path = Path(argv[-1])
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"format": "raw", "virtual-size": path.stat().st_size}),
                "",
            )
        raise AssertionError(f"unexpected command: {argv}")


def _oci_dispatch() -> DispatchKey:
    return DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)


def test_oci_root_prepare_commits_path_free_ready_ledger_and_recovers(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()

    with reserve_new_run(roots, "oci-demo", _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            materialization,
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            runner=tools,
        )

    snapshot = read_run_ledger_snapshot(roots, "oci-demo")
    encoded = (roots.runs / "oci-demo" / "state.json").read_text(encoding="utf-8")
    recovered = reconcile_oci_root_preparation(roots, "oci-demo", store, runner=tools)

    assert snapshot.state["status"] == "creating"
    assert snapshot.state["lifecycle_revision"] == 2
    assert snapshot.state["oci_root"]["phase"] == "resources-ready"
    assert str(tmp_path) not in encoded
    assert recovered.transaction == prepared.transaction
    assert recovered.lower_leases == prepared.boot_plan.lower_leases
    assert recovered.root_volume == prepared.root_volume.record

    release_prepared_oci_root_run(roots, prepared, store, runner=tools)
    assert not prepared.root_volume.path.exists()
    assert store.list_lease_set_intents(prepared.transaction.owner) == ()


def test_oci_root_kvm_domain_plan_is_path_free_ordered_and_durable(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    tools = _RootVolumeTools()
    kernel = tmp_path / "vmlinuz"
    kernel_bytes = bytearray(0x206)
    kernel_bytes[0x202:0x206] = b"HdrS"
    kernel.write_bytes(kernel_bytes)
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"\x1f\x8bpayload")
    boot = verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())

    with reserve_new_run(roots, "domain-plan", _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            _image_materialization(store),
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            runner=tools,
        )
    resolved = build_oci_root_domain_plan(
        roots,
        prepared,
        store,
        boot,
        platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64"),
        memory_mib=2048,
        vcpus=2,
        runner=tools,
    )
    plan = commit_oci_root_domain_plan(roots, resolved, store, runner=tools)
    encoded = (roots.runs / "domain-plan" / "state.json").read_text(encoding="utf-8")

    assert load_oci_root_domain_plan(roots, "domain-plan") == plan
    assert str(tmp_path) not in json.dumps(plan.to_dict(), sort_keys=True)
    assert str(tmp_path) not in encoded
    assert "stage1-plan.raw" not in encoded
    assert [layer["ordinal"] for layer in plan.layers] == [0, 1, 2]
    assert len({layer["serial"] for layer in plan.layers}) == 3
    assert len({layer["image_digest"] for layer in plan.layers}) == 1
    assert [layer["target"] for layer in plan.layers] == ["vdc", "vdd", "vde"]
    assert "<kernel>" in resolved.xml and "<initrd>" in resolved.xml
    assert 'type="raw"' in resolved.xml and 'device="cdrom"' not in resolved.xml
    assert "palimpsest.root=virtio-" in plan.kernel_cmdline
    assert f"palimpsest.resource={prepared.transaction.boot_plan_digest}" in plan.kernel_cmdline
    assert f"palimpsest.core={plan.domain_core_digest}" in plan.kernel_cmdline
    assert f"palimpsest.stage1={plan.stage1_transport['artifact_digest']}" in plan.kernel_cmdline
    assert plan.stage1_transport["target"] == "vdb"
    transport_path = roots.runs / "domain-plan" / "stage1-plan.raw"
    assert transport_path.is_file()
    assert transport_path.stat().st_mode & 0o777 == 0o400
    assert {layer["filesystem"] for layer in plan.layers} == {"squashfs"}

    stage1 = OCIStage1Plan.from_domain_plan(plan)
    assert OCIStage1Plan.from_dict(stage1.to_dict(), expected_domain_plan=plan) == stage1
    guest_bindings = parse_guest_kernel_cmdline(plan.kernel_cmdline)
    guest_verified = verify_guest_stage1_transport(transport_path.read_bytes(), guest_bindings)
    assert guest_verified.plan == stage1
    assert stage1.process.argv == ("/sbin/init",)
    assert stage1.domain_core_digest == plan.domain_core_digest
    assert "domain_plan_digest" not in stage1.to_dict()
    assert stage1.to_dict()["assembly"]["lowerdir_ordinals"] == [2, 1, 0]
    assert str(tmp_path) not in json.dumps(stage1.to_dict(), sort_keys=True)

    tampered = deepcopy(plan.to_dict())
    tampered["layers"][0]["serial"] = "0" * 20
    with pytest.raises(StateError, match="order or identity"):
        type(plan).from_dict(tampered)
    tampered = deepcopy(plan.to_dict())
    tampered["stage1_transport"]["artifact_digest"] = "sha256:" + "0" * 64
    with pytest.raises(StateError, match="transport"):
        type(plan).from_dict(tampered)
    with pytest.raises(TypeError):
        plan.layers[0]["serial"] = "0" * 20
    with pytest.raises(TypeError):
        stage1.root["serial"] = "0" * 20

    stage1_wire = stage1.to_dict()
    stage1_wire["assembly"]["layers"][0]["filesystem"] = "ext4"
    with pytest.raises(StateError, match="lower mount policy"):
        OCIStage1Plan.from_dict(stage1_wire, expected_domain_plan=plan)

    stage1_wire = stage1.to_dict()
    stage1_wire["assembly"]["lowerdir_ordinals"] = [0, 1, 2]
    with pytest.raises(StateError, match="policy"):
        OCIStage1Plan.from_dict(stage1_wire, expected_domain_plan=plan)

    stage1_wire = stage1.to_dict()
    stage1_wire["process"]["argv"] = []
    with pytest.raises(StateError, match="not bootable"):
        OCIStage1Plan.from_dict(stage1_wire, expected_domain_plan=plan)

    stage1_wire = stage1.to_dict()
    original_serial = stage1_wire["assembly"]["root"]["serial"]
    stage1_wire["assembly"]["root"]["serial"] = "f" * 20 if original_serial != "f" * 20 else "e" * 20
    with pytest.raises(StateError, match="root mount policy"):
        OCIStage1Plan.from_dict(stage1_wire, expected_domain_plan=plan)

    corrupted_transport = bytearray(transport_path.read_bytes())
    corrupted_transport[64] ^= 1
    transport_path.chmod(0o600)
    transport_path.write_bytes(corrupted_transport)
    transport_path.chmod(0o400)
    with pytest.raises(StateError, match="stage-1 transport"):
        load_oci_root_domain_plan(roots, "domain-plan")

    release_prepared_oci_root_run(roots, prepared, store, runner=tools)


def test_oci_root_kvm_domain_plan_rejects_foreign_root_binding(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    tools = _RootVolumeTools()
    kernel = tmp_path / "vmlinuz"
    kernel_bytes = bytearray(0x206)
    kernel_bytes[0x202:0x206] = b"HdrS"
    kernel.write_bytes(kernel_bytes)
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"070701payload")
    boot = verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())

    with reserve_new_run(roots, "domain-tamper", _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            _image_materialization(store),
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            runner=tools,
        )
    stem = prepared.transaction.volume_id.replace("-", "")
    record_path = roots.oci_root_volumes / f"{stem}.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    raw["attached_run_name"] = "foreign"
    record_path.chmod(0o600)
    record_path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    record_path.chmod(0o600)

    with pytest.raises(StateError, match="root volume binding"):
        build_oci_root_domain_plan(
            roots,
            prepared,
            store,
            boot,
            platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64"),
            runner=tools,
        )


def test_oci_root_domain_transport_commit_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store = _store(tmp_path)
    tools = _RootVolumeTools()
    kernel = tmp_path / "vmlinuz"
    kernel_bytes = bytearray(0x206)
    kernel_bytes[0x202:0x206] = b"HdrS"
    kernel.write_bytes(kernel_bytes)
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"070701payload")
    boot = verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())
    with reserve_new_run(roots, "transport-retry", _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            _image_materialization(store),
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            runner=tools,
        )
    resolved = build_oci_root_domain_plan(
        roots,
        prepared,
        store,
        boot,
        platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64"),
        runner=tools,
    )

    original_verify = oci_root_kvm_module.verify_stage1_transport_file

    def fail_verify(*_args: object, **_kwargs: object) -> None:
        raise StateError("injected stage-1 transport verification failure")

    monkeypatch.setattr(oci_root_kvm_module, "verify_stage1_transport_file", fail_verify)
    with pytest.raises(StateError, match="injected"):
        commit_oci_root_domain_plan(roots, resolved, store, runner=tools)
    assert "oci_root_domain" not in read_run_ledger_snapshot(roots, "transport-retry").state
    assert (roots.runs / "transport-retry" / "stage1-plan.raw").is_file()

    monkeypatch.setattr(oci_root_kvm_module, "verify_stage1_transport_file", original_verify)
    plan = commit_oci_root_domain_plan(roots, resolved, store, runner=tools)
    assert load_oci_root_domain_plan(roots, "transport-retry") == plan


def test_oci_root_prepare_retains_and_reuses_exact_root_volume(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()

    with reserve_new_run(roots, "first", _oci_dispatch()) as reservation:
        first = prepare_oci_root_run(
            reservation,
            materialization,
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            retention_policy="retain",
            runner=tools,
        )
    volume_id = first.transaction.volume_id
    release_prepared_oci_root_run(roots, first, store, runner=tools)
    retained = load_oci_root_volume(roots, volume_id, runner=tools).record
    assert retained.status == "retained"

    with reserve_new_run(roots, "second", _oci_dispatch()) as reservation:
        second = prepare_oci_root_run(
            reservation,
            materialization,
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            retained_volume_id=volume_id,
            retention_policy="retain",
            runner=tools,
        )
    assert second.root_volume.claimed_from_retained is True
    assert second.root_volume.record.attached_run_name == "second"
    assert second.transaction.lower_graph_digest == first.transaction.lower_graph_digest
    release_prepared_oci_root_run(roots, second, store, runner=tools)


def test_oci_root_prepare_rolls_back_lowers_when_volume_claim_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()

    def fail_claim(*_args: object, **_kwargs: object) -> None:
        raise StateError("injected volume claim failure")

    monkeypatch.setattr(oci_root_prepare_module, "claim_oci_root_volume", fail_claim)
    with pytest.raises(StateError, match="injected volume claim failure"):
        with reserve_new_run(roots, "claim-failure", _oci_dispatch()) as reservation:
            prepare_oci_root_run(
                reservation,
                materialization,
                store,
                root_volume_size_bytes=_ROOT_VOLUME_SIZE,
                runner=tools,
            )

    snapshot = read_run_ledger_snapshot(roots, "claim-failure")
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root"]["phase"] == "rolled-back"
    assert store.list_lease_set_intents() == ()


def test_release_required_reconcile_finishes_after_root_released_before_lower_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()
    with reserve_new_run(roots, "release-fault", _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            materialization,
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            runner=tools,
        )

    original_rollback = oci_root_prepare_module._rollback_lower

    def fail_lower(*_args: object, **_kwargs: object) -> None:
        raise OCIStoreError("oci-store-test", "injected lower release failure")

    monkeypatch.setattr(oci_root_prepare_module, "_rollback_lower", fail_lower)
    with pytest.raises(OCIStoreError, match="injected lower release failure"):
        release_prepared_oci_root_run(roots, prepared, store, runner=tools)
    assert not prepared.root_volume.path.exists()
    assert read_run_ledger_snapshot(roots, "release-fault").state["oci_root"]["phase"] == "release-required"

    monkeypatch.setattr(oci_root_prepare_module, "_rollback_lower", original_rollback)
    reconciled = reconcile_oci_root_preparation(roots, "release-fault", store, runner=tools)
    assert reconciled.transaction.phase == "released"
    assert read_run_ledger_snapshot(roots, "release-fault").state["status"] == "removed"
    assert store.list_lease_set_intents() == ()


def test_release_required_reconcile_accepts_already_retained_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store = _store(tmp_path)
    tools = _RootVolumeTools()
    with reserve_new_run(roots, "retain-release-fault", _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            _image_materialization(store),
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            retention_policy="retain",
            runner=tools,
        )
    original_rollback = oci_root_prepare_module._rollback_lower
    monkeypatch.setattr(
        oci_root_prepare_module,
        "_rollback_lower",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OCIStoreError("oci-store-test", "injected lower release failure")
        ),
    )
    with pytest.raises(OCIStoreError, match="injected lower release failure"):
        release_prepared_oci_root_run(roots, prepared, store, runner=tools)
    retained = load_oci_root_volume(roots, prepared.transaction.volume_id, runner=tools).record
    assert retained.status == "retained"

    monkeypatch.setattr(oci_root_prepare_module, "_rollback_lower", original_rollback)
    reconciled = reconcile_oci_root_preparation(roots, "retain-release-fault", store, runner=tools)
    assert reconciled.transaction.phase == "released"
    assert reconciled.root_volume is not None and reconciled.root_volume.status == "retained"


def test_reconcile_rolls_back_resources_acquired_after_planned_commit(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()

    with reserve_new_run(roots, "crashed", _oci_dispatch()) as reservation:
        intent = OCIBootPlanIntent(reservation.record.run_id, reservation.record.name, materialization)
        lease_set_id = store.lease_set_id(intent.receipts, intent.owner, plan_digest=intent.digest)
        transaction = OCIRootPreparationTransaction(
            "resources-planned",
            intent.to_dict(),
            intent.digest,
            lease_set_id,
            str(uuid.uuid4()),
            _ROOT_VOLUME_SIZE,
            intent.lower_graph_digest,
            "delete",
            "delete",
        )
        reservation.write_state("creating", {"created_at": "2026-09-01T00:00:00Z", "oci_root": transaction.to_dict()})
        prepared_lower = prepare_oci_boot_plan(
            materialization,
            run_id=reservation.record.run_id,
            run_name=reservation.record.name,
            store=store,
        )
        claimed = claim_oci_root_volume(
            roots,
            transaction.volume_id,
            size_bytes=_ROOT_VOLUME_SIZE,
            lower_graph_digest=intent.lower_graph_digest,
            retention_policy="delete",
            owner=intent.owner,
            runner=tools,
        )

    reconciled = reconcile_oci_root_preparation(roots, "crashed", store, runner=tools)
    snapshot = read_run_ledger_snapshot(roots, "crashed")

    assert reconciled.transaction.phase == "rolled-back"
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root"]["phase"] == "rolled-back"
    assert not claimed.path.exists()
    assert store.list_lease_set_intents(prepared_lower.intent.owner) == ()


def test_reconcile_removes_creating_hardlink_pair_after_publish_crash(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()
    with reserve_new_run(roots, "create-marker", _oci_dispatch()) as reservation:
        intent = OCIBootPlanIntent(reservation.record.run_id, reservation.record.name, materialization)
        transaction = OCIRootPreparationTransaction(
            "resources-planned",
            intent.to_dict(),
            intent.digest,
            store.lease_set_id(intent.receipts, intent.owner, plan_digest=intent.digest),
            str(uuid.uuid4()),
            _ROOT_VOLUME_SIZE,
            intent.lower_graph_digest,
            "delete",
            "delete",
        )
        reservation.write_state("creating", {"created_at": "2026-09-01T00:00:00Z", "oci_root": transaction.to_dict()})
        record = OCIRootVolumeRecord(
            transaction.volume_id,
            transaction.volume_size_bytes,
            transaction.lower_graph_digest,
            transaction.retention_policy,
            "creating",
            intent.owner.run_id,
            intent.owner.run_name,
            1,
        )
        record_path = roots.oci_root_volumes / f"{transaction.volume_id.replace('-', '')}.json"
        state_module.atomic_write_json(record_path, record.to_dict())
        stem = transaction.volume_id.replace("-", "")
        creating_path = roots.oci_root_volumes / f".{stem}-creating.raw"
        raw_path = roots.oci_root_volumes / f"{stem}.raw"
        with creating_path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.truncate(_ROOT_VOLUME_SIZE)
        tools(
            [
                "mkfs.ext4",
                "-F",
                "-q",
                "-L",
                oci_root_volume_label(transaction.volume_id),
                str(creating_path),
            ]
        )
        os.link(creating_path, raw_path)
        state_module.fsync_directory(roots.oci_root_volumes)
        assert raw_path.stat().st_nlink == creating_path.stat().st_nlink == 2

    reconciled = reconcile_oci_root_preparation(roots, "create-marker", store, runner=tools)
    assert reconciled.transaction.phase == "rolled-back"
    assert not record_path.exists()
    assert not raw_path.exists()
    assert not creating_path.exists()


def test_reconcile_restores_interrupted_retained_volume_claim(tmp_path: Path) -> None:
    roots, store = _store(tmp_path)
    materialization = _image_materialization(store)
    tools = _RootVolumeTools()
    original_owner = ArtifactLeaseOwner(str(uuid.uuid4()), "old", "root-lower")
    seed_intent = OCIBootPlanIntent(original_owner.run_id, original_owner.run_name, materialization)
    volume_id = str(uuid.uuid4())
    seed = claim_oci_root_volume(
        roots,
        volume_id,
        size_bytes=_ROOT_VOLUME_SIZE,
        lower_graph_digest=seed_intent.lower_graph_digest,
        retention_policy="retain",
        owner=original_owner,
        runner=tools,
    )
    release_oci_root_volume(
        roots,
        volume_id,
        owner=original_owner,
        lower_graph_digest=seed.record.lower_graph_digest,
        runner=tools,
    )

    with reserve_new_run(roots, "reuse-crash", _oci_dispatch()) as reservation:
        intent = OCIBootPlanIntent(reservation.record.run_id, reservation.record.name, materialization)
        lease_set_id = store.lease_set_id(intent.receipts, intent.owner, plan_digest=intent.digest)
        transaction = OCIRootPreparationTransaction(
            "resources-planned",
            intent.to_dict(),
            intent.digest,
            lease_set_id,
            volume_id,
            _ROOT_VOLUME_SIZE,
            intent.lower_graph_digest,
            "retain",
            "retain",
        )
        reservation.write_state("creating", {"created_at": "2026-09-01T00:00:00Z", "oci_root": transaction.to_dict()})
        prepare_oci_boot_plan(
            materialization,
            run_id=reservation.record.run_id,
            run_name=reservation.record.name,
            store=store,
        )
        claim_oci_root_volume(
            roots,
            volume_id,
            size_bytes=_ROOT_VOLUME_SIZE,
            lower_graph_digest=intent.lower_graph_digest,
            retention_policy="retain",
            owner=intent.owner,
            runner=tools,
        )

    reconcile_oci_root_preparation(roots, "reuse-crash", store, runner=tools)
    restored = load_oci_root_volume(roots, volume_id, runner=tools).record
    assert restored.status == "retained"
    assert restored.attached_run_id is None


def test_preparation_ledger_rejects_path_field_even_with_rehashed_plan(tmp_path: Path) -> None:
    _roots, store = _store(tmp_path)
    intent = OCIBootPlanIntent(str(uuid.uuid4()), "strict", _image_materialization(store))
    plan = intent.to_dict()
    plan["host_path"] = "/tmp/attacker-root.raw"
    forged_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    forged_digest = f"sha256:{hashlib.sha256(forged_bytes).hexdigest()}"

    with pytest.raises(StateError, match="boot plan fields"):
        OCIRootPreparationTransaction(
            "resources-planned",
            plan,
            forged_digest,
            _digest("9"),
            str(uuid.uuid4()),
            _ROOT_VOLUME_SIZE,
            intent.lower_graph_digest,
            "delete",
            "delete",
        )
