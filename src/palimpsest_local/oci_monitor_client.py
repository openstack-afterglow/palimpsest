"""Pinned public-lifecycle observations of an independently owned OCI monitor.

Acceptance is not READY, and a terminal journal is not completed-worker proof.
This client never adopts a writer, shuts down its socket, or cleans up a VM.
"""

from __future__ import annotations

import math
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import oci_monitor_ipc as ipc
from .errors import StateError
from .oci_monitor_recovery import _validate_ledger
from .runtime_types import ProcessExit, ProcessExitCategory
from .state import StatePaths, locked_existing_run

_ORDER = {phase: number for number, phase in enumerate(("committed", "activating", "active", "ready", "terminal"))}


class MonitorClientError(StateError):
    """Path-free failure; uncertain execution and its evidence remain owned."""


@dataclass(frozen=True, slots=True)
class MonitorObservation:
    phase: str
    terminal: ProcessExit | None = None


class _Deadline:
    def __init__(self, timeout: float | None, *, unbounded: bool = False):
        if timeout is None and unbounded:
            self.end = None
        elif type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0.1 <= timeout <= 3600:
            raise MonitorClientError("OCI monitor client timeout is invalid")
        else:
            self.end = time.monotonic() + timeout

    def remaining(self, *, minimum: float = 0.0) -> float:
        value = 5.0 if self.end is None else self.end - time.monotonic()
        if value <= minimum:
            raise MonitorClientError("OCI monitor client timed out; preserve the run evidence")
        return value


@contextmanager
def _stable_errors():
    try:
        yield
    except MonitorClientError:
        raise
    except ipc.MonitorIPCError as exc:
        if exc.category is ipc.MonitorIPCErrorCategory.TIMEOUT:
            raise MonitorClientError("OCI monitor client timed out; preserve the run evidence") from None
        raise MonitorClientError("OCI monitor client authority or control is unavailable") from None
    except StateError as exc:
        if str(exc) == "run lock timed out":
            raise MonitorClientError("OCI monitor client timed out; preserve the run evidence") from None
        raise MonitorClientError("OCI monitor client run evidence is invalid") from None
    except (OSError, ValueError, TypeError):
        raise MonitorClientError("OCI monitor client run evidence is invalid") from None


class MonitorClient:
    """One exact run, generation, writer and pinned private directory.

    A client is process-local and must be closed, preferably with ``with``.
    Polling never holds a run lock across an IPC request: the launch worker
    needs that same lock to publish READY and authenticated terminal receipts.
    """

    def __init__(
        self,
        roots: StatePaths,
        binding: ipc.MonitorPreActivationBinding,
        endpoint: ipc.MonitorExecEndpoint,
        *,
        timeout: float = 5.0,
    ):
        self._fd = -1
        self._pid = os.getpid()
        self._previous = None
        self._lifecycle_identity = None
        self._terminal_result = None
        self._roots, self._binding, self._endpoint = roots, binding, endpoint
        deadline = _Deadline(timeout)
        try:
            with _stable_errors():
                if type(roots) is not StatePaths or type(binding) is not ipc.MonitorPreActivationBinding:
                    raise MonitorClientError("OCI monitor client identity is invalid")
                binding.__post_init__()
                if type(endpoint) is not ipc.MonitorExecEndpoint or endpoint.identity.binding != binding:
                    raise MonitorClientError("OCI monitor client identity is invalid")
                endpoint.__post_init__()
                with locked_existing_run(
                    roots, binding.record.name, expected=binding.record, lock_timeout=deadline.remaining()
                ) as mutation:
                    self._fd = os.open(
                        "monitor-private",
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=mutation._run_fd,
                    )
                    self._directory_identity = self._identity(os.fstat(self._fd))
                    self._run_identity = self._identity(os.fstat(mutation._run_fd))
                    self._read_locked(mutation)
                deadline.remaining()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self) -> None:
        descriptor, self._fd = self._fd, -1
        if descriptor >= 0:
            os.close(descriptor)

    def _verify(self, mutation) -> None:
        if self._fd < 0 or os.getpid() != self._pid:
            raise MonitorClientError("OCI monitor client authority is closed or changed")
        mutation.verify_binding()
        opened = os.fstat(self._fd)
        visible = os.stat("monitor-private", dir_fd=mutation._run_fd, follow_symlinks=False)
        for info in (opened, visible):
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self._binding.owner_uid
                or stat.S_IMODE(info.st_mode) != 0o700
                or self._identity(info) != self._directory_identity
            ):
                raise MonitorClientError("OCI monitor client private directory changed")
        if self._identity(os.fstat(mutation._run_fd)) != self._run_identity:
            raise MonitorClientError("OCI monitor client run directory changed")

    def _read_locked(self, mutation):
        self._verify(mutation)
        loaded = ipc._read_preactivation_journal(self._fd, self._endpoint.identity)
        assert loaded is not None
        journal = loaded[0]
        if journal.endpoint != self._endpoint or journal.phase not in _ORDER:
            raise MonitorClientError("OCI monitor client authority or control is unavailable")
        if self._previous is not None:
            before, before_bytes = self._previous
            if (
                journal.nonce_digest != before.nonce_digest
                or journal.revision - before.revision != _ORDER[journal.phase] - _ORDER[before.phase]
                or _ORDER[journal.phase] < _ORDER[before.phase]
                or (before.active_binding is not None and journal.active_binding != before.active_binding)
                or (journal.revision == before.revision and loaded[1] != before_bytes)
            ):
                raise MonitorClientError("OCI monitor client journal progression is invalid")
        terminal = None
        if journal.phase in {"ready", "terminal"}:
            _validate_ledger(mutation, self._binding, journal)
            lifecycle = mutation.mutable_state()["oci_root_handoff"]["lifecycle"]
            identity = lifecycle["boot_generation"], lifecycle["key_id"]
            if self._lifecycle_identity is not None and self._lifecycle_identity != identity:
                raise MonitorClientError("OCI monitor client lifecycle identity changed")
            self._lifecycle_identity = identity
            if journal.phase == "terminal":
                result = lifecycle["terminal"]
                terminal = ProcessExit(
                    result["returncode"],
                    result["exit_code"],
                    result["signal_number"],
                    ProcessExitCategory(result["category"]),
                )
                if self._terminal_result is not None and terminal != self._terminal_result:
                    raise MonitorClientError("OCI monitor client terminal evidence changed")
                self._terminal_result = terminal
        self._verify(mutation)
        self._previous = loaded
        return journal, terminal

    def _read(self, deadline):
        with locked_existing_run(
            self._roots, self._binding.record.name, expected=self._binding.record, lock_timeout=deadline.remaining()
        ) as mutation:
            result = self._read_locked(mutation)
        deadline.remaining()
        return result

    def _request(self, operation, deadline):
        result = ipc.request_monitor(
            self._fd, self._endpoint, operation, timeout=min(5.0, deadline.remaining(minimum=0.1))
        )
        deadline.remaining()
        return result

    def _poll(self, deadline) -> MonitorObservation:
        before, _ = self._read(deadline)
        reply = self._request(ipc.MonitorIPCOperation.DESCRIBE, deadline)
        after, terminal = self._read(deadline)
        phase = "committed" if reply.state == "launch-pending" else reply.state
        if phase not in _ORDER or not _ORDER[before.phase] <= _ORDER[phase] <= _ORDER[after.phase]:
            raise MonitorClientError("OCI monitor client authority or control is unavailable")
        if after.phase == "terminal":
            # STOP is a read-only completion query once exact TERMINAL is
            # durable. Only stop-terminal certifies worker descriptor cleanup.
            completed = self._request(ipc.MonitorIPCOperation.STOP, deadline)
            final, final_terminal = self._read(deadline)
            if final.phase != "terminal" or final_terminal != terminal:
                raise MonitorClientError("OCI monitor client terminal evidence changed")
            if completed.state != "stop-terminal":
                if completed.state not in {"stop-refused", "stop-accepted"}:
                    raise MonitorClientError("OCI monitor client terminal response is invalid")
                terminal = None
        return MonitorObservation(after.phase, terminal)

    def poll(self, *, timeout: float = 5.0) -> MonitorObservation:
        with _stable_errors():
            return self._poll(_Deadline(timeout))

    def _stop(self, deadline) -> str:
        observation = self._poll(deadline)
        if observation.terminal is not None:
            return "stop-terminal"
        if observation.phase == "terminal":
            self._wait(deadline, ready=False)
            return "stop-terminal"
        if observation.phase not in {"ready", "terminal"}:
            raise MonitorClientError("OCI monitor client STOP was refused before READY")
        reply = self._request(ipc.MonitorIPCOperation.STOP, deadline)
        after, _ = self._read(deadline)
        if reply.state not in {"stop-accepted", "stop-terminal"}:
            if reply.state == "stop-refused" and after.phase == "terminal":
                self._wait(deadline, ready=False)
                return "stop-terminal"
            raise MonitorClientError("OCI monitor client STOP was refused")
        if reply.state == "stop-terminal" and after.phase != "terminal":
            raise MonitorClientError("OCI monitor client terminal evidence is missing")
        return reply.state

    def request_stop(self, *, timeout: float = 5.0) -> str:
        """Queue STOP; the returned acceptance is explicitly not completion."""
        with _stable_errors():
            return self._stop(_Deadline(timeout))

    def _wait(self, deadline, *, ready: bool):
        while True:
            observation = self._poll(deadline)
            if observation.terminal is not None or (ready and observation.phase == "ready"):
                return observation
            time.sleep(min(0.01, deadline.remaining()))

    def wait_ready(self, *, timeout: float = 30.0) -> MonitorObservation:
        """Return READY or an already-completed natural terminal observation."""
        with _stable_errors():
            return self._wait(_Deadline(timeout), ready=True)

    def wait_terminal(self, *, timeout: float | None = 30.0) -> ProcessExit:
        with _stable_errors():
            result = self._wait(_Deadline(timeout, unbounded=True), ready=False).terminal
            assert result is not None
            return result

    def stop_and_wait(self, *, timeout: float = 30.0) -> ProcessExit:
        with _stable_errors():
            deadline = _Deadline(timeout)
            self._stop(deadline)
            result = self._wait(deadline, ready=False).terminal
            assert result is not None
            return result

    def exec_request(self, operation, payload, *, timeout=5.0):
        """Bind each mailbox exchange to the same pinned boot; retry only identical logical requests."""
        with _stable_errors():
            deadline = _Deadline(timeout)
            self._read(deadline)
            try:
                result = ipc.request_monitor_exec(
                    self._fd,
                    self._endpoint,
                    operation,
                    payload,
                    timeout=min(2.0, deadline.remaining(minimum=0.2) / 2),
                )
            except ipc.MonitorIPCError as exc:
                if exc.category is not ipc.MonitorIPCErrorCategory.TIMEOUT:
                    raise
                # No new sequence/token: a lost reply must never cause another
                # guest command. The caller may also retry this exact payload.
                result = ipc.request_monitor_exec(
                    self._fd,
                    self._endpoint,
                    operation,
                    payload,
                    timeout=min(5.0, deadline.remaining(minimum=0.1)),
                )
            self._read(deadline)
            return result
