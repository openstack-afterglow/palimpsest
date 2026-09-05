"""The successor's complete lower graph must protect every retired source pin."""

from __future__ import annotations

import json
import os
import uuid

import pytest
import test_oci_monitor_retention as retention_tests
import test_oci_store as store_tests

from palimpsest_local import oci_monitor_handoff as handoff
from palimpsest_local import oci_root_volume as volumes
from palimpsest_local import state as state_module
from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor import ProcessLiveness
from palimpsest_local.oci_store import ArtifactLeaseOwner
from palimpsest_local.state import locked_existing_run, read_run_ledger_snapshot, reserve_new_run


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    value = retention_tests.case.__wrapped__(tmp_path, monkeypatch, request)
    value.retention = retention_tests._run(value)
    value.source_leases = retention_tests._leases(value)
    successor_name = getattr(request, "param", "successor")
    with reserve_new_run(value.roots, successor_name, store_tests._oci_dispatch()) as reservation:
        value.prepared_successor = store_tests.prepare_oci_root_run(
            reservation,
            store_tests._image_materialization(value.store),
            value.store,
            root_volume_size_bytes=value.volume.record.size_bytes,
            retained_volume_id=value.volume.record.volume_id,
            retention_policy="retain",
            runner=value.runner,
        )
    value.successor = read_run_ledger_snapshot(value.roots, successor_name).record
    value.successor_state = value.roots.runs / successor_name / "state.json"
    value.successor_leases = value.store.load_lease_set(
        value.prepared_successor.transaction.lower_lease_set_id,
        value.prepared_successor.transaction.owner,
        plan_digest=value.prepared_successor.transaction.boot_plan_digest,
    )
    return value


def test_partial_retirement_resumes_from_saved_original_snapshot(case, monkeypatch):
    unlink = os.unlink
    second = case.source_leases.members[1].lease_id

    def fail_second(path, *args, **kwargs):
        if path == second:
            raise OSError("injected second pin unlink failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_second)
        with pytest.raises(handoff.MonitorLowerHandoffError):
            _run(case)
    intent = json.loads(case.state.read_bytes())["oci_monitor_lower_handoff"]
    assert intent["phase"] == "intent"
    assert intent["source_leases"] == handoff._lease_snapshot(case.source_leases)
    lease_dir = case.roots.oci_derived_store / "leases"
    assert not (lease_dir / case.source_leases.members[0].lease_id).exists()
    assert (lease_dir / second).exists()
    receipt = _run(case)
    assert receipt.phase == "completed" and receipt.handoff_id == intent["handoff_id"]
    case.store._require_lease_set_retired(case.source_leases)


def test_partial_source_without_own_intent_is_refused(case):
    before = case.state.read_bytes()
    (case.roots.oci_derived_store / "leases" / case.source_leases.members[0].lease_id).unlink()
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)
    assert case.state.read_bytes() == before


@pytest.mark.parametrize(
    "change", ["root-generation", "journal", "successor-state", "writer", "successor-domain", "uri"]
)
def test_mid_retirement_authority_drift_preserves_remaining_source_pins(case, monkeypatch, change):
    original = case.store._retire_replaced_lease_set
    stale = True
    changed = False

    def retire(source, successor, *, resume, verify):
        calls = 0

        def guarded_verify():
            nonlocal calls, stale, changed
            calls += 1
            if calls == 2:
                changed = True
                if change == "root-generation":
                    record = json.loads(case.volume_record.read_bytes())
                    record["generation"] += 1
                    case.volume_record.write_text(json.dumps(record))
                elif change == "journal":
                    case.journal.write_bytes(case.journal.read_bytes() + b"\n")
                elif change == "successor-state":
                    case.successor_state.write_bytes(case.successor_state.read_bytes() + b"\n")
                elif change == "writer":
                    stale = False
                elif change == "successor-domain":
                    case.conn.conn.domains[case.successor.name] = case.domain.domain
                else:
                    case.conn.conn.uri = "qemu:///session"
            verify()

        return original(source, successor, resume=resume, verify=guarded_verify)

    monkeypatch.setattr(case.store, "_retire_replaced_lease_set", retire)
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case, liveness_probe=lambda _: ProcessLiveness.STALE if stale else ProcessLiveness.LIVE)
    assert changed
    assert (
        case.store.load_lease_set(
            case.source_leases.lease_set_id, case.source_leases.owner, plan_digest=case.source_leases.plan_digest
        )
        == case.source_leases
    )


def test_successor_pin_reacquisition_cannot_replace_saved_intent(case, monkeypatch):
    original = case.store._retire_replaced_lease_set

    def replace_successor(source, successor, *, resume, verify):
        case.store.release_lease_set(successor)
        replacement = case.store.acquire_lease_set(
            tuple(member.receipt for member in successor.members), successor.owner, plan_digest=successor.plan_digest
        )
        assert replacement != successor
        return original(source, successor, resume=resume, verify=verify)

    with monkeypatch.context() as patch:
        patch.setattr(case.store, "_retire_replaced_lease_set", replace_successor)
        with pytest.raises(handoff.MonitorLowerHandoffError):
            _run(case)
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)
    assert (
        case.store.load_lease_set(
            case.source_leases.lease_set_id, case.source_leases.owner, plan_digest=case.source_leases.plan_digest
        )
        == case.source_leases
    )


def test_retirement_callbacks_do_not_write_ledgers_or_reenter_store_loads(case, monkeypatch):
    retiring = False
    original_retire = case.store._retire_replaced_lease_set
    original_write = state_module.ExistingRunMutation.write_state
    original_load = case.store.load_lease_set

    def retire(*args, **kwargs):
        nonlocal retiring
        retiring = True
        try:
            return original_retire(*args, **kwargs)
        finally:
            retiring = False

    def write(*args, **kwargs):
        assert not retiring, "ledger write under store digest guards"
        return original_write(*args, **kwargs)

    def load(*args, **kwargs):
        assert not retiring, "store load reentered by guard callback"
        return original_load(*args, **kwargs)

    monkeypatch.setattr(case.store, "_retire_replaced_lease_set", retire)
    monkeypatch.setattr(state_module.ExistingRunMutation, "write_state", write)
    monkeypatch.setattr(case.store, "load_lease_set", load)
    assert _run(case).phase == "completed"


@pytest.mark.parametrize("case", ["aaa-successor"], indirect=True)
def test_two_run_locks_follow_sorted_names(case, monkeypatch):
    original = handoff.locked_existing_run
    names = []

    def lock(roots, name, **kwargs):
        names.append(name)
        return original(roots, name, **kwargs)

    monkeypatch.setattr(handoff, "locked_existing_run", lock)
    assert _run(case).phase == "completed"
    assert names == [case.binding.record.name, "aaa-successor", case.binding.record.name]


@pytest.mark.parametrize("change", ["member-id", "set-id", "plan-digest", "extra", "acquired-type"])
def test_completed_successor_snapshot_corruption_refuses_without_mutation(case, change):
    _run(case)
    with locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        snapshot = data["oci_monitor_lower_handoff"]["successor_leases"]
        if change == "member-id":
            snapshot["members"][0]["lease_id"] = str(uuid.uuid4())
        elif change == "set-id":
            snapshot["lease_set_id"] = "sha256:" + "a" * 64
        elif change == "plan-digest":
            snapshot["plan_digest"] = "sha256:" + "b" * 64
        elif change == "extra":
            snapshot["unexpected"] = True
        else:
            snapshot["members"][0]["acquired_ns"] = True
        mutation.write_state(data["status"], data)
    before = case.state.read_bytes()
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)
    assert case.state.read_bytes() == before


def test_completed_replay_rejects_reappeared_old_pins(case):
    _run(case)
    source = case.source_leases
    recreated = case.store.acquire_lease_set(
        tuple(member.receipt for member in source.members), source.owner, plan_digest=source.plan_digest
    )
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)
    assert case.store.load_lease_set(source.lease_set_id, source.owner, plan_digest=source.plan_digest) == recreated


def test_completed_replay_after_actual_later_root_owner_and_target_removal(case):
    receipt = _run(case)
    source = case.retention.source_volume
    volumes.release_oci_root_volume(
        case.roots,
        source.volume_id,
        owner=case.successor_leases.owner,
        lower_graph_digest=source.lower_graph_digest,
        delete=False,
        runner=case.runner,
    )
    volumes.claim_oci_root_volume(
        case.roots,
        source.volume_id,
        size_bytes=source.size_bytes,
        lower_graph_digest=source.lower_graph_digest,
        retention_policy="retain",
        owner=ArtifactLeaseOwner(str(uuid.uuid4()), "later-owner", "root-lower"),
        runner=case.runner,
    )
    successor_dir = case.successor_state.parent
    successor_dir.rename(successor_dir.with_name("saved-successor"))
    before = case.volume_record.read_bytes()
    assert json.loads(before)["generation"] == case.retention.retained_generation + 3
    assert _run(case) == receipt
    assert case.volume_record.read_bytes() == before


def _run(case, **kwargs):
    return handoff.handoff_retained_root_lower_leases(
        case.roots,
        case.binding,
        case.successor,
        case.store,
        conn=case.conn,
        runner=case.runner,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def test_handoff_retires_old_pins_only_and_preserves_both_run_evidence(case):
    before = json.loads(case.state.read_bytes())
    target_before = case.successor_state.read_bytes()
    root_stat = case.volume.path.stat()
    volume_before = case.volume_record.read_bytes()
    journal = case.journal.read_bytes()
    receipt = _run(case)
    assert receipt.phase == "completed"
    case.store._require_lease_set_retired(case.source_leases)
    successor = case.prepared_successor.transaction
    assert (
        case.store.load_lease_set(successor.lower_lease_set_id, successor.owner, plan_digest=successor.boot_plan_digest)
        == case.successor_leases
    )
    after = json.loads(case.state.read_bytes())
    assert after["oci_monitor_lower_handoff"] == receipt.to_dict()
    assert handoff.MonitorLowerHandoffReceipt.from_dict(receipt.to_dict()) == receipt
    for key, value in before.items():
        if key != "lifecycle_revision":
            assert after[key] == value
    assert case.successor_state.read_bytes() == target_before
    assert case.volume.path.stat() == root_stat and case.volume_record.read_bytes() == volume_before
    assert case.journal.read_bytes() == journal
    assert _run(case) == receipt
    with pytest.raises(StateError):
        retention_tests._run(case)  # Old retention API intentionally remains fail-closed after old-pin retirement.


@pytest.mark.parametrize("change", ["status", "domain-plan", "definition", "handoff", "monitor", "transaction-phase"])
def test_successor_must_be_resources_ready_before_definition(case, change):
    with locked_existing_run(case.roots, case.successor.name) as mutation:
        data = mutation.mutable_state()
        status = "creating"
        if change == "status":
            status = "defined"
        elif change == "transaction-phase":
            data["oci_root"]["phase"] = "resources-planned"
        else:
            data[
                {
                    "domain-plan": "oci_root_domain",
                    "definition": "oci_root_definition",
                    "handoff": "oci_root_handoff",
                    "monitor": "oci_monitor_fake",
                }[change]
            ] = {}
        mutation.write_state(status, data)
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)
    assert (
        case.store.load_lease_set(
            case.source_leases.lease_set_id, case.source_leases.owner, plan_digest=case.source_leases.plan_digest
        )
        == case.source_leases
    )


@pytest.mark.parametrize("liveness", [ProcessLiveness.LIVE, ProcessLiveness.UNKNOWN, None])
def test_live_or_unknown_old_writer_cannot_retire_pins(case, liveness):
    original = case.state.read_bytes()
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case, liveness_probe=lambda _: liveness)
    assert case.state.read_bytes() == original
    assert (
        case.store.load_lease_set(
            case.source_leases.lease_set_id, case.source_leases.owner, plan_digest=case.source_leases.plan_digest
        )
        == case.source_leases
    )


def test_intent_write_failure_never_enters_retirement(case, monkeypatch):
    def fail(*_args, **_kwargs):
        raise StateError("intent fsync failed")

    def forbidden(*_args, **_kwargs):
        pytest.fail("store retirement entered before durable intent")

    monkeypatch.setattr(state_module.ExistingRunMutation, "write_state", fail)
    monkeypatch.setattr(case.store, "_retire_replaced_lease_set", forbidden)
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)


def test_old_pin_retirement_complete_but_ledger_write_failed_can_resume(case, monkeypatch):
    original = state_module.ExistingRunMutation.write_state

    def fail_completed(self, status, data):
        if data["oci_monitor_lower_handoff"]["phase"] == "completed":
            raise StateError("completion fsync failed")
        return original(self, status, data)

    with monkeypatch.context() as patch:
        patch.setattr(state_module.ExistingRunMutation, "write_state", fail_completed)
        with pytest.raises(handoff.MonitorLowerHandoffError):
            _run(case)
    intent = json.loads(case.state.read_bytes())["oci_monitor_lower_handoff"]
    assert intent["phase"] == "intent"
    case.store._require_lease_set_retired(case.source_leases)
    receipt = _run(case)
    assert receipt.phase == "completed" and receipt.handoff_id == intent["handoff_id"]


def test_historical_handoff_replay_does_not_open_successor_or_volume(case, monkeypatch):
    receipt = _run(case)
    with locked_existing_run(case.roots, case.successor.name) as mutation:
        mutation.write_state("running", mutation.mutable_state())
    volume_before = case.volume_record.read_bytes()
    successor_before = case.successor_state.read_bytes()
    original_locked_run = handoff.locked_existing_run

    def lock(roots, name, **kwargs):
        assert name == case.binding.record.name
        return original_locked_run(roots, name, **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("historical handoff replay touched successor resource")

    monkeypatch.setattr(handoff, "locked_existing_run", lock)
    monkeypatch.setattr(handoff, "_locked_exact_root_volume", forbidden)
    monkeypatch.setattr(case.store, "_retire_replaced_lease_set", forbidden)
    assert _run(case) == receipt
    assert case.volume_record.read_bytes() == volume_before and case.successor_state.read_bytes() == successor_before


@pytest.mark.parametrize("change", ["generation", "owner", "root-inode", "missing-successor-leases"])
def test_changed_successor_attachment_or_pins_block_handoff(case, change):
    if change == "missing-successor-leases":
        case.store.release_lease_set(case.successor_leases)
    elif change == "root-inode":
        original = case.volume.path
        moved = original.with_suffix(".saved")
        original.rename(moved)
        with original.open("wb") as stream:
            stream.truncate(case.volume.record.size_bytes)
            with moved.open("rb") as source:
                stream.write(source.read(2048))
        original.chmod(0o600)
    else:
        record = json.loads(case.volume_record.read_bytes())
        if change == "generation":
            record["generation"] += 1
        else:
            record["attached_run_id"] = str(uuid.uuid4())
            record["attached_run_name"] = "foreign"
        case.volume_record.write_text(json.dumps(record))
    with pytest.raises(handoff.MonitorLowerHandoffError):
        _run(case)
    assert (
        case.store.load_lease_set(
            case.source_leases.lease_set_id, case.source_leases.owner, plan_digest=case.source_leases.plan_digest
        )
        == case.source_leases
    )
