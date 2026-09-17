"""Short pinned console reads and a monitor-backed single-consumer session."""

import os
from types import SimpleNamespace

import pytest
import test_oci_runtime_io as io_tests

from palimpsest_local import oci_process_session as sessions
from palimpsest_local import oci_runtime_io as runtime_io
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import (
    ProcessCapabilityError,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSession,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
)

EXIT = ProcessExit(7, 7, None, ProcessExitCategory.EXITED)


@pytest.fixture
def case(tmp_path, monkeypatch):
    roots, paths = io_tests.case.__wrapped__(tmp_path)
    receipt = io_tests._commit((roots, paths))
    record = state.read_run_ledger_snapshot(roots, "vm").record
    binding = SimpleNamespace(record=record, plan_digest=io_tests.PLAN)
    observations = []
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            calls.append("open")

        def poll(self, **kwargs):
            calls.append("poll")
            # A client call must never happen while the console's run lock is
            # held: the real worker needs that lock for READY and TERMINAL.
            with state.locked_existing_run(roots, "vm", lock_timeout=0.1):
                pass
            if observations:
                return observations.pop(0)
            return SimpleNamespace(phase="terminal", terminal=EXIT)

        def request_stop(self, **kwargs):
            calls.append("stop")
            return "stop-accepted"

        def close(self):
            calls.append("close")

    monkeypatch.setattr(sessions, "MonitorClient", Client)
    return SimpleNamespace(
        roots=roots,
        paths=paths,
        receipt=receipt,
        binding=binding,
        calls=calls,
        observations=observations,
        console=paths.root / "io" / "console.log",
    )


def session(case):
    return sessions.OCIMonitorProcessSession(case.roots, case.binding, object())


def test_combined_console_exact_bytes_bounded_and_drained_before_actual_exit(case):
    payload = b"boot diagnostic\nworkload\x00\xff\n" + b"x" * 140000
    case.console.write_bytes(payload)
    current = session(case)
    try:
        assert isinstance(current, ProcessSession)
        events = list(current.events())
        output = events[:-1]
        assert all(isinstance(event, ProcessOutputEvent) and event.stream is ProcessStream.STDOUT for event in output)
        assert all(len(event.data) <= 65536 for event in output)
        assert b"".join(event.data for event in output) == payload
        assert events[-1] == ProcessStatusEvent(EXIT)
        assert current.wait() == EXIT
        assert case.calls.count("poll") == len(events)
        with pytest.raises(StateError, match="already consumed"):
            current.events()
    finally:
        current.close()
    assert "stop" not in case.calls
    assert current.wait() == EXIT


def test_console_append_after_readiness_is_followed_without_replay(case):
    case.console.write_bytes(b"first")
    case.observations.append(SimpleNamespace(phase="ready", terminal=None))
    current = session(case)
    try:
        events = current.events()
        assert next(events).data == b"first"
        with case.console.open("ab") as stream:
            stream.write(b"second")
        assert next(events).data == b"second"
        assert next(events) == ProcessStatusEvent(EXIT)
        with pytest.raises(StopIteration):
            next(events)
    finally:
        current.close()


@pytest.mark.parametrize("signal", [ProcessSignal.INTERRUPT, ProcessSignal.TERMINATE])
def test_supported_interrupts_request_stop_without_claiming_exit(case, signal):
    current = session(case)
    try:
        current.signal(signal)
        assert case.calls == ["open"]
        assert current._result is None
        assert current.wait() == EXIT
        assert case.calls.count("stop") == 1
    finally:
        current.close()


def test_close_is_detach_only_and_closed_reader_fails(case):
    current = session(case)
    events = current.events()
    current.close()
    with pytest.raises(StateError, match="closed"):
        next(events)
    with pytest.raises(StateError, match="closed"):
        current.signal(ProcessSignal.TERMINATE)
    assert "stop" not in case.calls


def test_wait_does_not_compete_with_event_consumer(case):
    current = session(case)
    try:
        current.events()
        with pytest.raises(StateError, match="still active"):
            current.wait()
    finally:
        current.close()


def test_signal_reentrant_during_console_lock_is_deferred_and_coalesced(case, monkeypatch):
    case.console.write_bytes(b"workload")
    case.observations.append(SimpleNamespace(phase="ready", terminal=None))
    current = session(case)
    original = runtime_io.RuntimeIOGuard.read_console

    def interrupt(guard, *args, **kwargs):
        current.signal(ProcessSignal.INTERRUPT)
        current.signal(ProcessSignal.TERMINATE)
        assert "stop" not in case.calls
        return original(guard, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime_io.RuntimeIOGuard, "read_console", interrupt)
            events = current.events()
            assert next(events).data == b"workload"
        assert next(events) == ProcessStatusEvent(EXIT)
        assert case.calls.count("stop") == 1
    finally:
        current.close()


@pytest.mark.parametrize("operation", ["stdin", "close_stdin", "resize", "hangup", "string_signal"])
def test_unavailable_capabilities_have_no_monitor_side_effects(case, operation):
    current = session(case)
    try:
        assert not current.capabilities.stdin and not current.capabilities.tty and not current.capabilities.resize
        with pytest.raises(ProcessCapabilityError):
            if operation == "stdin":
                current.write_stdin(b"x")
            elif operation == "close_stdin":
                current.close_stdin()
            elif operation == "resize":
                current.resize(20, 80)
            else:
                current.signal(ProcessSignal.HANGUP if operation == "hangup" else "interrupt")
        assert case.calls == ["open"]
    finally:
        current.close()


@pytest.mark.parametrize("damage", ["replace", "symlink", "truncate", "receipt"])
def test_console_identity_damage_never_returns_foreign_bytes_or_success(case, damage):
    case.console.write_bytes(b"original")
    current = session(case)
    try:
        events = current.events()
        assert next(events).data == b"original"
        if damage in {"replace", "symlink"}:
            original = case.console.with_name("preserved")
            case.console.rename(original)
            if damage == "replace":
                case.console.write_bytes(b"foreign secret")
                case.console.chmod(0o600)
            else:
                case.console.symlink_to(original)
        elif damage == "truncate":
            case.console.write_bytes(b"x")
        else:
            with state.locked_existing_run(case.roots, "vm") as mutation:
                value = mutation.mutable_state()
                value["oci_runtime_io"]["console_inode"] += 1
                mutation.write_state("creating", value)
        with pytest.raises(StateError):
            next(events)
        assert current._result is None
    finally:
        current.close()


def test_client_is_closed_when_constructor_cannot_pin_console(case):
    case.console.unlink()
    with pytest.raises(StateError):
        session(case)
    assert case.calls == ["open", "close"]


def test_forked_session_cannot_read_or_signal(case, monkeypatch):
    current = session(case)
    try:
        monkeypatch.setattr(sessions.os, "getpid", lambda: current._pid + 1)
        with pytest.raises(StateError, match="closed"):
            current.events()
        with pytest.raises(StateError, match="closed"):
            current.signal(ProcessSignal.INTERRUPT)
    finally:
        current.close()


@pytest.mark.parametrize("offset,limit", [(True, 1), (-1, 1), (2**63, 1), (0, False), (0, 0), (0, 65537)])
def test_guard_console_read_rejects_invalid_bounds(case, offset, limit):
    with state.locked_existing_run(case.roots, "vm") as mutation:
        with runtime_io.runtime_io_guard(mutation, plan_digest=io_tests.PLAN) as guard:
            with pytest.raises(StateError):
                guard.read_console(offset, limit)


def test_guard_pread_keeps_offset_and_revalidates_after_read(case, monkeypatch):
    case.console.write_bytes(b"abcdef")
    with state.locked_existing_run(case.roots, "vm") as mutation:
        with runtime_io.runtime_io_guard(mutation, plan_digest=io_tests.PLAN) as guard:
            assert guard.read_console(1, 3) == b"bcd"
            assert guard.read_console(1, 3) == b"bcd"
    original = os.pread

    def truncate(fd, size, offset):
        content = original(fd, size, offset)
        case.console.write_bytes(b"")
        return content

    with state.locked_existing_run(case.roots, "vm") as mutation:
        with runtime_io.runtime_io_guard(mutation, plan_digest=io_tests.PLAN) as guard:
            monkeypatch.setattr(os, "pread", truncate)
            with pytest.raises(StateError):
                guard.read_console(0)
