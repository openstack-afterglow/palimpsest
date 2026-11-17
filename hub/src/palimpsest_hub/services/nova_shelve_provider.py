"""Nova-backed `ShelveProvider` for the delayed dev-instance shelve.

The connection is injected, so this module chooses no credential: which identity may shelve
or unshelve a server is the delegation question tracked as M4-11. Every SDK call runs through
`run_blocking`, never on the event loop.

Outcomes are confirmed from the server status and task state, not from request acceptance:
* `shelve` succeeds only at idle SHELVED_OFFLOADED; an already offloaded server is success,
  so a retry after an unknown outcome is idempotent and never issues a second shelve;
* an action already in progress is awaited, not repeated; unshelve during shelving conflicts;
* any other state, an SDK error or a timeout raises `ProviderError`; the caller keeps the
  instance as it is and restarts its full grace period.

If the cloud never offloads a shelved server (Nova `shelve_offload_time` not zero), the wait
times out and fails closed; the server stays SHELVED, which is a visible state, not a silent loss.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from palimpsest_hub.services.session_controller import ProviderError
from palimpsest_hub.services.worker_leases import run_blocking

SHELVED_OFFLOADED = "SHELVED_OFFLOADED"
_SHELVED_STATUSES = frozenset({"SHELVED", SHELVED_OFFLOADED})
_SHELVE_TASKS = frozenset(
    {"shelving", "shelving_image_pending_upload", "shelving_image_uploading", "shelving_offloading"}
)
_UNSHELVE_TASKS = frozenset({"unshelving", "spawning"})


@dataclass(frozen=True, slots=True)
class ServerState:
    """Nova's public status plus task state; transitional tasks do not define statuses."""

    status: str
    task_state: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", self.status.upper())
        if self.task_state is not None:
            object.__setattr__(self, "task_state", self.task_state.lower())

    @property
    def shelving(self) -> bool:
        return self.task_state in _SHELVE_TASKS

    @property
    def unshelving(self) -> bool:
        return self.status in _SHELVED_STATUSES and self.task_state in _UNSHELVE_TASKS

    @property
    def shelved(self) -> bool:
        return self.status in _SHELVED_STATUSES and not self.shelving and not self.unshelving


class NovaShelveProvider:
    def __init__(
        self,
        connection: Any,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("timeout_seconds and poll_seconds must be positive")
        self._compute = connection.compute
        self._timeout = timeout_seconds
        self._poll = poll_seconds
        self._clock = clock
        self._sleep = sleep

    async def _call(self, fn, *args):
        try:
            return await run_blocking(fn, *args)
        except Exception as exc:
            raise ProviderError(f"nova call failed: {type(exc).__name__}") from None

    async def server_state(self, server_id: str) -> ServerState:
        """Read normalized Nova status and task state without issuing a lifecycle action."""
        server = await self._call(self._compute.get_server, server_id)
        status = getattr(server, "status", None)
        task_state = getattr(server, "task_state", None)
        if isinstance(server, Mapping):
            if status is None:
                status = server.get("status")
            if task_state is None:
                task_state = server.get("task_state")
            if task_state is None:
                task_state = server.get("OS-EXT-STS:task_state")
        if not isinstance(status, str):
            raise ProviderError("nova returned no server status")
        if task_state is not None and not isinstance(task_state, str):
            raise ProviderError("nova returned invalid server task state")
        return ServerState(status, task_state)

    async def _await_status(self, server_id: str, target: str) -> None:
        deadline = self._clock() + self._timeout
        while True:
            state = await self.server_state(server_id)
            if state.status == target and not state.shelving and not state.unshelving:
                return
            if self._clock() >= deadline:
                raise ProviderError(f"timed out waiting for {target}")
            await self._sleep(self._poll)

    async def shelve(self, instance_id: str) -> None:
        state = await self.server_state(instance_id)
        if state.unshelving:
            raise ProviderError("server unshelving is still in progress")
        if state.status == SHELVED_OFFLOADED and not state.shelving:
            return
        if not state.shelving:
            if state.status == "ACTIVE":
                await self._call(self._compute.shelve_server, instance_id)
            elif state.status != "SHELVED":
                raise ProviderError(f"server is {state.status}, not shelvable")
        await self._await_status(instance_id, SHELVED_OFFLOADED)

    async def unshelve(self, instance_id: str) -> None:
        state = await self.server_state(instance_id)
        if state.shelving:
            raise ProviderError("server shelving is still in progress")
        if state.unshelving:
            await self._await_status(instance_id, "ACTIVE")
            return
        if state.status == "ACTIVE":
            return
        if state.shelved:
            await self._call(self._compute.unshelve_server, instance_id)
        else:
            raise ProviderError(f"server is {state.status}, not unshelvable")
        await self._await_status(instance_id, "ACTIVE")

    async def start(self, instance_id: str) -> None:
        """Power on a stopped server; any state other than SHUTOFF or ACTIVE is refused without a call."""
        state = await self.server_state(instance_id)
        if state.shelving:
            raise ProviderError("server shelving is still in progress")
        if state.unshelving:
            raise ProviderError("server unshelving is still in progress")
        status = state.status
        if status == "ACTIVE":
            return
        if status != "SHUTOFF":
            raise ProviderError(f"server is {status}, not startable")
        await self._call(self._compute.start_server, instance_id)
        await self._await_status(instance_id, "ACTIVE")
