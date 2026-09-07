"""Client uncertainty retries the same logical job without releasing its boot pin."""

import copy
import os
import uuid

import pytest
from test_oci_monitor_recovery import case as _recovery_case

from palimpsest_local import oci_exec_session as sessions
from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local.oci_exec_control import MonitorExecControl
from palimpsest_local.oci_monitor_client import MonitorClient, MonitorClientError
from palimpsest_local.runtime_types import ProcessExit, ProcessExitCategory

case = _recovery_case


def test_lost_submit_ack_retries_exact_sequence_token_argv_and_timeout(case, monkeypatch):
    calls = []
    payload = {"sequence": 1, "token": str(uuid.uuid4()), "argv": ["/bin/probe", "literal $VALUE"], "timeout_ms": 100}
    response = {"state": "queued"}

    def exchange(fd, endpoint, operation, value, *, timeout):
        calls.append((fd, endpoint, operation, copy.deepcopy(value), timeout))
        if len(calls) == 1:
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.TIMEOUT)
        return response

    monkeypatch.setattr(ipc, "request_monitor_exec", exchange)
    before = case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes()
    with MonitorClient(case.roots, case.binding, case.snapshot.endpoint) as client:
        assert client.exec_request("submit", payload) == response
    assert len(calls) == 2 and calls[0][:4] == calls[1][:4]
    assert calls[0][2:4] == ("submit", payload)
    assert 0.1 <= calls[0][4] <= 2 and 0.1 <= calls[1][4] <= 5
    assert before == (case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes())


def test_non_timeout_authority_failure_is_not_retried(case, monkeypatch):
    calls = []

    def exchange(*args, **kwargs):
        calls.append(True)
        raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.BINDING_MISMATCH)

    monkeypatch.setattr(ipc, "request_monitor_exec", exchange)
    with MonitorClient(case.roots, case.binding, case.snapshot.endpoint) as client:
        with pytest.raises(MonitorClientError):
            client.exec_request("status", {})
    assert calls == [True]


def test_real_pinned_client_ack_failure_retains_process_local_completion_and_closes_on_caller_close(case, monkeypatch):
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
        if operation == "acknowledge":
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.BINDING_MISMATCH)
        return getattr(control, operation)(**payload)

    monkeypatch.setattr(ipc, "request_monitor_exec", exchange)
    durable_before = case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes()
    session = sessions.OCIExecProcessSession(
        case.roots,
        case.binding,
        case.snapshot.endpoint,
        ("/bin/probe",),
    )
    descriptor = session._client._fd

    with pytest.raises(sessions.OCIExecAcknowledgementError) as caught:
        list(session.events())

    assert caught.value.observation == sessions.OCIExecCompletionObservation(
        ProcessExit(31, 31, None, ProcessExitCategory.EXITED),
        "completed",
        18,
        0,
        "unconfirmed",
    )
    assert session.observed_completion == caught.value.observation
    assert control.status()["occupied"] is True
    assert [operation for operation, _payload in calls].count("submit") == 1
    assert [operation for operation, _payload in calls].count("acknowledge") == 1
    assert os.fstat(descriptor).st_ino == case.directory.stat().st_ino
    assert durable_before == (case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes())

    session.close()
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert durable_before == (case.journal.read_bytes(), case.state.read_bytes(), case.owner.read_bytes())
