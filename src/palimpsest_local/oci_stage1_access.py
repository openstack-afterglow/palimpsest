"""Exact run-owned immutable stage-1 read access, with durable recovery phases."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace

from .errors import ArtifactValidationError, StateError
from .oci_acl import (
    ACLStructure,
    LinuxFdACLBackend,
    parse_qemu_dac_baselabel,
    readonly_baseline_acl,
    readonly_grant_acl,
)
from .oci_monitor import probe_process_liveness
from .oci_monitor_ipc import MonitorPreActivationBinding
from .oci_monitor_recovery import (
    MonitorInactiveCleanupReceipt,
    _inspect_domain,
    _RecoveryAuthority,
    _stale,
    _validate_ledger,
)
from .oci_runtime_access import _digest, _grant_authority, _source_io
from .oci_stage1 import OCIStage1Plan
from .oci_stage1_transport import (
    OCI_STAGE1_TRANSPORT_FILENAME,
    OCIStage1TransportReceipt,
    verify_stage1_transport,
    verify_stage1_transport_file,
)
from .state import StatePaths, _read_pinned_json_object, locked_existing_run

OCI_STAGE1_ACCESS_STATE_KEY = "oci_stage1_access"
_SCHEMA = "palimpsest.oci-stage1-access.v1"


def _invalid():
    return StateError("OCI stage-1 access authority is invalid or changed")


@dataclass(frozen=True, slots=True)
class ImmutableAccessTarget:
    device: int
    inode: int
    uid: int
    gid: int
    nlink: int
    baseline: ACLStructure
    granted: ACLStructure

    def __post_init__(self):
        if (
            any(type(v) is not int or v < 0 for v in (self.device, self.inode, self.uid, self.gid, self.nlink))
            or self.inode == 0
            or self.nlink != 1
            or type(self.baseline) is not ACLStructure
            or self.baseline != readonly_baseline_acl()
            or type(self.granted) is not ACLStructure
            or len(self.granted.named_users) != 1
            or self.granted != readonly_grant_acl(self.granted.named_users[0][0])
        ):
            raise _invalid()

    def to_dict(self):
        return {
            key: getattr(self, key).to_dict() if key in {"baseline", "granted"} else getattr(self, key)
            for key in self.__dataclass_fields__
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
class Stage1AccessReceipt:
    access_id: str
    phase: str
    binding: MonitorPreActivationBinding
    transport: OCIStage1TransportReceipt
    qemu_uid: int
    qemu_gid: int
    target: ImmutableAccessTarget
    cleanup_digest: str | None = None

    def __post_init__(self):
        try:
            valid_uuid = type(self.access_id) is str and str(uuid.UUID(self.access_id)) == self.access_id
        except (ValueError, AttributeError):
            valid_uuid = False
        if (
            not valid_uuid
            or self.phase not in {"intent", "granted", "revoking", "revoked"}
            or type(self.binding) is not MonitorPreActivationBinding
            or type(self.transport) is not OCIStage1TransportReceipt
            or type(self.target) is not ImmutableAccessTarget
            or any(type(v) is not int or not 0 < v < 2**32 - 1 for v in (self.qemu_uid, self.qemu_gid))
        ):
            raise _invalid()
        self.binding.__post_init__()
        self.transport.__post_init__()
        self.target.__post_init__()
        if (
            self.target.uid != self.binding.owner_uid
            or self.qemu_uid == self.binding.owner_uid
            or self.target.granted != readonly_grant_acl(self.qemu_uid)
            or self.transport.artifact_digest != self.binding.stage1_artifact_digest
        ):
            raise _invalid()
        if self.phase in {"intent", "granted"}:
            if self.cleanup_digest is not None:
                raise _invalid()
        elif (
            type(self.cleanup_digest) is not str
            or len(self.cleanup_digest) != 71
            or not self.cleanup_digest.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in self.cleanup_digest[7:])
        ):
            raise _invalid()

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            **{
                key: getattr(self, key).to_dict() if key in {"binding", "transport", "target"} else getattr(self, key)
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
        try:
            return cls(
                value["access_id"],
                value["phase"],
                MonitorPreActivationBinding.from_dict(dict(value["binding"])),
                OCIStage1TransportReceipt.from_dict(dict(value["transport"])),
                value["qemu_uid"],
                value["qemu_gid"],
                ImmutableAccessTarget.from_dict(value["target"]),
                value["cleanup_digest"],
            )
        except (KeyError, TypeError, ValueError):
            raise _invalid() from None


def _plan(state, binding=None):
    from .oci_root_kvm import OCIRootDomainPlan

    value = state.get("oci_root_domain")
    if not isinstance(value, Mapping) or set(value) != {"digest", "plan"}:
        raise _invalid()
    plan = OCIRootDomainPlan.from_dict(value["plan"])
    if (
        value["digest"] != plan.digest
        or binding is not None
        and (plan.digest, plan.run_id, plan.run_name)
        != (binding.plan_digest, binding.record.run_id, binding.record.name)
    ):
        raise _invalid()
    return plan


def _transport(plan):
    return OCIStage1TransportReceipt.from_dict(
        {key: value for key, value in plan.stage1_transport.items() if key not in {"target", "serial"}}
    )


def _validate_metadata(info, receipt, acl):
    target = receipt.target
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_nlink, info.st_size)
        != (target.device, target.inode, target.uid, target.gid, 1, receipt.transport.artifact_size_bytes)
        or target.uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != (0o400 if acl == target.baseline else 0o440)
        or acl not in (target.baseline, target.granted)
    ):
        raise _invalid()


def _immutable_stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _metadata_tail(fd, run_fd, receipt, acl, *, expected_stamp=None):
    for info in (os.fstat(fd), os.stat(OCI_STAGE1_TRANSPORT_FILENAME, dir_fd=run_fd, follow_symlinks=False)):
        _validate_metadata(info, receipt, acl)
        if expected_stamp is not None and _immutable_stamp(info) != expected_stamp:
            raise _invalid()


def _verify_file(fd, run_fd, receipt, plan, acl, backend):
    _metadata_tail(fd, run_fd, receipt, acl)
    if _transport(plan) != receipt.transport or (plan.digest, plan.run_id, plan.run_name) != (
        receipt.binding.plan_digest,
        receipt.binding.record.run_id,
        receipt.binding.record.name,
    ):
        raise _invalid()
    before = os.fstat(fd)
    if backend.read_acl(fd) != acl:
        raise _invalid()
    _verify_payload(fd, receipt, plan)
    after = os.fstat(fd)
    if _immutable_stamp(before) != _immutable_stamp(after):
        raise _invalid()
    _metadata_tail(fd, run_fd, receipt, acl, expected_stamp=_immutable_stamp(before))
    return _immutable_stamp(before)


def _verify_payload(fd, receipt, plan):
    before = os.fstat(fd)
    data = os.pread(fd, receipt.transport.artifact_size_bytes + 1, 0)
    try:
        verify_stage1_transport(data, receipt.transport, expected_stage1_plan=OCIStage1Plan.from_domain_plan(plan))
    except ArtifactValidationError:
        raise _invalid() from None
    after = os.fstat(fd)
    if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_size,
    ):
        raise _invalid()


@contextmanager
def _pinned(mutation):
    mutation.verify_binding()
    fd = os.open(
        OCI_STAGE1_TRANSPORT_FILENAME,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=mutation._run_fd,
    )
    try:
        yield fd
    finally:
        os.close(fd)


def _member(mutation, expected):
    mutation.verify_binding()
    if (
        mutation.mutable_state().get(OCI_STAGE1_ACCESS_STATE_KEY) != (None if expected is None else expected.to_dict())
        or expected is None
        and OCI_STAGE1_ACCESS_STATE_KEY in mutation.snapshot.state
    ):
        raise _invalid()


def _write(mutation, receipt):
    data = mutation.mutable_state()
    data[OCI_STAGE1_ACCESS_STATE_KEY] = receipt.to_dict()
    mutation.write_state(data["status"], data)


def grant_oci_stage1_access(roots, binding, *, conn, qemu_uid=None, qemu_gid=None, acl_backend=None):
    """Grant exactly read-only access before monitor bootstrap admission."""
    try:
        if type(roots) is not StatePaths or type(binding) is not MonitorPreActivationBinding:
            raise _invalid()
        backend = acl_backend or LinuxFdACLBackend()
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
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            plan = _plan(mutation.snapshot.state, binding)
            _source_io(mutation, binding)
            _grant_authority(mutation, binding, conn, principal)
            with _pinned(mutation) as fd:
                saved = mutation.mutable_state().get(OCI_STAGE1_ACCESS_STATE_KEY)
                has_saved = OCI_STAGE1_ACCESS_STATE_KEY in mutation.snapshot.state
                if has_saved:
                    receipt = Stage1AccessReceipt.from_dict(saved)
                    if (
                        receipt.phase not in {"intent", "granted"}
                        or receipt.binding != binding
                        or (receipt.qemu_uid, receipt.qemu_gid) != principal
                    ):
                        raise _invalid()
                else:
                    info = os.fstat(fd)
                    receipt = Stage1AccessReceipt(
                        str(uuid.uuid4()),
                        "intent",
                        binding,
                        _transport(plan),
                        *principal,
                        ImmutableAccessTarget(
                            info.st_dev,
                            info.st_ino,
                            info.st_uid,
                            info.st_gid,
                            info.st_nlink,
                            readonly_baseline_acl(),
                            readonly_grant_acl(principal[0]),
                        ),
                    )
                expected = receipt if has_saved else None
                acl = backend.read_acl(fd)
                if acl not in (
                    (receipt.target.baseline, receipt.target.granted)
                    if has_saved and receipt.phase == "intent"
                    else (receipt.target.granted,)
                    if has_saved
                    else (receipt.target.baseline,)
                ):
                    raise _invalid()

                def verify():
                    _member(mutation, expected)
                    if _plan(mutation.snapshot.state, binding) != plan:
                        raise _invalid()
                    _grant_authority(mutation, binding, conn, principal)
                    stamp = _verify_file(fd, mutation._run_fd, receipt, plan, acl, backend)
                    _grant_authority(mutation, binding, conn, principal)
                    _member(mutation, expected)
                    _verify_payload(fd, receipt, plan)
                    _metadata_tail(fd, mutation._run_fd, receipt, acl, expected_stamp=stamp)

                verify()
                if receipt.phase == "granted":
                    return receipt
                if not has_saved:
                    _write(mutation, receipt)
                    expected = receipt
                    verify()
                if acl != receipt.target.granted:
                    backend.write_acl(fd, receipt.target.granted)
                    acl = receipt.target.granted
                os.fsync(fd)
                verify()
                receipt = replace(receipt, phase="granted")
                _write(mutation, receipt)
                expected = receipt
                verify()
                return receipt
    except StateError:
        raise
    except Exception:
        raise _invalid() from None


def revoke_oci_stage1_access(roots, binding, *, conn, acl_backend=None, liveness_probe=probe_process_liveness):
    """Restore immutable owner-only access after original stale terminal cleanup."""
    authority = None
    try:
        backend = acl_backend or LinuxFdACLBackend()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            receipt = Stage1AccessReceipt.from_dict(mutation.mutable_state().get(OCI_STAGE1_ACCESS_STATE_KEY))
            if receipt.binding != binding or receipt.phase not in {"granted", "revoking", "revoked"}:
                raise _invalid()
            plan = _plan(mutation.snapshot.state, binding)
            authority = _RecoveryAuthority(mutation, binding)
            journal = authority.snapshot
            cleanup = MonitorInactiveCleanupReceipt.from_dict(
                mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
            )
            exact = MonitorInactiveCleanupReceipt(
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
            digest = _digest(cleanup.to_dict())
            if journal.phase != "terminal" or cleanup != exact or receipt.cleanup_digest not in (None, digest):
                raise _invalid()
            expected = receipt
            with _pinned(mutation) as fd:
                acl = backend.read_acl(fd)
                if acl not in (
                    (receipt.target.baseline, receipt.target.granted)
                    if receipt.phase == "revoking"
                    else (receipt.target.baseline,)
                    if receipt.phase == "revoked"
                    else (receipt.target.granted,)
                ):
                    raise _invalid()

                def verify():
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    stamp = _verify_file(fd, mutation._run_fd, receipt, plan, acl, backend)
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _member(mutation, expected)
                    if (
                        _plan(mutation.snapshot.state, binding) != plan
                        or MonitorInactiveCleanupReceipt.from_dict(
                            mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                        )
                        != cleanup
                    ):
                        raise _invalid()
                    _verify_payload(fd, receipt, plan)
                    _metadata_tail(fd, mutation._run_fd, receipt, acl, expected_stamp=stamp)

                verify()
                if receipt.phase == "revoked":
                    return receipt
                if receipt.phase == "granted":
                    receipt = replace(receipt, phase="revoking", cleanup_digest=digest)
                    _write(mutation, receipt)
                    expected = receipt
                    verify()
                if acl != receipt.target.baseline:
                    backend.write_acl(fd, receipt.target.baseline)
                    acl = receipt.target.baseline
                os.fsync(fd)
                verify()
                receipt = replace(receipt, phase="revoked")
                _write(mutation, receipt)
                expected = receipt
                verify()
                return receipt
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if authority is not None:
            authority.close()


def _context_receipt(state, record, plan):
    if OCI_STAGE1_ACCESS_STATE_KEY not in state:
        return None
    receipt = Stage1AccessReceipt.from_dict(state[OCI_STAGE1_ACCESS_STATE_KEY])
    if (
        receipt.binding.record != record
        or receipt.binding.plan_digest != plan.digest
        or receipt.transport != _transport(plan)
    ):
        raise _invalid()
    return receipt


def verify_run_stage1_transport(roots, snapshot, plan):
    """Run-bound reader. Generic transport readers retain their exact0400 policy."""
    receipt = _context_receipt(snapshot.state, snapshot.record, plan)
    path = roots.runs / plan.run_name / OCI_STAGE1_TRANSPORT_FILENAME
    if receipt is None:
        return verify_stage1_transport_file(
            path, _transport(plan), expected_stage1_plan=OCIStage1Plan.from_domain_plan(plan)
        )
    if receipt.phase not in {"granted", "revoked"}:
        raise _invalid()
    run_fd = os.open(roots.runs / plan.run_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:

        def validate(fd):
            held, visible = os.fstat(run_fd), os.stat(roots.runs / plan.run_name, follow_symlinks=False)
            if (
                (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino)
                or not stat.S_ISDIR(visible.st_mode)
                or visible.st_uid != os.geteuid()
            ):
                raise _invalid()
            state = _read_pinned_json_object(run_fd, "state.json")
            if (
                Stage1AccessReceipt.from_dict(state.get(OCI_STAGE1_ACCESS_STATE_KEY)) != receipt
                or _plan(state, receipt.binding) != plan
            ):
                raise _invalid()
            _verify_file(
                fd,
                run_fd,
                receipt,
                plan,
                receipt.target.granted if receipt.phase == "granted" else receipt.target.baseline,
                LinuxFdACLBackend(),
            )
            current = _read_pinned_json_object(run_fd, "state.json")
            if (
                Stage1AccessReceipt.from_dict(current.get(OCI_STAGE1_ACCESS_STATE_KEY)) != receipt
                or _plan(current, receipt.binding) != plan
            ):
                raise _invalid()
            visible = os.stat(roots.runs / plan.run_name, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino):
                raise _invalid()

        return verify_stage1_transport_file(
            path,
            receipt.transport,
            expected_stage1_plan=OCIStage1Plan.from_domain_plan(plan),
            access_validator=validate,
        )
    finally:
        os.close(run_fd)


def verify_stage1_launch(roots, member, run_fd, fd, *, binding, metadata_only=False, expected_stamp=None):
    state = _read_pinned_json_object(run_fd, "state.json")
    if (
        OCI_STAGE1_ACCESS_STATE_KEY in state
        and state[OCI_STAGE1_ACCESS_STATE_KEY] is None
        or state.get(OCI_STAGE1_ACCESS_STATE_KEY) != member
    ):
        raise _invalid()
    if member is None:
        # Missing membership cannot bless an outstanding0440 grant.
        if state.get("oci_root_domain") is not None:
            info = os.stat(OCI_STAGE1_TRANSPORT_FILENAME, dir_fd=run_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != binding.owner_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o400
            ):
                raise _invalid()
        return
    receipt = Stage1AccessReceipt.from_dict(member)
    if receipt.phase != "granted" or receipt.binding != binding:
        raise _invalid()
    plan = _plan(state, binding)
    if metadata_only:
        _verify_payload(fd, receipt, plan)
        _metadata_tail(fd, run_fd, receipt, receipt.target.granted, expected_stamp=expected_stamp)
        stamp = expected_stamp
    else:
        stamp = _verify_file(fd, run_fd, receipt, plan, receipt.target.granted, LinuxFdACLBackend())
    current = _read_pinned_json_object(run_fd, "state.json")
    if current.get(OCI_STAGE1_ACCESS_STATE_KEY) != member or _plan(current, binding) != plan:
        raise _invalid()
    return stamp


def require_stage1_access_revoked(mutation, binding, *, metadata_only=False, expected_stamp=None):
    state = mutation.mutable_state()
    if OCI_STAGE1_ACCESS_STATE_KEY not in state:
        info = os.stat(OCI_STAGE1_TRANSPORT_FILENAME, dir_fd=mutation._run_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != binding.owner_uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o400
        ):
            raise _invalid()
        return
    receipt = Stage1AccessReceipt.from_dict(state[OCI_STAGE1_ACCESS_STATE_KEY])
    if receipt.phase != "revoked" or receipt.binding != binding:
        raise _invalid()
    with _pinned(mutation) as fd:
        if metadata_only:
            _verify_payload(fd, receipt, _plan(state, binding))
            _metadata_tail(fd, mutation._run_fd, receipt, receipt.target.baseline, expected_stamp=expected_stamp)
            stamp = expected_stamp
        else:
            stamp = _verify_file(
                fd, mutation._run_fd, receipt, _plan(state, binding), receipt.target.baseline, LinuxFdACLBackend()
            )
        _member(mutation, receipt)
        return stamp
