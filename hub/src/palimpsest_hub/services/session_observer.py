"""Concrete session observer: guest login sessions from `who` and the process table.

The command runner is injected (for example an exec through the instance's management path).
The runner MUST be a non-pty exec: a pty session writes its own utmp line, which `who` then lists
and counts, and the probe cannot exclude it, so such a session would block shelve indefinitely.
Any failure, timeout, non-text result or malformed line is an `ObservationError`, never an
empty set: an empty set reads as "no user" and could lead to a shelve.

The probe runs through the same SSH path that it observes. Its own sshd session is therefore an
ancestor of the probe shell and is excluded by walking the process table up from `$$`. If the
shell is missing from the table, no ancestor can be identified, and the listing is refused rather
than guessed (fail closed: an unknown session blocks shelve).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

from palimpsest_hub.services.session_controller import ObservationError

DEFAULT_TIMEOUT_SECONDS = 10.0

# The guest probe must print both markers. UTMP_OK proves the login database was readable (an
# OCI root without utmp lists nobody with exit 0); END proves the listing was not truncated.
UTMP_MARKER = "PALIMPSEST-UTMP-OK"
SELF_MARKER = "PALIMPSEST-SELF"
END_MARKER = "PALIMPSEST-END"
# A post-authentication session title: `sshd: user@pts/N|notty` (OpenSSH < 9.8) or
# `sshd-session: user@pts/N|notty` (OpenSSH >= 9.8). The listener (`sshd: /usr/sbin/sshd -D
# [listener] ...`) and pre-auth (`sshd: [accepted]`, `sshd: user [priv]`) titles do not match.
SSHD_SESSION_PATTERN = r"sshd(-session)?: [^ ]+@(pts/[0-9]+|notty)"
SSHD_SESSION_RE = re.compile(SSHD_SESSION_PATTERN)
PS_ROW_RE = re.compile(r"([0-9]+) ([0-9]+)(?: (.+))?")
# Split so the process-table part can be run locally against a real `sh` and `ps`.
_UTMP_PART = "if [ -r /run/utmp ] || [ -r /var/run/utmp ]; then echo PALIMPSEST-UTMP-OK; who || exit 1; fi; "
_PROCESS_PART = (
    "echo PALIMPSEST-SELF $$; "
    # A failed `ps` exits before the end marker, so it can never read as "no sessions".
    "out=$(ps -e -o pid= -o ppid= -o args=) || exit 1; "
    # Only sshd rows keep their argv; every other row is reduced to pid and ppid, so guest
    # command lines (which can carry tokens) never leave the guest.
    "printf '%s\\n' \"$out\" | awk '{ if ($3 ~ /^sshd/) print; else print $1, $2 }' || exit 1; "
    "echo PALIMPSEST-END"
)
GUEST_PROBE_COMMAND = _UTMP_PART + _PROCESS_PART


def parse_who(text: str) -> set[str]:
    """Return one normalised identity per login, or raise when the probe is not trustworthy.

    Counted: `who` lines (pty logins) and every post-authentication sshd session in the process
    table that is not an ancestor of the probe (non-pty `ssh host cmd`, scp, rsync, forwards).
    Each sshd session is keyed by its pid, so two sessions for one user stay distinct.
    """
    if not isinstance(text, str):
        raise ObservationError("session listing is not text")
    lines = [" ".join(raw.split()) for raw in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines or lines[-1] != END_MARKER or lines.count(END_MARKER) != 1:
        raise ObservationError("session listing is truncated or missing its end marker")
    if lines[0] != UTMP_MARKER:
        raise ObservationError("login database was not readable")
    selves = [line for line in lines if line.startswith(SELF_MARKER + " ")]
    if len(selves) != 1 or not re.fullmatch(SELF_MARKER + " [0-9]+", selves[0]):
        raise ObservationError("probe shell identity missing")
    self_index = lines.index(selves[0])
    self_pid = int(selves[0].split(" ")[1])

    sessions: set[str] = set()
    for line in lines[1:self_index]:
        if len(line.split(" ")) < 2:
            raise ObservationError("malformed session line")
        sessions.add(line)

    table: dict[int, tuple[int, str]] = {}
    for line in lines[self_index + 1 : -1]:
        match = PS_ROW_RE.fullmatch(line)
        if not match:
            raise ObservationError("malformed process line")
        pid, ppid, args = int(match.group(1)), int(match.group(2)), match.group(3) or ""
        table[pid] = (ppid, args)
    if self_pid not in table:
        raise ObservationError("probe shell missing from the process table")

    ancestors: set[int] = set()
    pid = self_pid
    while pid in table and pid not in ancestors:
        ancestors.add(pid)
        pid = table[pid][0]

    for pid, (_ppid, args) in table.items():
        if pid in ancestors:
            continue
        if SSHD_SESSION_RE.fullmatch(args):
            sessions.add(f"{args} (pid {pid})")
    return sessions


class WhoSessionObserver:
    """Observe a guest by running the probe through the injected `fetch(instance_id)`."""

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


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "GUEST_PROBE_COMMAND",
    "SSHD_SESSION_PATTERN",
    "WhoSessionObserver",
    "parse_who",
]
