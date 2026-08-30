"""Inactive, transport-neutral OCI workload supervision primitives.

This module deliberately defines no guest wire format, listener, adoption, or
runtime integration.  A future decoder may feed semantic events into
``SupervisorCore`` after authenticating its own transport.

The inactive journal favors bounded-memory full integrity rescans.  Its
deliberate O(n) pull cost must be replaced by a verified incremental index
before this module is activated for unbounded production journals.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat as stat_module
import struct
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .errors import PalimpsestError
from .runtime_types import (
    ExistingRunRecord,
    ProcessCapabilities,
    ProcessCapabilityError,
    ProcessEvent,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
    RunAttachmentMode,
    RunResult,
    RuntimeBackend,
    RuntimeKind,
)

_JOURNAL_NAME = "oci-supervisor-host-v1.journal"
_JOURNAL_HEADER = b"PALIMPSEST-OCI-SUPERVISOR-HOST-JOURNAL-V1\n"
_FRAME_LENGTH = struct.Struct(">I")
_CHECKSUM_BYTES = 32
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_FRAME_BYTES = 128 * 1024
_SESSION_PULL_RECORD_LIMIT = 1
MAX_ACTIVE_SESSIONS = 32


class SupervisorErrorCategory(StrEnum):
    INVALID_IDENTITY = "invalid-identity"
    INVALID_JOURNAL = "invalid-journal"
    JOURNAL_CORRUPT = "journal-corrupt"
    JOURNAL_IO = "journal-io"
    INVALID_TRANSITION = "invalid-transition"
    NOT_READY = "not-ready"
    READINESS_FAILED = "readiness-failed"
    CONTROL_LOST = "control-lost"
    ALREADY_CONSUMED = "already-consumed"
    SESSION_CAPACITY = "session-capacity"
    CLOSED = "closed"
    UNAUTHORIZED_PEER = "unauthorized-peer"
    PROCESS_MISMATCH = "process-mismatch"
    JOURNAL_BUSY = "journal-busy"
    ADOPTION_FORBIDDEN = "adoption-forbidden"


class SupervisorError(PalimpsestError):
    """Stable failure whose message never reflects attacker-controlled data."""

    def __init__(self, category: SupervisorErrorCategory) -> None:
        if not isinstance(category, SupervisorErrorCategory):
            raise TypeError("supervisor error requires a stable category")
        self.category = category
        super().__init__(category.value)


class SupervisorPhase(StrEnum):
    STARTING = "starting"
    READY = "ready"
    EXITED = "exited"
    READINESS_FAILED = "readiness-failed"
    DEGRADED = "degraded"
    CLOSED = "closed"


class SupervisorRecordKind(StrEnum):
    READY = "ready"
    OUTPUT = "output"
    EXIT = "exit"
    DEGRADED = "degraded"
    READINESS_FAILED = "readiness-failed"


@dataclass(frozen=True, slots=True)
class SupervisorIdentity:
    record: ExistingRunRecord
    generation: str
    owner_uid: int

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise SupervisorError(SupervisorErrorCategory.INVALID_IDENTITY)
        if (
            self.record.state_schema_version != 2
            or self.record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
            or self.record.dispatch_key.backend is not RuntimeBackend.KVM
            or type(self.owner_uid) is not int
            or self.owner_uid != os.geteuid()
        ):
            raise SupervisorError(SupervisorErrorCategory.INVALID_IDENTITY)
        try:
            parsed = uuid.UUID(self.generation)
        except (AttributeError, TypeError, ValueError):
            raise SupervisorError(SupervisorErrorCategory.INVALID_IDENTITY) from None
        if str(parsed) != self.generation:
            raise SupervisorError(SupervisorErrorCategory.INVALID_IDENTITY)

    @property
    def public_id(self) -> str:
        return self.record.run_id


@dataclass(frozen=True, slots=True)
class ProcessIncarnation:
    pid: int
    boot_id: str
    start_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("process incarnation requires a positive PID")
        if not isinstance(self.boot_id, str) or not self.boot_id or "\x00" in self.boot_id:
            raise ValueError("process incarnation requires a boot ID")
        if type(self.start_ticks) is not int or self.start_ticks < 0:
            raise ValueError("process incarnation requires nonnegative start ticks")


def same_process_incarnation(left: ProcessIncarnation, right: ProcessIncarnation) -> bool:
    if not isinstance(left, ProcessIncarnation) or not isinstance(right, ProcessIncarnation):
        return False
    return left.pid == right.pid and left.boot_id == right.boot_id and left.start_ticks == right.start_ticks


def authorize_supervisor_peer(
    identity: SupervisorIdentity,
    *,
    owner_uid: int,
    run_id: str,
    generation: str,
) -> bool:
    if not isinstance(identity, SupervisorIdentity) or type(owner_uid) is not int:
        return False
    return (
        owner_uid == identity.owner_uid
        and isinstance(run_id, str)
        and run_id == identity.record.run_id
        and isinstance(generation, str)
        and generation == identity.generation
    )


@runtime_checkable
class SupervisorControlPort(Protocol):
    """Semantic control surface; it intentionally specifies no wire format."""

    def signal(self, requested: ProcessSignal) -> None: ...

    def request_stop(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class SupervisorJournalRecord:
    cursor: int
    kind: SupervisorRecordKind
    stream: ProcessStream | None = None
    data: bytes | None = field(default=None, repr=False)
    exit: ProcessExit | None = None
    error_category: SupervisorErrorCategory | None = None


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    phase: SupervisorPhase
    journal_cursor: int
    ready_cursor: int | None
    exit: ProcessExit | None = None
    error_category: SupervisorErrorCategory | None = None


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except OSError:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
        if written <= 0:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO)
        offset += written


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


class SupervisorJournal:
    """Descriptor-pinned, owner-only host semantic journal version 1."""

    def __init__(self, directory_fd: int, identity: SupervisorIdentity) -> None:
        if type(directory_fd) is not int or not isinstance(identity, SupervisorIdentity):
            raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL)
        self._lock = threading.RLock()
        self._closed = False
        self._failed = False
        self._identity = identity
        self._directory_fd = -1
        self._file_fd = -1
        self._directory_identity: tuple[int, int] | None = None
        self._file_identity: tuple[int, int] | None = None
        self._offsets: list[int] = []
        self._phase = SupervisorPhase.STARTING
        self._ready_cursor: int | None = None
        self._exit: ProcessExit | None = None
        self._error_category: SupervisorErrorCategory | None = None
        try:
            self._directory_fd = os.dup(directory_fd)
            directory_metadata = os.fstat(self._directory_fd)
            if (
                not stat_module.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != os.geteuid()
                or stat_module.S_IMODE(directory_metadata.st_mode) != 0o700
            ):
                raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL)
            self._directory_identity = _metadata_identity(directory_metadata)
            self._open_file()
            self._reload()
        except SupervisorError:
            self.close()
            raise
        except OSError:
            self.close()
            raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL) from None

    @property
    def identity(self) -> SupervisorIdentity:
        return self._identity

    @property
    def cursor(self) -> int:
        with self._lock:
            self._require_open()
            self._reload()
            return len(self._offsets)

    @property
    def snapshot(self) -> SupervisorSnapshot:
        with self._lock:
            self._require_open()
            self._reload()
            return SupervisorSnapshot(
                self._phase,
                len(self._offsets),
                self._ready_cursor,
                self._exit,
                self._error_category,
            )

    def _open_file(self) -> None:
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        created = False
        try:
            self._file_fd = os.open(_JOURNAL_NAME, flags, dir_fd=self._directory_fd)
        except FileNotFoundError:
            try:
                self._file_fd = os.open(
                    _JOURNAL_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=self._directory_fd,
                )
                created = True
            except OSError:
                raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL) from None
        except OSError:
            raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL) from None
        if created:
            try:
                os.fchmod(self._file_fd, 0o600)
            except OSError:
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
        self._validate_binding()
        try:
            fcntl.flock(self._file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_BUSY) from None
        self._validate_binding()
        if created:
            try:
                _write_all(self._file_fd, _JOURNAL_HEADER)
                os.fsync(self._file_fd)
                os.fsync(self._directory_fd)
            except (OSError, SupervisorError):
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
            self._validate_binding()

    def _require_open(self) -> None:
        if self._closed:
            raise SupervisorError(SupervisorErrorCategory.CLOSED)
        if self._failed:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO)

    def _validate_binding(self) -> None:
        try:
            directory_metadata = os.fstat(self._directory_fd)
            file_metadata = os.fstat(self._file_fd)
            entry_metadata = os.stat(_JOURNAL_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError:
            raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL) from None
        if self._directory_identity is None:
            self._directory_identity = _metadata_identity(directory_metadata)
        if self._file_identity is None:
            self._file_identity = _metadata_identity(file_metadata)
        if (
            not stat_module.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(directory_metadata.st_mode) != 0o700
            or _metadata_identity(directory_metadata) != self._directory_identity
            or not stat_module.S_ISREG(file_metadata.st_mode)
            or not stat_module.S_ISREG(entry_metadata.st_mode)
            or file_metadata.st_uid != os.geteuid()
            or entry_metadata.st_uid != os.geteuid()
            or file_metadata.st_nlink != 1
            or entry_metadata.st_nlink != 1
            or stat_module.S_IMODE(file_metadata.st_mode) != 0o600
            or stat_module.S_IMODE(entry_metadata.st_mode) != 0o600
            or _metadata_identity(file_metadata) != self._file_identity
            or _metadata_identity(entry_metadata) != self._file_identity
        ):
            raise SupervisorError(SupervisorErrorCategory.INVALID_JOURNAL)

    def _read_exact(self, offset: int, length: int) -> bytes:
        try:
            content = os.pread(self._file_fd, length, offset)
        except OSError:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
        if len(content) != length:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        return content

    def _decode_payload(self, payload: bytes, expected_cursor: int) -> SupervisorJournalRecord:
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT) from None
        expected_keys = {
            "version",
            "run_id",
            "generation",
            "cursor",
            "kind",
            "stream",
            "data",
            "returncode",
            "exit_code",
            "signal_number",
            "exit_category",
            "error_category",
        }
        if type(raw) is not dict or set(raw) != expected_keys:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        if (
            type(raw["version"]) is not int
            or raw["version"] != 1
            or raw["run_id"] != self._identity.record.run_id
            or raw["generation"] != self._identity.generation
            or type(raw["cursor"]) is not int
            or raw["cursor"] != expected_cursor
        ):
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        try:
            kind = SupervisorRecordKind(raw["kind"])
        except (TypeError, ValueError):
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT) from None
        stream: ProcessStream | None = None
        data: bytes | None = None
        exit_result: ProcessExit | None = None
        error_category: SupervisorErrorCategory | None = None
        if kind is SupervisorRecordKind.OUTPUT:
            try:
                stream = ProcessStream(raw["stream"])
                data = base64.b64decode(raw["data"], validate=True)
            except (TypeError, ValueError):
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT) from None
            if stream not in {ProcessStream.STDOUT, ProcessStream.STDERR} or not data or len(data) > _MAX_OUTPUT_BYTES:
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        elif kind is SupervisorRecordKind.EXIT:
            try:
                exit_result = ProcessExit(
                    raw["returncode"],
                    raw["exit_code"],
                    raw["signal_number"],
                    ProcessExitCategory(raw["exit_category"]),
                )
            except (TypeError, ValueError):
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT) from None
            if exit_result.category not in {
                ProcessExitCategory.EXITED,
                ProcessExitCategory.SIGNALED,
            }:
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        elif kind in {SupervisorRecordKind.DEGRADED, SupervisorRecordKind.READINESS_FAILED}:
            expected_error = (
                SupervisorErrorCategory.CONTROL_LOST
                if kind is SupervisorRecordKind.DEGRADED
                else SupervisorErrorCategory.READINESS_FAILED
            )
            if raw["error_category"] != expected_error.value:
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
            error_category = expected_error
        empty_fields_valid = (
            (kind is SupervisorRecordKind.OUTPUT or raw["stream"] is None)
            and (kind is SupervisorRecordKind.OUTPUT or raw["data"] is None)
            and (kind is SupervisorRecordKind.EXIT or raw["returncode"] is None)
            and (kind is SupervisorRecordKind.EXIT or raw["exit_code"] is None)
            and (kind is SupervisorRecordKind.EXIT or raw["signal_number"] is None)
            and (kind is SupervisorRecordKind.EXIT or raw["exit_category"] is None)
            and (
                kind in {SupervisorRecordKind.DEGRADED, SupervisorRecordKind.READINESS_FAILED}
                or raw["error_category"] is None
            )
        )
        if not empty_fields_valid:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        return SupervisorJournalRecord(expected_cursor, kind, stream, data, exit_result, error_category)

    def _apply_semantics(self, record: SupervisorJournalRecord) -> None:
        if self._phase is SupervisorPhase.STARTING:
            if record.kind is SupervisorRecordKind.READY:
                self._phase = SupervisorPhase.READY
                self._ready_cursor = record.cursor
                return
            if record.kind is SupervisorRecordKind.READINESS_FAILED:
                self._phase = SupervisorPhase.READINESS_FAILED
                self._error_category = SupervisorErrorCategory.READINESS_FAILED
                return
        elif self._phase is SupervisorPhase.READY:
            if record.kind is SupervisorRecordKind.OUTPUT:
                return
            if record.kind is SupervisorRecordKind.EXIT:
                self._phase = SupervisorPhase.EXITED
                self._exit = record.exit
                return
            if record.kind is SupervisorRecordKind.DEGRADED:
                self._phase = SupervisorPhase.DEGRADED
                self._error_category = SupervisorErrorCategory.CONTROL_LOST
                return
        raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)

    def _read_record_at(
        self,
        offset: int,
        expected_cursor: int,
        *,
        file_size: int,
    ) -> tuple[SupervisorJournalRecord, int]:
        if file_size - offset < _FRAME_LENGTH.size:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        payload_length = _FRAME_LENGTH.unpack(self._read_exact(offset, _FRAME_LENGTH.size))[0]
        if payload_length <= 0 or payload_length > _MAX_FRAME_BYTES:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        frame_length = _FRAME_LENGTH.size + payload_length + _CHECKSUM_BYTES
        if file_size - offset < frame_length:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        payload_offset = offset + _FRAME_LENGTH.size
        payload = self._read_exact(payload_offset, payload_length)
        checksum = self._read_exact(payload_offset + payload_length, _CHECKSUM_BYTES)
        if checksum != hashlib.sha256(payload).digest():
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        return self._decode_payload(payload, expected_cursor), frame_length

    def _reload(self) -> None:
        self._validate_binding()
        try:
            size = os.fstat(self._file_fd).st_size
        except OSError:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
        if size < len(_JOURNAL_HEADER) or self._read_exact(0, len(_JOURNAL_HEADER)) != _JOURNAL_HEADER:
            raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
        offsets: list[int] = []
        self._phase = SupervisorPhase.STARTING
        self._ready_cursor = None
        self._exit = None
        self._error_category = None
        offset = len(_JOURNAL_HEADER)
        while offset < size:
            record, frame_length = self._read_record_at(offset, len(offsets) + 1, file_size=size)
            offsets.append(offset)
            self._apply_semantics(record)
            offset += frame_length
        self._offsets = offsets

    def _payload_for(
        self,
        cursor: int,
        kind: SupervisorRecordKind,
        *,
        stream: ProcessStream | None = None,
        data: bytes | None = None,
        exit_result: ProcessExit | None = None,
        error_category: SupervisorErrorCategory | None = None,
    ) -> bytes:
        payload = {
            "version": 1,
            "run_id": self._identity.record.run_id,
            "generation": self._identity.generation,
            "cursor": cursor,
            "kind": kind.value,
            "stream": None if stream is None else stream.value,
            "data": None if data is None else base64.b64encode(data).decode("ascii"),
            "returncode": None if exit_result is None else exit_result.returncode,
            "exit_code": None if exit_result is None else exit_result.exit_code,
            "signal_number": None if exit_result is None else exit_result.signal_number,
            "exit_category": None if exit_result is None else exit_result.category.value,
            "error_category": None if error_category is None else error_category.value,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def append(
        self,
        kind: SupervisorRecordKind,
        *,
        stream: ProcessStream | None = None,
        data: bytes | None = None,
        exit_result: ProcessExit | None = None,
        error_category: SupervisorErrorCategory | None = None,
    ) -> SupervisorJournalRecord:
        with self._lock:
            self._require_open()
            self._reload()
            cursor = len(self._offsets) + 1
            payload = self._payload_for(
                cursor,
                kind,
                stream=stream,
                data=data,
                exit_result=exit_result,
                error_category=error_category,
            )
            if len(payload) > _MAX_FRAME_BYTES:
                raise SupervisorError(SupervisorErrorCategory.INVALID_TRANSITION)
            candidate = self._decode_payload(payload, cursor)
            prior = (self._phase, self._ready_cursor, self._exit, self._error_category)
            try:
                self._apply_semantics(candidate)
            except SupervisorError:
                self._phase, self._ready_cursor, self._exit, self._error_category = prior
                raise SupervisorError(SupervisorErrorCategory.INVALID_TRANSITION) from None
            self._phase, self._ready_cursor, self._exit, self._error_category = prior
            frame = _FRAME_LENGTH.pack(len(payload)) + payload + hashlib.sha256(payload).digest()
            try:
                self._validate_binding()
                offset = os.fstat(self._file_fd).st_size
                _write_all(self._file_fd, frame)
                os.fsync(self._file_fd)
                self._validate_binding()
            except (OSError, SupervisorError):
                self._failed = True
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
            self._offsets.append(offset)
            self._apply_semantics(candidate)
            return candidate

    def records_after(
        self,
        cursor: int,
        *,
        limit: int | None = None,
    ) -> tuple[SupervisorJournalRecord, ...]:
        with self._lock:
            self._require_open()
            self._reload()
            if type(cursor) is not int or cursor < 0 or cursor > len(self._offsets):
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)
            if limit is not None and (type(limit) is not int or limit <= 0):
                raise ValueError("journal record limit must be positive")
            stop = None if limit is None else cursor + limit
            selected_offsets = self._offsets[cursor:stop]
            try:
                file_size = os.fstat(self._file_fd).st_size
            except OSError:
                raise SupervisorError(SupervisorErrorCategory.JOURNAL_IO) from None
            result: list[SupervisorJournalRecord] = []
            for expected_cursor, offset in enumerate(selected_offsets, start=cursor + 1):
                record, _frame_length = self._read_record_at(
                    offset,
                    expected_cursor,
                    file_size=file_size,
                )
                result.append(record)
            return tuple(result)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for descriptor_name in ("_file_fd", "_directory_fd"):
                descriptor = getattr(self, descriptor_name)
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, descriptor_name, -1)


class SupervisorCore:
    """Durability-first semantic state machine with no restart policy."""

    def __init__(
        self,
        identity: SupervisorIdentity,
        journal: SupervisorJournal,
        control_port: SupervisorControlPort,
    ) -> None:
        if not isinstance(identity, SupervisorIdentity) or journal.identity != identity:
            raise SupervisorError(SupervisorErrorCategory.INVALID_IDENTITY)
        if not isinstance(control_port, SupervisorControlPort):
            raise TypeError("supervisor core requires a semantic control port")
        snapshot = journal.snapshot
        if (
            snapshot.phase is not SupervisorPhase.STARTING
            or snapshot.journal_cursor != 0
            or snapshot.ready_cursor is not None
            or snapshot.exit is not None
            or snapshot.error_category is not None
        ):
            raise SupervisorError(SupervisorErrorCategory.ADOPTION_FORBIDDEN)
        self._identity = identity
        self._journal = journal
        self._control_port = control_port
        self._condition = threading.Condition(threading.RLock())
        self._phase = snapshot.phase
        self._ready_cursor = snapshot.ready_cursor
        self._exit = snapshot.exit
        self._error_category = snapshot.error_category
        self._journal_cursor = snapshot.journal_cursor
        self._closed = False
        self._active_clients: set[int] = set()
        self._next_client = 1

    @property
    def identity(self) -> SupervisorIdentity:
        return self._identity

    @property
    def ready_cursor(self) -> int | None:
        with self._condition:
            return self._ready_cursor

    @property
    def public_id(self) -> str:
        with self._condition:
            self._require_ready()
            return self._identity.public_id

    @property
    def snapshot(self) -> SupervisorSnapshot:
        with self._condition:
            return SupervisorSnapshot(
                SupervisorPhase.CLOSED if self._closed else self._phase,
                self._journal_cursor,
                self._ready_cursor,
                self._exit,
                self._error_category,
            )

    def _require_open(self) -> None:
        if self._closed:
            raise SupervisorError(SupervisorErrorCategory.CLOSED)

    def _require_ready(self) -> None:
        self._require_open()
        if self._error_category in {
            SupervisorErrorCategory.INVALID_JOURNAL,
            SupervisorErrorCategory.JOURNAL_CORRUPT,
            SupervisorErrorCategory.JOURNAL_IO,
            SupervisorErrorCategory.CLOSED,
        }:
            raise SupervisorError(self._error_category)
        if self._phase is SupervisorPhase.READINESS_FAILED:
            raise SupervisorError(SupervisorErrorCategory.READINESS_FAILED)
        if self._phase is SupervisorPhase.DEGRADED:
            raise SupervisorError(SupervisorErrorCategory.CONTROL_LOST)
        if self._ready_cursor is None:
            raise SupervisorError(SupervisorErrorCategory.NOT_READY)

    def _commit(
        self,
        kind: SupervisorRecordKind,
        *,
        stream: ProcessStream | None = None,
        data: bytes | None = None,
        exit_result: ProcessExit | None = None,
        error_category: SupervisorErrorCategory | None = None,
    ) -> SupervisorJournalRecord:
        self._require_open()
        try:
            record = self._journal.append(
                kind,
                stream=stream,
                data=data,
                exit_result=exit_result,
                error_category=error_category,
            )
            snapshot = self._journal.snapshot
        except SupervisorError as error:
            if error.category in {
                SupervisorErrorCategory.INVALID_JOURNAL,
                SupervisorErrorCategory.JOURNAL_CORRUPT,
                SupervisorErrorCategory.JOURNAL_IO,
                SupervisorErrorCategory.CLOSED,
            }:
                self._phase = (
                    SupervisorPhase.READINESS_FAILED if self._ready_cursor is None else SupervisorPhase.DEGRADED
                )
                self._error_category = error.category
                self._condition.notify_all()
            raise
        self._phase = snapshot.phase
        self._ready_cursor = snapshot.ready_cursor
        self._exit = snapshot.exit
        self._error_category = snapshot.error_category
        self._journal_cursor = record.cursor
        self._condition.notify_all()
        return record

    def wait_ready(self, timeout: float | None = None) -> int:
        if timeout is not None and (isinstance(timeout, bool) or timeout < 0):
            raise ValueError("readiness timeout must be nonnegative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._ready_cursor is None:
                self._require_open()
                if self._error_category in {
                    SupervisorErrorCategory.INVALID_JOURNAL,
                    SupervisorErrorCategory.JOURNAL_CORRUPT,
                    SupervisorErrorCategory.JOURNAL_IO,
                    SupervisorErrorCategory.CLOSED,
                }:
                    raise SupervisorError(self._error_category)
                if self._phase is SupervisorPhase.READINESS_FAILED:
                    raise SupervisorError(SupervisorErrorCategory.READINESS_FAILED)
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SupervisorError(SupervisorErrorCategory.NOT_READY)
                self._condition.wait(remaining)
            return self._ready_cursor

    def mark_ready(self) -> int:
        with self._condition:
            return self._commit(SupervisorRecordKind.READY).cursor

    def append_output(self, stream: ProcessStream, data: bytes) -> int:
        if stream not in {ProcessStream.STDOUT, ProcessStream.STDERR}:
            raise SupervisorError(SupervisorErrorCategory.INVALID_TRANSITION)
        if not isinstance(data, bytes) or not data or len(data) > _MAX_OUTPUT_BYTES:
            raise SupervisorError(SupervisorErrorCategory.INVALID_TRANSITION)
        with self._condition:
            return self._commit(SupervisorRecordKind.OUTPUT, stream=stream, data=data).cursor

    def mark_exit(self, result: ProcessExit) -> int:
        if not isinstance(result, ProcessExit) or result.category not in {
            ProcessExitCategory.EXITED,
            ProcessExitCategory.SIGNALED,
        }:
            raise SupervisorError(SupervisorErrorCategory.INVALID_TRANSITION)
        with self._condition:
            return self._commit(SupervisorRecordKind.EXIT, exit_result=result).cursor

    def fail_readiness(self) -> int:
        with self._condition:
            return self._commit(
                SupervisorRecordKind.READINESS_FAILED,
                error_category=SupervisorErrorCategory.READINESS_FAILED,
            ).cursor

    def mark_control_lost(self) -> int:
        with self._condition:
            return self._commit(
                SupervisorRecordKind.DEGRADED,
                error_category=SupervisorErrorCategory.CONTROL_LOST,
            ).cursor

    def _require_control_available(self) -> None:
        self._require_open()
        if self._phase is SupervisorPhase.DEGRADED:
            raise SupervisorError(SupervisorErrorCategory.CONTROL_LOST)
        if self._phase is SupervisorPhase.READINESS_FAILED:
            raise SupervisorError(SupervisorErrorCategory.READINESS_FAILED)
        if self._phase is SupervisorPhase.EXITED:
            raise SupervisorError(SupervisorErrorCategory.INVALID_TRANSITION)

    def request_stop(self) -> None:
        with self._condition:
            self._require_control_available()
            self._control_port.request_stop()

    def detached_result(self) -> RunResult:
        self.wait_ready()
        with self._condition:
            self._require_ready()
            status = "exited" if self._phase is SupervisorPhase.EXITED else "running"
            return RunResult(
                self._identity.record,
                status,
                True,
                RunAttachmentMode.DETACHED,
            )

    def attached_result(self) -> RunResult:
        self.wait_ready()
        with self._condition:
            self._require_ready()
            if len(self._active_clients) >= MAX_ACTIVE_SESSIONS:
                raise SupervisorError(SupervisorErrorCategory.SESSION_CAPACITY)
            token = self._next_client
            self._next_client += 1
            self._active_clients.add(token)
            assert self._ready_cursor is not None
            session = SupervisorProcessSession(self, token, self._ready_cursor)
            status = "exited" if self._phase is SupervisorPhase.EXITED else "running"
            return RunResult(
                self._identity.record,
                status,
                True,
                RunAttachmentMode.ATTACHED,
                session=session,
            )

    def _release_client(self, token: int) -> None:
        with self._condition:
            self._active_clients.discard(token)
            self._condition.notify_all()

    def _records_or_wait(self, cursor: int, token: int) -> tuple[SupervisorJournalRecord, ...] | None:
        with self._condition:
            while token in self._active_clients:
                records = self._journal.records_after(cursor, limit=_SESSION_PULL_RECORD_LIMIT)
                if records:
                    return records
                if self._closed:
                    raise SupervisorError(SupervisorErrorCategory.CLOSED)
                self._condition.wait()
            return None

    def _wait_terminal(self, cursor: int, token: int) -> ProcessExit:
        current = cursor
        try:
            while True:
                records = self._records_or_wait(current, token)
                if records is None:
                    raise SupervisorError(SupervisorErrorCategory.CLOSED)
                for record in records:
                    current = record.cursor
                    if record.kind is SupervisorRecordKind.EXIT:
                        assert record.exit is not None
                        self._release_client(token)
                        return record.exit
                    if record.kind is SupervisorRecordKind.DEGRADED:
                        raise SupervisorError(SupervisorErrorCategory.CONTROL_LOST)
        except SupervisorError:
            self._release_client(token)
            raise

    def _signal(self, token: int, requested: ProcessSignal) -> None:
        if not isinstance(requested, ProcessSignal):
            raise TypeError("process signal must be a ProcessSignal")
        with self._condition:
            if token not in self._active_clients:
                raise SupervisorError(SupervisorErrorCategory.CLOSED)
            self._require_control_available()
            self._control_port.signal(requested)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        try:
            self._control_port.close()
        finally:
            self._journal.close()


class SupervisorProcessSession:
    capabilities = ProcessCapabilities(stdin=False, tty=False, resize=False, signal=True)

    def __init__(self, core: SupervisorCore, token: int, ready_cursor: int) -> None:
        self._core = core
        self._token = token
        self._cursor = ready_cursor
        self._consumed = False
        self._closed = False
        self._terminal: ProcessExit | None = None
        self._terminal_error: SupervisorErrorCategory | None = None
        self._lock = threading.RLock()

    @property
    def replay_cursor(self) -> int:
        return self._cursor

    def events(self) -> Iterator[ProcessEvent]:
        with self._lock:
            if self._consumed:
                raise SupervisorError(SupervisorErrorCategory.ALREADY_CONSUMED)
            self._consumed = True
        return self._iterate_events()

    def _iterate_events(self) -> Iterator[ProcessEvent]:
        try:
            while True:
                records = self._core._records_or_wait(self._cursor, self._token)
                if records is None:
                    return
                for record in records:
                    self._cursor = record.cursor
                    if record.kind is SupervisorRecordKind.OUTPUT:
                        assert record.stream is not None and record.data is not None
                        yield ProcessOutputEvent(record.stream, record.data)
                    elif record.kind is SupervisorRecordKind.EXIT:
                        assert record.exit is not None
                        self._terminal = record.exit
                        self._core._release_client(self._token)
                        yield ProcessStatusEvent(record.exit)
                        return
                    elif record.kind is SupervisorRecordKind.DEGRADED:
                        raise SupervisorError(SupervisorErrorCategory.CONTROL_LOST)
        except SupervisorError as error:
            self._terminal_error = error.category
            raise
        finally:
            self._core._release_client(self._token)

    def write_stdin(self, data: bytes) -> None:
        del data
        raise ProcessCapabilityError("stdin")

    def close_stdin(self) -> None:
        raise ProcessCapabilityError("stdin")

    def resize(self, rows: int, columns: int) -> None:
        del rows, columns
        raise ProcessCapabilityError("resize")

    def signal(self, requested: ProcessSignal) -> None:
        self._core._signal(self._token, requested)

    def wait(self) -> ProcessExit:
        with self._lock:
            if self._terminal is not None:
                return self._terminal
            if self._terminal_error is not None:
                raise SupervisorError(self._terminal_error)
            if self._closed:
                raise SupervisorError(SupervisorErrorCategory.CLOSED)
            if self._consumed:
                raise SupervisorError(SupervisorErrorCategory.ALREADY_CONSUMED)
            self._consumed = True
        try:
            terminal = self._core._wait_terminal(self._cursor, self._token)
        except SupervisorError as error:
            with self._lock:
                self._terminal_error = error.category
            raise
        with self._lock:
            self._terminal = terminal
        return terminal

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._core._release_client(self._token)


__all__ = (
    "MAX_ACTIVE_SESSIONS",
    "ProcessIncarnation",
    "SupervisorControlPort",
    "SupervisorCore",
    "SupervisorError",
    "SupervisorErrorCategory",
    "SupervisorIdentity",
    "SupervisorJournal",
    "SupervisorJournalRecord",
    "SupervisorPhase",
    "SupervisorProcessSession",
    "SupervisorRecordKind",
    "SupervisorSnapshot",
    "authorize_supervisor_peer",
    "same_process_incarnation",
)
