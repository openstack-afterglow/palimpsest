"""Behavioral image-worker lease, staging, cleanup and recovery boundaries."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
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
            pending = self.journal.load()
            assert pending["phase"] == "claim_pending" and pending["protocol"] == protocol
            assert kwargs["headers"] == worker.BuildWorker._claim_headers(pending)
        body = kwargs.get("body")
        if hasattr(body, "read"):
            data = bytearray()
            for part in iter(body.read, b""):
                data.extend(part)
            assert bytes(data) in (b"hsqsone", b"hsqstwo")
        response = self.responses.pop(0)
        if callable(response):
            response = response()
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
    instance, transport = harness.make([response, Response(503)])
    with pytest.raises(worker.WorkerHalted):
        instance.process_one()
    pending = harness.journal.load()
    assert pending["phase"] == "claim_pending" and pending["protocol"] == "image"
    assert worker._uuid(pending["channel"]) and pending["sequence"] == 1
    restarted, other = harness.make([Response(503)])
    with pytest.raises(worker.WorkerHalted, match="claim outcome uncertain"):
        restarted.process_one()
    assert [method for method, _ in transport.calls] == ["POST", "GET"]
    assert [method for method, _ in other.calls] == ["GET"]
    assert not harness.executions and harness.journal.load() == pending


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


@pytest.mark.parametrize("accepted", [False, True])
def test_image_response_loss_settles_exact_outcome_without_recipe_fallback(harness, accepted):
    def recovery():
        pending = harness.journal.load()
        return Response(
            200,
            {
                **{key: pending[key] for key in ("channel", "sequence", "protocol")},
                "claim": {"id": JOB, "lease": LEASE} if accepted else None,
            },
        )

    responses = [OSError("lost image claim response"), recovery]
    if accepted:
        responses += [Response(200, {"id": JOB, "status": "error"}), Response(200, {"id": JOB, "status": "error"})]
    instance, transport = harness.make(responses)
    assert instance.process_one()
    assert transport.calls[:2] == [("POST", "/v1/image-worker/claim"), ("GET", "/v1/image-worker/claim")]
    assert all("/build-worker/" not in route for _, route in transport.calls)
    assert not harness.executions and harness.journal.load() is None
    assert harness.journal.cursor["sequence"] == 1


def test_image_restart_recovers_exact_lease_without_execution(harness):
    pending = harness.journal.begin_claim("image")
    instance, transport = harness.make(
        [
            Response(
                200,
                {
                    **{key: pending[key] for key in ("channel", "sequence", "protocol")},
                    "claim": {"id": JOB, "lease": LEASE},
                },
            ),
            Response(200, {"id": JOB, "status": "error"}),
            Response(200, {"id": JOB, "status": "error"}),
        ]
    )
    instance.reconcile()
    assert transport.calls == [
        ("GET", "/v1/image-worker/claim"),
        ("POST", f"/v1/image-worker/jobs/{JOB}/fail"),
        ("GET", f"/v1/image-worker/jobs/{JOB}"),
    ]
    assert not harness.executions and harness.journal.load() is None


def test_real_failed_executor_tail_survives_cleanup_disposal_and_next_idle_claim(tmp_path, monkeypatch, caplog):
    journal = worker.Journal(tmp_path / "worker")
    events = []
    payloads = []
    actual_popen = subprocess.Popen
    actual_executor = worker.run_executor
    attempt = 0

    class PrivateTransport(Transport):
        @contextmanager
        def request(self, method, path, **kwargs):
            if path.endswith("/fail"):
                payloads.append(json.loads(kwargs["body"]))
                assert not journal.job_dir({"id": JOB}).joinpath("scratch").exists()
            with super().request(method, path, **kwargs) as response:
                yield response

    def launch(argv, **kwargs):
        nonlocal attempt
        attempt += 1
        assert kwargs["env"] == worker.EXECUTOR_ENV
        assert "PALIMPSEST_HUB_BUILD_WORKER_KEY" not in kwargs["env"]
        # A real failing process emits far more than one retained window. Final
        # stderr (not a mocked error string) must survive the entire worker path.
        code = (
            "import os, pathlib, sys; "
            "d=pathlib.Path(sys.argv[1]).parent; (d/'scratch').mkdir(); "
            "[(d/n).write_bytes(b'prefix'+b'x'*200000+b'private-tool-tail') "
            "for n in ('buildctl.log','buildkitd.log')]; "
            "[(d/n).chmod(0o600) for n in ('buildctl.log','buildkitd.log')]; "
            "os.write(2,b'discarded-prefix'+b'x'*200000+b'private-recipe-path-tail-'+sys.argv[2].encode()); "
            "raise SystemExit(1)"
        )
        return actual_popen([sys.executable, "-I", "-c", code, argv[-2], str(attempt)], **kwargs)

    def executor(arguments, timeout, stop=None, log=None, *, module=None):
        if arguments[0] == "cleanup":
            directory = journal.job_dir({"id": JOB})
            assert (directory / "executor.log").stat().st_size == worker.LOG_LIMIT
            shutil.rmtree(directory / "scratch")
            events.append(("cleanup", str(directory)))
            return b""
        return actual_executor(arguments, timeout, stop, log, module=module)

    monkeypatch.setenv("PALIMPSEST_HUB_BUILD_WORKER_KEY", "never-copy-worker-key")
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    monkeypatch.setattr(worker, "run_executor", executor)
    transport = PrivateTransport(
        [
            response
            for _ in range(2)
            for response in (Response(202, claim()), Response(200), Response(200, {"id": JOB, "status": "error"}))
        ],
        journal,
        events,
    )
    instance = worker.BuildWorker(settings(), transport, journal, threading.Event())
    try:
        for ordinal in (1, 2):
            assert instance.process_one()
            artifact = journal.root / image.FAILURE_FILE
            raw = artifact.read_bytes()
            assert len(raw) <= image.FAILURE_LIMIT
            assert artifact.stat().st_mode & 0o777 == 0o600
            retained = json.loads(raw)
            tails = {key: base64.b64decode(value) for key, value in retained["tails_base64"].items()}
            assert set(tails) == {"executor.log", "buildctl.log", "buildkitd.log"}
            assert all(len(value) == worker.LOG_LIMIT for value in tails.values())
            assert tails["executor.log"].endswith(f"private-recipe-path-tail-{ordinal}".encode())
            assert tails["buildctl.log"].endswith(b"private-tool-tail")
            assert all(b"prefix" not in value for value in tails.values())
            assert b"never-copy-worker-key" not in raw
            assert not journal.job_dir({"id": JOB}).exists()
            assert journal.load() is None
            assert {path.name for path in journal.root.iterdir()} == {"worker.lock", "worker.json", image.FAILURE_FILE}
        assert journal.cursor["sequence"] == 2
        assert payloads == [{"error_code": "build_failed"}] * 2
        assert "private-recipe" not in caplog.text and "private-tool" not in caplog.text
    finally:
        journal.close()


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "permissions", "oversize"])
def test_idle_journal_rejects_unsafe_diagnostic_artifact(tmp_path, damage):
    journal = worker.Journal(tmp_path / "worker")
    artifact = journal.root / image.FAILURE_FILE
    protected = tmp_path / "protected"
    protected.write_bytes(b"private unrelated content")
    protected.chmod(0o600)
    if damage == "symlink":
        artifact.symlink_to(protected)
    elif damage == "hardlink":
        os.link(protected, artifact)
    else:
        artifact.write_bytes(b"x" * (image.FAILURE_LIMIT + 1) if damage == "oversize" else b"private")
        artifact.chmod(0o600 if damage == "oversize" else 0o644)
    try:
        with pytest.raises((worker.WorkerHalted, OSError)):
            journal.load()
        assert protected.read_bytes() == b"private unrelated content"
    finally:
        journal.close()


def test_uncommitted_diagnostics_never_replace_previous_evidence_or_journal(tmp_path):
    journal = worker.Journal(tmp_path / "worker")
    journal.begin_claim("image")
    journal.clear()
    original = journal.path.read_bytes()
    target = journal.root / image.FAILURE_FILE
    target.write_bytes(b"committed failure")
    target.chmod(0o600)
    temporary = journal.root / (image.FAILURE_FILE + ".new")
    temporary.write_bytes(b"partial replacement")
    temporary.chmod(0o600)
    try:
        assert journal.load() is None
        assert target.read_bytes() == b"committed failure"
        assert not temporary.exists()
        assert journal.path.read_bytes() == original
    finally:
        journal.close()


def test_cleanup_failure_does_not_read_or_replace_private_evidence(harness, monkeypatch):
    def failed(instance, admitted, record, deadline):
        directory = instance.journal.job_dir(record)
        directory.mkdir(mode=0o700)
        record["phase"] = "executing"
        instance.journal.save(record)
        raise worker.BuildFailed("private failure")

    def unsafe_cleanup(*args, **kwargs):
        raise worker.BuildFailed("owned namespace still populated")

    def forbidden(*args, **kwargs):
        pytest.fail("evidence read before cleanup quiescence")

    monkeypatch.setattr(image, "build", failed)
    monkeypatch.setattr(worker, "run_executor", unsafe_cleanup)
    monkeypatch.setattr(image, "retain_failure", forbidden)
    instance, transport = harness.make([Response(202, claim())])
    with pytest.raises(worker.WorkerHalted, match="cleanup"):
        instance.process_one()
    assert len(transport.calls) == 1
