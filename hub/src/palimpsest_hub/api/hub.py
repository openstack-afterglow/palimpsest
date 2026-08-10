"""Palimpsest 허브 API — digest 로 레이어를 저장·검색·배포한다.

업로드는 OCI Distribution 의 blob upload(POST/PATCH/PUT)를 `/v2/` 없이 차용한다 —
중단 후 재개가 가능하고 구현자에게 익숙하다. **선언된 digest 는 신뢰하지 않는다**:
완료 시 수신 바이트로 digest 를 재계산해 불일치면 폐기한다(fail-closed).

부모 체인 일괄 다운로드는 `POST /bundles` 가 OCI image-layout tar 로 흘린다.
설계는 `docs/palimpsest.md`.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from palimpsest_hub.auth import get_os_conn, get_token_info, require_admin
from palimpsest_hub.cache import get_redis
from palimpsest_hub.config import get_settings
from palimpsest_hub.database import get_session_factory
from palimpsest_hub.models import PalimpsestHubLayer, PalimpsestHubUpload, PalimpsestImageExport
from palimpsest_hub.rate_limit import limiter
from palimpsest_hub.services.digest import (
    compute_config_digest,
    is_digest_prefix,
    normalize_digest,
    normalize_md5,
)
from palimpsest_hub.services.hub_bundle import (
    BundleError,
    BundleLayer,
    extract_blob,
    iter_bundle_tar,
    parse_bundle,
)
from palimpsest_hub.services.hub_store import (
    DISK_FORMAT_MEDIA_TYPES,
    IMAGE_FORMAT_SPECS,
    KIND_CLOUD_IMAGE,
    MEDIA_TYPE_LAYER_SQUASHFS,
    HubDigestMismatch,
    HubStoreError,
    HubStoreUnavailable,
    LocalPathBlobStore,
    get_blob_store,
    write_upload_stream,
)
from palimpsest_hub.services.image_exports import (
    EXPORT_STATUSES,
    STATUS_COMPLETE,
    ImageExportError,
    enqueue_image_export,
    get_project_export,
    list_project_exports,
    serialize_export,
    soft_delete_project_export,
)

_logger = logging.getLogger(__name__)

router = APIRouter()

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.+\-]{0,63}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
_UBUNTU_BASE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")
_OS_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,63}$")
_PY_VERSION_RE = re.compile(r"^\d+\.\d+$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_MAX_REFS = 32
_MAX_BUNDLE_UPLOAD_BYTES = 64 * 1024 * 1024 * 1024  # 64 GiB
_EXPORT_TOKEN_TTL_SECONDS = 60
_EXPORT_TOKEN_PREFIX = "afterglow:export-dl-token:"


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------


class HubUploadStartRequest(BaseModel):
    """업로드 세션 시작. digest 를 미리 선언하면 이미 있는 콘텐츠는 즉시 완료로 단축된다."""

    digest: str | None = None

    @field_validator("digest")
    @classmethod
    def _check_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_digest(value)
        if normalized is None:
            raise ValueError("digest 는 sha256:<64hex> 형식이어야 합니다")
        return normalized


class HubLayerMeta(BaseModel):
    """업로드 완료 시 함께 등록할 메타.

    `kind='cloud-image'` 면 베이스 cloud image 로 등록된다 — 레이어가 아니라 스택의 출발점이다.
    그 경우 `disk_format` 이 필수이고 `parent_digest`/`chain_id`/`python_version` 은 의미가 없다.
    """

    name: str
    kind: str = "squashfs"
    ubuntu_base: str | None = None
    python_version: str | None = None
    parent_digest: str | None = None
    chain_id: str | None = None
    is_published: bool = False
    # 이 레이어가 어떤 베이스 cloud image 위에서 만들어졌는지. 번들이 베이스까지 함께
    # 담을 수 있게 해 준다("이 스택을 돌리는 데 필요한 전부"를 한 번에 받는다).
    base_image_digest: str | None = None
    # --- kind='cloud-image' 전용 ---
    disk_format: str | None = None  # qcow2 | raw
    arch: str = "x86_64"
    os_variant: str | None = None

    @field_validator("disk_format")
    @classmethod
    def _check_disk_format(cls, value: str | None) -> str | None:
        if value is not None and value not in DISK_FORMAT_MEDIA_TYPES:
            raise ValueError(f"disk_format 은 {sorted(DISK_FORMAT_MEDIA_TYPES)} 중 하나여야 합니다")
        return value

    @field_validator("arch")
    @classmethod
    def _check_arch(cls, value: str) -> str:
        if value not in {"x86_64", "aarch64"}:
            raise ValueError("arch 는 x86_64 또는 aarch64 여야 합니다")
        return value

    @field_validator("os_variant")
    @classmethod
    def _check_os_variant(cls, value: str | None) -> str | None:
        if value is not None and not _OS_VARIANT_RE.match(value):
            raise ValueError("os_variant 형식이 유효하지 않습니다")
        return value

    @model_validator(mode="after")
    def _check_kind_consistency(self):
        if self.kind == KIND_CLOUD_IMAGE:
            if not self.disk_format:
                raise ValueError("cloud-image 는 disk_format(qcow2|raw)이 필요합니다")
            if self.parent_digest or self.chain_id:
                raise ValueError("cloud-image 는 부모 레이어를 가질 수 없습니다 — 스택의 출발점입니다")
            if self.base_image_digest:
                raise ValueError("cloud-image 자신이 베이스입니다 — base_image_digest 를 가질 수 없습니다")
        elif self.disk_format:
            raise ValueError("disk_format 은 kind='cloud-image' 에서만 사용합니다")
        return self

    def resolved_media_type(self) -> str:
        if self.kind == KIND_CLOUD_IMAGE and self.disk_format:
            return DISK_FORMAT_MEDIA_TYPES[self.disk_format]
        return MEDIA_TYPE_LAYER_SQUASHFS

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError("name 은 소문자/숫자/점/플러스/하이픈만 허용하며 64자 이하입니다")
        return value

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if not _KIND_RE.match(value):
            raise ValueError("kind 형식이 유효하지 않습니다")
        return value

    @field_validator("ubuntu_base")
    @classmethod
    def _check_base(cls, value: str | None) -> str | None:
        if value is not None and not _UBUNTU_BASE_RE.match(value):
            raise ValueError("ubuntu_base 형식이 유효하지 않습니다")
        return value

    @field_validator("python_version")
    @classmethod
    def _check_py(cls, value: str | None) -> str | None:
        if value is not None and not _PY_VERSION_RE.match(value):
            raise ValueError("python_version 은 major.minor 형식이어야 합니다")
        return value

    @field_validator("parent_digest", "chain_id", "base_image_digest")
    @classmethod
    def _check_digests(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_digest(value)
        if normalized is None:
            raise ValueError("digest 는 sha256:<64hex> 형식이어야 합니다")
        return normalized


class BundleExportRequest(BaseModel):
    refs: list[str] = Field(..., min_length=1, max_length=_MAX_REFS)
    # 각 leaf 가 선언한 베이스 cloud image 까지 함께 담는다 — 로컬 빌드/실행에 필요한 전부를
    # 한 번에 받기 위한 옵션. 이미지가 수 GB 라 기본값은 False 다.
    include_base_image: bool = False

    @field_validator("refs")
    @classmethod
    def _check_refs(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            digest = normalize_digest(value)
            if digest is None:
                raise ValueError("refs 항목은 sha256:<64hex> 형식이어야 합니다")
            normalized.append(digest)
        return normalized


class HubImageExportRequest(BaseModel):
    image_id: UUID
    disk_format: Literal["raw", "qcow2", "vmdk", "vdi", "vhd", "vhdx"]


class HubImageExportResponse(BaseModel):
    id: str
    source_image_id: str
    source_name: str
    source_disk_format: str
    source_size_bytes: int
    target_disk_format: str
    status: Literal["queued", "downloading", "converting", "finalizing", "complete", "error"]
    progress_pct: int
    error_code: str | None
    error_message: str | None
    blob_digest: str | None
    size_bytes: int | None
    filename: str | None
    download_path: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


def _factory_or_503():
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="DB 연결이 초기화되지 않았습니다")
    return factory


def _store_or_503():
    try:
        return get_blob_store()
    except HubStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _required_project_id(token_info: dict) -> str:
    project_id = _project_id(token_info)
    if not isinstance(project_id, str) or not project_id:
        raise HTTPException(status_code=401, detail="프로젝트 범위가 필요합니다")
    return project_id


def _raise_export_http(exc: ImageExportError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _complete_export_blob(
    row: PalimpsestImageExport,
    store: LocalPathBlobStore,
) -> tuple[str, int, str, str]:
    if row.status != STATUS_COMPLETE:
        raise HTTPException(status_code=409, detail="이미지 내보내기가 아직 완료되지 않았습니다")
    digest = normalize_digest(row.result_blob_digest)
    if digest is None:
        raise HTTPException(status_code=409, detail="완료된 내보내기에 blob 정보가 없습니다")
    try:
        if not store.exists(digest):
            raise HTTPException(status_code=404, detail="내보낸 blob이 저장소에 없습니다")
        size = store.size(digest)
    except HTTPException:
        raise
    except (HubStoreError, OSError) as exc:
        raise HTTPException(status_code=404, detail="내보낸 blob이 저장소에 없습니다") from exc
    if row.result_size_bytes is None or row.result_size_bytes != size:
        raise HTTPException(status_code=409, detail="내보낸 blob의 크기 정보가 일치하지 않습니다")
    payload = serialize_export(row)
    filename = payload["filename"]
    if not isinstance(filename, str):
        raise HTTPException(status_code=409, detail="내보낸 blob의 파일 이름을 만들 수 없습니다")
    format_spec = IMAGE_FORMAT_SPECS.get(row.target_disk_format)
    if format_spec is None:
        raise HTTPException(status_code=409, detail="완료된 내보내기 형식이 유효하지 않습니다")
    media_type = format_spec.media_type
    return digest, size, filename, media_type


def _project_id(token_info: dict) -> str | None:
    return token_info.get("project_id")


def _visible_filter(stmt, token_info: dict):
    """공개(`is_published`) 이거나 사이트 공용(`project_id IS NULL`) 이거나 내 프로젝트 것."""
    project_id = _project_id(token_info)
    return stmt.where(
        PalimpsestHubLayer.is_published.is_(True)
        | PalimpsestHubLayer.project_id.is_(None)
        | (PalimpsestHubLayer.project_id == project_id)
    )


def _layer_dict(row: PalimpsestHubLayer) -> dict[str, Any]:
    return {
        "blob_digest": row.blob_digest,
        "blob_md5": row.blob_md5,
        "size_bytes": row.size_bytes,
        "media_type": row.media_type,
        "disk_format": row.disk_format,
        "arch": row.arch,
        "os_variant": row.os_variant,
        "config_digest": row.config_digest,
        "chain_id": row.chain_id,
        "parent_digest": row.parent_digest,
        "name": row.name,
        "kind": row.kind,
        "ubuntu_base": row.ubuntu_base,
        "python_version": row.python_version,
        "project_id": row.project_id,
        "is_published": row.is_published,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _hub_blob_filename(row: PalimpsestHubLayer) -> str:
    stem = row.name if row.name and _NAME_RE.fullmatch(row.name) else row.blob_digest[len("sha256:") :][:12]
    if row.kind == KIND_CLOUD_IMAGE and row.disk_format in IMAGE_FORMAT_SPECS:
        return f"{stem}.{IMAGE_FORMAT_SPECS[row.disk_format].extension}"
    return f"{stem}.sqsh"


def _blob_response(
    *,
    store: LocalPathBlobStore,
    digest: str,
    total: int,
    media_type: str,
    filename: str,
    range_header: str | None,
    allow_ranges: bool = True,
) -> StreamingResponse:
    start, length, status_code = 0, total, 200
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    }
    if allow_ranges:
        headers["Accept-Ranges"] = "bytes"
    if allow_ranges and range_header:
        match = _RANGE_RE.fullmatch(range_header.strip())
        if not match:
            raise HTTPException(status_code=416, detail="지원하지 않는 Range 형식입니다")
        raw_start, raw_end = match.group(1), match.group(2)
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else total - 1
        else:
            suffix = int(raw_end or 0)
            start = max(0, total - suffix)
            end = total - 1
        if start >= total or end < start:
            raise HTTPException(status_code=416, detail="Range 가 blob 범위를 벗어났습니다")
        end = min(end, total - 1)
        length = end - start + 1
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    headers["Content-Length"] = str(length)
    return StreamingResponse(
        store.iter_blob(digest, start=start, length=length),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


async def _load_visible(session, digest: str, token_info: dict) -> PalimpsestHubLayer:
    row = (
        await session.execute(
            _visible_filter(select(PalimpsestHubLayer), token_info).where(PalimpsestHubLayer.blob_digest == digest)
        )
    ).scalar_one_or_none()
    if row is None:
        # 비공개 항목의 존재 여부를 흘리지 않는다 — 403 이 아니라 404
        raise HTTPException(status_code=404, detail="레이어를 찾을 수 없습니다")
    return row


async def _ancestor_chain(session, row: PalimpsestHubLayer, token_info: dict) -> list[PalimpsestHubLayer]:
    """루트 → 자기 자신 순서. 부모가 허브에 없으면 거기서 끊는다(사이클 방어 포함)."""
    chain: list[PalimpsestHubLayer] = [row]
    seen = {row.blob_digest}
    cursor = row.parent_digest
    while cursor:
        if cursor in seen:
            _logger.warning("[palimpsest_hub] 부모 체인 사이클 감지: %s", cursor)
            break
        seen.add(cursor)
        parent = (
            await session.execute(
                _visible_filter(select(PalimpsestHubLayer), token_info).where(PalimpsestHubLayer.blob_digest == cursor)
            )
        ).scalar_one_or_none()
        if parent is None:
            break
        chain.append(parent)
        cursor = parent.parent_digest
    return list(reversed(chain))


# ---------------------------------------------------------------------------
# 검색 / 조회
# ---------------------------------------------------------------------------


@router.get("/layers")
async def search_hub_layers(
    digest: str | None = Query(None),
    digest_prefix: str | None = Query(None),
    md5: str | None = Query(None, description="보조 검색 키. 무결성 권위는 sha256 이다"),
    chain_id: str | None = Query(None),
    name: str | None = Query(None),
    kind: str | None = Query(None),
    parent_digest: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    token_info: dict = Depends(get_token_info),
) -> list[dict[str, Any]]:
    # 입력 검증이 먼저 — 잘못된 요청은 DB/스토어 상태와 무관하게 422 여야 한다.
    stmt = _visible_filter(select(PalimpsestHubLayer), token_info)

    for value, column, label in (
        (digest, PalimpsestHubLayer.blob_digest, "digest"),
        (chain_id, PalimpsestHubLayer.chain_id, "chain_id"),
        (parent_digest, PalimpsestHubLayer.parent_digest, "parent_digest"),
    ):
        if value is not None:
            normalized = normalize_digest(value)
            if normalized is None:
                raise HTTPException(status_code=422, detail=f"{label} 는 sha256:<64hex> 형식이어야 합니다")
            stmt = stmt.where(column == normalized)

    if digest_prefix is not None:
        if not is_digest_prefix(digest_prefix):
            raise HTTPException(status_code=422, detail="digest_prefix 는 hex 4~64자여야 합니다")
        bare = digest_prefix.strip().lower().removeprefix("sha256:")
        stmt = stmt.where(PalimpsestHubLayer.blob_digest.startswith(f"sha256:{bare}"))

    if md5 is not None:
        normalized_md5 = normalize_md5(md5)
        if normalized_md5 is None:
            raise HTTPException(status_code=422, detail="md5 는 32자리 hex 여야 합니다")
        stmt = stmt.where(PalimpsestHubLayer.blob_md5 == normalized_md5)

    if name is not None:
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=422, detail="name 형식이 유효하지 않습니다")
        stmt = stmt.where(PalimpsestHubLayer.name == name)

    if kind is not None:
        if not _KIND_RE.match(kind):
            raise HTTPException(status_code=422, detail="kind 형식이 유효하지 않습니다")
        stmt = stmt.where(PalimpsestHubLayer.kind == kind)

    factory = _factory_or_503()
    async with factory() as session:
        rows = (await session.execute(stmt.order_by(PalimpsestHubLayer.id.desc()).limit(limit))).scalars().all()
        return [_layer_dict(row) for row in rows]


@router.get("/images")
async def list_hub_images(
    ubuntu_base: str | None = Query(None, description="예: ubuntu-24.04"),
    arch: str | None = Query(None),
    os_variant: str | None = Query(None),
    disk_format: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    token_info: dict = Depends(get_token_info),
) -> list[dict[str, Any]]:
    """베이스 cloud image 목록.

    로컬 빌드 환경이 여기서 이미지를 골라 받아 VM 을 띄우고 그 위에 레이어를 만든다.
    `/layers?kind=cloud-image` 와 같은 데이터지만 이미지 전용 필터를 준다.
    """
    if arch is not None and arch not in {"x86_64", "aarch64"}:
        raise HTTPException(status_code=422, detail="arch 는 x86_64 또는 aarch64 여야 합니다")
    if disk_format is not None and disk_format not in DISK_FORMAT_MEDIA_TYPES:
        raise HTTPException(status_code=422, detail=f"지원 disk_format: {', '.join(IMAGE_FORMAT_SPECS)}")
    if ubuntu_base is not None and not _UBUNTU_BASE_RE.match(ubuntu_base):
        raise HTTPException(status_code=422, detail="ubuntu_base 형식이 유효하지 않습니다")
    if os_variant is not None and not _OS_VARIANT_RE.match(os_variant):
        raise HTTPException(status_code=422, detail="os_variant 형식이 유효하지 않습니다")

    stmt = _visible_filter(select(PalimpsestHubLayer), token_info).where(PalimpsestHubLayer.kind == KIND_CLOUD_IMAGE)
    for value, column in (
        (ubuntu_base, PalimpsestHubLayer.ubuntu_base),
        (arch, PalimpsestHubLayer.arch),
        (os_variant, PalimpsestHubLayer.os_variant),
        (disk_format, PalimpsestHubLayer.disk_format),
    ):
        if value is not None:
            stmt = stmt.where(column == value)

    factory = _factory_or_503()
    async with factory() as session:
        rows = (await session.execute(stmt.order_by(PalimpsestHubLayer.id.desc()).limit(limit))).scalars().all()
        return [_layer_dict(row) for row in rows]


@router.post("/image-exports", status_code=202, response_model=HubImageExportResponse)
@limiter.limit("6/hour")
async def create_image_export(
    request: Request,
    req: HubImageExportRequest,
    conn: Any = Depends(get_os_conn),
    token_info: dict = Depends(get_token_info),
) -> dict[str, Any]:
    _store_or_503()
    try:
        row = await enqueue_image_export(conn, token_info, str(req.image_id), req.disk_format)
    except ImageExportError as exc:
        _raise_export_http(exc)
    return serialize_export(row)


@router.get("/image-exports", response_model=list[HubImageExportResponse])
async def list_image_exports(
    source_image_id: UUID | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    token_info: dict = Depends(get_token_info),
) -> list[dict[str, Any]]:
    if status is not None and status not in EXPORT_STATUSES:
        raise HTTPException(status_code=422, detail="지원하지 않는 내보내기 상태입니다")
    try:
        rows = await list_project_exports(
            _required_project_id(token_info),
            source_image_id=str(source_image_id) if source_image_id else None,
            status=status,
            limit=limit,
        )
    except ImageExportError as exc:
        _raise_export_http(exc)
    return [serialize_export(row) for row in rows]


@router.get("/image-exports/{export_id}", response_model=HubImageExportResponse)
async def get_image_export(
    export_id: UUID,
    token_info: dict = Depends(get_token_info),
) -> dict[str, Any]:
    try:
        row = await get_project_export(_required_project_id(token_info), str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)
    return serialize_export(row)


@router.get("/image-exports/{export_id}/blob")
async def download_image_export_blob(
    export_id: UUID,
    request: Request,
    token_info: dict = Depends(get_token_info),
) -> StreamingResponse:
    try:
        row = await get_project_export(_required_project_id(token_info), str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)
    store = _store_or_503()
    digest, size, filename, media_type = _complete_export_blob(row, store)
    return _blob_response(
        store=store,
        digest=digest,
        total=size,
        media_type=media_type,
        filename=filename,
        range_header=request.headers.get("range"),
    )


@router.post("/image-exports/{export_id}/download-token")
async def create_image_export_download_token(
    export_id: UUID,
    token_info: dict = Depends(get_token_info),
) -> dict[str, Any]:
    project_id = _required_project_id(token_info)
    try:
        row = await get_project_export(project_id, str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)
    digest, _, _, _ = _complete_export_blob(row, _store_or_503())
    token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {"export_id": row.id, "project_id": project_id, "digest": digest},
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        redis = await get_redis()
        await redis.setex(f"{_EXPORT_TOKEN_PREFIX}{token}", _EXPORT_TOKEN_TTL_SECONDS, payload)
    except Exception as exc:
        _logger.warning("이미지 내보내기 다운로드 토큰 저장 실패", exc_info=True)
        raise HTTPException(status_code=503, detail="다운로드 토큰을 만들 수 없습니다") from exc
    return {
        "url": f"/api/v1/palimpsest/hub/image-exports/{row.id}/download?dl_token={token}",
        "expires_in": _EXPORT_TOKEN_TTL_SECONDS,
    }


@router.get("/image-exports/{export_id}/download")
async def download_image_export_with_token(
    export_id: UUID,
    dl_token: str = Query(..., min_length=32, max_length=128),
) -> StreamingResponse:
    try:
        redis = await get_redis()
        raw_payload = await redis.getdel(f"{_EXPORT_TOKEN_PREFIX}{dl_token}")
    except Exception as exc:
        _logger.warning("이미지 내보내기 다운로드 토큰 소비 실패", exc_info=True)
        raise HTTPException(status_code=503, detail="다운로드 토큰을 확인할 수 없습니다") from exc
    if raw_payload is None:
        raise HTTPException(status_code=404, detail="다운로드 토큰이 없거나 만료되었습니다")
    try:
        payload = json.loads(raw_payload)
        project_id = payload["project_id"]
        bound_export_id = payload["export_id"]
        bound_digest = normalize_digest(payload["digest"])
        if not isinstance(project_id, str) or bound_export_id != str(export_id) or bound_digest is None:
            raise ValueError("invalid ticket binding")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="다운로드 토큰이 유효하지 않습니다") from exc

    try:
        row = await get_project_export(project_id, str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)
    store = _store_or_503()
    digest, size, filename, media_type = _complete_export_blob(row, store)
    if digest != bound_digest:
        raise HTTPException(status_code=404, detail="다운로드 토큰이 유효하지 않습니다")
    return _blob_response(
        store=store,
        digest=digest,
        total=size,
        media_type=media_type,
        filename=filename,
        range_header=None,
        allow_ranges=False,
    )


@router.delete("/image-exports/{export_id}", status_code=204)
async def delete_image_export(
    export_id: UUID,
    token_info: dict = Depends(get_token_info),
) -> None:
    try:
        await soft_delete_project_export(_required_project_id(token_info), str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)


@router.get("/layers/{digest}")
async def get_hub_layer(digest: str, token_info: dict = Depends(get_token_info)) -> dict[str, Any]:
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=422, detail="digest 는 sha256:<64hex> 형식이어야 합니다")
    factory = _factory_or_503()
    async with factory() as session:
        row = await _load_visible(session, normalized, token_info)
        chain = await _ancestor_chain(session, row, token_info)
        return {
            **_layer_dict(row),
            "ancestors": [item.blob_digest for item in chain[:-1]],
            "chain_complete": (chain[0].parent_digest is None),
        }


@router.get("/layers/{digest}/ancestors")
async def get_hub_layer_ancestors(digest: str, token_info: dict = Depends(get_token_info)) -> list[dict[str, Any]]:
    """루트 → 자기 자신 순서. 허브에 없는 조상에서 끊기며 `chain_complete` 로 판별한다."""
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=422, detail="digest 는 sha256:<64hex> 형식이어야 합니다")
    factory = _factory_or_503()
    async with factory() as session:
        row = await _load_visible(session, normalized, token_info)
        return [_layer_dict(item) for item in await _ancestor_chain(session, row, token_info)]


@router.get("/layers/{digest}/blob")
async def download_hub_blob(
    digest: str, request: Request, token_info: dict = Depends(get_token_info)
) -> StreamingResponse:
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=422, detail="digest 는 sha256:<64hex> 형식이어야 합니다")
    factory = _factory_or_503()
    store = _store_or_503()
    async with factory() as session:
        row = await _load_visible(session, normalized, token_info)

    if not store.exists(normalized):
        raise HTTPException(status_code=404, detail="blob 이 저장소에 없습니다")

    return _blob_response(
        store=store,
        digest=normalized,
        total=row.size_bytes,
        media_type=row.media_type or MEDIA_TYPE_LAYER_SQUASHFS,
        filename=_hub_blob_filename(row),
        range_header=request.headers.get("range"),
    )


# ---------------------------------------------------------------------------
# 업로드 (POST → PATCH… → PUT)
# ---------------------------------------------------------------------------


@router.post("/uploads")
async def start_upload(req: HubUploadStartRequest, token_info: dict = Depends(get_token_info)) -> dict[str, Any]:
    factory = _factory_or_503()
    store = _store_or_503()

    # 이미 있는 콘텐츠면 업로드 자체를 건너뛴다 — content-addressable 의 이점.
    if req.digest and store.exists(req.digest):
        async with factory() as session:
            existing = (
                await session.execute(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == req.digest))
            ).scalar_one_or_none()
        return {
            "session_id": None,
            "completed": True,
            "blob_digest": req.digest,
            "already_present": True,
            "registered": existing is not None,
        }

    session_id = uuid.uuid4().hex
    store.start_upload(session_id)
    async with factory() as session:
        session.add(
            PalimpsestHubUpload(
                id=session_id,
                declared_digest=req.digest,
                received_bytes=0,
                project_id=_project_id(token_info),
                created_by=token_info.get("user_id"),
            )
        )
        await session.commit()
    return {"session_id": session_id, "completed": False, "received_bytes": 0}


async def _owned_upload(session, session_id: str, token_info: dict) -> PalimpsestHubUpload:
    upload = await session.get(PalimpsestHubUpload, session_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다")
    # 남의 세션에 바이트를 밀어 넣지 못하게 한다(IDOR)
    if upload.project_id is not None and upload.project_id != _project_id(token_info):
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다")
    return upload


@router.patch("/uploads/{session_id}")
async def append_upload(
    session_id: str, request: Request, token_info: dict = Depends(get_token_info)
) -> dict[str, Any]:
    factory = _factory_or_503()
    store = _store_or_503()
    settings = get_settings()

    async with factory() as session:
        upload = await _owned_upload(session, session_id, token_info)
        already = upload.received_bytes

    try:
        total = await write_upload_stream(
            store,
            session_id,
            request.stream(),
            already_received=already,
            max_bytes=settings.palimpsest_hub_max_blob_bytes,
        )
    except HubStoreError as exc:
        async with factory() as session:
            upload = await session.get(PalimpsestHubUpload, session_id)
            if upload is not None:
                await session.delete(upload)
                await session.commit()
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    async with factory() as session:
        upload = await session.get(PalimpsestHubUpload, session_id)
        if upload is not None:
            upload.received_bytes = total
            await session.commit()
    return {"session_id": session_id, "received_bytes": total}


@router.put("/uploads/{session_id}")
async def finalize_upload(
    session_id: str, meta: HubLayerMeta, token_info: dict = Depends(get_token_info)
) -> dict[str, Any]:
    """수신 바이트의 digest 를 재계산해 검증하고 레이어로 등록한다."""
    factory = _factory_or_503()
    store = _store_or_503()

    async with factory() as session:
        upload = await _owned_upload(session, session_id, token_info)
        declared = upload.declared_digest

    try:
        finalized = store.finalize_upload(session_id, declared)
    except HubDigestMismatch as exc:
        async with factory() as session:
            row = await session.get(PalimpsestHubUpload, session_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HubStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    config = {
        "name": meta.name,
        "kind": meta.kind,
        "ubuntu_base": meta.ubuntu_base,
        "python_version": meta.python_version,
        "parent_digest": meta.parent_digest,
        "chain_id": meta.chain_id,
        "blob_digest": finalized.blob_digest,
        "disk_format": meta.disk_format,
        "arch": meta.arch,
        "os_variant": meta.os_variant,
        "base_image_digest": meta.base_image_digest,
    }

    async with factory() as session:
        existing = (
            await session.execute(
                select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == finalized.blob_digest)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                PalimpsestHubLayer(
                    blob_digest=finalized.blob_digest,
                    blob_md5=finalized.blob_md5,
                    size_bytes=finalized.size_bytes,
                    media_type=meta.resolved_media_type(),
                    disk_format=meta.disk_format,
                    arch=meta.arch if meta.kind == KIND_CLOUD_IMAGE else None,
                    os_variant=meta.os_variant,
                    config_digest=compute_config_digest(config),
                    chain_id=meta.chain_id,
                    parent_digest=meta.parent_digest,
                    name=meta.name,
                    kind=meta.kind,
                    ubuntu_base=meta.ubuntu_base,
                    python_version=meta.python_version,
                    config_json=config,
                    project_id=_project_id(token_info),
                    is_published=meta.is_published,
                    created_by=token_info.get("user_id"),
                )
            )
        upload = await session.get(PalimpsestHubUpload, session_id)
        if upload is not None:
            await session.delete(upload)
        await session.commit()

    return {
        "blob_digest": finalized.blob_digest,
        "blob_md5": finalized.blob_md5,
        "size_bytes": finalized.size_bytes,
        "already_present": existing is not None,
    }


@router.delete("/uploads/{session_id}", status_code=204)
async def abort_upload(session_id: str, token_info: dict = Depends(get_token_info)) -> None:
    factory = _factory_or_503()
    store = _store_or_503()
    async with factory() as session:
        upload = await _owned_upload(session, session_id, token_info)
        await session.delete(upload)
        await session.commit()
    store.abort_upload(session_id)


# ---------------------------------------------------------------------------
# 번들 (부모 체인 일괄 다운로드 / 가져오기)
# ---------------------------------------------------------------------------


@router.post("/bundles")
async def export_bundle(req: BundleExportRequest, token_info: dict = Depends(get_token_info)) -> StreamingResponse:
    """요청한 leaf 들의 **부모 체인 전체**를 OCI image-layout tar 로 흘린다."""
    factory = _factory_or_503()
    store = _store_or_503()

    chains: list[list[BundleLayer]] = []
    async with factory() as session:
        for ref in req.refs:
            row = await _load_visible(session, ref, token_info)
            chain_rows = await _ancestor_chain(session, row, token_info)

            if req.include_base_image:
                # 베이스 cloud image 를 체인 맨 앞에 얹는다. 부모 체인의 루트가 선언한
                # base_image_digest 를 쓴다 — 스택 전체가 같은 베이스 위에 있기 때문이다.
                base_digest = normalize_digest((chain_rows[0].config_json or {}).get("base_image_digest") or "")
                if base_digest is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{chain_rows[0].blob_digest} 가 베이스 이미지를 선언하지 않았습니다",
                    )
                base_row = await _load_visible(session, base_digest, token_info)
                if base_row.kind != KIND_CLOUD_IMAGE:
                    raise HTTPException(status_code=409, detail=f"{base_digest} 는 cloud-image 가 아닙니다")
                chain_rows = [base_row, *chain_rows]

            if chain_rows[0].parent_digest is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"부모 체인이 허브에 완전하지 않습니다: {chain_rows[0].parent_digest} 누락",
                )
            for item in chain_rows:
                if not store.exists(item.blob_digest):
                    raise HTTPException(status_code=409, detail=f"blob 이 저장소에 없습니다: {item.blob_digest}")
            chains.append(
                [
                    BundleLayer(
                        blob_digest=item.blob_digest,
                        size_bytes=item.size_bytes,
                        name=item.name,
                        config=dict(item.config_json or {}),
                        # 베이스 cloud image 는 레이어와 mediaType 이 다르다 — 받는 쪽이
                        # qcow2 를 squashfs 로 착각하지 않도록 그대로 싣는다.
                        media_type=item.media_type or MEDIA_TYPE_LAYER_SQUASHFS,
                    )
                    for item in chain_rows
                ]
            )

    try:
        stream = iter_bundle_tar(store, chains)
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StreamingResponse(
        stream,
        media_type="application/x-tar",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="palimpsest-bundle.tar"',
        },
    )


@router.post("/bundles/import")
async def import_bundle(file: UploadFile, token_info: dict = Depends(get_token_info)) -> dict[str, Any]:
    """번들을 받아 blob digest 를 **전부 재검증**한 뒤 허브에 등록한다."""
    factory = _factory_or_503()
    store = _store_or_503()

    imported: list[str] = []
    skipped: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="palimpsest-import-") as tmpdir:
        tmp = Path(tmpdir)
        bundle_path = tmp / "bundle.tar"
        size = 0
        with bundle_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_BUNDLE_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="번들 크기 상한을 초과했습니다")
                out.write(chunk)

        try:
            parsed = parse_bundle(bundle_path)
        except (BundleError, HubStoreError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async with factory() as session:
            for entry in parsed.layers:
                declared = entry["blob_digest"]
                try:
                    staged = tmp / f"blob-{declared[len('sha256:') :][:16]}"
                    extract_blob(bundle_path, parsed.blob_members[declared], staged)
                    finalized = store.ingest_file(staged)
                    staged.unlink(missing_ok=True)
                    # 번들이 선언한 digest 와 실제 바이트가 다르면 받지 않는다(fail-closed).
                    if finalized.blob_digest != declared:
                        store.delete(finalized.blob_digest)
                        skipped.append({"digest": declared, "error": "digest 불일치"})
                        continue
                except (BundleError, HubStoreError, KeyError, OSError) as exc:
                    skipped.append({"digest": declared, "error": str(exc)[:200]})
                    continue

                existing = (
                    await session.execute(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == declared))
                ).scalar_one_or_none()
                if existing is not None:
                    skipped.append({"digest": declared, "error": "이미 허브에 있음"})
                    continue

                config = dict(entry.get("config") or {})
                # 부모는 **manifest layers[] 순서**에서 온 값을 쓴다(parse_bundle 이 채운다).
                # config 는 leaf 것만 실려 오므로 여기서 읽으면 조상이 전부 루트가 된다.
                parent_digest = normalize_digest(entry.get("parent_digest") or "")
                name = config.get("name") or entry.get("name") or ""
                if not _NAME_RE.match(name or ""):
                    skipped.append({"digest": declared, "error": "레이어 이름 형식이 유효하지 않습니다"})
                    continue
                kind = config.get("kind") or "squashfs"
                if not _KIND_RE.match(kind):
                    skipped.append({"digest": declared, "error": "kind 형식이 유효하지 않습니다"})
                    continue

                session.add(
                    PalimpsestHubLayer(
                        blob_digest=declared,
                        blob_md5=finalized.blob_md5,
                        size_bytes=finalized.size_bytes,
                        media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                        config_digest=compute_config_digest(config or {"blob_digest": declared}),
                        chain_id=normalize_digest(config.get("chain_id") or ""),
                        parent_digest=parent_digest,
                        name=name,
                        kind=kind,
                        ubuntu_base=config.get("ubuntu_base"),
                        python_version=config.get("python_version"),
                        config_json=config or {"blob_digest": declared},
                        project_id=_project_id(token_info),
                        is_published=False,
                        created_by=token_info.get("user_id"),
                    )
                )
                imported.append(declared)
            await session.commit()

    return {"imported": imported, "imported_count": len(imported), "skipped": skipped}


# ---------------------------------------------------------------------------
# 삭제
# ---------------------------------------------------------------------------


@router.delete("/layers/{digest}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_hub_layer(digest: str) -> None:
    """관리자 전용. 자식이 있으면 거부하고 blob 정리는 지연 GC에 맡긴다."""
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=422, detail="digest 는 sha256:<64hex> 형식이어야 합니다")
    factory = _factory_or_503()

    async with factory() as session:
        row = (
            await session.execute(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == normalized))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="레이어를 찾을 수 없습니다")
        child = (
            await session.execute(
                select(PalimpsestHubLayer.id).where(PalimpsestHubLayer.parent_digest == normalized).limit(1)
            )
        ).scalar_one_or_none()
        if child is not None:
            raise HTTPException(status_code=409, detail="자식 레이어가 있어 삭제할 수 없습니다")
        await session.delete(row)
        await session.commit()
