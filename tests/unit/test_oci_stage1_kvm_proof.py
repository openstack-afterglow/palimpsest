"""Pure policy tests for the native-KVM stage-1 qualification harness."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    _REQUIRED_KERNEL_CONFIG,
    ASSEMBLY_NEGATIVE_CONTROL_NAMES,
    ASSEMBLY_REJECTION_MARKER,
    EVIDENCE_FILE_NAMES,
    FILESYSTEM_NEGATIVE_CONTROL_NAMES,
    FILESYSTEM_REJECTION_MARKER,
    LIFECYCLE_REJECTION_PREFIX,
    NEGATIVE_CONTROL_NAMES,
    PREPARATION_FAILURE_MARKER,
    REJECTION_MARKER,
    ROOT_TRANSITION_MARKER,
    ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES,
    ROOT_TRANSITION_REJECTION_MARKER,
    SUCCESS_MARKER,
    WORKLOAD_CLEANUP_REJECTION_PREFIX,
    WORKLOAD_NEGATIVE_CONTROL_NAMES,
    WORKLOAD_NEGATIVE_REJECTION_MARKERS,
    WORKLOAD_SIGNAL_ARMED_MARKER,
    WORKLOAD_STARTED_MARKER,
    WORKLOAD_TERMINAL_PREFIX,
    KVMProofFailure,
    KVMProofUnavailable,
    OCIStage1KVMProofReceipt,
    _logical_line_count,
    _read_console_until,
    _secure_write,
    _verify_fixture_source_tree,
    _verify_workload_proof_provenance,
    assembly_negative_control_contract,
    build_assembly_negative_qemu_command,
    build_filesystem_negative_qemu_command,
    build_kernel_cmdline,
    build_negative_qemu_command,
    build_proof_plan,
    build_qemu_command,
    build_root_transition_negative_qemu_command,
    build_workload_negative_qemu_command,
    filesystem_negative_control_contract,
    negative_control_contract,
    pre_mount_topology,
    root_transition_negative_control_contract,
    transport_serial,
    verify_evidence_directory,
    verify_kernel_config,
    verify_kernel_configuration_selection,
    verify_linux_bzimage,
    verify_proof_filesystem_manifest,
    workload_negative_control_contract,
)
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_control_protocol import OCIControlBinding, OCIControlMessage, encode_frame
from palimpsest_local.oci_initramfs import build_bootstrap_initramfs
from palimpsest_local.oci_stage1_transport import build_stage1_transport


def _write(path: Path, payload: bytes, mode: int = 0o644) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path.resolve()


def _negative_consoles() -> dict[str, bytes]:
    return {name: b"kernel boot\n" + REJECTION_MARKER + b"\n" for name in NEGATIVE_CONTROL_NAMES}


def _filesystem_negative_consoles() -> dict[str, bytes]:
    return {name: b"kernel boot\n" + FILESYSTEM_REJECTION_MARKER + b"\n" for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES}


def _assembly_negative_consoles() -> dict[str, bytes]:
    return {name: b"kernel boot\n" + ASSEMBLY_REJECTION_MARKER + b"\n" for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES}


def _root_transition_negative_consoles() -> dict[str, bytes]:
    return {
        name: b"kernel boot\n" + ROOT_TRANSITION_REJECTION_MARKER + b"\n"
        for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES
    }


def _positive_console() -> bytes:
    return (
        b"kernel boot\r\n"
        + ROOT_TRANSITION_MARKER
        + b"\r\n"
        + WORKLOAD_STARTED_MARKER
        + b"\r\n"
        + WORKLOAD_SIGNAL_ARMED_MARKER
        + b"\r\n"
        + SUCCESS_MARKER
        + b"\r\n"
    )


def _workload_negative_consoles() -> dict[str, bytes]:
    return {
        name: b"kernel boot\n" + ROOT_TRANSITION_MARKER + b"\n" + marker + b"\n"
        for name, marker in WORKLOAD_NEGATIVE_REJECTION_MARKERS.items()
    }


def _lifecycle() -> dict[str, object]:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    boots = []
    for boot_index in (1, 2):
        nonce = str(boot_index) * 64
        generation = f"{boot_index * 11111111:08d}-{boot_index * 1111:04d}-4{boot_index * 111:03d}-8{boot_index * 111:03d}-{boot_index * 111111111111:012d}"
        messages = (
            OCIControlMessage(kind="HELLO", binding=binding, host_nonce=nonce, payload={}, request_id=1),
            OCIControlMessage(
                kind="READY",
                binding=binding,
                host_nonce=nonce,
                payload={},
                sequence=1,
                boot_generation=generation,
                reply_to=1,
            ),
            OCIControlMessage(
                kind="STOP",
                binding=binding,
                host_nonce=nonce,
                payload={"signal": 15},
                request_id=2,
                boot_generation=generation,
            ),
            OCIControlMessage(
                kind="TERMINAL",
                binding=binding,
                host_nonce=nonce,
                payload={"terminal": {"exit_code": 42, "signal": None}},
                sequence=2,
                boot_generation=generation,
                reply_to=2,
            ),
        )
        frames = []
        for direction, message in zip(
            ("host-to-guest", "guest-to-host", "host-to-guest", "guest-to-host"), messages, strict=True
        ):
            encoded = encode_frame(message)
            frames.append(
                {
                    "boot_generation": message.boot_generation,
                    "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                    "direction": direction,
                    "host_nonce": message.host_nonce,
                    "kind": message.kind,
                    "reply_to": message.reply_to,
                    "request_id": message.request_id,
                    "sequence": message.sequence,
                    "size_bytes": len(encoded),
                }
            )
        boots.append(
            {
                "frames": frames,
                "pid1_alive_after_terminal": True,
                "ready": True,
                "terminal": {"exit_code": 42, "signal": None},
            }
        )
    return {
        "binding": {
            "domain_core_digest": plan.domain_core_digest,
            "run_id": plan.run_id,
            "stage1_artifact_digest": transport.receipt.artifact_digest,
        },
        "boots": boots,
        "broker_contract": "palimpsest.guest-lifecycle-broker.v1",
        "channel_name": "org.palimpsest.oci.lifecycle.0",
        "nonce_semantics": "correlation-and-replay-challenge-not-peer-authentication",
        "negative_input_proven": False,
        "protocol": "palimpsest.oci-lifecycle-control.v1",
        "reconnect_proven": False,
        "single_connection_proven": True,
        "transport": "qemu-private-unix-socket-to-virtio-serial",
    }


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
        pre_mount_topology(plan)["devices"][0]["artifact_digest"],
        "sha256:" + "4" * 64,
        "sha256:" + "5" * 64,
        _positive_console(),
        _positive_console(),
        _negative_consoles(),
        _filesystem_negative_consoles(),
        _assembly_negative_consoles(),
        {name: "sha256:" + "6" * 64 for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES},
        _root_transition_negative_consoles(),
        {name: "sha256:" + "7" * 64 for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES},
        _workload_negative_consoles(),
        {name: "sha256:" + "8" * 64 for name in WORKLOAD_NEGATIVE_CONTROL_NAMES},
        _lifecycle(),
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
    assert "CONFIG_CGROUPS" in _REQUIRED_KERNEL_CONFIG
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
    assert plan.process.to_dict() == {
        "argv": ["/.__palimpsest_workload_proof_v1", "palimpsest-argv-one", "", "line\nbreak"],
        "cwd": "/proof/workdir",
        "environment": [
            {"name": "PALIMPSEST_PROOF_ENV", "value": "value with spaces"},
            {"name": "PALIMPSEST_PROOF_EMPTY", "value": ""},
        ],
        "stop_signal": 15,
        "user": {"group": "65534", "user": "65534"},
    }
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
    lifecycle_socket = (tmp_path / "lifecycle.sock").resolve()
    lifecycle_command = build_qemu_command(
        qemu_path=paths[0],
        kernel_path=paths[1],
        initramfs_path=paths[2],
        transport_path=paths[3],
        root_path=(tmp_path / "root").resolve(),
        lower_paths=tuple((tmp_path / f"lower-{index}").resolve() for index in range(len(plan.layers))),
        plan=plan,
        cmdline=cmdline,
        serial=transport_serial(transport.receipt.artifact_digest),
        lifecycle_socket_path=lifecycle_socket,
    )
    rendered = " ".join(lifecycle_command)
    assert rendered.count("org.palimpsest.oci.lifecycle.0") == 1
    assert rendered.count("virtio-serial-pci") == 1
    assert rendered.count("virtserialport") == 1
    assert "server=on,wait=off" in rendered
    assert "reconnect=" not in rendered
    assert "rng-random,id=palimpsest-rng,filename=/dev/urandom" in rendered
    assert "virtio-rng-pci,rng=palimpsest-rng" in rendered


def test_actual_filesystem_fixture_manifest_is_exact_and_receipt_bound() -> None:
    topology = pre_mount_topology(build_proof_plan())
    manifest_digest = topology["fixture_manifest_digest"]
    assert manifest_digest == "sha256:dbca92880316d4a142480d28ddea00bb862f3fc61bed3182c2d26156d0f0493a"
    assert topology["fixture_policy"] == "palimpsest.kvm-actual-filesystem-fixtures.v7"
    manifest = json.loads((Path(__file__).parents[1] / "kvm" / "assets" / "filesystem-fixtures.json").read_text())
    helper = manifest["provenance"]["workload_proof"]
    helper_source = (Path(__file__).parents[2] / helper["source"]).read_bytes()
    assert b"/proc/self/root/.__palimpsest_oci_root_workload_proof_v1" in helper_source
    assert b"/proc/1/root/.__palimpsest_oci_root_workload_proof_v1" not in helper_source
    assert b'"/proc/1/status"' in helper_source
    assert b'"Uid:\\t0\\t0\\t0\\t0\\n"' in helper_source
    assert b'"Gid:\\t0\\t0\\t0\\t0\\n"' in helper_source
    assert b'"Groups:\\t \\n"' in helper_source
    assert b'"0::/palimpsest.workload\\n"' in helper_source
    assert b'"/sys/fs/cgroup/cgroup.procs"' in helper_source
    assert b'"/sys/fs/cgroup/palimpsest.workload/cgroup.procs"' in helper_source
    assert helper == {
        "build_script": "scripts/build_oci_guest_workload_proof.sh",
        "build_script_sha256": "4f88223bc5cf8b853254a229187f55d6c3cbf6c31992ee0008c8f797bf43e25d",
        "elf_mode": 0o755,
        "elf_sha256": "0adcb6bf3a77d6ee6e59fbb8083e7a995091c8f0f51b416830280cc2ba7fecc4",
        "elf_size_bytes": 8952,
        "source": "guest/workload-proof/proof.c",
        "source_sha256": "d57abd8c6e27ac4ab740aa0956626695774fe825eb965c31db7ba3cccd66c6a3",
        "toolchain": "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1",
    }
    with pytest.raises(ArtifactValidationError, match="fixture policy"):
        verify_proof_filesystem_manifest({"schema": "palimpsest.kvm-filesystem-fixtures.v1"})


def test_workload_proof_provenance_rejects_a_final_symlink(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    metadata = json.loads((repository / "tests/kvm/assets/filesystem-fixtures.json").read_text())
    provenance = metadata["provenance"]["workload_proof"]
    replica = tmp_path / "repository"
    fixture_directory = tmp_path / "fixtures"
    for relative, mode in (
        (provenance["source"], 0o644),
        (provenance["build_script"], 0o755),
    ):
        destination = replica / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write(destination, (repository / relative).read_bytes(), mode)
    fixture_directory.mkdir()
    helper = _write(
        fixture_directory / "workload-proof.x86_64",
        (repository / "tests/kvm/assets/workload-proof.x86_64").read_bytes(),
        0o755,
    )
    _verify_workload_proof_provenance(replica.absolute(), fixture_directory.absolute(), provenance)

    target = _write(tmp_path / "helper-target", helper.read_bytes(), 0o755)
    helper.unlink()
    helper.symlink_to(target)
    with pytest.raises(ArtifactValidationError, match="opened safely"):
        _verify_workload_proof_provenance(replica.absolute(), fixture_directory.absolute(), provenance)


def test_fixture_source_tree_rejects_unlisted_entries(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "probe"
    source.write_bytes(b"x")
    source.chmod(0o644)
    sources = [
        {
            "mode": 0o644,
            "path": "probe",
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "size_bytes": 1,
            "type": "file",
        }
    ]
    _verify_fixture_source_tree(source_root, sources)

    (source_root / "unlisted").write_bytes(b"extra")
    with pytest.raises(ArtifactValidationError, match="source tree"):
        _verify_fixture_source_tree(source_root, sources)


@pytest.mark.parametrize("control_name", ASSEMBLY_NEGATIVE_CONTROL_NAMES)
def test_each_assembly_negative_binds_distinct_post_overlay_probe_boot(tmp_path: Path, control_name: str) -> None:
    contract = assembly_negative_control_contract(control_name)
    paths = {name: (tmp_path / name).resolve() for name in contract["backings"]}
    command = build_assembly_negative_qemu_command(
        qemu_path=(tmp_path / "qemu").resolve(),
        kernel_path=(tmp_path / "kernel").resolve(),
        initramfs_path=(tmp_path / "initrd").resolve(),
        backing_paths=paths,
        cmdline=contract["cmdline"],
        control=contract,
    )
    assert contract["stage"] == "post-overlay-probe"
    assert contract["rejection_marker"] == ASSEMBLY_REJECTION_MARKER.decode("ascii")
    assert command[command.index("-append") + 1] == contract["cmdline"]
    assert contract["stage1_plan"]["assembly"]["probes"] != build_proof_plan().to_dict()["assembly"]["probes"]


@pytest.mark.parametrize("control_name", ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES)
def test_each_root_transition_negative_binds_distinct_real_lower_and_boot(tmp_path: Path, control_name: str) -> None:
    contract = root_transition_negative_control_contract(control_name)
    paths = {name: (tmp_path / name).resolve() for name in contract["backings"]}
    command = build_root_transition_negative_qemu_command(
        qemu_path=(tmp_path / "qemu").resolve(),
        kernel_path=(tmp_path / "kernel").resolve(),
        initramfs_path=(tmp_path / "initrd").resolve(),
        backing_paths=paths,
        cmdline=contract["cmdline"],
        control=contract,
    )
    assert contract["stage"] == "root-transition-target-preparation"
    assert contract["target"] in {"dev", "sys", "proc"}
    assert contract["rejection_marker"] == ROOT_TRANSITION_REJECTION_MARKER.decode("ascii")
    assert command[command.index("-append") + 1] == contract["cmdline"]
    assert (
        contract["stage1_plan"]["assembly"]["layers"][1]["image_digest"]
        == contract["backings"]["lower1"]["artifact_digest"]
    )
    assert (
        contract["stage1_plan"]["assembly"]["layers"][1]["occurrence_digest"]
        == build_proof_plan().layers[1]["occurrence_digest"]
    )
    assert (
        contract["backings"]["root"]["artifact_digest"]
        == pre_mount_topology(build_proof_plan())["devices"][0]["artifact_digest"]
    )


@pytest.mark.parametrize("control_name", WORKLOAD_NEGATIVE_CONTROL_NAMES)
def test_each_workload_negative_binds_distinct_plan_transport_root_and_boot(tmp_path: Path, control_name: str) -> None:
    contract = workload_negative_control_contract(control_name)
    paths = {name: (tmp_path / name).resolve() for name in contract["backings"]}
    command = build_workload_negative_qemu_command(
        qemu_path=(tmp_path / "qemu").resolve(),
        kernel_path=(tmp_path / "kernel").resolve(),
        initramfs_path=(tmp_path / "initrd").resolve(),
        backing_paths=paths,
        cmdline=contract["cmdline"],
        control=contract,
    )
    assert contract["stage"] == "post-root-transition-workload-launch"
    assert contract["rejection_marker"] == WORKLOAD_NEGATIVE_REJECTION_MARKERS[control_name].decode("ascii")
    assert command[command.index("-append") + 1] == contract["cmdline"]
    assert contract["stage1_plan"] != build_proof_plan().to_dict()
    assert contract["stage1_transport"]["artifact_digest"] not in {
        workload_negative_control_contract(other)["stage1_transport"]["artifact_digest"]
        for other in WORKLOAD_NEGATIVE_CONTROL_NAMES
        if other != control_name
    }
    assert (
        contract["backings"]["root"]["artifact_digest"]
        == pre_mount_topology(build_proof_plan())["devices"][0]["artifact_digest"]
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


@pytest.mark.parametrize("control_name", FILESYSTEM_NEGATIVE_CONTROL_NAMES)
def test_each_filesystem_negative_has_same_topology_and_one_exact_byte_contract(
    tmp_path: Path,
    control_name: str,
) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    contract = filesystem_negative_control_contract(control_name)
    backing_paths = {name: (tmp_path / name).resolve() for name in contract["backings"]}
    command = build_filesystem_negative_qemu_command(
        qemu_path=(tmp_path / "qemu").resolve(),
        kernel_path=(tmp_path / "kernel").resolve(),
        initramfs_path=(tmp_path / "initrd").resolve(),
        backing_paths=backing_paths,
        cmdline=contract["cmdline"],
        control=contract,
    )

    assert contract["policy"] == "palimpsest.stage1-kvm-filesystem-negative-control.v1"
    assert set(contract) == {
        "attachments",
        "backings",
        "cmdline",
        "digest",
        "name",
        "policy",
        "stage1_plan",
        "stage1_transport",
    }
    assert len(contract["attachments"]) == 4
    assert all(item["read_only"] is (item["role"] != "root") for item in contract["attachments"])
    assert tuple(item["role"] for item in contract["attachments"]) == (
        "lower",
        "transport",
        "root",
        "lower",
    )
    assert command[command.index("-accel") + 1] == "kvm"
    assert command[command.index("-append") + 1] == contract["cmdline"]
    assert "accel=tcg" not in " ".join(command)
    transport_attachment = next(item for item in contract["attachments"] if item["role"] == "transport")
    assert transport_attachment["serial"] == contract["stage1_transport"]["serial"]
    assert contract["backings"]["transport"]["artifact_digest"] == contract["stage1_transport"]["artifact_digest"]
    if control_name in {"lower_bad_magic", "lower_bad_structure"}:
        assert contract["stage1_plan"] != plan.to_dict()
        assert contract["cmdline"] != build_kernel_cmdline(plan, transport)
        assert (
            contract["stage1_plan"]["assembly"]["layers"][0]["image_digest"]
            == contract["backings"]["lower0"]["artifact_digest"]
        )
    else:
        assert contract["stage1_plan"] == plan.to_dict()

    with pytest.raises(ArtifactValidationError, match="cmdline"):
        build_filesystem_negative_qemu_command(
            qemu_path=(tmp_path / "qemu").resolve(),
            kernel_path=(tmp_path / "kernel").resolve(),
            initramfs_path=(tmp_path / "initrd").resolve(),
            backing_paths=backing_paths,
            cmdline=build_kernel_cmdline(plan, transport) + " drift",
            control=contract,
        )


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


def test_console_reader_rejects_an_unexpected_terminal_without_waiting_for_timeout() -> None:
    unexpected = WORKLOAD_TERMINAL_PREFIX + b"109; cooperative_status=137; forced_status=-1; reaped=2\n"
    program = f"import sys,time;sys.stdout.buffer.write({unexpected!r});sys.stdout.flush();time.sleep(10)"
    started = time.monotonic()
    with pytest.raises(KVMProofFailure, match="unexpected workload terminal"):
        _read_console_until(
            (sys.executable, "-c", program),
            expected=SUCCESS_MARKER,
            forbidden=(REJECTION_MARKER,),
            timeout_seconds=3,
            require_alive_after_marker=True,
        )
    assert time.monotonic() - started < 2


def test_console_reader_rejects_cleanup_uncertainty_without_waiting_for_timeout() -> None:
    rejected = WORKLOAD_CLEANUP_REJECTION_PREFIX + b"18; errno=16; terminal disabled; waiting fail-closed\n"
    program = f"import sys,time;sys.stdout.buffer.write({rejected!r});sys.stdout.flush();time.sleep(10)"
    started = time.monotonic()
    with pytest.raises(KVMProofFailure, match="cleanup rejection"):
        _read_console_until(
            (sys.executable, "-c", program),
            expected=SUCCESS_MARKER,
            forbidden=(REJECTION_MARKER,),
            timeout_seconds=3,
            require_alive_after_marker=True,
        )
    assert time.monotonic() - started < 2


def test_console_reader_rejects_lifecycle_failure_without_waiting_for_timeout() -> None:
    rejected = LIFECYCLE_REJECTION_PREFIX + b"21; errno=5; terminal disabled; waiting fail-closed\n"
    program = f"import sys,time;sys.stdout.buffer.write({rejected!r});sys.stdout.flush();time.sleep(10)"
    started = time.monotonic()
    with pytest.raises(KVMProofFailure, match="lifecycle rejection"):
        _read_console_until(
            (sys.executable, "-c", program),
            expected=SUCCESS_MARKER,
            forbidden=(REJECTION_MARKER,),
            timeout_seconds=3,
            require_alive_after_marker=True,
        )
    assert time.monotonic() - started < 2


def test_console_reader_drives_fragmented_single_connection_lifecycle(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    temporary = tempfile.TemporaryDirectory(prefix="pali-lifecycle-", dir="/tmp")
    channel = (Path(temporary.name) / "lifecycle.sock").resolve()
    program = f"""
import socket, struct, sys, time
from palimpsest_local.oci_control_protocol import OCIControlMessage, decode_frame, encode_frame

def receive(connection):
    header = b''
    while len(header) < 4:
        header += connection.recv(1)
    size = struct.unpack('>I', header)[0]
    payload = b''
    while len(payload) < size:
        payload += connection.recv(1)
    return decode_frame(header + payload)

listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1])
listener.listen(1)
connection, _ = listener.accept()
hello = receive(connection)
generation = '11111111-1111-4111-8111-111111111111'
ready = OCIControlMessage(kind='READY', binding=hello.binding, host_nonce=hello.host_nonce,
    payload={{}}, sequence=1, boot_generation=generation, reply_to=hello.request_id)
frame = encode_frame(ready)
for part in (frame[:1], frame[1:3], frame[3:]): connection.sendall(part)
for marker in ({ROOT_TRANSITION_MARKER!r}, {WORKLOAD_STARTED_MARKER!r}, {WORKLOAD_SIGNAL_ARMED_MARKER!r}):
    sys.stdout.buffer.write(marker + b'\\n'); sys.stdout.flush()
stop = receive(connection)
terminal = OCIControlMessage(kind='TERMINAL', binding=hello.binding, host_nonce=hello.host_nonce,
    payload={{'terminal': {{'exit_code': 42, 'signal': None}}}}, sequence=2,
    boot_generation=generation, reply_to=stop.request_id)
frame = encode_frame(terminal)
for byte in frame: connection.sendall(bytes((byte,)))
sys.stdout.buffer.write({SUCCESS_MARKER!r} + b'\\n'); sys.stdout.flush()
time.sleep(2)
"""
    transcript: list[dict[str, object]] = []
    console = _read_console_until(
        (sys.executable, "-c", program, os.fspath(channel)),
        expected=SUCCESS_MARKER,
        forbidden=(REJECTION_MARKER,),
        timeout_seconds=3,
        require_alive_after_marker=True,
        lifecycle_socket_path=channel,
        lifecycle_binding=binding,
        lifecycle_success=True,
        lifecycle_transcript=transcript,
    )
    assert _logical_line_count(console, WORKLOAD_SIGNAL_ARMED_MARKER) == 1
    assert [frame["kind"] for frame in transcript] == ["HELLO", "READY", "STOP", "TERMINAL"]
    assert [frame["direction"] for frame in transcript] == [
        "host-to-guest",
        "guest-to-host",
        "host-to-guest",
        "guest-to-host",
    ]
    assert transcript[1]["boot_generation"] == transcript[2]["boot_generation"] == transcript[3]["boot_generation"]
    temporary.cleanup()


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
            filesystem_negative_consoles=receipt.filesystem_negative_consoles,
            retained_console=receipt.retained_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
        )
        == receipt
    )
    assert decoded["qemu"]["artifact_digest"] == "sha256:" + "3" * 64
    assert decoded["root_assembly"] is True
    assert decoded["root_is_slash"] is True
    assert decoded["pivot_root"] is False
    assert decoded["switch_root"] is True
    assert decoded["root_transition"] == {
        "contract": "palimpsest.stage1-root-transition.v1",
        "method": "move-mount-chroot",
        "pid1_root_matches_slash": True,
        "pivot_root": False,
        "pseudo_filesystems": ["dev", "sys", "proc"],
        "root_filesystem": "overlay",
        "switch_root": True,
        "workload_started": False,
    }
    assert decoded["workload_started"] is True
    assert decoded["supervisor"] == {
        "contract": "palimpsest.guest-pid1-supervisor.v4",
        "cgroup": "/palimpsest.workload",
        "cgroup_security": "containment-and-cleanup-not-hostile-root-sandbox",
        "cgroup_write_escape_denied": ["parent", "own"],
        "cleanup": "stop-signal-grace-cgroup.kill-wait4-echild-populated-zero-rmdir",
        "cooperative_status": 43,
        "credential_timing": "child-after-parent-cgroup-attach-release",
        "forced_status": 137,
        "forwarded_signal": 15,
        "lifecycle_broker": "palimpsest.guest-lifecycle-broker.v1",
        "lifecycle_stop": "host-issued-after-ready-and-proof-signal-sync",
        "main_status": 42,
        "pid1_credentials": {"gid": 0, "supplementary_groups": [], "uid": 0},
        "privileged_broker_after_fork": True,
        "process_group": True,
        "reaped_children": 3,
        "terminal_state": "parent-marker-then-fail-closed-wait",
        "terminal_wire_order": "cleanup-certainty-then-terminal-frame-then-console-marker",
        "workload_credentials": {"gid": 65534, "supplementary_groups": [], "uid": 65534},
    }
    assert decoded["pre_mount_devices"] is True
    assert decoded["filesystem_verified"] is True
    assert decoded["root_filesystem_verified"] is True
    assert decoded["root_content_verified"] is False
    assert decoded["lower_filesystem_verified"] is True
    assert decoded["lower_content_verified"] is True
    assert decoded["mount_attempted"] is True
    assert decoded["root_filesystem_mounted"] is True
    assert decoded["lower_filesystems_mounted"] is True
    assert decoded["overlay_assembled"] is True
    assert decoded["root_volume"] == {
        "boot1_post_and_boot2_pre_digest": "sha256:" + "4" * 64,
        "boot2_post_digest": "sha256:" + "5" * 64,
        "content_verified": False,
        "retained_same_backing": True,
        "seed_digest": pre_mount_topology(build_proof_plan())["devices"][0]["artifact_digest"],
    }
    assert decoded["topology"] == pre_mount_topology(build_proof_plan())
    assert tuple(decoded["negative_controls"]) == tuple(sorted(NEGATIVE_CONTROL_NAMES))
    for name in NEGATIVE_CONTROL_NAMES:
        assert decoded["negative_controls"][name]["contract"] == negative_control_contract(name)
    for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES:
        assert decoded["filesystem_negative_controls"][name]["contract"] == filesystem_negative_control_contract(name)
    for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES:
        assert decoded["root_transition_negative_controls"][name]["contract"] == (
            root_transition_negative_control_contract(name)
        )
    for name in WORKLOAD_NEGATIVE_CONTROL_NAMES:
        assert decoded["workload_negative_controls"][name]["contract"] == workload_negative_control_contract(name)
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
    "mutation",
    ["missing", "extra", "method", "pivot", "switch", "root", "pseudo-order", "workload"],
)
def test_receipt_rejects_root_transition_claim_tamper(mutation: str) -> None:
    receipt = _receipt()
    value = copy.deepcopy(receipt.to_dict())
    transition = value["root_transition"]
    if mutation == "missing":
        del transition["root_filesystem"]
    elif mutation == "extra":
        transition["old_root_detached"] = True
    elif mutation == "method":
        transition["method"] = "pivot-root"
    elif mutation == "pivot":
        transition["pivot_root"] = True
    elif mutation == "switch":
        transition["switch_root"] = False
    elif mutation == "root":
        transition["pid1_root_matches_slash"] = False
    elif mutation == "pseudo-order":
        transition["pseudo_filesystems"] = ["dev", "proc", "sys"]
    else:
        transition["workload_started"] = True

    with pytest.raises(ArtifactValidationError, match="policy|canonical"):
        OCIStage1KVMProofReceipt.from_dict(
            value,
            console=receipt.console,
            negative_consoles=receipt.negative_consoles,
            filesystem_negative_consoles=receipt.filesystem_negative_consoles,
            retained_console=receipt.retained_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
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
            filesystem_negative_consoles=receipt.filesystem_negative_consoles,
            retained_console=receipt.retained_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
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
        OCIStage1KVMProofReceipt.from_dict(
            value,
            console=receipt.console,
            negative_consoles=consoles,
            filesystem_negative_consoles=receipt.filesystem_negative_consoles,
            retained_console=receipt.retained_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
        )


@pytest.mark.parametrize("mutation", ["missing", "extra", "contract", "console_digest", "marker_count", "liveness"])
def test_receipt_rejects_filesystem_control_mapping_tamper(mutation: str) -> None:
    receipt = _receipt()
    value = copy.deepcopy(receipt.to_dict())
    controls = value["filesystem_negative_controls"]
    name = FILESYSTEM_NEGATIVE_CONTROL_NAMES[0]
    if mutation == "missing":
        del controls[name]
    elif mutation == "extra":
        controls["unexpected"] = copy.deepcopy(controls[name])
    elif mutation == "contract":
        controls[name]["contract"]["name"] = "root_geometry"
    elif mutation == "console_digest":
        controls[name]["console_digest"] = "sha256:" + "f" * 64
    elif mutation == "marker_count":
        controls[name]["rejection_marker_count"] = 2
    else:
        controls[name]["pid1_alive_after_marker"] = False

    with pytest.raises(ArtifactValidationError, match="policy|canonical"):
        OCIStage1KVMProofReceipt.from_dict(
            value,
            console=receipt.console,
            negative_consoles=receipt.negative_consoles,
            filesystem_negative_consoles=receipt.filesystem_negative_consoles,
            retained_console=receipt.retained_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
        )


@pytest.mark.parametrize(
    "marker",
    [
        REJECTION_MARKER,
        SUCCESS_MARKER,
        ASSEMBLY_REJECTION_MARKER,
        ROOT_TRANSITION_REJECTION_MARKER,
        PREPARATION_FAILURE_MARKER,
    ],
)
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


@pytest.mark.parametrize(
    "marker",
    [
        FILESYSTEM_REJECTION_MARKER,
        REJECTION_MARKER,
        SUCCESS_MARKER,
        ASSEMBLY_REJECTION_MARKER,
        ROOT_TRANSITION_REJECTION_MARKER,
        PREPARATION_FAILURE_MARKER,
    ],
)
def test_receipt_requires_one_filesystem_rejection_and_no_other_marker(marker: bytes) -> None:
    receipt = _receipt()
    consoles = dict(receipt.filesystem_negative_consoles)
    name = FILESYSTEM_NEGATIVE_CONTROL_NAMES[0]
    consoles[name] += marker + b"\n"
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, filesystem_negative_consoles=consoles)


@pytest.mark.parametrize(
    "marker",
    [
        ASSEMBLY_REJECTION_MARKER,
        FILESYSTEM_REJECTION_MARKER,
        REJECTION_MARKER,
        SUCCESS_MARKER,
        ROOT_TRANSITION_REJECTION_MARKER,
        PREPARATION_FAILURE_MARKER,
    ],
)
def test_receipt_requires_one_assembly_rejection_and_no_other_marker(marker: bytes) -> None:
    receipt = _receipt()
    consoles = dict(receipt.assembly_negative_consoles)
    name = ASSEMBLY_NEGATIVE_CONTROL_NAMES[0]
    consoles[name] += marker + b"\n"
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, assembly_negative_consoles=consoles)


@pytest.mark.parametrize(
    "marker",
    [
        ROOT_TRANSITION_REJECTION_MARKER,
        ASSEMBLY_REJECTION_MARKER,
        FILESYSTEM_REJECTION_MARKER,
        REJECTION_MARKER,
        SUCCESS_MARKER,
        PREPARATION_FAILURE_MARKER,
    ],
)
def test_receipt_requires_one_root_transition_rejection_and_no_other_marker(marker: bytes) -> None:
    receipt = _receipt()
    consoles = dict(receipt.root_transition_negative_consoles)
    name = ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES[0]
    consoles[name] += marker + b"\n"
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, root_transition_negative_consoles=consoles)


@pytest.mark.parametrize(
    "marker",
    [
        ROOT_TRANSITION_MARKER,
        WORKLOAD_STARTED_MARKER,
        SUCCESS_MARKER,
        REJECTION_MARKER,
        FILESYSTEM_REJECTION_MARKER,
        ASSEMBLY_REJECTION_MARKER,
        ROOT_TRANSITION_REJECTION_MARKER,
        PREPARATION_FAILURE_MARKER,
    ],
)
def test_receipt_requires_exact_workload_rejection_after_root_transition(marker: bytes) -> None:
    receipt = _receipt()
    consoles = dict(receipt.workload_negative_consoles)
    name = WORKLOAD_NEGATIVE_CONTROL_NAMES[0]
    consoles[name] += marker + b"\n"
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, workload_negative_consoles=consoles)


@pytest.mark.parametrize("field", ["console", "retained_console"])
def test_receipt_rejects_reversed_positive_marker_order(field: str) -> None:
    receipt = _receipt()
    reversed_console = (
        b"kernel boot\n" + SUCCESS_MARKER + b"\n" + WORKLOAD_STARTED_MARKER + b"\n" + ROOT_TRANSITION_MARKER + b"\n"
    )
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, **{field: reversed_console})


def test_receipt_rejects_workload_rejection_before_root_transition() -> None:
    receipt = _receipt()
    consoles = dict(receipt.workload_negative_consoles)
    name = WORKLOAD_NEGATIVE_CONTROL_NAMES[0]
    consoles[name] = (
        b"kernel boot\n" + WORKLOAD_NEGATIVE_REJECTION_MARKERS[name] + b"\n" + ROOT_TRANSITION_MARKER + b"\n"
    )
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, workload_negative_consoles=consoles)


def test_receipt_rejects_workload_negative_control_mapping_tamper() -> None:
    receipt = _receipt()
    value = copy.deepcopy(receipt.to_dict())
    name = WORKLOAD_NEGATIVE_CONTROL_NAMES[0]
    value["workload_negative_controls"][name]["rejection_marker_count"] = 2
    with pytest.raises(ArtifactValidationError, match="policy|canonical"):
        OCIStage1KVMProofReceipt.from_dict(
            value,
            console=receipt.console,
            negative_consoles=receipt.negative_consoles,
            filesystem_negative_consoles=receipt.filesystem_negative_consoles,
            retained_console=receipt.retained_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "reconnect",
        "negative-proven",
        "binding",
        "direction",
        "nonce",
        "generation",
        "request",
        "reply",
        "sequence",
        "digest",
        "valid-looking-digest",
        "size",
        "valid-looking-size",
        "cross-boot-nonce-generation",
        "missing",
    ],
)
def test_receipt_rejects_lifecycle_evidence_tamper(mutation: str) -> None:
    receipt = _receipt()
    lifecycle = copy.deepcopy(receipt.lifecycle)
    if mutation == "reconnect":
        lifecycle["reconnect_proven"] = True
    elif mutation == "negative-proven":
        lifecycle["negative_input_proven"] = True
    elif mutation == "binding":
        lifecycle["binding"]["run_id"] = "00000000-0000-4000-8000-000000000000"
    elif mutation == "direction":
        lifecycle["boots"][0]["frames"][0]["direction"] = "guest-to-host"
    elif mutation == "nonce":
        lifecycle["boots"][0]["frames"][0]["host_nonce"] = "a" * 64
    elif mutation == "generation":
        lifecycle["boots"][0]["frames"][1]["boot_generation"] = "33333333-3333-4333-8333-333333333333"
    elif mutation == "request":
        lifecycle["boots"][0]["frames"][2]["request_id"] = 3
    elif mutation == "reply":
        lifecycle["boots"][0]["frames"][3]["reply_to"] = 1
    elif mutation == "sequence":
        lifecycle["boots"][0]["frames"][3]["sequence"] = 3
    elif mutation == "digest":
        lifecycle["boots"][0]["frames"][0]["digest"] = "sha256:bad"
    elif mutation == "valid-looking-digest":
        lifecycle["boots"][0]["frames"][0]["digest"] = "sha256:" + "f" * 64
    elif mutation == "size":
        lifecycle["boots"][0]["frames"][0]["size_bytes"] = 65537
    elif mutation == "valid-looking-size":
        lifecycle["boots"][0]["frames"][0]["size_bytes"] += 1
    elif mutation == "cross-boot-nonce-generation":
        lifecycle["boots"][1] = copy.deepcopy(lifecycle["boots"][0])
    else:
        lifecycle.pop("protocol")
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, lifecycle=lifecycle)


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
        "retained-console.bin",
        "receipt.json",
        *(f"negative-{name}.bin" for name in NEGATIVE_CONTROL_NAMES),
        *(f"filesystem-negative-{name}.bin" for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES),
        *(f"assembly-negative-{name}.bin" for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES),
        *(f"root-transition-negative-{name}.bin" for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES),
        *(f"workload-negative-{name}.bin" for name in WORKLOAD_NEGATIVE_CONTROL_NAMES),
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    output = _secure_write(evidence.resolve(), EVIDENCE_FILE_NAMES[2], b"console", mode=0o400)
    assert output.read_bytes() == b"console"
    assert output.stat().st_mode & 0o777 == 0o400
    with pytest.raises(ArtifactValidationError, match="cannot be published"):
        _secure_write(evidence.resolve(), EVIDENCE_FILE_NAMES[2], b"replacement", mode=0o400)
