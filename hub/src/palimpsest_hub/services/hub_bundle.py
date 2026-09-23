"""부모 체인 일괄 다운로드 — OCI image-layout 번들 export/import.

요구사항 "부모 레이어를 추적하여 한번에 다운로드"를 구현한다. 번들 포맷은
**OCI image-layout**(`oci-layout` + `index.json` + `blobs/sha256/<hex>`)이라
표준 도구로도 열 수 있고, 로컬 KVM 호스트에 그대로 펼치면 곧바로 레이어 경로가 된다.

manifest 는 OCI image manifest 형태를 쓰되 mediaType 만 프로젝트 고유로 둔다:
- config: `application/vnd.afterglow.palimpsest.layer.config.v1+json`
- layer : `application/vnd.afterglow.palimpsest.layer.squashfs.v1`
- `layers[]` 는 **루트→리프 순서**의 부모 체인 전체

덕분에 "부모 추적 일괄 다운로드"가 manifest 하나를 받는 것과 같아지고, 중복 blob 은
콘텐츠 주소이므로 자동으로 한 번만 담긴다.

tar 스트리밍은 `tarfile.addfile` 을 쓰지 않는다 — 그 API 는 큰 blob 을 통째로 버퍼에
올려 메모리를 터뜨린다. 헤더만 `TarInfo.tobuf()` 로 만들고 본문은 청크로 흘린다.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import logging
import lzma
import os
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from palimpsest_hub.services.digest import normalize_digest
from palimpsest_hub.services.hub_store import (
    DISK_FORMAT_MEDIA_TYPES,
    MEDIA_TYPE_BUILDKIT_CACHE,
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
    LocalPathBlobStore,
)

_logger = logging.getLogger(__name__)

OCI_LAYOUT_VERSION = "1.0.0"
MEDIA_TYPE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_INDEX = "application/vnd.oci.image.index.v1+json"
ANNOTATION_NAME = "dev.afterglow.palimpsest.name"
ANNOTATION_CONFIG_DIGEST = "dev.afterglow.palimpsest.config-digest"
ANNOTATION_CHAIN_ID = "dev.afterglow.palimpsest.chain-id"

_MAX_BUNDLE_MEMBERS = 4096
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_TAR_EXTENSION_BYTES = 4 * 1024 * 1024
_DECOMPRESSION_CHUNK_BYTES = 1024 * 1024
_TAR_BLOCK = 512


class BundleError(ValueError):
    """번들 구성/해석 실패."""


class BundleLimitError(BundleError):
    """번들이 명시된 자원 상한을 넘었다."""


def materialize_plain_tar(source: Path, destination: Path, *, max_expanded_bytes: int) -> Path:
    """Return an uncompressed tar, expanding a supported stream into ``destination`` when needed."""
    if type(max_expanded_bytes) is not int or max_expanded_bytes < 1:
        raise ValueError("max_expanded_bytes must be a positive integer")
    try:
        with source.open("rb") as handle:
            prefix = handle.read(_TAR_BLOCK)
    except OSError as exc:
        raise BundleError("번들 파일을 읽을 수 없습니다") from exc

    if prefix.startswith(b"\x1f\x8b"):
        opener = gzip.open
    elif prefix.startswith(b"BZh"):
        opener = bz2.open
    elif prefix.startswith(b"\xfd7zXZ\x00"):
        opener = lzma.open
    elif len(prefix) == _TAR_BLOCK and prefix[257:262] == b"ustar":
        try:
            if source.stat().st_size > max_expanded_bytes:
                raise BundleLimitError("번들 확장 크기가 상한을 넘습니다")
        except OSError as exc:
            raise BundleError("번들 파일을 읽을 수 없습니다") from exc
        return source
    else:
        raise BundleError("지원하지 않는 번들 압축 형식입니다")

    if source == destination:
        raise BundleError("압축 번들 대상은 원본과 달라야 합니다")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        written = 0
        with opener(source, "rb") as compressed, destination.open("xb") as plain:
            while chunk := compressed.read(_DECOMPRESSION_CHUNK_BYTES):
                if written + len(chunk) > max_expanded_bytes:
                    raise BundleLimitError("번들 확장 크기가 상한을 넘습니다")
                plain.write(chunk)
                written += len(chunk)
            plain.flush()
            os.fsync(plain.fileno())
        return destination
    except BundleError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, EOFError, lzma.LZMAError) as exc:
        destination.unlink(missing_ok=True)
        raise BundleError("압축 번들을 풀 수 없습니다") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class BundleLayer:
    """번들에 담을 항목 하나(루트→리프 순서로 전달된다).

    베이스 cloud image 도 여기에 담긴다 — `media_type` 만 다르다. 그래야 "이 스택에 필요한
    전부"(베이스 이미지 + 레이어 체인)를 번들 하나로 받을 수 있다.
    """

    blob_digest: str
    size_bytes: int
    name: str
    config: dict[str, Any]
    media_type: str = MEDIA_TYPE_LAYER_SQUASHFS


# ---------------------------------------------------------------------------
# tar 스트리밍 (헤더만 tarfile, 본문은 직접 흘림)
# ---------------------------------------------------------------------------


def _tar_header(name: str, size: int) -> bytes:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = 0  # 결정적 번들 — 같은 입력이면 같은 바이트
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    return info.tobuf(format=tarfile.PAX_FORMAT)


def _tar_padding(size: int) -> bytes:
    remainder = size % _TAR_BLOCK
    return b"\0" * (_TAR_BLOCK - remainder) if remainder else b""


def _emit_bytes(name: str, payload: bytes) -> Iterator[bytes]:
    yield _tar_header(name, len(payload))
    yield payload
    padding = _tar_padding(len(payload))
    if padding:
        yield padding


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def build_manifest(chain: list[BundleLayer], config_blobs: dict[str, bytes]) -> dict[str, Any]:
    """루트→리프 순서의 체인을 OCI image manifest 로 만든다."""
    if not chain:
        raise BundleError("빈 체인은 manifest 로 만들 수 없습니다")
    leaf = chain[-1]
    config_bytes = config_blobs[leaf.blob_digest]
    return {
        "schemaVersion": 2,
        "mediaType": MEDIA_TYPE_MANIFEST,
        "config": {
            "mediaType": MEDIA_TYPE_LAYER_CONFIG,
            "digest": _config_digest(config_bytes),
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": layer.media_type,
                "digest": layer.blob_digest,
                "size": layer.size_bytes,
                "annotations": {
                    ANNOTATION_NAME: layer.name,
                    ANNOTATION_CONFIG_DIGEST: _config_digest(config_blobs[layer.blob_digest]),
                },
            }
            for layer in chain
        ],
        "annotations": {
            ANNOTATION_NAME: leaf.name,
            **({ANNOTATION_CHAIN_ID: leaf.config["chain_id"]} if leaf.config.get("chain_id") else {}),
        },
    }


def _config_digest(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def iter_bundle_tar(store: LocalPathBlobStore, chains: list[list[BundleLayer]]) -> Iterator[bytes]:
    """OCI image-layout 번들을 tar 스트림으로 흘린다.

    `chains` 는 요청된 leaf 마다 하나씩, 각 원소는 **루트→리프** 순서의 부모 체인 전체다.
    중복 blob(공통 조상)은 콘텐츠 주소이므로 한 번만 담긴다.
    """
    if not chains:
        raise BundleError("번들에 담을 레이어가 없습니다")

    emitted: set[str] = set()
    manifest_descriptors: list[dict[str, Any]] = []

    yield from _emit_bytes("oci-layout", _canonical_json({"imageLayoutVersion": OCI_LAYOUT_VERSION}))

    for chain in chains:
        config_blobs = {layer.blob_digest: _canonical_json(layer.config) for layer in chain}

        # 각 layer descriptor가 자신의 config blob을 참조한다.
        for layer in chain:
            payload = config_blobs[layer.blob_digest]
            digest = _config_digest(payload)
            if digest not in emitted:
                emitted.add(digest)
                yield from _emit_bytes(f"blobs/sha256/{digest[len('sha256:') :]}", payload)

        # layer blob
        for layer in chain:
            if layer.blob_digest in emitted:
                continue
            emitted.add(layer.blob_digest)
            hex_part = layer.blob_digest[len("sha256:") :]
            yield _tar_header(f"blobs/sha256/{hex_part}", layer.size_bytes)
            written = 0
            for chunk in store.iter_blob(layer.blob_digest):
                written += len(chunk)
                yield chunk
            if written != layer.size_bytes:
                # tar 헤더에 선언한 크기와 실제가 다르면 아카이브가 깨진다 — 조용히 넘기지 않는다.
                raise BundleError(f"blob 크기 불일치: {layer.blob_digest} 선언={layer.size_bytes} 실제={written}")
            padding = _tar_padding(layer.size_bytes)
            if padding:
                yield padding

        manifest = build_manifest(chain, config_blobs)
        manifest_bytes = _canonical_json(manifest)
        manifest_digest = _config_digest(manifest_bytes)
        if manifest_digest not in emitted:
            emitted.add(manifest_digest)
            yield from _emit_bytes(f"blobs/sha256/{manifest_digest[len('sha256:') :]}", manifest_bytes)
        manifest_descriptors.append(
            {
                "mediaType": MEDIA_TYPE_MANIFEST,
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "annotations": manifest["annotations"],
            }
        )

    yield from _emit_bytes(
        "index.json",
        _canonical_json({"schemaVersion": 2, "mediaType": MEDIA_TYPE_INDEX, "manifests": manifest_descriptors}),
    )
    yield b"\0" * (_TAR_BLOCK * 2)  # tar 종료 블록


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleBlobMember:
    """A validated regular tar member and the byte offset of its payload."""

    name: str
    size_bytes: int
    data_offset: int


@dataclass
class ParsedBundle:
    """번들에서 읽어낸 것. blob 은 아직 허브에 편입되지 않았다."""

    layers: list[dict[str, Any]]  # 루트→리프 순서, 중복 제거됨
    blob_members: dict[str, BundleBlobMember]  # digest → 검증된 tar 멤버


def _safe_member_name(name: str) -> str:
    """tar 멤버 이름 검증 — 경로 traversal / 절대 경로 거부."""
    if name.startswith("/") or ".." in Path(name).parts:
        raise BundleError(f"안전하지 않은 번들 경로: {name!r}")
    return name


def _parse_tar_size(field: bytes, *, name: str) -> int:
    """Parse the tar size field without accepting a negative or malformed value."""
    if not field:
        raise BundleError(f"tar 멤버 크기가 유효하지 않습니다: {name!r}")
    if field[0] & 0x80:
        # GNU base-256 values reserve the high bit as a marker; 0xff represents
        # a negative value and is never a valid member size.
        if field[0] == 0xFF:
            raise BundleError(f"tar 멤버 크기가 유효하지 않습니다: {name!r}")
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    value = field.rstrip(b"\0 ")
    if not value:
        return 0
    if any(digit not in b"01234567" for digit in value):
        raise BundleError(f"tar 멤버 크기가 유효하지 않습니다: {name!r}")
    return int(value, 8)


def _tar_member_name(header: bytes) -> str:
    name = header[:100].split(b"\0", 1)[0]
    prefix = header[345:500].split(b"\0", 1)[0]
    raw_name = prefix + (b"/" if prefix and name else b"") + name
    return _safe_member_name(raw_name.decode("utf-8", "surrogateescape"))


def _consume_zero_trailer(handle: Any) -> bool:
    while chunk := handle.read(_DECOMPRESSION_CHUNK_BYTES):
        if any(chunk):
            return False
    return True


def _scan_members(tar_path: Path, *, max_member_bytes: int, max_expanded_bytes: int) -> dict[str, BundleBlobMember]:
    """Pre-scan physical tar headers without allowing tarfile to load extensions."""
    by_name: dict[str, BundleBlobMember] = {}
    member_count = 0
    expanded_bytes = 0
    try:
        total_bytes = tar_path.stat().st_size
        offset = 0
        terminated = False
        with tar_path.open("rb") as handle:
            while offset < total_bytes:
                if total_bytes - offset < _TAR_BLOCK:
                    raise BundleError("tar 헤더가 잘렸습니다")
                header = handle.read(_TAR_BLOCK)
                if len(header) != _TAR_BLOCK:
                    raise BundleError("tar 헤더를 읽을 수 없습니다")
                if not any(header):
                    if not _consume_zero_trailer(handle):
                        raise BundleError("tar 종료 블록 뒤에 데이터가 있습니다")
                    terminated = True
                    break

                member_count += 1
                if member_count > _MAX_BUNDLE_MEMBERS:
                    raise BundleLimitError(f"번들 멤버 수가 상한({_MAX_BUNDLE_MEMBERS})을 넘습니다")
                name = _tar_member_name(header)
                size = _parse_tar_size(header[124:136], name=name)
                padded_size = size + (-size % _TAR_BLOCK)
                data_offset = offset + _TAR_BLOCK
                next_offset = data_offset + padded_size
                if next_offset > total_bytes:
                    raise BundleError(f"tar 멤버 payload가 잘렸습니다: {name}")

                typeflag = header[156:157]
                if typeflag in {b"x", b"g", b"L", b"K", b"S"} and size > _MAX_TAR_EXTENSION_BYTES:
                    raise BundleLimitError(f"tar 확장 멤버 크기가 상한을 넘습니다: {name}")
                expanded_bytes += size
                if expanded_bytes > max_expanded_bytes:
                    raise BundleLimitError("번들 확장 크기가 상한을 넘습니다")
                if typeflag in {b"\0", b"0", b"7"}:
                    if size > max_member_bytes:
                        raise BundleLimitError(f"번들 멤버 크기가 상한을 넘습니다: {name}")
                    if name in by_name:
                        raise BundleError(f"번들에 중복 멤버가 있습니다: {name}")
                    by_name[name] = BundleBlobMember(name=name, size_bytes=size, data_offset=data_offset)
                handle.seek(padded_size, os.SEEK_CUR)
                offset = next_offset
        if not terminated:
            raise BundleError("tar 종료 블록이 없습니다")
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError("번들 tar를 읽을 수 없습니다") from exc
    return by_name


def _plain_member_chunks(tar_path: Path, member: BundleBlobMember) -> Iterator[bytes]:
    """Read an already pre-scanned member at its payload offset from a plain tar."""
    try:
        with tar_path.open("rb") as handle:
            handle.seek(member.data_offset)
            remaining = member.size_bytes
            while remaining:
                chunk = handle.read(min(_DECOMPRESSION_CHUNK_BYTES, remaining))
                if not chunk:
                    raise BundleError(f"{member.name} 실제 크기가 tar 헤더와 일치하지 않습니다")
                remaining -= len(chunk)
                yield chunk
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError("번들 tar를 읽을 수 없습니다") from exc


def _read_member_bytes(
    tar_path: Path, member: BundleBlobMember, *, max_member_bytes: int, max_expanded_bytes: int
) -> bytes:
    """Read one already-scanned JSON member directly from its payload offset."""
    if member.size_bytes > max_member_bytes or member.size_bytes > _MAX_JSON_BYTES:
        raise BundleLimitError(f"{member.name} 크기가 상한을 넘습니다")
    if member.size_bytes > max_expanded_bytes:
        raise BundleLimitError("번들 확장 크기가 상한을 넘습니다")
    return b"".join(_plain_member_chunks(tar_path, member))


def _descriptor_member(
    descriptor: Any, by_name: dict[str, BundleBlobMember], *, label: str
) -> tuple[str, BundleBlobMember]:
    if not isinstance(descriptor, dict):
        raise BundleError(f"{label} 형식이 유효하지 않습니다")
    digest = normalize_digest(descriptor.get("digest", ""))
    size = descriptor.get("size")
    if digest is None:
        raise BundleError(f"{label} 의 digest 형식이 유효하지 않습니다")
    if type(size) is not int or size < 0:
        raise BundleError(f"{label} 의 size 형식이 유효하지 않습니다")
    member_name = f"blobs/sha256/{digest[len('sha256:') :]}"
    member = by_name.get(member_name)
    if member is None:
        raise BundleError(f"번들에 blob 이 없습니다: {digest}")
    if member.size_bytes != size:
        raise BundleError(f"{label} 의 선언 크기와 tar 크기가 일치하지 않습니다: {digest}")
    return digest, member


def _read_json(
    tar_path: Path,
    member: BundleBlobMember,
    *,
    max_member_bytes: int,
    max_expanded_bytes: int,
    expected_digest: str | None = None,
) -> Any:
    payload = _read_member_bytes(
        tar_path,
        member,
        max_member_bytes=max_member_bytes,
        max_expanded_bytes=max_expanded_bytes,
    )
    if expected_digest is not None:
        actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if actual != expected_digest:
            raise BundleError(f"{member.name} digest 가 descriptor와 일치하지 않습니다")
    try:
        return json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{member.name} JSON 형식이 유효하지 않습니다") from exc


def _annotated_layer_config(
    tar_path: Path,
    layer_desc: dict[str, Any],
    by_name: dict[str, BundleBlobMember],
    configs_by_digest: dict[str, dict[str, Any]],
    *,
    blob_digest: str,
    parent_digest: str | None,
    max_blob_bytes: int,
    max_expanded_bytes: int,
) -> dict[str, Any] | None:
    """Resolve and validate the optional per-layer config annotation."""
    annotations = layer_desc.get("annotations")
    if annotations is None:
        return None
    if not isinstance(annotations, dict):
        raise BundleError("layer descriptor 의 annotations 형식이 유효하지 않습니다")
    if ANNOTATION_CONFIG_DIGEST not in annotations:
        return None
    raw_config_digest = annotations[ANNOTATION_CONFIG_DIGEST]
    config_digest = normalize_digest(raw_config_digest) if isinstance(raw_config_digest, str) else None
    if config_digest is None:
        raise BundleError("layer descriptor 의 config-digest annotation 형식이 유효하지 않습니다")
    member_name = f"blobs/sha256/{config_digest[len('sha256:') :]}"
    config_member = by_name.get(member_name)
    if config_member is None:
        raise BundleError(f"config-digest annotation blob 이 번들에 없습니다: {config_digest}")
    if config_digest not in configs_by_digest:
        config = _read_json(
            tar_path,
            config_member,
            max_member_bytes=max_blob_bytes,
            max_expanded_bytes=max_expanded_bytes,
            expected_digest=config_digest,
        )
        if not isinstance(config, dict):
            raise BundleError("config-digest annotation JSON 형식이 유효하지 않습니다")
        configs_by_digest[config_digest] = config
    config = configs_by_digest[config_digest]
    _validate_layer_config(config, blob_digest=blob_digest, parent_digest=parent_digest)
    return config


def _validate_layer_config(config: dict[str, Any], *, blob_digest: str, parent_digest: str | None) -> None:
    """Reject config identity fields that contradict this manifest layer."""
    if "blob_digest" in config and config["blob_digest"] != blob_digest:
        raise BundleError(f"config 의 blob_digest 가 layer descriptor 와 일치하지 않습니다: {blob_digest}")
    if "parent_digest" in config and config["parent_digest"] != parent_digest:
        raise BundleError(f"config 의 parent_digest 가 manifest 체인과 일치하지 않습니다: {blob_digest}")


def parse_bundle(tar_path: Path, *, max_blob_bytes: int, max_expanded_bytes: int) -> ParsedBundle:
    """Parse a pre-materialized OCI tar with bounded header and content reads."""
    if type(max_blob_bytes) is not int or max_blob_bytes < 1:
        raise ValueError("max_blob_bytes must be a positive integer")
    if type(max_expanded_bytes) is not int or max_expanded_bytes < 1:
        raise ValueError("max_expanded_bytes must be a positive integer")

    layers_by_digest: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    blob_members: dict[str, BundleBlobMember] = {}
    by_name = _scan_members(
        tar_path,
        max_member_bytes=max_blob_bytes,
        max_expanded_bytes=max_expanded_bytes,
    )
    index_member = by_name.get("index.json")
    if index_member is None:
        raise BundleError("번들에 index.json 이 없습니다")
    index = _read_json(
        tar_path,
        index_member,
        max_member_bytes=max_blob_bytes,
        max_expanded_bytes=max_expanded_bytes,
    )
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list) or not manifests:
        raise BundleError("index.json 에 manifests 가 없습니다")
    if len(manifests) > _MAX_BUNDLE_MEMBERS:
        raise BundleLimitError(f"manifest 수가 상한({_MAX_BUNDLE_MEMBERS})을 넘습니다")

    manifests_by_digest: dict[str, Any] = {}
    configs_by_digest: dict[str, dict[str, Any]] = {}
    allowed_layer_media_types = {
        MEDIA_TYPE_LAYER_SQUASHFS,
        MEDIA_TYPE_BUILDKIT_CACHE,
        *DISK_FORMAT_MEDIA_TYPES.values(),
    }
    for manifest_descriptor in manifests:
        digest, manifest_member = _descriptor_member(manifest_descriptor, by_name, label="manifest descriptor")
        if digest not in manifests_by_digest:
            manifests_by_digest[digest] = _read_json(
                tar_path,
                manifest_member,
                max_member_bytes=max_blob_bytes,
                max_expanded_bytes=max_expanded_bytes,
                expected_digest=digest,
            )
        manifest = manifests_by_digest[digest]
        layer_descriptors = manifest.get("layers") if isinstance(manifest, dict) else None
        if not isinstance(layer_descriptors, list) or not layer_descriptors:
            raise BundleError("manifest 에 layers 가 없습니다")
        if len(layer_descriptors) > _MAX_BUNDLE_MEMBERS:
            raise BundleLimitError(f"manifest layer 수가 상한({_MAX_BUNDLE_MEMBERS})을 넘습니다")

        previous_digest: str | None = None
        for layer_desc in layer_descriptors:
            blob_digest, member = _descriptor_member(layer_desc, by_name, label="layer descriptor")
            media_type = layer_desc.get("mediaType", MEDIA_TYPE_LAYER_SQUASHFS)
            if type(media_type) is not str or media_type not in allowed_layer_media_types:
                raise BundleError(f"layer descriptor 의 mediaType 이 유효하지 않습니다: {media_type!r}")
            annotations = layer_desc.get("annotations")
            name = annotations.get(ANNOTATION_NAME, "") if isinstance(annotations, dict) else ""
            config = _annotated_layer_config(
                tar_path,
                layer_desc,
                by_name,
                configs_by_digest,
                blob_digest=blob_digest,
                parent_digest=previous_digest,
                max_blob_bytes=max_blob_bytes,
                max_expanded_bytes=max_expanded_bytes,
            )
            blob_members[blob_digest] = member
            if blob_digest in layers_by_digest:
                known_layer = layers_by_digest[blob_digest]
                if (
                    known_layer["parent_digest"] != previous_digest
                    or known_layer["media_type"] != media_type
                    or known_layer["name"] != name
                ):
                    raise BundleError(f"번들의 공유 blob descriptor가 모순됩니다: {blob_digest}")
                if config is not None:
                    if "config" in known_layer and known_layer["config"] != config:
                        raise BundleError(f"번들의 공유 blob config가 모순됩니다: {blob_digest}")
                    known_layer["config"] = config
                previous_digest = blob_digest
                continue
            ordered.append(blob_digest)
            layers_by_digest[blob_digest] = {
                "blob_digest": blob_digest,
                "size_bytes": member.size_bytes,
                "media_type": media_type,
                "name": name,
                "parent_digest": previous_digest,
            }
            if config is not None:
                layers_by_digest[blob_digest]["config"] = config
            previous_digest = blob_digest

        config_desc = manifest.get("config") if isinstance(manifest, dict) else None
        if config_desc is not None:
            config_digest, config_member = _descriptor_member(config_desc, by_name, label="config descriptor")
            if config_digest not in configs_by_digest:
                config = _read_json(
                    tar_path,
                    config_member,
                    max_member_bytes=max_blob_bytes,
                    max_expanded_bytes=max_expanded_bytes,
                    expected_digest=config_digest,
                )
                if not isinstance(config, dict):
                    raise BundleError("config JSON 형식이 유효하지 않습니다")
                configs_by_digest[config_digest] = config
            if previous_digest is not None:
                config = configs_by_digest[config_digest]
                _validate_layer_config(
                    config,
                    blob_digest=previous_digest,
                    parent_digest=layers_by_digest[previous_digest]["parent_digest"],
                )
                known_config = layers_by_digest[previous_digest].get("config")
                if known_config is not None and known_config != config:
                    raise BundleError(f"번들의 leaf config가 layer annotation과 모순됩니다: {previous_digest}")
                layers_by_digest[previous_digest]["config"] = config

    return ParsedBundle(layers=[layers_by_digest[digest] for digest in ordered], blob_members=blob_members)


def extract_blob(
    tar_path: Path,
    member: BundleBlobMember | str,
    destination: Path,
    *,
    expected_size: int | None = None,
    max_blob_bytes: int | None = None,
    max_expanded_bytes: int = 107374182400,
) -> None:
    """Extract one bounded blob directly from its verified plain-tar offset."""
    max_member_bytes = max_blob_bytes if max_blob_bytes is not None else max_expanded_bytes
    destination.unlink(missing_ok=True)
    try:
        scanned = _scan_members(
            tar_path,
            max_member_bytes=max_member_bytes,
            max_expanded_bytes=max_expanded_bytes,
        )
        member_name = _safe_member_name(member) if isinstance(member, str) else member.name
        scanned_member = scanned.get(member_name)
        if scanned_member is None:
            raise BundleError(f"번들에 {member_name} 이(가) 없습니다")
        if not isinstance(member, str) and member != scanned_member:
            raise BundleError(f"번들 멤버가 스캔 결과와 일치하지 않습니다: {member.name}")
        required_size = scanned_member.size_bytes if expected_size is None else expected_size
        if type(required_size) is not int or required_size < 0 or required_size != scanned_member.size_bytes:
            raise BundleError(f"blob 선언 크기와 tar 크기가 일치하지 않습니다: {member_name}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with destination.open("xb") as out:
            for chunk in _plain_member_chunks(tar_path, scanned_member):
                written += len(chunk)
                if written > required_size:
                    raise BundleLimitError(f"blob 크기가 상한을 넘습니다: {member_name}")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if written != required_size:
            raise BundleError(f"blob 실제 크기가 tar 헤더와 일치하지 않습니다: {member_name}")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
