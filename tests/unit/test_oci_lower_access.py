"""One exact read-only lifecycle for the distinct run-owned lower images."""

import hashlib
import json
import os
import stat

import pytest
import test_oci_boot_access as boot_tests
import test_oci_lower_exports as export_tests
import test_oci_runtime_access as runtime_tests
import test_oci_store as fixtures

from palimpsest_local import oci_lower_access as access
from palimpsest_local import oci_lower_exports as exports
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import readonly_baseline_acl, readonly_grant_acl
from palimpsest_local.oci_monitor import ProcessLiveness


@pytest.fixture
def case(tmp_path, monkeypatch):
    build = fixtures.build_oci_root_domain_plan
    materialize = fixtures._image_materialization

    def distinct(store):
        monkeypatch.setattr(fixtures, "_image_materialization", materialize)
        return export_tests.two_distinct_materialization(store)

    monkeypatch.setattr(fixtures, "_image_materialization", distinct)

    def with_lowers(roots, prepared, store, *args, **kwargs):
        # Existing fixture publishes BOOT immediately before building the plan.
        conn = fixtures._DefinitionConnection()
        conn.getCapabilities = lambda: (
            "<capabilities><host><secmodel><model>dac</model><doi>0</doi>"
            '<baselabel type="kvm">+12345:+12346</baselabel></secmodel></host></capabilities>'
        )
        exports.publish_oci_lower_exports(roots, prepared, store, conn=conn)
        return build(roots, prepared, store, *args, **kwargs)

    monkeypatch.setattr(fixtures, "build_oci_root_domain_plan", with_lowers)
    value = boot_tests.case.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(fixtures, "build_oci_root_domain_plan", build)
    value.lower_paths = exports.load_oci_lower_exports(value.roots, value.name)
    assert len(value.lower_paths) == 2
    value.lower_exports = exports.LowerExportReceipt.from_dict(
        json.loads(value.state.read_bytes())["oci_lower_exports"]
    )
    identities = {(path.stat().st_dev, path.stat().st_ino): digest for digest, path in value.lower_paths.items()}
    read, write = value.backend.read_acl, value.backend.write_acl

    def read_lower(fd):
        info = os.fstat(fd)
        identity = info.st_dev, info.st_ino
        return value.backend.acls.get(identity, readonly_baseline_acl()) if identity in identities else read(fd)

    def write_lower(fd, acl):
        info = os.fstat(fd)
        identity = info.st_dev, info.st_ino
        if identity not in identities:
            return write(fd, acl)
        value.backend.before_write(fd, acl)
        value.backend.writes.append((identities[identity], acl))
        value.backend.acls[identity] = acl
        os.fchmod(fd, 0o440 if acl.named_users else 0o400)
        value.backend.after_write(fd, acl)
        return read_lower(fd)

    monkeypatch.setattr(value.backend, "read_acl", read_lower)
    monkeypatch.setattr(value.backend, "write_acl", write_lower)
    monkeypatch.setattr(access, "LinuxFdACLBackend", lambda: value.backend)
    return value


def grant(case):
    return access.grant_oci_lower_access(case.roots, case.binding, conn=case.conn)


def revoke(case, **kwargs):
    return access.revoke_oci_lower_access(
        case.roots,
        case.binding,
        conn=case.conn,
        liveness_probe=kwargs.pop("liveness_probe", lambda _: ProcessLiveness.STALE),
        **kwargs,
    )


def test_grant_revoke_and_completed_replay_preserve_exact_lower_payload(case, monkeypatch, request):
    before = {digest: (path.stat().st_ino, path.read_bytes()) for digest, path in case.lower_paths.items()}
    receipt = grant(case)
    assert receipt.phase == "granted"
    assert access.LowerAccessReceipt.from_dict(receipt.to_dict()) == receipt
    stable = case.state.read_bytes(), list(case.backend.writes)
    assert grant(case) == receipt
    assert (case.state.read_bytes(), case.backend.writes) == stable
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    restored = revoke(case)
    assert restored.phase == "revoked"
    assert restored.access_id == receipt.access_id
    stable = case.state.read_bytes(), list(case.backend.writes)
    assert revoke(case) == restored
    assert (case.state.read_bytes(), case.backend.writes) == stable
    for digest, path in case.lower_paths.items():
        assert (path.stat().st_ino, path.read_bytes()) == before[digest]
        assert stat.S_IMODE(path.stat().st_mode) == 0o400


@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_partial_acl_prefix_resumes_only_recorded_transition(case, monkeypatch, request, operation, timing):
    if operation == "revoke":
        grant(case)
        runtime_tests._terminal_cleanup(case, monkeypatch, request)
        case.backend.writes.clear()
    action = grant if operation == "grant" else revoke

    def fail(*_args):
        raise OSError("interrupted lower ACL")

    setattr(case.backend, timing + "_write", fail)
    with pytest.raises(StateError):
        action(case)
    saved = json.loads(case.state.read_bytes())["oci_lower_access"]
    assert saved["phase"] == ("intent" if operation == "grant" else "revoking")
    setattr(case.backend, timing + "_write", lambda *_: None)
    assert action(case).access_id == saved["access_id"]
    assert len(case.backend.writes) == len(case.lower_paths)


def test_live_writer_cannot_revoke_after_cleanup(case, monkeypatch, request):
    grant(case)
    runtime_tests._terminal_cleanup(case, monkeypatch, request)
    before = case.state.read_bytes(), list(case.backend.writes)
    with pytest.raises(StateError):
        revoke(case, liveness_probe=lambda _: ProcessLiveness.LIVE)
    assert (case.state.read_bytes(), case.backend.writes) == before


@pytest.mark.parametrize("damage", ["principal", "bytes"])
def test_late_libvirt_callback_cannot_change_a_checked_lower(case, monkeypatch, damage):
    grant(case)
    path = next(iter(case.lower_paths.values()))
    identity = path.stat().st_dev, path.stat().st_ino
    original = case.conn.getURI
    calls = 0

    def count():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(case.conn, "getURI", count)
    grant(case)
    trigger = calls
    calls = 0

    def mutate():
        result = count()
        if calls == trigger:
            if damage == "principal":
                case.backend.acls[identity] = readonly_grant_acl(12347)
                path.chmod(0o440)
            else:
                path.chmod(0o600)
                with path.open("r+b") as stream:
                    stream.write(b"x")
                path.chmod(0o440)
        return result

    monkeypatch.setattr(case.conn, "getURI", mutate)
    with pytest.raises(StateError):
        grant(case)


def test_generic_load_rechecks_current_preparation_after_acl_callback(case, monkeypatch):
    grant(case)
    original = case.backend.read_acl
    fired = False

    def mutate(fd):
        nonlocal fired
        result = original(fd)
        if not fired:
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
            fired = True
        return result

    monkeypatch.setattr(case.backend, "read_acl", mutate)
    with pytest.raises(StateError):
        exports.load_oci_lower_exports(case.roots, case.name)
    assert fired


@pytest.mark.parametrize("damage", ["bytes", "principal"])
def test_last_lower_acl_callback_cannot_modify_already_checked_first_lower(case, monkeypatch, damage):
    from palimpsest_local.state import locked_existing_run

    grant(case)
    first, last = case.lower_paths.values()
    first_identity = first.stat().st_dev, first.stat().st_ino
    last_identity = last.stat().st_dev, last.stat().st_ino
    original = case.backend.read_acl
    fired = False

    def mutate(fd):
        nonlocal fired
        result = original(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == last_identity:
            if damage == "principal":
                case.backend.acls[first_identity] = readonly_grant_acl(12347)
                first.chmod(0o440)
            else:
                first.chmod(0o600)
                with first.open("r+b") as stream:
                    stream.write(b"x")
                first.chmod(0o440)
            fired = True
        return result

    monkeypatch.setattr(case.backend, "read_acl", mutate)
    with locked_existing_run(case.roots, case.name) as mutation:
        with exports._pinned_pair(mutation._run_fd, case.lower_exports) as descriptors:
            with pytest.raises(StateError):
                access._verify_pair(
                    mutation._run_fd, case.lower_exports, descriptors, dict.fromkeys(descriptors, 0o440), case.backend
                )
    assert fired
