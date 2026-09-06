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
    assert value["stage1"]["consumer_contract"] == "palimpsest.guest-stage1-consumer.x86_64.v18"
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
        "agent_cgroup": "/palimpsest.agent",
        "cgroup": "fd-pinned-cgroup-v2-agent-exec-session-hierarchy",
        "cgroup_security": "private-readonly-view-plus-pid1-owned-leaf-cleanup-authority",
        "cleanup_scope": "exec-session-leaf-then-empty-agent-parent",
        "contract": "palimpsest.guest-pid1-supervisor.v10",
        "credential_transition": "child-isolate-drop-verify-parent-attach-key-bootstrap-ack-release",
        "credentials": "root-pid1-broker-image-root-passwd-group-resolution-empty-supplementary-groups",
        "environment": "authenticated-image-environment-with-fixed-container-default-path",
        "exec_session_cgroup": "/palimpsest.agent/exec-00000001",
        "execution": "shell-free-path-search-fork-isolation-ready-exec-session-attach-release-gate-execve-cloexec-error-pipe",
        "isolation": {
            "capabilities": "empty-bounding-ambient-permitted-effective-inheritable",
            "contract": "palimpsest.workload-lifecycle-authority-isolation.v3",
            "devices": ["full", "null", "random", "tty", "urandom", "zero"],
            "lifecycle_fd": "child-closed-before-isolation-ready",
            "lifecycle_key": "pid1-generated-post-fork-post-isolation-never-inherited",
            "mounts": "private-dev-tmpfs-masked-virtio-ports-readonly-proc-sys-cgroup",
            "pid1_proc": "nondumpable-before-fork",
            "seccomp": "authority-escape-boundary-filter",
        },
        "lifecycle_broker": "palimpsest.guest-lifecycle-broker.v3",
        "lifecycle_delivery": "authenticated-v2-bootstrap-boundary-ack-reconnect-snapshot-same-id-stop-retry",
        "max_active_sessions_qualified": 1,
        "membership": "pid1-root-agent-parent-empty-workload-in-primary-leaf",
        "parallel_exec_sessions_proven": False,
        "privilege_after_fork": "root-pid1-narrow-broker-capabilityless-workload",
        "session_id": 1,
        "session_id_allocation": "guest-internal-monotonic-u32",
        "signal_transport": "blocked-signalfd-process-group-forwarding",
        "production_cleanup": "stop-signal-grace-leaf-cgroup.kill-wait4-echild-leaf-empty-rmdir-parent-empty-rmdir",
        "terminal_root_quiesce": {
            "contract": "palimpsest.terminal-root-quiesce.v1",
            "filesystem": "overlay",
            "identity": "nofollow-slash-directory-proc-self-root-stable-before-after",
            "ordering": "workload-and-cgroup-cleanup-then-syncfs-and-close-then-terminal",
            "sync": "syncfs",
        },
        "terminal_state": "root-quiesce-then-parent-marker-then-fail-closed-wait",
        "wait": "wait4-to-echild-with-empty-leaf-and-parent-proof",
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
    assert (
        b"; cleanup=exec-session-cgroup.kill; leaf_populated=0; leaf_removed=1; "
        b"parent_populated=0; parent_removed=1" in stage1
    )
    assert b"org.palimpsest.oci.lifecycle.0" in stage1
    assert b"palimpsest.oci-lifecycle-control.v2" in stage1
    assert b"lifecycle rejected; stage=" in stage1
    for marker in (
        b"lifecycle channel ready",
        b"lifecycle initial HELLO accepted",
        b"lifecycle BOOTSTRAP sent",
        b"lifecycle KEY_ACK accepted",
    ):
        assert marker in stage1


def test_initial_lifecycle_wait_is_unbounded_only_before_the_first_input_byte() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "guest" / "stage1" / "init.c").read_text()

    prepare = source[
        source.index("static int prepare_lifecycle(") : source.index("static int authenticate_lifecycle_bootstrap(")
    ]
    reader = source[source.index("static int read_control_frame(") : source.index("static int nonce_seen(")]
    connection_lost = source[
        source.index("static void lifecycle_connection_lost(") : source.index("static int lifecycle_poll_timeout(")
    ]
    live_main = source[source.index("static __attribute__((noreturn, used)) void start_c(") :]
    assert "deadline = monotonic_millis() + 5000" not in prepare
    assert "for (;;)" in prepare
    assert "frame == -2 && session->initial_input_seen" in prepare
    assert "session->initial_input_seen = 1" in reader
    assert "initial_input_seen" not in connection_lost
    assert "session->frame_deadline = now + 5000" in reader
    assert "session->frame_deadline && monotonic_millis() >= session->frame_deadline" in reader
    assert live_main.index("if (!prepare_lifecycle(&lifecycle))") < live_main.index("supervise_workload(&workload")


def test_lifecycle_progress_markers_follow_only_committed_boundaries() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "guest" / "stage1" / "init.c").read_text()
    prepare = source[
        source.index("static int prepare_lifecycle(") : source.index("static int authenticate_lifecycle_bootstrap(")
    ]
    send_bootstrap = source[
        source.index("static int send_bootstrap(") : source.index("static int send_control_message(")
    ]
    authenticate = source[
        source.index("static int authenticate_lifecycle_bootstrap(") : source.index(
            "static void wipe_lifecycle_secret("
        )
    ]
    supervisor = source[source.index("static int supervise_workload(") : source.index("static int run_consumer(")]

    assert prepare.index("if (session->fd < 0)") < prepare.index("LIFECYCLE_CHANNEL_READY_MARKER")
    assert prepare.index("if (parse_hello(session, size))") < prepare.index("LIFECYCLE_HELLO_ACCEPTED_MARKER")
    assert (
        send_bootstrap.index("write_signed_message(session, control_body")
        < send_bootstrap.index("secure_zero(control_body, used)")
        < send_bootstrap.index("return (int)i")
    )
    assert authenticate.index("!send_bootstrap(session)") < authenticate.index("LIFECYCLE_BOOTSTRAP_SENT_MARKER")
    assert authenticate.index("if (!parse_signed_host(session, size, 0") < authenticate.index(
        "LIFECYCLE_KEY_ACK_ACCEPTED_MARKER"
    )
    assert authenticate.index("session->key_ack_wire_sequence = wire") < authenticate.index(
        "LIFECYCLE_KEY_ACK_ACCEPTED_MARKER"
    )
    assert supervisor.index("if (!resolve_workload_identity(process))") < supervisor.index(
        "if (!authenticate_lifecycle_bootstrap(lifecycle))"
    )
    assert supervisor.index("if (!authenticate_lifecycle_bootstrap(lifecycle))") < supervisor.index(
        "WORKLOAD_ISOLATION_MARKER"
    )


def test_post_fork_launch_failures_prioritize_cleanup_uncertainty() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "guest" / "stage1" / "init.c").read_text()
    packaged = (repository / "src" / "palimpsest_local" / "assets" / "oci-stage1-init.x86_64").read_bytes()

    assert source.count("return n ? 0 : -1;") == 4
    assert "sc2(SYS_kill, main_pid, SIGKILL)" not in source
    assert "SYS_kill, -1" not in source
    assert b"kill(-1" not in packaged
    assert b"whole-guest" not in packaged
    assert 'exact_string(&j, "palimpsest.guest-stage1.v15")' in source
    assert 'exact_string(&j, "first-party-pid1-supervisor.v9")' in source
    assert '"palimpsest.agent"' in source
    assert 'append_text(expected, &expected_used, "0::/palimpsest.agent/")' in source
    assert "append_text(expected, &expected_used, session->name)" in source
    assert "return read_exact_attr(proc_path, expected)" in source
    assert "struct workload_agent" in source
    assert "struct exec_session" in source
    assert "static int create_exec_session(" in source
    assert 'safe_dir("/sys/fs/cgroup", 0, 0, 0555' in source
    assert "root_fs.type != CGROUP2_MAGIC" in source
    assert "static int close_cgroup_node(" in source
    assert "if (!close_cgroup_node(&session->leaf)) valid = 0;" in source
    assert "if (!close_cgroup_node(&agent->parent)) valid = 0;" in source
    assert "agent->next_session_id++" in source
    assert "u32 divisor = 1000000000" in source
    assert "divisor <= 10000000" in source
    assert "agent->active_sessions >= 2" in source
    assert "agent->active_sessions == 1 && remove_empty_exec_session(agent, session)" in source
    assert 'cgroup_populated(&agent->parent, "populated 1\\n")' in source
    assert "cgroup_procs_empty(&agent->parent)" in source
    assert "kill_exec_session(session)" in source
    assert "remove_empty_exec_session_and_agent(agent, session)" in source
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
    assert 'exact_string(&j, "palimpsest.workload-lifecycle-authority-isolation.v3")' in source
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
    assert "lifecycle_poll_timeout(lifecycle, remote_exec.active ? 100 : -1)" in source
    assert "lifecycle_poll_timeout(session, -1)" in source
    assert "session->outbound_failed" in source
    assert "lifecycle_connection_lost(session);" in source
    assert "n != 0 && n != -ESRCH" in source
    assert "lifecycle->state >= LIFECYCLE_READY && !lifecycle->natural_terminal_frozen" in source
    assert "terminate_and_reap(main_pid, (int)signal_fd, &agent, &session, result, lifecycle)" in source
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


def test_ready_root_identity_revalidates_the_transition_baseline() -> None:
    source = (Path(__file__).resolve().parents[2] / "guest" / "stage1" / "init.c").read_text()
    assert "root_identity_evidence.device = merged_identity.dev" in source
    assert "root_identity_evidence.inode = merged_identity.ino" in source
    assert "slash.dev == root_identity_evidence.device && slash.ino == root_identity_evidence.inode" in source
    assert source.index("if (!refresh_root_identity_evidence())") < source.index(
        "lifecycle->state = LIFECYCLE_READY"
    )


def test_terminal_root_quiesce_failure_cannot_publish_terminal_state_or_frame() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "guest" / "stage1" / "init.c").read_text()
    quiesce = source.split("static int quiesce_terminal_root(void)", 1)[1].split("static int transition_root", 1)[0]
    terminal_tail = source.rsplit("if (!lifecycle->stop_request_id)", 1)[1].split("static int run_consumer", 1)[0]

    assert 'SYS_open, (i64)"/", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY' in quiesce
    assert 'SYS_open, (i64)"/proc/self/root", O_RDONLY | O_CLOEXEC | O_DIRECTORY' in quiesce
    assert "before_fs.type == OVERLAYFS_MAGIC" in quiesce
    assert "before.dev == proc_root.dev && before.ino == proc_root.ino" in quiesce
    assert "sc1(SYS_syncfs, root_fd) == 0" in quiesce
    assert "after_fs.type == OVERLAYFS_MAGIC" in quiesce
    assert "sc1(SYS_close, proc_root_fd) != 0" in quiesce
    assert "sc1(SYS_close, root_fd) != 0" in quiesce

    cleanup = terminal_tail.index("terminate_and_reap(")
    cleanup_proof = terminal_tail.index("if (!verify_root_supervisor(result))")
    quiesce_call = terminal_tail.index("if (!quiesce_terminal_root())")
    quiesce_failure = terminal_tail.index("set_workload_failure(failure, 37, EIO)")
    failure_return = terminal_tail.index("return -1;", quiesce_failure)
    quiesce_marker = terminal_tail.index("write_all(1, TERMINAL_ROOT_QUIESCED_MARKER)")
    terminal_state = terminal_tail.index("lifecycle->state = LIFECYCLE_TERMINAL")
    terminal_frame = terminal_tail.index("send_control_message(lifecycle, 3, result)")
    assert cleanup < cleanup_proof < quiesce_call < quiesce_failure < failure_return < quiesce_marker
    assert quiesce_marker < terminal_state < terminal_frame


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
