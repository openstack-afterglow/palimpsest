"""Native project authority and publication. CAS deduplication never grants access."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

# Reuse the existing cancellation-safe bounded filesystem workers and sorted flock protocol.
from palimpsest_hub.api.hub import (
    _expire_project_uploads,
    _locked_blobs,
    _locked_file,
    _run_blocking,
    _wait_without_releasing,
)
from palimpsest_hub.auth import validate_package_owner
from palimpsest_hub.config import default_project_namespace, get_settings, validate_keystone_id
from palimpsest_hub.database import get_session_factory
from palimpsest_hub.models import (
    PackageBlobReference,
    PackageCache,
    PackageKey,
    PackageNamespace,
    PackageTag,
    PackageUpload,
    PackageVersion,
    PalimpsestHubUpload,
    RegistryPackage,
    exact_identity,
)
from palimpsest_hub.package_dto import (
    CachePartition,
    KeyCreate,
    canonical_digest,
    canonical_namespace,
    canonical_package,
)
from palimpsest_hub.services.blob_references import blob_referenced
from palimpsest_hub.services.hub_store import (
    HubStoreError,
    HubStoreLimit,
    HubStoreUnavailable,
    get_blob_store,
)
from palimpsest_hub.services.package_contents import (
    PackageContentError,
    PackageContentLimitError,
    validate_cache_archive,
    validate_package_archive,
)

_logger = logging.getLogger(__name__)

_KEY = re.compile(r"ppk_v1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", re.ASCII)
_IDLE = timedelta(hours=24)
_ACTIVE = {"uploading", "validating"}


class RegistryError(HTTPException):
    def __init__(self, status: int, code: str, message: str, *, headers=None):
        super().__init__(status_code=status, detail={"code": code, "message": message}, headers=headers)


def now():
    # SQL DATETIME is UTC without a timezone, including when SQLite returns it.
    return datetime.now(UTC).replace(tzinfo=None)


def iso(value):
    return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def canonical_uuid(value):
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, TypeError, AttributeError):
        raise RegistryError(422, "INVALID_UUID", "invalid UUID") from None


def factory():
    result = get_session_factory()
    if result is None:
        raise RegistryError(503, "HUB_UNAVAILABLE", "Hub database unavailable")
    return result


def store():
    try:
        return get_blob_store()
    except HubStoreUnavailable:
        raise RegistryError(503, "HUB_UNAVAILABLE", "Hub storage unavailable") from None


def require_policy(member=None):
    settings = get_settings()
    try:
        projects = settings.palimpsest_hub_package_forbidden_project_ids
        users = settings.palimpsest_hub_package_forbidden_user_ids
        if not projects or not users:
            raise ValueError
        projects = {validate_keystone_id(value) for value in projects}
        users = {validate_keystone_id(value) for value in users}
    except (ValueError, TypeError, AttributeError):
        raise RegistryError(503, "POLICY_UNAVAILABLE", "package protection policy unavailable") from None
    if member is not None:
        if validate_keystone_id(member["project_id"]) in projects or validate_keystone_id(member["user_id"]) in users:
            raise RegistryError(403, "ADMIN_CREDENTIAL_FORBIDDEN", "protected principals cannot use package authority")
        if member.get("is_system_admin") or {str(r).lower() for r in member.get("roles", [])} & {"admin", "service"}:
            raise RegistryError(403, "ADMIN_CREDENTIAL_FORBIDDEN", "administrator or service authority forbidden")
    return settings


@dataclass(frozen=True)
class Actor:
    project_id: str
    user_id: str
    namespace: str
    key_id: str | None = None
    scope: dict = field(default_factory=lambda: {"all_packages": True})
    actions: tuple[str, ...] = ("packages:read",)
    credential: str | None = field(default=None, repr=False)
    can_write: bool = False


async def namespace_row(session, namespace):
    try:
        canonical_namespace(namespace)
    except ValueError:
        raise RegistryError(422, "INVALID_NAMESPACE", "invalid namespace") from None
    row = await session.scalar(select(PackageNamespace).where(PackageNamespace.namespace == namespace))
    if row is None:
        raise RegistryError(404, "NOT_FOUND", "namespace not registered")
    return row


def package_authority():
    """Return only trusted configured authority, never request Host or a guessed gateway."""
    origin = get_settings().palimpsest_hub_package_public_origin
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        return None
    return parsed.netloc


async def project_context(member):
    require_policy(member)
    project = validate_keystone_id(member["project_id"])
    async with factory()() as session:
        row = await session.get(PackageNamespace, project)
    return {"project_id": project, "project_name": member["project_name"], "namespace": row.namespace if row else None,
            "package_authority": package_authority(),
            "capabilities": {"packages_read": True, "packages_write": bool(member["can_write"]), "keys_issue": True}}


async def register_namespace(project_id, member):
    settings = require_policy(member)
    project = validate_keystone_id(member["project_id"])
    if project_id != project:
        raise RegistryError(403, "PROJECT_SCOPE_MISMATCH", "project does not match original token")
    if not member["can_write"]:
        raise RegistryError(403, "ACTION_DENIED", "namespace registration requires member write authority")
    # A readable namespace is only a trusted configuration binding, never a display-name guess.
    try:
        bindings = settings.palimpsest_hub_package_namespace_bindings
        configured = {}
        for name, owner in bindings.items():
            canonical_namespace(name)
            owner = validate_keystone_id(owner)
            if (re.fullmatch(r"p-[0-9a-f]{32}", name) or re.fullmatch(r"p-h-[0-9a-f]{56}", name)) and name != default_project_namespace(owner):
                raise ValueError
            if owner in configured:
                raise ValueError
            configured[owner] = name
        name = configured.get(project, default_project_namespace(project))
    except (ValueError, TypeError, AttributeError):
        raise RegistryError(503, "POLICY_UNAVAILABLE", "namespace binding policy invalid") from None
    blob_store = store()
    async with (
        _locked_file(blob_store, lambda: blob_store.acquire_project_upload_lock(project, blocking=False)),
        factory()() as session,
    ):
        existing = await session.get(PackageNamespace, project)
        if existing:
            return {"project_id": project, "project_name": member["project_name"], "namespace": existing.namespace}, False
        row = PackageNamespace(project_id=project, namespace=name, project_name=member["project_name"], created_at=now())
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            raise RegistryError(409, "NAMESPACE_CONFLICT", "namespace is already bound") from None
    return {"project_id": project, "project_name": row.project_name, "namespace": name}, True


async def member_actor(namespace, member):
    require_policy(member)
    async with factory()() as session:
        row = await namespace_row(session, namespace)
    if row.project_id != validate_keystone_id(member["project_id"]):
        raise RegistryError(403, "PROJECT_SCOPE_MISMATCH", "namespace does not match current project")
    return Actor(row.project_id, validate_keystone_id(member["user_id"]), namespace)


def key_metadata(row, namespace):
    return {"key_id": str(uuid.UUID(row.id)), "name": row.name, "owner_user_id": row.owner_user_id,
            "project_id": row.project_id, "namespace": namespace, "scope": row.scope, "actions": row.actions,
            "created_at": iso(row.created_at), "expires_at": iso(row.expires_at), "revoked_at": iso(row.revoked_at)}


async def issue_key(namespace, member, request: KeyCreate):
    actor = await member_actor(namespace, member)
    if any(action.endswith(":write") for action in request.actions) and not member["can_write"]:
        raise RegistryError(403, "ACTION_DENIED", "reader cannot delegate write authority")
    scope = request.scope.model_dump(exclude_none=True)
    for package in scope.get("packages", []):
        check_package(namespace, package)
    secret = secrets.token_bytes(32)
    key_id = uuid.uuid4().hex
    row = PackageKey(id=key_id, project_id=actor.project_id, owner_user_id=actor.user_id,
                     secret_hash=hashlib.sha256(secret).hexdigest(), name=request.name, scope=scope,
                     actions=request.actions, created_at=now(), expires_at=now() + timedelta(days=request.expires_in_days))
    async with factory()() as session:
        session.add(row)
        await session.commit()
    wire = f"ppk_v1_{key_id}." + base64.urlsafe_b64encode(secret).decode().rstrip("=")
    return {"key": key_metadata(row, namespace), "secret": wire}


async def list_keys(namespace, member):
    actor = await member_actor(namespace, member)
    async with factory()() as session:
        rows = (await session.scalars(select(PackageKey).where(PackageKey.project_id == actor.project_id,
                   PackageKey.owner_user_id == actor.user_id).order_by(PackageKey.created_at.desc(), PackageKey.id))).all()
    return [key_metadata(row, namespace) for row in rows]


async def revoke_key(namespace, key_id, member):
    actor = await member_actor(namespace, member)
    key_id = canonical_uuid(key_id)
    async with factory()() as session:
        row = await session.scalar(select(PackageKey).where(PackageKey.id == key_id).with_for_update())
        if row is None or row.project_id != actor.project_id or row.owner_user_id != actor.user_id:
            raise RegistryError(404, "NOT_FOUND", "key not found")
        if row.revoked_at is None:
            row.revoked_at = now()
        await session.commit()


def decode_credential(credential):
    match = _KEY.fullmatch(credential or "")
    if not match:
        raise RegistryError(401, "KEY_INVALID", "invalid package key")
    secret = base64.urlsafe_b64decode(match[2] + "=")
    if len(secret) != 32 or base64.urlsafe_b64encode(secret).decode().rstrip("=") != match[2]:
        raise RegistryError(401, "KEY_INVALID", "invalid package key")
    return match[1], hashlib.sha256(secret).hexdigest()


async def authenticate_key(credential, *, session=None, lock=False):
    require_policy()
    key_id, secret_hash = decode_credential(credential)
    if session is None:
        async with factory()() as own:
            return await authenticate_key(credential, session=own)
    stmt = select(PackageKey).where(PackageKey.id == key_id)
    if lock:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    # Format and secret are checked before exposing revocation or expiry.
    if row is None or not hmac.compare_digest(row.secret_hash, secret_hash):
        raise RegistryError(401, "KEY_INVALID", "invalid package key")
    if row.revoked_at is not None:
        raise RegistryError(401, "KEY_REVOKED", "package key revoked")
    if row.expires_at <= now():
        raise RegistryError(401, "KEY_EXPIRED", "package key expired")
    member = await asyncio.to_thread(validate_package_owner, row.owner_user_id, row.project_id)
    require_policy(member)
    if validate_keystone_id(member["project_id"]) != row.project_id or validate_keystone_id(member["user_id"]) != row.owner_user_id:
        raise RegistryError(403, "PROJECT_SCOPE_MISMATCH", "owner identity mismatch")
    if not lock:
        # A current/locking read bypasses a MySQL repeatable-read snapshot and
        # sees revocation that completed while the reader request was in flight.
        await session.refresh(row, with_for_update=True)
        if row.revoked_at is not None:
            raise RegistryError(401, "KEY_REVOKED", "package key was revoked during identity validation")
    if row.expires_at <= now():
        raise RegistryError(401, "KEY_EXPIRED", "package key expired during identity validation")
    namespace = await session.get(PackageNamespace, row.project_id)
    if namespace is None:
        raise RegistryError(404, "NOT_FOUND", "namespace not registered")
    return Actor(row.project_id, row.owner_user_id, namespace.namespace, row.id,
                 row.scope, tuple(row.actions), credential, bool(member["can_write"]))


async def auth_me(actor):
    async with factory()() as session:
        row = await session.get(PackageKey, actor.key_id)
        return {**key_metadata(row, actor.namespace), "actor_type": "package-key"}


def check_package(namespace, package):
    try:
        canonical_package(package)
        if len(namespace + "/" + package) > 255:
            raise ValueError
    except ValueError:
        raise RegistryError(422, "INVALID_PACKAGE", "invalid canonical package name") from None


def authorize(actor, namespace, package, action):
    check_package(namespace, package)
    if actor.namespace != namespace:
        raise RegistryError(403, "PROJECT_SCOPE_MISMATCH", "key or token belongs to another namespace")
    if action not in actor.actions:
        raise RegistryError(403, "ACTION_DENIED", "required action not delegated")
    if not actor.scope.get("all_packages") and package not in actor.scope.get("packages", []):
        raise RegistryError(403, "PACKAGE_SCOPE_DENIED", "package outside exact key scope")
    if action.endswith(":write") and not actor.can_write:
        raise RegistryError(403, "ACTION_DENIED", "owner no longer has delegated write authority")


async def fresh_actor(actor, namespace, package, action, *, session=None, lock=False):
    if actor.key_id is None:
        raise RegistryError(401, "AUTH_REQUIRED", "package key required")
    current = await authenticate_key(actor.credential, session=session, lock=lock)
    authorize(current, namespace, package, action)
    return current




async def upload_io(operation, *args):
    """Keep staging locks through cancellation without competing with graph/hash workers."""
    worker = asyncio.create_task(asyncio.to_thread(operation, *args))
    result, cancelled = await _wait_without_releasing(worker)
    if cancelled:
        raise asyncio.CancelledError
    return result


async def rollback_created(blob_store, digests):
    # Called with sorted digest locks held. Unknown commit outcome retains data.
    for digest in reversed(digests):
        try:
            async with factory()() as session:
                referenced = await blob_referenced(session, digest)
            if not referenced:
                await _run_blocking(blob_store.delete, digest)
        except Exception:
            # An orphan is preferable to unlinking an ambiguously committed/shared graph.
            continue


def publish_sources(blob_store, sources, created):
    """Copy/hash under CAS locks, never while holding key/package SQL row locks."""
    for digest in sorted(sources):
        path, inspection = sources[digest]
        existed = blob_store.blob_path(digest).exists() or blob_store.blob_path(digest).is_symlink()
        made = blob_store.publish_verified(path, inspection)
        if made and not existed:
            created.append(digest)


def upload_view(row, namespace):
    return {"upload_id": row.id, "project_id": row.project_id, "namespace": namespace, "package": row.package,
            "key_id": str(uuid.UUID(row.key_id)), "owner_user_id": row.owner_user_id,
            "received_bytes": row.received_bytes, "expires_at": iso(row.expires_at),
            "status": row.status, "result": row.result}


async def owned_upload(session, upload_id, actor, package, resource):
    row = await session.get(PackageUpload, canonical_uuid(upload_id))
    if row is None or (row.project_id, row.package, row.key_id, row.owner_user_id, row.resource) != (
            actor.project_id, package, actor.key_id, actor.user_id, resource):
        raise RegistryError(404, "NOT_FOUND", "upload session not found")
    return row


def upload_action(resource):
    return "cache:write" if resource == "cache" else "packages:write"


async def start_upload(actor, namespace, package, request, resource):
    actor = await fresh_actor(actor, namespace, package, upload_action(resource))
    settings = require_policy()
    if request.archive_size_bytes > settings.palimpsest_hub_max_blob_bytes:
        raise RegistryError(413, "SIZE_LIMIT", "archive exceeds configured byte limit")
    blob_store = store()
    async with _locked_file(blob_store, lambda: blob_store.acquire_project_upload_lock(actor.project_id, blocking=False)):
        await _expire_project_uploads(factory(), blob_store, actor.project_id)
        async with factory()() as session:
            stale = (await session.scalars(select(PackageUpload).where(PackageUpload.project_id == actor.project_id,
                          PackageUpload.status.in_(_ACTIVE), PackageUpload.expires_at <= now()))).all()
            for expired in stale:
                async with _locked_file(blob_store, lambda upload_id=expired.id: blob_store.acquire_upload_lock(upload_id, blocking=False)):
                    # Re-read after a PATCH/finalize holding the session lock has committed.
                    await session.refresh(expired)
                    if expired.status in _ACTIVE and expired.expires_at <= now():
                        expired.status = "failed"
                        await _run_blocking(blob_store.abort_upload, expired.id)
            await session.commit()
            active = await session.scalar(select(func.count()).select_from(PackageUpload).where(
                PackageUpload.project_id == actor.project_id, PackageUpload.status.in_(_ACTIVE)))
            legacy = await session.scalar(select(func.count()).select_from(PalimpsestHubUpload).where(
                exact_identity(PalimpsestHubUpload.project_id, actor.project_id)))
            if active + legacy >= 4:
                raise RegistryError(429, "UPLOAD_LIMIT", "project active upload limit reached")
            await fresh_actor(actor, namespace, package, upload_action(resource), session=session, lock=True)
            row = PackageUpload(id=uuid.uuid4().hex, project_id=actor.project_id, package=package,
                key_id=actor.key_id, owner_user_id=actor.user_id, resource=resource,
                request=request.model_dump(exclude_none=True) | ({"expected_tag_digest": request.expected_tag_digest} if resource == "package" else {}),
                received_bytes=0, status="uploading",
                created_at=now(), updated_at=now(), expires_at=now() + _IDLE)
            await _run_blocking(blob_store.start_upload, row.id)
            try:
                session.add(row)
                await session.commit()
            except BaseException:
                await _run_blocking(blob_store.abort_upload, row.id)
                raise
    return upload_view(row, namespace)


async def upload_status(actor, namespace, package, upload_id, resource):
    authorize(actor, namespace, package, upload_action(resource))
    async with factory()() as session:
        row = await owned_upload(session, upload_id, actor, package, resource)
        if row.status in _ACTIVE and row.expires_at <= now():
            # Expired sessions cannot be resumed even before periodic cleanup.
            return {**upload_view(row, namespace), "status": "failed"}
        return upload_view(row, namespace)


def resumable(row):
    if row.status != "uploading" or row.expires_at <= now():
        raise RegistryError(409, "UPLOAD_STATE_CONFLICT", "upload is not active")


async def append_upload(actor, namespace, package, upload_id, resource, request):
    authorize(actor, namespace, package, upload_action(resource))
    upload_id = canonical_uuid(upload_id)
    header = request.headers.get("Upload-Offset", "")
    if len(header) > 19 or re.fullmatch(r"[0-9]+", header) is None:
        raise RegistryError(422, "INVALID_OFFSET", "Upload-Offset must be a nonnegative integer")
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/octet-stream":
        raise RegistryError(422, "INVALID_CONTENT_TYPE", "upload requires application/octet-stream")
    blob_store = store()
    async with _locked_file(blob_store, lambda: blob_store.acquire_upload_lock(upload_id, blocking=False)):
        async with factory()() as session:
            row = await owned_upload(session, upload_id, actor, package, resource)
            resumable(row)
            already = row.received_bytes
            maximum = row.request["archive_size_bytes"]
        if int(header) != already:
            raise RegistryError(409, "OFFSET_CONFLICT", "Upload-Offset mismatch", headers={"Upload-Offset": str(already)})
        try:
            await upload_io(blob_store.reconcile_upload, upload_id, already)
            total = already
            async for chunk in request.stream():
                if not chunk:
                    continue
                if total + len(chunk) > maximum:
                    raise HubStoreLimit("archive exceeds declared byte limit")
                total = await upload_io(blob_store.append_upload, upload_id, chunk)
            await upload_io(blob_store.sync_upload, upload_id)
            async with factory()() as session:
                await fresh_actor(actor, namespace, package, upload_action(resource), session=session, lock=True)
                row = await owned_upload(session, upload_id, actor, package, resource)
                row.received_bytes = total
                row.updated_at = now()
                row.expires_at = now() + _IDLE
                await session.commit()
        except HubStoreLimit:
            await fail_upload(upload_id)
            raise RegistryError(413, "SIZE_LIMIT", "archive exceeds declared byte limit") from None
        except HubStoreError:
            await fail_upload(upload_id)
            raise RegistryError(409, "UPLOAD_STATE_CONFLICT", "staging file unavailable or oversized") from None
    return total


async def fail_upload(upload_id):
    async with factory()() as session:
        row = await session.get(PackageUpload, upload_id)
        if row and row.status != "complete":
            row.status = "failed"
            row.updated_at = now()
            await session.commit()
            await _run_blocking(store().abort_upload, upload_id)


async def abort_upload(actor, namespace, package, upload_id, resource):
    authorize(actor, namespace, package, upload_action(resource))
    upload_id = canonical_uuid(upload_id)
    blob_store = store()
    async with _locked_file(blob_store, lambda: blob_store.acquire_upload_lock(upload_id, blocking=False)):
        async with factory()() as session:
            await fresh_actor(actor, namespace, package, upload_action(resource), session=session, lock=True)
            row = await owned_upload(session, upload_id, actor, package, resource)
            # Aborting a completed operation never deletes its immutable version/CAS data.
            if row.status != "complete":
                row.status = "aborted"
                row.updated_at = now()
            await session.commit()
        await _run_blocking(blob_store.abort_upload, upload_id)


def publication_url(namespace, package, digest):
    origin = get_settings().palimpsest_hub_package_public_origin
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise RegistryError(503, "HUB_UNAVAILABLE", "trusted HTTPS public origin unavailable")
    return origin.rstrip("/") + "/palimpsest/packages?" + urlencode({"namespace": namespace, "package": package, "digest": digest})


def cache_receipt(row, namespace):
    return {"project_id": row.project_id, "namespace": namespace, "package": row.package,
            "build_key": row.build_key, "cache_scope": row.cache_scope, "platform": row.platform,
            "builder_fingerprint": row.builder_fingerprint, "archive_digest": row.archive_digest,
            "archive_size_bytes": row.archive_size_bytes, "created_at": iso(row.created_at), "created_by": row.created_by}


async def finalize_upload(actor, namespace, package, upload_id, resource):
    authorize(actor, namespace, package, upload_action(resource))
    upload_id = canonical_uuid(upload_id)
    blob_store = store()
    settings = require_policy()
    async with _locked_file(blob_store, lambda: blob_store.acquire_upload_lock(upload_id, blocking=False)):
        async with factory()() as session:
            await fresh_actor(actor, namespace, package, upload_action(resource), session=session, lock=True)
            row = await owned_upload(session, upload_id, actor, package, resource)
            if row.status == "complete":
                return row.result, False
            # Holding the upload flock proves no finalizer is still active after a crash.
            if row.status == "validating":
                row.status = "uploading"
            resumable(row)
            data = row.request
            if row.received_bytes != data["archive_size_bytes"]:
                raise RegistryError(409, "UPLOAD_INCOMPLETE", "archive has not been fully received", headers={"Upload-Offset": str(row.received_bytes)})
            row.status = "validating"
            row.updated_at = now()
            row.expires_at = now() + _IDLE
            await session.commit()
        try:
            await _run_blocking(blob_store.reconcile_upload, upload_id, data["archive_size_bytes"])
            archive = await _run_blocking(blob_store.inspect_file, blob_store.upload_path(upload_id), max_bytes=settings.palimpsest_hub_max_blob_bytes)
            if archive.blob_digest != data["archive_digest"] or archive.size_bytes != data["archive_size_bytes"]:
                raise RegistryError(422, "CONTENT_INVALID", "archive digest or size mismatch")
            with tempfile.TemporaryDirectory(prefix="palimpsest-package-") as temporary:
                staging = Path(temporary)
                if resource == "cache":
                    binding = {"project_id": actor.project_id, "namespace": namespace, "package": package,
                               **{key: data[key] for key in ("build_key", "cache_scope", "platform", "builder_fingerprint")}}
                    await _run_blocking(validate_cache_archive, blob_store.upload_path(upload_id), staging,
                        expected_binding=binding, max_blob_bytes=settings.palimpsest_hub_max_blob_bytes,
                        max_expanded_bytes=settings.palimpsest_hub_max_bundle_expanded_bytes)
                    validated = None
                    sources = {archive.blob_digest: (blob_store.upload_path(upload_id), archive)}
                else:
                    validated = await _run_blocking(validate_package_archive, blob_store.upload_path(upload_id), staging,
                        package_type=data["package_type"], root_digest=data["root_digest"],
                        max_blob_bytes=settings.palimpsest_hub_max_blob_bytes,
                        max_expanded_bytes=settings.palimpsest_hub_max_bundle_expanded_bytes)
                    # Inspect every selected graph blob; publication never fills it from global CAS.
                    sources = {archive.blob_digest: (blob_store.upload_path(upload_id), archive)}
                    for digest, path in validated.blob_paths.items():
                        inspection = await _run_blocking(blob_store.inspect_file, path, max_bytes=settings.palimpsest_hub_max_blob_bytes)
                        if inspection.blob_digest != digest or inspection.size_bytes != validated.graph[digest]["size_bytes"]:
                            raise RegistryError(422, "CONTENT_INVALID", "selected graph descriptor mismatch")
                        sources[digest] = (path, inspection)
                    if set(validated.graph) != set(validated.blob_paths) or validated.root_digest != data["root_digest"]:
                        raise RegistryError(422, "CONTENT_INVALID", "incomplete selected graph")
                    publication_url(namespace, package, validated.root_digest)
                # One package lock covers tag and cache metadata; CAS locks are always lexical.
                package_lock = actor.project_id + "/" + package
                async with (
                    _locked_file(blob_store, lambda: blob_store.acquire_package_lock(package_lock, blocking=False)),
                    _locked_blobs(blob_store, list(sources)),
                ):
                    created = []
                    try:
                        # Authorization is fresh before filesystem publication and again at SQL commit.
                        await fresh_actor(actor, namespace, package, upload_action(resource))
                        await _run_blocking(publish_sources, blob_store, sources, created)
                        async with factory()() as session:
                            await fresh_actor(actor, namespace, package, upload_action(resource), session=session, lock=True)
                            upload = await owned_upload(session, upload_id, actor, package, resource)
                            if upload.expires_at <= now():
                                raise RegistryError(409, "UPLOAD_STATE_CONFLICT", "upload session expired during validation")
                            if resource == "cache":
                                result, changed = await commit_cache(session, actor, namespace, package, data)
                            else:
                                result, changed = await commit_package(session, actor, namespace, package, data, validated)
                            upload.status = "complete"
                            upload.result = result
                            upload.updated_at = now()
                            await session.commit()
                        # An idempotent re-push may carry a distinct archive for an existing root.
                        # Retain only references committed by this or another independent publication.
                        await rollback_created(blob_store, created)
                    except BaseException:
                        await rollback_created(blob_store, created)
                        raise
            try:
                await _run_blocking(blob_store.abort_upload, upload_id)
            except OSError:
                _logger.warning("committed native upload staging could not be removed")
            return result, changed
        except (PackageContentError, HubStoreError, ValueError) as exc:
            await fail_upload(upload_id)
            status = 413 if isinstance(exc, (HubStoreLimit, PackageContentLimitError)) else 422
            raise RegistryError(status, "CONTENT_INVALID", "archive or selected graph validation failed") from None
        except BaseException:
            # Retryable tag/identity/SQL failures retain verified staging, not stale approval.
            async with factory()() as session:
                upload = await session.get(PackageUpload, upload_id)
                if upload and upload.status == "validating":
                    upload.status = "uploading"
                    await session.commit()
            raise


async def commit_package(session, actor, namespace, package, data, validated):
    row = await session.scalar(select(RegistryPackage).where(RegistryPackage.project_id == actor.project_id,
                             RegistryPackage.name == package).with_for_update())
    if row is None:
        row = RegistryPackage(id=uuid.uuid4().hex, project_id=actor.project_id, name=package,
                              package_type=data["package_type"], created_at=now(), updated_at=now())
        session.add(row)
        await session.flush()
    elif row.package_type != data["package_type"]:
        raise RegistryError(409, "PACKAGE_TYPE_CONFLICT", "package type is immutable")
    tag = await session.get(PackageTag, (row.id, data["tag"]))
    same = tag is not None and tag.root_digest == validated.root_digest
    if not same and (tag.root_digest if tag else None) != data["expected_tag_digest"]:
        raise RegistryError(412, "TAG_CONFLICT", "tag changed since preflight")
    version = await session.get(PackageVersion, (row.id, validated.root_digest))
    if version is None:
        version = PackageVersion(package_id=row.id, root_digest=validated.root_digest,
            root_media_type=validated.root_media_type, graph=validated.graph, platforms=validated.platforms,
            archive_digest=data["archive_digest"], archive_size_bytes=data["archive_size_bytes"],
            total_bytes=validated.total_bytes, provenance={k: v for k, v in data.get("provenance", {}).items() if v is not None},
            pushed_by=actor.user_id, pushed_key_id=actor.key_id, pushed_at=now())
        session.add(version)
        await session.flush()
        for digest in sorted(set(validated.graph) | {data["archive_digest"]}):
            session.add(PackageBlobReference(package_id=row.id, root_digest=validated.root_digest, blob_digest=digest))
    if not same:
        if tag is None:
            session.add(PackageTag(package_id=row.id, tag=data["tag"], root_digest=validated.root_digest,
                                   updated_at=now(), updated_by=actor.user_id, revision=1))
            # Unique PK is the SQL compare-and-set for expected absence.
            try:
                await session.flush()
            except IntegrityError:
                raise RegistryError(412, "TAG_CONFLICT", "tag changed since preflight") from None
        else:
            updated = await session.execute(update(PackageTag).where(PackageTag.package_id == row.id,
                PackageTag.tag == data["tag"], PackageTag.root_digest == data["expected_tag_digest"],
                PackageTag.revision == tag.revision).values(root_digest=validated.root_digest,
                updated_at=now(), updated_by=actor.user_id, revision=tag.revision + 1))
            if updated.rowcount != 1:
                raise RegistryError(412, "TAG_CONFLICT", "tag changed since preflight")
        row.updated_at = now()
    return {"project_id": actor.project_id, "namespace": namespace, "package": package, "tag": data["tag"],
            "digest": validated.root_digest, "package_type": row.package_type, "visibility": "project",
            "platforms": validated.platforms, "already_published": same,
            "pushed_by": actor.user_id, "pushed_key_id": str(uuid.UUID(actor.key_id)),
            "web_url": publication_url(namespace, package, validated.root_digest)}, not same


async def commit_cache(session, actor, namespace, package, data):
    row = await session.scalar(select(PackageCache).where(PackageCache.project_id == actor.project_id,
        PackageCache.package == package, PackageCache.build_key == data["build_key"],
        PackageCache.cache_scope == data["cache_scope"], PackageCache.platform == data["platform"],
        PackageCache.builder_fingerprint == data["builder_fingerprint"], PackageCache.archive_digest == data["archive_digest"],
        PackageCache.created_by == actor.user_id))
    if row is not None:
        return cache_receipt(row, namespace), False
    row = PackageCache(id=uuid.uuid4().hex, project_id=actor.project_id, package=package,
        **{key: data[key] for key in ("build_key", "cache_scope", "platform", "builder_fingerprint", "archive_digest", "archive_size_bytes")},
        created_at=now(), created_by=actor.user_id)
    session.add(row)
    return cache_receipt(row, namespace), True


async def resolve_cache(actor, namespace, package, partition: CachePartition):
    authorize(actor, namespace, package, "cache:read")
    async with factory()() as session:
        base = select(PackageCache).where(PackageCache.project_id == actor.project_id, PackageCache.package == package,
            PackageCache.cache_scope == partition.cache_scope, PackageCache.platform == partition.platform,
            PackageCache.builder_fingerprint == partition.builder_fingerprint).order_by(PackageCache.created_at.desc(), PackageCache.id.desc())
        row = await session.scalar(base.where(PackageCache.build_key == partition.build_key).limit(1))
        resolution = "exact"
        if row is None:
            row = await session.scalar(base.limit(1))
            resolution = "scope"
        if row is None:
            raise RegistryError(404, "NOT_FOUND", "cache miss")
        return {**cache_receipt(row, namespace), "resolution": resolution}


async def cache_archive(actor, namespace, package, digest):
    authorize(actor, namespace, package, "cache:read")
    digest = checked_digest(digest)
    async with factory()() as session:
        row = await session.scalar(select(PackageCache).where(PackageCache.project_id == actor.project_id,
                    PackageCache.package == package, PackageCache.archive_digest == digest).limit(1))
        if row is None:
            raise RegistryError(404, "NOT_FOUND", "cache archive not found")
        return row.archive_size_bytes


def checked_digest(digest):
    try:
        return canonical_digest(digest)
    except ValueError:
        raise RegistryError(422, "INVALID_DIGEST", "invalid canonical sha256 digest") from None


async def package_row(session, actor, package):
    row = await session.scalar(select(RegistryPackage).where(RegistryPackage.project_id == actor.project_id,
                              RegistryPackage.name == package))
    if row is None:
        raise RegistryError(404, "NOT_FOUND", "package not found")
    return row


async def summary(session, row, namespace):
    tags = (await session.scalars(select(PackageTag).where(PackageTag.package_id == row.id).order_by(PackageTag.tag))).all()
    latest = await session.scalar(select(PackageVersion).where(PackageVersion.package_id == row.id)
                                  .order_by(PackageVersion.pushed_at.desc(), PackageVersion.root_digest.desc()).limit(1))
    count = await session.scalar(select(func.count()).select_from(PackageVersion).where(PackageVersion.package_id == row.id))
    return {"package_id": str(uuid.UUID(row.id)), "project_id": row.project_id, "namespace": namespace,
        "name": row.name, "package_type": row.package_type, "visibility": "project",
        "tags": [{"tag": tag.tag, "digest": tag.root_digest} for tag in tags], "platforms": latest.platforms if latest else [],
        "version_count": count, "latest_pushed_at": iso(latest.pushed_at) if latest else None,
        "latest_pushed_by": latest.pushed_by if latest else None}


def cursor_binding(actor, kind, extra):
    return hashlib.sha256(json.dumps([actor.project_id, actor.namespace, actor.key_id, actor.scope, kind, extra], sort_keys=True).encode()).hexdigest()


def read_cursor(cursor, binding):
    if not cursor:
        return None
    try:
        if len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, dict) or set(value) != {"binding", "after"} or value["binding"] != binding:
            raise ValueError
        return value["after"]
    except (ValueError, TypeError, UnicodeError):
        raise RegistryError(422, "INVALID_CURSOR", "invalid pagination cursor") from None


def make_cursor(binding, after):
    return base64.urlsafe_b64encode(json.dumps({"binding": binding, "after": after}, separators=(",", ":")).encode()).decode().rstrip("=")


async def inventory(actor, namespace, limit, cursor, package_type):
    if actor.namespace != namespace:
        raise RegistryError(403, "PROJECT_SCOPE_MISMATCH", "namespace does not match actor")
    if "packages:read" not in actor.actions:
        raise RegistryError(403, "ACTION_DENIED", "package read not delegated")
    binding = cursor_binding(actor, "packages", package_type)
    after = read_cursor(cursor, binding)
    if after is not None and not isinstance(after, str):
        raise RegistryError(422, "INVALID_CURSOR", "invalid package cursor")
    stmt = select(RegistryPackage).where(RegistryPackage.project_id == actor.project_id)
    if not actor.scope.get("all_packages"):
        stmt = stmt.where(RegistryPackage.name.in_(actor.scope.get("packages", [])))
    if package_type:
        stmt = stmt.where(RegistryPackage.package_type == package_type)
    if after:
        stmt = stmt.where(RegistryPackage.name > after)
    async with factory()() as session:
        rows = (await session.scalars(stmt.order_by(RegistryPackage.name).limit(limit + 1))).all()
        items = [await summary(session, row, namespace) for row in rows[:limit]]
    return {"project_id": actor.project_id, "namespace": namespace, "items": items,
            "next_cursor": make_cursor(binding, rows[limit - 1].name) if len(rows) > limit else None}


async def package_detail(actor, namespace, package):
    authorize(actor, namespace, package, "packages:read")
    async with factory()() as session:
        return await summary(session, await package_row(session, actor, package), namespace)


def version_view(row, actor, package):
    return {"project_id": actor.project_id, "namespace": actor.namespace, "package": package,
        "root_digest": row.root_digest, "root_media_type": row.root_media_type,
        "graph": row.graph, "platforms": row.platforms, "archive_digest": row.archive_digest,
        "archive_size_bytes": row.archive_size_bytes, "total_bytes": row.total_bytes, "provenance": row.provenance,
        "pushed_by": row.pushed_by, "pushed_key_id": str(uuid.UUID(row.pushed_key_id)), "pushed_at": iso(row.pushed_at),
        "root_descriptor": {"digest": row.root_digest, "mediaType": row.root_media_type,
                            "size": row.graph[row.root_digest]["size_bytes"]}}


async def versions(actor, namespace, package, limit, cursor):
    authorize(actor, namespace, package, "packages:read")
    binding = cursor_binding(actor, "versions", package)
    after = read_cursor(cursor, binding)
    async with factory()() as session:
        row = await package_row(session, actor, package)
        stmt = select(PackageVersion).where(PackageVersion.package_id == row.id)
        if after is not None:
            try:
                if not isinstance(after, list) or len(after) != 2:
                    raise ValueError
                pushed = datetime.fromisoformat(after[0]).replace(tzinfo=None)
                digest = canonical_digest(after[1])
            except (ValueError, TypeError):
                raise RegistryError(422, "INVALID_CURSOR", "invalid version cursor") from None
            stmt = stmt.where((PackageVersion.pushed_at < pushed) | ((PackageVersion.pushed_at == pushed) & (PackageVersion.root_digest < digest)))
        rows = (await session.scalars(stmt.order_by(PackageVersion.pushed_at.desc(), PackageVersion.root_digest.desc()).limit(limit + 1))).all()
    return {"project_id": actor.project_id, "namespace": namespace, "package": package,
            "items": [version_view(row, actor, package) for row in rows[:limit]],
            "next_cursor": make_cursor(binding, [iso(rows[limit - 1].pushed_at), rows[limit - 1].root_digest]) if len(rows) > limit else None}


async def version(actor, namespace, package, *, digest=None, tag=None):
    authorize(actor, namespace, package, "packages:read")
    async with factory()() as session:
        row = await package_row(session, actor, package)
        if tag is not None:
            alias = await session.get(PackageTag, (row.id, tag))
            if alias is None:
                raise RegistryError(404, "NOT_FOUND", "tag not found")
            digest = alias.root_digest
        digest = checked_digest(digest)
        value = await session.get(PackageVersion, (row.id, digest))
        if value is None:
            raise RegistryError(404, "NOT_FOUND", "version not found")
        result = version_view(value, actor, package)
        result["package_type"] = row.package_type
        result["visibility"] = "project"
        if tag is not None:
            result["tag"] = tag
            result["digest"] = value.root_digest
        return result
