"""Pinned, untrusted QEMU I/O beneath an otherwise trusted OCI run directory.

Creation is exclusive. The caller commits ``receipt.to_dict()`` in the trusted
``oci_runtime_io`` run-state field alongside the domain plan before reuse. This
module never grants QEMU access, reads console content, or deletes endpoints.
"""

from __future__ import annotations

import os
import re
import stat
import threading
import uuid
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import StateError
from .state import ExistingRunMutation

OCI_RUNTIME_DIRECTORY = "io"
OCI_RUNTIME_LIFECYCLE_FILENAME = "lifecycle.sock"
OCI_RUNTIME_CONSOLE_FILENAME = "console.log"
OCI_RUNTIME_IO_STATE_KEY = "oci_runtime_io"
_SCHEMA = "palimpsest.oci-runtime-io.v1"
_FORK_LOCK = threading.Lock()
_OWNED_DESCRIPTORS: weakref.WeakSet[_RuntimeIODescriptors] = weakref.WeakSet()


class _RuntimeIODescriptors:
    """Invalidate inherited capabilities before child code can reuse their FDs."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.directory = self.console = -1
        self._identities: dict[int, tuple[int, int, int]] = {}
        with _FORK_LOCK:
            _OWNED_DESCRIPTORS.add(self)

    def open(self, role: str, name: str, flags: int, *, dir_fd: int, mode: int = 0o600) -> int:
        with _FORK_LOCK:
            if self.pid != os.getpid():
                raise _invalid()
            fd = os.open(name, flags, mode, dir_fd=dir_fd)
            try:
                info = os.fstat(fd)
                self._identities[fd] = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
                setattr(self, role, fd)
            except BaseException:
                os.close(fd)
                raise
            return fd

    def _close_descriptors(self) -> None:
        descriptors = (self.console, self.directory)
        self.console = self.directory = -1
        for fd in descriptors:
            expected = self._identities.pop(fd, None)
            if fd >= 0 and expected is not None:
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) == expected:
                        os.close(fd)
                except OSError:
                    pass

    def close(self) -> None:
        with _FORK_LOCK:
            self._close_descriptors()
            _OWNED_DESCRIPTORS.discard(self)


def _close_inherited_descriptors() -> None:
    try:
        for resource in tuple(_OWNED_DESCRIPTORS):
            resource._close_descriptors()
        _OWNED_DESCRIPTORS.clear()
    finally:
        _FORK_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_LOCK.acquire,
        after_in_parent=_FORK_LOCK.release,
        after_in_child=_close_inherited_descriptors,
    )


def _invalid() -> StateError:
    return StateError("OCI runtime I/O authority is invalid or changed")


@dataclass(frozen=True, slots=True)
class RuntimeIOPaths:
    root: Path
    lifecycle_socket: Path
    console_log: Path


def runtime_io_paths(run_root: Path) -> RuntimeIOPaths:
    root = run_root / OCI_RUNTIME_DIRECTORY
    return RuntimeIOPaths(root, root / OCI_RUNTIME_LIFECYCLE_FILENAME, root / OCI_RUNTIME_CONSOLE_FILENAME)


@dataclass(frozen=True, slots=True)
class RuntimeIOReceipt:
    schema: str
    run_id: str
    run_name: str
    plan_digest: str
    directory_device: int
    directory_inode: int
    console_device: int
    console_inode: int

    def __post_init__(self) -> None:
        try:
            valid_id = type(self.run_id) is str and str(uuid.UUID(self.run_id)) == self.run_id
        except (ValueError, AttributeError):
            valid_id = False
        if (
            self.schema != _SCHEMA
            or not valid_id
            or type(self.run_name) is not str
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.run_name) is None
            or type(self.plan_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.plan_digest) is None
            or any(
                type(value) is not int or value < 0
                for value in (self.directory_device, self.directory_inode, self.console_device, self.console_inode)
            )
            or self.directory_inode == 0
            or self.console_inode == 0
        ):
            raise _invalid()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeIOReceipt:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise _invalid()
        try:
            receipt = cls(**value)
        except (TypeError, ValueError):
            raise _invalid() from None
        if receipt.to_dict() != dict(value):
            raise _invalid()
        return receipt


def _validate_runtime_io_metadata(
    directory: os.stat_result, console: os.stat_result, receipt: RuntimeIOReceipt
) -> None:
    """Strict production metadata boundary; no implicit ACL authorization.

    Console length and timestamps may change as QEMU appends. Ownership, link
    count, type, mode, and both pinned inode identities must remain exact.
    """
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
        or (directory.st_dev, directory.st_ino) != (receipt.directory_device, receipt.directory_inode)
        or not stat.S_ISREG(console.st_mode)
        or console.st_uid != os.geteuid()
        or console.st_nlink != 1
        or stat.S_IMODE(console.st_mode) != 0o600
        or (console.st_dev, console.st_ino) != (receipt.console_device, receipt.console_inode)
    ):
        raise _invalid()


def _verify_parent(mutation: ExistingRunMutation) -> None:
    mutation.verify_binding()
    for fd in (mutation._runs_fd, mutation._run_fd):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise _invalid()


class RuntimeIOGuard:
    """A borrowed run mutation and owned I/O descriptors; context lifetime only."""

    def __init__(
        self, mutation: ExistingRunMutation, descriptors: _RuntimeIODescriptors, receipt: RuntimeIOReceipt
    ) -> None:
        self._mutation = mutation
        self._descriptors = descriptors
        self._receipt = receipt
        self._closed = False

    @property
    def paths(self) -> RuntimeIOPaths:
        return runtime_io_paths(self._mutation.paths.root)

    @property
    def receipt(self) -> RuntimeIOReceipt:
        return self._receipt

    @property
    def directory_fd(self) -> int:
        if self._closed or self._descriptors.pid != os.getpid() or self._descriptors.directory < 0:
            raise _invalid()
        return self._descriptors.directory

    def read_console(self, offset: int, limit: int = 64 * 1024) -> bytes:
        """Read bounded, untrusted VM-console bytes under this short run guard.

        Appends are expected. Replaced inodes, invalid access and truncation
        behind the consumed offset are not. No read changes the file offset.
        """
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 65536:
            raise _invalid()
        self.verify()
        try:
            descriptor = self._descriptors.console
            if os.fstat(descriptor).st_size < offset:
                raise _invalid()
            content = os.pread(descriptor, limit, offset)
            if os.fstat(descriptor).st_size < offset + len(content):
                raise _invalid()
            self.verify()
            return content
        except OSError:
            raise _invalid() from None

    def verify(self, *, require_socket_absent: bool = False) -> None:
        if self._closed or self._descriptors.pid != os.getpid() or type(require_socket_absent) is not bool:
            raise _invalid()
        try:
            _verify_parent(self._mutation)
            visible_directory = os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=self._mutation._run_fd, follow_symlinks=False)
            visible_console = os.stat(
                OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=self._descriptors.directory, follow_symlinks=False
            )
            access = self._mutation.snapshot.state.get("oci_runtime_access")
            if "oci_runtime_access" in self._mutation.snapshot.state:
                from .oci_runtime_access import verify_runtime_access

                verify_runtime_access(
                    access,
                    self._receipt,
                    self._descriptors.directory,
                    self._descriptors.console,
                    visible_directory,
                    visible_console,
                    run_directory_fd=self._mutation._run_fd,
                    runs_directory_fd=self._mutation._runs_fd,
                )
                from .oci_shared_traversal import verify_shared_traversal

                verify_shared_traversal(
                    self._mutation._roots,
                    self._mutation.snapshot.state.get("oci_shared_traversal"),
                    access=access,
                    runs_fd=self._mutation._runs_fd,
                )
            else:
                for directory, console in (
                    (os.fstat(self._descriptors.directory), os.fstat(self._descriptors.console)),
                    (visible_directory, visible_console),
                ):
                    _validate_runtime_io_metadata(directory, console, self._receipt)
            if require_socket_absent:
                try:
                    os.stat(OCI_RUNTIME_LIFECYCLE_FILENAME, dir_fd=self._descriptors.directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise StateError("OCI runtime lifecycle socket path is already reserved")
            _verify_parent(self._mutation)
        except OSError:
            raise _invalid() from None


@contextmanager
def runtime_io_guard(
    mutation: ExistingRunMutation,
    *,
    plan_digest: str,
    create: bool = False,
    require_socket_absent: bool = False,
) -> Iterator[RuntimeIOGuard]:
    """Create fresh I/O or reopen the exact durable receipt under a run lock.

    Failed creation is deliberately preserved, never adopted on retry. The
    caller must retain this context through its sensitive launch boundary.
    """
    if type(mutation) is not ExistingRunMutation or type(create) is not bool or type(require_socket_absent) is not bool:
        raise _invalid()
    descriptors = _RuntimeIODescriptors()
    guard = None
    try:
        _verify_parent(mutation)
        # Validate caller binding before mkdir or any other filesystem write.
        RuntimeIOReceipt(_SCHEMA, mutation.record.run_id, mutation.record.name, plan_digest, 0, 1, 0, 1)
        if create:
            if OCI_RUNTIME_IO_STATE_KEY in mutation.snapshot.state:
                raise _invalid()
            os.mkdir(OCI_RUNTIME_DIRECTORY, 0o700, dir_fd=mutation._run_fd)
            expected_directory = os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=mutation._run_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(expected_directory.st_mode)
                or expected_directory.st_uid != os.geteuid()
                or stat.S_IMODE(expected_directory.st_mode) != 0o700
            ):
                raise _invalid()
        else:
            receipt = RuntimeIOReceipt.from_dict(mutation.snapshot.state.get(OCI_RUNTIME_IO_STATE_KEY))
            if (receipt.run_id, receipt.run_name, receipt.plan_digest) != (
                mutation.record.run_id,
                mutation.record.name,
                plan_digest,
            ):
                raise _invalid()
            expected_directory = os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=mutation._run_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(expected_directory.st_mode)
                or expected_directory.st_uid != os.geteuid()
                or (expected_directory.st_dev, expected_directory.st_ino)
                != (receipt.directory_device, receipt.directory_inode)
            ):
                raise _invalid()
        directory_fd = descriptors.open(
            "directory",
            OCI_RUNTIME_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=mutation._run_fd,
        )
        opened = os.fstat(directory_fd)
        visible = os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=mutation._run_fd, follow_symlinks=False)
        for info in (opened, visible):
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                expected_directory.st_dev,
                expected_directory.st_ino,
            ):
                raise _invalid()
        if create:
            if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
                raise _invalid()
            _verify_parent(mutation)
        else:
            console = os.stat(OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=directory_fd, follow_symlinks=False)
            access = mutation.snapshot.state.get("oci_runtime_access")
            if "oci_runtime_access" in mutation.snapshot.state:
                from .oci_runtime_access import RuntimeAccessReceipt, _validate_target

                access = RuntimeAccessReceipt.from_dict(access)
                if access.phase != "granted" or access.runtime_io != receipt:
                    raise _invalid()
                _validate_target(opened, access.directory, access.directory.granted)
                _validate_target(visible, access.directory, access.directory.granted)
                _validate_target(console, access.console, access.console.granted)
            else:
                _validate_runtime_io_metadata(opened, console, receipt)
                _validate_runtime_io_metadata(visible, console, receipt)
            _verify_parent(mutation)
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= os.O_RDWR | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
        console_fd = descriptors.open("console", OCI_RUNTIME_CONSOLE_FILENAME, flags, dir_fd=directory_fd)
        if create:
            directory, console = os.fstat(directory_fd), os.fstat(console_fd)
            receipt = RuntimeIOReceipt(
                _SCHEMA,
                mutation.record.run_id,
                mutation.record.name,
                plan_digest,
                directory.st_dev,
                directory.st_ino,
                console.st_dev,
                console.st_ino,
            )
        guard = RuntimeIOGuard(mutation, descriptors, receipt)
        guard.verify(require_socket_absent=require_socket_absent or create)
        if create:
            os.fsync(console_fd)
            os.fsync(directory_fd)
            os.fsync(mutation._run_fd)
            guard.verify(require_socket_absent=True)
    except OSError:
        descriptors.close()
        raise _invalid() from None
    except BaseException:
        descriptors.close()
        raise
    try:
        yield guard
        guard.verify()
    finally:
        guard._closed = True
        descriptors.close()
