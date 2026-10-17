"""Read-only Docker Hub metadata over bounded, verified HTTPS.

Only manifest and config bytes are fetched. Layer entries describe compressed
registry blobs; inspecting them does not download or verify those blobs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import platform
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import registry, registry_intake
from .errors import ArtifactValidationError, PalimpsestError, StateError
from .oci_image import strict_json_object
from .registry import RegistryProfile

_MAX_JSON = 4 * 1024 * 1024
_MAX_DESCRIPTORS = 128
_TIMEOUT = 30.0
_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io"}
_REGISTRY = "https://registry-1.docker.io"
_AUTH = "https://auth.docker.io/token"
_SEARCH = "https://hub.docker.com/v2/search/repositories/"
_IMAGE_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
_INDEX_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}
_CONFIG_TYPES = {"application/vnd.docker.container.image.v1+json", "application/vnd.oci.image.config.v1+json"}
_ACCEPT = ", ".join(sorted(_IMAGE_TYPES | _INDEX_TYPES))
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PLATFORM = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _text(value: object, field: str, *, empty: bool = False, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not value and not empty)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise StateError(f"invalid Docker Hub {field}")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise StateError("invalid Docker Hub SHA-256 digest")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise StateError("Docker Hub limit must be between 1 and 100")
    return value


def _repository(value: object) -> str:
    try:
        result = registry._normalize_repository_path(value, "Docker Hub repository")
    except PalimpsestError:
        raise StateError("invalid Docker Hub repository") from None
    return result if "/" in result else "library/" + result


def _tag(value: object) -> str:
    if not isinstance(value, str) or registry._TAG_RE.fullmatch(value) is None:
        raise StateError("invalid Docker Hub tag boundary")
    return value


def _descriptor(value: object) -> dict:
    if not isinstance(value, dict):
        raise StateError("invalid Docker Hub descriptor")
    size = value.get("size")
    if type(size) is not int or not 0 <= size <= 2**63 - 1:
        raise StateError("invalid Docker Hub descriptor size")
    return {
        "digest": _digest(value.get("digest")),
        "size_bytes": size,
        "media_type": _text(value.get("mediaType"), "descriptor media type", maximum=256),
    }


def _platform(value: object) -> dict:
    if not isinstance(value, dict):
        raise StateError("invalid Docker Hub platform")
    result = {}
    for field in ("os", "architecture", "variant"):
        item = value.get(field)
        if item is None and field == "variant":
            continue
        if not isinstance(item, str) or _PLATFORM.fullmatch(item) is None:
            raise StateError("invalid Docker Hub platform")
        result[field] = item
    return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StateError("Docker Hub request exceeded its time limit")
    return min(_TIMEOUT, remaining)


def _config_redirect_url(value: object) -> str:
    """Allow one Docker-owned blob CDN hop, without registry credentials."""
    value = _text(value, "config CDN URL", maximum=8192)
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise StateError("invalid Docker Hub config CDN URL") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {
            "production.cloudfront.docker.com",
            "production.cloudflare.docker.com",
        }
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise StateError("Docker Hub config redirect is outside its trusted HTTPS CDN boundary")
    return value


def _http_error(code: int, headers) -> StateError:
    messages = {
        401: "Docker Hub authentication failed (HTTP 401)",
        403: "Docker Hub access denied (HTTP 403)",
        404: "Docker Hub repository or manifest not found (HTTP 404)",
        429: "Docker Hub rate limit exceeded (HTTP 429)",
    }
    message = messages.get(code, f"Docker Hub request failed (HTTP {code})")
    if code == 429:
        retry = headers.get("Retry-After", "")
        if isinstance(retry, str) and re.fullmatch(r"[0-9]{1,4}", retry) and int(retry) <= 3600:
            message += f"; retry after {int(retry)} seconds"
    return StateError(message)


class DockerHubCatalog:
    """Read-only catalog bound to one selected Docker credential authority."""

    def __init__(self, profile: RegistryProfile):
        if profile.protocol != "oci" or profile.endpoint not in _HOSTS or profile.plain_http or profile.tls_skip_verify:
            raise StateError("Docker Hub catalog requires a verified Docker Hub HTTPS profile")
        self.profile = profile
        self.connection_id = "registry:" + profile.alias
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            for path in profile.ca:
                context.load_verify_locations(cafile=path)
            self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())
        except (OSError, ValueError):
            raise StateError("Docker Hub TLS configuration is invalid") from None

    def _json(
        self,
        url: str,
        *,
        deadline: float,
        headers: dict | None = None,
        data: bytes | None = None,
        config_redirect: bool = False,
    ):
        # Challenge realms, pagination URLs and descriptor URLs are never followed.
        # Only config blobs may take one explicit, credential-free Docker CDN hop.
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in {
            "hub.docker.com",
            "auth.docker.io",
            "registry-1.docker.io",
        }:
            raise StateError("Docker Hub request is outside the HTTPS endpoint boundary")
        request = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
        try:
            expected_url = url
            try:
                response = self._opener.open(request, timeout=_remaining(deadline))
            except urllib.error.HTTPError as redirect:
                if (
                    not config_redirect
                    or redirect.code not in {302, 307}
                    or parsed.netloc != "registry-1.docker.io"
                    or re.fullmatch(r"/v2/.+/blobs/sha256:[0-9a-f]{64}", parsed.path) is None
                ):
                    raise
                location = redirect.headers.get("Location", "")
                redirect.close()
                expected_url = _config_redirect_url(location)
                anonymous = urllib.request.Request(expected_url, headers={"Accept": "application/json"})
                response = self._opener.open(anonymous, timeout=_remaining(deadline))
            with response:
                if response.geturl() != expected_url:
                    raise StateError("Docker Hub redirects are not allowed")
                if not 200 <= response.status < 300:
                    raise _http_error(response.status, response.headers)
                chunks = []
                size = 0
                reader = getattr(response, "read1", response.read)
                while True:
                    _remaining(deadline)
                    chunk = reader(min(64 * 1024, _MAX_JSON + 1 - size))
                    _remaining(deadline)
                    size += len(chunk)
                    if size > _MAX_JSON:
                        raise StateError("Docker Hub response exceeds its 4 MiB byte limit")
                    if not chunk:
                        break
                    chunks.append(chunk)
                payload = b"".join(chunks)
                digest = "sha256:" + hashlib.sha256(payload).hexdigest()
                advertised = response.headers.get("Docker-Content-Digest")
                if advertised is not None and _digest(advertised) != digest:
                    raise StateError("Docker Hub content digest mismatch")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        except urllib.error.HTTPError as exc:
            error = _http_error(exc.code, exc.headers)
            exc.close()
            raise error from None
        except (OSError, urllib.error.URLError, ValueError):
            raise StateError("Docker Hub HTTPS connection failed") from None
        try:
            document = strict_json_object(payload, "Docker Hub metadata")
        except (ArtifactValidationError, RecursionError):
            raise StateError("Docker Hub returned invalid JSON") from None
        return document, digest, content_type, size

    def _token(self, repository: str, deadline: float) -> str:
        query = {"service": "registry.docker.io", "scope": f"repository:{repository}:pull"}
        headers = {}
        body = None
        try:
            with tempfile.TemporaryDirectory(prefix="palimpsest-docker-metadata-") as temporary:
                private = registry_intake._stage_docker_auth(
                    registry.resolve_docker_config_dir(),
                    self.profile.endpoint,
                    Path(temporary),
                    timeout_seconds=_remaining(deadline),
                )
                if private is not None:
                    credentials = json.loads((private / "config.json").read_bytes())["auths"][self.profile.endpoint]
                    if not isinstance(credentials, dict):
                        raise ValueError
                    if credentials.get("identitytoken"):
                        refresh = _text(credentials["identitytoken"], "identity token", maximum=16384)
                        body = urllib.parse.urlencode(
                            {
                                **query,
                                "grant_type": "refresh_token",
                                "refresh_token": refresh,
                                "client_id": "palimpsest",
                            }
                        ).encode("ascii")
                        headers["Content-Type"] = "application/x-www-form-urlencoded"
                    elif credentials.get("auth"):
                        encoded = credentials["auth"]
                        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                        username, separator, password = decoded.partition(":")
                        if not separator or not username or not password or len(decoded) > 16384:
                            raise ValueError
                        headers["Authorization"] = "Basic " + base64.b64encode(decoded.encode()).decode("ascii")
                    else:
                        raise ValueError
                document, _, _, _ = self._json(
                    _AUTH if body is not None else _AUTH + "?" + urllib.parse.urlencode(query),
                    deadline=deadline,
                    headers=headers,
                    data=body,
                )
        except (OSError, ValueError, KeyError, TypeError, binascii.Error):
            raise StateError("Docker Hub credential configuration is invalid") from None
        except registry_intake.RegistryIntakeError:
            raise StateError("Docker Hub credential lookup failed") from None
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) or len(token) > 16384:
            raise StateError("Docker Hub authentication returned an invalid token")
        return token

    def search(self, query: str, *, limit: int = 30, cursor: str | None = None) -> dict:
        limit = _limit(limit)
        query = _text(query, "search query", empty=True)
        if cursor in (None, ""):
            page = 1
        elif isinstance(cursor, str) and re.fullmatch(r"[1-9][0-9]{0,8}", cursor):
            page = int(cursor) + 1
        else:
            raise StateError("invalid Docker Hub search page cursor")
        document, _, _, _ = self._json(
            _SEARCH + "?" + urllib.parse.urlencode({"query": query, "page_size": limit, "page": page}),
            deadline=time.monotonic() + _TIMEOUT,
        )
        results = document.get("results")
        if not isinstance(results, list) or len(results) > limit:
            raise StateError("invalid Docker Hub repository search results")
        items = []
        for row in results:
            if not isinstance(row, dict):
                raise StateError("invalid Docker Hub repository search result")
            repository = _repository(row.get("repo_name"))
            items.append(
                {
                    "repository": repository,
                    "name": repository,
                    "reference": None,
                    "kind": "oci-image",
                    "download_supported": False,
                    "details_supported": False,
                    "architectures": [],
                    "run_supported": False,
                    "run_limitation": "Choose an exact tag to inspect its platforms and layers.",
                }
            )
        return {
            "connection_id": self.connection_id,
            "search_mode": "public-repository-search",
            "items": items,
            "next_cursor": str(page) if document.get("next") else None,
        }

    def tags(self, repository: str, *, query: str = "", limit: int = 30, cursor: str | None = None) -> dict:
        repository = _repository(repository)
        limit = _limit(limit)
        query = _text(query, "tag query", empty=True)
        parameters: dict[str, str | int] = {"n": limit + 1}
        if cursor not in (None, ""):
            parameters["last"] = _tag(cursor)
        deadline = time.monotonic() + _TIMEOUT
        token = self._token(repository, deadline)
        document, _, _, _ = self._json(
            f"{_REGISTRY}/v2/{repository}/tags/list?" + urllib.parse.urlencode(parameters),
            deadline=deadline,
            headers={"Authorization": "Bearer " + token},
        )
        tags = document.get("tags")
        if tags is None:
            tags = []
        if document.get("name") != repository or not isinstance(tags, list) or len(tags) > limit + 1:
            raise StateError("invalid Docker Hub tag list")
        tags = [_tag(tag) for tag in tags]
        if len(set(tags)) != len(tags):
            raise StateError("Docker Hub returned duplicate tag boundaries")
        page = tags[:limit]
        if len(tags) > limit and page[-1] == cursor:
            raise StateError("Docker Hub tag page did not advance")
        return {
            "connection_id": self.connection_id,
            "search_mode": "exact-reference/tags",
            "items": [
                {
                    "reference": f"{self.profile.endpoint}/{repository}:{tag}",
                    "repository": repository,
                    "tag": tag,
                    "name": f"{repository}:{tag}",
                    "kind": "oci-image",
                    "download_supported": False,
                    "details_supported": True,
                    "architectures": [],
                    "run_supported": False,
                    "run_limitation": "Inspect the exact manifest before downloading or configuring a VM.",
                }
                for tag in page
                if query.casefold() in tag.casefold()
            ],
            # The lookahead belongs to the next page, not this boundary.
            "next_cursor": page[-1] if len(tags) > limit else None,
            "message": "Tag filters apply to each server page. Continue to the next page for more matches, or enter an exact repository:tag to inspect directly.",
        }

    def _manifest(self, repository: str, selector: str, token: str, deadline: float, descriptor: dict | None = None):
        document, digest, content_type, size = self._json(
            f"{_REGISTRY}/v2/{repository}/manifests/{selector}",
            deadline=deadline,
            headers={"Authorization": "Bearer " + token, "Accept": _ACCEPT},
        )
        if selector.startswith("sha256:") and digest != selector:
            raise StateError("Docker Hub manifest digest mismatch")
        media_type = document.get("mediaType", content_type)
        if (
            media_type not in _IMAGE_TYPES | _INDEX_TYPES
            or type(document.get("schemaVersion")) is not int
            or document["schemaVersion"] != 2
        ):
            raise StateError("unsupported Docker Hub manifest media type or schema")
        if content_type in _IMAGE_TYPES | _INDEX_TYPES and content_type != media_type:
            raise StateError("Docker Hub manifest media type mismatch")
        if descriptor is not None and (size != descriptor["size_bytes"] or media_type != descriptor["media_type"]):
            raise StateError("Docker Hub manifest descriptor mismatch")
        return document, digest, media_type, size

    def inspect(self, reference: str, *, manifest: str | None = None) -> dict:
        reference = _text(reference, "reference")
        authority, separator, path = reference.partition("/")
        if not separator or authority != self.profile.endpoint or authority not in _HOSTS:
            raise StateError("Docker Hub reference is outside the selected registry connection")
        name, at, pin = path.partition("@")
        if at:
            pin = _digest(pin)
        repository, colon, tag = name.partition(":")
        repository = _repository(repository)
        if colon:
            tag = _tag(tag)
        selector = pin if at else tag if colon else "latest"
        canonical = f"{authority}/{repository}" + (f":{tag}" if colon else "") + (f"@{pin}" if at else "")
        if not at and not colon:
            canonical += ":latest"
        if manifest is not None:
            manifest = _digest(manifest)
        deadline = time.monotonic() + _TIMEOUT
        token = self._token(repository, deadline)
        root, root_digest, root_type, root_size = self._manifest(repository, selector, token, deadline)
        is_index = root_type in _INDEX_TYPES
        platforms = []
        selected = None
        document = root
        manifest_digest = root_digest
        if is_index:
            descriptors = root.get("manifests")
            if not isinstance(descriptors, list) or len(descriptors) > _MAX_DESCRIPTORS:
                raise StateError("invalid Docker Hub index descriptor list")
            for raw in descriptors:
                descriptor = _descriptor(raw)
                entry = {**descriptor, **_platform(raw.get("platform", {"os": "unknown", "architecture": "unknown"}))}
                platforms.append(entry)
                if manifest == descriptor["digest"]:
                    selected = entry
            manifest_digest = None
            if manifest is not None:
                if selected is None:
                    raise StateError("selected manifest is not present in the Docker Hub parent index")
                if selected["media_type"] not in _IMAGE_TYPES:
                    raise StateError("selected Docker Hub descriptor is not an image manifest")
                document, manifest_digest, _, _ = self._manifest(repository, manifest, token, deadline, selected)
        elif manifest is not None and manifest != root_digest:
            raise StateError("selected manifest is not the Docker Hub image manifest")

        layers = []
        config = None
        if manifest_digest is not None:
            raw_layers = document.get("layers")
            if not isinstance(raw_layers, list) or len(raw_layers) > _MAX_DESCRIPTORS:
                raise StateError("invalid Docker Hub layer descriptor list")
            layers = [{"ordinal": ordinal, **_descriptor(raw)} for ordinal, raw in enumerate(raw_layers)]
            config = _descriptor(document.get("config"))
            if config["media_type"] not in _CONFIG_TYPES or config["size_bytes"] > _MAX_JSON:
                raise StateError("unsupported Docker Hub config descriptor")
            metadata, config_digest, _, size = self._json(
                f"{_REGISTRY}/v2/{repository}/blobs/{config['digest']}",
                deadline=deadline,
                headers={"Authorization": "Bearer " + token},
                config_redirect=True,
            )
            if config_digest != config["digest"] or size != config["size_bytes"]:
                raise StateError("Docker Hub config digest or size mismatch")
            image_platform = _platform(metadata)
            config.update(image_platform)
            if selected is not None:
                for field in ("os", "architecture", "variant"):
                    if field in selected and field in image_platform and selected[field] != image_platform[field]:
                        raise StateError("Docker Hub config platform does not match the parent index")
            if not is_index:
                platforms = [
                    {"digest": root_digest, "size_bytes": root_size, "media_type": root_type, **image_platform}
                ]
            rootfs = metadata.get("rootfs")
            if isinstance(rootfs, dict) and "diff_ids" in rootfs:
                diff_ids = rootfs["diff_ids"]
                if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
                    raise StateError("invalid Docker Hub config layer diff IDs")
                for layer, diff_id in zip(layers, diff_ids, strict=True):
                    layer["diff_id"] = _digest(diff_id)

        visible = [selected] if selected is not None else platforms
        supported = any(
            item.get("os") == "linux" and item.get("architecture") in {"amd64", "arm64", "x86_64", "aarch64"}
            for item in visible
        )
        return {
            "connection_id": self.connection_id,
            "reference": canonical,
            "immutable_reference": f"{authority}/{repository}@{root_digest}",
            "download_reference": f"{authority}/{repository}@{manifest_digest or root_digest}",
            "name": canonical,
            "kind": "oci-image",
            "digest": root_digest,
            "media_type": root_type,
            "size_bytes": root_size,
            "platforms": platforms,
            "architectures": sorted({item["architecture"] for item in visible}),
            "is_index": is_index,
            "manifest_digest": manifest_digest,
            "config": config,
            "layers": layers,
            "total_size_bytes": sum(layer["size_bytes"] for layer in layers),
            "download_supported": supported,
            "download_limitation": "OCI registry acquisition supports linux/amd64 and linux/arm64 container roots, not conventional VM boot disks.",
            "run_supported": any(item.get("os") == "linux" and item.get("architecture") == "amd64" for item in visible)
            and platform.system() == "Linux"
            and platform.machine().lower() in {"amd64", "x86_64"},
            "run_limitation": "Linux amd64 OCI container roots require the OCI-root runtime and runtime preflight; other hosts/platforms are unsupported.",
            "runtime_kind": "oci-root",
        }
