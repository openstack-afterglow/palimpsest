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
    (directory / "harness.c").write_text(
        _HARNESS.replace("@SOURCE@", source).replace("@KEY_ID@", wire.key_identifier(_KEY))
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

    return execute


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
