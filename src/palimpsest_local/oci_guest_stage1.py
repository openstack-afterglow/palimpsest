"""First-party guest consumer for the OCI stage-1 plan transport.

This module is the portable transport and block-role reference shared with the
packaged freestanding x86_64 ``/init`` implementation. Filesystem byte parsing
lives in :mod:`oci_guest_filesystems`. The packaged PID 1 additionally mounts
the authenticated filesystems, assembles OverlayFS, and performs an
initramfs-safe move-mount/chroot root transition. This portable reference does
not mount, transition root, or execute a process from the image.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, StateError
from .oci_packer import SQUASHFS_BLOCK_DEVICE_ALIGNMENT
from .oci_process import OCIProcessSpec
from .oci_provenance import canonical_json_bytes
from .oci_stage1 import (
    OCI_STAGE1_DEVICE_POLICY,
    OCI_STAGE1_HANDOFF,
    OCI_STAGE1_PLAN_SCHEMA,
    OCI_STAGE1_PROCESS_POLICY,
    OCI_STAGE1_PROTOCOL,
    OCI_STAGE1_ROOT_LAYOUT,
    OCIStage1Plan,
)
from .oci_stage1_transport import MAX_OCI_STAGE1_TRANSPORT_BYTES, MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES

OCI_GUEST_STAGE1_CONTRACT = "palimpsest.guest-stage1-consumer.x86_64.v11"
OCI_GUEST_STAGE1_CAPABILITY = "authenticated-overlay-switch-root-pid1-supervisor"
OCI_GUEST_STAGE1_PLAN_TRANSPORT = "virtio-blk-raw-envelope-4k.v1"
MAX_GUEST_KERNEL_CMDLINE_BYTES = 4096
MAX_GUEST_SYSFS_SERIAL_BYTES = 64

_MAGIC = b"PALIMPSEST-S1\0\0\0"
_HEADER = struct.Struct("<16sIIQ32s")
_VERSION = 1
_ALIGNMENT = 4096
_SERIAL_RE = re.compile(r"^[0-9a-f]{20}$")
_BLOCK_NAME_RE = re.compile(r"^vd[a-z]$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PALIMPSEST_FIELDS = {
    "palimpsest.core",
    "palimpsest.lowers",
    "palimpsest.resource",
    "palimpsest.root",
    "palimpsest.stage1",
    "palimpsest.stage1dev",
}


def _aligned(size: int) -> int:
    return (size + _ALIGNMENT - 1) // _ALIGNMENT * _ALIGNMENT


def _transport_serial(transport_digest: str) -> str:
    identity = f"palimpsest-oci-root-stage1-transport-v1\0{transport_digest}".encode()
    return hashlib.sha256(identity).hexdigest()[:20]


def _canonical_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ArtifactValidationError(f"{field_name} is invalid")
    try:
        normalized = normalize_digest(value)
    except (ArtifactValidationError, TypeError, ValueError):
        raise ArtifactValidationError(f"{field_name} is invalid") from None
    if normalized != value:
        raise ArtifactValidationError(f"{field_name} is not canonical")
    return normalized


@dataclass(frozen=True, slots=True)
class GuestStage1KernelBindings:
    resource_digest: str
    domain_core_digest: str
    transport_digest: str
    transport_serial: str
    root_serial: str
    lower_serials: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_digest", _canonical_digest(self.resource_digest, "guest resource digest"))
        object.__setattr__(
            self,
            "domain_core_digest",
            _canonical_digest(self.domain_core_digest, "guest domain-core digest"),
        )
        object.__setattr__(self, "transport_digest", _canonical_digest(self.transport_digest, "guest transport digest"))
        if (
            not isinstance(self.transport_serial, str)
            or _SERIAL_RE.fullmatch(self.transport_serial) is None
            or not isinstance(self.root_serial, str)
            or _SERIAL_RE.fullmatch(self.root_serial) is None
            or not isinstance(self.lower_serials, tuple)
            or not 1 <= len(self.lower_serials) <= 24
            or any(not isinstance(value, str) or _SERIAL_RE.fullmatch(value) is None for value in self.lower_serials)
        ):
            raise ArtifactValidationError("guest disk serial binding is invalid")
        serials = (self.transport_serial, self.root_serial, *self.lower_serials)
        if len(serials) != len(set(serials)):
            raise ArtifactValidationError("guest disk serial binding is ambiguous")
        if self.transport_serial != _transport_serial(self.transport_digest):
            raise ArtifactValidationError("guest transport serial does not match its digest")


def parse_guest_kernel_cmdline(cmdline: bytes | str) -> GuestStage1KernelBindings:
    """Parse the closed Palimpsest subset of ``/proc/cmdline``.

    Unrelated kernel arguments remain the kernel's concern.  Every
    ``palimpsest.*`` argument is closed-world, unique and required so an old or
    misspelled producer cannot silently weaken guest validation.
    """

    if isinstance(cmdline, bytes):
        if not 1 <= len(cmdline) <= MAX_GUEST_KERNEL_CMDLINE_BYTES or b"\0" in cmdline:
            raise ArtifactValidationError("guest kernel command line is invalid")
        try:
            decoded = cmdline.decode("ascii")
        except UnicodeDecodeError:
            raise ArtifactValidationError("guest kernel command line is invalid") from None
    elif isinstance(cmdline, str):
        try:
            encoded = cmdline.encode("ascii")
        except UnicodeEncodeError:
            raise ArtifactValidationError("guest kernel command line is invalid") from None
        if not 1 <= len(encoded) <= MAX_GUEST_KERNEL_CMDLINE_BYTES or "\0" in cmdline:
            raise ArtifactValidationError("guest kernel command line is invalid")
        decoded = cmdline
    else:
        raise ArtifactValidationError("guest kernel command line is invalid")

    values: dict[str, str] = {}
    for token in decoded.split():
        if not token.startswith("palimpsest."):
            continue
        if "=" not in token:
            raise ArtifactValidationError("guest kernel command line field is invalid")
        key, value = token.split("=", 1)
        if key not in _PALIMPSEST_FIELDS or key in values or not value:
            raise ArtifactValidationError("guest kernel command line field is invalid")
        values[key] = value
    if set(values) != _PALIMPSEST_FIELDS:
        raise ArtifactValidationError("guest kernel command line binding is incomplete")
    prefix = "virtio-"
    device = values["palimpsest.stage1dev"]
    root = values["palimpsest.root"]
    lowers = values["palimpsest.lowers"].split(",")
    if (
        not device.startswith(prefix)
        or len(device) != len(prefix) + 20
        or not root.startswith(prefix)
        or len(root) != len(prefix) + 20
        or not 1 <= len(lowers) <= 24
        or any(not item.startswith(prefix) or len(item) != len(prefix) + 20 for item in lowers)
    ):
        raise ArtifactValidationError("guest disk device binding is invalid")
    return GuestStage1KernelBindings(
        values["palimpsest.resource"],
        values["palimpsest.core"],
        values["palimpsest.stage1"],
        device[len(prefix) :],
        root[len(prefix) :],
        tuple(item[len(prefix) :] for item in lowers),
    )


@dataclass(frozen=True, slots=True)
class GuestBlockCandidate:
    name: str
    serial: str
    read_only: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _BLOCK_NAME_RE.fullmatch(self.name) is None:
            raise ArtifactValidationError("guest block-device name is invalid")
        if not isinstance(self.serial, str) or _SERIAL_RE.fullmatch(self.serial) is None:
            raise ArtifactValidationError("guest block-device serial is invalid")
        if self.read_only is not None and type(self.read_only) is not bool:
            raise ArtifactValidationError("guest block-device read-only state is invalid")


@dataclass(frozen=True, slots=True)
class GuestExpectedBlockDevice:
    role: str
    ordinal: int | None
    serial: str
    read_only: bool
    size_bytes: int
    image_digest: str | None = None
    occurrence_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            self.role not in {"root", "lower"}
            or (self.role == "root" and self.ordinal is not None)
            or (self.role == "lower" and (type(self.ordinal) is not int or self.ordinal < 0))
            or not isinstance(self.serial, str)
            or _SERIAL_RE.fullmatch(self.serial) is None
            or type(self.read_only) is not bool
            or self.read_only != (self.role == "lower")
            or type(self.size_bytes) is not int
            or self.size_bytes <= 0
            or (
                self.role == "root"
                and (not 16 * 1024 * 1024 <= self.size_bytes <= 16 * 1024**4 or self.size_bytes % (1024 * 1024))
            )
            or (
                self.role == "lower"
                and (
                    not SQUASHFS_BLOCK_DEVICE_ALIGNMENT <= self.size_bytes <= 32 * 1024**3
                    or self.size_bytes % SQUASHFS_BLOCK_DEVICE_ALIGNMENT
                )
            )
        ):
            raise ArtifactValidationError("guest expected block-device contract is invalid")
        if self.role == "root":
            if self.image_digest is not None or self.occurrence_digest is not None:
                raise ArtifactValidationError("guest root block-device provenance is invalid")
        else:
            object.__setattr__(self, "image_digest", _canonical_digest(self.image_digest, "guest lower image digest"))
            object.__setattr__(
                self,
                "occurrence_digest",
                _canonical_digest(self.occurrence_digest, "guest lower occurrence digest"),
            )


@dataclass(frozen=True, slots=True)
class GuestSelectedBlockDevice:
    expected: GuestExpectedBlockDevice
    path: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.expected, GuestExpectedBlockDevice)
            or not isinstance(self.path, Path)
            or not self.path.is_absolute()
        ):
            raise ArtifactValidationError("guest selected block-device contract is invalid")


def expected_pre_mount_block_devices(plan: OCIStage1Plan) -> tuple[GuestExpectedBlockDevice, ...]:
    """Project the authenticated plan into ordered root/lower block roles."""

    if not isinstance(plan, OCIStage1Plan):
        raise ArtifactValidationError("guest pre-mount plan is invalid")
    return (
        GuestExpectedBlockDevice("root", None, plan.root["serial"], False, plan.root["size_bytes"]),
        *tuple(
            GuestExpectedBlockDevice(
                "lower",
                layer["ordinal"],
                layer["serial"],
                True,
                layer["size_bytes"],
                layer["image_digest"],
                layer["occurrence_digest"],
            )
            for layer in plan.layers
        ),
    )


def select_pre_mount_block_devices(
    candidates: Sequence[GuestBlockCandidate],
    expected: Sequence[GuestExpectedBlockDevice],
    *,
    dev_root: Path = Path("/dev"),
) -> tuple[GuestSelectedBlockDevice, ...]:
    """Bind every ordered role to one serial and exact sysfs ``ro`` state.

    The portable contract intentionally cannot prove block-node major/minor or
    ``BLKROGET``/``BLKGETSIZE64``.  The freestanding Linux PID 1 keeps those
    descriptors open and performs the ioctl checks before emitting readiness.
    """

    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes))
        or any(not isinstance(item, GuestBlockCandidate) for item in candidates)
        or not isinstance(expected, Sequence)
        or isinstance(expected, (str, bytes))
        or not expected
        or any(not isinstance(item, GuestExpectedBlockDevice) for item in expected)
        or not isinstance(dev_root, Path)
        or not dev_root.is_absolute()
    ):
        raise ArtifactValidationError("guest pre-mount discovery input is invalid")
    if (
        expected[0].role != "root"
        or expected[0].ordinal is not None
        or any(item.role != "lower" or item.ordinal != ordinal for ordinal, item in enumerate(expected[1:]))
    ):
        raise ArtifactValidationError("guest pre-mount discovery role order is invalid")
    names = tuple(item.name for item in candidates)
    serials = tuple(item.serial for item in candidates)
    expected_serials = tuple(item.serial for item in expected)
    if (
        len(candidates) != len(expected)
        or len(names) != len(set(names))
        or len(serials) != len(set(serials))
        or len(expected_serials) != len(set(expected_serials))
    ):
        raise ArtifactValidationError("guest pre-mount discovery is ambiguous")
    by_serial = {item.serial: item for item in candidates}
    selected: list[GuestSelectedBlockDevice] = []
    for contract in expected:
        candidate = by_serial.get(contract.serial)
        if candidate is None or candidate.read_only is not contract.read_only:
            raise ArtifactValidationError("guest pre-mount block device is missing or has the wrong role")
        selected.append(GuestSelectedBlockDevice(contract, dev_root / candidate.name))
    return tuple(selected)


def select_stage1_block_device(
    candidates: Sequence[GuestBlockCandidate],
    *,
    expected_serial: str,
    dev_root: Path = Path("/dev"),
) -> Path:
    """Select exactly one allow-listed block name by its exact virtio serial."""

    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes))
        or any(not isinstance(item, GuestBlockCandidate) for item in candidates)
        or not isinstance(expected_serial, str)
        or _SERIAL_RE.fullmatch(expected_serial) is None
        or not isinstance(dev_root, Path)
        or not dev_root.is_absolute()
    ):
        raise ArtifactValidationError("guest block-device discovery input is invalid")
    names = [item.name for item in candidates]
    if len(names) != len(set(names)):
        raise ArtifactValidationError("guest block-device discovery is ambiguous")
    matches = [item for item in candidates if item.serial == expected_serial]
    if len(matches) != 1:
        raise ArtifactValidationError("guest transport block device is missing or ambiguous")
    return dev_root / matches[0].name


def _read_bounded_sysfs_attribute(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ArtifactValidationError("guest sysfs attribute cannot be opened safely") from None
    try:
        opened = os.fstat(descriptor)
        # sysfs attributes normally report PAGE_SIZE rather than their emitted
        # payload length in st_size.  Bound the descriptor read itself instead.
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ArtifactValidationError("guest sysfs attribute metadata is invalid")
        payload = os.read(descriptor, limit + 1)
        if len(payload) > limit or os.read(descriptor, 1):
            raise ArtifactValidationError("guest sysfs attribute is too large")
        closed = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (closed.st_dev, closed.st_ino, closed.st_size):
            raise ArtifactValidationError("guest sysfs attribute changed during discovery")
        return payload
    except OSError:
        raise ArtifactValidationError("guest sysfs attribute cannot be read safely") from None
    finally:
        os.close(descriptor)


def discover_stage1_block_device(
    expected_serial: str,
    *,
    sys_block: Path = Path("/sys/class/block"),
    sys_devices: Path = Path("/sys/devices"),
    dev_root: Path = Path("/dev"),
) -> Path:
    """Discover one read-only ``vd*`` candidate through canonical sysfs links.

    This portable boundary validates the libvirt virtio target-name convention,
    serial and sysfs read-only bit. Driver-chain and opened block-node ioctl
    identity are enforced by the freestanding Linux consumer and its KVM gate.
    """

    if (
        not isinstance(sys_block, Path)
        or not sys_block.is_absolute()
        or not isinstance(sys_devices, Path)
        or not sys_devices.is_absolute()
    ):
        raise ArtifactValidationError("guest sysfs block root is invalid")
    try:
        devices_root = sys_devices.resolve(strict=True)
        if not devices_root.is_dir():
            raise ArtifactValidationError("guest sysfs devices root is invalid")
        entries = tuple(os.scandir(sys_block))
    except OSError:
        raise ArtifactValidationError("guest sysfs block devices cannot be enumerated") from None
    candidates_by_name: dict[str, tuple[Path, bytes, bytes]] = {}
    for entry in entries:
        if _BLOCK_NAME_RE.fullmatch(entry.name) is None:
            continue
        try:
            if not entry.is_symlink():
                raise ArtifactValidationError("guest sysfs block candidate is not a canonical link")
            resolved = Path(entry.path).resolve(strict=True)
            resolved.relative_to(devices_root)
            if not resolved.is_dir():
                raise ArtifactValidationError("guest sysfs block candidate target is invalid")
        except (OSError, ValueError):
            raise ArtifactValidationError("guest sysfs block candidate cannot be inspected") from None
        try:
            raw = _read_bounded_sysfs_attribute(resolved / "serial", MAX_GUEST_SYSFS_SERIAL_BYTES)
            read_only = _read_bounded_sysfs_attribute(resolved / "ro", 2)
            serial_bytes = raw[:-1] if raw.endswith(b"\n") else raw
            serial = serial_bytes.decode("ascii")
            if read_only != b"1\n":
                continue
            GuestBlockCandidate(entry.name, serial)
        except (ArtifactValidationError, UnicodeDecodeError):
            continue
        candidates_by_name[entry.name] = (resolved, raw, read_only)
    selected = select_stage1_block_device(
        tuple(
            GuestBlockCandidate(name, (raw[:-1] if raw.endswith(b"\n") else raw).decode("ascii"))
            for name, (_, raw, _) in sorted(candidates_by_name.items())
        ),
        expected_serial=expected_serial,
        dev_root=dev_root,
    )
    resolved, serial_before, read_only_before = candidates_by_name[selected.name]
    serial_after = _read_bounded_sysfs_attribute(resolved / "serial", MAX_GUEST_SYSFS_SERIAL_BYTES)
    read_only_after = _read_bounded_sysfs_attribute(resolved / "ro", 2)
    if serial_after != serial_before or read_only_after != read_only_before or read_only_after != b"1\n":
        raise ArtifactValidationError("guest sysfs block candidate changed during discovery")
    return selected


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError("guest stage-1 JSON contains duplicate keys")
        result[key] = value
    return result


def _semantic_stage1_plan(value: Any) -> OCIStage1Plan:
    if not isinstance(value, Mapping) or set(value) != {
        "assembly",
        "boot_plan_digest",
        "domain_core_digest",
        "handoff",
        "phase",
        "process",
        "process_policy",
        "protocol",
        "run",
        "schema",
    }:
        raise ArtifactValidationError("guest stage-1 plan fields are invalid")
    assembly = value.get("assembly")
    run = value.get("run")
    if (
        value.get("schema") != OCI_STAGE1_PLAN_SCHEMA
        or value.get("protocol") != OCI_STAGE1_PROTOCOL
        or value.get("phase") != "stage1-contract"
        or value.get("handoff") != OCI_STAGE1_HANDOFF
        or value.get("process_policy") != OCI_STAGE1_PROCESS_POLICY
        or not isinstance(run, Mapping)
        or set(run) != {"name", "run_id"}
        or not isinstance(assembly, Mapping)
        or set(assembly)
        != {
            "device_policy",
            "layers",
            "lowerdir_ordinals",
            "overlay_mount_options",
            "probes",
            "root",
            "root_layout",
        }
        or assembly.get("device_policy") != OCI_STAGE1_DEVICE_POLICY
        or assembly.get("overlay_mount_options") != ["rw", "nodev", "nosuid"]
        or assembly.get("root_layout") != OCI_STAGE1_ROOT_LAYOUT
        or not isinstance(assembly.get("probes"), list)
        or not isinstance(assembly.get("layers"), list)
        or assembly.get("lowerdir_ordinals") != list(reversed(range(len(assembly["layers"]))))
    ):
        raise ArtifactValidationError("guest stage-1 plan policy is invalid")
    try:
        process = OCIProcessSpec.from_dict(value["process"])
        plan = OCIStage1Plan(
            run_id=run["run_id"],
            run_name=run["name"],
            boot_plan_digest=value["boot_plan_digest"],
            domain_core_digest=value["domain_core_digest"],
            root=assembly["root"],
            layers=tuple(assembly["layers"]),
            process=process,
            assembly_probes=tuple(assembly["probes"]),
        )
    except (ArtifactValidationError, KeyError, TypeError, ValueError):
        raise ArtifactValidationError("guest stage-1 plan semantics are invalid") from None
    except StateError:
        raise ArtifactValidationError("guest stage-1 plan semantics are invalid") from None
    if plan.to_dict() != dict(value):
        raise ArtifactValidationError("guest stage-1 plan is not canonical")
    return plan


@dataclass(frozen=True, slots=True)
class VerifiedGuestStage1Plan:
    bindings: GuestStage1KernelBindings
    plan: OCIStage1Plan
    artifact_size_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bindings, GuestStage1KernelBindings)
            or not isinstance(self.plan, OCIStage1Plan)
            or type(self.artifact_size_bytes) is not int
            or not _HEADER.size < self.artifact_size_bytes <= MAX_OCI_STAGE1_TRANSPORT_BYTES
        ):
            raise ArtifactValidationError("verified guest stage-1 result is invalid")


def verify_guest_stage1_transport(
    artifact: bytes,
    bindings: GuestStage1KernelBindings,
) -> VerifiedGuestStage1Plan:
    """Verify envelope, artifact digest, canonical JSON and cmdline cross-binding."""

    if (
        not isinstance(artifact, bytes)
        or not _HEADER.size < len(artifact) <= MAX_OCI_STAGE1_TRANSPORT_BYTES
        or len(artifact) % _ALIGNMENT
        or not isinstance(bindings, GuestStage1KernelBindings)
    ):
        raise ArtifactValidationError("guest stage-1 transport bytes are invalid")
    if f"sha256:{hashlib.sha256(artifact).hexdigest()}" != bindings.transport_digest:
        raise ArtifactValidationError("guest stage-1 transport digest does not match the kernel command line")
    try:
        magic, version, header_size, payload_size, payload_hash = _HEADER.unpack_from(artifact)
    except struct.error:
        raise ArtifactValidationError("guest stage-1 transport header is invalid") from None
    if (
        magic != _MAGIC
        or version != _VERSION
        or header_size != _HEADER.size
        or not 1 <= payload_size <= MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES
        or _aligned(header_size + payload_size) != len(artifact)
    ):
        raise ArtifactValidationError("guest stage-1 transport header policy is invalid")
    payload = artifact[header_size : header_size + payload_size]
    padding = artifact[header_size + payload_size :]
    if hashlib.sha256(payload).digest() != payload_hash or any(padding):
        raise ArtifactValidationError("guest stage-1 transport payload binding is invalid")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        canonical = canonical_json_bytes(value)
    except (ArtifactValidationError, UnicodeDecodeError, RecursionError, ValueError):
        raise ArtifactValidationError("guest stage-1 transport JSON is invalid") from None
    if canonical != payload:
        raise ArtifactValidationError("guest stage-1 transport JSON is not canonical")
    plan = _semantic_stage1_plan(value)
    if (
        plan.boot_plan_digest != bindings.resource_digest
        or plan.domain_core_digest != bindings.domain_core_digest
        or plan.root["serial"] != bindings.root_serial
        or tuple(layer["serial"] for layer in plan.layers) != bindings.lower_serials
        or plan.digest != f"sha256:{hashlib.sha256(payload).hexdigest()}"
    ):
        raise ArtifactValidationError("guest stage-1 plan does not match the kernel command line")
    return VerifiedGuestStage1Plan(bindings, plan, len(artifact))


__all__ = [
    "MAX_GUEST_KERNEL_CMDLINE_BYTES",
    "GuestBlockCandidate",
    "GuestExpectedBlockDevice",
    "GuestSelectedBlockDevice",
    "GuestStage1KernelBindings",
    "OCI_GUEST_STAGE1_CAPABILITY",
    "OCI_GUEST_STAGE1_CONTRACT",
    "OCI_GUEST_STAGE1_PLAN_TRANSPORT",
    "VerifiedGuestStage1Plan",
    "discover_stage1_block_device",
    "expected_pre_mount_block_devices",
    "parse_guest_kernel_cmdline",
    "select_stage1_block_device",
    "select_pre_mount_block_devices",
    "verify_guest_stage1_transport",
]
