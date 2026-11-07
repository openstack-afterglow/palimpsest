"""Lima physical storage for the canonical OCI writable-root authority.

Lima owns attachment operations, not volume identity or retention. No KVM DAC
receipt is accepted here. Failed/partial preparation remains enrolled and is
never implicitly adopted, reformatted, or replayed.
"""

from __future__ import annotations

import base64
import os
import re
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from .digest import normalize_digest
from .errors import StateError
from .oci_guest_filesystems import EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET, verify_ext4_superblock
from .oci_root_volume import (
    ClaimedOCIRootVolume,
    OCIRootVolumeRecord,
    VerifiedOCIRootVolume,
    VZRootVolumeStorage,
    _canonical_uuid,
    _paths,
    _read_record,
    _remove_record,
    _root_authority,
    _validate_owner,
    _write_record,
    oci_root_volume_label,
)
from .oci_store import ArtifactLeaseOwner
from .project_volumes import _validate_size
from .state import StatePaths, file_lock


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _private_directory(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        visible = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or _identity(info) != _identity(visible)
        ):
            raise StateError("VZ root-volume parent is not a private owned directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_file(record: OCIRootVolumeRecord) -> VerifiedOCIRootVolume:
    storage = record.storage
    if not isinstance(storage, VZRootVolumeStorage):
        raise StateError("root volume has no VZ physical binding")
    if storage.directory_identity is None or storage.file_identity is None:
        raise StateError("VZ root-volume preparation is incomplete")
    with ExitStack() as opened:
        directory_fds = []
        for path in (Path(storage.lima_home), storage.directory.parent, storage.directory):
            descriptor = _private_directory(path)
            opened.callback(os.close, descriptor)
            directory_fds.append((path, descriptor))
        disk_fd = directory_fds[-1][1]
        if _identity(os.fstat(disk_fd)) != storage.directory_identity:
            raise StateError("VZ root-volume directory identity changed")
        data_fd = os.open("datadisk", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=disk_fd)
        opened.callback(os.close, data_fd)
        info = os.fstat(data_fd)
        visible = os.stat("datadisk", dir_fd=disk_fd, follow_symlinks=False)
        if any(
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or stat.S_IMODE(item.st_mode) != 0o600
            or item.st_nlink != 1
            or item.st_size != record.size_bytes
            or _identity(item) != storage.file_identity
            for item in (info, visible)
        ):
            raise StateError("VZ root-volume data identity, size or ownership changed")
        verify_ext4_superblock(
            os.pread(data_fd, EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET),
            device_size=record.size_bytes,
            volume_id=record.volume_id,
            filesystem_uuid=record.volume_id,
        )
        if (
            _identity(os.fstat(data_fd)) != storage.file_identity
            or _identity(os.stat("datadisk", dir_fd=disk_fd, follow_symlinks=False)) != storage.file_identity
        ):
            raise StateError("VZ root-volume data changed during verification")
        for path, descriptor in directory_fds:
            if _identity(os.stat(path, follow_symlinks=False)) != _identity(os.fstat(descriptor)):
                raise StateError("VZ root-volume parent changed during verification")
    return VerifiedOCIRootVolume(record, storage.path, record.volume_id)


def load_vz_root_volume(
    roots: StatePaths,
    volume_id: str,
    *,
    owner: ArtifactLeaseOwner | None = None,
) -> VerifiedOCIRootVolume:
    """Read the canonical record and pinned raw disk without creating anything."""
    _, record_path, lock_path = _paths(roots, volume_id)
    with file_lock(lock_path), _root_authority(roots) as authority:
        record = _read_record(authority, record_path)
        if record.status not in {"attached", "retained"}:
            raise StateError("VZ root-volume lifecycle is incomplete")
        if owner is not None:
            _validate_owner(owner)
            if record.status != "attached" or (record.attached_run_id, record.attached_run_name) != (
                owner.run_id,
                owner.run_name,
            ):
                raise StateError("VZ root-volume attachment owner changed")
        result = _verify_file(record)
        if _read_record(authority, record_path) != record:
            raise StateError("VZ root-volume generation changed during verification")
        return result


def claim_vz_root_volume(
    roots: StatePaths,
    volume_id: str,
    *,
    size_bytes: int,
    lower_graph_digest: str,
    retention_policy: str,
    owner: ArtifactLeaseOwner,
    lima_home: Path,
    root_nonce: str,
    prepare_disk: Callable[..., str],
) -> tuple[ClaimedOCIRootVolume, str]:
    """Enroll once, prepare offline, then pin the new exact Lima disk inode.

    The callback creates a NEW raw file using an explicitly selected owned Linux
    worker. Any exception leaves the creating record and exact partial storage
    visible; this API deliberately does not retry uncertain preparation.
    """
    _validate_owner(owner)
    volume_id = _canonical_uuid(volume_id)
    size_bytes = _validate_size(size_bytes)
    if normalize_digest(lower_graph_digest) != lower_graph_digest:
        raise StateError("VZ root-volume lower graph digest is not canonical")
    if retention_policy not in {"delete", "retain"}:
        raise StateError("VZ root-volume retention policy is invalid")
    if not isinstance(root_nonce, str) or re.fullmatch(r"[0-9a-f]{64}", root_nonce) is None:
        raise StateError("VZ root-volume initialization nonce is invalid")
    if not isinstance(lima_home, Path) or not lima_home.is_absolute() or lima_home.resolve(strict=True) != lima_home:
        raise StateError("VZ root-volume Lima home must be an existing canonical directory")
    if not callable(prepare_disk):
        raise StateError("VZ root-volume requires an offline disk preparer")
    storage = VZRootVolumeStorage(str(lima_home), "plm-root-" + volume_id.replace("-", ""))
    raw_path, record_path, lock_path = _paths(roots, volume_id)
    with file_lock(lock_path), _root_authority(roots) as authority, ExitStack() as opened:
        home_fd = _private_directory(lima_home)
        opened.callback(os.close, home_fd)
        if any(os.path.lexists(path) for path in (record_path, raw_path, storage.directory)):
            raise StateError("VZ root-volume ID is already enrolled or has uncertain storage; reconciliation required")
        creating = OCIRootVolumeRecord(
            volume_id,
            size_bytes,
            lower_graph_digest,
            retention_policy,
            "creating",
            owner.run_id,
            owner.run_name,
            1,
            storage,
        )
        _write_record(authority, record_path, creating)
        try:
            os.mkdir("_disks", 0o700, dir_fd=home_fd)
            os.fsync(home_fd)
        except FileExistsError:
            pass
        disks_fd = _private_directory(storage.directory.parent)
        opened.callback(os.close, disks_fd)
        os.mkdir(storage.disk_name, 0o700, dir_fd=disks_fd)
        os.fsync(disks_fd)
        disk_fd = _private_directory(storage.directory)
        opened.callback(os.close, disk_fd)
        storage = replace(storage, directory_identity=_identity(os.fstat(disk_fd)))
        creating = replace(creating, storage=storage)
        _write_record(authority, record_path, creating)
        binding = {
            "schema": "palimpsest.vz-upper-binding.v1",
            "run_id": owner.run_id,
            "root_volume_id": volume_id,
            "root_graph_digest": lower_graph_digest,
            "root_nonce": root_nonce,
            "phase": "prepared",
        }
        public_key = prepare_disk(
            storage.path,
            size_bytes=size_bytes,
            filesystem_uuid=volume_id,
            label=oci_root_volume_label(volume_id),
            binding=binding,
        )
        if not isinstance(public_key, str) or len(public_key) > 8192:
            raise StateError("VZ root-volume preparer returned an invalid host public key")
        parts = public_key.strip().split()
        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise StateError("VZ root-volume host key must be ed25519")
        try:
            key_bytes = base64.b64decode(parts[1], validate=True)
        except ValueError:
            raise StateError("VZ root-volume host public key encoding is invalid") from None
        if not 32 <= len(key_bytes) <= 1024:
            raise StateError("VZ root-volume host public key size is invalid")
        info = os.stat("datadisk", dir_fd=disk_fd, follow_symlinks=False)
        storage = replace(storage, file_identity=_identity(info))
        ready = replace(creating, status="attached", storage=storage)
        _verify_file(ready)
        if _identity(os.stat(storage.directory, follow_symlinks=False)) != storage.directory_identity:
            raise StateError("VZ root-volume directory changed during offline preparation")
        os.fsync(disk_fd)
        if _read_record(authority, record_path) != creating:
            raise StateError("VZ root-volume preparation record changed")
        _write_record(authority, record_path, ready)
        return ClaimedOCIRootVolume(ready, storage.path, True, False), " ".join(parts[:2])


def release_vz_root_volume(
    roots: StatePaths,
    volume_id: str,
    *,
    owner: ArtifactLeaseOwner,
    lower_graph_digest: str,
    provider_absent: bool,
    delete: bool | None = None,
) -> OCIRootVolumeRecord | None:
    """Release only after exact provider absence; never dispose ordinary volumes."""
    _validate_owner(owner)
    if provider_absent is not True:
        raise StateError("VZ root-volume release requires exact provider absence")
    _, record_path, lock_path = _paths(roots, volume_id)
    with file_lock(lock_path), _root_authority(roots) as authority:
        record = _read_record(authority, record_path)
        if (
            record.storage is None
            or record.lower_graph_digest != lower_graph_digest
            or (record.attached_run_id, record.attached_run_name) != (owner.run_id, owner.run_name)
            or record.status not in {"attached", "deleting"}
        ):
            raise StateError("VZ root-volume release binding is invalid")
        _verify_file(record)
        storage = record.storage
        with ExitStack() as opened:
            parent_fd = _private_directory(storage.directory.parent)
            opened.callback(os.close, parent_fd)
            disk_fd = _private_directory(storage.directory)
            opened.callback(os.close, disk_fd)
            if _identity(os.fstat(disk_fd)) != storage.directory_identity:
                raise StateError("VZ root-volume directory changed before release")
            names = set(os.listdir(disk_fd))
            if names != {"datadisk"}:
                raise StateError("VZ root-volume has a consumer lock or unknown files; cleanup is held")
            should_delete = record.retention_policy == "delete" if delete is None else delete
            if type(should_delete) is not bool:
                raise StateError("VZ root-volume deletion policy must be boolean")
            if not should_delete:
                if record.status == "deleting":
                    raise StateError("deleting VZ root volume cannot be retained")
                retained = replace(
                    record,
                    status="retained",
                    attached_run_id=None,
                    attached_run_name=None,
                    generation=record.generation + 1,
                )
                _write_record(authority, record_path, retained)
                return retained
            deleting = (
                record
                if record.status == "deleting"
                else replace(
                    record,
                    status="deleting",
                    generation=record.generation + 1,
                )
            )
            if deleting != record:
                _write_record(authority, record_path, deleting)
            if _identity(os.stat("datadisk", dir_fd=disk_fd, follow_symlinks=False)) != storage.file_identity:
                raise StateError("VZ root-volume image changed before deletion")
            if _read_record(authority, record_path) != deleting:
                raise StateError("VZ root-volume deletion record changed")
            os.unlink("datadisk", dir_fd=disk_fd)
            os.fsync(disk_fd)
            if (
                _identity(os.stat(storage.disk_name, dir_fd=parent_fd, follow_symlinks=False))
                != storage.directory_identity
            ):
                raise StateError("VZ root-volume directory changed before removal")
            os.rmdir(storage.disk_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            _remove_record(authority, record_path)
    return None
