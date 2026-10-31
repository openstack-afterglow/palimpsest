from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time

import pytest

from palimpsest_local import benchmark_tensorflow_context as context
from palimpsest_local.errors import ArtifactValidationError, HubError
from palimpsest_local.hub import HubClient


@pytest.fixture
def download_model(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    payloads = {"tensorflow": b"fixed tensorflow wheel", "grpcio": b"fixed grpc wheel"}
    wheels = [
        {
            "name": name,
            "version": "2.21.0" if name == "tensorflow" else "1.84.0",
            "filename": name + "-2.21.0-py3-none-any.whl",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://files.pythonhosted.org/" + name + "-2.21.0-py3-none-any.whl",
        }
        for name, payload in payloads.items()
    ]
    model = SimpleNamespace(
        mode="normal", calls=[], wheels=wheels, stopped=threading.Event(), started=threading.Event()
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            model.calls.append(self.path)
            model.started.set()
            wheel = next(item for item in wheels if self.path.endswith(item["filename"]))
            payload = payloads[wheel["name"]]
            if model.mode == "redirected":
                self.send_response(302)
                self.send_header("Location", "https://evil.example/wheel")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if model.mode == "headers":
                model.stopped.wait(3)
                return
            content = (
                (b"x" * len(payload))
                if model.mode == "corrupt"
                else payload[:-1]
                if model.mode == "truncated"
                else payload
            )
            self.send_response(200)
            self.send_header(
                "Content-Length",
                str(
                    context.MAX_WHEEL_BYTES + 1
                    if model.mode == "oversized"
                    else 1000000
                    if model.mode == "trickle"
                    else len(payload)
                ),
            )
            self.end_headers()
            try:
                if model.mode == "trickle":
                    while not model.stopped.wait(0.025):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                else:
                    self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    original_command = context._curl_command
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:

        def local_command(url, remaining, maximum):
            command = original_command(url, remaining, maximum)
            command[command.index("--proto") + 1] = "=http"
            command[-1] = "http://127.0.0.1:" + str(server.server_port) + "/" + url.rsplit("/", 1)[-1]
            return command

        monkeypatch.setattr(context, "_curl_command", local_command)
        monkeypatch.setattr(context, "fixed_wheels", lambda: ("historical hash-pinned lock", wheels))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield model
        finally:
            model.stopped.set()
            server.shutdown()
            thread.join()


def test_offline_context_contains_only_hash_verified_wheels_and_offline_paths(tmp_path, download_model):
    calls, wheels = download_model.calls, download_model.wheels
    events = []
    archive, receipt = context.acquire_context(
        tmp_path,
        deadline=time.monotonic() + 60,
        check=lambda *_: None,
        event=lambda kind, row: events.append((kind, row)),
    )
    assert len(calls) == 2 and calls == ["/" + wheel["filename"] for wheel in wheels]
    assert receipt["context_digest"] == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    assert receipt["worker_run_network"] == "none" and receipt["guest_acquisition"] is False
    assert len(receipt["wheels"]) == 2
    with tarfile.open(archive) as stream:
        assert set(stream.getnames()) == {"tensorflow.lock", *("wheels/" + item["filename"] for item in wheels)}
        install_lock = stream.extractfile("tensorflow.lock").read().decode()
        assert "https://" not in install_lock
        assert "/opt/palimpsest-wheels/tensorflow-2.21.0-py3-none-any.whl --hash=sha256:" in install_lock
        assert all(member.uid == member.gid == member.mtime == 0 for member in stream.getmembers())
    assert [kind for kind, _ in events].count("dependency-download-verified") == 2


@pytest.mark.parametrize("failure", ["corrupt", "truncated", "oversized", "redirected"])
def test_fixed_context_rejects_hash_truncation_bounds_and_redirects(tmp_path, download_model, failure):
    download_model.mode = failure
    with pytest.raises(ArtifactValidationError, match="hash|size|bounds|transfer"):
        context.acquire_context(tmp_path, deadline=time.monotonic() + 60, check=lambda *_: None, event=lambda *_: None)
    assert not (tmp_path / "context.tar").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://pypi.org/file",
        "https://evil.example/file",
        "https://pypi.org@evil.example/file",
        "https://pypi.org:444/file",
        "https://pypi.org/file?credential=x",
        "https://pypi.org/file#x",
    ],
)
def test_fixed_context_redirect_allowlist_rejects_untrusted_urls(url):
    with pytest.raises(ArtifactValidationError, match="allowlisted"):
        context._url(url)


def test_fixed_context_checks_authority_before_download(tmp_path, download_model):

    def expired(*_):
        raise ArtifactValidationError("approval expired")

    with pytest.raises(ArtifactValidationError, match="expired"):
        context.acquire_context(tmp_path, deadline=time.monotonic() + 60, check=expired, event=lambda *_: None)
    assert download_model.calls == []


@pytest.mark.parametrize("mode", ["trickle", "headers"])
def test_real_transfer_absolute_deadline_kills_and_reaps_blocked_curl(tmp_path, monkeypatch, download_model, mode):
    download_model.mode = mode
    processes = []
    original_popen = context.subprocess.Popen

    def track(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(context.subprocess, "Popen", track)
    started = time.monotonic()
    with pytest.raises(ArtifactValidationError, match="deadline|transfer"):
        context.acquire_context(tmp_path, deadline=started + 0.4, check=lambda *_: None, event=lambda *_: None)
    elapsed = time.monotonic() - started
    assert download_model.started.is_set()
    assert elapsed < 2
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not (tmp_path / "context.tar").exists()


def test_context_upload_streams_same_descriptor_and_keeps_default_limit(tmp_path, monkeypatch):
    source = tmp_path / "context.tar"
    source.write_bytes(b"a" * (16 * 1024**2 + 1))
    client = HubClient("https://hub.example", "private-test-token")
    calls = []

    def upload(method, path, *, body, headers, **kwargs):
        calls.append((method, path, headers))
        assert not isinstance(body, bytes)
        digest = hashlib.sha256()
        total = 0
        while chunk := body.read(65536):
            assert len(chunk) <= 65536
            digest.update(chunk)
            total += len(chunk)
        assert headers["Content-Length"] == str(total)
        assert headers["X-Content-SHA256"] == "sha256:" + digest.hexdigest()
        return io.BytesIO(json.dumps({"digest": headers["X-Content-SHA256"], "size_bytes": total}).encode())

    monkeypatch.setattr(client, "_open", upload)
    with pytest.raises(HubError, match="16777216"):
        client.upload_build_context(source)
    assert calls == []
    receipt = client.upload_build_context(source, max_bytes=1024**3)
    assert receipt["size_bytes"] == 16 * 1024**2 + 1 and len(calls) == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "overlimit"])
def test_context_upload_rejects_unsafe_or_unapproved_source(tmp_path, kind):
    source = tmp_path / "context.tar"
    source.write_bytes(b"context")
    if kind == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(source)
        source = alias
    elif kind == "hardlink":
        os.link(source, tmp_path / "alias")
    elif kind == "writable":
        source.chmod(0o666)
    client = HubClient("https://hub.example", "private-test-token")
    with pytest.raises((HubError, OSError)):
        client.upload_build_context(source, max_bytes=1 if kind == "overlimit" else 16 * 1024**2)


def test_context_upload_detects_same_inode_rewrite_before_any_success(tmp_path, monkeypatch):
    source = tmp_path / "context.tar"
    source.write_bytes(b"original")
    client = HubClient("https://hub.example", "private-test-token")

    def upload(*args, body, **kwargs):
        source.write_bytes(b"modified")
        body.read(8)
        pytest.fail("changed context must not be sent")

    monkeypatch.setattr(client, "_open", upload)
    with pytest.raises(HubError, match="changed"):
        client.upload_build_context(source)


def test_build_context_upload_uses_content_length_on_real_http_wire(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payload = b"fixed context bytes" * 1000
    source = tmp_path / "context.tar"
    source.write_bytes(payload)
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received = self.rfile.read(length)
            digest = "sha256:" + hashlib.sha256(received).hexdigest()
            observed.append(
                {
                    "path": self.path,
                    "length": length,
                    "payload": received,
                    "transfer_encoding": self.headers.get("Transfer-Encoding"),
                    "digest": self.headers["X-Content-SHA256"],
                }
            )
            response = json.dumps({"digest": digest, "size_bytes": length}).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            receipt = HubClient("http://127.0.0.1:" + str(server.server_port), "test-only-token").upload_build_context(
                source
            )
        finally:
            server.shutdown()
            thread.join()
    assert observed == [
        {
            "path": "/v1/build-contexts",
            "length": len(payload),
            "payload": payload,
            "transfer_encoding": None,
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    ]
    assert receipt["size_bytes"] == len(payload)


def test_context_upload_guard_rechecks_expiry_after_descriptor_hashing(tmp_path, monkeypatch):
    source = tmp_path / "context.tar"
    source.write_bytes(b"context whose local hash crosses approval expiry")
    client = HubClient("https://hub.example", "private-test-token")
    clock = [1000]
    sha256 = hashlib.sha256

    class ExpiringDigest:
        def __init__(self):
            self.digest = sha256()

        def update(self, chunk):
            self.digest.update(chunk)
            clock[0] = 1101

        def hexdigest(self):
            return self.digest.hexdigest()

    def before_send():
        if clock[0] >= 1100:
            raise ArtifactValidationError("approval expired after context hashing")

    monkeypatch.setattr(hashlib, "sha256", ExpiringDigest)
    monkeypatch.setattr(client, "_open", lambda *args, **kwargs: pytest.fail("expired hashed context cannot POST"))
    with pytest.raises(ArtifactValidationError, match="expired after context hashing"):
        client.upload_build_context(source, before_send=before_send)
