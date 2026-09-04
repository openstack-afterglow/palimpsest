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
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import palimpsest_local.artifact_store as artifact_store_module
import palimpsest_local.oci_root_kvm as oci_root_kvm_module
import palimpsest_local.oci_root_prepare as oci_root_prepare_module
import palimpsest_local.oci_root_runtime as oci_root_runtime_module
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
from palimpsest_local.oci_lifecycle_transport import OCILifecycleHandoffReceipt
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
from palimpsest_local.oci_root_runtime import define_committed_oci_root_domain, launch_defined_oci_root_domain
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
from palimpsest_local.runtime_types import (
    DispatchKey,
    ProcessExit,
    ProcessExitCategory,
    RuntimeBackend,
    RuntimeKind,
)
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


def _short_oci_store() -> tuple[StatePaths, OCIStore]:
    temporary = tempfile.TemporaryDirectory(prefix="p-", dir=Path("/tmp").resolve())
    roots, store = _store(Path(temporary.name))
    store._test_temporary_directory = temporary
    return roots, store


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


class _FakeLibvirtError(RuntimeError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self._code = code

    def get_error_code(self) -> int:
        return self._code


_FAKE_LIBVIRT = SimpleNamespace(
    libvirtError=_FakeLibvirtError,
    VIR_DOMAIN_XML_INACTIVE=2,
    VIR_ERR_NO_DOMAIN=42,
    VIR_STREAM_NONBLOCK=1,
)


class _DefinedDomain:
    def __init__(self, connection, name: str, xml: str, *, domain_uuid: str | None = None):
        self.connection = connection
        self.name = name
        self.xml = xml
        self.domain_uuid = domain_uuid or str(uuid.uuid4())
        self.active = 0
        self.domain_id = 7
        self.create_calls = 0
        self.destroy_calls = 0
        self.open_channel_calls: list[tuple[str, object, int]] = []
        self.xml_desc_flags: list[int | None] = []
        self.undefine_calls = 0

    def XMLDesc(self, flags: int | None = None) -> str:
        self.xml_desc_flags.append(flags)
        return self.xml

    def isActive(self) -> int:
        return self.active

    def UUIDString(self) -> str:
        return self.domain_uuid

    def ID(self) -> int:
        return self.domain_id if self.active == 1 else -1

    def create(self) -> None:
        self.create_calls += 1
        self.active = 1

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.connection.destroy_error is not None:
            raise self.connection.destroy_error
        self.active = 0

    def openChannel(self, name: str, stream: object, flags: int) -> int | None:
        self.open_channel_calls.append((name, stream, flags))
        if self.connection.open_channel_error is not None:
            raise self.connection.open_channel_error
        return self.connection.open_channel_result

    def undefine(self) -> None:
        self.undefine_calls += 1
        if self.connection.undefine_error is not None:
            raise self.connection.undefine_error
        if self.connection.domains.get(self.name) is self:
            del self.connection.domains[self.name]


class _DefinitionConnection:
    def __init__(
        self,
        *,
        transform=None,
        define_error: Exception | None = None,
        uri: str = "qemu:///system",
        rebind_uuid: bool = False,
        undefine_error: Exception | None = None,
        destroy_error: Exception | None = None,
        open_channel_error: Exception | None = None,
        open_channel_result: int | None = 0,
    ):
        self.domains: dict[str, _DefinedDomain] = {}
        self.transform = transform
        self.define_error = define_error
        self.uri = uri
        self.rebind_uuid = rebind_uuid
        self.undefine_error = undefine_error
        self.destroy_error = destroy_error
        self.open_channel_error = open_channel_error
        self.open_channel_result = open_channel_result
        self.lookup_error: Exception | None = None
        self.define_calls = 0
        self.stream = SimpleNamespace(
            send=lambda payload: len(payload),
            recv=lambda _size: -2,
            abort=lambda: None,
            free=lambda: None,
        )
        self.stream_flags: list[int] = []
        self.domain_capability_calls: list[tuple[str, str, str, str, int]] = []

    def getURI(self) -> str:
        return self.uri

    def getDomainCapabilities(self, emulator: str, arch: str, machine: str, domain: str, flags: int) -> str:
        self.domain_capability_calls.append((emulator, arch, machine, domain, flags))
        return (
            "<domainCapabilities>"
            f"<path>{emulator}</path><domain>{domain}</domain><machine>pc-q35-noble</machine><arch>{arch}</arch>"
            "</domainCapabilities>"
        )

    def lookupByName(self, name: str) -> _DefinedDomain:
        if self.lookup_error is not None:
            raise self.lookup_error
        try:
            return self.domains[name]
        except KeyError:
            raise _FakeLibvirtError("missing", 42) from None

    def lookupByUUIDString(self, domain_uuid: str) -> _DefinedDomain:
        for domain in self.domains.values():
            if domain.UUIDString() == domain_uuid:
                return domain
        raise _FakeLibvirtError("missing", 42)

    def newStream(self, flags: int) -> object:
        self.stream_flags.append(flags)
        return self.stream

    def defineXML(self, xml: str) -> _DefinedDomain:
        self.define_calls += 1
        root = ET.fromstring(xml)
        name = root.findtext("./name")
        assert name is not None
        actual = self.transform(xml) if self.transform is not None else xml
        domain = _DefinedDomain(self, name, actual)
        self.domains[name] = domain
        if self.define_error is not None:
            raise self.define_error
        if self.rebind_uuid:
            self.domains[name] = _DefinedDomain(self, name, actual)
        return domain


def _committed_oci_domain(tmp_path: Path, name: str):
    roots, store = _short_oci_store()
    tools = _RootVolumeTools()
    kernel = tmp_path / "vmlinuz"
    kernel_bytes = bytearray(0x206)
    kernel_bytes[0x202:0x206] = b"HdrS"
    kernel.write_bytes(kernel_bytes)
    initramfs = tmp_path / "initramfs"
    initramfs.write_bytes(b"070701payload")
    boot = verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())
    profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
    with reserve_new_run(roots, name, _oci_dispatch()) as reservation:
        prepared = prepare_oci_root_run(
            reservation,
            _image_materialization(store),
            store,
            root_volume_size_bytes=_ROOT_VOLUME_SIZE,
            runner=tools,
        )
    preview = build_oci_root_domain_plan(roots, prepared, store, boot, profile, runner=tools)
    plan = commit_oci_root_domain_plan(roots, preview, store, runner=tools)
    return roots, store, tools, boot, profile, prepared, plan


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


def test_oci_root_kvm_domain_plan_is_path_free_ordered_and_durable(tmp_path: Path, monkeypatch) -> None:
    roots, store = _short_oci_store()
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
    assert plan.to_dict()["lifecycle_control"] == {
        "channel_name": "org.palimpsest.oci.lifecycle.0",
        "endpoint": "run-private/lifecycle.sock",
        "protocol": "palimpsest.oci-lifecycle-control.v2",
        "transport": "virtio-serial",
    }
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
    assert stage1.to_dict()["protocol"] == "palimpsest.guest-stage1.v15"
    assert stage1.to_dict()["handoff"] == "first-party-pid1-supervisor.v9"
    assert stage1.to_dict()["isolation"] == "palimpsest.workload-lifecycle-authority-isolation.v3"
    assert str(tmp_path) not in json.dumps(stage1.to_dict(), sort_keys=True)

    tampered = deepcopy(plan.to_dict())
    tampered["layers"][0]["serial"] = "0" * 20
    with pytest.raises(StateError, match="order or identity"):
        type(plan).from_dict(tampered)
    tampered = deepcopy(plan.to_dict())
    tampered["stage1_transport"]["artifact_digest"] = "sha256:" + "0" * 64
    with pytest.raises(StateError, match="transport"):
        type(plan).from_dict(tampered)
    tampered = deepcopy(plan.to_dict())
    tampered["lifecycle_control"]["channel_name"] = "attacker.controlled"
    with pytest.raises(StateError, match="dispatch"):
        type(plan).from_dict(tampered)
    legacy = deepcopy(plan.to_dict())
    legacy["schema"] = "palimpsest.oci-root-domain-plan.v4"
    legacy.pop("lifecycle_control")
    with pytest.raises(StateError, match="pre-production.*v4.*rebuild"):
        type(plan).from_dict(legacy)
    obsolete = deepcopy(plan.to_dict())
    obsolete["schema"] = "palimpsest.oci-root-domain-plan.v5"
    with pytest.raises(StateError, match="pre-production.*v5.*rebuild"):
        type(plan).from_dict(obsolete)
    lifecycle_obsolete = deepcopy(plan.to_dict())
    lifecycle_obsolete["schema"] = "palimpsest.oci-root-domain-plan.v7"
    with pytest.raises(StateError, match="pre-production.*v7.*rebuild"):
        type(plan).from_dict(lifecycle_obsolete)
    define_obsolete = deepcopy(plan.to_dict())
    define_obsolete["schema"] = "palimpsest.oci-root-domain-plan.v8"
    define_obsolete["lifecycle_control"].pop("endpoint")
    with pytest.raises(StateError, match="pre-production.*v8.*rebuild"):
        type(plan).from_dict(define_obsolete)
    state_path = roots.runs / "domain-plan" / "state.json"
    state_before = state_path.read_bytes()
    transport_before = transport_path.read_bytes()
    snapshot = read_run_ledger_snapshot(roots, "domain-plan")
    legacy_snapshot = replace(
        snapshot,
        state={
            **snapshot.state,
            "oci_root_domain": {
                "digest": snapshot.state["oci_root_domain"]["digest"],
                "plan": lifecycle_obsolete,
            },
        },
    )
    with monkeypatch.context() as isolated:
        isolated.setattr(oci_root_kvm_module, "read_run_ledger_snapshot", lambda *_args: legacy_snapshot)
        with pytest.raises(StateError, match="pre-production.*v7.*rebuild"):
            load_oci_root_domain_plan(roots, "domain-plan")
    assert state_path.read_bytes() == state_before
    assert transport_path.read_bytes() == transport_before
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
    assert read_run_ledger_snapshot(roots, "domain-plan").state["status"] == "removed"


def test_oci_root_define_revalidates_and_durably_records_inactive_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store, tools, boot, profile, _prepared, plan = _committed_oci_domain(tmp_path, "define-ready")
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    receipt = define_committed_oci_root_domain(
        roots,
        "define-ready",
        store,
        boot,
        profile,
        conn=conn,
        runner=tools,
    )

    assert receipt.run_id == plan.run_id
    assert receipt.run_name == "define-ready"
    assert receipt.plan_digest == plan.digest
    assert receipt.domain_uuid == conn.domains["define-ready"].UUIDString()
    assert receipt.libvirt_uri == profile.uri
    assert conn.define_calls == 1
    assert conn.domains["define-ready"].isActive() == 0
    snapshot = read_run_ledger_snapshot(roots, "define-ready")
    assert snapshot.state["status"] == "defined"
    assert snapshot.state["oci_root_domain"]["digest"] == plan.digest
    assert snapshot.state["oci_root_definition"] == {
        "domain_uuid": receipt.domain_uuid,
        "libvirt_uri": profile.uri,
        "phase": "defined",
        "plan_digest": plan.digest,
        "projection_digest": oci_root_runtime_module._projection_digest(
            oci_root_runtime_module._domain_projection(conn.domains["define-ready"].xml)
        ),
        "schema": "palimpsest.oci-root-definition.v2",
    }


def test_oci_root_define_accepts_only_bounded_non_resource_libvirt_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, "safe-defaults")

    def safe_defaults(xml: str) -> str:
        root = ET.fromstring(xml)
        memory = root.find("./memory")
        assert memory is not None and memory.text is not None
        memory.set("unit", "KiB")
        memory.text = str(int(memory.text) * 1024)
        ET.SubElement(root, "currentMemory", dict(memory.attrib)).text = memory.text
        ET.SubElement(root, "clock", {"offset": "utc"})
        ET.SubElement(root, "on_poweroff").text = "destroy"
        ET.SubElement(root, "on_reboot").text = "restart"
        ET.SubElement(root, "on_crash").text = "destroy"
        pm = ET.SubElement(root, "pm")
        ET.SubElement(pm, "suspend-to-mem", {"enabled": "no"})
        ET.SubElement(pm, "suspend-to-disk", {"enabled": "no"})
        devices = root.find("./devices")
        ET.SubElement(devices, "controller", {"type": "pci", "index": "0", "model": "pcie-root"})
        serial = ET.SubElement(devices, "serial", {"type": "pty"})
        ET.SubElement(serial, "target", {"type": "isa-serial", "port": "0"})
        ET.SubElement(devices, "input", {"type": "keyboard", "bus": "ps2"})
        ET.SubElement(devices, "memballoon", {"model": "virtio"})
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=safe_defaults)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    receipt = define_committed_oci_root_domain(roots, "safe-defaults", store, boot, profile, conn=conn, runner=tools)

    assert receipt.domain_uuid == conn.domains["safe-defaults"].UUIDString()
    assert read_run_ledger_snapshot(roots, "safe-defaults").state["status"] == "defined"


def test_oci_root_disk_projection_binds_exact_dac_no_relabel_sources(tmp_path: Path) -> None:
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, "disk-source-seclabel")
    snapshot = read_run_ledger_snapshot(roots, "disk-source-seclabel")
    xml = oci_root_kvm_module.resolve_committed_oci_root_domain_plan(
        roots,
        snapshot,
        store,
        boot,
        profile,
        runner=tools,
    ).xml

    projection = oci_root_runtime_module._domain_projection(xml)
    disks = projection["disks"]
    assert disks
    assert all(disk[4] == (("model", "dac"), ("relabel", "no")) for disk in disks)

    root = ET.fromstring(xml)
    ET.indent(root)
    assert oci_root_runtime_module._domain_projection(ET.tostring(root, encoding="unicode")) == projection


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "duplicate",
        "wrong-model",
        "relabel-yes",
        "missing-attribute",
        "extra-attribute",
        "text",
        "child",
        "namespace",
        "unknown-source-child",
    ],
)
def test_oci_root_disk_projection_rejects_noncanonical_source_seclabel(
    tmp_path: Path,
    tamper: str,
) -> None:
    name = f"disk-source-seclabel-{tamper}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    snapshot = read_run_ledger_snapshot(roots, name)
    xml = oci_root_kvm_module.resolve_committed_oci_root_domain_plan(
        roots,
        snapshot,
        store,
        boot,
        profile,
        runner=tools,
    ).xml
    root = ET.fromstring(xml)
    source = root.find("./devices/disk/source")
    label = source.find("./seclabel") if source is not None else None
    assert source is not None and label is not None
    if tamper == "missing":
        source.remove(label)
    elif tamper == "duplicate":
        source.append(deepcopy(label))
    elif tamper == "wrong-model":
        label.set("model", "selinux")
    elif tamper == "relabel-yes":
        label.set("relabel", "yes")
    elif tamper == "missing-attribute":
        label.attrib.pop("relabel")
    elif tamper == "extra-attribute":
        label.set("label", "+0:+0")
    elif tamper == "text":
        label.text = "forbidden"
    elif tamper == "child":
        ET.SubElement(label, "attacker")
    elif tamper == "namespace":
        label.tag = "{https://attacker.invalid/domain/v1}seclabel"
    else:
        ET.SubElement(source, "attacker")

    with pytest.raises(StateError, match="disk projection"):
        oci_root_runtime_module._domain_projection(ET.tostring(root, encoding="unicode"))


def test_oci_root_definition_rejects_later_dac_no_relabel_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "disk-source-seclabel-drift"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    definition = read_run_ledger_snapshot(roots, name).state["oci_root_definition"]
    projection = oci_root_runtime_module._domain_projection(conn.domains[name].xml)
    assert definition["projection_digest"] == oci_root_runtime_module._projection_digest(projection)
    root = ET.fromstring(conn.domains[name].xml)
    source = root.find("./devices/disk/source")
    label = source.find("./seclabel") if source is not None else None
    assert source is not None and label is not None
    source.remove(label)
    conn.domains[name].xml = ET.tostring(root, encoding="unicode")

    with pytest.raises(StateError, match="disk projection"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    assert conn.domains[name].create_calls == 0


@pytest.mark.parametrize("removed_device", ["audio", "watchdog"])
def test_oci_root_define_records_safe_generated_devices_and_rejects_later_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    removed_device: str,
) -> None:
    name = f"generated-devices-remove-{removed_device}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def add_generated_devices(xml: str) -> str:
        root = ET.fromstring(xml)
        devices = root.find("./devices")
        assert devices is not None
        ET.SubElement(devices, "audio", {"id": "1", "type": "none"})
        ET.SubElement(devices, "watchdog", {"action": "reset", "model": "itco"})
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=add_generated_devices)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    definition = read_run_ledger_snapshot(roots, name).state["oci_root_definition"]
    projection = oci_root_runtime_module._domain_projection(conn.domains[name].xml)
    assert dict(projection["device_counts"])["audio"] == 1
    assert dict(projection["device_counts"])["watchdog"] == 1
    assert definition["projection_digest"] == oci_root_runtime_module._projection_digest(projection)

    root = ET.fromstring(conn.domains[name].xml)
    devices = root.find("./devices")
    removed = root.find(f"./devices/{removed_device}")
    assert devices is not None and removed is not None
    devices.remove(removed)
    conn.domains[name].xml = ET.tostring(root, encoding="unicode")
    with pytest.raises(StateError, match="changed after definition"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    assert conn.domains[name].create_calls == 0


@pytest.mark.parametrize(
    "tamper",
    [
        "duplicate",
        "non-none",
        "extra-attribute",
        "missing-id",
        "missing-type",
        "text",
        "child",
        "source",
    ],
)
@pytest.mark.parametrize("device", ["audio", "watchdog"])
def test_oci_root_define_rejects_noncanonical_safe_generated_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    device: str,
) -> None:
    name = f"generated-{device}-{tamper}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def add_bad_generated_device(xml: str) -> str:
        root = ET.fromstring(xml)
        devices = root.find("./devices")
        assert devices is not None
        canonical = {"id": "1", "type": "none"} if device == "audio" else {"action": "reset", "model": "itco"}
        generated = ET.SubElement(devices, device, canonical)
        if tamper == "duplicate":
            ET.SubElement(devices, device, canonical)
        elif tamper == "non-none":
            generated.set("type" if device == "audio" else "action", "spice" if device == "audio" else "shutdown")
        elif tamper == "extra-attribute":
            generated.set("extra", "forbidden")
        elif tamper == "missing-id":
            generated.attrib.pop("id" if device == "audio" else "action")
        elif tamper == "missing-type":
            generated.attrib.pop("type" if device == "audio" else "model")
        elif tamper == "text":
            generated.text = "forbidden"
        elif tamper == "child":
            ET.SubElement(generated, "attacker")
        else:
            ET.SubElement(generated, "source", {"path": "/tmp/attacker"})
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=add_bad_generated_device)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    with pytest.raises(StateError):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name not in conn.domains
    assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"


def test_oci_root_define_accepts_libvirt_elided_redundant_lifecycle_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, "elided-lifecycle")

    def elide_lifecycle(xml: str) -> str:
        root = ET.fromstring(xml)
        authored_projection = oci_root_runtime_module._domain_projection(xml)
        metadata = root.find("./metadata")
        lifecycle = root.find(f"./metadata/{{{oci_root_runtime_module.kvm.DOMAIN_MARKER_NAMESPACE}}}lifecycle")
        owner = root.find(f"./metadata/{{{oci_root_runtime_module.kvm.DOMAIN_MARKER_NAMESPACE}}}run")
        assert metadata is not None and lifecycle is not None and owner is not None
        metadata.text = "\n  "
        metadata.tail = "\n"
        owner.tail = "\n  "
        lifecycle.tail = "\n"
        metadata.remove(lifecycle)
        normalized = ET.tostring(root, encoding="unicode")
        assert oci_root_runtime_module._domain_projection(normalized) == authored_projection
        return normalized

    conn = _DefinitionConnection(transform=elide_lifecycle)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    receipt = define_committed_oci_root_domain(
        roots,
        "elided-lifecycle",
        store,
        boot,
        profile,
        conn=conn,
        runner=tools,
    )

    assert receipt.domain_uuid == conn.domains["elided-lifecycle"].UUIDString()
    assert read_run_ledger_snapshot(roots, "elided-lifecycle").state["status"] == "defined"


@pytest.mark.parametrize(
    "tamper",
    [
        "attributes",
        "missing-attribute",
        "content",
        "binding",
        "duplicate",
        "namespace",
        "owner-missing",
        "metadata-attribute",
        "metadata-text",
        "metadata-tail",
        "owner-tail",
        "lifecycle-tail",
    ],
)
def test_oci_root_domain_projection_rejects_noncanonical_lifecycle_metadata(
    tmp_path: Path,
    tamper: str,
) -> None:
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(
        tmp_path, f"lifecycle-metadata-{tamper}"
    )
    snapshot = read_run_ledger_snapshot(roots, f"lifecycle-metadata-{tamper}")
    xml = oci_root_kvm_module.resolve_committed_oci_root_domain_plan(
        roots,
        snapshot,
        store,
        boot,
        profile,
        runner=tools,
    ).xml
    root = ET.fromstring(xml)
    metadata = root.find("./metadata")
    lifecycle = root.find(f"./metadata/{{{oci_root_runtime_module.kvm.DOMAIN_MARKER_NAMESPACE}}}lifecycle")
    owner = root.find(f"./metadata/{{{oci_root_runtime_module.kvm.DOMAIN_MARKER_NAMESPACE}}}run")
    assert metadata is not None and lifecycle is not None and owner is not None
    if tamper == "attributes":
        lifecycle.set("extra", "forbidden")
    elif tamper == "missing-attribute":
        lifecycle.attrib.pop("protocol")
    elif tamper == "content":
        lifecycle.text = "forbidden"
    elif tamper == "binding":
        lifecycle.set("channel", "attacker.control")
    elif tamper == "duplicate":
        metadata.append(deepcopy(lifecycle))
    elif tamper == "namespace":
        lifecycle.tag = "{https://attacker.invalid/domain/v1}lifecycle"
    elif tamper == "owner-missing":
        metadata.remove(owner)
    elif tamper == "metadata-attribute":
        metadata.set("attacker", "forbidden")
    elif tamper == "metadata-text":
        metadata.text = "forbidden"
    elif tamper == "metadata-tail":
        metadata.tail = "forbidden"
    elif tamper == "owner-tail":
        owner.tail = "forbidden"
    else:
        lifecycle.tail = "forbidden"

    with pytest.raises(StateError):
        oci_root_runtime_module._domain_projection(ET.tostring(root, encoding="unicode"))


def test_oci_root_define_accepts_libvirt_generated_hard_disk_boot_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, "generated-boot")

    def add_boot_default(xml: str) -> str:
        root = ET.fromstring(xml)
        authored_projection = oci_root_runtime_module._domain_projection(xml)
        os_element = root.find("./os")
        assert os_element is not None
        ET.SubElement(os_element, "boot", {"dev": "hd"})
        normalized = ET.tostring(root, encoding="unicode")
        assert oci_root_runtime_module._domain_projection(normalized) == authored_projection
        return normalized

    conn = _DefinitionConnection(transform=add_boot_default)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    receipt = define_committed_oci_root_domain(
        roots,
        "generated-boot",
        store,
        boot,
        profile,
        conn=conn,
        runner=tools,
    )

    assert receipt.domain_uuid == conn.domains["generated-boot"].UUIDString()
    assert read_run_ledger_snapshot(roots, "generated-boot").state["status"] == "defined"


@pytest.mark.parametrize(
    "tamper",
    [
        "duplicate",
        "cdrom",
        "network",
        "fd",
        "extra-attribute",
        "missing-attribute",
        "text",
        "child",
        "unknown-os-child",
    ],
)
def test_oci_root_domain_projection_rejects_noncanonical_generated_boot_default(
    tmp_path: Path,
    tamper: str,
) -> None:
    name = f"generated-boot-{tamper}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    snapshot = read_run_ledger_snapshot(roots, name)
    xml = oci_root_kvm_module.resolve_committed_oci_root_domain_plan(
        roots,
        snapshot,
        store,
        boot,
        profile,
        runner=tools,
    ).xml
    root = ET.fromstring(xml)
    os_element = root.find("./os")
    assert os_element is not None
    generated = ET.SubElement(os_element, "boot", {"dev": "hd"})
    if tamper == "duplicate":
        ET.SubElement(os_element, "boot", {"dev": "hd"})
    elif tamper in {"cdrom", "network", "fd"}:
        generated.set("dev", tamper)
    elif tamper == "extra-attribute":
        generated.set("extra", "forbidden")
    elif tamper == "missing-attribute":
        generated.attrib.clear()
    elif tamper == "text":
        generated.text = "forbidden"
    elif tamper == "child":
        ET.SubElement(generated, "attacker")
    else:
        ET.SubElement(os_element, "attacker")

    with pytest.raises(StateError, match="direct-boot contract"):
        oci_root_runtime_module._domain_projection(ET.tostring(root, encoding="unicode"))


@pytest.mark.parametrize("drift", ["remove-defaults", "change-default"])
def test_oci_root_define_records_defaulted_cpu_and_rejects_later_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    name = f"defaulted-cpu-{drift}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def default_cpu(xml: str) -> str:
        root = ET.fromstring(xml)
        cpu = root.find("./cpu")
        assert cpu is not None
        cpu.set("check", "none")
        cpu.set("migratable", "on")
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=default_cpu)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    definition = read_run_ledger_snapshot(roots, name).state["oci_root_definition"]
    projection = oci_root_runtime_module._domain_projection(conn.domains[name].xml)
    assert projection["cpu"] == (
        (("check", "none"), ("migratable", "on"), ("mode", "host-passthrough")),
        (),
    )
    assert definition["projection_digest"] == oci_root_runtime_module._projection_digest(projection)

    root = ET.fromstring(conn.domains[name].xml)
    cpu = root.find("./cpu")
    assert cpu is not None
    if drift == "remove-defaults":
        cpu.attrib.pop("check")
        cpu.attrib.pop("migratable")
    else:
        cpu.set("migratable", "off")
    conn.domains[name].xml = ET.tostring(root, encoding="unicode")
    with pytest.raises(StateError, match="changed after definition|CPU contract"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    assert conn.domains[name].create_calls == 0


@pytest.mark.parametrize(
    "tamper",
    [
        "check-full",
        "migratable-off",
        "extra-attribute",
        "missing-check",
        "missing-migratable",
        "missing-mode",
        "text",
        "child",
    ],
)
def test_oci_root_define_rejects_noncanonical_defaulted_cpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    name = f"defaulted-cpu-{tamper}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def bad_cpu(xml: str) -> str:
        root = ET.fromstring(xml)
        cpu = root.find("./cpu")
        assert cpu is not None
        cpu.set("check", "none")
        cpu.set("migratable", "on")
        if tamper == "check-full":
            cpu.set("check", "full")
        elif tamper == "migratable-off":
            cpu.set("migratable", "off")
        elif tamper == "extra-attribute":
            cpu.set("match", "exact")
        elif tamper == "missing-check":
            cpu.attrib.pop("check")
        elif tamper == "missing-migratable":
            cpu.attrib.pop("migratable")
        elif tamper == "missing-mode":
            cpu.attrib.pop("mode")
        elif tamper == "text":
            cpu.text = "forbidden"
        else:
            ET.SubElement(cpu, "attacker")
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=bad_cpu)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    with pytest.raises(StateError, match="CPU contract"):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name not in conn.domains
    assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"


def test_oci_root_define_records_capability_validated_canonical_machine_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "canonical-machine"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def canonicalize_machine(xml: str) -> str:
        root = ET.fromstring(xml)
        machine = root.find("./os/type")
        assert machine is not None
        machine.set("machine", "pc-q35-noble")
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=canonicalize_machine)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert conn.domain_capability_calls == [(str(profile.emulator), profile.arch, profile.machine, "kvm", 0)]
    definition = read_run_ledger_snapshot(roots, name).state["oci_root_definition"]
    actual_projection = oci_root_runtime_module._domain_projection(conn.domains[name].xml)
    assert definition["schema"] == "palimpsest.oci-root-definition.v2"
    assert definition["projection_digest"] == oci_root_runtime_module._projection_digest(actual_projection)

    root = ET.fromstring(conn.domains[name].xml)
    root.find("./os/type").set("machine", "pc-q35-8.2")
    conn.domains[name].xml = ET.tostring(root, encoding="unicode")
    with pytest.raises(StateError, match="changed after definition"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    assert conn.domains[name].create_calls == 0
    assert read_run_ledger_snapshot(roots, name).state["status"] == "defined"


@pytest.mark.parametrize(
    "capability",
    [
        "missing-method",
        "malformed-root",
        "duplicate",
        "wrong-path",
        "wrong-domain",
        "wrong-machine",
        "wrong-arch",
        "scalar-attributes",
        "scalar-child",
    ],
)
def test_oci_root_define_rejects_unproven_machine_alias_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    name = f"machine-alias-{capability}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def canonicalize_machine(xml: str) -> str:
        root = ET.fromstring(xml)
        root.find("./os/type").set("machine", "pc-q35-noble")
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=canonicalize_machine)
    if capability == "missing-method":
        conn.getDomainCapabilities = None  # type: ignore[method-assign]
    else:

        def bad_domain_capabilities(emulator: str, arch: str, machine: str, domain: str, flags: int) -> str:
            assert (emulator, arch, machine, domain, flags) == (
                str(profile.emulator),
                profile.arch,
                profile.machine,
                "kvm",
                0,
            )
            root = ET.Element("capabilities" if capability == "malformed-root" else "domainCapabilities")
            values = {
                "path": "/foreign/qemu" if capability == "wrong-path" else emulator,
                "domain": "qemu" if capability == "wrong-domain" else domain,
                "machine": "pc-q35-8.2" if capability == "wrong-machine" else "pc-q35-noble",
                "arch": "aarch64" if capability == "wrong-arch" else arch,
            }
            for tag, value in values.items():
                ET.SubElement(root, tag).text = value
            if capability == "duplicate":
                ET.SubElement(root, "machine").text = values["machine"]
            elif capability == "scalar-attributes":
                root.find("./machine").set("canonical", "forbidden")
            elif capability == "scalar-child":
                ET.SubElement(root.find("./machine"), "attacker")
            return ET.tostring(root, encoding="unicode")

        conn.getDomainCapabilities = bad_domain_capabilities  # type: ignore[method-assign]
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    with pytest.raises(StateError, match="machine alias"):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name not in conn.domains
    assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"


@pytest.mark.parametrize("tamper", ["legacy-schema", "missing-projection", "changed-projection"])
def test_oci_root_private_launch_rejects_unbound_definition_projection_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    name = f"definition-projection-{tamper}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    with state_module.locked_existing_run(roots, name) as mutation:
        data = mutation.mutable_state()
        definition = data["oci_root_definition"]
        if tamper == "legacy-schema":
            definition["schema"] = "palimpsest.oci-root-definition.v1"
        elif tamper == "missing-projection":
            definition.pop("projection_digest")
        else:
            definition["projection_digest"] = "sha256:" + "f" * 64
        mutation.write_state("defined", data)

    with pytest.raises(StateError, match="definition ledger|changed after definition"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert conn.domains[name].create_calls == 0
    assert read_run_ledger_snapshot(roots, name).state["status"] == "defined"


@pytest.mark.parametrize("tamper", ["mismatch", "duplicate", "extra-attribute", "child"])
def test_oci_root_define_rejects_noncanonical_generated_current_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    name = f"current-memory-{tamper}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)

    def bad_current_memory(xml: str) -> str:
        root = ET.fromstring(xml)
        memory = root.find("./memory")
        assert memory is not None and memory.text is not None
        current = ET.SubElement(root, "currentMemory", dict(memory.attrib))
        current.text = memory.text
        if tamper == "mismatch":
            current.text = str(int(memory.text) + 1)
        elif tamper == "duplicate":
            ET.SubElement(root, "currentMemory", dict(memory.attrib)).text = memory.text
        elif tamper == "extra-attribute":
            current.set("placement", "static")
        else:
            ET.SubElement(current, "attacker")
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=bad_current_memory)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    with pytest.raises(StateError):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name not in conn.domains
    assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"


@pytest.mark.parametrize("tamper", ["lower", "root", "transport", "kernel", "socket", "profile"])
def test_oci_root_define_rejects_live_authority_tamper_before_libvirt_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    roots, store, tools, boot, profile, prepared, plan = _committed_oci_domain(tmp_path, f"tamper-{tamper}")
    selected_profile = profile
    if tamper == "lower":
        lower = roots.store / "blobs" / "sha256" / str(plan.layers[0]["image_digest"]).removeprefix("sha256:")
        payload = bytearray(lower.read_bytes())
        payload[-1] ^= 1
        lower.chmod(0o600)
        lower.write_bytes(payload)
        lower.chmod(0o400)
    elif tamper == "root":
        with prepared.root_volume.path.open("r+b") as stream:
            stream.seek(1024 + 104)
            byte = stream.read(1)
            stream.seek(1024 + 104)
            stream.write(bytes([byte[0] ^ 1]))
            stream.flush()
            os.fsync(stream.fileno())
    elif tamper == "transport":
        transport = roots.runs / f"tamper-{tamper}" / "stage1-plan.raw"
        payload = bytearray(transport.read_bytes())
        payload[-1] ^= 1
        transport.chmod(0o600)
        transport.write_bytes(payload)
        transport.chmod(0o400)
    elif tamper == "kernel":
        payload = bytearray(boot.kernel.path.read_bytes())
        payload[0] ^= 1
        boot.kernel.path.write_bytes(payload)
    elif tamper == "socket":
        (roots.runs / f"tamper-{tamper}" / "lifecycle.sock").symlink_to(tmp_path / "foreign.sock")
    else:
        selected_profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "aarch64")
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    with pytest.raises((StateError, OCIStoreError)):
        define_committed_oci_root_domain(
            roots,
            f"tamper-{tamper}",
            store,
            boot,
            selected_profile,
            conn=conn,
            runner=tools,
        )

    assert conn.define_calls == 0
    assert read_run_ledger_snapshot(roots, f"tamper-{tamper}").state["status"] == "creating"


def test_oci_root_define_fails_closed_for_ambiguous_lookup_and_foreign_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, "lookup-guard")
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    wrong_uri = _DefinitionConnection(uri="qemu:///session")
    with pytest.raises(StateError, match="URI does not match"):
        define_committed_oci_root_domain(roots, "lookup-guard", store, boot, profile, conn=wrong_uri, runner=tools)
    assert wrong_uri.define_calls == 0

    ambiguous = _DefinitionConnection()
    ambiguous.lookup_error = _FakeLibvirtError("transport failure", 99)
    with pytest.raises(StateError, match="cannot determine"):
        define_committed_oci_root_domain(roots, "lookup-guard", store, boot, profile, conn=ambiguous, runner=tools)
    assert ambiguous.define_calls == 0

    foreign = _DefinitionConnection()
    foreign_domain = _DefinedDomain(foreign, "lookup-guard", "<domain><name>lookup-guard</name></domain>")
    foreign.domains["lookup-guard"] = foreign_domain
    with pytest.raises(StateError, match="already reserved"):
        define_committed_oci_root_domain(roots, "lookup-guard", store, boot, profile, conn=foreign, runner=tools)
    assert foreign.define_calls == 0
    assert foreign.domains["lookup-guard"] is foreign_domain
    assert foreign_domain.undefine_calls == 0


@pytest.mark.parametrize(
    "failure",
    [
        "disk",
        "lifecycle",
        "channel-source",
        "cpu",
        "features",
        "console",
        "direct-boot",
        "filesystem",
        "hostdev",
        "shmem",
        "unknown-device",
        "disk-backing-store",
        "interface-script",
        "qemu-commandline",
        "xml-uuid",
    ],
)
def test_oci_root_define_failure_cleans_only_exact_new_owned_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    name = f"cleanup-{failure}"
    roots, store, tools, boot, profile, prepared, _plan = _committed_oci_domain(tmp_path, name)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    def transform(xml: str) -> str:
        root = ET.fromstring(xml)
        if failure == "disk":
            root.find("./devices/disk/source").set("file", "/foreign/root.raw")
        elif failure == "lifecycle":
            root.find(f"./metadata/{{{oci_root_runtime_module.kvm.DOMAIN_MARKER_NAMESPACE}}}lifecycle").set(
                "channel", "attacker.control"
            )
        elif failure == "channel-source":
            root.find("./devices/channel/source").set("path", "/tmp/attacker.sock")
        elif failure == "cpu":
            root.find("./cpu").set("mode", "custom")
        elif failure == "features":
            root.find("./features").remove(root.find("./features/apic"))
        elif failure == "console":
            root.find("./devices/console/target").set("port", "1")
        elif failure == "direct-boot":
            ET.SubElement(root.find("./os"), "boot", {"dev": "cdrom"})
        elif failure == "filesystem":
            filesystem = ET.SubElement(root.find("./devices"), "filesystem", {"type": "mount"})
            ET.SubElement(filesystem, "source", {"dir": "/"})
            ET.SubElement(filesystem, "target", {"dir": "host"})
        elif failure == "hostdev":
            ET.SubElement(root.find("./devices"), "hostdev", {"mode": "subsystem", "type": "pci"})
        elif failure == "shmem":
            ET.SubElement(root.find("./devices"), "shmem", {"name": "attacker"})
        elif failure == "unknown-device":
            ET.SubElement(root.find("./devices"), "attacker-device")
        elif failure == "disk-backing-store":
            backing = ET.SubElement(root.find("./devices/disk"), "backingStore")
            ET.SubElement(backing, "source", {"file": "/etc/passwd"})
        elif failure == "interface-script":
            ET.SubElement(root.find("./devices/interface"), "script", {"path": "/tmp/attacker"})
        elif failure == "qemu-commandline":
            commandline = ET.SubElement(root, "{http://libvirt.org/schemas/domain/qemu/1.0}commandline")
            ET.SubElement(commandline, "{http://libvirt.org/schemas/domain/qemu/1.0}arg", {"value": "-S"})
        elif failure == "xml-uuid":
            ET.SubElement(root, "uuid").text = str(uuid.uuid4())
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=transform)
    with pytest.raises(StateError):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name not in conn.domains
    assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"
    assert prepared.root_volume.path.is_file()
    assert store.load_lease_set(
        prepared.transaction.lower_lease_set_id,
        prepared.transaction.owner,
        plan_digest=prepared.transaction.boot_plan_digest,
    ).members
    assert (roots.runs / name / "stage1-plan.raw").is_file()


@pytest.mark.parametrize("failure", ["partial-define", "uuid-rebind", "undefine"])
def test_oci_root_define_records_cleanup_required_when_safe_cleanup_cannot_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    name = f"cleanup-required-{failure}"
    roots, store, tools, boot, profile, _prepared, plan = _committed_oci_domain(tmp_path, name)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    def bad_disk(xml: str) -> str:
        root = ET.fromstring(xml)
        root.find("./devices/disk/source").set("file", "/foreign/root.raw")
        return ET.tostring(root, encoding="unicode")

    if failure == "partial-define":
        conn = _DefinitionConnection(define_error=RuntimeError("partial define failure"))
    elif failure == "uuid-rebind":
        conn = _DefinitionConnection(rebind_uuid=True)
    else:
        conn = _DefinitionConnection(transform=bad_disk, undefine_error=RuntimeError("undefine failed"))

    with pytest.raises(StateError, match="cleanup.*required"):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name in conn.domains
    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    cleanup = snapshot.state["oci_root_definition"]
    assert cleanup["phase"] == "cleanup-required"
    assert cleanup["plan_digest"] == plan.digest
    assert cleanup["libvirt_uri"] == profile.uri
    if failure == "partial-define":
        assert cleanup["domain_uuid"] is None
    else:
        assert isinstance(cleanup["domain_uuid"], str)


def test_oci_root_define_does_not_cleanup_foreign_post_define_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "foreign-rebind"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)

    def foreign_marker(xml: str) -> str:
        root = ET.fromstring(xml)
        marker = root.find(f"./metadata/{{{oci_root_runtime_module.kvm.DOMAIN_MARKER_NAMESPACE}}}run")
        marker.set("id", str(uuid.uuid4()))
        return ET.tostring(root, encoding="unicode")

    conn = _DefinitionConnection(transform=foreign_marker)
    with pytest.raises(StateError, match="cleanup.*required"):
        define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert conn.domains[name].undefine_calls == 0
    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root_definition"]["phase"] == "cleanup-required"


def _handoff_receipt(
    phase: str,
    terminal: ProcessExit | None = None,
    *,
    boot_attempt_id: str = "aca88126-d991-4de8-b66b-90dc07904dff",
) -> OCILifecycleHandoffReceipt:
    return OCILifecycleHandoffReceipt(
        boot_attempt_id,
        "b22b1c81-dfa4-478a-b352-27b5b35fe5b7",
        "sha256:" + "9" * 64,
        phase,
        (),
        terminal,
    )


def test_oci_root_private_launch_records_ready_and_terminal_before_exited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-complete"
    roots, store, tools, boot, profile, _prepared, plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    defined = define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    observed_statuses: list[str] = []
    terminal = ProcessExit(17, 17, None, ProcessExitCategory.EXITED)

    def handoff(_stream, binding, *, on_ready, timeout_seconds, terminal_timeout_seconds, session):
        assert binding.run_id == plan.run_id
        assert binding.domain_core_digest == plan.domain_core_digest
        assert binding.stage1_artifact_digest == plan.stage1_transport["artifact_digest"]
        assert timeout_seconds == 9
        assert terminal_timeout_seconds is None
        starting = read_run_ledger_snapshot(roots, name).state
        observed_statuses.append(starting["status"])
        assert set(starting["oci_root_handoff"]) == {
            "boot_attempt_id",
            "domain_id",
            "domain_uuid",
            "libvirt_uri",
            "phase",
            "plan_digest",
            "schema",
        }
        assert starting["oci_root_handoff"]["domain_id"] == 7
        on_ready(_handoff_receipt("ready", boot_attempt_id=session.boot_attempt_id))
        observed_statuses.append(read_run_ledger_snapshot(roots, name).state["status"])
        return _handoff_receipt("terminal", terminal, boot_attempt_id=session.boot_attempt_id)

    monkeypatch.setattr(oci_root_runtime_module, "complete_initial_lifecycle_handoff", handoff)
    result = launch_defined_oci_root_domain(
        roots,
        name,
        store,
        boot,
        profile,
        conn=conn,
        runner=tools,
        timeout_seconds=9,
    )

    domain = conn.domains[name]
    assert observed_statuses == ["starting", "running"]
    assert conn.stream_flags == [_FAKE_LIBVIRT.VIR_STREAM_NONBLOCK]
    assert [(channel, flags) for channel, _stream, flags in domain.open_channel_calls] == [
        ("org.palimpsest.oci.lifecycle.0", 0)
    ]
    assert domain.create_calls == domain.destroy_calls == 1
    assert domain.isActive() == 0
    assert _FAKE_LIBVIRT.VIR_DOMAIN_XML_INACTIVE in domain.xml_desc_flags
    assert result.domain_uuid == defined.domain_uuid
    assert result.domain_id == 7
    assert result.terminal == terminal
    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "exited"
    handoff_ledger = snapshot.state["oci_root_handoff"]
    assert handoff_ledger["schema"] == "palimpsest.oci-root-handoff.v1"
    assert handoff_ledger["phase"] == "terminal"
    assert handoff_ledger["plan_digest"] == plan.digest
    assert handoff_ledger["domain_id"] == 7
    assert handoff_ledger["boot_attempt_id"] == result.lifecycle.boot_attempt_id
    assert handoff_ledger["lifecycle"]["terminal"] == {
        "category": "exited",
        "exit_code": 17,
        "returncode": 17,
        "signal_number": None,
    }
    assert "boot_key" not in repr(handoff_ledger)
    assert "tag" not in repr(handoff_ledger)


def test_oci_root_private_launch_rejects_name_uuid_disagreement_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-mismatch"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    original = conn.domains[name]
    foreign = _DefinedDomain(conn, name, original.xml)
    conn.lookupByUUIDString = lambda _domain_uuid: foreign  # type: ignore[method-assign]

    with pytest.raises(StateError, match="name and UUID"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert original.create_calls == original.destroy_calls == original.undefine_calls == 0
    assert read_run_ledger_snapshot(roots, name).state["status"] == "defined"


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_oci_root_private_launch_failure_cleans_exact_domain_or_records_cleanup_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: bool,
) -> None:
    name = f"launch-failure-{'yes' if cleanup_failure else 'no'}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection(destroy_error=RuntimeError("SENSITIVE DESTROY FAILURE") if cleanup_failure else None)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    domain = conn.domains[name]

    def handoff_failure(*_args, **_kwargs):
        raise RuntimeError("SENSITIVE LIFECYCLE FAILURE")

    monkeypatch.setattr(oci_root_runtime_module, "complete_initial_lifecycle_handoff", handoff_failure)
    message = "cleanup.*required" if cleanup_failure else "launch failed"
    with pytest.raises(StateError, match=message):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root_handoff"]["phase"] == ("cleanup-required" if cleanup_failure else "failed")
    assert "SENSITIVE" not in repr(snapshot.state)
    if cleanup_failure:
        assert conn.domains[name] is domain
        assert domain.isActive() == 1
        assert domain.undefine_calls == 0
    else:
        assert name not in conn.domains
        assert domain.destroy_calls == domain.undefine_calls == 1


def test_oci_root_private_launch_requires_libvirt_surface_before_starting_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-libvirt-surface"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    unsupported = SimpleNamespace(
        libvirtError=_FakeLibvirtError,
        VIR_ERR_NO_DOMAIN=42,
        VIR_STREAM_NONBLOCK=1,
    )
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: unsupported)

    with pytest.raises(StateError, match="libvirt stream or inactive XML"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    domain = conn.domains[name]
    assert domain.create_calls == domain.destroy_calls == domain.undefine_calls == 0
    assert read_run_ledger_snapshot(roots, name).state["status"] == "defined"


def test_oci_root_private_launch_preallocates_and_validates_stream_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-stream-preflight"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    domain = conn.domains[name]
    calls = {"abort": 0, "free": 0}

    def close(operation: str) -> None:
        calls[operation] += 1

    conn.stream = SimpleNamespace(
        send=lambda payload: len(payload),
        abort=lambda: close("abort"),
        free=lambda: close("free"),
    )

    with pytest.raises(StateError, match="stream surface is invalid"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert domain.create_calls == domain.destroy_calls == domain.undefine_calls == 0
    assert calls == {"abort": 1, "free": 1}
    assert read_run_ledger_snapshot(roots, name).state["status"] == "defined"


def test_oci_root_runtime_stream_surface_accepts_binding_without_public_free() -> None:
    calls = {"abort": 0}
    stream = SimpleNamespace(
        send=lambda payload: len(payload),
        recv=lambda _size: -2,
        abort=lambda: calls.__setitem__("abort", calls["abort"] + 1),
    )

    assert oci_root_runtime_module._valid_lifecycle_stream_surface(stream)
    assert not oci_root_runtime_module._valid_lifecycle_stream_surface(
        SimpleNamespace(send=stream.send, recv=stream.recv)
    )
    assert not oci_root_runtime_module._valid_lifecycle_stream_surface(
        SimpleNamespace(send=stream.send, recv=stream.recv, abort=stream.abort, free=object())
    )
    oci_root_runtime_module._close_unowned_stream(stream)
    assert calls == {"abort": 1}


@pytest.mark.parametrize("failure", ["definite-create", "partial-create", "id-inspection"])
def test_oci_root_private_launch_create_boundary_uses_durable_activation_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    name = f"launch-{failure}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    domain = conn.domains[name]
    observed: list[tuple[str, str, bool]] = []

    def fail_create() -> None:
        domain.create_calls += 1
        snapshot = read_run_ledger_snapshot(roots, name).state
        observed.append(
            (snapshot["status"], snapshot["oci_root_handoff"]["phase"], "domain_id" in snapshot["oci_root_handoff"])
        )
        if failure == "partial-create":
            domain.active = 1
        raise RuntimeError("create failed")

    if failure in {"definite-create", "partial-create"}:
        domain.create = fail_create  # type: ignore[method-assign]
    else:
        original_create = domain.create

        def create_then_break_id() -> None:
            original_create()
            snapshot = read_run_ledger_snapshot(roots, name).state
            observed.append(
                (snapshot["status"], snapshot["oci_root_handoff"]["phase"], "domain_id" in snapshot["oci_root_handoff"])
            )

        domain.create = create_then_break_id  # type: ignore[method-assign]
        domain.ID = lambda: (_ for _ in ()).throw(RuntimeError("ID failed"))  # type: ignore[method-assign]

    message = "cleanup was not attempted" if failure != "definite-create" else "launch failed"
    with pytest.raises(StateError, match=message):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert observed == [("starting", "activating", False)]
    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    if failure == "definite-create":
        assert name not in conn.domains
        assert domain.undefine_calls == 1
        assert snapshot.state["oci_root_handoff"]["phase"] == "failed"
    else:
        assert conn.domains[name] is domain
        assert domain.destroy_calls == domain.undefine_calls == 0
        assert snapshot.state["oci_root_handoff"]["phase"] == "cleanup-not-attempted"


def test_oci_root_private_launch_rejects_nonzero_or_none_open_channel_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-open-result"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection(open_channel_result=None)
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    monkeypatch.setattr(
        oci_root_runtime_module,
        "complete_initial_lifecycle_handoff",
        lambda *_args, **_kwargs: pytest.fail("handoff must not start"),
    )

    with pytest.raises(StateError, match="launch failed"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert name not in conn.domains
    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root_handoff"]["phase"] == "failed"


def test_oci_root_private_launch_does_not_cleanup_or_overwrite_changed_boot_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-concurrent-change"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    domain = conn.domains[name]

    def change_ledger_then_fail(*_args, **_kwargs):
        with state_module.locked_existing_run(roots, name) as mutation:
            data = mutation.mutable_state()
            data["oci_root_handoff"]["boot_attempt_id"] = str(uuid.uuid4())
            mutation.write_state("stopped", data)
        raise RuntimeError("SENSITIVE FAILURE")

    monkeypatch.setattr(
        oci_root_runtime_module,
        "complete_initial_lifecycle_handoff",
        change_ledger_then_fail,
    )
    with pytest.raises(StateError, match="cleanup was not attempted"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert conn.domains[name] is domain
    assert domain.isActive() == 1
    assert domain.destroy_calls == domain.undefine_calls == 0
    assert read_run_ledger_snapshot(roots, name).state["status"] == "stopped"


def test_oci_root_private_launch_failure_retains_durable_ready_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "launch-ready-failure"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    def ready_then_fail(_stream, _binding, *, on_ready, session, **_kwargs):
        on_ready(_handoff_receipt("ready", boot_attempt_id=session.boot_attempt_id))
        raise RuntimeError("SENSITIVE FAILURE")

    monkeypatch.setattr(oci_root_runtime_module, "complete_initial_lifecycle_handoff", ready_then_fail)
    with pytest.raises(StateError, match="launch failed"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root_handoff"]["phase"] == "failed"
    assert snapshot.state["oci_root_handoff"]["lifecycle"]["phase"] == "ready"


@pytest.mark.parametrize("drift", ["domain-id", "xml"])
def test_oci_root_private_launch_never_cleans_a_changed_boot_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    name = f"launch-drift-{drift}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    domain = conn.domains[name]

    def drift_then_fail(*_args, **_kwargs):
        if drift == "domain-id":
            domain.domain_id += 1
        else:
            root = ET.fromstring(domain.xml)
            root.find("./devices/disk/source").set("file", "/foreign/new-boot.raw")
            domain.xml = ET.tostring(root, encoding="unicode")
        raise RuntimeError("SENSITIVE FAILURE")

    monkeypatch.setattr(oci_root_runtime_module, "complete_initial_lifecycle_handoff", drift_then_fail)
    with pytest.raises(StateError, match="control is ambiguous; cleanup was not attempted"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert conn.domains[name] is domain
    assert domain.isActive() == 1
    assert domain.destroy_calls == domain.undefine_calls == 0
    snapshot = read_run_ledger_snapshot(roots, name)
    assert snapshot.state["status"] == "failed"
    assert snapshot.state["oci_root_handoff"]["phase"] == "cleanup-not-attempted"


@pytest.mark.parametrize("tamper", ["domain_uuid", "domain_id", "libvirt_uri", "plan_digest", "lifecycle"])
def test_oci_root_private_launch_rejects_same_attempt_handoff_ledger_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    name = f"launch-ledger-{tamper.replace('_', '-')}"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(oci_root_runtime_module.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    domain = conn.domains[name]

    def tamper_then_fail(_stream, _binding, *, on_ready, session, **_kwargs):
        if tamper == "lifecycle":
            on_ready(_handoff_receipt("ready", boot_attempt_id=session.boot_attempt_id))
        with state_module.locked_existing_run(roots, name) as mutation:
            data = mutation.mutable_state()
            handoff = data["oci_root_handoff"]
            if tamper == "domain_uuid":
                handoff[tamper] = str(uuid.uuid4())
            elif tamper == "domain_id":
                handoff[tamper] += 1
            elif tamper in {"libvirt_uri", "plan_digest"}:
                handoff[tamper] = "tampered"
            else:
                handoff["lifecycle"]["key_id"] = "sha256:" + "8" * 64
            mutation.write_state("running" if tamper == "lifecycle" else "starting", data)
        raise RuntimeError("SENSITIVE FAILURE")

    monkeypatch.setattr(
        oci_root_runtime_module,
        "complete_initial_lifecycle_handoff",
        tamper_then_fail,
    )
    with pytest.raises(StateError, match="cleanup was not attempted"):
        launch_defined_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)

    assert conn.domains[name] is domain
    assert domain.isActive() == 1
    assert domain.destroy_calls == domain.undefine_calls == 0


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
    roots, store = _short_oci_store()
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
