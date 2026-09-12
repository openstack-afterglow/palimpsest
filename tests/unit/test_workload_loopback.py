"""Actual-C syscall-double coverage for bounded workload loopback setup."""

import platform
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


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
    raise AssertionError(f"unterminated function: {signature}")


HARNESS_PREFIX = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned char u8;
typedef unsigned int u32;
typedef signed long i64;
#define SYS_close 3
#define SYS_ioctl 16
#define SYS_socket 41
#define O_CLOEXEC 02000000
#define SOCK_DGRAM 2
#define SOCK_CLOEXEC O_CLOEXEC
#define AF_INET 2
#define IFNAMSIZ 16
#define SIOCGIFNAME 0x8910
#define SIOCGIFFLAGS 0x8913
#define SIOCSIFFLAGS 0x8914
#define SIOCGIFINDEX 0x8933
#define IFF_UP 0x1
#define IFF_LOOPBACK 0x8
#define IFF_RUNNING 0x40
#define EIO 5
struct ifreq_local { char name[IFNAMSIZ]; union { i64 align; int index; short flags; u8 bytes[24]; } value; };
struct child_error_local { u32 stage, error; };
enum scenario { SUCCESS, ALREADY_UP, SOCKET_FAIL, INDEX_FAIL, ZERO_INDEX, NAME_FAIL,
 WRONG_NAME, FLAGS_FAIL, WRONG_FLAGS, SET_FAIL, CHANGED_INDEX, FINAL_FLAGS_FAIL,
 FINAL_NOT_UP, CLOSE_FAIL };
static enum scenario scenario;
static int event, socket_event, close_event, set_event, ioctl_count, flags_count, index_count;
static void set_workload_failure(struct child_error_local *failure, u32 stage, i64 error) {
 failure->stage=stage; failure->error=error < 0 ? (u32)-error : (u32)error;
}
static i64 sc1(i64 call, i64 fd) {
 if (call != SYS_close || fd != 17) return -99;
 close_event=++event; return scenario == CLOSE_FAIL ? -9 : 0;
}
static i64 sc3(i64 call, i64 a, i64 b, i64 c) {
 struct ifreq_local *r=(struct ifreq_local *)(uintptr_t)c;
 if (call == SYS_socket) {
  if (a != AF_INET || b != (SOCK_DGRAM|SOCK_CLOEXEC) || c != 0) return -98;
  socket_event=++event; return scenario == SOCKET_FAIL ? -13 : 17;
 }
 if (call != SYS_ioctl || a != 17) return -97;
 ioctl_count++; ++event;
 if (b == SIOCGIFINDEX) {
  index_count++;
  if (r->name[0]!='l'||r->name[1]!='o'||r->name[2]) return -96;
  if (scenario == INDEX_FAIL && index_count == 1) return -6;
  r->value.index = scenario == ZERO_INDEX ? 0 :
      (scenario == CHANGED_INDEX && index_count == 2 ? 8 : 7); return 0;
 }
 if (b == SIOCGIFNAME) {
  if (r->value.index != 7) return -95;
  if (scenario == NAME_FAIL) return -6;
  memset(r->name, 0, IFNAMSIZ); memcpy(r->name, scenario == WRONG_NAME ? "xx" : "lo", 3); return 0;
 }
 if (b == SIOCGIFFLAGS) {
  flags_count++;
  if (scenario == FLAGS_FAIL && flags_count == 1) return -6;
  if (scenario == FINAL_FLAGS_FAIL && flags_count == 2) return -6;
  if (scenario == WRONG_FLAGS && flags_count == 1) r->value.flags=IFF_LOOPBACK|0x2;
  else if (scenario == ALREADY_UP) r->value.flags=IFF_UP|IFF_LOOPBACK|IFF_RUNNING;
  else if (scenario == FINAL_NOT_UP && flags_count == 2) r->value.flags=IFF_LOOPBACK;
  else r->value.flags=flags_count == 1 ? IFF_LOOPBACK : IFF_UP|IFF_LOOPBACK|IFF_RUNNING;
  return 0;
 }
 if (b == SIOCSIFFLAGS) {
  set_event=event;
  if (r->name[0]!='l'||r->name[1]!='o'||r->name[2] || r->value.flags!=(IFF_UP|IFF_LOOPBACK)) return -94;
  return scenario == SET_FAIL ? -1 : 0;
 }
 return -93;
}
"""

HARNESS_SUFFIX = r"""
int main(int argc, char **argv) {
 struct child_error_local failure={0,0}; int result;
 if (argc != 2 || sizeof(struct ifreq_local) != 40) return 90;
 scenario=(enum scenario)atoi(argv[1]); result=prepare_workload_loopback(&failure);
 if (scenario == SUCCESS || scenario == ALREADY_UP) {
  if (!result || failure.stage || socket_event != 1 || close_event <= socket_event ||
      index_count != 2 || flags_count != 2 || (scenario == SUCCESS ? set_event == 0 : set_event != 0)) return 1;
 } else if (result || failure.stage != 44 || !failure.error) return 2;
 if (scenario == SOCKET_FAIL) { if (close_event) return 3; }
 else if (close_event == 0) return 4;
 return 0;
}
"""


@pytest.fixture(scope="module")
def loopback_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    extracted = _function(source, "static int prepare_workload_loopback")
    directory = tmp_path_factory.mktemp("workload-loopback")
    harness_source = directory / "harness.c"
    binary = directory / "harness"
    harness_source.write_text(HARNESS_PREFIX + extracted + HARNESS_SUFFIX, encoding="utf-8")
    subprocess.run(
        ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(harness_source)],
        check=True,
        timeout=30,
    )
    return binary


@pytest.mark.parametrize("scenario", range(14))
def test_loopback_setup_syscall_contract(loopback_harness: Path, scenario: int) -> None:
    subprocess.run([str(loopback_harness), str(scenario)], check=True, timeout=10)


def test_loopback_precedes_privilege_drop_and_seccomp() -> None:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    isolation = _function(source, "static int prepare_workload_isolation")
    assert isolation.index("prepare_workload_mount_boundary") < isolation.index("prepare_workload_loopback")
    assert isolation.index("prepare_workload_loopback") < isolation.index("prepare_workload_securebits")
    assert isolation.index("prepare_workload_loopback") < isolation.index("drop_workload_credentials")
    assert isolation.index("prepare_workload_loopback") < isolation.index("install_workload_seccomp")


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or platform.machine() not in {"x86_64", "amd64"},
    reason="Linux x86_64 UAPI ABI check",
)
def test_production_loopback_abi_matches_linux_uapi(tmp_path: Path) -> None:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    for spelling in (
        "#define SYS_socket 41",
        "#define IFNAMSIZ 16",
        "#define SIOCGIFNAME 0x8910",
        "#define SIOCGIFFLAGS 0x8913",
        "#define SIOCSIFFLAGS 0x8914",
        "#define SIOCGIFINDEX 0x8933",
        "#define IFF_UP 0x1",
        "#define IFF_LOOPBACK 0x8",
        "#define IFF_RUNNING 0x40",
    ):
        assert spelling in source
    assert "u8 bytes[24]" in source
    check = tmp_path / "abi.c"
    binary = tmp_path / "abi"
    check.write_text(
        "#define _DEFAULT_SOURCE\n#include <stddef.h>\n#include <sys/socket.h>\n#include <sys/ioctl.h>\n"
        "#include <net/if.h>\n#include <linux/sockios.h>\n#include <sys/syscall.h>\n"
        "_Static_assert(SYS_socket==41,\"socket\");\n"
        "_Static_assert(AF_INET==2 && SOCK_DGRAM==2,\"socket ABI\");\n"
        "_Static_assert(IFNAMSIZ==16 && sizeof(struct ifreq)==40,\"ifreq size\");\n"
        "_Static_assert(offsetof(struct ifreq,ifr_ifindex)==16,\"ifindex offset\");\n"
        "_Static_assert(offsetof(struct ifreq,ifr_flags)==16,\"flags offset\");\n"
        "_Static_assert(SIOCGIFNAME==0x8910 && SIOCGIFFLAGS==0x8913 && "
        "SIOCSIFFLAGS==0x8914 && SIOCGIFINDEX==0x8933,\"ioctls\");\n"
        "_Static_assert(IFF_UP==1 && IFF_LOOPBACK==8 && IFF_RUNNING==64,\"flags\");\n"
        "int main(void){return 0;}\n",
        encoding="ascii",
    )
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", binary, check], check=True, timeout=30)
    subprocess.run([binary], check=True, timeout=10)
