"""Dispatcher-owned normalization for exact retained-console bytes."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

from . import state
from .runtime_types import (
    ExistingRunRecord,
    LogCursor,
    LogDataEvent,
    LogErrorCategory,
    LogEvent,
    LogMode,
    LogSourceStream,
    LogStreamError,
    LogTerminalCategory,
    LogTerminalEvent,
    LogTerminalOutcome,
)

_CHUNK_SIZE = 64 * 1024
_FOLLOW_POLL_SECONDS = 0.05
_TERMINAL_STATUSES = frozenset({"stopped", "removed", "failed", "exited"})


class RetainedConsoleLogStream:
    """Single-consumer byte stream over one pinned console inode."""

    def __init__(self, source: state.PinnedRunConsole, mode: LogMode) -> None:
        if not isinstance(source, state.PinnedRunConsole):
            raise TypeError("retained console stream requires a pinned source")
        if not isinstance(mode, LogMode):
            raise TypeError("retained console stream requires a LogMode")
        self._source = source
        self._mode = mode
        self._generation = str(uuid.uuid4())
        self._cancelled = threading.Event()
        self._consume_lock = threading.Lock()
        self._consumed = False
        self._iterating = False
        self._closed = False
        self._cursor_position = 0
        self._stream_sequence = 0
        self._last_observed_at: datetime | None = None

    @property
    def record(self) -> ExistingRunRecord:
        return self._source.record

    @property
    def mode(self) -> LogMode:
        return self._mode

    def _observed_at(self) -> datetime:
        observed = datetime.now(UTC)
        if self._last_observed_at is not None and observed < self._last_observed_at:
            observed = self._last_observed_at
        self._last_observed_at = observed
        return observed

    def _cursor(self) -> LogCursor:
        self._cursor_position += 1
        return LogCursor(self.record, self._generation, self._cursor_position)

    def _terminal(
        self,
        category: LogTerminalCategory,
        *,
        error: LogErrorCategory | None = None,
        status: str | None = None,
    ) -> LogTerminalEvent:
        return LogTerminalEvent(
            self._cursor(),
            self._observed_at(),
            LogTerminalOutcome(category, error_category=error, run_status=status),
        )

    def events(self) -> Iterator[LogEvent]:
        with self._consume_lock:
            if self._consumed:
                raise LogStreamError(LogErrorCategory.ALREADY_CONSUMED)
            self._consumed = True
        return self._iterate()

    def _iterate(self) -> Iterator[LogEvent]:
        with self._consume_lock:
            self._iterating = True
        try:
            if self._closed or self._cancelled.is_set():
                yield self._terminal(LogTerminalCategory.CANCELLED)
                return
            while True:
                try:
                    content = self._source.read(_CHUNK_SIZE, snapshot=self._mode is LogMode.SNAPSHOT)
                except LogStreamError as exc:
                    yield self._terminal(LogTerminalCategory.ERROR, error=exc.category)
                    return
                if content:
                    self._stream_sequence += 1
                    yield LogDataEvent(
                        self._cursor(),
                        LogSourceStream.VM_CONSOLE,
                        self._stream_sequence,
                        self._observed_at(),
                        content,
                    )
                    if self._cancelled.is_set():
                        yield self._terminal(LogTerminalCategory.CANCELLED)
                        return
                    continue
                if self._mode is LogMode.SNAPSHOT:
                    try:
                        self._source.current_status()
                    except LogStreamError as exc:
                        yield self._terminal(LogTerminalCategory.ERROR, error=exc.category)
                        return
                    yield self._terminal(LogTerminalCategory.SNAPSHOT_COMPLETE)
                    return
                try:
                    status = self._source.current_status()
                except LogStreamError as exc:
                    yield self._terminal(LogTerminalCategory.ERROR, error=exc.category)
                    return
                if status in _TERMINAL_STATUSES:
                    # Producers publish retained bytes before their terminal
                    # ledger state. Drain once more after observing that state
                    # so an append between the first EOF and status read is not
                    # lost behind the terminal event.
                    try:
                        content = self._source.read(_CHUNK_SIZE)
                    except LogStreamError as exc:
                        yield self._terminal(LogTerminalCategory.ERROR, error=exc.category)
                        return
                    if content:
                        self._stream_sequence += 1
                        yield LogDataEvent(
                            self._cursor(),
                            LogSourceStream.VM_CONSOLE,
                            self._stream_sequence,
                            self._observed_at(),
                            content,
                        )
                        if self._cancelled.is_set():
                            yield self._terminal(LogTerminalCategory.CANCELLED)
                            return
                        continue
                    yield self._terminal(LogTerminalCategory.RUN_TERMINAL, status=status)
                    return
                if self._cancelled.wait(_FOLLOW_POLL_SECONDS):
                    yield self._terminal(LogTerminalCategory.CANCELLED)
                    return
        finally:
            with self._consume_lock:
                self._iterating = False
                self._closed = True
                self._source.close()

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        self._cancelled.set()
        with self._consume_lock:
            if self._closed:
                return
            self._closed = True
            if not self._iterating:
                self._source.close()


def open_retained_console_stream(
    roots: state.StatePaths,
    record: ExistingRunRecord,
    mode: LogMode,
) -> RetainedConsoleLogStream:
    return RetainedConsoleLogStream(state.open_pinned_run_console(roots, record), mode)
