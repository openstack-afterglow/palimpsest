"""Private fresh-exec monitor IPC and its serialized VM ownership journal.

The default child is inert. A typed inherited launch authority optionally
enables a child-owned worker, only after the parent's post-COMMITTED fence.
Transport shutdown never stops a VM or erases activation evidence.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import select
import socket
import stat as stat_module
import struct
import subprocess
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any

from .errors import PalimpsestError
from .oci_monitor import (
    _JOURNAL_NAME,
    _LOCK_NAME,
    MonitorBinding,
    MonitorProcessIdentity,
    ProcessLiveness,
    _parse_proc_start_ticks,
    current_process_identity,
    probe_process_liveness,
)
from .oci_monitor_control import MonitorStopControl
from .runtime_types import DispatchKey, ExistingRunRecord, RuntimeBackend, RuntimeKind

if TYPE_CHECKING:
    from .oci_monitor_launch import MonitorLaunchAuthority

_SOCKET_NAME = "oci-monitor-ipc-v1.sock"
_CONFIG_SCHEMA = "palimpsest.oci-monitor-exec-config.v2"
_SPAWN_SCHEMA = "palimpsest.oci-monitor-spawn.v2"
_REQUEST_SCHEMA = "palimpsest.oci-monitor-ipc-request.v1"
_RESPONSE_SCHEMA = "palimpsest.oci-monitor-ipc-response.v1"
_RECEIPT_SCHEMA_V1 = "palimpsest.oci-monitor-exec-receipt.v1"
_RECEIPT_SCHEMA = "palimpsest.oci-monitor-exec-receipt.v2"
_PREACTIVATION_SCHEMA = "palimpsest.oci-monitor-preactivation.v1"
_PREACTIVATION_JOURNAL_SCHEMA = "palimpsest.oci-root-monitor-owner.v2"
_LIFECYCLE_PROTOCOL = "palimpsest.oci-lifecycle-control.v2"
_FRAME_LENGTH = struct.Struct(">I")
_PEER_CREDENTIALS = struct.Struct("3i")
_MAX_FRAME_BYTES = 16 * 1024
# Only the initial inherited CONFIG socket carries filesystem authority.
# v10 adds up to 24 distinct lower FD paths to the previous maximum of 24.
# The 1MiB encoded envelope is an admission limit, not a promise that every
# combination of maximum-length/JSON-escaped paths fits. Validate its exact
# bytes before creating a socket or child; do not silently truncate authority.
# Lower lease graphs are still reloaded from the ledger. Control stays 16KiB.
_MAX_CONFIG_FRAME_BYTES = 1024 * 1024
_MAX_JOURNAL_BYTES = 16 * 1024
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 30.0
_DIGEST_RE = __import__("re").compile(r"^sha256:[0-9a-f]{64}$")
_NONCE_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
_SPAWN_LOCK = threading.Lock()
_PREACTIVATION_PHASES = frozenset(
    {
        "claiming",
        "prepared",
        "committed",
        "aborting",
        "adopting",
        "control-lost",
        "abandoned",
        "activating",
        "active",
        "ready",
        "terminal",
    }
)
_ACTIVATION_PHASES = frozenset({"activating", "active", "ready", "terminal"})
_DISCOVERABLE_PHASES = frozenset({"committed"}) | _ACTIVATION_PHASES
_DESCRIBE_STATES = _DISCOVERABLE_PHASES | {"launch-pending", "launch-failed", "control-lost"}
_RENAME_NOREPLACE = 1


class MonitorIPCErrorCategory(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported-platform"
    INVALID_IDENTITY = "invalid-identity"
    UNSAFE_DIRECTORY = "unsafe-directory"
    SPAWN_BOUNDARY = "spawn-boundary"
    SPAWN_FAILED = "spawn-failed"
    TIMEOUT = "timeout"
    INVALID_FRAME = "invalid-frame"
    UNAUTHORIZED_PEER = "unauthorized-peer"
    BINDING_MISMATCH = "binding-mismatch"
    SOCKET_COLLISION = "socket-collision"
    SOCKET_CHANGED = "socket-changed"
    CHILD_FAILED = "child-failed"
    CLOSED = "closed"
    INVALID_JOURNAL = "invalid-journal"
    JOURNAL_IO = "journal-io"
    JOURNAL_BUSY = "journal-busy"
    INVALID_TRANSITION = "invalid-transition"
    WRITER_LIVE = "writer-live"
    WRITER_UNKNOWN = "writer-unknown"
    NOT_COMMITTED = "not-committed"
    CONTROL_LOST = "control-lost"
    POISONED = "poisoned"


class MonitorIPCError(PalimpsestError):
    """Path-free, peer-input-free monitor IPC failure."""

    def __init__(self, category: MonitorIPCErrorCategory) -> None:
        if not isinstance(category, MonitorIPCErrorCategory):
            raise TypeError("monitor IPC error requires a stable category")
        self.category = category
        super().__init__(category.value)


class MonitorIPCOperation(StrEnum):
    DESCRIBE = "describe"
    PING = "ping"
    SHUTDOWN = "shutdown"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class MonitorPreActivationBinding:
    record: ExistingRunRecord
    owner_uid: int
    plan_digest: str
    expected_definition_projection_digest: str
    stage1_artifact_digest: str
    domain_uuid: str
    boot_attempt_id: str
    libvirt_uri: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record, ExistingRunRecord)
            or self.record.state_schema_version != 2
            or self.record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
            or self.record.dispatch_key.backend is not RuntimeBackend.KVM
            or type(self.owner_uid) is not int
            or self.owner_uid != os.geteuid()
            or not isinstance(self.plan_digest, str)
            or _DIGEST_RE.fullmatch(self.plan_digest) is None
            or not isinstance(self.expected_definition_projection_digest, str)
            or _DIGEST_RE.fullmatch(self.expected_definition_projection_digest) is None
            or not isinstance(self.stage1_artifact_digest, str)
            or _DIGEST_RE.fullmatch(self.stage1_artifact_digest) is None
            or not _canonical_uuid(self.domain_uuid)
            or not _canonical_uuid(self.boot_attempt_id)
            or self.libvirt_uri != "qemu:///system"
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.record.dispatch_key.backend.value,
            "boot_attempt_id": self.boot_attempt_id,
            "expected_definition_projection_digest": self.expected_definition_projection_digest,
            "domain_uuid": self.domain_uuid,
            "libvirt_uri": self.libvirt_uri,
            "lifecycle_protocol": _LIFECYCLE_PROTOCOL,
            "name": self.record.name,
            "owner_uid": self.owner_uid,
            "plan_digest": self.plan_digest,
            "run_id": self.record.run_id,
            "runtime_kind": self.record.dispatch_key.runtime_kind.value,
            "schema": _PREACTIVATION_SCHEMA,
            "stage1_artifact_digest": self.stage1_artifact_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> MonitorPreActivationBinding:
        expected = {
            "backend",
            "boot_attempt_id",
            "expected_definition_projection_digest",
            "domain_uuid",
            "libvirt_uri",
            "lifecycle_protocol",
            "name",
            "owner_uid",
            "plan_digest",
            "run_id",
            "runtime_kind",
            "schema",
            "stage1_artifact_digest",
        }
        if type(value) is not dict or set(value) != expected:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        if (
            value.get("schema") != _PREACTIVATION_SCHEMA
            or value.get("runtime_kind") != RuntimeKind.OCI_ROOT.value
            or value.get("backend") != RuntimeBackend.KVM.value
            or value.get("lifecycle_protocol") != _LIFECYCLE_PROTOCOL
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        try:
            record = ExistingRunRecord(
                value["name"],
                value["run_id"],
                2,
                DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
            )
            return cls(
                record,
                value["owner_uid"],
                value["plan_digest"],
                value["expected_definition_projection_digest"],
                value["stage1_artifact_digest"],
                value["domain_uuid"],
                value["boot_attempt_id"],
                value["libvirt_uri"],
            )
        except (KeyError, TypeError, ValueError, MonitorIPCError):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME) from None

    @property
    def digest(self) -> str:
        payload = _canonical_bytes(self.to_dict())
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class MonitorExecIdentity:
    binding: MonitorPreActivationBinding
    generation: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, MonitorPreActivationBinding) or not _canonical_uuid(self.generation):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)

    @property
    def run_id(self) -> str:
        return self.binding.record.run_id

    @property
    def binding_digest(self) -> str:
        return self.binding.digest

    @property
    def owner_uid(self) -> int:
        return self.binding.owner_uid

    def handshake_dict(self) -> dict[str, Any]:
        return {
            "binding_digest": self.binding_digest,
            "generation": self.generation,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class MonitorExecEndpoint:
    identity: MonitorExecIdentity
    writer: MonitorProcessIdentity
    socket_device: int
    socket_inode: int
    socket_name: str | None = None
    receipt_schema: str = _RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.identity, MonitorExecIdentity):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
        if self.socket_name is None:
            object.__setattr__(self, "socket_name", _socket_name_for_generation(self.identity.generation))
        if (
            not isinstance(self.writer, MonitorProcessIdentity)
            or type(self.socket_device) is not int
            or self.socket_device < 0
            or type(self.socket_inode) is not int
            or self.socket_inode <= 0
            or not _valid_socket_name(self.socket_name, self.identity.generation, self.receipt_schema)
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.identity.binding.to_dict(),
            "binding_digest": self.identity.binding_digest,
            "generation": self.identity.generation,
            "schema": self.receipt_schema,
            "socket": (
                {"device": self.socket_device, "inode": self.socket_inode}
                if self.receipt_schema == _RECEIPT_SCHEMA_V1
                else {"device": self.socket_device, "inode": self.socket_inode, "name": self.socket_name}
            ),
            "writer": self.writer.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict()) + b"\n"

    @classmethod
    def from_dict(cls, value: object) -> MonitorExecEndpoint:
        expected = {"binding", "binding_digest", "generation", "schema", "socket", "writer"}
        if (
            type(value) is not dict
            or set(value) != expected
            or value.get("schema") not in {_RECEIPT_SCHEMA_V1, _RECEIPT_SCHEMA}
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        socket_value = value.get("socket")
        socket_fields = {"device", "inode"} if value["schema"] == _RECEIPT_SCHEMA_V1 else {"device", "inode", "name"}
        if type(socket_value) is not dict or set(socket_value) != socket_fields:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        try:
            binding = MonitorPreActivationBinding.from_dict(value["binding"])
            identity = MonitorExecIdentity(binding, value["generation"])
            if value["binding_digest"] != identity.binding_digest:
                raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
            return cls(
                identity,
                _process_from_dict(value["writer"]),
                socket_value["device"],
                socket_value["inode"],
                _SOCKET_NAME if value["schema"] == _RECEIPT_SCHEMA_V1 else socket_value["name"],
                value["schema"],
            )
        except (KeyError, MonitorIPCError):
            raise
        except Exception:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME) from None

    @classmethod
    def from_bytes(cls, payload: bytes) -> MonitorExecEndpoint:
        if not isinstance(payload, bytes) or not payload.endswith(b"\n") or len(payload) > _MAX_FRAME_BYTES:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        value = _decode_frame(payload[:-1])
        endpoint = cls.from_dict(value)
        if endpoint.to_bytes() != payload:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        return endpoint


@dataclass(frozen=True, slots=True)
class MonitorPreactivationJournalSnapshot:
    identity: MonitorExecIdentity
    nonce_digest: str
    phase: str
    revision: int
    writer: MonitorProcessIdentity
    socket_name: str
    socket_device: int | None
    socket_inode: int | None
    active_binding: MonitorBinding | None = None

    def __post_init__(self) -> None:
        socket_absent = self.socket_device is None and self.socket_inode is None
        socket_present = (
            type(self.socket_device) is int
            and self.socket_device >= 0
            and type(self.socket_inode) is int
            and self.socket_inode > 0
        )
        if (
            not isinstance(self.identity, MonitorExecIdentity)
            or not isinstance(self.nonce_digest, str)
            or _DIGEST_RE.fullmatch(self.nonce_digest) is None
            or not isinstance(self.phase, str)
            or self.phase not in _PREACTIVATION_PHASES
            or type(self.revision) is not int
            or not 1 <= self.revision <= 2**63 - 1
            or not isinstance(self.writer, MonitorProcessIdentity)
            or self.socket_name != _socket_name_for_generation(self.identity.generation)
            or not (socket_absent or socket_present)
            or (self.phase == "claiming" and not socket_absent)
            or (self.phase in {"prepared", "committed"} | _ACTIVATION_PHASES and not socket_present)
            or (self.phase in {"active", "ready", "terminal"} and self.active_binding is None)
            or (self.active_binding is not None and not socket_present)
            or (self.phase not in {"active", "ready", "terminal", "control-lost"} and self.active_binding is not None)
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
        if self.active_binding is not None:
            _validate_active_binding(self.active_binding, self.identity.binding)

    @property
    def endpoint(self) -> MonitorExecEndpoint:
        if self.socket_device is None or self.socket_inode is None:
            raise MonitorIPCError(MonitorIPCErrorCategory.NOT_COMMITTED)
        return MonitorExecEndpoint(
            self.identity,
            self.writer,
            self.socket_device,
            self.socket_inode,
            self.socket_name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_binding": None if self.active_binding is None else self.active_binding.to_dict(),
            "binding": self.identity.binding.to_dict(),
            "binding_digest": self.identity.binding_digest,
            "monitor_generation": self.identity.generation,
            "nonce_digest": self.nonce_digest,
            "phase": self.phase,
            "revision": self.revision,
            "schema": _PREACTIVATION_JOURNAL_SCHEMA,
            "socket": {
                "device": self.socket_device,
                "inode": self.socket_inode,
                "name": self.socket_name,
            },
            "writer": self.writer.to_dict(),
        }


def _validate_active_binding(binding: MonitorBinding, expected: MonitorPreActivationBinding) -> None:
    if type(binding) is not MonitorBinding:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    try:
        binding.__post_init__()
        raw = binding.to_dict()
        expected_raw = expected.to_dict()
        expected_raw.pop("schema")
        expected_raw["definition_projection_digest"] = expected_raw.pop("expected_definition_projection_digest")
        expected_raw["domain_id"] = binding.domain_id
        if any(type(raw[key]) is not type(value) or raw[key] != value for key, value in expected_raw.items()):
            raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
    except PalimpsestError:
        raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH) from None


def _active_binding_from_dict(value: object, expected: MonitorPreActivationBinding) -> MonitorBinding | None:
    if value is None:
        return None
    template = expected.to_dict()
    template.pop("schema")
    template["definition_projection_digest"] = template.pop("expected_definition_projection_digest")
    if type(value) is not dict or set(value) != set(template) | {"domain_id"}:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
    if any(type(value[key]) is not type(item) or value[key] != item for key, item in template.items()):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
    try:
        binding = MonitorBinding(
            expected.record,
            value["owner_uid"],
            value["plan_digest"],
            value["definition_projection_digest"],
            value["stage1_artifact_digest"],
            value["domain_uuid"],
            value["domain_id"],
            value["boot_attempt_id"],
            value["libvirt_uri"],
        )
        _validate_active_binding(binding, expected)
        return binding
    except PalimpsestError:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None


def _has_activation_evidence(snapshot: MonitorPreactivationJournalSnapshot) -> bool:
    return snapshot.phase in _ACTIVATION_PHASES or snapshot.active_binding is not None


def _nonce_digest(nonce: str) -> str:
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    return f"sha256:{hashlib.sha256(nonce.encode('ascii')).hexdigest()}"


def _decode_preactivation_snapshot(
    content: bytes,
    expected: MonitorExecIdentity | MonitorPreActivationBinding,
) -> MonitorPreactivationJournalSnapshot:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
    expected_fields = {
        "active_binding",
        "binding",
        "binding_digest",
        "monitor_generation",
        "nonce_digest",
        "phase",
        "revision",
        "schema",
        "socket",
        "writer",
    }
    socket_value = raw.get("socket") if type(raw) is dict else None
    binding = expected.binding if isinstance(expected, MonitorExecIdentity) else expected
    if not isinstance(binding, MonitorPreActivationBinding):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    if (
        type(raw) is not dict
        or set(raw) != expected_fields
        or raw.get("schema") != _PREACTIVATION_JOURNAL_SCHEMA
        or raw.get("binding") != binding.to_dict()
        or (
            type(raw.get("binding")) is dict
            and any(type(raw["binding"].get(key)) is not type(value) for key, value in binding.to_dict().items())
        )
        or raw.get("binding_digest") != binding.digest
        or type(socket_value) is not dict
        or set(socket_value) != {"device", "inode", "name"}
        or _canonical_bytes(raw) + b"\n" != content
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
    try:
        identity = MonitorExecIdentity(binding, raw["monitor_generation"])
        if isinstance(expected, MonitorExecIdentity) and identity != expected:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
        return MonitorPreactivationJournalSnapshot(
            identity,
            raw["nonce_digest"],
            raw["phase"],
            raw["revision"],
            _process_from_dict(raw["writer"]),
            socket_value["name"],
            socket_value["device"],
            socket_value["inode"],
            _active_binding_from_dict(raw["active_binding"], binding),
        )
    except (KeyError, MonitorIPCError, TypeError, ValueError, RecursionError):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None


def _journal_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _read_preactivation_journal(
    directory_fd: int,
    expected: MonitorExecIdentity | MonitorPreActivationBinding,
    *,
    missing_ok: bool = False,
) -> tuple[MonitorPreactivationJournalSnapshot, bytes] | None:
    _validate_directory(directory_fd)
    descriptor = -1
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        try:
            descriptor = os.open(_JOURNAL_NAME, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
        opened = os.fstat(descriptor)
        visible = os.stat(_JOURNAL_NAME, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or not stat_module.S_ISREG(visible.st_mode)
            or opened.st_uid != os.geteuid()
            or visible.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or visible.st_nlink != 1
            or stat_module.S_IMODE(opened.st_mode) != 0o600
            or stat_module.S_IMODE(visible.st_mode) != 0o600
            or _journal_identity(opened) != _journal_identity(visible)
            or opened.st_size <= 0
            or opened.st_size > _MAX_JOURNAL_BYTES
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
        content = os.read(descriptor, _MAX_JOURNAL_BYTES + 1)
        after = os.fstat(descriptor)
        current = os.stat(_JOURNAL_NAME, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(content) != opened.st_size
            or _journal_identity(after) != _journal_identity(opened)
            or _journal_identity(current) != _journal_identity(opened)
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
    except MonitorIPCError:
        raise
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.JOURNAL_IO) from None
    return _decode_preactivation_snapshot(content, expected), content


class _ForkClosePreactivationLease:
    __slots__ = ("directory_fd", "lease", "lock_fd")

    def __init__(self, lease: _PreactivationJournalLease, directory_fd: int, lock_fd: int) -> None:
        self.directory_fd = directory_fd
        self.lease = weakref.ref(lease)
        self.lock_fd = lock_fd

    def close_in_child(self) -> None:
        lock_fd, self.lock_fd = self.lock_fd, -1
        directory_fd, self.directory_fd = self.directory_fd, -1
        lease = self.lease()
        if lease is not None:
            # A vanished worker may have held this lock at fork. The child
            # has no authority and must fail CLOSED, not deadlock on that lock.
            lease._mutex = threading.RLock()
            lease._lock_fd = -1
            lease._directory_fd = -1
            lease._fork_resource = None
            lease._closed = True
            lease._poisoned = True
        for descriptor in (lock_fd, directory_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


_FORK_PREACTIVATION_LEASES: set[_ForkClosePreactivationLease] = set()
_FORK_PREACTIVATION_LOCK = threading.Lock()


def _close_inherited_preactivation_leases() -> None:
    try:
        inherited = tuple(_FORK_PREACTIVATION_LEASES)
        _FORK_PREACTIVATION_LEASES.clear()
        for resource in inherited:
            resource.close_in_child()
    finally:
        _FORK_PREACTIVATION_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_PREACTIVATION_LOCK.acquire,
        after_in_parent=_FORK_PREACTIVATION_LOCK.release,
        after_in_child=_close_inherited_preactivation_leases,
    )


def _serialized_lease(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def locked(self: _PreactivationJournalLease, *args: Any, **kwargs: Any) -> Any:
        with self._mutex:
            return method(self, *args, **kwargs)

    return locked


class _PreactivationJournalLease:
    __slots__ = (
        "__weakref__",
        "_closed",
        "_directory_fd",
        "_directory_identity",
        "_fork_resource",
        "_identity",
        "_lock_fd",
        "_lock_identity",
        "_mutex",
        "_poisoned",
        "_snapshot",
        "_snapshot_bytes",
    )

    def __init__(self, directory_fd: int, identity: MonitorExecIdentity) -> None:
        if type(directory_fd) is not int or not isinstance(identity, MonitorExecIdentity):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
        self._closed = False
        self._mutex = threading.RLock()
        self._directory_fd = -1
        self._directory_identity: tuple[int, int] | None = None
        self._fork_resource: _ForkClosePreactivationLease | None = None
        self._identity = identity
        self._lock_fd = -1
        self._lock_identity: tuple[int, int] | None = None
        self._poisoned = False
        self._snapshot: MonitorPreactivationJournalSnapshot | None = None
        self._snapshot_bytes: bytes | None = None
        _FORK_PREACTIVATION_LOCK.acquire()
        try:
            try:
                self._directory_fd = os.dup(directory_fd)
                self._validate_directory()
                self._open_lock()
                resource = _ForkClosePreactivationLease(self, self._directory_fd, self._lock_fd)
                self._fork_resource = resource
                _FORK_PREACTIVATION_LEASES.add(resource)
            except BaseException:
                self._closed = True
                for name in ("_lock_fd", "_directory_fd"):
                    descriptor = getattr(self, name)
                    setattr(self, name, -1)
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                raise
        finally:
            _FORK_PREACTIVATION_LOCK.release()

    @classmethod
    def create(
        cls,
        directory_fd: int,
        identity: MonitorExecIdentity,
        nonce: str,
        writer: MonitorProcessIdentity,
    ) -> _PreactivationJournalLease:
        lease = cls(directory_fd, identity)
        try:
            loaded = _read_preactivation_journal(
                lease._directory_fd,
                identity.binding,
                missing_ok=True,
            )
            snapshot = MonitorPreactivationJournalSnapshot(
                identity,
                _nonce_digest(nonce),
                "claiming",
                1 if loaded is None else loaded[0].revision + 1,
                writer,
                _socket_name_for_generation(identity.generation),
                None,
                None,
            )
            if loaded is None:
                lease._publish(snapshot, create=True)
            else:
                previous, previous_bytes = loaded
                if previous.phase != "abandoned" or _has_activation_evidence(previous):
                    raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
                try:
                    os.stat(previous.socket_name, dir_fd=lease._directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
                else:
                    raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
                lease._snapshot = previous
                lease._snapshot_bytes = previous_bytes
                lease._publish(snapshot, create=False)
            return lease
        except BaseException:
            lease.close()
            raise

    @classmethod
    def adopt_stale(
        cls,
        directory_fd: int,
        identity: MonitorExecIdentity,
        expected: MonitorPreactivationJournalSnapshot,
        expected_bytes: bytes,
        writer: MonitorProcessIdentity,
        liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness],
    ) -> _PreactivationJournalLease:
        lease = cls(directory_fd, identity)
        try:
            loaded = lease._read(missing_ok=False)
            if loaded != (expected, expected_bytes):
                raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
            if _has_activation_evidence(expected) or expected.phase == "control-lost":
                raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
            try:
                liveness = liveness_probe(expected.writer)
            except Exception:
                liveness = ProcessLiveness.UNKNOWN
            if liveness is ProcessLiveness.LIVE:
                raise MonitorIPCError(MonitorIPCErrorCategory.WRITER_LIVE)
            if liveness is not ProcessLiveness.STALE:
                raise MonitorIPCError(MonitorIPCErrorCategory.WRITER_UNKNOWN)
            lease._snapshot, lease._snapshot_bytes = loaded
            lease._take_over(writer)
            return lease
        except BaseException:
            lease.close()
            raise

    @property
    @_serialized_lease
    def snapshot(self) -> MonitorPreactivationJournalSnapshot:
        self._require_authority()
        assert self._snapshot is not None
        return self._snapshot

    def mark_prepared(self, socket_device: int, socket_inode: int) -> MonitorPreactivationJournalSnapshot:
        return self._transition("prepared", socket_identity=(socket_device, socket_inode), allowed={"claiming"})

    def mark_committed(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("committed", allowed={"prepared"})

    @_serialized_lease
    def validate_directory_binding(self, directory_fd: int) -> None:
        self._require_authority()
        if type(directory_fd) is not int:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
        _validate_directory(directory_fd)
        if _journal_identity(os.fstat(directory_fd)) != self._directory_identity:
            raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)

    @_serialized_lease
    def validate_launch_binding(
        self,
        binding: MonitorPreActivationBinding,
        *,
        directory_fd: int,
        activating: bool = False,
    ) -> None:
        self.validate_directory_binding(directory_fd)
        assert self._snapshot is not None
        if type(binding) is not MonitorPreActivationBinding or type(activating) is not bool:
            raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
        binding.__post_init__()
        if binding != self._identity.binding:
            raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
        phase = "activating" if activating else "committed"
        if self._snapshot.phase != phase or self._snapshot.active_binding is not None:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_TRANSITION)

    def mark_activating(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("activating", allowed={"committed"})

    def promote_active(self, binding: MonitorBinding) -> MonitorPreactivationJournalSnapshot:
        _validate_active_binding(binding, self._identity.binding)
        return self._transition("active", allowed={"activating"}, active_binding=binding)

    def mark_ready(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("ready", allowed={"active"})

    def mark_terminal(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("terminal", allowed={"ready"})

    def mark_aborting(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("aborting", allowed={"claiming", "prepared", "committed"})

    def mark_abandoned(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("abandoned", allowed={"aborting", "adopting"})

    def mark_control_lost(self) -> MonitorPreactivationJournalSnapshot:
        return self._transition("control-lost", allowed={"aborting", "adopting"} | _ACTIVATION_PHASES)

    @_serialized_lease
    def _take_over(self, writer: MonitorProcessIdentity) -> MonitorPreactivationJournalSnapshot:
        """Publish stale adoption without requiring authority from the stale writer."""

        if not isinstance(writer, MonitorProcessIdentity):
            raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
        self._validate_lock()
        loaded = self._read(missing_ok=False)
        if loaded != (self._snapshot, self._snapshot_bytes):
            self._poisoned = True
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
        try:
            current = current_process_identity()
        except Exception:
            self._poisoned = True
            raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER) from None
        if (
            current != writer
            or self._snapshot is None
            or self._snapshot.phase in {"abandoned", "control-lost"}
            or _has_activation_evidence(self._snapshot)
        ):
            self._poisoned = True
            raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
        candidate = MonitorPreactivationJournalSnapshot(
            self._identity,
            self._snapshot.nonce_digest,
            "adopting",
            self._snapshot.revision + 1,
            writer,
            self._snapshot.socket_name,
            self._snapshot.socket_device,
            self._snapshot.socket_inode,
        )
        self._publish(candidate, create=False)
        return candidate

    @_serialized_lease
    def _transition(
        self,
        phase: str,
        *,
        allowed: set[str] | None = None,
        writer: MonitorProcessIdentity | None = None,
        socket_identity: tuple[int, int] | None = None,
        active_binding: MonitorBinding | None = None,
    ) -> MonitorPreactivationJournalSnapshot:
        self._require_authority()
        assert self._snapshot is not None
        if allowed is not None and self._snapshot.phase not in allowed:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_TRANSITION)
        device, inode = (
            socket_identity
            if socket_identity is not None
            else (self._snapshot.socket_device, self._snapshot.socket_inode)
        )
        candidate = MonitorPreactivationJournalSnapshot(
            self._identity,
            self._snapshot.nonce_digest,
            phase,
            self._snapshot.revision + 1,
            writer or self._snapshot.writer,
            self._snapshot.socket_name,
            device,
            inode,
            self._snapshot.active_binding if active_binding is None else active_binding,
        )
        self._publish(candidate, create=False)
        return candidate

    def _validate_directory(self) -> None:
        try:
            metadata = os.fstat(self._directory_fd)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
        if self._directory_identity is None:
            self._directory_identity = _journal_identity(metadata)
        if (
            not stat_module.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(metadata.st_mode) != 0o700
            or _journal_identity(metadata) != self._directory_identity
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)

    def _open_lock(self) -> None:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        created = False
        try:
            self._lock_fd = os.open(_LOCK_NAME, flags, dir_fd=self._directory_fd)
        except FileNotFoundError:
            try:
                self._lock_fd = os.open(
                    _LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=self._directory_fd,
                )
                os.fchmod(self._lock_fd, 0o600)
                created = True
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
        self._validate_lock()
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.JOURNAL_BUSY) from None
        self._validate_lock()
        if created:
            try:
                os.fsync(self._lock_fd)
                os.fsync(self._directory_fd)
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.JOURNAL_IO) from None

    def _validate_lock(self) -> None:
        self._validate_directory()
        try:
            opened = os.fstat(self._lock_fd)
            visible = os.stat(_LOCK_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
        if self._lock_identity is None:
            self._lock_identity = _journal_identity(opened)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or not stat_module.S_ISREG(visible.st_mode)
            or opened.st_uid != os.geteuid()
            or visible.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or visible.st_nlink != 1
            or stat_module.S_IMODE(opened.st_mode) != 0o600
            or stat_module.S_IMODE(visible.st_mode) != 0o600
            or _journal_identity(opened) != self._lock_identity
            or _journal_identity(visible) != self._lock_identity
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)

    def _read(
        self,
        *,
        missing_ok: bool,
    ) -> tuple[MonitorPreactivationJournalSnapshot, bytes] | None:
        self._validate_lock()
        expected = self._snapshot.identity if self._snapshot is not None else self._identity
        return _read_preactivation_journal(self._directory_fd, expected, missing_ok=missing_ok)

    def _publish(self, snapshot: MonitorPreactivationJournalSnapshot, *, create: bool) -> None:
        try:
            self._validate_lock()
            if not create:
                loaded = self._read(missing_ok=False)
                if loaded != (self._snapshot, self._snapshot_bytes):
                    raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
        except MonitorIPCError:
            self._poisoned = True
            raise
        content = _canonical_bytes(snapshot.to_dict()) + b"\n"
        temporary = f".oci-monitor-v2-{uuid.uuid4().hex}.tmp"
        descriptor = -1
        temporary_identity: tuple[int, int] | None = None
        published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            temporary_identity = _journal_identity(metadata)
            if (
                not stat_module.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat_module.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise MonitorIPCError(MonitorIPCErrorCategory.JOURNAL_IO)
            offset = 0
            while offset < len(content):
                written = os.write(descriptor, content[offset:])
                if written <= 0:
                    raise MonitorIPCError(MonitorIPCErrorCategory.JOURNAL_IO)
                offset += written
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            if _journal_identity(after) != temporary_identity or after.st_size != len(content):
                raise MonitorIPCError(MonitorIPCErrorCategory.JOURNAL_IO)
            self._validate_lock()
            if create:
                os.link(
                    temporary,
                    _JOURNAL_NAME,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=self._directory_fd)
            else:
                os.replace(
                    temporary,
                    _JOURNAL_NAME,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
            temporary = ""
            published = True
            os.fsync(self._directory_fd)
            loaded = _read_preactivation_journal(self._directory_fd, snapshot.identity)
            if loaded != (snapshot, content):
                raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
            self._snapshot = snapshot
            self._snapshot_bytes = content
        except FileExistsError:
            self._poisoned = True
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL) from None
        except MonitorIPCError:
            self._poisoned = True
            raise
        except OSError as exc:
            self._poisoned = True
            category = (
                MonitorIPCErrorCategory.INVALID_JOURNAL
                if create and exc.errno == errno.EEXIST
                else MonitorIPCErrorCategory.JOURNAL_IO
            )
            raise MonitorIPCError(category) from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary and temporary_identity is not None:
                try:
                    current = os.stat(temporary, dir_fd=self._directory_fd, follow_symlinks=False)
                    if _journal_identity(current) == temporary_identity and stat_module.S_ISREG(current.st_mode):
                        os.unlink(temporary, dir_fd=self._directory_fd)
                except OSError:
                    pass
            if published and self._poisoned:
                self._snapshot = None
                self._snapshot_bytes = None

    @_serialized_lease
    def _require_authority(self) -> None:
        if self._closed:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED)
        if self._poisoned:
            raise MonitorIPCError(MonitorIPCErrorCategory.POISONED)
        try:
            self._validate_lock()
            loaded = self._read(missing_ok=False)
            if loaded != (self._snapshot, self._snapshot_bytes):
                raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
            try:
                current = current_process_identity()
            except Exception:
                raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER) from None
            if self._snapshot is None or current != self._snapshot.writer:
                raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
        except MonitorIPCError:
            self._poisoned = True
            raise

    @_serialized_lease
    def close(self) -> None:
        _FORK_PREACTIVATION_LOCK.acquire()
        try:
            if self._closed:
                return
            self._closed = True
            resource, self._fork_resource = self._fork_resource, None
            lock_fd, self._lock_fd = self._lock_fd, -1
            directory_fd, self._directory_fd = self._directory_fd, -1
            if resource is not None:
                resource.lock_fd = -1
                resource.directory_fd = -1
                _FORK_PREACTIVATION_LEASES.discard(resource)
            for descriptor in (lock_fd, directory_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        finally:
            _FORK_PREACTIVATION_LOCK.release()


@dataclass(frozen=True, slots=True)
class MonitorIPCReply:
    operation: MonitorIPCOperation
    state: str
    writer: MonitorProcessIdentity


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _socket_name_for_generation(generation: str) -> str:
    if not _canonical_uuid(generation):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    return f"oci-monitor-{uuid.UUID(generation).hex}.sock"


def _valid_socket_name(value: object, generation: str, schema: object = _RECEIPT_SCHEMA) -> bool:
    if not isinstance(value, str) or "/" in value or "\x00" in value:
        return False
    if schema == _RECEIPT_SCHEMA_V1:
        return value == _SOCKET_NAME
    return schema == _RECEIPT_SCHEMA and value == _socket_name_for_generation(generation)


def _socket_quarantine_name(socket_name: str, identity: tuple[int, int]) -> str:
    if (
        not isinstance(socket_name, str)
        or "/" in socket_name
        or "\x00" in socket_name
        or len(identity) != 2
        or any(type(value) is not int or value < 0 for value in identity)
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    return f".{socket_name}.{identity[0]:x}-{identity[1]:x}.quarantine"


def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    """Atomically rename one descriptor-relative entry without clobbering."""

    if sys.platform != "linux" or type(directory_fd) is not int:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME) from None


def _decode_frame(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME) from None
    if type(value) is not dict or _canonical_bytes(value) != payload:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    return value


def _send_all(channel: socket.socket, payload: bytes) -> None:
    content = _FRAME_LENGTH.pack(len(payload)) + payload
    offset = 0
    while offset < len(content):
        try:
            written = channel.send(content[offset:])
        except TimeoutError:
            raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT) from None
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED) from None
        if written <= 0:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED)
        offset += written


def _send_frame(channel: socket.socket, value: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(value)
    if not payload or len(payload) > _MAX_FRAME_BYTES:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    _send_all(channel, payload)


def _recv_exact(channel: socket.socket, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        try:
            chunk = channel.recv(size - len(content))
        except TimeoutError:
            raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT) from None
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED) from None
        if not chunk:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED)
        content.extend(chunk)
    return bytes(content)


def _recv_bounded_frame(channel: socket.socket, maximum: int) -> dict[str, Any]:
    (size,) = _FRAME_LENGTH.unpack(_recv_exact(channel, _FRAME_LENGTH.size))
    if not 1 <= size <= maximum:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    return _decode_frame(_recv_exact(channel, size))


def _recv_frame(channel: socket.socket) -> dict[str, Any]:
    return _recv_bounded_frame(channel, _MAX_FRAME_BYTES)


def _encode_config_frame(value: Mapping[str, Any]) -> bytes:
    """Preflight the exact private CONFIG bytes before creating a child/socket."""
    _identity_from_config(value)
    payload = _canonical_bytes(value)
    if not payload or len(payload) > _MAX_CONFIG_FRAME_BYTES:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    return payload


def _recv_config_frame(channel: socket.socket) -> dict[str, Any]:
    value = _recv_bounded_frame(channel, _MAX_CONFIG_FRAME_BYTES)
    _identity_from_config(value)
    return value


def _process_to_dict(identity: MonitorProcessIdentity) -> dict[str, Any]:
    return identity.to_dict()


def _process_from_dict(value: object) -> MonitorProcessIdentity:
    try:
        return MonitorProcessIdentity.from_dict(value)
    except Exception:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME) from None


def _read_peer_credentials(channel: socket.socket) -> tuple[int, int, int]:
    option = getattr(socket, "SO_PEERCRED", None)
    if sys.platform != "linux" or option is None:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSUPPORTED_PLATFORM)
    try:
        payload = channel.getsockopt(socket.SOL_SOCKET, option, _PEER_CREDENTIALS.size)
        pid, uid, gid = _PEER_CREDENTIALS.unpack(payload)
    except (OSError, struct.error):
        raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER) from None
    if pid <= 0 or uid < 0 or gid < 0:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
    return pid, uid, gid


def _authorize_peer(
    channel: socket.socket,
    expected: MonitorProcessIdentity,
    owner_uid: int,
    *,
    credential_reader: Callable[[socket.socket], tuple[int, int, int]] = _read_peer_credentials,
    liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
) -> None:
    try:
        pid, uid, _gid = credential_reader(channel)
        liveness = liveness_probe(expected)
    except MonitorIPCError:
        raise
    except Exception:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER) from None
    if pid != expected.pid or uid != owner_uid or liveness is not ProcessLiveness.LIVE:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)


def _validate_directory(directory_fd: int) -> tuple[int, int]:
    try:
        metadata = os.fstat(directory_fd)
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY) from None
    if (
        not stat_module.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat_module.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY)
    return metadata.st_dev, metadata.st_ino


class _ForkCloseBoundSocket:
    __slots__ = ("bound", "directory_fd", "listener_fd")

    def __init__(self, bound: _BoundMonitorSocket, directory_fd: int, listener_fd: int) -> None:
        self.bound = weakref.ref(bound)
        self.directory_fd = directory_fd
        self.listener_fd = listener_fd

    def close_in_child(self) -> None:
        directory_fd, self.directory_fd = self.directory_fd, -1
        listener_fd, self.listener_fd = self.listener_fd, -1
        bound = self.bound()
        if bound is not None:
            listener, bound._listener = bound._listener, None
            bound._directory_fd = -1
            bound._fork_resource = None
            bound._fork_poisoned = True
            if listener is not None:
                try:
                    detached = listener.detach()
                except OSError:
                    detached = -1
                if detached >= 0:
                    listener_fd = detached
        for descriptor in (listener_fd, directory_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


_FORK_BOUND_SOCKETS: set[_ForkCloseBoundSocket] = set()
_FORK_BOUND_SOCKET_LOCK = threading.Lock()


def _close_inherited_bound_sockets() -> None:
    try:
        inherited = tuple(_FORK_BOUND_SOCKETS)
        _FORK_BOUND_SOCKETS.clear()
        for resource in inherited:
            resource.close_in_child()
    finally:
        _FORK_BOUND_SOCKET_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_BOUND_SOCKET_LOCK.acquire,
        after_in_parent=_FORK_BOUND_SOCKET_LOCK.release,
        after_in_child=_close_inherited_bound_sockets,
    )


def _socket_address(directory_fd: int, socket_name: str = _SOCKET_NAME) -> str:
    if not isinstance(socket_name, str) or "/" in socket_name or "\x00" in socket_name:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY)
    address = f"/proc/self/fd/{directory_fd}/{socket_name}"
    if len(os.fsencode(address)) > 107 or "\x00" in address:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY)
    return address


def _connect_socket(channel: socket.socket, directory_fd: int, socket_name: str = _SOCKET_NAME) -> None:
    try:
        channel.connect(_socket_address(directory_fd, socket_name))
    except TimeoutError:
        raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT) from None
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED) from None


def _visible_socket(directory_fd: int, socket_name: str = _SOCKET_NAME) -> os.stat_result:
    try:
        metadata = os.stat(socket_name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED) from None
    if (
        not stat_module.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat_module.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
    return metadata


class _BoundMonitorSocket:
    def __init__(self, directory_fd: int, socket_name: str = _SOCKET_NAME) -> None:
        _validate_directory(directory_fd)
        if not isinstance(socket_name, str) or "/" in socket_name or "\x00" in socket_name:
            raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY)
        try:
            self._directory_fd = os.dup(directory_fd)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY) from None
        self._listener: socket.socket | None = None
        self._identity: tuple[int, int] | None = None
        self._socket_name = socket_name
        self._fork_resource: _ForkCloseBoundSocket | None = None
        self._fork_poisoned = False
        try:
            try:
                os.stat(self._socket_name, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_COLLISION) from None
            else:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_COLLISION)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(_socket_address(self._directory_fd, self._socket_name))
            created = os.stat(self._socket_name, dir_fd=self._directory_fd, follow_symlinks=False)
            if not stat_module.S_ISSOCK(created.st_mode) or created.st_uid != os.geteuid():
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            self._identity = (created.st_dev, created.st_ino)
            os.chmod(self._socket_name, 0o600, dir_fd=self._directory_fd, follow_symlinks=False)
            metadata = _visible_socket(self._directory_fd, self._socket_name)
            if (metadata.st_dev, metadata.st_ino) != self._identity:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            listener.listen(8)
            self._listener = listener
            _FORK_BOUND_SOCKET_LOCK.acquire()
            try:
                resource = _ForkCloseBoundSocket(self, self._directory_fd, listener.fileno())
                self._fork_resource = resource
                _FORK_BOUND_SOCKETS.add(resource)
            finally:
                _FORK_BOUND_SOCKET_LOCK.release()
        except MonitorIPCError:
            self.close(ignore_changed=True)
            raise
        except OSError:
            self.close(ignore_changed=True)
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_COLLISION) from None

    @property
    def identity(self) -> tuple[int, int]:
        assert self._identity is not None
        return self._identity

    @property
    def listener(self) -> socket.socket:
        if self._listener is None:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED)
        return self._listener

    def validate(self) -> None:
        if self._fork_poisoned:
            raise MonitorIPCError(MonitorIPCErrorCategory.POISONED)
        metadata = _visible_socket(self._directory_fd, self._socket_name)
        if (metadata.st_dev, metadata.st_ino) != self.identity:
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)

    def unlink_exact_and_fsync(self) -> None:
        _FORK_BOUND_SOCKET_LOCK.acquire()
        try:
            listener, self._listener = self._listener, None
            if self._fork_resource is not None:
                self._fork_resource.listener_fd = -1
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
        finally:
            _FORK_BOUND_SOCKET_LOCK.release()
        descriptor = -1
        quarantine = _socket_quarantine_name(self._socket_name, self.identity)
        try:
            try:
                os.stat(quarantine, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
            else:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            path_flag = getattr(os, "O_PATH", None)
            if path_flag is None:
                raise OSError
            descriptor = os.open(
                self._socket_name,
                path_flag | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self._directory_fd,
            )
            opened = os.fstat(descriptor)
            metadata = _visible_socket(self._directory_fd, self._socket_name)
            if (
                _journal_identity(opened) != self.identity
                or _journal_identity(metadata) != self.identity
                or not stat_module.S_ISSOCK(opened.st_mode)
            ):
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            _rename_noreplace(self._directory_fd, self._socket_name, quarantine)
            moved = os.stat(quarantine, dir_fd=self._directory_fd, follow_symlinks=False)
            held = os.fstat(descriptor)
            if _journal_identity(moved) != self.identity or _journal_identity(held) != self.identity:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            os.unlink(quarantine, dir_fd=self._directory_fd)
            after_unlink = os.fstat(descriptor)
            if _journal_identity(after_unlink) != self.identity or after_unlink.st_nlink != 0:
                raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
            os.fsync(self._directory_fd)
            try:
                os.stat(quarantine, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
        except MonitorIPCError:
            raise
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
        self._identity = None

    def close(self, *, ignore_changed: bool = False, preserve_path: bool = False) -> None:
        if self._directory_fd < 0:
            return
        _FORK_BOUND_SOCKET_LOCK.acquire()
        try:
            resource, self._fork_resource = self._fork_resource, None
            if resource is not None:
                resource.listener_fd = -1
                resource.directory_fd = -1
                _FORK_BOUND_SOCKETS.discard(resource)
            changed = False
            listener, self._listener = self._listener, None
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            if self._identity is not None and not preserve_path:
                try:
                    metadata = os.stat(self._socket_name, dir_fd=self._directory_fd, follow_symlinks=False)
                    if (metadata.st_dev, metadata.st_ino) != self._identity or not stat_module.S_ISSOCK(
                        metadata.st_mode
                    ):
                        changed = True
                    else:
                        os.unlink(self._socket_name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    changed = True
                except OSError:
                    changed = True
            try:
                os.close(self._directory_fd)
            except OSError:
                pass
            self._directory_fd = -1
        finally:
            _FORK_BOUND_SOCKET_LOCK.release()
        if changed and not ignore_changed:
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)


def _spawn_message(
    kind: str,
    identity: MonitorExecIdentity,
    nonce: str,
    writer: MonitorProcessIdentity,
    socket_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    message = {
        "binding_digest": identity.binding_digest,
        "generation": identity.generation,
        "kind": kind,
        "nonce": nonce,
        "run_id": identity.run_id,
        "schema": _SPAWN_SCHEMA,
        "writer": _process_to_dict(writer),
    }
    if socket_identity is not None:
        message["socket"] = {
            "device": socket_identity[0],
            "inode": socket_identity[1],
            "name": _socket_name_for_generation(identity.generation),
        }
    return message


def _validate_spawn_message(
    value: object,
    kind: str,
    identity: MonitorExecIdentity,
    nonce: str,
    writer: MonitorProcessIdentity,
    socket_identity: tuple[int, int] | None = None,
) -> None:
    if value != _spawn_message(kind, identity, nonce, writer, socket_identity):
        raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)


def _request_message(
    operation: MonitorIPCOperation,
    identity: MonitorExecIdentity,
    client: MonitorProcessIdentity,
    request_id: str,
) -> dict[str, Any]:
    return {
        "binding_digest": identity.binding_digest,
        "client": _process_to_dict(client),
        "generation": identity.generation,
        "operation": operation.value,
        "request_id": request_id,
        "run_id": identity.run_id,
        "schema": _REQUEST_SCHEMA,
    }


def _response_message(
    operation: MonitorIPCOperation,
    identity: MonitorExecIdentity,
    writer: MonitorProcessIdentity,
    request_id: str,
    *,
    state: str | None = None,
) -> dict[str, Any]:
    state = (
        state
        or {
            MonitorIPCOperation.DESCRIBE: "committed",
            MonitorIPCOperation.PING: "pong",
            MonitorIPCOperation.SHUTDOWN: "shutting-down",
            MonitorIPCOperation.STOP: "stop-refused",
        }[operation]
    )
    return {
        "binding_digest": identity.binding_digest,
        "generation": identity.generation,
        "operation": operation.value,
        "request_id": request_id,
        "run_id": identity.run_id,
        "schema": _RESPONSE_SCHEMA,
        "state": state,
        "writer": _process_to_dict(writer),
    }


def _decode_request(
    value: dict[str, Any], identity: MonitorExecIdentity
) -> tuple[MonitorIPCOperation, str, MonitorProcessIdentity]:
    expected = {"binding_digest", "client", "generation", "operation", "request_id", "run_id", "schema"}
    if set(value) != expected or value.get("schema") != _REQUEST_SCHEMA:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    try:
        operation = MonitorIPCOperation(value["operation"])
    except (TypeError, ValueError):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME) from None
    request_id = value["request_id"]
    if not _canonical_uuid(request_id):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    if (
        value["run_id"] != identity.run_id
        or value["generation"] != identity.generation
        or value["binding_digest"] != identity.binding_digest
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
    return operation, request_id, _process_from_dict(value["client"])


class _LaunchWorker:
    """One child-local worker; IPC stays responsive during blocking lifecycle IO."""

    def __init__(
        self,
        authority: MonitorLaunchAuthority,
        directory_fd: int,
        binding: MonitorPreActivationBinding,
        lease: _PreactivationJournalLease,
    ) -> None:
        self.done = threading.Event()
        self.failed = False
        self.stop_control = MonitorStopControl()
        self._authority = authority
        self._directory_fd = directory_fd
        self._binding = binding
        self._lease = lease
        self._thread = threading.Thread(target=self._run, name="oci-monitor-launch", daemon=False)

    def start(self) -> None:
        try:
            self._thread.start()
        except BaseException:
            self._authority.close()
            self.done.set()
            raise

    def join(self) -> None:
        if self._thread.ident is not None:
            self._thread.join()

    def _run(self) -> None:
        try:
            self._authority.run(self._directory_fd, self._binding, self._lease, stop_control=self.stop_control)
        except Exception:
            # Never expose an exception string, path, or lifecycle secret.
            self.failed = True
            self.stop_control.mark_control_lost()
            try:
                if self._lease.snapshot.phase in _ACTIVATION_PHASES:
                    self._lease.mark_control_lost()
            except MonitorIPCError:
                pass
        finally:
            try:
                self._authority.close()
            finally:
                self.done.set()


def _serve_committed(
    bound: _BoundMonitorSocket,
    identity: MonitorExecIdentity,
    writer: MonitorProcessIdentity,
    timeout: float,
    lease: _PreactivationJournalLease,
    worker: _LaunchWorker | None = None,
) -> bool:
    while True:
        bound.validate()
        bound.listener.settimeout(timeout)
        try:
            channel, _address = bound.listener.accept()
        except TimeoutError:
            continue
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED) from None
        with channel:
            channel.settimeout(timeout)
            try:
                request = _recv_frame(channel)
                operation, request_id, client = _decode_request(request, identity)
                _authorize_peer(channel, client, identity.owner_uid)
                bound.validate()
            except MonitorIPCError:
                continue
            # A journal publication and this decision must be one serialized
            # view, including the interval between fsync and in-memory update.
            with lease._mutex:
                snapshot = lease.snapshot
                terminal_shutdown = snapshot.phase == "terminal" and worker is not None and worker.done.is_set()
                state = None
                if operation is MonitorIPCOperation.STOP:
                    state = "stop-refused"
                    if worker is not None and not worker.failed:
                        if terminal_shutdown:
                            state = "stop-terminal"
                        elif snapshot.phase == "terminal" and not worker.done.is_set() and worker.stop_control.accepted:
                            # Durable completion can precede worker descriptor
                            # cleanup. Preserve only an already-accepted STOP;
                            # a natural terminal never admits a first request.
                            state = "stop-accepted"
                        elif snapshot.phase == "ready" and not worker.done.is_set():
                            # The mailbox never touches libvirt/session state.
                            # READY in the durable journal is the admission
                            # fence; terminal observation can only revoke it.
                            requested = worker.stop_control.request()
                            if requested == "stop-accepted":
                                state = requested
                elif operation is MonitorIPCOperation.SHUTDOWN:
                    if worker is not None and not worker.done.is_set():
                        state = "shutdown-refused"
                    elif terminal_shutdown:
                        pass
                    elif _has_activation_evidence(snapshot) or snapshot.phase == "control-lost":
                        state = "shutdown-refused"
                    else:
                        lease.mark_aborting()
                elif operation is MonitorIPCOperation.DESCRIBE:
                    state = snapshot.phase
                    if state == "committed" and worker is not None:
                        state = "launch-failed" if worker.done.is_set() else "launch-pending"
                response = _response_message(operation, identity, writer, request_id, state=state)
            try:
                # A slow/lost peer must not hold the ownership mutex while
                # the lifecycle worker needs to publish READY or TERMINAL.
                _send_frame(channel, response)
            except MonitorIPCError:
                if operation is not MonitorIPCOperation.SHUTDOWN or state == "shutdown-refused":
                    continue
                # An accepted transport shutdown is durable even if its ACK
                # is lost. Finish exact socket cleanup after leaving channel.
        if operation is MonitorIPCOperation.SHUTDOWN:
            if state != "shutdown-refused":
                return terminal_shutdown


def _identity_from_config(value: dict[str, Any]) -> tuple[MonitorExecIdentity, MonitorProcessIdentity, str, float]:
    expected = {"binding", "generation", "nonce", "parent", "schema", "timeout_ms"}
    if set(value) not in (expected, expected | {"launch_authority"}) or value.get("schema") != _CONFIG_SCHEMA:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    nonce = value["nonce"]
    timeout_ms = value["timeout_ms"]
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None or type(timeout_ms) is not int:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    timeout = timeout_ms / 1000
    if not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    identity = MonitorExecIdentity(MonitorPreActivationBinding.from_dict(value["binding"]), value["generation"])
    return identity, _process_from_dict(value["parent"]), nonce, timeout


def _accept_precommit(
    bound: _BoundMonitorSocket,
    config: socket.socket,
    timeout: float,
) -> socket.socket:
    try:
        readable, _writable, exceptional = select.select(
            [bound.listener, config],
            [],
            [bound.listener, config],
            timeout,
        )
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED) from None
    if exceptional:
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
    if config in readable:
        try:
            probe = config.recv(1, socket.MSG_PEEK)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED) from None
        if not probe:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED)
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    if bound.listener not in readable:
        raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT)
    try:
        channel, _address = bound.listener.accept()
    except TimeoutError:
        raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT) from None
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED) from None
    return channel


def _child_main(directory_fd: int, config_fd: int) -> int:
    try:
        config = socket.socket(fileno=config_fd)
    except OSError:
        return 70
    bound: _BoundMonitorSocket | None = None
    lease: _PreactivationJournalLease | None = None
    authority = None
    worker: _LaunchWorker | None = None
    try:
        config.settimeout(_MAX_TIMEOUT_SECONDS)
        value = _recv_config_frame(config)
        identity, parent, nonce, timeout = _identity_from_config(value)
        if "launch_authority" in value:
            from .oci_monitor_launch import MonitorLaunchAuthority

            authority = MonitorLaunchAuthority.from_dict(
                value["launch_authority"], excluded_fds=(directory_fd, config_fd)
            )
            authority.validate(directory_fd=directory_fd, binding=identity.binding)
        if parent.pid == os.getpid():
            raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
        writer = current_process_identity()
        lease = _PreactivationJournalLease.create(directory_fd, identity, nonce, writer)
        bound = _BoundMonitorSocket(directory_fd, lease.snapshot.socket_name)
        _send_frame(config, {"kind": "bound", "schema": _SPAWN_SCHEMA})
        channel = _accept_precommit(bound, config, timeout)
        with channel:
            channel.settimeout(timeout)
            _authorize_peer(channel, parent, identity.owner_uid)
            prepared = _recv_frame(channel)
            _validate_spawn_message(prepared, "prepare", identity, nonce, parent)
            prepared_snapshot = lease.mark_prepared(*bound.identity)
            _send_frame(
                channel,
                _spawn_message("prepared", identity, nonce, writer, bound.identity),
            )
            committed = _recv_frame(channel)
            _validate_spawn_message(committed, "commit", identity, nonce, parent)
            bound.validate()
            if lease.snapshot != prepared_snapshot:
                raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
            lease.mark_committed()
            try:
                _send_frame(
                    channel,
                    _spawn_message("committed", identity, nonce, writer, bound.identity),
                )
            except MonitorIPCError:
                # The durable committed journal is now authoritative. Losing
                # the parent ACK path must not tear down a discoverable monitor.
                pass
            if authority is not None:
                try:
                    fence = _recv_frame(channel)
                    _validate_spawn_message(fence, "activate", identity, nonce, parent, bound.identity)
                    _authorize_peer(channel, parent, identity.owner_uid)
                    bound.validate()
                    if lease.snapshot.phase != "committed":
                        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
                    authority.validate(directory_fd=directory_fd, binding=identity.binding)
                except Exception:
                    # COMMIT alone grants no VM mutation authority. Parent
                    # death/lost fence leaves an inert discoverable journal.
                    authority.close()
                    authority = None
                else:
                    worker = _LaunchWorker(authority, directory_fd, identity.binding, lease)
                    authority = None  # The worker now owns these inherited FDs.
                    worker.start()
                    try:
                        _send_frame(channel, _spawn_message("launch-accepted", identity, nonce, writer, bound.identity))
                    except MonitorIPCError:
                        # The activation fence was accepted. Losing this ACK
                        # cannot revoke child ownership or stop an active VM.
                        pass
        try:
            config.close()
        except OSError:
            pass
        preserve_terminal = _serve_committed(bound, identity, writer, timeout, lease, worker)
        bound.unlink_exact_and_fsync()
        if not preserve_terminal:
            lease.mark_abandoned()
        bound.close()
        lease.close()
        return 0
    except Exception as failure:
        # Never tear down the worker's descriptors or release its ownership
        # lease while lifecycle IO might still be using them. A broken IPC
        # listener is not permission to abandon or terminate its VM.
        if worker is not None:
            worker.join()
        error = (
            failure if isinstance(failure, MonitorIPCError) else MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
        )
        try:
            _send_frame(config, {"category": error.category.value, "kind": "error", "schema": _SPAWN_SCHEMA})
        except MonitorIPCError:
            pass
        if lease is not None:
            try:
                phase = lease.snapshot.phase
                if phase in {"claiming", "prepared", "committed"}:
                    lease.mark_aborting()
                if lease.snapshot.phase == "aborting":
                    if bound is not None:
                        bound.unlink_exact_and_fsync()
                    else:
                        os.fsync(lease._directory_fd)
                    lease.mark_abandoned()
            except (MonitorIPCError, OSError):
                try:
                    if lease.snapshot.phase in {"aborting", "adopting"}:
                        lease.mark_control_lost()
                except MonitorIPCError:
                    pass
        if bound is not None:
            try:
                preserve = lease is not None and (
                    lease._poisoned
                    or (
                        lease._snapshot is not None
                        and (_has_activation_evidence(lease._snapshot) or lease._snapshot.phase == "control-lost")
                    )
                )
                bound.close(preserve_path=preserve)
            except MonitorIPCError:
                pass
        if lease is not None:
            lease.close()
        return 70
    finally:
        if authority is not None:
            authority.close()
        try:
            config.close()
        except OSError:
            pass


def _require_spawn_boundary() -> None:
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSUPPORTED_PLATFORM)
    # This private foundation must be invoked before importing libvirt and while
    # the caller is single-threaded. It is intentionally not a general daemonizer.
    if "libvirt" in sys.modules or threading.active_count() != 1:
        raise MonitorIPCError(MonitorIPCErrorCategory.SPAWN_BOUNDARY)


def _terminate_failed_child(process: subprocess.Popen[bytes], config: socket.socket) -> None:
    try:
        config.close()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _require_exact_journal_snapshot(
    directory_fd: int,
    expected: MonitorPreactivationJournalSnapshot,
) -> None:
    loaded = _read_preactivation_journal(directory_fd, expected.identity)
    if loaded is None or loaded[0] != expected or loaded[1] != _canonical_bytes(expected.to_dict()) + b"\n":
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)


class MonitorExecHandle:
    __slots__ = ("_directory_fd", "_endpoint", "_process", "_timeout")

    def __init__(
        self,
        directory_fd: int,
        endpoint: MonitorExecEndpoint,
        process: subprocess.Popen[bytes],
        timeout: float,
    ) -> None:
        try:
            self._directory_fd = os.dup(directory_fd)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY) from None
        self._endpoint = endpoint
        self._process = process
        self._timeout = timeout

    @property
    def endpoint(self) -> MonitorExecEndpoint:
        return self._endpoint

    @property
    def pid(self) -> int:
        return self._process.pid

    def request(self, operation: MonitorIPCOperation) -> MonitorIPCReply:
        if self._directory_fd < 0:
            raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED)
        return request_monitor(self._directory_fd, self._endpoint, operation, timeout=self._timeout)

    def shutdown(self) -> None:
        reply = self.request(MonitorIPCOperation.SHUTDOWN)
        if reply.state != "shutting-down":
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        try:
            result = self._process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT) from None
        if result != 0:
            raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
        loaded = _read_preactivation_journal(self._directory_fd, self._endpoint.identity)
        if (
            loaded is None
            or loaded[0].phase not in {"abandoned", "terminal"}
            or loaded[0].writer != self._endpoint.writer
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
        assert self._endpoint.socket_name is not None
        try:
            os.stat(self._endpoint.socket_name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
        else:
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
        self.close()

    def close(self) -> None:
        descriptor, self._directory_fd = self._directory_fd, -1
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def spawn_monitor_exec(
    directory_fd: int,
    identity: MonitorExecIdentity,
    *,
    timeout: float = 5.0,
    executable: str | None = None,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    launch_authority: MonitorLaunchAuthority | None = None,
) -> MonitorExecHandle:
    """Commit reciprocal identity, then optionally fence a typed child launch."""

    if (
        not isinstance(identity, MonitorExecIdentity)
        or isinstance(timeout, bool)
        or not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    _validate_directory(directory_fd)
    _require_spawn_boundary()
    authority_fds: tuple[int, ...] = ()
    if launch_authority is not None:
        from .oci_monitor_launch import MonitorLaunchAuthority

        if type(launch_authority) is not MonitorLaunchAuthority:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
        launch_authority.validate(directory_fd=directory_fd, binding=identity.binding)
        authority_fds = launch_authority.pass_fds
        if directory_fd in authority_fds:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    with _SPAWN_LOCK:
        try:
            parent = current_process_identity()
            nonce = os.urandom(32).hex()
        except Exception:
            raise MonitorIPCError(MonitorIPCErrorCategory.SPAWN_FAILED) from None
        config_value = {
            "binding": identity.binding.to_dict(),
            "generation": identity.generation,
            "nonce": nonce,
            "parent": _process_to_dict(parent),
            "schema": _CONFIG_SCHEMA,
            "timeout_ms": int(timeout * 1000),
        }
        if launch_authority is not None:
            config_value["launch_authority"] = launch_authority.to_dict()
        config_payload = _encode_config_frame(config_value)
        try:
            local, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        except Exception:
            raise MonitorIPCError(MonitorIPCErrorCategory.SPAWN_FAILED) from None
        process: subprocess.Popen[bytes] | None = None
        commit_sent = False
        try:
            argv = [
                executable or sys.executable,
                "-m",
                "palimpsest_local.oci_monitor_ipc",
                "--private-child-v2",
                str(directory_fd),
                str(child.fileno()),
            ]
            environment = {"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}
            try:
                process = popen_factory(
                    argv,
                    close_fds=True,
                    pass_fds=(directory_fd, child.fileno(), *authority_fds),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    start_new_session=True,
                )
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.SPAWN_FAILED) from None
            child.close()
            local.settimeout(timeout)
            _send_all(local, config_payload)
            if _recv_frame(local) != {"kind": "bound", "schema": _SPAWN_SCHEMA}:
                raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
            socket_name = _socket_name_for_generation(identity.generation)
            metadata = _visible_socket(directory_fd, socket_name)
            endpoint_identity = (metadata.st_dev, metadata.st_ino)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(timeout)
                _connect_socket(channel, directory_fd, socket_name)
                writer = MonitorProcessIdentity(
                    process.pid, _read_boot_id_for_spawn(), _read_start_ticks_for_spawn(process.pid)
                )
                _authorize_peer(channel, writer, identity.owner_uid)
                _send_frame(channel, _spawn_message("prepare", identity, nonce, parent))
                prepared = _recv_frame(channel)
                _validate_spawn_message(prepared, "prepared", identity, nonce, writer, endpoint_identity)
                current = _visible_socket(directory_fd, socket_name)
                if (current.st_dev, current.st_ino) != endpoint_identity:
                    raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
                loaded_prepared = _read_preactivation_journal(directory_fd, identity)
                assert loaded_prepared is not None
                prepared_snapshot = loaded_prepared[0]
                if (
                    prepared_snapshot.phase != "prepared"
                    or prepared_snapshot.revision < 2
                    or prepared_snapshot.nonce_digest != _nonce_digest(nonce)
                    or prepared_snapshot.writer != writer
                    or prepared_snapshot.socket_name != socket_name
                    or (prepared_snapshot.socket_device, prepared_snapshot.socket_inode) != endpoint_identity
                ):
                    raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
                _require_exact_journal_snapshot(directory_fd, prepared_snapshot)
                _send_frame(channel, _spawn_message("commit", identity, nonce, parent))
                commit_sent = True
                _validate_spawn_message(
                    _recv_frame(channel),
                    "committed",
                    identity,
                    nonce,
                    writer,
                    endpoint_identity,
                )
                _require_exact_journal_snapshot(
                    directory_fd,
                    MonitorPreactivationJournalSnapshot(
                        identity,
                        _nonce_digest(nonce),
                        "committed",
                        prepared_snapshot.revision + 1,
                        writer,
                        socket_name,
                        *endpoint_identity,
                    ),
                )
                if launch_authority is not None:
                    _send_frame(channel, _spawn_message("activate", identity, nonce, parent, endpoint_identity))
                    _validate_spawn_message(
                        _recv_frame(channel),
                        "launch-accepted",
                        identity,
                        nonce,
                        writer,
                        endpoint_identity,
                    )
            endpoint = MonitorExecEndpoint(identity, writer, *endpoint_identity, socket_name)
            local.close()
            return MonitorExecHandle(directory_fd, endpoint, process, timeout)
        except BaseException:
            if process is not None and not commit_sent:
                _terminate_failed_child(process, local)
            raise
        finally:
            try:
                child.close()
            except OSError:
                pass
            try:
                local.close()
            except OSError:
                pass


def _read_boot_id_for_spawn() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
            value = stream.read().strip()
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED) from None
    if not _canonical_uuid(value):
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
    return value


def _read_start_ticks_for_spawn(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
            content = stream.read()
        ticks = _parse_proc_start_ticks(content, pid)
        if ticks <= 0:
            raise ValueError
        return ticks
    except (OSError, ValueError):
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED) from None


def request_monitor(
    directory_fd: int,
    endpoint: MonitorExecEndpoint,
    operation: MonitorIPCOperation,
    *,
    timeout: float = 5.0,
) -> MonitorIPCReply:
    """Send an authenticated private request.

    STOP acceptance means one coalesced request is queued for the worker, not
    that its guest signal has been delivered or the VM has terminated.
    """
    if (
        not isinstance(endpoint, MonitorExecEndpoint)
        or not isinstance(operation, MonitorIPCOperation)
        or isinstance(timeout, bool)
        or not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    _validate_directory(directory_fd)
    assert endpoint.socket_name is not None
    metadata = _visible_socket(directory_fd, endpoint.socket_name)
    if (metadata.st_dev, metadata.st_ino) != (endpoint.socket_device, endpoint.socket_inode):
        raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
    client = current_process_identity()
    request_id = str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        _connect_socket(channel, directory_fd, endpoint.socket_name)
        _authorize_peer(channel, endpoint.writer, endpoint.identity.owner_uid)
        current = _visible_socket(directory_fd, endpoint.socket_name)
        if (current.st_dev, current.st_ino) != (endpoint.socket_device, endpoint.socket_inode):
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
        _send_frame(channel, _request_message(operation, endpoint.identity, client, request_id))
        value = _recv_frame(channel)
    expected = _response_message(operation, endpoint.identity, endpoint.writer, request_id)
    if (
        operation is MonitorIPCOperation.DESCRIBE
        and type(value.get("state")) is str
        and value["state"] in _DESCRIBE_STATES
    ):
        expected["state"] = value["state"]
    elif operation is MonitorIPCOperation.SHUTDOWN and value.get("state") == "shutdown-refused":
        expected["state"] = "shutdown-refused"
    elif operation is MonitorIPCOperation.STOP and value.get("state") in (
        "stop-accepted",
        "stop-terminal",
        "stop-refused",
    ):
        expected["state"] = value["state"]
    if value != expected:
        raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
    if expected["state"] == "shutdown-refused":
        raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
    return MonitorIPCReply(operation, expected["state"], endpoint.writer)


def shutdown_monitor_exec(
    directory_fd: int,
    endpoint: MonitorExecEndpoint,
    *,
    timeout: float = 5.0,
) -> MonitorPreactivationJournalSnapshot:
    """Retire inert or completed-terminal transport without stopping a VM.

    Inert retirement verifies abandonment; completed launch retirement keeps
    the exact durable terminal journal and removes only its owned socket.
    """

    loaded = _read_preactivation_journal(directory_fd, endpoint.identity)
    assert loaded is not None
    if (_has_activation_evidence(loaded[0]) and loaded[0].phase != "terminal") or loaded[0].phase == "control-lost":
        raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
    if loaded[0].phase == "terminal":
        try:
            live = probe_process_liveness(loaded[0].writer)
        except Exception:
            live = ProcessLiveness.UNKNOWN
        if live is not ProcessLiveness.LIVE:
            raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
    reply = request_monitor(directory_fd, endpoint, MonitorIPCOperation.SHUTDOWN, timeout=timeout)
    if reply.state != "shutting-down":
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    deadline = time.monotonic() + timeout
    while True:
        loaded = _read_preactivation_journal(directory_fd, endpoint.identity)
        assert loaded is not None
        snapshot = loaded[0]
        if snapshot.phase == "control-lost":
            raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
        if snapshot.phase in {"abandoned", "terminal"}:
            assert endpoint.socket_name is not None
            try:
                os.stat(endpoint.socket_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return snapshot
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
            if snapshot.phase == "abandoned":
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
        if time.monotonic() >= deadline:
            raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT)
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def discover_monitor_exec(
    directory_fd: int,
    binding: MonitorPreActivationBinding,
    *,
    timeout: float = 5.0,
    liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
) -> MonitorExecEndpoint:
    """Discover an exact live binding across monotonic activation revisions."""

    if not isinstance(binding, MonitorPreActivationBinding) or not callable(liveness_probe):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    loaded = _read_preactivation_journal(directory_fd, binding)
    assert loaded is not None
    snapshot, _content = loaded
    try:
        liveness = liveness_probe(snapshot.writer)
    except Exception:
        liveness = ProcessLiveness.UNKNOWN
    if liveness is ProcessLiveness.STALE:
        raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
    if liveness is not ProcessLiveness.LIVE:
        raise MonitorIPCError(MonitorIPCErrorCategory.WRITER_UNKNOWN)
    if snapshot.phase not in _DISCOVERABLE_PHASES:
        raise MonitorIPCError(MonitorIPCErrorCategory.NOT_COMMITTED)
    endpoint = snapshot.endpoint
    reply = request_monitor(directory_fd, endpoint, MonitorIPCOperation.DESCRIBE, timeout=timeout)
    if reply.state not in _DISCOVERABLE_PHASES | {"launch-pending", "launch-failed"}:
        raise MonitorIPCError(MonitorIPCErrorCategory.NOT_COMMITTED)
    after = _read_preactivation_journal(directory_fd, endpoint.identity)
    assert after is not None
    # The worker may advance while DESCRIBE is in flight. Only monotonic
    # revisions with the exact same process/socket/identity are acceptable.
    latest = after[0]
    order = {phase: index for index, phase in enumerate(("committed", "activating", "active", "ready", "terminal"))}
    described_phase = "committed" if reply.state in {"launch-pending", "launch-failed"} else reply.state
    if (
        latest.phase not in _DISCOVERABLE_PHASES
        or latest.endpoint != endpoint
        or latest.nonce_digest != snapshot.nonce_digest
        or latest.revision < snapshot.revision
        or not order[snapshot.phase] <= order[described_phase] <= order[latest.phase]
        or latest.revision - snapshot.revision != order[latest.phase] - order[snapshot.phase]
        or (snapshot.active_binding is not None and latest.active_binding != snapshot.active_binding)
        or (latest.revision == snapshot.revision and after != loaded)
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_JOURNAL)
    return endpoint


def _remove_exact_quarantined_socket(
    directory_fd: int,
    quarantine: str,
    expected: tuple[int, int],
) -> bool:
    """Finish an interrupted deterministic quarantine cleanup."""

    descriptor = -1
    try:
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            return False
        descriptor = os.open(
            quarantine,
            path_flag | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        visible = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat_module.S_ISSOCK(opened.st_mode)
            or not stat_module.S_ISSOCK(visible.st_mode)
            or opened.st_uid != os.geteuid()
            or visible.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or visible.st_nlink != 1
            or stat_module.S_IMODE(opened.st_mode) != 0o600
            or stat_module.S_IMODE(visible.st_mode) != 0o600
            or _journal_identity(opened) != expected
            or _journal_identity(visible) != expected
        ):
            return False
        os.unlink(quarantine, dir_fd=directory_fd)
        after_unlink = os.fstat(descriptor)
        if _journal_identity(after_unlink) != expected or after_unlink.st_nlink != 0:
            return False
        os.fsync(directory_fd)
        try:
            os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                return False


def _cleanup_stale_socket(
    directory_fd: int,
    snapshot: MonitorPreactivationJournalSnapshot,
) -> bool:
    if _has_activation_evidence(snapshot) or snapshot.phase == "control-lost":
        return False
    if snapshot.socket_device is None or snapshot.socket_inode is None:
        # CLAIMING deliberately precedes bind, so no durable inode authority
        # exists for a pathname observed after a crash. Absence is safe; any
        # object is ambiguous and must be preserved as control-lost evidence.
        try:
            os.stat(snapshot.socket_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.fsync(directory_fd)
                return True
            except OSError:
                return False
        except OSError:
            return False
        return False
    descriptor = -1
    expected = (snapshot.socket_device, snapshot.socket_inode)
    quarantine = _socket_quarantine_name(snapshot.socket_name, expected)
    try:
        os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        try:
            os.stat(snapshot.socket_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _remove_exact_quarantined_socket(directory_fd, quarantine, expected)
        except OSError:
            return False
        return False
    try:
        path_flag = getattr(os, "O_PATH", None)
        if path_flag is None:
            raise OSError
        descriptor = os.open(
            snapshot.socket_name,
            path_flag | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        try:
            os.fsync(directory_fd)
            return True
        except OSError:
            return False
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        visible = os.stat(snapshot.socket_name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat_module.S_ISSOCK(opened.st_mode)
            or not stat_module.S_ISSOCK(visible.st_mode)
            or opened.st_uid != os.geteuid()
            or visible.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or visible.st_nlink != 1
            or stat_module.S_IMODE(opened.st_mode) != 0o600
            or stat_module.S_IMODE(visible.st_mode) != 0o600
            or _journal_identity(opened) != expected
            or _journal_identity(visible) != expected
        ):
            return False
        _rename_noreplace(directory_fd, snapshot.socket_name, quarantine)
        moved = os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        held = os.fstat(descriptor)
        if (
            not stat_module.S_ISSOCK(moved.st_mode)
            or _journal_identity(moved) != expected
            or _journal_identity(held) != expected
        ):
            return False
        os.unlink(quarantine, dir_fd=directory_fd)
        after_unlink = os.fstat(descriptor)
        if _journal_identity(after_unlink) != expected or after_unlink.st_nlink != 0:
            return False
        os.fsync(directory_fd)
        try:
            os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                return False


def reconcile_stale_monitor_exec(
    directory_fd: int,
    binding: MonitorPreActivationBinding,
    *,
    current_process: Callable[[], MonitorProcessIdentity] = current_process_identity,
    liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
) -> MonitorPreactivationJournalSnapshot:
    """Take stale recovery ownership and remove only the exact recorded socket."""

    if (
        not isinstance(binding, MonitorPreActivationBinding)
        or not callable(current_process)
        or not callable(liveness_probe)
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    loaded = _read_preactivation_journal(directory_fd, binding)
    assert loaded is not None
    snapshot, content = loaded
    if snapshot.phase == "abandoned":
        try:
            os.stat(snapshot.socket_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return snapshot
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST) from None
        raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
    if snapshot.phase == "control-lost" or _has_activation_evidence(snapshot):
        raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
    try:
        liveness = liveness_probe(snapshot.writer)
    except Exception:
        liveness = ProcessLiveness.UNKNOWN
    if liveness is ProcessLiveness.LIVE:
        raise MonitorIPCError(MonitorIPCErrorCategory.WRITER_LIVE)
    if liveness is not ProcessLiveness.STALE:
        raise MonitorIPCError(MonitorIPCErrorCategory.WRITER_UNKNOWN)
    try:
        writer = current_process()
    except Exception:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER) from None
    if not isinstance(writer, MonitorProcessIdentity):
        raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
    lease = _PreactivationJournalLease.adopt_stale(
        directory_fd,
        snapshot.identity,
        snapshot,
        content,
        writer,
        liveness_probe,
    )
    try:
        if _cleanup_stale_socket(lease._directory_fd, lease.snapshot):
            return lease.mark_abandoned()
        lease.mark_control_lost()
        raise MonitorIPCError(MonitorIPCErrorCategory.CONTROL_LOST)
    finally:
        lease.close()


def _entrypoint(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] != "--private-child-v2":
        return 64
    try:
        directory_fd = int(argv[2])
        config_fd = int(argv[3])
    except ValueError:
        return 64
    return _child_main(directory_fd, config_fd)


if __name__ == "__main__":
    # Runtime imports must share canonical dataclass/lease identities, not a
    # second set of classes created under Python's __main__ module alias.
    from palimpsest_local.oci_monitor_ipc import _entrypoint as _canonical_entrypoint

    raise SystemExit(_canonical_entrypoint(sys.argv))


__all__ = [
    "MonitorExecEndpoint",
    "MonitorExecHandle",
    "MonitorExecIdentity",
    "MonitorPreactivationJournalSnapshot",
    "MonitorPreActivationBinding",
    "MonitorIPCError",
    "MonitorIPCErrorCategory",
    "MonitorIPCOperation",
    "MonitorIPCReply",
    "discover_monitor_exec",
    "reconcile_stale_monitor_exec",
    "request_monitor",
    "shutdown_monitor_exec",
    "spawn_monitor_exec",
]
