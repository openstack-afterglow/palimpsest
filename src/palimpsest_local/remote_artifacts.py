"""Connected artifact inventory and fixed, bounded transfer jobs.

Connections never share credentials. Downloads retain verified canonical bytes;
OCI images are container roots, not conventional VM disks. HTTP callers select
connection/reference/build identities, never executable arguments or file paths.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import signal
import ssl
import stat
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from .digest import digest_file, digest_hex, require_digest
from .docker_hub import DockerHubCatalog
from .errors import PalimpsestError, StateError
from .hub import DEFAULT_HUB_URL, DISK_FORMAT_MEDIA_TYPES, MEDIA_TYPE_LAYER_SQUASHFS, HubClient
from .oci_layout import ContentStore
from .package_credentials import get_package_key
from .package_reference import read_package_reference, write_package_reference
from .packages import NativePackageClient, validate_keystone_id
from .registry import _TAG_RE, RegistryProfile, _has_explicit_registry, load_registry_config, resolve_image_reference
from .registry_intake import inspect_registry_reference, pull_registry_oci_archive
from .state import StatePaths, atomic_write_json, init_roots, read_tag_record

_MAX_JSON = 4 * 1024 * 1024
_MAX_CHAIN = 64
_KINDS = {"all", "cloud-image", "squashfs", "oci-image", "runtime-bundle"}
_HUB_ENDPOINTS = {"docker.io", "index.docker.io", "registry-1.docker.io"}
_DESCRIPTOR_FIELDS = {
    "name",
    "kind",
    "media_type",
    "disk_format",
    "arch",
    "os_variant",
    "ubuntu_base",
    "python_version",
    "parent_digest",
    "base_image_digest",
    "chain_id",
}


def _text(value: object, field: str, *, empty: bool = False, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not value and not empty)
        or any(ord(c) < 32 for c in value)
    ):
        raise StateError(f"invalid remote {field}")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise StateError("remote limit must be between 1 and 100")
    return value


def _page_cursor(value: str | None) -> int:
    if value in (None, ""):
        return 0
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,18}", value) is None:
        raise StateError("invalid remote page cursor")
    return int(value)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener(ca: tuple[str, ...] = ()):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    for path in ca:
        context.load_verify_locations(cafile=path)
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())


def _read_json(response):
    payload = response.read(_MAX_JSON + 1)
    if len(payload) > _MAX_JSON:
        raise StateError("remote JSON response exceeds its byte limit")
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise StateError("invalid remote JSON response") from None


def _safe_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise StateError("invalid Hub URL") from None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise StateError("Hub connection must be a credential-free HTTPS URL")
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise StateError("Hub connection requires HTTPS (HTTP allowed only for isolated loopback Hub)")
    return value.rstrip("/")


class _BoundedResponse:
    def __init__(self, response, *, deadline: float | None = None, max_bytes: int = 16 * 1024**3):
        self.response = response
        self.remaining = max_bytes
        self.max_bytes = max_bytes
        self.deadline = deadline

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read(self, size=-1):
        maximum = self.remaining + 1
        requested = min(size, maximum) if size >= 0 else maximum
        if self.deadline is None:
            data = self.response.read(requested)
        else:
            chunks = []
            reader = getattr(self.response, "read1", self.response.read)
            while requested:
                if time.monotonic() >= self.deadline:
                    raise StateError("remote browse exceeded its aggregate read budget")
                chunk = reader(min(requested, 64 * 1024))
                if time.monotonic() >= self.deadline:
                    raise StateError("remote browse exceeded its aggregate read budget")
                if not chunk:
                    break
                chunks.append(chunk)
                requested -= len(chunk)
            data = b"".join(chunks)
        self.remaining -= len(data)
        if self.remaining < 0:
            if self.max_bytes == _MAX_JSON:
                raise StateError("Hub JSON response exceeds its 4 MiB byte limit")
            raise StateError("Hub transfer exceeds its 16 GiB byte limit")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.response.close()


@dataclass(frozen=True, repr=False)
class HubConnection:
    """One tab's original Keystone authority, never persisted or shared globally."""

    url: str
    token: str
    project_id: str | None = None

    @classmethod
    def from_headers(cls, headers) -> HubConnection | None:
        url = headers.get("X-Palimpsest-Hub-Url")
        token = headers.get("X-Palimpsest-Hub-Token")
        project = headers.get("X-Palimpsest-Hub-Project")
        if url is None and token is None and project is None:
            return None
        if not url or not token:
            raise StateError("Hub URL and original Keystone token must be supplied together")
        url = _safe_url(_text(url, "Hub URL", maximum=4096))
        token = _text(token, "Hub token", maximum=8192)
        if not token.isascii() or not token.strip():
            raise StateError("invalid Hub token")
        return cls(url, token, validate_keystone_id(project, "project ID") if project else None)

    def environment(self) -> dict[str, str]:
        return {
            "PALIMPSEST_URL": self.url,
            "PALIMPSEST_TOKEN": self.token,
            "PALIMPSEST_PROJECT_ID": self.project_id or "",
        }


class _ConnectedHub(HubClient):
    """Reuse resumable blob protocol, but bound JSON and suppress remote error text."""

    def __init__(self, url: str, token: str, project: str | None, ca: tuple[str, ...]):
        super().__init__(
            _safe_url(url),
            token,
            project_id=validate_keystone_id(project, "project ID") if project else None,
            timeout_seconds=60,
        )
        self._opener = _opener(ca)

    def _open(self, method, path, **kwargs):
        expected = self._base_url + (path if path.startswith("/v1") else "/v1" + path)
        query = {key: value for key, value in (kwargs.get("query") or {}).items() if value is not None}
        if query:
            expected += "?" + urllib.parse.urlencode(query)
        deadline = getattr(self, "_browse_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            raise StateError("remote browse exceeded its aggregate read budget")
        try:
            response = super()._open(method, path, **kwargs)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise StateError(f"Hub request failed (HTTP {code})") from None
        except (PalimpsestError, OSError, ValueError):
            raise StateError("Hub connection failed") from None
        # urllib's no-redirect handler also protects streaming blob requests.
        if response.geturl() != expected or not 200 <= response.status < 300:
            response.close()
            raise StateError("Hub returned an invalid response status")
        blob_response = method == "GET" and re.fullmatch(r"(?:/v1)?/layers/sha256:[0-9a-f]{64}/blob", path) is not None
        return _BoundedResponse(response, deadline=deadline, max_bytes=16 * 1024**3 if blob_response else _MAX_JSON)

    def _json_request(self, method, path, payload=None, *, query=None):
        body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        with self._open(
            method, path, query=query, body=body, content_type="application/json" if body else None
        ) as response:
            return _read_json(response)

    def identity(self, action: str | None = None) -> dict:
        actor = self._json_request("GET", "/me")
        if not isinstance(actor, dict):
            raise StateError("invalid Hub identity response")
        project = validate_keystone_id(actor.get("project_id"), "project ID")
        if self._project_id is not None and project != self._project_id:
            raise StateError("Hub identity does not match the requested project assertion")
        validate_keystone_id(actor.get("user_id"), "user ID")
        if not isinstance(actor.get("capabilities"), dict):
            raise StateError("invalid Hub identity capabilities")
        if action is not None and actor["capabilities"].get(action) is not True:
            raise StateError("Hub identity does not authorize the requested artifact action")
        self._project_id = project
        return actor


class _ConnectedNative(NativePackageClient):
    def _read_error_body(self, response, limit):
        return _BoundedResponse(response, deadline=getattr(self, "_browse_deadline", None)).read(limit)

    def _open(self, *args, **kwargs):
        deadline = getattr(self, "_browse_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            raise StateError("remote browse exceeded its aggregate read budget")
        return _BoundedResponse(super()._open(*args, **kwargs), deadline=deadline)


def _linux_amd64(platforms: list[dict] | None) -> bool:
    return bool(platforms) and any(
        isinstance(item, dict) and item.get("os") == "linux" and item.get("architecture") in {"amd64", "x86_64"}
        for item in platforms
    )


def _acquisition_supported(platforms: list[dict] | None) -> bool:
    return bool(platforms) and any(
        isinstance(item, dict)
        and item.get("os") == "linux"
        and item.get("architecture") in {"amd64", "x86_64", "arm64", "aarch64"}
        for item in platforms
    )


def _run_info(kind: str, architectures: list[str], platforms: list[dict] | None = None) -> dict:
    host_arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    host_system = platform.system()
    archs = [{"amd64": "x86_64", "arm64": "aarch64"}.get(a, a) for a in architectures]
    if kind == "oci-image":
        supported = host_system == "Linux" and host_arch == "x86_64" and _linux_amd64(platforms)
        limitation = "Native OCI-root requires Linux amd64 KVM. On Darwin arm64 or foreign architectures, explicit force-architecture execution is required."
        runtime = "oci-root"
    else:
        supported = (
            bool(archs)
            and host_arch in archs
            and (host_system == "Linux" or (host_system == "Darwin" and host_arch == "aarch64"))
        )
        limitation = "Cloud-image execution requires matching architecture and an available VM backend; Darwin x86_64 is unsupported. Runtime preflight still applies."
        runtime = "cloud-image"
    return {"run_supported": supported, "run_limitation": limitation, "runtime_kind": runtime}


def _architectures(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise StateError("invalid remote platform list")
    found = set()
    for item in value:
        if isinstance(item, dict):
            architecture = item.get("architecture") or item.get("arch")
        elif isinstance(item, str):
            architecture = item.split("/")[1] if "/" in item else item
        else:
            raise StateError("invalid remote platform entry")
        if isinstance(architecture, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", architecture):
            found.add(architecture)
    return sorted(found)


def _descriptor(raw: dict, expected: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise StateError("invalid Hub artifact descriptor")
    config = raw.get("config_json", {})
    if not isinstance(config, dict):
        raise StateError("invalid Hub artifact config")
    metadata = {key: raw.get(key) if raw.get(key) is not None else config.get(key) for key in _DESCRIPTOR_FIELDS}
    metadata = {key: value for key, value in metadata.items() if value is not None}
    digest = require_digest(raw.get("blob_digest") or raw.get("digest"))
    if expected is not None and digest != expected:
        raise StateError("Hub artifact descriptor digest mismatch")
    kind = metadata.get("kind")
    if kind == "cloud-image":
        disk_format = metadata.get("disk_format")
        if disk_format not in DISK_FORMAT_MEDIA_TYPES or metadata.get("arch") not in {"x86_64", "aarch64"}:
            raise StateError("cloud-image descriptor requires disk_format and architecture")
        if metadata.get("media_type", DISK_FORMAT_MEDIA_TYPES[disk_format]) != DISK_FORMAT_MEDIA_TYPES[disk_format]:
            raise StateError("cloud-image descriptor has incompatible media type")
        metadata["media_type"] = DISK_FORMAT_MEDIA_TYPES[disk_format]
    elif kind == "squashfs":
        if metadata.get("media_type") != MEDIA_TYPE_LAYER_SQUASHFS:
            raise StateError("SquashFS descriptor has incompatible media type")
        pack_key = raw.get("runtime_pack_manifest_digest")
        if pack_key is not None:
            if metadata.get("parent_digest") is not None or metadata.get("arch") not in {"x86_64", "aarch64"}:
                raise StateError("runtime-pack requires an architecture and no parent layer")
            normalized_pack = require_digest(pack_key)
            if metadata.get("chain_id") not in (None, normalized_pack):
                raise StateError("runtime-pack manifest conflicts with chain identity")
            metadata["chain_id"] = normalized_pack
            metadata["runtime_pack_manifest_digest"] = normalized_pack
        for field in ("parent_digest", "base_image_digest", "chain_id"):
            if field in metadata:
                metadata[field] = require_digest(metadata[field])
    else:
        raise StateError("unsupported legacy artifact type; use native packages for OCI images/runtime bundles")
    return {**metadata, "digest": digest}


def _merge_local_metadata(store: ContentStore, metadata: dict) -> dict:
    """Preserve richer canonical sidecars; reject contradictory execution bindings."""
    digest = metadata["digest"]
    if not store.metadata_path(digest).exists():
        return metadata
    existing = store.read_metadata(digest)
    previous = _descriptor(existing, digest)
    for field in sorted((_DESCRIPTOR_FIELDS - {"name"}) | {"runtime_pack_manifest_digest"}):
        if field in {"parent_digest", "base_image_digest"} or (field in previous and field in metadata):
            if previous.get(field) != metadata.get(field):
                raise StateError(f"existing local artifact metadata conflicts with downloaded {field}")
    return {**metadata, **existing}


def _catalog_descriptor(raw: dict, expected: str | None = None) -> dict:
    metadata = _descriptor(raw, expected)
    size = raw.get("size_bytes")
    if size is not None:
        if type(size) is not int or size < 0:
            raise StateError("invalid Hub artifact size")
        metadata["size_bytes"] = size
    if isinstance(raw.get("created_at"), str):
        metadata["created_at"] = _text(raw["created_at"], "creation time", maximum=128)
    if type(raw.get("is_published")) is bool:
        metadata["is_published"] = raw["is_published"]
    return metadata


class RemoteArtifacts:
    def __init__(self, roots: StatePaths, *, hub_connection: HubConnection | None = None):
        self.roots = roots
        self.hub_connection = hub_connection

    def _legacy_config(self) -> tuple[str | None, tuple[str, ...]]:
        path = self.roots.config / "config.toml"
        try:
            with path.open("rb") as stream:
                payload = stream.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise StateError("Hub configuration exceeds its size bound")
            config = tomllib.loads(payload.decode())
        except FileNotFoundError:
            config = {}
        except (OSError, ValueError, UnicodeError):
            raise StateError("invalid Hub configuration") from None
        hub = config.get("hub", {})
        if not isinstance(hub, dict):
            raise StateError("invalid Hub configuration")
        url = (
            self.hub_connection.url
            if self.hub_connection is not None
            else (os.environ.get("PALIMPSEST_URL") or config.get("url") or hub.get("url") or DEFAULT_HUB_URL)
        )
        ca = hub.get("ca", [])
        if (
            not isinstance(ca, list)
            or len(ca) > 16
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in ca)
        ):
            raise StateError("Hub CA configuration must contain absolute certificate paths")
        return _safe_url(_text(url, "Hub URL", maximum=4096)) if url else None, tuple(ca)

    def _hub(self) -> _ConnectedHub:
        url, ca = self._legacy_config()
        if not url:
            raise StateError("legacy Hub connection is not configured")
        token = self.hub_connection.token if self.hub_connection is not None else os.environ.get("PALIMPSEST_TOKEN")
        project = (
            self.hub_connection.project_id
            if self.hub_connection is not None
            else os.environ.get("PALIMPSEST_PROJECT_ID")
        )
        if not token:
            raise StateError("Hub requires an original project-scoped Keystone token")
        return _ConnectedHub(url, token, project, ca)

    def hub_identity(self) -> dict:
        hub = self._hub()
        hub._timeout_seconds = 10
        hub._browse_deadline = time.monotonic() + 15
        actor = hub.identity()
        return {
            "connection_id": "legacy",
            "endpoint": hub._base_url,
            "project_id": actor["project_id"],
            "user_id": actor["user_id"],
            "capabilities": {
                key: actor["capabilities"].get(key) is True
                for key in ("inventory", "download", "upload", "build", "delete")
            },
            "credential_source": "tab-memory" if self.hub_connection is not None else "process-environment",
        }

    def _profile(self, connection_id: str) -> RegistryProfile:
        _text(connection_id, "connection")
        alias = connection_id.removeprefix("registry:")
        if not connection_id.startswith("registry:") or alias not in load_registry_config(self.roots).registries:
            raise StateError("unknown remote artifact connection")
        profile = load_registry_config(self.roots).registries[alias]
        if profile.plain_http or profile.tls_skip_verify:
            raise StateError("remote artifact transfers require verified production HTTPS")
        return profile

    def _native(self, profile: RegistryProfile) -> NativePackageClient:
        # Global PALIMPSEST_PACKAGE_KEY cannot bleed into another configured connection.
        credential = get_package_key(profile, profile.namespace)
        if credential is None:
            raise StateError("selected native connection has no credential-helper key; log in to that profile")
        return _ConnectedNative(profile, credential=credential)

    def _reference(self, profile: RegistryProfile, reference: str):
        _text(reference, "reference")
        if reference.startswith("-"):
            raise StateError("invalid remote reference")
        resolved = resolve_image_reference(reference, load_registry_config(self.roots), registry_alias=profile.alias)
        if resolved.endpoint != profile.endpoint:
            raise StateError("reference is outside the selected connection")
        if profile.protocol == "palimpsest" and resolved.repository.partition("/")[0] != profile.namespace:
            raise StateError("native reference is outside the selected project namespace")
        return resolved

    def connections(self) -> list[dict]:
        url, _ = self._legacy_config()
        project = (
            self.hub_connection.project_id
            if self.hub_connection is not None
            else os.environ.get("PALIMPSEST_PROJECT_ID")
        )
        token_available = self.hub_connection is not None or bool(os.environ.get("PALIMPSEST_TOKEN"))
        items = [
            {
                "id": "legacy",
                "label": "Palimpsest Hub",
                "protocol": "legacy",
                "endpoint": url or "",
                "api_base": url or "",
                "namespace": project,
                "configured": bool(url),
                "search_mode": "inventory",
                "credential_available": token_available,
                "credential_source": "tab-memory" if self.hub_connection is not None else "process-environment",
                "capabilities": {"inventory": True, "download": True, "upload": True},
                "project_configured": bool(project),
                "kinds": ["cloud-image", "squashfs"],
            }
        ]
        for alias, profile in sorted(load_registry_config(self.roots).registries.items()):
            items.append(
                {
                    "id": "registry:" + alias,
                    "label": "Docker Hub"
                    if profile.protocol == "oci" and profile.endpoint in _HUB_ENDPOINTS
                    else alias,
                    "protocol": profile.protocol,
                    "endpoint": profile.endpoint,
                    "api_base": profile.api_base,
                    "namespace": profile.namespace,
                    "credential_available": None,
                    "credential_state": "resolved-on-use",
                    "capabilities": {"inventory": True, "download": True, "upload": profile.protocol == "palimpsest"},
                    "search_mode": "inventory"
                    if profile.protocol == "palimpsest"
                    else ("public-repository-search" if profile.endpoint in _HUB_ENDPOINTS else "exact-reference/tags"),
                    "credential_source": "native-profile-helper"
                    if profile.protocol == "palimpsest"
                    else "Docker-config/helper",
                    "secure": not (profile.plain_http or profile.tls_skip_verify),
                    "kinds": ["oci-image", "runtime-bundle"] if profile.protocol == "palimpsest" else ["oci-image"],
                }
            )
        for item in items:
            if item.get("download_supported") is None:
                item["download_supported"] = True
        return items

    def browse(
        self,
        connection_id: str,
        *,
        kind: str = "all",
        query: str = "",
        repository: str = "",
        limit: int = 30,
        cursor: str | None = None,
        arch: str = "",
        disk_format: str = "",
        parent: str = "",
    ) -> dict:
        if kind not in _KINDS:
            raise StateError("unsupported remote artifact kind")
        query, repository, limit = (
            _text(query, "query", empty=True),
            _text(repository, "repository", empty=True),
            _limit(limit),
        )
        if arch not in {"", "x86_64", "aarch64"} or disk_format not in {"", "qcow2", "raw"}:
            raise StateError("invalid Hub architecture or disk format filter")
        if parent:
            parent = require_digest(parent)
        if connection_id != "legacy" and (arch or disk_format or parent):
            raise StateError("cloud-image filters require the Palimpsest Hub connection")
        deadline = time.monotonic() + 40
        if connection_id == "legacy":
            hub = self._hub()
            hub._timeout_seconds = 15
            hub._browse_deadline = deadline
            hub.identity("inventory")
            offset = _page_cursor(cursor)
            digest_query = re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{4,64}", query)
            filters = {"digest_prefix": query} if digest_query else {"name": query or None}
            if query.startswith("sha256:") and not digest_query:
                raise StateError("digest search requires 4–64 hexadecimal characters")
            if arch:
                filters["arch"] = arch
            if disk_format:
                filters["disk_format"] = disk_format
            if parent:
                filters["parent_digest"] = parent
            rows = hub.list_layers(**filters, kind=None if kind == "all" else kind, limit=limit + 1, offset=offset)
            if not isinstance(rows, list) or len(rows) > limit + 1:
                raise StateError("invalid or over-limit Hub inventory")
            items = []
            for row in rows[:limit]:
                if isinstance(row, dict) and row.get("kind") in {"cloud-image", "squashfs"}:
                    metadata = _catalog_descriptor(row)
                    archs = [metadata["arch"]] if metadata.get("arch") else []
                    items.append(
                        {
                            "reference": metadata["digest"],
                            "name": metadata.get("name", metadata["digest"]),
                            "kind": metadata["kind"],
                            "architectures": archs,
                            "download_supported": True,
                            "details_supported": True,
                            "digest": metadata["digest"],
                            "size_bytes": metadata.get("size_bytes"),
                            "disk_format": metadata.get("disk_format"),
                            "base_digest": metadata.get("base_image_digest"),
                            "parent_digest": metadata.get("parent_digest"),
                            **_run_info(metadata["kind"], archs),
                        }
                    )
                    if metadata["kind"] == "squashfs" and not metadata.get("base_image_digest"):
                        items[-1]["run_supported"] = False
                        items[-1]["run_limitation"] = (
                            "Run requires a verified complete cloud-image base dependency; Download preserves valid layer-only parent chains without inventing a VM base."
                        )
            return {
                "connection_id": connection_id,
                "search_mode": "inventory",
                "items": items,
                "next_cursor": str(offset + limit) if len(rows) > limit else None,
                "message": "Download resolves and verifies the full base/parent chain before enabling Run.",
            }
        profile = self._profile(connection_id)
        if profile.protocol == "palimpsest":
            client = self._native(profile)
            client.timeout_seconds = 5
            client._browse_deadline = deadline
            items = []
            if repository:
                package = repository.removeprefix(profile.namespace + "/")
                summary = client.get_package(package)
                page = client.list_versions(package, limit=limit, cursor=cursor)
                for version in page["items"]:
                    archs = _architectures(version.get("platforms", []))
                    artifact_kind = summary["package_type"]
                    if kind in {"all", artifact_kind}:
                        items.append(
                            {
                                "reference": profile.endpoint
                                + "/"
                                + profile.namespace
                                + "/"
                                + package
                                + "@"
                                + version["root_digest"],
                                "repository": profile.namespace + "/" + package,
                                "name": package,
                                "digest": version["root_digest"],
                                "kind": artifact_kind,
                                "architectures": archs,
                                "platforms": version.get("platforms", []),
                                "size_bytes": version.get("archive_size_bytes"),
                                "download_supported": True,
                                **_run_info(artifact_kind, archs, version.get("platforms", [])),
                            }
                        )
            else:
                if kind not in {"all", "oci-image", "runtime-bundle"}:
                    return {
                        "connection_id": connection_id,
                        "search_mode": "inventory",
                        "items": [],
                        "next_cursor": None,
                    }
                page = client.list_packages(limit=limit, cursor=cursor, package_type=None if kind == "all" else kind)
                for package in page["items"]:
                    if query and query.lower() not in package["name"].lower():
                        continue
                    archs = _architectures(package.get("platforms", []))
                    items.append(
                        {
                            "repository": profile.namespace + "/" + package["name"],
                            "name": package["name"],
                            "reference": None,
                            "kind": package["package_type"],
                            "tags": package["tags"],
                            "architectures": archs,
                            "platforms": package.get("platforms", []),
                            "download_supported": False,
                            **_run_info(package["package_type"], archs, package.get("platforms", [])),
                        }
                    )
            return {
                "connection_id": connection_id,
                "search_mode": "inventory",
                "items": items,
                "next_cursor": page.get("next_cursor"),
            }
        if kind not in {"all", "oci-image"}:
            return {
                "connection_id": connection_id,
                "search_mode": "exact-reference/tags",
                "items": [],
                "next_cursor": None,
            }
        if not repository and (_has_explicit_registry(query) or "@sha256:" in query or ":" in query.rsplit("/", 1)[-1]):
            repository, query = query, ""
        if profile.endpoint in _HUB_ENDPOINTS:
            catalog = DockerHubCatalog(profile)
            if not repository:
                return catalog.search(query, limit=limit, cursor=cursor)
            resolved = self._reference(profile, repository)
            explicit = "@" in repository or ":" in repository.rsplit("/", 1)[-1]
            if not explicit:
                return catalog.tags(resolved.repository, query=query, limit=limit, cursor=cursor)
            detail = catalog.inspect(resolved.canonical)
            return {
                "connection_id": connection_id,
                "search_mode": "exact-reference/tags",
                "items": [{**detail, "details_supported": True}],
                "next_cursor": None,
            }
        if not repository:
            return {
                "connection_id": connection_id,
                "search_mode": "exact-reference/tags",
                "items": [],
                "next_cursor": None,
                "message": "This OCI registry has no configured repository search API. Enter an exact repository or reference to inspect tags/manifests.",
            }
        resolved = self._reference(profile, repository)
        explicit = "@" in repository or ":" in repository.rsplit("/", 1)[-1]
        if explicit:
            references = [resolved.canonical]
        else:
            data = inspect_registry_reference(
                resolved.canonical, self.roots, profile=profile, tags=True, timeout_seconds=10
            )
            tags = data.get("Tags")
            if not isinstance(tags, list) or any(not isinstance(t, str) or _TAG_RE.fullmatch(t) is None for t in tags):
                raise StateError("invalid registry tag inventory")
            offset = _page_cursor(cursor)
            selected_tags = sorted(tag for tag in tags if not query or query.lower() in tag.lower())
            references = [
                resolved.endpoint + "/" + resolved.repository + ":" + tag
                for tag in selected_tags[offset : offset + limit]
            ]
            return {
                "connection_id": connection_id,
                "search_mode": "exact-reference/tags",
                "items": [
                    {
                        "reference": reference,
                        "repository": reference,
                        "name": reference,
                        "kind": "oci-image",
                        "architectures": [],
                        "download_supported": True,
                        **_run_info("oci-image", []),
                    }
                    for reference in references
                ],
                "next_cursor": str(offset + limit) if offset + limit < len(selected_tags) else None,
                "message": "Select an exact tag/reference to inspect manifest architectures; tag inventory does not promise VM compatibility.",
            }
        items = []
        for reference in references:
            document = inspect_registry_reference(reference, self.roots, profile=profile, raw=True, timeout_seconds=10)
            manifests = document.get("manifests")
            if manifests is not None:
                if not isinstance(manifests, list) or len(manifests) > 128:
                    raise StateError("invalid registry manifest index")
                platforms = [m.get("platform", {}) for m in manifests if isinstance(m, dict)]
                archs = _architectures(platforms)
                digest = None
            else:
                inspected = inspect_registry_reference(reference, self.roots, profile=profile, timeout_seconds=10)
                platforms = [
                    {"os": inspected.get("Os") or inspected.get("OS"), "architecture": inspected.get("Architecture")}
                ]
                archs = _architectures(platforms)
                digest = require_digest(inspected.get("Digest"))
            items.append(
                {
                    "reference": reference,
                    "repository": resolved.repository,
                    "name": reference,
                    "kind": "oci-image",
                    "digest": digest,
                    "architectures": archs,
                    "platforms": platforms,
                    "download_supported": _acquisition_supported(platforms),
                    "download_limitation": "OCI registry acquisition supports linux/amd64 and linux/arm64 container roots.",
                    **_run_info("oci-image", archs, platforms),
                }
            )
        return {
            "connection_id": connection_id,
            "search_mode": "exact-reference/tags",
            "items": items,
            "next_cursor": None,
        }

    def inspect(self, connection_id: str, reference: str, *, manifest: str | None = None) -> dict:
        """Read selected remote metadata; never download layers or execute an image."""
        _text(reference, "reference")
        if connection_id != "legacy":
            profile = self._profile(connection_id)
            if profile.protocol != "oci" or profile.endpoint not in _HUB_ENDPOINTS:
                raise StateError("artifact inspection supports Palimpsest Hub and Docker Hub")
            resolved = self._reference(profile, reference)
            return DockerHubCatalog(profile).inspect(resolved.canonical, manifest=manifest)
        if manifest is not None:
            raise StateError("platform manifest selection requires a Docker Hub image")
        digest = require_digest(reference)
        hub = self._hub()
        hub._timeout_seconds = 10
        hub._browse_deadline = time.monotonic() + 40
        actor = hub.identity("inventory")
        observed = {}

        def metadata(selected: str) -> dict:
            if selected not in observed:
                observed[selected] = hub.get_layer(selected)
            return observed[selected]

        chain = self._legacy_chain(digest, metadata, require_base=False)
        chain = [_catalog_descriptor(observed[item["digest"]], item["digest"]) for item in chain]
        leaf = chain[-1]
        has_base = chain[0]["kind"] == "cloud-image"
        archs = _architectures([item["arch"] for item in chain if item.get("arch")])
        compatibility = _run_info(leaf["kind"], archs)
        if not has_base:
            compatibility.update(
                run_supported=False,
                run_limitation="Layer-only chain has no bootable cloud-image base; download is available without VM execution.",
            )
        return {
            **leaf,
            **compatibility,
            "connection_id": connection_id,
            "reference": digest,
            "immutable_reference": digest,
            "architectures": archs,
            "platforms": [],
            "chain": chain,
            "chain_complete": True,
            "base_digest": leaf.get("base_image_digest"),
            "parent_digest": leaf.get("parent_digest"),
            "layers": [
                {**item, "ordinal": index}
                for index, item in enumerate(item for item in chain if item["kind"] == "squashfs")
            ],
            "download_supported": actor["capabilities"].get("download") is True,
        }

    def _legacy_chain(self, leaf: str, get_metadata, *, require_base: bool = True) -> list[dict]:
        current = require_digest(leaf)
        chain = []
        seen = set()
        while current:
            if current in seen or len(chain) >= _MAX_CHAIN:
                raise StateError("artifact parent chain is cyclic or exceeds 64 layers")
            seen.add(current)
            metadata = _descriptor(get_metadata(current), current)
            chain.append(metadata)
            if metadata["kind"] == "cloud-image":
                if len(chain) != 1:
                    raise StateError("SquashFS parent must be a layer, not a cloud image")
                return chain
            current = metadata.get("parent_digest")
        chain.reverse()
        base = chain[0].get("base_image_digest")
        if not base:
            if any(layer.get("base_image_digest") for layer in chain):
                raise StateError("artifact chain has conflicting base-image dependencies")
            if require_base:
                raise StateError("SquashFS execution requires a complete base-image dependency")
            return chain
        if any(layer.get("base_image_digest") not in (None, base) for layer in chain):
            raise StateError("artifact chain has conflicting base-image dependencies")
        metadata = _descriptor(get_metadata(base), base)
        if metadata["kind"] != "cloud-image":
            raise StateError("artifact base dependency is not a cloud image")
        if any(layer.get("arch") not in (None, metadata["arch"]) for layer in chain):
            raise StateError("artifact layer architecture differs from its base image")
        return [metadata, *chain]

    def _import_runtime(self, snapshot) -> dict:
        """Import only snapshot-verified runtime blobs/configs, preserving base and parent chain."""
        store = ContentStore(self.roots.store)
        configs = {digest: dict(config) for digest, config in snapshot.runtime_configs.items()}
        for digest, config in configs.items():
            bases = snapshot.runtime_bases.get(digest, ())
            if config.get("kind", "squashfs") == "squashfs" and len(bases) == 1:
                if not config.get("base_image_digest"):
                    config["base_image_digest"] = bases[0]
                if not config.get("arch"):
                    config["arch"] = configs[bases[0]].get("arch")
        parents = {config.get("parent_digest") for config in configs.values()}
        leaves = [
            digest
            for digest, config in configs.items()
            if config.get("kind", "squashfs") == "squashfs" and digest not in parents
        ]
        if not leaves:
            leaves = [digest for digest, config in configs.items() if config.get("kind") == "cloud-image"]
        host_arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
        if len(leaves) != 1:
            leaves = [digest for digest in leaves if configs[digest].get("arch") == host_arch]
        if len(leaves) != 1:
            return {
                "run_supported": False,
                "runtime_kind": "cloud-image",
                "run_limitation": "Verified runtime package retained, but no unambiguous host-compatible runtime chain could be selected for Run.",
            }
        metadata = {
            digest: _descriptor(
                {
                    **config,
                    "digest": digest,
                    "media_type": snapshot.graph[digest]["media_type"],
                    "kind": config.get("kind", "squashfs"),
                },
                digest,
            )
            for digest, config in configs.items()
        }
        chain = self._legacy_chain(leaves[0], metadata.__getitem__, require_base=False)
        if chain[0]["kind"] != "cloud-image":
            return {
                "run_supported": False,
                "runtime_kind": "cloud-image",
                "run_limitation": "Verified runtime package retained, but no complete cloud-image base dependency is available for Run.",
            }
        chain = [_merge_local_metadata(store, descriptor) for descriptor in chain]
        with tarfile.open(snapshot.archive, mode="r:*") as archive:
            for descriptor in chain:
                digest = descriptor["digest"]
                member = archive.getmember("blobs/sha256/" + digest_hex(digest))
                if not member.isfile() or member.size != snapshot.graph[digest]["size_bytes"]:
                    raise StateError("runtime package blob differs from verified graph")
                with archive.extractfile(member) as stream:
                    store.write_stream(iter(lambda: stream.read(1024 * 1024), b""), expected_digest=digest)
                store.write_metadata(digest, descriptor)
        from .cli import _layer_refs_from_store

        _layer_refs_from_store(store, [descriptor["digest"] for descriptor in chain[1:]], chain[0]["digest"])
        archs = [chain[0]["arch"]]
        return {
            "source": chain[0]["digest"],
            "image": chain[0]["digest"],
            "layers": [m["digest"] for m in chain[1:]],
            "architectures": archs,
            **_run_info("runtime-bundle", archs),
        }

    def download(self, connection_id: str, reference: str, *, platform: str = "linux/amd64") -> dict:
        _text(reference, "reference")
        if connection_id == "legacy":
            hub = self._hub()
            hub.identity("download")
            chain = self._legacy_chain(reference, hub.get_layer, require_base=False)
            store = ContentStore(self.roots.store)
            chain = [_merge_local_metadata(store, metadata) for metadata in chain]
            with tempfile.TemporaryDirectory(prefix="palimpsest-hub-download-") as temporary:
                for metadata in chain:
                    digest = metadata["digest"]
                    if store.exists(digest):
                        store.verify_blob(digest)
                    else:
                        path = hub.pull_blob(digest, Path(temporary) / digest_hex(digest), resume=False)
                        store.ingest_file(path, expected_digest=digest)
                    store.write_metadata(digest, metadata)
            has_base = chain[0]["kind"] == "cloud-image"
            layers = [metadata["digest"] for metadata in (chain[1:] if has_base else chain)]
            if has_base:
                from .cli import _layer_refs_from_store

                _layer_refs_from_store(store, layers, chain[0]["digest"])
            archs = _architectures([metadata["arch"] for metadata in chain if metadata.get("arch")])
            compatibility = (
                _run_info(chain[-1]["kind"], archs)
                if has_base
                else {
                    "run_supported": False,
                    "runtime_kind": "cloud-image",
                    "run_limitation": "Verified layer parent chain retained, but no complete cloud-image base dependency is available; Run is unsupported for this artifact.",
                }
            )
            result = {
                "connection_id": connection_id,
                "reference": require_digest(reference),
                "source": chain[0]["digest"] if has_base else chain[-1]["digest"],
                "image": chain[0]["digest"] if has_base else None,
                "layers": layers,
                "kind": chain[-1]["kind"],
                "architectures": archs,
                **compatibility,
            }
        else:
            profile = self._profile(connection_id)
            resolved = self._reference(profile, reference)
            if profile.protocol == "palimpsest":
                # Reuse canonical CLI snapshot retention/reference registration.
                from .cli import _local_package_record, _retain_package_archive
                from .packages import snapshot_package

                client = self._native(profile)
                package = resolved.repository.partition("/")[2]
                with tempfile.TemporaryDirectory(prefix="palimpsest-native-download-") as temporary:
                    archive = Path(temporary) / "package.oci.tar"
                    receipt = client.pull(package, tag=resolved.tag, digest=resolved.digest, destination=archive)
                    immutable = replace(resolved, tag=None, digest=receipt["digest"])
                    with snapshot_package(archive, manifest=receipt["digest"]) as snapshot:
                        retained = _retain_package_archive(self.roots, snapshot)
                        record = _local_package_record(
                            profile, immutable, snapshot, retained, project_id=client.project_id
                        )
                        archs = _architectures(list(snapshot.platforms))
                        platforms = list(snapshot.platforms)
                        runtime = self._import_runtime(snapshot) if snapshot.package_type == "runtime-bundle" else None
                    write_package_reference(self.roots, replace(record, published_digest=record.root_digest))
                    if resolved.tag:
                        write_package_reference(
                            self.roots,
                            replace(record, reference=resolved.canonical, published_digest=record.root_digest),
                        )
                result = {
                    "connection_id": connection_id,
                    "reference": immutable.canonical,
                    "source": str(retained),
                    "image": str(retained),
                    "manifest": record.root_digest,
                    "kind": record.package_type,
                    "archive": str(retained),
                    "archive_digest": record.archive_digest,
                    "package_manifest": record.root_digest,
                    "layers": [],
                    "architectures": archs,
                    "platforms": platforms,
                    **_run_info(record.package_type, archs, platforms),
                }
                if runtime is not None:
                    result.update(runtime)
                    if "source" in runtime:
                        result.pop("manifest", None)
                    result["archive"] = str(retained)
            else:
                destination = (
                    self.roots.state
                    / "registry-artifacts"
                    / (hashlib.sha256(resolved.canonical.encode()).hexdigest() + ".oci.tar")
                )
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Acquisition deliberately refuses overwrite; retain uniquely and record the verified reference.
                norm_platform = "linux/arm64" if platform in {"linux/arm64", "arm64", "aarch64"} else "linux/amd64"
                with tempfile.TemporaryDirectory(prefix=".transfer-", dir=destination.parent) as temporary:
                    output = Path(temporary) / "image.oci.tar"
                    receipt = pull_registry_oci_archive(
                        resolved.canonical,
                        output,
                        self.roots,
                        profile=profile,
                        platform=norm_platform,
                    )
                    archive_digest = digest_file(output)
                    destination = destination.parent / (digest_hex(archive_digest) + ".oci.tar")
                    if destination.exists():
                        if destination.is_symlink() or digest_file(destination) != archive_digest:
                            raise StateError("retained registry source identity mismatch")
                    else:
                        os.replace(output, destination)
                target_arch = "arm64" if "arm64" in receipt.platform else "amd64"
                target_platform = [{"os": "linux", "architecture": target_arch}]
                result = {
                    "connection_id": connection_id,
                    "reference": receipt.reference,
                    "source": str(destination),
                    "image": str(destination),
                    "manifest": receipt.root_digest,
                    "kind": "oci-image",
                    "layers": [],
                    "archive": str(destination),
                    "archive_digest": archive_digest,
                    "package_manifest": receipt.root_digest,
                    "selected_manifest": receipt.manifest_digest,
                    "architectures": [target_arch],
                    "source_cas_id": receipt.source_cas_id,
                    "source_binding_digest": receipt.source_binding_digest,
                    "platforms": target_platform,
                    **_run_info("oci-image", [target_arch], target_platform),
                }
        result["requested_reference"] = reference
        identity = hashlib.sha256((connection_id + "\0" + reference).encode()).hexdigest()
        path = self.roots.state / "remote-references" / (identity + ".json")
        atomic_write_json(path, {"schema": "palimpsest-remote-reference-v1", **result})
        return {**result, "receipt": str(path)}

    def _source(self, source: dict) -> dict:
        if not isinstance(source, dict):
            raise StateError("upload requires a selected local reference or managed build")
        store = ContentStore(self.roots.store)
        if source.get("kind") == "remote" and set(source) == {"kind", "connection_id", "reference"}:
            connection = _text(source["connection_id"], "connection")
            reference = _text(source["reference"], "reference")
            identity = hashlib.sha256((connection + "\0" + reference).encode()).hexdigest()
            path = self.roots.state / "remote-references" / (identity + ".json")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise StateError("remote receipt must be an owner-only regular file")
                record = _read_json(stream)
            if (
                not isinstance(record, dict)
                or record.get("schema") != "palimpsest-remote-reference-v1"
                or record.get("connection_id") != connection
                or record.get("requested_reference") != reference
            ):
                raise StateError("selected remote receipt identity mismatch")
            if record.get("kind") not in {"oci-image", "runtime-bundle"}:
                return self._source({"kind": "local", "reference": record.get("reference")})
            archive = record.get("archive")
            if (
                not isinstance(archive, str)
                or Path(archive).is_symlink()
                or digest_file(Path(archive)) != require_digest(record.get("archive_digest"))
            ):
                raise StateError("retained remote package source has changed")
            return {"kind": "package", "archive": archive, "manifest": require_digest(record.get("package_manifest"))}
        if source.get("kind") == "build" and set(source) == {"kind", "build_id"}:
            from .inventory import BUILD_ID_RE, _read_build_record
            from .state import pinned_owner_directory

            build_id = _text(source["build_id"], "build ID")
            if BUILD_ID_RE.fullmatch(build_id) is None:
                raise StateError("invalid upload build ID")
            with pinned_owner_directory(self.roots.builds) as fd:
                record = _read_build_record(fd, build_id)
            if record.get("status") not in {"success", "succeeded"}:
                raise StateError("only successful managed builds can be uploaded")
            archive = record.get("output_path")
            digest = record.get("output_oci_archive_digest")
            manifest = record.get("output_oci_manifest_digest")
            if not isinstance(archive, str) or not digest or not manifest:
                local_digest = record.get("runtime_block_digest") or record.get("output_digest")
                if local_digest:
                    return self._source({"kind": "local", "reference": local_digest})
                raise StateError("selected build has no retained artifact output")
            if Path(archive).is_symlink() or digest_file(Path(archive)) != require_digest(digest):
                raise StateError("selected build OCI output has changed")
            return {"kind": "package", "archive": archive, "manifest": require_digest(manifest)}
        if source.get("kind") == "package" and set(source) == {"kind", "reference"}:
            reference = _text(source["reference"], "local package reference")
            record = read_package_reference(self.roots, reference)
            if Path(record.archive).is_symlink() or digest_file(Path(record.archive)) != record.archive_digest:
                raise StateError("selected native local source has changed")
            return {"kind": "package", "archive": record.archive, "manifest": record.root_digest}
        if source.get("kind") != "local" or set(source) != {"kind", "reference"}:
            raise StateError("unsupported upload source selector")
        reference = _text(source["reference"], "local reference")
        if reference.startswith("sha256:"):
            digest = require_digest(reference)
        elif "/" not in reference and ":" not in reference:
            digest = read_tag_record(self.roots, reference).digest
        else:
            raise StateError(
                "local CAS upload requires a digest/tag; use the package selector for typed native references"
            )
        store.verify_blob(digest)
        metadata = _descriptor(store.read_metadata(digest), digest)
        return {"kind": metadata["kind"], "digest": digest}

    def upload(self, connection_id: str, source: dict, target: str) -> dict:
        selected = self._source(source)
        target = _text(target, "upload target", maximum=255)
        if connection_id == "legacy":
            if selected["kind"] not in {"cloud-image", "squashfs"}:
                raise StateError(
                    "legacy Hub upload supports cloud-image/SquashFS, not OCI image archives; select a native Hub package profile"
                )
            store = ContentStore(self.roots.store)
            chain = self._legacy_chain(selected["digest"], store.read_metadata, require_base=False)
            if chain[0]["kind"] == "cloud-image":
                from .cli import _layer_refs_from_store

                _layer_refs_from_store(store, [metadata["digest"] for metadata in chain[1:]], chain[0]["digest"])
            # Resolve and verify every local dependency before the first remote mutation.
            for index, metadata in enumerate(chain):
                store.verify_blob(metadata["digest"])
                metadata["name"] = (
                    target
                    if index == len(chain) - 1
                    else _text(
                        metadata.get("name") or "artifact-" + digest_hex(metadata["digest"])[:12],
                        "artifact dependency name",
                        maximum=255,
                    )
                )
            hub = self._hub()
            hub.identity("upload")
            receipts = []
            for index, metadata in enumerate(chain):
                descriptor = {key: value for key, value in metadata.items() if key in _DESCRIPTOR_FIELDS}
                descriptor["is_published"] = False
                if index == len(chain) - 1:
                    descriptor["name"] = target
                receipts.append(hub.push_blob(store.blob_path(metadata["digest"]), descriptor, resume=False))
            return {
                "connection_id": connection_id,
                "reference": selected["digest"],
                "kind": selected["kind"],
                "uploaded": len(receipts),
            }
        profile = self._profile(connection_id)
        resolved = self._reference(profile, target)
        if selected["kind"] != "package":
            raise StateError(
                "native/OCI upload requires an OCI archive or runtime-bundle package; legacy cloud-image/SquashFS use the Hub connection"
            )
        if profile.protocol != "palimpsest":
            raise StateError(
                "OCI registry upload is unavailable; local uploads target native or legacy Palimpsest Hub connections"
            )
        if resolved.tag is None or resolved.digest is not None:
            raise StateError("native publication requires a target tag")
        from .cli import _local_package_record, _retain_package_archive
        from .packages import snapshot_package

        client = self._native(profile)
        with snapshot_package(Path(selected["archive"]), manifest=selected["manifest"]) as snapshot:
            retained = _retain_package_archive(self.roots, snapshot)
            receipt = client.push(resolved.repository.partition("/")[2], resolved.tag, snapshot)
            record = _local_package_record(profile, resolved, snapshot, retained, project_id=client.project_id)
        path = write_package_reference(self.roots, replace(record, published_digest=receipt["digest"]))
        return {
            "connection_id": connection_id,
            "reference": resolved.canonical,
            "kind": record.package_type,
            "digest": record.root_digest,
            "receipt": str(path),
        }

    def start_download(self, operations, connection_id: str, reference: str, *, platform: str = "linux/amd64") -> dict:
        if connection_id == "legacy":
            require_digest(reference)
            self._hub()
        else:
            self._reference(self._profile(connection_id), reference)
        return operations.start(
            self.roots,
            "download",
            ["download", f"--platform={platform}", "--", connection_id, reference],
            module="palimpsest_local.remote_artifacts",
            hub_environment=self.hub_connection.environment()
            if connection_id == "legacy" and self.hub_connection is not None
            else None,
        )

    def start_upload(self, operations, connection_id: str, source: dict, target: str) -> dict:
        selected = self._source(source)
        _text(target, "upload target", maximum=255)
        if connection_id == "legacy":
            if selected["kind"] not in {"cloud-image", "squashfs"}:
                raise StateError("OCI archives upload to native Hub package profiles, not the legacy Hub")
            self._hub()
        else:
            profile = self._profile(connection_id)
            if profile.protocol != "palimpsest":
                raise StateError("OCI registry upload is unavailable; choose a Palimpsest Hub profile")
            if selected["kind"] != "package":
                raise StateError(
                    "cloud-image/SquashFS upload to legacy Hub; native Hub requires a runtime-bundle/OCI package"
                )
            resolved = self._reference(profile, target)
            if resolved.tag is None or resolved.digest is not None:
                raise StateError("native publication requires a target tag")
        return operations.start(
            self.roots,
            "upload",
            ["upload", "--", connection_id, json.dumps(source, separators=(",", ":")), target],
            module="palimpsest_local.remote_artifacts",
            hub_environment=self.hub_connection.environment()
            if connection_id == "legacy" and self.hub_connection is not None
            else None,
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fixed remote artifact transfer job")
    commands = parser.add_subparsers(dest="operation", required=True)
    download = commands.add_parser("download")
    download.add_argument("--platform", default="linux/amd64")
    download.add_argument("connection")
    download.add_argument("reference")
    upload = commands.add_parser("upload")
    upload.add_argument("connection")
    upload.add_argument("source")
    upload.add_argument("target")
    args = parser.parse_args(argv)
    from .web_job_client import _cancel_preparation

    signal.signal(signal.SIGUSR1, _cancel_preparation)
    signal.signal(signal.SIGTERM, _cancel_preparation)
    try:
        backend = RemoteArtifacts(init_roots())
        if args.operation == "download":
            result = backend.download(args.connection, args.reference, platform=args.platform)
        else:
            result = backend.upload(args.connection, json.loads(args.source), args.target)
        print(json.dumps(result, allow_nan=False))
        return 0
    except (PalimpsestError, OSError, ValueError, TypeError):
        # Never print response bodies, credential-helper output, URLs or arbitrary exception text.
        print(
            json.dumps(
                {
                    "error": "Remote artifact transfer failed; check selected connection credentials, artifact type, exact reference and dependencies."
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
