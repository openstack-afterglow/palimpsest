"""The public OCI adapter orders real run reservation around narrow host seams."""

import os
import signal
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_oci_store as fixtures

from palimpsest_local import oci_run_adapter as adapter
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_host import OCIHostConfig
from palimpsest_local.oci_monitor_ipc import MonitorPreActivationBinding
from palimpsest_local.oci_run_request import LocalOCIRunRequest
from palimpsest_local.oci_store import OCIStore
from palimpsest_local.runtime_types import ProcessExit, ProcessExitCategory

_GRANTS = (
    "grant_oci_runtime_access",
    "join_oci_shared_traversal",
    "grant_oci_root_access",
    "grant_oci_stage1_access",
    "grant_oci_boot_access",
    "grant_oci_lower_access",
)


@pytest.fixture
def case(tmp_path, monkeypatch):
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    packer = tmp_path / "packer"
    packer.write_bytes(b"qualified test packer")
    request = LocalOCIRunRequest("local-test", tmp_path / "oci-layout", root_size_bytes=fixtures._ROOT_VOLUME_SIZE)
    config = OCIHostConfig(
        tmp_path / "kernel", "sha256:" + "1" * 64, tmp_path / "kernel.config", "sha256:" + "2" * 64, packer
    )
    value = SimpleNamespace(
        roots=roots,
        request=request,
        config=config,
        calls=[],
        terminal=None,
        endpoint=object(),
        session=object(),
        authority=object(),
        toolchain=object(),
        source_boot=object(),
        boot=object(),
        plan=object(),
    )

    def note(name, result=None):
        def operation(*args, **kwargs):
            value.calls.append(name)
            return result

        return operation

    value.note = note
    monkeypatch.setattr(adapter, "preflight_oci_host", note("preflight"))
    monkeypatch.setattr(adapter, "discover_squashfs_toolchain", note("toolchain", value.toolchain))
    value.profile = SimpleNamespace(uri="qemu:///system")
    monkeypatch.setattr(adapter.platforms, "resolve_domain_profile", note("profile", value.profile))

    def initialize(selected):
        value.calls.append("init")
        assert selected == roots
        return state.init_resolved_roots(selected)

    monkeypatch.setattr(adapter, "init_resolved_roots", initialize)

    def materialize(selected, **kwargs):
        value.calls.append("materialize")
        assert selected == value.request and kwargs == {
            "roots": roots,
            "packer_path": packer,
            "toolchain": value.toolchain,
        }
        assert not (roots.runs / request.name).exists()
        return SimpleNamespace(receipt=fixtures._image_materialization(OCIStore(roots)))

    monkeypatch.setattr(adapter, "materialize_local_oci_run", materialize)
    value.conn = SimpleNamespace(close=note("close"))
    monkeypatch.setattr(adapter, "connect_oci_root_libvirt", note("connect", value.conn))

    @contextmanager
    def boot(*args):
        value.calls.append("boot-enter")
        try:
            yield value.source_boot
        finally:
            value.calls.append("boot-exit")

    monkeypatch.setattr(adapter, "first_party_boot", boot)

    @contextmanager
    def reserve(selected_roots, name, dispatch):
        value.calls.append("reserve")
        with state.reserve_new_run(selected_roots, name, dispatch) as reservation:
            yield reservation

    monkeypatch.setattr(adapter, "reserve_new_run", reserve)

    def prepare(reservation, receipt, store, **kwargs):
        value.calls.append("prepare")
        assert kwargs == {"root_volume_size_bytes": request.root_size_bytes, "retention_policy": "delete"}
        value.prepared = fixtures.prepare_oci_root_run(
            reservation, receipt, store, runner=fixtures._RootVolumeTools(), **kwargs
        )
        return value.prepared

    monkeypatch.setattr(adapter, "prepare_oci_root_run", prepare)
    monkeypatch.setattr(adapter, "publish_oci_boot_exports", note("boot-publish"))
    monkeypatch.setattr(adapter, "load_oci_boot_exports", note("boot-load", value.boot))
    monkeypatch.setattr(adapter, "publish_oci_lower_exports", note("lower-publish"))

    def build(*args, **kwargs):
        value.calls.append("plan")
        assert kwargs == {"memory_mib": request.memory_mib, "vcpus": request.vcpus, "network": None}
        return value.plan

    monkeypatch.setattr(adapter, "build_oci_root_domain_plan", build)
    monkeypatch.setattr(adapter, "commit_oci_root_domain_plan", note("commit-plan"))
    monkeypatch.setattr(adapter, "define_committed_oci_root_domain", note("define"))

    def binding(*args, **kwargs):
        value.calls.append("binding")
        value.record = state.read_run_ledger_snapshot(roots, request.name).record
        directory = roots.runs / request.name / "monitor-private"
        assert directory.is_dir() and directory.stat().st_mode & 0o777 == 0o700
        value.binding = MonitorPreActivationBinding(
            value.record,
            os.geteuid(),
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            "sha256:" + "5" * 64,
            str(uuid.uuid4()),
            kwargs["boot_attempt_id"],
            "qemu:///system",
        )
        return value.binding

    monkeypatch.setattr(adapter, "prepare_oci_root_monitor_binding", binding)
    for name in _GRANTS:
        monkeypatch.setattr(adapter, name, note(name))
    monkeypatch.setattr(adapter, "verify_runtime_parent", note("parent"))

    @contextmanager
    def authority(*args, **kwargs):
        assert kwargs == {"timeout_seconds": 60, "terminal_timeout_seconds": None}
        value.calls.append("authority-enter")
        try:
            yield value.authority
        finally:
            value.calls.append("authority-exit")

    monkeypatch.setattr(adapter, "prepare_monitor_launch_authority", authority)

    def spawn(identity, authority, *, timeout):
        assert identity.binding == value.binding and authority is value.authority and timeout == 15
        value.calls.append("spawn")
        return value.endpoint

    monkeypatch.setattr(adapter, "spawn_monitor_coordinator", spawn)

    class Client:
        def __init__(self, selected_roots, binding, endpoint):
            assert selected_roots == roots and binding == value.binding and endpoint is value.endpoint
            value.calls.append("client")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            value.calls.append("client-close")

        def wait_ready(self, *, timeout):
            assert timeout == 75
            value.calls.append("ready")
            return SimpleNamespace(terminal=value.terminal)

        def stop_and_wait(self, *, timeout):
            assert timeout == 35
            value.calls.append("stop-wait")
            return value.terminal

    monkeypatch.setattr(adapter, "MonitorClient", Client)
    monkeypatch.setattr(adapter, "OCIMonitorProcessSession", note("session", value.session))
    return value


def launch(case):
    return adapter.run_local_oci(case.roots, case.request, case.config)


@pytest.mark.parametrize("detached", [False, True])
def test_pipeline_prepares_exact_run_before_six_grants_and_waits_for_ready(case, detached):
    case.request = replace(case.request, detached=detached)
    result = launch(case)
    expected = [
        "preflight",
        "toolchain",
        "profile",
        "init",
        "materialize",
        "connect",
        "boot-enter",
        "reserve",
        "prepare",
        "boot-publish",
        "boot-load",
        "lower-publish",
        "plan",
        "commit-plan",
        "define",
        "binding",
        *_GRANTS,
        "parent",
        "authority-enter",
        "spawn",
        "authority-exit",
        "boot-exit",
        "close",
        "client",
        "ready",
        "client-close",
    ]
    assert case.calls == expected + ([] if detached else ["session"])
    assert result.record == case.record and result.endpoint is case.endpoint
    assert result.session is (None if detached else case.session)
    saved = state.read_run_ledger_snapshot(case.roots, case.request.name)
    assert saved.record == case.record
    assert saved.state["oci_root"]["phase"] == "resources-ready"
    assert saved.state["oci_root"]["root_volume"]["retention_policy"] == "delete"


@pytest.mark.parametrize("failure", ["preflight_oci_host", "discover_squashfs_toolchain"])
def test_readonly_admission_failure_does_not_initialize_or_reserve(case, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise StateError("readonly admission refused")

    monkeypatch.setattr(adapter, failure, fail)
    with pytest.raises(StateError, match="admission"):
        launch(case)
    assert not case.roots.state.exists() and not case.roots.config.exists()
    assert "connect" not in case.calls and "init" not in case.calls


def test_non_system_libvirt_profile_is_rejected_before_state_mutation(case):
    case.profile.uri = "qemu:///session"
    with pytest.raises(StateError, match="qualified system"):
        launch(case)
    assert not case.roots.state.exists() and "connect" not in case.calls


@pytest.mark.parametrize("failure", list(_GRANTS) + ["spawn_monitor_coordinator"])
def test_partial_grant_or_spawn_failure_preserves_resources_and_closes_connection(case, monkeypatch, failure):
    def fail(*args, **kwargs):
        case.calls.append("injected-failure")
        raise StateError("launch interrupted")

    monkeypatch.setattr(adapter, failure, fail)
    monkeypatch.setattr(adapter, "remove_oci_run", lambda *a, **k: pytest.fail("must not forge cleanup authority"))
    with pytest.raises(StateError, match="interrupted"):
        launch(case)
    assert case.calls[-1] == "close" and case.calls.count("close") == 1
    assert "ready" not in case.calls and "session" not in case.calls
    saved = state.read_run_ledger_snapshot(case.roots, case.request.name)
    assert saved.record == case.record and saved.state["oci_root"]["phase"] == "resources-ready"
    assert (case.roots.runs / case.request.name / "monitor-private").is_dir()


def test_detached_terminal_is_not_successful_background_readiness(case):
    case.request = replace(case.request, detached=True)
    case.terminal = ProcessExit(3, 3, None, ProcessExitCategory.EXITED)
    with pytest.raises(StateError, match="exited before detached READY"):
        launch(case)
    assert "session" not in case.calls and case.calls[-1] == "client-close"
    assert state.read_run_ledger_snapshot(case.roots, case.request.name).record == case.record


def test_foreground_early_terminal_retains_exact_exit_and_console_session(case):
    case.terminal = ProcessExit(3, 3, None, ProcessExitCategory.EXITED)
    result = launch(case)
    assert result.terminal == case.terminal and result.session is case.session


@pytest.mark.parametrize("operation", [adapter.stop_oci_run, adapter.rm_oci_run])
def test_existing_stop_and_rm_refuse_replaced_record_before_any_control(case, monkeypatch, operation):
    launch(case)
    case.calls.clear()
    monkeypatch.setattr(adapter, "load_oci_run_binding", lambda *_: case.binding)
    foreign = replace(case.record, run_id=str(uuid.uuid4()))
    with pytest.raises(StateError, match="identity changed"):
        operation(case.roots, case.request.name, expected_record=foreign)
    assert case.calls == []


def test_stop_waits_for_terminal_and_returns_exact_result(case, monkeypatch):
    launch(case)
    case.calls.clear()
    case.terminal = ProcessExit(-15, None, 15, ProcessExitCategory.SIGNALED)
    monkeypatch.setattr(adapter, "load_oci_run_binding", lambda *_: case.binding)
    monkeypatch.setattr(
        adapter, "_read_preactivation_journal", lambda *_: (SimpleNamespace(endpoint=case.endpoint), b"")
    )
    assert adapter.stop_oci_run(case.roots, case.request.name, expected_record=case.record) == case.terminal
    assert case.calls == ["client", "stop-wait", "client-close"]


@pytest.mark.parametrize("fail", [False, True])
def test_rm_uses_existing_binding_and_always_closes_its_connection(case, monkeypatch, fail):
    launch(case)
    case.calls.clear()
    monkeypatch.setattr(adapter, "load_oci_run_binding", lambda *_: case.binding)

    def remove(roots, binding, store, *, conn, timeout):
        assert roots == case.roots and binding == case.binding and isinstance(store, OCIStore)
        assert conn is case.conn and timeout == 10
        case.calls.append("remove")
        if fail:
            raise StateError("removal refused")
        return "removed"

    monkeypatch.setattr(adapter, "remove_oci_run", remove)
    if fail:
        with pytest.raises(StateError, match="refused"):
            adapter.rm_oci_run(case.roots, case.request.name, expected_record=case.record)
    else:
        assert adapter.rm_oci_run(case.roots, case.request.name, expected_record=case.record) == "removed"
    assert case.calls == ["connect", "remove", "close"]


@pytest.mark.parametrize("first", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("body_fails", [False, True])
def test_startup_handlers_only_queue_one_signal_and_restore_both(first, body_fails):
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

    def body():
        with adapter._startup_signals() as pending:
            for number in (first, signal.SIGINT, signal.SIGTERM):
                handler = signal.getsignal(number)
                assert callable(handler)
                assert handler(number, None) is None
            assert pending == [first]
            if body_fails:
                raise StateError("startup failed")

    if body_fails:
        with pytest.raises(StateError, match="startup failed"):
            body()
    else:
        body()
    assert {number: signal.getsignal(number) for number in previous} == previous


def test_startup_signal_context_off_main_thread_does_not_install_handlers():
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    results = []

    def worker():
        with adapter._startup_signals() as pending:
            results.append((list(pending), {number: signal.getsignal(number) for number in previous}))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and results == [([], previous)]
    assert {number: signal.getsignal(number) for number in previous} == previous


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_signal_inside_grant_lock_queues_then_cancels_before_spawn(case, monkeypatch, number):
    previous = signal.getsignal(number)
    grant = adapter.grant_oci_root_access

    def interrupted_grant(roots, binding, *, conn):
        with state.locked_existing_run(roots, case.request.name) as mutation:
            before = mutation.mutable_state()
            signal.getsignal(number)(number, None)
            assert mutation.mutable_state() == before
        return grant(roots, binding, conn=conn)

    monkeypatch.setattr(adapter, "grant_oci_root_access", interrupted_grant)
    monkeypatch.setattr(adapter, "stop_oci_run", lambda *a, **k: pytest.fail("no activated monitor to stop"))
    with pytest.raises(StateError, match="interrupted before activation.*local-test.*retained"):
        launch(case)
    assert "spawn" not in case.calls and "authority-enter" not in case.calls
    assert case.calls[-1] == "close"
    saved = state.read_run_ledger_snapshot(case.roots, case.request.name)
    assert saved.record == case.record and saved.state["oci_root"]["phase"] == "resources-ready"
    assert signal.getsignal(number) == previous


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("stop_fails", [False, True])
def test_signal_during_ready_wait_requests_authenticated_bounded_stop(case, monkeypatch, number, stop_fails):
    previous = signal.getsignal(number)
    base = adapter.MonitorClient

    class InterruptedClient(base):
        def wait_ready(self, *, timeout):
            signal.getsignal(number)(number, None)
            return super().wait_ready(timeout=timeout)

        def stop_and_wait(self, *, timeout):
            result = super().stop_and_wait(timeout=timeout)
            if stop_fails:
                raise StateError("authenticated stop unavailable")
            return result

    monkeypatch.setattr(adapter, "MonitorClient", InterruptedClient)
    monkeypatch.setattr(adapter, "remove_oci_run", lambda *a, **k: pytest.fail("must retain terminal evidence"))
    pattern = "authenticated stop unavailable" if stop_fails else "interrupted and stopped"
    with pytest.raises(StateError, match=pattern):
        launch(case)
    assert case.calls[-4:] == ["client", "ready", "stop-wait", "client-close"]
    assert "session" not in case.calls and case.calls.count("close") == 1
    assert state.read_run_ledger_snapshot(case.roots, case.request.name).record == case.record
    assert signal.getsignal(number) == previous


@pytest.mark.parametrize("stop_fails", [False, True])
def test_sigterm_inside_foreground_session_constructor_is_not_discarded(case, monkeypatch, stop_fails):
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    session = SimpleNamespace(close=lambda: case.calls.append("session-close"))

    def construct(roots, binding, endpoint):
        assert roots == case.roots and binding == case.binding and endpoint is case.endpoint
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        case.calls.append("session")
        return session

    def stop(roots, name, *, expected_record):
        assert roots == case.roots and name == case.request.name and expected_record == case.record
        assert {number: signal.getsignal(number) for number in previous} == previous
        case.calls.append("outer-stop")
        if stop_fails:
            raise StateError("authenticated stop unavailable")

    monkeypatch.setattr(adapter, "OCIMonitorProcessSession", construct)
    monkeypatch.setattr(adapter, "stop_oci_run", stop)
    pattern = "authenticated stop unavailable" if stop_fails else "interrupted and stopped.*retained for rm"
    with pytest.raises(StateError, match=pattern):
        launch(case)
    assert case.calls[-3:] == ["session", "outer-stop", "session-close"]
    assert state.read_run_ledger_snapshot(case.roots, case.request.name).record == case.record


def test_signal_queued_while_restoring_startup_handlers_still_stops_detached_run(case, monkeypatch):
    case.request = replace(case.request, detached=True)
    previous = signal.getsignal(signal.SIGINT)
    install = signal.signal
    fired = []

    def restore(number, handler):
        if number == signal.SIGINT and handler == previous and not fired:
            fired.append(True)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return install(number, handler)

    def stop(roots, name, *, expected_record):
        assert roots == case.roots and name == case.request.name and expected_record == case.record
        case.calls.append("outer-stop")

    monkeypatch.setattr(adapter.signal, "signal", restore)
    monkeypatch.setattr(adapter, "stop_oci_run", stop)
    with pytest.raises(StateError, match="interrupted and stopped"):
        launch(case)
    assert fired == [True] and case.calls[-1] == "outer-stop" and "session" not in case.calls
    assert state.read_run_ledger_snapshot(case.roots, case.request.name).record == case.record
