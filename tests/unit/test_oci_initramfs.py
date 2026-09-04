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
        "run",
        "run/palimpsest",
        "run/palimpsest/lowers",
        "run/palimpsest/merged",
        "run/palimpsest/root",
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
        stat.S_IFDIR | 0o755,
        stat.S_IFDIR | 0o755,
        stat.S_IFDIR | 0o755,
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


def test_bootstrap_manifest_is_canonical_path_free_switch_root_checkpoint() -> None:
    built = build_bootstrap_initramfs()
    value = built.manifest.to_dict()
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))

    assert OCIInitramfsManifest.from_dict(value) == built.manifest
    assert value["stage1"]["capability"] == ("authenticated-overlay-switch-root-pid1-supervisor-workload-isolation")
    assert value["stage1"]["plan_transport"] == "virtio-blk-raw-envelope-4k.v1"
    assert value["stage1"]["embedded_consumer"] is True
    assert value["stage1"]["consumer_contract"] == "palimpsest.guest-stage1-consumer.x86_64.v15"
    assert value["stage1"]["root_assembly"] is True
    assert value["stage1"]["root_is_slash"] is True
    assert value["stage1"]["pivot_root"] is False
    assert value["stage1"]["switch_root"] is True
    assert value["stage1"]["root_transition"] == {
        "contract": "palimpsest.stage1-root-transition.v1",
        "method": "move-mount-chroot",
        "pid1_root_matches_slash": True,
        "pivot_root": False,
        "pseudo_filesystems": ["dev", "sys", "proc"],
        "root_filesystem": "overlay",
        "switch_root": True,
        "workload_started": False,
    }
    assert value["stage1"]["workload_started"] is True
    assert value["stage1"]["supervisor"] == {
        "cgroup": "fd-pinned-cgroup-v2-palimpsest.workload",
        "cgroup_security": "private-readonly-view-plus-dedicated-cleanup-authority",
        "cleanup_scope": "dedicated-workload-cgroup",
        "contract": "palimpsest.guest-pid1-supervisor.v8",
        "credential_transition": "child-isolate-drop-verify-parent-attach-key-bootstrap-ack-release",
        "credentials": "root-pid1-broker-image-root-passwd-group-resolution-empty-supplementary-groups",
        "environment": "authenticated-image-environment-with-fixed-container-default-path",
        "execution": "shell-free-path-search-fork-isolation-ready-cgroup-attach-release-gate-execve-cloexec-error-pipe",
        "isolation": {
            "capabilities": "empty-bounding-ambient-permitted-effective-inheritable",
            "contract": "palimpsest.workload-lifecycle-authority-isolation.v2",
            "devices": ["full", "null", "random", "tty", "urandom", "zero"],
            "lifecycle_fd": "child-closed-before-isolation-ready",
            "lifecycle_key": "pid1-generated-post-fork-post-isolation-never-inherited",
            "mounts": "private-dev-tmpfs-masked-virtio-ports-readonly-proc-sys-cgroup",
            "pid1_proc": "nondumpable-before-fork",
            "seccomp": "authority-escape-boundary-filter",
        },
        "lifecycle_broker": "palimpsest.guest-lifecycle-broker.v3",
        "lifecycle_delivery": "authenticated-v2-bootstrap-boundary-ack-reconnect-snapshot-same-id-stop-retry",
        "privilege_after_fork": "root-pid1-narrow-broker-capabilityless-workload",
        "signal_transport": "blocked-signalfd-process-group-forwarding",
        "production_cleanup": "stop-signal-grace-cgroup.kill-wait4-echild-populated-zero-rmdir",
        "terminal_state": "parent-marker-then-fail-closed-wait",
        "wait": "wait4-to-echild-with-empty-cgroup-proof",
    }
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
    assert consumer["supervisor"] == value["stage1"]["supervisor"]
    assert abi["supervisor"] == value["stage1"]["supervisor"]
    assert consumer["root_transition"] == value["stage1"]["root_transition"]
    assert abi["root_transition"] == value["stage1"]["root_transition"]
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
    stage1 = paths[OCI_STAGE1_BINARY_DIGEST].read_bytes()
    assert b"workload terminal; main_status=" in stage1
    assert b"; cooperative_status=" in stage1
    assert b"; forced_status=" in stage1
    assert b"; reaped=" in stage1
    assert b"; forwarded=" in stage1
    assert b"; pid1_uid=" in stage1
    assert b"; pid1_gid=" in stage1
    assert b"; pid1_groups=" in stage1
    assert b"; cleanup=cgroup.kill; cgroup_populated=0" in stage1
    assert b"org.palimpsest.oci.lifecycle.0" in stage1
    assert b"palimpsest.oci-lifecycle-control.v2" in stage1
    assert b"lifecycle rejected; stage=" in stage1


def test_post_fork_launch_failures_prioritize_cleanup_uncertainty() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "guest" / "stage1" / "init.c").read_text()
    packaged = (repository / "src" / "palimpsest_local" / "assets" / "oci-stage1-init.x86_64").read_bytes()

    assert source.count("return n ? 0 : -1;") == 4
    assert "sc2(SYS_kill, main_pid, SIGKILL)" not in source
    assert "SYS_kill, -1" not in source
    assert b"kill(-1" not in packaged
    assert b"whole-guest" not in packaged
    assert 'exact_string(&j, "palimpsest.guest-stage1.v13")' in source
    assert 'exact_string(&j, "first-party-pid1-supervisor.v7")' in source
    assert 'SYS_open, (i64)"/etc"' in source
    assert 'read_account_database("passwd"' in source
    assert 'read_account_database("group"' in source
    assert "O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY" in source
    assert "O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW" in source
    assert "return path_seen &&" in source
    assert "static i64 exec_workload(struct guest_process *process)" in source
    assert "operation = exec_workload(process);" in source
    assert "SYS_execve" in source
    assert b"/bin/sh\0" not in packaged
    assert 'exact_string(&j, "palimpsest.workload-lifecycle-authority-isolation.v2")' in source
    assert '"org.palimpsest.oci.lifecycle.0"' in source
    assert '"palimpsest.oci-lifecycle-control.v2"' in source
    assert "BPF_JMP | BPF_JSET | BPF_K, X32_SYSCALL_BIT" in source
    assert "X32_SYSCALL_BIT 0x40000000U" in source
    assert 'verify_mountinfo("/proc", "proc", 1' in source
    assert 'verify_mountinfo("/proc/1/fd", "tmpfs", 1' in source
    assert 'verify_mountinfo("/proc/1/fdinfo", "tmpfs", 1' in source
    assert "sc2(SYS_chmod, (i64)path, 0666)" in source
    assert "MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC" in source
    assert 'SYS_mount, (i64)staging, (i64)"/sys/fs/cgroup", 0, MS_MOVE' in source
    assert "child_failure_ready = early_error_bytes == sizeof(early_failure)" in source
    assert "SYS_getrandom 318" in source
    assert "O_RDWR | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW | O_NOCTTY" in source
    assert "value.n != 36" in source
    assert "for (i = 0; i < nonce.n; i++) if (!is_hex" in source
    assert 'memcpy(name_path + 24 + slen(selected), "/name", 6)' in source
    assert "lifecycle_poll_timeout(lifecycle, -1)" in source
    assert "lifecycle_poll_timeout(session, -1)" in source
    assert "session->outbound_failed" in source
    assert "lifecycle_connection_lost(session);" in source
    assert "n != 0 && n != -ESRCH" in source
    assert "lifecycle->stop_request_id ? lifecycle : 0" in source
    assert "if (session->state == LIFECYCLE_TERMINAL)" in source
    assert "session->natural_late_stop_allowed = 0;" in source
    assert "lifecycle->natural_late_stop_allowed = lifecycle->connection_has_hello;" in source
    assert "session->connection == LIFECYCLE_CONNECTED" in source
    assert "send_boundary_ack(session, discarded_header, discarded_payload, discarded_expected)" in source
    signed_host_parser = source.split("static int parse_signed_host", 1)[1].split("static int parse_stop", 1)[0]
    assert signed_host_parser.index('key(j, "reply_to")') < signed_host_parser.index('key(j, "request_id")')
    assert "session->payload_used + 1 == session->payload_expected" in source
    assert "write_all(1, LIFECYCLE_PARTIAL_BUFFERED_MARKER);" in source
    assert source.count("lifecycle_rejected(21, EIO);") >= 2
    assert "else if (stop == 2) write_all(1, LIFECYCLE_STOP_DUPLICATE_MARKER);" in source


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
    value["stage1"]["root_assembly"] = False
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


def test_guest_init_exact_mode_checks_include_special_permission_bits() -> None:
    source = (Path(__file__).resolve().parents[2] / "guest/stage1/init.c").read_text(encoding="utf-8")

    assert "(st.mode & 07777) != (u32)expected_mode" in source
    assert "(st.mode & 0777) != (u32)expected_mode" not in source


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
