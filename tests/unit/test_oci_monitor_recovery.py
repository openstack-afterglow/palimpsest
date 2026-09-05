"""Inactive cleanup never inherits authority to kill, relaunch, or delete data."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_oci_store import (
    _FAKE_LIBVIRT,
    _committed_monitor_lease,
    _committed_oci_domain,
    _DefinitionConnection,
    _handoff_receipt,
)

from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local import oci_monitor_recovery as recovery
from palimpsest_local import oci_root_runtime as runtime
from palimpsest_local import state as state_module
from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor import MonitorBinding, ProcessLiveness
from palimpsest_local.runtime_types import ProcessExit, ProcessExitCategory
from palimpsest_local.state import locked_existing_run


class _Domain:
    def __init__(self, domain):
        self.domain = domain
        self.persistent = 1
        self.after_undefine = lambda: None

    def __getattr__(self, name):
        return getattr(self.domain, name)

    def name(self):
        return self.domain.name

    def isPersistent(self):
        return self.persistent

    def undefine(self):
        self.domain.undefine()
        self.after_undefine()
        return 0


class _Connection:
    def __init__(self, conn, domain):
        self.conn = conn
        self.domain = domain

    def getURI(self):
        return self.conn.getURI()

    def lookupByName(self, name):
        assert self.conn.lookupByName(name) is self.domain.domain
        return self.domain

    def lookupByUUIDString(self, identifier):
        assert self.conn.lookupByUUIDString(identifier) is self.domain.domain
        return self.domain


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    name = "recover-inactive"
    roots, store, tools, boot, profile, _prepared, _plan = _committed_oci_domain(tmp_path, name)
    conn = _DefinitionConnection()
    monkeypatch.setattr(runtime.kvm, "_libvirt", lambda: _FAKE_LIBVIRT)
    runtime.define_committed_oci_root_domain(roots, name, store, boot, profile, conn=conn, runner=tools)
    original = conn.domains[name]
    xml = ET.fromstring(original.xml)
    ET.SubElement(xml, "uuid").text = original.domain_uuid
    original.xml = ET.tostring(xml, encoding="unicode")
    binding = runtime.prepare_oci_root_monitor_binding(
        roots,
        name,
        store,
        boot,
        profile,
        conn=conn,
        boot_attempt_id=str(uuid.uuid4()),
        runner=tools,
    )
    lease = _committed_monitor_lease(roots, name, binding, monkeypatch, request)
    lease.mark_activating()
    lease.promote_active(
        MonitorBinding(
            binding.record,
            binding.owner_uid,
            binding.plan_digest,
            binding.expected_definition_projection_digest,
            binding.stage1_artifact_digest,
            binding.domain_uuid,
            7,
            binding.boot_attempt_id,
            binding.libvirt_uri,
        )
    )
    lease.mark_ready()
    snapshot = lease.snapshot
    lease.close()
    with locked_existing_run(roots, name) as mutation:
        data = mutation.mutable_state()
        data["oci_root_handoff"] = {
            "schema": "palimpsest.oci-root-handoff.v1",
            "boot_attempt_id": binding.boot_attempt_id,
            "domain_uuid": binding.domain_uuid,
            "domain_id": 7,
            "plan_digest": binding.plan_digest,
            "libvirt_uri": binding.libvirt_uri,
            "phase": "ready",
            "lifecycle": _handoff_receipt("ready", boot_attempt_id=binding.boot_attempt_id).to_dict(),
        }
        mutation.write_state("running", data)
    directory = roots.runs / name / "monitor-private"
    domain = _Domain(original)
    return SimpleNamespace(
        store=store,
        roots=roots,
        binding=binding,
        conn=_Connection(conn, domain),
        domain=domain,
        directory=directory,
        journal=directory / ipc._JOURNAL_NAME,
        lock=directory / ipc._LOCK_NAME,
        snapshot=snapshot,
        state=roots.runs / name / "state.json",
        owner=roots.runs / name / "owner.json",
    )


def _run(case, **kwargs):
    return recovery.reconcile_inactive_monitor_domain(
        case.roots,
        case.binding,
        conn=case.conn,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def test_cleanup_preserves_original_evidence_status_and_idempotent_receipt(case):
    before = json.loads(case.state.read_bytes())
    journal = case.journal.read_bytes()
    owner = case.owner.read_bytes()
    receipt = _run(case)
    assert receipt.phase == "completed"
    after = json.loads(case.state.read_bytes())
    assert after["oci_monitor_inactive_cleanup"] == receipt.to_dict()
    for field, value in before.items():
        if field != "lifecycle_revision":
            assert after[field] == value
    assert case.journal.read_bytes() == journal and case.owner.read_bytes() == owner
    assert case.domain.undefine_calls == 1
    assert case.domain.destroy_calls == case.domain.create_calls == 0
    assert case.domain.open_channel_calls == []
    assert _run(case) == receipt
    assert recovery.MonitorInactiveCleanupReceipt.from_dict(receipt.to_dict()) == receipt
    assert case.domain.undefine_calls == 1
    assert str(case.roots.state) not in json.dumps(receipt.to_dict())


@pytest.mark.parametrize("liveness", [ProcessLiveness.LIVE, ProcessLiveness.UNKNOWN, "stale", None, RuntimeError()])
def test_nonstale_writer_refused_without_mutation(case, liveness):
    before = case.state.read_bytes()

    def probe(_identity):
        if isinstance(liveness, Exception):
            raise liveness
        return liveness

    with pytest.raises(recovery.MonitorInactiveCleanupError, match="writer"):
        _run(case, liveness_probe=probe)
    assert case.domain.undefine_calls == case.domain.destroy_calls == 0
    assert case.state.read_bytes() == before


@pytest.mark.parametrize("change", ["active", "transient", "bool-active", "positive-id", "xml", "name", "uuid", "uri"])
def test_domain_mismatch_refuses_before_intent(case, change):
    before = case.state.read_bytes()
    if change == "active":
        case.domain.domain.active = 1
    elif change == "transient":
        case.domain.persistent = 0
    elif change == "bool-active":
        case.domain.isActive = lambda: False
    elif change == "positive-id":
        case.domain.ID = lambda: 7
    elif change == "xml":
        case.domain.domain.xml = case.domain.xml.replace("<memory", "<unexpected").replace("</memory", "</unexpected")
    elif change == "name":
        case.domain.name = lambda: "foreign"
    elif change == "uuid":
        case.domain.UUIDString = lambda: str(uuid.uuid4())
    else:
        case.conn.conn.uri = "qemu:///session"
    with pytest.raises(recovery.MonitorInactiveCleanupError):
        _run(case)
    assert case.state.read_bytes() == before
    assert case.domain.undefine_calls == case.domain.destroy_calls == 0


@pytest.mark.parametrize("change", ["missing", "symlink", "hardlink", "mode", "busy"])
def test_lock_must_be_exact_existing_owner_private_lock(case, change, tmp_path):
    held = None
    if change == "missing":
        case.lock.unlink()
    elif change == "symlink":
        moved = case.lock.with_suffix(".saved")
        case.lock.rename(moved)
        case.lock.symlink_to(moved)
    elif change == "hardlink":
        os.link(case.lock, case.lock.with_suffix(".saved"))
    elif change == "mode":
        case.lock.chmod(0o644)
    else:
        held = os.open(case.lock, os.O_RDWR)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(recovery.MonitorInactiveCleanupError):
            _run(case)
        assert case.domain.undefine_calls == 0
        if change == "missing":
            assert not case.lock.exists()
    finally:
        if held is not None:
            os.close(held)


def test_initial_absence_requires_prior_intent(case):
    case.conn.conn.domains.clear()
    before = case.state.read_bytes()
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="prior cleanup intent"):
        _run(case)
    assert case.state.read_bytes() == before


def test_undefine_ambiguous_after_deletion_resumes_only_existing_intent(case):
    case.domain.after_undefine = lambda: (_ for _ in ()).throw(RuntimeError("lost reply"))
    with pytest.raises(recovery.MonitorInactiveCleanupError):
        _run(case)
    intent = json.loads(case.state.read_bytes())["oci_monitor_inactive_cleanup"]
    assert intent["phase"] == "intent"
    result = _run(case)
    assert result.cleanup_id == intent["cleanup_id"] and result.phase == "completed"
    assert case.domain.undefine_calls == 1


def test_completed_receipt_never_authorizes_reappeared_definition(case):
    _run(case)
    case.conn.conn.domains[case.binding.record.name] = case.domain.domain
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="reappeared"):
        _run(case)
    assert case.domain.undefine_calls == 1


@pytest.mark.parametrize(
    "boundary", ["journal-bytes", "journal-inode", "lock-inode", "owner", "state", "uri", "writer"]
)
def test_rechecks_authority_after_last_domain_read(case, monkeypatch, boundary):
    original_id = case.domain.ID
    reads = 0
    invalidated = False

    def identity():
        nonlocal reads, invalidated
        reads += 1
        if reads == 6:
            invalidated = True
            if boundary == "journal-bytes":
                case.journal.write_bytes(case.journal.read_bytes() + b"\n")
            elif boundary in {"journal-inode", "lock-inode"}:
                path = case.journal if boundary == "journal-inode" else case.lock
                content = path.read_bytes()
                path.rename(path.with_suffix(".saved"))
                path.write_bytes(content)
                path.chmod(0o600)
            elif boundary == "owner":
                case.owner.chmod(0o644)
            elif boundary == "state":
                case.state.write_bytes(case.state.read_bytes() + b"\n")
            elif boundary == "uri":
                case.conn.conn.uri = "qemu:///session"
        return original_id()

    case.domain.ID = identity

    def probe(_identity):
        return ProcessLiveness.LIVE if boundary == "writer" and invalidated else ProcessLiveness.STALE

    with pytest.raises(recovery.MonitorInactiveCleanupError):
        _run(case, liveness_probe=probe)
    assert invalidated
    assert case.domain.undefine_calls == case.domain.destroy_calls == 0


def test_intent_write_failure_prevents_undefine(case, monkeypatch):
    def failure(*_args, **_kwargs):
        raise StateError("fsync failed")

    monkeypatch.setattr(state_module.ExistingRunMutation, "write_state", failure)
    with pytest.raises(recovery.MonitorInactiveCleanupError):
        _run(case)
    assert case.domain.undefine_calls == 0


@pytest.mark.parametrize("phase", ["intent", "completed"])
def test_same_bytes_new_journal_inode_invalidates_resume(case, phase):
    if phase == "intent":
        case.domain.after_undefine = lambda: (_ for _ in ()).throw(RuntimeError())
        with pytest.raises(recovery.MonitorInactiveCleanupError):
            _run(case)
    else:
        _run(case)
    content = case.journal.read_bytes()
    case.journal.rename(case.journal.with_suffix(".saved"))
    case.journal.write_bytes(content)
    case.journal.chmod(0o600)
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="evidence changed"):
        _run(case)
    assert case.domain.undefine_calls == 1


@pytest.mark.parametrize(
    "field", ["boot_attempt_id", "stage1_artifact_digest", "plan_digest", "owner_uid", "domain_uuid"]
)
def test_binding_changes_rejected_without_mutation(case, field):
    value = "sha256:" + "f" * 64 if "digest" in field else str(uuid.uuid4())
    if field == "owner_uid":
        value = os.geteuid() + 1
    changed = replace(case.binding)
    object.__setattr__(changed, field, value)
    before = case.state.read_bytes()
    with pytest.raises(recovery.MonitorInactiveCleanupError):
        recovery.reconcile_inactive_monitor_domain(
            case.roots, changed, conn=case.conn, liveness_probe=lambda _: ProcessLiveness.STALE
        )
    assert case.domain.undefine_calls == 0 and case.state.read_bytes() == before


@pytest.mark.parametrize("side", ["lookupByName", "lookupByUUIDString"])
def test_asymmetric_absence_rejects_cleanup(case, side):
    def missing(_value):
        raise _FAKE_LIBVIRT.libvirtError("gone", _FAKE_LIBVIRT.VIR_ERR_NO_DOMAIN)

    setattr(case.conn, side, missing)
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="disagree"):
        _run(case)
    assert case.domain.undefine_calls == 0


def test_activation_at_last_instance_read_prevents_undefine(case):
    reads = 0

    def identity():
        nonlocal reads
        reads += 1
        if reads == 6:
            case.domain.domain.active = 1
        return 7 if case.domain.domain.active else -1

    case.domain.ID = identity
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="inactive"):
        _run(case)
    assert reads == 6
    assert case.domain.undefine_calls == case.domain.destroy_calls == 0


def test_cleanup_fork_closes_inherited_flock_and_does_not_close_reused_fd(case):
    if not hasattr(os, "fork"):
        pytest.skip("requires fork")
    with locked_existing_run(case.roots, case.binding.record.name) as mutation:
        authority = recovery._RecoveryAuthority(mutation, case.binding)
        held_fd = authority.lock_fd
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(ready_read)
            os.close(release_write)
            try:
                assert authority.lock_fd == authority.directory_fd == authority.journal_fd == -1
                descriptor = os.open(os.devnull, os.O_RDONLY)
                if descriptor != held_fd:
                    os.dup2(descriptor, held_fd)
                    os.close(descriptor)
                authority.close()
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
            authority.close()
            check = os.open(case.lock, os.O_RDWR)
            try:
                fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(check)
        finally:
            authority.close()
            os.write(release_write, b"X")
            os.close(release_write)
            os.close(ready_read)
            _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0


@pytest.mark.parametrize(
    ("journal_phase", "status", "handoff_phase"),
    [
        ("active", "starting", "activating"),
        ("active", "starting", "starting"),
        ("active", "running", "ready"),
        ("ready", "running", "ready"),
        ("ready", "exited", "terminal"),
        ("terminal", "exited", "terminal"),
        ("control-lost", "failed", "cleanup-required"),
        ("ready", "failed", "cleanup-required"),
    ],
)
def test_supported_crash_compatible_ledger_pairs(case, journal_phase, status, handoff_phase):
    journal = replace(case.snapshot, phase=journal_phase)
    case.journal.write_bytes(ipc._canonical_bytes(journal.to_dict()) + b"\n")
    with locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        handoff = data["oci_root_handoff"]
        handoff["phase"] = handoff_phase
        if handoff_phase in {"activating", "starting"}:
            handoff.pop("lifecycle")
        if handoff_phase == "activating":
            handoff.pop("domain_id")
        if handoff_phase == "terminal":
            handoff["lifecycle"] = _handoff_receipt(
                "terminal",
                ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
                boot_attempt_id=case.binding.boot_attempt_id,
            ).to_dict()
        mutation.write_state(status, data)
    receipt = _run(case)
    assert receipt.phase == "completed"
    assert json.loads(case.state.read_bytes())["status"] == status


@pytest.mark.parametrize(
    "tamper",
    [
        "domain-id",
        "attempt",
        "receipt",
        "missing-ready-id",
        "missing-ready-receipt",
        "failed-ready-id",
        "failed-ready-receipt",
        "starting-receipt",
        "terminal-result",
        "terminal-fields",
        "transcript",
    ],
)
def test_malformed_handoff_refuses_undefine(case, tamper):
    with locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        handoff = data["oci_root_handoff"]
        if tamper == "domain-id":
            handoff["domain_id"] = 1234
        elif tamper == "attempt":
            handoff["boot_attempt_id"] = str(uuid.uuid4())
        elif tamper == "receipt":
            handoff["lifecycle"]["phase"] = "terminal"
        elif tamper in {"missing-ready-id", "failed-ready-id"}:
            handoff.pop("domain_id")
        elif tamper in {"missing-ready-receipt", "failed-ready-receipt"}:
            handoff.pop("lifecycle")
        elif tamper == "transcript":
            handoff["lifecycle"]["transcript"] = ["malformed"]
        elif tamper == "starting-receipt":
            handoff["phase"] = "starting"
            journal = replace(case.snapshot, phase="active")
            case.journal.write_bytes(ipc._canonical_bytes(journal.to_dict()) + b"\n")
        else:
            handoff["phase"] = "terminal"
            handoff["lifecycle"] = _handoff_receipt(
                "terminal",
                ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
                boot_attempt_id=case.binding.boot_attempt_id,
            ).to_dict()
            if tamper == "terminal-result":
                handoff["lifecycle"]["terminal"]["returncode"] = True
            else:
                handoff["lifecycle"]["terminal"]["extra"] = "invalid"
        status = "exited" if tamper.startswith("terminal-") else "running"
        if tamper.startswith("failed-"):
            status = "failed"
            handoff["phase"] = "cleanup-required"
        elif tamper == "starting-receipt":
            status = "starting"
        mutation.write_state(status, data)
    with pytest.raises(recovery.MonitorInactiveCleanupError):
        _run(case)
    assert case.domain.undefine_calls == 0


def test_uncaptured_control_lost_journal_refuses_cleanup(case):
    journal = replace(case.snapshot, phase="control-lost", active_binding=None)
    case.journal.write_bytes(ipc._canonical_bytes(journal.to_dict()) + b"\n")
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="captured"):
        _run(case)
    assert case.domain.undefine_calls == 0


@pytest.mark.parametrize(
    "field", ["journal_inode", "journal_device", "journal_digest", "writer", "binding_digest", "phase", "extra"]
)
def test_cleanup_receipt_strictly_rejects_mutated_or_extra_fields(case, field):
    receipt = _run(case).to_dict()
    if field in {"journal_inode", "journal_device"}:
        receipt[field] = True
    elif field == "writer":
        receipt[field]["pid"] = True
    else:
        receipt[field] = "invalid"
    with pytest.raises(recovery.MonitorInactiveCleanupError):
        recovery.MonitorInactiveCleanupReceipt.from_dict(receipt)


def test_null_cleanup_record_cannot_be_overwritten_as_fresh_intent(case):
    with locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        data["oci_monitor_inactive_cleanup"] = None
        mutation.write_state(data["status"], data)
    with pytest.raises(recovery.MonitorInactiveCleanupError, match="receipt"):
        _run(case)
    assert case.domain.undefine_calls == 0
