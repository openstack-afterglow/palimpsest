"""The root-disk parent is one member-set target, never a per-VM grant."""

import json
import os
import stat

import pytest
import test_oci_root_access as root_tests
import test_oci_runtime_access_launch as launch_tests
import test_oci_shared_traversal as shared_tests

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_root_access as root_access
from palimpsest_local import oci_root_volume as volumes
from palimpsest_local import oci_shared_traversal as shared
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import OCIACLError, traversal_acl
from palimpsest_local.oci_provenance import canonical_json_bytes
from palimpsest_local.oci_store import ArtifactLeaseOwner


@pytest.fixture
def case(tmp_path, monkeypatch):
    value = shared_tests.case.__wrapped__(tmp_path, monkeypatch)
    value.namespace_ids[_identity(value.roots.oci_root_volumes)] = "root_volumes"
    return value


def _identity(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino


def _acl(case):
    fd = os.open(case.roots.oci_root_volumes, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return case.backend.read_acl(fd)
    finally:
        os.close(fd)


def _root_writes(case):
    return [acl for role, acl in case.backend.writes if role == "root_volumes"]


def test_two_distinct_root_vms_share_parent_until_last_leave(case, monkeypatch, request):
    first = shared_tests._join(case)
    second = shared_tests._second(case)
    second_member = shared_tests._join(second)
    assert case.plan.root_volume["volume_id"] != second.plan.root_volume["volume_id"]
    assert first.root_volumes == second_member.root_volumes
    assert _identity(case.roots.oci_root_volumes) == (first.root_volumes.device, first.root_volumes.inode)
    (second.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    with launch_tests._prepare(second) as authority:
        frame = authority.to_dict()
        assert frame["schema"] == "palimpsest.monitor-launch-authority.v8"
        assert frame["entries"]["root_volumes"]["inode"] == first.root_volumes.inode
        shared_tests._finish(case, monkeypatch, request)
        parent_info = case.roots.oci_root_volumes.stat()
        original_sync = os.fsync

        def sync(fd):
            info = os.fstat(fd)
            assert (info.st_dev, info.st_ino) != (parent_info.st_dev, parent_info.st_ino)
            return original_sync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", sync)
            assert shared_tests._leave(case).phase == "left"
        assert not _root_writes(case)
        assert _acl(second) == second_member.root_volumes.granted
        assert stat.S_IMODE(case.roots.oci_root_volumes.stat().st_mode) == 0o710
        authority.validate(binding=second.binding)
        assert authority.to_dict() == frame
        for entry in frame["entries"].values():
            entry["fd"] = os.dup(entry["fd"])
        try:
            child = launch.MonitorLaunchAuthority.from_dict(frame)
        except BaseException:
            for entry in frame["entries"].values():
                os.close(entry["fd"])
            raise
        with child:
            child.validate(binding=second.binding)
    shared_tests._finish(second, monkeypatch, request)
    assert shared_tests._leave(second).phase == "left"
    assert [role for role, _ in second.backend.writes] == ["state", "runs", "root_volumes"]
    assert _acl(second) == first.root_volumes.baseline
    assert stat.S_IMODE(second.roots.oci_root_volumes.stat().st_mode) == 0o700


@pytest.mark.parametrize("empty", [False, True])
def test_managed_constructor_never_chmods_root_parent(case, monkeypatch, request, empty):
    member = shared_tests._join(case)
    if empty:
        shared_tests._finish(case, monkeypatch, request)
        shared_tests._leave(case)
    parent = _identity(case.roots.oci_root_volumes)
    original_chmod = os.fchmod
    before = _acl(case), case.roots.oci_root_volumes.stat().st_mode

    def chmod(fd, mode):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) != parent
        return original_chmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", chmod)
    state.init_resolved_roots(case.roots)
    state.init_resolved_roots(case.roots)
    assert (_acl(case), case.roots.oci_root_volumes.stat().st_mode) == before
    assert _acl(case) == (member.root_volumes.baseline if empty else member.root_volumes.granted)


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("damage", ["mode", "principal", "default-acl"])
def test_managed_parent_drift_refuses_initialization_without_repair(case, monkeypatch, request, empty, damage):
    member = shared_tests._join(case)
    if empty:
        shared_tests._finish(case, monkeypatch, request)
        shared_tests._leave(case)
    parent = case.roots.oci_root_volumes
    target = member.root_volumes
    if damage == "mode":
        parent.chmod(0o750)
    elif damage == "principal":
        case.backend.acls[target.device, target.inode] = traversal_acl(target.baseline, member.qemu_uid + 1)
    else:
        original_read = case.backend.read_acl

        def read(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (target.device, target.inode):
                raise OCIACLError("unsupported default ACL")
            return original_read(fd)

        monkeypatch.setattr(case.backend, "read_acl", read)
    before = parent.stat(), list(case.backend.writes)
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    after = parent.stat()
    assert (after.st_dev, after.st_ino, after.st_mode) == (before[0].st_dev, before[0].st_ino, before[0].st_mode)
    assert case.backend.writes == before[1]


@pytest.mark.parametrize("operation", ["join", "leave"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_interrupted_root_parent_transition_preserves_prefix_and_resumes(case, monkeypatch, request, operation, timing):
    if operation == "leave":
        shared_tests._join(case)
        shared_tests._finish(case, monkeypatch, request)
    parent = _identity(case.roots.oci_root_volumes)
    fired = False

    def fail(fd, acl):
        nonlocal fired
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == parent:
            fired = True
            raise OSError("interrupted root parent ACL transition")

    action = shared_tests._join if operation == "join" else shared_tests._leave
    setattr(case.backend, timing + "_write", fail)
    with pytest.raises(StateError):
        action(case)
    assert fired
    setattr(case.backend, timing + "_write", lambda *_: None)
    # Grant opens the leaf parent first; leave closes it last. The two other
    # parents must remain baseline at either interrupted root-parent boundary.
    assert [stat.S_IMODE(path.stat().st_mode) for path in (case.roots.state, case.roots.runs)] == [0o700, 0o700]
    before = case.roots.oci_root_volumes.stat().st_mode, list(case.backend.writes)
    state.init_resolved_roots(case.roots)
    assert (case.roots.oci_root_volumes.stat().st_mode, case.backend.writes) == before
    result = action(case)
    assert result.phase == ("active" if operation == "join" else "left")
    assert len(_root_writes(case)) == 1
    assert _acl(case) == (result.root_volumes.granted if operation == "join" else result.root_volumes.baseline)


def test_replaced_root_parent_is_not_adopted_by_initializer_or_existing_frame(case):
    member = shared_tests._join(case)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    parent = case.roots.oci_root_volumes
    with launch_tests._prepare(case) as authority:
        original = parent.with_name("original-root-volumes")
        parent.rename(original)
        parent.mkdir(mode=0o710)
        replacement = _identity(parent)
        case.backend.acls[replacement] = member.root_volumes.granted
        for action in (lambda: state.init_resolved_roots(case.roots), authority.validate):
            with pytest.raises(StateError):
                action()
        assert _identity(parent) == replacement
        assert _identity(original) == (member.root_volumes.device, member.root_volumes.inode)
        assert stat.S_IMODE(parent.stat().st_mode) == stat.S_IMODE(original.stat().st_mode) == 0o710


@pytest.mark.parametrize("target", ["member", "registry"])
def test_v1_authority_is_refused_without_chmod_or_implicit_upgrade(case, target):
    member = shared_tests._join(case)
    parent = case.roots.oci_root_volumes
    before = parent.stat().st_mode, list(case.backend.writes)
    if target == "member":
        old = member.to_dict()
        old["schema"] = "palimpsest.oci-shared-traversal-member.v1"
        old.pop("root_volumes")
        with pytest.raises(StateError):
            shared.SharedTraversalMembership.from_dict(old)
    else:
        path = case.roots.locks / shared._REGISTRY
        old = json.loads(path.read_bytes())
        old["schema"] = "palimpsest.oci-shared-traversal.v1"
        old.pop("root_volumes")
        content = canonical_json_bytes(old)
        path.write_bytes(content)
        with pytest.raises(StateError):
            state.init_resolved_roots(case.roots)
        assert path.read_bytes() == content
    assert (parent.stat().st_mode, case.backend.writes) == before


@pytest.mark.parametrize("member_state", ["present", "missing", "null"])
def test_shared_leave_waits_for_managed_root_disk_revocation(case, monkeypatch, request, member_state):
    monkeypatch.setattr(root_access, "LinuxFdACLBackend", lambda: case.backend)
    root_receipt = root_tests.grant(case)
    member = shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    if member_state != "present":
        with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
            data = mutation.mutable_state()
            if member_state == "missing":
                data.pop(root_access.OCI_ROOT_ACCESS_STATE_KEY)
            else:
                data[root_access.OCI_ROOT_ACCESS_STATE_KEY] = None
            mutation.write_state(mutation.snapshot.state["status"], data)
    disk = volumes._paths(case.roots, root_receipt.volume.volume_id)[0]
    registry = case.roots.locks / shared._REGISTRY
    before = case.state.read_bytes(), registry.read_bytes(), list(case.backend.writes)
    assert stat.S_IMODE(disk.stat().st_mode) == 0o660
    with pytest.raises(StateError):
        shared_tests._leave(case)
    assert (case.state.read_bytes(), registry.read_bytes(), case.backend.writes) == before
    assert _acl(case) == member.root_volumes.granted
    assert stat.S_IMODE(disk.stat().st_mode) == 0o660
    if member_state != "present":
        with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
            data = mutation.mutable_state()
            data[root_access.OCI_ROOT_ACCESS_STATE_KEY] = root_receipt.to_dict()
            mutation.write_state(mutation.snapshot.state["status"], data)
    assert root_tests.revoke(case) == root_receipt
    assert shared_tests._leave(case).phase == "left"
    assert stat.S_IMODE(disk.stat().st_mode) == 0o600
    assert _acl(case) == member.root_volumes.baseline


@pytest.mark.parametrize("empty", [False, True])
def test_missing_enrolled_root_parent_is_not_recreated(case, monkeypatch, request, empty):
    shared_tests._join(case)
    if empty:
        shared_tests._finish(case, monkeypatch, request)
        shared_tests._leave(case)
    parent = case.roots.oci_root_volumes
    original = parent.with_name("original-root-volumes")
    before = parent.stat()
    parent.rename(original)
    registry = case.roots.locks / shared._REGISTRY
    evidence = case.state.read_bytes(), registry.read_bytes(), list(case.backend.writes)
    with pytest.raises(StateError):
        state.init_resolved_roots(case.roots)
    assert not parent.exists() and not parent.is_symlink()
    after = original.stat()
    assert (after.st_dev, after.st_ino, after.st_mode) == (before.st_dev, before.st_ino, before.st_mode)
    assert (case.state.read_bytes(), registry.read_bytes(), case.backend.writes) == evidence


@pytest.mark.parametrize("delete", [False, True])
def test_completed_left_replay_does_not_reopen_retained_or_deleted_root(case, monkeypatch, request, delete):
    monkeypatch.setattr(root_access, "LinuxFdACLBackend", lambda: case.backend)
    root_receipt = root_tests.grant(case)
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    root_tests.revoke(case)
    left = shared_tests._leave(case)
    volume_id = root_receipt.volume.volume_id
    result = volumes.release_oci_root_volume(
        case.roots,
        volume_id,
        owner=ArtifactLeaseOwner(case.binding.record.run_id, case.binding.record.name, "root-lower"),
        lower_graph_digest=root_receipt.volume.lower_graph_digest,
        delete=delete,
        runner=case.runner,
    )
    if delete:
        assert result is None
    else:
        claimed = volumes.claim_oci_root_volume(
            case.roots,
            volume_id,
            size_bytes=result.size_bytes,
            lower_graph_digest=result.lower_graph_digest,
            retention_policy="retain",
            owner=ArtifactLeaseOwner("00000000-0000-4000-8000-000000000002", "next-root-owner", "root-lower"),
            runner=case.runner,
        )
        assert claimed.record.generation == root_receipt.volume.generation + 2
    _, record, _ = volumes._paths(case.roots, volume_id)
    record_bytes = record.read_bytes() if record.exists() else None
    registry = case.roots.locks / shared._REGISTRY
    before = case.state.read_bytes(), registry.read_bytes(), list(case.backend.writes)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("historical left replay must not inspect a successor volume record")

    monkeypatch.setattr(volumes, "_read_record", unexpected_read)
    monkeypatch.setattr(root_access, "_read_record", unexpected_read)
    assert shared_tests._leave(case) == left
    assert shared_tests._leave(case) == left
    assert (case.state.read_bytes(), registry.read_bytes(), case.backend.writes) == before
    assert (record.read_bytes() if record.exists() else None) == record_bytes
