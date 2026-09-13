"""Opt-in actual-PID1 KVM proof for covering a populated image ``/dev``."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    KVMProofUnavailable,
    _secure_write,
    qemu_version,
    select_kernel_config,
    select_linux_bzimage,
    select_qemu,
    verify_kernel_configuration_selection,
    verify_kvm_api,
)
from palimpsest_local.oci_initramfs import NewcEntry, build_newc

from .test_oci_console_ofd_live import _completed_probe_lines, _finalize_probe
from .test_oci_docker_hub_cli_live import _bounded_command, _save, _success
from .test_oci_stdio_cli_live import _TOOLCHAIN

_ENABLE = "PALIMPSEST_OCI_DEV_COVER_LIVE"
_PREFIX = b"PALIMPSEST_DEV_COVER_V1 "


def _build_probe_initramfs(init_payload: bytes) -> bytes:
    return build_newc([
        NewcEntry("dev", stat.S_IFDIR | 0o755, b""),
        NewcEntry("init", stat.S_IFREG | 0o755, init_payload),
        NewcEntry("proc", stat.S_IFDIR | 0o755, b""),
        NewcEntry("trusted", stat.S_IFDIR | 0o755, b""),
    ])


def _compile(root: Path) -> Path:
    source = Path(__file__).with_name("assets") / "dev-cover-probe.c"
    command = [
        "docker", "run", "--rm", "--pull=never", "--platform", "linux/amd64",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
        "--pids-limit", "16", "--memory", "128m", "--cpus", "0.25",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,src={Path(__file__).resolve().parents[2]},dst=/repo,readonly",
        "--mount", f"type=bind,src={source},dst=/src/probe.c,readonly",
        "--mount", f"type=bind,src={root},dst=/out",
        "--entrypoint", "/usr/bin/timeout", _TOOLCHAIN, "--kill-after=3", "55",
        "/usr/local/bin/gcc", "-std=c11", "-Os", "-nostdlib", "-static", "-fno-builtin",
        "-fno-ident", "-fno-stack-protector", "-fno-unwind-tables", "-fno-pie", "-no-pie",
        "-ffreestanding", "-mno-red-zone", "-Wall", "-Wextra", "-Werror",
        "-Wl,--build-id=none,-z,noexecstack,-s,-e,probe_start", "-o", "/out/init", "/src/probe.c",
    ]
    completed = _bounded_command(command, environment=dict(os.environ), timeout=60)
    _save(root, "compile", completed)
    _success(completed)
    output = root / "init"
    assert output.is_file() and stat.S_IMODE(output.stat().st_mode) & 0o111
    return output


def _boot(command: list[str], evidence: Path, name: str, terminal: bytes) -> tuple[bytes, ...]:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    data = bytearray()
    lines: tuple[bytes, ...] = ()
    boot_evidence = evidence / name
    boot_evidence.mkdir(mode=0o700)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fd, 65536)
                if chunk:
                    data.extend(chunk)
            assert len(data) <= 1024 * 1024
            lines = _completed_probe_lines(bytes(data))
            if terminal in lines or _PREFIX + b"FAIL" in lines or process.poll() is not None:
                break
        assert terminal in lines, f"evidence retained at {evidence}"
        assert _PREFIX + b"FAIL" not in lines, f"evidence retained at {evidence}"
        return lines
    finally:
        error = _finalize_probe(process, selector, boot_evidence, data)
        if error is not None and sys.exc_info()[0] is None:
            raise error


@pytest.mark.kvm
@pytest.mark.stage1_kvm
@pytest.mark.skipif(os.environ.get(_ENABLE) != "1", reason=f"set {_ENABLE}=1 for native proof")
def test_populated_image_dev_is_covered_by_trusted_devtmpfs_then_private_child_tmpfs() -> None:
    try:
        verify_kernel_configuration_selection()
        verify_kvm_api()
        kernel, config, qemu = select_linux_bzimage(), select_kernel_config(), select_qemu()
        version = qemu_version(qemu)
    except KVMProofUnavailable as exc:
        pytest.fail(str(exc))
    evidence = Path(tempfile.mkdtemp(prefix="palimpsest-dev-cover-"))
    evidence.chmod(0o700)
    print(f"dev cover proof evidence: {evidence}")
    init = _compile(evidence)
    archive = _build_probe_initramfs(init.read_bytes())
    kernel_path = _secure_write(evidence, "kernel", kernel.payload, mode=0o400)
    initrd = _secure_write(evidence, "initramfs.cpio", archive, mode=0o400)
    qemu_path = _secure_write(evidence, "qemu-system-x86_64", qemu.payload, mode=0o500)
    probe_sha256 = hashlib.sha256(
        (Path(__file__).with_name("assets") / "dev-cover-probe.c").read_bytes()
    ).hexdigest()
    receipt = {
        "schema": "palimpsest.guest-dev-cover-proof.v1",
        "kernel_sha256": hashlib.sha256(kernel.payload).hexdigest(),
        "kernel_config_sha256": hashlib.sha256(config.payload).hexdigest(),
        "qemu_sha256": hashlib.sha256(qemu.payload).hexdigest(),
        "qemu_version": version.decode(), "probe_source_sha256": probe_sha256,
        "planned_boots": 2, "executed_boots": 0, "result": "prepared",
        "memory_mib": 128, "vcpus": 1, "network": "none",
    }
    receipt_path = evidence / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    base = [os.fspath(qemu_path), "-nodefaults", "-no-reboot", "-display", "none",
            "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "128", "-smp", "1",
            "-kernel", os.fspath(kernel_path), "-initrd", os.fspath(initrd),
            "-serial", "stdio", "-net", "none"]
    success = _boot(base + ["-append", "console=ttyS0,115200n8 rdinit=/init panic=-1"],
                    evidence, "success-console.bin", _PREFIX + b"PASS")
    receipt.update(executed_boots=1, result="success-boot-complete")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    assert success.count(_PREFIX + b"TRUSTED_DEVTMPFS") == 1
    assert success.count(_PREFIX + b"CHILD_TMPFS_6_PLUS_2") == 1
    assert success.count(_PREFIX + b"PARENT_DEVTMPFS_UNCHANGED") == 1
    rejected = _boot(base + ["-append", "console=ttyS0,115200n8 rdinit=/init panic=-1 palimpsest.dev_cover_fail=1"],
                     evidence, "rejected-console.bin", _PREFIX + b"REJECT")
    receipt.update(executed_boots=2, result="boots-complete-unqualified")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    assert _PREFIX + b"TRUSTED_DEVTMPFS" not in rejected
    assert _PREFIX + b"CHILD_TMPFS_6_PLUS_2" not in rejected
    assert _PREFIX + b"PASS" not in rejected
    receipt["result"] = "passed"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
