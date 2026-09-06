"""Unit tests for palimpsest_local.platforms."""

from __future__ import annotations

import re
import socket
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import palimpsest_local.kvm as kvm_module
import palimpsest_local.platforms as platforms
from palimpsest_local.errors import ArtifactValidationError, LifecycleError
from palimpsest_local.runtime_types import (
    CapabilityCheck,
    CapabilityErrorCategory,
    CapabilityRequirement,
    DispatchKey,
    PreflightReportPurpose,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
    RuntimePreflightError,
)


def test_backend_constants_are_stable_identifiers():
    assert platforms.BACKEND_KVM == "kvm"
    assert platforms.BACKEND_LIMA == "lima-vz"
    assert platforms.BACKEND_HVF == "libvirt-hvf"
    assert platforms.BACKENDS == ("kvm", "lima-vz", "libvirt-hvf")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("arm64", "aarch64"),
        ("aarch64", "aarch64"),
        ("AArch64", "aarch64"),
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("AMD64", "x86_64"),
        ("ppc64le", "ppc64le"),
    ],
)
def test_normalize_machine(value, expected):
    assert platforms.normalize_machine(value) == expected


def test_detect_host_normalizes_injected_values():
    host = platforms.detect_host(system="Darwin", machine="arm64")
    assert host == platforms.HostPlatform(system="Darwin", machine="aarch64")


def test_detect_host_falls_back_to_platform_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platforms.platform, "machine", lambda: "amd64")
    assert platforms.detect_host() == platforms.HostPlatform(system="Linux", machine="x86_64")


# --- select_backend -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("host_system", "host_machine", "image_arch", "requested", "expected"),
    [
        ("Darwin", "aarch64", "aarch64", "lima-vz", "lima-vz"),
        ("Darwin", "aarch64", "aarch64", "libvirt-hvf", "libvirt-hvf"),
        ("Linux", "x86_64", "x86_64", "kvm", "kvm"),
        ("Linux", "aarch64", "aarch64", "kvm", "kvm"),
        ("Darwin", "aarch64", "aarch64", "auto", "lima-vz"),
        ("Linux", "x86_64", "x86_64", "auto", "kvm"),
        ("Linux", "aarch64", "aarch64", "auto", "kvm"),
    ],
)
def test_select_backend_accepts_supported_combinations(host_system, host_machine, image_arch, requested, expected):
    host = platforms.HostPlatform(system=host_system, machine=host_machine)
    assert platforms.select_backend(image_arch, host=host, requested=requested) == expected


@pytest.mark.parametrize(
    ("host_system", "host_machine", "image_arch", "requested", "match"),
    [
        (
            "Darwin",
            "x86_64",
            "x86_64",
            "lima-vz",
            r"^the lima-vz backend requires macOS on Apple Silicon$",
        ),
        (
            "Linux",
            "x86_64",
            "x86_64",
            "lima-vz",
            r"^the lima-vz backend requires macOS on Apple Silicon$",
        ),
        (
            "Linux",
            "aarch64",
            "aarch64",
            "lima-vz",
            r"^the lima-vz backend requires macOS on Apple Silicon$",
        ),
        (
            "Linux",
            "x86_64",
            "x86_64",
            "libvirt-hvf",
            r"^the libvirt-hvf backend requires macOS$",
        ),
        (
            "Darwin",
            "aarch64",
            "x86_64",
            "libvirt-hvf",
            r"^the local libvirt runtime does not emulate foreign architectures: x86_64 image on aarch64 host$",
        ),
        (
            "Darwin",
            "aarch64",
            "aarch64",
            "kvm",
            r"^the kvm backend requires Linux$",
        ),
        (
            "Linux",
            "x86_64",
            "aarch64",
            "kvm",
            r"^the local libvirt runtime does not emulate foreign architectures: aarch64 image on x86_64 host$",
        ),
        (
            "Darwin",
            "aarch64",
            "x86_64",
            "auto",
            r"^no local runtime can boot a x86_64 image on Darwin/aarch64; supported combinations are Linux "
            r"x86_64, Linux aarch64, and macOS arm64 \(Lima\)$",
        ),
        (
            "Linux",
            "x86_64",
            "aarch64",
            "auto",
            r"^no local runtime can boot a aarch64 image on Linux/x86_64; supported combinations are Linux "
            r"x86_64, Linux aarch64, and macOS arm64 \(Lima\)$",
        ),
        (
            "Linux",
            "ppc64le",
            "ppc64le",
            "auto",
            r"^no local runtime can boot a ppc64le image on Linux/ppc64le;",
        ),
        (
            "FreeBSD",
            "x86_64",
            "x86_64",
            "auto",
            r"^no local runtime can boot a x86_64 image on FreeBSD/x86_64;",
        ),
    ],
)
def test_select_backend_rejects_unsupported_combinations_with_exact_message(
    host_system, host_machine, image_arch, requested, match
):
    host = platforms.HostPlatform(system=host_system, machine=host_machine)
    with pytest.raises(ArtifactValidationError, match=match):
        platforms.select_backend(image_arch, host=host, requested=requested)


def test_select_backend_rejects_unknown_requested_value():
    host = platforms.HostPlatform(system="Linux", machine="x86_64")
    with pytest.raises(ArtifactValidationError, match="^unknown backend requested: bogus$"):
        platforms.select_backend("x86_64", host=host, requested="bogus")


def test_select_backend_defaults_host_to_current_platform(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform(system="Linux", machine="x86_64"))
    assert platforms.select_backend("x86_64") == "kvm"


# --- resolve_domain_profile -----------------------------------------------------------


def test_resolve_domain_profile_kvm_x86_64_matches_todays_domain_shape():
    profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
    assert profile == platforms.DomainProfile(
        backend="kvm",
        domain_type="kvm",
        arch="x86_64",
        machine="q35",
        emulator=Path("/usr/bin/qemu-system-x86_64"),
        uri="qemu:///system",
        firmware=None,
        autoselect_firmware=False,
        network_mode="libvirt-network",
        seed_tool="cloud-localds",
        seed_bus="sata",
    )


def test_resolve_domain_profile_kvm_aarch64_uses_virt_machine_and_efi_autoselect():
    profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "aarch64")
    assert profile == platforms.DomainProfile(
        backend="kvm",
        domain_type="kvm",
        arch="aarch64",
        machine="virt",
        emulator=Path("/usr/bin/qemu-system-aarch64"),
        uri="qemu:///system",
        firmware=None,
        autoselect_firmware=True,
        network_mode="libvirt-network",
        seed_tool="cloud-localds",
        seed_bus="scsi",
    )


def test_resolve_domain_profile_kvm_rejects_unsupported_arch():
    with pytest.raises(ArtifactValidationError, match="the kvm backend does not support ppc64le images"):
        platforms.resolve_domain_profile(platforms.BACKEND_KVM, "ppc64le")


def test_resolve_domain_profile_hvf_rejects_non_aarch64():
    with pytest.raises(ArtifactValidationError, match="the libvirt-hvf backend supports only aarch64 images"):
        platforms.resolve_domain_profile(platforms.BACKEND_HVF, "x86_64")


def test_resolve_domain_profile_unknown_backend():
    with pytest.raises(ArtifactValidationError, match="resolve_domain_profile does not support backend: bogus"):
        platforms.resolve_domain_profile("bogus", "x86_64")


def _fake_emulator(tmp_path: Path) -> Path:
    return tmp_path / "opt" / "homebrew" / "bin" / "qemu-system-aarch64"


def test_resolve_domain_profile_hvf_uses_default_firmware_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    share_dir = emulator.resolve().parent.parent / "share" / "qemu"
    share_dir.mkdir(parents=True)
    loader = share_dir / "edk2-aarch64-code.fd"
    nvram = share_dir / "edk2-arm-vars.fd"
    loader.write_bytes(b"")
    nvram.write_bytes(b"")
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))

    profile = platforms.resolve_domain_profile(platforms.BACKEND_HVF, "aarch64")

    assert profile.backend == platforms.BACKEND_HVF
    assert profile.domain_type == "hvf"
    assert profile.machine == "virt"
    assert profile.emulator == emulator
    assert profile.uri == "qemu:///session"
    assert profile.autoselect_firmware is False
    assert profile.network_mode == "user-hostfwd"
    assert profile.seed_tool == "hdiutil"
    assert profile.seed_bus == "scsi"
    assert profile.firmware == platforms.Firmware(loader=loader, nvram_template=nvram)


def test_resolve_domain_profile_hvf_falls_back_to_sole_glob_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    share_dir = emulator.resolve().parent.parent / "share" / "qemu"
    share_dir.mkdir(parents=True)
    loader = share_dir / "edk2-aarch64-code.20240101.fd"
    nvram = share_dir / "edk2-arm-vars.20240101.fd"
    loader.write_bytes(b"")
    nvram.write_bytes(b"")
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))

    profile = platforms.resolve_domain_profile(platforms.BACKEND_HVF, "aarch64")

    assert profile.firmware == platforms.Firmware(loader=loader, nvram_template=nvram)


def test_resolve_domain_profile_hvf_raises_when_firmware_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    share_dir = emulator.resolve().parent.parent / "share" / "qemu"
    share_dir.mkdir(parents=True)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))

    with pytest.raises(LifecycleError, match=f"aarch64 UEFI firmware not found under {re.escape(str(share_dir))}"):
        platforms.resolve_domain_profile(platforms.BACKEND_HVF, "aarch64")


def test_resolve_domain_profile_hvf_raises_when_glob_is_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    share_dir = emulator.resolve().parent.parent / "share" / "qemu"
    share_dir.mkdir(parents=True)
    (share_dir / "edk2-aarch64-code.a.fd").write_bytes(b"")
    (share_dir / "edk2-aarch64-code.b.fd").write_bytes(b"")
    (share_dir / "edk2-arm-vars.fd").write_bytes(b"")
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))

    with pytest.raises(LifecycleError, match="aarch64 UEFI firmware not found under"):
        platforms.resolve_domain_profile(platforms.BACKEND_HVF, "aarch64")


def test_resolve_domain_profile_hvf_fails_closed_without_which_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: None)

    with pytest.raises(LifecycleError, match="aarch64 UEFI firmware not found under"):
        platforms.resolve_domain_profile(platforms.BACKEND_HVF, "aarch64")


# --- preflight ------------------------------------------------------------------------


def _patch_dev_kvm(monkeypatch: pytest.MonkeyPatch, *, accessible: bool) -> None:
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/dev/kvm":
            return accessible
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)


def test_preflight_kvm_requires_dev_kvm_access(monkeypatch: pytest.MonkeyPatch):
    _patch_dev_kvm(monkeypatch, accessible=False)
    host = platforms.HostPlatform(system="Linux", machine="x86_64")
    with pytest.raises(LifecycleError, match="/dev/kvm is not accessible"):
        platforms.preflight(platforms.BACKEND_KVM, host=host)


def test_preflight_kvm_requires_tools_on_path(monkeypatch: pytest.MonkeyPatch):
    _patch_dev_kvm(monkeypatch, accessible=True)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: None if name == "cloud-localds" else f"/usr/bin/{name}")
    host = platforms.HostPlatform(system="Linux", machine="x86_64")
    with pytest.raises(LifecycleError, match="cloud-localds"):
        platforms.preflight(platforms.BACKEND_KVM, host=host)


def test_preflight_kvm_checks_the_host_specific_emulator_binary(monkeypatch: pytest.MonkeyPatch):
    _patch_dev_kvm(monkeypatch, accessible=True)
    monkeypatch.setattr(
        platforms.shutil,
        "which",
        lambda name: None if name == "qemu-system-aarch64" else f"/usr/bin/{name}",
    )
    host = platforms.HostPlatform(system="Linux", machine="aarch64")
    with pytest.raises(LifecycleError, match="qemu-system-aarch64"):
        platforms.preflight(platforms.BACKEND_KVM, host=host)


def test_preflight_kvm_wraps_missing_libvirt_python_as_lifecycle_error(monkeypatch: pytest.MonkeyPatch):
    _patch_dev_kvm(monkeypatch, accessible=True)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: f"/usr/bin/{name}")

    def raise_unavailable():
        raise kvm_module.KvmUnavailable("libvirt-python is not installed; install palimpsest-local[kvm]")

    monkeypatch.setattr(kvm_module, "_libvirt", raise_unavailable)
    host = platforms.HostPlatform(system="Linux", machine="x86_64")
    with pytest.raises(LifecycleError, match="libvirt-python is not installed"):
        platforms.preflight(platforms.BACKEND_KVM, host=host)


def test_preflight_kvm_succeeds_when_everything_is_available(monkeypatch: pytest.MonkeyPatch):
    _patch_dev_kvm(monkeypatch, accessible=True)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kvm_module, "_libvirt", lambda: object())
    host = platforms.HostPlatform(system="Linux", machine="x86_64")
    assert platforms.preflight(platforms.BACKEND_KVM, host=host) is None


def _fake_run(stdout: str):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


def test_preflight_hvf_rejects_non_macos_before_sysctl(monkeypatch: pytest.MonkeyPatch):
    def unexpected_run(*args, **kwargs):
        raise AssertionError("sysctl must not run for a non-macOS host")

    monkeypatch.setattr(platforms.subprocess, "run", unexpected_run)
    host = platforms.HostPlatform(system="Linux", machine="aarch64")
    with pytest.raises(LifecycleError, match="the libvirt-hvf backend requires macOS"):
        platforms.preflight(platforms.BACKEND_HVF, host=host)


def test_preflight_hvf_requires_hypervisor_framework(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms.subprocess, "run", _fake_run("0\n"))
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    with pytest.raises(LifecycleError, match="Hypervisor.framework is unavailable on this host"):
        platforms.preflight(platforms.BACKEND_HVF, host=host)


def test_preflight_hvf_requires_tools_on_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms.subprocess, "run", _fake_run("1\n"))
    monkeypatch.setattr(platforms.shutil, "which", lambda name: None if name == "hdiutil" else f"/usr/bin/{name}")
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    with pytest.raises(LifecycleError, match="hdiutil"):
        platforms.preflight(platforms.BACKEND_HVF, host=host)


def test_preflight_hvf_requires_firmware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    monkeypatch.setattr(platforms.subprocess, "run", _fake_run("1\n"))
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    with pytest.raises(LifecycleError, match="aarch64 UEFI firmware not found under"):
        platforms.preflight(platforms.BACKEND_HVF, host=host)


def test_preflight_hvf_wraps_missing_libvirt_python_as_lifecycle_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    share_dir = emulator.resolve().parent.parent / "share" / "qemu"
    share_dir.mkdir(parents=True)
    (share_dir / "edk2-aarch64-code.fd").write_bytes(b"")
    (share_dir / "edk2-arm-vars.fd").write_bytes(b"")
    monkeypatch.setattr(platforms.subprocess, "run", _fake_run("1\n"))
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))

    def raise_unavailable():
        raise kvm_module.KvmUnavailable("libvirt-python is not installed; install palimpsest-local[kvm]")

    monkeypatch.setattr(kvm_module, "_libvirt", raise_unavailable)
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    with pytest.raises(LifecycleError, match="libvirt-python is not installed"):
        platforms.preflight(platforms.BACKEND_HVF, host=host)


def test_preflight_hvf_succeeds_when_everything_is_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    emulator = _fake_emulator(tmp_path)
    share_dir = emulator.resolve().parent.parent / "share" / "qemu"
    share_dir.mkdir(parents=True)
    (share_dir / "edk2-aarch64-code.fd").write_bytes(b"")
    (share_dir / "edk2-arm-vars.fd").write_bytes(b"")
    monkeypatch.setattr(platforms.subprocess, "run", _fake_run("1\n"))
    monkeypatch.setattr(platforms.shutil, "which", lambda name: str(emulator))
    monkeypatch.setattr(kvm_module, "_libvirt", lambda: object())
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    assert platforms.preflight(platforms.BACKEND_HVF, host=host) is None


def test_preflight_lima_requires_lima_available(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms.lima, "available", lambda: False)
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    with pytest.raises(LifecycleError, match="Lima is required on macOS; install it with: brew install lima"):
        platforms.preflight(platforms.BACKEND_LIMA, host=host)


def test_preflight_lima_requires_limactl_binary(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms.lima, "available", lambda: True)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: None)
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    with pytest.raises(LifecycleError, match="Lima is required on macOS; install it with: brew install lima"):
        platforms.preflight(platforms.BACKEND_LIMA, host=host)


def test_preflight_lima_succeeds_when_available(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(platforms.lima, "available", lambda: True)
    monkeypatch.setattr(platforms.shutil, "which", lambda name: "/usr/local/bin/limactl")
    host = platforms.HostPlatform(system="Darwin", machine="aarch64")
    assert platforms.preflight(platforms.BACKEND_LIMA, host=host) is None


def test_preflight_rejects_unknown_backend():
    with pytest.raises(ArtifactValidationError, match="^unknown backend: bogus$"):
        platforms.preflight("bogus")


def test_operation_capability_matrix_is_exact_for_all_30_supported_cloud_operations() -> None:
    kvm_run = (
        "host.kvm-device",
        "tool.qemu-img",
        "tool.cloud-localds",
        "tool.ssh",
        "tool.ssh-keygen",
        "tool.qemu-system",
        "python.libvirt",
        "network.libvirt",
    )
    hvf_run = (
        "host.hypervisor-framework",
        "tool.qemu-system",
        "tool.qemu-img",
        "tool.hdiutil",
        "tool.ssh",
        "tool.ssh-keygen",
        "firmware.uefi",
        "python.libvirt",
        "network.user-hostfwd",
    )
    expected: dict[tuple[RuntimeBackend, RuntimeOperation], tuple[str, ...]] = {}
    for backend, run_requirements in (
        (RuntimeBackend.KVM, kvm_run),
        (RuntimeBackend.LIBVIRT_HVF, hvf_run),
    ):
        expected[(backend, RuntimeOperation.RUN)] = run_requirements
        for operation in (
            RuntimeOperation.START,
            RuntimeOperation.STOP,
            RuntimeOperation.RM,
            RuntimeOperation.RECONCILE,
        ):
            expected[(backend, operation)] = ("python.libvirt",)
        expected[(backend, RuntimeOperation.INSPECT)] = ()
        expected[(backend, RuntimeOperation.LOGS)] = ()
        expected[(backend, RuntimeOperation.PS)] = ()
        expected[(backend, RuntimeOperation.EXEC)] = ("tool.ssh",)
        expected[(backend, RuntimeOperation.SHELL)] = ("tool.ssh",)
        expected[(backend, RuntimeOperation.COMMIT)] = ("python.libvirt", "tool.ssh", "tool.scp")
    expected[(RuntimeBackend.LIMA_VZ, RuntimeOperation.RUN)] = (
        "host.lima-vz",
        "tool.limactl",
        "network.lima",
    )
    for operation in (
        RuntimeOperation.START,
        RuntimeOperation.STOP,
        RuntimeOperation.RM,
        RuntimeOperation.RECONCILE,
    ):
        expected[(RuntimeBackend.LIMA_VZ, operation)] = ("host.lima-vz", "tool.limactl")
    expected[(RuntimeBackend.LIMA_VZ, RuntimeOperation.INSPECT)] = ()
    expected[(RuntimeBackend.LIMA_VZ, RuntimeOperation.LOGS)] = ()
    expected[(RuntimeBackend.LIMA_VZ, RuntimeOperation.PS)] = ()

    assert len(expected) == 30
    for (backend, operation), requirement_ids in expected.items():
        key = DispatchKey(RuntimeKind.CLOUD_IMAGE, backend)
        profile = platforms.capability_profile(key, operation)
        assert profile.dispatch_key == key
        assert profile.operation is operation
        assert tuple(item.capability_id for item in profile.requirements) == requirement_ids
        selectors = tuple(item.selector for item in profile.requirements)
        assert selectors == tuple("default" if item.startswith("network.") else None for item in requirement_ids)


@pytest.mark.parametrize("operation", [RuntimeOperation.EXEC, RuntimeOperation.SHELL])
def test_lima_process_profiles_refuse_before_any_probe(
    operation: RuntimeOperation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ)
    monkeypatch.setattr(platforms, "_check_capability", lambda *_args, **_kwargs: pytest.fail("probe reached"))

    with pytest.raises(RuntimeCapabilityError) as captured:
        platforms.capability_profile(key, operation)

    assert captured.value.operation is operation


def test_oci_root_unsupported_operation_precedes_every_cloud_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    key = DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)
    monkeypatch.setattr(platforms.shutil, "which", lambda _name: pytest.fail("cloud tool probe reached"))
    monkeypatch.setattr(Path, "exists", lambda _path: pytest.fail("KVM device probe reached"))

    with pytest.raises(RuntimeCapabilityError) as captured:
        platforms.capability_profile(key, RuntimeOperation.SHELL)

    assert captured.value.code == "runtime-operation-unavailable"
    assert captured.value.operation is RuntimeOperation.SHELL
    assert platforms.capability_profile(key, RuntimeOperation.PS).requirements == ()
    assert platforms.capability_profile(key, RuntimeOperation.EXEC).requirements == ()


def test_oci_root_capability_matrix_refuses_unimplemented_operations() -> None:
    key = DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM)

    for operation in set(RuntimeOperation) - {
        RuntimeOperation.RUN,
        RuntimeOperation.STOP,
        RuntimeOperation.RM,
        RuntimeOperation.PS,
        RuntimeOperation.EXEC,
    }:
        with pytest.raises(RuntimeCapabilityError) as captured:
            platforms.capability_profile(key, operation)
        assert captured.value.operation is operation


def test_operation_report_is_authenticated_and_consumed_exactly_once() -> None:
    host = platforms.HostPlatform("Linux", "x86_64")
    profile = platforms.capability_profile(
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        RuntimeOperation.LOGS,
    )
    subject = "sha256:" + "a" * 64
    report = platforms.evaluate_capability_profile(profile, subject_digest=subject, host=host, now_ns=100)
    forged = replace(report, authentication_tag="0" * 64)

    with pytest.raises(RuntimePreflightError) as forged_error:
        platforms.consume_capability_report(
            forged,
            expected_profile=profile,
            expected_subject_digest=subject,
            host=host,
            now_ns=100,
        )
    assert forged_error.value.category is CapabilityErrorCategory.REPORT_PROVENANCE

    platforms.consume_capability_report(
        report,
        expected_profile=profile,
        expected_subject_digest=subject,
        host=host,
        now_ns=100,
    )
    with pytest.raises(RuntimePreflightError) as reused_error:
        platforms.consume_capability_report(
            report,
            expected_profile=profile,
            expected_subject_digest=subject,
            host=host,
            now_ns=100,
        )
    assert reused_error.value.category is CapabilityErrorCategory.REPORT_CONSUMED


def test_discovery_report_cannot_authorize_an_operation() -> None:
    host = platforms.HostPlatform("Linux", "x86_64")
    profile = platforms.capability_profile(
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        RuntimeOperation.LOGS,
    )
    subject = "sha256:" + "b" * 64
    report = platforms.evaluate_capability_profile(
        profile,
        subject_digest=subject,
        host=host,
        now_ns=200,
        purpose=PreflightReportPurpose.DISCOVERY,
    )

    with pytest.raises(RuntimePreflightError) as captured:
        platforms.consume_capability_report(
            report,
            expected_profile=profile,
            expected_subject_digest=subject,
            host=host,
            now_ns=200,
        )

    assert captured.value.category is CapabilityErrorCategory.REPORT_PROVENANCE
    assert report.issuer_nonce not in platforms._ISSUED_OPERATION_REPORTS


def test_operation_report_registries_prune_expired_issued_and_consumed_nonces() -> None:
    host = platforms.HostPlatform("Linux", "x86_64")
    profile = platforms.capability_profile(
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        RuntimeOperation.LOGS,
    )
    subject = "sha256:" + "c" * 64
    expired_issued = platforms.evaluate_capability_profile(profile, subject_digest=subject, host=host, now_ns=0)
    current = platforms.evaluate_capability_profile(
        profile,
        subject_digest=subject,
        host=host,
        now_ns=expired_issued.expires_at_monotonic_ns,
    )

    assert expired_issued.issuer_nonce not in platforms._ISSUED_OPERATION_REPORTS
    platforms.consume_capability_report(
        current,
        expected_profile=profile,
        expected_subject_digest=subject,
        host=host,
        now_ns=current.issued_at_monotonic_ns,
    )
    assert current.issuer_nonce in platforms._CONSUMED_OPERATION_REPORTS

    platforms.evaluate_capability_profile(
        profile,
        subject_digest=subject,
        host=host,
        now_ns=current.expires_at_monotonic_ns,
    )
    assert current.issuer_nonce not in platforms._CONSUMED_OPERATION_REPORTS


def test_operation_report_registries_bound_outstanding_and_consumed_tokens_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = platforms.HostPlatform("Linux", "x86_64")
    profile = platforms.capability_profile(
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        RuntimeOperation.LOGS,
    )
    subject = "sha256:" + "e" * 64
    with platforms._REPORT_REGISTRY_LOCK:
        saved_issued = dict(platforms._ISSUED_OPERATION_REPORTS)
        saved_consumed = dict(platforms._CONSUMED_OPERATION_REPORTS)
        platforms._ISSUED_OPERATION_REPORTS.clear()
        platforms._CONSUMED_OPERATION_REPORTS.clear()
    monkeypatch.setattr(platforms, "_MAX_OPERATION_REPORT_REGISTRY_ENTRIES", 3)
    try:
        consumed = []
        for _ in range(10):
            report = platforms.evaluate_capability_profile(profile, subject_digest=subject, host=host, now_ns=0)
            platforms.consume_capability_report(
                report,
                expected_profile=profile,
                expected_subject_digest=subject,
                host=host,
                now_ns=0,
            )
            consumed.append(report)
        with platforms._REPORT_REGISTRY_LOCK:
            assert platforms._ISSUED_OPERATION_REPORTS == {}
            assert len(platforms._CONSUMED_OPERATION_REPORTS) == 3
        with pytest.raises(RuntimePreflightError) as evicted_replay:
            platforms.consume_capability_report(
                consumed[0],
                expected_profile=profile,
                expected_subject_digest=subject,
                host=host,
                now_ns=0,
            )
        assert evicted_replay.value.category is CapabilityErrorCategory.REPORT_PROVENANCE

        issued = [
            platforms.evaluate_capability_profile(profile, subject_digest=subject, host=host, now_ns=0)
            for _ in range(3)
        ]
        with pytest.raises(RuntimePreflightError) as captured:
            platforms.evaluate_capability_profile(profile, subject_digest=subject, host=host, now_ns=0)
        assert captured.value.category is CapabilityErrorCategory.REPORT_CAPACITY
        with platforms._REPORT_REGISTRY_LOCK:
            assert len(platforms._ISSUED_OPERATION_REPORTS) == 3
            assert len(platforms._CONSUMED_OPERATION_REPORTS) == 3

        recovered = platforms.evaluate_capability_profile(
            profile,
            subject_digest=subject,
            host=host,
            now_ns=issued[0].expires_at_monotonic_ns,
        )
        assert recovered.issuer_nonce in platforms._ISSUED_OPERATION_REPORTS
        with platforms._REPORT_REGISTRY_LOCK:
            assert len(platforms._ISSUED_OPERATION_REPORTS) == 1
            assert platforms._CONSUMED_OPERATION_REPORTS == {}
    finally:
        with platforms._REPORT_REGISTRY_LOCK:
            platforms._ISSUED_OPERATION_REPORTS.clear()
            platforms._ISSUED_OPERATION_REPORTS.update(saved_issued)
            platforms._CONSUMED_OPERATION_REPORTS.clear()
            platforms._CONSUMED_OPERATION_REPORTS.update(saved_consumed)


def test_run_report_network_selector_is_authenticated_and_must_match_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    host = platforms.HostPlatform("Linux", "x86_64")
    monkeypatch.setattr(
        platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "present", True),
    )
    issued_profile = platforms.capability_profile(key, RuntimeOperation.RUN, network="default")
    expected_profile = platforms.capability_profile(key, RuntimeOperation.RUN, network="private")
    subject = "sha256:" + "f" * 64
    report = platforms.evaluate_capability_profile(issued_profile, subject_digest=subject, host=host, now_ns=100)

    with pytest.raises(RuntimePreflightError) as captured:
        platforms.consume_capability_report(
            report,
            expected_profile=expected_profile,
            expected_subject_digest=subject,
            host=host,
            now_ns=100,
        )

    assert captured.value.category is CapabilityErrorCategory.REPORT_MISMATCH
    assert issued_profile != expected_profile


def test_kvm_network_none_profile_omits_external_network_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    profile = platforms.capability_profile(key, RuntimeOperation.RUN, network="none")
    probed: list[str] = []
    monkeypatch.setattr(
        platforms,
        "_check_capability",
        lambda requirement, **_kwargs: (
            probed.append(requirement.capability_id) or CapabilityCheck(requirement.capability_id, "present", True)
        ),
    )

    platforms.evaluate_capability_profile(
        profile,
        subject_digest="sha256:" + "1" * 64,
        host=platforms.HostPlatform("Linux", "x86_64"),
        purpose=PreflightReportPurpose.DISCOVERY,
    )

    assert "network.libvirt" not in probed


@pytest.mark.parametrize("selector", ["none", "default", "adapter-ignored-selector"])
def test_hvf_user_network_selector_matches_adapter_no_lookup_semantics(
    selector: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        platforms,
        "resolve_domain_profile",
        lambda *_args, **_kwargs: pytest.fail("domain/network resolution reached"),
    )
    monkeypatch.setattr(kvm_module, "connect", lambda *_args, **_kwargs: pytest.fail("network lookup reached"))

    check = platforms._check_capability(
        CapabilityRequirement("network.user-hostfwd", selector),
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
        host=platforms.HostPlatform("Darwin", "aarch64"),
        arch="aarch64",
    )

    assert check.passed is True
    assert check.observed == "built-in"


def test_kvm_device_probe_requires_rw_access_and_kvm_api_v12(monkeypatch: pytest.MonkeyPatch) -> None:
    requirement = CapabilityRequirement("host.kvm-device")
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    closed: list[int] = []
    monkeypatch.setattr(Path, "exists", lambda _path: True)
    monkeypatch.setattr(platforms.os, "access", lambda *_args: True)
    monkeypatch.setattr(platforms.os, "open", lambda *_args: 17)
    monkeypatch.setattr(platforms.os, "close", closed.append)
    monkeypatch.setattr(platforms.fcntl, "ioctl", lambda descriptor, request: 12)

    check = platforms._check_capability(
        requirement,
        dispatch_key=key,
        host=platforms.HostPlatform("Linux", "x86_64"),
        arch="x86_64",
    )

    assert check.passed is True
    assert check.observed == "api-v12"
    assert closed == [17]


def test_qemu_probe_uses_exact_domain_profile_emulator_not_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emulator = tmp_path / "qemu-system-x86_64"
    emulator.write_bytes(b"binary")
    emulator.chmod(0o700)
    domain = platforms.DomainProfile(
        backend="kvm",
        domain_type="kvm",
        arch="x86_64",
        machine="q35",
        emulator=emulator,
        uri="qemu:///system",
        firmware=None,
        autoselect_firmware=False,
        network_mode="libvirt-network",
        seed_tool="cloud-localds",
        seed_bus="sata",
    )
    monkeypatch.setattr(platforms, "resolve_domain_profile", lambda *_args, **_kwargs: domain)
    monkeypatch.setattr(platforms.shutil, "which", lambda _tool: pytest.fail("PATH lookup reached"))

    check = platforms._check_capability(
        CapabilityRequirement("tool.qemu-system"),
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        host=platforms.HostPlatform("Linux", "x86_64"),
        arch="x86_64",
    )

    assert check.passed is True
    assert check.observed == "configured-executable"


@pytest.mark.parametrize("version", ["2.1.0", "2.9.9"])
def test_lima_tool_probe_accepts_exact_supported_adapter_versions(
    version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platforms.shutil, "which", lambda _tool: "/opt/bin/limactl")
    monkeypatch.setattr(
        platforms.lima,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["limactl", "--version"],
            0,
            f"limactl version {version}\n",
            "",
        ),
    )

    check = platforms._check_capability(
        CapabilityRequirement("tool.limactl"),
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        host=platforms.HostPlatform("Darwin", "aarch64"),
        arch="aarch64",
    )

    assert check.passed is True
    assert check.observed == "supported-2.x"


@pytest.mark.parametrize("version", ["2.0.9", "3.0.0"])
def test_lima_tool_probe_rejects_versions_the_adapter_rejects(
    version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platforms.shutil, "which", lambda _tool: "/opt/bin/limactl")
    monkeypatch.setattr(
        platforms.lima,
        "_run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["limactl", "--version"],
            0,
            f"limactl version {version}\n",
            "",
        ),
    )

    check = platforms._check_capability(
        CapabilityRequirement("tool.limactl"),
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        host=platforms.HostPlatform("Darwin", "aarch64"),
        arch="aarch64",
    )

    assert check.passed is False
    assert check.error_category is CapabilityErrorCategory.UNSUPPORTED
    assert check.observed == "unsupported-version"


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess(["limactl", "--version"], 0, "not a version", ""),
        subprocess.CompletedProcess(["limactl", "--version"], 7, "", "ATTACKER-SECRET"),
    ],
)
def test_lima_tool_probe_normalizes_malformed_or_failed_version_commands(
    result: subprocess.CompletedProcess[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platforms.shutil, "which", lambda _tool: "/opt/bin/limactl")
    monkeypatch.setattr(platforms.lima, "_run_command", lambda *_args, **_kwargs: result)

    check = platforms._check_capability(
        CapabilityRequirement("tool.limactl"),
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        host=platforms.HostPlatform("Darwin", "aarch64"),
        arch="aarch64",
    )

    assert check.passed is False
    assert check.error_category is CapabilityErrorCategory.CHECK_FAILED
    assert check.observed == "version-check-failed"
    assert "ATTACKER-SECRET" not in (check.remediation or "")


# --- allocate_local_port ---------------------------------------------------------------


def test_allocate_local_port_returns_a_reusable_ephemeral_port():
    port = platforms.allocate_local_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_allocate_local_port_can_be_called_repeatedly_without_collision():
    ports = {platforms.allocate_local_port() for _ in range(4)}
    assert all(1 <= port <= 65535 for port in ports)
