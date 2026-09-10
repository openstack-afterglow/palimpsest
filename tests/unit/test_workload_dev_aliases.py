"""Real-C coverage for the private workload /dev stdout/stderr aliases."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "guest/stage1/init.c"
TOOLCHAIN = "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"

HARNESS = r"""
#define SYS_renameat 264
#define SYS_linkat 265
#define SYS_fchownat 260
#define SYS_fchmodat 268
#define O_CREAT 0100
#define O_EXCL 0200
#define AT_EMPTY_PATH 0x1000
#define _start guest_boot_start
#include "/repo/guest/stage1/init.c"
#undef _start

static int make_node_at(int directory, const char *name, u32 major, u32 minor) {
    struct stat_local st;
    if (sc4(SYS_mknodat, directory, (i64)name, S_IFCHR | 0666,
            make_device_number(major, minor)) != 0) return 0;
    if (sc3(SYS_fchmodat, directory, (i64)name, 0666) != 0) return 0;
    return sc4(SYS_newfstatat, directory, (i64)name, (i64)&st, AT_SYMLINK_NOFOLLOW) == 0;
}

static int make_six_devices(int directory) {
    static const char *names[] = {"null", "zero", "full", "random", "urandom", "tty"};
    static const u32 majors[] = {1, 1, 1, 1, 1, 5};
    static const u32 minors[] = {3, 5, 7, 8, 9, 0};
    for (usize i = 0; i < 6; i++)
        if (!make_node_at(directory, names[i], majors[i], minors[i])) return 0;
    return 1;
}

static __attribute__((noreturn, used)) void harness_main(u64 *stack) {
    char **argv = (char **)(stack + 1);
    const char *scenario = stack[0] == 2 ? argv[1] : "usage";
    int directory = (int)sc3(SYS_open, (i64)"/fixtures", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (directory < 0 || !make_six_devices(directory)) exit_now(80);
    if (text_equal(scenario, "getdents-error")) {
        struct stat_local st;
        i64 regular = sc4(SYS_openat, directory, (i64)"ordinary", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
        if (regular < 0 || safe_workload_dev_entries_at((int)regular) ||
            sc2(SYS_fstat, regular, (i64)&st) != 0 || sc1(SYS_close, regular) != 0) exit_now(91);
    } else if (text_equal(scenario, "existing")) {
        if (sc3(SYS_symlinkat, (i64)"/wrong", directory, (i64)"stdout") != 0 ||
            safe_workload_stdio_aliases_at(directory, 1)) exit_now(81);
    } else {
        if (!safe_workload_stdio_aliases_at(directory, 1)) exit_now(82);
        if (text_equal(scenario, "valid")) {
            if (!safe_workload_dev_entries_at(directory)) exit_now(83);
        } else if (text_equal(scenario, "wrong-target")) {
            if (sc3(SYS_unlinkat, directory, (i64)"stderr", 0) != 0 ||
                sc3(SYS_symlinkat, (i64)"/proc/self/fd/20", directory, (i64)"stderr") != 0 ||
                safe_workload_dev_entries_at(directory)) exit_now(84);
        } else if (text_equal(scenario, "wrong-type")) {
            if (sc3(SYS_unlinkat, directory, (i64)"stdout", 0) != 0 ||
                sc4(SYS_mknodat, directory, (i64)"stdout", S_IFCHR | 0666,
                    make_device_number(1, 3)) != 0 || safe_workload_dev_entries_at(directory)) exit_now(85);
        } else if (text_equal(scenario, "hardlink")) {
            if (sc5(SYS_linkat, directory, (i64)"stdout", directory, (i64)"linked", 0) != 0 ||
                safe_workload_stdio_aliases_at(directory, 0)) exit_now(86);
        } else if (text_equal(scenario, "wrong-owner")) {
            if (sc5(SYS_fchownat, directory, (i64)"stdout", 1, 1, AT_SYMLINK_NOFOLLOW) != 0 ||
                safe_workload_stdio_aliases_at(directory, 0)) exit_now(86);
        } else if (text_equal(scenario, "same-length-target")) {
            if (sc3(SYS_unlinkat, directory, (i64)"stdout", 0) != 0 ||
                sc3(SYS_symlinkat, (i64)"/proc/1/fd/1000", directory, (i64)"stdout") != 0 ||
                safe_workload_stdio_aliases_at(directory, 0)) exit_now(86);
        } else if (text_equal(scenario, "long-target")) {
            if (sc3(SYS_unlinkat, directory, (i64)"stdout", 0) != 0 ||
                sc3(SYS_symlinkat, (i64)"/proc/self/fd/10000000", directory, (i64)"stdout") != 0 ||
                safe_workload_stdio_aliases_at(directory, 0)) exit_now(86);
        } else if (text_equal(scenario, "missing")) {
            if (sc3(SYS_unlinkat, directory, (i64)"stderr", 0) != 0 ||
                safe_workload_dev_entries_at(directory)) exit_now(87);
        } else if (text_equal(scenario, "extra")) {
            if (sc3(SYS_symlinkat, (i64)"/proc/self/fd", directory, (i64)"fd") != 0 ||
                safe_workload_dev_entries_at(directory)) exit_now(88);
        } else if (text_equal(scenario, "device-wrong-type")) {
            if (sc3(SYS_unlinkat, directory, (i64)"null", 0) != 0 ||
                sc3(SYS_symlinkat, (i64)"/dev/null", directory, (i64)"null") != 0 ||
                safe_workload_device_at(directory, "null", 1, 3)) exit_now(88);
        } else if (text_equal(scenario, "device-wrong-mode")) {
            if (sc3(SYS_fchmodat, directory, (i64)"null", 0600) != 0 ||
                safe_workload_device_at(directory, "null", 1, 3)) exit_now(88);
        } else if (text_equal(scenario, "device-wrong-owner")) {
            if (sc5(SYS_fchownat, directory, (i64)"null", 1, 1, 0) != 0 ||
                safe_workload_device_at(directory, "null", 1, 3)) exit_now(88);
        } else if (text_equal(scenario, "device-hardlink")) {
            if (sc5(SYS_linkat, directory, (i64)"null", directory, (i64)"linked", 0) != 0 ||
                safe_workload_device_at(directory, "null", 1, 3)) exit_now(88);
        } else if (text_equal(scenario, "device-wrong-rdev")) {
            if (sc3(SYS_unlinkat, directory, (i64)"null", 0) != 0 ||
                !make_node_at(directory, "null", 1, 5) ||
                safe_workload_device_at(directory, "null", 1, 3)) exit_now(88);
        } else exit_now(89);
    }
    if (sc1(SYS_close, directory) != 0) exit_now(90);
    exit_now(0);
}

__attribute__((naked, noreturn, visibility("default"))) void harness_start(void) {
    __asm__ volatile("mov %rsp,%rdi\nand $-16,%rsp\ncall harness_main\n");
}
"""


@pytest.fixture(scope="module")
def alias_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if os.environ.get("PALIMPSEST_WORKLOAD_DEV_ALIAS_DOCKER_TESTS") != "1":
        pytest.skip("requires PALIMPSEST_WORKLOAD_DEV_ALIAS_DOCKER_TESTS=1 and Docker")
    directory = tmp_path_factory.mktemp("workload-dev-alias-c")
    source = directory / "harness.c"
    binary = directory / "harness"
    source.write_text(HARNESS, encoding="utf-8")
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
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=32m",
        "--mount",
        f"type=bind,src={ROOT},dst=/repo,readonly",
        "--mount",
        f"type=bind,src={source},dst=/harness.c,readonly",
        "--mount",
        f"type=bind,src={directory},dst=/out",
        "--entrypoint",
        "/usr/local/bin/gcc",
        TOOLCHAIN,
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
        "-o",
        "/out/harness",
        "/harness.c",
    ]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return binary


@pytest.mark.parametrize(
    "scenario",
    [
        "valid",
        "existing",
        "wrong-target",
        "wrong-type",
        "hardlink",
        "wrong-owner",
        "same-length-target",
        "long-target",
        "missing",
        "extra",
        "getdents-error",
        "device-wrong-type",
        "device-wrong-mode",
        "device-wrong-owner",
        "device-hardlink",
        "device-wrong-rdev",
    ],
)
def test_real_c_private_dev_alias_policy(alias_harness: Path, scenario: str) -> None:
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
        "--cap-add",
        "MKNOD",
        "--cap-add",
        "CHOWN",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "0:0",
        "--tmpfs",
        "/fixtures:rw,nosuid,nodev,noexec,size=64k,nr_inodes=16,mode=0755",
        "--mount",
        f"type=bind,src={alias_harness},dst=/harness,readonly",
        "--entrypoint",
        "/harness",
        TOOLCHAIN,
        scenario,
    ]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""


def test_production_callsite_is_child_private_and_revalidates_after_cgroup_staging() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    prepare = source[source.index("static int prepare_workload_mount_boundary") :]
    assert prepare.index('mount, (i64)"tmpfs", (i64)"/dev"') < prepare.index("make_safe_workload_stdio_aliases()")
    assert prepare.index("make_safe_workload_stdio_aliases()") < prepare.index("drop_workload_credentials")
    cgroup = source[
        source.index("static i64 install_read_only_cgroup_view") : source.index(
            "static int prepare_workload_mount_boundary"
        )
    ]
    assert cgroup.index("SYS_unlinkat, AT_FDCWD, (i64)staging, AT_REMOVEDIR") < cgroup.index(
        "safe_workload_dev_entries()"
    )
    assert (
        '"stdin"'
        not in source[
            source.index("static int safe_workload_stdio_aliases_at") : source.index(
                "static i64 install_read_only_cgroup_view"
            )
        ]
    )
    assert (
        '"fd"'
        not in source[
            source.index("static int safe_workload_stdio_aliases_at") : source.index(
                "static i64 install_read_only_cgroup_view"
            )
        ]
    )
