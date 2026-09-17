"""Durable, local-only OCI exec completion records.

This module deliberately has no runtime-state or monitor dependencies.  Records
are historical local claims; loading one never recreates live-run authority.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import struct
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import StateError
from .runtime_types import ProcessExit, ProcessExitCategory

SCHEMA = "palimpsest.oci-exec-record"
VERSION = 1
MAX_ARTIFACT_BYTES = 4096

_FINAL_NAMES = ("pending.json", "observed.json", "confirmed.json")
_PHASES = ("pending", "observed", "confirmed")
_MAX_PATH_BYTES = 4096
_MAX_COMPONENT_BYTES = 255
_MAX_COMPONENTS = 64
_MAX_DIRECTORY_ENTRIES = 16
_MAX_TEMPORARIES = 8
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 64
_MAX_INTEGER_DIGITS = 20
_TEMPORARY = re.compile(r"^\.(pending|observed|confirmed)\.[0-9a-f]{32}\.tmp$")
_GUIDANCE = (
    "Local historical metadata only; it grants no run or monitor authority and is not proof that replay is safe."
)


class OCIExecRecordError(StateError):
    """Fixed, path-free failure for record storage or inspection."""

    _MESSAGES = {
        "reserve": "OCI exec record reservation failed",
        "pending": "OCI exec pending record publication failed",
        "observed": "OCI exec observed record publication failed",
        "confirmed": "OCI exec confirmed record publication failed",
        "read": "OCI exec record inspection failed",
        "changed": "OCI exec record changed during inspection",
        "closed": "OCI exec record writer is closed",
        "forked": "OCI exec record writer belongs to another process",
        "poisoned": "OCI exec record writer publication state is uncertain",
    }

    def __init__(self, stage: str) -> None:
        message = self._MESSAGES.get(stage)
        if message is None:
            raise ValueError("invalid OCI exec record error stage")
        self.stage = stage
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class OCIExecRecordSnapshot:
    """Validated immutable historical snapshot, never live-session authority."""

    record_id: str
    phase: str
    observation: Any | None

    def __post_init__(self) -> None:
        _validate_record_id(self.record_id)
        if self.phase not in _PHASES:
            raise ValueError("OCI exec record phase is invalid")
        if (self.phase == "pending") != (self.observation is None):
            raise ValueError("OCI exec record snapshot is inconsistent")
        if self.observation is not None:
            _validate_observation(
                self.observation, acknowledgement="confirmed" if self.phase == "confirmed" else "unconfirmed"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": VERSION,
            "record_id": self.record_id,
            "phase": self.phase,
            "observation": None if self.observation is None else _observation_to_dict(self.observation),
            "classification": "local-historical-metadata",
            "guidance": _GUIDANCE,
        }


@dataclass(slots=True)
class _DirectoryChain:
    fds: list[int]
    names: list[str]
    secure: bool = False
    private_tail: int = 0

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def revalidate(self) -> None:
        uid = os.geteuid()
        private_start = len(self.fds) - self.private_tail
        if self.secure:
            _check_directory(os.fstat(self.fds[0]), uid=uid, root_or_user=True)
            _check_no_acl(self.fds[0])
        for index, name in enumerate(self.names, start=1):
            visible = os.stat(name, dir_fd=self.fds[index - 1], follow_symlinks=False)
            held = os.fstat(self.fds[index])
            if not stat.S_ISDIR(visible.st_mode) or not _same_inode(visible, held):
                raise OSError(errno.ESTALE, "directory binding changed")
            if self.secure:
                immediate = index >= private_start
                _check_directory(visible, uid=uid, root_or_user=True, immediate=immediate)
                _check_directory(held, uid=uid, root_or_user=True, immediate=immediate)
                _check_no_acl(self.fds[index])

    def close(self) -> None:
        while self.fds:
            fd = self.fds.pop()
            try:
                os.close(fd)
            except BaseException:
                pass


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _path_components(path: Path | str) -> tuple[str, ...]:
    try:
        raw = os.fspath(path)
    except TypeError:
        raise OSError(errno.EINVAL, "invalid path") from None
    if type(raw) is not str or not raw.startswith("/") or raw == "/" or "\x00" in raw:
        raise OSError(errno.EINVAL, "invalid path")
    try:
        encoded = raw.encode(sys.getfilesystemencoding(), "strict")
    except UnicodeError:
        raise OSError(errno.EINVAL, "invalid path") from None
    components = raw.split("/")[1:]
    if (
        len(encoded) > _MAX_PATH_BYTES
        or not 1 <= len(components) <= _MAX_COMPONENTS
        or any(
            component in {"", ".", ".."}
            or len(component.encode(sys.getfilesystemencoding(), "strict")) > _MAX_COMPONENT_BYTES
            for component in components
        )
    ):
        raise OSError(errno.EINVAL, "invalid path")
    return tuple(components)


def _require_linux() -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "unsupported platform")


def _read_xattr_bounded(fd: int, name: str) -> bytes:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        fgetxattr = library.fgetxattr
    except AttributeError:
        raise OSError(errno.ENOSYS, "fgetxattr is unavailable") from None
    fgetxattr.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t)
    fgetxattr.restype = ctypes.c_ssize_t
    buffer = ctypes.create_string_buffer(MAX_ARTIFACT_BYTES + 1)
    result = fgetxattr(fd, name.encode("ascii"), buffer, len(buffer))
    if result < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if result > MAX_ARTIFACT_BYTES:
        raise OSError(errno.E2BIG, "extended attribute is too large")
    return buffer.raw[:result]


def _check_no_acl(fd: int) -> None:
    names = os.listxattr(fd)
    if len(names) > 64 or sum(len(name.encode("utf-8", "strict")) for name in names) > MAX_ARTIFACT_BYTES:
        raise OSError(errno.E2BIG, "extended attributes are unbounded")
    if "system.posix_acl_default" in names:
        raise OSError(errno.EACCES, "default ACL is not permitted")
    if "system.posix_acl_access" not in names:
        return
    payload = _read_xattr_bounded(fd, "system.posix_acl_access")
    if len(payload) != 28 or struct.unpack_from("<I", payload)[0] != 2:
        raise OSError(errno.EACCES, "extended ACL is not permitted")
    entries = [struct.unpack_from("<HHI", payload, offset) for offset in (4, 12, 20)]
    metadata = os.fstat(fd)
    mode = stat.S_IMODE(metadata.st_mode)
    expected = [(1, (mode >> 6) & 7, 0xFFFFFFFF), (4, (mode >> 3) & 7, 0xFFFFFFFF), (32, mode & 7, 0xFFFFFFFF)]
    if entries != expected:
        raise OSError(errno.EACCES, "extended ACL is not permitted")


def _check_directory(metadata: os.stat_result, *, uid: int, root_or_user: bool, immediate: bool = False) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.ENOTDIR, "not a directory")
    if immediate:
        if metadata.st_uid != uid or mode != 0o700:
            raise OSError(errno.EACCES, "private parent is unsafe")
        return
    if root_or_user and metadata.st_uid not in {0, uid}:
        raise OSError(errno.EACCES, "directory owner is unsafe")
    if mode & 0o022 and not (metadata.st_uid == 0 and mode & stat.S_ISVTX):
        raise OSError(errno.EACCES, "directory mode is unsafe")


def _open_chain(path: Path | str, *, secure: bool) -> _DirectoryChain:
    components = _path_components(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_NONBLOCK
    root_fd = os.open("/", flags)
    chain = _DirectoryChain([root_fd], [], secure=secure)
    uid = os.geteuid()
    try:
        if secure:
            _check_directory(os.fstat(root_fd), uid=uid, root_or_user=True)
            _check_no_acl(root_fd)
        for component in components:
            child = os.open(component, flags, dir_fd=chain.fd)
            chain.fds.append(child)
            chain.names.append(component)
            held = os.fstat(child)
            visible = os.stat(component, dir_fd=chain.fds[-2], follow_symlinks=False)
            if not _same_inode(held, visible):
                raise OSError(errno.ESTALE, "directory binding changed")
            if secure:
                _check_directory(held, uid=uid, root_or_user=True)
                _check_no_acl(child)
        chain.revalidate()
        return chain
    except BaseException:
        chain.close()
        raise


def _validate_record_id(value: Any) -> str:
    if type(value) is not str or len(value) != 36:
        raise ValueError("record ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("record ID is invalid") from None
    if str(parsed) != value or parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError("record ID is invalid")
    return value


def _observation_type():
    # Lazy so the session can later depend on this storage module without a cycle.
    from .oci_exec_session import OCIExecCompletionObservation

    return OCIExecCompletionObservation


def _validate_observation(value: Any, *, acknowledgement: str):
    observation_type = _observation_type()
    if type(value) is not observation_type or value.acknowledgement != acknowledgement:
        raise ValueError("OCI exec record observation is invalid")
    value.__post_init__()
    if value.terminal is not None:
        value.terminal.__post_init__()
    return value


def _terminal_to_dict(value: ProcessExit | None) -> dict[str, Any] | None:
    if value is None:
        return None
    value.__post_init__()
    return {
        "returncode": value.returncode,
        "exit_code": value.exit_code,
        "signal_number": value.signal_number,
        "category": value.category.value,
    }


def _observation_to_dict(value: Any) -> dict[str, Any]:
    _validate_observation(value, acknowledgement=value.acknowledgement)
    return {
        "terminal": _terminal_to_dict(value.terminal),
        "reason": value.reason,
        "stdout_bytes": value.stdout_bytes,
        "stderr_bytes": value.stderr_bytes,
        "acknowledgement": value.acknowledgement,
    }


def _artifact_dict(record_id: str, phase: str, observation: Any | None) -> dict[str, Any]:
    _validate_record_id(record_id)
    if phase not in _PHASES:
        raise ValueError("OCI exec record phase is invalid")
    if phase == "pending":
        if observation is not None:
            raise ValueError("pending record cannot contain an observation")
    else:
        observation = _validate_observation(
            observation, acknowledgement="confirmed" if phase == "confirmed" else "unconfirmed"
        )
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "record_id": record_id,
        "phase": phase,
        "observation": None if observation is None else _observation_to_dict(observation),
    }


def _encode_artifact(record_id: str, phase: str, observation: Any | None) -> bytes:
    payload = (
        json.dumps(
            _artifact_dict(record_id, phase, observation),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("OCI exec record artifact is too large")
    return payload


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_integer(value: str) -> int:
    if len(value.lstrip("-")) > _MAX_INTEGER_DIGITS:
        raise ValueError("JSON integer is too large")
    return int(value)


def _bound_json(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    count[0] += 1
    if depth > _MAX_JSON_DEPTH or count[0] > _MAX_JSON_NODES:
        raise ValueError("JSON structure is too large")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or len(key) > 64:
                raise ValueError("JSON key is invalid")
            _bound_json(item, depth=depth + 1, count=count)
    elif type(value) is list:
        for item in value:
            _bound_json(item, depth=depth + 1, count=count)
    elif type(value) is str:
        if len(value) > 256:
            raise ValueError("JSON string is too large")
    elif value is not None and type(value) not in {int, bool}:
        raise ValueError("JSON scalar is invalid")


def _decode_observation(value: Any, *, acknowledgement: str):
    if type(value) is not dict or set(value) != {
        "terminal",
        "reason",
        "stdout_bytes",
        "stderr_bytes",
        "acknowledgement",
    }:
        raise ValueError("observation schema is invalid")
    if value["acknowledgement"] != acknowledgement:
        raise ValueError("observation acknowledgement is invalid")
    terminal_value = value["terminal"]
    terminal = None
    if terminal_value is not None:
        if type(terminal_value) is not dict or set(terminal_value) != {
            "returncode",
            "exit_code",
            "signal_number",
            "category",
        }:
            raise ValueError("terminal schema is invalid")
        for name in ("returncode", "exit_code", "signal_number"):
            if terminal_value[name] is not None and type(terminal_value[name]) is not int:
                raise ValueError("terminal integer is invalid")
        if type(terminal_value["category"]) is not str:
            raise ValueError("terminal category is invalid")
        terminal = ProcessExit(
            terminal_value["returncode"],
            terminal_value["exit_code"],
            terminal_value["signal_number"],
            ProcessExitCategory(terminal_value["category"]),
        )
    for name in ("stdout_bytes", "stderr_bytes"):
        if type(value[name]) is not int:
            raise ValueError("observation byte count is invalid")
    if type(value["reason"]) is not str or type(value["acknowledgement"]) is not str:
        raise ValueError("observation string is invalid")
    observation = _observation_type()(
        terminal, value["reason"], value["stdout_bytes"], value["stderr_bytes"], value["acknowledgement"]
    )
    return _validate_observation(observation, acknowledgement=acknowledgement)


def _decode_artifact(payload: bytes, *, expected_phase: str) -> tuple[str, Any | None]:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_ARTIFACT_BYTES or expected_phase not in _PHASES:
        raise ValueError("artifact framing is invalid")
    try:
        text = payload.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_int=_parse_integer,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON value")),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("artifact JSON is invalid") from None
    _bound_json(value)
    if type(value) is not dict or set(value) != {"schema", "version", "record_id", "phase", "observation"}:
        raise ValueError("artifact schema is invalid")
    if value["schema"] != SCHEMA or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("artifact version is invalid")
    if value["phase"] != expected_phase:
        raise ValueError("artifact phase is invalid")
    record_id = _validate_record_id(value["record_id"])
    if expected_phase == "pending":
        if value["observation"] is not None:
            raise ValueError("pending artifact is invalid")
        observation = None
    else:
        observation = _decode_observation(
            value["observation"], acknowledgement="confirmed" if expected_phase == "confirmed" else "unconfirmed"
        )
    if payload != _encode_artifact(record_id, expected_phase, observation):
        raise ValueError("artifact encoding is not canonical")
    return record_id, observation


def _safe_file(metadata: os.stat_result, *, uid: int, permit_empty: bool = False) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not (0 if permit_empty else 1) <= metadata.st_size <= MAX_ARTIFACT_BYTES
    ):
        raise OSError(errno.EACCES, "record file metadata is unsafe")


def _entry_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _enumerate(directory_fd: int) -> dict[str, tuple[int, ...]]:
    entries: dict[str, tuple[int, ...]] = {}
    temporary_count = 0
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            if (
                len(entries) >= _MAX_DIRECTORY_ENTRIES
                or type(entry.name) is not str
                or len(entry.name.encode("utf-8")) > 80
            ):
                raise OSError(errno.E2BIG, "record directory is unbounded")
            if entry.name not in _FINAL_NAMES:
                if _TEMPORARY.fullmatch(entry.name) is None:
                    raise OSError(errno.EINVAL, "unknown record entry")
                temporary_count += 1
                if temporary_count > _MAX_TEMPORARIES:
                    raise OSError(errno.E2BIG, "too many temporary artifacts")
            metadata = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            _safe_file(metadata, uid=os.geteuid(), permit_empty=entry.name not in _FINAL_NAMES)
            fd = os.open(entry.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            failure = None
            try:
                held = os.fstat(fd)
                if not _same_inode(metadata, held):
                    raise OSError(errno.ESTALE, "record entry changed")
                _safe_file(held, uid=os.geteuid(), permit_empty=entry.name not in _FINAL_NAMES)
                _check_no_acl(fd)
            except BaseException as error:
                failure = error
                raise
            finally:
                try:
                    os.close(fd)
                except BaseException:
                    if failure is None:
                        raise
            entries[entry.name] = _entry_signature(metadata)
    return entries


def _read_artifact(directory_fd: int, name: str) -> bytes:
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    _safe_file(before, uid=os.geteuid())
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    failure = None
    try:
        held = os.fstat(fd)
        if not _same_inode(before, held):
            raise OSError(errno.ESTALE, "record file changed")
        _safe_file(held, uid=os.geteuid())
        _check_no_acl(fd)
        chunks = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        if _entry_signature(held) != _entry_signature(after) or len(payload) > MAX_ARTIFACT_BYTES:
            raise OSError(errno.ESTALE, "record file changed")
        return payload
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            os.close(fd)
        except BaseException:
            if failure is None:
                raise


def _rename_noreplace(directory_fd: int, source: str, target: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from None
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(directory_fd, os.fsencode(source), directory_fd, os.fsencode(target), 1)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


class OCIExecRecordWriter:
    """Current-process-only append-only publisher for one new record directory."""

    def __init__(self, chain: _DirectoryChain, record_id: str) -> None:
        self._chain = chain
        self._record_id = record_id
        self._pid = os.getpid()
        self._closed = False
        self._poisoned = False
        self._observed = None
        self._published: dict[str, tuple[tuple[int, ...], bytes]] = {}

    @classmethod
    def reserve(cls, path: Path | str, *, managed_state: Path) -> OCIExecRecordWriter:
        chain = managed_chain = None
        writer = None
        try:
            _require_linux()
            components = _path_components(path)
            parent_path = "/" + "/".join(components[:-1])
            managed_chain = _open_chain(managed_state, secure=False)
            managed_identity = (os.fstat(managed_chain.fd).st_dev, os.fstat(managed_chain.fd).st_ino)
            chain = _open_chain(parent_path, secure=True)
            if any((os.fstat(fd).st_dev, os.fstat(fd).st_ino) == managed_identity for fd in chain.fds):
                raise OSError(errno.EACCES, "record path is inside managed state")
            _check_directory(os.fstat(chain.fd), uid=os.geteuid(), root_or_user=True, immediate=True)
            os.mkdir(components[-1], 0o700, dir_fd=chain.fd)
            created = os.stat(components[-1], dir_fd=chain.fd, follow_symlinks=False)
            _check_directory(created, uid=os.geteuid(), root_or_user=True, immediate=True)
            child = os.open(
                components[-1],
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_NONBLOCK,
                dir_fd=chain.fd,
            )
            try:
                held = os.fstat(child)
                visible = os.stat(components[-1], dir_fd=chain.fd, follow_symlinks=False)
                if not _same_inode(created, held) or not _same_inode(held, visible):
                    raise OSError(errno.ESTALE, "created directory changed")
                _check_directory(held, uid=os.geteuid(), root_or_user=True, immediate=True)
                _check_directory(visible, uid=os.geteuid(), root_or_user=True, immediate=True)
                _check_no_acl(child)
            except BaseException:
                try:
                    os.close(child)
                except BaseException:
                    pass
                raise
            chain.fds.append(child)
            chain.names.append(components[-1])
            chain.private_tail = 2
            chain.revalidate()
            os.fsync(child)
            os.fsync(chain.fds[-2])
            chain.revalidate()
            writer = cls(chain, str(uuid.uuid4()))
            writer._publish("pending", None)
            chain = None
            return writer
        except BaseException as error:
            if writer is not None:
                writer.close()
            if chain is not None:
                chain.close()
            if isinstance(error, OCIExecRecordError):
                raise
            if not isinstance(error, Exception):
                raise
            raise OCIExecRecordError("reserve") from None
        finally:
            if managed_chain is not None:
                managed_chain.close()

    @property
    def record_id(self) -> str:
        self._ensure_active()
        return self._record_id

    def _ensure_active(self) -> None:
        if os.getpid() != self._pid:
            raise OCIExecRecordError("forked")
        if self._closed:
            raise OCIExecRecordError("closed")
        if self._poisoned:
            raise OCIExecRecordError("poisoned")

    def _cleanup_exact(self, name: str | None, identity: tuple[int, int] | None) -> None:
        if name is None or identity is None:
            return
        try:
            visible = os.stat(name, dir_fd=self._chain.fd, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) == identity:
                os.unlink(name, dir_fd=self._chain.fd)
        except BaseException:
            pass

    def _validate_predecessors(self, phase: str) -> None:
        for predecessor in _PHASES[: _PHASES.index(phase)]:
            expected = self._published.get(predecessor)
            if expected is None:
                raise OSError(errno.ESTALE, "record predecessor is unavailable")
            expected_signature, expected_payload = expected
            name = f"{predecessor}.json"
            visible = os.stat(name, dir_fd=self._chain.fd, follow_symlinks=False)
            if _entry_signature(visible) != expected_signature:
                raise OSError(errno.ESTALE, "record predecessor changed")
            payload = _read_artifact(self._chain.fd, name)
            if payload != expected_payload:
                raise OSError(errno.ESTALE, "record predecessor changed")
            record_id, predecessor_observation = _decode_artifact(payload, expected_phase=predecessor)
            if record_id != self._record_id:
                raise OSError(errno.ESTALE, "record predecessor identity changed")
            if predecessor == "pending" and predecessor_observation is not None:
                raise OSError(errno.ESTALE, "pending predecessor changed")
            if predecessor == "observed" and predecessor_observation != self._observed:
                raise OSError(errno.ESTALE, "observed predecessor changed")

    def _validate_temporary(self, name: str, fd: int, identity: tuple[int, int], payload_size: int) -> None:
        held = os.fstat(fd)
        visible = os.stat(name, dir_fd=self._chain.fd, follow_symlinks=False)
        if not _same_inode(held, visible) or (held.st_dev, held.st_ino) != identity:
            raise OSError(errno.ESTALE, "temporary artifact changed")
        _safe_file(held, uid=os.geteuid())
        _safe_file(visible, uid=os.geteuid())
        if held.st_size != payload_size or visible.st_size != payload_size:
            raise OSError(errno.ESTALE, "temporary artifact changed")
        _check_no_acl(fd)

    def _publish(self, phase: str, observation: Any | None) -> None:
        self._ensure_active()
        temporary = None
        temporary_fd = None
        identity = None
        failure = None
        try:
            temporary = f".{phase}.{uuid.uuid4().hex}.tmp"
            payload = _encode_artifact(self._record_id, phase, observation)
            self._chain.revalidate()
            self._validate_predecessors(phase)
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self._chain.fd,
            )
            metadata = os.fstat(temporary_fd)
            identity = (metadata.st_dev, metadata.st_ino)
            _safe_file(metadata, uid=os.geteuid(), permit_empty=True)
            _check_no_acl(temporary_fd)
            offset = 0
            while offset < len(payload):
                written = os.write(temporary_fd, payload[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                offset += written
            os.fsync(temporary_fd)
            self._validate_temporary(temporary, temporary_fd, identity, len(payload))
            self._chain.revalidate()
            self._validate_predecessors(phase)
            self._validate_temporary(temporary, temporary_fd, identity, len(payload))
            _rename_noreplace(self._chain.fd, temporary, f"{phase}.json")
            visible = os.stat(f"{phase}.json", dir_fd=self._chain.fd, follow_symlinks=False)
            if (visible.st_dev, visible.st_ino) != identity:
                raise OSError(errno.ESTALE, "published artifact changed")
            _safe_file(visible, uid=os.geteuid())
            os.fsync(self._chain.fd)
            self._chain.revalidate()
            self._validate_predecessors(phase)
            after_sync = os.stat(f"{phase}.json", dir_fd=self._chain.fd, follow_symlinks=False)
            if _entry_signature(after_sync) != _entry_signature(visible):
                raise OSError(errno.ESTALE, "published artifact changed")
            _check_no_acl(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            self._published[phase] = (_entry_signature(after_sync), payload)
        except BaseException as error:
            failure = error
            self._poisoned = True
            self._cleanup_exact(temporary, identity)
            if not isinstance(error, Exception):
                self.close()
                raise
            raise OCIExecRecordError(phase) from None
        finally:
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except BaseException:
                    if failure is None:
                        raise

    def publish_observed(self, observation: Any) -> None:
        self._publish("observed", observation)
        self._observed = observation

    def publish_confirmed(self, observation: Any) -> None:
        self._ensure_active()
        try:
            confirmed = _validate_observation(observation, acknowledgement="confirmed")
            if self._observed is None or confirmed != replace(self._observed, acknowledgement="confirmed"):
                raise ValueError("confirmed facts do not match observed facts")
        except BaseException as error:
            self._poisoned = True
            if not isinstance(error, Exception):
                self.close()
                raise
            raise OCIExecRecordError("confirmed") from None
        self._publish("confirmed", confirmed)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._chain.close()
        except BaseException:
            pass

    def __enter__(self) -> OCIExecRecordWriter:
        self._ensure_active()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def read_exec_record(path: Path | str) -> OCIExecRecordSnapshot:
    """Load a stable local historical snapshot without runtime initialization."""

    chain = None
    try:
        _require_linux()
        chain = _open_chain(path, secure=True)
        if len(chain.fds) < 2:
            raise OSError(errno.EACCES, "record parent is unsafe")
        chain.private_tail = 2
        chain.revalidate()
        before = _enumerate(chain.fd)
        decoded: dict[str, tuple[str, Any | None]] = {}
        for phase, name in zip(_PHASES, _FINAL_NAMES, strict=True):
            if name in before:
                decoded[phase] = _decode_artifact(_read_artifact(chain.fd, name), expected_phase=phase)
        try:
            chain.revalidate()
        except Exception:
            raise OCIExecRecordError("changed") from None
        after = _enumerate(chain.fd)
        if before != after:
            raise OCIExecRecordError("changed")
        try:
            chain.revalidate()
        except Exception:
            raise OCIExecRecordError("changed") from None
        if "pending" not in decoded or "confirmed" in decoded and "observed" not in decoded:
            raise ValueError("record phase chain is incomplete")
        record_id = decoded["pending"][0]
        if any(item[0] != record_id for item in decoded.values()):
            raise ValueError("record identity mismatch")
        if "confirmed" in decoded:
            observed = decoded["observed"][1]
            confirmed = decoded["confirmed"][1]
            if confirmed != replace(observed, acknowledgement="confirmed"):
                raise ValueError("confirmed facts do not match observed facts")
            return OCIExecRecordSnapshot(record_id, "confirmed", confirmed)
        if "observed" in decoded:
            return OCIExecRecordSnapshot(record_id, "observed", decoded["observed"][1])
        return OCIExecRecordSnapshot(record_id, "pending", None)
    except OCIExecRecordError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise OCIExecRecordError("read") from None
    finally:
        if chain is not None:
            chain.close()


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "OCIExecRecordError",
    "OCIExecRecordSnapshot",
    "OCIExecRecordWriter",
    "SCHEMA",
    "VERSION",
    "read_exec_record",
]
