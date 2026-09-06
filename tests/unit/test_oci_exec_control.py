"""A lost acknowledgement cannot turn a retry into a second guest process."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from palimpsest_local.oci_exec_control import MonitorExecControl, MonitorExecControlError


def ready():
    control = MonitorExecControl()
    control.mark_ready()
    return control


def submitted(control):
    token = str(uuid.uuid4())
    control.submit(1, token, ("/bin/probe",), 30000)
    return token


def test_single_job_duplicate_submit_and_take_are_exactly_once():
    control = ready()
    token = submitted(control)
    assert control.submit(1, token, ["/bin/probe"], 30000)["state"] == "queued"
    job = control.take_exec()
    assert control.take_exec() is None
    assert control.submit(1, token, ["/bin/probe"], 30000)["state"] == "running"
    control.append_output(job, "stdout", 0, b"out\0")
    control.append_output(job, "stderr", 0, b"err")
    control.complete(job, {"exit_code": 7, "signal": None}, 4, 3, "completed")
    result = control.poll(1, token)
    assert bytes.fromhex(result["stdout_hex"]) == b"out\0"
    assert bytes.fromhex(result["stderr_hex"]) == b"err"
    assert result["terminal"] == {"exit_code": 7, "signal": None}
    result["terminal"]["exit_code"] = 0
    assert control.poll(1, token)["terminal"]["exit_code"] == 7
    assert control.submit(1, token, ["/bin/probe"], 30000)["state"] == "completed"
    assert control.acknowledge(1, token) == control.acknowledge(1, token)
    for candidate in (token, str(uuid.uuid4())):
        with pytest.raises(MonitorExecControlError, match="stale-sequence"):
            control.submit(1, candidate, ["/bin/probe"], 30000)
    control.submit(2, str(uuid.uuid4()), ["/bin/probe"], 1)


@pytest.mark.parametrize(
    "argv,timeout",
    [
        ([], 1),
        ([""], 1),
        (["/p", "x\0"], 1),
        (["/p", "\ud800"], 1),
        (["/p", "x" * 8192], 1),
        (["/p"] * 65, 1),
        (["/p"], True),
        (["/p"], 0),
        (["/p"], 30001),
    ],
)
def test_invalid_submission_does_not_poison_ready_mailbox(argv, timeout):
    control = ready()
    before = control.status()
    with pytest.raises(MonitorExecControlError, match="invalid-request"):
        control.submit(1, str(uuid.uuid4()), argv, timeout)
    assert control.status() == before and control.take_exec() is None


def test_parallel_submissions_admit_only_one_literal_request():
    control = ready()
    tokens = [str(uuid.uuid4()) for _ in range(8)]

    def submit(token):
        try:
            return control.submit(1, token, ["/bin/probe"], 1)["token"]
        except MonitorExecControlError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [item for item in pool.map(submit, tokens) if item is not None]
    assert len(winners) == 1 and control.take_exec().token == winners[0]


@pytest.mark.parametrize("reason", ["stopping", "terminal", "control-lost"])
def test_closing_admission_never_fabricates_guest_completion(reason):
    control = ready()
    token = submitted(control)
    job = control.take_exec()
    control.close_to_exec(reason)
    control.mark_ready()
    assert control.status()["state"] == reason
    assert control.poll(1, token)["terminal"] is None
    with pytest.raises(MonitorExecControlError):
        control.submit(1, str(uuid.uuid4()), ["/bin/other"], 1)
    if reason == "stopping":
        control.complete(job, {"exit_code": None, "signal": 9}, 0, 0, "cancelled")
        assert control.poll(1, token)["state"] == "completed"
    else:
        with pytest.raises(MonitorExecControlError):
            control.complete(job, {"exit_code": 0, "signal": None}, 0, 0, "completed")


def test_output_budget_and_offsets_are_bounded_per_stream_and_combined():
    control = ready()
    token = submitted(control)
    job = control.take_exec()
    for index in range(64):
        control.append_output(job, "stdout", index * 1024, b"x" * 1024)
    with pytest.raises(MonitorExecControlError):
        control.append_output(job, "stderr", 0, b"x")
    for offset in (-1, 65537, True):
        with pytest.raises(MonitorExecControlError):
            control.poll(1, token, offset, 0)
    value = control.poll(1, token, 65535, 0)
    assert value["stdout_hex"] == "78" and value["stdout_size"] == 65536
    with pytest.raises(MonitorExecControlError):
        control.complete(job, {"exit_code": 0, "signal": None}, 65535, 0, "completed")
    control.complete(job, {"exit_code": None, "signal": 9}, 65536, 0, "output-limit")


def test_ack_cannot_discard_active_job_or_change_logical_request():
    control = ready()
    token = submitted(control)
    with pytest.raises(MonitorExecControlError, match="not-completed"):
        control.acknowledge(1, token)
    with pytest.raises(MonitorExecControlError, match="busy"):
        control.submit(1, token, ["/bin/other"], 30000)
    with pytest.raises(MonitorExecControlError, match="busy"):
        control.submit(1, token, ["/bin/probe"], 10)


@pytest.mark.parametrize("reason", ["completed", "timeout", "output-limit", "cancelled"])
def test_pre_fork_cancellation_has_no_fabricated_process_terminal(reason):
    control = ready()
    token = submitted(control)
    job = control.take_exec()
    if reason == "cancelled":
        control.complete(job, None, 0, 0, reason)
        assert control.poll(1, token)["terminal"] is None
        assert control.poll(1, token)["state"] == "completed"
        control.acknowledge(1, token)
    else:
        with pytest.raises(MonitorExecControlError):
            control.complete(job, None, 0, 0, reason)


def test_null_cancellation_cannot_discard_already_observed_guest_output():
    control = ready()
    submitted(control)
    job = control.take_exec()
    control.append_output(job, "stdout", 0, b"x")
    with pytest.raises(MonitorExecControlError):
        control.complete(job, None, 1, 0, "cancelled")
