"""Small immutable contracts shared by runtime ledger routing."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePath, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .errors import LifecycleError, PalimpsestError
from .refs import RunSpec

_RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]{0,63}$")
_LIFECYCLE_ADAPTER_AUTHENTICATION_KEY = secrets.token_bytes(32)
_MAX_LIFECYCLE_REVISION = 2**63 - 1


class RuntimeKind(StrEnum):
    CLOUD_IMAGE = "cloud-image"
    OCI_ROOT = "oci-root"


class RuntimeBackend(StrEnum):
    KVM = "kvm"
    LIBVIRT_HVF = "libvirt-hvf"
    LIMA_VZ = "lima-vz"


class RuntimeOperation(StrEnum):
    RUN = "run"
    START = "start"
    STOP = "stop"
    RM = "rm"
    INSPECT = "inspect"
    LOGS = "logs"
    PS = "ps"
    RECONCILE = "reconcile"
    EXEC = "exec"
    SHELL = "shell"
    COMMIT = "commit"


class RunWarningCategory(StrEnum):
    """Stable warnings computed from a typed create result."""

    EXPERIMENTAL_BACKEND = "experimental-backend"


class LifecycleWarningCategory(StrEnum):
    """Stable, non-reflective warnings attached to lifecycle receipts."""

    FORCED_SHUTDOWN = "forced-shutdown"
    BACKEND_RECONCILED = "backend-reconciled"


class InspectWarningCategory(StrEnum):
    """Stable warnings reserved for future state-only inspect projections."""

    BACKEND_OBJECT_MISSING = "backend-object-missing"
    BACKEND_STATUS_DRIFT = "backend-status-drift"
    CONTROL_DEGRADED = "control-degraded"
    RESOURCE_PRESSURE = "resource-pressure"


class LogMode(StrEnum):
    """Stable stream behavior selected when logs are opened."""

    SNAPSHOT = "snapshot"
    FOLLOW = "follow"


class LogSourceStream(StrEnum):
    """Logical byte source, independent of a backend's implementation."""

    VM_CONSOLE = "vm-console"
    WORKLOAD_STDOUT = "workload-stdout"
    WORKLOAD_STDERR = "workload-stderr"
    WORKLOAD_PTY = "workload-pty"


class LogTerminalCategory(StrEnum):
    SNAPSHOT_COMPLETE = "snapshot-complete"
    RUN_TERMINAL = "run-terminal"
    CANCELLED = "cancelled"
    ERROR = "error"


class LogErrorCategory(StrEnum):
    """Non-reflective failures safe for callers to branch on."""

    INVALID_CONSOLE = "invalid-console"
    RUN_CHANGED = "run-changed"
    CONSOLE_CHANGED = "console-changed"
    READ_FAILED = "read-failed"
    ALREADY_CONSUMED = "already-consumed"


class CapabilityErrorCategory(StrEnum):
    MISSING = "capability-missing"
    UNSUPPORTED = "capability-unsupported"
    CHECK_FAILED = "capability-check-failed"
    REPORT_MISMATCH = "preflight-report-mismatch"
    REPORT_STALE = "preflight-report-stale"
    REPORT_PROVENANCE = "preflight-report-provenance"
    REPORT_CONSUMED = "preflight-report-consumed"
    REPORT_CAPACITY = "preflight-report-capacity"


class PreflightReportPurpose(StrEnum):
    OPERATION = "operation"
    DISCOVERY = "discovery"


_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_SUBJECT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPORT_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    capability_id: str
    selector: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, str) or _CAPABILITY_ID_RE.fullmatch(self.capability_id) is None:
            raise ValueError("capability requirement has an invalid ID")
        if self.selector is not None and (
            not isinstance(self.selector, str)
            or not self.selector
            or len(self.selector) > 256
            or "\x00" in self.selector
        ):
            raise ValueError("capability requirement has an invalid selector")


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    schema_version: int
    dispatch_key: DispatchKey
    operation: RuntimeOperation
    requirements: tuple[CapabilityRequirement, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("capability profile has an unsupported schema version")
        if not isinstance(self.dispatch_key, DispatchKey):
            raise TypeError("capability profile requires a DispatchKey")
        if not isinstance(self.operation, RuntimeOperation):
            raise TypeError("capability profile requires a RuntimeOperation")
        if not isinstance(self.requirements, tuple) or not all(
            isinstance(item, CapabilityRequirement) for item in self.requirements
        ):
            raise TypeError("capability profile requires immutable requirements")
        identifiers = tuple(item.capability_id for item in self.requirements)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("capability profile contains duplicate requirements")

    @property
    def profile_id(self) -> str:
        return (
            f"runtime-capability-v{self.schema_version}:"
            f"{self.dispatch_key.runtime_kind.value}/{self.dispatch_key.backend.value}/{self.operation.value}"
        )


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    capability_id: str
    observed: str
    passed: bool
    error_category: CapabilityErrorCategory | None = None
    remediation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, str) or _CAPABILITY_ID_RE.fullmatch(self.capability_id) is None:
            raise ValueError("capability check has an invalid ID")
        if not isinstance(self.observed, str) or not self.observed or len(self.observed) > 128:
            raise ValueError("capability check has an invalid observation")
        if type(self.passed) is not bool:
            raise TypeError("capability check pass state must be a bool")
        if self.passed:
            if self.error_category is not None or self.remediation is not None:
                raise ValueError("successful capability check cannot contain failure metadata")
        elif not isinstance(self.error_category, CapabilityErrorCategory) or not self.remediation:
            raise ValueError("failed capability check requires stable failure metadata")


@dataclass(frozen=True, slots=True, repr=False)
class PreflightReport:
    schema_version: int
    profile: CapabilityProfile
    subject_digest: str
    checks: tuple[CapabilityCheck, ...]
    issued_at_monotonic_ns: int
    expires_at_monotonic_ns: int
    host_digest: str
    purpose: PreflightReportPurpose
    issuer_nonce: str
    authentication_tag: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("preflight report has an unsupported schema version")
        if not isinstance(self.profile, CapabilityProfile):
            raise TypeError("preflight report requires a CapabilityProfile")
        if not isinstance(self.subject_digest, str) or _SUBJECT_DIGEST_RE.fullmatch(self.subject_digest) is None:
            raise ValueError("preflight report has an invalid subject digest")
        if not isinstance(self.host_digest, str) or _SUBJECT_DIGEST_RE.fullmatch(self.host_digest) is None:
            raise ValueError("preflight report has an invalid host digest")
        if not isinstance(self.purpose, PreflightReportPurpose):
            raise TypeError("preflight report requires a purpose")
        if not isinstance(self.issuer_nonce, str) or _REPORT_TOKEN_RE.fullmatch(self.issuer_nonce) is None:
            raise ValueError("preflight report has an invalid issuer nonce")
        if not isinstance(self.authentication_tag, str) or _REPORT_TOKEN_RE.fullmatch(self.authentication_tag) is None:
            raise ValueError("preflight report has an invalid authentication tag")
        if not isinstance(self.checks, tuple) or not all(isinstance(item, CapabilityCheck) for item in self.checks):
            raise TypeError("preflight report requires immutable checks")
        if tuple(item.capability_id for item in self.checks) != tuple(
            item.capability_id for item in self.profile.requirements
        ):
            raise ValueError("preflight report checks do not match its profile")
        if (
            type(self.issued_at_monotonic_ns) is not int
            or type(self.expires_at_monotonic_ns) is not int
            or self.issued_at_monotonic_ns < 0
            or self.expires_at_monotonic_ns <= self.issued_at_monotonic_ns
        ):
            raise ValueError("preflight report has an invalid freshness window")

    @property
    def successful(self) -> bool:
        return all(item.passed for item in self.checks)


class RuntimePreflightError(LifecycleError):
    code = "runtime-preflight-failed"

    def __init__(
        self,
        category: CapabilityErrorCategory,
        message: str,
        *,
        report: PreflightReport | None = None,
        capability_id: str | None = None,
    ) -> None:
        if not isinstance(category, CapabilityErrorCategory):
            raise TypeError("runtime preflight error requires a stable category")
        if not isinstance(message, str) or not message:
            raise ValueError("runtime preflight error requires a message")
        if report is not None and not isinstance(report, PreflightReport):
            raise TypeError("runtime preflight error report is invalid")
        if capability_id is not None and (
            not isinstance(capability_id, str) or _CAPABILITY_ID_RE.fullmatch(capability_id) is None
        ):
            raise ValueError("runtime preflight error capability ID is invalid")
        self.category = category
        self.report = report
        self.capability_id = capability_id
        super().__init__(message)


class ProcessStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    PTY = "pty"


class ProcessSignal(StrEnum):
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"
    HANGUP = "hangup"


class ProcessExitCategory(StrEnum):
    EXITED = "exited"
    SIGNALED = "signaled"
    CANCELLED = "cancelled"
    TRANSPORT_ERROR = "transport-error"


@dataclass(frozen=True, slots=True)
class ExecRequest:
    """Validated literal guest argv for one non-interactive exec operation."""

    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("exec requires a nonempty argv")
        if any(not isinstance(item, str) or "\x00" in item for item in self.argv):
            raise ValueError("exec argv must contain only NUL-free strings")

    @classmethod
    def from_argv(cls, argv: Sequence[str]) -> ExecRequest:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise TypeError("exec argv must be a sequence of strings")
        return cls(tuple(argv))


@dataclass(frozen=True, slots=True)
class ProcessCapabilities:
    stdin: bool
    tty: bool
    resize: bool
    signal: bool

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.stdin, self.tty, self.resize, self.signal)):
            raise TypeError("process capabilities must be booleans")
        if self.resize and not self.tty:
            raise ValueError("process resize capability requires a TTY")


@dataclass(frozen=True, slots=True)
class ProcessExit:
    returncode: int
    exit_code: int | None
    signal_number: int | None
    category: ProcessExitCategory

    def __post_init__(self) -> None:
        if type(self.returncode) is not int:
            raise TypeError("process return code must be an integer")
        if not isinstance(self.category, ProcessExitCategory):
            raise TypeError("process exit requires a category")
        if self.returncode >= 0:
            if self.exit_code != self.returncode or self.signal_number is not None:
                raise ValueError("exited process result has inconsistent fields")
            if self.category not in {
                ProcessExitCategory.EXITED,
                ProcessExitCategory.CANCELLED,
                ProcessExitCategory.TRANSPORT_ERROR,
            }:
                raise ValueError("exited process result has an invalid category")
        else:
            if self.exit_code is not None or self.signal_number != -self.returncode:
                raise ValueError("signaled process result has inconsistent fields")
            if self.category not in {ProcessExitCategory.SIGNALED, ProcessExitCategory.CANCELLED}:
                raise ValueError("signaled process result has an invalid category")


@dataclass(frozen=True, slots=True)
class ProcessOutputEvent:
    stream: ProcessStream
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.stream, ProcessStream):
            raise TypeError("process output event requires a stream")
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("process output event requires nonempty bytes")


@dataclass(frozen=True, slots=True)
class ProcessStatusEvent:
    result: ProcessExit

    def __post_init__(self) -> None:
        if not isinstance(self.result, ProcessExit):
            raise TypeError("process status event requires a ProcessExit")


ProcessEvent = ProcessOutputEvent | ProcessStatusEvent


@runtime_checkable
class ProcessSession(Protocol):
    """One adapter-owned process transport with a single event consumer."""

    @property
    def capabilities(self) -> ProcessCapabilities: ...

    def events(self) -> Iterator[ProcessEvent]: ...

    def write_stdin(self, data: bytes) -> None: ...

    def close_stdin(self) -> None: ...

    def resize(self, rows: int, columns: int) -> None: ...

    def signal(self, requested: ProcessSignal) -> None: ...

    def wait(self) -> ProcessExit: ...

    def close(self) -> None: ...


class ProcessCapabilityError(PalimpsestError):
    code = "process-capability-unavailable"

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"process session does not support {capability}")


class RunAttachmentMode(StrEnum):
    """How a successful create result is attached to its caller.

    A detached launch returns a durable identity and optional launch hint.  An
    attached launch owns one process session whose exit remains authoritative.
    """

    DETACHED = "detached"
    ATTACHED = "attached"


class RunRequestProvenanceStage(StrEnum):
    LOGICAL = "logical"
    BOUND = "bound"


@dataclass(frozen=True, slots=True, repr=False)
class RunRequestProvenance:
    stage: RunRequestProvenanceStage
    issuer_nonce: str
    authentication_tag: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, RunRequestProvenanceStage):
            raise TypeError("run request provenance requires a stage")
        if not isinstance(self.issuer_nonce, str) or _REPORT_TOKEN_RE.fullmatch(self.issuer_nonce) is None:
            raise ValueError("run request provenance has an invalid nonce")
        if not isinstance(self.authentication_tag, str) or _REPORT_TOKEN_RE.fullmatch(self.authentication_tag) is None:
            raise ValueError("run request provenance has an invalid authentication tag")


@dataclass(frozen=True, slots=True)
class CloudInitWriteFileSnapshot:
    path: str
    content: str
    permissions: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (self.path, self.content, self.permissions)):
            raise TypeError("cloud-init write-file snapshot fields must be strings")


@dataclass(frozen=True, slots=True)
class CloudInitSnapshot:
    packages: tuple[str, ...]
    write_files: tuple[CloudInitWriteFileSnapshot, ...]
    runcmd: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.packages, tuple) or not all(isinstance(value, str) for value in self.packages):
            raise TypeError("cloud-init package snapshot must contain strings")
        if not isinstance(self.write_files, tuple) or not all(
            isinstance(value, CloudInitWriteFileSnapshot) for value in self.write_files
        ):
            raise TypeError("cloud-init write-file snapshot is invalid")
        if not isinstance(self.runcmd, tuple) or not all(
            isinstance(command, tuple) and all(isinstance(argument, str) for argument in command)
            for command in self.runcmd
        ):
            raise TypeError("cloud-init command snapshot is invalid")


def snapshot_cloud_init(value: object | None) -> CloudInitSnapshot | None:
    """Freeze exactly the cloud-init fields consumed by runtime adapters.

    Ordinary getter/conversion failures are normalized without chaining so
    secret-bearing exception text cannot escape. Process-control exceptions
    such as ``KeyboardInterrupt`` and ``SystemExit`` intentionally propagate.
    """

    if value is None or isinstance(value, CloudInitSnapshot):
        return value
    snapshot: CloudInitSnapshot | None = None
    try:
        packages = tuple(value.packages)  # type: ignore[attr-defined]
        write_files = tuple(
            CloudInitWriteFileSnapshot(item.path, item.content, item.permissions)  # type: ignore[attr-defined]
            for item in value.write_files  # type: ignore[attr-defined]
        )
        commands = tuple(tuple(command) for command in value.runcmd)  # type: ignore[attr-defined]
        snapshot = CloudInitSnapshot(packages, write_files, commands)
    except Exception:
        pass
    if snapshot is None:
        # Raise with no active exception so attacker-controlled getter errors
        # cannot survive in __context__, __cause__, or the rendered traceback.
        raise TypeError("runtime cloud-init input cannot be converted to an immutable snapshot")
    return snapshot


ALLOWED_RUNTIME_COMBINATIONS = frozenset(
    {
        (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIBVIRT_HVF),
        (RuntimeKind.CLOUD_IMAGE, RuntimeBackend.LIMA_VZ),
        (RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    }
)


@dataclass(frozen=True, slots=True)
class DispatchKey:
    runtime_kind: RuntimeKind
    backend: RuntimeBackend

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_kind, RuntimeKind) or not isinstance(self.backend, RuntimeBackend):
            raise TypeError("dispatch key requires RuntimeKind and RuntimeBackend")
        if (self.runtime_kind, self.backend) not in ALLOWED_RUNTIME_COMBINATIONS:
            raise ValueError(f"unsupported runtime/backend combination: {self.runtime_kind.value}/{self.backend.value}")


@dataclass(frozen=True, slots=True, repr=False)
class RunVolumeIntent:
    """Ordered logical volume policy, without a host or backend attachment."""

    name: str
    target: str
    filesystem: str = "ext4"
    read_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SUMMARY_LOGICAL_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("run volume intent has an invalid name")
        if not _valid_mount_path(self.target):
            raise ValueError("run volume intent has an invalid target")
        if self.filesystem != "ext4":
            raise ValueError("run volume intent has an invalid filesystem")
        if type(self.read_only) is not bool:
            raise TypeError("run volume intent read-only policy must be a bool")


@dataclass(frozen=True, slots=True)
class ResolvedRunRequest:
    """A pure create-time routing decision plus its non-reflective logical spec.

    ``attachments_bound`` is false only while a project is still preparing
    managed volume artifacts. Resolver and binder provenance authenticate the
    immutable request shape, while the separate one-use preflight report
    authorizes a fresh adapter entry.
    """

    dispatch_key: DispatchKey
    spec: RunSpec = dataclass_field(repr=False)
    volume_intents: tuple[RunVolumeIntent, ...] = dataclass_field(default=(), repr=False)
    attachments_bound: bool = True
    provenance: RunRequestProvenance | None = dataclass_field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_key, DispatchKey):
            raise TypeError("resolved run request requires a DispatchKey")
        if not isinstance(self.spec, RunSpec):
            raise TypeError("resolved run request requires a RunSpec")
        if not isinstance(self.volume_intents, tuple) or not all(
            isinstance(intent, RunVolumeIntent) for intent in self.volume_intents
        ):
            raise TypeError("resolved run request requires immutable volume intents")
        intent_names = tuple(intent.name for intent in self.volume_intents)
        intent_targets = tuple(intent.target for intent in self.volume_intents)
        if len(intent_names) != len(set(intent_names)) or len(intent_targets) != len(set(intent_targets)):
            raise ValueError("resolved run request has duplicate volume intents")
        if self.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE:
            raise ValueError("OCI-root create requests are unavailable before the OCI input contract lands")
        if type(self.attachments_bound) is not bool:
            raise TypeError("resolved run request attachment binding must be a boolean")
        if self.provenance is not None and not isinstance(self.provenance, RunRequestProvenance):
            raise TypeError("resolved run request provenance is invalid")
        if not self.attachments_bound and self.spec.volumes:
            raise ValueError("an unbound logical run request cannot contain physical volume attachments")
        if not self.attachments_bound and not self.volume_intents:
            raise ValueError("an unbound logical run request requires volume intents")
        if self.attachments_bound:
            bound_intents = tuple(
                RunVolumeIntent(volume.name, volume.mount_path, volume.filesystem, volume.read_only)
                for volume in self.spec.volumes
            )
            if bound_intents != self.volume_intents:
                raise ValueError("bound run attachments do not match logical volume intents")
            sources_match = all(
                (
                    volume.backend_name is not None
                    if self.dispatch_key.backend is RuntimeBackend.LIMA_VZ
                    else volume.host_path is not None
                )
                for volume in self.spec.volumes
            )
            if not sources_match:
                raise ValueError("bound volume attachment source does not match resolved backend")
        if self.dispatch_key.backend in {RuntimeBackend.LIBVIRT_HVF, RuntimeBackend.LIMA_VZ}:
            if self.spec.stack.base.arch != "aarch64":
                raise ValueError("selected runtime backend requires an aarch64 cloud image")


@dataclass(frozen=True, slots=True)
class RunResult:
    """Safe transport-neutral projection of a completed create operation."""

    record: ExistingRunRecord
    status: str
    ready: bool
    attachment_mode: RunAttachmentMode
    guest_ip: str | None = None
    session: ProcessSession | None = dataclass_field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("run result requires an ExistingRunRecord")
        if (
            not isinstance(self.status, str)
            or self.status not in ALLOWED_RUNTIME_STATUSES[self.record.dispatch_key.runtime_kind]
        ):
            raise ValueError("run result has an invalid runtime status")
        if type(self.ready) is not bool:
            raise TypeError("run result ready flag must be a bool")
        if not isinstance(self.attachment_mode, RunAttachmentMode):
            raise TypeError("run result requires a RunAttachmentMode")
        if self.guest_ip is not None and not _valid_ip(self.guest_ip):
            raise ValueError("run result has an invalid guest IP")
        if self.record.dispatch_key.runtime_kind is RuntimeKind.CLOUD_IMAGE:
            if self.ready != (self.status == "running"):
                raise ValueError("run result readiness does not match runtime status")
        else:
            ready_statuses = {"running", "exited"}
            if self.ready and self.status not in ready_statuses:
                raise ValueError("run result readiness does not match runtime status")
            if not self.ready and self.status in {"running", "exited"}:
                raise ValueError("run result readiness does not match runtime status")
        if self.attachment_mode is RunAttachmentMode.DETACHED:
            if self.session is not None:
                raise ValueError("detached run result cannot contain a process session")
        elif (
            self.record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
            or not self.ready
            or self.status not in {"running", "exited"}
            or not isinstance(self.session, ProcessSession)
        ):
            raise ValueError("attached run result requires a ready OCI-root process session")

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def runtime_kind(self) -> RuntimeKind:
        return self.record.dispatch_key.runtime_kind

    @property
    def backend(self) -> RuntimeBackend:
        return self.record.dispatch_key.backend

    @property
    def launch_hint(self) -> str | None:
        """Return the existing detached CLI output without exposing routing."""

        if not self.ready or self.attachment_mode is RunAttachmentMode.ATTACHED:
            return None
        if self.runtime_kind is RuntimeKind.OCI_ROOT:
            return self.run_id
        if self.backend is RuntimeBackend.LIMA_VZ:
            return f"limactl shell {self.name}"
        return self.guest_ip

    @property
    def warnings(self) -> tuple[RunWarningCategory, ...]:
        if self.backend is RuntimeBackend.LIBVIRT_HVF:
            return (RunWarningCategory.EXPERIMENTAL_BACKEND,)
        return ()


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Safe immutable receipt for one exact existing-run commit."""

    record: ExistingRunRecord
    tag: str
    digest: str
    size_bytes: int
    parent_digest: str | None
    base_image_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("commit result requires an ExistingRunRecord")
        if not isinstance(self.tag, str) or _TAG_RE.fullmatch(self.tag) is None:
            raise ValueError("commit result has an invalid tag")
        for label, value, optional in (
            ("digest", self.digest, False),
            ("parent digest", self.parent_digest, True),
            ("base image digest", self.base_image_digest, False),
        ):
            if optional and value is None:
                continue
            if not isinstance(value, str) or _SUBJECT_DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"commit result has an invalid {label}")
        if type(self.size_bytes) is not int or self.size_bytes < 4:
            raise ValueError("commit result has an invalid size")


@dataclass(frozen=True, slots=True)
class ExistingRunRecord:
    """Validated immutable identity and routing fields from an existing run ledger."""

    name: str
    run_id: str
    state_schema_version: int
    dispatch_key: DispatchKey

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _RUN_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("existing run record has an invalid name")
        if not isinstance(self.run_id, str):
            raise TypeError("existing run record requires a string run ID")
        try:
            parsed_run_id = uuid.UUID(self.run_id)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("existing run record has an invalid run ID") from None
        if str(parsed_run_id) != self.run_id:
            raise ValueError("existing run record run ID is not canonical")
        if type(self.state_schema_version) is not int or self.state_schema_version not in {1, 2}:
            raise ValueError("existing run record has an invalid state schema version")
        if not isinstance(self.dispatch_key, DispatchKey):
            raise TypeError("existing run record requires a DispatchKey")


@dataclass(frozen=True, slots=True)
class LogCursor:
    """One shared byte/event position bound to an exact stream generation."""

    record: ExistingRunRecord
    generation: str
    position: int

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("log cursor requires an ExistingRunRecord")
        if not isinstance(self.generation, str):
            raise TypeError("log cursor requires a string generation")
        try:
            parsed = uuid.UUID(self.generation)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("log cursor has an invalid generation") from None
        if str(parsed) != self.generation:
            raise ValueError("log cursor generation is not canonical")
        if type(self.position) is not int or not 1 <= self.position <= _MAX_LIFECYCLE_REVISION:
            raise ValueError("log cursor position must start at one")


@dataclass(frozen=True, slots=True)
class LogDataEvent:
    cursor: LogCursor
    source: LogSourceStream
    stream_sequence: int
    observed_at: datetime
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, LogCursor):
            raise TypeError("log data event requires a LogCursor")
        if not isinstance(self.source, LogSourceStream):
            raise TypeError("log data event requires a LogSourceStream")
        if type(self.stream_sequence) is not int or not 1 <= self.stream_sequence <= _MAX_LIFECYCLE_REVISION:
            raise ValueError("log data event sequence must start at one")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != UTC.utcoffset(self.observed_at)
        ):
            raise ValueError("log data event requires a UTC observation time")
        if type(self.data) is not bytes or not self.data or len(self.data) > 64 * 1024:
            raise ValueError("log data event requires 1..65536 exact bytes")


@dataclass(frozen=True, slots=True)
class LogTerminalOutcome:
    category: LogTerminalCategory
    error_category: LogErrorCategory | None = None
    run_status: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, LogTerminalCategory):
            raise TypeError("log terminal outcome requires a category")
        if self.category is LogTerminalCategory.ERROR:
            if not isinstance(self.error_category, LogErrorCategory) or self.run_status is not None:
                raise ValueError("log error terminal requires only a stable error category")
        elif self.category is LogTerminalCategory.RUN_TERMINAL:
            if self.error_category is not None or self.run_status not in {"stopped", "removed", "failed", "exited"}:
                raise ValueError("run terminal requires a terminal run status")
        elif self.error_category is not None or self.run_status is not None:
            raise ValueError("normal log terminal cannot contain error or run metadata")


@dataclass(frozen=True, slots=True)
class LogTerminalEvent:
    cursor: LogCursor
    observed_at: datetime
    outcome: LogTerminalOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.cursor, LogCursor):
            raise TypeError("log terminal event requires a LogCursor")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() != UTC.utcoffset(self.observed_at)
        ):
            raise ValueError("log terminal event requires a UTC observation time")
        if not isinstance(self.outcome, LogTerminalOutcome):
            raise TypeError("log terminal event requires a LogTerminalOutcome")
        if (
            self.outcome.category is LogTerminalCategory.RUN_TERMINAL
            and self.outcome.run_status not in ALLOWED_RUNTIME_STATUSES[self.cursor.record.dispatch_key.runtime_kind]
        ):
            raise ValueError("log terminal status does not match the runtime kind")


LogEvent = LogDataEvent | LogTerminalEvent


class LogStreamError(LifecycleError):
    """Stable log failure that never reflects paths or backend output."""

    code = "log-stream-failed"

    def __init__(self, category: LogErrorCategory) -> None:
        if not isinstance(category, LogErrorCategory):
            raise TypeError("log stream error requires a stable category")
        self.category = category
        super().__init__(f"log stream failed: {category.value}")


@runtime_checkable
class LogStream(Protocol):
    @property
    def record(self) -> ExistingRunRecord: ...

    @property
    def mode(self) -> LogMode: ...

    def events(self) -> Iterator[LogEvent]: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LifecycleCursor:
    """A durable lifecycle position bound to one exact run identity."""

    record: ExistingRunRecord
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("lifecycle cursor requires an ExistingRunRecord")
        if type(self.revision) is not int or not 0 <= self.revision <= _MAX_LIFECYCLE_REVISION:
            raise ValueError("lifecycle cursor has an invalid revision")


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Safe result of one existing-run lifecycle transition."""

    record: ExistingRunRecord
    operation: RuntimeOperation
    previous_status: str
    current_status: str
    cursor: LifecycleCursor
    warning_category: LifecycleWarningCategory | None = None
    fallback_used: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("lifecycle result requires an ExistingRunRecord")
        if type(self.operation) is not RuntimeOperation or self.operation not in {
            RuntimeOperation.START,
            RuntimeOperation.STOP,
            RuntimeOperation.RM,
        }:
            raise ValueError("lifecycle result has an invalid operation")
        allowed = ALLOWED_RUNTIME_STATUSES[self.record.dispatch_key.runtime_kind]
        if self.previous_status not in allowed or self.current_status not in allowed:
            raise ValueError("lifecycle result has an invalid runtime status")
        if not isinstance(self.cursor, LifecycleCursor) or self.cursor.record != self.record:
            raise ValueError("lifecycle result cursor does not match its record")
        if self.warning_category is not None and not isinstance(self.warning_category, LifecycleWarningCategory):
            raise TypeError("lifecycle result has an invalid warning category")
        if type(self.fallback_used) is not bool:
            raise TypeError("lifecycle result fallback flag must be a bool")
        if self.fallback_used != (self.warning_category is LifecycleWarningCategory.FORCED_SHUTDOWN):
            raise ValueError("lifecycle result fallback metadata is inconsistent")
        if (
            self.warning_category is LifecycleWarningCategory.FORCED_SHUTDOWN
            and self.operation is not RuntimeOperation.STOP
        ):
            raise ValueError("forced shutdown warning requires a stop operation")
        if self.warning_category is LifecycleWarningCategory.BACKEND_RECONCILED and (
            self.operation not in {RuntimeOperation.START, RuntimeOperation.STOP}
            or self.previous_status != self.current_status
        ):
            raise ValueError("backend reconciliation warning requires a same-status start or stop")


@dataclass(frozen=True, slots=True, repr=False)
class _LifecycleAdapterOutcome:
    """Internal adapter-to-dispatch receipt; never serialized directly."""

    record: ExistingRunRecord
    previous_status: str
    previous_revision: int
    status: str
    revision: int
    warning_category: LifecycleWarningCategory | None = None
    fallback_used: bool = False
    authentication_tag: bytes = dataclass_field(default=b"", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("adapter lifecycle outcome requires an ExistingRunRecord")
        if self.previous_status not in ALLOWED_RUNTIME_STATUSES[self.record.dispatch_key.runtime_kind]:
            raise ValueError("adapter lifecycle outcome has an invalid previous status")
        if type(self.previous_revision) is not int or not 0 <= self.previous_revision <= _MAX_LIFECYCLE_REVISION:
            raise ValueError("adapter lifecycle outcome has an invalid previous revision")
        if self.status not in ALLOWED_RUNTIME_STATUSES[self.record.dispatch_key.runtime_kind]:
            raise ValueError("adapter lifecycle outcome has an invalid status")
        if type(self.revision) is not int or not 0 <= self.revision <= _MAX_LIFECYCLE_REVISION:
            raise ValueError("adapter lifecycle outcome has an invalid revision")
        if self.warning_category is not None and not isinstance(self.warning_category, LifecycleWarningCategory):
            raise TypeError("adapter lifecycle outcome has an invalid warning category")
        if type(self.fallback_used) is not bool:
            raise TypeError("adapter lifecycle outcome fallback flag must be a bool")
        if self.fallback_used != (self.warning_category is LifecycleWarningCategory.FORCED_SHUTDOWN):
            raise ValueError("adapter lifecycle outcome fallback metadata is inconsistent")
        if self.warning_category is LifecycleWarningCategory.BACKEND_RECONCILED and (
            self.previous_status != self.status or self.revision <= self.previous_revision
        ):
            raise ValueError("adapter lifecycle outcome reconciliation metadata is inconsistent")
        if self.warning_category is LifecycleWarningCategory.BACKEND_RECONCILED and (
            self.previous_status != self.status or self.revision <= self.previous_revision
        ):
            raise ValueError("adapter lifecycle outcome recovery metadata is inconsistent")
        if type(self.authentication_tag) is not bytes or len(self.authentication_tag) != 32:
            raise ValueError("adapter lifecycle outcome authentication is invalid")


def _lifecycle_outcome_authentication_tag(
    record: ExistingRunRecord,
    previous_status: str,
    previous_revision: int,
    status: str,
    revision: int,
    warning_category: LifecycleWarningCategory | None,
    fallback_used: bool,
) -> bytes:
    payload = json.dumps(
        {
            "name": record.name,
            "run_id": record.run_id,
            "state_schema_version": record.state_schema_version,
            "runtime_kind": record.dispatch_key.runtime_kind.value,
            "backend": record.dispatch_key.backend.value,
            "previous_status": previous_status,
            "previous_revision": previous_revision,
            "status": status,
            "revision": revision,
            "warning_category": None if warning_category is None else warning_category.value,
            "fallback_used": fallback_used,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.digest(_LIFECYCLE_ADAPTER_AUTHENTICATION_KEY, payload, "sha256")


def _issue_lifecycle_adapter_outcome(
    record: ExistingRunRecord,
    previous_status: str,
    previous_revision: int,
    status: str,
    revision: int,
    warning_category: LifecycleWarningCategory | None = None,
    fallback_used: bool = False,
) -> _LifecycleAdapterOutcome:
    tag = _lifecycle_outcome_authentication_tag(
        record,
        previous_status,
        previous_revision,
        status,
        revision,
        warning_category,
        fallback_used,
    )
    return _LifecycleAdapterOutcome(
        record,
        previous_status,
        previous_revision,
        status,
        revision,
        warning_category,
        fallback_used,
        tag,
    )


def _binding_projection(value: Any) -> Any:
    """Return an in-process canonical projection used only behind SHA-256."""

    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, tuple):
        return [_binding_projection(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("preflight binding mappings require string keys")
        return {key: _binding_projection(value[key]) for key in sorted(value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _binding_projection(getattr(value, item.name)) for item in fields(value)}
    raise TypeError(f"preflight binding does not support {type(value).__module__}.{type(value).__qualname__}")


def _cloud_init_binding_projection(value: object | None) -> Mapping[str, Any] | None:
    """Project exactly the mutable cloud-init fields consumed by guest rendering."""

    snapshot = snapshot_cloud_init(value)
    if snapshot is None:
        return None
    return {
        "packages": snapshot.packages,
        "write_files": tuple(
            {
                "path": item.path,
                "content": item.content,
                "permissions": item.permissions,
            }
            for item in snapshot.write_files
        ),
        "runcmd": snapshot.runcmd,
    }


def _subject_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _binding_projection(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def run_request_subject_digest(request: ResolvedRunRequest) -> str:
    """Bind preflight to logical create intent without exposing its secrets."""

    if not isinstance(request, ResolvedRunRequest):
        raise TypeError("run request subject requires a ResolvedRunRequest")
    spec = request.spec
    return _subject_digest(
        {
            "schema_version": 1,
            "dispatch_key": {
                "runtime_kind": request.dispatch_key.runtime_kind,
                "backend": request.dispatch_key.backend,
            },
            "name": spec.name,
            "stack": {
                "base": {
                    "digest": spec.stack.base.digest,
                    "disk_format": spec.stack.base.disk_format,
                    "arch": spec.stack.base.arch,
                    "os_variant": spec.stack.base.os_variant,
                },
                "layers": tuple({"digest": item.digest, "media_type": item.media_type} for item in spec.stack.layers),
            },
            "memory_mib": spec.memory_mib,
            "vcpus": spec.vcpus,
            "network": spec.network,
            "writable_overlay": spec.writable_overlay,
            "seed": spec.seed,
            "ports": spec.ports,
            "volume_intents": request.volume_intents,
            "environment": spec.environment,
            "cloud_init": _cloud_init_binding_projection(spec.cloud_init),
        }
    )


def existing_record_subject_digest(record: ExistingRunRecord) -> str:
    """Bind an operation profile to one exact durable run identity."""

    if not isinstance(record, ExistingRunRecord):
        raise TypeError("existing run subject requires an ExistingRunRecord")
    return _subject_digest(
        {
            "schema_version": 1,
            "name": record.name,
            "run_id": record.run_id,
            "state_schema_version": record.state_schema_version,
            "runtime_kind": record.dispatch_key.runtime_kind,
            "backend": record.dispatch_key.backend,
        }
    )


@dataclass(frozen=True, slots=True)
class ExpectedRunIdentity:
    """Durable identity a project expects before mutating an existing run."""

    name: str
    run_id: str
    dispatch_key: DispatchKey

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _RUN_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("expected run identity has an invalid name")
        if not isinstance(self.run_id, str):
            raise TypeError("expected run identity requires a string run ID")
        try:
            parsed_run_id = uuid.UUID(self.run_id)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("expected run identity has an invalid run ID") from None
        if str(parsed_run_id) != self.run_id:
            raise ValueError("expected run identity run ID is not canonical")
        if not isinstance(self.dispatch_key, DispatchKey):
            raise TypeError("expected run identity requires a DispatchKey")


ALLOWED_RUNTIME_STATUSES = {
    RuntimeKind.CLOUD_IMAGE: frozenset(
        {"creating", "defined", "starting", "running", "stopping", "stopped", "removed", "failed"}
    ),
    RuntimeKind.OCI_ROOT: frozenset(
        {
            "creating",
            "defined",
            "fetching",
            "converting",
            "root-mounted",
            "starting",
            "running",
            "stopping",
            "stopped",
            "exited",
            "removing",
            "removed",
            "failed",
        }
    ),
}

_SUMMARY_DETAIL_KEYS = frozenset(
    {
        "base_digest",
        "base_arch",
        "layers",
        "memory_mib",
        "vcpus",
        "network",
        "ports",
        "volumes",
        "ssh",
        "guest_ip",
        "created_at",
        "updated_at",
    }
)
_SUMMARY_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUMMARY_LOGICAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_SUMMARY_NETWORK_RE = re.compile(r"^(?:none|default|vzNAT|lima:[a-z0-9][a-z0-9-]{0,62}|[a-z0-9][a-z0-9_.-]{0,62})$")
_SUMMARY_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$")


def _validate_frozen_projection(value: Any) -> None:
    if value is None or type(value) in {bool, int, float, str}:
        if isinstance(value, str) and "-----BEGIN" in value and "KEY-----" in value:
            raise ValueError("run summary contains forbidden key material")
        return
    if isinstance(value, tuple):
        for item in value:
            _validate_frozen_projection(item)
        return
    if isinstance(value, MappingProxyType):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("run summary projection keys must be strings")
            if any(word in key.lower() for word in ("token", "secret", "password", "private_key")):
                raise ValueError("run summary contains a forbidden field")
            _validate_frozen_projection(item)
        return
    raise TypeError("run summary projection must be deeply immutable")


def _valid_ip(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or _SUMMARY_TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo == UTC


def _valid_mount_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    return (
        path.is_absolute()
        and not any(part in {".", ".."} for part in path.parts)
        and not value.startswith("//")
        and str(path) == value
        and value != "/"
        and not any(value == prefix or value.startswith(prefix + "/") for prefix in ("/dev", "/proc", "/sys"))
    )


def _validate_summary_details(details: Mapping[str, Any]) -> None:
    if set(details) != _SUMMARY_DETAIL_KEYS:
        raise ValueError("run summary requires the exact public detail fields")
    digest = details["base_digest"]
    if not isinstance(digest, str) or (digest and _SUMMARY_DIGEST_RE.fullmatch(digest) is None):
        raise ValueError("run summary has an invalid base digest")
    if details["base_arch"] not in {"", "x86_64", "aarch64"}:
        raise ValueError("run summary has an invalid base architecture")
    for field, minimum, maximum in (("memory_mib", 256, 1_048_576), ("vcpus", 1, 256)):
        value = details[field]
        if value is not None and (type(value) is not int or not minimum <= value <= maximum):
            raise ValueError("run summary has an invalid numeric field")
    network = details["network"]
    if network is not None and (not isinstance(network, str) or _SUMMARY_NETWORK_RE.fullmatch(network) is None):
        raise ValueError("run summary has an invalid network")
    for field in ("guest_ip",):
        if details[field] is not None and not _valid_ip(details[field]):
            raise ValueError("run summary has an invalid IP address")
    for field in ("created_at", "updated_at"):
        if details[field] is not None and not _valid_timestamp(details[field]):
            raise ValueError("run summary has an invalid timestamp")

    layers = details["layers"]
    if not isinstance(layers, tuple):
        raise TypeError("run summary layers must be immutable")
    for layer in layers:
        if (
            not isinstance(layer, Mapping)
            or isinstance(layer, dict)
            or set(layer)
            not in (
                {"digest"},
                {"digest", "target_dev"},
            )
        ):
            raise ValueError("run summary has an invalid layer")
        if not isinstance(layer["digest"], str) or _SUMMARY_DIGEST_RE.fullmatch(layer["digest"]) is None:
            raise ValueError("run summary has an invalid layer digest")
        if "target_dev" in layer and (
            not isinstance(layer["target_dev"], str) or re.fullmatch(r"vd[b-z]", layer["target_dev"]) is None
        ):
            raise ValueError("run summary has an invalid layer target")

    ports = details["ports"]
    if not isinstance(ports, tuple):
        raise TypeError("run summary ports must be immutable")
    for port in ports:
        if (
            not isinstance(port, Mapping)
            or isinstance(port, dict)
            or set(port)
            != {
                "host_ip",
                "host_port",
                "guest_port",
                "protocol",
            }
        ):
            raise ValueError("run summary has an invalid port")
        if not _valid_ip(port["host_ip"]):
            raise ValueError("run summary has an invalid port IP")
        if any(type(port[field]) is not int or not 1 <= port[field] <= 65_535 for field in ("host_port", "guest_port")):
            raise ValueError("run summary has an invalid port number")
        if port["protocol"] not in {"tcp", "udp"}:
            raise ValueError("run summary has an invalid port protocol")

    volumes = details["volumes"]
    if not isinstance(volumes, tuple):
        raise TypeError("run summary volumes must be immutable")
    allowed_volume_fields = {"name", "mount_path", "filesystem", "read_only", "target_dev"}
    for volume in volumes:
        if (
            not isinstance(volume, Mapping)
            or isinstance(volume, dict)
            or "name" not in volume
            or not set(volume).issubset(allowed_volume_fields)
            or not isinstance(volume["name"], str)
            or _SUMMARY_LOGICAL_NAME_RE.fullmatch(volume["name"]) is None
        ):
            raise ValueError("run summary has an invalid volume")
        if "mount_path" in volume and not _valid_mount_path(volume["mount_path"]):
            raise ValueError("run summary has an invalid volume mount")
        if "filesystem" in volume and volume["filesystem"] != "ext4":
            raise ValueError("run summary has an invalid volume filesystem")
        if "read_only" in volume and type(volume["read_only"]) is not bool:
            raise ValueError("run summary has an invalid volume policy")
        if "target_dev" in volume and (
            not isinstance(volume["target_dev"], str) or re.fullmatch(r"vd[b-z]", volume["target_dev"]) is None
        ):
            raise ValueError("run summary has an invalid volume target")

    ssh = details["ssh"]
    if not isinstance(ssh, Mapping) or isinstance(ssh, dict) or set(ssh) != {"host", "port"}:
        raise ValueError("run summary has an invalid SSH endpoint")
    if ssh["host"] is not None and not _valid_ip(ssh["host"]):
        raise ValueError("run summary has an invalid SSH host")
    if type(ssh["port"]) is not int or not 1 <= ssh["port"] <= 65_535:
        raise ValueError("run summary has an invalid SSH port")


@dataclass(frozen=True, slots=True)
class InspectLifecycle:
    """Allowlisted durable lifecycle position captured by one ledger read."""

    status: str
    lifecycle_revision: int
    created_at: str | None
    updated_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in ALLOWED_RUNTIME_STATUSES[RuntimeKind.CLOUD_IMAGE]:
            raise ValueError("inspect lifecycle has an invalid status")
        if type(self.lifecycle_revision) is not int or not 0 <= self.lifecycle_revision <= _MAX_LIFECYCLE_REVISION:
            raise ValueError("inspect lifecycle has an invalid revision")
        if self.created_at is not None and not _valid_timestamp(self.created_at):
            raise ValueError("inspect lifecycle has an invalid creation timestamp")
        if self.updated_at is not None and not _valid_timestamp(self.updated_at):
            raise ValueError("inspect lifecycle has an invalid update timestamp")


@dataclass(frozen=True, slots=True)
class InspectBase:
    digest: str | None
    arch: str | None
    disk_format: str | None

    def __post_init__(self) -> None:
        if self.digest is not None and _SUMMARY_DIGEST_RE.fullmatch(self.digest) is None:
            raise ValueError("inspect base has an invalid digest")
        if self.arch is not None and self.arch not in {"x86_64", "aarch64"}:
            raise ValueError("inspect base has an invalid architecture")
        if self.disk_format is not None and self.disk_format not in {"qcow2", "raw"}:
            raise ValueError("inspect base has an invalid disk format")


@dataclass(frozen=True, slots=True)
class InspectLayer:
    digest: str
    target_dev: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or _SUMMARY_DIGEST_RE.fullmatch(self.digest) is None:
            raise ValueError("inspect layer has an invalid digest")
        if self.target_dev is not None and re.fullmatch(r"vd[b-z]", self.target_dev) is None:
            raise ValueError("inspect layer has an invalid target")


@dataclass(frozen=True, slots=True)
class InspectPort:
    host_ip: str
    host_port: int
    guest_port: int
    protocol: str

    def __post_init__(self) -> None:
        if not _valid_ip(self.host_ip):
            raise ValueError("inspect port has an invalid host IP")
        if any(type(value) is not int or not 1 <= value <= 65_535 for value in (self.host_port, self.guest_port)):
            raise ValueError("inspect port has an invalid port number")
        if self.protocol not in {"tcp", "udp"}:
            raise ValueError("inspect port has an invalid protocol")


@dataclass(frozen=True, slots=True)
class InspectVolume:
    name: str
    mount_path: str | None = None
    filesystem: str | None = None
    read_only: bool | None = None
    target_dev: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SUMMARY_LOGICAL_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("inspect volume has an invalid name")
        if self.mount_path is not None and not _valid_mount_path(self.mount_path):
            raise ValueError("inspect volume has an invalid mount path")
        if self.filesystem is not None and self.filesystem != "ext4":
            raise ValueError("inspect volume has an invalid filesystem")
        if self.read_only is not None and type(self.read_only) is not bool:
            raise TypeError("inspect volume has an invalid read-only policy")
        if self.target_dev is not None and re.fullmatch(r"vd[b-z]", self.target_dev) is None:
            raise ValueError("inspect volume has an invalid target")


@dataclass(frozen=True, slots=True)
class InspectSshEndpoint:
    host: str | None
    port: int

    def __post_init__(self) -> None:
        if self.host is not None and not _valid_ip(self.host):
            raise ValueError("inspect SSH endpoint has an invalid host")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("inspect SSH endpoint has an invalid port")


@dataclass(frozen=True, slots=True)
class CloudImageInspectDetail:
    """Public cloud-image detail without host paths or backend identifiers."""

    base: InspectBase
    layers: tuple[InspectLayer, ...]
    memory_mib: int | None
    vcpus: int | None
    network: str | None
    ports: tuple[InspectPort, ...]
    volumes: tuple[InspectVolume, ...]
    ssh: InspectSshEndpoint
    guest_ip: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.base, InspectBase):
            raise TypeError("cloud-image inspect detail requires a base")
        if not isinstance(self.layers, tuple) or not all(isinstance(item, InspectLayer) for item in self.layers):
            raise TypeError("cloud-image inspect detail requires immutable layers")
        if not isinstance(self.ports, tuple) or not all(isinstance(item, InspectPort) for item in self.ports):
            raise TypeError("cloud-image inspect detail requires immutable ports")
        if not isinstance(self.volumes, tuple) or not all(isinstance(item, InspectVolume) for item in self.volumes):
            raise TypeError("cloud-image inspect detail requires immutable volumes")
        for value, minimum, maximum in ((self.memory_mib, 256, 1_048_576), (self.vcpus, 1, 256)):
            if value is not None and (type(value) is not int or not minimum <= value <= maximum):
                raise ValueError("cloud-image inspect detail has an invalid numeric field")
        if self.network is not None and (
            not isinstance(self.network, str) or _SUMMARY_NETWORK_RE.fullmatch(self.network) is None
        ):
            raise ValueError("cloud-image inspect detail has an invalid network")
        if not isinstance(self.ssh, InspectSshEndpoint):
            raise TypeError("cloud-image inspect detail requires an SSH endpoint")
        if self.guest_ip is not None and not _valid_ip(self.guest_ip):
            raise ValueError("cloud-image inspect detail has an invalid guest IP")


@dataclass(frozen=True, slots=True)
class InspectRecord:
    """Versioned, deeply immutable public projection of one pinned run ledger."""

    schema_version: int
    record: ExistingRunRecord
    lifecycle: InspectLifecycle
    detail: CloudImageInspectDetail
    warnings: tuple[InspectWarningCategory, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("inspect record has an unsupported response schema version")
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("inspect record requires an ExistingRunRecord")
        if self.record.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE:
            raise ValueError("inspect record requires a cloud-image run")
        if not isinstance(self.lifecycle, InspectLifecycle):
            raise TypeError("inspect record requires an InspectLifecycle")
        if not isinstance(self.detail, CloudImageInspectDetail):
            raise TypeError("inspect record requires a cloud-image detail")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(item, InspectWarningCategory) for item in self.warnings
        ):
            raise TypeError("inspect record requires immutable warnings")
        if len(self.warnings) != len(set(self.warnings)):
            raise ValueError("inspect record contains duplicate warnings")


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Allowlisted immutable projection of one durable or live-refreshed run."""

    record: ExistingRunRecord
    status: str
    details: Mapping[str, Any]
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExistingRunRecord):
            raise TypeError("run summary requires an ExistingRunRecord")
        if (
            not isinstance(self.status, str)
            or self.status not in ALLOWED_RUNTIME_STATUSES[self.record.dispatch_key.runtime_kind]
        ):
            raise ValueError("run summary has an invalid runtime status")
        if not isinstance(self.details, MappingProxyType):
            raise TypeError("run summary requires immutable detail mapping")
        _validate_summary_details(self.details)
        _validate_frozen_projection(self.details)
        if type(self.stale) is not bool:
            raise TypeError("run summary stale flag must be a bool")

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def runtime_kind(self) -> RuntimeKind:
        return self.record.dispatch_key.runtime_kind

    @property
    def backend(self) -> RuntimeBackend:
        return self.record.dispatch_key.backend


_AGGREGATION_ERROR_CODES = frozenset(
    {
        "invalid-entry",
        "invalid-ledger",
        "runtime-capability",
        "runtime-failure",
        "runtime-warning",
    }
)


@dataclass(frozen=True, slots=True)
class RunAggregationError:
    """Stable, non-reflective metadata for one failed aggregation entry."""

    name: str | None
    entry_token: str | None
    operation: RuntimeOperation
    dispatch_key: DispatchKey | None
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.name is not None and (not isinstance(self.name, str) or _RUN_NAME_RE.fullmatch(self.name) is None):
            raise ValueError("run aggregation error has an invalid name")
        if self.name is None:
            if not isinstance(self.entry_token, str) or re.fullmatch(r"entry-[0-9a-f]{12}", self.entry_token) is None:
                raise ValueError("anonymous run aggregation error requires a stable entry token")
        elif self.entry_token is not None:
            raise ValueError("named run aggregation errors cannot have an entry token")
        if not isinstance(self.operation, RuntimeOperation):
            raise TypeError("run aggregation error requires a RuntimeOperation")
        if self.dispatch_key is not None and not isinstance(self.dispatch_key, DispatchKey):
            raise TypeError("run aggregation error dispatch key must be a DispatchKey")
        if self.code not in _AGGREGATION_ERROR_CODES:
            raise ValueError("run aggregation error has an invalid code")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("run aggregation error requires a message")


@dataclass(frozen=True, slots=True)
class RunAggregationResult:
    """Deterministic valid summaries plus independent per-entry failures."""

    summaries: tuple[RunSummary, ...]
    errors: tuple[RunAggregationError, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.summaries, tuple) or not all(isinstance(item, RunSummary) for item in self.summaries):
            raise TypeError("run aggregation result requires RunSummary values")
        if not isinstance(self.errors, tuple) or not all(isinstance(item, RunAggregationError) for item in self.errors):
            raise TypeError("run aggregation result requires RunAggregationError values")
        names = tuple(item.name for item in self.summaries)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("run aggregation summaries must have unique sorted names")
        error_keys = tuple(
            (
                item.name or item.entry_token or "",
                item.code,
                item.operation.value,
            )
            for item in self.errors
        )
        if error_keys != tuple(sorted(error_keys)) or len(error_keys) != len(set(error_keys)):
            raise ValueError("run aggregation errors must be unique and deterministically sorted")


class RuntimeCapabilityError(PalimpsestError):
    """An exact runtime/backend pair cannot yet perform an operation."""

    code = "runtime-operation-unavailable"

    def __init__(self, operation: RuntimeOperation, dispatch_key: DispatchKey) -> None:
        self.operation = operation
        self.dispatch_key = dispatch_key
        super().__init__(
            f"runtime operation '{operation.value}' is unavailable for "
            f"{dispatch_key.runtime_kind.value}/{dispatch_key.backend.value}"
        )


__all__ = (
    "ALLOWED_RUNTIME_COMBINATIONS",
    "ALLOWED_RUNTIME_STATUSES",
    "CapabilityCheck",
    "CapabilityErrorCategory",
    "CapabilityProfile",
    "CapabilityRequirement",
    "CloudImageInspectDetail",
    "CloudInitSnapshot",
    "CloudInitWriteFileSnapshot",
    "CommitResult",
    "DispatchKey",
    "ExecRequest",
    "ExistingRunRecord",
    "ExpectedRunIdentity",
    "InspectBase",
    "InspectLayer",
    "InspectLifecycle",
    "InspectPort",
    "InspectRecord",
    "InspectSshEndpoint",
    "InspectVolume",
    "InspectWarningCategory",
    "LifecycleCursor",
    "LifecycleResult",
    "LifecycleWarningCategory",
    "LogCursor",
    "LogDataEvent",
    "LogErrorCategory",
    "LogEvent",
    "LogMode",
    "LogSourceStream",
    "LogStream",
    "LogStreamError",
    "LogTerminalCategory",
    "LogTerminalEvent",
    "LogTerminalOutcome",
    "ProcessCapabilities",
    "ProcessCapabilityError",
    "ProcessEvent",
    "ProcessExit",
    "ProcessExitCategory",
    "ProcessOutputEvent",
    "ProcessSession",
    "ProcessSignal",
    "ProcessStatusEvent",
    "ProcessStream",
    "PreflightReport",
    "PreflightReportPurpose",
    "ResolvedRunRequest",
    "RunAttachmentMode",
    "RunAggregationError",
    "RunAggregationResult",
    "RunResult",
    "RunWarningCategory",
    "RunRequestProvenance",
    "RunRequestProvenanceStage",
    "RunSummary",
    "RunVolumeIntent",
    "RuntimeBackend",
    "RuntimeCapabilityError",
    "RuntimeKind",
    "RuntimeOperation",
    "RuntimePreflightError",
    "existing_record_subject_digest",
    "run_request_subject_digest",
    "snapshot_cloud_init",
)
