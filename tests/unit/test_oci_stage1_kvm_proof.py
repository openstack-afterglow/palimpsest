"""Pure policy tests for the native-KVM stage-1 qualification harness."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    _REQUIRED_KERNEL_CONFIG,
    PREPARATION_FAILURE_MARKER,
    REJECTION_MARKER,
    SUCCESS_MARKER,
    KVMProofFailure,
    KVMProofUnavailable,
    OCIStage1KVMProofReceipt,
    _logical_line_count,
    _read_console_until,
    build_kernel_cmdline,
    build_proof_plan,
    build_qemu_command,
    transport_serial,
    verify_evidence_directory,
    verify_kernel_config,
    verify_kernel_configuration_selection,
    verify_linux_bzimage,
)
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_initramfs import build_bootstrap_initramfs
from palimpsest_local.oci_stage1_transport import build_stage1_transport


def _write(path: Path, payload: bytes, mode: int = 0o644) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path.resolve()


def test_kernel_and_config_are_secure_bounded_built_in_fixtures(tmp_path: Path) -> None:
    kernel = bytearray(0x206)
    kernel[0x202:0x206] = b"HdrS"
    kernel_path = _write(tmp_path / "bzImage", bytes(kernel))
    config = "".join(f"{key}=y\n" for key in _REQUIRED_KERNEL_CONFIG).encode("ascii")
    config_path = _write(tmp_path / "config", config)

    assert verify_linux_bzimage(kernel_path).size_bytes == len(kernel)
    assert verify_kernel_config(config_path).payload == config

    config_path.chmod(0o666)
    with pytest.raises(ArtifactValidationError, match="metadata"):
        verify_kernel_config(config_path)
    config_path.chmod(0o644)
    link = tmp_path / "config-link"
    link.symlink_to(config_path)
    with pytest.raises(ArtifactValidationError, match="opened safely"):
        verify_kernel_config(link.absolute())


def test_kernel_config_rejects_modules_missing_and_duplicate_keys(tmp_path: Path) -> None:
    complete = {key: "y" for key in _REQUIRED_KERNEL_CONFIG}
    for key, replacement in (("CONFIG_VIRTIO_BLK", "m"), ("CONFIG_PCI", None)):
        changed = dict(complete)
        if replacement is None:
            del changed[key]
        else:
            changed[key] = replacement
        path = _write(tmp_path / f"{key}.config", "".join(f"{k}={v}\n" for k, v in changed.items()).encode())
        with pytest.raises(ArtifactValidationError, match="missing built-ins"):
            verify_kernel_config(path)

    duplicate = "".join(f"{key}=y\n" for key in _REQUIRED_KERNEL_CONFIG) + "CONFIG_VIRTIO=y\n"
    with pytest.raises(ArtifactValidationError, match="duplicate"):
        verify_kernel_config(_write(tmp_path / "duplicate.config", duplicate.encode()))


def test_explicit_kernel_and_config_must_be_selected_as_a_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALIMPSEST_KVM_KERNEL", "/fixtures/bzImage")
    monkeypatch.delenv("PALIMPSEST_KVM_KERNEL_CONFIG", raising=False)
    with pytest.raises(KVMProofUnavailable, match="must be configured together"):
        verify_kernel_configuration_selection()

    monkeypatch.setenv("PALIMPSEST_KVM_KERNEL_CONFIG", "/fixtures/config")
    verify_kernel_configuration_selection()


def test_qemu_command_is_explicit_native_kvm_readonly_and_networkless(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    cmdline = build_kernel_cmdline(plan, transport)
    paths = [
        path.resolve() for path in (tmp_path / "qemu", tmp_path / "kernel", tmp_path / "initrd", tmp_path / "plan")
    ]
    command = build_qemu_command(
        qemu_path=paths[0],
        kernel_path=paths[1],
        initramfs_path=paths[2],
        transport_path=paths[3],
        cmdline=cmdline,
        serial=transport_serial(transport.receipt.artifact_digest),
    )

    assert command[command.index("-accel") + 1] == "kvm"
    assert command[command.index("-cpu") + 1] == "host"
    assert command[command.index("-display") + 1] == "none"
    assert command[command.index("-nic") + 1] == "none"
    assert command[command.index("-serial") + 1] == "stdio"
    assert "-no-user-config" in command
    assert "readonly=on" in command[command.index("-drive") + 1]
    assert "accel=tcg" not in " ".join(command)
    assert f"palimpsest.stage1={transport.receipt.artifact_digest}" in cmdline


def test_console_reader_requires_one_marker_and_a_live_process() -> None:
    terminated_marker = SUCCESS_MARKER + b"\n"
    program = f"import sys,time;sys.stdout.buffer.write({terminated_marker!r});sys.stdout.flush();time.sleep(10)"
    console = _read_console_until(
        (sys.executable, "-c", program),
        expected=SUCCESS_MARKER,
        forbidden=(REJECTION_MARKER, PREPARATION_FAILURE_MARKER),
        timeout_seconds=3,
        require_alive_after_marker=True,
    )
    assert _logical_line_count(console, SUCCESS_MARKER) == 1


def test_console_reader_rejects_marker_embedded_in_a_forged_line() -> None:
    forged = b"FORGED:" + SUCCESS_MARKER + b":NOT-A-LINE\r\n"
    assert _logical_line_count(forged, SUCCESS_MARKER) == 0
    assert _logical_line_count(SUCCESS_MARKER + b"\r\n", SUCCESS_MARKER) == 1
    program = f"import sys;sys.stdout.buffer.write({forged!r});sys.stdout.flush()"
    with pytest.raises(KVMProofFailure, match="exited before the proof marker"):
        _read_console_until(
            (sys.executable, "-c", program),
            expected=SUCCESS_MARKER,
            forbidden=(REJECTION_MARKER,),
            timeout_seconds=3,
            require_alive_after_marker=True,
        )


def test_proof_receipt_round_trips_all_executed_artifact_bindings() -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    initramfs = build_bootstrap_initramfs()
    console = b"kernel boot\r\n" + SUCCESS_MARKER + b"\r\n"
    writable_console = b"kernel boot\n" + REJECTION_MARKER + b"\n"
    receipt = OCIStage1KVMProofReceipt(
        "sha256:" + "1" * 64,
        4096,
        "sha256:" + "2" * 64,
        initramfs.manifest.artifact_digest,
        initramfs.manifest.artifact_size_bytes,
        initramfs.manifest.digest,
        initramfs.manifest.stage1_binary_digest,
        transport.receipt.to_dict(),
        transport_serial(transport.receipt.artifact_digest),
        build_kernel_cmdline(plan, transport),
        "sha256:" + "3" * 64,
        8192,
        b"QEMU emulator version 9.2.0\n",
        console,
        writable_console,
    )

    decoded = json.loads(receipt.canonical_bytes)
    assert (
        OCIStage1KVMProofReceipt.from_dict(
            decoded,
            console=console,
            writable_console=writable_console,
        )
        == receipt
    )
    assert decoded["qemu"]["artifact_digest"] == "sha256:" + "3" * 64
    assert decoded["root_assembly"] is False
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, transport_serial="f" * 20)
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(
            receipt,
            cmdline=receipt.cmdline.replace(
                transport.receipt.artifact_digest,
                "sha256:" + "f" * 64,
            ),
        )
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(
            receipt,
            cmdline=receipt.cmdline.replace(
                plan.boot_plan_digest,
                "sha256:" + "e" * 64,
            ),
        )


def test_empty_owner_only_evidence_directory_is_required(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    assert verify_evidence_directory(evidence.resolve()) == evidence.resolve()

    (evidence / "receipt.json").write_text(json.dumps({}), encoding="ascii")
    with pytest.raises(ArtifactValidationError, match="already exists"):
        verify_evidence_directory(evidence.resolve())
    (evidence / "receipt.json").unlink()
    os.chmod(evidence, 0o755)
    with pytest.raises(ArtifactValidationError, match="metadata"):
        verify_evidence_directory(evidence.resolve())
