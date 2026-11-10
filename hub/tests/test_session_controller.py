from __future__ import annotations

import asyncio

from palimpsest_hub.services.session_controller import (
    ObservationError,
    Outcome,
    ProviderError,
    SessionController,
)
from palimpsest_hub.services.session_lifecycle import Phase

G = 300.0
IID = "i-1"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Observer:
    """Scripted observation results; a result that is an Exception is raised."""

    def __init__(self, *results):
        self.results = list(results)
        self.last = set()
        self.calls = 0

    async def sessions(self, instance_id):
        self.calls += 1
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            self.last = set(item)
        return set(self.last)


class Provider:
    def __init__(self, shelve_error=None, unshelve_error=None, on_shelve=None):
        self.calls = []
        self.shelve_error = shelve_error
        self.unshelve_error = unshelve_error
        self.on_shelve = on_shelve

    async def shelve(self, instance_id):
        self.calls.append("shelve")
        if self.on_shelve:
            await self.on_shelve()
        if self.shelve_error:
            raise self.shelve_error

    async def unshelve(self, instance_id):
        self.calls.append("unshelve")
        if self.unshelve_error:
            raise self.unshelve_error


def make(observer, provider=None):
    clock = Clock()
    provider = provider or Provider()
    controller = SessionController(observer, provider, clock, G)
    controller.register(IID)
    return controller, clock, provider


async def test_shelves_only_after_the_full_grace_period_with_no_session():
    controller, clock, provider = make(Observer([]))
    clock.now = G - 1
    assert await controller.tick(IID) is Outcome.NOOP
    clock.now = G
    assert await controller.tick(IID) is Outcome.SHELVED
    assert provider.calls == ["shelve"]
    assert controller.phase(IID) is Phase.SHELVED


async def test_observation_failure_never_shelves_even_after_the_grace_period():
    controller, clock, provider = make(Observer(ObservationError()))
    clock.now = G * 10
    assert await controller.tick(IID) is Outcome.OBSERVATION_FAILED
    assert provider.calls == []
    assert controller.phase(IID) is Phase.GRACE


async def test_a_session_seen_at_the_deadline_cancels_the_shelve():
    controller, clock, provider = make(Observer({"ssh-1"}))
    clock.now = G * 2
    assert await controller.tick(IID) is Outcome.NOOP
    assert provider.calls == []
    assert controller.phase(IID) is Phase.ACTIVE


async def test_second_observation_blocks_a_shelve_when_a_user_reconnected_after_the_claim():
    controller, clock, provider = make(Observer([], {"ssh-1"}))
    clock.now = G
    assert await controller.tick(IID) is Outcome.SHELVE_CANCELLED
    assert provider.calls == []
    assert controller.phase(IID) is Phase.ACTIVE


async def test_unreadable_second_observation_blocks_the_shelve_and_restarts_grace():
    controller, clock, provider = make(Observer([], ObservationError()))
    clock.now = G
    assert await controller.tick(IID) is Outcome.SHELVE_CANCELLED
    assert provider.calls == []
    assert controller.phase(IID) is Phase.GRACE
    clock.now = G + 1
    assert await controller.tick(IID) is Outcome.NOOP  # full grace restarts, no tight retry


async def test_failed_shelve_keeps_the_instance_and_does_not_retry_immediately():
    controller, clock, provider = make(Observer([]), Provider(shelve_error=ProviderError()))
    clock.now = G
    assert await controller.tick(IID) is Outcome.SHELVE_FAILED
    assert controller.phase(IID) is Phase.GRACE
    clock.now = G + 10
    assert await controller.tick(IID) is Outcome.NOOP
    assert provider.calls == ["shelve"]
    clock.now = G * 2
    assert await controller.tick(IID) is Outcome.SHELVE_FAILED
    assert provider.calls == ["shelve", "shelve"]


async def test_user_arriving_while_shelved_triggers_exactly_one_unshelve():
    controller, clock, provider = make(Observer([], [], {"ssh-1"}, {"ssh-1"}))
    clock.now = G
    assert await controller.tick(IID) is Outcome.SHELVED
    assert await controller.tick(IID) is Outcome.UNSHELVED
    assert await controller.tick(IID) is Outcome.NOOP
    assert provider.calls == ["shelve", "unshelve"]
    assert controller.phase(IID) is Phase.ACTIVE


async def test_failed_unshelve_is_retried_on_the_next_tick():
    provider = Provider(unshelve_error=ProviderError())
    controller, clock, provider = make(Observer([], [], {"ssh-1"}, {"ssh-1"}), provider)
    clock.now = G
    await controller.tick(IID)
    assert await controller.tick(IID) is Outcome.UNSHELVE_FAILED
    assert controller.phase(IID) is Phase.SHELVED
    provider.unshelve_error = None
    assert await controller.tick(IID) is Outcome.UNSHELVED


async def test_second_session_keeps_the_instance_awake_until_the_last_one_leaves():
    controller, clock, provider = make(Observer({"a", "b"}, {"b"}, set()))
    clock.now = 10
    await controller.tick(IID)
    clock.now = G + 100
    assert await controller.tick(IID) is Outcome.NOOP  # "a" gone, "b" still open
    assert provider.calls == []
    clock.now = G + 110  # last session leaves: grace restarts here
    assert await controller.tick(IID) is Outcome.NOOP
    clock.now = G + 110 + G
    assert await controller.tick(IID) is Outcome.SHELVED


async def test_concurrent_ticks_shelve_only_once():
    gate = asyncio.Event()

    async def slow():
        await gate.wait()

    controller, clock, provider = make(Observer([]), Provider(on_shelve=slow))
    clock.now = G
    first = asyncio.create_task(controller.tick(IID))
    await asyncio.sleep(0)
    second = asyncio.create_task(controller.tick(IID))
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(first, second)
    assert provider.calls == ["shelve"]
    assert results[0] is Outcome.SHELVED
    assert results[1] is Outcome.NOOP
