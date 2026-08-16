"""Focused unit tests for HubClient using stdlib HTTP server fakes."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from palimpsest_local import hub
from palimpsest_local.errors import HubError
from palimpsest_local.hub import HubClient


def test_hub_module_uses_only_stdlib_and_package():
    tree = ast.parse(Path(hub.__file__).read_text(encoding="utf-8"))
    top_level = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            top_level.add(node.module.split(".")[0])
    assert top_level <= {
        "__future__",
        "contextlib",
        "json",
        "os",
        "re",
        "shutil",
        "tempfile",
        "urllib",
        "pathlib",
        "typing",
    }


def test_hub_client_constructor_validation_and_repr():
    client = HubClient("http://localhost:8080/", "secret-token")
    assert repr(client) == "HubClient(base_url='http://localhost:8080', timeout_seconds=300.0)"
    assert "secret-token" not in repr(client)

    with pytest.raises(HubError, match="base_url"):
        HubClient("", "token")

    with pytest.raises(HubError, match="token"):
        HubClient("http://localhost:8080", "  ")

    with pytest.raises(HubError, match="timeout_seconds"):
        HubClient("http://localhost:8080", "token", timeout_seconds=0)


class MockHubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logging during tests

    def do_GET(self):
        auth = self.headers.get("X-Auth-Token")
        if auth != "valid-token":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        if self.path.startswith("/v1/images"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([{"name": "ubuntu-24.04", "kind": "cloud-image"}]).encode())
        elif self.path.startswith("/v1/layers/sha256:" + "a" * 64 + "/blob"):
            content = b"fake squashfs layer bytes"
            range_hdr = self.headers.get("Range")
            if range_hdr and range_hdr.startswith("bytes="):
                offset = int(range_hdr.split("=")[1].rstrip("-"))
                if offset < len(content):
                    slice_bytes = content[offset:]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {offset}-{len(content) - 1}/{len(content)}")
                    self.send_header("Content-Length", str(len(slice_bytes)))
                    self.end_headers()
                    self.wfile.write(slice_bytes)
                    return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        auth = self.headers.get("X-Auth-Token")
        if auth != "valid-token":
            self.send_response(401)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        if self.path == "/v1/uploads":
            digest = body.get("digest")
            if digest == "sha256:" + "0" * 64:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"completed": True, "registered": True}).encode())
            elif digest == "sha256:" + "1" * 64:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"completed": True, "registered": False}).encode())
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"session_id": "sess123", "completed": False, "received_bytes": 0}).encode()
                )
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), MockHubHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_hub_client_list_images(mock_server: str):
    client = HubClient(mock_server, "valid-token")
    images = client.list_images()
    assert len(images) == 1
    assert images[0]["name"] == "ubuntu-24.04"


def test_hub_client_redacts_tokens_on_error(mock_server: str):
    client = HubClient(mock_server, "secret-token")
    with pytest.raises(HubError) as exc_info:
        client.list_images()
    assert "secret-token" not in str(exc_info.value)
    assert "HTTP 401" in str(exc_info.value)


def test_hub_client_blocks_redirects():
    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://evil.com/leak")
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HubClient(f"http://127.0.0.1:{port}", "secret-token")
        with pytest.raises(HubError) as exc_info:
            client.list_images()
        assert "secret-token" not in str(exc_info.value)
        assert "HTTP 302" in str(exc_info.value)
    finally:
        server.shutdown()


class _Response:
    def __init__(self, payload: dict | None = None, *, status: int = 200):
        self._payload = json.dumps(payload).encode() if payload is not None else b""
        self.status = status
        self.headers = Message()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _http_conflict(offset: int) -> HTTPError:
    headers = Message()
    headers["Upload-Offset"] = str(offset)
    return HTTPError("http://hub.invalid/uploads/session", 409, "Conflict", headers, io.BytesIO(b"offset"))


def test_push_registered_blob_short_circuits_without_chunk_or_final_put(tmp_path: Path, monkeypatch):
    source = tmp_path / "layer.squashfs"
    source.write_bytes(b"registered")
    calls: list[tuple[str, str, dict[str, str]]] = []

    def fake_open(method, path, **kwargs):
        calls.append((method, path, kwargs.get("headers", {})))
        assert method == "POST"
        return _Response({"completed": True, "registered": True})

    client = HubClient("http://hub.invalid", "token")
    monkeypatch.setattr(client, "_open", fake_open)

    assert client.push_blob(source, {"name": "registered"}, resume=False) == {
        "blob_digest": f"sha256:{hashlib.sha256(b'registered').hexdigest()}",
        "already_present": True,
        "registered": True,
    }
    assert calls == [("POST", "/uploads", {})]


def test_push_unregistered_blob_refuses_without_chunk_or_final_put(tmp_path: Path, monkeypatch):
    source = tmp_path / "layer.squashfs"
    source.write_bytes(b"orphaned")
    calls: list[str] = []

    def fake_open(method, path, **_kwargs):
        calls.append(method)
        return _Response({"completed": True, "registered": False})

    client = HubClient("http://hub.invalid", "token")
    monkeypatch.setattr(client, "_open", fake_open)

    with pytest.raises(HubError, match="not registered"):
        client.push_blob(source, {"name": "orphaned"}, resume=False)
    assert calls == ["POST"]


def test_push_reconciles_offset_conflict_and_finalizes_with_exact_offset(tmp_path: Path, monkeypatch):
    source = tmp_path / "layer.squashfs"
    payload = b"resume from server offset"
    source.write_bytes(payload)
    calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
    patch_attempts = 0

    def fake_open(method, path, **kwargs):
        nonlocal patch_attempts
        calls.append((method, path, kwargs.get("body"), kwargs.get("headers", {})))
        if method == "POST":
            return _Response({"session_id": "session", "completed": False, "received_bytes": 0})
        if method == "PATCH":
            patch_attempts += 1
            if patch_attempts == 1:
                raise _http_conflict(4)
            expected = int(kwargs["headers"]["Upload-Offset"]) + len(kwargs["body"])
            return _Response({"received_bytes": expected})
        if method == "PUT":
            return _Response({"blob_digest": f"sha256:{hashlib.sha256(payload).hexdigest()}"})
        raise AssertionError(f"unexpected {method} {path}")

    client = HubClient("http://hub.invalid", "token")
    monkeypatch.setattr(client, "_open", fake_open)

    assert client.push_blob(source, {"name": "resumed"}, resume=False)["blob_digest"] == (
        f"sha256:{hashlib.sha256(payload).hexdigest()}"
    )
    patch_calls = [call for call in calls if call[0] == "PATCH"]
    assert [call[3]["Upload-Offset"] for call in patch_calls] == ["0", "4"]
    assert patch_calls[1][2] == payload[4:]
    final_call = calls[-1]
    assert final_call[0] == "PUT"
    assert final_call[3]["Upload-Offset"] == str(len(payload))


def test_pull_blob_resumes_only_from_exact_content_range(tmp_path: Path, monkeypatch):
    payload = b"range-resume-contract"
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    destination = tmp_path / "blob"
    part = tmp_path / "blob.part"
    sidecar = tmp_path / "blob.part.json"
    part.write_bytes(payload[:5])
    sidecar.write_text(json.dumps({"digest": digest}), encoding="utf-8")

    class RangeResponse:
        status = 206

        def __init__(self):
            self.headers = Message()
            self.headers["Content-Range"] = f"bytes 5-{len(payload) - 1}/{len(payload)}"

        def read(self, _size: int = -1) -> bytes:
            if hasattr(self, "_read"):
                return b""
            self._read = True
            return payload[5:]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    requests: list[dict[str, str]] = []
    client = HubClient("http://hub.invalid", "token")
    monkeypatch.setattr(
        client,
        "_open",
        lambda method, path, **kwargs: requests.append(kwargs["headers"]) or RangeResponse(),
    )

    assert client.pull_blob(digest, destination) == destination
    assert destination.read_bytes() == payload
    assert requests == [{"Range": "bytes=5-"}]
    assert not part.exists()
    assert not sidecar.exists()


def test_pull_blob_drops_partial_for_malformed_content_range(tmp_path: Path, monkeypatch):
    payload = b"bad-content-range"
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    destination = tmp_path / "blob"
    part = tmp_path / "blob.part"
    sidecar = tmp_path / "blob.part.json"
    part.write_bytes(payload[:3])
    sidecar.write_text(json.dumps({"digest": digest}), encoding="utf-8")

    class MalformedRangeResponse:
        status = 206

        def __init__(self):
            self.headers = Message()
            self.headers["Content-Range"] = "bytes bad"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    client = HubClient("http://hub.invalid", "token")
    monkeypatch.setattr(client, "_open", lambda *_args, **_kwargs: MalformedRangeResponse())

    with pytest.raises(HubError, match="Content-Range"):
        client.pull_blob(digest, destination)
    assert not destination.exists()
    assert not part.exists()
    assert not sidecar.exists()
