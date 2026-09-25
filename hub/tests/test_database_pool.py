from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from palimpsest_hub.database import _recover_closed_transport_ping


@pytest.mark.asyncio
async def test_closed_transport_ping_reconnects_without_masking_unrelated_errors(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}", pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1

        original_ping = engine.sync_engine.dialect.do_ping
        failure = "unable to perform operation on <TCPTransport closed=True reading=False>; the handler is closed"

        def ping(connection):
            if failure:
                raise RuntimeError(failure)
            return original_ping(connection)

        engine.sync_engine.dialect.do_ping = ping
        _recover_closed_transport_ping(engine)

        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1

        failure = "unrelated application error"
        with pytest.raises(RuntimeError, match="unrelated application error"):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()
