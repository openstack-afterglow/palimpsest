"""Publish verified local catalog images to the Hub and pull Hub-published images.

The local catalog is discovery metadata, never byte authority: publication
re-verifies every SquashFS payload, the image config and every OCI source
closure blob from their content stores, and pull verifies every downloaded
payload's SHA-256 digest and size before atomically publishing it into the
selected state root.

A publication may carry the original OCI source closure (root index, image
manifest, config and compressed layers). A pull rebuilds it as a standard OCI
image layout under the explicit state root and registers it as a local OCI
source exactly like a locally acquired image, so ``plm run`` works unchanged.

Credentials (image API keys or Keystone tokens) are only handed to
:class:`~palimpsest_local.hub.HubClient`. They never enter the catalog, the
command output or error text.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore, ArtifactStoreError
from .errors import HubError, PalimpsestError
from .hub import IMAGE_KEY_RE, HubClient, LocalPayloadIntegrityError
from .image_manifest import SCHEMA, manifest_digest, normalize_manifest, verify_public_manifest
from .local_catalog import CatalogError, canonical_hub_url, open_catalog
from .oci_image import strict_json_object
from .oci_provenance import (
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    SUPPORTED_IMAGE_INDEX_MEDIA_TYPES,
    SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES,
    Descriptor,
)
from .oci_source import SourceCAS, SourceSnapshot
from .state import StatePaths

IMAGE_KEY_ENV = "PALIMPSEST_IMAGE_KEY"
TOKEN_ENV = "PALIMPSEST_TOKEN"
VISIBILITIES = ("private", "internal", "public")
AUDIENCES = {
    "private": "you (the publishing user)",
    "internal": "every member of your project",
    "public": "anyone, anonymously",
}
# The Hub's PublicationRequest name rule; checked before any local or network effect.
NAME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}\Z")
# Upper bound on one pull's declared bytes (Hub caps are at most ~100 GiB per kind).
MAX_PULL_BYTES = 256 * 2**30
# The local OCI source intake (snapshot_acquisition) refuses larger acquisitions.
SOURCE_INTAKE_MAX_BYTES = 8 * 2**30
_FREE_SPACE_MARGIN = 256 * 2**20
# Abandoned pull staging directories older than this are swept (active pulls are far younger).
_STAGING_SWEEP_AGE = 24 * 3600
_READ_CHUNK = 1024 * 1024
_KEY_FILE_LIMIT = 4096
# Same bound as the Hub's closure document check (root/manifest/config).
_DOCUMENT_LIMIT = 2**20
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[\x21-\x7e]+\Z")
_SOURCE_ROLES = ("root", "manifest", "config", "layer")
# Only content-derived receipt facts are published; local store/CAS identities
# (store_id, occurrence and snapshot bindings) would make the same image
# published from two state roots produce two different manifests.
_PUBLISHED_RECEIPT_FIELDS = (
    "key_digest",
    "record_digest",
    "image_digest",
    "image_size",
    "filesystem",
    "ordinal",
    "source_image_digest",
)
_LAYOUT_MARKER = b'{"imageLayoutVersion":"1.0.0"}'
_TRANSPORT_ERRORS = (HubError, OSError, http.client.HTTPException)


class PublicationError(PalimpsestError):
    """An actionable Hub publication or pull failure; never contains credentials."""


@dataclass(frozen=True)
class HubCredential:
    """Selected credential form; the secret is excluded from repr."""

    kind: str
    secret: str | None = field(default=None, repr=False)

    def client(self, hub_url: str, *, timeout_seconds: float = 300.0) -> HubClient:
        return HubClient(hub_url, self.secret, credential=self.kind, timeout_seconds=timeout_seconds)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_key_file(path: Path) -> str:
    """Read one owner-only regular key file without following a final symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise PublicationError("image API key file cannot be opened (it must be a regular file)") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PublicationError("image API key file must be an owner-only (0600) regular file owned by you")
        if info.st_size > _KEY_FILE_LIMIT:
            raise PublicationError("image API key file is too large")
        payload = os.read(fd, _KEY_FILE_LIMIT + 1)
    finally:
        os.close(fd)
    try:
        return payload.decode("ascii").strip()
    except UnicodeDecodeError:
        raise PublicationError("image API key file must contain one ASCII ppi_v1_ key") from None


def resolve_credential(
    environment: Mapping[str, str],
    *,
    key_file: Path | None = None,
    allow_anonymous: bool,
    anonymous: bool = False,
) -> HubCredential:
    """Select exactly one credential: key file, ``PALIMPSEST_IMAGE_KEY``, then ``PALIMPSEST_TOKEN``.

    Pull may fall back to anonymous public access; publication never does.
    Error text never repeats a credential value.
    """
    if anonymous:
        if not allow_anonymous:
            raise PublicationError("Hub publication always requires credentials")
        if key_file is not None:
            raise PublicationError("--anonymous cannot be combined with --image-key-file")
        return HubCredential("anonymous")
    if key_file is not None:
        kind, secret = "image-key", _read_key_file(key_file)
    elif environment.get(IMAGE_KEY_ENV):
        kind, secret = "image-key", environment[IMAGE_KEY_ENV].strip()
    elif environment.get(TOKEN_ENV):
        kind, secret = "keystone", environment[TOKEN_ENV].strip()
    elif allow_anonymous:
        return HubCredential("anonymous")
    else:
        raise PublicationError(
            f"Hub publication requires credentials: set {IMAGE_KEY_ENV} to an image API key (ppi_v1_...), "
            f"pass --image-key-file, or set {TOKEN_ENV} to a Keystone token"
        )
    if kind == "image-key" and IMAGE_KEY_RE.fullmatch(secret) is None:
        raise PublicationError("image API key is malformed; expected one complete ppi_v1_ key")
    if kind == "keystone" and _TOKEN.fullmatch(secret) is None:
        raise PublicationError(f"{TOKEN_ENV} is malformed: a token must be printable ASCII without spaces")
    return HubCredential(kind, secret)


def selected_hub_url(value: str) -> str:
    try:
        return canonical_hub_url(value)
    except CatalogError as exc:
        raise PublicationError(str(exc)) from None


def require_secure_transport(hub_url: str, credential: HubCredential, *, allow_insecure_http: bool) -> None:
    """Never send a credential over plain HTTP to a non-loopback host without an explicit opt-in."""
    if credential.kind == "anonymous" or not hub_url.startswith("http://") or allow_insecure_http:
        return
    host = hub_url[len("http://") :].split("/", 1)[0]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.rsplit(":", 1)[0]
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        raise PublicationError(
            "refusing to send Hub credentials over plain http:// to a non-loopback host; use https:// or pass "
            "--allow-insecure-http to accept that risk explicitly"
        )


def _gib(value: int) -> str:
    return f"{value / 2**30:.2f} GiB"


def _safe_text(value: Any, redact: Callable[[str], str]) -> str:
    text = redact(value if isinstance(value, str) else "")
    return "".join(character if character.isprintable() else "?" for character in text)[:200]


def _validation_messages(detail: Any, redact: Callable[[str], str]) -> str:
    """Render FastAPI 422 ``detail[]`` entries as ``field: message``."""
    messages = []
    for item in detail[:5] if isinstance(detail, list) else ():
        if not isinstance(item, dict):
            continue
        location = item.get("loc") if isinstance(item.get("loc"), list) else []
        where = ".".join(str(part) for part in location[1:] if isinstance(part, (str, int)))
        message = _safe_text(item.get("msg"), redact)
        if message:
            messages.append(f"{_safe_text(where, redact)}: {message}" if where else message)
    return "; ".join(messages)


def _request_id_conflict(exc: BaseException) -> bool:
    detail = getattr(exc, "detail", None)
    return getattr(exc, "http_status", None) == 409 and isinstance(detail, str) and "request_id conflicts" in detail


def describe_hub_error(
    exc: BaseException,
    *,
    action: str,
    visibility: str | None = None,
    redact: Callable[[str], str] = lambda text: text,
) -> str:
    """Map the Hub error contract to actionable text built from structured fields only."""
    status = getattr(exc, "http_status", None)
    detail = getattr(exc, "detail", None)
    code = detail.get("code") if isinstance(detail, dict) else None

    def number(key: str) -> int | None:
        value = detail.get(key) if isinstance(detail, dict) else None
        return value if type(value) is int else None

    def selected_visibility() -> str | None:
        return detail.get("visibility") if detail.get("visibility") in VISIBILITIES else visibility

    if status == 401:
        return (
            "Hub rejected the credential (HTTP 401): it is missing, malformed, expired, revoked, or no longer "
            f"valid for its project. Set a valid {IMAGE_KEY_ENV} or {TOKEN_ENV} and retry."
        )
    if status in (301, 302, 303, 307, 308):
        return (
            f"Hub answered with a redirect (HTTP {status}); redirects are never followed so credentials cannot "
            "leak. Use the final Hub URL directly with --url or PALIMPSEST_URL."
        )
    if status == 403 and code == "image_count_limit":
        scope = detail.get("scope") if detail.get("scope") in ("user", "project") else "scope"
        return (
            f"Hub image count limit reached: {selected_visibility()} images per {scope} are limited to "
            f"{number('limit')} names and {number('used')} are in use. Publishing a new digest under an existing "
            "name in the same scope does not count again; otherwise remove an image, choose another visibility, "
            "or ask a Hub administrator to raise the limit."
        )
    if status == 413 and code in ("image_chain_size_limit", "image_source_size_limit"):
        kind = "layer payloads" if code == "image_chain_size_limit" else "OCI source closure blobs"
        observed = number("chain_bytes" if code == "image_chain_size_limit" else "source_bytes")
        limit = number("limit_bytes")
        sizes = (
            f"{observed} bytes ({_gib(observed)}) exceed the {selected_visibility()} limit of {limit} bytes "
            f"({_gib(limit)})"
            if limit is not None and observed is not None
            else f"exceed the {selected_visibility()} limit"
        )
        label = "chain" if code == "image_chain_size_limit" else "source"
        hint = " or publish with --no-source" if code == "image_source_size_limit" else ""
        return (
            f"Hub image {label} size limit exceeded: the image's distinct {kind} {sizes}. Reduce the image, "
            f"choose another visibility{hint}, or ask a Hub administrator for a larger tier."
        )
    if status == 413:
        return "Hub refused the image because it exceeds the server's maximum image stack size (HTTP 413)."
    if status == 403:
        return (
            f"Hub denied the {action} (HTTP 403): the credential is valid but not allowed to do this. Image keys "
            "need images:publish to publish and images:read to pull, and the key's issuing user must still belong "
            "to its project."
        )
    if status == 404:
        if action == "pull":
            return (
                "Hub image was not found or is not visible to this credential (HTTP 404); anonymous access can "
                "read only public images."
            )
        return "Hub did not find a referenced payload or image (HTTP 404)."
    if status == 409:
        if _request_id_conflict(exc):
            return (
                "Hub reports that this publication request id already belongs to a different publication or user "
                "(HTTP 409); the next attempt uses a new request id."
            )
        return f"Hub reported a conflict (HTTP 409): {_safe_text(detail, redact) or 'request conflicts'}."
    if status == 422:
        rendered = _validation_messages(detail, redact) or _safe_text(detail, redact) or "request rejected"
        return f"Hub rejected the {action} request (HTTP 422): {rendered}."
    if status == 429:
        retry = getattr(exc, "retry_after", None)
        return "Hub rate limit reached (HTTP 429); " + (
            f"retry after {retry} seconds." if type(retry) is int else "retry later."
        )
    if isinstance(status, int) and status >= 500:
        return f"Hub failed while handling the {action} (HTTP {status}); the outcome is uncertain."
    if isinstance(status, int):
        return f"Hub refused the {action} (HTTP {status}): {_safe_text(detail, redact) or 'request rejected'}."
    if getattr(exc, "transport_timeout", False) or isinstance(exc, TimeoutError):
        return f"Hub did not answer the {action} before the timeout; the outcome is uncertain."
    if isinstance(exc, http.client.HTTPException):
        return f"Hub connection broke during the {action} ({type(exc).__name__}); the outcome is uncertain."
    return f"Hub {action} failed: {_safe_text(str(exc), redact) or type(exc).__name__}."


def _outcome(exc: BaseException) -> tuple[str, str, bool]:
    """``(status, error_code, terminal)``: only a definitive 4xx refusal is ``failed``.

    A 409 request_id conflict is terminal for that request id: retrying it can
    never succeed, so the next attempt starts a new generation.
    """
    status = getattr(exc, "http_status", None)
    if isinstance(exc, HubError) and type(status) is int:
        if _request_id_conflict(exc):
            return "failed", "http_409:request_id_conflict", True
        detail = getattr(exc, "detail", None)
        code = detail.get("code") if isinstance(detail, dict) else None
        label = f"http_{status}" + (f":{code}" if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "")
        return ("failed" if 400 <= status < 500 and status != 408 else "unknown"), label, False
    if getattr(exc, "transport_timeout", False) or isinstance(exc, TimeoutError):
        return "unknown", "timeout", False
    if isinstance(exc, _TRANSPORT_ERRORS):
        return "unknown", "transport", False
    return "unknown", "interrupted", False


def _image_config(roots: StatePaths, image: dict) -> dict:
    descriptor = image["config_descriptor"]
    try:
        cas = SourceCAS.open_existing(roots.oci_source_cas, expected_cas_id=image["source_cas_id"])
        payload = cas.read_metadata(
            SourceSnapshot(Descriptor(descriptor["mediaType"], descriptor["digest"], descriptor["size"]), cas.identity)
        )
        config = strict_json_object(payload, "verified image config")
    except (PalimpsestError, OSError, TypeError, ValueError):
        raise PublicationError("local image config is unavailable or no longer matches its verified source") from None
    if config.get("os") != image["platform"]["os"] or config.get("architecture") != image["platform"]["architecture"]:
        raise PublicationError("local image config platform differs from its catalog platform")
    return config


def local_image_manifest(
    roots: StatePaths,
    image: dict,
    *,
    os_family: str,
    os_version: str,
    metadata: dict | None = None,
) -> tuple[dict, dict[str, Path]]:
    """Build the canonical manifest and verified payload paths for one complete catalog image.

    Layers are the per-occurrence SquashFS conversions in original OCI order,
    including the original base layers; each keeps its original compressed
    descriptor/DiffID and the content-derived conversion receipt facts.
    """
    if image.get("materialization_status") != "complete" or not image.get("source_binding_verified"):
        raise PublicationError(
            "local image is not completely materialized from verified source bytes; materialize it before publishing"
        )
    layers, derived = image["layers"], image["derived"]
    if not layers or len(layers) != len(derived):
        raise PublicationError("local image lacks one conversion receipt per original layer")
    config = _image_config(roots, image)
    runtime_config = config.get("config") if isinstance(config.get("config"), dict) else {}
    store = ArtifactStore(roots.store)
    manifest_layers, blobs = [], {}
    for layer, receipt in zip(layers, derived, strict=True):
        if receipt["ordinal"] != layer["ordinal"] or receipt["source_image_digest"] != image["image_id"]:
            raise PublicationError("local conversion receipts are not in original layer order")
        digest, size = receipt["image_digest"], receipt["image_size"]
        try:
            stored = store.verify_blob(digest)
        except ArtifactStoreError:
            raise PublicationError(f"local layer payload {digest} is missing or corrupt") from None
        if stored.size != size:
            raise PublicationError(f"local layer payload {digest} size differs from its receipt")
        blobs[digest] = roots.store / "blobs" / "sha256" / digest.split(":", 1)[1]
        manifest_layers.append(
            {
                "digest": digest,
                "size_bytes": size,
                "source": {
                    "oci_layer": layer,
                    "conversion": {"receipt": {key: receipt[key] for key in _PUBLISHED_RECEIPT_FIELDS}},
                },
            }
        )
    reference = image["reference"]
    manifest = {
        "schema": SCHEMA,
        "mode": "oci-root",
        "platform": f"{image['platform']['os']}/{image['platform']['architecture']}",
        "os_family": os_family,
        "os_version": os_version,
        "base_image_digest": None,
        "runtime": {
            "entrypoint": runtime_config.get("Entrypoint") or [],
            "cmd": runtime_config.get("Cmd") or [],
            "env": runtime_config.get("Env") or [],
            "working_dir": runtime_config.get("WorkingDir") or "/",
            "user": runtime_config.get("User") or "0",
        },
        "source": {
            "kind": "local",
            "registry": reference["registry"],
            "repository": reference["repository"],
            "reference": reference["requested_reference"],
            "root_digest": image["root_descriptor"]["digest"],
            "manifest_digest": image["manifest_descriptor"]["digest"],
            "config_digest": image["config_descriptor"]["digest"],
            "diff_ids": [layer["diff_id"] for layer in layers],
            "metadata": {"local_image_id": image["image_id"]},
        },
        "metadata": metadata or {},
        "layers": manifest_layers,
    }
    try:
        return normalize_manifest(manifest), blobs
    except ValueError as exc:
        raise PublicationError(f"local image cannot be described as a publishable manifest: {exc}") from None


def _hash_file(path: Path, size: int) -> str | None:
    """SHA-256 of one regular, non-symlink file of exactly ``size`` bytes; ``None`` otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return None
        sha, total = hashlib.sha256(), 0
        while chunk := stream.read(_READ_CHUNK):
            sha.update(chunk)
            total += len(chunk)
        return "sha256:" + sha.hexdigest() if total == size else None


def local_source_closure(roots: StatePaths, image: dict) -> tuple[list[dict], dict[str, Path]]:
    """The original OCI closure from the image's own source CAS, every blob re-verified."""
    try:
        SourceCAS.open_existing(roots.oci_source_cas, expected_cas_id=image["source_cas_id"])
    except (PalimpsestError, OSError, TypeError, ValueError):
        raise PublicationError("local OCI source CAS is unavailable; re-acquire the original source") from None
    closure = []
    if image["root_descriptor"] != image["manifest_descriptor"]:
        closure.append(("root", 0, image["root_descriptor"]))
    closure += [("manifest", 0, image["manifest_descriptor"]), ("config", 0, image["config_descriptor"])]
    closure += [("layer", layer["ordinal"], layer["compressed"]) for layer in image["layers"]]
    items, blobs = [], {}
    for role, ordinal, descriptor in closure:
        digest, size = descriptor["digest"], descriptor["size"]
        if digest not in blobs:
            path = roots.oci_source_cas / "blobs" / "sha256" / digest.split(":", 1)[1]
            if size < 1 or _hash_file(path, size) != digest:
                raise PublicationError(
                    f"local OCI source blob {digest} is missing or corrupt; re-acquire the source or use --no-source"
                )
            blobs[digest] = path
        items.append({"role": role, "digest": digest, "size_bytes": size, "ordinal": ordinal})
    return items, blobs


def _response_mismatch(response: Any, manifest: dict, digest: str, name: str, visibility: str) -> str | None:
    if not isinstance(response, dict):
        return "malformed response"
    expected_layers = [
        (item["id"], item["parent_id"], item["digest"], item["size_bytes"]) for item in manifest["layers"]
    ]
    public = response.get("manifest")
    public_layers = public.get("layers") if isinstance(public, dict) else None
    observed_layers = None
    if isinstance(public_layers, list) and all(isinstance(item, dict) for item in public_layers):
        observed_layers = [
            tuple(item.get(key) for key in ("id", "parent_id", "digest", "size_bytes")) for item in public_layers
        ]
    checks = (
        (response.get("manifest_digest") == digest, "manifest digest"),
        (response.get("name") == name, "name"),
        (response.get("visibility") == visibility, "visibility"),
        (response.get("leaf_id") == manifest["layers"][-1]["id"], "leaf layer"),
        (observed_layers is not None and len(observed_layers) == len(expected_layers), "layer count"),
        (observed_layers == expected_layers, "layer chain"),
        (isinstance(response.get("project_id"), str) and bool(response.get("project_id")), "owner project"),
    )
    return next((label for passed, label in checks if not passed), None)


def _closure_key(items: Any) -> list[tuple] | None:
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    return sorted((item.get("role"), item.get("ordinal"), item.get("digest"), item.get("size_bytes")) for item in items)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def _settle(roots: StatePaths, request_id: str, *, attempts: int = 1, **outcome: Any) -> dict | None:
    """Record an outcome; ``None`` means it could not be recorded and the row stays ``pending``."""
    for attempt in range(attempts):
        try:
            with open_catalog(roots, create=False) as catalog:
                if catalog is not None:
                    return catalog.settle_publication(request_id, now=_now(), **outcome)
        except CatalogError:
            pass
        if attempt + 1 < attempts:
            time.sleep(0.2 * (attempt + 1))
    return None


def _matches(value: Any, row: dict, digest: str, name: str, visibility: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("id") == row["hub_image_id"]
        and value.get("manifest_digest") == digest
        and value.get("name") == name
        and value.get("visibility") == visibility
        and value.get("project_id") == row["hub_project_id"]
        and _closure_key(value.get("source_closure") or []) == _closure_key(row["source_closure"] or [])
    )


def _not_revalidated(exc: BaseException, client: HubClient, how: str) -> PublicationError:
    return PublicationError(
        describe_hub_error(exc, action="revalidation", redact=client._redact)
        + f" The stored publication could not be revalidated {how}; nothing changed."
    )


def _read_back(client: HubClient, row: dict, digest: str, name: str, visibility: str) -> str:
    """Read one ``published`` row with the CURRENT credential: ``valid``, ``stale`` or ``forbidden``.

    Only "not visible" (404) or a differing record makes the row stale. An
    authenticated-but-forbidden read (403, for example an image key with
    ``images:publish`` but not ``images:read``) is ``forbidden``.
    """
    try:
        value = client.get_published_image(row["hub_image_id"])
    except HubError as exc:
        status = getattr(exc, "http_status", None)
        if status == 404:
            return "stale"
        if status == 403:
            return "forbidden"
        raise _not_revalidated(exc, client, "by reading it") from None
    except (OSError, http.client.HTTPException) as exc:
        raise _not_revalidated(exc, client, "by reading it") from None
    return "valid" if _matches(value, row, digest, name, visibility) else "stale"


def _revalidate_generations(
    roots: StatePaths,
    client: HubClient,
    rows: list[dict],
    manifest: dict,
    digest: str,
    name: str,
    visibility: str,
    intent: dict,
) -> tuple[dict | None, str | None]:
    """Return ``(row, inherited_request_id)``: the ``published`` row this credential may reuse, if any.

    ``rows`` are the generations visible to this credential (every ``published`` row plus its own
    unpublished attempts). Every published row is read first with the current credential. A credential that
    may publish but not read (HTTP 403, for example an image key without ``images:read``) cannot read any row
    back, so it only tracks publications it made itself: it replays the stored Hub request id of ONE row
    that this very image key recorded (replays are idempotent per Hub project and user). Rows of other
    credentials are never replayed or adopted, because that registers an image in this credential's project
    for a request id of someone else's. A credential without a public key id that cannot read refuses.
    While this credential has ANY unresolved (unpublished, non-terminal) attempt, no replay runs: the normal
    publication retries it. The second value, set only when the replay could not be confirmed (HTTP 404),
    is the Hub request id the new generation must send so the Hub answers an already committed image.
    """
    key_id = client.public_credential_id
    own = []
    for row in rows:
        if row["status"] != "published":
            continue
        outcome = _read_back(client, row, digest, name, visibility)
        if outcome == "valid":
            return row, None
        if outcome == "forbidden" and not row["terminal"]:
            if key_id is None:
                raise PublicationError(
                    "this credential may publish but not read images and has no image-key id to track its own "
                    "publications, so an earlier publication of this image cannot be revalidated. Nothing "
                    "changed. Re-run with a credential that can read images (images:read) or an image key."
                )
            if row["hub_credential"] == key_id:
                own.append(row)
    if not own:
        return None, None
    if any(row["status"] != "published" and not row["terminal"] for row in rows):
        # Every visible unpublished row is this credential's own unresolved attempt: retry it, never replay.
        return None, None
    return _replay(roots, client, own[0], manifest, digest, name, visibility, intent)


def _replay_failed(client: HubClient, exc: BaseException) -> PublicationError:
    definitive = isinstance(exc, HubError) and _outcome(exc)[0] == "failed"
    tail = (
        " The Hub refused the replay before registering anything; no new publication was started."
        if definitive
        else " Replaying the stored request id may have reached the Hub; the outcome is unknown. Re-running the "
        "same command replays the same request id, which the Hub answers idempotently."
    )
    return PublicationError(describe_hub_error(exc, action="revalidation", redact=client._redact) + tail)


def _replay(
    roots: StatePaths,
    client: HubClient,
    row: dict,
    manifest: dict,
    digest: str,
    name: str,
    visibility: str,
    intent: dict,
) -> tuple[dict | None, str | None]:
    """Replay the Hub request id of a row this image key recorded; see ``_revalidate_generations``."""
    hub_request_id = row["hub_request_id"] or row["request_id"]
    try:
        response = client.publish_image(
            manifest,
            name=name,
            request_id=hub_request_id,
            visibility=visibility,
            source_closure=row["source_closure"],
        )
    except HubError as exc:
        if _request_id_conflict(exc):
            try:
                with open_catalog(roots, create=False) as catalog:
                    catalog.mark_publication_terminal(row["request_id"], now=_now())
            except CatalogError:
                raise PublicationError(
                    "the stored request id conflicts on the Hub, but that could not be recorded locally; re-run"
                ) from None
            return None, None
        if getattr(exc, "http_status", None) == 404:
            # Unknown (project, request id), or this key never staged the payloads: not conclusive. A normal
            # publication that re-sends the same Hub request id registers nothing the Hub already holds.
            return None, hub_request_id
        raise _replay_failed(client, exc) from None
    except (OSError, http.client.HTTPException) as exc:
        raise _replay_failed(client, exc) from None
    hub_image_id = _uuid(response.get("id")) if isinstance(response, dict) else None
    if (
        _response_mismatch(response, manifest, digest, name, visibility) is not None
        or hub_image_id is None
        or _closure_key(response.get("source_closure") or []) != _closure_key(row["source_closure"] or [])
    ):
        # Not this intent: the row is stale and a new generation publishes normally.
        return None, None
    project = response["project_id"]
    if project != row["hub_project_id"]:
        raise PublicationError(
            "the Hub answered the stored request id with an image of another project than the one recorded for "
            "it; refusing to guess. Nothing was recorded."
        )
    key_id = client.public_credential_id
    if hub_image_id == row["hub_image_id"] and row["hub_credential"] == key_id:
        return row, None
    created_by = response.get("created_by")
    created_by = created_by if isinstance(created_by, str) and created_by else None
    try:
        with open_catalog(roots, create=False) as catalog:
            # Same project, re-registered image (for example after a Hub-side delete): rebind.
            return (
                catalog.rebind_publication(
                    row["request_id"],
                    hub_image_id=hub_image_id,
                    hub_project_id=project,
                    hub_created_by=created_by,
                    hub_credential=key_id,
                    now=_now(),
                ),
                None,
            )
    except CatalogError:
        raise PublicationError(
            f"the Hub holds image {hub_image_id} for this publication, but it could not be recorded locally; "
            "re-run the command"
        ) from None


def publish_local_image(
    roots: StatePaths,
    image_id: str,
    *,
    client: HubClient,
    hub_url: str,
    name: str,
    visibility: str = "private",
    os_family: str = "unknown",
    os_version: str = "unknown",
    include_source: bool = True,
    confirm_public: bool = False,
    notify: Callable[[str], None] = lambda message: None,
) -> dict:
    """Publish one complete local image and record its verified Hub result.

    The request_id is durably recorded before any network call and reused by
    every retry of the same intent. ``published`` is recorded only after the
    Hub's authoritative response matches the locally computed manifest. Every
    stored ``published`` generation of the intent is revalidated with the
    current credential, newest first, and the first that still holds is
    reused; a new generation is created only when none does.
    """
    if visibility not in VISIBILITIES:
        raise PublicationError("visibility must be private, internal or public")
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise PublicationError(
            "Hub image name must match ^[a-z0-9][a-z0-9.+-]{0,63}$ (lowercase letters, digits, '.', '+', '-')"
        )
    if visibility == "public" and not confirm_public:
        raise PublicationError(
            "--visibility public makes the image (and, with its source, its config environment values) readable "
            "by anyone anonymously; pass --confirm-public to proceed"
        )
    with open_catalog(roots, create=False) as catalog:
        if catalog is None:
            raise PublicationError("the selected state root has no local image catalog")
        image = next((item for item in catalog.read_images() if item["image_id"] == image_id), None)
    if image is None:
        raise PublicationError("local image is not registered in the selected state root's catalog")
    manifest, blobs = local_image_manifest(roots, image, os_family=os_family, os_version=os_version)
    closure, source_blobs = local_source_closure(roots, image) if include_source else (None, {})
    digest = manifest_digest(manifest)
    intent = {
        "image_id": image_id,
        "hub_url": hub_url,
        "name": name,
        "visibility": visibility,
        "manifest_digest": digest,
        "source_closure": closure,
    }

    def public_row(row: dict) -> dict:
        return {key: value for key, value in row.items() if key != "prior_error_code"}

    key_id = client.public_credential_id
    with open_catalog(roots, create=False) as catalog:
        generations = catalog.intent_publications(**intent, credential=key_id)
    reused, inherited = _revalidate_generations(roots, client, generations, manifest, digest, name, visibility, intent)
    if reused is not None:
        return public_row(reused)
    newest = generations[0] if generations else None
    stale = newest["request_id"] if newest is not None and newest["status"] == "published" else None
    with open_catalog(roots, create=False) as catalog:
        publication = catalog.begin_publication(
            **intent, now=_now(), stale_request_id=stale, credential=key_id, inherit_hub_request_id=inherited
        )
    if publication["status"] == "published":
        # Another run published this intent between our revalidation and begin.
        reused, _ = _revalidate_generations(roots, client, [publication], manifest, digest, name, visibility, intent)
        if reused is not None:
            return public_row(reused)
        raise PublicationError(
            "another run published this image concurrently, but that publication does not revalidate with this "
            "credential; nothing was registered. Re-run the command."
        )
    request_id = publication["request_id"]
    if closure is not None:
        notify(
            "Publishing the original OCI source closure with this image: its config, including environment "
            f"variable VALUES, becomes readable by {AUDIENCES[visibility]}. Use --no-source to publish layers "
            "and metadata only (not runnable after pull)."
        )
    try:
        response = client.push_image(
            manifest,
            blobs,
            name=name,
            request_id=publication["hub_request_id"] or request_id,
            visibility=visibility,
            source_closure=closure,
            source_blobs=source_blobs,
        )
    except LocalPayloadIntegrityError as exc:
        # Registration was never requested: this is a local failure, not a Hub outcome.
        recorded = _settle(roots, request_id, status="failed", error_code="local_integrity", hub_credential=key_id)
        raise PublicationError(
            f"local integrity error: {exc}. The local bytes changed after they were verified, so nothing was "
            "registered on the Hub; re-materialize or re-acquire the local image and retry."
            + ("" if recorded is not None else f" Publication {request_id} stays pending locally.")
        ) from None
    except BaseException as exc:
        status, code, terminal = _outcome(exc)
        recorded = _settle(roots, request_id, status=status, error_code=code, terminal=terminal, hub_credential=key_id)
        if isinstance(exc, _TRANSPORT_ERRORS):
            message = describe_hub_error(exc, action="publication", visibility=visibility, redact=client._redact)
            if recorded is None:
                tail = f" The outcome could not be recorded locally; publication {request_id} stays pending."
            else:
                tail = f" Local publication {request_id} is recorded as {status}" + (
                    "; retrying the same command reuses this request id."
                    if status == "unknown"
                    else "; the next attempt uses a new request id."
                    if terminal
                    else "."
                )
            raise PublicationError(message + tail) from None
        raise
    mismatch = _response_mismatch(response, manifest, digest, name, visibility)
    hub_image_id = _uuid(response.get("id")) if isinstance(response, dict) else None
    if mismatch is not None or hub_image_id is None:
        # A repeated mismatch for the same request id will never resolve by retrying it.
        terminal = publication.get("prior_error_code") == "response_mismatch"
        _settle(
            roots,
            request_id,
            status="unknown",
            hub_image_id=hub_image_id,
            error_code="response_mismatch",
            terminal=terminal,
            hub_credential=key_id,
        )
        raise PublicationError(
            f"Hub response does not match the local manifest ({mismatch or 'image id'}); local publication "
            f"{request_id} is recorded as unknown, not published"
            + ("; the next attempt uses a new request id." if terminal else ".")
        )
    created_by = response.get("created_by")
    settled = _settle(
        roots,
        request_id,
        attempts=3,
        status="published",
        hub_image_id=hub_image_id,
        hub_project_id=response["project_id"],
        hub_created_by=created_by if isinstance(created_by, str) and created_by else None,
        hub_credential=key_id,
    )
    if settled is None:
        raise PublicationError(
            f"Hub published image {hub_image_id} matches the local manifest, but its local status could not be "
            "recorded (it stays pending); re-running the same command reconciles it."
        )
    return settled


def _source_closure(value: Any, public: dict) -> list[dict]:
    """Validate the Hub's closure descriptors and their mandatory manifest commitments.

    Every closure must be committed to by the verified public manifest: its
    source manifest/config (and root) digests and, BY ORDINAL with equal counts,
    each layer's original compressed digest and size. Document sizes are bounded
    before anything is downloaded.
    """
    if value in (None, []):
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 300:
        raise PublicationError("Hub image source closure is malformed")
    items, sizes = {}, {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "digest", "size_bytes", "ordinal"}:
            raise PublicationError("Hub image source closure item is malformed")
        role, digest, size, ordinal = item["role"], item["digest"], item["size_bytes"], item["ordinal"]
        if (
            role not in _SOURCE_ROLES
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or type(size) is not int
            or not 0 < size < 2**63
            or type(ordinal) is not int
            or (role, ordinal) in items
            or sizes.setdefault(digest, size) != size
        ):
            raise PublicationError("Hub image source closure item is malformed")
        if role != "layer" and size > _DOCUMENT_LIMIT:
            raise PublicationError(
                f"Hub image source {role} document declares {size} bytes, above the {_DOCUMENT_LIMIT}-byte limit; "
                "nothing was downloaded"
            )
        items[(role, ordinal)] = {"role": role, "digest": digest, "size_bytes": size, "ordinal": ordinal}
    ordinals = {role: sorted(ordinal for kind, ordinal in items if kind == role) for role in _SOURCE_ROLES}
    if (
        ordinals["manifest"] != [0]
        or ordinals["config"] != [0]
        or ordinals["root"] not in ([], [0])
        or ordinals["layer"] != list(range(len(ordinals["layer"])))
        or not ordinals["layer"]
    ):
        raise PublicationError("Hub image source closure roles are incomplete")
    source = public.get("source") if isinstance(public.get("source"), dict) else {}
    root = items.get(("root", 0))
    commitments = [
        ("manifest_digest", items[("manifest", 0)]["digest"]),
        ("config_digest", items[("config", 0)]["digest"]),
    ]
    if root is not None:
        commitments.append(("root_digest", root["digest"]))
    for key, expected in commitments:
        if source.get(key) != expected:
            raise PublicationError(f"Hub image manifest does not commit to its source closure ({key})")
    if "root_digest" in source and source["root_digest"] != (root or items[("manifest", 0)])["digest"]:
        raise PublicationError("Hub image manifest does not commit to its source closure (root_digest)")
    closure_layers = [items[("layer", ordinal)] for ordinal in ordinals["layer"]]
    if len(public["layers"]) != len(closure_layers):
        raise PublicationError("Hub image source closure layer count differs from its manifest layers")
    for layer, item in zip(public["layers"], closure_layers, strict=True):
        origin = layer.get("source") if isinstance(layer.get("source"), dict) else {}
        if (origin.get("compressed_digest"), origin.get("compressed_size")) != (item["digest"], item["size_bytes"]):
            raise PublicationError(
                f"Hub image manifest does not commit to source closure layer {item['ordinal']} (digest and size)"
            )
    if "diff_ids" in source and (
        not isinstance(source["diff_ids"], list) or len(source["diff_ids"]) != len(closure_layers)
    ):
        raise PublicationError("Hub image manifest DiffIDs do not match its source closure layer count")
    return [items[key] for key in sorted(items, key=lambda key: (_SOURCE_ROLES.index(key[0]), key[1]))]


def _pull_record(value: Any, hub_image_id: str, hub_url: str, expected_name: str | None) -> dict:
    if not isinstance(value, dict) or value.get("id") != hub_image_id:
        raise PublicationError("Hub returned a different or malformed image record")
    if expected_name is not None and value.get("name") != expected_name:
        raise PublicationError("Hub image name differs from the requested --name")
    try:
        layers = verify_public_manifest(value.get("manifest"))
    except (ValueError, TypeError, KeyError) as exc:
        raise PublicationError(f"Hub image manifest failed verification: {exc}") from None
    digest = value.get("manifest_digest")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or value.get("leaf_id") != layers[-1]["id"]
        or value.get("visibility") not in VISIBILITIES
        or not isinstance(value.get("name"), str)
        or not 0 < len(value["name"]) <= 256
        or not value["name"].isprintable()
    ):
        raise PublicationError("Hub image identity fields are malformed or contradict its layer chain")
    manifest = value["manifest"]
    closure = _source_closure(value.get("source_closure"), manifest)
    return {
        "hub_url": hub_url,
        "hub_image_id": hub_image_id,
        "name": value["name"],
        "visibility": value["visibility"],
        "manifest_digest": digest,
        "leaf_id": value["leaf_id"],
        "mode": manifest["mode"],
        "platform": manifest["platform"],
        "manifest": manifest,
        "source_closure": closure,
        "layers": layers,
        "pulled_at": _now(),
    }


def _same_pull(existing: dict, record: dict) -> bool:
    keys = ("name", "visibility", "manifest_digest", "leaf_id", "mode", "platform", "layers", "source_closure")
    return all(existing[key] == record[key] for key in keys)


def _stream(client: HubClient, hub_image_id: str, digest: str, size: int) -> Iterator[bytes]:
    """Yield exactly ``size`` bytes of one Hub payload; raise before yielding any excess."""
    try:
        response = client.open_published_image_blob(hub_image_id, digest)
    except _TRANSPORT_ERRORS as exc:
        raise PublicationError(describe_hub_error(exc, action="pull", redact=client._redact)) from None
    with response:
        total = 0
        try:
            while chunk := response.read(_READ_CHUNK):
                total += len(chunk)
                if total > size:
                    raise PublicationError(f"Hub payload {digest} is larger than its manifest size; nothing was stored")
                yield chunk
        except (OSError, http.client.HTTPException) as exc:
            raise PublicationError(
                f"Hub payload {digest} transfer failed ({type(exc).__name__}); nothing was stored"
            ) from None
        if total != size:
            raise PublicationError(f"Hub payload {digest} is shorter than its manifest size; nothing was stored")


def _fetch_payload(client: HubClient, store: ArtifactStore, hub_image_id: str, digest: str, size: int) -> bool:
    """Atomically store one verified payload; return whether bytes were downloaded."""
    if _payload_present(store, digest, size):
        return False
    try:
        store.publish_blob(_stream(client, hub_image_id, digest, size), expected_digest=digest)
    except ArtifactStoreError as exc:
        if exc.code == "artifact-digest":
            raise PublicationError(f"Hub payload {digest} failed SHA-256 verification; nothing was stored") from None
        raise PublicationError(
            f"Hub payload {digest} transfer or local store write failed; nothing was stored"
        ) from None
    return True


def _payload_present(store: ArtifactStore, digest: str, size: int) -> bool:
    try:
        return store.verify_blob(digest).size == size
    except ArtifactStoreError as exc:
        if exc.code not in ("artifact-missing", "artifact-corrupt"):
            raise PublicationError("local layer store cannot be verified") from None
        return False


def _private_directory(path: Path) -> None:
    """Create or accept one owner-only real directory (never a symlink)."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PublicationError(f"{path.name} must be an owner-only real directory")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file(directory: Path, name: str, chunks: Iterator[bytes] | list[bytes], digest: str | None = None) -> None:
    """Write a new read-only file atomically inside a private staging directory."""
    temporary = directory / f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        sha = hashlib.sha256()
        with os.fdopen(fd, "wb") as stream:
            for chunk in chunks:
                sha.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest is not None and "sha256:" + sha.hexdigest() != digest:
            raise PublicationError(f"Hub source blob {digest} failed SHA-256 verification; nothing was stored")
        os.chmod(temporary, 0o400)
        os.rename(temporary, directory / name)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _document(path: Path, item: dict, *, versioned: bool = True) -> dict:
    """Parse one bounded closure document; duplicate object keys are refused (as on the Hub)."""
    if item["size_bytes"] > _DOCUMENT_LIMIT:
        raise PublicationError(f"source {item['role']} document exceeds {_DOCUMENT_LIMIT} bytes")
    with path.open("rb") as stream:
        payload = stream.read(item["size_bytes"] + 1)
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, UnicodeError, RecursionError):
        raise PublicationError(f"source {item['role']} document is not valid JSON or repeats an object key") from None
    if not isinstance(value, dict) or (versioned and value.get("schemaVersion") != 2):
        raise PublicationError(f"source {item['role']} document is not a valid OCI JSON document")
    return value


def _descriptor_pair(value: Any) -> tuple | None:
    if not isinstance(value, dict):
        return None
    return (value.get("digest"), value.get("size")) if type(value.get("size")) is int else None


def _verify_oci_chain(blobs: Path, closure: list[dict], platform: str, public: dict) -> dict:
    """Verify root -> manifest -> config + layers from downloaded bytes; return the root descriptor."""
    by_role = {(item["role"], item["ordinal"]): item for item in closure}
    manifest_item, config_item = by_role[("manifest", 0)], by_role[("config", 0)]
    layer_items = [item for item in closure if item["role"] == "layer"]

    def path(item: dict) -> Path:
        return blobs / item["digest"].split(":", 1)[1]

    manifest = _document(path(manifest_item), manifest_item)
    manifest_media = manifest.get("mediaType", OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    if manifest_media not in SUPPORTED_IMAGE_MANIFEST_MEDIA_TYPES:
        raise PublicationError("source manifest is not an OCI image manifest or Docker v2 schema 2 manifest")
    if _descriptor_pair(manifest.get("config")) != (config_item["digest"], config_item["size_bytes"]):
        raise PublicationError("source manifest config descriptor differs from the closure config")
    declared = manifest.get("layers")
    if not isinstance(declared, list) or [_descriptor_pair(item) for item in declared] != [
        (item["digest"], item["size_bytes"]) for item in layer_items
    ]:
        raise PublicationError("source manifest layer descriptors differ from the closure layers")
    config = _document(path(config_item), config_item, versioned=False)
    if f"{config.get('os')}/{config.get('architecture')}" != platform:
        raise PublicationError("source config platform differs from the published image platform")
    rootfs = config.get("rootfs") if isinstance(config.get("rootfs"), dict) else {}
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layer_items):
        raise PublicationError("source config DiffIDs do not match the closure layer count")
    source = public.get("source") if isinstance(public.get("source"), dict) else {}
    if "diff_ids" in source and source["diff_ids"] != diff_ids:
        raise PublicationError("Hub image manifest DiffIDs differ from the source config")
    for layer, diff_id in zip(public["layers"], diff_ids, strict=True):
        origin = layer.get("source") if isinstance(layer.get("source"), dict) else {}
        if "diff_id" in origin and origin["diff_id"] != diff_id:
            raise PublicationError("Hub image manifest layer DiffID differs from the source config")
    root = by_role.get(("root", 0))
    if root is None:
        return {"mediaType": manifest_media, "digest": manifest_item["digest"], "size": manifest_item["size_bytes"]}
    index = _document(path(root), root)
    index_media = index.get("mediaType", OCI_IMAGE_INDEX_MEDIA_TYPE)
    entries = index.get("manifests")
    if index_media not in SUPPORTED_IMAGE_INDEX_MEDIA_TYPES or not isinstance(entries, list):
        raise PublicationError("source root is not an OCI image index or Docker manifest list")
    if (manifest_item["digest"], manifest_item["size_bytes"]) not in {_descriptor_pair(item) for item in entries}:
        raise PublicationError("source root index does not list the closure manifest")
    return {"mediaType": index_media, "digest": root["digest"], "size": root["size_bytes"]}


def _layout_index(root_descriptor: dict) -> bytes:
    return json.dumps(
        {"schemaVersion": 2, "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE, "manifests": [root_descriptor]},
        separators=(",", ":"),
    ).encode()


def _verify_existing_layout(layout: Path, closure: list[dict], platform: str, public: dict) -> dict:
    """Accept an already rebuilt layout only if it is exactly this closure, byte for byte."""
    blobs = layout / "blobs" / "sha256"
    try:
        for directory in (layout.parent, layout, layout / "blobs", blobs):
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise PublicationError("existing published source layout is not a real owned directory")
        expected = {item["digest"]: item["size_bytes"] for item in closure}
        if set(os.listdir(blobs)) != {digest.split(":", 1)[1] for digest in expected} or set(os.listdir(layout)) != {
            "blobs",
            "index.json",
            "oci-layout",
        }:
            raise PublicationError("existing published source layout differs; refusing to overwrite")
        for digest, size in expected.items():
            if _hash_file(blobs / digest.split(":", 1)[1], size) != digest:
                raise PublicationError("existing published source layout differs; refusing to overwrite")
        root = _verify_oci_chain(blobs, closure, platform, public)
        for name, payload in (("index.json", _layout_index(root)), ("oci-layout", _LAYOUT_MARKER)):
            if _hash_file(layout / name, len(payload)) != "sha256:" + hashlib.sha256(payload).hexdigest():
                raise PublicationError("existing published source layout differs; refusing to overwrite")
    except OSError as exc:
        raise PublicationError(
            f"existing published source layout cannot be verified ({type(exc).__name__}); refusing to use it"
        ) from None
    return root


def _newest_mtime(entry: Path) -> float:
    """Newest modification time inside one staging tree (links are never followed).

    A blob being downloaded updates its own file, not the staging directory, so the
    directory's own time alone would make a slow active pull look abandoned.
    """
    newest = os.lstat(entry).st_mtime
    for directory, directories, files in os.walk(entry):
        for name in (*directories, *files):
            try:
                newest = max(newest, os.lstat(os.path.join(directory, name)).st_mtime)
            except OSError:
                continue
    return newest


def _sweep_staging(base: Path, *, minimum_age_seconds: float = _STAGING_SWEEP_AGE) -> None:
    """Remove abandoned ``.staging-*`` directories of earlier interrupted pulls (never following links).

    A directory is abandoned when nothing inside it changed for ``minimum_age_seconds``.
    """
    try:
        info = os.lstat(base)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return
    cutoff = time.time() - minimum_age_seconds
    for name in os.listdir(base):
        if not name.startswith(".staging-"):
            continue
        entry = base / name
        try:
            child = os.lstat(entry)
            if stat.S_ISDIR(child.st_mode) and child.st_uid == os.geteuid() and _newest_mtime(entry) < cutoff:
                _remove_staging(entry)
        except OSError:
            continue


def _materialize_layout(
    roots: StatePaths, client: HubClient, hub_image_id: str, closure: list[dict], platform: str, public: dict
) -> tuple[Path, dict, bool]:
    """Download, verify and atomically publish ``<state>/published/<id>/layout``.

    Concurrent pulls of the same image each build a private staging copy; the
    first rename wins and every other puller verifies that winner byte for byte.
    """
    base = roots.state / "published"
    try:
        _private_directory(base)
        final = base / hub_image_id
        if os.path.lexists(final):
            return final / "layout", _verify_existing_layout(final / "layout", closure, platform, public), False
        staging = base / f".staging-{uuid.uuid4().hex}"
        os.mkdir(staging, 0o700)
    except OSError as exc:
        raise PublicationError(f"published source directory cannot be prepared ({type(exc).__name__})") from None
    try:
        layout = staging / "layout"
        blobs = layout / "blobs" / "sha256"
        for directory in (layout, layout / "blobs", blobs):
            os.mkdir(directory, 0o700)
        for digest, size in {item["digest"]: item["size_bytes"] for item in closure}.items():
            _write_file(blobs, digest.split(":", 1)[1], _stream(client, hub_image_id, digest, size), digest)
        root = _verify_oci_chain(blobs, closure, platform, public)
        _write_file(layout, "oci-layout", [_LAYOUT_MARKER])
        _write_file(layout, "index.json", [_layout_index(root)])
        for directory in (blobs, layout / "blobs", layout, staging):
            _fsync_directory(directory)
        try:
            os.rename(staging, final)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST) and not os.path.lexists(final):
                raise
            # Another puller published first: verify its layout instead of ours.
            _remove_staging(staging)
            return final / "layout", _verify_existing_layout(final / "layout", closure, platform, public), False
        _fsync_directory(base)
    except OSError as exc:
        _remove_staging(staging)
        raise PublicationError(f"published source layout could not be written ({type(exc).__name__})") from None
    except BaseException:
        _remove_staging(staging)
        raise
    return final / "layout", root, True


def _remove_staging(staging: Path) -> None:
    for directory, _, _ in os.walk(staging):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    shutil.rmtree(staging, ignore_errors=True)


def _register_source(roots: StatePaths, layout: Path, root: dict, platform: str, public: dict) -> str:
    """Register the rebuilt layout through the existing local OCI intake (no conversion, no VM)."""
    from . import inventory
    from .oci_image import OCIImageRef
    from .oci_source import LocalLayoutSource

    source = public.get("source") if isinstance(public.get("source"), dict) else {}
    try:
        reference = OCIImageRef(
            registry=source["registry"], repository=source["repository"], requested_reference=source["reference"]
        )
    except (KeyError, TypeError, ValueError, PalimpsestError):
        reference = None
    _private_directory(roots.runtime_packs)
    cas = SourceCAS(roots.oci_source_cas)
    layout_source = LocalLayoutSource(layout=layout, root_digest=root["digest"])
    snapshot = (
        layout_source.snapshot(reference, cas)
        if platform == "linux/amd64"
        else layout_source.snapshot_acquisition(reference, cas, platform=platform)
    )
    return inventory.record_oci_source(roots, snapshot)["image_id"]


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def pull_published_image(
    roots: StatePaths,
    hub_image_id: str,
    *,
    client: HubClient,
    hub_url: str,
    expected_name: str | None = None,
    max_bytes: int = MAX_PULL_BYTES,
) -> dict:
    """Verify and store every payload and source blob, then register the image as source ``published``.

    No VM is started. SquashFS payloads are digest-addressed and immutable, so a
    payload already verified in this state root is reused; differing content for
    an already registered Hub image id is refused.
    """
    if _uuid(hub_image_id) is None:
        raise PublicationError("Hub image id must be a canonical UUID")
    try:
        value = client.get_published_image(hub_image_id)
    except _TRANSPORT_ERRORS as exc:
        raise PublicationError(describe_hub_error(exc, action="pull", redact=client._redact)) from None
    record = _pull_record(value, hub_image_id, hub_url, expected_name)
    closure = record["source_closure"]
    payloads = {item["digest"]: item["size_bytes"] for item in record["layers"]}
    source_bytes = sum({item["digest"]: item["size_bytes"] for item in closure}.values())
    declared = sum(payloads.values()) + source_bytes
    if declared > max_bytes:
        raise PublicationError(
            f"Hub image declares {declared} bytes ({_gib(declared)}), above this client's {_gib(max_bytes)} pull cap"
        )
    if record["platform"] != "linux/amd64" and source_bytes > SOURCE_INTAKE_MAX_BYTES:
        raise PublicationError(
            f"the image's OCI source closure declares {source_bytes} bytes ({_gib(source_bytes)}), above the local "
            f"OCI source intake limit of {_gib(SOURCE_INTAKE_MAX_BYTES)}; it could not be registered, so nothing "
            "was downloaded"
        )
    try:
        roots.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise PublicationError("the selected state root cannot be created") from None
    try:
        _sweep_staging(roots.state / "published")
    except OSError:
        pass  # Best effort only; a later pull retries the sweep.
    with open_catalog(roots) as catalog:
        existing = next(
            (
                item
                for item in catalog.read_pulled_images()
                if (item["hub_url"], item["hub_image_id"]) == (hub_url, hub_image_id)
            ),
            None,
        )
    if existing is not None and not _same_pull(existing, record):
        raise PublicationError("a different image is already registered for this Hub image id; refusing to overwrite")
    store = ArtifactStore(roots.store)
    missing = sum(size for digest, size in payloads.items() if not _payload_present(store, digest, size))
    if closure and not os.path.lexists(roots.state / "published" / hub_image_id):
        # The closure is written twice: the rebuilt layout and the source CAS copy made at registration.
        missing += 2 * source_bytes
    if missing + _FREE_SPACE_MARGIN > _free_bytes(roots.state):
        raise PublicationError(
            f"the state root's filesystem lacks free space for {missing} bytes ({_gib(missing)}) plus a safety "
            "margin; nothing was downloaded"
        )
    downloaded = [
        digest for digest, size in payloads.items() if _fetch_payload(client, store, hub_image_id, digest, size)
    ]
    layout, root, local_image_id, registration_error = None, None, None, None
    if closure:
        layout_path, root, created_layout = _materialize_layout(
            roots, client, hub_image_id, closure, record["platform"], record["manifest"]
        )
        if created_layout:
            downloaded += sorted({item["digest"] for item in closure})
        layout = layout_path.relative_to(roots.state).as_posix()
        try:
            local_image_id = _register_source(roots, layout_path, root, record["platform"], record["manifest"])
        except PalimpsestError as exc:
            registration_error = str(exc)
    with open_catalog(roots) as catalog:
        try:
            stored, created = catalog.register_pulled_image(
                {**record, "source_layout": layout, "local_image_id": local_image_id}
            )
        except CatalogError as exc:
            raise PublicationError(str(exc)) from None
    result = {
        "state_root": str(roots.state),
        "created": created,
        "downloaded": downloaded,
        **{
            key: stored[key]
            for key in (
                "hub_url",
                "hub_image_id",
                "name",
                "visibility",
                "manifest_digest",
                "source",
                "platform",
                "layers",
                "source_closure",
                "source_layout",
                "local_image_id",
                "pulled_at",
            )
        },
        "runnable": bool(closure) and registration_error is None,
    }
    if closure and registration_error is None:
        arm64 = record["platform"] == "linux/arm64"
        result["run_hint"] = shlex.join(
            [
                "plm",
                "run",
                str(roots.state / layout),
                "--runtime-kind",
                "oci-root",
                "--manifest",
                root["digest"],
                *(["--platform", "linux/arm64", "--backend", "lima-vz"] if arm64 else []),
            ]
        )
        result["run_requirement"] = (
            "macOS on Apple silicon with an explicitly configured, qualified Ubuntu 24.04 arm64 Lima VZ profile"
            if arm64
            else "a Linux x86_64 host with KVM (OCI-root)"
        )
    elif closure:
        result["source_registration_error"] = registration_error
        result["message"] = (
            "The OCI source closure was downloaded and verified, but registering it as a local OCI source failed, "
            "so this image is not marked runnable and no run command is printed. The layout is kept at "
            f"{roots.state / layout}."
        )
    else:
        result["message"] = (
            "This Hub image was published without its OCI source closure (--no-source): its SquashFS layers are "
            "stored here, but it cannot be run with plm run."
        )
    return result


def local_status(roots: StatePaths) -> dict:
    """Read publication status and pulled images without creating or upgrading the catalog."""
    with open_catalog(roots, create=False) as catalog:
        if catalog is None:
            return {"state_root": str(roots.state), "publications": [], "pulled_images": []}
        return {
            "state_root": str(roots.state),
            "publications": catalog.read_publications(),
            "pulled_images": [
                {key: value for key, value in item.items() if key != "manifest"}
                for item in catalog.read_pulled_images()
            ],
        }
