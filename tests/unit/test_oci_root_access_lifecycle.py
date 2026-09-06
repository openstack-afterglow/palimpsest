"""Retained roots change writer generations without reviving old ACL authority."""

import hashlib
import json
import os
import stat
import uuid
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
import test_oci_monitor_recovery as recovery_tests
import test_oci_root_access as root_tests
import test_oci_runtime_access as runtime_tests
import test_oci_store as fixtures

from palimpsest_local import oci_root_access as access
from palimpsest_local import oci_root_volume as volumes
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import grant_acl
from palimpsest_local.oci_monitor import ProcessLiveness
from palimpsest_local.oci_store import ArtifactLeaseOwner


@pytest.fixture
def case(tmp_path, monkeypatch):
    original_prepare = fixtures.prepare_oci_root_run

    def prepare(*args, **kwargs):
        kwargs.setdefault("retention_policy", "retain")
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(fixtures, "prepare_oci_root_run", prepare)
    return root_tests.case.__wrapped__(tmp_path, monkeypatch)


def _snapshot(case):
    with case.disk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    info = case.disk.stat()
    _, record, _ = volumes._paths(case.roots, case.plan.root_volume["volume_id"])
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        digest,
        record.read_bytes(),
        case.state.read_bytes(),
        tuple(
            (name, (case.roots.locks / name).read_bytes() if (case.roots.locks / name).exists() else None)
            for name in access._names(case.plan.root_volume["volume_id"])
        ),
        list(case.backend.writes),
    )


def _retire(case, monkeypatch, request):
    receipt = root_tests.grant(case)
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    root_tests.revoke(case)
    retained = volumes.release_oci_root_volume(
        case.roots,
        receipt.volume.volume_id,
        owner=ArtifactLeaseOwner(case.binding.record.run_id, case.binding.record.name, "root-lower"),
        lower_graph_digest=receipt.volume.lower_graph_digest,
        delete=False,
        runner=case.runner,
    )
    assert retained.status == "retained"
    assert retained.generation == receipt.volume.generation + 1
    assert retained.attached_run_id is retained.attached_run_name is None
    return receipt, retained


def _successor(case, volume_id):
    name = "root-access-successor"
    with state.reserve_new_run(case.roots, name, fixtures._oci_dispatch()) as reservation:
        prepared = fixtures.prepare_oci_root_run(
            reservation,
            fixtures._image_materialization(case.store),
            case.store,
            root_volume_size_bytes=fixtures._ROOT_VOLUME_SIZE,
            retained_volume_id=volume_id,
            retention_policy="retain",
            runner=case.runner,
        )
    preview = fixtures.build_oci_root_domain_plan(
        case.roots,
        prepared,
        case.store,
        case.boot,
        case.profile,
        runner=case.runner,
    )
    plan = fixtures.commit_oci_root_domain_plan(case.roots, preview, case.store, runner=case.runner)
    raw = fixtures._DefinitionConnection()
    runtime_tests.runtime.define_committed_oci_root_domain(
        case.roots,
        name,
        case.store,
        case.boot,
        case.profile,
        conn=raw,
        runner=case.runner,
    )
    original = raw.domains[name]
    xml = ET.fromstring(original.xml)
    ET.SubElement(xml, "uuid").text = original.domain_uuid
    original.xml = ET.tostring(xml, encoding="unicode")
    binding = runtime_tests.runtime.prepare_oci_root_monitor_binding(
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
    return SimpleNamespace(
        **{
            **vars(case),
            "binding": binding,
            "conn": conn,
            "domain": domain,
            "plan": plan,
            "state": case.roots.runs / name / "state.json",
            "paths": runtime_tests.io.runtime_io_paths(case.roots.runs / name),
        }
    )


def test_retain_and_explicit_claim_increment_generation_without_changing_disk(case, monkeypatch, request):
    original = _snapshot(case)[:7]
    receipt, retained = _retire(case, monkeypatch, request)
    assert _snapshot(case)[:7] == original
    successor = ArtifactLeaseOwner(str(uuid.uuid4()), "new-root-owner", "root-lower")
    claimed = volumes.claim_oci_root_volume(
        case.roots,
        retained.volume_id,
        size_bytes=retained.size_bytes,
        lower_graph_digest=retained.lower_graph_digest,
        retention_policy="retain",
        owner=successor,
        runner=case.runner,
    )
    assert claimed.record.generation == retained.generation + 1 == receipt.volume.generation + 2
    assert (claimed.record.attached_run_id, claimed.record.attached_run_name) == (successor.run_id, successor.run_name)
    assert claimed.claimed_from_retained and not claimed.created
    assert _snapshot(case)[:7] == original
    before = _snapshot(case)
    with pytest.raises(StateError):
        root_tests.revoke(case)
    fd = os.open(case.disk, os.O_RDONLY)
    try:
        with pytest.raises(StateError):
            access.verify_root_launch_access(case.roots, receipt.to_dict(), fd, binding=case.binding)
    finally:
        os.close(fd)
    assert _snapshot(case) == before


def test_successor_grant_is_not_revoked_by_old_generation(case, monkeypatch, request):
    old, retained = _retire(case, monkeypatch, request)
    second = _successor(case, retained.volume_id)
    granted = root_tests.grant(second)
    assert granted.volume.generation == old.volume.generation + 2
    assert granted.access_id != old.access_id
    assert granted.target.inode == old.target.inode
    assert root_tests.load(second, granted).record == granted.volume
    before = _snapshot(second)
    for action in (lambda: root_tests.revoke(case), lambda: root_tests.grant(case)):
        with pytest.raises(StateError):
            action()
        assert _snapshot(second) == before
    fd = os.open(second.disk, os.O_RDONLY)
    try:
        with pytest.raises(StateError):
            access.verify_root_launch_access(case.roots, old.to_dict(), fd, binding=case.binding)
        access.verify_root_launch_access(second.roots, granted.to_dict(), fd, binding=second.binding)
    finally:
        os.close(fd)
    assert _snapshot(second) == before


@pytest.mark.parametrize("action", ["grant", "revoke"])
def test_access_uses_existing_volume_lock_file(case, monkeypatch, request, action):
    if action == "revoke":
        root_tests.grant(case)
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
    _, _, lock_path = volumes._paths(case.roots, case.plan.root_volume["volume_id"])
    original = lock_path.stat()
    before = _snapshot(case)
    with state.file_lock(lock_path):
        with pytest.raises(StateError, match="lock is busy"):
            getattr(root_tests, action)(case)
    current = lock_path.stat()
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    assert _snapshot(case) == before


def test_terminal_cleanup_does_not_authorize_revoke_while_writer_is_live(case, monkeypatch, request):
    root_tests.grant(case)
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    before = _snapshot(case)
    with pytest.raises(StateError):
        root_tests.revoke(case, liveness_probe=lambda _: ProcessLiveness.LIVE)
    assert _snapshot(case) == before
    assert stat.S_IMODE(case.disk.stat().st_mode) == 0o660


@pytest.mark.parametrize("action", ["grant", "launch", "revoke"])
def test_wrong_principal_full_acl_is_refused_even_with_granted_mode(case, monkeypatch, request, action):
    receipt = root_tests.grant(case)
    if action == "revoke":
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
    case.backend.acls[receipt.target.device, receipt.target.inode] = grant_acl(
        receipt.target.baseline, receipt.qemu_uid + 1
    )
    assert stat.S_IMODE(case.disk.stat().st_mode) == 0o660
    before = _snapshot(case)
    if action == "launch":
        fd = os.open(case.disk, os.O_RDONLY)
        try:
            with pytest.raises(StateError):
                access.verify_root_launch_access(case.roots, receipt.to_dict(), fd, binding=case.binding)
        finally:
            os.close(fd)
    else:
        with pytest.raises(StateError):
            getattr(root_tests, action)(case)
    assert _snapshot(case) == before


@pytest.mark.parametrize("boundary", ["unlinked", "quarantined", "replaced-quarantine"])
def test_managed_deletion_replay_keeps_revocation_and_exact_quarantine(case, monkeypatch, request, boundary):
    receipt = root_tests.grant(case)
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    root_tests.revoke(case)
    _, record_path, _ = volumes._paths(case.roots, receipt.volume.volume_id)
    quarantine = volumes._deletion_quarantine(case.roots, receipt.volume.volume_id)
    evidence = tuple((case.roots.locks / name).read_bytes() for name in access._names(receipt.volume.volume_id))
    writes = list(case.backend.writes)

    class Crash(BaseException):
        pass

    def release():
        return volumes.release_oci_root_volume(
            case.roots,
            receipt.volume.volume_id,
            owner=ArtifactLeaseOwner(case.binding.record.run_id, case.binding.record.name, "root-lower"),
            lower_graph_digest=receipt.volume.lower_graph_digest,
            delete=True,
            runner=case.runner,
        )

    original_sync = state.fsync_directory

    def sync(path):
        original_sync(path)
        if quarantine.exists() and not case.disk.exists():
            raise Crash()

    def remove_record(*_args):
        assert not case.disk.exists() and not quarantine.exists()
        raise Crash()

    with monkeypatch.context() as patch:
        if boundary == "unlinked":
            patch.setattr(volumes, "_remove_record", remove_record)
        else:
            patch.setattr(state, "fsync_directory", sync)
        with pytest.raises(Crash):
            release()
    deleting = json.loads(record_path.read_bytes())
    assert deleting["status"] == "deleting"
    assert deleting["generation"] == receipt.volume.generation + 1
    assert not case.disk.exists()
    if boundary != "unlinked":
        assert quarantine.stat().st_ino == receipt.target.inode
    if boundary == "replaced-quarantine":
        original = quarantine.with_suffix(".original")
        quarantine.rename(original)
        quarantine.write_bytes(original.read_bytes())
        quarantine.chmod(0o600)
        replacement = quarantine.stat()
        with pytest.raises(StateError):
            release()
        assert quarantine.stat().st_ino == replacement.st_ino
        assert original.stat().st_ino == receipt.target.inode
        assert json.loads(record_path.read_bytes()) == deleting
    else:
        assert release() is None
        assert not record_path.exists() and not quarantine.exists()
        assert release() is None
    assert tuple((case.roots.locks / name).read_bytes() for name in access._names(receipt.volume.volume_id)) == evidence
    assert case.backend.writes == writes
