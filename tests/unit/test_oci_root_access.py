"""Root grants bind the single writer generation, not mutable guest contents."""

import json
import os
import stat
from dataclasses import replace

import pytest
import test_oci_runtime_access as runtime_access_tests

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_root_access as access
from palimpsest_local import oci_root_volume as volumes
from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor import ProcessLiveness
from palimpsest_local.oci_store import ArtifactLeaseOwner


@pytest.fixture
def case(tmp_path, monkeypatch):
    result = runtime_access_tests.case.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(access, "LinuxFdACLBackend", lambda: result.backend)
    result.disk = volumes._paths(result.roots, result.plan.root_volume["volume_id"])[0]
    return result


def grant(case):
    return access.grant_oci_root_access(case.roots, case.binding, conn=case.conn, runner=case.runner)


def revoke(case, **kwargs):
    return access.revoke_oci_root_access(
        case.roots,
        case.binding,
        conn=case.conn,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def load(case, receipt=None):
    return volumes.load_oci_root_volume(
        case.roots,
        case.plan.root_volume["volume_id"],
        runner=case.runner,
        root_access=None if receipt is None else receipt.to_dict(),
    )


def test_exact_grant_load_and_cleanup(case, monkeypatch, request):
    before = case.disk.stat()
    receipt = grant(case)
    assert receipt.volume.generation == case.plan.root_volume["generation"]
    assert receipt.target.inode == before.st_ino
    assert stat.S_IMODE(case.disk.stat().st_mode) == 0o660
    assert len(case.backend.writes) == 1
    assert access.RootAccessReceipt.from_dict(receipt.to_dict()) == receipt
    assert grant(case) == receipt
    assert len(case.backend.writes) == 1
    assert load(case, receipt).record == receipt.volume
    with pytest.raises(StateError):
        load(case)
    runtime_access_tests._terminal_cleanup(case, monkeypatch, request)
    assert revoke(case) == receipt
    assert revoke(case) == receipt
    assert stat.S_IMODE(case.disk.stat().st_mode) == 0o600
    assert len(case.backend.writes) == 2
    assert load(case).record == receipt.volume


@pytest.mark.parametrize("action", ["release", "claim", "retain"])
def test_granted_fence_blocks_volume_lifecycle(case, action):
    receipt = grant(case)
    with pytest.raises(StateError):
        if action == "release":
            volumes.release_oci_root_volume(
                case.roots,
                receipt.volume.volume_id,
                owner=ArtifactLeaseOwner(case.binding.record.run_id, case.binding.record.name, "root-lower"),
                lower_graph_digest=receipt.volume.lower_graph_digest,
                runner=case.runner,
            )
        elif action == "claim":
            volumes.claim_oci_root_volume(
                case.roots,
                receipt.volume.volume_id,
                owner=ArtifactLeaseOwner(case.binding.record.run_id, case.binding.record.name, "root-lower"),
                lower_graph_digest=receipt.volume.lower_graph_digest,
                size_bytes=receipt.volume.size_bytes,
                retention_policy=receipt.volume.retention_policy,
                runner=case.runner,
            )
        else:
            access.require_root_access_revoked(case.roots, receipt.volume)
    assert len(case.backend.writes) == 1


def test_guest_writes_do_not_invalidate_exact_root(case):
    receipt = grant(case)
    with case.disk.open("r+b") as disk:
        disk.seek(8192)
        disk.write(b"guest writes are mutable")
    assert load(case, receipt).record == receipt.volume
    fd = os.open(case.disk, os.O_RDONLY)
    try:
        access.verify_root_launch_access(case.roots, receipt.to_dict(), fd, binding=case.binding)
    finally:
        os.close(fd)


@pytest.mark.parametrize("damage", ["fence", "marker", "member", "acl", "inode", "size", "generation"])
def test_missing_or_replaced_evidence_fails_closed(case, damage):
    receipt = grant(case)
    marker, fence = access._names(receipt.volume.volume_id)
    if damage in {"fence", "marker"}:
        (case.roots.locks / (fence if damage == "fence" else marker)).unlink()
    elif damage == "member":
        receipt = None
    elif damage == "acl":
        case.backend.acls.clear()
        case.disk.chmod(0o600)
    elif damage == "inode":
        original = case.disk.with_suffix(".original")
        case.disk.rename(original)
        case.disk.write_bytes(original.read_bytes())
        case.disk.chmod(0o660)
    elif damage == "size":
        with case.disk.open("ab") as disk:
            disk.write(b"x")
    else:
        _, record_path, _ = volumes._paths(case.roots, receipt.volume.volume_id)
        with volumes._root_authority(case.roots) as fd:
            volumes._write_record(fd, record_path, replace(receipt.volume, generation=receipt.volume.generation + 1))
    with pytest.raises(StateError):
        load(case, receipt)


def test_grant_crash_after_acl_resumes_exact_intent(case):
    def fail(fd, acl):
        raise OSError("crash after ACL")

    case.backend.after_write = fail
    with pytest.raises(StateError):
        grant(case)
    case.backend.after_write = lambda *_: None
    receipt = grant(case)
    assert load(case, receipt).record == receipt.volume
    assert len(case.backend.writes) == 1


def test_revoke_requires_original_stale_terminal_cleanup(case):
    grant(case)
    with pytest.raises(StateError):
        revoke(case)
    assert len(case.backend.writes) == 1


def test_revoke_crash_restores_before_lifecycle(case, monkeypatch, request):
    receipt = grant(case)
    runtime_access_tests._terminal_cleanup(case, monkeypatch, request)
    case.backend.after_write = lambda *_: (_ for _ in ()).throw(OSError("crash after restore"))
    with pytest.raises(StateError):
        revoke(case)
    with pytest.raises(StateError):
        access.require_root_access_revoked(case.roots, receipt.volume)
    case.backend.after_write = lambda *_: None
    assert revoke(case) == receipt
    access.require_root_access_revoked(case.roots, receipt.volume)


def test_marker_only_recovery_requires_original_binding(case, monkeypatch):
    original_write = access._write

    def fail(fd, name, value, expected):
        if not name.endswith(".enrolled.json"):
            raise OSError("crash after enrollment")
        return original_write(fd, name, value, expected)

    monkeypatch.setattr(access, "_write", fail)
    with pytest.raises(StateError):
        grant(case)
    assert not case.backend.writes
    with pytest.raises(StateError):
        load(case)
    monkeypatch.setattr(access, "_write", original_write)
    receipt = grant(case)
    assert load(case, receipt).record == receipt.volume


def test_receipt_deletion_cannot_restore_legacy_lifecycle(case):
    receipt = grant(case)
    value = json.loads(case.state.read_bytes())
    del value["oci_root_access"]
    case.state.write_text(json.dumps(value))
    with pytest.raises(StateError):
        access.require_root_access_revoked(case.roots, receipt.volume)


def test_completed_grant_is_read_only_and_missing_run_member_is_not_repaired(case, monkeypatch):
    receipt = grant(case)
    original_fsync = os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (receipt.target.device, receipt.target.inode):
            pytest.fail("completed replay root fsync")
        return original_fsync(fd)

    monkeypatch.setattr(access.os, "fsync", fsync)
    assert grant(case) == receipt
    value = json.loads(case.state.read_bytes())
    del value["oci_root_access"]
    case.state.write_text(json.dumps(value))
    with pytest.raises(StateError):
        grant(case)


def test_both_enrollment_files_missing_still_cannot_retain_granted_file(case):
    receipt = grant(case)
    for name in access._names(receipt.volume.volume_id):
        (case.roots.locks / name).unlink()
    with pytest.raises(StateError):
        access.require_root_access_revoked(case.roots, receipt.volume)


def test_launch_requires_managed_member_and_pins_mutable_root_fd(case):
    receipt = grant(case)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    with launch.prepare_monitor_launch_authority(
        case.roots, case.store, case.boot, case.profile, case.binding
    ) as authority:
        frame = authority.to_dict()
        assert frame["root_access"] == receipt.to_dict()
        assert frame["entries"]["root_disk"]["inode"] == receipt.target.inode
        with case.disk.open("r+b") as disk:
            disk.seek(8192)
            disk.write(b"guest mutation")
        authority.validate()
        frame["root_access"] = None
        frame["entries"].pop("root_disk")
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)


def test_legacy_frame_cannot_survive_later_root_enrollment(case):
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    with launch.prepare_monitor_launch_authority(
        case.roots, case.store, case.boot, case.profile, case.binding
    ) as authority:
        grant(case)
        with pytest.raises(StateError):
            authority.validate()


def test_acl_read_callback_cannot_replace_volume_record_at_grant_tail(case, monkeypatch):
    receipt = grant(case)
    original = case.backend.read_acl
    _, record_path, _ = volumes._paths(case.roots, receipt.volume.volume_id)

    def mutate(fd):
        result = original(fd)
        with volumes._root_authority(case.roots) as directory:
            volumes._write_record(
                directory, record_path, replace(receipt.volume, generation=receipt.volume.generation + 1)
            )
        return result

    monkeypatch.setattr(case.backend, "read_acl", mutate)
    with pytest.raises(StateError):
        grant(case)


def test_explicit_null_root_member_is_not_legacy(case):
    value = json.loads(case.state.read_bytes())
    value["oci_root_access"] = None
    case.state.write_text(json.dumps(value))
    with pytest.raises(StateError):
        grant(case)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    with pytest.raises(StateError):
        launch.prepare_monitor_launch_authority(case.roots, case.store, case.boot, case.profile, case.binding)


@pytest.mark.parametrize("damage", ["member", "fence"])
def test_launch_acl_callback_cannot_delete_evidence_at_tail(case, monkeypatch, damage):
    receipt = grant(case)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    with launch.prepare_monitor_launch_authority(
        case.roots, case.store, case.boot, case.profile, case.binding
    ) as authority:
        original = case.backend.read_acl

        def mutate(fd):
            result = original(fd)
            if damage == "member":
                value = json.loads(case.state.read_bytes())
                value.pop("oci_root_access", None)
                case.state.write_text(json.dumps(value))
            else:
                _, name = access._names(receipt.volume.volume_id)
                (case.roots.locks / name).unlink(missing_ok=True)
            return result

        monkeypatch.setattr(case.backend, "read_acl", mutate)
        with pytest.raises(StateError):
            authority.validate()
