"""Dashboard foreground attachment with a retained non-daemon process owner.

Access.open wraps canonical OCI exec sessions without changing their deadlines.
The worker starts at construction and continues draining after foreground close;
only its finally block closes the underlying session. Probe timers and drawer
disconnects detach the foreground, not the native SSH process or mailbox owner.
The dashboard registry stops admission and waits through original cleanup.

Completion protocols are explicit. requires_ack=True is the strict default for
KVM mailboxes; missing/unknown acknowledgement remains unconfirmed. The trusted
native Lima-VZ route selects False: its process lifetime is retained, but it has
no mailbox ACK to read, synthesize, confirm or count as unconfirmed.

The handoff retains at most 128 output events and 64 KiB of output. Overflow fails
and detaches instead of silently dropping successful output. Closed foregrounds
retain no queued data; unknown outcomes never authorize command replay.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator

from .errors import StateError
from .oci_exec_control import MAX_EXEC_OUTPUT
from .oci_exec_session import OCIExecCompletionObservation
from .runtime_types import (
    ProcessCapabilities,
    ProcessCapabilityError,
    ProcessEvent,
    ProcessExit,
    ProcessOutputEvent,
    ProcessSession,
    ProcessSignal,
    ProcessStatusEvent,
)

_MAX_HANDOFF_EVENTS = 128
_MAX_HANDOFF_BYTES = MAX_EXEC_OUTPUT
_CLOSED = "Dashboard OCI exec consumer is closed; the original result remains owned"
_FAILED = (
    "Dashboard OCI exec transport failed; command outcome or acknowledgement is unknown; "
    "preserve the original evidence and do not rerun"
)
_PROCESS_FAILED = (
    "Dashboard exec transport failed; command outcome is unknown; preserve the original evidence and do not rerun"
)
_ACK_UNKNOWN = (
    "Dashboard OCI exec terminal was observed but acknowledgement is unconfirmed; "
    "mailbox occupancy is unknown; preserve the original evidence and do not rerun"
)
_OUTPUT_LIMIT = (
    "Dashboard OCI exec consumer exceeded its output handoff limit; the original owner continues draining the result"
)


class ExecOwners:
    """Bounded per-dashboard lifetime for consoles and detached probe owners."""

    def __init__(self, *, limit: int = 32) -> None:
        self._condition = threading.Condition()
        self._limit = limit
        self._closing = False
        self._opening = 0
        self._active: set[RetainedExecSession] = set()
        self._unconfirmed = 0

    def open(self, factory: Callable[[], ProcessSession], *, requires_ack: bool = True) -> RetainedExecSession:
        if type(requires_ack) is not bool:
            raise TypeError("requires_ack must be a bool")
        # Reserve before submission: shutdown cannot miss an admitted command.
        with self._condition:
            if self._closing:
                raise StateError("Dashboard OCI exec admission is closed")
            if self._opening + len(self._active) >= self._limit:
                raise StateError("Dashboard OCI exec owner limit reached")
            self._opening += 1
        try:
            return RetainedExecSession(factory(), owner=self, requires_ack=requires_ack)
        finally:
            with self._condition:
                self._opening -= 1
                self._condition.notify_all()

    def _attach(self, session: RetainedExecSession) -> None:
        with self._condition:
            self._active.add(session)

    def _finished(self, session: RetainedExecSession) -> None:
        requires_ack = session.requires_ack
        confirmed = False
        try:
            if requires_ack:
                observation = session.observed_completion
                confirmed = observation is not None and observation.acknowledgement == "confirmed"
        except BaseException:
            pass
        finally:
            with self._condition:
                if requires_ack and not confirmed:
                    self._unconfirmed += 1
                self._active.discard(session)
                self._condition.notify_all()

    def stop_admitting(self) -> None:
        with self._condition:
            self._closing = True

    def wait(self) -> int:
        """Return the unresolved ACK count without claiming a free mailbox."""
        with self._condition:
            self._condition.wait_for(lambda: not self._opening and not self._active)
            return self._unconfirmed


class RetainedExecSession:
    """One foreground consumer, independent of the continuously running OCI owner.

    close() wakes a blocked events() consumer and ends that iterator cleanly, so
    the probe collector can report its own timeout/output cap. wait() returns the
    same result after normal completion, including after close(); a close before
    completion instead leaves wait() permanently failed with a fixed safe error.
    Transport, ACK and command-limit failures never synthesize successful exits.
    """

    def __init__(self, session: ProcessSession, *, owner: ExecOwners | None = None, requires_ack: bool = True) -> None:
        if type(requires_ack) is not bool:
            raise TypeError("requires_ack must be a bool")
        self._session = session
        self._owner = owner
        self._requires_ack = requires_ack
        self._condition = threading.Condition()
        self._output: deque[ProcessOutputEvent] = deque()
        self._output_bytes = 0
        self._consumed = False
        self._closed = False
        self._done = False
        self._result: ProcessExit | None = None
        self._error: str | None = None
        self._worker = threading.Thread(target=self._drain, name="dashboard-oci-exec", daemon=False)
        if owner is not None:
            owner._attach(self)
        try:
            self._worker.start()
        except BaseException:
            try:
                session.close()
            finally:
                if owner is not None:
                    owner._finished(self)
            raise

    @property
    def capabilities(self) -> ProcessCapabilities:
        return ProcessCapabilities(stdin=False, tty=False, resize=False, signal=False)

    @property
    def requires_ack(self) -> bool:
        """Whether this transport owns a mailbox whose ACK must be accounted."""
        return self._requires_ack

    @property
    def observed_completion(self) -> OCIExecCompletionObservation | None:
        if not self._requires_ack:
            return None
        return self._session.observed_completion

    def _offer(self, event: ProcessOutputEvent) -> None:
        with self._condition:
            if self._closed or self._error is not None:
                return
            if len(self._output) >= _MAX_HANDOFF_EVENTS or self._output_bytes + len(event.data) > _MAX_HANDOFF_BYTES:
                self._output.clear()
                self._output_bytes = 0
                self._error = _OUTPUT_LIMIT
            else:
                self._output.append(event)
                self._output_bytes += len(event.data)
            self._condition.notify_all()

    def _failure_message(self) -> str:
        # Only immutable, core-validated facts may inform the client message.
        # Never retain a raw exception (or its traceback and transport secrets).
        if not self._requires_ack:
            return _PROCESS_FAILED
        try:
            observation = self.observed_completion
        except BaseException:
            return _FAILED
        if observation is not None:
            if observation.acknowledgement != "confirmed":
                return _ACK_UNKNOWN
            if observation.reason in {"timeout", "output-limit", "cancelled"}:
                return "Dashboard OCI exec did not complete: " + observation.reason
        return _FAILED

    def _drain(self) -> None:
        result = None
        error = None
        try:
            for event in self._session.events():
                if isinstance(event, ProcessOutputEvent):
                    self._offer(event)
                elif isinstance(event, ProcessStatusEvent):
                    result = event.result
            if result is None:
                error = _FAILED
        except BaseException:
            error = self._failure_message()
        finally:
            try:
                self._session.close()
            except BaseException:
                error = _FAILED
            with self._condition:
                if not self._closed and self._error is None:
                    self._error = error
                    if error is None:
                        self._result = result
                self._done = True
                self._condition.notify_all()
            if self._owner is not None:
                self._owner._finished(self)

    def events(self) -> Iterator[ProcessEvent]:
        with self._condition:
            if self._consumed:
                raise StateError("Dashboard OCI exec events are already consumed")
            self._consumed = True
        return self._iterate()

    def _iterate(self) -> Iterator[ProcessEvent]:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._closed or self._output or self._done or self._error is not None
                    )
                    if self._closed:
                        return
                    if self._output:
                        event = self._output.popleft()
                        self._output_bytes -= len(event.data)
                    elif self._error is not None:
                        raise StateError(self._error)
                    else:
                        assert self._done and self._result is not None
                        event = ProcessStatusEvent(self._result)
                yield event
                if isinstance(event, ProcessStatusEvent):
                    return
        finally:
            # Explicit iterator.close() also detaches, without closing the owner.
            self.close()

    def wait(self) -> ProcessExit:
        with self._condition:
            self._condition.wait_for(lambda: self._closed or self._done or self._error is not None)
            if self._error is not None:
                raise StateError(self._error)
            if self._result is not None:
                return self._result
            raise StateError(_CLOSED)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._output.clear()
            self._output_bytes = 0
            self._condition.notify_all()

    def write_stdin(self, data: bytes) -> None:
        raise ProcessCapabilityError("stdin")

    def close_stdin(self) -> None:
        raise ProcessCapabilityError("stdin")

    def resize(self, rows: int, columns: int) -> None:
        raise ProcessCapabilityError("resize")

    def signal(self, requested: ProcessSignal) -> None:
        raise ProcessCapabilityError("signal")
