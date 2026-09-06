"""Canonical wire and process-boundary tests for the OCI hard worker."""

from __future__ import annotations

import errno
import io
import json
import os
import signal
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

import palimpsest_local.oci_materializer as materializer
import palimpsest_local.oci_materializer_worker as worker
from palimpsest_local.oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID
from palimpsest_local.oci_materializer import OCIHardWorkerError
from palimpsest_local.oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    SquashFSPackError,
    SquashFSToolchainIdentity,
    VerifiedSquashFSToolchain,
)
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
    Platform,
    ProvenanceSource,
)
from palimpsest_local.oci_store import (
    DerivedLayerOccurrence,
    DerivedLayerReceipt,
    DerivedSquashFSKey,
    MaterializationResult,
)
from palimpsest_local.oci_worker_protocol import (
    MAX_OCI_WORKER_MESSAGE_BYTES,
    OCIWorkerProtocolError,
    OCIWorkerRequest,
    OCIWorkerResponse,
)


def _digest(character: str) -> str:
    return f"sha256:{character * 64}"


def _request() -> OCIWorkerRequest:
    occurrence = DerivedLayerOccurrence(
        source_snapshot_binding_digest=_digest("a"),
        source_image_digest=_digest("b"),
        ordinal=0,
        media_type=OCI_LAYER_MEDIA_TYPE,
        compressed_digest=_digest("c"),
        compressed_size=123,
        diff_id=_digest("d"),
    )
    identity = SquashFSToolchainIdentity("4.7.5", _digest("e"), (_digest("f"),))
    toolchain = VerifiedSquashFSToolchain(identity, Path("/usr/bin/mksquashfs"), ())
    key = DerivedSquashFSKey.for_occurrence(
        occurrence,
        intake_policy_id=LAYER_INTAKE_POLICY_ID,
        intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
        pack_policy_id=SQUASHFS_PACK_POLICY_ID,
        pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
        toolchain=toolchain,
    )
    source = ProvenanceSource(
        registry="registry.example.com",
        repository="team/app",
        requested_reference=f"registry.example.com/team/app@{_digest('1')}",
        index_descriptor=None,
        manifest_descriptor=Descriptor(OCI_IMAGE_MANIFEST_MEDIA_TYPE, _digest("1"), 100),
        config_descriptor=Descriptor(OCI_IMAGE_CONFIG_MEDIA_TYPE, _digest("2"), 50),
        platform=Platform("linux", "amd64"),
    )
    return OCIWorkerRequest(
        nonce=str(uuid.uuid4()),
        config_root=Path("/var/lib/palimpsest-config"),
        state_root=Path("/var/lib/palimpsest-state"),
        expected_store_id="oci-store-v1:" + "3" * 64,
        source_cas_root=Path("/var/lib/palimpsest-source"),
        expected_source_cas_id="source-cas-v1:" + "4" * 64,
        source=source,
        occurrence=occurrence,
        key=key,
        key_digest=key.digest,
        packer_path=Path("/usr/bin/mksquashfs"),
        packer_sha256=identity.executable_digest.removeprefix("sha256:"),
        cpu_limit_seconds=30,
    )


def _result(request: OCIWorkerRequest) -> MaterializationResult:
    return MaterializationResult(
        receipt=DerivedLayerReceipt(
            store_id=request.expected_store_id,
            occurrence_digest=_digest("5"),
            record_digest=_digest("6"),
            key_digest=request.key_digest,
            source_snapshot_binding_digest=request.occurrence.source_snapshot_binding_digest,
            source_image_digest=request.occurrence.source_image_digest,
            ordinal=request.occurrence.ordinal,
            image_digest=_digest("7"),
            image_size=4096,
        ),
        cache_result="cold_miss",
    )


def test_request_and_success_response_round_trip_as_canonical_json() -> None:
    request = _request()
    response = OCIWorkerResponse(request.nonce, request.digest, "succeeded", _result(request), None)

    assert OCIWorkerRequest.from_json_bytes(request.to_json_bytes()) == request
    assert OCIWorkerResponse.from_json_bytes(response.to_json_bytes()) == response
    assert (
        request.to_json_bytes()
        == json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b"\xff",
        b" " + _request().to_json_bytes(),
        b"[" * 20 + b"0" + b"]" * 20,
        b"x" * (MAX_OCI_WORKER_MESSAGE_BYTES + 1),
    ],
)
def test_request_rejects_duplicate_nonfinite_noncanonical_deep_and_oversize_json(payload: bytes) -> None:
    with pytest.raises(OCIWorkerProtocolError):
        OCIWorkerRequest.from_json_bytes(payload)


def test_request_rejects_unknown_fields_and_digest_or_path_tampering() -> None:
    request = _request()
    unknown = request.to_dict()
    unknown["unexpected"] = True
    bad_digest = request.to_dict()
    bad_digest["key"]["digest"] = _digest("9")
    bad_path = request.to_dict()
    bad_path["roots"]["state"] = "/var/lib/../tmp"

    for value in (unknown, bad_digest, bad_path):
        with pytest.raises(OCIWorkerProtocolError):
            OCIWorkerRequest.from_dict(value)


def test_failed_response_exposes_only_a_stable_category() -> None:
    request = _request()
    response = OCIWorkerResponse(request.nonce, request.digest, "failed", None, "pack")

    assert OCIWorkerResponse.from_json_bytes(response.to_json_bytes()) == response
    assert "/" not in response.to_json_bytes().decode()
    with pytest.raises(OCIWorkerProtocolError):
        replace(response, error_category="/tmp/private-detail")


def test_parent_worker_boundary_starts_a_new_session_and_binds_stdin(tmp_path: Path) -> None:
    request = _request()
    script = (
        "import json,os,sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.write(json.dumps({'bytes':len(data),'pid':os.getpid(),'pgrp':os.getpgrp()}))"
    )

    output, return_code, process = materializer._spawn_and_exchange(
        request,
        scratch=tmp_path,
        timeout_seconds=2,
        grace_seconds=0.2,
        command=(sys.executable, "-c", script),
    )

    value = json.loads(output)
    assert return_code == 0
    assert value == {"bytes": len(request.to_json_bytes()), "pid": process.pid, "pgrp": process.pid}


@pytest.mark.parametrize("number", [errno.EAGAIN, errno.ENOMEM, errno.EACCES, errno.ENOENT])
def test_worker_spawn_classifies_only_known_resource_errors(tmp_path, monkeypatch, number):
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise OSError(number, "/private/host/detail")

    monkeypatch.setattr(materializer.subprocess, "Popen", fail)
    with pytest.raises(OCIHardWorkerError) as raised:
        materializer._spawn_and_exchange(_request(), scratch=tmp_path, timeout_seconds=1, grace_seconds=0.1)
    resource = number in {errno.EAGAIN, errno.ENOMEM}
    assert raised.value.code == ("oci-worker-resource" if resource else "oci-worker-spawn")
    assert "/private/host/detail" not in str(raised.value)
    if resource:
        assert errno.errorcode[number] in str(raised.value)
        assert "no automatic retry or limit change" in str(raised.value)
    assert len(calls) == 1 and calls[0]["start_new_session"] is True


@pytest.mark.parametrize("failed_start", [1, 2])
def test_partial_helper_start_failure_reaps_owned_worker(tmp_path, monkeypatch, failed_start):
    original_start = materializer.threading.Thread.start
    starts = []

    def start(thread):
        starts.append(thread)
        if len(starts) == failed_start:
            raise RuntimeError("can't start new thread")
        return original_start(thread)

    monkeypatch.setattr(materializer.threading.Thread, "start", start)
    with pytest.raises(materializer._WorkerBoundaryFailure) as raised:
        materializer._spawn_and_exchange(
            _request(),
            scratch=tmp_path,
            timeout_seconds=2,
            grace_seconds=0.5,
            command=(sys.executable, "-c", "import sys,time;sys.stdin.buffer.read();time.sleep(30)"),
        )
    failure = raised.value
    assert failure.code == "oci-worker-resource" and failure.reaped
    assert failure.process.poll() is not None
    assert failure.process.stdin.closed and failure.process.stdout.closed
    assert all(not thread.is_alive() for thread in starts)


def test_helper_constructor_failure_also_reaps_owned_worker(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(materializer.threading, "Thread", fail)
    with pytest.raises(materializer._WorkerBoundaryFailure) as raised:
        materializer._spawn_and_exchange(
            _request(),
            scratch=tmp_path,
            timeout_seconds=2,
            grace_seconds=0.5,
            command=(sys.executable, "-c", "import time;time.sleep(30)"),
        )
    assert raised.value.reaped and raised.value.process.poll() is not None
    assert raised.value.process.stdin.closed and raised.value.process.stdout.closed


def test_interrupted_start_after_actual_thread_creation_reaps_and_joins(tmp_path, monkeypatch):
    original_start = materializer.threading.Thread.start
    original_spawn = materializer.subprocess.Popen
    processes = []
    threads = []

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def start(thread):
        threads.append(thread)
        original_start(thread)
        raise KeyboardInterrupt

    monkeypatch.setattr(materializer.subprocess, "Popen", spawn)
    monkeypatch.setattr(materializer.threading.Thread, "start", start)
    with pytest.raises(KeyboardInterrupt):
        materializer._spawn_and_exchange(
            _request(),
            scratch=tmp_path,
            timeout_seconds=2,
            grace_seconds=0.5,
            command=(sys.executable, "-c", "import time;time.sleep(30)"),
        )
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed
    assert not threads[0].is_alive()


def test_request_serialization_failure_precedes_worker_spawn(tmp_path, monkeypatch):
    def fail(request):
        raise MemoryError

    monkeypatch.setattr(OCIWorkerRequest, "to_json_bytes", fail)
    monkeypatch.setattr(materializer.subprocess, "Popen", lambda *a, **k: pytest.fail("worker spawned"))
    with pytest.raises(MemoryError):
        materializer._spawn_and_exchange(_request(), scratch=tmp_path, timeout_seconds=1, grace_seconds=0.1)


def test_unreaped_helper_failure_retains_process_and_open_pipes(tmp_path, monkeypatch):
    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()

    process = Process()

    def fail(thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(materializer.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(materializer.threading.Thread, "start", fail)
    monkeypatch.setattr(materializer, "_terminate_worker", lambda *a: False)
    with pytest.raises(materializer._WorkerBoundaryFailure) as raised:
        materializer._spawn_and_exchange(_request(), scratch=tmp_path, timeout_seconds=1, grace_seconds=0.1)
    assert raised.value.process is process and not raised.value.reaped
    assert "cleanup is deferred" in str(raised.value)
    assert not process.stdin.closed and not process.stdout.closed


def test_termination_error_during_startup_failure_preserves_unknown_worker_ownership(tmp_path, monkeypatch):
    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()

    process = Process()
    primary = RuntimeError("can't start new thread")

    def fail(thread):
        raise primary

    def cleanup(*args):
        raise OSError(errno.EIO, "uncertain wait")

    monkeypatch.setattr(materializer.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(materializer.threading.Thread, "start", fail)
    monkeypatch.setattr(materializer, "_terminate_worker", cleanup)
    with pytest.raises(materializer._WorkerBoundaryFailure) as raised:
        materializer._spawn_and_exchange(_request(), scratch=tmp_path, timeout_seconds=1, grace_seconds=0.1)
    assert raised.value.process is process and not raised.value.reaped
    assert raised.value.__cause__ is primary
    assert not process.stdin.closed and not process.stdout.closed


def test_reaper_start_failure_retains_scratch_but_releases_untransferred_pin(tmp_path, monkeypatch):
    scratch = tmp_path / "owned-scratch"
    scratch.mkdir()
    (scratch / "evidence").write_text("retain")
    descriptor = os.open(scratch, os.O_RDONLY)

    def fail(thread):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(materializer.threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        materializer._background_reap(object(), scratch, descriptor, tmp_path)
    assert (scratch / "evidence").read_text() == "retain"
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF


@pytest.mark.parametrize("failure", [RuntimeError("start interrupted"), MemoryError(), KeyboardInterrupt()])
def test_reaper_exception_after_actual_start_does_not_close_transferred_pin(tmp_path, monkeypatch, failure):
    scratch = tmp_path / "owned-scratch"
    scratch.mkdir()
    descriptor = os.open(scratch, os.O_RDONLY)
    release = materializer.threading.Event()
    original_start = materializer.threading.Thread.start
    threads = []
    closed = []

    class Process:
        pid = 12345

        def wait(self):
            assert release.wait(timeout=2)

    def start(thread):
        threads.append(thread)
        original_start(thread)
        raise failure

    def cleanup(path, fd, parent):
        os.fstat(fd)
        closed.append(fd)
        os.close(fd)

    monkeypatch.setattr(materializer.threading.Thread, "start", start)
    monkeypatch.setattr(materializer, "_group_exists", lambda pid: False)
    monkeypatch.setattr(materializer, "_cleanup_scratch", cleanup)
    try:
        with pytest.raises(type(failure)) as raised:
            materializer._background_reap(Process(), scratch, descriptor, tmp_path)
        assert raised.value is failure
        os.fstat(descriptor)
        assert closed == []
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
    assert closed == [descriptor]
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF


@pytest.mark.parametrize("failure_point", ["wait", "group"])
def test_reaper_uncertain_liveness_preserves_scratch_and_releases_pin(tmp_path, monkeypatch, failure_point):
    scratch = tmp_path / "owned-scratch"
    scratch.mkdir()
    descriptor = os.open(scratch, os.O_RDONLY)
    targets = []

    class Thread:
        def __init__(self, *, target, **kwargs):
            targets.append(target)

        def start(self):
            pass

    class Process:
        pid = 12345

        def wait(self):
            if failure_point == "wait":
                raise OSError(errno.EIO, "uncertain wait")

    def group(pid):
        raise OSError(errno.EIO, "uncertain group")

    monkeypatch.setattr(materializer.threading, "Thread", Thread)
    monkeypatch.setattr(materializer, "_group_exists", group)
    monkeypatch.setattr(materializer, "_cleanup_scratch", lambda *a: pytest.fail("scratch deleted"))
    materializer._background_reap(Process(), scratch, descriptor, tmp_path)
    with pytest.raises(OSError, match="uncertain"):
        targets[0]()
    assert scratch.is_dir()
    with pytest.raises(OSError) as raised:
        os.fstat(descriptor)
    assert raised.value.errno == errno.EBADF


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (OSError(errno.EAGAIN, "private"), "resource"),
        (OSError(errno.ENOMEM, "private"), "resource"),
        (MemoryError(), "resource"),
        (OSError(errno.EACCES, "private"), "internal"),
        (SquashFSPackError("oci-packer-resource", "private"), "resource"),
        (SquashFSPackError("oci-packer-spawn", "private"), "pack"),
    ],
)
def test_worker_resource_classification_is_path_free_and_precise(failure, category):
    assert worker._category(failure) == category


def test_worker_process_cap_and_other_limits_are_not_relaxed(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.resource, "getrlimit", lambda limit: (worker.resource.RLIM_INFINITY,) * 2)
    monkeypatch.setattr(worker.resource, "setrlimit", lambda limit, bounds: calls.append((limit, bounds)))
    worker._apply_resource_limits(30)
    assert (worker.resource.RLIMIT_CORE, (0, 0)) in calls
    assert (worker.resource.RLIMIT_CPU, (30, 30)) in calls
    assert (worker.resource.RLIMIT_NOFILE, (256, 256)) in calls
    assert (worker.resource.RLIMIT_FSIZE, (40 * 1024**3,) * 2) in calls
    if hasattr(worker.resource, "RLIMIT_NPROC"):
        assert (worker.resource.RLIMIT_NPROC, (256, 256)) in calls
    if hasattr(worker.resource, "RLIMIT_AS"):
        assert (worker.resource.RLIMIT_AS, (40 * 1024**3,) * 2) in calls


def test_parent_worker_boundary_caps_response_bytes(tmp_path: Path) -> None:
    request = _request()
    script = f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'x'*{MAX_OCI_WORKER_MESSAGE_BYTES + 1})"

    with pytest.raises(OCIHardWorkerError, match="response exceeds its bound"):
        materializer._spawn_and_exchange(
            request,
            scratch=tmp_path,
            timeout_seconds=2,
            grace_seconds=0.2,
            command=(sys.executable, "-c", script),
        )


@pytest.mark.skipif(os.name != "posix", reason="process-group signals require POSIX")
def test_parent_worker_boundary_terminates_and_reaps_timed_out_group(tmp_path: Path) -> None:
    request = _request()
    script = "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"

    with pytest.raises(OCIHardWorkerError, match="wall-clock deadline") as raised:
        materializer._spawn_and_exchange(
            request,
            scratch=tmp_path,
            timeout_seconds=0.2,
            grace_seconds=0.5,
            command=(sys.executable, "-c", script),
        )

    process = raised.value.process
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, signal.SIGCONT)


@pytest.mark.skipif(os.name != "posix", reason="process-group signals require POSIX")
def test_parent_worker_boundary_kills_a_term_ignoring_child_after_leader_exit(tmp_path: Path) -> None:
    request = _request()
    child_pid_file = tmp_path / "child.pid"
    child_script = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({os.fspath(child_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    leader_script = (
        "import pathlib,subprocess,sys,time\n"
        "sys.stdin.buffer.read()\n"
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
        f"path = pathlib.Path({os.fspath(child_pid_file)!r})\n"
        "deadline = time.monotonic() + 2\n"
        "while not path.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )

    with pytest.raises(OCIHardWorkerError, match="wall-clock deadline"):
        materializer._spawn_and_exchange(
            request,
            scratch=tmp_path,
            timeout_seconds=0.2,
            grace_seconds=0.5,
            command=(sys.executable, "-c", leader_script),
        )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGCONT)


@pytest.mark.skipif(os.name != "posix", reason="process-group signals require POSIX")
def test_parent_worker_boundary_rejects_success_with_a_live_descendant(tmp_path: Path) -> None:
    request = _request()
    child_pid_file = tmp_path / "successful-child.pid"
    child_script = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({os.fspath(child_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    leader_script = (
        "import pathlib,subprocess,sys,time\n"
        "sys.stdin.buffer.read()\n"
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
        f"path = pathlib.Path({os.fspath(child_pid_file)!r})\n"
        "deadline = time.monotonic() + 2\n"
        "while not path.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('{}')\n"
    )

    with pytest.raises(OCIHardWorkerError, match="left a live descendant"):
        materializer._spawn_and_exchange(
            request,
            scratch=tmp_path,
            timeout_seconds=2,
            grace_seconds=0.5,
            command=(sys.executable, "-c", leader_script),
        )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGCONT)
