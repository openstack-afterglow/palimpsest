"""Drive `SessionMachine` from observed guest sessions and a provider shelve/unshelve port.

The controller owns no clock, network or persistence: callers inject an observer, a
provider, and a monotonic clock. One instance is processed at a time (per-instance lock),
and every provider call happens only after a fresh observation, so a user who reconnected
after the grace period elapsed is never shelved.

The per-instance lock and the claim are process-local: with more than one Hub worker or
replica two controllers could each win a claim and both call shelve. Before this runs in
more than one process, serialise through `sql_mutex` or the instance row lock (as
`api/builds.py` does), not `asyncio.Lock`.
State is in memory. After a process restart an instance gets a new machine whose grace
period starts over, which can only delay a shelve, never cause an early one.

Rules enforced here (the machine decides timing, this layer decides safety):
* an observation failure is unknown, never a disconnect: no timer movement, no shelve;
* a claim is re-checked with a second observation before the provider is called;
* a failed shelve keeps the instance and restarts the full grace period;
* a user present while the instance is shelved triggers exactly one unshelve.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from palimpsest_hub.services.session_lifecycle import GRACE_SECONDS, Phase, SessionMachine


class ObservationError(Exception):
    """The guest session list could not be read; the result is unknown."""


class ProviderError(Exception):
    """The provider refused or could not confirm a shelve or unshelve."""


class SessionObserver(Protocol):
    async def sessions(self, instance_id: str) -> set[str]: ...


class ShelveProvider(Protocol):
    async def shelve(self, instance_id: str) -> None: ...

    async def unshelve(self, instance_id: str) -> None: ...


class Outcome(StrEnum):
    NOOP = "noop"
    OBSERVATION_FAILED = "observation-failed"
    SHELVED = "shelved"
    SHELVE_FAILED = "shelve-failed"
    SHELVE_CANCELLED = "shelve-cancelled"
    UNSHELVED = "unshelved"
    UNSHELVE_FAILED = "unshelve-failed"


class SessionController:
    def __init__(
        self,
        observer: SessionObserver,
        provider: ShelveProvider,
        clock: Callable[[], float],
        grace_seconds: float = GRACE_SECONDS,
        checkpoint: Callable[[str, SessionMachine], Awaitable[None]] | None = None,
    ) -> None:
        self._observer = observer
        self._provider = provider
        self._clock = clock
        self._grace = grace_seconds
        # Persist the machine right after a shelve claim, before the provider is called, so a
        # crash during the provider call leaves a durable SHELVING phase to recover from.
        self._checkpoint = checkpoint
        self._machines: dict[str, SessionMachine] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def adopt(self, instance_id: str, machine: SessionMachine) -> None:
        """Track an instance with a machine restored from durable state."""
        self._machines[instance_id] = machine
        self._locks.setdefault(instance_id, asyncio.Lock())

    def machine(self, instance_id: str) -> SessionMachine:
        return self._machines[instance_id]

    def register(self, instance_id: str) -> SessionMachine:
        """Start tracking a new instance; its grace period starts now."""
        machine = SessionMachine.created(self._clock(), self._grace)
        self._machines[instance_id] = machine
        self._locks.setdefault(instance_id, asyncio.Lock())
        return machine

    def forget(self, instance_id: str) -> None:
        self._machines.pop(instance_id, None)
        self._locks.pop(instance_id, None)

    def phase(self, instance_id: str) -> Phase:
        return self._machines[instance_id].phase

    def _sync(self, machine: SessionMachine, observed: set[str], now: float) -> None:
        for gone in sorted(machine.sessions - observed):
            machine.disconnect(gone, now)
        for new in sorted(observed - machine.sessions):
            machine.connect(new, now)

    async def tick(self, instance_id: str) -> Outcome:
        machine = self._machines[instance_id]
        async with self._locks[instance_id]:
            now = self._clock()
            try:
                observed = await self._observer.sessions(instance_id)
            except ObservationError:
                machine.observation_failed(now)
                return Outcome.OBSERVATION_FAILED
            self._sync(machine, observed, now)
            if machine.phase is Phase.SHELVED:
                return await self._unshelve_if_user_present(instance_id, machine)
            claim = machine.claim_shelve(now)
            if claim is None:
                return Outcome.NOOP
            if self._checkpoint is not None:
                await self._checkpoint(instance_id, machine)
            return await self._shelve(instance_id, machine, claim)

    async def _recheck(self, instance_id: str, machine: SessionMachine) -> bool:
        """True when it is still safe to shelve after a second, fresh observation."""
        try:
            observed = await self._observer.sessions(instance_id)
        except ObservationError:
            machine.observation_failed(self._clock())
            return False
        self._sync(machine, observed, self._clock())
        return not machine.sessions

    async def _shelve(self, instance_id: str, machine: SessionMachine, claim) -> Outcome:
        if not await self._recheck(instance_id, machine) or not machine.shelve_still_valid(claim):
            machine.shelve_settled(claim, self._clock(), ok=False)
            return Outcome.SHELVE_CANCELLED
        try:
            await self._provider.shelve(instance_id)
        except ProviderError:
            machine.shelve_settled(claim, self._clock(), ok=False)
            return Outcome.SHELVE_FAILED
        machine.shelve_settled(claim, self._clock(), ok=True)
        return Outcome.SHELVED

    async def _unshelve_if_user_present(self, instance_id: str, machine: SessionMachine) -> Outcome:
        if not machine.sessions:
            return Outcome.NOOP
        try:
            await self._provider.unshelve(instance_id)
        except ProviderError:
            return Outcome.UNSHELVE_FAILED
        machine.unshelved()
        return Outcome.UNSHELVED


__all__ = [
    "ObservationError",
    "Outcome",
    "ProviderError",
    "SessionController",
    "SessionObserver",
    "ShelveProvider",
]
