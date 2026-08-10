"""Palimpsest 콘텐츠 주소화 — digest 계산·파싱 유틸.

레이어의 정체성은 `.sqsh` blob 바이트의 sha256(`sha256:<64hex>`)이다. 설계 근거와 용어는
`docs/palimpsest.md` §3 참조. 여기 담긴 것은 순수 함수뿐이며 DB/네트워크에 접근하지 않는다.

- `blob_digest`  — `.sqsh` 파일 바이트의 sha256. 레이어 정체성
- `blob_md5`     — 외부 도구 호환용 **보조 검색 키**. 무결성 권위 아님. 보안 용도 금지
- `config_digest`— 레이어 메타 JSON 을 정규화(sort_keys)한 sha256
- `chain_id`     — 스택 전체의 정체성. OCI chainID 방식

빌드 VM 은 `mksquashfs` 직후 digest 를 콘솔 sentinel 로 방출하고(`recipe_blocks`),
오케스트레이터가 `parse_digest_sentinel` 로 회수한다(`layer_build`). 계산 실패는 빌드 실패로
취급하지 않는다 — digest 는 나중에 백필할 수 있지만 빌드 산출물은 되살릴 수 없다.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 빌드 스크립트가 방출하는 sentinel 접두사. `recipe_blocks.digest_sentinel_block()` 과 짝을 이룬다.
DIGEST_SENTINEL = "::AFTERGLOW::DIGEST::"

# digest_state 값 — layer_artifacts.digest_state
DIGEST_PENDING = "pending"
DIGEST_READY = "ready"
DIGEST_FAILED = "failed"

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
# 한 번의 빌드가 여러 레이어를 만들 수 있으므로(Dockerfile import) 레이어 이름을 함께 싣는다.
# 위치 기반 매핑은 콘솔이 잘리면 조용히 어긋나 잘못된 digest 가 붙는다.
_SENTINEL_LINE_RE = re.compile(
    re.escape(DIGEST_SENTINEL) + r"layer=([A-Za-z0-9][A-Za-z0-9.+_-]*) "
    r"sha256=([0-9a-fA-F]*) md5=([0-9a-fA-F]*) size=([0-9]*)"
)


class DigestError(ValueError):
    """digest 형식이 유효하지 않다."""


@dataclass(frozen=True)
class DigestReport:
    """빌드 VM 이 방출한 blob digest 한 건."""

    layer_name: str
    blob_digest: str  # "sha256:<64hex>"
    blob_md5: str | None  # 32 hex 또는 None
    size_bytes: int | None


def normalize_digest(value: str | None) -> str | None:
    """`sha256:<64hex>` 로 정규화한다. 접두사 없는 64hex 도 받는다. 무효하면 None."""
    if not value:
        return None
    candidate = value.strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[len("sha256:") :]
    if not _SHA256_HEX_RE.match(candidate):
        return None
    return f"sha256:{candidate}"


def require_digest(value: str | None, *, field: str = "digest") -> str:
    """`normalize_digest` 하되 무효하면 예외."""
    normalized = normalize_digest(value)
    if normalized is None:
        raise DigestError(f"{field}는 sha256:<64hex> 형식이어야 합니다")
    return normalized


def normalize_md5(value: str | None) -> str | None:
    """32 hex 소문자로 정규화한다. 무효하면 None."""
    if not value:
        return None
    candidate = value.strip().lower()
    if not _MD5_HEX_RE.match(candidate):
        return None
    return candidate


def is_digest_prefix(value: str) -> bool:
    """digest prefix 검색어로 쓸 수 있는 형태인지(hex, 4자 이상)."""
    candidate = value.strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[len("sha256:") :]
    return 4 <= len(candidate) <= 64 and all(c in "0123456789abcdef" for c in candidate)


def compute_chain_id(parent_chain_id: str | None, blob_digest: str) -> str:
    """스택 전체의 정체성. OCI chainID 방식.

    루트: `chain_id = blob_digest`
    이후: `chain_id(n) = sha256(chain_id(n-1) + " " + blob_digest(n))`

    부모 chain_id 가 아직 없으면(백필 대기) 호출자가 계산을 미뤄야 한다 — 여기서
    임의로 루트 취급하면 서로 다른 스택이 같은 chain_id 를 갖게 된다.
    """
    digest = require_digest(blob_digest, field="blob_digest")
    if parent_chain_id is None:
        return digest
    parent = require_digest(parent_chain_id, field="parent_chain_id")
    return "sha256:" + hashlib.sha256(f"{parent} {digest}".encode()).hexdigest()


def compute_config_digest(config: Mapping[str, Any]) -> str:
    """레이어 메타를 정규화(sort_keys, 공백 없음)해 sha256."""
    payload = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_layer_config(
    *,
    name: str,
    kind: str,
    ubuntu_base: str | None,
    python_version: str | None,
    pip_packages: list[str] | None,
    apt_packages: list[str] | None,
    parent_digest: str | None,
) -> dict[str, Any]:
    """`config_digest` 계산에 들어가는 정규 메타.

    빌드 산출물의 정체성이 아니라 **빌드 의도**를 표현한다. 필드를 추가할 때는 기존 레이어의
    config_digest 가 바뀐다는 점에 유의(재계산 필요).
    """
    return {
        "name": name,
        "kind": kind,
        "ubuntu_base": ubuntu_base,
        "python_version": python_version,
        "pip_packages": sorted(pip_packages) if pip_packages else [],
        "apt_packages": sorted(apt_packages) if apt_packages else [],
        "parent_digest": normalize_digest(parent_digest),
    }


def parse_digest_sentinels(console_text: str | None) -> dict[str, DigestReport]:
    """콘솔 출력의 digest sentinel 전부를 `{layer_name: DigestReport}` 로 회수한다.

    같은 레이어 이름이 여러 번 나오면 **마지막** 유효 항목을 채택한다(재시도 대비).
    sha256 이 없거나 형식이 어긋난 항목은 건너뛴다 — 호출자는 해당 레이어를 pending 으로
    두고 백필에 맡긴다(빌드를 실패시키지 않는다).
    """
    if not console_text:
        return {}

    reports: dict[str, DigestReport] = {}
    for match in _SENTINEL_LINE_RE.finditer(console_text):
        digest = normalize_digest(match.group(2))
        if digest is None:
            continue
        layer_name = match.group(1)
        size_raw = match.group(4)
        reports[layer_name] = DigestReport(
            layer_name=layer_name,
            blob_digest=digest,
            blob_md5=normalize_md5(match.group(3)),
            size_bytes=int(size_raw) if size_raw else None,
        )
    return reports


def parse_digest_sentinel(console_text: str | None, layer_name: str | None = None) -> DigestReport | None:
    """단일 레이어 빌드용. `layer_name` 을 주면 그 레이어 것만, 없으면 마지막 항목."""
    reports = parse_digest_sentinels(console_text)
    if not reports:
        return None
    if layer_name is not None:
        return reports.get(layer_name)
    return list(reports.values())[-1]
