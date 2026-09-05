"""Runtime I/O identities remain bound across planning and libvirt boundaries."""

from __future__ import annotations

import json

import pytest
import test_oci_store as fixtures

from palimpsest_local import oci_root_kvm as plans
from palimpsest_local import oci_root_runtime as runtime
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_runtime_io import runtime_io_paths


@pytest.fixture
def case(tmp_path, monkeypatch):
    values = fixtures._committed_oci_domain(tmp_path, "io-boundary")
    monkeypatch.setattr(runtime.kvm, "_libvirt", lambda: fixtures._FAKE_LIBVIRT)
    return values


def _define(case, conn):
    roots, store, runner, boot, profile, _, _ = case
    return runtime.define_committed_oci_root_domain(
        roots, "io-boundary", store, boot, profile, conn=conn, runner=runner
    )


def _launch(case, conn):
    roots, store, runner, boot, profile, _, _ = case
    return runtime.launch_defined_oci_root_domain(roots, "io-boundary", store, boot, profile, conn=conn, runner=runner)


def test_committed_io_receipt_and_logical_paths_do_not_expose_host_paths(case):
    roots, _, _, _, _, _, plan = case
    run_root = roots.runs / "io-boundary"
    paths = runtime_io_paths(run_root)
    snapshot = state.read_run_ledger_snapshot(roots, "io-boundary")
    receipt = snapshot.state["oci_runtime_io"]
    assert receipt["plan_digest"] == plan.digest
    assert receipt["directory_inode"] == paths.root.stat().st_ino
    assert receipt["console_inode"] == paths.console_log.stat().st_ino
    assert str(run_root) not in json.dumps(dict(receipt))
    assert plan.to_dict()["console"] == {"append": True, "endpoint": "run-private/io/console.log", "transport": "file"}
    assert plan.to_dict()["lifecycle_control"]["endpoint"] == "run-private/io/lifecycle.sock"
    assert not (run_root / "console.log").exists() and not (run_root / "lifecycle.sock").exists()
    assert run_root.stat().st_mode & 0o022 == 0
    for version in ("v14",):
        legacy = plan.to_dict()
        legacy["schema"] = "palimpsest.oci-root-domain-plan." + version
        with pytest.raises(StateError, match="invalidated.*rebuild"):
            plans.OCIRootDomainPlan.from_dict(legacy)


@pytest.mark.parametrize("boundary", ["define", "launch"])
@pytest.mark.parametrize(
    "tamper", ["missing-receipt", "plan-binding", "directory", "console", "socket", "write-parent"]
)
def test_io_drift_is_refused_before_libvirt_mutation(case, monkeypatch, boundary, tamper):
    roots = case[0]
    paths = runtime_io_paths(roots.runs / "io-boundary")
    conn = fixtures._DefinitionConnection()
    if boundary == "launch":
        conn = fixtures._evented_connection(conn, monkeypatch)
        _define(case, conn)
    if tamper in {"missing-receipt", "plan-binding"}:
        with state.locked_existing_run(roots, "io-boundary") as mutation:
            data = mutation.mutable_state()
            if tamper == "missing-receipt":
                data.pop("oci_runtime_io")
            else:
                data["oci_runtime_io"]["plan_digest"] = "sha256:" + "f" * 64
            mutation.write_state(data["status"], data)
    elif tamper == "directory":
        paths.root.rename(paths.root.with_name("old-io"))
        paths.root.mkdir(mode=0o700)
        paths.console_log.touch(mode=0o600)
    elif tamper == "console":
        paths.console_log.rename(paths.console_log.with_name("old-console"))
        paths.console_log.touch(mode=0o600)
    elif tamper == "socket":
        paths.lifecycle_socket.symlink_to(paths.console_log)
    else:
        paths.root.parent.chmod(0o730)
    with pytest.raises(StateError):
        (_define if boundary == "define" else _launch)(case, conn)
    if boundary == "define":
        assert conn.define_calls == 0
    else:
        assert conn.domains["io-boundary"].create_calls == 0


def test_socket_inserted_after_starting_publication_blocks_create(case, monkeypatch):
    conn = fixtures._evented_connection(fixtures._DefinitionConnection(), monkeypatch)
    _define(case, conn)
    domain = conn.domains["io-boundary"]
    paths = runtime_io_paths(case[0].runs / "io-boundary")
    original = state.ExistingRunMutation.write_state
    injected = False

    def publish(self, status, data):
        nonlocal injected
        result = original(self, status, data)
        if status == "starting" and not injected:
            injected = True
            paths.lifecycle_socket.symlink_to(paths.console_log)
        return result

    monkeypatch.setattr(state.ExistingRunMutation, "write_state", publish)
    with pytest.raises(StateError):
        _launch(case, conn)
    assert injected
    assert domain.create_calls == 0


def test_directory_swap_during_name_lookup_blocks_define(case, monkeypatch):
    conn = fixtures._DefinitionConnection()
    paths = runtime_io_paths(case[0].runs / "io-boundary")
    original = runtime._lookup

    def lookup(connection, name):
        result = original(connection, name)
        paths.root.rename(paths.root.with_name("old-io"))
        paths.root.mkdir(mode=0o700)
        paths.console_log.touch(mode=0o600)
        return result

    monkeypatch.setattr(runtime, "_lookup", lookup)
    with pytest.raises(StateError):
        _define(case, conn)
    assert conn.define_calls == 0


@pytest.mark.parametrize("change", ["before-ready", "before-terminal", "append-only"])
def test_synchronous_live_checkpoints_keep_original_console_identity(case, monkeypatch, change):
    conn = fixtures._evented_connection(fixtures._DefinitionConnection(), monkeypatch)
    _define(case, conn)
    domain = conn.domains["io-boundary"]
    foreign = fixtures._DefinedDomain(conn, "untouched-vm", domain.xml)
    foreign.active = 1
    conn.domains[foreign.name] = foreign
    paths = runtime_io_paths(case[0].runs / "io-boundary")
    paths.console_log.write_bytes(b"original guest output\n")
    original_console_inode = paths.console_log.stat().st_ino
    replacement_console_inode = None

    def change_console():
        nonlocal replacement_console_inode
        if change == "append-only":
            paths.console_log.write_bytes(b"untrusted guest output\n")
        else:
            paths.console_log.rename(paths.console_log.with_name("old-console"))
            paths.console_log.touch(mode=0o600)
            paths.console_log.write_bytes(b"replacement console output\n")
            replacement_console_inode = paths.console_log.stat().st_ino

    def handoff(stream, _binding, *, on_ready, session, before_stream_close, **kwargs):
        try:
            if change != "before-terminal":
                change_console()
            on_ready(fixtures._handoff_receipt("ready", boot_attempt_id=session.boot_attempt_id))
            if change == "before-terminal":
                change_console()
            terminal = fixtures.ProcessExit(0, 0, None, fixtures.ProcessExitCategory.EXITED)
            return fixtures._handoff_receipt("terminal", terminal, boot_attempt_id=session.boot_attempt_id)
        finally:
            before_stream_close()
            stream.abort()
            stream.free()

    monkeypatch.setattr(runtime, "complete_initial_lifecycle_handoff", handoff)
    if change == "append-only":
        _launch(case, conn)
        assert state.read_run_ledger_snapshot(case[0], "io-boundary").state["status"] == "exited"
    else:
        with pytest.raises(StateError):
            _launch(case, conn)
        snapshot = state.read_run_ledger_snapshot(case[0], "io-boundary")
        assert snapshot.state["status"] != "exited"
        assert snapshot.state["oci_root_handoff"].get("lifecycle", {}).get("phase") != "terminal"
        assert domain.create_calls == domain.destroy_calls == domain.undefine_calls == 1
        assert "io-boundary" not in conn.domains
        original_console = paths.console_log.with_name("old-console")
        assert original_console.stat().st_ino == original_console_inode
        assert original_console.read_bytes() == b"original guest output\n"
        assert paths.console_log.stat().st_ino == replacement_console_inode
        assert paths.console_log.read_bytes() == b"replacement console output\n"
    assert conn.domains[foreign.name] is foreign and foreign.active == 1
    assert foreign.create_calls == foreign.destroy_calls == foreign.undefine_calls == 0


def test_define_callback_io_swap_cannot_publish_defined_state(case):
    paths = runtime_io_paths(case[0].runs / "io-boundary")
    original_inode = paths.console_log.stat().st_ino

    def swap(xml):
        paths.console_log.rename(paths.console_log.with_name("old-console"))
        paths.console_log.touch(mode=0o600)
        return xml

    conn = fixtures._DefinitionConnection(transform=swap)
    with pytest.raises(StateError):
        _define(case, conn)
    snapshot = state.read_run_ledger_snapshot(case[0], "io-boundary")
    assert snapshot.state["status"] == "creating"
    assert "oci_root_definition" not in snapshot.state
    assert conn.define_calls == 1 and "io-boundary" not in conn.domains
    assert paths.console_log.with_name("old-console").stat().st_ino == original_inode


def test_create_callback_io_swap_cannot_publish_active_starting_state(case, monkeypatch):
    conn = fixtures._evented_connection(fixtures._DefinitionConnection(), monkeypatch)
    _define(case, conn)
    paths = runtime_io_paths(case[0].runs / "io-boundary")
    domain = conn.domains["io-boundary"]
    original_create = domain.create
    original_write = state.ExistingRunMutation.write_state
    published_phases = []

    def create():
        original_create()
        paths.console_log.rename(paths.console_log.with_name("old-console"))
        paths.console_log.touch(mode=0o600)

    def write(self, status, data):
        if "oci_root_handoff" in data:
            published_phases.append(data["oci_root_handoff"]["phase"])
        return original_write(self, status, data)

    monkeypatch.setattr(domain, "create", create)
    monkeypatch.setattr(state.ExistingRunMutation, "write_state", write)
    with pytest.raises(StateError):
        _launch(case, conn)
    assert "activating" in published_phases and "starting" not in published_phases
    assert domain.create_calls == 1 and not domain.open_channel_calls
