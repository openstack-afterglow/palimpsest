"""Durable single-writer ownership for OCI-root writable ext4 volumes.

The owner-only state directory is the local trust boundary, matching project
volume handling. Descriptor checks prevent accidental path substitution; this
is not an OS sandbox against a malicious same-UID process rewriting its state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, StateError
from .oci_guest_filesystems import EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET, verify_ext4_superblock
from .oci_store import ArtifactLeaseOwner
from .project_volumes import (
    CommandRunner,
    _default_runner,
    _delete_ext4_raw_file_locked,
    _ensure_ext4_raw_file_locked,
    _validate_size,
    _verify_kvm_path,
)
from .state import StatePaths, file_lock, pinned_owner_directory

OCI_ROOT_VOLUME_SCHEMA = "palimpsest.oci-root-volume.v1"
OCI_ROOT_VOLUME_RETENTION_POLICIES = frozenset({"delete", "retain"})
MAX_OCI_ROOT_VOLUME_GENERATION_DIGITS = 4096
MAX_OCI_ROOT_VOLUME_GENERATION = 10**MAX_OCI_ROOT_VOLUME_GENERATION_DIGITS - 1
_RECORD_BYTES = 64 * 1024
_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
_RETENTION_FORK_LOCK = threading.Lock()
_RETENTION_LOCKS: weakref.WeakSet[_RetentionVolumeLock] = weakref.WeakSet()


def _close_retention_locks_after_fork() -> None:
    try:
        for lock in tuple(_RETENTION_LOCKS):
            lock._close_descriptors()
        _RETENTION_LOCKS.clear()
    finally:
        _RETENTION_FORK_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_RETENTION_FORK_LOCK.acquire,
        after_in_parent=_RETENTION_FORK_LOCK.release,
        after_in_child=_close_retention_locks_after_fork,
    )


class _RetentionVolumeLock:
    """Existing-only lock for guarded retention, without chmod or symlink following."""

    def __init__(self, roots: StatePaths, path: Path):
        self.roots, self.path, self.pid = roots, path, os.getpid()
        self.directory_fd = self.lock_fd = -1
        _RETENTION_FORK_LOCK.acquire()
        try:
            self.directory_fd = os.open(roots.locks, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.lock_fd = os.open(
                path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.directory_fd
            )
            self.directory_identity = os.fstat(self.directory_fd)
            self.lock_identity = os.fstat(self.lock_fd)
            self.verify()
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("root retention volume lock is busy") from None
            self.verify()
            _RETENTION_LOCKS.add(self)
        except BaseException:
            self._close_descriptors()
            raise
        finally:
            _RETENTION_FORK_LOCK.release()

    def verify(self) -> None:
        if os.getpid() != self.pid or self.lock_fd < 0:
            raise StateError("root retention lock authority changed")
        for held_fd, visible, expected, directory in (
            (self.directory_fd, os.stat(self.roots.locks, follow_symlinks=False), self.directory_identity, True),
            (
                self.lock_fd,
                os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False),
                self.lock_identity,
                False,
            ),
        ):
            for info in (os.fstat(held_fd), visible):
                if (
                    not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                    or (not directory and info.st_nlink != 1)
                    or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
                ):
                    raise StateError("root retention lock authority changed")

    def _close_descriptors(self) -> None:
        for name in ("lock_fd", "directory_fd"):
            descriptor = getattr(self, name)
            setattr(self, name, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def close(self) -> None:
        with _RETENTION_FORK_LOCK:
            _RETENTION_LOCKS.discard(self)
            self._close_descriptors()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _canonical_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise StateError("OCI-root volume ID is invalid") from None
    canonical = str(parsed)
    if canonical != value:
        raise StateError("OCI-root volume ID is not canonical")
    return canonical


def new_oci_root_volume_id() -> str:
    return str(uuid.uuid4())


def oci_root_volume_label(volume_id: str) -> str:
    canonical = _canonical_uuid(volume_id)
    return "pali-root-" + hashlib.sha256(f"palimpsest-oci-root-volume-v1\0{canonical}".encode()).hexdigest()[:6]


def _paths(roots: StatePaths, volume_id: str) -> tuple[Path, Path, Path]:
    if not isinstance(roots, StatePaths):
        raise StateError("OCI-root volume roots are invalid")
    canonical = _canonical_uuid(volume_id)
    stem = canonical.replace("-", "")
    return (
        roots.oci_root_volumes / f"{stem}.raw",
        roots.oci_root_volumes / f"{stem}.json",
        roots.locks / f"oci-root-volume-{stem}.lock",
    )


def _deletion_quarantine(roots: StatePaths, volume_id: str) -> Path:
    canonical = _canonical_uuid(volume_id)
    return roots.oci_root_volumes / f".{canonical.replace('-', '')}-deleting.raw"


def _creation_temporary(roots: StatePaths, volume_id: str) -> Path:
    canonical = _canonical_uuid(volume_id)
    return roots.oci_root_volumes / f".{canonical.replace('-', '')}-creating.raw"


def _cleanup_creation_temporary(
    roots: StatePaths,
    directory_fd: int,
    path: Path,
    volume_id: str,
) -> None:
    temporary = _creation_temporary(roots, volume_id)
    if not temporary.exists() and not temporary.is_symlink():
        return
    temp_entry = temporary.stat(follow_symlinks=False)
    if not stat.S_ISREG(temp_entry.st_mode) or temp_entry.st_uid != os.geteuid():
        raise StateError("OCI-root volume creation temporary is unsafe")
    if path.exists() or path.is_symlink():
        path_entry = path.stat(follow_symlinks=False)
        if (
            (temp_entry.st_dev, temp_entry.st_ino) != (path_entry.st_dev, path_entry.st_ino)
            or temp_entry.st_nlink != 2
            or path_entry.st_nlink != 2
        ):
            raise StateError("OCI-root volume creation publication is inconsistent")
    elif temp_entry.st_nlink != 1:
        raise StateError("OCI-root volume creation temporary link count is unsafe")
    temporary.unlink()
    os.fsync(directory_fd)


@contextmanager
def _root_authority(roots: StatePaths) -> Iterator[int]:
    with pinned_owner_directory(roots.oci_root_volumes) as directory_fd:
        if directory_fd is None:
            raise StateError("OCI-root volume authority is missing")
        before = os.fstat(directory_fd)
        visible = os.stat(roots.oci_root_volumes, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (visible.st_dev, visible.st_ino):
            raise StateError("OCI-root volume authority changed")
        yield directory_fd
        after = os.fstat(directory_fd)
        current = os.stat(roots.oci_root_volumes, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or (
            current.st_dev,
            current.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise StateError("OCI-root volume authority changed")


@dataclass(frozen=True, slots=True)
class OCIRootVolumeRecord:
    volume_id: str
    size_bytes: int
    lower_graph_digest: str
    retention_policy: str
    status: str
    attached_run_id: str | None
    attached_run_name: str | None
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "volume_id", _canonical_uuid(self.volume_id))
        _validate_size(self.size_bytes)
        try:
            normalized = normalize_digest(self.lower_graph_digest)
        except (ArtifactValidationError, TypeError, ValueError):
            raise StateError("OCI-root volume lower graph digest is invalid") from None
        if normalized != self.lower_graph_digest:
            raise StateError("OCI-root volume lower graph digest is not canonical")
        if self.retention_policy not in OCI_ROOT_VOLUME_RETENTION_POLICIES:
            raise StateError("OCI-root volume retention policy is invalid")
        if self.status not in {"creating", "attached", "retained", "deleting"}:
            raise StateError("OCI-root volume status is invalid")
        if type(self.generation) is not int or self.generation < 1 or self.generation > MAX_OCI_ROOT_VOLUME_GENERATION:
            raise StateError("OCI-root volume generation is invalid")
        if self.status == "retained":
            if self.attached_run_id is not None or self.attached_run_name is not None:
                raise StateError("retained OCI-root volume cannot have an attachment")
        else:
            ArtifactLeaseOwner(self.attached_run_id or "", self.attached_run_name or "", "root-lower")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attached_run_id": self.attached_run_id,
            "attached_run_name": self.attached_run_name,
            "filesystem": "ext4",
            "generation": self.generation,
            "lower_graph_digest": self.lower_graph_digest,
            "retention_policy": self.retention_policy,
            "schema": OCI_ROOT_VOLUME_SCHEMA,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "volume_id": self.volume_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> OCIRootVolumeRecord:
        expected = {
            "attached_run_id",
            "attached_run_name",
            "filesystem",
            "generation",
            "lower_graph_digest",
            "retention_policy",
            "schema",
            "size_bytes",
            "status",
            "volume_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise StateError("OCI-root volume record fields are invalid")
        if value.get("schema") != OCI_ROOT_VOLUME_SCHEMA or value.get("filesystem") != "ext4":
            raise StateError("OCI-root volume record schema is invalid")
        fields = dict(value)
        fields.pop("schema")
        fields.pop("filesystem")
        try:
            record = cls(**fields)
        except (StateError, TypeError, ValueError):
            raise StateError("OCI-root volume record is invalid") from None
        if record.to_dict() != value:
            raise StateError("OCI-root volume record is not canonical")
        return record


@dataclass(frozen=True, slots=True)
class ClaimedOCIRootVolume:
    record: OCIRootVolumeRecord
    path: Path
    created: bool
    claimed_from_retained: bool

    def __post_init__(self) -> None:
        if self.record.status != "attached" or not self.path.is_absolute():
            raise StateError("claimed OCI-root volume is invalid")
        if type(self.created) is not bool or type(self.claimed_from_retained) is not bool:
            raise StateError("claimed OCI-root volume provenance is invalid")
        if self.created and self.claimed_from_retained:
            raise StateError("claimed OCI-root volume provenance conflicts")


@dataclass(frozen=True, slots=True)
class VerifiedOCIRootVolume:
    record: OCIRootVolumeRecord
    path: Path
    filesystem_uuid: str

    def __post_init__(self) -> None:
        try:
            canonical_uuid = str(uuid.UUID(self.filesystem_uuid))
        except (AttributeError, TypeError, ValueError):
            raise StateError("verified OCI-root filesystem UUID is invalid") from None
        if not self.path.is_absolute() or canonical_uuid != self.filesystem_uuid:
            raise StateError("verified OCI-root volume path is invalid")


def _strict_json_load(directory_fd: int, name: str) -> dict[str, Any]:
    file_fd: int | None = None
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        file_fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(entry.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size <= 0
            or opened.st_size > _RECORD_BYTES
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise StateError("OCI-root volume record is unsafe")
        payload = b""
        while len(payload) < opened.st_size:
            chunk = os.read(file_fd, opened.st_size - len(payload))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(file_fd)
        if len(payload) != opened.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise StateError("OCI-root volume record changed during read")
    except FileNotFoundError:
        raise StateError("OCI-root volume record is missing") from None
    except OSError:
        raise StateError("OCI-root volume record cannot be read") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise StateError("OCI-root volume record has duplicate keys")
            result[key] = item
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except StateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StateError("OCI-root volume record is invalid JSON") from None
    if not isinstance(value, dict):
        raise StateError("OCI-root volume record must be an object")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if payload != canonical:
        raise StateError("OCI-root volume record is not canonical JSON")
    return value


def _read_record(directory_fd: int, record_path: Path) -> OCIRootVolumeRecord:
    return OCIRootVolumeRecord.from_dict(_strict_json_load(directory_fd, record_path.name))


def _write_record(directory_fd: int, record_path: Path, record: OCIRootVolumeRecord) -> None:
    payload = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    temporary = f".record-{uuid.uuid4().hex}.tmp"
    file_fd: int | None = None
    try:
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(file_fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(file_fd, payload[offset:])
            if written <= 0:
                raise StateError("OCI-root volume record write failed")
            offset += written
        os.fsync(file_fd)
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != len(payload)
        ):
            raise StateError("OCI-root volume record write is unsafe")
        os.replace(temporary, record_path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = ""
        os.fsync(directory_fd)
    except OSError:
        raise StateError("OCI-root volume record write failed") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass
    if _read_record(directory_fd, record_path) != record:
        raise StateError("OCI-root volume record changed during write")


def _remove_record(directory_fd: int, record_path: Path) -> None:
    try:
        os.unlink(record_path.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except FileNotFoundError:
        pass
    except OSError:
        raise StateError("OCI-root volume owner record deletion failed") from None


def _validate_owner(owner: ArtifactLeaseOwner) -> None:
    if not isinstance(owner, ArtifactLeaseOwner) or owner.role != "root-lower":
        raise StateError("OCI-root volume owner is invalid")


def claim_oci_root_volume(
    roots: StatePaths,
    volume_id: str,
    *,
    size_bytes: int,
    lower_graph_digest: str,
    retention_policy: str,
    owner: ArtifactLeaseOwner,
    runner: CommandRunner = _default_runner,
) -> ClaimedOCIRootVolume:
    """Create or exclusively attach one new/retained root volume."""
    _validate_owner(owner)
    size_bytes = _validate_size(size_bytes)
    if retention_policy not in OCI_ROOT_VOLUME_RETENTION_POLICIES:
        raise StateError("OCI-root volume retention policy is invalid")
    normalized_lower_graph = normalize_digest(lower_graph_digest)
    if normalized_lower_graph != lower_graph_digest:
        raise StateError("OCI-root volume lower graph digest is not canonical")
    path, record_path, lock_path = _paths(roots, volume_id)
    label = oci_root_volume_label(volume_id)
    with file_lock(lock_path), _root_authority(roots) as directory_fd:
        file_exists = path.exists() or path.is_symlink()
        record_exists = record_path.exists() or record_path.is_symlink()
        if file_exists and not record_exists:
            raise StateError("OCI-root volume artifact and owner record are inconsistent")
        if record_exists:
            record = _read_record(directory_fd, record_path)
            from .oci_root_access import require_root_access_revoked

            require_root_access_revoked(roots, record)
            if (
                record.volume_id != volume_id
                or record.size_bytes != size_bytes
                or record.lower_graph_digest != lower_graph_digest
            ):
                raise StateError("OCI-root retained volume conflicts with requested lower graph")
            if record.status != "creating" and not file_exists:
                raise StateError("OCI-root volume artifact and owner record are inconsistent")
            if record.status == "creating":
                if (
                    record.attached_run_id != owner.run_id
                    or record.attached_run_name != owner.run_name
                    or record.retention_policy != retention_policy
                ):
                    raise StateError("OCI-root volume creation belongs to another run")
                quarantine = _deletion_quarantine(roots, volume_id)
                if quarantine.exists() or quarantine.is_symlink():
                    _delete_ext4_raw_file_locked(
                        path,
                        size_bytes,
                        label,
                        volume_id,
                        runner,
                        quarantine_path=quarantine,
                    )
                    file_exists = False
                _ensure_ext4_raw_file_locked(
                    path,
                    size_bytes,
                    label,
                    volume_id,
                    runner,
                    creation_temp_path=_creation_temporary(roots, volume_id),
                    filesystem_uuid=volume_id,
                )
                attached = OCIRootVolumeRecord(
                    volume_id,
                    size_bytes,
                    lower_graph_digest,
                    retention_policy,
                    "attached",
                    owner.run_id,
                    owner.run_name,
                    record.generation + 1,
                )
                _write_record(directory_fd, record_path, attached)
                return ClaimedOCIRootVolume(attached, path, True, False)
            if file_exists:
                _verify_kvm_path(path, size_bytes, label, runner)
            if record.status == "deleting":
                raise StateError("OCI-root volume deletion must be reconciled before reuse")
            if record.status == "attached":
                if (
                    record.attached_run_id != owner.run_id
                    or record.attached_run_name != owner.run_name
                    or record.retention_policy != retention_policy
                ):
                    raise StateError("OCI-root volume is attached to another run")
                return ClaimedOCIRootVolume(record, path, False, False)
            claimed = OCIRootVolumeRecord(
                volume_id,
                size_bytes,
                lower_graph_digest,
                retention_policy,
                "attached",
                owner.run_id,
                owner.run_name,
                record.generation + 1,
            )
            _write_record(directory_fd, record_path, claimed)
            return ClaimedOCIRootVolume(claimed, path, False, True)

        from .oci_root_access import _evidence

        if _evidence(roots, volume_id) != (None, None):
            raise StateError("enrolled OCI-root volume ID cannot be recreated")
        creating = OCIRootVolumeRecord(
            volume_id,
            size_bytes,
            lower_graph_digest,
            retention_policy,
            "creating",
            owner.run_id,
            owner.run_name,
            1,
        )
        _write_record(directory_fd, record_path, creating)
        try:
            created = _ensure_ext4_raw_file_locked(
                path,
                size_bytes,
                label,
                volume_id,
                runner,
                creation_temp_path=_creation_temporary(roots, volume_id),
                filesystem_uuid=volume_id,
            )
            if not created:
                raise StateError("OCI-root volume appeared without an owner record")
            record = OCIRootVolumeRecord(
                volume_id,
                size_bytes,
                lower_graph_digest,
                retention_policy,
                "attached",
                owner.run_id,
                owner.run_name,
                2,
            )
            _write_record(directory_fd, record_path, record)
        except BaseException:
            try:
                _delete_ext4_raw_file_locked(
                    path,
                    size_bytes,
                    label,
                    volume_id,
                    runner,
                    quarantine_path=_deletion_quarantine(roots, volume_id),
                )
                _remove_record(directory_fd, record_path)
            except BaseException:
                pass
            raise
        return ClaimedOCIRootVolume(record, path, True, False)


def load_oci_root_volume(
    roots: StatePaths,
    volume_id: str,
    *,
    runner: CommandRunner = _default_runner,
    root_access=None,
) -> VerifiedOCIRootVolume:
    path, record_path, lock_path = _paths(roots, volume_id)
    with file_lock(lock_path), _root_authority(roots) as directory_fd:
        record = _read_record(directory_fd, record_path)
        if record.status in {"creating", "deleting"}:
            raise StateError("OCI-root volume lifecycle is incomplete")
        file_fd: int | None = None
        try:
            entry = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            file_fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
            opened = os.fstat(file_fd)
            from .oci_root_access import verify_volume_root_access

            managed = verify_volume_root_access(roots, record, file_fd, receipt=root_access)
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (not managed and stat.S_IMODE(opened.st_mode) != 0o600)
                or opened.st_size != record.size_bytes
            ):
                raise StateError("OCI-root volume data file is unsafe")

            def verify_access():
                if _read_record(directory_fd, record_path) != record:
                    raise StateError("OCI-root volume generation changed")
                verify_volume_root_access(roots, record, file_fd, receipt=root_access)

            if managed:
                _verify_kvm_path(
                    path, record.size_bytes, oci_root_volume_label(volume_id), runner, access_validator=verify_access
                )
            else:
                _verify_kvm_path(path, record.size_bytes, oci_root_volume_label(volume_id), runner)
            visible = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
                raise StateError("OCI-root volume data file changed during verification")
            superblock = os.pread(file_fd, EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET)
            filesystem_uuid = str(uuid.UUID(bytes=superblock[104:120]))
            verify_ext4_superblock(
                superblock,
                device_size=record.size_bytes,
                volume_id=record.volume_id,
                filesystem_uuid=filesystem_uuid,
            )
            after = os.fstat(file_fd)
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
                after.st_size,
                None if managed else after.st_mtime_ns,
                None if managed else after.st_ctime_ns,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_uid,
                opened.st_nlink,
                opened.st_size,
                None if managed else opened.st_mtime_ns,
                None if managed else opened.st_ctime_ns,
            ) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise StateError("OCI-root volume data file changed during verification")
            if managed:
                verify_access()
        except StateError:
            raise
        except (OSError, ValueError, ArtifactValidationError):
            raise StateError("OCI-root volume ext4 identity is invalid") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
        return VerifiedOCIRootVolume(record, path, filesystem_uuid)


@contextmanager
def _locked_exact_root_volume(
    roots: StatePaths,
    expected: OCIRootVolumeRecord,
    *,
    filesystem_uuid: str,
    allow_retained_successor: bool = False,
    runner: CommandRunner = _default_runner,
) -> Iterator[tuple[OCIRootVolumeRecord, tuple[int, int], Callable[[OCIRootVolumeRecord], None], int, Path]]:
    """Hold exact volume identity; yielded verification performs no store calls."""
    if type(expected) is not OCIRootVolumeRecord:
        raise StateError("exact root retention record is invalid")
    expected.__post_init__()
    if expected.retention_policy != "retain" or expected.status != "attached":
        raise StateError("exact root retention requires an originally retained attachment")
    retained = (
        replace(
            expected,
            status="retained",
            attached_run_id=None,
            attached_run_name=None,
            generation=expected.generation + 1,
        )
        if allow_retained_successor
        else None
    )
    path, record_path, lock_path = _paths(roots, expected.volume_id)
    with _RetentionVolumeLock(roots, lock_path) as volume_lock, _root_authority(roots) as directory_fd:
        directory_identity = os.fstat(directory_fd)
        current = _read_record(directory_fd, record_path)
        from .oci_root_access import require_root_access_revoked

        require_root_access_revoked(roots, current)
        if current not in (expected, retained):
            raise StateError("root retention attachment generation changed")
        file_fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            opened = os.fstat(file_fd)
            identity = (opened.st_dev, opened.st_ino)
            fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )

            def verify(expected_current: OCIRootVolumeRecord) -> None:
                volume_lock.verify()
                require_root_access_revoked(roots, expected_current)
                held_directory = os.fstat(directory_fd)
                visible_directory = os.stat(roots.oci_root_volumes, follow_symlinks=False)
                if any(
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o022 != 0
                    or (info.st_dev, info.st_ino) != (directory_identity.st_dev, directory_identity.st_ino)
                    for info in (held_directory, visible_directory)
                ):
                    raise StateError("root retention volume directory changed")
                held, visible = os.fstat(file_fd), os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_size != expected.size_bytes
                    or any(
                        getattr(info, field) != getattr(opened, field) for info in (held, visible) for field in fields
                    )
                    or _read_record(directory_fd, record_path) != expected_current
                ):
                    raise StateError("root retention volume authority changed")
                verify_ext4_superblock(
                    os.pread(file_fd, EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET),
                    device_size=expected.size_bytes,
                    volume_id=expected.volume_id,
                    filesystem_uuid=filesystem_uuid,
                )

            verify(current)
            _verify_kvm_path(path, expected.size_bytes, oci_root_volume_label(expected.volume_id), runner)
            verify(current)
            yield current, identity, verify, directory_fd, record_path
        finally:
            os.close(file_fd)


def _retain_exact_oci_root_volume(
    roots: StatePaths,
    expected: OCIRootVolumeRecord,
    *,
    filesystem_uuid: str,
    before_retention: Callable[[OCIRootVolumeRecord, tuple[int, int]], None],
    after_retention: Callable[[OCIRootVolumeRecord, tuple[int, int]], None],
    runner: CommandRunner = _default_runner,
) -> OCIRootVolumeRecord:
    """Private retain-only CAS; callbacks run inside the volume lock."""
    with _locked_exact_root_volume(
        roots,
        expected,
        filesystem_uuid=filesystem_uuid,
        allow_retained_successor=True,
        runner=runner,
    ) as (current, identity, verify, directory_fd, record_path):
        retained = replace(
            expected,
            status="retained",
            attached_run_id=None,
            attached_run_name=None,
            generation=expected.generation + 1,
        )
        before_retention(current, identity)
        verify(current)
        if current == expected:
            _write_record(directory_fd, record_path, retained)
        verify(retained)
        after_retention(retained, identity)
        verify(retained)
        return retained


def release_oci_root_volume(
    roots: StatePaths,
    volume_id: str,
    *,
    owner: ArtifactLeaseOwner,
    lower_graph_digest: str,
    delete: bool | None = None,
    runner: CommandRunner = _default_runner,
) -> OCIRootVolumeRecord | None:
    """Detach to retained state or idempotently delete an exact attachment."""
    _validate_owner(owner)
    normalized_lower_graph = normalize_digest(lower_graph_digest)
    if normalized_lower_graph != lower_graph_digest:
        raise StateError("OCI-root volume lower graph digest is not canonical")
    path, record_path, lock_path = _paths(roots, volume_id)
    with file_lock(lock_path), _root_authority(roots) as directory_fd:
        try:
            record = _read_record(directory_fd, record_path)
        except StateError as exc:
            if str(exc) == "OCI-root volume record is missing" and not path.exists() and not path.is_symlink():
                return None
            raise
        if (
            record.volume_id != volume_id
            or record.lower_graph_digest != lower_graph_digest
            or record.attached_run_id != owner.run_id
            or record.attached_run_name != owner.run_name
            or record.status not in {"creating", "attached", "deleting"}
        ):
            raise StateError("OCI-root volume release binding is invalid")
        should_delete = record.retention_policy == "delete" if delete is None else delete
        from .oci_root_access import require_root_access_revoked

        require_root_access_revoked(roots, record, allow_deleting=should_delete)
        if not should_delete:
            if record.status != "attached":
                raise StateError("deleting OCI-root volume cannot be retained")
            retained = OCIRootVolumeRecord(
                record.volume_id,
                record.size_bytes,
                record.lower_graph_digest,
                record.retention_policy,
                "retained",
                None,
                None,
                record.generation + 1,
            )
            _write_record(directory_fd, record_path, retained)
            return retained

        deleting = record
        if record.status == "creating":
            _cleanup_creation_temporary(roots, directory_fd, path, volume_id)
        if record.status not in {"creating", "deleting"}:
            deleting = OCIRootVolumeRecord(
                record.volume_id,
                record.size_bytes,
                record.lower_graph_digest,
                record.retention_policy,
                "deleting",
                record.attached_run_id,
                record.attached_run_name,
                record.generation + 1,
            )
            _write_record(directory_fd, record_path, deleting)
        from .oci_root_access import validate_root_deletion_path

        def validate_deletion(candidate):
            if _read_record(directory_fd, record_path) != deleting:
                raise StateError("OCI-root deletion generation changed")
            validate_root_deletion_path(roots, deleting, candidate)
            if _read_record(directory_fd, record_path) != deleting:
                raise StateError("OCI-root deletion generation changed")

        _delete_ext4_raw_file_locked(
            path,
            deleting.size_bytes,
            oci_root_volume_label(volume_id),
            volume_id,
            runner,
            quarantine_path=_deletion_quarantine(roots, volume_id),
            access_validator=validate_deletion,
        )
        _remove_record(directory_fd, record_path)
        return None


def rollback_oci_root_volume_claim(
    roots: StatePaths,
    claimed: ClaimedOCIRootVolume,
    *,
    owner: ArtifactLeaseOwner,
    runner: CommandRunner = _default_runner,
) -> None:
    if not claimed.created and not claimed.claimed_from_retained:
        return
    release_oci_root_volume(
        roots,
        claimed.record.volume_id,
        owner=owner,
        lower_graph_digest=claimed.record.lower_graph_digest,
        delete=True if claimed.created else False,
        runner=runner,
    )


def list_oci_root_volume_records(roots: StatePaths) -> tuple[OCIRootVolumeRecord, ...]:
    found: list[OCIRootVolumeRecord] = []
    raw_names: set[str] = set()
    record_stems: set[str] = set()
    with _root_authority(roots) as directory_fd:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            raise StateError("OCI-root volumes cannot be enumerated") from None
        for name in names:
            if name.startswith("."):
                continue
            if name.endswith(".raw") and _HEX_RE.fullmatch(name[:-4]) is not None:
                raw_names.add(name[:-4])
                continue
            if not name.endswith(".json") or _HEX_RE.fullmatch(name[:-5]) is None:
                raise StateError("OCI-root volume namespace contains an invalid entry")
            record = OCIRootVolumeRecord.from_dict(_strict_json_load(directory_fd, name))
            stem = record.volume_id.replace("-", "")
            if stem != name[:-5]:
                raise StateError("OCI-root volume record filename is invalid")
            record_stems.add(stem)
            found.append(record)
        expected_raws = {
            record.volume_id.replace("-", "")
            for record in found
            if record.status != "creating" or record.volume_id.replace("-", "") in raw_names
        }
        if raw_names != expected_raws or not expected_raws.issubset(record_stems):
            raise StateError("OCI-root volume artifact and owner records are inconsistent")
    return tuple(found)


__all__ = [
    "ClaimedOCIRootVolume",
    "MAX_OCI_ROOT_VOLUME_GENERATION",
    "MAX_OCI_ROOT_VOLUME_GENERATION_DIGITS",
    "OCI_ROOT_VOLUME_RETENTION_POLICIES",
    "OCI_ROOT_VOLUME_SCHEMA",
    "OCIRootVolumeRecord",
    "VerifiedOCIRootVolume",
    "claim_oci_root_volume",
    "list_oci_root_volume_records",
    "load_oci_root_volume",
    "new_oci_root_volume_id",
    "oci_root_volume_label",
    "release_oci_root_volume",
    "rollback_oci_root_volume_claim",
]
