"""Private inactive-definition cleanup; never adopt a monitor or stop a guest.

The caller supplies the trusted run roots and preactivation binding. Durable
intent makes an interrupted undefine resumable, but never authorizes deleting
a definition which reappears after completion. No store or boot files are read.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import threading
import uuid
import weakref
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from . import kvm
from .errors import StateError
from .oci_monitor import MonitorProcessIdentity, ProcessLiveness, probe_process_liveness
from .oci_monitor_ipc import (
    _JOURNAL_NAME,
    _LOCK_NAME,
    MonitorPreActivationBinding,
    MonitorPreactivationJournalSnapshot,
    _read_preactivation_journal,
)
from .oci_root_runtime import OCI_ROOT_DEFINITION_SCHEMA, _domain_projection, _projection_digest
from .state import ExistingRunMutation, StatePaths, locked_existing_run

_SCHEMA = "palimpsest.oci-monitor-inactive-cleanup.v1"
_STATE_KEY = "oci_monitor_inactive_cleanup"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORK_LOCK = threading.Lock()
_AUTHORITIES: weakref.WeakSet[_RecoveryAuthority] = weakref.WeakSet()


def _close_after_fork() -> None:
    try:
        for authority in tuple(_AUTHORITIES):
            authority._close_descriptors()
        _AUTHORITIES.clear()
    finally:
        _FORK_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_LOCK.acquire,
        after_in_parent=_FORK_LOCK.release,
        after_in_child=_close_after_fork,
    )


class MonitorInactiveCleanupError(StateError):
    """A path-free refusal or ambiguous cleanup result."""


def _uuid(value: object) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class MonitorInactiveCleanupReceipt:
    binding: MonitorPreActivationBinding
    cleanup_id: str
    journal_digest: str
    monitor_generation: str
    journal_revision: int
    journal_device: int
    journal_inode: int
    writer: MonitorProcessIdentity
    phase: str

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not MonitorPreActivationBinding
            or not _uuid(self.cleanup_id)
            or type(self.journal_digest) is not str
            or _DIGEST.fullmatch(self.journal_digest) is None
            or not _uuid(self.monitor_generation)
            or type(self.journal_revision) is not int
            or not 1 <= self.journal_revision <= 2**63 - 1
            or type(self.journal_device) is not int
            or self.journal_device < 0
            or type(self.journal_inode) is not int
            or self.journal_inode <= 0
            or type(self.writer) is not MonitorProcessIdentity
            or self.phase not in {"intent", "completed"}
        ):
            raise MonitorInactiveCleanupError("invalid cleanup receipt")
        self.binding.__post_init__()
        self.writer.__post_init__()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "phase": self.phase,
            "cleanup_id": self.cleanup_id,
            "binding": self.binding.to_dict(),
            "binding_digest": self.binding.digest,
            "journal_digest": self.journal_digest,
            "monitor_generation": self.monitor_generation,
            "journal_revision": self.journal_revision,
            "journal_device": self.journal_device,
            "journal_inode": self.journal_inode,
            "writer": self.writer.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> MonitorInactiveCleanupReceipt:
        try:
            if (
                not isinstance(value, Mapping)
                or set(value)
                != {
                    "schema",
                    "phase",
                    "cleanup_id",
                    "binding",
                    "binding_digest",
                    "journal_digest",
                    "monitor_generation",
                    "journal_revision",
                    "journal_device",
                    "journal_inode",
                    "writer",
                }
                or value["schema"] != _SCHEMA
            ):
                raise ValueError
            binding = MonitorPreActivationBinding.from_dict(dict(value["binding"]))
            writer = value["writer"]
            if not isinstance(writer, Mapping) or set(writer) != {"pid", "host_boot_id", "start_ticks"}:
                raise ValueError
            receipt = cls(
                binding,
                value["cleanup_id"],
                value["journal_digest"],
                value["monitor_generation"],
                value["journal_revision"],
                value["journal_device"],
                value["journal_inode"],
                MonitorProcessIdentity.from_dict(writer),
                value["phase"],
            )
            if value["binding_digest"] != binding.digest:
                raise ValueError
            return receipt
        except Exception:
            raise MonitorInactiveCleanupError("invalid cleanup receipt") from None


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _private(info: os.stat_result, *, directory: bool) -> bool:
    return (
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
        and (directory or info.st_nlink == 1)
    )


class _RecoveryAuthority:
    """Existing-only journal flock; no writer takeover and no journal writes."""

    def __init__(self, mutation: ExistingRunMutation, binding: MonitorPreActivationBinding) -> None:
        self.mutation = mutation
        self.binding = binding
        self.pid = os.getpid()
        self.directory_fd = -1
        self.lock_fd = -1
        self.journal_fd = -1
        _FORK_LOCK.acquire()
        try:
            self.directory_fd = os.open(
                "monitor-private",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=mutation._run_fd,
            )
            self.directory_identity = _identity(os.fstat(self.directory_fd))
            self.lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=self.directory_fd,
            )
            self.lock_identity = _identity(os.fstat(self.lock_fd))
            self.validate()
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MonitorInactiveCleanupError("monitor journal is busy") from None
            self.validate()
            self.journal_fd = os.open(
                _JOURNAL_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=self.directory_fd,
            )
            self.journal_metadata = os.fstat(self.journal_fd)
            loaded = _read_preactivation_journal(self.directory_fd, binding)
            assert loaded is not None
            self.snapshot, self.content = loaded
            self.validate()
            _AUTHORITIES.add(self)
        except BaseException:
            self._close_descriptors()
            raise
        finally:
            _FORK_LOCK.release()

    def validate(self) -> None:
        if os.getpid() != self.pid:
            raise MonitorInactiveCleanupError("cleanup authority changed")
        self.mutation.verify_binding()
        for descriptor, name, parent, expected, directory in (
            (self.directory_fd, "monitor-private", self.mutation._run_fd, self.directory_identity, True),
            (self.lock_fd, _LOCK_NAME, self.directory_fd, self.lock_identity, False),
        ):
            opened = os.fstat(descriptor)
            visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not _private(opened, directory=directory) or not _private(visible, directory=directory):
                raise MonitorInactiveCleanupError("cleanup authority changed")
            if _identity(opened) != expected or _identity(visible) != expected:
                raise MonitorInactiveCleanupError("cleanup authority changed")
        if hasattr(self, "snapshot"):
            opened = os.fstat(self.journal_fd)
            visible = os.stat(_JOURNAL_NAME, dir_fd=self.directory_fd, follow_symlinks=False)
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
            if (
                any(
                    getattr(info, field) != getattr(self.journal_metadata, field)
                    for info in (opened, visible)
                    for field in fields
                )
                or os.pread(self.journal_fd, len(self.content) + 1, 0) != self.content
            ):
                raise MonitorInactiveCleanupError("monitor journal changed")
            if _read_preactivation_journal(self.directory_fd, self.binding) != (self.snapshot, self.content):
                raise MonitorInactiveCleanupError("monitor journal changed")
        self.mutation.verify_binding()

    def close(self) -> None:
        with _FORK_LOCK:
            _AUTHORITIES.discard(self)
            self._close_descriptors()

    def _close_descriptors(self) -> None:
        for name in ("journal_fd", "lock_fd", "directory_fd"):
            descriptor = getattr(self, name)
            setattr(self, name, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _stale(snapshot: MonitorPreactivationJournalSnapshot, probe: Callable) -> None:
    try:
        result = probe(snapshot.writer)
    except Exception:
        result = ProcessLiveness.UNKNOWN
    if result is ProcessLiveness.LIVE:
        raise MonitorInactiveCleanupError("monitor writer is live")
    if result is not ProcessLiveness.STALE:
        raise MonitorInactiveCleanupError("monitor writer liveness is unknown")


def _validate_ledger(mutation: ExistingRunMutation, binding: MonitorPreActivationBinding, journal) -> None:
    state = mutation.mutable_state()
    definition = state.get("oci_root_definition")
    expected_definition = {
        "schema": OCI_ROOT_DEFINITION_SCHEMA,
        "phase": "defined",
        "domain_uuid": binding.domain_uuid,
        "plan_digest": binding.plan_digest,
        "projection_digest": binding.expected_definition_projection_digest,
        "libvirt_uri": binding.libvirt_uri,
    }
    if definition != expected_definition or mutation.record != binding.record:
        raise MonitorInactiveCleanupError("cleanup definition binding changed")
    if journal.phase not in {"active", "ready", "terminal", "control-lost"} or journal.active_binding is None:
        raise MonitorInactiveCleanupError("monitor journal has no captured boot instance")
    handoff = state.get("oci_root_handoff")
    fixed = {
        "schema": "palimpsest.oci-root-handoff.v1",
        "boot_attempt_id": binding.boot_attempt_id,
        "domain_uuid": binding.domain_uuid,
        "libvirt_uri": binding.libvirt_uri,
        "plan_digest": binding.plan_digest,
    }
    if not isinstance(handoff, dict) or any(handoff.get(key) != item for key, item in fixed.items()):
        raise MonitorInactiveCleanupError("cleanup handoff binding changed")
    if set(handoff) - (set(fixed) | {"phase", "domain_id", "lifecycle"}):
        raise MonitorInactiveCleanupError("cleanup handoff shape is invalid")
    pair = (state.get("status"), handoff.get("phase"))
    allowed = {
        "active": {("starting", "activating"), ("starting", "starting"), ("running", "ready")},
        "ready": {("running", "ready"), ("exited", "terminal")},
        "terminal": {("exited", "terminal")},
        "control-lost": {
            ("starting", "activating"),
            ("starting", "starting"),
            ("running", "ready"),
            ("exited", "terminal"),
        },
    }
    if journal.phase != "terminal":
        allowed[journal.phase] |= {
            ("failed", phase) for phase in ("failed", "cleanup-required", "cleanup-not-attempted")
        }
    if pair not in allowed[journal.phase]:
        raise MonitorInactiveCleanupError("cleanup lifecycle state is unsupported")
    domain_id = handoff.get("domain_id")
    if "domain_id" in handoff and (type(domain_id) is not int or domain_id != journal.active_binding.domain_id):
        raise MonitorInactiveCleanupError("cleanup boot instance changed")
    if pair[1] in {"starting", "ready", "terminal"} and domain_id is None:
        raise MonitorInactiveCleanupError("cleanup boot instance is missing")
    if pair[1] == "activating" and (domain_id is not None or "lifecycle" in handoff):
        raise MonitorInactiveCleanupError("cleanup activation state is invalid")
    lifecycle = handoff.get("lifecycle")
    if pair[1] == "starting" and lifecycle is not None:
        raise MonitorInactiveCleanupError("cleanup starting state is invalid")
    if journal.phase == "ready" and (domain_id is None or lifecycle is None):
        raise MonitorInactiveCleanupError("cleanup READY boot receipt is missing")
    if pair[1] in {"ready", "terminal"} and lifecycle is None:
        raise MonitorInactiveCleanupError("cleanup lifecycle receipt is missing")
    if lifecycle is not None:
        expected_phase = "terminal" if pair == ("exited", "terminal") else "ready"
        if (
            domain_id is None
            or not isinstance(lifecycle, dict)
            or set(lifecycle)
            != {"schema", "boot_attempt_id", "boot_generation", "key_id", "phase", "terminal", "transcript"}
            or lifecycle["schema"] != "palimpsest.oci-root-handoff.v1"
            or lifecycle["boot_attempt_id"] != binding.boot_attempt_id
            or not _uuid(lifecycle["boot_generation"])
            or type(lifecycle["key_id"]) is not str
            or _DIGEST.fullmatch(lifecycle["key_id"]) is None
            or lifecycle["phase"] != expected_phase
            or not isinstance(lifecycle["transcript"], list)
            or any(not isinstance(item, dict) for item in lifecycle["transcript"])
            or (expected_phase == "ready" and lifecycle["terminal"] is not None)
            or (expected_phase == "terminal" and not isinstance(lifecycle["terminal"], dict))
        ):
            raise MonitorInactiveCleanupError("cleanup lifecycle receipt is invalid")
        if expected_phase == "terminal":
            terminal = lifecycle["terminal"]
            if set(terminal) != {"category", "exit_code", "returncode", "signal_number"}:
                raise MonitorInactiveCleanupError("cleanup terminal receipt is invalid")
            code, sig, returned = terminal["exit_code"], terminal["signal_number"], terminal["returncode"]
            exited = (
                terminal["category"] == "exited"
                and type(code) is int
                and 0 <= code <= 255
                and sig is None
                and returned == code
            )
            signaled = (
                terminal["category"] == "signaled"
                and code is None
                and type(sig) is int
                and 1 <= sig <= 64
                and returned == -sig
            )
            if type(returned) is not int or not (exited or signaled):
                raise MonitorInactiveCleanupError("cleanup terminal receipt is invalid")


def _lookup(conn: Any, name: str, value: str) -> Any | None:
    libvirt = kvm._libvirt()
    try:
        result = getattr(conn, name)(value)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return None
        raise MonitorInactiveCleanupError("domain lookup is ambiguous") from None
    if result is None:
        raise MonitorInactiveCleanupError("domain lookup is ambiguous")
    return result


def _inspect_domain(conn: Any, binding: MonitorPreActivationBinding) -> Any | None:
    if conn.getURI() != binding.libvirt_uri:
        raise MonitorInactiveCleanupError("cleanup connection URI changed")
    by_name = _lookup(conn, "lookupByName", binding.record.name)
    by_uuid = _lookup(conn, "lookupByUUIDString", binding.domain_uuid)
    if by_name is None and by_uuid is None:
        return None
    if by_name is None or by_uuid is None:
        raise MonitorInactiveCleanupError("domain name and UUID disagree")
    inactive_flag = kvm._libvirt().VIR_DOMAIN_XML_INACTIVE
    if type(inactive_flag) is not int:
        raise MonitorInactiveCleanupError("inactive domain inspection is unavailable")
    for domain in (by_name, by_uuid):
        if domain.UUIDString() != binding.domain_uuid or domain.name() != binding.record.name:
            raise MonitorInactiveCleanupError("domain name and UUID disagree")
        xml = domain.XMLDesc(inactive_flag)
        root = ET.fromstring(xml)
        uuids = root.findall("./uuid")
        if len(uuids) != 1 or uuids[0].text != binding.domain_uuid:
            raise MonitorInactiveCleanupError("inactive XML UUID changed")
        markers = root.findall(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
        if len(markers) != 1 or markers[0].attrib != {
            "contract": binding.plan_digest,
            "id": binding.record.run_id,
            "schema": "1",
            "version": kvm.DOMAIN_MARKER_VERSION,
        }:
            raise MonitorInactiveCleanupError("domain owner marker changed")
        if _projection_digest(_domain_projection(xml)) != binding.expected_definition_projection_digest:
            raise MonitorInactiveCleanupError("inactive domain projection changed")
        persistent, active, domain_id = domain.isPersistent(), domain.isActive(), domain.ID()
        if type(persistent) is not int or persistent != 1:
            raise MonitorInactiveCleanupError("domain is not persistent")
        if type(active) is not int or active != 0 or type(domain_id) is not int or domain_id != -1:
            raise MonitorInactiveCleanupError("domain is not exactly inactive")
    return by_name


def reconcile_inactive_monitor_domain(
    roots: StatePaths,
    binding: MonitorPreActivationBinding,
    *,
    conn: Any,
    liveness_probe: Callable[[MonitorProcessIdentity], ProcessLiveness] = probe_process_liveness,
) -> MonitorInactiveCleanupReceipt:
    """Undefine only an exact inactive persistent domain owned by a stale writer.

    No VM destroy, key adoption, socket removal, volume deletion, or launch is
    possible here. A separate external administrator can race libvirt checks;
    the API guarantees revalidation, not an atomic hypervisor compare-and-swap.
    """
    authority = None
    try:
        if (
            type(roots) is not StatePaths
            or type(binding) is not MonitorPreActivationBinding
            or not callable(liveness_probe)
        ):
            raise MonitorInactiveCleanupError("invalid cleanup authority")
        binding.__post_init__()
        if binding.owner_uid != os.geteuid():
            raise MonitorInactiveCleanupError("cleanup owner changed")
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            authority = _RecoveryAuthority(mutation, binding)
            journal = authority.snapshot
            _validate_ledger(mutation, binding, journal)

            def verify() -> None:
                authority.validate()
                _validate_ledger(mutation, binding, journal)
                _stale(journal, liveness_probe)
                authority.validate()
                if conn.getURI() != binding.libvirt_uri:
                    raise MonitorInactiveCleanupError("cleanup connection URI changed")

            verify()
            template = MonitorInactiveCleanupReceipt(
                binding,
                str(uuid.uuid4()),
                "sha256:" + hashlib.sha256(authority.content).hexdigest(),
                journal.identity.generation,
                journal.revision,
                authority.journal_metadata.st_dev,
                authority.journal_metadata.st_ino,
                journal.writer,
                "intent",
            )
            saved = mutation.snapshot.state.get(_STATE_KEY)
            has_saved = _STATE_KEY in mutation.snapshot.state
            receipt = MonitorInactiveCleanupReceipt.from_dict(saved) if has_saved else template
            if replace(receipt, cleanup_id=template.cleanup_id, phase="intent") != template:
                raise MonitorInactiveCleanupError("cleanup evidence changed")
            domain = _inspect_domain(conn, binding)
            verify()
            if receipt.phase == "completed":
                if domain is not None:
                    raise MonitorInactiveCleanupError("domain reappeared after cleanup")
                return receipt
            if not has_saved:
                if domain is None:
                    raise MonitorInactiveCleanupError("absent domain has no prior cleanup intent")
                data = mutation.mutable_state()
                data[_STATE_KEY] = receipt.to_dict()
                mutation.write_state(data["status"], data)
            verify()
            domain = _inspect_domain(conn, binding)
            verify()
            if domain is not None:
                # Re-read domain last, then re-read pinned authority. Never
                # destroy: undefine affects the persistent definition only.
                domain = _inspect_domain(conn, binding)
                verify()
                if domain is None:
                    raise MonitorInactiveCleanupError("domain changed before undefine")
                result = domain.undefine()
                if type(result) is not int or result != 0:
                    raise MonitorInactiveCleanupError("domain undefine result is ambiguous")
            verify()
            if _inspect_domain(conn, binding) is not None:
                raise MonitorInactiveCleanupError("domain absence could not be proven")
            verify()
            receipt = replace(receipt, phase="completed")
            data = mutation.mutable_state()
            data[_STATE_KEY] = receipt.to_dict()
            mutation.write_state(data["status"], data)
            verify()
            return receipt
    except MonitorInactiveCleanupError:
        raise
    except Exception:
        raise MonitorInactiveCleanupError("inactive monitor cleanup could not be verified") from None
    finally:
        if authority is not None:
            authority.close()


__all__ = ["MonitorInactiveCleanupError", "MonitorInactiveCleanupReceipt", "reconcile_inactive_monitor_domain"]
