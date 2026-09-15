"""Typed retained-console stream contracts."""

from __future__ import annotations

import io
import json
import os
import shutil
import threading
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from palimpsest_local import cli, runtime_dispatch, state, ui
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
    status: str = "running",
    backend: str = "kvm",
    runtime_kind: str = "cloud-image",
    content: bytes = b"",
) -> tuple[state.RunPaths, str]:
    paths = state.run_paths(roots, "demo")
    paths.root.mkdir(mode=0o700)
    run_id = str(uuid.uuid4())
    state.atomic_write_json(paths.owner, {"schema_version": 1, "run_id": run_id, "name": "demo"})
    state.atomic_write_json(
        paths.state,
        {
            "schema_version": 2,
            "runtime_kind": runtime_kind,
            "backend": backend,
            "name": "demo",
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
def test_console_authority_rejects_unsafe_entries(tmp_path: Path, kind: str) -> None:
    roots = _roots(tmp_path)
    paths, _ = _run(roots, content=b"x")
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


def test_ui_tail_is_bounded_and_decodes_only_at_rendering_boundary(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _run(roots, content=b"old\nmid\xff\nlast-\xe2\x82\xac")
    stream = runtime_dispatch.logs("demo", roots=roots)
    assert ui._bounded_log_tail(stream, 2) == "mid�\nlast-€"


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
