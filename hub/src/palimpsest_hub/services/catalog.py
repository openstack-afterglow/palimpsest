"""SQL-only catalog reads. Visibility and policy are the legacy Hub's, not CAS possession."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, select

from palimpsest_hub.api.hub import _factory_or_503, _project_id, _visible_filter
from palimpsest_hub.models import (
    PalimpsestHubBuild,
    PalimpsestHubLayer,
    PalimpsestHubLayerAccess,
    PalimpsestImageExport,
)
from palimpsest_hub.services.digest import normalize_digest
from palimpsest_hub.services.hub_store import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from palimpsest_hub.services.package_contents import MAX_DEPTH


def search_pattern(q: str | None) -> str | None:
    """Literal substring, including SQL wildcard and escape characters."""
    if q is None or q == "":
        return None
    if not isinstance(q, str) or len(q) > 128:
        raise HTTPException(status_code=422, detail="q must contain at most 128 characters")
    return "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search(stmt, q):
    pattern = search_pattern(q)
    return stmt.where(func.lower(PalimpsestHubLayer.name).like(pattern, escape="\\")) if pattern else stmt


class _Graph:
    def __init__(self, session, token_info=None):
        self.session = session
        self.token_info = token_info
        self.rows = {}

    async def get(self, digest):
        if digest not in self.rows:
            stmt = select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest)
            if self.token_info is not None:
                stmt = _visible_filter(stmt, self.token_info)
            self.rows[digest] = await self.session.scalar(stmt)
        return self.rows[digest]

    async def chain(self, row):
        """Root-to-self, with explicit missing/cycle outcome; never fabricate an edge."""
        chain = [row]
        seen = {row.blob_digest}
        current = row
        while current.parent_digest:
            if current.parent_digest in seen:
                return list(reversed(chain)), False
            if len(chain) > MAX_DEPTH:
                raise HTTPException(status_code=413, detail="catalog ancestry exceeds depth limit")
            parent = await self.get(current.parent_digest)
            if parent is None:
                return list(reversed(chain)), False
            seen.add(parent.blob_digest)
            chain.append(parent)
            current = parent
        return list(reversed(chain)), True

    async def base(self, chain):
        raw = (chain[0].config_json or {}).get("base_image_digest")
        if any((item.config_json or {}).get("base_image_digest") not in (None, raw) for item in chain):
            return None, False
        if raw is None:
            return None, True
        digest = normalize_digest(raw) if isinstance(raw, str) else None
        if digest is None:
            return None, False
        base = await self.get(digest)
        if base is None or base.kind != KIND_CLOUD_IMAGE or base.parent_digest:
            return None, False
        return base, True


def _local_compatibility(chain, base):
    """Metadata-only eligibility; host/runtime prerequisites remain caller-owned."""
    if base is None:
        return False, "base-image-required"
    if base.disk_format not in {"raw", "qcow2"}:
        return False, "base-image-format-unsupported"
    if base.arch not in {"x86_64", "aarch64"} or any(item.arch not in {"x86_64", "aarch64"} for item in chain):
        return False, "architecture-unknown"
    if any(item.arch != base.arch for item in chain):
        return False, "architecture-mismatch"
    return True, None


async def _origins(session, rows):
    digests = {row.blob_digest for row in rows}
    origins = {}
    if not digests:
        return origins
    builds = (
        await session.scalars(
            select(PalimpsestHubBuild)
            .where(
                PalimpsestHubBuild.status == "complete",
                PalimpsestHubBuild.output_digest.in_(digests),
            )
            .order_by(PalimpsestHubBuild.completed_at, PalimpsestHubBuild.id)
        )
    ).all()
    for job in builds:
        origins.setdefault(
            job.output_digest,
            {
                "kind": "build",
                "source": "hub-build",
                "evidence": "recorded",
                "build_id": job.id,
                "job_id": job.id,
                "revision": None,
            },
        )
    exports = (
        await session.scalars(
            select(PalimpsestImageExport)
            .where(
                PalimpsestImageExport.status == "complete",
                PalimpsestImageExport.result_blob_digest.in_(digests),
            )
            .order_by(PalimpsestImageExport.completed_at, PalimpsestImageExport.id)
        )
    ).all()
    for job in exports:
        origins.setdefault(
            job.result_blob_digest,
            {
                "kind": "image-export",
                "source": "glance",
                "evidence": "recorded",
                "build_id": None,
                "job_id": job.id,
                "revision": None,
            },
        )
    # Registration deletes upload sessions and has no durable origin receipt.
    # Neither caller config nor PackageVersion.provenance is server attestation.
    return origins


def _visibility(row, token_info, shared=False):
    if row.is_published:
        return "public"
    if row.project_id is None:
        return "site"
    if token_info is not None and row.project_id == _project_id(token_info):
        return "project"
    return "shared" if shared else "private"


async def _layers(session, graph, rows, token_info):
    origins = await _origins(session, rows)
    owners = {row.blob_digest: row.project_id for row in rows}
    grants = (
        (
            await session.execute(
                select(
                    PalimpsestHubLayerAccess.blob_digest,
                    PalimpsestHubLayerAccess.project_id,
                ).where(PalimpsestHubLayerAccess.blob_digest.in_(owners))
            )
        ).all()
        if owners
        else []
    )
    shared = {digest for digest, project_id in grants if project_id != owners[digest]}
    items = []
    for row in rows:
        chain, complete = await graph.chain(row)
        _, base_complete = await graph.base(chain)
        items.append(
            {
                "id": row.blob_digest,
                "name": row.name,
                "blob_digest": row.blob_digest,
                "parent_id": row.parent_digest,
                "parent_digest": row.parent_digest,
                "kind": row.kind,
                "arch": row.arch,
                "ubuntu_base": row.ubuntu_base,
                "size_bytes": row.size_bytes,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "project_id": row.project_id,
                "visibility": _visibility(row, None, row.blob_digest in shared),
                "chain_complete": complete and base_complete,
                "origin": origins.get(
                    row.blob_digest,
                    {
                        "kind": "unknown",
                        "source": "unknown",
                        "evidence": "unrecorded",
                        "build_id": None,
                        "job_id": None,
                        "revision": None,
                    },
                ),
            }
        )
    return items


def _page(items, offset, limit, **extra):
    has_more = len(items) > limit
    return {
        **extra,
        "items": items[:limit],
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


async def layers(token_info, q, offset, limit):
    async with _factory_or_503()() as session:
        stmt = _search(select(PalimpsestHubLayer), q).order_by(PalimpsestHubLayer.name, PalimpsestHubLayer.blob_digest)
        rows = (await session.scalars(stmt.offset(offset).limit(limit + 1))).all()
        graph = _Graph(session)
        graph.rows.update((row.blob_digest, row) for row in rows)
        return _page(await _layers(session, graph, rows, token_info), offset, limit, source="hub")


async def detail(token_info, digest):
    normalized = normalize_digest(digest)
    if normalized is None:
        raise HTTPException(status_code=422, detail="digest must be sha256:<64hex>")
    async with _factory_or_503()() as session:
        graph = _Graph(session)
        row = await graph.get(normalized)
        if row is None:
            raise HTTPException(status_code=404, detail="layer not found")
        chain, _ = await graph.chain(row)
        children = (
            await session.scalars(
                select(PalimpsestHubLayer)
                .where(
                    PalimpsestHubLayer.parent_digest == normalized,
                )
                .order_by(PalimpsestHubLayer.name, PalimpsestHubLayer.blob_digest)
                .limit(51)
            )
        ).all()
        projected = await _layers(session, graph, [row, *chain[:-1], *children[:50]], token_info)
        return {
            **projected[0],
            "ancestors": projected[1 : len(chain)],
            "children": projected[len(chain) :],
            "children_has_more": len(children) > 50,
            "bundles": [],
        }


async def assets(token_info, q, offset, limit):
    async with _factory_or_503()() as session:
        # A private child must not suppress a visible parent's leaf status. Search
        # is applied after this relation, so q never turns a matching ancestor into a leaf.
        parents = _visible_filter(select(PalimpsestHubLayer.parent_digest), token_info).where(
            PalimpsestHubLayer.parent_digest.is_not(None),
        )
        stmt = (
            _search(_visible_filter(select(PalimpsestHubLayer), token_info), q)
            .where(
                PalimpsestHubLayer.media_type == MEDIA_TYPE_LAYER_SQUASHFS,
                PalimpsestHubLayer.kind != KIND_CLOUD_IMAGE,
                PalimpsestHubLayer.name != "",
                PalimpsestHubLayer.blob_digest.not_in(parents),
            )
            .order_by(PalimpsestHubLayer.name, PalimpsestHubLayer.blob_digest)
        )
        # Paginate SQL candidate leaves, not the filtered logical result set.
        # Each request examines at most limit candidates and one lookahead;
        # incomplete/private chains may leave an empty but resumable page.
        rows = (await session.scalars(stmt.offset(offset).limit(limit + 1))).all()
        candidates = rows[:limit]
        graph = _Graph(session, token_info)
        graph.rows.update((row.blob_digest, row) for row in candidates)
        items = []
        for row in candidates:
            chain, complete = await graph.chain(row)
            if not complete or any(item.media_type != MEDIA_TYPE_LAYER_SQUASHFS for item in chain):
                continue
            base, base_complete = await graph.base(chain)
            if not base_complete:
                continue
            local_supported, local_reason = _local_compatibility(chain, base)
            items.append(
                {
                    "id": row.blob_digest,
                    "name": row.name,
                    "kind": "layer-bundle",
                    "blob_digest": row.blob_digest,
                    "chain_id": row.chain_id,
                    "arch": row.arch,
                    "ubuntu_base": row.ubuntu_base,
                    "size_bytes": sum(item.size_bytes for item in chain) + (base.size_bytes if base else 0),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "visibility": _visibility(row, token_info, True),
                    "layer_count": len(chain),
                    "chain_complete": True,
                    "download_allowed": "packages:read" in token_info.get("package_actions", ()),
                    "base_image_digest": base.blob_digest if base else None,
                    "local_supported": local_supported,
                    "local_reason": local_reason,
                }
            )
        has_more = len(rows) > limit
        return {
            "items": items,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + len(candidates) if has_more else None,
        }
