from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from palimpsest_hub.migrate import MigrationError, migrate

_TABLES = ("palimpsest_hub_layers", "palimpsest_hub_uploads", "palimpsest_image_exports")


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
async def test_migrate_rejects_same_database():
    with pytest.raises(MigrationError, match="must differ"):
        await migrate("sqlite+aiosqlite:///same.sqlite", "sqlite+aiosqlite:///same.sqlite")
