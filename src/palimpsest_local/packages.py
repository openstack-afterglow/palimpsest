"""Verified HTTPS transport for project-scoped native packages and build caches.

One client binds one exact API base, namespace and package key. Every package or
cache operation re-authenticates ``/auth/me`` and checks exact package scope and
actions before any session, download or cache effect. There is no Docker,
Keystone-token, admin or other-host fallback, and redirects are never followed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError, PalimpsestError
from .oci_image import strict_json_object
from .oci_source import _open_absolute_regular_file
from .package_credentials import get_package_key, validate_package_key
from .package_source import PackageLimits, PackageSnapshot, _copy_fd, _hash_file, snapshot_package
from .registry import (
    _TAG_RE,
    RegistryError,
    RegistryProfile,
    _normalize_repository_path,
    normalize_native_api_base,
    normalize_native_namespace,
)

__all__ = [
    "NativePackageClient",
    "PackageError",
    "PackageHTTPError",
    "PackageLimits",
    "PackageSnapshot",
    "snapshot_package",
    "validate_keystone_id",
]

_CHUNK = 1024 * 1024
_PATCH_BYTES = 64 * 1024 * 1024
_MAX_RESPONSE = 4 * 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_KEYSTONE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_ERROR_CODE = re.compile(r"[A-Z0-9_]{1,80}\Z", re.ASCII)
_CACHE_SCOPE = re.compile(r"[a-z0-9][a-z0-9.-]{0,47}\Z", re.ASCII)
_CACHE_PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9_]+(?:/[a-zA-Z0-9_.-]+)?\Z", re.ASCII)
_BUILDER_FINGERPRINT = re.compile(r"[A-Za-z0-9_.:+/-]{1,128}\Z", re.ASCII)
_ACTIONS = frozenset({"packages:read", "packages:write", "cache:read", "cache:write"})
_PACKAGE_TYPES = frozenset({"oci-image", "runtime-bundle"})
_CACHE_SCHEMA = "palimpsest-buildkit-cache-archive-v1"
_CACHE_BINDING = ("project_id", "namespace", "package", "build_key", "cache_scope", "platform", "builder_fingerprint")
_ENV_KEY = "PALIMPSEST_PACKAGE_KEY"


class PackageError(PalimpsestError):
    """A native package request, response or binding is invalid."""


class PackageHTTPError(PackageError):
    """An HTTP status plus its bounded, credential-free error envelope code."""

    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(f"native package request failed: HTTP {status} ({code})")

    @property
    def authoritative_missing(self) -> bool:
        return self.status == 404 and self.code == "NOT_FOUND"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects, so a bearer key cannot reach another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Window:
    """Bounded file reader for one PATCH body; hashes exactly the bytes sent."""

    def __init__(self, stream, length: int, hasher):
        self.stream, self.remaining, self.hasher = stream, length, hasher

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        size = self.remaining if size is None or size < 0 else min(size, self.remaining)
        chunk = self.stream.read(min(size, _CHUNK))
        if not chunk:
            raise PackageError("native upload snapshot ended early")
        self.remaining -= len(chunk)
        self.hasher.update(chunk)
        return chunk


def validate_keystone_id(value: object, field: str = "identity") -> str:
    """Keystone user/project IDs are opaque exact strings, never UUID-normalized."""
    if not isinstance(value, str) or _KEYSTONE_ID.fullmatch(value) is None:
        raise PackageError(f"invalid native {field}")
    return value


def _hub_uuid(value: object, field: str) -> str:
    """Hub-generated key/upload IDs are UUIDs; compare their canonical hex."""
    if not isinstance(value, str):
        raise PackageError(f"invalid native {field}")
    try:
        return uuid.UUID(value).hex
    except ValueError:
        raise PackageError(f"invalid native {field}") from None


def _digest(value: object, field: str = "digest") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PackageError(f"invalid native {field}")
    return value


def _size(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise PackageError("invalid or over-limit native archive size")
    return value


def _tag(value: object) -> str:
    if not isinstance(value, str) or _TAG_RE.fullmatch(value) is None:
        raise PackageError("invalid native tag")
    return value


def _future(value: object) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        raise PackageError("invalid native timestamp") from None
    if moment.tzinfo is None:
        raise PackageError("invalid native timestamp")
    return moment


class NativePackageClient:
    """Exact project-namespace package/cache client for one native profile.

    Construction performs no network or source effects; it only reads an
    explicit credential, then ``PALIMPSEST_PACKAGE_KEY``, then the configured
    credential helper. ``namespace`` (a reference's first path component)
    overrides the profile default and must equal the key's ``/auth/me``
    namespace; it is never rewritten.
    """

    def __init__(
        self,
        profile: RegistryProfile,
        *,
        namespace: str | None = None,
        credential: str | None = None,
        timeout_seconds: float = 60,
        limits: PackageLimits | None = None,
    ):
        if not isinstance(profile, RegistryProfile) or profile.protocol != "palimpsest":
            raise RegistryError("native package client requires a palimpsest registry profile")
        self.profile = profile
        self.api_base = normalize_native_api_base(profile.api_base, profile.endpoint)
        self.namespace = normalize_native_namespace(profile.namespace if namespace is None else namespace)
        if credential is None:
            credential = os.environ.get(_ENV_KEY)
        if credential is None:
            credential = get_package_key(profile, self.namespace)
        self.key_id = validate_package_key(credential)
        self._credential = credential
        self.project_id: str | None = None
        self.owner_user_id: str | None = None
        self.timeout_seconds = timeout_seconds
        self.limits = limits or PackageLimits()
        self._opener = None
        self._cache_receipts: dict[str, dict[str, Any]] = {}

    # Transport -----------------------------------------------------------

    def _transport(self):
        if self._opener is None:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            for ca in self.profile.ca:
                context.load_verify_locations(cafile=ca)
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())
        return self._opener

    def _url(self, path: str, query: dict[str, str] | None) -> str:
        url = self.api_base + path
        return url + "?" + urllib.parse.urlencode(query) if query else url

    def _open(self, method, path, *, query=None, body=None, headers=None):
        url = self._url(path, query)
        request_headers = {"Authorization": "Bearer " + self._credential, "Accept": "application/json"}
        if isinstance(body, dict):
            body = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            response = self._transport().open(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            code = "REQUEST_FAILED"
            try:
                payload = exc.read(_MAX_RESPONSE + 1)
                error = (
                    strict_json_object(payload, "native error").get("error") if len(payload) <= _MAX_RESPONSE else None
                )
                if (
                    isinstance(error, dict)
                    and isinstance(error.get("code"), str)
                    and _ERROR_CODE.fullmatch(error["code"])
                ):
                    code = error["code"]
            except (ArtifactValidationError, OSError, RecursionError, ValueError):
                pass
            finally:
                exc.close()
            raise PackageHTTPError(exc.code, code) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise PackageError("native package HTTPS transport failed") from None
        if response.geturl() != url or not 200 <= response.status < 300:
            response.close()
            raise PackageError("native package response was redirected or has an invalid status")
        return response

    def _json(self, method, path, *, query=None, body=None) -> dict[str, Any]:
        with self._open(method, path, query=query, body=body) as response:
            payload = response.read(_MAX_RESPONSE + 1)
        if len(payload) > _MAX_RESPONSE:
            raise PackageError("native response exceeds its JSON byte limit")
        try:
            return strict_json_object(payload, "native response")
        except (ArtifactValidationError, RecursionError):
            raise PackageError("native response is not one strict JSON object") from None

    def _path(self, suffix: str) -> str:
        return "/projects/" + self.namespace + suffix

    def _package(self, package: object) -> str:
        try:
            value = _normalize_repository_path(package, "native package")
        except RegistryError:
            raise PackageError("invalid native package name") from None
        if len(self.namespace) + 1 + len(value) > 255:
            raise PackageError("native package reference exceeds its length limit")
        return value

    # Authentication ------------------------------------------------------

    def authenticate(self) -> dict[str, Any]:
        """Validate the key actor, embedded key ID, opaque owner/project and namespace."""
        actor = self._json("GET", "/auth/me")
        if actor.get("actor_type") != "package-key" or _hub_uuid(actor.get("key_id"), "key ID") != self.key_id:
            raise PackageError("native credential is not the presented package key")
        if actor.get("namespace") != self.namespace:
            raise PackageError("native key namespace differs from the requested namespace")
        project = validate_keystone_id(actor.get("project_id"), "project ID")
        owner = validate_keystone_id(actor.get("owner_user_id"), "owner user ID")
        if self.project_id is not None and (project, owner) != (self.project_id, self.owner_user_id):
            raise PackageError("native key ownership changed between requests")
        scope = actor.get("scope")
        if isinstance(scope, dict) and set(scope) == {"all_packages"} and scope["all_packages"] is True:
            pass
        elif (
            isinstance(scope, dict)
            and set(scope) == {"packages"}
            and isinstance(scope["packages"], list)
            and 1 <= len(scope["packages"]) <= 32
        ):
            names = [self._package(name) for name in scope["packages"]]
            if names != scope["packages"] or len(set(names)) != len(names):
                raise PackageError("native key package scope is not canonical")
        else:
            raise PackageError("native key has an invalid package scope")
        actions = actor.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or any(not isinstance(action, str) or action not in _ACTIONS for action in actions)
            or len(set(actions)) != len(actions)
        ):
            raise PackageError("native key has invalid actions")
        for resource in ("packages", "cache"):
            if f"{resource}:write" in actions and f"{resource}:read" not in actions:
                raise PackageError("native key write action lacks its read action")
        if actor.get("revoked_at") is not None:
            raise PackageError("native key is revoked")
        if _future(actor.get("expires_at")) <= datetime.now(UTC):
            raise PackageError("native key is expired")
        self.project_id, self.owner_user_id = project, owner
        return actor

    def authorize(self, package: str, actions) -> dict[str, Any]:
        """Freshly authenticate, then require exact package scope and actions."""
        package = self._package(package)
        requested = set(actions)
        if not requested or not requested <= _ACTIONS:
            raise PackageError("invalid requested native actions")
        actor = self.authenticate()
        if actor["scope"].get("all_packages") is not True and package not in actor["scope"]["packages"]:
            raise PackageError("native key does not authorize this exact package")
        if not requested <= set(actor["actions"]):
            raise PackageError("native key does not authorize the requested actions")
        return actor

    def _bound(self, data: dict[str, Any], package: str) -> None:
        if (
            validate_keystone_id(data.get("project_id"), "project ID") != self.project_id
            or data.get("namespace") != self.namespace
            or data.get("package") != package
        ):
            raise PackageError("native response project/namespace/package binding mismatch")

    # Uploads -------------------------------------------------------------

    def _abort(self, path: str, package: str) -> None:
        try:
            self._open("DELETE", path, query={"package": package}).close()
        except PackageError:
            pass

    def _upload(self, package: str, archive: Path, body: dict[str, Any], *, cache: bool) -> dict[str, Any]:
        """Create one key-bound session, PATCH acknowledged offsets, then PUT."""
        suffix = "/cache/uploads" if cache else "/uploads"
        session = self._json("POST", self._path(suffix), query={"package": package}, body=body)
        upload_id = _hub_uuid(session.get("upload_id"), "upload ID")
        path = self._path(f"{suffix}/{upload_id}")
        try:
            self._bound(session, package)
            if (
                _hub_uuid(session.get("key_id"), "session key ID") != self.key_id
                or validate_keystone_id(session.get("owner_user_id"), "session owner") != self.owner_user_id
            ):
                raise PackageError("native upload session is bound to another key or owner")
            if session.get("received_bytes") != 0 or session.get("status") != "uploading":
                raise PackageError("new native upload session has an invalid offset or status")
            total = body["archive_size_bytes"]
            hasher = hashlib.sha256()
            offset = 0
            with archive.open("rb") as stream:
                while offset < total:
                    length = min(_PATCH_BYTES, total - offset)
                    window = _Window(stream, length, hasher)
                    headers = {
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(length),
                        "Upload-Offset": str(offset),
                    }
                    with self._open(
                        "PATCH", path, query={"package": package}, body=window, headers=headers
                    ) as response:
                        acknowledged = response.headers.get("Upload-Offset")
                        if response.status != 204 or window.remaining or acknowledged != str(offset + length):
                            raise PackageError("native upload acknowledged an invalid byte offset")
                    offset += length
                if stream.read(1):
                    raise PackageError("native upload snapshot grew during transfer")
            if "sha256:" + hasher.hexdigest() != body["archive_digest"]:
                raise PackageError("native upload snapshot digest changed during transfer")
        except BaseException:
            self._abort(path, package)
            raise
        try:
            return self._json("PUT", path, query={"package": package}, body={})
        except PackageHTTPError as exc:
            if exc.status < 500:
                self._abort(path, package)
            raise
        except PackageError:
            raise PackageError(
                "native finalization outcome is unknown; read the tag or digest before retrying"
            ) from None

    # Packages ------------------------------------------------------------

    def _resolve(self, package: str, tag: str) -> dict[str, Any] | None:
        try:
            result = self._json("GET", self._path("/resolve"), query={"package": package, "tag": tag})
        except PackageHTTPError as exc:
            if exc.authoritative_missing:
                return None
            raise
        self._bound(result, package)
        if (
            result.get("tag") != tag
            or _digest(result.get("digest")) != result.get("root_digest")
            or result.get("package_type") not in _PACKAGE_TYPES
        ):
            raise PackageError("native tag resolution has a mismatched reference or root")
        return result

    def resolve(self, package: str, tag: str) -> dict[str, Any] | None:
        """Return the authoritative tag target, or ``None`` only on envelope 404."""
        package = self._package(package)
        self.authorize(package, ("packages:read",))
        return self._resolve(package, _tag(tag))

    def push(
        self, package: str, tag: str, snapshot: PackageSnapshot, provenance: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Publish one frozen snapshot with tag compare-and-set; verify the receipt."""
        package = self._package(package)
        tag = _tag(tag)
        if not isinstance(snapshot, PackageSnapshot) or snapshot.package_type not in _PACKAGE_TYPES:
            raise PackageError("native push requires a verified package snapshot")
        body: dict[str, Any] = {
            "package_type": snapshot.package_type,
            "tag": tag,
            "root_digest": snapshot.root_digest,
            "archive_digest": snapshot.archive_digest,
            "archive_size_bytes": _size(snapshot.archive_size_bytes, self.limits.max_archive_bytes),
        }
        if provenance:
            if not set(provenance) <= {"source_revision", "build_id"} or any(
                not isinstance(value, str) or len(value) > 128 for value in provenance.values()
            ):
                raise PackageError("native provenance permits only bounded source_revision/build_id strings")
            body["provenance"] = dict(provenance)
        self.authorize(package, ("packages:read", "packages:write"))
        current = self._resolve(package, tag)
        body["expected_tag_digest"] = None if current is None else current["digest"]
        result = self._upload(package, snapshot.archive, body, cache=False)
        self._bound(result, package)
        if (
            result.get("digest") != snapshot.root_digest
            or result.get("package_type") != snapshot.package_type
            or result.get("tag") != tag
            or result.get("visibility") != "project"
            or type(result.get("already_published")) is not bool
        ):
            raise PackageError("native publication receipt root/type/reference mismatch")
        # pushed_by/pushed_key_id describe the immutable version's first
        # publisher, which may be another project member for an idempotent root.
        validate_keystone_id(result.get("pushed_by"), "version publisher")
        _hub_uuid(result.get("pushed_key_id"), "version publisher key")
        return {
            **result,
            "upload_archive_digest": snapshot.archive_digest,
            "upload_archive_size_bytes": snapshot.archive_size_bytes,
            "root_media_type": snapshot.root_media_type,
        }

    def pull(self, package: str, *, tag: str | None = None, digest: str | None = None, destination) -> dict[str, Any]:
        """Resolve once, download one version and verify its graph before rename."""
        package = self._package(package)
        if (tag is None) == (digest is None):
            raise PackageError("native pull requires exactly one tag or digest")
        self.authorize(package, ("packages:read",))
        if tag is not None:
            resolved = self._resolve(package, _tag(tag))
            if resolved is None:
                raise PackageHTTPError(404, "NOT_FOUND")
            digest = resolved["digest"]
        digest = _digest(digest)
        metadata = self._json("GET", self._path(f"/versions/{digest}"), query={"package": package})
        self._bound(metadata, package)
        if metadata.get("root_digest") != digest or metadata.get("package_type") not in _PACKAGE_TYPES:
            raise PackageError("native version metadata root/type mismatch")
        archive_digest = _digest(metadata.get("archive_digest"), "archive digest")
        archive_size = _size(metadata.get("archive_size_bytes"), self.limits.max_archive_bytes)
        graph = metadata.get("graph")
        if not isinstance(graph, dict):
            raise PackageError("native version metadata lacks its verified graph")

        def verify(path: Path) -> None:
            with snapshot_package(path, digest, limits=self.limits) as snapshot:
                if (
                    snapshot.root_digest != digest
                    or snapshot.package_type != metadata["package_type"]
                    or snapshot.root_media_type != metadata.get("root_media_type")
                    or graph != snapshot.graph
                ):
                    raise PackageError("downloaded package graph differs from its version metadata")

        self._download(
            self._path(f"/versions/{digest}/download"), package, destination, archive_digest, archive_size, verify
        )
        return {**metadata, "digest": digest, **({"tag": tag} if tag is not None else {})}

    def _download(self, path: str, package: str, destination, digest: str, size: int, verify=None) -> Path:
        """Stream into a private sibling, verify, then atomically replace."""
        destination = Path(destination).expanduser().absolute()
        if destination.is_dir():
            raise PackageError("native download destination must be a file path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".palimpsest-download-", dir=destination.parent)
        temporary = Path(name)
        try:
            hasher = hashlib.sha256()
            received = 0
            with os.fdopen(fd, "wb") as output, self._open("GET", path, query={"package": package}) as response:
                if response.headers.get("Content-Encoding") not in (None, "identity"):
                    raise PackageError("native download used an unsupported content encoding")
                declared = response.headers.get("Content-Length")
                if declared is not None and declared != str(size):
                    raise PackageError("native download length differs from its bound size")
                while chunk := response.read(min(_CHUNK, size + 1 - received)):
                    received += len(chunk)
                    if received > size:
                        raise PackageError("native download exceeds its bound size")
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != size or "sha256:" + hasher.hexdigest() != digest:
                raise PackageError("native download digest/size mismatch")
            if verify is not None:
                verify(temporary)
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    # Build cache -----------------------------------------------------------

    def _partition(self, build_key, cache_scope, platform, builder_fingerprint) -> dict[str, str]:
        partition = {
            "build_key": _digest(build_key, "cache build key"),
            "cache_scope": cache_scope,
            "platform": platform,
            "builder_fingerprint": builder_fingerprint,
        }
        if (
            not isinstance(cache_scope, str)
            or _CACHE_SCOPE.fullmatch(cache_scope) is None
            or not isinstance(platform, str)
            or len(platform) > 64
            or _CACHE_PLATFORM.fullmatch(platform) is None
            or not isinstance(builder_fingerprint, str)
            or _BUILDER_FINGERPRINT.fullmatch(builder_fingerprint) is None
        ):
            raise PackageError("invalid native cache partition")
        return partition

    def _cache_receipt(
        self, receipt: dict[str, Any], package: str, partition: dict[str, str], *, exact_key: bool
    ) -> None:
        self._bound(receipt, package)
        _digest(receipt.get("archive_digest"), "cache archive digest")
        _size(receipt.get("archive_size_bytes"), self.limits.max_archive_bytes)
        _digest(receipt.get("build_key"), "cache build key")
        validate_keystone_id(receipt.get("created_by"), "cache creator")
        for field, value in partition.items():
            if (field != "build_key" or exact_key) and receipt.get(field) != value:
                raise PackageError(f"native cache receipt has mismatched {field}")

    def resolve_cache(
        self, package: str, *, build_key: str, cache_scope: str, platform: str, builder_fingerprint: str
    ) -> dict[str, Any] | None:
        """Exact-key then same-scope hit in this project/package; ``None`` only on envelope 404."""
        package = self._package(package)
        partition = self._partition(build_key, cache_scope, platform, builder_fingerprint)
        self.authorize(package, ("cache:read",))
        try:
            receipt = self._json("GET", self._path("/cache/resolve"), query={"package": package, **partition})
        except PackageHTTPError as exc:
            if exc.authoritative_missing:
                return None
            raise
        if receipt.get("resolution") not in {"exact", "scope"}:
            raise PackageError("native cache receipt has an invalid resolution")
        self._cache_receipt(receipt, package, partition, exact_key=receipt["resolution"] == "exact")
        self._cache_receipts[receipt["archive_digest"]] = dict(receipt)
        return receipt

    def pull_cache(self, package: str, receipt: dict[str, Any], destination) -> Path:
        """Download only an archive this client resolved, bounded by its receipt size."""
        package = self._package(package)
        if not isinstance(receipt, dict) or self._cache_receipts.get(receipt.get("archive_digest")) != receipt:
            raise PackageError("native cache pull requires a receipt returned by resolve_cache")
        if receipt.get("package") != package:
            raise PackageError("native cache receipt belongs to another package")
        self.authorize(package, ("cache:read",))
        return self._download(
            self._path(f"/cache/archives/{receipt['archive_digest']}"),
            package,
            destination,
            receipt["archive_digest"],
            receipt["archive_size_bytes"],
        )

    def push_cache(self, package: str, archive, descriptor: dict[str, Any]) -> dict[str, Any]:
        """Upload one frozen cache archive bound to this exact project/package."""
        package = self._package(package)
        if not isinstance(descriptor, dict) or descriptor.get("schema") != _CACHE_SCHEMA:
            raise PackageError("native cache descriptor has an unsupported schema")
        partition = self._partition(
            descriptor.get("build_key"),
            descriptor.get("cache_scope"),
            descriptor.get("platform"),
            descriptor.get("builder_fingerprint"),
        )
        if descriptor.get("oci_manifest_digest") is not None:  # optional image provenance, not the cache root
            _digest(descriptor["oci_manifest_digest"], "cache OCI manifest digest")
        self.authorize(package, ("cache:read", "cache:write"))
        expected = {"project_id": self.project_id, "namespace": self.namespace, "package": package, **partition}
        if any(descriptor.get(field) != expected[field] for field in _CACHE_BINDING):
            raise PackageError("native cache descriptor is bound to another project, namespace or package")
        with tempfile.TemporaryDirectory(prefix="palimpsest-cache-upload-") as directory:
            frozen = Path(directory).resolve(strict=True) / "cache.tar"
            source = Path(archive).expanduser().absolute()
            with _open_absolute_regular_file(source.parent.resolve(strict=True) / source.name) as (fd, _metadata):
                _copy_fd(fd, frozen, self.limits.max_archive_bytes)
            digest, size = _hash_file(frozen)
            body = {
                **partition,
                "archive_digest": digest,
                "archive_size_bytes": _size(size, self.limits.max_archive_bytes),
            }
            receipt = self._upload(package, frozen, body, cache=True)
        self._cache_receipt(receipt, package, partition, exact_key=True)
        if (
            receipt["archive_digest"] != digest
            or receipt["archive_size_bytes"] != size
            or receipt["created_by"] != self.owner_user_id
        ):
            raise PackageError("native cache receipt transport or creator mismatch")
        return receipt
