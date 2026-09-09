"""Execute the real guest C parser/emitter against Python-signed EXEC frames.

Linux uses host cc; macOS can explicitly opt into the pinned offline container
with PALIMPSEST_GUEST_EXEC_DOCKER_TESTS=1. No VM or privileged syscall is used.
"""

import hashlib
import hmac
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from palimpsest_local import oci_control_protocol_v2 as wire

_TOOLCHAIN = "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"
_KEY = bytes(range(32))
_RUN = "f6f546e2-e734-4920-9eff-1762b348a249"
_ATTEMPT = "aca88126-d991-4de8-b66b-90dc07904dff"
_GENERATION = "b22b1c81-dfa4-478a-b352-27b5b35fe5b7"
_HARNESS = r"""
#define _start guest_boot_start
#include "@SOURCE@"
#undef _start

static __attribute__((used, noreturn)) void harness_main(void) {
    struct lifecycle_session session;
    u8 mode = 0, chunk[1024];
    usize used = 0, i;
    i64 n;
    memset(&session, 0, sizeof(session));
    session.connection = LIFECYCLE_CONNECTED;
    session.connection_has_hello = 1; session.state = LIFECYCLE_READY;
    session.epoch = 1; session.next_sequence = 4; session.last_accepted_host_wire = 2;
    memcpy(session.boot_attempt_id, "aca88126-d991-4de8-b66b-90dc07904dff", 37);
    memcpy(session.boot_generation, "b22b1c81-dfa4-478a-b352-27b5b35fe5b7", 37);
    memcpy(session.host_nonce, "1111111111111111111111111111111111111111111111111111111111111111", 65);
    memcpy(session.key_id, "@KEY_ID@", 72);
    memcpy(lifecycle_binding.run_id, "f6f546e2-e734-4920-9eff-1762b348a249", 37);
    memcpy(lifecycle_binding.core, "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 72);
    memcpy(lifecycle_binding.stage1, "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 72);
    for (i = 0; i < 32; i++) session.boot_key[i] = (u8)i;
    workload.cwd = "/work"; workload.envp[0] = "PATH=/bin"; workload.envp[1] = "VALUE=unchanged";
    workload.envp[2] = 0; workload.envc = 2; workload.uid = 123; workload.gid = 456;
    if (sc3(SYS_read, 0, (i64)&mode, 1) != 1) exit_now(90);
    while (used < sizeof(control_payload)) {
        n = sc3(SYS_read, 0, (i64)(control_payload + used), sizeof(control_payload) - used);
        if (!n) break;
        if (n < 0) exit_now(91);
        used += (usize)n;
    }
    if (mode == 11) {
        struct stat_local current;
        i64 root_fd = sc3(SYS_open, (i64)"/", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
        if (root_fd < 0 || sc2(SYS_fstat, root_fd, (i64)&current) != 0 || sc1(SYS_close, root_fd) != 0)
            exit_now(100);
        root_identity_evidence.device = current.dev;
        root_identity_evidence.inode = current.ino;
        root_identity_evidence.verified = 1;
        if (!refresh_root_identity_evidence()) exit_now(102);
        session.fd = 1;
        session.key_ack_wire_sequence = 2;
        if (!send_control_message(&session, 0, 0)) exit_now(103);
        root_identity_evidence.inode = current.ino ^ 1;
        if (!root_identity_evidence.inode) root_identity_evidence.inode = current.ino + 1;
        session.state = LIFECYCLE_NEW;
        if (refresh_root_identity_evidence() || root_identity_evidence.verified ||
            root_identity_evidence.device || root_identity_evidence.inode ||
            send_control_message(&session, 0, 0) || session.state != LIFECYCLE_NEW)
            exit_now(101);
        exit_now(0);
    }
    if (mode == 12) {
        struct expected_device expected;
        i64 fd;
        memset(&expected, 0, sizeof(expected));
        expected.size = 512;
        fd = sc3(SYS_open, (i64)"@SQUASHFS_FIXTURE@", O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
        if (fd < 0) exit_now(104);
        i = verify_squashfs_structure_fd((int)fd, &expected) ? 0 : 2;
        if (sc1(SYS_close, fd) != 0) exit_now(105);
        exit_now((int)i);
    }
    if (mode == 13 || mode == 14 || mode == 15) {
        int pipes[5][2];
        struct guest_process process;
        struct stat_local observed;
        i64 child, status;
        memset(pipes, 0, sizeof(pipes));
        memset(&process, 0, sizeof(process));
        process.uid = 101; process.gid = 202;
        for (i = 0; i < 2; i++) if (sc2(SYS_pipe2, (i64)pipes[i], O_CLOEXEC) != 0) exit_now(106);
        if (mode == 14 && sc3(SYS_fchown, pipes[0][0], 1, 1) != 0) exit_now(107);
        if (mode == 15 && sc2(91, pipes[1][0], 0400) != 0) exit_now(107);
        if (own_exec_output_pipes(pipes, &process) != (mode == 13)) exit_now(108);
        if (mode != 13) {
            for (i = 0; i < 2; i++) { sc1(SYS_close, pipes[i][0]); sc1(SYS_close, pipes[i][1]); }
            for (i = 0; i < 2; i++) if (sc2(SYS_fstat, pipes[i][0], (i64)&observed) != -9 ||
                                           sc2(SYS_fstat, pipes[i][1], (i64)&observed) != -9) exit_now(114);
            exit_now(0);
        }
        for (i = 0; i < 2; i++) {
            if (sc2(SYS_fstat, pipes[i][0], (i64)&observed) != 0 || observed.uid != 101 ||
                observed.gid != 202 || (observed.mode & S_IFMT) != S_IFIFO ||
                (observed.mode & 07777) != 0600) exit_now(109);
        }
        child = sc0(SYS_fork);
        if (child == 0) {
            if (sc3(SYS_dup3, pipes[0][1], 1, 0) != 1 || sc3(SYS_dup3, pipes[1][1], 2, 0) != 2 ||
                sc2(SYS_setgroups, 0, 0) != 0 || sc3(SYS_setresgid, 202, 202, 202) != 0 ||
                sc3(SYS_setresuid, 101, 101, 101) != 0) exit_now(110);
            if (sc3(SYS_open, (i64)"/proc/self/fd/1", O_WRONLY | O_NONBLOCK | O_NOCTTY, 0) < 0 ||
                sc3(SYS_open, (i64)"/proc/self/fd/2", O_WRONLY | O_NONBLOCK | O_NOCTTY, 0) < 0) exit_now(111);
            exit_now(0);
        }
        if (child < 0) exit_now(112);
        for (i = 0; i < 2; i++) { sc1(SYS_close, pipes[i][0]); sc1(SYS_close, pipes[i][1]); }
        do { status = 0; n = sc4(SYS_wait4, child, (i64)&status, 0, 0); } while (n == -EINTR);
        if (n != child || status != 0) exit_now(113);
        exit_now(0);
    }
    if (mode == 3) session.state = LIFECYCLE_STOPPING;
    if (mode == 8) session.last_exec_request_id = 9;
    if (mode == 9) remote_exec.active = 1;
    if (!parse_exec(&session, used)) exit_now(2);
    if (!remote_exec.pending || remote_exec.request_id != 9 || session.last_exec_request_id != 9 ||
        remote_exec.process.uid != 123 || remote_exec.process.gid != 456 ||
        !text_equal(remote_exec.process.cwd, "/work") ||
        !text_equal(remote_exec.process.envp[1], "VALUE=unchanged")) exit_now(92);
    if (mode == 1) {
        if (parse_exec(&session, used) || session.last_accepted_host_wire != 3) exit_now(93);
    } else if (mode == 4) {
        for (i = 0; i < sizeof(chunk); i++) chunk[i] = (u8)i;
        if (!queue_exec_output(&session, 0, chunk, sizeof(chunk))) exit_now(94);
        if (sc3(SYS_write, 1, (i64)remote_exec.wire, remote_exec.wire_used) != (i64)remote_exec.wire_used) exit_now(95);
        remote_exec.wire_used = 0;
        if (!queue_exec_output(&session, 1, chunk, 16)) exit_now(94);
        if (sc3(SYS_write, 1, (i64)remote_exec.wire, remote_exec.wire_used) != (i64)remote_exec.wire_used) exit_now(95);
        remote_exec.wire_used = 0; remote_exec.status = 23 << 8; remote_exec.reason = 0;
        if (!queue_exec_exit(&session)) exit_now(96);
        if (sc3(SYS_write, 1, (i64)remote_exec.wire, remote_exec.wire_used) != (i64)remote_exec.wire_used) exit_now(95);
    } else if (mode == 6) {
        if (!cancel_remote_exec(3) || remote_exec.pending || remote_exec.phase != 3 || !queue_exec_exit(&session)) exit_now(97);
        if (sc3(SYS_write, 1, (i64)remote_exec.wire, remote_exec.wire_used) != (i64)remote_exec.wire_used) exit_now(95);
    } else if (mode == 7) {
        char name[32];
        if (!format_exec_session_name(name, 2) || !text_equal(name, "exec-00000002")) exit_now(98);
    } else if (mode == 10) {
        memset(control_body, 123, sizeof(control_body));
        memset(control_output, 234, sizeof(control_output));
        memset(remote_exec.wire, 45, sizeof(remote_exec.wire));
        wipe_child_control_authority(&session);
        if (!bytes_all_zero((u8 *)&session, sizeof(session)) ||
            !bytes_all_zero((u8 *)&lifecycle_binding, sizeof(lifecycle_binding)) ||
            !bytes_all_zero(control_payload, sizeof(control_payload)) ||
            !bytes_all_zero(control_body, sizeof(control_body)) ||
            !bytes_all_zero(control_output, sizeof(control_output)) ||
            !bytes_all_zero((u8 *)&control_parser, sizeof(control_parser)) ||
            !bytes_all_zero(remote_exec.wire, sizeof(remote_exec.wire)) ||
            !text_equal(remote_exec.process.envp[1], "VALUE=unchanged")) exit_now(99);
    } else {
        for (i = 0; i < remote_exec.process.argc; i++) {
            usize size = slen(remote_exec.process.argv[i]) + 1;
            if (sc3(SYS_write, 1, (i64)remote_exec.process.argv[i], size) != (i64)size) exit_now(95);
        }
    }
    exit_now(0);
}
__attribute__((naked, noreturn, visibility("default"))) void harness_start(void) {
    __asm__ volatile("and $-16,%rsp\ncall harness_main\n");
}
"""


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    docker = os.environ.get("PALIMPSEST_GUEST_EXEC_DOCKER_TESTS") == "1"
    if not docker and not sys.platform.startswith("linux"):
        pytest.skip("guest C harness requires Linux or explicit pinned-Docker opt-in")
    directory = tmp_path_factory.mktemp("guest-exec-c")
    repository = Path(__file__).resolve().parents[2]
    source = "/repo/guest/stage1/init.c" if docker else str(repository / "guest/stage1/init.c")
    squashfs_fixture = "/out/lower.raw" if docker else str(directory / "lower.raw")
    (directory / "harness.c").write_text(
        _HARNESS.replace("@SOURCE@", source)
        .replace("@KEY_ID@", wire.key_identifier(_KEY))
        .replace("@SQUASHFS_FIXTURE@", squashfs_fixture)
    )
    flags = [
        "-std=c11",
        "-Os",
        "-nostdlib",
        "-static",
        "-fno-builtin",
        "-fno-stack-protector",
        "-no-pie",
        "-mno-red-zone",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wl,-e,harness_start",
    ]
    if docker:
        base = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--mount",
            f"type=bind,src={directory},dst=/out",
            "--mount",
            f"type=bind,src={repository},dst=/repo,readonly",
        ]
        build = [
            *base,
            "--entrypoint",
            "/usr/local/bin/gcc",
            _TOOLCHAIN,
            *flags,
            "-o",
            "/out/harness",
            "/out/harness.c",
        ]
        command = [*base, "--entrypoint", "/out/harness", _TOOLCHAIN]
    else:
        compiler = shutil.which("cc")
        assert compiler, "guest C harness needs cc"
        build = [compiler, *flags, "-o", str(directory / "harness"), str(directory / "harness.c")]
        command = [str(directory / "harness")]
    compiled = subprocess.run(build, capture_output=True, timeout=60, check=False)
    assert compiled.returncode == 0, compiled.stderr.decode(errors="replace")

    def execute(frame, mode=0):
        return subprocess.run(command, input=bytes([mode]) + frame[4:], capture_output=True, timeout=15, check=False)

    execute.squashfs_fixture = directory / "lower.raw"
    if docker:
        execute.root_command = [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "0:0",
            "--pids-limit",
            "16",
            "--memory",
            "128m",
            "--cpus",
            "0.25",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=16m",
            "--mount",
            f"type=bind,src={directory / 'harness'},dst=/harness,readonly",
            "--entrypoint",
            "/harness",
            _TOOLCHAIN,
        ]

    return execute


def _structural_squashfs(*, fragments, fragment_table_start, id_table_start=144, padding=None):
    maximum = 2**64 - 1
    bytes_used = 160
    payload = struct.pack(
        "<5I6H8Q",
        0x73717368,
        1,
        0,
        131072,
        fragments,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        bytes_used,
        id_table_start,
        maximum,
        96,
        112,
        fragment_table_start,
        maximum,
    )
    payload += b"\0" * (bytes_used - len(payload))
    return payload + (padding if padding is not None else b"\0" * (512 - bytes_used))


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        pytest.param(_structural_squashfs(fragments=0, fragment_table_start=128), True, id="zero-finite"),
        pytest.param(_structural_squashfs(fragments=0, fragment_table_start=2**64 - 1), True, id="zero-sentinel"),
        pytest.param(_structural_squashfs(fragments=1, fragment_table_start=128), True, id="nonzero-finite"),
        pytest.param(_structural_squashfs(fragments=1, fragment_table_start=2**64 - 1), False, id="nonzero-sentinel"),
        pytest.param(_structural_squashfs(fragments=0, fragment_table_start=160), False, id="out-of-range"),
        pytest.param(
            _structural_squashfs(fragments=0, fragment_table_start=128, id_table_start=2**64 - 1),
            False,
            id="required-table",
        ),
        pytest.param(
            _structural_squashfs(
                fragments=0,
                fragment_table_start=128,
                padding=b"\0" * (512 - 161) + b"x",
            ),
            False,
            id="nonzero-padding",
        ),
    ],
)
def test_real_c_squashfs_structural_acceptance_matches_v3(runner, payload, accepted):
    runner.squashfs_fixture.write_bytes(payload)
    result = runner(b"\0\0\0\0", mode=12)
    assert result.returncode == (0 if accepted else 2), result.stderr


@pytest.mark.parametrize("mode", [13, 14, 15])
def test_real_c_exec_output_pipe_ownership_and_uid101_self_reopen(runner, mode):
    if os.environ.get("PALIMPSEST_GUEST_EXEC_DOCKER_TESTS") != "1":
        pytest.skip("UID 101 pipe ownership proof requires the restricted pinned Docker harness")
    result = subprocess.run(
        runner.root_command,
        input=bytes([mode]),
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_exec_output_ownership_is_before_fork_and_failure_closes_all_pairs() -> None:
    source = (Path(__file__).resolve().parents[2] / "guest/stage1/init.c").read_text()
    start = source.index("static int start_remote_exec")
    body = source[start : source.index("static int cancel_remote_exec", start)]
    assert body.index("own_exec_output_pipes") < body.index("pid = sc0(SYS_fork)")
    failed = body[body.index("failed:") :]
    assert "for (i = 0; i < count; i++)" in failed
    assert "sc1(SYS_close, pipes[i][0])" in failed and "sc1(SYS_close, pipes[i][1])" in failed


def _frame(argv=("/bin/demo",), timeout=1000):
    message = wire.OCIControlV2Message(
        "EXEC",
        wire.OCIControlV2Binding(_RUN, "sha256:" + "a" * 64, "sha256:" + "b" * 64),
        _ATTEMPT,
        "1" * 64,
        1,
        3,
        {"argv": list(argv), "timeout_ms": timeout},
        request_id=9,
        boot_generation=_GENERATION,
        reply_to=None,
    )
    return wire.encode_frame(wire.sign_message(message, _KEY))


@pytest.mark.parametrize(
    "argv", [("/bin/demo",), ("printf", "line\nbreak", "", "$HOME;literal", "한글"), tuple(["x"] * 64)]
)
def test_real_c_parser_accepts_authenticated_literal_argv_and_inherited_policy(runner, argv):
    result = runner(_frame(argv))
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"".join(item.encode() + b"\0" for item in argv)


@pytest.mark.parametrize("mode,expected", [(1, 0), (3, 2), (7, 0), (8, 2), (9, 2)])
def test_real_c_parser_rejects_duplicate_nonready_replayed_or_busy_exec(runner, mode, expected):
    assert runner(_frame(), mode).returncode == expected


def test_real_c_parser_rejects_tampered_authenticated_argv(runner):
    encoded = _frame().replace(b"/bin/demo", b"/bin/evil")
    assert runner(encoded).returncode == 2


def test_real_c_root_identity_drift_clears_evidence_and_suppresses_ready(runner):
    if os.environ.get("PALIMPSEST_GUEST_EXEC_DOCKER_TESTS") != "1":
        pytest.skip("root-identity harness requires the pinned Docker process to run as PID 1")
    result = runner(_frame(), 11)
    assert result.returncode == 0, result.stderr
    frames = _decode_outputs(result.stdout)
    assert len(frames) == 1
    ready = frames[0]
    assert ready.kind == "READY"
    assert ready.binding == wire.OCIControlV2Binding(_RUN, "sha256:" + "a" * 64, "sha256:" + "b" * 64)
    assert ready.boot_attempt_id == _ATTEMPT
    assert ready.boot_generation == _GENERATION
    assert ready.wire_sequence == 4
    assert ready.reply_to == 2
    assert ready.payload["root_identity"]["schema"] == "palimpsest.oci-root-identity.v1"
    assert ready.payload["root_identity"]["pid"] == 1
    assert ready.payload["root_identity"]["filesystem"] == "overlayfs"
    assert type(ready.payload["root_identity"]["device"]) is int
    assert type(ready.payload["root_identity"]["inode"]) is int
    assert ready.payload["root_identity"]["inode"] > 0


@pytest.mark.parametrize(
    "payload",
    [
        {"argv": [], "timeout_ms": 1},
        {"argv": ["x"] * 65, "timeout_ms": 1},
        {"argv": ["x" * 8192], "timeout_ms": 1},
        {"argv": ["x"], "timeout_ms": 0},
        {"argv": ["x"], "timeout_ms": 30001},
        {"argv": ["x"], "timeout_ms": 1, "env": {}},
    ],
)
def test_real_c_parser_rejects_valid_mac_with_out_of_contract_payload(runner, payload):
    # Sign deliberately invalid wire data without calling the production codec's
    # admission validator; the independently executing C must reject it too.
    original = wire.decode_frame(_frame())
    body = original.body.to_dict()
    body["payload"] = payload
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    prefix = wire.OCI_CONTROL_PROTOCOL_V2.encode() + b"\0frame\0host-to-guest\0channel-frame\0"
    key = wire._hkdf_subkey(_KEY, original.body, wire.OCI_CONTROL_CHANNEL_CARRIER)
    tag = hmac.new(key, prefix + struct.pack(">I", len(encoded)) + encoded, hashlib.sha256).hexdigest()
    outer = json.dumps(
        {"body": body, "mac": {"key_id": wire.key_identifier(_KEY), "tag": tag}}, sort_keys=True, separators=(",", ":")
    ).encode()
    assert runner(struct.pack(">I", len(outer)) + outer).returncode == 2


def test_real_c_secret_wipe_preserves_process_policy_but_erases_control_authority(runner):
    assert runner(_frame(), 10).returncode == 0


def _decode_outputs(payload):
    decoder = wire.OCIControlV2FrameDecoder()
    frames = decoder.feed(payload)
    for envelope in frames:
        wire.verify_message_authentication(envelope, _KEY)
    return [envelope.body for envelope in frames]


def test_real_c_emitter_matches_python_binary_output_offsets_and_terminal(runner):
    result = runner(_frame(), 4)
    assert result.returncode == 0, result.stderr
    output, errors, terminal = _decode_outputs(result.stdout)
    assert output.kind == errors.kind == "EXEC_OUTPUT"
    assert output.payload == {"stream": "stdout", "offset": 0, "data_hex": bytes(range(256)).hex() * 4}
    assert errors.payload == {"stream": "stderr", "offset": 0, "data_hex": bytes(range(16)).hex()}
    assert terminal.kind == "EXEC_EXIT" and terminal.reply_to == 9
    assert terminal.payload == {
        "reason": "completed",
        "stdout_bytes": 1024,
        "stderr_bytes": 16,
        "terminal": {"exit_code": 23, "signal": None},
    }


def test_real_c_pending_exec_cancel_emits_terminal_without_starting_process(runner):
    result = runner(_frame(), 6)
    assert result.returncode == 0, result.stderr
    (terminal,) = _decode_outputs(result.stdout)
    assert terminal.payload == {
        "reason": "cancelled",
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "terminal": None,
    }
