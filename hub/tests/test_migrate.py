from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from palimpsest_hub.image_models import PalimpsestHubBuildContext, PalimpsestHubImageJob, PalimpsestHubImageQuotaTier
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.migrate import MigrationError, migrate
from palimpsest_hub.models import (
    Base,
    PalimpsestHubBuild,
    PalimpsestImageExport,
    PalimpsestImageExportDelegation,
    PalimpsestWorkerClaim,
    PalimpsestWorkerLease,
    RuntimeBlobReference,
)


async def create_database(url: str, *, with_rows: bool) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if with_rows:
                await connection.execute(
                    Base.metadata.tables["palimpsest_hub_layers"]
                    .insert()
                    .values(
                        id=1,
                        blob_digest="sha256:" + "a" * 64,
                        size_bytes=4096,
                        media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
                        config_digest="sha256:" + "b" * 64,
                        name="legacy-layer",
                        kind="squashfs",
                        config_json={"instance_mode": "oci-layer"},
                        project_id="c" * 32,
                        is_published=False,
                    )
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_preserves_legacy_layer_without_fabricating_a_published_image(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'old.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'new.sqlite'}"
    engine = create_async_engine(source_url)
    try:
        async with engine.begin() as connection:
            legacy = [
                Base.metadata.tables[name]
                for name in ("palimpsest_hub_layers", "palimpsest_hub_uploads", "palimpsest_image_exports")
            ]
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=legacy))
            await connection.execute(
                legacy[0]
                .insert()
                .values(
                    id=1,
                    blob_digest="sha256:" + "a" * 64,
                    size_bytes=4096,
                    media_type="application/vnd.afterglow.palimpsest.layer.squashfs.v1",
                    config_digest="sha256:" + "b" * 64,
                    name="legacy-layer",
                    kind="squashfs",
                    config_json={"instance_mode": "oci-layer"},
                    project_id="c" * 32,
                    is_published=False,
                )
            )
    finally:
        await engine.dispose()
    await create_database(destination_url, with_rows=False)
    copied = await migrate(source_url, destination_url)
    assert copied["palimpsest_hub_layers"] == 1
    destination = create_async_engine(destination_url)
    try:
        async with destination.connect() as connection:
            layer = (await connection.execute(text("SELECT blob_digest, config_json FROM palimpsest_hub_layers"))).one()
            assert layer.blob_digest == "sha256:" + "a" * 64
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_published_images")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layer_nodes")) == 0
    finally:
        await destination.dispose()


@pytest.mark.asyncio
async def test_migrate_rejects_same_database():
    with pytest.raises(MigrationError):
        await migrate("sqlite+aiosqlite:///same.sqlite", "sqlite+aiosqlite:///same.sqlite")


@pytest.mark.asyncio
async def test_migrate_rejects_partial_native_state_before_copying_any_legacy_row(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'partial.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'empty.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    engine = create_async_engine(source_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE palimpsest_package_tags"))
        with pytest.raises(MigrationError):
            await migrate(source_url, destination_url)
        destination = create_async_engine(destination_url)
        try:
            async with destination.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layers")) == 0
        finally:
            await destination.dispose()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "unknown-tier",
        "negative-limit",
        "negative-source",
        "subject-kind",
        "subject-id",
        "blank-updater",
        "partial-schema",
    ],
)
async def test_migrate_copies_image_quota_policy_and_tiers_or_rejects_corrupt_rows(tmp_path: Path, damage):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-destination.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    policies = Base.metadata.tables["palimpsest_hub_image_quota_policies"]
    tiers = Base.metadata.tables["palimpsest_hub_image_quota_tiers"]
    policy_rows = [
        {
            "tier": "free",
            "visibility": "private",
            "max_images": 3,
            "max_chain_bytes": 4096,
            "max_source_bytes": 8192,
            "updated_by": "f" * 32,
        },
        {
            "tier": "paid",
            "visibility": "public",
            "max_images": None,
            "max_chain_bytes": 2**63 - 1,
            "max_source_bytes": None,
            "updated_by": None,
        },
    ]
    tier_rows = [{"subject_kind": "user", "subject_id": "d" * 32, "tier": "paid", "updated_by": "f" * 32}]
    if damage == "unknown-tier":
        policy_rows[0]["tier"] = "gold"
    elif damage == "negative-limit":
        policy_rows[0]["max_images"] = -1
    elif damage == "subject-kind":
        tier_rows[0]["subject_kind"] = "group"
    elif damage == "subject-id":
        tier_rows[0]["subject_id"] = "bad id"
    elif damage == "blank-updater":
        tier_rows[0]["updated_by"] = "  "
    elif damage == "negative-source":
        policy_rows[1]["max_source_bytes"] = -5
    source = create_async_engine(source_url)
    try:
        async with source.begin() as connection:
            await connection.execute(policies.insert(), policy_rows)
            await connection.execute(tiers.insert(), tier_rows)
            if damage == "partial-schema":
                await connection.execute(text("DROP TABLE palimpsest_hub_image_quota_tiers"))
    finally:
        await source.dispose()
    destination = create_async_engine(destination_url)
    try:
        if damage:
            with pytest.raises(MigrationError):
                await migrate(source_url, destination_url)
            async with destination.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layers")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_image_quota_policies")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_image_quota_tiers")) == 0
            return
        copied = await migrate(source_url, destination_url)
        assert copied["palimpsest_hub_image_quota_policies"] == 2
        assert copied["palimpsest_hub_image_quota_tiers"] == 1
        fields = ("tier", "visibility", "max_images", "max_chain_bytes", "max_source_bytes", "updated_by")
        async with destination.connect() as connection:
            stored = (await connection.execute(select(*(policies.c[name] for name in fields)))).all()
            by_tier = {row["tier"]: row for row in (dict(zip(fields, item, strict=True)) for item in stored)}
            assert by_tier == {row["tier"]: row for row in policy_rows}
            assigned = (
                await connection.execute(
                    select(tiers.c.subject_kind, tiers.c.subject_id, tiers.c.tier, tiers.c.updated_by)
                )
            ).all()
            assert [tuple(row) for row in assigned] == [("user", "d" * 32, "paid", "f" * 32)]
    finally:
        await destination.dispose()


@pytest.mark.asyncio
async def test_migrate_accepts_a_source_from_before_image_quota(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'pre-quota.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-ready.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    source = create_async_engine(source_url)
    try:
        async with source.begin() as connection:
            await connection.execute(text("DROP TABLE palimpsest_hub_image_quota_policies"))
            await connection.execute(text("DROP TABLE palimpsest_hub_image_quota_tiers"))
    finally:
        await source.dispose()
    copied = await migrate(source_url, destination_url)
    assert copied["palimpsest_hub_layers"] == 1
    assert copied["palimpsest_hub_image_quota_policies"] == copied["palimpsest_hub_image_quota_tiers"] == 0


@pytest.mark.asyncio
async def test_migrate_fills_a_missing_source_cap_from_the_chain_cap(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'pre-source-cap.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'source-cap.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    source = create_async_engine(source_url)
    try:
        async with source.begin() as connection:
            await connection.execute(
                text("ALTER TABLE palimpsest_hub_image_quota_policies DROP COLUMN max_source_bytes")
            )
            await connection.execute(
                text(
                    "INSERT INTO palimpsest_hub_image_quota_policies "
                    "(tier, visibility, max_images, max_chain_bytes, updated_by, updated_at) "
                    "VALUES ('free', 'private', 2, 4096, NULL, '2026-11-07 00:00:00')"
                )
            )
    finally:
        await source.dispose()
    await migrate(source_url, destination_url)
    destination = create_async_engine(destination_url)
    try:
        async with destination.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT max_chain_bytes, max_source_bytes FROM palimpsest_hub_image_quota_policies")
                )
            ).one()
            assert tuple(row) == (4096, 4096)
    finally:
        await destination.dispose()


def _closure_image(image_id: str, project: str) -> tuple[dict, list[dict]]:
    from palimpsest_local.image_manifest import manifest_from_result

    manifest = manifest_from_result(
        {
            "layers": [
                {
                    "digest": "sha256:" + "a" * 64,
                    "size_bytes": 4096,
                    "source": {
                        "oci_layer": {"ordinal": 0, "compressed": {"digest": "sha256:" + "6" * 64, "size": 600}}
                    },
                }
            ],
            "source": {
                "manifest_digest": "sha256:" + "4" * 64,
                "config_digest": "sha256:" + "5" * 64,
                "root_digest": "sha256:" + "4" * 64,
            },
            "runtime": {"entrypoint": [], "cmd": [], "env": [], "working_dir": "/", "user": "0"},
        },
        os_family="fixture-os",
        os_version="1",
        source_kind="local",
    )
    image = {
        "id": image_id,
        "project_id": project,
        "created_by": "d" * 32,
        "request_id": image_id,
        "name": "closure",
        "visibility": "private",
        "manifest_digest": "sha256:" + "1" * 64,
        "manifest_json": manifest,
        "leaf_id": manifest["layers"][-1]["id"],
        "storage_binding_json": {"kind": "local", "root": "blobs/sha256"},
    }
    blobs = [
        {"role": role, "ordinal": 0, "blob_digest": "sha256:" + char * 64, "size_bytes": size}
        for role, char, size in (("manifest", "4", 400), ("config", "5", 500), ("layer", "6", 600))
    ]
    return image, [{**item, "image_id": image_id, "project_id": project} for item in blobs]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "unknown-role", "orphan", "two-manifests", "project", "uncommitted"])
async def test_migrate_copies_source_closure_rows_or_rejects_incoherent_ones(tmp_path: Path, damage):
    from uuid import uuid4

    source_url = f"sqlite+aiosqlite:///{tmp_path / 'closure-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'closure-destination.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    image_id = str(uuid4())
    image, blobs = _closure_image(image_id, "c" * 32)
    if damage == "unknown-role":
        blobs[0]["role"] = "kernel"
    elif damage == "orphan":
        blobs = [{**item, "image_id": str(uuid4())} for item in blobs]
    elif damage == "two-manifests":
        blobs.append({**blobs[0], "ordinal": 1})
    elif damage == "project":
        blobs[0]["project_id"] = "e" * 32
    elif damage == "uncommitted":
        # Manifest that does not name its closure: its digest would not commit to these blobs.
        image["manifest_json"] = {**image["manifest_json"], "source": {"kind": "local"}}
    source = create_async_engine(source_url)
    try:
        async with source.begin() as connection:
            await connection.execute(Base.metadata.tables["palimpsest_hub_published_images"].insert(), [image])
            await connection.execute(Base.metadata.tables["palimpsest_hub_image_source_blobs"].insert(), blobs)
    finally:
        await source.dispose()
    destination = create_async_engine(destination_url)
    try:
        if damage:
            with pytest.raises(MigrationError):
                await migrate(source_url, destination_url)
            async with destination.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_image_source_blobs")) == 0
            return
        copied = await migrate(source_url, destination_url)
        assert copied["palimpsest_hub_image_source_blobs"] == 3
        async with destination.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT role, blob_digest, size_bytes FROM palimpsest_hub_image_source_blobs ORDER BY role")
                )
            ).all()
            assert [tuple(row) for row in rows] == [
                ("config", "sha256:" + "5" * 64, 500),
                ("layer", "sha256:" + "6" * 64, 600),
                ("manifest", "sha256:" + "4" * 64, 400),
            ]
    finally:
        await destination.dispose()


def _image_key_row(**changes) -> dict:
    created = datetime(2026, 11, 1, 8, 0, 0, 123456)
    return {
        "id": "e" * 32,
        "project_id": "c" * 32,
        "owner_user_id": "f" * 64,
        "secret_hash": "9" * 64,
        "name": "ci publisher",
        "actions": ["images:publish", "images:read"],
        "created_at": created,
        "expires_at": created + timedelta(days=90),
        "revoked_at": created + timedelta(days=2),
        "last_used_at": created + timedelta(days=1, microseconds=7),
        **changes,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "plaintext-secret",
        "uppercase-hash",
        "lifetime-over-90-days",
        "expires-before-issue",
        "revoked-before-issue",
        "unknown-action",
        "duplicate-action",
        "bad-owner",
        "bad-project",
        "bad-id",
    ],
)
async def test_migrate_copies_image_keys_verbatim_or_rejects_corrupt_rows(tmp_path: Path, damage):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'keys-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'keys-destination.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    keys = Base.metadata.tables["palimpsest_image_keys"]
    created = _image_key_row()["created_at"]
    # A valid, already-expired and never-used key is preserved as well.
    expired = _image_key_row(
        id="d" * 32, actions=["images:read"], expires_at=created + timedelta(days=1), revoked_at=None, last_used_at=None
    )
    changes = {
        None: {},
        "plaintext-secret": {"secret_hash": "ppi_v1_" + "0" * 32 + "." + "A" * 43},
        "uppercase-hash": {"secret_hash": "A" * 64},
        "lifetime-over-90-days": {"expires_at": created + timedelta(days=90, seconds=1)},
        "expires-before-issue": {"expires_at": created},
        "revoked-before-issue": {"revoked_at": created - timedelta(seconds=1)},
        "unknown-action": {"actions": ["images:read", "packages:write"]},
        "duplicate-action": {"actions": ["images:read", "images:read"]},
        "bad-owner": {"owner_user_id": "bad owner"},
        "bad-project": {"project_id": ""},
        "bad-id": {"id": "E" * 32},
    }[damage]
    rows = [_image_key_row(**changes), expired]
    source = create_async_engine(source_url)
    try:
        async with source.begin() as connection:
            await connection.execute(keys.insert(), rows)
    finally:
        await source.dispose()
    destination = create_async_engine(destination_url)
    try:
        if damage:
            with pytest.raises(MigrationError, match="palimpsest_image_keys"):
                await migrate(source_url, destination_url)
            async with destination.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layers")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_image_keys")) == 0
            return
        copied = await migrate(source_url, destination_url)
        assert copied["palimpsest_image_keys"] == 2 and copied["palimpsest_hub_layers"] == 1
        async with destination.connect() as connection:
            stored = [dict(row) for row in (await connection.execute(select(keys).order_by(keys.c.id))).mappings()]
        assert stored == sorted(rows, key=lambda row: row["id"])
    finally:
        await destination.dispose()


@pytest.mark.asyncio
async def test_migrate_accepts_a_source_from_before_image_keys(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'pre-keys.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'keys-ready.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    source = create_async_engine(source_url)
    try:
        async with source.begin() as connection:
            await connection.execute(text("DROP TABLE palimpsest_image_keys"))
    finally:
        await source.dispose()
    copied = await migrate(source_url, destination_url)
    assert copied["palimpsest_hub_layers"] == 1 and copied["palimpsest_image_keys"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("include_delegations", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_migrate_preserves_actual_export_and_delegation_metadata(tmp_path, include_delegations, dry_run):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'source-models.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'destination-models.sqlite'}"
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    exports = PalimpsestImageExport.__table__
    delegations = PalimpsestImageExportDelegation.__table__
    now = datetime(2026, 10, 8, tzinfo=UTC)
    states = ["pending", "active", "cleanup", "deleted", "expired"] if include_delegations else ["legacy"]
    try:
        async with source.begin() as connection:
            tables = [table for table in Base.metadata.sorted_tables if include_delegations or table != delegations]
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
            for index, state in enumerate(states):
                export_id = f"00000000-0000-4000-8000-{index:012d}"
                await connection.execute(
                    exports.insert().values(
                        id=export_id,
                        project_id="a" * 32,
                        created_by="f" * 64,
                        source_image_id="11111111-2222-4333-8444-555555555555",
                        source_name="migration-image",
                        source_disk_format="raw",
                        source_size_bytes=1024,
                        source_fingerprint=f"{index:064x}",
                        artifact_key=f"{index:064x}",
                        target_disk_format="raw",
                        status="queued",
                        attempts=2,
                        next_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                if include_delegations:
                    await connection.execute(
                        delegations.insert().values(
                            trust_id=f"{index:032x}",
                            export_id=None if state == "pending" else export_id,
                            project_id="a" * 32,
                            trustor_user_id="f" * 64,
                            trustee_user_id="5" * 32,
                            role_names=["member"],
                            expires_at=now + timedelta(hours=6),
                            state=state,
                            cleanup_attempts=3 if state == "cleanup" else 0,
                            cleanup_next_at=now if state == "cleanup" else None,
                            created_at=now,
                            updated_at=now,
                            finished_at=now if state in {"deleted", "expired"} else None,
                        )
                    )
        async with destination.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        copied = await migrate(source_url, destination_url, dry_run=dry_run)
        assert copied[exports.name] == len(states)
        assert copied[delegations.name] == (len(states) if include_delegations else 0)
        async with source.connect() as original, destination.connect() as migrated:
            original_jobs = list((await original.execute(select(exports).order_by(exports.c.id))).mappings())
            migrated_jobs = list((await migrated.execute(select(exports).order_by(exports.c.id))).mappings())
            # Migration copies queue/attempt metadata unchanged; it does not fabricate legacy authority.
            assert migrated_jobs == ([] if dry_run else original_jobs)
            migrated_trusts = list(
                (await migrated.execute(select(delegations).order_by(delegations.c.trust_id))).mappings()
            )
            if include_delegations and not dry_run:
                original_trusts = list(
                    (await original.execute(select(delegations).order_by(delegations.c.trust_id))).mappings()
                )
                assert migrated_trusts == original_trusts
                assert migrated_trusts[0]["role_names"] == ["member"]
            else:
                assert migrated_trusts == []
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_migration_does_not_transfer_transient_worker_or_api_mutex_ownership(tmp_path, dry_run):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'roles-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'roles-destination.sqlite'}"
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    roles = PalimpsestWorkerLease.__table__
    try:
        for engine in (source, destination):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with source.begin() as connection:
            await connection.execute(
                roles.insert(),
                [
                    {"name": name, "token": "old-process-token", "expires_at": datetime.now(UTC)}
                    for name in ("export-maintenance", "export-delegation-cleanup", "build-api:single-flight")
                ],
            )
        copied = await migrate(source_url, destination_url, dry_run=dry_run)
        assert roles.name not in copied
        async with destination.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_worker_leases")) == 0
        async with source.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_worker_leases")) == 3
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
async def test_migration_preserves_build_receipt_and_deadline_but_retires_sql_execution_node(tmp_path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'build-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'build-destination.sqlite'}"
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    builds = PalimpsestHubBuild.__table__
    now = datetime(2026, 10, 14, tzinfo=UTC)
    try:
        for engine in (source, destination):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with source.begin() as connection:
            await connection.execute(text("ALTER TABLE palimpsest_hub_builds ADD COLUMN execution_node VARCHAR(128)"))
            await connection.execute(
                builds.insert().values(
                    id="00000000-0000-4000-8000-000000000001",
                    project_id="original-project",
                    created_by="original-user",
                    name="private-build",
                    recipe="RUN never-replay",
                    recipe_digest="sha256:" + "1" * 64,
                    base_digest="sha256:" + "2" * 64,
                    layer_digests=[],
                    status="error",
                    error_code="worker_interrupted",
                    lease_owner="00000000-0000-4000-8000-000000000002",
                    lease_expires_at=now,
                    started_at=now - timedelta(seconds=60),
                    completed_at=now,
                    created_at=now,
                )
            )
            await connection.execute(text("UPDATE palimpsest_hub_builds SET execution_node='retired-kvm-node'"))
        assert (await migrate(source_url, destination_url))[builds.name] == 1
        async with source.connect() as original, destination.connect() as migrated:
            before = (await original.execute(select(builds))).mappings().one()
            after = (await migrated.execute(select(builds))).mappings().one()
            assert after == before
            assert after["status"] == "error" and after["lease_owner"] == "00000000-0000-4000-8000-000000000002"
            assert "execution_node" not in after
            assert await original.scalar(text("SELECT execution_node FROM palimpsest_hub_builds")) == "retired-kvm-node"
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_receipts", [False, True])
async def test_migration_preserves_claim_cursors_or_accepts_old_schema(tmp_path, include_receipts):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'claim-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'claim-destination.sqlite'}"
    source, destination = create_async_engine(source_url), create_async_engine(destination_url)
    receipts = PalimpsestWorkerClaim.__table__
    try:
        async with source.begin() as connection:
            tables = [table for table in Base.metadata.sorted_tables if include_receipts or table != receipts]
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
            if include_receipts:
                await connection.execute(
                    receipts.insert(),
                    [
                        {
                            "channel": "00000000-0000-4000-8000-000000000001",
                            "protocol": "image",
                            "sequence": 81,
                            "result_json": None,
                        },
                        {
                            "channel": "00000000-0000-4000-8000-000000000001",
                            "protocol": "recipe",
                            "sequence": 82,
                            "result_json": {
                                "id": "00000000-0000-4000-8000-000000000002",
                                "lease": "00000000-0000-4000-8000-000000000003",
                            },
                        },
                    ],
                )
        async with destination.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        copied = await migrate(source_url, destination_url)
        assert copied[receipts.name] == (2 if include_receipts else 0)
        async with destination.connect() as connection:
            after = list((await connection.execute(select(receipts).order_by(receipts.c.protocol))).mappings())
        if include_receipts:
            async with source.connect() as connection:
                before = list((await connection.execute(select(receipts).order_by(receipts.c.protocol))).mappings())
            assert after == before
        else:
            assert after == []
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_call", [False, True], ids=["legacy-server", "prepared-nova-no-call"])
async def test_migrate_preserves_real_image_instance_and_runtime_reference_rows(tmp_path, no_call):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub.services.blob_references import runtime_blob_referenced

    source_url = f"sqlite+aiosqlite:///{tmp_path / 'execution-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'execution-destination.sqlite'}"
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    context = PalimpsestHubBuildContext.__table__
    jobs = PalimpsestHubImageJob.__table__
    instances = PalimpsestHubInstance.__table__
    references = RuntimeBlobReference.__table__
    now = datetime(2026, 10, 30, 1, 2, 3, 456789, tzinfo=UTC)
    project = "a" * 32
    digest = "sha256:" + "b" * 64
    job_id = "00000000-0000-4000-8000-000000000001"
    instance_id = "00000000-0000-4000-8000-000000000002"
    prepared = {
        "version": 1,
        "phase": "nova_not_attempted",
        "volume_create_attempted": True,
        "server_create_attempted": False,
        "terminal": True,
    }
    spec = {"layer_digests": [digest], "command": ["/bin/true"]}
    if no_call:
        spec["_boot_prepare"] = prepared
    server_id = None if no_call else "00000000-0000-4000-8000-000000000005"
    try:
        for engine in (source, destination):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with source.begin() as connection:
            await connection.execute(
                context.insert().values(
                    project_id=project, digest=digest, size_bytes=1234, created_by="original-user", created_at=now
                )
            )
            await connection.execute(
                jobs.insert().values(
                    id=job_id,
                    project_id=project,
                    created_by="original-user",
                    request_id="00000000-0000-4000-8000-000000000003",
                    kind="dockerfile",
                    name="original-image",
                    request_digest="sha256:" + "c" * 64,
                    request_json={"context_digest": digest, "platform": "linux/amd64"},
                    context_digest=digest,
                    status="complete",
                    staged_outputs=[{"digest": digest, "size_bytes": 1234}],
                    output_digests=[digest],
                    result_json={"layer_digests": [digest]},
                    lease_owner="original-owner",
                    lease_expires_at=now + timedelta(minutes=5),
                    created_at=now,
                    started_at=now,
                    completed_at=now + timedelta(seconds=1),
                )
            )
            await connection.execute(
                instances.insert().values(
                    id=instance_id,
                    project_id=project,
                    created_by="original-user",
                    request_id="00000000-0000-4000-8000-000000000004",
                    request_digest="sha256:" + "d" * 64,
                    name="original-consumer",
                    spec_json=spec,
                    server_id=server_id,
                    boot_volume_id="00000000-0000-4000-8000-000000000006",
                    status="error",
                    error_code="create_bootstrap_expired" if no_call else "bootstrap_expired",
                    capability_hash=None,
                    result_json=None if no_call else {"exit_code": 1},
                    capability_expires_at=now + timedelta(minutes=10),
                    created_at=now,
                    updated_at=now,
                )
            )
            await connection.execute(
                references.insert().values(
                    resource_kind="instance", resource_id=instance_id, digest=digest, created_at=now
                )
            )

        copied = await migrate(source_url, destination_url)
        assert {table.name: copied[table.name] for table in (context, jobs, instances, references)} == {
            context.name: 1,
            jobs.name: 1,
            instances.name: 1,
            references.name: 1,
        }
        async with source.connect() as original, destination.connect() as migrated:
            for table in (context, jobs, instances, references):
                before = (await original.execute(select(table))).mappings().one()
                after = (await migrated.execute(select(table))).mappings().one()
                assert after == before
            job = (await migrated.execute(select(jobs))).mappings().one()
            instance = (await migrated.execute(select(instances))).mappings().one()
            assert job["lease_expires_at"].microsecond == instance["capability_expires_at"].microsecond == 456789
            assert job["output_digests"] == instance["spec_json"]["layer_digests"] == [digest]
            assert instance["server_id"] == server_id and instance["capability_hash"] is None
            assert instance["spec_json"] == spec
            assert instance["result_json"] == (None if no_call else {"exit_code": 1})
            assert instance["boot_volume_id"] == "00000000-0000-4000-8000-000000000006"
        async with async_sessionmaker(destination)() as session:
            assert await runtime_blob_referenced(session, digest)
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "orphan", "cycle"])
@pytest.mark.parametrize("text_source,text_destination", [(False, False), (True, False), (False, True), (True, True)])
async def test_published_graph_survives_migration_or_rejects_corrupt_parent_atomically(
    tmp_path, monkeypatch, damage, text_source, text_destination
):
    import hashlib
    from types import SimpleNamespace
    from uuid import uuid4

    from palimpsest_local.image_manifest import manifest_from_result
    from sqlalchemy import Text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from palimpsest_hub.services import published_images
    from palimpsest_hub.services.hub_store import LocalPathBlobStore

    source_url = f"sqlite+aiosqlite:///{tmp_path / 'published-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'published-destination.sqlite'}"
    for url, use_text in ((source_url, text_source), (destination_url, text_destination)):
        with monkeypatch.context() as schema:
            if use_text:
                # MariaDB reflects its JSON alias as LONGTEXT, not SQLAlchemy JSON.
                schema.setattr(
                    Base.metadata.tables["palimpsest_hub_image_memberships"].c.reuse_projects_json, "type", Text()
                )
            await create_database(url, with_rows=False)
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    factory = async_sessionmaker(source, expire_on_commit=False)
    project = "c" * 32
    actor = {"project_id": project, "user_id": "d" * 32}
    monkeypatch.setattr(published_images, "get_settings", lambda: SimpleNamespace())
    store = LocalPathBlobStore(tmp_path / "payloads")
    descriptors = []
    try:
        async with factory() as session:
            for index in range(3):
                payload = b"hsqs-metadata-migration-" + bytes([index])
                path = tmp_path / f"layer-{index}"
                path.write_bytes(payload)
                verified = store.inspect_file(path, max_bytes=4096)
                store.publish_verified(path, verified)
                descriptors.append({"digest": verified.blob_digest, "size_bytes": verified.size_bytes})
                await session.execute(
                    Base.metadata.tables["palimpsest_hub_publication_payloads"]
                    .insert()
                    .values(
                        project_id=project,
                        created_by=actor["user_id"],
                        blob_digest=verified.blob_digest,
                        size_bytes=verified.size_bytes,
                    )
                )
            # Three private names exceed the free seed (2); this test is about graph migration, not quota.
            session.add(
                PalimpsestHubImageQuotaTier(
                    subject_kind="user", subject_id=actor["user_id"], tier="paid", updated_by="test"
                )
            )
            await session.commit()
        images = []
        for branch, indices in (("left", [0, 1]), ("right", [0, 2]), ("reused", [2, 1])):
            manifest = manifest_from_result(
                {
                    "layers": [descriptors[index] for index in indices],
                    "source": {},
                    "runtime": {"entrypoint": ["/bin/sh"], "cmd": [], "env": [], "working_dir": "/", "user": "0"},
                },
                os_family="fixture-os",
                os_version="1",
                source_kind="local",
                metadata={"branch": branch},
            )
            images.append(
                await published_images.publish_image(
                    factory,
                    store,
                    actor,
                    str(uuid4()),
                    branch,
                    manifest,
                )
            )
        if damage:
            parent = (
                "sha256:" + hashlib.sha256(b"missing-parent").hexdigest()
                if damage == "orphan"
                else images[0]["leaf_id"]
            )
            async with source.begin() as connection:
                await connection.execute(
                    text("UPDATE palimpsest_hub_layer_nodes SET parent_id=:parent WHERE id=:root"),
                    {"parent": parent, "root": images[0]["nodes"][0]["id"]},
                )
            with pytest.raises(MigrationError):
                await migrate(source_url, destination_url)
            async with destination.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layers")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_published_images")) == 0
        else:
            await migrate(source_url, destination_url)
            migrated = [
                await published_images.resolve_image(
                    async_sessionmaker(destination, expire_on_commit=False),
                    image["id"],
                    actor,
                )
                for image in images
            ]
            assert migrated[0]["nodes"][0]["id"] == migrated[1]["nodes"][0]["id"]
            assert migrated[0]["nodes"][1]["parent_id"] == migrated[1]["nodes"][1]["parent_id"]
            assert migrated[0]["nodes"][1]["id"] != migrated[2]["nodes"][1]["id"]
            assert migrated[0]["nodes"][1]["digest"] == migrated[2]["nodes"][1]["digest"]
            assert [image["manifest"]["metadata"]["branch"] for image in migrated] == ["left", "right", "reused"]
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("creator", [None, "", "   "], ids=["null-creator", "empty-creator", "blank-creator"])
async def test_migrate_prepublication_schema_accepts_only_reviewed_exact_creator_mapping(tmp_path, dry_run, creator):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    source_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy-acl.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'reviewed-acl.sqlite'}"
    await create_database(source_url, with_rows=False)
    await create_database(destination_url, with_rows=False)
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    project = "original-project"
    digest = "sha256:" + "1" * 64
    try:
        async with source.begin() as connection:
            await connection.execute(text("DROP TABLE palimpsest_hub_build_contexts"))
            await connection.execute(
                text("""
                CREATE TABLE palimpsest_hub_build_contexts (
                    project_id VARCHAR(64) NOT NULL, digest VARCHAR(71) NOT NULL,
                    size_bytes BIGINT NOT NULL, created_by VARCHAR(128), created_at DATETIME NOT NULL,
                    PRIMARY KEY (project_id, digest)
                )
            """)
            )
            await connection.execute(
                text("""
                INSERT INTO palimpsest_hub_build_contexts
                (project_id, digest, size_bytes, created_by, created_at)
                VALUES (:project, :digest, 321, :creator, '2026-11-02 01:02:03.123456')
            """),
                {"project": project, "digest": digest, "creator": creator},
            )
            # Same baseline image-job schema: no visibility column, nullable real creator.
            await connection.execute(text("ALTER TABLE palimpsest_hub_image_jobs DROP COLUMN visibility"))
            await connection.execute(
                text("""
                INSERT INTO palimpsest_hub_image_jobs
                (id, project_id, created_by, request_id, kind, name, request_digest, request_json,
                 status, staged_outputs, output_digests, created_at)
                VALUES ('00000000-0000-4000-8000-000000000001', :project, NULL,
                    '00000000-0000-4000-8000-000000000002', 'registry', 'old-receipt', :digest,
                    '{}', 'complete', '[]', '[]', '2026-11-02 01:02:03.123456')
            """),
                {"project": project, "digest": digest},
            )
        with pytest.raises(MigrationError, match="reviewed exact-row mapping"):
            await migrate(source_url, destination_url, dry_run=dry_run)
        mapping = [
            {
                "table": "palimpsest_hub_build_contexts",
                "key": {"project_id": project, "digest": digest},
                "values": {"created_by": "evidence-proven-original-user"},
            }
        ]
        copied = await migrate(source_url, destination_url, dry_run=dry_run, reviewed_mapping=mapping)
        assert copied["palimpsest_hub_build_contexts"] == copied["palimpsest_hub_image_jobs"] == 1
        async with async_sessionmaker(destination)() as session:
            contexts = (await session.scalars(select(PalimpsestHubBuildContext))).all()
            jobs = (await session.scalars(select(PalimpsestHubImageJob))).all()
            if dry_run:
                assert not contexts and not jobs
            else:
                assert len(contexts) == len(jobs) == 1
                assert contexts[0].created_by == "evidence-proven-original-user"
                assert contexts[0].created_at.microsecond == 123456
                assert jobs[0].visibility == "internal" and jobs[0].created_by is None
                assert jobs[0].request_json == {} and jobs[0].status == "complete"
        async with destination.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_published_images")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layer_nodes")) == 0
        async with source.connect() as connection:
            assert await connection.scalar(text("SELECT created_by FROM palimpsest_hub_build_contexts")) == creator
    finally:
        await source.dispose()
        await destination.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial-key", "wrong-row", "overwrite", "unknown-table", "duplicate"])
async def test_reviewed_mapping_rejects_ambiguous_or_unused_facts_before_copy(tmp_path, damage):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'mapping-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'mapping-destination.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    digest = "sha256:" + "3" * 64
    source_creator = "" if damage == "duplicate" else "known-user"
    try:
        async with source.begin() as connection:
            await connection.execute(
                PalimpsestHubBuildContext.__table__.insert().values(
                    project_id="project", digest=digest, size_bytes=10, created_by=source_creator
                )
            )
        entry = {
            "table": "palimpsest_hub_build_contexts",
            "key": {"project_id": "project", "digest": digest, "created_by": source_creator},
            "values": {"created_by": "different-user"},
        }
        if damage == "partial-key":
            entry["key"].pop("created_by")
        elif damage == "wrong-row":
            entry["key"]["digest"] = "sha256:" + "4" * 64
        elif damage == "unknown-table":
            entry["table"] = "not-a-real-table"
        mapping = [entry, entry] if damage == "duplicate" else [entry]
        message = {
            "partial-key": "complete exact source primary key",
            "wrong-row": "match exactly one source row",
            "overwrite": "cannot overwrite a known source fact",
            "unknown-table": "unused or unknown-table",
            "duplicate": "match exactly one source row",
        }[damage]
        with pytest.raises(MigrationError, match=message):
            await migrate(source_url, destination_url, reviewed_mapping=mapping)
        async with destination.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_layers")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM palimpsest_hub_build_contexts")) == 0
    finally:
        await source.dispose()
        await destination.dispose()


def test_migrate_cli_accepts_reviewed_mapping_path():
    from palimpsest_hub.migrate import parse_args

    args = parse_args(
        [
            "--source-url",
            "sqlite+aiosqlite:///old.db",
            "--destination-url",
            "sqlite+aiosqlite:///new.db",
            "--reviewed-mapping",
            "/operator/reviewed-creators.json",
            "--dry-run",
        ]
    )
    assert args.reviewed_mapping == Path("/operator/reviewed-creators.json")
    assert args.dry_run is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source_has_ledger", [True, False], ids=["with-ledger", "pre-lifecycle-source"])
async def test_migrate_keeps_the_dev_instance_lifecycle_ledger_or_accepts_an_older_source(tmp_path, source_has_ledger):
    from palimpsest_hub.instance_lifecycle_models import PalimpsestHubInstanceLifecycle

    ledger = PalimpsestHubInstanceLifecycle.__table__
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'ledger-source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'ledger-destination.sqlite'}"
    source = create_async_engine(source_url)
    destination = create_async_engine(destination_url)
    now = datetime(2026, 11, 12, 1, 2, 3, 456789, tzinfo=UTC)
    row = {
        "instance_id": "00000000-0000-4000-8000-000000000010",
        "project_id": "a" * 32,
        "machine_json": {"phase": "grace", "sessions": [], "grace_started": None},
        "session_capability_hash": "e" * 64,
        "reported_sessions_json": ["sshd: alice@pts/0 (pid 200)"],
        "report_received_at": now,
        "trust_json": {"trust_id": "t" * 32, "role_names": ["member"]},
        "trust_expires_at": now + timedelta(days=7),
        "retired": False,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        async with destination.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with source.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if source_has_ledger:
                await connection.execute(ledger.insert().values(**row))
            else:
                await connection.execute(text("DROP TABLE palimpsest_hub_instance_lifecycle"))
        copied = await migrate(source_url, destination_url)
        assert copied[ledger.name] == (1 if source_has_ledger else 0)
        if source_has_ledger:
            async with source.connect() as original, destination.connect() as migrated:
                before = (await original.execute(select(ledger))).mappings().one()
                after = (await migrated.execute(select(ledger))).mappings().one()
                assert after == before and after["trust_json"]["trust_id"] == "t" * 32
    finally:
        await source.dispose()
        await destination.dispose()
