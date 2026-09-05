"""Production-inert fresh-exec and IPC foundation for a future OCI monitor.

This module owns no libvirt connection, lifecycle key, domain, or MonitorLease.
The child can therefore prove a fresh-exec process and a reciprocal local peer
binding before a future slice gives that child any VM mutation capability.
The returned endpoint is only a canonical in-memory handoff; this module does
not durably publish it and therefore makes no restart-discovery claim.
"""

from __future__ import annotations

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
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import PalimpsestError
from .oci_monitor import (
    MonitorProcessIdentity,
    ProcessLiveness,
    _parse_proc_start_ticks,
    current_process_identity,
    probe_process_liveness,
)
from .runtime_types import DispatchKey, ExistingRunRecord, RuntimeBackend, RuntimeKind

_SOCKET_NAME = "oci-monitor-ipc-v1.sock"
_CONFIG_SCHEMA = "palimpsest.oci-monitor-exec-config.v1"
_SPAWN_SCHEMA = "palimpsest.oci-monitor-spawn.v1"
_REQUEST_SCHEMA = "palimpsest.oci-monitor-ipc-request.v1"
_RESPONSE_SCHEMA = "palimpsest.oci-monitor-ipc-response.v1"
_RECEIPT_SCHEMA = "palimpsest.oci-monitor-exec-receipt.v1"
_PREACTIVATION_SCHEMA = "palimpsest.oci-monitor-preactivation.v1"
_LIFECYCLE_PROTOCOL = "palimpsest.oci-lifecycle-control.v2"
_FRAME_LENGTH = struct.Struct(">I")
_PEER_CREDENTIALS = struct.Struct("3i")
_MAX_FRAME_BYTES = 16 * 1024
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 30.0
_DIGEST_RE = __import__("re").compile(r"^sha256:[0-9a-f]{64}$")
_NONCE_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
_SPAWN_LOCK = threading.Lock()


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

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, MonitorExecIdentity)
            or not isinstance(self.writer, MonitorProcessIdentity)
            or type(self.socket_device) is not int
            or self.socket_device < 0
            or type(self.socket_inode) is not int
            or self.socket_inode <= 0
        ):
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.identity.binding.to_dict(),
            "binding_digest": self.identity.binding_digest,
            "generation": self.identity.generation,
            "schema": _RECEIPT_SCHEMA,
            "socket": {"device": self.socket_device, "inode": self.socket_inode},
            "writer": self.writer.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict()) + b"\n"

    @classmethod
    def from_dict(cls, value: object) -> MonitorExecEndpoint:
        expected = {"binding", "binding_digest", "generation", "schema", "socket", "writer"}
        if type(value) is not dict or set(value) != expected or value.get("schema") != _RECEIPT_SCHEMA:
            raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
        socket_value = value.get("socket")
        if type(socket_value) is not dict or set(socket_value) != {"device", "inode"}:
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
class MonitorIPCReply:
    operation: MonitorIPCOperation
    state: str
    writer: MonitorProcessIdentity


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


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


def _recv_frame(channel: socket.socket) -> dict[str, Any]:
    (size,) = _FRAME_LENGTH.unpack(_recv_exact(channel, _FRAME_LENGTH.size))
    if not 1 <= size <= _MAX_FRAME_BYTES:
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_FRAME)
    return _decode_frame(_recv_exact(channel, size))


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


def _socket_address(directory_fd: int) -> str:
    address = f"/proc/self/fd/{directory_fd}/{_SOCKET_NAME}"
    if len(os.fsencode(address)) > 107 or "\x00" in address:
        raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY)
    return address


def _connect_socket(channel: socket.socket, directory_fd: int) -> None:
    try:
        channel.connect(_socket_address(directory_fd))
    except TimeoutError:
        raise MonitorIPCError(MonitorIPCErrorCategory.TIMEOUT) from None
    except OSError:
        raise MonitorIPCError(MonitorIPCErrorCategory.CLOSED) from None


def _visible_socket(directory_fd: int) -> os.stat_result:
    try:
        metadata = os.stat(_SOCKET_NAME, dir_fd=directory_fd, follow_symlinks=False)
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
    def __init__(self, directory_fd: int) -> None:
        _validate_directory(directory_fd)
        try:
            self._directory_fd = os.dup(directory_fd)
        except OSError:
            raise MonitorIPCError(MonitorIPCErrorCategory.UNSAFE_DIRECTORY) from None
        self._listener: socket.socket | None = None
        self._identity: tuple[int, int] | None = None
        try:
            try:
                os.stat(_SOCKET_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_COLLISION) from None
            else:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_COLLISION)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(_socket_address(self._directory_fd))
            created = os.stat(_SOCKET_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
            if not stat_module.S_ISSOCK(created.st_mode) or created.st_uid != os.geteuid():
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            self._identity = (created.st_dev, created.st_ino)
            os.chmod(_SOCKET_NAME, 0o600, dir_fd=self._directory_fd, follow_symlinks=False)
            metadata = _visible_socket(self._directory_fd)
            if (metadata.st_dev, metadata.st_ino) != self._identity:
                raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
            listener.listen(8)
            self._listener = listener
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
        metadata = _visible_socket(self._directory_fd)
        if (metadata.st_dev, metadata.st_ino) != self.identity:
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)

    def close(self, *, ignore_changed: bool = False) -> None:
        if self._directory_fd < 0:
            return
        changed = False
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._identity is not None:
            try:
                metadata = os.stat(_SOCKET_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != self._identity or not stat_module.S_ISSOCK(metadata.st_mode):
                    changed = True
                else:
                    os.unlink(_SOCKET_NAME, dir_fd=self._directory_fd)
            except FileNotFoundError:
                changed = True
            except OSError:
                changed = True
        try:
            os.close(self._directory_fd)
        except OSError:
            pass
        self._directory_fd = -1
        if changed and not ignore_changed:
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)


def _spawn_message(
    kind: str, identity: MonitorExecIdentity, nonce: str, writer: MonitorProcessIdentity
) -> dict[str, Any]:
    return {
        "binding_digest": identity.binding_digest,
        "generation": identity.generation,
        "kind": kind,
        "nonce": nonce,
        "run_id": identity.run_id,
        "schema": _SPAWN_SCHEMA,
        "writer": _process_to_dict(writer),
    }


def _validate_spawn_message(
    value: object,
    kind: str,
    identity: MonitorExecIdentity,
    nonce: str,
    writer: MonitorProcessIdentity,
) -> None:
    if value != _spawn_message(kind, identity, nonce, writer):
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
) -> dict[str, Any]:
    state = {
        MonitorIPCOperation.DESCRIBE: "committed",
        MonitorIPCOperation.PING: "pong",
        MonitorIPCOperation.SHUTDOWN: "shutting-down",
    }[operation]
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


def _serve_committed(
    bound: _BoundMonitorSocket,
    identity: MonitorExecIdentity,
    writer: MonitorProcessIdentity,
    timeout: float,
) -> None:
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
                _send_frame(channel, _response_message(operation, identity, writer, request_id))
            except MonitorIPCError:
                continue
        if operation is MonitorIPCOperation.SHUTDOWN:
            return


def _identity_from_config(value: dict[str, Any]) -> tuple[MonitorExecIdentity, MonitorProcessIdentity, str, float]:
    expected = {"binding", "generation", "nonce", "parent", "schema", "timeout_ms"}
    if set(value) != expected or value.get("schema") != _CONFIG_SCHEMA:
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
    try:
        config.settimeout(_MAX_TIMEOUT_SECONDS)
        identity, parent, nonce, timeout = _identity_from_config(_recv_frame(config))
        if parent.pid == os.getpid():
            raise MonitorIPCError(MonitorIPCErrorCategory.UNAUTHORIZED_PEER)
        writer = current_process_identity()
        bound = _BoundMonitorSocket(directory_fd)
        _send_frame(config, {"kind": "bound", "schema": _SPAWN_SCHEMA})
        channel = _accept_precommit(bound, config, timeout)
        with channel:
            channel.settimeout(timeout)
            _authorize_peer(channel, parent, identity.owner_uid)
            prepared = _recv_frame(channel)
            _validate_spawn_message(prepared, "prepare", identity, nonce, parent)
            _send_frame(channel, _spawn_message("prepared", identity, nonce, writer))
            committed = _recv_frame(channel)
            _validate_spawn_message(committed, "commit", identity, nonce, parent)
            bound.validate()
            _send_frame(channel, _spawn_message("committed", identity, nonce, writer))
        config.close()
        _serve_committed(bound, identity, writer, timeout)
        bound.close()
        return 0
    except Exception as failure:
        error = (
            failure if isinstance(failure, MonitorIPCError) else MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
        )
        try:
            _send_frame(config, {"category": error.category.value, "kind": "error", "schema": _SPAWN_SCHEMA})
        except MonitorIPCError:
            pass
        if bound is not None:
            try:
                bound.close()
            except MonitorIPCError:
                pass
        return 70
    finally:
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
) -> MonitorExecHandle:
    """Fresh-exec a capability-free monitor and commit reciprocal IPC identity."""

    if (
        not isinstance(identity, MonitorExecIdentity)
        or isinstance(timeout, bool)
        or not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    _validate_directory(directory_fd)
    _require_spawn_boundary()
    with _SPAWN_LOCK:
        try:
            parent = current_process_identity()
            nonce = os.urandom(32).hex()
            local, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        except Exception:
            raise MonitorIPCError(MonitorIPCErrorCategory.SPAWN_FAILED) from None
        process: subprocess.Popen[bytes] | None = None
        try:
            argv = [
                executable or sys.executable,
                "-m",
                "palimpsest_local.oci_monitor_ipc",
                "--private-child-v1",
                str(directory_fd),
                str(child.fileno()),
            ]
            environment = {"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}
            try:
                process = popen_factory(
                    argv,
                    close_fds=True,
                    pass_fds=(directory_fd, child.fileno()),
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
            _send_frame(
                local,
                {
                    "binding": identity.binding.to_dict(),
                    "generation": identity.generation,
                    "nonce": nonce,
                    "parent": _process_to_dict(parent),
                    "schema": _CONFIG_SCHEMA,
                    "timeout_ms": int(timeout * 1000),
                },
            )
            if _recv_frame(local) != {"kind": "bound", "schema": _SPAWN_SCHEMA}:
                raise MonitorIPCError(MonitorIPCErrorCategory.CHILD_FAILED)
            metadata = _visible_socket(directory_fd)
            endpoint_identity = (metadata.st_dev, metadata.st_ino)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(timeout)
                _connect_socket(channel, directory_fd)
                writer = MonitorProcessIdentity(
                    process.pid, _read_boot_id_for_spawn(), _read_start_ticks_for_spawn(process.pid)
                )
                _authorize_peer(channel, writer, identity.owner_uid)
                _send_frame(channel, _spawn_message("prepare", identity, nonce, parent))
                prepared = _recv_frame(channel)
                _validate_spawn_message(prepared, "prepared", identity, nonce, writer)
                current = _visible_socket(directory_fd)
                if (current.st_dev, current.st_ino) != endpoint_identity:
                    raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
                _send_frame(channel, _spawn_message("commit", identity, nonce, parent))
                _validate_spawn_message(_recv_frame(channel), "committed", identity, nonce, writer)
            endpoint = MonitorExecEndpoint(identity, writer, *endpoint_identity)
            local.close()
            return MonitorExecHandle(directory_fd, endpoint, process, timeout)
        except BaseException:
            if process is not None:
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
    if (
        not isinstance(endpoint, MonitorExecEndpoint)
        or not isinstance(operation, MonitorIPCOperation)
        or isinstance(timeout, bool)
        or not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise MonitorIPCError(MonitorIPCErrorCategory.INVALID_IDENTITY)
    _validate_directory(directory_fd)
    metadata = _visible_socket(directory_fd)
    if (metadata.st_dev, metadata.st_ino) != (endpoint.socket_device, endpoint.socket_inode):
        raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
    client = current_process_identity()
    request_id = str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        _connect_socket(channel, directory_fd)
        _authorize_peer(channel, endpoint.writer, endpoint.identity.owner_uid)
        current = _visible_socket(directory_fd)
        if (current.st_dev, current.st_ino) != (endpoint.socket_device, endpoint.socket_inode):
            raise MonitorIPCError(MonitorIPCErrorCategory.SOCKET_CHANGED)
        _send_frame(channel, _request_message(operation, endpoint.identity, client, request_id))
        value = _recv_frame(channel)
    expected = _response_message(operation, endpoint.identity, endpoint.writer, request_id)
    if value != expected:
        raise MonitorIPCError(MonitorIPCErrorCategory.BINDING_MISMATCH)
    return MonitorIPCReply(operation, expected["state"], endpoint.writer)


def _entrypoint(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] != "--private-child-v1":
        return 64
    try:
        directory_fd = int(argv[2])
        config_fd = int(argv[3])
    except ValueError:
        return 64
    return _child_main(directory_fd, config_fd)


if __name__ == "__main__":
    raise SystemExit(_entrypoint(sys.argv))


__all__ = [
    "MonitorExecEndpoint",
    "MonitorExecHandle",
    "MonitorExecIdentity",
    "MonitorPreActivationBinding",
    "MonitorIPCError",
    "MonitorIPCErrorCategory",
    "MonitorIPCOperation",
    "MonitorIPCReply",
    "request_monitor",
    "spawn_monitor_exec",
]
