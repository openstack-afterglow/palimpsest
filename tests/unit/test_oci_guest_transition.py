"""Real C coverage for bounded stage-1 transition-target diagnostics."""

import os
import subprocess
from pathlib import Path

import pytest

_TOOLCHAIN = "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"
_MARKER = "palimpsest guest stage1: root transition target rejected; target=proc; check={}\n"
_HARNESS = r"""
#define SYS_lstat 6
#define SYS_symlink 88
#define O_CREAT 0100
#define O_EXCL 0200
#define _start guest_boot_start
#include "@SOURCE@"
#undef _start

static __attribute__((noreturn, used)) void harness_main(u64 *stack) {
    char **argv = (char **)(stack + 1);
    const char *kind = stack[0] == 3 ? argv[1] : "usage";
    enum transition_target label = TRANSITION_TARGET_PROC;
    const char *target = "@FIXTURE@";
    enum safe_dir_reason reason = SAFE_DIR_REASON_UNKNOWN;
    struct stat_local before, after;
    int fd = -1;
    i64 child;
    if (stack[0] != 3) exit_now(89);
    if (text_equal(argv[2], "sys")) label = TRANSITION_TARGET_SYS;
    else if (text_equal(argv[2], "dev")) label = TRANSITION_TARGET_DEV;
    else if (!text_equal(argv[2], "proc")) exit_now(89);
    if (text_equal(kind, "symlink")) {
        if (sc2(SYS_mkdir, (i64)"@FIXTURE@/real", 0755) != 0 ||
            sc2(SYS_symlink, (i64)"real", (i64)"@FIXTURE@/target") != 0) exit_now(90);
        target = "@FIXTURE@/target";
    } else if (text_equal(kind, "nonempty")) {
        child = sc3(SYS_open, (i64)"@FIXTURE@/child", O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (child < 0 || sc1(SYS_close, child) != 0) exit_now(91);
    }
    if (sc2(SYS_lstat, (i64)target, (i64)&before) != 0) exit_now(92);
    if (safe_dir_checked(target, 1, 1, 0755, &fd, &reason)) {
        if (fd < 0 || sc1(SYS_close, fd) != 0 || !text_equal(kind, "accepted")) exit_now(93);
        exit_now(0);
    }
    transition_target_rejected(label, reason);
    if (sc2(SYS_lstat, (i64)target, (i64)&after) != 0 ||
        before.mode != after.mode || before.uid != after.uid || before.gid != after.gid) exit_now(94);
    if (text_equal(kind, "mode") && reason == SAFE_DIR_REASON_MODE) exit_now(0);
    if (text_equal(kind, "nonempty") && reason == SAFE_DIR_REASON_NONEMPTY) exit_now(0);
    if (text_equal(kind, "symlink") && reason == SAFE_DIR_REASON_OPEN) exit_now(0);
    if (text_equal(kind, "owner") && reason == SAFE_DIR_REASON_OWNER) exit_now(0);
    exit_now(95);
}

__attribute__((naked, noreturn, visibility("default"))) void harness_start(void) {
    __asm__ volatile("mov %rsp,%rdi\nand $-16,%rsp\ncall harness_main\n");
}
"""


@pytest.fixture(scope="module")
def transition_harness(tmp_path_factory):
    if os.environ.get("PALIMPSEST_GUEST_TRANSITION_DOCKER_TESTS") != "1":
        pytest.skip("requires PALIMPSEST_GUEST_TRANSITION_DOCKER_TESTS=1 and Docker")
    directory = tmp_path_factory.mktemp("guest-transition-c")
    repository = Path(__file__).resolve().parents[2]
    harness_source = directory / "harness.c"
    output_directory = directory / "output"
    output_directory.mkdir()
    harness_source.write_text(
        _HARNESS.replace("@SOURCE@", "/repo/guest/stage1/init.c").replace("@FIXTURE@", "/fixtures")
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
    build = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=32m",
        "--mount",
        f"type=bind,src={output_directory},dst=/out",
        "--mount",
        f"type=bind,src={harness_source},dst=/harness.c,readonly",
        "--mount",
        f"type=bind,src={repository},dst=/repo,readonly",
        "--entrypoint",
        "/usr/local/bin/gcc",
        _TOOLCHAIN,
        *flags,
        "-o",
        "/out/harness",
        "/harness.c",
    ]
    compiled = subprocess.run(build, capture_output=True, timeout=60, check=False)
    assert compiled.returncode == 0, compiled.stderr.decode(errors="replace")

    def execute(kind, *, target="proc", fixture_mode="0755", fixture_uid=0):
        command = [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "0:0",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=16m",
            "--tmpfs",
            f"/fixtures:rw,nosuid,nodev,noexec,size=1m,mode={fixture_mode},uid={fixture_uid},gid={fixture_uid}",
            "--mount",
            f"type=bind,src={output_directory / 'harness'},dst=/harness,readonly",
            "--entrypoint",
            "/harness",
            _TOOLCHAIN,
            kind,
            target,
        ]
        return subprocess.run(command, capture_output=True, timeout=15, check=False)

    return execute


@pytest.mark.parametrize(
    ("kind", "target", "mode", "uid", "reason"),
    [
        pytest.param("mode", "proc", "0555", 0, "mode", id="root-empty-0555-proc"),
        pytest.param("mode", "sys", "0555", 0, "mode", id="root-empty-0555-sys"),
        pytest.param("mode", "dev", "0555", 0, "mode", id="root-empty-0555-dev"),
        pytest.param("nonempty", "proc", "0755", 0, "nonempty", id="root-nonempty"),
        pytest.param("symlink", "proc", "0755", 0, "open", id="nofollow-symlink"),
        pytest.param("owner", "proc", "0755", 12345, "owner", id="wrong-owner"),
    ],
)
def test_real_c_transition_target_rejections_are_exact_and_do_not_normalize(
    transition_harness, kind, target, mode, uid, reason
):
    result = transition_harness(kind, target=target, fixture_mode=mode, fixture_uid=uid)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""
    assert result.stderr.decode() == _MARKER.replace("target=proc", f"target={target}").format(reason)


def test_real_c_transition_target_accepts_root_owned_empty_0755(transition_harness):
    result = transition_harness("accepted")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""
    assert result.stderr == b""


@pytest.mark.parametrize("mode", ["01755", "02755", "04755", "0775"])
def test_real_c_transition_target_rejects_other_modes(transition_harness, mode):
    result = transition_harness("mode", fixture_mode=mode)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""
    assert result.stderr.decode() == _MARKER.format("mode")
