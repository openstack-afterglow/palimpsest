"""Canonical wire and process-boundary tests for the OCI hard worker."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import signal
import struct
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import palimpsest_local.oci_materializer as materializer
import palimpsest_local.oci_materializer_worker as worker
import palimpsest_local.oci_store as oci_store
from palimpsest_local.oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID
from palimpsest_local.oci_image import OCIImageRef
from palimpsest_local.oci_materializer import OCIHardWorkerError
from palimpsest_local.oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
    SQUASHFS_STRUCTURAL_VERIFIER_ID,
    LeasedSquashFS,
    PackedSquashFSReceipt,
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
from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS
from palimpsest_local.oci_store import (
    DerivedLayerOccurrence,
    DerivedLayerReceipt,
    DerivedSquashFSKey,
    MaterializationResult,
    OCIStore,
)
from palimpsest_local.oci_worker_protocol import (
    MAX_OCI_WORKER_MESSAGE_BYTES,
    OCI_WORKER_LEGACY_RESPONSE_SCHEMA,
    OCI_WORKER_RESPONSE_SCHEMA,
    OCIWorkerProtocolError,
    OCIWorkerRequest,
    OCIWorkerResponse,
)
from palimpsest_local.state import StatePaths, init_resolved_roots


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


def _materialization_fixture(tmp_path: Path) -> SimpleNamespace:
    layer_stream = io.BytesIO()
    member = tarfile.TarInfo("value")
    member.mode = 0o644
    member.size = len(b"fixture")
    with tarfile.open(fileobj=layer_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(member, io.BytesIO(b"fixture"))
    layer_payload = layer_stream.getvalue()

    layout = tmp_path / "layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")

    def add_blob(payload: bytes, media_type: str) -> Descriptor:
        descriptor = Descriptor(
            media_type=media_type,
            digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            size=len(payload),
        )
        (blobs / descriptor.digest.removeprefix("sha256:")).write_bytes(payload)
        return descriptor

    layer = add_blob(layer_payload, OCI_LAYER_MEDIA_TYPE)
    config_payload = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [layer.digest]},
        },
        separators=(",", ":"),
    ).encode()
    config = add_blob(config_payload, OCI_IMAGE_CONFIG_MEDIA_TYPE)
    manifest_payload = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "config": config.to_dict(),
            "layers": [layer.to_dict()],
        },
        separators=(",", ":"),
    ).encode()
    manifest = add_blob(manifest_payload, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
    (layout / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [manifest.to_dict()]}, separators=(",", ":")),
        encoding="utf-8",
    )

    source_cas_root = tmp_path / "source-cas"
    cas = SourceCAS(source_cas_root)
    reference = OCIImageRef(
        registry="registry.example.com",
        repository="team/app",
        requested_reference="registry.example.com/team/app:latest",
    )
    image = LocalLayoutSource.parse(f"oci-layout://{layout}@{manifest.digest}").snapshot(reference, cas)
    roots = init_resolved_roots(StatePaths(tmp_path / "config", tmp_path / "state"))
    store = OCIStore(roots)
    packer = (tmp_path / "mksquashfs").resolve()
    packer.write_bytes(b"fixture-mksquashfs")
    packer.chmod(0o500)
    executable_digest = f"sha256:{hashlib.sha256(packer.read_bytes()).hexdigest()}"
    identity = SquashFSToolchainIdentity("4.7.5", executable_digest, ())
    toolchain = VerifiedSquashFSToolchain(identity, packer, ())
    return SimpleNamespace(
        cas=cas,
        image=image,
        packer=packer,
        roots=roots,
        source_cas_root=source_cas_root,
        store=store,
        toolchain=toolchain,
    )


def _fixture_worker_request(fixture: SimpleNamespace) -> OCIWorkerRequest:
    occurrence = DerivedLayerOccurrence.from_image(fixture.image, 0)
    key = DerivedSquashFSKey.for_occurrence(
        occurrence,
        intake_policy_id=LAYER_INTAKE_POLICY_ID,
        intake_policy_fingerprint=DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint,
        pack_policy_id=SQUASHFS_PACK_POLICY_ID,
        pack_policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
        toolchain=fixture.toolchain,
    )
    return OCIWorkerRequest(
        nonce=str(uuid.uuid4()),
        config_root=fixture.roots.config,
        state_root=fixture.roots.state,
        expected_store_id=fixture.store.identity,
        source_cas_root=fixture.source_cas_root,
        expected_source_cas_id=fixture.cas.identity,
        source=fixture.image.image.source,
        occurrence=occurrence,
        key=key,
        key_digest=key.digest,
        packer_path=fixture.packer,
        packer_sha256=fixture.toolchain.identity.executable_digest.removeprefix("sha256:"),
        cpu_limit_seconds=30,
    )


def _minimal_squashfs() -> bytes:
    payload = b"payload" + b"\0" * 57
    bytes_used = 96 + len(payload)
    image_size = ((bytes_used + 511) // 512) * 512
    superblock = struct.pack(
        "<5I6H8Q",
        0x73717368,
        1,
        0,
        131072,
        0,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        bytes_used,
        144,
        2**64 - 1,
        96,
        112,
        2**64 - 1,
        2**64 - 1,
    )
    return superblock + payload + b"\0" * (image_size - bytes_used)


def test_request_and_success_response_round_trip_as_canonical_json() -> None:
    request = _request()
    response = OCIWorkerResponse(request.nonce, request.digest, "succeeded", _result(request), None)

    assert OCIWorkerRequest.from_json_bytes(request.to_json_bytes()) == request
    assert OCIWorkerResponse.from_json_bytes(response.to_json_bytes()) == response
    assert response.schema == OCI_WORKER_RESPONSE_SCHEMA
    assert response.to_dict()["failure_stage"] is None
    assert response.to_dict()["failure_errno"] is None
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


def test_v3_failed_response_exposes_only_allowlisted_fixed_resource_facts() -> None:
    request = _request()
    response = OCIWorkerResponse(
        request.nonce,
        request.digest,
        "failed",
        None,
        "resource",
        failure_stage="packer-final-spawn",
        failure_errno=errno.EAGAIN,
    )

    assert OCIWorkerResponse.from_json_bytes(response.to_json_bytes()) == response
    assert "/" not in response.to_json_bytes().decode()
    with pytest.raises(OCIWorkerProtocolError):
        replace(response, error_category="/tmp/private-detail")


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_legacy_v2_response_round_trips_its_original_canonical_shape(status: str) -> None:
    request = _request()
    response = OCIWorkerResponse(
        request.nonce,
        request.digest,
        status,
        _result(request) if status == "succeeded" else None,
        None if status == "succeeded" else "resource",
        schema=OCI_WORKER_LEGACY_RESPONSE_SCHEMA,
    )
    payload = response.to_json_bytes()

    assert b'"failure_errno"' not in payload and b'"failure_stage"' not in payload
    decoded = OCIWorkerResponse.from_json_bytes(payload)
    assert decoded == response
    assert decoded.to_json_bytes() == payload
    assert set(decoded.to_dict()) == {"error_category", "nonce", "request_digest", "result", "schema", "status"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_stage", "unknown-stage"),
        ("failure_stage", True),
        ("failure_stage", {}),
        ("failure_errno", True),
        ("failure_errno", errno.EACCES),
        ("failure_errno", {}),
    ],
)
def test_v3_response_rejects_unknown_boolean_and_object_failure_facts(field, value) -> None:
    request = _request()
    response = OCIWorkerResponse(
        request.nonce,
        request.digest,
        "failed",
        None,
        "resource",
        failure_stage="source",
    )
    payload = response.to_dict()
    payload[field] = value

    with pytest.raises(OCIWorkerProtocolError):
        OCIWorkerResponse.from_json_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_stage", []),
        ("failure_stage", {}),
        ("failure_errno", []),
        ("failure_errno", {}),
    ],
)
def test_response_constructor_rejects_unhashable_failure_facts_with_path_free_typed_error(field, value) -> None:
    arguments = {"failure_stage": "source", "failure_errno": None}
    arguments[field] = value

    with pytest.raises(OCIWorkerProtocolError) as raised:
        OCIWorkerResponse(
            _request().nonce,
            _digest("9"),
            "failed",
            None,
            "resource",
            **arguments,
        )

    assert "/" not in str(raised.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=None),
        lambda value: value.update(schema="palimpsest.oci-materialize-worker-response.v1"),
        lambda value: value.update(schema="palimpsest.oci-materialize-worker-response.v99"),
        lambda value: value.update(schema={}),
        lambda value: value.update(error_category="pack", failure_stage="packing"),
        lambda value: value.update(failure_stage=None, failure_errno=errno.ENOMEM),
    ],
)
def test_response_rejects_extra_unknown_schema_and_wrong_fact_combinations(mutate) -> None:
    request = _request()
    value = OCIWorkerResponse(request.nonce, request.digest, "failed", None, "resource").to_dict()
    mutate(value)

    with pytest.raises(OCIWorkerProtocolError):
        OCIWorkerResponse.from_json_bytes(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )


def test_materialize_layer_hard_rejects_valid_success_with_resource_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _materialization_fixture(tmp_path)
    monkeypatch.setattr(materializer.sys, "platform", "linux")

    def exchange(request: OCIWorkerRequest, **_kwargs):
        value = OCIWorkerResponse(request.nonce, request.digest, "succeeded", _result(request), None).to_dict()
        value["failure_stage"] = "source"
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return payload, 0, SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(materializer, "_spawn_and_exchange", exchange)
    with pytest.raises(OCIHardWorkerError) as raised:
        materializer.materialize_layer_hard(
            fixture.image,
            0,
            source_cas_root=fixture.source_cas_root,
            roots=fixture.roots,
            store=fixture.store,
            packer_path=fixture.packer,
            toolchain=fixture.toolchain,
        )

    assert raised.value.code == "oci-worker-protocol"
    assert "response is invalid" in str(raised.value)


def test_response_rejects_noncanonical_json_and_legacy_shape_extensions() -> None:
    request = _request()
    response = OCIWorkerResponse(request.nonce, request.digest, "failed", None, "resource")
    with pytest.raises(OCIWorkerProtocolError):
        OCIWorkerResponse.from_json_bytes(b" " + response.to_json_bytes())

    legacy = replace(response, schema=OCI_WORKER_LEGACY_RESPONSE_SCHEMA).to_dict()
    legacy["failure_stage"] = None
    with pytest.raises(OCIWorkerProtocolError):
        OCIWorkerResponse.from_json_bytes(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode())


def test_response_request_binding_fields_remain_exact() -> None:
    request = _request()
    response = OCIWorkerResponse(
        request.nonce,
        request.digest,
        "failed",
        None,
        "resource",
        failure_stage="source",
        failure_errno=errno.ENOMEM,
    )
    decoded = OCIWorkerResponse.from_json_bytes(response.to_json_bytes())

    assert decoded.nonce == request.nonce
    assert decoded.request_digest == request.digest
    materializer._validate_worker_response_binding(decoded, request)
    for tampered in (
        replace(decoded, nonce=str(uuid.uuid4())),
        replace(decoded, request_digest=_digest("9")),
    ):
        with pytest.raises(OCIHardWorkerError, match="response binding is invalid"):
            materializer._validate_worker_response_binding(tampered, request)


@pytest.mark.parametrize("tampered_field", ["nonce", "request_digest"])
def test_materialize_layer_hard_rejects_tampered_detailed_failure_before_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered_field: str,
) -> None:
    fixture = _materialization_fixture(tmp_path)
    monkeypatch.setattr(materializer.sys, "platform", "linux")

    def exchange(request: OCIWorkerRequest, **_kwargs):
        response = OCIWorkerResponse(
            nonce=str(uuid.uuid4()) if tampered_field == "nonce" else request.nonce,
            request_digest=_digest("9") if tampered_field == "request_digest" else request.digest,
            status="failed",
            result=None,
            error_category="resource",
            failure_stage="packer-dependency-inspection",
            failure_errno=errno.ENOMEM,
        )
        return response.to_json_bytes(), 0, SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(materializer, "_spawn_and_exchange", exchange)
    with pytest.raises(OCIHardWorkerError) as raised:
        materializer.materialize_layer_hard(
            fixture.image,
            0,
            source_cas_root=fixture.source_cas_root,
            roots=fixture.roots,
            store=fixture.store,
            packer_path=fixture.packer,
            toolchain=fixture.toolchain,
        )

    assert raised.value.code == "oci-worker-protocol"
    assert "response binding is invalid" in str(raised.value)
    assert "dependency inspection" not in str(raised.value)
    assert "ENOMEM" not in str(raised.value)


def test_materialize_layer_hard_accepts_bound_detailed_failure_and_renders_fixed_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _materialization_fixture(tmp_path)
    monkeypatch.setattr(materializer.sys, "platform", "linux")

    def exchange(request: OCIWorkerRequest, **_kwargs):
        response = OCIWorkerResponse(
            nonce=request.nonce,
            request_digest=request.digest,
            status="failed",
            result=None,
            error_category="resource",
            failure_stage="packer-dependency-inspection",
            failure_errno=errno.ENOMEM,
        )
        return response.to_json_bytes(), 0, SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(materializer, "_spawn_and_exchange", exchange)
    with pytest.raises(OCIHardWorkerError) as raised:
        materializer.materialize_layer_hard(
            fixture.image,
            0,
            source_cas_root=fixture.source_cas_root,
            roots=fixture.roots,
            store=fixture.store,
            packer_path=fixture.packer,
            toolchain=fixture.toolchain,
        )

    assert raised.value.code == "oci-worker-resource"
    assert "dependency inspection" in str(raised.value)
    assert "ENOMEM" in str(raised.value)


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


@pytest.mark.parametrize(
    ("failure", "expected_errno"),
    [
        (OSError(errno.EAGAIN, "/private/eagain-secret"), errno.EAGAIN),
        (OSError(errno.ENOMEM, "/private/enomem-secret"), errno.ENOMEM),
        (MemoryError("private-memory-secret"), None),
    ],
)
def test_worker_operation_boundary_preserves_only_actual_resource_errno(failure, expected_errno):
    def fail():
        raise failure

    with pytest.raises(worker._WorkerResourceError) as raised:
        worker._resource_boundary("staging", fail)
    assert worker._failure_details(raised.value, "resource") == ("staging", expected_errno)
    assert "private" not in str(raised.value)


def test_worker_operation_boundary_does_not_reclassify_nonresource_oserror() -> None:
    failure = OSError(errno.EACCES, "/private/access-secret")

    def fail():
        raise failure

    with pytest.raises(OSError) as raised:
        worker._resource_boundary("source", fail)
    assert raised.value is failure


@pytest.mark.parametrize(
    ("failure", "category", "stage", "expected_errno"),
    [
        (OSError(errno.EAGAIN, "/private/limit-secret"), "resource", "limits", errno.EAGAIN),
        (MemoryError("private-limit-secret"), "resource", "limits", None),
        (OSError(errno.EACCES, "/private/limit-secret"), "internal", None, None),
    ],
)
def test_worker_main_writes_v3_fixed_shape_without_exception_text(
    monkeypatch, failure, category, stage, expected_errno
) -> None:
    request = _request()
    output = io.BytesIO()

    def fail(_seconds):
        raise failure

    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request.to_json_bytes())))
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(worker.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(worker, "_apply_resource_limits", fail)
    monkeypatch.setattr(worker, "_materialize", lambda _request: pytest.fail("materialization started"))

    assert worker.main() == 0
    response = OCIWorkerResponse.from_json_bytes(output.getvalue())
    assert response.schema == OCI_WORKER_RESPONSE_SCHEMA
    assert response.error_category == category
    assert response.failure_stage == stage
    assert response.failure_errno == expected_errno
    assert b"private" not in output.getvalue()


@pytest.mark.parametrize("failure_boundary", ["publication", "producer-teardown"])
def test_real_store_publication_and_producer_teardown_memoryerror_remain_coarse_store_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    fixture = _materialization_fixture(tmp_path)
    request = _fixture_worker_request(fixture)
    worker_scratch = tmp_path / "worker-scratch"
    worker_scratch.mkdir(mode=0o700)
    monkeypatch.chdir(worker_scratch)
    monkeypatch.setattr(worker, "discover_squashfs_toolchain", lambda *_args, **_kwargs: fixture.toolchain)

    captured: dict[str, object] = {}
    original_lease_layer = worker.SourceCAS.lease_layer
    original_stage_layer = worker.stage_layer
    original_verify_structure = oci_store.ArtifactStore._verify_structure

    @contextmanager
    def observed_lease_layer(source_cas, *args, **kwargs):
        with original_lease_layer(source_cas, *args, **kwargs) as source:
            captured["source_fd"] = source._file_fd
            yield source

    @contextmanager
    def observed_stage_layer(*args, **kwargs):
        with original_stage_layer(*args, **kwargs) as staged:
            captured["staged"] = staged
            captured["staged_fd"] = staged._spool.fileno()
            yield staged

    class DescriptorBackedPackedFile:
        def __init__(self, payload: bytes, *, fail_on_close: bool) -> None:
            self._file = tempfile.TemporaryFile(mode="w+b")
            self._file.write(payload)
            self._file.seek(0)
            self._fail_on_close = fail_on_close

        def fileno(self) -> int:
            return self._file.fileno()

        def seek(self, *args):
            return self._file.seek(*args)

        def read(self, *args):
            return self._file.read(*args)

        def close(self) -> None:
            self._file.close()
            if self._fail_on_close:
                raise MemoryError("private-teardown-secret")

    @contextmanager
    def packed_fixture(staged, **_kwargs):
        image = _minimal_squashfs()
        identity = fixture.toolchain.identity
        receipt = PackedSquashFSReceipt(
            policy_id=SQUASHFS_PACK_POLICY_ID,
            policy_fingerprint=DEFAULT_SQUASHFS_PACK_POLICY.fingerprint,
            source_ordinal=staged.receipt.ordinal,
            source_diff_id=staged.receipt.diff_id,
            normalized_tar_digest=_digest("8"),
            normalized_tar_size=staged.receipt.uncompressed_size,
            entries=staged.receipt.members,
            packer_version=identity.version,
            packer_sha256=identity.executable_digest.removeprefix("sha256:"),
            image_digest=f"sha256:{hashlib.sha256(image).hexdigest()}",
            image_size=len(image),
            structural_verifier=SQUASHFS_STRUCTURAL_VERIFIER_ID,
            toolchain_fingerprint=identity.fingerprint,
            toolchain_dependency_digests=identity.dependency_digests,
        )
        packed_file = DescriptorBackedPackedFile(image, fail_on_close=failure_boundary == "producer-teardown")
        captured["packed_fd"] = packed_file.fileno()
        packed = LeasedSquashFS(packed_file, receipt)
        captured["packed"] = packed
        try:
            yield packed
        finally:
            close_error = packed._close()
            if close_error is not None:
                raise close_error

    def fail_publication(fd: int, size: int, maximum: int) -> None:
        captured["publication_fd"] = fd
        original_verify_structure(fd, size, maximum)
        if failure_boundary == "publication":
            raise MemoryError("private-publication-secret")
        raise oci_store.ArtifactStoreError("artifact-structure", "injected publication rejection")

    monkeypatch.setattr(worker.SourceCAS, "lease_layer", observed_lease_layer)
    monkeypatch.setattr(worker, "stage_layer", observed_stage_layer)
    monkeypatch.setattr(worker, "pack_staged_squashfs", packed_fixture)
    monkeypatch.setattr(oci_store.ArtifactStore, "_verify_structure", staticmethod(fail_publication))

    with pytest.raises(worker._WorkerResourceError) as raised:
        worker._materialize(request)

    assert raised.value.failure_stage == "store"
    assert raised.value.failure_errno is None
    assert "private" not in str(raised.value)
    assert captured["staged"]._closed
    assert captured["packed"]._closed
    for name in ("source_fd", "staged_fd", "packed_fd", "publication_fd"):
        with pytest.raises(OSError):
            os.fstat(captured[name])

    monkeypatch.setattr(oci_store.ArtifactStore, "_verify_structure", staticmethod(original_verify_structure))
    with fixture.store._authority() as authority:
        assert fixture.store._record_from_index(authority, request.key) is None
    with pytest.raises(oci_store.ArtifactStoreError) as unpublished:
        fixture.store._artifacts.verify_squashfs(
            captured["packed"].receipt.image_digest,
            captured["packed"].receipt.image_size,
            maximum=oci_store.MAX_OCI_STORE_IMAGE_BYTES,
        )
    assert unpublished.value.code == "artifact-missing"


@pytest.mark.parametrize(
    ("stage", "number", "fragment"),
    [
        ("packer-dependency-inspection", errno.EAGAIN, "dependency inspection"),
        ("packer-toolchain-version", errno.ENOMEM, "toolchain version check"),
        ("packer-pinned-version", None, "pinned filesystem-packer version spawn"),
        ("packer-final-spawn", errno.EAGAIN, "final filesystem-packer spawn"),
    ],
)
def test_parent_resource_detail_uses_only_fixed_stage_and_errno_text(stage, number, fragment):
    request = _request()
    response = OCIWorkerResponse(
        request.nonce,
        request.digest,
        "failed",
        None,
        "resource",
        failure_stage=stage,
        failure_errno=number,
    )

    detail = materializer._reported_worker_resource_detail(response)

    assert fragment in detail
    assert (errno.errorcode[number] in detail) if number is not None else "no operating-system errno" in detail
    assert "no automatic retry or limit change" in detail


def test_parent_resource_detail_preserves_generic_no_details_rendering() -> None:
    request = _request()
    response = OCIWorkerResponse(request.nonce, request.digest, "failed", None, "resource")

    detail = materializer._reported_worker_resource_detail(response)

    assert "exact limiting resource is not identified" in detail
    assert "during" not in detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failure_stage", []),
        ("failure_stage", {}),
        ("failure_errno", []),
        ("failure_errno", {}),
    ],
)
def test_materialize_layer_hard_rejects_malformed_decoder_resource_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    fixture = _materialization_fixture(tmp_path)
    monkeypatch.setattr(materializer.sys, "platform", "linux")
    original_decode = materializer.OCIWorkerResponse.from_json_bytes

    def exchange(request: OCIWorkerRequest, **_kwargs):
        response = OCIWorkerResponse(request.nonce, request.digest, "failed", None, "resource")
        return response.to_json_bytes(), 0, SimpleNamespace(poll=lambda: 0)

    def malformed_decode(payload: bytes):
        response = original_decode(payload)
        facts = {"failure_stage": response.failure_stage, "failure_errno": response.failure_errno}
        facts[field] = value
        return SimpleNamespace(
            nonce=response.nonce,
            request_digest=response.request_digest,
            status=response.status,
            result=response.result,
            error_category=response.error_category,
            **facts,
        )

    monkeypatch.setattr(materializer, "_spawn_and_exchange", exchange)
    monkeypatch.setattr(materializer.OCIWorkerResponse, "from_json_bytes", staticmethod(malformed_decode))
    with pytest.raises(OCIHardWorkerError) as raised:
        materializer.materialize_layer_hard(
            fixture.image,
            0,
            source_cas_root=fixture.source_cas_root,
            roots=fixture.roots,
            store=fixture.store,
            packer_path=fixture.packer,
            toolchain=fixture.toolchain,
        )

    assert raised.value.code == "oci-worker-protocol"
    assert "/" not in str(raised.value)


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
