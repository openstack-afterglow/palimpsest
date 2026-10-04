from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from palimpsest_hub.migrate import MigrationError, migrate

_TABLES = (
    "palimpsest_hub_layers",
    "palimpsest_hub_layer_access",
    "palimpsest_hub_uploads",
    "palimpsest_image_exports",
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
