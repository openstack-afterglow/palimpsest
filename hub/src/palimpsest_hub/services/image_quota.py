"""Published-image quota policy, enforcement and storage accounting.

Policy rows in SQL are authoritative. Code only seeds rows that are missing.
Counts use one unique name per scope; chain bytes are the stored compressed sizes
of the DISTINCT payload digests in a manifest's ``layers``; source bytes are the
DISTINCT OCI source-closure digests. An ``application`` manifest's original cloud
image (``base_image_digest``) is outside both and therefore outside the caps.
Owner decision (receipt 537): in ``oci-root`` mode every manifest layer,
including original OCI base layers, counts toward the chain cap.

Usage and reports aggregate in SQL from memberships, physical layer sizes and
source-closure rows; they never load stored manifests.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import LargeBinary, case, cast, distinct, func, select, union
from sqlalchemy.exc import IntegrityError

from palimpsest_hub.image_models import (
    PalimpsestHubImageMembership,
    PalimpsestHubImageQuotaPolicy,
    PalimpsestHubImageQuotaTier,
    PalimpsestHubImageSourceBlob,
    PalimpsestHubPublishedImage,
)
from palimpsest_hub.models import PalimpsestHubLayer, exact_identity
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.worker_leases import run_blocking

GIB = 2**30
MAX_BIGINT = 2**63 - 1
TIERS = ("free", "paid")
VISIBILITIES = ("private", "internal", "public")
SUBJECT_KINDS = ("user", "project")
DEFAULT_TIER = "free"
LIMIT_FIELDS = ("max_images", "max_chain_bytes", "max_source_bytes")
# Definite publication refusals; image-job completion settles these as terminal job errors.
QUOTA_ERROR_CODES = frozenset({"image_count_limit", "image_chain_size_limit", "image_source_size_limit"})
# Initial rows only (paid values were kept as proposed by owner decision 537); administrators own them afterwards.
# (max_images, max_chain_bytes, max_source_bytes); source seed equals the chain cap.
SEED_POLICIES = {
    ("free", "private"): (2, 1 * GIB, 1 * GIB),
    ("free", "internal"): (5, 5 * GIB, 5 * GIB),
    ("free", "public"): (None, 10 * GIB, 10 * GIB),
    ("paid", "private"): (20, 50 * GIB, 50 * GIB),
    ("paid", "internal"): (50, 50 * GIB, 50 * GIB),
    ("paid", "public"): (None, 100 * GIB, 100 * GIB),
}

_IMAGE = PalimpsestHubPublishedImage
_MEMBER = PalimpsestHubImageMembership
_SOURCE = PalimpsestHubImageSourceBlob
_LAYER = PalimpsestHubLayer


def scope_of(visibility):
    """private/public are charged to the publishing user, internal to the project."""
    return "project" if visibility == "internal" else "user"


def chain_bytes(manifest):
    """Each distinct payload digest counts once, however often the chain repeats it."""
    return sum({item["digest"]: item["size_bytes"] for item in manifest["layers"]}.values())


def _now():
    return datetime.now(UTC)


def _int(value):
    return int(value) if value is not None else 0


def _policy_value(row, key, *, stored):
    limits = (row.max_images, row.max_chain_bytes, row.max_source_bytes) if stored else SEED_POLICIES[key]
    return {
        "tier": key[0],
        "visibility": key[1],
        "scope": scope_of(key[1]),
        **dict(zip(LIMIT_FIELDS, limits, strict=True)),
        # "seed" = not yet persisted; the identical row is inserted on first enforcement or update.
        "source": "stored" if stored else "seed",
        "updated_by": row.updated_by if stored else None,
        "updated_at": row.updated_at.isoformat() if stored and row.updated_at else None,
    }


async def ensure_seeded(session):
    """Insert only missing seed rows. Caller holds the global image mutex."""
    present = set(
        (
            await session.execute(select(PalimpsestHubImageQuotaPolicy.tier, PalimpsestHubImageQuotaPolicy.visibility))
        ).all()
    )
    for (tier, visibility), limits in SEED_POLICIES.items():
        if (tier, visibility) not in present:
            session.add(
                PalimpsestHubImageQuotaPolicy(
                    tier=tier, visibility=visibility, **dict(zip(LIMIT_FIELDS, limits, strict=True)), updated_by=None
                )
            )
    await session.flush()


async def load_policies(session):
    """Every tier/visibility policy: the stored row, else the seed it would receive."""
    rows = {
        (row.tier, row.visibility): row
        for row in (await session.scalars(select(PalimpsestHubImageQuotaPolicy))).all()
        if row.tier in TIERS and row.visibility in VISIBILITIES
    }
    return {
        key: _policy_value(rows.get(key), key, stored=key in rows)
        for key in ((tier, visibility) for tier in TIERS for visibility in VISIBILITIES)
    }


async def update_policy(session, tier, visibility, limits, admin_user):
    """Caller holds the global image mutex and commits. Lowering never deletes images."""
    await ensure_seeded(session)
    row = await session.scalar(
        select(PalimpsestHubImageQuotaPolicy)
        .where(PalimpsestHubImageQuotaPolicy.tier == tier, PalimpsestHubImageQuotaPolicy.visibility == visibility)
        .with_for_update()
    )
    for field in LIMIT_FIELDS:
        setattr(row, field, limits[field])
    row.updated_by, row.updated_at = admin_user, _now()
    await session.flush()
    return _policy_value(row, (tier, visibility), stored=True)


async def _tier_row(session, subject_kind, subject_id, *, lock=False):
    query = select(PalimpsestHubImageQuotaTier).where(
        PalimpsestHubImageQuotaTier.subject_kind == subject_kind,
        exact_identity(PalimpsestHubImageQuotaTier.subject_id, subject_id),
    )
    return await session.scalar(query.with_for_update() if lock else query)


def _tier_value(row, subject_kind, subject_id):
    if row is not None and row.tier not in TIERS:
        raise HTTPException(status_code=409, detail="stored image quota tier is invalid")
    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "tier": row.tier if row is not None else DEFAULT_TIER,
        "assigned": row is not None,
        "updated_by": row.updated_by if row is not None else None,
        "updated_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
    }


async def subject_tier(session, subject_kind, subject_id):
    return _tier_value(await _tier_row(session, subject_kind, subject_id), subject_kind, subject_id)


async def assign_tier(session, subject_kind, subject_id, tier, admin_user):
    """Caller holds the global image mutex and commits.

    Lookups are byte-exact. A case-insensitive SQL collation may still refuse a second
    key that differs only in letter case; that is a definite 409, never a 500.
    """
    row = await _tier_row(session, subject_kind, subject_id, lock=True)
    if row is None:
        row = PalimpsestHubImageQuotaTier(
            subject_kind=subject_kind, subject_id=subject_id, tier=tier, updated_by=admin_user
        )
        session.add(row)
    else:
        row.tier, row.updated_by = tier, admin_user
    row.updated_at = _now()
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="a tier assignment exists for an identifier differing only in letter case"
        ) from None
    return _tier_value(row, subject_kind, subject_id)


def _exact(column, value):
    return (column == value) & (cast(column, LargeBinary) == value.encode())


def _owner_column(visibility):
    return _IMAGE.project_id if scope_of(visibility) == "project" else _IMAGE.created_by


def _scope_filter(visibility, subject_id):
    return (_IMAGE.visibility == visibility) & exact_identity(_owner_column(visibility), subject_id)


def _subject(actor, visibility):
    return actor["project_id"] if scope_of(visibility) == "project" else actor["user_id"]


def _chains():
    """Per image: distinct payload bytes, distinct digest count, and digests with a physical row."""
    pairs = select(_MEMBER.image_id, _MEMBER.blob_digest).distinct().subquery()
    return (
        select(
            pairs.c.image_id,
            func.coalesce(func.sum(_LAYER.size_bytes), 0).label("bytes"),
            func.count().label("digests"),
            func.count(_LAYER.id).label("known"),
        )
        .select_from(pairs.outerjoin(_LAYER, _LAYER.blob_digest == pairs.c.blob_digest))
        .group_by(pairs.c.image_id)
        .subquery()
    )


def _sources():
    pairs = select(_SOURCE.image_id, _SOURCE.blob_digest, _SOURCE.size_bytes).distinct().subquery()
    return select(pairs.c.image_id, func.sum(pairs.c.size_bytes).label("bytes")).group_by(pairs.c.image_id).subquery()


def _require_consistent(missing, partial):
    # Same wording as image_value: a corrupt row is a definite 409, not a 500.
    if _int(missing):
        raise HTTPException(status_code=409, detail="published image membership is incomplete")
    if _int(partial):
        raise HTTPException(status_code=409, detail="published image graph is incomplete")


async def enforce(session, actor, name, manifest, visibility, *, source=0):
    """Run inside ``register_image`` after request_id replay, under the global image mutex.

    The mutex serializes every publisher, so this check and the following insert
    cannot interleave with another publication exceeding the same limit.
    """
    await ensure_seeded(session)
    scope, subject = scope_of(visibility), _subject(actor, visibility)
    tier = (await subject_tier(session, scope, subject))["tier"]
    policy = (await load_policies(session))[(tier, visibility)]
    limit = policy["max_images"]
    if limit is not None:
        scoped = _scope_filter(visibility, subject)
        existing = await session.scalar(select(_IMAGE.id).where(scoped, _exact(_IMAGE.name, name)).limit(1))
        if existing is None:
            used = _int(
                await session.scalar(select(func.count(distinct(cast(_IMAGE.name, LargeBinary)))).where(scoped))
            )
            if used >= limit:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "image_count_limit",
                        "visibility": visibility,
                        "scope": scope,
                        "limit": limit,
                        "used": used,
                    },
                )
    limit_bytes, total = policy["max_chain_bytes"], chain_bytes(manifest)
    if limit_bytes is not None and total > limit_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "image_chain_size_limit",
                "visibility": visibility,
                "limit_bytes": limit_bytes,
                "chain_bytes": total,
            },
        )
    limit_source = policy["max_source_bytes"]
    if limit_source is not None and source > limit_source:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "image_source_size_limit",
                "visibility": visibility,
                "limit_bytes": limit_source,
                "source_bytes": source,
            },
        )


def _over(column, limit):
    return func.sum(case((column > limit, 1), else_=0)) if limit is not None else func.sum(0)


async def scope_usage(session, visibility, subject_id, tier, policies):
    policy = policies[(tier, visibility)]
    max_images, max_bytes, max_source = (policy[field] for field in LIMIT_FIELDS)
    chains, sources = _chains(), _sources()
    chain = func.coalesce(chains.c.bytes, 0)
    source = func.coalesce(sources.c.bytes, 0)
    row = (
        await session.execute(
            select(
                func.count(_IMAGE.id),
                func.count(distinct(cast(_IMAGE.name, LargeBinary))),
                func.max(chain),
                _over(chain, max_bytes),
                func.max(source),
                _over(source, max_source),
                func.sum(case((chains.c.image_id.is_(None), 1), else_=0)),
                func.sum(case((chains.c.digests != chains.c.known, 1), else_=0)),
            )
            .select_from(
                _IMAGE.__table__.outerjoin(chains, chains.c.image_id == _IMAGE.id).outerjoin(
                    sources, sources.c.image_id == _IMAGE.id
                )
            )
            .where(_scope_filter(visibility, subject_id))
        )
    ).one()
    images, names, largest, over_chain, largest_source, over_source = (_int(value) for value in row[:6])
    _require_consistent(row[6], row[7])
    over_images = max_images is not None and names > max_images
    over_bytes, over_sources = over_chain > 0, over_source > 0
    return {
        "visibility": visibility,
        "scope": scope_of(visibility),
        "subject_id": subject_id,
        "tier": tier,
        "max_images": max_images,
        "max_chain_bytes": max_bytes,
        "max_source_bytes": max_source,
        "unique_names": names,
        "images": images,
        "largest_chain_bytes": largest,
        "images_over_chain_limit": over_chain,
        "largest_source_bytes": largest_source,
        "images_over_source_limit": over_source,
        "can_publish_new_name": max_images is None or names < max_images,
        "over_limit_images": over_images,
        "over_limit_chain_bytes": over_bytes,
        "over_limit_source_bytes": over_sources,
        "over_limit": over_images or over_bytes or over_sources,
    }


async def subject_usage(session, subject_kind, subject_id):
    policies = await load_policies(session)
    tier = await subject_tier(session, subject_kind, subject_id)
    visibilities = ("internal",) if subject_kind == "project" else ("private", "public")
    return {
        **tier,
        "scopes": [await scope_usage(session, item, subject_id, tier["tier"], policies) for item in visibilities],
    }


async def caller_usage(session, actor):
    """Effective tiers, limits and usage for every scope the caller publishes into."""
    user = await subject_usage(session, "user", actor["user_id"])
    project = await subject_usage(session, "project", actor["project_id"])
    return {
        "user_id": actor["user_id"],
        "project_id": actor["project_id"],
        "user_tier": user["tier"],
        "project_tier": project["tier"],
        "scopes": [*user["scopes"], *project["scopes"]],
    }


def _capacity(store):
    try:
        usage = shutil.disk_usage(store.root)
    except (OSError, TypeError, ValueError, AttributeError):
        return None
    return usage.free, usage.total


def _namespace_columns():
    binding = _IMAGE.storage_binding_json
    return (
        binding["kind"].as_string().label("kind"),
        binding["share_id"].as_string().label("share_id"),
        binding["scope_key"].as_string().label("scope_key"),
    )


def _payload_digests():
    return (
        select(*_namespace_columns(), _MEMBER.blob_digest.label("digest"), _LAYER.size_bytes.label("size"))
        .select_from(_IMAGE)
        .join(_MEMBER, _MEMBER.image_id == _IMAGE.id)
        .join(_LAYER, _LAYER.blob_digest == _MEMBER.blob_digest)
    )


def _source_digests():
    return (
        select(*_namespace_columns(), _SOURCE.blob_digest.label("digest"), _SOURCE.size_bytes.label("size"))
        .select_from(_IMAGE)
        .join(_SOURCE, _SOURCE.image_id == _IMAGE.id)
    )


async def _namespace_totals(session, digests):
    """Distinct (digest, size) per storage namespace, aggregated in SQL."""
    rows = digests.subquery()
    unique = select(rows.c.kind, rows.c.share_id, rows.c.scope_key, rows.c.digest, rows.c.size).distinct().subquery()
    result = await session.execute(
        select(unique.c.kind, unique.c.share_id, unique.c.scope_key, func.count(), func.sum(unique.c.size)).group_by(
            unique.c.kind, unique.c.share_id, unique.c.scope_key
        )
    )
    return {(kind, share, scope): (_int(count), _int(size)) for kind, share, scope, count, size in result.all()}


async def _hub_totals(session, digests):
    rows = digests.subquery()
    unique = select(rows.c.digest, rows.c.size).distinct().subquery()
    count, size = (await session.execute(select(func.count(), func.sum(unique.c.size)))).one()
    return _int(count), _int(size)


def _namespace_label(kind, share, scope):
    if kind == "local":
        return {"kind": "local", "share_id": None, "scope_key": None}
    if kind == "manila-namespace-v1":
        return {"kind": kind, "share_id": share, "scope_key": scope}
    return {"kind": kind, "share_id": share, "scope_key": None}


async def physical_report(session, store, *, manila):
    """Exact SQL-derived accounting; anything the Hub does not know is null, never estimated.

    ``store`` is the Hub CAS; ``manila`` says publications are copied into Manila namespaces.
    """
    chains, sources = _chains(), _sources()
    owner = case((_IMAGE.visibility == "internal", _IMAGE.project_id), else_=_IMAGE.created_by)
    totals = (
        await session.execute(
            select(
                _IMAGE.visibility,
                func.count(_IMAGE.id),
                func.sum(func.coalesce(chains.c.bytes, 0)),
                func.sum(func.coalesce(sources.c.bytes, 0)),
                func.sum(case((chains.c.image_id.is_(None), 1), else_=0)),
                func.sum(case((chains.c.digests != chains.c.known, 1), else_=0)),
            )
            .select_from(
                _IMAGE.__table__.outerjoin(chains, chains.c.image_id == _IMAGE.id).outerjoin(
                    sources, sources.c.image_id == _IMAGE.id
                )
            )
            .group_by(_IMAGE.visibility)
        )
    ).all()
    named = select(_IMAGE.visibility, cast(owner, LargeBinary), cast(_IMAGE.name, LargeBinary)).distinct().subquery()
    names = dict((await session.execute(select(named.c.visibility, func.count()).group_by(named.c.visibility))).all())
    scopes = {
        visibility: {"images": 0, "unique_names": 0, "logical_bytes": 0, "logical_source_bytes": 0}
        for visibility in VISIBILITIES
    }
    for visibility, count, logical, logical_source, missing, partial in totals:
        if visibility not in scopes:
            raise HTTPException(status_code=409, detail="published image visibility is invalid")
        _require_consistent(missing, partial)
        scopes[visibility].update(
            images=_int(count),
            unique_names=_int(names.get(visibility)),
            logical_bytes=_int(logical),
            logical_source_bytes=_int(logical_source),
        )
    payload = await _namespace_totals(session, _payload_digests())
    source = await _namespace_totals(session, _source_digests())
    combined = await _namespace_totals(session, union(_payload_digests(), _source_digests()))
    namespaces = [
        {
            **_namespace_label(*key),
            "distinct_blobs": combined[key][0],
            "physical_bytes": combined[key][1],
            "payload_bytes": payload.get(key, (0, 0))[1],
            "source_bytes": source.get(key, (0, 0))[1],
        }
        for key in sorted(combined, key=lambda key: tuple(part or "" for part in key))
    ]
    hub_blobs, hub_bytes = await _hub_totals(session, union(_payload_digests(), _source_digests()))
    base_digest = _IMAGE.manifest_json["base_image_digest"].as_string()
    referenced = (
        await session.execute(
            select(base_digest, _IMAGE.visibility, func.count())
            .where(_IMAGE.manifest_json["mode"].as_string() == "application", base_digest.is_not(None))
            .group_by(base_digest, _IMAGE.visibility)
        )
    ).all()
    bases = {}
    for digest, visibility, count in referenced:
        entry = bases.setdefault(digest, {"referencing_images": 0, "by_visibility": dict.fromkeys(VISIBILITIES, 0)})
        entry["referencing_images"] += _int(count)
        if visibility in entry["by_visibility"]:
            entry["by_visibility"][visibility] += _int(count)
    known = {}
    if bases:
        known = dict(
            (
                await session.execute(
                    select(_LAYER.blob_digest, _LAYER.size_bytes).where(_LAYER.blob_digest.in_(sorted(bases)))
                )
            ).all()
        )
    if manila:
        # Publications live in Manila namespaces; the Hub does not measure share capacity.
        store_value = {"kind": "manila", "free_bytes": None, "total_bytes": None}
    else:
        capacity = await run_blocking(_capacity, store) if isinstance(store, LocalPathBlobStore) else None
        store_value = {
            "kind": "local_path" if capacity is not None else None,
            "free_bytes": capacity[0] if capacity is not None else None,
            "total_bytes": capacity[1] if capacity is not None else None,
        }
    return {
        "unit": "bytes",
        "byte_source": "stored compressed sizes of payload rows and source-closure rows",
        "scopes": scopes,
        "namespaces": namespaces,
        "hub_cas": {"distinct_blobs": hub_blobs, "physical_bytes": hub_bytes},
        "total": {
            "images": sum(value["images"] for value in scopes.values()),
            "logical_bytes": sum(value["logical_bytes"] for value in scopes.values()),
            "logical_source_bytes": sum(value["logical_source_bytes"] for value in scopes.values()),
        },
        "base_images": [
            {
                "digest": digest,
                **bases[digest],
                # Only a Hub-registered blob has a known size; otherwise null, not an estimate.
                "size_bytes": known.get(digest),
                "size_known": digest in known,
            }
            for digest in sorted(bases)
        ],
        "store": store_value,
        "notes": {
            "namespaces": "each digest counted once per storage namespace: once Hub-wide for local storage, "
            "once per (share_id, scope_key) for Manila namespaces",
            "hub_cas": "distinct published digests retained in the Hub CAS (also the local publication store); "
            "Manila namespace copies are in addition to it",
            "logical_bytes": "sum of each image's charged chain bytes (distinct payload digests per image)",
            "logical_source_bytes": "sum of each image's charged source bytes (distinct closure digests per image)",
            "base_images": "application-mode original cloud images; excluded from chain and source caps",
            "oci_root": "every oci-root manifest layer, including original OCI base layers, is charged (owner decision)",
            "store": "free/total only for a readable local publication store; Manila capacity is not measured",
        },
    }
