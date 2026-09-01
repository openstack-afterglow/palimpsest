"""Deterministic OCI-derived recipe cache and durable occurrence leases."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore, ArtifactStoreError
from .digest import normalize_digest
from .errors import ArtifactValidationError
from .oci_changeset import OCI_CHANGESET_NORMALIZATION_ID
from .oci_converter import LayerIntakeReceipt
from .oci_packer import (
    SQUASHFS_PACKER_ARGV_CONTRACT_ID,
    SQUASHFS_STRUCTURAL_VERIFIER_ID,
    LeasedSquashFS,
    PackedSquashFSReceipt,
    SquashFSToolchainIdentity,
    VerifiedSquashFSToolchain,
)
from .oci_source import SnapshottedOCIImage
from .oci_tar_emitter import OCI_NORMALIZED_TAR_EMISSION_ID
from .state import StatePaths

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o400
_TEMP_MODE = 0o600
_LOCK_MODE = 0o600
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024**3
_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_TEMP_RE = re.compile(r"^\.oci-derived-tmp-[0-9a-f]{32}$")


class _ForkCloseFD:
    __slots__ = ("fd",)

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close_in_child(self) -> None:
        fd, self.fd = self.fd, -1
        _close_noerror(fd)


_FORK_CLOSE_LOCK_FDS: set[_ForkCloseFD] = set()
_FORK_REGISTRY_LOCK = threading.Lock()

DERIVED_RECIPE_SCHEMA = "palimpsest.oci-derived-recipe.v1"
DERIVED_RECORD_SCHEMA = "palimpsest.oci-derived-record.v1"
DERIVED_OCCURRENCE_SCHEMA = "palimpsest.oci-derived-occurrence.v1"
_DERIVED_LEASE_SCHEMA_V1 = "palimpsest.oci-derived-lease.v1"
_DERIVED_LEASE_SET_SCHEMA_V1 = "palimpsest.oci-derived-lease-set.v1"
DERIVED_LEASE_SCHEMA = "palimpsest.oci-derived-lease.v2"
DERIVED_LEASE_SET_SCHEMA = "palimpsest.oci-derived-lease-set.v2"
MATERIALIZATION_CACHE_RESULTS = frozenset({"warm_hit", "cold_miss", "cold_repair"})


def _close_inherited_lease_locks() -> None:
    """Drop child copies so a surviving fork cannot retain a parent's store lock."""
    try:
        inherited = tuple(_FORK_CLOSE_LOCK_FDS)
        _FORK_CLOSE_LOCK_FDS.clear()
        for resource in inherited:
            resource.close_in_child()
    finally:
        _FORK_REGISTRY_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_FORK_REGISTRY_LOCK.acquire,
        after_in_parent=_FORK_REGISTRY_LOCK.release,
        after_in_child=_close_inherited_lease_locks,
    )


class OCIStoreError(ArtifactValidationError):
    """Stable path-free failure from the OCI-derived store."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _exact_wire_fields(data: Any, expected: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(data, dict) or any(not isinstance(name, str) for name in data):
        raise OCIStoreError("oci-store-wire", f"{field_name} must be an object")
    if set(data) != expected:
        raise OCIStoreError("oci-store-wire", f"{field_name} fields are invalid")
    return data


def _raise_with_cleanup(
    primary: BaseException,
    label: str,
    contexts: tuple[AbstractContextManager[Any] | None, ...],
) -> None:
    failures: list[BaseException] = [primary]
    for context in contexts:
        if context is None:
            continue
        try:
            context.__exit__(type(primary), primary, primary.__traceback__)
        except BaseException as cleanup:
            failures.append(cleanup)
    if len(failures) > 1:
        raise BaseExceptionGroup(label, failures) from None
    raise primary.with_traceback(primary.__traceback__)


def _digest_hex(value: str) -> str:
    normalized = normalize_digest(value)
    return normalized.split(":", 1)[1]


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _stable(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _close_noerror(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(fd, payload[offset:])
        except OSError:
            raise OCIStoreError("oci-store-write", "derived metadata write failed") from None
        if written <= 0:
            raise OCIStoreError("oci-store-write", "derived metadata write was incomplete")
        offset += written


@dataclass(frozen=True, slots=True)
class DerivedLayerOccurrence:
    source_snapshot_binding_digest: str
    source_image_digest: str
    ordinal: int
    media_type: str
    compressed_digest: str
    compressed_size: int
    diff_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_snapshot_binding_digest",
            normalize_digest(self.source_snapshot_binding_digest),
        )
        object.__setattr__(self, "source_image_digest", normalize_digest(self.source_image_digest))
        object.__setattr__(self, "compressed_digest", normalize_digest(self.compressed_digest))
        object.__setattr__(self, "diff_id", normalize_digest(self.diff_id))
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise OCIStoreError("oci-store-occurrence", "derived occurrence ordinal is invalid")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise OCIStoreError("oci-store-occurrence", "derived occurrence media type is invalid")
        if type(self.compressed_size) is not int or not 0 <= self.compressed_size <= 2**63 - 1:
            raise OCIStoreError("oci-store-occurrence", "derived occurrence size is invalid")

    @classmethod
    def from_image(cls, image: SnapshottedOCIImage, ordinal: int) -> DerivedLayerOccurrence:
        if not isinstance(image, SnapshottedOCIImage) or type(ordinal) is not int:
            raise OCIStoreError("oci-store-occurrence", "snapshotted image occurrence is invalid")
        if not 0 <= ordinal < len(image.image.layers):
            raise OCIStoreError("oci-store-occurrence", "snapshotted image ordinal is out of range")
        layer = image.image.layers[ordinal]
        return cls(
            source_snapshot_binding_digest=image.binding_digest,
            source_image_digest=image.image.digest,
            ordinal=ordinal,
            media_type=layer.compressed.media_type,
            compressed_digest=layer.compressed.digest,
            compressed_size=layer.compressed.size,
            diff_id=layer.diff_id,
        )

    def source_recipe(self) -> dict[str, Any]:
        return {
            "compressed_digest": self.compressed_digest,
            "compressed_size": self.compressed_size,
            "diff_id": self.diff_id,
            "media_type": self.media_type,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "compressed_digest": self.compressed_digest,
            "compressed_size": self.compressed_size,
            "diff_id": self.diff_id,
            "media_type": self.media_type,
            "ordinal": self.ordinal,
            "source_image_digest": self.source_image_digest,
            "source_snapshot_binding_digest": self.source_snapshot_binding_digest,
        }

    @classmethod
    def from_dict(cls, data: Any) -> DerivedLayerOccurrence:
        fields = {
            "compressed_digest",
            "compressed_size",
            "diff_id",
            "media_type",
            "ordinal",
            "source_image_digest",
            "source_snapshot_binding_digest",
        }
        value = _exact_wire_fields(data, fields, "derived occurrence")
        try:
            occurrence = cls(**value)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-wire", "derived occurrence is invalid") from None
        if occurrence.to_dict() != value:
            raise OCIStoreError("oci-store-wire", "derived occurrence is not canonical")
        return occurrence


@dataclass(frozen=True, slots=True)
class DerivedSquashFSKey:
    source_media_type: str
    compressed_digest: str
    compressed_size: int
    diff_id: str
    intake_policy_id: str
    intake_policy_fingerprint: str
    normalization_contract_id: str
    tar_emission_contract_id: str
    pack_policy_id: str
    pack_policy_fingerprint: str
    packer_contract_id: str
    packer_version: str
    packer_executable_digest: str
    packer_dependency_digests: tuple[str, ...]
    packer_toolchain_fingerprint: str
    structural_verifier: str

    def __post_init__(self) -> None:
        for field_name in (
            "compressed_digest",
            "diff_id",
            "intake_policy_fingerprint",
            "pack_policy_fingerprint",
            "packer_executable_digest",
            "packer_toolchain_fingerprint",
        ):
            object.__setattr__(self, field_name, normalize_digest(getattr(self, field_name)))
        if type(self.compressed_size) is not int or not 0 <= self.compressed_size <= 2**63 - 1:
            raise OCIStoreError("oci-store-key", "derived recipe size is invalid")
        for field_name in (
            "source_media_type",
            "intake_policy_id",
            "normalization_contract_id",
            "tar_emission_contract_id",
            "pack_policy_id",
            "packer_contract_id",
            "packer_version",
            "structural_verifier",
        ):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name):
                raise OCIStoreError("oci-store-key", "derived recipe contains an invalid contract")
        if not isinstance(self.packer_dependency_digests, tuple):
            raise OCIStoreError("oci-store-key", "packer dependency digests must be immutable")
        dependencies = tuple(sorted(normalize_digest(value) for value in self.packer_dependency_digests))
        if len(set(dependencies)) != len(dependencies):
            raise OCIStoreError("oci-store-key", "packer dependency digests must be unique")
        object.__setattr__(self, "packer_dependency_digests", dependencies)
        expected = SquashFSToolchainIdentity(
            self.packer_version,
            self.packer_executable_digest,
            dependencies,
        ).fingerprint
        if expected != self.packer_toolchain_fingerprint:
            raise OCIStoreError("oci-store-key", "packer toolchain fingerprint is inconsistent")

    @classmethod
    def for_occurrence(
        cls,
        occurrence: DerivedLayerOccurrence,
        *,
        intake_policy_id: str,
        intake_policy_fingerprint: str,
        pack_policy_id: str,
        pack_policy_fingerprint: str,
        toolchain: VerifiedSquashFSToolchain,
    ) -> DerivedSquashFSKey:
        if not isinstance(occurrence, DerivedLayerOccurrence) or not isinstance(toolchain, VerifiedSquashFSToolchain):
            raise OCIStoreError("oci-store-key", "derived recipe input is invalid")
        identity = toolchain.identity
        return cls(
            source_media_type=occurrence.media_type,
            compressed_digest=occurrence.compressed_digest,
            compressed_size=occurrence.compressed_size,
            diff_id=occurrence.diff_id,
            intake_policy_id=intake_policy_id,
            intake_policy_fingerprint=intake_policy_fingerprint,
            normalization_contract_id=OCI_CHANGESET_NORMALIZATION_ID,
            tar_emission_contract_id=OCI_NORMALIZED_TAR_EMISSION_ID,
            pack_policy_id=pack_policy_id,
            pack_policy_fingerprint=pack_policy_fingerprint,
            packer_contract_id=SQUASHFS_PACKER_ARGV_CONTRACT_ID,
            packer_version=identity.version,
            packer_executable_digest=identity.executable_digest,
            packer_dependency_digests=identity.dependency_digests,
            packer_toolchain_fingerprint=identity.fingerprint,
            structural_verifier=SQUASHFS_STRUCTURAL_VERIFIER_ID,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["domain"] = DERIVED_RECIPE_SCHEMA
        value["packer_dependency_digests"] = list(self.packer_dependency_digests)
        return value

    @classmethod
    def from_dict(cls, data: Any) -> DerivedSquashFSKey:
        fields = {
            "compressed_digest",
            "compressed_size",
            "diff_id",
            "domain",
            "intake_policy_fingerprint",
            "intake_policy_id",
            "normalization_contract_id",
            "pack_policy_fingerprint",
            "pack_policy_id",
            "packer_contract_id",
            "packer_dependency_digests",
            "packer_executable_digest",
            "packer_toolchain_fingerprint",
            "packer_version",
            "source_media_type",
            "structural_verifier",
            "tar_emission_contract_id",
        }
        value = _exact_wire_fields(data, fields, "derived key")
        if value["domain"] != DERIVED_RECIPE_SCHEMA or not isinstance(value["packer_dependency_digests"], list):
            raise OCIStoreError("oci-store-wire", "derived key schema is invalid")
        constructor = dict(value)
        constructor.pop("domain")
        constructor["packer_dependency_digests"] = tuple(value["packer_dependency_digests"])
        try:
            key = cls(**constructor)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-wire", "derived key is invalid") from None
        if key.to_dict() != value:
            raise OCIStoreError("oci-store-wire", "derived key is not canonical")
        return key

    @property
    def digest(self) -> str:
        return _digest_bytes(_canonical(self.to_dict()))

    def matches(self, occurrence: DerivedLayerOccurrence) -> bool:
        return occurrence.source_recipe() == {
            "compressed_digest": self.compressed_digest,
            "compressed_size": self.compressed_size,
            "diff_id": self.diff_id,
            "media_type": self.source_media_type,
        }


@dataclass(frozen=True, slots=True)
class DerivedLayerReceipt:
    store_id: str
    occurrence_digest: str
    record_digest: str
    key_digest: str
    source_snapshot_binding_digest: str
    source_image_digest: str
    ordinal: int
    image_digest: str
    image_size: int
    filesystem: str = "squashfs"

    def __post_init__(self) -> None:
        if not isinstance(self.store_id, str) or re.fullmatch(r"oci-store-v1:[0-9a-f]{64}", self.store_id) is None:
            raise OCIStoreError("oci-store-receipt", "derived receipt store identity is invalid")
        for field_name in (
            "occurrence_digest",
            "record_digest",
            "key_digest",
            "source_snapshot_binding_digest",
            "source_image_digest",
            "image_digest",
        ):
            original = getattr(self, field_name)
            try:
                normalized = normalize_digest(original)
            except (ArtifactValidationError, TypeError, ValueError):
                raise OCIStoreError("oci-store-receipt", "derived receipt digest is invalid") from None
            if normalized != original:
                raise OCIStoreError("oci-store-receipt", "derived receipt digest is not canonical")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise OCIStoreError("oci-store-receipt", "derived receipt ordinal is invalid")
        if type(self.image_size) is not int or self.image_size <= 0:
            raise OCIStoreError("oci-store-receipt", "derived receipt image size is invalid")
        if self.filesystem != "squashfs":
            raise OCIStoreError("oci-store-receipt", "derived receipt filesystem is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> DerivedLayerReceipt:
        fields = {
            "image_digest",
            "image_size",
            "filesystem",
            "key_digest",
            "occurrence_digest",
            "ordinal",
            "record_digest",
            "source_image_digest",
            "source_snapshot_binding_digest",
            "store_id",
        }
        value = _exact_wire_fields(data, fields, "derived receipt")
        try:
            receipt = cls(**value)
        except (OCIStoreError, TypeError, ValueError):
            raise OCIStoreError("oci-store-wire", "derived receipt is invalid") from None
        if receipt.to_dict() != value:
            raise OCIStoreError("oci-store-wire", "derived receipt is not canonical")
        return receipt


def _durable_receipt_value(receipt: DerivedLayerReceipt, schema: str) -> dict[str, Any]:
    value = receipt.to_dict()
    if schema in {_DERIVED_LEASE_SCHEMA_V1, _DERIVED_LEASE_SET_SCHEMA_V1}:
        value.pop("filesystem")
    elif schema not in {DERIVED_LEASE_SCHEMA, DERIVED_LEASE_SET_SCHEMA}:
        raise OCIStoreError("oci-store-wire", "derived durable receipt schema is invalid")
    return value


def _durable_receipt_from_dict(value: Any, schema: Any) -> DerivedLayerReceipt:
    if schema in {DERIVED_LEASE_SCHEMA, DERIVED_LEASE_SET_SCHEMA}:
        return DerivedLayerReceipt.from_dict(value)
    if schema not in {_DERIVED_LEASE_SCHEMA_V1, _DERIVED_LEASE_SET_SCHEMA_V1}:
        raise OCIStoreError("oci-store-wire", "derived durable receipt schema is invalid")
    legacy_fields = {
        "image_digest",
        "image_size",
        "key_digest",
        "occurrence_digest",
        "ordinal",
        "record_digest",
        "source_image_digest",
        "source_snapshot_binding_digest",
        "store_id",
    }
    legacy = _exact_wire_fields(value, legacy_fields, "legacy derived receipt")
    return DerivedLayerReceipt.from_dict({**legacy, "filesystem": "squashfs"})


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    """One receipt plus the invocation-local derived-cache result."""

    receipt: DerivedLayerReceipt
    cache_result: str

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, DerivedLayerReceipt):
            raise OCIStoreError("oci-store-result", "materialization receipt is invalid")
        if not isinstance(self.cache_result, str) or self.cache_result not in MATERIALIZATION_CACHE_RESULTS:
            raise OCIStoreError("oci-store-result", "materialization cache result is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"cache_result": self.cache_result, "receipt": self.receipt.to_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> MaterializationResult:
        value = _exact_wire_fields(data, {"cache_result", "receipt"}, "materialization result")
        try:
            result = cls(
                receipt=DerivedLayerReceipt.from_dict(value["receipt"]),
                cache_result=value["cache_result"],
            )
        except (OCIStoreError, TypeError, ValueError):
            raise OCIStoreError("oci-store-wire", "materialization result is invalid") from None
        if result.to_dict() != value:
            raise OCIStoreError("oci-store-wire", "materialization result is not canonical")
        return result


@dataclass(frozen=True, slots=True)
class ArtifactLeaseOwner:
    run_id: str
    run_name: str
    role: str

    def __post_init__(self) -> None:
        try:
            canonical_id = str(uuid.UUID(self.run_id))
        except (ValueError, AttributeError):
            raise OCIStoreError("oci-store-owner", "lease owner run ID is invalid") from None
        object.__setattr__(self, "run_id", canonical_id)
        if _NAME_RE.fullmatch(self.run_name or "") is None or _ROLE_RE.fullmatch(self.role or "") is None:
            raise OCIStoreError("oci-store-owner", "lease owner name or role is invalid")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RecoverableDerivedLease:
    """Durable lease reference discoverable after a publisher crash."""

    lease_id: str
    owner: ArtifactLeaseOwner
    receipt: DerivedLayerReceipt
    acquired_ns: int


@dataclass(frozen=True, slots=True)
class DurableLeaseSetMember:
    """One ordinal-preserving member of an immutable durable lease set."""

    ordinal: int
    lease_id: str
    receipt: DerivedLayerReceipt
    acquired_ns: int

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise OCIStoreError("oci-store-lease-set", "lease-set member ordinal is invalid")
        try:
            canonical_id = str(uuid.UUID(self.lease_id))
        except (ValueError, AttributeError):
            raise OCIStoreError("oci-store-lease-set", "lease-set member ID is invalid") from None
        object.__setattr__(self, "lease_id", canonical_id)
        if not isinstance(self.receipt, DerivedLayerReceipt) or self.receipt.ordinal != self.ordinal:
            raise OCIStoreError("oci-store-lease-set", "lease-set member receipt is invalid")
        if type(self.acquired_ns) is not int or self.acquired_ns < 0:
            raise OCIStoreError("oci-store-lease-set", "lease-set member time is invalid")


@dataclass(frozen=True, slots=True)
class DurableLeaseSet:
    """Complete, path-free reservation of an ordered derived-layer graph."""

    lease_set_id: str
    plan_digest: str
    owner: ArtifactLeaseOwner
    members: tuple[DurableLeaseSetMember, ...]

    def __post_init__(self) -> None:
        try:
            normalized_id = normalize_digest(self.lease_set_id)
            normalized_plan = normalize_digest(self.plan_digest)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-lease-set", "lease-set digest is invalid") from None
        if normalized_id != self.lease_set_id or normalized_plan != self.plan_digest:
            raise OCIStoreError("oci-store-lease-set", "lease-set digest is not canonical")
        if not isinstance(self.owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-lease-set", "lease-set owner is invalid")
        if not isinstance(self.members, tuple) or not self.members:
            raise OCIStoreError("oci-store-lease-set", "lease-set members are invalid")
        if tuple(member.ordinal for member in self.members) != tuple(range(len(self.members))):
            raise OCIStoreError("oci-store-lease-set", "lease-set member order is invalid")
        if len({member.lease_id for member in self.members}) != len(self.members):
            raise OCIStoreError("oci-store-lease-set", "lease-set member IDs are not unique")


@dataclass(frozen=True, slots=True)
class RecoverableLeaseSetIntent:
    """Restart-discoverable lease-set intent, including partial publication."""

    lease_set_id: str
    plan_digest: str
    owner: ArtifactLeaseOwner
    receipts: tuple[DerivedLayerReceipt, ...]
    member_lease_ids: tuple[str, ...]
    present_lease_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.member_lease_ids == self.present_lease_ids


@dataclass(slots=True)
class _MetadataAuthority:
    root_fd: int
    records_fd: int
    keys_fd: int
    occurrences_fd: int
    leases_fd: int
    lease_sets_fd: int
    locks_fd: int
    signature: tuple[tuple[int, int, int, int], ...]
    store_id: str


def _directory_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode)


@contextmanager
def _open_absolute_directory(path: Path) -> Iterator[int]:
    if not path.is_absolute() or "\0" in os.fspath(path):
        raise OCIStoreError("oci-store-root", "derived store root must be absolute")
    opened: list[int] = []
    try:
        current = os.open("/", _DIR_FLAGS)
        opened.append(current)
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise OCIStoreError("oci-store-root", "derived store root contains relative components")
            current = os.open(component, _DIR_FLAGS, dir_fd=current)
            opened.append(current)
        yield current
    except OCIStoreError:
        raise
    except OSError:
        raise OCIStoreError("oci-store-root", "derived store root cannot be securely opened") from None
    finally:
        for fd in reversed(opened):
            _close_noerror(fd)


def _ensure_root(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute == Path("/") or not absolute.name:
        raise OCIStoreError("oci-store-root", "derived store root must be a private child")
    with _open_absolute_directory(absolute.parent) as parent_fd:
        fd: int | None = None
        created = False
        try:
            try:
                os.mkdir(absolute.name, _DIRECTORY_MODE, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            fd = os.open(absolute.name, _DIR_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(fd)
            entry = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or _identity(opened) != _identity(entry)
            ):
                raise OCIStoreError("oci-store-root", "derived store root is unsafe")
            if stat.S_IMODE(opened.st_mode) != _DIRECTORY_MODE:
                os.fchmod(fd, _DIRECTORY_MODE)
                os.fsync(fd)
            if created:
                os.fsync(parent_fd)
        except OCIStoreError:
            raise
        except OSError:
            raise OCIStoreError("oci-store-root", "derived store root cannot be initialized") from None
        finally:
            _close_noerror(fd)
    return absolute


def _ensure_child(parent_fd: int, name: str) -> int:
    created = False
    fd: int | None = None
    try:
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(fd)
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid() or _identity(opened) != _identity(entry):
            raise OCIStoreError("oci-store-root", "derived store component is unsafe")
        if stat.S_IMODE(opened.st_mode) != _DIRECTORY_MODE:
            os.fchmod(fd, _DIRECTORY_MODE)
            os.fsync(fd)
        if created:
            os.fsync(parent_fd)
        result = fd
        fd = None
        return result
    except OCIStoreError:
        raise
    except OSError:
        raise OCIStoreError("oci-store-root", "derived store component cannot be initialized") from None
    finally:
        _close_noerror(fd)


class DurableDerivedLayerLease:
    """Process/thread-bound reader backed by a durable on-disk lease record."""

    __slots__ = (
        "_closed",
        "_context",
        "_owner_pid",
        "_owner_thread",
        "_reader",
        "_started",
        "_store",
        "_verified",
        "_lease_id",
        "_owner",
        "_receipt",
        "_use_authority_context",
        "_use_lock_context",
    )

    def __init__(
        self,
        store: OCIStore,
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
        context: AbstractContextManager[Any],
        reader: Any,
        use_authority_context: AbstractContextManager[Any],
        use_lock_context: AbstractContextManager[Any],
    ) -> None:
        self._store = store
        self._lease_id = lease_id
        self._owner = owner
        self._receipt = receipt
        self._context = context
        self._reader = reader
        self._use_authority_context = use_authority_context
        self._use_lock_context = use_lock_context
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._started = False
        self._verified = False
        self._closed = False

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def owner(self) -> ArtifactLeaseOwner:
        return self._owner

    @property
    def receipt(self) -> DerivedLayerReceipt:
        return self._receipt

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            self._reader._abort()
            self._closed = True
            raise OCIStoreError("oci-store-lease-owner", "derived lease cannot cross a process")
        if threading.get_ident() != self._owner_thread:
            raise OCIStoreError("oci-store-lease-owner", "derived lease cannot cross a thread")
        if self._closed:
            raise OCIStoreError("oci-store-lease-closed", "derived lease is closed")

    def chunks(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        self._check_owner()
        if type(chunk_size) is not int or not 1 <= chunk_size <= 1024 * 1024:
            raise OCIStoreError("oci-store-lease-read", "derived lease chunk size is invalid")
        if self._started:
            raise OCIStoreError("oci-store-lease-read", "derived lease stream is single-use")
        self._started = True
        while True:
            self._check_owner()
            payload = self._reader.read(chunk_size)
            if not payload:
                break
            yield payload
        self._reader.finish()
        self._verified = True

    def close(self) -> None:
        self._check_owner()
        if not self._verified:
            raise OCIStoreError("oci-store-lease-incomplete", "derived lease must reach verified EOF before release")
        try:
            self._context.__exit__(None, None, None)
            self._store._release_lease(self.lease_id, self.owner, self.receipt)
        finally:
            self._closed = True
            self._release_use_lock()

    def detach(self) -> str:
        """Close the reader while deliberately retaining its durable lease."""
        self._check_owner()
        lease_id = self.lease_id
        self._abort()
        return lease_id

    def _release_use_lock(self) -> None:
        cleanup_errors: list[BaseException] = []
        for context in (self._use_lock_context, self._use_authority_context):
            try:
                context.__exit__(None, None, None)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            primary = sys.exception()
            failures = ([primary] if primary is not None else []) + cleanup_errors
            if len(failures) == 1:
                raise failures[0]
            raise BaseExceptionGroup("derived lease lock cleanup failed", failures) from None

    def _abort(self) -> None:
        if not self._closed:
            self._closed = True
            failure = OCIStoreError("oci-store-lease-abort", "derived lease reader was aborted")
            try:
                self._context.__exit__(OCIStoreError, failure, None)
            finally:
                self._release_use_lock()

    def __enter__(self) -> DurableDerivedLayerLease:
        self._check_owner()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            self._abort()
            return
        self.close()

    def __copy__(self) -> DurableDerivedLayerLease:
        raise OCIStoreError("oci-store-lease-copy", "derived lease cannot be copied")

    def __deepcopy__(self, _memo: object) -> DurableDerivedLayerLease:
        raise OCIStoreError("oci-store-lease-copy", "derived lease cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("derived lease cannot be serialized")


Producer = Callable[[], AbstractContextManager[tuple[LayerIntakeReceipt, LeasedSquashFS]]]


class OCIStore:
    """Recipe-keyed derived cache with per-occurrence durable retention leases."""

    def __init__(
        self,
        roots: StatePaths,
        *,
        repair_min_age_seconds: float = 300.0,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not isinstance(roots, StatePaths) or not callable(wall_clock_ns):
            raise OCIStoreError("oci-store-policy", "derived store inputs are invalid")
        if (
            not isinstance(repair_min_age_seconds, (int, float))
            or isinstance(repair_min_age_seconds, bool)
            or not 0 <= float(repair_min_age_seconds) < float("inf")
        ):
            raise OCIStoreError("oci-store-policy", "derived repair age is invalid")
        self._root = _ensure_root(roots.oci_derived_store)
        self._repair_age_ns = int(float(repair_min_age_seconds) * 1_000_000_000)
        self._clock = wall_clock_ns
        self._artifacts = ArtifactStore(
            roots.store,
            repair_min_age_seconds=repair_min_age_seconds,
            wall_clock_ns=wall_clock_ns,
        )
        with self._authority() as authority:
            self._signature = authority.signature
            self._metadata_id = authority.store_id
        self._store_id = _digest_bytes(f"{self._artifacts.identity}|{self._metadata_id}".encode())

    @property
    def identity(self) -> str:
        return f"oci-store-v1:{self._store_id.split(':', 1)[1]}"

    def _verify_authority_binding(self, authority: _MetadataAuthority) -> None:
        try:
            with _open_absolute_directory(self._root) as visible_root_fd:
                opened = tuple(
                    os.fstat(fd)
                    for fd in (
                        authority.root_fd,
                        authority.records_fd,
                        authority.keys_fd,
                        authority.occurrences_fd,
                        authority.leases_fd,
                        authority.lease_sets_fd,
                        authority.locks_fd,
                    )
                )
                entries = (
                    os.fstat(visible_root_fd),
                    *(
                        os.stat(name, dir_fd=authority.root_fd, follow_symlinks=False)
                        for name in (
                            "records",
                            "keys",
                            "occurrences",
                            "leases",
                            "lease-sets",
                            "locks",
                        )
                    ),
                )
        except (OSError, OCIStoreError):
            raise OCIStoreError("oci-store-authority", "derived store authority changed") from None
        opened_signature = tuple(_directory_signature(value) for value in opened)
        entry_signature = tuple(_directory_signature(value) for value in entries)
        expected = getattr(self, "_signature", authority.signature)
        if (
            opened_signature != authority.signature
            or entry_signature != authority.signature
            or expected != authority.signature
        ):
            raise OCIStoreError("oci-store-authority", "derived store authority changed")

    @contextmanager
    def _authority(self) -> Iterator[_MetadataAuthority]:
        opened: list[int] = []
        with _open_absolute_directory(self._root) as root_fd:
            try:
                root = os.fstat(root_fd)
                if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != _DIRECTORY_MODE:
                    raise OCIStoreError("oci-store-authority", "derived store root changed")
                for name in ("records", "keys", "occurrences", "leases", "lease-sets", "locks"):
                    opened.append(_ensure_child(root_fd, name))
                signature = tuple(_directory_signature(os.fstat(fd)) for fd in (root_fd, *opened))
                identity = _digest_bytes(repr(signature).encode())
                expected = getattr(self, "_signature", signature)
                if signature != expected:
                    raise OCIStoreError("oci-store-authority", "derived store authority changed")
                authority = _MetadataAuthority(root_fd, *opened, signature, identity)
                self._verify_authority_binding(authority)
                yield authority
                self._verify_authority_binding(authority)
            finally:
                for fd in reversed(opened):
                    _close_noerror(fd)

    @contextmanager
    def _lock(
        self,
        authority: _MetadataAuthority,
        name: str,
        *,
        close_in_fork_child: bool = True,
    ) -> Iterator[None]:
        if not re.fullmatch(r"[a-z0-9-]{1,100}\.lock", name):
            raise OCIStoreError("oci-store-lock", "derived lock name is invalid")
        fd: int | None = None
        fork_resource: _ForkCloseFD | None = None
        try:
            self._verify_authority_binding(authority)
            created = False
            prior: os.stat_result | None = None
            registry_locked = False
            try:
                if close_in_fork_child:
                    _FORK_REGISTRY_LOCK.acquire()
                    registry_locked = True
                try:
                    fd = os.open(name, _LOCK_FLAGS | os.O_EXCL, _LOCK_MODE, dir_fd=authority.locks_fd)
                    created = True
                except FileExistsError:
                    prior = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
                    fd = os.open(name, _LOCK_FLAGS, dir_fd=authority.locks_fd)
                if close_in_fork_child:
                    fork_resource = _ForkCloseFD(fd)
                    _FORK_CLOSE_LOCK_FDS.add(fork_resource)
                opened = os.fstat(fd)
            except OSError:
                raise OCIStoreError("oci-store-lock", "derived lock cannot be opened") from None
            finally:
                if registry_locked:
                    _FORK_REGISTRY_LOCK.release()
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (prior is not None and _identity(prior) != _identity(opened))
            ):
                raise OCIStoreError("oci-store-lock", "derived lock is unsafe")
            os.fchmod(fd, _LOCK_MODE)
            os.fsync(fd)
            if created:
                os.fsync(authority.locks_fd)
            visible = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
            if _identity(visible) != _identity(opened):
                raise OCIStoreError("oci-store-lock", "derived lock binding changed")
            fcntl.flock(fd, fcntl.LOCK_EX)
            visible = os.stat(name, dir_fd=authority.locks_fd, follow_symlinks=False)
            if _identity(visible) != _identity(opened):
                raise OCIStoreError("oci-store-lock", "derived lock split during acquire")
            self._verify_authority_binding(authority)
            yield
            self._verify_authority_binding(authority)
        except OCIStoreError:
            raise
        except OSError:
            raise OCIStoreError("oci-store-lock", "derived lock operation failed") from None
        finally:
            if fork_resource is not None:
                _FORK_REGISTRY_LOCK.acquire()
                try:
                    fd, fork_resource.fd = fork_resource.fd, -1
                    if fd >= 0:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                        _close_noerror(fd)
                    _FORK_CLOSE_LOCK_FDS.discard(fork_resource)
                finally:
                    _FORK_REGISTRY_LOCK.release()
            elif fd is not None and fd >= 0:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                _close_noerror(fd)

    def _read_file(self, directory_fd: int, name: str) -> tuple[bytes, os.stat_result]:
        fd: int | None = None
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            fd = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != _FILE_MODE
                or _identity(entry) != _identity(opened)
                or not 0 < opened.st_size <= _MAX_RECORD_BYTES
            ):
                raise OCIStoreError("oci-store-corrupt", "derived metadata target is unsafe")
            payload = bytearray()
            while len(payload) < opened.st_size:
                chunk = os.read(fd, min(65536, opened.st_size - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(fd)
            final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if len(payload) != opened.st_size or _stable(after) != _stable(opened) or _stable(final) != _stable(opened):
                raise OCIStoreError("oci-store-corrupt", "derived metadata changed during read")
            return bytes(payload), opened
        except FileNotFoundError:
            raise OCIStoreError("oci-store-missing", "derived metadata target is missing") from None
        except OCIStoreError:
            raise
        except OSError:
            raise OCIStoreError("oci-store-corrupt", "derived metadata cannot be read") from None
        finally:
            _close_noerror(fd)

    @staticmethod
    def _decode(payload: bytes) -> dict[str, Any]:
        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise OCIStoreError("oci-store-corrupt", "derived metadata has duplicate keys")
                result[key] = value
            return result

        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
        except OCIStoreError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OCIStoreError("oci-store-corrupt", "derived metadata is not strict JSON") from None
        if not isinstance(value, dict) or _canonical(value) != payload:
            raise OCIStoreError("oci-store-corrupt", "derived metadata is not canonical")
        return value

    def _old_enough(self, metadata: os.stat_result) -> bool:
        now = self._clock()
        newest = max(metadata.st_mtime_ns, metadata.st_ctime_ns)
        return type(now) is int and now >= newest and now - newest >= self._repair_age_ns

    def _publish_file(
        self,
        authority: _MetadataAuthority,
        directory_fd: int,
        name: str,
        payload: bytes,
        *,
        existing_validator: Callable[[bytes], bool] | None = None,
    ) -> None:
        with self._lock(authority, "metadata-publish.lock"):
            self._publish_file_locked(
                directory_fd,
                name,
                payload,
                existing_validator=existing_validator,
            )

    def _publish_file_locked(
        self,
        directory_fd: int,
        name: str,
        payload: bytes,
        *,
        existing_validator: Callable[[bytes], bool] | None,
    ) -> None:
        if not 0 < len(payload) <= _MAX_RECORD_BYTES:
            raise OCIStoreError("oci-store-record", "derived metadata size is invalid")
        temporary = f".oci-derived-tmp-{uuid.uuid4().hex}"
        temp_fd: int | None = None
        try:
            temp_fd = os.open(temporary, _TEMP_FLAGS, _TEMP_MODE, dir_fd=directory_fd)
            initial = os.fstat(temp_fd)
            _write_all(temp_fd, payload)
            os.fchmod(temp_fd, _FILE_MODE)
            os.fsync(temp_fd)
            sealed = os.fstat(temp_fd)
            if _identity(initial) != _identity(sealed) or sealed.st_size != len(payload) or sealed.st_nlink != 1:
                raise OCIStoreError("oci-store-record", "derived metadata temporary changed")
            try:
                existing, prior = self._read_file(directory_fd, name)
            except OCIStoreError as exc:
                if exc.code == "oci-store-missing":
                    prior = None
                elif exc.code == "oci-store-corrupt":
                    try:
                        prior = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError:
                        raise OCIStoreError("oci-store-corrupt", "derived metadata repair state changed") from None
                    stable = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _stable(stable) != _stable(prior) or not self._old_enough(stable):
                        raise OCIStoreError(
                            "oci-store-repair-deferred", "fresh derived corruption cannot be repaired"
                        ) from None
                else:
                    raise
            else:
                if existing == payload:
                    return
                if existing_validator is not None and not existing_validator(existing):
                    if not self._old_enough(prior):
                        raise OCIStoreError("oci-store-repair-deferred", "fresh derived corruption cannot be repaired")
                else:
                    raise OCIStoreError("oci-store-collision", "derived metadata name has different valid content")
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary = ""
            os.fsync(directory_fd)
            published, current = self._read_file(directory_fd, name)
            if published != payload or _identity(current) != _identity(sealed):
                raise OCIStoreError("oci-store-record", "derived metadata publication changed")
        except OCIStoreError:
            raise
        except OSError:
            raise OCIStoreError("oci-store-record", "derived metadata publication failed") from None
        finally:
            cleanup_errors: list[BaseException] = []
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    cleanup_errors.append(OCIStoreError("oci-store-cleanup", "metadata temporary close failed"))
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except OSError:
                    cleanup_errors.append(OCIStoreError("oci-store-cleanup", "metadata temporary cleanup failed"))
            if cleanup_errors:
                primary = sys.exception()
                failures = ([primary] if primary is not None else []) + cleanup_errors
                if len(failures) == 1:
                    raise failures[0]
                raise BaseExceptionGroup("derived metadata cleanup failed", failures) from None

    def _record_from_index(
        self,
        authority: _MetadataAuthority,
        key: DerivedSquashFSKey,
    ) -> tuple[dict[str, Any], str] | None:
        try:
            index_payload, _ = self._read_file(authority.keys_fd, _digest_hex(key.digest))
        except OCIStoreError as exc:
            if exc.code == "oci-store-missing":
                return None
            raise
        index = self._decode(index_payload)
        if index.get("schema") != DERIVED_RECIPE_SCHEMA or index.get("key_digest") != key.digest:
            raise OCIStoreError("oci-store-corrupt", "derived recipe index binding is invalid")
        try:
            record_digest = normalize_digest(index.get("record_digest", ""))
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-corrupt", "derived recipe record digest is invalid") from None
        record_payload, _ = self._read_file(authority.records_fd, _digest_hex(record_digest))
        if _digest_bytes(record_payload) != record_digest:
            raise OCIStoreError("oci-store-corrupt", "derived record content digest is invalid")
        record = self._decode(record_payload)
        if (
            record.get("schema") != DERIVED_RECORD_SCHEMA
            or record.get("key_digest") != key.digest
            or record.get("key") != key.to_dict()
        ):
            raise OCIStoreError("oci-store-corrupt", "derived record recipe binding is invalid")
        try:
            image_digest = normalize_digest(record.get("image_digest", ""))
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-corrupt", "derived record image digest is invalid") from None
        image_size = record.get("image_size")
        if type(image_size) is not int or not 0 < image_size <= _MAX_IMAGE_BYTES:
            raise OCIStoreError("oci-store-corrupt", "derived record image size is invalid")
        self._validate_cached_receipts(record, key, image_digest, image_size)
        self._artifacts.verify_squashfs(image_digest, image_size, maximum=_MAX_IMAGE_BYTES)
        return record, record_digest

    @staticmethod
    def _validate_cached_receipts(
        record: dict[str, Any],
        key: DerivedSquashFSKey,
        image_digest: str,
        image_size: int,
    ) -> None:
        intake_value = record.get("intake_receipt")
        packed_value = record.get("packed_receipt")
        if not isinstance(intake_value, dict) or not isinstance(packed_value, dict):
            raise OCIStoreError("oci-store-corrupt", "derived cached receipts are missing")
        try:
            intake = LayerIntakeReceipt(**intake_value)
            packed_fields = dict(packed_value)
            dependencies = packed_fields.get("toolchain_dependency_digests")
            if not isinstance(dependencies, list):
                raise TypeError
            packed_fields["toolchain_dependency_digests"] = tuple(dependencies)
            packed = PackedSquashFSReceipt(**packed_fields)
        except (TypeError, ValueError):
            raise OCIStoreError("oci-store-corrupt", "derived cached receipts are malformed") from None
        if (
            type(intake.ordinal) is not int
            or intake.ordinal < 0
            or intake.ordinal != packed.source_ordinal
            or intake.media_type != key.source_media_type
            or intake.compressed_digest != key.compressed_digest
            or intake.compressed_size != key.compressed_size
            or intake.diff_id != key.diff_id
            or intake.policy_id != key.intake_policy_id
            or intake.policy_fingerprint != key.intake_policy_fingerprint
            or packed.source_diff_id != key.diff_id
            or packed.policy_id != key.pack_policy_id
            or packed.policy_fingerprint != key.pack_policy_fingerprint
            or packed.packer_version != key.packer_version
            or f"sha256:{packed.packer_sha256}" != key.packer_executable_digest
            or packed.toolchain_fingerprint != key.packer_toolchain_fingerprint
            or packed.toolchain_dependency_digests != key.packer_dependency_digests
            or packed.structural_verifier != key.structural_verifier
            or packed.image_digest != image_digest
            or packed.image_size != image_size
            or type(packed.normalized_tar_size) is not int
            or packed.normalized_tar_size <= 0
            or type(packed.entries) is not int
            or packed.entries <= 0
        ):
            raise OCIStoreError("oci-store-corrupt", "derived cached receipt binding is invalid")
        try:
            normalize_digest(packed.normalized_tar_digest)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-corrupt", "derived cached tar digest is invalid") from None

    def _occurrence_receipt(
        self,
        authority: _MetadataAuthority,
        occurrence: DerivedLayerOccurrence,
        key: DerivedSquashFSKey,
        record: dict[str, Any],
        record_digest: str,
    ) -> DerivedLayerReceipt:
        occurrence_record = {
            "key_digest": key.digest,
            "ordinal": occurrence.ordinal,
            "record_digest": record_digest,
            "schema": DERIVED_OCCURRENCE_SCHEMA,
            "source": occurrence.source_recipe(),
            "source_image_digest": occurrence.source_image_digest,
            "source_snapshot_binding_digest": occurrence.source_snapshot_binding_digest,
        }
        payload = _canonical(occurrence_record)
        occurrence_digest = _digest_bytes(payload)
        self._publish_file(
            authority,
            authority.occurrences_fd,
            _digest_hex(occurrence_digest),
            payload,
            existing_validator=lambda existing: _digest_bytes(existing) == occurrence_digest,
        )
        return DerivedLayerReceipt(
            store_id=self.identity,
            occurrence_digest=occurrence_digest,
            record_digest=record_digest,
            key_digest=key.digest,
            source_snapshot_binding_digest=occurrence.source_snapshot_binding_digest,
            source_image_digest=occurrence.source_image_digest,
            ordinal=occurrence.ordinal,
            image_digest=normalize_digest(record["image_digest"]),
            image_size=record["image_size"],
            filesystem="squashfs",
        )

    @staticmethod
    def _validate_producer(
        occurrence: DerivedLayerOccurrence,
        key: DerivedSquashFSKey,
        intake: LayerIntakeReceipt,
        packed: PackedSquashFSReceipt,
    ) -> None:
        if not isinstance(intake, LayerIntakeReceipt) or not isinstance(packed, PackedSquashFSReceipt):
            raise OCIStoreError("oci-store-producer", "derived producer receipts are invalid")
        packed_value = asdict(packed)
        packed_value["toolchain_dependency_digests"] = list(packed.toolchain_dependency_digests)
        try:
            OCIStore._validate_cached_receipts(
                {"intake_receipt": asdict(intake), "packed_receipt": packed_value},
                key,
                packed.image_digest,
                packed.image_size,
            )
        except OCIStoreError:
            raise OCIStoreError("oci-store-producer", "derived producer receipts are internally inconsistent") from None
        if (
            intake.ordinal != occurrence.ordinal
            or intake.media_type != occurrence.media_type
            or intake.compressed_digest != occurrence.compressed_digest
            or intake.compressed_size != occurrence.compressed_size
            or intake.diff_id != occurrence.diff_id
            or intake.policy_id != key.intake_policy_id
            or intake.policy_fingerprint != key.intake_policy_fingerprint
            or packed.source_ordinal != occurrence.ordinal
            or packed.source_diff_id != occurrence.diff_id
            or packed.policy_id != key.pack_policy_id
            or packed.policy_fingerprint != key.pack_policy_fingerprint
            or packed.packer_version != key.packer_version
            or f"sha256:{packed.packer_sha256}" != key.packer_executable_digest
            or packed.toolchain_fingerprint != key.packer_toolchain_fingerprint
            or packed.toolchain_dependency_digests != key.packer_dependency_digests
            or packed.structural_verifier != key.structural_verifier
        ):
            raise OCIStoreError("oci-store-producer", "derived producer receipts do not match the recipe")

    def materialize(
        self,
        occurrence: DerivedLayerOccurrence,
        key: DerivedSquashFSKey,
        producer: Producer,
    ) -> DerivedLayerReceipt:
        return self.materialize_observed(occurrence, key, producer).receipt

    def materialize_observed(
        self,
        occurrence: DerivedLayerOccurrence,
        key: DerivedSquashFSKey,
        producer: Producer,
    ) -> MaterializationResult:
        if not isinstance(occurrence, DerivedLayerOccurrence) or not isinstance(key, DerivedSquashFSKey):
            raise OCIStoreError("oci-store-input", "derived materialization input is invalid")
        if not key.matches(occurrence) or not callable(producer):
            raise OCIStoreError("oci-store-input", "derived recipe does not match its occurrence")
        with self._authority() as authority, self._lock(authority, f"recipe-{_digest_hex(key.digest)}.lock"):
            cache_result = "warm_hit"
            try:
                cached = self._record_from_index(authority, key)
            except OCIStoreError as exc:
                if exc.code not in {"oci-store-corrupt", "oci-store-missing"}:
                    raise
                cached = None
                cache_result = "cold_repair"
            except ArtifactStoreError as exc:
                if exc.code not in {"artifact-corrupt", "artifact-missing", "artifact-structure"}:
                    raise
                cached = None
                cache_result = "cold_repair"
            if cached is None:
                if cache_result == "warm_hit":
                    cache_result = "cold_miss"
                with producer() as produced:
                    if not isinstance(produced, tuple) or len(produced) != 2:
                        raise OCIStoreError("oci-store-producer", "derived producer contract is invalid")
                    intake, packed = produced
                    self._validate_producer(occurrence, key, intake, packed.receipt)
                    stored = self._artifacts.publish_squashfs(
                        packed.chunks(),
                        expected_digest=packed.receipt.image_digest,
                        expected_size=packed.receipt.image_size,
                        maximum=_MAX_IMAGE_BYTES,
                    )
                    record = {
                        "image_digest": stored.digest,
                        "image_size": stored.size,
                        "intake_receipt": asdict(intake),
                        "key": key.to_dict(),
                        "key_digest": key.digest,
                        "packed_receipt": asdict(packed.receipt),
                        "schema": DERIVED_RECORD_SCHEMA,
                    }
                    record["packed_receipt"]["toolchain_dependency_digests"] = list(
                        packed.receipt.toolchain_dependency_digests
                    )
                    record_payload = _canonical(record)
                    record_digest = _digest_bytes(record_payload)
                    self._publish_file(
                        authority,
                        authority.records_fd,
                        _digest_hex(record_digest),
                        record_payload,
                        existing_validator=lambda existing: _digest_bytes(existing) == record_digest,
                    )
                    index_payload = _canonical(
                        {
                            "key_digest": key.digest,
                            "record_digest": record_digest,
                            "schema": DERIVED_RECIPE_SCHEMA,
                        }
                    )

                    self._publish_file(
                        authority,
                        authority.keys_fd,
                        _digest_hex(key.digest),
                        index_payload,
                        existing_validator=lambda existing: existing == index_payload,
                    )
                    cached = record, record_digest
            record, record_digest = cached
            receipt = self._occurrence_receipt(authority, occurrence, key, record, record_digest)
            return MaterializationResult(receipt=receipt, cache_result=cache_result)

    def _read_occurrence(
        self,
        authority: _MetadataAuthority,
        receipt: DerivedLayerReceipt,
        *,
        verify_artifact: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(receipt, DerivedLayerReceipt) or receipt.store_id != self.identity:
            raise OCIStoreError("oci-store-receipt", "derived receipt belongs to a different store")
        payload, _ = self._read_file(authority.occurrences_fd, _digest_hex(receipt.occurrence_digest))
        if _digest_bytes(payload) != receipt.occurrence_digest:
            raise OCIStoreError("oci-store-corrupt", "derived occurrence digest is invalid")
        value = self._decode(payload)
        if (
            value.get("schema") != DERIVED_OCCURRENCE_SCHEMA
            or value.get("record_digest") != receipt.record_digest
            or value.get("key_digest") != receipt.key_digest
            or value.get("ordinal") != receipt.ordinal
            or value.get("source_image_digest") != receipt.source_image_digest
            or value.get("source_snapshot_binding_digest") != receipt.source_snapshot_binding_digest
        ):
            raise OCIStoreError("oci-store-corrupt", "derived occurrence binding is invalid")
        record_payload, _ = self._read_file(authority.records_fd, _digest_hex(receipt.record_digest))
        if _digest_bytes(record_payload) != receipt.record_digest:
            raise OCIStoreError("oci-store-corrupt", "derived record digest is invalid")
        record = self._decode(record_payload)
        if (
            record.get("image_digest") != receipt.image_digest
            or record.get("image_size") != receipt.image_size
            or receipt.filesystem != "squashfs"
        ):
            raise OCIStoreError("oci-store-corrupt", "derived receipt image binding is invalid")
        if verify_artifact:
            self._artifacts.verify_squashfs(receipt.image_digest, receipt.image_size, maximum=_MAX_IMAGE_BYTES)
        return value

    def validate_receipt_occurrences(
        self,
        receipts: tuple[DerivedLayerReceipt, ...],
        occurrences: tuple[DerivedLayerOccurrence, ...],
    ) -> None:
        """Bind public receipts back to the exact ordered OCI layer recipes."""
        if (
            not isinstance(receipts, tuple)
            or not receipts
            or not isinstance(occurrences, tuple)
            or len(receipts) != len(occurrences)
            or any(not isinstance(receipt, DerivedLayerReceipt) for receipt in receipts)
            or any(not isinstance(occurrence, DerivedLayerOccurrence) for occurrence in occurrences)
        ):
            raise OCIStoreError("oci-store-receipt", "ordered receipt occurrences are invalid")
        if tuple(receipt.ordinal for receipt in receipts) != tuple(range(len(receipts))) or tuple(
            occurrence.ordinal for occurrence in occurrences
        ) != tuple(range(len(occurrences))):
            raise OCIStoreError("oci-store-receipt", "ordered receipt occurrences are out of order")
        with self._authority() as authority:
            for receipt, occurrence in zip(receipts, occurrences, strict=True):
                value = self._read_occurrence(authority, receipt)
                if (
                    receipt.store_id != self.identity
                    or occurrence.source_snapshot_binding_digest != receipt.source_snapshot_binding_digest
                    or occurrence.source_image_digest != receipt.source_image_digest
                    or occurrence.ordinal != receipt.ordinal
                    or value.get("source") != occurrence.source_recipe()
                ):
                    raise OCIStoreError("oci-store-receipt", "derived receipt occurrence binding is invalid")

    def acquire_lease(
        self,
        receipt: DerivedLayerReceipt,
        owner: ArtifactLeaseOwner,
    ) -> DurableDerivedLayerLease:
        if not isinstance(owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-owner", "derived lease owner is invalid")
        lease_id = str(uuid.uuid4())
        use_authority_context, use_lock_context, authority = self._acquire_use_lock(lease_id)
        artifact_context: AbstractContextManager[Any] | None = None
        digest_context = self._artifacts.digest_guard(receipt.image_digest)
        digest_entered = False
        try:
            digest_context.__enter__()
            digest_entered = True
            self._read_occurrence(authority, receipt)
            artifact_context = self._artifacts.open_squashfs(
                receipt.image_digest,
                receipt.image_size,
                maximum=_MAX_IMAGE_BYTES,
            )
            reader = artifact_context.__enter__()
            acquired_ns = self._clock()
            if type(acquired_ns) is not int or acquired_ns < 0:
                raise OCIStoreError("oci-store-clock", "derived lease acquisition clock is invalid")
            with self._lock(authority, "lease-index.lock"):
                payload = _canonical(
                    {
                        "acquired_ns": acquired_ns,
                        "image_digest": receipt.image_digest,
                        "image_size": receipt.image_size,
                        "lease_id": lease_id,
                        "occurrence_digest": receipt.occurrence_digest,
                        "owner": asdict(owner),
                        "receipt": _durable_receipt_value(receipt, DERIVED_LEASE_SCHEMA),
                        "schema": DERIVED_LEASE_SCHEMA,
                    }
                )
                self._publish_file(authority, authority.leases_fd, lease_id, payload)
            lease = DurableDerivedLayerLease(
                self,
                lease_id,
                owner,
                receipt,
                artifact_context,
                reader,
                use_authority_context,
                use_lock_context,
            )
            digest_entered = False
            digest_context.__exit__(None, None, None)
            return lease
        except BaseException as exc:
            _raise_with_cleanup(
                exc,
                "derived lease acquisition cleanup failed",
                (
                    digest_context if digest_entered else None,
                    artifact_context,
                    use_lock_context,
                    use_authority_context,
                ),
            )

    def _acquire_use_lock(
        self, lease_id: str
    ) -> tuple[AbstractContextManager[Any], AbstractContextManager[Any], _MetadataAuthority]:
        canonical_id = str(uuid.UUID(lease_id))
        authority_context = self._authority()
        authority = authority_context.__enter__()
        lock_context = self._lock(
            authority,
            f"lease-use-{canonical_id.replace('-', '')}.lock",
            close_in_fork_child=True,
        )
        try:
            lock_context.__enter__()
        except BaseException as exc:
            _raise_with_cleanup(exc, "derived lease lock acquisition cleanup failed", (authority_context,))
        return authority_context, lock_context, authority

    def _validate_lease_record(
        self,
        authority: _MetadataAuthority,
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
    ) -> None:
        self._read_occurrence(authority, receipt)
        payload, _ = self._read_file(authority.leases_fd, lease_id)
        value = self._decode(payload)
        self._validate_lease_value(value, lease_id, owner, receipt)

    @staticmethod
    def _validate_lease_value(
        value: dict[str, Any],
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
    ) -> None:
        schema = value.get("schema")
        if schema not in {DERIVED_LEASE_SCHEMA, _DERIVED_LEASE_SCHEMA_V1}:
            raise OCIStoreError("oci-store-corrupt", "derived lease record schema is invalid")
        if (
            value.get("lease_id") != lease_id
            or value.get("owner") != asdict(owner)
            or value.get("receipt") != _durable_receipt_value(receipt, schema)
            or value.get("occurrence_digest") != receipt.occurrence_digest
            or value.get("image_digest") != receipt.image_digest
            or value.get("image_size") != receipt.image_size
        ):
            raise OCIStoreError("oci-store-corrupt", "derived lease record binding is invalid")

    def _open_lease(
        self,
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
    ) -> DurableDerivedLayerLease:
        use_authority_context, use_lock_context, authority = self._acquire_use_lock(lease_id)
        context: AbstractContextManager[Any] | None = None
        try:
            self._validate_lease_record(authority, lease_id, owner, receipt)
            context = self._artifacts.open_squashfs(
                receipt.image_digest,
                receipt.image_size,
                maximum=_MAX_IMAGE_BYTES,
            )
            reader = context.__enter__()
        except BaseException as exc:
            _raise_with_cleanup(
                exc,
                "derived lease resume cleanup failed",
                (context, use_lock_context, use_authority_context),
            )
        return DurableDerivedLayerLease(
            self,
            lease_id,
            owner,
            receipt,
            context,
            reader,
            use_authority_context,
            use_lock_context,
        )

    def resume_lease(
        self,
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
    ) -> DurableDerivedLayerLease:
        try:
            canonical_id = str(uuid.UUID(lease_id))
        except (ValueError, AttributeError):
            raise OCIStoreError("oci-store-lease", "derived lease ID is invalid") from None
        return self._open_lease(canonical_id, owner, receipt)

    def release_recoverable_lease(
        self,
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
    ) -> None:
        """Release a detached/recovered lease without streaming its artifact."""
        if not isinstance(owner, ArtifactLeaseOwner) or not isinstance(receipt, DerivedLayerReceipt):
            raise OCIStoreError("oci-store-lease", "recoverable lease binding is invalid")
        try:
            canonical_id = str(uuid.UUID(lease_id))
        except (ValueError, AttributeError):
            raise OCIStoreError("oci-store-lease", "derived lease ID is invalid") from None
        authority_context, use_lock_context, _authority = self._acquire_use_lock(canonical_id)
        try:
            self._release_lease(canonical_id, owner, receipt)
        finally:
            failures: list[BaseException] = []
            for context in (use_lock_context, authority_context):
                try:
                    context.__exit__(None, None, None)
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                primary = sys.exception()
                combined = ([primary] if primary is not None else []) + failures
                if len(combined) == 1:
                    raise combined[0]
                raise BaseExceptionGroup("recoverable lease release cleanup failed", combined) from None

    @staticmethod
    def _validate_lease_set_inputs(
        receipts: tuple[DerivedLayerReceipt, ...],
        owner: ArtifactLeaseOwner,
        plan_digest: str,
    ) -> str:
        if not isinstance(owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-owner", "lease-set owner is invalid")
        try:
            normalized_plan = normalize_digest(plan_digest)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-lease-set", "lease-set plan digest is invalid") from None
        if normalized_plan != plan_digest:
            raise OCIStoreError("oci-store-lease-set", "lease-set plan digest is not canonical")
        if not isinstance(receipts, tuple) or not receipts:
            raise OCIStoreError("oci-store-lease-set", "lease-set receipts are invalid")
        if any(not isinstance(receipt, DerivedLayerReceipt) for receipt in receipts):
            raise OCIStoreError("oci-store-lease-set", "lease-set receipt is invalid")
        if tuple(receipt.ordinal for receipt in receipts) != tuple(range(len(receipts))):
            raise OCIStoreError("oci-store-lease-set", "lease-set receipt order is invalid")
        first = receipts[0]
        if any(
            receipt.store_id != first.store_id
            or receipt.source_snapshot_binding_digest != first.source_snapshot_binding_digest
            or receipt.source_image_digest != first.source_image_digest
            for receipt in receipts
        ):
            raise OCIStoreError("oci-store-lease-set", "lease-set source binding is inconsistent")
        return normalized_plan

    @staticmethod
    def _lease_set_identity_for_schema(
        receipts: tuple[DerivedLayerReceipt, ...],
        owner: ArtifactLeaseOwner,
        plan_digest: str,
        schema: str,
    ) -> str:
        return _digest_bytes(
            _canonical(
                {
                    "domain": schema,
                    "owner": owner.to_dict(),
                    "plan_digest": plan_digest,
                    "receipts": [_durable_receipt_value(receipt, schema) for receipt in receipts],
                }
            )
        )

    @staticmethod
    def _lease_set_member_id_for_schema(
        lease_set_id: str,
        ordinal: int,
        receipt: DerivedLayerReceipt,
        schema: str,
    ) -> str:
        binding = _canonical(
            {
                "domain": f"{schema}.member",
                "lease_set_id": lease_set_id,
                "ordinal": ordinal,
                "receipt": _durable_receipt_value(receipt, schema),
            }
        )
        return str(uuid.UUID(bytes=hashlib.sha256(binding).digest()[:16], version=5))

    @classmethod
    def _lease_set_identity(
        cls,
        receipts: tuple[DerivedLayerReceipt, ...],
        owner: ArtifactLeaseOwner,
        plan_digest: str,
    ) -> str:
        return cls._lease_set_identity_for_schema(receipts, owner, plan_digest, DERIVED_LEASE_SET_SCHEMA)

    @classmethod
    def _lease_set_member_id(cls, lease_set_id: str, ordinal: int, receipt: DerivedLayerReceipt) -> str:
        return cls._lease_set_member_id_for_schema(lease_set_id, ordinal, receipt, DERIVED_LEASE_SET_SCHEMA)

    @classmethod
    def _lease_set_intent_value(
        cls,
        receipts: tuple[DerivedLayerReceipt, ...],
        owner: ArtifactLeaseOwner,
        plan_digest: str,
        *,
        schema: str = DERIVED_LEASE_SET_SCHEMA,
    ) -> dict[str, Any]:
        lease_set_id = cls._lease_set_identity_for_schema(receipts, owner, plan_digest, schema)
        return {
            "lease_set_id": lease_set_id,
            "members": [
                {
                    "lease_id": cls._lease_set_member_id_for_schema(lease_set_id, ordinal, receipt, schema),
                    "ordinal": ordinal,
                    "receipt": _durable_receipt_value(receipt, schema),
                }
                for ordinal, receipt in enumerate(receipts)
            ],
            "owner": owner.to_dict(),
            "plan_digest": plan_digest,
            "schema": schema,
        }

    @classmethod
    def _decode_lease_set_intent(
        cls,
        value: dict[str, Any],
        lease_set_id: str,
        owner: ArtifactLeaseOwner,
        plan_digest: str,
    ) -> tuple[DerivedLayerReceipt, ...]:
        _exact_wire_fields(
            value,
            {"lease_set_id", "members", "owner", "plan_digest", "schema"},
            "derived lease set",
        )
        schema = value.get("schema")
        if schema not in {DERIVED_LEASE_SET_SCHEMA, _DERIVED_LEASE_SET_SCHEMA_V1}:
            raise OCIStoreError("oci-store-corrupt", "derived lease-set schema is invalid")
        if (
            value.get("lease_set_id") != lease_set_id
            or value.get("owner") != owner.to_dict()
            or value.get("plan_digest") != plan_digest
        ):
            raise OCIStoreError("oci-store-corrupt", "derived lease-set binding is invalid")
        members = value.get("members")
        if not isinstance(members, list) or not members:
            raise OCIStoreError("oci-store-corrupt", "derived lease-set members are invalid")
        receipts: list[DerivedLayerReceipt] = []
        for expected_ordinal, member_value in enumerate(members):
            member = _exact_wire_fields(
                member_value,
                {"lease_id", "ordinal", "receipt"},
                "derived lease-set member",
            )
            try:
                receipt = _durable_receipt_from_dict(member["receipt"], schema)
                canonical_id = str(uuid.UUID(member["lease_id"]))
            except (OCIStoreError, TypeError, ValueError, AttributeError):
                raise OCIStoreError("oci-store-corrupt", "derived lease-set member is malformed") from None
            if (
                member["ordinal"] != expected_ordinal
                or receipt.ordinal != expected_ordinal
                or canonical_id != member["lease_id"]
            ):
                raise OCIStoreError("oci-store-corrupt", "derived lease-set member order is invalid")
            receipts.append(receipt)
        receipt_tuple = tuple(receipts)
        cls._validate_lease_set_inputs(receipt_tuple, owner, plan_digest)
        expected = cls._lease_set_intent_value(receipt_tuple, owner, plan_digest, schema=schema)
        if value != expected:
            raise OCIStoreError("oci-store-corrupt", "derived lease-set content is not deterministic")
        return receipt_tuple

    def _read_lease_set_intent(
        self,
        authority: _MetadataAuthority,
        lease_set_id: str,
        owner: ArtifactLeaseOwner,
        plan_digest: str,
    ) -> tuple[dict[str, Any], tuple[DerivedLayerReceipt, ...]]:
        try:
            canonical_set_id = normalize_digest(lease_set_id)
            canonical_plan = normalize_digest(plan_digest)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-lease-set", "lease-set identity is invalid") from None
        if canonical_set_id != lease_set_id or canonical_plan != plan_digest:
            raise OCIStoreError("oci-store-lease-set", "lease-set identity is not canonical")
        payload, _ = self._read_file(authority.lease_sets_fd, _digest_hex(canonical_set_id))
        value = self._decode(payload)
        receipts = self._decode_lease_set_intent(value, canonical_set_id, owner, canonical_plan)
        if self._lease_set_identity_for_schema(receipts, owner, canonical_plan, value["schema"]) != canonical_set_id:
            raise OCIStoreError("oci-store-corrupt", "derived lease-set identity is invalid")
        return value, receipts

    @staticmethod
    def _lease_record_value(
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
        acquired_ns: int,
        *,
        schema: str = DERIVED_LEASE_SCHEMA,
    ) -> dict[str, Any]:
        if schema not in {DERIVED_LEASE_SCHEMA, _DERIVED_LEASE_SCHEMA_V1}:
            raise OCIStoreError("oci-store-wire", "derived lease schema is invalid")
        return {
            "acquired_ns": acquired_ns,
            "image_digest": receipt.image_digest,
            "image_size": receipt.image_size,
            "lease_id": lease_id,
            "occurrence_digest": receipt.occurrence_digest,
            "owner": owner.to_dict(),
            "receipt": _durable_receipt_value(receipt, schema),
            "schema": schema,
        }

    def acquire_lease_set(
        self,
        receipts: tuple[DerivedLayerReceipt, ...],
        owner: ArtifactLeaseOwner,
        *,
        plan_digest: str,
    ) -> DurableLeaseSet:
        """Atomically converge an ordered boot-plan reservation after retries."""
        self._validate_lease_set_inputs(receipts, owner, plan_digest)
        if any(receipt.store_id != self.identity for receipt in receipts):
            raise OCIStoreError("oci-store-receipt", "lease-set receipt belongs to a different store")
        current_intent = self._lease_set_intent_value(receipts, owner, plan_digest)
        legacy_intent = self._lease_set_intent_value(
            receipts,
            owner,
            plan_digest,
            schema=_DERIVED_LEASE_SET_SCHEMA_V1,
        )
        acquired_ns = self._clock()
        if type(acquired_ns) is not int or acquired_ns < 0:
            raise OCIStoreError("oci-store-clock", "lease-set acquisition clock is invalid")
        with ExitStack() as guards:
            for digest in sorted({receipt.image_digest for receipt in receipts}):
                guards.enter_context(self._artifacts.digest_guard(digest))
            with self._authority() as authority:
                for receipt in receipts:
                    self._read_occurrence(authority, receipt)
                members: list[DurableLeaseSetMember] = []
                with self._lock(authority, "lease-index.lock"):
                    intent = current_intent
                    try:
                        legacy_payload, _ = self._read_file(
                            authority.lease_sets_fd,
                            _digest_hex(legacy_intent["lease_set_id"]),
                        )
                    except OCIStoreError as exc:
                        if exc.code != "oci-store-missing":
                            raise
                    else:
                        legacy_value = self._decode(legacy_payload)
                        legacy_receipts = self._decode_lease_set_intent(
                            legacy_value,
                            legacy_intent["lease_set_id"],
                            owner,
                            plan_digest,
                        )
                        if legacy_receipts != receipts:
                            raise OCIStoreError("oci-store-corrupt", "legacy lease-set receipt binding is invalid")
                        intent = legacy_intent
                    lease_set_id = intent["lease_set_id"]
                    set_schema = intent["schema"]
                    lease_schema = (
                        _DERIVED_LEASE_SCHEMA_V1 if set_schema == _DERIVED_LEASE_SET_SCHEMA_V1 else DERIVED_LEASE_SCHEMA
                    )
                    self._publish_file(
                        authority,
                        authority.lease_sets_fd,
                        _digest_hex(lease_set_id),
                        _canonical(intent),
                    )
                    for ordinal, receipt in enumerate(receipts):
                        lease_id = intent["members"][ordinal]["lease_id"]
                        try:
                            payload, _ = self._read_file(authority.leases_fd, lease_id)
                        except OCIStoreError as exc:
                            if exc.code != "oci-store-missing":
                                raise
                            value = self._lease_record_value(
                                lease_id,
                                owner,
                                receipt,
                                acquired_ns,
                                schema=lease_schema,
                            )
                            self._publish_file(authority, authority.leases_fd, lease_id, _canonical(value))
                        else:
                            value = self._decode(payload)
                            self._validate_lease_value(value, lease_id, owner, receipt)
                        member_time = value.get("acquired_ns")
                        if type(member_time) is not int or member_time < 0:
                            raise OCIStoreError("oci-store-corrupt", "derived lease acquisition time is invalid")
                        members.append(DurableLeaseSetMember(ordinal, lease_id, receipt, member_time))
        return DurableLeaseSet(lease_set_id, plan_digest, owner, tuple(members))

    def lease_set_id(
        self,
        receipts: tuple[DerivedLayerReceipt, ...],
        owner: ArtifactLeaseOwner,
        *,
        plan_digest: str,
    ) -> str:
        """Return the deterministic identity for a validated ordered set."""
        self._validate_lease_set_inputs(receipts, owner, plan_digest)
        if any(receipt.store_id != self.identity for receipt in receipts):
            raise OCIStoreError("oci-store-receipt", "lease-set receipt belongs to a different store")
        return self._lease_set_identity(receipts, owner, plan_digest)

    def load_lease_set(
        self,
        lease_set_id: str,
        owner: ArtifactLeaseOwner,
        *,
        plan_digest: str,
    ) -> DurableLeaseSet:
        """Load only a complete, fully validated lease-set reservation."""
        if not isinstance(owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-owner", "lease-set owner is invalid")
        members: list[DurableLeaseSetMember] = []
        with self._authority() as authority:
            with self._lock(authority, "lease-index.lock"):
                intent, receipts = self._read_lease_set_intent(authority, lease_set_id, owner, plan_digest)
                for ordinal, receipt in enumerate(receipts):
                    lease_id = intent["members"][ordinal]["lease_id"]
                    payload, _ = self._read_file(authority.leases_fd, lease_id)
                    value = self._decode(payload)
                    self._validate_lease_value(value, lease_id, owner, receipt)
                    acquired_ns = value.get("acquired_ns")
                    if type(acquired_ns) is not int or acquired_ns < 0:
                        raise OCIStoreError("oci-store-corrupt", "derived lease acquisition time is invalid")
                    members.append(DurableLeaseSetMember(ordinal, lease_id, receipt, acquired_ns))
            for receipt in receipts:
                self._read_occurrence(authority, receipt)
        return DurableLeaseSet(lease_set_id, plan_digest, owner, tuple(members))

    def list_lease_set_intents(
        self,
        owner: ArtifactLeaseOwner | None = None,
    ) -> tuple[RecoverableLeaseSetIntent, ...]:
        """Enumerate exact complete and partial intents for restart reconciliation."""
        if owner is not None and not isinstance(owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-owner", "lease-set owner filter is invalid")
        found: list[RecoverableLeaseSetIntent] = []
        with self._authority() as authority:
            with self._lock(authority, "lease-index.lock"):
                try:
                    names = sorted(os.listdir(authority.lease_sets_fd))
                except OSError:
                    raise OCIStoreError("oci-store-lease-set", "lease-set intents cannot be enumerated") from None
                for name in names:
                    if _TEMP_RE.fullmatch(name) is not None:
                        continue
                    if _HEX_RE.fullmatch(name) is None:
                        raise OCIStoreError("oci-store-corrupt", "derived lease-set name is invalid")
                    lease_set_id = f"sha256:{name}"
                    payload, _ = self._read_file(authority.lease_sets_fd, name)
                    value = self._decode(payload)
                    owner_value = _exact_wire_fields(
                        value.get("owner"), {"role", "run_id", "run_name"}, "lease-set owner"
                    )
                    try:
                        intent_owner = ArtifactLeaseOwner(**owner_value)
                        plan_digest = normalize_digest(value.get("plan_digest", ""))
                        receipts = self._decode_lease_set_intent(value, lease_set_id, intent_owner, plan_digest)
                    except (OCIStoreError, ArtifactValidationError, TypeError, ValueError):
                        raise OCIStoreError("oci-store-corrupt", "derived lease-set intent is malformed") from None
                    if owner is not None and intent_owner != owner:
                        continue
                    member_ids = tuple(member["lease_id"] for member in value["members"])
                    present: list[str] = []
                    for lease_id, receipt in zip(member_ids, receipts, strict=True):
                        try:
                            lease_payload, _ = self._read_file(authority.leases_fd, lease_id)
                        except OCIStoreError as exc:
                            if exc.code == "oci-store-missing":
                                continue
                            raise
                        lease_value = self._decode(lease_payload)
                        self._validate_lease_value(lease_value, lease_id, intent_owner, receipt)
                        acquired_ns = lease_value.get("acquired_ns")
                        if type(acquired_ns) is not int or acquired_ns < 0:
                            raise OCIStoreError("oci-store-corrupt", "derived lease acquisition time is invalid")
                        present.append(lease_id)
                    found.append(
                        RecoverableLeaseSetIntent(
                            lease_set_id,
                            plan_digest,
                            intent_owner,
                            receipts,
                            member_ids,
                            tuple(present),
                        )
                    )
            for intent in found:
                for receipt in intent.receipts:
                    self._read_occurrence(authority, receipt, verify_artifact=False)
        return tuple(found)

    def release_lease_set(self, lease_set: DurableLeaseSet) -> None:
        """Release a complete lease set; interrupted releases remain retryable."""
        if not isinstance(lease_set, DurableLeaseSet):
            raise OCIStoreError("oci-store-lease-set", "lease-set release input is invalid")
        receipts = tuple(member.receipt for member in lease_set.members)
        valid_bindings = []
        for schema in (DERIVED_LEASE_SET_SCHEMA, _DERIVED_LEASE_SET_SCHEMA_V1):
            expected_id = self._lease_set_identity_for_schema(
                receipts,
                lease_set.owner,
                lease_set.plan_digest,
                schema,
            )
            expected_members = tuple(
                self._lease_set_member_id_for_schema(expected_id, member.ordinal, member.receipt, schema)
                for member in lease_set.members
            )
            valid_bindings.append((expected_id, expected_members))
        if not any(
            expected_id == lease_set.lease_set_id
            and expected_members == tuple(member.lease_id for member in lease_set.members)
            for expected_id, expected_members in valid_bindings
        ):
            raise OCIStoreError("oci-store-lease-set", "lease-set release binding changed")
        try:
            self.rollback_lease_set(
                lease_set.lease_set_id,
                lease_set.owner,
                plan_digest=lease_set.plan_digest,
            )
        except OCIStoreError as exc:
            if exc.code != "oci-store-missing":
                raise
            with self._authority() as authority, self._lock(authority, "lease-index.lock"):
                for member in lease_set.members:
                    try:
                        self._read_file(authority.leases_fd, member.lease_id)
                    except OCIStoreError as missing:
                        if missing.code == "oci-store-missing":
                            continue
                        raise
                    raise OCIStoreError(
                        "oci-store-corrupt", "released lease-set intent is missing while a member remains"
                    ) from None

    def rollback_lease_set(
        self,
        lease_set_id: str,
        owner: ArtifactLeaseOwner,
        *,
        plan_digest: str,
    ) -> None:
        """Remove an exact complete or partially published deterministic set."""
        if not isinstance(owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-owner", "lease-set owner is invalid")
        with self._authority() as authority:
            with self._lock(authority, "lease-index.lock"):
                intent, receipts = self._read_lease_set_intent(authority, lease_set_id, owner, plan_digest)
            with ExitStack() as locks:
                lease_ids = tuple(member["lease_id"] for member in intent["members"])
                for lease_id in sorted(lease_ids):
                    locks.enter_context(
                        self._lock(
                            authority,
                            f"lease-use-{lease_id.replace('-', '')}.lock",
                            close_in_fork_child=True,
                        )
                    )
                for digest in sorted({receipt.image_digest for receipt in receipts}):
                    locks.enter_context(self._artifacts.digest_guard(digest))
                with self._lock(authority, "lease-index.lock"):
                    current, current_receipts = self._read_lease_set_intent(authority, lease_set_id, owner, plan_digest)
                    if current != intent or current_receipts != receipts:
                        raise OCIStoreError("oci-store-corrupt", "derived lease-set changed during release")
                    for ordinal, receipt in enumerate(receipts):
                        lease_id = lease_ids[ordinal]
                        try:
                            payload, _ = self._read_file(authority.leases_fd, lease_id)
                        except OCIStoreError as exc:
                            if exc.code == "oci-store-missing":
                                continue
                            raise
                        value = self._decode(payload)
                        self._validate_lease_value(value, lease_id, owner, receipt)
                        try:
                            os.unlink(lease_id, dir_fd=authority.leases_fd)
                        except FileNotFoundError:
                            continue
                        except OSError:
                            raise OCIStoreError("oci-store-lease", "lease-set member release failed") from None
                    os.fsync(authority.leases_fd)
                    try:
                        os.unlink(_digest_hex(lease_set_id), dir_fd=authority.lease_sets_fd)
                        os.fsync(authority.lease_sets_fd)
                    except OSError:
                        raise OCIStoreError("oci-store-lease-set", "lease-set intent release failed") from None

    def assert_artifact_unleased(self, digest: str) -> None:
        """Fail closed when any durable derived occurrence lease retains a digest.

        Callers that mutate physical bytes must already hold ArtifactStore's
        per-digest guard.  This preserves the shared lock order used by lease
        acquisition: artifact digest, then durable lease index.
        """
        try:
            normalized = normalize_digest(digest)
        except (ArtifactValidationError, TypeError, ValueError):
            raise OCIStoreError("oci-store-retention", "artifact retention digest is invalid") from None
        expected_fields = {
            "acquired_ns",
            "image_digest",
            "image_size",
            "lease_id",
            "occurrence_digest",
            "owner",
            "receipt",
            "schema",
        }
        with self._authority() as authority, self._lock(authority, "lease-index.lock"):
            try:
                lease_set_names = sorted(os.listdir(authority.lease_sets_fd))
            except OSError:
                raise OCIStoreError("oci-store-retention", "derived lease sets cannot be enumerated") from None
            for name in lease_set_names:
                if _TEMP_RE.fullmatch(name) is not None:
                    continue
                if _HEX_RE.fullmatch(name) is None:
                    raise OCIStoreError("oci-store-corrupt", "derived lease-set name is invalid")
                lease_set_id = f"sha256:{name}"
                payload, _ = self._read_file(authority.lease_sets_fd, name)
                value = self._decode(payload)
                owner_value = _exact_wire_fields(value.get("owner"), {"role", "run_id", "run_name"}, "lease-set owner")
                try:
                    owner = ArtifactLeaseOwner(**owner_value)
                    plan_digest = normalize_digest(value.get("plan_digest", ""))
                    receipts = self._decode_lease_set_intent(value, lease_set_id, owner, plan_digest)
                except (OCIStoreError, ArtifactValidationError, TypeError, ValueError):
                    raise OCIStoreError("oci-store-corrupt", "derived lease-set retention is malformed") from None
                for receipt in receipts:
                    self._read_occurrence(authority, receipt, verify_artifact=False)
                    if receipt.image_digest == normalized:
                        raise OCIStoreError("oci-store-in-use", "artifact is retained by a durable OCI lease set")
            try:
                names = sorted(os.listdir(authority.leases_fd))
            except OSError:
                raise OCIStoreError("oci-store-retention", "derived leases cannot be enumerated") from None
            for name in names:
                if _TEMP_RE.fullmatch(name) is not None:
                    continue
                try:
                    canonical_id = str(uuid.UUID(name))
                except (ValueError, AttributeError):
                    raise OCIStoreError("oci-store-corrupt", "derived lease name is invalid") from None
                payload, _ = self._read_file(authority.leases_fd, canonical_id)
                value = _exact_wire_fields(self._decode(payload), expected_fields, "derived lease")
                owner_value = _exact_wire_fields(value.get("owner"), {"role", "run_id", "run_name"}, "lease owner")
                try:
                    owner = ArtifactLeaseOwner(**owner_value)
                    receipt = _durable_receipt_from_dict(value.get("receipt"), value.get("schema"))
                except (OCIStoreError, TypeError, ValueError):
                    raise OCIStoreError("oci-store-corrupt", "derived lease binding is malformed") from None
                self._validate_lease_value(value, canonical_id, owner, receipt)
                acquired_ns = value.get("acquired_ns")
                if type(acquired_ns) is not int or acquired_ns < 0:
                    raise OCIStoreError("oci-store-corrupt", "derived lease acquisition time is invalid")
                self._read_occurrence(authority, receipt, verify_artifact=False)
                if receipt.image_digest == normalized:
                    raise OCIStoreError("oci-store-in-use", "artifact is retained by a durable OCI lease")

    def list_leases(self, owner: ArtifactLeaseOwner) -> tuple[RecoverableDerivedLease, ...]:
        """List fully validated durable leases owned by one run identity."""
        if not isinstance(owner, ArtifactLeaseOwner):
            raise OCIStoreError("oci-store-owner", "derived lease owner is invalid")
        found: list[RecoverableDerivedLease] = []
        snapshots: list[tuple[str, dict[str, Any], DerivedLayerReceipt, int]] = []
        with self._authority() as authority:
            with self._lock(authority, "lease-index.lock"):
                try:
                    names = sorted(os.listdir(authority.leases_fd))
                except OSError:
                    raise OCIStoreError("oci-store-lease", "derived leases cannot be enumerated") from None
                for name in names:
                    if _TEMP_RE.fullmatch(name) is not None:
                        continue
                    try:
                        canonical_id = str(uuid.UUID(name))
                    except (ValueError, AttributeError):
                        raise OCIStoreError("oci-store-corrupt", "derived lease name is invalid") from None
                    payload, _ = self._read_file(authority.leases_fd, canonical_id)
                    value = self._decode(payload)
                    if value.get("owner") != asdict(owner):
                        continue
                    try:
                        receipt = _durable_receipt_from_dict(value.get("receipt"), value.get("schema"))
                    except (OCIStoreError, TypeError, ValueError):
                        raise OCIStoreError("oci-store-corrupt", "derived lease receipt is malformed") from None
                    self._validate_lease_value(value, canonical_id, owner, receipt)
                    acquired_ns = value.get("acquired_ns")
                    if type(acquired_ns) is not int or acquired_ns < 0:
                        raise OCIStoreError("oci-store-corrupt", "derived lease acquisition time is invalid")
                    snapshots.append((canonical_id, value, receipt, acquired_ns))
            artifacts: dict[tuple[str, int], None] = {}
            for canonical_id, _value, receipt, acquired_ns in snapshots:
                self._read_occurrence(authority, receipt, verify_artifact=False)
                artifacts[(receipt.image_digest, receipt.image_size)] = None
                found.append(RecoverableDerivedLease(canonical_id, owner, receipt, acquired_ns))
            for image_digest, image_size in artifacts:
                self._artifacts.verify_squashfs(image_digest, image_size, maximum=_MAX_IMAGE_BYTES)
        return tuple(found)

    def _release_lease(
        self,
        lease_id: str,
        owner: ArtifactLeaseOwner,
        receipt: DerivedLayerReceipt,
    ) -> None:
        try:
            canonical_id = str(uuid.UUID(lease_id))
        except (ValueError, AttributeError):
            raise OCIStoreError("oci-store-lease", "derived lease ID is invalid") from None
        with self._authority() as authority, self._lock(authority, "lease-index.lock"):
            self._validate_lease_record(authority, canonical_id, owner, receipt)
            try:
                os.unlink(canonical_id, dir_fd=authority.leases_fd)
                os.fsync(authority.leases_fd)
            except OSError:
                raise OCIStoreError("oci-store-lease", "derived lease release failed") from None

    def repair_stale_temporaries(self, *, minimum_age_seconds: float) -> int:
        if (
            not isinstance(minimum_age_seconds, (int, float))
            or isinstance(minimum_age_seconds, bool)
            or not 0 <= float(minimum_age_seconds) < float("inf")
        ):
            raise OCIStoreError("oci-store-policy", "stale temporary age is invalid")
        minimum_ns = int(float(minimum_age_seconds) * 1_000_000_000)
        minimum_ns = max(minimum_ns, self._repair_age_ns)
        now = self._clock()
        if type(now) is not int:
            raise OCIStoreError("oci-store-clock", "derived repair clock is invalid")
        removed = 0
        with self._authority() as authority:
            with self._lock(authority, "metadata-publish.lock"):
                for directory_fd in (
                    authority.records_fd,
                    authority.keys_fd,
                    authority.occurrences_fd,
                    authority.leases_fd,
                    authority.lease_sets_fd,
                ):
                    try:
                        names = os.listdir(directory_fd)
                    except OSError:
                        raise OCIStoreError("oci-store-repair", "derived temporaries cannot be enumerated") from None
                    removed_here = 0
                    for name in names:
                        if _TEMP_RE.fullmatch(name) is None:
                            continue
                        try:
                            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        except OSError:
                            raise OCIStoreError("oci-store-repair", "derived temporary state is unavailable") from None
                        newest = max(entry.st_mtime_ns, entry.st_ctime_ns)
                        if (
                            not stat.S_ISREG(entry.st_mode)
                            or entry.st_uid != os.geteuid()
                            or now < newest
                            or now - newest < minimum_ns
                        ):
                            continue
                        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if _stable(current) != _stable(entry):
                            continue
                        try:
                            os.unlink(name, dir_fd=directory_fd)
                            removed += 1
                            removed_here += 1
                        except FileNotFoundError:
                            continue
                        except OSError:
                            raise OCIStoreError("oci-store-repair", "stale derived temporary removal failed") from None
                    if removed_here:
                        os.fsync(directory_fd)
        try:
            removed += self._artifacts.repair_stale_temporaries(minimum_age_seconds=float(minimum_ns) / 1_000_000_000)
        except ArtifactStoreError as exc:
            raise OCIStoreError("oci-store-repair", str(exc)) from None
        return removed


__all__ = [
    "ArtifactLeaseOwner",
    "DERIVED_LEASE_SCHEMA",
    "DERIVED_LEASE_SET_SCHEMA",
    "DERIVED_OCCURRENCE_SCHEMA",
    "DERIVED_RECIPE_SCHEMA",
    "DERIVED_RECORD_SCHEMA",
    "DerivedLayerOccurrence",
    "DerivedLayerReceipt",
    "DerivedSquashFSKey",
    "DurableDerivedLayerLease",
    "DurableLeaseSet",
    "DurableLeaseSetMember",
    "MATERIALIZATION_CACHE_RESULTS",
    "MaterializationResult",
    "OCIStore",
    "OCIStoreError",
    "RecoverableDerivedLease",
    "RecoverableLeaseSetIntent",
]
