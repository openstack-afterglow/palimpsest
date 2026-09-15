"""Pure canonical observations for Palimpsest baseline measurements.

This module is deliberately limited to immutable data contracts.  Adapters are
responsible for collecting evidence and must represent an unavailable value
with a reason instead of estimating or fabricating it here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .digest import require_digest
from .errors import ArtifactValidationError, InvalidDigestError

METRIC_SCHEMA_VERSION = 1
INT64_MAX = 2**63 - 1
MAX_METRIC_JSON_BYTES = 1024 * 1024
MAX_METRIC_JSON_DEPTH = 16
MAX_EVIDENCE_ITEMS = 128

PHASES = (
    "resolve",
    "locate",
    "fetch",
    "verify",
    "materialize",
    "vm_boot",
    "mount",
    "initialize",
    "model_load",
    "application_ready",
)
CACHE_TEMPERATURES = frozenset({"cold", "warm"})
CACHE_HIT_LEVELS = frozenset({"miss", "node", "region", "prefetched", "retained-root"})
PLACEMENTS = frozenset({"node-local", "region-local", "cross-region", "retained-root"})
DISTRIBUTION_MODES = frozenset({"registry-pull", "shared-store", "prefetched", "retained-root"})
MATERIALIZATION_MODES = frozenset({"eager", "lazy", "prebuilt-root"})
MISSING_REASONS = frozenset(
    {
        "not_applicable",
        "not_executed",
        "not_reported",
        "not_supported",
        "permission_denied",
        "collection_failed",
        "clock_incompatible",
        "evidence_missing",
    }
)
PHASE_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "timed_out", "skipped", "not_applicable"})
RUN_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})
FAILURE_CATEGORIES = frozenset(
    {
        "source_unavailable",
        "resolution",
        "verification",
        "materialization",
        "vm_boot",
        "mount",
        "initialization",
        "model_load",
        "application",
        "timeout",
        "resource_exhausted",
        "policy_denied",
        "unsupported",
        "cancelled",
        "internal",
    }
)
CLOCK_SOURCES = frozenset({"monotonic", "boottime", "steady"})
RESOURCE_SCOPES = frozenset({"process", "vm", "host"})
RESOURCE_BOUNDARIES = frozenset({"adapter", "workload", "guest", "whole-run"})

_ALL_PHASES = frozenset(PHASES)
# Resource, policy, support, timeout, cancellation, and internal failures are
# intentionally cross-cutting, but remain bounded to the canonical phase set.
FAILURE_CATEGORY_PHASES = MappingProxyType(
    {
        "source_unavailable": frozenset({"resolve", "locate", "fetch"}),
        "resolution": frozenset({"resolve"}),
        "verification": frozenset({"verify"}),
        "materialization": frozenset({"materialize"}),
        "vm_boot": frozenset({"vm_boot"}),
        "mount": frozenset({"mount"}),
        "initialization": frozenset({"initialize"}),
        "model_load": frozenset({"model_load"}),
        "application": frozenset({"application_ready"}),
        "timeout": _ALL_PHASES,
        "resource_exhausted": _ALL_PHASES,
        "policy_denied": _ALL_PHASES,
        "unsupported": _ALL_PHASES,
        "cancelled": _ALL_PHASES,
        "internal": _ALL_PHASES,
    }
)

_EXECUTED_TIMING_MISSING_REASONS = frozenset(
    {"not_reported", "not_supported", "permission_denied", "collection_failed", "clock_incompatible"}
)

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_APPROVED_EVIDENCE_SCHEMES = frozenset({"artifact", "https", "s3", "gs", "oci"})
_EVIDENCE_URI_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/]*)(/[^?#]*)?$")
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_OPAQUE_AUTHORITY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252}[A-Za-z0-9])?$")
_EVIDENCE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:authorization|cookie|token|password|passwd|secret|credential|private[_-]?key|api[_-]?key|"
    r"client[_-]?secret|access[_-]?key|signature)[:=]",
    re.IGNORECASE,
)
_AUTH_MATERIAL_RE = re.compile(r"(?:^|/)(?:bearer|basic)[:=]", re.IGNORECASE)
_PATH_USERINFO_RE = re.compile(r"(?:^|/)[^/@:]+:[^/@]+@[^/]+(?:/|$)")
_JWT_SEGMENT_RE = re.compile(r"(?:^|/)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:/|$)")
_AWS_ACCESS_KEY_SEGMENT_RE = re.compile(r"(?:^|/)(?:AKIA|ASIA)[A-Z0-9]{16}(?:/|$)")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical compact UTF-8 JSON without non-finite numbers."""
    encoded: bytes | None = None
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        pass
    if encoded is None:
        raise ArtifactValidationError("metric value is not canonical JSON data") from None
    return encoded


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _plain_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip():
        raise ArtifactValidationError(f"{field_name} must be a nonempty string without surrounding whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ArtifactValidationError(f"{field_name} cannot contain control characters")
    return value


def _token(value: Any, field_name: str) -> str:
    token = _plain_string(value, field_name)
    if _TOKEN_RE.fullmatch(token) is None:
        raise ArtifactValidationError(f"{field_name} must use canonical lowercase token syntax")
    return token


def _exact_fields(data: Any, expected: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ArtifactValidationError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in data):
        raise ArtifactValidationError(f"invalid field name in {field_name}: object keys must be strings")
    actual = set(data)
    if actual != expected:
        raise ArtifactValidationError(f"invalid fields in {field_name}")
    return data


def _canonical_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} must be a canonical sha256 digest")
    normalized: str | None = None
    try:
        normalized = require_digest(value)
    except InvalidDigestError:
        pass
    if normalized is None:
        raise ArtifactValidationError(f"{field_name} must be a canonical sha256 digest") from None
    if normalized != value:
        raise ArtifactValidationError(f"{field_name} must use canonical sha256:<lowercase-hex> syntax")
    return value


def _canonical_dns_authority(authority: str, *, omit_port: int | None = None) -> str:
    if not authority or "@" in authority or authority.count(":") > 1:
        raise ArtifactValidationError("evidence.uri has an invalid network authority")
    if ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        if not port_text.isascii() or not port_text.isdigit():
            raise ArtifactValidationError("evidence.uri has an invalid network port")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ArtifactValidationError("evidence.uri has an invalid network port")
        canonical_port = None if port == omit_port else str(port)
    else:
        host = authority
        canonical_port = None
    if len(host) > 253:
        raise ArtifactValidationError("evidence.uri has an invalid DNS host")
    labels = host.split(".")
    if any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels):
        raise ArtifactValidationError("evidence.uri has an invalid DNS host")
    canonical_host = host.lower()
    return canonical_host if canonical_port is None else f"{canonical_host}:{canonical_port}"


def _canonical_evidence_uri(value: Any) -> str:
    uri = _plain_string(value, "evidence.uri")
    if (
        len(uri) > 2048
        or any(ord(character) <= 0x20 or ord(character) >= 0x7F for character in uri)
        or any(character in uri for character in "\\%?#|[]")
    ):
        raise ArtifactValidationError("evidence.uri contains unsafe raw characters")
    match = _EVIDENCE_URI_RE.fullmatch(uri)
    if match is None:
        raise ArtifactValidationError("evidence.uri must be an absolute approved URI")
    scheme = match.group(1).lower()
    authority = match.group(2)
    path = match.group(3) or ""
    if scheme not in _APPROVED_EVIDENCE_SCHEMES:
        raise ArtifactValidationError("evidence.uri uses an unsupported scheme")
    if scheme in {"https", "oci"}:
        canonical_authority = _canonical_dns_authority(authority, omit_port=443 if scheme == "https" else None)
    elif scheme in {"s3", "gs"}:
        if not 3 <= len(authority) <= 63 or authority != authority.lower():
            raise ArtifactValidationError("evidence.uri has an invalid bucket authority")
        labels = authority.split(".")
        if any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels):
            raise ArtifactValidationError("evidence.uri has an invalid bucket authority")
        canonical_authority = authority
    else:
        if _OPAQUE_AUTHORITY_RE.fullmatch(authority) is None:
            raise ArtifactValidationError("evidence.uri has an invalid artifact authority")
        canonical_authority = authority
    if path and _EVIDENCE_PATH_RE.fullmatch(path) is None:
        raise ArtifactValidationError("evidence.uri has an invalid evidence path")
    if (
        any(segment in {".", ".."} for segment in path.split("/"))
        or _SECRET_ASSIGNMENT_RE.search(uri)
        or _AUTH_MATERIAL_RE.search(path)
        or _PATH_USERINFO_RE.search(path)
        or _JWT_SEGMENT_RE.search(path)
        or _AWS_ACCESS_KEY_SEGMENT_RE.search(path)
    ):
        raise ArtifactValidationError("evidence.uri cannot contain unsafe path material")
    if scheme == "https" and not path:
        path = "/"
    return f"{scheme}://{canonical_authority}{path}"


@dataclass(frozen=True, slots=True)
class ObservedInt:
    """An exact nonnegative int64 measurement or an honest missing reason."""

    value: int | None
    missing_reason: str | None

    def __post_init__(self) -> None:
        if self.value is None:
            if not isinstance(self.missing_reason, str) or self.missing_reason not in MISSING_REASONS:
                raise ArtifactValidationError("a missing integer observation requires a supported missing_reason")
            return
        if type(self.value) is not int or not 0 <= self.value <= INT64_MAX:
            raise ArtifactValidationError("observed integer value must be an exact nonnegative int64")
        if self.missing_reason is not None:
            raise ArtifactValidationError("an observed integer value cannot also have a missing_reason")

    @classmethod
    def measured(cls, value: int) -> ObservedInt:
        return cls(value=value, missing_reason=None)

    @classmethod
    def missing(cls, reason: str) -> ObservedInt:
        return cls(value=None, missing_reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {"missing_reason": self.missing_reason, "value": self.value}

    @classmethod
    def from_dict(cls, data: Any, field_name: str) -> ObservedInt:
        value = _exact_fields(data, {"missing_reason", "value"}, field_name)
        return cls(value=value["value"], missing_reason=value["missing_reason"])


@dataclass(frozen=True, slots=True)
class ObservedText:
    """A disclosed string or an honest missing reason."""

    value: str | None
    missing_reason: str | None

    def __post_init__(self) -> None:
        if self.value is None:
            if not isinstance(self.missing_reason, str) or self.missing_reason not in MISSING_REASONS:
                raise ArtifactValidationError("a missing text observation requires a supported missing_reason")
            return
        _plain_string(self.value, "observed text value")
        if self.missing_reason is not None:
            raise ArtifactValidationError("an observed text value cannot also have a missing_reason")

    @classmethod
    def measured(cls, value: str) -> ObservedText:
        return cls(value=value, missing_reason=None)

    @classmethod
    def missing(cls, reason: str) -> ObservedText:
        return cls(value=None, missing_reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {"missing_reason": self.missing_reason, "value": self.value}

    @classmethod
    def from_dict(cls, data: Any, field_name: str) -> ObservedText:
        value = _exact_fields(data, {"missing_reason", "value"}, field_name)
        return cls(value=value["value"], missing_reason=value["missing_reason"])


@dataclass(frozen=True, slots=True)
class EnvironmentDisclosure:
    """System, adapter, host, and capacity context for one observation."""

    system: str
    system_version: ObservedText
    adapter: str
    adapter_version: ObservedText
    provider: ObservedText
    host_os: ObservedText
    architecture: ObservedText
    virtualization: ObservedText
    cpu_count: ObservedInt
    memory_bytes: ObservedInt

    def __post_init__(self) -> None:
        _token(self.system, "environment.system")
        _token(self.adapter, "environment.adapter")
        for field_name in (
            "system_version",
            "adapter_version",
            "provider",
            "host_os",
            "architecture",
            "virtualization",
        ):
            if not isinstance(getattr(self, field_name), ObservedText):
                raise ArtifactValidationError(f"environment.{field_name} must be ObservedText")
        for field_name in ("cpu_count", "memory_bytes"):
            observation = getattr(self, field_name)
            if not isinstance(observation, ObservedInt):
                raise ArtifactValidationError(f"environment.{field_name} must be ObservedInt")
            if observation.value == 0:
                raise ArtifactValidationError(f"environment.{field_name}, when measured, must be greater than zero")

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version.to_dict(),
            "architecture": self.architecture.to_dict(),
            "cpu_count": self.cpu_count.to_dict(),
            "host_os": self.host_os.to_dict(),
            "memory_bytes": self.memory_bytes.to_dict(),
            "provider": self.provider.to_dict(),
            "system": self.system,
            "system_version": self.system_version.to_dict(),
            "virtualization": self.virtualization.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> EnvironmentDisclosure:
        fields = {
            "adapter",
            "adapter_version",
            "architecture",
            "cpu_count",
            "host_os",
            "memory_bytes",
            "provider",
            "system",
            "system_version",
            "virtualization",
        }
        value = _exact_fields(data, fields, "environment")
        return cls(
            system=value["system"],
            system_version=ObservedText.from_dict(value["system_version"], "environment.system_version"),
            adapter=value["adapter"],
            adapter_version=ObservedText.from_dict(value["adapter_version"], "environment.adapter_version"),
            provider=ObservedText.from_dict(value["provider"], "environment.provider"),
            host_os=ObservedText.from_dict(value["host_os"], "environment.host_os"),
            architecture=ObservedText.from_dict(value["architecture"], "environment.architecture"),
            virtualization=ObservedText.from_dict(value["virtualization"], "environment.virtualization"),
            cpu_count=ObservedInt.from_dict(value["cpu_count"], "environment.cpu_count"),
            memory_bytes=ObservedInt.from_dict(value["memory_bytes"], "environment.memory_bytes"),
        )


@dataclass(frozen=True, slots=True)
class ClockDisclosure:
    """Elapsed clock identity plus an independently observed wall-time anchor."""

    source: str
    resolution_ns: ObservedInt
    wall_started_unix_ns: ObservedInt

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or self.source not in CLOCK_SOURCES:
            raise ArtifactValidationError("clock.source must be a supported clock source")
        if not isinstance(self.resolution_ns, ObservedInt):
            raise ArtifactValidationError("clock.resolution_ns must be ObservedInt")
        if self.resolution_ns.value == 0:
            raise ArtifactValidationError("clock.resolution_ns, when measured, must be greater than zero")
        if not isinstance(self.wall_started_unix_ns, ObservedInt):
            raise ArtifactValidationError("clock.wall_started_unix_ns must be ObservedInt")

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution_ns": self.resolution_ns.to_dict(),
            "source": self.source,
            "wall_started_unix_ns": self.wall_started_unix_ns.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ClockDisclosure:
        value = _exact_fields(data, {"resolution_ns", "source", "wall_started_unix_ns"}, "clock")
        return cls(
            source=value["source"],
            resolution_ns=ObservedInt.from_dict(value["resolution_ns"], "clock.resolution_ns"),
            wall_started_unix_ns=ObservedInt.from_dict(value["wall_started_unix_ns"], "clock.wall_started_unix_ns"),
        )


@dataclass(frozen=True, slots=True)
class PhaseObservation:
    """Timing for one canonical phase relative to the disclosed run origin."""

    phase: str
    outcome: str
    started_at_ns: ObservedInt
    duration_ns: ObservedInt

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or self.phase not in PHASES:
            raise ArtifactValidationError("phase.phase must be a canonical phase")
        if not isinstance(self.outcome, str) or self.outcome not in PHASE_OUTCOMES:
            raise ArtifactValidationError("phase.outcome must be a supported phase outcome")
        if not isinstance(self.started_at_ns, ObservedInt) or not isinstance(self.duration_ns, ObservedInt):
            raise ArtifactValidationError("phase timestamps and durations must be ObservedInt")
        timings = (self.started_at_ns, self.duration_ns)
        if self.outcome in {"skipped", "not_applicable"}:
            required_reason = "not_executed" if self.outcome == "skipped" else "not_applicable"
            if any(item.value is not None for item in timings):
                raise ArtifactValidationError("skipped or inapplicable phases cannot claim timing measurements")
            if any(item.missing_reason != required_reason for item in timings):
                raise ArtifactValidationError("phase timing missing reason does not align with its outcome")
        elif any(
            item.value is None and item.missing_reason not in _EXECUTED_TIMING_MISSING_REASONS for item in timings
        ):
            raise ArtifactValidationError("phase timing missing reason does not align with an executed outcome")

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_ns": self.duration_ns.to_dict(),
            "outcome": self.outcome,
            "phase": self.phase,
            "started_at_ns": self.started_at_ns.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any, index: int) -> PhaseObservation:
        value = _exact_fields(data, {"duration_ns", "outcome", "phase", "started_at_ns"}, f"phases[{index}]")
        return cls(
            phase=value["phase"],
            outcome=value["outcome"],
            started_at_ns=ObservedInt.from_dict(value["started_at_ns"], f"phases[{index}].started_at_ns"),
            duration_ns=ObservedInt.from_dict(value["duration_ns"], f"phases[{index}].duration_ns"),
        )


@dataclass(frozen=True, slots=True)
class DistributionObservation:
    """Independent cache, locality, distribution, and storage dimensions."""

    executed: bool
    cache_temperature: str
    cache_hit_level: str
    placement: str
    distribution_mode: str
    materialization_mode: str
    source_region: ObservedText
    destination_region: ObservedText
    registry_bytes_received: ObservedInt
    cross_region_bytes_received: ObservedInt
    unique_storage_bytes: ObservedInt
    writable_upper_growth_bytes: ObservedInt

    def __post_init__(self) -> None:
        if not isinstance(self.executed, bool):
            raise ArtifactValidationError("distribution.executed must be a boolean")
        dimensions = (
            ("cache_temperature", self.cache_temperature, CACHE_TEMPERATURES | {"not-observed"}),
            ("cache_hit_level", self.cache_hit_level, CACHE_HIT_LEVELS | {"not-observed"}),
            ("placement", self.placement, PLACEMENTS | {"not-started"}),
            ("distribution_mode", self.distribution_mode, DISTRIBUTION_MODES | {"not-started"}),
            ("materialization_mode", self.materialization_mode, MATERIALIZATION_MODES | {"not-started"}),
        )
        for field_name, value, supported in dimensions:
            if not isinstance(value, str) or value not in supported:
                raise ArtifactValidationError(f"distribution.{field_name} has an unsupported value")
        for field_name in ("source_region", "destination_region"):
            if not isinstance(getattr(self, field_name), ObservedText):
                raise ArtifactValidationError(f"distribution.{field_name} must be ObservedText")
        for field_name in (
            "registry_bytes_received",
            "cross_region_bytes_received",
            "unique_storage_bytes",
            "writable_upper_growth_bytes",
        ):
            if not isinstance(getattr(self, field_name), ObservedInt):
                raise ArtifactValidationError(f"distribution.{field_name} must be ObservedInt")
        counters = (
            self.registry_bytes_received,
            self.cross_region_bytes_received,
            self.unique_storage_bytes,
            self.writable_upper_growth_bytes,
        )
        if not self.executed:
            expected_states = ("not-observed", "not-observed", "not-started", "not-started", "not-started")
            actual_states = (
                self.cache_temperature,
                self.cache_hit_level,
                self.placement,
                self.distribution_mode,
                self.materialization_mode,
            )
            if actual_states != expected_states:
                raise ArtifactValidationError("unexecuted distribution must use the canonical not-started profile")
            regions = (self.source_region, self.destination_region)
            if any(item.value is not None or item.missing_reason != "not_executed" for item in regions):
                raise ArtifactValidationError("unexecuted distribution regions must be missing as not_executed")
            if any(
                item.value not in {None, 0} or (item.value is None and item.missing_reason != "not_executed")
                for item in counters
            ):
                raise ArtifactValidationError(
                    "unexecuted distribution counters must be zero or missing as not_executed"
                )
            return
        not_started_states = {"not-observed", "not-started"}
        if any(value in not_started_states for _field_name, value, _supported in dimensions):
            raise ArtifactValidationError("executed distribution cannot use a not-started state")
        observations = (self.source_region, self.destination_region, *counters)
        if any(item.missing_reason == "not_executed" for item in observations):
            raise ArtifactValidationError(
                "executed distribution region/counter observations cannot be missing as not_executed"
            )
        if (self.cache_temperature == "cold") != (self.cache_hit_level == "miss"):
            raise ArtifactValidationError("distribution cache temperature and hit level contradict each other")
        if self.cache_hit_level == "node" and self.placement != "node-local":
            raise ArtifactValidationError("distribution node cache hits must use node-local placement")
        if self.cache_hit_level == "region" and self.placement != "region-local":
            raise ArtifactValidationError("distribution region cache hits must use region-local placement")
        if self.cache_hit_level == "prefetched" and self.distribution_mode != "prefetched":
            raise ArtifactValidationError("distribution prefetched cache hits must use prefetched distribution")
        retained_claims = (
            self.cache_hit_level == "retained-root",
            self.placement == "retained-root",
            self.distribution_mode == "retained-root",
        )
        if any(retained_claims) and not all(retained_claims):
            raise ArtifactValidationError("distribution retained-root dimensions must agree")
        if all(retained_claims) and self.materialization_mode != "prebuilt-root":
            raise ArtifactValidationError("distribution retained-root requires prebuilt-root materialization")
        source_region = self.source_region.value
        destination_region = self.destination_region.value
        if self.placement == "cross-region" and (
            source_region is None or destination_region is None or source_region == destination_region
        ):
            raise ArtifactValidationError("cross-region placement requires distinct disclosed regions")
        if (
            self.placement in {"node-local", "region-local"}
            and source_region is not None
            and destination_region is not None
            and source_region != destination_region
        ):
            raise ArtifactValidationError("local placement requires matching disclosed regions")
        cross_region_bytes = self.cross_region_bytes_received
        if self.placement == "cross-region":
            if cross_region_bytes.value == 0 or cross_region_bytes.missing_reason in {
                "not_applicable",
                "not_executed",
            }:
                raise ArtifactValidationError("cross-region placement requires an applicable nonzero byte counter")
        elif cross_region_bytes.value not in {None, 0}:
            raise ArtifactValidationError("local or retained placement cannot report cross-region bytes")
        registry_bytes = self.registry_bytes_received
        if self.distribution_mode == "registry-pull":
            if registry_bytes.value == 0 or registry_bytes.missing_reason in {"not_applicable", "not_executed"}:
                raise ArtifactValidationError("registry-pull requires an applicable nonzero registry byte counter")
        elif registry_bytes.value not in {None, 0}:
            raise ArtifactValidationError("non-registry distribution cannot report registry bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_hit_level": self.cache_hit_level,
            "cache_temperature": self.cache_temperature,
            "cross_region_bytes_received": self.cross_region_bytes_received.to_dict(),
            "distribution_mode": self.distribution_mode,
            "destination_region": self.destination_region.to_dict(),
            "executed": self.executed,
            "materialization_mode": self.materialization_mode,
            "placement": self.placement,
            "registry_bytes_received": self.registry_bytes_received.to_dict(),
            "source_region": self.source_region.to_dict(),
            "unique_storage_bytes": self.unique_storage_bytes.to_dict(),
            "writable_upper_growth_bytes": self.writable_upper_growth_bytes.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> DistributionObservation:
        fields = {
            "cache_hit_level",
            "cache_temperature",
            "cross_region_bytes_received",
            "destination_region",
            "distribution_mode",
            "executed",
            "materialization_mode",
            "placement",
            "registry_bytes_received",
            "source_region",
            "unique_storage_bytes",
            "writable_upper_growth_bytes",
        }
        value = _exact_fields(data, fields, "distribution")
        return cls(
            executed=value["executed"],
            cache_temperature=value["cache_temperature"],
            cache_hit_level=value["cache_hit_level"],
            placement=value["placement"],
            distribution_mode=value["distribution_mode"],
            materialization_mode=value["materialization_mode"],
            source_region=ObservedText.from_dict(value["source_region"], "distribution.source_region"),
            destination_region=ObservedText.from_dict(value["destination_region"], "distribution.destination_region"),
            registry_bytes_received=ObservedInt.from_dict(
                value["registry_bytes_received"], "distribution.registry_bytes_received"
            ),
            cross_region_bytes_received=ObservedInt.from_dict(
                value["cross_region_bytes_received"], "distribution.cross_region_bytes_received"
            ),
            unique_storage_bytes=ObservedInt.from_dict(
                value["unique_storage_bytes"], "distribution.unique_storage_bytes"
            ),
            writable_upper_growth_bytes=ObservedInt.from_dict(
                value["writable_upper_growth_bytes"], "distribution.writable_upper_growth_bytes"
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    """Comparable process and I/O resource counters."""

    scope: str
    boundary: str
    cpu_time_ns: ObservedInt
    peak_memory_bytes: ObservedInt
    disk_read_bytes: ObservedInt
    disk_write_bytes: ObservedInt
    network_receive_bytes: ObservedInt
    network_transmit_bytes: ObservedInt

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or self.scope not in RESOURCE_SCOPES:
            raise ArtifactValidationError("resources.scope must be a supported measurement scope")
        if not isinstance(self.boundary, str) or self.boundary not in RESOURCE_BOUNDARIES:
            raise ArtifactValidationError("resources.boundary must be a supported measurement boundary")
        for field_name in (
            "cpu_time_ns",
            "peak_memory_bytes",
            "disk_read_bytes",
            "disk_write_bytes",
            "network_receive_bytes",
            "network_transmit_bytes",
        ):
            if not isinstance(getattr(self, field_name), ObservedInt):
                raise ArtifactValidationError(f"resources.{field_name} must be ObservedInt")

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary": self.boundary,
            "cpu_time_ns": self.cpu_time_ns.to_dict(),
            "disk_read_bytes": self.disk_read_bytes.to_dict(),
            "disk_write_bytes": self.disk_write_bytes.to_dict(),
            "network_receive_bytes": self.network_receive_bytes.to_dict(),
            "network_transmit_bytes": self.network_transmit_bytes.to_dict(),
            "peak_memory_bytes": self.peak_memory_bytes.to_dict(),
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ResourceObservation:
        fields = {
            "boundary",
            "cpu_time_ns",
            "disk_read_bytes",
            "disk_write_bytes",
            "network_receive_bytes",
            "network_transmit_bytes",
            "peak_memory_bytes",
            "scope",
        }
        value = _exact_fields(data, fields, "resources")
        metric_fields = fields - {"boundary", "scope"}
        return cls(
            scope=value["scope"],
            boundary=value["boundary"],
            **{name: ObservedInt.from_dict(value[name], f"resources.{name}") for name in metric_fields},
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    """Stable run result and optional stable failure category."""

    status: str
    failure_category: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in RUN_OUTCOMES:
            raise ArtifactValidationError("outcome.status must be a supported run outcome")
        if self.status == "succeeded":
            if self.failure_category is not None:
                raise ArtifactValidationError("a successful outcome cannot have a failure_category")
            return
        if not isinstance(self.failure_category, str) or self.failure_category not in FAILURE_CATEGORIES:
            raise ArtifactValidationError("a non-success outcome requires a supported failure_category")
        if self.status == "cancelled" and self.failure_category != "cancelled":
            raise ArtifactValidationError("a cancelled outcome must use the cancelled failure_category")
        if self.status == "failed" and self.failure_category == "cancelled":
            raise ArtifactValidationError("a failed outcome cannot use the cancelled failure_category")

    def to_dict(self) -> dict[str, Any]:
        return {"failure_category": self.failure_category, "status": self.status}

    @classmethod
    def from_dict(cls, data: Any) -> Outcome:
        value = _exact_fields(data, {"failure_category", "status"}, "outcome")
        return cls(status=value["status"], failure_category=value["failure_category"])


@dataclass(frozen=True, slots=True)
class RawEvidence:
    """A retained raw receipt linked by URI and exact content digest."""

    uri: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "uri", _canonical_evidence_uri(self.uri))
        object.__setattr__(self, "digest", _canonical_digest(self.digest, "evidence.digest"))

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "uri": self.uri}

    @classmethod
    def from_dict(cls, data: Any, index: int) -> RawEvidence:
        value = _exact_fields(data, {"digest", "uri"}, f"evidence[{index}]")
        return cls(uri=value["uri"], digest=value["digest"])


@dataclass(frozen=True, slots=True)
class MetricEvent:
    """One canonical raw observation; percentile aggregation is a later layer."""

    run_id: str
    environment: EnvironmentDisclosure
    clock: ClockDisclosure
    phases: tuple[PhaseObservation, ...]
    distribution: DistributionObservation
    resources: ResourceObservation
    outcome: Outcome
    evidence: tuple[RawEvidence, ...]
    schema_version: int = METRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != METRIC_SCHEMA_VERSION:
            raise ArtifactValidationError("unsupported metric schema_version")
        if not isinstance(self.run_id, str) or _RUN_ID_RE.fullmatch(self.run_id) is None:
            raise ArtifactValidationError("run_id must be an opaque identifier token")
        if not isinstance(self.environment, EnvironmentDisclosure):
            raise ArtifactValidationError("environment must be EnvironmentDisclosure")
        if not isinstance(self.clock, ClockDisclosure):
            raise ArtifactValidationError("clock must be ClockDisclosure")
        if not isinstance(self.phases, tuple) or any(not isinstance(item, PhaseObservation) for item in self.phases):
            raise ArtifactValidationError("phases must be an immutable tuple of PhaseObservation values")
        actual_phases = tuple(item.phase for item in self.phases)
        if actual_phases != PHASES:
            raise ArtifactValidationError(f"phases must exactly follow canonical order: expected {PHASES!r}")
        known_starts = [item.started_at_ns.value for item in self.phases if item.started_at_ns.value is not None]
        if known_starts != sorted(known_starts):
            raise ArtifactValidationError("known phase started_at_ns values must be nondecreasing")
        if not isinstance(self.distribution, DistributionObservation):
            raise ArtifactValidationError("distribution must be DistributionObservation")
        if not isinstance(self.resources, ResourceObservation):
            raise ArtifactValidationError("resources must be ResourceObservation")
        if not isinstance(self.outcome, Outcome):
            raise ArtifactValidationError("outcome must be Outcome")
        terminal_indexes = [
            index for index, item in enumerate(self.phases) if item.outcome in {"failed", "cancelled", "timed_out"}
        ]
        if terminal_indexes:
            terminal_index = terminal_indexes[0]
            if any(item.outcome not in {"skipped", "not_applicable"} for item in self.phases[terminal_index + 1 :]):
                raise ArtifactValidationError("every phase after the first terminal phase must be nonexecuted")
            terminal_outcome = self.phases[terminal_index].outcome
            terminal_phase = self.phases[terminal_index].phase
        else:
            terminal_index = None
            terminal_outcome = None
            terminal_phase = None
        if self.outcome.status == "succeeded" and terminal_outcome is not None:
            raise ArtifactValidationError("a successful run cannot contain a terminal phase")
        if self.outcome.status == "cancelled" and terminal_outcome != "cancelled":
            raise ArtifactValidationError("a cancelled run must identify a cancelled terminal phase")
        if self.outcome.status == "failed":
            expected_terminal = "timed_out" if self.outcome.failure_category == "timeout" else "failed"
            if terminal_outcome != expected_terminal:
                raise ArtifactValidationError("a failed run must identify an aligned terminal phase")
            if terminal_phase not in FAILURE_CATEGORY_PHASES[self.outcome.failure_category]:
                raise ArtifactValidationError("failure category is not valid for the terminal phase")
        fetch_index = PHASES.index("fetch")
        if terminal_index is not None and terminal_index < fetch_index and self.distribution.executed:
            raise ArtifactValidationError("a pre-fetch terminal phase requires unexecuted distribution evidence")
        if not self.distribution.executed and (terminal_index is None or terminal_index > fetch_index):
            raise ArtifactValidationError("unexecuted distribution requires a terminal phase no later than fetch")
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ArtifactValidationError("evidence must be a nonempty immutable tuple")
        if len(self.evidence) > MAX_EVIDENCE_ITEMS:
            raise ArtifactValidationError("evidence exceeds the supported item count")
        if any(not isinstance(item, RawEvidence) for item in self.evidence):
            raise ArtifactValidationError("evidence must contain only RawEvidence values")
        evidence_uris = [item.uri for item in self.evidence]
        if len(evidence_uris) != len(set(evidence_uris)):
            raise ArtifactValidationError("each evidence URI must be unique")
        object.__setattr__(self, "evidence", tuple(sorted(self.evidence, key=lambda item: (item.uri, item.digest))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clock": self.clock.to_dict(),
            "distribution": self.distribution.to_dict(),
            "environment": self.environment.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "outcome": self.outcome.to_dict(),
            "phases": [item.to_dict() for item in self.phases],
            "resources": self.resources.to_dict(),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return _sha256(self.to_json_bytes())

    @classmethod
    def from_dict(cls, data: Any) -> MetricEvent:
        fields = {
            "clock",
            "distribution",
            "environment",
            "evidence",
            "outcome",
            "phases",
            "resources",
            "run_id",
            "schema_version",
        }
        value = _exact_fields(data, fields, "metric event")
        raw_phases = value["phases"]
        raw_evidence = value["evidence"]
        if not isinstance(raw_phases, list):
            raise ArtifactValidationError("phases must be an array")
        if not isinstance(raw_evidence, list):
            raise ArtifactValidationError("evidence must be an array")
        return cls(
            run_id=value["run_id"],
            environment=EnvironmentDisclosure.from_dict(value["environment"]),
            clock=ClockDisclosure.from_dict(value["clock"]),
            phases=tuple(PhaseObservation.from_dict(item, index) for index, item in enumerate(raw_phases)),
            distribution=DistributionObservation.from_dict(value["distribution"]),
            resources=ResourceObservation.from_dict(value["resources"]),
            outcome=Outcome.from_dict(value["outcome"]),
            evidence=tuple(RawEvidence.from_dict(item, index) for index, item in enumerate(raw_evidence)),
            schema_version=value["schema_version"],
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> MetricEvent:
        data = _load_strict_json(payload)
        event = cls.from_dict(data)
        if payload != event.to_json_bytes():
            raise ArtifactValidationError("metric event JSON must use the canonical UTF-8 encoding")
        return event


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError("duplicate JSON object key") from None
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ArtifactValidationError("non-finite JSON numbers are forbidden") from None


def _load_strict_json(payload: bytes) -> Any:
    if not isinstance(payload, bytes):
        raise ArtifactValidationError("metric event JSON must be bytes")
    if len(payload) > MAX_METRIC_JSON_BYTES:
        raise ArtifactValidationError("metric event JSON exceeds the maximum size")
    text: str | None = None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if text is None:
        raise ArtifactValidationError("metric event JSON must be UTF-8") from None
    parse_failure: str | None = None
    value: Any = None
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_json_constant)
    except ArtifactValidationError:
        raise
    except RecursionError:
        parse_failure = "depth"
    except (json.JSONDecodeError, TypeError, ValueError):
        parse_failure = "invalid"
    if parse_failure == "depth":
        raise ArtifactValidationError("metric event JSON exceeds the maximum nesting depth") from None
    if parse_failure == "invalid":
        raise ArtifactValidationError("metric event JSON is invalid") from None
    _require_max_depth(value)
    return value


def _require_max_depth(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_METRIC_JSON_DEPTH:
            raise ArtifactValidationError("metric event JSON exceeds the maximum nesting depth") from None
        if isinstance(item, dict):
            stack.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            stack.extend((nested, depth + 1) for nested in item)


BaselineObservation = MetricEvent
