"""Concrete session observer: guest login sessions from `who` output.

The command runner is injected (for example an exec through the instance's management path).
Any failure, timeout, non-text result or malformed line is an `ObservationError`, never an
empty set: an empty set reads as "no user" and could lead to a shelve.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from palimpsest_hub.services.session_controller import ObservationError

DEFAULT_TIMEOUT_SECONDS = 10.0


def parse_who(text: str) -> set[str]:
    """Return one normalised identity per `who` line; blank lines are ignored."""
    if not isinstance(text, str):
        raise ObservationError("session listing is not text")
    sessions: set[str] = set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if len(line.split(" ")) < 2:
            raise ObservationError("malformed session line")
        sessions.add(line)
    return sessions


class WhoSessionObserver:
    """Observe a guest by running `who` through the injected `fetch(instance_id)`."""

    def __init__(
        self,
        fetch: Callable[[str], Awaitable[str]],
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._fetch = fetch
        self._timeout = timeout_seconds

    async def sessions(self, instance_id: str) -> set[str]:
        try:
            text = await asyncio.wait_for(self._fetch(instance_id), self._timeout)
        except TimeoutError:
            raise ObservationError("session listing timed out") from None
        except Exception:
            # The exception text may carry transport detail; never copy it into the error.
            raise ObservationError("session listing failed") from None
        return parse_who(text)


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "WhoSessionObserver", "parse_who"]
