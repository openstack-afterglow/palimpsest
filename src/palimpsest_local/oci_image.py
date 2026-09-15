"""Pure validation and selection for pinned OCI image graphs.

This module deliberately has no filesystem, registry, decompression, or VM
integration.  A caller supplies immutable bytes for descriptors; the resolver
verifies those bytes and returns only source-side identities.  Local layout
snapshotting is a separate trust boundary in :mod:`palimpsest_local.oci_source`.

Phase 1 intentionally supports one coherent OCI or Docker-v2 wire profile,
exactly ``linux/amd64``, and 1..128 distributable tar/gzip layers.  Foreign,
nondistributable, zstd, embedded-descriptor-data, and mixed-profile graphs are
rejected.  Unknown JSON extension fields are ignored for forward compatibility.
Layer payloads are never opened here, so the reserved-root policy carried in
the receipt is a requirement for the later converter rather than a claim that
archive member paths have already been inspected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ArtifactValidationError
from .oci_process import OCIProcessSpec
from .oci_provenance import (
    DOCKER_IMAGE_CONFIG_MEDIA_TYPE,
    DOCKER_IMAGE_MANIFEST_MEDIA_TYPE,
    DOCKER_LAYER_GZIP_MEDIA_TYPE,
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES,
    SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES,
    SUPPORTED_IMAGE_INDEX_MEDIA_TYPES,
    SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES,
    Descriptor,
    Platform,
    ProvenanceSource,
    _canonical_digest,
    _registry_authority,
    _repository_name,
    _requested_reference,
    canonical_json_bytes,
)

MAX_IMAGE_JSON_BYTES = 4 * 1024 * 1024
MAX_IMAGE_LAYERS = 128
RESERVED_PATH_POLICY_ID = "dev.pieroot.palimpsest.reserved-root.v1"
RESERVED_PATH_ERROR_CATEGORY = "oci-reserved-path"

BlobReader = Callable[[Descriptor], bytes]


def _strict_json(payload: bytes, field_name: str) -> dict[str, Any]:
    if len(payload) > MAX_IMAGE_JSON_BYTES:
        raise ArtifactValidationError(f"{field_name} exceeds the {MAX_IMAGE_JSON_BYTES}-byte JSON limit")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ArtifactValidationError(f"{field_name} contains duplicate object key {key!r}")
            value[key] = item
        return value

    def reject_nonfinite_constant(value: str) -> None:
        raise ArtifactValidationError(f"{field_name} contains non-JSON numeric constant {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_constant,
        )
    except ArtifactValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"{field_name} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{field_name} must be a JSON object")
    return value


def strict_json_object(payload: bytes, field_name: str) -> dict[str, Any]:
    """Parse one bounded caller-supplied object with the OCI JSON rules."""
    return _strict_json(payload, field_name)


def verify_blob_chunks(*, expected_digest: str, expected_size: int, chunks: Iterable[bytes]) -> None:
    """Stream-verify one canonical OCI digest/size pair without retaining bytes."""
    digest = _canonical_digest(expected_digest, "blob expected_digest")
    if type(expected_size) is not int or not 0 <= expected_size <= 2**63 - 1:
        raise ArtifactValidationError("blob expected_size must be an exact integer between 0 and 2**63-1")

    hasher = hashlib.sha256()
    total = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise ArtifactValidationError("blob chunks must contain immutable bytes")
        if not chunk:
            raise ArtifactValidationError("blob chunks must be nonempty")
        total += len(chunk)
        if total > expected_size:
            raise ArtifactValidationError(f"OCI descriptor size mismatch for {digest}")
        hasher.update(chunk)
    if total != expected_size:
        raise ArtifactValidationError(f"OCI descriptor size mismatch for {digest}")
    if f"sha256:{hasher.hexdigest()}" != digest:
        raise ArtifactValidationError(f"OCI descriptor digest mismatch for {digest}")


def verify_descriptor_bytes(descriptor: Descriptor, payload: bytes) -> bytes:
    """Verify that *payload* is exactly the content named by *descriptor*."""
    if not isinstance(descriptor, Descriptor):
        raise ArtifactValidationError("blob descriptor must be a Descriptor")
    if not isinstance(payload, bytes):
        raise ArtifactValidationError("blob reader must return immutable bytes")
    verify_blob_chunks(
        expected_digest=descriptor.digest,
        expected_size=descriptor.size,
        chunks=(payload,) if payload else (),
    )
    return payload


def read_verified_blob(reader: BlobReader, descriptor: Descriptor) -> bytes:
    try:
        payload = reader(descriptor)
    except ArtifactValidationError:
        raise
    except (KeyError, FileNotFoundError) as exc:
        raise ArtifactValidationError(f"OCI image is missing referenced blob {descriptor.digest}") from exc
    return verify_descriptor_bytes(descriptor, payload)


def _read_json_blob(reader: BlobReader, descriptor: Descriptor, field_name: str) -> dict[str, Any]:
    if descriptor.size > MAX_IMAGE_JSON_BYTES:
        raise ArtifactValidationError(f"{field_name} exceeds the {MAX_IMAGE_JSON_BYTES}-byte JSON limit")
    return _strict_json(read_verified_blob(reader, descriptor), field_name)


@dataclass(frozen=True, slots=True)
class OCIImageRef:
    """Canonical named source identity, separate from its transport location."""

    registry: str
    repository: str
    requested_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "registry", _registry_authority(self.registry))
        _repository_name(self.repository)
        object.__setattr__(self, "requested_reference", _requested_reference(self.requested_reference))

    def to_dict(self) -> dict[str, str]:
        return {
            "registry": self.registry,
            "repository": self.repository,
            "requested_reference": self.requested_reference,
        }


@dataclass(frozen=True, slots=True)
class OCIConfig:
    """The platform and ordered DiffIDs proven by an OCI image config."""

    descriptor: Descriptor
    os: str
    architecture: str
    rootfs_type: str
    diff_ids: tuple[str, ...]
    process: OCIProcessSpec

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, Descriptor):
            raise ArtifactValidationError("image config descriptor must be a Descriptor")
        if self.descriptor.media_type not in SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES:
            raise ArtifactValidationError("image config descriptor has an unsupported media type")
        if self.os != "linux" or self.architecture != "amd64":
            raise ArtifactValidationError("Phase-1 image config must target exactly linux/amd64")
        if self.rootfs_type != "layers":
            raise ArtifactValidationError("image config rootfs.type must be 'layers'")
        if not isinstance(self.diff_ids, tuple):
            raise ArtifactValidationError("image config rootfs.diff_ids must be an immutable tuple")
        for index, diff_id in enumerate(self.diff_ids):
            _canonical_digest(diff_id, f"image config rootfs.diff_ids[{index}]")
        if not isinstance(self.process, OCIProcessSpec):
            raise ArtifactValidationError("image config process must be an OCIProcessSpec")

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "descriptor": self.descriptor.to_dict(),
            "os": self.os,
            "process": self.process.to_dict(),
            "rootfs": {"diff_ids": list(self.diff_ids), "type": self.rootfs_type},
        }


@dataclass(frozen=True, slots=True)
class OCILayerSource:
    """One ordered source-layer occurrence; repeated descriptors are preserved."""

    ordinal: int
    compressed: Descriptor
    diff_id: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ArtifactValidationError("source layer ordinal must be a nonnegative integer")
        if not isinstance(self.compressed, Descriptor):
            raise ArtifactValidationError("source layer compressed descriptor must be a Descriptor")
        if self.compressed.media_type not in SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES:
            raise ArtifactValidationError("source layer has an unsupported compressed media type")
        object.__setattr__(self, "diff_id", _canonical_digest(self.diff_id, "source layer diff_id"))

    def to_dict(self) -> dict[str, Any]:
        return {"compressed": self.compressed.to_dict(), "diff_id": self.diff_id, "ordinal": self.ordinal}


@dataclass(frozen=True, slots=True)
class OCIImage:
    """A selected, source-only OCI image receipt."""

    reference: OCIImageRef
    index_descriptor: Descriptor | None
    manifest_descriptor: Descriptor
    config: OCIConfig
    platform: Platform
    layers: tuple[OCILayerSource, ...]
    reserved_path_policy: str = RESERVED_PATH_POLICY_ID

    def __post_init__(self) -> None:
        if not isinstance(self.reference, OCIImageRef):
            raise ArtifactValidationError("image reference must be an OCIImageRef")
        if not isinstance(self.manifest_descriptor, Descriptor):
            raise ArtifactValidationError("image manifest descriptor must be a Descriptor")
        if (
            self.index_descriptor is not None
            and self.index_descriptor.media_type not in SUPPORTED_IMAGE_INDEX_MEDIA_TYPES
        ):
            raise ArtifactValidationError("image index descriptor has an unsupported media type")
        if self.manifest_descriptor.media_type not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
            raise ArtifactValidationError("image manifest descriptor has an unsupported media type")
        if not isinstance(self.config, OCIConfig):
            raise ArtifactValidationError("image config must be an OCIConfig")
        if self.platform != Platform(os="linux", architecture="amd64"):
            raise ArtifactValidationError("Phase-1 image platform must be exactly linux/amd64")
        if not isinstance(self.layers, tuple) or not 1 <= len(self.layers) <= MAX_IMAGE_LAYERS:
            raise ArtifactValidationError(f"image must contain between 1 and {MAX_IMAGE_LAYERS} layers")
        if any(not isinstance(layer, OCILayerSource) for layer in self.layers):
            raise ArtifactValidationError("image layers must contain only OCILayerSource values")
        if tuple(layer.ordinal for layer in self.layers) != tuple(range(len(self.layers))):
            raise ArtifactValidationError("source layer ordinals must be contiguous, ordered, and zero-based")
        if tuple(layer.diff_id for layer in self.layers) != self.config.diff_ids:
            raise ArtifactValidationError("source layer DiffIDs must match the image config order")
        if self.reserved_path_policy != RESERVED_PATH_POLICY_ID:
            raise ArtifactValidationError("image uses an unsupported reserved-path policy")

    @property
    def source(self) -> ProvenanceSource:
        return ProvenanceSource(
            registry=self.reference.registry,
            repository=self.reference.repository,
            requested_reference=self.reference.requested_reference,
            index_descriptor=self.index_descriptor,
            manifest_descriptor=self.manifest_descriptor,
            config_descriptor=self.config.descriptor,
            platform=self.platform,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "index_descriptor": self.index_descriptor.to_dict() if self.index_descriptor is not None else None,
            "layers": [layer.to_dict() for layer in self.layers],
            "manifest_descriptor": self.manifest_descriptor.to_dict(),
            "platform": self.platform.to_dict(),
            "reference": self.reference.to_dict(),
            "reserved_path_policy": self.reserved_path_policy,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class _IndexEntry:
    descriptor: Descriptor
    platform: _WirePlatform | None


@dataclass(frozen=True, slots=True)
class _WirePlatform:
    os: str
    architecture: str
    variant: str | None
    os_version: str | None
    os_features: tuple[str, ...]


def _wire_descriptor(value: Any, field_name: str, *, platform_allowed: bool) -> _IndexEntry:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ArtifactValidationError(f"{field_name} must be an OCI descriptor object")
    try:
        descriptor = Descriptor(
            media_type=value["mediaType"],
            digest=value["digest"],
            size=value["size"],
        )
    except KeyError as exc:
        raise ArtifactValidationError(f"{field_name} is missing {exc.args[0]!r}") from exc
    urls = value.get("urls")
    if urls is not None and (not isinstance(urls, list) or any(not isinstance(url, str) for url in urls)):
        raise ArtifactValidationError(f"{field_name}.urls must be an array of strings")
    annotations = value.get("annotations")
    if annotations is not None and (
        not isinstance(annotations, dict)
        or any(not isinstance(key, str) or not isinstance(item, str) for key, item in annotations.items())
    ):
        raise ArtifactValidationError(f"{field_name}.annotations must be a string map")
    if "artifactType" in value and not isinstance(value["artifactType"], str):
        raise ArtifactValidationError(f"{field_name}.artifactType must be a string")
    if "data" in value:
        raise ArtifactValidationError(f"{field_name} uses unsupported embedded descriptor data")
    raw_platform = value.get("platform")
    if raw_platform is not None and not platform_allowed:
        raise ArtifactValidationError(f"{field_name} cannot contain a platform")
    platform = _wire_platform(raw_platform, f"{field_name}.platform") if raw_platform is not None else None
    return _IndexEntry(descriptor=descriptor, platform=platform)


def _wire_platform(value: Any, field_name: str) -> _WirePlatform:
    if not isinstance(value, dict):
        raise ArtifactValidationError(f"{field_name} must be an object")
    if "architecture" not in value or "os" not in value:
        raise ArtifactValidationError(f"{field_name} is malformed")
    features = value.get("os.features", [])
    if not isinstance(features, list) or any(not isinstance(item, str) for item in features):
        raise ArtifactValidationError(f"{field_name}.os.features must be an array of strings")
    os_name = value["os"]
    architecture = value["architecture"]
    variant = value.get("variant")
    os_version = value.get("os.version")
    if not isinstance(os_name, str) or not os_name or not isinstance(architecture, str) or not architecture:
        raise ArtifactValidationError(f"{field_name} os and architecture must be nonempty strings")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ArtifactValidationError(f"{field_name}.variant must be a nonempty string")
    if os_version is not None and (not isinstance(os_version, str) or not os_version):
        raise ArtifactValidationError(f"{field_name}.os.version must be a nonempty string")
    return _WirePlatform(
        os=os_name,
        architecture=architecture,
        variant=value.get("variant"),
        os_version=os_version,
        os_features=tuple(features),
    )


def _exact_phase1_platform(platform: _WirePlatform | None) -> bool:
    return platform == _WirePlatform(os="linux", architecture="amd64", variant=None, os_version=None, os_features=())


def _require_document_media_type(document: Mapping[str, Any], descriptor: Descriptor, field_name: str) -> None:
    if document.get("mediaType") != descriptor.media_type:
        raise ArtifactValidationError(f"{field_name} mediaType does not match its descriptor")


def _parse_config(reader: BlobReader, descriptor: Descriptor) -> OCIConfig:
    if descriptor.media_type not in SUPPORTED_IMAGE_CONFIG_MEDIA_TYPES:
        raise ArtifactValidationError("image config descriptor has an unsupported media type")
    document = _read_json_blob(reader, descriptor, f"OCI image config {descriptor.digest}")
    rootfs = document.get("rootfs")
    if not isinstance(rootfs, dict) or "type" not in rootfs or "diff_ids" not in rootfs:
        raise ArtifactValidationError("image config rootfs must contain type and diff_ids")
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list):
        raise ArtifactValidationError("image config rootfs.diff_ids must be an array")
    return OCIConfig(
        descriptor=descriptor,
        os=document.get("os"),
        architecture=document.get("architecture"),
        rootfs_type=rootfs.get("type"),
        diff_ids=tuple(diff_ids),
        process=OCIProcessSpec.from_config(document.get("config")),
    )


def _resolve_manifest(
    reference: OCIImageRef,
    reader: BlobReader,
    manifest_descriptor: Descriptor,
    *,
    index_descriptor: Descriptor | None,
) -> OCIImage:
    if manifest_descriptor.media_type not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
        raise ArtifactValidationError("selected descriptor is not a supported OCI/Docker v2 image manifest")
    document = _read_json_blob(reader, manifest_descriptor, f"OCI image manifest {manifest_descriptor.digest}")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
        raise ArtifactValidationError("OCI image manifest must use schemaVersion 2")
    _require_document_media_type(document, manifest_descriptor, "OCI image manifest")
    config_entry = _wire_descriptor(document.get("config"), "manifest.config", platform_allowed=False)
    if manifest_descriptor.media_type == OCI_IMAGE_MANIFEST_MEDIA_TYPE:
        expected_config_type = OCI_IMAGE_CONFIG_MEDIA_TYPE
        expected_layer_types = {OCI_LAYER_MEDIA_TYPE, OCI_LAYER_GZIP_MEDIA_TYPE}
    else:
        expected_config_type = DOCKER_IMAGE_CONFIG_MEDIA_TYPE
        expected_layer_types = {DOCKER_LAYER_GZIP_MEDIA_TYPE}
    if config_entry.descriptor.media_type != expected_config_type:
        raise ArtifactValidationError("image manifest and config media types belong to different wire profiles")
    raw_layers = document.get("layers")
    if not isinstance(raw_layers, list):
        raise ArtifactValidationError("OCI image manifest layers must be an array")
    if not 1 <= len(raw_layers) <= MAX_IMAGE_LAYERS:
        raise ArtifactValidationError(f"OCI image manifest must contain between 1 and {MAX_IMAGE_LAYERS} layers")
    compressed: list[Descriptor] = []
    for index, raw_layer in enumerate(raw_layers):
        layer = _wire_descriptor(raw_layer, f"manifest.layers[{index}]", platform_allowed=False).descriptor
        if layer.media_type not in SUPPORTED_COMPRESSED_LAYER_MEDIA_TYPES:
            raise ArtifactValidationError(f"manifest.layers[{index}] has an unsupported layer media type")
        if layer.media_type not in expected_layer_types:
            raise ArtifactValidationError(f"manifest.layers[{index}] belongs to a different wire profile")
        compressed.append(layer)
    config = _parse_config(reader, config_entry.descriptor)
    if len(config.diff_ids) != len(compressed):
        raise ArtifactValidationError("image config DiffID count does not match manifest layer count")
    layers = tuple(
        OCILayerSource(ordinal=index, compressed=layer, diff_id=config.diff_ids[index])
        for index, layer in enumerate(compressed)
    )
    return OCIImage(
        reference=reference,
        index_descriptor=index_descriptor,
        manifest_descriptor=manifest_descriptor,
        config=config,
        platform=Platform(os="linux", architecture="amd64"),
        layers=layers,
    )


def resolve_image(reference: OCIImageRef, root_descriptor: Descriptor, reader: BlobReader) -> OCIImage:
    """Resolve a pinned manifest or index to exactly one ``linux/amd64`` image.

    Layer blobs are intentionally not read or decompressed here.  Their
    descriptors and ordered DiffIDs are validated; snapshotting and conversion
    happen at later trust boundaries.
    """
    if not isinstance(reference, OCIImageRef):
        raise ArtifactValidationError("reference must be an OCIImageRef")
    if not isinstance(root_descriptor, Descriptor):
        raise ArtifactValidationError("root descriptor must be a Descriptor")
    if "@" in reference.requested_reference:
        requested_digest = reference.requested_reference.rsplit("@", 1)[1]
        if requested_digest != root_descriptor.digest:
            raise ArtifactValidationError("requested reference digest does not match the root descriptor")
    if root_descriptor.media_type in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
        return _resolve_manifest(reference, reader, root_descriptor, index_descriptor=None)
    if root_descriptor.media_type not in SUPPORTED_IMAGE_INDEX_MEDIA_TYPES:
        raise ArtifactValidationError("root descriptor is not a supported OCI/Docker v2 image manifest or index")

    document = _read_json_blob(reader, root_descriptor, f"OCI image index {root_descriptor.digest}")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
        raise ArtifactValidationError("OCI image index must use schemaVersion 2")
    _require_document_media_type(document, root_descriptor, "OCI image index")
    manifests = document.get("manifests")
    if not isinstance(manifests, list):
        raise ArtifactValidationError("OCI image index manifests must be an array")
    matches: list[Descriptor] = []
    for index, raw_manifest in enumerate(manifests):
        entry = _wire_descriptor(raw_manifest, f"index.manifests[{index}]", platform_allowed=True)
        if _exact_phase1_platform(entry.platform):
            if entry.descriptor.media_type not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
                raise ArtifactValidationError("linux/amd64 index entry is not a supported image manifest")
            expected_manifest_type = (
                OCI_IMAGE_MANIFEST_MEDIA_TYPE
                if root_descriptor.media_type == OCI_IMAGE_INDEX_MEDIA_TYPE
                else DOCKER_IMAGE_MANIFEST_MEDIA_TYPE
            )
            if entry.descriptor.media_type != expected_manifest_type:
                raise ArtifactValidationError("image index and manifest media types belong to different wire profiles")
            matches.append(entry.descriptor)
    if not matches:
        raise ArtifactValidationError("OCI image index has no exact linux/amd64 manifest")
    if len(matches) != 1:
        raise ArtifactValidationError("OCI image index has ambiguous linux/amd64 manifests")
    return _resolve_manifest(reference, reader, matches[0], index_descriptor=root_descriptor)
