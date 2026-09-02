/* SPDX-License-Identifier: MIT
 * Deterministic workload used only by the Palimpsest PID 1 qualification.
 *
 * This freestanding Linux x86_64 program deliberately emits no qualification
 * marker.  The supervising PID 1 must establish execution, signal forwarding,
 * and child status itself.  A successful main process exits 42 and its orphaned
 * process-group member exits 43 after both observe SIGTERM sent by PID 1.
 */

typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long u64;
typedef signed long i64;
typedef unsigned long usize;

#define SYS_read 0
#define SYS_write 1
#define SYS_open 2
#define SYS_close 3
#define SYS_rt_sigprocmask 14
#define SYS_getpid 39
#define SYS_fork 57
#define SYS_exit 60
#define SYS_kill 62
#define SYS_getcwd 79
#define SYS_getuid 102
#define SYS_getgid 104
#define SYS_getppid 110
#define SYS_getpgrp 111
#define SYS_getgroups 115
#define SYS_waitid 247
#define SYS_signalfd4 289
#define SYS_pipe2 293

#define O_RDONLY 0
#define O_CLOEXEC 02000000
#define SIG_BLOCK 0
#define SIGTERM 15
#define P_PID 1
#define WEXITED 4
#define WNOWAIT 0x01000000
#define SIGNALLFD_INFO_BYTES 128
#define PID1_STATUS_MAX 8192
#define EINTR 4

#define MAIN_SUCCESS 42
#define DESCENDANT_SUCCESS 43
#define FAILURE_BASE 100

static inline i64 sc0(i64 n) {
    i64 r;
    __asm__ volatile("syscall" : "=a"(r) : "a"(n) : "rcx", "r11", "memory");
    return r;
}

static inline i64 sc1(i64 n, i64 a) {
    i64 r;
    __asm__ volatile("syscall" : "=a"(r) : "a"(n), "D"(a) : "rcx", "r11", "memory");
    return r;
}

static inline i64 sc2(i64 n, i64 a, i64 b) {
    i64 r;
    __asm__ volatile("syscall" : "=a"(r) : "a"(n), "D"(a), "S"(b) : "rcx", "r11", "memory");
    return r;
}

static inline i64 sc3(i64 n, i64 a, i64 b, i64 c) {
    i64 r;
    __asm__ volatile("syscall" : "=a"(r) : "a"(n), "D"(a), "S"(b), "d"(c) : "rcx", "r11", "memory");
    return r;
}

static inline i64 sc4(i64 n, i64 a, i64 b, i64 c, i64 d) {
    register i64 r10 __asm__("r10") = d;
    i64 r;
    __asm__ volatile("syscall"
                     : "=a"(r)
                     : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10)
                     : "rcx", "r11", "memory");
    return r;
}

static inline i64 sc5(i64 n, i64 a, i64 b, i64 c, i64 d, i64 e) {
    register i64 r10 __asm__("r10") = d;
    register i64 r8 __asm__("r8") = e;
    i64 r;
    __asm__ volatile("syscall"
                     : "=a"(r)
                     : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10), "r"(r8)
                     : "rcx", "r11", "memory");
    return r;
}

static usize slen(const char *value) {
    usize size = 0;
    while (value[size]) size++;
    return size;
}

static int same_text(const char *actual, const char *expected) {
    usize index;
    usize actual_size = slen(actual);
    usize expected_size = slen(expected);
    u8 difference = (u8)(actual_size != expected_size);
    if (actual_size != expected_size) return 0;
    for (index = 0; index < actual_size; index++) difference |= (u8)actual[index] ^ (u8)expected[index];
    return difference == 0;
}

static __attribute__((noreturn)) void exit_now(int status) {
    sc1(SYS_exit, status);
    for (;;) {}
}

static int read_exact_file(const char *path, const char *expected) {
    char buffer[64];
    usize expected_size = slen(expected);
    usize used = 0;
    int descriptor;
    if (expected_size >= sizeof(buffer)) return 0;
    descriptor = (int)sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC, 0);
    if (descriptor < 0) return 0;
    while (used < expected_size) {
        i64 count = sc3(SYS_read, descriptor, (i64)(buffer + used), (i64)(expected_size - used));
        if (count <= 0 || (usize)count > expected_size - used) {
            sc1(SYS_close, descriptor);
            return 0;
        }
        used += (usize)count;
    }
    if (sc3(SYS_read, descriptor, (i64)(buffer + used), 1) != 0 || sc1(SYS_close, descriptor) != 0) return 0;
    buffer[used] = 0;
    return same_text(buffer, expected);
}

static int exact_line_count(const char *payload, usize payload_size, const char *expected) {
    usize expected_size = slen(expected);
    usize start = 0;
    usize index;
    int count = 0;
    for (index = 0; index < payload_size; index++) {
        usize line_size;
        usize offset;
        u8 difference = 0;
        if (payload[index] != '\n') continue;
        line_size = index - start + 1;
        if (line_size == expected_size) {
            for (offset = 0; offset < line_size; offset++)
                difference |= (u8)payload[start + offset] ^ (u8)expected[offset];
            if (difference == 0) count++;
        }
        start = index + 1;
    }
    return count;
}

static int verify_pid1_credentials(void) {
    static const char uid_line[] = "Uid:\t65534\t65534\t65534\t65534\n";
    static const char gid_line[] = "Gid:\t65534\t65534\t65534\t65534\n";
    static const char groups_line[] = "Groups:\t \n";
    char status[PID1_STATUS_MAX];
    usize used = 0;
    int descriptor = (int)sc3(SYS_open, (i64)"/proc/1/status", O_RDONLY | O_CLOEXEC, 0);
    int eof = 0;
    if (descriptor < 0) return 0;
    while (used < sizeof(status)) {
        i64 count = sc3(SYS_read, descriptor, (i64)(status + used), (i64)(sizeof(status) - used));
        if (count == -EINTR) continue;
        if (count < 0 || (usize)count > sizeof(status) - used) {
            sc1(SYS_close, descriptor);
            return 0;
        }
        if (count == 0) {
            eof = 1;
            break;
        }
        used += (usize)count;
    }
    if (!eof) {
        char extra;
        i64 count;
        do count = sc3(SYS_read, descriptor, (i64)&extra, 1); while (count == -EINTR);
        if (count != 0) {
            sc1(SYS_close, descriptor);
            return 0;
        }
    }
    if (sc1(SYS_close, descriptor) != 0) return 0;
    return exact_line_count(status, used, uid_line) == 1 &&
           exact_line_count(status, used, gid_line) == 1 &&
           exact_line_count(status, used, groups_line) == 1;
}

static int block_sigterm(void) {
    u64 mask = 1ul << (SIGTERM - 1);
    return sc4(SYS_rt_sigprocmask, SIG_BLOCK, (i64)&mask, 0, sizeof(mask)) == 0;
}

static int new_sigterm_fd(void) {
    u64 mask = 1ul << (SIGTERM - 1);
    return (int)sc4(SYS_signalfd4, -1, (i64)&mask, sizeof(mask), O_CLOEXEC);
}

static int wait_for_pid1_sigterm(int descriptor) {
    u8 info[SIGNALLFD_INFO_BYTES];
    usize used = 0;
    u32 signal_number;
    u32 sender_pid;
    while (used < sizeof(info)) {
        i64 count = sc3(SYS_read, descriptor, (i64)(info + used), (i64)(sizeof(info) - used));
        if (count <= 0 || (usize)count > sizeof(info) - used) return 0;
        used += (usize)count;
    }
    signal_number = (u32)info[0] | ((u32)info[1] << 8) | ((u32)info[2] << 16) | ((u32)info[3] << 24);
    sender_pid = (u32)info[12] | ((u32)info[13] << 8) | ((u32)info[14] << 16) | ((u32)info[15] << 24);
    return signal_number == SIGTERM && sender_pid == 1;
}

static int verify_invocation(u64 argc, char **argv, char **environment) {
    static const char sentinel[] = "palimpsest-oci-root-workload-proof-v1\n";
    char cwd[64];
    i64 cwd_size;
    if (argc != 4 || !same_text(argv[0], "/.__palimpsest_workload_proof_v1") ||
        !same_text(argv[1], "palimpsest-argv-one") || !same_text(argv[2], "") ||
        !same_text(argv[3], "line\nbreak")) return 0;
    if (!environment[0] || !environment[1] || environment[2] ||
        !same_text(environment[0], "PALIMPSEST_PROOF_ENV=value with spaces") ||
        !same_text(environment[1], "PALIMPSEST_PROOF_EMPTY=")) return 0;
    if (sc0(SYS_getpid) <= 1 || sc0(SYS_getppid) != 1 || sc0(SYS_getpgrp) != sc0(SYS_getpid)) return 0;
    if (sc0(SYS_getuid) != 65534 || sc0(SYS_getgid) != 65534 || sc2(SYS_getgroups, 0, 0) != 0) return 0;
    cwd_size = sc2(SYS_getcwd, (i64)cwd, sizeof(cwd));
    if (cwd_size <= 0 || (usize)cwd_size > sizeof(cwd) || !same_text(cwd, "/proof/workdir")) return 0;
    return read_exact_file("/.__palimpsest_oci_root_workload_proof_v1", sentinel) &&
           read_exact_file("/proc/self/root/.__palimpsest_oci_root_workload_proof_v1", sentinel) &&
           verify_pid1_credentials();
}

static __attribute__((noreturn)) void run_descendant(int ready_writer, int completion_writer,
                                                     i64 expected_group) {
    int signal_fd;
    u8 ready = 1;
    u8 completed = 1;
    if (sc0(SYS_getppid) <= 1 || sc0(SYS_getpgrp) != expected_group) exit_now(FAILURE_BASE + 5);
    signal_fd = new_sigterm_fd();
    if (signal_fd < 0 || sc3(SYS_write, ready_writer, (i64)&ready, 1) != 1 ||
        sc1(SYS_close, ready_writer) != 0) exit_now(FAILURE_BASE + 6);
    if (!wait_for_pid1_sigterm(signal_fd) || sc1(SYS_close, signal_fd) != 0 ||
        sc3(SYS_write, completion_writer, (i64)&completed, 1) != 1 ||
        sc1(SYS_close, completion_writer) != 0) exit_now(FAILURE_BASE + 7);
    exit_now(DESCENDANT_SUCCESS);
}

static __attribute__((noreturn, used)) void start_c(u64 *stack) {
    u64 argc = stack[0];
    char **argv = (char **)(stack + 1);
    char **environment = argv + argc + 1;
    int ready_pipe[2], completion_pipe[2];
    int signal_fd;
    i64 main_pid;
    i64 child;
    u8 ready = 0;
    u8 child_info[128];
    if (!verify_invocation(argc, argv, environment)) exit_now(FAILURE_BASE + 1);
    if (!block_sigterm()) exit_now(FAILURE_BASE + 2);
    signal_fd = new_sigterm_fd();
    if (signal_fd < 0 || sc2(SYS_pipe2, (i64)ready_pipe, O_CLOEXEC) != 0 ||
        sc2(SYS_pipe2, (i64)completion_pipe, O_CLOEXEC) != 0) exit_now(FAILURE_BASE + 3);
    main_pid = sc0(SYS_getpid);
    child = sc0(SYS_fork);
    if (child < 0) exit_now(FAILURE_BASE + 4);
    if (child == 0) {
        sc1(SYS_close, ready_pipe[0]);
        sc1(SYS_close, completion_pipe[0]);
        sc1(SYS_close, signal_fd);
        run_descendant(ready_pipe[1], completion_pipe[1], main_pid);
    }
    if (sc1(SYS_close, ready_pipe[1]) != 0 || sc1(SYS_close, completion_pipe[1]) != 0 ||
        sc3(SYS_read, ready_pipe[0], (i64)&ready, 1) != 1 ||
        ready != 1 || sc1(SYS_close, ready_pipe[0]) != 0) exit_now(FAILURE_BASE + 8);
    if (sc2(SYS_kill, 1, SIGTERM) != 0) exit_now(FAILURE_BASE + 9);
    ready = 0;
    if (!wait_for_pid1_sigterm(signal_fd) || sc1(SYS_close, signal_fd) != 0 ||
        sc3(SYS_read, completion_pipe[0], (i64)&ready, 1) != 1 || ready != 1 ||
        sc1(SYS_close, completion_pipe[0]) != 0 ||
        sc5(SYS_waitid, P_PID, child, (i64)child_info, WEXITED | WNOWAIT, 0) != 0)
        exit_now(FAILURE_BASE + 10);
    exit_now(MAIN_SUCCESS);
}

__attribute__((naked, noreturn, visibility("default"))) void _start(void) {
    __asm__ volatile("mov %rsp,%rdi\n"
                     "and $-16,%rsp\n"
                     "call start_c\n");
}
