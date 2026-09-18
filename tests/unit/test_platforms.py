"""Unit tests for palimpsest_local.platforms."""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

import pytest

import palimpsest_local.kvm as kvm_module
import palimpsest_local.platforms as platforms
from palimpsest_local.errors import ArtifactValidationError, LifecycleError


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
