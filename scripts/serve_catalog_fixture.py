"""Serve only synthetic catalog fixtures on loopback; never load Hub settings/lifespan.

Use the Hub dev interpreter (aiosqlite/httpx installed). All rows are created in
an automatically removed private TemporaryDirectory. Identity is synthetic,
while native routes, DTO validation, require_global_admin fences and SQL are real.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import uvicorn

# This is an explicitly test-only executable, not a production app factory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hub" / "tests"))
from catalog_fixture import isolated_catalog  # noqa: E402


async def serve(port):
    with tempfile.TemporaryDirectory(prefix="palimpsest-catalog-fixture-") as directory:
        async with isolated_catalog(Path(directory)) as fixture:
            server = uvicorn.Server(uvicorn.Config(fixture.app, host="127.0.0.1", port=port, lifespan="off"))
            await server.serve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18029)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    asyncio.run(serve(args.port))


if __name__ == "__main__":
    main()
