"""Parent-side hard wall-clock supervisor for one OCI layer materialization."""

from __future__ import annotations

import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID
from .oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    VerifiedSquashFSToolchain,
)
from .oci_source import SnapshottedOCIImage
from .oci_store import DerivedLayerOccurrence, DerivedSquashFSKey, MaterializationResult, OCIStore
from .oci_worker_protocol import (
    MAX_OCI_WORKER_MESSAGE_BYTES,
    OCIWorkerProtocolError,
    OCIWorkerRequest,
    OCIWorkerResponse,
)
from .state import StatePaths

_WORKER_MODULE = "palimpsest_local.oci_materializer_worker"
_SCRATCH_PREFIX = ".oci-materializer-worker-"


class OCIHardWorkerError(ArtifactValidationError):
    """Stable path-free failure from the parent worker boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class _WorkerBoundaryFailure(OCIHardWorkerError):
    """Internal failure that preserves ownership of a possibly live worker."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        process: subprocess.Popen[bytes],
        reaped: bool,
    ):
        self.process = process
        self.reaped = reaped
        super().__init__(code, detail)


def _fixed_env() -> dict[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
    }


def _signal_group(process: subprocess.Popen[bytes], selected_signal: signal.Signals) -> None:
    try:
        os.killpg(process.pid, selected_signal)
    except (OSError, ProcessLookupError):
        pass


def _wait_bounded(process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
    try:
        process.wait(timeout=timeout_seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group_gone(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _group_exists(process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def _terminate_worker(process: subprocess.Popen[bytes], grace_seconds: float) -> bool:
    _signal_group(process, signal.SIGTERM)
    leader_reaped = _wait_bounded(process, grace_seconds)
    group_gone = _wait_group_gone(process.pid, grace_seconds)
    if not group_gone:
        _signal_group(process, signal.SIGKILL)
    if not leader_reaped:
        leader_reaped = _wait_bounded(process, grace_seconds)
    if not group_gone:
        group_gone = _wait_group_gone(process.pid, grace_seconds)
    return leader_reaped and group_gone


def _cleanup_scratch(scratch: Path, scratch_fd: int, expected_parent: Path) -> None:
    try:
        opened = os.fstat(scratch_fd)
        visible = os.stat(scratch, follow_symlinks=False)
        if (
            scratch.parent != expected_parent
            or not scratch.name.startswith(_SCRATCH_PREFIX)
            or not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            return
        shutil.rmtree(scratch)
    except OSError:
        return
    finally:
        try:
            os.close(scratch_fd)
        except OSError:
            pass


def _background_reap(
    process: subprocess.Popen[bytes],
    scratch: Path,
    scratch_fd: int,
    expected_parent: Path,
) -> None:
    def reap() -> None:
        try:
            process.wait()
            while _group_exists(process.pid):
                _signal_group(process, signal.SIGKILL)
                time.sleep(0.05)
        finally:
            _cleanup_scratch(scratch, scratch_fd, expected_parent)

    threading.Thread(target=reap, name="palimpsest-oci-worker-reaper", daemon=True).start()


def _spawn_and_exchange(
    request: OCIWorkerRequest,
    *,
    scratch: Path,
    timeout_seconds: float,
    grace_seconds: float,
    command: tuple[str, ...] | None = None,
) -> tuple[bytes, int, subprocess.Popen[bytes]]:
    selected_command = command or (sys.executable, "-I", "-m", _WORKER_MODULE)
    try:
        process = subprocess.Popen(
            selected_command,
            cwd=scratch,
            env=_fixed_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise OCIHardWorkerError("oci-worker-spawn", "materializer worker could not be started") from None

    request_bytes = request.to_json_bytes()
    output = bytearray()
    output_overflow = threading.Event()

    def write_request() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(request_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def read_response() -> None:
        assert process.stdout is not None
        try:
            while True:
                remaining = MAX_OCI_WORKER_MESSAGE_BYTES + 1 - len(output)
                if remaining <= 0:
                    output_overflow.set()
                    process.stdout.close()
                    return
                chunk = process.stdout.read(min(64 * 1024, remaining))
                if not chunk:
                    return
                output.extend(chunk)
        except OSError:
            return

    writer = threading.Thread(target=write_request, name="palimpsest-oci-worker-stdin", daemon=True)
    reader = threading.Thread(target=read_response, name="palimpsest-oci-worker-stdout", daemon=True)
    writer.start()
    reader.start()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        reaped = _terminate_worker(process, grace_seconds)
        raise _WorkerBoundaryFailure(
            "oci-worker-timeout",
            "materializer worker exceeded its wall-clock deadline",
            process=process,
            reaped=reaped,
        ) from None
    except BaseException as primary:
        reaped = _terminate_worker(process, grace_seconds)
        if not reaped:
            raise _WorkerBoundaryFailure(
                "oci-worker-interrupted",
                "materializer worker cleanup was deferred",
                process=process,
                reaped=False,
            ) from primary
        raise
    if _group_exists(process.pid):
        reaped = _terminate_worker(process, grace_seconds)
        raise _WorkerBoundaryFailure(
            "oci-worker-protocol",
            "materializer worker left a live descendant",
            process=process,
            reaped=reaped,
        )
    writer.join(timeout=grace_seconds)
    reader.join(timeout=grace_seconds)
    if output_overflow.is_set() or len(output) > MAX_OCI_WORKER_MESSAGE_BYTES:
        raise OCIHardWorkerError("oci-worker-protocol", "materializer worker response exceeds its bound")
    return bytes(output), process.returncode, process


def materialize_layer_hard(
    image: SnapshottedOCIImage,
    ordinal: int,
    *,
    source_cas_root: Path,
    roots: StatePaths,
    store: OCIStore,
    packer_path: Path,
    toolchain: VerifiedSquashFSToolchain,
    timeout_seconds: float = 300.0,
    terminate_grace_seconds: float = 1.0,
) -> MaterializationResult:
    """Materialize one occurrence in an exec worker or return no success."""
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("hard OCI materialization workers require Linux")
    if not isinstance(image, SnapshottedOCIImage) or type(ordinal) is not int:
        raise OCIHardWorkerError("oci-worker-input", "materializer image or ordinal is invalid")
    if not isinstance(roots, StatePaths) or not isinstance(store, OCIStore):
        raise OCIHardWorkerError("oci-worker-input", "materializer store authority is invalid")
    if not isinstance(toolchain, VerifiedSquashFSToolchain):
        raise OCIHardWorkerError("oci-worker-input", "materializer toolchain is invalid")
    for value in (timeout_seconds, terminate_grace_seconds):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < float(value) < float("inf"):
            raise OCIHardWorkerError("oci-worker-input", "materializer deadline is invalid")
    occurrence = DerivedLayerOccurrence.from_image(image, ordinal)
    key = DerivedSquashFSKey.for_occurrence(
        occurrence,
        intake_policy_id=LAYER_INTAKE_POLICY_ID,
        intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
        pack_policy_id=SQUASHFS_PACK_POLICY_ID,
        pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
        toolchain=toolchain,
    )
    request = OCIWorkerRequest(
        nonce=str(uuid.uuid4()),
        config_root=roots.config,
        state_root=roots.state,
        expected_store_id=store.identity,
        source_cas_root=source_cas_root,
        expected_source_cas_id=image.cas_id,
        source=image.image.source,
        occurrence=occurrence,
        key=key,
        key_digest=key.digest,
        packer_path=packer_path,
        packer_sha256=toolchain.identity.executable_digest.removeprefix("sha256:"),
        cpu_limit_seconds=min(3600, max(1, math.ceil(float(timeout_seconds)))),
    )

    scratch_parent = roots.runtime_packs
    started = time.monotonic()
    scratch: Path | None = None
    scratch_fd: int | None = None
    try:
        scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=scratch_parent))
        os.chmod(scratch, 0o700)
        scratch_fd = os.open(scratch, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        raise OCIHardWorkerError("oci-worker-scratch", "materializer worker scratch could not be created") from None
    assert scratch is not None and scratch_fd is not None
    process: subprocess.Popen[bytes] | None = None
    deferred_cleanup = False
    try:
        remaining = float(timeout_seconds) - (time.monotonic() - started)
        if remaining <= 0:
            raise OCIHardWorkerError("oci-worker-timeout", "materializer deadline expired before worker start")
        output, return_code, process = _spawn_and_exchange(
            request,
            scratch=scratch,
            timeout_seconds=remaining,
            grace_seconds=float(terminate_grace_seconds),
        )
        if time.monotonic() - started > float(timeout_seconds):
            raise OCIHardWorkerError("oci-worker-timeout", "late materializer success was discarded")
        if return_code != 0 or not output:
            raise OCIHardWorkerError("oci-worker-protocol", "materializer worker returned no valid response")
        try:
            response = OCIWorkerResponse.from_json_bytes(output)
        except OCIWorkerProtocolError:
            raise OCIHardWorkerError("oci-worker-protocol", "materializer worker response is invalid") from None
        if response.nonce != request.nonce or response.request_digest != request.digest:
            raise OCIHardWorkerError("oci-worker-protocol", "materializer worker response binding is invalid")
        if response.status == "failed":
            raise OCIHardWorkerError(
                f"oci-worker-{response.error_category}",
                "materializer worker failed inside its isolated boundary",
            )
        if response.result is None:
            raise OCIHardWorkerError("oci-worker-protocol", "materializer worker omitted its result")
        receipt = response.result.receipt
        if (
            receipt.store_id != request.expected_store_id
            or receipt.key_digest != request.key_digest
            or receipt.source_snapshot_binding_digest != occurrence.source_snapshot_binding_digest
            or receipt.source_image_digest != occurrence.source_image_digest
            or receipt.ordinal != occurrence.ordinal
        ):
            raise OCIHardWorkerError("oci-worker-protocol", "materializer worker receipt binding is invalid")
        return response.result
    except _WorkerBoundaryFailure as exc:
        process = exc.process
        deferred_cleanup = not exc.reaped
        if deferred_cleanup:
            _background_reap(process, scratch, scratch_fd, scratch_parent)
        raise exc
    except OCIHardWorkerError as exc:
        if process is not None and process.poll() is None:
            deferred_cleanup = not _terminate_worker(process, float(terminate_grace_seconds))
            if deferred_cleanup:
                _background_reap(process, scratch, scratch_fd, scratch_parent)
        raise exc
    finally:
        if not deferred_cleanup:
            _cleanup_scratch(scratch, scratch_fd, scratch_parent)


__all__ = ["OCIHardWorkerError", "materialize_layer_hard"]
