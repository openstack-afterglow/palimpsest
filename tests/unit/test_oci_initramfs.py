"""Portable deterministic initramfs and first-party bootstrap tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError, StateError
from palimpsest_local.oci_initramfs import (
    MAX_OCI_INITRAMFS_ENTRIES,
    OCI_STAGE1_BINARY_DIGEST,
    OCI_STAGE1_BUILD_RECIPE_DIGEST,
    OCI_STAGE1_SEAL_RECIPE_DIGEST,
    OCI_STAGE1_SOURCE_DIGEST,
    InitramfsEntryReceipt,
    NewcEntry,
    OCIInitramfsManifest,
    build_bootstrap_initramfs,
    build_newc,
    parse_newc,
    verify_bootstrap_initramfs,
    verify_static_x86_64_elf,
)
from palimpsest_local.oci_root_kvm import verify_first_party_bootstrap_initramfs


def _independent_newc(payload: bytes) -> list[dict[str, int | str | bytes]]:
    """Small test oracle intentionally independent from the production parser."""

    records: list[dict[str, int | str | bytes]] = []
    offset = 0
    while offset < len(payload):
        start = offset
        assert payload[offset : offset + 6] == b"070701"
        fields = [int(payload[offset + 6 + index * 8 : offset + 14 + index * 8], 16) for index in range(13)]
        offset += 110
        namesize = fields[11]
        name = payload[offset : offset + namesize]
        assert name[-1:] == b"\0"
        offset += namesize
        name_padding = (-(110 + namesize)) % 4
        assert payload[offset : offset + name_padding] == b"\0" * name_padding
        offset += name_padding
        data = payload[offset : offset + fields[6]]
        offset += fields[6]
        data_padding = (-fields[6]) % 4
        assert payload[offset : offset + data_padding] == b"\0" * data_padding
        offset += data_padding
        records.append(
            {
                "data": data,
                "data_padding": data_padding,
                "end": offset,
                "mode": fields[1],
                "name": name[:-1].decode("ascii"),
                "name_padding": name_padding,
                "start": start,
            }
        )
        if name == b"TRAILER!!!\0":
            break
    assert offset == len(payload)
    return records


def test_bootstrap_initramfs_is_byte_deterministic_and_independently_well_formed() -> None:
    first = build_bootstrap_initramfs()
    second = build_bootstrap_initramfs()
    records = _independent_newc(first.payload)

    assert first == second
    assert [record["name"] for record in records] == [
        "dev",
        "etc",
        "etc/palimpsest",
        "etc/palimpsest/guest-stage1-consumer.json",
        "etc/palimpsest/stage1-abi.json",
        "init",
        "proc",
        "sys",
        "TRAILER!!!",
    ]
    assert [record["mode"] for record in records[:-1]] == [
        stat.S_IFDIR | 0o755,
        stat.S_IFDIR | 0o755,
        stat.S_IFDIR | 0o755,
        stat.S_IFREG | 0o644,
        stat.S_IFREG | 0o644,
        stat.S_IFREG | 0o755,
        stat.S_IFDIR | 0o755,
        stat.S_IFDIR | 0o755,
    ]
    assert records[-1]["end"] == len(first.payload)
    assert verify_bootstrap_initramfs(first.payload, first.manifest) == parse_newc(first.payload)


def test_bootstrap_initramfs_is_byte_deterministic_across_fresh_processes() -> None:
    built = build_bootstrap_initramfs()
    script = """
import json
from palimpsest_local.oci_initramfs import build_bootstrap_initramfs
built = build_bootstrap_initramfs()
print(json.dumps({"manifest": built.manifest.to_dict(), "payload": built.payload.hex()}, sort_keys=True))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    fresh = json.loads(completed.stdout)
    assert bytes.fromhex(fresh["payload"]) == built.payload
    assert fresh["manifest"] == built.manifest.to_dict()


def test_bootstrap_manifest_is_canonical_path_free_and_explicitly_not_root_assembly() -> None:
    built = build_bootstrap_initramfs()
    value = built.manifest.to_dict()
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))

    assert OCIInitramfsManifest.from_dict(value) == built.manifest
    assert value["stage1"]["capability"] == "pre-mount-filesystem-set-consumer-fail-closed"
    assert value["stage1"]["plan_transport"] == "virtio-blk-raw-envelope-4k.v1"
    assert value["stage1"]["embedded_consumer"] is True
    assert value["stage1"]["consumer_contract"] == "palimpsest.guest-stage1-consumer.x86_64.v3"
    assert value["stage1"]["root_assembly"] is False
    assert value["stage1"]["linkage"] == "static"
    assert "/Users/" not in rendered and "/tmp/" not in rendered
    assert "run_id" not in rendered and "domain_plan" not in rendered
    entries = {entry.path: entry.data for entry in parse_newc(built.payload)}
    consumer = json.loads(entries["etc/palimpsest/guest-stage1-consumer.json"])
    abi = json.loads(entries["etc/palimpsest/stage1-abi.json"])
    assert consumer["embedded_in_init"] is True
    assert consumer["plan_transport"] == "virtio-blk-raw-envelope-4k.v1"
    assert abi["embedded_consumer"] is True
    assert abi["consumer_contract_digest"] == built.manifest.consumer_contract_digest
    assert value["stage1"]["build"]["toolchain_image"].endswith(
        "@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"
    )


def test_packaged_stage1_binary_and_reproducible_build_inputs_match_provenance() -> None:
    repository = Path(__file__).resolve().parents[2]
    paths = {
        OCI_STAGE1_SOURCE_DIGEST: repository / "guest/stage1/init.c",
        OCI_STAGE1_BUILD_RECIPE_DIGEST: repository / "scripts/build_oci_guest_init.sh",
        OCI_STAGE1_SEAL_RECIPE_DIGEST: repository / "scripts/seal_static_elf.py",
        OCI_STAGE1_BINARY_DIGEST: repository / "src/palimpsest_local/assets/oci-stage1-init.x86_64",
    }
    for expected, path in paths.items():
        assert f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}" == expected
    assert stat.S_IMODE(paths[OCI_STAGE1_BINARY_DIGEST].stat().st_mode) == 0o644


@pytest.mark.parametrize(
    "entries,match",
    [
        (
            [
                NewcEntry("init", stat.S_IFREG | 0o755, b"x"),
                NewcEntry("etc", stat.S_IFDIR | 0o755, b""),
            ],
            "ordered",
        ),
        ([NewcEntry("etc/value", stat.S_IFREG | 0o644, b"x")], "parent"),
    ],
)
def test_newc_builder_rejects_ambiguous_layout(entries: list[NewcEntry], match: str) -> None:
    with pytest.raises(ArtifactValidationError, match=match):
        build_newc(entries)


@pytest.mark.parametrize("path", ["../init", "/init", "etc//value", "etc\\value", ".", "\ud800"])
def test_newc_entry_rejects_noncanonical_paths(path: str) -> None:
    with pytest.raises(ArtifactValidationError, match="path"):
        NewcEntry(path, stat.S_IFREG | 0o755, b"x")


def test_newc_builder_enforces_entry_bound() -> None:
    entries = [NewcEntry(f"f{index:02d}", stat.S_IFREG | 0o644, b"") for index in range(MAX_OCI_INITRAMFS_ENTRIES + 1)]
    with pytest.raises(ArtifactValidationError, match="entries"):
        build_newc(entries)


def _mutated(payload: bytes, offset: int, replacement: bytes) -> bytes:
    return payload[:offset] + replacement + payload[offset + len(replacement) :]


def test_newc_parser_rejects_header_metadata_padding_path_and_trailing_mutations() -> None:
    payload = build_bootstrap_initramfs().payload
    records = _independent_newc(payload)
    first = records[0]
    first_padding = int(first["start"]) + 110 + len(str(first["name"]).encode()) + 1
    trailer_start = int(records[-1]["start"])
    mutations = (
        _mutated(payload, 0, b"070702"),
        _mutated(payload, 6, b"0000000A"),
        _mutated(payload, 22, b"00000001"),
        _mutated(payload, 110, b"../"),
        _mutated(payload, first_padding, b"\x01"),
        payload[:trailer_start],
        payload[:-1],
        payload + b"\0",
    )
    for mutation in mutations:
        with pytest.raises(ArtifactValidationError):
            parse_newc(mutation)


def test_static_stage1_elf_policy_rejects_dynamic_wrong_machine_wx_and_bad_entry() -> None:
    stage1 = {entry.path: entry.data for entry in parse_newc(build_bootstrap_initramfs().payload)}["init"]
    verify_static_x86_64_elf(stage1)

    mutations = (
        _mutated(stage1, 16, (3).to_bytes(2, "little")),
        _mutated(stage1, 18, (183).to_bytes(2, "little")),
        _mutated(stage1, 24, (0).to_bytes(8, "little")),
        _mutated(stage1, 56, (1).to_bytes(2, "little")),
        _mutated(stage1, 64, (3).to_bytes(4, "little")),
        _mutated(stage1, 68, (7).to_bytes(4, "little")),
        _mutated(stage1, 104, (2**64 - 1).to_bytes(8, "little")),
        stage1[:63],
    )
    for mutation in mutations:
        with pytest.raises(ArtifactValidationError):
            verify_static_x86_64_elf(mutation)


def test_manifest_and_artifact_tampering_fail_closed() -> None:
    built = build_bootstrap_initramfs()
    changed = bytearray(built.payload)
    changed[-20] ^= 1
    with pytest.raises(ArtifactValidationError, match="manifest"):
        verify_bootstrap_initramfs(bytes(changed), built.manifest)

    value = deepcopy(built.manifest.to_dict())
    value["stage1"]["root_assembly"] = True
    with pytest.raises(ArtifactValidationError, match="policy"):
        OCIInitramfsManifest.from_dict(value)

    with pytest.raises(ArtifactValidationError, match="entries"):
        replace(built.manifest, entries=(object(),))

    oversized_entries = deepcopy(built.manifest.to_dict())
    oversized_entries["entries"].append(deepcopy(oversized_entries["entries"][-1]))
    with pytest.raises(ArtifactValidationError, match="policy"):
        OCIInitramfsManifest.from_dict(oversized_entries)

    entries = list(parse_newc(built.payload))
    stage1_index = next(index for index, entry in enumerate(entries) if entry.path == "init")
    changed_stage1 = bytearray(entries[stage1_index].data)
    changed_stage1[-2] ^= 1
    entries[stage1_index] = NewcEntry("init", stat.S_IFREG | 0o755, bytes(changed_stage1))
    forged_payload = build_newc(entries)
    forged_receipts = tuple(
        InitramfsEntryReceipt(
            entry.path,
            entry.mode,
            len(entry.data),
            f"sha256:{hashlib.sha256(entry.data).hexdigest()}",
        )
        for entry in entries
    )
    forged_manifest = replace(
        built.manifest,
        artifact_digest=f"sha256:{hashlib.sha256(forged_payload).hexdigest()}",
        artifact_size_bytes=len(forged_payload),
        entries=forged_receipts,
        stage1_binary_digest=f"sha256:{hashlib.sha256(changed_stage1).hexdigest()}",
    )
    with pytest.raises(ArtifactValidationError, match="first-party"):
        verify_bootstrap_initramfs(forged_payload, forged_manifest)

    non_executable_entries = list(parse_newc(built.payload))
    non_executable_entries[stage1_index] = NewcEntry("init", stat.S_IFREG | 0o644, entries[stage1_index].data)
    non_executable_payload = build_newc(non_executable_entries)
    with pytest.raises(ArtifactValidationError, match="entries"):
        replace(
            built.manifest,
            artifact_digest=f"sha256:{hashlib.sha256(non_executable_payload).hexdigest()}",
            artifact_size_bytes=len(non_executable_payload),
            entries=tuple(
                InitramfsEntryReceipt(
                    entry.path,
                    entry.mode,
                    len(entry.data),
                    f"sha256:{hashlib.sha256(entry.data).hexdigest()}",
                )
                for entry in non_executable_entries
            ),
        )

    value = deepcopy(built.manifest.to_dict())
    value["archive"]["extra"] = True
    with pytest.raises(ArtifactValidationError, match="policy"):
        OCIInitramfsManifest.from_dict(value)


@pytest.mark.parametrize("payload", [None, "not-bytes"])
def test_bootstrap_verifier_rejects_non_bytes_with_typed_error(payload: object) -> None:
    with pytest.raises(ArtifactValidationError, match="archive bytes"):
        verify_bootstrap_initramfs(payload, build_bootstrap_initramfs().manifest)  # type: ignore[arg-type]


def test_host_boundary_pins_and_verifies_first_party_archive(tmp_path: Path) -> None:
    built = build_bootstrap_initramfs()
    path = tmp_path / "palimpsest-bootstrap.initramfs"
    path.write_bytes(built.payload)

    verified = verify_first_party_bootstrap_initramfs(path.resolve(), built.manifest)

    assert verified.digest == built.manifest.artifact_digest
    assert verified.size_bytes == len(built.payload)

    junk = tmp_path / "junk.initramfs"
    junk_payload = b"070701payload"
    junk.write_bytes(junk_payload)
    forged_manifest = replace(
        built.manifest,
        artifact_digest=f"sha256:{hashlib.sha256(junk_payload).hexdigest()}",
        artifact_size_bytes=len(junk_payload),
    )
    with pytest.raises(StateError):
        verify_first_party_bootstrap_initramfs(junk.resolve(), forged_manifest)

    link = tmp_path / "linked.initramfs"
    link.symlink_to(path)
    with pytest.raises(StateError):
        verify_first_party_bootstrap_initramfs(link.absolute(), built.manifest)

    fifo = tmp_path / "blocking.fifo"
    os.mkfifo(fifo)
    with pytest.raises(StateError, match="metadata"):
        verify_first_party_bootstrap_initramfs(fifo.resolve(), built.manifest)
