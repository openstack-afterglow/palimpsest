"""Guest exec output and exit are independent of the VM workload lifecycle."""

import asyncio
import os
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from palimpsest_local import oci_exec_session as sessions
from palimpsest_local.errors import StateError
from palimpsest_local.oci_exec_control import MonitorExecControl
from palimpsest_local.oci_monitor_client import MonitorClientError, MonitorClientTimeoutSource
from palimpsest_local.runtime_types import (
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSession,
    ProcessStatusEvent,
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
        mutate_status=lambda x: x,
        acknowledge=lambda control, payload: control.acknowledge(**payload),
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
            if operation == "acknowledge":
                return value.acknowledge(value.control, payload)
            result = getattr(value.control, operation)(**payload)
            if operation == "status":
                return value.mutate_status(result)
            return value.mutate(result) if operation == "poll" else result

        def close(self):
            value.calls.append("close")

    monkeypatch.setattr(sessions, "MonitorClient", Client)
    return value


def open_session():
    return sessions.OCIExecProcessSession(object(), object(), object(), ("/bin/probe", "literal $HOME"))


def isolate_cli(monkeypatch, tmp_path):
    """Pin state, config and the host command journal so stderr stays exact."""
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(journal))


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
    assert session.observed_completion == sessions.OCIExecCompletionObservation(
        ProcessExit(23, 23, None, ProcessExitCategory.EXITED),
        "completed",
        len(case.output),
        len(case.error),
        "confirmed",
    )
    assert case.control.status() == {"state": "ready", "next_sequence": 2, "occupied": False}
    assert case.calls[-2][0] == "acknowledge" and case.calls[-1] == "close"
    session.close()
    assert case.calls[-1] == "close"


@pytest.mark.parametrize("reason", ["timeout", "output-limit", "cancelled"])
def test_incomplete_command_never_reports_success_even_if_leader_exit_zero(case, reason):
    case.reason, case.code = reason, 0
    session = open_session()
    with pytest.raises(StateError, match=reason):
        list(session.events())
    assert session.observed_completion == sessions.OCIExecCompletionObservation(
        ProcessExit(0, 0, None, ProcessExitCategory.EXITED),
        reason,
        len(case.output),
        len(case.error),
        "confirmed",
    )
    assert session._result is None
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
    assert session.observed_completion is None
    assert case.control.status()["occupied"] and not any(
        isinstance(call, tuple) and call[0] == "acknowledge" for call in case.calls
    )
    session.close()


def test_closed_reader_does_not_reexec_or_stop_guest(case):
    session = open_session()
    session.close()
    assert session.observed_completion is None
    assert case.control.status()["occupied"]
    with pytest.raises(StateError):
        session.events()
    with pytest.raises(StateError):
        open_session()
    assert len([call for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]) == 1


@pytest.mark.parametrize("occupied", [False, True])
@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("not-ready", "check the run status and wait for authenticated READY"),
        ("stopping", "wait for shutdown and inspect existing results"),
        ("terminal", "run has ended; inspect the run's terminal result"),
        ("control-lost", "do not rerun a command whose outcome is unknown"),
    ],
)
def test_lifecycle_refusal_is_specific_and_never_submits_or_acknowledges(case, state, occupied, message):
    case.mutate_status = lambda result: {**result, "state": state, "occupied": occupied}
    with pytest.raises(StateError, match=message):
        open_session()
    assert case.calls == ["client", ("status", {}), "close"]


@pytest.mark.parametrize("phase", ["queued", "running", "completed"])
def test_occupied_does_not_claim_abandonment_or_take_over_a_result(case, phase):
    original = open_session()
    if phase != "queued":
        job = case.control.take_exec()
        if phase == "completed":
            case.control.complete(job, {"exit_code": 0, "signal": None}, 0, 0, "completed")
    before = case.control.status()
    case.calls.clear()
    with pytest.raises(StateError, match="may still be active or its result may be unacknowledged") as exc:
        open_session()
    assert "original client" in str(exc.value)
    assert "result takeover is not supported" in str(exc.value)
    assert "unknown outcome must not be rerun" in str(exc.value)
    assert "abandoned" not in str(exc.value)
    assert case.calls == ["client", ("status", {}), "close"]
    assert case.control.status() == before
    original.close()


@pytest.mark.parametrize(
    "status",
    [
        None,
        [],
        {},
        {"state": "ready", "next_sequence": 1, "occupied": False, "extra": True},
        *({"state": state, "next_sequence": 1, "occupied": False} for state in ("unknown", None, [], {}, 1)),
        *(
            {"state": "ready", "next_sequence": sequence, "occupied": False}
            for sequence in (True, 0, sessions.MAX_EXEC_SEQUENCE + 1, "1")
        ),
        {"state": "ready", "next_sequence": 1, "occupied": 0},
    ],
)
def test_invalid_status_is_rejected_without_submit_or_ack(case, status):
    case.mutate_status = lambda result: status
    with pytest.raises(StateError, match="mailbox status is invalid"):
        open_session()
    assert case.calls == ["client", ("status", {}), "close"]


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
    assert session.observed_completion == sessions.OCIExecCompletionObservation(None, "cancelled", 0, 0, "confirmed")
    session.close()


def test_chunked_output_does_not_expose_observation_until_generator_resumes_past_all_output(case):
    case.output, case.error = b"stdout", b"stderr"
    session = open_session()
    events = session.events()

    assert next(events) == ProcessOutputEvent(ProcessStream.STDOUT, case.output)
    assert session.observed_completion is None
    assert next(events) == ProcessOutputEvent(ProcessStream.STDERR, case.error)
    assert session.observed_completion is None

    status = next(events)
    assert status == ProcessStatusEvent(ProcessExit(23, 23, None, ProcessExitCategory.EXITED))
    assert session.observed_completion.acknowledgement == "confirmed"
    with pytest.raises(StopIteration):
        next(events)


@pytest.mark.parametrize("after_mutation", [False, True])
def test_ack_failure_retains_same_unconfirmed_observation_without_claiming_mailbox_state(case, after_mutation):
    def fail_ack(control, payload):
        if after_mutation:
            control.acknowledge(**payload)
        raise RuntimeError("raw secret token and path must not escape")

    case.acknowledge = fail_ack
    session = open_session()
    emitted = []
    with pytest.raises(sessions.OCIExecAcknowledgementError) as caught:
        for event in session.events():
            emitted.append(event)

    expected = sessions.OCIExecCompletionObservation(
        ProcessExit(23, 23, None, ProcessExitCategory.EXITED),
        "completed",
        len(case.output),
        len(case.error),
        "unconfirmed",
    )
    assert caught.value.observation == session.observed_completion == expected
    assert case.control.status()["occupied"] is (not after_mutation)
    assert session._result is None
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    assert "reason=completed" in str(caught.value) and "status=exit-code-23" in str(caught.value)
    assert "acknowledgement is unconfirmed" in str(caught.value)
    assert "mailbox occupancy is unknown" in str(caught.value)
    assert "preserve the original output" in str(caught.value) and "do not rerun" in str(caught.value)
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len([call for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]) == 1
    assert len([call for call in case.calls if isinstance(call, tuple) and call[0] == "acknowledge"]) == 1
    session.close()


@pytest.mark.parametrize(
    "reply",
    [
        {"state": "ready", "next_sequence": 2, "occupied": False, "extra": True},
        {"state": "ready", "next_sequence": 1, "occupied": False},
    ],
)
def test_invalid_ack_status_or_binding_is_unconfirmed_and_never_publishes_success(case, reply):
    def invalid_ack(control, payload):
        control.acknowledge(**payload)
        return reply

    case.acknowledge = invalid_ack
    session = open_session()
    emitted = []
    with pytest.raises(sessions.OCIExecAcknowledgementError) as caught:
        for event in session.events():
            emitted.append(event)
    assert caught.value.observation.acknowledgement == "unconfirmed"
    assert session.observed_completion == caught.value.observation
    assert session._result is None
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    session.close()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(9), asyncio.CancelledError()])
def test_ack_base_exception_identity_is_unchanged_with_unconfirmed_observation(case, interruption):
    def interrupt_ack(_control, _payload):
        raise interruption

    case.acknowledge = interrupt_ack
    session = open_session()
    with pytest.raises(type(interruption)) as caught:
        list(session.events())
    assert caught.value is interruption
    assert session.observed_completion.acknowledgement == "unconfirmed"
    assert case.control.status()["occupied"] is True
    session.close()


def test_observation_is_frozen_and_survives_same_process_close_but_fails_closed_after_fork(case):
    session = open_session()
    session.wait()
    session.close()
    observation = session.observed_completion
    assert observation.acknowledgement == "confirmed"
    with pytest.raises(FrozenInstanceError):
        observation.acknowledgement = "unconfirmed"
    with pytest.raises(FrozenInstanceError):
        observation.terminal.exit_code = 0

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            _observation = session.observed_completion
        except StateError:
            os.write(write_fd, b"closed")
        else:
            os.write(write_fd, b"exposed")
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 16) == b"closed"
        _pid, status = os.waitpid(child, 0)
        assert status == 0
    finally:
        os.close(read_fd)


def test_control_lost_poll_does_not_fabricate_an_observation(case):
    def control_lost(result):
        return {
            **result,
            "state": "control-lost",
            "stdout_hex": "",
            "stderr_hex": "",
            "stdout_size": 0,
            "stderr_size": 0,
            "terminal": None,
            "reason": "control-lost",
        }

    case.output = case.error = b""
    case.mutate = control_lost
    session = open_session()
    with pytest.raises(StateError, match="control was lost"):
        list(session.events())
    assert session.observed_completion is None
    assert not any(isinstance(call, tuple) and call[0] == "acknowledge" for call in case.calls)
    session.close()


def test_monitor_timeout_source_survives_exec_session_boundary(case):
    expected = MonitorClientError(
        "OCI monitor client timed out; preserve the run evidence",
        timeout_source=MonitorClientTimeoutSource.IPC_TIMEOUT,
    )
    original = sessions.MonitorClient

    class TimeoutClient(original):
        def exec_request(self, operation, payload, *, timeout):
            if operation == "poll":
                raise expected
            return super().exec_request(operation, payload, timeout=timeout)

    sessions.MonitorClient = TimeoutClient
    try:
        session = open_session()
        with pytest.raises(MonitorClientError) as error:
            list(session.events())
        assert error.value is expected
        assert error.value.timeout_source is MonitorClientTimeoutSource.IPC_TIMEOUT
        assert str(error.value).endswith("; timeout-source=ipc-timeout")
    finally:
        sessions.MonitorClient = original


def test_cli_ack_failure_prints_observed_facts_once_and_returns_nonzero(case, monkeypatch, tmp_path, capsys):
    from palimpsest_local import cli, runtime_dispatch

    def fail_ack(_control, _payload):
        raise RuntimeError("private transport detail")

    case.acknowledge = fail_ack
    sessions_opened = []

    def execute(*_args, **_kwargs):
        session = open_session()
        sessions_opened.append(session)
        return session

    isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_dispatch, "exec", execute)

    assert cli.main(["exec", "demo", "--", "/bin/probe"]) == 1
    captured = capsys.readouterr()
    assert captured.out.encode() == case.output
    assert captured.err.encode().startswith(case.error)
    assert captured.err.count("OCI exec terminal observed") == 1
    assert "reason=completed" in captured.err and "status=exit-code-23" in captured.err
    assert "acknowledgement is unconfirmed" in captured.err and "mailbox occupancy is unknown" in captured.err
    assert "private transport detail" not in captured.err
    assert sessions_opened[0]._result is None
    assert sessions_opened[0].observed_completion.acknowledgement == "unconfirmed"
    assert len([call for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]) == 1


def test_cli_preserves_fixed_monitor_timeout_source(monkeypatch, tmp_path, capsys):
    from palimpsest_local import cli, runtime_dispatch

    expected = MonitorClientError(
        "OCI monitor client timed out; preserve the run evidence",
        timeout_source=MonitorClientTimeoutSource.RUN_LOCK_TIMEOUT,
    )

    def execute(*_args, **_kwargs):
        raise expected

    isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_dispatch, "exec", execute)

    assert cli.main(["exec", "demo", "--", "/bin/probe"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == str(expected)
    assert captured.err.endswith("; timeout-source=run-lock-timeout\n")


def test_repeated_embedded_cli_exec_closes_each_client_after_ack(case, monkeypatch, tmp_path, capsys):
    from palimpsest_local import cli, runtime_dispatch

    isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_dispatch, "exec", lambda *args, **kwargs: open_session())
    for _ in range(2):
        assert cli.main(["exec", "demo", "--", "/bin/probe"]) == 23
        captured = capsys.readouterr()
        assert captured.out.encode() == case.output and captured.err.encode() == case.error
    assert case.calls.count("client") == case.calls.count("close") == 2
    assert case.control.status()["next_sequence"] == 3
    assert not case.control.status()["occupied"]


@pytest.mark.parametrize("requested,expected", [(None, 30000), (1, 1), (150000, 150000), (600000, 600000)])
def test_effective_timeout_resolves_absent_request_to_thirty_seconds(requested, expected):
    request = sessions.ExecRequest.from_argv(["/bin/probe"], timeout_ms=requested)
    assert sessions.effective_exec_timeout_ms(request) == expected


@pytest.mark.parametrize("timeout_ms,submitted", [(None, 30000), (150000, 150000)])
def test_session_submits_its_resolved_guest_timeout(case, timeout_ms, submitted):
    arguments = {} if timeout_ms is None else {"timeout_ms": timeout_ms}
    session = sessions.OCIExecProcessSession(object(), object(), object(), ("/bin/probe",), **arguments)
    payloads = [call[1] for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]
    assert [item["timeout_ms"] for item in payloads] == [submitted]
    session.close()


@pytest.mark.parametrize("requested,submitted", [(None, 30000), (150000, 150000)])
def test_exec_session_submits_the_requested_guest_timeout(case, monkeypatch, requested, submitted):
    record = object()
    binding = SimpleNamespace(record=record)
    monkeypatch.setattr(sessions, "load_oci_run_binding", lambda _roots, _name: binding)
    monkeypatch.setattr(sessions, "locked_existing_run", lambda *_args, **_kwargs: nullcontext(SimpleNamespace()))
    monkeypatch.setattr(sessions, "_read_run_journal", lambda _mutation, _binding: SimpleNamespace(endpoint=object()))
    session = sessions.exec_session(
        "demo",
        sessions.ExecRequest.from_argv(["/bin/probe"], timeout_ms=requested),
        roots=object(),
        _expected_record=record,
    )
    payloads = [call[1] for call in case.calls if isinstance(call, tuple) and call[0] == "submit"]
    assert [item["timeout_ms"] for item in payloads] == [submitted]
    session.close()


def test_cli_timeout_seconds_reach_dispatch_as_milliseconds(case, monkeypatch, tmp_path, capsys):
    from palimpsest_local import cli, runtime_dispatch

    observed = {}

    def execute(name, argv, **kwargs):
        observed.update(name=name, argv=list(argv), **kwargs)
        return open_session()

    isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_dispatch, "exec", execute)
    assert cli.main(["exec", "--timeout", "150", "demo", "--", "/bin/probe"]) == 23
    assert observed["timeout_ms"] == 150000 and observed["argv"] == ["/bin/probe"]


def test_cli_omitted_timeout_leaves_the_dispatch_default(case, monkeypatch, tmp_path, capsys):
    from palimpsest_local import cli, runtime_dispatch

    observed = {}

    def execute(name, argv, **kwargs):
        observed.update(kwargs)
        return open_session()

    isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_dispatch, "exec", execute)
    assert cli.main(["exec", "demo", "--", "/bin/probe"]) == 23
    assert observed["timeout_ms"] is None


@pytest.mark.parametrize("seconds", ["0", "601", "-5", "1.5", "30s"])
def test_cli_rejects_out_of_range_timeout_without_dispatching(monkeypatch, tmp_path, seconds):
    from palimpsest_local import cli, runtime_dispatch

    def execute(*_args, **_kwargs):
        raise AssertionError("dispatch must not run for an invalid timeout")

    isolate_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_dispatch, "exec", execute)
    assert cli.main(["exec", "--timeout", seconds, "demo", "--", "/bin/probe"]) == 2
