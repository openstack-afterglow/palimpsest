from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from palimpsest_hub.services.nova_shelve_provider import SHELVED_OFFLOADED, NovaShelveProvider
from palimpsest_hub.services.session_controller import ProviderError

SID = "server-1"


class FakeCompute:
    """Scripted Nova: `statuses` is consumed by get_server; the last value repeats."""

    def __init__(self, *statuses, shelve_error=None, unshelve_error=None):
        self.statuses = list(statuses)
        self.shelve_error = shelve_error
        self.unshelve_error = unshelve_error
        self.calls: list[str] = []
        self.threads: list[threading.Thread] = []

    def _note(self, name):
        self.calls.append(name)
        self.threads.append(threading.current_thread())

    def get_server(self, server_id):
        self._note("get")
        status = self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)
        return SimpleNamespace(id=server_id, status=status)

    def shelve_server(self, server_id):
        self._note("shelve")
        if self.shelve_error:
            raise self.shelve_error

    def unshelve_server(self, server_id):
        self._note("unshelve")
        if self.unshelve_error:
            raise self.unshelve_error


def provider(compute, *, timeout=10.0):
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds

    return NovaShelveProvider(
        SimpleNamespace(compute=compute),
        timeout_seconds=timeout,
        poll_seconds=1.0,
        clock=lambda: now[0],
        sleep=sleep,
    )


async def test_an_active_server_is_shelved_and_confirmed_only_when_offloaded():
    compute = FakeCompute("ACTIVE", "SHELVING", SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get", "shelve", "get", "get"]


async def test_an_already_offloaded_server_is_success_without_a_second_shelve():
    compute = FakeCompute(SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get"]


async def test_a_shelve_already_in_progress_is_awaited_not_repeated():
    compute = FakeCompute("SHELVING", SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert "shelve" not in compute.calls


async def test_shelve_of_a_server_in_another_state_is_refused_without_calling_nova():
    compute = FakeCompute("ERROR")
    with pytest.raises(ProviderError, match="not shelvable"):
        await provider(compute).shelve(SID)
    assert compute.calls == ["get"]


async def test_shelve_that_never_reaches_offloaded_times_out_as_a_provider_error():
    compute = FakeCompute("ACTIVE", "SHELVING")
    with pytest.raises(ProviderError, match="timed out"):
        await provider(compute, timeout=3.0).shelve(SID)


async def test_sdk_failure_is_mapped_to_a_provider_error_without_leaking_details():
    compute = FakeCompute("ACTIVE", shelve_error=RuntimeError("token=secret"))
    with pytest.raises(ProviderError) as info:
        await provider(compute).shelve(SID)
    assert "secret" not in str(info.value)
    assert "RuntimeError" in str(info.value)


async def test_unshelve_of_an_offloaded_server_waits_for_active():
    compute = FakeCompute(SHELVED_OFFLOADED, "UNSHELVING", "ACTIVE")
    await provider(compute).unshelve(SID)
    assert compute.calls.count("unshelve") == 1


async def test_unshelve_of_an_active_server_is_success_without_a_call():
    compute = FakeCompute("ACTIVE")
    await provider(compute).unshelve(SID)
    assert compute.calls == ["get"]


async def test_unshelve_of_a_building_server_is_refused():
    compute = FakeCompute("BUILD")
    with pytest.raises(ProviderError, match="not unshelvable"):
        await provider(compute).unshelve(SID)
    assert compute.calls == ["get"]


async def test_a_status_missing_from_the_response_is_refused():
    compute = FakeCompute(None)
    with pytest.raises(ProviderError, match="no server status"):
        await provider(compute).shelve(SID)


async def test_sdk_calls_run_off_the_event_loop_thread():
    compute = FakeCompute("ACTIVE", SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.threads
    assert all(t is not threading.main_thread() for t in compute.threads)


def test_nonpositive_timeouts_are_rejected():
    with pytest.raises(ValueError):
        NovaShelveProvider(SimpleNamespace(compute=None), timeout_seconds=0)


@pytest.mark.asyncio
async def test_server_status_reports_the_uppercase_nova_status_without_effects():
    compute = FakeCompute("shelved_offloaded")
    provider = NovaShelveProvider(SimpleNamespace(compute=compute))
    assert await provider.server_status(SID) == SHELVED_OFFLOADED
    assert compute.calls == ["get"]
