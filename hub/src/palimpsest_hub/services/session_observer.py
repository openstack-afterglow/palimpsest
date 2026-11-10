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

# The guest probe must print both markers. UTMP_OK proves the login database was readable (an
# OCI root without utmp lists nobody with exit 0); END proves the listing was not truncated.
UTMP_MARKER = "PALIMPSEST-UTMP-OK"
END_MARKER = "PALIMPSEST-END"
GUEST_PROBE_COMMAND = (
    "if [ -r /run/utmp ] || [ -r /var/run/utmp ]; then echo PALIMPSEST-UTMP-OK; who; fi; "
    "ps -eo args= | grep '^sshd: ' | grep -v grep; "
    "echo PALIMPSEST-END"
)


def parse_who(text: str) -> set[str]:
    """Return one normalised identity per login line, or raise when the probe is not trustworthy.

    Counted: `who` lines (pty logins) and `sshd: user@pts/N|notty` process lines (every sshd
    session, including non-pty `ssh host cmd`, scp, rsync and forwards that never reach utmp).
    """
    if not isinstance(text, str):
        raise ObservationError("session listing is not text")
    lines = [" ".join(raw.split()) for raw in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[-1] != END_MARKER or lines.count(END_MARKER) != 1:
        raise ObservationError("session listing is truncated or missing its end marker")
    if UTMP_MARKER not in lines:
        raise ObservationError("login database was not readable")
    sessions: set[str] = set()
    for line in lines[:-1]:
        if line == UTMP_MARKER:
            continue
        if line.startswith("sshd: "):
            sessions.add(line)
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


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "GUEST_PROBE_COMMAND", "WhoSessionObserver", "parse_who"]
