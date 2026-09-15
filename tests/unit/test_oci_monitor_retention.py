"""Retain a proven inactive root without releasing its lower-layer leases."""

from __future__ import annotations

import fcntl
import json
import os
import uuid

import pytest
import test_oci_monitor_recovery as recovery_tests
import test_oci_store as store_tests

from palimpsest_local import oci_monitor_retention as retention
from palimpsest_local import oci_root_volume as volumes
from palimpsest_local import state as state_module
from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor import ProcessLiveness
from palimpsest_local.oci_root_prepare import OCIRootPreparationTransaction
from palimpsest_local.oci_store import ArtifactLeaseOwner
from palimpsest_local.state import locked_existing_run


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    original = store_tests.prepare_oci_root_run

    def prepare(*args, **kwargs):
        kwargs["retention_policy"] = "retain"
        return original(*args, **kwargs)

    monkeypatch.setattr(store_tests, "prepare_oci_root_run", prepare)
    value = recovery_tests.case.__wrapped__(tmp_path, monkeypatch, request)
    value.runner = store_tests._RootVolumeTools()
    value.transaction = OCIRootPreparationTransaction.from_dict(json.loads(value.state.read_bytes())["oci_root"])
    value.volume = volumes.load_oci_root_volume(value.roots, value.transaction.volume_id, runner=value.runner)
    value.volume_record = volumes._paths(value.roots, value.volume.record.volume_id)[1]
    value.cleanup = recovery_tests._run(value)
    return value


def _run(case, **kwargs):
    return retention.retain_inactive_monitor_root(
        case.roots,
        case.binding,
        case.store,
        conn=case.conn,
        runner=case.runner,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def _leases(case):
    txn = case.transaction
    return case.store.load_lease_set(txn.lower_lease_set_id, txn.owner, plan_digest=txn.boot_plan_digest)


def test_retention_preserves_root_bytes_lower_leases_and_original_status(case):
    before = json.loads(case.state.read_bytes())
    journal = case.journal.read_bytes()
    root_metadata = case.volume.path.stat()
    leases = _leases(case)
    receipt = _run(case)
    assert receipt.phase == "completed"
    assert receipt.retained_generation == case.volume.record.generation + 1
    current = volumes.load_oci_root_volume(case.roots, case.volume.record.volume_id, runner=case.runner)
    assert current.record.status == "retained"
    assert current.record.attached_run_id is current.record.attached_run_name is None
    assert current.record.generation == receipt.retained_generation
    assert current.filesystem_uuid == case.volume.filesystem_uuid
    assert case.volume.path.stat() == root_metadata
    assert _leases(case) == leases
    assert case.journal.read_bytes() == journal
    after = json.loads(case.state.read_bytes())
    assert after["oci_monitor_root_retention"] == receipt.to_dict()
    for key, value in before.items():
        if key != "lifecycle_revision":
            assert after[key] == value
    assert _run(case) == receipt
    assert retention.MonitorRootRetentionReceipt.from_dict(receipt.to_dict()) == receipt


@pytest.mark.parametrize("liveness", [ProcessLiveness.LIVE, ProcessLiveness.UNKNOWN, None])
def test_retention_requires_stale_writer_before_mutation(case, liveness):
    state, volume = case.state.read_bytes(), case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case, liveness_probe=lambda _: liveness)
    assert case.state.read_bytes() == state and case.volume_record.read_bytes() == volume


@pytest.mark.parametrize("change", ["absent", "intent", "journal-inode", "binding", "reappeared"])
def test_retention_requires_exact_completed_cleanup(case, change):
    if change == "journal-inode":
        content = case.journal.read_bytes()
        case.journal.rename(case.journal.with_suffix(".saved"))
        case.journal.write_bytes(content)
        case.journal.chmod(0o600)
    elif change == "reappeared":
        case.conn.conn.domains[case.binding.record.name] = case.domain.domain
    else:
        with locked_existing_run(case.roots, case.binding.record.name) as mutation:
            data = mutation.mutable_state()
            if change == "absent":
                data.pop("oci_monitor_inactive_cleanup")
            elif change == "intent":
                data["oci_monitor_inactive_cleanup"]["phase"] = "intent"
            else:
                data["oci_monitor_inactive_cleanup"]["binding_digest"] = "sha256:" + "f" * 64
            mutation.write_state(data["status"], data)
    original = case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case)
    assert case.volume_record.read_bytes() == original


def test_original_delete_policy_cannot_be_silently_converted(case):
    with locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        data["oci_root"]["root_volume"]["retention_policy"] = "delete"
        mutation.write_state(data["status"], data)
    original = case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError, match="resource binding"):
        _run(case)
    assert case.volume_record.read_bytes() == original


def test_failed_intent_write_never_detaches(case, monkeypatch):
    before = case.volume_record.read_bytes()

    def fail(*_args, **_kwargs):
        raise StateError("intent fsync failed")

    monkeypatch.setattr(state_module.ExistingRunMutation, "write_state", fail)
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case)
    assert case.volume_record.read_bytes() == before


def test_resume_exact_detachment_after_completion_write_failure(case, monkeypatch):
    original_write = state_module.ExistingRunMutation.write_state

    def fail_completed(self, status, data):
        if data["oci_monitor_root_retention"]["phase"] == "completed":
            raise StateError("completion fsync failed")
        return original_write(self, status, data)

    with monkeypatch.context() as patch:
        patch.setattr(state_module.ExistingRunMutation, "write_state", fail_completed)
        with pytest.raises(retention.MonitorRootRetentionError):
            _run(case)
    intent = json.loads(case.state.read_bytes())["oci_monitor_root_retention"]
    assert intent["phase"] == "intent"
    detached = volumes.load_oci_root_volume(case.roots, case.volume.record.volume_id, runner=case.runner).record
    assert detached.status == "retained" and detached.generation == case.volume.record.generation + 1
    receipt = _run(case)
    assert receipt.retention_id == intent["retention_id"] and receipt.phase == "completed"
    assert volumes.load_oci_root_volume(case.roots, detached.volume_id, runner=case.runner).record == detached


def _claim_successor(case):
    source = case.volume.record
    return volumes.claim_oci_root_volume(
        case.roots,
        source.volume_id,
        size_bytes=source.size_bytes,
        lower_graph_digest=source.lower_graph_digest,
        retention_policy="retain",
        owner=ArtifactLeaseOwner(str(uuid.uuid4()), "successor", "root-lower"),
        runner=case.runner,
    )


def test_completed_replay_is_historical_and_does_not_open_or_detach_successor(case, monkeypatch):
    receipt = _run(case)
    successor = _claim_successor(case)
    record = case.volume_record.read_bytes()
    leases = _leases(case)

    def forbidden(*_args, **_kwargs):
        pytest.fail("completed replay touched the root")

    monkeypatch.setattr(retention, "_retain_exact_oci_root_volume", forbidden)
    assert _run(case) == receipt
    assert case.volume_record.read_bytes() == record
    assert successor.record.generation == receipt.retained_generation + 1
    assert _leases(case) == leases


def test_incomplete_intent_refuses_successor_attachment(case, monkeypatch):
    original_write = state_module.ExistingRunMutation.write_state

    def fail_completed(self, status, data):
        if data["oci_monitor_root_retention"]["phase"] == "completed":
            raise StateError("completion fsync failed")
        return original_write(self, status, data)

    with monkeypatch.context() as patch:
        patch.setattr(state_module.ExistingRunMutation, "write_state", fail_completed)
        with pytest.raises(retention.MonitorRootRetentionError):
            _run(case)
    _claim_successor(case)
    record = case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case)
    assert case.volume_record.read_bytes() == record


@pytest.mark.parametrize("change", ["generation", "owner", "retention", "size", "graph", "status"])
def test_changed_volume_attachment_is_not_detached(case, change):
    value = case.volume.record.to_dict()
    if change == "generation":
        value["generation"] += 1
    elif change == "owner":
        value["attached_run_id"] = str(uuid.uuid4())
        value["attached_run_name"] = "foreign"
    elif change == "retention":
        value["retention_policy"] = "delete"
    elif change == "size":
        value["size_bytes"] += 4096
    elif change == "graph":
        value["lower_graph_digest"] = "sha256:" + "f" * 64
    else:
        value["status"] = "retained"
        value["attached_run_id"] = value["attached_run_name"] = None
        value["generation"] += 1
    case.volume_record.write_text(json.dumps(value))
    before = case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case)
    assert case.volume_record.read_bytes() == before


def test_original_lower_lease_loss_blocks_detachment(case):
    leases = _leases(case)
    case.store.release_lease_set(leases)
    before = case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case)
    assert case.volume_record.read_bytes() == before


@pytest.mark.parametrize(
    "change", ["root-inode", "volume-directory", "journal", "record-generation", "writer", "domain", "uri"]
)
def test_authority_changes_during_locked_intent_callback_prevent_detach(case, monkeypatch, change):
    original_inspect = retention._inspect_domain
    original_probe = ProcessLiveness.STALE
    armed = False

    def inspect(conn, binding):
        nonlocal armed, original_probe
        result = original_inspect(conn, binding)
        if not armed and "oci_monitor_root_retention" in json.loads(case.state.read_bytes()):
            armed = True
            if change == "root-inode":
                original = case.volume.path
                moved = original.with_suffix(".saved")
                original.rename(moved)
                # A new sparse file with the same superblock is not the old root.
                with original.open("wb") as stream:
                    stream.truncate(case.volume.record.size_bytes)
                    with moved.open("rb") as source:
                        stream.write(source.read(2048))
                original.chmod(0o600)
            elif change == "volume-directory":
                original = case.roots.oci_root_volumes
                moved = original.with_name(original.name + "-saved")
                original.rename(moved)
                original.mkdir(mode=0o700)
                case.volume_record = moved / case.volume_record.name
            elif change == "journal":
                case.journal.write_bytes(case.journal.read_bytes() + b"\n")
            elif change == "record-generation":
                data = case.volume.record.to_dict()
                data["generation"] += 2
                case.volume_record.write_text(json.dumps(data))
            elif change == "writer":
                original_probe = ProcessLiveness.LIVE
            elif change == "domain":
                case.conn.conn.domains[case.binding.record.name] = case.domain.domain
                return case.domain
            else:
                case.conn.conn.uri = "qemu:///session"
        return result

    monkeypatch.setattr(retention, "_inspect_domain", inspect)
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case, liveness_probe=lambda _: original_probe)
    assert armed
    assert json.loads(case.volume_record.read_bytes())["status"] == "attached"
    assert json.loads(case.state.read_bytes())["oci_monitor_root_retention"]["phase"] == "intent"


def test_failed_detach_record_write_can_retry_original_attachment(case, monkeypatch):
    original = volumes._write_record

    def fail(*_args, **_kwargs):
        raise StateError("volume fsync failed")

    with monkeypatch.context() as patch:
        patch.setattr(volumes, "_write_record", fail)
        with pytest.raises(retention.MonitorRootRetentionError):
            _run(case)
    intent = json.loads(case.state.read_bytes())["oci_monitor_root_retention"]
    assert intent["phase"] == "intent"
    assert (
        volumes.load_oci_root_volume(case.roots, case.volume.record.volume_id, runner=case.runner).record
        == case.volume.record
    )
    monkeypatch.setattr(volumes, "_write_record", original)
    result = _run(case)
    assert result.retention_id == intent["retention_id"] and result.phase == "completed"


@pytest.mark.parametrize(
    "field",
    [
        "extra",
        "root_inode",
        "root_device",
        "retained_generation",
        "filesystem_uuid",
        "phase",
        "source_volume",
        "cleanup_receipt_digest",
    ],
)
def test_retention_receipt_rejects_malformed_fields(case, field):
    value = _run(case).to_dict()
    if field in {"root_inode", "root_device", "retained_generation"}:
        value[field] = True
    elif field == "source_volume":
        value[field]["status"] = "retained"
    else:
        value[field] = "invalid"
    with pytest.raises(retention.MonitorRootRetentionError):
        retention.MonitorRootRetentionReceipt.from_dict(value)


def test_intent_data_inode_is_bound_across_invocations(case, monkeypatch):
    def fail(*_args, **_kwargs):
        raise StateError("volume fsync failed")

    with monkeypatch.context() as patch:
        patch.setattr(volumes, "_write_record", fail)
        with pytest.raises(retention.MonitorRootRetentionError):
            _run(case)
    original = case.volume.path
    moved = original.with_suffix(".saved")
    original.rename(moved)
    with original.open("wb") as stream:
        stream.truncate(case.volume.record.size_bytes)
        with moved.open("rb") as source:
            stream.write(source.read(2048))
    original.chmod(0o600)
    with pytest.raises(retention.MonitorRootRetentionError, match="identity"):
        _run(case)
    assert json.loads(case.volume_record.read_bytes())["status"] == "attached"


@pytest.mark.parametrize("change", ["missing", "symlink", "hardlink", "mode", "busy"])
def test_retention_requires_existing_private_volume_lock_without_chmod(case, change):
    path = volumes._paths(case.roots, case.volume.record.volume_id)[2]
    held = None
    target = path.with_suffix(".other")
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        path.unlink()
        target.write_bytes(b"preserve mode")
        target.chmod(0o644)
        path.symlink_to(target)
    elif change == "hardlink":
        os.link(path, target)
    elif change == "mode":
        path.chmod(0o644)
    else:
        held = os.open(path, os.O_RDWR)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = case.volume_record.read_bytes()
    try:
        with pytest.raises(retention.MonitorRootRetentionError):
            _run(case)
        assert case.volume_record.read_bytes() == before
        if change == "symlink":
            assert target.stat().st_mode & 0o777 == 0o644
        elif change == "missing":
            assert not path.exists()
        elif change == "mode":
            assert path.stat().st_mode & 0o777 == 0o644
    finally:
        if held is not None:
            os.close(held)


def test_volume_lock_inode_swap_during_callback_refuses_detach(case, monkeypatch):
    path = volumes._paths(case.roots, case.volume.record.volume_id)[2]
    inspect_original = retention._inspect_domain
    changed = False

    def inspect(conn, binding):
        nonlocal changed
        result = inspect_original(conn, binding)
        if not changed and "oci_monitor_root_retention" in json.loads(case.state.read_bytes()):
            changed = True
            path.rename(path.with_suffix(".saved"))
            path.write_bytes(b"")
            path.chmod(0o600)
        return result

    monkeypatch.setattr(retention, "_inspect_domain", inspect)
    before = case.volume_record.read_bytes()
    with pytest.raises(retention.MonitorRootRetentionError):
        _run(case)
    assert changed and case.volume_record.read_bytes() == before


def test_retention_volume_lock_closes_after_fork_without_fd_reuse_damage(case):
    if not hasattr(os, "fork"):
        pytest.skip("requires fork")
    path = volumes._paths(case.roots, case.volume.record.volume_id)[2]
    lock = volumes._RetentionVolumeLock(case.roots, path)
    held_fd = lock.lock_fd
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.close(release_write)
        try:
            assert lock.lock_fd == lock.directory_fd == -1
            descriptor = os.open(os.devnull, os.O_RDONLY)
            if descriptor != held_fd:
                os.dup2(descriptor, held_fd)
                os.close(descriptor)
            lock.close()
            os.fstat(held_fd)
            os.close(held_fd)
            os.write(ready_write, b"Y")
            os.read(release_read, 1)
            os._exit(0)
        except BaseException:
            os.write(ready_write, b"N")
            os._exit(1)
    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 1) == b"Y"
        lock.close()
        check = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(check)
    finally:
        lock.close()
        os.write(release_write, b"X")
        os.close(release_write)
        os.close(ready_read)
        _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
