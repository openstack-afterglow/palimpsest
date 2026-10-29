"""Behavioral image-worker lease, staging, cleanup and recovery boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from palimpsest_hub import build_worker as worker
from palimpsest_hub import image_executor as image

JOB = "11111111-1111-4111-8111-111111111111"
LEASE = "22222222-2222-4222-8222-222222222222"


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def settings():
    return SimpleNamespace(
        palimpsest_hub_max_blob_bytes=4096,
        palimpsest_hub_build_timeout_seconds=60,
        palimpsest_hub_registry_hosts=("docker.io", "ghcr.io"),
        palimpsest_hub_max_context_bytes=1024,
        palimpsest_hub_max_context_expanded_bytes=4096,
        palimpsest_hub_max_context_members=10,
        palimpsest_hub_image_run_network="none",
        palimpsest_hub_image_cgroup_root="/sys/fs/cgroup",
        palimpsest_hub_image_scratch_bytes=256 * 1024**2,
    )


def claim():
    return {
        "id": JOB,
        "lease": LEASE,
        "deadline": "2030-01-01T00:00:00+00:00",
        "timeout_seconds": 60,
        "name": "image",
        "kind": "registry",
        "platform": "linux/amd64",
        "max_output_bytes": 4096,
        "request": {"reference": "docker.io/library/alpine:3"},
        "context": None,
    }


def result():
    return {
        "layers": [{"digest": sha(b"hsqsone"), "size_bytes": 7}, {"digest": sha(b"hsqstwo"), "size_bytes": 7}],
        "source": {"root_digest": sha(b"root"), "manifest_digest": sha(b"manifest"), "config_digest": sha(b"config")},
        "runtime": {"entrypoint": [], "cmd": ["/bin/true"], "env": [], "working_dir": "/", "user": "0"},
    }


class Response:
    def __init__(self, status, value=None):
        self.status = status
        self.body = io.BytesIO(json.dumps(value or {}).encode())

    def getheader(self, key, default=None):
        return default

    def read1(self, size):
        return self.body.read(size)


class Transport:
    def __init__(self, responses, journal, events):
        self.responses = list(responses)
        self.journal = journal
        self.events = events
        self.calls = []

    @contextmanager
    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        self.events.append((method, path))
        if path.endswith("/claim"):
            protocol = "image" if "/image-worker/" in path else "recipe"
            assert self.journal.load() == {"phase": "claim_pending", "protocol": protocol}
        body = kwargs.get("body")
        if hasattr(body, "read"):
            data = bytearray()
            for part in iter(body.read, b""):
                data.extend(part)
            assert bytes(data) in (b"hsqsone", b"hsqstwo")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        yield response


@pytest.fixture
def harness(tmp_path, monkeypatch):
    journal = worker.Journal(tmp_path / "worker")
    events = []
    executions = []

    def execute(instance, admitted, record, deadline):
        executions.append(record["id"])
        directory = journal.job_dir(record)
        directory.mkdir(mode=0o700)
        path = directory / "outputs.pack"
        path.write_bytes(b"hsqsonehsqstwo" + bytes(4096 - 14))
        path.chmod(0o600)
        record["output_reserve_bytes"] = 4096
        record["phase"] = "executing"
        journal.save(record)
        return result()

    def helper(arguments, timeout, stop=None, log=None, *, module=None):
        assert arguments[0] == "cleanup"
        assert module == "palimpsest_local.hub_image_builder"
        events.append(("cleanup", arguments[1]))
        return b""

    monkeypatch.setattr(image, "build", execute)
    monkeypatch.setattr(worker, "run_executor", helper)

    def make(responses):
        transport = Transport(responses, journal, events)
        return worker.BuildWorker(settings(), transport, journal, threading.Event()), transport

    yield SimpleNamespace(make=make, journal=journal, events=events, executions=executions)
    journal.close()


def complete():
    return Response(200, {"id": JOB, "status": "complete", "result": result()})


def test_image_executes_once_cleanup_precedes_all_uploads_and_finalizes_ordered_layers(harness):
    instance, transport = harness.make([Response(202, claim()), Response(200), Response(200), complete(), complete()])
    assert instance.process_one()
    assert harness.executions == [JOB]
    assert [method for method, _ in transport.calls] == ["POST", "PUT", "PUT", "POST", "GET"]
    assert transport.calls[1][1].endswith("/outputs/" + sha(b"hsqsone"))
    assert transport.calls[2][1].endswith("/outputs/" + sha(b"hsqstwo"))
    assert harness.events[1][0] == "cleanup"
    assert harness.journal.load() is None


def test_empty_image_queue_falls_back_to_recipe_only_after_durable_204(harness):
    instance, transport = harness.make([Response(204), Response(204)])
    assert not instance.process_one()
    assert transport.calls == [("POST", "/v1/image-worker/claim"), ("POST", "/v1/build-worker/claim")]
    assert harness.journal.load() is None


@pytest.mark.parametrize("response", [OSError("lost claim"), Response(500), Response(202, {"id": JOB})])
def test_uncertain_image_claim_never_falls_back_or_replays(harness, response):
    instance, transport = harness.make([response])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    assert harness.journal.load() == {"phase": "claim_pending", "protocol": "image"}
    restarted, other = harness.make([])
    with pytest.raises(worker.WorkerHalted, match="claim outcome uncertain"):
        restarted.process_one()
    assert len(transport.calls) == 1 and not other.calls and not harness.executions


def test_lost_staged_output_response_retains_image_protocol_and_never_restages(harness):
    instance, transport = harness.make(
        [Response(202, claim()), OSError("lost output"), Response(200, {"id": JOB, "status": "building"})]
    )
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    record = harness.journal.load()
    assert record["protocol"] == "image" and record["phase"] == "output_pending"
    restarted, other = harness.make([complete(), Response(204), Response(204)])
    assert not restarted.process_one()
    assert harness.executions == [JOB]
    assert [method for method, _ in other.calls] == ["GET", "POST", "POST"]
    assert all(method != "PUT" for method, _ in other.calls)


def test_completed_provenance_conflict_holds_without_disposing(harness):
    wrong = result()
    wrong["runtime"]["cmd"] = ["changed"]
    instance, _ = harness.make(
        [
            Response(202, claim()),
            Response(200),
            Response(200),
            complete(),
            Response(200, {"id": JOB, "status": "complete", "result": wrong}),
        ]
    )
    with pytest.raises(worker.WorkerHalted, match="conflicts"):
        instance.process_one()
    assert harness.journal.load()["phase"] == "output_pending"


def test_cleanup_failure_never_stages_or_fails_remote_job(harness, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("cgroup populated")

    monkeypatch.setattr(worker, "run_executor", failed)
    instance, transport = harness.make([Response(202, claim())])
    with pytest.raises(worker.WorkerHalted, match="cleanup"):
        instance.process_one()
    assert [method for method, _ in transport.calls] == ["POST"]
    assert harness.journal.load()["phase"] == "executing"


@pytest.mark.parametrize("reference", ["localhost/a", "127.0.0.1/a", "docker.io:443/a", "https://docker.io/a"])
def test_worker_revalidates_registry_allowlist_before_execution(reference):
    value = claim()
    value["request"]["reference"] = reference
    with pytest.raises(ValueError):
        image.validate_claim(value, settings())


def test_context_claim_requires_exact_digest_descriptor_binding():
    value = claim()
    value.update(
        kind="dockerfile",
        request={"dockerfile": "FROM scratch\nCOPY . /app\n", "context_digest": sha(b"context")},
        context={"digest": sha(b"different"), "size_bytes": 10},
    )
    with pytest.raises(ValueError, match="context"):
        image.validate_claim(value, settings())


def test_result_rejects_total_layer_size_and_noncanonical_provenance():
    with pytest.raises(ValueError, match="exceeds"):
        image.validate_result(result(), 10)
    value = result()
    value["source"]["config_digest"] = "sha256:" + "A" * 64
    with pytest.raises(ValueError):
        image.validate_result(value, 100)


@pytest.mark.parametrize(
    "key,value",
    [
        ("entrypoint", ["arg"] * 257),
        ("cmd", ["arg"] * 257),
        ("env", ["A=" + "x" * 16385]),
        ("user", "u" * 257),
    ],
)
def test_image_runtime_rejected_before_unrecoverable_output_staging(key, value):
    descriptor = result()
    descriptor["runtime"][key] = value
    with pytest.raises(ValueError, match="runtime"):
        image.validate_result(descriptor, 4096)


def test_image_completion_escape_expansion_is_bounded_before_output_staging():
    descriptor = result()
    descriptor["runtime"]["env"] = ["A=" + "\u2603" * 4000] * 32
    with pytest.raises(ValueError, match="wire limit"):
        image.validate_result(descriptor, 4096)


@pytest.mark.parametrize("damage", ["second-digest", "truncated-reserve", "oversized-reserve", "missing-reserve"])
def test_packed_output_is_fully_pinned_before_any_mutation(tmp_path, damage):
    journal = worker.Journal(tmp_path / "worker")
    record = {"id": JOB, "lease": LEASE, "phase": "executing", "protocol": "image", "output_reserve_bytes": 4096}
    directory = journal.job_dir(record)
    directory.mkdir(mode=0o700)
    payload = b"hsqsonehsqstwo" + bytes(4096 - 14)
    if damage == "second-digest":
        payload = payload[:7] + b"hsqsbAD" + payload[14:]
    elif damage == "truncated-reserve":
        payload = payload[:-1]
    elif damage == "oversized-reserve":
        payload += b"x"
    if damage != "missing-reserve":
        (directory / "outputs.pack").write_bytes(payload)
        (directory / "outputs.pack").chmod(0o600)

    class NoMutations:
        def request(self, *args, **kwargs):
            pytest.fail("invalid packed output reached remote mutation")

    instance = SimpleNamespace(settings=settings(), journal=journal, transport=NoMutations(), stop=threading.Event())
    try:
        with pytest.raises((worker.BuildFailed, FileNotFoundError)):
            image.upload(instance, record, result(), float("inf"))
        assert record["phase"] == "executing"
    finally:
        journal.close()


def test_worker_scratch_default_and_bounds_are_separate_from_blob_retention():
    from palimpsest_hub.config import BuildWorkerSettings

    values = {
        "palimpsest_hub_url": "https://hub.example.test",
        "palimpsest_hub_build_worker_key_file": "/private/key",
        "palimpsest_hub_build_work_path": "/private/scratch",
    }
    parsed = BuildWorkerSettings(_env_file=None, **values)
    assert parsed.palimpsest_hub_image_scratch_bytes == 4 * 1024**3
    assert parsed.palimpsest_hub_image_scratch_bytes < parsed.palimpsest_hub_max_blob_bytes
    for capacity in (0, 256 * 1024**2 - 1, 64 * 1024**3 + 1):
        with pytest.raises(ValueError):
            BuildWorkerSettings(_env_file=None, **values, palimpsest_hub_image_scratch_bytes=capacity)


def test_packed_range_eof_never_reads_adjacent_layer_or_reserved_padding(tmp_path):
    path = tmp_path / "outputs.pack"
    path.write_bytes(b"hsqsonehsqstwo" + bytes(100))
    with path.open("rb") as stream:
        first = image._PackedRange(stream, 0, 7, 114)
        assert first.read(4) == b"hsqs"
        assert first.read(100) == b"one"
        assert first.read(1) == b""
        second = worker.PinnedBody(image._PackedRange(stream, 7, 7, 114), 7, float("inf"))
        assert b"".join(iter(second.read, b"")) == b"hsqstwo"


@pytest.mark.parametrize("change", ["truncate", "grow"])
def test_packed_range_rechecks_pinned_size_before_every_read_including_eof(tmp_path, change):
    path = tmp_path / "outputs.pack"
    path.write_bytes(b"hsqsone" + bytes(100))
    with path.open("rb") as stream:
        span = image._PackedRange(stream, 0, 7, 107)
        assert span.read(4) == b"hsqs"
        with path.open("r+b") as mutation:
            mutation.truncate(5 if change == "truncate" else 108)
        with pytest.raises(worker.BuildFailed, match="changed"):
            span.read(100)


def test_packed_range_rejects_span_beyond_pinned_reserve(tmp_path):
    path = tmp_path / "outputs.pack"
    path.write_bytes(b"hsqsone")
    with path.open("rb") as stream, pytest.raises(worker.BuildFailed, match="span"):
        image._PackedRange(stream, 3, 7, 7)
