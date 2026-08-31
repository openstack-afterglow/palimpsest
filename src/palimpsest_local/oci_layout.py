"""Strict local content-addressed store and OCI layout verification/extraction."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .artifact_store import ArtifactStore, ArtifactStoreError
from .digest import digest_file, digest_hex, normalize_digest
from .errors import ArtifactValidationError, DigestMismatchError
from .state import atomic_write_json, read_json

OCI_LAYOUT_VERSION = "1.0.0"

MEDIA_TYPE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_INDEX = "application/vnd.oci.image.index.v1+json"

MEDIA_TYPE_LAYER_SQUASHFS = "application/vnd.afterglow.palimpsest.layer.squashfs.v1"
MEDIA_TYPE_LAYER_CONFIG = "application/vnd.afterglow.palimpsest.layer.config.v1+json"
MEDIA_TYPE_IMAGE_QCOW2 = "application/vnd.afterglow.palimpsest.image.qcow2.v1"
MEDIA_TYPE_IMAGE_RAW = "application/vnd.afterglow.palimpsest.image.raw.v1"

DISK_FORMAT_MEDIA_TYPES: dict[str, str] = {
    "qcow2": MEDIA_TYPE_IMAGE_QCOW2,
    "raw": MEDIA_TYPE_IMAGE_RAW,
}

ANNOTATION_NAME = "dev.afterglow.palimpsest.name"
ANNOTATION_CHAIN_ID = "dev.afterglow.palimpsest.chain-id"

MAX_BUNDLE_MEMBERS = 4096
MAX_JSON_BYTES = 4 * 1024 * 1024  # 4 MiB
TAR_BLOCK = 512
READ_CHUNK = 1024 * 1024  # 1 MiB

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOB_PATH_RE = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")


def canonical_json(value: Any) -> bytes:
    """Return deterministic canonical JSON bytes with sorted keys and compact formatting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def config_digest(payload: bytes) -> str:
    """Return canonical ``sha256:<hex>`` digest of raw bytes."""
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def build_tar_header(name: str, size: int, mode: int = 0o644, mtime: int = 0) -> bytes:
    """Build a PAX tar header for a single file member."""
    import tarfile

    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = mtime
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = mode
    return info.tobuf(format=tarfile.PAX_FORMAT)


def build_tar_padding(size: int) -> bytes:
    """Return zero bytes padding to align payload to 512-byte tar blocks."""
    remainder = size % TAR_BLOCK
    return b"\0" * (TAR_BLOCK - remainder) if remainder else b""


def build_bundle_tar_bytes(members: dict[str, bytes]) -> bytes:
    """Build deterministic OCI bundle tar bytes for a dictionary of member paths and payloads."""
    out = io.BytesIO()
    for name, payload in sorted(members.items()):
        out.write(build_tar_header(name, len(payload)))
        out.write(payload)
        padding = build_tar_padding(len(payload))
        if padding:
            out.write(padding)
    out.write(b"\0" * (TAR_BLOCK * 2))
    return out.getvalue()


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int
    annotations: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", normalize_digest(self.digest))
        if self.size < 0:
            raise ArtifactValidationError(f"descriptor size cannot be negative: {self.size}")

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
        }
        if self.annotations:
            res["annotations"] = dict(self.annotations)
        return res


@dataclass(frozen=True)
class ChainEntry:
    digest: str
    media_type: str
    size: int
    name: str
    parent_digest: str | None
    local_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "digest", normalize_digest(self.digest))
        if self.parent_digest is not None:
            object.__setattr__(self, "parent_digest", normalize_digest(self.parent_digest))
        if self.size < 0:
            raise ArtifactValidationError(f"chain entry size cannot be negative: {self.size}")


@dataclass(frozen=True)
class VerifiedManifest:
    manifest_digest: str
    config_digest: str
    entries: tuple[ChainEntry, ...]
    annotations: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_digest", normalize_digest(self.manifest_digest))
        object.__setattr__(self, "config_digest", normalize_digest(self.config_digest))


@dataclass(frozen=True)
class VerifiedLayout:
    manifests: tuple[VerifiedManifest, ...]


class ContentStore:
    """Strict content-addressed local store at ``root/blobs/sha256/<hex>``."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _physical_store(self) -> ArtifactStore:
        return ArtifactStore(self.root)

    @property
    def blobs_dir(self) -> Path:
        return self.root / "blobs" / "sha256"

    @property
    def metadata_dir(self) -> Path:
        return self.root / "metadata"

    def metadata_path(self, digest: str) -> Path:
        return self.metadata_dir / f"{digest_hex(digest)}.json"

    def write_metadata(self, digest: str, metadata: dict[str, Any]) -> Path:
        """Persist non-secret verified descriptor metadata beside an immutable blob."""
        normalized = normalize_digest(digest)
        physical = self._physical_store()
        with physical.digest_guard(normalized):
            try:
                size = physical.verify_blob(normalized).size
            except ArtifactStoreError:
                raise ArtifactValidationError(f"cannot record metadata for missing blob: {normalized}") from None
            record = {**metadata, "digest": normalized, "size": size}
            atomic_write_json(self.metadata_path(normalized), record)
        return self.metadata_path(normalized)

    def read_metadata(self, digest: str) -> dict[str, Any]:
        """Load a descriptor record only when it still names the requested blob."""
        normalized = normalize_digest(digest)
        record = read_json(self.metadata_path(normalized))
        if record.get("digest") != normalized:
            raise ArtifactValidationError(f"metadata digest mismatch for blob: {normalized}")
        return record

    def blob_path(self, digest: str) -> Path:
        return self.blobs_dir / digest_hex(digest)

    def exists(self, digest: str) -> bool:
        return self.blob_path(digest).is_file()

    def size(self, digest: str) -> int:
        path = self.blob_path(digest)
        if not path.is_file():
            raise ArtifactValidationError(f"blob does not exist in store: {digest}")
        return path.stat().st_size

    def open_read(self, digest: str) -> BinaryIO:
        path = self.blob_path(digest)
        if not path.is_file():
            raise ArtifactValidationError(f"blob does not exist in store: {digest}")
        return path.open("rb")

    def verify_blob(self, digest: str) -> int:
        """Verify a generic blob through the descriptor-pinned store boundary."""
        return self._physical_store().verify_blob(normalize_digest(digest)).size

    def write_stream(self, chunks: Iterator[bytes] | Sequence[bytes], expected_digest: str | None = None) -> Path:
        """Publish through the descriptor-pinned physical mutation authority."""
        if expected_digest is None:
            with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b") as spool:
                hasher = hashlib.sha256()
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ArtifactValidationError("content store producer yielded non-bytes")
                    hasher.update(chunk)
                    spool.write(chunk)
                expected_digest = f"sha256:{hasher.hexdigest()}"
                spool.seek(0)

                def replay() -> Iterator[bytes]:
                    while payload := spool.read(READ_CHUNK):
                        yield payload

                self._physical_store().publish_blob(replay(), expected_digest=expected_digest)
        else:
            normalized = normalize_digest(expected_digest)
            try:
                self._physical_store().publish_blob(chunks, expected_digest=normalized)
            except ArtifactStoreError as exc:
                if exc.code == "artifact-digest":
                    raise DigestMismatchError(f"stream digest mismatch: expected {normalized}") from None
                raise
            expected_digest = normalized
        return self.blob_path(expected_digest)

    def ingest_file(self, source: Path, expected_digest: str | None = None) -> Path:
        """Ingest a local file into the store via streaming verification."""
        source_path = source.resolve()
        if not source_path.is_file():
            raise ArtifactValidationError(f"source path is not a file: {source_path}")

        def _iter_file():
            with source_path.open("rb") as f:
                while chunk := f.read(READ_CHUNK):
                    yield chunk

        return self.write_stream(_iter_file(), expected_digest=expected_digest)

    def delete(self, digest: str, *, retention_guard: Callable[[], None] | None = None) -> int:
        """Delete only through a caller-supplied durable-reference guard."""
        if retention_guard is None:
            raise ArtifactValidationError("content store deletion requires a durable-reference guard")
        return self._physical_store().delete_blob(normalize_digest(digest), retention_guard=retention_guard)


def _safe_member_name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name:
        raise ArtifactValidationError(f"unsafe bundle member path: {name!r}")
    parts = Path(name).parts
    if ".." in parts or "." in parts:
        raise ArtifactValidationError(f"unsafe path traversal in member: {name!r}")
    if name not in ("oci-layout", "index.json") and not _BLOB_PATH_RE.fullmatch(name):
        raise ArtifactValidationError(f"invalid canonical member name: {name!r}")
    return name


def _parse_descriptor(data: Any, field_name: str) -> Descriptor:
    if not isinstance(data, dict):
        raise ArtifactValidationError(f"expected dict for {field_name}, got {type(data).__name__}")
    media_type = data.get("mediaType")
    digest = data.get("digest")
    size = data.get("size")
    if not isinstance(media_type, str) or not media_type:
        raise ArtifactValidationError(f"missing or invalid mediaType in {field_name}")
    if not isinstance(digest, str) or not digest:
        raise ArtifactValidationError(f"missing or invalid digest in {field_name}")
    if not isinstance(size, int) or size < 0:
        raise ArtifactValidationError(f"missing or invalid size in {field_name}")
    annotations = data.get("annotations") or {}
    if not isinstance(annotations, dict):
        raise ArtifactValidationError(f"annotations must be dict in {field_name}")
    str_annotations = {str(k): str(v) for k, v in annotations.items()}
    return Descriptor(media_type=media_type, digest=digest, size=size, annotations=str_annotations)


def _validate_manifest_layers(layers: list[Descriptor]) -> None:
    if not layers:
        raise ArtifactValidationError("manifest layers list cannot be empty")

    valid_base_types = {MEDIA_TYPE_LAYER_SQUASHFS, MEDIA_TYPE_IMAGE_QCOW2, MEDIA_TYPE_IMAGE_RAW}
    if layers[0].media_type not in valid_base_types:
        raise ArtifactValidationError(f"invalid mediaType at layer position 0: {layers[0].media_type!r}")

    for i, layer in enumerate(layers[1:], start=1):
        if layer.media_type != MEDIA_TYPE_LAYER_SQUASHFS:
            raise ArtifactValidationError(
                f"invalid mediaType at layer position {i}: {layer.media_type!r} (must be {MEDIA_TYPE_LAYER_SQUASHFS!r})"
            )

    digests = [layer.digest for layer in layers]
    if len(digests) != len(set(digests)):
        raise ArtifactValidationError("manifest cannot contain duplicate layer digests")


def build_manifest(chain: list[dict[str, Any]], config_bytes: bytes) -> dict[str, Any]:
    """Build an OCI image manifest dict for a root-to-leaf chain."""
    if not chain:
        raise ArtifactValidationError("chain cannot be empty")
    leaf = chain[-1]
    cfg_digest = config_digest(config_bytes)
    return {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_MANIFEST,
        "config": {
            "mediaType": MEDIA_TYPE_LAYER_CONFIG,
            "digest": cfg_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": item.get("media_type", MEDIA_TYPE_LAYER_SQUASHFS),
                "digest": normalize_digest(item["digest"]),
                "size": item["size"],
                **({"annotations": {ANNOTATION_NAME: item["name"]}} if item.get("name") else {}),
            }
            for item in chain
        ],
        "annotations": {
            **({ANNOTATION_NAME: leaf["name"]} if leaf.get("name") else {}),
            **({ANNOTATION_CHAIN_ID: leaf["chain_id"]} if leaf.get("chain_id") else {}),
        },
    }


def extract_bundle_tar(tar_path: Path, destination: Path) -> VerifiedLayout:
    """Extract and verify an OCI image-layout tar bundle atomically into destination."""
    import tarfile

    dest_path = destination.resolve()
    if dest_path.exists():
        if dest_path.is_dir() and any(dest_path.iterdir()):
            raise ArtifactValidationError(f"destination directory exists and is not empty: {dest_path}")
        elif not dest_path.is_dir():
            raise ArtifactValidationError(f"destination path is not a directory: {dest_path}")

    with tarfile.open(tar_path, mode="r:*") as tar:
        members = tar.getmembers()
        if len(members) > MAX_BUNDLE_MEMBERS:
            raise ArtifactValidationError(f"bundle member count ({len(members)}) exceeds max ({MAX_BUNDLE_MEMBERS})")

        by_name: dict[str, tarfile.TarInfo] = {}
        for m in members:
            if not m.isfile():
                raise ArtifactValidationError(f"bundle member is not a regular file: {m.name!r}")
            safe_name = _safe_member_name(m.name)
            if safe_name in by_name:
                raise ArtifactValidationError(f"duplicate member name in bundle: {safe_name!r}")
            by_name[safe_name] = m

        if "oci-layout" not in by_name:
            raise ArtifactValidationError("bundle missing oci-layout member")
        if "index.json" not in by_name:
            raise ArtifactValidationError("bundle missing index.json member")

        def _read_json(name: str) -> Any:
            info = by_name[name]
            if info.size > MAX_JSON_BYTES:
                raise ArtifactValidationError(f"{name} size ({info.size}) exceeds limit ({MAX_JSON_BYTES})")
            h = tar.extractfile(info)
            if h is None:
                raise ArtifactValidationError(f"unable to read member {name}")
            try:
                return json.loads(h.read().decode("utf-8"))
            except Exception as exc:
                raise ArtifactValidationError(f"invalid JSON in {name}: {exc}") from exc

        layout_data = _read_json("oci-layout")
        if not isinstance(layout_data, dict) or layout_data.get("imageLayoutVersion") != OCI_LAYOUT_VERSION:
            raise ArtifactValidationError("oci-layout imageLayoutVersion must be 1.0.0")

        index_data = _read_json("index.json")
        if not isinstance(index_data, dict) or index_data.get("schemaVersion") != 2:
            raise ArtifactValidationError("index.json schemaVersion must be 2")
        if index_data.get("mediaType") != MEDIA_TYPE_INDEX:
            raise ArtifactValidationError(f"index.json mediaType must be {MEDIA_TYPE_INDEX!r}")

        manifest_descs_raw = index_data.get("manifests")
        if not isinstance(manifest_descs_raw, list) or not manifest_descs_raw:
            raise ArtifactValidationError("index.json manifests list cannot be empty")

        verified_manifests: list[VerifiedManifest] = []
        parent_registry: dict[str, str | None] = {}

        # First pass: parse and validate all JSON structures before writing files
        parsed_manifest_triples: list[tuple[Descriptor, Any, Descriptor, list[Descriptor]]] = []

        for i, m_raw in enumerate(manifest_descs_raw):
            m_desc = _parse_descriptor(m_raw, f"index.json manifests[{i}]")
            if m_desc.media_type != MEDIA_TYPE_MANIFEST:
                raise ArtifactValidationError(f"manifest descriptor mediaType must be {MEDIA_TYPE_MANIFEST!r}")

            m_member_name = f"blobs/sha256/{digest_hex(m_desc.digest)}"
            if m_member_name not in by_name:
                raise ArtifactValidationError(f"manifest blob missing from bundle: {m_desc.digest}")

            m_data = _read_json(m_member_name)
            if not isinstance(m_data, dict) or m_data.get("schemaVersion") != 2:
                raise ArtifactValidationError(f"manifest {m_desc.digest} schemaVersion must be 2")
            if m_data.get("mediaType") != MEDIA_TYPE_MANIFEST:
                raise ArtifactValidationError(f"manifest {m_desc.digest} mediaType must be {MEDIA_TYPE_MANIFEST!r}")

            c_desc = _parse_descriptor(m_data.get("config"), "manifest config")
            if c_desc.media_type != MEDIA_TYPE_LAYER_CONFIG:
                raise ArtifactValidationError("manifest config mediaType must be layer config v1+json")

            c_member_name = f"blobs/sha256/{digest_hex(c_desc.digest)}"
            if c_member_name not in by_name:
                raise ArtifactValidationError(f"config blob missing from bundle: {c_desc.digest}")

            layers_raw = m_data.get("layers")
            if not isinstance(layers_raw, list) or not layers_raw:
                raise ArtifactValidationError("manifest layers list cannot be empty")

            layer_descs = [
                _parse_descriptor(layer_raw, f"manifest layers[{index}]") for index, layer_raw in enumerate(layers_raw)
            ]
            _validate_manifest_layers(layer_descs)

            for l_desc in layer_descs:
                l_member_name = f"blobs/sha256/{digest_hex(l_desc.digest)}"
                if l_member_name not in by_name:
                    raise ArtifactValidationError(f"layer blob missing from bundle: {l_desc.digest}")

            for descriptor, label in [
                (m_desc, "manifest"),
                (c_desc, "config"),
                *[(layer, "layer") for layer in layer_descs],
            ]:
                member = by_name[f"blobs/sha256/{digest_hex(descriptor.digest)}"]
                if member.size != descriptor.size:
                    raise ArtifactValidationError(
                        f"{label} descriptor size mismatch for {descriptor.digest}: "
                        f"expected {descriptor.size}, got {member.size}"
                    )
            parsed_manifest_triples.append((m_desc, m_data, c_desc, layer_descs))

        # Second pass: atomic extraction into temporary directory
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir_str = tempfile.mkdtemp(dir=dest_path.parent, prefix=".tmp_layout_")
        tmp_work_dir = Path(tmp_dir_str)

        try:
            blobs_dir = tmp_work_dir / "blobs" / "sha256"
            blobs_dir.mkdir(parents=True, exist_ok=True)

            # Write oci-layout and index.json
            for json_name in ("oci-layout", "index.json"):
                info = by_name[json_name]
                handle = tar.extractfile(info)
                if handle is None:
                    raise ArtifactValidationError(f"unable to read member {json_name}")
                dest_file = tmp_work_dir / json_name
                with dest_file.open("wb") as out:
                    out.write(handle.read())
                    out.flush()
                    os.fsync(out.fileno())

            # Write and verify all blobs
            for name, info in by_name.items():
                if name in ("oci-layout", "index.json"):
                    continue
                hex_part = name.split("/")[-1]
                expected_digest = f"sha256:{hex_part}"
                dest_file = blobs_dir / hex_part

                handle = tar.extractfile(info)
                if handle is None:
                    raise ArtifactValidationError(f"unable to read blob member {name}")

                hasher = hashlib.sha256()
                written = 0
                with dest_file.open("wb") as out:
                    while chunk := handle.read(READ_CHUNK):
                        hasher.update(chunk)
                        out.write(chunk)
                        written += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())

                actual_digest = f"sha256:{hasher.hexdigest()}"
                if actual_digest != expected_digest:
                    raise DigestMismatchError(
                        f"digest mismatch for blob {name}: expected {expected_digest}, got {actual_digest}"
                    )

                if info.size != written:
                    raise ArtifactValidationError(
                        f"size mismatch for member {name}: declared {info.size}, got {written}"
                    )

                os.chmod(dest_file, 0o444)

            # Build VerifiedLayout structure
            for m_desc, m_data, c_desc, layer_descs in parsed_manifest_triples:
                chain_entries: list[ChainEntry] = []
                prev_digest: str | None = None

                for l_desc in layer_descs:
                    l_path = blobs_dir / digest_hex(l_desc.digest)
                    name = l_desc.annotations.get(ANNOTATION_NAME, "")

                    if l_desc.digest in parent_registry:
                        known_parent = parent_registry[l_desc.digest]
                        if known_parent != prev_digest:
                            raise ArtifactValidationError(
                                f"contradictory parent chain for layer {l_desc.digest}: "
                                f"known parent={known_parent!r}, new parent={prev_digest!r}"
                            )
                    else:
                        parent_registry[l_desc.digest] = prev_digest

                    chain_entries.append(
                        ChainEntry(
                            digest=l_desc.digest,
                            media_type=l_desc.media_type,
                            size=l_desc.size,
                            name=name,
                            parent_digest=prev_digest,
                            local_path=l_path,
                        )
                    )
                    prev_digest = l_desc.digest

                m_annotations = m_data.get("annotations") or {}
                str_m_annotations = (
                    {str(k): str(v) for k, v in m_annotations.items()} if isinstance(m_annotations, dict) else {}
                )

                verified_manifests.append(
                    VerifiedManifest(
                        manifest_digest=m_desc.digest,
                        config_digest=c_desc.digest,
                        entries=tuple(chain_entries),
                        annotations=str_m_annotations,
                    )
                )

            # Finalize: atomically move tmp_work_dir to dest_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if dest_path.exists() and dest_path.is_dir() and not any(dest_path.iterdir()):
                dest_path.rmdir()
            os.replace(tmp_work_dir, dest_path)

            # Update local_paths to final destination
            final_manifests: list[VerifiedManifest] = []
            for vm in verified_manifests:
                final_entries = [
                    ChainEntry(
                        digest=e.digest,
                        media_type=e.media_type,
                        size=e.size,
                        name=e.name,
                        parent_digest=e.parent_digest,
                        local_path=dest_path / "blobs" / "sha256" / digest_hex(e.digest),
                    )
                    for e in vm.entries
                ]
                final_manifests.append(
                    VerifiedManifest(
                        manifest_digest=vm.manifest_digest,
                        config_digest=vm.config_digest,
                        entries=tuple(final_entries),
                        annotations=vm.annotations,
                    )
                )

            return VerifiedLayout(manifests=tuple(final_manifests))

        except Exception:
            shutil.rmtree(tmp_work_dir, ignore_errors=True)
            raise


def verify_layout_dir(directory: Path) -> VerifiedLayout:
    """Verify an existing local OCI layout directory without modifying it."""
    dir_path = directory.resolve()
    if not dir_path.is_dir():
        raise ArtifactValidationError(f"layout directory does not exist: {dir_path}")

    def _read_file_bytes(rel_path: str, max_bytes: int | None = None) -> bytes:
        p = dir_path / rel_path
        if p.is_symlink() or not p.is_file():
            raise ArtifactValidationError(f"layout file {rel_path!r} is missing or unsafe (symlink/non-file)")
        size = p.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise ArtifactValidationError(f"layout file {rel_path!r} size ({size}) exceeds limit ({max_bytes})")
        with p.open("rb") as f:
            return f.read()

    def _verify_blob_file(digest: str, expected_size: int | None = None) -> Path:
        hex_part = digest_hex(digest)
        rel_path = f"blobs/sha256/{hex_part}"
        p = dir_path / rel_path
        if p.is_symlink() or not p.is_file():
            raise ArtifactValidationError(f"blob file {rel_path!r} is missing or unsafe (symlink/non-file)")
        actual_digest = digest_file(p)
        if actual_digest != digest:
            raise DigestMismatchError(f"digest mismatch for blob {rel_path!r}: expected {digest}, got {actual_digest}")
        actual_size = p.stat().st_size
        if expected_size is not None and actual_size != expected_size:
            raise ArtifactValidationError(
                f"size mismatch for blob {rel_path!r}: expected {expected_size}, got {actual_size}"
            )
        return p

    layout_raw = _read_file_bytes("oci-layout", max_bytes=MAX_JSON_BYTES)
    try:
        layout_data = json.loads(layout_raw.decode("utf-8"))
    except Exception as exc:
        raise ArtifactValidationError(f"invalid oci-layout JSON: {exc}") from exc
    if not isinstance(layout_data, dict) or layout_data.get("imageLayoutVersion") != OCI_LAYOUT_VERSION:
        raise ArtifactValidationError("oci-layout imageLayoutVersion must be 1.0.0")

    index_raw = _read_file_bytes("index.json", max_bytes=MAX_JSON_BYTES)
    try:
        index_data = json.loads(index_raw.decode("utf-8"))
    except Exception as exc:
        raise ArtifactValidationError(f"invalid index.json JSON: {exc}") from exc

    if not isinstance(index_data, dict) or index_data.get("schemaVersion") != 2:
        raise ArtifactValidationError("index.json schemaVersion must be 2")
    if index_data.get("mediaType") != MEDIA_TYPE_INDEX:
        raise ArtifactValidationError(f"index.json mediaType must be {MEDIA_TYPE_INDEX!r}")

    manifest_descs_raw = index_data.get("manifests")
    if not isinstance(manifest_descs_raw, list) or not manifest_descs_raw:
        raise ArtifactValidationError("index.json manifests list cannot be empty")

    verified_manifests: list[VerifiedManifest] = []
    parent_registry: dict[str, str | None] = {}

    for i, m_raw in enumerate(manifest_descs_raw):
        m_desc = _parse_descriptor(m_raw, f"index.json manifests[{i}]")
        if m_desc.media_type != MEDIA_TYPE_MANIFEST:
            raise ArtifactValidationError(f"manifest descriptor mediaType must be {MEDIA_TYPE_MANIFEST!r}")

        m_blob_path = _verify_blob_file(m_desc.digest, m_desc.size)
        manifest_bytes = m_blob_path.read_bytes()
        try:
            m_data = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as exc:
            raise ArtifactValidationError(f"invalid manifest JSON at {m_desc.digest}: {exc}") from exc

        if not isinstance(m_data, dict) or m_data.get("schemaVersion") != 2:
            raise ArtifactValidationError("manifest schemaVersion must be 2")
        if m_data.get("mediaType") != MEDIA_TYPE_MANIFEST:
            raise ArtifactValidationError("manifest mediaType must be manifest v1+json")

        c_desc = _parse_descriptor(m_data.get("config"), "manifest config")
        if c_desc.media_type != MEDIA_TYPE_LAYER_CONFIG:
            raise ArtifactValidationError("manifest config mediaType must be layer config v1+json")
        _verify_blob_file(c_desc.digest, c_desc.size)

        layers_raw = m_data.get("layers")
        if not isinstance(layers_raw, list) or not layers_raw:
            raise ArtifactValidationError("manifest layers list cannot be empty")

        layer_descs = [
            _parse_descriptor(layer_raw, f"manifest layers[{index}]") for index, layer_raw in enumerate(layers_raw)
        ]
        _validate_manifest_layers(layer_descs)

        chain_entries: list[ChainEntry] = []
        prev_digest: str | None = None

        for l_desc in layer_descs:
            l_path = _verify_blob_file(l_desc.digest, l_desc.size)
            name = l_desc.annotations.get(ANNOTATION_NAME, "")

            if l_desc.digest in parent_registry:
                known_parent = parent_registry[l_desc.digest]
                if known_parent != prev_digest:
                    raise ArtifactValidationError(
                        f"contradictory parent chain for layer {l_desc.digest}: "
                        f"known parent={known_parent!r}, new parent={prev_digest!r}"
                    )
            else:
                parent_registry[l_desc.digest] = prev_digest

            chain_entries.append(
                ChainEntry(
                    digest=l_desc.digest,
                    media_type=l_desc.media_type,
                    size=l_desc.size,
                    name=name,
                    parent_digest=prev_digest,
                    local_path=l_path,
                )
            )
            prev_digest = l_desc.digest

        m_annotations = m_data.get("annotations") or {}
        str_m_annotations = (
            {str(k): str(v) for k, v in m_annotations.items()} if isinstance(m_annotations, dict) else {}
        )

        verified_manifests.append(
            VerifiedManifest(
                manifest_digest=m_desc.digest,
                config_digest=c_desc.digest,
                entries=tuple(chain_entries),
                annotations=str_m_annotations,
            )
        )

    return VerifiedLayout(manifests=tuple(verified_manifests))
