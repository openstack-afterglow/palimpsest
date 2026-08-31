"""Canonical wire and process-boundary tests for the OCI hard worker."""

from __future__ import annotations

import json
import os
import signal
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

import palimpsest_local.oci_materializer as materializer
from palimpsest_local.oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID
from palimpsest_local.oci_materializer import OCIHardWorkerError
from palimpsest_local.oci_packer import (
    DEFAULT_SQUASHFS_PACK_POLICY,
    SQUASHFS_PACK_POLICY_ID,
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
