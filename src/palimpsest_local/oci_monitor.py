"""Production-inert durable ownership for a future OCI-root monitor.

This module deliberately has no runtime, CLI, libvirt, lifecycle-transport, or
``oci_supervisor`` integration.  It establishes only the restart boundary: one
owner-private writer may hold an exact run/plan/domain/boot binding, and a new
writer may adopt that binding only after the recorded process incarnation is
proven stale.

The per-run lock is the live single-writer authority.  The JSON journal is the
durable recovery intent.  Neither is evidence that a libvirt domain is still
the recorded domain; a future adopter must revalidate that external object
before exact cleanup.  Because the lifecycle boot key is memory-only, adoption
can never promote the journal back to ``running``.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat as stat_module
import threading
import uuid
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import PalimpsestError
from .runtime_types import ExistingRunRecord, RuntimeBackend, RuntimeKind

_JOURNAL_NAME = "oci-monitor-owner-v1.json"
_LOCK_NAME = "oci-monitor-owner-v1.lock"
_JOURNAL_SCHEMA = "palimpsest.oci-root-monitor-owner.v1"
_LIFECYCLE_PROTOCOL = "palimpsest.oci-lifecycle-control.v2"
_MAX_JOURNAL_BYTES = 16 * 1024
_MAX_REVISION = 2**63 - 1
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_DOMAIN_ID = 2**31 - 1
_PHASES = frozenset({"starting", "adopting", "running", "control-lost", "terminal"})


class _ForkCloseMonitorLease:
    """Mutable child-close record kept separate from reusable descriptor numbers."""

    __slots__ = ("directory_fd", "lease", "lock_fd")

    def __init__(self, lease: MonitorLease, directory_fd: int, lock_fd: int) -> None:
        self.directory_fd = directory_fd
        self.lease = weakref.ref(lease)
        self.lock_fd = lock_fd

    def close_in_child(self) -> None:
        lock_fd, self.lock_fd = self.lock_fd, -1
        directory_fd, self.directory_fd = self.directory_fd, -1
        lease = self.lease()
        if lease is not None:
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


_FORK_MONITOR_LEASES: set[_ForkCloseMonitorLease] = set()
_FORK_REGISTRY_LOCK = threading.Lock()


def _close_inherited_monitor_leases() -> None:
    """Drop child copies so fork descendants cannot retain parent ownership."""

    try:
        inherited = tuple(_FORK_MONITOR_LEASES)
        _FORK_MONITOR_LEASES.clear()
        for resource in inherited:
            resource.close_in_child()
    finally:
        _FORK_REGISTRY_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_REGISTRY_LOCK.acquire,
        after_in_parent=_FORK_REGISTRY_LOCK.release,
        after_in_child=_close_inherited_monitor_leases,
    )


class MonitorErrorCategory(StrEnum):
    INVALID_IDENTITY = "invalid-identity"
    INVALID_JOURNAL = "invalid-journal"
    JOURNAL_IO = "journal-io"
    JOURNAL_BUSY = "journal-busy"
    INVALID_TRANSITION = "invalid-transition"
    ADOPTION_FORBIDDEN = "adoption-forbidden"
    WRITER_LIVE = "writer-live"
    WRITER_UNKNOWN = "writer-unknown"
    PROCESS_MISMATCH = "process-mismatch"
    POISONED = "poisoned"


class MonitorError(PalimpsestError):
    """Stable monitor failure that never reflects journal-controlled values."""

    def __init__(self, category: MonitorErrorCategory) -> None:
        if not isinstance(category, MonitorErrorCategory):
            raise TypeError("monitor error requires a stable category")
        self.category = category
        super().__init__(category.value)


class ProcessLiveness(StrEnum):
    LIVE = "live"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MonitorProcessIdentity:
    pid: int
    host_boot_id: str
    start_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise MonitorError(MonitorErrorCategory.INVALID_IDENTITY)
        if type(self.start_ticks) is not int or self.start_ticks < 0:
            raise MonitorError(MonitorErrorCategory.INVALID_IDENTITY)
        if not _canonical_uuid(self.host_boot_id):
            raise MonitorError(MonitorErrorCategory.INVALID_IDENTITY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_boot_id": self.host_boot_id,
            "pid": self.pid,
            "start_ticks": self.start_ticks,
        }

    @classmethod
    def from_dict(cls, value: Any) -> MonitorProcessIdentity:
        if not isinstance(value, Mapping) or set(value) != {"host_boot_id", "pid", "start_ticks"}:
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
        try:
            return cls(value["pid"], value["host_boot_id"], value["start_ticks"])
        except MonitorError:
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None


@dataclass(frozen=True, slots=True)
class MonitorBinding:
    record: ExistingRunRecord
    owner_uid: int
    plan_digest: str
    definition_projection_digest: str
    stage1_artifact_digest: str
    domain_uuid: str
    domain_id: int
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
            or not _canonical_digest(self.plan_digest)
            or not _canonical_digest(self.definition_projection_digest)
            or not _canonical_digest(self.stage1_artifact_digest)
            or not _canonical_uuid(self.domain_uuid)
            or type(self.domain_id) is not int
            or not 1 <= self.domain_id <= _MAX_DOMAIN_ID
            or not _canonical_uuid(self.boot_attempt_id)
            or self.libvirt_uri != "qemu:///system"
        ):
            raise MonitorError(MonitorErrorCategory.INVALID_IDENTITY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.record.dispatch_key.backend.value,
            "boot_attempt_id": self.boot_attempt_id,
            "definition_projection_digest": self.definition_projection_digest,
            "domain_id": self.domain_id,
            "domain_uuid": self.domain_uuid,
            "libvirt_uri": self.libvirt_uri,
            "lifecycle_protocol": _LIFECYCLE_PROTOCOL,
            "name": self.record.name,
            "owner_uid": self.owner_uid,
            "plan_digest": self.plan_digest,
            "run_id": self.record.run_id,
            "runtime_kind": self.record.dispatch_key.runtime_kind.value,
            "stage1_artifact_digest": self.stage1_artifact_digest,
        }


@dataclass(frozen=True, slots=True)
class MonitorJournalSnapshot:
    binding: MonitorBinding
    monitor_generation: str
    phase: str
    revision: int
    writer: MonitorProcessIdentity

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, MonitorBinding)
            or not _canonical_uuid(self.monitor_generation)
            or self.phase not in _PHASES
            or type(self.revision) is not int
            or not 1 <= self.revision <= _MAX_REVISION
            or not isinstance(self.writer, MonitorProcessIdentity)
        ):
            raise MonitorError(MonitorErrorCategory.INVALID_IDENTITY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "monitor_generation": self.monitor_generation,
            "phase": self.phase,
            "revision": self.revision,
            "schema": _JOURNAL_SCHEMA,
            "writer": self.writer.to_dict(),
        }


def _canonical_uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (AttributeError, TypeError, ValueError):
        return False


def _canonical_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        try:
            written = os.write(descriptor, content[offset:])
        except OSError:
            raise MonitorError(MonitorErrorCategory.JOURNAL_IO) from None
        if written <= 0:
            raise MonitorError(MonitorErrorCategory.JOURNAL_IO)
        offset += written


def _read_boot_id(path: str = "/proc/sys/kernel/random/boot_id") -> str:
    try:
        with open(path, encoding="ascii") as stream:
            value = stream.read().strip()
    except OSError:
        raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH) from None
    if not _canonical_uuid(value):
        raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH)
    return value


def _parse_proc_start_ticks(content: str, expected_pid: int) -> int:
    prefix = f"{expected_pid} ("
    marker = content.rfind(") ")
    if not content.startswith(prefix) or marker < len(prefix):
        raise ValueError
    fields_from_state = content[marker + 2 :].split()
    # The suffix starts at field 3 (state); starttime is Linux proc field 22.
    if len(fields_from_state) <= 19:
        raise ValueError
    value = fields_from_state[19]
    if not value.isascii() or not value.isdigit():
        raise ValueError
    return int(value)


def _read_process_start_ticks(pid: int) -> int:
    with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
        return _parse_proc_start_ticks(stream.read(), pid)


def current_process_identity() -> MonitorProcessIdentity:
    pid = os.getpid()
    try:
        start_ticks = _read_process_start_ticks(pid)
    except (OSError, UnicodeError, ValueError):
        raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH) from None
    return MonitorProcessIdentity(pid, _read_boot_id(), start_ticks)


def probe_process_liveness(identity: MonitorProcessIdentity) -> ProcessLiveness:
    if not isinstance(identity, MonitorProcessIdentity):
        return ProcessLiveness.UNKNOWN
    try:
        boot_id = _read_boot_id()
    except MonitorError:
        return ProcessLiveness.UNKNOWN
    if boot_id != identity.host_boot_id:
        return ProcessLiveness.STALE
    try:
        start_ticks = _read_process_start_ticks(identity.pid)
    except FileNotFoundError:
        return ProcessLiveness.STALE
    except (OSError, UnicodeError, ValueError):
        return ProcessLiveness.UNKNOWN
    return ProcessLiveness.LIVE if start_ticks == identity.start_ticks else ProcessLiveness.STALE


def _decode_snapshot(content: bytes, binding: MonitorBinding) -> MonitorJournalSnapshot:
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
    if (
        type(raw) is not dict
        or set(raw) != {"binding", "monitor_generation", "phase", "revision", "schema", "writer"}
        or raw.get("schema") != _JOURNAL_SCHEMA
        or _canonical_bytes(raw) != content
        or raw.get("binding") != binding.to_dict()
    ):
        raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
    try:
        snapshot = MonitorJournalSnapshot(
            binding,
            raw["monitor_generation"],
            raw["phase"],
            raw["revision"],
            MonitorProcessIdentity.from_dict(raw["writer"]),
        )
    except (KeyError, MonitorError):
        raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
    return snapshot


class MonitorLease:
    """Exclusive descriptor-held writer for one exact monitor journal."""

    __slots__ = (
        "__weakref__",
        "_binding",
        "_closed",
        "_current_process",
        "_directory_fd",
        "_directory_identity",
        "_fork_resource",
        "_lock_fd",
        "_lock_identity",
        "_poisoned",
        "_snapshot",
        "_snapshot_bytes",
    )

    def __init__(
        self,
        directory_fd: int,
        binding: MonitorBinding,
        *,
        current_process: Callable[[], MonitorProcessIdentity],
    ) -> None:
        if type(directory_fd) is not int or not isinstance(binding, MonitorBinding) or not callable(current_process):
            raise MonitorError(MonitorErrorCategory.INVALID_IDENTITY)
        self._binding = binding
        self._closed = False
        self._current_process = current_process
        self._directory_fd = -1
        self._directory_identity: tuple[int, int] | None = None
        self._fork_resource: _ForkCloseMonitorLease | None = None
        self._lock_fd = -1
        self._lock_identity: tuple[int, int] | None = None
        self._poisoned = False
        self._snapshot: MonitorJournalSnapshot | None = None
        self._snapshot_bytes: bytes | None = None
        _FORK_REGISTRY_LOCK.acquire()
        try:
            try:
                self._directory_fd = os.dup(directory_fd)
                self._validate_directory()
                self._open_lock()
                resource = _ForkCloseMonitorLease(self, self._directory_fd, self._lock_fd)
                self._fork_resource = resource
                _FORK_MONITOR_LEASES.add(resource)
            except BaseException:
                self._closed = True
                for descriptor_name in ("_lock_fd", "_directory_fd"):
                    descriptor = getattr(self, descriptor_name)
                    setattr(self, descriptor_name, -1)
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                raise
        finally:
            _FORK_REGISTRY_LOCK.release()

    @classmethod
    def create(
        cls,
        directory_fd: int,
        binding: MonitorBinding,
        *,
        current_process: Callable[[], MonitorProcessIdentity] = current_process_identity,
        generation_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> MonitorLease:
        lease = cls(directory_fd, binding, current_process=current_process)
        try:
            if lease._read_journal(missing_ok=True) is not None:
                raise MonitorError(MonitorErrorCategory.ADOPTION_FORBIDDEN)
            writer = lease._capture_current_process()
            generation = str(generation_factory())
            snapshot = MonitorJournalSnapshot(binding, generation, "starting", 1, writer)
            lease._publish(snapshot, create=True)
            return lease
        except BaseException:
            lease.close()
            raise

    @classmethod
    def adopt(
        cls,
        directory_fd: int,
        binding: MonitorBinding,
        *,
        current_process: Callable[[], MonitorProcessIdentity] = current_process_identity,
        liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
        generation_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> MonitorLease:
        lease = cls(directory_fd, binding, current_process=current_process)
        try:
            loaded = lease._read_journal(missing_ok=False)
            assert loaded is not None
            previous, previous_bytes = loaded
            lease._snapshot = previous
            lease._snapshot_bytes = previous_bytes
            if previous.phase in {"control-lost", "terminal"}:
                raise MonitorError(MonitorErrorCategory.ADOPTION_FORBIDDEN)
            try:
                liveness = liveness_probe(previous.writer)
            except Exception:
                liveness = ProcessLiveness.UNKNOWN
            if liveness is ProcessLiveness.LIVE:
                raise MonitorError(MonitorErrorCategory.WRITER_LIVE)
            if liveness is not ProcessLiveness.STALE:
                raise MonitorError(MonitorErrorCategory.WRITER_UNKNOWN)
            writer = lease._capture_current_process()
            generation = str(generation_factory())
            adopted = MonitorJournalSnapshot(binding, generation, "adopting", previous.revision + 1, writer)
            lease._publish(adopted, create=False)
            return lease
        except BaseException:
            lease.close()
            raise

    @property
    def snapshot(self) -> MonitorJournalSnapshot:
        self._require_authority()
        assert self._snapshot is not None
        return self._snapshot

    def mark_running(self) -> MonitorJournalSnapshot:
        # Lifecycle v2's boot key is intentionally memory-only.  A stale
        # adopter cannot authenticate a reconnect, so only the original
        # starting writer may claim running authority.
        return self._transition("running", allowed=frozenset({"starting"}))

    def mark_control_lost(self) -> MonitorJournalSnapshot:
        """End a stale takeover as recovery-only ownership.

        A future integration may use this state to perform exact, conservative
        domain cleanup.  It may not issue authenticated lifecycle control or
        promote the guest back to running.
        """

        return self._transition("control-lost", allowed=frozenset({"adopting"}))

    def mark_terminal(self) -> MonitorJournalSnapshot:
        return self._transition("terminal", allowed=frozenset({"running"}))

    def _transition(self, phase: str, *, allowed: frozenset[str]) -> MonitorJournalSnapshot:
        self._require_authority()
        assert self._snapshot is not None
        if self._snapshot.phase not in allowed:
            raise MonitorError(MonitorErrorCategory.INVALID_TRANSITION)
        candidate = MonitorJournalSnapshot(
            self._binding,
            self._snapshot.monitor_generation,
            phase,
            self._snapshot.revision + 1,
            self._snapshot.writer,
        )
        self._publish(candidate, create=False)
        return candidate

    def _capture_current_process(self) -> MonitorProcessIdentity:
        try:
            identity = self._current_process()
        except MonitorError:
            raise
        except Exception:
            raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH) from None
        if not isinstance(identity, MonitorProcessIdentity):
            raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH)
        return identity

    def _validate_directory(self) -> None:
        try:
            metadata = os.fstat(self._directory_fd)
        except OSError:
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
        if self._directory_identity is None:
            self._directory_identity = _identity(metadata)
        if (
            not stat_module.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat_module.S_IMODE(metadata.st_mode) != 0o700
            or _identity(metadata) != self._directory_identity
        ):
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)

    def _open_lock(self) -> None:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        created = False
        try:
            self._lock_fd = os.open(_LOCK_NAME, flags, dir_fd=self._directory_fd)
        except FileNotFoundError:
            try:
                self._lock_fd = os.open(_LOCK_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._directory_fd)
                os.fchmod(self._lock_fd, 0o600)
                created = True
            except OSError:
                raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
        except OSError:
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
        self._validate_lock()
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MonitorError(MonitorErrorCategory.JOURNAL_BUSY) from None
        self._validate_lock()
        if created:
            try:
                os.fsync(self._lock_fd)
                os.fsync(self._directory_fd)
            except OSError:
                raise MonitorError(MonitorErrorCategory.JOURNAL_IO) from None

    def _validate_lock(self) -> None:
        self._validate_directory()
        try:
            opened = os.fstat(self._lock_fd)
            visible = os.stat(_LOCK_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
        except OSError:
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
        if self._lock_identity is None:
            self._lock_identity = _identity(opened)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or not stat_module.S_ISREG(visible.st_mode)
            or opened.st_uid != os.geteuid()
            or visible.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or visible.st_nlink != 1
            or stat_module.S_IMODE(opened.st_mode) != 0o600
            or stat_module.S_IMODE(visible.st_mode) != 0o600
            or _identity(opened) != self._lock_identity
            or _identity(visible) != self._lock_identity
        ):
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)

    def _read_journal(self, *, missing_ok: bool) -> tuple[MonitorJournalSnapshot, bytes] | None:
        self._validate_lock()
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        descriptor = -1
        try:
            try:
                descriptor = os.open(_JOURNAL_NAME, flags, dir_fd=self._directory_fd)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
            opened = os.fstat(descriptor)
            visible = os.stat(_JOURNAL_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
            if (
                not stat_module.S_ISREG(opened.st_mode)
                or not stat_module.S_ISREG(visible.st_mode)
                or opened.st_uid != os.geteuid()
                or visible.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or visible.st_nlink != 1
                or stat_module.S_IMODE(opened.st_mode) != 0o600
                or stat_module.S_IMODE(visible.st_mode) != 0o600
                or _identity(opened) != _identity(visible)
                or opened.st_size <= 0
                or opened.st_size > _MAX_JOURNAL_BYTES
            ):
                raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
            content = os.read(descriptor, _MAX_JOURNAL_BYTES + 1)
            after = os.fstat(descriptor)
            current = os.stat(_JOURNAL_NAME, dir_fd=self._directory_fd, follow_symlinks=False)
            if (
                len(content) != opened.st_size
                or _identity(after) != _identity(opened)
                or _identity(current) != _identity(opened)
            ):
                raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
        except MonitorError:
            raise
        except OSError:
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    self._poisoned = True
                    raise MonitorError(MonitorErrorCategory.JOURNAL_IO) from None
        snapshot = _decode_snapshot(content, self._binding)
        return snapshot, content

    def _unlink_exact_temporary(self, name: str, expected_identity: tuple[int, int]) -> None:
        try:
            current = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            if _identity(current) == expected_identity and stat_module.S_ISREG(current.st_mode):
                os.unlink(name, dir_fd=self._directory_fd)
        except OSError:
            pass

    def _publish(self, snapshot: MonitorJournalSnapshot, *, create: bool) -> None:
        try:
            self._validate_lock()
            if not create:
                expected_snapshot = self._snapshot
                expected_bytes = self._snapshot_bytes
                loaded = self._read_journal(missing_ok=False)
                assert loaded is not None
                current, current_bytes = loaded
                if (
                    expected_snapshot is None
                    or expected_bytes is None
                    or current != expected_snapshot
                    or current_bytes != expected_bytes
                ):
                    raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
        except MonitorError:
            self._poisoned = True
            raise
        content = _canonical_bytes(snapshot.to_dict())
        temporary = f".oci-monitor-{uuid.uuid4().hex}.tmp"
        descriptor = -1
        temp_identity: tuple[int, int] | None = None
        published = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            temp_metadata = os.fstat(descriptor)
            temp_identity = _identity(temp_metadata)
            if (
                not stat_module.S_ISREG(temp_metadata.st_mode)
                or temp_metadata.st_uid != os.geteuid()
                or temp_metadata.st_nlink != 1
                or stat_module.S_IMODE(temp_metadata.st_mode) != 0o600
            ):
                raise MonitorError(MonitorErrorCategory.JOURNAL_IO)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            after_write = os.fstat(descriptor)
            if _identity(after_write) != temp_identity or after_write.st_size != len(content):
                raise MonitorError(MonitorErrorCategory.JOURNAL_IO)
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
                temporary = ""
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
            loaded = self._read_journal(missing_ok=False)
            if loaded is None or loaded != (snapshot, content):
                raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
            self._snapshot = snapshot
            self._snapshot_bytes = content
        except FileExistsError:
            self._poisoned = True
            raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL) from None
        except MonitorError:
            self._poisoned = True
            raise
        except OSError as exc:
            self._poisoned = True
            category = (
                MonitorErrorCategory.INVALID_JOURNAL
                if create and exc.errno == errno.EEXIST
                else MonitorErrorCategory.JOURNAL_IO
            )
            raise MonitorError(category) from None
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary and temp_identity is not None:
                self._unlink_exact_temporary(temporary, temp_identity)
            # After publication, especially after directory-fsync failure, the
            # visible journal may be either the old or new durable state after a
            # crash.  The live handle is poisoned and retains its lock; it never
            # reports the transition as committed or attempts another write.
            if published and self._poisoned:
                self._snapshot = None
                self._snapshot_bytes = None

    def _require_authority(self) -> None:
        if self._closed:
            raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH)
        if self._poisoned:
            raise MonitorError(MonitorErrorCategory.POISONED)
        try:
            self._validate_lock()
            assert self._snapshot is not None and self._snapshot_bytes is not None
            if self._capture_current_process() != self._snapshot.writer:
                raise MonitorError(MonitorErrorCategory.PROCESS_MISMATCH)
            expected_snapshot = self._snapshot
            expected_bytes = self._snapshot_bytes
            loaded = self._read_journal(missing_ok=False)
            if loaded is None or loaded != (expected_snapshot, expected_bytes):
                raise MonitorError(MonitorErrorCategory.INVALID_JOURNAL)
        except MonitorError:
            self._poisoned = True
            raise

    def close(self) -> None:
        _FORK_REGISTRY_LOCK.acquire()
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
                _FORK_MONITOR_LEASES.discard(resource)
            # Keep the registry lock through the actual closes. Otherwise a
            # concurrent fork could inherit these descriptors after their
            # registry entry was removed but before they were closed.
            for descriptor in (lock_fd, directory_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        finally:
            _FORK_REGISTRY_LOCK.release()


__all__ = [
    "MonitorBinding",
    "MonitorError",
    "MonitorErrorCategory",
    "MonitorJournalSnapshot",
    "MonitorLease",
    "MonitorProcessIdentity",
    "ProcessLiveness",
    "current_process_identity",
    "probe_process_liveness",
]
