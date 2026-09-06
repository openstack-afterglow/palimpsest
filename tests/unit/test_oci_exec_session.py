"""Guest exec output and exit are independent of the VM workload lifecycle."""

from types import SimpleNamespace

import pytest

from palimpsest_local import oci_exec_session as sessions
from palimpsest_local.errors import StateError
from palimpsest_local.oci_exec_control import MonitorExecControl
from palimpsest_local.runtime_types import (
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSession,
    ProcessStream,
)


@pytest.fixture
def case(monkeypatch):
    value = SimpleNamespace(
        control=MonitorExecControl(),
        calls=[],
        output=b"out\0" * 800,
        error=b"stderr\n",
        code=23,
        number=None,
        reason="completed",
        terminal_none=False,
        mutate=lambda x: x,
    )
    value.control.mark_ready()

    class Client:
        def __init__(self, *args):
            value.calls.append("client")

        def exec_request(self, operation, payload, *, timeout):
            assert 0.1 <= timeout <= 5
            value.calls.append((operation, dict(payload)))
            if operation == "poll":
                job = value.control.take_exec()
                if job is not None:
                    for stream, content in (("stdout", value.output), ("stderr", value.error)):
                        for offset in range(0, len(content), 1024):
                            value.control.append_output(job, stream, offset, content[offset : offset + 1024])
                    value.control.complete(
                        job,
                        None if value.terminal_none else {"exit_code": value.code, "signal": value.number},
                        len(value.output),
                        len(value.error),
                        value.reason,
                    )
            result = getattr(value.control, operation)(**payload)
            return value.mutate(result) if operation == "poll" else result

        def close(self):
            value.calls.append("close")

    monkeypatch.setattr(sessions, "MonitorClient", Client)
    return value


def open_session():
    return sessions.OCIExecProcessSession(object(), object(), object(), ("/bin/probe", "literal $HOME"))


def test_split_exact_output_and_nonzero_exit_drain_before_ack(case):
    session = open_session()
    assert isinstance(session, ProcessSession)
    events = list(session.events())
    assert (
        b"".join(
            item.data for item in events if isinstance(item, ProcessOutputEvent) and item.stream is ProcessStream.STDOUT
        )
        == case.output
    )
    assert (
        b"".join(
            item.data for item in events if isinstance(item, ProcessOutputEvent) and item.stream is ProcessStream.STDERR
        )
        == case.error
    )
    assert events[-1].result == session.wait() == ProcessExit(23, 23, None, ProcessExitCategory.EXITED)
    assert case.control.status() == {"state": "ready", "next_sequence": 2, "occupied": False}
    assert case.calls[-1][0] == "acknowledge"
    session.close()
    assert case.calls[-1] == "close"


@pytest.mark.parametrize("reason", ["timeout", "output-limit", "cancelled"])
def test_incomplete_command_never_reports_success_even_if_leader_exit_zero(case, reason):
    case.reason, case.code = reason, 0
    session = open_session()
    with pytest.raises(StateError, match=reason):
        list(session.events())
    assert case.control.status()["next_sequence"] == 2
    session.close()


def test_signaled_guest_exec_preserves_actual_signal_without_stopping_vm(case):
    case.code, case.number = None, 15
    session = open_session()
    assert session.wait() == ProcessExit(-15, None, 15, ProcessExitCategory.SIGNALED)
    assert case.control.status()["state"] == "ready"
    session.close()


@pytest.mark.parametrize("damage", ["token", "sequence", "offset", "encoding", "size", "terminal"])
def test_changed_output_or_terminal_proof_is_rejected_without_ack(case, damage):
    def mutate(result):
        if damage == "token":
            result["token"] = "foreign"
        elif damage == "sequence":
            result["sequence"] = True
        elif damage == "offset":
            result["stdout_offset"] += 1
        elif damage == "encoding":
            result["stdout_hex"] = "GG"
        elif damage == "size":
            result["stderr_size"] = 65537
        else:
            result["terminal"] = {"exit_code": True, "signal": None}
        return result

    case.mutate = mutate
    session = open_session()
    with pytest.raises(StateError):
        list(session.events())
    assert case.control.status()["occupied"] and not any(
        isinstance(call, tuple) and call[0] == "acknowledge" for call in case.calls
    )
    session.close()


def test_closed_reader_does_not_reexec_or_stop_guest(case):
    session = open_session()
    session.close()
    assert case.control.status()["occupied"]
    with pytest.raises(StateError):
        session.events()
    with pytest.raises(StateError):
        open_session()
    assert len([call for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]) == 1


def test_same_vm_accepts_next_exec_only_after_previous_result_is_acknowledged(case):
    first = open_session()
    first.wait()
    first.close()
    second = open_session()
    second.wait()
    second.close()
    submissions = [call[1] for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]
    assert [item["sequence"] for item in submissions] == [1, 2]
    assert submissions[0]["token"] != submissions[1]["token"]


def test_pre_fork_cancelled_job_is_acknowledged_without_a_fabricated_exit(case):
    case.terminal_none = True
    case.output = case.error = b""
    case.reason = "cancelled"
    session = open_session()
    with pytest.raises(StateError, match="cancelled"):
        list(session.events())
    assert case.control.status()["next_sequence"] == 2
    assert session._result is None
    session.close()
