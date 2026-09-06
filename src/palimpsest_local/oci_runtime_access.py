"""Private, durable access to untrusted OCI I/O and traversal of its exact run.

No shared ancestor, boot artifact, root volume, monitor socket or run ledger ACL is
changed here. Interrupted grants resume only while no monitor journal exists;
restoration requires a stale terminal writer and completed inactive cleanup.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace

from .errors import PalimpsestError, StateError
from .oci_acl import ACLStructure, LinuxFdACLBackend, baseline_acl, grant_acl, parse_qemu_dac_baselabel, traversal_acl
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
from .oci_root_kvm import OCIRootDomainPlan
from .oci_runtime_io import (
    OCI_RUNTIME_CONSOLE_FILENAME,
    OCI_RUNTIME_DIRECTORY,
    OCI_RUNTIME_LIFECYCLE_FILENAME,
    RuntimeIOReceipt,
    _RuntimeIODescriptors,
    _verify_parent,
)
from .state import StatePaths, locked_existing_run

OCI_RUNTIME_ACCESS_STATE_KEY = "oci_runtime_access"
_SCHEMA = "palimpsest.oci-runtime-access.v2"


def _invalid() -> StateError:
    return StateError("OCI runtime access authority is invalid or changed")


def _digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeAccessTarget:
    device: int
    inode: int
    uid: int
    gid: int
    nlink: int
    directory: bool
    baseline: ACLStructure
    granted: ACLStructure

    def __post_init__(self):
        if (
            any(
                type(value) is not int or value < 0
                for value in (self.device, self.inode, self.uid, self.gid, self.nlink)
            )
            or self.inode == 0
            or self.nlink == 0
            or type(self.directory) is not bool
            or (not self.directory and self.nlink != 1)
            or type(self.baseline) is not ACLStructure
            or type(self.granted) is not ACLStructure
            or self.baseline != baseline_acl(directory=self.directory)
        ):
            raise _invalid()

    def to_dict(self):
        return {
            "device": self.device,
            "inode": self.inode,
            "uid": self.uid,
            "gid": self.gid,
            "nlink": self.nlink,
            "directory": self.directory,
            "baseline": self.baseline.to_dict(),
            "granted": self.granted.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise _invalid()
        return cls(
            **{
                **dict(value),
                "baseline": ACLStructure.from_dict(value["baseline"]),
                "granted": ACLStructure.from_dict(value["granted"]),
            }
        )


@dataclass(frozen=True, slots=True)
class RuntimeAccessReceipt:
    access_id: str
    phase: str
    binding: MonitorPreActivationBinding
    runtime_io: RuntimeIOReceipt
    qemu_uid: int
    qemu_gid: int
    run: RuntimeAccessTarget
    directory: RuntimeAccessTarget
    console: RuntimeAccessTarget
    cleanup_digest: str | None = None

    def __post_init__(self):
        try:
            valid_id = type(self.access_id) is str and str(uuid.UUID(self.access_id)) == self.access_id
        except (ValueError, AttributeError):
            valid_id = False
        if (
            not valid_id
            or type(self.phase) is not str
            or self.phase not in {"intent", "granted", "revoking", "revoked"}
            or type(self.binding) is not MonitorPreActivationBinding
            or type(self.runtime_io) is not RuntimeIOReceipt
            or any(type(value) is not int or not 0 < value < 2**32 - 1 for value in (self.qemu_uid, self.qemu_gid))
            or type(self.directory) is not RuntimeAccessTarget
            or type(self.run) is not RuntimeAccessTarget
            or type(self.console) is not RuntimeAccessTarget
        ):
            raise _invalid()
        MonitorPreActivationBinding.__post_init__(self.binding)
        RuntimeIOReceipt.__post_init__(self.runtime_io)
        if (
            (self.runtime_io.run_id, self.runtime_io.run_name, self.runtime_io.plan_digest)
            != (self.binding.record.run_id, self.binding.record.name, self.binding.plan_digest)
            or self.qemu_uid == self.binding.owner_uid
            or not self.run.directory
            or self.run.nlink < 2
            or self.run.uid != self.binding.owner_uid
            or self.run.granted != traversal_acl(self.run.baseline, self.qemu_uid)
            or len({(target.device, target.inode) for target in (self.run, self.directory, self.console)}) != 3
            or not self.directory.directory
            or self.console.directory
            or (self.directory.device, self.directory.inode)
            != (self.runtime_io.directory_device, self.runtime_io.directory_inode)
            or (self.console.device, self.console.inode)
            != (self.runtime_io.console_device, self.runtime_io.console_inode)
            or any(
                target.uid != self.binding.owner_uid or target.granted != grant_acl(target.baseline, self.qemu_uid)
                for target in (self.directory, self.console)
            )
            or ((self.phase in {"intent", "granted"}) != (self.cleanup_digest is None))
            or (
                self.cleanup_digest is not None
                and (
                    type(self.cleanup_digest) is not str
                    or len(self.cleanup_digest) != 71
                    or not self.cleanup_digest.startswith("sha256:")
                    or any(char not in "0123456789abcdef" for char in self.cleanup_digest[7:])
                )
            )
        ):
            raise _invalid()

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            "access_id": self.access_id,
            "phase": self.phase,
            "binding": self.binding.to_dict(),
            "runtime_io": self.runtime_io.to_dict(),
            "qemu_uid": self.qemu_uid,
            "qemu_gid": self.qemu_gid,
            "run": self.run.to_dict(),
            "directory": self.directory.to_dict(),
            "console": self.console.to_dict(),
            "cleanup_digest": self.cleanup_digest,
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, Mapping)
            or set(value) != {"schema", *cls.__dataclass_fields__}
            or value["schema"] != _SCHEMA
        ):
            raise _invalid()
        try:
            return cls(
                value["access_id"],
                value["phase"],
                MonitorPreActivationBinding.from_dict(dict(value["binding"])),
                RuntimeIOReceipt.from_dict(value["runtime_io"]),
                value["qemu_uid"],
                value["qemu_gid"],
                RuntimeAccessTarget.from_dict(value["run"]),
                RuntimeAccessTarget.from_dict(value["directory"]),
                RuntimeAccessTarget.from_dict(value["console"]),
                value["cleanup_digest"],
            )
        except (TypeError, ValueError, KeyError, PalimpsestError):
            raise _invalid() from None


def _validate_target(info, target, acl, *, run_directory=False):
    mode = (
        (0o700 if target.directory else 0o600)
        if acl == target.baseline
        else (0o710 if run_directory else (0o730 if target.directory else 0o660))
    )
    if (
        (info.st_dev, info.st_ino, info.st_uid, info.st_gid) != (target.device, target.inode, target.uid, target.gid)
        or (info.st_nlink < 2 if run_directory else info.st_nlink != target.nlink)
        or stat.S_IMODE(info.st_mode) != mode
        or (not stat.S_ISDIR(info.st_mode) if target.directory else not stat.S_ISREG(info.st_mode))
        or target.uid != os.geteuid()
        or acl not in (target.baseline, target.granted)
    ):
        raise _invalid()


def verify_runtime_access(
    receipt,
    runtime_io,
    directory_fd,
    console_fd,
    directory_stat,
    console_stat,
    *,
    run_directory_fd,
    runs_directory_fd,
    acl_backend=None,
):
    """Read-only exact granted ACL verifier; safe beneath existing run locks."""
    if type(receipt) is not RuntimeAccessReceipt:
        receipt = RuntimeAccessReceipt.from_dict(receipt)
    RuntimeAccessReceipt.__post_init__(receipt)
    if receipt.phase != "granted" or receipt.runtime_io != runtime_io:
        raise _invalid()
    backend = acl_backend or LinuxFdACLBackend()
    for target, fd, visible, run_directory in (
        (
            receipt.run,
            run_directory_fd,
            os.stat(receipt.binding.record.name, dir_fd=runs_directory_fd, follow_symlinks=False),
            True,
        ),
        (receipt.directory, directory_fd, directory_stat, False),
        (receipt.console, console_fd, console_stat, False),
    ):
        _validate_target(os.fstat(fd), target, target.granted, run_directory=run_directory)
        _validate_target(visible, target, target.granted, run_directory=run_directory)
        if backend.read_acl(fd) != target.granted:
            raise _invalid()
        _validate_target(os.fstat(fd), target, target.granted, run_directory=run_directory)
    for target, fd, current, run_directory in (
        (
            receipt.run,
            run_directory_fd,
            os.stat(receipt.binding.record.name, dir_fd=runs_directory_fd, follow_symlinks=False),
            True,
        ),
        (
            receipt.directory,
            directory_fd,
            os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=run_directory_fd, follow_symlinks=False),
            False,
        ),
        (
            receipt.console,
            console_fd,
            os.stat(OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=directory_fd, follow_symlinks=False),
            False,
        ),
    ):
        _validate_target(os.fstat(fd), target, target.granted, run_directory=run_directory)
        _validate_target(current, target, target.granted, run_directory=run_directory)


@contextmanager
def _pinned_io(mutation, runtime_io):
    """Borrow the existing fork-safe descriptor holder without launch-mode gates."""
    descriptors = _RuntimeIODescriptors()
    try:
        _verify_parent(mutation)
        descriptors.open(
            "directory",
            OCI_RUNTIME_DIRECTORY,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=mutation._run_fd,
        )
        opened = os.fstat(descriptors.directory)
        visible = os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=mutation._run_fd, follow_symlinks=False)
        for info in (opened, visible):
            if (info.st_dev, info.st_ino, info.st_uid) != (
                runtime_io.directory_device,
                runtime_io.directory_inode,
                os.geteuid(),
            ):
                raise _invalid()
        # Verify the trusted receipt before opening any child in this directory.
        console = os.stat(OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=descriptors.directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(console.st_mode)
            or console.st_nlink != 1
            or (console.st_dev, console.st_ino) != (runtime_io.console_device, runtime_io.console_inode)
        ):
            raise _invalid()
        descriptors.open(
            "console",
            OCI_RUNTIME_CONSOLE_FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=descriptors.directory,
        )
        yield descriptors
    finally:
        descriptors.close()


def _no_monitor_journal(mutation):
    try:
        fd = os.open(
            "monitor-private", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=mutation._run_fd
        )
    except FileNotFoundError:
        return
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise _invalid()
        # Even a prepared socket/lock means bootstrap admission may be in flight.
        if os.listdir(fd):
            raise _invalid()
    finally:
        os.close(fd)


def _source_io(mutation, binding):
    if mutation.record != binding.record or binding.owner_uid != os.geteuid():
        raise _invalid()
    value = mutation.snapshot.state.get("oci_root_domain")
    if not isinstance(value, Mapping) or set(value) != {"digest", "plan"}:
        raise _invalid()
    plan = OCIRootDomainPlan.from_dict(value["plan"])
    if (value["digest"], plan.digest, plan.run_id, plan.run_name, plan.stage1_transport["artifact_digest"]) != (
        binding.plan_digest,
        binding.plan_digest,
        binding.record.run_id,
        binding.record.name,
        binding.stage1_artifact_digest,
    ):
        raise _invalid()
    result = RuntimeIOReceipt.from_dict(mutation.snapshot.state.get("oci_runtime_io"))
    if (result.run_id, result.run_name, result.plan_digest) != (
        binding.record.run_id,
        binding.record.name,
        binding.plan_digest,
    ):
        raise _invalid()
    return result


def _grant_authority(mutation, binding, conn, principal):
    _verify_parent(mutation)
    if mutation.snapshot.state.get("status") != "defined" or "oci_root_handoff" in mutation.snapshot.state:
        raise _invalid()
    if dict(mutation.snapshot.state.get("oci_root_definition", {})) != {
        "schema": "palimpsest.oci-root-definition.v2",
        "phase": "defined",
        "domain_uuid": binding.domain_uuid,
        "plan_digest": binding.plan_digest,
        "projection_digest": binding.expected_definition_projection_digest,
        "libvirt_uri": binding.libvirt_uri,
    }:
        raise _invalid()
    _no_monitor_journal(mutation)
    if parse_qemu_dac_baselabel(conn.getCapabilities()) != principal or _inspect_domain(conn, binding) is None:
        raise _invalid()
    if "oci_boot_exports" in mutation.snapshot.state:
        import xml.etree.ElementTree as ET

        from . import kvm
        from .oci_boot_exports import _receipt
        from .oci_root_runtime import _dac_projection

        exports = _receipt(mutation.mutable_state(), mutation.record)
        domain = _inspect_domain(conn, binding)
        if (
            exports.phase != "ready"
            or (exports.qemu_uid, exports.qemu_gid) != principal
            or domain is None
            or _dac_projection(ET.fromstring(domain.XMLDesc(kvm._libvirt().VIR_DOMAIN_XML_INACTIVE)))
            != exports.dac_label
        ):
            raise _invalid()
    _verify_parent(mutation)
    _no_monitor_journal(mutation)
    if conn.getURI() != binding.libvirt_uri:
        raise _invalid()


def _verify_pinned(mutation, descriptors, receipt, backend, expected):
    _verify_parent(mutation)
    if descriptors.pid != os.getpid() or descriptors.directory < 0 or descriptors.console < 0:
        raise _invalid()
    for role, target, fd, visible in (
        (
            "run",
            receipt.run,
            mutation._run_fd,
            os.stat(mutation.record.name, dir_fd=mutation._runs_fd, follow_symlinks=False),
        ),
        (
            "directory",
            receipt.directory,
            descriptors.directory,
            os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=mutation._run_fd, follow_symlinks=False),
        ),
        (
            "console",
            receipt.console,
            descriptors.console,
            os.stat(OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=descriptors.directory, follow_symlinks=False),
        ),
    ):
        acl = expected[role]
        _validate_target(os.fstat(fd), target, acl, run_directory=role == "run")
        _validate_target(visible, target, acl, run_directory=role == "run")
        if backend.read_acl(fd) != acl:
            raise _invalid()
    _verify_pinned_metadata(mutation, descriptors, receipt, expected)


def _verify_pinned_metadata(mutation, descriptors, receipt, expected):
    _verify_parent(mutation)
    for role, target, fd, current in (
        (
            "run",
            receipt.run,
            mutation._run_fd,
            os.stat(mutation.record.name, dir_fd=mutation._runs_fd, follow_symlinks=False),
        ),
        (
            "directory",
            receipt.directory,
            descriptors.directory,
            os.stat(OCI_RUNTIME_DIRECTORY, dir_fd=mutation._run_fd, follow_symlinks=False),
        ),
        (
            "console",
            receipt.console,
            descriptors.console,
            os.stat(OCI_RUNTIME_CONSOLE_FILENAME, dir_fd=descriptors.directory, follow_symlinks=False),
        ),
    ):
        _validate_target(os.fstat(fd), target, expected[role], run_directory=role == "run")
        _validate_target(current, target, expected[role], run_directory=role == "run")
    _verify_parent(mutation)


def _require_access_member(mutation, expected):
    if expected is None:
        if OCI_RUNTIME_ACCESS_STATE_KEY in mutation.snapshot.state:
            raise _invalid()
    elif RuntimeAccessReceipt.from_dict(mutation.snapshot.state.get(OCI_RUNTIME_ACCESS_STATE_KEY)) != expected:
        raise _invalid()


def _write_state(mutation, receipt):
    data = mutation.mutable_state()
    data[OCI_RUNTIME_ACCESS_STATE_KEY] = receipt.to_dict()
    mutation.write_state(data["status"], data)


def grant_oci_runtime_access(
    roots: StatePaths, binding: MonitorPreActivationBinding, *, conn, qemu_uid=None, qemu_gid=None, acl_backend=None
):
    """Grant console-rw, I/O-wx, then run-search to the exact DAC/KVM principal."""
    try:
        if type(roots) is not StatePaths or type(binding) is not MonitorPreActivationBinding:
            raise _invalid()
        backend = acl_backend or LinuxFdACLBackend()
        principal = parse_qemu_dac_baselabel(conn.getCapabilities())
        if principal[0] in {0, os.geteuid()} or principal[1] == 0:
            raise _invalid()
        for supplied, observed in ((qemu_uid, principal[0]), (qemu_gid, principal[1])):
            if supplied is not None and (type(supplied) is not int or supplied != observed):
                raise _invalid()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            runtime_io = _source_io(mutation, binding)
            _grant_authority(mutation, binding, conn, principal)
            with _pinned_io(mutation, runtime_io) as descriptors:
                has_saved = OCI_RUNTIME_ACCESS_STATE_KEY in mutation.snapshot.state
                saved = mutation.snapshot.state.get(OCI_RUNTIME_ACCESS_STATE_KEY)
                if not has_saved:
                    targets = []
                    for fd, directory, run_directory in (
                        (mutation._run_fd, True, True),
                        (descriptors.directory, True, False),
                        (descriptors.console, False, False),
                    ):
                        info = os.fstat(fd)
                        acl = backend.read_acl(fd)
                        if acl != baseline_acl(directory=directory):
                            raise _invalid()
                        target = RuntimeAccessTarget(
                            info.st_dev,
                            info.st_ino,
                            info.st_uid,
                            info.st_gid,
                            info.st_nlink,
                            directory,
                            acl,
                            traversal_acl(acl, principal[0]) if run_directory else grant_acl(acl, principal[0]),
                        )
                        _validate_target(info, target, acl, run_directory=run_directory)
                        targets.append(target)
                    receipt = RuntimeAccessReceipt(
                        str(uuid.uuid4()), "intent", binding, runtime_io, *principal, *targets
                    )
                else:
                    receipt = RuntimeAccessReceipt.from_dict(saved)
                    if (
                        receipt.phase not in {"intent", "granted"}
                        or receipt.binding != binding
                        or receipt.runtime_io != runtime_io
                        or (receipt.qemu_uid, receipt.qemu_gid) != principal
                    ):
                        raise _invalid()
                expected_record = receipt if has_saved else None
                expected = {
                    role: backend.read_acl(fd)
                    for role, fd in (
                        ("run", mutation._run_fd),
                        ("directory", descriptors.directory),
                        ("console", descriptors.console),
                    )
                }
                allowed = [
                    (receipt.run.baseline, receipt.directory.baseline, receipt.console.baseline),
                    (receipt.run.baseline, receipt.directory.baseline, receipt.console.granted),
                    (receipt.run.baseline, receipt.directory.granted, receipt.console.granted),
                    (receipt.run.granted, receipt.directory.granted, receipt.console.granted),
                ]
                if (expected["run"], expected["directory"], expected["console"]) not in (
                    allowed if receipt.phase == "intent" else allowed[-1:]
                ):
                    raise _invalid()

                def verify():
                    _require_access_member(mutation, expected_record)
                    if _source_io(mutation, binding) != runtime_io:
                        raise _invalid()
                    _grant_authority(mutation, binding, conn, principal)
                    _verify_pinned(mutation, descriptors, receipt, backend, expected)
                    try:
                        os.stat(OCI_RUNTIME_LIFECYCLE_FILENAME, dir_fd=descriptors.directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise _invalid()
                    _grant_authority(mutation, binding, conn, principal)
                    _require_access_member(mutation, expected_record)
                    _verify_pinned_metadata(mutation, descriptors, receipt, expected)

                verify()
                if receipt.phase == "granted":
                    return receipt
                if not has_saved:
                    _write_state(mutation, receipt)
                    expected_record = receipt
                    verify()
                for role, target, fd in (
                    ("console", receipt.console, descriptors.console),
                    ("directory", receipt.directory, descriptors.directory),
                    ("run", receipt.run, mutation._run_fd),
                ):
                    if expected[role] != target.granted:
                        verify()
                        backend.write_acl(fd, target.granted)
                        expected[role] = target.granted
                    os.fsync(fd)
                    verify()
                if receipt.phase != "granted":
                    verify()
                    receipt = replace(receipt, phase="granted")
                    _write_state(mutation, receipt)
                    expected_record = receipt
                    verify()
                return receipt
    except StateError:
        raise
    except Exception:
        raise _invalid() from None


def revoke_oci_runtime_access(
    roots: StatePaths,
    binding: MonitorPreActivationBinding,
    *,
    conn,
    acl_backend=None,
    liveness_probe=probe_process_liveness,
):
    """Close run traversal first, then I/O and console after stale terminal cleanup."""
    authority = None
    try:
        if type(roots) is not StatePaths or type(binding) is not MonitorPreActivationBinding:
            raise _invalid()
        backend = acl_backend or LinuxFdACLBackend()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            runtime_io = _source_io(mutation, binding)
            receipt = RuntimeAccessReceipt.from_dict(mutation.snapshot.state.get(OCI_RUNTIME_ACCESS_STATE_KEY))
            if (
                receipt.phase not in {"granted", "revoking", "revoked"}
                or receipt.binding != binding
                or receipt.runtime_io != runtime_io
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
            if cleanup != expected_cleanup or journal.phase != "terminal":
                raise _invalid()
            cleanup_digest = _digest(cleanup.to_dict())
            if receipt.cleanup_digest is not None and receipt.cleanup_digest != cleanup_digest:
                raise _invalid()
            expected_record = receipt
            with _pinned_io(mutation, runtime_io) as descriptors:
                expected = {
                    role: backend.read_acl(fd)
                    for role, fd in (
                        ("run", mutation._run_fd),
                        ("directory", descriptors.directory),
                        ("console", descriptors.console),
                    )
                }
                allowed = [
                    (receipt.run.granted, receipt.directory.granted, receipt.console.granted),
                    (receipt.run.baseline, receipt.directory.granted, receipt.console.granted),
                    (receipt.run.baseline, receipt.directory.baseline, receipt.console.granted),
                    (receipt.run.baseline, receipt.directory.baseline, receipt.console.baseline),
                ]
                choices = (
                    allowed
                    if receipt.phase == "revoking"
                    else (allowed[:1] if receipt.phase == "granted" else allowed[-1:])
                )
                if (expected["run"], expected["directory"], expected["console"]) not in choices:
                    raise _invalid()

                def verify():
                    _require_access_member(mutation, expected_record)
                    if (
                        MonitorInactiveCleanupReceipt.from_dict(
                            mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                        )
                        != cleanup
                    ):
                        raise _invalid()
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _stale(journal, liveness_probe)
                    if _source_io(mutation, binding) != runtime_io or _inspect_domain(conn, binding) is not None:
                        raise _invalid()
                    authority.validate()
                    _stale(journal, liveness_probe)
                    if conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    _verify_pinned(mutation, descriptors, receipt, backend, expected)
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    authority.validate()
                    _stale(journal, liveness_probe)
                    _require_access_member(mutation, expected_record)
                    if (
                        MonitorInactiveCleanupReceipt.from_dict(
                            mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                        )
                        != cleanup
                    ):
                        raise _invalid()
                    _verify_pinned_metadata(mutation, descriptors, receipt, expected)

                verify()
                if receipt.phase == "revoked":
                    return receipt
                if receipt.phase == "granted":
                    receipt = replace(receipt, phase="revoking", cleanup_digest=cleanup_digest)
                    _write_state(mutation, receipt)
                    expected_record = receipt
                    verify()
                for role, target, fd in (
                    ("run", receipt.run, mutation._run_fd),
                    ("directory", receipt.directory, descriptors.directory),
                    ("console", receipt.console, descriptors.console),
                ):
                    if expected[role] != target.baseline:
                        verify()
                        backend.write_acl(fd, target.baseline)
                        expected[role] = target.baseline
                    os.fsync(fd)
                    verify()
                if receipt.phase != "revoked":
                    verify()
                    receipt = replace(receipt, phase="revoked")
                    _write_state(mutation, receipt)
                    expected_record = receipt
                    verify()
                return receipt
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if authority is not None:
            authority.close()
