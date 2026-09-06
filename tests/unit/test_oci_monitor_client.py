"""Client readiness and STOP require exact durable and live-worker evidence."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace

import pytest
from test_oci_monitor_recovery import case as _recovery_case
from test_oci_store import _handoff_receipt

from palimpsest_local import oci_monitor_client as client
from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import ProcessExit, ProcessExitCategory
from palimpsest_local.state import locked_existing_run

case = _recovery_case


@pytest.fixture
def transport(case, monkeypatch):
    calls = []
    controls = {"stop": "stop-terminal", "before": lambda _: None}

    def request(descriptor, endpoint, operation, *, timeout):
        assert 0.1 <= timeout <= 5
        assert endpoint == case.snapshot.endpoint
        assert os.fstat(descriptor).st_ino == case.directory.stat().st_ino
        calls.append(operation)
        controls["before"](operation)
        phase = json.loads(case.journal.read_bytes())["phase"]
        state = phase if operation is ipc.MonitorIPCOperation.DESCRIBE else controls["stop"]
        return ipc.MonitorIPCReply(operation, state, endpoint.writer)

    monkeypatch.setattr(ipc, "request_monitor", request)
    return calls, controls


def _open(case, **kwargs):
    return client.MonitorClient(case.roots, case.binding, case.snapshot.endpoint, **kwargs)


def _journal(case, phase, **changes):
    before = ipc._decode_preactivation_snapshot(case.journal.read_bytes(), case.binding)
    value = replace(before, phase=phase, revision=before.revision + 1, **changes)
    case.journal.write_bytes(ipc._canonical_bytes(value.to_dict()) + b"\n")


def _terminal(case, result=None):
    result = result or ProcessExit(7, 7, None, ProcessExitCategory.EXITED)
    with locked_existing_run(case.roots, case.binding.record.name, expected=case.binding.record) as mutation:
        state = mutation.mutable_state()
        handoff = state["oci_root_handoff"]
        handoff["phase"] = "terminal"
        handoff["lifecycle"] = _handoff_receipt(
            "terminal", result, boot_attempt_id=case.binding.boot_attempt_id
        ).to_dict()
        mutation.write_state("exited", state)
    _journal(case, "terminal")
    return result


def test_ready_uses_live_describe_and_exact_ledger_without_mutation(case, transport):
    before = case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes()
    with _open(case) as monitor:
        observed = monitor.wait_ready()
        assert observed == client.MonitorObservation("ready")
    assert transport[0] == [ipc.MonitorIPCOperation.DESCRIBE]
    assert before == (case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes())


@pytest.mark.parametrize("stop_reply", ["stop-refused", "stop-accepted"])
def test_terminal_journal_does_not_claim_worker_done(case, transport, stop_reply):
    _terminal(case)
    transport[1]["stop"] = stop_reply
    with _open(case) as monitor:
        assert monitor.poll() == client.MonitorObservation("terminal")
        transport[1]["stop"] = "stop-terminal"
        assert monitor.wait_terminal() == ProcessExit(7, 7, None, ProcessExitCategory.EXITED)


@pytest.mark.parametrize(
    "result",
    [
        ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
        ProcessExit(255, 255, None, ProcessExitCategory.EXITED),
        ProcessExit(-15, None, 15, ProcessExitCategory.SIGNALED),
    ],
)
def test_returns_exact_authenticated_terminal_for_natural_exit(case, transport, result):
    _terminal(case, result)
    with _open(case) as monitor:
        assert monitor.wait_ready().terminal == result
        assert monitor.wait_terminal(timeout=None) == result


def test_ready_to_terminal_race_returns_completed_terminal(case, transport):
    fired = False

    def advance(operation):
        nonlocal fired
        if not fired:
            fired = True
            _terminal(case)

    transport[1]["before"] = advance
    with _open(case) as monitor:
        assert monitor.wait_ready().terminal.returncode == 7


def test_stop_acceptance_is_not_terminal(case, transport):
    transport[1]["stop"] = "stop-accepted"
    with _open(case) as monitor:
        assert monitor.request_stop() == "stop-accepted"
        assert monitor.poll().terminal is None
        with pytest.raises(client.MonitorClientError, match="timed out"):
            monitor.stop_and_wait(timeout=0.15)
    assert json.loads(case.journal.read_bytes())["phase"] == "ready"


def test_stop_waits_until_receipt_and_worker_completion(case, transport):
    transport[1]["stop"] = "stop-accepted"
    stops = 0

    def advance(operation):
        nonlocal stops
        if operation is ipc.MonitorIPCOperation.STOP:
            stops += 1
            if stops == 1:
                _terminal(case)
            elif stops == 3:
                transport[1]["stop"] = "stop-terminal"

    transport[1]["before"] = advance
    with _open(case) as monitor:
        assert monitor.stop_and_wait().returncode == 7
    assert stops == 3


def test_refused_stop_does_not_fake_exit(case, transport):
    transport[1]["stop"] = "stop-refused"
    with _open(case) as monitor:
        with pytest.raises(client.MonitorClientError, match="refused"):
            monitor.stop_and_wait()
    assert json.loads(case.state.read_bytes())["status"] == "running"


@pytest.mark.parametrize("already_terminal", [False, True])
def test_natural_terminal_racing_stop_waits_for_worker(case, transport, already_terminal):
    if already_terminal:
        _terminal(case)
    transport[1]["stop"] = "stop-refused"
    stops = 0

    def advance(operation):
        nonlocal stops
        if operation is ipc.MonitorIPCOperation.STOP:
            stops += 1
            if stops == 1 and not already_terminal:
                _terminal(case)
            elif stops == 3:
                transport[1]["stop"] = "stop-terminal"

    transport[1]["before"] = advance
    with _open(case) as monitor:
        assert monitor.request_stop() == "stop-terminal"
        assert monitor.wait_terminal().returncode == 7
    assert stops == 4


def test_observed_terminal_result_cannot_change_between_polls(case, transport):
    _terminal(case)
    with _open(case) as monitor:
        assert monitor.poll().terminal.returncode == 7
        transport[0].clear()
        state = json.loads(case.state.read_bytes())
        terminal = state["oci_root_handoff"]["lifecycle"]["terminal"]
        terminal["exit_code"] = terminal["returncode"] = 8
        case.state.write_text(json.dumps(state))
        with pytest.raises(client.MonitorClientError, match="terminal evidence changed"):
            monitor.poll()
        assert transport[0] == []


@pytest.mark.parametrize("change", ["generation", "socket", "writer"])
def test_wrong_endpoint_rejected_before_transport(case, transport, change):
    endpoint = case.snapshot.endpoint
    if change == "generation":
        endpoint = ipc.MonitorExecEndpoint(
            replace(endpoint.identity, generation=str(uuid.uuid4())),
            endpoint.writer,
            endpoint.socket_device,
            endpoint.socket_inode,
        )
    elif change == "socket":
        endpoint = replace(endpoint, socket_inode=endpoint.socket_inode + 1)
    else:
        endpoint = replace(endpoint, writer=replace(endpoint.writer, pid=endpoint.writer.pid + 1))
    with pytest.raises(client.MonitorClientError):
        client.MonitorClient(case.roots, case.binding, endpoint)
    assert transport[0] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_code", True),
        ("signal_number", True),
        ("returncode", True),
        ("exit_code", 256),
        ("category", "control-lost"),
    ],
)
def test_bad_terminal_receipt_refused_before_ipc(case, transport, field, value):
    _terminal(case)
    state = json.loads(case.state.read_bytes())
    state["oci_root_handoff"]["lifecycle"]["terminal"][field] = value
    case.state.write_text(json.dumps(state))
    with pytest.raises(client.MonitorClientError, match="evidence is invalid"):
        _open(case)
    assert transport[0] == []


@pytest.mark.parametrize(
    "change", ["endpoint", "generation", "run", "domain", "receipt-identity", "journal-regression"]
)
def test_changed_authority_refused_without_retry(case, transport, change):
    with _open(case) as monitor:
        assert monitor.poll().phase == "ready"
        transport[0].clear()
        if change == "receipt-identity":
            state = json.loads(case.state.read_bytes())
            state["oci_root_handoff"]["lifecycle"]["key_id"] = "sha256:" + "e" * 64
            case.state.write_text(json.dumps(state))
        elif change == "run":
            state = json.loads(case.owner.read_bytes())
            state["run_id"] = str(uuid.uuid4())
            case.owner.write_text(json.dumps(state))
        elif change == "domain":
            state = json.loads(case.state.read_bytes())
            state["oci_root_handoff"]["domain_id"] += 1
            case.state.write_text(json.dumps(state))
        else:
            raw = json.loads(case.journal.read_bytes())
            if change == "endpoint":
                raw["socket"]["inode"] += 1
            elif change == "generation":
                raw["monitor_generation"] = str(uuid.uuid4())
            else:
                raw["revision"] -= 1
            case.journal.write_bytes(ipc._canonical_bytes(raw) + b"\n")
        with pytest.raises(client.MonitorClientError):
            monitor.poll()
        assert transport[0] == []


def test_private_directory_replacement_refused(case, transport):
    with _open(case) as monitor:
        case.directory.rename(case.directory.with_name("preserved-monitor"))
        case.directory.mkdir(mode=0o700)
        with pytest.raises(client.MonitorClientError, match="directory changed"):
            monitor.poll()
    assert transport[0] == []


def test_closed_or_forked_client_refused(case, transport, monkeypatch):
    monitor = _open(case)
    monitor.close()
    monitor.close()
    with pytest.raises(client.MonitorClientError):
        monitor.poll()
    with _open(case) as monitor:
        monkeypatch.setattr(client.os, "getpid", lambda: monitor._pid + 1)
        with pytest.raises(client.MonitorClientError):
            monitor.poll()
    assert transport[0] == []


@pytest.mark.parametrize("timeout", [None, True, False, 0, -1, 0.09, 3601, float("inf"), float("nan"), "5"])
def test_invalid_deadline_refused_before_io(case, transport, timeout):
    with pytest.raises(client.MonitorClientError, match="timeout is invalid"):
        _open(case, timeout=timeout)
    with _open(case) as monitor:
        with pytest.raises(client.MonitorClientError, match="timeout is invalid"):
            monitor.stop_and_wait(timeout=timeout)
    assert transport[0] == []


def test_run_lock_contention_has_finite_deadline(case, transport):
    with locked_existing_run(case.roots, case.binding.record.name):
        started = time.monotonic()
        with pytest.raises(client.MonitorClientError, match="timed out"):
            _open(case, timeout=0.1)
        assert time.monotonic() - started < 1
    assert transport[0] == []


@pytest.mark.parametrize("timeout", [False, 0, -1, float("inf"), float("nan"), "1"])
def test_invalid_state_lock_deadline(case, timeout):
    with pytest.raises(StateError, match="timeout"):
        with locked_existing_run(case.roots, case.binding.record.name, lock_timeout=timeout):
            pytest.fail("invalid timeout admitted")


def test_deadline_not_reset_between_partial_ipc_reads(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(ipc.time, "monotonic", lambda: now[0])
    timeouts = []

    class Drip:
        def settimeout(self, value):
            timeouts.append(value)

        def recv(self, count):
            now[0] += 0.06
            return b"x"

    timed = ipc._RequestDeadlineSocket(Drip(), 1.1)
    with pytest.raises(ipc.MonitorIPCError) as error:
        ipc._recv_exact(timed, 4)
    assert error.value.category is ipc.MonitorIPCErrorCategory.TIMEOUT
    assert timeouts == pytest.approx([0.1, 0.04])


def test_deadline_not_reset_between_partial_ipc_writes(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(ipc.time, "monotonic", lambda: now[0])

    class Drip:
        def settimeout(self, value):
            pass

        def send(self, data):
            now[0] += 0.06
            return 1

    with pytest.raises(ipc.MonitorIPCError) as error:
        ipc._send_all(ipc._RequestDeadlineSocket(Drip(), 1.1), b"example")
    assert error.value.category is ipc.MonitorIPCErrorCategory.TIMEOUT


def test_poll_never_rounds_up_remaining_ipc_deadline(case, transport, monkeypatch):
    with _open(case) as monitor:
        now = [10.0]
        monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
        original = monitor._read

        def read(deadline):
            result = original(deadline)
            now[0] += 0.11
            return result

        monkeypatch.setattr(monitor, "_read", read)
        with pytest.raises(client.MonitorClientError, match="timed out"):
            monitor.poll(timeout=0.2)
    assert transport[0] == []


def test_terminal_snapshot_still_needs_valid_describe(case, transport):
    _terminal(case)
    transport[1]["before"] = lambda _: (_ for _ in ()).throw(ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.CLOSED))
    with _open(case) as monitor:
        with pytest.raises(client.MonitorClientError, match="control is unavailable"):
            monitor.wait_terminal()
    assert transport[0] == [ipc.MonitorIPCOperation.DESCRIBE]
