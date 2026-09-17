"""Normal terminal OCI removal; uncertain ownership always preserves evidence.

This is not crash takeover: a stale writer with a remaining control socket,
prelaunch failures, or a running guest require separate recovery, not rm.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace

from . import oci_monitor_ipc as ipc
from .errors import StateError
from .oci_boot_access import revoke_oci_boot_access
from .oci_lower_access import revoke_oci_lower_access
from .oci_monitor_client import MonitorClient, _Deadline
from .oci_monitor_recovery import _inspect_domain, _RecoveryAuthority, reconcile_inactive_monitor_domain
from .oci_root_access import revoke_oci_root_access
from .oci_root_prepare import (
    _transaction_from_snapshot,
    reconcile_oci_root_preparation,
    release_oci_root_transaction,
)
from .oci_runtime_access import revoke_oci_runtime_access
from .oci_shared_traversal import leave_oci_shared_traversal
from .oci_stage1_access import revoke_oci_stage1_access
from .project_volumes import _default_runner
from .state import StatePaths, _read_exact_private_file, locked_existing_run, read_run_ledger_snapshot

_STATE_KEY = "oci_run_removal"
_SCHEMA = "palimpsest.oci-run-removal.v1"


class OCIRunRemovalError(StateError):
    """Normal removal could not be proven; the remaining run is preserved."""


@dataclass(frozen=True, slots=True)
class OCIRunRemovalResult:
    name: str
    run_id: str
    root_volume_id: str
    retention_policy: str


@contextmanager
def _errors():
    try:
        yield
    except OCIRunRemovalError:
        raise
    except (StateError, ipc.MonitorIPCError, OSError, ValueError, TypeError, KeyError):
        raise OCIRunRemovalError("OCI run removal could not be verified; preserve the run evidence") from None


def load_oci_run_binding(roots: StatePaths, name: str) -> ipc.MonitorPreActivationBinding:
    """Load a bounded private journal and bind it to the exact existing run."""
    with _errors(), locked_existing_run(roots, name) as mutation:
        return _read_run_journal(mutation).identity.binding


def _read_run_journal(mutation, binding=None):
    """Observe a live writer's atomic journal without taking its lifetime flock."""
    descriptor = os.open(
        "monitor-private",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=mutation._run_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if binding is None:
            content = _read_exact_private_file(descriptor, ipc._JOURNAL_NAME, max_bytes=ipc._MAX_JOURNAL_BYTES)
            binding = ipc.MonitorPreActivationBinding.from_dict(json.loads(content)["binding"])
        if binding.record != mutation.record or binding.owner_uid != os.geteuid():
            raise OCIRunRemovalError("OCI run monitor binding changed")
        loaded = ipc._read_preactivation_journal(descriptor, binding)
        assert loaded is not None
        visible = os.stat("monitor-private", dir_fd=mutation._run_fd, follow_symlinks=False)
        for info in (opened, visible):
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OCIRunRemovalError("OCI run monitor directory changed")
        mutation.verify_binding()
        return loaded[0]
    finally:
        os.close(descriptor)


@contextmanager
def _authority(mutation, binding):
    authority = _RecoveryAuthority(mutation, binding)
    try:
        yield authority
        authority.validate()
    finally:
        authority.close()


def _liveness(writer, probe):
    try:
        result = probe(writer)
    except Exception:
        result = ipc.ProcessLiveness.UNKNOWN
    if result not in {ipc.ProcessLiveness.LIVE, ipc.ProcessLiveness.STALE}:
        raise OCIRunRemovalError("OCI run monitor liveness is unknown")
    return result


def _socket_absent(directory_fd, endpoint):
    if endpoint.socket_name is None:
        raise OCIRunRemovalError("OCI run terminal socket identity is missing")
    try:
        os.stat(endpoint.socket_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _retire(roots, binding, timeout, probe):
    deadline = _Deadline(timeout)
    with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
        snapshot = _read_run_journal(mutation, binding)
        if snapshot.phase != "terminal":
            raise OCIRunRemovalError("OCI run is not terminal; stop it before removal")
        endpoint = snapshot.endpoint
    # No run or journal lock may be held across SHUTDOWN: its worker needs both.
    with MonitorClient(roots, binding, endpoint, timeout=deadline.remaining()) as client:
        snapshot, _ = client._read(deadline)
        absent = _socket_absent(client._fd, endpoint)
        live = _liveness(endpoint.writer, probe)
        if not absent:
            if live is not ipc.ProcessLiveness.LIVE:
                raise OCIRunRemovalError("OCI stale monitor socket requires recovery; preserve the run evidence")
            retired = ipc.shutdown_monitor_exec(client._fd, endpoint, timeout=deadline.remaining(minimum=0.1))
            if retired != snapshot:
                raise OCIRunRemovalError("OCI terminal journal changed during shutdown")
        while True:
            current, _ = client._read(deadline)
            if current != snapshot or not _socket_absent(client._fd, endpoint):
                raise OCIRunRemovalError("OCI terminal shutdown evidence changed")
            if _liveness(endpoint.writer, probe) is ipc.ProcessLiveness.STALE:
                return
            time.sleep(min(0.01, deadline.remaining()))


def _milestone(binding, authority, transaction):
    return {
        "schema": _SCHEMA,
        "phase": "resources-releasable",
        "binding": binding.to_dict(),
        "journal_digest": "sha256:" + hashlib.sha256(authority.content).hexdigest(),
        "journal_device": authority.journal_metadata.st_dev,
        "journal_inode": authority.journal_metadata.st_ino,
        "transaction": replace(transaction, phase="resources-ready").to_dict(),
    }


def _released_authority(mutation, binding, transaction, conn, probe, *, create=False):
    with _authority(mutation, binding) as authority:
        if authority.snapshot.phase != "terminal":
            raise OCIRunRemovalError("OCI removal requires the original terminal journal")
        if _liveness(authority.snapshot.writer, probe) is not ipc.ProcessLiveness.STALE:
            raise OCIRunRemovalError("OCI removal monitor is not stale")
        if not _socket_absent(authority.directory_fd, authority.snapshot.endpoint):
            raise OCIRunRemovalError("OCI removal monitor socket remains")
        expected = _milestone(binding, authority, transaction)
        data = mutation.mutable_state()
        if (not create or _STATE_KEY in data) and data.get(_STATE_KEY) != expected:
            raise OCIRunRemovalError("OCI removal release milestone changed or is missing")
        if _inspect_domain(conn, binding) is not None:
            raise OCIRunRemovalError("OCI removal domain reappeared")
        # STALE for an exact boot-ID/PID/start-ticks tuple cannot become LIVE.
        # Keep domain inspection last among external authority callbacks.
        authority.validate()
        if not _socket_absent(authority.directory_fd, authority.snapshot.endpoint):
            raise OCIRunRemovalError("OCI removal monitor socket reappeared")
        return expected


def remove_oci_run(
    roots: StatePaths,
    binding: ipc.MonitorPreActivationBinding,
    store,
    *,
    conn,
    timeout: float = 5.0,
    acl_backend=None,
    runner=_default_runner,
    liveness_probe=ipc.probe_process_liveness,
) -> OCIRunRemovalResult:
    """Remove only a normally completed run, retaining its root by saved policy.

    Each existing cleanup/revocation receipt is resumable. The final durable
    milestone permits retrying release-required/released without needing lowers
    that a previous successful release has already removed.
    """
    with _errors():
        _Deadline(timeout)
        if type(roots) is not StatePaths or type(binding) is not ipc.MonitorPreActivationBinding:
            raise OCIRunRemovalError("OCI removal identity is invalid")
        binding.__post_init__()
        snapshot = read_run_ledger_snapshot(roots, binding.record.name)
        if snapshot.record != binding.record or binding.owner_uid != os.geteuid():
            raise OCIRunRemovalError("OCI removal run owner changed")
        transaction = _transaction_from_snapshot(snapshot)
        if transaction.phase not in {"resources-ready", "release-required", "released"}:
            raise OCIRunRemovalError("OCI removal preparation requires separate recovery")
        if transaction.phase == "resources-ready":
            _retire(roots, binding, timeout, liveness_probe)
            reconcile_inactive_monitor_domain(roots, binding, conn=conn, liveness_probe=liveness_probe)
            for revoke in (
                revoke_oci_lower_access,
                revoke_oci_boot_access,
                revoke_oci_stage1_access,
                revoke_oci_root_access,
                revoke_oci_runtime_access,
                leave_oci_shared_traversal,
            ):
                revoke(roots, binding, conn=conn, acl_backend=acl_backend, liveness_probe=liveness_probe)
            with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
                if _transaction_from_snapshot(mutation.snapshot) != transaction:
                    raise OCIRunRemovalError("OCI removal preparation changed")
                receipt = _released_authority(mutation, binding, transaction, conn, liveness_probe, create=True)
                data = mutation.mutable_state()
                data[_STATE_KEY] = receipt
                mutation.write_state(data["status"], data)
            release_oci_root_transaction(roots, transaction, store, runner=runner)
        else:
            with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
                _released_authority(mutation, binding, transaction, conn, liveness_probe)
            reconcile_oci_root_preparation(roots, binding.record.name, store, runner=runner)
        with locked_existing_run(roots, binding.record.name, expected=binding.record) as mutation:
            released = _transaction_from_snapshot(mutation.snapshot)
            if released != replace(transaction, phase="released"):
                raise OCIRunRemovalError("OCI removal resource release is incomplete")
            _released_authority(mutation, binding, released, conn, liveness_probe)
            mutation.delete_run_tree()
        return OCIRunRemovalResult(
            binding.record.name, binding.record.run_id, transaction.volume_id, transaction.retention_policy
        )
