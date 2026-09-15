"""Public exec status observes one pinned mailbox without adopting its work."""

from __future__ import annotations

import json
import multiprocessing
import os
import time
import uuid
from dataclasses import replace

import pytest
import test_runtime_dispatch as dispatch_fixtures
from test_oci_monitor_recovery import case as _recovery_case

from palimpsest_local import cli
from palimpsest_local import oci_exec_status as status
from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local import state as state_module
from palimpsest_local.oci_exec_control import MonitorExecControl, MonitorExecControlError

case = _recovery_case


@pytest.fixture(autouse=True)
def linux_status_platform(monkeypatch):
    monkeypatch.setattr(status.sys, "platform", "linux")


def _durable_bytes(case):
    return {
        path.relative_to(case.roots.state).as_posix(): path.read_bytes()
        for path in case.roots.state.rglob("*")
        if path.is_file()
    }


def _hold_run_lock(roots, name, ready, release):
    with state_module.locked_existing_run(roots, name):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("test did not release held run lock")


def _install_status_boundary(case, monkeypatch, response):
    calls = []
    descriptors = []

    def exchange(descriptor, endpoint, operation, payload, *, timeout):
        assert endpoint == case.snapshot.endpoint
        assert os.fstat(descriptor).st_ino == case.directory.stat().st_ino
        assert 0.1 <= timeout <= 5
        calls.append((operation, dict(payload)))
        descriptors.append(descriptor)
        return response() if callable(response) else response

    monkeypatch.setattr(ipc, "request_monitor_exec", exchange)
    return calls, descriptors


@pytest.mark.parametrize("state_name", ["not-ready", "ready", "stopping", "terminal", "control-lost"])
@pytest.mark.parametrize("occupied", [False, True])
def test_exact_schema_all_states_are_advisory_secret_free_and_read_only(case, monkeypatch, state_name, occupied):
    raw = {"state": state_name, "next_sequence": 1, "occupied": occupied}
    calls, descriptors = _install_status_boundary(case, monkeypatch, raw)
    before = _durable_bytes(case)

    report = status.exec_status(case.roots, case.binding.record.name)

    assert set(report) == {"schema", "state", "occupied", "guidance"}
    assert report["schema"] == status.OCI_EXEC_STATUS_SCHEMA
    assert report["state"] == state_name and report["occupied"] is occupied
    assert type(report["guidance"]) is str and report["guidance"]
    rendered = json.dumps(report)
    for secret in (case.binding.record.run_id, case.binding.boot_attempt_id, "next_sequence", "token", "argv"):
        assert secret not in rendered
    if state_name == "ready" and not occupied:
        assert "point-in-time" in report["guidance"] and "not a guarantee" in report["guidance"]
    if occupied:
        assert "original client may finish" in report["guidance"]
        assert "cannot be distinguished" in report["guidance"]
        assert "takeover is unsupported" in report["guidance"]
        assert "does not prove abandonment" in report["guidance"]
        assert "unknown" in report["guidance"]
    assert calls == [("status", {})]
    assert _durable_bytes(case) == before
    assert descriptors
    with pytest.raises(OSError):
        os.fstat(descriptors[-1])


@pytest.mark.parametrize("phase", ["queued", "running", "completed"])
def test_real_mailbox_occupied_observation_never_takes_result_or_changes_ack(case, monkeypatch, phase):
    control = MonitorExecControl()
    control.mark_ready()
    token = str(uuid.uuid4())
    control.submit(1, token, ["/bin/probe", "literal $SECRET"], 100)
    if phase != "queued":
        job = control.take_exec()
        assert job is not None
        if phase == "completed":
            control.append_output(job, "stdout", 0, b"private result")
            control.complete(job, {"exit_code": 17, "signal": None}, 14, 0, "completed")
    before_result = control.poll(1, token, 0, 0)
    before_durable = _durable_bytes(case)
    calls, descriptors = _install_status_boundary(case, monkeypatch, control.status)

    first = status.exec_status(case.roots, case.binding.record.name)
    second = status.exec_status(case.roots, case.binding.record.name)

    assert first == second
    assert first["state"] == "ready" and first["occupied"] is True
    assert control.poll(1, token, 0, 0) == before_result
    assert calls == [("status", {}), ("status", {})]
    assert _durable_bytes(case) == before_durable
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    if phase == "completed":
        assert control.acknowledge(1, token) == {"state": "ready", "next_sequence": 2, "occupied": False}
    else:
        with pytest.raises(MonitorExecControlError, match="not-completed"):
            control.acknowledge(1, token)


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        {},
        {"state": "ready", "next_sequence": 1, "occupied": False, "extra": True},
        *({"state": value, "next_sequence": 1, "occupied": False} for value in (None, True, 1, [], "unknown")),
        *(
            {"state": "ready", "next_sequence": value, "occupied": False}
            for value in (None, True, False, 0, -1, 2**63, "1")
        ),
        *({"state": "ready", "next_sequence": 1, "occupied": value} for value in (None, 0, 1, "false", [])),
    ],
)
def test_malformed_status_is_typed_path_free_and_never_succeeds(case, monkeypatch, malformed):
    calls, descriptors = _install_status_boundary(case, monkeypatch, malformed)
    before = _durable_bytes(case)
    with pytest.raises(status.OCIExecStatusError, match="response is invalid") as error:
        status.exec_status(case.roots, case.binding.record.name)
    assert str(case.roots.state) not in str(error.value)
    assert calls == [("status", {})] and _durable_bytes(case) == before
    with pytest.raises(OSError):
        os.fstat(descriptors[-1])


def test_durable_control_lost_is_unavailable_without_fabricated_status(case, monkeypatch):
    snapshot = replace(case.snapshot, phase="control-lost", revision=case.snapshot.revision + 1, active_binding=None)
    case.journal.write_bytes(ipc._canonical_bytes(snapshot.to_dict()) + b"\n")
    calls, _descriptors = _install_status_boundary(case, monkeypatch, pytest.fail)
    before = _durable_bytes(case)
    with pytest.raises(status.OCIExecStatusError, match="unavailable") as error:
        status.exec_status(case.roots, case.binding.record.name)
    assert calls == [] and _durable_bytes(case) == before
    assert str(case.roots.state) not in str(error.value)


def test_replaced_exact_run_is_refused_before_ipc(case, monkeypatch):
    owner = json.loads(case.owner.read_bytes())
    state_payload = json.loads(case.state.read_bytes())
    replacement = str(uuid.uuid4())
    owner["run_id"] = state_payload["run_id"] = replacement
    case.owner.write_text(json.dumps(owner, sort_keys=True) + "\n")
    case.state.write_text(json.dumps(state_payload, sort_keys=True) + "\n")
    calls, _descriptors = _install_status_boundary(case, monkeypatch, pytest.fail)
    before = _durable_bytes(case)
    with pytest.raises(status.OCIExecStatusError, match="unavailable"):
        status.exec_status(case.roots, case.binding.record.name)
    assert calls == [] and _durable_bytes(case) == before


def test_run_lock_contention_is_bounded_without_ipc_or_evidence_changes(case, monkeypatch):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_run_lock,
        args=(case.roots, case.binding.record.name, ready, release),
    )
    holder.start()
    closed = []
    original_close = state_module._close_noerror

    def close_and_record(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    try:
        assert ready.wait(5), "separate process did not acquire the exact run lock"
        monkeypatch.setattr(status, "_RUN_LOCK_TIMEOUT", 0.1)
        monkeypatch.setattr(state_module, "_close_noerror", close_and_record)
        monkeypatch.setattr(ipc, "request_monitor_exec", lambda *a, **k: pytest.fail("IPC entered"))
        before = _durable_bytes(case)
        started = time.monotonic()

        with pytest.raises(status.OCIExecStatusError, match="unavailable"):
            status.exec_status(case.roots, case.binding.record.name)

        assert time.monotonic() - started < 1
        assert _durable_bytes(case) == before
        assert closed
        for descriptor in closed:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
    assert holder.exitcode == 0


def test_foreign_journal_binding_is_refused_by_canonical_validator(case, monkeypatch):
    raw = json.loads(case.journal.read_bytes())
    raw_binding = dict(raw["binding"])
    raw_binding["name"] = "foreign"
    raw_binding["run_id"] = str(uuid.uuid4())
    foreign = ipc.MonitorPreActivationBinding.from_dict(raw_binding)
    raw["binding"] = foreign.to_dict()
    raw["binding_digest"] = foreign.digest
    case.journal.write_bytes(ipc._canonical_bytes(raw) + b"\n")
    calls, _descriptors = _install_status_boundary(case, monkeypatch, pytest.fail)
    before = _durable_bytes(case)
    with pytest.raises(status.OCIExecStatusError, match="unavailable"):
        status.exec_status(case.roots, case.binding.record.name)
    assert calls == [] and _durable_bytes(case) == before


def test_cloud_and_missing_runs_refuse_without_state_creation(tmp_path, monkeypatch):
    roots = dispatch_fixtures._roots(tmp_path)
    dispatch_fixtures._write_ledger(
        roots,
        name="cloud",
        record={"schema_version": 2, "runtime_kind": "cloud-image", "backend": "kvm", "status": "running"},
    )
    before = _durable_bytes(type("Case", (), {"roots": roots})())
    monkeypatch.setattr(ipc, "request_monitor_exec", lambda *a, **k: pytest.fail("IPC entered"))
    for name in ("cloud", "missing"):
        with pytest.raises(status.OCIExecStatusError, match="unavailable"):
            status.exec_status(roots, name)
    assert _durable_bytes(type("Case", (), {"roots": roots})()) == before


def test_unavailable_monitor_error_is_typed_and_does_not_reflect_raw_exception(case, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError(f"secret path {case.roots.state} token=private")

    monkeypatch.setattr(ipc, "request_monitor_exec", unavailable)
    with pytest.raises(status.OCIExecStatusError, match="unavailable") as error:
        status.exec_status(case.roots, case.binding.record.name)
    assert "secret" not in str(error.value) and str(case.roots.state) not in str(error.value)


def test_cli_parse_dispatch_uses_existing_roots_and_prints_only_schema(case, monkeypatch, capsys):
    parsed = cli.build_parser().parse_args(["oci", "exec-status", case.binding.record.name])
    assert parsed.operation == "oci" and parsed.oci_operation == "exec-status"
    assert parsed.name == case.binding.record.name
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(case.roots.state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(case.roots.config.parent))
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("exec-status initialized roots"))
    calls, descriptors = _install_status_boundary(
        case, monkeypatch, {"state": "ready", "next_sequence": 1, "occupied": False}
    )

    assert cli.main(["oci", "exec-status", case.binding.record.name]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "schema": status.OCI_EXEC_STATUS_SCHEMA,
        "state": "ready",
        "occupied": False,
        "guidance": "Ready and unoccupied is a point-in-time observation, not a guarantee the next exec will succeed.",
    }
    assert calls == [("status", {})]
    with pytest.raises(OSError):
        os.fstat(descriptors[-1])


def test_cli_malformed_status_has_no_success_json_or_raw_error(case, monkeypatch, capsys):
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(case.roots.state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(case.roots.config.parent))
    _install_status_boundary(
        case,
        monkeypatch,
        {"state": "ready", "next_sequence": 1, "occupied": False, "path": str(case.roots.state)},
    )

    assert cli.main(["oci", "exec-status", case.binding.record.name]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "response is invalid" in captured.err
    assert str(case.roots.state) not in captured.err


def test_unsupported_platform_fails_before_roots_or_ipc(case, monkeypatch):
    monkeypatch.setattr(status.sys, "platform", "darwin")
    monkeypatch.setattr(status, "read_run_dispatch_record", lambda *a, **k: pytest.fail("state entered"))
    monkeypatch.setattr(ipc, "request_monitor_exec", lambda *a, **k: pytest.fail("IPC entered"))
    with pytest.raises(status.OCIExecStatusError, match="POSIX Linux"):
        status.exec_status(case.roots, case.binding.record.name)
