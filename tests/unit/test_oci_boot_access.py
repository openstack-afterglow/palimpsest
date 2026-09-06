"""The sealed kernel/initramfs pair has one exact read-only lifecycle."""

import hashlib
import json
import os
import stat
import uuid
import xml.etree.ElementTree as ET

import pytest
import test_oci_boot_exports as export_tests
import test_oci_monitor_recovery as recovery_tests
import test_oci_root_access as root_tests
import test_oci_runtime_access as runtime_tests
import test_oci_shared_traversal as shared_tests
import test_oci_stage1_access as stage_tests
import test_oci_store as fixtures

from palimpsest_local import oci_boot_access as access
from palimpsest_local import oci_root_access as root_access
from palimpsest_local import oci_shared_traversal as shared
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import readonly_baseline_acl, readonly_grant_acl
from palimpsest_local.oci_monitor import ProcessLiveness


@pytest.fixture
def case(tmp_path, monkeypatch):
    value = export_tests.case.__wrapped__(tmp_path)
    value.export_receipt = export_tests.publish(value)
    value.boot = export_tests.load(value)
    preview = fixtures.build_oci_root_domain_plan(
        value.roots,
        value.prepared,
        value.store,
        value.boot,
        value.profile,
        runner=value.runner,
    )
    value.plan = fixtures.commit_oci_root_domain_plan(value.roots, preview, value.store, runner=value.runner)
    raw = value.conn
    capabilities = raw.getCapabilities
    monkeypatch.setattr(runtime_tests.runtime.kvm, "_libvirt", lambda: fixtures._FAKE_LIBVIRT)
    runtime_tests.runtime.define_committed_oci_root_domain(
        value.roots,
        value.name,
        value.store,
        value.boot,
        value.profile,
        conn=raw,
        runner=value.runner,
    )
    original = raw.domains[value.name]
    xml = ET.fromstring(original.xml)
    ET.SubElement(xml, "uuid").text = original.domain_uuid
    original.xml = ET.tostring(xml, encoding="unicode")
    value.binding = runtime_tests.runtime.prepare_oci_root_monitor_binding(
        value.roots,
        value.name,
        value.store,
        value.boot,
        value.profile,
        conn=raw,
        boot_attempt_id=str(uuid.uuid4()),
        runner=value.runner,
    )
    value.domain = recovery_tests._Domain(original)
    value.conn = recovery_tests._Connection(raw, value.domain)
    value.conn.getCapabilities = capabilities
    value.paths = runtime_tests.io.runtime_io_paths(value.run_root)
    value.backend = runtime_tests.FakeACL()
    info = value.run_root.stat()
    value.backend.run_identity = info.st_dev, info.st_ino
    originals = value.backend.read_acl, value.backend.write_acl
    identities = {export_tests._snapshot(path)[:2]: role for role, path in value.export_paths.items()}

    def read(fd):
        metadata = os.fstat(fd)
        identity = metadata.st_dev, metadata.st_ino
        if identity in identities:
            return value.backend.acls.get(identity, readonly_baseline_acl())
        return originals[0](fd)

    def write(fd, acl):
        metadata = os.fstat(fd)
        identity = metadata.st_dev, metadata.st_ino
        if identity not in identities:
            return originals[1](fd, acl)
        value.backend.before_write(fd, acl)
        value.backend.writes.append((identities[identity], acl))
        value.backend.acls[identity] = acl
        os.fchmod(fd, 0o440 if acl.named_users else 0o400)
        value.backend.after_write(fd, acl)
        return value.backend.read_acl(fd)

    monkeypatch.setattr(value.backend, "read_acl", read)
    monkeypatch.setattr(value.backend, "write_acl", write)
    monkeypatch.setattr(access, "LinuxFdACLBackend", lambda: value.backend)
    monkeypatch.setattr(runtime_tests.access, "LinuxFdACLBackend", lambda: value.backend)
    return value


def grant(case, **kwargs):
    return access.grant_oci_boot_access(case.roots, case.binding, conn=case.conn, **kwargs)


def revoke(case, **kwargs):
    return access.revoke_oci_boot_access(
        case.roots,
        case.binding,
        conn=case.conn,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def snapshot(case):
    return (
        {role: export_tests._snapshot(path) for role, path in case.export_paths.items()},
        case.state.read_bytes(),
        list(case.backend.writes),
    )


def test_pair_grant_and_revoke_preserve_payload_sources_and_export_identity(case, monkeypatch, request):
    sources = {role: export_tests._snapshot(getattr(case.source_boot, role).path) for role in case.export_paths}
    before = snapshot(case)[0]
    receipt = grant(case)
    assert receipt.phase == "granted" and receipt.binding == case.binding
    assert receipt.exports == case.export_receipt
    assert access.BootAccessReceipt.from_dict(receipt.to_dict()) == receipt
    assert len(case.backend.writes) == 2
    for role, path in case.export_paths.items():
        assert export_tests._snapshot(path)[:2] == before[role][:2]
        assert path.read_bytes() == before[role][-1]
        assert stat.S_IMODE(path.stat().st_mode) == 0o440
        assert export_tests._snapshot(getattr(case.source_boot, role).path) == sources[role]
    assert export_tests.load(case).to_dict() == case.boot.to_dict()
    stable = snapshot(case)
    assert grant(case) == receipt
    assert snapshot(case) == stable
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    restored = revoke(case)
    assert restored.phase == "revoked" and restored.access_id == receipt.access_id
    assert len(case.backend.writes) == 4
    for role, path in case.export_paths.items():
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert path.read_bytes() == before[role][-1]
    stable = snapshot(case)
    assert revoke(case) == restored
    assert snapshot(case) == stable


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("role", ["kernel", "initramfs"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_interrupted_pair_acl_transition_resumes_without_rewriting_completed_member(
    case, monkeypatch, request, operation, role, timing
):
    if operation == "revoke":
        grant(case)
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        case.backend.writes.clear()
    action = grant if operation == "grant" else revoke
    identity = export_tests._snapshot(case.export_paths[role])[:2]
    fired = False

    def fail(fd, _acl):
        nonlocal fired
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            fired = True
            raise OSError("pair ACL transition interrupted")

    setattr(case.backend, timing + "_write", fail)
    with pytest.raises(StateError):
        action(case)
    assert fired
    saved = json.loads(case.state.read_bytes())["oci_boot_access"]
    assert saved["phase"] == ("intent" if operation == "grant" else "revoking")
    setattr(case.backend, timing + "_write", lambda *_: None)
    result = action(case)
    assert result.access_id == saved["access_id"]
    assert result.phase == ("granted" if operation == "grant" else "revoked")
    assert sorted(name for name, _ in case.backend.writes) == ["initramfs", "kernel"]


@pytest.mark.parametrize("operation", ["grant", "revoke"])
def test_completed_replay_does_not_fsync_either_boot_file(case, monkeypatch, request, operation):
    grant(case)
    if operation == "revoke":
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        revoke(case)
    identities = {export_tests._snapshot(path)[:2] for path in case.export_paths.values()}
    original = os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) not in identities
        return original(fd)

    before = snapshot(case)
    monkeypatch.setattr(os, "fsync", fsync)
    (grant if operation == "grant" else revoke)(case)
    assert snapshot(case) == before


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("role", ["kernel", "initramfs"])
def test_pair_transition_fsync_failure_keeps_pending_evidence(case, monkeypatch, request, operation, role):
    if operation == "revoke":
        grant(case)
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
    action = grant if operation == "grant" else revoke
    original = os.fsync
    identity = export_tests._snapshot(case.export_paths[role])[:2]
    fired = False

    def fsync(fd):
        nonlocal fired
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            fired = True
            raise OSError("pair grant durability interrupted")
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(StateError):
            action(case)
    assert fired
    saved = json.loads(case.state.read_bytes())["oci_boot_access"]
    assert saved["phase"] == ("intent" if operation == "grant" else "revoking")
    receipt = action(case)
    assert receipt.access_id == saved["access_id"]
    assert receipt.phase == ("granted" if operation == "grant" else "revoked")


def test_pair_revoke_before_terminal_cleanup_cannot_restore_acl(case):
    grant(case)
    before = snapshot(case)
    with pytest.raises(StateError):
        revoke(case)
    assert snapshot(case) == before


@pytest.mark.parametrize("liveness", [ProcessLiveness.LIVE, ProcessLiveness.UNKNOWN])
def test_pair_revoke_requires_original_stale_writer_even_after_cleanup(case, monkeypatch, request, liveness):
    grant(case)
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    before = snapshot(case)
    with pytest.raises(StateError):
        revoke(case, liveness_probe=lambda _: liveness)
    assert snapshot(case) == before


@pytest.mark.parametrize("change", ["explicit-uid", "capabilities", "relabel", "dynamic", "label"])
def test_publication_principal_and_exact_static_no_relabel_policy_are_not_replaceable(case, change):
    kwargs = {}
    if change == "explicit-uid":
        kwargs["qemu_uid"] = 12347
    elif change == "capabilities":
        original = case.conn.getCapabilities()
        case.conn.getCapabilities = lambda: original.replace("+12345:+12346", "+12347:+12346")
    else:
        domain = case.domain.domain
        xml = ET.fromstring(domain.xml)
        label = xml.find("seclabel")
        assert label is not None
        assert label.attrib == {"type": "static", "model": "dac", "relabel": "no"}
        if change == "relabel":
            label.set("relabel", "yes")
        elif change == "dynamic":
            label.set("type", "dynamic")
        else:
            label.find("label").text = "+12347:+12346"
        domain.xml = ET.tostring(xml, encoding="unicode")
    before = snapshot(case)
    with pytest.raises(StateError):
        grant(case, **kwargs)
    assert snapshot(case) == before


@pytest.mark.parametrize("damage", ["payload", "same-mode-acl"])
def test_last_initramfs_acl_callback_cannot_change_already_checked_kernel(case, monkeypatch, damage):
    grant(case)
    kernel = case.export_paths["kernel"]
    identity = export_tests._snapshot(case.export_paths["initramfs"])[:2]
    original = case.backend.read_acl
    count = 0

    def read(fd):
        nonlocal count
        result = original(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            count += 1
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    grant(case)
    last, count = count, 0
    assert last > 0
    fired = False

    def mutate(fd):
        nonlocal fired
        result = read(fd)
        if count == last and not fired:
            fired = True
            before = kernel.stat()
            if damage == "payload":
                kernel.chmod(0o600)
                with kernel.open("r+b") as stream:
                    stream.seek(0)
                    stream.write(b"x")
            else:
                case.backend.acls[before.st_dev, before.st_ino] = readonly_grant_acl(12347)
            os.chmod(kernel, 0o440)
            assert kernel.stat().st_ctime_ns != before.st_ctime_ns
        return result

    monkeypatch.setattr(case.backend, "read_acl", mutate)
    with pytest.raises(StateError):
        grant(case)
    assert fired


@pytest.fixture
def shared_case(case, monkeypatch):
    original = case.backend.write_acl
    identities = {
        (path.stat().st_dev, path.stat().st_ino): role
        for role, path in (
            ("state", case.roots.state),
            ("runs", case.roots.runs),
            ("root_volumes", case.roots.oci_root_volumes),
        )
    }

    def write(fd, acl):
        info = os.fstat(fd)
        role = identities.get((info.st_dev, info.st_ino))
        if role is None:
            return original(fd, acl)
        case.backend.before_write(fd, acl)
        case.backend.acls[info.st_dev, info.st_ino] = acl
        case.backend.writes.append((role, acl))
        os.fchmod(fd, 0o710 if acl.named_users else 0o700)
        case.backend.after_write(fd, acl)
        return case.backend.read_acl(fd)

    monkeypatch.setattr(case.backend, "write_acl", write)
    monkeypatch.setattr(shared, "LinuxFdACLBackend", lambda: case.backend)
    monkeypatch.setattr(root_access, "LinuxFdACLBackend", lambda: case.backend)
    runtime_tests._grant(case)
    return case


@pytest.mark.parametrize("member", ["present", "missing", "null"])
def test_shared_departure_requires_restored_boot_pair(shared_case, monkeypatch, request, member):
    case = shared_case
    receipt = grant(case)
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    if member != "present":
        data = json.loads(case.state.read_bytes())
        if member == "missing":
            data.pop("oci_boot_access")
        else:
            data["oci_boot_access"] = None
        case.state.write_text(json.dumps(data))
    before = snapshot(case)
    with pytest.raises(StateError):
        shared_tests._leave(case)
    assert snapshot(case) == before
    if member != "present":
        data = json.loads(case.state.read_bytes())
        data["oci_boot_access"] = receipt.to_dict()
        case.state.write_text(json.dumps(data))
    assert revoke(case).phase == "revoked"
    assert shared_tests._leave(case).phase == "left"


@pytest.mark.parametrize("later", ["root", "stage1"])
@pytest.mark.parametrize("damage", ["bytes", "same-mode-acl"])
def test_shared_departure_later_target_callback_cannot_change_boot_pair(
    shared_case, monkeypatch, request, later, damage
):
    case = shared_case
    grant(case)
    if later == "root":
        target = root_tests.grant(case).target
    else:
        stage_tests._install_stage1_backend(case, monkeypatch)
        target = stage_tests.grant(case).target
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    revoke(case)
    (root_tests.revoke if later == "root" else stage_tests.revoke)(case)
    original = case.backend.read_acl
    fired = False
    writes = list(case.backend.writes)

    def read(fd):
        nonlocal fired
        result = original(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (target.device, target.inode) and not fired:
            fired = True
            kernel = case.export_paths["kernel"]
            before = kernel.stat()
            if damage == "same-mode-acl":
                case.backend.acls[before.st_dev, before.st_ino] = readonly_grant_acl(case.export_receipt.qemu_uid)
            else:
                kernel.chmod(0o600)
                with kernel.open("r+b") as stream:
                    stream.write(b"x")
            kernel.chmod(0o400)
            assert kernel.stat().st_ctime_ns != before.st_ctime_ns
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    with pytest.raises(StateError):
        shared_tests._leave(case)
    assert fired
    assert case.backend.writes == writes
    assert stat.S_IMODE(case.roots.runs.stat().st_mode) == 0o710


@pytest.mark.parametrize("remove", [False, True])
def test_completed_shared_departure_does_not_reopen_obsolete_boot_pair(shared_case, monkeypatch, request, remove):
    case = shared_case
    grant(case)
    shared_tests._join(case)
    shared_tests._finish(case, monkeypatch, request)
    revoke(case)
    completed = shared_tests._leave(case)
    for path in case.export_paths.values():
        if remove:
            path.unlink()
        else:
            path.chmod(0o600)
            path.write_bytes(b"obsolete historical export")
            path.chmod(0o400)
    before = case.state.read_bytes(), list(case.backend.writes)
    assert shared_tests._leave(case) == completed
    assert (case.state.read_bytes(), case.backend.writes) == before


def test_boot_loader_rechecks_preparation_owner_after_acl_callback(case, monkeypatch):
    grant(case)
    original = case.backend.read_acl
    fired = False

    def read(fd):
        nonlocal fired
        result = original(fd)
        if not fired:
            fired = True
            data = json.loads(case.state.read_bytes())
            transaction = data["oci_root"]
            transaction["boot_plan"]["run"]["run_id"] = "00000000-0000-4000-8000-000000000004"
            transaction["boot_plan_digest"] = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(transaction["boot_plan"], sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            )
            case.state.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(case.backend, "read_acl", read)
    with pytest.raises(StateError):
        export_tests.load(case)
    assert fired
