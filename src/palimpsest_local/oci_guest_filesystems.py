"""Portable pre-mount ext4/SquashFS verification contracts.

These parsers inspect already authenticated raw device bytes.  They never
mount a filesystem and deliberately stop at primary-superblock structure plus
the complete immutable-lower digest boundary.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from dataclasses import dataclass

from .errors import ArtifactValidationError

EXT4_SUPERBLOCK_OFFSET = 1024
EXT4_SUPERBLOCK_BYTES = 1024
EXT4_REQUIRED_INCOMPAT = 0x42  # FILETYPE | EXTENTS
EXT4_ALLOWED_COMPAT = 0x3F
EXT4_ALLOWED_INCOMPAT = 0x2C6  # FILETYPE | RECOVER | EXTENTS | 64BIT | FLEX_BG
EXT4_ALLOWED_RO_COMPAT = 0x47B  # common non-BIGALLOC ext4 features + metadata_csum
EXT4_FEATURE_INCOMPAT_64BIT = 0x80
EXT4_FEATURE_RO_COMPAT_METADATA_CSUM = 0x400

SQUASHFS_SUPERBLOCK_BYTES = 96
SQUASHFS_STRUCTURAL_POLICY = "palimpsest.squashfs-superblock.v2"
MAX_STAGE1_FILESYSTEM_VERIFY_BYTES = 32 * 1024**3
_UINT64_MAX = (1 << 64) - 1
_SQUASHFS = struct.Struct("<5I6H8Q")


def _crc32c(payload: bytes, initial: int = 0xFFFFFFFF) -> int:
    value = initial
    for byte in payload:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x82F63B78 if value & 1 else value >> 1
    return value & 0xFFFFFFFF


def _ceil_div(value: int, divisor: int) -> int:
    if value < 0 or divisor <= 0:
        raise ArtifactValidationError("filesystem geometry is invalid")
    return (value + divisor - 1) // divisor


def ext4_primary_superblock_checksum(payload: bytes) -> int:
    """Return the ext4 primary-superblock metadata checksum."""

    if not isinstance(payload, bytes) or len(payload) != EXT4_SUPERBLOCK_BYTES:
        raise ArtifactValidationError("ext4 primary superblock is truncated")
    return _crc32c(payload[:1020])


@dataclass(frozen=True, slots=True)
class Ext4FilesystemIdentity:
    filesystem_uuid: str
    label: str
    block_size: int
    blocks_count: int
    inode_size: int
    feature_compat: int
    feature_incompat: int
    feature_ro_compat: int


def ext4_volume_label(volume_id: str) -> str:
    try:
        canonical = str(uuid.UUID(volume_id))
    except (AttributeError, TypeError, ValueError):
        raise ArtifactValidationError("ext4 volume identity is invalid") from None
    if canonical != volume_id:
        raise ArtifactValidationError("ext4 volume identity is not canonical")
    return "pali-root-" + hashlib.sha256(f"palimpsest-oci-root-volume-v1\0{canonical}".encode()).hexdigest()[:6]


def verify_ext4_superblock(
    payload: bytes,
    *,
    device_size: int,
    volume_id: str,
    filesystem_uuid: str,
) -> Ext4FilesystemIdentity:
    """Verify one ext4 primary superblock against its authenticated identity."""

    if not isinstance(payload, bytes) or len(payload) != EXT4_SUPERBLOCK_BYTES:
        raise ArtifactValidationError("ext4 primary superblock is truncated")
    if type(device_size) is not int or device_size < 16 * 1024**2 or device_size % 1024**2:
        raise ArtifactValidationError("ext4 device size is invalid")
    try:
        expected_uuid = str(uuid.UUID(filesystem_uuid))
    except (AttributeError, TypeError, ValueError):
        raise ArtifactValidationError("ext4 filesystem UUID is invalid") from None
    if expected_uuid != filesystem_uuid:
        raise ArtifactValidationError("ext4 filesystem UUID is not canonical")

    def u16(offset: int) -> int:
        return int.from_bytes(payload[offset : offset + 2], "little")

    def u32(offset: int) -> int:
        return int.from_bytes(payload[offset : offset + 4], "little")

    if u16(56) != 0xEF53 or u32(76) != 1:
        raise ArtifactValidationError("ext4 identity or revision is invalid")
    log_block_size = u32(24)
    if log_block_size > 6:
        raise ArtifactValidationError("ext4 block size is invalid")
    block_size = 1024 << log_block_size
    incompat = u32(96)
    compat = u32(92)
    ro_compat = u32(100)
    if (
        incompat & EXT4_REQUIRED_INCOMPAT != EXT4_REQUIRED_INCOMPAT
        or compat & ~EXT4_ALLOWED_COMPAT
        or incompat & ~EXT4_ALLOWED_INCOMPAT
        or ro_compat & ~EXT4_ALLOWED_RO_COMPAT
    ):
        raise ArtifactValidationError("ext4 feature policy is invalid")
    blocks_high = u32(336)
    if not incompat & EXT4_FEATURE_INCOMPAT_64BIT and blocks_high:
        raise ArtifactValidationError("ext4 64-bit block accounting is invalid")
    blocks_count = u32(4) | (blocks_high << 32 if incompat & EXT4_FEATURE_INCOMPAT_64BIT else 0)
    if blocks_count == 0 or blocks_count > _UINT64_MAX // block_size or blocks_count * block_size != device_size:
        raise ArtifactValidationError("ext4 block geometry does not match the device")
    first_data = u32(20)
    blocks_per_group = u32(32)
    inodes_count = u32(0)
    inodes_per_group = u32(40)
    if (
        first_data >= blocks_count
        or first_data != (1 if block_size == 1024 else 0)
        or not 0 < blocks_per_group <= block_size * 8
        or inodes_count == 0
        or inodes_per_group == 0
        or _ceil_div(blocks_count - first_data, blocks_per_group) != _ceil_div(inodes_count, inodes_per_group)
    ):
        raise ArtifactValidationError("ext4 group geometry is invalid")
    inode_size = u16(88)
    if inode_size < 128 or inode_size > block_size or inode_size & (inode_size - 1):
        raise ArtifactValidationError("ext4 inode geometry is invalid")
    if incompat & EXT4_FEATURE_INCOMPAT_64BIT:
        descriptor_size = u16(254)
        if descriptor_size < 64 or descriptor_size > block_size or descriptor_size & 7:
            raise ArtifactValidationError("ext4 descriptor geometry is invalid")
    raw_uuid = payload[104:120]
    actual_uuid = str(uuid.UUID(bytes=raw_uuid))
    if actual_uuid != expected_uuid:
        raise ArtifactValidationError("ext4 filesystem UUID does not match the plan")
    expected_label = ext4_volume_label(volume_id)
    if payload[120:136] != expected_label.encode("ascii").ljust(16, b"\0"):
        raise ArtifactValidationError("ext4 filesystem label does not match the volume")
    if ro_compat & EXT4_FEATURE_RO_COMPAT_METADATA_CSUM:
        if payload[0x175] != 1:
            raise ArtifactValidationError("ext4 primary superblock checksum type is invalid")
        if ext4_primary_superblock_checksum(payload) != u32(1020):
            raise ArtifactValidationError("ext4 primary superblock checksum is invalid")
    return Ext4FilesystemIdentity(
        actual_uuid,
        expected_label,
        block_size,
        blocks_count,
        inode_size,
        compat,
        incompat,
        ro_compat,
    )


@dataclass(frozen=True, slots=True)
class SquashFSIdentity:
    block_size: int
    bytes_used: int
    compression: int
    inodes: int


def verify_squashfs_superblock(payload: bytes, *, device_size: int, padding: bytes) -> SquashFSIdentity:
    """Mirror ``oci_packer.verify_squashfs_fd`` for guest-visible bytes."""

    if not isinstance(payload, bytes) or len(payload) != SQUASHFS_SUPERBLOCK_BYTES:
        raise ArtifactValidationError("SquashFS superblock is truncated")
    if type(device_size) is not int or device_size < 512 or device_size % 512:
        raise ArtifactValidationError("SquashFS device size is invalid")
    values = _SQUASHFS.unpack(payload)
    magic, inodes, mkfs_time, block_size, fragments = values[:5]
    compression, block_log, flags, id_count, major, minor = values[5:11]
    root_inode, bytes_used, id_start, xattr_start, inode_start, directory_start, fragment_start, export_start = values[
        11:
    ]
    offsets = (id_start, xattr_start, inode_start, directory_start, fragment_start, export_start)
    if magic != 0x73717368 or inodes < 1 or mkfs_time != 0 or (major, minor) != (4, 0):
        raise ArtifactValidationError("SquashFS identity is invalid")
    if block_size < 4096 or block_size > 1024 * 1024 or block_size & (block_size - 1):
        raise ArtifactValidationError("SquashFS block size is invalid")
    if block_log != block_size.bit_length() - 1 or compression not in range(1, 7) or id_count < 1:
        raise ArtifactValidationError("SquashFS encoding metadata is invalid")
    if not SQUASHFS_SUPERBLOCK_BYTES <= bytes_used <= device_size:
        raise ArtifactValidationError("SquashFS byte accounting is invalid")
    if any(value != _UINT64_MAX and not SQUASHFS_SUPERBLOCK_BYTES <= value < bytes_used for value in offsets):
        raise ArtifactValidationError("SquashFS table offset is invalid")
    required = (id_start, inode_start, directory_start)
    if _UINT64_MAX in required or len(set(required)) != len(required):
        raise ArtifactValidationError("SquashFS required tables are unavailable")
    if inode_start >= directory_start or root_inode >> 16 >= bytes_used - inode_start:
        raise ArtifactValidationError("SquashFS root inode location is invalid")
    if (fragments == 0) != (fragment_start == _UINT64_MAX):
        raise ArtifactValidationError("SquashFS fragment accounting is invalid")
    if bool(flags & 0x80) != (export_start != _UINT64_MAX):
        raise ArtifactValidationError("SquashFS export accounting is invalid")
    padding_size = device_size - bytes_used
    if padding_size >= block_size or not isinstance(padding, bytes) or len(padding) != padding_size or any(padding):
        raise ArtifactValidationError("SquashFS image padding is invalid")
    return SquashFSIdentity(block_size, bytes_used, compression, inodes)


def verify_lower_device(payload: bytes, *, expected_digest: str) -> SquashFSIdentity:
    if not isinstance(payload, bytes) or len(payload) > MAX_STAGE1_FILESYSTEM_VERIFY_BYTES:
        raise ArtifactValidationError("lower device bytes are invalid")
    try:
        algorithm, hexdigest = expected_digest.split(":", 1)
    except (AttributeError, ValueError):
        raise ArtifactValidationError("lower device digest is invalid") from None
    if algorithm != "sha256" or len(hexdigest) != 64 or any(char not in "0123456789abcdef" for char in hexdigest):
        raise ArtifactValidationError("lower device digest is invalid")
    if hashlib.sha256(payload).hexdigest() != hexdigest:
        raise ArtifactValidationError("lower device digest does not match the plan")
    superblock = payload[:SQUASHFS_SUPERBLOCK_BYTES]
    bytes_used = int.from_bytes(superblock[40:48], "little") if len(superblock) == SQUASHFS_SUPERBLOCK_BYTES else 0
    return verify_squashfs_superblock(superblock, device_size=len(payload), padding=payload[bytes_used:])


__all__ = [
    "EXT4_SUPERBLOCK_BYTES",
    "EXT4_SUPERBLOCK_OFFSET",
    "MAX_STAGE1_FILESYSTEM_VERIFY_BYTES",
    "SQUASHFS_STRUCTURAL_POLICY",
    "Ext4FilesystemIdentity",
    "SquashFSIdentity",
    "ext4_primary_superblock_checksum",
    "ext4_volume_label",
    "verify_ext4_superblock",
    "verify_lower_device",
    "verify_squashfs_superblock",
]
