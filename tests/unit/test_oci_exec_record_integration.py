"""OCI exec routing, publication barriers, and offline inspection integration."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_runtime_dispatch as dispatch_fixtures
from test_oci_monitor_recovery import case as _recovery_case

from palimpsest_local import cli, runtime_dispatch
from palimpsest_local import oci_exec_record as records
from palimpsest_local import oci_exec_session as sessions
from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local.errors import StateError
from palimpsest_local.oci_exec_control import MonitorExecControl
from palimpsest_local.runtime_types import ExecRequest, ProcessOutputEvent, ProcessStatusEvent, RuntimeKind

case = _recovery_case


class DeliberateCancellation(BaseException):
    pass


class RecordingWriter:
    def __init__(self, timeline: list[str], *, fail: str | None = None, failure: BaseException | None = None):
        self.timeline = timeline
        self.fail = fail
        self.failure = failure or RuntimeError("raw storage path /private/secret")
        self.observed = None
        self.confirmed = None
        self.closed = 0

    def publish_observed(self, observation):
        self.timeline.append("observed")
        self.observed = observation
        if self.fail == "observed":
            raise self.failure

    def publish_confirmed(self, observation):
        self.timeline.append("confirmed")
        self.confirmed = observation
        if self.fail == "confirmed":
            raise self.failure

    def close(self):
        self.timeline.append("writer-close")
        self.closed += 1


@pytest.fixture
def mailbox(monkeypatch):
    value = SimpleNamespace(
        control=MonitorExecControl(),
        timeline=[],
        output=b"stdout",
        error=b"stderr",
        code=23,
        reason="completed",
        ack_mode="success",
        poll_failure=None,
        poll_mutate=lambda result: result,
        client_closed=0,
    )
    value.control.mark_ready()

    class Client:
        def __init__(self, *_args):
            value.timeline.append("client")

        def exec_request(self, operation, payload, *, timeout):
            assert 0.1 <= timeout <= 5.0
            value.timeline.append(operation)
            if operation == "poll":
                if value.poll_failure is not None:
                    raise value.poll_failure
                job = value.control.take_exec()
                if job is not None:
                    value.control.append_output(job, "stdout", 0, value.output)
                    value.control.append_output(job, "stderr", 0, value.error)
                    value.control.complete(
                        job,
                        {"exit_code": value.code, "signal": None},
                        len(value.output),
                        len(value.error),
                        value.reason,
                    )
            if operation == "acknowledge":
                if value.ack_mode == "fail-before":
                    raise RuntimeError("raw mailbox token")
                result = value.control.acknowledge(**payload)
                if value.ack_mode == "fail-after":
                    raise RuntimeError("raw mailbox token")
                if value.ack_mode == "invalid":
                    return {**result, "occupied": "invalid"}
                return result
            result = getattr(value.control, operation)(**payload)
            return value.poll_mutate(result) if operation == "poll" else result

        def close(self):
            value.timeline.append("client-close")
            value.client_closed += 1

    monkeypatch.setattr(sessions, "MonitorClient", Client)
    monkeypatch.setattr(sessions, "OCIExecRecordWriter", RecordingWriter)
    return value


def _session(mailbox, writer):
    return sessions.OCIExecProcessSession(
        object(),
        object(),
        object(),
        ("/bin/probe", "literal $HOME; $(uname)"),
        _record_writer=writer,
    )


def test_observed_barrier_runs_after_all_output_resumes_and_before_first_ack(mailbox):
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    events = session.events()
    assert isinstance(next(events), ProcessOutputEvent)
    assert writer.observed is None and "acknowledge" not in mailbox.timeline
    assert isinstance(next(events), ProcessOutputEvent)
    assert writer.observed is None and "acknowledge" not in mailbox.timeline
    assert isinstance(next(events), ProcessStatusEvent)
    assert (
        mailbox.timeline.index("observed") < mailbox.timeline.index("acknowledge") < mailbox.timeline.index("confirmed")
    )
    assert writer.observed.acknowledgement == "unconfirmed"
    assert writer.confirmed == session.observed_completion
    assert writer.confirmed.acknowledgement == "confirmed"
    assert writer.closed == mailbox.client_closed == 1


def test_pre_ack_storage_failure_retains_observation_never_acks_or_reports_status(mailbox):
    writer = RecordingWriter(mailbox.timeline, fail="observed")
    session = _session(mailbox, writer)
    emitted = []
    with pytest.raises(sessions.OCIExecRecordingError) as caught:
        emitted.extend(session.events())
    assert caught.value.stage == "pre-ack"
    assert caught.value.observation == session.observed_completion == writer.observed
    assert caught.value.observation.acknowledgement == "unconfirmed"
    assert "acknowledge" not in mailbox.timeline and writer.confirmed is None
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    assert session._result is None and mailbox.control.status()["occupied"] is True
    assert "/private" not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None
    with pytest.raises(StateError):
        session.wait()
    assert writer.closed == mailbox.client_closed == 1


def test_post_ack_storage_failure_keeps_live_confirmation_without_false_success(mailbox):
    writer = RecordingWriter(mailbox.timeline, fail="confirmed")
    session = _session(mailbox, writer)
    emitted = []
    with pytest.raises(sessions.OCIExecRecordingError) as caught:
        emitted.extend(session.events())
    assert caught.value.stage == "post-ack"
    assert caught.value.observation == session.observed_completion == writer.confirmed
    assert caught.value.observation.acknowledgement == "confirmed"
    assert mailbox.control.status() == {"state": "ready", "next_sequence": 2, "occupied": False}
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    assert session._result is None
    with pytest.raises(StateError):
        session.wait()
    assert writer.closed == mailbox.client_closed == 1


@pytest.mark.parametrize("reason", ["timeout", "output-limit", "cancelled"])
def test_recorded_noncompletion_confirms_facts_but_never_reports_process_success(mailbox, reason):
    mailbox.code = 0
    mailbox.reason = reason
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    emitted = []
    with pytest.raises(StateError, match=reason):
        emitted.extend(session.events())
    assert writer.confirmed == session.observed_completion
    assert writer.confirmed.terminal.exit_code == 0
    assert writer.confirmed.reason == reason and writer.confirmed.acknowledgement == "confirmed"
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    assert session._result is None and writer.closed == mailbox.client_closed == 1


@pytest.mark.parametrize("mode", ["fail-before", "fail-after"])
def test_ordinary_ack_failure_stays_acknowledgement_error_and_never_confirms(mailbox, mode):
    mailbox.ack_mode = mode
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    with pytest.raises(sessions.OCIExecAcknowledgementError) as caught:
        list(session.events())
    assert caught.value.observation == writer.observed
    assert caught.value.observation.acknowledgement == "unconfirmed"
    assert writer.confirmed is None and mailbox.timeline.count("acknowledge") == 1
    assert mailbox.control.status()["occupied"] is (mode == "fail-before")
    assert writer.closed == mailbox.client_closed == 1
    session.close()
    assert writer.closed == mailbox.client_closed == 1


def test_invalid_ack_response_retains_observed_facts_without_status_or_confirmation(mailbox):
    mailbox.ack_mode = "invalid"
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    emitted = []
    with pytest.raises(sessions.OCIExecAcknowledgementError) as caught:
        emitted.extend(session.events())
    assert caught.value.observation == session.observed_completion == writer.observed
    assert caught.value.observation.acknowledgement == "unconfirmed"
    assert writer.confirmed is None and session._result is None
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    assert mailbox.control.status() == {"state": "ready", "next_sequence": 2, "occupied": False}
    assert writer.closed == mailbox.client_closed == 1


def test_poll_failure_and_abandoned_output_consumer_publish_no_observation(mailbox):
    writer = RecordingWriter(mailbox.timeline)
    mailbox.poll_failure = StateError("invalid poll")
    session = _session(mailbox, writer)
    with pytest.raises(StateError, match="invalid poll"):
        list(session.events())
    assert writer.observed is writer.confirmed is session.observed_completion is None
    assert "acknowledge" not in mailbox.timeline
    assert writer.closed == mailbox.client_closed == 1

    mailbox.poll_failure = None
    mailbox.control = MonitorExecControl()
    mailbox.control.mark_ready()
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    events = session.events()
    assert isinstance(next(events), ProcessOutputEvent)
    events.close()
    assert writer.observed is writer.confirmed is session.observed_completion is None
    assert writer.closed == 1


def test_incomplete_terminal_and_output_consumer_failure_publish_nothing(mailbox, monkeypatch):
    writer = RecordingWriter(mailbox.timeline)
    mailbox.poll_mutate = lambda result: {**result, "terminal": None}
    session = _session(mailbox, writer)
    with pytest.raises(StateError, match="terminal is invalid"):
        list(session.events())
    assert writer.observed is writer.confirmed is session.observed_completion is None
    assert "acknowledge" not in mailbox.timeline

    mailbox.control = MonitorExecControl()
    mailbox.control.mark_ready()
    mailbox.poll_mutate = lambda result: result
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    monkeypatch.setattr(cli, "_write_process_bytes", lambda *_a: (_ for _ in ()).throw(StateError("consumer failed")))
    with pytest.raises(StateError, match="consumer failed"):
        cli._run_process_session(session, interactive=False)
    assert writer.observed is writer.confirmed is session.observed_completion is None
    assert writer.closed == 1


@pytest.mark.parametrize(
    "interruption",
    [KeyboardInterrupt(), SystemExit(7), asyncio.CancelledError(), DeliberateCancellation()],
)
def test_recording_baseexception_identity_is_preserved_and_descriptors_close(mailbox, interruption):
    writer = RecordingWriter(mailbox.timeline, fail="observed", failure=interruption)
    session = _session(mailbox, writer)
    with pytest.raises(type(interruption)) as caught:
        list(session.events())
    assert caught.value is interruption
    assert session.observed_completion.acknowledgement == "unconfirmed"
    assert "acknowledge" not in mailbox.timeline
    assert writer.closed == mailbox.client_closed == 1


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(7), DeliberateCancellation()])
def test_confirmed_recording_baseexception_keeps_live_confirmation_and_closes(mailbox, interruption):
    writer = RecordingWriter(mailbox.timeline, fail="confirmed", failure=interruption)
    session = _session(mailbox, writer)
    with pytest.raises(type(interruption)) as caught:
        list(session.events())
    assert caught.value is interruption
    assert session.observed_completion == writer.confirmed
    assert session.observed_completion.acknowledgement == "confirmed"
    assert mailbox.control.status() == {"state": "ready", "next_sequence": 2, "occupied": False}
    assert session._result is None and writer.closed == mailbox.client_closed == 1


def test_recording_error_is_fixed_and_observation_is_immutable():
    observation = sessions.OCIExecCompletionObservation(None, "cancelled", 0, 0, "unconfirmed")
    error = sessions.OCIExecRecordingError("pre-ack", observation)
    assert error.stage == "pre-ack" and error.observation is observation
    assert "path" not in str(error) and "token" not in str(error)
    with pytest.raises(FrozenInstanceError):
        error.observation.reason = "completed"
    with pytest.raises(ValueError):
        sessions.OCIExecRecordingError("post-ack", observation)


def test_constructor_baseexception_closes_accepted_writer_without_replacement(mailbox, monkeypatch):
    interruption = DeliberateCancellation()
    writer = RecordingWriter(mailbox.timeline)
    monkeypatch.setattr(sessions.threading, "Event", lambda: (_ for _ in ()).throw(interruption))
    with pytest.raises(DeliberateCancellation) as caught:
        _session(mailbox, writer)
    assert caught.value is interruption and writer.closed == 1


@pytest.mark.parametrize("timeout_ms", [None, "30000"])
def test_constructor_validates_timeout_before_arithmetic_and_closes_writer(mailbox, timeout_ms):
    writer = RecordingWriter(mailbox.timeline)
    with pytest.raises(StateError, match="invalid-request"):
        sessions.OCIExecProcessSession(
            object(),
            object(),
            object(),
            ("true",),
            timeout_ms=timeout_ms,
            _record_writer=writer,
        )
    assert writer.closed == 1
    assert "client" not in mailbox.timeline and "submit" not in mailbox.timeline


@pytest.mark.parametrize("boundary", ["deadline", "uuid"])
def test_constructor_boundary_baseexception_closes_writer_without_submit(mailbox, monkeypatch, boundary):
    interruption = DeliberateCancellation()
    writer = RecordingWriter(mailbox.timeline)

    def interrupt(*_args, **_kwargs):
        raise interruption

    if boundary == "deadline":
        monkeypatch.setattr(sessions, "_Deadline", interrupt)
    else:
        monkeypatch.setattr(sessions.uuid, "uuid4", interrupt)
    with pytest.raises(DeliberateCancellation) as caught:
        _session(mailbox, writer)
    assert caught.value is interruption and writer.closed == 1
    assert "client" not in mailbox.timeline and "submit" not in mailbox.timeline


@pytest.mark.parametrize("failure", ["poll", "interrupt", "consumer"])
def test_no_option_failures_retain_client_until_caller_close(mailbox, failure):
    if failure == "poll":
        mailbox.poll_failure = StateError("invalid poll")
    session = sessions.OCIExecProcessSession(object(), object(), object(), ("true",))
    events = session.events()
    if failure == "poll":
        with pytest.raises(StateError, match="invalid poll"):
            next(events)
    else:
        assert isinstance(next(events), ProcessOutputEvent)
        if failure == "interrupt":
            interruption = KeyboardInterrupt()
            with pytest.raises(KeyboardInterrupt) as caught:
                events.throw(interruption)
            assert caught.value is interruption
        else:
            events.close()
    assert mailbox.client_closed == 0
    session.close()
    assert mailbox.client_closed == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork safety requires os.fork")
def test_fork_after_output_yield_cannot_resume_session_or_publish(mailbox):
    writer = RecordingWriter(mailbox.timeline)
    session = _session(mailbox, writer)
    events = session.events()
    assert isinstance(next(events), ProcessOutputEvent)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            next(events)
        except StateError:
            outcome = b"blocked" if writer.observed is None else b"published"
        else:
            outcome = b"resumed"
        os.write(write_fd, outcome)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 32) == b"blocked"
        _pid, status = os.waitpid(child, 0)
        assert status == 0
    finally:
        os.close(read_fd)
    assert isinstance(next(events), ProcessOutputEvent)
    assert isinstance(next(events), ProcessStatusEvent)


def _ledger(tmp_path: Path, *, runtime_kind: str = "oci-root"):
    roots = dispatch_fixtures._roots(tmp_path)
    dispatch_fixtures._write_ledger(
        roots,
        record={"schema_version": 2, "runtime_kind": runtime_kind, "backend": "kvm", "status": "running"},
    )
    return roots


def test_recorded_dispatch_validates_preflights_reserves_then_submits_once(tmp_path, monkeypatch):
    roots = _ledger(tmp_path)
    timeline = []
    writer = SimpleNamespace(close=lambda: timeline.append("writer-close"))
    session = dispatch_fixtures._FakeProcessSession()

    class WriterType:
        @classmethod
        def reserve(cls, path, *, managed_state):
            assert path == "/private/raw/record" and managed_state == roots.state
            timeline.append("pending")
            return writer

    record = runtime_dispatch.resolve_existing_run("demo", roots=roots)

    def enter_session(name, request, **kwargs):
        assert name == "demo" and request.argv == ("/bin/probe", "literal")
        assert kwargs.get("_record_writer") is writer
        timeline.append("submit")
        return session

    adapter = SimpleNamespace(exec_session=enter_session)

    def preflight(current, operation, selected_roots):
        assert current == record and selected_roots == roots
        timeline.append("preflight")
        return adapter

    monkeypatch.setattr(runtime_dispatch, "_preflight_existing_adapter", preflight)
    monkeypatch.setattr(records, "OCIExecRecordWriter", WriterType)
    assert (
        runtime_dispatch.exec("demo", ["/bin/probe", "literal"], roots=roots, completion_record="/private/raw/record")
        is session
    )
    assert timeline == ["preflight", "pending", "submit"]


@pytest.mark.parametrize("argv", [[""], ["x"] * 65, ["x", "\ud800"]])
def test_invalid_oci_argv_creates_no_record_preflight_or_submission(tmp_path, monkeypatch, argv):
    roots = _ledger(tmp_path)
    monkeypatch.setattr(runtime_dispatch, "_preflight_existing_adapter", lambda *a: pytest.fail("preflight reached"))
    monkeypatch.setattr(records.OCIExecRecordWriter, "reserve", lambda *a, **k: pytest.fail("record reached"))
    with pytest.raises(StateError, match="invalid-request"):
        runtime_dispatch.exec("demo", argv, roots=roots, completion_record="/private/record")


def test_unsupported_runtime_rejects_recording_before_preflight_or_files(tmp_path, monkeypatch):
    roots = _ledger(tmp_path, runtime_kind="cloud-image")
    monkeypatch.setattr(runtime_dispatch, "_preflight_existing_adapter", lambda *a: pytest.fail("preflight reached"))
    monkeypatch.setattr(records.OCIExecRecordWriter, "reserve", lambda *a, **k: pytest.fail("record reached"))
    with pytest.raises(StateError, match="require the OCI-root runtime"):
        runtime_dispatch.exec("demo", ["true"], roots=roots, completion_record="/private/record")


def test_preflight_failure_creates_no_record_or_submission(tmp_path, monkeypatch):
    roots = _ledger(tmp_path)
    monkeypatch.setattr(
        runtime_dispatch,
        "_preflight_existing_adapter",
        lambda *a: (_ for _ in ()).throw(StateError("preflight failed")),
    )
    monkeypatch.setattr(records.OCIExecRecordWriter, "reserve", lambda *a, **k: pytest.fail("record reached"))
    with pytest.raises(StateError, match="preflight failed"):
        runtime_dispatch.exec("demo", ["true"], roots=roots, completion_record="/private/record")


def test_entry_failure_leaves_pending_and_dispatcher_closes_writer(tmp_path, monkeypatch):
    roots = _ledger(tmp_path)
    timeline = []

    def fail_close():
        timeline.append("writer-close")
        raise RuntimeError("close detail must not replace cancellation")

    writer = SimpleNamespace(close=fail_close)

    class WriterType:
        @classmethod
        def reserve(cls, *_args, **_kwargs):
            timeline.append("pending")
            return writer

    adapter = SimpleNamespace(exec_session=lambda *_a, **_k: (_ for _ in ()).throw(DeliberateCancellation()))
    monkeypatch.setattr(runtime_dispatch, "_preflight_existing_adapter", lambda *_a: adapter)
    monkeypatch.setattr(records, "OCIExecRecordWriter", WriterType)
    cancellation = None
    try:
        runtime_dispatch.exec("demo", ["true"], roots=roots, completion_record="/private/record")
    except DeliberateCancellation as caught:
        cancellation = caught
    assert type(cancellation) is DeliberateCancellation
    assert timeline == ["pending", "writer-close"]


def test_reservation_failure_is_fixed_pre_ack_error_without_observation(tmp_path, monkeypatch):
    roots = _ledger(tmp_path)
    adapter = SimpleNamespace(exec_session=lambda *_a, **_k: pytest.fail("submitted"))
    monkeypatch.setattr(runtime_dispatch, "_preflight_existing_adapter", lambda *_a: adapter)
    monkeypatch.setattr(
        records.OCIExecRecordWriter,
        "reserve",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("/private/secret storage token")),
    )
    with pytest.raises(sessions.OCIExecRecordingError) as caught:
        runtime_dispatch.exec("demo", ["true"], roots=roots, completion_record="/private/secret/record")
    assert caught.value.stage == "pre-ack" and caught.value.observation is None
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_session_entry_repeats_oci_argv_validation_before_binding(monkeypatch):
    monkeypatch.setattr(sessions, "load_oci_run_binding", lambda *_a: pytest.fail("binding reached"))
    with pytest.raises(StateError, match="invalid-request"):
        sessions.exec_session("demo", ExecRequest(("",)), roots=object(), _expected_record=object())


def test_parser_keeps_record_path_raw_and_option_after_name_literal():
    parser = cli.build_parser()
    raw = "/private/parent/../record"
    args = parser.parse_args(["exec", "--completion-record", raw, "demo", "--", "true"])
    assert args.completion_record == raw and type(args.completion_record) is str
    literal = parser.parse_args(["exec", "demo", "--completion-record", raw])
    assert literal.completion_record is None and literal.command == ["--completion-record", raw]


def test_public_recorded_exec_forwards_raw_path_and_literal_argv(monkeypatch):
    raw = "/private/parent/../record"
    candidate = object()
    seen = []

    def execute(name, argv, *, roots, completion_record):
        seen.append((name, argv, roots, completion_record))
        return candidate

    monkeypatch.setattr(cli.runtime_dispatch, "exec", execute)
    monkeypatch.setattr(
        cli,
        "_run_process_session",
        lambda session, *, interactive: 23 if session is candidate and not interactive else pytest.fail(),
    )
    assert cli.main(["exec", "--completion-record", raw, "demo", "--", "printf", "%s", "$HOME"]) == 23
    assert seen[0][0:2] == ("demo", ["printf", "%s", "$HOME"])
    assert seen[0][3] == raw and type(seen[0][3]) is str


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        ("observed", "OCI exec completion recording failed before acknowledgement\n"),
        ("confirmed", "OCI exec completion recording failed after validated acknowledgement\n"),
    ],
)
def test_cli_recording_failure_is_nonzero_with_one_fixed_diagnostic(
    mailbox, tmp_path, monkeypatch, capsys, failure, diagnostic
):
    writer = RecordingWriter(mailbox.timeline, fail=failure)
    session = _session(mailbox, writer)
    roots = _ledger(tmp_path)
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setattr(cli.runtime_dispatch, "exec", lambda *_args, **_kwargs: session)
    assert cli.main(["exec", "--completion-record", "/private/record", "demo", "true"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "stdout"
    assert captured.err == "stderr" + diagnostic
    assert writer.closed == mailbox.client_closed == 1


def test_offline_inspector_runs_before_root_resolution_and_emits_only_snapshot(monkeypatch, capsys):
    payload = {
        "schema": records.SCHEMA,
        "version": 1,
        "record_id": "00000000-0000-4000-8000-000000000000",
        "phase": "pending",
        "observation": None,
        "classification": "local-historical-metadata",
        "guidance": "fixed",
    }
    seen = []
    monkeypatch.setattr(
        records, "read_exec_record", lambda path: seen.append(path) or SimpleNamespace(to_dict=lambda: payload)
    )
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("roots resolved"))
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("roots initialized"))
    monkeypatch.setattr(sessions, "MonitorClient", lambda *_a: pytest.fail("monitor opened"))
    assert cli.main(["oci", "exec-record", "/private/raw/record"]) == 0
    assert seen == ["/private/raw/record"] and json.loads(capsys.readouterr().out) == payload


def test_offline_inspector_failure_is_fixed_and_does_not_echo_path(monkeypatch, capsys):
    monkeypatch.setattr(
        records,
        "read_exec_record",
        lambda _path: (_ for _ in ()).throw(records.OCIExecRecordError("read")),
    )
    assert cli.main(["oci", "exec-record", "/private/secret/record"]) == 1
    captured = capsys.readouterr()
    assert not captured.out and captured.err == "OCI exec record inspection failed\n"


def _assert_descriptors_closed(descriptors):
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor and ACL integration")
@pytest.mark.parametrize(
    ("failure_phase", "disk_phase", "acknowledgements"),
    [("observed", "pending", 0), ("confirmed", "observed", 1)],
)
def test_real_pinned_client_publication_failure_preserves_boundary_facts(
    case, tmp_path, monkeypatch, failure_phase, disk_phase, acknowledgements
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    record_path = private / "record"
    writer = records.OCIExecRecordWriter.reserve(str(record_path), managed_state=case.roots.state)
    writer_descriptors = tuple(writer._chain.fds)
    original_publish = writer._publish

    def publish_at_boundary(phase, observation):
        if phase == failure_phase:
            raise records.OCIExecRecordError(phase)
        return original_publish(phase, observation)

    monkeypatch.setattr(writer, "_publish", publish_at_boundary)
    control = MonitorExecControl()
    control.mark_ready()
    calls = []

    def exchange(descriptor, endpoint, operation, payload, *, timeout):
        assert endpoint == case.snapshot.endpoint
        assert os.fstat(descriptor).st_ino == case.directory.stat().st_ino
        assert 0.1 <= timeout <= 5
        calls.append((operation, copy.deepcopy(payload)))
        if operation == "poll":
            job = control.take_exec()
            if job is not None:
                control.append_output(job, "stdout", 0, b"canonical-boundary")
                control.complete(job, {"exit_code": 31, "signal": None}, 18, 0, "completed")
        return getattr(control, operation)(**payload)

    monkeypatch.setattr(ipc, "request_monitor_exec", exchange)
    durable_before = case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes()
    session = sessions.OCIExecProcessSession(
        case.roots,
        case.binding,
        case.snapshot.endpoint,
        ("/bin/probe",),
        _record_writer=writer,
    )
    monitor_descriptor = session._client._fd
    emitted = []
    with pytest.raises(sessions.OCIExecRecordingError) as caught:
        emitted.extend(session.events())

    assert caught.value.stage == ("pre-ack" if failure_phase == "observed" else "post-ack")
    assert caught.value.observation == session.observed_completion
    assert not any(isinstance(event, ProcessStatusEvent) for event in emitted)
    assert [operation for operation, _payload in calls].count("submit") == 1
    assert [operation for operation, _payload in calls].count("acknowledge") == acknowledgements
    assert control.status()["occupied"] is (failure_phase == "observed")
    _assert_descriptors_closed((*writer_descriptors, monitor_descriptor))
    assert durable_before == (case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes())
    snapshot = records.read_exec_record(str(record_path))
    assert snapshot.phase == disk_phase
    if failure_phase == "observed":
        assert snapshot.observation is None
        assert session.observed_completion.acknowledgement == "unconfirmed"
    else:
        assert snapshot.observation.acknowledgement == "unconfirmed"
        assert session.observed_completion.acknowledgement == "confirmed"
        assert snapshot.observation == writer._observed


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor and ACL integration")
@pytest.mark.parametrize("ack_boundary", ["before-mailbox", "after-mailbox"])
def test_real_recorded_ack_failure_retries_identically_and_closes_owned_fds(case, tmp_path, monkeypatch, ack_boundary):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    record_path = private / "record"
    writer = records.OCIExecRecordWriter.reserve(str(record_path), managed_state=case.roots.state)
    writer_descriptors = tuple(writer._chain.fds)
    control = MonitorExecControl()
    control.mark_ready()
    calls = []
    ack_calls = []

    def exchange(descriptor, endpoint, operation, payload, *, timeout):
        assert endpoint == case.snapshot.endpoint
        assert os.fstat(descriptor).st_ino == case.directory.stat().st_ino
        assert 0.1 <= timeout <= 5
        calls.append((operation, copy.deepcopy(payload), timeout))
        if operation == "poll":
            job = control.take_exec()
            if job is not None:
                control.complete(job, {"exit_code": 31, "signal": None}, 0, 0, "completed")
        if operation == "acknowledge":
            ack_calls.append((copy.deepcopy(payload), timeout))
            if ack_boundary == "after-mailbox":
                control.acknowledge(**payload)
            if len(ack_calls) == 1:
                raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.TIMEOUT)
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.BINDING_MISMATCH)
        return getattr(control, operation)(**payload)

    monkeypatch.setattr(ipc, "request_monitor_exec", exchange)
    durable_before = case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes()
    session = sessions.OCIExecProcessSession(
        case.roots,
        case.binding,
        case.snapshot.endpoint,
        ("/bin/probe",),
        _record_writer=writer,
    )
    monitor_descriptor = session._client._fd
    with pytest.raises(sessions.OCIExecAcknowledgementError) as caught:
        list(session.events())

    assert caught.value.observation == session.observed_completion
    assert caught.value.observation.acknowledgement == "unconfirmed"
    assert [operation for operation, _payload, _timeout in calls].count("submit") == 1
    assert len(ack_calls) == 2 and ack_calls[0][0] == ack_calls[1][0]
    assert 0.1 <= ack_calls[0][1] <= 2 and 0.1 <= ack_calls[1][1] <= 5
    assert control.status()["occupied"] is (ack_boundary == "before-mailbox")
    _assert_descriptors_closed((*writer_descriptors, monitor_descriptor))
    assert durable_before == (case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes())
    snapshot = records.read_exec_record(str(record_path))
    assert snapshot.phase == "observed" and snapshot.observation == session.observed_completion


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor and ACL integration")
def test_record_change_after_reservation_rejects_before_submit_and_retains_pending(case, tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    record_path = private / "record"
    original_reserve = records.OCIExecRecordWriter.reserve
    accepted = {}

    def reserve(cls, path, *, managed_state):
        writer = original_reserve(path, managed_state=managed_state)
        accepted["writer"] = writer
        accepted["descriptors"] = tuple(writer._chain.fds)
        replacement_run_id = str(uuid.uuid4())
        owner = json.loads(case.owner.read_bytes())
        state = json.loads(case.state.read_bytes())
        owner["run_id"] = replacement_run_id
        state["run_id"] = replacement_run_id
        case.owner.write_text(json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8")
        case.state.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        case.owner.chmod(0o600)
        case.state.chmod(0o600)
        return writer

    monkeypatch.setattr(records.OCIExecRecordWriter, "reserve", classmethod(reserve))
    monkeypatch.setattr(ipc, "request_monitor_exec", lambda *_args, **_kwargs: pytest.fail("submitted"))
    with pytest.raises(StateError, match="run ledger changed during dispatch"):
        runtime_dispatch.exec("recover-inactive", ["true"], roots=case.roots, completion_record=str(record_path))
    assert accepted["writer"]._closed is True
    _assert_descriptors_closed(accepted["descriptors"])
    snapshot = records.read_exec_record(str(record_path))
    assert snapshot.phase == "pending" and snapshot.observation is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor and ACL integration")
def test_linux_real_writer_session_and_offline_subprocess_readback(tmp_path, monkeypatch):
    private = tmp_path / "private"
    state_root = tmp_path / "state"
    private.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    record_path = private / "record"
    writer = records.OCIExecRecordWriter.reserve(str(record_path), managed_state=state_root)

    value = SimpleNamespace(control=MonitorExecControl())
    value.control.mark_ready()

    class Client:
        def __init__(self, *_args):
            pass

        def exec_request(self, operation, payload, *, timeout):
            if operation == "poll":
                job = value.control.take_exec()
                if job is not None:
                    value.control.complete(job, {"exit_code": 23, "signal": None}, 0, 0, "completed")
            return getattr(value.control, operation)(**payload)

        def close(self):
            pass

    monkeypatch.setattr(sessions, "MonitorClient", Client)
    session = sessions.OCIExecProcessSession(object(), object(), object(), ("true",), _record_writer=writer)
    assert session.wait().returncode == 23
    snapshot = records.read_exec_record(str(record_path))
    assert snapshot.phase == "confirmed" and snapshot.observation.terminal.exit_code == 23

    offline_record = private / "offline-record"
    writer_program = """
import sys
from dataclasses import replace
from pathlib import Path
from palimpsest_local.oci_exec_record import OCIExecRecordWriter
from palimpsest_local.oci_exec_session import OCIExecCompletionObservation
from palimpsest_local.runtime_types import ProcessExit, ProcessExitCategory
writer = OCIExecRecordWriter.reserve(sys.argv[1], managed_state=Path(sys.argv[2]))
observed = OCIExecCompletionObservation(ProcessExit(23, 23, None, ProcessExitCategory.EXITED), 'completed', 0, 0, 'unconfirmed')
writer.publish_observed(observed)
writer.publish_confirmed(replace(observed, acknowledgement='confirmed'))
writer.close()
"""
    created = subprocess.run(
        [sys.executable, "-c", writer_program, str(offline_record), str(state_root)],
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0 and not created.stderr

    environment = dict(os.environ)
    environment["PALIMPSEST_STATE_HOME"] = str(tmp_path / "absent-runtime-state")
    inspected = subprocess.run(
        [sys.executable, "-m", "palimpsest_local.cli", "oci", "exec-record", str(offline_record)],
        cwd=Path(__file__).parents[2],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert inspected.returncode == 0 and not inspected.stderr
    payload = json.loads(inspected.stdout)
    assert payload["phase"] == "confirmed" and payload["observation"]["terminal"]["exit_code"] == 23
    assert not (tmp_path / "absent-runtime-state").exists()
    assert RuntimeKind.OCI_ROOT.value not in payload["guidance"]
