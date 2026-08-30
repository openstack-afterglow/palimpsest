"""Secure local OCI-layout snapshotting into a private source CAS.

The mutable layout is only a transport.  Every selected content descriptor is
opened without following symlinks, verified and copied from one pinned source
file descriptor, then atomically promoted into an owner-only CAS.  Returned
snapshot contracts contain content and CAS identities, never source or store
paths.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .errors import ArtifactValidationError
from .oci_image import (
    MAX_IMAGE_JSON_BYTES,
    OCIImage,
    OCIImageRef,
    resolve_image,
    strict_json_object,
)
from .oci_provenance import (
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    SUPPORTED_IMAGE_INDEX_MEDIA_TYPES,
    SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES,
    Descriptor,
    canonical_json_bytes,
)

_READ_CHUNK = 1024 * 1024
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_TEMP_MODE = 0o600
_PRIVATE_BLOB_MODE = 0o400
_PRIVATE_LOCK_MODE = 0o600

_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW

_SYS_OPENAT2 = 437
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


def _openat2_directory(root_fd: int, relative_path: str) -> int | None:
    """Use Linux openat2 when available; return ``None`` for secure fallback."""
    if sys.platform != "linux":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    how = _OpenHow(
        flags=_DIR_FLAGS,
        mode=0,
        resolve=_RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS,
    )
    result = syscall(
        _SYS_OPENAT2,
        ctypes.c_int(root_fd),
        ctypes.c_char_p(os.fsencode(relative_path)),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if result >= 0:
        return int(result)
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL}:
        return None
    raise OSError(error, os.strerror(error), relative_path)


class SnapshotStage(StrEnum):
    AFTER_ROOT_OPEN = "after-root-open"
    AFTER_COMPONENT_OPEN = "after-component-open"
    AFTER_BLOB_OPEN = "after-blob-open"
    AFTER_INITIAL_FSTAT = "after-initial-fstat"
    AFTER_COPY_CHUNK = "after-copy-chunk"
    BEFORE_FINAL_FSTAT = "before-final-fstat"
    AFTER_TEMP_FSYNC = "after-temp-fsync"
    AFTER_LOCK = "after-lock"
    BEFORE_PROMOTE = "before-promote"
    AFTER_PROMOTE = "after-promote"
    BEFORE_CAS_REOPEN = "before-cas-reopen"
    BEFORE_RESULT = "before-result"


@dataclass(frozen=True, slots=True)
class SnapshotCheckpoint:
    stage: SnapshotStage
    digest: str | None = None
    component: str | None = None
    bytes_copied: int = 0


Checkpoint = Callable[[SnapshotCheckpoint], None]


def _noop_checkpoint(_checkpoint: SnapshotCheckpoint) -> None:
    return None


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _private_directory_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_uid, stat.S_IMODE(metadata.st_mode)


def _close_noerror(file_fd: int | None) -> None:
    if file_fd is None:
        return
    try:
        os.close(file_fd)
    except OSError:
        pass


def _write_all(file_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(file_fd, payload[offset:])
        except OSError:
            raise ArtifactValidationError("cannot write private source-CAS temporary") from None
        if written <= 0:
            raise ArtifactValidationError("cannot write private source-CAS temporary")
        offset += written


def _notify(
    checkpoint: Checkpoint,
    stage: SnapshotStage,
    *,
    digest: str | None = None,
    component: str | None = None,
    bytes_copied: int = 0,
) -> None:
    checkpoint(SnapshotCheckpoint(stage=stage, digest=digest, component=component, bytes_copied=bytes_copied))


@contextmanager
def _open_absolute_directory(path: Path, checkpoint: Checkpoint) -> Iterator[int]:
    raw_path = os.fspath(path)
    if not path.is_absolute() or "\x00" in raw_path:
        raise ArtifactValidationError("OCI layout path must be an absolute path without NUL bytes")
    components = path.parts[1:]
    if any(component in {"", ".", ".."} for component in components):
        raise ArtifactValidationError("OCI layout path must not contain relative components")
    opened: list[int] = []
    try:
        try:
            current = os.open("/", _DIR_FLAGS)
        except OSError:
            raise ArtifactValidationError("cannot securely open OCI layout root") from None
        opened.append(current)
        if components:
            try:
                fast_fd = _openat2_directory(current, "/".join(components))
            except OSError:
                raise ArtifactValidationError("cannot securely open OCI layout directory") from None
            if fast_fd is not None:
                opened.append(fast_fd)
                metadata = os.fstat(fast_fd)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ArtifactValidationError("OCI layout path is not a directory")
                _notify(checkpoint, SnapshotStage.AFTER_COMPONENT_OPEN, component="openat2")
                _notify(checkpoint, SnapshotStage.AFTER_ROOT_OPEN)
                yield fast_fd
                return
        for component in components:
            try:
                current = os.open(component, _DIR_FLAGS, dir_fd=current)
            except OSError:
                raise ArtifactValidationError("cannot securely open OCI layout directory") from None
            opened.append(current)
            metadata = os.fstat(current)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactValidationError("OCI layout component is not a directory")
            _notify(checkpoint, SnapshotStage.AFTER_COMPONENT_OPEN, component=component)
        _notify(checkpoint, SnapshotStage.AFTER_ROOT_OPEN)
        yield current
    finally:
        for file_fd in reversed(opened):
            _close_noerror(file_fd)


def _open_child_directory(parent_fd: int, name: str, checkpoint: Checkpoint) -> int:
    child_fd: int | None = None
    try:
        child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        metadata = os.fstat(child_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactValidationError(f"OCI layout component {name!r} is not a directory")
        _notify(checkpoint, SnapshotStage.AFTER_COMPONENT_OPEN, component=name)
        result = child_fd
        child_fd = None
        return result
    except ArtifactValidationError:
        raise
    except OSError:
        raise ArtifactValidationError(f"cannot securely open OCI layout component {name!r}") from None
    finally:
        _close_noerror(child_fd)


def _read_stable_regular_file(parent_fd: int, name: str, *, max_bytes: int) -> bytes:
    file_fd: int | None = None
    try:
        try:
            entry_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            file_fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
            opened_before = os.fstat(file_fd)
        except OSError:
            raise ArtifactValidationError(f"cannot securely read OCI layout file {name!r}") from None
        if (
            not stat.S_ISREG(entry_before.st_mode)
            or not stat.S_ISREG(opened_before.st_mode)
            or entry_before.st_nlink != 1
            or opened_before.st_nlink != 1
            or _identity(entry_before) != _identity(opened_before)
            or opened_before.st_size > max_bytes
        ):
            raise ArtifactValidationError(f"OCI layout file {name!r} is unsafe or too large")
        payload = bytearray()
        while True:
            try:
                chunk = os.read(file_fd, min(_READ_CHUNK, max_bytes + 1 - len(payload)))
            except OSError:
                raise ArtifactValidationError(f"cannot securely read OCI layout file {name!r}") from None
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ArtifactValidationError(f"OCI layout file {name!r} is too large")
        try:
            opened_after = os.fstat(file_fd)
            entry_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise ArtifactValidationError(f"OCI layout file {name!r} changed during read") from None
        if (
            _stable_metadata(opened_before) != _stable_metadata(opened_after)
            or _stable_metadata(opened_before) != _stable_metadata(entry_after)
            or len(payload) != opened_before.st_size
        ):
            raise ArtifactValidationError(f"OCI layout file {name!r} changed during read")
        return bytes(payload)
    finally:
        _close_noerror(file_fd)


def _ensure_private_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute == Path("/") or not absolute.name:
        raise ArtifactValidationError("source CAS root must be a private child directory")
    parent = absolute.parent
    with _open_absolute_directory(parent, _noop_checkpoint) as parent_fd:
        created = False
        try:
            try:
                os.mkdir(absolute.name, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            root_fd = os.open(absolute.name, _DIR_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(root_fd)
            entry = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise ArtifactValidationError("cannot create private source CAS") from None
        finally:
            if "root_fd" in locals():
                _close_noerror(root_fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != _PRIVATE_DIRECTORY_MODE
            or _identity(opened) != _identity(entry)
        ):
            raise ArtifactValidationError("source CAS root must be an owner-only directory")
        if created:
            try:
                os.fsync(parent_fd)
            except OSError:
                raise ArtifactValidationError("cannot durably create private source CAS") from None
    return absolute


def _ensure_private_child(parent_fd: int, name: str) -> int:
    created = False
    try:
        try:
            os.mkdir(name, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != _PRIVATE_DIRECTORY_MODE
            or _identity(opened) != _identity(entry)
        ):
            raise ArtifactValidationError("source CAS component must be an owner-only directory")
        if created:
            os.fsync(parent_fd)
        return child_fd
    except ArtifactValidationError:
        if "child_fd" in locals():
            _close_noerror(child_fd)
        raise
    except OSError:
        if "child_fd" in locals():
            _close_noerror(child_fd)
        raise ArtifactValidationError("cannot create private source-CAS component") from None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One descriptor backed by an identified private source CAS."""

    descriptor: Descriptor
    cas_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, Descriptor):
            raise ArtifactValidationError("source snapshot descriptor must be a Descriptor")
        if not isinstance(self.cas_id, str) or not self.cas_id.startswith("source-cas-v1:"):
            raise ArtifactValidationError("source snapshot has an invalid CAS identity")

    def to_dict(self) -> dict[str, Any]:
        return {"cas_id": self.cas_id, "descriptor": self.descriptor.to_dict()}


@dataclass(frozen=True, slots=True)
class SnapshottedOCIImage:
    """A selected OCI graph whose complete source payload set is in one CAS."""

    image: OCIImage
    root: SourceSnapshot
    manifest: SourceSnapshot
    config: SourceSnapshot
    layers: tuple[SourceSnapshot, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.image, OCIImage):
            raise ArtifactValidationError("snapshotted image must contain an OCIImage")
        if any(not isinstance(item, SourceSnapshot) for item in (self.root, self.manifest, self.config)):
            raise ArtifactValidationError("snapshotted image metadata members must be SourceSnapshot values")
        if not isinstance(self.layers, tuple) or any(not isinstance(item, SourceSnapshot) for item in self.layers):
            raise ArtifactValidationError("snapshotted image layers must be an immutable SourceSnapshot tuple")
        expected_root = self.image.index_descriptor or self.image.manifest_descriptor
        if self.root.descriptor != expected_root:
            raise ArtifactValidationError("root snapshot does not match the selected image root")
        if self.manifest.descriptor != self.image.manifest_descriptor:
            raise ArtifactValidationError("manifest snapshot does not match the selected image")
        if self.config.descriptor != self.image.config.descriptor:
            raise ArtifactValidationError("config snapshot does not match the selected image")
        if tuple(item.descriptor for item in self.layers) != tuple(
            occurrence.compressed for occurrence in self.image.layers
        ):
            raise ArtifactValidationError("layer snapshots do not match selected image occurrence order")
        cas_ids = {item.cas_id for item in (self.root, self.manifest, self.config, *self.layers)}
        if len(cas_ids) != 1:
            raise ArtifactValidationError("snapshotted image members must belong to one source CAS")

    @property
    def cas_id(self) -> str:
        return self.root.cas_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_digest": self.binding_digest,
            "cas_id": self.cas_id,
            "config": self.config.to_dict(),
            "image_digest": self.image.digest,
            "layers": [item.to_dict() for item in self.layers],
            "manifest": self.manifest.to_dict(),
            "root": self.root.to_dict(),
        }

    @property
    def binding_digest(self) -> str:
        members = {
            "cas_id": self.cas_id,
            "config": self.config.descriptor.to_dict(),
            "domain": "palimpsest.source-snapshot.v1",
            "image_digest": self.image.digest,
            "layers": [item.descriptor.to_dict() for item in self.layers],
            "manifest": self.manifest.descriptor.to_dict(),
            "root": self.root.descriptor.to_dict(),
        }
        return f"sha256:{hashlib.sha256(canonical_json_bytes(members)).hexdigest()}"


@dataclass(slots=True)
class _CASAuthority:
    root_fd: int
    blobs_parent_fd: int
    blobs_fd: int
    locks_fd: int
    cas_id: str
    signature: tuple[tuple[int, int, int, int], ...]


class SourceLeaseError(ArtifactValidationError):
    """A descriptor-pinned source-layer lease could not be verified."""


class LeasedSourceLayer:
    """One occurrence-bound, single-use stream from the private source CAS.

    The capability intentionally exposes neither a path nor a raw file
    descriptor.  Successful context exit requires that ``chunks()`` reached
    EOF and verified the exact descriptor bytes from the pinned open file.
    """

    __slots__ = (
        "_authority",
        "_closed",
        "_descriptor",
        "_diff_id",
        "_entry_name",
        "_file_fd",
        "_initial_metadata",
        "_media_type",
        "_ordinal",
        "_owner_pid",
        "_owner_thread",
        "_started",
        "_verified",
    )

    def __init__(
        self,
        *,
        authority: _CASAuthority,
        descriptor: Descriptor,
        diff_id: str,
        ordinal: int,
        entry_name: str,
        file_fd: int,
        initial_metadata: os.stat_result,
    ) -> None:
        self._authority = authority
        self._descriptor = descriptor
        self._entry_name = entry_name
        self._file_fd = file_fd
        self._initial_metadata = initial_metadata
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._started = False
        self._verified = False
        self._closed = False
        self._ordinal = ordinal
        self._media_type = descriptor.media_type
        self._diff_id = diff_id

    @property
    def ordinal(self) -> int:
        return self._ordinal

    @property
    def media_type(self) -> str:
        return self._media_type

    @property
    def diff_id(self) -> str:
        return self._diff_id

    @property
    def compressed_digest(self) -> str:
        return self._descriptor.digest

    @property
    def compressed_size(self) -> int:
        return self._descriptor.size

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            if not self._closed:
                self._closed = True
                _close_noerror(self._file_fd)
                self._file_fd = -1
            raise SourceLeaseError("source layer lease cannot cross a process boundary")
        if threading.get_ident() != self._owner_thread:
            raise SourceLeaseError("source layer lease cannot cross a process or thread boundary")

    def _check_readable(self) -> None:
        self._check_owner()
        if self._closed:
            raise SourceLeaseError("source layer lease is closed")

    def chunks(self, chunk_size: int = _READ_CHUNK) -> Iterator[bytes]:
        """Yield descriptor bytes once and verify the pinned target at EOF."""
        self._check_readable()
        if type(chunk_size) is not int or not 1 <= chunk_size <= _READ_CHUNK:
            raise SourceLeaseError("source layer lease chunk size is out of range")
        if self._started:
            raise SourceLeaseError("source layer lease stream is single-use")
        self._started = True
        hasher = hashlib.sha256()
        total = 0
        while True:
            self._check_readable()
            try:
                chunk = os.read(self._file_fd, chunk_size)
            except OSError:
                raise SourceLeaseError("cannot read source layer lease") from None
            if not chunk:
                break
            total += len(chunk)
            if total > self._descriptor.size:
                raise SourceLeaseError("source layer lease exceeds its descriptor size")
            hasher.update(chunk)
            yield chunk
        self._check_readable()
        try:
            opened_after = os.fstat(self._file_fd)
            entry_after = os.stat(
                self._entry_name,
                dir_fd=self._authority.blobs_fd,
                follow_symlinks=False,
            )
        except OSError:
            raise SourceLeaseError("source layer lease target changed") from None
        if (
            total != self._descriptor.size
            or f"sha256:{hasher.hexdigest()}" != self._descriptor.digest
            or _stable_metadata(self._initial_metadata) != _stable_metadata(opened_after)
            or _stable_metadata(self._initial_metadata) != _stable_metadata(entry_after)
        ):
            raise SourceLeaseError("source layer lease failed descriptor verification")
        self._verified = True

    def close(self) -> None:
        """Cooperatively revoke the capability; safe to call more than once."""
        self._check_owner()
        if not self._closed:
            self._closed = True
            _close_noerror(self._file_fd)
            self._file_fd = -1

    def _abort(self) -> None:
        if not self._closed:
            self._closed = True
            _close_noerror(self._file_fd)
            self._file_fd = -1

    def __copy__(self) -> LeasedSourceLayer:
        raise SourceLeaseError("source layer lease cannot be copied")

    def __deepcopy__(self, _memo: object) -> LeasedSourceLayer:
        raise SourceLeaseError("source layer lease cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("source layer lease cannot be serialized")

    def __repr__(self) -> str:
        state = "verified" if self._verified else "open"
        if self._closed:
            state = "closed"
        return (
            "LeasedSourceLayer("
            f"ordinal={self.ordinal}, media_type={self.media_type!r}, "
            f"compressed_digest={self.compressed_digest!r}, state={state!r})"
        )


class SourceCAS:
    """Owner-only CAS authority used to reopen verified source snapshots."""

    def __init__(self, root: Path):
        self._root = _ensure_private_root(root)
        with self._authority() as authority:
            pass
        self._cas_id = authority.cas_id
        self._signature = authority.signature

    @property
    def identity(self) -> str:
        return self._cas_id

    @staticmethod
    def _signature_id(signature: tuple[tuple[int, int, int, int], ...]) -> str:
        payload = "|".join(":".join(str(value) for value in item) for item in signature).encode()
        return f"source-cas-v1:{hashlib.sha256(payload).hexdigest()}"

    def _verify_authority_binding(self, authority: _CASAuthority) -> None:
        try:
            with _open_absolute_directory(self._root, _noop_checkpoint) as visible_root_fd:
                visible_root = os.fstat(visible_root_fd)
                root_open = os.fstat(authority.root_fd)
                blobs_parent_open = os.fstat(authority.blobs_parent_fd)
                blobs_open = os.fstat(authority.blobs_fd)
                locks_open = os.fstat(authority.locks_fd)
                blobs_parent_entry = os.stat("blobs", dir_fd=authority.root_fd, follow_symlinks=False)
                blobs_entry = os.stat("sha256", dir_fd=authority.blobs_parent_fd, follow_symlinks=False)
                locks_entry = os.stat("locks", dir_fd=authority.root_fd, follow_symlinks=False)
        except (ArtifactValidationError, OSError):
            raise ArtifactValidationError("source CAS authority changed") from None
        open_signature = tuple(
            _private_directory_signature(item) for item in (root_open, blobs_parent_open, blobs_open, locks_open)
        )
        entry_signature = tuple(
            _private_directory_signature(item) for item in (visible_root, blobs_parent_entry, blobs_entry, locks_entry)
        )
        expected_signature = getattr(self, "_signature", authority.signature)
        expected_cas_id = getattr(self, "_cas_id", authority.cas_id)
        if (
            open_signature != authority.signature
            or entry_signature != authority.signature
            or authority.signature != expected_signature
            or authority.cas_id != expected_cas_id
        ):
            raise ArtifactValidationError("source CAS authority changed")

    @contextmanager
    def _authority(self) -> Iterator[_CASAuthority]:
        blobs_parent_fd: int | None = None
        blobs_fd: int | None = None
        locks_fd: int | None = None
        with _open_absolute_directory(self._root, _noop_checkpoint) as root_fd:
            try:
                root_stat = os.fstat(root_fd)
                if (
                    not stat.S_ISDIR(root_stat.st_mode)
                    or root_stat.st_uid != os.geteuid()
                    or stat.S_IMODE(root_stat.st_mode) != _PRIVATE_DIRECTORY_MODE
                ):
                    raise ArtifactValidationError("source CAS authority changed")
                blobs_parent_fd = _ensure_private_child(root_fd, "blobs")
                blobs_fd = _ensure_private_child(blobs_parent_fd, "sha256")
                locks_fd = _ensure_private_child(root_fd, "locks")
                signature = tuple(
                    _private_directory_signature(os.fstat(file_fd))
                    for file_fd in (root_fd, blobs_parent_fd, blobs_fd, locks_fd)
                )
                authority = _CASAuthority(
                    root_fd=root_fd,
                    blobs_parent_fd=blobs_parent_fd,
                    blobs_fd=blobs_fd,
                    locks_fd=locks_fd,
                    cas_id=self._signature_id(signature),
                    signature=signature,
                )
                expected_signature = getattr(self, "_signature", signature)
                expected_cas_id = getattr(self, "_cas_id", authority.cas_id)
                if signature != expected_signature or authority.cas_id != expected_cas_id:
                    raise ArtifactValidationError("source CAS authority changed")
                self._verify_authority_binding(authority)
                yield authority
                self._verify_authority_binding(authority)
            finally:
                _close_noerror(locks_fd)
                _close_noerror(blobs_fd)
                _close_noerror(blobs_parent_fd)

    @contextmanager
    def _digest_lock(self, authority: _CASAuthority, digest_hex: str, checkpoint: Checkpoint) -> Iterator[None]:
        lock_fd: int | None = None
        name = f"{digest_hex}.lock"
        try:
            self._verify_authority_binding(authority)
            created = False
            try:
                try:
                    lock_fd = os.open(
                        name,
                        _LOCK_FLAGS | os.O_EXCL,
                        _PRIVATE_LOCK_MODE,
                        dir_fd=authority.locks_fd,
                    )
                    created = True
                    prior = None
                except FileExistsError:
                    prior = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                    lock_fd = os.open(
                        name,
                        os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=authority.locks_fd,
                    )
                opened = os.fstat(lock_fd)
            except OSError:
                raise ArtifactValidationError("cannot securely open source-CAS digest lock") from None
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (prior is not None and _identity(prior) != _identity(opened))
            ):
                raise ArtifactValidationError("source-CAS digest lock is unsafe")
            try:
                os.fchmod(lock_fd, _PRIVATE_LOCK_MODE)
                os.fsync(lock_fd)
                if created:
                    os.fsync(authority.locks_fd)
                entry = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                if (
                    _identity(entry) != _identity(opened)
                    or stat.S_IMODE(entry.st_mode) != _PRIVATE_LOCK_MODE
                    or entry.st_nlink != 1
                ):
                    raise ArtifactValidationError("source-CAS digest lock changed before acquire")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                entry = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                if _identity(entry) != _identity(opened):
                    raise ArtifactValidationError("source-CAS digest lock changed during acquire")
                self._verify_authority_binding(authority)
            except ArtifactValidationError:
                raise
            except OSError:
                raise ArtifactValidationError("cannot acquire source-CAS digest lock") from None
            _notify(checkpoint, SnapshotStage.AFTER_LOCK, digest=f"sha256:{digest_hex}")
            yield
            self._verify_authority_binding(authority)
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            _close_noerror(lock_fd)

    def _read_target(
        self,
        authority: _CASAuthority,
        descriptor: Descriptor,
        *,
        capture: bool,
    ) -> bytes | None:
        name = descriptor.digest.split(":", 1)[1]
        file_fd: int | None = None
        try:
            try:
                entry_before = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
                file_fd = os.open(name, _READ_FLAGS, dir_fd=authority.blobs_fd)
                opened_before = os.fstat(file_fd)
            except FileNotFoundError:
                return None
            except OSError:
                return None
            if (
                not stat.S_ISREG(entry_before.st_mode)
                or not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_uid != os.geteuid()
                or opened_before.st_nlink != 1
                or stat.S_IMODE(opened_before.st_mode) != _PRIVATE_BLOB_MODE
                or opened_before.st_size != descriptor.size
                or _identity(entry_before) != _identity(opened_before)
            ):
                return None
            payload = bytearray() if capture else None
            hasher = hashlib.sha256()
            total = 0
            while True:
                try:
                    chunk = os.read(file_fd, _READ_CHUNK)
                except OSError:
                    return None
                if not chunk:
                    break
                total += len(chunk)
                if payload is not None:
                    payload.extend(chunk)
                hasher.update(chunk)
                if total > descriptor.size:
                    return None
            try:
                opened_after = os.fstat(file_fd)
                entry_after = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
            except OSError:
                return None
            actual = f"sha256:{hasher.hexdigest()}"
            if (
                total != descriptor.size
                or actual != descriptor.digest
                or _stable_metadata(opened_before) != _stable_metadata(opened_after)
                or _stable_metadata(opened_before) != _stable_metadata(entry_after)
            ):
                return None
            return bytes(payload) if payload is not None else b""
        finally:
            _close_noerror(file_fd)

    def import_source_blob(
        self,
        source_directory_fd: int,
        descriptor: Descriptor,
        checkpoint: Checkpoint = _noop_checkpoint,
    ) -> SourceSnapshot:
        """Copy and promote one descriptor from a pinned layout blob directory."""
        if not isinstance(descriptor, Descriptor):
            raise ArtifactValidationError("source-CAS import requires a Descriptor")
        digest_hex = descriptor.digest.split(":", 1)[1]
        source_fd: int | None = None
        temporary_fd: int | None = None
        temporary_name: str | None = None
        cleanup_blobs_fd: int | None = None
        try:
            try:
                entry_before = os.stat(digest_hex, dir_fd=source_directory_fd, follow_symlinks=False)
                source_fd = os.open(digest_hex, _READ_FLAGS, dir_fd=source_directory_fd)
                _notify(checkpoint, SnapshotStage.AFTER_BLOB_OPEN, digest=descriptor.digest)
                opened_before = os.fstat(source_fd)
            except OSError:
                raise ArtifactValidationError(f"cannot securely open OCI blob {descriptor.digest}") from None
            if (
                not stat.S_ISREG(entry_before.st_mode)
                or not stat.S_ISREG(opened_before.st_mode)
                or entry_before.st_nlink != 1
                or opened_before.st_nlink != 1
                or _identity(entry_before) != _identity(opened_before)
                or opened_before.st_size != descriptor.size
            ):
                raise ArtifactValidationError(f"OCI blob {descriptor.digest} is unsafe or has the wrong size")
            _notify(checkpoint, SnapshotStage.AFTER_INITIAL_FSTAT, digest=descriptor.digest)

            with self._authority() as authority:
                if authority.cas_id != self._cas_id:
                    raise ArtifactValidationError("source CAS authority changed")
                temporary_name = f".source-tmp-{uuid.uuid4().hex}"
                try:
                    cleanup_blobs_fd = os.dup(authority.blobs_fd)
                    temporary_fd = os.open(
                        temporary_name,
                        _TEMP_FLAGS,
                        _PRIVATE_TEMP_MODE,
                        dir_fd=authority.blobs_fd,
                    )
                    temporary_before = os.fstat(temporary_fd)
                except OSError:
                    raise ArtifactValidationError("cannot create private source-CAS temporary") from None
                blobs_stat = os.fstat(authority.blobs_fd)
                if (
                    not stat.S_ISREG(temporary_before.st_mode)
                    or temporary_before.st_uid != os.geteuid()
                    or temporary_before.st_nlink != 1
                    or stat.S_IMODE(temporary_before.st_mode) != _PRIVATE_TEMP_MODE
                    or temporary_before.st_dev != blobs_stat.st_dev
                ):
                    raise ArtifactValidationError("source-CAS temporary is unsafe")

                hasher = hashlib.sha256()
                copied = 0
                while True:
                    try:
                        chunk = os.read(source_fd, _READ_CHUNK)
                    except OSError:
                        raise ArtifactValidationError(f"cannot read OCI blob {descriptor.digest}") from None
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > descriptor.size:
                        raise ArtifactValidationError(f"OCI blob {descriptor.digest} grew during copy")
                    hasher.update(chunk)
                    _write_all(temporary_fd, chunk)
                    _notify(
                        checkpoint,
                        SnapshotStage.AFTER_COPY_CHUNK,
                        digest=descriptor.digest,
                        bytes_copied=copied,
                    )
                _notify(checkpoint, SnapshotStage.BEFORE_FINAL_FSTAT, digest=descriptor.digest, bytes_copied=copied)
                try:
                    opened_after = os.fstat(source_fd)
                    entry_after = os.stat(digest_hex, dir_fd=source_directory_fd, follow_symlinks=False)
                except OSError:
                    raise ArtifactValidationError(f"OCI blob {descriptor.digest} changed during copy") from None
                actual = f"sha256:{hasher.hexdigest()}"
                if (
                    copied != descriptor.size
                    or actual != descriptor.digest
                    or _stable_metadata(opened_before) != _stable_metadata(opened_after)
                    or _stable_metadata(opened_before) != _stable_metadata(entry_after)
                ):
                    raise ArtifactValidationError(f"OCI blob {descriptor.digest} changed or failed verification")
                try:
                    os.fchmod(temporary_fd, _PRIVATE_BLOB_MODE)
                    os.fsync(temporary_fd)
                    temporary_after = os.fstat(temporary_fd)
                except OSError:
                    raise ArtifactValidationError("cannot durably stage source-CAS blob") from None
                if (
                    _identity(temporary_before) != _identity(temporary_after)
                    or temporary_after.st_uid != os.geteuid()
                    or temporary_after.st_nlink != 1
                    or stat.S_IMODE(temporary_after.st_mode) != _PRIVATE_BLOB_MODE
                    or temporary_after.st_size != descriptor.size
                ):
                    raise ArtifactValidationError("source-CAS temporary changed during staging")
                _notify(checkpoint, SnapshotStage.AFTER_TEMP_FSYNC, digest=descriptor.digest, bytes_copied=copied)

                with self._digest_lock(authority, digest_hex, checkpoint):
                    existing = self._read_target(authority, descriptor, capture=False)
                    if existing is None:
                        _notify(checkpoint, SnapshotStage.BEFORE_PROMOTE, digest=descriptor.digest)
                        try:
                            os.replace(
                                temporary_name,
                                digest_hex,
                                src_dir_fd=authority.blobs_fd,
                                dst_dir_fd=authority.blobs_fd,
                            )
                            temporary_name = None
                            os.fsync(authority.blobs_fd)
                            published = os.stat(digest_hex, dir_fd=authority.blobs_fd, follow_symlinks=False)
                        except OSError:
                            raise ArtifactValidationError("cannot atomically promote source-CAS blob") from None
                        if _identity(published) != _identity(temporary_after):
                            raise ArtifactValidationError("source-CAS target changed during promotion")
                        _notify(checkpoint, SnapshotStage.AFTER_PROMOTE, digest=descriptor.digest)
                return SourceSnapshot(descriptor=descriptor, cas_id=authority.cas_id)
        finally:
            _close_noerror(temporary_fd)
            if temporary_name is not None and cleanup_blobs_fd is not None:
                try:
                    os.unlink(temporary_name, dir_fd=cleanup_blobs_fd)
                    os.fsync(cleanup_blobs_fd)
                except OSError:
                    pass
            _close_noerror(cleanup_blobs_fd)
            _close_noerror(source_fd)

    def read_metadata(self, snapshot: SourceSnapshot) -> bytes:
        """Read a bounded JSON snapshot; layer streaming is owned by PR 4 leases."""
        if not isinstance(snapshot, SourceSnapshot) or snapshot.cas_id != self._cas_id:
            raise ArtifactValidationError("source snapshot belongs to a different CAS")
        with self._authority() as authority:
            if authority.cas_id != snapshot.cas_id:
                raise ArtifactValidationError("source CAS authority changed")
            if snapshot.descriptor.size > MAX_IMAGE_JSON_BYTES:
                raise ArtifactValidationError("source-CAS byte read exceeds the bounded metadata limit")
            payload = self._read_target(authority, snapshot.descriptor, capture=True)
            if payload is None:
                raise ArtifactValidationError(f"source-CAS blob is missing or corrupt: {snapshot.descriptor.digest}")
            return payload

    @contextmanager
    def lease_layer(
        self,
        image: SnapshottedOCIImage,
        ordinal: int,
    ) -> Iterator[LeasedSourceLayer]:
        """Lease one exact image-layer occurrence from its pinned source blob.

        A clean context exit is successful only after the consumer has drained
        ``chunks()`` to EOF.  Body exceptions abort the lease without masking
        the original exception.
        """
        if not isinstance(image, SnapshottedOCIImage) or image.cas_id != self._cas_id:
            raise SourceLeaseError("snapshotted image belongs to a different source CAS")
        if type(ordinal) is not int or not 0 <= ordinal < len(image.layers):
            raise SourceLeaseError("source layer ordinal is out of range")
        occurrence = image.image.layers[ordinal]
        snapshot = image.layers[ordinal]
        if occurrence.ordinal != ordinal or occurrence.compressed != snapshot.descriptor:
            raise SourceLeaseError("source layer occurrence binding is inconsistent")

        file_fd: int | None = None
        lease: LeasedSourceLayer | None = None
        with self._authority() as authority:
            if authority.cas_id != snapshot.cas_id:
                raise SourceLeaseError("source CAS authority changed")
            entry_name = snapshot.descriptor.digest.split(":", 1)[1]
            try:
                entry = os.stat(entry_name, dir_fd=authority.blobs_fd, follow_symlinks=False)
                file_fd = os.open(entry_name, _READ_FLAGS, dir_fd=authority.blobs_fd)
                opened = os.fstat(file_fd)
            except OSError:
                _close_noerror(file_fd)
                file_fd = None
                raise SourceLeaseError("source layer lease target is missing or inaccessible") from None
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != _PRIVATE_BLOB_MODE
                or opened.st_size != snapshot.descriptor.size
                or _identity(entry) != _identity(opened)
            ):
                _close_noerror(file_fd)
                file_fd = None
                raise SourceLeaseError("source layer lease target is unsafe")
            lease = LeasedSourceLayer(
                authority=authority,
                descriptor=snapshot.descriptor,
                diff_id=occurrence.diff_id,
                ordinal=ordinal,
                entry_name=entry_name,
                file_fd=file_fd,
                initial_metadata=opened,
            )
            file_fd = None
            try:
                yield lease
            except BaseException:
                lease._abort()
                raise
            else:
                if not lease._verified:
                    raise SourceLeaseError("source layer lease was not consumed to verified EOF")
            finally:
                lease._abort()
                _close_noerror(file_fd)

    def verify_image(self, snapshot: SnapshottedOCIImage) -> None:
        if not isinstance(snapshot, SnapshottedOCIImage) or snapshot.cas_id != self._cas_id:
            raise ArtifactValidationError("snapshotted image belongs to a different CAS")
        with self._authority() as authority:
            seen: set[tuple[str, int]] = set()
            for member in (snapshot.root, snapshot.manifest, snapshot.config, *snapshot.layers):
                identity = (member.descriptor.digest, member.descriptor.size)
                if identity in seen:
                    continue
                if self._read_target(authority, member.descriptor, capture=False) is None:
                    raise ArtifactValidationError(f"source-CAS blob is missing or corrupt: {member.descriptor.digest}")
                seen.add(identity)


@runtime_checkable
class RegistrySource(Protocol):
    def snapshot(self, reference: OCIImageRef, cas: SourceCAS) -> SnapshottedOCIImage: ...


@dataclass(frozen=True, slots=True)
class LocalLayoutSource:
    """One digest-pinned standard OCI image-layout source."""

    layout: Path = field(repr=False)
    root_digest: str
    _checkpoint: Checkpoint = field(default=_noop_checkpoint, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.layout, Path) or not self.layout.is_absolute():
            raise ArtifactValidationError("local OCI layout path must be absolute")
        if any(component in {"", ".", ".."} for component in self.layout.parts[1:]):
            raise ArtifactValidationError("local OCI layout path must not contain relative components")
        Descriptor(media_type="application/octet-stream", digest=self.root_digest, size=0)
        if not callable(self._checkpoint):
            raise ArtifactValidationError("local OCI layout checkpoint must be callable")

    @classmethod
    def parse(cls, value: str, *, checkpoint: Checkpoint = _noop_checkpoint) -> LocalLayoutSource:
        prefix = "oci-layout://"
        if not isinstance(value, str) or not value.startswith(prefix):
            raise ArtifactValidationError("local OCI source must use oci-layout:///absolute/path@sha256:<digest>")
        remainder = value[len(prefix) :]
        if any(marker in remainder for marker in ("?", "#", "\x00")) or "@" not in remainder:
            raise ArtifactValidationError("local OCI source URI contains unsupported query, fragment, or pin syntax")
        raw_path, digest = remainder.rsplit("@", 1)
        path = Path(raw_path)
        if not raw_path.startswith("/") or not path.is_absolute():
            raise ArtifactValidationError("local OCI source URI path must be absolute")
        return cls(layout=path, root_digest=digest, _checkpoint=checkpoint)

    def snapshot(self, reference: OCIImageRef, cas: SourceCAS) -> SnapshottedOCIImage:
        if not isinstance(reference, OCIImageRef) or not isinstance(cas, SourceCAS):
            raise ArtifactValidationError("local OCI snapshot requires an OCIImageRef and SourceCAS")
        with _open_absolute_directory(self.layout, self._checkpoint) as layout_fd:
            marker = strict_json_object(
                _read_stable_regular_file(layout_fd, "oci-layout", max_bytes=4096),
                "OCI layout marker",
            )
            if marker != {"imageLayoutVersion": "1.0.0"}:
                raise ArtifactValidationError("unsupported OCI image layout version")
            top_index = strict_json_object(
                _read_stable_regular_file(layout_fd, "index.json", max_bytes=MAX_IMAGE_JSON_BYTES),
                "OCI layout index.json",
            )
            if type(top_index.get("schemaVersion")) is not int or top_index["schemaVersion"] != 2:
                raise ArtifactValidationError("OCI layout index.json must use schemaVersion 2")
            manifests = top_index.get("manifests")
            if not isinstance(manifests, list):
                raise ArtifactValidationError("OCI layout index.json manifests must be an array")
            top_media_type = top_index.get("mediaType")
            if top_media_type is not None and top_media_type != OCI_IMAGE_INDEX_MEDIA_TYPE:
                raise ArtifactValidationError("OCI layout index.json has an unsupported mediaType")
            roots: list[Descriptor] = []
            for index, raw_descriptor in enumerate(manifests):
                if not isinstance(raw_descriptor, dict):
                    raise ArtifactValidationError(f"OCI layout index.json manifests[{index}] is malformed")
                try:
                    parsed_descriptor = Descriptor(
                        media_type=raw_descriptor["mediaType"],
                        digest=raw_descriptor["digest"],
                        size=raw_descriptor["size"],
                    )
                except KeyError as exc:
                    raise ArtifactValidationError(f"OCI layout index.json manifests[{index}] is malformed") from exc
                if parsed_descriptor.digest == self.root_digest:
                    if "data" in raw_descriptor:
                        raise ArtifactValidationError("pinned OCI layout root uses unsupported embedded data")
                    roots.append(parsed_descriptor)
            if len(roots) != 1:
                raise ArtifactValidationError("OCI layout must declare the pinned root descriptor exactly once")
            root_descriptor = roots[0]
            if root_descriptor.media_type not in (
                SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES | SUPPORTED_IMAGE_INDEX_MEDIA_TYPES
            ):
                raise ArtifactValidationError("pinned OCI layout root has an unsupported media type")
            if root_descriptor.size > MAX_IMAGE_JSON_BYTES:
                raise ArtifactValidationError("pinned OCI layout root exceeds the JSON size limit")

            blobs_fd = _open_child_directory(layout_fd, "blobs", self._checkpoint)
            sha256_fd: int | None = None
            try:
                sha256_fd = _open_child_directory(blobs_fd, "sha256", self._checkpoint)
                memo: dict[str, SourceSnapshot] = {}

                def ensure(descriptor: Descriptor) -> SourceSnapshot:
                    existing = memo.get(descriptor.digest)
                    if existing is not None:
                        if existing.descriptor != descriptor:
                            raise ArtifactValidationError("same OCI digest has contradictory descriptor metadata")
                        return existing
                    created = cas.import_source_blob(sha256_fd, descriptor, self._checkpoint)
                    memo[descriptor.digest] = created
                    return created

                root_snapshot = ensure(root_descriptor)

                def reader(descriptor: Descriptor) -> bytes:
                    member = ensure(descriptor)
                    _notify(self._checkpoint, SnapshotStage.BEFORE_CAS_REOPEN, digest=descriptor.digest)
                    return cas.read_metadata(member)

                image = resolve_image(reference, root_descriptor, reader)
                manifest_snapshot = ensure(image.manifest_descriptor)
                config_snapshot = ensure(image.config.descriptor)
                layer_snapshots = tuple(ensure(occurrence.compressed) for occurrence in image.layers)
                result = SnapshottedOCIImage(
                    image=image,
                    root=root_snapshot,
                    manifest=manifest_snapshot,
                    config=config_snapshot,
                    layers=layer_snapshots,
                )
                _notify(self._checkpoint, SnapshotStage.BEFORE_RESULT, digest=root_descriptor.digest)
                cas.verify_image(result)
                return result
            finally:
                _close_noerror(sha256_fd)
                _close_noerror(blobs_fd)
