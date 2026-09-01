"""Bounded canonical protocol for the exec-based OCI materializer worker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError
from .oci_provenance import ProvenanceSource
from .oci_store import (
    DerivedLayerOccurrence,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStoreError,
)

OCI_WORKER_REQUEST_SCHEMA = "palimpsest.oci-materialize-worker-request.v1"
OCI_WORKER_RESPONSE_SCHEMA = "palimpsest.oci-materialize-worker-response.v2"
MAX_OCI_WORKER_MESSAGE_BYTES = 256 * 1024
MAX_OCI_WORKER_JSON_DEPTH = 12
OCI_WORKER_ERROR_CATEGORIES = frozenset(
    {
        "authority",
        "intake",
        "internal",
        "invalid_request",
        "pack",
        "resource",
        "source",
        "store",
        "toolchain",
        "unsupported",
    }
)

_SOURCE_CAS_ID_RE = re.compile(r"^source-cas-v1:[0-9a-f]{64}$")
_OCI_STORE_ID_RE = re.compile(r"^oci-store-v1:[0-9a-f]{64}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class OCIWorkerProtocolError(ArtifactValidationError):
    """Stable path-free protocol validation failure."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        raise OCIWorkerProtocolError("OCI worker message is not canonical JSON data") from None


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _exact_fields(data: Any, expected: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(data, dict) or any(not isinstance(name, str) for name in data):
        raise OCIWorkerProtocolError(f"{field_name} must be an object")
    if set(data) != expected:
        raise OCIWorkerProtocolError(f"{field_name} fields are invalid")
    return data


def _absolute_path(value: Any, field_name: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", errors="ignore")) > 4096
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or not value.startswith("/")
        or any(component in {"", ".", ".."} for component in value.split("/")[1:])
    ):
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or os.fspath(path) != value:
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical absolute path")
    return path


def _canonical_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical UUID") from None
    if str(parsed) != value:
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical UUID")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OCIWorkerProtocolError(f"{field_name} is invalid")
    return value


def _path_input(value: Any, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical absolute path")
    try:
        rendered = os.fspath(value)
    except TypeError:
        raise OCIWorkerProtocolError(f"{field_name} must be a canonical absolute path") from None
    return _absolute_path(rendered, field_name)


@dataclass(frozen=True, slots=True)
class OCIWorkerRequest:
    nonce: str
    config_root: Path
    state_root: Path
    expected_store_id: str
    source_cas_root: Path
    expected_source_cas_id: str
    source: ProvenanceSource
    occurrence: DerivedLayerOccurrence
    key: DerivedSquashFSKey
    key_digest: str
    packer_path: Path
    packer_sha256: str
    cpu_limit_seconds: int
    schema: str = OCI_WORKER_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "nonce", _canonical_uuid(self.nonce, "request.nonce"))
        object.__setattr__(self, "config_root", _path_input(self.config_root, "request.config_root"))
        object.__setattr__(self, "state_root", _path_input(self.state_root, "request.state_root"))
        object.__setattr__(
            self,
            "source_cas_root",
            _path_input(self.source_cas_root, "request.source_cas_root"),
        )
        object.__setattr__(self, "packer_path", _path_input(self.packer_path, "request.packer_path"))
        object.__setattr__(
            self,
            "expected_store_id",
            _identifier(self.expected_store_id, _OCI_STORE_ID_RE, "request.expected_store_id"),
        )
        object.__setattr__(
            self,
            "expected_source_cas_id",
            _identifier(self.expected_source_cas_id, _SOURCE_CAS_ID_RE, "request.expected_source_cas_id"),
        )
        if not isinstance(self.source, ProvenanceSource):
            raise OCIWorkerProtocolError("request.source is invalid")
        if not isinstance(self.occurrence, DerivedLayerOccurrence):
            raise OCIWorkerProtocolError("request.occurrence is invalid")
        if not isinstance(self.key, DerivedSquashFSKey) or not self.key.matches(self.occurrence):
            raise OCIWorkerProtocolError("request.key is invalid")
        if not isinstance(self.key_digest, str) or self.key.digest != self.key_digest:
            raise OCIWorkerProtocolError("request.key_digest does not match the key")
        _identifier(self.key_digest.removeprefix("sha256:"), _SHA256_HEX_RE, "request.key_digest")
        object.__setattr__(
            self,
            "packer_sha256",
            _identifier(self.packer_sha256, _SHA256_HEX_RE, "request.packer_sha256"),
        )
        if f"sha256:{self.packer_sha256}" != self.key.packer_executable_digest:
            raise OCIWorkerProtocolError("request packer digest does not match the key")
        if type(self.cpu_limit_seconds) is not int or not 1 <= self.cpu_limit_seconds <= 3600:
            raise OCIWorkerProtocolError("request.cpu_limit_seconds is invalid")
        if self.schema != OCI_WORKER_REQUEST_SCHEMA:
            raise OCIWorkerProtocolError("OCI worker request schema is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_limit_seconds": self.cpu_limit_seconds,
            "expected_source_cas_id": self.expected_source_cas_id,
            "expected_store_id": self.expected_store_id,
            "key": {"digest": self.key_digest, "value": self.key.to_dict()},
            "nonce": self.nonce,
            "occurrence": self.occurrence.to_dict(),
            "packer_path": os.fspath(self.packer_path),
            "packer_sha256": self.packer_sha256,
            "roots": {"config": os.fspath(self.config_root), "state": os.fspath(self.state_root)},
            "schema": self.schema,
            "source": self.source.to_dict(),
            "source_cas": {"cas_id": self.expected_source_cas_id, "root": os.fspath(self.source_cas_root)},
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return _digest(self.to_json_bytes())

    @classmethod
    def from_dict(cls, data: Any) -> OCIWorkerRequest:
        fields = {
            "cpu_limit_seconds",
            "expected_source_cas_id",
            "expected_store_id",
            "key",
            "nonce",
            "occurrence",
            "packer_path",
            "packer_sha256",
            "roots",
            "schema",
            "source",
            "source_cas",
        }
        value = _exact_fields(data, fields, "OCI worker request")
        roots = _exact_fields(value["roots"], {"config", "state"}, "request.roots")
        source_cas = _exact_fields(value["source_cas"], {"cas_id", "root"}, "request.source_cas")
        key_value = _exact_fields(value["key"], {"digest", "value"}, "request.key")
        try:
            source = ProvenanceSource.from_dict(value["source"])
            occurrence = DerivedLayerOccurrence.from_dict(value["occurrence"])
            key = DerivedSquashFSKey.from_dict(key_value["value"])
        except (ArtifactValidationError, OCIStoreError, TypeError, ValueError):
            raise OCIWorkerProtocolError("OCI worker request content is invalid") from None
        return cls(
            nonce=value["nonce"],
            config_root=_absolute_path(roots["config"], "request.roots.config"),
            state_root=_absolute_path(roots["state"], "request.roots.state"),
            expected_store_id=value["expected_store_id"],
            source_cas_root=_absolute_path(source_cas["root"], "request.source_cas.root"),
            expected_source_cas_id=source_cas["cas_id"],
            source=source,
            occurrence=occurrence,
            key=key,
            key_digest=key_value["digest"],
            packer_path=_absolute_path(value["packer_path"], "request.packer_path"),
            packer_sha256=value["packer_sha256"],
            cpu_limit_seconds=value["cpu_limit_seconds"],
            schema=value["schema"],
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> OCIWorkerRequest:
        value = _load_strict_json(payload)
        request = cls.from_dict(value)
        if request.to_json_bytes() != payload:
            raise OCIWorkerProtocolError("OCI worker request JSON must be canonical UTF-8")
        return request


@dataclass(frozen=True, slots=True)
class OCIWorkerResponse:
    nonce: str
    request_digest: str
    status: str
    result: MaterializationResult | None
    error_category: str | None
    schema: str = OCI_WORKER_RESPONSE_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "nonce", _canonical_uuid(self.nonce, "response.nonce"))
        if (
            not isinstance(self.request_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_digest) is None
        ):
            raise OCIWorkerProtocolError("response.request_digest is invalid")
        if self.schema != OCI_WORKER_RESPONSE_SCHEMA:
            raise OCIWorkerProtocolError("OCI worker response schema is unsupported")
        if self.status == "succeeded":
            if not isinstance(self.result, MaterializationResult) or self.error_category is not None:
                raise OCIWorkerProtocolError("successful OCI worker response is invalid")
        elif self.status == "failed":
            if self.result is not None or not isinstance(self.error_category, str):
                raise OCIWorkerProtocolError("failed OCI worker response is invalid")
            if self.error_category not in OCI_WORKER_ERROR_CATEGORIES:
                raise OCIWorkerProtocolError("OCI worker error category is invalid")
        else:
            raise OCIWorkerProtocolError("OCI worker response status is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_category": self.error_category,
            "nonce": self.nonce,
            "request_digest": self.request_digest,
            "result": self.result.to_dict() if self.result is not None else None,
            "schema": self.schema,
            "status": self.status,
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> OCIWorkerResponse:
        value = _load_strict_json(payload)
        fields = {"error_category", "nonce", "request_digest", "result", "schema", "status"}
        data = _exact_fields(value, fields, "OCI worker response")
        try:
            result = None if data["result"] is None else MaterializationResult.from_dict(data["result"])
        except (OCIStoreError, TypeError, ValueError):
            raise OCIWorkerProtocolError("OCI worker response result is invalid") from None
        response = cls(
            nonce=data["nonce"],
            request_digest=data["request_digest"],
            status=data["status"],
            result=result,
            error_category=data["error_category"],
            schema=data["schema"],
        )
        if response.to_json_bytes() != payload:
            raise OCIWorkerProtocolError("OCI worker response JSON must be canonical UTF-8")
        return response


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OCIWorkerProtocolError("OCI worker message contains a duplicate object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise OCIWorkerProtocolError("OCI worker message contains a non-finite number")


def _load_strict_json(payload: bytes) -> Any:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_OCI_WORKER_MESSAGE_BYTES:
        raise OCIWorkerProtocolError("OCI worker message size is invalid")
    invalid = False
    value: Any = None
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except OCIWorkerProtocolError:
        raise
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        invalid = True
    if invalid:
        raise OCIWorkerProtocolError("OCI worker message JSON is invalid") from None
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_OCI_WORKER_JSON_DEPTH:
            raise OCIWorkerProtocolError("OCI worker message exceeds the maximum depth")
        if isinstance(item, dict):
            stack.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            stack.extend((nested, depth + 1) for nested in item)
    return value


__all__ = [
    "MAX_OCI_WORKER_MESSAGE_BYTES",
    "OCI_WORKER_ERROR_CATEGORIES",
    "OCI_WORKER_REQUEST_SCHEMA",
    "OCI_WORKER_RESPONSE_SCHEMA",
    "OCIWorkerProtocolError",
    "OCIWorkerRequest",
    "OCIWorkerResponse",
]
