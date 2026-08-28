"""Small immutable contracts shared by runtime ledger routing."""

from __future__ import annotations

import ipaddress
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from .errors import PalimpsestError
from .refs import RunSpec

_RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


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


class RunAttachmentMode(StrEnum):
    """How a successful create result is attached to its caller.

    Foreground process sessions are intentionally a later contract. The typed
    create boundary currently returns only completed, detached VM launches.
    """

    DETACHED = "detached"


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
    managed volume artifacts. It is deliberately not a capability token: the
    current backend-only preflight has no freshness or request-binding contract.
    """

    dispatch_key: DispatchKey
    spec: RunSpec = dataclass_field(repr=False)
    volume_intents: tuple[RunVolumeIntent, ...] = dataclass_field(default=(), repr=False)
    attachments_bound: bool = True

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
        if self.ready != (self.status == "running"):
            raise ValueError("run result readiness does not match runtime status")
        if not isinstance(self.attachment_mode, RunAttachmentMode):
            raise TypeError("run result requires a RunAttachmentMode")
        if self.guest_ip is not None and not _valid_ip(self.guest_ip):
            raise ValueError("run result has an invalid guest IP")

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
    "DispatchKey",
    "ExistingRunRecord",
    "ExpectedRunIdentity",
    "ResolvedRunRequest",
    "RunAttachmentMode",
    "RunAggregationError",
    "RunAggregationResult",
    "RunResult",
    "RunSummary",
    "RunVolumeIntent",
    "RuntimeBackend",
    "RuntimeCapabilityError",
    "RuntimeKind",
    "RuntimeOperation",
)
