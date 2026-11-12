"""Focused unit tests for HubClient using stdlib HTTP server fakes."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from palimpsest_local.errors import HubError
from palimpsest_local.hub import MEDIA_TYPE_LAYER_SQUASHFS, HubClient


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


@pytest.mark.parametrize(
    "metadata",
    [
        {"kind": "buildkit-cache"},
        {"media_type": "application/vnd.afterglow.palimpsest.buildkit.cache.v1.tar"},
    ],
)
def test_generic_hub_cache_upload_is_rejected_before_transport(tmp_path: Path, metadata: dict, monkeypatch):
    client = HubClient("http://hub.invalid", "original-project-token")
    source = tmp_path / "cache.tar"
    source.write_bytes(b"cache bytes")
    monkeypatch.setattr(
        client, "_json_request", lambda *_args, **_kwargs: pytest.fail("legacy cache transport accessed")
    )
    with pytest.raises(HubError):
        client.push_blob(source, metadata, resume=False)
    assert source.read_bytes() == b"cache bytes"


@pytest.mark.parametrize(
    ("method", "verb", "suffix"),
    [
        ("resume_instance", "POST", "/resume"),
        ("renew_instance_lifecycle", "POST", "/lifecycle/renew"),
        ("instance_connection", "GET", "/connection"),
    ],
)
def test_instance_lifecycle_methods_send_exact_verb_and_quoted_path(monkeypatch, method, verb, suffix):
    client = HubClient("https://hub.example", "original-token")
    calls = []

    def opened(actual_verb, path, **kwargs):
        calls.append((actual_verb, path, kwargs))
        return _Response({"instance_id": "instance/id"})

    monkeypatch.setattr(client, "_open", opened)
    assert getattr(client, method)("instance/id") == {"instance_id": "instance/id"}
    assert len(calls) == 1
    assert calls[0][:2] == (verb, "/instances/instance%2Fid" + suffix)
    assert calls[0][2]["body"] is None


@pytest.mark.parametrize("method", ["resume_instance", "renew_instance_lifecycle", "instance_connection"])
def test_instance_lifecycle_methods_never_retry_uncertain_transport(monkeypatch, method):
    client = HubClient("https://hub.example", "original-token")
    calls = []

    def failed(*args, **kwargs):
        calls.append(args)
        raise HubError("uncertain provider response")

    monkeypatch.setattr(client, "_open", failed)
    with pytest.raises(HubError, match="uncertain provider response"):
        getattr(client, method)("instance/id")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["server-error", "disconnect", "rejected"])
def test_instance_advance_posts_exact_original_binding_and_get_remains_pure(failure):
    import http.client
    import socket

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, body, self.headers["X-Project-Id"]))
            if failure == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(403 if failure == "rejected" else 500)
            self.end_headers()

        def do_GET(self):
            calls.append(("GET", self.path, None, self.headers["X-Project-Id"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id":"instance/id","provider_phase":"volume_ready"}')

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HubClient(
            "http://127.0.0.1:" + str(server.server_address[1]), "original-token", project_id="original-project"
        )
        with pytest.raises((HubError, http.client.RemoteDisconnected)) as caught:
            client.advance_instance(
                "instance/id", operation="create-server", request_id="original-request", boot_volume_id="exact-boot"
            )
        if failure != "disconnect":
            assert caught.value.http_status == (403 if failure == "rejected" else 500)
        assert client.get_instance("instance/id")["provider_phase"] == "volume_ready"
        assert calls == [
            (
                "POST",
                "/v1/instances/instance%2Fid/advance",
                {"operation": "create-server", "request_id": "original-request", "boot_volume_id": "exact-boot"},
                "original-project",
            ),
            ("GET", "/v1/instances/instance%2Fid", None, "original-project"),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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


def test_push_registered_blob_rejects_incompatible_runtime_descriptor(tmp_path: Path, monkeypatch):
    source = tmp_path / "runtime.squashfs"
    source.write_bytes(b"same-bytes")
    digest = f"sha256:{hashlib.sha256(b'same-bytes').hexdigest()}"

    def fake_open(method, path, **_kwargs):
        assert (method, path) == ("POST", "/uploads")
        return _Response(
            {
                "completed": True,
                "registered": True,
                "registration": {
                    "blob_digest": digest,
                    "name": "generic-layer",
                    "kind": "squashfs",
                    "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                    "chain_id": None,
                    "base_image_digest": None,
                    "arch": "x86_64",
                    "is_published": False,
                },
            }
        )

    client = HubClient("http://hub.invalid", "token")
    monkeypatch.setattr(client, "_open", fake_open)
    with pytest.raises(HubError, match="incompatible descriptor fields"):
        client.push_blob(
            source,
            {
                "name": "runtime-pack",
                "kind": "squashfs",
                "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                "chain_id": "sha256:" + "c" * 64,
                "base_image_digest": "sha256:" + "d" * 64,
                "arch": "x86_64",
            },
            resume=False,
        )


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


def test_instance_send_guard_runs_after_serialization_and_request_construction(monkeypatch):
    from types import SimpleNamespace

    from palimpsest_local import hub

    client = HubClient("https://hub.example", "token")
    events = []
    dumps = hub.json.dumps
    request = hub.urllib.request.Request
    response = _Response({"id": "original-instance"})

    def serialize(payload):
        events.append("serialize")
        return dumps(payload)

    def construct(*args, **kwargs):
        events.append("request")
        return request(*args, **kwargs)

    def guard():
        events.append("guard")

    def send(prepared, **kwargs):
        events.append("send")
        assert prepared.full_url == "https://hub.example/v1/instances"
        assert prepared.method == "POST"
        assert prepared.data == b'{"request_id": "original-request"}'
        return response

    monkeypatch.setattr(hub.json, "dumps", serialize)
    monkeypatch.setattr(hub.urllib.request, "Request", construct)
    client._opener = SimpleNamespace(open=send)
    assert client.create_instance({"request_id": "original-request"}, before_send=guard) == {"id": "original-instance"}
    assert events == ["serialize", "request", "guard", "send"]


def test_instance_send_guard_refusal_never_opens_transport():
    from types import SimpleNamespace

    from palimpsest_local.errors import ArtifactValidationError

    client = HubClient("https://hub.example", "token")
    client._opener = SimpleNamespace(open=lambda *a, **kw: pytest.fail("refused request reached transport"))
    refusal = ArtifactValidationError("approval expired before allocation")

    def guard():
        raise refusal

    with pytest.raises(ArtifactValidationError) as caught:
        client.create_instance({"request_id": "original-request"}, before_send=guard)
    assert caught.value is refusal


def test_instance_send_guard_does_not_wrap_transport_failure():
    from types import SimpleNamespace
    from urllib.error import URLError

    client = HubClient("https://hub.example", "token")
    events = []

    def send(*args, **kwargs):
        events.append("send")
        raise URLError("uncertain provider response")

    client._opener = SimpleNamespace(open=send)
    with pytest.raises(HubError, match="uncertain provider response"):
        client.create_instance({"request_id": "guarded"}, before_send=lambda: events.append("guard"))
    assert events == ["guard", "send"]


def image_manifest_for_sdk(payloads):
    from palimpsest_local.image_manifest import manifest_from_result

    return manifest_from_result(
        {
            "layers": [
                {"digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}
                for payload in payloads
            ],
            "runtime": {
                "entrypoint": [],
                "cmd": ["/bin/true"],
                "env": ["TOKEN=private"],
                "working_dir": "/",
                "user": "0",
            },
            "source": {},
        },
        os_family="unknown",
        os_version="unknown",
        source_kind="local",
        metadata={"label": "actual local build"},
    )


def test_sdk_push_image_partial_payloads_still_registers_every_logical_manifest(tmp_path, monkeypatch):
    client = HubClient("https://hub.invalid", "original-token", project_id="alpha")
    base, child = b"hsqsbase", b"hsqsnewchild"
    child_path = tmp_path / "child.sqfs"
    child_path.write_bytes(child)
    manifest = image_manifest_for_sdk((base, child))
    calls = []

    def opened(method, path, **kwargs):
        calls.append((method, path, kwargs["body"].read()))
        return io.BytesIO(b"{}")

    def json_request(method, path, payload=None, **kwargs):
        calls.append((method, path, payload))
        return {"id": "hub-image-id", "leaf_id": payload["manifest"]["layers"][-1]["id"]}

    monkeypatch.setattr(client, "_open", opened)
    monkeypatch.setattr(client, "_json_request", json_request)
    value = client.push_image(
        manifest, {manifest["layers"][1]["digest"]: child_path}, name="local-child", request_id="request"
    )
    assert calls[0] == ("PUT", "/published-images/blobs/" + manifest["layers"][1]["digest"], child)
    assert calls[1][0:2] == ("POST", "/published-images")
    assert calls[1][2]["manifest"] == manifest
    assert calls[1][2]["visibility"] == "private"
    assert value["id"] == "hub-image-id"
    calls.clear()
    client.push_image(manifest, {}, name="local-child", request_id="request")
    assert len(calls) == 1 and calls[0][0:2] == ("POST", "/published-images")
    calls.clear()
    client.push_image(manifest, {}, name="shared-child", request_id="shared", visibility="internal")
    assert calls[0][2]["visibility"] == "internal"


def test_logical_manifest_parent_identity_and_public_projection():
    from palimpsest_local.image_manifest import (
        manifest_digest,
        native_image_projection,
        normalize_manifest,
        public_manifest,
    )

    first = image_manifest_for_sdk((b"hsqsbase", b"hsqscommon"))
    other = image_manifest_for_sdk((b"hsqsother", b"hsqscommon"))
    assert first["layers"][-1]["digest"] == other["layers"][-1]["digest"]
    assert first["layers"][-1]["id"] != other["layers"][-1]["id"]
    forged = {**first, "layers": [{**first["layers"][0], "parent_id": other["layers"][0]["id"]}]}
    with pytest.raises(ValueError):
        normalize_manifest(forged)
    first["source"]["metadata"] = {"config": {"Env": ["TOKEN=private"]}}
    first["layers"][0]["source"] = {
        "oci_layer": {
            "compressed": {
                "digest": "sha256:" + "a" * 64,
                "size": 71,
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            },
            "diff_id": "sha256:" + "b" * 64,
        },
        "conversion": {
            "receipt": {
                "key_digest": "sha256:" + "c" * 64,
                "image_digest": first["layers"][0]["digest"],
                "secret": "private",
            }
        },
    }
    public = public_manifest(first)
    assert "private" not in json.dumps(public)
    assert public["metadata"] == first["metadata"]
    assert public["layers"][0]["source"]["compressed_size"] == 71
    assert public["layers"][0]["source"]["key_digest"] == "sha256:" + "c" * 64
    assert public["layers"][0]["id"] == first["layers"][0]["id"]
    projection = native_image_projection(first)
    assert projection["source"]["manifest_digest"] == manifest_digest(first)
    assert projection["source"]["manifest"]["runtime"]["env"] == ["TOKEN=private"]
    assert projection["layers"] == [
        {"ordinal": index, "digest": layer["digest"], "size": layer["size_bytes"]}
        for index, layer in enumerate(first["layers"])
    ]


def test_sdk_extension_transfers_only_new_payload_and_never_reconstructs_private_base(tmp_path, monkeypatch):
    client = HubClient("https://hub.invalid", "original-token", project_id="alpha")
    payload = b"hsqs-new-sdk-extension"
    child = tmp_path / "new.sqfs"
    child.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    calls = []

    def opened(method, path, **kwargs):
        calls.append((method, path, kwargs["body"].read()))
        return io.BytesIO(b"{}")

    def json_request(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"id": "server-extended-image"}

    monkeypatch.setattr(client, "_open", opened)
    monkeypatch.setattr(client, "_json_request", json_request)
    value = client.push_image_extension(
        "base-hub-id",
        [
            {"digest": digest, "size_bytes": len(payload), "id": "discard-id", "parent_id": "discard-parent"},
        ],
        {digest: child},
        name="child",
        request_id="extension-id",
        visibility="internal",
        metadata={"label": "child"},
    )
    assert value == {"id": "server-extended-image"}
    assert calls[0] == ("PUT", "/published-images/blobs/" + digest, payload)
    assert calls[1][0:2] == ("POST", "/published-images/extensions")
    assert calls[1][2] == {
        "base_image_id": "base-hub-id",
        "layers": [{"digest": digest, "size_bytes": len(payload)}],
        "name": "child",
        "request_id": "extension-id",
        "visibility": "internal",
        "metadata": {"label": "child"},
    }
    assert len(calls) == 2
    calls.clear()
    client.get_layer_node("node", project_id="origin", image_id="base-hub-id")
    client.get_layer_children("node", project_id="origin", image_id="base-hub-id")
    client.get_layer_ancestors("node", project_id="origin", image_id="base-hub-id")
    assert all(
        call[3]["query"]["project_id"] == "origin" and call[3]["query"]["image_id"] == "base-hub-id" for call in calls
    )


def test_build_git_posts_the_pinned_request_to_the_git_builds_contract(monkeypatch):
    client = HubClient("http://hub.invalid", "token")
    sent = []
    monkeypatch.setattr(
        client,
        "_json_request",
        lambda method, path, body=None, **_: sent.append((method, path, body)) or {"id": "job-1"},
    )
    request = {"repo_url": "https://github.com/o/r.git", "revision": "a" * 40, "name": "git-app"}
    assert client.build_git(request) == {"id": "job-1"}
    assert sent == [("POST", "/git-builds", request)]
