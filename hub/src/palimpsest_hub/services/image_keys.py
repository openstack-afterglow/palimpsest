"""Image API keys: issuance, listing, revocation and per-request authentication.

`ppi_v1_<32 hex key id>.<43 base64url secret>` keys are bound to the issuing
user and project. Only the SHA-256 of the 32-byte secret is stored. Every use
re-validates the owner's current native project membership, so removing the
user from the project (or disabling them) stops the key. Effective actions are
the key's delegated actions intersected with the owner's current authority:

- `images:read`    needs current `packages:inventory` and `packages:read`;
                   allows image list/get/blob download, layer-node reads and
                   the caller's own quota view.
- `images:publish` needs current `packages:write`; allows payload staging and
                   publication of payloads this user staged. It does not imply
                   `images:read`: extension (which reads a base image) and
                   reuse of payloads that were not staged by this user also
                   require `images:read`, so a publish-only key reveals nothing
                   beyond the image it just published from its own staged bytes.

Issuers may delegate only actions they currently hold (so a reader may issue
read-only keys). Each user may hold a bounded number of active keys.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import Field, field_validator
from sqlalchemy import func, select

from palimpsest_hub.auth import get_package_member_info, validate_package_owner, validate_token
from palimpsest_hub.config import get_settings, validate_keystone_id
from palimpsest_hub.database import get_session_factory
from palimpsest_hub.image_key_models import IMAGE_ACTIONS, MAX_EXPIRY_DAYS, PalimpsestImageKey
from palimpsest_hub.package_dto import StrictDTO
from palimpsest_hub.services.worker_leases import sql_mutex

PREFIX = "ppi_v1_"
_KEY = re.compile(r"ppi_v1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", re.ASCII)
# Bound the per-request write load: last use is recorded at this granularity.
LAST_USED_GRANULARITY = timedelta(minutes=5)
# Issuance is rare; one mutex orders every active-key count with its insert.
_ISSUE_MUTEX = "build-api:image-keys:issue"


class ImageKeyCreate(StrictDTO):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    actions: list[str] = Field(min_length=1, max_length=2)
    expires_in_days: int = Field(default=30, ge=1, le=MAX_EXPIRY_DAYS)

    @field_validator("actions")
    @classmethod
    def image_actions(cls, value):
        if len(set(value)) != len(value) or not set(value) <= IMAGE_ACTIONS:
            raise ValueError("invalid or duplicate image key actions")
        return value


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def now() -> datetime:
    # SQL DATETIME is naive UTC, including when SQLite returns it.
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value):
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def _factory():
    factory = get_session_factory()
    if factory is None:
        raise _error(503, "HUB_UNAVAILABLE", "Hub database unavailable")
    return factory


def available_image_actions(package_actions) -> frozenset[str]:
    """Map the owner's current native package authority onto image key actions."""
    current = set(package_actions or ())
    available = set()
    if {"packages:inventory", "packages:read"} <= current:
        available.add("images:read")
    if "packages:write" in current:
        available.add("images:publish")
    return frozenset(available)


def key_metadata(row: PalimpsestImageKey) -> dict:
    return {
        "key_id": str(uuid.UUID(row.id)),
        "name": row.name,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "actions": list(row.actions),
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
        "last_used_at": _iso(row.last_used_at),
    }


def _member_identity(member: dict) -> tuple[str, str]:
    try:
        return validate_keystone_id(member["project_id"]), validate_keystone_id(member["user_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise _error(403, "ACTION_DENIED", "a verified project member is required") from None


async def _fresh_member(member: dict) -> dict:
    """Re-validate the same original token and current membership before a key mutation."""
    original = await asyncio.to_thread(validate_token, member["token"], member["project_id"])
    current = await get_package_member_info(token_info=original)
    if (current["project_id"], current["user_id"]) != (member["project_id"], member["user_id"]):
        raise _error(403, "PROJECT_SCOPE_MISMATCH", "original key manager subject or project changed")
    return current


def _require(actions, action: str) -> None:
    if action not in actions:
        raise _error(403, "ACTION_DENIED", "required Palimpsest action is unavailable")


async def _active_key_count(session, user_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(PalimpsestImageKey)
            .where(
                PalimpsestImageKey.owner_user_id == user_id,
                PalimpsestImageKey.revoked_at.is_(None),
                PalimpsestImageKey.expires_at > now(),
            )
        )
        or 0
    )


async def issue_key(member: dict, request: ImageKeyCreate) -> dict:
    """Human member tokens issue keys only within their own current authority."""
    project_id, user_id = _member_identity(member)
    if not set(request.actions) <= available_image_actions(member.get("package_actions", ())):
        raise _error(403, "ACTION_DENIED", "key actions exceed current owner authority")
    limit = get_settings().palimpsest_hub_image_key_max_active_per_user
    secret = secrets.token_bytes(32)
    key_id = uuid.uuid4().hex
    issued = now()
    row = PalimpsestImageKey(
        id=key_id,
        project_id=project_id,
        owner_user_id=user_id,
        secret_hash=hashlib.sha256(secret).hexdigest(),
        name=request.name,
        actions=sorted(request.actions),
        created_at=issued,
        expires_at=issued + timedelta(days=request.expires_in_days),
    )
    async with sql_mutex(_factory(), _ISSUE_MUTEX) as session:
        active = await _active_key_count(session, user_id)
        if active >= limit:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "image_key_limit",
                    "message": "revoke an active image key before issuing another",
                    "limit": limit,
                    "active": active,
                },
            )
        current = await _fresh_member(member)
        if not set(request.actions) <= available_image_actions(current["package_actions"]):
            raise _error(403, "ACTION_DENIED", "key actions exceed current owner authority")
        session.add(row)
        await session.commit()
    wire = PREFIX + key_id + "." + base64.urlsafe_b64encode(secret).decode().rstrip("=")
    return {"key": key_metadata(row), "secret": wire}


async def list_keys(member: dict) -> list[dict]:
    project_id, user_id = _member_identity(member)
    _require(member.get("package_actions", ()), "keys:list")
    async with _factory()() as session:
        rows = (
            await session.scalars(
                select(PalimpsestImageKey)
                .where(PalimpsestImageKey.project_id == project_id, PalimpsestImageKey.owner_user_id == user_id)
                .order_by(PalimpsestImageKey.created_at.desc(), PalimpsestImageKey.id)
            )
        ).all()
    return [key_metadata(row) for row in rows]


def _canonical_key_id(value) -> str:
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        raise _error(404, "NOT_FOUND", "key not found") from None


async def revoke_key(member: dict, key_id: str) -> None:
    """Owners revoke only their own keys in the current project; revocation is idempotent."""
    project_id, user_id = _member_identity(member)
    _require(member.get("package_actions", ()), "keys:revoke_own")
    key_id = _canonical_key_id(key_id)
    async with _factory()() as session:
        row = await session.scalar(select(PalimpsestImageKey).where(PalimpsestImageKey.id == key_id).with_for_update())
        if row is None or row.project_id != project_id or row.owner_user_id != user_id:
            raise _error(404, "NOT_FOUND", "key not found")
        if row.revoked_at is None:
            row.revoked_at = now()
        current = await _fresh_member(member)
        _require(current["package_actions"], "keys:revoke_own")
        await session.commit()


def decode_credential(credential: str) -> tuple[str, str]:
    match = _KEY.fullmatch(credential or "")
    if not match:
        raise _error(401, "KEY_INVALID", "invalid image key")
    secret = base64.urlsafe_b64decode(match[2] + "=")
    if len(secret) != 32 or base64.urlsafe_b64encode(secret).decode().rstrip("=") != match[2]:
        raise _error(401, "KEY_INVALID", "invalid image key")
    return match[1], hashlib.sha256(secret).hexdigest()


def _check_lifetime(row: PalimpsestImageKey) -> None:
    if row.revoked_at is not None:
        raise _error(401, "KEY_REVOKED", "image key revoked")
    if row.expires_at <= now():
        raise _error(401, "KEY_EXPIRED", "image key expired")


def _last_use_stale(row: PalimpsestImageKey, current: datetime) -> bool:
    return row.last_used_at is None or current - row.last_used_at >= LAST_USED_GRANULARITY


async def _current_actor(session, row: PalimpsestImageKey) -> dict:
    """Re-validate lifetime and current owner membership; returns the bound actor.

    The row is re-read in a new transaction after identity validation, so a
    revocation committed meanwhile stops this request without a write lock
    (a fresh snapshot also on MySQL REPEATABLE READ). Only a due last-use
    update takes a row lock and commits.
    """
    _check_lifetime(row)
    owner = await asyncio.to_thread(validate_package_owner, row.owner_user_id, row.project_id)
    if (
        validate_keystone_id(owner["project_id"]) != row.project_id
        or validate_keystone_id(owner["user_id"]) != row.owner_user_id
    ):
        raise _error(403, "PROJECT_SCOPE_MISMATCH", "owner identity mismatch")
    await session.rollback()
    await session.refresh(row)
    _check_lifetime(row)
    actor = {
        "actor_type": "image-key",
        "key_id": row.id,
        "project_id": row.project_id,
        "user_id": row.owner_user_id,
        "image_actions": frozenset(set(row.actions) & available_image_actions(owner["package_actions"])),
        "is_package_system_admin": False,
    }
    current = now()
    if _last_use_stale(row, current):
        await session.refresh(row, with_for_update=True)
        _check_lifetime(row)
        if _last_use_stale(row, current):
            row.last_used_at = current
            await session.commit()
    # End the read transaction without a write.
    await session.rollback()
    return actor


async def authenticate(credential: str) -> dict:
    key_id, secret_hash = decode_credential(credential)
    async with _factory()() as session:
        row = await session.scalar(select(PalimpsestImageKey).where(PalimpsestImageKey.id == key_id))
        # Format and secret are verified before revocation or expiry is revealed.
        if row is None or not hmac.compare_digest(row.secret_hash, secret_hash):
            raise _error(401, "KEY_INVALID", "invalid image key")
        return await _current_actor(session, row)


async def fresh_actor(actor: dict, action: str) -> dict:
    """Re-check an already authenticated key at a mutation boundary."""
    async with _factory()() as session:
        row = await session.get(PalimpsestImageKey, actor["key_id"])
        if row is None:
            raise _error(401, "KEY_INVALID", "invalid image key")
        current = await _current_actor(session, row)
    if (current["project_id"], current["user_id"]) != (actor["project_id"], actor["user_id"]):
        raise _error(403, "PROJECT_SCOPE_MISMATCH", "image key owner changed")
    _require(current["image_actions"], action)
    return current
