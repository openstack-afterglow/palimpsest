"""Deterministic run-owned transport for the OCI guest stage-1 plan.

The transport is a fixed-header, 4 KiB-aligned raw block artifact.  It is
attached read-only to a future OCI-root guest, but the current bootstrap
``/init`` deliberately does not consume it and runtime launch remains disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .digest import normalize_digest
from .errors import ArtifactValidationError, StateError
from .oci_provenance import canonical_json_bytes
from .oci_stage1 import OCIStage1Plan

OCI_STAGE1_TRANSPORT_SCHEMA = "palimpsest.oci-stage1-transport.v1"
OCI_STAGE1_TRANSPORT_FORMAT = "raw-envelope-4k.v1"
OCI_STAGE1_TRANSPORT_DEVICE_POLICY = "virtio-blk-readonly.v1"
OCI_STAGE1_TRANSPORT_FILENAME = "stage1-plan.raw"
MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_OCI_STAGE1_TRANSPORT_BYTES = MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES + 4096

_MAGIC = b"PALIMPSEST-S1\0\0\0"
_VERSION = 1
_ALIGNMENT = 4096
_HEADER = struct.Struct("<16sIIQ32s")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} is invalid")
    try:
        normalized = normalize_digest(value)
    except (ArtifactValidationError, TypeError, ValueError):
        raise ArtifactValidationError(f"{field_name} is invalid") from None
    if normalized != value:
        raise ArtifactValidationError(f"{field_name} is not canonical")
    return normalized


def _aligned(size: int) -> int:
    return (size + _ALIGNMENT - 1) // _ALIGNMENT * _ALIGNMENT


@dataclass(frozen=True, slots=True)
class OCIStage1TransportReceipt:
    artifact_digest: str
    artifact_size_bytes: int
    payload_digest: str
    payload_size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_digest",
            _canonical_digest(self.artifact_digest, "stage-1 transport artifact digest"),
        )
        object.__setattr__(
            self,
            "payload_digest",
            _canonical_digest(self.payload_digest, "stage-1 transport payload digest"),
        )
        if (
            type(self.payload_size_bytes) is not int
            or not 1 <= self.payload_size_bytes <= MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES
            or type(self.artifact_size_bytes) is not int
            or self.artifact_size_bytes != _aligned(_HEADER.size + self.payload_size_bytes)
            or self.artifact_size_bytes > MAX_OCI_STAGE1_TRANSPORT_BYTES
        ):
            raise ArtifactValidationError("stage-1 transport size is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_digest": self.artifact_digest,
            "artifact_size_bytes": self.artifact_size_bytes,
            "device_policy": OCI_STAGE1_TRANSPORT_DEVICE_POLICY,
            "format": OCI_STAGE1_TRANSPORT_FORMAT,
            "payload_digest": self.payload_digest,
            "payload_size_bytes": self.payload_size_bytes,
            "schema": OCI_STAGE1_TRANSPORT_SCHEMA,
        }

    @classmethod
    def from_dict(cls, value: Any) -> OCIStage1TransportReceipt:
        if not isinstance(value, Mapping) or set(value) != {
            "artifact_digest",
            "artifact_size_bytes",
            "device_policy",
            "format",
            "payload_digest",
            "payload_size_bytes",
            "schema",
        }:
            raise ArtifactValidationError("stage-1 transport receipt fields are invalid")
        if (
            value.get("schema") != OCI_STAGE1_TRANSPORT_SCHEMA
            or value.get("format") != OCI_STAGE1_TRANSPORT_FORMAT
            or value.get("device_policy") != OCI_STAGE1_TRANSPORT_DEVICE_POLICY
        ):
            raise ArtifactValidationError("stage-1 transport receipt policy is invalid")
        receipt = cls(
            value["artifact_digest"],
            value["artifact_size_bytes"],
            value["payload_digest"],
            value["payload_size_bytes"],
        )
        if receipt.to_dict() != dict(value):
            raise ArtifactValidationError("stage-1 transport receipt is not canonical")
        return receipt


@dataclass(frozen=True, slots=True)
class BuiltOCIStage1Transport:
    artifact: bytes
    receipt: OCIStage1TransportReceipt
    stage1_plan: OCIStage1Plan

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact, bytes)
            or not isinstance(self.receipt, OCIStage1TransportReceipt)
            or not isinstance(self.stage1_plan, OCIStage1Plan)
        ):
            raise ArtifactValidationError("built stage-1 transport is invalid")
        expected = _build_artifact_bytes(canonical_json_bytes(self.stage1_plan.to_dict()))
        if self.artifact != expected or self.receipt != _receipt(self.artifact, self.stage1_plan):
            raise ArtifactValidationError("built stage-1 transport binding is invalid")


def _build_artifact_bytes(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES:
        raise ArtifactValidationError("stage-1 transport payload bytes are invalid")
    payload_hash = hashlib.sha256(payload).digest()
    header = _HEADER.pack(_MAGIC, _VERSION, _HEADER.size, len(payload), payload_hash)
    size = _aligned(len(header) + len(payload))
    artifact = header + payload + b"\0" * (size - len(header) - len(payload))
    if len(artifact) > MAX_OCI_STAGE1_TRANSPORT_BYTES:
        raise ArtifactValidationError("stage-1 transport artifact is too large")
    return artifact


def _receipt(artifact: bytes, plan: OCIStage1Plan) -> OCIStage1TransportReceipt:
    payload = canonical_json_bytes(plan.to_dict())
    return OCIStage1TransportReceipt(_digest(artifact), len(artifact), _digest(payload), len(payload))


def build_stage1_transport(plan: OCIStage1Plan) -> BuiltOCIStage1Transport:
    """Build one deterministic raw transport from a cycle-free stage-1 plan."""

    if not isinstance(plan, OCIStage1Plan):
        raise ArtifactValidationError("stage-1 transport plan is invalid")
    artifact = _build_artifact_bytes(canonical_json_bytes(plan.to_dict()))
    return BuiltOCIStage1Transport(artifact, _receipt(artifact, plan), plan)


def verify_stage1_transport(
    artifact: bytes,
    receipt: OCIStage1TransportReceipt,
    *,
    expected_stage1_plan: OCIStage1Plan,
) -> OCIStage1Plan:
    """Verify framing, canonical JSON, receipt, and exact domain projection."""

    if (
        not isinstance(artifact, bytes)
        or not _HEADER.size < len(artifact) <= MAX_OCI_STAGE1_TRANSPORT_BYTES
        or len(artifact) % _ALIGNMENT != 0
        or not isinstance(receipt, OCIStage1TransportReceipt)
        or not isinstance(expected_stage1_plan, OCIStage1Plan)
    ):
        raise ArtifactValidationError("stage-1 transport artifact bytes are invalid")
    if _digest(artifact) != receipt.artifact_digest or len(artifact) != receipt.artifact_size_bytes:
        raise ArtifactValidationError("stage-1 transport artifact does not match its receipt")
    try:
        magic, version, header_size, payload_size, payload_hash = _HEADER.unpack_from(artifact)
    except struct.error:
        raise ArtifactValidationError("stage-1 transport header is invalid") from None
    if (
        magic != _MAGIC
        or version != _VERSION
        or header_size != _HEADER.size
        or not 1 <= payload_size <= MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES
        or _aligned(header_size + payload_size) != len(artifact)
    ):
        raise ArtifactValidationError("stage-1 transport header policy is invalid")
    payload = artifact[header_size : header_size + payload_size]
    padding = artifact[header_size + payload_size :]
    if (
        hashlib.sha256(payload).digest() != payload_hash
        or _digest(payload) != receipt.payload_digest
        or len(payload) != receipt.payload_size_bytes
        or padding != b"\0" * len(padding)
    ):
        raise ArtifactValidationError("stage-1 transport payload binding is invalid")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, RecursionError, ValueError):
        raise ArtifactValidationError("stage-1 transport payload JSON is invalid") from None
    try:
        canonical = canonical_json_bytes(value)
    except ArtifactValidationError:
        raise ArtifactValidationError("stage-1 transport payload JSON is invalid") from None
    if canonical != payload:
        raise ArtifactValidationError("stage-1 transport payload JSON is not canonical")
    if value != expected_stage1_plan.to_dict():
        raise ArtifactValidationError("stage-1 transport plan binding is invalid") from None
    if expected_stage1_plan.digest != receipt.payload_digest:
        raise ArtifactValidationError("stage-1 transport plan digest is invalid")
    return expected_stage1_plan


@dataclass(frozen=True, slots=True)
class VerifiedOCIStage1Transport:
    path: Path
    receipt: OCIStage1TransportReceipt
    plan: OCIStage1Plan
    device: int
    inode: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or not isinstance(self.receipt, OCIStage1TransportReceipt)
            or not isinstance(self.plan, OCIStage1Plan)
            or type(self.device) is not int
            or type(self.inode) is not int
            or self.device < 0
            or self.inode <= 0
        ):
            raise StateError("verified stage-1 transport identity is invalid")


def verify_stage1_transport_file(
    path: Path,
    receipt: OCIStage1TransportReceipt,
    *,
    expected_stage1_plan: OCIStage1Plan,
) -> VerifiedOCIStage1Transport:
    """Pin a run-owned raw transport and verify it through one descriptor."""

    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or "\0" in os.fspath(path)
        or not isinstance(receipt, OCIStage1TransportReceipt)
        or not isinstance(expected_stage1_plan, OCIStage1Plan)
    ):
        raise StateError("stage-1 transport verification inputs are invalid")
    descriptor: int | None = None
    try:
        visible = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(visible.st_mode):
            raise StateError("stage-1 transport cannot be securely read")
        if not stat.S_ISREG(visible.st_mode):
            raise StateError("stage-1 transport metadata is unsafe")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_size != receipt.artifact_size_bytes
        ):
            raise StateError("stage-1 transport metadata is unsafe")
        artifact = bytearray()
        offset = 0
        while offset < opened.st_size:
            chunk = os.pread(descriptor, min(1024 * 1024, opened.st_size - offset), offset)
            if not chunk:
                raise StateError("stage-1 transport ended during verification")
            artifact.extend(chunk)
            offset += len(chunk)
        try:
            plan = verify_stage1_transport(
                bytes(artifact),
                receipt,
                expected_stage1_plan=expected_stage1_plan,
            )
        except ArtifactValidationError:
            raise StateError("stage-1 transport structure or provenance is invalid") from None
        after = os.fstat(descriptor)
        final = path.stat(follow_symlinks=False)

        def stable(item: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if stable(after) != stable(opened) or stable(final) != stable(opened):
            raise StateError("stage-1 transport changed during verification")
        return VerifiedOCIStage1Transport(path, receipt, plan, opened.st_dev, opened.st_ino)
    except FileNotFoundError:
        raise StateError("stage-1 transport is missing") from None
    except OSError:
        raise StateError("stage-1 transport cannot be securely read") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


__all__ = [
    "MAX_OCI_STAGE1_TRANSPORT_BYTES",
    "MAX_OCI_STAGE1_TRANSPORT_PAYLOAD_BYTES",
    "BuiltOCIStage1Transport",
    "OCIStage1TransportReceipt",
    "OCI_STAGE1_TRANSPORT_DEVICE_POLICY",
    "OCI_STAGE1_TRANSPORT_FILENAME",
    "OCI_STAGE1_TRANSPORT_FORMAT",
    "OCI_STAGE1_TRANSPORT_SCHEMA",
    "VerifiedOCIStage1Transport",
    "build_stage1_transport",
    "verify_stage1_transport",
    "verify_stage1_transport_file",
]
