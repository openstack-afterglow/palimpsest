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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from palimpsest_hub.auth import get_os_conn, get_token_info, require_admin
from palimpsest_hub.cache import get_redis
from palimpsest_hub.config import get_settings
from palimpsest_hub.database import get_session_factory
from palimpsest_hub.models import (
    PalimpsestHubLayer,
    PalimpsestHubLayerAccess,
    PalimpsestHubUpload,
    PalimpsestImageExport,
)
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
    base_image_digest: str | None = None
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


class HubLayerResponse(BaseModel):
    blob_digest: str
    blob_md5: str | None = None
    size_bytes: int
    media_type: str
    disk_format: str | None = None
    arch: str | None = None
    os_variant: str | None = None
    config_digest: str
    chain_id: str | None = None
    parent_digest: str | None = None
    name: str
    kind: str
    ubuntu_base: str | None = None
    python_version: str | None = None
    config_json: dict[str, Any]
    project_id: str | None = None
    is_published: bool
    created_by: str | None = None
    created_at: str | None = None


class HubUploadStartResponse(BaseModel):
    session_id: str | None = None
    completed: bool
    received_bytes: int = 0
    blob_digest: str | None = None
    already_present: bool = False
    registered: bool = False


class HubUploadStatusResponse(BaseModel):
    session_id: str
    declared_digest: str | None = None
    received_bytes: int
    project_id: str | None = None
    created_by: str | None = None


class HubUploadAppendResponse(BaseModel):
    session_id: str
    received_bytes: int


class HubUploadFinalizeResponse(BaseModel):
    blob_digest: str
    blob_md5: str
    size_bytes: int
    already_present: bool


class HubImageExportTokenResponse(BaseModel):
    url: str
    expires_in: int


class HubBundleImportResponse(BaseModel):
    imported: list[str]
    imported_count: int
    skipped: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


def _factory_or_503():
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="데이터베이스 세션 팩토리가 준비되지 않았습니다")
    return factory


def _store_or_503():
    try:
        return get_blob_store()
    except HubStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _required_project_id(token_info: dict) -> str:
    project_id = _project_id(token_info)
    if not project_id:
        raise HTTPException(status_code=401, detail="프로젝트 스코프의 인증이 필요합니다")
    return project_id


def _raise_export_http(exc: ImageExportError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _complete_export_blob(row: PalimpsestImageExport, store: LocalPathBlobStore) -> tuple[str, int, str, str]:
    if row.status != STATUS_COMPLETE:
        raise HTTPException(status_code=409, detail="이미지 내보내기가 완료되지 않았습니다")
    digest = normalize_digest(row.result_blob_digest or "")
    if digest is None or not store.exists(digest):
        raise HTTPException(status_code=404, detail="내보내기 blob 이 허브 스토어에 없습니다")
    size = store.size(digest)
    ext = IMAGE_FORMAT_SPECS.get(row.target_disk_format, IMAGE_FORMAT_SPECS["qcow2"]).extension
    stem = row.source_name if _NAME_RE.fullmatch(row.source_name) else row.id[:8]
    filename = f"{stem}.{ext}"
    media_type = DISK_FORMAT_MEDIA_TYPES.get(row.target_disk_format, "application/octet-stream")
    return digest, size, filename, media_type


def _project_id(token_info: dict) -> str | None:
    return token_info.get("project_id")


def _visible_filter(stmt, token_info: dict):
    """공개, 사이트 공용, 소유 프로젝트, 또는 검증된 동일 blob 접근 레이어."""
    project_id = _project_id(token_info)
    shared_access = (
        select(PalimpsestHubLayerAccess.blob_digest)
        .where(
            PalimpsestHubLayerAccess.blob_digest == PalimpsestHubLayer.blob_digest,
            PalimpsestHubLayerAccess.project_id == project_id,
        )
        .exists()
    )
    return stmt.where(
        (PalimpsestHubLayer.is_published.is_(True))
        | (PalimpsestHubLayer.project_id.is_(None))
        | (PalimpsestHubLayer.project_id == project_id)
        | shared_access
    )


async def _grant_layer_access(session, digest: str, token_info: dict) -> None:
    project_id = _required_project_id(token_info)
    access = await session.get(PalimpsestHubLayerAccess, (digest, project_id))
    if access is None:
        session.add(
            PalimpsestHubLayerAccess(
                blob_digest=digest,
                project_id=project_id,
                created_by=token_info.get("user_id"),
            )
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
        "config_json": row.config_json,
        "project_id": row.project_id,
        "is_published": row.is_published,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _hub_blob_filename(row: PalimpsestHubLayer) -> str:
    stem = row.name if row.name and _NAME_RE.fullmatch(row.name) else row.blob_digest[len("sha256:") :][:12]
    if row.kind == KIND_CLOUD_IMAGE and row.disk_format in IMAGE_FORMAT_SPECS:
        ext = IMAGE_FORMAT_SPECS[row.disk_format].extension
        return f"{stem}.{ext}"
    return f"{stem}.sqsh"


def _blob_response(
    store: LocalPathBlobStore,
    digest: str,
    total: int,
    media_type: str,
    filename: str,
    range_header: str | None,
    allow_ranges: bool = True,
) -> StreamingResponse:
    start, length, status_code = 0, total, 200
    headers: dict[str, str] = {
        "Content-Type": media_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if allow_ranges:
        headers["Accept-Ranges"] = "bytes"

    if allow_ranges and range_header and range_header.startswith("bytes="):
        match = _RANGE_RE.match(range_header.strip())
        if match:
            raw_start, raw_end = match.group(1), match.group(2)
            req_start = int(raw_start) if raw_start else 0
            req_end = int(raw_end) if raw_end else total - 1
            if req_start <= req_end and req_start < total:
                start = req_start
                end = min(req_end, total - 1)
                length = end - start + 1
                status_code = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            else:
                raise HTTPException(
                    status_code=416,
                    detail="요청한 Range 가 바이트 범위를 벗어났습니다",
                    headers={"Content-Range": f"bytes */{total}"},
                )

    headers["Content-Length"] = str(length)
    return StreamingResponse(
        store.iter_blob(digest, start=start, length=length),
        status_code=status_code,
        headers=headers,
    )


async def _load_visible(session, digest: str, token_info: dict) -> PalimpsestHubLayer:
    row = (
        await session.execute(
            _visible_filter(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest), token_info)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="레이어를 찾을 수 없습니다")
    return row


async def _ancestor_chain(session, row: PalimpsestHubLayer, token_info: dict) -> list[PalimpsestHubLayer]:
    """루트 → 자기 자신 순서. 부모가 허브에 없으면 거기서 끊기며(사이클 방어 포함)."""
    chain = [row]
    seen = {row.blob_digest}
    current = row
    while current.parent_digest:
        if current.parent_digest in seen:
            break
        parent = (
            await session.execute(
                _visible_filter(
                    select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == current.parent_digest),
                    token_info,
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            break
        seen.add(parent.blob_digest)
        chain.append(parent)
        current = parent
    return list(reversed(chain))


# ---------------------------------------------------------------------------
# 검색 / 조회
# ---------------------------------------------------------------------------


@router.get("/layers", response_model=list[HubLayerResponse], operation_id="search_hub_layers")
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


@router.get("/images", response_model=list[HubLayerResponse], operation_id="list_hub_images")
async def list_hub_images(
    ubuntu_base: str | None = Query(None, description="예: ubuntu-24.04"),
    arch: str | None = Query(None),
    os_variant: str | None = Query(None),
    disk_format: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    token_info: dict = Depends(get_token_info),
) -> list[dict[str, Any]]:
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


@router.post(
    "/image-exports", status_code=202, response_model=HubImageExportResponse, operation_id="create_image_export"
)
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


@router.get("/image-exports", response_model=list[HubImageExportResponse], operation_id="list_image_exports")
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


@router.get("/image-exports/{export_id}", response_model=HubImageExportResponse, operation_id="get_image_export")
async def get_image_export(
    export_id: UUID,
    token_info: dict = Depends(get_token_info),
) -> dict[str, Any]:
    try:
        row = await get_project_export(_required_project_id(token_info), str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)
    return serialize_export(row)


@router.get("/image-exports/{export_id}/blob", operation_id="download_image_export_blob")
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


@router.post(
    "/image-exports/{export_id}/download-token",
    response_model=HubImageExportTokenResponse,
    operation_id="create_image_export_download_token",
)
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
        "url": f"/v1/image-exports/{row.id}/download?dl_token={token}",
        "expires_in": _EXPORT_TOKEN_TTL_SECONDS,
    }


@router.get("/image-exports/{export_id}/download", operation_id="download_image_export_with_token")
async def download_image_export_with_token(
    export_id: UUID,
    request: Request,
    dl_token: str = Query(..., min_length=32, max_length=128),
) -> StreamingResponse:
    token_key = f"{_EXPORT_TOKEN_PREFIX}{dl_token}"
    try:
        redis = await get_redis()
        raw_payload = await redis.get(token_key)
        if raw_payload is not None:
            await redis.expire(token_key, _EXPORT_TOKEN_TTL_SECONDS)
    except Exception as exc:
        _logger.warning("이미지 내보내기 다운로드 토큰 확인 실패", exc_info=True)
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
        range_header=request.headers.get("range"),
    )


@router.delete("/image-exports/{export_id}", status_code=204, operation_id="delete_image_export")
async def delete_image_export(
    export_id: UUID,
    token_info: dict = Depends(get_token_info),
) -> None:
    try:
        await soft_delete_project_export(_required_project_id(token_info), str(export_id))
    except ImageExportError as exc:
        _raise_export_http(exc)


@router.get("/layers/{digest}", response_model=HubLayerResponse, operation_id="get_hub_layer")
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


@router.get("/layers/{digest}/ancestors", response_model=list[HubLayerResponse], operation_id="get_hub_layer_ancestors")
async def get_hub_layer_ancestors(digest: str, token_info: dict = Depends(get_token_info)) -> list[dict[str, Any]]:
    """루트 → 자기 자신 순서. 허브에 없는 조상에서 끊기며 `chain_complete` 로 판별한다."""
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=422, detail="digest 는 sha256:<64hex> 형식이어야 합니다")
    factory = _factory_or_503()
    async with factory() as session:
        row = await _load_visible(session, normalized, token_info)
        return [_layer_dict(item) for item in await _ancestor_chain(session, row, token_info)]


@router.get("/layers/{digest}/blob", operation_id="download_hub_blob")
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


@router.post("/uploads", response_model=HubUploadStartResponse, operation_id="start_upload")
async def start_upload(req: HubUploadStartRequest, token_info: dict = Depends(get_token_info)) -> dict[str, Any]:
    factory = _factory_or_503()
    store = _store_or_503()

    if req.digest and store.exists(req.digest):
        async with factory() as session:
            existing = (
                await session.execute(
                    _visible_filter(
                        select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == req.digest),
                        token_info,
                    )
                )
            ).scalar_one_or_none()
        if existing is not None:
            return {
                "session_id": None,
                "completed": True,
                "blob_digest": req.digest,
                "already_present": True,
                "registered": True,
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
    if upload.project_id is not None and upload.project_id != _project_id(token_info):
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없습니다")
    return upload


@router.get("/uploads/{session_id}", response_model=HubUploadStatusResponse, operation_id="get_upload_status")
async def get_upload_status(session_id: str, token_info: dict = Depends(get_token_info)) -> dict[str, Any]:
    factory = _factory_or_503()
    async with factory() as session:
        upload = await _owned_upload(session, session_id, token_info)
        return {
            "session_id": upload.id,
            "declared_digest": upload.declared_digest,
            "received_bytes": upload.received_bytes,
            "project_id": upload.project_id,
            "created_by": upload.created_by,
        }


@router.patch("/uploads/{session_id}", response_model=HubUploadAppendResponse, operation_id="append_upload")
async def append_upload(
    session_id: str, request: Request, response: Response, token_info: dict = Depends(get_token_info)
) -> dict[str, Any]:
    factory = _factory_or_503()
    store = _store_or_503()
    settings = get_settings()

    offset_hdr = request.headers.get("Upload-Offset")
    if offset_hdr is None:
        raise HTTPException(status_code=400, detail="Upload-Offset header is required")
    try:
        expected_offset = int(offset_hdr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Upload-Offset header") from exc

    async with factory() as session:
        upload = await _owned_upload(session, session_id, token_info)
        already = upload.received_bytes

    if expected_offset != already:
        raise HTTPException(
            status_code=409,
            detail="Upload-Offset mismatch",
            headers={"Upload-Offset": str(already)},
        )

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
    response.headers["Upload-Offset"] = str(total)
    return {"session_id": session_id, "received_bytes": total}


@router.put("/uploads/{session_id}", response_model=HubUploadFinalizeResponse, operation_id="finalize_upload")
async def finalize_upload(
    session_id: str, meta: HubLayerMeta, request: Request, token_info: dict = Depends(get_token_info)
) -> dict[str, Any]:
    """수신 바이트의 digest 를 재계산해 검증하고 레이어로 등록한다."""
    if meta.is_published and not token_info.get("is_system_admin"):
        raise HTTPException(status_code=403, detail="공개 레이어 등록은 시스템 관리자만 허용됩니다")
    factory = _factory_or_503()
    store = _store_or_503()

    offset_hdr = request.headers.get("Upload-Offset")
    if offset_hdr is None:
        raise HTTPException(status_code=400, detail="Upload-Offset header is required")
    try:
        expected_offset = int(offset_hdr)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Upload-Offset header") from exc

    async with factory() as session:
        upload = await _owned_upload(session, session_id, token_info)
        declared = upload.declared_digest
        already = upload.received_bytes

    if expected_offset != already:
        raise HTTPException(
            status_code=409,
            detail="Upload-Offset mismatch",
            headers={"Upload-Offset": str(already)},
        )

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
        else:
            await _grant_layer_access(session, finalized.blob_digest, token_info)
            if meta.is_published:
                existing.is_published = True
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


@router.delete("/uploads/{session_id}", status_code=204, operation_id="abort_upload")
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


@router.post("/bundles", operation_id="export_bundle")
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


@router.post("/bundles/import", response_model=HubBundleImportResponse, operation_id="import_bundle")
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
                    await _grant_layer_access(session, declared, token_info)
                    imported.append(declared)
                    continue

                config = dict(entry.get("config") or {})
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


@router.delete(
    "/layers/{digest}", status_code=204, dependencies=[Depends(require_admin)], operation_id="delete_hub_layer"
)
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
