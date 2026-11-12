# ruff: noqa: F811
"""Dev-instance idle auto-shelve: durable state, guest reports, trust admission and the worker tick.

Real SQL (SQLite) and real instance API through the `instance_hub` fixture; the cloud, Keystone
trust and Nova shelve provider are fakes at their boundaries. Nothing here qualifies a real guest,
Keystone trust or Nova offload.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_instances import PROJECT, USER, advance_instance, create_and_advance, instance_hub, request_body  # noqa: F401

from palimpsest_hub.instance_lifecycle_models import PalimpsestHubInstanceLifecycle as Lifecycle
from palimpsest_hub.instance_models import PalimpsestHubInstance
from palimpsest_hub.openstack import ExportDelegation
from palimpsest_hub.services import instance_lifecycle as lifecycle
from palimpsest_hub.services import instances as service
from palimpsest_hub.services.session_controller import ProviderError
from palimpsest_hub.services.session_lifecycle import GRACE_SECONDS, Phase, SessionMachine

TOKEN = "guest-session-capability-" + "x" * 24
NO_SESSIONS = "PALIMPSEST-UTMP-OK\nPALIMPSEST-SELF 100\n100 1\n1 0\nPALIMPSEST-END\n"
ONE_SESSION = (
    "PALIMPSEST-UTMP-OK\nalice pts/0 2030-01-01 00:00 (10.0.0.2)\n"
    "PALIMPSEST-SELF 100\n100 1\n200 1 sshd: alice@pts/0\n1 0\nPALIMPSEST-END\n"
)


def delegation(expires_in=timedelta(days=7)):
    return ExportDelegation(
        trust_id="trust-" + uuid4().hex,
        project_id=PROJECT,
        trustor_user_id=USER,
        trustee_user_id="e" * 32,
        role_names=("member",),
        expires_at=datetime.now(UTC) + expires_in,
    )


class FakeProvider:
    """Records Nova effects; `status` is what `server_status` reports."""

    def __init__(self, *, status="ACTIVE", shelve_error=None, crash=None):
        self.calls: list[tuple[str, str]] = []
        self.status = status
        self.shelve_error = shelve_error
        self.crash = crash

    async def shelve(self, instance_id):
        self.calls.append(("shelve", instance_id))
        if self.crash is not None:
            raise self.crash
        if self.shelve_error is not None:
            raise self.shelve_error
        self.status = "SHELVED_OFFLOADED"

    async def unshelve(self, instance_id):
        self.calls.append(("unshelve", instance_id))
        self.status = "ACTIVE"

    async def server_status(self, instance_id):
        self.calls.append(("status", instance_id))
        return self.status


@pytest.fixture
def trust_boundary(monkeypatch):
    """Fake Keystone trust creation/deletion; records every trust created and discarded."""
    record = SimpleNamespace(created=[], discarded=[], revoked=[], create_error=None)

    async def admit(actor, project, settings):
        if record.create_error is not None:
            raise record.create_error
        item = delegation()
        record.created.append(item)
        return item

    async def discard(actor, item):
        record.discarded.append(item.trust_id)
        return True

    def revoke(item):
        record.revoked.append(item.trust_id)
        return True

    monkeypatch.setattr(lifecycle, "admit_trust", admit)
    monkeypatch.setattr(lifecycle, "discard_trust", discard)
    monkeypatch.setattr(lifecycle, "_revoke_with_trust_token", revoke)
    return record


def sdk_servers(h):
    """Expose the dict-based fake servers as SDK-style objects (NovaShelveProvider reads attributes)."""
    original = h.provider.get_server

    def get_server(server_id):
        found = original(server_id)
        return None if found is None else SimpleNamespace(**found)

    h.provider.get_server = get_server


def lifecycle_settings(h):
    h.settings.palimpsest_hub_session_report_interval_seconds = 30
    h.settings.palimpsest_hub_session_report_max_age_seconds = 120
    h.settings.palimpsest_hub_session_delegated_roles = ("member",)
    h.settings.palimpsest_hub_session_delegation_ttl_seconds = 604800
    return h.settings


async def make_dev(h, *, bind=True, **overrides):
    """Create and advance a real dev instance; by default bind a known guest capability."""
    lifecycle_settings(h)
    response = await create_and_advance(h, json=request_body(lifecycle="dev", **overrides))
    assert response.status_code == 202, response.text
    instance_id = response.json()["id"]
    if bind:
        async with h.factory() as session:
            row = await session.get(Lifecycle, instance_id)
            row.session_capability_hash = hashlib.sha256(TOKEN.encode()).hexdigest()
            await session.commit()
    return instance_id


async def set_instance(h, instance_id, **values):
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, instance_id)
        for key, item in values.items():
            setattr(row, key, item)
        await session.commit()


async def set_life(h, instance_id, **values):
    async with h.factory() as session:
        row = await session.get(Lifecycle, instance_id)
        for key, item in values.items():
            setattr(row, key, item)
        await session.commit()


async def life_state(h, instance_id):
    async with h.factory() as session:
        row = await session.get(Lifecycle, instance_id)
        instance = await session.get(PalimpsestHubInstance, instance_id)
        return SimpleNamespace(
            machine=SessionMachine.from_state(row.machine_json),
            life=row,
            status=instance.status,
            provider_status=instance.provider_status,
        )


async def report(h, instance_id, text=NO_SESSIONS, *, age=5):
    await lifecycle.ingest_report(h.factory, instance_id, TOKEN, text)
    if age != 0:
        await set_life(h, instance_id, report_received_at=datetime.now(UTC) - timedelta(seconds=age))


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


async def tick(h, instance_id, provider, clock):
    return await lifecycle.tick_instance(
        h.factory,
        lifecycle_settings(h),
        instance_id,
        clock=clock,
        provider_factory=lambda *args, **kwargs: provider,
    )


# -- creation -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_create_admits_a_trust_binds_the_capability_and_installs_the_reporter(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h, key_name="laptop")
    assert len(trust_boundary.created) == 1 and trust_boundary.discarded == []
    state = await life_state(h, instance_id)
    assert state.life.trust_json["trust_id"] == trust_boundary.created[0].trust_id
    assert state.life.trust_expires_at is not None and not state.life.retired
    assert state.machine.phase is Phase.GRACE and state.machine.grace_started is None
    sent = h.provider.creates[-1]
    assert sent["key_name"] == "laptop"
    # The reporter is installed from cloud-init and the raw capability never reaches the database.
    assert b"session_reporter.py" in __import__("base64").b64decode(sent["user_data"])
    assert TOKEN not in str(state.life.machine_json) and state.life.session_capability_hash


@pytest.mark.asyncio
async def test_service_instances_get_no_lifecycle_row_trust_or_reporter(instance_hub, trust_boundary):
    h = instance_hub
    response = await create_and_advance(h, json=request_body())
    assert response.status_code == 202, response.text
    async with h.factory() as session:
        assert (await session.scalars(select(Lifecycle))).all() == []
    assert trust_boundary.created == []
    assert "key_name" not in h.provider.creates[-1]
    assert b"session_reporter.py" not in __import__("base64").b64decode(h.provider.creates[-1]["user_data"])
    assert response.json()["lifecycle"] == "service"


@pytest.mark.asyncio
async def test_dev_create_that_cannot_delegate_allocates_nothing(instance_hub, trust_boundary):
    h = instance_hub
    lifecycle_settings(h)
    trust_boundary.create_error = HTTPException(403, "The requester does not currently hold the delegated role")
    response = await h.client.post("/v1/instances", json=request_body(lifecycle="dev"))
    assert response.status_code == 403
    assert h.provider.volume_creates == [] and h.provider.creates == []
    async with h.factory() as session:
        assert (await session.scalars(select(PalimpsestHubInstance))).all() == []


@pytest.mark.asyncio
async def test_trust_is_discarded_when_the_durable_intent_cannot_be_written(instance_hub, trust_boundary, monkeypatch):
    h = instance_hub
    lifecycle_settings(h)

    async def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(service, "retain_blob", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await h.client.post("/v1/instances", json=request_body(lifecycle="dev"))
    assert len(trust_boundary.created) == 1
    assert trust_boundary.discarded == [trust_boundary.created[0].trust_id]


@pytest.mark.asyncio
async def test_lifecycle_defaults_do_not_change_an_existing_request_digest(instance_hub, trust_boundary):
    h = instance_hub
    body = request_body()
    first = await create_and_advance(h, json=body)
    replay = await h.client.post("/v1/instances", json={**body, "lifecycle": "service", "key_name": None})
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    changed = await h.client.post("/v1/instances", json={**body, "lifecycle": "dev"})
    assert changed.status_code == 409


# -- guest report -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_requires_the_exact_capability_and_stores_only_parsed_sessions(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    for wrong in ("", "wrong", TOKEN + "x"):
        with pytest.raises(HTTPException) as denied:
            await lifecycle.ingest_report(h.factory, instance_id, wrong, NO_SESSIONS)
        assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as unknown:
        await lifecycle.ingest_report(h.factory, str(uuid4()), TOKEN, NO_SESSIONS)
    assert unknown.value.status_code == 404
    await lifecycle.ingest_report(h.factory, instance_id, TOKEN, ONE_SESSION)
    state = await life_state(h, instance_id)
    assert state.life.reported_sessions_json == [
        "alice pts/0 2030-01-01 00:00 (10.0.0.2)",
        "sshd: alice@pts/0 (pid 200)",
    ]
    assert state.life.report_received_at is not None


@pytest.mark.asyncio
async def test_an_untrustworthy_report_is_rejected_and_keeps_the_previous_receipt(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await report(h, instance_id, ONE_SESSION, age=40)
    before = (await life_state(h, instance_id)).life.report_received_at
    for text in ("garbage", NO_SESSIONS.replace("PALIMPSEST-END\n", "")):
        with pytest.raises(HTTPException) as bad:
            await lifecycle.ingest_report(h.factory, instance_id, TOKEN, text)
        assert bad.value.status_code == 422
    after = await life_state(h, instance_id)
    assert after.life.report_received_at == before
    assert after.life.reported_sessions_json != []


@pytest.mark.asyncio
async def test_a_retired_instance_no_longer_accepts_reports(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_life(h, instance_id, retired=True)
    with pytest.raises(HTTPException) as denied:
        await lifecycle.ingest_report(h.factory, instance_id, TOKEN, NO_SESSIONS)
    assert denied.value.status_code == 404


# -- worker tick --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_time_never_counts_as_idle_and_the_first_fresh_report_starts_the_timer(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    # No report yet: unknown, nothing starts.
    assert await tick(h, instance_id, provider, clock) == "observation-failed"
    assert (await life_state(h, instance_id)).machine.grace_started is None
    clock.now += 10_000
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "noop"
    assert (await life_state(h, instance_id)).machine.grace_started == clock.now
    clock.now += GRACE_SECONDS - 1
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "noop"
    assert [call for call in provider.calls if call[0] == "shelve"] == []


@pytest.mark.asyncio
async def test_an_idle_ready_instance_is_shelved_once_after_the_full_grace_period(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "shelved"
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.SHELVED
    assert state.status == "shelved" and state.provider_status == "SHELVED_OFFLOADED"
    # A shelved instance is never touched again by the worker.
    assert await tick(h, instance_id, provider, clock) == "shelved"
    assert [call[0] for call in provider.calls].count("shelve") == 1


@pytest.mark.asyncio
async def test_an_open_session_blocks_shelve_and_leaving_restarts_the_full_grace(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id, ONE_SESSION)
    clock.now += 10 * GRACE_SECONDS
    assert await tick(h, instance_id, provider, clock) == "noop"
    assert (await life_state(h, instance_id)).machine.phase is Phase.ACTIVE
    await report(h, instance_id, NO_SESSIONS)
    assert await tick(h, instance_id, provider, clock) == "noop"
    left = (await life_state(h, instance_id)).machine.grace_started
    assert left == clock.now
    clock.now += GRACE_SECONDS - 1
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "noop"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_stale_report_is_unknown_not_idle(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += 100 * GRACE_SECONDS
    await report(h, instance_id, age=3600)
    assert await tick(h, instance_id, provider, clock) == "observation-failed"
    state = await life_state(h, instance_id)
    assert state.life.last_error == "report_stale" and state.machine.phase is Phase.GRACE
    assert provider.calls == []


@pytest.mark.asyncio
async def test_time_without_a_usable_report_is_not_idle_time(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += 100 * GRACE_SECONDS
    await report(h, instance_id, age=3600)
    await tick(h, instance_id, provider, clock)  # stale: the timer is cleared, not left running
    assert (await life_state(h, instance_id)).machine.grace_started is None
    await report(h, instance_id)  # the first fresh empty report only restarts the full grace period
    assert await tick(h, instance_id, provider, clock) == "noop"
    assert provider.calls == []
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "shelved"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["booting", "uncertain", "stopped", "deleting", "deleted"])
async def test_only_a_ready_or_error_guest_is_ever_shelved(instance_hub, trust_boundary, status):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status=status)
    await report(h, instance_id)
    provider, clock = FakeProvider(), Clock()
    await tick(h, instance_id, provider, clock)
    clock.now += 100 * GRACE_SECONDS
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) in {"not-shelvable", "skipped"}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_an_expired_or_missing_trust_fails_closed_and_keeps_the_instance_running(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += 100 * GRACE_SECONDS
    await report(h, instance_id)
    await set_life(h, instance_id, trust_expires_at=datetime.now(UTC) + timedelta(seconds=30))
    assert await tick(h, instance_id, provider, clock) == "trust-unusable"
    assert (await life_state(h, instance_id)).life.last_error == "trust_expired"
    await set_life(h, instance_id, trust_expires_at=datetime.now(UTC) + timedelta(days=1), trust_json=None)
    assert await tick(h, instance_id, provider, clock) == "trust-unusable"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_failed_shelve_keeps_the_instance_and_restarts_the_full_grace(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(shelve_error=ProviderError("nova refused")), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "shelve-failed"
    state = await life_state(h, instance_id)
    assert state.status == "ready" and state.life.last_error == "shelve_failed"
    assert state.machine.phase is Phase.GRACE and state.machine.grace_started == clock.now
    clock.now += GRACE_SECONDS - 1
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    assert [call[0] for call in provider.calls].count("shelve") == 1


@pytest.mark.asyncio
async def test_a_crash_during_the_nova_call_leaves_shelving_and_the_next_tick_settles_from_nova(
    instance_hub, trust_boundary
):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    crashing, clock = FakeProvider(crash=RuntimeError("worker killed")), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, crashing, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    with pytest.raises(RuntimeError, match="worker killed"):
        await tick(h, instance_id, crashing, clock)
    assert (await life_state(h, instance_id)).machine.phase is Phase.SHELVING
    # Nova finished the offload while the worker was gone: settle, never shelve a second time.
    recovered = FakeProvider(status="SHELVED_OFFLOADED")
    assert await tick(h, instance_id, recovered, clock) == "recovered-shelved"
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.SHELVED and state.status == "shelved"
    assert [call[0] for call in recovered.calls] == ["status"]


@pytest.mark.asyncio
async def test_a_shelve_that_never_started_is_released_with_a_fresh_grace(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    crashing, clock = FakeProvider(crash=RuntimeError("worker killed")), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, crashing, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    with pytest.raises(RuntimeError):
        await tick(h, instance_id, crashing, clock)
    clock.now += 5
    assert await tick(h, instance_id, FakeProvider(status="ACTIVE"), clock) == "recovered-active"
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.GRACE and state.machine.grace_started == clock.now
    # Nova still working: look again later, change nothing.
    await set_life(h, instance_id, machine_json={**state.machine.to_state(), "phase": "shelving"})
    assert await tick(h, instance_id, FakeProvider(status="SHELVING"), clock) == "shelve-in-progress"
    assert (await life_state(h, instance_id)).machine.phase is Phase.SHELVING


@pytest.mark.asyncio
async def test_a_corrupt_machine_state_fails_closed(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    await set_life(h, instance_id, machine_json={"phase": "nonsense"})
    provider = FakeProvider()
    assert await tick(h, instance_id, provider, Clock()) == "state-invalid"
    assert provider.calls == [] and (await life_state_raw(h, instance_id)).last_error == "state_invalid"


async def life_state_raw(h, instance_id):
    async with h.factory() as session:
        return await session.get(Lifecycle, instance_id)


@pytest.mark.asyncio
async def test_tick_all_isolates_one_failing_instance_and_sweeps_retired_trusts(instance_hub, trust_boundary):
    h = instance_hub
    first = await make_dev(h)
    second = await make_dev(h)
    await set_instance(h, first, status="ready")
    await set_life(h, second, retired=True)

    calls = []

    async def fake_tick(factory, settings, instance_id, **kwargs):
        calls.append(instance_id)
        raise RuntimeError("boom " + instance_id)

    counts = await lifecycle.tick_all(h.factory, lifecycle_settings(h), tick=fake_tick)
    assert calls == [first] and counts["error"] == 1 and counts["revoked"] == 1
    assert trust_boundary.revoked == [trust_boundary.created[1].trust_id]
    assert (await life_state_raw(h, second)).trust_json is None


@pytest.mark.asyncio
async def test_a_trust_that_cannot_be_revoked_stays_pending_for_the_next_sweep(
    instance_hub, trust_boundary, monkeypatch
):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_life(h, instance_id, retired=True)
    monkeypatch.setattr(lifecycle, "_revoke_with_trust_token", lambda item: False)
    assert await lifecycle.sweep_retired(h.factory, lifecycle_settings(h)) == 0
    row = await life_state_raw(h, instance_id)
    assert row.trust_json is not None and row.last_error == "trust_revoke_pending"


# -- deletion, stop and resume -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_dev_instance_retires_its_lifecycle_and_blocks_stop_while_shelved(
    instance_hub, trust_boundary
):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="shelved", provider_status="SHELVED_OFFLOADED")
    stop = await h.client.post(f"/v1/instances/{instance_id}/stop")
    assert stop.status_code == 409 and "shelved" in stop.json()["detail"]
    assert h.provider.stops == []
    deleted = await h.client.delete(f"/v1/instances/{instance_id}")
    assert deleted.status_code == 202, deleted.text
    assert (await life_state_raw(h, instance_id)).retired is True


@pytest.mark.asyncio
async def test_resume_unshelves_with_the_owners_connection_and_starts_a_new_grace(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    async with h.factory() as session:
        row = await session.get(PalimpsestHubInstance, instance_id)
        server_id = row.server_id
    machine = SessionMachine(phase=Phase.SHELVED, grace_started=None)
    await set_life(h, instance_id, machine_json=machine.to_state())
    await set_instance(h, instance_id, status="shelved", provider_status="SHELVED_OFFLOADED")
    unshelved = []
    sdk_servers(h)
    h.provider.servers_by_id[server_id]["status"] = "SHELVED_OFFLOADED"

    def unshelve_server(identity):
        unshelved.append(identity)
        h.provider.servers_by_id[identity]["status"] = "ACTIVE"

    h.provider.unshelve_server = unshelve_server
    actor = {"project_id": PROJECT, "user_id": USER, "package_actions": {"packages:manage"}}
    projected = await lifecycle.resume(h.factory, actor, h.provider, instance_id, lifecycle_settings(h))
    assert unshelved == [server_id] and projected["status"] == "resumed"
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.GRACE and state.machine.grace_started is None
    # Boot time is not idle time: the first fresh report after boot starts the timer, and the old
    # pre-shelve report cannot be mistaken for a fresh one.
    assert state.life.report_received_at is None and state.life.reported_sessions_json is None
    assert state.status == "resumed" and state.provider_status == "ACTIVE"
    assert len(trust_boundary.created) == 2  # the resume renewed the owner's trust
    assert trust_boundary.revoked == [trust_boundary.created[0].trust_id]


@pytest.mark.asyncio
async def test_a_resume_whose_trust_renewal_fails_still_succeeds_and_records_it(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    async with h.factory() as session:
        server_id = (await session.get(PalimpsestHubInstance, instance_id)).server_id
    await set_life(h, instance_id, machine_json=SessionMachine(phase=Phase.SHELVED).to_state())
    await set_instance(h, instance_id, status="shelved")
    sdk_servers(h)
    h.provider.servers_by_id[server_id]["status"] = "SHELVED_OFFLOADED"
    h.provider.unshelve_server = lambda identity: h.provider.servers_by_id[identity].update(status="ACTIVE")
    trust_boundary.create_error = HTTPException(503, "Keystone unavailable")
    actor = {"project_id": PROJECT, "user_id": USER, "package_actions": {"packages:manage"}}
    projected = await lifecycle.resume(h.factory, actor, h.provider, instance_id, lifecycle_settings(h))
    assert projected["status"] == "resumed"
    assert (await life_state_raw(h, instance_id)).last_error == "trust_renew_failed"


@pytest.mark.asyncio
async def test_resume_and_renew_require_a_dev_instance(instance_hub, trust_boundary):
    h = instance_hub
    response = await create_and_advance(h, json=request_body())
    actor = {"project_id": PROJECT, "user_id": USER, "package_actions": {"packages:manage"}}
    for call in (lifecycle.resume, lifecycle.renew_trust):
        with pytest.raises(HTTPException) as denied:
            await call(h.factory, actor, h.provider, response.json()["id"], lifecycle_settings(h))
        assert denied.value.status_code == 409


@pytest.mark.asyncio
async def test_a_manager_who_is_not_the_creator_can_resume_but_cannot_replace_the_trust(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    async with h.factory() as session:
        server_id = (await session.get(PalimpsestHubInstance, instance_id)).server_id
    await set_life(h, instance_id, machine_json=SessionMachine(phase=Phase.SHELVED).to_state())
    await set_instance(h, instance_id, status="shelved")
    sdk_servers(h)
    h.provider.servers_by_id[server_id]["status"] = "SHELVED_OFFLOADED"
    h.provider.unshelve_server = lambda identity: h.provider.servers_by_id[identity].update(status="ACTIVE")
    manager = {"project_id": PROJECT, "user_id": "f" * 32, "package_actions": {"packages:manage"}}
    h.provider._afterglow_user_id = manager["user_id"]  # the manager's own connection
    projected = await lifecycle.resume(h.factory, manager, h.provider, instance_id, lifecycle_settings(h))
    assert projected["status"] == "resumed" and len(trust_boundary.created) == 1
    with pytest.raises(HTTPException) as denied:
        await lifecycle.renew_trust(h.factory, manager, h.provider, instance_id, lifecycle_settings(h))
    assert denied.value.status_code == 403 and len(trust_boundary.created) == 1


@pytest.mark.asyncio
async def test_resume_of_a_server_that_cannot_be_unshelved_is_a_conflict_not_a_provider_outage(
    instance_hub, trust_boundary
):
    h = instance_hub
    instance_id = await make_dev(h)
    async with h.factory() as session:
        server_id = (await session.get(PalimpsestHubInstance, instance_id)).server_id
    sdk_servers(h)
    h.provider.servers_by_id[server_id]["status"] = "SHUTOFF"
    actor = {"project_id": PROJECT, "user_id": USER, "package_actions": {"packages:manage"}}
    with pytest.raises(HTTPException) as refused:
        await lifecycle.resume(h.factory, actor, h.provider, instance_id, lifecycle_settings(h))
    assert refused.value.status_code == 409


async def _in_flight(h, nova_status):
    instance_id = await make_dev(h)
    async with h.factory() as session:
        server_id = (await session.get(PalimpsestHubInstance, instance_id)).server_id
    await set_life(h, instance_id, machine_json=SessionMachine(phase=Phase.SHELVING).to_state())
    sdk_servers(h)
    h.provider.servers_by_id[server_id]["status"] = nova_status
    unshelved = []

    def unshelve_server(identity):
        unshelved.append(identity)
        h.provider.servers_by_id[identity]["status"] = "ACTIVE"

    h.provider.unshelve_server = unshelve_server
    return instance_id, unshelved


ACTOR = {"project_id": PROJECT, "user_id": USER, "package_actions": {"packages:manage"}}


@pytest.mark.asyncio
async def test_resume_refuses_while_nova_is_still_shelving(instance_hub, trust_boundary):
    h = instance_hub
    instance_id, unshelved = await _in_flight(h, "SHELVING")
    with pytest.raises(HTTPException) as busy:
        await lifecycle.resume(h.factory, ACTOR, h.provider, instance_id, lifecycle_settings(h))
    assert busy.value.status_code == 409 and unshelved == []
    # The refusal must not settle the worker's shelve: the machine is still SHELVING.
    assert (await life_state(h, instance_id)).machine.phase is Phase.SHELVING


@pytest.mark.asyncio
@pytest.mark.parametrize(("nova_status", "unshelve_calls"), [("SHELVED_OFFLOADED", 1), ("SHELVED", 1), ("ACTIVE", 0)])
async def test_resume_settles_an_unfinished_shelve_itself_so_the_owner_is_never_locked_out(
    instance_hub, trust_boundary, nova_status, unshelve_calls
):
    h = instance_hub
    instance_id, unshelved = await _in_flight(h, nova_status)
    projected = await lifecycle.resume(h.factory, ACTOR, h.provider, instance_id, lifecycle_settings(h))
    assert len(unshelved) == unshelve_calls
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.GRACE and projected["provider_status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_resume_replaces_the_stored_trust_with_the_new_one(instance_hub, trust_boundary):
    h = instance_hub
    instance_id, _ = await _in_flight(h, "SHELVED_OFFLOADED")
    await lifecycle.resume(h.factory, ACTOR, h.provider, instance_id, lifecycle_settings(h))
    stored = (await life_state_raw(h, instance_id)).trust_json["trust_id"]
    assert stored == trust_boundary.created[1].trust_id != trust_boundary.created[0].trust_id


@pytest.mark.asyncio
async def test_connection_info_reports_addresses_and_key_name_but_not_what_the_hub_cannot_know(
    instance_hub, trust_boundary
):
    h = instance_hub
    instance_id = await make_dev(h, key_name="laptop")
    async with h.factory() as session:
        server_id = (await session.get(PalimpsestHubInstance, instance_id)).server_id
    h.provider.servers_by_id[server_id]["addresses"] = {
        "tenant": [
            {"addr": "10.0.0.5", "version": 4, "OS-EXT-IPS:type": "fixed"},
            {"addr": "203.0.113.9", "version": 4, "OS-EXT-IPS:type": "floating"},
            {"addr": 7},
        ]
    }
    actor = {"project_id": PROJECT, "user_id": USER, "package_actions": {"packages:inventory"}}
    info = await lifecycle.connection_info(h.factory, actor, h.provider, instance_id, lifecycle_settings(h))
    assert info["addresses"] == [
        {"network": "tenant", "version": 4, "address": "10.0.0.5", "type": "fixed"},
        {"network": "tenant", "version": 4, "address": "203.0.113.9", "type": "floating"},
    ]
    assert info["ssh"] == {"port": 22, "key_name": "laptop", "user": None, "host_keys": None}
    assert info["server_id"] == server_id


@pytest.mark.asyncio
async def test_the_worker_pass_runs_under_one_exclusive_role(instance_hub, trust_boundary):
    from palimpsest_hub.services.worker_leases import role_lease

    h = instance_hub
    settings = lifecycle_settings(h)
    assert await lifecycle.run_session_lifecycle(settings, h.factory) == {"revoked": 0}
    async with role_lease(h.factory, "session-lifecycle", ttl_seconds=60) as held:
        assert held is not None
        # Another worker replica finds the role busy and does nothing, so a shelve is never doubled.
        assert await lifecycle.run_session_lifecycle(settings, h.factory) is None


@pytest.mark.asyncio
async def test_service_instances_delete_even_before_the_ledger_table_exists(instance_hub, trust_boundary):
    """A deploy that has not yet bootstrapped the new table must not break existing instances."""
    from sqlalchemy import text

    h = instance_hub
    response = await create_and_advance(h, json=request_body())
    async with h.factory() as session:
        await session.execute(text("DROP TABLE palimpsest_hub_instance_lifecycle"))
        await session.commit()
    deleted = await h.client.delete(f"/v1/instances/{response.json()['id']}")
    assert deleted.status_code == 202, deleted.text


REAL_ADMIT_TRUST = lifecycle.admit_trust


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ready", "error", "resumed"])
async def test_ready_error_and_resumed_guests_are_all_shelvable(instance_hub, trust_boundary, status):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status=status)
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "shelved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("nova_status", "label", "phase"),
    [
        ("SHELVED", "recovered-shelved", Phase.SHELVED),
        ("SHUTOFF", "recovered-not-shelved", Phase.GRACE),
        ("ERROR", "recovered-not-shelved", Phase.GRACE),
    ],
)
async def test_every_nova_status_settles_an_unknown_shelve_so_it_never_stays_stuck(
    instance_hub, trust_boundary, nova_status, label, phase
):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    await set_life(h, instance_id, machine_json=SessionMachine(phase=Phase.SHELVING).to_state())
    assert await tick(h, instance_id, FakeProvider(status=nova_status), Clock()) == label
    assert (await life_state(h, instance_id)).machine.phase is phase


@pytest.mark.asyncio
async def test_a_shelve_that_nova_accepted_but_the_worker_could_not_confirm_stays_shelving(
    instance_hub, trust_boundary
):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(shelve_error=ProviderError("timed out waiting for SHELVED_OFFLOADED")), Clock()
    provider.status = "SHELVING"
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    assert await tick(h, instance_id, provider, clock) == "shelve-failed"
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.SHELVING and state.life.last_error == "shelve_unconfirmed"
    assert state.status == "ready"
    # The next tick settles from Nova instead of leaving a half-shelved instance looking ready.
    provider.status = "SHELVED_OFFLOADED"
    assert await tick(h, instance_id, provider, clock) == "recovered-shelved"


@pytest.mark.asyncio
async def test_a_delete_that_lands_after_the_claim_cancels_the_shelve(instance_hub, trust_boundary, monkeypatch):
    from sqlalchemy import update

    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    original = lifecycle._refence

    async def delete_lands_first(session, identity):
        await original(session, identity)
        await session.execute(update(Lifecycle).where(Lifecycle.instance_id == identity).values(retired=True))
        await session.execute(
            update(PalimpsestHubInstance).where(PalimpsestHubInstance.id == identity).values(status="deleting")
        )

    monkeypatch.setattr(lifecycle, "_refence", delete_lands_first)
    assert await tick(h, instance_id, provider, clock) == "claim-cancelled"
    assert [call for call in provider.calls if call[0] == "shelve"] == []
    state = await life_state(h, instance_id)
    assert state.status == "deleting" and state.machine.phase is Phase.GRACE


@pytest.mark.asyncio
async def test_a_user_who_connects_between_the_claim_and_the_call_is_never_shelved(
    instance_hub, trust_boundary, monkeypatch
):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_instance(h, instance_id, status="ready")
    provider, clock = FakeProvider(), Clock()
    await report(h, instance_id)
    await tick(h, instance_id, provider, clock)
    clock.now += GRACE_SECONDS
    await report(h, instance_id)
    original = lifecycle._refence

    async def user_connects(session, identity):
        await original(session, identity)
        row = await session.get(Lifecycle, identity)
        row.reported_sessions_json = ["sshd: alice@pts/0 (pid 200)"]
        row.report_received_at = datetime.now(UTC)

    monkeypatch.setattr(lifecycle, "_refence", user_connects)
    assert await tick(h, instance_id, provider, clock) == "shelve-cancelled"
    assert [call for call in provider.calls if call[0] == "shelve"] == []


@pytest.mark.asyncio
async def test_tick_all_only_visits_shelvable_rows_and_rotates_so_none_starves(instance_hub, trust_boundary):
    h = instance_hub
    ids = [await make_dev(h) for _ in range(3)]
    parked = await make_dev(h)
    for instance_id in ids:
        await set_instance(h, instance_id, status="ready")
    await set_instance(h, parked, status="shelved")
    seen = []

    async def noop_tick(factory, settings, instance_id, **kwargs):
        seen.append(instance_id)
        async with factory() as session:
            (await session.get(Lifecycle, instance_id)).updated_at = datetime.now(UTC)
            await session.commit()
        return "noop"

    for _ in range(3):
        await lifecycle.tick_all(h.factory, lifecycle_settings(h), limit=1, tick=noop_tick)
    assert sorted(seen) == sorted(ids) and parked not in seen


@pytest.mark.asyncio
async def test_sweep_clears_an_expired_trust_and_serves_the_oldest_pending_first(
    instance_hub, trust_boundary, monkeypatch
):
    h = instance_hub
    expired, old, new = await make_dev(h), await make_dev(h), await make_dev(h)
    stamp = datetime.now(UTC)
    await set_life(h, expired, retired=True, trust_expires_at=stamp - timedelta(hours=1), updated_at=stamp)
    await set_life(h, old, retired=True, updated_at=stamp - timedelta(minutes=5))
    await set_life(h, new, retired=True, updated_at=stamp - timedelta(minutes=1))
    assert await lifecycle.sweep_retired(h.factory, lifecycle_settings(h), limit=1) == 1
    assert trust_boundary.revoked == [(await life_trust_id(h, old, trust_boundary))]
    assert (await life_state_raw(h, new)).trust_json is not None
    assert await lifecycle.sweep_retired(h.factory, lifecycle_settings(h)) == 1  # `new`; `expired` is dropped
    assert (await life_state_raw(h, expired)).trust_json is None and len(trust_boundary.revoked) == 2


async def life_trust_id(h, instance_id, trust_boundary):
    # The trust ids were handed out in creation order; find the one a revoked row held.
    for item in trust_boundary.created:
        if item.trust_id in trust_boundary.revoked:
            return item.trust_id
    return None


@pytest.mark.asyncio
async def test_the_guest_capability_in_user_data_is_the_one_whose_hash_is_stored(instance_hub, trust_boundary):
    import base64
    import json as jsonlib

    h = instance_hub
    instance_id = await make_dev(h, bind=False)
    cloud_config = base64.b64decode(h.provider.creates[-1]["user_data"]).decode().split("\n", 1)[1]
    files = {item["path"]: item for item in jsonlib.loads(cloud_config)["write_files"]}
    session_json = jsonlib.loads(base64.b64decode(files["/var/lib/palimpsest-instance/session.json"]["content"]))
    stored = (await life_state_raw(h, instance_id)).session_capability_hash
    assert stored == hashlib.sha256(session_json["capability"].encode()).hexdigest()
    spec_json = base64.b64decode(files["/var/lib/palimpsest-instance/spec.json"]["content"]).decode()
    assert session_json["capability"] not in spec_json
    # The guest can now report with exactly that capability.
    await lifecycle.ingest_report(h.factory, instance_id, session_json["capability"], NO_SESSIONS)


@pytest.mark.asyncio
async def test_an_unshelve_outside_the_hub_is_noticed_on_the_next_read(instance_hub, trust_boundary):
    h = instance_hub
    instance_id = await make_dev(h)
    await set_life(h, instance_id, machine_json=SessionMachine(phase=Phase.SHELVED).to_state())
    await set_instance(h, instance_id, status="shelved", provider_status="SHELVED_OFFLOADED")
    observed = await h.client.get(f"/v1/instances/{instance_id}")  # the fake server is ACTIVE
    assert observed.status_code == 200 and observed.json()["status"] == "resumed"
    state = await life_state(h, instance_id)
    assert state.machine.phase is Phase.GRACE and state.machine.grace_started is not None


@pytest.mark.asyncio
async def test_deleting_a_dev_instance_whose_create_never_reached_nova_still_retires_the_ledger(
    instance_hub, trust_boundary, monkeypatch
):
    from palimpsest_hub.services import instances as instance_service

    h = instance_hub
    lifecycle_settings(h)
    calls = []
    real = instance_service.preflight.require

    def second_check_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise HTTPException(409, {"code": "preflight_failed", "failures": []})
        return real(*args, **kwargs)

    monkeypatch.setattr(instance_service.preflight, "require", second_check_fails)
    created = await h.client.post("/v1/instances", json=request_body(lifecycle="dev"))
    assert created.status_code == 409
    async with h.factory() as session:
        row = (await session.scalars(select(PalimpsestHubInstance))).one()
    assert (await life_state_raw(h, row.id)).retired is False
    deleted = await h.client.delete(f"/v1/instances/{row.id}")
    assert deleted.status_code == 202, deleted.text
    assert (await life_state_raw(h, row.id)).retired is True


@pytest.mark.asyncio
async def test_a_wrong_key_name_is_refused_before_any_allocation_or_trust(instance_hub, trust_boundary):
    h = instance_hub
    lifecycle_settings(h)
    response = await h.client.post("/v1/instances", json=request_body(lifecycle="dev", key_name="no-such-key"))
    assert response.status_code == 422 and "key pair" in response.json()["detail"]
    assert h.provider.volume_creates == [] and h.provider.creates == [] and trust_boundary.created == []


def _auth_ref(token="token-1", project=PROJECT, user=USER):
    return SimpleNamespace(auth_token=token, project_id=project, user_id=user)


@pytest.mark.asyncio
async def test_admit_trust_requires_the_delegated_roles_and_the_owners_validated_token(instance_hub, monkeypatch):
    h = instance_hub
    settings = lifecycle_settings(h)
    created = []
    monkeypatch.setattr(
        lifecycle,
        "create_export_trust",
        lambda auth_ref, **kwargs: created.append(kwargs) or delegation(),
    )
    good = {"user_id": USER, "token": "token-1", "auth_ref": _auth_ref(), "current_roles": ["member"]}
    assert (await REAL_ADMIT_TRUST(good, PROJECT, settings)).trust_id
    assert created == [
        {"project_id": PROJECT, "trustor_user_id": USER, "role_names": ("member",), "ttl_seconds": 604800}
    ]
    for actor, status in (
        ({**good, "current_roles": ["reader"]}, 403),
        ({**good, "auth_ref": None}, 401),
        ({**good, "token": "another-token"}, 401),
        ({**good, "auth_ref": _auth_ref(project="b" * 32)}, 401),
        ({**good, "auth_ref": _auth_ref(user="d" * 32)}, 401),
    ):
        with pytest.raises(HTTPException) as refused:
            await REAL_ADMIT_TRUST(actor, PROJECT, settings)
        assert refused.value.status_code == status
    assert len(created) == 1
