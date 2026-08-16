"""A synchronous, stdlib-only client for the Keystone-authenticated Palimpsest Hub API.

Every request is prefixed with :data:`HUB_API_PREFIX` and carries
``X-Auth-Token: <token>`` (and optional ``X-Project-Id: <project_id>``). Downloads (:meth:`HubClient.pull_blob`) verify SHA-256
after every transfer and resume interrupted transfers from a ``<destination>.part`` file
plus an owner-only JSON sidecar. Uploads (:meth:`HubClient.push_blob`) honor the Hub's
``completed``/``registered`` existing-blob short-circuit and persist enough state to
resume an interrupted upload across process restarts against a Hub that exposes the
additive offset-query route; against an older Hub (``GET /uploads/{id}`` answering 404/405)
the client falls back to starting a fresh session rather than trusting an unverifiable local offset.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .digest import digest_file, normalize_digest
from .errors import DigestMismatchError, HubError
from .state import delete_transfer, fingerprint, read_transfer, write_transfer

HUB_API_PREFIX = "/v1"

MEDIA_TYPE_LAYER_SQUASHFS = "application/vnd.afterglow.palimpsest.layer.squashfs.v1"
MEDIA_TYPE_LAYER_CONFIG = "application/vnd.afterglow.palimpsest.layer.config.v1+json"
MEDIA_TYPE_IMAGE_QCOW2 = "application/vnd.afterglow.palimpsest.image.qcow2.v1"
MEDIA_TYPE_IMAGE_RAW = "application/vnd.afterglow.palimpsest.image.raw.v1"

KIND_CLOUD_IMAGE = "cloud-image"

DISK_FORMAT_MEDIA_TYPES: dict[str, str] = {
    "qcow2": MEDIA_TYPE_IMAGE_QCOW2,
    "raw": MEDIA_TYPE_IMAGE_RAW,
}

_DOWNLOAD_CHUNK = 1024 * 1024
_UPLOAD_CHUNK = 8 * 1024 * 1024
_MAX_UPLOAD_CONFLICT_RETRIES = 3
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    """Best-effort read of a small local JSON sidecar; ``None`` when absent or corrupt."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` as owner-only (0600) bytes via ``fsync`` + rename."""
    directory = path.parent
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(data).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class HubClient:
    """Talks to a single Palimpsest Hub instance over ``urllib`` — no third-party deps."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        project_id: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        normalized_base = (base_url or "").strip().rstrip("/")
        if not normalized_base:
            raise HubError("hub base_url must be nonempty")
        if not token or not token.strip():
            raise HubError("hub token must be nonempty")
        if timeout_seconds <= 0:
            raise HubError("timeout_seconds must be positive")
        self._base_url = normalized_base
        self._token = token.strip()
        self._project_id = project_id.strip() if project_id and project_id.strip() else None
        self._timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirectHandler)

    def __repr__(self) -> str:
        return f"HubClient(base_url={self._base_url!r}, timeout_seconds={self._timeout_seconds!r})"

    # -- low-level transport --------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***REDACTED***") if self._token else text

    def _open(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        prefix = HUB_API_PREFIX.rstrip("/")
        if not path.startswith(prefix):
            full_path = f"{prefix}{path}"
        else:
            full_path = path
        url = f"{self._base_url}{full_path}"
        if query:
            filtered = {key: value for key, value in query.items() if value is not None}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered)}"
        request_headers = {"X-Auth-Token": self._token}
        if self._project_id:
            request_headers["X-Project-Id"] = self._project_id
        if content_type is not None:
            request_headers["Content-Type"] = content_type
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            return self._opener.open(request, timeout=self._timeout_seconds)  # noqa: S310 - operator-controlled URL
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            raise HubError(self._redact(f"cannot reach hub at {self._base_url}: {exc.reason}")) from exc

    def _wrap_http_error(self, exc: urllib.error.HTTPError, method: str, path: str) -> HubError:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        return HubError(self._redact(f"{method} {path} failed (HTTP {exc.code}): {detail[:300]}"))

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, Any] | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            with self._open(
                method,
                path,
                query=query,
                body=body,
                content_type="application/json" if body is not None else None,
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise self._wrap_http_error(exc, method, path) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HubError(f"malformed JSON response from hub for {method} {path}") from exc

    # -- search / read ----------------------------------------------------------

    def list_images(
        self,
        *,
        ubuntu_base: str | None = None,
        arch: str | None = None,
        os_variant: str | None = None,
        disk_format: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise HubError("limit must be between 1 and 200")
        query = {
            "ubuntu_base": ubuntu_base,
            "arch": arch,
            "os_variant": os_variant,
            "disk_format": disk_format,
            "limit": limit,
        }
        return self._json_request("GET", "/images", query=query)

    def list_layers(
        self,
        *,
        name: str | None = None,
        kind: str | None = None,
        parent_digest: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise HubError("limit must be between 1 and 200")
        normalized_parent = normalize_digest(parent_digest) if parent_digest is not None else None
        query = {"name": name, "kind": kind, "parent_digest": normalized_parent, "limit": limit}
        return self._json_request("GET", "/layers", query=query)

    def get_layer(self, digest: str) -> dict[str, Any]:
        normalized = normalize_digest(digest)
        return self._json_request("GET", f"/layers/{normalized}")

    def get_ancestors(self, digest: str) -> list[dict[str, Any]]:
        normalized = normalize_digest(digest)
        return self._json_request("GET", f"/layers/{normalized}/ancestors")

    # -- download -----------------------------------------------------------------

    def pull_blob(self, digest: str, destination: str | Path, *, resume: bool = True) -> Path:
        """Download and verify a blob into ``destination``, resuming a prior partial transfer.

        Streams into ``<destination>.part`` plus an owner-only ``.part.json`` sidecar
        recording the requested digest. A prior partial is resumed with
        ``Range: bytes=<part-size>-`` only when the sidecar's digest matches; an
        unexpected/malformed ``Content-Range`` or a final digest mismatch deletes both
        files and raises. Success atomically renames the verified file into place.
        """
        normalized = normalize_digest(digest)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        part_path = destination.parent / f"{destination.name}.part"
        sidecar_path = destination.parent / f"{destination.name}.part.json"

        offset = 0
        if resume and part_path.is_file() and sidecar_path.is_file():
            record = _read_json(sidecar_path)
            if record is not None and record.get("digest") == normalized:
                offset = part_path.stat().st_size
        if offset == 0:
            part_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)

        _atomic_write_json(sidecar_path, {"digest": normalized})

        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        try:
            with self._open("GET", f"/layers/{normalized}/blob", headers=headers) as resp:
                if offset > 0 and resp.status == 206:
                    content_range = resp.headers.get("Content-Range", "")
                    match = _CONTENT_RANGE_RE.match(content_range)
                    if match is None:
                        raise HubError(f"unexpected Content-Range for resumed download: {content_range!r}")
                    start_byte, end_byte, total_bytes = (int(match.group(i)) for i in (1, 2, 3))
                    if start_byte != offset or end_byte < start_byte or total_bytes <= end_byte:
                        raise HubError(f"invalid Content-Range boundaries: {content_range!r}")
                    with part_path.open("ab") as handle:
                        shutil.copyfileobj(resp, handle, _DOWNLOAD_CHUNK)
                elif resp.status == 200:
                    with part_path.open("wb") as handle:
                        shutil.copyfileobj(resp, handle, _DOWNLOAD_CHUNK)
                else:
                    raise HubError(f"unexpected status {resp.status} for blob download")
        except urllib.error.HTTPError as exc:
            part_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            raise self._wrap_http_error(exc, "GET", f"/layers/{normalized}/blob") from exc
        except HubError:
            part_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            raise

        actual = digest_file(part_path)
        if actual != normalized:
            part_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            raise DigestMismatchError(f"downloaded blob digest mismatch: expected {normalized}, got {actual}")

        os.replace(part_path, destination)
        sidecar_path.unlink(missing_ok=True)
        return destination

    def pull_bundle(self, leaf_digest: str, destination: str | Path, *, include_base_image: bool = False) -> Path:
        """Stream ``POST /bundles`` for a single leaf into ``destination`` (a tar path).

        Bytes are written to ``<destination>.part`` and atomically renamed on success;
        content verification/expansion into an OCI layout is the caller's (oci_layout)
        responsibility, not this transport call's.
        """
        normalized = normalize_digest(leaf_digest)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        part_path = destination.parent / f"{destination.name}.part"
        payload = {"refs": [normalized], "include_base_image": bool(include_base_image)}
        body = json.dumps(payload).encode("utf-8")
        try:
            with self._open("POST", "/bundles", body=body, content_type="application/json") as resp:
                with part_path.open("wb") as handle:
                    shutil.copyfileobj(resp, handle, _DOWNLOAD_CHUNK)
        except urllib.error.HTTPError as exc:
            part_path.unlink(missing_ok=True)
            raise self._wrap_http_error(exc, "POST", "/bundles") from exc
        os.replace(part_path, destination)
        return destination

    # -- upload -----------------------------------------------------------------------

    def _probe_upload_offset(self, session_id: str, digest: str) -> int | None:
        """Return the hub's authoritative received-byte offset for ``session_id``.

        Returns ``None`` when the hub predates the additive ``GET /uploads/{id}`` route
        (404/405) or the session/digest no longer matches — either way the caller must
        restart a fresh session rather than trust an unverifiable local offset.
        """
        try:
            with self._open("GET", f"/uploads/{session_id}") as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                return None
            raise self._wrap_http_error(exc, "GET", f"/uploads/{session_id}") from exc
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise HubError("malformed upload session response from hub") from exc
        if payload.get("declared_digest") != digest:
            return None
        received = payload.get("received_bytes")
        if not isinstance(received, int) or received < 0:
            raise HubError("malformed upload session response from hub: received_bytes")
        return received

    def push_blob(self, path: str | Path, metadata: dict[str, Any], *, resume: bool = True) -> dict[str, Any]:
        """Upload ``path`` (declaring ``digest`` first) and finalize it with ``metadata``.

        An already-registered blob short-circuits without PATCH/PUT; an existing-but-
        unregistered blob is a typed error (no upload session exists to attach metadata
        to). When ``resume`` is True, an interrupted upload persists state and a later
        call reconciles via :meth:`_probe_upload_offset` before resuming.
        """
        source = Path(path)
        if not source.is_file():
            raise HubError(f"upload source is not a file: {source}")
        digest = digest_file(source)
        size = source.stat().st_size
        fp = fingerprint(source) if resume else None

        session_id: str | None = None
        offset = 0

        if resume and fp is not None:
            record = read_transfer(digest)
            if record is not None and record.get("path_fingerprint") == fp and record.get("digest") == digest:
                candidate = record.get("session_id")
                if isinstance(candidate, str) and candidate:
                    probed = self._probe_upload_offset(candidate, digest)
                    if probed is not None:
                        session_id, offset = candidate, probed
            if session_id is None:
                delete_transfer(digest)

        if session_id is None:
            started = self._json_request("POST", "/uploads", {"digest": digest})
            if started.get("completed"):
                if not started.get("registered"):
                    raise HubError(
                        f"blob {digest} exists in hub storage but is not registered as a layer; "
                        "resolve the orphaned blob before pushing again"
                    )
                if resume:
                    delete_transfer(digest)
                return {"blob_digest": digest, "already_present": True, "registered": True}
            session_id = started.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise HubError("hub did not return an upload session id")
            offset = int(started.get("received_bytes") or 0)
            if resume and fp is not None:
                write_transfer(
                    {"digest": digest, "path_fingerprint": fp, "session_id": session_id, "acknowledged_offset": offset}
                )

        if offset > size:
            raise HubError(f"upload session offset {offset} exceeds local file size {size} for {source}")

        self._upload_chunks(session_id, source, offset, size, digest, fp, resume)

        try:
            with self._open(
                "PUT",
                f"/uploads/{session_id}",
                body=json.dumps(metadata).encode("utf-8"),
                content_type="application/json",
                headers={"Upload-Offset": str(size)},
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._wrap_http_error(exc, "PUT", f"/uploads/{session_id}") from exc
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise HubError(f"malformed JSON response from hub for PUT /uploads/{session_id}") from exc
        if resume:
            delete_transfer(digest)
        return result

    def _upload_chunks(
        self,
        session_id: str,
        source: Path,
        offset: int,
        size: int,
        digest: str,
        fp: str | None,
        resume: bool,
    ) -> None:
        conflict_retries = 0
        with source.open("rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                try:
                    with self._open(
                        "PATCH",
                        f"/uploads/{session_id}",
                        body=chunk,
                        content_type="application/octet-stream",
                        headers={"Upload-Offset": str(offset)},
                    ) as resp:
                        raw = resp.read()
                except urllib.error.HTTPError as exc:
                    if exc.code == 409 and conflict_retries < _MAX_UPLOAD_CONFLICT_RETRIES:
                        conflict_retries += 1
                        offset = self._reconcile_conflict(exc, size, session_id, digest, fp, resume)
                        continue
                    raise self._wrap_http_error(exc, "PATCH", f"/uploads/{session_id}") from exc
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError as exc:
                    raise HubError("malformed PATCH response from hub") from exc
                expected = offset + len(chunk)
                received = payload.get("received_bytes")
                if received != expected:
                    raise HubError(f"upload offset diverged from hub: expected {expected}, hub reported {received!r}")
                offset = received
                conflict_retries = 0
                if resume and fp is not None:
                    write_transfer(
                        {
                            "digest": digest,
                            "path_fingerprint": fp,
                            "session_id": session_id,
                            "acknowledged_offset": offset,
                        }
                    )

    def _reconcile_conflict(
        self,
        exc: urllib.error.HTTPError,
        size: int,
        session_id: str,
        digest: str,
        fp: str | None,
        resume: bool,
    ) -> int:
        server_offset = exc.headers.get("Upload-Offset") if exc.headers is not None else None
        if server_offset is None:
            raise self._wrap_http_error(exc, "PATCH", f"/uploads/{session_id}") from exc
        try:
            new_offset = int(server_offset)
        except ValueError as parse_exc:
            raise HubError(f"hub returned a malformed Upload-Offset header: {server_offset!r}") from parse_exc
        if not 0 <= new_offset <= size:
            raise HubError(f"hub reported an out-of-range Upload-Offset: {new_offset}") from exc
        if resume and fp is not None:
            write_transfer(
                {
                    "digest": digest,
                    "path_fingerprint": fp,
                    "session_id": session_id,
                    "acknowledged_offset": new_offset,
                }
            )
        return new_offset
