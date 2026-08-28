"""Pure, inspection-safe sandbox policy request and preflight contracts.

This module describes policy intent only.  It deliberately performs no policy
enforcement and has no filesystem, network, subprocess, registry, guest, or VM
integration.  Enforcement belongs to later materialization/runtime layers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

SANDBOX_POLICY_SCHEMA_VERSION = 1
POLICY_PREFLIGHT_SCHEMA_VERSION = 1
MAX_POLICY_JSON_BYTES = 1024 * 1024

_SAFE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|"
    r"authorization|cookie|client[_-]?secret)\s*[:=]",
    re.IGNORECASE,
)
_USERINFO_RE = re.compile(r"[^:@/\\\s]+:[^@/\\\s]+@[^@/\\\s]+")
_SCHEMA_KEYS = frozenset(
    {
        "schema_version",
        "egress",
        "secret_delivery",
        "device_access",
        "audit",
        "snapshot_scrub",
        "binary_policy",
        "mode",
        "references",
        "provider",
        "reference_id",
        "devices",
        "device_class",
        "allocation_id",
        "enabled",
        "scrub_required",
        "hooks",
        "policy_id",
        "policy_digest",
        "egress_modes",
        "secret_delivery_modes",
        "secret_references",
        "device_access_modes",
        "device_requests",
        "audit_modes",
        "snapshots_enabled",
        "snapshot_scrub_required",
        "binary_policy_modes",
        "binary_policy_hooks",
        "issues",
        "supported",
        "code",
        "field",
    }
)
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "credential",
    "authorization",
    "cookie",
    "clientsecret",
)


class SandboxPolicyError(ValueError):
    """Base class for safe, typed sandbox-policy failures."""


class SandboxPolicyValidationError(SandboxPolicyError):
    """A request is malformed or not canonical policy data."""


class UnsupportedSandboxPolicyError(SandboxPolicyError):
    """A valid request cannot be enforced by the selected capabilities."""

    def __init__(self, issues: tuple[PolicyPreflightIssue, ...]) -> None:
        self.issues = issues
        codes = ",".join(issue.code.value for issue in issues)
        super().__init__(f"sandbox policy is unsupported: {codes}")


def canonical_policy_json_bytes(value: Any) -> bytes:
    """Encode JSON data as deterministic compact UTF-8 bytes."""
    _validate_inspection_safe(value)
    encoding_failed = False
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        encoding_failed = True
        payload = b""
    if encoding_failed:
        raise SandboxPolicyValidationError("policy contains non-JSON data") from None
    if len(payload) > MAX_POLICY_JSON_BYTES:
        raise SandboxPolicyValidationError("policy exceeds the canonical size limit")
    return payload


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _exact_string(value: Any, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SandboxPolicyValidationError(f"{field_name} must be a nonempty exact string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise SandboxPolicyValidationError(f"{field_name} cannot contain control characters")
    return value


def _safe_id(value: Any, field_name: str) -> str:
    identifier = _exact_string(value, field_name)
    _reject_sensitive_string(identifier)
    if _SAFE_ID_RE.fullmatch(identifier) is None:
        raise SandboxPolicyValidationError(f"{field_name} must be an inspection-safe opaque identifier")
    return identifier


def _sha256(value: Any, field_name: str) -> str:
    digest = _exact_string(value, field_name)
    if _SHA256_RE.fullmatch(digest) is None:
        raise SandboxPolicyValidationError(f"{field_name} must be a canonical sha256 digest")
    return digest


def _exact_fields(data: Any, expected: set[str], field_name: str) -> dict[str, Any]:
    if type(data) is not dict:
        raise SandboxPolicyValidationError(f"{field_name} must be an object")
    if any(type(key) is not str for key in data):
        raise SandboxPolicyValidationError(f"{field_name} field names must be exact strings")
    actual = set(data)
    if actual != expected:
        raise SandboxPolicyValidationError(f"{field_name} has missing or unknown fields")
    return data


def _tuple(value: Any, item_type: type[Any], field_name: str) -> tuple[Any, ...]:
    if type(value) is not tuple or any(type(item) is not item_type for item in value):
        raise SandboxPolicyValidationError(f"{field_name} must be an immutable tuple of {item_type.__name__}")
    return value


def _array(value: Any, field_name: str) -> list[Any]:
    if type(value) is not list:
        raise SandboxPolicyValidationError(f"{field_name} must be an array")
    return value


def _reject_ambiguous_identity(items: tuple[Any, ...], identity_field: str, field_name: str) -> None:
    seen: dict[str, Any] = {}
    for item in items:
        identity = getattr(item, identity_field)
        previous = seen.get(identity)
        if previous is not None and previous != item:
            raise SandboxPolicyValidationError(f"{field_name} has an ambiguous {identity_field}")
        seen[identity] = item


def _reject_sensitive_string(value: str) -> None:
    lowered = value.lower()
    if (
        "-----begin " in lowered
        or lowered.startswith(("bearer ", "basic "))
        or lowered.startswith(("file:", "env:"))
        or "/" in value
        or "\\" in value
        or _ASSIGNMENT_SECRET_RE.search(value) is not None
        or _JWT_RE.fullmatch(value) is not None
        or _USERINFO_RE.search(value) is not None
        or re.fullmatch(r"AKIA[0-9A-Z]{16}", value) is not None
    ):
        raise SandboxPolicyValidationError("policy contains secret-shaped or path-like material")


def _reject_sensitive_material(value: Any) -> None:
    """Reject likely credential/path payloads without reflecting them in errors."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise SandboxPolicyValidationError("policy object field names must be exact strings")
            compact_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if key not in _SCHEMA_KEYS and any(marker in compact_key for marker in _SENSITIVE_KEY_MARKERS):
                raise SandboxPolicyValidationError("policy contains a secret-shaped field")
            _reject_sensitive_material(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_material(item)
    elif type(value) is str:
        _reject_sensitive_string(value)


def _validate_inspection_safe(value: Any) -> None:
    recursion_failed = False
    try:
        _reject_sensitive_material(value)
    except RecursionError:
        recursion_failed = True
    if recursion_failed:
        raise SandboxPolicyValidationError("policy data exceeds the supported nesting depth") from None


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SandboxPolicyValidationError("policy JSON contains a duplicate object field")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise SandboxPolicyValidationError("policy JSON contains a non-finite number")


def _load_json(payload: bytes) -> Any:
    if type(payload) is not bytes:
        raise SandboxPolicyValidationError("policy JSON must be exact bytes")
    if len(payload) > MAX_POLICY_JSON_BYTES:
        raise SandboxPolicyValidationError("policy exceeds the canonical size limit")
    decode_failed = False
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        decode_failed = True
        text = ""
    if decode_failed:
        raise SandboxPolicyValidationError("policy JSON must be UTF-8") from None
    parse_failed = False
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except SandboxPolicyValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        parse_failed = True
        value = None
    if parse_failed:
        raise SandboxPolicyValidationError("policy JSON is invalid") from None
    _validate_inspection_safe(value)
    return value


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Requested outbound-connectivity posture; enforcement is external."""

    mode: str = "deny"

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in {"deny", "allow_all"}:
            raise SandboxPolicyValidationError("egress.mode must be 'deny' or 'allow_all'")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}

    @classmethod
    def from_dict(cls, data: Any) -> EgressPolicy:
        value = _exact_fields(data, {"mode"}, "egress")
        return cls(mode=value["mode"])


@dataclass(frozen=True, slots=True, order=True)
class SecretReference:
    """Opaque external reference only; never secret bytes or a delivery path."""

    provider: str
    reference_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _safe_id(self.provider, "secret reference provider"))
        object.__setattr__(self, "reference_id", _safe_id(self.reference_id, "secret reference id"))

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "reference_id": self.reference_id}

    @classmethod
    def from_dict(cls, data: Any, index: int) -> SecretReference:
        value = _exact_fields(data, {"provider", "reference_id"}, f"secret_delivery.references[{index}]")
        return cls(provider=value["provider"], reference_id=value["reference_id"])


@dataclass(frozen=True, slots=True)
class SecretDeliveryPolicy:
    mode: str = "none"
    references: tuple[SecretReference, ...] = ()

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in {"none", "references"}:
            raise SandboxPolicyValidationError("secret_delivery.mode must be 'none' or 'references'")
        references = _tuple(self.references, SecretReference, "secret_delivery.references")
        canonical = tuple(sorted(set(references)))
        if (self.mode == "none") != (not canonical):
            raise SandboxPolicyValidationError("secret_delivery mode and references contradict")
        object.__setattr__(self, "references", canonical)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "references": [reference.to_dict() for reference in self.references]}

    @classmethod
    def from_dict(cls, data: Any) -> SecretDeliveryPolicy:
        value = _exact_fields(data, {"mode", "references"}, "secret_delivery")
        raw = _array(value["references"], "secret_delivery.references")
        return cls(
            mode=value["mode"],
            references=tuple(SecretReference.from_dict(item, index) for index, item in enumerate(raw)),
        )


@dataclass(frozen=True, slots=True, order=True)
class DeviceRequest:
    """Opaque allocation identity, not a host device path."""

    device_class: str
    allocation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_class", _safe_id(self.device_class, "device class"))
        object.__setattr__(self, "allocation_id", _safe_id(self.allocation_id, "device allocation id"))

    def to_dict(self) -> dict[str, Any]:
        return {"allocation_id": self.allocation_id, "device_class": self.device_class}

    @classmethod
    def from_dict(cls, data: Any, index: int) -> DeviceRequest:
        value = _exact_fields(data, {"allocation_id", "device_class"}, f"device_access.devices[{index}]")
        return cls(device_class=value["device_class"], allocation_id=value["allocation_id"])


@dataclass(frozen=True, slots=True)
class DeviceAccessPolicy:
    mode: str = "deny"
    devices: tuple[DeviceRequest, ...] = ()

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in {"deny", "allowlist"}:
            raise SandboxPolicyValidationError("device_access.mode must be 'deny' or 'allowlist'")
        devices = _tuple(self.devices, DeviceRequest, "device_access.devices")
        _reject_ambiguous_identity(devices, "allocation_id", "device_access.devices")
        canonical = tuple(sorted(set(devices)))
        if (self.mode == "deny") != (not canonical):
            raise SandboxPolicyValidationError("device_access mode and devices contradict")
        object.__setattr__(self, "devices", canonical)

    def to_dict(self) -> dict[str, Any]:
        return {"devices": [device.to_dict() for device in self.devices], "mode": self.mode}

    @classmethod
    def from_dict(cls, data: Any) -> DeviceAccessPolicy:
        value = _exact_fields(data, {"devices", "mode"}, "device_access")
        raw = _array(value["devices"], "device_access.devices")
        return cls(
            mode=value["mode"],
            devices=tuple(DeviceRequest.from_dict(item, index) for index, item in enumerate(raw)),
        )


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    mode: str = "required"

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in {"required", "disabled"}:
            raise SandboxPolicyValidationError("audit.mode must be 'required' or 'disabled'")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}

    @classmethod
    def from_dict(cls, data: Any) -> AuditPolicy:
        value = _exact_fields(data, {"mode"}, "audit")
        return cls(mode=value["mode"])


@dataclass(frozen=True, slots=True)
class SnapshotScrubPolicy:
    """Snapshot export is closed; any future enablement must require scrub."""

    enabled: bool = False
    scrub_required: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.scrub_required) is not bool:
            raise SandboxPolicyValidationError("snapshot_scrub fields must be exact booleans")
        if self.enabled and not self.scrub_required:
            raise SandboxPolicyValidationError("enabled snapshots require scrubbing")

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "scrub_required": self.scrub_required}

    @classmethod
    def from_dict(cls, data: Any) -> SnapshotScrubPolicy:
        value = _exact_fields(data, {"enabled", "scrub_required"}, "snapshot_scrub")
        return cls(enabled=value["enabled"], scrub_required=value["scrub_required"])


@dataclass(frozen=True, slots=True, order=True)
class BinaryPolicyHook:
    """Content-addressed policy identity, never an executable or filesystem path."""

    policy_id: str
    policy_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _safe_id(self.policy_id, "binary policy id"))
        object.__setattr__(self, "policy_digest", _sha256(self.policy_digest, "binary policy digest"))

    def to_dict(self) -> dict[str, Any]:
        return {"policy_digest": self.policy_digest, "policy_id": self.policy_id}

    @classmethod
    def from_dict(cls, data: Any, index: int) -> BinaryPolicyHook:
        value = _exact_fields(data, {"policy_digest", "policy_id"}, f"binary_policy.hooks[{index}]")
        return cls(policy_id=value["policy_id"], policy_digest=value["policy_digest"])


@dataclass(frozen=True, slots=True)
class BinaryPolicy:
    """External hook selection, not workload-binary authorization.

    ``no_hooks`` requests no external evaluator and does not deny ordinary
    workload execution.  ``evaluate`` names every evaluator by ID and digest.
    """

    mode: str = "no_hooks"
    hooks: tuple[BinaryPolicyHook, ...] = ()

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in {"no_hooks", "evaluate"}:
            raise SandboxPolicyValidationError("binary_policy.mode must be 'no_hooks' or 'evaluate'")
        hooks = _tuple(self.hooks, BinaryPolicyHook, "binary_policy.hooks")
        _reject_ambiguous_identity(hooks, "policy_id", "binary_policy.hooks")
        canonical = tuple(sorted(set(hooks)))
        if (self.mode == "no_hooks") != (not canonical):
            raise SandboxPolicyValidationError("binary_policy mode and hooks contradict")
        object.__setattr__(self, "hooks", canonical)

    def to_dict(self) -> dict[str, Any]:
        return {"hooks": [hook.to_dict() for hook in self.hooks], "mode": self.mode}

    @classmethod
    def from_dict(cls, data: Any) -> BinaryPolicy:
        value = _exact_fields(data, {"hooks", "mode"}, "binary_policy")
        raw = _array(value["hooks"], "binary_policy.hooks")
        return cls(
            mode=value["mode"],
            hooks=tuple(BinaryPolicyHook.from_dict(item, index) for index, item in enumerate(raw)),
        )


@dataclass(frozen=True, slots=True)
class SandboxPolicyRequest:
    """Canonical policy intent, separate from capability and enforcement state."""

    egress: EgressPolicy = EgressPolicy()
    secret_delivery: SecretDeliveryPolicy = SecretDeliveryPolicy()
    device_access: DeviceAccessPolicy = DeviceAccessPolicy()
    audit: AuditPolicy = AuditPolicy()
    snapshot_scrub: SnapshotScrubPolicy = SnapshotScrubPolicy()
    binary_policy: BinaryPolicy = BinaryPolicy()
    schema_version: int = SANDBOX_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SANDBOX_POLICY_SCHEMA_VERSION:
            raise SandboxPolicyValidationError("unsupported sandbox policy schema_version")
        expected_types = (
            ("egress", self.egress, EgressPolicy),
            ("secret_delivery", self.secret_delivery, SecretDeliveryPolicy),
            ("device_access", self.device_access, DeviceAccessPolicy),
            ("audit", self.audit, AuditPolicy),
            ("snapshot_scrub", self.snapshot_scrub, SnapshotScrubPolicy),
            ("binary_policy", self.binary_policy, BinaryPolicy),
        )
        for field_name, value, expected_type in expected_types:
            if type(value) is not expected_type:
                raise SandboxPolicyValidationError(f"{field_name} must be {expected_type.__name__}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit": self.audit.to_dict(),
            "binary_policy": self.binary_policy.to_dict(),
            "device_access": self.device_access.to_dict(),
            "egress": self.egress.to_dict(),
            "schema_version": self.schema_version,
            "secret_delivery": self.secret_delivery.to_dict(),
            "snapshot_scrub": self.snapshot_scrub.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_policy_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return _digest(self.to_json_bytes())

    @classmethod
    def from_dict(cls, data: Any) -> SandboxPolicyRequest:
        _validate_inspection_safe(data)
        value = _exact_fields(
            data,
            {
                "audit",
                "binary_policy",
                "device_access",
                "egress",
                "schema_version",
                "secret_delivery",
                "snapshot_scrub",
            },
            "sandbox policy",
        )
        return cls(
            schema_version=value["schema_version"],
            egress=EgressPolicy.from_dict(value["egress"]),
            secret_delivery=SecretDeliveryPolicy.from_dict(value["secret_delivery"]),
            device_access=DeviceAccessPolicy.from_dict(value["device_access"]),
            audit=AuditPolicy.from_dict(value["audit"]),
            snapshot_scrub=SnapshotScrubPolicy.from_dict(value["snapshot_scrub"]),
            binary_policy=BinaryPolicy.from_dict(value["binary_policy"]),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> SandboxPolicyRequest:
        request = cls.from_dict(_load_json(payload))
        if payload != request.to_json_bytes():
            raise SandboxPolicyValidationError("policy JSON must use canonical UTF-8 encoding")
        return request


CLOSED_SANDBOX_POLICY = SandboxPolicyRequest()


@dataclass(frozen=True, slots=True)
class SandboxPolicyCapabilities:
    """Declarative enforcement capabilities supplied by a later runtime layer."""

    egress_modes: tuple[str, ...] = ("deny",)
    secret_delivery_modes: tuple[str, ...] = ("none",)
    secret_references: tuple[SecretReference, ...] = ()
    device_access_modes: tuple[str, ...] = ("deny",)
    device_requests: tuple[DeviceRequest, ...] = ()
    audit_modes: tuple[str, ...] = ("required",)
    snapshots_enabled: bool = False
    snapshot_scrub_required: bool = True
    binary_policy_modes: tuple[str, ...] = ("no_hooks",)
    binary_policy_hooks: tuple[BinaryPolicyHook, ...] = ()
    schema_version: int = SANDBOX_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SANDBOX_POLICY_SCHEMA_VERSION:
            raise SandboxPolicyValidationError("capability schema_version is unsupported")
        allowed_modes = {
            "egress_modes": {"allow_all", "deny"},
            "secret_delivery_modes": {"none", "references"},
            "device_access_modes": {"allowlist", "deny"},
            "audit_modes": {"disabled", "required"},
            "binary_policy_modes": {"evaluate", "no_hooks"},
        }
        for field_name, allowed in allowed_modes.items():
            raw = getattr(self, field_name)
            if type(raw) is not tuple or not raw or any(type(item) is not str or item not in allowed for item in raw):
                raise SandboxPolicyValidationError(f"{field_name} contains unsupported capability modes")
            object.__setattr__(self, field_name, tuple(sorted(set(raw))))
        secret_references = _tuple(self.secret_references, SecretReference, "secret_references")
        device_requests = _tuple(self.device_requests, DeviceRequest, "device_requests")
        binary_policy_hooks = _tuple(self.binary_policy_hooks, BinaryPolicyHook, "binary_policy_hooks")
        _reject_ambiguous_identity(device_requests, "allocation_id", "device_requests")
        _reject_ambiguous_identity(binary_policy_hooks, "policy_id", "binary_policy_hooks")
        object.__setattr__(self, "secret_references", tuple(sorted(set(secret_references))))
        object.__setattr__(self, "device_requests", tuple(sorted(set(device_requests))))
        object.__setattr__(self, "binary_policy_hooks", tuple(sorted(set(binary_policy_hooks))))
        if ("references" in self.secret_delivery_modes) != bool(self.secret_references):
            raise SandboxPolicyValidationError("secret capability mode and references contradict")
        if ("allowlist" in self.device_access_modes) != bool(self.device_requests):
            raise SandboxPolicyValidationError("device capability mode and requests contradict")
        if ("evaluate" in self.binary_policy_modes) != bool(self.binary_policy_hooks):
            raise SandboxPolicyValidationError("binary capability mode and hooks contradict")
        if type(self.snapshots_enabled) is not bool or type(self.snapshot_scrub_required) is not bool:
            raise SandboxPolicyValidationError("snapshot capabilities must be exact booleans")
        if not self.snapshot_scrub_required:
            raise SandboxPolicyValidationError("snapshot capabilities must require scrubbing")

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_modes": list(self.audit_modes),
            "binary_policy_hooks": [hook.to_dict() for hook in self.binary_policy_hooks],
            "binary_policy_modes": list(self.binary_policy_modes),
            "device_access_modes": list(self.device_access_modes),
            "device_requests": [device.to_dict() for device in self.device_requests],
            "egress_modes": list(self.egress_modes),
            "schema_version": self.schema_version,
            "secret_delivery_modes": list(self.secret_delivery_modes),
            "secret_references": [reference.to_dict() for reference in self.secret_references],
            "snapshot_scrub_required": self.snapshot_scrub_required,
            "snapshots_enabled": self.snapshots_enabled,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_policy_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return _digest(self.to_json_bytes())

    @classmethod
    def from_dict(cls, data: Any) -> SandboxPolicyCapabilities:
        _validate_inspection_safe(data)
        value = _exact_fields(
            data,
            {
                "audit_modes",
                "binary_policy_hooks",
                "binary_policy_modes",
                "device_access_modes",
                "device_requests",
                "egress_modes",
                "schema_version",
                "secret_delivery_modes",
                "secret_references",
                "snapshot_scrub_required",
                "snapshots_enabled",
            },
            "sandbox policy capabilities",
        )

        def modes(field_name: str) -> tuple[Any, ...]:
            return tuple(_array(value[field_name], f"capabilities.{field_name}"))

        raw_secrets = _array(value["secret_references"], "capabilities.secret_references")
        raw_devices = _array(value["device_requests"], "capabilities.device_requests")
        raw_hooks = _array(value["binary_policy_hooks"], "capabilities.binary_policy_hooks")
        return cls(
            schema_version=value["schema_version"],
            egress_modes=modes("egress_modes"),
            secret_delivery_modes=modes("secret_delivery_modes"),
            secret_references=tuple(SecretReference.from_dict(item, index) for index, item in enumerate(raw_secrets)),
            device_access_modes=modes("device_access_modes"),
            device_requests=tuple(DeviceRequest.from_dict(item, index) for index, item in enumerate(raw_devices)),
            audit_modes=modes("audit_modes"),
            snapshots_enabled=value["snapshots_enabled"],
            snapshot_scrub_required=value["snapshot_scrub_required"],
            binary_policy_modes=modes("binary_policy_modes"),
            binary_policy_hooks=tuple(BinaryPolicyHook.from_dict(item, index) for index, item in enumerate(raw_hooks)),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> SandboxPolicyCapabilities:
        capabilities = cls.from_dict(_load_json(payload))
        if payload != capabilities.to_json_bytes():
            raise SandboxPolicyValidationError("capability JSON must use canonical UTF-8 encoding")
        return capabilities


PHASE1_SANDBOX_CAPABILITIES = SandboxPolicyCapabilities()


class PolicyPreflightCode(StrEnum):
    UNSUPPORTED_EGRESS = "unsupported_egress"
    UNSUPPORTED_SECRET_DELIVERY = "unsupported_secret_delivery"
    UNSUPPORTED_DEVICE_ACCESS = "unsupported_device_access"
    UNSUPPORTED_AUDIT = "unsupported_audit"
    UNSUPPORTED_SNAPSHOT = "unsupported_snapshot"
    UNSAFE_SNAPSHOT_SCRUB = "unsafe_snapshot_scrub"
    UNSUPPORTED_BINARY_POLICY = "unsupported_binary_policy"


_PREFLIGHT_CODE_FIELDS = {
    PolicyPreflightCode.UNSUPPORTED_EGRESS: "egress",
    PolicyPreflightCode.UNSUPPORTED_SECRET_DELIVERY: "secret_delivery",
    PolicyPreflightCode.UNSUPPORTED_DEVICE_ACCESS: "device_access",
    PolicyPreflightCode.UNSUPPORTED_AUDIT: "audit",
    PolicyPreflightCode.UNSUPPORTED_SNAPSHOT: "snapshot_scrub",
    PolicyPreflightCode.UNSAFE_SNAPSHOT_SCRUB: "snapshot_scrub",
    PolicyPreflightCode.UNSUPPORTED_BINARY_POLICY: "binary_policy",
}


@dataclass(frozen=True, slots=True)
class PolicyPreflightIssue:
    code: PolicyPreflightCode
    field: str

    def __post_init__(self) -> None:
        if type(self.code) is not PolicyPreflightCode:
            raise SandboxPolicyValidationError("preflight issue code must be PolicyPreflightCode")
        _safe_id(self.field, "preflight issue field")
        if self.field != _PREFLIGHT_CODE_FIELDS[self.code]:
            raise SandboxPolicyValidationError("preflight issue code and field contradict")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "field": self.field}

    @classmethod
    def from_dict(cls, data: Any, index: int) -> PolicyPreflightIssue:
        value = _exact_fields(data, {"code", "field"}, f"preflight issues[{index}]")
        if type(value["code"]) is not str:
            raise SandboxPolicyValidationError("preflight issue code must be an exact string")
        code = next((candidate for candidate in PolicyPreflightCode if candidate.value == value["code"]), None)
        if code is None:
            raise SandboxPolicyValidationError("preflight issue code is unsupported") from None
        return cls(code=code, field=value["field"])


@dataclass(frozen=True, slots=True)
class PolicyPreflightResult:
    supported: bool
    issues: tuple[PolicyPreflightIssue, ...]
    schema_version: int = POLICY_PREFLIGHT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != POLICY_PREFLIGHT_SCHEMA_VERSION:
            raise SandboxPolicyValidationError("preflight result schema_version is unsupported")
        if type(self.supported) is not bool:
            raise SandboxPolicyValidationError("preflight result supported must be an exact boolean")
        raw_issues = _tuple(self.issues, PolicyPreflightIssue, "preflight issues")
        code_order = {code: index for index, code in enumerate(PolicyPreflightCode)}
        canonical_issues = tuple(sorted(set(raw_issues), key=lambda item: (code_order[item.code], item.field)))
        object.__setattr__(self, "issues", canonical_issues)
        if self.supported == bool(canonical_issues):
            raise SandboxPolicyValidationError("preflight result supported flag contradicts issues")

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "schema_version": self.schema_version,
            "supported": self.supported,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_policy_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return _digest(self.to_json_bytes())

    @classmethod
    def from_dict(cls, data: Any) -> PolicyPreflightResult:
        _validate_inspection_safe(data)
        value = _exact_fields(data, {"issues", "schema_version", "supported"}, "preflight result")
        raw_issues = _array(value["issues"], "preflight result issues")
        return cls(
            supported=value["supported"],
            issues=tuple(PolicyPreflightIssue.from_dict(item, index) for index, item in enumerate(raw_issues)),
            schema_version=value["schema_version"],
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> PolicyPreflightResult:
        result = cls.from_dict(_load_json(payload))
        if payload != result.to_json_bytes():
            raise SandboxPolicyValidationError("preflight result JSON must use canonical UTF-8 encoding")
        return result

    def raise_for_unsupported(self) -> None:
        if self.issues:
            raise UnsupportedSandboxPolicyError(self.issues)


def preflight_sandbox_policy(
    request: SandboxPolicyRequest,
    capabilities: SandboxPolicyCapabilities = PHASE1_SANDBOX_CAPABILITIES,
) -> PolicyPreflightResult:
    """Compare request intent with capabilities without performing side effects."""
    if type(request) is not SandboxPolicyRequest:
        raise SandboxPolicyValidationError("request must be SandboxPolicyRequest")
    if type(capabilities) is not SandboxPolicyCapabilities:
        raise SandboxPolicyValidationError("capabilities must be SandboxPolicyCapabilities")

    issues: list[PolicyPreflightIssue] = []

    def add(condition: bool, code: PolicyPreflightCode, field: str) -> None:
        if condition:
            issues.append(PolicyPreflightIssue(code=code, field=field))

    add(
        request.egress.mode not in capabilities.egress_modes,
        PolicyPreflightCode.UNSUPPORTED_EGRESS,
        "egress",
    )
    add(
        request.secret_delivery.mode not in capabilities.secret_delivery_modes
        or not set(request.secret_delivery.references).issubset(capabilities.secret_references),
        PolicyPreflightCode.UNSUPPORTED_SECRET_DELIVERY,
        "secret_delivery",
    )
    add(
        request.device_access.mode not in capabilities.device_access_modes
        or not set(request.device_access.devices).issubset(capabilities.device_requests),
        PolicyPreflightCode.UNSUPPORTED_DEVICE_ACCESS,
        "device_access",
    )
    add(
        request.audit.mode not in capabilities.audit_modes,
        PolicyPreflightCode.UNSUPPORTED_AUDIT,
        "audit",
    )
    add(
        request.snapshot_scrub.enabled and not capabilities.snapshots_enabled,
        PolicyPreflightCode.UNSUPPORTED_SNAPSHOT,
        "snapshot_scrub",
    )
    add(
        capabilities.snapshot_scrub_required and not request.snapshot_scrub.scrub_required,
        PolicyPreflightCode.UNSAFE_SNAPSHOT_SCRUB,
        "snapshot_scrub",
    )
    add(
        request.binary_policy.mode not in capabilities.binary_policy_modes
        or not set(request.binary_policy.hooks).issubset(capabilities.binary_policy_hooks),
        PolicyPreflightCode.UNSUPPORTED_BINARY_POLICY,
        "binary_policy",
    )
    return PolicyPreflightResult(supported=not issues, issues=tuple(issues))


__all__ = [
    "AuditPolicy",
    "BinaryPolicy",
    "BinaryPolicyHook",
    "CLOSED_SANDBOX_POLICY",
    "DeviceAccessPolicy",
    "DeviceRequest",
    "EgressPolicy",
    "MAX_POLICY_JSON_BYTES",
    "PHASE1_SANDBOX_CAPABILITIES",
    "POLICY_PREFLIGHT_SCHEMA_VERSION",
    "PolicyPreflightCode",
    "PolicyPreflightIssue",
    "PolicyPreflightResult",
    "SANDBOX_POLICY_SCHEMA_VERSION",
    "SandboxPolicyCapabilities",
    "SandboxPolicyError",
    "SandboxPolicyRequest",
    "SandboxPolicyValidationError",
    "SecretDeliveryPolicy",
    "SecretReference",
    "SnapshotScrubPolicy",
    "UnsupportedSandboxPolicyError",
    "canonical_policy_json_bytes",
    "preflight_sandbox_policy",
]
