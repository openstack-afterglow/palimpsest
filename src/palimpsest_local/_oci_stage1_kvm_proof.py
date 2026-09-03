"""Qualified native-KVM proof for the fail-closed OCI stage-1 consumer.

This private module is test support for the packaged bootstrap boundary. It
does not define or start a production VM. It proves authenticated filesystem
mounts, an initramfs-safe move-mount/chroot root transition, and the narrow
first-party PID 1 workload supervisor contract.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import gzip
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError, StateError
from .oci_control_protocol import (
    OCI_CONTROL_CHANNEL_NAME,
    OCI_CONTROL_PROTOCOL,
    HostOCIControlSession,
    OCIControlBinding,
    OCIControlFrameDecoder,
    OCIControlMessage,
    OCIControlProtocolError,
    encode_frame,
)
from .oci_guest_filesystems import (
    EXT4_SUPERBLOCK_BYTES,
    EXT4_SUPERBLOCK_OFFSET,
    ext4_primary_superblock_checksum,
    verify_ext4_superblock,
    verify_lower_device,
)
from .oci_initramfs import (
    OCI_BOOTSTRAP_STAGE1_CONTRACT,
    OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT,
    OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
    OCI_STAGE1_SUPERVISOR_CONTRACT,
    build_bootstrap_initramfs,
)
from .oci_process import OCIProcessSpec, OCIUserSpec
from .oci_provenance import canonical_json_bytes
from .oci_stage1 import OCIStage1Plan, oci_stage1_device_serial
from .oci_stage1_transport import BuiltOCIStage1Transport, OCIStage1TransportReceipt, build_stage1_transport

OCI_STAGE1_KVM_PROOF_SCHEMA = "palimpsest.oci-stage1-kvm-proof.v13"
KVM_GET_API_VERSION = 0xAE00
REQUIRED_KVM_API_VERSION = 12
MAX_KERNEL_BYTES = 128 * 1024 * 1024
MAX_KERNEL_CONFIG_BYTES = 4 * 1024 * 1024
MAX_QEMU_VERSION_BYTES = 16 * 1024
MAX_CONSOLE_BYTES = 4 * 1024 * 1024
DEFAULT_BOOT_TIMEOUT_SECONDS = 45.0
# Do not include the line ending: a real 8250 console commonly maps LF to
# CRLF, while the pipe-based pure test preserves LF exactly.
ROOT_TRANSITION_MARKER = b"palimpsest guest stage1: root transition complete; root is slash; workload pending"
WORKLOAD_STARTED_MARKER = b"palimpsest guest stage1: workload started; root is slash; supervisor active"
WORKLOAD_SIGNAL_ARMED_MARKER = b"palimpsest workload proof: signal handlers armed"
WORKLOAD_STOP_OBSERVED_MARKER = b"palimpsest workload proof: stop observed"
LIFECYCLE_READY_COMMITTED_MARKER = b"palimpsest guest stage1: lifecycle ready committed"
LIFECYCLE_STOP_DISPATCHED_MARKER = b"palimpsest guest stage1: lifecycle stop dispatched"
LIFECYCLE_STOP_DUPLICATE_MARKER = b"palimpsest guest stage1: lifecycle stop duplicate accepted"
WORKLOAD_TERMINAL_PREFIX = b"palimpsest guest stage1: workload terminal; main_status="
WORKLOAD_TERMINAL_MARKER = (
    b"palimpsest guest stage1: workload terminal; main_status=42; cooperative_status=43; "
    b"forced_status=137; reaped=3; forwarded=15; pid1_uid=0; pid1_gid=0; pid1_groups=0; "
    b"cleanup=cgroup.kill; cgroup_populated=0; waiting fail-closed"
)
SUCCESS_MARKER = WORKLOAD_TERMINAL_MARKER
REJECTION_MARKER = b"palimpsest guest stage1: pre-mount contract rejected; waiting fail-closed"
FILESYSTEM_REJECTION_MARKER = (
    b"palimpsest guest stage1: filesystem contract rejected; mount disabled; waiting fail-closed"
)
PREPARATION_FAILURE_MARKER = b"palimpsest guest stage1: bootstrap preparation failed; waiting fail-closed"
ASSEMBLY_REJECTION_MARKER = (
    b"palimpsest guest stage1: mount or staging assembly rejected; root is not slash; "
    b"pivot and workload disabled; waiting fail-closed"
)
ROOT_TRANSITION_REJECTION_MARKER = (
    b"palimpsest guest stage1: root transition rejected; root state is indeterminate; "
    b"workload disabled; waiting fail-closed"
)
WORKLOAD_REJECTION_PREFIX = b"palimpsest guest stage1: workload launch rejected; stage="
WORKLOAD_CLEANUP_REJECTION_PREFIX = b"palimpsest guest stage1: workload cleanup rejected; stage="
LIFECYCLE_REJECTION_PREFIX = b"palimpsest guest stage1: lifecycle rejected; stage="
WORKLOAD_NEGATIVE_CONTROL_NAMES = (
    "workload_missing_executable",
    "workload_non_executable",
    "workload_missing_cwd",
)
LIFECYCLE_NEGATIVE_CONTROL_NAMES = (
    "lifecycle_missing_port",
    "lifecycle_wrong_name_only",
    "hello_zero_length",
    "hello_oversized_length",
    "hello_duplicate_key_noncanonical",
    "hello_wrong_domain_core_binding",
    "hello_reused_nonce",
    "stop_stale_generation",
    "stop_request_id_collides_with_hello",
    "second_distinct_stop",
)
LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES = LIFECYCLE_NEGATIVE_CONTROL_NAMES[:2]
LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES = LIFECYCLE_NEGATIVE_CONTROL_NAMES[2:]
LIFECYCLE_NEGATIVE_NONCE = "a" * 64
LIFECYCLE_NEGATIVE_FRESH_NONCE = "b" * 64
LIFECYCLE_WRONG_CHANNEL_NAME = "org.palimpsest.oci.lifecycle.wrong"
QEMU_DUPLICATE_NAME_REJECTION_MARKER = (
    b"virtio-serial-bus: A port already exists by name org.palimpsest.oci.lifecycle.0"
)
WORKLOAD_NEGATIVE_REJECTION_MARKERS = {
    "workload_missing_executable": WORKLOAD_REJECTION_PREFIX
    + b"7; errno=2; started and terminal disabled; waiting fail-closed",
    "workload_non_executable": WORKLOAD_REJECTION_PREFIX
    + b"7; errno=13; started and terminal disabled; waiting fail-closed",
    "workload_missing_cwd": WORKLOAD_REJECTION_PREFIX
    + b"6; errno=2; started and terminal disabled; waiting fail-closed",
}

KERNEL_ENV = "PALIMPSEST_KVM_KERNEL"
KERNEL_CONFIG_ENV = "PALIMPSEST_KVM_KERNEL_CONFIG"
QEMU_ENV = "PALIMPSEST_KVM_QEMU"
EVIDENCE_ENV = "PALIMPSEST_KVM_EVIDENCE_DIR"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERIAL_RE = re.compile(r"^[0-9a-f]{20}$")
NEGATIVE_CONTROL_NAMES = (
    "writable_transport",
    "missing_root",
    "wrong_root_serial",
    "readonly_root",
    "root_size_smaller",
    "root_size_larger",
    "missing_lower",
    "wrong_lower_serial",
    "writable_lower",
    "lower_size_smaller",
    "lower_size_larger",
    "duplicate_serial",
    "extra_disk",
)
FILESYSTEM_NEGATIVE_CONTROL_NAMES = (
    "root_bad_magic",
    "root_wrong_label",
    "root_geometry",
    "lower_bad_magic",
    "lower_bad_structure",
    "lower_digest_mismatch",
)
ASSEMBLY_NEGATIVE_CONTROL_NAMES = (
    "probe_missing",
    "probe_size_mismatch",
    "probe_digest_mismatch",
)
ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES = (
    "transition_dev_not_directory",
    "transition_sys_not_directory",
    "transition_proc_not_directory",
)
KVM_PROOF_BOOT_COUNT = (
    2
    + len(NEGATIVE_CONTROL_NAMES)
    + len(FILESYSTEM_NEGATIVE_CONTROL_NAMES)
    + len(ASSEMBLY_NEGATIVE_CONTROL_NAMES)
    + len(ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES)
    + len(WORKLOAD_NEGATIVE_CONTROL_NAMES)
    + len(LIFECYCLE_NEGATIVE_CONTROL_NAMES)
)
EVIDENCE_FILE_NAMES = (
    "console.bin",
    "retained-console.bin",
    "receipt.json",
    *(f"negative-{name}.bin" for name in NEGATIVE_CONTROL_NAMES),
    *(f"filesystem-negative-{name}.bin" for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES),
    *(f"assembly-negative-{name}.bin" for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES),
    *(f"root-transition-negative-{name}.bin" for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES),
    *(f"workload-negative-{name}.bin" for name in WORKLOAD_NEGATIVE_CONTROL_NAMES),
    *(f"lifecycle-negative-{name}.bin" for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES),
    "qemu-duplicate-lifecycle-name.bin",
)
_REQUIRED_KERNEL_CONFIG = (
    "CONFIG_64BIT",
    "CONFIG_BINFMT_ELF",
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_CGROUPS",
    "CONFIG_DEVTMPFS",
    "CONFIG_EXT4_FS",
    "CONFIG_OVERLAY_FS",
    "CONFIG_PCI",
    "CONFIG_PROC_FS",
    "CONFIG_SERIAL_8250",
    "CONFIG_SERIAL_8250_CONSOLE",
    "CONFIG_SQUASHFS",
    "CONFIG_SQUASHFS_XATTR",
    "CONFIG_SQUASHFS_ZLIB",
    "CONFIG_SQUASHFS_ZSTD",
    "CONFIG_SYSFS",
    "CONFIG_VIRTIO",
    "CONFIG_VIRTIO_BLK",
    "CONFIG_VIRTIO_CONSOLE",
    "CONFIG_HW_RANDOM",
    "CONFIG_HW_RANDOM_VIRTIO",
    "CONFIG_VIRTIO_PCI",
)


class KVMProofUnavailable(StateError):
    """The host cannot make the narrowly qualified native-KVM claim."""


class KVMProofFailure(StateError):
    """A qualified proof attempt failed closed."""


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ProofFilesystemSet:
    root: bytes
    lowers: tuple[bytes, bytes]
    root_digest: str
    lower_digests: tuple[str, str]
    transition_lowers: Mapping[str, bytes]
    transition_lower_digests: Mapping[str, str]
    filesystem_uuid: str
    manifest_digest: str


_PROOF_FILESYSTEM_MANIFEST_DIGEST = "sha256:22994200aeb8559cdcb7eae9d4a47a813c4be6d82b32b82b0684ada8b81c695c"
_PROOF_ASSEMBLY_PROBE = {
    "digest": "sha256:f6f8a6d4cc482c9589ab87159165dab15c4802ace3f3759325144f2734fa761a",
    "path": "/.__palimpsest_overlay_order_probe_v1",
    "size_bytes": 10,
    "top_ordinal": 1,
}


def verify_proof_filesystem_manifest(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise ArtifactValidationError("KVM filesystem fixture policy is invalid")
    digest = _digest(canonical_json_bytes(value))
    if (
        digest != _PROOF_FILESYSTEM_MANIFEST_DIGEST
        or value.get("schema") != "palimpsest.kvm-filesystem-fixtures.v8"
        or value.get("policy") != "palimpsest.kvm-actual-filesystem-fixtures.v8"
        or value.get("assembly_probe") != _PROOF_ASSEMBLY_PROBE
    ):
        raise ArtifactValidationError("KVM filesystem fixture policy is invalid")
    return digest


def _verify_workload_proof_provenance(
    repository: Path,
    fixture_directory: Path,
    provenance: Mapping[str, Any],
) -> None:
    expected = {
        "build_script": "scripts/build_oci_guest_workload_proof.sh",
        "build_script_sha256": "4f88223bc5cf8b853254a229187f55d6c3cbf6c31992ee0008c8f797bf43e25d",
        "elf_mode": 0o755,
        "elf_sha256": "d70514b6ea07ef566fb85503b1c858cf7292235434c6772b9c49ef7c69d8ac12",
        "elf_size_bytes": 9044,
        "source": "guest/workload-proof/proof.c",
        "source_sha256": "3276e256b16dc30d61d0c4a787b2e0a0ae9b669d493b0936bd97387043f60a00",
        "toolchain": "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1",
    }
    if not isinstance(provenance, Mapping) or dict(provenance) != expected:
        raise ArtifactValidationError("KVM workload proof provenance is invalid")
    for path, digest, mode, size in (
        (
            repository / expected["source"],
            f"sha256:{expected['source_sha256']}",
            0o644,
            None,
        ),
        (
            repository / expected["build_script"],
            f"sha256:{expected['build_script_sha256']}",
            0o755,
            None,
        ),
        (
            fixture_directory / "workload-proof.x86_64",
            f"sha256:{expected['elf_sha256']}",
            expected["elf_mode"],
            expected["elf_size_bytes"],
        ),
    ):
        verified = _read_pinned_regular_file(path.absolute(), maximum=65 * 1024, label="KVM workload proof")
        if verified.digest != digest or stat.S_IMODE(verified.mode) != mode or (size and verified.size_bytes != size):
            raise ArtifactValidationError("KVM workload proof provenance binding is invalid")


def _verify_fixture_source_tree(source_root: Path, sources: list[dict[str, Any]]) -> None:
    expected_source_names = {source["path"] for source in sources}
    try:
        source_root_stat = source_root.lstat()
        actual_source_names = {entry.relative_to(source_root).as_posix() for entry in source_root.rglob("*")}
    except OSError:
        raise ArtifactValidationError("KVM SquashFS fixture source root is unavailable") from None
    if (
        not stat.S_ISDIR(source_root_stat.st_mode)
        or stat.S_IMODE(source_root_stat.st_mode) != 0o755
        or actual_source_names != expected_source_names
    ):
        raise ArtifactValidationError("KVM SquashFS fixture source tree is invalid")
    for source in sources:
        source_path = source_root / source["path"]
        expected_type = source.get("type")
        try:
            visible = source_path.lstat()
        except OSError:
            raise ArtifactValidationError("KVM SquashFS fixture source is unavailable") from None
        if expected_type == "directory":
            if set(source) != {"mode", "path", "type"} or not stat.S_ISDIR(visible.st_mode):
                raise ArtifactValidationError("KVM SquashFS fixture source binding is invalid")
            source_stat = visible
        elif expected_type == "file" and set(source) == {"mode", "path", "sha256", "size_bytes", "type"}:
            try:
                with source_path.open("rb") as opened:
                    source_stat = os.fstat(opened.fileno())
                    source_payload = opened.read(65 * 1024)
            except OSError:
                raise ArtifactValidationError("KVM SquashFS fixture source is unavailable") from None
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or (visible.st_dev, visible.st_ino) != (source_stat.st_dev, source_stat.st_ino)
                or source_stat.st_nlink != 1
                or len(source_payload) != source["size_bytes"]
                or hashlib.sha256(source_payload).hexdigest() != source["sha256"]
            ):
                raise ArtifactValidationError("KVM SquashFS fixture source binding is invalid")
        else:
            raise ArtifactValidationError("KVM SquashFS fixture source binding is invalid")
        if stat.S_IMODE(source_stat.st_mode) != source["mode"]:
            raise ArtifactValidationError("KVM SquashFS fixture source binding is invalid")


@lru_cache(maxsize=1)
def load_proof_filesystems() -> ProofFilesystemSet:
    """Load exact source-controlled outputs from real mkfs tools."""

    repository = Path(__file__).resolve().parents[2]
    directory = repository / "tests" / "kvm" / "assets"
    try:
        metadata = json.loads((directory / "filesystem-fixtures.json").read_text(encoding="ascii"))
        artifacts = metadata["artifacts"]
        compressed_root = base64.b64decode(
            "".join((directory / artifacts["root"]["base64_file"]).read_text(encoding="ascii").split()),
            validate=True,
        )
        root = gzip.decompress(compressed_root)
        lowers = tuple(
            base64.b64decode(
                "".join((directory / artifacts[name]["base64_file"]).read_text(encoding="ascii").split()),
                validate=True,
            )
            for name in ("lower0", "lower1")
        )
        transition_lowers = {
            name.removeprefix("transition_"): base64.b64decode(
                "".join((directory / artifacts[name]["base64_file"]).read_text(encoding="ascii").split()),
                validate=True,
            )
            for name in ("transition_dev", "transition_sys", "transition_proc")
        }
    except (OSError, ValueError, KeyError, TypeError, gzip.BadGzipFile, json.JSONDecodeError):
        raise ArtifactValidationError("KVM filesystem fixtures are unavailable") from None
    manifest_digest = verify_proof_filesystem_manifest(metadata)
    _verify_workload_proof_provenance(repository, directory, metadata["provenance"]["workload_proof"])
    root_meta = artifacts["root"]
    if (
        _digest(compressed_root) != f"sha256:{root_meta['compressed_sha256']}"
        or len(root) != root_meta["raw_size_bytes"]
        or _digest(root) != f"sha256:{root_meta['raw_sha256']}"
        or len(lowers) != 2
    ):
        raise ArtifactValidationError("KVM ext4 fixture binding is invalid")
    filesystem_uuid = root_meta["filesystem_uuid"]
    identity = verify_ext4_superblock(
        root[EXT4_SUPERBLOCK_OFFSET : EXT4_SUPERBLOCK_OFFSET + EXT4_SUPERBLOCK_BYTES],
        device_size=len(root),
        volume_id="1fd7a60e-fdb2-4877-91d3-148bbca3884f",
        filesystem_uuid=filesystem_uuid,
    )
    if identity.label != root_meta["filesystem_label"]:
        raise ArtifactValidationError("KVM ext4 fixture label is invalid")
    lower_digests: list[str] = []
    for name, payload in zip(("lower0", "lower1"), lowers, strict=True):
        item = artifacts[name]
        digest = f"sha256:{item['raw_sha256']}"
        if len(payload) != item["raw_size_bytes"] or _digest(payload) != digest:
            raise ArtifactValidationError("KVM SquashFS fixture binding is invalid")
        identity = verify_lower_device(payload, expected_digest=digest)
        if identity.compression != item["compression_id"]:
            raise ArtifactValidationError("KVM SquashFS fixture compression is invalid")
        source_root = directory / item["source_root"]
        _verify_fixture_source_tree(source_root, item["source_entries"])
        lower_digests.append(digest)
    transition_lower_digests: dict[str, str] = {}
    for role, payload in transition_lowers.items():
        name = f"transition_{role}"
        item = artifacts[name]
        digest = f"sha256:{item['raw_sha256']}"
        if len(payload) != item["raw_size_bytes"] or _digest(payload) != digest:
            raise ArtifactValidationError("KVM transition SquashFS fixture binding is invalid")
        identity = verify_lower_device(payload, expected_digest=digest)
        if identity.compression != item["compression_id"]:
            raise ArtifactValidationError("KVM transition SquashFS compression is invalid")
        _verify_fixture_source_tree(directory / item["source_root"], item["source_entries"])
        transition_lower_digests[role] = digest
    return ProofFilesystemSet(
        root,
        (lowers[0], lowers[1]),
        _digest(root),
        tuple(lower_digests),
        transition_lowers,
        transition_lower_digests,
        filesystem_uuid,
        manifest_digest,
    )


def _zero_digest(size_bytes: int) -> str:
    if type(size_bytes) is not int or not 1 <= size_bytes <= MAX_KERNEL_BYTES:
        raise ArtifactValidationError("KVM proof zero backing size is invalid")
    hasher = hashlib.sha256()
    chunk = b"\0" * min(size_bytes, 1024 * 1024)
    remaining = size_bytes
    while remaining:
        piece = chunk[: min(len(chunk), remaining)]
        hasher.update(piece)
        remaining -= len(piece)
    return f"sha256:{hasher.hexdigest()}"


def _logical_line_count(payload: bytes, marker: bytes) -> int:
    """Count complete LF/CRLF console lines exactly equal to ``marker``."""

    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sum(line == marker for line in normalized.split(b"\n")[:-1])


def _logical_prefix_count(payload: bytes, prefix: bytes) -> int:
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sum(line.startswith(prefix) for line in normalized.split(b"\n")[:-1])


def _logical_lines_in_order(payload: bytes, *markers: bytes) -> bool:
    """Require exact complete console lines to occur in the given order."""

    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = normalized.split(b"\n")[:-1]
    try:
        positions = [lines.index(marker) for marker in markers]
    except ValueError:
        return False
    return all(left < right for left, right in zip(positions, positions[1:], strict=False))


def _validate_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ArtifactValidationError(f"{field} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class VerifiedHostFile:
    path: Path
    payload: bytes
    digest: str
    size_bytes: int
    mode: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or not isinstance(self.payload, bytes)
            or self.size_bytes != len(self.payload)
            or self.digest != _digest(self.payload)
            or type(self.mode) is not int
            or not stat.S_ISREG(self.mode)
        ):
            raise ArtifactValidationError("verified host file is invalid")


def _read_pinned_regular_file(path: Path, *, maximum: int, label: str) -> VerifiedHostFile:
    if not isinstance(path, Path) or not path.is_absolute() or "\0" in os.fspath(path):
        raise ArtifactValidationError(f"{label} path is invalid")
    try:
        visible_before = path.lstat()
    except OSError:
        raise ArtifactValidationError(f"{label} cannot be opened safely") from None
    if not stat.S_ISREG(visible_before.st_mode):
        raise ArtifactValidationError(f"{label} cannot be opened safely")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ArtifactValidationError(f"{label} cannot be opened safely") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (visible_before.st_dev, visible_before.st_ino) != (before.st_dev, before.st_ino)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.getuid()}
            or before.st_mode & 0o022
            or not 1 <= before.st_size <= maximum
        ):
            raise ArtifactValidationError(f"{label} metadata is invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ArtifactValidationError(f"{label} is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArtifactValidationError(f"{label} grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ArtifactValidationError(f"{label} changed while reading")
        visible_after = path.lstat()
        if (
            visible_before.st_dev,
            visible_before.st_ino,
            visible_before.st_mode,
            visible_before.st_uid,
            visible_before.st_nlink,
            visible_before.st_size,
            visible_before.st_mtime_ns,
        ) != (
            visible_after.st_dev,
            visible_after.st_ino,
            visible_after.st_mode,
            visible_after.st_uid,
            visible_after.st_nlink,
            visible_after.st_size,
            visible_after.st_mtime_ns,
        ):
            raise ArtifactValidationError(f"{label} changed while reading")
        payload = b"".join(chunks)
        return VerifiedHostFile(path, payload, _digest(payload), len(payload), before.st_mode)
    except OSError:
        raise ArtifactValidationError(f"{label} cannot be read safely") from None
    finally:
        os.close(descriptor)


def verify_linux_bzimage(path: Path) -> VerifiedHostFile:
    """Pin a bounded, owner-controlled bzImage and require ``HdrS``."""

    verified = _read_pinned_regular_file(path, maximum=MAX_KERNEL_BYTES, label="KVM proof kernel")
    if verified.size_bytes < 0x206 or verified.payload[0x202:0x206] != b"HdrS":
        raise ArtifactValidationError("KVM proof kernel is not a Linux bzImage")
    return verified


def _candidate_paths(env_name: str, fallbacks: tuple[Path, ...]) -> tuple[Path, ...]:
    configured = os.environ.get(env_name)
    if configured is not None:
        if not configured or len(configured) > 4096 or "\0" in configured:
            raise ArtifactValidationError(f"{env_name} is invalid")
        return (Path(configured),)
    return fallbacks


def verify_kernel_configuration_selection() -> None:
    """Require an explicit kernel/config pair or the same host-release fallback."""

    kernel_configured = KERNEL_ENV in os.environ
    config_configured = KERNEL_CONFIG_ENV in os.environ
    if kernel_configured != config_configured:
        raise KVMProofUnavailable(f"{KERNEL_ENV} and {KERNEL_CONFIG_ENV} must be configured together")


def select_linux_bzimage() -> VerifiedHostFile:
    release = os.uname().release
    candidates = _candidate_paths(
        KERNEL_ENV,
        (
            Path(f"/boot/vmlinuz-{release}"),
            Path(f"/boot/bzImage-{release}"),
            Path(f"/usr/lib/modules/{release}/vmlinuz"),
        ),
    )
    errors: list[str] = []
    for candidate in candidates:
        try:
            return verify_linux_bzimage(candidate)
        except ArtifactValidationError as exc:
            errors.append(str(exc))
    raise KVMProofUnavailable("no qualified Linux bzImage: " + "; ".join(errors))


def verify_kernel_config(path: Path) -> VerifiedHostFile:
    verified = _read_pinned_regular_file(path, maximum=MAX_KERNEL_CONFIG_BYTES, label="KVM proof kernel config")
    try:
        text = verified.payload.decode("ascii")
    except UnicodeDecodeError:
        raise ArtifactValidationError("KVM proof kernel config is not ASCII") from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            if key in values:
                raise ArtifactValidationError("KVM proof kernel config has duplicate keys")
            values[key] = value
    missing = [key for key in _REQUIRED_KERNEL_CONFIG if values.get(key) != "y"]
    if missing:
        raise ArtifactValidationError("KVM proof kernel config is missing built-ins: " + ",".join(missing))
    return verified


def select_kernel_config() -> VerifiedHostFile:
    release = os.uname().release
    candidates = _candidate_paths(
        KERNEL_CONFIG_ENV,
        (Path(f"/boot/config-{release}"), Path(f"/usr/lib/modules/{release}/config")),
    )
    errors: list[str] = []
    for candidate in candidates:
        try:
            return verify_kernel_config(candidate)
        except ArtifactValidationError as exc:
            errors.append(str(exc))
    raise KVMProofUnavailable("no qualified Linux kernel config: " + "; ".join(errors))


def verify_kvm_api() -> int:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise KVMProofUnavailable("qualified proof requires Linux x86_64")
    try:
        descriptor = os.open("/dev/kvm", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise KVMProofUnavailable("/dev/kvm is not available read-write") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISCHR(opened.st_mode):
            raise KVMProofUnavailable("/dev/kvm is not a character device")
        try:
            version = fcntl.ioctl(descriptor, KVM_GET_API_VERSION, 0)
        except OSError:
            raise KVMProofUnavailable("KVM_GET_API_VERSION failed") from None
    finally:
        os.close(descriptor)
    if version != REQUIRED_KVM_API_VERSION:
        raise KVMProofUnavailable(f"KVM API {version} is not qualified")
    return version


def select_qemu() -> VerifiedHostFile:
    configured = os.environ.get(QEMU_ENV)
    selected = configured if configured is not None else shutil.which("qemu-system-x86_64")
    if not selected:
        raise KVMProofUnavailable("qemu-system-x86_64 is unavailable")
    try:
        path = Path(selected)
        verified = _read_pinned_regular_file(path, maximum=MAX_KERNEL_BYTES, label="KVM proof QEMU")
    except ArtifactValidationError as exc:
        raise KVMProofUnavailable(str(exc)) from None
    if not verified.payload.startswith(b"\x7fELF") or not verified.path.stat().st_mode & 0o111:
        raise KVMProofUnavailable("KVM proof QEMU is not an executable ELF")
    return verified


def qemu_version(qemu: VerifiedHostFile) -> bytes:
    with _temp_root() as temporary_name:
        copied_qemu = _secure_write(Path(temporary_name), "qemu-system-x86_64", qemu.payload, mode=0o500)
        try:
            completed = subprocess.run(
                [os.fspath(copied_qemu), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise KVMProofUnavailable("QEMU version probe failed") from None
        _verify_file_digest(copied_qemu, qemu.digest, 0o500)
    payload = completed.stdout + completed.stderr
    if completed.returncode != 0 or not 1 <= len(payload) <= MAX_QEMU_VERSION_BYTES or b"\0" in payload:
        raise KVMProofUnavailable("QEMU version output is invalid")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        raise KVMProofUnavailable("QEMU version output is not UTF-8") from None
    return payload


def build_proof_plan() -> OCIStage1Plan:
    """Return the deterministic plan used solely by the direct-boot proof."""

    filesystems = load_proof_filesystems()
    return OCIStage1Plan(
        run_id="f6f546e2-e734-4920-9eff-1762b348a249",
        run_name="kvm-stage1-proof",
        boot_plan_digest="sha256:" + "a" * 64,
        domain_core_digest="sha256:" + "b" * 64,
        root={
            "filesystem": "ext4",
            "filesystem_uuid": filesystems.filesystem_uuid,
            "generation": 1,
            "mount_options": ["rw", "nodev", "nosuid"],
            "serial": oci_stage1_device_serial("root", "1fd7a60e-fdb2-4877-91d3-148bbca3884f"),
            "size_bytes": 16 * 1024 * 1024,
            "volume_id": "1fd7a60e-fdb2-4877-91d3-148bbca3884f",
        },
        layers=(
            {
                "filesystem": "squashfs",
                "image_digest": filesystems.lower_digests[0],
                "mount_options": ["ro", "nodev", "nosuid"],
                "occurrence_digest": "sha256:" + "3" * 64,
                "ordinal": 0,
                "serial": oci_stage1_device_serial("lower", "sha256:" + "3" * 64),
                "size_bytes": len(filesystems.lowers[0]),
            },
            {
                "filesystem": "squashfs",
                "image_digest": filesystems.lower_digests[1],
                "mount_options": ["ro", "nodev", "nosuid"],
                "occurrence_digest": "sha256:" + "5" * 64,
                "ordinal": 1,
                "serial": oci_stage1_device_serial("lower", "sha256:" + "5" * 64),
                "size_bytes": len(filesystems.lowers[1]),
            },
        ),
        process=OCIProcessSpec(
            (
                "/.__palimpsest_workload_proof_v1",
                "palimpsest-argv-one",
                "",
                "line\nbreak",
            ),
            (
                ("PALIMPSEST_PROOF_ENV", "value with spaces"),
                ("PALIMPSEST_PROOF_EMPTY", ""),
            ),
            "/proof/workdir",
            OCIUserSpec("65534", "65534"),
            15,
        ),
        assembly_probes=(dict(_PROOF_ASSEMBLY_PROBE),),
    )


def transport_serial(artifact_digest: str) -> str:
    _validate_digest(artifact_digest, "transport digest")
    identity = f"palimpsest-oci-root-stage1-transport-v1\0{artifact_digest}".encode()
    return hashlib.sha256(identity).hexdigest()[:20]


def build_kernel_cmdline(plan: OCIStage1Plan, transport: BuiltOCIStage1Transport) -> str:
    if not isinstance(plan, OCIStage1Plan) or not isinstance(transport, BuiltOCIStage1Transport):
        raise ArtifactValidationError("KVM proof cmdline input is invalid")
    serial = transport_serial(transport.receipt.artifact_digest)
    lowers = ",".join(f"virtio-{layer['serial']}" for layer in plan.layers)
    cmdline = (
        "console=ttyS0,115200n8 rdinit=/init panic=-1 "
        f"palimpsest.resource={plan.boot_plan_digest} "
        f"palimpsest.core={plan.domain_core_digest} "
        f"palimpsest.stage1={transport.receipt.artifact_digest} "
        f"palimpsest.stage1dev=virtio-{serial} "
        f"palimpsest.root=virtio-{plan.root['serial']} "
        f"palimpsest.lowers={lowers}"
    )
    encoded = cmdline.encode("ascii")
    if not 1 <= len(encoded) <= 4096 or "\n" in cmdline or "\0" in cmdline:
        raise ArtifactValidationError("KVM proof cmdline is invalid")
    return cmdline


def build_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    transport_path: Path,
    root_path: Path,
    lower_paths: tuple[Path, ...],
    plan: OCIStage1Plan,
    cmdline: str,
    serial: str,
    transport_readonly: bool = True,
    lifecycle_socket_path: Path | None = None,
) -> tuple[str, ...]:
    paths = (qemu_path, kernel_path, initramfs_path, transport_path, root_path, *lower_paths)
    if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
        raise ArtifactValidationError("KVM proof command path is invalid")
    if (
        not isinstance(cmdline, str)
        or not cmdline
        or not isinstance(plan, OCIStage1Plan)
        or len(lower_paths) != len(plan.layers)
        or not isinstance(serial, str)
        or _SERIAL_RE.fullmatch(serial) is None
    ):
        raise ArtifactValidationError("KVM proof command binding is invalid")
    readonly = "on" if transport_readonly else "off"
    command = (
        os.fspath(qemu_path),
        "-accel",
        "kvm",
        "-cpu",
        "host",
        "-machine",
        "q35",
        "-m",
        "256M",
        "-smp",
        "1",
        "-nodefaults",
        "-no-user-config",
        "-no-reboot",
        "-display",
        "none",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-serial",
        "stdio",
        "-kernel",
        os.fspath(kernel_path),
        "-initrd",
        os.fspath(initramfs_path),
        "-append",
        cmdline,
    )
    # Deliberately permute role order so the proof cannot accidentally rely on
    # vda/vdb positions instead of authenticated virtio serials.
    first_lower = plan.layers[0]
    command += (
        "-drive",
        f"if=none,file={lower_paths[0]},format=raw,readonly=on,id=lower0",
        "-device",
        f"virtio-blk-pci,drive=lower0,serial={first_lower['serial']}",
        "-drive",
        f"if=none,file={transport_path},format=raw,readonly={readonly},id=stage1",
        "-device",
        f"virtio-blk-pci,drive=stage1,serial={serial}",
        "-drive",
        f"if=none,file={root_path},format=raw,readonly=off,id=root",
        "-device",
        f"virtio-blk-pci,drive=root,serial={plan.root['serial']}",
    )
    for index, (path, layer) in enumerate(zip(lower_paths[1:], plan.layers[1:], strict=True), 1):
        command += (
            "-drive",
            f"if=none,file={path},format=raw,readonly=on,id=lower{index}",
            "-device",
            f"virtio-blk-pci,drive=lower{index},serial={layer['serial']}",
        )
    if lifecycle_socket_path is not None:
        command += _lifecycle_qemu_arguments(lifecycle_socket_path)
    return command


def _lifecycle_qemu_arguments(path: Path, *, channel_name: str = OCI_CONTROL_CHANNEL_NAME) -> tuple[str, ...]:
    if not isinstance(path, Path) or not path.is_absolute() or "\0" in os.fspath(path):
        raise ArtifactValidationError("KVM lifecycle socket path is invalid")
    if channel_name not in {OCI_CONTROL_CHANNEL_NAME, LIFECYCLE_WRONG_CHANNEL_NAME}:
        raise ArtifactValidationError("KVM lifecycle channel name is invalid")
    return (
        "-object",
        "rng-random,id=palimpsest-rng,filename=/dev/urandom",
        "-device",
        "virtio-rng-pci,rng=palimpsest-rng",
        "-chardev",
        f"socket,id=palimpsest-lifecycle,path={path},server=on,wait=off",
        "-device",
        "virtio-serial-pci,id=palimpsest-lifecycle-serial",
        "-device",
        f"virtserialport,bus=palimpsest-lifecycle-serial.0,nr=1,chardev=palimpsest-lifecycle,name={channel_name}",
    )


def _duplicate_lifecycle_qemu_arguments(first: Path, second: Path) -> tuple[str, ...]:
    if first == second:
        raise ArtifactValidationError("KVM duplicate lifecycle socket paths are invalid")
    primary = _lifecycle_qemu_arguments(first)
    return primary + (
        "-chardev",
        f"socket,id=palimpsest-lifecycle-duplicate,path={second},server=on,wait=off",
        "-device",
        "virtserialport,bus=palimpsest-lifecycle-serial.0,nr=2,"
        f"chardev=palimpsest-lifecycle-duplicate,name={OCI_CONTROL_CHANNEL_NAME}",
    )


def pre_mount_topology(plan: OCIStage1Plan) -> dict[str, Any]:
    if not isinstance(plan, OCIStage1Plan) or plan.to_dict() != build_proof_plan().to_dict():
        raise ArtifactValidationError("KVM proof topology plan is invalid")

    filesystems = load_proof_filesystems()
    devices = [
        {
            "artifact_digest": filesystems.root_digest,
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
            "size_bytes": plan.root["size_bytes"],
        }
    ]
    devices.extend(
        {
            "artifact_digest": filesystems.lower_digests[layer["ordinal"]],
            "image_digest": layer["image_digest"],
            "occurrence_digest": layer["occurrence_digest"],
            "ordinal": layer["ordinal"],
            "read_only": True,
            "role": "lower",
            "serial": layer["serial"],
            "size_bytes": layer["size_bytes"],
        }
        for layer in plan.layers
    )
    topology = {
        "devices": devices,
        "fixture_manifest_digest": filesystems.manifest_digest,
        "fixture_policy": "palimpsest.kvm-actual-filesystem-fixtures.v8",
        "policy": "virtio-blk-pre-mount-device-set.v1",
    }
    topology["digest"] = _digest(canonical_json_bytes(topology))
    return topology


def negative_control_contract(
    name: str,
    *,
    plan: OCIStage1Plan | None = None,
    transport: BuiltOCIStage1Transport | None = None,
) -> dict[str, Any]:
    """Return one exact path-free topology mutation executed under KVM."""

    selected_plan = build_proof_plan() if plan is None else plan
    selected_transport = build_stage1_transport(selected_plan) if transport is None else transport
    if (
        name not in NEGATIVE_CONTROL_NAMES
        or not isinstance(selected_plan, OCIStage1Plan)
        or selected_plan.to_dict() != build_proof_plan().to_dict()
        or not isinstance(selected_transport, BuiltOCIStage1Transport)
        or selected_transport.stage1_plan != selected_plan
    ):
        raise ArtifactValidationError("KVM negative control input is invalid")

    attachments: list[dict[str, Any]] = [
        {
            "backing": "lower0",
            "drive_id": "lower0",
            "ordinal": 0,
            "read_only": True,
            "role": "lower",
            "serial": selected_plan.layers[0]["serial"],
        },
        {
            "backing": "transport",
            "drive_id": "stage1",
            "ordinal": None,
            "read_only": True,
            "role": "transport",
            "serial": transport_serial(selected_transport.receipt.artifact_digest),
        },
        {
            "backing": "root",
            "drive_id": "root",
            "ordinal": None,
            "read_only": False,
            "role": "root",
            "serial": selected_plan.root["serial"],
        },
        {
            "backing": "lower1",
            "drive_id": "lower1",
            "ordinal": 1,
            "read_only": True,
            "role": "lower",
            "serial": selected_plan.layers[1]["serial"],
        },
    ]
    backing_specs: dict[str, dict[str, Any]] = {
        "lower0": {
            "artifact_digest": load_proof_filesystems().lower_digests[0],
            "mode": 0o400,
            "size_bytes": selected_plan.layers[0]["size_bytes"],
        },
        "lower1": {
            "artifact_digest": load_proof_filesystems().lower_digests[1],
            "mode": 0o400,
            "size_bytes": selected_plan.layers[1]["size_bytes"],
        },
        "root": {
            "artifact_digest": load_proof_filesystems().root_digest,
            "mode": 0o600,
            "size_bytes": selected_plan.root["size_bytes"],
        },
        "transport": {
            "artifact_digest": selected_transport.receipt.artifact_digest,
            "mode": 0o400,
            "size_bytes": selected_transport.receipt.artifact_size_bytes,
        },
    }

    by_role = {(item["role"], item["ordinal"]): item for item in attachments}
    if name == "writable_transport":
        by_role[("transport", None)]["read_only"] = False
        backing_specs["transport"]["mode"] = 0o600
    elif name == "missing_root":
        attachments.remove(by_role[("root", None)])
    elif name == "wrong_root_serial":
        by_role[("root", None)]["serial"] = "f" * 20
    elif name == "readonly_root":
        by_role[("root", None)]["read_only"] = True
    elif name in {"root_size_smaller", "root_size_larger"}:
        size = selected_plan.root["size_bytes"] + (-512 if name.endswith("smaller") else 512)
        payload = (
            load_proof_filesystems().root[:size]
            if size < selected_plan.root["size_bytes"]
            else load_proof_filesystems().root + b"\0" * (size - selected_plan.root["size_bytes"])
        )
        backing_specs["root"] = {"artifact_digest": _digest(payload), "mode": 0o600, "size_bytes": size}
    elif name == "missing_lower":
        attachments.remove(by_role[("lower", 0)])
    elif name == "wrong_lower_serial":
        by_role[("lower", 0)]["serial"] = "e" * 20
    elif name == "writable_lower":
        by_role[("lower", 0)]["read_only"] = False
        backing_specs["lower0"]["mode"] = 0o600
    elif name in {"lower_size_smaller", "lower_size_larger"}:
        size = selected_plan.layers[0]["size_bytes"] + (-512 if name.endswith("smaller") else 512)
        payload = (
            load_proof_filesystems().lowers[0][:size]
            if size < selected_plan.layers[0]["size_bytes"]
            else load_proof_filesystems().lowers[0] + b"\0" * (size - selected_plan.layers[0]["size_bytes"])
        )
        backing_specs["lower0"] = {"artifact_digest": _digest(payload), "mode": 0o400, "size_bytes": size}
    elif name == "duplicate_serial":
        by_role[("lower", 1)]["serial"] = selected_plan.layers[0]["serial"]
    elif name == "extra_disk":
        backing_specs["extra"] = {"artifact_digest": _zero_digest(4096), "mode": 0o400, "size_bytes": 4096}
        attachments.append(
            {
                "backing": "extra",
                "drive_id": "extra",
                "ordinal": None,
                "read_only": True,
                "role": "extra",
                "serial": "d" * 20,
            }
        )

    used = {item["backing"] for item in attachments}
    contract: dict[str, Any] = {
        "attachments": attachments,
        "backings": {key: backing_specs[key] for key in sorted(used)},
        "name": name,
        "policy": "palimpsest.stage1-kvm-negative-control.v1",
    }
    contract["digest"] = _digest(canonical_json_bytes(contract))
    return contract


def negative_control_contracts() -> dict[str, dict[str, Any]]:
    return {name: negative_control_contract(name) for name in NEGATIVE_CONTROL_NAMES}


def _mutated_filesystem_payload(name: str) -> tuple[str, bytes]:
    filesystems = load_proof_filesystems()
    if name.startswith("root_"):
        role = "root"
        payload = bytearray(filesystems.root)
        if name == "root_bad_magic":
            payload[EXT4_SUPERBLOCK_OFFSET + 56] ^= 0x01
        elif name == "root_wrong_label":
            payload[EXT4_SUPERBLOCK_OFFSET + 120] ^= 0x01
        elif name == "root_geometry":
            payload[EXT4_SUPERBLOCK_OFFSET + 4] ^= 0x01
        else:  # pragma: no cover - caller validates the finite name set
            raise ArtifactValidationError("KVM filesystem control name is invalid")
        superblock = payload[EXT4_SUPERBLOCK_OFFSET : EXT4_SUPERBLOCK_OFFSET + EXT4_SUPERBLOCK_BYTES]
        checksum = ext4_primary_superblock_checksum(bytes(superblock))
        payload[EXT4_SUPERBLOCK_OFFSET + 1020 : EXT4_SUPERBLOCK_OFFSET + 1024] = checksum.to_bytes(4, "little")
        return role, bytes(payload)
    role = "lower0"
    payload = bytearray(filesystems.lowers[0])
    if name == "lower_bad_magic":
        payload[0] ^= 0x01
    elif name == "lower_bad_structure":
        payload[20:22] = b"\0\0"  # compression id outside the v2 policy
    elif name == "lower_digest_mismatch":
        # Keep the superblock and zero padding intact while changing one byte
        # inside bytes_used.  Structural validation succeeds, then the exact
        # whole-device image digest rejects this control.
        bytes_used = int.from_bytes(payload[40:48], "little")
        offset = 128
        if bytes_used <= offset:
            raise ArtifactValidationError("KVM SquashFS fixture is too small")
        payload[offset] ^= 0x01
    else:  # pragma: no cover - caller validates the finite name set
        raise ArtifactValidationError("KVM filesystem control name is invalid")
    return role, bytes(payload)


def _filesystem_negative_context(name: str) -> tuple[OCIStage1Plan, BuiltOCIStage1Transport]:
    if name not in FILESYSTEM_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM filesystem control input is invalid")
    base = build_proof_plan()
    layers = [dict(layer) for layer in base.layers]
    if name in {"lower_bad_magic", "lower_bad_structure"}:
        _backing_name, payload = _mutated_filesystem_payload(name)
        layers[0]["image_digest"] = _digest(payload)
    plan = OCIStage1Plan(
        base.run_id,
        base.run_name,
        base.boot_plan_digest,
        base.domain_core_digest,
        dict(base.root),
        tuple(layers),
        base.process,
        base.assembly_probes,
    )
    return plan, build_stage1_transport(plan)


def filesystem_negative_control_contract(name: str) -> dict[str, Any]:
    """Return one exact same-topology filesystem-byte mutation."""

    if name not in FILESYSTEM_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM filesystem control input is invalid")
    plan, transport = _filesystem_negative_context(name)
    filesystems = load_proof_filesystems()
    transport_device_serial = transport_serial(transport.receipt.artifact_digest)
    attachments = [
        {
            "backing": "lower0",
            "drive_id": "lower0",
            "ordinal": 0,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[0]["serial"],
        },
        {
            "backing": "transport",
            "drive_id": "stage1",
            "ordinal": None,
            "read_only": True,
            "role": "transport",
            "serial": transport_device_serial,
        },
        {
            "backing": "root",
            "drive_id": "root",
            "ordinal": None,
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
        },
        {
            "backing": "lower1",
            "drive_id": "lower1",
            "ordinal": 1,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[1]["serial"],
        },
    ]
    backings = {
        "lower0": {
            "artifact_digest": filesystems.lower_digests[0],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[0]),
        },
        "lower1": {
            "artifact_digest": filesystems.lower_digests[1],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[1]),
        },
        "root": {
            "artifact_digest": filesystems.root_digest,
            "mode": 0o600,
            "size_bytes": len(filesystems.root),
        },
        "transport": {
            "artifact_digest": transport.receipt.artifact_digest,
            "mode": 0o400,
            "size_bytes": transport.receipt.artifact_size_bytes,
        },
    }
    backing_name, payload = _mutated_filesystem_payload(name)
    backings[backing_name] = {
        "artifact_digest": _digest(payload),
        "mode": backings[backing_name]["mode"],
        "size_bytes": len(payload),
    }
    contract: dict[str, Any] = {
        "attachments": attachments,
        "backings": backings,
        "cmdline": build_kernel_cmdline(plan, transport),
        "name": name,
        "policy": "palimpsest.stage1-kvm-filesystem-negative-control.v1",
        "stage1_plan": plan.to_dict(),
        "stage1_transport": {**transport.receipt.to_dict(), "serial": transport_device_serial},
    }
    contract["digest"] = _digest(canonical_json_bytes(contract))
    return contract


def filesystem_negative_control_contracts() -> dict[str, dict[str, Any]]:
    return {name: filesystem_negative_control_contract(name) for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES}


def _assembly_negative_context(name: str) -> tuple[OCIStage1Plan, BuiltOCIStage1Transport]:
    if name not in ASSEMBLY_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM assembly control input is invalid")
    base = build_proof_plan()
    probe = dict(base.assembly_probes[0])
    if name == "probe_missing":
        probe["path"] = "/.__palimpsest_missing_probe_v1"
    elif name == "probe_size_mismatch":
        probe["size_bytes"] -= 1
    else:
        probe["digest"] = "sha256:" + "e" * 64
    plan = OCIStage1Plan(
        base.run_id,
        base.run_name,
        base.boot_plan_digest,
        base.domain_core_digest,
        dict(base.root),
        tuple(dict(layer) for layer in base.layers),
        base.process,
        (probe,),
    )
    return plan, build_stage1_transport(plan)


def assembly_negative_control_contract(name: str) -> dict[str, Any]:
    """Bind one post-mount merged-tree probe rejection."""

    plan, transport = _assembly_negative_context(name)
    filesystems = load_proof_filesystems()
    serial = transport_serial(transport.receipt.artifact_digest)
    attachments = [
        {
            "backing": "lower0",
            "drive_id": "lower0",
            "ordinal": 0,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[0]["serial"],
        },
        {
            "backing": "transport",
            "drive_id": "stage1",
            "ordinal": None,
            "read_only": True,
            "role": "transport",
            "serial": serial,
        },
        {
            "backing": "root",
            "drive_id": "root",
            "ordinal": None,
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
        },
        {
            "backing": "lower1",
            "drive_id": "lower1",
            "ordinal": 1,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[1]["serial"],
        },
    ]
    backings = {
        "lower0": {
            "artifact_digest": filesystems.lower_digests[0],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[0]),
        },
        "lower1": {
            "artifact_digest": filesystems.lower_digests[1],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[1]),
        },
        "root": {"artifact_digest": filesystems.root_digest, "mode": 0o600, "size_bytes": len(filesystems.root)},
        "transport": {
            "artifact_digest": transport.receipt.artifact_digest,
            "mode": 0o400,
            "size_bytes": transport.receipt.artifact_size_bytes,
        },
    }
    contract: dict[str, Any] = {
        "attachments": attachments,
        "backings": backings,
        "cmdline": build_kernel_cmdline(plan, transport),
        "name": name,
        "policy": "palimpsest.stage1-kvm-assembly-negative-control.v1",
        "rejection_marker": ASSEMBLY_REJECTION_MARKER.decode("ascii"),
        "stage": "post-overlay-probe",
        "stage1_plan": plan.to_dict(),
        "stage1_transport": {**transport.receipt.to_dict(), "serial": serial},
    }
    contract["digest"] = _digest(canonical_json_bytes(contract))
    return contract


def assembly_negative_control_contracts() -> dict[str, dict[str, Any]]:
    return {name: assembly_negative_control_contract(name) for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES}


def verify_assembly_negative_control_contract(name: str, value: Any) -> dict[str, Any]:
    expected = assembly_negative_control_contract(name)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ArtifactValidationError("KVM assembly negative control contract is invalid")
    return expected


def _root_transition_negative_context(name: str) -> tuple[OCIStage1Plan, BuiltOCIStage1Transport]:
    if name not in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM root-transition control input is invalid")
    role = name.removeprefix("transition_").removesuffix("_not_directory")
    base = build_proof_plan()
    layers = [dict(layer) for layer in base.layers]
    layers[1]["image_digest"] = load_proof_filesystems().transition_lower_digests[role]
    plan = OCIStage1Plan(
        base.run_id,
        base.run_name,
        base.boot_plan_digest,
        base.domain_core_digest,
        dict(base.root),
        tuple(layers),
        base.process,
        tuple(dict(probe) for probe in base.assembly_probes),
    )
    return plan, build_stage1_transport(plan)


def root_transition_negative_control_contract(name: str) -> dict[str, Any]:
    """Bind one valid assembly whose named pseudo-filesystem target is not a directory."""

    plan, transport = _root_transition_negative_context(name)
    role = name.removeprefix("transition_").removesuffix("_not_directory")
    filesystems = load_proof_filesystems()
    serial = transport_serial(transport.receipt.artifact_digest)
    attachments = [
        {
            "backing": "lower0",
            "drive_id": "lower0",
            "ordinal": 0,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[0]["serial"],
        },
        {
            "backing": "transport",
            "drive_id": "stage1",
            "ordinal": None,
            "read_only": True,
            "role": "transport",
            "serial": serial,
        },
        {
            "backing": "root",
            "drive_id": "root",
            "ordinal": None,
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
        },
        {
            "backing": "lower1",
            "drive_id": "lower1",
            "ordinal": 1,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[1]["serial"],
        },
    ]
    backings = {
        "lower0": {
            "artifact_digest": filesystems.lower_digests[0],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[0]),
        },
        "lower1": {
            "artifact_digest": filesystems.transition_lower_digests[role],
            "mode": 0o400,
            "size_bytes": len(filesystems.transition_lowers[role]),
        },
        "root": {"artifact_digest": filesystems.root_digest, "mode": 0o600, "size_bytes": len(filesystems.root)},
        "transport": {
            "artifact_digest": transport.receipt.artifact_digest,
            "mode": 0o400,
            "size_bytes": transport.receipt.artifact_size_bytes,
        },
    }
    contract: dict[str, Any] = {
        "attachments": attachments,
        "backings": backings,
        "cmdline": build_kernel_cmdline(plan, transport),
        "name": name,
        "policy": "palimpsest.stage1-kvm-root-transition-negative-control.v1",
        "rejection_marker": ROOT_TRANSITION_REJECTION_MARKER.decode("ascii"),
        "stage": "root-transition-target-preparation",
        "stage1_plan": plan.to_dict(),
        "stage1_transport": {**transport.receipt.to_dict(), "serial": serial},
        "target": role,
    }
    contract["digest"] = _digest(canonical_json_bytes(contract))
    return contract


def root_transition_negative_control_contracts() -> dict[str, dict[str, Any]]:
    return {name: root_transition_negative_control_contract(name) for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES}


def verify_root_transition_negative_control_contract(name: str, value: Any) -> dict[str, Any]:
    expected = root_transition_negative_control_contract(name)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ArtifactValidationError("KVM root-transition negative control contract is invalid")
    return expected


def _workload_negative_context(name: str) -> tuple[OCIStage1Plan, BuiltOCIStage1Transport]:
    if name not in WORKLOAD_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM workload control input is invalid")
    base = build_proof_plan()
    process = base.process
    if name == "workload_missing_executable":
        process = OCIProcessSpec(
            ("/.__palimpsest_missing_workload_v1", *process.argv[1:]),
            process.environment,
            process.cwd,
            process.user,
            process.stop_signal,
        )
    elif name == "workload_non_executable":
        process = OCIProcessSpec(
            ("/layer.txt", *process.argv[1:]),
            process.environment,
            process.cwd,
            process.user,
            process.stop_signal,
        )
    else:
        process = OCIProcessSpec(
            process.argv,
            process.environment,
            "/proof/missing",
            process.user,
            process.stop_signal,
        )
    plan = OCIStage1Plan(
        base.run_id,
        base.run_name,
        base.boot_plan_digest,
        base.domain_core_digest,
        dict(base.root),
        tuple(dict(layer) for layer in base.layers),
        process,
        tuple(dict(probe) for probe in base.assembly_probes),
    )
    return plan, build_stage1_transport(plan)


def workload_negative_control_contract(name: str) -> dict[str, Any]:
    """Bind one valid root transition followed by an exact workload launch rejection."""

    plan, transport = _workload_negative_context(name)
    filesystems = load_proof_filesystems()
    serial = transport_serial(transport.receipt.artifact_digest)
    attachments = [
        {
            "backing": "lower0",
            "drive_id": "lower0",
            "ordinal": 0,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[0]["serial"],
        },
        {
            "backing": "transport",
            "drive_id": "stage1",
            "ordinal": None,
            "read_only": True,
            "role": "transport",
            "serial": serial,
        },
        {
            "backing": "root",
            "drive_id": "root",
            "ordinal": None,
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
        },
        {
            "backing": "lower1",
            "drive_id": "lower1",
            "ordinal": 1,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[1]["serial"],
        },
    ]
    backings = {
        "lower0": {
            "artifact_digest": filesystems.lower_digests[0],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[0]),
        },
        "lower1": {
            "artifact_digest": filesystems.lower_digests[1],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[1]),
        },
        "root": {
            "artifact_digest": filesystems.root_digest,
            "mode": 0o600,
            "size_bytes": len(filesystems.root),
        },
        "transport": {
            "artifact_digest": transport.receipt.artifact_digest,
            "mode": 0o400,
            "size_bytes": transport.receipt.artifact_size_bytes,
        },
    }
    expected_stage = 6 if name == "workload_missing_cwd" else 7
    expected_errno = 13 if name == "workload_non_executable" else 2
    contract: dict[str, Any] = {
        "attachments": attachments,
        "backings": backings,
        "cmdline": build_kernel_cmdline(plan, transport),
        "expected_errno": expected_errno,
        "expected_stage": expected_stage,
        "name": name,
        "policy": "palimpsest.stage1-kvm-workload-negative-control.v1",
        "rejection_marker": WORKLOAD_NEGATIVE_REJECTION_MARKERS[name].decode("ascii"),
        "root_transition_marker": ROOT_TRANSITION_MARKER.decode("ascii"),
        "stage": "post-root-transition-workload-launch",
        "stage1_plan": plan.to_dict(),
        "stage1_transport": {**transport.receipt.to_dict(), "serial": serial},
    }
    contract["digest"] = _digest(canonical_json_bytes(contract))
    return contract


def workload_negative_control_contracts() -> dict[str, dict[str, Any]]:
    return {name: workload_negative_control_contract(name) for name in WORKLOAD_NEGATIVE_CONTROL_NAMES}


def verify_workload_negative_control_contract(name: str, value: Any) -> dict[str, Any]:
    expected = workload_negative_control_contract(name)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ArtifactValidationError("KVM workload negative control contract is invalid")
    return expected


def _base_lifecycle_control_artifacts() -> tuple[
    OCIStage1Plan, BuiltOCIStage1Transport, list[dict[str, Any]], dict[str, dict[str, Any]]
]:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    filesystems = load_proof_filesystems()
    serial = transport_serial(transport.receipt.artifact_digest)
    attachments = [
        {
            "backing": "lower0",
            "drive_id": "lower0",
            "ordinal": 0,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[0]["serial"],
        },
        {
            "backing": "transport",
            "drive_id": "stage1",
            "ordinal": None,
            "read_only": True,
            "role": "transport",
            "serial": serial,
        },
        {
            "backing": "root",
            "drive_id": "root",
            "ordinal": None,
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
        },
        {
            "backing": "lower1",
            "drive_id": "lower1",
            "ordinal": 1,
            "read_only": True,
            "role": "lower",
            "serial": plan.layers[1]["serial"],
        },
    ]
    backings = {
        "lower0": {
            "artifact_digest": filesystems.lower_digests[0],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[0]),
        },
        "lower1": {
            "artifact_digest": filesystems.lower_digests[1],
            "mode": 0o400,
            "size_bytes": len(filesystems.lowers[1]),
        },
        "root": {"artifact_digest": filesystems.root_digest, "mode": 0o600, "size_bytes": len(filesystems.root)},
        "transport": {
            "artifact_digest": transport.receipt.artifact_digest,
            "mode": 0o400,
            "size_bytes": transport.receipt.artifact_size_bytes,
        },
    }
    return plan, transport, attachments, backings


def _lifecycle_control_binding() -> OCIControlBinding:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    return OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)


def _lifecycle_hello_frame(*, request_id: int = 1, nonce: str = LIFECYCLE_NEGATIVE_NONCE) -> bytes:
    return encode_frame(OCIControlMessage("HELLO", _lifecycle_control_binding(), nonce, {}, request_id=request_id))


def lifecycle_negative_control_contract(name: str) -> dict[str, Any]:
    """Bind one native guest lifecycle discovery or canonical-wire rejection."""

    if name not in LIFECYCLE_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM lifecycle control input is invalid")
    plan, transport, attachments, backings = _base_lifecycle_control_artifacts()
    phase = (
        "pre-workload"
        if name
        in {
            "lifecycle_missing_port",
            "lifecycle_wrong_name_only",
            "hello_zero_length",
            "hello_oversized_length",
            "hello_duplicate_key_noncanonical",
            "hello_wrong_domain_core_binding",
        }
        else "post-workload"
    )
    hello = _lifecycle_hello_frame()
    if name == "lifecycle_missing_port":
        mutation: dict[str, Any] = {"channel_names": [], "operation": "omit-virtio-port"}
    elif name == "lifecycle_wrong_name_only":
        mutation = {"channel_names": [LIFECYCLE_WRONG_CHANNEL_NAME], "operation": "wrong-name-only"}
    elif name == "hello_zero_length":
        mutation = {"length_prefix_hex": "00000000", "operation": "raw-length-prefix"}
    elif name == "hello_oversized_length":
        mutation = {"length_prefix_hex": "0000fffd", "maximum_payload_bytes": 65532, "operation": "raw-length-prefix"}
    elif name == "hello_duplicate_key_noncanonical":
        mutation = {
            "base_frame_digest": _digest(hello),
            "duplicate_key": "domain_core_digest",
            "operation": "insert-duplicate-sorted-key",
        }
    elif name == "hello_wrong_domain_core_binding":
        mutation = {
            "base_frame_digest": _digest(hello),
            "field": "domain_core_digest",
            "operation": "replace",
            "value": "sha256:" + "f" * 64,
        }
    elif name == "hello_reused_nonce":
        mutation = {
            "base_frame_digest": _digest(hello),
            "first_request_id": 1,
            "operation": "reconnect-reuse-first-nonce",
            "second_request_id": 2,
        }
    elif name == "stop_stale_generation":
        mutation = {
            "base": "canonical-stop-from-observed-ready",
            "field": "boot_generation",
            "operation": "replace",
            "value": "00000000-0000-4000-8000-000000000000",
        }
    elif name == "stop_request_id_collides_with_hello":
        mutation = {
            "base": "canonical-stop-from-observed-ready",
            "hello_request_id": 1,
            "operation": "reuse-request-id",
            "stop_request_id": 1,
        }
    else:
        mutation = {
            "base": "canonical-stop-from-observed-ready",
            "first_request_id": 2,
            "operation": "append-second-distinct-stop",
            "second_request_id": 3,
        }
    contract: dict[str, Any] = {
        "attachments": attachments,
        "backings": backings,
        "cmdline": build_kernel_cmdline(plan, transport),
        "expected_errno": 5,
        "expected_stage": 20 if phase == "pre-workload" else 21,
        "immutable_backings": ["lower0", "lower1", "transport"],
        "mutation": mutation,
        "name": name,
        "phase": phase,
        "policy": "palimpsest.stage1-kvm-lifecycle-negative-control.v1",
        "rejection_marker": f"{LIFECYCLE_REJECTION_PREFIX.decode('ascii')}{20 if phase == 'pre-workload' else 21}; errno=5; terminal disabled; waiting fail-closed",
        "stage1_plan": plan.to_dict(),
        "stage1_transport": {
            **transport.receipt.to_dict(),
            "serial": transport_serial(transport.receipt.artifact_digest),
        },
    }
    contract["digest"] = _digest(canonical_json_bytes(contract))
    return contract


def lifecycle_negative_control_contracts() -> dict[str, dict[str, Any]]:
    return {name: lifecycle_negative_control_contract(name) for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES}


def verify_lifecycle_negative_control_contract(name: str, value: Any) -> dict[str, Any]:
    expected = lifecycle_negative_control_contract(name)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ArtifactValidationError("KVM lifecycle negative control contract is invalid")
    return expected


def verify_filesystem_negative_control_contract(name: str, value: Any) -> dict[str, Any]:
    expected = filesystem_negative_control_contract(name)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ArtifactValidationError("KVM filesystem negative control contract is invalid")
    return expected


def verify_negative_control_contract(name: str, value: Any) -> dict[str, Any]:
    expected = negative_control_contract(name)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise ArtifactValidationError("KVM negative control contract is invalid")
    return expected


def build_negative_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    control: Mapping[str, Any],
) -> tuple[str, ...]:
    name = control.get("name") if isinstance(control, Mapping) else None
    contract = verify_negative_control_contract(name, control)
    return _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
    )


def build_filesystem_negative_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    control: Mapping[str, Any],
) -> tuple[str, ...]:
    name = control.get("name") if isinstance(control, Mapping) else None
    contract = verify_filesystem_negative_control_contract(name, control)
    if cmdline != contract["cmdline"]:
        raise ArtifactValidationError("KVM filesystem control cmdline is invalid")
    return _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
    )


def build_assembly_negative_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    control: Mapping[str, Any],
) -> tuple[str, ...]:
    name = control.get("name") if isinstance(control, Mapping) else None
    contract = verify_assembly_negative_control_contract(name, control)
    if cmdline != contract["cmdline"]:
        raise ArtifactValidationError("KVM assembly control cmdline is invalid")
    return _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
    )


def build_root_transition_negative_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    control: Mapping[str, Any],
) -> tuple[str, ...]:
    name = control.get("name") if isinstance(control, Mapping) else None
    contract = verify_root_transition_negative_control_contract(name, control)
    if cmdline != contract["cmdline"]:
        raise ArtifactValidationError("KVM root-transition control cmdline is invalid")
    return _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
    )


def build_workload_negative_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    control: Mapping[str, Any],
    lifecycle_socket_path: Path | None = None,
) -> tuple[str, ...]:
    name = control.get("name") if isinstance(control, Mapping) else None
    contract = verify_workload_negative_control_contract(name, control)
    if cmdline != contract["cmdline"]:
        raise ArtifactValidationError("KVM workload control cmdline is invalid")
    return _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
        lifecycle_socket_path=lifecycle_socket_path,
    )


def build_lifecycle_negative_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    control: Mapping[str, Any],
    lifecycle_socket_path: Path | None,
) -> tuple[str, ...]:
    name = control.get("name") if isinstance(control, Mapping) else None
    contract = verify_lifecycle_negative_control_contract(name, control)
    if cmdline != contract["cmdline"]:
        raise ArtifactValidationError("KVM lifecycle control cmdline is invalid")
    if name == "lifecycle_missing_port":
        if lifecycle_socket_path is not None:
            raise ArtifactValidationError("KVM missing lifecycle port control has a socket")
        return _build_control_qemu_command(
            qemu_path=qemu_path,
            kernel_path=kernel_path,
            initramfs_path=initramfs_path,
            backing_paths=backing_paths,
            cmdline=cmdline,
            contract=contract,
        )
    if lifecycle_socket_path is None:
        raise ArtifactValidationError("KVM lifecycle control socket is missing")
    command = _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
    )
    return command + _lifecycle_qemu_arguments(
        lifecycle_socket_path,
        channel_name=LIFECYCLE_WRONG_CHANNEL_NAME if name == "lifecycle_wrong_name_only" else OCI_CONTROL_CHANNEL_NAME,
    )


def build_duplicate_lifecycle_name_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    first_socket_path: Path,
    second_socket_path: Path,
) -> tuple[str, ...]:
    contract = lifecycle_negative_control_contract("lifecycle_missing_port")
    command = _build_control_qemu_command(
        qemu_path=qemu_path,
        kernel_path=kernel_path,
        initramfs_path=initramfs_path,
        backing_paths=backing_paths,
        cmdline=cmdline,
        contract=contract,
    )
    return command + _duplicate_lifecycle_qemu_arguments(first_socket_path, second_socket_path)


def _build_control_qemu_command(
    *,
    qemu_path: Path,
    kernel_path: Path,
    initramfs_path: Path,
    backing_paths: Mapping[str, Path],
    cmdline: str,
    contract: Mapping[str, Any],
    lifecycle_socket_path: Path | None = None,
) -> tuple[str, ...]:
    if (
        not isinstance(backing_paths, Mapping)
        or set(backing_paths) != set(contract["backings"])
        or any(not isinstance(path, Path) or not path.is_absolute() for path in backing_paths.values())
    ):
        raise ArtifactValidationError("KVM negative control backing paths are invalid")
    command = (
        os.fspath(qemu_path),
        "-accel",
        "kvm",
        "-cpu",
        "host",
        "-machine",
        "q35",
        "-m",
        "256M",
        "-smp",
        "1",
        "-nodefaults",
        "-no-user-config",
        "-no-reboot",
        "-display",
        "none",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-serial",
        "stdio",
        "-kernel",
        os.fspath(kernel_path),
        "-initrd",
        os.fspath(initramfs_path),
        "-append",
        cmdline,
    )
    for attachment in contract["attachments"]:
        drive_id = attachment["drive_id"]
        path = backing_paths[attachment["backing"]]
        readonly = "on" if attachment["read_only"] else "off"
        command += (
            "-drive",
            f"if=none,file={path},format=raw,readonly={readonly},id={drive_id}",
            "-device",
            f"virtio-blk-pci,drive={drive_id},serial={attachment['serial']}",
        )
    if lifecycle_socket_path is not None:
        command += _lifecycle_qemu_arguments(lifecycle_socket_path)
    return command


def _valid_lifecycle_receipt(value: Any, plan: OCIStage1Plan, transport: BuiltOCIStage1Transport) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "binding",
        "boots",
        "broker_contract",
        "channel_discovery_negative_controls",
        "channel_name",
        "connection_limit",
        "natural_terminal_proven",
        "nonce_semantics",
        "negative_input_proven",
        "peer_identity",
        "protocol",
        "qemu_duplicate_name_rejected",
        "reconnect_proven",
        "session_profile",
        "single_connection_proven",
        "transport",
        "wire_negative_controls",
    }:
        return False
    if (
        value.get("binding")
        != {
            "domain_core_digest": plan.domain_core_digest,
            "run_id": plan.run_id,
            "stage1_artifact_digest": transport.receipt.artifact_digest,
        }
        or value.get("broker_contract") != OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT
    ):
        return False
    if (
        value.get("channel_name") != OCI_CONTROL_CHANNEL_NAME
        or value.get("protocol") != OCI_CONTROL_PROTOCOL
        or value.get("transport") != "qemu-private-unix-socket-to-virtio-serial"
        or value.get("nonce_semantics") != "correlation-and-replay-challenge-not-peer-authentication"
        or value.get("negative_input_proven") is not True
        or value.get("natural_terminal_proven") is not False
        or value.get("peer_identity") != "socket-dev-ino-uid-type-plus-linux-so-peercred-qemu-pid.v1"
        or value.get("single_connection_proven") is not True
        or value.get("reconnect_proven") is not True
        or value.get("session_profile") != "reconnect-snapshot-partial-retry-committed-same-id-dedupe.v1"
        or value.get("connection_limit") != 16
    ):
        return False
    boots = value.get("boots")
    discovery_controls = value.get("channel_discovery_negative_controls")
    wire_controls = value.get("wire_negative_controls")
    qemu_duplicate = value.get("qemu_duplicate_name_rejected")
    if (
        not isinstance(discovery_controls, Mapping)
        or set(discovery_controls) != set(LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES)
        or not isinstance(wire_controls, Mapping)
        or set(wire_controls) != set(LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES)
    ):
        return False
    for controls, names, wire in (
        (discovery_controls, LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES, False),
        (wire_controls, LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES, True),
    ):
        for name in names:
            item = controls.get(name)
            expected_fields = {
                "console_digest",
                "console_size_bytes",
                "contract",
                "immutable_backings_verified",
                "lifecycle_rejection_marker_count",
                "pid1_alive_after_marker",
                "root_post_digest",
                "root_seed_digest",
                "success_marker_count",
                "terminal_marker_count",
                "wire_input",
            }
            if (
                not isinstance(item, Mapping)
                or set(item) != expected_fields
                or item.get("contract") != lifecycle_negative_control_contract(name)
                or item.get("immutable_backings_verified") is not True
                or item.get("lifecycle_rejection_marker_count") != 1
                or item.get("pid1_alive_after_marker") is not True
                or item.get("success_marker_count") != 0
                or item.get("terminal_marker_count") != 0
                or not isinstance(item.get("console_digest"), str)
                or _DIGEST_RE.fullmatch(item["console_digest"]) is None
                or type(item.get("console_size_bytes")) is not int
                or not 1 <= item["console_size_bytes"] <= MAX_CONSOLE_BYTES
                or not isinstance(item.get("root_seed_digest"), str)
                or _DIGEST_RE.fullmatch(item["root_seed_digest"]) is None
                or item["root_seed_digest"] != item["contract"]["backings"]["root"]["artifact_digest"]
                or not isinstance(item.get("root_post_digest"), str)
                or _DIGEST_RE.fullmatch(item["root_post_digest"]) is None
            ):
                return False
            wire_input = item.get("wire_input")
            if not wire and wire_input is not None:
                return False
            if wire and (
                not isinstance(wire_input, Mapping)
                or set(wire_input) != {"boot_generation", "bytes_written", "digest", "size_bytes"}
                or type(wire_input.get("bytes_written")) is not int
                or wire_input.get("bytes_written") != wire_input.get("size_bytes")
                or type(wire_input.get("size_bytes")) is not int
                or not 4 <= wire_input["size_bytes"] <= 2 * (65536 + 4)
                or not isinstance(wire_input.get("digest"), str)
                or _DIGEST_RE.fullmatch(wire_input["digest"]) is None
            ):
                return False
            if wire:
                generation = wire_input["boot_generation"]
                if name.startswith("stop_") or name == "second_distinct_stop":
                    try:
                        parsed = uuid.UUID(generation)
                    except (AttributeError, TypeError, ValueError):
                        return False
                    if str(parsed) != generation or parsed.version != 4:
                        return False
                elif generation is not None:
                    return False
                expected_input = _lifecycle_negative_wire_bytes(
                    name,
                    OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest),
                    boot_generation=generation,
                )
                if wire_input != {
                    "boot_generation": generation,
                    "bytes_written": len(expected_input),
                    "digest": _digest(expected_input),
                    "size_bytes": len(expected_input),
                }:
                    return False
    if (
        not isinstance(qemu_duplicate, Mapping)
        or set(qemu_duplicate)
        != {
            "exit_code",
            "guest_boot_started",
            "invocation_count",
            "nonzero_exit",
            "output_digest",
            "output_size_bytes",
            "rejection_marker",
            "rejection_marker_count",
            "stage1_marker_count",
        }
        or type(qemu_duplicate.get("exit_code")) is not int
        or qemu_duplicate["exit_code"] <= 0
        or qemu_duplicate.get("guest_boot_started") is not False
        or qemu_duplicate.get("invocation_count") != 1
        or qemu_duplicate.get("nonzero_exit") is not True
        or not isinstance(qemu_duplicate.get("output_digest"), str)
        or _DIGEST_RE.fullmatch(qemu_duplicate["output_digest"]) is None
        or type(qemu_duplicate.get("output_size_bytes")) is not int
        or not 1 <= qemu_duplicate["output_size_bytes"] <= MAX_QEMU_VERSION_BYTES
        or qemu_duplicate.get("rejection_marker") != QEMU_DUPLICATE_NAME_REJECTION_MARKER.decode("ascii")
        or qemu_duplicate.get("rejection_marker_count") != 1
        or qemu_duplicate.get("stage1_marker_count") != 0
    ):
        return False
    normal_expected = (
        ("host-to-guest", "HELLO"),
        ("guest-to-host", "READY"),
        ("host-to-guest", "STOP"),
        ("guest-to-host", "TERMINAL"),
    )
    if not isinstance(boots, list) or len(boots) != 2:
        return False
    generations: list[str] = []
    nonces: list[str] = []
    for boot_index, boot in enumerate(boots):
        if not isinstance(boot, Mapping) or set(boot) != {
            "connection_count",
            "frames",
            "initial_ready_host_observed",
            "logical_attempts",
            "pid1_alive_after_terminal",
            "profile",
            "ready",
            "reopen_count",
            "stop_signal_dispatch_count",
            "terminal",
        }:
            return False
        if (
            boot.get("pid1_alive_after_terminal") is not True
            or boot.get("ready") is not True
            or boot.get("terminal") != {"exit_code": 42, "signal": None}
            or boot.get("reopen_count") != 0
            or boot.get("stop_signal_dispatch_count") != 1
        ):
            return False
        frames = boot.get("frames")
        if not isinstance(frames, list):
            return False
        if boot_index == 0:
            expected = tuple((1, direction, kind) for direction, kind in normal_expected)
            if (
                boot.get("profile") != "single-connection"
                or boot.get("connection_count") != 1
                or boot.get("initial_ready_host_observed") is not True
                or boot.get("logical_attempts") != []
            ):
                return False
        else:
            expected = (
                (1, "host-to-guest", "HELLO"),
                (2, "host-to-guest", "HELLO"),
                (2, "guest-to-host", "SNAPSHOT"),
                (3, "host-to-guest", "HELLO"),
                (3, "guest-to-host", "SNAPSHOT"),
                (4, "host-to-guest", "HELLO"),
                (4, "guest-to-host", "SNAPSHOT"),
                (4, "host-to-guest", "STOP"),
                (4, "host-to-guest", "STOP"),
                (5, "host-to-guest", "HELLO"),
                (5, "guest-to-host", "SNAPSHOT"),
                (5, "guest-to-host", "TERMINAL"),
                (6, "host-to-guest", "HELLO"),
                (6, "guest-to-host", "SNAPSHOT"),
            )
            if (
                boot.get("profile") != "six-connection-partial-retry-committed-dedupe-composite"
                or boot.get("connection_count") != 6
                or boot.get("initial_ready_host_observed") is not False
            ):
                return False
        if len(frames) != len(expected):
            return False
        for frame, (connection, direction, kind) in zip(frames, expected, strict=True):
            if (
                not isinstance(frame, Mapping)
                or set(frame)
                != {
                    "boot_generation",
                    "connection",
                    "digest",
                    "direction",
                    "host_nonce",
                    "kind",
                    "reply_to",
                    "request_id",
                    "sequence",
                    "size_bytes",
                }
                or frame.get("connection") != connection
                or frame.get("direction") != direction
                or frame.get("kind") != kind
                or not isinstance(frame.get("digest"), str)
                or _DIGEST_RE.fullmatch(frame["digest"]) is None
                or type(frame.get("size_bytes")) is not int
                or not 5 <= frame["size_bytes"] <= 64 * 1024
            ):
                return False
        hello = frames[0]
        nonce = hello["host_nonce"]
        generation = frames[1 if boot_index == 0 else 2]["boot_generation"]
        try:
            parsed_generation = uuid.UUID(generation)
        except (AttributeError, TypeError, ValueError):
            return False
        if (
            not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{64}", nonce) is None
            or str(parsed_generation) != generation
            or parsed_generation.version != 4
        ):
            return False
        by_connection: dict[int, str] = {}
        for frame in frames:
            connection = frame["connection"]
            frame_nonce = frame["host_nonce"]
            if not isinstance(frame_nonce, str) or re.fullmatch(r"[0-9a-f]{64}", frame_nonce) is None:
                return False
            if connection in by_connection and by_connection[connection] != frame_nonce:
                return False
            by_connection[connection] = frame_nonce
            if frame["kind"] != "HELLO" and frame["boot_generation"] != generation:
                return False
        if len(set(by_connection.values())) != boot["connection_count"]:
            return False
        try:
            binding = OCIControlBinding(
                run_id=plan.run_id,
                domain_core_digest=plan.domain_core_digest,
                stage1_artifact_digest=transport.receipt.artifact_digest,
            )
            expected_messages = []
            for index, frame in enumerate(frames):
                kind = frame["kind"]
                payload: dict[str, Any]
                if kind in {"HELLO", "READY"}:
                    payload = {}
                elif kind == "STOP":
                    payload = {"signal": 15}
                elif kind == "TERMINAL":
                    payload = {"terminal": {"exit_code": 42, "signal": None}}
                else:
                    state = "ready" if index in {2, 4, 6} else "stopping" if index == 10 else "terminal"
                    payload = {
                        "state": state,
                        "stop_request_id": 4 if state in {"stopping", "terminal"} else None,
                        "terminal": {"exit_code": 42, "signal": None} if state == "terminal" else None,
                    }
                expected_messages.append(
                    OCIControlMessage(
                        kind=kind,
                        binding=binding,
                        host_nonce=frame["host_nonce"],
                        payload=payload,
                        request_id=frame["request_id"],
                        sequence=frame["sequence"],
                        boot_generation=frame["boot_generation"],
                        reply_to=frame["reply_to"],
                    )
                )
            encoded_frames = tuple(encode_frame(message) for message in expected_messages)
        except OCIControlProtocolError:
            return False
        if any(
            frame["size_bytes"] != len(encoded) or frame["digest"] != f"sha256:{hashlib.sha256(encoded).hexdigest()}"
            for frame, encoded in zip(frames, encoded_frames, strict=True)
        ):
            return False
        generations.append(generation)
        nonces.extend(by_connection.values())
        if boot_index == 0:
            if (
                [frame["request_id"] for frame in frames] != [1, None, 2, None]
                or [frame["sequence"] for frame in frames] != [None, 1, None, 2]
                or [frame["reply_to"] for frame in frames] != [None, 1, None, 2]
            ):
                return False
        else:
            if [frame["request_id"] for frame in frames] != [
                1,
                2,
                None,
                3,
                None,
                5,
                None,
                4,
                4,
                6,
                None,
                None,
                7,
                None,
            ]:
                return False
            if [frame["sequence"] for frame in frames] != [
                None,
                None,
                2,
                None,
                3,
                None,
                4,
                None,
                None,
                None,
                5,
                6,
                None,
                7,
            ]:
                return False
            if [frame["reply_to"] for frame in frames] != [
                None,
                None,
                2,
                None,
                3,
                None,
                5,
                None,
                None,
                None,
                6,
                4,
                None,
                7,
            ]:
                return False
            attempts = boot["logical_attempts"]
            if not isinstance(attempts, list) or len(attempts) != 1:
                return False
            attempt = attempts[0]
            if not isinstance(attempt, Mapping) or set(attempt) != {
                "bytes_sent",
                "connection",
                "digest",
                "frame_size_bytes",
                "kind",
                "request_id",
            }:
                return False
            partial = OCIControlMessage(
                kind="STOP",
                binding=binding,
                host_nonce=by_connection[3],
                payload={"signal": 15},
                request_id=4,
                boot_generation=generation,
            )
            encoded = encode_frame(partial)
            if attempt != {
                "bytes_sent": len(encoded) - 1,
                "connection": 3,
                "digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                "frame_size_bytes": len(encoded),
                "kind": "STOP",
                "request_id": 4,
            }:
                return False
    return len(set(generations)) == 2 and len(set(nonces)) == 7


def _lifecycle_receipt(
    binding: OCIControlBinding,
    first: list[dict[str, Any]],
    retained: list[dict[str, Any]],
    retained_attempts: list[dict[str, Any]],
    negative_consoles: Mapping[str, bytes],
    negative_root_post_digests: Mapping[str, str],
    negative_inputs: Mapping[str, Mapping[str, Any]],
    duplicate_name_output: bytes,
    duplicate_name_exit_code: int,
    first_console: bytes,
    retained_console: bytes,
) -> dict[str, Any]:
    def negative_item(name: str) -> dict[str, Any]:
        contract = lifecycle_negative_control_contract(name)
        return {
            "console_digest": _digest(negative_consoles[name]),
            "console_size_bytes": len(negative_consoles[name]),
            "contract": contract,
            "immutable_backings_verified": True,
            "lifecycle_rejection_marker_count": 1,
            "pid1_alive_after_marker": True,
            "root_post_digest": negative_root_post_digests[name],
            "root_seed_digest": contract["backings"]["root"]["artifact_digest"],
            "success_marker_count": 0,
            "terminal_marker_count": 0,
            "wire_input": dict(negative_inputs[name]) if name in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES else None,
        }

    return {
        "binding": {
            "domain_core_digest": binding.domain_core_digest,
            "run_id": binding.run_id,
            "stage1_artifact_digest": binding.stage1_artifact_digest,
        },
        "boots": [
            {
                "connection_count": 1,
                "frames": first,
                "initial_ready_host_observed": True,
                "logical_attempts": [],
                "pid1_alive_after_terminal": True,
                "profile": "single-connection",
                "ready": True,
                "reopen_count": 0,
                "stop_signal_dispatch_count": _logical_line_count(first_console, LIFECYCLE_STOP_DISPATCHED_MARKER),
                "terminal": {"exit_code": 42, "signal": None},
            },
            {
                "connection_count": 6,
                "frames": retained,
                "initial_ready_host_observed": False,
                "logical_attempts": retained_attempts,
                "pid1_alive_after_terminal": True,
                "profile": "six-connection-partial-retry-committed-dedupe-composite",
                "ready": True,
                "reopen_count": 0,
                "stop_signal_dispatch_count": _logical_line_count(retained_console, LIFECYCLE_STOP_DISPATCHED_MARKER),
                "terminal": {"exit_code": 42, "signal": None},
            },
        ],
        "broker_contract": OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT,
        "channel_discovery_negative_controls": {
            name: negative_item(name) for name in LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES
        },
        "channel_name": OCI_CONTROL_CHANNEL_NAME,
        "nonce_semantics": "correlation-and-replay-challenge-not-peer-authentication",
        "connection_limit": 16,
        "natural_terminal_proven": False,
        "negative_input_proven": True,
        "peer_identity": "socket-dev-ino-uid-type-plus-linux-so-peercred-qemu-pid.v1",
        "protocol": OCI_CONTROL_PROTOCOL,
        "qemu_duplicate_name_rejected": {
            "exit_code": duplicate_name_exit_code,
            "guest_boot_started": False,
            "invocation_count": 1,
            "nonzero_exit": True,
            "output_digest": _digest(duplicate_name_output),
            "output_size_bytes": len(duplicate_name_output),
            "rejection_marker": QEMU_DUPLICATE_NAME_REJECTION_MARKER.decode("ascii"),
            "rejection_marker_count": duplicate_name_output.count(QEMU_DUPLICATE_NAME_REJECTION_MARKER),
            "stage1_marker_count": duplicate_name_output.count(b"palimpsest guest stage1:"),
        },
        "reconnect_proven": True,
        "session_profile": "reconnect-snapshot-partial-retry-committed-same-id-dedupe.v1",
        "single_connection_proven": True,
        "transport": "qemu-private-unix-socket-to-virtio-serial",
        "wire_negative_controls": {name: negative_item(name) for name in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES},
    }


@dataclass(frozen=True, slots=True)
class OCIStage1KVMProofReceipt:
    kernel_digest: str
    kernel_size_bytes: int
    kernel_config_digest: str
    initramfs_digest: str
    initramfs_size_bytes: int
    initramfs_manifest_digest: str
    stage1_elf_digest: str
    transport: Mapping[str, Any]
    transport_serial: str
    cmdline: str
    qemu_digest: str
    qemu_size_bytes: int
    qemu_version_bytes: bytes
    root_seed_digest: str
    root_post_run_digest: str
    root_second_boot_digest: str
    console: bytes
    retained_console: bytes
    negative_consoles: Mapping[str, bytes]
    filesystem_negative_consoles: Mapping[str, bytes]
    assembly_negative_consoles: Mapping[str, bytes]
    assembly_negative_root_post_digests: Mapping[str, str]
    root_transition_negative_consoles: Mapping[str, bytes]
    root_transition_negative_root_post_digests: Mapping[str, str]
    workload_negative_consoles: Mapping[str, bytes]
    workload_negative_root_post_digests: Mapping[str, str]
    lifecycle_negative_consoles: Mapping[str, bytes]
    lifecycle_negative_root_post_digests: Mapping[str, str]
    qemu_duplicate_name_output: bytes
    lifecycle: Mapping[str, Any]

    def __post_init__(self) -> None:
        for value, field in (
            (self.kernel_digest, "kernel digest"),
            (self.kernel_config_digest, "kernel config digest"),
            (self.initramfs_digest, "initramfs digest"),
            (self.initramfs_manifest_digest, "initramfs manifest digest"),
            (self.stage1_elf_digest, "stage-1 ELF digest"),
            (self.qemu_digest, "QEMU digest"),
            (self.root_seed_digest, "root seed digest"),
            (self.root_post_run_digest, "root post-run digest"),
            (self.root_second_boot_digest, "root second-boot digest"),
        ):
            _validate_digest(value, field)
        try:
            cmdline_bytes = self.cmdline.encode("ascii")
            transport_receipt = OCIStage1TransportReceipt.from_dict(dict(self.transport))
        except (ArtifactValidationError, UnicodeEncodeError):
            raise ArtifactValidationError("KVM proof receipt value is invalid") from None
        expected_initramfs = build_bootstrap_initramfs().manifest
        expected_plan = build_proof_plan()
        expected_transport = build_stage1_transport(expected_plan)
        if (
            type(self.kernel_size_bytes) is not int
            or not 1 <= self.kernel_size_bytes <= MAX_KERNEL_BYTES
            or type(self.initramfs_size_bytes) is not int
            or not 1 <= self.initramfs_size_bytes <= 64 * 1024 * 1024
            or not isinstance(self.transport, Mapping)
            or transport_receipt.to_dict() != dict(self.transport)
            or transport_receipt != expected_transport.receipt
            or not isinstance(self.transport_serial, str)
            or _SERIAL_RE.fullmatch(self.transport_serial) is None
            or self.transport_serial != transport_serial(transport_receipt.artifact_digest)
            or self.cmdline != build_kernel_cmdline(expected_plan, expected_transport)
            or self.initramfs_digest != expected_initramfs.artifact_digest
            or self.initramfs_size_bytes != expected_initramfs.artifact_size_bytes
            or self.initramfs_manifest_digest != expected_initramfs.digest
            or self.stage1_elf_digest != expected_initramfs.stage1_binary_digest
            or self.root_seed_digest != load_proof_filesystems().root_digest
            or not isinstance(self.cmdline, str)
            or not 1 <= len(cmdline_bytes) <= 4096
            or "\n" in self.cmdline
            or "\0" in self.cmdline
            or type(self.qemu_size_bytes) is not int
            or not 1 <= self.qemu_size_bytes <= MAX_KERNEL_BYTES
            or not isinstance(self.qemu_version_bytes, bytes)
            or not 1 <= len(self.qemu_version_bytes) <= MAX_QEMU_VERSION_BYTES
            or not isinstance(self.console, bytes)
            or not 1 <= len(self.console) <= MAX_CONSOLE_BYTES
            or _logical_line_count(self.console, SUCCESS_MARKER) != 1
            or _logical_prefix_count(self.console, WORKLOAD_TERMINAL_PREFIX) != 1
            or _logical_line_count(self.console, ROOT_TRANSITION_MARKER) != 1
            or _logical_line_count(self.console, WORKLOAD_STARTED_MARKER) != 1
            or _logical_line_count(self.console, WORKLOAD_SIGNAL_ARMED_MARKER) != 1
            or _logical_line_count(self.console, LIFECYCLE_STOP_DISPATCHED_MARKER) != 1
            or _logical_line_count(self.console, LIFECYCLE_STOP_DUPLICATE_MARKER) != 0
            or not _logical_lines_in_order(
                self.console,
                ROOT_TRANSITION_MARKER,
                WORKLOAD_STARTED_MARKER,
                WORKLOAD_SIGNAL_ARMED_MARKER,
                LIFECYCLE_STOP_DISPATCHED_MARKER,
                SUCCESS_MARKER,
            )
            or _logical_prefix_count(self.console, WORKLOAD_REJECTION_PREFIX) != 0
            or _logical_prefix_count(self.console, WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
            or _logical_prefix_count(self.console, LIFECYCLE_REJECTION_PREFIX) != 0
            or _logical_line_count(self.console, REJECTION_MARKER) != 0
            or _logical_line_count(self.console, FILESYSTEM_REJECTION_MARKER) != 0
            or _logical_line_count(self.console, ASSEMBLY_REJECTION_MARKER) != 0
            or _logical_line_count(self.console, ROOT_TRANSITION_REJECTION_MARKER) != 0
            or _logical_line_count(self.console, PREPARATION_FAILURE_MARKER) != 0
            or not isinstance(self.retained_console, bytes)
            or not 1 <= len(self.retained_console) <= MAX_CONSOLE_BYTES
            or _logical_line_count(self.retained_console, SUCCESS_MARKER) != 1
            or _logical_prefix_count(self.retained_console, WORKLOAD_TERMINAL_PREFIX) != 1
            or _logical_line_count(self.retained_console, ROOT_TRANSITION_MARKER) != 1
            or _logical_line_count(self.retained_console, WORKLOAD_STARTED_MARKER) != 1
            or _logical_line_count(self.retained_console, WORKLOAD_SIGNAL_ARMED_MARKER) != 1
            or _logical_line_count(self.retained_console, LIFECYCLE_STOP_DISPATCHED_MARKER) != 1
            or _logical_line_count(self.retained_console, LIFECYCLE_STOP_DUPLICATE_MARKER) != 1
            or not _logical_lines_in_order(
                self.retained_console,
                ROOT_TRANSITION_MARKER,
                WORKLOAD_STARTED_MARKER,
                WORKLOAD_SIGNAL_ARMED_MARKER,
                LIFECYCLE_STOP_DISPATCHED_MARKER,
                LIFECYCLE_STOP_DUPLICATE_MARKER,
                SUCCESS_MARKER,
            )
            or _logical_prefix_count(self.retained_console, WORKLOAD_REJECTION_PREFIX) != 0
            or _logical_prefix_count(self.retained_console, WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
            or _logical_prefix_count(self.retained_console, LIFECYCLE_REJECTION_PREFIX) != 0
            or any(
                _logical_line_count(self.retained_console, marker) != 0
                for marker in (
                    REJECTION_MARKER,
                    FILESYSTEM_REJECTION_MARKER,
                    ASSEMBLY_REJECTION_MARKER,
                    ROOT_TRANSITION_REJECTION_MARKER,
                    PREPARATION_FAILURE_MARKER,
                )
            )
            or not isinstance(self.negative_consoles, Mapping)
            or set(self.negative_consoles) != set(NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(control_console, bytes)
                or not 1 <= len(control_console) <= MAX_CONSOLE_BYTES
                or _logical_line_count(control_console, REJECTION_MARKER) != 1
                or _logical_line_count(control_console, SUCCESS_MARKER) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_MARKER) != 0
                or _logical_line_count(control_console, WORKLOAD_STARTED_MARKER) != 0
                or _logical_prefix_count(control_console, WORKLOAD_REJECTION_PREFIX) != 0
                or _logical_prefix_count(control_console, WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
                or _logical_line_count(control_console, ASSEMBLY_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, PREPARATION_FAILURE_MARKER) != 0
                for control_console in self.negative_consoles.values()
            )
            or not isinstance(self.filesystem_negative_consoles, Mapping)
            or set(self.filesystem_negative_consoles) != set(FILESYSTEM_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(control_console, bytes)
                or not 1 <= len(control_console) <= MAX_CONSOLE_BYTES
                or _logical_line_count(control_console, FILESYSTEM_REJECTION_MARKER) != 1
                or _logical_line_count(control_console, REJECTION_MARKER) != 0
                or _logical_line_count(control_console, SUCCESS_MARKER) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_MARKER) != 0
                or _logical_line_count(control_console, WORKLOAD_STARTED_MARKER) != 0
                or _logical_prefix_count(control_console, WORKLOAD_REJECTION_PREFIX) != 0
                or _logical_prefix_count(control_console, WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
                or _logical_line_count(control_console, ASSEMBLY_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, PREPARATION_FAILURE_MARKER) != 0
                for control_console in self.filesystem_negative_consoles.values()
            )
            or not isinstance(self.assembly_negative_consoles, Mapping)
            or set(self.assembly_negative_consoles) != set(ASSEMBLY_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(control_console, bytes)
                or not 1 <= len(control_console) <= MAX_CONSOLE_BYTES
                or _logical_line_count(control_console, ASSEMBLY_REJECTION_MARKER) != 1
                or _logical_line_count(control_console, REJECTION_MARKER) != 0
                or _logical_line_count(control_console, FILESYSTEM_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, SUCCESS_MARKER) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_MARKER) != 0
                or _logical_line_count(control_console, WORKLOAD_STARTED_MARKER) != 0
                or _logical_prefix_count(control_console, WORKLOAD_REJECTION_PREFIX) != 0
                or _logical_prefix_count(control_console, WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, PREPARATION_FAILURE_MARKER) != 0
                for control_console in self.assembly_negative_consoles.values()
            )
            or not isinstance(self.assembly_negative_root_post_digests, Mapping)
            or set(self.assembly_negative_root_post_digests) != set(ASSEMBLY_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
                for value in self.assembly_negative_root_post_digests.values()
            )
            or not isinstance(self.root_transition_negative_consoles, Mapping)
            or set(self.root_transition_negative_consoles) != set(ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(control_console, bytes)
                or not 1 <= len(control_console) <= MAX_CONSOLE_BYTES
                or _logical_line_count(control_console, ROOT_TRANSITION_REJECTION_MARKER) != 1
                or _logical_line_count(control_console, REJECTION_MARKER) != 0
                or _logical_line_count(control_console, FILESYSTEM_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, ASSEMBLY_REJECTION_MARKER) != 0
                or _logical_line_count(control_console, SUCCESS_MARKER) != 0
                or _logical_line_count(control_console, ROOT_TRANSITION_MARKER) != 0
                or _logical_line_count(control_console, WORKLOAD_STARTED_MARKER) != 0
                or _logical_prefix_count(control_console, WORKLOAD_REJECTION_PREFIX) != 0
                or _logical_prefix_count(control_console, WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
                or _logical_line_count(control_console, PREPARATION_FAILURE_MARKER) != 0
                for control_console in self.root_transition_negative_consoles.values()
            )
            or not isinstance(self.root_transition_negative_root_post_digests, Mapping)
            or set(self.root_transition_negative_root_post_digests) != set(ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
                for value in self.root_transition_negative_root_post_digests.values()
            )
            or not isinstance(self.workload_negative_consoles, Mapping)
            or set(self.workload_negative_consoles) != set(WORKLOAD_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(self.workload_negative_consoles[name], bytes)
                or not 1 <= len(self.workload_negative_consoles[name]) <= MAX_CONSOLE_BYTES
                or _logical_line_count(self.workload_negative_consoles[name], ROOT_TRANSITION_MARKER) != 1
                or _logical_line_count(self.workload_negative_consoles[name], WORKLOAD_NEGATIVE_REJECTION_MARKERS[name])
                != 1
                or not _logical_lines_in_order(
                    self.workload_negative_consoles[name],
                    ROOT_TRANSITION_MARKER,
                    WORKLOAD_NEGATIVE_REJECTION_MARKERS[name],
                )
                or _logical_prefix_count(self.workload_negative_consoles[name], WORKLOAD_REJECTION_PREFIX) != 1
                or _logical_prefix_count(self.workload_negative_consoles[name], WORKLOAD_CLEANUP_REJECTION_PREFIX) != 0
                or _logical_line_count(self.workload_negative_consoles[name], WORKLOAD_STARTED_MARKER) != 0
                or _logical_line_count(self.workload_negative_consoles[name], WORKLOAD_TERMINAL_MARKER) != 0
                or any(
                    _logical_line_count(self.workload_negative_consoles[name], marker) != 0
                    for marker in (
                        REJECTION_MARKER,
                        FILESYSTEM_REJECTION_MARKER,
                        ASSEMBLY_REJECTION_MARKER,
                        ROOT_TRANSITION_REJECTION_MARKER,
                        PREPARATION_FAILURE_MARKER,
                    )
                )
                for name in WORKLOAD_NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(self.workload_negative_root_post_digests, Mapping)
            or set(self.workload_negative_root_post_digests) != set(WORKLOAD_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
                for value in self.workload_negative_root_post_digests.values()
            )
            or not isinstance(self.lifecycle_negative_consoles, Mapping)
            or set(self.lifecycle_negative_consoles) != set(LIFECYCLE_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(self.lifecycle_negative_consoles[name], bytes)
                or not 1 <= len(self.lifecycle_negative_consoles[name]) <= MAX_CONSOLE_BYTES
                or _logical_line_count(self.lifecycle_negative_consoles[name], ROOT_TRANSITION_MARKER) != 1
                or _logical_line_count(
                    self.lifecycle_negative_consoles[name],
                    lifecycle_negative_control_contract(name)["rejection_marker"].encode("ascii"),
                )
                != 1
                or _logical_prefix_count(self.lifecycle_negative_consoles[name], LIFECYCLE_REJECTION_PREFIX) != 1
                or _logical_prefix_count(self.lifecycle_negative_consoles[name], WORKLOAD_TERMINAL_PREFIX) != 0
                or _logical_line_count(self.lifecycle_negative_consoles[name], SUCCESS_MARKER) != 0
                or _logical_line_count(self.lifecycle_negative_consoles[name], LIFECYCLE_STOP_DISPATCHED_MARKER)
                != (1 if name == "second_distinct_stop" else 0)
                or _logical_line_count(self.lifecycle_negative_consoles[name], LIFECYCLE_STOP_DUPLICATE_MARKER) != 0
                or _logical_line_count(self.lifecycle_negative_consoles[name], WORKLOAD_STARTED_MARKER)
                != (1 if lifecycle_negative_control_contract(name)["phase"] == "post-workload" else 0)
                or not _logical_lines_in_order(
                    self.lifecycle_negative_consoles[name],
                    ROOT_TRANSITION_MARKER,
                    *(
                        (WORKLOAD_STARTED_MARKER, LIFECYCLE_STOP_DISPATCHED_MARKER)
                        if name == "second_distinct_stop"
                        else (WORKLOAD_STARTED_MARKER,)
                        if lifecycle_negative_control_contract(name)["phase"] == "post-workload"
                        else ()
                    ),
                    lifecycle_negative_control_contract(name)["rejection_marker"].encode("ascii"),
                )
                for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(self.lifecycle_negative_root_post_digests, Mapping)
            or set(self.lifecycle_negative_root_post_digests) != set(LIFECYCLE_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None
                for value in self.lifecycle_negative_root_post_digests.values()
            )
            or not isinstance(self.qemu_duplicate_name_output, bytes)
            or not 1 <= len(self.qemu_duplicate_name_output) <= MAX_QEMU_VERSION_BYTES
            or self.qemu_duplicate_name_output.count(QEMU_DUPLICATE_NAME_REJECTION_MARKER) != 1
            or b"palimpsest guest stage1:" in self.qemu_duplicate_name_output
            or not _valid_lifecycle_receipt(self.lifecycle, expected_plan, expected_transport)
        ):
            raise ArtifactValidationError("KVM proof receipt value is invalid")
        lifecycle_controls = {
            **self.lifecycle["channel_discovery_negative_controls"],
            **self.lifecycle["wire_negative_controls"],
        }
        if any(
            lifecycle_controls[name]["console_digest"] != _digest(self.lifecycle_negative_consoles[name])
            or lifecycle_controls[name]["console_size_bytes"] != len(self.lifecycle_negative_consoles[name])
            or lifecycle_controls[name]["root_post_digest"] != self.lifecycle_negative_root_post_digests[name]
            for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES
        ):
            raise ArtifactValidationError("KVM proof receipt value is invalid")
        duplicate = self.lifecycle["qemu_duplicate_name_rejected"]
        if duplicate["output_digest"] != _digest(self.qemu_duplicate_name_output) or duplicate[
            "output_size_bytes"
        ] != len(self.qemu_duplicate_name_output):
            raise ArtifactValidationError("KVM proof receipt value is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmdline": {"digest": _digest(self.cmdline.encode("ascii")), "text": self.cmdline},
            "console": {
                "digest": _digest(self.console),
                "size_bytes": len(self.console),
                "success_marker": SUCCESS_MARKER.decode("ascii").rstrip("\n"),
            },
            "retained_console": {
                "digest": _digest(self.retained_console),
                "size_bytes": len(self.retained_console),
                "success_marker": SUCCESS_MARKER.decode("ascii"),
            },
            "initramfs": {
                "artifact_digest": self.initramfs_digest,
                "artifact_size_bytes": self.initramfs_size_bytes,
                "manifest_digest": self.initramfs_manifest_digest,
            },
            "kernel": {
                "artifact_digest": self.kernel_digest,
                "artifact_size_bytes": self.kernel_size_bytes,
                "config_digest": self.kernel_config_digest,
            },
            "lifecycle": dict(self.lifecycle),
            "negative_controls": {
                name: {
                    "contract": negative_control_contract(name),
                    "console_digest": _digest(self.negative_consoles[name]),
                    "console_size_bytes": len(self.negative_consoles[name]),
                    "pid1_alive_after_marker": True,
                    "preparation_failure_marker_count": 0,
                    "rejection_marker": REJECTION_MARKER.decode("ascii").rstrip("\n"),
                    "rejection_marker_count": 1,
                    "root_transition_rejection_marker_count": 0,
                    "success_marker_count": 0,
                }
                for name in NEGATIVE_CONTROL_NAMES
            },
            "filesystem_negative_controls": {
                name: {
                    "contract": filesystem_negative_control_contract(name),
                    "console_digest": _digest(self.filesystem_negative_consoles[name]),
                    "console_size_bytes": len(self.filesystem_negative_consoles[name]),
                    "pid1_alive_after_marker": True,
                    "preparation_failure_marker_count": 0,
                    "rejection_marker": FILESYSTEM_REJECTION_MARKER.decode("ascii"),
                    "rejection_marker_count": 1,
                    "root_transition_rejection_marker_count": 0,
                    "success_marker_count": 0,
                    "topology_rejection_marker_count": 0,
                }
                for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES
            },
            "executed_boots": KVM_PROOF_BOOT_COUNT,
            "qemu_invocations": KVM_PROOF_BOOT_COUNT + 1,
            "assembly_negative_controls": {
                name: {
                    "contract": assembly_negative_control_contract(name),
                    "console_digest": _digest(self.assembly_negative_consoles[name]),
                    "console_size_bytes": len(self.assembly_negative_consoles[name]),
                    "pid1_alive_after_marker": True,
                    "rejection_marker_count": 1,
                    "root_transition_rejection_marker_count": 0,
                    "root_post_digest": self.assembly_negative_root_post_digests[name],
                    "root_seed_digest": assembly_negative_control_contract(name)["backings"]["root"]["artifact_digest"],
                    "success_marker_count": 0,
                }
                for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES
            },
            "root_transition_negative_controls": {
                name: {
                    "assembly_rejection_marker_count": 0,
                    "contract": root_transition_negative_control_contract(name),
                    "console_digest": _digest(self.root_transition_negative_consoles[name]),
                    "console_size_bytes": len(self.root_transition_negative_consoles[name]),
                    "filesystem_rejection_marker_count": 0,
                    "pid1_alive_after_marker": True,
                    "pre_mount_rejection_marker_count": 0,
                    "preparation_failure_marker_count": 0,
                    "rejection_marker": ROOT_TRANSITION_REJECTION_MARKER.decode("ascii"),
                    "rejection_marker_count": 1,
                    "root_post_digest": self.root_transition_negative_root_post_digests[name],
                    "root_seed_digest": root_transition_negative_control_contract(name)["backings"]["root"][
                        "artifact_digest"
                    ],
                    "success_marker_count": 0,
                }
                for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES
            },
            "workload_negative_controls": {
                name: {
                    "contract": workload_negative_control_contract(name),
                    "console_digest": _digest(self.workload_negative_consoles[name]),
                    "console_size_bytes": len(self.workload_negative_consoles[name]),
                    "pid1_alive_after_marker": True,
                    "rejection_marker_count": 1,
                    "root_post_digest": self.workload_negative_root_post_digests[name],
                    "root_seed_digest": workload_negative_control_contract(name)["backings"]["root"]["artifact_digest"],
                    "root_transition_marker_count": 1,
                    "terminal_marker_count": 0,
                    "workload_started_marker_count": 0,
                }
                for name in WORKLOAD_NEGATIVE_CONTROL_NAMES
            },
            "pre_mount_devices": True,
            "filesystem_verified": True,
            "root_filesystem_verified": True,
            "root_content_verified": False,
            "lower_filesystem_verified": True,
            "lower_content_verified": True,
            "mount_attempted": True,
            "root_filesystem_mounted": True,
            "lower_filesystems_mounted": True,
            "overlay_assembled": True,
            "pivot_root": False,
            "qualification": {
                "accelerator": "kvm",
                "architecture": "x86_64",
                "cpu": "host",
                "kvm_api_version": REQUIRED_KVM_API_VERSION,
                "live_pid1": True,
            },
            "qemu": {
                "artifact_digest": self.qemu_digest,
                "artifact_size_bytes": self.qemu_size_bytes,
                "version_digest": _digest(self.qemu_version_bytes),
                "version_text": self.qemu_version_bytes.decode("utf-8"),
            },
            "root_assembly": True,
            "root_is_slash": True,
            "root_transition": {
                "contract": OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
                "method": "move-mount-chroot",
                "pid1_root_matches_slash": True,
                "pivot_root": False,
                "pseudo_filesystems": ["dev", "sys", "proc"],
                "root_filesystem": "overlay",
                "switch_root": True,
                "workload_started": False,
            },
            "root_volume": {
                "boot1_post_and_boot2_pre_digest": self.root_post_run_digest,
                "boot2_post_digest": self.root_second_boot_digest,
                "content_verified": False,
                "retained_same_backing": True,
                "seed_digest": self.root_seed_digest,
            },
            "schema": OCI_STAGE1_KVM_PROOF_SCHEMA,
            "stage1": {"contract": OCI_BOOTSTRAP_STAGE1_CONTRACT, "elf_digest": self.stage1_elf_digest},
            "supervisor": {
                "contract": OCI_STAGE1_SUPERVISOR_CONTRACT,
                "cgroup": "/palimpsest.workload",
                "cgroup_security": "containment-and-cleanup-not-hostile-root-sandbox",
                "cgroup_write_escape_denied": ["parent", "own"],
                "cleanup": "stop-signal-grace-cgroup.kill-wait4-echild-populated-zero-rmdir",
                "cooperative_status": 43,
                "credential_timing": "child-after-parent-cgroup-attach-release",
                "forced_status": 137,
                "forwarded_signal": 15,
                "lifecycle_broker": OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT,
                "lifecycle_stop": "host-issued-after-ready-and-proof-signal-sync",
                "main_status": 42,
                "pid1_credentials": {"gid": 0, "supplementary_groups": [], "uid": 0},
                "privileged_broker_after_fork": True,
                "process_group": True,
                "reaped_children": 3,
                "terminal_state": "parent-marker-then-fail-closed-wait",
                "terminal_wire_order": "cleanup-certainty-then-terminal-frame-then-console-marker",
                "workload_credentials": {"gid": 65534, "supplementary_groups": [], "uid": 65534},
            },
            "switch_root": True,
            "topology": pre_mount_topology(build_proof_plan()),
            "transport": {**dict(self.transport), "serial": self.transport_serial},
            "workload_started": True,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        console: bytes,
        negative_consoles: Mapping[str, bytes],
        filesystem_negative_consoles: Mapping[str, bytes],
        retained_console: bytes,
        assembly_negative_consoles: Mapping[str, bytes],
        assembly_negative_root_post_digests: Mapping[str, str],
        root_transition_negative_consoles: Mapping[str, bytes],
        root_transition_negative_root_post_digests: Mapping[str, str],
        workload_negative_consoles: Mapping[str, bytes],
        workload_negative_root_post_digests: Mapping[str, str],
        lifecycle_negative_consoles: Mapping[str, bytes],
        lifecycle_negative_root_post_digests: Mapping[str, str],
        qemu_duplicate_name_output: bytes,
    ) -> OCIStage1KVMProofReceipt:
        if not isinstance(value, Mapping) or set(value) != {
            "assembly_negative_controls",
            "cmdline",
            "console",
            "initramfs",
            "kernel",
            "lifecycle",
            "negative_controls",
            "filesystem_negative_controls",
            "executed_boots",
            "pre_mount_devices",
            "filesystem_verified",
            "root_filesystem_verified",
            "root_content_verified",
            "lower_filesystem_verified",
            "lower_content_verified",
            "mount_attempted",
            "root_filesystem_mounted",
            "lower_filesystems_mounted",
            "overlay_assembled",
            "pivot_root",
            "qualification",
            "qemu",
            "qemu_invocations",
            "retained_console",
            "root_assembly",
            "root_is_slash",
            "root_transition",
            "root_transition_negative_controls",
            "root_volume",
            "schema",
            "stage1",
            "supervisor",
            "switch_root",
            "topology",
            "transport",
            "workload_negative_controls",
            "workload_started",
        }:
            raise ArtifactValidationError("KVM proof receipt fields are invalid")
        cmdline = value.get("cmdline")
        console_value = value.get("console")
        retained_console_value = value.get("retained_console")
        initramfs = value.get("initramfs")
        kernel = value.get("kernel")
        qemu = value.get("qemu")
        stage1 = value.get("stage1")
        transport = value.get("transport")
        qualification = value.get("qualification")
        negative_controls = value.get("negative_controls")
        filesystem_negative_controls = value.get("filesystem_negative_controls")
        assembly_negative_controls = value.get("assembly_negative_controls")
        root_transition_negative_controls = value.get("root_transition_negative_controls")
        workload_negative_controls = value.get("workload_negative_controls")
        lifecycle = value.get("lifecycle")
        supervisor = value.get("supervisor")
        root_volume = value.get("root_volume")
        if (
            value.get("schema") != OCI_STAGE1_KVM_PROOF_SCHEMA
            or value.get("executed_boots") != KVM_PROOF_BOOT_COUNT
            or value.get("qemu_invocations") != KVM_PROOF_BOOT_COUNT + 1
            or value.get("root_assembly") is not True
            or value.get("root_is_slash") is not True
            or value.get("pivot_root") is not False
            or value.get("switch_root") is not True
            or value.get("workload_started") is not True
            or value.get("root_transition")
            != {
                "contract": OCI_STAGE1_ROOT_TRANSITION_CONTRACT,
                "method": "move-mount-chroot",
                "pid1_root_matches_slash": True,
                "pivot_root": False,
                "pseudo_filesystems": ["dev", "sys", "proc"],
                "root_filesystem": "overlay",
                "switch_root": True,
                "workload_started": False,
            }
            or supervisor
            != {
                "contract": OCI_STAGE1_SUPERVISOR_CONTRACT,
                "cgroup": "/palimpsest.workload",
                "cgroup_security": "containment-and-cleanup-not-hostile-root-sandbox",
                "cgroup_write_escape_denied": ["parent", "own"],
                "cleanup": "stop-signal-grace-cgroup.kill-wait4-echild-populated-zero-rmdir",
                "cooperative_status": 43,
                "credential_timing": "child-after-parent-cgroup-attach-release",
                "forced_status": 137,
                "forwarded_signal": 15,
                "lifecycle_broker": OCI_STAGE1_LIFECYCLE_BROKER_CONTRACT,
                "lifecycle_stop": "host-issued-after-ready-and-proof-signal-sync",
                "main_status": 42,
                "pid1_credentials": {"gid": 0, "supplementary_groups": [], "uid": 0},
                "privileged_broker_after_fork": True,
                "process_group": True,
                "reaped_children": 3,
                "terminal_state": "parent-marker-then-fail-closed-wait",
                "terminal_wire_order": "cleanup-certainty-then-terminal-frame-then-console-marker",
                "workload_credentials": {"gid": 65534, "supplementary_groups": [], "uid": 65534},
            }
            or value.get("pre_mount_devices") is not True
            or value.get("filesystem_verified") is not True
            or value.get("root_filesystem_verified") is not True
            or value.get("root_content_verified") is not False
            or value.get("lower_filesystem_verified") is not True
            or value.get("lower_content_verified") is not True
            or value.get("mount_attempted") is not True
            or value.get("root_filesystem_mounted") is not True
            or value.get("lower_filesystems_mounted") is not True
            or value.get("overlay_assembled") is not True
            or not isinstance(root_volume, Mapping)
            or set(root_volume)
            != {
                "boot1_post_and_boot2_pre_digest",
                "boot2_post_digest",
                "content_verified",
                "retained_same_backing",
                "seed_digest",
            }
            or root_volume.get("content_verified") is not False
            or root_volume.get("retained_same_backing") is not True
            or value.get("topology") != pre_mount_topology(build_proof_plan())
            or qualification
            != {
                "accelerator": "kvm",
                "architecture": "x86_64",
                "cpu": "host",
                "kvm_api_version": 12,
                "live_pid1": True,
            }
            or not isinstance(cmdline, Mapping)
            or set(cmdline) != {"digest", "text"}
            or not isinstance(console_value, Mapping)
            or set(console_value) != {"digest", "size_bytes", "success_marker"}
            or not isinstance(retained_console_value, Mapping)
            or set(retained_console_value) != {"digest", "size_bytes", "success_marker"}
            or not isinstance(initramfs, Mapping)
            or set(initramfs) != {"artifact_digest", "artifact_size_bytes", "manifest_digest"}
            or not isinstance(kernel, Mapping)
            or set(kernel) != {"artifact_digest", "artifact_size_bytes", "config_digest"}
            or not isinstance(negative_controls, Mapping)
            or set(negative_controls) != set(NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(negative_controls.get(name), Mapping)
                or set(negative_controls[name])
                != {
                    "console_digest",
                    "console_size_bytes",
                    "contract",
                    "pid1_alive_after_marker",
                    "preparation_failure_marker_count",
                    "rejection_marker",
                    "rejection_marker_count",
                    "root_transition_rejection_marker_count",
                    "success_marker_count",
                }
                or negative_controls[name].get("contract") != negative_control_contract(name)
                for name in NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(filesystem_negative_controls, Mapping)
            or set(filesystem_negative_controls) != set(FILESYSTEM_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(filesystem_negative_controls.get(name), Mapping)
                or set(filesystem_negative_controls[name])
                != {
                    "console_digest",
                    "console_size_bytes",
                    "contract",
                    "pid1_alive_after_marker",
                    "preparation_failure_marker_count",
                    "rejection_marker",
                    "rejection_marker_count",
                    "root_transition_rejection_marker_count",
                    "success_marker_count",
                    "topology_rejection_marker_count",
                }
                or filesystem_negative_controls[name].get("contract") != filesystem_negative_control_contract(name)
                for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(assembly_negative_controls, Mapping)
            or set(assembly_negative_controls) != set(ASSEMBLY_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(assembly_negative_controls.get(name), Mapping)
                or set(assembly_negative_controls[name])
                != {
                    "console_digest",
                    "console_size_bytes",
                    "contract",
                    "pid1_alive_after_marker",
                    "rejection_marker_count",
                    "root_transition_rejection_marker_count",
                    "root_post_digest",
                    "root_seed_digest",
                    "success_marker_count",
                }
                or assembly_negative_controls[name].get("contract") != assembly_negative_control_contract(name)
                for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(root_transition_negative_controls, Mapping)
            or set(root_transition_negative_controls) != set(ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(root_transition_negative_controls.get(name), Mapping)
                or set(root_transition_negative_controls[name])
                != {
                    "assembly_rejection_marker_count",
                    "console_digest",
                    "console_size_bytes",
                    "contract",
                    "filesystem_rejection_marker_count",
                    "pid1_alive_after_marker",
                    "pre_mount_rejection_marker_count",
                    "preparation_failure_marker_count",
                    "rejection_marker",
                    "rejection_marker_count",
                    "root_post_digest",
                    "root_seed_digest",
                    "success_marker_count",
                }
                or root_transition_negative_controls[name].get("contract")
                != root_transition_negative_control_contract(name)
                for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(workload_negative_controls, Mapping)
            or set(workload_negative_controls) != set(WORKLOAD_NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(workload_negative_controls.get(name), Mapping)
                or set(workload_negative_controls[name])
                != {
                    "console_digest",
                    "console_size_bytes",
                    "contract",
                    "pid1_alive_after_marker",
                    "rejection_marker_count",
                    "root_post_digest",
                    "root_seed_digest",
                    "root_transition_marker_count",
                    "terminal_marker_count",
                    "workload_started_marker_count",
                }
                or workload_negative_controls[name].get("contract") != workload_negative_control_contract(name)
                for name in WORKLOAD_NEGATIVE_CONTROL_NAMES
            )
            or not isinstance(qemu, Mapping)
            or set(qemu) != {"artifact_digest", "artifact_size_bytes", "version_digest", "version_text"}
            or not isinstance(stage1, Mapping)
            or set(stage1) != {"contract", "elf_digest"}
            or stage1.get("contract") != OCI_BOOTSTRAP_STAGE1_CONTRACT
            or not isinstance(transport, Mapping)
            or "serial" not in transport
        ):
            raise ArtifactValidationError("KVM proof receipt policy is invalid")
        serial = transport["serial"]
        receipt = cls(
            kernel["artifact_digest"],
            kernel["artifact_size_bytes"],
            kernel["config_digest"],
            initramfs["artifact_digest"],
            initramfs["artifact_size_bytes"],
            initramfs["manifest_digest"],
            stage1["elf_digest"],
            {key: item for key, item in transport.items() if key != "serial"},
            serial,
            cmdline["text"],
            qemu["artifact_digest"],
            qemu["artifact_size_bytes"],
            qemu["version_text"].encode("utf-8"),
            root_volume["seed_digest"],
            root_volume["boot1_post_and_boot2_pre_digest"],
            root_volume["boot2_post_digest"],
            console,
            retained_console,
            negative_consoles,
            filesystem_negative_consoles,
            assembly_negative_consoles,
            assembly_negative_root_post_digests,
            root_transition_negative_consoles,
            root_transition_negative_root_post_digests,
            workload_negative_consoles,
            workload_negative_root_post_digests,
            lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output,
            lifecycle,
        )
        if receipt.to_dict() != dict(value):
            raise ArtifactValidationError("KVM proof receipt is not canonical")
        return receipt


@dataclass(frozen=True, slots=True)
class OCIStage1KVMProofResult:
    receipt: OCIStage1KVMProofReceipt
    console: bytes
    retained_console: bytes
    negative_consoles: Mapping[str, bytes]
    filesystem_negative_consoles: Mapping[str, bytes]
    assembly_negative_consoles: Mapping[str, bytes]
    root_transition_negative_consoles: Mapping[str, bytes]
    workload_negative_consoles: Mapping[str, bytes]
    lifecycle_negative_consoles: Mapping[str, bytes]
    qemu_duplicate_name_output: bytes
    evidence_directory: Path | None


def _secure_write(directory: Path, name: str, payload: bytes, *, mode: int) -> Path:
    if "/" in name or name in {"", ".", ".."} or mode not in {0o400, 0o500, 0o600}:
        raise ArtifactValidationError("secure proof output request is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(name, flags, mode=0o600, dir_fd=directory_fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ArtifactValidationError("secure proof output write made no progress")
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    except OSError:
        raise ArtifactValidationError("secure proof output cannot be published") from None
    finally:
        os.close(directory_fd)
    return directory / name


def verify_evidence_directory(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or "\0" in os.fspath(path):
        raise ArtifactValidationError("KVM proof evidence directory is invalid")
    try:
        metadata = path.lstat()
    except OSError:
        raise ArtifactValidationError("KVM proof evidence directory is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ArtifactValidationError("KVM proof evidence directory metadata is invalid")
    for name in EVIDENCE_FILE_NAMES:
        if (path / name).exists() or (path / name).is_symlink():
            raise ArtifactValidationError("KVM proof evidence output already exists")
    return path


def _lifecycle_negative_wire_bytes(
    name: str,
    binding: OCIControlBinding,
    *,
    boot_generation: str | None = None,
) -> bytes:
    if name not in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM lifecycle wire mutation name is invalid")
    hello = encode_frame(OCIControlMessage("HELLO", binding, LIFECYCLE_NEGATIVE_NONCE, {}, request_id=1))
    if name == "hello_zero_length":
        return b"\0\0\0\0"
    if name == "hello_oversized_length":
        return struct.pack(">I", 65533)
    if name == "hello_duplicate_key_noncanonical":
        payload = hello[4:]
        domain = f'"domain_core_digest":"{binding.domain_core_digest}"'.encode("ascii")
        needle = domain + b',"host_nonce"'
        mutated = payload.replace(needle, domain + b"," + needle, 1)
        if mutated == payload:
            raise KVMProofFailure("KVM lifecycle duplicate-key mutation failed")
        return struct.pack(">I", len(mutated)) + mutated
    if name == "hello_wrong_domain_core_binding":
        wrong = OCIControlBinding(binding.run_id, "sha256:" + "f" * 64, binding.stage1_artifact_digest)
        return encode_frame(OCIControlMessage("HELLO", wrong, LIFECYCLE_NEGATIVE_NONCE, {}, request_id=1))
    if name == "hello_reused_nonce":
        return encode_frame(OCIControlMessage("HELLO", binding, LIFECYCLE_NEGATIVE_NONCE, {}, request_id=2))
    if boot_generation is None:
        raise ArtifactValidationError("KVM lifecycle STOP mutation has no boot generation")
    request_id = 1 if name == "stop_request_id_collides_with_hello" else 2
    first = encode_frame(
        OCIControlMessage(
            "STOP",
            binding,
            LIFECYCLE_NEGATIVE_NONCE,
            {"signal": 15},
            request_id=request_id,
            boot_generation=(
                "00000000-0000-4000-8000-000000000000" if name == "stop_stale_generation" else boot_generation
            ),
        )
    )
    if name != "second_distinct_stop":
        return first
    return encode_frame(
        OCIControlMessage(
            "STOP",
            binding,
            LIFECYCLE_NEGATIVE_NONCE,
            {"signal": 15},
            request_id=3,
            boot_generation=boot_generation,
        )
    )


def _read_console_until(
    command: tuple[str, ...],
    *,
    expected: bytes,
    forbidden: tuple[bytes, ...],
    timeout_seconds: float,
    require_alive_after_marker: bool,
    lifecycle_socket_path: Path | None = None,
    lifecycle_binding: OCIControlBinding | None = None,
    lifecycle_success: bool | None = None,
    lifecycle_transcript: list[dict[str, Any]] | None = None,
    lifecycle_scenario: str = "normal",
    lifecycle_attempts: list[dict[str, Any]] | None = None,
    lifecycle_negative_name: str | None = None,
    lifecycle_negative_input: dict[str, Any] | None = None,
) -> bytes:
    if (lifecycle_socket_path is None) != (lifecycle_binding is None) or (
        lifecycle_binding is None and lifecycle_success is not None
    ):
        raise ArtifactValidationError("KVM lifecycle driver configuration is invalid")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise KVMProofFailure("QEMU could not be started") from None
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ, "console")
    console = bytearray()
    deadline = time.monotonic() + timeout_seconds
    marker_seen_at: float | None = None
    channel: socket.socket | None = None
    if lifecycle_scenario not in {"normal", "composite", "negative"}:
        raise ArtifactValidationError("KVM lifecycle scenario is invalid")
    if (lifecycle_scenario == "negative") != (lifecycle_negative_name is not None):
        raise ArtifactValidationError("KVM lifecycle negative scenario is invalid")
    if lifecycle_negative_name is not None and lifecycle_negative_name not in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES:
        raise ArtifactValidationError("KVM lifecycle negative scenario name is invalid")
    channel_finished = False
    channel_connected = False
    channel_pending = bytearray()
    pending_frame: bytes | None = None
    pending_message: OCIControlMessage | None = None
    pending_full_frame = True
    pending_negative_input = False
    decoder = OCIControlFrameDecoder()
    session = HostOCIControlSession(lifecycle_binding) if lifecycle_binding is not None else None
    lifecycle_ready = False
    connection_ordinal = 0
    socket_identity: tuple[int, int, int, int] | None = None
    composite_stage = "connect-1" if lifecycle_scenario == "composite" else "normal"
    composite_done = False
    negative_input_written = False
    negative_reconnect_pending = False
    committed_stop: OCIControlMessage | None = None

    def record_frame(direction: str, frame: bytes, message: Any) -> None:
        if lifecycle_transcript is not None:
            lifecycle_transcript.append(
                {
                    "boot_generation": message.boot_generation,
                    "connection": connection_ordinal,
                    "digest": f"sha256:{hashlib.sha256(frame).hexdigest()}",
                    "direction": direction,
                    "host_nonce": message.host_nonce,
                    "kind": message.kind,
                    "reply_to": message.reply_to,
                    "request_id": message.request_id,
                    "sequence": message.sequence,
                    "size_bytes": len(frame),
                }
            )

    def queue_message(message: OCIControlMessage, *, complete: bool = True) -> None:
        nonlocal pending_frame, pending_message, pending_full_frame
        if channel_pending:
            raise KVMProofFailure("KVM lifecycle host write overlapped")
        frame = encode_frame(message)
        channel_pending.extend(frame if complete else frame[:-1])
        pending_frame = frame
        pending_message = message
        pending_full_frame = complete
        if channel is not None:
            selector.modify(channel, selectors.EVENT_READ | selectors.EVENT_WRITE, "channel")

    def queue_raw(frame: bytes, *, negative_input: bool = False) -> None:
        nonlocal pending_frame, pending_message, pending_full_frame, pending_negative_input
        if channel_pending or not isinstance(frame, bytes) or not frame:
            raise KVMProofFailure("KVM lifecycle raw write is invalid")
        channel_pending.extend(frame)
        pending_frame = frame
        pending_message = None
        pending_full_frame = True
        pending_negative_input = negative_input
        if channel is not None:
            selector.modify(channel, selectors.EVENT_READ | selectors.EVENT_WRITE, "channel")

    def close_channel() -> None:
        nonlocal channel, channel_connected, decoder
        if channel is None:
            return
        try:
            selector.unregister(channel)
        except (KeyError, OSError):
            pass
        try:
            channel.close()
        except OSError:
            pass
        channel = None
        channel_connected = False
        decoder = OCIControlFrameDecoder()

    def verify_connected_peer() -> None:
        if channel is None or lifecycle_socket_path is None or socket_identity is None:
            raise KVMProofFailure("KVM lifecycle connected peer state is invalid")
        try:
            metadata = lifecycle_socket_path.lstat()
        except OSError:
            raise KVMProofFailure("KVM lifecycle socket vanished after connect") from None
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid, stat.S_IFMT(metadata.st_mode))
        if identity != socket_identity:
            raise KVMProofFailure("KVM lifecycle socket identity changed during connect")
        if platform.system() == "Linux":
            peercred = getattr(socket, "SO_PEERCRED", None)
            if peercred is None:
                raise KVMProofFailure("KVM lifecycle SO_PEERCRED is unavailable")
            try:
                peer_pid, peer_uid, _peer_gid = struct.unpack(
                    "3i", channel.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
                )
            except (OSError, struct.error):
                raise KVMProofFailure("KVM lifecycle peer credentials are unavailable") from None
            if peer_pid != process.pid or peer_uid != os.getuid():
                raise KVMProofFailure("KVM lifecycle peer credentials do not identify QEMU")

    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise KVMProofFailure("QEMU console proof timed out")
            if (
                channel is None
                and not channel_finished
                and lifecycle_socket_path is not None
                and lifecycle_socket_path.exists()
            ):
                try:
                    metadata = lifecycle_socket_path.lstat()
                except OSError:
                    raise KVMProofFailure("KVM lifecycle socket metadata is unavailable") from None
                identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid, stat.S_IFMT(metadata.st_mode))
                if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                    raise KVMProofFailure("KVM lifecycle socket identity is invalid")
                if socket_identity is None:
                    socket_identity = identity
                elif identity != socket_identity:
                    raise KVMProofFailure("KVM lifecycle socket identity changed across reconnect")
                channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                channel.setblocking(False)
                result = channel.connect_ex(os.fspath(lifecycle_socket_path))
                if result not in {0, errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise KVMProofFailure("KVM lifecycle socket connection failed")
                channel_connected = result == 0
                connection_ordinal += 1
                selector.register(channel, selectors.EVENT_READ | selectors.EVENT_WRITE, "channel")
                if channel_connected:
                    verify_connected_peer()
                    assert session is not None
                    if lifecycle_scenario == "negative":
                        assert lifecycle_negative_name is not None
                        if connection_ordinal == 1:
                            if (
                                lifecycle_negative_name.startswith("hello_")
                                and lifecycle_negative_name != "hello_reused_nonce"
                            ):
                                if lifecycle_negative_input is not None:
                                    lifecycle_negative_input["boot_generation"] = None
                                queue_raw(
                                    _lifecycle_negative_wire_bytes(lifecycle_negative_name, lifecycle_binding),
                                    negative_input=True,
                                )
                            else:
                                queue_raw(
                                    _lifecycle_hello_frame(
                                        request_id=1
                                    )
                                )
                        else:
                            if lifecycle_negative_input is not None:
                                lifecycle_negative_input["boot_generation"] = None
                            queue_raw(
                                _lifecycle_negative_wire_bytes(lifecycle_negative_name, lifecycle_binding),
                                negative_input=True,
                            )
                    else:
                        queue_message(session.hello(reconnect=connection_ordinal > 1))
            events = selector.select(min(0.1, deadline - now))
            for key, mask in events:
                if key.data == "console":
                    try:
                        chunk = os.read(descriptor, 65536)
                    except BlockingIOError:
                        continue
                    if chunk:
                        console.extend(chunk)
                        if len(console) > MAX_CONSOLE_BYTES:
                            raise KVMProofFailure("QEMU console exceeds proof bound")
                    continue
                assert channel is not None and session is not None
                if mask & selectors.EVENT_WRITE:
                    if not channel_connected:
                        if channel.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0:
                            raise KVMProofFailure("KVM lifecycle socket connection failed")
                        channel_connected = True
                        verify_connected_peer()
                        if lifecycle_scenario == "negative":
                            assert lifecycle_negative_name is not None
                            if connection_ordinal == 1:
                                if (
                                    lifecycle_negative_name.startswith("hello_")
                                    and lifecycle_negative_name != "hello_reused_nonce"
                                ):
                                    if lifecycle_negative_input is not None:
                                        lifecycle_negative_input["boot_generation"] = None
                                    queue_raw(
                                        _lifecycle_negative_wire_bytes(lifecycle_negative_name, lifecycle_binding),
                                        negative_input=True,
                                    )
                                else:
                                    queue_raw(
                                        _lifecycle_hello_frame(
                                            request_id=1
                                        )
                                    )
                            else:
                                if lifecycle_negative_input is not None:
                                    lifecycle_negative_input["boot_generation"] = None
                                queue_raw(
                                    _lifecycle_negative_wire_bytes(lifecycle_negative_name, lifecycle_binding),
                                    negative_input=True,
                                )
                        else:
                            queue_message(session.hello(reconnect=connection_ordinal > 1))
                    if channel_pending:
                        try:
                            sent = channel.send(channel_pending)
                        except BlockingIOError:
                            sent = 0
                        if sent > 0:
                            del channel_pending[:sent]
                        if not channel_pending:
                            assert pending_frame is not None
                            if pending_full_frame:
                                if pending_message is not None:
                                    record_frame("host-to-guest", pending_frame, pending_message)
                                elif lifecycle_scenario == "negative" and pending_negative_input:
                                    negative_input_written = True
                                    if lifecycle_negative_input is not None:
                                        lifecycle_negative_input.update(
                                            {
                                                "bytes_written": len(pending_frame),
                                                "digest": _digest(pending_frame),
                                                "size_bytes": len(pending_frame),
                                            }
                                        )
                            else:
                                if lifecycle_attempts is not None:
                                    lifecycle_attempts.append(
                                        {
                                            "bytes_sent": len(pending_frame) - 1,
                                            "connection": connection_ordinal,
                                            "digest": f"sha256:{hashlib.sha256(pending_frame).hexdigest()}",
                                            "frame_size_bytes": len(pending_frame),
                                            "kind": pending_message.kind,
                                            "request_id": pending_message.request_id,
                                        }
                                    )
                                pending_frame = None
                                pending_message = None
                                close_channel()
                                composite_stage = "connect-4"
                                continue
                            pending_frame = None
                            pending_message = None
                            pending_negative_input = False
                            if lifecycle_scenario == "composite" and connection_ordinal == 1:
                                selector.unregister(channel)
                                # Do not consume a coalesced fast READY from this same
                                # readiness event; connection one deliberately loses it.
                                mask &= ~selectors.EVENT_READ
                            elif lifecycle_scenario == "composite" and composite_stage == "duplicate-stop-4":
                                selector.unregister(channel)
                                mask &= ~selectors.EVENT_READ
                            else:
                                selector.modify(channel, selectors.EVENT_READ, "channel")
                if mask & selectors.EVENT_READ:
                    try:
                        chunk = channel.recv(65536)
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        try:
                            decoder.finish()
                        except OCIControlProtocolError:
                            raise KVMProofFailure("KVM lifecycle protocol was rejected") from None
                        if lifecycle_scenario == "negative" and negative_input_written:
                            close_channel()
                            channel_finished = True
                            continue
                        if lifecycle_success is False and session.state == "hello-sent":
                            close_channel()
                            channel_finished = True
                            continue
                        raise KVMProofFailure("KVM lifecycle channel closed before proof completion")
                    if chunk:
                        try:
                            messages = decoder.feed(chunk)
                            for message in messages:
                                frame = encode_frame(message)
                                record_frame("guest-to-host", frame, message)
                                if lifecycle_scenario == "negative":
                                    if (
                                        message.kind != "READY"
                                        or message.binding != lifecycle_binding
                                        or message.host_nonce != LIFECYCLE_NEGATIVE_NONCE
                                        or message.reply_to != 1
                                        or message.sequence != 1
                                    ):
                                        raise KVMProofFailure("KVM lifecycle negative setup READY is invalid")
                                else:
                                    session.accept(message)
                                if message.kind == "READY":
                                    if lifecycle_success is not True:
                                        raise KVMProofFailure("workload-negative boot emitted lifecycle READY")
                                    lifecycle_ready = True
                                    if lifecycle_scenario == "negative":
                                        assert lifecycle_negative_name is not None
                                        if lifecycle_negative_name == "hello_reused_nonce":
                                            negative_reconnect_pending = True
                                        else:
                                            if lifecycle_negative_input is not None:
                                                lifecycle_negative_input["boot_generation"] = message.boot_generation
                                            if lifecycle_negative_name == "second_distinct_stop":
                                                queue_raw(
                                                    encode_frame(
                                                        OCIControlMessage(
                                                            "STOP",
                                                            lifecycle_binding,
                                                            LIFECYCLE_NEGATIVE_NONCE,
                                                            {"signal": 15},
                                                            request_id=2,
                                                            boot_generation=message.boot_generation,
                                                        )
                                                    )
                                                )
                                                composite_stage = "negative-await-stop-dispatched"
                                            else:
                                                queue_raw(
                                                    _lifecycle_negative_wire_bytes(
                                                        lifecycle_negative_name,
                                                        lifecycle_binding,
                                                        boot_generation=message.boot_generation,
                                                    ),
                                                    negative_input=True,
                                                )
                                elif message.kind == "TERMINAL":
                                    if lifecycle_success is not True or message.payload != {
                                        "terminal": {"exit_code": 42, "signal": None}
                                    }:
                                        raise KVMProofFailure("KVM lifecycle terminal status is invalid")
                                if lifecycle_scenario == "composite" and message.kind == "SNAPSHOT":
                                    state = message.payload["state"]
                                    if connection_ordinal == 2 and state == "ready":
                                        close_channel()
                                        composite_stage = "connect-3"
                                    elif connection_ordinal == 3 and state == "ready":
                                        stop = session.stop()
                                        queue_message(stop, complete=False)
                                        composite_stage = "partial-stop-4"
                                    elif connection_ordinal == 4 and state == "ready":
                                        committed_stop = session.stop()
                                        queue_message(committed_stop)
                                        composite_stage = "await-stop-dispatched-4"
                                    elif connection_ordinal == 5 and state == "stopping":
                                        composite_stage = "await-terminal"
                                    elif connection_ordinal == 6 and state == "terminal":
                                        composite_done = True
                        except OCIControlProtocolError:
                            raise KVMProofFailure("KVM lifecycle protocol was rejected") from None
            current = bytes(console)
            armed_count = _logical_line_count(current, WORKLOAD_SIGNAL_ARMED_MARKER)
            if armed_count > 1:
                raise KVMProofFailure("workload signal synchronization marker was emitted more than once")
            ready_committed = _logical_line_count(current, LIFECYCLE_READY_COMMITTED_MARKER)
            stop_dispatched = _logical_line_count(current, LIFECYCLE_STOP_DISPATCHED_MARKER)
            stop_duplicate = _logical_line_count(current, LIFECYCLE_STOP_DUPLICATE_MARKER)
            if lifecycle_scenario == "negative" and negative_reconnect_pending and ready_committed == 1:
                close_channel()
                negative_reconnect_pending = False
            if lifecycle_scenario == "composite" and connection_ordinal == 1 and ready_committed == 1:
                if channel_pending:
                    raise KVMProofFailure("initial lifecycle HELLO was not fully written")
                close_channel()
                composite_stage = "connect-2"
            if lifecycle_scenario == "composite" and stop_dispatched > 1:
                raise KVMProofFailure("lifecycle STOP was dispatched more than once")
            if lifecycle_scenario == "composite" and stop_duplicate > 1:
                raise KVMProofFailure("lifecycle duplicate STOP was accepted more than once")
            if (
                lifecycle_scenario == "negative"
                and lifecycle_negative_name == "second_distinct_stop"
                and composite_stage == "negative-await-stop-dispatched"
                and stop_dispatched == 1
            ):
                assert lifecycle_binding is not None and lifecycle_negative_input is not None
                observed_generation = lifecycle_negative_input.get("boot_generation")
                if not isinstance(observed_generation, str):
                    raise KVMProofFailure("KVM lifecycle negative boot generation was not observed")
                queue_raw(
                    _lifecycle_negative_wire_bytes(
                        lifecycle_negative_name,
                        lifecycle_binding,
                        boot_generation=observed_generation,
                    ),
                    negative_input=True,
                )
                composite_stage = "negative-second-stop-written"
            if (
                lifecycle_scenario == "composite"
                and connection_ordinal == 4
                and composite_stage == "await-stop-dispatched-4"
                and stop_dispatched == 1
            ):
                if channel_pending:
                    raise KVMProofFailure("retransmitted lifecycle STOP was not fully written")
                assert committed_stop is not None
                queue_message(committed_stop)
                composite_stage = "duplicate-stop-4"
            if (
                lifecycle_scenario == "composite"
                and connection_ordinal == 4
                and composite_stage == "duplicate-stop-4"
                and stop_duplicate == 1
            ):
                if channel_pending:
                    raise KVMProofFailure("duplicate lifecycle STOP was not fully written")
                close_channel()
                composite_stage = "connect-5"
            if (
                lifecycle_scenario == "composite"
                and connection_ordinal == 5
                and session is not None
                and session.state == "terminal"
            ):
                close_channel()
                composite_stage = "connect-6"
            if (
                lifecycle_scenario == "normal"
                and lifecycle_ready
                and armed_count == 1
                and session is not None
                and session.state == "ready"
            ):
                queue_message(session.stop())
            if any(_logical_line_count(current, marker) for marker in forbidden):
                raise KVMProofFailure("QEMU emitted a forbidden stage-1 marker")
            if _logical_prefix_count(current, WORKLOAD_CLEANUP_REJECTION_PREFIX):
                raise KVMProofFailure("QEMU emitted a workload cleanup rejection marker")
            if lifecycle_scenario != "negative" and _logical_prefix_count(current, LIFECYCLE_REJECTION_PREFIX):
                raise KVMProofFailure("QEMU emitted a lifecycle rejection marker")
            if lifecycle_scenario == "negative" and _logical_prefix_count(current, LIFECYCLE_REJECTION_PREFIX) > 1:
                raise KVMProofFailure("QEMU emitted lifecycle rejection more than once")
            terminal_count = _logical_prefix_count(current, WORKLOAD_TERMINAL_PREFIX)
            successful_terminal_count = _logical_line_count(current, SUCCESS_MARKER)
            if terminal_count > successful_terminal_count:
                raise KVMProofFailure("QEMU emitted an unexpected workload terminal marker")
            if terminal_count > 1:
                raise KVMProofFailure("QEMU emitted the workload terminal marker more than once")
            count = _logical_line_count(current, expected)
            if count > 1:
                raise KVMProofFailure("QEMU emitted the proof marker more than once")
            if count == 1 and marker_seen_at is None:
                if process.poll() is not None:
                    raise KVMProofFailure("QEMU exited at the proof marker")
                marker_seen_at = now
            lifecycle_complete = (
                session is None
                or (lifecycle_success is True and session.state == "terminal")
                or (lifecycle_success is False and session.state == "hello-sent")
            )
            if lifecycle_scenario == "composite":
                lifecycle_complete = composite_done and connection_ordinal == 6
            elif lifecycle_scenario == "negative":
                lifecycle_complete = negative_input_written
            if (
                marker_seen_at is not None
                and lifecycle_complete
                and (not require_alive_after_marker or now - marker_seen_at >= 0.25)
            ):
                if process.poll() is not None:
                    raise KVMProofFailure("QEMU did not remain alive after the proof marker")
                return bytes(console)
            if process.poll() is not None:
                for _ in range(8):
                    try:
                        chunk = os.read(descriptor, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    console.extend(chunk)
                raise KVMProofFailure(f"QEMU exited before the proof marker ({process.returncode})")
    except OSError:
        raise KVMProofFailure("KVM lifecycle or console I/O failed") from None
    finally:
        close_channel()
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        process.stdout.close()


def _read_qemu_duplicate_name_rejection(command: tuple[str, ...]) -> tuple[bytes, int]:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise KVMProofFailure("QEMU duplicate lifecycle name control failed") from None
    output = completed.stdout
    if (
        completed.returncode <= 0
        or not isinstance(output, bytes)
        or not 1 <= len(output) <= MAX_QEMU_VERSION_BYTES
        or output.count(QEMU_DUPLICATE_NAME_REJECTION_MARKER) != 1
        or b"palimpsest guest stage1:" in output
    ):
        raise KVMProofFailure("QEMU did not reject duplicate lifecycle names before guest boot")
    return output, completed.returncode


def _temp_root() -> tempfile.TemporaryDirectory[str]:
    temporary = tempfile.TemporaryDirectory(prefix="palimpsest-stage1-kvm-")
    os.chmod(temporary.name, 0o700)
    metadata = os.stat(temporary.name)
    if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        temporary.cleanup()
        raise KVMProofFailure("KVM proof temporary directory is not private")
    return temporary


def _verify_file_digest(path: Path, expected_digest: str, expected_mode: int) -> None:
    verified = _read_pinned_regular_file(path, maximum=MAX_KERNEL_BYTES, label="KVM proof temporary artifact")
    if verified.digest != expected_digest or stat.S_IMODE(path.stat().st_mode) != expected_mode:
        raise KVMProofFailure("KVM proof temporary artifact changed")


def _materialize_negative_backings(
    directory: Path,
    contracts: Mapping[str, Mapping[str, Any]],
    transport: BuiltOCIStage1Transport,
) -> dict[str, dict[str, Path]]:
    """Materialize each distinct path-free backing contract once in the private root."""

    cache: dict[tuple[str, str, int, int], Path] = {}
    result: dict[str, dict[str, Path]] = {}
    for control_name in NEGATIVE_CONTROL_NAMES:
        contract = verify_negative_control_contract(control_name, contracts.get(control_name))
        result[control_name] = {}
        for backing_name, backing in contract["backings"].items():
            # Preserve distinct guest disks even when two roles intentionally
            # contain identical bytes (for example lower0 and extra_disk).
            signature = (backing_name, backing["artifact_digest"], backing["size_bytes"], backing["mode"])
            path = cache.get(signature)
            if path is None:
                if backing["artifact_digest"] == transport.receipt.artifact_digest:
                    payload = transport.artifact
                else:
                    filesystems = load_proof_filesystems()
                    candidates = (
                        filesystems.root,
                        filesystems.lowers[0],
                        filesystems.lowers[1],
                        filesystems.root[:-512],
                        filesystems.root + b"\0" * 512,
                        filesystems.lowers[0][:-512],
                        filesystems.lowers[0] + b"\0" * 512,
                        b"\0" * backing["size_bytes"],
                    )
                    payload = next(
                        (
                            candidate
                            for candidate in candidates
                            if len(candidate) == backing["size_bytes"]
                            and _digest(candidate) == backing["artifact_digest"]
                        ),
                        b"",
                    )
                    if not payload:
                        raise KVMProofFailure("KVM negative backing contract digest is invalid")
                if len(payload) != backing["size_bytes"]:
                    raise KVMProofFailure("KVM negative backing contract size is invalid")
                path = _secure_write(directory, f"negative-backing-{len(cache)}.raw", payload, mode=backing["mode"])
                cache[signature] = path
            result[control_name][backing_name] = path
    return result


def _verify_negative_backings(
    control_name: str,
    paths: Mapping[str, Path],
    contract: Mapping[str, Any],
) -> None:
    verified = verify_negative_control_contract(control_name, contract)
    if set(paths) != set(verified["backings"]):
        raise KVMProofFailure("KVM negative backing set changed")
    for backing_name, backing in verified["backings"].items():
        path = paths[backing_name]
        _verify_file_digest(path, backing["artifact_digest"], backing["mode"])
        if path.stat().st_size != backing["size_bytes"]:
            raise KVMProofFailure("KVM negative backing size changed")


def _materialize_filesystem_negative_backings(
    directory: Path,
    contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Path]]:
    filesystems = load_proof_filesystems()
    result: dict[str, dict[str, Path]] = {}
    for control_name in FILESYSTEM_NEGATIVE_CONTROL_NAMES:
        contract = verify_filesystem_negative_control_contract(control_name, contracts.get(control_name))
        _control_plan, control_transport = _filesystem_negative_context(control_name)
        mutated_name, mutated_payload = _mutated_filesystem_payload(control_name)
        candidates = {
            "transport": control_transport.artifact,
            "root": filesystems.root,
            "lower0": filesystems.lowers[0],
            "lower1": filesystems.lowers[1],
        }
        candidates[mutated_name] = mutated_payload
        paths: dict[str, Path] = {}
        for backing_name, backing in contract["backings"].items():
            payload = candidates[backing_name]
            if len(payload) != backing["size_bytes"] or _digest(payload) != backing["artifact_digest"]:
                raise KVMProofFailure("KVM filesystem negative backing contract is invalid")
            paths[backing_name] = _secure_write(
                directory,
                f"filesystem-negative-{control_name}-{backing_name}.raw",
                payload,
                mode=backing["mode"],
            )
        result[control_name] = paths
    return result


def _verify_filesystem_negative_backings(
    control_name: str,
    paths: Mapping[str, Path],
    contract: Mapping[str, Any],
) -> None:
    verified = verify_filesystem_negative_control_contract(control_name, contract)
    if set(paths) != set(verified["backings"]):
        raise KVMProofFailure("KVM filesystem negative backing set changed")
    for backing_name, backing in verified["backings"].items():
        path = paths[backing_name]
        _verify_file_digest(path, backing["artifact_digest"], backing["mode"])
        if path.stat().st_size != backing["size_bytes"]:
            raise KVMProofFailure("KVM filesystem negative backing size changed")


def _materialize_assembly_negative_backings(
    directory: Path,
    contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Path]]:
    filesystems = load_proof_filesystems()
    result: dict[str, dict[str, Path]] = {}
    for control_name in ASSEMBLY_NEGATIVE_CONTROL_NAMES:
        contract = verify_assembly_negative_control_contract(control_name, contracts.get(control_name))
        _control_plan, control_transport = _assembly_negative_context(control_name)
        payloads = {
            "transport": control_transport.artifact,
            "root": filesystems.root,
            "lower0": filesystems.lowers[0],
            "lower1": filesystems.lowers[1],
        }
        paths: dict[str, Path] = {}
        for backing_name, backing in contract["backings"].items():
            payload = payloads[backing_name]
            if len(payload) != backing["size_bytes"] or _digest(payload) != backing["artifact_digest"]:
                raise KVMProofFailure("KVM assembly negative backing contract is invalid")
            paths[backing_name] = _secure_write(
                directory,
                f"assembly-negative-{control_name}-{backing_name}.raw",
                payload,
                mode=backing["mode"],
            )
        result[control_name] = paths
    return result


def _verify_assembly_negative_backings(
    control_name: str,
    paths: Mapping[str, Path],
    contract: Mapping[str, Any],
    *,
    after_boot: bool = False,
) -> str:
    verified = verify_assembly_negative_control_contract(control_name, contract)
    if set(paths) != set(verified["backings"]):
        raise KVMProofFailure("KVM assembly negative backing set changed")
    for backing_name, backing in verified["backings"].items():
        path = paths[backing_name]
        if backing_name == "root" and after_boot:
            opened = _read_pinned_regular_file(
                path, maximum=MAX_KERNEL_BYTES, label="KVM assembly control mutable root"
            )
            if stat.S_IMODE(path.stat().st_mode) != backing["mode"]:
                raise KVMProofFailure("KVM assembly control root mode changed")
            root_digest = opened.digest
        else:
            _verify_file_digest(path, backing["artifact_digest"], backing["mode"])
        if path.stat().st_size != backing["size_bytes"]:
            raise KVMProofFailure("KVM assembly negative backing size changed")
    return root_digest if after_boot else verified["backings"]["root"]["artifact_digest"]


def _materialize_root_transition_negative_backings(
    directory: Path,
    contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Path]]:
    filesystems = load_proof_filesystems()
    result: dict[str, dict[str, Path]] = {}
    for control_name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES:
        contract = verify_root_transition_negative_control_contract(control_name, contracts.get(control_name))
        role = contract["target"]
        _control_plan, control_transport = _root_transition_negative_context(control_name)
        payloads = {
            "transport": control_transport.artifact,
            "root": filesystems.root,
            "lower0": filesystems.lowers[0],
            "lower1": filesystems.transition_lowers[role],
        }
        paths: dict[str, Path] = {}
        for backing_name, backing in contract["backings"].items():
            payload = payloads[backing_name]
            if len(payload) != backing["size_bytes"] or _digest(payload) != backing["artifact_digest"]:
                raise KVMProofFailure("KVM root-transition negative backing contract is invalid")
            paths[backing_name] = _secure_write(
                directory,
                f"root-transition-negative-{control_name}-{backing_name}.raw",
                payload,
                mode=backing["mode"],
            )
        result[control_name] = paths
    return result


def _verify_root_transition_negative_backings(
    control_name: str,
    paths: Mapping[str, Path],
    contract: Mapping[str, Any],
    *,
    after_boot: bool = False,
) -> str:
    verified = verify_root_transition_negative_control_contract(control_name, contract)
    if set(paths) != set(verified["backings"]):
        raise KVMProofFailure("KVM root-transition negative backing set changed")
    root_digest = verified["backings"]["root"]["artifact_digest"]
    for backing_name, backing in verified["backings"].items():
        path = paths[backing_name]
        if backing_name == "root" and after_boot:
            opened = _read_pinned_regular_file(
                path, maximum=MAX_KERNEL_BYTES, label="KVM root-transition control mutable root"
            )
            if stat.S_IMODE(path.stat().st_mode) != backing["mode"]:
                raise KVMProofFailure("KVM root-transition control root mode changed")
            root_digest = opened.digest
        else:
            _verify_file_digest(path, backing["artifact_digest"], backing["mode"])
        if path.stat().st_size != backing["size_bytes"]:
            raise KVMProofFailure("KVM root-transition negative backing size changed")
    return root_digest


def _materialize_workload_negative_backings(
    directory: Path,
    contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Path]]:
    filesystems = load_proof_filesystems()
    result: dict[str, dict[str, Path]] = {}
    for control_name in WORKLOAD_NEGATIVE_CONTROL_NAMES:
        contract = verify_workload_negative_control_contract(control_name, contracts.get(control_name))
        _control_plan, control_transport = _workload_negative_context(control_name)
        payloads = {
            "transport": control_transport.artifact,
            "root": filesystems.root,
            "lower0": filesystems.lowers[0],
            "lower1": filesystems.lowers[1],
        }
        paths: dict[str, Path] = {}
        for backing_name, backing in contract["backings"].items():
            payload = payloads[backing_name]
            if len(payload) != backing["size_bytes"] or _digest(payload) != backing["artifact_digest"]:
                raise KVMProofFailure("KVM workload negative backing contract is invalid")
            paths[backing_name] = _secure_write(
                directory,
                f"workload-negative-{control_name}-{backing_name}.raw",
                payload,
                mode=backing["mode"],
            )
        result[control_name] = paths
    return result


def _verify_workload_negative_backings(
    control_name: str,
    paths: Mapping[str, Path],
    contract: Mapping[str, Any],
    *,
    after_boot: bool = False,
) -> str:
    verified = verify_workload_negative_control_contract(control_name, contract)
    if set(paths) != set(verified["backings"]):
        raise KVMProofFailure("KVM workload negative backing set changed")
    root_digest = verified["backings"]["root"]["artifact_digest"]
    for backing_name, backing in verified["backings"].items():
        path = paths[backing_name]
        if backing_name == "root" and after_boot:
            opened = _read_pinned_regular_file(
                path, maximum=MAX_KERNEL_BYTES, label="KVM workload control mutable root"
            )
            if stat.S_IMODE(path.stat().st_mode) != backing["mode"]:
                raise KVMProofFailure("KVM workload control root mode changed")
            root_digest = opened.digest
        else:
            _verify_file_digest(path, backing["artifact_digest"], backing["mode"])
        if path.stat().st_size != backing["size_bytes"]:
            raise KVMProofFailure("KVM workload negative backing size changed")
    return root_digest


def _materialize_lifecycle_negative_backings(
    directory: Path,
    contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Path]]:
    filesystems = load_proof_filesystems()
    _plan, transport, _attachments, _backings = _base_lifecycle_control_artifacts()
    payloads = {
        "transport": transport.artifact,
        "root": filesystems.root,
        "lower0": filesystems.lowers[0],
        "lower1": filesystems.lowers[1],
    }
    result: dict[str, dict[str, Path]] = {}
    for control_name in LIFECYCLE_NEGATIVE_CONTROL_NAMES:
        contract = verify_lifecycle_negative_control_contract(control_name, contracts.get(control_name))
        paths: dict[str, Path] = {}
        for backing_name, backing in contract["backings"].items():
            payload = payloads[backing_name]
            if len(payload) != backing["size_bytes"] or _digest(payload) != backing["artifact_digest"]:
                raise KVMProofFailure("KVM lifecycle negative backing contract is invalid")
            paths[backing_name] = _secure_write(
                directory,
                f"lifecycle-negative-{control_name}-{backing_name}.raw",
                payload,
                mode=backing["mode"],
            )
        result[control_name] = paths
    return result


def _verify_lifecycle_negative_backings(
    control_name: str,
    paths: Mapping[str, Path],
    contract: Mapping[str, Any],
    *,
    after_boot: bool = False,
) -> str:
    verified = verify_lifecycle_negative_control_contract(control_name, contract)
    if set(paths) != set(verified["backings"]):
        raise KVMProofFailure("KVM lifecycle negative backing set changed")
    root_digest = verified["backings"]["root"]["artifact_digest"]
    for backing_name, backing in verified["backings"].items():
        path = paths[backing_name]
        if backing_name == "root" and after_boot:
            opened = _read_pinned_regular_file(
                path, maximum=MAX_KERNEL_BYTES, label="KVM lifecycle control mutable root"
            )
            if stat.S_IMODE(path.stat().st_mode) != backing["mode"]:
                raise KVMProofFailure("KVM lifecycle control root mode changed")
            root_digest = opened.digest
        else:
            _verify_file_digest(path, backing["artifact_digest"], backing["mode"])
        if path.stat().st_size != backing["size_bytes"]:
            raise KVMProofFailure("KVM lifecycle negative backing size changed")
    return root_digest


def _verify_pinned_boot_files(
    *,
    qemu_path: Path,
    qemu: VerifiedHostFile,
    kernel_path: Path,
    kernel: VerifiedHostFile,
    initramfs_path: Path,
    initramfs_digest: str,
) -> None:
    _verify_file_digest(qemu_path, qemu.digest, 0o500)
    _verify_file_digest(kernel_path, kernel.digest, 0o400)
    _verify_file_digest(initramfs_path, initramfs_digest, 0o400)


def _write_pre_mount_topology(directory: Path, plan: OCIStage1Plan) -> tuple[Path, tuple[Path, ...]]:
    topology = pre_mount_topology(plan)["devices"]
    filesystems = load_proof_filesystems()
    root_contract = topology[0]
    root_path = _secure_write(directory, "root.raw", filesystems.root, mode=0o600)
    lower_paths = tuple(
        _secure_write(
            directory,
            f"lower-{layer['ordinal']}.raw",
            filesystems.lowers[layer["ordinal"]],
            mode=0o400,
        )
        for layer in topology[1:]
    )
    _verify_file_digest(root_path, root_contract["artifact_digest"], 0o600)
    for path, layer in zip(lower_paths, topology[1:], strict=True):
        _verify_file_digest(path, layer["artifact_digest"], 0o400)
    return root_path, lower_paths


def _verify_pre_mount_topology(root_path: Path, lower_paths: tuple[Path, ...], plan: OCIStage1Plan) -> None:
    devices = pre_mount_topology(plan)["devices"]
    _verify_file_digest(root_path, devices[0]["artifact_digest"], 0o600)
    for path, layer in zip(lower_paths, devices[1:], strict=True):
        _verify_file_digest(path, layer["artifact_digest"], 0o400)


def _verify_post_run_topology(root_path: Path, lower_paths: tuple[Path, ...], plan: OCIStage1Plan) -> str:
    """Bind the mutable root's post-run bytes while retaining immutable lower equality."""

    verified_root = _read_pinned_regular_file(
        root_path,
        maximum=MAX_KERNEL_BYTES,
        label="KVM proof mutable root",
    )
    if verified_root.size_bytes != plan.root["size_bytes"] or stat.S_IMODE(root_path.stat().st_mode) != 0o600:
        raise KVMProofFailure("KVM proof mutable root identity changed")
    devices = pre_mount_topology(plan)["devices"]
    for path, layer in zip(lower_paths, devices[1:], strict=True):
        _verify_file_digest(path, layer["artifact_digest"], 0o400)
    return verified_root.digest


def _actual_evidence_directory() -> Path | None:
    configured = os.environ.get(EVIDENCE_ENV)
    if configured is None:
        return None
    return verify_evidence_directory(Path(configured))


def run_oci_stage1_kvm_proof() -> OCIStage1KVMProofResult:
    """Direct-boot the packaged consumer and retain a qualified receipt."""

    api_version = verify_kvm_api()
    if api_version != REQUIRED_KVM_API_VERSION:  # pragma: no cover - guarded above
        raise KVMProofUnavailable("KVM API is not qualified")
    verify_kernel_configuration_selection()
    kernel = select_linux_bzimage()
    kernel_config = select_kernel_config()
    qemu = select_qemu()
    version = qemu_version(qemu)
    initramfs = build_bootstrap_initramfs()
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    cmdline = build_kernel_cmdline(plan, transport)
    serial = transport_serial(transport.receipt.artifact_digest)
    lifecycle_binding = OCIControlBinding(
        run_id=plan.run_id,
        domain_core_digest=plan.domain_core_digest,
        stage1_artifact_digest=transport.receipt.artifact_digest,
    )
    evidence = _actual_evidence_directory()

    with _temp_root() as temporary_name:
        root = Path(temporary_name)
        qemu_path = _secure_write(root, "qemu-system-x86_64", qemu.payload, mode=0o500)
        kernel_path = _secure_write(root, "kernel", kernel.payload, mode=0o400)
        initramfs_path = _secure_write(root, "initramfs.cpio", initramfs.payload, mode=0o400)
        transport_path = _secure_write(root, "stage1-plan.raw", transport.artifact, mode=0o400)
        root_path, lower_paths = _write_pre_mount_topology(root, plan)
        contracts = negative_control_contracts()
        negative_backings = _materialize_negative_backings(root, contracts, transport)
        filesystem_contracts = filesystem_negative_control_contracts()
        filesystem_negative_backings = _materialize_filesystem_negative_backings(root, filesystem_contracts)
        assembly_contracts = assembly_negative_control_contracts()
        assembly_negative_backings = _materialize_assembly_negative_backings(root, assembly_contracts)
        root_transition_contracts = root_transition_negative_control_contracts()
        root_transition_negative_backings = _materialize_root_transition_negative_backings(
            root, root_transition_contracts
        )
        workload_contracts = workload_negative_control_contracts()
        workload_negative_backings = _materialize_workload_negative_backings(root, workload_contracts)
        lifecycle_contracts = lifecycle_negative_control_contracts()
        lifecycle_negative_backings = _materialize_lifecycle_negative_backings(root, lifecycle_contracts)
        _verify_pinned_boot_files(
            qemu_path=qemu_path,
            qemu=qemu,
            kernel_path=kernel_path,
            kernel=kernel,
            initramfs_path=initramfs_path,
            initramfs_digest=initramfs.manifest.artifact_digest,
        )
        _verify_file_digest(transport_path, transport.receipt.artifact_digest, 0o400)

        first_lifecycle_socket = root / "lifecycle-first.sock"
        command = build_qemu_command(
            qemu_path=qemu_path,
            kernel_path=kernel_path,
            initramfs_path=initramfs_path,
            transport_path=transport_path,
            root_path=root_path,
            lower_paths=lower_paths,
            plan=plan,
            cmdline=cmdline,
            serial=serial,
            lifecycle_socket_path=first_lifecycle_socket,
        )
        lifecycle_transcript: list[dict[str, Any]] = []
        console = _read_console_until(
            command,
            expected=SUCCESS_MARKER,
            forbidden=(
                REJECTION_MARKER,
                FILESYSTEM_REJECTION_MARKER,
                ASSEMBLY_REJECTION_MARKER,
                ROOT_TRANSITION_REJECTION_MARKER,
                PREPARATION_FAILURE_MARKER,
                *WORKLOAD_NEGATIVE_REJECTION_MARKERS.values(),
            ),
            timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
            require_alive_after_marker=True,
            lifecycle_socket_path=first_lifecycle_socket,
            lifecycle_binding=lifecycle_binding,
            lifecycle_success=True,
            lifecycle_transcript=lifecycle_transcript,
        )
        _verify_pinned_boot_files(
            qemu_path=qemu_path,
            qemu=qemu,
            kernel_path=kernel_path,
            kernel=kernel,
            initramfs_path=initramfs_path,
            initramfs_digest=initramfs.manifest.artifact_digest,
        )
        _verify_file_digest(transport_path, transport.receipt.artifact_digest, 0o400)
        root_post_run_digest = _verify_post_run_topology(root_path, lower_paths, plan)
        # The second boot consumes the exact mutable backing left by boot one.
        if _verify_post_run_topology(root_path, lower_paths, plan) != root_post_run_digest:
            raise KVMProofFailure("KVM retained root changed between boots")
        second_lifecycle_socket = root / "lifecycle-second.sock"
        retained_command = build_qemu_command(
            qemu_path=qemu_path,
            kernel_path=kernel_path,
            initramfs_path=initramfs_path,
            transport_path=transport_path,
            root_path=root_path,
            lower_paths=lower_paths,
            plan=plan,
            cmdline=cmdline,
            serial=serial,
            lifecycle_socket_path=second_lifecycle_socket,
        )
        retained_lifecycle_transcript: list[dict[str, Any]] = []
        retained_lifecycle_attempts: list[dict[str, Any]] = []
        retained_console = _read_console_until(
            retained_command,
            expected=SUCCESS_MARKER,
            forbidden=(
                REJECTION_MARKER,
                FILESYSTEM_REJECTION_MARKER,
                ASSEMBLY_REJECTION_MARKER,
                ROOT_TRANSITION_REJECTION_MARKER,
                PREPARATION_FAILURE_MARKER,
                *WORKLOAD_NEGATIVE_REJECTION_MARKERS.values(),
            ),
            timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
            require_alive_after_marker=True,
            lifecycle_socket_path=second_lifecycle_socket,
            lifecycle_binding=lifecycle_binding,
            lifecycle_success=True,
            lifecycle_transcript=retained_lifecycle_transcript,
            lifecycle_scenario="composite",
            lifecycle_attempts=retained_lifecycle_attempts,
        )
        _verify_pinned_boot_files(
            qemu_path=qemu_path,
            qemu=qemu,
            kernel_path=kernel_path,
            kernel=kernel,
            initramfs_path=initramfs_path,
            initramfs_digest=initramfs.manifest.artifact_digest,
        )
        _verify_file_digest(transport_path, transport.receipt.artifact_digest, 0o400)
        root_second_boot_digest = _verify_post_run_topology(root_path, lower_paths, plan)
        negative_consoles: dict[str, bytes] = {}
        for control_name in NEGATIVE_CONTROL_NAMES:
            contract = contracts[control_name]
            backing_paths = negative_backings[control_name]
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_negative_backings(control_name, backing_paths, contract)
            control_command = build_negative_qemu_command(
                qemu_path=qemu_path,
                kernel_path=kernel_path,
                initramfs_path=initramfs_path,
                backing_paths=backing_paths,
                cmdline=cmdline,
                control=contract,
            )
            negative_consoles[control_name] = _read_console_until(
                control_command,
                expected=REJECTION_MARKER,
                forbidden=(
                    SUCCESS_MARKER,
                    FILESYSTEM_REJECTION_MARKER,
                    ASSEMBLY_REJECTION_MARKER,
                    ROOT_TRANSITION_REJECTION_MARKER,
                    PREPARATION_FAILURE_MARKER,
                ),
                timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                require_alive_after_marker=True,
            )
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_negative_backings(control_name, backing_paths, contract)
        filesystem_negative_consoles: dict[str, bytes] = {}
        for control_name in FILESYSTEM_NEGATIVE_CONTROL_NAMES:
            contract = filesystem_contracts[control_name]
            backing_paths = filesystem_negative_backings[control_name]
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_filesystem_negative_backings(control_name, backing_paths, contract)
            control_command = build_filesystem_negative_qemu_command(
                qemu_path=qemu_path,
                kernel_path=kernel_path,
                initramfs_path=initramfs_path,
                backing_paths=backing_paths,
                cmdline=contract["cmdline"],
                control=contract,
            )
            filesystem_negative_consoles[control_name] = _read_console_until(
                control_command,
                expected=FILESYSTEM_REJECTION_MARKER,
                forbidden=(
                    SUCCESS_MARKER,
                    REJECTION_MARKER,
                    ASSEMBLY_REJECTION_MARKER,
                    ROOT_TRANSITION_REJECTION_MARKER,
                    PREPARATION_FAILURE_MARKER,
                ),
                timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                require_alive_after_marker=True,
            )
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_filesystem_negative_backings(control_name, backing_paths, contract)
        assembly_negative_consoles: dict[str, bytes] = {}
        assembly_negative_root_post_digests: dict[str, str] = {}
        for control_name in ASSEMBLY_NEGATIVE_CONTROL_NAMES:
            contract = assembly_contracts[control_name]
            backing_paths = assembly_negative_backings[control_name]
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_assembly_negative_backings(control_name, backing_paths, contract)
            control_command = build_assembly_negative_qemu_command(
                qemu_path=qemu_path,
                kernel_path=kernel_path,
                initramfs_path=initramfs_path,
                backing_paths=backing_paths,
                cmdline=contract["cmdline"],
                control=contract,
            )
            assembly_negative_consoles[control_name] = _read_console_until(
                control_command,
                expected=ASSEMBLY_REJECTION_MARKER,
                forbidden=(
                    SUCCESS_MARKER,
                    REJECTION_MARKER,
                    FILESYSTEM_REJECTION_MARKER,
                    ROOT_TRANSITION_REJECTION_MARKER,
                    PREPARATION_FAILURE_MARKER,
                ),
                timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                require_alive_after_marker=True,
            )
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            assembly_negative_root_post_digests[control_name] = _verify_assembly_negative_backings(
                control_name, backing_paths, contract, after_boot=True
            )
        root_transition_negative_consoles: dict[str, bytes] = {}
        root_transition_negative_root_post_digests: dict[str, str] = {}
        for control_name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES:
            contract = root_transition_contracts[control_name]
            backing_paths = root_transition_negative_backings[control_name]
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_root_transition_negative_backings(control_name, backing_paths, contract)
            control_command = build_root_transition_negative_qemu_command(
                qemu_path=qemu_path,
                kernel_path=kernel_path,
                initramfs_path=initramfs_path,
                backing_paths=backing_paths,
                cmdline=contract["cmdline"],
                control=contract,
            )
            root_transition_negative_consoles[control_name] = _read_console_until(
                control_command,
                expected=ROOT_TRANSITION_REJECTION_MARKER,
                forbidden=(
                    SUCCESS_MARKER,
                    REJECTION_MARKER,
                    FILESYSTEM_REJECTION_MARKER,
                    ASSEMBLY_REJECTION_MARKER,
                    PREPARATION_FAILURE_MARKER,
                ),
                timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                require_alive_after_marker=True,
            )
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            root_transition_negative_root_post_digests[control_name] = _verify_root_transition_negative_backings(
                control_name, backing_paths, contract, after_boot=True
            )
        workload_negative_consoles: dict[str, bytes] = {}
        workload_negative_root_post_digests: dict[str, str] = {}
        for control_name in WORKLOAD_NEGATIVE_CONTROL_NAMES:
            contract = workload_contracts[control_name]
            backing_paths = workload_negative_backings[control_name]
            control_plan, _control_transport = _workload_negative_context(control_name)
            control_binding = OCIControlBinding(
                run_id=control_plan.run_id,
                domain_core_digest=control_plan.domain_core_digest,
                stage1_artifact_digest=contract["stage1_transport"]["artifact_digest"],
            )
            control_lifecycle_socket = root / f"lifecycle-{control_name}.sock"
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_workload_negative_backings(control_name, backing_paths, contract)
            control_command = build_workload_negative_qemu_command(
                qemu_path=qemu_path,
                kernel_path=kernel_path,
                initramfs_path=initramfs_path,
                backing_paths=backing_paths,
                cmdline=contract["cmdline"],
                control=contract,
                lifecycle_socket_path=control_lifecycle_socket,
            )
            workload_negative_consoles[control_name] = _read_console_until(
                control_command,
                expected=WORKLOAD_NEGATIVE_REJECTION_MARKERS[control_name],
                forbidden=(
                    WORKLOAD_STARTED_MARKER,
                    WORKLOAD_TERMINAL_MARKER,
                    REJECTION_MARKER,
                    FILESYSTEM_REJECTION_MARKER,
                    ASSEMBLY_REJECTION_MARKER,
                    ROOT_TRANSITION_REJECTION_MARKER,
                    PREPARATION_FAILURE_MARKER,
                ),
                timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                require_alive_after_marker=True,
                lifecycle_socket_path=control_lifecycle_socket,
                lifecycle_binding=control_binding,
                lifecycle_success=False,
            )
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            workload_negative_root_post_digests[control_name] = _verify_workload_negative_backings(
                control_name, backing_paths, contract, after_boot=True
            )
        lifecycle_negative_consoles: dict[str, bytes] = {}
        lifecycle_negative_root_post_digests: dict[str, str] = {}
        lifecycle_negative_inputs: dict[str, dict[str, Any]] = {}
        for control_name in LIFECYCLE_NEGATIVE_CONTROL_NAMES:
            contract = lifecycle_contracts[control_name]
            backing_paths = lifecycle_negative_backings[control_name]
            socket_path = None if control_name == "lifecycle_missing_port" else root / f"lifecycle-{control_name}.sock"
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            _verify_lifecycle_negative_backings(control_name, backing_paths, contract)
            control_command = build_lifecycle_negative_qemu_command(
                qemu_path=qemu_path,
                kernel_path=kernel_path,
                initramfs_path=initramfs_path,
                backing_paths=backing_paths,
                cmdline=contract["cmdline"],
                control=contract,
                lifecycle_socket_path=socket_path,
            )
            exact_rejection = contract["rejection_marker"].encode("ascii")
            forbidden = (
                SUCCESS_MARKER,
                WORKLOAD_TERMINAL_MARKER,
                REJECTION_MARKER,
                FILESYSTEM_REJECTION_MARKER,
                ASSEMBLY_REJECTION_MARKER,
                ROOT_TRANSITION_REJECTION_MARKER,
                PREPARATION_FAILURE_MARKER,
            )
            wire_input: dict[str, Any] = {}
            if control_name in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES:
                assert socket_path is not None
                lifecycle_negative_consoles[control_name] = _read_console_until(
                    control_command,
                    expected=exact_rejection,
                    forbidden=forbidden,
                    timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                    require_alive_after_marker=True,
                    lifecycle_socket_path=socket_path,
                    lifecycle_binding=lifecycle_binding,
                    lifecycle_success=True,
                    lifecycle_scenario="negative",
                    lifecycle_negative_name=control_name,
                    lifecycle_negative_input=wire_input,
                )
                lifecycle_negative_inputs[control_name] = wire_input
            else:
                lifecycle_negative_consoles[control_name] = _read_console_until(
                    control_command,
                    expected=exact_rejection,
                    forbidden=forbidden,
                    timeout_seconds=DEFAULT_BOOT_TIMEOUT_SECONDS,
                    require_alive_after_marker=True,
                )
            _verify_pinned_boot_files(
                qemu_path=qemu_path,
                qemu=qemu,
                kernel_path=kernel_path,
                kernel=kernel,
                initramfs_path=initramfs_path,
                initramfs_digest=initramfs.manifest.artifact_digest,
            )
            lifecycle_negative_root_post_digests[control_name] = _verify_lifecycle_negative_backings(
                control_name, backing_paths, contract, after_boot=True
            )

        duplicate_contract = lifecycle_contracts["lifecycle_missing_port"]
        duplicate_payloads = {
            "transport": transport.artifact,
            "root": load_proof_filesystems().root,
            "lower0": load_proof_filesystems().lowers[0],
            "lower1": load_proof_filesystems().lowers[1],
        }
        duplicate_backings = {
            name: _secure_write(root, f"duplicate-name-{name}.raw", duplicate_payloads[name], mode=backing["mode"])
            for name, backing in duplicate_contract["backings"].items()
        }
        _verify_lifecycle_negative_backings(
            "lifecycle_missing_port",
            duplicate_backings,
            duplicate_contract,
        )
        duplicate_command = build_duplicate_lifecycle_name_qemu_command(
            qemu_path=qemu_path,
            kernel_path=kernel_path,
            initramfs_path=initramfs_path,
            backing_paths=duplicate_backings,
            cmdline=cmdline,
            first_socket_path=root / "lifecycle-duplicate-first.sock",
            second_socket_path=root / "lifecycle-duplicate-second.sock",
        )
        qemu_duplicate_name_output, qemu_duplicate_name_exit_code = _read_qemu_duplicate_name_rejection(
            duplicate_command
        )
        _verify_pinned_boot_files(
            qemu_path=qemu_path,
            qemu=qemu,
            kernel_path=kernel_path,
            kernel=kernel,
            initramfs_path=initramfs_path,
            initramfs_digest=initramfs.manifest.artifact_digest,
        )
        _verify_lifecycle_negative_backings(
            "lifecycle_missing_port",
            duplicate_backings,
            duplicate_contract,
        )

    receipt = OCIStage1KVMProofReceipt(
        kernel.digest,
        kernel.size_bytes,
        kernel_config.digest,
        initramfs.manifest.artifact_digest,
        initramfs.manifest.artifact_size_bytes,
        initramfs.manifest.digest,
        initramfs.manifest.stage1_binary_digest,
        transport.receipt.to_dict(),
        serial,
        cmdline,
        qemu.digest,
        qemu.size_bytes,
        version,
        load_proof_filesystems().root_digest,
        root_post_run_digest,
        root_second_boot_digest,
        console,
        retained_console,
        negative_consoles,
        filesystem_negative_consoles,
        assembly_negative_consoles,
        assembly_negative_root_post_digests,
        root_transition_negative_consoles,
        root_transition_negative_root_post_digests,
        workload_negative_consoles,
        workload_negative_root_post_digests,
        lifecycle_negative_consoles,
        lifecycle_negative_root_post_digests,
        qemu_duplicate_name_output,
        _lifecycle_receipt(
            lifecycle_binding,
            lifecycle_transcript,
            retained_lifecycle_transcript,
            retained_lifecycle_attempts,
            lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests,
            lifecycle_negative_inputs,
            qemu_duplicate_name_output,
            qemu_duplicate_name_exit_code,
            console,
            retained_console,
        ),
    )
    if evidence is not None:
        _secure_write(evidence, "console.bin", console, mode=0o400)
        _secure_write(evidence, "retained-console.bin", retained_console, mode=0o400)
        for control_name in NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"negative-{control_name}.bin",
                negative_consoles[control_name],
                mode=0o400,
            )
        for control_name in FILESYSTEM_NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"filesystem-negative-{control_name}.bin",
                filesystem_negative_consoles[control_name],
                mode=0o400,
            )
        for control_name in ASSEMBLY_NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"assembly-negative-{control_name}.bin",
                assembly_negative_consoles[control_name],
                mode=0o400,
            )
        for control_name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"root-transition-negative-{control_name}.bin",
                root_transition_negative_consoles[control_name],
                mode=0o400,
            )
        for control_name in WORKLOAD_NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"workload-negative-{control_name}.bin",
                workload_negative_consoles[control_name],
                mode=0o400,
            )
        for control_name in LIFECYCLE_NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"lifecycle-negative-{control_name}.bin",
                lifecycle_negative_consoles[control_name],
                mode=0o400,
            )
        _secure_write(evidence, "qemu-duplicate-lifecycle-name.bin", qemu_duplicate_name_output, mode=0o400)
        _secure_write(evidence, "receipt.json", receipt.canonical_bytes, mode=0o400)
    return OCIStage1KVMProofResult(
        receipt,
        console,
        retained_console,
        negative_consoles,
        filesystem_negative_consoles,
        assembly_negative_consoles,
        root_transition_negative_consoles,
        workload_negative_consoles,
        lifecycle_negative_consoles,
        qemu_duplicate_name_output,
        evidence,
    )
