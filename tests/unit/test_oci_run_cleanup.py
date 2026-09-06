"""Normal rm integrates the existing exact ACL/resource cleanup transactions."""

import fcntl
import json
import os
from dataclasses import replace

import pytest
import test_oci_boot_access as boot_tests
import test_oci_lower_access as lower_tests
import test_oci_root_access as root_tests
import test_oci_runtime_access as runtime_tests
import test_oci_shared_traversal as shared_tests
import test_oci_stage1_access as stage_tests
import test_oci_store as fixtures

from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local import oci_root_prepare as preparation
from palimpsest_local import oci_run_cleanup as cleanup
from palimpsest_local import state
from palimpsest_local.errors import StateError


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    if getattr(request, "param", "delete") == "retain":
        prepare = fixtures.prepare_oci_root_run

        def retain(*args, **kwargs):
            return prepare(*args, **kwargs, retention_policy="retain")

        monkeypatch.setattr(fixtures, "prepare_oci_root_run", retain)
    value = lower_tests.case.__wrapped__(tmp_path, monkeypatch)
    boot_tests.shared_case.__wrapped__(value, monkeypatch)
    stage_tests._install_stage1_backend(value, monkeypatch)
    shared_tests._join(value)
    root_tests.grant(value)
    stage_tests.grant(value)
    boot_tests.grant(value)
    lower_tests.grant(value)
    runtime_tests._terminal_cleanup(value, monkeypatch, request)
    value.directory = value.run_root / "monitor-private"
    descriptor = os.open(value.directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        value.journal = ipc._read_preactivation_journal(descriptor, value.binding)[0]
    finally:
        os.close(descriptor)
    # Simulate a prior completed normal SHUTDOWN. Product stale-socket recovery
    # deliberately cannot do this, and is separately refused below.
    assert not (value.directory / value.journal.socket_name).exists()
    return value


def remove(case, **kwargs):
    return cleanup.remove_oci_run(
        case.roots,
        case.binding,
        case.store,
        conn=case.conn,
        acl_backend=case.backend,
        runner=case.runner,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ipc.ProcessLiveness.STALE),
        **kwargs,
    )


def test_normal_terminal_removal_uses_real_revocation_release_and_pinned_delete(case):
    assert cleanup.load_oci_run_binding(case.roots, case.name) == case.binding
    result = remove(case)
    assert result == cleanup.OCIRunRemovalResult(
        case.name, case.binding.record.run_id, case.prepared.transaction.volume_id, "delete"
    )
    assert not case.run_root.exists()
    with pytest.raises(StateError, match="missing"):
        preparation.load_oci_root_volume(case.roots, result.root_volume_id, runner=case.runner)


@pytest.mark.parametrize("case", ["retain"], indirect=True)
def test_retained_root_policy_remains_exact_and_reusable_after_run_removal(case):
    result = remove(case)
    assert result.retention_policy == "retain"
    assert not case.run_root.exists()
    volume = preparation.load_oci_root_volume(case.roots, result.root_volume_id, runner=case.runner).record
    assert volume.status == "retained"
    assert volume.lower_graph_digest == case.prepared.transaction.lower_graph_digest


def test_live_terminal_monitor_is_shutdown_without_holding_run_lock(case, monkeypatch):
    socket_path = case.directory / case.journal.socket_name
    socket_path.touch(mode=0o600)
    live = [ipc.ProcessLiveness.LIVE]
    calls = []
    journal_lock = os.open(case.directory / ipc._LOCK_NAME, os.O_RDWR)
    fcntl.flock(journal_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def shutdown(descriptor, endpoint, *, timeout):
        assert endpoint == case.journal.endpoint
        assert os.fstat(descriptor).st_ino == case.directory.stat().st_ino
        # Acquiring the same lock independently proves rm released it for IPC.
        with state.locked_existing_run(case.roots, case.name, lock_timeout=0.1):
            calls.append("shutdown")
        socket_path.unlink()
        fcntl.flock(journal_lock, fcntl.LOCK_UN)
        live[0] = ipc.ProcessLiveness.STALE
        return case.journal

    monkeypatch.setattr(ipc, "shutdown_monitor_exec", shutdown)
    try:
        assert cleanup.load_oci_run_binding(case.roots, case.name) == case.binding
        remove(case, liveness_probe=lambda _: live[0])
    finally:
        os.close(journal_lock)
    assert calls == ["shutdown"] and not case.run_root.exists()


def test_shutdown_refusal_does_not_revoke_or_release(case, monkeypatch):
    (case.directory / case.journal.socket_name).touch(mode=0o600)
    before = case.state.read_bytes(), list(case.backend.writes)

    def refused(*args, **kwargs):
        raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.CONTROL_LOST)

    monkeypatch.setattr(ipc, "shutdown_monitor_exec", refused)
    with pytest.raises(cleanup.OCIRunRemovalError):
        remove(case, liveness_probe=lambda _: ipc.ProcessLiveness.LIVE)
    assert (case.state.read_bytes(), case.backend.writes) == before


@pytest.mark.parametrize(
    "operation",
    [
        "revoke_oci_lower_access",
        "revoke_oci_boot_access",
        "revoke_oci_stage1_access",
        "revoke_oci_root_access",
        "revoke_oci_runtime_access",
        "leave_oci_shared_traversal",
    ],
)
def test_partial_revocation_retry_uses_existing_exact_receipts(case, monkeypatch, operation):
    original = getattr(cleanup, operation)

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise StateError("interrupted after durable revoke")

    monkeypatch.setattr(cleanup, operation, fail)
    with pytest.raises(cleanup.OCIRunRemovalError):
        remove(case)
    assert case.run_root.exists()
    assert json.loads(case.state.read_bytes())["oci_root"]["phase"] == "resources-ready"
    monkeypatch.setattr(cleanup, operation, original)
    remove(case)
    assert not case.run_root.exists()


@pytest.mark.parametrize("phase", ["release-required", "released"])
def test_release_retry_preserves_cleanup_receipts_and_skips_obsolete_grants(case, monkeypatch, phase):
    target = "_finish_release" if phase == "release-required" else "_commit_released_ledger"
    original = getattr(preparation, target)

    def fail(*args, **kwargs):
        if phase == "released":
            original(*args, **kwargs)
        raise StateError("interrupted release")

    monkeypatch.setattr(preparation, target, fail)
    with pytest.raises(cleanup.OCIRunRemovalError):
        remove(case)
    saved = json.loads(case.state.read_bytes())
    assert saved["oci_root"]["phase"] == phase
    assert saved["oci_run_removal"]["binding"] == case.binding.to_dict()
    assert saved["oci_lower_access"]["phase"] == "revoked"
    assert saved["oci_shared_traversal"]["phase"] == "left"
    monkeypatch.setattr(preparation, target, original)
    monkeypatch.setattr(cleanup, "revoke_oci_lower_access", lambda *a, **k: pytest.fail("obsolete grant reread"))
    remove(case)
    assert not case.run_root.exists()


@pytest.mark.parametrize("damage", ["missing", "transaction", "journal_inode", "binding"])
def test_release_retry_rejects_changed_durable_milestone(case, monkeypatch, damage):
    original = preparation._finish_release
    monkeypatch.setattr(preparation, "_finish_release", lambda *a, **k: (_ for _ in ()).throw(StateError("fail")))
    with pytest.raises(cleanup.OCIRunRemovalError):
        remove(case)
    monkeypatch.setattr(preparation, "_finish_release", original)
    with state.locked_existing_run(case.roots, case.name) as mutation:
        data = mutation.mutable_state()
        if damage == "missing":
            data.pop("oci_run_removal")
        elif damage == "transaction":
            data["oci_run_removal"]["transaction"]["root_volume"]["retention_policy"] = "retain"
        elif damage == "journal_inode":
            data["oci_run_removal"]["journal_inode"] += 1
        else:
            data["oci_run_removal"]["binding"]["domain_uuid"] = "00000000-0000-0000-0000-000000000001"
        mutation.write_state(data["status"], data)
    before = case.state.read_bytes()
    with pytest.raises(cleanup.OCIRunRemovalError):
        remove(case)
    assert case.state.read_bytes() == before and case.run_root.exists()


@pytest.mark.parametrize("liveness", [ipc.ProcessLiveness.LIVE, ipc.ProcessLiveness.UNKNOWN])
def test_live_or_unknown_writer_never_releases_resources(case, liveness):
    before = case.state.read_bytes(), list(case.backend.writes)
    with pytest.raises(cleanup.OCIRunRemovalError):
        remove(case, timeout=0.1, liveness_probe=lambda _: liveness)
    assert (case.state.read_bytes(), case.backend.writes) == before


def test_stale_writer_with_remaining_socket_is_not_normal_removal(case):
    # Any remaining entry is evidence, including a replaced regular file.
    (case.directory / case.journal.socket_name).touch(mode=0o600)
    before = case.state.read_bytes(), list(case.backend.writes)
    with pytest.raises(cleanup.OCIRunRemovalError, match="requires recovery"):
        remove(case)
    assert (case.state.read_bytes(), case.backend.writes) == before


def test_running_journal_requires_stop_before_any_cleanup(case):
    journal = replace(case.journal, phase="ready", revision=case.journal.revision - 1)
    (case.directory / ipc._JOURNAL_NAME).write_bytes(ipc._canonical_bytes(journal.to_dict()) + b"\n")
    before = case.state.read_bytes(), list(case.backend.writes)
    with pytest.raises(cleanup.OCIRunRemovalError, match="stop it"):
        remove(case)
    assert (case.state.read_bytes(), case.backend.writes) == before


def test_binding_loader_rejects_replaced_owner(case):
    before = case.state.read_bytes()
    journal = case.journal.to_dict()
    journal["binding"]["owner_uid"] += 1
    (case.directory / ipc._JOURNAL_NAME).write_bytes(ipc._canonical_bytes(journal) + b"\n")
    with pytest.raises(cleanup.OCIRunRemovalError):
        cleanup.load_oci_run_binding(case.roots, case.name)
    assert case.state.read_bytes() == before


def test_fresh_cleanup_does_not_overwrite_a_conflicting_milestone(case):
    with state.locked_existing_run(case.roots, case.name) as mutation:
        data = mutation.mutable_state()
        data["oci_run_removal"] = {"schema": "foreign"}
        mutation.write_state(data["status"], data)
    with pytest.raises(cleanup.OCIRunRemovalError, match="milestone"):
        remove(case)
    saved = json.loads(case.state.read_bytes())
    assert saved["oci_run_removal"] == {"schema": "foreign"}
    assert saved["oci_root"]["phase"] == "resources-ready"
