"""Generation-bound QEMU access to one private, mutable OCI root disk.

The run member is immutable. A volume-side fence owns the durable grant/revoke
phase, and a permanent enrollment marker prevents missing evidence downgrade.
Mutations take the run lock before the existing volume lock. Runtime readers
never acquire a run lock beneath a volume lock.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import StateError
from .oci_acl import LinuxFdACLBackend, baseline_acl, grant_acl, parse_qemu_dac_baselabel
from .oci_guest_filesystems import EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET, verify_ext4_superblock
from .oci_monitor import probe_process_liveness
from .oci_monitor_ipc import MonitorPreActivationBinding
from .oci_monitor_recovery import (
    MonitorInactiveCleanupReceipt,
    _inspect_domain,
    _RecoveryAuthority,
    _stale,
    _validate_ledger,
)
from .oci_root_kvm import OCIRootDomainPlan
from .oci_root_volume import (
    OCIRootVolumeRecord,
    _paths,
    _read_record,
    _RetentionVolumeLock,
    _root_authority,
    _strict_json_load,
    oci_root_volume_label,
)
from .oci_runtime_access import RuntimeAccessTarget, _digest, _grant_authority, _source_io, _validate_target
from .project_volumes import _default_runner, _verify_kvm_path
from .state import StatePaths, _read_pinned_json_object, locked_existing_run, pinned_owner_directory

OCI_ROOT_ACCESS_STATE_KEY = "oci_root_access"
_SCHEMA = "palimpsest.oci-root-access.v1"


def _invalid():
    return StateError("OCI root disk access authority is invalid or changed")


@dataclass(frozen=True, slots=True)
class RootAccessReceipt:
    access_id: str
    binding: MonitorPreActivationBinding
    volume: OCIRootVolumeRecord
    filesystem_uuid: str
    qemu_uid: int
    qemu_gid: int
    target: RuntimeAccessTarget

    def __post_init__(self):
        try:
            if (
                str(uuid.UUID(self.access_id)) != self.access_id
                or str(uuid.UUID(self.filesystem_uuid)) != self.filesystem_uuid
            ):
                raise _invalid()
            if (
                type(self.binding) is not MonitorPreActivationBinding
                or type(self.volume) is not OCIRootVolumeRecord
                or type(self.target) is not RuntimeAccessTarget
            ):
                raise _invalid()
            self.binding.__post_init__()
            self.volume.__post_init__()
            self.target.__post_init__()
            if (
                self.volume.status != "attached"
                or (self.volume.attached_run_id, self.volume.attached_run_name)
                != (self.binding.record.run_id, self.binding.record.name)
                or any(type(v) is not int or not 0 < v < 2**32 - 1 for v in (self.qemu_uid, self.qemu_gid))
                or self.qemu_uid == self.binding.owner_uid
                or self.target.directory
                or self.target.uid != self.binding.owner_uid
                or self.target.granted != grant_acl(self.target.baseline, self.qemu_uid)
            ):
                raise _invalid()
        except (TypeError, ValueError, AttributeError):
            raise _invalid() from None

    def to_dict(self):
        return {
            "schema": _SCHEMA,
            "access_id": self.access_id,
            "binding": self.binding.to_dict(),
            "volume": self.volume.to_dict(),
            "filesystem_uuid": self.filesystem_uuid,
            "qemu_uid": self.qemu_uid,
            "qemu_gid": self.qemu_gid,
            "target": self.target.to_dict(),
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
                MonitorPreActivationBinding.from_dict(dict(value["binding"])),
                OCIRootVolumeRecord.from_dict(dict(value["volume"])),
                value["filesystem_uuid"],
                value["qemu_uid"],
                value["qemu_gid"],
                RuntimeAccessTarget.from_dict(value["target"]),
            )
        except (KeyError, TypeError, ValueError):
            raise _invalid() from None


def _names(volume_id):
    if str(uuid.UUID(volume_id)) != volume_id:
        raise _invalid()
    prefix = "oci-root-access-" + volume_id.replace("-", "")
    return prefix + ".enrolled.json", prefix + ".json"


def _optional(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _strict_json_load(fd, name)


def _write(fd, name, value, expected):
    if _optional(fd, name) != expected:
        raise _invalid()
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if len(payload) > 64 * 1024:
        raise _invalid()
    temporary = ".root-access-" + uuid.uuid4().hex + ".tmp"
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    try:
        os.fchmod(out, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(out, payload[offset:])
            if written <= 0:
                raise _invalid()
            offset += written
        os.fsync(out)
        if _optional(fd, name) != expected:
            raise _invalid()
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
        if _optional(fd, name) != value:
            raise _invalid()
    finally:
        os.close(out)
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _evidence(roots, volume_id):
    marker_name, fence_name = _names(volume_id)
    with pinned_owner_directory(roots.locks) as fd:
        marker, fence = _optional(fd, marker_name), _optional(fd, fence_name)
    original = RootAccessReceipt.from_dict(marker) if marker is not None else None
    if original is not None and original.volume.volume_id != volume_id:
        raise _invalid()
    if fence is not None:
        if (
            original is None
            or type(fence) is not dict
            or set(fence) != {"receipt", "phase", "cleanup_digest"}
            or fence["phase"] not in {"intent", "granted", "revoking", "revoked"}
        ):
            raise _invalid()
        current = RootAccessReceipt.from_dict(fence["receipt"])
        if (
            current.volume.volume_id,
            current.volume.size_bytes,
            current.volume.lower_graph_digest,
            current.filesystem_uuid,
            current.target.device,
            current.target.inode,
        ) != (
            original.volume.volume_id,
            original.volume.size_bytes,
            original.volume.lower_graph_digest,
            original.filesystem_uuid,
            original.target.device,
            original.target.inode,
        ) or current.volume.generation < original.volume.generation:
            raise _invalid()
        digest = fence["cleanup_digest"]
        if fence["phase"] in {"intent", "granted"}:
            if digest is not None:
                raise _invalid()
        elif (
            type(digest) is not str
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in digest[7:])
        ):
            raise _invalid()
    return marker, fence


def _disk(roots, receipt, fd, acl, backend, *, path=None):
    path = path or _paths(roots, receipt.volume.volume_id)[0]
    for info in (os.fstat(fd), os.stat(path, follow_symlinks=False)):
        _validate_target(info, receipt.target, acl)
        if info.st_size != receipt.volume.size_bytes:
            raise _invalid()
    if backend.read_acl(fd) != acl:
        raise _invalid()
    verify_ext4_superblock(
        os.pread(fd, EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET),
        device_size=receipt.volume.size_bytes,
        volume_id=receipt.volume.volume_id,
        filesystem_uuid=receipt.filesystem_uuid,
    )
    # External ACL implementations may call back into the filesystem.
    for info in (os.fstat(fd), os.stat(path, follow_symlinks=False)):
        _validate_target(info, receipt.target, acl)
        if info.st_size != receipt.volume.size_bytes:
            raise _invalid()


def verify_volume_root_access(roots, record, fd, *, receipt=None, require_revoked=False, acl_backend=None):
    """Read-only, explicit managed ACL policy, also used under volume locks."""
    marker, fence = _evidence(roots, record.volume_id)
    if marker is None:
        if receipt is not None:
            raise _invalid()
        return False
    if fence is None:
        raise _invalid()
    member = RootAccessReceipt.from_dict(fence["receipt"])
    backend = acl_backend or LinuxFdACLBackend()
    if fence["phase"] == "revoked":
        if (
            record.volume_id != member.volume.volume_id
            or record.size_bytes != member.volume.size_bytes
            or record.lower_graph_digest != member.volume.lower_graph_digest
            or record.generation < member.volume.generation
        ):
            raise _invalid()
        if record.generation == member.volume.generation and record != member.volume:
            raise _invalid()
        if receipt is not None and RootAccessReceipt.from_dict(receipt) != member:
            raise _invalid()
        _disk(roots, member, fd, member.target.baseline, backend)
    else:
        if (
            require_revoked
            or fence["phase"] != "granted"
            or record != member.volume
            or receipt is None
            or RootAccessReceipt.from_dict(receipt) != member
        ):
            raise _invalid()
        _disk(roots, member, fd, member.target.granted, backend)
    if _evidence(roots, record.volume_id) != (marker, fence):
        raise _invalid()
    return True


def require_root_access_revoked(roots, record, *, allow_deleting=False):
    """Fence lifecycle mutations even if a caller has removed the run member."""
    marker, fence = _evidence(roots, record.volume_id)
    if marker is None:
        # Legacy lifecycle paths still require an owner-only root. Losing both
        # access files must not turn an outstanding 0660 grant into retention.
        path = _paths(roots, record.volume_id)[0]
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            if record.status in {"creating", "deleting"}:
                return
            raise _invalid() from None
        expected_links = 1
        if record.status == "creating" and info.st_nlink == 2:
            from .oci_root_volume import _creation_temporary

            temporary = os.stat(_creation_temporary(roots, record.volume_id), follow_symlinks=False)
            if (
                not stat.S_ISREG(temporary.st_mode)
                or (temporary.st_dev, temporary.st_ino, temporary.st_uid, temporary.st_nlink, temporary.st_size)
                != (info.st_dev, info.st_ino, os.geteuid(), 2, record.size_bytes)
                or stat.S_IMODE(temporary.st_mode) != 0o600
            ):
                raise _invalid()
            expected_links = 2
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != expected_links
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != record.size_bytes
        ):
            raise _invalid()
        return
    if fence is None or fence["phase"] != "revoked":
        raise _invalid()
    path = _paths(roots, record.volume_id)[0]
    if allow_deleting and record.status == "deleting":
        from .oci_root_volume import _deletion_quarantine

        member = RootAccessReceipt.from_dict(fence["receipt"])
        if record.generation <= member.volume.generation or (
            record.volume_id,
            record.size_bytes,
            record.lower_graph_digest,
        ) != (member.volume.volume_id, member.volume.size_bytes, member.volume.lower_graph_digest):
            raise _invalid()
        quarantine = _deletion_quarantine(roots, record.volume_id)
        if not path.exists() and not path.is_symlink():
            if not quarantine.exists() and not quarantine.is_symlink():
                return
            fd = os.open(quarantine, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                _disk(roots, member, fd, member.target.baseline, LinuxFdACLBackend(), path=quarantine)
                if _evidence(roots, record.volume_id) != (marker, fence):
                    raise _invalid()
                return
            finally:
                os.close(fd)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        verify_volume_root_access(roots, record, fd, require_revoked=True)
    finally:
        os.close(fd)


@contextmanager
def _locked_disk(roots, binding, mutation):
    value = mutation.snapshot.state.get("oci_root_domain")
    if not isinstance(value, Mapping) or set(value) != {"digest", "plan"}:
        raise _invalid()
    plan = OCIRootDomainPlan.from_dict(value["plan"])
    if plan.digest != binding.plan_digest or value["digest"] != plan.digest:
        raise _invalid()
    path, record_path, lock_path = _paths(roots, plan.root_volume["volume_id"])
    with _RetentionVolumeLock(roots, lock_path) as lock, _root_authority(roots) as directory:
        record = _read_record(directory, record_path)
        if record.status != "attached" or (
            record.attached_run_id,
            record.attached_run_name,
            record.generation,
            record.lower_graph_digest,
            record.size_bytes,
        ) != (
            binding.record.run_id,
            binding.record.name,
            plan.root_volume["generation"],
            plan.lower_graph_digest,
            plan.root_volume["size_bytes"],
        ):
            raise _invalid()
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        try:

            def verify():
                lock.verify()
                if (
                    _read_record(directory, record_path) != record
                    or mutation.snapshot.state.get("oci_root_domain") != value
                ):
                    raise _invalid()

            verify()
            yield record, plan.root_volume["filesystem_uuid"], fd, verify, lock.directory_fd
        finally:
            os.close(fd)


def grant_oci_root_access(
    roots, binding, *, conn, qemu_uid=None, qemu_gid=None, acl_backend=None, runner=_default_runner
):
    """Enroll one inactive committed root generation and grant only its raw FD."""
    try:
        if type(roots) is not StatePaths or type(binding) is not MonitorPreActivationBinding:
            raise _invalid()
        backend = acl_backend or LinuxFdACLBackend()
        principal = parse_qemu_dac_baselabel(conn.getCapabilities())
        if (
            principal[0] in {0, os.geteuid()}
            or principal[1] == 0
            or any(
                s is not None and (type(s) is not int or s != p)
                for s, p in ((qemu_uid, principal[0]), (qemu_gid, principal[1]))
            )
        ):
            raise _invalid()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            _source_io(mutation, binding)
            _grant_authority(mutation, binding, conn, principal)
            with _locked_disk(roots, binding, mutation) as (record, fsuuid, fd, verify_record, locks_fd):
                marker, fence = _evidence(roots, record.volume_id)
                saved = mutation.mutable_state().get(OCI_ROOT_ACCESS_STATE_KEY)
                if OCI_ROOT_ACCESS_STATE_KEY in mutation.snapshot.state and saved is None:
                    raise _invalid()
                if fence is not None and fence["phase"] in {"intent", "granted"}:
                    receipt = RootAccessReceipt.from_dict(fence["receipt"])
                elif marker is not None and fence is None:
                    receipt = RootAccessReceipt.from_dict(marker)
                else:
                    if saved is not None:
                        raise _invalid()
                    if fence is not None:
                        old = RootAccessReceipt.from_dict(fence["receipt"])
                        verify_volume_root_access(roots, record, fd, require_revoked=True, acl_backend=backend)
                        if record.generation <= old.volume.generation:
                            raise _invalid()
                    info = os.fstat(fd)
                    acl = backend.read_acl(fd)
                    if acl != baseline_acl(directory=False):
                        raise _invalid()
                    target = RuntimeAccessTarget(
                        info.st_dev,
                        info.st_ino,
                        info.st_uid,
                        info.st_gid,
                        info.st_nlink,
                        False,
                        acl,
                        grant_acl(acl, principal[0]),
                    )
                    receipt = RootAccessReceipt(str(uuid.uuid4()), binding, record, fsuuid, *principal, target)
                if (
                    receipt.binding != binding
                    or receipt.volume != record
                    or receipt.filesystem_uuid != fsuuid
                    or (receipt.qemu_uid, receipt.qemu_gid) != principal
                    or saved is not None
                    and saved != receipt.to_dict()
                ):
                    raise _invalid()
                expected_acl = backend.read_acl(fd)
                choices = (
                    (receipt.target.granted,)
                    if fence is not None and fence["phase"] == "granted"
                    else (receipt.target.baseline, receipt.target.granted)
                    if fence is not None and fence["phase"] == "intent"
                    else (receipt.target.baseline,)
                )
                if expected_acl not in choices:
                    raise _invalid()

                def verify():
                    verify_record()
                    if (
                        _evidence(roots, record.volume_id) != (marker, fence)
                        or mutation.mutable_state().get(OCI_ROOT_ACCESS_STATE_KEY) != saved
                    ):
                        raise _invalid()
                    _source_io(mutation, binding)
                    _grant_authority(mutation, binding, conn, principal)
                    verify_record()
                    _disk(roots, receipt, fd, expected_acl, backend)
                    verify_record()
                    if (
                        _evidence(roots, record.volume_id) != (marker, fence)
                        or mutation.mutable_state().get(OCI_ROOT_ACCESS_STATE_KEY) != saved
                    ):
                        raise _invalid()

                verify()
                if fence is not None and fence["phase"] == "granted":
                    if saved != receipt.to_dict():
                        raise _invalid()
                    return receipt
                _verify_kvm_path(
                    _paths(roots, record.volume_id)[0],
                    record.size_bytes,
                    oci_root_volume_label(record.volume_id),
                    runner,
                    access_validator=verify,
                )
                verify()
                marker_name, fence_name = _names(record.volume_id)
                if marker is None:
                    _write(locks_fd, marker_name, receipt.to_dict(), None)
                    marker = receipt.to_dict()
                    verify()
                if fence is None or fence["phase"] == "revoked":
                    new = {"receipt": receipt.to_dict(), "phase": "intent", "cleanup_digest": None}
                    _write(locks_fd, fence_name, new, fence)
                    fence = new
                    verify()
                if saved is None:
                    data = mutation.mutable_state()
                    data[OCI_ROOT_ACCESS_STATE_KEY] = receipt.to_dict()
                    mutation.write_state(data["status"], data)
                    saved = receipt.to_dict()
                    verify()
                if expected_acl != receipt.target.granted:
                    backend.write_acl(fd, receipt.target.granted)
                    expected_acl = receipt.target.granted
                os.fsync(fd)
                verify()
                if fence["phase"] != "granted":
                    new = {**fence, "phase": "granted"}
                    _write(locks_fd, fence_name, new, fence)
                    fence = new
                    verify()
                return receipt
    except StateError:
        raise
    except Exception:
        raise _invalid() from None


def revoke_oci_root_access(roots, binding, *, conn, acl_backend=None, liveness_probe=probe_process_liveness):
    """Restore exact owner ACL after original stale terminal writer cleanup."""
    authority = None
    try:
        backend = acl_backend or LinuxFdACLBackend()
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            receipt = RootAccessReceipt.from_dict(mutation.mutable_state().get(OCI_ROOT_ACCESS_STATE_KEY))
            if receipt.binding != binding:
                raise _invalid()
            authority = _RecoveryAuthority(mutation, binding)
            journal = authority.snapshot
            cleanup = MonitorInactiveCleanupReceipt.from_dict(
                mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
            )
            expected = MonitorInactiveCleanupReceipt(
                binding,
                cleanup.cleanup_id,
                _digest_bytes(authority.content),
                journal.identity.generation,
                journal.revision,
                authority.journal_metadata.st_dev,
                authority.journal_metadata.st_ino,
                journal.writer,
                "completed",
            )
            if cleanup != expected or journal.phase != "terminal":
                raise _invalid()
            with _locked_disk(roots, binding, mutation) as (record, fsuuid, fd, verify_record, locks_fd):
                marker, fence = _evidence(roots, record.volume_id)
                digest = _digest(cleanup.to_dict())
                if (
                    fence is None
                    or fence["receipt"] != receipt.to_dict()
                    or fence["phase"] not in {"granted", "revoking", "revoked"}
                    or fence["cleanup_digest"] not in (None, digest)
                    or receipt.volume != record
                    or receipt.filesystem_uuid != fsuuid
                ):
                    raise _invalid()
                expected_acl = backend.read_acl(fd)
                choices = (
                    (receipt.target.baseline, receipt.target.granted)
                    if fence["phase"] == "revoking"
                    else (receipt.target.baseline,)
                    if fence["phase"] == "revoked"
                    else (receipt.target.granted,)
                )
                if expected_acl not in choices:
                    raise _invalid()

                def verify():
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    _stale(journal, liveness_probe)
                    if _inspect_domain(conn, binding) is not None or conn.getURI() != binding.libvirt_uri:
                        raise _invalid()
                    authority.validate()
                    _stale(journal, liveness_probe)
                    verify_record()
                    if (
                        mutation.mutable_state().get(OCI_ROOT_ACCESS_STATE_KEY) != receipt.to_dict()
                        or MonitorInactiveCleanupReceipt.from_dict(
                            mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                        )
                        != cleanup
                        or _evidence(roots, record.volume_id) != (marker, fence)
                    ):
                        raise _invalid()
                    _disk(roots, receipt, fd, expected_acl, backend)
                    authority.validate()
                    _validate_ledger(mutation, binding, journal)
                    verify_record()
                    if (
                        _evidence(roots, record.volume_id) != (marker, fence)
                        or mutation.mutable_state().get(OCI_ROOT_ACCESS_STATE_KEY) != receipt.to_dict()
                        or MonitorInactiveCleanupReceipt.from_dict(
                            mutation.snapshot.state.get("oci_monitor_inactive_cleanup")
                        )
                        != cleanup
                    ):
                        raise _invalid()

                verify()
                if fence["phase"] == "revoked":
                    return receipt
                _, fence_name = _names(record.volume_id)
                if fence["phase"] == "granted":
                    new = {**fence, "phase": "revoking", "cleanup_digest": digest}
                    _write(locks_fd, fence_name, new, fence)
                    fence = new
                    verify()
                if expected_acl != receipt.target.baseline:
                    backend.write_acl(fd, receipt.target.baseline)
                    expected_acl = receipt.target.baseline
                os.fsync(fd)
                verify()
                new = {**fence, "phase": "revoked"}
                _write(locks_fd, fence_name, new, fence)
                fence = new
                verify()
                return receipt
    except StateError:
        raise
    except Exception:
        raise _invalid() from None
    finally:
        if authority is not None:
            authority.close()


def _digest_bytes(content):
    import hashlib

    return "sha256:" + hashlib.sha256(content).hexdigest()


def verify_root_launch_access(roots, receipt, fd, *, binding):
    """Validate a managed inherited disk FD without taking a mutation lock."""
    member = RootAccessReceipt.from_dict(receipt)
    if member.binding != binding:
        raise _invalid()
    _, record_path, _ = _paths(roots, member.volume.volume_id)
    with _root_authority(roots) as directory:
        record = _read_record(directory, record_path)
        if record != member.volume:
            raise _invalid()
        marker, fence = _evidence(roots, record.volume_id)
        if fence is None or fence["phase"] != "granted":
            raise _invalid()
        verify_volume_root_access(roots, record, fd, receipt=receipt)
        if _read_record(directory, record_path) != record or _evidence(roots, record.volume_id) != (marker, fence):
            raise _invalid()


def verify_root_launch_member(roots, receipt, run_fd, *, binding):
    """Reject a null/omitted managed disk member using the inherited run FD."""
    state = _read_pinned_json_object(run_fd, "state.json")
    if OCI_ROOT_ACCESS_STATE_KEY in state and state[OCI_ROOT_ACCESS_STATE_KEY] is None:
        raise _invalid()
    if state.get(OCI_ROOT_ACCESS_STATE_KEY) != receipt:
        raise _invalid()
    value = state.get("oci_root_domain")
    if value is None and receipt is None and "oci_root" not in state:
        return
    if not isinstance(value, Mapping) or set(value) != {"digest", "plan"}:
        raise _invalid()
    plan = OCIRootDomainPlan.from_dict(value["plan"])
    if plan.digest != binding.plan_digest or value["digest"] != binding.plan_digest:
        raise _invalid()
    marker, fence = _evidence(roots, plan.root_volume["volume_id"])
    if receipt is None and marker is not None and (fence is None or fence["phase"] != "revoked"):
        raise _invalid()


def verify_root_launch_tail(roots, receipt, run_fd, disk_fd, *, binding):
    """Recheck immutable evidence after all external ACL readers, without callbacks."""
    verify_root_launch_member(roots, receipt, run_fd, binding=binding)
    if receipt is None:
        return
    member = RootAccessReceipt.from_dict(receipt)
    if member.binding != binding:
        raise _invalid()
    path, record_path, _ = _paths(roots, member.volume.volume_id)
    with _root_authority(roots) as directory:
        if _read_record(directory, record_path) != member.volume:
            raise _invalid()
        _, fence = _evidence(roots, member.volume.volume_id)
        if fence != {"receipt": receipt, "phase": "granted", "cleanup_digest": None}:
            raise _invalid()
        for info in (os.fstat(disk_fd), os.stat(path, follow_symlinks=False)):
            _validate_target(info, member.target, member.target.granted)
            if info.st_size != member.volume.size_bytes:
                raise _invalid()
        verify_ext4_superblock(
            os.pread(disk_fd, EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET),
            device_size=member.volume.size_bytes,
            volume_id=member.volume.volume_id,
            filesystem_uuid=member.filesystem_uuid,
        )


def validate_root_deletion_path(roots, record, path):
    """Keep the revoked inode bound across external qemu-info callbacks."""
    from .oci_root_volume import _deletion_quarantine

    if path not in (_paths(roots, record.volume_id)[0], _deletion_quarantine(roots, record.volume_id)):
        raise _invalid()
    marker, fence = _evidence(roots, record.volume_id)
    if marker is None:
        return
    if fence is None or fence["phase"] != "revoked":
        raise _invalid()
    member = RootAccessReceipt.from_dict(fence["receipt"])
    if (
        record.status != "deleting"
        or record.generation <= member.volume.generation
        or (record.volume_id, record.size_bytes, record.lower_graph_digest)
        != (member.volume.volume_id, member.volume.size_bytes, member.volume.lower_graph_digest)
    ):
        raise _invalid()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _disk(roots, member, fd, member.target.baseline, LinuxFdACLBackend(), path=path)
        if _evidence(roots, record.volume_id) != (marker, fence):
            raise _invalid()
    finally:
        os.close(fd)
