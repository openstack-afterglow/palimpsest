"""Bounded canonical protocol for the exec-based OCI materializer worker."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStoreError
from .errors import ArtifactValidationError
from .oci_converter import LayerIntakeError
from .oci_packer import SquashFSPackError
from .oci_provenance import ProvenanceSource
from .oci_store import (
    DerivedLayerOccurrence,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStoreError,
)

OCI_WORKER_REQUEST_SCHEMA = "palimpsest.oci-materialize-worker-request.v1"
OCI_WORKER_RESPONSE_SCHEMA = "palimpsest.oci-materialize-worker-response.v4"
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
OCI_WORKER_FAILURE_STAGES = frozenset(
    {
        "limits",
        "packing",
        "packer-dependency-inspection",
        "packer-final-spawn",
        "packer-pinned-version",
        "packer-toolchain-version",
        "source",
        "staging",
        "store",
    }
)
OCI_WORKER_FAILURE_ERRNOS = frozenset({errno.EAGAIN, errno.ENOMEM})

# Fixed project identifiers only: never serialize exception text or arbitrary codes.
OCI_WORKER_EXCEPTION_CODES = {
    ArtifactStoreError: frozenset({"artifact-write", "artifact-publish"}),
    OCIStoreError: frozenset(
        "oci-store-authority oci-store-cleanup oci-store-clock oci-store-collision "
        "oci-store-corrupt oci-store-handoff oci-store-in-use oci-store-input oci-store-key "
        "oci-store-lease oci-store-lease-abort oci-store-lease-closed oci-store-lease-copy "
        "oci-store-lease-incomplete oci-store-lease-owner oci-store-lease-read oci-store-lease-set "
        "oci-store-lock oci-store-missing oci-store-occurrence oci-store-owner oci-store-policy "
        "oci-store-producer oci-store-receipt oci-store-record oci-store-repair "
        "oci-store-repair-deferred oci-store-result oci-store-retained oci-store-retention "
        "oci-store-root oci-store-wire oci-store-write".split()
    ),
    SquashFSPackError: frozenset(
        "oci-pack-cleanup oci-pack-execution oci-pack-input oci-pack-output-changed "
        "oci-pack-output-io oci-pack-output-open oci-pack-output-seal oci-pack-output-short "
        "oci-pack-output-size oci-pack-output-type oci-pack-policy oci-pack-private oci-pack-root "
        "oci-pack-spool oci-pack-superblock oci-packed-chunk oci-packed-cleanup oci-packed-closed "
        "oci-packed-consumed oci-packed-copy oci-packed-digest oci-packed-incomplete oci-packed-io "
        "oci-packed-owner oci-packer-changed oci-packer-copy oci-packer-digest oci-packer-exit "
        "oci-packer-open oci-packer-path oci-packer-resource oci-packer-short oci-packer-spawn "
        "oci-packer-timeout oci-packer-toolchain oci-packer-type oci-packer-version".split()
    ),
    LayerIntakeError: frozenset(
        "oci-changeset-member oci-changeset-payload oci-changeset-input oci-whiteout-marker "
        "oci-whiteout-target oci-hardlink-self oci-hardlink-chain oci-hardlink-target "
        "oci-opaque-state oci-hardlink-group oci-hardlink-order "
        "oci-expansion-limit oci-uncompressed-limit "
        "oci-codec oci-compressed-limit oci-device-metadata oci-diffid oci-gzip oci-gzip-trailing "
        "oci-header-limit oci-invalid-link oci-invalid-path oci-layer-timeout oci-limit-policy "
        "oci-member-limit oci-member-mode oci-member-type oci-metadata-limit oci-numeric-metadata "
        "oci-path-limit oci-pax-metadata oci-payload-chunk oci-payload-closed oci-payload-consumed "
        "oci-payload-copy oci-payload-digest oci-payload-incomplete oci-payload-io oci-payload-owner "
        "oci-payload-read oci-payload-ref oci-payload-revoked oci-payload-short-read "
        "oci-regular-limit oci-reserved-path oci-root-metadata oci-source-lease oci-sparse "
        "oci-spool-io oci-spool-safety oci-stage-chunk oci-stage-closed oci-stage-consumed "
        "oci-stage-copy oci-stage-io oci-stage-owner oci-tar-checksum oci-tar-framing oci-tar-io "
        "oci-tar-metadata oci-tar-trailer oci-tar-utf8 oci-whiteout oci-xattr-limit oci-xattr-policy".split()
    ),
}
OCI_WORKER_CATEGORY_CODES = {
    "intake": OCI_WORKER_EXCEPTION_CODES[LayerIntakeError],
    "pack": OCI_WORKER_EXCEPTION_CODES[SquashFSPackError] - {"oci-packer-resource", "oci-packer-toolchain"},
    "toolchain": frozenset({"oci-packer-toolchain"}),
    "resource": frozenset({"oci-packer-resource"}),
    "store": (OCI_WORKER_EXCEPTION_CODES[OCIStoreError] - {"oci-store-authority", "oci-store-root"})
    | OCI_WORKER_EXCEPTION_CODES[ArtifactStoreError],
    "authority": frozenset({"oci-store-authority", "oci-store-root"}),
}
OCI_WORKER_IO_ERRNOS = frozenset(
    {
        errno.ENOSPC,
        errno.EDQUOT,
        errno.EIO,
        errno.EROFS,
        errno.EACCES,
        errno.EPERM,
        errno.EMFILE,
        errno.ENFILE,
    }
)
OCI_WORKER_IO_CODES = frozenset(
    "artifact-write artifact-publish "
    "oci-spool-io oci-stage-io oci-payload-io oci-payload-read oci-tar-io "
    "oci-packed-io oci-pack-output-io oci-pack-output-open oci-pack-spool "
    "oci-packer-spawn oci-packer-open oci-store-write oci-store-root "
    "oci-store-record oci-store-lock oci-store-corrupt oci-store-retention".split()
)
OCI_WORKER_OPERATION_STAGES = frozenset({"source", "staging", "packing", "store", "limits"})

_SOURCE_CAS_ID_RE = re.compile(r"^source-cas-v1:[0-9a-f]{64}$")
_OCI_STORE_ID_RE = re.compile(r"^oci-store-v1:[0-9a-f]{64}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class OCIWorkerProtocolError(ArtifactValidationError):
    """Stable path-free protocol validation failure."""


def validate_worker_failure_code(category: str | None, code: str | None) -> None:
    if code is not None and (
        type(code) is not str or type(category) is not str or code not in OCI_WORKER_CATEGORY_CODES.get(category, ())
    ):
        raise OCIWorkerProtocolError("OCI worker failure code is invalid")


def validate_worker_io_errno(code: str | None, number: int | None) -> None:
    if number is not None and (
        type(number) is not int
        or number not in OCI_WORKER_IO_ERRNOS
        or type(code) is not str
        or code not in OCI_WORKER_IO_CODES
    ):
        raise OCIWorkerProtocolError("OCI worker I/O errno is invalid")


def validate_worker_operation_stage(stage: str | None) -> None:
    if stage is not None and (type(stage) is not str or stage not in OCI_WORKER_OPERATION_STAGES):
        raise OCIWorkerProtocolError("OCI worker operation stage is invalid")


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
    failure_stage: str | None = None
    failure_errno: int | None = None
    failure_code: str | None = None
    io_errno: int | None = None
    operation_stage: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "nonce", _canonical_uuid(self.nonce, "response.nonce"))
        if (
            not isinstance(self.request_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_digest) is None
        ):
            raise OCIWorkerProtocolError("response.request_digest is invalid")
        if type(self.schema) is not str or self.schema != OCI_WORKER_RESPONSE_SCHEMA:
            raise OCIWorkerProtocolError("OCI worker response schema is unsupported")
        if self.failure_stage is not None and (
            not isinstance(self.failure_stage, str) or self.failure_stage not in OCI_WORKER_FAILURE_STAGES
        ):
            raise OCIWorkerProtocolError("OCI worker failure stage is invalid")
        if self.failure_errno is not None and (
            type(self.failure_errno) is not int or self.failure_errno not in OCI_WORKER_FAILURE_ERRNOS
        ):
            raise OCIWorkerProtocolError("OCI worker failure errno is invalid")
        if self.failure_errno is not None and self.failure_stage is None:
            raise OCIWorkerProtocolError("OCI worker failure errno requires a known stage")
        if self.status == "succeeded":
            if (
                not isinstance(self.result, MaterializationResult)
                or self.error_category is not None
                or self.failure_stage is not None
                or self.failure_errno is not None
                or self.failure_code is not None
                or self.io_errno is not None
                or self.operation_stage is not None
            ):
                raise OCIWorkerProtocolError("successful OCI worker response is invalid")
        elif self.status == "failed":
            if self.result is not None or not isinstance(self.error_category, str):
                raise OCIWorkerProtocolError("failed OCI worker response is invalid")
            if self.error_category not in OCI_WORKER_ERROR_CATEGORIES:
                raise OCIWorkerProtocolError("OCI worker error category is invalid")
            if self.error_category != "resource" and (self.failure_stage is not None or self.failure_errno is not None):
                raise OCIWorkerProtocolError("non-resource OCI worker response cannot carry failure details")
            validate_worker_failure_code(self.error_category, self.failure_code)
            validate_worker_io_errno(self.failure_code, self.io_errno)
            validate_worker_operation_stage(self.operation_stage)
        else:
            raise OCIWorkerProtocolError("OCI worker response status is invalid")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "error_category": self.error_category,
            "failure_code": self.failure_code,
            "failure_errno": self.failure_errno,
            "failure_stage": self.failure_stage,
            "io_errno": self.io_errno,
            "operation_stage": self.operation_stage,
            "nonce": self.nonce,
            "request_digest": self.request_digest,
            "result": self.result.to_dict() if self.result is not None else None,
            "schema": self.schema,
            "status": self.status,
        }
        return value

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> OCIWorkerResponse:
        value = _load_strict_json(payload)
        if not isinstance(value, dict):
            raise OCIWorkerProtocolError("OCI worker response must be an object")
        if value.get("schema") != OCI_WORKER_RESPONSE_SCHEMA:
            raise OCIWorkerProtocolError("OCI worker response schema is unsupported")
        fields = {
            "error_category",
            "failure_code",
            "failure_errno",
            "failure_stage",
            "io_errno",
            "operation_stage",
            "nonce",
            "request_digest",
            "result",
            "schema",
            "status",
        }
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
            failure_stage=data["failure_stage"],
            failure_errno=data["failure_errno"],
            failure_code=data["failure_code"],
            io_errno=data["io_errno"],
            operation_stage=data["operation_stage"],
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
    "OCI_WORKER_FAILURE_ERRNOS",
    "OCI_WORKER_FAILURE_STAGES",
    "OCI_WORKER_EXCEPTION_CODES",
    "OCI_WORKER_CATEGORY_CODES",
    "OCI_WORKER_IO_CODES",
    "OCI_WORKER_IO_ERRNOS",
    "OCI_WORKER_OPERATION_STAGES",
    "validate_worker_failure_code",
    "validate_worker_io_errno",
    "validate_worker_operation_stage",
    "OCI_WORKER_REQUEST_SCHEMA",
    "OCI_WORKER_RESPONSE_SCHEMA",
    "OCIWorkerProtocolError",
    "OCIWorkerRequest",
    "OCIWorkerResponse",
]
