"""Run-owned sealed BOOT copies, selected before the exact domain projection."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps

from .errors import StateError
from .oci_acl import parse_qemu_dac_baselabel
from .oci_runtime_access import _no_monitor_journal
from .oci_stage1_access import _immutable_stamp
from .state import locked_existing_run, read_run_ledger_snapshot

OCI_BOOT_EXPORTS_STATE_KEY = "oci_boot_exports"
BOOT_EXPORT_FILENAMES = {"kernel": "boot-kernel", "initramfs": "boot-initramfs"}
_SCHEMA = "palimpsest.oci-boot-exports.v1"


def _invalid():
    return StateError("OCI BOOT export authority is invalid or changed")


def _state_boundary(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except StateError:
            raise
        except Exception:
            raise _invalid() from None

    return call


def _digest(value):
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(c in "0123456789abcdef" for c in value[7:])
    )


@dataclass(frozen=True, slots=True)
class BootExportTarget:
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
            or not 0 < self.size_bytes <= 1024 * 1024 * 1024
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
class BootExportReceipt:
    export_id: str
    phase: str
    run_id: str
    run_name: str
    resource_plan_digest: str
    qemu_uid: int
    qemu_gid: int
    kernel: BootExportTarget
    initramfs: BootExportTarget

    def __post_init__(self):
        try:
            valid = all(
                type(value) is str and str(uuid.UUID(value)) == value for value in (self.export_id, self.run_id)
            )
        except (ValueError, AttributeError):
            valid = False
        from .kvm import _valid_name

        if (
            not valid
            or self.phase not in {"intent", "ready"}
            or not isinstance(self.run_name, str)
            or not _valid_name(self.run_name)
            or not _digest(self.resource_plan_digest)
            or any(type(v) is not int or not 0 < v < 2**32 - 1 for v in (self.qemu_uid, self.qemu_gid))
            or any(type(getattr(self, role)) is not BootExportTarget for role in BOOT_EXPORT_FILENAMES)
        ):
            raise _invalid()
        for role in BOOT_EXPORT_FILENAMES:
            target = getattr(self, role)
            target.__post_init__()
            if target.uid != os.geteuid() or target.uid == self.qemu_uid:
                raise _invalid()
        if (self.kernel.device, self.kernel.inode) == (self.initramfs.device, self.initramfs.inode):
            raise _invalid()

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            **{
                key: getattr(self, key).to_dict() if key in BOOT_EXPORT_FILENAMES else getattr(self, key)
                for key in self.__dataclass_fields__
            },
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", *cls.__dataclass_fields__}
            or value["schema"] != _SCHEMA
        ):
            raise _invalid()
        return cls(
            **{
                key: BootExportTarget.from_dict(value[key]) if key in BOOT_EXPORT_FILENAMES else value[key]
                for key in cls.__dataclass_fields__
            }
        )

    @property
    def dac_label(self):
        return f"+{self.qemu_uid}:+{self.qemu_gid}"

    def boot_dict(self):
        from .oci_root_kvm import OCI_ROOT_BOOT_ARTIFACT_POLICY

        return {
            "architecture": "x86_64",
            "policy": OCI_ROOT_BOOT_ARTIFACT_POLICY,
            **{
                role: {"digest": getattr(self, role).digest, "size_bytes": getattr(self, role).size_bytes}
                for role in BOOT_EXPORT_FILENAMES
            },
        }


def _receipt(state, record):
    value = BootExportReceipt.from_dict(state.get(OCI_BOOT_EXPORTS_STATE_KEY))
    if (value.run_id, value.run_name) != (record.run_id, record.name):
        raise _invalid()
    from .oci_root_prepare import OCIRootPreparationTransaction

    transaction = OCIRootPreparationTransaction.from_dict(state.get("oci_root"))
    if transaction.boot_plan_digest != value.resource_plan_digest or (
        transaction.owner.run_id,
        transaction.owner.run_name,
    ) != (value.run_id, value.run_name):
        raise _invalid()
    plan = state.get("oci_root_domain")
    if plan is not None:
        from .oci_stage1_access import _plan

        parsed = _plan(state)
        if (
            parsed.resource_plan_digest != value.resource_plan_digest
            or dict(parsed.boot_artifacts) != value.boot_dict()
        ):
            raise _invalid()
    return value


def _metadata(info, target, *, modes=(0o400,)):
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink)
        != (target.device, target.inode, target.uid, target.gid, 1)
        or stat.S_IMODE(info.st_mode) not in modes
    ):
        raise _invalid()


def _verify_payload(fd, target):
    before = os.fstat(fd)
    if before.st_size != target.size_bytes:
        raise _invalid()
    digest = hashlib.sha256()
    offset = 0
    while offset < target.size_bytes:
        chunk = os.pread(fd, min(1024 * 1024, target.size_bytes - offset), offset)
        if not chunk:
            raise _invalid()
        digest.update(chunk)
        offset += len(chunk)
    if "sha256:" + digest.hexdigest() != target.digest or _immutable_stamp(os.fstat(fd)) != _immutable_stamp(before):
        raise _invalid()


@contextmanager
def _pinned_pair(run_fd, receipt, *, writable=False):
    descriptors = {}
    try:
        for role, name in BOOT_EXPORT_FILENAMES.items():
            visible = os.stat(name, dir_fd=run_fd, follow_symlinks=False)
            target = getattr(receipt, role)
            _metadata(visible, target, modes=(0o400, 0o440, 0o600) if writable else (0o400, 0o440))
            flags = os.O_RDWR if writable and stat.S_IMODE(visible.st_mode) == 0o600 else os.O_RDONLY
            fd = os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_fd)
            descriptors[role] = fd
            _metadata(os.fstat(fd), target, modes=(stat.S_IMODE(visible.st_mode),))
        yield descriptors
    finally:
        for fd in descriptors.values():
            os.close(fd)


def _tail(run_fd, receipt, descriptors, modes, *, stamps=None):
    for role, name in BOOT_EXPORT_FILENAMES.items():
        fd, target = descriptors[role], getattr(receipt, role)
        _verify_payload(fd, target)
        for info in (os.fstat(fd), os.stat(name, dir_fd=run_fd, follow_symlinks=False)):
            _metadata(info, target, modes=(modes[role],))
            if stamps is not None and _immutable_stamp(info) != stamps[role]:
                raise _invalid()


def _write(mutation, receipt):
    state = mutation.mutable_state()
    state[OCI_BOOT_EXPORTS_STATE_KEY] = receipt.to_dict()
    mutation.write_state(state["status"], state)


@_state_boundary
def publish_oci_boot_exports(roots, prepared, boot_artifacts, *, conn, qemu_uid=None, qemu_gid=None):
    """Seal independent copies before planning; only recorded partial inodes resume."""
    from .oci_root_kvm import VerifiedHostBootArtifacts, verify_host_boot_artifacts
    from .oci_root_prepare import OCIRootPreparationTransaction

    if type(boot_artifacts) is not VerifiedHostBootArtifacts:
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
            transaction != OCIRootPreparationTransaction.from_dict(state.get("oci_root"))
            or transaction.phase != "resources-ready"
            or transaction.owner.run_id != mutation.record.run_id
        ):
            raise _invalid()
        if OCI_BOOT_EXPORTS_STATE_KEY in state:
            receipt = _receipt(state, mutation.record)
            if receipt.boot_dict() != boot_artifacts.to_dict() or (receipt.qemu_uid, receipt.qemu_gid) != principal:
                raise _invalid()
            if receipt.phase == "ready":
                select_boot_exports(roots, mutation.snapshot, boot_artifacts)
                mutation.verify_binding()
                return receipt
        else:
            receipt = None
        if (
            transaction != OCIRootPreparationTransaction.from_dict(state.get("oci_root"))
            or transaction.phase != "resources-ready"
            or transaction.owner.run_id != mutation.record.run_id
            or any(
                key in state
                for key in ("oci_root_domain", "oci_root_definition", "oci_root_handoff", "oci_boot_access")
            )
        ):
            raise _invalid()
        _no_monitor_journal(mutation)
        if receipt is None:
            checked = verify_host_boot_artifacts(
                boot_artifacts.kernel.path,
                boot_artifacts.initramfs.path,
                expected_kernel_digest=boot_artifacts.kernel.digest,
                expected_initramfs_digest=boot_artifacts.initramfs.digest,
            )
            if checked != boot_artifacts:
                raise _invalid()
            created = {}
            intent_attempted = False
            try:
                targets = {}
                for role, name in BOOT_EXPORT_FILENAMES.items():
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=mutation._run_fd,
                    )
                    created[role] = fd
                    info = os.fstat(fd)
                    source = getattr(boot_artifacts, role)
                    targets[role] = BootExportTarget(
                        source.digest, source.size_bytes, info.st_dev, info.st_ino, info.st_uid, info.st_gid
                    )
                receipt = BootExportReceipt(
                    str(uuid.uuid4()),
                    "intent",
                    mutation.record.run_id,
                    mutation.record.name,
                    transaction.boot_plan_digest,
                    *principal,
                    **targets,
                )
                os.fsync(mutation._run_fd)
                intent_attempted = True
                _write(mutation, receipt)
            finally:
                for role, fd in created.items():
                    if not intent_attempted:
                        visible = os.stat(BOOT_EXPORT_FILENAMES[role], dir_fd=mutation._run_fd, follow_symlinks=False)
                        held = os.fstat(fd)
                        if (visible.st_dev, visible.st_ino) == (held.st_dev, held.st_ino):
                            os.unlink(BOOT_EXPORT_FILENAMES[role], dir_fd=mutation._run_fd)
                    os.close(fd)
        with _pinned_pair(mutation._run_fd, receipt, writable=True) as descriptors:
            for role, fd in descriptors.items():
                target = getattr(receipt, role)
                if stat.S_IMODE(os.fstat(fd).st_mode) == 0o400:
                    _verify_payload(fd, target)
                    os.fsync(fd)
                    continue
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    raise _invalid()
                source = getattr(boot_artifacts, role)
                source_fd = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
                try:
                    before = os.fstat(source_fd)
                    if (before.st_dev, before.st_ino, before.st_size) != (
                        source.device,
                        source.inode,
                        target.size_bytes,
                    ):
                        raise _invalid()
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or before.st_uid not in {0, os.geteuid()}
                        or before.st_mode & 0o022
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
                        for info in (os.fstat(source_fd), source.path.lstat())
                    ):
                        raise _invalid()
                    _verify_payload(fd, target)
                finally:
                    os.close(source_fd)
                os.fsync(fd)
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            _tail(mutation._run_fd, receipt, descriptors, dict.fromkeys(BOOT_EXPORT_FILENAMES, 0o400))
            if _receipt(mutation.mutable_state(), mutation.record) != receipt:
                raise _invalid()
            mutation.verify_binding()
            _no_monitor_journal(mutation)
            receipt = replace(receipt, phase="ready")
            _write(mutation, receipt)
            _tail(mutation._run_fd, receipt, descriptors, dict.fromkeys(BOOT_EXPORT_FILENAMES, 0o400))
            if _receipt(mutation.mutable_state(), mutation.record) != receipt:
                raise _invalid()
            mutation.verify_binding()
            return receipt


@_state_boundary
def select_boot_exports(roots, snapshot, supplied=None):
    """Return selected copies and DAC policy, or the unchanged legacy selection."""
    state = snapshot.state
    if OCI_BOOT_EXPORTS_STATE_KEY not in state:
        if "oci_boot_access" in state:
            raise _invalid()
        for role, name in BOOT_EXPORT_FILENAMES.items():
            path = roots.runs / snapshot.record.name / name
            if path.exists() or path.is_symlink() or supplied is not None and getattr(supplied, role).path == path:
                raise _invalid()
        return supplied, None
    receipt = _receipt(state, snapshot.record)
    if receipt.phase != "ready" or supplied is not None and supplied.to_dict() != receipt.boot_dict():
        raise _invalid()
    from .oci_boot_access import verify_boot_context

    return verify_boot_context(roots, snapshot, receipt), receipt.dac_label


@_state_boundary
def load_oci_boot_exports(roots, run_name):
    snapshot = read_run_ledger_snapshot(roots, run_name)
    if OCI_BOOT_EXPORTS_STATE_KEY not in snapshot.state:
        raise _invalid()
    return select_boot_exports(roots, snapshot)[0]
