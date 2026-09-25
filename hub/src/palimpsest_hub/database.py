from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _recover_closed_transport_ping(engine: AsyncEngine) -> None:
    ping = engine.sync_engine.dialect.do_ping

    def do_ping(connection) -> bool:
        try:
            return ping(connection)
        except RuntimeError as exc:
            # asyncmy lets uvloop's closed-socket RuntimeError escape its DBAPI adapter.
            # Report only that disconnected transport to SQLAlchemy's pool pre-ping.
            message = str(exc)
            if "TCPTransport closed=True" not in message or "handler is closed" not in message:
                raise
            return False

    engine.sync_engine.dialect.do_ping = do_ping


def init_db(
    database_url: str,
    *,
    pool_size: int,
    max_overflow: int,
    connect_timeout: int,
    pool_timeout: int,
    unhealthy_seconds: int,
) -> None:
    del unhealthy_seconds
    global _engine, _session_factory
    if _engine is not None:
        return
    _engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        connect_args={"connect_timeout": connect_timeout},
    )
    if _engine.sync_engine.dialect.driver == "asyncmy":
        _recover_closed_transport_ping(_engine)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail="Palimpsest Hub database is unavailable")
    async with factory() as session:
        yield session


async def create_schema() -> None:
    if _engine is None:
        raise RuntimeError("database is not initialized")
    from palimpsest_hub.models import Base

    async with _engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
