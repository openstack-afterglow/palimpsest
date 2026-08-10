from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy import MetaData, Table, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_TABLES = ("palimpsest_hub_layers", "palimpsest_hub_uploads", "palimpsest_image_exports")


class MigrationError(RuntimeError):
    pass


async def _reflect(connection: AsyncConnection, table_name: str) -> Table:
    def reflect(sync_connection) -> Table:
        metadata = MetaData()
        return Table(table_name, metadata, autoload_with=sync_connection)

    return await connection.run_sync(reflect)


async def _row_count(connection: AsyncConnection, table: Table) -> int:
    return int((await connection.scalar(select(func.count()).select_from(table))) or 0)


async def migrate(source_url: str, destination_url: str, *, dry_run: bool = False) -> dict[str, int]:
    if make_url(source_url) == make_url(destination_url):
        raise MigrationError("source and destination databases must differ")

    source_engine = create_async_engine(source_url, pool_pre_ping=True)
    destination_engine = create_async_engine(destination_url, pool_pre_ping=True)
    copied: dict[str, int] = {}
    try:
        async with source_engine.connect() as source, destination_engine.begin() as destination:
            for table_name in _TABLES:
                source_table = await _reflect(source, table_name)
                destination_table = await _reflect(destination, table_name)
                destination_count = await _row_count(destination, destination_table)
                if destination_count:
                    raise MigrationError(f"destination table {table_name!r} is not empty")
                rows = [dict(row) for row in (await source.execute(select(source_table))).mappings()]
                copied[table_name] = len(rows)
                if rows and not dry_run:
                    await destination.execute(destination_table.insert(), rows)
        return copied
    finally:
        await source_engine.dispose()
        await destination_engine.dispose()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy Palimpsest Hub state into an initialized dedicated database")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--destination-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def run() -> None:
    args = parse_args()
    copied = asyncio.run(migrate(args.source_url, args.destination_url, dry_run=args.dry_run))
    mode = "would copy" if args.dry_run else "copied"
    print(f"{mode}: " + ", ".join(f"{table}={count}" for table, count in copied.items()))


if __name__ == "__main__":
    run()
