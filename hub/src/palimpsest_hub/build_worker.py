"""Single-owner, KVM-capable Hub build worker entrypoint.

Run separately from the unprivileged API and Glance export containers. The
configured palimpsest-local interpreter owns VM execution; this process never
receives caller Keystone tokens.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import socket
import subprocess
import sys
import uuid
from contextlib import suppress
from pathlib import Path

from palimpsest_hub.config import get_build_worker_settings
from palimpsest_hub.database import close_db, init_db
from palimpsest_hub.services.builds import fail_interrupted_builds, process_one_hub_build
from palimpsest_hub.services.hub_store import get_blob_store

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = get_build_worker_settings()
    if sys.platform != "linux" or not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("server-side builds require a Linux host with accessible /dev/kvm")
    python = Path(settings.palimpsest_hub_builder_python)
    if not python.is_absolute() or not python.is_file():
        raise RuntimeError("PALIMPSEST_HUB_BUILDER_PYTHON must be a KVM-capable absolute interpreter path")
    check = subprocess.run(
        [str(python), "-I", "-m", "palimpsest_local.hub_builder", "preflight"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": "/nonexistent"},
        timeout=30,
        check=False,
    )
    if check.returncode:
        raise RuntimeError("the isolated builder host did not pass its KVM preflight")
    store = get_blob_store(settings)
    store.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    lock_fd = os.open(store.locks_dir / "build-worker.lock", flags, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("a build worker already owns this Hub store") from exc
        init_db(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            connect_timeout=settings.database_connect_timeout,
            pool_timeout=settings.database_pool_timeout,
            unhealthy_seconds=settings.database_unhealthy_seconds,
        )
        try:
            interrupted = await fail_interrupted_builds()
            if interrupted:
                logger.warning("Reconciled %d interrupted or retained private build jobs", interrupted)
            owner = f"build-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            while not stop.is_set():
                processed = await process_one_hub_build(owner)
                if not processed:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=2.0)
        finally:
            await close_db()
    finally:
        os.close(lock_fd)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
