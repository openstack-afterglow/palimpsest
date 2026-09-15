"""Actual-C syscall-double coverage for the authenticated guest NIC contract."""

import subprocess
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
#include <stdlib.h>
#include <string.h>
typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long u64;
typedef signed long i64;
typedef unsigned long usize;
#define SYS_close 3
#define SYS_ioctl 16
#define SYS_socket 41
#define O_CLOEXEC 02000000
#define SOCK_DGRAM 2
#define SOCK_CLOEXEC O_CLOEXEC
#define AF_INET 2
#define IFNAMSIZ 16
#define SIOCGIFNAME 0x8910
#define SIOCGIFCONF 0x8912
#define SIOCGIFFLAGS 0x8913
#define SIOCGIFADDR 0x8915
#define SIOCGIFNETMASK 0x891b
#define SIOCGIFHWADDR 0x8927
#define SIOCGIFINDEX 0x8933
#define IFF_UP 0x1
#define IFF_LOOPBACK 0x8
#define IFF_RUNNING 0x40
#define ARPHRD_ETHER 1
#define EIO 5
#define GUEST_NAMESERVER_MAX 3
#define GUEST_INTERFACE_MAX 8
struct span { const char *p; usize n; };
struct ifreq_local {
    char name[IFNAMSIZ];
    union {
        i64 align;
        int index;
        short flags;
        struct { unsigned short family; unsigned short port; u32 address; u8 pad[8]; } inet;
        struct { unsigned short family; u8 mac[14]; } hardware;
        u8 bytes[24];
    } value;
};
struct ifconf_local { int len; int pad; void *buffer; };
struct guest_network {
    int enabled;
    char interface[IFNAMSIZ];
    u32 address;
    u32 netmask;
    u32 gateway;
    u8 mac[6];
    char nameservers[GUEST_NAMESERVER_MAX][16];
    u32 nameserver_count;
};
struct child_error_local { u32 stage, error; };
enum scenario { SUCCESS, NONE_MODE, SOCKET_FAIL, INDEX_FAIL, FLAGS_DOWN, LOOPBACK_FLAGS,
 WRONG_MAC, WRONG_ADDRESS, WRONG_NETMASK, EXTRA_INTERFACE, MISSING_ROUTE, RESOLVER_FAIL };
static enum scenario scenario;
static int socket_event, close_event, resolver_calls, route_calls;
static u32 expected_address = 0x0f02000a; /* 10.0.2.15 */
static u32 expected_netmask = 0x00ffffff; /* 255.255.255.0 */
static u8 expected_mac[6] = {0x52, 0x54, 0x00, 0xab, 0xcd, 0xef};
static void set_workload_failure(struct child_error_local *failure, u32 stage, i64 error) {
    failure->stage = stage;
    failure->error = error < 0 ? (u32)-error : (u32)error;
}
static usize slen(const char *s) { usize n = 0; while (s[n]) n++; return n; }
static int bytes_equal(const char *a, const char *b, usize n) { return memcmp(a, b, n) == 0; }
static int text_equal(const char *a, const char *b) {
    usize an = slen(a), bn = slen(b);
    return an == bn && bytes_equal(a, b, an);
}
static int copy_span(char *dst, usize cap, struct span s) {
    if (s.n + 1 > cap) return 0;
    memcpy(dst, s.p, s.n);
    dst[s.n] = 0;
    return 1;
}
static int write_workload_resolver(const struct guest_network *net) {
    if (!net->nameserver_count) return 1;
    resolver_calls++;
    return scenario != RESOLVER_FAIL;
}
static int verify_default_route(const struct guest_network *net) {
    route_calls++;
    (void)net;
    return scenario != MISSING_ROUTE;
}
static i64 sc1(i64 number, i64 a) {
    if (number == SYS_close) { close_event++; return 0; }
    (void)a;
    return -EIO;
}
static i64 sc3(i64 number, i64 a, i64 b, i64 c) {
    if (number == SYS_socket) {
        socket_event++;
        return scenario == SOCKET_FAIL ? -EIO : 7;
    }
    if (number != SYS_ioctl) return -EIO;
    (void)a;
    if (b == SIOCGIFCONF) {
        struct ifconf_local *configuration = (struct ifconf_local *)c;
        struct ifreq_local *entries = (struct ifreq_local *)configuration->buffer;
        int count = scenario == NONE_MODE ? 1 : 2;
        if (scenario == EXTRA_INTERFACE) count = 3;
        memset(entries, 0, (usize)count * sizeof(entries[0]));
        memcpy(entries[0].name, "lo", 3);
        entries[0].value.inet.family = AF_INET;
        entries[0].value.inet.address = 0x0100007f;
        if (count > 1) {
            memcpy(entries[1].name, "eth0", 5);
            entries[1].value.inet.family = AF_INET;
            entries[1].value.inet.address = expected_address;
        }
        if (count > 2) {
            memcpy(entries[2].name, "eth1", 5);
            entries[2].value.inet.family = AF_INET;
            entries[2].value.inet.address = 0x0f02010a;
        }
        configuration->len = count * (int)sizeof(entries[0]);
        return 0;
    }
    {
        struct ifreq_local *request = (struct ifreq_local *)c;
        if (b == SIOCGIFINDEX) {
            if (scenario == INDEX_FAIL) return -EIO;
            request->value.index = 3;
            return 0;
        }
        if (b == SIOCGIFNAME) {
            memcpy(request->name, "eth0", 5);
            return 0;
        }
        if (b == SIOCGIFFLAGS) {
            if (scenario == FLAGS_DOWN) request->value.flags = IFF_RUNNING;
            else if (scenario == LOOPBACK_FLAGS) request->value.flags = IFF_UP | IFF_RUNNING | IFF_LOOPBACK;
            else request->value.flags = IFF_UP | IFF_RUNNING;
            return 0;
        }
        if (b == SIOCGIFHWADDR) {
            request->value.hardware.family = ARPHRD_ETHER;
            memcpy(request->value.hardware.mac, expected_mac, 6);
            if (scenario == WRONG_MAC) request->value.hardware.mac[5] ^= 0xff;
            return 0;
        }
        if (b == SIOCGIFADDR) {
            request->value.inet.family = AF_INET;
            request->value.inet.address = scenario == WRONG_ADDRESS ? 0x0f02010a : expected_address;
            return 0;
        }
        if (b == SIOCGIFNETMASK) {
            request->value.inet.family = AF_INET;
            request->value.inet.address = scenario == WRONG_NETMASK ? 0x0000ffff : expected_netmask;
            return 0;
        }
    }
    return -EIO;
}
"""

HARNESS_SUFFIX = r"""
int main(int argc, char **argv) {
    struct child_error_local failure = {0, 0};
    struct guest_network net;
    int result;
    if (argc != 2 || sizeof(struct ifreq_local) != 40) return 90;
    scenario = (enum scenario)atoi(argv[1]);
    memset(&net, 0, sizeof(net));
    memcpy(net.interface, "eth0", 5);
    net.enabled = scenario != NONE_MODE;
    net.address = expected_address;
    net.netmask = expected_netmask;
    net.gateway = 0x0202000a;
    memcpy(net.mac, expected_mac, 6);
    if (net.enabled) {
        memcpy(net.nameservers[0], "10.0.2.3", 9);
        net.nameserver_count = 1;
    }
    result = prepare_workload_network(&net, &failure);
    if (scenario == SUCCESS) {
        if (!result || failure.stage || socket_event != 1 || close_event != 1 ||
            route_calls != 1 || resolver_calls != 1) return 1;
        return 0;
    }
    if (scenario == NONE_MODE) {
        /* Without networking the guest proves loopback is the only address and
           never writes a resolver file. */
        if (!result || failure.stage || route_calls || resolver_calls) return 2;
        return 0;
    }
    if (result) return 3;
    if (scenario == RESOLVER_FAIL) {
        if (failure.stage != 46) return 4;
        return 0;
    }
    if (failure.stage != 45 || !failure.error) return 5;
    if (scenario == SOCKET_FAIL) {
        if (close_event) return 6;
        return 0;
    }
    if (!close_event) return 7;
    if (scenario != MISSING_ROUTE && route_calls) return 8;
    if (resolver_calls) return 9;
    return 0;
}
"""


@pytest.fixture(scope="module")
def network_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    extracted = _function(source, "static int prepare_workload_network")
    verify = _function(source, "static int verify_configured_interfaces")
    directory = tmp_path_factory.mktemp("workload-network")
    harness_source = directory / "harness.c"
    binary = directory / "harness"
    harness_source.write_text(HARNESS_PREFIX + verify + extracted + HARNESS_SUFFIX, encoding="utf-8")
    subprocess.run(
        ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(harness_source)],
        check=True,
        timeout=60,
    )
    return binary


@pytest.mark.parametrize("scenario", range(12))
def test_guest_nic_contract_is_verified_before_the_workload(network_harness: Path, scenario: int) -> None:
    subprocess.run([str(network_harness), str(scenario)], check=True, timeout=10)


def test_network_verification_precedes_privilege_drop_and_seccomp() -> None:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    isolation = _function(source, "static int prepare_workload_isolation")
    assert isolation.index("prepare_workload_loopback") < isolation.index("prepare_workload_network")
    for later in ("prepare_workload_securebits", "drop_workload_credentials", "install_workload_seccomp"):
        assert isolation.index("prepare_workload_network") < isolation.index(later)


def test_resolver_write_refuses_symlinked_or_irregular_targets() -> None:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    writer = _function(source, "static int write_workload_resolver")
    assert "O_NOFOLLOW" in writer and "O_CREAT | O_TRUNC" in writer
    assert "(st.mode & S_IFMT) != S_IFREG" in writer
    assert "st.nlink != 1" in writer
    assert 'sc3(SYS_open, (i64)"/etc", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0)' in writer


def test_default_route_requires_exactly_one_committed_gateway() -> None:
    source = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    route = _function(source, "static int verify_default_route")
    assert 'read_bounded_file("/proc/net/route"' in route
    assert "return defaults == 1;" in route
    assert "gateway_value != net->gateway" in route
