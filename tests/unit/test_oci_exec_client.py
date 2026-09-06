"""Client uncertainty retries the same logical job without releasing its boot pin."""

import copy
import uuid

import pytest
from test_oci_monitor_recovery import case as _recovery_case

from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local.oci_monitor_client import MonitorClient, MonitorClientError

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
