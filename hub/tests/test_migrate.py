from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from palimpsest_hub.migrate import MigrationError, migrate
from palimpsest_hub.models import Base, PalimpsestImageExport, PalimpsestImageExportDelegation

_TABLES = (
    "palimpsest_hub_layers",
    "palimpsest_hub_layer_access",
    "palimpsest_hub_uploads",
    "palimpsest_image_exports",
    "palimpsest_image_export_delegations",
    "palimpsest_hub_builds",
    "palimpsest_package_namespaces",
    "palimpsest_packages",
    "palimpsest_package_keys",
    "palimpsest_package_versions",
    "palimpsest_package_blob_references",
    "palimpsest_package_tags",
    "palimpsest_package_uploads",
    "palimpsest_package_caches",
)


async def create_database(url: str, *, with_rows: bool) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for index, table in enumerate(_TABLES, start=1):
                await connection.execute(text(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, value TEXT NOT NULL)"))
                if with_rows:
                    await connection.execute(
                        text(f"INSERT INTO {table} (id, value) VALUES (:id, :value)"),
                        {"id": str(index), "value": table},
                    )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migrate_copies_each_hub_table(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'source.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'destination.sqlite'}"
    await create_database(source_url, with_rows=True)
    await create_database(destination_url, with_rows=False)

    copied = await migrate(source_url, destination_url)

    assert copied == {table: 1 for table in _TABLES}
    destination = create_async_engine(destination_url)
    try:
        async with destination.connect() as connection:
            for table in _TABLES:
                assert (await connection.scalar(text(f"SELECT value FROM {table}"))) == table
    finally:
        await destination.dispose()


@pytest.mark.asyncio
async def test_migrate_accepts_legacy_source_without_build_table(tmp_path: Path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'old.sqlite'}"
    destination_url = f"sqlite+aiosqlite:///{tmp_path / 'new.sqlite'}"
    engine = create_async_engine(source_url)
    try:
        async with engine.begin() as connection:
            for table in ("palimpsest_hub_layers", "palimpsest_hub_uploads", "palimpsest_image_exports"):
                await connection.execute(text(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, value TEXT NOT NULL)"))
    finally:
        await engine.dispose()
    await create_database(destination_url, with_rows=False)
    copied = await migrate(source_url, destination_url)
    assert copied == {table: 0 for table in _TABLES}


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
