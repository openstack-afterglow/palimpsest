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

import io
import json
import logging
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from palimpsest_hub.services.digest import normalize_digest
from palimpsest_hub.services.hub_store import (
    MEDIA_TYPE_LAYER_CONFIG,
    MEDIA_TYPE_LAYER_SQUASHFS,
    HubStoreError,
    LocalPathBlobStore,
)

_logger = logging.getLogger(__name__)

OCI_LAYOUT_VERSION = "1.0.0"
MEDIA_TYPE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_INDEX = "application/vnd.oci.image.index.v1+json"
ANNOTATION_NAME = "dev.afterglow.palimpsest.name"
ANNOTATION_CHAIN_ID = "dev.afterglow.palimpsest.chain-id"

_MAX_BUNDLE_MEMBERS = 4096
_MAX_JSON_BYTES = 4 * 1024 * 1024
_TAR_BLOCK = 512


class BundleError(ValueError):
    """번들 구성/해석 실패."""


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
                "annotations": {ANNOTATION_NAME: layer.name},
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

        # config blob (leaf 것만 manifest 가 참조하지만, 조상 config 도 함께 담아 재구성 가능하게 한다)
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


@dataclass
class ParsedBundle:
    """번들에서 읽어낸 것. blob 은 아직 허브에 편입되지 않았다."""

    layers: list[dict[str, Any]]  # 루트→리프 순서, 중복 제거됨
    blob_members: dict[str, str]  # digest → tar 내부 멤버 이름


def _safe_member_name(name: str) -> str:
    """tar 멤버 이름 검증 — 경로 traversal / 절대 경로 거부."""
    if name.startswith("/") or ".." in Path(name).parts:
        raise BundleError(f"안전하지 않은 번들 경로: {name!r}")
    return name


def parse_bundle(tar_path: Path) -> ParsedBundle:
    """번들 tar 를 해석해 레이어 메타를 뽑는다. blob 편입은 호출자 몫.

    검증: 멤버 수 상한, 경로 traversal 거부, `index.json`/manifest/config 존재,
    layers[] digest 형식. blob 바이트의 digest 재계산은 편입 시 `store.ingest_file` 이 한다.
    """
    layers_by_digest: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    blob_members: dict[str, str] = {}

    with tarfile.open(tar_path, mode="r:*") as tar:
        members = tar.getmembers()
        if len(members) > _MAX_BUNDLE_MEMBERS:
            raise BundleError(f"번들 멤버 수가 상한({_MAX_BUNDLE_MEMBERS})을 넘습니다")

        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            if not member.isfile():
                continue
            by_name[_safe_member_name(member.name)] = member

        if "index.json" not in by_name:
            raise BundleError("번들에 index.json 이 없습니다")

        def _read_json(name: str) -> Any:
            info = by_name.get(name)
            if info is None:
                raise BundleError(f"번들에 {name} 이(가) 없습니다")
            if info.size > _MAX_JSON_BYTES:
                raise BundleError(f"{name} 크기가 상한을 넘습니다")
            handle = tar.extractfile(info)
            if handle is None:
                raise BundleError(f"{name} 을(를) 읽을 수 없습니다")
            return json.loads(io.TextIOWrapper(handle, encoding="utf-8").read())

        index = _read_json("index.json")
        manifests = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(manifests, list) or not manifests:
            raise BundleError("index.json 에 manifests 가 없습니다")

        for descriptor in manifests:
            digest = normalize_digest((descriptor or {}).get("digest", ""))
            if digest is None:
                raise BundleError("manifest descriptor 의 digest 형식이 유효하지 않습니다")
            manifest = _read_json(f"blobs/sha256/{digest[len('sha256:') :]}")
            layer_descriptors = manifest.get("layers") if isinstance(manifest, dict) else None
            if not isinstance(layer_descriptors, list) or not layer_descriptors:
                raise BundleError("manifest 에 layers 가 없습니다")

            # 🔴 부모 체인의 권위는 **manifest 의 layers[] 순서**다(루트→리프).
            # config blob 에서 parent_digest 를 읽으면 안 된다 — manifest 는 leaf 의 config 만
            # 참조하므로 조상들은 config 를 못 받아 전부 루트로 import 되고 체인이 조용히 사라진다.
            previous_digest: str | None = None
            for layer_desc in layer_descriptors:
                blob_digest = normalize_digest((layer_desc or {}).get("digest", ""))
                if blob_digest is None:
                    raise BundleError("layer descriptor 의 digest 형식이 유효하지 않습니다")
                member_name = f"blobs/sha256/{blob_digest[len('sha256:') :]}"
                if member_name not in by_name:
                    raise BundleError(f"번들에 blob 이 없습니다: {blob_digest}")
                blob_members[blob_digest] = member_name
                if blob_digest in layers_by_digest:
                    # 여러 manifest 가 공통 조상을 공유하면 여기로 온다. 이미 기록된 부모가
                    # 이번 체인과 다르면 번들이 모순이다 — 조용히 덮어쓰지 않는다.
                    known_parent = layers_by_digest[blob_digest].get("parent_digest")
                    if known_parent != previous_digest:
                        raise BundleError(
                            f"번들의 부모 체인이 모순됩니다: {blob_digest} 의 부모가 "
                            f"{known_parent} 와 {previous_digest} 로 다릅니다"
                        )
                    previous_digest = blob_digest
                    continue
                ordered.append(blob_digest)
                layers_by_digest[blob_digest] = {
                    "blob_digest": blob_digest,
                    "size_bytes": int((layer_desc or {}).get("size") or 0),
                    "name": ((layer_desc or {}).get("annotations") or {}).get(ANNOTATION_NAME, ""),
                    "parent_digest": previous_digest,
                }
                previous_digest = blob_digest

            # config 는 leaf 것만 manifest 가 참조한다. 메타 보강용으로만 쓰고,
            # parent_digest 는 위에서 정한 값을 신뢰한다.
            config_desc = manifest.get("config") if isinstance(manifest, dict) else None
            config_digest = normalize_digest((config_desc or {}).get("digest", ""))
            if config_digest is not None:
                config_name = f"blobs/sha256/{config_digest[len('sha256:') :]}"
                if config_name in by_name:
                    config = _read_json(config_name)
                    leaf_digest = normalize_digest(layer_descriptors[-1].get("digest", ""))
                    if leaf_digest and leaf_digest in layers_by_digest and isinstance(config, dict):
                        layers_by_digest[leaf_digest]["config"] = config

    return ParsedBundle(
        layers=[layers_by_digest[d] for d in ordered],
        blob_members=blob_members,
    )


def extract_blob(tar_path: Path, member_name: str, destination: Path) -> None:
    """번들에서 blob 하나를 꺼내 임시 파일로 쓴다(편입 전)."""
    with tarfile.open(tar_path, mode="r:*") as tar:
        info = tar.getmember(_safe_member_name(member_name))
        handle = tar.extractfile(info)
        if handle is None:
            raise HubStoreError(f"번들에서 blob 을 읽을 수 없습니다: {member_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            while chunk := handle.read(1024 * 1024):
                out.write(chunk)
