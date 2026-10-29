"""Behavioral security/recovery boundaries; no source-text assertions or host mounts."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_hub import build_worker as worker

BUILD_ID = "11111111-1111-4111-8111-111111111111"
LEASE = "22222222-2222-4222-8222-222222222222"
BASE = b"pinned cloud image"
OUTPUT = b"hsqsprivate squashfs output"


def digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def claim():
    recipe = "FROM pinned\nRUN true\n"
    return {
        "id": BUILD_ID,
        "lease": LEASE,
        "deadline": "2030-01-01T00:00:00+00:00",
        "timeout_seconds": 60,
        "name": "private-build",
        "recipe": recipe,
        "recipe_digest": digest(recipe.encode()),
        "base": {"digest": digest(BASE), "arch": "x86_64", "disk_format": "raw", "size_bytes": len(BASE)},
        "layers": [],
        "max_output_bytes": 1024,
    }


def terminal(status="error"):
    return {"id": BUILD_ID, "status": status, "output_digest": digest(OUTPUT), "output_size_bytes": len(OUTPUT)}


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.stream = io.BytesIO(json.dumps(body).encode() if isinstance(body, dict) else body)
        self.headers = headers or {}
        self.read_sizes = []
        self.closed = False

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)

    def read1(self, size):
        return self.read(size)

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, responses, events):
        self.responses = list(responses)
        self.calls = []
        self.events = events
        self.image_claims = []

    @contextmanager
    def request(self, method, path, **kwargs):
        # This suite exercises recipe jobs against a matching API with an empty
        # image queue. Keep its recipe protocol trace distinct from image polls.
        if path == "/v1/image-worker/claim":
            self.image_claims.append((method, path))
            yield Response(204)
            return
        body = kwargs.get("body")
        if hasattr(body, "read"):
            body = body.read()
        call = (method, path, kwargs.get("lease"), body, kwargs.get("headers", {}))
        self.calls.append(call)
        self.events.append((method, path))
        assert self.responses, f"unexpected request: {method} {path}"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        try:
            yield response
        finally:
            response.close()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    settings = SimpleNamespace(palimpsest_hub_max_blob_bytes=1024, palimpsest_hub_build_timeout_seconds=60)
    journal = worker.Journal(root)
    events = []
    manifests = []

    def executor(args, timeout, stop=None, log=None):
        assert 0 < timeout <= 60
        assert args[0] != "preflight"
        if args[0] == "cleanup":
            assert len(args) == 3 and args[2] == str(os.getpid())
            events.append(("cleanup", args[1]))
            return b""
        manifest_path = Path(args[0])
        manifest = json.loads(manifest_path.read_text())
        manifests.append(manifest)
        assert manifest["max_output_bytes"] == 1024
        assert Path(manifest["base"]["path"]).read_bytes() == BASE
        assert Path(manifest["base"]["path"]).stat().st_mode & 0o777 == 0o600
        events.append(("execute", str(manifest_path)))
        result = manifest_path.parent / "result.sqsh"
        result.write_bytes(OUTPUT)
        result.chmod(0o600)
        return json.dumps({"digest": digest(OUTPUT), "size_bytes": len(OUTPUT)}).encode()

    monkeypatch.setattr(worker, "run_executor", executor)

    def make(responses):
        transport = Transport(responses, events)
        instance = worker.BuildWorker(settings, transport, journal, threading.Event())
        return instance, transport

    yield SimpleNamespace(make=make, journal=journal, events=events, manifests=manifests, executor=executor)
    journal.close()


def input_response(payload=BASE, declared=None):
    return Response(200, payload, {"Content-Length": str(len(payload) if declared is None else declared)})


def test_key_only_reads_owner_regular_absolute_file(tmp_path, monkeypatch):
    key = tmp_path / "worker.key"
    key.write_text("k" * 32 + "\n")
    key.chmod(0o600)
    monkeypatch.setenv("PALIMPSEST_HUB_BUILD_WORKER_KEY", "environment-secret-never-used")
    assert worker.read_worker_key(str(key)) == "k" * 32
    key.chmod(0o400)
    assert worker.read_worker_key(str(key)) == "k" * 32
    key.unlink()
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(key))
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key("worker.key")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_key_rejects_nonprivate_permissions(tmp_path, mode):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(mode)
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(key))


def test_key_rejects_symlink_hardlink_directory_and_fifo(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(key)
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(symlink))
    hardlink = tmp_path / "hardlink"
    os.link(key, hardlink)
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(key))
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(tmp_path))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(fifo))


def test_key_rejects_other_owner(tmp_path, monkeypatch):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    monkeypatch.setattr(worker.os, "geteuid", lambda: key.stat().st_uid + 1)
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(key))


@pytest.mark.parametrize("payload", [b"short", b"k" * 4097, b"k" * 32 + b"\x00", b"k" * 31 + b" ", b"\xff" * 32])
def test_key_rejects_invalid_or_unbounded_value(tmp_path, payload):
    key = tmp_path / "key"
    key.write_bytes(payload)
    key.chmod(0o600)
    with pytest.raises(worker.WorkerHalted):
        worker.read_worker_key(str(key))


@pytest.mark.parametrize(
    "origin", ["http://hub", "https://u:p@hub", "https://hub/path", "https://hub/?query", "https://hub/#x"]
)
def test_transport_rejects_nonorigin_or_insecure_url(origin):
    with pytest.raises(worker.WorkerHalted):
        worker.HttpsTransport(origin, "k" * 32)


@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308])
def test_transport_direct_tls_no_proxy_redirect_or_retry(monkeypatch, status):
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv("https_proxy", "http://untrusted.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://untrusted.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", ssl.get_default_verify_paths().cafile or "")
    response = Response(status, b"", {"Location": "https://untrusted.invalid/steal"})
    connections = []
    calls = []

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            connections.append(self)
            assert (host, port, timeout) == ("hub.example", 443, 10)
            assert context.check_hostname
            assert context.verify_mode == ssl.CERT_REQUIRED
            self.closed = False

        def connect(self):
            self.sock = SimpleNamespace(shutdown=lambda _: None)

        def request(self, method, path, *, body, headers):
            calls.append((method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(worker.http.client, "HTTPSConnection", Connection)
    transport = worker.HttpsTransport("https://hub.example", "k" * 32)
    if status == 200:
        with transport.request("GET", "/v1/build-worker/builds/" + BUILD_ID, lease=LEASE) as received:
            assert received is response
    else:
        with (
            pytest.raises(worker.WorkerHalted, match="redirect"),
            transport.request("GET", "/v1/build-worker/builds/" + BUILD_ID, lease=LEASE),
        ):
            pytest.fail("redirect accepted")
    assert len(connections) == len(calls) == 1
    assert connections[0].closed and response.closed
    assert calls[0][2]["X-Palimpsest-Build-Worker-Key"] == "k" * 32
    assert calls[0][2]["X-Palimpsest-Build-Lease"] == LEASE


def test_transport_lost_response_is_not_retried(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            self.sock = SimpleNamespace(shutdown=lambda _: None)

        def request(self, *args, **kwargs):
            calls.append(args)

        def getresponse(self):
            raise OSError("lost HTTP response")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(worker.http.client, "HTTPSConnection", Connection)
    transport = worker.HttpsTransport("https://hub.example", "k" * 32)
    with pytest.raises(OSError), transport.request("POST", "/v1/build-worker/claim"):
        pytest.fail("unexpected response")
    assert len(calls) == 2 and calls[-1] == "closed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(deadline="2000-01-01T00:00:00+00:00"),
        lambda c: c.update(deadline="2030-01-01T00:00:00"),
        lambda c: c.update(timeout_seconds=True),
        lambda c: c.update(timeout_seconds=3601),
        lambda c: c.update(recipe_digest=digest(b"changed")),
        lambda c: c["base"].update(digest="sha256:../../escape"),
        lambda c: c["base"].update(size_bytes=True),
        lambda c: c["base"].update(size_bytes=1025),
        lambda c: c["base"].update(arch="aarch64"),
        lambda c: c["base"].update(disk_format="vmdk"),
        lambda c: c.update(max_output_bytes=0),
    ],
)
def test_invalid_claim_descriptors_fail_known_lease_without_downloading_or_executing(harness, mutate):
    descriptor = claim()
    mutate(descriptor)
    instance, transport = harness.make(
        [
            Response(202, descriptor),
            Response(200, terminal()),
            Response(200, terminal()),
        ]
    )
    assert instance.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "POST", "GET"]
    assert transport.calls[1][1].endswith(f"/{BUILD_ID}/fail")
    assert json.loads(transport.calls[1][3]) == {"error_code": "build_failed"}
    assert harness.journal.load() is None
    assert not harness.manifests


def layer_descriptor(payload, parent=None):
    return {
        "digest": digest(payload),
        "size_bytes": len(payload),
        "media_type": worker.LAYER_MEDIA_TYPE,
        "parent_digest": parent,
        "base_image_digest": digest(BASE),
    }


@pytest.mark.parametrize("boundary", ["parent", "base", "duplicate", "total", "media"])
def test_claim_graph_and_total_boundaries(harness, boundary):
    descriptor = claim()
    first = layer_descriptor(b"layer-one")
    second = layer_descriptor(b"layer-two", first["digest"])
    descriptor["layers"] = [first, second]
    if boundary == "parent":
        second["parent_digest"] = None
    elif boundary == "base":
        first["base_image_digest"] = digest(b"foreign-base")
    elif boundary == "duplicate":
        second["digest"] = first["digest"]
    elif boundary == "total":
        first["size_bytes"] = 1024
    else:
        first["media_type"] = "application/octet-stream"
    instance, transport = harness.make(
        [
            Response(202, descriptor),
            Response(200, terminal()),
            Response(200, terminal()),
        ]
    )
    assert instance.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "POST", "GET"]
    assert json.loads(transport.calls[1][3]) == {"error_code": "build_failed"}
    assert harness.journal.load() is None
    assert not harness.manifests


def test_claim_strips_urls_paths_and_unknown_executor_fields():
    descriptor = claim()
    descriptor.update(url="https://evil.invalid", path="/etc/shadow", environment={"secret": "never"})
    descriptor["base"].update(path="/etc/shadow", url="https://evil.invalid")
    clean = worker.validate_claim(descriptor, 1024, 60)
    assert "url" not in clean and "path" not in clean and "environment" not in clean
    assert "path" not in clean["base"] and "url" not in clean["base"]


@pytest.mark.parametrize(
    "response",
    [
        input_response(BASE + b"excess", len(BASE)),
        input_response(BASE[:-1], len(BASE)),
        input_response(b"wrong".ljust(len(BASE), b"!")),
        input_response(BASE, len(BASE) + 1),
        Response(200, BASE),
        Response(200, BASE, {"Content-Length": str(len(BASE)), "Content-Encoding": "gzip"}),
        Response(302, BASE, {"Location": "https://evil.invalid"}),
    ],
)
def test_download_digest_size_encoding_and_status_fail_closed(harness, response):
    instance, transport = harness.make(
        [
            Response(202, claim()),
            response,
            Response(200, terminal()),
            Response(200, terminal()),
        ]
    )
    assert instance.process_one()
    assert not harness.manifests
    failure = next(call for call in transport.calls if call[1].endswith("/fail"))
    assert json.loads(failure[3]) == {"error_code": "build_failed"}
    assert harness.events[2][0] == "cleanup"
    assert harness.journal.load() is None


def test_happy_path_pins_download_upload_and_cleans_before_next_claim(harness):
    instance, transport = harness.make(
        [
            Response(202, claim()),
            input_response(),
            Response(200, terminal("complete")),
            Response(200, terminal("complete")),
            Response(204),
        ]
    )
    assert instance.process_one()
    assert not instance.process_one()
    assert len(harness.manifests) == 1
    manifest = harness.manifests[0]
    assert manifest["layers"] == []
    assert 0 < manifest["timeout_seconds"] <= 60
    download = transport.calls[1]
    assert download[:3] == ("GET", f"/v1/build-worker/builds/{BUILD_ID}/inputs/{digest(BASE)}", LEASE)
    upload = transport.calls[2]
    assert upload[:4] == ("PUT", f"/v1/build-worker/builds/{BUILD_ID}/output", LEASE, OUTPUT)
    assert upload[4]["X-Content-Digest"] == digest(OUTPUT)
    assert upload[4]["Content-Length"] == str(len(OUTPUT))
    phases = [event[0] for event in harness.events]
    assert phases == ["POST", "GET", "execute", "cleanup", "PUT", "GET", "POST"]
    assert not list(harness.journal.root.glob("job-*"))
    assert harness.journal.load() is None


@pytest.mark.parametrize("response", [OSError("lost claim"), Response(500), Response(202, b"broken json")])
def test_uncertain_claim_never_reclaims_even_after_restart(harness, response):
    instance, transport = harness.make([response])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    restarted, restarted_transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="claim outcome uncertain"):
        restarted.process_one()
    assert len(transport.calls) == 1
    assert restarted_transport.calls == []
    assert harness.journal.load() == {"phase": "claim_pending", "protocol": "recipe"}


@pytest.mark.parametrize("response", [OSError("lost output"), Response(500), Response(200, b"broken json")])
def test_uncertain_output_only_settles_status_never_reexecutes(harness, response):
    instance, transport = harness.make(
        [
            Response(202, claim()),
            input_response(),
            response,
            Response(200, {"id": BUILD_ID, "status": "building"}),
        ]
    )
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert harness.journal.load()["phase"] == "output_pending"
    assert len(harness.manifests) == 1
    restarted, restarted_transport = harness.make([Response(200, terminal("complete")), Response(204)])
    assert not restarted.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "GET", "PUT", "GET"]
    assert [call[0] for call in restarted_transport.calls] == ["GET", "POST"]
    assert len(harness.manifests) == 1
    assert harness.journal.load() is None


def test_unknown_output_repeated_restart_only_status_reads(harness):
    record = {
        "phase": "output_pending",
        "id": BUILD_ID,
        "lease": LEASE,
        "output": {"digest": digest(OUTPUT), "size_bytes": len(OUTPUT)},
    }
    harness.journal.save(record)
    harness.journal.job_dir(record).mkdir(mode=0o700)
    for _ in range(2):
        instance, transport = harness.make([Response(200, {"id": BUILD_ID, "status": "building"})])
        with pytest.raises(worker.WorkerHalted):
            instance.process_one()
        assert [call[0] for call in transport.calls] == ["GET"]
        assert harness.journal.load() == record
    assert not harness.manifests


def test_conflicting_output_status_retains_original_journal(harness):
    wrong = terminal("complete") | {"output_digest": digest(b"different output")}
    instance, transport = harness.make(
        [Response(202, claim()), input_response(), Response(200, wrong), Response(200, wrong)]
    )
    with pytest.raises(worker.WorkerHalted, match="conflicts"):
        instance.process_one()
    assert harness.journal.load()["phase"] == "output_pending"
    assert sum(call[0] == "PUT" for call in transport.calls) == 1


@pytest.mark.parametrize("phase", ["claimed", "executing"])
def test_restart_interrupts_without_recipe_reexecution(harness, phase):
    record = {"phase": phase, "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    harness.journal.job_dir(record).mkdir(mode=0o700)
    instance, transport = harness.make([Response(200, terminal()), Response(200, terminal()), Response(204)])
    assert not instance.process_one()
    assert not harness.manifests
    assert harness.events[0][0] == "cleanup"
    assert json.loads(transport.calls[0][3]) == {"error_code": "build_failed"}
    assert harness.journal.load() is None


def test_uncertain_failure_is_not_reposted_on_restart(harness):
    record = {"phase": "executing", "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    harness.journal.job_dir(record).mkdir(mode=0o700)
    instance, transport = harness.make([OSError("lost fail"), Response(200, {"id": BUILD_ID, "status": "building"})])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert harness.journal.load()["phase"] == "fail_pending"
    restarted, restarted_transport = harness.make([Response(200, terminal()), Response(204)])
    assert not restarted.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
    assert [call[0] for call in restarted_transport.calls] == ["GET", "POST"]
    assert not harness.manifests


@pytest.mark.parametrize("phase", ["claimed", "executing", "output_pending", "fail_pending", "terminal"])
def test_cleanup_failure_halts_before_any_remote_effect(harness, monkeypatch, phase):
    record = {"phase": phase, "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    directory = harness.journal.job_dir(record)
    directory.mkdir(mode=0o700)

    def failed_cleanup(*args, **kwargs):
        raise worker.BuildTimeout("cannot verify owned mount")

    monkeypatch.setattr(worker, "run_executor", failed_cleanup)
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="cleanup"):
        instance.process_one()
    assert transport.calls == []
    assert directory.exists() and harness.journal.load() == record


def test_cleanup_failure_after_execution_never_uploads_or_claims(harness, monkeypatch):
    def executor(args, *rest):
        if args[0] == "cleanup":
            raise worker.BuildFailed("owned loop ambiguous")
        return harness.executor(args, *rest)

    monkeypatch.setattr(worker, "run_executor", executor)
    instance, transport = harness.make([Response(202, claim()), input_response()])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
    assert harness.journal.load()["phase"] == "executing"


@pytest.mark.parametrize(
    "error,code",
    [
        (worker.BuildFailed("raw recipe secret"), "build_failed"),
        (worker.BuildTimeout("private filesystem path"), "build_timeout"),
    ],
)
def test_process_failure_uses_generic_codes_and_cleanup(harness, monkeypatch, error, code):
    def executor(args, *rest):
        if args[0] == "cleanup":
            return harness.executor(args, *rest)
        raise error

    monkeypatch.setattr(worker, "run_executor", executor)
    instance, transport = harness.make(
        [
            Response(202, claim()),
            input_response(),
            Response(200, terminal()),
            Response(200, terminal()),
        ]
    )
    assert instance.process_one()
    assert json.loads(transport.calls[2][3]) == {"error_code": code}
    assert harness.events[2][0] == "cleanup"
    assert harness.journal.load() is None


def test_local_flock_and_unjournaled_scratch_fail_closed(tmp_path):
    first = worker.Journal(tmp_path / "scratch")
    try:
        with pytest.raises(worker.WorkerHalted, match="already owned"):
            worker.Journal(first.root)
        (first.root / "job-unknown").mkdir(mode=0o700)
        with pytest.raises(worker.WorkerHalted, match="unjournaled"):
            first.load()
    finally:
        first.close()


def test_stop_does_not_claim(harness):
    instance, transport = harness.make([])
    instance.stop.set()
    assert not instance.process_one()
    assert transport.calls == []


@pytest.mark.parametrize(
    "pipe,size,match", [("stdout", worker.STDOUT_LIMIT + 1, "output"), ("stderr", worker.LOG_LIMIT + 1, "log")]
)
def test_executor_bounds_pipes_and_uses_clean_process_group(tmp_path, monkeypatch, pipe, size, match):
    actual_popen = subprocess.Popen
    launches = []
    children = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        child = actual_popen(
            [sys.executable, "-I", "-c", f"import os; os.write({1 if pipe == 'stdout' else 2}, b'x' * {size})"],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setenv("PALIMPSEST_HUB_BUILD_WORKER_KEY", "never-pass-to-executor")
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    log = tmp_path / "private.log"
    with pytest.raises(worker.BuildFailed, match=match):
        worker.run_executor(["test-manifest", "123"], 10, log=log)
    args, options = launches[0]
    assert args[1:4] == ["-I", "-m", "palimpsest_local.hub_builder"]
    assert options["env"] == worker.EXECUTOR_ENV
    assert "PALIMPSEST_HUB_BUILD_WORKER_KEY" not in options["env"]
    assert options["start_new_session"] and options["close_fds"]
    assert options["umask"] == 0o077
    assert children[0].poll() is not None
    assert log.stat().st_size <= worker.LOG_LIMIT


def test_executor_timeout_terminates_group(monkeypatch):
    actual_popen = subprocess.Popen
    children = []

    def launch(argv, **kwargs):
        child = actual_popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    with pytest.raises(worker.BuildTimeout):
        worker.run_executor(["test-manifest", "123"], 0)
    assert children[0].poll() is not None


def test_json_response_memory_bound():
    response = Response(200, b"x" * (worker.JSON_LIMIT + 1))
    with pytest.raises(worker.WorkerHalted, match="limit"):
        worker._json_response(response)
    assert max(response.read_sizes) <= worker.CHUNK


def test_parent_first_layers_are_downloaded_to_distinct_private_paths(harness):
    descriptor = claim()
    first, second = b"hsqsfirst", b"hsqssecond"
    descriptor["layers"] = [layer_descriptor(first), layer_descriptor(second, digest(first))]
    instance, transport = harness.make(
        [
            Response(202, descriptor),
            input_response(),
            input_response(first),
            input_response(second),
            Response(200, terminal("complete")),
            Response(200, terminal("complete")),
        ]
    )
    assert instance.process_one()
    manifest = harness.manifests[0]
    layers = manifest["layers"]
    assert [layer["digest"] for layer in layers] == [digest(first), digest(second)]
    assert [layer["parent_digest"] for layer in layers] == [None, digest(first)]
    assert len({manifest["base"]["path"], *(layer["path"] for layer in layers)}) == 3
    assert [call[1].rsplit("/", 1)[-1] for call in transport.calls[1:4]] == [
        digest(BASE),
        digest(first),
        digest(second),
    ]


@pytest.mark.parametrize("kind", ["size", "digest", "magic", "symlink"])
def test_invalid_local_output_is_not_uploaded(harness, monkeypatch, kind):
    def executor(args, *rest):
        raw = harness.executor(args, *rest)
        if args[0] == "cleanup":
            return raw
        output = Path(args[0]).parent / "result.sqsh"
        if kind == "size":
            output.write_bytes(OUTPUT[:-1])
        elif kind == "digest":
            output.write_bytes(OUTPUT[:-1] + b"!")
        elif kind == "magic":
            output.write_bytes(b"bad!" + OUTPUT[4:])
            raw = json.dumps({"digest": digest(output.read_bytes()), "size_bytes": len(OUTPUT)}).encode()
        else:
            output.unlink()
            output.symlink_to(Path(args[0]).parent / "input-0")
        return raw

    monkeypatch.setattr(worker, "run_executor", executor)
    instance, transport = harness.make(
        [
            Response(202, claim()),
            input_response(),
            Response(200, terminal()),
            Response(200, terminal()),
        ]
    )
    assert instance.process_one()
    assert not any(call[0] == "PUT" for call in transport.calls)
    assert json.loads(transport.calls[2][3]) == {"error_code": "build_failed"}
    assert harness.journal.load() is None


def test_insufficient_scratch_never_downloads(harness, monkeypatch):
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    instance, transport = harness.make([Response(202, claim()), Response(200, terminal()), Response(200, terminal())])
    assert instance.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "POST", "GET"]
    assert not harness.manifests


def test_download_deadline_and_stop_fence(harness):
    record = {"phase": "claimed", "id": BUILD_ID, "lease": LEASE}
    descriptor = claim()["base"]
    instance, transport = harness.make([input_response()])
    with pytest.raises(worker.BuildTimeout):
        instance._download(record, descriptor, harness.journal.root / "timed-out-input", 0)
    stopped, _ = harness.make([input_response()])
    stopped.stop.set()
    with pytest.raises(worker.BuildFailed, match="stopping"):
        stopped._download(record, descriptor, harness.journal.root / "stopped-input", float("inf"))
    assert len(transport.calls) == 1


def test_pinned_body_bounds_changed_file_and_transfer_time(monkeypatch):
    monkeypatch.setattr(worker.time, "monotonic", lambda: 1)
    body = worker.PinnedBody(io.BytesIO(b"data"), 4, 2)
    assert body.read() == b"data"
    assert body.read() == b""
    with pytest.raises(worker.BuildFailed, match="changed"):
        worker.PinnedBody(io.BytesIO(b"data-excess"), 4, 2).read()
    truncated = worker.PinnedBody(io.BytesIO(b"dat"), 4, 2)
    assert truncated.read() == b"dat"
    with pytest.raises(worker.BuildFailed, match="changed"):
        truncated.read()
    with pytest.raises(worker.BuildTimeout):
        worker.PinnedBody(io.BytesIO(b"data"), 4, 1).read()


def test_executor_exit124_maps_to_timeout(monkeypatch):
    actual_popen = subprocess.Popen

    def launch(argv, **kwargs):
        return actual_popen([sys.executable, "-I", "-c", "raise SystemExit(124)"], **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    with pytest.raises(worker.BuildTimeout):
        worker.run_executor(["test-manifest", "123"], 10)


def test_unverifiable_process_group_halts(monkeypatch):
    signals = []
    times = iter([0, 0, 3])
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    child = SimpleNamespace(pid=123, wait=lambda **kwargs: 0)
    with pytest.raises(worker.WorkerHalted, match="group"):
        worker._terminate(child)
    assert signals == [(123, worker.signal.SIGTERM), (123, worker.signal.SIGKILL), (123, 0)]


def test_dangling_job_symlink_never_authorizes_cleanup_or_fail(harness):
    record = {"phase": "claimed", "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    harness.journal.job_dir(record).symlink_to(harness.journal.root / "missing")
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="unsafe"):
        instance.process_one()
    assert not transport.calls
    assert harness.journal.load() == record


@pytest.mark.asyncio
async def test_cancellation_drains_thread_before_releasing_ownership():
    stop = threading.Event()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def effect():
        started.set()
        release.wait()
        finished.set()

    task = asyncio.create_task(worker._drain_blocking(stop, effect))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        assert await asyncio.to_thread(stop.wait, 5)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_output_transfer_stop_fence():
    stop = threading.Event()
    stop.set()
    body = worker.PinnedBody(io.BytesIO(OUTPUT), len(OUTPUT), float("inf"), stop)
    with pytest.raises(worker.BuildFailed, match="stopping"):
        body.read()


@pytest.mark.asyncio
async def test_preflight_is_executor_cli_only_and_precedes_claim(tmp_path, monkeypatch):
    key = tmp_path / "worker.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    root = tmp_path / "scratch"
    settings = SimpleNamespace(
        palimpsest_hub_build_worker_key_file=str(key),
        palimpsest_hub_build_work_path=str(root),
        palimpsest_hub_url="https://hub.example",
    )
    monkeypatch.setattr(worker, "get_build_worker_settings", lambda: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda: None)
    events = []
    sealed = threading.Event()
    monkeypatch.setattr(worker, "_seal_worker_memory", sealed.set)
    original_read_key = worker.read_worker_key

    def read_key(path):
        assert sealed.is_set(), "worker key was loaded before memory sealing"
        return original_read_key(path)

    monkeypatch.setattr(worker, "read_worker_key", read_key)

    def executor(args, timeout, stop):
        events.append((args, timeout))
        raise worker.BuildFailed("generic preflight failure")

    class NoRequests:
        def __init__(self, origin, secret):
            assert origin == "https://hub.example" and secret == "k" * 32

        def request(self, *args, **kwargs):
            pytest.fail("claim preceded executor preflight")

    monkeypatch.setattr(worker, "HttpsTransport", NoRequests)
    monkeypatch.setattr(worker, "run_executor", executor)
    with pytest.raises(worker.WorkerHalted, match="preflight"):
        await worker.main()
    assert events == [(["preflight", str(os.getpid()), str(root)], 30)]
    reopened = worker.Journal(root)
    try:
        assert reopened.load() is None
    finally:
        reopened.close()


def test_failed_tree_removal_retains_terminal_journal(harness, monkeypatch):
    instance, transport = harness.make(
        [
            Response(202, claim()),
            input_response(),
            Response(200, terminal("complete")),
            Response(200, terminal("complete")),
        ]
    )

    def remove_failed(path):
        raise OSError("private tree retained")

    monkeypatch.setattr(worker.shutil, "rmtree", remove_failed)
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert harness.journal.load()["phase"] == "terminal"
    assert harness.journal.job_dir(harness.journal.load()).exists()
    assert sum(call[0] == "PUT" for call in transport.calls) == 1


def test_worker_config_ceiling_is_passed_to_executor(harness):
    instance, _ = harness.make(
        [
            Response(202, claim()),
            input_response(),
            Response(200, terminal("complete")),
            Response(200, terminal("complete")),
        ]
    )
    assert instance.process_one()
    assert harness.manifests[0]["max_input_bytes"] == instance.settings.palimpsest_hub_max_blob_bytes


@pytest.mark.parametrize("phase", ["output_pending", "fail_pending", "terminal"])
def test_cleaned_mutation_intent_can_settle_without_tree(harness, phase):
    record = {"phase": phase, "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    instance, transport = harness.make([Response(200, terminal()), Response(204)])
    assert not instance.process_one()
    assert [call[0] for call in transport.calls] == ["GET", "POST"]
    assert not harness.manifests and harness.journal.load() is None


def test_missing_executing_tree_halts_without_remote_effect(harness):
    harness.journal.save({"phase": "executing", "id": BUILD_ID, "lease": LEASE})
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="tree missing"):
        instance.process_one()
    assert not transport.calls
    assert harness.journal.load()["phase"] == "executing"


def test_interrupted_claim_without_tree_failure_uncertainty_settles_on_restart(harness):
    harness.journal.save({"phase": "claimed", "id": BUILD_ID, "lease": LEASE})
    instance, transport = harness.make([OSError("lost fail"), Response(200, {"id": BUILD_ID, "status": "building"})])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert harness.journal.load()["phase"] == "fail_pending"
    restarted, restarted_transport = harness.make([Response(200, terminal()), Response(204)])
    assert not restarted.process_one()
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
    assert [call[0] for call in restarted_transport.calls] == ["GET", "POST"]
    assert harness.journal.load() is None


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_request_watchdog_interrupts_slow_http_headers_and_retained_body_socket(monkeypatch, stage):
    timers = []
    sockets = []
    requests = []

    class ManualTimer:
        def __init__(self, interval, callback):
            assert 0 < interval <= 30
            self.callback = callback
            self.ident = None
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    class SlowSocket:
        def __init__(self):
            self.shutdowns = []

        def shutdown(self, mode):
            self.shutdowns.append(mode)

    class SlowResponse(Response):
        def read1(self, size):
            timers[0].callback()
            assert sockets[0].shutdowns == [worker.socket.SHUT_RDWR]
            raise OSError("read interrupted by request deadline")

        def read(self, size):
            pytest.fail("buffer-filling read bypassed incremental deadline boundary")

    response = SlowResponse(200)

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = None

        def connect(self):
            self.sock = SlowSocket()
            sockets.append(self.sock)

        def request(self, *args, **kwargs):
            requests.append(args)

        def getresponse(self):
            if stage == "headers":
                timers[0].callback()
                assert sockets[0].shutdowns == [worker.socket.SHUT_RDWR]
                raise OSError("headers interrupted by request deadline")
            # http.client may clear connection.sock for Connection: close while
            # the response still retains the socket through its buffered file.
            self.sock = None
            return response

        def close(self):
            self.sock = None

    monkeypatch.setattr(worker.threading, "Timer", ManualTimer)
    monkeypatch.setattr(worker.http.client, "HTTPSConnection", Connection)
    transport = worker.HttpsTransport("https://hub.example", "k" * 32)
    with (
        pytest.raises(worker.BuildTimeout, match="HTTP request deadline"),
        transport.request("POST", "/v1/build-worker/claim") as received,
    ):
        worker._json_response(received)
    assert len(requests) == 1
    assert timers[0].cancelled
    assert response.closed == (stage == "body")


def test_incremental_json_reader_checks_deadline_between_trickles(monkeypatch):
    clock = iter([0, 0, 31])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))

    class TrickleResponse(Response):
        def read1(self, size):
            return b"{"

        def read(self, size):
            pytest.fail("buffer-filling read hides the overall deadline")

    with pytest.raises(worker.WorkerHalted, match="deadline"):
        worker._json_response(TrickleResponse(200))


@pytest.mark.parametrize(
    "staged",
    [
        b"",
        b'{"phase":',
        json.dumps({"phase": "output_pending", "id": "33333333-3333-4333-8333-333333333333", "lease": LEASE}).encode(),
    ],
)
def test_stale_uncommitted_journal_does_not_block_durable_job_cleanup_and_fail(harness, staged):
    record = {"phase": "executing", "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    harness.journal.job_dir(record).mkdir(mode=0o700)
    temporary = harness.journal.root / "worker.json.new"
    temporary.write_bytes(staged)
    temporary.chmod(0o600)
    instance, transport = harness.make([Response(200, terminal()), Response(200, terminal()), Response(204)])
    assert not instance.process_one()
    assert harness.events[0][0] == "cleanup"
    assert transport.calls[0][:3] == ("POST", f"/v1/build-worker/builds/{BUILD_ID}/fail", LEASE)
    assert json.loads(transport.calls[0][3]) == {"error_code": "build_failed"}
    assert not any(call[0] == "PUT" for call in transport.calls)
    assert not harness.manifests and not temporary.exists()
    assert harness.journal.load() is None


@pytest.mark.parametrize("unsafe", ["permissions", "symlink", "hardlink", "oversize"])
def test_unsafe_staging_file_retains_durable_journal_and_halts(harness, unsafe):
    record = {"phase": "executing", "id": BUILD_ID, "lease": LEASE}
    harness.journal.save(record)
    directory = harness.journal.job_dir(record)
    directory.mkdir(mode=0o700)
    temporary = harness.journal.root / "worker.json.new"
    external = harness.journal.root.parent / "external-staging"
    external.write_bytes(b"private")
    external.chmod(0o600)
    if unsafe == "symlink":
        temporary.symlink_to(external)
    elif unsafe == "hardlink":
        os.link(external, temporary)
    else:
        temporary.write_bytes(b"x" * (worker.JSON_LIMIT + 1) if unsafe == "oversize" else b"partial")
        temporary.chmod(0o644 if unsafe == "permissions" else 0o600)
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert not transport.calls and directory.exists()
    assert json.loads(harness.journal.path.read_bytes()) == record
    assert temporary.exists() or temporary.is_symlink()


def test_staging_without_durable_journal_is_not_adopted_or_discarded(harness):
    temporary = harness.journal.root / "worker.json.new"
    temporary.write_bytes(json.dumps({"phase": "claimed", "id": BUILD_ID, "lease": LEASE}).encode())
    temporary.chmod(0o600)
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="unjournaled"):
        instance.process_one()
    assert not transport.calls and temporary.exists()


def test_invalid_durable_journal_does_not_authorize_staging_discard(harness):
    harness.journal.save({"phase": "executing", "id": "../unknown", "lease": LEASE})
    temporary = harness.journal.root / "worker.json.new"
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="invalid worker journal"):
        instance.process_one()
    assert not transport.calls and temporary.read_bytes() == b"partial"


def test_staging_never_supplies_identity_for_an_uncertain_claim(harness):
    harness.journal.save({"phase": "claim_pending"})
    temporary = harness.journal.root / "worker.json.new"
    temporary.write_bytes(json.dumps({"phase": "claimed", "id": BUILD_ID, "lease": LEASE}).encode())
    temporary.chmod(0o600)
    instance, transport = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="claim outcome uncertain"):
        instance.process_one()
    assert not transport.calls and not temporary.exists()
    assert harness.journal.load() == {"phase": "claim_pending"}


@pytest.mark.parametrize("results,failed", [([0, 0], False), ([-1], True), ([0, 1], True)])
def test_linux_memory_fence_sets_and_verifies_dumpability_with_fake_libc(monkeypatch, results, failed):
    calls = []
    responses = iter(results)

    class Prctl:
        def __call__(self, *args):
            calls.append(args)
            return next(responses)

    prctl = Prctl()

    def libc(name, *, use_errno):
        assert name is None and use_errno
        return SimpleNamespace(prctl=prctl)

    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.ctypes, "CDLL", libc)
    if failed:
        with pytest.raises(worker.WorkerHalted, match="memory cannot be sealed"):
            worker._seal_worker_memory()
    else:
        worker._seal_worker_memory()
    assert calls[0] == (4, 0, 0, 0, 0)
    assert calls[1:] == ([(3, 0, 0, 0, 0)] if results[0] == 0 else [])
    assert prctl.restype is worker.ctypes.c_int


@pytest.mark.parametrize("failure", ["missing", "load"])
def test_linux_memory_fence_does_not_skip_missing_or_denied_primitive(monkeypatch, failure):
    monkeypatch.setattr(worker.sys, "platform", "linux")

    def libc(*args, **kwargs):
        if failure == "load":
            raise OSError("libc primitive unavailable")
        return SimpleNamespace()

    monkeypatch.setattr(worker.ctypes, "CDLL", libc)
    with pytest.raises(worker.WorkerHalted, match="memory cannot be sealed"):
        worker._seal_worker_memory()


def test_unsupported_memory_fence_is_fail_closed_without_loading_libc(monkeypatch):
    monkeypatch.setattr(worker.sys, "platform", "unsupported")
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *args, **kwargs: pytest.fail("unguarded Linux primitive"))
    with pytest.raises(worker.WorkerHalted, match="memory cannot be sealed"):
        worker._seal_worker_memory()


@pytest.mark.parametrize("field,value", [("id", "../escape"), ("lease", "not-a-lease")])
def test_unidentified_claim_stays_uncertain_without_any_failure_request(harness, field, value):
    descriptor = claim()
    descriptor[field] = value
    instance, transport = harness.make([Response(202, descriptor)])
    with pytest.raises(worker.WorkerHalted, match="claim outcome uncertain"):
        instance.process_one()
    assert [call[0] for call in transport.calls] == ["POST"]
    assert harness.journal.load() == {"phase": "claim_pending", "protocol": "recipe"}
    assert not harness.manifests


@pytest.mark.parametrize("recipe", ["x" * (70 * 1024), "\x00" * (1024 * 1024), "é" * (1024 * 1024 // 2)])
def test_public_size_recipe_and_max_escaped_claim_are_admitted(harness, recipe):
    descriptor = claim()
    descriptor.update(recipe=recipe, recipe_digest=digest(recipe.encode("utf-8")))
    encoded = json.dumps(descriptor, ensure_ascii=True).encode("ascii")
    instance, transport = harness.make(
        [
            Response(202, encoded),
            input_response(),
            Response(200, terminal("complete")),
            Response(200, terminal("complete")),
        ]
    )
    assert instance.process_one()
    assert harness.manifests[0]["recipe"] == recipe
    assert [call[0] for call in transport.calls] == ["POST", "GET", "PUT", "GET"]
    assert harness.journal.load() is None


def test_oversize_actual_utf8_recipe_fails_durable_identity_without_poisoning_worker(harness, monkeypatch):
    descriptor = claim()
    recipe = "é" * (1024 * 1024 // 2 + 1)
    descriptor.update(recipe=recipe, recipe_digest=digest(recipe.encode("utf-8")))
    instance, transport = harness.make(
        [
            Response(202, descriptor),
            Response(200, terminal()),
            Response(200, terminal()),
            Response(204),
        ]
    )
    original_fail = instance._fail
    identities = []

    def fail(record, code):
        identities.append(harness.journal.load())
        return original_fail(record, code)

    monkeypatch.setattr(instance, "_fail", fail)
    assert instance.process_one()
    assert not instance.process_one()
    assert identities == [{"phase": "claimed", "protocol": "recipe", "id": BUILD_ID, "lease": LEASE}]
    assert [call[0] for call in transport.calls] == ["POST", "POST", "GET", "POST"]
    assert json.loads(transport.calls[1][3]) == {"error_code": "build_failed"}
    assert not harness.manifests and harness.journal.load() is None
