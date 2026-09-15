"""Bounded, secret-free single-job mailbox for the monitor lifecycle worker.

Monotonic sequences make old submissions permanently stale after ACK, without
keeping an unbounded UUID/result history. No method performs guest I/O.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from .errors import StateError
from .oci_control_protocol_v2 import OCIControlProtocolV2Error, validate_exec_argv, validate_exec_timeout

MAX_EXEC_OUTPUT = 65536
MAX_EXEC_CHUNK = 1024
MAX_EXEC_SEQUENCE = 2**63 - 1


class MonitorExecControlError(StateError):
    def __init__(self, category):
        self.category = category
        super().__init__("OCI exec " + category)


def _fail(category):
    raise MonitorExecControlError(category)


def _identity(sequence, token):
    if type(sequence) is not int or not 1 <= sequence <= MAX_EXEC_SEQUENCE:
        _fail("invalid-request")
    try:
        if type(token) is not str or str(uuid.UUID(token)) != token:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail("invalid-request")


def validate_exec_request(argv, timeout_ms):
    try:
        result = validate_exec_argv(argv)
        validate_exec_timeout(timeout_ms)
    except OCIControlProtocolV2Error:
        _fail("invalid-request")
    return result


@dataclass(frozen=True, slots=True)
class MonitorExecJob:
    sequence: int
    token: str
    argv: tuple[str, ...]
    timeout_ms: int


class MonitorExecControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = "not-ready"
        self._next = 1
        self._job = None
        self._job_state = None
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._terminal = None
        self._reason = None
        self._last_ack = None

    def _status(self):
        return {"state": self._state, "next_sequence": self._next, "occupied": self._job is not None}

    def status(self):
        with self._lock:
            return self._status()

    def mark_ready(self):
        with self._lock:
            if self._state == "not-ready":
                self._state = "ready"

    def close_to_exec(self, reason):
        if reason not in {"stopping", "terminal", "control-lost"}:
            _fail("invalid-state")
        with self._lock:
            if self._state not in {"terminal", "control-lost"}:
                self._state = reason
            if reason in {"terminal", "control-lost"} and self._job_state in {"queued", "running"}:
                self._job_state = "control-lost"
                self._reason = "control-lost"

    def submit(self, sequence, token, argv, timeout_ms):
        _identity(sequence, token)
        argv = validate_exec_request(argv, timeout_ms)
        candidate = MonitorExecJob(sequence, token, argv, timeout_ms)
        with self._lock:
            if sequence != self._next:
                _fail("stale-sequence")
            if self._job is not None:
                if self._job != candidate:
                    _fail("busy")
                return self._poll(0, 0)
            if self._state != "ready" or self._next == MAX_EXEC_SEQUENCE:
                _fail("not-ready")
            self._job = candidate
            self._job_state = "queued"
            return self._poll(0, 0)

    def _require(self, sequence, token):
        if self._job is None or (self._job.sequence, self._job.token) != (sequence, token):
            _fail("unknown-job")

    def _poll(self, stdout_offset, stderr_offset):
        for offset, output in ((stdout_offset, self._stdout), (stderr_offset, self._stderr)):
            if type(offset) is not int or not 0 <= offset <= len(output):
                _fail("invalid-offset")
        return {
            "sequence": self._job.sequence,
            "token": self._job.token,
            "state": self._job_state,
            "stdout_offset": stdout_offset,
            "stderr_offset": stderr_offset,
            "stdout_hex": bytes(self._stdout[stdout_offset : stdout_offset + MAX_EXEC_CHUNK]).hex(),
            "stderr_hex": bytes(self._stderr[stderr_offset : stderr_offset + MAX_EXEC_CHUNK]).hex(),
            "stdout_size": len(self._stdout),
            "stderr_size": len(self._stderr),
            "terminal": None if self._terminal is None else dict(self._terminal),
            "reason": self._reason,
        }

    def poll(self, sequence, token, stdout_offset=0, stderr_offset=0):
        _identity(sequence, token)
        with self._lock:
            self._require(sequence, token)
            return self._poll(stdout_offset, stderr_offset)

    def acknowledge(self, sequence, token):
        _identity(sequence, token)
        with self._lock:
            if self._last_ack == (sequence, token):
                return self._status()
            self._require(sequence, token)
            if self._job_state != "completed":
                _fail("not-completed")
            self._last_ack = (sequence, token)
            self._next += 1
            self._job = self._job_state = self._terminal = self._reason = None
            self._stdout.clear()
            self._stderr.clear()
            return self._status()

    def take_exec(self):
        with self._lock:
            if self._state != "ready" or self._job_state != "queued":
                return None
            self._job_state = "running"
            return self._job

    def append_output(self, job, stream, offset, data):
        if type(job) is not MonitorExecJob or stream not in {"stdout", "stderr"} or type(data) is not bytes:
            _fail("invalid-output")
        with self._lock:
            if self._job != job or self._job_state != "running":
                _fail("invalid-output")
            target = self._stdout if stream == "stdout" else self._stderr
            if (
                type(offset) is not int
                or offset != len(target)
                or not 1 <= len(data) <= MAX_EXEC_CHUNK
                or len(self._stdout) + len(self._stderr) + len(data) > MAX_EXEC_OUTPUT
            ):
                _fail("invalid-output")
            target.extend(data)

    def complete(self, job, terminal, stdout_bytes, stderr_bytes, reason):
        if type(job) is not MonitorExecJob or reason not in {"completed", "timeout", "output-limit", "cancelled"}:
            _fail("invalid-result")
        if terminal is None:
            if reason != "cancelled" or stdout_bytes != 0 or stderr_bytes != 0:
                _fail("invalid-result")
        else:
            if type(terminal) is not dict or set(terminal) != {"exit_code", "signal"}:
                _fail("invalid-result")
            exited, signaled = terminal["exit_code"], terminal["signal"]
            if not (
                (type(exited) is int and 0 <= exited <= 255 and signaled is None)
                or (exited is None and type(signaled) is int and 1 <= signaled <= 64)
            ):
                _fail("invalid-result")
        with self._lock:
            if (
                self._job != job
                or self._job_state != "running"
                or type(stdout_bytes) is not int
                or stdout_bytes != len(self._stdout)
                or type(stderr_bytes) is not int
                or stderr_bytes != len(self._stderr)
            ):
                _fail("invalid-result")
            self._terminal = None if terminal is None else dict(terminal)
            self._reason = reason
            self._job_state = "completed"
