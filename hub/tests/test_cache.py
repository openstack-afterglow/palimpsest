from __future__ import annotations

import asyncio
import json
import traceback
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError

from palimpsest_hub import cache
from palimpsest_hub.config import Settings


def _settings(*, sentinel_from_environment=False, **overrides) -> Settings:
    values = {
        "database_url": "mysql+asyncmy://synthetic:synthetic@invalid/hub",
        "redis_url": "redis://unused/9",
        "palimpsest_hub_local_path": "/synthetic/hub",
        "os_auth_url": "https://keystone.invalid/v3",
        "os_username": "synthetic",
        "os_password": "synthetic",
        "os_project_name": "synthetic",
        "redis_sentinel_addresses": (),
        "redis_sentinel_master": "",
        "redis_sentinel_password": "",
    }
    if sentinel_from_environment:
        for name in ("redis_sentinel_addresses", "redis_sentinel_master", "redis_sentinel_password"):
            values.pop(name)
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"redis_sentinel_addresses": (("localhost", 26379),)},
        {"redis_sentinel_master": "kolla"},
        {"redis_sentinel_password": "orphan-secret"},
        {"redis_sentinel_addresses": (("localhost", 0),), "redis_sentinel_master": "kolla"},
        {"redis_sentinel_addresses": (("localhost", 65536),), "redis_sentinel_master": "kolla"},
        {"redis_sentinel_addresses": (("host/path", 26379),), "redis_sentinel_master": "kolla"},
        {"redis_sentinel_addresses": (("host name", 26379),), "redis_sentinel_master": "kolla"},
        {"redis_sentinel_addresses": (("[::1]", 26379),), "redis_sentinel_master": "kolla"},
        {"redis_sentinel_addresses": (("localhost", 26379),) * 17, "redis_sentinel_master": "kolla"},
        {"redis_sentinel_addresses": (("localhost", 26379),), "redis_sentinel_master": "kolla\n"},
    ],
)
def test_incoherent_sentinel_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        _settings(**overrides)


def test_sentinel_endpoint_uniqueness_uses_coerced_port_values():
    with pytest.raises(ValidationError):
        _settings(
            redis_sentinel_addresses=(("localhost", 26379), ("localhost", "26379")),
            redis_sentinel_master="kolla",
        )


@pytest.mark.parametrize("timeout", ["0", "nan", "31"])
@pytest.mark.parametrize("option", ["socket_timeout", "socket_connect_timeout"])
def test_sentinel_socket_waits_must_be_finite_and_bounded(option, timeout):
    with pytest.raises(ValidationError):
        _settings(
            redis_sentinel_addresses=(("localhost", 26379),),
            redis_sentinel_master="kolla",
            redis_url=f"redis://unused/9?{option}={timeout}",
        )


@pytest.mark.parametrize("url", ["unix:///tmp/redis.sock", "redis://unused/9?connection_class=arbitrary"])
def test_sentinel_discovery_cannot_be_replaced_by_an_unmanaged_connection(url):
    with pytest.raises(ValidationError):
        _settings(redis_sentinel_addresses=(("localhost", 26379),), redis_sentinel_master="kolla", redis_url=url)


def test_invalid_sentinel_settings_do_not_echo_credentials_in_validation_errors():
    with pytest.raises(ValidationError) as rejected:
        _settings(
            redis_sentinel_password="synthetic-sentinel-secret",
            redis_url="redis://:synthetic-data-secret@unused/9",
        )
    assert "synthetic-sentinel-secret" not in str(rejected.value)
    assert "synthetic-data-secret" not in str(rejected.value)


def test_invalid_sentinel_data_url_keeps_credentials_out_of_startup_tracebacks():
    secret = "synthetic-parser-secret"
    with pytest.raises(ValidationError) as rejected:
        _settings(
            redis_sentinel_addresses=(("localhost", 26379),),
            redis_sentinel_master="kolla",
            redis_url=f"redis://username:{secret}@invalid\uff1a6379/9",
        )
    assert secret not in "".join(traceback.format_exception(rejected.value))


def test_sentinel_environment_addresses_use_json_pairs_and_password_stays_secret(monkeypatch):
    monkeypatch.setenv("REDIS_SENTINEL_ADDRESSES", json.dumps([["controller1", 26379], ["::1", 26379]]))
    monkeypatch.setenv("REDIS_SENTINEL_MASTER", "kolla")
    monkeypatch.setenv("REDIS_SENTINEL_PASSWORD", "sentinel-only-secret")
    settings = _settings(
        sentinel_from_environment=True,
        redis_url="rediss://data-user:data-secret@unused/9?socket_timeout=1&socket_connect_timeout=2",
    )
    assert settings.redis_sentinel_addresses == (("controller1", 26379), ("::1", 26379))
    assert settings.redis_sentinel_password.get_secret_value() == "sentinel-only-secret"
    assert "sentinel-only-secret" not in repr(settings)


def _resp(value):
    if isinstance(value, dict):
        return (
            b"%" + str(len(value)).encode() + b"\r\n" + b"".join(_resp(item) for pair in value.items() for item in pair)
        )
    if isinstance(value, list):
        return b"*" + str(len(value)).encode() + b"\r\n" + b"".join(map(_resp, value))
    if isinstance(value, int):
        return b":" + str(value).encode() + b"\r\n"
    if value is None:
        return b"$-1\r\n"
    if isinstance(value, str):
        value = value.encode()
    return b"$" + str(len(value)).encode() + b"\r\n" + value + b"\r\n"


class _RedisWireEndpoint:
    """Adversarial RESP endpoint: real redis-py sockets, authentication and DB selection."""

    def __init__(self, *, credentials=()):
        self.credentials = credentials
        self.master = None
        self.commands = []
        self.tokens = {}
        self.drop_write_response = False
        self.read_only = False
        self.writers = set()
        self.handlers = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        authenticated = not self.credentials
        database = 0
        protocol = 2
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                assert line.startswith(b"*")
                command = []
                for _ in range(int(line[1:])):
                    length = await reader.readline()
                    assert length.startswith(b"$")
                    command.append((await reader.readexactly(int(length[1:]) + 2))[:-2])
                command[0] = command[0].upper()
                self.commands.append(tuple(command))
                name = command[0]
                if name == b"HELLO":
                    protocol = int(command[1])
                    if b"AUTH" in command:
                        expected = (b"default", *self.credentials) if len(self.credentials) == 1 else self.credentials
                        authenticated = tuple(command[command.index(b"AUTH") + 1 :]) == expected
                    if not authenticated:
                        writer.write(b"-WRONGPASS invalid credentials\r\n")
                    else:
                        writer.write(_resp({"proto": protocol}))
                elif name == b"AUTH":
                    authenticated = tuple(command[1:]) == self.credentials
                    writer.write(b"+OK\r\n" if authenticated else b"-WRONGPASS invalid credentials\r\n")
                elif not authenticated:
                    writer.write(b"-NOAUTH authentication required\r\n")
                elif name == b"CLIENT":
                    writer.write(b"+OK\r\n")
                elif name == b"SELECT":
                    database = int(command[1])
                    writer.write(b"+OK\r\n")
                elif name == b"PING":
                    writer.write(b"+PONG\r\n")
                elif name in (b"SENTINEL", b"SENTINEL MASTERS"):
                    masters = []
                    if self.master is not None:
                        state = {
                            "name": "kolla",
                            "ip": "127.0.0.1",
                            "port": self.master,
                            "flags": "master",
                            "num-other-sentinels": 2,
                        }
                        masters.append(state if protocol == 3 else [item for pair in state.items() for item in pair])
                    writer.write(_resp(masters))
                elif name == b"SETEX" and self.read_only:
                    writer.write(b"-READONLY demoted master\r\n")
                elif name == b"SETEX":
                    self.tokens[(database, command[1])] = command[3]
                    if self.drop_write_response:
                        writer.close()
                        return
                    writer.write(b"+OK\r\n")
                elif name == b"GET":
                    writer.write(_resp(self.tokens.get((database, command[1]))))
                else:
                    writer.write(b"-ERR unsupported command\r\n")
                await writer.drain()
        except (ConnectionError, ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.handlers.discard(task)

    @asynccontextmanager
    async def listen(self):
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            server.close()
            await server.wait_closed()
            for writer in tuple(self.writers):
                writer.close()
            if self.handlers:
                await asyncio.gather(*tuple(self.handlers))

    async def expect_closed(self):
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(self.handlers)), 1)
        assert not self.writers


@pytest.fixture
async def isolated_cache(monkeypatch):
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_sentinel", None)
    monkeypatch.setattr(cache, "_client_lock", asyncio.Lock())
    yield
    await cache.close_redis()


@pytest.mark.asyncio
async def test_unavailable_master_never_uses_seed_and_failed_initialization_closes_discovery(
    isolated_cache, monkeypatch
):
    from palimpsest_hub import main

    seed = _RedisWireEndpoint()
    discovery = _RedisWireEndpoint()
    async with seed.listen() as seed_port, discovery.listen() as sentinel_port:
        settings = _settings(
            redis_url=f"redis://127.0.0.1:{seed_port}/9?socket_timeout=0.1&socket_connect_timeout=0.1",
            redis_sentinel_addresses=(("127.0.0.1", sentinel_port),),
            redis_sentinel_master="kolla",
        )
        monkeypatch.setattr(cache, "get_settings", lambda: settings)
        with pytest.raises(ConnectionError):
            await main._check_redis()
        await discovery.expect_closed()
        assert seed.commands == []
        discovery.master = seed_port
        await main._check_redis()
        assert not seed.tokens
        client = await cache.get_redis()
        assert await client.setex("export-token", 60, b"original-project") is True
        assert await client.get("export-token") == b"original-project"
        await cache.close_redis()
        await seed.expect_closed()
        await discovery.expect_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel_credentials", [(), (b"sentinel-secret",)])
@pytest.mark.parametrize(
    "decode_query, expected", [("", b"original-project"), ("&decode_responses=True", "original-project")]
)
async def test_sentinel_and_data_auth_are_separate_and_concurrent_consumers_share_one_client(
    isolated_cache, monkeypatch, sentinel_credentials, decode_query, expected
):
    data = _RedisWireEndpoint(credentials=(b"data-user", b"data-secret"))
    discovery = _RedisWireEndpoint(credentials=sentinel_credentials)
    async with data.listen() as data_port, discovery.listen() as sentinel_port:
        discovery.master = data_port
        settings = _settings(
            redis_url="redis://data-user:data-secret@unused.invalid/9?protocol=2&socket_timeout=0.1" + decode_query,
            redis_sentinel_addresses=(("127.0.0.1", sentinel_port),),
            redis_sentinel_master="kolla",
            redis_sentinel_password=sentinel_credentials[0].decode() if sentinel_credentials else "",
        )
        monkeypatch.setattr(cache, "get_settings", lambda: settings)
        clients = await asyncio.gather(*(cache.get_redis() for _ in range(8)))
        assert all(client is clients[0] for client in clients)
        client = clients[0]
        assert await client.setex("export-token", 60, b"original-project") is True
        assert await client.get("export-token") == expected
        assert data.tokens == {(9, b"export-token"): b"original-project"}
        assert not any(b"data-secret" in command or b"data-user" in command for command in discovery.commands)
        assert not any(b"sentinel-secret" in command for command in data.commands)
        await cache.close_redis()
        await data.expect_closed()
        await discovery.expect_closed()


@pytest.mark.asyncio
async def test_lost_write_response_is_not_replayed_and_same_client_reconnects(isolated_cache, monkeypatch):
    data = _RedisWireEndpoint()
    discovery = _RedisWireEndpoint()
    async with data.listen() as data_port, discovery.listen() as sentinel_port:
        discovery.master = data_port
        settings = _settings(
            redis_url="redis://unused.invalid/9?protocol=2&socket_timeout=0.1&retry_on_timeout=true",
            redis_sentinel_addresses=(("127.0.0.1", sentinel_port),),
            redis_sentinel_master="kolla",
        )
        monkeypatch.setattr(cache, "get_settings", lambda: settings)
        client = await cache.get_redis()
        data.drop_write_response = True
        with pytest.raises(ConnectionError):
            await client.setex("export-token", 60, b"original-project")
        data.drop_write_response = False
        assert await cache.get_redis() is client
        assert await client.get("export-token") == b"original-project"
        assert sum(command[0] == b"SETEX" for command in data.commands) == 1
        await cache.close_redis()
        await data.expect_closed()
        await discovery.expect_closed()


@pytest.mark.asyncio
async def test_standalone_url_still_uses_its_credentials_database_and_bytes(isolated_cache, monkeypatch):
    data = _RedisWireEndpoint(credentials=(b"data-secret",))
    async with data.listen() as data_port:
        settings = _settings(redis_url=f"redis://:data-secret@127.0.0.1:{data_port}/9?protocol=2")
        monkeypatch.setattr(cache, "get_settings", lambda: settings)
        client = await cache.get_redis()
        await client.setex("export-token", 60, b"original-project")
        assert await client.get("export-token") == b"original-project"
        assert data.tokens == {(9, b"export-token"): b"original-project"}
        await cache.close_redis()
        await data.expect_closed()


@pytest.mark.asyncio
async def test_demoted_master_error_is_not_replayed_and_next_operation_rediscovers(isolated_cache, monkeypatch):
    old = _RedisWireEndpoint()
    promoted = _RedisWireEndpoint()
    unavailable = _RedisWireEndpoint()
    discovery = _RedisWireEndpoint()
    async with (
        old.listen() as old_port,
        promoted.listen() as promoted_port,
        unavailable.listen() as unavailable_port,
        discovery.listen() as sentinel_port,
    ):
        discovery.master = old_port
        settings = _settings(
            redis_url="redis://unused.invalid/9?socket_timeout=0.1",
            redis_sentinel_addresses=(("127.0.0.1", unavailable_port), ("127.0.0.1", sentinel_port)),
            redis_sentinel_master="kolla",
        )
        monkeypatch.setattr(cache, "get_settings", lambda: settings)
        client = await cache.get_redis()
        await client.setex("original-token", 60, b"original-project")
        # Synthetic replicated state and an explicit READONLY boundary exercise
        # redis-py reconnection, not real Sentinel election/Redis replication.
        promoted.tokens.update(old.tokens)
        old.read_only = True
        discovery.master = promoted_port
        with pytest.raises(ConnectionError):
            await client.setex("interrupted-token", 60, b"must-not-replay")
        assert await cache.get_redis() is client
        assert await client.get("original-token") == b"original-project"
        assert await client.get("interrupted-token") is None
        await client.setex("new-token", 60, b"original-project")
        assert await client.get("new-token") == b"original-project"
        assert (9, b"new-token") not in old.tokens
        await cache.close_redis()
        for endpoint in (old, promoted, unavailable, discovery):
            await endpoint.expect_closed()
