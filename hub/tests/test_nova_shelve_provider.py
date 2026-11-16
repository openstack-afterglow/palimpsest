from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from palimpsest_hub.services.nova_shelve_provider import SHELVED_OFFLOADED, NovaShelveProvider, ServerState
from palimpsest_hub.services.session_controller import ProviderError

SID = "server-1"
SHELVE_TASKS = ("shelving", "shelving_image_pending_upload", "shelving_image_uploading", "shelving_offloading")
UNSHELVE_TASKS = ("unshelving", "spawning")


class FakeCompute:
    """Scripted Nova: states are status strings, (status, task_state) pairs or mappings."""

    def __init__(self, *states, shelve_error=None, unshelve_error=None):
        self.states = list(states)
        self.shelve_error = shelve_error
        self.unshelve_error = unshelve_error
        self.calls: list[str] = []
        self.threads: list[threading.Thread] = []

    def _note(self, name):
        self.calls.append(name)
        self.threads.append(threading.current_thread())

    def get_server(self, server_id):
        self._note("get")
        state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        if isinstance(state, dict):
            return state
        status, task_state = state if isinstance(state, tuple) else (state, None)
        return SimpleNamespace(id=server_id, status=status, task_state=task_state)

    def shelve_server(self, server_id):
        self._note("shelve")
        if self.shelve_error:
            raise self.shelve_error

    def unshelve_server(self, server_id):
        self._note("unshelve")
        if self.unshelve_error:
            raise self.unshelve_error

    def start_server(self, server_id):
        self._note("start")


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
    compute = FakeCompute("ACTIVE", ("ACTIVE", "shelving"), SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get", "shelve", "get", "get"]


async def test_an_already_offloaded_server_is_success_without_a_second_shelve():
    compute = FakeCompute(SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get"]


@pytest.mark.parametrize("task_state", SHELVE_TASKS)
@pytest.mark.parametrize("status", ["ACTIVE", "SHELVED", SHELVED_OFFLOADED, "SHUTOFF", "PAUSED", "SUSPENDED"])
async def test_a_shelve_already_in_progress_is_awaited_not_repeated(status, task_state):
    compute = FakeCompute((status, task_state), SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get", "get"]


async def test_an_idle_shelved_server_still_waits_for_offload():
    compute = FakeCompute("SHELVED", "SHELVED", SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get", "get", "get"]


async def test_shelve_waits_for_the_offloading_task_to_clear_at_the_target_status():
    compute = FakeCompute("ACTIVE", (SHELVED_OFFLOADED, "shelving_offloading"), SHELVED_OFFLOADED)
    await provider(compute).shelve(SID)
    assert compute.calls == ["get", "shelve", "get", "get"]


@pytest.mark.parametrize("task_state", UNSHELVE_TASKS)
async def test_shelve_during_unshelving_conflicts_without_a_second_action(task_state):
    compute = FakeCompute((SHELVED_OFFLOADED, task_state))
    with pytest.raises(ProviderError, match="unshelving is still in progress"):
        await provider(compute).shelve(SID)
    assert compute.calls == ["get"]


async def test_shelve_of_a_server_in_another_state_is_refused_without_calling_nova():
    compute = FakeCompute("ERROR")
    with pytest.raises(ProviderError, match="not shelvable"):
        await provider(compute).shelve(SID)
    assert compute.calls == ["get"]


async def test_shelve_that_never_reaches_offloaded_times_out_as_a_provider_error():
    compute = FakeCompute("ACTIVE", ("ACTIVE", "shelving"))
    with pytest.raises(ProviderError, match="timed out"):
        await provider(compute, timeout=3.0).shelve(SID)


async def test_sdk_failure_is_mapped_to_a_provider_error_without_leaking_details():
    compute = FakeCompute("ACTIVE", shelve_error=RuntimeError("token=secret"))
    with pytest.raises(ProviderError) as info:
        await provider(compute).shelve(SID)
    assert "secret" not in str(info.value)
    assert "RuntimeError" in str(info.value)


@pytest.mark.parametrize("status", ["SHELVED", SHELVED_OFFLOADED])
async def test_unshelve_of_an_idle_shelved_server_waits_for_active(status):
    compute = FakeCompute(status, (SHELVED_OFFLOADED, "unshelving"), ("ACTIVE", "spawning"))
    await provider(compute).unshelve(SID)
    assert compute.calls == ["get", "unshelve", "get", "get"]


@pytest.mark.parametrize("task_state", SHELVE_TASKS)
@pytest.mark.parametrize("status", ["ACTIVE", "SHELVED", SHELVED_OFFLOADED, "SHUTOFF", "PAUSED", "SUSPENDED"])
async def test_unshelve_during_shelving_conflicts_instead_of_returning_active(status, task_state):
    compute = FakeCompute((status, task_state))
    with pytest.raises(ProviderError, match="shelving is still in progress"):
        await provider(compute).unshelve(SID)
    assert compute.calls == ["get"]


@pytest.mark.parametrize("task_state", UNSHELVE_TASKS)
@pytest.mark.parametrize("status", ["SHELVED", SHELVED_OFFLOADED])
async def test_unshelve_already_in_progress_waits_without_a_second_action(status, task_state):
    compute = FakeCompute((status, task_state), (status, task_state), ("ACTIVE", task_state))
    await provider(compute).unshelve(SID)
    assert compute.calls == ["get", "get", "get"]


@pytest.mark.parametrize("task_state", UNSHELVE_TASKS)
async def test_unshelve_of_an_active_server_with_an_unrelated_task_is_success_without_a_call(task_state):
    compute = FakeCompute(("ACTIVE", task_state))
    await provider(compute).unshelve(SID)
    assert compute.calls == ["get"]


async def test_unshelve_does_not_finish_if_shelving_starts_during_the_wait():
    compute = FakeCompute(SHELVED_OFFLOADED, ("ACTIVE", "shelving"))
    with pytest.raises(ProviderError, match="timed out"):
        await provider(compute, timeout=3.0).unshelve(SID)
    assert compute.calls.count("unshelve") == 1


async def test_unshelve_sdk_failure_does_not_leak_details():
    compute = FakeCompute(SHELVED_OFFLOADED, unshelve_error=RuntimeError("token=secret"))
    with pytest.raises(ProviderError) as info:
        await provider(compute).unshelve(SID)
    assert str(info.value) == "nova call failed: RuntimeError"


async def test_unshelve_of_an_active_server_is_success_without_a_call():
    compute = FakeCompute("ACTIVE")
    await provider(compute).unshelve(SID)
    assert compute.calls == ["get"]


async def test_unshelve_of_a_building_server_is_refused():
    compute = FakeCompute("BUILD")
    with pytest.raises(ProviderError, match="not unshelvable"):
        await provider(compute).unshelve(SID)
    assert compute.calls == ["get"]


async def test_start_of_a_stopped_server_waits_for_active():
    compute = FakeCompute("SHUTOFF", "SHUTOFF", "ACTIVE")
    await provider(compute).start(SID)
    assert compute.calls == ["get", "start", "get", "get"]


async def test_start_of_an_active_server_is_success_without_a_call():
    compute = FakeCompute("ACTIVE")
    await provider(compute).start(SID)
    assert compute.calls == ["get"]


@pytest.mark.parametrize("task_state", SHELVE_TASKS)
async def test_start_during_shelving_conflicts_without_powering_on(task_state):
    compute = FakeCompute(("SHUTOFF", task_state))
    with pytest.raises(ProviderError, match="shelving is still in progress"):
        await provider(compute).start(SID)
    assert compute.calls == ["get"]


@pytest.mark.parametrize("status", ["SHELVED_OFFLOADED", "BUILD", "ERROR", "PAUSED"])
async def test_start_of_a_server_that_is_not_stopped_is_refused_without_calling_nova(status):
    compute = FakeCompute(status)
    with pytest.raises(ProviderError, match="not startable"):
        await provider(compute).start(SID)
    assert compute.calls == ["get"]


async def test_start_that_never_reaches_active_times_out_as_a_provider_error():
    compute = FakeCompute("SHUTOFF")
    with pytest.raises(ProviderError, match="timed out"):
        await provider(compute, timeout=3.0).start(SID)


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


async def test_server_state_reports_normalized_sdk_fields_without_effects():
    compute = FakeCompute(("active", "SHELVING_IMAGE_PENDING_UPLOAD"))
    assert await provider(compute).server_state(SID) == ServerState("ACTIVE", "shelving_image_pending_upload")
    assert compute.calls == ["get"]
    assert all(thread is not threading.main_thread() for thread in compute.threads)


@pytest.mark.parametrize("key", ["task_state", "OS-EXT-STS:task_state"])
async def test_server_state_reads_mapping_fields(key):
    compute = FakeCompute({"status": "shelved_offloaded", key: "UNSHELVING"})
    state = await provider(compute).server_state(SID)
    assert state == ServerState(SHELVED_OFFLOADED, "unshelving")
    assert state.unshelving
    assert compute.calls == ["get"]


async def test_server_state_reads_raw_task_state_when_sdk_attribute_is_none():
    class SDKServer(dict):
        status = "ACTIVE"
        task_state = None

    compute = FakeCompute(SDKServer({"OS-EXT-STS:task_state": "SHELVING"}))
    assert (await provider(compute).server_state(SID)).shelving


async def test_server_state_rejects_invalid_task_state_without_leaking_it():
    compute = FakeCompute({"status": "ACTIVE", "task_state": {"token": "secret"}})
    with pytest.raises(ProviderError) as info:
        await provider(compute).server_state(SID)
    assert str(info.value) == "nova returned invalid server task state"


def test_server_state_is_immutable_and_normalizes_fields():
    state = ServerState("shelved", "SPAWNING")
    assert state.status == "SHELVED"
    assert state.task_state == "spawning"
    with pytest.raises(FrozenInstanceError):
        state.status = "ACTIVE"


@pytest.mark.parametrize("task_state", SHELVE_TASKS)
@pytest.mark.parametrize(
    "status", ["ACTIVE", "SHELVED", SHELVED_OFFLOADED, "BUILD", "ERROR", "SHUTOFF", "PAUSED", "SUSPENDED", "UNKNOWN"]
)
def test_server_state_recognizes_shelving_tasks(status, task_state):
    state = ServerState(status, task_state)
    assert state.shelving
    assert not state.unshelving
    assert not state.shelved


@pytest.mark.parametrize("task_state", UNSHELVE_TASKS)
@pytest.mark.parametrize("status", ["SHELVED", SHELVED_OFFLOADED])
def test_server_state_recognizes_unshelving_tasks(status, task_state):
    state = ServerState(status, task_state)
    assert not state.shelving
    assert state.unshelving
    assert not state.shelved


@pytest.mark.parametrize("status", ["ACTIVE", "SHELVED", SHELVED_OFFLOADED])
def test_server_state_distinguishes_idle_statuses(status):
    state = ServerState(status)
    assert not state.shelving
    assert not state.unshelving
    assert state.shelved == (status in {"SHELVED", SHELVED_OFFLOADED})


@pytest.mark.parametrize("status", ["ACTIVE", "BUILD", "ERROR", "SHUTOFF", "PAUSED", "SUSPENDED", "UNKNOWN"])
@pytest.mark.parametrize("task_state", UNSHELVE_TASKS)
def test_server_state_does_not_treat_unrelated_statuses_as_unshelving(status, task_state):
    state = ServerState(status, task_state)
    assert not state.shelving
    assert not state.unshelving
    assert not state.shelved


@pytest.mark.parametrize("status", ["ACTIVE", "SHELVED", SHELVED_OFFLOADED, "SHUTOFF", "PAUSED"])
@pytest.mark.parametrize("task_state", ["shelving_unknown", "shelving_done", "rebuilding", "powering-on"])
def test_server_state_does_not_confuse_unrecognized_tasks_with_shelving(status, task_state):
    state = ServerState(status, task_state)
    assert not state.shelving
    assert not state.unshelving
    assert state.shelved == (status in {"SHELVED", SHELVED_OFFLOADED})
