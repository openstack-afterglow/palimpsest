"""Shared permissions belong to an exact member set, not the first VM."""

import copy
import json
import os
import stat
import threading
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_oci_monitor_recovery as recovery_tests
import test_oci_runtime_access as access_tests
import test_oci_store as fixtures

from palimpsest_local import oci_runtime_access as access
from palimpsest_local import oci_shared_traversal as shared
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor import ProcessLiveness


@pytest.fixture
def case(tmp_path, monkeypatch):
    value = access_tests.case.__wrapped__(tmp_path, monkeypatch)
    backend = value.backend
    original = backend.write_acl
    namespace_ids = {
        (path.stat().st_dev, path.stat().st_ino): role
        for role, path in (("state", value.roots.state), ("runs", value.roots.runs))
    }

    def write(fd, acl):
        info = os.fstat(fd)
        role = namespace_ids.get((info.st_dev, info.st_ino))
        if role is not None or (acl.named_users and acl.named_users[0][1] == "--x"):
            backend.before_write(fd, acl)
            backend.acls[info.st_dev, info.st_ino] = acl
            backend.writes.append((role or "run", acl))
            os.fchmod(fd, 0o710 if acl.named_users else 0o700)
            backend.after_write(fd, acl)
            return backend.read_acl(fd)
        return original(fd, acl)

    monkeypatch.setattr(backend, "write_acl", write)
    monkeypatch.setattr(shared, "LinuxFdACLBackend", lambda: backend)
    value.namespace_ids = namespace_ids
    access_tests._grant(value)
    backend.writes.clear()
    return value


def _join(case):
    return shared.join_oci_shared_traversal(case.roots, case.binding, conn=case.conn, acl_backend=case.backend)


def _leave(case):
    return shared.leave_oci_shared_traversal(
        case.roots,
        case.binding,
        conn=case.conn,
        acl_backend=case.backend,
        liveness_probe=lambda _: ProcessLiveness.STALE,
    )


def _finish(case, monkeypatch, request):
    access_tests._terminal_cleanup(case, monkeypatch, request)
    access_tests._revoke(case)
    case.backend.writes.clear()


def _second(case):
    name = "shared-second"
    with state.reserve_new_run(case.roots, name, fixtures._oci_dispatch()) as reservation:
        prepared = fixtures.prepare_oci_root_run(
            reservation,
            fixtures._image_materialization(case.store),
            case.store,
            root_volume_size_bytes=fixtures._ROOT_VOLUME_SIZE,
            runner=case.runner,
        )
    preview = fixtures.build_oci_root_domain_plan(
        case.roots, prepared, case.store, case.boot, case.profile, runner=case.runner
    )
    plan = fixtures.commit_oci_root_domain_plan(case.roots, preview, case.store, runner=case.runner)
    raw = fixtures._DefinitionConnection()
    access_tests.runtime.define_committed_oci_root_domain(
        case.roots, name, case.store, case.boot, case.profile, conn=raw, runner=case.runner
    )
    original = raw.domains[name]
    xml = ET.fromstring(original.xml)
    ET.SubElement(xml, "uuid").text = original.domain_uuid
    original.xml = ET.tostring(xml, encoding="unicode")
    binding = access_tests.runtime.prepare_oci_root_monitor_binding(
        case.roots,
        name,
        case.store,
        case.boot,
        case.profile,
        conn=raw,
        boot_attempt_id=str(uuid.uuid4()),
        runner=case.runner,
    )
    domain = recovery_tests._Domain(original)
    conn = recovery_tests._Connection(raw, domain)
    conn.getCapabilities = case.conn.getCapabilities
    value = SimpleNamespace(
        **{
            **vars(case),
            "binding": binding,
            "conn": conn,
            "domain": domain,
            "plan": plan,
            "state": case.roots.runs / name / "state.json",
            "paths": access_tests.io.runtime_io_paths(case.roots.runs / name),
        }
    )
    access_tests._grant(value)
    return value


def _verify(case, member):
    return shared.verify_shared_traversal(
        case.roots,
        member,
        binding=case.binding,
        access=state.read_run_ledger_snapshot(case.roots, case.binding.record.name).state["oci_runtime_access"],
        acl_backend=case.backend,
    )


def test_first_join_and_final_leave_preserve_evidence_and_order(case, monkeypatch, request):
    before = json.loads(case.state.read_bytes())
    member = _join(case)
    assert member.phase == "active"
    assert [role for role, _ in case.backend.writes] == ["runs", "state"]
    assert _verify(case, member) == member
    after = json.loads(case.state.read_bytes())
    for key, value in before.items():
        if key != "lifecycle_revision":
            assert after[key] == value
    _finish(case, monkeypatch, request)
    member = _leave(case)
    assert member.phase == "left"
    assert [role for role, _ in case.backend.writes] == ["state", "runs"]
    registry = json.loads((case.roots.locks / shared._REGISTRY).read_bytes())
    assert registry["pending"] is None and not shared._active(registry)
    assert registry["members"] == {}
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (case.roots.state, case.roots.runs))


def test_two_runs_nonfinal_leave_never_changes_ancestor_acl_or_fsync(case, monkeypatch, request):
    first = _join(case)
    second = _second(case)
    case.backend.writes.clear()
    second_member = _join(second)
    assert case.backend.writes == []
    assert _verify(case, first) == first
    assert _verify(second, second_member) == second_member
    _finish(case, monkeypatch, request)
    fsync = os.fsync

    def no_ancestor_sync(fd):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) not in case.namespace_ids
        return fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", no_ancestor_sync)
        assert _leave(case).phase == "left"
    assert case.backend.writes == []
    assert _verify(second, second_member) == second_member
    _finish(second, monkeypatch, request)
    assert _leave(second).phase == "left"
    assert [role for role, _ in case.backend.writes] == ["state", "runs"]


@pytest.mark.parametrize("operation", ["join", "leave"])
@pytest.mark.parametrize("role", ["state", "runs"])
def test_failed_acl_or_fsync_resumes_exact_owned_intent(case, monkeypatch, request, operation, role):
    if operation == "leave":
        _join(case)
        _finish(case, monkeypatch, request)
    target = getattr(case.roots, role).stat()
    fsync = os.fsync
    fired = False

    def fail(fd):
        nonlocal fired
        info = os.fstat(fd)
        if not fired and (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            fired = True
            raise OSError("injected target fsync failure")
        return fsync(fd)

    run = _join if operation == "join" else _leave
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(StateError):
            run(case)
    assert fired
    assert run(case).phase == ("active" if operation == "join" else "left")


@pytest.mark.parametrize("operation", ["join", "leave"])
def test_completed_replay_is_read_only(case, monkeypatch, request, operation):
    member = _join(case)
    if operation == "leave":
        _finish(case, monkeypatch, request)
        member = _leave(case)
    before = case.state.read_bytes(), (case.roots.locks / shared._REGISTRY).read_bytes()
    fsync = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) not in case.namespace_ids
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(case.backend, "write_acl", lambda *_: pytest.fail("replay changed ACL"))
    monkeypatch.setattr(state.ExistingRunMutation, "write_state", lambda *_: pytest.fail("replay changed ledger"))
    monkeypatch.setattr(shared._Namespace, "write", lambda *_: pytest.fail("replay changed registry"))
    assert (_join if operation == "join" else _leave)(case) == member
    assert before == (case.state.read_bytes(), (case.roots.locks / shared._REGISTRY).read_bytes())


def test_unregistered_granted_run_cannot_use_managed_namespace(case):
    _join(case)
    second = _second(case)
    with pytest.raises(StateError):
        _verify(second, None)
    with state.locked_existing_run(second.roots, second.binding.record.name) as mutation:
        with pytest.raises(StateError):
            with access_tests.io.runtime_io_guard(mutation, plan_digest=second.plan.digest):
                pytest.fail("unregistered runtime admitted")


@pytest.mark.parametrize("role", ["state", "runs"])
def test_inode_swap_after_acl_read_refuses_without_mutation(case, monkeypatch, role):
    original = case.backend.read_acl
    fired = False
    path = getattr(case.roots, role)

    def read(fd):
        nonlocal fired
        result = original(fd)
        if not fired and (case.roots.locks / shared._REGISTRY).exists():
            fired = True
            path.rename(path.with_name(path.name + "-original"))
            path.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    with pytest.raises(StateError):
        _join(case)
    assert fired and case.backend.writes == []


def test_left_membership_never_rejoins_or_authorizes_launch(case, monkeypatch, request):
    member = _join(case)
    _finish(case, monkeypatch, request)
    left = _leave(case)
    with pytest.raises(StateError):
        _verify(case, member)
    with pytest.raises(StateError):
        _join(case)
    assert left.phase == "left"


def test_membership_parser_rejects_unknown_or_old_fields(case):
    member = _join(case)
    for key, value in (("schema", "v0"), ("epoch", True), ("phase", "unknown"), ("extra", 1)):
        raw = {**member.to_dict(), key: value}
        with pytest.raises(StateError):
            shared.SharedTraversalMembership.from_dict(raw)
    frozen = state.read_run_ledger_snapshot(case.roots, case.binding.record.name).state[
        shared.SHARED_TRAVERSAL_STATE_KEY
    ]
    assert shared.SharedTraversalMembership.from_dict(frozen) == member
    assert replace(member, phase="joining") != member


@pytest.mark.parametrize("phase", ["joining", "active", "leaving", "left"])
def test_durable_registry_ahead_of_run_ledger_is_resumable(case, monkeypatch, request, phase):
    operation = _join
    if phase in {"leaving", "left"}:
        _join(case)
        _finish(case, monkeypatch, request)
        operation = _leave
    write = state.ExistingRunMutation.write_state

    def fail(mutation, status, data):
        if data.get(shared.SHARED_TRAVERSAL_STATE_KEY, {}).get("phase") == phase:
            raise OSError("injected ledger publication failure")
        return write(mutation, status, data)

    with monkeypatch.context() as patch:
        patch.setattr(state.ExistingRunMutation, "write_state", fail)
        with pytest.raises(StateError):
            operation(case)
    assert operation(case).phase == ("active" if operation is _join else "left")


@pytest.mark.parametrize("operation", ["join", "leave"])
@pytest.mark.parametrize("bits", [(False, False), (False, True), (True, False), (True, True)])
def test_partial_registry_accepts_only_exact_acl_prefixes(case, monkeypatch, request, operation, bits):
    _join(case)
    if operation == "leave":
        _finish(case, monkeypatch, request)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        member = shared.SharedTraversalMembership.from_dict(mutation.snapshot.state[shared.SHARED_TRAVERSAL_STATE_KEY])
        pending = replace(member, phase="joining" if operation == "join" else "leaving")
        shared._write_member(mutation, pending)
    ns = shared._Namespace(case.roots, locked=True)
    try:
        registry = json.loads(ns.content)
        if operation == "join":
            registry["members"] = {}
        registry["pending"] = pending.to_dict()
        ns.write(registry)
        for role, granted in zip(("state", "runs"), bits, strict=True):
            target = getattr(member, role)
            case.backend.write_acl(ns.fds[role], target.granted if granted else target.baseline)
    finally:
        ns.close()
    case.backend.writes.clear()
    if bits == (True, False):
        with pytest.raises(StateError):
            (_join if operation == "join" else _leave)(case)
        assert case.backend.writes == []
    else:
        assert (_join if operation == "join" else _leave)(case).phase == ("active" if operation == "join" else "left")


def test_concurrent_duplicate_first_join_is_one_membership_and_two_acl_writes(case):
    start = threading.Barrier(2)

    def join():
        start.wait(timeout=5)
        return _join(case)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: join(), range(2)))
    assert first == second
    assert [role for role, _ in case.backend.writes] == ["runs", "state"]
    registry = json.loads((case.roots.locks / shared._REGISTRY).read_bytes())
    assert len(registry["members"]) == 1


def test_final_leave_and_new_join_serialize_and_advance_empty_epoch(case, monkeypatch, request):
    first = _join(case)
    second = _second(case)
    _finish(case, monkeypatch, request)
    restoring = threading.Event()
    attempted = threading.Event()
    original = case.backend.before_write

    def pause(fd, acl):
        info = os.fstat(fd)
        if case.namespace_ids.get((info.st_dev, info.st_ino)) == "state" and not acl.named_users:
            restoring.set()
            assert attempted.wait(5)
        original(fd, acl)

    monkeypatch.setattr(case.backend, "before_write", pause)

    def join():
        assert restoring.wait(5)
        attempted.set()
        return _join(second)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(join)
        assert _leave(case).phase == "left"
        member = future.result(timeout=5)
    assert member.epoch == first.epoch + 1
    assert _verify(second, member) == member
    assert [role for role, _ in case.backend.writes] == ["state", "runs", "runs", "state"]


def test_initial_enable_refuses_another_unregistered_active_ledger(case):
    _second(case)
    case.backend.writes.clear()
    with pytest.raises(StateError):
        _join(case)
    assert not (case.roots.locks / shared._REGISTRY).exists()
    assert case.backend.writes == []


@pytest.mark.parametrize("role", ["state", "runs"])
def test_exact_full_acl_is_checked_even_when_mode_is_unchanged(case, role):
    member = _join(case)
    path = getattr(case.roots, role)
    info = path.stat()
    target = getattr(member, role)
    case.backend.acls[info.st_dev, info.st_ino] = replace(target.granted, named_users=((23456, "--x"),))
    with pytest.raises(StateError):
        _verify(case, member)
    assert stat.S_IMODE(path.stat().st_mode) == 0o710


def test_missing_managed_global_lock_is_not_recreated(case):
    _join(case)
    lock = case.roots.locks / shared._LOCK
    lock.unlink()
    with pytest.raises(StateError):
        _join(case)
    assert not lock.exists()


def test_namespace_holder_fork_invalidation_does_not_close_reused_fd(case):
    ns = shared._Namespace(case.roots, locked=True)
    old = ns.fds["lock"]
    shared._FORK_LOCK.acquire()
    shared._fork_child()
    with pytest.raises(StateError):
        ns.verify()
    replacement = os.open(os.devnull, os.O_RDONLY)
    try:
        if replacement != old:
            os.dup2(replacement, old)
        ns.close()
        os.fstat(old)
    finally:
        os.close(old)
        if replacement != old:
            os.close(replacement)


@pytest.mark.parametrize("operation", ["join", "leave"])
@pytest.mark.parametrize("role", ["console", "io", "run"])
def test_completed_replay_rejects_private_inode_swap_during_third_uri_call(case, monkeypatch, request, operation, role):
    _join(case)
    if operation == "leave":
        _finish(case, monkeypatch, request)
        _leave(case)
    case.backend.writes.clear()
    path = {"console": case.paths.console_log, "io": case.paths.root, "run": case.paths.root.parent}[role]
    saved = path.with_name(path.name + "-original")
    original_info = path.stat()
    before_registry = (case.roots.locks / shared._REGISTRY).read_bytes()
    uri = case.conn.getURI
    calls = 0

    def change():
        nonlocal calls
        calls += 1
        if calls == 3:
            path.rename(saved)
            if role == "console":
                path.write_bytes(b"replacement remains untouched")
                path.chmod(0o660 if operation == "join" else 0o600)
            else:
                path.mkdir(mode=stat.S_IMODE(original_info.st_mode))
        return uri()

    monkeypatch.setattr(case.conn, "getURI", change)
    with pytest.raises(StateError):
        (_join if operation == "join" else _leave)(case)
    assert calls >= 3
    assert saved.stat().st_ino == original_info.st_ino
    assert path.stat().st_ino != original_info.st_ino
    assert case.backend.writes == []
    assert (case.roots.locks / shared._REGISTRY).read_bytes() == before_registry


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("missing", [shared._REGISTRY, shared._MARKER])
def test_enrolled_evidence_disappearance_is_never_unmanaged_repair(case, monkeypatch, request, empty, missing):
    member = _join(case)
    if empty:
        _finish(case, monkeypatch, request)
        _leave(case)
    (case.roots.locks / missing).unlink()
    before = [path.stat() for path in (case.roots.state, case.roots.runs)]
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    with pytest.raises(StateError):
        shared.verify_shared_traversal(case.roots, member, acl_backend=case.backend)
    assert [(x.st_ino, x.st_mode) for x in before] == [
        (path.stat().st_ino, path.stat().st_mode) for path in (case.roots.state, case.roots.runs)
    ]
    assert not (case.roots.locks / missing).exists()


def test_marker_only_crash_is_recoverable_only_by_original_explicit_join(case, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(shared._Namespace, "write", lambda *_: (_ for _ in ()).throw(OSError("registry fsync")))
        with pytest.raises(StateError):
            _join(case)
    marker = (case.roots.locks / shared._MARKER).read_bytes()
    assert not (case.roots.locks / shared._REGISTRY).exists()
    assert case.backend.writes == []
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    with pytest.raises(StateError):
        shared.verify_shared_traversal(case.roots, None, acl_backend=case.backend)
    wrong = replace(case.binding, boot_attempt_id=str(uuid.uuid4()))
    with pytest.raises(StateError):
        shared.join_oci_shared_traversal(case.roots, wrong, conn=case.conn, acl_backend=case.backend)
    assert not (case.roots.locks / shared._REGISTRY).exists()
    assert _join(case).phase == "active"
    assert (case.roots.locks / shared._MARKER).read_bytes() == marker


def test_crash_after_run_left_retains_only_own_tombstone_and_replay_is_read_only(case, monkeypatch, request):
    member = _join(case)
    _finish(case, monkeypatch, request)
    write = shared._Namespace.write

    def fail(ns, value):
        if shared._key(member) not in value["members"]:
            raise OSError("final compaction publication failure")
        return write(ns, value)

    with monkeypatch.context() as patch:
        patch.setattr(shared._Namespace, "write", fail)
        with pytest.raises(StateError):
            _leave(case)
    before = (case.roots.locks / shared._REGISTRY).read_bytes()
    assert json.loads(before)["members"][shared._key(member)]["phase"] == "left"
    monkeypatch.setattr(shared._Namespace, "write", lambda *_: pytest.fail("completed replay wrote registry"))
    assert _leave(case).phase == "left"
    assert (case.roots.locks / shared._REGISTRY).read_bytes() == before


def test_registry_size_preflight_never_opens_or_publishes_unreadable_temp(case, monkeypatch):
    member = _join(case)
    ns = shared._Namespace(case.roots, locked=True)
    try:
        oversized = copy.deepcopy(ns.registry)
        for _ in range(1000):
            left = replace(member, access_id=str(uuid.uuid4()), phase="left")
            oversized["members"][shared._key(left)] = left.to_dict()
        assert len(shared.canonical_json_bytes(oversized)) > shared._MAX_REGISTRY_BYTES
        before = ns.content
        monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: pytest.fail("oversized write opened a temp"))
        # Verification reads are allowed before the size fence; isolate the
        # publication boundary to assert no temp descriptor is created.
        monkeypatch.setattr(ns, "verify", lambda: None)
        with pytest.raises(StateError):
            ns.write(oversized)
        assert ns.content == before
    finally:
        ns.close()


@pytest.mark.parametrize("reserve", ["completion", "future-leave"])
def test_join_reserves_completed_registry_capacity_before_pending_intent(case, monkeypatch, reserve):
    _join(case)
    second = _second(case)
    receipt = access.RuntimeAccessReceipt.from_dict(
        state.read_run_ledger_snapshot(second.roots, second.binding.record.name).state["oci_runtime_access"]
    )
    raw = json.loads((case.roots.locks / shared._REGISTRY).read_bytes())
    candidate = shared.SharedTraversalMembership(
        raw["namespace_id"],
        raw["epoch"],
        receipt.access_id,
        second.binding,
        access.RuntimeAccessTarget.from_dict(raw["state"]),
        access.RuntimeAccessTarget.from_dict(raw["runs"]),
        receipt.qemu_uid,
        receipt.qemu_gid,
        "joining",
    )
    pending = copy.deepcopy(raw)
    pending["pending"] = candidate.to_dict()
    completed = copy.deepcopy(raw)
    completed["members"][shared._key(candidate)] = replace(candidate, phase="active").to_dict()
    cap = len(shared.canonical_json_bytes(completed)) + (-1 if reserve == "completion" else 1)
    assert len(shared.canonical_json_bytes(pending)) <= cap
    if reserve == "future-leave":
        leaving = copy.deepcopy(completed)
        leaving["pending"] = replace(candidate, phase="leaving").to_dict()
        assert len(shared.canonical_json_bytes(completed)) <= cap < len(shared.canonical_json_bytes(leaving))
    before = second.state.read_bytes(), (case.roots.locks / shared._REGISTRY).read_bytes()
    case.backend.writes.clear()
    monkeypatch.setattr(shared, "_MAX_REGISTRY_BYTES", cap)
    with pytest.raises(StateError):
        _join(second)
    assert before == (second.state.read_bytes(), (case.roots.locks / shared._REGISTRY).read_bytes())
    assert case.backend.writes == []


def test_registry_and_marker_reads_ignore_access_time_metadata(case):
    _join(case)
    ns = shared._Namespace(case.roots, locked=True)
    try:
        for name in (shared._REGISTRY, shared._MARKER):
            info = (case.roots.locks / name).stat()
            changed = SimpleNamespace(
                **{
                    key: getattr(info, key)
                    for key in (
                        "st_dev",
                        "st_ino",
                        "st_uid",
                        "st_gid",
                        "st_mode",
                        "st_nlink",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                },
                st_atime_ns=info.st_atime_ns + 1000000000,
            )
            assert shared._file_signature(info) == shared._file_signature(changed)
        # Real FD reads, including Linux relatime, must preserve the CAS snapshot.
        ns.verify()
        ns.verify()
    finally:
        ns.close()
