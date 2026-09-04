"""Opt-in live qualification of the production-inert OCI-root libvirt handoff."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from palimpsest_local import kvm, oci_root_runtime, platforms
from palimpsest_local._oci_stage1_kvm_proof import (
    KERNEL_CONFIG_ENV,
    KERNEL_ENV,
    load_proof_filesystems,
    verify_kernel_config,
    verify_kvm_api,
)
from palimpsest_local.errors import StateError
from palimpsest_local.oci_converter import (
    DEFAULT_LAYER_CONVERSION_LIMITS,
    LAYER_INTAKE_POLICY_ID,
    LayerIntakeReceipt,
)
from palimpsest_local.oci_initramfs import build_bootstrap_initramfs
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
    verify_first_party_bootstrap_initramfs,
    verify_host_boot_artifacts,
)
from palimpsest_local.oci_root_prepare import (
    PreparedOCIRootRun,
    prepare_oci_root_run,
    release_prepared_oci_root_run,
)
from palimpsest_local.oci_root_runtime import (
    define_committed_oci_root_domain,
    launch_defined_oci_root_domain,
)
from palimpsest_local.oci_root_volume import load_oci_root_volume
from palimpsest_local.oci_store import (
    DerivedLayerOccurrence,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStore,
)
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind
from palimpsest_local.state import StatePaths, init_resolved_roots, read_run_ledger_snapshot, reserve_new_run

pytestmark = [pytest.mark.kvm, pytest.mark.oci_root_libvirt]

_ENABLE_ENV = "PALIMPSEST_REQUIRE_OCI_ROOT_LIBVIRT"
_ROOT_SIZE_BYTES = 16 * 1024 * 1024


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_live_host(tmp_path: Path):
    if os.environ.get(_ENABLE_ENV) != "1":
        pytest.skip(f"set {_ENABLE_ENV}=1 on the qualified native Linux/KVM libvirt runner")
    kernel_value = os.environ.get(KERNEL_ENV)
    config_value = os.environ.get(KERNEL_CONFIG_ENV)
    if not kernel_value or not config_value:
        pytest.fail(f"{KERNEL_ENV} and {KERNEL_CONFIG_ENV} must explicitly select qualified artifacts")
    for executable in ("qemu-img", "mkfs.ext4", "debugfs"):
        if shutil.which(executable) is None:
            pytest.fail(f"required live OCI-root tool is unavailable: {executable}")
    assert verify_kvm_api() == 12
    verify_kernel_config(Path(config_value).resolve())
    built = build_bootstrap_initramfs()
    initramfs_path = tmp_path / "palimpsest-oci-root.initramfs"
    initramfs_path.write_bytes(built.payload)
    initramfs_path.chmod(0o400)
    boot = verify_first_party_bootstrap_initramfs(initramfs_path.resolve(), built.manifest)
    verified = verify_host_boot_artifacts(
        Path(kernel_value).resolve(),
        boot.path,
        expected_initramfs_digest=boot.digest,
    )
    profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
    if profile.uri != "qemu:///system":
        pytest.fail("qualified OCI-root live test requires the qemu:///system profile")
    return verified, profile


def _proof_materialization(store: OCIStore) -> OCIImageMaterializationReceipt:
    # This qualification starts at the derived-store boundary.  The loader
    # below independently verifies the checked-in real mksquashfs outputs and
    # their source/build manifest; OCI registry intake is covered elsewhere.
    filesystems = load_proof_filesystems()
    source_binding = _digest(b"palimpsest-live-libvirt-proof-fixtures-v1")
    source_image = _digest(b"".join(filesystems.lowers))
    toolchain = SquashFSToolchainIdentity(
        "4.7.5",
        _digest(b"source-controlled-mksquashfs-fixture"),
        (_digest(b"source-controlled-zstd-fixture"),),
    )
    capability = VerifiedSquashFSToolchain(toolchain, Path("/source-controlled/mksquashfs"), ())
    occurrences: list[DerivedLayerOccurrence] = []
    results: list[MaterializationResult] = []
    for ordinal, image in enumerate(filesystems.lowers):
        occurrence = DerivedLayerOccurrence(
            source_snapshot_binding_digest=source_binding,
            source_image_digest=source_image,
            ordinal=ordinal,
            media_type=OCI_LAYER_MEDIA_TYPE,
            compressed_digest=_digest(image),
            compressed_size=len(image),
            diff_id=_digest(b"proof-diff-id\0" + ordinal.to_bytes(4, "big") + image),
        )
        key = DerivedSquashFSKey.for_occurrence(
            occurrence,
            intake_policy_id=LAYER_INTAKE_POLICY_ID,
            intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            pack_policy_id=SQUASHFS_PACK_POLICY_ID,
            pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
            toolchain=capability,
        )
        intake = LayerIntakeReceipt(
            policy_id=LAYER_INTAKE_POLICY_ID,
            policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            ordinal=ordinal,
            media_type=OCI_LAYER_MEDIA_TYPE,
            compressed_digest=occurrence.compressed_digest,
            compressed_size=occurrence.compressed_size,
            diff_id=occurrence.diff_id,
            uncompressed_size=len(image),
            physical_headers=1,
            members=1,
            regular_bytes=len(image),
            xattr_bytes=0,
        )
        packed_receipt = PackedSquashFSReceipt(
            policy_id=SQUASHFS_PACK_POLICY_ID,
            policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
            source_ordinal=ordinal,
            source_diff_id=occurrence.diff_id,
            normalized_tar_digest=_digest(b"proof-normalized-tar\0" + ordinal.to_bytes(4, "big")),
            normalized_tar_size=10240,
            entries=1,
            packer_version=toolchain.version,
            packer_sha256=toolchain.executable_digest.removeprefix("sha256:"),
            image_digest=_digest(image),
            image_size=len(image),
            structural_verifier=SQUASHFS_STRUCTURAL_VERIFIER_ID,
            toolchain_fingerprint=toolchain.fingerprint,
            toolchain_dependency_digests=toolchain.dependency_digests,
        )

        @contextmanager
        def producer(
            payload: bytes = image,
            bound_intake: LayerIntakeReceipt = intake,
            bound_receipt: PackedSquashFSReceipt = packed_receipt,
        ):
            packed = LeasedSquashFS(io.BytesIO(payload), bound_receipt)
            try:
                yield bound_intake, packed
            finally:
                failure = packed._close()
                if failure is not None:
                    raise failure

        occurrences.append(occurrence)
        results.append(store.materialize_observed(occurrence, key, producer))

    process = OCIProcessSpec(
        (".__palimpsest_workload_proof_v1", "deliberately-not-the-proof-invocation"),
        (("PATH", "/proof/missing:/"),),
        "/proof/workdir",
        OCIUserSpec("palimpsest", None),
        15,
    )
    manifest_digest = _digest(b"palimpsest-live-libvirt-manifest-v1")
    config_digest = _digest(json.dumps(process.to_dict(), sort_keys=True).encode())
    return OCIImageMaterializationReceipt(
        source_snapshot_binding_digest=source_binding,
        source_image_digest=source_image,
        root_descriptor=Descriptor(OCI_IMAGE_MANIFEST_MEDIA_TYPE, manifest_digest, 1),
        manifest_digest=manifest_digest,
        config_descriptor=Descriptor(OCI_IMAGE_CONFIG_MEDIA_TYPE, config_digest, 1),
        platform_os="linux",
        platform_architecture="amd64",
        layer_descriptors=tuple(
            Descriptor(OCI_LAYER_MEDIA_TYPE, occurrence.compressed_digest, occurrence.compressed_size)
            for occurrence in occurrences
        ),
        layer_diff_ids=tuple(occurrence.diff_id for occurrence in occurrences),
        process=process,
        results=tuple(results),
    )


def _owner_marker(domain) -> dict[str, str]:
    root = ET.fromstring(domain.XMLDesc())
    marker = root.find(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
    assert marker is not None
    return dict(marker.attrib)


def _lookup_domain(conn, name: str):
    try:
        return conn.lookupByName(name)
    except kvm._libvirt().libvirtError as exc:
        assert exc.get_error_code() == kvm._libvirt().VIR_ERR_NO_DOMAIN
        return None


def _lookup_domain_uuid(conn, domain_uuid: str):
    try:
        return conn.lookupByUUIDString(domain_uuid)
    except kvm._libvirt().libvirtError as exc:
        assert exc.get_error_code() == kvm._libvirt().VIR_ERR_NO_DOMAIN
        return None


def _inspect_exact_owned_domain(
    conn,
    name: str,
    domain_uuid: str,
    run_id: str,
    plan_digest: str,
    expected_inactive_projection: Mapping[str, Any],
):
    by_name = _lookup_domain(conn, name)
    by_uuid = _lookup_domain_uuid(conn, domain_uuid)
    assert by_name is not None and by_uuid is not None
    assert by_name.UUIDString() == by_uuid.UUIDString() == domain_uuid
    expected_owner = {
        "contract": plan_digest,
        "id": run_id,
        "schema": "1",
        "version": kvm.DOMAIN_MARKER_VERSION,
    }
    assert _owner_marker(by_name) == _owner_marker(by_uuid) == expected_owner
    inactive_flag = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
    assert type(inactive_flag) is int
    observed: list[tuple[int, int]] = []
    for candidate in (by_name, by_uuid):
        inactive_xml = candidate.XMLDesc(inactive_flag)
        assert oci_root_runtime._domain_projection(inactive_xml) == expected_inactive_projection
        xml_root = ET.fromstring(inactive_xml)
        xml_uuids = xml_root.findall("./uuid")
        assert not xml_uuids or xml_uuids[0].text == domain_uuid
        observed.append((candidate.isActive(), candidate.ID()))
    assert observed[0] == observed[1]
    active, domain_id = observed[0]
    assert active in {0, 1}
    assert type(domain_id) is int
    return by_name, active, domain_id


def _remove_exact_owned_domain(
    conn,
    name: str,
    domain_uuid: str,
    run_id: str,
    plan_digest: str,
    expected_domain_id: int,
    expected_inactive_projection: Mapping[str, Any],
) -> None:
    domain = _lookup_domain(conn, name)
    if domain is None:
        assert _lookup_domain_uuid(conn, domain_uuid) is None
        return
    domain, active, domain_id = _inspect_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        expected_inactive_projection,
    )
    if active == 1:
        assert expected_domain_id > 0 and domain_id == expected_domain_id
        domain, active, domain_id = _inspect_exact_owned_domain(
            conn,
            name,
            domain_uuid,
            run_id,
            plan_digest,
            expected_inactive_projection,
        )
        assert active == 1 and domain_id == expected_domain_id
        domain.destroy()
    domain, active, domain_id = _inspect_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        expected_inactive_projection,
    )
    assert active == 0 and domain_id == -1
    domain.undefine()
    assert _lookup_domain(conn, name) is None
    assert _lookup_domain_uuid(conn, domain_uuid) is None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _MissingCleanupDomainError(Exception):
    def get_error_code(self) -> int:
        return 404


class _CleanupTestDomain:
    def __init__(self, conn: _CleanupTestConnection, domain_uuid: str, xml: str, domain_id: int) -> None:
        self.conn = conn
        self.domain_uuid = domain_uuid
        self.xml = xml
        self.active = 1
        self.domain_id = domain_id
        self.destroy_calls = 0
        self.undefine_calls = 0

    def UUIDString(self) -> str:
        return self.domain_uuid

    def XMLDesc(self, _flags: int | None = None) -> str:
        return self.xml

    def isActive(self) -> int:
        return self.active

    def ID(self) -> int:
        return self.domain_id if self.active == 1 else -1

    def destroy(self) -> None:
        self.destroy_calls += 1
        self.active = 0

    def undefine(self) -> None:
        self.undefine_calls += 1
        self.conn.domain = None


class _CleanupTestConnection:
    def __init__(self, name: str, domain_uuid: str, xml: str, domain_id: int) -> None:
        self.name = name
        self.domain: _CleanupTestDomain | None = None
        self.domain = _CleanupTestDomain(self, domain_uuid, xml, domain_id)

    def lookupByName(self, name: str) -> _CleanupTestDomain:
        if self.domain is None or name != self.name:
            raise _MissingCleanupDomainError
        return self.domain

    def lookupByUUIDString(self, domain_uuid: str) -> _CleanupTestDomain:
        if self.domain is None or domain_uuid != self.domain.domain_uuid:
            raise _MissingCleanupDomainError
        return self.domain


def _cleanup_test_xml(domain_uuid: str, run_id: str, plan_digest: str, *, drift: bool = False) -> str:
    suffix = "<drift />" if drift else ""
    return (
        f'<domain><uuid>{domain_uuid}</uuid><metadata xmlns:pali="{kvm.DOMAIN_MARKER_NAMESPACE}">'
        f'<pali:run contract="{plan_digest}" id="{run_id}" schema="1" '
        f'version="{kvm.DOMAIN_MARKER_VERSION}" /></metadata>{suffix}</domain>'
    )


@pytest.mark.parametrize("drift", ["domain_id", "inactive_xml"])
def test_exact_cleanup_refuses_active_domain_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    name = "cleanup-proof"
    domain_uuid = "2cc94a91-eabc-44f4-b2bd-f3f177618fb9"
    run_id = "524e3513-98f1-4406-a152-8d726b396a91"
    plan_digest = "sha256:" + "c" * 64
    expected_xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest)
    actual_xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest, drift=drift == "inactive_xml")
    actual_domain_id = 42 if drift == "domain_id" else 41
    conn = _CleanupTestConnection(name, domain_uuid, actual_xml, actual_domain_id)
    domain = conn.domain
    assert domain is not None
    fake_libvirt = type(
        "FakeLibvirt",
        (),
        {
            "VIR_DOMAIN_XML_INACTIVE": 1,
            "VIR_ERR_NO_DOMAIN": 404,
            "libvirtError": _MissingCleanupDomainError,
        },
    )
    monkeypatch.setattr(kvm, "_libvirt", lambda: fake_libvirt)
    monkeypatch.setattr(oci_root_runtime, "_domain_projection", lambda xml: {"xml": xml})

    with pytest.raises(AssertionError):
        _remove_exact_owned_domain(
            conn,
            name,
            domain_uuid,
            run_id,
            plan_digest,
            41,
            {"xml": expected_xml},
        )

    assert domain.destroy_calls == 0
    assert domain.undefine_calls == 0


def test_exact_cleanup_revalidates_active_domain_before_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "cleanup-proof"
    domain_uuid = "2cc94a91-eabc-44f4-b2bd-f3f177618fb9"
    run_id = "524e3513-98f1-4406-a152-8d726b396a91"
    plan_digest = "sha256:" + "c" * 64
    xml = _cleanup_test_xml(domain_uuid, run_id, plan_digest)
    conn = _CleanupTestConnection(name, domain_uuid, xml, 41)
    domain = conn.domain
    assert domain is not None
    fake_libvirt = type(
        "FakeLibvirt",
        (),
        {
            "VIR_DOMAIN_XML_INACTIVE": 1,
            "VIR_ERR_NO_DOMAIN": 404,
            "libvirtError": _MissingCleanupDomainError,
        },
    )
    monkeypatch.setattr(kvm, "_libvirt", lambda: fake_libvirt)
    monkeypatch.setattr(oci_root_runtime, "_domain_projection", lambda value: {"xml": value})

    _remove_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        41,
        {"xml": xml},
    )

    assert domain.destroy_calls == 1
    assert domain.undefine_calls == 1


def test_live_production_prepare_define_launch_natural_terminal_and_exact_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot, profile = _require_live_host(tmp_path)
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    store = OCIStore(roots, repair_min_age_seconds=0)
    materialization = _proof_materialization(store)
    name = f"pali-live-{uuid.uuid4().hex[:12]}"
    prepared: PreparedOCIRootRun | None = None
    conn = kvm.connect(profile.uri)
    owned_uuid: str | None = None
    owned_domain_id: int | None = None
    plan_digest: str | None = None
    expected_inactive_projection: Mapping[str, Any] | None = None
    try:
        with reserve_new_run(
            roots,
            name,
            DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        ) as reservation:
            prepared = prepare_oci_root_run(
                reservation,
                materialization,
                store,
                root_volume_size_bytes=_ROOT_SIZE_BYTES,
            )
        root_path = prepared.root_volume.path
        before_digest = _sha256_file(root_path)
        resolved = build_oci_root_domain_plan(
            roots,
            prepared,
            store,
            boot,
            profile,
            memory_mib=512,
            vcpus=1,
            network=None,
        )
        plan = commit_oci_root_domain_plan(roots, resolved, store)
        plan_digest = plan.digest
        expected_inactive_projection = oci_root_runtime._domain_projection(resolved.xml)

        assert _lookup_domain(conn, name) is None
        collision = conn.defineXML(resolved.xml)
        assert collision is not None and collision.isActive() == 0
        owned_uuid = collision.UUIDString()
        owned_domain_id = collision.ID()
        assert owned_domain_id == -1
        assert _owner_marker(collision)["id"] == plan.run_id
        with pytest.raises(StateError, match="domain name is already reserved"):
            define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn)
        assert read_run_ledger_snapshot(roots, name).state["status"] == "creating"
        _remove_exact_owned_domain(
            conn,
            name,
            owned_uuid,
            plan.run_id,
            plan.digest,
            owned_domain_id,
            expected_inactive_projection,
        )
        owned_uuid = None
        owned_domain_id = None

        defined = define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn)
        owned_uuid = defined.domain_uuid
        owned_domain_id = -1
        direct_connect_attempts: list[object] = []
        lifecycle_path = roots.runs / name / "lifecycle.sock"
        original_connect = socket.socket.connect

        def reject_direct_lifecycle_connect(instance: socket.socket, address: object) -> object:
            if address == os.fspath(lifecycle_path) or address == lifecycle_path:
                direct_connect_attempts.append(address)
                raise AssertionError("production handoff bypassed libvirt openChannel")
            return original_connect(instance, address)  # type: ignore[arg-type]

        monkeypatch.setattr(socket.socket, "connect", reject_direct_lifecycle_connect)
        completed = launch_defined_oci_root_domain(
            roots,
            name,
            store,
            boot,
            profile,
            conn=conn,
            timeout_seconds=45,
            terminal_timeout_seconds=45,
        )
        owned_domain_id = completed.domain_id

        assert direct_connect_attempts == []
        assert completed.domain_uuid == defined.domain_uuid
        assert completed.terminal.returncode == 101
        assert completed.terminal.exit_code == 101
        assert completed.terminal.signal_number is None
        lifecycle = completed.lifecycle.to_dict()
        assert lifecycle["schema"] == "palimpsest.oci-root-handoff.v1"
        assert lifecycle["phase"] == "terminal"
        assert [entry["kind"] for entry in lifecycle["transcript"]] == [
            "HELLO",
            "BOOTSTRAP",
            "KEY_ACK",
            "READY",
            "TERMINAL",
        ]
        assert "boot_key" not in repr(lifecycle)
        assert "tag" not in repr(lifecycle)

        snapshot = read_run_ledger_snapshot(roots, name)
        assert snapshot.state["status"] == "exited"
        assert snapshot.state["oci_root_handoff"]["phase"] == "terminal"
        domain = conn.lookupByUUIDString(defined.domain_uuid)
        assert domain.UUIDString() == defined.domain_uuid
        assert domain.isActive() == 0
        assert _owner_marker(domain)["contract"] == plan.digest

        verified_root = load_oci_root_volume(roots, prepared.transaction.volume_id)
        assert verified_root.path == root_path
        assert verified_root.record.status == "attached"
        assert _sha256_file(root_path) != before_digest
        debugfs = subprocess.run(
            ["debugfs", "-R", "stat /.palimpsest/upper", os.fspath(root_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert debugfs.returncode == 0, debugfs.stderr[-2000:]
        assert "Type: directory" in debugfs.stdout

        _remove_exact_owned_domain(
            conn,
            name,
            defined.domain_uuid,
            plan.run_id,
            plan.digest,
            owned_domain_id,
            expected_inactive_projection,
        )
        owned_uuid = None
        owned_domain_id = None
        release_prepared_oci_root_run(roots, prepared, store)
        assert read_run_ledger_snapshot(roots, name).state["status"] == "removed"
        assert _lookup_domain(conn, name) is None
        with pytest.raises(StateError, match="record is missing"):
            load_oci_root_volume(roots, prepared.transaction.volume_id)
        assert not root_path.exists()
        assert store.list_lease_set_intents(prepared.transaction.owner) == ()
        prepared = None
    finally:
        if (
            owned_uuid is not None
            and owned_domain_id is not None
            and plan_digest is not None
            and expected_inactive_projection is not None
            and prepared is not None
        ):
            _remove_exact_owned_domain(
                conn,
                name,
                owned_uuid,
                prepared.transaction.owner.run_id,
                plan_digest,
                owned_domain_id,
                expected_inactive_projection,
            )
        if prepared is not None and _lookup_domain(conn, name) is None:
            release_prepared_oci_root_run(roots, prepared, store)
        conn.close()
