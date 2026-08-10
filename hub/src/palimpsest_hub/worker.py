"""Palimpsest Glance Image Export Worker process entrypoint."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import sys
import uuid
from contextlib import suppress

from palimpsest_hub.config import get_settings
from palimpsest_hub.database import close_db, init_db
from palimpsest_hub.services.image_exports import (
    process_one_image_export,
    run_export_maintenance,
    validate_qemu_img_support,
)

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = get_settings()
    if not settings.database_url:
        logger.error("palimpsest_worker requires database_url to be configured")
        sys.exit(1)

    await validate_qemu_img_support()
    logger.info("qemu-img supports all configured Palimpsest image formats")

    init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout=settings.database_connect_timeout,
        pool_timeout=settings.database_pool_timeout,
        unhealthy_seconds=settings.database_unhealthy_seconds,
    )
    logger.info("Palimpsest worker initialized database connection pool")

    worker_id = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    logger.info("Palimpsest worker starting (owner=%s)", worker_id)

    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Received termination signal, stopping worker...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal)

    last_maint = 0.0
    maint_interval = 3600.0  # Run maintenance once per hour

    try:
        while not stop_event.is_set():
            processed = False
            try:
                processed = await process_one_image_export(owner=worker_id)
            except Exception:
                logger.error("Unexpected error in process_one_image_export", exc_info=True)

            now = loop.time()
            if now - last_maint > maint_interval:
                try:
                    await run_export_maintenance()
                    last_maint = now
                except Exception:
                    logger.warning("Error running export maintenance", exc_info=True)

            if not processed and not stop_event.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=2.0)
    finally:
        logger.info("Palimpsest worker shutting down DB connection pool")
        await close_db()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
