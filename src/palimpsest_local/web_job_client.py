"""Web-owned run clients cancel preparation, not VM lifecycle authority."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from contextlib import contextmanager

_cancellation_depth = 0
_pending_cancellation = None


def _cancel_preparation(number, frame) -> None:
    global _pending_cancellation
    if _cancellation_depth:
        _pending_cancellation = number
        return
    _pending_cancellation = None
    # Ignore repeated shutdown requests while owned resources unwind. SIGTERM
    # is deliberately not used: OCI startup interprets it as a VM STOP.
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    raise SystemExit(128 + number)


@contextmanager
def cancellable_run_preparation():
    """Handle the private client signal only while no VM has been launched.

    Outside these scopes SIGUSR1 retains its normal process-only termination;
    the VM's separate monitor/daemon is not signaled. This is not a public run
    cancellation API and does not change ordinary CLI SIGINT/SIGTERM behavior.
    """
    if os.environ.get("PALIMPSEST_WEB_JOB") != "run" or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        signal.signal(signal.SIGUSR1, _cancel_preparation)
        yield
    finally:
        signal.signal(signal.SIGUSR1, previous)


@contextmanager
def defer_web_cancellation():
    """Arm an owner's try/finally before delivering the private web signal.

    Deferral is in the Python handler, not a pthread mask: a process-directed
    signal may arrive on an existing libvirt/I/O thread and still dispatch on
    the main thread. Callers arm cleanup outside this scope before acquiring.
    """
    global _cancellation_depth, _pending_cancellation
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGUSR1) is not _cancel_preparation
    ):
        yield
        return
    _cancellation_depth += 1
    try:
        yield
    finally:
        _cancellation_depth -= 1
        if not _cancellation_depth and _pending_cancellation is not None:
            number, _pending_cancellation = _pending_cancellation, None
            _cancel_preparation(number, None)


def run_preparation_tool(*args, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """subprocess.run semantics with a web-owned, cancellation-safe tool session.

    Ordinary CLI and monitor calls keep their existing process-group behavior.
    This runner is only for preparation tools, never VM/monitor activation.
    """
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGUSR1) is not _cancel_preparation
    ):
        return subprocess.run(*args, input=input, capture_output=capture_output, timeout=timeout, check=check, **kwargs)
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process = None
    try:
        with defer_web_cancellation():
            process = subprocess.Popen(*args, start_new_session=True, **kwargs)
        stdout, stderr = process.communicate(input, timeout=timeout)
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    finally:
        if process is not None:
            with defer_web_cancellation():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
