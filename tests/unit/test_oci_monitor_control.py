from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from palimpsest_local.oci_monitor_control import MonitorStopControl


def test_stop_is_ready_only_and_retries_coalesce_without_resetting_deadline(monkeypatch):
    monkeypatch.setattr("palimpsest_local.oci_monitor_control.time.monotonic", lambda: 10.0)
    control = MonitorStopControl()
    assert control.request() == "stop-refused"
    assert not control.accepted and control.deadline is None
    control.mark_ready()
    assert control.request() == "stop-accepted"
    assert control.deadline == 40.0
    monkeypatch.setattr("palimpsest_local.oci_monitor_control.time.monotonic", lambda: 20.0)
    assert control.request() == "stop-accepted"
    assert control.deadline == 40.0
    assert control.take_stop()
    assert not control.take_stop()
    assert control.request() == "stop-accepted"
    control.mark_observed_terminal()
    assert control.request() == "stop-accepted"
    control.mark_terminal()
    assert control.request() == "stop-terminal"


def test_natural_terminal_closes_admission_before_durability():
    control = MonitorStopControl()
    control.mark_ready()
    control.mark_observed_terminal()
    assert control.request() == "stop-refused"
    assert not control.accepted
    control.mark_terminal()
    assert control.request() == "stop-terminal"
    control.mark_control_lost()
    control.mark_ready()
    assert control.request() == "stop-terminal"


def test_lost_control_cannot_be_readmitted():
    control = MonitorStopControl()
    control.mark_ready()
    assert control.request() == "stop-accepted"
    control.mark_control_lost()
    assert control.accepted
    assert not control.take_stop()
    control.mark_ready()
    control.mark_terminal()
    assert control.request() == "control-lost"


def test_parallel_requests_have_one_consumer_and_one_deadline():
    control = MonitorStopControl()
    control.mark_ready()
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(lambda _: control.request(), range(100))) == {"stop-accepted"}
        deadline = control.deadline
        assert sum(pool.map(lambda _: control.take_stop(), range(100))) == 1
        assert set(pool.map(lambda _: control.request(), range(100))) == {"stop-accepted"}
    assert control.deadline == deadline
