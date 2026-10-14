"""Foreground-owned, bounded canonical CLI jobs for the local dashboard."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .errors import StateError
from .state import StatePaths


@dataclass
class _Operation:
    id: str
    kind: str
    process: subprocess.Popen
    resource_name: str | None
    work: tempfile.TemporaryDirectory | None = None
    status: str = "running"
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    returncode: int | None = None
    thread: threading.Thread | None = None
    deadline: float | None = None


class OperationManager:
    MAX_ACTIVE = 2
    MAX_RETAINED = 32
    MAX_OUTPUT_BYTES = 128 * 1024
    MAX_TRANSFER_SECONDS = 15 * 60

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._operations: dict[str, _Operation] = {}
        self._closed = False

    def start(
        self,
        roots: StatePaths,
        kind: str,
        args: list[str],
        *,
        resource_name: str | None = None,
        dockerfile_text: str | None = None,
        module: str = "palimpsest_local.cli",
    ) -> dict:
        """Inventory owns arguments; only fixed first-party job modules may execute."""
        if module not in {"palimpsest_local.cli", "palimpsest_local.remote_artifacts"}:
            raise StateError("Unsupported web operation module")
        with self._lock:
            if self._closed:
                raise StateError("Web operation manager is closed")
            if sum(item.status == "running" for item in self._operations.values()) >= self.MAX_ACTIVE:
                raise StateError("Web operation limit reached; wait for an active operation")
            if len(self._operations) >= self.MAX_RETAINED:
                completed = next((key for key, item in self._operations.items() if item.status != "running"), None)
                if completed is None:
                    raise StateError("Web operation retention limit reached")
                del self._operations[completed]
            work = None
            try:
                if dockerfile_text is not None:
                    work = tempfile.TemporaryDirectory(prefix="palimpsest-web-build-")
                    recipe = Path(work.name) / "Dockerfile"
                    recipe.write_text(dockerfile_text, encoding="utf-8")
                    args = [*args, f"--file={recipe}"]
                environment = dict(os.environ)
                environment["PALIMPSEST_STATE_HOME"] = str(roots.state.resolve())
                environment["XDG_CONFIG_HOME"] = str(roots.config.resolve().parent)
                environment["PYTHONUNBUFFERED"] = "1"
                environment["PALIMPSEST_WEB_JOB"] = kind
                process = subprocess.Popen(
                    [sys.executable, "-m", module, *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                )
            except Exception:
                if work is not None:
                    work.cleanup()
                raise StateError("Cannot start web operation") from None
            operation = _Operation(uuid.uuid4().hex, kind, process, resource_name, work)
            if kind in {"download", "upload"}:
                operation.deadline = time.monotonic() + self.MAX_TRANSFER_SECONDS
            self._operations[operation.id] = operation
            operation.thread = threading.Thread(target=self._pump, args=(operation,), name="web-operation", daemon=True)
            operation.thread.start()
            return self._project(operation)

    def _append(self, operation: _Operation, data: bytes) -> None:
        with self._lock:
            operation.output.extend(data)
            excess = len(operation.output) - self.MAX_OUTPUT_BYTES
            if excess > 0:
                del operation.output[:excess]
                operation.truncated = True

    @staticmethod
    def _cancel(operation: _Operation, number: int = signal.SIGTERM) -> None:
        try:
            if operation.kind in {"build", "download", "upload"}:
                # Build/transfer sessions contain only owned clients/tools, never VMs.
                os.killpg(operation.process.pid, number)
            elif operation.process.poll() is None:
                # Unwind prelaunch tool ownership; once launching, SIGUSR1 is
                # process-only termination, never the OCI startup STOP signal.
                operation.process.send_signal(signal.SIGUSR1 if number != signal.SIGKILL else number)
        except ProcessLookupError:
            pass

    def _pump(self, operation: _Operation) -> None:
        process = operation.process
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if operation.deadline is not None and time.monotonic() >= operation.deadline:
                        self._append(operation, b"\nRemote transfer exceeded its 15-minute operation deadline\n")
                        raise TimeoutError("remote transfer deadline")
                    ready = selector.select(timeout=0.1)
                    if ready:
                        data = os.read(process.stdout.fileno(), 16384)
                        if not data:
                            break
                        self._append(operation, data)
                    elif process.poll() is not None:
                        # A detached VM may inherit a descriptor: do not await its lifetime.
                        break
            returncode = process.wait()
        except Exception:
            if process.poll() is None:
                self._cancel(operation)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._cancel(operation, signal.SIGKILL)
                    process.wait()
            if operation.kind in {"build", "download", "upload"}:
                self._cancel(operation, signal.SIGKILL)
            returncode = process.returncode if process.returncode else 1
            self._append(operation, b"\nWeb operation output transport failed\n")
        finally:
            process.stdout.close()
            if operation.work is not None:
                try:
                    operation.work.cleanup()
                except OSError:
                    self._append(operation, b"\nTemporary recipe cleanup failed\n")
                    returncode = returncode or 1
        with self._lock:
            operation.returncode = returncode
            operation.status = "succeeded" if returncode == 0 else "failed"

    @staticmethod
    def _project(operation: _Operation) -> dict:
        result = {
            "id": operation.id,
            "kind": operation.kind,
            "status": operation.status,
            "output": bytes(operation.output).decode("utf-8", errors="replace"),
            "output_truncated": operation.truncated,
        }
        if operation.resource_name is not None:
            result["resource_name"] = operation.resource_name
        if operation.returncode is not None:
            result["returncode"] = operation.returncode
            if operation.returncode != 0:
                result["error"] = f"Command exited with status {operation.returncode}"
        return result

    def list(self) -> list[dict]:
        with self._lock:
            return [self._project(item) for item in reversed(tuple(self._operations.values()))]

    def has_active(self) -> bool:
        with self._lock:
            return any(item.status == "running" for item in self._operations.values())

    def close_all(self) -> None:
        """Stop/reap owned CLI clients; never issue VM lifecycle operations."""
        with self._lock:
            self._closed = True
            operations = tuple(self._operations.values())
            running = tuple(item for item in operations if item.status == "running")
        for item in running:
            self._cancel(item)
        for item in running:
            try:
                item.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self._cancel(item, signal.SIGKILL)
                item.process.wait()
            if item.kind in {"build", "download", "upload"}:
                # A leader exiting on TERM does not prove its tools exited.
                self._cancel(item, signal.SIGKILL)
        for item in operations:
            if item.thread is not None:
                item.thread.join(timeout=4)
