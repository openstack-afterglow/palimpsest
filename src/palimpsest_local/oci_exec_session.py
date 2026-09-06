"""One real noninteractive guest exec, never VM-console output or host execution."""

from __future__ import annotations

import os
import threading
import time
import uuid

from .errors import StateError
from .oci_exec_control import MAX_EXEC_CHUNK, MAX_EXEC_OUTPUT, MAX_EXEC_SEQUENCE, validate_exec_request
from .oci_monitor_client import MonitorClient, _Deadline
from .oci_run_cleanup import _read_run_journal, load_oci_run_binding
from .runtime_types import (
    ExecRequest,
    ProcessCapabilities,
    ProcessCapabilityError,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessStatusEvent,
    ProcessStream,
)
from .state import locked_existing_run


def exec_session(name, request, *, roots, _expected_record):
    if type(request) is not ExecRequest:
        raise StateError("OCI exec requires literal guest argv")
    binding = load_oci_run_binding(roots, name)
    if binding.record != _expected_record:
        raise StateError("OCI exec run identity changed")
    with locked_existing_run(roots, name, expected=binding.record, lock_timeout=5) as mutation:
        endpoint = _read_run_journal(mutation, binding).endpoint
    return OCIExecProcessSession(roots, binding, endpoint, request.argv)


class OCIExecProcessSession:
    def __init__(self, roots, binding, endpoint, argv, *, timeout_ms=30000):
        argv = validate_exec_request(argv, timeout_ms)
        self._pid = os.getpid()
        self._closed = threading.Event()
        self._consumed = False
        self._result = None
        self._client = None
        self._stdout = self._stderr = 0
        self._sizes = (0, 0)
        self._phase = 0
        self._terminal = None
        self._deadline = _Deadline(timeout_ms / 1000 + 10)
        self._token = str(uuid.uuid4())
        try:
            self._client = MonitorClient(roots, binding, endpoint)
            status = self._request("status", {})
            self._validate_status(status)
            if status["state"] != "ready" or status["occupied"]:
                raise StateError("OCI exec is not ready or another result is still owned")
            self._sequence = status["next_sequence"]
            self._decode(self._request("submit", {**self._identity(), "argv": list(argv), "timeout_ms": timeout_ms}))
        except BaseException:
            self.close()
            raise

    @property
    def capabilities(self):
        return ProcessCapabilities(stdin=False, tty=False, resize=False, signal=False)

    def _identity(self):
        return {"sequence": self._sequence, "token": self._token}

    def _request(self, operation, payload):
        if self._closed.is_set() or os.getpid() != self._pid:
            raise StateError("OCI exec session is closed")
        return self._client.exec_request(operation, payload, timeout=min(5.0, self._deadline.remaining(minimum=0.1)))

    @staticmethod
    def _validate_status(value):
        if (
            type(value) is not dict
            or set(value) != {"state", "next_sequence", "occupied"}
            or value["state"] not in {"not-ready", "ready", "stopping", "terminal", "control-lost"}
            or type(value["next_sequence"]) is not int
            or not 1 <= value["next_sequence"] <= MAX_EXEC_SEQUENCE
            or type(value["occupied"]) is not bool
        ):
            raise StateError("OCI exec mailbox status is invalid")

    def _decode(self, value):
        expected = {
            "sequence",
            "token",
            "state",
            "stdout_offset",
            "stderr_offset",
            "stdout_hex",
            "stderr_hex",
            "stdout_size",
            "stderr_size",
            "terminal",
            "reason",
        }
        phases = {"queued": 0, "running": 1, "completed": 2, "control-lost": 3}
        if (
            type(value) is not dict
            or set(value) != expected
            or value["sequence"] != self._sequence
            or type(value["sequence"]) is not int
            or value["token"] != self._token
            or type(value["state"]) is not str
            or value["state"] not in phases
            or phases[value["state"]] < self._phase
        ):
            raise StateError("OCI exec result identity changed")
        output = []
        for index, (stream, offset) in enumerate((("stdout", self._stdout), ("stderr", self._stderr))):
            encoded, size = value[stream + "_hex"], value[stream + "_size"]
            if (
                type(size) is not int
                or not self._sizes[index] <= size <= MAX_EXEC_OUTPUT
                or type(value[stream + "_offset"]) is not int
                or value[stream + "_offset"] != offset
                or type(encoded) is not str
                or len(encoded) > MAX_EXEC_CHUNK * 2
            ):
                raise StateError("OCI exec output bounds changed")
            try:
                data = bytes.fromhex(encoded)
            except ValueError:
                raise StateError("OCI exec output encoding is invalid") from None
            if data.hex() != encoded or len(data) != min(MAX_EXEC_CHUNK, size - offset):
                raise StateError("OCI exec output offset changed")
            output.append(data)
        if value["stdout_size"] + value["stderr_size"] > MAX_EXEC_OUTPUT:
            raise StateError("OCI exec output limit exceeded")
        terminal = value["terminal"]
        if value["state"] == "completed":
            if terminal is None:
                if value["reason"] != "cancelled" or value["stdout_size"] != 0 or value["stderr_size"] != 0:
                    raise StateError("OCI exec terminal is invalid")
            else:
                if type(terminal) is not dict or set(terminal) != {"exit_code", "signal"}:
                    raise StateError("OCI exec terminal is invalid")
                code, number = terminal["exit_code"], terminal["signal"]
                if not (
                    (type(code) is int and 0 <= code <= 255 and number is None)
                    or (code is None and type(number) is int and 1 <= number <= 64)
                ):
                    raise StateError("OCI exec terminal is invalid")
            if value["reason"] not in {"completed", "timeout", "output-limit", "cancelled"}:
                raise StateError("OCI exec completion reason is invalid")
            evidence = (
                None if terminal is None else dict(terminal),
                value["reason"],
                value["stdout_size"],
                value["stderr_size"],
            )
            if self._terminal is not None and evidence != self._terminal:
                raise StateError("OCI exec terminal changed")
            self._terminal = evidence
        elif terminal is not None or value["reason"] not in (
            {"control-lost"} if value["state"] == "control-lost" else {None}
        ):
            raise StateError("OCI exec pending result is invalid")
        self._phase = phases[value["state"]]
        self._sizes = value["stdout_size"], value["stderr_size"]
        return output

    def events(self):
        if self._consumed or self._closed.is_set() or os.getpid() != self._pid:
            raise StateError("OCI exec session is closed or already consumed")
        self._consumed = True
        return self._iterate()

    def _iterate(self):
        while True:
            value = self._request(
                "poll", {**self._identity(), "stdout_offset": self._stdout, "stderr_offset": self._stderr}
            )
            stdout, stderr = self._decode(value)
            self._stdout += len(stdout)
            self._stderr += len(stderr)
            if stdout:
                yield ProcessOutputEvent(ProcessStream.STDOUT, stdout)
            if stderr:
                yield ProcessOutputEvent(ProcessStream.STDERR, stderr)
            if value["state"] == "control-lost":
                raise StateError("OCI exec control was lost; preserve the run evidence")
            if self._terminal is not None and (self._stdout, self._stderr) == self._sizes:
                terminal, reason, *_ = self._terminal
                status = self._request("acknowledge", self._identity())
                self._validate_status(status)
                if status["next_sequence"] != self._sequence + 1:
                    raise StateError("OCI exec acknowledgement identity changed")
                if reason != "completed":
                    raise StateError("OCI exec did not complete: " + reason)
                code, number = terminal["exit_code"], terminal["signal"]
                category = ProcessExitCategory.EXITED if code is not None else ProcessExitCategory.SIGNALED
                self._result = ProcessExit(code if code is not None else -number, code, number, category)
                yield ProcessStatusEvent(self._result)
                return
            if not stdout and not stderr:
                time.sleep(min(0.01, self._deadline.remaining()))

    def wait(self):
        if self._result is not None:
            return self._result
        if self._consumed:
            raise StateError("OCI exec events are still active")
        for _ in self.events():
            pass
        return self._result

    def close(self):
        self._closed.set()
        if self._client is not None:
            self._client.close()
            self._client = None

    def write_stdin(self, data):
        raise ProcessCapabilityError("stdin")

    def close_stdin(self):
        raise ProcessCapabilityError("stdin")

    def resize(self, rows, columns):
        raise ProcessCapabilityError("resize")

    def signal(self, requested):
        raise ProcessCapabilityError("signal")
