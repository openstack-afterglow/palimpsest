"""Host capability detection, backend selection, and domain profiles.

This is the single place that knows which local virtualization backend a
given host can run, and what libvirt domain shape that backend needs. Every
other module (``kvm``, ``runtime``, ``cli``, ``project_adapter``, ``build``)
is expected to route arch/host decisions through here instead of re-deriving
``platform.system()``/``platform.machine()`` checks locally.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import lima
from .errors import ArtifactValidationError, LifecycleError

BACKEND_KVM = "kvm"
BACKEND_LIMA = "lima-vz"
BACKEND_HVF = "libvirt-hvf"
BACKENDS = (BACKEND_KVM, BACKEND_LIMA, BACKEND_HVF)

_QEMU_SYSTEM_X86_64 = Path("/usr/bin/qemu-system-x86_64")
_QEMU_SYSTEM_AARCH64 = Path("/usr/bin/qemu-system-aarch64")


@dataclass(frozen=True)
class HostPlatform:
    """The host operating system and normalized CPU architecture."""

    system: str  # platform.system(): "Darwin" | "Linux" | other
    machine: str  # normalized: "x86_64" | "aarch64" | raw value


@dataclass(frozen=True)
class Firmware:
    """UEFI firmware files required to boot an aarch64 guest under HVF."""

    loader: Path  # read-only pflash code image
    nvram_template: Path  # per-run nvram is copied from this


@dataclass(frozen=True)
class DomainProfile:
    """Everything backend-specific needed to build and boot a libvirt domain."""

    backend: str  # BACKEND_KVM | BACKEND_HVF
    domain_type: str  # "kvm" | "hvf"
    arch: str  # "x86_64" | "aarch64"
    machine: str  # "q35" | "virt"
    emulator: Path
    uri: str  # "qemu:///system" | "qemu:///session"
    firmware: Firmware | None  # None with autoselect_firmware or BIOS boot
    autoselect_firmware: bool  # emit <os firmware='efi'>
    network_mode: str  # "libvirt-network" | "user-hostfwd"
    seed_tool: str  # "cloud-localds" | "hdiutil"
    seed_bus: str  # "sata" | "scsi"


def normalize_machine(value: str) -> str:
    """Collapse platform-specific machine spellings to a canonical name."""

    lowered = value.strip().lower()
    if lowered in {"arm64", "aarch64"}:
        return "aarch64"
    if lowered in {"x86_64", "amd64"}:
        return "x86_64"
    return value


def detect_host(*, system: str | None = None, machine: str | None = None) -> HostPlatform:
    """Report the current host's OS and normalized CPU architecture."""

    resolved_system = platform.system() if system is None else system
    resolved_machine = normalize_machine(platform.machine() if machine is None else machine)
    return HostPlatform(system=resolved_system, machine=resolved_machine)


def select_backend(image_arch: str, *, host: HostPlatform | None = None, requested: str = "auto") -> str:
    """Choose a runtime backend for booting ``image_arch``, failing closed.

    Raises ``ArtifactValidationError`` with an actionable message whenever no
    backend can honor ``requested`` on ``host`` (or, for ``requested="auto"``,
    whenever no backend can run at all).
    """

    host = host or detect_host()
    arch = normalize_machine(image_arch)

    if requested == BACKEND_LIMA:
        if not (host.system == "Darwin" and host.machine == "aarch64"):
            raise ArtifactValidationError("the lima-vz backend requires macOS on Apple Silicon")
        return BACKEND_LIMA

    if requested == BACKEND_HVF:
        if host.system != "Darwin":
            raise ArtifactValidationError("the libvirt-hvf backend requires macOS")
        if arch != host.machine:
            raise ArtifactValidationError(
                f"the local libvirt runtime does not emulate foreign architectures: {arch} image on {host.machine} host"
            )
        return BACKEND_HVF

    if requested == BACKEND_KVM:
        if host.system != "Linux":
            raise ArtifactValidationError("the kvm backend requires Linux")
        if arch != host.machine:
            raise ArtifactValidationError(
                f"the local libvirt runtime does not emulate foreign architectures: {arch} image on {host.machine} host"
            )
        return BACKEND_KVM

    if requested == "auto":
        if host.system == "Darwin" and host.machine == "aarch64" and arch == "aarch64":
            return BACKEND_LIMA
        if host.system == "Linux" and arch == host.machine and host.machine in {"x86_64", "aarch64"}:
            return BACKEND_KVM
        raise ArtifactValidationError(
            f"no local runtime can boot a {arch} image on {host.system}/{host.machine}; supported combinations are "
            "Linux x86_64, Linux aarch64, and macOS arm64 (Lima)"
        )

    raise ArtifactValidationError(f"unknown backend requested: {requested}")


def _discover_firmware_file(directory: Path, primary_name: str, glob_pattern: str) -> Path:
    primary = directory / primary_name
    if primary.exists():
        return primary
    matches = sorted(directory.glob(glob_pattern)) if directory.is_dir() else []
    if len(matches) == 1:
        return matches[0]
    raise LifecycleError(f"aarch64 UEFI firmware not found under {directory}; install it with: brew install qemu")


def resolve_domain_profile(backend: str, image_arch: str, *, host: HostPlatform | None = None) -> DomainProfile:
    """Build the libvirt domain shape for ``backend`` booting ``image_arch``.

    ``host`` is accepted for symmetry with :func:`select_backend` but does not
    change the result: the profile depends only on the backend and arch.
    """

    arch = normalize_machine(image_arch)

    if backend == BACKEND_KVM:
        if arch == "x86_64":
            return DomainProfile(
                backend=BACKEND_KVM,
                domain_type="kvm",
                arch="x86_64",
                machine="q35",
                emulator=_QEMU_SYSTEM_X86_64,
                uri="qemu:///system",
                firmware=None,
                autoselect_firmware=False,
                network_mode="libvirt-network",
                seed_tool="cloud-localds",
                seed_bus="sata",
            )
        if arch == "aarch64":
            return DomainProfile(
                backend=BACKEND_KVM,
                domain_type="kvm",
                arch="aarch64",
                machine="virt",
                emulator=_QEMU_SYSTEM_AARCH64,
                uri="qemu:///system",
                firmware=None,
                autoselect_firmware=True,
                network_mode="libvirt-network",
                seed_tool="cloud-localds",
                seed_bus="scsi",
            )
        raise ArtifactValidationError(f"the kvm backend does not support {image_arch} images")

    if backend == BACKEND_HVF:
        if arch != "aarch64":
            raise ArtifactValidationError("the libvirt-hvf backend supports only aarch64 images")
        emulator = Path(shutil.which("qemu-system-aarch64") or "qemu-system-aarch64")
        share_dir = emulator.resolve().parent.parent / "share" / "qemu"
        loader = _discover_firmware_file(share_dir, "edk2-aarch64-code.fd", "edk2-aarch64-code*.fd")
        nvram_template = _discover_firmware_file(share_dir, "edk2-arm-vars.fd", "edk2-arm-vars*.fd")
        return DomainProfile(
            backend=BACKEND_HVF,
            domain_type="hvf",
            arch="aarch64",
            machine="virt",
            emulator=emulator,
            uri="qemu:///session",
            firmware=Firmware(loader=loader, nvram_template=nvram_template),
            autoselect_firmware=False,
            network_mode="user-hostfwd",
            seed_tool="hdiutil",
            seed_bus="scsi",
        )

    raise ArtifactValidationError(f"resolve_domain_profile does not support backend: {backend}")


def _missing_tools(*names: str) -> list[str]:
    return [name for name in names if shutil.which(name) is None]


def _require_tools(*names: str) -> None:
    missing = _missing_tools(*names)
    if missing:
        raise LifecycleError(f"required tool(s) not found on PATH: {', '.join(missing)}; install them first")


def preflight(backend: str, *, host: HostPlatform | None = None) -> None:
    """Fail closed with an install hint unless ``backend`` can actually run here."""

    from . import kvm as _kvm  # deferred: avoid a module-level import cycle with kvm.py

    host = host or detect_host()

    if backend == BACKEND_KVM:
        if not Path("/dev/kvm").exists():
            raise LifecycleError("/dev/kvm is not accessible; load KVM modules and grant access to run local VMs")
        profile = resolve_domain_profile(BACKEND_KVM, host.machine, host=host)
        _require_tools("qemu-img", "cloud-localds", "ssh", "ssh-keygen", profile.emulator.name)
        try:
            _kvm._libvirt()
        except _kvm.KvmUnavailable as exc:
            raise LifecycleError(str(exc)) from exc
        return

    if backend == BACKEND_HVF:
        if host.system != "Darwin":
            raise LifecycleError("the libvirt-hvf backend requires macOS")
        result = subprocess.run(["sysctl", "-n", "kern.hv_support"], capture_output=True, text=True, check=False)
        if result.stdout.strip() != "1":
            raise LifecycleError("Hypervisor.framework is unavailable on this host")
        _require_tools("qemu-system-aarch64", "qemu-img", "hdiutil", "ssh", "ssh-keygen")
        resolve_domain_profile(BACKEND_HVF, "aarch64", host=host)
        try:
            _kvm._libvirt()
        except _kvm.KvmUnavailable as exc:
            raise LifecycleError(str(exc)) from exc
        return

    if backend == BACKEND_LIMA:
        if not (lima.available() and shutil.which("limactl")):
            raise LifecycleError("Lima is required on macOS; install it with: brew install lima")
        return

    raise ArtifactValidationError(f"unknown backend: {backend}")


def allocate_local_port() -> int:
    """Reserve an ephemeral localhost TCP port, then release it for reuse."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
