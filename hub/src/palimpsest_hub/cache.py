from __future__ import annotations

import asyncio

import redis.asyncio as redis
from redis.asyncio.connection import SSLConnection, parse_url
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import Sentinel
from redis.backoff import NoBackoff

from palimpsest_hub.config import get_settings

_client: redis.Redis | None = None
_sentinel: Sentinel | None = None
_client_lock = asyncio.Lock()


async def _close_clients(client: redis.Redis | None, sentinel: Sentinel | None) -> None:
    # Older supported redis-py releases lack Sentinel.aclose(). Explicitly close
    # every discovery client as well as the caller-owned data client's pool.
    clients = ([client] if client is not None else []) + (sentinel.sentinels if sentinel is not None else [])
    results = await asyncio.gather(
        *(owned.aclose(close_connection_pool=True) for owned in clients), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def get_redis() -> redis.Redis:
    global _client, _sentinel
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        settings = get_settings()
        sentinel = None
        client = None
        try:
            if settings.redis_sentinel_addresses:
                options = dict(parse_url(settings.redis_url))
                # URL credentials/DB/options survive, but its seed must never
                # bypass discovery or override Sentinel's managed connection.
                options.pop("host", None)
                options.pop("port", None)
                if options.pop("connection_class", None) is SSLConnection:
                    options["ssl"] = True
                options.setdefault("socket_connect_timeout", 2.0)
                options.setdefault("socket_timeout", 2.0)
                # An interrupted write might have succeeded. Let redis-py
                # rediscover on reconnect without replaying any operation.
                options["retry"] = Retry(NoBackoff(), 0)
                options.setdefault("decode_responses", False)
                sentinel_options = {
                    name: value for name, value in options.items() if name.startswith(("socket_", "ssl"))
                }
                sentinel_options["retry"] = Retry(NoBackoff(), 0)
                sentinel_options["password"] = settings.redis_sentinel_password.get_secret_value() or None
                sentinel = Sentinel(settings.redis_sentinel_addresses, sentinel_kwargs=sentinel_options)
                client = sentinel.master_for(settings.redis_sentinel_master, **options)
            else:
                client = redis.from_url(settings.redis_url, decode_responses=False)
            await client.ping()
        except BaseException:
            await _close_clients(client, sentinel)
            raise
        _client, _sentinel = client, sentinel
        return client


async def close_redis() -> None:
    global _client, _sentinel
    async with _client_lock:
        client, sentinel = _client, _sentinel
        _client, _sentinel = None, None
        await _close_clients(client, sentinel)
