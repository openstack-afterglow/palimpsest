"""Atomic old-lower-pin retirement under exact successor protection."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_oci_store as fixtures

from palimpsest_local.artifact_store import ArtifactStore
from palimpsest_local.oci_boot_plan import prepare_oci_boot_plan
from palimpsest_local.oci_store import OCIStoreError


@pytest.fixture
def case(tmp_path):
    roots, store = fixtures._store(tmp_path)
    materialization = fixtures._image_materialization(store)
    sets = [
        prepare_oci_boot_plan(materialization, run_id=str(uuid.uuid4()), run_name=name, store=store).lower_leases
        for name in ("old", "next")
    ]
    return SimpleNamespace(roots=roots, store=store, source=sets[0], successor=sets[1])


def _run(case, *, resume=False, verify=lambda: None):
    case.store._retire_replaced_lease_set(case.source, case.successor, resume=resume, verify=verify)


def _member(case, member):
    return case.roots.oci_derived_store / "leases" / member.lease_id


def _intent(case, lease_set):
    return case.roots.oci_derived_store / "lease-sets" / lease_set.lease_set_id.split(":")[1]


def _rewrite(path, change):
    value = json.loads(path.read_bytes())
    change(value)
    path.chmod(0o600)
    path.write_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    path.chmod(0o400)


def _load(case, lease_set):
    return case.store.load_lease_set(lease_set.lease_set_id, lease_set.owner, plan_digest=lease_set.plan_digest)


def test_retirement_preserves_successor_blobs_and_occurrences(case):
    paths = [
        *list((case.roots.oci_derived_store / "occurrences").iterdir()),
        *list((case.roots.oci_derived_store / "records").iterdir()),
        *list((case.roots.store / "blobs" / "sha256").iterdir()),
        _intent(case, case.successor),
        *(_member(case, member) for member in case.successor.members),
    ]
    before = {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
    _run(case)
    case.store._require_lease_set_retired(case.source)
    assert _load(case, case.successor) == case.successor
    assert {path: (path.stat().st_ino, path.read_bytes()) for path in paths} == before
    for member in case.source.members:
        with pytest.raises(OCIStoreError):
            ArtifactStore(case.roots.store).delete_blob(
                member.receipt.image_digest,
                retention_guard=lambda digest=member.receipt.image_digest: case.store.assert_artifact_unleased(digest),
            )


@pytest.mark.parametrize("missing", ["member", "intent"])
def test_missing_successor_fails_before_any_old_pin_removal(case, missing):
    (_member(case, case.successor.members[0]) if missing == "member" else _intent(case, case.successor)).unlink()
    with pytest.raises(OCIStoreError):
        _run(case)
    assert _load(case, case.source) == case.source


@pytest.mark.parametrize(
    "side,field",
    [("source", "acquired_ns"), ("successor", "acquired_ns"), ("source", "receipt"), ("successor", "receipt")],
)
def test_changed_acquisition_snapshot_is_not_retired(case, side, field):
    target = getattr(case, side)
    path = _member(case, target.members[0])
    original = path.read_bytes()
    _rewrite(path, lambda value: value.__setitem__(field, value[field] + 1 if field == "acquired_ns" else {}))
    with pytest.raises(OCIStoreError):
        _run(case, resume=True)
    assert _intent(case, case.source).exists()
    assert all(_member(case, member).exists() for member in case.source.members)
    assert path.read_bytes() != original


def test_successor_is_rechecked_after_callback_before_unlink(case):
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 2:
            _rewrite(
                _member(case, case.successor.members[0]),
                lambda value: value.__setitem__("acquired_ns", value["acquired_ns"] + 1),
            )

    with pytest.raises(OCIStoreError):
        _run(case, verify=verify)
    assert _load(case, case.source) == case.source


def test_unexplained_future_source_member_disappearance_stops_before_next_unlink(case):
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 2:
            _member(case, case.source.members[-1]).unlink()

    with pytest.raises(OCIStoreError, match="source pins changed"):
        _run(case, verify=verify)
    assert _member(case, case.source.members[0]).exists()
    assert _intent(case, case.source).exists()
    assert _load(case, case.successor) == case.successor


def test_unexplained_source_intent_disappearance_is_not_adopted(case):
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 5:
            _intent(case, case.source).unlink()

    with pytest.raises(OCIStoreError, match="source pins changed"):
        _run(case, verify=verify)
    assert _load(case, case.successor) == case.successor


@pytest.mark.parametrize("boundary", [1, 2, 3, 5, 6])
def test_callback_failure_preserves_successor_and_allows_exact_resume(case, boundary):
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise RuntimeError("authority revoked")

    with pytest.raises(RuntimeError, match="authority revoked"):
        _run(case, verify=verify)
    assert _load(case, case.successor) == case.successor
    _run(case, resume=True)
    case.store._require_lease_set_retired(case.source)


@pytest.mark.parametrize("boundary", ["first-member", "last-member", "intent"])
def test_crash_after_unlink_resumes_without_renewing_or_dropping_successor(case, monkeypatch, boundary):
    target = {
        "first-member": case.source.members[0].lease_id,
        "last-member": case.source.members[-1].lease_id,
        "intent": case.source.lease_set_id.split(":")[1],
    }[boundary]
    unlink = os.unlink

    def crash(name, **kwargs):
        unlink(name, **kwargs)
        if name == target:
            raise OSError("simulated crash after unlink")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "unlink", crash)
        with pytest.raises(OCIStoreError):
            _run(case)
    assert _load(case, case.successor) == case.successor
    with pytest.raises(OCIStoreError):
        _run(case)
    _run(case, resume=True)
    assert _load(case, case.successor) == case.successor
    case.store._require_lease_set_retired(case.source)


def test_missing_source_intent_with_remaining_members_is_not_a_valid_resume(case):
    _intent(case, case.source).unlink()
    with pytest.raises(OCIStoreError):
        _run(case, resume=True)
    assert all(_member(case, member).exists() for member in case.source.members)


def test_reacquired_source_and_historical_replay_are_never_deleted(case):
    _run(case)
    case.store._clock = lambda: max(member.acquired_ns for member in case.source.members) + 100
    reacquired = case.store.acquire_lease_set(
        tuple(member.receipt for member in case.source.members), case.source.owner, plan_digest=case.source.plan_digest
    )
    assert reacquired != case.source
    with pytest.raises(OCIStoreError):
        _run(case, resume=True)
    with pytest.raises(OCIStoreError):
        case.store._require_lease_set_retired(case.source)
    assert _load(case, reacquired) == reacquired


def test_historical_retirement_proof_does_not_require_old_successor(case):
    _run(case)
    case.store.release_lease_set(case.successor)
    case.store._require_lease_set_retired(case.source)


@pytest.mark.parametrize("side", ["source", "successor"])
def test_handoff_waits_for_either_sets_active_reader(case, side):
    selected = getattr(case, side)
    member = selected.members[0]
    reader = case.store.resume_lease(member.lease_id, selected.owner, member.receipt)
    entered, finished = threading.Event(), threading.Event()
    failures = []

    def retire():
        try:
            _run(case, verify=entered.set)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=retire)
    thread.start()
    try:
        assert not entered.wait(0.05)
        reader.detach()
        assert finished.wait(3)
        assert not failures
        assert entered.is_set()
    finally:
        if not reader._closed:
            reader.detach()
        thread.join(3)


def test_gc_waits_for_handoff_then_remains_blocked_by_successor(case):
    entered, release, gc_done, retirement_done = (threading.Event() for _ in range(4))
    errors = []
    receipt = case.source.members[0].receipt

    def verify():
        entered.set()
        assert release.wait(3)

    def retire():
        try:
            _run(case, verify=verify)
        except BaseException as exc:
            errors.append(exc)
        finally:
            retirement_done.set()

    def gc():
        try:
            ArtifactStore(case.roots.store).delete_blob(
                receipt.image_digest, retention_guard=lambda: case.store.assert_artifact_unleased(receipt.image_digest)
            )
        except OCIStoreError:
            pass
        else:
            errors.append(AssertionError("GC deleted a successor-pinned blob"))
        finally:
            gc_done.set()

    retiring, collecting = threading.Thread(target=retire), threading.Thread(target=gc)
    retiring.start()
    assert entered.wait(3)
    collecting.start()
    try:
        assert not gc_done.wait(0.05)
        release.set()
        assert retirement_done.wait(3) and gc_done.wait(3)
        assert not errors
        assert _load(case, case.successor) == case.successor
    finally:
        release.set()
        retiring.join(3)
        collecting.join(3)


def test_handoff_lock_order_is_union_use_then_sorted_digest_then_index(case, monkeypatch):
    order = []
    original_lock, original_guard = case.store._lock, case.store._artifacts.digest_guard

    @contextmanager
    def lock(authority, name, **kwargs):
        with original_lock(authority, name, **kwargs):
            order.append(name)
            yield

    @contextmanager
    def digest_guard(digest):
        with original_guard(digest):
            order.append(digest)
            yield

    monkeypatch.setattr(case.store, "_lock", lock)
    monkeypatch.setattr(case.store._artifacts, "digest_guard", digest_guard)
    _run(case)
    expected = [
        f"lease-use-{lease_id.replace('-', '')}.lock"
        for lease_id in sorted(member.lease_id for item in (case.source, case.successor) for member in item.members)
    ]
    expected += sorted({member.receipt.image_digest for member in case.source.members})
    assert order == [*expected, "lease-index.lock"]


@pytest.mark.parametrize("change", ["same-owner", "wrong-set-id", "bool-time", "reversed-receipts", "invalid-member"])
def test_invalid_snapshot_rejected_without_pin_changes(case, change):
    if change == "same-owner":
        case.successor = case.source
    elif change == "wrong-set-id":
        case.source = replace(case.source, lease_set_id="sha256:" + "f" * 64)
    elif change == "bool-time":
        object.__setattr__(case.source.members[0], "acquired_ns", True)
    elif change == "invalid-member":
        object.__setattr__(case.source, "members", (object(),))
    else:
        object.__setattr__(case.source, "members", tuple(reversed(case.source.members)))
    count = len(list((case.roots.oci_derived_store / "leases").iterdir()))
    with pytest.raises(OCIStoreError):
        _run(case)
    assert len(list((case.roots.oci_derived_store / "leases").iterdir())) == count


def test_member_directory_fsync_failure_keeps_intent_for_resume(case, monkeypatch):
    fsync = os.fsync
    leases_inode = (case.roots.oci_derived_store / "leases").stat().st_ino

    def fail_member_fsync(fd):
        if os.fstat(fd).st_ino == leases_inode:
            raise OSError("member directory fsync failed")
        fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fsync", fail_member_fsync)
        with pytest.raises(OCIStoreError):
            _run(case)
    assert _intent(case, case.source).exists()
    assert not any(_member(case, member).exists() for member in case.source.members)
    assert _load(case, case.successor) == case.successor
    _run(case, resume=True)
    case.store._require_lease_set_retired(case.source)


def test_exact_source_member_reappearance_during_retirement_is_not_removed(case):
    path = _member(case, case.source.members[0])
    payload = path.read_bytes()
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 3:
            path.write_bytes(payload)
            path.chmod(0o400)

    with pytest.raises(OCIStoreError, match="reappeared"):
        _run(case, verify=verify)
    assert path.read_bytes() == payload
    assert _load(case, case.source) == case.source
    assert _load(case, case.successor) == case.successor


def test_source_intent_changed_after_members_removed_is_not_unlinked(case):
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 5:
            _rewrite(_intent(case, case.source), lambda value: value.__setitem__("plan_digest", "sha256:" + "f" * 64))

    with pytest.raises(OCIStoreError, match="intent changed"):
        _run(case, verify=verify)
    assert _intent(case, case.source).exists()
    assert not any(_member(case, member).exists() for member in case.source.members)
    assert _load(case, case.successor) == case.successor


@pytest.mark.parametrize("kwargs", [{"resume": 1}, {"verify": None}])
def test_invalid_retirement_authority_is_rejected(case, kwargs):
    with pytest.raises(OCIStoreError):
        _run(case, **kwargs)
    assert _load(case, case.source) == case.source
    assert _load(case, case.successor) == case.successor
