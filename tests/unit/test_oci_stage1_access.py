"""Explicit read-only ACL authority never weakens sealed stage-1 transport."""

import hashlib
import json
import os
import stat

import pytest
import test_oci_runtime_access as runtime_tests

from palimpsest_local import oci_root_kvm as root_kvm
from palimpsest_local import oci_stage1_access as access
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import grant_acl, readonly_baseline_acl, readonly_grant_acl
from palimpsest_local.oci_monitor import ProcessLiveness
from palimpsest_local.oci_stage1_transport import build_stage1_transport, verify_stage1_transport_file


@pytest.fixture
def case(tmp_path, monkeypatch):
    value = runtime_tests.case.__wrapped__(tmp_path, monkeypatch)
    return _install_stage1_backend(value, monkeypatch)


def _install_stage1_backend(value, monkeypatch):
    value.transport_path = value.roots.runs / value.binding.record.name / "stage1-plan.raw"
    metadata = value.transport_path.stat()
    identity = metadata.st_dev, metadata.st_ino
    original_read = value.backend.read_acl
    original_write = value.backend.write_acl

    def read(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            return value.backend.acls.get(identity, readonly_baseline_acl())
        return original_read(fd)

    def write(fd, acl):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            return original_write(fd, acl)
        value.backend.before_write(fd, acl)
        value.backend.writes.append(("stage1", acl))
        value.backend.acls[identity] = acl
        os.fchmod(fd, 0o440 if acl.named_users else 0o400)
        value.backend.after_write(fd, acl)
        return value.backend.read_acl(fd)

    monkeypatch.setattr(value.backend, "read_acl", read)
    monkeypatch.setattr(value.backend, "write_acl", write)
    monkeypatch.setattr(access, "LinuxFdACLBackend", lambda: value.backend)
    return value


def grant(case, **kwargs):
    return access.grant_oci_stage1_access(case.roots, case.binding, conn=case.conn, **kwargs)


def revoke(case, **kwargs):
    return access.revoke_oci_stage1_access(
        case.roots,
        case.binding,
        conn=case.conn,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def _snapshot(case):
    info = case.transport_path.stat(follow_symlinks=False)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        hashlib.sha256(case.transport_path.read_bytes()).hexdigest(),
        case.state.read_bytes(),
        list(case.backend.writes),
    )


def _legacy_verify(case):
    plan = root_kvm.OCIStage1Plan.from_domain_plan(case.plan)
    transport = build_stage1_transport(plan)
    return verify_stage1_transport_file(case.transport_path, transport.receipt, expected_stage1_plan=plan)


def test_readonly_acl_shape_cannot_be_used_as_mutable_grant():
    baseline = readonly_baseline_acl()
    granted = readonly_grant_acl(12345)
    assert baseline.user == granted.user == "r--"
    assert baseline.named_users == () and baseline.mask is None
    assert granted.named_users == ((12345, "r--"),) and granted.mask == "r--"
    assert granted.group == granted.other == "---"
    with pytest.raises(StateError):
        grant_acl(baseline, 12345)


def test_exact_grant_and_revoke_preserve_sealed_payload_and_original_evidence(case, monkeypatch, request):
    before = _snapshot(case)
    _legacy_verify(case)
    receipt = grant(case)
    assert receipt.phase == "granted"
    assert receipt.binding == case.binding
    assert receipt.transport.artifact_digest == case.binding.stage1_artifact_digest
    assert (receipt.target.device, receipt.target.inode) == before[:2]
    assert receipt.target.baseline == readonly_baseline_acl()
    assert receipt.target.granted == readonly_grant_acl(12345)
    assert access.Stage1AccessReceipt.from_dict(receipt.to_dict()) == receipt
    assert stat.S_IMODE(case.transport_path.stat().st_mode) == 0o440
    assert _snapshot(case)[7] == before[7]
    stable = _snapshot(case)
    assert grant(case) == receipt
    assert _snapshot(case) == stable
    with pytest.raises(StateError):
        _legacy_verify(case)
    assert root_kvm.load_oci_root_domain_plan(case.roots, case.binding.record.name) == case.plan
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    restored = revoke(case)
    assert restored.phase == "revoked"
    assert restored.access_id == receipt.access_id
    assert stat.S_IMODE(case.transport_path.stat().st_mode) == 0o400
    assert _snapshot(case)[7] == before[7]
    stable = _snapshot(case)
    assert revoke(case) == restored
    assert _snapshot(case) == stable
    _legacy_verify(case)


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o660, 0o444, 0o440])
def test_unmanaged_nonbaseline_mode_never_becomes_readonly_authority(case, mode):
    case.transport_path.chmod(mode)
    before = _snapshot(case)
    with pytest.raises(StateError):
        grant(case)
    assert _snapshot(case) == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "replacement", "bytes", "size"])
def test_granted_transport_identity_and_full_payload_remain_immutable(case, damage):
    receipt = grant(case)
    path = case.transport_path
    if damage in {"symlink", "replacement"}:
        original = path.with_suffix(".original")
        path.rename(original)
        if damage == "symlink":
            path.symlink_to(original)
        else:
            path.write_bytes(original.read_bytes())
            path.chmod(0o440)
            info = path.stat()
            case.backend.acls[info.st_dev, info.st_ino] = receipt.target.granted
    elif damage == "hardlink":
        path.with_suffix(".link").hardlink_to(path)
    else:
        path.chmod(0o600)
        with path.open("r+b") as stream:
            if damage == "bytes":
                stream.seek(-1, os.SEEK_END)
                stream.write(b"x")
            else:
                stream.seek(0, os.SEEK_END)
                stream.write(b"x")
        path.chmod(0o440)
    state_before = case.state.read_bytes()
    writes = list(case.backend.writes)
    with pytest.raises(StateError):
        grant(case)
    with pytest.raises(StateError):
        root_kvm.load_oci_root_domain_plan(case.roots, case.binding.record.name)
    assert case.state.read_bytes() == state_before
    assert case.backend.writes == writes


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_interrupted_acl_transition_resumes_exact_owned_phase(case, monkeypatch, request, operation, timing):
    if operation == "revoke":
        grant(case)
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        case.backend.writes.clear()
    action = grant if operation == "grant" else revoke

    def fail(*_args):
        raise OSError("interrupted stage1 ACL write")

    setattr(case.backend, timing + "_write", fail)
    with pytest.raises(StateError):
        action(case)
    saved = json.loads(case.state.read_bytes())["oci_stage1_access"]
    assert saved["phase"] == ("intent" if operation == "grant" else "revoking")
    setattr(case.backend, timing + "_write", lambda *_: None)
    result = action(case)
    assert result.phase == ("granted" if operation == "grant" else "revoked")
    assert result.access_id == saved["access_id"]
    assert len(case.backend.writes) == 1


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_target_fsync_failure_remains_resumable_without_second_acl_write(case, monkeypatch, request, operation):
    if operation == "revoke":
        grant(case)
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        case.backend.writes.clear()
    action = grant if operation == "grant" else revoke
    info = case.transport_path.stat()
    original_fsync = os.fsync

    def fail(fd):
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            raise OSError("stage1 target fsync interrupted")
        return original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(StateError):
            action(case)
    assert json.loads(case.state.read_bytes())["oci_stage1_access"]["phase"] == (
        "intent" if operation == "grant" else "revoking"
    )
    assert action(case).phase == ("granted" if operation == "grant" else "revoked")
    assert len(case.backend.writes) == 1


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_completed_replay_never_fsyncs_immutable_file(case, monkeypatch, request, operation):
    grant(case)
    if operation == "revoke":
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        revoke(case)
    info = case.transport_path.stat()
    original_fsync = os.fsync

    def forbid(fd):
        current = os.fstat(fd)
        assert (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
        return original_fsync(fd)

    before = _snapshot(case)
    monkeypatch.setattr(os, "fsync", forbid)
    (grant if operation == "grant" else revoke)(case)
    assert _snapshot(case) == before


@pytest.mark.parametrize("liveness", [ProcessLiveness.LIVE, ProcessLiveness.UNKNOWN])
def test_terminal_cleanup_still_requires_original_writer_stale(case, monkeypatch, request, liveness):
    grant(case)
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    before = _snapshot(case)
    with pytest.raises(StateError):
        revoke(case, liveness_probe=lambda _: liveness)
    assert _snapshot(case) == before


def test_preterminal_revoke_is_refused_without_changes(case):
    grant(case)
    before = _snapshot(case)
    with pytest.raises(StateError):
        revoke(case)
    assert _snapshot(case) == before


@pytest.mark.parametrize("damage", ["missing", "null", "wrong-attempt", "wrong-inode"])
def test_managed_receipt_loss_or_rebinding_cannot_adopt_granted_mode(case, damage):
    grant(case)
    with state.locked_existing_run(case.roots, case.binding.record.name) as mutation:
        data = mutation.mutable_state()
        if damage == "missing":
            data.pop("oci_stage1_access")
        elif damage == "null":
            data["oci_stage1_access"] = None
        elif damage == "wrong-attempt":
            data["oci_stage1_access"]["binding"]["boot_attempt_id"] = "00000000-0000-4000-8000-000000000003"
        else:
            data["oci_stage1_access"]["target"]["inode"] += 1
        mutation.write_state("defined", data)
    before = _snapshot(case)
    with pytest.raises(StateError):
        grant(case)
    if damage != "wrong-attempt":
        # The generic plan loader has no independently selected boot attempt;
        # grant and launch compare the receipt to the caller's exact binding.
        with pytest.raises(StateError):
            root_kvm.load_oci_root_domain_plan(case.roots, case.binding.record.name)
    assert _snapshot(case) == before


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_wrong_principal_acl_is_refused_with_unchanged_readonly_mode(case, monkeypatch, request, operation):
    receipt = grant(case)
    if operation == "revoke":
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
    case.backend.acls[receipt.target.device, receipt.target.inode] = readonly_grant_acl(receipt.qemu_uid + 1)
    before = _snapshot(case)
    with pytest.raises(StateError):
        (grant if operation == "grant" else revoke)(case)
    assert _snapshot(case) == before


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("damage", ["member", "payload"])
def test_final_acl_callback_cannot_mutate_member_or_payload_at_replay_tail(
    case, monkeypatch, request, operation, damage
):
    receipt = grant(case)
    if operation == "revoke":
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        revoke(case)
    action = grant if operation == "grant" else revoke
    original_read = case.backend.read_acl
    reads = 0

    def count(fd):
        nonlocal reads
        value = original_read(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (receipt.target.device, receipt.target.inode):
            reads += 1
        return value

    monkeypatch.setattr(case.backend, "read_acl", count)
    action(case)
    last_read = reads
    assert last_read > 0
    reads = 0
    fired = False

    def mutate(fd):
        nonlocal fired
        value = count(fd)
        if reads == last_read and not fired:
            fired = True
            if damage == "member":
                data = json.loads(case.state.read_bytes())
                data.pop("oci_stage1_access")
                case.state.write_text(json.dumps(data))
            else:
                mode = stat.S_IMODE(case.transport_path.stat().st_mode)
                case.transport_path.chmod(0o600)
                with case.transport_path.open("r+b") as stream:
                    stream.seek(-1, os.SEEK_END)
                    stream.write(b"x")
                case.transport_path.chmod(mode)
        return value

    monkeypatch.setattr(case.backend, "read_acl", mutate)
    with pytest.raises(StateError):
        action(case)
    assert fired


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_later_libvirt_callback_cannot_change_same_mode_stage1_acl(case, monkeypatch, request, operation):
    receipt = grant(case)
    if operation == "revoke":
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
    original_read = case.backend.read_acl
    original_uri = case.conn.getURI
    reads = 0
    fired = False

    def read(fd):
        nonlocal reads
        result = original_read(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (receipt.target.device, receipt.target.inode):
            reads += 1
        return result

    def uri():
        nonlocal fired
        result = original_uri()
        # Initial ACL selection is read1; read2 is the full immutable check.
        # This is the later libvirt callback after that check returned.
        if reads >= 2 and not fired:
            fired = True
            before = case.transport_path.stat()
            assert stat.S_IMODE(before.st_mode) == 0o440
            case.backend.acls[receipt.target.device, receipt.target.inode] = readonly_grant_acl(receipt.qemu_uid + 1)
            os.chmod(case.transport_path, 0o440)
            assert case.transport_path.stat().st_ctime_ns != before.st_ctime_ns
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    monkeypatch.setattr(case.conn, "getURI", uri)
    with pytest.raises(StateError):
        (grant if operation == "grant" else revoke)(case)
    assert fired
