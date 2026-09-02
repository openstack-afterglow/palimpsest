"""Portable differential tests for mount-free guest filesystem verification."""

from __future__ import annotations

import hashlib

import pytest

from palimpsest_local._oci_stage1_kvm_proof import (
    _filesystem_negative_context,
    _mutated_filesystem_payload,
    build_proof_plan,
    load_proof_filesystems,
)
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_guest_filesystems import (
    EXT4_SUPERBLOCK_BYTES,
    EXT4_SUPERBLOCK_OFFSET,
    ext4_primary_superblock_checksum,
    verify_ext4_superblock,
    verify_lower_device,
    verify_squashfs_superblock,
)


def _root_superblock() -> tuple[bytearray, int, str, str]:
    plan = build_proof_plan()
    root = load_proof_filesystems().root
    return (
        bytearray(root[EXT4_SUPERBLOCK_OFFSET : EXT4_SUPERBLOCK_OFFSET + EXT4_SUPERBLOCK_BYTES]),
        len(root),
        plan.root["volume_id"],
        plan.root["filesystem_uuid"],
    )


def test_actual_mkfs_ext4_fixture_has_exact_identity_geometry_and_checksum() -> None:
    superblock, size, volume_id, filesystem_uuid = _root_superblock()

    identity = verify_ext4_superblock(
        bytes(superblock),
        device_size=size,
        volume_id=volume_id,
        filesystem_uuid=filesystem_uuid,
    )

    assert identity.filesystem_uuid == filesystem_uuid
    assert identity.label == "pali-root-e3f97d"
    assert identity.block_size * identity.blocks_count == size
    assert identity.feature_ro_compat & 0x400


@pytest.mark.parametrize("offset", [56, 4, 104, 120, 1020])
def test_ext4_identity_geometry_and_checksum_mutations_fail_closed(offset: int) -> None:
    superblock, size, volume_id, filesystem_uuid = _root_superblock()
    superblock[offset] ^= 1

    with pytest.raises(ArtifactValidationError):
        verify_ext4_superblock(
            bytes(superblock),
            device_size=size,
            volume_id=volume_id,
            filesystem_uuid=filesystem_uuid,
        )


@pytest.mark.parametrize("control_name", ["root_bad_magic", "root_wrong_label", "root_geometry"])
def test_ext4_targeted_controls_reseal_metadata_checksum(control_name: str) -> None:
    plan = build_proof_plan()
    _role, payload = _mutated_filesystem_payload(control_name)
    superblock = payload[EXT4_SUPERBLOCK_OFFSET : EXT4_SUPERBLOCK_OFFSET + EXT4_SUPERBLOCK_BYTES]

    assert int.from_bytes(superblock[1020:1024], "little") == ext4_primary_superblock_checksum(superblock)
    with pytest.raises(ArtifactValidationError):
        verify_ext4_superblock(
            superblock,
            device_size=len(payload),
            volume_id=plan.root["volume_id"],
            filesystem_uuid=plan.root["filesystem_uuid"],
        )


def test_ext4_metadata_checksum_type_must_be_crc32c_even_with_valid_checksum() -> None:
    superblock, size, volume_id, filesystem_uuid = _root_superblock()
    superblock[0x175] = 0
    superblock[1020:1024] = ext4_primary_superblock_checksum(bytes(superblock)).to_bytes(4, "little")

    with pytest.raises(ArtifactValidationError, match="checksum type"):
        verify_ext4_superblock(
            bytes(superblock),
            device_size=size,
            volume_id=volume_id,
            filesystem_uuid=filesystem_uuid,
        )


def test_actual_mksquashfs_fixtures_have_structure_padding_and_whole_digest() -> None:
    plan = build_proof_plan()
    filesystems = load_proof_filesystems()

    for layer, payload in zip(plan.layers, filesystems.lowers, strict=True):
        identity = verify_lower_device(payload, expected_digest=layer["image_digest"])
        assert identity.bytes_used <= len(payload)
        assert identity.block_size == 131072


def test_squashfs_structure_and_digest_are_independent_fail_closed_boundaries() -> None:
    plan = build_proof_plan()
    lower = bytearray(load_proof_filesystems().lowers[0])
    digest = plan.layers[0]["image_digest"]
    bytes_used = int.from_bytes(lower[40:48], "little")

    content_changed = bytearray(lower)
    content_changed[128] ^= 1
    verify_squashfs_superblock(
        bytes(content_changed[:96]),
        device_size=len(content_changed),
        padding=bytes(content_changed[bytes_used:]),
    )
    with pytest.raises(ArtifactValidationError, match="digest"):
        verify_lower_device(bytes(content_changed), expected_digest=digest)

    bad_structure = bytearray(lower)
    bad_structure[20:22] = b"\0\0"
    with pytest.raises(ArtifactValidationError, match="encoding"):
        verify_squashfs_superblock(
            bytes(bad_structure[:96]),
            device_size=len(bad_structure),
            padding=bytes(bad_structure[bytes_used:]),
        )


@pytest.mark.parametrize("control_name", ["lower_bad_magic", "lower_bad_structure", "lower_digest_mismatch"])
def test_lower_targeted_controls_bind_digest_or_structure_independently(control_name: str) -> None:
    plan, _transport = _filesystem_negative_context(control_name)
    _role, payload = _mutated_filesystem_payload(control_name)
    actual_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    expected_digest = plan.layers[0]["image_digest"]

    if control_name == "lower_digest_mismatch":
        assert expected_digest != actual_digest
        bytes_used = int.from_bytes(payload[40:48], "little")
        verify_squashfs_superblock(payload[:96], device_size=len(payload), padding=payload[bytes_used:])
        with pytest.raises(ArtifactValidationError, match="digest"):
            verify_lower_device(payload, expected_digest=expected_digest)
    else:
        assert expected_digest == actual_digest
        with pytest.raises(ArtifactValidationError, match="identity|encoding"):
            verify_lower_device(payload, expected_digest=expected_digest)
