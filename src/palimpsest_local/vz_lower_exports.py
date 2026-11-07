"""Native VZ read-only transport views of canonically leased OCI lowers.

Only publish creates anything. Load/verify never repair a missing path, including
paths Lima would otherwise recreate with MkdirAll. The exported directory contains
only digest-named payload copies; completion records and ordered run references
are private siblings, not guest-visible authority. Stop retains the reference.

All operations serialize cooperative projection writers. The caller must also hold
its canonical run/lifecycle lock across prestart verification and provider calls,
and across exact provider-absence proof and release. Durable references, rather
than an unconsumed file descriptor, retain path-based backing-file custody. These
checks do not attest against a malicious same-UID process or host writer.

A failed publication/removal leaves its exact reference or incomplete directory
in place and keeps canonical leases. Unknown/incomplete state blocks reuse; it is
never opportunistically rebuilt, adopted, garbage-collected, or lease-released.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from .errors import StateError
from .oci_store import (
    ArtifactLeaseOwner,
    DerivedLayerReceipt,
    DurableLeaseSet,
    DurableLeaseSetMember,
    OCIStore,
    OCIStoreError,
    _open_absolute_directory,
)
from .state import StatePaths

_CONTRACT = "palimpsest.vz-lower-export.v1"
_REFERENCE_SCHEMA = "palimpsest.vz-lower-reference.v1"
_DIRECTORY = "vz-lower-exports-v1"
_MOUNT_POINT = "/.palimpsest/lower-input"
_MAX_LAYERS = 128
_MAX_RECORD = 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _invalid(detail: str) -> StateError:
    return StateError(f"native VZ lower export {detail}")


def _boundary(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, OCIStoreError):
            raise _invalid("authority is missing, unsafe, incomplete, or changed") from None

    return guarded


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _identity(value: os.stat_result, *, mode: int | None = None) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode) if mode is None else mode,
    )


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    return (*_identity(value), value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _require_file(value: os.stat_result, mode: int = 0o400) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != mode
        or value.st_nlink != 1
    ):
        raise _invalid("file ownership, type, mode, or link count is unsafe")


def _require_directory(value: os.stat_result, mode: int) -> None:
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != mode:
        raise _invalid("directory ownership, type, or mode is unsafe")


def _entry(parent_fd: int, name: str, fd: int, *, mode: int, directory: bool = False) -> os.stat_result:
    opened = os.fstat(fd)
    visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if directory:
        _require_directory(opened, mode)
        _require_directory(visible, mode)
        if _identity(opened) != _identity(visible):
            raise _invalid("directory identity changed")
    else:
        _require_file(opened, mode)
        _require_file(visible, mode)
        if _stamp(opened) != _stamp(visible):
            raise _invalid("file identity changed")
    return opened


def _open_directory(stack: ExitStack, parent_fd: int, name: str, mode: int) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    stack.callback(os.close, fd)
    _entry(parent_fd, name, fd, mode=mode, directory=True)
    return fd


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise _invalid("write did not advance")
        remaining = remaining[written:]


def _new_record(parent_fd: int, name: str, value: Callable[[tuple[int, ...]], dict[str, Any]]) -> None:
    """Exclusive creation; self-inode binding precedes the first written byte.

    No rename-over-existing fallback: a failed/partial record remains a blocker.
    The parent directory is synced only after complete bytes and final mode.
    """
    fd = os.open(name, _NEW_FLAGS, 0o600, dir_fd=parent_fd)
    try:
        _entry(parent_fd, name, fd, mode=0o600)
        payload = _canonical(value(_identity(os.fstat(fd), mode=0o400)))
        if len(payload) > _MAX_RECORD:
            raise _invalid("record exceeds its size bound")
        _write_all(fd, payload)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        _entry(parent_fd, name, fd, mode=0o400)
        os.fsync(parent_fd)
    finally:
        os.close(fd)


def _read_record(parent_fd: int, name: str) -> tuple[dict[str, Any], bytes, tuple[int, ...]]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _invalid("record contains duplicate fields")
            result[key] = value
        return result

    fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    try:
        before = _entry(parent_fd, name, fd, mode=0o400)
        if not 0 < before.st_size <= _MAX_RECORD:
            raise _invalid("record size is invalid")
        payload = bytearray()
        while True:
            block = os.read(fd, min(65536, _MAX_RECORD + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
            if len(payload) > _MAX_RECORD:
                raise _invalid("record exceeds its size bound")
        after = _entry(parent_fd, name, fd, mode=0o400)
        if _stamp(before) != _stamp(after) or len(payload) != before.st_size:
            raise _invalid("record changed while being read")
        try:
            value = json.loads(payload, object_pairs_hook=pairs)
            if not isinstance(value, dict) or _canonical(value) != payload:
                raise _invalid("record is not canonical")
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise _invalid("record encoding is invalid") from None
        return value, bytes(payload), _stamp(after)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class VZLowerExportFile:
    """One logical occurrence; repeated digests share one physical file."""

    filename: str
    digest: str
    size: int
    ordinal: int
    device: int
    inode: int
    uid: int
    gid: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class VZLowerExport:
    root: Path
    tag: str
    binding_digest: str
    files: tuple[VZLowerExportFile, ...]
    mount_point: str = _MOUNT_POINT
    _state_root: Path = Path()
    _reference_digest: str = ""
    _reference_stamp: tuple[int, ...] = ()
    _completion_stamp: tuple[int, ...] = ()
    _chain_identity: tuple[int, ...] = ()
    _payload_identity: tuple[int, ...] = ()

    @property
    def allocated_bytes(self) -> int:
        """Actual copied payload bytes, counted once per unique physical digest."""
        return sum({entry.digest: entry.size for entry in self.files}.values())


def lima_vz_mount_tag(resolved_location: Path, resolved_mount_point: str) -> str:
    """Lima v2.2.1's tag for already-resolved absolute location/mountPoint.

    Resolution is a caller responsibility: this pure helper neither follows a
    symlink nor probes/creates a host or guest path.
    """
    if (
        not isinstance(resolved_location, Path)
        or not resolved_location.is_absolute()
        or ".." in resolved_location.parts
        or not isinstance(resolved_mount_point, str)
        or not resolved_mount_point.startswith("/")
        or str(Path(resolved_mount_point)) != resolved_mount_point
        or ".." in Path(resolved_mount_point).parts
        or "\0" in str(resolved_location) + resolved_mount_point
    ):
        raise _invalid("Lima mount paths are not resolved absolute paths")
    return "lima-" + hashlib.sha256(f"{resolved_location}:{resolved_mount_point}".encode()).hexdigest()[:16]


@dataclass(slots=True)
class _Authority:
    state_root: Path
    state_fd: int
    base_fd: int
    chains_fd: int
    refs_fd: int
    lock_fd: int

    @property
    def root(self) -> Path:
        return self.state_root / _DIRECTORY

    def anchors(self) -> dict[str, list[int]]:
        return {
            "state": list(_identity(os.fstat(self.state_fd))),
            "base": list(_identity(os.fstat(self.base_fd))),
            "chains": list(_identity(os.fstat(self.chains_fd))),
            "refs": list(_identity(os.fstat(self.refs_fd))),
            "lock": list(_identity(os.fstat(self.lock_fd))),
        }

    def verify(self) -> None:
        with _open_absolute_directory(self.state_root) as visible:
            _require_directory(os.fstat(visible), 0o700)
            if _identity(os.fstat(visible)) != _identity(os.fstat(self.state_fd)):
                raise _invalid("state directory identity changed")
        _entry(self.state_fd, _DIRECTORY, self.base_fd, mode=0o700, directory=True)
        _entry(self.base_fd, "chains", self.chains_fd, mode=0o700, directory=True)
        _entry(self.base_fd, "refs", self.refs_fd, mode=0o700, directory=True)
        _entry(self.base_fd, "custody.lock", self.lock_fd, mode=0o600)
        if set(os.listdir(self.base_fd)) != {"chains", "refs", "custody.lock"}:
            raise _invalid("private custody directory has unknown entries")
        if any(_HEX.fullmatch(name) is None for name in os.listdir(self.chains_fd)):
            raise _invalid("incomplete publication or cleanup holds custody")


@contextmanager
def _authority(roots: StatePaths, *, create: bool = False) -> Iterator[_Authority]:
    if not isinstance(roots, StatePaths) or not isinstance(roots.state, Path) or ".." in roots.state.parts:
        raise _invalid("state authority is invalid")
    state_root = Path(os.path.abspath(roots.state))
    with _open_absolute_directory(state_root) as state_fd, ExitStack() as stack:
        _require_directory(os.fstat(state_fd), 0o700)
        if create:
            try:
                os.mkdir(_DIRECTORY, 0o700, dir_fd=state_fd)
            except FileExistsError:
                pass
            else:
                # Only the creator initializes. Existing partial roots are never
                # repaired, even on a fresh publish invocation.
                base = _open_directory(stack, state_fd, _DIRECTORY, 0o700)
                os.mkdir("chains", 0o700, dir_fd=base)
                os.mkdir("refs", 0o700, dir_fd=base)
                lock = os.open("custody.lock", _NEW_FLAGS, 0o600, dir_fd=base)
                try:
                    os.fsync(lock)
                finally:
                    os.close(lock)
                os.fsync(base)
                os.fsync(state_fd)
        base_fd = _open_directory(stack, state_fd, _DIRECTORY, 0o700)
        lock_fd = os.open("custody.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=base_fd)
        stack.callback(os.close, lock_fd)
        _entry(base_fd, "custody.lock", lock_fd, mode=0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        chains_fd = _open_directory(stack, base_fd, "chains", 0o700)
        refs_fd = _open_directory(stack, base_fd, "refs", 0o700)
        authority = _Authority(state_root, state_fd, base_fd, chains_fd, refs_fd, lock_fd)
        authority.verify()
        yield authority
        authority.verify()


def _lease_value(lease_set: DurableLeaseSet) -> dict[str, Any]:
    return {
        "lease_set_id": lease_set.lease_set_id,
        "plan_digest": lease_set.plan_digest,
        "members": [
            {
                "ordinal": member.ordinal,
                "lease_id": member.lease_id,
                "acquired_ns": member.acquired_ns,
                "receipt": member.receipt.to_dict(),
            }
            for member in lease_set.members
        ],
    }


def _lease_from_value(value: Any, owner: ArtifactLeaseOwner) -> DurableLeaseSet:
    if (
        not isinstance(value, dict)
        or set(value) != {"lease_set_id", "plan_digest", "members"}
        or not isinstance(value["members"], list)
        or not 0 < len(value["members"]) <= _MAX_LAYERS
    ):
        raise _invalid("ordered reference lease set is invalid")
    members = []
    for member in value["members"]:
        if not isinstance(member, dict) or set(member) != {"ordinal", "lease_id", "acquired_ns", "receipt"}:
            raise _invalid("ordered reference lease member is invalid")
        members.append(
            DurableLeaseSetMember(
                member["ordinal"],
                member["lease_id"],
                DerivedLayerReceipt.from_dict(member["receipt"]),
                member["acquired_ns"],
            )
        )
    result = DurableLeaseSet(value["lease_set_id"], value["plan_digest"], owner, tuple(members))
    if _lease_value(result) != value:
        raise _invalid("ordered reference lease set is not canonical")
    return result


def _inputs(
    store: OCIStore, receipts: tuple[DerivedLayerReceipt, ...], owner: ArtifactLeaseOwner, lease_set: DurableLeaseSet
) -> None:
    if (
        not isinstance(store, OCIStore)
        or type(owner) is not ArtifactLeaseOwner
        or type(lease_set) is not DurableLeaseSet
        or type(receipts) is not tuple
        or not 0 < len(receipts) <= _MAX_LAYERS
        or any(type(receipt) is not DerivedLayerReceipt for receipt in receipts)
        or lease_set.owner != owner
        or tuple(member.receipt for member in lease_set.members) != receipts
        or tuple(receipt.ordinal for receipt in receipts) != tuple(range(len(receipts)))
        or any(receipt.store_id != store.identity for receipt in receipts)
    ):
        raise _invalid("canonical ordered lease authority does not match")
    sizes = {}
    for receipt in receipts:
        receipt.__post_init__()
        if receipt.image_digest in sizes and sizes[receipt.image_digest] != receipt.image_size:
            raise _invalid("one physical digest has conflicting sizes")
        sizes[receipt.image_digest] = receipt.image_size
    live = store.load_lease_set(lease_set.lease_set_id, owner, plan_digest=lease_set.plan_digest)
    if live != lease_set:
        raise _invalid("canonical ordered lease set changed")


def _chain(receipts: tuple[DerivedLayerReceipt, ...]) -> list[dict[str, Any]]:
    return [{"ordinal": r.ordinal, "digest": r.image_digest, "size": r.image_size} for r in receipts]


def _physical_key(store_id: str, receipts: tuple[DerivedLayerReceipt, ...]) -> str:
    return _digest(_canonical({"contract": _CONTRACT, "store_id": store_id, "chain": _chain(receipts)}))[7:]


def _key(store: OCIStore, receipts: tuple[DerivedLayerReceipt, ...]) -> str:
    return _physical_key(store.identity, receipts)


def _owner_key(owner: ArtifactLeaseOwner) -> str:
    return hashlib.sha256(_canonical(owner.to_dict())).hexdigest()


def _reference_value(
    authority: _Authority,
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
    key: str,
    completion_digest: str,
    completion_stamp: tuple[int, ...],
    identity: tuple[int, ...],
) -> dict[str, Any]:
    return {
        "schema": _REFERENCE_SCHEMA,
        "phase": "active",
        "owner": owner.to_dict(),
        "store_id": lease_set.members[0].receipt.store_id,
        "chain_key": key,
        "completion_digest": completion_digest,
        "completion_stamp": list(completion_stamp),
        "anchors": authority.anchors(),
        "lease_set": _lease_value(lease_set),
        "reference_identity": list(identity),
    }


def _references(authority: _Authority) -> dict[str, tuple[dict[str, Any], bytes, tuple[int, ...], DurableLeaseSet]]:
    result = {}
    for name in os.listdir(authority.refs_fd):
        if not name.endswith(".json") or _HEX.fullmatch(name[:-5]) is None:
            raise _invalid("unknown reference or interrupted publication holds custody")
        value, raw, stamp = _read_record(authority.refs_fd, name)
        if (
            set(value)
            != {
                "schema",
                "phase",
                "owner",
                "store_id",
                "chain_key",
                "completion_digest",
                "completion_stamp",
                "anchors",
                "lease_set",
                "reference_identity",
            }
            or value["schema"] != _REFERENCE_SCHEMA
            or value["phase"] != "active"
            or not isinstance(value["owner"], dict)
            or set(value["owner"]) != {"run_id", "run_name", "role"}
            or any(type(field) is not str for field in value["owner"].values())
            or not isinstance(value["chain_key"], str)
            or _HEX.fullmatch(value["chain_key"]) is None
            or not isinstance(value["completion_digest"], str)
            or _DIGEST.fullmatch(value["completion_digest"]) is None
            or _canonical(value["reference_identity"]) != _canonical(list(stamp[:5]))
            or _canonical(value["anchors"]) != _canonical(authority.anchors())
        ):
            raise _invalid("run reference is incomplete, replaced, or ambiguous")
        owner = ArtifactLeaseOwner(**value["owner"])
        lease_set = _lease_from_value(value["lease_set"], owner)
        if (
            owner.to_dict() != value["owner"]
            or name != _owner_key(owner) + ".json"
            or value["store_id"] != lease_set.members[0].receipt.store_id
            or any(member.receipt.store_id != value["store_id"] for member in lease_set.members)
            or value["chain_key"]
            != _physical_key(
                value["store_id"],
                tuple(member.receipt for member in lease_set.members),
            )
            or not isinstance(value["completion_stamp"], list)
            or len(value["completion_stamp"]) != 9
            or any(type(v) is not int for v in value["completion_stamp"])
        ):
            raise _invalid("run reference owner or completion binding is invalid")
        result[name] = value, raw, stamp, lease_set
    return result


def _payload(parent_fd: int, filename: str, digest: str, size: int) -> dict[str, Any]:
    fd = os.open(filename, _READ_FLAGS, dir_fd=parent_fd)
    try:
        before = _entry(parent_fd, filename, fd, mode=0o400)
        if before.st_size != size:
            raise _invalid("payload size changed")
        hasher = hashlib.sha256()
        count = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            count += len(block)
            if count > size:
                raise _invalid("payload exceeds its bound")
            hasher.update(block)
        after = _entry(parent_fd, filename, fd, mode=0o400)
        if count != size or "sha256:" + hasher.hexdigest() != digest or _stamp(before) != _stamp(after):
            raise _invalid("payload bytes or retained identity changed")
        return {"filename": filename, "digest": digest, "size": size, "stamp": list(_stamp(after))}
    finally:
        os.close(fd)


def _completion_value(
    authority: _Authority,
    key: str,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    chain_identity: tuple[int, ...],
    payload_identity: tuple[int, ...],
    files: list[dict[str, Any]],
    identity: tuple[int, ...],
) -> dict[str, Any]:
    location = authority.root / "chains" / key / "payload"
    return {
        "contract": _CONTRACT,
        "store_id": store.identity,
        "chain_key": key,
        "chain": _chain(receipts),
        "location": str(location),
        "mount_point": _MOUNT_POINT,
        "tag": lima_vz_mount_tag(location, _MOUNT_POINT),
        "anchors": authority.anchors(),
        "chain_identity": list(chain_identity),
        "payload_identity": list(payload_identity),
        "files": files,
        "completion_identity": list(identity),
    }


def _projection(
    authority: _Authority, key: str, store: OCIStore, receipts: tuple[DerivedLayerReceipt, ...]
) -> tuple[dict[str, Any], str, tuple[int, ...]]:
    with ExitStack() as stack:
        chain_fd = _open_directory(stack, authority.chains_fd, key, 0o500)
        payload_fd = _open_directory(stack, chain_fd, "payload", 0o500)
        if set(os.listdir(chain_fd)) != {"payload", "complete.json"}:
            raise _invalid("completed chain has unknown or missing entries")
        unique = {r.image_digest: r.image_size for r in receipts}
        names = {digest[7:] + ".sqfs" for digest in unique}
        if set(os.listdir(payload_fd)) != names:
            raise _invalid("payload allowlist changed")
        complete, raw, completion_stamp = _read_record(chain_fd, "complete.json")
        files = [_payload(payload_fd, digest[7:] + ".sqfs", digest, unique[digest]) for digest in sorted(unique)]
        expected = _completion_value(
            authority,
            key,
            store,
            receipts,
            _identity(os.fstat(chain_fd)),
            _identity(os.fstat(payload_fd)),
            files,
            completion_stamp[:5],
        )
        if _canonical(complete) != _canonical(expected):
            raise _invalid("completed publication binding or retained identities changed")
        if len({tuple(item["stamp"][:2]) for item in files}) != len(files):
            raise _invalid("distinct physical payloads alias one inode")
        _entry(authority.chains_fd, key, chain_fd, mode=0o500, directory=True)
        _entry(chain_fd, "payload", payload_fd, mode=0o500, directory=True)
        if set(os.listdir(payload_fd)) != names or set(os.listdir(chain_fd)) != {"payload", "complete.json"}:
            raise _invalid("projection changed during verification")
        authority.verify()
        return complete, _digest(raw), completion_stamp


def _chain_references(
    authority: _Authority,
    references,
    key: str,
    store: OCIStore,
    completion_digest: str,
    completion_stamp: tuple[int, ...],
) -> list[str]:
    consumers = []
    for name, (value, _raw, stamp, lease_set) in references.items():
        if value["chain_key"] != key:
            continue
        receipts = tuple(member.receipt for member in lease_set.members)
        _inputs(store, receipts, lease_set.owner, lease_set)
        expected = _reference_value(
            authority,
            lease_set.owner,
            lease_set,
            key,
            completion_digest,
            completion_stamp,
            stamp[:5],
        )
        if _key(store, receipts) != key or _canonical(value) != _canonical(expected):
            raise _invalid("shared chain has an unaccounted or changed consumer")
        consumers.append(name)
    return consumers


def _export(authority: _Authority, complete, binding_digest, completion_stamp, reference) -> VZLowerExport:
    _value, raw, reference_stamp, lease_set = reference
    unique = {item["digest"]: item for item in complete["files"]}
    files = []
    for member in lease_set.members:
        receipt = member.receipt
        entry = unique[receipt.image_digest]
        stamp = entry["stamp"]
        files.append(
            VZLowerExportFile(
                entry["filename"],
                receipt.image_digest,
                receipt.image_size,
                member.ordinal,
                stamp[0],
                stamp[1],
                stamp[2],
                stamp[3],
                stamp[7],
                stamp[8],
            )
        )
    return VZLowerExport(
        root=Path(complete["location"]),
        tag=complete["tag"],
        binding_digest=binding_digest,
        files=tuple(files),
        _state_root=authority.state_root,
        _reference_digest=_digest(raw),
        _chain_identity=tuple(complete["chain_identity"]),
        _payload_identity=tuple(complete["payload_identity"]),
        _reference_stamp=reference_stamp,
        _completion_stamp=completion_stamp,
    )


def _load(
    authority: _Authority,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
    *,
    references=None,
) -> VZLowerExport:
    _inputs(store, receipts, owner, lease_set)
    key = _key(store, receipts)
    references = _references(authority) if references is None else references
    name = _owner_key(owner) + ".json"
    if name not in references or references[name][3] != lease_set:
        raise _invalid("exact retained run reference is missing or changed")
    complete, binding, stamp = _projection(authority, key, store, receipts)
    if name not in _chain_references(authority, references, key, store, binding, stamp):
        raise _invalid("run reference does not retain this projection")
    return _export(authority, complete, binding, stamp, references[name])


def _copy_payload(payload_fd: int, member: DurableLeaseSetMember, owner: ArtifactLeaseOwner, store: OCIStore) -> None:
    receipt = member.receipt
    filename = receipt.image_digest[7:] + ".sqfs"
    fd = os.open(filename, _NEW_FLAGS, 0o600, dir_fd=payload_fd)
    reader = None
    try:
        _entry(payload_fd, filename, fd, mode=0o600)
        reader = store.resume_lease(member.lease_id, owner, receipt)
        hasher = hashlib.sha256()
        count = 0
        for block in reader.chunks():
            count += len(block)
            if count > receipt.image_size:
                raise _invalid("canonical payload stream exceeds its bound")
            hasher.update(block)
            _write_all(fd, block)
        # Exhausting chunks also invokes the canonical reader's verified EOF.
        if count != receipt.image_size or "sha256:" + hasher.hexdigest() != receipt.image_digest:
            raise _invalid("canonical payload stream did not verify")
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        _entry(payload_fd, filename, fd, mode=0o400)
    finally:
        try:
            if reader is not None:
                # close()/context-manager exit would release the run's lease.
                reader.detach()
        finally:
            os.close(fd)


def _publish_projection(
    authority: _Authority,
    key: str,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
) -> None:
    pending = ".pending-" + uuid.uuid4().hex
    os.mkdir(pending, 0o700, dir_fd=authority.chains_fd)
    os.fsync(authority.chains_fd)
    with ExitStack() as stack:
        chain_fd = _open_directory(stack, authority.chains_fd, pending, 0o700)
        os.mkdir("payload", 0o700, dir_fd=chain_fd)
        payload_fd = _open_directory(stack, chain_fd, "payload", 0o700)
        copied = set()
        for member in lease_set.members:
            if member.receipt.image_digest not in copied:
                _copy_payload(payload_fd, member, owner, store)
                copied.add(member.receipt.image_digest)
        os.fchmod(payload_fd, 0o500)
        os.fsync(payload_fd)
        files = [
            _payload(
                payload_fd,
                digest[7:] + ".sqfs",
                digest,
                next(r.image_size for r in receipts if r.image_digest == digest),
            )
            for digest in sorted(copied)
        ]
        _inputs(store, receipts, owner, lease_set)
        _new_record(
            chain_fd,
            "complete.json",
            lambda identity: _completion_value(
                authority,
                key,
                store,
                receipts,
                _identity(os.fstat(chain_fd), mode=0o500),
                _identity(os.fstat(payload_fd)),
                files,
                identity,
            ),
        )
        os.fchmod(chain_fd, 0o500)
        os.fsync(chain_fd)
        # The global custody lock excludes cooperative publishers. Never replace
        # an existing completed (or partial) chain, including an empty directory.
        try:
            os.stat(key, dir_fd=authority.chains_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _invalid("chain appeared during publication")
        _entry(authority.chains_fd, pending, chain_fd, mode=0o500, directory=True)
        os.rename(pending, key, src_dir_fd=authority.chains_fd, dst_dir_fd=authority.chains_fd)
        os.fsync(authority.chains_fd)


@_boundary
def publish_vz_lower_export(
    roots: StatePaths,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
) -> VZLowerExport:
    """Publish/reuse one completed same-chain view and an independent run ref.

    The key binds the store and complete ordered physical digest/size chain, not
    worker IDs, recipe authority, QEMU identities, or per-run execution settings.
    Duplicate digests are copied once without hard links. A completed projection
    left before reference publication may be reused by this explicit publish
    operation, but load/resume never adopts it. Partial publication blocks reuse.
    """
    _inputs(store, receipts, owner, lease_set)
    with _authority(roots, create=True) as authority:
        references = _references(authority)
        name = _owner_key(owner) + ".json"
        if name in references:
            return _load(authority, store, receipts, owner, lease_set, references=references)
        key = _key(store, receipts)
        try:
            os.stat(key, dir_fd=authority.chains_fd, follow_symlinks=False)
        except FileNotFoundError:
            if any(value[0]["chain_key"] == key for value in references.values()):
                raise _invalid("referenced projection is missing; rebuilding is forbidden") from None
            _publish_projection(authority, key, store, receipts, owner, lease_set)
        complete, binding, stamp = _projection(authority, key, store, receipts)
        _chain_references(authority, references, key, store, binding, stamp)
        _inputs(store, receipts, owner, lease_set)
        _new_record(
            authority.refs_fd,
            name,
            lambda identity: _reference_value(
                authority,
                owner,
                lease_set,
                key,
                binding,
                stamp,
                identity,
            ),
        )
        references[name] = (*_read_record(authority.refs_fd, name), lease_set)
        return _export(authority, complete, binding, stamp, references[name])


@_boundary
def load_vz_lower_export(
    roots: StatePaths,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
) -> VZLowerExport:
    """Reopen exact retained authority only; no mkdir, chmod, copy or adoption."""
    with _authority(roots) as authority:
        return _load(authority, store, receipts, owner, lease_set)


@_boundary
def verify_vz_lower_export(
    export: VZLowerExport,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
) -> None:
    """Mandatory immediately before every create/start/resume provider call."""
    if type(export) is not VZLowerExport or not export._state_root.is_absolute():
        raise _invalid("retained export handle is invalid")
    roots = StatePaths(config=export._state_root, state=export._state_root)
    with _authority(roots) as authority:
        if _load(authority, store, receipts, owner, lease_set) != export:
            raise _invalid("retained export handle or run reference changed")


@_boundary
def release_vz_lower_export(
    export: VZLowerExport,
    store: OCIStore,
    receipts: tuple[DerivedLayerReceipt, ...],
    owner: ArtifactLeaseOwner,
    lease_set: DurableLeaseSet,
    *,
    provider_absent: bool,
) -> None:
    """Drop only this exact run's transport custody after provider absence.

    Other complete run references retain shared bytes, including stopped runs.
    Last-consumer removal keeps the run reference until every payload/metadata
    removal is durably complete. Interrupted removal therefore fails closed and
    keeps canonical custody, rather than granting permission to rebuild/resume.
    This function does NOT release caller-owned canonical leases: their owner may
    release them separately only after successful transport teardown.
    """
    if provider_absent is not True:
        raise _invalid("release requires exact provider absence")
    if type(export) is not VZLowerExport or not export._state_root.is_absolute():
        raise _invalid("retained export handle is invalid")
    roots = StatePaths(config=export._state_root, state=export._state_root)
    with _authority(roots) as authority:
        references = _references(authority)
        if _load(authority, store, receipts, owner, lease_set, references=references) != export:
            raise _invalid("release authority changed")
        key = _key(store, receipts)
        name = _owner_key(owner) + ".json"
        consumers = _chain_references(
            authority,
            references,
            key,
            store,
            export.binding_digest,
            export._completion_stamp,
        )
        if consumers == [name]:
            # Mutation is deliberately last-consumer-only and descriptor-relative.
            # No recursive deletion, global prune, lease release, or chmod of CAS.
            with ExitStack() as stack:
                chain_fd = _open_directory(stack, authority.chains_fd, key, 0o500)
                payload_fd = _open_directory(stack, chain_fd, "payload", 0o500)
                if (
                    _identity(os.fstat(chain_fd)) != export._chain_identity
                    or _identity(os.fstat(payload_fd)) != export._payload_identity
                ):
                    raise _invalid("projection directory changed before release")
                completion_fd = os.open("complete.json", _READ_FLAGS, dir_fd=chain_fd)
                stack.callback(os.close, completion_fd)
                if _stamp(_entry(chain_fd, "complete.json", completion_fd, mode=0o400)) != export._completion_stamp:
                    raise _invalid("completion changed before release")
                unique = {entry.filename: entry for entry in export.files}
                descriptors = {}
                for filename, target in unique.items():
                    fd = os.open(filename, _READ_FLAGS, dir_fd=payload_fd)
                    stack.callback(os.close, fd)
                    expected = (
                        target.device,
                        target.inode,
                        target.uid,
                        target.gid,
                        0o400,
                        1,
                        target.size,
                        target.mtime_ns,
                        target.ctime_ns,
                    )
                    if _stamp(_entry(payload_fd, filename, fd, mode=0o400)) != expected:
                        raise _invalid("payload changed before release")
                    descriptors[filename] = fd
                if set(os.listdir(payload_fd)) != set(unique) or set(os.listdir(chain_fd)) != {
                    "payload",
                    "complete.json",
                }:
                    raise _invalid("projection allowlist changed before release")
                os.fchmod(payload_fd, 0o700)
                os.fsync(payload_fd)
                for filename in sorted(unique):
                    _entry(payload_fd, filename, descriptors[filename], mode=0o400)
                    os.unlink(filename, dir_fd=payload_fd)
                os.fsync(payload_fd)
                os.fchmod(chain_fd, 0o700)
                os.fsync(chain_fd)
                os.rmdir("payload", dir_fd=chain_fd)
                os.unlink("complete.json", dir_fd=chain_fd)
                os.fsync(chain_fd)
                os.rmdir(key, dir_fd=authority.chains_fd)
                os.fsync(authority.chains_fd)
        elif name not in consumers:
            raise _invalid("release has no exact accounted consumer")
        # A missing/partially removed projection above leaves this ref untouched.
        _value, _raw, stamp, _leases = references[name]
        fd = os.open(name, _READ_FLAGS, dir_fd=authority.refs_fd)
        try:
            if _stamp(_entry(authority.refs_fd, name, fd, mode=0o400)) != stamp:
                raise _invalid("run reference changed before release")
            os.unlink(name, dir_fd=authority.refs_fd)
            os.fsync(authority.refs_fd)
        finally:
            os.close(fd)
