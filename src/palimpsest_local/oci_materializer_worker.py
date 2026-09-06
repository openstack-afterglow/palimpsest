"""Exec worker owning one complete cold OCI layer materialization attempt."""

from __future__ import annotations

import os
import resource
import sys
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStoreError
from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID, LayerIntakeError, stage_layer
from .oci_image import OCIImageRef, resolve_image
from .oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    SquashFSPackError,
    SquashFSPackExecution,
    discover_squashfs_toolchain,
    pack_staged_squashfs,
    process_resource_errno,
    process_resource_failure,
)
from .oci_source import SnapshottedOCIImage, SourceCAS, SourceLeaseError, SourceSnapshot
from .oci_store import DerivedLayerOccurrence, DerivedSquashFSKey, OCIStore, OCIStoreError
from .oci_worker_limits import OCI_WORKER_NPROC_LIMIT
from .oci_worker_protocol import (
    MAX_OCI_WORKER_MESSAGE_BYTES,
    OCIWorkerProtocolError,
    OCIWorkerRequest,
    OCIWorkerResponse,
)
from .state import StatePaths

_MAX_WORKER_ADDRESS_SPACE = 40 * 1024**3
_MAX_WORKER_FILE_SIZE = 40 * 1024**3
_MAX_WORKER_FDS = 256
_MAX_WORKER_PROCESSES = OCI_WORKER_NPROC_LIMIT


class _WorkerResourceError(Exception):
    """Internal fixed-fact carrier; never serialized as exception text."""

    def __init__(self, failure_stage: str, failure_errno: int | None):
        self.failure_stage = failure_stage
        self.failure_errno = failure_errno
        super().__init__("OCI worker resource operation failed")


def _resource_boundary[T](failure_stage: str, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return operation(*args, **kwargs)
    except BaseException as exc:
        if process_resource_failure(exc):
            raise _WorkerResourceError(failure_stage, process_resource_errno(exc)) from None
        raise


def _limit(resource_name: int, soft_limit: int) -> None:
    current_soft, current_hard = resource.getrlimit(resource_name)
    hard_target = soft_limit if current_hard == resource.RLIM_INFINITY else min(soft_limit, current_hard)
    soft_target = hard_target if current_soft == resource.RLIM_INFINITY else min(current_soft, hard_target)
    if (current_soft, current_hard) != (soft_target, hard_target):
        resource.setrlimit(resource_name, (soft_target, hard_target))


def _apply_resource_limits(cpu_limit_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _limit(resource.RLIMIT_CPU, cpu_limit_seconds)
    _limit(resource.RLIMIT_NOFILE, _MAX_WORKER_FDS)
    _limit(resource.RLIMIT_FSIZE, _MAX_WORKER_FILE_SIZE)
    if hasattr(resource, "RLIMIT_AS"):
        _limit(resource.RLIMIT_AS, _MAX_WORKER_ADDRESS_SPACE)
    if hasattr(resource, "RLIMIT_NPROC"):
        _limit(resource.RLIMIT_NPROC, _MAX_WORKER_PROCESSES)


def _reconstruct_image(request: OCIWorkerRequest, cas: SourceCAS) -> SnapshottedOCIImage:
    reference = OCIImageRef(
        registry=request.source.registry,
        repository=request.source.repository,
        requested_reference=request.source.requested_reference,
    )
    root_descriptor = request.source.index_descriptor or request.source.manifest_descriptor
    root = SourceSnapshot(root_descriptor, cas.identity)

    def reader(descriptor):
        return cas.read_metadata(SourceSnapshot(descriptor, cas.identity))

    image = resolve_image(reference, root_descriptor, reader)
    if image.source != request.source or image.digest != request.occurrence.source_image_digest:
        raise ArtifactValidationError("worker source graph binding is inconsistent")
    snapshot = SnapshottedOCIImage(
        image=image,
        root=root,
        manifest=SourceSnapshot(image.manifest_descriptor, cas.identity),
        config=SourceSnapshot(image.config.descriptor, cas.identity),
        layers=tuple(SourceSnapshot(layer.compressed, cas.identity) for layer in image.layers),
    )
    if snapshot.binding_digest != request.occurrence.source_snapshot_binding_digest:
        raise ArtifactValidationError("worker source snapshot binding is inconsistent")
    if DerivedLayerOccurrence.from_image(snapshot, request.occurrence.ordinal) != request.occurrence:
        raise ArtifactValidationError("worker source occurrence binding is inconsistent")
    return snapshot


def _materialize(request: OCIWorkerRequest):
    roots = StatePaths(request.config_root, request.state_root)
    store = _resource_boundary("store", OCIStore, roots)
    if store.identity != request.expected_store_id:
        raise OCIStoreError("oci-store-authority", "worker derived store identity does not match")

    @contextmanager
    def producer():
        cas = _resource_boundary(
            "source",
            SourceCAS.open_existing,
            request.source_cas_root,
            expected_cas_id=request.expected_source_cas_id,
        )
        snapshot = _resource_boundary("source", _reconstruct_image, request, cas)
        toolchain = _resource_boundary(
            "packing",
            discover_squashfs_toolchain,
            request.packer_path,
            expected_packer_sha256=request.packer_sha256,
            policy=DEFAULT_SQUASHFS_PACK_POLICY,
        )
        computed_key = DerivedSquashFSKey.for_occurrence(
            request.occurrence,
            intake_policy_id=LAYER_INTAKE_POLICY_ID,
            intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            pack_policy_id=SQUASHFS_PACK_POLICY_ID,
            pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
            toolchain=toolchain,
        )
        if computed_key != request.key:
            raise OCIStoreError("oci-store-key", "worker recipe does not match the verified toolchain")
        execution = SquashFSPackExecution(scratch_root=Path.cwd(), inherit_process_group=True)
        with ExitStack() as stack:
            source = _resource_boundary(
                "source",
                stack.enter_context,
                cas.lease_layer(snapshot, request.occurrence.ordinal),
            )
            staged = _resource_boundary(
                "staging",
                stack.enter_context,
                stage_layer(source, limits=DEFAULT_LAYER_CONVERSION_LIMITS),
            )
            packed = _resource_boundary(
                "packing",
                stack.enter_context,
                pack_staged_squashfs(
                    staged,
                    packer_path=request.packer_path,
                    expected_packer_sha256=request.packer_sha256,
                    policy=DEFAULT_SQUASHFS_PACK_POLICY,
                    toolchain=toolchain,
                    execution=execution,
                ),
            )
            # Exceptions from store publication are thrown back through this
            # yield. ExitStack cleanup deliberately runs outside every phase
            # boundary so it cannot be mislabeled as source, staging or pack.
            yield staged.receipt, packed

    return _resource_boundary("store", store.materialize_observed, request.occurrence, request.key, producer)


def _category(exc: BaseException) -> str:
    if isinstance(exc, _WorkerResourceError):
        return "resource"
    if isinstance(exc, UnsupportedPlatformError):
        return "unsupported"
    if isinstance(exc, SourceLeaseError):
        return "source"
    if isinstance(exc, LayerIntakeError):
        return "intake"
    if isinstance(exc, SquashFSPackError):
        if exc.code == "oci-packer-resource":
            return "resource"
        return "toolchain" if exc.code.startswith("oci-packer-toolchain") else "pack"
    if isinstance(exc, ArtifactStoreError):
        return "store"
    if isinstance(exc, OCIStoreError):
        return "authority" if "authority" in exc.code or "root" in exc.code else "store"
    if isinstance(exc, ArtifactValidationError):
        return "source"
    if process_resource_failure(exc):
        return "resource"
    return "internal"


def _failure_details(exc: BaseException, category: str) -> tuple[str | None, int | None]:
    if category != "resource":
        return None, None
    if isinstance(exc, _WorkerResourceError):
        return exc.failure_stage, exc.failure_errno
    if isinstance(exc, SquashFSPackError) and exc.code == "oci-packer-resource":
        return exc.failure_stage, exc.failure_errno
    return None, None


def main() -> int:
    os.umask(0o077)
    payload = sys.stdin.buffer.read(MAX_OCI_WORKER_MESSAGE_BYTES + 1)
    try:
        request = OCIWorkerRequest.from_json_bytes(payload)
    except OCIWorkerProtocolError:
        return 2
    try:
        _resource_boundary("limits", _apply_resource_limits, request.cpu_limit_seconds)
        result = _materialize(request)
        response = OCIWorkerResponse(
            nonce=request.nonce,
            request_digest=request.digest,
            status="succeeded",
            result=result,
            error_category=None,
        )
    except BaseException as exc:
        category = _category(exc)
        failure_stage, failure_errno = _failure_details(exc, category)
        response = OCIWorkerResponse(
            nonce=request.nonce,
            request_digest=request.digest,
            status="failed",
            result=None,
            error_category=category,
            failure_stage=failure_stage,
            failure_errno=failure_errno,
        )
    output = response.to_json_bytes()
    try:
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
    except OSError:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
