"""Opt-in native PID 1 diagnostic for an independent console open description."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
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

from .test_oci_docker_hub_cli_live import _bounded_command, _save, _success
from .test_oci_stdio_cli_live import _TOOLCHAIN

_ENABLE = "PALIMPSEST_OCI_CONSOLE_OFD_LIVE"
_MARKERS = (
    b"PALIMPSEST_CONSOLE_OFD_V1 ORIGINAL_WRITE",
    b"PALIMPSEST_CONSOLE_OFD_V1 REOPEN_WRITE",
    b"PALIMPSEST_CONSOLE_OFD_V1 PASS",
)


def _completed_probe_lines(payload: bytes) -> tuple[bytes, ...]:
    if len(payload) > 1024 * 1024:
        raise ValueError("console diagnostic exceeded its byte limit")
    return tuple(line.rstrip(b"\r") for line in payload.split(b"\n")[:-1])


def _finalize_probe(process, selector, evidence: Path, data: bytearray) -> BaseException | None:
    errors = []
    for operation in (selector.close,):
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)
    if process is not None:
        try:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
        except BaseException as exc:
            errors.append(exc)
        try:
            if process.stdout is not None:
                process.stdout.close()
        except BaseException as exc:
            errors.append(exc)
    try:
        (evidence / "console.bin").write_bytes(data)
    except BaseException as exc:
        errors.append(exc)
    return errors[0] if errors else None


def _compile(root: Path) -> Path:
    # Reuse the pinned offline compiler policy, substituting only this test source.
    source = Path(__file__).with_name("assets") / "console-ofd-probe.c"
    assert source.is_file()
    # Keep compilation explicit rather than mutating product artifacts.
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--pids-limit",
        "16",
        "--memory",
        "128m",
        "--cpus",
        "0.25",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=bind,src={source},dst=/src/probe.c,readonly",
        "--mount",
        f"type=bind,src={root},dst=/out",
        "--entrypoint",
        "/usr/bin/timeout",
        _TOOLCHAIN,
        "--kill-after=3",
        "55",
        "/usr/local/bin/gcc",
        "-std=c11",
        "-Os",
        "-nostdlib",
        "-static",
        "-fno-builtin",
        "-fno-ident",
        "-fno-stack-protector",
        "-fno-unwind-tables",
        "-fno-pie",
        "-no-pie",
        "-ffreestanding",
        "-mno-red-zone",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wl,--build-id=none,-z,noexecstack,-s",
        "-o",
        "/out/init",
        "/src/probe.c",
    ]
    completed = _bounded_command(command, environment=dict(os.environ), timeout=60)
    _save(root, "compile", completed)
    _success(completed)
    output = root / "init"
    assert output.is_file() and stat.S_IMODE(output.stat().st_mode) & 0o111
    return output


@pytest.mark.skipif(os.environ.get(_ENABLE) != "1", reason=f"set {_ENABLE}=1 for native diagnostic")
def test_pid1_console_procfd_reopen_has_independent_nonblocking_ofd() -> None:
    try:
        verify_kernel_configuration_selection()
        verify_kvm_api()
        kernel = select_linux_bzimage()
        config = select_kernel_config()
        qemu = select_qemu()
        version = qemu_version(qemu)
    except KVMProofUnavailable as exc:
        pytest.fail(str(exc))
    evidence = Path(tempfile.mkdtemp(prefix="palimpsest-console-ofd-"))
    evidence.chmod(0o700)
    print(f"console OFD diagnostic evidence: {evidence}")
    init = _compile(evidence)
    archive = build_newc(
        [
            NewcEntry("dev", stat.S_IFDIR | 0o755, b""),
            NewcEntry("init", stat.S_IFREG | 0o755, init.read_bytes()),
            NewcEntry("proc", stat.S_IFDIR | 0o755, b""),
            NewcEntry("sys", stat.S_IFDIR | 0o755, b""),
        ]
    )
    kernel_path = _secure_write(evidence, "kernel", kernel.payload, mode=0o400)
    initrd = _secure_write(evidence, "initramfs.cpio", archive, mode=0o400)
    qemu_path = _secure_write(evidence, "qemu-system-x86_64", qemu.payload, mode=0o500)
    receipt = {
        "schema": "palimpsest.console-ofd-native-diagnostic.v1",
        "kernel_sha256": hashlib.sha256(kernel.payload).hexdigest(),
        "kernel_config_sha256": hashlib.sha256(config.payload).hexdigest(),
        "qemu_sha256": hashlib.sha256(qemu.payload).hexdigest(),
        "qemu_version": version.decode("utf-8"),
        "initramfs_sha256": hashlib.sha256(archive).hexdigest(),
        "memory_mib": 128,
        "vcpus": 1,
        "network": "none",
    }
    (evidence / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    command = [
        os.fspath(qemu_path),
        "-nodefaults",
        "-no-reboot",
        "-display",
        "none",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-m",
        "128",
        "-smp",
        "1",
        "-kernel",
        os.fspath(kernel_path),
        "-initrd",
        os.fspath(initrd),
        "-append",
        "console=ttyS0,115200n8 rdinit=/init panic=-1",
        "-serial",
        "stdio",
        "-net",
        "none",
    ]
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    data = bytearray()
    lines: tuple[bytes, ...] = ()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
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
            if (
                b"PALIMPSEST_CONSOLE_OFD_V1 PASS" in lines
                or any(line.startswith(b"PALIMPSEST_CONSOLE_OFD_V1 FAIL ") for line in lines)
                or process.poll() is not None
            ):
                break
        assert not any(line.startswith(b"PALIMPSEST_CONSOLE_OFD_V1 FAIL ") for line in lines)
        assert all(lines.count(marker) == 1 for marker in _MARKERS), f"evidence retained at {evidence}"
    finally:
        final_error = _finalize_probe(process, selector, evidence, data)
        if final_error is not None and sys.exc_info()[0] is None:
            raise final_error
