"""Opt-in live qualification of the production-inert OCI-root libvirt handoff."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

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
_DAC_BASELABEL_RE = re.compile(r"^\+((?:0|[1-9][0-9]*)):\+((?:0|[1-9][0-9]*))$")
_MAX_DAC_ID = 2**32 - 2


def _strict_scalar(element: ET.Element, *, attributes: dict[str, str] | None = None) -> str:
    if (
        element.attrib != (attributes or {})
        or list(element)
        or element.text is None
        or element.text.strip() != element.text
        or (element.tail is not None and element.tail.strip())
    ):
        raise ValueError("libvirt DAC capability is not canonical")
    return element.text


def _parse_qemu_dac_baselabel(capabilities: str) -> tuple[int, int]:
    if not isinstance(capabilities, str):
        raise ValueError("libvirt capabilities are invalid")
    try:
        root = ET.fromstring(capabilities)
    except ET.ParseError:
        raise ValueError("libvirt capabilities are invalid") from None
    if root.tag != "capabilities" or root.attrib or (root.text is not None and root.text.strip()):
        raise ValueError("libvirt capabilities root is invalid")
    hosts = [child for child in root if child.tag == "host"]
    if len(hosts) != 1:
        raise ValueError("libvirt capabilities host is ambiguous")
    host = hosts[0]
    dac_models: list[ET.Element] = []
    for secmodel in (child for child in host if child.tag == "secmodel"):
        models = [child for child in secmodel if child.tag == "model"]
        if any(model.text == "dac" for model in models):
            dac_models.append(secmodel)
    if len(dac_models) != 1:
        raise ValueError("libvirt DAC capability is ambiguous")
    secmodel = dac_models[0]
    if secmodel.attrib or (secmodel.text is not None and secmodel.text.strip()):
        raise ValueError("libvirt DAC capability is not canonical")
    models = [child for child in secmodel if child.tag == "model"]
    if len(models) != 1 or _strict_scalar(models[0]) != "dac":
        raise ValueError("libvirt DAC capability is not canonical")
    dois = [child for child in secmodel if child.tag == "doi"]
    if len(dois) != 1 or _strict_scalar(dois[0]) != "0":
        raise ValueError("libvirt DAC capability is not canonical")
    labels = [child for child in secmodel if child.tag == "baselabel" and child.get("type") == "kvm"]
    if len(labels) != 1:
        raise ValueError("libvirt KVM DAC baselabel is ambiguous")
    encoded = _strict_scalar(labels[0], attributes={"type": "kvm"})
    match = _DAC_BASELABEL_RE.fullmatch(encoded)
    if match is None:
        raise ValueError("libvirt KVM DAC baselabel is invalid")
    uid, gid = (int(value) for value in match.groups())
    if uid > _MAX_DAC_ID or gid > _MAX_DAC_ID:
        raise ValueError("libvirt KVM DAC baselabel is invalid")
    return uid, gid


def _stat_identity(item: os.stat_result) -> tuple[int, ...]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )


def _validate_qualification_acl_target(root: Path, target: Path, *, kind: str) -> tuple[Path, os.stat_result]:
    """Return a closed-FD identity snapshot, not an ACL mutation authority.

    A future path-based ``setfacl`` call must repeat this walk immediately before
    mutation and still cannot make the validation/mutation pair atomic.  Keeping
    the final descriptor open and addressing ``/proc/self/fd/<n>`` narrows that
    gap on Linux, but ``setfacl`` itself remains pathname-based.
    """

    if kind not in {"directory", "regular"} or not root.is_absolute() or not target.is_absolute():
        raise ValueError("qualification ACL target is invalid")
    try:
        root_lexical = Path(os.path.abspath(root))
        target_lexical = Path(os.path.abspath(target))
        root_resolved = root.resolve(strict=True)
        target_resolved = target.resolve(strict=True)
        relative = target_lexical.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("qualification ACL target escapes the test root") from None
    if root_lexical != root_resolved or target_lexical != target_resolved:
        raise ValueError("qualification ACL target contains a symlink")
    descriptors: list[int] = []
    try:
        root_visible = root_resolved.lstat()
        root_fd = os.open(root_resolved, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
        descriptors.append(root_fd)
        root_opened = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_visible.st_mode)
            or not stat.S_ISDIR(root_opened.st_mode)
            or (root_visible.st_dev, root_visible.st_ino) != (root_opened.st_dev, root_opened.st_ino)
            or root_visible.st_uid != os.geteuid()
            or root_opened.st_uid != os.geteuid()
        ):
            raise ValueError("qualification ACL root identity is unsafe")
        opened = root_opened
        parent_fd = root_fd
        for index, component in enumerate(relative.parts):
            expected_kind = kind if index == len(relative.parts) - 1 else "directory"
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if expected_kind == "directory":
                flags |= os.O_DIRECTORY
            visible = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            child_fd = os.open(component, flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            opened = os.fstat(child_fd)
            predicate = stat.S_ISDIR if expected_kind == "directory" else stat.S_ISREG
            if (
                not predicate(visible.st_mode)
                or not predicate(opened.st_mode)
                or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
                or visible.st_uid != os.geteuid()
                or opened.st_uid != os.geteuid()
            ):
                raise ValueError("qualification ACL target identity is unsafe")
            parent_fd = child_fd
        final = target_resolved.lstat()
    except ValueError:
        raise
    except OSError:
        raise ValueError("qualification ACL target cannot be securely opened") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    predicate = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if (
        not predicate(opened.st_mode)
        or not predicate(final.st_mode)
        or (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino)
        or opened.st_uid != os.geteuid()
        or final.st_uid != os.geteuid()
    ):
        raise ValueError("qualification ACL target identity is unsafe")
    return target_resolved, opened


def _remove_failed_kernel_copy(destination: Path, expected: os.stat_result) -> None:
    try:
        visible = destination.lstat()
    except OSError:
        raise ValueError("failed qualified kernel copy identity changed") from None
    if (
        not stat.S_ISREG(visible.st_mode)
        or visible.st_uid != os.geteuid()
        or (visible.st_dev, visible.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ValueError("failed qualified kernel copy identity changed")
    try:
        destination.unlink()
    except OSError:
        raise ValueError("failed qualified kernel copy could not be removed") from None


def _copy_qualified_kernel(source: Path, test_root: Path) -> Path:
    try:
        source = source.resolve(strict=True)
        visible = source.lstat()
        source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("qualified kernel cannot be securely opened") from None
    destination = test_root / "k"
    destination_fd: int | None = None
    created: os.stat_result | None = None
    try:
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(visible.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError("qualified kernel identity is unsafe")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                created = os.fstat(destination_fd)
            except OSError:
                raise ValueError(
                    "qualified kernel copy identity is unavailable; partial destination was preserved"
                ) from None
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise ValueError("qualified kernel copy did not make progress")
                    view = view[written:]
            os.fchmod(destination_fd, 0o400)
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
            current = source.lstat()
            if _stat_identity(after) != _stat_identity(opened) or _stat_identity(current) != _stat_identity(opened):
                raise ValueError("qualified kernel changed while it was copied")
            _, validated = _validate_qualification_acl_target(test_root, destination, kind="regular")
            destination_current = os.fstat(destination_fd)
            if not (
                (validated.st_dev, validated.st_ino)
                == (created.st_dev, created.st_ino)
                == (destination_current.st_dev, destination_current.st_ino)
            ):
                raise ValueError("qualified kernel copy destination identity changed")
            if stat.S_IMODE(destination_current.st_mode) != 0o400:
                raise ValueError("qualified kernel copy mode is unsafe")
        except (OSError, ValueError) as exc:
            if created is None:
                # O_EXCL proves that this call created some directory entry, but
                # without an FD identity a same-UID replacement is
                # indistinguishable.  Preserve the path instead of unlinking an
                # inode that this call may not own.
                if isinstance(exc, ValueError):
                    raise
                raise ValueError(
                    "qualified kernel copy identity is unavailable; partial destination was preserved"
                ) from None
            try:
                _remove_failed_kernel_copy(destination, created)
            except ValueError as cleanup_error:
                raise cleanup_error from exc
            if isinstance(exc, ValueError):
                raise
            raise ValueError("qualified kernel cannot be securely copied") from None
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    return destination


_DAC_CAPABILITIES = """\
<capabilities>
  <host>
    <secmodel>
      <model>dac</model>
      <doi>0</doi>
      <baselabel type="qemu">+64055:+64055</baselabel>
      <baselabel type="kvm">+107:+108</baselabel>
    </secmodel>
  </host>
</capabilities>
"""


def test_qemu_dac_baselabel_parser_accepts_one_exact_kvm_identity() -> None:
    assert _parse_qemu_dac_baselabel(_DAC_CAPABILITIES) == (107, 108)


@pytest.mark.parametrize(
    "capabilities",
    [
        _DAC_CAPABILITIES.replace("<capabilities>", "<domainCapabilities>", 1).replace(
            "</capabilities>", "</domainCapabilities>", 1
        ),
        _DAC_CAPABILITIES.replace("</capabilities>", "<host /></capabilities>"),
        _DAC_CAPABILITIES.replace("</host>", "<secmodel><model>dac</model></secmodel></host>"),
        _DAC_CAPABILITIES.replace("<model>dac</model>", "<model>dac</model><model>dac</model>"),
        _DAC_CAPABILITIES.replace("<doi>0</doi>", "<doi>1</doi>"),
        _DAC_CAPABILITIES.replace('type="kvm"', 'type="kvm" extra="1"'),
        _DAC_CAPABILITIES.replace("</secmodel>", '<baselabel type="kvm">+109:+110</baselabel></secmodel>'),
        _DAC_CAPABILITIES.replace("+107:+108", "107:108"),
        _DAC_CAPABILITIES.replace("+107:+108", "+0107:+108"),
        _DAC_CAPABILITIES.replace("+107:+108", "+4294967295:+108"),
        _DAC_CAPABILITIES.replace('type="kvm"', 'type="xen"'),
    ],
)
def test_qemu_dac_baselabel_parser_rejects_ambiguous_or_noncanonical_input(capabilities: str) -> None:
    with pytest.raises(ValueError, match="libvirt"):
        _parse_qemu_dac_baselabel(capabilities)


def test_qualification_acl_target_requires_owned_stable_tmp_descendant(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    artifact = directory / "root.raw"
    artifact.write_bytes(b"root")

    resolved_directory, directory_identity = _validate_qualification_acl_target(tmp_path, directory, kind="directory")
    resolved_artifact, artifact_identity = _validate_qualification_acl_target(tmp_path, artifact, kind="regular")

    assert resolved_directory == directory.resolve()
    assert stat.S_ISDIR(directory_identity.st_mode)
    assert resolved_artifact == artifact.resolve()
    assert stat.S_ISREG(artifact_identity.st_mode)


def test_qualification_acl_target_rejects_escape_symlink_and_wrong_kind(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    artifact = directory / "root.raw"
    artifact.write_bytes(b"root")
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        _validate_qualification_acl_target(tmp_path, tmp_path.parent, kind="directory")
    with pytest.raises(ValueError, match="symlink"):
        _validate_qualification_acl_target(tmp_path, alias / artifact.name, kind="regular")
    with pytest.raises(ValueError, match="securely opened"):
        _validate_qualification_acl_target(tmp_path, artifact, kind="directory")


def test_qualification_acl_target_openat_walk_rejects_intermediate_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    displaced = tmp_path / "displaced"
    directory.mkdir()
    artifact = directory / "root.raw"
    artifact.write_bytes(b"root")
    original_open = os.open
    injected = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal injected
        if path == "run" and kwargs.get("dir_fd") is not None and not injected:
            injected = True
            directory.rename(displaced)
            directory.mkdir()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(ValueError, match="identity"):
        _validate_qualification_acl_target(tmp_path, artifact, kind="regular")
    assert injected is True


def test_qualified_kernel_copy_is_owner_only_and_leaves_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    source.chmod(0o444)
    source_before = source.stat()

    copied = _copy_qualified_kernel(source, tmp_path)

    source_after = source.stat()
    assert copied.name == "k"
    assert copied.read_bytes() == b"kernel"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o400
    assert (source_after.st_dev, source_after.st_ino, source_after.st_mode) == (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_mode,
    )


@pytest.mark.parametrize("failure", ["read", "write", "fsync", "source-recheck", "final-validation"])
def test_qualified_kernel_copy_removes_exact_partial_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"

    def raise_oserror(*_args):
        raise OSError(failure)

    if failure == "read":
        monkeypatch.setattr(os, "read", raise_oserror)
    elif failure == "write":
        monkeypatch.setattr(os, "write", raise_oserror)
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", raise_oserror)
    elif failure == "source-recheck":
        original_lstat = Path.lstat
        source_stats = 0

        def mutate_source(path: Path):
            nonlocal source_stats
            if path == source:
                source_stats += 1
                if source_stats == 2:
                    source.write_bytes(b"changed")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", mutate_source)
    else:

        def fail_validation(*_args, **_kwargs):
            raise ValueError("injected final validation failure")

        monkeypatch.setitem(globals(), "_validate_qualification_acl_target", fail_validation)

    with pytest.raises(ValueError):
        _copy_qualified_kernel(source, tmp_path)

    assert not destination.exists()


def test_qualified_kernel_copy_does_not_remove_replaced_destination_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"

    def replace_then_fail(_descriptor: int) -> None:
        destination.unlink()
        destination.write_bytes(b"replacement")
        raise OSError("fsync")

    monkeypatch.setattr(os, "fsync", replace_then_fail)

    with pytest.raises(ValueError, match="identity changed"):
        _copy_qualified_kernel(source, tmp_path)

    assert destination.read_bytes() == b"replacement"


def test_qualified_kernel_copy_rejects_same_uid_replacement_on_success_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"
    original_validator = _validate_qualification_acl_target

    def replace_during_validation(root: Path, target: Path, *, kind: str):
        destination.unlink()
        destination.write_bytes(b"replacement")
        return original_validator(root, target, kind=kind)

    monkeypatch.setitem(globals(), "_validate_qualification_acl_target", replace_during_validation)

    with pytest.raises(ValueError, match="identity changed"):
        _copy_qualified_kernel(source, tmp_path)

    assert destination.read_bytes() == b"replacement"


def test_qualified_kernel_copy_preserves_partial_when_initial_fd_identity_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"kernel")
    destination = tmp_path / "k"
    original_fstat = os.fstat
    calls = 0

    def fail_destination_fstat(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected destination fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", fail_destination_fstat)

    with pytest.raises(ValueError, match="identity is unavailable.*preserved"):
        _copy_qualified_kernel(source, tmp_path)

    assert destination.is_file()
    assert destination.read_bytes() == b""


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
    kernel_path = _copy_qualified_kernel(Path(kernel_value), tmp_path.resolve(strict=True))
    verified = verify_host_boot_artifacts(
        kernel_path,
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
    expected_inactive_projection_digest: str,
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
        assert (
            oci_root_runtime._projection_digest(oci_root_runtime._domain_projection(inactive_xml))
            == expected_inactive_projection_digest
        )
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
    expected_inactive_projection_digest: str,
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
        expected_inactive_projection_digest,
    )
    if active == 1:
        assert expected_domain_id > 0 and domain_id == expected_domain_id
        domain, active, domain_id = _inspect_exact_owned_domain(
            conn,
            name,
            domain_uuid,
            run_id,
            plan_digest,
            expected_inactive_projection_digest,
        )
        assert active == 1 and domain_id == expected_domain_id
        domain.destroy()
    domain, active, domain_id = _inspect_exact_owned_domain(
        conn,
        name,
        domain_uuid,
        run_id,
        plan_digest,
        expected_inactive_projection_digest,
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
            oci_root_runtime._projection_digest({"xml": expected_xml}),
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
        oci_root_runtime._projection_digest({"xml": xml}),
    )

    assert domain.destroy_calls == 1
    assert domain.undefine_calls == 1


def test_live_oci_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot, profile = _require_live_host(tmp_path)
    roots = init_resolved_roots(StatePaths(tmp_path / "c", tmp_path / "s"))
    store = OCIStore(roots, repair_min_age_seconds=0)
    materialization = _proof_materialization(store)
    name = f"p-{uuid.uuid4().hex[:6]}"
    lifecycle_path = roots.runs / name / "lifecycle.sock"
    assert len(os.fsencode(lifecycle_path)) <= kvm.LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES - 10
    prepared: PreparedOCIRootRun | None = None
    conn = kvm.connect(profile.uri)
    qemu_uid, qemu_gid = _parse_qemu_dac_baselabel(conn.getCapabilities())
    assert 0 <= qemu_uid <= _MAX_DAC_ID
    assert 0 <= qemu_gid <= _MAX_DAC_ID
    owned_uuid: str | None = None
    owned_domain_id: int | None = None
    plan_digest: str | None = None
    expected_inactive_projection_digest: str | None = None
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
        assert _lookup_domain(conn, name) is None
        collision = conn.defineXML(resolved.xml)
        assert collision is not None and collision.isActive() == 0
        owned_uuid = collision.UUIDString()
        owned_domain_id = collision.ID()
        assert owned_domain_id == -1
        assert _owner_marker(collision)["id"] == plan.run_id
        inactive_flag = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
        assert type(inactive_flag) is int
        expected_inactive_projection_digest = oci_root_runtime._validated_post_define_projection(
            conn,
            resolved,
            collision.XMLDesc(inactive_flag),
        )
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
            expected_inactive_projection_digest,
        )
        owned_uuid = None
        owned_domain_id = None

        defined = define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn)
        owned_uuid = defined.domain_uuid
        owned_domain_id = -1
        definition = read_run_ledger_snapshot(roots, name).state["oci_root_definition"]
        assert definition["schema"] == "palimpsest.oci-root-definition.v2"
        expected_inactive_projection_digest = definition["projection_digest"]
        direct_connect_attempts: list[object] = []
        original_connect = socket.socket.connect

        def reject_direct_lifecycle_connect(instance: socket.socket, address: object) -> object:
            if address == os.fspath(lifecycle_path) or address == lifecycle_path:
                direct_connect_attempts.append(address)
                raise AssertionError("production handoff bypassed libvirt openChannel")
            return original_connect(instance, address)  # type: ignore[arg-type]

        monkeypatch.setattr(socket.socket, "connect", reject_direct_lifecycle_connect)
        # A future qualification-only DAC broker belongs in a conn/domain proxy
        # around domain.create(): production revalidation must see original modes,
        # and ambiguous activation must retain ACLs and backing files.  Effective
        # POSIX ACLs cannot satisfy the former while they are installed because
        # the ACL mask is exposed as the group mode bits.
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
            expected_inactive_projection_digest,
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
            and expected_inactive_projection_digest is not None
            and prepared is not None
        ):
            _remove_exact_owned_domain(
                conn,
                name,
                owned_uuid,
                prepared.transaction.owner.run_id,
                plan_digest,
                owned_domain_id,
                expected_inactive_projection_digest,
            )
        if prepared is not None and _lookup_domain(conn, name) is None:
            release_prepared_oci_root_run(roots, prepared, store)
        conn.close()
