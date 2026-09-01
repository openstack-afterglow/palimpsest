"""Qualified native-KVM proof for the fail-closed OCI stage-1 consumer.

This private module is test support for the packaged bootstrap boundary.  It
does not define or start a production VM and deliberately stops before any
root filesystem mount, pivot, or workload execution.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError, StateError
from .oci_initramfs import OCI_BOOTSTRAP_STAGE1_CONTRACT, build_bootstrap_initramfs
from .oci_process import OCIProcessSpec, OCIUserSpec
from .oci_provenance import canonical_json_bytes
from .oci_stage1 import OCIStage1Plan, oci_stage1_device_serial
from .oci_stage1_transport import BuiltOCIStage1Transport, OCIStage1TransportReceipt, build_stage1_transport

OCI_STAGE1_KVM_PROOF_SCHEMA = "palimpsest.oci-stage1-kvm-proof.v3"
KVM_GET_API_VERSION = 0xAE00
REQUIRED_KVM_API_VERSION = 12
MAX_KERNEL_BYTES = 128 * 1024 * 1024
MAX_KERNEL_CONFIG_BYTES = 4 * 1024 * 1024
MAX_QEMU_VERSION_BYTES = 16 * 1024
MAX_CONSOLE_BYTES = 4 * 1024 * 1024
DEFAULT_BOOT_TIMEOUT_SECONDS = 45.0
# Do not include the line ending: a real 8250 console commonly maps LF to
# CRLF, while the pipe-based pure test preserves LF exactly.
SUCCESS_MARKER = b"palimpsest guest stage1: pre-mount device set verified; root assembly disabled; waiting fail-closed"
REJECTION_MARKER = b"palimpsest guest stage1: pre-mount contract rejected; waiting fail-closed"
PREPARATION_FAILURE_MARKER = b"palimpsest guest stage1: bootstrap preparation failed; waiting fail-closed"

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
EVIDENCE_FILE_NAMES = ("console.bin", "receipt.json") + tuple(f"negative-{name}.bin" for name in NEGATIVE_CONTROL_NAMES)
_REQUIRED_KERNEL_CONFIG = (
    "CONFIG_64BIT",
    "CONFIG_BINFMT_ELF",
    "CONFIG_BLK_DEV_INITRD",
    "CONFIG_DEVTMPFS",
    "CONFIG_PCI",
    "CONFIG_PROC_FS",
    "CONFIG_SERIAL_8250",
    "CONFIG_SERIAL_8250_CONSOLE",
    "CONFIG_SYSFS",
    "CONFIG_VIRTIO",
    "CONFIG_VIRTIO_BLK",
    "CONFIG_VIRTIO_PCI",
)


class KVMProofUnavailable(StateError):
    """The host cannot make the narrowly qualified native-KVM claim."""


class KVMProofFailure(StateError):
    """A qualified proof attempt failed closed."""


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


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

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or not isinstance(self.payload, bytes)
            or self.size_bytes != len(self.payload)
            or self.digest != _digest(self.payload)
        ):
            raise ArtifactValidationError("verified host file is invalid")


def _read_pinned_regular_file(path: Path, *, maximum: int, label: str) -> VerifiedHostFile:
    if not isinstance(path, Path) or not path.is_absolute() or "\0" in os.fspath(path):
        raise ArtifactValidationError(f"{label} path is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ArtifactValidationError(f"{label} cannot be opened safely") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
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
        payload = b"".join(chunks)
        return VerifiedHostFile(path, payload, _digest(payload), len(payload))
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

    return OCIStage1Plan(
        run_id="f6f546e2-e734-4920-9eff-1762b348a249",
        run_name="kvm-stage1-proof",
        boot_plan_digest="sha256:" + "a" * 64,
        domain_core_digest="sha256:" + "b" * 64,
        root={
            "filesystem": "ext4",
            "generation": 1,
            "mount_options": ["rw", "nodev", "nosuid"],
            "serial": oci_stage1_device_serial("root", "1fd7a60e-fdb2-4877-91d3-148bbca3884f"),
            "size_bytes": 16 * 1024 * 1024,
            "volume_id": "1fd7a60e-fdb2-4877-91d3-148bbca3884f",
        },
        layers=(
            {
                "filesystem": "squashfs",
                "image_digest": "sha256:" + "2" * 64,
                "mount_options": ["ro", "nodev", "nosuid"],
                "occurrence_digest": "sha256:" + "3" * 64,
                "ordinal": 0,
                "serial": oci_stage1_device_serial("lower", "sha256:" + "3" * 64),
                "size_bytes": 4096,
            },
            {
                "filesystem": "squashfs",
                "image_digest": "sha256:" + "4" * 64,
                "mount_options": ["ro", "nodev", "nosuid"],
                "occurrence_digest": "sha256:" + "5" * 64,
                "ordinal": 1,
                "serial": oci_stage1_device_serial("lower", "sha256:" + "5" * 64),
                "size_bytes": 8192,
            },
        ),
        process=OCIProcessSpec(("/sbin/init",), (("LANG", "C.UTF-8"),), "/", OCIUserSpec("0", "0"), 15),
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
    return command


def pre_mount_topology(plan: OCIStage1Plan) -> dict[str, Any]:
    if not isinstance(plan, OCIStage1Plan) or plan.to_dict() != build_proof_plan().to_dict():
        raise ArtifactValidationError("KVM proof topology plan is invalid")

    devices = [
        {
            "artifact_digest": _zero_digest(plan.root["size_bytes"]),
            "read_only": False,
            "role": "root",
            "serial": plan.root["serial"],
            "size_bytes": plan.root["size_bytes"],
        }
    ]
    devices.extend(
        {
            "artifact_digest": _zero_digest(layer["size_bytes"]),
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
    topology = {"devices": devices, "policy": "virtio-blk-pre-mount-device-set.v1"}
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
            "artifact_digest": _zero_digest(selected_plan.layers[0]["size_bytes"]),
            "mode": 0o400,
            "size_bytes": selected_plan.layers[0]["size_bytes"],
        },
        "lower1": {
            "artifact_digest": _zero_digest(selected_plan.layers[1]["size_bytes"]),
            "mode": 0o400,
            "size_bytes": selected_plan.layers[1]["size_bytes"],
        },
        "root": {
            "artifact_digest": _zero_digest(selected_plan.root["size_bytes"]),
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
        backing_specs["root"] = {"artifact_digest": _zero_digest(size), "mode": 0o600, "size_bytes": size}
    elif name == "missing_lower":
        attachments.remove(by_role[("lower", 0)])
    elif name == "wrong_lower_serial":
        by_role[("lower", 0)]["serial"] = "e" * 20
    elif name == "writable_lower":
        by_role[("lower", 0)]["read_only"] = False
        backing_specs["lower0"]["mode"] = 0o600
    elif name in {"lower_size_smaller", "lower_size_larger"}:
        size = selected_plan.layers[0]["size_bytes"] + (-512 if name.endswith("smaller") else 512)
        backing_specs["lower0"] = {"artifact_digest": _zero_digest(size), "mode": 0o400, "size_bytes": size}
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
    return command


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
    console: bytes
    negative_consoles: Mapping[str, bytes]

    def __post_init__(self) -> None:
        for value, field in (
            (self.kernel_digest, "kernel digest"),
            (self.kernel_config_digest, "kernel config digest"),
            (self.initramfs_digest, "initramfs digest"),
            (self.initramfs_manifest_digest, "initramfs manifest digest"),
            (self.stage1_elf_digest, "stage-1 ELF digest"),
            (self.qemu_digest, "QEMU digest"),
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
            or _logical_line_count(self.console, REJECTION_MARKER) != 0
            or _logical_line_count(self.console, PREPARATION_FAILURE_MARKER) != 0
            or not isinstance(self.negative_consoles, Mapping)
            or set(self.negative_consoles) != set(NEGATIVE_CONTROL_NAMES)
            or any(
                not isinstance(control_console, bytes)
                or not 1 <= len(control_console) <= MAX_CONSOLE_BYTES
                or _logical_line_count(control_console, REJECTION_MARKER) != 1
                or _logical_line_count(control_console, SUCCESS_MARKER) != 0
                or _logical_line_count(control_console, PREPARATION_FAILURE_MARKER) != 0
                for control_console in self.negative_consoles.values()
            )
        ):
            raise ArtifactValidationError("KVM proof receipt value is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmdline": {"digest": _digest(self.cmdline.encode("ascii")), "text": self.cmdline},
            "console": {
                "digest": _digest(self.console),
                "size_bytes": len(self.console),
                "success_marker": SUCCESS_MARKER.decode("ascii").rstrip("\n"),
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
            "negative_controls": {
                name: {
                    "contract": negative_control_contract(name),
                    "console_digest": _digest(self.negative_consoles[name]),
                    "console_size_bytes": len(self.negative_consoles[name]),
                    "pid1_alive_after_marker": True,
                    "preparation_failure_marker_count": 0,
                    "rejection_marker": REJECTION_MARKER.decode("ascii").rstrip("\n"),
                    "rejection_marker_count": 1,
                    "success_marker_count": 0,
                }
                for name in NEGATIVE_CONTROL_NAMES
            },
            "pre_mount_devices": True,
            "filesystem_verified": False,
            "content_verified": False,
            "mount_attempted": False,
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
            "root_assembly": False,
            "schema": OCI_STAGE1_KVM_PROOF_SCHEMA,
            "stage1": {"contract": OCI_BOOTSTRAP_STAGE1_CONTRACT, "elf_digest": self.stage1_elf_digest},
            "topology": pre_mount_topology(build_proof_plan()),
            "transport": {**dict(self.transport), "serial": self.transport_serial},
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
    ) -> OCIStage1KVMProofReceipt:
        if not isinstance(value, Mapping) or set(value) != {
            "cmdline",
            "console",
            "initramfs",
            "kernel",
            "negative_controls",
            "pre_mount_devices",
            "filesystem_verified",
            "content_verified",
            "mount_attempted",
            "qualification",
            "qemu",
            "root_assembly",
            "schema",
            "stage1",
            "topology",
            "transport",
        }:
            raise ArtifactValidationError("KVM proof receipt fields are invalid")
        cmdline = value.get("cmdline")
        console_value = value.get("console")
        initramfs = value.get("initramfs")
        kernel = value.get("kernel")
        qemu = value.get("qemu")
        stage1 = value.get("stage1")
        transport = value.get("transport")
        qualification = value.get("qualification")
        negative_controls = value.get("negative_controls")
        if (
            value.get("schema") != OCI_STAGE1_KVM_PROOF_SCHEMA
            or value.get("root_assembly") is not False
            or value.get("pre_mount_devices") is not True
            or value.get("filesystem_verified") is not False
            or value.get("content_verified") is not False
            or value.get("mount_attempted") is not False
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
                    "success_marker_count",
                }
                or negative_controls[name].get("contract") != negative_control_contract(name)
                for name in NEGATIVE_CONTROL_NAMES
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
            console,
            negative_consoles,
        )
        if receipt.to_dict() != dict(value):
            raise ArtifactValidationError("KVM proof receipt is not canonical")
        return receipt


@dataclass(frozen=True, slots=True)
class OCIStage1KVMProofResult:
    receipt: OCIStage1KVMProofReceipt
    console: bytes
    negative_consoles: Mapping[str, bytes]
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


def _read_console_until(
    command: tuple[str, ...],
    *,
    expected: bytes,
    forbidden: tuple[bytes, ...],
    timeout_seconds: float,
    require_alive_after_marker: bool,
) -> bytes:
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
    selector.register(descriptor, selectors.EVENT_READ)
    console = bytearray()
    deadline = time.monotonic() + timeout_seconds
    marker_seen_at: float | None = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise KVMProofFailure("QEMU console proof timed out")
            events = selector.select(min(0.1, deadline - now))
            for _key, _mask in events:
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    continue
                if chunk:
                    console.extend(chunk)
                    if len(console) > MAX_CONSOLE_BYTES:
                        raise KVMProofFailure("QEMU console exceeds proof bound")
            current = bytes(console)
            if any(_logical_line_count(current, marker) for marker in forbidden):
                raise KVMProofFailure("QEMU emitted a forbidden stage-1 marker")
            count = _logical_line_count(current, expected)
            if count > 1:
                raise KVMProofFailure("QEMU emitted the proof marker more than once")
            if count == 1 and marker_seen_at is None:
                if process.poll() is not None:
                    raise KVMProofFailure("QEMU exited at the proof marker")
                marker_seen_at = now
            if marker_seen_at is not None and (not require_alive_after_marker or now - marker_seen_at >= 0.25):
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
    finally:
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
                    payload = b"\0" * backing["size_bytes"]
                    if _digest(payload) != backing["artifact_digest"]:
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
    root_contract = topology[0]
    root_path = _secure_write(directory, "root.raw", b"\0" * root_contract["size_bytes"], mode=0o600)
    lower_paths = tuple(
        _secure_write(directory, f"lower-{layer['ordinal']}.raw", b"\0" * layer["size_bytes"], mode=0o400)
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
        _verify_pinned_boot_files(
            qemu_path=qemu_path,
            qemu=qemu,
            kernel_path=kernel_path,
            kernel=kernel,
            initramfs_path=initramfs_path,
            initramfs_digest=initramfs.manifest.artifact_digest,
        )
        _verify_file_digest(transport_path, transport.receipt.artifact_digest, 0o400)

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
        )
        console = _read_console_until(
            command,
            expected=SUCCESS_MARKER,
            forbidden=(REJECTION_MARKER, PREPARATION_FAILURE_MARKER),
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
        _verify_file_digest(transport_path, transport.receipt.artifact_digest, 0o400)
        _verify_pre_mount_topology(root_path, lower_paths, plan)
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
                forbidden=(SUCCESS_MARKER, PREPARATION_FAILURE_MARKER),
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
        console,
        negative_consoles,
    )
    if evidence is not None:
        _secure_write(evidence, "console.bin", console, mode=0o400)
        for control_name in NEGATIVE_CONTROL_NAMES:
            _secure_write(
                evidence,
                f"negative-{control_name}.bin",
                negative_consoles[control_name],
                mode=0o400,
            )
        _secure_write(evidence, "receipt.json", receipt.canonical_bytes, mode=0o400)
    return OCIStage1KVMProofResult(receipt, console, negative_consoles, evidence)
