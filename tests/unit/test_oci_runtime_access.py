"""Exact production grant/restoration state machine with a deterministic FD ACL store."""

import json
import os
import stat
import uuid
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
import test_oci_monitor_recovery as recovery_tests
import test_oci_store as fixtures

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_root_runtime as runtime
from palimpsest_local import oci_runtime_access as access
from palimpsest_local import oci_runtime_io as io
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import baseline_acl
from palimpsest_local.oci_monitor import MonitorBinding, ProcessLiveness


class FakeACL:
    def __init__(self):
        self.acls = {}
        self.writes = []
        self.run_identity = None
        self.before_write = lambda fd, acl: None
        self.after_write = lambda fd, acl: None

    def read_acl(self, fd):
        info = os.fstat(fd)
        return self.acls.get((info.st_dev, info.st_ino), baseline_acl(directory=stat.S_ISDIR(info.st_mode)))

    def role(self, fd):
        info = os.fstat(fd)
        return (
            "run"
            if (info.st_dev, info.st_ino) == self.run_identity
            else ("directory" if stat.S_ISDIR(info.st_mode) else "console")
        )

    def write_acl(self, fd, acl):
        self.before_write(fd, acl)
        info = os.fstat(fd)
        directory = stat.S_ISDIR(info.st_mode)
        run_directory = (info.st_dev, info.st_ino) == self.run_identity
        self.writes.append(("run" if run_directory else ("directory" if directory else "console"), acl))
        self.acls[info.st_dev, info.st_ino] = acl
        os.fchmod(
            fd,
            (0o710 if run_directory else (0o730 if directory else 0o660))
            if acl.named_users
            else (0o700 if directory else 0o600),
        )
        self.after_write(fd, acl)
        return self.read_acl(fd)


@pytest.fixture
def case(tmp_path, monkeypatch):
    name = "access-test"
    roots, store, runner, boot, profile, _, plan = fixtures._committed_oci_domain(tmp_path, name)
    raw_conn = fixtures._DefinitionConnection()
    monkeypatch.setattr(runtime.kvm, "_libvirt", lambda: fixtures._FAKE_LIBVIRT)
    runtime.define_committed_oci_root_domain(roots, name, store, boot, profile, conn=raw_conn, runner=runner)
    original = raw_conn.domains[name]
    xml = ET.fromstring(original.xml)
    ET.SubElement(xml, "uuid").text = original.domain_uuid
    original.xml = ET.tostring(xml, encoding="unicode")
    binding = runtime.prepare_oci_root_monitor_binding(
        roots, name, store, boot, profile, conn=raw_conn, boot_attempt_id=str(uuid.uuid4()), runner=runner
    )
    domain = recovery_tests._Domain(original)
    conn = recovery_tests._Connection(raw_conn, domain)
    conn.getCapabilities = lambda: (
        '<capabilities><host><secmodel><model>dac</model><doi>0</doi><baselabel type="kvm">+12345:+12346</baselabel></secmodel></host></capabilities>'
    )
    backend = FakeACL()
    run_info = (roots.runs / name).stat()
    backend.run_identity = (run_info.st_dev, run_info.st_ino)
    monkeypatch.setattr(access, "LinuxFdACLBackend", lambda: backend)
    roots.runtime_packs.mkdir(mode=0o700, exist_ok=True)
    return SimpleNamespace(
        roots=roots,
        store=store,
        runner=runner,
        boot=boot,
        profile=profile,
        plan=plan,
        binding=binding,
        conn=conn,
        domain=domain,
        backend=backend,
        paths=io.runtime_io_paths(roots.runs / name),
        state=roots.runs / name / "state.json",
    )


def _grant(case, **kwargs):
    return access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn, **kwargs)


def _revoke(case, **kwargs):
    return access.revoke_oci_runtime_access(
        case.roots,
        case.binding,
        conn=case.conn,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def _terminal_cleanup(case, monkeypatch, request):
    binding = case.binding
    lease = fixtures._committed_monitor_lease(case.roots, binding.record.name, binding, monkeypatch, request)
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
    lease.mark_terminal()
    lease.close()
    terminal = fixtures.ProcessExit(0, 0, None, fixtures.ProcessExitCategory.EXITED)
    with state.locked_existing_run(case.roots, binding.record.name) as mutation:
        data = mutation.mutable_state()
        data["oci_root_handoff"] = {
            "schema": "palimpsest.oci-root-handoff.v1",
            "boot_attempt_id": binding.boot_attempt_id,
            "domain_uuid": binding.domain_uuid,
            "domain_id": 7,
            "plan_digest": binding.plan_digest,
            "libvirt_uri": binding.libvirt_uri,
            "phase": "terminal",
            "lifecycle": fixtures._handoff_receipt(
                "terminal", terminal, boot_attempt_id=binding.boot_attempt_id
            ).to_dict(),
        }
        mutation.write_state("exited", data)
    recovery_tests.recovery.reconcile_inactive_monitor_domain(
        case.roots, binding, conn=case.conn, liveness_probe=lambda _: ProcessLiveness.STALE
    )


def test_grant_pins_exact_acl_and_preserves_original_run_evidence(case):
    before = json.loads(case.state.read_bytes())
    parent_mode = case.paths.root.parent.parent.stat().st_mode
    receipt = _grant(case)
    assert receipt.phase == "granted"
    assert [role for role, _ in case.backend.writes] == ["console", "directory", "run"]
    assert (receipt.qemu_uid, receipt.qemu_gid) == (12345, 12346)
    assert access.RuntimeAccessReceipt.from_dict(receipt.to_dict()) == receipt
    assert str(case.paths.root) not in json.dumps(receipt.to_dict())
    after = json.loads(case.state.read_bytes())
    for key, value in before.items():
        if key != "lifecycle_revision":
            assert after[key] == value
    assert case.paths.root.parent.parent.stat().st_mode == parent_mode
    assert stat.S_IMODE(case.paths.root.parent.stat().st_mode) == 0o710
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        with io.runtime_io_guard(mutation, plan_digest=case.plan.digest):
            pass
    assert _grant(case) == receipt and len(case.backend.writes) == 3


@pytest.mark.parametrize(
    "failure", ["before-console", "after-console", "before-directory", "after-directory", "before-run", "after-run"]
)
def test_partial_grant_resumes_exact_prefix_without_rollback(case, failure):
    def fail(fd, acl):
        if case.backend.role(fd) == failure.split("-")[1]:
            raise StateError("injected ACL failure")

    setattr(case.backend, "before_write" if failure.startswith("before") else "after_write", fail)
    with pytest.raises(StateError):
        _grant(case)
    intent = json.loads(case.state.read_bytes())["oci_runtime_access"]
    assert intent["phase"] == "intent"
    case.backend.before_write = case.backend.after_write = lambda *_: None
    receipt = _grant(case)
    assert receipt.phase == "granted" and receipt.access_id == intent["access_id"]


@pytest.mark.parametrize("change", ["principal", "journal", "active", "console-inode", "uri"])
def test_grant_rechecks_authority_before_second_acl_write(case, change):
    def drift(fd, acl):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            return
        if change == "principal":
            case.conn.getCapabilities = lambda: (
                '<capabilities><host><secmodel><model>dac</model><doi>0</doi><baselabel type="kvm">+12347:+12346</baselabel></secmodel></host></capabilities>'
            )
        elif change == "journal":
            monitor = case.paths.root.parent / "monitor-private"
            monitor.mkdir(mode=0o700)
            (monitor / "journal.json").touch()
        elif change == "active":
            case.domain.domain.active = 1
        elif change == "console-inode":
            case.paths.console_log.rename(case.paths.console_log.with_name("old-console"))
            case.paths.console_log.touch(mode=0o600)
        else:
            case.conn.conn.uri = "qemu:///session"

    case.backend.after_write = drift
    with pytest.raises(StateError):
        _grant(case)
    assert [role for role, _ in case.backend.writes] == ["console"]
    assert stat.S_IMODE(case.paths.root.stat().st_mode) == 0o700


def test_intent_publication_failure_prevents_acl_writes(case, monkeypatch):
    monkeypatch.setattr(
        state.ExistingRunMutation, "write_state", lambda *_: (_ for _ in ()).throw(StateError("fsync failed"))
    )
    with pytest.raises(StateError):
        _grant(case)
    assert not case.backend.writes


def test_launch_capture_uses_product_acl_verifier_without_run_lock_recursion(case, monkeypatch):
    receipt = _grant(case)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    with launch.prepare_monitor_launch_authority(
        case.roots, case.store, case.boot, case.profile, case.binding
    ) as authority:
        frame = authority.to_dict()
        assert frame["runtime_access"] == receipt.to_dict()
        assert frame["schema"].endswith(".v5")
        case.paths.console_log.write_bytes(b"untrusted output")
        monkeypatch.setattr(launch, "locked_existing_run", lambda *_args, **_kwargs: pytest.fail("recursive run lock"))
        authority.validate()
        case.backend.acls[receipt.console.device, receipt.console.inode] = receipt.console.baseline
        with pytest.raises(StateError):
            authority.validate()


@pytest.mark.parametrize("phase", ["intent", "revoking", "revoked"])
def test_non_granted_record_cannot_authorize_launch(case, phase):
    receipt = _grant(case)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        record = receipt.to_dict()
        record["phase"] = phase
        if phase != "intent":
            record["cleanup_digest"] = "sha256:" + "a" * 64
        data["oci_runtime_access"] = record
        mutation.write_state("defined", data)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        with pytest.raises(StateError):
            with io.runtime_io_guard(mutation, plan_digest=case.plan.digest):
                pass


def test_revoke_requires_completed_stale_terminal_cleanup_and_restores_reverse_order(case, monkeypatch, request):
    _grant(case)
    with pytest.raises(StateError):
        _revoke(case)
    _terminal_cleanup(case, monkeypatch, request)
    before = json.loads(case.state.read_bytes())
    journal = (case.paths.root.parent / "monitor-private" / recovery_tests.ipc._JOURNAL_NAME).read_bytes()
    receipt = _revoke(case)
    assert receipt.phase == "revoked"
    assert [role for role, _ in case.backend.writes] == ["console", "directory", "run", "run", "directory", "console"]
    assert stat.S_IMODE(case.paths.root.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(case.paths.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(case.paths.console_log.stat().st_mode) == 0o600
    after = json.loads(case.state.read_bytes())
    for key, value in before.items():
        if key not in {"lifecycle_revision", "oci_runtime_access"}:
            assert after[key] == value
    assert (case.paths.root.parent / "monitor-private" / recovery_tests.ipc._JOURNAL_NAME).read_bytes() == journal
    assert _revoke(case) == receipt and len(case.backend.writes) == 6


@pytest.mark.parametrize("failure", ["after-run", "after-directory", "after-console"])
def test_partial_revocation_resumes_original_receipt(case, monkeypatch, request, failure):
    _grant(case)
    _terminal_cleanup(case, monkeypatch, request)

    def fail(fd, acl):
        if case.backend.role(fd) == failure.split("-")[1]:
            raise StateError("ambiguous revoke")

    case.backend.after_write = fail
    with pytest.raises(StateError):
        _revoke(case)
    intent = json.loads(case.state.read_bytes())["oci_runtime_access"]
    assert intent["phase"] == "revoking"
    case.backend.after_write = lambda *_: None
    assert _revoke(case).access_id == intent["access_id"]


@pytest.mark.parametrize("liveness", [ProcessLiveness.LIVE, ProcessLiveness.UNKNOWN, None])
def test_nonstale_writer_refuses_restore_without_acl_mutation(case, monkeypatch, request, liveness):
    _grant(case)
    _terminal_cleanup(case, monkeypatch, request)
    before = case.state.read_bytes()
    with pytest.raises(StateError):
        _revoke(case, liveness_probe=lambda _: liveness)
    assert len(case.backend.writes) == 3 and case.state.read_bytes() == before


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("target", ["run", "directory", "console"])
def test_successful_acl_write_failed_fsync_resume_syncs_all_three_inodes(case, monkeypatch, request, operation, target):
    if operation == "revoke":
        _grant(case)
        _terminal_cleanup(case, monkeypatch, request)
    selected = (
        case.paths.root.parent
        if target == "run"
        else (case.paths.root if target == "directory" else case.paths.console_log)
    )
    target_inode = selected.stat().st_ino
    original = os.fsync

    def fail(fd):
        desired_written = any(
            role == target and bool(acl.named_users) == (operation == "grant") for role, acl in case.backend.writes
        )
        if os.fstat(fd).st_ino == target_inode and desired_written:
            raise OSError("injected ACL inode fsync failure")
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(StateError):
            (_grant if operation == "grant" else _revoke)(case)
    phase = json.loads(case.state.read_bytes())["oci_runtime_access"]["phase"]
    assert phase == ("intent" if operation == "grant" else "revoking")
    synced = []

    def observe(fd):
        synced.append(os.fstat(fd).st_ino)
        return original(fd)

    monkeypatch.setattr(os, "fsync", observe)
    assert (_grant if operation == "grant" else _revoke)(case).phase == (
        "granted" if operation == "grant" else "revoked"
    )
    assert {
        case.paths.root.parent.stat().st_ino,
        case.paths.root.stat().st_ino,
        case.paths.console_log.stat().st_ino,
    } <= set(synced)


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("change", ["inode", "domain"])
def test_acl_read_time_authority_swap_stops_before_next_write(case, monkeypatch, request, operation, change):
    if operation == "revoke":
        _grant(case)
        _terminal_cleanup(case, monkeypatch, request)
    writes_before = len(case.backend.writes)
    original = case.backend.read_acl
    changed = False

    def read(fd):
        nonlocal changed
        result = original(fd)
        durable = json.loads(case.state.read_bytes()).get("oci_runtime_access", {})
        if not changed and durable.get("phase") == ("intent" if operation == "grant" else "revoking"):
            changed = True
            if change == "inode":
                case.paths.console_log.rename(case.paths.console_log.with_name("original-console"))
                case.paths.console_log.touch(mode=0o600)
            elif operation == "grant":
                case.domain.domain.active = 1
            else:
                case.conn.conn.domains[case.binding.record.name] = case.domain.domain
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    with pytest.raises(StateError):
        (_grant if operation == "grant" else _revoke)(case)
    assert changed and len(case.backend.writes) == writes_before


def test_null_ledger_access_is_malformed_not_fresh_authority(case):
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        data["oci_runtime_access"] = None
        mutation.write_state("defined", data)
    before = case.state.read_bytes()
    with pytest.raises(StateError):
        _grant(case)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        with pytest.raises(StateError):
            with io.runtime_io_guard(mutation, plan_digest=case.plan.digest):
                pass
    assert case.state.read_bytes() == before and not case.backend.writes


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_changed_durable_intent_member_is_not_overwritten(case, monkeypatch, request, operation):
    if operation == "revoke":
        _grant(case)
        _terminal_cleanup(case, monkeypatch, request)
    before = len(case.backend.writes)
    original = state.ExistingRunMutation.write_state
    injected = False

    def write(self, status, data):
        nonlocal injected
        result = original(self, status, data)
        if not injected:
            injected = True
            altered = self.mutable_state()
            altered["oci_runtime_access"]["access_id"] = str(uuid.uuid4())
            original(self, status, altered)
        return result

    monkeypatch.setattr(state.ExistingRunMutation, "write_state", write)
    with pytest.raises(StateError):
        (_grant if operation == "grant" else _revoke)(case)
    assert injected and len(case.backend.writes) == before


def test_revocation_rechecks_original_cleanup_member_before_mutating(case, monkeypatch, request):
    _grant(case)
    _terminal_cleanup(case, monkeypatch, request)
    original = state.ExistingRunMutation.write_state

    def write(self, status, data):
        result = original(self, status, data)
        changed = self.mutable_state()
        changed["oci_monitor_inactive_cleanup"]["cleanup_id"] = str(uuid.uuid4())
        original(self, status, changed)
        return result

    monkeypatch.setattr(state.ExistingRunMutation, "write_state", write)
    with pytest.raises(StateError):
        _revoke(case)
    assert len(case.backend.writes) == 3


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_completed_replay_never_writes_acl_fsync_or_state(case, monkeypatch, request, operation):
    receipt = _grant(case)
    if operation == "revoke":
        _terminal_cleanup(case, monkeypatch, request)
        receipt = _revoke(case)

    def forbidden(*_args, **_kwargs):
        pytest.fail("completed access replay must be verification-only")

    original_fsync = os.fsync
    targets = {
        (receipt.run.device, receipt.run.inode),
        (receipt.directory.device, receipt.directory.inode),
        (receipt.console.device, receipt.console.inode),
    }

    def sync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in targets:
            forbidden()
        return original_fsync(fd)  # Existing run-lock acquisition has its own durable lock setup.

    monkeypatch.setattr(case.backend, "write_acl", forbidden)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(state.ExistingRunMutation, "write_state", forbidden)
    assert (_grant if operation == "grant" else _revoke)(case) == receipt


def test_access_pinned_holder_invalidates_fork_descriptors_without_closing_reused_fd(case):
    reused = -1
    try:
        with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
            receipt = io.RuntimeIOReceipt.from_dict(mutation.snapshot.state["oci_runtime_io"])
            with access._pinned_io(mutation, receipt) as descriptors:
                reused = descriptors.directory
                io._FORK_LOCK.acquire()
                io._close_inherited_descriptors()
                assert descriptors.directory == descriptors.console == -1
                fresh = os.open(os.devnull, os.O_RDONLY)
                if fresh != reused:
                    os.dup2(fresh, reused)
                    os.close(fresh)
            os.fstat(reused)
    finally:
        if reused >= 0:
            os.close(reused)


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("bits", ["000", "001", "010", "011", "100", "101", "110", "111"])
def test_three_target_resume_accepts_only_exact_ordered_prefixes(case, monkeypatch, request, operation, bits):
    receipt = _grant(case)
    if operation == "revoke":
        _terminal_cleanup(case, monkeypatch, request)
        receipt = _revoke(case)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        data["oci_runtime_access"]["phase"] = "intent" if operation == "grant" else "revoking"
        mutation.write_state(data["status"], data)
        for bit, target, path in zip(
            bits,
            (receipt.run, receipt.directory, receipt.console),
            (case.paths.root.parent, case.paths.root, case.paths.console_log),
            strict=True,
        ):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                case.backend.write_acl(fd, target.granted if bit == "1" else target.baseline)
            finally:
                os.close(fd)
    case.backend.writes.clear()
    if bits in {"000", "001", "011", "111"}:
        assert (_grant if operation == "grant" else _revoke)(case).phase == (
            "granted" if operation == "grant" else "revoked"
        )
    else:
        before = case.state.read_bytes()
        with pytest.raises(StateError):
            (_grant if operation == "grant" else _revoke)(case)
        assert not case.backend.writes and case.state.read_bytes() == before


def test_run_link_count_changes_for_owner_monitor_directory_not_for_identity(case):
    receipt = _grant(case)
    before = case.paths.root.parent.stat().st_nlink
    monitor = case.paths.root.parent / "monitor-private"
    monitor.mkdir(mode=0o700)
    assert case.paths.root.parent.stat().st_nlink == before + 1
    assert _grant(case) == receipt
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        with io.runtime_io_guard(mutation, plan_digest=case.plan.digest):
            pass


@pytest.mark.parametrize("mode", [0o710, 0o755, 0o770])
def test_fresh_run_traversal_refuses_nonprivate_baseline_without_adoption(case, mode):
    case.paths.root.parent.chmod(mode)
    before = case.state.read_bytes()
    with pytest.raises(StateError):
        _grant(case)
    assert case.state.read_bytes() == before and not case.backend.writes


def test_access_v1_is_not_reinterpreted_as_a_three_target_receipt(case):
    receipt = _grant(case)
    legacy = receipt.to_dict()
    legacy["schema"] = "palimpsest.oci-runtime-access.v1"
    legacy.pop("run")
    with pytest.raises(StateError):
        access.RuntimeAccessReceipt.from_dict(legacy)


@pytest.mark.parametrize("field", ["inode", "uid", "gid", "granted"])
def test_changed_run_target_receipt_cannot_authorize_runtime(case, field):
    receipt = _grant(case)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        target = data["oci_runtime_access"]["run"]
        if field == "granted":
            target[field] = receipt.directory.granted.to_dict()  # -wx is forbidden for trusted run traversal.
        else:
            target[field] += 1
        mutation.write_state("defined", data)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        with pytest.raises(StateError):
            with io.runtime_io_guard(mutation, plan_digest=case.plan.digest):
                pass
