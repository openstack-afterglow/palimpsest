"""Private run-owned sealed lower copies; CAS objects and occurrence leases stay intact."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace

from .errors import StateError
from .kvm import MAX_OCI_ROOT_LAYER_DISKS, _valid_name
from .oci_acl import parse_qemu_dac_baselabel
from .oci_boot_exports import _digest, _metadata, _state_boundary, _verify_payload
from .oci_root_prepare import OCIRootPreparationTransaction, PreparedOCIRootRun
from .oci_runtime_access import _no_monitor_journal
from .oci_stage1_access import _immutable_stamp
from .oci_store import MAX_OCI_STORE_IMAGE_BYTES, OCIStore
from .state import locked_existing_run, read_run_ledger_snapshot

OCI_LOWER_EXPORTS_STATE_KEY = "oci_lower_exports"
_SCHEMA = "palimpsest.oci-lower-exports.v1"
_PREFIX = "lower-"


def _invalid():
    return StateError("OCI lower export authority is invalid or changed")


@dataclass(frozen=True, slots=True)
class LowerExportTarget:
    digest: str
    size_bytes: int
    device: int
    inode: int
    uid: int
    gid: int

    def __post_init__(self):
        if (
            not _digest(self.digest)
            or any(type(v) is not int or v < 0 for v in (self.size_bytes, self.device, self.inode, self.uid, self.gid))
            or self.inode == 0
            or not 0 < self.size_bytes <= MAX_OCI_STORE_IMAGE_BYTES
        ):
            raise _invalid()

    def to_dict(self):
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise _invalid()
        return cls(**value)


@dataclass(frozen=True, slots=True)
class LowerExportReceipt:
    export_id: str
    phase: str
    run_id: str
    run_name: str
    resource_plan_digest: str
    lower_lease_set_id: str
    lower_graph_digest: str
    qemu_uid: int
    qemu_gid: int
    targets: tuple[LowerExportTarget, ...]

    def __post_init__(self):
        try:
            valid = all(type(v) is str and str(uuid.UUID(v)) == v for v in (self.export_id, self.run_id))
        except (ValueError, AttributeError):
            valid = False
        if (
            not valid
            or self.phase not in {"intent", "ready"}
            or not isinstance(self.run_name, str)
            or not _valid_name(self.run_name)
            or not all(
                _digest(v) for v in (self.resource_plan_digest, self.lower_lease_set_id, self.lower_graph_digest)
            )
            or any(type(v) is not int or not 0 < v < 2**32 - 1 for v in (self.qemu_uid, self.qemu_gid))
            or type(self.targets) is not tuple
            or not 0 < len(self.targets) <= MAX_OCI_ROOT_LAYER_DISKS
            or any(type(v) is not LowerExportTarget for v in self.targets)
        ):
            raise _invalid()
        for target in self.targets:
            target.__post_init__()
            if target.uid != os.geteuid() or target.uid == self.qemu_uid:
                raise _invalid()
        if tuple(t.digest for t in self.targets) != tuple(sorted({t.digest for t in self.targets})) or len(
            {(t.device, t.inode) for t in self.targets}
        ) != len(self.targets):
            raise _invalid()

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            **{key: getattr(self, key) for key in self.__dataclass_fields__ if key != "targets"},
            "targets": [target.to_dict() for target in self.targets],
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", *cls.__dataclass_fields__}
            or value["schema"] != _SCHEMA
            or not isinstance(value["targets"], (list, tuple))
        ):
            raise _invalid()
        return cls(
            **{key: value[key] for key in cls.__dataclass_fields__ if key != "targets"},
            targets=tuple(LowerExportTarget.from_dict(v) for v in value["targets"]),
        )

    @property
    def dac_label(self):
        return f"+{self.qemu_uid}:+{self.qemu_gid}"


def _filenames(receipt):
    return {target.digest: _PREFIX + target.digest[7:] for target in receipt.targets}


def _target(receipt, digest):
    return next(target for target in receipt.targets if target.digest == digest)


def _receipt(state, record):
    receipt = LowerExportReceipt.from_dict(state.get(OCI_LOWER_EXPORTS_STATE_KEY))
    transaction = OCIRootPreparationTransaction.from_dict(state.get("oci_root"))
    from .oci_boot_exports import _receipt as boot_receipt

    boot = boot_receipt(state, record)
    if (
        (receipt.run_id, receipt.run_name) != (record.run_id, record.name)
        or transaction.owner.run_id != record.run_id
        or transaction.owner.run_name != record.name
        or transaction.phase != "resources-ready"
        or boot.phase != "ready"
        or (boot.qemu_uid, boot.qemu_gid) != (receipt.qemu_uid, receipt.qemu_gid)
        or (receipt.resource_plan_digest, receipt.lower_lease_set_id, receipt.lower_graph_digest)
        != (transaction.boot_plan_digest, transaction.lower_lease_set_id, transaction.lower_graph_digest)
        or {(target.digest, target.size_bytes) for target in receipt.targets}
        != {(layer.image_digest, layer.image_size) for layer in transaction.receipts}
    ):
        raise _invalid()
    if "oci_root_domain" in state:
        from .oci_stage1_access import _plan

        plan = _plan(state)
        if (
            plan.resource_plan_digest != receipt.resource_plan_digest
            or plan.lower_lease_set_id != receipt.lower_lease_set_id
            or plan.lower_graph_digest != receipt.lower_graph_digest
            or tuple((layer["image_digest"], layer["occurrence_digest"], layer["size_bytes"]) for layer in plan.layers)
            != tuple((layer.image_digest, layer.occurrence_digest, layer.image_size) for layer in transaction.receipts)
        ):
            raise _invalid()
    return receipt


@contextmanager
def _pinned_pair(run_fd, receipt, *, writable=False):
    descriptors = {}
    try:
        for digest, name in _filenames(receipt).items():
            visible = os.stat(name, dir_fd=run_fd, follow_symlinks=False)
            target = _target(receipt, digest)
            _metadata(visible, target, modes=(0o400, 0o440, 0o600) if writable else (0o400, 0o440))
            flags = os.O_RDWR if writable and stat.S_IMODE(visible.st_mode) == 0o600 else os.O_RDONLY
            fd = os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_fd)
            descriptors[digest] = fd
            _metadata(os.fstat(fd), target, modes=(stat.S_IMODE(visible.st_mode),))
        yield descriptors
    finally:
        for fd in descriptors.values():
            os.close(fd)


def _tail(run_fd, receipt, descriptors, modes, *, stamps=None):
    for digest, name in _filenames(receipt).items():
        fd, target = descriptors[digest], _target(receipt, digest)
        _verify_payload(fd, target)
        for info in (os.fstat(fd), os.stat(name, dir_fd=run_fd, follow_symlinks=False)):
            _metadata(info, target, modes=(modes[digest],))
            if stamps is not None and _immutable_stamp(info) != stamps[digest]:
                raise _invalid()


def _write(mutation, receipt):
    state = mutation.mutable_state()
    state[OCI_LOWER_EXPORTS_STATE_KEY] = receipt.to_dict()
    mutation.write_state(state["status"], state)


def _leases(store, transaction):
    leases = store.load_lease_set(
        transaction.lower_lease_set_id, transaction.owner, plan_digest=transaction.boot_plan_digest
    )
    if tuple(member.receipt for member in leases.members) != transaction.receipts:
        raise _invalid()
    return leases


@_state_boundary
def publish_oci_lower_exports(roots, prepared, store, *, conn, qemu_uid=None, qemu_gid=None):
    """Copy each distinct leased digest once; preserve ordered logical occurrences."""
    if type(prepared) is not PreparedOCIRootRun or not isinstance(store, OCIStore):
        raise _invalid()
    transaction = prepared.transaction
    principal = parse_qemu_dac_baselabel(conn.getCapabilities())
    if (
        principal[0] in {0, os.geteuid()}
        or principal[1] == 0
        or any(
            v is not None and (type(v) is not int or v != p)
            for v, p in ((qemu_uid, principal[0]), (qemu_gid, principal[1]))
        )
    ):
        raise _invalid()
    with locked_existing_run(roots, transaction.owner.run_name) as mutation:
        state = mutation.mutable_state()
        if (
            OCIRootPreparationTransaction.from_dict(state.get("oci_root")) != transaction
            or transaction.owner.run_id != mutation.record.run_id
        ):
            raise _invalid()
        _leases(store, transaction)
        from .oci_boot_exports import _receipt as boot_receipt

        boot = boot_receipt(state, mutation.record)
        if boot.phase != "ready" or (boot.qemu_uid, boot.qemu_gid) != principal:
            raise _invalid()
        receipt = _receipt(state, mutation.record) if OCI_LOWER_EXPORTS_STATE_KEY in state else None
        if receipt is not None:
            if (receipt.qemu_uid, receipt.qemu_gid) != principal:
                raise _invalid()
            if receipt.phase == "ready":
                select_lower_exports(roots, mutation.snapshot)
                mutation.verify_binding()
                return receipt
        if any(
            key in state for key in ("oci_root_domain", "oci_root_definition", "oci_root_handoff", "oci_lower_access")
        ):
            raise _invalid()
        _no_monitor_journal(mutation)
        images = dict(sorted((layer.image_digest, layer.image_size) for layer in transaction.receipts))
        if receipt is None:
            created, targets = {}, []
            intent_attempted = False
            try:
                for digest, size in images.items():
                    name = _PREFIX + digest[7:]
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=mutation._run_fd,
                    )
                    created[digest] = fd
                    info = os.fstat(fd)
                    targets.append(LowerExportTarget(digest, size, info.st_dev, info.st_ino, info.st_uid, info.st_gid))
                receipt = LowerExportReceipt(
                    str(uuid.uuid4()),
                    "intent",
                    mutation.record.run_id,
                    mutation.record.name,
                    transaction.boot_plan_digest,
                    transaction.lower_lease_set_id,
                    transaction.lower_graph_digest,
                    *principal,
                    tuple(targets),
                )
                os.fsync(mutation._run_fd)
                intent_attempted = True
                _write(mutation, receipt)
            finally:
                for digest, fd in created.items():
                    if not intent_attempted:
                        visible = os.stat(_PREFIX + digest[7:], dir_fd=mutation._run_fd, follow_symlinks=False)
                        held = os.fstat(fd)
                        if (visible.st_dev, visible.st_ino) == (held.st_dev, held.st_ino):
                            os.unlink(_PREFIX + digest[7:], dir_fd=mutation._run_fd)
                    os.close(fd)
        with _pinned_pair(mutation._run_fd, receipt, writable=True) as descriptors:
            for digest, fd in descriptors.items():
                target = _target(receipt, digest)
                if stat.S_IMODE(os.fstat(fd).st_mode) == 0o400:
                    _verify_payload(fd, target)
                    os.fsync(fd)
                    continue
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    raise _invalid()
                from .oci_root_kvm import _verified_lower_path

                path = _verified_lower_path(roots, digest, target.size_bytes)
                source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
                try:
                    before = os.fstat(source_fd)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or before.st_size != target.size_bytes
                        or before.st_uid != os.geteuid()
                        or stat.S_IMODE(before.st_mode) not in {0o400, 0o444}
                    ):
                        raise _invalid()
                    os.ftruncate(fd, 0)
                    offset = 0
                    while offset < target.size_bytes:
                        chunk = os.pread(source_fd, min(1024 * 1024, target.size_bytes - offset), offset)
                        if not chunk:
                            raise _invalid()
                        while chunk:
                            written = os.write(fd, chunk)
                            if written <= 0:
                                raise _invalid()
                            offset += written
                            chunk = chunk[written:]
                    if any(
                        _immutable_stamp(info) != _immutable_stamp(before)
                        for info in (os.fstat(source_fd), path.lstat())
                    ):
                        raise _invalid()
                    _verify_payload(fd, target)
                finally:
                    os.close(source_fd)
                os.fsync(fd)
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            _leases(store, transaction)
            _tail(mutation._run_fd, receipt, descriptors, dict.fromkeys(descriptors, 0o400))
            if _receipt(mutation.mutable_state(), mutation.record) != receipt:
                raise _invalid()
            mutation.verify_binding()
            _no_monitor_journal(mutation)
            receipt = replace(receipt, phase="ready")
            _write(mutation, receipt)
            _tail(mutation._run_fd, receipt, descriptors, dict.fromkeys(descriptors, 0o400))
            if _receipt(mutation.mutable_state(), mutation.record) != receipt:
                raise _invalid()
            mutation.verify_binding()
            return receipt


@_state_boundary
def select_lower_exports(roots, snapshot):
    """Return digest→owned path, or None for a namespace with no managed exports."""
    if OCI_LOWER_EXPORTS_STATE_KEY not in snapshot.state:
        if "oci_lower_access" in snapshot.state:
            raise _invalid()
        path = roots.runs / snapshot.record.name
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if any(name.startswith(_PREFIX) for name in os.listdir(fd)):
                raise _invalid()
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (path.lstat().st_dev, path.lstat().st_ino):
                raise _invalid()
        finally:
            os.close(fd)
        return None
    receipt = _receipt(snapshot.state, snapshot.record)
    if receipt.phase != "ready":
        raise _invalid()
    from .oci_lower_access import verify_lower_context

    return verify_lower_context(roots, snapshot, receipt)


@_state_boundary
def load_oci_lower_exports(roots, run_name):
    snapshot = read_run_ledger_snapshot(roots, run_name)
    if OCI_LOWER_EXPORTS_STATE_KEY not in snapshot.state:
        raise _invalid()
    return select_lower_exports(roots, snapshot)
