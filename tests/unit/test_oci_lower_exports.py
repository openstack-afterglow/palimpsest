"""Owned lower copies retain CAS bytes and ordered occurrence/lease authority."""

import json
import os
import stat
from dataclasses import replace

import pytest
import test_oci_boot_exports as boot_tests
import test_oci_store as fixtures

from palimpsest_local import oci_lower_exports as exports
from palimpsest_local import state
from palimpsest_local.errors import StateError


def two_distinct_materialization(store):
    original = fixtures._image_materialization(store)
    occurrence = replace(fixtures._occurrence(1), diff_id=fixtures._digest("e"))
    result = store.materialize_observed(
        occurrence,
        fixtures._key(occurrence),
        fixtures._producer(occurrence, [], fixtures._squashfs(b"different" + b"\0" * 55)),
    )
    return replace(
        original,
        layer_diff_ids=(original.layer_diff_ids[0], occurrence.diff_id, original.layer_diff_ids[2]),
        results=(original.results[0], result, original.results[2]),
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    materialize = fixtures._image_materialization

    def distinct(store):
        monkeypatch.setattr(fixtures, "_image_materialization", materialize)
        return two_distinct_materialization(store)

    monkeypatch.setattr(fixtures, "_image_materialization", distinct)
    value = boot_tests.case.__wrapped__(tmp_path)
    boot_tests.publish(value)
    return value


def publish(case):
    return exports.publish_oci_lower_exports(case.roots, case.prepared, case.store, conn=case.conn)


def load(case):
    return exports.load_oci_lower_exports(case.roots, case.name)


def test_distinct_copies_preserve_cas_and_ordered_occurrences(case):
    transaction = case.prepared.transaction
    before = {
        layer.image_digest: boot_tests._snapshot(case.roots.store / "blobs" / "sha256" / layer.image_digest[7:])
        for layer in transaction.receipts
    }
    leases = case.store.load_lease_set(
        transaction.lower_lease_set_id, transaction.owner, plan_digest=transaction.boot_plan_digest
    )
    receipt = publish(case)
    assert receipt.phase == "ready"
    assert exports.LowerExportReceipt.from_dict(receipt.to_dict()) == receipt
    selected = load(case)
    assert len(selected) == 2
    assert len(selected) < len(transaction.receipts), "fixture must exercise repeated physical digest"
    for digest, path in selected.items():
        assert path.read_bytes() == before[digest][-1]
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert path.stat().st_nlink == 1
        assert boot_tests._snapshot(path)[:2] != before[digest][:2]
        assert boot_tests._snapshot(case.roots.store / "blobs" / "sha256" / digest[7:]) == before[digest]
    preview = fixtures.build_oci_root_domain_plan(
        case.roots, case.prepared, case.store, case.boot, case.profile, runner=case.runner
    )
    assert [disk.host_path for disk in preview.spec.layers] == [
        selected[layer.image_digest] for layer in transaction.receipts
    ]
    fixtures.commit_oci_root_domain_plan(case.roots, preview, case.store, runner=case.runner)
    assert (
        case.store.load_lease_set(
            transaction.lower_lease_set_id, transaction.owner, plan_digest=transaction.boot_plan_digest
        )
        == leases
    )
    assert "/blobs/" not in json.dumps(receipt.to_dict())


def test_completed_replay_is_readonly(case, monkeypatch):
    receipt = publish(case)
    before = case.state.read_bytes()

    def fail(*_args):
        raise AssertionError("completed publication must not mutate")

    monkeypatch.setattr(exports, "_write", fail)
    assert publish(case) == receipt
    assert case.state.read_bytes() == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "inode", "bytes", "mode", "missing", "null", "both-missing"])
def test_export_damage_never_falls_back_to_cas(case, tmp_path, damage):
    publish(case)
    path = next(iter(load(case).values()))
    if damage in {"missing", "null", "both-missing"}:
        data = json.loads(case.state.read_bytes())
        if damage in {"missing", "both-missing"}:
            data.pop("oci_lower_exports")
            if damage == "both-missing":
                data.pop("oci_lower_access", None)
        else:
            data["oci_lower_exports"] = None
        case.state.write_text(json.dumps(data))
    elif damage == "hardlink":
        os.link(path, tmp_path / "alias")
    elif damage in {"inode", "symlink"}:
        held = path.with_suffix(".held")
        path.rename(held)
        if damage == "symlink":
            path.symlink_to(held)
        else:
            path.write_bytes(held.read_bytes())
            path.chmod(0o400)
    elif damage == "mode":
        path.chmod(0o600)
    else:
        path.chmod(0o600)
        with path.open("r+b") as stream:
            stream.write(b"x")
        path.chmod(0o400)
    with pytest.raises(StateError):
        exports.select_lower_exports(case.roots, state.read_run_ledger_snapshot(case.roots, case.name))


def test_sealed_fsync_crash_resumes_recorded_inode_without_recopy(case, monkeypatch):
    original = os.fsync
    fired = False

    def fail(fd):
        nonlocal fired
        info = os.fstat(fd)
        if not fired and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400:
            fired = True
            raise OSError("sealed durability interrupted")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(StateError):
        publish(case)
    assert fired
    saved = exports.LowerExportReceipt.from_dict(json.loads(case.state.read_bytes())["oci_lower_exports"])
    assert saved.phase == "intent"
    first = next(
        target
        for target in saved.targets
        if stat.S_IMODE((case.run_root / ("lower-" + target.digest[7:])).stat().st_mode) == 0o400
    )
    before = boot_tests._snapshot(case.run_root / ("lower-" + first.digest[7:]))
    monkeypatch.setattr(os, "fsync", original)
    assert publish(case).export_id == saved.export_id
    assert boot_tests._snapshot(case.run_root / ("lower-" + first.digest[7:])) == before
