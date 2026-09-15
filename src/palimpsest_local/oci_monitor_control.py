"""One bounded, secret-free STOP mailbox for a single monitor-owned boot.

Only the lifecycle worker consumes requests. This object never carries a
libvirt stream, protocol session, key, or a caller-supplied guest request ID.
Its lock protects state only and is never held across external callbacks.
"""

from __future__ import annotations

import threading
import time

DEFAULT_MONITOR_STOP_TIMEOUT_SECONDS = 30.0


class MonitorStopControl:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "not-ready"
        self._accepted = False
        self._taken = False
        self._deadline: float | None = None

    def request(self) -> str:
        with self._lock:
            if self._state == "terminal":
                return "stop-terminal"
            if self._state == "control-lost":
                return "control-lost"
            if self._accepted:
                return "stop-accepted"
            if self._state != "ready":
                return "stop-refused"
            self._accepted = True
            self._deadline = time.monotonic() + DEFAULT_MONITOR_STOP_TIMEOUT_SECONDS
            return "stop-accepted"

    @property
    def accepted(self) -> bool:
        with self._lock:
            return self._accepted

    @property
    def deadline(self) -> float | None:
        with self._lock:
            return self._deadline

    def mark_ready(self) -> None:
        with self._lock:
            if self._state == "not-ready":
                self._state = "ready"

    def take_stop(self) -> bool:
        with self._lock:
            if self._state != "ready" or not self._accepted or self._taken:
                return False
            self._taken = True
            return True

    def mark_observed_terminal(self) -> None:
        with self._lock:
            if self._state == "ready":
                self._state = "observed-terminal"

    def mark_terminal(self) -> None:
        with self._lock:
            if self._state == "observed-terminal":
                self._state = "terminal"

    def mark_control_lost(self) -> None:
        with self._lock:
            if self._state != "terminal":
                self._state = "control-lost"
