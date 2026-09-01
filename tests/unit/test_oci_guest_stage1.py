"""Portable adversarial tests for the guest-side stage-1 consumer contract."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError, StateError
from palimpsest_local.oci_guest_stage1 import (
    MAX_GUEST_KERNEL_CMDLINE_BYTES,
    GuestBlockCandidate,
    discover_stage1_block_device,
    expected_pre_mount_block_devices,
    parse_guest_kernel_cmdline,
    select_pre_mount_block_devices,
    select_stage1_block_device,
    verify_guest_stage1_transport,
)
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec
from palimpsest_local.oci_provenance import canonical_json_bytes
from palimpsest_local.oci_root_volume import MAX_OCI_ROOT_VOLUME_GENERATION
from palimpsest_local.oci_stage1 import OCIStage1Plan, oci_stage1_device_serial
from palimpsest_local.oci_stage1_transport import build_stage1_transport

_HEADER = struct.Struct("<16sIIQ32s")


def _plan() -> OCIStage1Plan:
    return OCIStage1Plan(
        run_id="f6f546e2-e734-4920-9eff-1762b348a249",
        run_name="guest-consumer",
        boot_plan_digest="sha256:" + "a" * 64,
        domain_core_digest="sha256:" + "b" * 64,
        root={
            "filesystem": "ext4",
            "generation": 3,
            "mount_options": ["rw", "nodev", "nosuid"],
            "serial": oci_stage1_device_serial("root", "1fd7a60e-fdb2-4877-91d3-148bbca3884f"),
            "size_bytes": 16 * 1024 * 1024,
            "volume_id": "1fd7a60e-fdb2-4877-91d3-148bbca3884f",
        },
        layers=(
            {
                "filesystem": "squashfs",
                "image_digest": "sha256:" + "2" * 64,
                "mount_options": ["ro", "nodev", "nosuid"],
                "occurrence_digest": "sha256:" + "3" * 64,
                "ordinal": 0,
                "serial": oci_stage1_device_serial("lower", "sha256:" + "3" * 64),
                "size_bytes": 4096,
            },
        ),
        process=OCIProcessSpec(
            ("/usr/bin/demo", "--serve"),
            (("LANG", "C.UTF-8"),),
            "/srv",
            OCIUserSpec("1000", "1000"),
            15,
        ),
    )


def _cmdline(
    artifact_digest: str,
    *,
    resource: str | None = None,
    core: str | None = None,
    root: str | None = None,
    lowers: str | None = None,
) -> str:
    plan = _plan()
    lower_devices = lowers or ",".join(f"virtio-{layer['serial']}" for layer in plan.layers)
    root_device = root or f"virtio-{plan.root['serial']}"
    transport_serial = hashlib.sha256(
        f"palimpsest-oci-root-stage1-transport-v1\0{artifact_digest}".encode()
    ).hexdigest()[:20]
    return (
        "console=ttyS0 rdinit=/init "
        f"palimpsest.resource={resource or plan.boot_plan_digest} "
        f"palimpsest.core={core or plan.domain_core_digest} "
        f"palimpsest.stage1={artifact_digest} "
        f"palimpsest.stage1dev=virtio-{transport_serial} "
        f"palimpsest.root={root_device} "
        f"palimpsest.lowers={lower_devices}"
    )


def test_guest_consumer_cross_binds_cmdline_envelope_and_full_plan() -> None:
    built = build_stage1_transport(_plan())
    bindings = parse_guest_kernel_cmdline(_cmdline(built.receipt.artifact_digest).encode("ascii"))

    verified = verify_guest_stage1_transport(built.artifact, bindings)

    assert verified.plan == _plan()
    assert (
        verified.bindings.transport_serial
        == hashlib.sha256(
            f"palimpsest-oci-root-stage1-transport-v1\0{built.receipt.artifact_digest}".encode()
        ).hexdigest()[:20]
    )
    assert verified.bindings.root_serial == _plan().root["serial"]
    assert verified.bindings.lower_serials == (_plan().layers[0]["serial"],)
    assert verified.artifact_size_bytes == len(built.artifact)


def test_stage1_generation_has_an_explicit_cross_language_decimal_bound() -> None:
    plan = _plan()
    accepted = replace(
        plan,
        root={**dict(plan.root), "generation": MAX_OCI_ROOT_VOLUME_GENERATION},
    )

    assert accepted.root["generation"] == MAX_OCI_ROOT_VOLUME_GENERATION
    with pytest.raises(StateError, match="root mount"):
        replace(
            plan,
            root={**dict(plan.root), "generation": MAX_OCI_ROOT_VOLUME_GENERATION + 1},
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.replace("palimpsest.core=", "palimpsest.unknown="),
        lambda value: value + " palimpsest.core=" + "b" * 71,
        lambda value: value.replace("palimpsest.stage1dev=virtio-", "palimpsest.stage1dev=scsi-"),
        lambda value: value.replace("palimpsest.resource=", ""),
    ],
)
def test_guest_cmdline_rejects_unknown_duplicate_missing_and_wrong_device_policy(mutate: object) -> None:
    built = build_stage1_transport(_plan())
    changed = mutate(_cmdline(built.receipt.artifact_digest))  # type: ignore[operator]
    with pytest.raises(ArtifactValidationError):
        parse_guest_kernel_cmdline(changed)


def test_guest_cmdline_is_ascii_nul_free_and_bounded() -> None:
    built = build_stage1_transport(_plan())
    valid = _cmdline(built.receipt.artifact_digest)
    for value in (valid + "\0", valid + " é", "x" * (MAX_GUEST_KERNEL_CMDLINE_BYTES + 1)):
        with pytest.raises(ArtifactValidationError, match="command line"):
            parse_guest_kernel_cmdline(value)


def test_guest_block_selection_requires_one_allowlisted_name_and_exact_serial() -> None:
    expected = "f" * 20
    candidates = (GuestBlockCandidate("vda", "a" * 20), GuestBlockCandidate("vdb", expected))
    assert select_stage1_block_device(candidates, expected_serial=expected) == Path("/dev/vdb")

    for values in ((), candidates + (GuestBlockCandidate("vdc", expected),)):
        with pytest.raises(ArtifactValidationError, match="missing or ambiguous"):
            select_stage1_block_device(values, expected_serial=expected)
    with pytest.raises(ArtifactValidationError, match="name"):
        GuestBlockCandidate("../../vdb", expected)
    with pytest.raises(ArtifactValidationError, match="ambiguous"):
        select_stage1_block_device(candidates + (GuestBlockCandidate("vdb", "e" * 20),), expected_serial=expected)


def test_pre_mount_selection_is_role_aware_ordered_and_exact_topology() -> None:
    plan = _plan()
    expected = expected_pre_mount_block_devices(plan)
    candidates = (
        GuestBlockCandidate("vdc", plan.layers[0]["serial"], True),
        GuestBlockCandidate("vda", plan.root["serial"], False),
    )

    selected = select_pre_mount_block_devices(candidates, expected)

    assert tuple(item.expected.role for item in selected) == ("root", "lower")
    assert tuple(item.path.name for item in selected) == ("vda", "vdc")
    with pytest.raises(ArtifactValidationError, match="wrong role"):
        select_pre_mount_block_devices((replace(candidates[0], read_only=False), candidates[1]), expected)
    with pytest.raises(ArtifactValidationError, match="ambiguous"):
        select_pre_mount_block_devices(candidates + (GuestBlockCandidate("vdd", "f" * 20, True),), expected)


def test_guest_sysfs_discovery_uses_bounded_exact_serial_files(tmp_path: Path) -> None:
    sys_block = tmp_path / "sys" / "class" / "block"
    sys_devices = tmp_path / "sys" / "devices"
    dev_root = tmp_path / "dev"
    sys_block.mkdir(parents=True)
    dev_root.mkdir(parents=True)
    for name, serial in (("vda", "a" * 20), ("vdb", "f" * 20)):
        device = sys_devices / "pci0000:00" / f"virtio-{name}" / "block" / name
        serial_path = device / "serial"
        serial_path.parent.mkdir(parents=True)
        serial_path.write_text(serial + "\n", encoding="ascii")
        (device / "ro").write_text("1\n", encoding="ascii")
        (sys_block / name).symlink_to(device, target_is_directory=True)

    assert discover_stage1_block_device(
        "f" * 20,
        sys_block=sys_block.resolve(),
        sys_devices=sys_devices.resolve(),
        dev_root=dev_root.resolve(),
    ) == (dev_root.resolve() / "vdb")

    (sys_devices / "pci0000:00" / "virtio-vdb" / "block" / "vdb" / "ro").write_text("0\n", encoding="ascii")
    with pytest.raises(ArtifactValidationError, match="missing"):
        discover_stage1_block_device(
            "f" * 20,
            sys_block=sys_block.resolve(),
            sys_devices=sys_devices.resolve(),
            dev_root=dev_root.resolve(),
        )

    (sys_block / "vdb").unlink()
    (sys_block / "vdb").mkdir()
    with pytest.raises(ArtifactValidationError, match="canonical link"):
        discover_stage1_block_device(
            "f" * 20,
            sys_block=sys_block.resolve(),
            sys_devices=sys_devices.resolve(),
            dev_root=dev_root.resolve(),
        )


def test_guest_sysfs_discovery_rejects_link_outside_kernel_devices(tmp_path: Path) -> None:
    sys_block = tmp_path / "sys" / "class" / "block"
    sys_devices = tmp_path / "sys" / "devices"
    outside = tmp_path / "outside" / "vdb"
    sys_block.mkdir(parents=True)
    sys_devices.mkdir(parents=True)
    outside.mkdir(parents=True)
    (outside / "serial").write_text("f" * 20 + "\n", encoding="ascii")
    (outside / "ro").write_text("1\n", encoding="ascii")
    (sys_block / "vdb").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactValidationError, match="inspected"):
        discover_stage1_block_device(
            "f" * 20,
            sys_block=sys_block.resolve(),
            sys_devices=sys_devices.resolve(),
            dev_root=(tmp_path / "dev").resolve(),
        )


def _envelope(payload: bytes) -> bytes:
    header = _HEADER.pack(b"PALIMPSEST-S1\0\0\0", 1, _HEADER.size, len(payload), hashlib.sha256(payload).digest())
    size = (len(header) + len(payload) + 4095) // 4096 * 4096
    return header + payload + b"\0" * (size - len(header) - len(payload))


def test_guest_consumer_rejects_cmdline_replay_and_self_consistent_semantic_drift() -> None:
    plan = _plan()
    built = build_stage1_transport(plan)
    wrong_resource = parse_guest_kernel_cmdline(_cmdline(built.receipt.artifact_digest, resource="sha256:" + "c" * 64))
    with pytest.raises(ArtifactValidationError, match="kernel command line"):
        verify_guest_stage1_transport(built.artifact, wrong_resource)

    wrong_root = parse_guest_kernel_cmdline(_cmdline(built.receipt.artifact_digest, root="virtio-" + "3" * 20))
    with pytest.raises(ArtifactValidationError, match="kernel command line"):
        verify_guest_stage1_transport(built.artifact, wrong_root)

    wrong_lowers = parse_guest_kernel_cmdline(_cmdline(built.receipt.artifact_digest, lowers="virtio-" + "3" * 20))
    with pytest.raises(ArtifactValidationError, match="kernel command line"):
        verify_guest_stage1_transport(built.artifact, wrong_lowers)

    value = plan.to_dict()
    value["assembly"]["lowerdir_ordinals"] = [0, 1]
    artifact = _envelope(canonical_json_bytes(value))
    bindings = parse_guest_kernel_cmdline(_cmdline(f"sha256:{hashlib.sha256(artifact).hexdigest()}"))
    with pytest.raises(ArtifactValidationError, match="policy"):
        verify_guest_stage1_transport(artifact, bindings)


def test_guest_consumer_rejects_duplicate_keys_noncanonical_bytes_and_padding() -> None:
    built = build_stage1_transport(_plan())
    payloads = (
        b'{"schema":"one","schema":"two"}',
        json.dumps(_plan().to_dict(), indent=2).encode(),
        b'{"value":NaN}',
    )
    for payload in payloads:
        artifact = _envelope(payload)
        bindings = parse_guest_kernel_cmdline(_cmdline(f"sha256:{hashlib.sha256(artifact).hexdigest()}"))
        with pytest.raises(ArtifactValidationError, match="JSON"):
            verify_guest_stage1_transport(artifact, bindings)

    changed = bytearray(built.artifact)
    changed[-1] = 1
    artifact = bytes(changed)
    bindings = parse_guest_kernel_cmdline(_cmdline(f"sha256:{hashlib.sha256(artifact).hexdigest()}"))
    with pytest.raises(ArtifactValidationError, match="payload binding"):
        verify_guest_stage1_transport(artifact, bindings)
