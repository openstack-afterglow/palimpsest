"""One real noninteractive guest exec, never VM-console output or host execution."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, replace

from .errors import StateError
from .oci_exec_control import MAX_EXEC_CHUNK, MAX_EXEC_OUTPUT, MAX_EXEC_SEQUENCE, validate_exec_request
from .oci_exec_record import OCIExecRecordWriter
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


@dataclass(frozen=True, slots=True)
class OCIExecCompletionObservation:
    """Process-local terminal facts retained by the original exec session."""

    terminal: ProcessExit | None
    reason: str
    stdout_bytes: int
    stderr_bytes: int
    acknowledgement: str

    def __post_init__(self) -> None:
        if self.reason not in {"completed", "timeout", "output-limit", "cancelled"}:
            raise ValueError("OCI exec completion observation reason is invalid")
        if self.acknowledgement not in {"unconfirmed", "confirmed"}:
            raise ValueError("OCI exec completion observation acknowledgement is invalid")
        if (
            type(self.stdout_bytes) is not int
            or type(self.stderr_bytes) is not int
            or self.stdout_bytes < 0
            or self.stderr_bytes < 0
            or self.stdout_bytes + self.stderr_bytes > MAX_EXEC_OUTPUT
        ):
            raise ValueError("OCI exec completion observation output bounds are invalid")
        if self.terminal is None:
            if self.reason != "cancelled" or self.stdout_bytes != 0 or self.stderr_bytes != 0:
                raise ValueError("OCI exec completion observation terminal is invalid")
            return
        if type(self.terminal) is not ProcessExit:
            raise TypeError("OCI exec completion observation terminal is invalid")
        if self.terminal.category is ProcessExitCategory.EXITED:
            valid = (
                type(self.terminal.exit_code) is int
                and 0 <= self.terminal.exit_code <= 255
                and self.terminal.signal_number is None
            )
        else:
            valid = (
                self.terminal.category is ProcessExitCategory.SIGNALED
                and self.terminal.exit_code is None
                and type(self.terminal.signal_number) is int
                and 1 <= self.terminal.signal_number <= 64
            )
        if not valid:
            raise ValueError("OCI exec completion observation terminal is invalid")


class OCIExecAcknowledgementError(StateError):
    """The original client observed completion but could not confirm its ACK."""

    def __init__(self, observation: OCIExecCompletionObservation):
        if type(observation) is not OCIExecCompletionObservation:
            raise TypeError("OCI exec acknowledgement error requires an observation")
        self._observation = observation
        if observation.terminal is None:
            status = "cancelled-before-child"
        elif observation.terminal.exit_code is not None:
            status = f"exit-code-{observation.terminal.exit_code}"
        else:
            status = f"signal-{observation.terminal.signal_number}"
        super().__init__(
            f"OCI exec terminal observed (reason={observation.reason}, status={status}) but acknowledgement is "
            "unconfirmed; mailbox occupancy is unknown; preserve the original output and do not rerun because "
            "the delivery outcome is unknown"
        )

    @property
    def observation(self) -> OCIExecCompletionObservation:
        return self._observation


class OCIExecRecordingError(StateError):
    """A fixed local-recording failure with the last trustworthy live facts."""

    _MESSAGES = {
        "pre-ack": "OCI exec completion recording failed before acknowledgement",
        "post-ack": "OCI exec completion recording failed after validated acknowledgement",
    }

    def __init__(self, stage: str, observation: OCIExecCompletionObservation | None) -> None:
        message = self._MESSAGES.get(stage)
        if message is None:
            raise ValueError("invalid OCI exec recording error stage")
        if observation is not None and type(observation) is not OCIExecCompletionObservation:
            raise TypeError("OCI exec recording error requires an immutable observation")
        if stage == "post-ack" and (observation is None or observation.acknowledgement != "confirmed"):
            raise ValueError("post-ACK recording failure requires a confirmed observation")
        if stage == "pre-ack" and observation is not None and observation.acknowledgement != "unconfirmed":
            raise ValueError("pre-ACK recording failure requires an unconfirmed observation")
        self.stage = stage
        self._observation = observation
        super().__init__(message)

    @property
    def observation(self) -> OCIExecCompletionObservation | None:
        return self._observation


def validate_exec_status(value):
    if (
        type(value) is not dict
        or set(value) != {"state", "next_sequence", "occupied"}
        or type(value["state"]) is not str
        or value["state"] not in {"not-ready", "ready", "stopping", "terminal", "control-lost"}
        or type(value["next_sequence"]) is not int
        or not 1 <= value["next_sequence"] <= MAX_EXEC_SEQUENCE
        or type(value["occupied"]) is not bool
    ):
        raise StateError("OCI exec mailbox status is invalid")


def exec_session(name, request, *, roots, _expected_record, _record_writer=None):
    if type(request) is not ExecRequest:
        raise StateError("OCI exec requires literal guest argv")
    argv = validate_exec_request(request.argv, 30000)
    if _record_writer is not None and type(_record_writer) is not OCIExecRecordWriter:
        raise StateError("OCI exec record writer is invalid")
    binding = load_oci_run_binding(roots, name)
    if binding.record != _expected_record:
        raise StateError("OCI exec run identity changed")
    with locked_existing_run(roots, name, expected=binding.record, lock_timeout=5) as mutation:
        endpoint = _read_run_journal(mutation, binding).endpoint
    return OCIExecProcessSession(roots, binding, endpoint, argv, _record_writer=_record_writer)


class OCIExecProcessSession:
    def __init__(self, roots, binding, endpoint, argv, *, timeout_ms=30000, _record_writer=None):
        if _record_writer is not None:
            if type(_record_writer) is not OCIExecRecordWriter:
                raise StateError("OCI exec record writer is invalid")
        self._recording_enabled = _record_writer is not None
        self._record_writer = _record_writer
        self._client = None
        try:
            self._pid = os.getpid()
            self._closed = threading.Event()
            self._consumed = False
            self._result = None
            self._stdout = self._stderr = 0
            self._sizes = (0, 0)
            self._phase = 0
            self._terminal = None
            self._observed_completion = None
            argv = validate_exec_request(argv, timeout_ms)
            self._deadline = _Deadline(timeout_ms / 1000 + 10)
            self._token = str(uuid.uuid4())
            self._client = MonitorClient(roots, binding, endpoint)
            status = self._request("status", {})
            self._validate_status(status)
            self._require_ready(status)
            self._sequence = status["next_sequence"]
            self._decode(self._request("submit", {**self._identity(), "argv": list(argv), "timeout_ms": timeout_ms}))
        except BaseException:
            self.close()
            raise

    @property
    def capabilities(self):
        return ProcessCapabilities(stdin=False, tty=False, resize=False, signal=False)

    @property
    def observed_completion(self) -> OCIExecCompletionObservation | None:
        if os.getpid() != self._pid:
            raise StateError("OCI exec session is closed")
        return self._observed_completion

    def _identity(self):
        return {"sequence": self._sequence, "token": self._token}

    def _request(self, operation, payload):
        if self._closed.is_set() or os.getpid() != self._pid:
            raise StateError("OCI exec session is closed")
        return self._client.exec_request(operation, payload, timeout=min(5.0, self._deadline.remaining(minimum=0.1)))

    @staticmethod
    def _validate_status(value):
        validate_exec_status(value)

    @staticmethod
    def _require_ready(status):
        refusals = {
            "not-ready": "OCI exec is not ready; check the run status and wait for authenticated READY",
            "stopping": "OCI exec is unavailable while the run is stopping; wait for shutdown and inspect existing results",
            "terminal": "OCI exec is unavailable because the run has ended; inspect the run's terminal result",
            "control-lost": (
                "OCI exec control was lost; preserve the run evidence and inspect the original client's result; "
                "do not rerun a command whose outcome is unknown"
            ),
        }
        if status["state"] != "ready":
            raise StateError(refusals[status["state"]])
        if status["occupied"]:
            raise StateError(
                "OCI exec is occupied: a previous command may still be active or its result may be unacknowledged; "
                "let the original client finish consuming its result; result takeover is not supported, "
                "and a command with an unknown outcome must not be rerun"
            )

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
        try:
            while True:
                value = self._request(
                    "poll", {**self._identity(), "stdout_offset": self._stdout, "stderr_offset": self._stderr}
                )
                stdout, stderr = self._decode(value)
                self._stdout += len(stdout)
                self._stderr += len(stderr)
                if stdout:
                    yield ProcessOutputEvent(ProcessStream.STDOUT, stdout)
                    if self._closed.is_set() or os.getpid() != self._pid:
                        raise StateError("OCI exec session is closed")
                if stderr:
                    yield ProcessOutputEvent(ProcessStream.STDERR, stderr)
                    if self._closed.is_set() or os.getpid() != self._pid:
                        raise StateError("OCI exec session is closed")
                if value["state"] == "control-lost":
                    raise StateError("OCI exec control was lost; preserve the run evidence")
                if self._terminal is not None and (self._stdout, self._stderr) == self._sizes:
                    if self._closed.is_set() or os.getpid() != self._pid:
                        raise StateError("OCI exec session is closed")
                    terminal, reason, stdout_bytes, stderr_bytes = self._terminal
                    if terminal is None:
                        result = None
                    else:
                        code, number = terminal["exit_code"], terminal["signal"]
                        category = ProcessExitCategory.EXITED if code is not None else ProcessExitCategory.SIGNALED
                        result = ProcessExit(code if code is not None else -number, code, number, category)
                    self._observed_completion = OCIExecCompletionObservation(
                        result,
                        reason,
                        stdout_bytes,
                        stderr_bytes,
                        "unconfirmed",
                    )
                    recording_error = None
                    if self._record_writer is not None:
                        try:
                            self._record_writer.publish_observed(self._observed_completion)
                        except Exception:
                            recording_error = OCIExecRecordingError("pre-ack", self._observed_completion)
                    if recording_error is not None:
                        self.close()
                        raise recording_error
                    acknowledgement_error = None
                    try:
                        status = self._request("acknowledge", self._identity())
                        self._validate_status(status)
                        if status["next_sequence"] != self._sequence + 1:
                            raise StateError("OCI exec acknowledgement identity changed")
                    except Exception:
                        acknowledgement_error = OCIExecAcknowledgementError(self._observed_completion)
                    if acknowledgement_error is not None:
                        raise acknowledgement_error
                    self._observed_completion = replace(self._observed_completion, acknowledgement="confirmed")
                    recording_error = None
                    if self._record_writer is not None:
                        try:
                            self._record_writer.publish_confirmed(self._observed_completion)
                        except Exception:
                            recording_error = OCIExecRecordingError("post-ack", self._observed_completion)
                    if recording_error is not None:
                        self.close()
                        raise recording_error
                    # All output and completion evidence are now local. Embedded
                    # CLI callers need not rely on process exit to release the pin.
                    self.close()
                    if reason != "completed":
                        raise StateError("OCI exec did not complete: " + reason)
                    assert result is not None
                    self._result = result
                    yield ProcessStatusEvent(self._result)
                    return
                if not stdout and not stderr:
                    time.sleep(min(0.01, self._deadline.remaining()))
        except BaseException:
            # Recording opts this session into eager cleanup on every error
            # exit.  Without that option, preserve the original pinned-client
            # lifetime: failure cleanup remains the caller's responsibility.
            if self._recording_enabled:
                self.close()
            raise

    def wait(self):
        if self._result is not None:
            return self._result
        if self._consumed:
            raise StateError("OCI exec events are still active")
        for _ in self.events():
            pass
        return self._result

    def close(self):
        closed = getattr(self, "_closed", None)
        if closed is not None:
            try:
                closed.set()
            except BaseException:
                pass
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                client.close()
            except BaseException:
                pass
        if self._record_writer is not None:
            writer = self._record_writer
            self._record_writer = None
            try:
                writer.close()
            except BaseException:
                pass

    def write_stdin(self, data):
        raise ProcessCapabilityError("stdin")

    def close_stdin(self):
        raise ProcessCapabilityError("stdin")

    def resize(self, rows, columns):
        raise ProcessCapabilityError("resize")

    def signal(self, requested):
        raise ProcessCapabilityError("signal")
