"""Delegated shelve for one server under a short-lived Keystone trust (M4-11 CPU slice).

Keystone trusts bind trustor, trustee, project, roles and expiry, but not a single server, so
the one-server limit is enforced here before any Nova call. A trust is created or renewed only
by `renew_on_user_request`, which the caller invokes with the trustor's own authenticated
request; the idle shelve path never creates or renews a trust. An expired, missing or
unopenable trust fails closed (ProviderError, instance kept running). There is no
service-credential fallback: the only connection this module opens is the trust connection.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from palimpsest_hub.services.nova_shelve_provider import NovaShelveProvider
from palimpsest_hub.services.session_controller import ProviderError


@dataclass(frozen=True)
class ShelveTrust:
    trust_id: str
    trustor_user_id: str
    server_id: str
    expires_at: float


class ShelveDelegation:
    def __init__(
        self,
        *,
        server_id: str,
        grace_seconds: float,
        clock: Callable[[], float],
        open_connection: Callable[[ShelveTrust], Any],
        delete_trust: Callable[[ShelveTrust], None],
        provider_for: Callable[[Any], Any] = NovaShelveProvider,
    ) -> None:
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        self._server_id = server_id
        # Approved bound: a short TTL no longer than the 5-minute grace period.
        self._ttl = grace_seconds
        self._clock = clock
        self._open = open_connection
        self._delete = delete_trust
        self._provider_for = provider_for
        self._trust: ShelveTrust | None = None

    def renew_on_user_request(self, create_trust: Callable[[str, float], ShelveTrust]) -> ShelveTrust:
        """Replace the trust with one minted from the trustor's own request; the old trust is deleted."""
        created = create_trust(self._server_id, self._ttl)
        if created.server_id != self._server_id or created.expires_at > self._clock() + self._ttl:
            with suppress(Exception):
                self._delete(created)
            raise ProviderError("trust does not match the delegated server or exceeds the TTL")
        previous, self._trust = self._trust, created
        if previous is not None:
            # The previous trust still expires on its own; never log or echo its details.
            with suppress(Exception):
                self._delete(previous)
        return created

    def retire(self) -> None:
        """Instance removed or user revoked: delete the trust and refuse further shelve or unshelve."""
        current, self._trust = self._trust, None
        if current is not None:
            with suppress(Exception):
                self._delete(current)

    def _live_trust(self, instance_id: str) -> ShelveTrust:
        if instance_id != self._server_id:
            raise ProviderError("server is outside this delegation")
        trust = self._trust
        if trust is None or self._clock() >= trust.expires_at:
            raise ProviderError("shelve delegation is missing or expired")
        return trust

    def _provider(self, trust: ShelveTrust) -> Any:
        try:
            connection = self._open(trust)
        except Exception:
            raise ProviderError("delegated connection is unavailable") from None
        return self._provider_for(connection)

    async def shelve(self, instance_id: str) -> None:
        await self._provider(self._live_trust(instance_id)).shelve(instance_id)

    async def unshelve(self, instance_id: str) -> None:
        await self._provider(self._live_trust(instance_id)).unshelve(instance_id)


__all__ = ["ShelveDelegation", "ShelveTrust"]
