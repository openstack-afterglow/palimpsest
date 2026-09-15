"""Descriptor-pinned physical artifact bytes for security-sensitive stores.

This module owns the mutable filesystem boundary for derived artifacts.  It
never returns a filesystem path or a raw descriptor; callers receive only a
bounded private reader whose clean EOF revalidates the pinned store entry.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ArtifactValidationError

_CHUNK = 1024 * 1024
_DIRECTORY_MODE = 0o700
_TEMP_MODE = 0o600
_BLOB_MODE = 0o400
_LOCK_MODE = 0o600
_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_TEMP_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
_SEALED_MODES = frozenset({_BLOB_MODE, 0o444})
_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_TEMP_RE = re.compile(r"^\.oci-artifact-tmp-([0-9a-f]{64})-[0-9a-f]{32}$")


class ArtifactStoreError(ArtifactValidationError):
    """Stable path-free error from the physical artifact boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    digest: str
    size: int
    store_id: str


@dataclass(slots=True)
class _Authority:
    root_fd: int
    blobs_parent_fd: int
    blobs_fd: int
    locks_fd: int
    signature: tuple[tuple[int, int, int, int], ...]
    store_id: str


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _stable(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode)


def _close_noerror(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class _ForkCloseFD:
    __slots__ = ("fd",)

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close_in_child(self) -> None:
        fd, self.fd = self.fd, -1
        _close_noerror(fd)


_FORK_CLOSE_LOCK_FDS: set[_ForkCloseFD] = set()
_FORK_REGISTRY_LOCK = threading.Lock()


def _close_inherited_digest_locks() -> None:
    try:
        inherited = tuple(_FORK_CLOSE_LOCK_FDS)
        _FORK_CLOSE_LOCK_FDS.clear()
        for resource in inherited:
            resource.close_in_child()
    finally:
        _FORK_REGISTRY_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_REGISTRY_LOCK.acquire,
        after_in_parent=_FORK_REGISTRY_LOCK.release,
        after_in_child=_close_inherited_digest_locks,
    )


def _digest_hex(digest: str) -> str:
    match = _DIGEST_RE.fullmatch(digest or "")
    if match is None:
        raise ArtifactStoreError("artifact-digest", "artifact digest must be canonical SHA-256")
    return match.group(1)


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(fd, payload[offset:])
        except OSError:
            raise ArtifactStoreError("artifact-write", "artifact temporary write failed") from None
        if written <= 0:
            raise ArtifactStoreError("artifact-write", "artifact temporary write was incomplete")
        offset += written


@contextmanager
def _open_absolute_directory(path: Path) -> Iterator[int]:
    if not path.is_absolute() or "\0" in os.fspath(path):
        raise ArtifactStoreError("artifact-root", "artifact store root must be absolute")
    components = path.parts[1:]
    if any(component in {"", ".", ".."} for component in components):
        raise ArtifactStoreError("artifact-root", "artifact store root contains relative components")
    opened: list[int] = []
    try:
        try:
            current = os.open("/", _DIR_FLAGS)
            opened.append(current)
            for component in components:
                current = os.open(component, _DIR_FLAGS, dir_fd=current)
                opened.append(current)
                if not stat.S_ISDIR(os.fstat(current).st_mode):
                    raise OSError
        except OSError:
            raise ArtifactStoreError("artifact-root", "artifact store root cannot be securely opened") from None
        yield current
    finally:
        for fd in reversed(opened):
            _close_noerror(fd)


def _ensure_private_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute == Path("/") or not absolute.name:
        raise ArtifactStoreError("artifact-root", "artifact store root must be a private child directory")
    parent = absolute.parent
    with _open_absolute_directory(parent) as parent_fd:
        created = False
        root_fd: int | None = None
        try:
            try:
                os.mkdir(absolute.name, _DIRECTORY_MODE, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            root_fd = os.open(absolute.name, _DIR_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(root_fd)
            entry = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or _identity(opened) != _identity(entry)
            ):
                raise ArtifactStoreError("artifact-root", "artifact store root is not owner-bound")
            if stat.S_IMODE(opened.st_mode) != _DIRECTORY_MODE:
                os.fchmod(root_fd, _DIRECTORY_MODE)
                os.fsync(root_fd)
            if created:
                os.fsync(parent_fd)
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError("artifact-root", "artifact store root cannot be created durably") from None
        finally:
            _close_noerror(root_fd)
    return absolute


def _ensure_private_child(parent_fd: int, name: str) -> int:
    child_fd: int | None = None
    created = False
    try:
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        child_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid() or _identity(opened) != _identity(entry):
            raise ArtifactStoreError("artifact-root", "artifact store component is not owner-bound")
        if stat.S_IMODE(opened.st_mode) != _DIRECTORY_MODE:
            os.fchmod(child_fd, _DIRECTORY_MODE)
            os.fsync(child_fd)
        if created:
            os.fsync(parent_fd)
        result = child_fd
        child_fd = None
        return result
    except ArtifactStoreError:
        raise
    except OSError:
        raise ArtifactStoreError("artifact-root", "artifact store component cannot be created durably") from None
    finally:
        _close_noerror(child_fd)


class _ArtifactReader:
    """Internal same-FD reader; deliberately has no fileno or path API."""

    __slots__ = (
        "_authority",
        "_closed",
        "_digest",
        "_fd",
        "_hasher",
        "_initial",
        "_name",
        "_owner_pid",
        "_owner_thread",
        "_size",
        "_total",
        "_verified",
    )

    def __init__(
        self,
        authority: _Authority,
        fd: int,
        name: str,
        digest: str,
        size: int,
        initial: os.stat_result,
    ) -> None:
        self._authority = authority
        self._fd = fd
        self._name = name
        self._digest = digest
        self._size = size
        self._initial = initial
        self._hasher = hashlib.sha256()
        self._total = 0
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._verified = False
        self._closed = False

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            self._abort()
            raise ArtifactStoreError("artifact-owner", "artifact reader cannot cross a process")
        if threading.get_ident() != self._owner_thread:
            raise ArtifactStoreError("artifact-owner", "artifact reader cannot cross a thread")
        if self._closed:
            raise ArtifactStoreError("artifact-read", "artifact reader is closed")

    def read(self, size: int) -> bytes:
        self._check_owner()
        if type(size) is not int or not 0 <= size <= _CHUNK:
            raise ArtifactStoreError("artifact-read", "artifact reader request is invalid")
        if size == 0:
            return b""
        try:
            payload = os.read(self._fd, min(size, self._size - self._total))
        except OSError:
            raise ArtifactStoreError("artifact-read", "artifact bytes cannot be read") from None
        self._hasher.update(payload)
        self._total += len(payload)
        return payload

    def finish(self) -> None:
        self._check_owner()
        if self._verified:
            return
        if self._total != self._size:
            raise ArtifactStoreError("artifact-incomplete", "artifact reader did not reach verified EOF")
        try:
            after = os.fstat(self._fd)
            entry = os.stat(self._name, dir_fd=self._authority.blobs_fd, follow_symlinks=False)
        except OSError:
            raise ArtifactStoreError("artifact-changed", "artifact binding changed during read") from None
        if (
            self._total != self._size
            or f"sha256:{self._hasher.hexdigest()}" != self._digest
            or _stable(after) != _stable(self._initial)
            or _stable(entry) != _stable(self._initial)
        ):
            raise ArtifactStoreError("artifact-changed", "artifact failed same-FD EOF verification")
        self._verified = True

    def close(self) -> ArtifactStoreError | None:
        self._check_owner()
        if not self._closed:
            self._closed = True
            try:
                os.close(self._fd)
            except OSError:
                self._fd = -1
                return ArtifactStoreError("artifact-cleanup", "artifact reader close failed")
            self._fd = -1
        return None

    def _abort(self) -> None:
        if not self._closed:
            self._closed = True
            _close_noerror(self._fd)
            self._fd = -1

    def __copy__(self) -> _ArtifactReader:
        raise ArtifactStoreError("artifact-copy", "artifact reader cannot be copied")

    def __deepcopy__(self, _memo: object) -> _ArtifactReader:
        raise ArtifactStoreError("artifact-copy", "artifact reader cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("artifact reader cannot be serialized")


class ArtifactStore:
    """Owner-bound physical bytes shared by legacy and OCI-derived indexes."""

    def __init__(
        self,
        root: Path,
        *,
        repair_min_age_seconds: float = 300.0,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if (
            not isinstance(repair_min_age_seconds, (int, float))
            or isinstance(repair_min_age_seconds, bool)
            or not 0 <= float(repair_min_age_seconds) < float("inf")
            or not callable(wall_clock_ns)
        ):
            raise ArtifactStoreError("artifact-policy", "artifact repair policy is invalid")
        self._root = _ensure_private_root(root)
        self._repair_age_ns = int(float(repair_min_age_seconds) * 1_000_000_000)
        self._wall_clock_ns = wall_clock_ns
        with self._authority() as authority:
            self._signature = authority.signature
            self._store_id = authority.store_id

    @property
    def identity(self) -> str:
        return self._store_id

    @staticmethod
    def _signature_id(signature: tuple[tuple[int, int, int, int], ...]) -> str:
        payload = "|".join(":".join(str(value) for value in item) for item in signature).encode()
        return f"artifact-store-v1:{hashlib.sha256(payload).hexdigest()}"

    def _verify_binding(self, authority: _Authority) -> None:
        try:
            with _open_absolute_directory(self._root) as visible_root_fd:
                opened = tuple(
                    os.fstat(fd)
                    for fd in (authority.root_fd, authority.blobs_parent_fd, authority.blobs_fd, authority.locks_fd)
                )
                entries = (
                    os.fstat(visible_root_fd),
                    os.stat("blobs", dir_fd=authority.root_fd, follow_symlinks=False),
                    os.stat("sha256", dir_fd=authority.blobs_parent_fd, follow_symlinks=False),
                    os.stat("oci-artifact-locks-v1", dir_fd=authority.root_fd, follow_symlinks=False),
                )
        except (OSError, ArtifactStoreError):
            raise ArtifactStoreError("artifact-authority", "artifact store authority changed") from None
        open_signature = tuple(_directory_signature(item) for item in opened)
        entry_signature = tuple(_directory_signature(item) for item in entries)
        expected_signature = getattr(self, "_signature", authority.signature)
        expected_id = getattr(self, "_store_id", authority.store_id)
        if (
            open_signature != authority.signature
            or entry_signature != authority.signature
            or authority.signature != expected_signature
            or authority.store_id != expected_id
        ):
            raise ArtifactStoreError("artifact-authority", "artifact store authority changed")

    @contextmanager
    def _authority(self) -> Iterator[_Authority]:
        blobs_parent_fd: int | None = None
        blobs_fd: int | None = None
        locks_fd: int | None = None
        with _open_absolute_directory(self._root) as root_fd:
            try:
                root = os.fstat(root_fd)
                if (
                    not stat.S_ISDIR(root.st_mode)
                    or root.st_uid != os.geteuid()
                    or stat.S_IMODE(root.st_mode) != _DIRECTORY_MODE
                ):
                    raise ArtifactStoreError("artifact-authority", "artifact store root changed")
                blobs_parent_fd = _ensure_private_child(root_fd, "blobs")
                blobs_fd = _ensure_private_child(blobs_parent_fd, "sha256")
                locks_fd = _ensure_private_child(root_fd, "oci-artifact-locks-v1")
                signature = tuple(
                    _directory_signature(os.fstat(fd)) for fd in (root_fd, blobs_parent_fd, blobs_fd, locks_fd)
                )
                authority = _Authority(
                    root_fd=root_fd,
                    blobs_parent_fd=blobs_parent_fd,
                    blobs_fd=blobs_fd,
                    locks_fd=locks_fd,
                    signature=signature,
                    store_id=self._signature_id(signature),
                )
                self._verify_binding(authority)
                yield authority
                self._verify_binding(authority)
            finally:
                _close_noerror(locks_fd)
                _close_noerror(blobs_fd)
                _close_noerror(blobs_parent_fd)

    @contextmanager
    def _digest_lock(self, authority: _Authority, digest_hex: str) -> Iterator[None]:
        name = f"blob-{digest_hex}.lock"
        lock_fd: int | None = None
        fork_resource: _ForkCloseFD | None = None
        try:
            prior: os.stat_result | None
            created = False
            registry_locked = False
            try:
                _FORK_REGISTRY_LOCK.acquire()
                registry_locked = True
                try:
                    lock_fd = os.open(name, _LOCK_FLAGS | os.O_EXCL, _LOCK_MODE, dir_fd=authority.locks_fd)
                    created = True
                    prior = None
                except FileExistsError:
                    prior = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                    lock_fd = os.open(name, _LOCK_FLAGS, dir_fd=authority.locks_fd)
                fork_resource = _ForkCloseFD(lock_fd)
                _FORK_CLOSE_LOCK_FDS.add(fork_resource)
                opened = os.fstat(lock_fd)
            except OSError:
                raise ArtifactStoreError("artifact-lock", "artifact digest lock cannot be opened") from None
            finally:
                if registry_locked:
                    _FORK_REGISTRY_LOCK.release()
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (prior is not None and _identity(prior) != _identity(opened))
            ):
                raise ArtifactStoreError("artifact-lock", "artifact digest lock is unsafe")
            try:
                os.fchmod(lock_fd, _LOCK_MODE)
                os.fsync(lock_fd)
                if created:
                    os.fsync(authority.locks_fd)
                before = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                if _identity(before) != _identity(opened) or stat.S_IMODE(before.st_mode) != _LOCK_MODE:
                    raise ArtifactStoreError("artifact-lock", "artifact digest lock binding changed")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                after = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                if _identity(after) != _identity(opened):
                    raise ArtifactStoreError("artifact-lock", "artifact digest lock split during acquire")
                self._verify_binding(authority)
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError("artifact-lock", "artifact digest lock cannot be acquired") from None
            yield
            self._verify_binding(authority)
        finally:
            if fork_resource is not None:
                _FORK_REGISTRY_LOCK.acquire()
                try:
                    lock_fd, fork_resource.fd = fork_resource.fd, -1
                    if lock_fd >= 0:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                        _close_noerror(lock_fd)
                    _FORK_CLOSE_LOCK_FDS.discard(fork_resource)
                finally:
                    _FORK_REGISTRY_LOCK.release()
            elif lock_fd is not None and lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                _close_noerror(lock_fd)

    @staticmethod
    def _verify_structure(fd: int, size: int, maximum: int) -> None:
        from .oci_packer import verify_squashfs_fd

        try:
            verify_squashfs_fd(fd, size, maximum)
        except ArtifactValidationError:
            raise ArtifactStoreError("artifact-structure", "artifact SquashFS structure is invalid") from None

    def _open_verified_fd(
        self,
        authority: _Authority,
        digest: str,
        size: int,
        maximum: int,
    ) -> tuple[int, os.stat_result]:
        name = _digest_hex(digest)
        fd: int | None = None
        try:
            entry = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
            fd = os.open(name, _READ_FLAGS, dir_fd=authority.blobs_fd)
            opened = os.fstat(fd)
            mode = stat.S_IMODE(opened.st_mode)
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or mode not in {_BLOB_MODE, 0o444}
                or opened.st_size != size
                or _identity(entry) != _identity(opened)
            ):
                raise ArtifactStoreError("artifact-corrupt", "artifact target metadata is invalid")
            hasher = hashlib.sha256()
            offset = 0
            while offset < size:
                payload = os.pread(fd, min(_CHUNK, size - offset), offset)
                if not payload:
                    raise ArtifactStoreError("artifact-corrupt", "artifact target ended before its recorded size")
                hasher.update(payload)
                offset += len(payload)
            if f"sha256:{hasher.hexdigest()}" != digest:
                raise ArtifactStoreError("artifact-corrupt", "artifact target digest is invalid")
            self._verify_structure(fd, size, maximum)
            after = os.fstat(fd)
            final_entry = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
            if _stable(after) != _stable(opened) or _stable(final_entry) != _stable(opened):
                raise ArtifactStoreError("artifact-corrupt", "artifact target changed during verification")
            os.lseek(fd, 0, os.SEEK_SET)
            result = fd, opened
            fd = None
            return result
        except FileNotFoundError:
            raise ArtifactStoreError("artifact-missing", "artifact target is missing") from None
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError("artifact-corrupt", "artifact target cannot be fully verified") from None
        finally:
            _close_noerror(fd)

    def _open_verified_blob_fd(
        self,
        authority: _Authority,
        digest: str,
    ) -> tuple[int, os.stat_result]:
        """Open and hash one generic CAS blob through its pinned directory entry."""
        name = _digest_hex(digest)
        fd: int | None = None
        try:
            entry = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
            fd = os.open(name, _READ_FLAGS, dir_fd=authority.blobs_fd)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) not in _SEALED_MODES
                or _identity(entry) != _identity(opened)
            ):
                raise ArtifactStoreError("artifact-corrupt", "artifact target metadata is invalid")
            hasher = hashlib.sha256()
            offset = 0
            while offset < opened.st_size:
                payload = os.pread(fd, min(_CHUNK, opened.st_size - offset), offset)
                if not payload:
                    raise ArtifactStoreError("artifact-corrupt", "artifact target ended during verification")
                hasher.update(payload)
                offset += len(payload)
            if f"sha256:{hasher.hexdigest()}" != digest:
                raise ArtifactStoreError("artifact-corrupt", "artifact target digest is invalid")
            after = os.fstat(fd)
            final_entry = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
            if _stable(after) != _stable(opened) or _stable(final_entry) != _stable(opened):
                raise ArtifactStoreError("artifact-corrupt", "artifact target changed during verification")
            os.lseek(fd, 0, os.SEEK_SET)
            result = fd, opened
            fd = None
            return result
        except FileNotFoundError:
            raise ArtifactStoreError("artifact-missing", "artifact target is missing") from None
        except ArtifactStoreError:
            raise
        except OSError:
            raise ArtifactStoreError("artifact-corrupt", "artifact target cannot be fully verified") from None
        finally:
            _close_noerror(fd)

    def verify_squashfs(self, digest: str, size: int, *, maximum: int) -> StoredArtifact:
        if type(size) is not int or type(maximum) is not int or not 0 < size <= maximum:
            raise ArtifactStoreError("artifact-size", "artifact size bound is invalid")
        with self._authority() as authority:
            fd, _opened = self._open_verified_fd(authority, digest, size, maximum)
            _close_noerror(fd)
            return StoredArtifact(digest=digest, size=size, store_id=authority.store_id)

    def verify_blob(self, digest: str) -> StoredArtifact:
        """Verify one generic CAS descriptor without exposing its path or FD."""
        with self._authority() as authority:
            fd, opened = self._open_verified_blob_fd(authority, digest)
            _close_noerror(fd)
            return StoredArtifact(digest=digest, size=opened.st_size, store_id=authority.store_id)

    def publish_blob(
        self,
        chunks: Iterable[bytes],
        *,
        expected_digest: str,
        sealed_mode: int = 0o444,
    ) -> StoredArtifact:
        """Publish generic CAS bytes without replacing an already-valid digest inode."""
        digest_hex = _digest_hex(expected_digest)
        if sealed_mode not in _SEALED_MODES:
            raise ArtifactStoreError("artifact-mode", "artifact sealed mode is invalid")
        temporary_name: str | None = None
        temporary_fd: int | None = None
        with self._authority() as authority, self._digest_lock(authority, digest_hex):
            try:
                temporary_name = f".oci-artifact-tmp-{digest_hex}-{uuid.uuid4().hex}"
                temporary_fd = os.open(temporary_name, _TEMP_FLAGS, _TEMP_MODE, dir_fd=authority.blobs_fd)
                initial = os.fstat(temporary_fd)
                blobs = os.fstat(authority.blobs_fd)
                if (
                    not stat.S_ISREG(initial.st_mode)
                    or initial.st_uid != os.geteuid()
                    or initial.st_nlink != 1
                    or initial.st_dev != blobs.st_dev
                    or stat.S_IMODE(initial.st_mode) != _TEMP_MODE
                ):
                    raise ArtifactStoreError("artifact-temp", "artifact temporary is unsafe")
                hasher = hashlib.sha256()
                total = 0
                for payload in chunks:
                    if not isinstance(payload, bytes):
                        raise ArtifactStoreError("artifact-write", "artifact producer yielded non-bytes")
                    total += len(payload)
                    hasher.update(payload)
                    _write_all(temporary_fd, payload)
                if f"sha256:{hasher.hexdigest()}" != expected_digest:
                    raise ArtifactStoreError("artifact-digest", "artifact producer did not match its descriptor")
                os.fchmod(temporary_fd, sealed_mode)
                os.fsync(temporary_fd)
                sealed = os.fstat(temporary_fd)
                if (
                    _identity(initial) != _identity(sealed)
                    or sealed.st_size != total
                    or sealed.st_nlink != 1
                    or stat.S_IMODE(sealed.st_mode) != sealed_mode
                ):
                    raise ArtifactStoreError("artifact-temp", "artifact temporary changed while sealing")
                try:
                    existing_fd, existing = self._open_verified_blob_fd(authority, expected_digest)
                except ArtifactStoreError as exc:
                    if exc.code not in {"artifact-missing", "artifact-corrupt"}:
                        raise
                else:
                    _close_noerror(existing_fd)
                    return StoredArtifact(expected_digest, existing.st_size, authority.store_id)
                os.replace(
                    temporary_name,
                    digest_hex,
                    src_dir_fd=authority.blobs_fd,
                    dst_dir_fd=authority.blobs_fd,
                )
                temporary_name = None
                os.fsync(authority.blobs_fd)
                published = os.stat(digest_hex, dir_fd=authority.blobs_fd, follow_symlinks=False)
                if _identity(published) != _identity(sealed):
                    raise ArtifactStoreError("artifact-publish", "artifact target changed during publication")
                verified_fd, verified = self._open_verified_blob_fd(authority, expected_digest)
                _close_noerror(verified_fd)
                return StoredArtifact(expected_digest, verified.st_size, authority.store_id)
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError("artifact-publish", "artifact publication failed") from None
            finally:
                cleanup_errors: list[BaseException] = []
                if temporary_fd is not None:
                    try:
                        os.close(temporary_fd)
                    except OSError:
                        cleanup_errors.append(ArtifactStoreError("artifact-cleanup", "artifact temporary close failed"))
                if temporary_name is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=authority.blobs_fd)
                        os.fsync(authority.blobs_fd)
                    except OSError:
                        cleanup_errors.append(
                            ArtifactStoreError("artifact-cleanup", "artifact temporary cleanup failed")
                        )
                if cleanup_errors:
                    primary = sys.exception()
                    failures = ([primary] if primary is not None else []) + cleanup_errors
                    if len(failures) == 1:
                        raise failures[0]
                    raise BaseExceptionGroup("artifact publication cleanup failed", failures) from None

    def delete_blob(
        self,
        digest: str,
        *,
        retention_guard: Callable[[], None],
        finalize: Callable[[], None] | None = None,
    ) -> int:
        """Delete one pinned blob and finalize its indexes under the digest lock.

        Callbacks must not re-enter this store for the same digest.  They run
        while the digest lock is held so retention and index cleanup are one
        mutation with respect to cooperating publishers.
        """
        digest_hex = _digest_hex(digest)
        if not callable(retention_guard) or (finalize is not None and not callable(finalize)):
            raise ArtifactStoreError("artifact-retention", "artifact deletion requires a retention guard")
        with self._authority() as authority, self._digest_lock(authority, digest_hex):
            retention_guard()
            try:
                fd, opened = self._open_verified_blob_fd(authority, digest)
            except ArtifactStoreError as exc:
                if exc.code == "artifact-missing":
                    if finalize is not None:
                        finalize()
                    return 0
                raise
            try:
                current = os.stat(digest_hex, dir_fd=authority.blobs_fd, follow_symlinks=False)
                if _stable(current) != _stable(opened):
                    raise ArtifactStoreError("artifact-changed", "artifact target changed before deletion")
                os.unlink(digest_hex, dir_fd=authority.blobs_fd)
                os.fsync(authority.blobs_fd)
                try:
                    os.stat(digest_hex, dir_fd=authority.blobs_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ArtifactStoreError("artifact-delete", "artifact target remained after deletion")
                if finalize is not None:
                    finalize()
                return opened.st_size
            except ArtifactStoreError:
                raise
            except OSError:
                raise ArtifactStoreError("artifact-delete", "artifact deletion failed") from None
            finally:
                _close_noerror(fd)

    @contextmanager
    def digest_guard(self, digest: str) -> Iterator[None]:
        """Serialize one physical digest mutation or durable-lease acquisition."""
        digest_hex = _digest_hex(digest)
        with self._authority() as authority, self._digest_lock(authority, digest_hex):
            yield

    @contextmanager
    def open_squashfs(self, digest: str, size: int, *, maximum: int) -> Iterator[_ArtifactReader]:
        if type(size) is not int or type(maximum) is not int or not 0 < size <= maximum:
            raise ArtifactStoreError("artifact-size", "artifact size bound is invalid")
        reader: _ArtifactReader | None = None
        with self._authority() as authority:
            fd, opened = self._open_verified_fd(authority, digest, size, maximum)
            reader = _ArtifactReader(authority, fd, _digest_hex(digest), digest, size, opened)
            try:
                yield reader
            except BaseException:
                reader._abort()
                raise
            else:
                try:
                    reader.finish()
                except BaseException:
                    reader._abort()
                    raise
                close_error = reader.close()
                if close_error is not None:
                    raise close_error

    def _repair_allowed(self, metadata: os.stat_result) -> bool:
        now = self._wall_clock_ns()
        if type(now) is not int:
            raise ArtifactStoreError("artifact-clock", "artifact repair clock returned an invalid value")
        newest = max(metadata.st_ctime_ns, metadata.st_mtime_ns)
        return now >= newest and now - newest >= self._repair_age_ns

    def _target_state(
        self,
        authority: _Authority,
        digest: str,
        size: int,
        maximum: int,
    ) -> tuple[str, os.stat_result | None]:
        name = _digest_hex(digest)
        try:
            entry = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            raise ArtifactStoreError("artifact-corrupt", "artifact target state is unavailable") from None
        try:
            fd, _opened = self._open_verified_fd(authority, digest, size, maximum)
        except ArtifactStoreError as exc:
            if exc.code == "artifact-missing":
                return "missing", None
            return "corrupt", entry
        else:
            _close_noerror(fd)
            return "valid", entry

    def publish_squashfs(
        self,
        chunks: Iterable[bytes],
        *,
        expected_digest: str,
        expected_size: int,
        maximum: int,
    ) -> StoredArtifact:
        digest_hex = _digest_hex(expected_digest)
        if type(expected_size) is not int or type(maximum) is not int or not 0 < expected_size <= maximum:
            raise ArtifactStoreError("artifact-size", "artifact publication size is invalid")
        temporary_name: str | None = None
        temporary_fd: int | None = None
        with self._authority() as authority:
            with self._digest_lock(authority, digest_hex):
                try:
                    temporary_name = f".oci-artifact-tmp-{digest_hex}-{uuid.uuid4().hex}"
                    temporary_fd = os.open(temporary_name, _TEMP_FLAGS, _TEMP_MODE, dir_fd=authority.blobs_fd)
                    initial = os.fstat(temporary_fd)
                    blobs = os.fstat(authority.blobs_fd)
                    if (
                        not stat.S_ISREG(initial.st_mode)
                        or initial.st_uid != os.geteuid()
                        or initial.st_nlink != 1
                        or initial.st_dev != blobs.st_dev
                        or stat.S_IMODE(initial.st_mode) != _TEMP_MODE
                    ):
                        raise ArtifactStoreError("artifact-temp", "artifact temporary is unsafe")
                    hasher = hashlib.sha256()
                    total = 0
                    for payload in chunks:
                        if not isinstance(payload, bytes):
                            raise ArtifactStoreError("artifact-write", "artifact producer yielded non-bytes")
                        total += len(payload)
                        if total > expected_size:
                            raise ArtifactStoreError("artifact-size", "artifact producer exceeded its recorded size")
                        hasher.update(payload)
                        _write_all(temporary_fd, payload)
                    if total != expected_size or f"sha256:{hasher.hexdigest()}" != expected_digest:
                        raise ArtifactStoreError("artifact-digest", "artifact producer did not match its receipt")
                    os.fchmod(temporary_fd, _BLOB_MODE)
                    os.fsync(temporary_fd)
                    sealed = os.fstat(temporary_fd)
                    if (
                        _identity(initial) != _identity(sealed)
                        or sealed.st_size != expected_size
                        or sealed.st_nlink != 1
                        or stat.S_IMODE(sealed.st_mode) != _BLOB_MODE
                    ):
                        raise ArtifactStoreError("artifact-temp", "artifact temporary changed while sealing")
                    self._verify_structure(temporary_fd, expected_size, maximum)
                    state, prior = self._target_state(authority, expected_digest, expected_size, maximum)
                    if state == "valid":
                        return StoredArtifact(expected_digest, expected_size, authority.store_id)
                    if state == "corrupt":
                        assert prior is not None
                        stable = os.stat(digest_hex, dir_fd=authority.blobs_fd, follow_symlinks=False)
                        if _stable(stable) != _stable(prior) or not self._repair_allowed(stable):
                            raise ArtifactStoreError(
                                "artifact-repair-deferred",
                                "fresh or changing artifact corruption cannot be replaced yet",
                            )
                    os.replace(
                        temporary_name,
                        digest_hex,
                        src_dir_fd=authority.blobs_fd,
                        dst_dir_fd=authority.blobs_fd,
                    )
                    temporary_name = None
                    os.fsync(authority.blobs_fd)
                    published = os.stat(digest_hex, dir_fd=authority.blobs_fd, follow_symlinks=False)
                    if _identity(published) != _identity(sealed):
                        raise ArtifactStoreError("artifact-publish", "artifact target changed during publication")
                    fd, _opened = self._open_verified_fd(
                        authority,
                        expected_digest,
                        expected_size,
                        maximum,
                    )
                    _close_noerror(fd)
                    return StoredArtifact(expected_digest, expected_size, authority.store_id)
                except ArtifactStoreError:
                    raise
                except OSError:
                    raise ArtifactStoreError("artifact-publish", "artifact publication failed") from None
                finally:
                    cleanup_errors: list[BaseException] = []
                    if temporary_fd is not None:
                        try:
                            os.close(temporary_fd)
                        except OSError:
                            cleanup_errors.append(
                                ArtifactStoreError("artifact-cleanup", "artifact temporary close failed")
                            )
                    if temporary_name is not None:
                        try:
                            os.unlink(temporary_name, dir_fd=authority.blobs_fd)
                            os.fsync(authority.blobs_fd)
                        except OSError:
                            cleanup_errors.append(
                                ArtifactStoreError("artifact-cleanup", "artifact temporary cleanup failed")
                            )
                    if cleanup_errors:
                        primary = sys.exception()
                        failures = ([primary] if primary is not None else []) + cleanup_errors
                        if len(failures) == 1:
                            raise failures[0]
                        raise BaseExceptionGroup("artifact publication cleanup failed", failures) from None

    def repair_stale_temporaries(self, *, minimum_age_seconds: float) -> int:
        if (
            not isinstance(minimum_age_seconds, (int, float))
            or isinstance(minimum_age_seconds, bool)
            or not 0 <= float(minimum_age_seconds) < float("inf")
        ):
            raise ArtifactStoreError("artifact-policy", "stale temporary age is invalid")
        return self._repair_stale_temporaries(minimum_age_ns=int(float(minimum_age_seconds) * 1_000_000_000))

    def _repair_stale_temporaries(self, *, minimum_age_ns: int) -> int:
        if type(minimum_age_ns) is not int or minimum_age_ns < 0:
            raise ArtifactStoreError("artifact-policy", "stale temporary age is invalid")
        minimum_age_ns = max(minimum_age_ns, self._repair_age_ns)
        removed = 0
        now = self._wall_clock_ns()
        if type(now) is not int:
            raise ArtifactStoreError("artifact-clock", "artifact repair clock returned an invalid value")
        with self._authority() as authority:
            try:
                names = os.listdir(authority.blobs_fd)
            except OSError:
                raise ArtifactStoreError("artifact-cleanup", "artifact temporaries cannot be enumerated") from None
            for name in names:
                match = _TEMP_RE.fullmatch(name)
                if match is None:
                    continue
                with self._digest_lock(authority, match.group(1)):
                    try:
                        entry = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        raise ArtifactStoreError(
                            "artifact-cleanup", "artifact temporary state is unavailable"
                        ) from None
                    newest = max(entry.st_ctime_ns, entry.st_mtime_ns)
                    if (
                        not stat.S_ISREG(entry.st_mode)
                        or entry.st_uid != os.geteuid()
                        or now < newest
                        or now - newest < minimum_age_ns
                    ):
                        continue
                    try:
                        current = os.stat(name, dir_fd=authority.blobs_fd, follow_symlinks=False)
                        if _stable(current) != _stable(entry):
                            continue
                        os.unlink(name, dir_fd=authority.blobs_fd)
                        removed += 1
                    except FileNotFoundError:
                        continue
                    except OSError:
                        raise ArtifactStoreError(
                            "artifact-cleanup", "stale artifact temporary cannot be removed"
                        ) from None
            if removed:
                try:
                    os.fsync(authority.blobs_fd)
                except OSError:
                    raise ArtifactStoreError("artifact-cleanup", "artifact temporary cleanup is not durable") from None
        return removed


__all__ = ["ArtifactStore", "ArtifactStoreError", "StoredArtifact"]
