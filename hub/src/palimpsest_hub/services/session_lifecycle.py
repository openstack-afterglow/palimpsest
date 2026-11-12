"""Pure last-session / grace-timer / shelve-claim state machine for dev instances.

No clocks, I/O or provider calls live here: callers pass `now` (monotonic seconds)
and serialise access (for example under the instance row lock). The machine decides
only when a delayed shelve may start; it never performs it.

Rules:
* a shelve is due only when no session is open and the grace period has fully elapsed;
* any (re)connect cancels the timer and invalidates every earlier shelve claim;
* an observation failure is not a disconnect and never starts or advances the timer;
* only one claim can win per due period, so concurrent workers cannot both shelve;
* a connect that arrives after a claim started wins the instance back: the in-flight
  shelve is reported as `reconnected` so the caller must unshelve once it settles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

GRACE_SECONDS = 300.0


class Phase(StrEnum):
    ACTIVE = "active"
    GRACE = "grace"
    SHELVING = "shelving"
    SHELVED = "shelved"


class Decision(StrEnum):
    WAIT = "wait"
    SHELVE_DUE = "shelve-due"


@dataclass(frozen=True, slots=True)
class Claim:
    generation: int


@dataclass(slots=True)
class SessionMachine:
    grace_seconds: float = GRACE_SECONDS
    phase: Phase = Phase.GRACE
    sessions: set[str] = field(default_factory=set)
    generation: int = 0
    grace_started: float | None = None
    reconnected_during_shelve: bool = False
    observation_failures: int = 0

    def __post_init__(self) -> None:
        if self.grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")

    @classmethod
    def created(cls, now: float, grace_seconds: float = GRACE_SECONDS) -> SessionMachine:
        """A new instance nobody has connected to yet starts its grace period immediately."""
        return cls(grace_seconds=grace_seconds, phase=Phase.GRACE, grace_started=now)

    def connect(self, session_id: str, now: float) -> None:
        self.sessions.add(session_id)
        self.generation += 1
        self.grace_started = None
        if self.phase is Phase.SHELVING:
            self.reconnected_during_shelve = True
        elif self.phase is Phase.SHELVED:
            # The caller must unshelve; the machine only records that a user is present.
            self.reconnected_during_shelve = True
        else:
            self.phase = Phase.ACTIVE

    def disconnect(self, session_id: str, now: float) -> None:
        if session_id not in self.sessions:
            return
        self.sessions.discard(session_id)
        self.generation += 1
        if not self.sessions and self.phase is Phase.ACTIVE:
            self.phase = Phase.GRACE
            self.grace_started = now

    def observation_failed(self, now: float) -> None:
        """A failed probe is unknown, not a disconnect: no session, timer or phase change."""
        self.observation_failures += 1

    def decide(self, now: float) -> Decision:
        due = (
            self.phase is Phase.GRACE
            and not self.sessions
            and self.grace_started is not None
            and now - self.grace_started >= self.grace_seconds
        )
        return Decision.SHELVE_DUE if due else Decision.WAIT

    def claim_shelve(self, now: float) -> Claim | None:
        """Atomically take the single right to shelve, or None when not due or already taken."""
        if self.decide(now) is not Decision.SHELVE_DUE:
            return None
        self.phase = Phase.SHELVING
        self.reconnected_during_shelve = False
        return Claim(self.generation)

    def shelve_still_valid(self, claim: Claim) -> bool:
        """True only if nothing connected since the claim was taken."""
        return self.phase is Phase.SHELVING and claim.generation == self.generation

    def shelve_settled(self, claim: Claim, now: float, *, ok: bool) -> bool:
        """Record the provider outcome; returns True when the caller must unshelve now."""
        if self.phase is not Phase.SHELVING:
            raise ValueError("no shelve in flight")
        reconnected = self.reconnected_during_shelve or claim.generation != self.generation
        if not ok:
            # A failed or unknown provider outcome keeps the instance as it is and restarts the
            # full grace period, so a failing shelve is never retried in a tight loop.
            self.phase = Phase.ACTIVE if self.sessions else Phase.GRACE
            self.grace_started = None if self.sessions else now
            return False
        self.phase = Phase.SHELVED
        return reconnected

    def unshelved(self, now: float | None = None) -> None:
        """Back from SHELVED. With no session the full grace period starts at `now` (when given).

        Without `now` a session-less machine has no running timer and never becomes due until a
        session connects and leaves; callers that resume a user-less instance must pass `now`.
        """
        if self.phase is not Phase.SHELVED:
            raise ValueError("instance is not shelved")
        self.reconnected_during_shelve = False
        self.phase = Phase.ACTIVE if self.sessions else Phase.GRACE
        self.grace_started = None if self.sessions or now is None else now

    def to_state(self) -> dict:
        """JSON-serializable snapshot; times are the caller's clock (persist with a wall clock)."""
        return {
            "grace_seconds": self.grace_seconds,
            "phase": self.phase.value,
            "sessions": sorted(self.sessions),
            "generation": self.generation,
            "grace_started": self.grace_started,
            "reconnected_during_shelve": self.reconnected_during_shelve,
            "observation_failures": self.observation_failures,
        }

    @classmethod
    def from_state(cls, state: dict) -> SessionMachine:
        """Rebuild a snapshot; any malformed field raises ValueError (callers fail closed)."""
        try:
            sessions = state["sessions"]
            started = state["grace_started"]
            if (
                not isinstance(sessions, list)
                or not all(isinstance(item, str) for item in sessions)
                or type(state["generation"]) is not int
                or type(state["observation_failures"]) is not int
                or type(state["reconnected_during_shelve"]) is not bool
                or (started is not None and (type(started) not in (int, float) or isinstance(started, bool)))
                or type(state["grace_seconds"]) not in (int, float)
            ):
                raise ValueError("malformed session machine state")
            return cls(
                grace_seconds=float(state["grace_seconds"]),
                phase=Phase(state["phase"]),
                sessions=set(sessions),
                generation=state["generation"],
                grace_started=None if started is None else float(started),
                reconnected_during_shelve=state["reconnected_during_shelve"],
                observation_failures=state["observation_failures"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed session machine state") from exc
