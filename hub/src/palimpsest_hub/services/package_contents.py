"""Independent, bounded validation of native package and BuildKit cache archives.

No extraction uses archive paths. A private operation directory holds verified
original bytes; only that directory is removed on failure (or after cache checks).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import zstandard

from palimpsest_hub.services.hub_bundle import (
    ANNOTATION_CHAIN_ID,
    ANNOTATION_CONFIG_DIGEST,
    BundleError,
    BundleLimitError,
    parse_bundle,
)
from palimpsest_hub.services.hub_store import (
    DISK_FORMAT_MEDIA_TYPES,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
)

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
CACHE_CONFIG = "application/vnd.buildkit.cacheconfig.v0"
IN_TOTO = "application/vnd.in-toto+json"
INDEX_TYPES = {OCI_INDEX, DOCKER_INDEX}
MANIFEST_TYPES = {OCI_MANIFEST, DOCKER_MANIFEST}
RAW_TYPES = {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.nondistributable.v1.tar"}
GZIP_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
}
ZSTD_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+zstd",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
}
LAYER_TYPES = RAW_TYPES | GZIP_TYPES | ZSTD_TYPES
ATTESTATION_REFERENCE = "vnd.docker.reference.type"
ATTESTATION_SUBJECT = "vnd.docker.reference.digest"
MAX_MEMBERS = 4096
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_JSON_NODES = 65536
MAX_DEPTH = 64
MAX_EDGES = 32768
MAX_EXTENSION_BYTES = 64 * 1024
MAX_CACHE_BINDING_BYTES = 64 * 1024
MAX_PROCESS_ENTRIES = 8192
MAX_PROCESS_BYTES = 256 * 1024
MAX_PROCESS_STRING_BYTES = 32 * 1024
# python-zstandard takes bytes; 128 MiB equals libzstd's default decoder cap.
MAX_ZSTD_WINDOW_BYTES = 1 << 27
CHUNK_BYTES = 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BLOB_NAME = re.compile(r"blobs/sha256/[0-9a-f]{64}\Z")
_KEYSTONE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
_STOP_SIGNAL = re.compile(r"(?:(?:SIG)?[A-Za-z][A-Za-z0-9]{0,15}(?:[+-][0-9]{1,2})?|[0-9]{1,2})\Z")
# PAX records that only carry metadata; anything changing names, links, sizes
# or sparse payload layout is either interpreted below or rejected.
_PAX_METADATA = {"mtime", "atime", "ctime", "uid", "gid", "uname", "gname", "comment"}
_PAX_METADATA_PREFIXES = ("SCHILY.xattr.", "LIBARCHIVE.xattr.")


class PackageContentError(ValueError):
    """Malformed, inconsistent, unsupported, or unverifiable package content (HTTP 422)."""


class PackageContentLimitError(PackageContentError):
    """Content exceeded an explicit parser, member, JSON, or expanded-byte bound (HTTP 413)."""


@dataclass(frozen=True)
class ValidatedPackage:
    root_digest: str
    root_media_type: str
    graph: dict[str, dict]
    platforms: list[dict[str, str]]
    blob_paths: dict[str, Path]
    total_bytes: int
    package_type: str


@dataclass(frozen=True)
class _Member:
    offset: int
    size: int


@contextmanager
def _content_errors(label: str) -> Iterator[None]:
    """Translate decoder/parser failures; staging I/O and programming errors stay distinct."""
    try:
        yield
    except PackageContentError:
        raise
    except BundleLimitError as exc:
        raise PackageContentLimitError(f"{label} exceeds runtime bundle limit: {exc}") from exc
    except BundleError as exc:
        raise PackageContentError(f"invalid runtime bundle: {exc}") from exc
    except (zstandard.ZstdError, gzip.BadGzipFile, zlib.error, EOFError) as exc:
        raise PackageContentError(f"{label} contains an invalid compressed stream") from exc


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PackageContentError("digest must be canonical sha256:<64 lowercase hex>")
    return value


def _optional_digest(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PackageContentError(f"{label} must be null or a canonical sha256 digest")
    return value


def _limits(blob: int, expanded: int) -> None:
    if type(blob) is not int or blob < 1 or type(expanded) is not int or expanded < 1:
        raise PackageContentError("blob and expanded limits must be positive integers")


def _json(payload: bytes, label: str) -> dict:
    if len(payload) > MAX_JSON_BYTES:
        raise PackageContentLimitError(f"{label} exceeds JSON byte limit ({MAX_JSON_BYTES})")

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise PackageContentError(f"{label} contains duplicate JSON key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise PackageContentError(f"{label} contains nonfinite JSON number")

    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except PackageContentError:
        raise
    except RecursionError as exc:
        raise PackageContentLimitError(f"{label} exceeds JSON depth limit") from exc
    except (UnicodeError, ValueError) as exc:
        raise PackageContentError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise PackageContentError(f"{label} must be a JSON object")
    pending = [(document, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_DEPTH:
            raise PackageContentLimitError(f"{label} exceeds JSON depth/node limit")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return document


def _safe_name(value: str, *, directory: bool = False) -> str:
    name = value.removesuffix("/") if directory else value
    if not name or len(name.encode("utf-8")) > 256 or "\\" in name or "\0" in name:
        raise PackageContentError("unsafe archive member name")
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise PackageContentError("unsafe archive member path")
    return name


def _pax(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    offset = 0
    while offset < len(payload):
        separator = payload.find(b" ", offset)
        if separator < 0 or separator - offset > 8:
            raise PackageContentError("invalid PAX record length")
        length_text = payload[offset:separator]
        if not length_text.isdigit():
            raise PackageContentError("invalid PAX record length")
        end = offset + int(length_text)
        if end > len(payload) or end <= separator + 1 or payload[end - 1:end] != b"\n":
            raise PackageContentError("truncated PAX record")
        key, equal, value = payload[separator + 1:end - 1].partition(b"=")
        if not equal:
            raise PackageContentError("invalid PAX record")
        try:
            key_text, value_text = key.decode("utf-8"), value.decode("utf-8")
        except UnicodeError as exc:
            raise PackageContentError("invalid PAX UTF-8") from exc
        if key_text in result:
            raise PackageContentError("duplicate PAX field")
        if key_text not in {"path", "size"} | _PAX_METADATA and not key_text.startswith(_PAX_METADATA_PREFIXES):
            raise PackageContentError(f"unsupported PAX field: {key_text}")
        result[key_text] = value_text
        offset = end
    return result


def _scan(plain: Path, *, max_blob: int, max_expanded: int, cache: bool) -> dict[str, _Member]:
    """Index regular members by physical offset; links/specials/GNU extensions fail."""
    members: dict[str, _Member] = {}
    seen: set[str] = set()
    pending: dict[str, str] | None = None
    size = plain.stat().st_size
    if size > max_expanded:
        raise PackageContentLimitError("archive exceeds expanded byte limit")
    count = 0
    with plain.open("rb") as handle:
        offset = 0
        while offset < size:
            header = handle.read(512)
            if len(header) != 512:
                raise PackageContentError("truncated tar header")
            if not any(header):
                if pending is not None or size - offset < 1024:
                    raise PackageContentError("incomplete tar termination")
                while chunk := handle.read(CHUNK_BYTES):
                    if any(chunk):
                        raise PackageContentError("data after tar terminator")
                return members
            count += 1
            if count > MAX_MEMBERS:
                raise PackageContentLimitError(f"archive exceeds member limit ({MAX_MEMBERS})")
            try:
                info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            except (tarfile.HeaderError, UnicodeError, ValueError) as exc:
                raise PackageContentError("invalid tar header/checksum") from exc
            if info.size < 0:
                raise PackageContentError("negative tar member size")
            if info.type == tarfile.XHDTYPE:
                if pending is not None:
                    raise PackageContentError("chained PAX extension")
                if info.size > MAX_EXTENSION_BYTES:
                    raise PackageContentLimitError("PAX extension exceeds limit")
                next_offset = offset + 512 + info.size + (-info.size % 512)
                if next_offset > size:
                    raise PackageContentError("truncated PAX extension")
                pending = _pax(handle.read(info.size))
                handle.seek(next_offset)
                offset = next_offset
                continue
            if not info.isdir() and info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                raise PackageContentError("archive links/special files/global or GNU extensions are forbidden")
            name = _safe_name((pending or {}).get("path", info.name), directory=info.isdir())
            declared_size = (pending or {}).get("size")
            member_size = info.size
            if declared_size is not None:
                if not declared_size.isdigit() or len(declared_size) > 20:
                    raise PackageContentError("invalid PAX size")
                member_size = int(declared_size)
            pending = None
            if name in seen:
                raise PackageContentError("duplicate archive member")
            seen.add(name)
            if cache:
                valid = name == "palimpsest-cache.json" or name == "cache" or name.startswith("cache/")
                local_name = name.removeprefix("cache/")
            else:
                valid = name != "palimpsest-cache.json"
                local_name = name
            if info.isdir():
                # BuildKit OCI exports and local cache layouts carry layout directory entries.
                allowed = {"cache", "blobs", "blobs/sha256", "ingest"} if cache else {"blobs", "blobs/sha256"}
                if not valid or member_size or local_name not in allowed:
                    raise PackageContentError("unexpected archive directory")
            elif not valid or not (local_name in {"oci-layout", "index.json", "palimpsest-cache.json"} or _BLOB_NAME.fullmatch(local_name)):
                raise PackageContentError("unexpected archive member")
            elif member_size > max_blob:
                raise PackageContentLimitError("archive member exceeds blob byte limit")
            elif local_name in {"oci-layout", "index.json", "palimpsest-cache.json"} and member_size > MAX_JSON_BYTES:
                raise PackageContentLimitError("archive metadata exceeds JSON byte limit")
            else:
                members[name] = _Member(offset + 512, member_size)
            next_offset = offset + 512 + member_size + (-member_size % 512)
            if next_offset > size:
                raise PackageContentError("truncated tar member payload")
            handle.seek(next_offset)
            offset = next_offset
    raise PackageContentError("missing tar terminator")


def _skip(source: BinaryIO, count: int) -> None:
    while count:
        chunk = source.read(min(CHUNK_BYTES, count))
        if not chunk:
            raise PackageContentError("truncated zstd stream")
        count -= len(chunk)


def _zstd_frames(source: BinaryIO) -> None:
    """Reject truncated/garbage frames that a streaming decoder could end silently.

    Reads only frame/block headers sequentially (skippable frames such as
    zstd:chunked TOCs are allowed); the bounded decoder verifies block data and
    checksums. No decompressed allocation is made here.
    """
    frames = 0
    while magic := source.read(4):
        frames += 1
        if len(magic) != 4:
            raise PackageContentError("truncated zstd frame magic")
        if magic[1:] == b"\x2a\x4d\x18" and magic[0] & 0xF0 == 0x50:
            length = source.read(4)
            if len(length) != 4:
                raise PackageContentError("truncated zstd skippable frame")
            _skip(source, int.from_bytes(length, "little"))
            continue
        if magic != b"\x28\xb5\x2f\xfd":
            raise PackageContentError("invalid zstd frame magic")
        flag = source.read(1)
        if len(flag) != 1 or flag[0] & 0x08:
            raise PackageContentError("invalid zstd frame header")
        descriptor = flag[0]
        single = bool(descriptor & 0x20)
        header_bytes = (0 if single else 1) + (0, 1, 2, 4)[descriptor & 3]
        header_bytes += (1 if single else 0, 2, 4, 8)[descriptor >> 6]
        if len(source.read(header_bytes)) != header_bytes:
            raise PackageContentError("truncated zstd frame header")
        while True:
            block_header = source.read(3)
            if len(block_header) != 3:
                raise PackageContentError("truncated zstd block header")
            block = int.from_bytes(block_header, "little")
            block_type, block_size = (block >> 1) & 3, block >> 3
            if block_type == 3 or block_size > 128 * 1024:
                raise PackageContentError("invalid zstd block")
            _skip(source, 1 if block_type == 1 else block_size)
            if block & 1:
                break
        if descriptor & 4 and len(source.read(4)) != 4:
            raise PackageContentError("truncated zstd checksum")
    if not frames:
        raise PackageContentError("empty zstd stream")


def _zstd_reader(source: BinaryIO) -> Any:
    return zstandard.ZstdDecompressor(max_window_size=MAX_ZSTD_WINDOW_BYTES).stream_reader(
        source, read_across_frames=True, closefd=False
    )


def _plain_archive(source: Path, work: Path, limit: int) -> Path:
    with source.open("rb") as handle:
        prefix = handle.read(4)
    if not prefix.startswith(b"\x1f\x8b") and prefix != b"\x28\xb5\x2f\xfd":
        if source.stat().st_size > limit:
            raise PackageContentLimitError("archive exceeds expanded byte limit")
        return source
    destination = work / "archive.tar"
    total = 0
    with source.open("rb") as compressed:
        if prefix == b"\x28\xb5\x2f\xfd":
            _zstd_frames(compressed)
            compressed.seek(0)
            stream: Any = _zstd_reader(compressed)
        else:
            stream = gzip.GzipFile(fileobj=compressed, mode="rb")
        with stream, destination.open("xb") as output:
            while chunk := stream.read(CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise PackageContentLimitError("archive decompression exceeds expanded byte limit")
                output.write(chunk)
    return destination


class _Slice(io.RawIOBase):
    """Exact-length read-only view of one tar payload without copying it."""

    def __init__(self, handle: BinaryIO, offset: int, size: int) -> None:
        handle.seek(offset)
        self.handle, self.remaining = handle, size

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self.remaining:
            return 0
        data = self.handle.read(min(len(buffer), self.remaining))
        if not data:
            raise PackageContentError("truncated archive blob")
        buffer[: len(data)] = data
        self.remaining -= len(data)
        return len(data)


class _Graph:
    """Selected-graph walker. ``copy`` stages verified original bytes; cache checks hash in place."""

    def __init__(self, plain: Path, members: dict[str, _Member], work: Path, max_blob: int, max_expanded: int, *, prefix: str = "", copy: bool = True):
        self.plain, self.members, self.work = plain, members, work
        self.max_blob, self.max_expanded, self.prefix, self.copy = max_blob, max_expanded, prefix, copy
        self.graph: dict[str, dict] = {}
        self.paths: dict[str, Path] = {}
        self.platforms: list[dict[str, str]] = []
        self.expanded = sum(member.size for member in members.values())
        self.edges = 0
        self.active: set[str] = set()
        self.diff_ids: dict[str, str] = {}
        self._layout_roots: dict[str, list[tuple[dict, dict]]] | None = None

    def _member(self, digest: str) -> _Member | None:
        return self.members.get(self.prefix + "blobs/sha256/" + digest[7:])

    def member_json(self, name: str) -> dict:
        member = self.members.get(self.prefix + name)
        if member is None:
            raise PackageContentError(f"missing archive metadata: {name}")
        with self.plain.open("rb") as stream:
            stream.seek(member.offset)
            return _json(stream.read(member.size), name)

    def descriptor(self, value: Any) -> dict:
        self.edges += 1
        if self.edges > MAX_EDGES:
            raise PackageContentLimitError("descriptor edge limit exceeded")
        if not isinstance(value, dict):
            raise PackageContentError("descriptor must be an object")
        _digest(value.get("digest"))
        if type(value.get("size")) is not int or value["size"] < 0:
            raise PackageContentError("descriptor size must be a nonnegative integer")
        if value["size"] > self.max_blob:
            raise PackageContentLimitError("descriptor size exceeds blob byte limit")
        if not isinstance(value.get("mediaType"), str) or not value["mediaType"]:
            raise PackageContentError("descriptor mediaType is required")
        if "data" in value:
            raise PackageContentError("embedded descriptor data is unsupported")
        urls = value.get("urls", [])
        if not isinstance(urls, list) or any(not isinstance(item, str) for item in urls):
            raise PackageContentError("descriptor urls must be strings")
        annotations = value.get("annotations", {})
        if not isinstance(annotations, dict) or any(not isinstance(item, str) for item in annotations.values()):
            raise PackageContentError("descriptor annotations must be a string map")
        return value

    def blob(self, value: Any) -> None:
        """Verify one reachable blob's original bytes; the archive name alone is never identity."""
        descriptor = self.descriptor(value)
        digest, media, size = descriptor["digest"], descriptor["mediaType"], descriptor["size"]
        expected = {"media_type": media, "size_bytes": size}
        if digest in self.graph:
            if self.graph[digest] != expected:
                raise PackageContentError("conflicting descriptor identity")
            return
        member = self._member(digest)
        if member is None:
            raise PackageContentError("reachable blob missing (external URLs are never fetched)")
        if member.size != size:
            raise PackageContentError("descriptor size differs from archive member")
        hasher = hashlib.sha256()
        path = self.work / digest[7:]
        with self.plain.open("rb") as handle:
            source = _Slice(handle, member.offset, size)
            if self.copy:
                with path.open("xb") as output:
                    while chunk := source.read(CHUNK_BYTES):
                        hasher.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            else:
                while chunk := source.read(CHUNK_BYTES):
                    hasher.update(chunk)
        if "sha256:" + hasher.hexdigest() != digest:
            if self.copy:
                path.unlink()
            raise PackageContentError("original blob digest mismatch")
        self.graph[digest] = expected
        if self.copy:
            self.paths[digest] = path

    @contextmanager
    def open_blob(self, descriptor: dict) -> Iterator[BinaryIO]:
        self.blob(descriptor)
        digest = descriptor["digest"]
        if self.copy:
            with self.paths[digest].open("rb") as handle:
                yield handle
        else:
            member = self._member(digest)
            with self.plain.open("rb") as handle:
                yield io.BufferedReader(_Slice(handle, member.offset, member.size))

    def document(self, descriptor: dict) -> dict:
        if self.descriptor(descriptor)["size"] > MAX_JSON_BYTES:
            raise PackageContentLimitError("JSON blob exceeds byte limit")
        with self.open_blob(descriptor) as handle:
            return _json(handle.read(), "descriptor JSON")

    def layout(self) -> dict:
        if self.member_json("oci-layout").get("imageLayoutVersion") != "1.0.0":
            raise PackageContentError("unsupported OCI layout version")
        index = self.member_json("index.json")
        self.schema(index, OCI_INDEX)
        self.array(index.get("manifests"), "layout manifests", nonempty=True)
        return index

    @staticmethod
    def schema(document: dict, media: str) -> None:
        if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
            raise PackageContentError("manifest/index schemaVersion must be 2")
        if document.get("mediaType", media) != media:
            raise PackageContentError("document mediaType differs from descriptor")
        if document.get("subject") is not None or document.get("artifactType") is not None:
            raise PackageContentError("artifact/subject graphs are not image packages")
        annotations = document.get("annotations", {})
        if not isinstance(annotations, dict) or any(not isinstance(value, str) for value in annotations.values()):
            raise PackageContentError("document annotations must be a string map")

    @staticmethod
    def array(value: Any, label: str, *, nonempty: bool = False) -> list:
        if not isinstance(value, list) or (nonempty and not value):
            raise PackageContentError(f"{label} must be a {'nonempty ' if nonempty else ''}array")
        if len(value) > MAX_MEMBERS:
            raise PackageContentLimitError(f"{label} exceeds array limit ({MAX_MEMBERS})")
        return value

    def layout_roots(self, layout: dict) -> dict[str, list[tuple[dict, dict]]]:
        """Index unselected OCI layout roots once by last-layer digest (discovery only)."""
        if self._layout_roots is None:
            roots: dict[str, list[tuple[dict, dict]]] = {}
            for raw in layout["manifests"]:
                descriptor = self.descriptor(raw)
                if descriptor["mediaType"] != OCI_MANIFEST:
                    continue
                member = self._member(descriptor["digest"])
                if member is None or member.size != descriptor["size"] or member.size > MAX_JSON_BYTES:
                    continue
                with self.plain.open("rb") as source:
                    source.seek(member.offset)
                    candidate = _json(source.read(member.size), "layout root candidate")
                layers = candidate.get("layers")
                if isinstance(layers, list) and layers and isinstance(layers[-1], dict) and isinstance(layers[-1].get("digest"), str):
                    roots.setdefault(layers[-1]["digest"], []).append((descriptor, candidate))
            self._layout_roots = roots
        return self._layout_roots

    def root(self, index: dict, digest: str) -> dict:
        matches = [self.descriptor(item) for item in index["manifests"] if isinstance(item, dict) and item.get("digest") == digest]
        if not matches:
            # An explicitly chosen layout index is itself a legitimate root.
            member = self.members[self.prefix + "index.json"]
            with self.plain.open("rb") as source:
                source.seek(member.offset)
                payload = source.read(member.size)
            if "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
                raise PackageContentError("selected root is not a layout root descriptor")
            path = self.work / digest[7:]
            with path.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            self.graph[digest] = {"media_type": OCI_INDEX, "size_bytes": len(payload)}
            self.paths[digest] = path
            return {"digest": digest, "size": len(payload), "mediaType": OCI_INDEX}
        if any(item != matches[0] for item in matches[1:]):
            raise PackageContentError("ambiguous selected root descriptors")
        return matches[0]

    def layer_diff_id(self, descriptor: dict) -> str:
        digest = descriptor["digest"]
        if digest in self.diff_ids:
            self.blob(descriptor)
            return self.diff_ids[digest]
        media = descriptor["mediaType"]
        if media not in LAYER_TYPES:
            raise PackageContentError(f"unsupported OCI layer media type: {media}")
        if media in ZSTD_TYPES:
            with self.open_blob(descriptor) as source:
                _zstd_frames(source)
        hasher = hashlib.sha256()
        with self.open_blob(descriptor) as source:
            if media in GZIP_TYPES:
                # GzipFile restarts on concatenated members (multi-member layers).
                stream: Any = gzip.GzipFile(fileobj=source, mode="rb")
            elif media in ZSTD_TYPES:
                stream = _zstd_reader(source)
            else:
                stream = source
            while chunk := stream.read(CHUNK_BYTES):
                if media not in RAW_TYPES:
                    self.expanded += len(chunk)
                    if self.expanded > self.max_expanded:
                        raise PackageContentLimitError("layer decompression exceeds expanded byte limit")
                hasher.update(chunk)
            if stream is not source:
                stream.close()
        result = "sha256:" + hasher.hexdigest()
        self.diff_ids[digest] = result
        return result

    def attestation(self, descriptor: dict, siblings: set[str]) -> None:
        """BuildKit/Docker provenance children: verified opaque bytes, never a runnable platform."""
        subject = descriptor["annotations"].get(ATTESTATION_SUBJECT)
        if subject is None or _digest(subject) not in siblings:
            raise PackageContentError("attestation manifest must reference a sibling image manifest")
        document = self.document(descriptor)
        self.schema(document, descriptor["mediaType"])
        if descriptor["mediaType"] not in MANIFEST_TYPES:
            raise PackageContentError("attestation child must be an image manifest")
        config = self.document(self.descriptor(document.get("config")))
        rootfs = config.get("rootfs")
        if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
            raise PackageContentError("attestation config rootfs.type must be layers")
        diff_ids = self.array(rootfs.get("diff_ids"), "attestation DiffIDs")
        layers = self.array(document.get("layers"), "attestation layers")
        if len(diff_ids) != len(layers):
            raise PackageContentError("attestation DiffID count differs from layer count")
        for layer, diff_id in zip(layers, diff_ids, strict=True):
            layer = self.descriptor(layer)
            if layer["mediaType"] == IN_TOTO:
                self.blob(layer)
                actual = layer["digest"]
            else:
                actual = self.layer_diff_id(layer)
            if actual != _digest(diff_id):
                raise PackageContentError("attestation layer DiffID mismatch")

    def image(self, descriptor: dict, expected_platform: dict | None = None, depth: int = 0) -> None:
        digest = self.descriptor(descriptor)["digest"]
        if depth > MAX_DEPTH:
            raise PackageContentLimitError("descriptor graph exceeds depth limit")
        if digest in self.active:
            raise PackageContentError("cyclic descriptor graph")
        self.active.add(digest)
        try:
            media = descriptor["mediaType"]
            document = self.document(descriptor)
            self.schema(document, media)
            if media in INDEX_TYPES:
                children = [self.descriptor(child) for child in self.array(document.get("manifests"), "index manifests", nonempty=True)]
                siblings = {child["digest"] for child in children if child.get("annotations", {}).get(ATTESTATION_REFERENCE) != "attestation-manifest"}
                for child in children:
                    if child.get("annotations", {}).get(ATTESTATION_REFERENCE) == "attestation-manifest":
                        platform = child.get("platform")
                        if platform is not None and (not isinstance(platform, dict) or platform.get("os") != "unknown" or platform.get("architecture") != "unknown"):
                            raise PackageContentError("attestation manifest must use unknown/unknown platform")
                        self.attestation(child, siblings)
                        continue
                    platform = _platform_constraint(child["platform"]) if "platform" in child else expected_platform
                    if expected_platform is not None and platform is not None and any(platform.get(key) != value for key, value in expected_platform.items()):
                        raise PackageContentError("nested index platform mismatch")
                    self.image(child, platform, depth + 1)
                return
            if media not in MANIFEST_TYPES:
                raise PackageContentError(f"unsupported image manifest/index media type: {media}")
            config_descriptor = self.descriptor(document.get("config"))
            expected_config = OCI_CONFIG if media == OCI_MANIFEST else DOCKER_CONFIG
            if config_descriptor["mediaType"] != expected_config:
                raise PackageContentError("manifest/config wire-profile mismatch")
            config = self.document(config_descriptor)
            platform = _platform(config)
            if expected_platform is not None:
                for key, value in expected_platform.items():
                    # Optional platform metadata is commonly carried only on
                    # an ARM/Windows index descriptor. Absence is not a conflict.
                    if key not in {"os", "architecture"} and key not in config:
                        if key != "os.features":
                            platform[key] = value
                        continue
                    if config.get(key) != value:
                        raise PackageContentError("index/config platform mismatch")
            if platform not in self.platforms:
                self.platforms.append(platform)
            _process(config.get("config"))
            rootfs = config.get("rootfs")
            if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
                raise PackageContentError("config rootfs.type must be layers")
            diff_ids = self.array(rootfs.get("diff_ids"), "rootfs DiffIDs")
            layers = self.array(document.get("layers"), "image layers")
            if len(diff_ids) != len(layers):
                raise PackageContentError("rootfs DiffID count differs from layer count")
            history = config.get("history")
            if history is not None:
                history = self.array(history, "config history")
                for entry in history:
                    if not isinstance(entry, dict) or ("empty_layer" in entry and type(entry["empty_layer"]) is not bool):
                        raise PackageContentError("invalid config history entry")
                    for key in ("created", "created_by", "author", "comment"):
                        if key in entry and not isinstance(entry[key], str):
                            raise PackageContentError("invalid config history metadata")
                if sum(not entry.get("empty_layer", False) for entry in history) != len(layers):
                    raise PackageContentError("history nonempty layer count differs from rootfs")
            for layer, diff_id in zip(layers, diff_ids, strict=True):
                layer = self.descriptor(layer)
                if media == DOCKER_MANIFEST and layer["mediaType"] not in GZIP_TYPES:
                    raise PackageContentError("Docker manifest layer wire-profile mismatch")
                if self.layer_diff_id(layer) != _digest(diff_id):
                    raise PackageContentError("uncompressed layer DiffID mismatch")
        finally:
            self.active.remove(digest)


def _platform(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PackageContentError("platform must be an object")
    result: dict[str, str] = {}
    for key in ("os", "architecture", "variant", "os.version"):
        item = value.get(key)
        if key in {"os", "architecture"} or item is not None:
            if not isinstance(item, str) or not item or "\0" in item or len(item.encode()) > 256:
                raise PackageContentError("invalid platform metadata")
            result[key] = item
    features = value.get("os.features", [])
    if not isinstance(features, list) or any(not isinstance(item, str) or not item for item in features):
        raise PackageContentError("invalid platform features")
    # The public platform DTO contains only string-valued fields.
    return result


def _platform_constraint(value: Any) -> dict:
    return {**_platform(value), "os.features": value.get("os.features", [])}


def _process(value: Any) -> None:
    """Structural process metadata only; runtime account/signal policy belongs to the run path."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise PackageContentError("config process metadata must be an object or null")
    count, total = 0, 0

    def text(item: Any, label: str) -> None:
        nonlocal total
        if not isinstance(item, str) or "\0" in item:
            raise PackageContentError(f"invalid process {label}")
        encoded = len(item.encode("utf-8", "surrogatepass"))
        if encoded > MAX_PROCESS_STRING_BYTES:
            raise PackageContentLimitError(f"process {label} exceeds string byte limit")
        total += encoded + 1

    for field in ("Entrypoint", "Cmd", "Env", "Shell", "OnBuild"):
        items = value.get(field)
        if items is None:
            continue
        if not isinstance(items, list):
            raise PackageContentError(f"process {field} must be an array or null")
        for item in items:
            text(item, field)
            count += 1
            if field == "Env" and (not isinstance(item, str) or "=" not in item or item.startswith("=")):
                raise PackageContentError("process Env entries must be NAME=value")
    for field in ("WorkingDir", "User", "StopSignal"):
        if value.get(field) is not None:
            text(value[field], field)
    stop = value.get("StopSignal")
    # image-spec: a signal name ("SIGRTMIN+3", Docker also "TERM") or an
    # unsigned number. Syntax only; which signals a runtime honours is run policy.
    if stop and _STOP_SIGNAL.fullmatch(stop) is None:
        raise PackageContentError("process StopSignal must be a signal name or number")
    if stop and stop.isdecimal() and not 1 <= int(stop) <= 64:
        raise PackageContentError("process StopSignal number must be 1-64")
    for field in ("ExposedPorts", "Volumes", "Labels"):
        mapping = value.get(field)
        if mapping is not None and not isinstance(mapping, dict):
            raise PackageContentError(f"process {field} must be an object or null")
        if field == "Labels" and mapping:
            for key, item in mapping.items():
                text(key, "label")
                text(item, "label")
                count += 1
    if value.get("ArgsEscaped") is not None and type(value["ArgsEscaped"]) is not bool:
        raise PackageContentError("process ArgsEscaped must be boolean or null")
    if count > MAX_PROCESS_ENTRIES or total > MAX_PROCESS_BYTES:
        raise PackageContentLimitError("process metadata exceeds count/byte limit")


def _runtime_config(config: dict, digest: str, media: str) -> None:
    if config.get("blob_digest", digest) != digest:
        raise PackageContentError("runtime config blob identity mismatch")
    if config.get("media_type", media) not in (None, media):
        raise PackageContentError("runtime config media type mismatch")
    for key in ("parent_digest", "chain_id", "base_image_digest"):
        _optional_digest(config.get(key), f"runtime config {key}")
    for key in ("kind", "disk_format", "arch", "name"):
        if config.get(key) is not None and not isinstance(config[key], str):
            raise PackageContentError(f"runtime config {key} must be a string")
    if config.get("arch") is not None and config["arch"] not in _ARCHITECTURES:
        raise PackageContentError("invalid runtime architecture")


def _runtime_ancestor_config(graph: _Graph, layout: dict, prefix: list[dict]) -> dict:
    """Local bundles encode ancestor configs as another layout root's leaf."""
    candidates = graph.layout_roots(layout).get(prefix[-1]["digest"], [])
    if len({descriptor["digest"] for descriptor, _ in candidates}) != 1:
        raise PackageContentError("runtime ancestor requires one config annotation or unambiguous layout-root config")
    descriptor = candidates[0][0]
    verified = graph.document(descriptor)
    graph.schema(verified, OCI_MANIFEST)
    ancestor_layers = graph.array(verified.get("layers"), "runtime ancestor layers", nonempty=True)
    without_base = [item for item in prefix if item["mediaType"] not in DISK_FORMAT_MEDIA_TYPES.values()]
    if ancestor_layers != prefix and ancestor_layers != without_base:
        raise PackageContentError("runtime ancestor root contradicts selected parent chain")
    config_descriptor = graph.descriptor(verified.get("config"))
    if config_descriptor["mediaType"] != MEDIA_TYPE_LAYER_CONFIG:
        raise PackageContentError("runtime ancestor config media type mismatch")
    return graph.document(config_descriptor)


def _write_projection(graph: _Graph, path: Path, json_members: dict[str, bytes], opaque: list[str]) -> None:
    """PAX tar for parse_bundle: JSON copied, large blobs as sparse holes (sizes/offsets only)."""
    with path.open("xb") as output:
        def header(name: str, size: int) -> None:
            info = tarfile.TarInfo(name)
            info.size = size
            output.write(info.tobuf(tarfile.PAX_FORMAT, "utf-8", "strict"))

        for name, payload in json_members.items():
            header(name, len(payload))
            output.write(payload + b"\0" * (-len(payload) % 512))
        for digest in opaque:
            size = graph.graph[digest]["size_bytes"]
            header("blobs/sha256/" + digest[7:], size)
            output.seek(size + (-size % 512), os.SEEK_CUR)
        output.write(b"\0" * 1024)


def _runtime(graph: _Graph, root: dict, layout: dict) -> None:
    manifests: list[tuple[dict, dict | None]] = []

    def visit(descriptor: dict, depth: int = 0, expected_platform: dict | None = None) -> None:
        descriptor = graph.descriptor(descriptor)
        if "platform" in descriptor:
            platform = _platform_constraint(descriptor["platform"])
            if expected_platform is not None and any(platform.get(key) != value for key, value in expected_platform.items()):
                raise PackageContentError("nested runtime index platform mismatch")
            expected_platform = platform
        digest = descriptor["digest"]
        if depth > MAX_DEPTH:
            raise PackageContentLimitError("runtime graph exceeds depth limit")
        if digest in graph.active:
            raise PackageContentError("cyclic runtime graph")
        graph.active.add(digest)
        try:
            document = graph.document(descriptor)
            graph.schema(document, descriptor["mediaType"])
            if descriptor["mediaType"] in INDEX_TYPES:
                for child in graph.array(document.get("manifests"), "runtime index", nonempty=True):
                    visit(child, depth + 1, expected_platform)
            elif descriptor["mediaType"] == OCI_MANIFEST:
                manifests.append((document, expected_platform))
            else:
                raise PackageContentError(f"unsupported runtime root media type: {descriptor['mediaType']}")
        finally:
            graph.active.remove(digest)

    visit(root)
    selected: list[tuple[dict, dict, list[dict], dict | None]] = []
    bases: dict[str, tuple[dict, dict]] = {}
    configs: dict[str, dict] = {}
    for manifest, expected_platform in manifests:
        leaf_descriptor = graph.descriptor(manifest.get("config"))
        if leaf_descriptor["mediaType"] != MEDIA_TYPE_LAYER_CONFIG:
            raise PackageContentError("runtime manifest config media type mismatch")
        leaf = graph.document(leaf_descriptor)
        annotated_chain = manifest.get("annotations", {}).get(ANNOTATION_CHAIN_ID)
        if annotated_chain is not None and annotated_chain != leaf.get("chain_id"):
            raise PackageContentError("runtime root chain-id annotation contradicts leaf config")
        layers = [graph.descriptor(raw) for raw in graph.array(manifest.get("layers"), "runtime layers", nonempty=True)]
        chain: list[dict] = []
        seen: set[str] = set()
        for ordinal, descriptor in enumerate(layers):
            digest = descriptor["digest"]
            if digest in seen:
                raise PackageContentError("cyclic/repeated runtime layer")
            seen.add(digest)
            media = descriptor["mediaType"]
            if media != MEDIA_TYPE_LAYER_SQUASHFS and media not in DISK_FORMAT_MEDIA_TYPES.values():
                raise PackageContentError(f"runtime package layer media type is not runnable bundle content: {media}")
            graph.blob(descriptor)
            annotation = descriptor.get("annotations", {}).get(ANNOTATION_CONFIG_DIGEST)
            if annotation is None:
                config = leaf if ordinal == len(layers) - 1 else _runtime_ancestor_config(graph, layout, layers[:ordinal + 1])
            else:
                config_digest = _digest(annotation)
                member = graph._member(config_digest)
                if member is None:
                    raise PackageContentError("missing runtime annotated config")
                config = graph.document({"digest": config_digest, "size": member.size, "mediaType": MEDIA_TYPE_LAYER_CONFIG})
            _runtime_config(config, digest, media)
            if digest in configs and configs[digest] != config:
                raise PackageContentError("inconsistent shared runtime config")
            configs[digest] = config
            if media in DISK_FORMAT_MEDIA_TYPES.values():
                if chain or ordinal != 0:
                    raise PackageContentError("runtime cloud base must precede all layers")
                if config.get("kind") != "cloud-image" or DISK_FORMAT_MEDIA_TYPES.get(config.get("disk_format")) != media:
                    raise PackageContentError("runtime cloud base kind/disk format mismatch")
                if any(config.get(key) for key in ("parent_digest", "chain_id", "base_image_digest")):
                    raise PackageContentError("runtime cloud base cannot have parent/chain/base")
                if config.get("arch") is None:
                    raise PackageContentError("runtime cloud base architecture is required")
                bases[digest] = (descriptor, config)
            else:
                if config.get("kind", "squashfs") != "squashfs":
                    raise PackageContentError("runtime layer kind mismatch")
                chain.append(descriptor)
            if config.get("arch") is not None:
                platform = {"os": "linux", "architecture": _ARCHITECTURES[config["arch"]]}
                if platform not in graph.platforms:
                    graph.platforms.append(platform)
        if leaf != configs[layers[-1]["digest"]]:
            raise PackageContentError("runtime leaf config contradicts layer annotation")
        selected.append((manifest, leaf_descriptor, chain, expected_platform))

    # base_image_digest is a real graph edge: find its root in the layout, but
    # do not include unrelated runtime roots.
    needed_bases = {config["base_image_digest"] for config in configs.values() if config.get("base_image_digest") is not None}
    for base_digest in needed_bases - bases.keys():
        candidates = [item for item in graph.layout_roots(layout).get(base_digest, []) if len(item[1]["layers"]) == 1]
        if len({descriptor["digest"] for descriptor, _ in candidates}) != 1:
            raise PackageContentError("runtime base_image_digest requires one complete base graph")
        descriptor = candidates[0][0]
        # Discovery bytes confer no identity. Re-read only through the
        # digest/size-verified staged descriptor before following edges.
        verified = graph.document(descriptor)
        verified_layers = [graph.descriptor(item) for item in graph.array(verified.get("layers"), "base root layers", nonempty=True)]
        if len(verified_layers) != 1 or verified_layers[0]["digest"] != base_digest:
            raise PackageContentError("runtime base discovery differs from verified root")
        if verified_layers[0]["mediaType"] not in DISK_FORMAT_MEDIA_TYPES.values():
            raise PackageContentError("runtime base reference does not identify a cloud image")
        _runtime(graph, descriptor, layout)
        bases[base_digest] = (verified_layers[0], graph.document(graph.descriptor(verified.get("config"))))

    for manifest, _, _, expected_platform in selected:
        if expected_platform is None:
            continue
        leaf = configs[manifest["layers"][-1]["digest"]]
        arch = leaf.get("arch")
        if arch is None and leaf.get("base_image_digest") in bases:
            arch = bases[leaf["base_image_digest"]][1].get("arch")
        if arch is None:
            arch = next((bases[item["digest"]][1].get("arch") for item in manifest["layers"] if item["digest"] in bases), None)
        if expected_platform["os"] != "linux" or expected_platform["architecture"] != _ARCHITECTURES.get(arch):
            raise PackageContentError("runtime index/config platform mismatch")

    json_members: dict[str, bytes] = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}
    projected_descriptors: list[dict] = []
    projected_paths: set[str] = set()
    for manifest, leaf_descriptor, chain, _ in selected:
        if not chain:
            continue
        previous: str | None = None
        previous_chain: str | None = None
        expected_base: str | None = None
        projected_paths.add(leaf_descriptor["digest"])
        for descriptor in chain:
            digest = descriptor["digest"]
            projected_paths.add(digest)
            annotated = descriptor.get("annotations", {}).get(ANNOTATION_CONFIG_DIGEST)
            if annotated is not None:
                projected_paths.add(annotated)
            config = configs[digest]
            if config.get("parent_digest") != previous:
                raise PackageContentError("runtime parent_digest differs from ordered chain")
            declared_base = config.get("base_image_digest")
            if declared_base is not None:
                if declared_base not in bases:
                    raise PackageContentError("runtime base is not a verified cloud image")
                if expected_base is not None and declared_base != expected_base:
                    raise PackageContentError("runtime layers disagree on cloud base")
                expected_base = declared_base
            chain_id = digest if previous_chain is None else "sha256:" + hashlib.sha256(f"{previous_chain} {digest}".encode()).hexdigest()
            if config.get("chain_id") is not None and config["chain_id"] != chain_id:
                raise PackageContentError("runtime chain_id mismatch")
            arch = config.get("arch")
            if arch is not None and expected_base is not None and arch != bases[expected_base][1].get("arch"):
                raise PackageContentError("runtime layer/base architecture mismatch")
            previous, previous_chain = digest, chain_id
        # Operation-local projection (base excluded) for hub_bundle's existing
        # parent/config invariants. Never published; originals stay untouched.
        payload = json.dumps(dict(manifest, layers=chain, config=leaf_descriptor), separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        name = "blobs/sha256/" + digest[7:]
        if name not in json_members:
            json_members[name] = payload
            projected_descriptors.append({"digest": digest, "size": len(payload), "mediaType": OCI_MANIFEST})
    if not projected_descriptors:
        return
    opaque: list[str] = []
    for digest in sorted(projected_paths):
        name = "blobs/sha256/" + digest[7:]
        if name in json_members:
            continue
        if graph.graph[digest]["media_type"] == MEDIA_TYPE_LAYER_CONFIG:
            json_members[name] = graph.paths[digest].read_bytes()
        else:
            opaque.append(digest)
    json_members["index.json"] = json.dumps({"schemaVersion": 2, "manifests": projected_descriptors}).encode()
    projection = graph.work / f"runtime-validation-{root['digest'][7:]}.tar"
    try:
        _write_projection(graph, projection, json_members, opaque)
        parse_bundle(projection, max_blob_bytes=graph.max_blob, max_expanded_bytes=graph.max_expanded)
    finally:
        projection.unlink(missing_ok=True)


def validate_package_archive(archive_path: Path, staging_dir: Path, *, package_type: str, root_digest: str, max_blob_bytes: int, max_expanded_bytes: int) -> ValidatedPackage:
    """Validate and stage exactly the original selected reachable package graph."""
    _limits(max_blob_bytes, max_expanded_bytes)
    _digest(root_digest)
    if package_type not in {"oci-image", "runtime-bundle"}:
        raise PackageContentError("unsupported package type (cache is not a package)")
    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="package-content-", dir=staging_dir))
    try:
        with _content_errors("package archive"):
            plain = _plain_archive(archive_path, work, max_expanded_bytes)
            members = _scan(plain, max_blob=max_blob_bytes, max_expanded=max_expanded_bytes, cache=False)
            graph = _Graph(plain, members, work, max_blob_bytes, max_expanded_bytes)
            layout = graph.layout()
            root = graph.root(layout, root_digest)
            if package_type == "oci-image":
                graph.image(root, _platform_constraint(root["platform"]) if "platform" in root else None)
            else:
                _runtime(graph, root, layout)
        if plain != archive_path:
            plain.unlink()
        return ValidatedPackage(root_digest, root["mediaType"], graph.graph, graph.platforms, graph.paths, sum(item["size_bytes"] for item in graph.graph.values()), package_type)
    except BaseException:
        shutil.rmtree(work)
        raise


def _acyclic(edges: list[list[int]], label: str) -> None:
    states = [0] * len(edges)
    for start in range(len(edges)):
        if states[start]:
            continue
        stack = [(start, False)]
        while stack:
            node, leave = stack.pop()
            if leave:
                states[node] = 2
                continue
            if states[node] == 1:
                raise PackageContentError(f"cyclic {label} graph")
            if states[node] == 2:
                continue
            states[node] = 1
            stack.append((node, True))
            for child in edges[node]:
                if type(child) is not int or not 0 <= child < len(edges):
                    raise PackageContentError(f"invalid {label} index")
                stack.append((child, False))


def _cache_config(graph: _Graph, config: dict, layer_descriptors: dict[str, dict]) -> None:
    """BuildKit cacheconfig.v0 (cache/remotecache/v1/types/spec.go) graph checks.

    ``parent`` is Go ``omitempty``: absent means layer 0, only -1 is a root.
    ``layer``/``link`` have no omitempty, so 0 is meaningful. Record digests
    may legitimately repeat.
    """
    layers = graph.array(config.get("layers", []), "cache layers")
    records = graph.array(config.get("records", []), "cache records")
    layer_edges: list[list[int]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            raise PackageContentError("invalid cache layer record")
        descriptor = layer_descriptors.get(_digest(layer.get("blob")))
        if descriptor is None:
            raise PackageContentError("cache config references missing layer blob")
        parent = layer.get("parent", 0)
        if type(parent) is not int:
            raise PackageContentError("invalid cache parent index")
        layer_edges.append([] if parent == -1 else [parent])
        annotations = layer.get("annotations")
        if annotations is not None:
            if not isinstance(annotations, dict):
                raise PackageContentError("invalid cache layer annotations")
            if "size" in annotations and (type(annotations["size"]) is not int or annotations["size"] != descriptor["size"]):
                raise PackageContentError("cache layer annotation size mismatch")
            if "mediaType" in annotations and annotations["mediaType"] != descriptor["mediaType"]:
                raise PackageContentError("cache layer annotation media type mismatch")
            if annotations.get("diffID") and graph.layer_diff_id(descriptor) != _digest(annotations["diffID"]):
                raise PackageContentError("cache layer DiffID mismatch")
    _acyclic(layer_edges, "cache layer")
    record_edges: list[list[int]] = []
    edge_count = 0
    for record in records:
        if not isinstance(record, dict):
            raise PackageContentError("invalid cache record")
        _digest(record.get("digest"))
        links: list[int] = []
        for inputs in graph.array(record.get("inputs", []), "cache inputs"):
            for item in graph.array(inputs, "cache input group", nonempty=True):
                if not isinstance(item, dict) or type(item.get("link")) is not int or not isinstance(item.get("selector", ""), str):
                    raise PackageContentError("invalid cache input binding")
                links.append(item["link"])
        edge_count += len(links)
        if edge_count > MAX_EDGES:
            raise PackageContentLimitError("cache record edge limit exceeded")
        record_edges.append(links)
        indexes = []
        for result in graph.array(record.get("layers", []), "cache results"):
            if not isinstance(result, dict):
                raise PackageContentError("invalid cache result")
            indexes.append(result.get("layer"))
        for result in graph.array(record.get("chains", []), "cache chains"):
            if not isinstance(result, dict):
                raise PackageContentError("invalid cache chain result")
            indexes.extend(graph.array(result.get("layers"), "cache chain layer indexes", nonempty=True))
        if any(type(index) is not int or not 0 <= index < len(layers) for index in indexes):
            raise PackageContentError("cache result references invalid layer index")
    _acyclic(record_edges, "cache record")


def validate_cache_archive(archive_path: Path, staging_dir: Path, *, expected_binding: dict[str, str], max_blob_bytes: int, max_expanded_bytes: int) -> None:
    """Verify a key-bound cache wrapper and its complete local OCI cache graph."""
    fields = {"project_id", "namespace", "package", "build_key", "cache_scope", "platform", "builder_fingerprint"}
    _limits(max_blob_bytes, max_expanded_bytes)
    if set(expected_binding) != fields or any(not isinstance(value, str) or not value for value in expected_binding.values()):
        raise PackageContentError("expected cache binding must contain exactly seven nonempty fields")
    if _KEYSTONE_ID.fullmatch(expected_binding["project_id"]) is None:
        raise PackageContentError("cache project_id must be an exact bounded Keystone identifier")
    _digest(expected_binding["build_key"])
    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="cache-content-", dir=staging_dir))
    try:
        with _content_errors("cache archive"):
            plain = _plain_archive(archive_path, work, max_expanded_bytes)
            members = _scan(plain, max_blob=max_blob_bytes, max_expanded=max_expanded_bytes, cache=True)
            binding_member = members.get("palimpsest-cache.json")
            if binding_member is None or binding_member.offset != 512:
                raise PackageContentError("cache requires one leading binding descriptor")
            if binding_member.size > MAX_CACHE_BINDING_BYTES:
                raise PackageContentLimitError("cache binding descriptor exceeds byte limit")
            with plain.open("rb") as source:
                source.seek(binding_member.offset)
                binding = _json(source.read(binding_member.size), "cache binding")
            if binding.get("schema") != "palimpsest-buildkit-cache-archive-v1":
                raise PackageContentError("unsupported cache archive binding schema")
            for field, expected in expected_binding.items():
                if binding.get(field) != expected:
                    raise PackageContentError(f"cache {field} binding mismatch")
            # BuildKit containerimage.digest identifies the built image, not its
            # separate cache-export manifest. It is optional client provenance.
            _optional_digest(binding.get("oci_manifest_digest"), "cache oci_manifest_digest")
            graph = _Graph(plain, members, work, max_blob_bytes, max_expanded_bytes, prefix="cache/", copy=False)
            layout = graph.layout()
            cache_roots = layout["manifests"]
            if len(cache_roots) != 1:
                raise PackageContentError("bound cache requires exactly one root")
            root = graph.descriptor(cache_roots[0])
            root_document = graph.document(root)
            graph.schema(root_document, root["mediaType"])
            if root["mediaType"] in MANIFEST_TYPES:
                config_descriptor = graph.descriptor(root_document.get("config"))
                layer_entries = graph.array(root_document.get("layers"), "cache manifest layers")
            elif root["mediaType"] in INDEX_TYPES:
                entries = [graph.descriptor(entry) for entry in graph.array(root_document.get("manifests"), "cache index entries", nonempty=True)]
                configs = [entry for entry in entries if entry["mediaType"] == CACHE_CONFIG]
                if len(configs) != 1:
                    raise PackageContentError("cache index requires exactly one cache config")
                config_descriptor = configs[0]
                layer_entries = [entry for entry in entries if entry["mediaType"] != CACHE_CONFIG]
            else:
                raise PackageContentError(f"unsupported BuildKit cache root media type: {root['mediaType']}")
            if config_descriptor["mediaType"] != CACHE_CONFIG:
                raise PackageContentError("cache root config is not BuildKit cache metadata")
            config = graph.document(config_descriptor)
            layer_descriptors: dict[str, dict] = {}
            for raw in layer_entries:
                descriptor = graph.descriptor(raw)
                if descriptor["mediaType"] not in LAYER_TYPES:
                    raise PackageContentError(f"unsupported BuildKit cache layer media type: {descriptor['mediaType']}")
                diff_id = graph.layer_diff_id(descriptor)
                declared_diff_id = descriptor.get("annotations", {}).get("containerd.io/uncompressed")
                if declared_diff_id is not None and diff_id != _digest(declared_diff_id):
                    raise PackageContentError("cache descriptor DiffID mismatch")
                layer_descriptors[descriptor["digest"]] = descriptor
            _cache_config(graph, config, layer_descriptors)
    finally:
        shutil.rmtree(work)
