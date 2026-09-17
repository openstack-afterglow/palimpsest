"""Noninteractive console session for one authenticated OCI monitor.

Output is the combined VM console (including boot diagnostics), not separate
guest stdout/stderr or additional exec. INT/TERM request the guest lifecycle's
coalesced STOP. Closing this local reader never stops or retires the monitor.
"""

from __future__ import annotations

import os
import threading

from .errors import StateError
from .oci_monitor_client import MonitorClient
from .oci_runtime_io import runtime_io_guard
from .runtime_types import (
    ProcessCapabilities,
    ProcessCapabilityError,
    ProcessOutputEvent,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
)
from .state import locked_existing_run


class OCIMonitorProcessSession:
    """Single-consumer foreground events with a receipt-pinned console cursor."""

    def __init__(self, roots, binding, endpoint):
        self._roots = roots
        self._binding = binding
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._stop_requested = False
        self._stop_sent = False
        self._consumed = False
        self._result = None
        self._offset = 0
        self._receipt = None
        self._client = MonitorClient(roots, binding, endpoint, timeout=5.0)
        try:
            # Pin the receipt before returning, without consuming any bytes.
            with locked_existing_run(roots, binding.record.name, lock_timeout=5.0) as mutation:
                if mutation.record != binding.record:
                    raise StateError("OCI console session run binding changed")
                with runtime_io_guard(mutation, plan_digest=binding.plan_digest) as guard:
                    self._receipt = guard.receipt
        except BaseException:
            self._client.close()
            raise

    @property
    def capabilities(self):
        return ProcessCapabilities(stdin=False, tty=False, resize=False, signal=True)

    def _require_open(self):
        if self._closed.is_set() or os.getpid() != self._pid:
            raise StateError("OCI console session is closed")

    def _read(self):
        # Release the run lock before any monitor IPC, yield or sleep. The
        # monitor must be able to publish READY/TERMINAL while we follow output.
        with locked_existing_run(self._roots, self._binding.record.name, lock_timeout=5.0) as mutation:
            if mutation.record != self._binding.record:
                raise StateError("OCI console session run binding changed")
            with runtime_io_guard(mutation, plan_digest=self._binding.plan_digest) as guard:
                if guard.receipt != self._receipt:
                    raise StateError("OCI console session receipt changed")
                content = guard.read_console(self._offset)
        self._offset += len(content)
        return content

    def events(self):
        with self._lock:
            self._require_open()
            if self._consumed:
                raise StateError("OCI console session already consumed")
            self._consumed = True
        return self._iterate()

    def _iterate(self):
        while True:
            with self._lock:
                self._require_open()
                if self._stop_requested and not self._stop_sent:
                    self._client.request_stop(timeout=5.0)
                    self._stop_sent = True
                # Poll even while output is busy, bounding control-loss latency
                # independently of the amount of untrusted console output.
                observed = self._client.poll(timeout=5.0)
                content = self._read()
                if not content and observed.terminal is not None:
                    self._result = observed.terminal
            if content:
                yield ProcessOutputEvent(ProcessStream.STDOUT, content)
            elif observed.terminal is not None:
                yield ProcessStatusEvent(observed.terminal)
                return
            else:
                self._closed.wait(0.05)

    def wait(self):
        with self._lock:
            if self._result is not None:
                return self._result
            self._require_open()
            if self._consumed:
                raise StateError("OCI console session events are still active")
        for _event in self.events():
            pass
        return self._result

    def signal(self, requested):
        if not isinstance(requested, ProcessSignal) or requested not in {
            ProcessSignal.INTERRUPT,
            ProcessSignal.TERMINATE,
        }:
            raise ProcessCapabilityError("signal")
        # CLI handlers can reenter this method while the same thread holds a
        # run flock inside poll/read. Only enqueue here; the event consumer
        # sends STOP after that critical section has released all run locks.
        self._require_open()
        self._stop_requested = True

    def write_stdin(self, data):
        raise ProcessCapabilityError("stdin")

    def close_stdin(self):
        raise ProcessCapabilityError("stdin")

    def resize(self, rows, columns):
        raise ProcessCapabilityError("resize")

    def close(self):
        self._closed.set()
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
