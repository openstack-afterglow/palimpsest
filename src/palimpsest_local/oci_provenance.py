"""Pure canonical provenance contracts for OCI-root materialization.

The types in this module describe content identities only.  They deliberately
have no registry, filesystem, subprocess, VM, or run-lifecycle integration.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .digest import require_digest
from .errors import ArtifactValidationError, InvalidDigestError

PROVENANCE_SCHEMA_VERSION = 1

OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
DOCKER_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
OCI_IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST_LIST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.list.v2+json"
OCI_IMAGE_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
DOCKER_IMAGE_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_LAYER_GZIP_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_LAYER_GZIP_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
PROVENANCE_ARTIFACT_TYPE = "application/vnd.pieroot.palimpsest.provenance.v1+json"
PROVENANCE_BLOB_MEDIA_TYPE = PROVENANCE_ARTIFACT_TYPE
PROVENANCE_DIGEST_ANNOTATION = "dev.pieroot.palimpsest.provenance.digest"
RUNTIME_INDEX_MEDIA_TYPE = "application/vnd.pieroot.palimpsest.runtime-index.v1+json"
SQUASHFS_DERIVED_LAYER_MEDIA_TYPE = "application/vnd.pieroot.palimpsest.layer.squashfs.v1"
EROFS_DERIVED_LAYER_MEDIA_TYPE = "application/vnd.pieroot.palimpsest.layer.erofs.v1"
SQUASHFS_ARTIFACT_BACKEND_ID = "dev.pieroot.palimpsest.artifact-backend.squashfs-v1"
EROFS_ARTIFACT_BACKEND_ID = "dev.pieroot.palimpsest.artifact-backend.erofs-v1"

SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES = frozenset({OCI_IMAGE_MANIFEST_MEDIA_TYPE, DOCKER_IMAGE_MANIFEST_MEDIA_TYPE})
SUPPORTED_IMAGE_INDEX_MEDIA_TYPES = frozenset({OCI_IMAGE_INDEX_MEDIA_TYPE, DOCKER_MANIFEST_LIST_MEDIA_TYPE})
SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES = frozenset({OCI_IMAGE_CONFIG_MEDIA_TYPE, DOCKER_IMAGE_CONFIG_MEDIA_TYPE})
SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES = frozenset(
    {OCI_LAYER_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE, DOCKER_LAYER_GZIP_MEDIA_TYPE}
)

OCI_EMPTY_CONFIG_BYTES = b"{}"
OCI_EMPTY_CONFIG_DIGEST = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
OCI_EMPTY_CONFIG_SIZE = 2

_PLATFORM_COMPONENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_REPOSITORY_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
_SQUASHFS_CONVERTER_RE = re.compile(r"^mksquashfs(?:-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)?@sha256:[0-9a-f]{64}$")
_EROFS_CONVERTER_RE = re.compile(r"^mkfs\.erofs(?:-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)?@sha256:[0-9a-f]{64}$")
_ARTIFACT_PROFILES = {
    "squashfs": (
        SQUASHFS_DERIVED_LAYER_MEDIA_TYPE,
        SQUASHFS_ARTIFACT_BACKEND_ID,
        _SQUASHFS_CONVERTER_RE,
    ),
    "erofs": (
        EROFS_DERIVED_LAYER_MEDIA_TYPE,
        EROFS_ARTIFACT_BACKEND_ID,
        _EROFS_CONVERTER_RE,
    ),
}
SUPPORTED_ARTIFACT_FILESYSTEMS = frozenset(_ARTIFACT_PROFILES)
_STANDARD_ANNOTATION_WHITELIST = frozenset(
    {
        "org.opencontainers.image.created",
        "org.opencontainers.image.authors",
        "org.opencontainers.image.url",
        "org.opencontainers.image.documentation",
        "org.opencontainers.image.source",
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.vendor",
        "org.opencontainers.image.licenses",
        "org.opencontainers.image.ref.name",
        "org.opencontainers.image.title",
        "org.opencontainers.image.description",
        "org.opencontainers.image.base.digest",
        "org.opencontainers.image.base.name",
    }
)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode *value* as deterministic compact UTF-8 JSON."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError("value is not canonical JSON data") from exc


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _canonical_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} must be a canonical sha256 digest")
    try:
        normalized = require_digest(value)
    except InvalidDigestError as exc:
        raise ArtifactValidationError(f"{field_name} must be a canonical sha256 digest") from exc
    if normalized != value:
        raise ArtifactValidationError(f"{field_name} must use canonical sha256:<lowercase-hex> syntax")
    return value


def _plain_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArtifactValidationError(f"{field_name} must be a nonempty string without surrounding whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ArtifactValidationError(f"{field_name} cannot contain control characters")
    return value


def _registry_authority(value: Any, field_name: str = "source.registry") -> str:
    authority = _plain_string(value, field_name)
    if any(character.isspace() for character in authority) or any(marker in authority for marker in ("/", "\\", "@")):
        raise ArtifactValidationError(f"{field_name} must be a registry authority without scheme, userinfo, or path")

    port_text: str | None = None
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            raise ArtifactValidationError(f"{field_name} has an invalid bracketed IPv6 host")
        host = authority[1:closing]
        remainder = authority[closing + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:]:
                raise ArtifactValidationError(f"{field_name} has an invalid port")
            port_text = remainder[1:]
        if "%" in host:
            raise ArtifactValidationError(f"{field_name} cannot contain an IPv6 scope identifier")
        try:
            address = ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            raise ArtifactValidationError(f"{field_name} has an invalid bracketed IPv6 host") from exc
        canonical_host = f"[{address.compressed.lower()}]"
    else:
        if authority.count(":") > 1:
            raise ArtifactValidationError(f"{field_name} requires brackets around an IPv6 host")
        if ":" in authority:
            host, port_text = authority.rsplit(":", 1)
        else:
            host = authority
        if not host:
            raise ArtifactValidationError(f"{field_name} has an empty host")
        host_parts = host.split(".")
        if len(host_parts) == 4 and all(part.isascii() and part.isdigit() for part in host_parts):
            try:
                address = ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError as exc:
                raise ArtifactValidationError(f"{field_name} has an invalid IPv4 host") from exc
            canonical_host = str(address)
        else:
            labels = host_parts
            if len(host) > 253 or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
                raise ArtifactValidationError(f"{field_name} has an invalid hostname")
            canonical_host = host.lower()

    canonical_port: str | None = None
    if port_text is not None:
        if not port_text.isascii() or not port_text.isdigit():
            raise ArtifactValidationError(f"{field_name} port must be numeric")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ArtifactValidationError(f"{field_name} port must be between 1 and 65535")
        canonical_port = str(port)
    return canonical_host if canonical_port is None else f"{canonical_host}:{canonical_port}"


def _repository_name(value: Any, field_name: str = "source.repository") -> str:
    repository = _plain_string(value, field_name)
    if any(character.isspace() for character in repository) or "\\" in repository:
        raise ArtifactValidationError(f"{field_name} must use Distribution repository syntax")
    components = repository.split("/")
    if any(_REPOSITORY_COMPONENT_RE.fullmatch(component) is None for component in components):
        raise ArtifactValidationError(f"{field_name} must use Distribution repository syntax")
    return repository


def _requested_reference(value: Any) -> str:
    field_name = "source.requested_reference"
    reference = _plain_string(value, field_name)
    if any(character.isspace() for character in reference) or "\\" in reference or "://" in reference:
        raise ArtifactValidationError(f"{field_name} must be a named OCI/Docker reference without a scheme")
    if reference.startswith(("/", "./", "../", "~")):
        raise ArtifactValidationError(f"{field_name} cannot be a local path")

    name_and_tag = reference
    digest_suffix = ""
    if "@" in reference:
        if reference.count("@") != 1:
            raise ArtifactValidationError(f"{field_name} contains invalid userinfo or digest syntax")
        name_and_tag, declared_digest = reference.rsplit("@", 1)
        _canonical_digest(declared_digest, f"{field_name}.digest")
        digest_suffix = f"@{declared_digest}"

    name = name_and_tag
    tag_suffix = ""
    last_slash = name_and_tag.rfind("/")
    last_colon = name_and_tag.rfind(":")
    if last_colon > last_slash:
        name, tag = name_and_tag[:last_colon], name_and_tag[last_colon + 1 :]
        if _TAG_RE.fullmatch(tag) is None:
            raise ArtifactValidationError(f"{field_name} has an invalid tag")
        tag_suffix = f":{tag}"
    if not name:
        raise ArtifactValidationError(f"{field_name} has an empty name")

    components = name.split("/")
    first = components[0]
    is_explicit_registry = len(components) > 1 and (
        first.lower() == "localhost" or "." in first or ":" in first or first.startswith("[")
    )
    if is_explicit_registry:
        canonical_registry = _registry_authority(first, f"{field_name}.registry")
        repository_components = components[1:]
        canonical_name = "/".join((canonical_registry, *repository_components))
    else:
        repository_components = components
        canonical_name = name
    _repository_name("/".join(repository_components), field_name)
    return canonical_name + tag_suffix + digest_suffix


def _exact_fields(data: Any, expected: set[str], field_name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ArtifactValidationError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in data):
        raise ArtifactValidationError(f"invalid field name in {field_name}: object keys must be strings")
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing!r}")
        if unknown:
            details.append(f"unknown={unknown!r}")
        raise ArtifactValidationError(f"invalid fields in {field_name}: {', '.join(details)}")
    return data


@dataclass(frozen=True, slots=True)
class Descriptor:
    """Strict immutable OCI content descriptor without mutable annotations."""

    media_type: str
    digest: str
    size: int

    def __post_init__(self) -> None:
        media_type = _plain_string(self.media_type, "descriptor.media_type")
        components = media_type.split("/")
        if (
            len(components) != 2
            or any(len(component) > 127 for component in components)
            or _MEDIA_TYPE_RE.fullmatch(media_type) is None
        ):
            raise ArtifactValidationError("descriptor.media_type must be a valid nonempty media type")
        object.__setattr__(self, "media_type", media_type.lower())
        object.__setattr__(self, "digest", _canonical_digest(self.digest, "descriptor.digest"))
        if type(self.size) is not int or not 0 <= self.size <= 2**63 - 1:
            raise ArtifactValidationError("descriptor.size must be an exact integer between 0 and 2**63-1")

    def to_dict(self) -> dict[str, Any]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": self.size}

    @classmethod
    def from_dict(cls, data: Any, field_name: str = "descriptor") -> Descriptor:
        value = _exact_fields(data, {"mediaType", "digest", "size"}, field_name)
        return cls(media_type=value["mediaType"], digest=value["digest"], size=value["size"])


EMPTY_CONFIG_DESCRIPTOR = Descriptor(
    media_type=OCI_EMPTY_CONFIG_MEDIA_TYPE,
    digest=OCI_EMPTY_CONFIG_DIGEST,
    size=OCI_EMPTY_CONFIG_SIZE,
)


@dataclass(frozen=True, slots=True)
class Platform:
    os: str
    architecture: str
    variant: str | None = None
    os_version: str | None = None
    os_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name, value in (("platform.os", self.os), ("platform.architecture", self.architecture)):
            component = _plain_string(value, field_name)
            if _PLATFORM_COMPONENT_RE.fullmatch(component) is None:
                raise ArtifactValidationError(f"{field_name} has invalid syntax")
        if self.os != "linux":
            raise ArtifactValidationError("platform.os must be 'linux' for Phase-1 materialization")
        if self.variant is not None:
            variant = _plain_string(self.variant, "platform.variant")
            if _PLATFORM_COMPONENT_RE.fullmatch(variant) is None:
                raise ArtifactValidationError("platform.variant has invalid syntax")
        if self.os_version is not None:
            _plain_string(self.os_version, "platform.os_version")
        if not isinstance(self.os_features, tuple):
            raise ArtifactValidationError("platform.os_features must be an immutable tuple")
        features: list[str] = []
        for feature in self.os_features:
            features.append(_plain_string(feature, "platform.os_features item"))
        object.__setattr__(self, "os_features", tuple(sorted(set(features))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "os": self.os,
            "os_features": list(self.os_features),
            "os_version": self.os_version,
            "variant": self.variant,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Platform:
        value = _exact_fields(
            data,
            {"architecture", "os", "os_features", "os_version", "variant"},
            "source.platform",
        )
        raw_features = value["os_features"]
        if not isinstance(raw_features, list):
            raise ArtifactValidationError("source.platform.os_features must be an array")
        return cls(
            os=value["os"],
            architecture=value["architecture"],
            variant=value["variant"],
            os_version=value["os_version"],
            os_features=tuple(raw_features),
        )


@dataclass(frozen=True, slots=True)
class ProvenanceSource:
    """Pinned source graph identities for one selected platform manifest."""

    registry: str
    repository: str
    requested_reference: str
    index_descriptor: Descriptor | None
    manifest_descriptor: Descriptor
    config_descriptor: Descriptor
    platform: Platform

    def __post_init__(self) -> None:
        object.__setattr__(self, "registry", _registry_authority(self.registry))
        _repository_name(self.repository)
        object.__setattr__(self, "requested_reference", _requested_reference(self.requested_reference))
        if not isinstance(self.manifest_descriptor, Descriptor):
            raise ArtifactValidationError("source.manifest_descriptor must be a Descriptor")
        if self.manifest_descriptor.media_type not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
            raise ArtifactValidationError("source.manifest_descriptor has an unsupported image manifest media type")
        if not isinstance(self.config_descriptor, Descriptor):
            raise ArtifactValidationError("source.config_descriptor must be a Descriptor")
        if self.config_descriptor.media_type not in SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES:
            raise ArtifactValidationError("source.config_descriptor has an unsupported image config media type")
        if self.index_descriptor is not None and not isinstance(self.index_descriptor, Descriptor):
            raise ArtifactValidationError("source.index_descriptor must be a Descriptor or None")
        if (
            self.index_descriptor is not None
            and self.index_descriptor.media_type not in SUPPORTED_IMAGE_INDEX_MEDIA_TYPES
        ):
            raise ArtifactValidationError("source.index_descriptor has an unsupported image index media type")
        if not isinstance(self.platform, Platform):
            raise ArtifactValidationError("source.platform must be a Platform")

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_descriptor": self.config_descriptor.to_dict(),
            "index_descriptor": self.index_descriptor.to_dict() if self.index_descriptor is not None else None,
            "manifest_descriptor": self.manifest_descriptor.to_dict(),
            "platform": self.platform.to_dict(),
            "registry": self.registry,
            "repository": self.repository,
            "requested_reference": self.requested_reference,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProvenanceSource:
        value = _exact_fields(
            data,
            {
                "config_descriptor",
                "index_descriptor",
                "manifest_descriptor",
                "platform",
                "registry",
                "repository",
                "requested_reference",
            },
            "source",
        )
        raw_index = value["index_descriptor"]
        return cls(
            registry=value["registry"],
            repository=value["repository"],
            requested_reference=value["requested_reference"],
            index_descriptor=None if raw_index is None else Descriptor.from_dict(raw_index, "source.index_descriptor"),
            manifest_descriptor=Descriptor.from_dict(value["manifest_descriptor"], "source.manifest_descriptor"),
            config_descriptor=Descriptor.from_dict(value["config_descriptor"], "source.config_descriptor"),
            platform=Platform.from_dict(value["platform"]),
        )


SourceProvenance = ProvenanceSource


@dataclass(frozen=True, slots=True)
class LayerOccurrence:
    """One ordered source layer occurrence and its derived artifact identity."""

    ordinal: int
    compressed: Descriptor
    diff_id: str
    derived: Descriptor
    filesystem: str
    backend: str
    converter: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ArtifactValidationError("occurrence.ordinal must be a nonnegative integer excluding bool")
        if not isinstance(self.compressed, Descriptor):
            raise ArtifactValidationError("occurrence.compressed must be a Descriptor")
        if self.compressed.media_type not in SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES:
            raise ArtifactValidationError("occurrence.compressed has an unsupported compressed layer media type")
        if not isinstance(self.derived, Descriptor):
            raise ArtifactValidationError("occurrence.derived must be a Descriptor")
        object.__setattr__(self, "diff_id", _canonical_digest(self.diff_id, "occurrence.diff_id"))
        if not isinstance(self.filesystem, str) or self.filesystem not in _ARTIFACT_PROFILES:
            raise ArtifactValidationError("occurrence artifact profile has an unsupported filesystem")
        expected_media_type, expected_backend, converter_pattern = _ARTIFACT_PROFILES[self.filesystem]
        if self.derived.media_type != expected_media_type:
            raise ArtifactValidationError("occurrence artifact profile has a contradictory derived media type")
        if self.backend != expected_backend:
            raise ArtifactValidationError("occurrence artifact profile has a contradictory backend identifier")
        if not isinstance(self.converter, str) or converter_pattern.fullmatch(self.converter) is None:
            raise ArtifactValidationError("occurrence artifact profile has an incompatible converter fingerprint")

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "compressed": self.compressed.to_dict(),
            "converter": self.converter,
            "derived": self.derived.to_dict(),
            "diff_id": self.diff_id,
            "filesystem": self.filesystem,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_dict(cls, data: Any, index: int) -> LayerOccurrence:
        value = _exact_fields(
            data,
            {"backend", "compressed", "converter", "derived", "diff_id", "filesystem", "ordinal"},
            f"occurrences[{index}]",
        )
        return cls(
            ordinal=value["ordinal"],
            compressed=Descriptor.from_dict(value["compressed"], f"occurrences[{index}].compressed"),
            diff_id=value["diff_id"],
            derived=Descriptor.from_dict(value["derived"], f"occurrences[{index}].derived"),
            filesystem=value["filesystem"],
            backend=value["backend"],
            converter=value["converter"],
        )


def compute_chain_id(occurrences: tuple[LayerOccurrence, ...]) -> str:
    """Hash the ordered occurrence identity tuples with explicit domain separation."""
    if not isinstance(occurrences, tuple) or any(not isinstance(item, LayerOccurrence) for item in occurrences):
        raise ArtifactValidationError("occurrences must be a tuple of LayerOccurrence values")
    tuples = [[item.ordinal, item.compressed.digest, item.diff_id, item.derived.digest] for item in occurrences]
    return _sha256(canonical_json_bytes({"domain": "palimpsest.oci.chain.v1", "occurrences": tuples}))


@dataclass(frozen=True, slots=True)
class SourceDerivedProvenance:
    """Canonical content provenance; mutable VM/run identities are out of scope."""

    source: ProvenanceSource
    occurrences: tuple[LayerOccurrence, ...]
    runtime_index: Descriptor
    schema_version: int = PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PROVENANCE_SCHEMA_VERSION:
            raise ArtifactValidationError(f"unsupported provenance schema_version: {self.schema_version!r}")
        if not isinstance(self.source, ProvenanceSource):
            raise ArtifactValidationError("source must be a ProvenanceSource")
        if not isinstance(self.occurrences, tuple):
            raise ArtifactValidationError("occurrences must be an immutable tuple")
        if any(not isinstance(item, LayerOccurrence) for item in self.occurrences):
            raise ArtifactValidationError("occurrences must contain only LayerOccurrence values")
        ordinals = tuple(item.ordinal for item in self.occurrences)
        expected = tuple(range(len(self.occurrences)))
        if ordinals != expected:
            raise ArtifactValidationError(
                f"occurrence ordinals must be contiguous, zero-based, and ordered: expected {expected!r}, got {ordinals!r}"
            )
        if not isinstance(self.runtime_index, Descriptor):
            raise ArtifactValidationError("runtime_index must be a caller-provided Descriptor")
        if self.runtime_index.media_type != RUNTIME_INDEX_MEDIA_TYPE:
            raise ArtifactValidationError(f"runtime_index media type must be {RUNTIME_INDEX_MEDIA_TYPE!r}")

    @property
    def chain_id(self) -> str:
        return compute_chain_id(self.occurrences)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "occurrences": [item.to_dict() for item in self.occurrences],
            "runtime_index": self.runtime_index.to_dict(),
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return _sha256(self.to_json_bytes())

    @classmethod
    def from_dict(cls, data: Any) -> SourceDerivedProvenance:
        value = _exact_fields(
            data,
            {"chain_id", "occurrences", "runtime_index", "schema_version", "source"},
            "provenance",
        )
        raw_occurrences = value["occurrences"]
        if not isinstance(raw_occurrences, list):
            raise ArtifactValidationError("occurrences must be an array")
        provenance = cls(
            source=ProvenanceSource.from_dict(value["source"]),
            occurrences=tuple(LayerOccurrence.from_dict(item, index) for index, item in enumerate(raw_occurrences)),
            runtime_index=Descriptor.from_dict(value["runtime_index"], "runtime_index"),
            schema_version=value["schema_version"],
        )
        declared_chain_id = _canonical_digest(value["chain_id"], "chain_id")
        if declared_chain_id != provenance.chain_id:
            raise ArtifactValidationError(f"chain_id mismatch: expected {provenance.chain_id}, got {declared_chain_id}")
        return provenance

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> SourceDerivedProvenance:
        data = _load_strict_json(payload, "provenance")
        provenance = cls.from_dict(data)
        if payload != provenance.to_json_bytes():
            raise ArtifactValidationError("provenance JSON must use the canonical UTF-8 encoding")
        return provenance


Provenance = SourceDerivedProvenance


def provenance_digest(provenance: SourceDerivedProvenance) -> str:
    if not isinstance(provenance, SourceDerivedProvenance):
        raise ArtifactValidationError("provenance must be SourceDerivedProvenance")
    return provenance.digest


def parse_provenance(payload: bytes) -> SourceDerivedProvenance:
    return SourceDerivedProvenance.from_json_bytes(payload)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ArtifactValidationError(f"non-finite JSON number is forbidden: {value}")


def _load_strict_json(payload: bytes, field_name: str) -> Any:
    if not isinstance(payload, bytes):
        raise ArtifactValidationError(f"{field_name} JSON must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError(f"{field_name} JSON must be UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except ArtifactValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"{field_name} JSON is invalid") from exc


@dataclass(frozen=True, slots=True)
class ProvenanceReferrerManifest:
    """Consumed OCI 1.1 referrer with lossless string annotations."""

    subject: Descriptor
    provenance_blob: Descriptor
    annotations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.subject, Descriptor):
            raise ArtifactValidationError("referrer subject must be a Descriptor")
        if self.subject.media_type not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
            raise ArtifactValidationError("referrer subject has an unsupported image manifest media type")
        if not isinstance(self.provenance_blob, Descriptor):
            raise ArtifactValidationError("referrer provenance blob must be a Descriptor")
        if self.provenance_blob.media_type != PROVENANCE_BLOB_MEDIA_TYPE:
            raise ArtifactValidationError("referrer provenance blob has the wrong media type")
        if not isinstance(self.annotations, tuple):
            raise ArtifactValidationError("referrer annotations must be an immutable tuple")
        seen: set[str] = set()
        previous: str | None = None
        for item in self.annotations:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ArtifactValidationError("referrer annotations must contain key/value tuples")
            key, value = item
            if not isinstance(key, str) or not isinstance(value, str):
                raise ArtifactValidationError("referrer annotation keys and values must be JSON strings")
            if key in seen:
                raise ArtifactValidationError(f"duplicate referrer annotation: {key!r}")
            if previous is not None and key < previous:
                raise ArtifactValidationError("referrer annotations must be sorted by key")
            seen.add(key)
            previous = key

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "artifactType": PROVENANCE_ARTIFACT_TYPE,
            "config": EMPTY_CONFIG_DESCRIPTOR.to_dict(),
            "layers": [self.provenance_blob.to_dict()],
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
            "subject": self.subject.to_dict(),
        }
        if self.annotations:
            result["annotations"] = dict(self.annotations)
        return result

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def _consumer_annotation_items(annotations: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if annotations is None:
        return ()
    if not isinstance(annotations, Mapping):
        raise ArtifactValidationError("referrer annotations must be a mapping")
    items: list[tuple[str, str]] = []
    for key, value in annotations.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ArtifactValidationError("referrer annotation keys and values must be JSON strings")
        items.append((key, value))
    return tuple(sorted(items))


def _producer_annotation_items(annotations: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    items = _consumer_annotation_items(annotations)
    for key, _value in items:
        if key in _STANDARD_ANNOTATION_WHITELIST:
            continue
        if key.startswith("org.opencontainers."):
            raise ArtifactValidationError(f"referrer annotation key uses a reserved OCI namespace: {key!r}")
    return items


def build_provenance_referrer_manifest(
    provenance: SourceDerivedProvenance,
    *,
    include_digest_annotation: bool = True,
    annotations: Mapping[str, str] | None = None,
) -> bytes:
    """Build a canonical OCI 1.1 referrer using approved producer annotation keys."""
    if not isinstance(provenance, SourceDerivedProvenance):
        raise ArtifactValidationError("provenance must be SourceDerivedProvenance")
    if not isinstance(include_digest_annotation, bool):
        raise ArtifactValidationError("include_digest_annotation must be a boolean")
    merged = dict(_producer_annotation_items(annotations))
    existing = merged.get(PROVENANCE_DIGEST_ANNOTATION)
    if existing is not None and existing != provenance.digest:
        raise ArtifactValidationError("provenance digest annotation conflicts with the provenance blob")
    if include_digest_annotation:
        merged[PROVENANCE_DIGEST_ANNOTATION] = provenance.digest
    blob = Descriptor(
        media_type=PROVENANCE_BLOB_MEDIA_TYPE,
        digest=provenance.digest,
        size=len(provenance.to_json_bytes()),
    )
    return ProvenanceReferrerManifest(
        subject=provenance.source.manifest_descriptor,
        provenance_blob=blob,
        annotations=tuple(sorted(merged.items())),
    ).to_json_bytes()


build_referrer_manifest = build_provenance_referrer_manifest


def parse_provenance_referrer_manifest(
    manifest_payload: bytes,
    provenance_payload: bytes,
    *,
    expected_subject: Descriptor | None = None,
) -> ProvenanceReferrerManifest:
    """Validate exact referrer/blob identities while preserving all string annotations."""
    provenance = SourceDerivedProvenance.from_json_bytes(provenance_payload)
    subject = provenance.source.manifest_descriptor if expected_subject is None else expected_subject
    if not isinstance(subject, Descriptor):
        raise ArtifactValidationError("expected_subject must be a Descriptor")
    data = _load_strict_json(manifest_payload, "referrer manifest")
    if not isinstance(data, dict):
        raise ArtifactValidationError("referrer manifest must be an object")
    base_fields = {"artifactType", "config", "layers", "mediaType", "schemaVersion", "subject"}
    allowed_fields = base_fields | {"annotations"}
    actual_fields = set(data)
    if not base_fields.issubset(actual_fields) or not actual_fields.issubset(allowed_fields):
        missing = sorted(base_fields - actual_fields)
        unknown = sorted(actual_fields - allowed_fields)
        raise ArtifactValidationError(f"invalid fields in referrer manifest: missing={missing!r}, unknown={unknown!r}")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 2:
        raise ArtifactValidationError("referrer manifest schemaVersion must be 2")
    if data["mediaType"] != OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        raise ArtifactValidationError("referrer manifest has the wrong mediaType")
    if data["artifactType"] != PROVENANCE_ARTIFACT_TYPE:
        raise ArtifactValidationError("referrer manifest has the wrong artifactType")
    config = Descriptor.from_dict(data["config"], "referrer config")
    if config != EMPTY_CONFIG_DESCRIPTOR:
        raise ArtifactValidationError("referrer config must be the exact OCI empty JSON descriptor")
    layers = data["layers"]
    if not isinstance(layers, list) or len(layers) != 1:
        raise ArtifactValidationError("referrer manifest must contain exactly one provenance layer")
    provenance_blob = Descriptor.from_dict(layers[0], "referrer layers[0]")
    expected_blob = Descriptor(
        media_type=PROVENANCE_BLOB_MEDIA_TYPE,
        digest=_sha256(provenance_payload),
        size=len(provenance_payload),
    )
    if provenance_blob != expected_blob:
        raise ArtifactValidationError("referrer provenance descriptor does not match the exact provenance bytes")
    parsed_subject = Descriptor.from_dict(data["subject"], "referrer subject")
    if parsed_subject != subject or parsed_subject != provenance.source.manifest_descriptor:
        raise ArtifactValidationError("referrer subject does not exactly match the selected platform manifest")
    raw_annotations = data.get("annotations", {})
    if not isinstance(raw_annotations, dict):
        raise ArtifactValidationError("referrer annotations must be an object")
    annotation_items = _consumer_annotation_items(raw_annotations)
    declared_digest = raw_annotations.get(PROVENANCE_DIGEST_ANNOTATION)
    if declared_digest is not None and declared_digest != provenance.digest:
        raise ArtifactValidationError("referrer provenance digest annotation is incorrect")
    manifest = ProvenanceReferrerManifest(
        subject=parsed_subject,
        provenance_blob=provenance_blob,
        annotations=annotation_items,
    )
    if manifest_payload != manifest.to_json_bytes():
        raise ArtifactValidationError("referrer manifest JSON must use the canonical UTF-8 encoding")
    return manifest


parse_referrer_manifest = parse_provenance_referrer_manifest


__all__ = [
    "DOCKER_IMAGE_CONFIG_MEDIA_TYPE",
    "DOCKER_IMAGE_MANIFEST_MEDIA_TYPE",
    "DOCKER_LAYER_GZIP_MEDIA_TYPE",
    "DOCKER_MANIFEST_LIST_MEDIA_TYPE",
    "EMPTY_CONFIG_DESCRIPTOR",
    "EROFS_ARTIFACT_BACKEND_ID",
    "EROFS_DERIVED_LAYER_MEDIA_TYPE",
    "LayerOccurrence",
    "OCI_EMPTY_CONFIG_BYTES",
    "OCI_EMPTY_CONFIG_DIGEST",
    "OCI_EMPTY_CONFIG_MEDIA_TYPE",
    "OCI_EMPTY_CONFIG_SIZE",
    "OCI_IMAGE_CONFIG_MEDIA_TYPE",
    "OCI_IMAGE_INDEX_MEDIA_TYPE",
    "OCI_IMAGE_MANIFEST_MEDIA_TYPE",
    "OCI_LAYER_GZIP_MEDIA_TYPE",
    "OCI_LAYER_MEDIA_TYPE",
    "PROVENANCE_ARTIFACT_TYPE",
    "PROVENANCE_BLOB_MEDIA_TYPE",
    "PROVENANCE_DIGEST_ANNOTATION",
    "PROVENANCE_SCHEMA_VERSION",
    "RUNTIME_INDEX_MEDIA_TYPE",
    "SQUASHFS_ARTIFACT_BACKEND_ID",
    "SQUASHFS_DERIVED_LAYER_MEDIA_TYPE",
    "SUPPORTED_ARTIFACT_FILESYSTEMS",
    "SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES",
    "SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES",
    "SUPPORTED_IMAGE_INDEX_MEDIA_TYPES",
    "SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES",
    "Descriptor",
    "Platform",
    "Provenance",
    "ProvenanceReferrerManifest",
    "ProvenanceSource",
    "SourceDerivedProvenance",
    "SourceProvenance",
    "build_provenance_referrer_manifest",
    "build_referrer_manifest",
    "canonical_json_bytes",
    "compute_chain_id",
    "parse_provenance",
    "parse_provenance_referrer_manifest",
    "parse_referrer_manifest",
    "provenance_digest",
]
