"""Shared departure preserves sealed stage-1 authority across other callbacks."""

import json
import os
import stat

import pytest
import test_oci_root_access as root_tests
import test_oci_shared_traversal as shared_tests
import test_oci_stage1_access as stage_tests

from palimpsest_local import oci_root_access as root_access
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import readonly_grant_acl


@pytest.fixture
def case(tmp_path, monkeypatch):
    value = shared_tests.case.__wrapped__(tmp_path, monkeypatch)
    stage_tests._install_stage1_backend(value, monkeypatch)
    monkeypatch.setattr(root_access, "LinuxFdACLBackend", lambda: value.backend)
    return value


@pytest.mark.parametrize("member_state", ["present", "missing", "null"])
def test_shared_leave_requires_stage1_restored_before_traversal_removal(case, monkeypatch, request, member_state):
    receipt = stage_tests.grant(case)
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    if member_state != "present":
        with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
            data = mutation.mutable_state()
            if member_state == "missing":
                data.pop("oci_stage1_access")
            else:
                data["oci_stage1_access"] = None
            mutation.write_state(data["status"], data)
    before = stage_tests._snapshot(case)
    with pytest.raises(StateError):
        shared_tests._leave(case)
    assert stage_tests._snapshot(case) == before
    if member_state != "present":
        with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
            data = mutation.mutable_state()
            data["oci_stage1_access"] = receipt.to_dict()
            mutation.write_state(data["status"], data)
    assert stage_tests.revoke(case).phase == "revoked"
    assert shared_tests._leave(case).phase == "left"
    assert stat.S_IMODE(case.transport_path.stat().st_mode) == 0o400


@pytest.mark.parametrize("damage", ["payload", "member", "same-mode-acl"])
def test_root_acl_callback_cannot_change_previously_verified_stage1_on_leave(case, monkeypatch, request, damage):
    receipt = stage_tests.grant(case)
    root = root_tests.grant(case)
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    stage_tests.revoke(case)
    root_tests.revoke(case)
    original = case.backend.read_acl
    fired = False
    writes = list(case.backend.writes)

    def read(fd):
        nonlocal fired
        result = original(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (root.target.device, root.target.inode) and not fired:
            fired = True
            if damage == "member":
                data = json.loads(case.state.read_bytes())
                data.pop("oci_stage1_access")
                case.state.write_text(json.dumps(data))
            elif damage == "payload":
                case.transport_path.chmod(0o600)
                with case.transport_path.open("r+b") as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b"x")
                case.transport_path.chmod(0o400)
            else:
                # Masked named ACL changes can preserve mode400; model an
                # unexpected ACL and the native metadata-change indication.
                before = case.transport_path.stat()
                case.backend.acls[receipt.target.device, receipt.target.inode] = readonly_grant_acl(
                    receipt.qemu_uid + 1
                )
                os.chmod(case.transport_path, 0o400)
                assert case.transport_path.stat().st_ctime_ns != before.st_ctime_ns
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    with pytest.raises(StateError):
        shared_tests._leave(case)
    assert fired
    assert case.backend.writes == writes
    assert stat.S_IMODE(case.roots.runs.stat().st_mode) == 0o710


@pytest.mark.parametrize("remove", [False, True])
def test_completed_shared_leave_replay_does_not_reopen_obsolete_transport(case, monkeypatch, request, remove):
    stage_tests.grant(case)
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    stage_tests.revoke(case)
    completed = shared_tests._leave(case)
    if remove:
        case.transport_path.unlink()
    else:
        case.transport_path.chmod(0o600)
        case.transport_path.write_bytes(b"obsolete transport bytes")
        case.transport_path.chmod(0o400)
    before = case.state.read_bytes(), list(case.backend.writes)
    assert shared_tests._leave(case) == completed
    assert (case.state.read_bytes(), case.backend.writes) == before
