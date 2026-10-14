"""Authenticated-route transport state; authorization belongs to the HTTP owner.

Each console has one event consumer. Retention is a bounded replay window: a
reader falling behind receives an explicit gap, never a truncated byte stream.
Disconnect closes only the adapter-owned process transport, not its VM.
"""

from __future__ import annotations

import base64
import math
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

from . import vm_access
from .errors import PalimpsestError
from .oci_control_protocol_v2 import validate_exec_argv
from .runtime_types import (
    ProcessCapabilityError,
    ProcessOutputEvent,
    ProcessSession,
    ProcessSignal,
    ProcessStatusEvent,
    RuntimeCapabilityError,
)
from .state import StatePaths


class ConsoleError(PalimpsestError):
    code = "console-error"

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


@dataclass
class _Console:
    id: str
    name: str
    session: ProcessSession
    condition: threading.Condition = field(default_factory=threading.Condition)
    input_lock: threading.Lock = field(default_factory=threading.Lock)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)
    events: deque = field(default_factory=deque)
    size: int = 0
    cursor: int = 0
    closed: bool = False
    stopping: bool = False
    touched: float = field(default_factory=time.monotonic)
    readers: int = 0
    thread: threading.Thread | None = None


class ConsoleManager:
    MAX_SESSIONS = 16
    MAX_EVENTS = 1024
    MAX_BYTES = 1024 * 1024  # encoded output bytes, per console
    MAX_INPUT_BYTES = 65536
    MAX_READERS = 4  # per console
    MAX_WAIT_SECONDS = 2.0
    IDLE_SECONDS = 300.0

    def __init__(self, *, exec_owners=None) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, _Console] = {}
        self._cancelled_opens: dict[str, float] = {}
        self._shutdown = threading.Event()
        self._exec_owners = exec_owners
        self._reaper: threading.Thread | None = None

    def open(
        self,
        roots: StatePaths,
        name: str,
        mode: str = "shell",
        argv=(),
        *,
        session_id: str | None = None,
        expected_run_id: str | None = None,
    ) -> dict:
        if not isinstance(name, str) or not isinstance(mode, str) or mode not in {"shell", "exec"}:
            raise ConsoleError("invalid", "Invalid console request")
        try:
            if mode == "exec":
                argv = validate_exec_argv(argv)
            elif not isinstance(argv, (tuple, list)) or argv:
                raise ValueError
        except (PalimpsestError, TypeError, ValueError):
            raise ConsoleError("invalid", "Invalid guest argv") from None
        session_id = uuid.uuid4().hex if session_id is None else session_id
        if not isinstance(session_id, str) or re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise ConsoleError("invalid", "Invalid console ID")
        with self._lock:
            self._prune_cancelled_opens()
            if session_id in self._sessions:
                raise ConsoleError("invalid", "Console ID is already used")
            if session_id in self._cancelled_opens:
                raise ConsoleError("closed", "Console open was cancelled")
            if self._shutdown.is_set():
                raise ConsoleError("closed", "Console manager is closed")
            if len(self._sessions) + len(self._cancelled_opens) >= self.MAX_SESSIONS:
                raise ConsoleError("limit", "Console session retention limit reached")
            session = None
            current = None
            try:
                access = vm_access.Access.resolve(roots, name, expected_run_id=expected_run_id)
                access.exec_owners = self._exec_owners
                session = access.open(mode, argv)
                current = _Console(session_id, name, session)
                response = {"id": current.id, "name": name, "capabilities": asdict(session.capabilities), "cursor": 0}
                current.thread = threading.Thread(target=self._pump, args=(current,), name="console-pump", daemon=True)
                self._sessions[current.id] = current
                current.thread.start()
                if self._reaper is None:
                    self._reaper = threading.Thread(target=self._expire, name="console-expiry", daemon=True)
                    self._reaper.start()
                return response
            except Exception as exc:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                if current is not None:
                    if current.thread is not None and current.thread.ident is not None:
                        current.thread.join(timeout=3)
                    if current.thread is None or not current.thread.is_alive():
                        self._sessions.pop(current.id, None)
                if isinstance(exc, vm_access.VMAccessError):
                    raise ConsoleError(exc.category, str(exc)) from None
                if isinstance(exc, (RuntimeCapabilityError, ProcessCapabilityError)):
                    raise ConsoleError("capability", "Runtime console mode is unavailable") from None
                raise ConsoleError("transport", "Cannot open runtime console") from None

    def _append(self, current: _Console, event: dict, size: int = 0) -> None:
        with current.condition:
            current.cursor += 1
            event["sequence"] = current.cursor
            current.events.append((event, size))
            current.size += size
            while len(current.events) > self.MAX_EVENTS or current.size > self.MAX_BYTES:
                _, removed = current.events.popleft()
                current.size -= removed
            current.condition.notify_all()

    def _pump(self, current: _Console) -> None:
        status = False
        try:
            for event in current.session.events():
                if isinstance(event, ProcessOutputEvent):
                    data = base64.b64encode(event.data).decode("ascii")
                    self._append(current, {"kind": "output", "stream": event.stream.value, "data": data}, len(data))
                elif isinstance(event, ProcessStatusEvent):
                    self._append(current, {"kind": "exit", **asdict(event.result)})
                    status = True
            if not status:
                result = current.session.wait()
                self._append(current, {"kind": "exit", **asdict(result)})
                status = True
        except Exception:
            self._append(current, {"kind": "error", "message": "Runtime console transport failed"})
        finally:
            try:
                with current.cleanup_lock:
                    current.session.close()
                if not status and current.stopping:
                    result = current.session.wait()
                    self._append(current, {"kind": "exit", **asdict(result)})
            except Exception:
                self._append(current, {"kind": "error", "message": "Runtime console cleanup failed"})
            with current.condition:
                current.closed = True
                current.condition.notify_all()

    def _get(self, session_id: str) -> _Console:
        if not isinstance(session_id, str) or re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise ConsoleError("invalid", "Invalid console ID")
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise ConsoleError("missing", "Console does not exist")
            with current.condition:
                current.touched = time.monotonic()
            return current

    def read(self, session_id: str, after: int = 0, wait_seconds: float = 1) -> dict:
        if (
            type(after) is not int
            or after < 0
            or type(wait_seconds) not in {int, float}
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= self.MAX_WAIT_SECONDS
        ):
            raise ConsoleError("invalid", "Invalid console cursor or wait")
        current = self._get(session_id)
        with current.condition:
            if current.readers >= self.MAX_READERS:
                raise ConsoleError("limit", "Console reader limit reached")
            current.readers += 1
            try:
                deadline = time.monotonic() + wait_seconds
                while True:
                    floor = current.events[0][0]["sequence"] - 1 if current.events else current.cursor
                    if after < floor:
                        raise ConsoleError("cursor-gap", "Console replay cursor expired; reconnect")
                    if after > current.cursor:
                        raise ConsoleError("invalid", "Console cursor is ahead of output")
                    events = [dict(event) for event, _ in current.events if event["sequence"] > after]
                    if events or current.closed or current.stopping or time.monotonic() >= deadline:
                        return {"id": session_id, "events": events, "cursor": current.cursor, "closed": current.closed}
                    current.condition.wait(deadline - time.monotonic())
            finally:
                current.readers -= 1

    def _control(self, session_id: str, capability: str, operation) -> None:
        current = self._get(session_id)
        # Do not hold the condition during a potentially blocked stdin write.
        # close() remains able to cancel that write and unblock the caller.
        if not current.input_lock.acquire(blocking=False):
            raise ConsoleError("limit", "Console control is busy; serialize requests")
        try:
            with current.condition:
                if current.closed or current.stopping:
                    raise ConsoleError("closed", "Console is closed")
            if not getattr(current.session.capabilities, capability):
                raise ConsoleError("capability", "Console capability is unavailable")
            try:
                operation(current.session)
            except ProcessCapabilityError:
                raise ConsoleError("capability", "Console capability is unavailable") from None
            except Exception:
                raise ConsoleError("transport", "Console control failed") from None
        finally:
            current.input_lock.release()

    def write(self, session_id: str, data: bytes) -> None:
        if not isinstance(data, bytes) or len(data) > self.MAX_INPUT_BYTES:
            raise ConsoleError("invalid", "Console input must be bounded bytes")
        self._control(session_id, "stdin", lambda session: session.write_stdin(data))

    def resize(self, session_id: str, rows: int, columns: int) -> None:
        if type(rows) is not int or type(columns) is not int or not 1 <= rows <= 65535 or not 1 <= columns <= 65535:
            raise ConsoleError("invalid", "Invalid terminal dimensions")
        self._control(session_id, "resize", lambda session: session.resize(rows, columns))

    def signal(self, session_id: str, requested: ProcessSignal) -> None:
        if not isinstance(requested, ProcessSignal):
            raise ConsoleError("invalid", "Invalid process signal")
        self._control(session_id, "signal", lambda session: session.signal(requested))

    def _close(self, current: _Console) -> None:
        with current.condition:
            current.stopping = True
            current.condition.notify_all()
        try:
            with current.cleanup_lock:
                current.session.close()
        except Exception:
            raise ConsoleError("transport", "Console cleanup failed") from None
        if current.thread is not None and current.thread is not threading.current_thread():
            current.thread.join(timeout=3)
        if current.thread is not None and current.thread.is_alive():
            raise ConsoleError("transport", "Console consumer did not stop")

    def _prune_cancelled_opens(self) -> None:
        now = time.monotonic()
        for session_id, cancelled in tuple(self._cancelled_opens.items()):
            if now - cancelled >= self.IDLE_SECONDS:
                del self._cancelled_opens[session_id]

    def close(self, session_id: str) -> None:
        if not isinstance(session_id, str) or re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise ConsoleError("invalid", "Invalid console ID")
        with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                # The tab knows its random ID before open's HTTP response. A
                # DELETE arriving first must prevent its queued open, not miss it.
                self._prune_cancelled_opens()
                if not self._shutdown.is_set() and session_id not in self._cancelled_opens:
                    if len(self._sessions) + len(self._cancelled_opens) >= self.MAX_SESSIONS:
                        raise ConsoleError("limit", "Console session retention limit reached")
                    self._cancelled_opens[session_id] = time.monotonic()
                return
        # Keep the final bounded journal readable until idle expiry.
        self._close(current)

    def close_all(self) -> None:
        with self._lock:
            self._shutdown.set()
            sessions = list(self._sessions.values())
        for current in sessions:
            with current.condition:
                current.stopping = True
                current.condition.notify_all()
        failed = False
        for current in sessions:
            try:
                self._close(current)
            except ConsoleError:
                failed = True
        if self._reaper is not None:
            self._reaper.join()
        if failed:
            raise ConsoleError("transport", "Console shutdown cleanup failed")

    def has_active(self) -> bool:
        with self._lock:
            return any(not current.closed for current in self._sessions.values())

    def _expire(self) -> None:
        while not self._shutdown.wait(1):
            with self._lock:
                self._prune_cancelled_opens()
                expired = [
                    current
                    for current in self._sessions.values()
                    if time.monotonic() - current.touched >= self.IDLE_SECONDS
                ]
                for current in expired:
                    try:
                        self._close(current)
                    except ConsoleError:
                        continue  # Retain ownership; retry cleanup, never forget a live pump.
                    self._sessions.pop(current.id, None)
