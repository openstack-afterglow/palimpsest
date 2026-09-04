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
    LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES,
    LIFECYCLE_NEGATIVE_CONTROL_NAMES,
    LIFECYCLE_PARTIAL_BUFFERED_MARKER,
    LIFECYCLE_PEER_BOUNDARY_MARKER,
    LIFECYCLE_READY_COMMITTED_MARKER,
    LIFECYCLE_REJECTION_PREFIX,
    LIFECYCLE_STOP_DISPATCHED_MARKER,
    LIFECYCLE_STOP_DUPLICATE_MARKER,
    LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES,
    NEGATIVE_CONTROL_NAMES,
    PREPARATION_FAILURE_MARKER,
    QEMU_DUPLICATE_NAME_REJECTION_MARKER,
    REJECTION_MARKER,
    ROOT_TRANSITION_MARKER,
    ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES,
    ROOT_TRANSITION_REJECTION_MARKER,
    SUCCESS_MARKER,
    WORKLOAD_CLEANUP_REJECTION_PREFIX,
    WORKLOAD_ISOLATION_MARKER,
    WORKLOAD_NEGATIVE_CONTROL_NAMES,
    WORKLOAD_NEGATIVE_REJECTION_MARKERS,
    WORKLOAD_SIGNAL_ARMED_MARKER,
    WORKLOAD_STARTED_MARKER,
    WORKLOAD_STOP_OBSERVED_MARKER,
    WORKLOAD_TERMINAL_PREFIX,
    KVMProofFailure,
    KVMProofUnavailable,
    OCIStage1KVMProofReceipt,
    _lifecycle_negative_wire_bytes,
    _logical_line_count,
    _read_console_until,
    _receipt_lifecycle_projection,
    _secure_write,
    _uid0_lifecycle_evidence,
    _verify_fixture_source_tree,
    _verify_post_run_topology,
    _verify_workload_proof_provenance,
    assembly_negative_control_contract,
    build_assembly_negative_qemu_command,
    build_filesystem_negative_qemu_command,
    build_kernel_cmdline,
    build_negative_qemu_command,
    build_proof_plan,
    build_qemu_command,
    build_root_transition_negative_qemu_command,
    build_uid0_isolation_proof_plan,
    build_workload_negative_qemu_command,
    filesystem_negative_control_contract,
    lifecycle_negative_control_contract,
    load_proof_filesystems,
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
from palimpsest_local.oci_control_protocol import OCIControlBinding
from palimpsest_local.oci_control_protocol_v2 import HostOCIControlV2Session, OCIControlV2Binding, encode_frame
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


def _positive_console(*, retained: bool = False) -> bytes:
    payload = (
        b"kernel boot\r\n"
        + ROOT_TRANSITION_MARKER
        + b"\r\n"
        + WORKLOAD_ISOLATION_MARKER
        + b"\r\n"
        + WORKLOAD_STARTED_MARKER
        + b"\r\n"
        + WORKLOAD_SIGNAL_ARMED_MARKER
        + b"\r\n"
    )
    if retained:
        payload += (
            (LIFECYCLE_PEER_BOUNDARY_MARKER + b"\r\n") * 2
            + LIFECYCLE_PARTIAL_BUFFERED_MARKER
            + b"\r\n"
            + LIFECYCLE_PEER_BOUNDARY_MARKER
            + b"\r\n"
        )
    payload += LIFECYCLE_STOP_DISPATCHED_MARKER + b"\r\n"
    if retained:
        payload += (
            LIFECYCLE_STOP_DUPLICATE_MARKER
            + b"\r\n"
            + LIFECYCLE_PEER_BOUNDARY_MARKER
            + b"\r\n"
            + SUCCESS_MARKER
            + b"\r\n"
            + LIFECYCLE_PEER_BOUNDARY_MARKER
            + b"\r\n"
        )
        return payload
    return payload + SUCCESS_MARKER + b"\r\n"


def _workload_negative_consoles() -> dict[str, bytes]:
    return {
        name: b"kernel boot\n" + ROOT_TRANSITION_MARKER + b"\n" + marker + b"\n"
        for name, marker in WORKLOAD_NEGATIVE_REJECTION_MARKERS.items()
    }


def _lifecycle_negative_consoles() -> dict[str, bytes]:
    result = {}
    for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES:
        contract = lifecycle_negative_control_contract(name)
        payload = b"kernel boot\n" + ROOT_TRANSITION_MARKER + b"\n"
        if contract["phase"] == "post-workload":
            payload += WORKLOAD_STARTED_MARKER + b"\n"
        if name == "hello_reused_nonce":
            payload += LIFECYCLE_PEER_BOUNDARY_MARKER + b"\n"
        if name == "second_distinct_stop":
            payload += LIFECYCLE_STOP_DISPATCHED_MARKER + b"\n"
        result[name] = payload + contract["rejection_marker"].encode("ascii") + b"\n"
    return result


def _lifecycle_negative_inputs() -> dict[str, dict[str, object]]:
    binding = _lifecycle_control_binding_for_test()
    generation = "33333333-3333-4333-8333-333333333333"
    result = {}
    for name in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES:
        selected_generation = (
            generation if name.startswith("stop_") or name in {"hello_reused_nonce", "second_distinct_stop"} else None
        )
        payload = _lifecycle_negative_wire_bytes(name, binding, boot_generation=selected_generation)
        result[name] = {
            "boot_generation": selected_generation,
            "bytes_written": len(payload),
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    return result


def _lifecycle_control_binding_for_test() -> OCIControlBinding:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    return OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)


def test_receipt_projection_rebinds_digest_after_adding_connection() -> None:
    binding = OCIControlV2Binding(
        "f6f546e2-e734-4920-9eff-1762b348a249",
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    )
    control = HostOCIControlV2Session(
        binding,
        nonce_factory=lambda: "1" * 64,
        boot_attempt_factory=lambda: "aca88126-d991-4de8-b66b-90dc07904dff",
    )
    hello = control.hello()
    projection = control.transcript_projection(hello, encode_frame(hello))
    receipt_projection = _receipt_lifecycle_projection(1, projection)

    assert receipt_projection["connection"] == 1
    assert receipt_projection["projection_digest"] == (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {key: value for key, value in receipt_projection.items() if key != "projection_digest"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert receipt_projection["projection_digest"] != projection["projection_digest"]


def _lifecycle() -> dict[str, object]:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)

    def frame(
        connection: int,
        direction: str,
        kind: str,
        nonce: str,
        generation: str,
        wire: int,
        *,
        request_id: int | None = None,
        reply_to: int | None = None,
    ) -> dict[str, object]:
        carrier = "console-line" if kind == "BOUNDARY_ACK" else "channel-frame"
        token = f"{connection}:{direction}:{kind}:{nonce}:{wire}".encode()
        digest = "sha256:" + hashlib.sha256(token).hexdigest()
        value: dict[str, object] = {
            "authentication_verified": kind != "HELLO",
            "body_digest": digest,
            "boot_attempt_id": (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                if generation.startswith("1")
                else "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            ),
            "boot_generation": None if kind == "HELLO" else generation,
            "carrier": carrier,
            "connection": connection,
            "direction": direction,
            "envelope_digest": digest,
            "epoch": connection,
            "host_nonce": nonce,
            "key_id": None if kind == "HELLO" else "sha256:" + "c" * 64,
            "kind": kind,
            "reply_to": reply_to,
            "request_id": request_id,
            "size_bytes": 1336 if kind == "BOUNDARY_ACK" else 800,
            "wire_sequence": wire,
        }
        value["projection_digest"] = (
            "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        )
        return value

    generation1 = "11111111-1111-4111-8111-111111111111"
    nonce1 = "1" * 64
    normal_frames = [
        frame(1, "host-to-guest", "HELLO", nonce1, generation1, 1, request_id=1),
        frame(1, "guest-to-host", "BOOTSTRAP", nonce1, generation1, 1, reply_to=1),
        frame(1, "host-to-guest", "KEY_ACK", nonce1, generation1, 2, reply_to=1),
        frame(1, "guest-to-host", "READY", nonce1, generation1, 2, reply_to=2),
        frame(1, "host-to-guest", "STOP", nonce1, generation1, 3, request_id=2),
        frame(1, "guest-to-host", "TERMINAL", nonce1, generation1, 3, reply_to=2),
    ]
    generation2 = "22222222-2222-4222-8222-222222222222"
    nonces = {index: str(index + 1) * 64 for index in range(1, 7)}
    composite_frames = [
        frame(1, "host-to-guest", "HELLO", nonces[1], generation2, 1, request_id=1),
        frame(1, "guest-to-host", "BOOTSTRAP", nonces[1], generation2, 1, reply_to=1),
        frame(1, "host-to-guest", "KEY_ACK", nonces[1], generation2, 2, reply_to=1),
        frame(1, "guest-to-host", "BOUNDARY_ACK", nonces[1], generation2, 3, reply_to=1),
        frame(2, "host-to-guest", "RECONNECT", nonces[2], generation2, 3, request_id=2),
        frame(2, "guest-to-host", "BOUNDARY_ACK", nonces[2], generation2, 5, reply_to=2),
        frame(3, "host-to-guest", "RECONNECT", nonces[3], generation2, 4, request_id=3),
        frame(3, "guest-to-host", "SNAPSHOT", nonces[3], generation2, 6, reply_to=3),
        frame(3, "guest-to-host", "BOUNDARY_ACK", nonces[3], generation2, 7, reply_to=3),
        frame(4, "host-to-guest", "RECONNECT", nonces[4], generation2, 6, request_id=5),
        frame(4, "guest-to-host", "SNAPSHOT", nonces[4], generation2, 8, reply_to=5),
        frame(4, "host-to-guest", "STOP", nonces[4], generation2, 7, request_id=4),
        frame(4, "host-to-guest", "STOP", nonces[4], generation2, 8, request_id=4),
        frame(4, "guest-to-host", "BOUNDARY_ACK", nonces[4], generation2, 9, reply_to=5),
        frame(5, "host-to-guest", "RECONNECT", nonces[5], generation2, 9, request_id=6),
        frame(5, "guest-to-host", "SNAPSHOT", nonces[5], generation2, 10, reply_to=6),
        frame(5, "guest-to-host", "TERMINAL", nonces[5], generation2, 11, reply_to=4),
        frame(5, "guest-to-host", "BOUNDARY_ACK", nonces[5], generation2, 12, reply_to=6),
        frame(6, "host-to-guest", "RECONNECT", nonces[6], generation2, 10, request_id=7),
        frame(6, "guest-to-host", "SNAPSHOT", nonces[6], generation2, 13, reply_to=7),
    ]
    partial_size = 800
    boots = [
        {
            "connection_count": 1,
            "frames": normal_frames,
            "initial_ready_host_observed": True,
            "logical_attempts": [],
            "peer_boundary_marker_count": 0,
            "partial_frame_buffered_marker_count": 0,
            "pid1_alive_after_terminal": True,
            "profile": "single-connection",
            "ready": True,
            "reopen_count": 0,
            "stop_signal_dispatch_count": 1,
            "terminal": {"exit_code": 42, "signal": None},
        },
        {
            "connection_count": 6,
            "frames": composite_frames,
            "initial_ready_host_observed": False,
            "logical_attempts": [
                {
                    "bytes_sent": partial_size - 1,
                    "connection": 3,
                    "digest": "sha256:" + "d" * 64,
                    "frame_size_bytes": partial_size,
                    "kind": "STOP",
                    "request_id": 4,
                }
            ],
            "peer_boundary_marker_count": 5,
            "partial_frame_buffered_marker_count": 1,
            "pid1_alive_after_terminal": True,
            "profile": "six-connection-partial-retry-committed-dedupe-composite",
            "ready": True,
            "reopen_count": 0,
            "stop_signal_dispatch_count": 1,
            "terminal": {"exit_code": 42, "signal": None},
        },
    ]
    negative_consoles = _lifecycle_negative_consoles()
    negative_inputs = _lifecycle_negative_inputs()

    def negative_item(name: str) -> dict[str, object]:
        contract = lifecycle_negative_control_contract(name)
        return {
            "console_digest": "sha256:" + hashlib.sha256(negative_consoles[name]).hexdigest(),
            "console_size_bytes": len(negative_consoles[name]),
            "contract": contract,
            "immutable_backings_verified": True,
            "lifecycle_rejection_marker_count": 1,
            "peer_boundary_marker_count": 1 if name == "hello_reused_nonce" else 0,
            "pid1_alive_after_marker": True,
            "root_post_digest": "sha256:" + "9" * 64,
            "root_seed_digest": contract["backings"]["root"]["artifact_digest"],
            "success_marker_count": 0,
            "terminal_marker_count": 0,
            "wire_input": negative_inputs[name] if name in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES else None,
        }

    duplicate_output = QEMU_DUPLICATE_NAME_REJECTION_MARKER + b"\n"
    return {
        "binding": {
            "domain_core_digest": plan.domain_core_digest,
            "run_id": plan.run_id,
            "stage1_artifact_digest": transport.receipt.artifact_digest,
        },
        "boots": boots,
        "broker_contract": "palimpsest.guest-lifecycle-broker.v3",
        "channel_discovery_negative_controls": {
            name: negative_item(name) for name in LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES
        },
        "channel_name": "org.palimpsest.oci.lifecycle.0",
        "nonce_semantics": "authenticated-epoch-challenge-and-replay-ledger",
        "connection_limit": 16,
        "natural_terminal_proven": False,
        "negative_input_proven": True,
        "peer_identity": "socket-dev-ino-uid-type-plus-linux-so-peercred-qemu-pid.v1",
        "production_reconnect_requirement": "authenticated-console-boundary-ack.v2",
        "protocol": "palimpsest.oci-lifecycle-control.v2",
        "qemu_duplicate_name_rejected": {
            "exit_code": 1,
            "guest_boot_started": False,
            "invocation_count": 1,
            "nonzero_exit": True,
            "output_digest": "sha256:" + hashlib.sha256(duplicate_output).hexdigest(),
            "output_size_bytes": len(duplicate_output),
            "rejection_marker": QEMU_DUPLICATE_NAME_REJECTION_MARKER.decode("ascii"),
            "rejection_marker_count": 1,
            "stage1_marker_count": 0,
        },
        "reconnect_boundary": "authenticated-console-boundary-ack.v2",
        "reconnect_proven": True,
        "rapid_reconnect_proven": False,
        "session_profile": "authenticated-bootstrap-boundary-reconnect-partial-retry-dedupe.v2",
        "single_connection_proven": True,
        "transport": "qemu-private-unix-socket-to-virtio-serial",
        "wire_negative_controls": {name: negative_item(name) for name in LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES},
    }


def _receipt() -> OCIStage1KVMProofReceipt:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    initramfs = build_bootstrap_initramfs()
    uid0_plan = build_uid0_isolation_proof_plan()
    uid0_transport = build_stage1_transport(uid0_plan)
    uid0_frames = copy.deepcopy(_lifecycle()["boots"][0]["frames"])
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
        _positive_console(retained=True),
        _positive_console(),
        "sha256:" + "a" * 64,
        _negative_consoles(),
        _filesystem_negative_consoles(),
        _assembly_negative_consoles(),
        {name: "sha256:" + "6" * 64 for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES},
        _root_transition_negative_consoles(),
        {name: "sha256:" + "7" * 64 for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES},
        _workload_negative_consoles(),
        {name: "sha256:" + "8" * 64 for name in WORKLOAD_NEGATIVE_CONTROL_NAMES},
        _lifecycle_negative_consoles(),
        {name: "sha256:" + "9" * 64 for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES},
        QEMU_DUPLICATE_NAME_REJECTION_MARKER + b"\n",
        _lifecycle(),
        _uid0_lifecycle_evidence(uid0_plan, uid0_transport, uid0_frames),
    )


def _decode_receipt(value: dict[str, object], receipt: OCIStage1KVMProofReceipt) -> OCIStage1KVMProofReceipt:
    return OCIStage1KVMProofReceipt.from_dict(
        value,
        console=receipt.console,
        negative_consoles=receipt.negative_consoles,
        filesystem_negative_consoles=receipt.filesystem_negative_consoles,
        retained_console=receipt.retained_console,
        uid0_isolation_console=receipt.uid0_isolation_console,
        assembly_negative_consoles=receipt.assembly_negative_consoles,
        assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
        root_transition_negative_consoles=receipt.root_transition_negative_consoles,
        root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
        workload_negative_consoles=receipt.workload_negative_consoles,
        workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
        lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
        lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
        qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
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
    assert manifest_digest == "sha256:b9220e29d4b306e7e564bb506ec7f128f22ec8abe2149bf8fbd3421654109cf9"
    assert topology["fixture_policy"] == "palimpsest.kvm-actual-filesystem-fixtures.v9"
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
        "elf_sha256": "fac936a406b773e5a041b27d8bf91ac702f93060467b4ec83ac0b5c34751bed7",
        "elf_size_bytes": 9868,
        "source": "guest/workload-proof/proof.c",
        "source_sha256": "4934d9abc8d695968050b7f2b319906e8819b239919481e20251ed9a0d2aafab",
        "toolchain": "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1",
    }


def test_uid0_topology_requires_explicit_exact_mode(tmp_path: Path) -> None:
    base = build_proof_plan()
    uid0 = build_uid0_isolation_proof_plan()
    with pytest.raises(ArtifactValidationError, match="topology plan"):
        pre_mount_topology(uid0)
    with pytest.raises(ArtifactValidationError, match="topology plan"):
        pre_mount_topology(base, mode="uid0")
    assert pre_mount_topology(uid0, mode="uid0")["devices"] == pre_mount_topology(base)["devices"]

    fixtures = load_proof_filesystems()
    root = _write(tmp_path / "root.raw", fixtures.root, 0o600)
    lowers = tuple(
        _write(tmp_path / f"lower-{index}.raw", payload, 0o400) for index, payload in enumerate(fixtures.lowers)
    )
    assert _verify_post_run_topology(root, lowers, uid0, mode="uid0") == fixtures.root_digest
    with pytest.raises(ArtifactValidationError, match="topology plan"):
        _verify_post_run_topology(root, lowers, uid0)
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


def test_console_reader_accepts_only_expected_discovery_negative_rejection() -> None:
    name = "lifecycle_missing_port"
    rejected = lifecycle_negative_control_contract(name)["rejection_marker"].encode("ascii")
    program = f"import sys,time;sys.stdout.buffer.write({rejected!r}+b'\\n');sys.stdout.flush();time.sleep(2)"
    console = _read_console_until(
        (sys.executable, "-c", program),
        expected=rejected,
        forbidden=(SUCCESS_MARKER,),
        timeout_seconds=3,
        require_alive_after_marker=True,
        lifecycle_scenario="discovery-negative",
        lifecycle_negative_name=name,
    )
    assert _logical_line_count(console, rejected) == 1


def test_console_reader_drives_fragmented_single_connection_lifecycle(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    temporary = tempfile.TemporaryDirectory(prefix="pali-lifecycle-", dir="/tmp")
    channel = (Path(temporary.name) / "lifecycle.sock").resolve()
    program = f"""
import socket, struct, sys, time
from palimpsest_local.oci_control_protocol_v2 import OCIControlV2Message, decode_frame, encode_frame, sign_message, verify_message_authentication

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
key = bytes(range(32))
bootstrap = sign_message(OCIControlV2Message(kind='BOOTSTRAP', binding=hello.body.binding,
    boot_attempt_id=hello.body.boot_attempt_id, host_nonce=hello.body.host_nonce, epoch=1,
    wire_sequence=1, payload={{'boot_key': key.hex()}}, boot_generation=generation,
    reply_to=hello.body.request_id), key)
connection.sendall(encode_frame(bootstrap))
key_ack = receive(connection); verify_message_authentication(key_ack, key)
ready = sign_message(OCIControlV2Message(kind='READY', binding=hello.body.binding,
    boot_attempt_id=hello.body.boot_attempt_id, host_nonce=hello.body.host_nonce, epoch=1,
    wire_sequence=2, payload={{}}, boot_generation=generation, reply_to=key_ack.body.wire_sequence), key)
frame = encode_frame(ready)
for part in (frame[:1], frame[1:3], frame[3:]): connection.sendall(part)
for marker in ({ROOT_TRANSITION_MARKER!r}, {WORKLOAD_STARTED_MARKER!r}, {WORKLOAD_SIGNAL_ARMED_MARKER!r}):
    sys.stdout.buffer.write(marker + b'\\n'); sys.stdout.flush()
stop = receive(connection)
verify_message_authentication(stop, key)
terminal = sign_message(OCIControlV2Message(kind='TERMINAL', binding=hello.body.binding,
    boot_attempt_id=hello.body.boot_attempt_id, host_nonce=hello.body.host_nonce, epoch=1,
    wire_sequence=3, payload={{'terminal': {{'exit_code': 42, 'signal': None}}}},
    boot_generation=generation, reply_to=stop.body.request_id), key)
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
    assert [frame["kind"] for frame in transcript] == ["HELLO", "BOOTSTRAP", "KEY_ACK", "READY", "STOP", "TERMINAL"]
    assert [frame["direction"] for frame in transcript] == [
        "host-to-guest",
        "guest-to-host",
        "host-to-guest",
        "guest-to-host",
        "host-to-guest",
        "guest-to-host",
    ]
    assert len({frame["boot_generation"] for frame in transcript[1:]}) == 1
    temporary.cleanup()


def test_console_reader_accepts_workload_failure_after_v2_key_ack(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    temporary = tempfile.TemporaryDirectory(prefix="pali-lifecycle-workload-failure-", dir="/tmp")
    channel = (Path(temporary.name) / "lifecycle.sock").resolve()
    expected = WORKLOAD_NEGATIVE_REJECTION_MARKERS["workload_missing_executable"]
    program = f"""
import socket, struct, sys, time
from palimpsest_local.oci_control_protocol_v2 import OCIControlV2Message, decode_frame, encode_frame, sign_message, verify_message_authentication

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
key = bytes(range(32))
bootstrap = sign_message(OCIControlV2Message(kind='BOOTSTRAP', binding=hello.body.binding,
    boot_attempt_id=hello.body.boot_attempt_id, host_nonce=hello.body.host_nonce, epoch=1,
    wire_sequence=1, payload={{'boot_key': key.hex()}},
    boot_generation='11111111-1111-4111-8111-111111111111', reply_to=hello.body.request_id), key)
connection.sendall(encode_frame(bootstrap))
key_ack = receive(connection)
verify_message_authentication(key_ack, key)
sys.stdout.buffer.write({expected!r} + b'\\n')
sys.stdout.flush()
time.sleep(2)
"""
    console = _read_console_until(
        (sys.executable, "-c", program, os.fspath(channel)),
        expected=expected,
        forbidden=(SUCCESS_MARKER, WORKLOAD_STARTED_MARKER),
        timeout_seconds=3,
        require_alive_after_marker=True,
        lifecycle_socket_path=channel,
        lifecycle_binding=binding,
        lifecycle_success=False,
    )
    assert _logical_line_count(console, expected) == 1
    temporary.cleanup()


def test_console_reader_drives_exact_six_connection_reconnect_composite(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    temporary = tempfile.TemporaryDirectory(prefix="pali-lifecycle-composite-", dir="/tmp")
    channel = (Path(temporary.name) / "lifecycle.sock").resolve()
    program = f"""
import hashlib, socket, struct, sys, time
from palimpsest_local.oci_control_protocol_v2 import (OCIControlV2Message, OCIControlV2Envelope,
    decode_frame, encode_boundary_line, encode_frame, lifecycle_state_digest, sign_message,
    verify_message_authentication)

def receive(connection, partial=False):
    header = b''
    while len(header) < 4:
        try: chunk = connection.recv(4-len(header))
        except ConnectionResetError: return None
        if not chunk: return None
        header += chunk
    size = struct.unpack('>I', header)[0]
    payload = b''
    expected = size - (1 if partial else 0)
    while len(payload) < expected:
        try: chunk = connection.recv(expected - len(payload))
        except ConnectionResetError: return None
        if not chunk: return None
        payload += chunk
    if partial:
        sys.stdout.buffer.write({LIFECYCLE_PARTIAL_BUFFERED_MARKER!r} + b'\\n'); sys.stdout.flush()
        try:
            while connection.recv(4096): pass
        except ConnectionResetError:
            pass
        return len(payload), size
    return decode_frame(header + payload)

def drain(connection):
    try:
        while connection.recv(4096): pass
    except ConnectionResetError:
        pass

def public_state(name, stop_id=None, terminal=None):
    return {{'state': name, 'stop_request_id': stop_id, 'terminal': terminal}}

def send_message(connection, kind, payload, reply_to):
    global guest_wire
    guest_wire += 1
    message = OCIControlV2Message(kind, binding, attempt, nonce, epoch, guest_wire, payload,
        boot_generation=generation, reply_to=reply_to)
    connection.sendall(encode_frame(sign_message(message, key)))

def send_boundary(state, last_host_wire, header_used=0, payload_used=0, payload_expected=0):
    global epoch, guest_wire
    previous = epoch; epoch += 1; guest_wire += 1
    boundary_id = f'44444444-4444-4444-8444-{{epoch:012d}}'
    digest = lifecycle_state_digest(state['state'], stop_request_id=state['stop_request_id'],
        terminal=state['terminal'])
    message = OCIControlV2Message('BOUNDARY_ACK', binding, attempt, nonce, epoch, guest_wire,
        {{'boundary_id': boundary_id, 'discarded_header_bytes': header_used,
         'discarded_payload_bytes': payload_used, 'discarded_payload_expected': payload_expected,
         'last_accepted_h2g_wire_sequence': last_host_wire,
         'last_attempted_g2h_wire_sequence': guest_wire, 'lifecycle_state': state,
         'previous_epoch': previous, 'state_digest': digest}},
        boot_generation=generation, reply_to=opener)
    line = encode_boundary_line(sign_message(message, key, carrier='console-line'))
    sys.stdout.buffer.write(line); sys.stdout.flush()

listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1]); listener.listen(1)
generation = '22222222-2222-4222-8222-222222222222'
key = bytes(range(32)); epoch = 1; guest_wire = 0
c, _ = listener.accept(); hello = receive(c); binding = hello.body.binding
attempt = hello.body.boot_attempt_id; nonce = hello.body.host_nonce; opener = hello.body.request_id
guest_wire += 1
bootstrap = OCIControlV2Message('BOOTSTRAP', binding, attempt, nonce, epoch, guest_wire,
    {{'boot_key': key.hex()}}, boot_generation=generation, reply_to=opener)
c.sendall(encode_frame(sign_message(bootstrap, key)))
key_ack = receive(c); verify_message_authentication(key_ack, key); last_host_wire = key_ack.body.wire_sequence
send_message(c, 'READY', {{}}, last_host_wire)
for marker in ({ROOT_TRANSITION_MARKER!r}, {WORKLOAD_STARTED_MARKER!r}, {WORKLOAD_SIGNAL_ARMED_MARKER!r},
               {LIFECYCLE_READY_COMMITTED_MARKER!r}):
    sys.stdout.buffer.write(marker + b'\\n'); sys.stdout.flush()
drain(c)
send_boundary(public_state('ready'), last_host_wire)
c, _ = listener.accept(); reconnect = receive(c); verify_message_authentication(reconnect, key)
nonce = reconnect.body.host_nonce; opener = reconnect.body.request_id; last_host_wire = reconnect.body.wire_sequence
try: send_message(c, 'SNAPSHOT', {{'state': 'ready', 'stop_request_id': None, 'terminal': None}}, opener)
except BrokenPipeError: pass
drain(c)
send_boundary(public_state('ready'), last_host_wire)
c, _ = listener.accept(); reconnect = receive(c); verify_message_authentication(reconnect, key)
nonce = reconnect.body.host_nonce; opener = reconnect.body.request_id; last_host_wire = reconnect.body.wire_sequence
send_message(c, 'SNAPSHOT', {{'state': 'ready', 'stop_request_id': None, 'terminal': None}}, opener)
payload_used, payload_expected = receive(c, partial=True)
send_boundary(public_state('ready'), last_host_wire, payload_used=payload_used, payload_expected=payload_expected)
c, _ = listener.accept(); reconnect = receive(c); verify_message_authentication(reconnect, key)
nonce = reconnect.body.host_nonce; opener = reconnect.body.request_id; last_host_wire = reconnect.body.wire_sequence
send_message(c, 'SNAPSHOT', {{'state': 'ready', 'stop_request_id': None, 'terminal': None}}, opener)
stop = receive(c); verify_message_authentication(stop, key); last_host_wire = stop.body.wire_sequence
sys.stdout.buffer.write({LIFECYCLE_STOP_DISPATCHED_MARKER!r} + b'\\n'); sys.stdout.flush()
duplicate = receive(c); verify_message_authentication(duplicate, key)
assert duplicate.body.request_id == stop.body.request_id and duplicate.body.wire_sequence > stop.body.wire_sequence
last_host_wire = duplicate.body.wire_sequence
sys.stdout.buffer.write({LIFECYCLE_STOP_DUPLICATE_MARKER!r} + b'\\n'); sys.stdout.flush()
sys.stdout.buffer.write({WORKLOAD_STOP_OBSERVED_MARKER!r} + b'\\n'); sys.stdout.flush()
drain(c)
send_boundary(public_state('stopping', stop.body.request_id), last_host_wire)
c, _ = listener.accept(); reconnect = receive(c); verify_message_authentication(reconnect, key)
nonce = reconnect.body.host_nonce; opener = reconnect.body.request_id; last_host_wire = reconnect.body.wire_sequence
send_message(c, 'SNAPSHOT', {{'state': 'stopping', 'stop_request_id': stop.body.request_id, 'terminal': None}}, opener)
terminal = {{'exit_code': 42, 'signal': None}}
send_message(c, 'TERMINAL', {{'terminal': terminal}}, stop.body.request_id)
sys.stdout.buffer.write({SUCCESS_MARKER!r} + b'\\n'); sys.stdout.flush()
drain(c)
send_boundary(public_state('terminal', stop.body.request_id, terminal), last_host_wire)
c, _ = listener.accept(); reconnect = receive(c); verify_message_authentication(reconnect, key)
nonce = reconnect.body.host_nonce; opener = reconnect.body.request_id
send_message(c, 'SNAPSHOT', {{'state': 'terminal', 'stop_request_id': stop.body.request_id,
    'terminal': terminal}}, opener)
time.sleep(2)
"""
    transcript: list[dict[str, object]] = []
    attempts: list[dict[str, object]] = []
    console = _read_console_until(
        (sys.executable, "-c", program, os.fspath(channel)),
        expected=SUCCESS_MARKER,
        forbidden=(REJECTION_MARKER,),
        timeout_seconds=5,
        require_alive_after_marker=True,
        lifecycle_socket_path=channel,
        lifecycle_binding=binding,
        lifecycle_success=True,
        lifecycle_transcript=transcript,
        lifecycle_scenario="composite",
        lifecycle_attempts=attempts,
    )
    assert _logical_line_count(console, WORKLOAD_STOP_OBSERVED_MARKER) == 1
    assert _logical_line_count(console, LIFECYCLE_STOP_DISPATCHED_MARKER) == 1
    assert _logical_line_count(console, LIFECYCLE_STOP_DUPLICATE_MARKER) == 1
    assert _logical_line_count(console, LIFECYCLE_PEER_BOUNDARY_MARKER) == 5
    assert _logical_line_count(console, LIFECYCLE_PARTIAL_BUFFERED_MARKER) == 1
    assert [frame["kind"] for frame in transcript].count("BOUNDARY_ACK") == 5
    assert [frame["kind"] for frame in transcript].count("RECONNECT") == 5
    assert attempts[0]["kind"] == "STOP"
    stops = [frame for frame in transcript if frame["kind"] == "STOP"]
    assert len(stops) == 2 and stops[0]["request_id"] == stops[1]["request_id"]
    assert stops[0]["wire_sequence"] < stops[1]["wire_sequence"]
    temporary.cleanup()


def test_console_reader_records_only_rejected_second_distinct_stop(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    temporary = tempfile.TemporaryDirectory(prefix="pali-lifecycle-negative-", dir="/tmp")
    channel = (Path(temporary.name) / "lifecycle.sock").resolve()
    rejection = lifecycle_negative_control_contract("second_distinct_stop")["rejection_marker"].encode("ascii")
    program = f"""
import socket, struct, sys, time
from palimpsest_local.oci_control_protocol_v2 import (OCIControlV2Message, decode_frame, encode_frame,
    sign_message, verify_message_authentication)

def receive(connection):
    header = connection.recv(4)
    while len(header) < 4: header += connection.recv(4-len(header))
    size = struct.unpack('>I', header)[0]
    payload = b''
    while len(payload) < size: payload += connection.recv(size-len(payload))
    return decode_frame(header + payload)

listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1]); listener.listen(1)
connection, _ = listener.accept()
hello = receive(connection)
generation = '33333333-3333-4333-8333-333333333333'
key = bytes(range(32))
bootstrap = OCIControlV2Message('BOOTSTRAP', hello.body.binding, hello.body.boot_attempt_id,
    hello.body.host_nonce, 1, 1, {{'boot_key': key.hex()}}, boot_generation=generation,
    reply_to=hello.body.request_id)
connection.sendall(encode_frame(sign_message(bootstrap, key)))
key_ack = receive(connection); verify_message_authentication(key_ack, key)
ready = OCIControlV2Message('READY', hello.body.binding, hello.body.boot_attempt_id,
    hello.body.host_nonce, 1, 2, {{}}, boot_generation=generation,
    reply_to=key_ack.body.wire_sequence)
connection.sendall(encode_frame(sign_message(ready, key)))
for marker in ({ROOT_TRANSITION_MARKER!r}, {WORKLOAD_STARTED_MARKER!r}, {WORKLOAD_SIGNAL_ARMED_MARKER!r}):
    sys.stdout.buffer.write(marker + b'\\n'); sys.stdout.flush()
first = receive(connection); verify_message_authentication(first, key); assert first.body.request_id == 2
sys.stdout.buffer.write({LIFECYCLE_STOP_DISPATCHED_MARKER!r} + b'\\n'); sys.stdout.flush()
second = receive(connection); verify_message_authentication(second, key); assert second.body.request_id == 3
sys.stdout.buffer.write({rejection!r} + b'\\n'); sys.stdout.flush()
time.sleep(2)
"""
    negative_input: dict[str, object] = {}
    console = _read_console_until(
        (sys.executable, "-c", program, os.fspath(channel)),
        expected=rejection,
        forbidden=(REJECTION_MARKER,),
        timeout_seconds=4,
        require_alive_after_marker=True,
        lifecycle_socket_path=channel,
        lifecycle_binding=binding,
        lifecycle_success=True,
        lifecycle_scenario="negative",
        lifecycle_negative_name="second_distinct_stop",
        lifecycle_negative_input=negative_input,
    )
    assert negative_input["boot_generation"] == "33333333-3333-4333-8333-333333333333"
    assert isinstance(negative_input["bytes_written"], int) and negative_input["bytes_written"] > 0
    assert negative_input["bytes_written"] == negative_input["size_bytes"]
    assert isinstance(negative_input["digest"], str)
    assert len(negative_input["digest"]) == 71 and negative_input["digest"].startswith("sha256:")
    assert _logical_line_count(console, LIFECYCLE_STOP_DISPATCHED_MARKER) == 1
    temporary.cleanup()


def test_console_reader_normalizes_partial_guest_frame_eof(tmp_path: Path) -> None:
    plan = build_proof_plan()
    transport = build_stage1_transport(plan)
    binding = OCIControlBinding(plan.run_id, plan.domain_core_digest, transport.receipt.artifact_digest)
    temporary = tempfile.TemporaryDirectory(prefix="pali-lifecycle-eof-", dir="/tmp")
    channel = (Path(temporary.name) / "lifecycle.sock").resolve()
    program = """
import socket, sys, time
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1]); listener.listen(1)
connection, _ = listener.accept()
connection.recv(65536)
connection.sendall(b'\\x00\\x00')
connection.close()
time.sleep(1)
"""
    with pytest.raises(KVMProofFailure, match="lifecycle protocol was rejected"):
        _read_console_until(
            (sys.executable, "-c", program, os.fspath(channel)),
            expected=SUCCESS_MARKER,
            forbidden=(REJECTION_MARKER,),
            timeout_seconds=3,
            require_alive_after_marker=True,
            lifecycle_socket_path=channel,
            lifecycle_binding=binding,
            lifecycle_success=True,
        )
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
            uid0_isolation_console=receipt.uid0_isolation_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
            lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
        )
        == receipt
    )
    assert decoded["qemu"]["artifact_digest"] == "sha256:" + "3" * 64
    assert decoded["schema"] == "palimpsest.oci-stage1-kvm-proof.v16"
    assert decoded["executed_boots"] == 41
    assert decoded["qemu_invocations"] == 42
    assert LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES == (
        "lifecycle_missing_port",
        "lifecycle_wrong_name_only",
    )
    assert LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES == (
        "hello_zero_length",
        "hello_oversized_length",
        "hello_duplicate_key_noncanonical",
        "hello_wrong_domain_core_binding",
        "hello_reused_nonce",
        "stop_stale_generation",
        "stop_request_id_collides_with_hello",
        "second_distinct_stop",
    )
    assert set(decoded["lifecycle"]["channel_discovery_negative_controls"]) == set(
        LIFECYCLE_CHANNEL_DISCOVERY_NEGATIVE_CONTROL_NAMES
    )
    assert set(decoded["lifecycle"]["wire_negative_controls"]) == set(LIFECYCLE_WIRE_NEGATIVE_CONTROL_NAMES)
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
        "contract": "palimpsest.guest-pid1-supervisor.v7",
        "cgroup": "/palimpsest.workload",
        "cgroup_security": "private-readonly-view-plus-dedicated-cleanup-authority",
        "cgroup_write_escape_denied": ["parent", "own"],
        "cleanup": "stop-signal-grace-cgroup.kill-wait4-echild-populated-zero-rmdir",
        "cooperative_status": 43,
        "credential_timing": "child-isolate-drop-verify-parent-attach-key-bootstrap-ack-release",
        "forced_status": 137,
        "forwarded_signal": 15,
        "lifecycle_broker": "palimpsest.guest-lifecycle-broker.v3",
        "lifecycle_stop": "host-issued-after-ready-and-proof-signal-sync",
        "isolation_contract": "palimpsest.workload-lifecycle-authority-isolation.v2",
        "main_status": 42,
        "pid1_credentials": {"gid": 0, "supplementary_groups": [], "uid": 0},
        "privileged_broker_after_fork": True,
        "process_group": True,
        "reaped_children": 3,
        "terminal_state": "parent-marker-then-fail-closed-wait",
        "terminal_wire_order": "cleanup-certainty-then-terminal-frame-then-console-marker",
        "workload_credentials": {"gid": 65534, "supplementary_groups": [], "uid": 65534},
        "uid0_capabilityless_proven": True,
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


def test_uid0_console_rejection_marker_is_rejected() -> None:
    receipt = _receipt()
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, uid0_isolation_console=receipt.uid0_isolation_console + REJECTION_MARKER + b"\n")


@pytest.mark.parametrize(
    "mutation",
    [
        "argv",
        "user",
        "transport",
        "cmdline",
        "lifecycle-request",
        "lifecycle-sequence",
        "lifecycle-reply",
        "lifecycle-contract",
    ],
)
def test_uid0_receipt_evidence_is_exactly_bound(mutation: str) -> None:
    receipt = _receipt()
    value = copy.deepcopy(receipt.to_dict())
    evidence = value["workload_isolation"]
    if mutation == "argv":
        evidence["plan"]["process"]["argv"][-1] = "wrong-mode"
    elif mutation == "user":
        evidence["plan"]["process"]["user"]["uid"] = "65534"
    elif mutation == "transport":
        evidence["transport"]["serial"] = "0" * 20
    elif mutation == "cmdline":
        evidence["cmdline"]["text"] += " changed"
    elif mutation == "lifecycle-request":
        evidence["lifecycle"]["frames"][0]["request_id"] = 9
    elif mutation == "lifecycle-sequence":
        evidence["lifecycle"]["frames"][1]["wire_sequence"] = 9
    elif mutation == "lifecycle-reply":
        evidence["lifecycle"]["frames"][3]["reply_to"] = 9
    else:
        evidence["lifecycle"]["broker_contract"] = "wrong"
    with pytest.raises(ArtifactValidationError, match="policy|canonical"):
        _decode_receipt(value, receipt)


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
            uid0_isolation_console=receipt.uid0_isolation_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
            lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
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
            uid0_isolation_console=receipt.uid0_isolation_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
            lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
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
            uid0_isolation_console=receipt.uid0_isolation_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
            lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
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
            uid0_isolation_console=receipt.uid0_isolation_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
            lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
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
            uid0_isolation_console=receipt.uid0_isolation_console,
            assembly_negative_consoles=receipt.assembly_negative_consoles,
            assembly_negative_root_post_digests=receipt.assembly_negative_root_post_digests,
            root_transition_negative_consoles=receipt.root_transition_negative_consoles,
            root_transition_negative_root_post_digests=receipt.root_transition_negative_root_post_digests,
            workload_negative_consoles=receipt.workload_negative_consoles,
            workload_negative_root_post_digests=receipt.workload_negative_root_post_digests,
            lifecycle_negative_consoles=receipt.lifecycle_negative_consoles,
            lifecycle_negative_root_post_digests=receipt.lifecycle_negative_root_post_digests,
            qemu_duplicate_name_output=receipt.qemu_duplicate_name_output,
        )


def test_receipt_rejects_valid_looking_lifecycle_root_seed_tamper() -> None:
    receipt = _receipt()
    lifecycle = copy.deepcopy(receipt.lifecycle)
    name = LIFECYCLE_NEGATIVE_CONTROL_NAMES[0]
    lifecycle["channel_discovery_negative_controls"][name]["root_seed_digest"] = "sha256:" + "f" * 64

    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, lifecycle=lifecycle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reconnect_boundary", "uncoordinated"),
        ("production_reconnect_requirement", "none"),
        ("rapid_reconnect_proven", True),
    ],
)
def test_receipt_rejects_reconnect_boundary_claim_tamper(field: str, value: object) -> None:
    receipt = _receipt()
    lifecycle = copy.deepcopy(receipt.lifecycle)
    lifecycle[field] = value
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, lifecycle=lifecycle)


def test_receipt_rejects_peer_boundary_count_tamper() -> None:
    receipt = _receipt()
    lifecycle = copy.deepcopy(receipt.lifecycle)
    lifecycle["boots"][1]["peer_boundary_marker_count"] = 4
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, lifecycle=lifecycle)


def test_receipt_rejects_partial_buffer_and_boundary_reordering() -> None:
    receipt = _receipt()
    needle = LIFECYCLE_PEER_BOUNDARY_MARKER + b"\r\n" + LIFECYCLE_PARTIAL_BUFFERED_MARKER
    replacement = LIFECYCLE_PARTIAL_BUFFERED_MARKER + b"\r\n" + LIFECYCLE_PEER_BOUNDARY_MARKER
    reordered = receipt.retained_console.replace(needle, replacement, 1)
    assert reordered != receipt.retained_console
    with pytest.raises(ArtifactValidationError, match="receipt value"):
        replace(receipt, retained_console=reordered)


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
        lifecycle["reconnect_proven"] = False
    elif mutation == "negative-proven":
        lifecycle["negative_input_proven"] = False
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
        lifecycle["boots"][0]["frames"][3]["wire_sequence"] = 3
    elif mutation == "digest":
        lifecycle["boots"][0]["frames"][0]["envelope_digest"] = "sha256:bad"
    elif mutation == "valid-looking-digest":
        lifecycle["boots"][0]["frames"][0]["envelope_digest"] = "sha256:" + "f" * 64
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
        "uid0-isolation-console.bin",
        "receipt.json",
        *(f"negative-{name}.bin" for name in NEGATIVE_CONTROL_NAMES),
        *(f"filesystem-negative-{name}.bin" for name in FILESYSTEM_NEGATIVE_CONTROL_NAMES),
        *(f"assembly-negative-{name}.bin" for name in ASSEMBLY_NEGATIVE_CONTROL_NAMES),
        *(f"root-transition-negative-{name}.bin" for name in ROOT_TRANSITION_NEGATIVE_CONTROL_NAMES),
        *(f"workload-negative-{name}.bin" for name in WORKLOAD_NEGATIVE_CONTROL_NAMES),
        *(f"lifecycle-negative-{name}.bin" for name in LIFECYCLE_NEGATIVE_CONTROL_NAMES),
        "qemu-duplicate-lifecycle-name.bin",
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    output = _secure_write(evidence.resolve(), EVIDENCE_FILE_NAMES[2], b"console", mode=0o400)
    assert output.read_bytes() == b"console"
    assert output.stat().st_mode & 0o777 == 0o400
    with pytest.raises(ArtifactValidationError, match="cannot be published"):
        _secure_write(evidence.resolve(), EVIDENCE_FILE_NAMES[2], b"replacement", mode=0o400)
