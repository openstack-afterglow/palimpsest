from __future__ import annotations

import asyncio

import pytest

from palimpsest_hub.services.session_controller import ProviderError
from palimpsest_hub.services.shelve_delegation import ShelveDelegation, ShelveTrust

GRACE = 300.0
SERVER = "srv-1"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RecordingProvider:
    calls: list = []

    def __init__(self, connection):
        self._connection = connection

    async def shelve(self, instance_id):
        RecordingProvider.calls.append(("shelve", instance_id, self._connection))

    async def unshelve(self, instance_id):
        RecordingProvider.calls.append(("unshelve", instance_id, self._connection))


def _make(clock, *, open_connection=None, delete_trust=None):
    opened = []
    deleted = []

    def default_open(trust):
        opened.append(trust)
        return f"conn-for-{trust.trust_id}"

    delegation = ShelveDelegation(
        server_id=SERVER,
        grace_seconds=GRACE,
        clock=clock,
        open_connection=open_connection or default_open,
        delete_trust=delete_trust or (lambda t: deleted.append(t.trust_id)),
        provider_for=RecordingProvider,
    )
    return delegation, opened, deleted


def _user_trust(clock, *, server=SERVER, ttl=None):
    def create(server_id, requested_ttl):
        return ShelveTrust(
            trust_id=f"t-{int(clock.now)}",
            trustor_user_id="user-1",
            server_id=server,
            expires_at=clock.now + (requested_ttl if ttl is None else ttl),
        )

    return create


def test_one_server_limit_is_enforced_before_any_connection_is_opened():
    clock = Clock()
    delegation, opened, _ = _make(clock)
    delegation.renew_on_user_request(_user_trust(clock))
    with pytest.raises(ProviderError, match="outside"):
        asyncio.run(delegation.shelve("other-server"))
    assert opened == []


def test_shelve_without_a_trust_fails_closed_and_opens_nothing():
    clock = Clock()
    delegation, opened, _ = _make(clock)
    with pytest.raises(ProviderError, match="missing or expired"):
        asyncio.run(delegation.shelve(SERVER))
    assert opened == []


def test_expired_trust_fails_closed_and_is_not_renewed_by_the_idle_path():
    clock = Clock()
    delegation, opened, _ = _make(clock)
    delegation.renew_on_user_request(_user_trust(clock))
    clock.now += GRACE
    with pytest.raises(ProviderError, match="missing or expired"):
        asyncio.run(delegation.shelve(SERVER))
    assert opened == []


def test_valid_trust_is_used_for_exactly_the_bound_server():
    clock = Clock()
    delegation, opened, _ = _make(clock)
    trust = delegation.renew_on_user_request(_user_trust(clock))
    RecordingProvider.calls = []
    asyncio.run(delegation.shelve(SERVER))
    assert opened == [trust]
    assert RecordingProvider.calls == [("shelve", SERVER, f"conn-for-{trust.trust_id}")]


def test_renewal_replaces_and_deletes_the_previous_trust_and_respects_the_ttl():
    clock = Clock()
    delegation, _, deleted = _make(clock)
    first = delegation.renew_on_user_request(_user_trust(clock))
    clock.now += 10
    second = delegation.renew_on_user_request(_user_trust(clock))
    assert deleted == [first.trust_id]
    assert second.expires_at - clock.now == GRACE


def test_a_trust_longer_than_the_ttl_or_for_another_server_is_refused():
    clock = Clock()
    delegation, _, _ = _make(clock)
    with pytest.raises(ProviderError):
        delegation.renew_on_user_request(_user_trust(clock, ttl=GRACE * 10))
    with pytest.raises(ProviderError):
        delegation.renew_on_user_request(_user_trust(clock, server="other"))


def test_unopenable_trust_is_a_refusal_without_leaking_the_cause():
    clock = Clock()

    def broken(_trust):
        raise RuntimeError("keystone said SECRET-VALUE")

    delegation, _, _ = _make(clock, open_connection=broken)
    delegation.renew_on_user_request(_user_trust(clock))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(delegation.shelve(SERVER))
    assert "SECRET-VALUE" not in str(caught.value)


def test_retire_deletes_the_trust_and_refuses_further_shelve():
    clock = Clock()
    delegation, opened, deleted = _make(clock)
    trust = delegation.renew_on_user_request(_user_trust(clock))
    delegation.retire()
    assert deleted == [trust.trust_id]
    with pytest.raises(ProviderError):
        asyncio.run(delegation.shelve(SERVER))
    assert opened == []


def test_a_rejected_trust_is_deleted_instead_of_left_valid():
    clock = Clock()
    delegation, _, deleted = _make(clock)
    with pytest.raises(ProviderError):
        delegation.renew_on_user_request(_user_trust(clock, server="other"))
    assert deleted == ["t-1000"]
