"""Byte-transparent local subprocess transport for runtime adapters."""

from __future__ import annotations

import errno
import fcntl
import os
import select
import selectors
import signal as signal_module
import struct
import subprocess
import sys
import termios
import threading
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from typing import Any

from .errors import LifecycleError
from .runtime_types import (
    ProcessCapabilities,
    ProcessCapabilityError,
    ProcessEvent,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSession,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
)

_READ_SIZE = 64 * 1024
_CLOSE_TIMEOUT_SECONDS = 1.0
_PTY_CHILD_BOOTSTRAP = """
import fcntl
import os
import sys
import termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.tcsetpgrp(0, os.getpgrp())
os.execvp(sys.argv[1], sys.argv[1:])
"""
_SIGNALS = {
    ProcessSignal.INTERRUPT: signal_module.SIGINT,
    ProcessSignal.TERMINATE: signal_module.SIGTERM,
    ProcessSignal.HANGUP: signal_module.SIGHUP,
}


def _exit_result(returncode: int, *, cancelled: bool = False) -> ProcessExit:
    if returncode < 0:
        category = ProcessExitCategory.CANCELLED if cancelled else ProcessExitCategory.SIGNALED
        return ProcessExit(returncode, None, -returncode, category)
    category = ProcessExitCategory.CANCELLED if cancelled else ProcessExitCategory.EXITED
    return ProcessExit(returncode, returncode, None, category)


def _rollback_spawned_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal_module.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=_CLOSE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal_module.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


class LocalProcessSession:
    """A single-consumer process session; backend argv is intentionally private."""

    __slots__ = (
        "_capabilities",
        "_cancelled",
        "_cancel_event",
        "_events_started",
        "_lifecycle_lock",
        "_master_fd",
        "_process",
        "_result",
        "_status_emitted",
        "_stdin_closed",
        "_stdin_lock",
        "_tty",
    )

    def __init__(
        self,
        argv: Sequence[str],
        *,
        tty: bool,
        stdin: bool,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
            raise TypeError("runtime adapter supplied an invalid process argv")
        if tty and not stdin:
            raise ValueError("a TTY process session requires stdin")
        self._tty = tty
        self._capabilities = ProcessCapabilities(stdin=stdin, tty=tty, resize=tty, signal=True)
        self._master_fd: int | None = None
        self._stdin_closed = not stdin
        self._stdin_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._events_started = False
        self._status_emitted = False
        self._result: ProcessExit | None = None
        self._cancelled = False

        master_fd: int | None = None
        slave_fd: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            if tty:
                master_fd, slave_fd = os.openpty()
                process = subprocess.Popen(
                    (sys.executable, "-c", _PTY_CHILD_BOOTSTRAP, *argv),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    bufsize=0,
                    close_fds=True,
                    pass_fds=pass_fds,
                    start_new_session=True,
                )
                self._master_fd = master_fd
                master_fd = None
            else:
                process = subprocess.Popen(
                    tuple(argv),
                    stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    close_fds=True,
                    pass_fds=pass_fds,
                    start_new_session=True,
                )
            self._process = process
            if tty:
                assert self._master_fd is not None
                os.set_blocking(self._master_fd, False)
            elif stdin and process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
        except (OSError, ValueError):
            if process is not None:
                _rollback_spawned_process(process)
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
            raise LifecycleError("cannot start runtime process session") from None
        finally:
            if slave_fd is not None:
                os.close(slave_fd)
            if master_fd is not None:
                os.close(master_fd)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(capabilities={self._capabilities!r})"

    @property
    def capabilities(self) -> ProcessCapabilities:
        return self._capabilities

    def _set_result(self, *, cancelled: bool | None = None) -> ProcessExit:
        returncode = self._process.wait()
        with self._lifecycle_lock:
            if self._result is None:
                self._result = _exit_result(
                    returncode,
                    cancelled=self._cancelled if cancelled is None else cancelled,
                )
            return self._result

    def _event_sources(self) -> tuple[tuple[int, ProcessStream], ...]:
        if self._tty:
            assert self._master_fd is not None
            return ((self._master_fd, ProcessStream.PTY),)
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        return (
            (self._process.stdout.fileno(), ProcessStream.STDOUT),
            (self._process.stderr.fileno(), ProcessStream.STDERR),
        )

    def _close_output_descriptors(self) -> None:
        if self._tty:
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
            return
        for stream in (self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def events(self) -> Iterator[ProcessEvent]:
        if self._events_started:
            raise LifecycleError("process events already have a consumer")
        self._events_started = True
        if self._result is not None:
            if not self._status_emitted:
                self._status_emitted = True
                yield ProcessStatusEvent(self._result)
            return
        selector = selectors.DefaultSelector()
        try:
            for descriptor, stream in self._event_sources():
                selector.register(descriptor, selectors.EVENT_READ, stream)
            while selector.get_map():
                for key, _mask in selector.select():
                    try:
                        data = os.read(key.fd, _READ_SIZE)
                    except OSError as exc:
                        if self._tty and exc.errno == errno.EIO:
                            data = b""
                        elif exc.errno == errno.EINTR:
                            continue
                        elif exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            continue
                        else:
                            raise LifecycleError("cannot read runtime process output") from None
                    if not data:
                        selector.unregister(key.fd)
                        continue
                    yield ProcessOutputEvent(key.data, data)
        finally:
            selector.close()
            self._close_output_descriptors()
        result = self._set_result()
        if not self._status_emitted:
            self._status_emitted = True
            yield ProcessStatusEvent(result)

    def write_stdin(self, data: bytes) -> None:
        if not self._capabilities.stdin:
            raise ProcessCapabilityError("stdin")
        if not isinstance(data, bytes):
            raise TypeError("process stdin requires bytes")
        if not data:
            return
        with self._stdin_lock:
            if self._stdin_closed or self._cancel_event.is_set():
                raise LifecycleError("process stdin is closed")
            if self._tty:
                assert self._master_fd is not None
                descriptor = self._master_fd
            else:
                assert self._process.stdin is not None
                descriptor = self._process.stdin.fileno()
        view = memoryview(data)
        while view:
            if self._cancel_event.is_set():
                raise LifecycleError("process stdin is closed")
            try:
                with self._stdin_lock:
                    if self._stdin_closed or self._cancel_event.is_set():
                        raise LifecycleError("process stdin is closed")
                    written = os.write(descriptor, view)
                view = view[written:]
            except BlockingIOError:
                try:
                    _readable, writable, _exceptional = select.select([], [descriptor], [], 0.05)
                except OSError:
                    raise LifecycleError("process stdin is closed") from None
                if not writable:
                    continue
            except LifecycleError:
                raise
            except (BrokenPipeError, OSError):
                with self._stdin_lock:
                    self._stdin_closed = True
                raise LifecycleError("process stdin is closed") from None

    def close_stdin(self) -> None:
        if not self._capabilities.stdin:
            return
        with self._stdin_lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
            try:
                if self._tty:
                    assert self._master_fd is not None
                    os.write(self._master_fd, b"\x04")
                elif self._process.stdin is not None:
                    self._process.stdin.close()
            except OSError:
                pass

    def resize(self, rows: int, columns: int) -> None:
        if not self._capabilities.resize:
            raise ProcessCapabilityError("terminal resize")
        if type(rows) is not int or type(columns) is not int or not 1 <= rows <= 65_535 or not 1 <= columns <= 65_535:
            raise ValueError("terminal dimensions must be integers between 1 and 65535")
        assert self._master_fd is not None
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            os.killpg(self._process.pid, signal_module.SIGWINCH)
        except ProcessLookupError:
            return
        except OSError:
            raise LifecycleError("cannot resize runtime process terminal") from None

    def signal(self, requested: ProcessSignal) -> None:
        if not isinstance(requested, ProcessSignal):
            raise TypeError("process signal requires a ProcessSignal")
        if self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, _SIGNALS[requested])
        except ProcessLookupError:
            return
        except OSError:
            raise LifecycleError("cannot signal runtime process session") from None

    def wait(self) -> ProcessExit:
        if self._result is not None:
            return self._result
        if not self._events_started:
            for _event in self.events():
                pass
        return self._set_result()

    def close(self) -> None:
        self._cancel_event.set()
        with self._lifecycle_lock:
            if self._result is not None:
                return
            completed = self._process.poll()
            if completed is not None:
                self._result = _exit_result(completed)
                self._close_output_descriptors()
                return
            self._cancelled = True
        self.close_stdin()
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal_module.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._process.wait(timeout=_CLOSE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, signal_module.SIGKILL)
                except ProcessLookupError:
                    pass
        self._set_result(cancelled=True)
        self._close_output_descriptors()


class ContextBoundProcessSession:
    """Retain adapter-owned resources or authority until a transport terminates."""

    __slots__ = ("_authority", "_release_lock", "_released", "_session")

    def __init__(self, session: ProcessSession, authority: AbstractContextManager[Any]) -> None:
        self._session = session
        self._authority = authority
        self._release_lock = threading.Lock()
        self._released = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(capabilities={self.capabilities!r})"

    @property
    def capabilities(self) -> ProcessCapabilities:
        return self._session.capabilities

    def _release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            self._authority.__exit__(None, None, None)

    def events(self) -> Iterator[ProcessEvent]:
        completed = False
        try:
            for event in self._session.events():
                if isinstance(event, ProcessStatusEvent):
                    self._release()
                    completed = True
                yield event
            completed = True
        finally:
            if not completed:
                self._session.close()
            self._release()

    def write_stdin(self, data: bytes) -> None:
        self._session.write_stdin(data)

    def close_stdin(self) -> None:
        self._session.close_stdin()

    def resize(self, rows: int, columns: int) -> None:
        self._session.resize(rows, columns)

    def signal(self, requested: ProcessSignal) -> None:
        self._session.signal(requested)

    def wait(self) -> ProcessExit:
        try:
            return self._session.wait()
        finally:
            self._release()

    def close(self) -> None:
        try:
            self._session.close()
        finally:
            self._release()


def bind_process_session_context(
    session: ProcessSession,
    authority: AbstractContextManager[Any],
) -> ContextBoundProcessSession:
    return ContextBoundProcessSession(session, authority)


def spawn_process_session(
    argv: Sequence[str],
    *,
    tty: bool = False,
    stdin: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> LocalProcessSession:
    """Spawn one adapter-owned process without a host shell."""

    return LocalProcessSession(argv, tty=tty, stdin=stdin, pass_fds=pass_fds)


__all__ = (
    "ContextBoundProcessSession",
    "LocalProcessSession",
    "bind_process_session_context",
    "spawn_process_session",
)
