"""Nova-backed `ShelveProvider` for the delayed dev-instance shelve.

The connection is injected, so this module chooses no credential: which identity may shelve
or unshelve a server is the delegation question tracked as M4-11. Every SDK call runs through
`run_blocking`, never on the event loop.

Outcomes are confirmed from the server status, not from the request being accepted:
* `shelve` succeeds only at SHELVED_OFFLOADED; an already offloaded server is success, so a
  retry after an unknown outcome is idempotent and never issues a second shelve;
* a shelve or unshelve already in progress is awaited, not repeated;
* any other state, an SDK error or a timeout raises `ProviderError`; the caller keeps the
  instance as it is and restarts its full grace period.

If the cloud never offloads a shelved server (Nova `shelve_offload_time` not zero), the wait
times out and fails closed; the server stays SHELVED, which is a visible state, not a silent loss.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from palimpsest_hub.services.session_controller import ProviderError
from palimpsest_hub.services.worker_leases import run_blocking

SHELVED_OFFLOADED = "SHELVED_OFFLOADED"
_SHELVE_IN_PROGRESS = frozenset({"SHELVING", "SHELVING_IMAGE_PENDING_UPLOAD", "SHELVING_IMAGE_UPLOADING", "SHELVED"})
_UNSHELVABLE = frozenset({"SHELVED", SHELVED_OFFLOADED})


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

    async def _status(self, server_id: str) -> str:
        server = await self._call(self._compute.get_server, server_id)
        status = getattr(server, "status", None)
        if not isinstance(status, str):
            raise ProviderError("nova returned no server status")
        return status

    async def server_status(self, server_id: str) -> str:
        """Current Nova status (uppercase); used to settle a shelve whose outcome was unknown."""
        return (await self._status(server_id)).upper()

    async def _await_status(self, server_id: str, target: str) -> None:
        deadline = self._clock() + self._timeout
        while True:
            if await self._status(server_id) == target:
                return
            if self._clock() >= deadline:
                raise ProviderError(f"timed out waiting for {target}")
            await self._sleep(self._poll)

    async def shelve(self, instance_id: str) -> None:
        status = await self._status(instance_id)
        if status == SHELVED_OFFLOADED:
            return
        if status == "ACTIVE":
            await self._call(self._compute.shelve_server, instance_id)
        elif status not in _SHELVE_IN_PROGRESS:
            raise ProviderError(f"server is {status}, not shelvable")
        await self._await_status(instance_id, SHELVED_OFFLOADED)

    async def unshelve(self, instance_id: str) -> None:
        status = await self._status(instance_id)
        if status == "ACTIVE":
            return
        if status in _UNSHELVABLE:
            await self._call(self._compute.unshelve_server, instance_id)
        elif status != "UNSHELVING":
            raise ProviderError(f"server is {status}, not unshelvable")
        await self._await_status(instance_id, "ACTIVE")
