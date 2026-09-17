"""The existing authenticated monitor socket carries only fast mailbox operations."""

import os
import threading
import uuid
from types import SimpleNamespace

import pytest
import test_oci_monitor_ipc as fixtures

from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local.oci_exec_control import MonitorExecControl, MonitorExecControlError
from palimpsest_local.oci_monitor_control import MonitorStopControl


@pytest.fixture
def case(monkeypatch):
    worker = SimpleNamespace(
        exec_control=MonitorExecControl(), stop_control=MonitorStopControl(), done=threading.Event(), failed=False
    )
    worker.stop_control.mark_ready()
    value = SimpleNamespace(
        worker=worker,
        lease=SimpleNamespace(_mutex=threading.Lock(), snapshot=SimpleNamespace(phase="ready")),
        sent=[],
        peers=[],
        token=str(uuid.uuid4()),
    )
    value.bound = SimpleNamespace(validate=lambda: None)
    monkeypatch.setattr(ipc, "_authorize_peer", lambda *args: value.peers.append(args))
    monkeypatch.setattr(ipc, "_send_frame", lambda channel, response: value.sent.append(response))
    return value


def exchange(case, operation, payload):
    request = ipc._exec_request_message(
        fixtures._identity(), fixtures._process(), str(uuid.uuid4()), operation, payload
    )
    ipc._serve_exec_request(
        request, object(), case.bound, fixtures._identity(), fixtures._process(), case.lease, case.worker
    )
    return case.sent[-1]


def submission(case):
    return {"sequence": 1, "token": case.token, "argv": ["/bin/probe"], "timeout_ms": 30000}


def test_lost_submit_response_retry_does_not_duplicate_worker_job(case):
    assert exchange(case, "status", {})["payload"]["state"] == "ready"
    first = exchange(case, "submit", submission(case))
    second = exchange(case, "submit", submission(case))
    assert first["payload"] == second["payload"] and first["error"] is None
    job = case.worker.exec_control.take_exec()
    assert case.worker.exec_control.take_exec() is None
    case.worker.exec_control.complete(job, {"exit_code": 0, "signal": None}, 0, 0, "completed")
    result = exchange(case, "poll", {"sequence": 1, "token": case.token, "stdout_offset": 0, "stderr_offset": 0})
    assert result["payload"]["terminal"] == {"exit_code": 0, "signal": None}
    assert len(case.peers) == 4
    exchange(case, "acknowledge", {"sequence": 1, "token": case.token})
    assert exchange(case, "submit", submission(case))["error"] == "stale-sequence"


@pytest.mark.parametrize(
    "phase,failed,done,stopped",
    [
        ("committed", False, False, False),
        ("active", False, False, False),
        ("terminal", False, True, False),
        ("ready", True, False, False),
        ("ready", False, True, False),
        ("ready", False, False, True),
    ],
)
def test_submission_requires_durable_ready_live_worker_and_no_accepted_stop(case, phase, failed, done, stopped):
    case.lease.snapshot.phase, case.worker.failed = phase, failed
    if done:
        case.worker.done.set()
    if stopped:
        assert case.worker.stop_control.request() == "stop-accepted"
    result = exchange(case, "submit", submission(case))
    assert result["error"] == "not-ready" and not case.worker.exec_control.status()["occupied"]


@pytest.mark.parametrize("damage", ["extra", "run_id", "generation", "sequence", "surrogate", "operation"])
def test_malformed_or_foreign_request_is_rejected_before_peer_or_mailbox(case, damage):
    request = ipc._exec_request_message(
        fixtures._identity(), fixtures._process(), str(uuid.uuid4()), "submit", submission(case)
    )
    if damage == "extra":
        request["extra"] = 1
    elif damage in {"run_id", "generation"}:
        request[damage] = str(uuid.uuid4())
    elif damage == "sequence":
        request["payload"]["sequence"] = True
    elif damage == "surrogate":
        request["payload"]["argv"] = ["\ud800"]
    else:
        request["operation"] = "arbitrary"
    with pytest.raises(ipc.MonitorIPCError):
        ipc._serve_exec_request(
            request, object(), case.bound, fixtures._identity(), fixtures._process(), case.lease, case.worker
        )
    assert not case.peers and not case.sent and not case.worker.exec_control.status()["occupied"]


def test_peer_authorization_failure_never_enqueues_job(case, monkeypatch):
    monkeypatch.setattr(
        ipc,
        "_authorize_peer",
        lambda *args: (_ for _ in ()).throw(ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.BINDING_MISMATCH)),
    )
    with pytest.raises(ipc.MonitorIPCError):
        exchange(case, "submit", submission(case))
    assert not case.worker.exec_control.status()["occupied"]


@pytest.mark.parametrize("damage", [None, "writer", "generation", "schema", "extra", "error"])
def test_mailbox_client_requires_exact_reply_binding(tmp_path, monkeypatch, damage):
    descriptor = fixtures._directory(tmp_path / "monitor")
    endpoint = ipc.MonitorExecEndpoint(fixtures._identity(), fixtures._process(), 12, 34)
    captured = []

    class Channel:
        def __init__(self, *args):
            pass

        def settimeout(self, timeout):
            assert 0 < timeout <= 5

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def response(channel):
        value = ipc._exec_response_message(
            endpoint.identity,
            endpoint.writer,
            captured[-1]["request_id"],
            "status",
            {"state": "ready", "next_sequence": 1, "occupied": False},
        )
        if damage == "writer":
            value["writer"] = fixtures._process(555).to_dict()
        elif damage == "generation":
            value["generation"] = str(uuid.uuid4())
        elif damage == "schema":
            value["schema"] = "foreign"
        elif damage == "extra":
            value["extra"] = True
        elif damage == "error":
            value["error"] = "private unbounded details"
        return value

    monkeypatch.setattr(ipc.socket, "socket", Channel)
    monkeypatch.setattr(ipc, "current_process_identity", fixtures._process)
    monkeypatch.setattr(ipc, "_visible_socket", lambda *args: SimpleNamespace(st_dev=12, st_ino=34))
    monkeypatch.setattr(ipc, "_authorize_peer", lambda *args: None)
    monkeypatch.setattr(ipc, "_connect_socket", lambda *args: None)
    monkeypatch.setattr(ipc, "_send_frame", lambda channel, request: captured.append(request))
    monkeypatch.setattr(ipc, "_recv_frame", response)
    try:
        if damage is None:
            assert ipc.request_monitor_exec(descriptor, endpoint, "status", {})["state"] == "ready"
        else:
            with pytest.raises(ipc.MonitorIPCError):
                ipc.request_monitor_exec(descriptor, endpoint, "status", {})
    finally:
        os.close(descriptor)


def test_mailbox_busy_is_bounded_explicit_response(case):
    exchange(case, "submit", submission(case))
    payload = {**submission(case), "token": str(uuid.uuid4())}
    result = exchange(case, "submit", payload)
    assert result["payload"] is None and result["error"] == "busy"
    with pytest.raises(MonitorExecControlError):
        case.worker.exec_control.submit(**payload)


def test_queued_exec_does_not_block_authenticated_stop_server(case, monkeypatch):
    requests = iter(
        [
            ipc._exec_request_message(
                fixtures._identity(), fixtures._process(), str(uuid.uuid4()), "submit", submission(case)
            ),
            ipc._request_message(
                ipc.MonitorIPCOperation.STOP, fixtures._identity(), fixtures._process(), str(uuid.uuid4())
            ),
            ipc._exec_request_message(
                fixtures._identity(),
                fixtures._process(),
                str(uuid.uuid4()),
                "poll",
                {"sequence": 1, "token": case.token, "stdout_offset": 0, "stderr_offset": 0},
            ),
        ]
    )

    class Channel:
        def settimeout(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Listener:
        def settimeout(self, timeout):
            pass

        def accept(self):
            if len(case.sent) == 3:
                raise OSError("end test")
            return Channel(), None

    case.bound.listener = Listener()
    monkeypatch.setattr(ipc, "_recv_frame", lambda channel: next(requests))
    with pytest.raises(ipc.MonitorIPCError):
        ipc._serve_committed(case.bound, fixtures._identity(), fixtures._process(), 1, case.lease, case.worker)
    assert case.sent[0]["payload"]["state"] == "queued"
    assert case.sent[1]["state"] == "stop-accepted"
    assert case.sent[2]["payload"]["terminal"] is None
    assert case.worker.exec_control.status()["state"] == "stopping"
    assert case.worker.stop_control.take_stop() and case.worker.exec_control.take_exec() is None
