"""Parent-side hard wall-clock supervisor for one OCI layer materialization."""

from __future__ import annotations

import hashlib
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID
from .oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    VerifiedSquashFSToolchain,
    process_resource_detail,
    process_resource_failure,
)
from .oci_process import OCIProcessSpec
from .oci_provenance import Descriptor, canonical_json_bytes
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


@dataclass(frozen=True, slots=True)
class OCIImageMaterializationReceipt:
    """Path-free, ordered result for one fully materialized OCI image graph."""

    source_snapshot_binding_digest: str
    source_image_digest: str
    root_descriptor: Descriptor
    manifest_digest: str
    config_descriptor: Descriptor
    platform_os: str
    platform_architecture: str
    layer_descriptors: tuple[Descriptor, ...]
    layer_diff_ids: tuple[str, ...]
    process: OCIProcessSpec
    results: tuple[MaterializationResult, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "source_snapshot_binding_digest",
            "source_image_digest",
            "manifest_digest",
        ):
            value = getattr(self, field_name)
            try:
                normalized = normalize_digest(value)
            except (ArtifactValidationError, TypeError, ValueError):
                raise OCIHardWorkerError("oci-image-receipt", "image materialization digest is invalid") from None
            if normalized != value:
                raise OCIHardWorkerError("oci-image-receipt", "image materialization digest is not canonical")
        if not isinstance(self.root_descriptor, Descriptor) or not isinstance(self.config_descriptor, Descriptor):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization metadata is invalid")
        if not isinstance(self.process, OCIProcessSpec):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization process is invalid")
        if self.platform_os != "linux" or self.platform_architecture != "amd64":
            raise OCIHardWorkerError("oci-image-receipt", "image materialization platform is unsupported")
        if (
            not isinstance(self.layer_descriptors, tuple)
            or any(not isinstance(descriptor, Descriptor) for descriptor in self.layer_descriptors)
            or not isinstance(self.layer_diff_ids, tuple)
            or len(self.layer_descriptors) != len(self.layer_diff_ids)
        ):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization layer sources are invalid")
        for diff_id in self.layer_diff_ids:
            try:
                normalized = normalize_digest(diff_id)
            except (ArtifactValidationError, TypeError, ValueError):
                raise OCIHardWorkerError("oci-image-receipt", "image materialization DiffID is invalid") from None
            if normalized != diff_id:
                raise OCIHardWorkerError("oci-image-receipt", "image materialization DiffID is not canonical")
        if not isinstance(self.results, tuple) or not self.results:
            raise OCIHardWorkerError("oci-image-receipt", "image materialization results are invalid")
        if any(not isinstance(result, MaterializationResult) for result in self.results):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization results are invalid")
        if tuple(result.receipt.ordinal for result in self.results) != tuple(range(len(self.results))):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization order is invalid")
        if len(self.results) != len(self.layer_descriptors):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization layer count is inconsistent")
        if len({result.receipt.store_id for result in self.results}) != 1:
            raise OCIHardWorkerError("oci-image-receipt", "image materialization store binding is inconsistent")
        if any(
            result.receipt.source_snapshot_binding_digest != self.source_snapshot_binding_digest
            or result.receipt.source_image_digest != self.source_image_digest
            for result in self.results
        ):
            raise OCIHardWorkerError("oci-image-receipt", "image materialization source binding is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_descriptor": self.config_descriptor.to_dict(),
            "layers": [
                {"compressed": descriptor.to_dict(), "diff_id": diff_id, "ordinal": ordinal}
                for ordinal, (descriptor, diff_id) in enumerate(
                    zip(self.layer_descriptors, self.layer_diff_ids, strict=True)
                )
            ],
            "manifest_digest": self.manifest_digest,
            "platform": {"architecture": self.platform_architecture, "os": self.platform_os},
            "process": self.process.to_dict(),
            "retention": "none",
            "results": [result.to_dict() for result in self.results],
            "root_descriptor": self.root_descriptor.to_dict(),
            "schema": "palimpsest.oci-image-materialization.v2",
            "source_image_digest": self.source_image_digest,
            "source_snapshot_binding_digest": self.source_snapshot_binding_digest,
        }

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()}"


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
        reaped = False
        try:
            process.wait()
            while _group_exists(process.pid):
                _signal_group(process, signal.SIGKILL)
                time.sleep(0.05)
            reaped = True
        finally:
            if reaped:
                _cleanup_scratch(scratch, scratch_fd, expected_parent)
            else:
                os.close(scratch_fd)

    try:
        thread = threading.Thread(target=reap, name="palimpsest-oci-worker-reaper", daemon=True)
    except BaseException:
        os.close(scratch_fd)
        raise
    try:
        thread.start()
    except BaseException as exc:
        # As with the I/O helpers, start() can fail after creating a thread.
        # Only proven native creation failure leaves this pin caller-owned;
        # an actual or pending reaper must remain its exclusive closer.
        if thread.ident is None and isinstance(exc, RuntimeError) and str(exc) == "can't start new thread":
            os.close(scratch_fd)
        raise


def _spawn_and_exchange(
    request: OCIWorkerRequest,
    *,
    scratch: Path,
    timeout_seconds: float,
    grace_seconds: float,
    command: tuple[str, ...] | None = None,
) -> tuple[bytes, int, subprocess.Popen[bytes]]:
    request_bytes = request.to_json_bytes()
    output = bytearray()
    output_overflow = threading.Event()
    threads: tuple[threading.Thread, ...] = ()
    start_attempts = 0
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
    except (OSError, MemoryError) as exc:
        if process_resource_failure(exc):
            raise OCIHardWorkerError("oci-worker-resource", process_resource_detail(exc)) from None
        raise OCIHardWorkerError("oci-worker-spawn", "materializer worker could not be started") from None

    def write_request() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(request_bytes)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
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
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass

    try:
        writer = threading.Thread(target=write_request, name="palimpsest-oci-worker-stdin", daemon=True)
        reader = threading.Thread(target=read_response, name="palimpsest-oci-worker-stdout", daemon=True)
        threads = (writer, reader)
        for thread in threads:
            start_attempts += 1
            thread.start()
    except BaseException as primary:
        reaped = False
        cleanup_failed = False
        try:
            reaped = _terminate_worker(process, grace_seconds)
            if reaped:
                for index, stream in enumerate((process.stdin, process.stdout)):
                    if index < start_attempts:
                        # start() can be interrupted after creating a thread.
                        # Its owner closes the stream; never contend on that
                        # buffered lock if actual startup cannot be established.
                        if threads[index].ident is not None:
                            threads[index].join(timeout=grace_seconds)
                        elif (
                            index == start_attempts - 1
                            and isinstance(primary, RuntimeError)
                            and str(primary) == "can't start new thread"
                        ):
                            # CPython emits this exact error when native thread
                            # creation fails, before the thread owns its pipe.
                            # An asynchronous interruption is not this contract.
                            if stream is not None:
                                stream.close()
                    elif stream is not None:
                        stream.close()
        except BaseException:
            cleanup_failed = True
        resource_failure = process_resource_failure(primary) or (
            isinstance(primary, RuntimeError) and str(primary) == "can't start new thread"
        )
        if resource_failure or not reaped or cleanup_failed:
            failure = _WorkerBoundaryFailure(
                "oci-worker-resource" if resource_failure else "oci-worker-interrupted",
                "materializer helper thread could not start; check process/thread and memory limits; "
                + ("worker was reaped" if reaped else "worker cleanup is deferred"),
                process=process,
                reaped=reaped,
            )
            if cleanup_failed:
                failure.add_note("helper startup cleanup raised; original worker ownership is retained")
            raise failure from primary
        raise
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
                (
                    "materializer worker reported a resource failure; check process/thread, memory, and "
                    "service/cgroup limits; worker RLIMIT_NPROC remains capped at 256; "
                    "the exact limiting resource is not identified"
                    if response.error_category == "resource"
                    else "materializer worker failed inside its isolated boundary"
                ),
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
            try:
                _background_reap(process, scratch, scratch_fd, scratch_parent)
            except BaseException:
                exc.add_note(
                    "worker reaper startup failed; cleanup is not confirmed; scratch requires proven termination"
                )
        raise exc
    except OCIHardWorkerError as exc:
        if process is not None and process.poll() is None:
            deferred_cleanup = not _terminate_worker(process, float(terminate_grace_seconds))
            if deferred_cleanup:
                try:
                    _background_reap(process, scratch, scratch_fd, scratch_parent)
                except BaseException:
                    exc.add_note(
                        "worker reaper startup failed; cleanup is not confirmed; scratch requires proven termination"
                    )
        raise exc
    finally:
        if not deferred_cleanup:
            _cleanup_scratch(scratch, scratch_fd, scratch_parent)


def materialize_image_hard(
    image: SnapshottedOCIImage,
    *,
    source_cas_root: Path,
    roots: StatePaths,
    store: OCIStore,
    packer_path: Path,
    toolchain: VerifiedSquashFSToolchain,
    timeout_seconds: float = 300.0,
    terminate_grace_seconds: float = 1.0,
) -> OCIImageMaterializationReceipt:
    """Materialize every layer occurrence in order under one wall-clock deadline.

    Results are immutable cache entries, not runtime leases.  If a later
    occurrence fails, earlier cache entries may remain available for retry.
    """
    if not isinstance(image, SnapshottedOCIImage):
        raise OCIHardWorkerError("oci-worker-input", "materializer image is invalid")
    for value in (timeout_seconds, terminate_grace_seconds):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < float(value) < float("inf"):
            raise OCIHardWorkerError("oci-worker-input", "materializer deadline is invalid")
    started = time.monotonic()
    results: list[MaterializationResult] = []
    for ordinal in range(len(image.layers)):
        remaining = float(timeout_seconds) - (time.monotonic() - started)
        if remaining <= 0:
            raise OCIHardWorkerError("oci-worker-timeout", "image materialization deadline expired")
        result = materialize_layer_hard(
            image,
            ordinal,
            source_cas_root=source_cas_root,
            roots=roots,
            store=store,
            packer_path=packer_path,
            toolchain=toolchain,
            timeout_seconds=remaining,
            terminate_grace_seconds=terminate_grace_seconds,
        )
        if time.monotonic() - started > float(timeout_seconds):
            raise OCIHardWorkerError("oci-worker-timeout", "late image materialization success was discarded")
        results.append(result)
    return OCIImageMaterializationReceipt(
        source_snapshot_binding_digest=image.binding_digest,
        source_image_digest=image.image.digest,
        root_descriptor=image.root.descriptor,
        manifest_digest=image.image.manifest_descriptor.digest,
        config_descriptor=image.config.descriptor,
        platform_os=image.image.platform.os,
        platform_architecture=image.image.platform.architecture,
        layer_descriptors=tuple(layer.descriptor for layer in image.layers),
        layer_diff_ids=tuple(layer.diff_id for layer in image.image.layers),
        process=image.image.config.process,
        results=tuple(results),
    )


__all__ = [
    "OCIHardWorkerError",
    "OCIImageMaterializationReceipt",
    "materialize_image_hard",
    "materialize_layer_hard",
]
