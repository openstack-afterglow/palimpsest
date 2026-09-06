"""Exact read-only access for the run-owned sealed lower exports."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .errors import StateError
from .oci_acl import LinuxFdACLBackend, parse_qemu_dac_baselabel, readonly_baseline_acl, readonly_grant_acl
from .oci_lower_exports import (
    OCI_LOWER_EXPORTS_STATE_KEY,
    LowerExportReceipt,
    _filenames,
    _invalid,
    _pinned_pair,
    _receipt,
    _tail,
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
from .oci_stage1_access import _immutable_stamp, _plan
from .state import _read_pinned_json_object, locked_existing_run

OCI_LOWER_ACCESS_STATE_KEY = "oci_lower_access"
_SCHEMA = "palimpsest.oci-lower-access.v1"


@dataclass(frozen=True, slots=True)
class LowerAccessReceipt:
    access_id: str
    phase: str
    binding: MonitorPreActivationBinding
    exports: LowerExportReceipt
    cleanup_digest: str | None = None

    def __post_init__(self):
        try:
            valid = type(self.access_id) is str and str(uuid.UUID(self.access_id)) == self.access_id
        except (ValueError, AttributeError):
            valid = False
        if (
            not valid
            or self.phase not in {"intent", "granted", "revoking", "revoked"}
            or type(self.binding) is not MonitorPreActivationBinding
            or type(self.exports) is not LowerExportReceipt
        ):
            raise _invalid()
        self.binding.__post_init__()
        self.exports.__post_init__()
        if self.exports.phase != "ready" or (self.exports.run_id, self.exports.run_name) != (
            self.binding.record.run_id,
            self.binding.record.name,
        ):
            raise _invalid()
        if self.phase in {"intent", "granted"}:
            if self.cleanup_digest is not None:
                raise _invalid()
        else:
            from .oci_lower_exports import _digest as valid_digest

            if not valid_digest(self.cleanup_digest):
                raise _invalid()

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            "access_id": self.access_id,
            "phase": self.phase,
            "binding": self.binding.to_dict(),
            "exports": self.exports.to_dict(),
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
        return cls(
            value["access_id"],
            value["phase"],
            MonitorPreActivationBinding.from_dict(dict(value["binding"])),
            LowerExportReceipt.from_dict(value["exports"]),
            value["cleanup_digest"],
        )


def _member(run_fd, exports, receipt, binding=None, *, record=None):
    state = _read_pinned_json_object(run_fd, "state.json")
    expected_record = binding.record if binding is not None else record
    if expected_record is None or _receipt(state, expected_record) != exports:
        raise _invalid()
    if receipt is None:
        if OCI_LOWER_ACCESS_STATE_KEY in state:
            raise _invalid()
    elif LowerAccessReceipt.from_dict(state.get(OCI_LOWER_ACCESS_STATE_KEY)) != receipt:
        raise _invalid()
    if binding is not None:
        plan = _plan(state, binding)
        if (
            plan.resource_plan_digest != exports.resource_plan_digest
            or plan.lower_lease_set_id != exports.lower_lease_set_id
            or plan.lower_graph_digest != exports.lower_graph_digest
        ):
            raise _invalid()
    return state


def _verify_pair(run_fd, exports, descriptors, modes, backend=None, stamps=None):
    _tail(run_fd, exports, descriptors, modes, stamps=stamps)
    before = {role: _immutable_stamp(os.fstat(fd)) for role, fd in descriptors.items()}
    if backend is not None:
        for role, fd in descriptors.items():
            expected = readonly_grant_acl(exports.qemu_uid) if modes[role] == 0o440 else readonly_baseline_acl()
            if backend.read_acl(fd) != expected:
                raise _invalid()
    _tail(run_fd, exports, descriptors, modes, stamps=before)
    return before


def _modes(descriptors, exports, receipt, backend):
    result = {}
    for role, fd in descriptors.items():
        acl = backend.read_acl(fd)
        if acl == readonly_baseline_acl():
            result[role] = 0o400
        elif acl == readonly_grant_acl(exports.qemu_uid):
            result[role] = 0o440
        else:
            raise _invalid()
    phase = "baseline" if receipt is None else receipt.phase
    values = tuple(result.values())
    count = len(values)
    grants = {tuple([0o440] * n + [0o400] * (count - n)) for n in range(count + 1)}
    allowed = {
        "baseline": {tuple([0o400] * count)},
        "intent": grants,
        "granted": {tuple([0o440] * count)},
        "revoking": grants,
        "revoked": {tuple([0o400] * count)},
    }
    if values not in allowed[phase]:
        raise _invalid()
    return result


def _write(mutation, receipt):
    data = mutation.mutable_state()
    data[OCI_LOWER_ACCESS_STATE_KEY] = receipt.to_dict()
    mutation.write_state(data["status"], data)


def grant_oci_lower_access(roots, binding, *, conn, qemu_uid=None, qemu_gid=None, acl_backend=None):
    try:
        backend = acl_backend or LinuxFdACLBackend()
        principal = parse_qemu_dac_baselabel(conn.getCapabilities())
        if any(
            v is not None and (type(v) is not int or v != p)
            for v, p in ((qemu_uid, principal[0]), (qemu_gid, principal[1]))
        ):
            raise _invalid()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            exports = _receipt(mutation.mutable_state(), mutation.record)
            if (exports.qemu_uid, exports.qemu_gid) != principal:
                raise _invalid()
            _source_io(mutation, binding)
            _grant_authority(mutation, binding, conn, principal)
            saved = mutation.mutable_state()
            expected = (
                LowerAccessReceipt.from_dict(saved[OCI_LOWER_ACCESS_STATE_KEY])
                if OCI_LOWER_ACCESS_STATE_KEY in saved
                else None
            )
            receipt = expected or LowerAccessReceipt(str(uuid.uuid4()), "intent", binding, exports)
            if receipt.binding != binding or receipt.exports != exports or receipt.phase not in {"intent", "granted"}:
                raise _invalid()
            with _pinned_pair(mutation._run_fd, exports) as descriptors:
                modes = _modes(descriptors, exports, expected, backend)

                def verify():
                    _grant_authority(mutation, binding, conn, principal)
                    _member(mutation._run_fd, exports, expected, binding)
                    stamps = _verify_pair(mutation._run_fd, exports, descriptors, modes, backend)
                    _grant_authority(mutation, binding, conn, principal)
                    _member(mutation._run_fd, exports, expected, binding)
                    _verify_pair(mutation._run_fd, exports, descriptors, modes, stamps=stamps)

                verify()
                if receipt.phase == "granted":
                    return receipt
                if expected is None:
                    _write(mutation, receipt)
                    expected = receipt
                    verify()
                for role, fd in descriptors.items():
                    if modes[role] != 0o440:
                        backend.write_acl(fd, readonly_grant_acl(exports.qemu_uid))
                        modes[role] = 0o440
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


def revoke_oci_lower_access(roots, binding, *, conn, acl_backend=None, liveness_probe=probe_process_liveness):
    authority = None
    try:
        backend = acl_backend or LinuxFdACLBackend()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            receipt = LowerAccessReceipt.from_dict(mutation.mutable_state().get(OCI_LOWER_ACCESS_STATE_KEY))
            exports = _receipt(mutation.mutable_state(), mutation.record)
            if (
                receipt.binding != binding
                or receipt.exports != exports
                or receipt.phase not in {"granted", "revoking", "revoked"}
            ):
                raise _invalid()
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
            if journal.phase != "terminal" or cleanup != exact or receipt.cleanup_digest not in {None, digest}:
                raise _invalid()
            expected = receipt
            with _pinned_pair(mutation._run_fd, exports) as descriptors:
                modes = _modes(descriptors, exports, receipt, backend)

                def verify():
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    stamps = _verify_pair(mutation._run_fd, exports, descriptors, modes, backend)
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _member(mutation._run_fd, exports, expected, binding)
                    if (
                        MonitorInactiveCleanupReceipt.from_dict(
                            mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                        )
                        != cleanup
                    ):
                        raise _invalid()
                    _verify_pair(mutation._run_fd, exports, descriptors, modes, stamps=stamps)

                verify()
                if receipt.phase == "revoked":
                    return receipt
                if receipt.phase == "granted":
                    receipt = replace(receipt, phase="revoking", cleanup_digest=digest)
                    _write(mutation, receipt)
                    expected = receipt
                    verify()
                for role in reversed(_filenames(exports)):
                    fd = descriptors[role]
                    if modes[role] != 0o400:
                        backend.write_acl(fd, readonly_baseline_acl())
                        modes[role] = 0o400
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


def verify_lower_context(roots, snapshot, exports):
    receipt = (
        LowerAccessReceipt.from_dict(snapshot.state[OCI_LOWER_ACCESS_STATE_KEY])
        if OCI_LOWER_ACCESS_STATE_KEY in snapshot.state
        else None
    )
    if receipt is not None and (receipt.exports != exports or receipt.phase not in {"granted", "revoked"}):
        raise _invalid()
    mode = 0o440 if receipt is not None and receipt.phase == "granted" else 0o400
    path = roots.runs / exports.run_name
    run_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        held = os.fstat(run_fd)
        with _pinned_pair(run_fd, exports) as descriptors:
            _member(run_fd, exports, receipt, None if receipt is None else receipt.binding, record=snapshot.record)
            _verify_pair(
                run_fd,
                exports,
                descriptors,
                dict.fromkeys(_filenames(exports), mode),
                LinuxFdACLBackend() if receipt is not None else None,
            )
            _member(run_fd, exports, receipt, None if receipt is None else receipt.binding, record=snapshot.record)
            visible = path.lstat()
            if (held.st_dev, held.st_ino, held.st_uid) != (visible.st_dev, visible.st_ino, visible.st_uid):
                raise _invalid()
            return {digest: path / name for digest, name in _filenames(exports).items()}
    finally:
        os.close(run_fd)


def verify_lower_launch(
    roots, exports_member, access_member, entries, *, binding, metadata_only=False, expected_stamp=None
):
    run_fd = entries["run"]["fd"]
    state = _read_pinned_json_object(run_fd, "state.json")
    if exports_member is None:
        if OCI_LOWER_EXPORTS_STATE_KEY in state or OCI_LOWER_ACCESS_STATE_KEY in state or access_member is not None:
            raise _invalid()
        if any(name.startswith("lower-") for name in os.listdir(run_fd)):
            raise _invalid()
        if any(key.startswith("lower_") for key in entries):
            raise _invalid()
        return
    exports = LowerExportReceipt.from_dict(exports_member)
    receipt = LowerAccessReceipt.from_dict(access_member) if access_member is not None else None
    if receipt is None or receipt.binding != binding or receipt.exports != exports or receipt.phase != "granted":
        raise _invalid()
    if exports.phase != "ready" or (exports.run_id, exports.run_name) != (binding.record.run_id, binding.record.name):
        raise _invalid()
    for index, name in enumerate(_filenames(exports).values()):
        if entries[f"lower_{index}"]["path"] != str(roots.runs / binding.record.name / name):
            raise _invalid()
    _member(run_fd, exports, receipt, binding)
    mode = 0o440 if receipt is not None else 0o400
    stamps = _verify_pair(
        run_fd,
        exports,
        {role: entries[f"lower_{index}"]["fd"] for index, role in enumerate(_filenames(exports))},
        dict.fromkeys(_filenames(exports), mode),
        None if metadata_only or receipt is None else LinuxFdACLBackend(),
        stamps=expected_stamp if metadata_only else None,
    )
    _member(run_fd, exports, receipt, binding)
    return stamps


def require_lower_access_revoked(mutation, binding, *, metadata_only=False, expected_stamp=None):
    state = mutation.mutable_state()
    if OCI_LOWER_EXPORTS_STATE_KEY not in state:
        from .oci_lower_exports import select_lower_exports

        select_lower_exports(mutation._roots, mutation.snapshot)
        return
    exports = _receipt(state, mutation.record)
    receipt = LowerAccessReceipt.from_dict(state.get(OCI_LOWER_ACCESS_STATE_KEY))
    if receipt.phase != "revoked" or receipt.binding != binding or receipt.exports != exports:
        raise _invalid()
    with _pinned_pair(mutation._run_fd, exports) as descriptors:
        stamps = _verify_pair(
            mutation._run_fd,
            exports,
            descriptors,
            dict.fromkeys(_filenames(exports), 0o400),
            None if metadata_only else LinuxFdACLBackend(),
            stamps=expected_stamp if metadata_only else None,
        )
        _member(mutation._run_fd, exports, receipt, binding)
        return stamps
