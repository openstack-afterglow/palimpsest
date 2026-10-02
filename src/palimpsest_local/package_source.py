"""Bounded, byte-preserving snapshots of native package sources.

A source is copied exactly once into a private directory and only that frozen
copy is verified and transferred, so later source edits cannot race upload.
Tar sources keep their original bytes; directory sources become one
deterministic USTAR archive. Verification follows the selected root's complete
reachable graph without platform selection: every index child, config and
layer, plus the Palimpsest runtime-bundle parent/config/base dialect.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import stat
import tarfile
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError
from .oci_image import strict_json_object
from .oci_layout import (
    ANNOTATION_CHAIN_ID,
    MEDIA_TYPE_IMAGE_QCOW2,
    MEDIA_TYPE_IMAGE_RAW,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
)
from .oci_provenance import (
    DOCKER_IMAGE_CONFIG_MEDIA_TYPE,
    DOCKER_IMAGE_MANIFEST_MEDIA_TYPE,
    DOCKER_MANIFEST_LIST_MEDIA_TYPE,
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    Descriptor,
)
from .oci_source import (
    _noop_checkpoint,
    _open_absolute_directory,
    _open_absolute_regular_file,
    _open_child_directory,
    _stable_metadata,
)

try:  # Python 3.14+; the root client deliberately has no third-party zstd dependency.
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - interpreter dependent
    _zstd = None

_CHUNK = 1024 * 1024
_BLOB = re.compile(r"blobs/sha256/[0-9a-f]{64}\Z")
_METADATA = frozenset({"oci-layout", "index.json"})
_DIRECTORIES = frozenset({"blobs", "blobs/sha256"})
_PAX_FIELDS = frozenset({"path", "size", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname"})
_INDEX_TYPES = frozenset({OCI_IMAGE_INDEX_MEDIA_TYPE, DOCKER_MANIFEST_LIST_MEDIA_TYPE})
_IMAGE_CONFIG_FOR_MANIFEST = {
    OCI_IMAGE_MANIFEST_MEDIA_TYPE: OCI_IMAGE_CONFIG_MEDIA_TYPE,
    DOCKER_IMAGE_MANIFEST_MEDIA_TYPE: DOCKER_IMAGE_CONFIG_MEDIA_TYPE,
}
_RAW_LAYERS = frozenset(
    {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.nondistributable.v1.tar"}
)
_GZIP_LAYERS = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    }
)
_ZSTD_LAYERS = frozenset(
    {"application/vnd.oci.image.layer.v1.tar+zstd", "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd"}
)
_LAYER_TYPES = _RAW_LAYERS | _GZIP_LAYERS | _ZSTD_LAYERS
_RUNTIME_BASES = {MEDIA_TYPE_IMAGE_QCOW2: "qcow2", MEDIA_TYPE_IMAGE_RAW: "raw"}
_CONFIG_DIGEST_ANNOTATION = "dev.afterglow.palimpsest.config-digest"
_ATTESTATION_REFERENCE = "vnd.docker.reference.type"
_ATTESTATION_SUBJECT = "vnd.docker.reference.digest"
_IN_TOTO = "application/vnd.in-toto+json"
_ZSTD_WINDOW_LOG_MAX = 27  # 128 MiB, the Hub validator's bound
_DECOMPRESSION_ERRORS: tuple[type[BaseException], ...] = (OSError, EOFError, ValueError, zlib.error) + (
    (_zstd.ZstdError,) if _zstd is not None else ()
)


@dataclass(frozen=True)
class PackageLimits:
    """Client-side bounds; they mirror, and never exceed, the Hub validator."""

    max_archive_bytes: int = 16 * 1024**3
    max_blob_bytes: int = 16 * 1024**3
    max_expanded_bytes: int = 32 * 1024**3
    max_members: int = 4096
    max_json_bytes: int = 4 * 1024**2
    max_extension_bytes: int = 64 * 1024
    max_depth: int = 64
    max_edges: int = 32768

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ArtifactValidationError("package limits must be positive integers")


@dataclass(frozen=True)
class PackageSnapshot:
    """One frozen package archive; ``archive`` exists only inside the context."""

    archive: Path
    archive_digest: str
    archive_size_bytes: int
    root_digest: str
    root_media_type: str
    package_type: str
    platforms: tuple[dict[str, str], ...]
    graph: dict[str, dict[str, Any]] = field(repr=False)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _create_private(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _copy_fd(fd: int, destination: Path, maximum: int) -> None:
    """Copy one pinned regular file, proving it did not change while copied."""
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise ArtifactValidationError("package source file is unsafe or exceeds its byte limit")
    copied = 0
    output = os.fdopen(_create_private(destination), "wb")
    with output:
        while chunk := os.read(fd, min(_CHUNK, maximum + 1 - copied)):
            copied += len(chunk)
            if copied > maximum:
                raise ArtifactValidationError("package source exceeds its byte limit")
            output.write(chunk)
    if copied != before.st_size or _stable_metadata(before) != _stable_metadata(os.fstat(fd)):
        raise ArtifactValidationError("package source changed during snapshot")


def _bounded_names(fd: int, maximum: int) -> list[str]:
    """List a directory without allocating more than ``maximum`` names."""
    names: list[str] = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= maximum:
                raise ArtifactValidationError("package source contains too many members")
            names.append(entry.name)
    return sorted(names, key=os.fsencode)


def _snapshot_directory(source: Path, layout: Path, limits: PackageLimits) -> None:
    members = 0
    total = 0

    def copy_file(parent_fd: int, name: str, relative: str, maximum: int) -> None:
        nonlocal total
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        maximum = min(maximum, limits.max_archive_bytes - total)
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_size > maximum:
            raise ArtifactValidationError("package source contains an unsafe file or exceeds its total byte limit")
        total += entry.st_size
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            if _stable_metadata(entry) != _stable_metadata(os.fstat(file_fd)):
                raise ArtifactValidationError("package source file binding changed")
            _copy_fd(file_fd, layout / relative, maximum)
        finally:
            os.close(file_fd)
        if _stable_metadata(entry) != _stable_metadata(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)):
            raise ArtifactValidationError("package source changed during snapshot")

    def walk(directory_fd: int, prefix: str) -> None:
        nonlocal members
        names = _bounded_names(directory_fd, limits.max_members - members)
        members += len(names)
        for name in names:
            relative = prefix + name
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode):
                if relative not in _DIRECTORIES:
                    raise ArtifactValidationError("package source contains an unexpected directory")
                (layout / relative).mkdir(mode=0o700, exist_ok=True)
                child = _open_child_directory(directory_fd, name, _noop_checkpoint)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                        raise ArtifactValidationError("package source directory binding changed")
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif relative in _METADATA:
                copy_file(directory_fd, name, relative, limits.max_json_bytes)
            elif _BLOB.fullmatch(relative):
                copy_file(directory_fd, name, relative, limits.max_blob_bytes)
            else:
                raise ArtifactValidationError("package source contains an unexpected or unsafe member")
        if _bounded_names(directory_fd, len(names) + 1) != names:
            raise ArtifactValidationError("package source directory changed during snapshot")

    with _open_absolute_directory(source, _noop_checkpoint) as root_fd:
        walk(root_fd, "")
    (layout / "blobs" / "sha256").mkdir(parents=True, mode=0o700, exist_ok=True)


def _write_deterministic_tar(layout: Path, archive: Path) -> None:
    """Emit stable USTAR bytes: sorted names, zero owners/times, fixed modes."""
    names = ["blobs", "blobs/sha256"]
    names.extend(name for name in _METADATA if (layout / name).is_file())
    names.extend("blobs/sha256/" + path.name for path in (layout / "blobs" / "sha256").iterdir())
    with os.fdopen(_create_private(archive), "wb") as output:
        for name in sorted(names, key=str.encode):
            path = layout / name
            info = tarfile.TarInfo(name)
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            if name in _DIRECTORIES:
                info.type, info.mode, info.size = tarfile.DIRTYPE, 0o755, 0
                output.write(info.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict"))
                continue
            info.mode, info.size = 0o644, path.stat().st_size
            # PAX emits a plain USTAR header unless a field (only size >= 8 GiB
            # here) needs an extension, so small archives stay byte-identical.
            output.write(info.tobuf(tarfile.PAX_FORMAT, "utf-8", "strict"))
            written = 0
            with path.open("rb") as payload:
                while chunk := payload.read(_CHUNK):
                    written += len(chunk)
                    output.write(chunk)
            if written != info.size:
                raise ArtifactValidationError("frozen package member changed")
            output.write(b"\0" * (-info.size % 512))
        output.write(b"\0" * 1024)


def _pax(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    offset = 0
    while offset < len(payload):
        separator = payload.find(b" ", offset)
        length = payload[offset:separator]
        if separator < 0 or separator - offset > 8 or not length.isdigit():
            raise ArtifactValidationError("package archive has an invalid PAX record")
        end = offset + int(length)
        if end > len(payload) or end <= separator + 1 or payload[end - 1 : end] != b"\n":
            raise ArtifactValidationError("package archive has a truncated PAX record")
        key, equal, value = payload[separator + 1 : end - 1].partition(b"=")
        try:
            key_text, value_text = key.decode("utf-8"), value.decode("utf-8")
        except UnicodeError:
            raise ArtifactValidationError("package archive PAX record is not UTF-8") from None
        if not equal or key_text in result or key_text not in _PAX_FIELDS:
            raise ArtifactValidationError("package archive has an unsupported or duplicate PAX field")
        result[key_text] = value_text
        offset = end
    return result


def _member_name(value: str, *, directory: bool) -> str:
    name = value.removesuffix("/") if directory else value
    if not name or len(name.encode("utf-8")) > 256 or "\\" in name or "\0" in name:
        raise ArtifactValidationError("package archive member name is unsafe")
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise ArtifactValidationError("package archive member path is not canonical")
    return name


def _extract_archive(archive: Path, layout: Path, limits: PackageLimits) -> None:
    """Copy members by physical offset; no tarfile extraction or path joins."""
    (layout / "blobs" / "sha256").mkdir(parents=True, mode=0o700)
    size = archive.stat().st_size
    seen: set[str] = set()
    pending: dict[str, str] | None = None
    count = 0
    offset = 0
    with archive.open("rb") as stream:
        while True:
            header = stream.read(512)
            if len(header) != 512:
                raise ArtifactValidationError("package archive is truncated or lacks a terminator")
            if not any(header):
                if pending is not None or size - offset < 1024:
                    raise ArtifactValidationError("package archive has an incomplete terminator")
                while chunk := stream.read(_CHUNK):
                    if any(chunk):
                        raise ArtifactValidationError("package archive has data after its terminator")
                return
            count += 1
            if count > limits.max_members:
                raise ArtifactValidationError("package archive contains too many members")
            try:
                info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            except (tarfile.TarError, UnicodeError, ValueError):
                raise ArtifactValidationError("package archive header is malformed") from None
            if info.size < 0:
                raise ArtifactValidationError("package archive member size is invalid")
            if info.type == tarfile.XHDTYPE:
                if pending is not None or info.size > limits.max_extension_bytes:
                    raise ArtifactValidationError("package archive PAX extension is chained or oversized")
                pending = _pax(stream.read(info.size))
                offset += 512 + info.size + (-info.size % 512)
                if offset > size:
                    raise ArtifactValidationError("package archive PAX extension is truncated")
                stream.seek(offset)
                continue
            if not info.isdir() and info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                raise ArtifactValidationError("package archive links, special files and extensions are forbidden")
            member_size = info.size
            declared = (pending or {}).get("size")
            if declared is not None:
                if not declared.isdigit() or len(declared) > 20:
                    raise ArtifactValidationError("package archive PAX size is invalid")
                member_size = int(declared)
            name = _member_name((pending or {}).get("path", info.name), directory=info.isdir())
            pending = None
            if name in seen:
                raise ArtifactValidationError("package archive contains duplicate members")
            seen.add(name)
            if info.isdir():
                if member_size or name not in _DIRECTORIES:
                    raise ArtifactValidationError("package archive contains an unexpected directory")
            elif name in _METADATA or _BLOB.fullmatch(name):
                maximum = limits.max_json_bytes if name in _METADATA else limits.max_blob_bytes
                if member_size > maximum or offset + 512 + member_size > size:
                    raise ArtifactValidationError("package archive member exceeds its byte bound")
                remaining = member_size
                with os.fdopen(_create_private(layout / name), "wb") as output:
                    while remaining:
                        chunk = stream.read(min(_CHUNK, remaining))
                        if not chunk:
                            raise ArtifactValidationError("package archive member is truncated")
                        output.write(chunk)
                        remaining -= len(chunk)
            else:
                raise ArtifactValidationError("package archive contains an unexpected member")
            offset += 512 + member_size + (-member_size % 512)
            if offset > size:
                raise ArtifactValidationError("package archive member is truncated")
            stream.seek(offset)


class _HashingReader:
    """Count and hash the compressed stream while a decompressor consumes it."""

    def __init__(self, raw, hasher, maximum: int):
        self.raw, self.hasher, self.maximum, self.size = raw, hasher, maximum, 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self.raw.read(_CHUNK if size is None or size < 0 else min(size, _CHUNK))
        self.size += len(chunk)
        if self.size > self.maximum:
            raise ArtifactValidationError("package layer exceeds its descriptor size")
        self.hasher.update(chunk)
        return chunk


def _string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise ArtifactValidationError(f"{label} must be a string map")
    return value


def _platform_constraint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"os", "architecture"} <= set(value):
        raise ArtifactValidationError("index platform must declare os and architecture")
    result: dict[str, Any] = {}
    for key in ("os", "architecture", "variant", "os.version"):
        item = value.get(key)
        if key in {"os", "architecture"} or item is not None:
            if not isinstance(item, str) or not item or "\0" in item or len(item.encode("utf-8")) > 256:
                raise ArtifactValidationError("index platform field is invalid")
            result[key] = item
    if "os.features" in value and (
        not isinstance(value["os.features"], list) or any(not isinstance(item, str) or not item for item in value["os.features"])
    ):
        raise ArtifactValidationError("index platform os.features is invalid")
    return {**result, "os.features": value.get("os.features", [])}


def _image_platform(config: dict[str, Any], constraint: dict[str, Any] | None) -> dict[str, str]:
    """Return the DTO platform; absent optional config fields take the index value."""
    platform: dict[str, str] = {}
    for key in ("os", "architecture"):
        value = config.get(key)
        if not isinstance(value, str) or not value or "\0" in value or len(value.encode("utf-8")) > 256:
            raise ArtifactValidationError("OCI config must declare a valid os and architecture")
        if constraint is not None and constraint[key] != value:
            raise ArtifactValidationError("index platform disagrees with image config")
        platform[key] = value
    for key in ("variant", "os.version"):
        value = config.get(key)
        declared = None if constraint is None else constraint.get(key)
        if value is not None and (not isinstance(value, str) or not value or "\0" in value or len(value.encode("utf-8")) > 256):
            raise ArtifactValidationError(f"OCI config {key} is invalid")
        if value is not None and declared is not None and value != declared:
            raise ArtifactValidationError("index platform disagrees with image config")
        if value or declared:
            platform[key] = value or declared
    features = config.get("os.features", [])
    if not isinstance(features, list) or any(not isinstance(item, str) or not item for item in features):
        raise ArtifactValidationError("OCI config os.features is invalid")
    if "os.features" in config and constraint is not None and features != constraint["os.features"]:
        raise ArtifactValidationError("index platform os.features disagree with image config")
    return platform


_MAX_PROCESS_ENTRIES = 8192
_MAX_PROCESS_BYTES = 256 * 1024
_MAX_PROCESS_STRING_BYTES = 32 * 1024
_STOP_SIGNAL = re.compile(r"(?:(?:SIG)?[A-Za-z][A-Za-z0-9]{0,15}(?:[+-][0-9]{1,2})?|[0-9]{1,2})\Z")


def _process(value: Any) -> None:
    """Structural process metadata only (literal port of the Hub ``_process``)."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise ArtifactValidationError("config process metadata must be an object or null")
    count, total = 0, 0

    def text(item: Any, label: str) -> None:
        nonlocal total
        if not isinstance(item, str) or "\0" in item:
            raise ArtifactValidationError(f"invalid process {label}")
        encoded = len(item.encode("utf-8", "surrogatepass"))
        if encoded > _MAX_PROCESS_STRING_BYTES:
            raise ArtifactValidationError(f"process {label} exceeds string byte limit")
        total += encoded + 1

    for name in ("Entrypoint", "Cmd", "Env", "Shell", "OnBuild"):
        items = value.get(name)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ArtifactValidationError(f"process {name} must be an array or null")
        for item in items:
            text(item, name)
            count += 1
            if name == "Env" and ("=" not in item or item.startswith("=")):
                raise ArtifactValidationError("process Env entries must be NAME=value")
    for name in ("WorkingDir", "User", "StopSignal"):
        if value.get(name) is not None:
            text(value[name], name)
    stop = value.get("StopSignal")
    if stop and _STOP_SIGNAL.fullmatch(stop) is None:
        raise ArtifactValidationError("process StopSignal must be a signal name or number")
    if stop and stop.isdecimal() and not 1 <= int(stop) <= 64:
        raise ArtifactValidationError("process StopSignal number must be 1-64")
    for name in ("ExposedPorts", "Volumes", "Labels"):
        mapping = value.get(name)
        if mapping is not None and not isinstance(mapping, dict):
            raise ArtifactValidationError(f"process {name} must be an object or null")
        if name == "Labels" and mapping:
            for key, item in mapping.items():
                text(key, "label")
                text(item, "label")
                count += 1
    if value.get("ArgsEscaped") is not None and type(value["ArgsEscaped"]) is not bool:
        raise ArtifactValidationError("process ArgsEscaped must be boolean or null")
    if count > _MAX_PROCESS_ENTRIES or total > _MAX_PROCESS_BYTES:
        raise ArtifactValidationError("process metadata exceeds count/byte limit")


class _Graph:
    """Verify exactly the selected root's reachable graph inside a frozen layout."""

    def __init__(self, layout: Path, limits: PackageLimits):
        self.layout, self.limits = layout, limits
        self.verified: dict[str, dict[str, Any]] = {}
        self.paths: dict[str, Path] = {}
        self.diff_ids: dict[str, str] = {}
        self.active: set[str] = set()
        self.platforms: list[dict[str, str]] = []
        self.package_types: set[str] = set()
        self.runtime_configs: dict[str, dict[str, Any]] = {}
        self.runtime_parents: dict[str, str | None] = {}
        self.roots: list[dict[str, Any]] = []
        self.edges = 0
        self.expanded = 0

    def json_file(self, path: Path, label: str) -> dict[str, Any]:
        if not path.is_file() or path.stat().st_size > self.limits.max_json_bytes:
            raise ArtifactValidationError(f"{label} is absent or exceeds the JSON byte limit")
        try:
            return strict_json_object(path.read_bytes(), label)
        except RecursionError:
            raise ArtifactValidationError(f"{label} exceeds JSON nesting bounds") from None

    def descriptor(self, raw: Any) -> dict[str, Any]:
        self.edges += 1
        if self.edges > self.limits.max_edges:
            raise ArtifactValidationError("package graph exceeds its descriptor edge limit")
        if not isinstance(raw, dict) or "data" in raw:
            raise ArtifactValidationError("package descriptor must be an object without embedded data")
        try:
            Descriptor(media_type=raw.get("mediaType"), digest=raw.get("digest"), size=raw.get("size"))
        except (ArtifactValidationError, TypeError):
            raise ArtifactValidationError("package descriptor is invalid") from None
        if raw["size"] > self.limits.max_blob_bytes:
            raise ArtifactValidationError("package descriptor exceeds the blob byte limit")
        urls = raw.get("urls", [])
        if not isinstance(urls, list) or any(not isinstance(item, str) for item in urls):
            raise ArtifactValidationError("package descriptor urls must be strings")
        _string_map(raw.get("annotations", {}), "package descriptor annotations")
        return raw

    def _path(self, descriptor: dict[str, Any]) -> Path:
        digest, media_type, size = descriptor["digest"], descriptor["mediaType"], descriptor["size"]
        known = self.verified.get(digest)
        if known is not None and known != {"media_type": media_type, "size_bytes": size}:
            raise ArtifactValidationError("package graph has conflicting descriptor identities")
        path = self.paths.get(digest, self.layout / "blobs" / "sha256" / digest.split(":", 1)[1])
        if not path.is_file() or path.is_symlink():
            raise ArtifactValidationError("package graph blob is missing (descriptor URLs are never fetched)")
        if path.stat().st_size != size:
            raise ArtifactValidationError("package graph blob size differs from its descriptor")
        return path

    def blob(self, raw: Any) -> Path:
        descriptor = self.descriptor(raw)
        path = self._path(descriptor)
        if descriptor["digest"] not in self.verified:
            if _hash_file(path) != (descriptor["digest"], descriptor["size"]):
                raise ArtifactValidationError("package graph blob digest mismatch")
            self.verified[descriptor["digest"]] = {"media_type": descriptor["mediaType"], "size_bytes": descriptor["size"]}
            self.paths[descriptor["digest"]] = path
        return path

    def document(self, raw: Any) -> dict[str, Any]:
        descriptor = self.descriptor(raw)
        if descriptor["size"] > self.limits.max_json_bytes:
            raise ArtifactValidationError("package JSON blob exceeds the JSON byte limit")
        return self.json_file(self.blob(descriptor), "package JSON blob")

    @staticmethod
    def schema(document: dict[str, Any], media_type: str) -> None:
        if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
            raise ArtifactValidationError("package manifest/index must use schemaVersion 2")
        if document.get("mediaType", media_type) != media_type:
            raise ArtifactValidationError("package document mediaType differs from its descriptor")
        if document.get("subject") is not None or document.get("artifactType") is not None:
            raise ArtifactValidationError("artifact/subject graphs are not packages")

    def array(self, value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
        if not isinstance(value, list) or len(value) > self.limits.max_members or (nonempty and not value):
            raise ArtifactValidationError(f"{label} must be a bounded array")
        return value

    def layer_diff_id(self, raw: Any) -> str:
        """Verify compressed digest and DiffID in one bounded pass."""
        descriptor = self.descriptor(raw)
        digest, media_type = descriptor["digest"], descriptor["mediaType"]
        if digest in self.diff_ids:
            self.blob(descriptor)
            return self.diff_ids[digest]
        if media_type not in _LAYER_TYPES:
            raise ArtifactValidationError("unsupported OCI image layer media type")
        if media_type in _ZSTD_LAYERS and _zstd is None:
            raise ArtifactValidationError("zstd OCI layers require Python 3.14 compression.zstd for DiffID verification")
        path = self._path(descriptor)
        compressed, uncompressed = hashlib.sha256(), hashlib.sha256()
        with path.open("rb") as raw_file:
            source = _HashingReader(raw_file, compressed, descriptor["size"])
            try:
                if media_type in _GZIP_LAYERS:
                    stream = gzip.GzipFile(fileobj=source, mode="rb")
                elif media_type in _ZSTD_LAYERS:
                    stream = _zstd.ZstdFile(
                        source, mode="rb", options={_zstd.DecompressionParameter.window_log_max: _ZSTD_WINDOW_LOG_MAX}
                    )
                else:
                    stream = source
                while chunk := stream.read(_CHUNK):
                    if stream is not source:
                        self.expanded += len(chunk)
                        if self.expanded > self.limits.max_expanded_bytes:
                            raise ArtifactValidationError("package layers exceed the expanded byte limit")
                    uncompressed.update(chunk)
                if stream is not source:
                    stream.close()
                    while source.read(_CHUNK):
                        pass
            except _DECOMPRESSION_ERRORS:
                raise ArtifactValidationError("package layer is not valid compressed data") from None
        if source.size != descriptor["size"] or "sha256:" + compressed.hexdigest() != digest:
            raise ArtifactValidationError("package graph blob digest mismatch")
        self.verified[digest] = {"media_type": media_type, "size_bytes": descriptor["size"]}
        self.paths[digest] = path
        self.diff_ids[digest] = "sha256:" + uncompressed.hexdigest()
        return self.diff_ids[digest]

    def select_root(self, manifest: str | None) -> dict[str, Any]:
        if self.json_file(self.layout / "oci-layout", "OCI layout marker") != {"imageLayoutVersion": "1.0.0"}:
            raise ArtifactValidationError("unsupported OCI image layout version")
        index_path = self.layout / "index.json"
        index = self.json_file(index_path, "OCI layout index.json")
        self.schema(index, OCI_IMAGE_INDEX_MEDIA_TYPE)
        self.roots = [self.descriptor(raw) for raw in self.array(index.get("manifests"), "layout roots", nonempty=True)]
        if manifest is None:
            if len(self.roots) != 1:
                raise ArtifactValidationError("package root selection requires exactly one root; specify --manifest")
            return self.roots[0]
        matches = [root for root in self.roots if root["digest"] == manifest]
        if matches:
            if any(match != matches[0] for match in matches[1:]):
                raise ArtifactValidationError("selected package root has ambiguous descriptors")
            return matches[0]
        # An explicitly selected layout index.json is itself a legitimate root.
        digest, size = _hash_file(index_path)
        if digest != manifest:
            raise ArtifactValidationError("selected package root is not declared by the layout")
        self.paths[digest] = index_path
        return {"mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE, "digest": digest, "size": size}

    def attestation(self, descriptor: dict[str, Any], siblings: set[str]) -> None:
        """BuildKit/Docker provenance children: verified bytes, never a platform or process."""
        subject = descriptor.get("annotations", {}).get(_ATTESTATION_SUBJECT)
        if subject is None or subject not in siblings:
            raise ArtifactValidationError("attestation manifest must reference a sibling image manifest")
        if descriptor["mediaType"] not in _IMAGE_CONFIG_FOR_MANIFEST:
            raise ArtifactValidationError("attestation child must be an image manifest")
        document = self.document(descriptor)
        self.schema(document, descriptor["mediaType"])
        config = self.document(self.descriptor(document.get("config")))
        rootfs = config.get("rootfs")
        if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
            raise ArtifactValidationError("attestation config rootfs.type must be layers")
        diff_ids = self.array(rootfs.get("diff_ids"), "attestation DiffIDs")
        layers = [self.descriptor(item) for item in self.array(document.get("layers"), "attestation layers")]
        if len(diff_ids) != len(layers):
            raise ArtifactValidationError("attestation DiffID count differs from its layers")
        for layer, diff_id in zip(layers, diff_ids, strict=True):
            if layer["mediaType"] == _IN_TOTO:
                self.blob(layer)
                actual = layer["digest"]
            else:
                actual = self.layer_diff_id(layer)
            if not isinstance(diff_id, str) or actual != diff_id:
                raise ArtifactValidationError("attestation layer DiffID mismatch")

    def visit(self, raw: Any, depth: int = 0, constraint: dict[str, Any] | None = None) -> None:
        descriptor = self.descriptor(raw)
        digest, media_type = descriptor["digest"], descriptor["mediaType"]
        if depth > self.limits.max_depth or digest in self.active:
            raise ArtifactValidationError("package graph is cyclic or exceeds its depth bound")
        self.active.add(digest)
        try:
            document = self.document(descriptor)
            self.schema(document, media_type)
            if media_type in _INDEX_TYPES:
                children = [self.descriptor(child) for child in self.array(document.get("manifests"), "index manifests", nonempty=True)]
                siblings = {
                    child["digest"]
                    for child in children
                    if child.get("annotations", {}).get(_ATTESTATION_REFERENCE) != "attestation-manifest"
                }
                for child in children:
                    if child.get("annotations", {}).get(_ATTESTATION_REFERENCE) == "attestation-manifest":
                        platform = child.get("platform")
                        if platform is not None and (
                            not isinstance(platform, dict)
                            or platform.get("os") != "unknown"
                            or platform.get("architecture") != "unknown"
                        ):
                            raise ArtifactValidationError("attestation manifest must use the unknown/unknown platform")
                        self.attestation(child, siblings)
                        continue
                    child_constraint = _platform_constraint(child["platform"]) if "platform" in child else constraint
                    if (
                        constraint is not None
                        and child_constraint is not constraint
                        and any(child_constraint.get(key) != value for key, value in constraint.items())
                    ):
                        raise ArtifactValidationError("nested index platform disagrees with its parent")
                    self.visit(child, depth + 1, child_constraint)
                return
            if media_type not in _IMAGE_CONFIG_FOR_MANIFEST:
                raise ArtifactValidationError("unsupported package manifest/index media type")
            config_descriptor = self.descriptor(document.get("config"))
            layers = [self.descriptor(item) for item in self.array(document.get("layers"), "manifest layers")]
            if config_descriptor["mediaType"] == MEDIA_TYPE_LAYER_CONFIG:
                if media_type != OCI_IMAGE_MANIFEST_MEDIA_TYPE:
                    raise ArtifactValidationError("runtime bundles use OCI image manifests")
                self.package_types.add("runtime-bundle")
                leaf = self.document(config_descriptor)
                annotations = _string_map(document.get("annotations", {}), "runtime manifest annotations")
                if annotations.get(ANNOTATION_CHAIN_ID) is not None and annotations[ANNOTATION_CHAIN_ID] != leaf.get("chain_id"):
                    raise ArtifactValidationError("runtime root chain-id annotation contradicts leaf config")
                self.runtime(layers, leaf, depth, constraint)
                return
            if config_descriptor["mediaType"] != _IMAGE_CONFIG_FOR_MANIFEST[media_type]:
                raise ArtifactValidationError("image manifest and config media types use different wire profiles")
            self.package_types.add("oci-image")
            config = self.document(config_descriptor)
            platform = _image_platform(config, constraint)
            if platform not in self.platforms:
                self.platforms.append(platform)
            _process(config.get("config"))
            rootfs = config.get("rootfs")
            if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
                raise ArtifactValidationError("OCI config rootfs.type must be layers")
            diff_ids = self.array(rootfs.get("diff_ids"), "OCI config rootfs.diff_ids")
            if len(diff_ids) != len(layers):
                raise ArtifactValidationError("OCI config DiffID count differs from manifest layers")
            history = config.get("history")
            if history is not None:
                entries = self.array(history, "OCI config history")
                for entry in entries:
                    if not isinstance(entry, dict) or ("empty_layer" in entry and type(entry["empty_layer"]) is not bool):
                        raise ArtifactValidationError("invalid OCI config history entry")
                    if any(key in entry and not isinstance(entry[key], str) for key in ("created", "created_by", "author", "comment")):
                        raise ArtifactValidationError("invalid OCI config history metadata")
                if sum(not entry.get("empty_layer", False) for entry in entries) != len(layers):
                    raise ArtifactValidationError("OCI history nonempty layer count differs from rootfs")
            for layer, diff_id in zip(layers, diff_ids, strict=True):
                if media_type == DOCKER_IMAGE_MANIFEST_MEDIA_TYPE and layer["mediaType"] not in _GZIP_LAYERS:
                    raise ArtifactValidationError("Docker manifest layer wire-profile mismatch")
                if not isinstance(diff_id, str) or self.layer_diff_id(layer) != diff_id:
                    raise ArtifactValidationError("OCI layer DiffID mismatch")
        finally:
            self.active.remove(digest)

    def peek(self, root: dict[str, Any]) -> list[Any] | None:
        """Read candidate root layers for discovery only; trust starts at ``document``."""
        if root["mediaType"] != OCI_IMAGE_MANIFEST_MEDIA_TYPE or root["size"] > self.limits.max_json_bytes:
            return None
        path = self.layout / "blobs" / "sha256" / root["digest"].split(":", 1)[1]
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != root["size"]:
                return None
            layers = strict_json_object(path.read_bytes(), "runtime candidate root").get("layers")
        except (ArtifactValidationError, OSError, RecursionError):
            return None
        return layers if isinstance(layers, list) and layers and all(isinstance(item, dict) for item in layers) else None

    def _layer_config(self, layer: dict[str, Any], leaf: dict[str, Any] | None, prefix: list[dict[str, Any]]) -> dict[str, Any]:
        annotation = layer.get("annotations", {}).get(_CONFIG_DIGEST_ANNOTATION)
        if annotation is not None:
            pin = Descriptor(media_type=MEDIA_TYPE_LAYER_CONFIG, digest=annotation, size=0)
            path = self.layout / "blobs" / "sha256" / pin.digest.split(":", 1)[1]
            if not path.is_file():
                raise ArtifactValidationError("runtime annotated layer config is missing")
            config = self.document({"mediaType": MEDIA_TYPE_LAYER_CONFIG, "digest": pin.digest, "size": path.stat().st_size})
            if leaf is not None and config != leaf:
                raise ArtifactValidationError("runtime leaf config contradicts its annotation")
            return config
        if leaf is not None:
            return leaf
        # The local producer stores an ancestor's config as another root's leaf.
        found: list[dict[str, Any]] = []
        found_roots: set[str] = set()
        for root in self.roots:
            discovered = self.peek(root)
            if discovered is not None and discovered[-1].get("digest") == layer["digest"]:
                candidate = self.document(root)
                candidate_layers = self.array(candidate.get("layers"), "runtime candidate layers", nonempty=True)
                # A candidate is trusted only after its own bytes verify and its
                # chain equals the selected prefix (optionally without a base).
                self.schema(candidate, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
                chain = [self.descriptor(item) for item in candidate_layers]
                without_base = [item for item in prefix if item["mediaType"] not in _RUNTIME_BASES]
                if chain != prefix and chain != without_base:
                    raise ArtifactValidationError("runtime ancestor root chain differs from the selected prefix")
                config_descriptor = self.descriptor(candidate.get("config"))
                if config_descriptor["mediaType"] == MEDIA_TYPE_LAYER_CONFIG:
                    found.append(self.document(config_descriptor))
                    found_roots.add(root["digest"])
        if len(found_roots) != 1 or not found or any(config != found[0] for config in found[1:]):
            raise ArtifactValidationError("runtime ancestor requires exactly one complete config")
        return found[0]

    def runtime(
        self, layers: list[dict[str, Any]], leaf: dict[str, Any], depth: int,
        constraint: dict[str, Any] | None = None,
    ) -> None:
        if not layers or len({layer["digest"] for layer in layers}) != len(layers):
            raise ArtifactValidationError("runtime bundle has an empty or repeated layer chain")
        previous: str | None = None
        previous_chain: str | None = None
        cloud_base: str | None = None
        for ordinal, layer in enumerate(layers):
            self.blob(layer)
            prefix = layers[: ordinal + 1]
            config = self._layer_config(layer, leaf if ordinal == len(layers) - 1 else None, prefix)
            digest, media_type = layer["digest"], layer["mediaType"]
            if config.get("blob_digest", digest) != digest or config.get("media_type", media_type) not in (None, media_type):
                raise ArtifactValidationError("runtime config blob identity mismatch")
            arch = config.get("arch")
            if arch is not None:
                if arch not in ("x86_64", "aarch64"):
                    raise ArtifactValidationError("runtime layer architecture is invalid")
                platform = {"os": "linux", "architecture": "amd64" if arch == "x86_64" else "arm64"}
                if platform not in self.platforms:
                    self.platforms.append(platform)
            if digest in self.runtime_configs and self.runtime_configs[digest] != config:
                raise ArtifactValidationError("runtime layer has inconsistent shared configs")
            self.runtime_configs[digest] = config
            if media_type in _RUNTIME_BASES:
                if (
                    ordinal != 0
                    or config.get("kind") != "cloud-image"
                    or config.get("disk_format") != _RUNTIME_BASES[media_type]
                    or any(config.get(key) for key in ("parent_digest", "chain_id", "base_image_digest"))
                ):
                    raise ArtifactValidationError("runtime cloud base is invalid")
                if arch is None:
                    raise ArtifactValidationError("runtime cloud base architecture is required")
                cloud_base = digest
                continue
            if (
                media_type != MEDIA_TYPE_LAYER_SQUASHFS
                or config.get("kind", "squashfs") != "squashfs"
                or config.get("parent_digest") != previous
            ):
                raise ArtifactValidationError("runtime layer parent/config/media type mismatch")
            if digest in self.runtime_parents and self.runtime_parents[digest] != previous:
                raise ArtifactValidationError("runtime bundle has contradictory parent chains")
            self.runtime_parents[digest] = previous
            chain = (
                digest
                if previous_chain is None
                else "sha256:" + hashlib.sha256(f"{previous_chain} {digest}".encode()).hexdigest()
            )
            if config.get("chain_id") is not None and config["chain_id"] != chain:
                raise ArtifactValidationError("runtime chain_id mismatch")
            base = config.get("base_image_digest")
            if base is not None:
                Descriptor(media_type="application/octet-stream", digest=base, size=0)
                if cloud_base is not None and base != cloud_base:
                    raise ArtifactValidationError("runtime layers disagree on their cloud base")
                if base not in self.runtime_configs:
                    candidates = [
                        root
                        for root in self.roots
                        if (discovered := self.peek(root)) is not None
                        and len(discovered) == 1
                        and discovered[0].get("digest") == base
                    ]
                    if len(candidates) != 1:
                        raise ArtifactValidationError("runtime base_image_digest lacks one complete base graph")
                    self.visit(candidates[0], depth + 1)
                base_config = self.runtime_configs.get(base, {})
                if base_config.get("kind") != "cloud-image" or config.get("arch", base_config.get("arch")) != base_config.get("arch"):
                    raise ArtifactValidationError("runtime layer and cloud base architectures differ")
                cloud_base = base
            previous, previous_chain = digest, chain
        if constraint is not None:
            arch = leaf.get("arch")
            if arch is None and cloud_base is not None:
                arch = self.runtime_configs[cloud_base].get("arch")
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch)
            if constraint["os"] != "linux" or constraint["architecture"] != architecture:
                raise ArtifactValidationError("runtime index/config platform mismatch")


def _absolute_source(source: os.PathLike[str] | str) -> Path:
    """Pin the leaf without following it; parent aliases such as macOS /tmp resolve."""
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.name or path.name in {".", ".."} or ".." in path.parts:
        raise ArtifactValidationError("package source path must name one file or directory")
    return path.parent.resolve(strict=True) / path.name


@contextmanager
def snapshot_package(
    source: os.PathLike[str] | str, manifest: str | None = None, *, limits: PackageLimits | None = None
) -> Iterator[PackageSnapshot]:
    """Freeze, verify and yield one package archive (deleted on exit)."""
    limits = limits or PackageLimits()
    if manifest is not None:
        Descriptor(media_type="application/octet-stream", digest=manifest, size=0)
    path = _absolute_source(source)
    with tempfile.TemporaryDirectory(prefix="palimpsest-package-") as temporary:
        private = Path(temporary).resolve(strict=True)
        os.chmod(private, 0o700)
        layout = private / "layout"
        layout.mkdir(mode=0o700)
        archive = private / "package.tar"
        try:
            kind = os.stat(path, follow_symlinks=False).st_mode
        except OSError:
            raise ArtifactValidationError("package source does not exist") from None
        if stat.S_ISDIR(kind):
            _snapshot_directory(path, layout, limits)
            _write_deterministic_tar(layout, archive)
        elif stat.S_ISREG(kind):
            with _open_absolute_regular_file(path) as (fd, _metadata):
                _copy_fd(fd, archive, limits.max_archive_bytes)
            _extract_archive(archive, layout, limits)
        else:
            raise ArtifactValidationError("package source must be a regular tar file or OCI layout directory")
        archive_digest, archive_size = _hash_file(archive)
        if archive_size > limits.max_archive_bytes:
            raise ArtifactValidationError("package archive exceeds its byte limit")
        graph = _Graph(layout, limits)
        root = graph.select_root(manifest)
        graph.visit(root)
        if len(graph.package_types) != 1:
            raise ArtifactValidationError("package graph mixes OCI image and runtime-bundle content")
        os.chmod(archive, 0o400)
        yield PackageSnapshot(
            archive=archive,
            archive_digest=archive_digest,
            archive_size_bytes=archive_size,
            root_digest=root["digest"],
            root_media_type=root["mediaType"],
            package_type=next(iter(graph.package_types)),
            platforms=tuple(graph.platforms),
            graph=dict(graph.verified),
        )
