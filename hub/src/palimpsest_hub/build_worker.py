"""HTTPS-only, single-execution Hub worker.

The private local journal is a write-ahead fence, not a retry queue. A lost
claim response permanently stops claiming until an operator establishes its
safe disposition. Lost output/failure responses are settled only by status
reads. Restart never executes a recorded job again. Executor cleanup must
succeed before uploading, deleting a job tree, or claiming another job.

Configuration comes from BuildWorkerSettings; only the key *path* is configured.
The executor CLI owns Linux/tool/mount preflight and exact resource cleanup.
"""

from __future__ import annotations

import asyncio
import ctypes
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import selectors
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from palimpsest_hub.config import get_build_worker_settings
from palimpsest_hub.logging import configure_logging

logger = logging.getLogger(__name__)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
LAYER_MEDIA_TYPE = "application/vnd.afterglow.palimpsest.layer.squashfs.v1"
RECIPE_LIMIT = 1024 * 1024
# JSON escaping can expand one UTF-8 input byte into six ASCII bytes.
JSON_LIMIT = 6 * RECIPE_LIMIT + 65536
STDOUT_LIMIT = 1024
LOG_LIMIT = 65536
CHUNK = 65536
TERMINAL = {"complete", "error"}
EXECUTOR_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}


class WorkerHalted(RuntimeError):
    """Uncertain remote disposition or local cleanup; retain the journal."""


class BuildFailed(RuntimeError):
    pass


class BuildTimeout(BuildFailed):
    pass


def _seal_worker_memory() -> None:
    """Deny same-UID recipe ptrace/proc-memory access before loading the key.

    This is a credential-memory fence, not the executor's build-host preflight.
    Never silently skip a missing or denied Linux primitive.
    """
    if sys.platform != "linux":
        raise WorkerHalted("worker credential memory cannot be sealed")
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        if prctl(4, 0, 0, 0, 0) != 0 or prctl(3, 0, 0, 0, 0) != 0:
            raise WorkerHalted("worker credential memory cannot be sealed")
    except (AttributeError, OSError) as exc:
        raise WorkerHalted("worker credential memory cannot be sealed") from exc


def _regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        raise WorkerHalted("worker file must be an owner-only regular file")
    return info


def _open_private(path: Path, flags: int = os.O_RDONLY) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        _regular(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_worker_key(path: str) -> str:
    """Never consult an environment secret or follow a keyfile symlink."""
    target = Path(path)
    if not target.is_absolute():
        raise WorkerHalted("worker key path must be absolute")
    try:
        fd = _open_private(target)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(4097)
        key = raw.decode("ascii").rstrip("\r\n")
        if not 32 <= len(key) <= 4096 or len(raw) > 4096 or any(not 33 <= ord(c) <= 126 for c in key):
            raise ValueError
        return key
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkerHalted("invalid worker key file") from exc


class HttpsTransport:
    """Standard TLS transport with no proxy, redirect, or retry machinery.

    http.client connects directly to the configured origin. A fresh connection
    prevents stale-connection retries; every request is made exactly once.
    """

    def __init__(self, origin: str, key: str):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in origin)
        ):
            raise WorkerHalted("worker requires an HTTPS origin")
        try:
            self.port = parsed.port or 443
        except ValueError as exc:
            raise WorkerHalted("worker requires an HTTPS origin") from exc
        self.host = parsed.hostname
        self.key = key
        self.context = ssl.create_default_context()

    @contextmanager
    def request(self, method: str, path: str, *, lease: str | None = None, body=None, headers=None, deadline=None):
        if not path.startswith(("/v1/build-worker/", "/v1/image-worker/")) or any(c in path for c in "\r\n?#"):
            raise WorkerHalted("invalid worker route")
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=10, context=self.context)
        end = deadline if deadline is not None else time.monotonic() + 30
        expired = threading.Event()
        socket_lock = threading.Lock()
        connected_socket = None

        def expire():
            expired.set()
            with socket_lock:
                active = connected_socket or connection.sock
            if active is not None:
                # close() alone cannot interrupt a response's retained makefile.
                with suppress(OSError):
                    active.shutdown(socket.SHUT_RDWR)

        timer = threading.Timer(max(0, end - time.monotonic()), expire)
        timer.daemon = True
        fields = {"X-Palimpsest-Build-Worker-Key": self.key, "Accept-Encoding": "identity"}
        if lease is not None:
            fields["X-Palimpsest-Build-Lease"] = lease
        fields.update(headers or {})
        try:
            timer.start()
            if expired.is_set() or time.monotonic() >= end:
                raise BuildTimeout("HTTP request deadline exceeded")
            connection.connect()
            with socket_lock:
                connected_socket = connection.sock
            if expired.is_set() or time.monotonic() >= end:
                expire()
                raise BuildTimeout("HTTP request deadline exceeded")
            connection.request(method, path, body=body, headers=fields)
            response = connection.getresponse()
            try:
                # Never consume or follow a Location, including same-origin redirects.
                if 300 <= response.status < 400:
                    raise WorkerHalted("worker transport refused redirect")
                yield response
                if expired.is_set() or time.monotonic() >= end:
                    raise BuildTimeout("HTTP request deadline exceeded")
            finally:
                response.close()
        except Exception as exc:
            if expired.is_set() or time.monotonic() >= end:
                raise BuildTimeout("HTTP request deadline exceeded") from exc
            raise
        finally:
            timer.cancel()
            if timer.ident is not None:
                timer.join()
            connection.close()


def _json_response(response) -> dict:
    if response.getheader("Content-Encoding", "identity") != "identity":
        raise WorkerHalted("encoded worker response refused")
    raw = bytearray()
    end = time.monotonic() + 30
    while True:
        if time.monotonic() >= end:
            raise WorkerHalted("worker response deadline exceeded")
        chunk = response.read1(min(CHUNK, JSON_LIMIT + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > JSON_LIMIT:
            raise WorkerHalted("worker response exceeded limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise WorkerHalted("invalid worker response")
    return result


def _uuid(value) -> str:
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError("invalid claim identity")
    return value


def _digest(value) -> str:
    if type(value) is not str or not DIGEST.fullmatch(value):
        raise ValueError("invalid digest")
    return value


def _size(value, limit: int) -> int:
    if type(value) is not int or not 0 < value <= limit:
        raise ValueError("invalid descriptor size")
    return value


def validate_claim(value: dict, limit: int, timeout: int) -> dict:
    """Validate the entire pinned descriptor graph before any input request."""
    _uuid(value["id"])
    _uuid(value["lease"])
    deadline = datetime.fromisoformat(value["deadline"])
    if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0:
        raise ValueError("invalid claim deadline")
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    seconds = value["timeout_seconds"]
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600 or remaining <= 0:
        raise ValueError("invalid claim timeout")
    if type(value["name"]) is not str or not 0 < len(value["name"]) <= 256:
        raise ValueError("invalid claim name")
    recipe = value["recipe"]
    if type(recipe) is not str or not 0 < len(recipe.encode()) <= RECIPE_LIMIT:
        raise ValueError("invalid recipe")
    if _digest(value["recipe_digest"]) != "sha256:" + hashlib.sha256(recipe.encode()).hexdigest():
        raise ValueError("recipe digest mismatch")
    base = value["base"]
    _digest(base["digest"])
    total = _size(base["size_bytes"], limit)
    if base["arch"] != "x86_64" or base["disk_format"] not in ("raw", "qcow2"):
        raise ValueError("invalid build base")
    layers = value["layers"]
    if type(layers) is not list or len(layers) > 128:
        raise ValueError("invalid build layers")
    previous = None
    seen = {base["digest"]}
    for layer in layers:
        digest = _digest(layer["digest"])
        total += _size(layer["size_bytes"], limit)
        if (
            digest in seen
            or layer["media_type"] != LAYER_MEDIA_TYPE
            or layer["parent_digest"] != previous
            or (previous is None and layer["base_image_digest"] != base["digest"])
            or (layer["base_image_digest"] is not None and layer["base_image_digest"] != base["digest"])
        ):
            raise ValueError("invalid build chain")
        seen.add(digest)
        previous = digest
    if total > limit:
        raise ValueError("total build inputs exceed limit")
    output_limit = min(_size(value["max_output_bytes"], 2**63 - 1), limit)
    # Rebuild the manifest; untrusted extra keys (URLs/paths) never reach the executor.
    return {
        "id": value["id"],
        "lease": value["lease"],
        "name": value["name"],
        "recipe": recipe,
        "base": {k: base[k] for k in ("digest", "arch", "disk_format", "size_bytes")},
        "layers": [
            {k: layer[k] for k in ("digest", "media_type", "parent_digest", "base_image_digest", "size_bytes")}
            for layer in layers
        ],
        "timeout_seconds": min(seconds, remaining, timeout),
        "max_output_bytes": output_limit,
        "input_bytes": total,
    }


class Journal:
    """One durable operation at a time, inside an owner-only local flock root."""

    def __init__(self, root: Path):
        if not root.is_absolute():
            raise WorkerHalted("worker scratch must be absolute")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise WorkerHalted("worker scratch must be an owner-only directory")
        self.root = root
        self.path = root / "worker.json"
        self.lock_fd = _open_private(root / "worker.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock_fd)
            raise WorkerHalted("worker scratch already owned")

    def close(self):
        os.close(self.lock_fd)

    def load(self) -> dict | None:
        try:
            fd = _open_private(self.path)
        except FileNotFoundError:
            if any(p.name != "worker.lock" for p in self.root.iterdir()):
                raise WorkerHalted("unjournaled worker scratch retained")
            return None
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(JSON_LIMIT + 1)
        if len(raw) > JSON_LIMIT:
            raise WorkerHalted("invalid worker journal")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("phase") not in {
                "claim_pending",
                "claimed",
                "executing",
                "output_pending",
                "fail_pending",
                "terminal",
            }:
                raise ValueError("invalid journal phase")
            if value.get("protocol", "recipe") not in {"recipe", "image"}:
                raise ValueError("invalid journal protocol")
            if value["phase"] != "claim_pending":
                _uuid(value["id"])
                _uuid(value["lease"])
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkerHalted("invalid worker journal") from exc
        self._discard_uncommitted()
        return value

    def _discard_uncommitted(self):
        """Under flock, only the validated durable journal authorizes recovery.

        A leftover .new file never crossed replace+fsync, so no HTTP effect or
        executor launch can have been authorized by it. Its content may be a
        partial write; never adopt it or replace the committed record with it.
        """
        temporary = self.root / "worker.json.new"
        try:
            fd = _open_private(temporary)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkerHalted("unsafe worker journal staging retained") from exc
        try:
            info = _regular(fd)
            current = temporary.lstat()
            if info.st_size > JSON_LIMIT or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise WorkerHalted("unsafe worker journal staging retained")
            temporary.unlink()
            self._sync()
        finally:
            os.close(fd)

    def _sync(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save(self, value: dict):
        temporary = self.root / "worker.json.new"
        fd = _open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self._sync()

    def clear(self):
        self.path.unlink()
        self._sync()

    def job_dir(self, record: dict) -> Path:
        return self.root / ("job-" + _uuid(record["id"]))


def _terminate(child: subprocess.Popen):
    """Drain the entire new process group, never only its original leader."""
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        child.wait(timeout=2)
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise WorkerHalted("executor process could not be stopped") from exc
    end = time.monotonic() + 2
    while time.monotonic() < end:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise WorkerHalted("executor process group could not be verified stopped")


def run_executor(
    arguments: list[str],
    timeout: float,
    stop: threading.Event | None = None,
    log: Path | None = None,
    *,
    module: str = "palimpsest_local.hub_builder",
):
    """Bound both output pipes and wall time without communicate() accumulation."""
    if module not in {"palimpsest_local.hub_builder", "palimpsest_local.hub_image_builder"}:
        raise WorkerHalted("invalid executor module")
    child = subprocess.Popen(
        [sys.executable, "-I", "-m", module, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=EXECUTOR_ENV,
        close_fds=True,
        start_new_session=True,
        umask=0o077,
    )
    output = bytearray()
    errors = 0
    end = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, "stdout")
            selector.register(child.stderr, selectors.EVENT_READ, "stderr")
            log_fd = _open_private(log, os.O_CREAT | os.O_WRONLY | os.O_EXCL) if log else None
            try:
                while selector.get_map() or child.poll() is None:
                    if time.monotonic() >= end:
                        raise BuildTimeout("executor deadline exceeded")
                    if stop is not None and stop.is_set():
                        raise BuildFailed("worker stopping")
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), CHUNK)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data == "stdout":
                            if len(output) + len(chunk) > STDOUT_LIMIT:
                                raise BuildFailed("executor output exceeded limit")
                            output.extend(chunk)
                        else:
                            errors += len(chunk)
                            if errors > LOG_LIMIT:
                                raise BuildFailed("executor log exceeded limit")
                            if log_fd is not None:
                                os.write(log_fd, chunk)
            finally:
                if log_fd is not None:
                    os.close(log_fd)
        if child.returncode == 124:
            raise BuildTimeout("executor deadline exceeded")
        if child.returncode != 0:
            raise BuildFailed("executor failed")
        return bytes(output)
    finally:
        # Also fence children left behind by an exited group leader.
        try:
            _terminate(child)
        finally:
            child.stdout.close()
            child.stderr.close()


class PinnedBody:
    """Bound a file stream by its pinned size and the job's remaining deadline."""

    def __init__(self, stream, size: int, deadline: float, stop: threading.Event | None = None):
        self.stream = stream
        self.size = size
        self.deadline = deadline
        self.stop = stop
        self.count = 0

    def read(self, size=CHUNK):
        if time.monotonic() >= self.deadline:
            raise BuildTimeout("output transfer deadline exceeded")
        if self.stop is not None and self.stop.is_set():
            raise BuildFailed("worker stopping")
        chunk = self.stream.read(min(size if size >= 0 else CHUNK, CHUNK, self.size + 1 - self.count))
        self.count += len(chunk)
        if self.count > self.size or (not chunk and self.count != self.size):
            raise BuildFailed("output changed during transfer")
        return chunk


class BuildWorker:
    def __init__(self, settings, transport, journal: Journal, stop: threading.Event):
        self.settings = settings
        self.transport = transport
        self.journal = journal
        self.stop = stop

    @staticmethod
    def _route(record, suffix=""):
        prefix = "/v1/image-worker/jobs/" if record.get("protocol") == "image" else "/v1/build-worker/builds/"
        return prefix + _uuid(record["id"]) + suffix

    def _status(self, record) -> dict:
        with self.transport.request("GET", self._route(record), lease=_uuid(record["lease"])) as response:
            if response.status != 200:
                raise WorkerHalted("build disposition unavailable")
            result = _json_response(response)
        if result.get("id") != record["id"] or result.get("status") not in TERMINAL:
            raise WorkerHalted("build disposition remains uncertain")
        if result["status"] == "complete" and record.get("output") is not None:
            output = record["output"]
            if record.get("protocol") == "image":
                if result.get("result") != output:
                    raise WorkerHalted("image output disposition conflicts")
                return result
            if (
                result.get("output_digest") != output["digest"]
                or result.get("output_size_bytes") != output["size_bytes"]
            ):
                raise WorkerHalted("build output disposition conflicts")
        return result

    def _cleanup(self, record):
        directory = self.journal.job_dir(record)
        try:
            info = directory.lstat()
        except FileNotFoundError:
            # Mutation intents are durable only AFTER exact executor cleanup.
            # A claimed tree has never executed; only executing needs its ledger.
            if record["phase"] not in ("claimed", "output_pending", "fail_pending", "terminal"):
                raise WorkerHalted("recorded executor tree missing")
            return
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise WorkerHalted("recorded executor tree is unsafe")
        try:
            arguments = ["cleanup", str(directory), str(os.getpid())]
            if record.get("protocol") == "image":
                run_executor(arguments, 60, module="palimpsest_local.hub_image_builder")
            else:
                run_executor(arguments, 30)
        except Exception as exc:
            raise WorkerHalted("executor cleanup could not be verified") from exc

    def _dispose(self, record):
        try:
            record["phase"] = "terminal"
            self.journal.save(record)
            directory = self.journal.job_dir(record)
            if directory.exists():
                shutil.rmtree(directory)
            self.journal.clear()
        except Exception as exc:
            raise WorkerHalted("private tree disposal could not be verified") from exc

    def _fail(self, record, code):
        record["phase"] = "fail_pending"
        self.journal.save(record)
        try:
            payload = json.dumps({"error_code": code}).encode()
            with self.transport.request(
                "POST",
                self._route(record, "/fail"),
                lease=record["lease"],
                body=payload,
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            ) as response:
                if response.status != 200:
                    raise WorkerHalted("failure disposition uncertain")
                _json_response(response)
        except Exception:
            # Never resend a mutation even when the first response was an error.
            self._status(record)
        else:
            self._status(record)
        self._dispose(record)

    def reconcile(self):
        record = self.journal.load()
        if record is None:
            return
        if record.get("phase") == "claim_pending":
            raise WorkerHalted("claim outcome uncertain; operator disposition required")
        if record.get("phase") not in {"claimed", "executing", "output_pending", "fail_pending", "terminal"}:
            raise WorkerHalted("invalid worker journal phase")
        _uuid(record["id"])
        _uuid(record["lease"])
        self._cleanup(record)
        if record["phase"] in {"output_pending", "fail_pending", "terminal"}:
            self._status(record)
            self._dispose(record)
        else:
            self._fail(record, "build_failed")

    def _download(self, record, descriptor, target: Path, deadline: float):
        expected = descriptor["size_bytes"]
        digest = hashlib.sha256()
        count = 0
        fd = _open_private(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with (
            os.fdopen(fd, "wb") as stream,
            self.transport.request(
                "GET",
                self._route(record, "/inputs/" + descriptor["digest"]),
                lease=record["lease"],
                deadline=deadline,
            ) as response,
        ):
            if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
                raise BuildFailed("input unavailable")
            declared = response.getheader("Content-Length")
            if declared is None or not declared.isdecimal() or int(declared) != expected:
                raise BuildFailed("input length mismatch")
            while True:
                if time.monotonic() >= deadline:
                    raise BuildTimeout("input deadline exceeded")
                if self.stop.is_set():
                    raise BuildFailed("worker stopping")
                chunk = response.read1(min(CHUNK, expected + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > expected:
                    raise BuildFailed("input exceeds descriptor")
                digest.update(chunk)
                stream.write(chunk)
            if count != expected or "sha256:" + digest.hexdigest() != descriptor["digest"]:
                raise BuildFailed("input digest or size mismatch")
            stream.flush()
            os.fsync(stream.fileno())

    def _build(self, claim, record, deadline):
        directory = self.journal.job_dir(record)
        directory.mkdir(mode=0o700)
        if shutil.disk_usage(directory).free < claim["input_bytes"] + claim["max_output_bytes"]:
            raise BuildFailed("insufficient private scratch")
        manifest = {k: claim[k] for k in ("name", "recipe", "max_output_bytes")}
        manifest["max_input_bytes"] = self.settings.palimpsest_hub_max_blob_bytes
        inputs = []
        for index, descriptor in enumerate([claim["base"], *claim["layers"]]):
            target = directory / f"input-{index}"
            self._download(record, descriptor, target, deadline)
            inputs.append({k: v for k, v in descriptor.items() if k != "size_bytes"} | {"path": str(target)})
        manifest["base"], *manifest["layers"] = inputs
        manifest["timeout_seconds"] = deadline - time.monotonic()
        if manifest["timeout_seconds"] <= 0:
            raise BuildTimeout("build deadline exceeded")
        path = directory / "input.json"
        fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream)
            stream.flush()
            os.fsync(stream.fileno())
        record["phase"] = "executing"
        self.journal.save(record)
        raw = run_executor(
            [str(path), str(os.getpid())], manifest["timeout_seconds"], self.stop, directory / "executor.log"
        )
        result = json.loads(raw)
        _digest(result["digest"])
        _size(result["size_bytes"], claim["max_output_bytes"])
        return {"digest": result["digest"], "size_bytes": result["size_bytes"]}

    def _upload(self, record, result, deadline):
        # Open once, hash and stream the same nofollow descriptor after cleanup.
        fd = _open_private(self.journal.job_dir(record) / "result.sqsh")
        with os.fdopen(fd, "rb") as stream:
            if os.fstat(stream.fileno()).st_size != result["size_bytes"]:
                raise BuildFailed("output size mismatch")
            digest = hashlib.sha256()
            if stream.read(4) != b"hsqs":
                raise BuildFailed("output is not SquashFS")
            stream.seek(0)
            bounded = PinnedBody(stream, result["size_bytes"], deadline, self.stop)
            for chunk in iter(bounded.read, b""):
                digest.update(chunk)
            if "sha256:" + digest.hexdigest() != result["digest"]:
                raise BuildFailed("output digest mismatch")
            stream.seek(0)
            record.update(phase="output_pending", output=result)
            self.journal.save(record)
            try:
                with self.transport.request(
                    "PUT",
                    self._route(record, "/output"),
                    lease=record["lease"],
                    body=PinnedBody(stream, result["size_bytes"], deadline, self.stop),
                    deadline=deadline,
                    headers={
                        "Content-Length": str(result["size_bytes"]),
                        "X-Content-Digest": result["digest"],
                        "Content-Type": "application/octet-stream",
                    },
                ) as response:
                    if response.status != 200:
                        raise WorkerHalted("output disposition uncertain")
                    _json_response(response)
            except Exception:
                self._status(record)
            else:
                self._status(record)
        self._dispose(record)

    def process_one(self) -> bool:
        self.reconcile()
        if self.stop.is_set():
            return False
        from palimpsest_hub import image_executor

        record = None
        try:
            for candidate in ("image", "recipe"):
                self.journal.save({"phase": "claim_pending", "protocol": candidate})
                route = "/v1/image-worker/claim" if candidate == "image" else "/v1/build-worker/claim"
                with self.transport.request("POST", route) as response:
                    if response.status == 204:
                        self.journal.clear()
                        continue
                    if response.status != 202:
                        raise WorkerHalted("claim outcome uncertain")
                    value = _json_response(response)
                    identity = {
                        "phase": "claimed",
                        "protocol": candidate,
                        "id": _uuid(value["id"]),
                        "lease": _uuid(value["lease"]),
                    }
                    self.journal.save(identity)
                    record = identity
                    if candidate == "image":
                        claim = image_executor.validate_claim(value, self.settings)
                    else:
                        claim = validate_claim(
                            value,
                            self.settings.palimpsest_hub_max_blob_bytes,
                            self.settings.palimpsest_hub_build_timeout_seconds,
                        )
                    break
            else:
                return False
        except Exception as exc:
            if record is None:
                raise WorkerHalted("claim outcome uncertain; journal retained") from exc
            # The exact lease is durable: invalid descriptors fail this attempt,
            # rather than poisoning the worker as an unidentified claim.
            self._cleanup(record)
            self._fail(record, "build_failed")
            return True
        deadline = time.monotonic() + claim["timeout_seconds"]
        try:
            result = (
                image_executor.build(self, claim, record, deadline)
                if record.get("protocol") == "image"
                else self._build(claim, record, deadline)
            )
        except WorkerHalted:
            self._cleanup(record)
            raise
        except Exception as exc:
            self._cleanup(record)
            self._fail(record, "build_timeout" if isinstance(exc, BuildTimeout) else "build_failed")
        else:
            self._cleanup(record)
            try:
                if record.get("protocol") == "image":
                    image_executor.upload(self, record, result, deadline)
                else:
                    self._upload(record, result, deadline)
            except WorkerHalted:
                raise
            except Exception as exc:
                # Upload intent means only status settlement remains safe.
                if record["phase"] == "output_pending":
                    raise WorkerHalted("output disposition uncertain; journal retained")
                self._fail(record, "build_timeout" if isinstance(exc, BuildTimeout) else "build_failed")
        return True


async def _drain_blocking(stop: threading.Event, function, *args):
    """Cancellation sets the stop fence but cannot release a live thread's flock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        with suppress(Exception):
            await task
        raise


async def main() -> None:
    configure_logging()
    settings = get_build_worker_settings()
    _seal_worker_memory()
    key = read_worker_key(settings.palimpsest_hub_build_worker_key_file)
    transport = HttpsTransport(settings.palimpsest_hub_url, key)
    journal = Journal(Path(settings.palimpsest_hub_build_work_path))
    stop = threading.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    try:
        try:
            await _drain_blocking(stop, run_executor, ["preflight", str(os.getpid()), str(journal.root)], 30, stop)
        except Exception as exc:
            raise WorkerHalted("executor preflight failed") from exc
        worker = BuildWorker(settings, transport, journal, stop)
        await _drain_blocking(stop, worker.reconcile)
        try:
            await _drain_blocking(
                stop,
                lambda: run_executor(
                    ["preflight", str(os.getpid()), str(journal.root), settings.palimpsest_hub_image_cgroup_root],
                    30,
                    stop,
                    module="palimpsest_local.hub_image_builder",
                ),
            )
        except Exception as exc:
            raise WorkerHalted("image executor preflight failed") from exc
        while not stop.is_set():
            processed = await _drain_blocking(stop, worker.process_one)
            if not processed:
                await _drain_blocking(stop, stop.wait, 2)
    finally:
        stop.set()
        for sig in installed:
            loop.remove_signal_handler(sig)
        journal.close()


def run() -> None:
    try:
        asyncio.run(main())
    except Exception:
        # Never send exception chains, recipes, paths, or credentials to logs.
        logger.error("Build worker halted; retained state requires safe disposition")
        raise SystemExit(1) from None


if __name__ == "__main__":
    # Image adapters import the canonical worker module. Run that same module's
    # loop so timeout/HOLD exception classes are identical under both entrypoints.
    from palimpsest_hub.build_worker import run as worker_main

    worker_main()
