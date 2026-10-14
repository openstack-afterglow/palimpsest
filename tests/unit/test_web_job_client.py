"""Deterministic cancellation at real helper ownership handoffs, without native tools."""

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from palimpsest_local import registry_intake, web_job_client

pytestmark = pytest.mark.skipif(os.name != "posix", reason="owned helper sessions require POSIX")


@pytest.mark.parametrize("boundary", ["spawn", "deadline"])
def test_bounded_copy_cancellation_owns_spawn_before_deadline(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv("PALIMPSEST_WEB_JOB", "run")
    original_spawn = subprocess.Popen
    original_clock = registry_intake.time.monotonic
    unrelated = original_spawn([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    processes = []
    directories = []
    cancelled = False

    def cancel():
        nonlocal cancelled
        cancelled = True
        os.kill(os.getpid(), signal.SIGUSR1)

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        if boundary == "spawn":
            # Before Popen has returned to its caller, not a worker-ready wait.
            cancel()
        return process

    def clock():
        if boundary == "deadline" and processes and not cancelled:
            cancel()
        return original_clock()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(registry_intake.time, "monotonic", clock)
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        with pytest.raises(SystemExit) as raised:
            with web_job_client.cancellable_run_preparation():
                with tempfile.TemporaryDirectory(dir=tmp_path, prefix="acquisition-") as scratch:
                    directories.append(Path(scratch))
                    registry_intake._run_bounded_copy(
                        (sys.executable, "-c", "import time; time.sleep(60)"),
                        Path(scratch) / "image.tar",
                        Path(scratch),
                        timeout_seconds=2,
                        maximum_bytes=1048576,
                    )
        assert raised.value.code == 128 + signal.SIGUSR1
        assert signal.getsignal(signal.SIGUSR1) == previous
        assert len(processes) == 1 and processes[0].returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)
        assert all(not directory.exists() for directory in directories)
        assert unrelated.poll() is None, "cancellation signaled an unrelated/VM-owned session"
    finally:
        for process in (*processes, unrelated):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_preparation_tool_cancellation_at_spawn_reaps_only_its_session(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_WEB_JOB", "run")
    original_spawn = subprocess.Popen
    processes = []

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        os.kill(os.getpid(), signal.SIGUSR1)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        with pytest.raises(SystemExit):
            with web_job_client.cancellable_run_preparation():
                web_job_client.run_preparation_tool(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    capture_output=True,
                    timeout=2,
                )
        assert len(processes) == 1 and processes[0].returncode is not None
        assert processes[0].stdin is None
        assert processes[0].stdout.closed and processes[0].stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_deferred_signal_dispatch_from_existing_thread_waits_for_owner(monkeypatch):
    import threading

    monkeypatch.setenv("PALIMPSEST_WEB_JOB", "run")
    owned = []
    with pytest.raises(SystemExit):
        with web_job_client.cancellable_run_preparation():
            try:
                with web_job_client.defer_web_cancellation():
                    thread = threading.Thread(target=lambda: os.kill(os.getpid(), signal.SIGUSR1))
                    thread.start()
                    thread.join(timeout=2)
                    assert not thread.is_alive()
                    owned.append("registered")
            finally:
                owned.append("cleaned")
    assert owned == ["registered", "cleaned"]


def test_preparation_tool_preserves_stdin_capture_and_check(monkeypatch):
    monkeypatch.setenv("PALIMPSEST_WEB_JOB", "run")
    with web_job_client.cancellable_run_preparation():
        completed = web_job_client.run_preparation_tool(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            input="private input",
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
    assert completed.stdout == "PRIVATE INPUT" and completed.stderr == "" and completed.returncode == 0
