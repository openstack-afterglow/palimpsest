"""Private set-based traversal of the exact state/runs namespace.

The namespace lock precedes every run lock. Read-only launch validation never
takes that lock: atomic registry snapshots contain immutable member identities,
not a revision pinned by another VM. Empty registries and enrollment persist;
only crash-orphaned left members remain until a future explicit repair.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import threading
import uuid
import weakref
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace

from .errors import StateError
from .oci_acl import LinuxFdACLBackend, baseline_acl, traversal_acl
from .oci_monitor import probe_process_liveness
from .oci_monitor_ipc import MonitorPreActivationBinding
from .oci_monitor_recovery import (
    MonitorInactiveCleanupReceipt,
    _inspect_domain,
    _RecoveryAuthority,
    _stale,
    _validate_ledger,
)
from .oci_provenance import canonical_json_bytes
from .oci_runtime_access import (
    RuntimeAccessReceipt,
    RuntimeAccessTarget,
    _digest,
    _grant_authority,
    _pinned_io,
    _source_io,
    _validate_target,
    _verify_pinned,
    _verify_pinned_metadata,
)
from .state import locked_existing_run

SHARED_TRAVERSAL_STATE_KEY = "oci_shared_traversal"
_REGISTRY = "oci-shared-traversal.json"
_MARKER = "oci-shared-traversal.enrolled.json"
_LOCK = "oci-shared-traversal.lock"
_SCHEMA = "palimpsest.oci-shared-traversal.v1"
_MEMBER_SCHEMA = "palimpsest.oci-shared-traversal-member.v1"
_MAX_REGISTRY_BYTES = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FORK_LOCK = threading.Lock()
_HOLDERS = weakref.WeakSet()


def _invalid():
    return StateError("OCI shared traversal authority is invalid or changed")


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True, slots=True)
class SharedTraversalMembership:
    namespace_id: str
    epoch: int
    access_id: str
    binding: MonitorPreActivationBinding
    state: RuntimeAccessTarget
    runs: RuntimeAccessTarget
    qemu_uid: int
    qemu_gid: int
    phase: str

    def __post_init__(self):
        if (
            not _uuid(self.namespace_id)
            or not _uuid(self.access_id)
            or type(self.epoch) is not int
            or self.epoch < 1
            or type(self.binding) is not MonitorPreActivationBinding
            or type(self.phase) is not str
            or self.phase not in {"joining", "active", "leaving", "left"}
            or any(type(x) is not int or not 0 < x < 2**32 - 1 for x in (self.qemu_uid, self.qemu_gid))
            or self.qemu_uid == self.binding.owner_uid
        ):
            raise _invalid()
        MonitorPreActivationBinding.__post_init__(self.binding)
        for target in (self.state, self.runs):
            if type(target) is not RuntimeAccessTarget:
                raise _invalid()
            RuntimeAccessTarget.__post_init__(target)
            if (
                not target.directory
                or target.nlink < 2
                or target.uid != self.binding.owner_uid
                or target.granted != traversal_acl(target.baseline, self.qemu_uid)
            ):
                raise _invalid()
        if (self.state.device, self.state.inode) == (self.runs.device, self.runs.inode):
            raise _invalid()

    def to_dict(self):
        return {
            "schema": _MEMBER_SCHEMA,
            **{
                key: value.to_dict() if key in {"binding", "state", "runs"} else value
                for key, value in ((key, getattr(self, key)) for key in self.__dataclass_fields__)
            },
        }

    @classmethod
    def from_dict(cls, value):
        try:
            if (
                not isinstance(value, Mapping)
                or set(value) != {"schema", *cls.__dataclass_fields__}
                or value["schema"] != _MEMBER_SCHEMA
            ):
                raise _invalid()
            fields = {k: v for k, v in value.items() if k != "schema"}
            fields["binding"] = MonitorPreActivationBinding.from_dict(dict(fields["binding"]))
            for key in ("state", "runs"):
                fields[key] = RuntimeAccessTarget.from_dict(fields[key])
            return cls(**fields)
        except (TypeError, ValueError, KeyError):
            raise _invalid() from None


def _key(member):
    return member.binding.record.run_id + ":" + member.access_id


def _same_member(a, b):
    return replace(a, phase="active") == replace(b, phase="active")


def _validate_registry(value):
    if (
        type(value) is not dict
        or set(value)
        != {"schema", "namespace_id", "epoch", "qemu_uid", "qemu_gid", "state", "runs", "members", "pending"}
        or value["schema"] != _SCHEMA
        or not _uuid(value["namespace_id"])
        or type(value["epoch"]) is not int
        or value["epoch"] < 0
        or type(value["members"]) is not dict
    ):
        raise _invalid()
    targets = {key: RuntimeAccessTarget.from_dict(value[key]) for key in ("state", "runs")}
    for target in targets.values():
        if (
            not target.directory
            or target.nlink < 2
            or target.uid != os.geteuid()
            or type(value["qemu_uid"]) is not int
            or value["qemu_uid"] in {0, os.geteuid()}
            or type(value["qemu_gid"]) is not int
            or not 0 < value["qemu_gid"] < 2**32 - 1
            or target.granted != traversal_acl(target.baseline, value["qemu_uid"])
        ):
            raise _invalid()
    members = [SharedTraversalMembership.from_dict(item) for item in value["members"].values()]
    if any(
        key != _key(member) or member.phase not in {"active", "left"}
        for key, member in zip(value["members"], members, strict=True)
    ):
        raise _invalid()
    pending = value["pending"]
    if pending is not None:
        pending = SharedTraversalMembership.from_dict(pending)
        if pending.phase not in {"joining", "leaving"}:
            raise _invalid()
        prior = value["members"].get(_key(pending))
        if pending.phase == "joining" and prior is not None:
            raise _invalid()
        if pending.phase == "leaving" and (
            prior is None
            or not _same_member(SharedTraversalMembership.from_dict(prior), pending)
            or prior["phase"] != "active"
        ):
            raise _invalid()
        members.append(pending)
    for member in members:
        if (
            member.namespace_id != value["namespace_id"]
            or member.epoch > value["epoch"]
            or (member.qemu_uid, member.qemu_gid) != (value["qemu_uid"], value["qemu_gid"])
            or member.state != targets["state"]
            or member.runs != targets["runs"]
        ):
            raise _invalid()
    active = [item for item in members if item.phase == "active"]
    if len({member.binding.record.run_id for member in active}) != len(active):
        raise _invalid()
    return value


def _active(registry):
    return [item for item in registry["members"].values() if item["phase"] == "active"]


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IFMT(info.st_mode)


def _file_signature(info):
    # A read can update atime under Linux relatime; that is not authority drift.
    return (_identity(info), info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_absolute(path):
    # Configured system ancestors may legitimately be aliases (macOS /var).
    # The exact selected state component is never followed or silently adopted.
    return os.open(path, _DIR_FLAGS)


class _Namespace:
    """One pinned namespace, optionally holding its exclusive mutation lock."""

    def __init__(self, roots, *, create=False, locked=False, recover_marker=False):
        self.roots = roots
        self.pid = os.getpid()
        self.fds = {}
        self.identities = {}
        self.content = None
        self.registry = None
        self.registry_identity = None
        self.marker = None
        self.marker_content = None
        self.marker_identity = None
        self._recover_marker = recover_marker
        self._initializing = create
        with _FORK_LOCK:
            _HOLDERS.add(self)
        try:
            if create:
                # Parent creation does not grant access or repair any existing mode.
                roots.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.mkdir(roots.state, 0o700)
                except FileExistsError:
                    pass
            self._hold("state", lambda: _open_absolute(roots.state))
            self.verify_metadata()
            create_children = create
            for role in ("runs", "locks"):
                if create_children:
                    try:
                        os.mkdir(role, 0o700, dir_fd=self.fds["state"])
                        os.fsync(self.fds["state"])
                    except FileExistsError:
                        pass
                self._hold(role, lambda role=role: os.open(role, _DIR_FLAGS, dir_fd=self.fds["state"]))
            self.verify_metadata()
            if locked:
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC

                def open_lock():
                    try:
                        os.stat(_REGISTRY, dir_fd=self.fds["locks"], follow_symlinks=False)
                    except FileNotFoundError:
                        try:
                            os.stat(_MARKER, dir_fd=self.fds["locks"], follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            return os.open(_LOCK, flags, dir_fd=self.fds["locks"])
                        try:
                            fd = os.open(_LOCK, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fds["locks"])
                            try:
                                os.fsync(fd)
                                os.fsync(self.fds["locks"])
                            except BaseException:
                                os.close(fd)
                                raise
                            return fd
                        except FileExistsError:
                            pass
                    return os.open(_LOCK, flags, dir_fd=self.fds["locks"])

                self._hold("lock", open_lock)
                fd = self.fds["lock"]
                self.verify_metadata()
                fcntl.flock(fd, fcntl.LOCK_EX)
                self.verify_metadata()
            self.reload()
        except BaseException:
            self.close()
            raise

    def _hold(self, role, opener):
        with _FORK_LOCK:
            if self.pid != os.getpid():
                raise _invalid()
            fd = opener()
            try:
                self.identities[role] = _identity(os.fstat(fd))
                self.fds[role] = fd
            except BaseException:
                os.close(fd)
                raise

    def verify_metadata(self):
        if self.pid != os.getpid() or not self.fds:
            raise _invalid()
        unmanaged_initialization = (
            self._initializing and self.content is None and self.registry is None and self.marker is None
        )
        for role, fd in self.fds.items():
            visible = os.stat(
                self.roots.state if role == "state" else (_LOCK if role == "lock" else role),
                dir_fd=None if role == "state" else self.fds["locks" if role == "lock" else "state"],
                follow_symlinks=False,
            )
            for info in (os.fstat(fd), visible):
                if (
                    _identity(info) != self.identities[role]
                    or info.st_uid != os.geteuid()
                    or (not stat.S_ISREG(info.st_mode) if role == "lock" else not stat.S_ISDIR(info.st_mode))
                    or (role == "lock" and (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600))
                    or (role == "locks" and not unmanaged_initialization and stat.S_IMODE(info.st_mode) != 0o700)
                    or (
                        role in {"state", "runs"}
                        and (info.st_nlink < 2 or (not unmanaged_initialization and info.st_mode & 0o067))
                    )
                ):
                    raise _invalid()

    def _read(self, name=_REGISTRY):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=self.fds["locks"])
        except FileNotFoundError:
            return None, None
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > _MAX_REGISTRY_BYTES
            ):
                raise _invalid()
            data = os.read(fd, _MAX_REGISTRY_BYTES + 1)
            after = os.fstat(fd)
            if _file_signature(before) != _file_signature(after) or len(data) != before.st_size:
                raise _invalid()
            return data, _file_signature(before)
        finally:
            os.close(fd)

    def reload(self):
        self.marker_content, self.marker_identity = self._read(_MARKER)
        self.marker = None
        if self.marker_content is not None:
            self.marker = SharedTraversalMembership.from_dict(json.loads(self.marker_content))
            if (
                self.marker.phase != "joining"
                or self.marker.epoch != 1
                or canonical_json_bytes(self.marker.to_dict()) != self.marker_content
            ):
                raise _invalid()
        self.content, self.registry_identity = self._read()
        if self.content is not None:
            self.registry = _validate_registry(json.loads(self.content))
            if canonical_json_bytes(self.registry) != self.content:
                raise _invalid()
        else:
            self.registry = None
        if self.registry is not None:
            if self.marker is None or any(
                self.registry[key]
                != (getattr(self.marker, key).to_dict() if key in {"state", "runs"} else getattr(self.marker, key))
                for key in ("namespace_id", "state", "runs", "qemu_uid", "qemu_gid")
            ):
                raise _invalid()
        elif self.marker is not None and not self._recover_marker:
            raise _invalid()
        self.verify_metadata()

    def verify(self):
        self.verify_metadata()
        if self._read() != (self.content, self.registry_identity):
            raise _invalid()
        if self._read(_MARKER) != (self.marker_content, self.marker_identity):
            raise _invalid()
        self.verify_metadata()

    def enroll(self, member):
        if "lock" not in self.fds or self.marker is not None or self.registry is not None:
            raise _invalid()
        self.verify()
        content = canonical_json_bytes(member.to_dict())
        if len(content) > _MAX_REGISTRY_BYTES:
            raise _invalid()
        fd = os.open(
            _MARKER,
            os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=self.fds["locks"],
        )
        try:
            offset = 0
            while offset < len(content):
                count = os.write(fd, content[offset:])
                if count <= 0:
                    raise _invalid()
                offset += count
            os.fsync(fd)
            self.verify_metadata()
            if os.stat(_MARKER, dir_fd=self.fds["locks"], follow_symlinks=False) != os.fstat(fd):
                raise _invalid()
            os.fsync(self.fds["locks"])
        finally:
            os.close(fd)
        self.reload()
        if self.marker != member:
            raise _invalid()

    def write(self, registry):
        if "lock" not in self.fds or self.marker is None:
            raise _invalid()
        self.verify()
        content = canonical_json_bytes(_validate_registry(registry))
        if len(content) > _MAX_REGISTRY_BYTES:
            raise _invalid()
        name = ".oci-shared-traversal-" + str(uuid.uuid4())
        fd = os.open(
            name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fds["locks"]
        )
        try:
            offset = 0
            while offset < len(content):
                count = os.write(fd, content[offset:])
                if count <= 0:
                    raise _invalid()
                offset += count
            os.fsync(fd)
            self.verify()
            if os.stat(name, dir_fd=self.fds["locks"], follow_symlinks=False) != os.fstat(fd):
                raise _invalid()
            os.rename(name, _REGISTRY, src_dir_fd=self.fds["locks"], dst_dir_fd=self.fds["locks"])
            os.fsync(self.fds["locks"])
        finally:
            os.close(fd)
        self.reload()
        if self.content != content:
            raise _invalid()

    def _close(self):
        items, self.fds = self.fds, {}
        for role, fd in reversed(tuple(items.items())):
            try:
                if _identity(os.fstat(fd)) == self.identities[role]:
                    os.close(fd)
            except OSError:
                pass

    def close(self):
        with _FORK_LOCK:
            self._close()
            _HOLDERS.discard(self)


def _fork_child():
    try:
        for item in tuple(_HOLDERS):
            item._close()
        _HOLDERS.clear()
    finally:
        _FORK_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_FORK_LOCK.acquire, after_in_parent=_FORK_LOCK.release, after_in_child=_fork_child)


def _acl_states(ns, backend):
    ns.verify_metadata()
    states = {}
    for role in ("state", "runs"):
        target = RuntimeAccessTarget.from_dict(ns.registry[role])
        acl = backend.read_acl(ns.fds[role])
        _validate_target(os.fstat(ns.fds[role]), target, acl, run_directory=True)
        states[role] = acl
    ns.verify_metadata()
    return states


def _verify_acls(ns, backend, expected):
    ns.verify()
    if _acl_states(ns, backend) != expected:
        raise _invalid()
    ns.verify()


def _allowed(ns):
    state, runs = (RuntimeAccessTarget.from_dict(ns.registry[role]) for role in ("state", "runs"))
    baseline = {"state": state.baseline, "runs": runs.baseline}
    granted = {"state": state.granted, "runs": runs.granted}
    pending = ns.registry["pending"]
    active = _active(ns.registry)
    if pending is not None and (
        (pending["phase"] == "joining" and not active) or (pending["phase"] == "leaving" and len(active) == 1)
    ):
        return [baseline, {"state": state.baseline, "runs": runs.granted}, granted]
    return [granted if active else baseline]


def _baseline(ns, backend):
    for role in ("state", "runs"):
        info = os.fstat(ns.fds[role])
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise _invalid()
        if backend is not None and backend.read_acl(ns.fds[role]) != baseline_acl(directory=True):
            raise _invalid()
    ns.verify()


@contextmanager
def shared_traversal_initialization(roots, *, acl_backend=None):
    """Hold global authority while legacy initialization skips state/runs chmod."""
    ns = None
    try:
        ns = _Namespace(roots, create=True, locked=True)
        backend = acl_backend
        if ns.registry is None:
            # Legacy ContentStore/storage-set callers may supply an owner-held
            # 0755 root. Repair only an explicitly unmanaged namespace, under
            # its global lock and through exact pinned, non-symlink FDs.
            for role in ("locks", "runs", "state"):
                ns.verify()
                fd = ns.fds[role]
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                    os.fchmod(fd, 0o700)
                    os.fsync(fd)
                ns.verify()
            ns._initializing = False
            _baseline(ns, backend)
        else:
            backend = acl_backend or LinuxFdACLBackend()
            if _acl_states(ns, backend) not in _allowed(ns):
                raise _invalid()
        yield
        if ns.registry is None:
            _baseline(ns, backend)
        else:
            ns.verify()
            if _acl_states(ns, backend) not in _allowed(ns):
                raise _invalid()
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if ns is not None:
            ns.close()


def _write_member(mutation, member):
    data = mutation.mutable_state()
    data[SHARED_TRAVERSAL_STATE_KEY] = member.to_dict()
    mutation.write_state(data["status"], data)


def _require_member(mutation, expected):
    current = mutation.snapshot.state.get(SHARED_TRAVERSAL_STATE_KEY)
    if expected is None:
        if SHARED_TRAVERSAL_STATE_KEY in mutation.snapshot.state:
            raise _invalid()
    elif SharedTraversalMembership.from_dict(current) != expected:
        raise _invalid()


@contextmanager
def _verify_private_access(mutation, access, backend, *, granted):
    if _source_io(mutation, access.binding) != access.runtime_io:
        raise _invalid()
    expected = {
        role: getattr(access, role).granted if granted else getattr(access, role).baseline
        for role in ("run", "directory", "console")
    }
    with _pinned_io(mutation, access.runtime_io) as descriptors:
        _verify_pinned(mutation, descriptors, access, backend, expected)
        yield
        # External libvirt/ACL commands above this tail may have rebound a
        # private endpoint. Recheck the SAME held descriptors and visible names.
        _verify_pinned_metadata(mutation, descriptors, access, expected)


def _scan_unmanaged(ns, current):
    """Do not introduce a new shared namespace around ambiguous active ledgers."""
    for name in os.listdir(ns.fds["runs"]):
        if name == current:
            continue
        with locked_existing_run(ns.roots, name) as other:
            if other.snapshot.state.get("status") not in {"exited", "removed"}:
                raise _invalid()
    ns.verify()


def join_oci_shared_traversal(roots, binding, *, conn, acl_backend=None):
    """Join after exact per-run access grant, before monitor preparation."""
    ns = None
    try:
        backend = acl_backend or LinuxFdACLBackend()
        ns = _Namespace(roots, locked=True, recover_marker=True)
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            access = RuntimeAccessReceipt.from_dict(mutation.snapshot.state.get("oci_runtime_access"))
            if access.phase != "granted" or access.binding != binding:
                raise _invalid()
            principal = access.qemu_uid, access.qemu_gid
            _grant_authority(mutation, binding, conn, principal)
            if ns.registry is None:
                _baseline(ns, backend)
                _scan_unmanaged(ns, binding.record.name)
                targets = {}
                for role in ("state", "runs"):
                    info = os.fstat(ns.fds[role])
                    base = baseline_acl(directory=True)
                    targets[role] = RuntimeAccessTarget(
                        info.st_dev,
                        info.st_ino,
                        info.st_uid,
                        info.st_gid,
                        info.st_nlink,
                        True,
                        base,
                        traversal_acl(base, principal[0]),
                    ).to_dict()
                if ns.marker is None:
                    marker = SharedTraversalMembership(
                        str(uuid.uuid4()),
                        1,
                        access.access_id,
                        binding,
                        RuntimeAccessTarget.from_dict(targets["state"]),
                        RuntimeAccessTarget.from_dict(targets["runs"]),
                        *principal,
                        "joining",
                    )
                    ns.enroll(marker)
                else:
                    marker = ns.marker
                    if (
                        marker.binding != binding
                        or marker.access_id != access.access_id
                        or (marker.qemu_uid, marker.qemu_gid) != principal
                    ):
                        raise _invalid()
                    for role in ("state", "runs"):
                        target = getattr(marker, role)
                        _validate_target(os.fstat(ns.fds[role]), target, target.baseline, run_directory=True)
                        targets[role] = target.to_dict()
                ns.write(
                    {
                        "schema": _SCHEMA,
                        "namespace_id": marker.namespace_id,
                        "epoch": 0,
                        "qemu_uid": principal[0],
                        "qemu_gid": principal[1],
                        **targets,
                        "members": {},
                        "pending": None,
                    }
                )
            if (ns.registry["qemu_uid"], ns.registry["qemu_gid"]) != principal:
                raise _invalid()
            saved = mutation.snapshot.state.get(SHARED_TRAVERSAL_STATE_KEY)
            if SHARED_TRAVERSAL_STATE_KEY in mutation.snapshot.state:
                member = SharedTraversalMembership.from_dict(saved)
            elif ns.registry["pending"] is not None:
                member = SharedTraversalMembership.from_dict(ns.registry["pending"])
            else:
                member = SharedTraversalMembership(
                    ns.registry["namespace_id"],
                    ns.registry["epoch"] + (not _active(ns.registry)),
                    access.access_id,
                    binding,
                    RuntimeAccessTarget.from_dict(ns.registry["state"]),
                    RuntimeAccessTarget.from_dict(ns.registry["runs"]),
                    *principal,
                    "joining",
                )
            if (
                member.binding != binding
                or member.access_id != access.access_id
                or member.namespace_id != ns.registry["namespace_id"]
                or member.phase not in {"joining", "active"}
            ):
                raise _invalid()
            expected_member = SharedTraversalMembership.from_dict(saved) if saved is not None else None
            expected_acl = _acl_states(ns, backend)
            if expected_acl not in _allowed(ns):
                raise _invalid()

            def verify():
                _require_member(mutation, expected_member)
                if RuntimeAccessReceipt.from_dict(mutation.snapshot.state.get("oci_runtime_access")) != access:
                    raise _invalid()
                with _verify_private_access(mutation, access, backend, granted=True):
                    _grant_authority(mutation, binding, conn, principal)
                    _verify_acls(ns, backend, expected_acl)
                    _grant_authority(mutation, binding, conn, principal)
                    _require_member(mutation, expected_member)
                ns.verify_metadata()

            verify()
            recorded = ns.registry["members"].get(_key(member))
            if member.phase == "active":
                if recorded != member.to_dict() or ns.registry["pending"] is not None:
                    raise _invalid()
                return member
            if recorded is not None:
                completed = replace(member, phase="active")
                if recorded != completed.to_dict() or ns.registry["pending"] is not None:
                    raise _invalid()
                _write_member(mutation, completed)
                expected_member = completed
                verify()
                return completed
            if ns.registry["pending"] is None:
                new = copy.deepcopy(ns.registry)
                new["epoch"] = member.epoch
                new["pending"] = member.to_dict()
                prospective = copy.deepcopy(new)
                prospective["pending"] = None
                prospective["members"][_key(member)] = replace(member, phase="active").to_dict()
                if len(canonical_json_bytes(_validate_registry(prospective))) > _MAX_REGISTRY_BYTES:
                    raise _invalid()
                # Every admitted member must still be able to publish a leave
                # intent, which temporarily duplicates that member in pending.
                largest = max(_active(prospective), key=lambda item: len(canonical_json_bytes(item)))
                prospective["pending"] = {**largest, "phase": "leaving"}
                if len(canonical_json_bytes(_validate_registry(prospective))) > _MAX_REGISTRY_BYTES:
                    raise _invalid()
                verify()
                ns.write(new)
            elif ns.registry["pending"] != member.to_dict():
                raise _invalid()
            if expected_member is None:
                verify()
                _write_member(mutation, member)
                expected_member = member
            if not _active(ns.registry):
                for role in ("runs", "state"):
                    target = getattr(member, role)
                    verify()
                    if expected_acl[role] != target.granted:
                        backend.write_acl(ns.fds[role], target.granted)
                        expected_acl[role] = target.granted
                    os.fsync(ns.fds[role])
                    verify()
            completed = replace(member, phase="active")
            new = copy.deepcopy(ns.registry)
            new["members"][_key(member)] = completed.to_dict()
            new["pending"] = None
            verify()
            ns.write(new)
            verify()
            _write_member(mutation, completed)
            expected_member = completed
            verify()
            return completed
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if ns is not None:
            ns.close()


def leave_oci_shared_traversal(roots, binding, *, conn, acl_backend=None, liveness_probe=probe_process_liveness):
    """Leave after 30Q restoration; only the last member closes ancestors."""
    ns = authority = None
    try:
        backend = acl_backend or LinuxFdACLBackend()
        ns = _Namespace(roots, locked=True)
        if ns.registry is None:
            raise _invalid()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            member = SharedTraversalMembership.from_dict(mutation.snapshot.state.get(SHARED_TRAVERSAL_STATE_KEY))
            access = RuntimeAccessReceipt.from_dict(mutation.snapshot.state.get("oci_runtime_access"))
            if (
                member.binding != binding
                or member.phase not in {"active", "leaving", "left"}
                or access.binding != binding
                or access.phase != "revoked"
                or member.access_id != access.access_id
                or (member.qemu_uid, member.qemu_gid) != (access.qemu_uid, access.qemu_gid)
                or (member.qemu_uid, member.qemu_gid) != (ns.registry["qemu_uid"], ns.registry["qemu_gid"])
            ):
                raise _invalid()
            authority = _RecoveryAuthority(mutation, binding)
            journal = authority.snapshot
            cleanup = MonitorInactiveCleanupReceipt.from_dict(
                mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
            )
            expected_cleanup = MonitorInactiveCleanupReceipt(
                binding,
                cleanup.cleanup_id,
                "sha256:" + hashlib.sha256(authority.content).hexdigest(),
                journal.identity.generation,
                journal.revision,
                authority.journal_metadata.st_dev,
                authority.journal_metadata.st_ino,
                journal.writer,
                "completed",
            )
            if (
                cleanup != expected_cleanup
                or journal.phase != "terminal"
                or access.cleanup_digest != _digest(cleanup.to_dict())
            ):
                raise _invalid()
            expected_member = member
            expected_acl = _acl_states(ns, backend)
            if expected_acl not in _allowed(ns):
                raise _invalid()

            def verify():
                _require_member(mutation, expected_member)
                if (
                    RuntimeAccessReceipt.from_dict(mutation.snapshot.state.get("oci_runtime_access")) != access
                    or MonitorInactiveCleanupReceipt.from_dict(
                        mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                    )
                    != cleanup
                ):
                    raise _invalid()
                authority.validate()
                _validate_ledger(mutation, binding, journal)
                _stale(journal, liveness_probe)
                with _verify_private_access(mutation, access, backend, granted=False):
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    _verify_acls(ns, backend, expected_acl)
                    authority.validate()
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    _require_member(mutation, expected_member)
                ns.verify_metadata()

            verify()
            recorded = ns.registry["members"].get(_key(member))
            if member.phase == "left":
                if recorded is not None and recorded != member.to_dict():
                    raise _invalid()
                if (
                    member.namespace_id != ns.registry["namespace_id"]
                    or member.state.to_dict() != ns.registry["state"]
                    or member.runs.to_dict() != ns.registry["runs"]
                    or member.epoch > ns.registry["epoch"]
                ):
                    raise _invalid()
                return member
            if recorded == replace(member, phase="left").to_dict() and ns.registry["pending"] is None:
                completed = replace(member, phase="left")
                _write_member(mutation, completed)
                expected_member = completed
                verify()
                compact = copy.deepcopy(ns.registry)
                del compact["members"][_key(member)]
                ns.write(compact)
                verify()
                return completed
            if recorded != replace(member, phase="active").to_dict():
                raise _invalid()
            pending = replace(member, phase="leaving")
            if ns.registry["pending"] is None:
                new = copy.deepcopy(ns.registry)
                new["pending"] = pending.to_dict()
                verify()
                ns.write(new)
            elif ns.registry["pending"] != pending.to_dict():
                raise _invalid()
            if member.phase == "active":
                verify()
                _write_member(mutation, pending)
                expected_member = member = pending
            if len(_active(ns.registry)) == 1:
                for role in ("state", "runs"):
                    target = getattr(member, role)
                    verify()
                    if expected_acl[role] != target.baseline:
                        backend.write_acl(ns.fds[role], target.baseline)
                        expected_acl[role] = target.baseline
                    os.fsync(ns.fds[role])
                    verify()
            completed = replace(member, phase="left")
            new = copy.deepcopy(ns.registry)
            new["members"][_key(member)] = completed.to_dict()
            new["pending"] = None
            verify()
            ns.write(new)
            verify()
            _write_member(mutation, completed)
            expected_member = completed
            verify()
            compact = copy.deepcopy(ns.registry)
            del compact["members"][_key(member)]
            ns.write(compact)
            verify()
            return completed
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if authority is not None:
            authority.close()
        if ns is not None:
            ns.close()


def verify_shared_traversal(roots, member, *, binding=None, access=None, state_fd=None, runs_fd=None, acl_backend=None):
    """Pure current-membership check without a namespace or run flock."""
    ns = None
    try:
        ns = _Namespace(roots)
        if ns.registry is None:
            if member is not None:
                raise _invalid()
            # Legacy qualification remains possible only without a managed namespace.
            return None
        member = (
            SharedTraversalMembership.from_dict(member) if type(member) is not SharedTraversalMembership else member
        )
        if (
            member.phase != "active"
            or ns.registry["members"].get(_key(member)) != member.to_dict()
            or (binding is not None and member.binding != binding)
        ):
            raise _invalid()
        if access is not None:
            access = RuntimeAccessReceipt.from_dict(access) if type(access) is not RuntimeAccessReceipt else access
            if access.phase != "granted" or access.access_id != member.access_id or access.binding != member.binding:
                raise _invalid()
        for role, supplied in (("state", state_fd), ("runs", runs_fd)):
            if supplied is not None and _identity(os.fstat(supplied)) != ns.identities[role]:
                raise _invalid()
        backend = acl_backend or LinuxFdACLBackend()
        expected = {role: getattr(member, role).granted for role in ("state", "runs")}
        # Atomic registry replacement by another member is legal. The coherent
        # snapshot's own member must remain active, and target identity/ACL exact.
        if _acl_states(ns, backend) != expected:
            raise _invalid()
        ns.verify_metadata()
        return member
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if ns is not None:
            ns.close()
