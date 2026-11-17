"""SQL-fenced export roles and API transaction mutexes, never remote-worker credentials.

A role fence holds its SQL row until non-token-aware effects have drained. The
heartbeat pauses behind that fence; its expiry is refreshed before unlocking.
No other process can take over an expired row while the fence holds its lock.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from math import ceil
from time import monotonic

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from palimpsest_hub.models import PalimpsestWorkerLease


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class LeaseLost(RuntimeError):
    """The caller must stop starting effects after losing ownership."""


class MutexBusy(RuntimeError):
    """The caller's API mutex acquisition could not obtain the SQL fence."""


def _mutex_timeout(exc: OperationalError) -> bool:
    original = exc.orig
    code = getattr(original, "errno", None)
    if code is None:
        code = next(iter(getattr(original, "args", ())), None)
    return code == 1205 or (isinstance(original, sqlite3.OperationalError) and str(original) == "database is locked")


async def run_blocking(fn, *args, **kwargs):
    """Drain an uncancellable thread before the enclosing SQL fence can exit."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with suppress(BaseException):
                await asyncio.shield(task)
        # Retrieve any exception without replacing the caller's cancellation.
        with suppress(BaseException):
            task.result()
        raise


class RenewableLease:
    def __init__(self, renew: Callable[[], Awaitable[bool]], ttl_seconds: float):
        self.lost = asyncio.Event()
        self._renew = renew
        self._interval = ttl_seconds / 3

    def ensure_owned(self) -> None:
        if self.lost.is_set():
            raise LeaseLost("Native worker lease lost")

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                if not await self._renew():
                    self.lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self.lost.set()


class RoleLease(RenewableLease):
    def __init__(self, factory, name: str, token: str, ttl_seconds: float):
        self.factory = factory
        self.name = name
        self.token = token
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = asyncio.Lock()
        super().__init__(self.renew, ttl_seconds)

    async def renew(self) -> bool:
        async with self._lock, self.factory() as session, session.begin():
            row = (
                await session.execute(
                    select(PalimpsestWorkerLease).where(PalimpsestWorkerLease.name == self.name).with_for_update()
                )
            ).scalar_one_or_none()
            now = _now()  # Check after waiting for the database lock, not before.
            if row is None or row.token != self.token or _aware(row.expires_at) <= now:
                self.lost.set()
                return False
            row.expires_at = now + self._ttl
            return True

    @asynccontextmanager
    async def fence(self):
        """Lock and verify the role; drain blocking effects inside this scope.

        The same transaction may lock job/attempt rows before starting an effect.
        A lock retained continuously fences takeover even across the nominal TTL.
        """
        async with self._lock, self.factory() as session, session.begin():
            self.ensure_owned()
            row = (
                await session.execute(
                    select(PalimpsestWorkerLease).where(PalimpsestWorkerLease.name == self.name).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.token != self.token or _aware(row.expires_at) <= _now():
                self.lost.set()
                raise LeaseLost("Native worker role expired or changed")
            row.expires_at = _now() + self._ttl
            await session.flush()
            yield session
            row.expires_at = _now() + self._ttl

    async def release(self) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(
                delete(PalimpsestWorkerLease).where(
                    PalimpsestWorkerLease.name == self.name,
                    PalimpsestWorkerLease.token == self.token,
                    PalimpsestWorkerLease.expires_at > _now(),
                )
            )


async def _take_expired_role(factory, name: str, token: str, ttl: timedelta) -> bool:
    async with factory() as session, session.begin():
        row = (
            await session.execute(
                select(PalimpsestWorkerLease)
                .where(PalimpsestWorkerLease.name == name)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        now = _now()
        if row is None or _aware(row.expires_at) > now:
            return False
        row.token = token
        row.expires_at = now + ttl
        return True


@asynccontextmanager
async def role_lease(factory, name: str, *, ttl_seconds: float = 120):
    """Try a unique SQL role, yielding None if busy; renew until scope exit.

    Existing fenced rows are skipped, not waited on. INSERT handles first
    ownership across processes; a uniqueness loser tries expired-row takeover.
    Job selection must remain outside this helper (no global job scheduler).
    """
    if ttl_seconds <= 0:
        raise ValueError("Lease TTL must be positive")
    token = uuid.uuid4().hex
    if name.startswith("build-api:"):
        raise ValueError("API transaction mutexes are not renewable worker roles")
    ttl = timedelta(seconds=ttl_seconds)
    async with factory() as session:
        existing = await session.scalar(select(PalimpsestWorkerLease.name).where(PalimpsestWorkerLease.name == name))
    if existing is not None:
        acquired = await _take_expired_role(factory, name, token, ttl)
    else:
        try:
            async with factory() as session, session.begin():
                row = PalimpsestWorkerLease(name=name, token=token, expires_at=_now() + ttl)
                session.add(row)
                await session.flush()
                # A competing first INSERT may have delayed this uniqueness race.
                row.expires_at = _now() + ttl
            acquired = True
        except IntegrityError:
            acquired = await _take_expired_role(factory, name, token, ttl)
    if not acquired:
        yield None
        return
    lease = RoleLease(factory, name, token, ttl_seconds)
    task = asyncio.create_task(lease._heartbeat())
    try:
        yield lease
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await lease.release()


@asynccontextmanager
async def sql_mutex(factory, name: str, *, wait_seconds: int | None = None):
    """Serialize API transactions even with empty queues and disjoint CAS locks.

    Reserved ``build-api:`` rows are transaction mutexes, not renewable worker
    roles. Their timestamp is inert; the database write lock is the authority.
    Use the yielded session for every protected SQL mutation and commit only
    after all blocking effects have drained. SQLite's UPDATE takes its writer
    lock too, so it does not rely on unsupported FOR UPDATE semantics.
    MariaDB bounds the row lock with FOR UPDATE WAIT (remaining seconds rounded up).
    Other dialects retain the plain UPDATE and driver-configured lock timeout.
    Every recognized lock timeout raises MutexBusy after rollback, including
    first-row insertion, which remains unbounded. No pooled settings are changed.
    """
    if not name.startswith("build-api:"):
        raise ValueError("API mutex names must use the build-api namespace")
    if wait_seconds is not None and (not isinstance(wait_seconds, int) or wait_seconds < 0):
        raise ValueError("Mutex wait_seconds must be a nonnegative integer or None")
    deadline = None if wait_seconds is None else monotonic() + wait_seconds
    async with factory() as session:
        try:
            exists = await session.get(PalimpsestWorkerLease, name)
        except OperationalError as exc:
            await session.rollback()
            if _mutex_timeout(exc):
                raise MutexBusy("Resource is busy with another operation") from None
            raise
    if exists is None:
        async with factory() as session:
            try:
                async with session.begin():
                    session.add(PalimpsestWorkerLease(name=name, token=uuid.uuid4().hex, expires_at=_now()))
            except IntegrityError:
                pass  # Another API initialized the same mutex first.
            except OperationalError as exc:
                await session.rollback()
                if _mutex_timeout(exc):
                    raise MutexBusy("Resource is busy with another operation") from None
                raise
    async with factory() as session:
        try:
            if wait_seconds is not None:
                dialect = (await session.connection()).dialect
                if getattr(dialect, "is_mariadb", False):
                    seconds = max(0, ceil(deadline - monotonic()))
                    await session.execute(
                        text(f"SELECT name FROM palimpsest_worker_leases WHERE name = :name FOR UPDATE WAIT {seconds}"),
                        {"name": name},
                    )
            await session.execute(
                update(PalimpsestWorkerLease)
                .where(PalimpsestWorkerLease.name == name)
                .values(token=uuid.uuid4().hex, expires_at=_now())
            )
        except OperationalError as exc:
            # A provider operation may hold this fence longer than the database's lock wait.
            await session.rollback()
            if _mutex_timeout(exc):
                raise MutexBusy("Resource is busy with another operation") from None
            raise
        yield session
