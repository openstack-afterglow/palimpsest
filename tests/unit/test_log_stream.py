"""Typed retained-console stream contracts."""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import threading
import uuid
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from palimpsest_local import cli, log_stream, runtime_dispatch, state, vz_native_runtime
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import (
    DispatchKey,
    ExistingRunRecord,
    ExpectedRunIdentity,
    LogCursor,
    LogDataEvent,
    LogErrorCategory,
    LogMode,
    LogSourceStream,
    LogStream,
    LogStreamError,
    LogTerminalCategory,
    LogTerminalEvent,
    LogTerminalOutcome,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
)


def _roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})


def _run(
    roots: state.StatePaths,
    *,
    name: str = "demo",
    status: str = "running",
    backend: str = "kvm",
    runtime_kind: str = "cloud-image",
    content: bytes = b"",
) -> tuple[state.RunPaths, str]:
    paths = state.run_paths(roots, name)
    paths.root.mkdir(mode=0o700)
    run_id = str(uuid.uuid4())
    state.atomic_write_json(paths.owner, {"schema_version": 1, "run_id": run_id, "name": name})
    state.atomic_write_json(
        paths.state,
        {
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": name,
            "run_id": run_id,
            "status": status,
        },
    )
    paths.console.write_bytes(content)
    paths.console.chmod(0o600)
    return paths, run_id


def test_log_contract_invariants_and_immutability() -> None:
    record = ExistingRunRecord(
        "demo",
        str(uuid.uuid4()),
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    cursor = LogCursor(record, str(uuid.uuid4()), 1)
    event = LogDataEvent(cursor, LogSourceStream.VM_CONSOLE, 1, datetime.now(UTC), b"\x00\xff")
    assert event.data == b"\x00\xff"
    with pytest.raises(FrozenInstanceError):
        event.data = b"changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LogCursor(record, str(uuid.uuid4()), 0)
    with pytest.raises(ValueError):
        LogDataEvent(cursor, LogSourceStream.VM_CONSOLE, 0, datetime.now(UTC), b"x")
    with pytest.raises(ValueError):
        LogDataEvent(cursor, LogSourceStream.VM_CONSOLE, 1, datetime.now(UTC), b"")
    with pytest.raises(ValueError):
        LogDataEvent(cursor, LogSourceStream.VM_CONSOLE, 1, datetime.now(UTC), b"x" * (64 * 1024 + 1))
    with pytest.raises(ValueError):
        LogDataEvent(cursor, LogSourceStream.VM_CONSOLE, 1, datetime.now(timezone(timedelta(hours=1))), b"x")
    with pytest.raises(ValueError):
        LogTerminalOutcome(LogTerminalCategory.ERROR)
    with pytest.raises(ValueError):
        LogTerminalEvent(
            cursor,
            datetime.now(UTC),
            LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="exited"),
        )


def test_snapshot_preserves_exact_bytes_and_has_one_terminal(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    raw = b"\x00\x1b[31mred\xff\r\n" + "한".encode() + b"\n"
    _run(roots, content=raw)
    stream = runtime_dispatch.logs("demo", roots=roots)
    assert isinstance(stream, LogStream)
    assert stream.mode is LogMode.SNAPSHOT
    events = list(stream.events())
    assert b"".join(item.data for item in events if isinstance(item, LogDataEvent)) == raw
    assert [item.cursor.position for item in events] == list(range(1, len(events) + 1))
    data_events = [item for item in events if isinstance(item, LogDataEvent)]
    assert [item.stream_sequence for item in data_events] == list(range(1, len(data_events) + 1))
    assert all(item.observed_at.utcoffset() == timedelta(0) for item in events)
    assert all(left.observed_at <= right.observed_at for left, right in zip(events, events[1:], strict=False))
    assert isinstance(events[-1], LogTerminalEvent)
    assert events[-1].outcome.category is LogTerminalCategory.SNAPSHOT_COMPLETE
    with pytest.raises(LogStreamError) as caught:
        stream.events()
    assert caught.value.category is LogErrorCategory.ALREADY_CONSUMED

    unopened = runtime_dispatch.logs("demo", roots=roots)
    iterator = unopened.events()
    unopened.close()
    closed_event = next(iterator)
    assert isinstance(closed_event, LogTerminalEvent)
    assert closed_event.outcome.category is LogTerminalCategory.CANCELLED


def test_empty_snapshot_terminal_starts_at_one_and_snapshot_boundary_is_fixed(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, content=b"")
    empty = runtime_dispatch.logs("demo", roots=roots)
    event = list(empty.events())[0]
    assert isinstance(event, LogTerminalEvent)
    assert event.cursor.position == 1

    paths.console.write_bytes(b"initial")
    paths.console.chmod(0o600)
    stream = runtime_dispatch.logs("demo", roots=roots)
    with paths.console.open("ab") as output:
        output.write(b"-later")
    events = list(stream.events())
    assert b"".join(item.data for item in events if isinstance(item, LogDataEvent)) == b"initial"


def test_follow_append_terminal_and_cancel(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, content=b"first")
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    first = next(events)
    assert isinstance(first, LogDataEvent) and first.data == b"first"
    with paths.console.open("ab") as output:
        output.write(b"second")
    second = next(events)
    assert isinstance(second, LogDataEvent) and second.data == b"second"
    payload = json.loads(paths.state.read_text())
    state.atomic_write_json(paths.state, {**payload, "status": "stopped"})
    terminal = next(events)
    assert isinstance(terminal, LogTerminalEvent)
    assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="stopped")
    with pytest.raises(StopIteration):
        next(events)

    cancelled = runtime_dispatch.logs("demo", roots=roots, follow=True)
    cancelled.cancel()
    cancel_event = list(cancelled.events())[0]
    assert isinstance(cancel_event, LogTerminalEvent)
    assert cancel_event.outcome.category is LogTerminalCategory.CANCELLED
    cancelled.close()
    cancelled.close()


def test_follow_drains_append_racing_with_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, status="stopped", content=b"first")
    original = state.PinnedRunConsole.current_status
    appended = False

    def append_before_status(source: state.PinnedRunConsole) -> str:
        nonlocal appended
        if not appended:
            with paths.console.open("ab") as output:
                output.write(b"-last")
            appended = True
        return original(source)

    monkeypatch.setattr(state.PinnedRunConsole, "current_status", append_before_status)
    events = list(runtime_dispatch.logs("demo", roots=roots, follow=True).events())
    assert b"".join(item.data for item in events if isinstance(item, LogDataEvent)) == b"first-last"
    assert isinstance(events[-1], LogTerminalEvent)
    assert events[-1].outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="stopped")


def test_follow_wait_is_released_promptly_by_concurrent_cancel(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _run(roots)
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    iterator = stream.events()
    started = threading.Event()
    completed = threading.Event()
    observed: list[LogTerminalEvent] = []

    def consume() -> None:
        started.set()
        event = next(iterator)
        assert isinstance(event, LogTerminalEvent)
        observed.append(event)
        completed.set()

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    assert started.wait(1)
    stream.cancel()
    assert completed.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert observed[0].outcome.category is LogTerminalCategory.CANCELLED


def test_follow_reports_removed_when_exact_run_tree_disappears(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, content=b"retained")
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    shutil.rmtree(paths.root)
    terminal = next(events)
    assert isinstance(terminal, LogTerminalEvent)
    assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="removed")


@pytest.mark.parametrize("change", ["rotate", "truncate", "identity", "runs-mode", "run-mode"])
def test_follow_rejects_console_or_run_replacement(tmp_path: Path, change: str) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, status="stopped", content=b"first")
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    if change == "rotate":
        paths.console.rename(paths.console.with_suffix(".old"))
        paths.console.write_bytes(b"secret replacement")
        paths.console.chmod(0o600)
    elif change == "truncate":
        paths.console.write_bytes(b"")
    elif change == "identity":
        payload = json.loads(paths.state.read_text())
        state.atomic_write_json(paths.state, {**payload, "run_id": str(uuid.uuid4())})
    elif change == "runs-mode":
        roots.runs.chmod(0o755)
    else:
        paths.root.chmod(0o755)
    terminal = next(events)
    assert isinstance(terminal, LogTerminalEvent)
    assert terminal.outcome.category is LogTerminalCategory.ERROR
    expected = LogErrorCategory.CONSOLE_CHANGED if change in {"rotate", "truncate"} else LogErrorCategory.RUN_CHANGED
    assert terminal.outcome.error_category is expected


@pytest.mark.parametrize("kind", ["mode", "hardlink", "symlink", "fifo"])
@pytest.mark.parametrize("native", [False, True])
def test_console_authority_rejects_unsafe_entries(tmp_path: Path, kind: str, native: bool) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(
        roots,
        content=b"x",
        status="stopped",
        backend="lima-vz" if native else "kvm",
        runtime_kind="oci-root" if native else "cloud-image",
    )
    if kind == "mode":
        paths.console.chmod(0o644)
    elif kind == "hardlink":
        os.link(paths.console, paths.console.with_suffix(".link"))
    elif kind == "symlink":
        target = paths.console.with_suffix(".target")
        paths.console.rename(target)
        paths.console.symlink_to(target)
    else:
        paths.console.unlink()
        os.mkfifo(paths.console, 0o600)
    with pytest.raises(LogStreamError) as caught:
        runtime_dispatch.logs("demo", roots=roots)
    assert caught.value.category is LogErrorCategory.INVALID_CONSOLE
    assert str(paths.root) not in str(caught.value)


def test_oci_logs_fail_before_console_validation(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, runtime_kind="oci-root", content=b"secret")
    paths.console.chmod(0o777)
    with pytest.raises(RuntimeCapabilityError):
        runtime_dispatch.logs("demo", roots=roots)


def test_expected_identity_mismatch_fails_before_console_validation(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, content=b"secret")
    paths.console.chmod(0o777)
    expected = ExpectedRunIdentity(
        "demo",
        str(uuid.uuid4()),
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    with pytest.raises(StateError, match="run identity changed before lifecycle operation"):
        runtime_dispatch.logs("demo", roots=roots, expected_identity=expected)


class _BinaryOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, _content: str) -> int:
        return len(_content)

    def flush(self) -> None:
        return None


def test_cli_writes_exact_log_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path)
    _run(roots, content=b"\x00\xff\x1b[31m\r\n")
    output = _BinaryOutput()
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setattr(cli.sys, "stdout", output)
    assert cli.main(["logs", "demo"]) == 0
    assert output.buffer.getvalue() == b"\x00\xff\x1b[31m\r\n"


def test_compose_frames_lf_across_byte_and_multibyte_chunk_boundaries() -> None:
    record = ExistingRunRecord(
        "demo",
        str(uuid.uuid4()),
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    generation = str(uuid.uuid4())
    now = datetime.now(UTC)
    output = io.StringIO()
    pending: dict[str, cli._ComposeLogRenderState] = {}
    events = (
        LogDataEvent(LogCursor(record, generation, 1), LogSourceStream.VM_CONSOLE, 1, now, b"a\r"),
        LogDataEvent(LogCursor(record, generation, 2), LogSourceStream.VM_CONSOLE, 2, now, b"\nutf:\xe2"),
        LogDataEvent(LogCursor(record, generation, 3), LogSourceStream.VM_CONSOLE, 3, now, b"\x82\xac"),
        LogTerminalEvent(
            LogCursor(record, generation, 4),
            now,
            LogTerminalOutcome(LogTerminalCategory.SNAPSHOT_COMPLETE),
        ),
    )
    for event in events:
        cli._render_compose_log_event("api", event, pending, output)
    assert output.getvalue() == "api | a\r\napi | utf:€\n"


def test_compose_streams_a_long_logical_line_without_utf8_damage_or_synthetic_lf() -> None:
    record = ExistingRunRecord(
        "demo",
        str(uuid.uuid4()),
        2,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    generation = str(uuid.uuid4())
    now = datetime.now(UTC)
    output = io.StringIO()
    pending: dict[str, cli._ComposeLogRenderState] = {}
    events = (
        LogDataEvent(
            LogCursor(record, generation, 1),
            LogSourceStream.VM_CONSOLE,
            1,
            now,
            b"a" * (64 * 1024 - 1) + b"\xe2",
        ),
        LogDataEvent(LogCursor(record, generation, 2), LogSourceStream.VM_CONSOLE, 2, now, b"\x82\xacend"),
        LogTerminalEvent(
            LogCursor(record, generation, 3),
            now,
            LogTerminalOutcome(LogTerminalCategory.SNAPSHOT_COMPLETE),
        ),
    )
    for event in events:
        cli._render_compose_log_event("api", event, pending, output)
    assert output.getvalue() == f"api | {'a' * (64 * 1024 - 1)}€end\n"
    assert pending["api"].decoder.getstate()[0] == b""
    assert not pending["api"].line_started


def _assert_log_run_lock(roots):
    probe = os.open(roots.locks / "demo.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)


@pytest.mark.parametrize("status", ["stopped", "removed"])
def test_native_cli_retained_logs_require_no_provider_or_guest(tmp_path, monkeypatch, status):
    roots = _roots(tmp_path)
    _run(roots, backend="lima-vz", runtime_kind="oci-root", status=status, content=b"\x00retained\xff\n")
    monkeypatch.setattr(
        vz_native_runtime, "refresh_run", lambda *_args, **_kwargs: pytest.fail("retained logs reached provider")
    )
    monkeypatch.setattr(
        vz_native_runtime, "_config_for", lambda *_args, **_kwargs: pytest.fail("retained logs required host custody")
    )
    monkeypatch.setattr(
        runtime_dispatch, "preflight_existing_record", lambda *_args, **_kwargs: pytest.fail("logs ran preflight")
    )
    output = _BinaryOutput()
    monkeypatch.setattr(cli, "resolve_roots", lambda: roots)
    monkeypatch.setattr(cli.sys, "stdout", output)
    assert cli.main(["logs", "demo"]) == 0
    assert output.buffer.getvalue() == b"\x00retained\xff\n"


@pytest.mark.parametrize("boundary", ["resolve", "refresh", "open", "follow"])
def test_native_cli_logs_cooperate_with_writer_and_release_before_yield(tmp_path, monkeypatch, boundary):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    payload = state.read_json(paths.state)
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)
    armed, settled = [], []

    def arm():
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        armed.append(True)

    def settle(_delay):
        assert armed == [True] and not settled
        state.atomic_write_json(paths.state, {**payload, "status": "removed", "lifecycle_revision": 1})
        settled.append(True)
        fcntl.flock(holder, fcntl.LOCK_UN)

    monkeypatch.setattr(state.time, "sleep", settle)
    if boundary == "resolve":
        arm()
    elif boundary == "refresh":
        original_refresh = vz_native_runtime.refresh_logs

        def refresh(*args, **kwargs):
            arm()
            return original_refresh(*args, **kwargs)

        monkeypatch.setattr(vz_native_runtime, "refresh_logs", refresh)
    elif boundary == "open":
        original_open = state.open_pinned_run_console

        def open_console(*args, **kwargs):
            arm()
            return original_open(*args, **kwargs)

        monkeypatch.setattr(state, "open_pinned_run_console", open_console)
    try:
        stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
        events = stream.events()
        first = next(events)
        assert isinstance(first, LogDataEvent) and first.data == b"retained"
        # A suspended consumer owns descriptors, not the writer's mutex.
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(holder, fcntl.LOCK_UN)
        if boundary == "follow":
            arm()
        terminal = next(events)
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="removed")
        assert armed == settled == [True]
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(holder, fcntl.LOCK_UN)
        events.close()
    finally:
        os.close(holder)


@pytest.mark.parametrize("change", ["identity", "state-mode", "console-hardlink", "run-replacement"])
def test_native_follow_refuses_identity_changed_by_cooperating_writer(tmp_path, monkeypatch, change):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"original")
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    settled = []

    def settle(_delay):
        assert not settled
        if change == "identity":
            payload = state.read_json(paths.state)
            state.atomic_write_json(paths.state, {**payload, "run_id": str(uuid.uuid4())})
        elif change == "state-mode":
            paths.state.chmod(0o644)
        elif change == "console-hardlink":
            os.link(paths.console, paths.root / "extra-console")
        else:
            paths.root.rename(paths.root.with_name("old-demo"))
            _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"foreign secret")
        settled.append(True)
        fcntl.flock(holder, fcntl.LOCK_UN)

    monkeypatch.setattr(state.time, "sleep", settle)
    try:
        terminal = next(events)
        assert settled == [True]
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome.category is LogTerminalCategory.ERROR
        assert terminal.outcome.error_category in {LogErrorCategory.RUN_CHANGED, LogErrorCategory.CONSOLE_CHANGED}
        assert list(events) == []
    finally:
        events.close()
        os.close(holder)


@pytest.mark.parametrize("follow", [False, True])
def test_native_log_initial_reads_share_one_contention_allowance(tmp_path, monkeypatch, follow):
    roots = _roots(tmp_path)
    _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)
    clock, stage_started, stages = [600.0], [600.0], []

    def arm(label):
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage_started[0] = clock[0]
        stages.append(label)

    def advance(delay):
        clock[0] += delay
        # Resolve and refresh each spend two seconds; open cannot get a new
        # five-second allowance after those four seconds were already spent.
        if stages[-1] != "open" and clock[0] - stage_started[0] >= 2.0:
            fcntl.flock(holder, fcntl.LOCK_UN)

    original_refresh, original_open = vz_native_runtime.refresh_logs, state.open_pinned_run_console

    def refresh(*args, **kwargs):
        arm("refresh")
        return original_refresh(*args, **kwargs)

    def open_console(*args, **kwargs):
        arm("open")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state.time, "sleep", advance)
    monkeypatch.setattr(vz_native_runtime, "refresh_logs", refresh)
    monkeypatch.setattr(state, "open_pinned_run_console", open_console)
    arm("resolve")
    try:
        with pytest.raises(LogStreamError) as caught:
            runtime_dispatch.logs("demo", roots=roots, follow=follow)
        assert caught.value.category is LogErrorCategory.READ_FAILED
        assert stages == ["resolve", "refresh", "open"]
        assert clock[0] - 600.0 == pytest.approx(5.0)
    finally:
        os.close(holder)


@pytest.mark.parametrize("busy_at", ["status", "refresh"])
def test_native_follow_busy_polls_keep_fd_and_drain_writer_stop(tmp_path, monkeypatch, busy_at):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", content=b"initial")
    clock, started, stages, provider_calls = [600.0], [600.0], [], []
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)

    def initial_provider(*args, **kwargs):
        provider_calls.append(True)
        # Ordinary provider time must not consume mutex-wait allowance.
        clock[0] += 600.0
        return state.read_json(paths.state)

    monkeypatch.setattr(vz_native_runtime, "refresh_run", initial_provider)
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    first = next(events)
    assert isinstance(first, LogDataEvent) and first.data == b"initial"
    source = stream._source
    console_identity = os.fstat(source.console_fd).st_ino
    original_refresh = vz_native_runtime.refresh_logs
    original_status = state.PinnedRunConsole.current_status
    budgets, waits, status_waits = [], [], []

    def arm(label):
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started[0] = clock[0]
        stages.append(label)

    def advance(delay):
        clock[0] += delay
        if busy_at == "refresh" and stages == ["status"] and clock[0] - started[0] >= 4.0 - 1e-9:
            fcntl.flock(holder, fcntl.LOCK_UN)

    def status(source, *, observation_budget=None):
        budgets.append(observation_budget)
        before = clock[0]
        try:
            return original_status(source, observation_budget=observation_budget)
        finally:
            status_waits.append(clock[0] - before)

    def refresh(*args, **kwargs):
        assert kwargs["_observation_budget"] is budgets[-1]
        arm("refresh")
        return original_refresh(*args, **kwargs)

    def after_busy_poll(delay):
        assert delay == log_stream._FOLLOW_POLL_SECONDS
        assert not source.closed
        assert os.fstat(source.console_fd).st_ino == console_identity
        assert provider_calls == [True]
        waits.append(clock[0])
        if len(waits) == 1:
            # The first poll exhausted one shared allowance; the next poll
            # receives its own allowance, not a reset inside this poll.
            assert clock[0] - poll_started == pytest.approx(5.0)
            assert budgets[-1].remaining_seconds == pytest.approx(0.0)
        else:
            assert len(waits) == 2
            assert waits[-1] - waits[-2] == pytest.approx(5.0)
            with paths.console.open("ab") as output:
                output.write(b"-last")
            payload = state.read_json(paths.state)
            state.atomic_write_json(paths.state, {**payload, "status": "stopped"})
            fcntl.flock(holder, fcntl.LOCK_UN)
        return False

    monkeypatch.setattr(state.time, "sleep", advance)
    monkeypatch.setattr(state.PinnedRunConsole, "current_status", status)
    monkeypatch.setattr(vz_native_runtime, "refresh_logs", refresh)
    monkeypatch.setattr(stream._cancelled, "wait", after_busy_poll)
    clock[0] += 1.0
    poll_started = clock[0]
    arm("status")
    try:
        final_data = next(events)
        assert isinstance(final_data, LogDataEvent) and final_data.data == b"-last"
        # The suspended consumer holds no writer mutex, even after busy polls.
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(holder, fcntl.LOCK_UN)
        terminal = next(events)
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="stopped")
        assert len(waits) == 2
        assert stages == (["status", "refresh"] if busy_at == "refresh" else ["status"])
        assert status_waits == pytest.approx([4.0 if busy_at == "refresh" else 5.0, 5.0, 0.0])
        assert len(budgets) == 3
        assert all(left is not right for left, right in zip(budgets, budgets[1:], strict=False))
        assert provider_calls == [True]
        assert list(events) == []
    finally:
        events.close()
        os.close(holder)


def _native_log_namespace_fixture(tmp_path, monkeypatch):
    from test_inventory import _native_custody_fixture

    native, config, custody = _native_custody_fixture(tmp_path, monkeypatch)
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"initial")
    _run(roots, name="other", backend="lima-vz", runtime_kind="oci-root")
    provider_calls = []

    def forbidden_provider(*_args, **_kwargs):
        provider_calls.append(True)
        pytest.fail("namespace acquisition admitted a provider request")

    monkeypatch.setattr(native, "_config_for", lambda _authority: config)
    monkeypatch.setattr(native, "_load", forbidden_provider)
    monkeypatch.setattr(native, "_owned_instance", forbidden_provider)
    monkeypatch.setattr(native, "_control", forbidden_provider)
    return native, config, custody, roots, paths, provider_calls


def _hold_other_native_consumer(native, config, roots):
    stack = ExitStack()
    try:
        stack.enter_context(state.locked_existing_run(roots, "other"))
        stack.enter_context(native._custody(config))
    except BaseException:
        stack.close()
        raise
    return stack


@pytest.mark.parametrize("named_wait", [0.0, 4.0])
def test_native_follow_survives_other_consumer_namespace_busy_and_drains_final_bytes(tmp_path, monkeypatch, named_wait):
    native, config, custody, roots, paths, provider_calls = _native_log_namespace_fixture(tmp_path, monkeypatch)
    clock = [600.0]
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert next(events).data == b"initial"
    source = stream._source
    original_fd = source.console_fd
    original_inode = os.fstat(original_fd).st_ino
    payload = state.read_json(paths.state)
    state.atomic_write_json(paths.state, {**payload, "status": "running"})
    holder = _hold_other_native_consumer(native, config, roots)
    named_holder = os.open(roots.locks / "demo.lock", os.O_RDWR)
    if named_wait:
        fcntl.flock(named_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    original_status, original_refresh = state.PinnedRunConsole.current_status, native.refresh_logs
    budgets, status_waits, refresh_waits, polls = [], [], [], []

    def advance(delay):
        clock[0] += delay
        if clock[0] - 600.0 >= named_wait - 1e-9:
            fcntl.flock(named_holder, fcntl.LOCK_UN)

    def status(source, *, observation_budget=None):
        budgets.append(observation_budget)
        before = clock[0]
        try:
            return original_status(source, observation_budget=observation_budget)
        finally:
            status_waits.append(clock[0] - before)

    def refresh(*args, **kwargs):
        assert kwargs["_observation_budget"] is budgets[-1]
        before = clock[0]
        try:
            return original_refresh(*args, **kwargs)
        finally:
            refresh_waits.append(clock[0] - before)

    def after_busy_poll(delay):
        assert delay == log_stream._FOLLOW_POLL_SECONDS
        assert not source.closed and source.console_fd == original_fd
        assert os.fstat(original_fd).st_ino == original_inode
        assert budgets[-1].remaining_seconds == pytest.approx(0)
        assert provider_calls == []
        # A is unlocked while B's real operation retains the shared namespace.
        fcntl.flock(named_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(named_holder, fcntl.LOCK_UN)
        with (custody / "lock").open("rb") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        polls.append(clock[0])
        assert clock[0] - 600.0 == pytest.approx(5.0 * len(polls))
        if len(polls) == 2:
            with paths.console.open("ab") as output:
                output.write(b"-last")
            state.atomic_write_json(paths.state, {**payload, "status": "stopped"})
            holder.close()
        # Make the next refresh due without adding fake sleep to the wait clock.
        stream._next_refresh = 0.0
        return False

    monkeypatch.setattr(state.time, "sleep", advance)
    monkeypatch.setattr(state.PinnedRunConsole, "current_status", status)
    monkeypatch.setattr(native, "refresh_logs", refresh)
    monkeypatch.setattr(stream._cancelled, "wait", after_busy_poll)
    stream._next_refresh = 0.0
    try:
        final_data = next(events)
        assert isinstance(final_data, LogDataEvent) and final_data.data == b"-last"
        # Yielding owns only the pinned descriptor, never either mutex.
        fcntl.flock(named_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(named_holder, fcntl.LOCK_UN)
        with native._custody(config):
            pass
        terminal = next(events)
        assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="stopped")
        assert status_waits == pytest.approx([named_wait, 0.0, 0.0])
        assert refresh_waits == pytest.approx([5.0 - named_wait, 5.0])
        assert len(budgets) == 3
        assert all(left is not right for left, right in zip(budgets, budgets[1:], strict=False))
        assert polls == pytest.approx([605.0, 610.0])
        assert provider_calls == [] and list(events) == []
    finally:
        holder.close()
        events.close()
        os.close(named_holder)


def test_native_follow_cancel_during_other_consumer_namespace_busy_is_bounded(tmp_path, monkeypatch):
    native, config, _custody, roots, paths, provider_calls = _native_log_namespace_fixture(tmp_path, monkeypatch)
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert next(events).data == b"initial"
    payload = state.read_json(paths.state)
    state.atomic_write_json(paths.state, {**payload, "status": "running"})
    holder = _hold_other_native_consumer(native, config, roots)
    clock = [600.0]
    waiting, cancelled, completed = threading.Event(), threading.Event(), threading.Event()
    observed, failures = [], []

    def advance(delay):
        waiting.set()
        assert cancelled.wait(1)
        clock[0] += delay

    def consume():
        try:
            observed.append(next(events))
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state.time, "sleep", advance)
    stream._next_refresh = 0.0
    worker = threading.Thread(target=consume, daemon=True)
    try:
        worker.start()
        assert waiting.wait(1)
        stream.cancel()
        cancelled.set()
        assert completed.wait(1)
        worker.join(1)
        assert not worker.is_alive() and failures == []
        assert clock[0] - 600.0 == pytest.approx(5.0)
        assert len(observed) == 1 and observed[0].outcome.category is LogTerminalCategory.CANCELLED
        assert provider_calls == []
    finally:
        stream.cancel()
        cancelled.set()
        worker.join(1)
        if not worker.is_alive():
            events.close()
        holder.close()


@pytest.mark.parametrize("damage", ["hold", "active-actor", "actor-journal", "unsafe-binding", "unknown-provider"])
def test_native_follow_namespace_failures_after_acquisition_are_terminal_without_retry(tmp_path, monkeypatch, damage):
    native, config, custody, roots, paths, provider_calls = _native_log_namespace_fixture(tmp_path, monkeypatch)
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert next(events).data == b"initial"
    payload = state.read_json(paths.state)
    state.atomic_write_json(paths.state, {**payload, "status": "running"})
    holder = _hold_other_native_consumer(native, config, roots)
    clock, waits = [600.0], []

    def release_with_damage(delay):
        clock[0] += delay
        waits.append(delay)
        if damage == "hold":
            (custody / "hold-11111111-1111-4111-8111-111111111111.json").write_bytes(b"{}")
        elif damage == "active-actor":
            (custody / "active.json").write_bytes(b"{}")
        elif damage == "actor-journal":
            (custody / "unsettled.actor.json").write_bytes(b"{}")
        elif damage == "unsafe-binding":
            (custody / "lock").chmod(0o640)
        holder.close()

    if damage == "unknown-provider":

        def load(authority, *_args, **_kwargs):
            provider_calls.append("load")
            return ({**authority.mutable_state(), "vz_native": {}},)

        def unknown_instance(*_args, **_kwargs):
            provider_calls.append("instance")
            return {"status": "Unknown"}

        monkeypatch.setattr(native, "_load", load)
        monkeypatch.setattr(native, "_owned_instance", unknown_instance)
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state.time, "sleep", release_with_damage)
    monkeypatch.setattr(stream._cancelled, "wait", lambda *_: pytest.fail("retried failed native custody"))
    stream._next_refresh = 0.0
    try:
        terminal = next(events)
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome.error_category is LogErrorCategory.READ_FAILED
        assert waits == [0.1]
        assert provider_calls == (["load", "instance"] if damage == "unknown-provider" else [])
        assert list(events) == []
    finally:
        holder.close()
        events.close()


@pytest.mark.parametrize("follow", [False, True])
@pytest.mark.parametrize("named_wait", [0.0, 4.0, 5.0])
def test_native_log_initial_namespace_busy_refuses_admission_with_shared_budget(
    tmp_path, monkeypatch, follow, named_wait
):
    native, config, _custody, roots, paths, provider_calls = _native_log_namespace_fixture(tmp_path, monkeypatch)
    payload = state.read_json(paths.state)
    state.atomic_write_json(paths.state, {**payload, "status": "running"})
    holder = _hold_other_native_consumer(native, config, roots)
    named_holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)
    if named_wait:
        fcntl.flock(named_holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = [600.0]

    def advance(delay):
        clock[0] += delay
        if clock[0] - 600.0 >= named_wait - 1e-9:
            fcntl.flock(named_holder, fcntl.LOCK_UN)

    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state.time, "sleep", advance)
    monkeypatch.setattr(
        state, "open_pinned_run_console", lambda *_args, **_kwargs: pytest.fail("busy initial refresh admitted logs")
    )
    try:
        with pytest.raises(LogStreamError) as caught:
            runtime_dispatch.logs("demo", roots=roots, follow=follow)
        assert caught.value.category is LogErrorCategory.READ_FAILED
        assert clock[0] - 600.0 == pytest.approx(5.0)
        assert provider_calls == []
    finally:
        holder.close()
        os.close(named_holder)


@pytest.mark.parametrize("busy_at", ["status", "refresh"])
def test_native_follow_cancel_during_long_mutex_hold_is_bounded(tmp_path, monkeypatch, busy_at):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", content=b"initial")
    provider_calls = []

    def provider(*args, **kwargs):
        provider_calls.append(True)
        assert provider_calls == [True]
        return state.read_json(paths.state)

    monkeypatch.setattr(vz_native_runtime, "refresh_run", provider)
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR)
    clock = [600.0]
    waiting, cancelled, completed = threading.Event(), threading.Event(), threading.Event()
    observed, failures = [], []
    original_refresh = vz_native_runtime.refresh_logs

    def advance(delay):
        waiting.set()
        assert cancelled.wait(1)
        clock[0] += delay

    def refresh(*args, **kwargs):
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_refresh(*args, **kwargs)

    def consume():
        try:
            observed.append(next(events))
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state.time, "sleep", advance)
    stream._next_refresh = 600.0
    if busy_at == "status":
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        monkeypatch.setattr(vz_native_runtime, "refresh_logs", refresh)
    worker = threading.Thread(target=consume, daemon=True)
    try:
        worker.start()
        assert waiting.wait(1)
        stream.cancel()
        cancelled.set()
        assert completed.wait(1)
        worker.join(1)
        assert not worker.is_alive()
        assert failures == []
        assert clock[0] - 600.0 == pytest.approx(5.0)
        assert len(observed) == 1 and isinstance(observed[0], LogTerminalEvent)
        assert observed[0].outcome.category is LogTerminalCategory.CANCELLED
        assert provider_calls == [True]
    finally:
        stream.cancel()
        cancelled.set()
        worker.join(1)
        if not worker.is_alive():
            events.close()
        os.close(holder)


@pytest.mark.parametrize(
    "category",
    [
        LogErrorCategory.READ_FAILED,
        LogErrorCategory.RUN_CHANGED,
        LogErrorCategory.INVALID_CONSOLE,
        LogErrorCategory.CONSOLE_CHANGED,
    ],
)
def test_native_follow_status_errors_are_terminal_without_retry(tmp_path, monkeypatch, category):
    roots = _roots(tmp_path)
    _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"initial")
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    attempts = []

    def fail_status(*args, **kwargs):
        attempts.append(True)
        raise LogStreamError(category)

    monkeypatch.setattr(state.PinnedRunConsole, "current_status", fail_status)
    monkeypatch.setattr(stream._cancelled, "wait", lambda *_: pytest.fail("retried failed status"))
    try:
        terminal = next(events)
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.ERROR, error_category=category)
        assert attempts == [True]
        assert list(events) == []
    finally:
        events.close()


@pytest.mark.parametrize("failure_type", [StateError, OSError, vz_native_runtime.VZNativeHold])
def test_native_follow_refresh_failure_is_terminal_without_retry(tmp_path, monkeypatch, failure_type):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", content=b"initial")
    monkeypatch.setattr(vz_native_runtime, "refresh_run", lambda *_args, **_kwargs: state.read_json(paths.state))
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    attempts = []

    def fail_refresh(_budget):
        attempts.append(True)
        raise failure_type("uncertain refresh")

    stream._refresh = fail_refresh
    stream._next_refresh = 0.0
    monkeypatch.setattr(stream._cancelled, "wait", lambda *_: pytest.fail("retried failed refresh"))
    try:
        terminal = next(events)
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome.error_category is LogErrorCategory.READ_FAILED
        assert attempts == [True]
        assert list(events) == []
    finally:
        events.close()


def test_budgeted_pinned_status_exposes_pure_busy_without_closing(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    record = state.read_run_dispatch_record(roots, "demo")
    source = state.open_pinned_run_console(roots, record)
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock = [600.0]
    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])

    def advance(delay):
        clock[0] += delay

    monkeypatch.setattr(state.time, "sleep", advance)
    try:
        budget = state.RunObservationWaitBudget()
        with pytest.raises(state.RunLockTimeoutError) as caught:
            source.current_status(observation_budget=budget)
        assert type(caught.value) is state.RunLockTimeoutError
        assert not source.closed
        assert clock[0] - 600.0 == pytest.approx(5.0)
        assert budget.remaining_seconds == pytest.approx(0.0)
        with paths.console.open("ab") as output:
            output.write(b"-last")
        assert source.read(64 * 1024) == b"retained-last"
        fcntl.flock(holder, fcntl.LOCK_UN)
        assert source.current_status(observation_budget=state.RunObservationWaitBudget()) == "stopped"
    finally:
        source.close()
        os.close(holder)


def test_native_follow_cancel_and_missing_run_keep_retained_descriptor_only(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    monkeypatch.setattr(
        vz_native_runtime, "refresh_run", lambda *_args, **_kwargs: pytest.fail("retained follow reached provider")
    )
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    shutil.rmtree(paths.root)
    terminal = next(events)
    assert isinstance(terminal, LogTerminalEvent)
    assert terminal.outcome == LogTerminalOutcome(LogTerminalCategory.RUN_TERMINAL, run_status="removed")
    events.close()

    _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    stream = runtime_dispatch.logs("demo", roots=roots, follow=True)
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    stream.cancel()
    terminal = next(events)
    assert isinstance(terminal, LogTerminalEvent)
    assert terminal.outcome.category is LogTerminalCategory.CANCELLED
    events.close()


def test_native_follow_wait_holds_no_mutex_and_concurrent_cancel_wakes_it(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    _run(roots, backend="lima-vz", runtime_kind="oci-root", content=b"initial")
    record = state.read_run_dispatch_record(roots, "demo")
    stream = log_stream.open_retained_console_stream(
        roots, record, LogMode.FOLLOW, observation_budget=state.RunObservationWaitBudget()
    )
    events = stream.events()
    assert isinstance(next(events), LogDataEvent)
    waiting, finished = threading.Event(), threading.Event()
    original_wait = stream._cancelled.wait
    observed = []

    def wait_without_mutex(delay):
        probe = os.open(roots.locks / "demo.lock", os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        finally:
            os.close(probe)
        waiting.set()
        return original_wait(delay)

    def consume():
        observed.append(next(events))
        finished.set()

    monkeypatch.setattr(stream._cancelled, "wait", wait_without_mutex)
    worker = threading.Thread(target=consume, daemon=True)
    try:
        worker.start()
        assert waiting.wait(1)
        stream.cancel()
        assert finished.wait(1)
        worker.join(1)
        assert not worker.is_alive()
        assert isinstance(observed[0], LogTerminalEvent)
        assert observed[0].outcome.category is LogTerminalCategory.CANCELLED
    finally:
        stream.cancel()
        worker.join(1)
        if not worker.is_alive():
            events.close()


def test_native_cli_failed_hold_does_not_bypass_custody_with_retained_bytes_or_guest(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="failed", content=b"retained")
    before = paths.state.read_bytes(), paths.console.read_bytes()

    def held_config(_authority):
        _assert_log_run_lock(roots)
        raise vz_native_runtime.VZNativeHold("native VZ resource HOLD")

    monkeypatch.setattr(vz_native_runtime, "_config_for", held_config)
    monkeypatch.setattr(
        vz_native_runtime, "_control", lambda *_args, **_kwargs: pytest.fail("HOLD logs reached guest transport")
    )
    monkeypatch.setattr(
        state, "open_pinned_run_console", lambda *_args, **_kwargs: pytest.fail("HOLD refresh bypassed with console")
    )
    with pytest.raises(LogStreamError) as caught:
        runtime_dispatch.logs("demo", roots=roots)
    assert caught.value.category is LogErrorCategory.READ_FAILED
    assert (paths.state.read_bytes(), paths.console.read_bytes()) == before


def test_native_snapshot_eof_reuses_initial_wait_allowance(tmp_path, monkeypatch):
    roots = _roots(tmp_path)
    _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    clock, phase = [600.0], ["initial"]

    def advance(delay):
        clock[0] += delay
        if phase[0] == "initial" and clock[0] >= 604.0:
            fcntl.flock(holder, fcntl.LOCK_UN)

    monkeypatch.setattr(state.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(state.time, "sleep", advance)
    events = None
    try:
        stream = runtime_dispatch.logs("demo", roots=roots)
        events = stream.events()
        first = next(events)
        assert isinstance(first, LogDataEvent) and first.data == b"retained"
        phase[0] = "snapshot-eof"
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        terminal = next(events)
        assert isinstance(terminal, LogTerminalEvent)
        assert terminal.outcome.error_category is LogErrorCategory.READ_FAILED
        assert clock[0] - 600.0 == pytest.approx(5.0)
    finally:
        if events is not None:
            events.close()
        os.close(holder)


@pytest.mark.parametrize("backend", ["kvm", "lima-vz"])
def test_legacy_boot_console_streams_while_lifecycle_owns_run_lock(tmp_path, backend):
    roots = _roots(tmp_path)
    _run(roots, backend=backend, status="starting", content=b"\x00boot\xff\n")
    holder = os.open(roots.locks / "demo.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        stream = runtime_dispatch.logs("demo", roots=roots)
        try:
            events = list(stream.events())
        finally:
            stream.close()
        assert b"".join(event.data for event in events if isinstance(event, LogDataEvent)) == b"\x00boot\xff\n"
        assert events[-1].outcome.category is LogTerminalCategory.SNAPSHOT_COMPLETE
    finally:
        os.close(holder)


def test_missing_logs_do_not_create_run_lock_or_metadata(tmp_path):
    roots = _roots(tmp_path)
    before_runs = tuple(roots.runs.iterdir())
    before_locks = tuple(roots.locks.iterdir())
    with pytest.raises(StateError):
        runtime_dispatch.logs("missing", roots=roots)
    assert tuple(roots.runs.iterdir()) == before_runs
    assert tuple(roots.locks.iterdir()) == before_locks


@pytest.mark.parametrize("hint_available", [False, True])
def test_native_log_routing_never_adopts_replaced_expected_identity(tmp_path, monkeypatch, hint_available):
    roots = _roots(tmp_path)
    paths, run_id = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"private")
    expected = ExpectedRunIdentity("demo", run_id, DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.LIMA_VZ))
    original = runtime_dispatch.resolve_existing_run
    replaced = str(uuid.uuid4())

    def route_then_replace(name, **kwargs):
        if kwargs.get("_observation_budget") is None:
            hint = original(name, **kwargs)
            payload = state.read_json(paths.state)
            state.atomic_write_json(paths.owner, {"schema_version": 1, "run_id": replaced, "name": "demo"})
            state.atomic_write_json(paths.state, {**payload, "run_id": replaced})
            if not hint_available:
                raise StateError("routing metadata changed")
            return hint
        return original(name, **kwargs)

    monkeypatch.setattr(runtime_dispatch, "resolve_existing_run", route_then_replace)
    with pytest.raises(StateError):
        runtime_dispatch.logs("demo", roots=roots, expected_identity=expected)


@pytest.mark.parametrize(
    "backend,runtime_kind",
    [
        ("kvm", "cloud-image"),
        ("lima-vz", "cloud-image"),
        ("lima-vz", "oci-root"),
    ],
)
def test_pinned_status_directory_ctime_guard_is_native_only(tmp_path, monkeypatch, backend, runtime_kind):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend=backend, runtime_kind=runtime_kind, status="stopped", content=b"retained")
    record = state.read_run_dispatch_record(roots, "demo")
    source = state.open_pinned_run_console(roots, record)
    native = runtime_kind == "oci-root"
    reader_name = "_read_exact_private_file" if native else "_read_pinned_json_object"
    original_reader = getattr(state, reader_name)
    changed = []

    def read_with_sibling_publication(directory_fd, filename):
        result = original_reader(directory_fd, filename)
        if filename == "owner.json":
            # Legacy boot writers may publish unrelated siblings while this
            # reader observes individually pinned JSON files.
            (paths.root / "boot-progress").write_bytes(b"ready")
            changed.append(True)
        return result

    monkeypatch.setattr(state, reader_name, read_with_sibling_publication)
    try:
        if native:
            with pytest.raises(LogStreamError) as caught:
                source.current_status()
            assert caught.value.category is LogErrorCategory.RUN_CHANGED
        else:
            assert source.current_status() == "stopped"
        assert changed == [True]
    finally:
        source.close()


@pytest.mark.parametrize("filename", ["owner.json", "state.json"])
@pytest.mark.parametrize("already_locked", [False, True])
def test_native_pinned_status_requires_private_ledgers_even_with_caller_lock(tmp_path, filename, already_locked):
    roots = _roots(tmp_path)
    paths, _ = _run(roots, backend="lima-vz", runtime_kind="oci-root", status="stopped", content=b"retained")
    record = state.read_run_dispatch_record(roots, "demo")
    source = state.open_pinned_run_console(roots, record)
    try:
        if already_locked:
            with state.locked_existing_run(roots, "demo", expected=record):
                (paths.root / filename).chmod(0o644)
                with pytest.raises(LogStreamError) as caught:
                    source.current_status()
        else:
            (paths.root / filename).chmod(0o644)
            with pytest.raises(LogStreamError) as caught:
                source.current_status(observation_budget=state.RunObservationWaitBudget())
        assert caught.value.category is LogErrorCategory.RUN_CHANGED
    finally:
        source.close()
