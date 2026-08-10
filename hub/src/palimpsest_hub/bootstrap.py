from __future__ import annotations

import asyncio

from palimpsest_hub.config import get_settings
from palimpsest_hub.database import close_db, create_schema, init_db


async def main() -> None:
    settings = get_settings()
    init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout=settings.database_connect_timeout,
        pool_timeout=settings.database_pool_timeout,
        unhealthy_seconds=settings.database_unhealthy_seconds,
    )
    try:
        await create_schema()
    finally:
        await close_db()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
