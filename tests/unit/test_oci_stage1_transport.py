"""Portable adversarial tests for the per-run stage-1 raw transport."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError, StateError
from palimpsest_local.kvm import MAX_OCI_ROOT_LAYER_DISKS
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec
from palimpsest_local.oci_stage1 import OCIStage1Plan, oci_stage1_device_serial
from palimpsest_local.oci_stage1_transport import (
    OCIStage1TransportReceipt,
    build_stage1_transport,
    verify_stage1_transport,
    verify_stage1_transport_file,
)

_MAGIC = b"PALIMPSEST-S1\0\0\0"
_HEADER = struct.Struct("<16sIIQ32s")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _plan(*, run_name: str = "transport-demo", layers: int = 2) -> OCIStage1Plan:
    return OCIStage1Plan(
        run_id="f6f546e2-e734-4920-9eff-1762b348a249",
        run_name=run_name,
        boot_plan_digest="sha256:" + "a" * 64,
        domain_core_digest="sha256:" + "b" * 64,
        root={
            "filesystem": "ext4",
            "filesystem_uuid": "1fd7a60e-fdb2-4877-91d3-148bbca3884f",
            "generation": 3,
            "mount_options": ["rw", "nodev", "nosuid"],
            "serial": oci_stage1_device_serial("root", "1fd7a60e-fdb2-4877-91d3-148bbca3884f"),
            "size_bytes": 16 * 1024 * 1024,
            "volume_id": "1fd7a60e-fdb2-4877-91d3-148bbca3884f",
        },
        layers=tuple(
            {
                "filesystem": "squashfs",
                "image_digest": f"sha256:{ordinal + 2:064x}",
                "mount_options": ["ro", "nodev", "nosuid"],
                "occurrence_digest": f"sha256:{ordinal + 100:064x}",
                "ordinal": ordinal,
                "serial": oci_stage1_device_serial("lower", f"sha256:{ordinal + 100:064x}"),
                "size_bytes": 4096 * (ordinal + 1),
            }
            for ordinal in range(layers)
        ),
        process=OCIProcessSpec(
            ("/usr/bin/demo", "--serve"),
            (("LANG", "C.UTF-8"),),
            "/srv",
            OCIUserSpec("1000", "1000"),
            15,
        ),
    )


def _raw_envelope(payload: bytes) -> tuple[bytes, OCIStage1TransportReceipt]:
    header = _HEADER.pack(_MAGIC, 1, _HEADER.size, len(payload), hashlib.sha256(payload).digest())
    size = (len(header) + len(payload) + 4095) // 4096 * 4096
    artifact = header + payload + b"\0" * (size - len(header) - len(payload))
    receipt = OCIStage1TransportReceipt(_digest(artifact), len(artifact), _digest(payload), len(payload))
    return artifact, receipt


def _mutated(payload: bytes, offset: int, replacement: bytes) -> bytes:
    return payload[:offset] + replacement + payload[offset + len(replacement) :]


def test_stage1_transport_is_deterministic_aligned_and_cycle_free() -> None:
    plan = _plan()
    first = build_stage1_transport(plan)
    second = build_stage1_transport(plan)

    assert first == second
    assert len(first.artifact) % 4096 == 0
    assert first.receipt.artifact_size_bytes == len(first.artifact)
    assert (
        verify_stage1_transport(
            first.artifact,
            first.receipt,
            expected_stage1_plan=plan,
        )
        == plan
    )
    encoded = json.dumps(plan.to_dict(), sort_keys=True)
    assert "domain_plan_digest" not in encoded
    assert "domain_core_digest" in encoded
    assert "/Users/" not in encoded and "/tmp/" not in encoded


def test_stage1_transport_is_deterministic_in_a_fresh_process() -> None:
    built = build_stage1_transport(_plan())
    script = """
from palimpsest_local.oci_process import OCIProcessSpec, OCIUserSpec
from palimpsest_local.oci_stage1 import OCIStage1Plan, oci_stage1_device_serial
from palimpsest_local.oci_stage1_transport import build_stage1_transport
plan = OCIStage1Plan(
    run_id="f6f546e2-e734-4920-9eff-1762b348a249", run_name="transport-demo",
    boot_plan_digest="sha256:" + "a" * 64, domain_core_digest="sha256:" + "b" * 64,
    root={"filesystem":"ext4","filesystem_uuid":"1fd7a60e-fdb2-4877-91d3-148bbca3884f",
          "generation":3,"mount_options":["rw","nodev","nosuid"],
          "serial":oci_stage1_device_serial("root","1fd7a60e-fdb2-4877-91d3-148bbca3884f"),"size_bytes":16777216,
          "volume_id":"1fd7a60e-fdb2-4877-91d3-148bbca3884f"},
    layers=tuple({"filesystem":"squashfs","image_digest":f"sha256:{i+2:064x}",
                  "mount_options":["ro","nodev","nosuid"],
                  "occurrence_digest":f"sha256:{i+100:064x}",
                  "ordinal":i,"serial":oci_stage1_device_serial("lower",f"sha256:{i+100:064x}"),
                  "size_bytes":4096*(i+1)} for i in range(2)),
    process=OCIProcessSpec(("/usr/bin/demo","--serve"),(("LANG","C.UTF-8"),),"/srv",
                           OCIUserSpec("1000","1000"),15),
)
built = build_stage1_transport(plan)
print(built.artifact.hex())
"""
    completed = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)

    assert bytes.fromhex(completed.stdout.strip()) == built.artifact


def test_stage1_transport_rejects_header_payload_padding_and_length_mutations() -> None:
    plan = _plan()
    built = build_stage1_transport(plan)
    padding_offset = _HEADER.size + built.receipt.payload_size_bytes
    mutations = (
        _mutated(built.artifact, 0, b"X"),
        _mutated(built.artifact, 16, (2).to_bytes(4, "little")),
        _mutated(built.artifact, 20, (32).to_bytes(4, "little")),
        _mutated(built.artifact, 24, (2**63).to_bytes(8, "little")),
        _mutated(built.artifact, 32, b"\0"),
        _mutated(built.artifact, _HEADER.size, b"X"),
        _mutated(built.artifact, padding_offset, b"\x01"),
        built.artifact[:-1],
        built.artifact + b"\0" * 4096,
    )
    for mutation in mutations:
        with pytest.raises(ArtifactValidationError):
            verify_stage1_transport(
                mutation,
                replace(built.receipt, artifact_digest=_digest(mutation)),
                expected_stage1_plan=plan,
            )


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b'{"schema":1,"schema":2}',
        b'{"value":NaN}',
        b'{"value":1}\n',
        b'{ "value" : 1 }',
    ],
)
def test_stage1_transport_rejects_noncanonical_json(payload: bytes) -> None:
    artifact, receipt = _raw_envelope(payload)
    with pytest.raises(ArtifactValidationError, match="JSON|binding"):
        verify_stage1_transport(artifact, receipt, expected_stage1_plan=_plan())


def test_stage1_transport_rejects_self_consistent_cross_run_replay() -> None:
    expected = _plan()
    replay = build_stage1_transport(_plan(run_name="foreign-run"))

    with pytest.raises(ArtifactValidationError, match="plan binding"):
        verify_stage1_transport(
            replay.artifact,
            replay.receipt,
            expected_stage1_plan=expected,
        )


def test_stage1_transport_receipt_is_exact_and_layer_budget_reserves_plan_disk() -> None:
    receipt = build_stage1_transport(_plan()).receipt
    assert OCIStage1TransportReceipt.from_dict(receipt.to_dict()) == receipt
    assert len(build_stage1_transport(_plan(layers=MAX_OCI_ROOT_LAYER_DISKS)).stage1_plan.layers) == 24

    value = receipt.to_dict()
    value["unknown"] = True
    with pytest.raises(ArtifactValidationError, match="fields"):
        OCIStage1TransportReceipt.from_dict(value)
    with pytest.raises(StateError, match="lower contract"):
        _plan(layers=MAX_OCI_ROOT_LAYER_DISKS + 1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root, _layers: root.__setitem__("size_bytes", True),
        lambda root, _layers: root.__setitem__("size_bytes", 16 * 1024 * 1024 - 512),
        lambda root, _layers: root.__setitem__("serial", "f" * 20),
        lambda _root, layers: layers[0].__setitem__("size_bytes", True),
        lambda _root, layers: layers[0].__setitem__("size_bytes", 513),
        lambda _root, layers: layers[0].__setitem__("size_bytes", 32 * 1024**3 + 512),
        lambda _root, layers: layers[0].__setitem__("occurrence_digest", "sha256:" + "f" * 64),
        lambda _root, layers: layers[0].__setitem__("ordinal", 1),
    ],
)
def test_stage1_plan_rejects_block_identity_size_and_ordinal_drift(mutate: object) -> None:
    plan = _plan()
    root = dict(plan.root)
    layers = [dict(layer) for layer in plan.layers]
    mutate(root, layers)  # type: ignore[operator]

    with pytest.raises(StateError, match="root mount policy|lower mount policy"):
        replace(plan, root=root, layers=tuple(layers))


@pytest.mark.parametrize("path", ["/.", "/.."])
def test_stage1_plan_rejects_probe_paths_rejected_by_guest_parser(path: str) -> None:
    plan = _plan()
    probe = {
        "digest": "sha256:" + "c" * 64,
        "path": path,
        "size_bytes": 1,
        "top_ordinal": 1,
    }

    with pytest.raises(StateError, match="assembly probe policy"):
        replace(plan, assembly_probes=(probe,))


def test_stage1_transport_covers_worst_case_canonical_process_escaping() -> None:
    escaped_argument = "\x01" * (32 * 1024)
    process = OCIProcessSpec(
        ("/bin/demo", *(escaped_argument for _ in range(7))),
        (),
        "/",
        OCIUserSpec("0", "0"),
        15,
    )
    plan = replace(_plan(), process=process)

    built = build_stage1_transport(plan)

    assert built.receipt.payload_size_bytes > 1024 * 1024
    assert (
        verify_stage1_transport(
            built.artifact,
            built.receipt,
            expected_stage1_plan=plan,
        )
        == plan
    )


def test_stage1_transport_file_boundary_rejects_tamper_links_modes_and_fifo(tmp_path: Path) -> None:
    plan = _plan()
    built = build_stage1_transport(plan)
    path = tmp_path / "stage1-plan.raw"
    path.write_bytes(built.artifact)
    path.chmod(0o400)

    verified = verify_stage1_transport_file(path.resolve(), built.receipt, expected_stage1_plan=plan)
    assert verified.plan == plan

    link = tmp_path / "linked.raw"
    link.symlink_to(path)
    with pytest.raises(StateError, match="securely read"):
        verify_stage1_transport_file(link.absolute(), built.receipt, expected_stage1_plan=plan)

    path.chmod(0o600)
    with pytest.raises(StateError, match="metadata"):
        verify_stage1_transport_file(path.resolve(), built.receipt, expected_stage1_plan=plan)
    path.chmod(0o400)

    hardlink = tmp_path / "hardlinked.raw"
    hardlink.hardlink_to(path)
    with pytest.raises(StateError, match="metadata"):
        verify_stage1_transport_file(path.resolve(), built.receipt, expected_stage1_plan=plan)
    hardlink.unlink()

    fifo = tmp_path / "blocking.fifo"
    os.mkfifo(fifo)
    with pytest.raises(StateError, match="metadata"):
        verify_stage1_transport_file(fifo.resolve(), built.receipt, expected_stage1_plan=plan)

    changed = bytearray(built.artifact)
    changed[_HEADER.size] ^= 1
    path.chmod(0o600)
    path.write_bytes(changed)
    path.chmod(0o400)
    with pytest.raises(StateError, match="structure"):
        verify_stage1_transport_file(path.resolve(), built.receipt, expected_stage1_plan=plan)
