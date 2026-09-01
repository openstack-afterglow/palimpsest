"""Pure policy tests for the native-KVM stage-1 qualification harness."""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    _REQUIRED_KERNEL_CONFIG,
    EVIDENCE_FILE_NAMES,
    NEGATIVE_CONTROL_NAMES,
    PREPARATION_FAILURE_MARKER,
    REJECTION_MARKER,
    SUCCESS_MARKER,
    KVMProofFailure,
    KVMProofUnavailable,
    OCIStage1KVMProofReceipt,
    _logical_line_count,
    _read_console_until,
    _secure_write,
    build_kernel_cmdline,
    build_negative_qemu_command,
    build_proof_plan,
    build_qemu_command,
    negative_control_contract,
    pre_mount_topology,
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


def _negative_consoles() -> dict[str, bytes]:
    return {name: b"kernel boot\n" + REJECTION_MARKER + b"\n" for name in NEGATIVE_CONTROL_NAMES}


def _receipt() -> OCIStage1KVMProofReceipt:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    initramfs = build_bootstrap_initramfs()
    return OCIStage1KVMProofReceipt(
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
        b"kernel boot\r\n" + SUCCESS_MARKER + b"\r\n",
        _negative_consoles(),
    )


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
        root_path=(tmp_path / "root").resolve(),
        lower_paths=tuple((tmp_path / f"lower-{index}").resolve() for index in range(len(plan.layers))),
        plan=plan,
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
    devices = tuple(command[index + 1] for index, item in enumerate(command) if item == "-device")
    assert devices == (
        f"virtio-blk-pci,drive=lower0,serial={plan.layers[0]['serial']}",
        f"virtio-blk-pci,drive=stage1,serial={transport_serial(transport.receipt.artifact_digest)}",
        f"virtio-blk-pci,drive=root,serial={plan.root['serial']}",
        f"virtio-blk-pci,drive=lower1,serial={plan.layers[1]['serial']}",
    )


@pytest.mark.parametrize("control_name", NEGATIVE_CONTROL_NAMES)
def test_each_negative_control_has_exact_path_free_contract_and_qemu_mutation(
    tmp_path: Path,
    control_name: str,
) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    contract = negative_control_contract(control_name)
    backing_paths = {name: (tmp_path / name).resolve() for name in contract["backings"]}
    command = build_negative_qemu_command(
        qemu_path=(tmp_path / "qemu").resolve(),
        kernel_path=(tmp_path / "kernel").resolve(),
        initramfs_path=(tmp_path / "initrd").resolve(),
        backing_paths=backing_paths,
        cmdline=build_kernel_cmdline(plan, transport),
        control=contract,
    )

    assert set(contract) == {"attachments", "backings", "digest", "name", "policy"}
    assert contract["name"] == control_name
    assert all("/" not in backing for backing in contract["backings"])
    drives = tuple(command[index + 1] for index, item in enumerate(command) if item == "-drive")
    devices = tuple(command[index + 1] for index, item in enumerate(command) if item == "-device")
    assert len(drives) == len(devices) == len(contract["attachments"])
    for attachment, drive, device in zip(contract["attachments"], drives, devices, strict=True):
        assert f"file={backing_paths[attachment['backing']]}" in drive
        assert f"readonly={'on' if attachment['read_only'] else 'off'}" in drive
        assert device == (f"virtio-blk-pci,drive={attachment['drive_id']},serial={attachment['serial']}")
    assert command[command.index("-accel") + 1] == "kvm"
    assert "accel=tcg" not in " ".join(command)


def test_negative_controls_cover_every_required_topology_mutation() -> None:
    contracts = {name: negative_control_contract(name) for name in NEGATIVE_CONTROL_NAMES}
    plan = build_proof_plan()
    by_name = {
        name: {(item["role"], item["ordinal"]): item for item in value["attachments"]}
        for name, value in contracts.items()
    }

    assert ("root", None) not in by_name["missing_root"]
    assert by_name["wrong_root_serial"][("root", None)]["serial"] != plan.root["serial"]
    assert by_name["readonly_root"][("root", None)]["read_only"] is True
    assert contracts["root_size_smaller"]["backings"]["root"]["size_bytes"] == plan.root["size_bytes"] - 512
    assert contracts["root_size_larger"]["backings"]["root"]["size_bytes"] == plan.root["size_bytes"] + 512
    assert ("lower", 0) not in by_name["missing_lower"]
    assert by_name["wrong_lower_serial"][("lower", 0)]["serial"] != plan.layers[0]["serial"]
    assert by_name["writable_lower"][("lower", 0)]["read_only"] is False
    assert contracts["lower_size_smaller"]["backings"]["lower0"]["size_bytes"] == 3584
    assert contracts["lower_size_larger"]["backings"]["lower0"]["size_bytes"] == 4608
    assert len(contracts["duplicate_serial"]["attachments"]) == 4
    assert len({item["serial"] for item in contracts["duplicate_serial"]["attachments"]}) == 3
    assert len(contracts["extra_disk"]["attachments"]) == 5
    assert by_name["writable_transport"][("transport", None)]["read_only"] is False


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
    receipt = _receipt()
    console = receipt.console
    negative_consoles = receipt.negative_consoles
    transport = build_stage1_transport(build_proof_plan())

    decoded = json.loads(receipt.canonical_bytes)
    assert (
        OCIStage1KVMProofReceipt.from_dict(
            decoded,
            console=console,
            negative_consoles=negative_consoles,
        )
        == receipt
    )
    assert decoded["qemu"]["artifact_digest"] == "sha256:" + "3" * 64
    assert decoded["root_assembly"] is False
    assert decoded["pre_mount_devices"] is True
    assert decoded["filesystem_verified"] is False
    assert decoded["content_verified"] is False
    assert decoded["mount_attempted"] is False
    assert decoded["topology"] == pre_mount_topology(build_proof_plan())
    assert tuple(decoded["negative_controls"]) == tuple(sorted(NEGATIVE_CONTROL_NAMES))
    for name in NEGATIVE_CONTROL_NAMES:
        assert decoded["negative_controls"][name]["contract"] == negative_control_contract(name)
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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["topology"]["devices"][0].__setitem__("read_only", True),
        lambda value: value["topology"]["devices"][1].__setitem__("read_only", False),
        lambda value: value["topology"]["devices"][1].__setitem__("size_bytes", 512),
        lambda value: value["topology"]["devices"].pop(),
        lambda value: value["topology"]["devices"][1].__setitem__("serial", "f" * 20),
        lambda value: value["topology"]["devices"][1].__setitem__("serial", value["topology"]["devices"][0]["serial"]),
        lambda value: value["topology"]["devices"].append(dict(value["topology"]["devices"][-1])),
    ],
)
def test_receipt_rejects_readonly_size_missing_wrong_duplicate_and_extra_topology(mutate: object) -> None:
    plan = build_proof_plan()
    receipt = _receipt()
    console = receipt.console
    negative_consoles = receipt.negative_consoles
    changed = copy.deepcopy(receipt.to_dict())
    mutate(changed)  # type: ignore[operator]

    with pytest.raises(ArtifactValidationError, match="policy|canonical"):
        OCIStage1KVMProofReceipt.from_dict(
            changed,
            console=console,
            negative_consoles=negative_consoles,
        )
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(
            receipt,
            cmdline=receipt.cmdline.replace(
                plan.boot_plan_digest,
                "sha256:" + "e" * 64,
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "contract", "console_digest", "marker_count", "preparation_count", "liveness"],
)
def test_receipt_rejects_negative_control_mapping_tamper(mutation: str) -> None:
    receipt = _receipt()
    value = copy.deepcopy(receipt.to_dict())
    consoles = dict(receipt.negative_consoles)
    if mutation == "missing":
        del value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]]
    elif mutation == "extra":
        value["negative_controls"]["unexpected"] = copy.deepcopy(value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]])
    elif mutation == "contract":
        value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]]["contract"]["name"] = "missing_root"
    elif mutation == "console_digest":
        value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]]["console_digest"] = "sha256:" + "f" * 64
    elif mutation == "marker_count":
        value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]]["rejection_marker_count"] = 2
    elif mutation == "preparation_count":
        value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]]["preparation_failure_marker_count"] = 1
    else:
        value["negative_controls"][NEGATIVE_CONTROL_NAMES[0]]["pid1_alive_after_marker"] = False

    with pytest.raises(ArtifactValidationError, match="policy|canonical"):
        OCIStage1KVMProofReceipt.from_dict(value, console=receipt.console, negative_consoles=consoles)


@pytest.mark.parametrize("marker", [REJECTION_MARKER, SUCCESS_MARKER, PREPARATION_FAILURE_MARKER])
def test_receipt_requires_exactly_one_rejection_and_no_other_marker(marker: bytes) -> None:
    receipt = _receipt()
    consoles = dict(receipt.negative_consoles)
    name = NEGATIVE_CONTROL_NAMES[0]
    if marker == REJECTION_MARKER:
        consoles[name] += REJECTION_MARKER + b"\n"
    else:
        consoles[name] += marker + b"\n"
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, negative_consoles=consoles)


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


@pytest.mark.parametrize(
    "reserved_name",
    EVIDENCE_FILE_NAMES,
)
def test_evidence_directory_rejects_every_reserved_output_name(tmp_path: Path, reserved_name: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    (evidence / reserved_name).write_bytes(b"occupied")
    with pytest.raises(ArtifactValidationError, match="already exists"):
        verify_evidence_directory(evidence.resolve())


def test_evidence_names_and_owner_only_publication_are_exact(tmp_path: Path) -> None:
    assert EVIDENCE_FILE_NAMES == (
        "console.bin",
        "receipt.json",
        *(f"negative-{name}.bin" for name in NEGATIVE_CONTROL_NAMES),
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    output = _secure_write(evidence.resolve(), EVIDENCE_FILE_NAMES[2], b"console", mode=0o400)
    assert output.read_bytes() == b"console"
    assert output.stat().st_mode & 0o777 == 0o400
    with pytest.raises(ArtifactValidationError, match="cannot be published"):
        _secure_write(evidence.resolve(), EVIDENCE_FILE_NAMES[2], b"replacement", mode=0o400)
