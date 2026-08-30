"""Fail-closed typed routing for create and existing-run lifecycle operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from . import cloud_runtime, lima, log_stream, platforms, state
from .errors import StateError
from .refs import RunSpec, VolumeAttachment
from .runtime_types import (
    ALLOWED_RUNTIME_STATUSES,
    CapabilityErrorCategory,
    CloudImageInspectDetail,
    CommitResult,
    DispatchKey,
    ExecRequest,
    ExistingRunRecord,
    ExpectedRunIdentity,
    InspectBase,
    InspectLayer,
    InspectLifecycle,
    InspectPort,
    InspectRecord,
    InspectSshEndpoint,
    InspectVolume,
    LifecycleCursor,
    LifecycleResult,
    LifecycleWarningCategory,
    LogMode,
    LogStream,
    PreflightReport,
    PreflightReportPurpose,
    ProcessSession,
    ResolvedRunRequest,
    RunAggregationError,
    RunAggregationResult,
    RunAttachmentMode,
    RunRequestProvenance,
    RunRequestProvenanceStage,
    RunResult,
    RunSummary,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
    RuntimePreflightError,
    RunVolumeIntent,
    _lifecycle_outcome_authentication_tag,
    _LifecycleAdapterOutcome,
    existing_record_subject_digest,
    run_request_subject_digest,
    snapshot_cloud_init,
)
from .state import StatePaths

_RECEIPT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RUN_REQUEST_AUTHENTICATION_KEY = secrets.token_bytes(32)
_VOLUME_BINDING_AUTHENTICATION_KEY = secrets.token_bytes(32)
_PROJECT_VOLUME_BINDING_AUTHORITY = object()
_VOLUME_BINDING_RECEIPT_TTL_NS = 60 * 1_000_000_000
_MAX_ISSUED_VOLUME_BINDING_RECEIPTS = 4096
_ISSUED_VOLUME_BINDING_RECEIPTS: dict[str, tuple[str, int]] = {}
_VOLUME_BINDING_RECEIPT_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class _CreationReceipt:
    """Allowlisted adapter identity fields used only for post-create binding."""

    name: str
    run_id: str
    dispatch_key: DispatchKey
    status: str


@dataclass(frozen=True, slots=True, repr=False)
class _VolumeBindingReceipt:
    issuer_nonce: str
    expires_at_monotonic_ns: int
    authentication_tag: str


def _attachment_subject_digest(request: ResolvedRunRequest) -> str:
    payload = [
        {
            "name": volume.name,
            "mount_path": volume.mount_path,
            "host_path": str(volume.host_path) if volume.host_path is not None else None,
            "backend_name": volume.backend_name,
            "filesystem": volume.filesystem,
            "read_only": volume.read_only,
            "format": volume.format,
        }
        for volume in request.spec.volumes
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _final_attachment_subject_digest(final_spec: RunSpec) -> str:
    payload = [
        {
            "name": volume.name,
            "mount_path": volume.mount_path,
            "host_path": str(volume.host_path) if volume.host_path is not None else None,
            "backend_name": volume.backend_name,
            "filesystem": volume.filesystem,
            "read_only": volume.read_only,
            "format": volume.format,
        }
        for volume in final_spec.volumes
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _volume_binding_authentication_tag(
    request: ResolvedRunRequest,
    final_spec: RunSpec,
    dispatch_key: DispatchKey,
    issuer_nonce: str,
    expires_at_monotonic_ns: int,
) -> str:
    payload = {
        "nonce": issuer_nonce,
        "expires_at_monotonic_ns": expires_at_monotonic_ns,
        "logical_subject": run_request_subject_digest(request),
        "logical_provenance_nonce": request.provenance.issuer_nonce if request.provenance is not None else None,
        "attachment_subject": _final_attachment_subject_digest(final_spec),
        "dispatch_key": [dispatch_key.runtime_kind.value, dispatch_key.backend.value],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_VOLUME_BINDING_AUTHENTICATION_KEY, encoded, hashlib.sha256).hexdigest()


def _issue_volume_binding_receipt(
    request: ResolvedRunRequest,
    final_spec: RunSpec,
    *,
    dispatch_key: DispatchKey,
    _authority: object,
    now_ns: int | None = None,
) -> _VolumeBindingReceipt:
    """Project preparation's private handoff after it verifies managed sources."""

    if _authority is not _PROJECT_VOLUME_BINDING_AUTHORITY:
        raise StateError("volume binding receipt issuer is unauthorized")
    if not isinstance(request, ResolvedRunRequest) or not isinstance(final_spec, RunSpec):
        raise TypeError("volume binding receipt requires a resolved request and final RunSpec")
    if not isinstance(dispatch_key, DispatchKey) or dispatch_key != request.dispatch_key:
        raise StateError("volume binding receipt dispatch identity is invalid")
    _require_run_request_provenance(request)
    if request.attachments_bound:
        raise StateError("volume binding receipt requires a logical request")
    issued = time.monotonic_ns() if now_ns is None else now_ns
    if type(issued) is not int or issued < 0:
        raise StateError("volume binding receipt clock is invalid")
    expires = issued + _VOLUME_BINDING_RECEIPT_TTL_NS
    nonce = secrets.token_hex(32)
    tag = _volume_binding_authentication_tag(request, final_spec, dispatch_key, nonce, expires)
    with _VOLUME_BINDING_RECEIPT_LOCK:
        _prune_volume_binding_receipts(issued)
        if len(_ISSUED_VOLUME_BINDING_RECEIPTS) >= _MAX_ISSUED_VOLUME_BINDING_RECEIPTS:
            raise StateError("volume binding receipt capacity is exhausted")
        _ISSUED_VOLUME_BINDING_RECEIPTS[nonce] = (tag, expires)
    return _VolumeBindingReceipt(nonce, expires, tag)


def _prune_volume_binding_receipts(now_ns: int, *, preserve_nonce: str | None = None) -> None:
    for nonce, (_, expires) in tuple(_ISSUED_VOLUME_BINDING_RECEIPTS.items()):
        if nonce != preserve_nonce and expires <= now_ns:
            del _ISSUED_VOLUME_BINDING_RECEIPTS[nonce]


def _consume_volume_binding_receipt(
    receipt: _VolumeBindingReceipt | None,
    request: ResolvedRunRequest,
    final_spec: RunSpec,
    dispatch_key: DispatchKey,
    *,
    now_ns: int | None = None,
) -> None:
    if not isinstance(receipt, _VolumeBindingReceipt):
        raise StateError("volume binding requires a verifier-issued receipt")
    now = time.monotonic_ns() if now_ns is None else now_ns
    if type(now) is not int or now < 0:
        raise StateError("volume binding receipt clock is invalid")
    expected = _volume_binding_authentication_tag(
        request,
        final_spec,
        dispatch_key,
        receipt.issuer_nonce,
        receipt.expires_at_monotonic_ns,
    )
    with _VOLUME_BINDING_RECEIPT_LOCK:
        _prune_volume_binding_receipts(now, preserve_nonce=receipt.issuer_nonce)
        registered = _ISSUED_VOLUME_BINDING_RECEIPTS.pop(receipt.issuer_nonce, None)
    if (
        registered is None
        or not hmac.compare_digest(receipt.authentication_tag, expected)
        or not hmac.compare_digest(registered[0], receipt.authentication_tag)
    ):
        raise StateError("volume binding receipt could not be verified")
    if now >= receipt.expires_at_monotonic_ns:
        raise StateError("volume binding receipt has expired")


def _run_request_authentication_tag(
    request: ResolvedRunRequest,
    *,
    stage: RunRequestProvenanceStage,
    issuer_nonce: str,
) -> str:
    payload = {
        "stage": stage.value,
        "nonce": issuer_nonce,
        "logical_subject": run_request_subject_digest(request),
        "attachment_subject": _attachment_subject_digest(request),
        "attachments_bound": request.attachments_bound,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_RUN_REQUEST_AUTHENTICATION_KEY, encoded, hashlib.sha256).hexdigest()


def _issue_run_request_provenance(
    request: ResolvedRunRequest,
    stage: RunRequestProvenanceStage,
) -> ResolvedRunRequest:
    nonce = secrets.token_hex(32)
    provenance = RunRequestProvenance(
        stage,
        nonce,
        _run_request_authentication_tag(request, stage=stage, issuer_nonce=nonce),
    )
    return replace(request, provenance=provenance)


def _require_run_request_provenance(request: ResolvedRunRequest) -> None:
    provenance = request.provenance
    expected_stage = RunRequestProvenanceStage.BOUND if request.attachments_bound else RunRequestProvenanceStage.LOGICAL
    if not isinstance(provenance, RunRequestProvenance) or provenance.stage is not expected_stage:
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "resolved run request was not issued by the runtime resolver",
        )
    expected_tag = _run_request_authentication_tag(
        request,
        stage=provenance.stage,
        issuer_nonce=provenance.issuer_nonce,
    )
    if not hmac.compare_digest(provenance.authentication_tag, expected_tag):
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "resolved run request provenance could not be verified",
        )


def _parse_creation_receipt(raw: Any) -> _CreationReceipt:
    if not isinstance(raw, Mapping):
        raise StateError("runtime adapter returned an invalid creation receipt")
    name = raw.get("name")
    run_id = raw.get("run_id")
    raw_backend = raw.get("backend")
    status = raw.get("status")
    if (
        not isinstance(name, str)
        or _RECEIPT_NAME_RE.fullmatch(name) is None
        or not isinstance(run_id, str)
        or status != "running"
    ):
        raise StateError("runtime adapter returned an invalid creation receipt")
    parsed_run_id: uuid.UUID | None = None
    try:
        parsed_run_id = uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError):
        pass
    if parsed_run_id is None or str(parsed_run_id) != run_id:
        raise StateError("runtime adapter returned an invalid creation receipt")
    backend: RuntimeBackend | None = None
    try:
        backend = RuntimeBackend(raw_backend)
    except (TypeError, ValueError):
        pass
    if backend is None:
        raise StateError("runtime adapter returned an invalid creation receipt")
    dispatch_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, backend)
    return _CreationReceipt(name, run_id, dispatch_key, status)


def _validate_resolved_run_network(dispatch_key: DispatchKey, network: str | None) -> None:
    if (
        dispatch_key.backend is RuntimeBackend.LIBVIRT_HVF
        and network is not None
        and network not in {"none", "default"}
    ):
        raise StateError("libvirt-hvf supports only none or default networking")


def resolve_run_request(
    spec: RunSpec,
    *,
    runtime_kind: RuntimeKind = RuntimeKind.CLOUD_IMAGE,
    requested_backend: str = "auto",
    require_volume_binding: bool = False,
    volume_intents: tuple[RunVolumeIntent, ...] | None = None,
) -> ResolvedRunRequest:
    """Resolve a typed create request without backend or state side effects."""

    if not isinstance(runtime_kind, RuntimeKind):
        raise TypeError("run resolver requires a RuntimeKind")
    if runtime_kind is RuntimeKind.OCI_ROOT:
        raise RuntimeCapabilityError(
            RuntimeOperation.RUN,
            DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        )
    if not isinstance(spec, RunSpec):
        raise TypeError("run resolver requires a RunSpec")
    if spec.volumes:
        raise StateError("physical volume attachments require the private project volume binder")
    if type(require_volume_binding) is not bool:
        raise TypeError("volume binding requirement must be a boolean")
    frozen_cloud_init = snapshot_cloud_init(spec.cloud_init)
    resolved_spec = spec if frozen_cloud_init is spec.cloud_init else replace(spec, cloud_init=frozen_cloud_init)
    resolved_intents = () if volume_intents is None else volume_intents
    backend = RuntimeBackend(platforms.select_backend(resolved_spec.stack.base.arch, requested=requested_backend))
    dispatch_key = DispatchKey(RuntimeKind.CLOUD_IMAGE, backend)
    _validate_resolved_run_network(dispatch_key, resolved_spec.network)
    request = ResolvedRunRequest(
        dispatch_key=dispatch_key,
        spec=resolved_spec,
        volume_intents=resolved_intents,
        attachments_bound=not require_volume_binding,
    )
    stage = RunRequestProvenanceStage.BOUND if request.attachments_bound else RunRequestProvenanceStage.LOGICAL
    return _issue_run_request_provenance(request, stage)


def preflight_run_request(
    request: ResolvedRunRequest,
    *,
    host: platforms.HostPlatform | None = None,
    now_ns: int | None = None,
) -> PreflightReport:
    """Return one successful, short-lived report bound to logical create intent."""

    if not isinstance(request, ResolvedRunRequest):
        raise TypeError("run preflight requires a ResolvedRunRequest")
    _require_run_request_provenance(request)
    _validate_resolved_run_network(request.dispatch_key, request.spec.network)
    profile = platforms.capability_profile(
        request.dispatch_key,
        RuntimeOperation.RUN,
        network=request.spec.network,
    )
    report = platforms.evaluate_capability_profile(
        profile,
        subject_digest=run_request_subject_digest(request),
        arch=request.spec.stack.base.arch,
        host=host,
        now_ns=now_ns,
    )
    if not report.successful:
        failed = next(item for item in report.checks if not item.passed)
        raise RuntimePreflightError(
            failed.error_category or CapabilityErrorCategory.CHECK_FAILED,
            failed.remediation or "runtime capability preflight failed",
            report=report,
            capability_id=failed.capability_id,
        )
    return report


def preflight_run_capabilities(
    arch: str,
    *,
    requested_backend: str = "auto",
    network: str | None = None,
    host: platforms.HostPlatform | None = None,
) -> DispatchKey:
    """Read-only early gate used before a remote image pull mutates local storage.

    ``network=None`` intentionally selects the host/tool-only profile for
    Compose, where the exact service network is resolved later but still
    before project volume or runtime mutation. Direct run callers pass the
    exact requested network.
    """

    resolved_host = host or platforms.detect_host()
    backend = RuntimeBackend(platforms.select_backend(arch, host=resolved_host, requested=requested_backend))
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, backend)
    _validate_resolved_run_network(key, network)
    profile = platforms.capability_profile(key, RuntimeOperation.RUN, network=network)
    raw_subject = f"early-run:{profile.profile_id}:{arch}:{network}".encode()
    report = platforms.evaluate_capability_profile(
        profile,
        subject_digest="sha256:" + hashlib.sha256(raw_subject).hexdigest(),
        arch=arch,
        host=resolved_host,
        purpose=PreflightReportPurpose.DISCOVERY,
    )
    if not report.successful:
        failed = next(item for item in report.checks if not item.passed)
        raise RuntimePreflightError(
            failed.error_category or CapabilityErrorCategory.CHECK_FAILED,
            failed.remediation or "runtime capability preflight failed",
            report=report,
            capability_id=failed.capability_id,
        )
    return key


def require_run_preflight(
    request: ResolvedRunRequest,
    report: PreflightReport | None,
    *,
    now_ns: int | None = None,
) -> None:
    """Fail before mutation unless ``report`` matches current request/profile/freshness."""

    if not isinstance(request, ResolvedRunRequest):
        raise TypeError("run preflight validation requires a ResolvedRunRequest")
    if not isinstance(report, PreflightReport):
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "run requires an issuer-authenticated preflight report",
        )
    _require_run_request_provenance(request)
    current_profile = platforms.capability_profile(
        request.dispatch_key,
        RuntimeOperation.RUN,
        network=request.spec.network,
    )
    platforms.consume_capability_report(
        report,
        expected_profile=current_profile,
        expected_subject_digest=run_request_subject_digest(request),
        now_ns=now_ns,
    )


def bind_run_request_volumes(
    request: ResolvedRunRequest,
    final_spec: RunSpec,
    *,
    dispatch_key: DispatchKey,
    receipt: _VolumeBindingReceipt | None = None,
) -> ResolvedRunRequest:
    """Bind prepared physical volumes without changing any logical run input."""

    if not isinstance(request, ResolvedRunRequest) or not isinstance(final_spec, RunSpec):
        raise TypeError("volume binding requires a resolved request and final RunSpec")
    _require_run_request_provenance(request)
    if request.attachments_bound:
        raise StateError("volume binding requires an unbound logical request")
    if not isinstance(dispatch_key, DispatchKey) or dispatch_key != request.dispatch_key:
        raise StateError("prepared run request changed its dispatch identity")
    if not isinstance(final_spec.volumes, tuple) or not all(
        isinstance(volume, VolumeAttachment) for volume in final_spec.volumes
    ):
        raise StateError("prepared volume attachments are invalid")
    try:
        final_intents = tuple(
            RunVolumeIntent(volume.name, volume.mount_path, volume.filesystem, volume.read_only)
            for volume in final_spec.volumes
        )
    except (AttributeError, TypeError, ValueError):
        raise StateError("prepared volume attachments are invalid") from None
    if final_intents != request.volume_intents:
        raise StateError("prepared volume attachments changed logical volume intent")
    backend = request.dispatch_key.backend
    sources_match = all(
        (volume.backend_name is not None if backend is RuntimeBackend.LIMA_VZ else volume.host_path is not None)
        for volume in final_spec.volumes
    )
    if not sources_match:
        raise StateError("prepared volume attachment source does not match resolved backend")
    logical = request.spec
    unchanged = (
        logical.name == final_spec.name
        and logical.stack == final_spec.stack
        and logical.memory_mib == final_spec.memory_mib
        and logical.vcpus == final_spec.vcpus
        and logical.network == final_spec.network
        and logical.writable_overlay == final_spec.writable_overlay
        and logical.seed == final_spec.seed
        and logical.ports == final_spec.ports
        and logical.environment == final_spec.environment
        and logical.cloud_init is final_spec.cloud_init
    )
    if not unchanged:
        raise StateError("prepared volume binding changed immutable run inputs")
    _consume_volume_binding_receipt(receipt, request, final_spec, dispatch_key)
    bound = ResolvedRunRequest(
        dispatch_key=request.dispatch_key,
        spec=final_spec,
        volume_intents=request.volume_intents,
        attachments_bound=True,
    )
    return _issue_run_request_provenance(bound, RunRequestProvenanceStage.BOUND)


def run(
    request: ResolvedRunRequest,
    *,
    preflight: PreflightReport | None = None,
    roots: StatePaths | None = None,
) -> RunResult:
    """Create one VM through the exact already-resolved backend adapter.

    A successful report is consumed before state-root initialization or adapter
    entry. Direct cloud compatibility APIs retain their historical behavior.
    """

    if not isinstance(request, ResolvedRunRequest):
        raise TypeError("runtime run requires a ResolvedRunRequest")
    _require_run_request_provenance(request)
    if not request.attachments_bound:
        raise StateError("run request volumes have not been prepared")
    resolved_roots = roots or state.resolve_roots()
    require_run_preflight(request, preflight)
    resolved_roots = state.init_resolved_roots(resolved_roots)
    if request.dispatch_key.backend is RuntimeBackend.LIMA_VZ:
        raw_result = lima.run(request.spec, roots=resolved_roots)
    else:
        profile = platforms.resolve_domain_profile(
            request.dispatch_key.backend.value,
            request.spec.stack.base.arch,
        )
        raw_result = cloud_runtime.run(request.spec, roots=resolved_roots, profile=profile)

    receipt = _parse_creation_receipt(raw_result)
    if receipt.name != request.spec.name or receipt.dispatch_key != request.dispatch_key:
        raise StateError("runtime adapter creation receipt does not match resolved request")
    snapshot = state.read_run_ledger_snapshot(resolved_roots, request.spec.name)
    if (
        snapshot.record.name != receipt.name
        or snapshot.record.run_id != receipt.run_id
        or snapshot.record.dispatch_key != receipt.dispatch_key
    ):
        raise StateError("created run ledger identity does not match resolved request")
    status = snapshot.state.get("status")
    if status != receipt.status:
        raise StateError("created run ledger did not reach running status")
    guest_ip = snapshot.state.get("guest_ip")
    try:
        return RunResult(
            record=snapshot.record,
            status=status,
            ready=True,
            attachment_mode=RunAttachmentMode.DETACHED,
            guest_ip=guest_ip,
        )
    except (TypeError, ValueError):
        raise StateError("created run ledger has invalid result fields") from None


def resolve_existing_run(name: str, *, roots: StatePaths | None = None) -> ExistingRunRecord:
    """Resolve a run exclusively from its validated durable owner and state ledgers."""
    resolved_roots = roots or state.resolve_roots()
    return state.read_run_dispatch_record(resolved_roots, name)


def preflight_existing_record(
    record: ExistingRunRecord,
    operation: RuntimeOperation,
    *,
    host: platforms.HostPlatform | None = None,
    now_ns: int | None = None,
) -> PreflightReport:
    """Issue one authenticated operation report bound to an exact run record."""

    if not isinstance(record, ExistingRunRecord) or not isinstance(operation, RuntimeOperation):
        raise TypeError("existing run preflight requires a record and operation")
    profile = platforms.capability_profile(record.dispatch_key, operation)
    report = platforms.evaluate_capability_profile(
        profile,
        subject_digest=existing_record_subject_digest(record),
        host=host,
        now_ns=now_ns,
    )
    if not report.successful:
        failed = next(item for item in report.checks if not item.passed)
        raise RuntimePreflightError(
            failed.error_category or CapabilityErrorCategory.CHECK_FAILED,
            failed.remediation or "runtime capability preflight failed",
            report=report,
            capability_id=failed.capability_id,
        )
    return report


def require_existing_preflight(
    record: ExistingRunRecord,
    operation: RuntimeOperation,
    report: PreflightReport | None,
    *,
    host: platforms.HostPlatform | None = None,
    now_ns: int | None = None,
) -> None:
    """Authenticate and consume one report for an exact record operation."""

    if not isinstance(record, ExistingRunRecord) or not isinstance(operation, RuntimeOperation):
        raise TypeError("existing run preflight validation requires a record and operation")
    if not isinstance(report, PreflightReport):
        raise RuntimePreflightError(
            CapabilityErrorCategory.REPORT_PROVENANCE,
            "existing run operation requires an issuer-authenticated preflight report",
        )
    platforms.consume_capability_report(
        report,
        expected_profile=platforms.capability_profile(record.dispatch_key, operation),
        expected_subject_digest=existing_record_subject_digest(record),
        host=host,
        now_ns=now_ns,
    )


def _adapter_for(record: ExistingRunRecord, operation: RuntimeOperation) -> Any:
    if record.dispatch_key.runtime_kind is RuntimeKind.OCI_ROOT:
        raise RuntimeCapabilityError(operation, record.dispatch_key)
    if record.dispatch_key.backend is RuntimeBackend.LIMA_VZ:
        return lima
    return cloud_runtime


def _revalidate_bound_record(record: ExistingRunRecord, roots: StatePaths) -> None:
    current = state.read_run_dispatch_record(roots, record.name)
    if current != record:
        raise StateError("run ledger changed during dispatch")


def _preflight_existing_adapter(
    record: ExistingRunRecord,
    operation: RuntimeOperation,
    roots: StatePaths,
) -> Any:
    report = preflight_existing_record(record, operation)
    adapter = _adapter_for(record, operation)
    _revalidate_bound_record(record, roots)
    require_existing_preflight(record, operation, report)
    return adapter


def _require_expected_identity(record: ExistingRunRecord, expected_identity: ExpectedRunIdentity | None) -> None:
    if expected_identity is None:
        return
    if not isinstance(expected_identity, ExpectedRunIdentity):
        raise StateError("invalid expected run identity")
    if (
        record.name != expected_identity.name
        or record.run_id != expected_identity.run_id
        or record.dispatch_key != expected_identity.dispatch_key
    ):
        raise StateError("run identity changed before lifecycle operation")


def _require_process_session(candidate: Any) -> ProcessSession:
    if not isinstance(candidate, ProcessSession):
        raise StateError("runtime adapter returned an invalid process session")
    return candidate


def _same_run_identity(left: ExistingRunRecord, right: ExistingRunRecord) -> bool:
    return left.name == right.name and left.run_id == right.run_id and left.dispatch_key == right.dispatch_key


@dataclass(frozen=True, slots=True)
class _NormalizedLifecycleOutcome:
    record: ExistingRunRecord
    previous_status: str
    previous_revision: int
    status: str
    revision: int
    warning_category: Any
    fallback_used: bool


def _normalize_lifecycle_outcome(candidate: object) -> _NormalizedLifecycleOutcome:
    normalized: _NormalizedLifecycleOutcome | None = None
    try:
        if type(candidate) is not _LifecycleAdapterOutcome:
            raise TypeError
        raw_record = candidate.record
        if type(raw_record) is not ExistingRunRecord:
            raise TypeError
        raw_key = raw_record.dispatch_key
        if (
            type(raw_record.name) is not str
            or type(raw_record.run_id) is not str
            or type(raw_record.state_schema_version) is not int
            or type(raw_key) is not DispatchKey
            or type(raw_key.runtime_kind) is not RuntimeKind
            or type(raw_key.backend) is not RuntimeBackend
        ):
            raise TypeError
        clean_record = ExistingRunRecord(
            raw_record.name,
            raw_record.run_id,
            raw_record.state_schema_version,
            DispatchKey(raw_key.runtime_kind, raw_key.backend),
        )
        previous_status = candidate.previous_status
        previous_revision = candidate.previous_revision
        current_status = candidate.status
        revision = candidate.revision
        warning_category = candidate.warning_category
        fallback_used = candidate.fallback_used
        authentication_tag = candidate.authentication_tag
        if (
            type(previous_status) is not str
            or type(previous_revision) is not int
            or previous_revision < 0
            or type(current_status) is not str
            or type(revision) is not int
            or revision < 0
            or type(fallback_used) is not bool
            or type(authentication_tag) is not bytes
            or len(authentication_tag) != 32
            or not (
                warning_category is None
                or warning_category is LifecycleWarningCategory.FORCED_SHUTDOWN
                or warning_category is LifecycleWarningCategory.BACKEND_RECONCILED
            )
        ):
            raise TypeError
        expected_tag = _lifecycle_outcome_authentication_tag(
            clean_record,
            previous_status,
            previous_revision,
            current_status,
            revision,
            warning_category,
            fallback_used,
        )
        if not hmac.compare_digest(authentication_tag, expected_tag):
            raise ValueError
        normalized = _NormalizedLifecycleOutcome(
            clean_record,
            previous_status,
            previous_revision,
            current_status,
            revision,
            warning_category,
            fallback_used,
        )
    except Exception:
        pass
    if normalized is None:
        raise StateError("runtime adapter returned an invalid lifecycle receipt")
    return normalized


def _validate_lifecycle_source(operation: RuntimeOperation, status: object) -> None:
    allowed_sources = {
        RuntimeOperation.START: ALLOWED_RUNTIME_STATUSES[RuntimeKind.CLOUD_IMAGE],
        RuntimeOperation.STOP: ALLOWED_RUNTIME_STATUSES[RuntimeKind.CLOUD_IMAGE],
        RuntimeOperation.RM: ALLOWED_RUNTIME_STATUSES[RuntimeKind.CLOUD_IMAGE],
    }[operation]
    if type(status) is not str or status not in allowed_sources:
        raise StateError("runtime lifecycle operation has an invalid source status")


def _dispatch_lifecycle(
    name: str,
    operation: RuntimeOperation,
    *,
    roots: StatePaths | None,
    expected_identity: ExpectedRunIdentity | None,
    volumes: bool = False,
) -> LifecycleResult:
    resolved_roots = roots or state.resolve_roots()
    before = state.read_run_ledger_snapshot(resolved_roots, name)
    record = before.record
    _require_expected_identity(record, expected_identity)
    previous_status = before.state.get("status")
    previous_revision = state.lifecycle_revision(before)
    _validate_lifecycle_source(operation, previous_status)
    # The durable status alone cannot prove a no-op: Lima or libvirt may need
    # backend reconciliation even when the final status is unchanged.  At the
    # saturated revision, reject every lifecycle operation before preflight or
    # adapter entry rather than risk an external effect that cannot be recorded.
    if previous_revision == 2**63 - 1:
        raise StateError("lifecycle revision cannot be incremented")
    adapter = _preflight_existing_adapter(record, operation, resolved_roots)
    # Rebind after the one-use preflight is consumed, then pin a complete
    # snapshot. This closes swaps that happen after the adapter selection read
    # but before the adapter acquires its mutation lock.
    _revalidate_bound_record(record, resolved_roots)
    bound = state.read_run_ledger_snapshot(resolved_roots, name)
    if bound.record != record or bound.state != before.state:
        raise StateError("run ledger changed during dispatch")
    if operation is RuntimeOperation.START:
        raw = adapter.start(
            name,
            roots=resolved_roots,
            _expected_record=record,
            _expected_snapshot=bound,
        )
    elif operation is RuntimeOperation.STOP:
        raw = adapter.stop(
            name,
            roots=resolved_roots,
            _expected_record=record,
            _expected_snapshot=bound,
        )
    else:
        raw = adapter.rm(
            name,
            roots=resolved_roots,
            volumes=volumes,
            _expected_record=record,
            _expected_snapshot=bound,
        )
    raw = _normalize_lifecycle_outcome(raw)
    if not _same_run_identity(raw.record, record):
        raise StateError("runtime adapter lifecycle receipt changed run identity")
    if raw.record.state_schema_version not in {record.state_schema_version, 2}:
        raise StateError("runtime adapter lifecycle receipt changed state schema")
    valid_transition = (
        operation is RuntimeOperation.START
        and (raw.previous_status, raw.status) in {("stopped", "running"), ("running", "running")}
        or operation is RuntimeOperation.STOP
        and (
            raw.previous_status != "removed"
            and raw.status == "stopped"
            or (raw.previous_status, raw.status) in {("stopped", "stopped"), ("removed", "removed")}
        )
        or operation is RuntimeOperation.RM
        and raw.status == "removed"
    )
    if not valid_transition:
        raise StateError("runtime adapter lifecycle receipt has an invalid terminal status")
    if (raw.previous_status, raw.previous_revision) != (previous_status, previous_revision):
        raise StateError("runtime adapter lifecycle receipt does not match the bound state")
    deletion_rewrite = (
        operation is RuntimeOperation.RM
        and volumes
        and previous_status == "removed"
        and raw.status == "removed"
        and raw.revision == previous_revision + 1
    )
    backend_reconciled = (
        raw.warning_category is LifecycleWarningCategory.BACKEND_RECONCILED
        and operation in {RuntimeOperation.START, RuntimeOperation.STOP}
        and raw.status == previous_status
        and raw.revision > previous_revision
    )
    if raw.warning_category is LifecycleWarningCategory.BACKEND_RECONCILED and not backend_reconciled:
        raise StateError("runtime adapter lifecycle receipt has invalid recovery metadata")
    if raw.status == previous_status:
        revision_valid = raw.revision == previous_revision or deletion_rewrite or backend_reconciled
    else:
        revision_valid = (
            raw.revision > previous_revision and raw.warning_category is not LifecycleWarningCategory.BACKEND_RECONCILED
        )
    if not revision_valid:
        raise StateError("runtime adapter lifecycle receipt has an invalid revision")
    if raw.fallback_used and operation is not RuntimeOperation.STOP:
        raise StateError("runtime adapter lifecycle receipt has invalid fallback metadata")

    removed_tree = operation is RuntimeOperation.RM and volumes
    if removed_tree:
        if state.run_entry_present_or_ambiguous(resolved_roots, name):
            raise StateError("removed run ledger is still present")
    else:
        after = state.read_run_ledger_snapshot(resolved_roots, name)
        if (
            after.record != raw.record
            or after.state.get("status") != raw.status
            or state.lifecycle_revision(after) != raw.revision
        ):
            raise StateError("runtime adapter lifecycle receipt does not match durable state")
    try:
        return LifecycleResult(
            raw.record,
            operation,
            previous_status,
            raw.status,
            LifecycleCursor(raw.record, raw.revision),
            raw.warning_category,
            raw.fallback_used,
        )
    except (TypeError, ValueError):
        raise StateError("runtime adapter returned an invalid lifecycle receipt") from None


def exec(
    name: str,
    argv: Sequence[str],
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> ProcessSession:
    request = ExecRequest.from_argv(argv)
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _preflight_existing_adapter(record, RuntimeOperation.EXEC, resolved_roots)
    return _require_process_session(
        adapter.exec_session(
            name,
            request,
            roots=resolved_roots,
            _expected_record=record,
        )
    )


def shell(
    name: str,
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> ProcessSession:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _preflight_existing_adapter(record, RuntimeOperation.SHELL, resolved_roots)
    return _require_process_session(
        adapter.shell_session(
            name,
            roots=resolved_roots,
            _expected_record=record,
        )
    )


def commit(
    name: str,
    tag: str,
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> CommitResult:
    """Capture one exact cloud VM run through a typed, fail-closed receipt."""

    resolved_roots = roots or state.resolve_roots()
    state.validate_tag(tag)
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _preflight_existing_adapter(record, RuntimeOperation.COMMIT, resolved_roots)
    raw = adapter.commit(
        name,
        tag,
        roots=resolved_roots,
        _expected_record=record,
    )
    expected_keys = {
        "tag",
        "digest",
        "size_bytes",
        "parent_digest",
        "base_image_digest",
        "source",
    }
    try:
        if type(raw) is not dict or set(raw) != expected_keys or raw["source"] != "commit":
            raise TypeError
        return CommitResult(
            record=record,
            tag=raw["tag"],
            digest=raw["digest"],
            size_bytes=raw["size_bytes"],
            parent_digest=raw["parent_digest"],
            base_image_digest=raw["base_image_digest"],
        )
    except (KeyError, TypeError, ValueError):
        raise StateError("runtime adapter returned an invalid commit receipt") from None


def start(
    name: str,
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> LifecycleResult:
    return _dispatch_lifecycle(
        name,
        RuntimeOperation.START,
        roots=roots,
        expected_identity=expected_identity,
    )


def stop(
    name: str,
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> LifecycleResult:
    return _dispatch_lifecycle(
        name,
        RuntimeOperation.STOP,
        roots=roots,
        expected_identity=expected_identity,
    )


def rm(
    name: str,
    *,
    volumes: bool = False,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> LifecycleResult:
    return _dispatch_lifecycle(
        name,
        RuntimeOperation.RM,
        roots=roots,
        expected_identity=expected_identity,
        volumes=volumes,
    )


def inspect_run(name: str, *, roots: StatePaths | None = None) -> InspectRecord:
    """Return one state-only, allowlisted projection of a pinned ledger snapshot."""

    resolved_roots = roots or state.resolve_roots()
    snapshot = state.read_run_ledger_snapshot(resolved_roots, name)
    # This declarative lookup both records the state-only operation contract and
    # keeps unsupported runtime kinds fail-closed without probing the host.
    platforms.capability_profile(snapshot.record.dispatch_key, RuntimeOperation.INSPECT)
    try:
        return _project_inspect(snapshot)
    except (RecursionError, TypeError, ValueError):
        raise StateError("invalid run ledger") from None


def logs(
    name: str,
    *,
    roots: StatePaths | None = None,
    follow: bool = False,
    expected_identity: ExpectedRunIdentity | None = None,
) -> LogStream:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    platforms.capability_profile(record.dispatch_key, RuntimeOperation.LOGS)
    mode = LogMode.FOLLOW if follow else LogMode.SNAPSHOT
    return log_stream.open_retained_console_stream(resolved_roots, record, mode)


def _optional_string(raw: Any, field: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError("run ledger contains an invalid public field")
    return value


def _optional_integer(raw: Any, field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if type(value) is not int:
        raise StateError("run ledger contains an invalid public field")
    return value


def _project_mapping_items(
    raw_items: Any,
    *,
    fields: Mapping[str, type],
    required: frozenset[str] = frozenset(),
) -> tuple[MappingProxyType[str, Any], ...]:
    if raw_items is None:
        return ()
    if not isinstance(raw_items, tuple):
        raise StateError("run ledger contains an invalid public collection")
    projected: list[MappingProxyType[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or not required.issubset(raw_item):
            raise StateError("run ledger contains an invalid public collection")
        item: dict[str, Any] = {}
        for field, expected_type in fields.items():
            if field not in raw_item:
                continue
            value = raw_item[field]
            if expected_type in {int, bool}:
                valid = type(value) is expected_type
            else:
                valid = isinstance(value, expected_type)
            if not valid:
                raise StateError("run ledger contains an invalid public collection")
            item[field] = value
        projected.append(MappingProxyType(item))
    return tuple(projected)


def _project_summary(snapshot: state.RunLedgerSnapshot, *, stale: bool) -> RunSummary:
    """Build a deeply immutable public projection without host paths or raw ledger data."""
    raw = snapshot.state
    base = raw.get("base")
    if base is not None and not isinstance(base, Mapping):
        raise StateError("run ledger contains an invalid public base")
    base_digest = _optional_string(raw, "base_digest")
    base_arch = _optional_string(raw, "base_arch")
    if base_digest is None and base is not None:
        base_digest = _optional_string(base, "digest")
    if base_arch is None and base is not None:
        base_arch = _optional_string(base, "arch")

    layers = _project_mapping_items(
        raw.get("layers", ()),
        fields={"digest": str, "target_dev": str},
        required=frozenset({"digest"}),
    )
    volumes = _project_mapping_items(
        raw.get("volumes", ()),
        fields={
            "name": str,
            "mount_path": str,
            "filesystem": str,
            "read_only": bool,
            "target_dev": str,
        },
        required=frozenset({"name"}),
    )
    ports = _project_mapping_items(
        raw.get("ports", ()),
        fields={
            "host_ip": str,
            "host_port": int,
            "guest_port": int,
            "protocol": str,
        },
        required=frozenset({"host_ip", "host_port", "guest_port", "protocol"}),
    )

    guest_ip = _optional_string(raw, "guest_ip")
    raw_ssh = raw.get("ssh")
    ssh: MappingProxyType[str, Any]
    if raw_ssh is not None:
        if not isinstance(raw_ssh, Mapping):
            raise StateError("run ledger contains an invalid public SSH endpoint")
        host = _optional_string(raw_ssh, "host")
        port = _optional_integer(raw_ssh, "port")
    else:
        host = _optional_string(raw, "ssh_host")
        port = _optional_integer(raw, "ssh_local_port")
    if host is None:
        host = guest_ip
    ssh = MappingProxyType({"host": host, "port": 22 if port is None else port})

    details = MappingProxyType(
        {
            "base_digest": "" if base_digest is None else base_digest,
            "base_arch": "" if base_arch is None else base_arch,
            "layers": layers,
            "memory_mib": _optional_integer(raw, "memory_mib"),
            "vcpus": _optional_integer(raw, "vcpus"),
            "network": _optional_string(raw, "network"),
            "ports": ports,
            "volumes": volumes,
            "ssh": ssh,
            "guest_ip": guest_ip,
            "created_at": _optional_string(raw, "created_at"),
            "updated_at": _optional_string(raw, "updated_at"),
        }
    )
    status = raw.get("status")
    if not isinstance(status, str):
        raise StateError("run ledger contains an invalid status")
    return RunSummary(snapshot.record, status, details, stale=stale)


def _project_inspect(snapshot: state.RunLedgerSnapshot) -> InspectRecord:
    """Build typed inspect data from exact public fields of one snapshot."""

    raw = snapshot.state
    base = raw.get("base")
    if base is not None and not isinstance(base, Mapping):
        raise StateError("run ledger contains an invalid public base")
    base_digest = _optional_string(raw, "base_digest")
    base_arch = _optional_string(raw, "base_arch")
    base_format = _optional_string(raw, "disk_format")
    if base is not None:
        if base_digest is None:
            base_digest = _optional_string(base, "digest")
        if base_arch is None:
            base_arch = _optional_string(base, "arch")
        if base_format is None:
            base_format = _optional_string(base, "disk_format")

    layers = tuple(
        InspectLayer(item["digest"], item.get("target_dev"))
        for item in _project_mapping_items(
            raw.get("layers", ()),
            fields={"digest": str, "target_dev": str},
            required=frozenset({"digest"}),
        )
    )
    ports = tuple(
        InspectPort(item["host_ip"], item["host_port"], item["guest_port"], item["protocol"])
        for item in _project_mapping_items(
            raw.get("ports", ()),
            fields={"host_ip": str, "host_port": int, "guest_port": int, "protocol": str},
            required=frozenset({"host_ip", "host_port", "guest_port", "protocol"}),
        )
    )
    volumes = tuple(
        InspectVolume(
            name=item["name"],
            mount_path=item.get("mount_path"),
            filesystem=item.get("filesystem"),
            read_only=item.get("read_only"),
            target_dev=item.get("target_dev"),
        )
        for item in _project_mapping_items(
            raw.get("volumes", ()),
            fields={
                "name": str,
                "mount_path": str,
                "filesystem": str,
                "read_only": bool,
                "target_dev": str,
            },
            required=frozenset({"name"}),
        )
    )

    guest_ip = _optional_string(raw, "guest_ip")
    raw_ssh = raw.get("ssh")
    if raw_ssh is not None:
        if not isinstance(raw_ssh, Mapping):
            raise StateError("run ledger contains an invalid public SSH endpoint")
        ssh_host = _optional_string(raw_ssh, "host")
        ssh_port = _optional_integer(raw_ssh, "port")
    else:
        ssh_host = _optional_string(raw, "ssh_host")
        ssh_port = _optional_integer(raw, "ssh_local_port")
    if ssh_host is None:
        ssh_host = guest_ip

    status = raw.get("status")
    if not isinstance(status, str):
        raise StateError("run ledger contains an invalid status")
    revision = _optional_integer(raw, "lifecycle_revision")
    return InspectRecord(
        schema_version=1,
        record=snapshot.record,
        lifecycle=InspectLifecycle(
            status=status,
            lifecycle_revision=0 if revision is None else revision,
            created_at=_optional_string(raw, "created_at"),
            updated_at=_optional_string(raw, "updated_at"),
        ),
        detail=CloudImageInspectDetail(
            base=InspectBase(base_digest, base_arch, base_format),
            layers=layers,
            memory_mib=_optional_integer(raw, "memory_mib"),
            vcpus=_optional_integer(raw, "vcpus"),
            network=_optional_string(raw, "network"),
            ports=ports,
            volumes=volumes,
            ssh=InspectSshEndpoint(ssh_host, 22 if ssh_port is None else ssh_port),
            guest_ip=guest_ip,
        ),
    )


def _aggregation_error(
    *,
    operation: RuntimeOperation,
    code: str,
    message: str,
    record: ExistingRunRecord | None = None,
    name: str | None = None,
    entry_token: str | None = None,
) -> RunAggregationError:
    return RunAggregationError(
        name=record.name if record is not None else name,
        entry_token=entry_token,
        operation=operation,
        dispatch_key=record.dispatch_key if record is not None else None,
        code=code,
        message=message,
    )


def _sorted_unique_errors(errors: list[RunAggregationError]) -> tuple[RunAggregationError, ...]:
    keyed: dict[tuple[str, str, str], RunAggregationError] = {}
    for error in errors:
        key = (
            error.name or error.entry_token or "",
            error.code,
            error.operation.value,
        )
        keyed.setdefault(key, error)
    return tuple(keyed[key] for key in sorted(keyed))


def _project_or_error(
    snapshot: state.RunLedgerSnapshot,
    *,
    operation: RuntimeOperation,
    stale: bool,
) -> tuple[RunSummary | None, RunAggregationError | None]:
    try:
        return _project_summary(snapshot, stale=stale), None
    except (RecursionError, StateError, TypeError, ValueError):
        return None, _aggregation_error(
            operation=operation,
            record=snapshot.record,
            code="invalid-ledger",
            message="invalid run ledger",
        )


def ps(*, roots: StatePaths | None = None) -> RunAggregationResult:
    """Return deterministic durable summaries without backend calls or writes."""
    resolved_roots = roots or state.resolve_roots()
    snapshots, snapshot_errors = state.enumerate_run_snapshots(resolved_roots)
    summaries: list[RunSummary] = []
    errors = list(snapshot_errors)
    for snapshot in snapshots:
        summary, error = _project_or_error(snapshot, operation=RuntimeOperation.PS, stale=True)
        if summary is not None:
            summaries.append(summary)
        if error is not None:
            errors.append(error)
    return RunAggregationResult(
        tuple(sorted(summaries, key=lambda item: item.name)),
        _sorted_unique_errors(errors),
    )


def reconcile(*, roots: StatePaths | None = None) -> RunAggregationResult:
    """Live-reconcile each valid run through its exact durable dispatch record."""
    resolved_roots = roots or state.resolve_roots()
    snapshots, snapshot_errors = state.enumerate_run_snapshots(resolved_roots)
    summaries: list[RunSummary] = []
    errors = [
        RunAggregationError(
            name=error.name,
            entry_token=error.entry_token,
            operation=RuntimeOperation.RECONCILE,
            dispatch_key=error.dispatch_key,
            code=error.code,
            message=error.message,
        )
        for error in snapshot_errors
    ]
    for snapshot in snapshots:
        record = snapshot.record
        try:
            adapter = _preflight_existing_adapter(record, RuntimeOperation.RECONCILE, resolved_roots)
            if adapter is cloud_runtime:
                adapter_result = cloud_runtime.reconcile_run(
                    record.name,
                    roots=resolved_roots,
                    _expected_record=record,
                )
            else:
                adapter_result = lima.reconcile_run(
                    record.name,
                    roots=resolved_roots,
                    _expected_record=record,
                )
            refreshed = state.read_run_ledger_snapshot(resolved_roots, record.name)
            if refreshed.record != record:
                raise StateError("run ledger changed during reconciliation")
            projected = refreshed
            if record.state_schema_version == 1:
                observed_state = adapter_result.get("state") if isinstance(adapter_result, Mapping) else None
                if not isinstance(observed_state, Mapping):
                    raise StateError("runtime reconciliation returned no observed state")
                projected = state.snapshot_from_runtime_observation(record, observed_state)
            summary, error = _project_or_error(
                projected,
                operation=RuntimeOperation.RECONCILE,
                stale=False,
            )
            if summary is None or error is not None:
                raise StateError("invalid reconciled run ledger")
            summaries.append(summary)
            adapter_warnings = adapter_result.get("warnings") if isinstance(adapter_result, Mapping) else None
            if isinstance(adapter_warnings, (list, tuple)) and adapter_warnings:
                errors.append(
                    _aggregation_error(
                        operation=RuntimeOperation.RECONCILE,
                        record=record,
                        code="runtime-warning",
                        message="runtime status changed during reconciliation",
                    )
                )
        except (RuntimeCapabilityError, RuntimePreflightError):
            summary, projection_error = _project_or_error(
                snapshot,
                operation=RuntimeOperation.RECONCILE,
                stale=True,
            )
            if summary is not None:
                summaries.append(summary)
            if projection_error is not None:
                errors.append(projection_error)
            errors.append(
                _aggregation_error(
                    operation=RuntimeOperation.RECONCILE,
                    record=record,
                    code="runtime-capability",
                    message="runtime reconciliation is unavailable",
                )
            )
        except Exception:
            summary, projection_error = _project_or_error(
                snapshot,
                operation=RuntimeOperation.RECONCILE,
                stale=True,
            )
            if summary is not None:
                summaries.append(summary)
            if projection_error is not None:
                errors.append(projection_error)
            errors.append(
                _aggregation_error(
                    operation=RuntimeOperation.RECONCILE,
                    record=record,
                    code="runtime-failure",
                    message="runtime reconciliation failed",
                )
            )
    return RunAggregationResult(
        tuple(sorted(summaries, key=lambda item: item.name)),
        _sorted_unique_errors(errors),
    )


__all__ = (
    "bind_run_request_volumes",
    "commit",
    "exec",
    "inspect_run",
    "logs",
    "preflight_existing_record",
    "preflight_run_capabilities",
    "preflight_run_request",
    "ps",
    "reconcile",
    "require_existing_preflight",
    "require_run_preflight",
    "resolve_existing_run",
    "resolve_run_request",
    "rm",
    "run",
    "shell",
    "start",
    "stop",
)
