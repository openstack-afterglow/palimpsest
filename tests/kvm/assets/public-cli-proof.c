/* SPDX-License-Identifier: MIT
 * Freestanding Linux/x86_64 public-CLI smoke workload. No libc or dynamic loader.
 */
typedef unsigned long u64;
typedef long i64;

static i64 call4(i64 number, i64 a, i64 b, i64 c, i64 d) {
    register i64 r10 __asm__("r10") = d;
    i64 result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a), "S"(b), "d"(c), "r"(r10)
                     : "rcx", "r11", "memory");
    return result;
}

static __attribute__((noreturn)) void finish(i64 status) {
    call4(60, status, 0, 0, 0);
    for (;;) {}
}

static int same(const char *a, const char *b) {
    while (*a && *a == *b) { ++a; ++b; }
    return *a == *b;
}

static void emit(const char *message) {
    u64 length = 0;
    while (message[length]) ++length;
    while (length) {
        i64 written = call4(1, 1, (i64)message, length, 0);
        if (written == -4) continue;
        if (written <= 0) finish(100);
        message += written;
        length -= written;
    }
}

__attribute__((used, noreturn)) void proof_entry(u64 *stack) {
    char **argv = (char **)(stack + 1);
    u64 filesystem[16] = {0};
    char marker[32] = {0};
    i64 file, count, signal_fd;
    u64 mask = 1UL << 14;
    unsigned char signal_info[128] = {0};
    if (stack[0] != 2 || (!same(argv[1], "foreground") && !same(argv[1], "service"))) finish(101);
    if (call4(137, (i64)"/", (i64)filesystem, 0, 0) || filesystem[0] != 0x794c7630UL) finish(102);
    file = call4(2, (i64)"/oci-public-root", 0, 0, 0);
    if (file < 0) finish(103);
    count = call4(0, file, (i64)marker, sizeof(marker) - 1, 0);
    if (call4(3, file, 0, 0, 0) || count != 17 || !same(marker, "OCI_IS_REAL_ROOT\n")) finish(104);
    emit("PALIMPSEST_PUBLIC_OCI_ROOT\n");
    if (same(argv[1], "foreground")) finish(23);
    if (call4(14, 0, (i64)&mask, 0, sizeof(mask))) finish(105);
    signal_fd = call4(289, -1, (i64)&mask, sizeof(mask), 0);
    if (signal_fd < 0) finish(106);
    emit("PALIMPSEST_PUBLIC_SERVICE_READY\n");
    do { count = call4(0, signal_fd, (i64)signal_info, sizeof(signal_info), 0); } while (count == -4);
    if (count != sizeof(signal_info) || signal_info[0] != 15 || signal_info[1] || signal_info[2] || signal_info[3] ||
        signal_info[12] != 1 || signal_info[13] || signal_info[14] || signal_info[15]) finish(107);
    if (call4(3, signal_fd, 0, 0, 0)) finish(108);
    emit("PALIMPSEST_PUBLIC_STOP_OBSERVED\n");
    finish(42);
}

__asm__(".global _start\n_start:\nmov %rsp,%rdi\nand $-16,%rsp\ncall proof_entry\n");
