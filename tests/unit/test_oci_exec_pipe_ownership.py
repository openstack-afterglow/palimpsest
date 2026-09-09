"""Fault-injection tests for the real stage-1 remote-exec pipe callsite.

The C harness extracts the production functions from ``guest/stage1/init.c`` at
test time and supplies deterministic syscall/cgroup doubles around them.  This
keeps the test executable while avoiding a second implementation of the pipe
ownership and pre-fork cleanup logic.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLCHAIN = "docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1"


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    raise AssertionError(f"unterminated production function: {signature}")


_HARNESS_PREFIX = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef unsigned int u32;
typedef unsigned long u64;
typedef signed long i64;
typedef unsigned long usize;

#define SYS_close 3
#define SYS_fstat 5
#define SYS_fcntl 72
#define SYS_fchown 93
#define SYS_setpgid 109
#define SYS_pipe2 293
#define SYS_fork 57
#define SYS_kill 62
#define O_NONBLOCK 04000
#define O_CLOEXEC 02000000
#define S_IFMT 0170000
#define S_IFIFO 0010000
#define SIGKILL 9

struct stat_local { u64 dev, ino, nlink; u32 mode, uid, gid; };
struct guest_process { u32 uid, gid; };
struct cgroup_node { int unused; };
struct exec_session { struct cgroup_node leaf; u32 id; char name[32]; int active; };
struct workload_agent { int root_fd; struct cgroup_node parent; u32 next_session_id, active_sessions; };
struct lifecycle_session { int unused; };
struct remote_exec_job {
    struct guest_process process; struct exec_session session;
    u64 request_id, deadline, cleanup_deadline; i64 pid;
    int pending, active, phase, reaped, status, reason, killed;
    int output[2], isolation, errors, release; u32 bytes[2];
    usize wire_used, wire_sent; unsigned char wire[32];
};
static struct remote_exec_job remote_exec;

enum scenario {
    SUCCESS, INITIAL_FSTAT, PARTIAL_FCHOWN, POST_FSTAT, POST_IDENTITY,
    POST_OWNER, POST_GID, POST_MODE, POST_TYPE, PRE_NONZERO_IDENTITY,
    PRE_WRONG_GID, PRE_SAME_PAIR
};
static enum scenario scenario;
static int open_fd[20], closed[20], owner_uid[20], owner_gid[20];
static int status_flags[20], pipe_mode[20];
static int pipe_count, fstat_count, fchown_count, fork_count, remove_count;
static int pipe_cloexec_count, fcntl_count, event_number;
static int ownership_event, fork_event;

static int create_exec_session(struct workload_agent *agent, struct exec_session *session) {
    session->active = 1; session->id = 2; agent->active_sessions = 1; return 1;
}
static int remove_empty_exec_session(struct workload_agent *agent, struct exec_session *session) {
    remove_count++; session->active = 0; agent->active_sessions = 0; return 1;
}
static int move_pid_to_exec_session(struct workload_agent *agent, struct exec_session *session, i64 pid) {
    (void)agent; (void)session; return pid == 4242;
}
static __attribute__((noreturn)) void exec_child(struct lifecycle_session *lifecycle,
        int error, int isolation, int release, int output, int errors) {
    (void)lifecycle; (void)error; (void)isolation; (void)release; (void)output; (void)errors; abort();
}

static i64 sc0(i64 call) {
    if (call != SYS_fork) return -1;
    fork_count++; fork_event = ++event_number; return 4242;
}
static i64 sc1(i64 call, i64 fd) {
    if (call != SYS_close || fd < 10 || fd >= 20) return -1;
    closed[fd]++; open_fd[fd] = 0; return 0;
}
static i64 sc2(i64 call, i64 a, i64 b) {
    if (call == SYS_pipe2) {
        int *pair = (int *)(uintptr_t)a; int left = 10 + pipe_count * 2;
        if (b != O_CLOEXEC) return -1;
        pipe_cloexec_count++; pair[0] = left; pair[1] = left + 1;
        open_fd[left] = open_fd[left + 1] = 1;
        owner_uid[left] = owner_uid[left + 1] = 0;
        owner_gid[left] = owner_gid[left + 1] = 0;
        pipe_mode[left] = pipe_mode[left + 1] = 0600;
        pipe_count++; return 0;
    }
    if (call == SYS_fstat) {
        struct stat_local *st = (struct stat_local *)(uintptr_t)b; int fd = (int)a;
        fstat_count++;
        if (!open_fd[fd] || (scenario == INITIAL_FSTAT && fstat_count == 1) ||
            (scenario == POST_FSTAT && fstat_count == 5)) return -5;
        memset(st, 0, sizeof(*st)); st->dev = 7; st->ino = 100 + (fd - 10) / 2;
        st->mode = S_IFIFO | pipe_mode[fd]; st->uid = owner_uid[fd]; st->gid = owner_gid[fd];
        if (scenario == PRE_NONZERO_IDENTITY && fstat_count == 1) st->ino = 0;
        if (scenario == PRE_WRONG_GID && fstat_count == 1) st->gid = 9;
        if (scenario == PRE_SAME_PAIR && fstat_count <= 4 && fd >= 12) st->ino = 100;
        if (fstat_count > 4) {
            if (scenario == POST_IDENTITY && fstat_count == 5) st->ino++;
            if (scenario == POST_OWNER && fstat_count == 5) st->uid++;
            if (scenario == POST_GID && fstat_count == 5) st->gid++;
            if (scenario == POST_MODE && fstat_count == 5) st->mode = S_IFIFO | 0400;
            if (scenario == POST_TYPE && fstat_count == 5) st->mode = 0100000 | 0600;
        }
        return 0;
    }
    if (call == SYS_kill || call == SYS_setpgid) return 0;
    return -1;
}
static i64 sc3(i64 call, i64 a, i64 b, i64 c) {
    if (call == SYS_fcntl) {
        int fd = (int)a;
        if (b != 4 || fd < 10 || fd >= 20) return -1;
        fcntl_count++; status_flags[fd] = (int)c;
        return 0;
    }
    if (call == SYS_fchown) {
        int fd = (int)a, mate = fd ^ 1;
        fchown_count++;
        if (!ownership_event) ownership_event = ++event_number;
        if (scenario == PARTIAL_FCHOWN && fchown_count == 2) return -1;
        owner_uid[fd] = owner_uid[mate] = (int)b;
        owner_gid[fd] = owner_gid[mate] = (int)c;
        return 0;
    }
    return -1;
}
"""


_HARNESS_SUFFIX = r"""
static int failure_state_ok(struct workload_agent *agent) {
    int fd;
    if (fork_count || pipe_count != 5 || remove_count != 1 || agent->active_sessions ||
        remote_exec.active || remote_exec.session.active || remote_exec.wire_used ||
        remote_exec.output[0] != -1 || remote_exec.output[1] != -1) return 0;
    for (fd = 10; fd < 20; fd++) if (open_fd[fd] || closed[fd] != 1) return 0;
    return 1;
}

int main(int argc, char **argv) {
    struct workload_agent agent; struct lifecycle_session lifecycle; int result, fd;
    if (argc != 2) return 90;
    scenario = (enum scenario)atoi(argv[1]);
    memset(&agent, 0, sizeof(agent)); memset(&lifecycle, 0, sizeof(lifecycle));
    memset(&remote_exec, 0, sizeof(remote_exec));
    remote_exec.pending = 1; remote_exec.process.uid = 101; remote_exec.process.gid = 202;
    result = start_remote_exec(&agent, &lifecycle);
    if (scenario != SUCCESS) return result || !failure_state_ok(&agent) ? 1 : 0;
    if (!result || fork_count != 1 || ownership_event <= 0 || ownership_event >= fork_event ||
        pipe_cloexec_count != 5 || fcntl_count != 6 || fchown_count != 4 || fstat_count != 8 ||
        remote_exec.process.uid != 101 || remote_exec.process.gid != 202 ||
        remote_exec.pid != 4242 || !remote_exec.active || !remote_exec.session.active ||
        agent.active_sessions != 1 || remote_exec.wire_used) return 2;
    for (fd = 10; fd < 20; fd++) {
        int parent_retained = fd == 10 || fd == 12 || fd == 14 || fd == 16 || fd == 19;
        if (open_fd[fd] != parent_retained || closed[fd] != !parent_retained) return 3;
        if (fd < 14 && (owner_uid[fd] != 101 || owner_gid[fd] != 202)) return 4;
        if (fd >= 14 && (owner_uid[fd] != 0 || owner_gid[fd] != 0)) return 5;
        if (pipe_mode[fd] != 0600) return 7;
    }
    if (status_flags[10] != O_NONBLOCK || status_flags[12] != O_NONBLOCK ||
        status_flags[14] != O_NONBLOCK || status_flags[16] != O_NONBLOCK ||
        status_flags[18] != 0 || status_flags[11] != 0 || status_flags[13] != 0 ||
        status_flags[15] != 0 || status_flags[17] != 0 || status_flags[19] != 0) return 8;
    if (remote_exec.output[0] != 10 || remote_exec.output[1] != 12 ||
        remote_exec.isolation != 14 || remote_exec.errors != 16 || remote_exec.release != 19) return 6;
    return 0;
}
"""


@pytest.fixture(scope="module")
def ownership_harness(tmp_path_factory):
    docker = os.environ.get("PALIMPSEST_GUEST_EXEC_DOCKER_TESTS") == "1"
    if not docker and not sys.platform.startswith("linux"):
        pytest.skip("requires Linux host cc or PALIMPSEST_GUEST_EXEC_DOCKER_TESTS=1")
    repository = Path(__file__).resolve().parents[2]
    source = (repository / "guest/stage1/init.c").read_text(encoding="utf-8")
    extracted = "\n\n".join(
        (_function(source, "static int own_exec_output_pipes"), _function(source, "static int start_remote_exec"))
    )
    directory = tmp_path_factory.mktemp("exec-pipe-ownership")
    harness_source = directory / "harness.c"
    harness_source.write_text(_HARNESS_PREFIX + extracted + _HARNESS_SUFFIX, encoding="utf-8")
    flags = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror"]
    if docker:
        build_base = [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--pids-limit",
            "32",
            "--memory",
            "128m",
            "--cpus",
            "0.5",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=16m",
            "--mount",
            f"type=bind,src={directory},dst=/out",
        ]
        build = [
            *build_base,
            "--entrypoint",
            "/usr/local/bin/gcc",
            _TOOLCHAIN,
            *flags,
            "-o",
            "/out/harness",
            "/out/harness.c",
        ]
        command = [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "16",
            "--memory",
            "64m",
            "--cpus",
            "0.25",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=8m",
            "--mount",
            f"type=bind,src={directory / 'harness'},dst=/harness,readonly",
            "--entrypoint",
            "/harness",
            _TOOLCHAIN,
        ]
    else:
        compiler = shutil.which("cc")
        assert compiler, "remote-exec ownership harness needs cc"
        build = [compiler, *flags, "-o", str(directory / "harness"), str(harness_source)]
        command = [str(directory / "harness")]
    compiled = subprocess.run(build, capture_output=True, timeout=60, check=False)
    assert compiled.returncode == 0, compiled.stderr.decode(errors="replace")
    return command


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(1, id="initial-fstat"),
        pytest.param(2, id="partial-fchown-after-one-change"),
        pytest.param(3, id="post-fstat"),
        pytest.param(4, id="post-identity"),
        pytest.param(5, id="post-owner"),
        pytest.param(6, id="post-gid"),
        pytest.param(7, id="post-mode"),
        pytest.param(8, id="post-type"),
        pytest.param(9, id="pre-zero-identity"),
        pytest.param(10, id="pre-wrong-gid"),
        pytest.param(11, id="same-pipe-for-both-streams"),
    ],
)
def test_start_remote_exec_pipe_faults_fail_before_fork_and_close_every_endpoint(ownership_harness, scenario):
    result = subprocess.run([*ownership_harness, str(scenario)], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_start_remote_exec_success_preserves_control_pipes_and_parent_fd_policy(ownership_harness):
    result = subprocess.run([*ownership_harness, "0"], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
