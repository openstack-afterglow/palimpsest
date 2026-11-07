"""Anonymous read access to `public` published images, layered on the publication service."""

from __future__ import annotations

import ipaddress
import math
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

from palimpsest_hub.config import get_settings
from palimpsest_hub.image_models import PalimpsestHubPublishedImage
from palimpsest_hub.services import published_images

_HIDDEN_IMAGE_FIELDS = ("created_by", "storage_binding", "project_id")
_HIDDEN_NODE_FIELDS = frozenset({"project_id", "payload_path"})
# Every listed image is normalized and projected synchronously; anonymous pages stay small so an
# unauthenticated caller cannot make one request cost an authenticated page of large manifests.
ANONYMOUS_LIST_LIMIT = 20


def anonymous_actor() -> dict:
    """A distinct marker with no project or user; never accepted by authenticated predicates."""
    return {"anonymous": True}


def is_anonymous(actor: dict) -> bool:
    return actor.get("anonymous") is True


def anonymous_predicate():
    return PalimpsestHubPublishedImage.visibility == "public"


def anonymous_readable(row) -> bool:
    return row.visibility == "public"


async def anonymous_image_value(session, row) -> dict:
    """Public projection without owner, project or storage location details."""
    if not anonymous_readable(row):
        raise HTTPException(status_code=404, detail="published image not found")
    value = await published_images.image_value(session, row)
    for field in _HIDDEN_IMAGE_FIELDS:
        value.pop(field, None)
    value["nodes"] = [
        {key: item for key, item in node.items() if key not in _HIDDEN_NODE_FIELDS} for node in value["nodes"]
    ]
    return value


class AnonymousRateLimiter:
    """Fixed-window per-client counters in a bounded insertion-ordered table.

    Entries are re-inserted when their window restarts, so the oldest entry is
    always the one closest to expiry and is evicted first when the table is full.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[str, list[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def hit(self, client: str, now: float, *, limit: int, window: int, capacity: int) -> int | None:
        """Count one request; return Retry-After seconds when the client is over its limit."""
        entry = self._entries.get(client)
        if entry is None or now - entry[0] >= window:
            if entry is not None:
                del self._entries[client]
            while len(self._entries) >= capacity:
                self._entries.popitem(last=False)
            self._entries[client] = [now, 1]
            return None
        if entry[1] >= limit:
            return max(1, math.ceil(entry[0] + window - now))
        entry[1] += 1
        return None


_limiter = AnonymousRateLimiter()


def client_bucket(host: str | None) -> str:
    """Counter key: IPv4 as is, IPv4-mapped IPv6 as IPv4, other IPv6 per /64."""
    if not host:
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def admit_anonymous(request: Request) -> None:
    """Count one anonymous request against uvicorn's resolved client address.

    `request.client.host` is the TCP peer unless uvicorn runs with proxy headers
    and trusts the peer through its forwarded-allow-ips list, in which case it is
    the forwarded client. The Hub never parses forwarding headers itself; behind
    an untrusted proxy all anonymous callers share the proxy's bucket. Counters
    are per process, so each API process and node enforces its own limit.
    """
    settings = get_settings()
    if not settings.palimpsest_hub_anonymous_rate_limit_enabled:
        return
    retry_after = _limiter.hit(
        client_bucket(request.client.host if request.client is not None else None),
        time.monotonic(),
        limit=settings.palimpsest_hub_anonymous_rate_limit_requests,
        window=settings.palimpsest_hub_anonymous_rate_limit_window_seconds,
        capacity=settings.palimpsest_hub_anonymous_rate_limit_max_clients,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="anonymous request rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
