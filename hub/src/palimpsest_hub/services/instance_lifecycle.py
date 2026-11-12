"""Idle auto-shelve for `lifecycle="dev"` instances, wired to durable state.

Pieces (each already tested on its own): `SessionMachine` decides timing, `SessionController`
drives it, `WhoSessionObserver.parse_who` reads the guest probe, `NovaShelveProvider` talks to Nova.
This module supplies what production needs around them:

* the guest pushes a periodic probe report with its own narrow capability (the Hub never opens a
  connection into a guest); the Hub keeps only the parsed session identities and the server-side
  receipt time. A report older than the configured age, or one that fails to parse, is an unknown
  observation: it never counts as "no session" and never moves the timer;
* the machine is persisted in SQL (wall-clock times), claimed under the same per-instance
  `build-api:instance:` mutex the API uses, and checkpointed before the Nova call so a crash
  leaves a durable SHELVING phase that the next tick settles from the server's real status;
* shelve runs through a Keystone Trust created from the owner's own request at instance creation
  (instance-lifetime, finite, renewed only by the owner's requests), never a service credential.
  An expired or unreadable trust fails closed: the instance keeps running;
* the owner resumes a shelved instance with their own token (`resume`), which starts a new full
  grace period. A shelved guest cannot report, so only an explicit resume brings it back.

Processes inside a shelved guest do not survive: shelve offloads the VM, the boot volume (with the
instance's writable layer) is preserved, and a resumed guest boots from that disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from palimpsest_hub.instance_lifecycle_models import PalimpsestHubInstanceLifecycle as Lifecycle
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.models import PalimpsestWorkerLease
from palimpsest_hub.openstack import (
    DelegationError,
    ExportDelegation,
    create_export_trust,
    delete_export_trust,
    delete_trust_as_trustor,
    open_trust_connection,
)
from palimpsest_hub.services import instances as instance_service
from palimpsest_hub.services.nova_shelve_provider import SHELVED_OFFLOADED, NovaShelveProvider
from palimpsest_hub.services.session_controller import (
    ObservationError,
    Outcome,
    ProviderError,
    SessionController,
)
from palimpsest_hub.services.session_lifecycle import GRACE_SECONDS, Claim, Phase, SessionMachine
from palimpsest_hub.services.session_observer import parse_who
from palimpsest_hub.services.worker_leases import run_blocking, sql_mutex

_logger = logging.getLogger(__name__)

# Guest status values after which an instance may be auto-shelved. A booting guest is still
# downloading layers and must never be shelved, and a shelved one has nothing left to observe.
SHELVABLE_STATUSES = frozenset({"ready", "error", "resumed"})
# Keep a trust at least this long before its expiry; a nearly expired trust is treated as expired.
_TRUST_MARGIN = timedelta(seconds=120)
_MAX_REPORTED_SESSIONS = 256


def new_capability() -> tuple[str, str]:
    """Return a random guest session-report capability and its SHA-256 hash."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(stamp: datetime) -> datetime:
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


# -- trust --------------------------------------------------------------------------------------


def delegation_json(delegation: ExportDelegation) -> dict[str, Any]:
    return {
        "trust_id": delegation.trust_id,
        "project_id": delegation.project_id,
        "trustor_user_id": delegation.trustor_user_id,
        "trustee_user_id": delegation.trustee_user_id,
        "role_names": list(delegation.role_names),
    }


def delegation_from(row: Lifecycle) -> ExportDelegation | None:
    """Rebuild the stored trust reference, or None when absent or malformed (fail closed)."""
    data, expires = row.trust_json, row.trust_expires_at
    if not isinstance(data, dict) or expires is None:
        return None
    try:
        roles = data["role_names"]
        if not isinstance(roles, list) or not all(isinstance(item, str) for item in roles):
            return None
        return ExportDelegation(
            trust_id=str(data["trust_id"]),
            project_id=str(data["project_id"]),
            trustor_user_id=str(data["trustor_user_id"]),
            trustee_user_id=str(data["trustee_user_id"]),
            role_names=tuple(roles),
            expires_at=_aware(expires),
        )
    except (KeyError, TypeError):
        return None


async def admit_trust(actor: dict, project_id: str, settings) -> ExportDelegation:
    """Create the owner's bounded trust from the owner's own validated request."""
    role_names = tuple(settings.palimpsest_hub_session_delegated_roles)
    if not set(role_names) <= set(actor.get("current_roles") or ()):
        raise HTTPException(403, "The requester does not currently hold the project role delegated for auto-shelve")
    auth_ref = actor.get("auth_ref")
    user_id = actor.get("user_id")
    if (
        auth_ref is None
        or auth_ref.auth_token != actor.get("token")
        or auth_ref.project_id != project_id
        or auth_ref.user_id != user_id
    ):
        raise HTTPException(401, "A validated original Keystone token is required for auto-shelve delegation")
    try:
        return await asyncio.to_thread(
            create_export_trust,
            auth_ref,
            project_id=project_id,
            trustor_user_id=user_id,
            role_names=role_names,
            ttl_seconds=settings.palimpsest_hub_session_delegation_ttl_seconds,
        )
    except DelegationError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


async def discard_trust(actor: dict, delegation: ExportDelegation) -> bool:
    """Delete a trust the owner just created (nothing was bound to it); True when it is gone."""
    return await asyncio.to_thread(delete_trust_as_trustor, actor.get("auth_ref"), delegation.trust_id)


def new_row(
    instance_id: str, project_id: str, delegation: ExportDelegation, now: datetime, *, grace: float = GRACE_SECONDS
) -> Lifecycle:
    """Ledger row for a new dev instance. The grace timer starts at the first fresh guest report."""
    machine = SessionMachine(grace_seconds=grace, phase=Phase.GRACE, grace_started=None)
    return Lifecycle(
        instance_id=instance_id,
        project_id=project_id,
        machine_json=machine.to_state(),
        trust_json=delegation_json(delegation),
        trust_expires_at=delegation.expires_at,
        retired=False,
        created_at=now,
        updated_at=now,
    )


async def bind_capability(session, instance_id: str, capability_hash: str) -> None:
    """Record the guest's session-report capability hash in the caller's own transaction."""
    await session.execute(
        update(Lifecycle)
        .where(Lifecycle.instance_id == instance_id)
        .values(session_capability_hash=capability_hash, updated_at=_now())
    )


async def retire(session, instance: PalimpsestHubInstance) -> None:
    """Mark a dev instance as being deleted so the worker revokes its trust and stops observing.

    Service instances have no ledger row and never touch this table, so a database that has not
    yet been bootstrapped with it keeps deleting them normally.
    """
    if instance.spec_json.get("lifecycle") != "dev":
        return
    await session.execute(
        update(Lifecycle).where(Lifecycle.instance_id == instance.id).values(retired=True, updated_at=_now())
    )


async def mark_resumed(session, instance_id: str) -> None:
    """The server is ACTIVE again outside `resume` (for example unshelved in Nova): leave SHELVED."""
    life = await session.get(Lifecycle, instance_id)
    if life is None:
        return
    try:
        machine = SessionMachine.from_state(life.machine_json)
    except ValueError:
        return
    if machine.phase is Phase.SHELVED:
        machine.unshelved(time.time())
        life.machine_json = machine.to_state()
        life.updated_at = _now()


# -- guest report ---------------------------------------------------------------------------------


async def ingest_report(factory, instance_id: str, token: str, probe_text: str) -> None:
    """Accept one guest probe report. Wrong capability or unknown instance is a 404."""
    actual = hashlib.sha256(token.encode()).hexdigest()
    async with factory() as session:
        row = await session.get(Lifecycle, instance_id)
        expected = row.session_capability_hash if row is not None else None
        if row is None or row.retired or not expected or not hmac.compare_digest(actual, expected):
            raise HTTPException(404, "Session report capability not found")
        try:
            sessions = sorted(parse_who(probe_text))
        except ObservationError:
            # An unparseable listing is an unknown observation: keep the previous receipt time.
            raise HTTPException(422, "Session listing is not a trustworthy probe result") from None
        if len(sessions) > _MAX_REPORTED_SESSIONS:
            raise HTTPException(422, "Session listing exceeds the reporting bound")
        now = _now()
        row.reported_sessions_json = sessions
        row.report_received_at = now
        row.updated_at = now
        await session.commit()


class ReportObserver:
    """`SessionObserver` over the last accepted guest report; stale or absent means unknown."""

    def __init__(self, sessions: list | None, received_at: datetime | None, max_age: float, clock=_now) -> None:
        self._sessions = sessions
        self._received_at = received_at
        self._max_age = max_age
        self._clock = clock

    def fresh(self) -> bool:
        if self._sessions is None or self._received_at is None:
            return False
        age = (self._clock() - _aware(self._received_at)).total_seconds()
        return 0 <= age <= self._max_age

    def update(self, sessions: list | None, received_at: datetime | None) -> None:
        """Adopt a report accepted after this observer was built (the recheck must see it)."""
        self._sessions = sessions
        self._received_at = received_at

    async def sessions(self, instance_id: str) -> set[str]:
        if not self.fresh():
            raise ObservationError("guest session report is missing or stale")
        return {item for item in self._sessions if isinstance(item, str)}


# -- provider through the stored trust -----------------------------------------------------------


class TrustShelveProvider:
    """`ShelveProvider` for one instance's server through its trust connection only."""

    def __init__(
        self,
        instance_id: str,
        server_id: str,
        delegation: ExportDelegation,
        settings,
        *,
        open_connection: Callable[..., Any] = open_trust_connection,
        provider_for: Callable[[Any], Any] = NovaShelveProvider,
    ) -> None:
        self._instance_id = instance_id
        self._server_id = server_id
        self._delegation = delegation
        self._roles = frozenset(settings.palimpsest_hub_session_delegated_roles)
        self._open = open_connection
        self._provider_for = provider_for

    async def _provider(self, instance_id: str) -> Any:
        if instance_id != self._instance_id:
            raise ProviderError("instance is outside this delegation")
        try:
            connection = await asyncio.to_thread(self._open, self._delegation, allowed_role_names=self._roles)
        except Exception:
            raise ProviderError("delegated connection is unavailable") from None
        return self._provider_for(connection)

    async def shelve(self, instance_id: str) -> None:
        await (await self._provider(instance_id)).shelve(self._server_id)

    async def unshelve(self, instance_id: str) -> None:
        await (await self._provider(instance_id)).unshelve(self._server_id)

    async def server_status(self, instance_id: str) -> str:
        return await (await self._provider(instance_id)).server_status(self._server_id)


# -- worker tick ---------------------------------------------------------------------------------


async def _refence(session, instance_id: str) -> None:
    """A commit releases the mutex transaction lock; retake the same mutex before continuing."""
    await session.execute(
        update(PalimpsestWorkerLease)
        .where(PalimpsestWorkerLease.name == "build-api:instance:" + instance_id)
        .values(token=secrets.token_hex(16), expires_at=_now())
    )


def _trust_usable(row: Lifecycle, now: datetime) -> ExportDelegation | None:
    delegation = delegation_from(row)
    if delegation is None or delegation.expires_at - _TRUST_MARGIN <= now:
        return None
    return delegation


class _ClaimCancelled(Exception):
    """The instance changed (deleted, no longer shelvable) between the claim and the provider call."""


async def _settle_unknown_shelve(session, instance, life, machine, provider, wall: float) -> str:
    """A durable SHELVING phase means a previous tick died: settle it from Nova's real status."""
    try:
        status = await provider.server_status(instance.id)
    except ProviderError:
        return "recovery-unavailable"
    claim = Claim(machine.generation)
    if status.startswith("SHELVED"):  # SHELVED or SHELVED_OFFLOADED: the VM is off either way
        machine.shelve_settled(claim, wall, ok=True)
        instance.status, instance.provider_status = "shelved", status[:32]
        return "recovered-shelved"
    if status.startswith("SHELVING"):
        return "shelve-in-progress"  # Nova is still working; look again next tick.
    # ACTIVE, SHUTOFF, ERROR or anything else: the shelve did not happen; release with a fresh grace.
    machine.shelve_settled(claim, wall, ok=False)
    return "recovered-active" if status == "ACTIVE" else "recovered-not-shelved"


async def tick_instance(
    factory,
    settings,
    instance_id: str,
    *,
    clock: Callable[[], float] = time.time,
    provider_factory: Callable[..., Any] = TrustShelveProvider,
) -> str:
    """One locked lifecycle step for a dev instance. Returns a short outcome label."""
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        instance = await session.get(PalimpsestHubInstance, instance_id)
        life = await session.get(Lifecycle, instance_id)
        if instance is None or life is None or life.retired or instance.status == "deleted":
            return "skipped"
        if instance.status not in SHELVABLE_STATUSES and instance.status != "shelved":
            return "not-shelvable"
        if not instance.server_id:
            return "no-server"
        try:
            machine = SessionMachine.from_state(life.machine_json)
        except ValueError:
            life.last_error = "state_invalid"
            await session.commit()
            return "state-invalid"
        if machine.phase is Phase.SHELVED:
            return "shelved"
        now = _now()
        wall = clock()
        delegation = _trust_usable(life, now)
        if delegation is None:
            # Observations are not fed to the machine without a usable trust, so any running timer
            # would be stale after a renew: clear it and let the next fresh report restart it.
            if machine.phase is Phase.GRACE:
                machine.grace_started = None
            life.machine_json = machine.to_state()
            life.last_error = "trust_expired"
            life.updated_at = _now()
            await session.commit()
            return "trust-unusable"
        provider = provider_factory(instance_id, instance.server_id, delegation, settings)
        if machine.phase is Phase.SHELVING:
            outcome = await _settle_unknown_shelve(session, instance, life, machine, provider, wall)
            life.machine_json = machine.to_state()
            life.last_error = None
            life.updated_at = _now()
            await session.commit()
            return outcome
        observer = ReportObserver(
            life.reported_sessions_json,
            life.report_received_at,
            float(settings.palimpsest_hub_session_report_max_age_seconds),
        )
        if not observer.fresh():
            # Time with no usable report is unknown, not idle: restart the timer at the next fresh report.
            if machine.phase is Phase.GRACE:
                machine.grace_started = None
        elif machine.phase is Phase.GRACE and machine.grace_started is None and not machine.sessions:
            # The first fresh report starts the grace timer, so boot time never counts as idle time.
            machine.grace_started = wall

        server_id = instance.server_id

        async def checkpoint(_: str, current: SessionMachine) -> None:
            life.machine_json = current.to_state()
            life.updated_at = _now()
            await session.commit()
            await _refence(session, instance_id)
            # The commit released the mutex: re-read what a delete, resume or report may have changed.
            await session.refresh(instance)
            await session.refresh(life)
            observer.update(life.reported_sessions_json, life.report_received_at)
            if life.retired or instance.status not in SHELVABLE_STATUSES:
                raise _ClaimCancelled

        controller = SessionController(observer, provider, clock, machine.grace_seconds, checkpoint=checkpoint)
        controller.adopt(instance_id, machine)
        try:
            result = await controller.tick(instance_id)
        except _ClaimCancelled:
            machine.shelve_settled(Claim(machine.generation), clock(), ok=False)
            life.machine_json = machine.to_state()
            life.updated_at = _now()
            await session.commit()
            return "claim-cancelled"
        life.machine_json = machine.to_state()
        life.last_error = {
            Outcome.OBSERVATION_FAILED: "report_stale",
            Outcome.SHELVE_FAILED: "shelve_failed",
        }.get(result)
        if result is Outcome.SHELVE_FAILED:
            # An accepted shelve can still fail the wait (poll error, timeout, trust expiring mid-wait)
            # while Nova finishes it: stay SHELVING so the next tick settles from Nova's real status.
            with suppress(ProviderError):
                if (await provider.server_status(instance_id)).startswith("SHELV"):
                    machine.phase = Phase.SHELVING
                    life.machine_json = machine.to_state()
                    life.last_error = "shelve_unconfirmed"
        life.updated_at = _now()
        if result is Outcome.SHELVED:
            instance.status, instance.provider_status = "shelved", SHELVED_OFFLOADED
            instance.updated_at = _now()
        await session.commit()
        _logger.info("session lifecycle tick %s -> %s (server %s)", instance_id, result.value, server_id)
        return result.value


async def sweep_retired(factory, settings, *, limit: int = 20) -> int:
    """Revoke the trust of deleted/deleting instances; keep the row until the trust is gone."""
    async with factory() as session:
        ids = list(
            (
                await session.scalars(
                    select(Lifecycle.instance_id)
                    .where(Lifecycle.retired.is_(True), Lifecycle.trust_json.isnot(None))
                    .order_by(Lifecycle.updated_at)
                    .limit(limit)
                )
            ).all()
        )
    revoked = 0
    for instance_id in ids:
        async with factory() as session:
            life = await session.get(Lifecycle, instance_id)
            delegation = delegation_from(life) if life is not None else None
            if life is None or not life.retired:
                continue
            if delegation is None:
                # A malformed reference has nothing left to revoke by id.
                life.trust_json, life.last_error = None, "trust_unrecoverable"
            elif delegation.expires_at <= _now():
                # An expired trust cannot be used or deleted any more; Keystone has already ended it.
                life.trust_json, life.trust_expires_at, life.last_error = None, None, None
            elif await asyncio.to_thread(_revoke_with_trust_token, delegation):
                life.trust_json, life.trust_expires_at, life.last_error = None, None, None
                revoked += 1
            else:
                life.last_error = "trust_revoke_pending"
            life.updated_at = _now()
            await session.commit()
    return revoked


def _revoke_with_trust_token(delegation: ExportDelegation) -> bool:
    try:
        return bool(delete_export_trust(delegation))
    except Exception:
        return False


async def tick_all(factory, settings, lease=None, *, limit: int = 200, tick=tick_instance) -> dict[str, int]:
    """Run one pass: step every live dev instance, then revoke retired trusts. Never raises per row."""
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Lifecycle.instance_id)
                    .join(PalimpsestHubInstance, PalimpsestHubInstance.id == Lifecycle.instance_id)
                    .where(Lifecycle.retired.is_(False), PalimpsestHubInstance.status.in_(SHELVABLE_STATUSES))
                    .order_by(Lifecycle.updated_at)
                    .limit(limit)
                )
            ).all()
        )
    counts: dict[str, int] = {}
    for (instance_id,) in rows:
        if lease is not None:
            lease.ensure_owned()
        try:
            label = await tick(factory, settings, instance_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if type(exc).__name__ == "LeaseLost":
                raise
            _logger.warning("session lifecycle tick failed for %s (%s)", instance_id, type(exc).__name__)
            label = "error"
        counts[label] = counts.get(label, 0) + 1
    counts["revoked"] = await sweep_retired(factory, settings)
    return counts


async def run_session_lifecycle(settings, factory=None) -> dict[str, int] | None:
    """Worker entry: one pass under the exclusive `session-lifecycle` role (None when another holds it)."""
    from palimpsest_hub.database import get_session_factory
    from palimpsest_hub.services.worker_leases import role_lease

    factory = factory or get_session_factory()
    if factory is None:
        return None
    async with role_lease(factory, "session-lifecycle", ttl_seconds=180) as lease:
        if lease is None:
            return None
        return await tick_all(factory, settings, lease)


# -- owner requests --------------------------------------------------------------------------------


async def _owned_dev(session, actor, conn, instance_id: str):
    project = instance_service.request_project(actor, conn)
    instance = await instance_service.owned(session, instance_id, project, actor["user_id"])
    await instance_service.fresh_instance_controller(actor, conn, instance)
    life = await session.get(Lifecycle, instance_id)
    if life is None or life.retired:
        raise HTTPException(409, "Instance has no auto-shelve lifecycle; only lifecycle=dev instances are shelved")
    return project, instance, life


def _require_creator(actor, instance) -> None:
    """Only the creating owner's own request may mint the trust that replaces theirs."""
    if actor.get("user_id") != instance.created_by:
        raise HTTPException(403, "Only the user who created the instance can replace its shelve delegation")


async def _replace_trust(session, actor, project: str, life: Lifecycle, settings) -> ExportDelegation:
    """Mint a fresh trust from the owner's request, persist it, then best-effort delete the old one."""
    old = delegation_from(life)
    new = await admit_trust(actor, project, settings)
    life.trust_json = delegation_json(new)
    life.trust_expires_at = new.expires_at
    life.last_error = None
    life.updated_at = _now()
    try:
        await session.commit()
    except Exception:
        await discard_trust(actor, new)
        raise
    if old is not None:
        deleted = await asyncio.to_thread(_revoke_with_trust_token, old)
        if not deleted:
            _logger.warning("Previous session trust remains bounded by its expiry")
    return new


async def renew_trust(factory, actor, conn, instance_id: str, settings) -> dict:
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        project, instance, life = await _owned_dev(session, actor, conn, instance_id)
        _require_creator(actor, instance)
        new = await _replace_trust(session, actor, project, life, settings)
        return {"instance_id": instance_id, "trust_expires_at": new.expires_at.isoformat()}


async def resume(factory, actor, conn, instance_id: str, settings) -> dict:
    """Unshelve with the owner's own token and start a new full grace period."""
    async with sql_mutex(factory, "build-api:instance:" + instance_id) as session:
        project, instance, life = await _owned_dev(session, actor, conn, instance_id)
        if not instance.server_id:
            raise HTTPException(409, "Instance has no provider server to resume")
        try:
            machine = SessionMachine.from_state(life.machine_json)
        except ValueError:
            raise HTTPException(409, "Instance lifecycle state is invalid; contact an operator") from None
        provider = NovaShelveProvider(conn)
        try:
            status = await provider.server_status(instance.server_id)
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from None
        if machine.phase is Phase.SHELVING:
            # The worker's shelve is unfinished or its outcome is unknown: settle it from Nova here, so
            # an owner is never locked out by a worker that cannot (for example an expired trust).
            if status.startswith("SHELVING"):
                raise HTTPException(409, "A shelve is still in flight; retry shortly")
            machine.shelve_settled(Claim(machine.generation), time.time(), ok=status.startswith("SHELVED"))
        try:
            await provider.unshelve(instance.server_id)
        except ProviderError as exc:
            # A server that is not unshelvable is the caller's state conflict; Nova failures are 502.
            raise HTTPException(409 if str(exc).startswith("server is ") else 502, str(exc)) from None
        if machine.phase is Phase.SHELVED:
            machine.unshelved()
        elif not machine.sessions:
            machine.phase = Phase.GRACE
        # Boot time must not count as idle time: the first fresh report after boot starts the timer.
        if not machine.sessions:
            machine.grace_started = None
        life.reported_sessions_json, life.report_received_at = None, None
        life.machine_json = machine.to_state()
        life.updated_at = _now()
        if instance.status == "shelved":
            # Only the base VM comes back: the runner does not restart the layer overlay or command.
            instance.status = "resumed"
        instance.provider_status = "ACTIVE"
        instance.updated_at = _now()
        await session.commit()
        await _refence(session, instance_id)
        projected = instance_service.projection(instance)
        if actor["user_id"] == instance.created_by:
            try:
                await _replace_trust(session, actor, project, life, settings)
            except HTTPException:
                life.last_error = "trust_renew_failed"  # The resume itself already succeeded.
                await session.commit()
        return projected


def _address_entries(server) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    addresses = instance_service.value(server, "addresses", {}) or {}
    if not isinstance(addresses, dict):
        return result
    for network, items in addresses.items():
        for item in items or []:
            if not isinstance(item, dict) or not isinstance(item.get("addr"), str):
                continue
            version = item.get("version")
            kind = item.get("OS-EXT-IPS:type")
            result.append(
                {
                    "network": str(network),
                    "version": version if version in (4, 6) else None,
                    "address": item["addr"],
                    "type": kind if kind in ("fixed", "floating") else None,
                }
            )
    return result


async def connection_info(factory, actor, conn, instance_id: str, settings) -> dict:
    """Addresses and key name only. The Hub does not know the guest user or its host keys."""
    project = instance_service.request_project(actor, conn)
    async with factory() as session:
        instance = await instance_service.owned(session, instance_id, project, actor["user_id"])
        server_id, status, provider_status = instance.server_id, instance.status, instance.provider_status
        key_name = instance.spec_json.get("key_name")
    addresses: list[dict[str, Any]] = []
    if server_id:
        try:
            server = await run_blocking(conn.compute.get_server, server_id)
        except Exception:
            raise HTTPException(502, "Provider instance observation unavailable") from None
        addresses = _address_entries(server)
    return {
        "instance_id": instance_id,
        "server_id": server_id,
        "status": status,
        "provider_status": provider_status,
        "addresses": addresses,
        "ssh": {"port": 22, "key_name": key_name, "user": None, "host_keys": None},
    }


__all__ = [
    "ReportObserver",
    "TrustShelveProvider",
    "admit_trust",
    "bind_capability",
    "connection_info",
    "discard_trust",
    "ingest_report",
    "new_capability",
    "new_row",
    "renew_trust",
    "resume",
    "retire",
    "run_session_lifecycle",
    "sweep_retired",
    "tick_all",
    "tick_instance",
]
