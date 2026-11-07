"""Web-owned run clients cancel preparation, not VM lifecycle authority."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

_cancellation_depth = 0
_pending_cancellation = None
_held_preparations = []
_termination_depth = 0
_termination_pending = None
_termination_checkpoint = ContextVar("palimpsest_no_force_checkpoint", default=None)


class _PreparationTerminated(BaseException):
    """Private graceful cancellation of one authenticated no-force worker."""


def require_preparation_custody_owner():
    """Admit actors only to a main/non-daemon owner that can retain their lifetime."""
    if threading.current_thread().daemon:
        raise ValueError("no-force preparation requires a main or non-daemon custody owner")


def _terminate_preparation(number, frame):
    global _termination_pending
    if _termination_depth:
        _termination_pending = number
        return
    if number == signal.SIGINT:
        raise KeyboardInterrupt
    raise _PreparationTerminated("no-force worker preparation was terminated")


@contextmanager
def defer_preparation_termination():
    """Defer no-force cancellation across process ownership and cleanup handoffs."""
    global _termination_depth, _termination_pending
    if (
        threading.current_thread() is not threading.main_thread()
        or signal.getsignal(signal.SIGTERM) is not _terminate_preparation
    ):
        yield
        return
    _termination_depth += 1
    try:
        yield
    finally:
        _termination_depth -= 1
        if not _termination_depth and _termination_pending:
            number = _termination_pending
            _termination_pending = None
            if number == signal.SIGINT:
                raise KeyboardInterrupt
            raise _PreparationTerminated("no-force worker preparation was terminated")


@contextmanager
def no_force_preparation_termination(*, checkpoint=None):
    """Own no-force cancellation/checkpoints for a main or non-daemon caller.

    Only the main thread may alter process-global signal handlers. Off-main
    owners remain non-daemon through all actor settlement or retained HOLD.
    """
    require_preparation_custody_owner()
    checkpoint_token = _termination_checkpoint.set(checkpoint)
    previous = []
    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                previous.append((number, signal.signal(number, _terminate_preparation)))
        yield
    finally:
        _termination_checkpoint.reset(checkpoint_token)
        for number, handler in previous:
            signal.signal(number, handler)


class PreparationToolHold(RuntimeError):
    """Exact process/FD custody is retained; callers must not remove live scratch."""


def retain_preparation_custody(*resources):
    _held_preparations.append(resources)


def preparation_custody_held():
    return bool(_held_preparations)


def park_preparation_custody():
    """Retain custody without unwinding; admitted owners must remain non-daemon."""
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(3600)


def _group_exists(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _run_no_force(*args, input=None, capture_output=False, timeout=None, check=False, checkpoint=None, **kwargs):
    require_preparation_custody_owner()
    if preparation_custody_held():
        raise PreparationToolHold("previous preparation retains custody; no replay permitted")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("no-force preparation requires a finite positive timeout")

    def check_bounds():
        phase_checkpoint = _termination_checkpoint.get()
        if phase_checkpoint is not None:
            phase_checkpoint()
        if checkpoint is not None:
            checkpoint()

    deadline = time.monotonic() + timeout
    if "start_new_session" in kwargs:
        raise ValueError("no-force preparation owns its process group")
    captures = []
    process = None
    held = False
    try:
        if capture_output:
            if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                raise ValueError("capture_output conflicts with stdout/stderr")
            captures = [tempfile.TemporaryFile(), tempfile.TemporaryFile()]
            kwargs["stdout"], kwargs["stderr"] = captures
        if input is not None:
            if kwargs.get("stdin") is not None:
                raise ValueError("stdin conflicts with input")
            kwargs["stdin"] = subprocess.PIPE
        with defer_web_cancellation(), defer_preparation_termination():
            check_bounds()
            process = subprocess.Popen(*args, start_new_session=True, **kwargs)
        try:
            pending_input = input
            while True:
                check_bounds()
                if sum(os.fstat(stream.fileno()).st_size for stream in captures) > 1024 * 1024:
                    raise subprocess.SubprocessError("preparation output exceeds its bound")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                try:
                    process.communicate(pending_input, timeout=min(remaining, 0.05))
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
            check_bounds()
            if _group_exists(process.pid):
                raise subprocess.SubprocessError("preparation left a live process group")
        except BaseException:
            with defer_web_cancellation(), defer_preparation_termination():
                settled = False
                try:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1.0)
                    deadline = time.monotonic() + 1.0
                    while _group_exists(process.pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    settled = not _group_exists(process.pid)
                except BaseException:
                    pass
                if not settled:
                    held = True
                    retain_preparation_custody(process, *captures)
                    raise PreparationToolHold("preparation cleanup is unknown; process and FDs retained") from None
                raise
        output = []
        if sum(os.fstat(stream.fileno()).st_size for stream in captures) > 1024 * 1024:
            raise subprocess.SubprocessError("preparation output exceeds its bound")
        for stream in captures:
            stream.seek(0, os.SEEK_END)
            if stream.tell() > 1024 * 1024:
                raise subprocess.SubprocessError("preparation output exceeds its bound")
            stream.seek(0)
            output.append(stream.read())
        stdout, stderr = output if captures else (None, None)
        if captures and process.text_mode:
            stdout = stdout.decode(process.encoding, errors=process.errors or "strict")
            stderr = stderr.decode(process.encoding, errors=process.errors or "strict")
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    finally:
        with defer_web_cancellation(), defer_preparation_termination():
            if process is not None and not held and (process.poll() is None or _group_exists(process.pid)):
                try:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1.0)
                    deadline = time.monotonic() + 1.0
                    while _group_exists(process.pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    held = _group_exists(process.pid)
                except BaseException:
                    held = True
                if held:
                    retain_preparation_custody(process, *captures)
                    raise PreparationToolHold("preparation actor/FD custody retained; cleanup is unknown") from None
            if not held:
                for stream in captures:
                    stream.close()


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


def run_preparation_tool(
    *args, input=None, capture_output=False, timeout=None, check=False, no_force=False, checkpoint=None, **kwargs
):
    """subprocess.run semantics with a web-owned, cancellation-safe tool session.

    Ordinary CLI and monitor calls keep their existing process-group behavior.
    This runner is only for preparation tools, never VM/monitor activation.
    """
    if type(no_force) is not bool:
        raise ValueError("no-force preparation policy must be boolean")
    if no_force:
        return _run_no_force(
            *args,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
            checkpoint=checkpoint,
            **kwargs,
        )
    if checkpoint is not None:
        raise ValueError("preparation checkpoints require explicit no-force policy")
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
