"""Claim response receipts, used only inside the protocol's SQL transaction mutex.

GET recovery is an authoritative settlement, not a lookup whose absence means
"retry": advancing an unseen sequence commits an empty outcome and fences any
later arrival of its POST. Normal idle polling overwrites one bounded cursor.
"""

from fastapi import HTTPException

from palimpsest_hub.models import PalimpsestWorkerClaim


async def prepare(session, protocol: str, channel: str, sequence: int):
    cursor = await session.get(PalimpsestWorkerClaim, (channel, protocol), with_for_update=True)
    if cursor is not None:
        if sequence < cursor.sequence:
            raise HTTPException(status_code=409, detail="worker claim sequence is stale")
        if sequence == cursor.sequence:
            return cursor, True
        cursor.sequence, cursor.result_json = sequence, None
    else:
        cursor = PalimpsestWorkerClaim(channel=channel, protocol=protocol, sequence=sequence, result_json=None)
        session.add(cursor)
    return cursor, False


def settlement(cursor):
    result = cursor.result_json
    return {
        "channel": cursor.channel,
        "protocol": cursor.protocol,
        "sequence": cursor.sequence,
        "claim": {key: result[key] for key in ("id", "lease")} if result is not None else None,
    }
