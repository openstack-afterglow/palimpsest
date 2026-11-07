"""Exec worker owning one complete cold OCI layer materialization attempt."""

from __future__ import annotations

import os
import resource
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any, TypeVar

from .artifact_store import ArtifactStoreError
from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_changeset import EffectiveFilesystem
from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID, LayerIntakeError, stage_layer
from .oci_image import OCIImageRef, resolve_archive_image, resolve_image
from .oci_packer import (
    SQUASHFS_PACK_POLICY_ID,
    SquashFSPackError,
    SquashFSPackExecution,
    discover_squashfs_toolchain,
    pack_staged_squashfs,
    process_resource_errno,
    process_resource_failure,
)
from .oci_source import SnapshottedOCIArchive, SnapshottedOCIImage, SourceCAS, SourceLeaseError, SourceSnapshot
from .oci_store import DerivedLayerOccurrence, DerivedSquashFSKey, OCIStore, OCIStoreError, parent_chain_digest
from .oci_worker_limits import OCI_WORKER_NPROC_LIMIT
from .oci_worker_protocol import (
    MAX_OCI_WORKER_MESSAGE_BYTES,
    OCI_WORKER_CATEGORY_CODES,
    OCI_WORKER_EXCEPTION_CODES,
    OCI_WORKER_IO_CODES,
    OCI_WORKER_IO_ERRNOS,
    OCIWorkerProtocolError,
    OCIWorkerRequest,
    OCIWorkerResponse,
)
from .state import StatePaths
from .web_job_client import (
    defer_preparation_termination,
    no_force_preparation_termination,
    park_preparation_custody,
    preparation_custody_held,
)

T = TypeVar("T")

_MAX_WORKER_ADDRESS_SPACE = 40 * 1024**3
_MAX_WORKER_FILE_SIZE = 40 * 1024**3
_MAX_WORKER_FDS = 256
_MAX_WORKER_PROCESSES = OCI_WORKER_NPROC_LIMIT
_OPERATION_MARKER = object()


def _observe_operation(exc: BaseException, stage: str | None) -> None:
    marker = getattr(exc, "_oci_operation", None)
    if not (type(marker) is tuple and len(marker) == 2 and marker[0] is _OPERATION_MARKER):
        exc._oci_operation = (_OPERATION_MARKER, stage)


def _operation_stage(exc: BaseException) -> str | None:
    marker = getattr(exc, "_oci_operation", None)
    if type(marker) is tuple and len(marker) == 2 and marker[0] is _OPERATION_MARKER:
        return marker[1]
    return None


class _OperationStack(ExitStack):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        except BaseException as exc:
            # A new teardown exception has no trustworthy producer phase.
            _observe_operation(exc, None)
            raise


class _WorkerResourceError(Exception):
    """Internal fixed-fact carrier; never serialized as exception text."""

    def __init__(self, failure_stage: str, failure_errno: int | None):
        self.failure_stage = failure_stage
        self.failure_errno = failure_errno
        super().__init__("OCI worker resource operation failed")


def _resource_boundary(failure_stage: str, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return operation(*args, **kwargs)
    except BaseException as exc:
        _observe_operation(exc, failure_stage)
        if process_resource_failure(exc):
            failure = _WorkerResourceError(failure_stage, process_resource_errno(exc))
            _observe_operation(failure, _operation_stage(exc))
            raise failure from None
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


def _reconstruct_image(request: OCIWorkerRequest, cas: SourceCAS) -> SnapshottedOCIImage | SnapshottedOCIArchive:
    reference = OCIImageRef(
        registry=request.source.registry,
        repository=request.source.repository,
        requested_reference=request.source.requested_reference,
    )
    root_descriptor = request.source.index_descriptor or request.source.manifest_descriptor
    root = SourceSnapshot(root_descriptor, cas.identity)

    def reader(descriptor):
        return cas.read_metadata(SourceSnapshot(descriptor, cas.identity))

    if request.source.platform.os == "linux" and request.source.platform.architecture == "amd64":
        # Native and acquisition amd64 graphs have identical canonical bytes
        # and snapshot bindings; retain the original native reconstruction.
        image = resolve_image(reference, root_descriptor, reader)
        snapshot_type = SnapshottedOCIImage
    else:
        image = resolve_archive_image(
            reference,
            root_descriptor,
            reader,
            platform=f"{request.source.platform.os}/{request.source.platform.architecture}",
        )
        snapshot_type = SnapshottedOCIArchive
    if image.source != request.source or image.digest != request.occurrence.source_image_digest:
        raise ArtifactValidationError("worker source graph binding is inconsistent")
    snapshot = snapshot_type(
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
    policy = request.pack_profile.policy
    roots = StatePaths(request.config_root, request.state_root)
    store = _resource_boundary("store", OCIStore, roots)
    if store.identity != request.expected_store_id:
        raise OCIStoreError("oci-store-authority", "worker derived store identity does not match")
    # Warm artifacts still need an authentic prefix identity. Only the bounded
    # source graph is reopened here; layer payload replay remains cold-only.
    cas = _resource_boundary(
        "source",
        SourceCAS.open_existing,
        request.source_cas_root,
        expected_cas_id=request.expected_source_cas_id,
    )
    snapshot = _resource_boundary("source", _reconstruct_image, request, cas)
    if request.key.parent_chain_digest != parent_chain_digest(snapshot, request.occurrence.ordinal):
        raise OCIStoreError("oci-store-key", "worker parent prefix does not match the verified source graph")

    @contextmanager
    def producer():
        toolchain = _resource_boundary(
            "packing",
            discover_squashfs_toolchain,
            request.packer_path,
            expected_packer_sha256=request.packer_sha256,
            policy=policy,
            **({"no_force": True} if request.no_force else {}),
        )
        computed_key = DerivedSquashFSKey.for_occurrence(
            request.occurrence,
            parent_chain_digest=parent_chain_digest(snapshot, request.occurrence.ordinal),
            intake_policy_id=LAYER_INTAKE_POLICY_ID,
            intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
            pack_policy_id=SQUASHFS_PACK_POLICY_ID,
            pack_policy_fingerprint=policy.fingerprint,
            toolchain=toolchain,
        )
        if computed_key != request.key:
            raise OCIStoreError("oci-store-key", "worker recipe does not match the verified toolchain")
        execution = SquashFSPackExecution(
            scratch_root=Path.cwd(), inherit_process_group=not request.no_force, no_force=request.no_force
        )
        limits = DEFAULT_LAYER_CONVERSION_LIMITS
        if request.occurrence.ordinal >= 128:
            raise LayerIntakeError("oci-prefix-layer-limit", "source prefix exceeds its ordered layer limit")
        expires = time.monotonic() + limits.timeout_seconds

        def check_prefix() -> None:
            if time.monotonic() >= expires:
                raise LayerIntakeError("oci-prefix-timeout", "source prefix exceeded its cooperative deadline")

        filesystem = EffectiveFilesystem(
            max_entries=limits.max_normalized_entries,
            max_metadata_bytes=limits.max_total_metadata_bytes,
            checkpoint=check_prefix,
        )
        uncompressed = 0
        members = 0

        def record_prefix(staged) -> None:
            nonlocal uncompressed, members
            check_prefix()
            uncompressed += staged.receipt.uncompressed_size
            members += staged.receipt.members
            if uncompressed > limits.max_uncompressed_bytes or members > limits.max_members:
                raise LayerIntakeError("oci-prefix-limit", "cumulative source prefix exceeds its intake limit")
            staged.bind_parent_directories(filesystem)

        # Replay one verified occurrence at a time. No payload lease or open
        # spool from an earlier layer survives to the packer, and no caller-
        # supplied metadata can substitute for this source-CAS prefix.
        for ordinal in range(request.occurrence.ordinal):
            check_prefix()
            with _OperationStack() as stack:
                source = _resource_boundary("source", stack.enter_context, cas.lease_layer(snapshot, ordinal))
                staged = _resource_boundary(
                    "staging",
                    stack.enter_context,
                    stage_layer(source, limits=limits, checkpoint=check_prefix),
                )
                _resource_boundary("staging", record_prefix, staged)
        with _OperationStack() as stack:
            source = _resource_boundary(
                "source",
                stack.enter_context,
                cas.lease_layer(snapshot, request.occurrence.ordinal),
            )
            staged = _resource_boundary(
                "staging",
                stack.enter_context,
                stage_layer(source, limits=limits, checkpoint=check_prefix),
            )
            _resource_boundary("staging", record_prefix, staged)
            packed = _resource_boundary(
                "packing",
                stack.enter_context,
                pack_staged_squashfs(
                    staged,
                    packer_path=request.packer_path,
                    expected_packer_sha256=request.packer_sha256,
                    policy=policy,
                    toolchain=toolchain,
                    execution=execution,
                ),
            )
            # Exceptions from store publication are thrown back through this
            # yield. ExitStack cleanup deliberately runs outside every phase
            # boundary so it cannot be mislabeled as source, staging or pack.
            try:
                yield staged.receipt, packed
            except BaseException as exc:
                _observe_operation(exc, "store")
                raise

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


def _failure_code(exc: BaseException, category: str) -> str | None:
    allowed = OCI_WORKER_CATEGORY_CODES.get(category, ())
    pending = [exc]
    seen = set()
    for _ in range(16):
        if not pending:
            break
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        for kind, codes in OCI_WORKER_EXCEPTION_CODES.items():
            if isinstance(cause, kind):
                code = cause.code
                if type(code) is str and code in codes and code in allowed:
                    return code
                break
        if cause.__context__ is not None:
            pending.append(cause.__context__)
        if cause.__cause__ is not None and cause.__cause__ is not cause.__context__:
            pending.append(cause.__cause__)
    return None


def _io_errno(exc: BaseException, code: str | None) -> int | None:
    if code not in OCI_WORKER_IO_CODES:
        return None
    pending = [exc]
    seen = set()
    for _ in range(16):
        if not pending:
            break
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, OSError) and type(cause.errno) is int and cause.errno in OCI_WORKER_IO_ERRNOS:
            return cause.errno
        if cause.__context__ is not None:
            pending.append(cause.__context__)
        if cause.__cause__ is not None and cause.__cause__ is not cause.__context__:
            pending.append(cause.__cause__)
    return None


def main() -> int:
    os.umask(0o077)
    payload = sys.stdin.buffer.read(MAX_OCI_WORKER_MESSAGE_BYTES + 1)
    try:
        request = OCIWorkerRequest.from_json_bytes(payload)
    except OCIWorkerProtocolError:
        return 2
    with no_force_preparation_termination() if request.no_force else nullcontext():
        return _run_request(request)


def _run_request(request: OCIWorkerRequest) -> int:
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
        with defer_preparation_termination():
            if request.no_force and preparation_custody_held():
                park_preparation_custody()
            category = _category(exc)
            failure_stage, failure_errno = _failure_details(exc, category)
            failure_code = _failure_code(exc, category)
            response = OCIWorkerResponse(
                nonce=request.nonce,
                request_digest=request.digest,
                status="failed",
                result=None,
                error_category=category,
                failure_stage=failure_stage,
                failure_errno=failure_errno,
                failure_code=failure_code,
                io_errno=_io_errno(exc, failure_code),
                operation_stage=_operation_stage(exc),
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
