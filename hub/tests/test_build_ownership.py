"""API-side ownership with real disposable SQL and disjoint local CAS locks.

Portable SQLite cases define the contract; the opt-in MariaDB/process smoke in
 test_database_pool exercises actual cross-process SQL exclusion. No recipe or
 cloud resource is executed by these tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from asyncmy.errors import OperationalError as AsyncmyOperationalError
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from palimpsest_hub.models import Base, PalimpsestHubBuild, PalimpsestHubLayer
from palimpsest_hub.services import builds, worker_leases
from palimpsest_hub.services.hub_store import LocalPathBlobStore
from palimpsest_hub.services.worker_leases import MutexBusy, role_lease, sql_mutex


def digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.fixture
async def ownership(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    stores = [LocalPathBlobStore(tmp_path / f"api-{index}") for index in range(3)]
    selected = ContextVar("api_store", default=stores[0])
    base = b"raw visible ownership input"
    base_digest = digest(base)
    for store in stores:
        store.blobs_dir.mkdir(parents=True, exist_ok=True)
        store.blob_path(base_digest).write_bytes(base)
    async with factory() as session, session.begin():
        session.add(
            PalimpsestHubLayer(
                blob_digest=base_digest,
                size_bytes=len(base),
                media_type="application/octet-stream",
                disk_format="raw",
                arch="x86_64",
                config_digest=base_digest,
                name="base",
                kind="cloud-image",
                config_json={},
                project_id=None,
                is_published=True,
            )
        )
    settings = SimpleNamespace(palimpsest_hub_build_timeout_seconds=60, palimpsest_hub_max_blob_bytes=1024)
    monkeypatch.setattr(builds, "_factory_or_503", lambda: factory)
    monkeypatch.setattr(builds, "_store_or_503", selected.get)
    monkeypatch.setattr(builds, "get_settings", lambda: settings)

    async def enqueue(project="alpha"):
        job_id = str(uuid4())
        recipe = f"FROM {base_digest}\nRUN private-command"
        async with factory() as session, session.begin():
            session.add(
                PalimpsestHubBuild(
                    id=job_id,
                    project_id=project,
                    created_by="requester",
                    name="private-output",
                    recipe=recipe,
                    recipe_digest=digest(recipe.encode()),
                    base_digest=base_digest,
                    layer_digests=[],
                    status="queued",
                    created_at=datetime.now(UTC),
                )
            )
        return job_id

    try:
        yield SimpleNamespace(factory=factory, stores=stores, selected=selected, settings=settings, enqueue=enqueue)
    finally:
        await engine.dispose()


async def submit(job, payload):
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    request = Request({"type": "http", "method": "PUT", "path": "/output", "headers": []}, receive)
    return await builds.upload_output(job["id"], job["lease"], request, digest(payload), len(payload))


@pytest.mark.asyncio
async def test_api_single_flight_uses_sql_not_shared_cas_or_worker_identity(ownership):
    h = ownership
    ids = [await h.enqueue(project) for project in ("alpha", "beta", "gamma")]

    async def claim_from(store):
        token = h.selected.set(store)
        try:
            return await builds.claim_build(str(uuid4()), 1)
        finally:
            h.selected.reset(token)

    claims = await asyncio.gather(*(claim_from(store) for store in h.stores))
    assert sum(job is not None for job in claims) == 1
    first = next(job for job in claims if job is not None)
    assert first["id"] == ids[0]
    async with h.factory() as session:
        rows = (await session.scalars(select(PalimpsestHubBuild))).all()
        assert sum(row.status == "building" for row in rows) == 1
        assert sum(row.status == "queued" for row in rows) == 2
    # Explicit failure releases single-flight, but retains an immutable receipt.
    failed = await builds.fail_build(first["id"], first["lease"], "cleanup_failed")
    second = await claim_from(h.stores[1])
    assert failed.status == "error" and second["id"] == ids[1]
    assert second["lease"] != first["lease"]
    assert (await builds.worker_status(first["id"], first["lease"])).error_code == "cleanup_failed"
    with pytest.raises(HTTPException) as error:
        await submit(first, b"hsqsstale-result")
    assert error.value.status_code == 409
    assert all(not store.exists(digest(b"hsqsstale-result")) for store in h.stores)


@pytest.mark.asyncio
async def test_persisted_deadline_survives_api_timeout_changes_without_replay(ownership):
    h = ownership
    job_id = await h.enqueue()
    claim = await builds.claim_build(str(uuid4()), 1)
    async with h.factory() as session:
        original = await session.get(PalimpsestHubBuild, job_id)
        deadline = original.lease_expires_at.replace(tzinfo=UTC)
    h.settings.palimpsest_hub_build_timeout_seconds = 3600
    status = await builds.worker_status(job_id, claim["lease"])
    assert builds._deadline(status) == deadline
    assert status.lease_owner == claim["lease"]
    # API expiry never authorizes private-tree cleanup or a second recipe attempt.
    async with h.factory() as session, session.begin():
        row = await session.get(PalimpsestHubBuild, job_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await builds.claim_build(str(uuid4()), 1) is None
    status = await builds.worker_status(job_id, claim["lease"])
    assert status.status == "error" and status.error_code == "worker_interrupted"
    assert status.lease_owner == claim["lease"] and status.output_digest is None
    assert await builds.claim_build(str(uuid4()), 1) is None


@pytest.mark.asyncio
async def test_publication_is_private_and_old_nonce_has_no_effect(ownership):
    h = ownership
    await h.enqueue("beta")
    claim = await builds.claim_build(str(uuid4()), 1)
    stale = {**claim, "lease": str(uuid4())}
    payload = b"hsqsprivate-publication"
    with pytest.raises(HTTPException) as error:
        await submit(stale, payload)
    assert error.value.status_code == 409
    assert not h.stores[0].exists(digest(payload))
    completed = await submit(claim, payload)
    async with h.factory() as session:
        layer = await session.scalar(
            select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest(payload))
        )
        assert layer.project_id == "beta" and layer.created_by == "requester" and not layer.is_published
    assert completed.status == "complete" and completed.lease_owner == claim["lease"]
    repeated = await submit(claim, payload)
    assert repeated.output_digest == completed.output_digest
    assert (await builds.fail_build(claim["id"], claim["lease"], "build_failed")).status == "complete"


@pytest.mark.asyncio
async def test_cancelled_publication_drains_before_disjoint_api_can_claim(ownership, monkeypatch):
    h = ownership
    await h.enqueue()
    claim = await builds.claim_build(str(uuid4()), 1)
    started, release, drained = threading.Event(), threading.Event(), threading.Event()
    original = h.stores[0].publish_verified

    def publish(*args):
        started.set()
        try:
            assert release.wait(5)
            return original(*args)
        finally:
            drained.set()

    monkeypatch.setattr(h.stores[0], "publish_verified", publish)
    publishing = asyncio.create_task(submit(claim, b"hsqscancelled-publication"))
    peer = None

    async def claim_from_peer():
        token = h.selected.set(h.stores[1])
        try:
            return await builds.claim_build(str(uuid4()), 1)
        finally:
            h.selected.reset(token)

    try:
        assert await asyncio.to_thread(started.wait, 3)
        publishing.cancel()
        publishing.cancel()
        peer = asyncio.create_task(claim_from_peer())
        await asyncio.sleep(0.05)
        assert not publishing.done() and not peer.done() and not drained.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await publishing
        assert drained.is_set() and await peer is None
        status = await builds.worker_status(claim["id"], claim["lease"])
        assert status.status == "building" and status.output_digest is None
    finally:
        release.set()
        await asyncio.gather(publishing, *([peer] if peer is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_api_mutex_namespace_cannot_be_adopted_as_a_worker_role(ownership):
    with pytest.raises(ValueError, match="not renewable"):
        async with role_lease(ownership.factory, "build-api:single-flight"):
            pytest.fail("worker role adopted API mutex")
    with pytest.raises(ValueError, match="build-api namespace"):
        async with sql_mutex(ownership.factory, "export-maintenance"):
            pytest.fail("API mutex adopted export role")


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_seconds", [None, 1], ids=["default", "bounded"])
@pytest.mark.parametrize(
    ("original", "busy"),
    [
        (AsyncmyOperationalError(1205, "Lock wait timeout exceeded"), True),
        (OSError(1205, "driver errno attribute"), True),
        (sqlite3.OperationalError("database is locked"), True),
        (AsyncmyOperationalError(2013, "Lost connection"), False),
        (AsyncmyOperationalError(1213, "Deadlock found"), False),
        (sqlite3.OperationalError("no such table"), False),
        (sqlite3.OperationalError("database schema is locked"), False),
    ],
    ids=[
        "mysql-timeout",
        "errno-attribute",
        "sqlite-locked",
        "connection-lost",
        "deadlock",
        "bad-sql",
        "schema-locked",
    ],
)
async def test_api_mutex_maps_only_lock_wait_timeouts_and_rolls_back_before_raising(
    ownership, monkeypatch, original, busy, wait_seconds
):
    name = "build-api:timeout"
    async with sql_mutex(ownership.factory, name) as session:
        await session.commit()
    failure = OperationalError("UPDATE worker_leases", {}, original)
    session_type = ownership.factory.class_
    execute, rollback = session_type.execute, session_type.rollback
    attempted, rolled_back = [], []

    async def fail_update(session, statement, *args, **kwargs):
        if getattr(statement, "is_update", False):
            attempted.append(session)
            raise failure
        return await execute(session, statement, *args, **kwargs)

    async def record_rollback(session):
        await rollback(session)
        assert not session.in_transaction()
        rolled_back.append(session)

    monkeypatch.setattr(session_type, "execute", fail_update)
    monkeypatch.setattr(session_type, "rollback", record_rollback)
    expected = MutexBusy if busy else OperationalError
    with pytest.raises(expected) as raised:
        async with sql_mutex(ownership.factory, name, wait_seconds=wait_seconds):
            pytest.fail("a failed mutex UPDATE yielded its session")
    assert len(attempted) == 1 and rolled_back == attempted
    if not busy:
        assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect", "is_mariadb"),
    [("mysql", True), ("mariadb", True), ("mysql", False), ("sqlite", False), ("postgresql", False)],
)
@pytest.mark.parametrize(
    ("wait_seconds", "setup_seconds", "remaining_wait"),
    [(None, 0.1, None), (0, 0.1, 0), (1, 0.1, 1), (2, 0.1, 2), (3, 0.1, 3), (2, 1.1, 1)],
    ids=["default", "zero", "bounded-1", "bounded-2", "bounded-3", "setup-consumes-one-second"],
)
@pytest.mark.parametrize("existing", [True, False], ids=["existing-row", "first-row"])
async def test_api_mutex_uses_only_mariadb_remaining_wait_without_session_changes(
    monkeypatch, dialect, is_mariadb, wait_seconds, setup_seconds, remaining_wait, existing
):
    statements, initialized, transactions = [], [], []
    connection = SimpleNamespace(dialect=SimpleNamespace(name=dialect, is_mariadb=is_mariadb))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args):
            return object() if existing else None

        def begin(self):
            transactions.append(self)
            return self

        def add(self, row):
            initialized.append(row)

        async def connection(self):
            return connection

        async def execute(self, statement, parameters=None):
            statements.append((statement, parameters))

    # The SQL WAIT must use the remaining budget, rounded up rather than truncated.
    clock = iter([100.0, 100.0 + setup_seconds])
    monkeypatch.setattr(worker_leases, "monotonic", lambda: next(clock))
    async with sql_mutex(Session, "build-api:row-lock", wait_seconds=wait_seconds):
        pass
    lock_first = wait_seconds is not None and is_mariadb
    assert len(transactions) == int(not existing)
    assert len(initialized) == int(not existing)
    assert len(statements) == 1 + int(lock_first)
    if lock_first:
        statement, parameters = statements[0]
        assert str(statement) == (
            "SELECT name FROM palimpsest_worker_leases WHERE name = :name FOR UPDATE WAIT " + str(remaining_wait)
        )
        assert parameters == {"name": "build-api:row-lock"}
    statement, parameters = statements[-1]
    assert statement.is_update and parameters is None
    assert all(not str(statement).lstrip().upper().startswith(("SET ", "PRAGMA ")) for statement, _ in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_mariadb", "wait_seconds", "code", "expected"),
    [
        (True, 2, 1205, MutexBusy),
        (True, None, 1205, MutexBusy),
        (False, 0, 1205, MutexBusy),
        (False, 2, 1205, MutexBusy),
        (False, None, 1205, MutexBusy),
        (False, 2, 2013, OperationalError),
    ],
)
@pytest.mark.parametrize("failure_at", ["lookup", "insert", "lock"])
async def test_api_mutex_timeout_and_mysql_fallback_roll_back_including_initialization(
    monkeypatch, is_mariadb, wait_seconds, code, expected, failure_at
):
    failure = OperationalError("mutex lock", {}, AsyncmyOperationalError(code, "driver failure"))
    rolled_back = []

    class Session:
        initializing = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, *args):
            if self.initializing and exc_type is None:
                raise failure

        async def get(self, *args):
            if failure_at == "lookup":
                raise failure
            return None if failure_at == "insert" else object()

        def begin(self):
            return self

        def add(self, row):
            self.initializing = True

        async def connection(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="mysql", is_mariadb=is_mariadb))

        async def execute(self, statement, parameters=None):
            raise failure

        async def rollback(self):
            rolled_back.append(self)

    monkeypatch.setattr(worker_leases, "monotonic", lambda: 100.0)
    with pytest.raises(expected) as raised:
        async with sql_mutex(Session, "build-api:row-lock", wait_seconds=wait_seconds):
            pytest.fail("failed row lock yielded its session")
    assert len(rolled_back) == 1
    if expected is OperationalError:
        assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [True, False], ids=["existing-row", "first-row"])
async def test_api_mutex_sqlite_uses_fixture_timeout_and_keeps_the_session_clean(tmp_path, monkeypatch, existing):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'bounded-mutex.sqlite'}",
        connect_args={"timeout": 0.05},
        pool_size=2,
        max_overflow=0,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    name = "build-api:bounded-sqlite"
    rollbacks = []
    original_rollback = factory.class_.rollback

    async def record_rollback(session):
        await original_rollback(session)
        assert not session.in_transaction()
        rollbacks.append(session)

    async def contend():
        with pytest.raises(MutexBusy):
            async with sql_mutex(factory, name, wait_seconds=1):
                pytest.fail("held SQLite writer fence was acquired")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        if existing:
            async with sql_mutex(factory, name, wait_seconds=0) as session:
                await session.commit()
        monkeypatch.setattr(factory.class_, "rollback", record_rollback)
        async with sql_mutex(factory, "build-api:holder") as holder:
            started = monotonic()
            await asyncio.wait_for(contend(), timeout=2.5)
            elapsed = monotonic() - started
            assert elapsed < 2.5, elapsed
            assert holder.in_transaction()
            # A timed-out contender must not roll back or release the holder.
            assert await holder.scalar(text("SELECT 1")) == 1
            assert len(rollbacks) == 1
            await holder.commit()
        async with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                assert (await connection.exec_driver_sql("SELECT 1")).scalar_one() == 1
        async with sql_mutex(factory, name, wait_seconds=1) as session:
            await session.commit()
        async with factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_on_disjoint_api_waits_for_original_claim_sql_commit(ownership, monkeypatch):
    h = ownership
    job_id = await h.enqueue()
    entered, release = asyncio.Event(), asyncio.Event()
    original = builds._inputs

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(builds, "_inputs", delayed)
    channel = str(uuid4())
    claiming = asyncio.create_task(builds.claim_build(channel, 1))

    async def recover_on_peer():
        token = h.selected.set(h.stores[1])
        try:
            return await builds.claim_build(channel, 1, recover=True)
        finally:
            h.selected.reset(token)

    recovery = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        recovery = asyncio.create_task(recover_on_peer())
        await asyncio.sleep(0.05)
        assert not recovery.done()
        release.set()
        accepted, recovered = await asyncio.gather(claiming, recovery)
        assert accepted["id"] == job_id
        assert recovered == {
            "channel": channel,
            "sequence": 1,
            "protocol": "recipe",
            "claim": {"id": job_id, "lease": accepted["lease"]},
        }
    finally:
        release.set()
        await asyncio.gather(claiming, *([recovery] if recovery else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_claim_transaction_cannot_leave_receipt_or_unknown_attempt(ownership, monkeypatch):
    h = ownership
    job_id = await h.enqueue()
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr(builds, "_inputs", paused)
    channel = str(uuid4())
    claiming = asyncio.create_task(builds.claim_build(channel, 1))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        claiming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await claiming
        recovered = await builds.claim_build(channel, 1, recover=True)
        assert recovered["claim"] is None
        assert await builds.claim_build(channel, 1) is None
        async with h.factory() as session:
            job = await session.get(PalimpsestHubBuild, job_id)
            assert job.status == "queued" and job.lease_owner is None
    finally:
        release.set()
        await asyncio.gather(claiming, return_exceptions=True)
