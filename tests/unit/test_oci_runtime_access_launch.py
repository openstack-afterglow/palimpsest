"""Real durable traversal/I/O grants carried into private exec authority v4."""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from test_oci_runtime_access import case as case

from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local import oci_runtime_access as access
from palimpsest_local import oci_runtime_io as io
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_acl import baseline_acl, grant_acl, traversal_acl


@pytest.fixture
def granted(case):
    # The shared fixture prepares, commits and defines a real OCI plan against
    # fake libvirt, with a deterministic FD ACL backend (no metadata bypass).
    case.receipt = access.grant_oci_runtime_access(case.roots, case.binding, conn=case.conn)
    (case.paths.root.parent / "monitor-private").mkdir(mode=0o700)
    return case


def _prepare(case):
    return launch.prepare_monitor_launch_authority(case.roots, case.store, case.boot, case.profile, case.binding)


def test_completed_grant_survives_prepared_and_serialized_authority(granted):
    before_writes = list(granted.backend.writes)
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        assert frame["schema"] == "palimpsest.monitor-launch-authority.v4"
        assert frame["runtime_access"] == granted.receipt.to_dict()
        assert frame["runtime_access"]["schema"] == "palimpsest.oci-runtime-access.v2"
        assert frame["runtime_access"]["phase"] == "granted"
        for entry in frame["entries"].values():
            entry["fd"] = os.dup(entry["fd"])
        try:
            child = launch.MonitorLaunchAuthority.from_dict(frame)
        except BaseException:
            for entry in frame["entries"].values():
                os.close(entry["fd"])
            raise
        with child:
            child.validate(binding=granted.binding)
            assert child.to_dict() == frame
            assert set(child.pass_fds).isdisjoint(authority.pass_fds)
        authority.validate()
    assert granted.backend.writes == before_writes
    assert granted.domain.domain.create_calls == granted.domain.domain.destroy_calls == 0


def test_preparation_and_held_run_validation_do_not_reacquire_run_lock(granted, monkeypatch):
    original = state.locked_existing_run
    depth = calls = 0

    @contextmanager
    def counted(*args, **kwargs):
        nonlocal depth, calls
        assert depth == 0, "nested run lock acquisition"
        depth += 1
        calls += 1
        try:
            with original(*args, **kwargs) as mutation:
                yield mutation
        finally:
            depth -= 1

    for module in (state, launch, access):
        monkeypatch.setattr(module, "locked_existing_run", counted)
    with _prepare(granted) as authority:
        assert calls == 1
        authority.validate()
        authority.to_dict()
        assert calls == 1
        with counted(granted.roots, granted.binding.record.name) as mutation:
            with io.runtime_io_guard(mutation, plan_digest=granted.binding.plan_digest) as runtime_io:
                runtime_io.verify(require_socket_absent=True)
                authority.validate()
                frame = authority.to_dict()
                for entry in frame["entries"].values():
                    entry["fd"] = os.dup(entry["fd"])
                try:
                    child = launch.MonitorLaunchAuthority.from_dict(frame)
                except BaseException:
                    for entry in frame["entries"].values():
                        os.close(entry["fd"])
                    raise
                child.close()
        assert calls == 2


@pytest.mark.parametrize(
    "change",
    [
        lambda frame: frame["runtime_access"].update(qemu_uid=12347),
        lambda frame: frame["runtime_access"]["run"].update(inode=True),
        lambda frame: frame["runtime_access"]["run"]["granted"].update(mask="-wx"),
        lambda frame: frame["runtime_access"].pop("run"),
        lambda frame: frame["runtime_access"].update(run=None),
        lambda frame: frame["runtime_access"].update(schema="palimpsest.oci-runtime-access.v1"),
        lambda frame: frame["runtime_access"]["directory"].update(inode=True),
        lambda frame: frame["runtime_access"]["console"].update(inode=1),
        lambda frame: frame["runtime_access"]["runtime_io"].update(plan_digest="sha256:" + "f" * 64),
        lambda frame: frame["runtime_access"]["directory"]["granted"].update(mask="rwx"),
        lambda frame: frame.update(runtime_access=None),
        lambda frame: frame.pop("runtime_access"),
        lambda frame: frame.update(schema="palimpsest.monitor-launch-authority.v2"),
        lambda frame: frame.update(schema="palimpsest.monitor-launch-authority.v3"),
    ],
)
def test_serialized_authority_rejects_tampered_or_omitted_grant(granted, change):
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        change(frame)
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        # A rejected untrusted frame does not close any caller-owned FD.
        authority.validate()


@pytest.mark.parametrize("role", ["run", "runtime_io", "runtime_console"])
def test_serialized_grant_cannot_be_applied_to_another_descriptor(granted, tmp_path, role):
    replacement = tmp_path / "unrelated"
    if role != "runtime_console":
        replacement.mkdir(mode=0o700)
        fd = os.open(replacement, os.O_RDONLY | os.O_DIRECTORY)
    else:
        fd = os.open(replacement, os.O_CREAT | os.O_EXCL | os.O_RDONLY, 0o600)
    try:
        # Keep type, owner, mode and full ACL valid. Only the inherited inode
        # identity differs, so a generic mode/ACL rejection cannot mask this test.
        os.fchmod(fd, {"run": 0o710, "runtime_io": 0o730, "runtime_console": 0o660}[role])
        info = os.fstat(fd)
        target = getattr(granted.receipt, {"run": "run", "runtime_io": "directory", "runtime_console": "console"}[role])
        granted.backend.acls[info.st_dev, info.st_ino] = target.granted
        with _prepare(granted) as authority:
            frame = authority.to_dict()
            frame["entries"][role]["fd"] = fd
            with pytest.raises(StateError):
                launch.MonitorLaunchAuthority.from_dict(frame)
            os.fstat(fd)
            authority.validate()
    finally:
        os.close(fd)


@pytest.mark.parametrize("role", ["run", "directory", "console"])
@pytest.mark.parametrize("drift", ["baseline", "other-user"])
def test_exact_live_acl_is_rechecked_without_mode_only_acceptance(granted, role, drift):
    target = getattr(granted.receipt, role)
    key = target.device, target.inode
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        # Leave granted mode bits intact so only full ACL verification can fail.
        granted.backend.acls[key] = (
            baseline_acl(directory=target.directory)
            if drift == "baseline"
            else (traversal_acl if role == "run" else grant_acl)(target.baseline, granted.receipt.qemu_uid + 1)
        )
        with pytest.raises(StateError):
            authority.validate()
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)


@pytest.mark.parametrize("phase", ["intent", "revoking", "revoked"])
def test_non_granted_phase_cannot_prepare_or_reconstruct_launch(granted, phase):
    with _prepare(granted) as authority:
        frame = authority.to_dict()
        record = granted.receipt.to_dict()
        record["phase"] = phase
        record["cleanup_digest"] = None if phase == "intent" else "sha256:" + "f" * 64
        frame["runtime_access"] = record
        # All inherited descriptors remain valid: refusal must be caused by
        # phase authority, not an unrelated closed-descriptor failure.
        with pytest.raises(StateError):
            launch.MonitorLaunchAuthority.from_dict(frame)
        authority.validate()
    with state.locked_existing_run(granted.roots, granted.binding.record.name) as mutation:
        mutation.write_state("defined", {**mutation.mutable_state(), "oci_runtime_access": record})
    with pytest.raises(StateError):
        _prepare(granted)


@pytest.mark.parametrize("missing", [False, True])
def test_granted_modes_without_trusted_ledger_receipt_cannot_prepare(granted, missing):
    with state.locked_existing_run(granted.roots, granted.binding.record.name) as mutation:
        data = mutation.mutable_state()
        if missing:
            del data["oci_runtime_access"]
        else:
            data["oci_runtime_access"] = None
        mutation.write_state("defined", data)
    with pytest.raises(StateError):
        _prepare(granted)


def test_console_output_remains_untrusted_but_allowed_after_grant(granted):
    with _prepare(granted) as authority:
        before = authority.to_dict()["runtime_access"]
        granted.paths.console_log.write_bytes(b"guest console output must not become a signed ledger\n")
        authority.validate()
        assert authority.to_dict()["runtime_access"] == before
