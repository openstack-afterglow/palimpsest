from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from palimpsest_local import process_session
from palimpsest_local.errors import LifecycleError
from palimpsest_local.process_session import spawn_process_session
from palimpsest_local.runtime_types import (
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessStatusEvent,
    ProcessStream,
)


def test_pipe_session_preserves_binary_split_streams_and_exact_exit() -> None:
    session = spawn_process_session(
        [
            sys.executable,
            "-c",
            "import os; os.write(1, b'out\\xff'); os.write(2, b'err\\xfe'); raise SystemExit(17)",
        ]
    )

    events = list(session.events())
    output = [event for event in events if isinstance(event, ProcessOutputEvent)]
    status = [event for event in events if isinstance(event, ProcessStatusEvent)]

    assert {(event.stream, event.data) for event in output} == {
        (ProcessStream.STDOUT, b"out\xff"),
        (ProcessStream.STDERR, b"err\xfe"),
    }
    assert len(status) == 1
    assert status[0].result.returncode == 17
    assert status[0].result.category is ProcessExitCategory.EXITED
    assert session.wait() is status[0].result
    session.close()


def test_pipe_session_stdin_close_is_idempotent() -> None:
    session = spawn_process_session(
        [sys.executable, "-c", "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"],
        stdin=True,
    )
    session.write_stdin(b"literal\x00bytes")
    session.close_stdin()
    session.close_stdin()

    events = list(session.events())
    assert [(event.stream, event.data) for event in events if isinstance(event, ProcessOutputEvent)] == [
        (ProcessStream.STDOUT, b"literal\x00bytes")
    ]
    assert session.wait().returncode == 0


def test_tty_session_merges_output_and_redacts_argv_from_repr() -> None:
    secret = "SENSITIVE_PROCESS_ARG"
    session = spawn_process_session(
        [sys.executable, "-c", "import os; os.write(1,b'one'); os.write(2,b'two')", secret],
        tty=True,
        stdin=True,
    )
    assert session.capabilities.tty
    assert session.capabilities.resize
    assert secret not in repr(session)
    session.resize(40, 100)

    events = list(session.events())
    output = b"".join(event.data for event in events if isinstance(event, ProcessOutputEvent))
    assert output in {b"onetwo", b"twoone"}
    assert all(event.stream is ProcessStream.PTY for event in events if isinstance(event, ProcessOutputEvent))
    assert session.wait().returncode == 0


def test_tty_session_has_controlling_terminal_and_foreground_process_group() -> None:
    session = spawn_process_session(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "fd=os.open('/dev/tty', os.O_RDWR); "
                "assert os.tcgetpgrp(fd) == os.getpgrp(); "
                "os.write(fd, b'controlling-tty-ok')"
            ),
        ],
        tty=True,
        stdin=True,
    )

    events = list(session.events())

    assert b"controlling-tty-ok" in b"".join(event.data for event in events if isinstance(event, ProcessOutputEvent))
    assert session.wait().returncode == 0


def test_close_is_idempotent_and_reports_cancelled_transport() -> None:
    session = spawn_process_session([sys.executable, "-c", "import time; time.sleep(60)"])
    session.close()
    first = session.wait()
    session.close()

    assert first.category is ProcessExitCategory.CANCELLED
    assert session.wait() is first
    assert [event.result for event in session.events() if isinstance(event, ProcessStatusEvent)] == [first]


def test_close_cancels_blocked_stdin_writer_for_sigterm_resistant_child(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    session = spawn_process_session(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).touch(); "
                "time.sleep(60)"
            ),
            str(ready),
        ],
        stdin=True,
    )
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    writer_finished = threading.Event()
    failures: list[BaseException] = []

    def write_until_cancelled() -> None:
        try:
            session.write_stdin(b"x" * (64 * 1024 * 1024))
        except BaseException as exc:
            failures.append(exc)
        finally:
            writer_finished.set()

    writer = threading.Thread(target=write_until_cancelled)
    writer.start()
    time.sleep(0.1)
    assert writer.is_alive()

    started = time.monotonic()
    session.close()
    elapsed = time.monotonic() - started
    writer.join(timeout=1)

    assert elapsed < 2
    assert writer_finished.is_set()
    assert len(failures) == 1
    assert isinstance(failures[0], LifecycleError)
    assert session.wait().category is ProcessExitCategory.CANCELLED


def test_close_after_natural_exit_does_not_reclassify_it_as_cancelled() -> None:
    session = spawn_process_session([sys.executable, "-c", "pass"])
    time.sleep(0.1)
    session.close()

    assert session.wait().category is ProcessExitCategory.EXITED


def test_constructor_setup_failure_reaps_spawned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "spawned.pid"
    real_set_blocking = os.set_blocking

    def fail_after_child_started(descriptor: int, blocking: bool) -> None:
        del descriptor, blocking
        deadline = time.monotonic() + 2
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        raise OSError("fault injection")

    monkeypatch.setattr(process_session.os, "set_blocking", fail_after_child_started)

    with pytest.raises(LifecycleError, match="cannot start runtime process session"):
        spawn_process_session(
            [
                sys.executable,
                "-c",
                ("import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"),
                str(pid_path),
            ],
            stdin=True,
        )

    monkeypatch.setattr(process_session.os, "set_blocking", real_set_blocking)
    child_pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
