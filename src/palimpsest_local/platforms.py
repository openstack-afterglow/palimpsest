"""Host capability detection, backend selection, and domain profiles.

This is the single place that knows which local virtualization backend a
given host can run, and what libvirt domain shape that backend needs. Every
other module (``kvm``, ``runtime``, ``cli``, ``project_adapter``, ``build``)
is expected to route arch/host decisions through here instead of re-deriving
``platform.system()``/``platform.machine()`` checks locally.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import lima
from .errors import ArtifactValidationError, LifecycleError
from .runtime_types import (
    CapabilityCheck,
    CapabilityErrorCategory,
    CapabilityProfile,
    CapabilityRequirement,
    DispatchKey,
    PreflightReport,
    PreflightReportPurpose,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
    RuntimePreflightError,
)

BACKEND_KVM = "kvm"
BACKEND_LIMA = "lima-vz"
BACKEND_HVF = "libvirt-hvf"
BACKENDS = (BACKEND_KVM, BACKEND_LIMA, BACKEND_HVF)

_QEMU_SYSTEM_X86_64 = Path("/usr/bin/qemu-system-x86_64")
_QEMU_SYSTEM_AARCH64 = Path("/usr/bin/qemu-system-aarch64")
_PREFLIGHT_TTL_NS = 5 * 60 * 1_000_000_000
_MAX_OPERATION_REPORT_REGISTRY_ENTRIES = 4096
_REPORT_AUTHENTICATION_KEY = secrets.token_bytes(32)
_REPORT_REGISTRY_LOCK = threading.Lock()
_ISSUED_OPERATION_REPORTS: dict[str, tuple[str, int]] = {}
_CONSUMED_OPERATION_REPORTS: dict[str, int] = {}

_KVM_CREATE_REQUIREMENTS = (
    "host.kvm-device",
    "tool.qemu-img",
    "tool.cloud-localds",
    "tool.ssh",
    "tool.ssh-keygen",
    "tool.qemu-system",
    "python.libvirt",
)
_HVF_CREATE_REQUIREMENTS = (
    "host.hypervisor-framework",
    "tool.qemu-system",
    "tool.qemu-img",
    "tool.hdiutil",
    "tool.ssh",
    "tool.ssh-keygen",
    "firmware.uefi",
    "python.libvirt",
)
_LIBVIRT_LIVE_OPERATIONS = frozenset(
    {
        RuntimeOperation.START,
        RuntimeOperation.STOP,
        RuntimeOperation.RM,
        RuntimeOperation.RECONCILE,
    }
)
_PROCESS_OPERATIONS = frozenset({RuntimeOperation.EXEC, RuntimeOperation.SHELL})
_READ_ONLY_STATE_OPERATIONS = frozenset({RuntimeOperation.INSPECT, RuntimeOperation.LOGS, RuntimeOperation.PS})
_LIMA_LIVE_OPERATIONS = frozenset(
    {
        RuntimeOperation.RUN,
        RuntimeOperation.START,
        RuntimeOperation.STOP,
        RuntimeOperation.RM,
        RuntimeOperation.RECONCILE,
    }
)


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


def _requirements(*identifiers: str) -> tuple[CapabilityRequirement, ...]:
    return tuple(CapabilityRequirement(identifier) for identifier in identifiers)


def capability_profile(
    dispatch_key: DispatchKey,
    operation: RuntimeOperation,
    *,
    network: str | None = "default",
) -> CapabilityProfile:
    """Return the exact declarative operation profile without probing the host."""

    if not isinstance(dispatch_key, DispatchKey) or not isinstance(operation, RuntimeOperation):
        raise TypeError("runtime capability lookup requires a DispatchKey and RuntimeOperation")
    if network is not None and (not isinstance(network, str) or not network or len(network) > 256 or "\x00" in network):
        raise ValueError("runtime capability network selector is invalid")
    kind = dispatch_key.runtime_kind
    backend = dispatch_key.backend
    if kind is RuntimeKind.OCI_ROOT:
        if operation is RuntimeOperation.RUN:
            identifiers = ("host.kvm-device", "tool.qemu-img", "tool.qemu-system", "python.libvirt")
        elif operation is RuntimeOperation.RM:
            identifiers = ("python.libvirt",)
        elif operation in {RuntimeOperation.STOP, RuntimeOperation.PS, RuntimeOperation.EXEC}:
            identifiers = ()
        else:
            raise RuntimeCapabilityError(operation, dispatch_key)
    elif backend is RuntimeBackend.KVM:
        if operation is RuntimeOperation.RUN:
            identifiers = _KVM_CREATE_REQUIREMENTS
        elif operation is RuntimeOperation.COMMIT:
            identifiers = ("python.libvirt", "tool.ssh", "tool.scp")
        elif operation in _LIBVIRT_LIVE_OPERATIONS:
            identifiers = ("python.libvirt",)
        elif operation in _PROCESS_OPERATIONS:
            identifiers = ("tool.ssh",)
        elif operation in _READ_ONLY_STATE_OPERATIONS:
            identifiers = ()
        else:  # pragma: no cover - fail closed when RuntimeOperation grows
            raise RuntimeCapabilityError(operation, dispatch_key)
    elif backend is RuntimeBackend.LIBVIRT_HVF:
        if operation is RuntimeOperation.RUN:
            identifiers = _HVF_CREATE_REQUIREMENTS
        elif operation is RuntimeOperation.COMMIT:
            identifiers = ("python.libvirt", "tool.ssh", "tool.scp")
        elif operation in _LIBVIRT_LIVE_OPERATIONS:
            identifiers = ("python.libvirt",)
        elif operation in _PROCESS_OPERATIONS:
            identifiers = ("tool.ssh",)
        elif operation in _READ_ONLY_STATE_OPERATIONS:
            identifiers = ()
        else:  # pragma: no cover - fail closed when RuntimeOperation grows
            raise RuntimeCapabilityError(operation, dispatch_key)
    elif backend is RuntimeBackend.LIMA_VZ:
        if operation in _LIMA_LIVE_OPERATIONS:
            identifiers = ("host.lima-vz", "tool.limactl")
        elif operation in _READ_ONLY_STATE_OPERATIONS:
            identifiers = ()
        else:  # pragma: no cover - fail closed when RuntimeOperation grows
            raise RuntimeCapabilityError(operation, dispatch_key)
    else:  # pragma: no cover - DispatchKey rejects unknown combinations
        raise RuntimeCapabilityError(operation, dispatch_key)
    requirements = _requirements(*identifiers)
    if (
        operation is RuntimeOperation.RUN
        and network is not None
        and not (backend is RuntimeBackend.KVM and network == "none")
    ):
        if backend is RuntimeBackend.LIMA_VZ:
            network_capability = "network.lima"
        elif backend is RuntimeBackend.LIBVIRT_HVF:
            network_capability = "network.user-hostfwd"
        else:
            network_capability = "network.libvirt"
        requirements += (CapabilityRequirement(network_capability, network),)
    return CapabilityProfile(1, dispatch_key, operation, requirements)


def _passed(requirement: CapabilityRequirement, observed: str) -> CapabilityCheck:
    return CapabilityCheck(requirement.capability_id, observed, True)


def _failed(
    requirement: CapabilityRequirement,
    observed: str,
    category: CapabilityErrorCategory,
    remediation: str,
) -> CapabilityCheck:
    return CapabilityCheck(requirement.capability_id, observed, False, category, remediation)


def _check_capability(
    requirement: CapabilityRequirement,
    *,
    dispatch_key: DispatchKey,
    host: HostPlatform,
    arch: str | None,
) -> CapabilityCheck:
    from . import kvm as _kvm  # deferred: platforms is imported by kvm.py

    identifier = requirement.capability_id
    if identifier == "host.kvm-device":
        device = Path("/dev/kvm")
        if not device.exists() or not os.access(device, os.R_OK | os.W_OK):
            return _failed(
                requirement,
                "inaccessible",
                CapabilityErrorCategory.MISSING,
                "/dev/kvm is not accessible; load KVM modules and grant read/write access to run local VMs",
            )
        descriptor: int | None = None
        try:
            descriptor = os.open(device, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
            api_version = fcntl.ioctl(descriptor, 0xAE00)
        except OSError:
            return _failed(
                requirement,
                "unusable",
                CapabilityErrorCategory.CHECK_FAILED,
                "/dev/kvm could not answer the KVM API probe",
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if api_version != 12:
            return _failed(
                requirement,
                "unsupported-api",
                CapabilityErrorCategory.UNSUPPORTED,
                "/dev/kvm exposes an unsupported KVM API version",
            )
        return _passed(requirement, "api-v12")
    if identifier == "host.hypervisor-framework":
        if host.system != "Darwin":
            return _failed(
                requirement,
                "unsupported-host",
                CapabilityErrorCategory.UNSUPPORTED,
                "the libvirt-hvf backend requires macOS",
            )
        try:
            result = subprocess.run(
                ["sysctl", "-n", "kern.hv_support"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return _failed(
                requirement,
                "probe-failed",
                CapabilityErrorCategory.CHECK_FAILED,
                "Hypervisor.framework capability check failed",
            )
        if result.stdout.strip() == "1":
            return _passed(requirement, "supported")
        return _failed(
            requirement,
            "unsupported",
            CapabilityErrorCategory.UNSUPPORTED,
            "Hypervisor.framework is unavailable on this host",
        )
    if identifier == "host.lima-vz":
        if host.system == "Darwin" and host.machine == "aarch64":
            return _passed(requirement, "supported")
        return _failed(
            requirement,
            "unsupported-host",
            CapabilityErrorCategory.UNSUPPORTED,
            "Lima is required on macOS; install it with: brew install lima",
        )
    if identifier.startswith("tool."):
        tool = identifier.removeprefix("tool.")
        if tool == "qemu-system":
            try:
                domain = resolve_domain_profile(
                    dispatch_key.backend.value,
                    normalize_machine(arch or host.machine),
                    host=host,
                )
            except LifecycleError as exc:
                return _failed(requirement, "missing", CapabilityErrorCategory.MISSING, str(exc))
            emulator = domain.emulator
            if emulator.is_file() and os.access(emulator, os.X_OK):
                return _passed(requirement, "configured-executable")
            return _failed(
                requirement,
                "missing",
                CapabilityErrorCategory.MISSING,
                f"configured QEMU emulator is not executable: {emulator}",
            )
        if tool == "limactl":
            if shutil.which(tool) is None:
                return _failed(
                    requirement,
                    "missing",
                    CapabilityErrorCategory.MISSING,
                    "required tool(s) not found on PATH: limactl; install them first",
                )
            try:
                lima._require_supported_version()
            except LifecycleError as exc:
                message = str(exc)
                unsupported = message.startswith("unsupported Lima version ")
                remediation = (
                    message
                    if unsupported or message.startswith("cannot determine Lima version;")
                    else "Lima version check failed; verify that limactl --version succeeds"
                )
                return _failed(
                    requirement,
                    "unsupported-version" if unsupported else "version-check-failed",
                    CapabilityErrorCategory.UNSUPPORTED if unsupported else CapabilityErrorCategory.CHECK_FAILED,
                    remediation,
                )
            return _passed(requirement, "supported-2.x")
        if shutil.which(tool) is not None:
            return _passed(requirement, "present")
        return _failed(
            requirement,
            "missing",
            CapabilityErrorCategory.MISSING,
            f"required tool(s) not found on PATH: {tool}; install them first",
        )
    if identifier == "firmware.uefi":
        try:
            resolve_domain_profile(BACKEND_HVF, "aarch64", host=host)
        except LifecycleError as exc:
            return _failed(requirement, "missing", CapabilityErrorCategory.MISSING, str(exc))
        return _passed(requirement, "present")
    if identifier == "python.libvirt":
        try:
            _kvm._libvirt()
        except _kvm.KvmUnavailable as exc:
            return _failed(requirement, "missing", CapabilityErrorCategory.MISSING, str(exc))
        except Exception:
            return _failed(
                requirement,
                "probe-failed",
                CapabilityErrorCategory.CHECK_FAILED,
                "libvirt capability check failed",
            )
        return _passed(requirement, "importable")
    if identifier == "network.libvirt":
        assert requirement.selector is not None
        try:
            domain = resolve_domain_profile(
                dispatch_key.backend.value,
                normalize_machine(arch or host.machine),
                host=host,
            )
            _kvm.validate_network(requirement.selector, uri=domain.uri, profile=domain)
        except (ArtifactValidationError, LifecycleError):
            return _failed(
                requirement,
                "unavailable",
                CapabilityErrorCategory.CHECK_FAILED,
                "the requested libvirt network is unavailable or inactive",
            )
        except Exception:
            return _failed(
                requirement,
                "probe-failed",
                CapabilityErrorCategory.CHECK_FAILED,
                "libvirt network capability check failed",
            )
        return _passed(requirement, "active" if domain.network_mode == "libvirt-network" else "user-mode")
    if identifier == "network.user-hostfwd":
        if dispatch_key.backend is RuntimeBackend.LIBVIRT_HVF:
            return _passed(requirement, "built-in")
        return _failed(
            requirement,
            "unsupported-backend",
            CapabilityErrorCategory.CHECK_FAILED,
            "user-mode host forwarding is unavailable for this backend",
        )
    if identifier == "network.lima":
        assert requirement.selector is not None
        try:
            lima.validate_network(requirement.selector)
        except (ArtifactValidationError, LifecycleError):
            return _failed(
                requirement,
                "unavailable",
                CapabilityErrorCategory.CHECK_FAILED,
                "the requested Lima network is unavailable",
            )
        except Exception:
            return _failed(
                requirement,
                "probe-failed",
                CapabilityErrorCategory.CHECK_FAILED,
                "Lima network capability check failed",
            )
        return _passed(requirement, "available")
    return _failed(
        requirement,
        "unknown",
        CapabilityErrorCategory.CHECK_FAILED,
        "runtime capability check is unavailable",
    )


def host_capability_digest(host: HostPlatform) -> str:
    """Return the canonical identity of the host used for capability probes."""

    if not isinstance(host, HostPlatform):
        raise TypeError("host capability binding requires a HostPlatform")
    encoded = json.dumps(
        {"machine": host.machine, "system": host.system},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _report_authentication_payload(
    *,
    schema_version: int,
    profile: CapabilityProfile,
    subject_digest: str,
    checks: tuple[CapabilityCheck, ...],
    issued_at_monotonic_ns: int,
    expires_at_monotonic_ns: int,
    host_digest: str,
    purpose: PreflightReportPurpose,
    issuer_nonce: str,
) -> bytes:
    payload = {
        "schema_version": schema_version,
        "profile": {
            "profile_id": profile.profile_id,
            "requirements": [
                {"capability_id": item.capability_id, "selector": item.selector} for item in profile.requirements
            ],
        },
        "subject_digest": subject_digest,
        "checks": [
            {
                "capability_id": item.capability_id,
                "observed": item.observed,
                "passed": item.passed,
                "error_category": item.error_category.value if item.error_category is not None else None,
                "remediation": item.remediation,
            }
            for item in checks
        ],
        "issued_at_monotonic_ns": issued_at_monotonic_ns,
        "expires_at_monotonic_ns": expires_at_monotonic_ns,
        "host_digest": host_digest,
        "purpose": purpose.value,
        "issuer_nonce": issuer_nonce,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _report_authentication_tag(report: PreflightReport) -> str:
    return hmac.new(
        _REPORT_AUTHENTICATION_KEY,
        _report_authentication_payload(
            schema_version=report.schema_version,
            profile=report.profile,
            subject_digest=report.subject_digest,
            checks=report.checks,
            issued_at_monotonic_ns=report.issued_at_monotonic_ns,
            expires_at_monotonic_ns=report.expires_at_monotonic_ns,
            host_digest=report.host_digest,
            purpose=report.purpose,
            issuer_nonce=report.issuer_nonce,
        ),
        hashlib.sha256,
    ).hexdigest()


def _issue_capability_report(
    profile: CapabilityProfile,
    *,
    subject_digest: str,
    checks: tuple[CapabilityCheck, ...],
    host: HostPlatform,
    issued_at_monotonic_ns: int,
    purpose: PreflightReportPurpose,
) -> PreflightReport:
    expires = issued_at_monotonic_ns + _PREFLIGHT_TTL_NS
    nonce = secrets.token_hex(32)
    unsigned = PreflightReport(
        1,
        profile,
        subject_digest,
        checks,
        issued_at_monotonic_ns,
        expires,
        host_capability_digest(host),
        purpose,
        nonce,
        "0" * 64,
    )
    report = PreflightReport(
        unsigned.schema_version,
        unsigned.profile,
        unsigned.subject_digest,
        unsigned.checks,
        unsigned.issued_at_monotonic_ns,
        unsigned.expires_at_monotonic_ns,
        unsigned.host_digest,
        unsigned.purpose,
        unsigned.issuer_nonce,
        _report_authentication_tag(unsigned),
    )
    if purpose is PreflightReportPurpose.OPERATION and report.successful:
        with _REPORT_REGISTRY_LOCK:
            _prune_report_registries(issued_at_monotonic_ns)
            if len(_ISSUED_OPERATION_REPORTS) >= _MAX_OPERATION_REPORT_REGISTRY_ENTRIES:
                raise RuntimePreflightError(
                    CapabilityErrorCategory.REPORT_CAPACITY,
                    "operation preflight report capacity is exhausted; retry after an existing report expires",
                )
            _ISSUED_OPERATION_REPORTS[nonce] = (report.authentication_tag, expires)
    return report


def _prune_report_registries(now_ns: int, *, preserve_nonce: str | None = None) -> None:
    """Bound daemon memory while preserving the token currently being classified."""

    for nonce, (_, expires) in tuple(_ISSUED_OPERATION_REPORTS.items()):
        if nonce != preserve_nonce and expires <= now_ns:
            del _ISSUED_OPERATION_REPORTS[nonce]
    for nonce, expires in tuple(_CONSUMED_OPERATION_REPORTS.items()):
        if nonce != preserve_nonce and expires <= now_ns:
            del _CONSUMED_OPERATION_REPORTS[nonce]


def evaluate_capability_profile(
    profile: CapabilityProfile,
    *,
    subject_digest: str,
    arch: str | None = None,
    host: HostPlatform | None = None,
    now_ns: int | None = None,
    purpose: PreflightReportPurpose = PreflightReportPurpose.OPERATION,
) -> PreflightReport:
    """Evaluate read-only probes and issue an authenticated capability report."""

    if not isinstance(profile, CapabilityProfile):
        raise TypeError("capability evaluation requires a CapabilityProfile")
    if not isinstance(purpose, PreflightReportPurpose):
        raise TypeError("capability evaluation requires a report purpose")
    resolved_host = host or detect_host()
    issued = time.monotonic_ns() if now_ns is None else now_ns
    if type(issued) is not int or issued < 0:
        raise ValueError("preflight clock returned an invalid value")
    checks = tuple(
        _check_capability(requirement, dispatch_key=profile.dispatch_key, host=resolved_host, arch=arch)
        for requirement in profile.requirements
    )
    return _issue_capability_report(
        profile,
        subject_digest=subject_digest,
        checks=checks,
        host=resolved_host,
        issued_at_monotonic_ns=issued,
        purpose=purpose,
    )


def consume_capability_report(
    report: PreflightReport,
    *,
    expected_profile: CapabilityProfile,
    expected_subject_digest: str,
    host: HostPlatform | None = None,
    now_ns: int | None = None,
) -> None:
    """Authenticate and atomically burn one operation report exactly once.

    Once provenance is established, the nonce is burned before subject and
    freshness checks. An authentic mismatched or stale report therefore cannot
    be retried against another operation.
    """

    if not isinstance(report, PreflightReport):
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "runtime operation requires an issuer-authenticated preflight report",
        )
    expected_tag = _report_authentication_tag(report)
    if not hmac.compare_digest(report.authentication_tag, expected_tag):
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "preflight report provenance could not be verified",
            report=report,
        )
    if report.purpose is not PreflightReportPurpose.OPERATION:
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "discovery reports cannot authorize runtime operations",
            report=report,
        )
    now = time.monotonic_ns() if now_ns is None else now_ns
    if type(now) is not int or now < 0:
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_STALE,
            "preflight clock returned an invalid value",
            report=report,
        )
    resolved_host = host or detect_host()
    with _REPORT_REGISTRY_LOCK:
        _prune_report_registries(now, preserve_nonce=report.issuer_nonce)
        if report.issuer_nonce in _CONSUMED_OPERATION_REPORTS:
            raise RuntimePreflightError(
                CapabilityErrorCategory.REPORT_CONSUMED,
                "preflight report has already been consumed",
                report=report,
            )
        registered = _ISSUED_OPERATION_REPORTS.get(report.issuer_nonce)
        if registered is None or not hmac.compare_digest(registered[0], report.authentication_tag):
            raise RuntimePreflightError(
                CapabilityErrorCategory.REPORT_PROVENANCE,
                "preflight report was not issued for an operation",
                report=report,
            )
        del _ISSUED_OPERATION_REPORTS[report.issuer_nonce]
        _CONSUMED_OPERATION_REPORTS[report.issuer_nonce] = registered[1]
        while len(_CONSUMED_OPERATION_REPORTS) > _MAX_OPERATION_REPORT_REGISTRY_ENTRIES:
            oldest_nonce = next(iter(_CONSUMED_OPERATION_REPORTS))
            del _CONSUMED_OPERATION_REPORTS[oldest_nonce]
    if (
        not report.successful
        or report.profile != expected_profile
        or report.subject_digest != expected_subject_digest
        or report.host_digest != host_capability_digest(resolved_host)
    ):
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_MISMATCH,
            "preflight report does not match the runtime operation subject",
            report=report,
        )
    if now < report.issued_at_monotonic_ns or now >= report.expires_at_monotonic_ns:
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_STALE,
            "preflight report is stale; run preflight again",
            report=report,
        )


def backend_capability_report(
    backend: str,
    *,
    operation: RuntimeOperation = RuntimeOperation.RUN,
    host: HostPlatform | None = None,
) -> PreflightReport:
    """Return a cloud-image backend report for UI/read-only discovery."""

    resolved_host = host or detect_host()
    try:
        runtime_backend = RuntimeBackend(backend)
    except ValueError:
        raise ArtifactValidationError(f"unknown backend: {backend}") from None
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, runtime_backend)
    profile = capability_profile(key, operation)
    raw_subject = f"{profile.profile_id}:{resolved_host.system}:{resolved_host.machine}".encode()
    subject_digest = "sha256:" + hashlib.sha256(raw_subject).hexdigest()
    return evaluate_capability_profile(
        profile,
        subject_digest=subject_digest,
        arch=resolved_host.machine,
        host=resolved_host,
        purpose=PreflightReportPurpose.DISCOVERY,
    )


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
