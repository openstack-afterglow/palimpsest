/* SPDX-License-Identifier: MIT
 * Deterministic workload used only by the Palimpsest PID 1 qualification.
 *
 * This freestanding Linux x86_64 program deliberately emits no qualification
 * marker.  The supervising PID 1 must establish execution, signal forwarding,
 * and child status itself. A successful main process exits 42, its cooperative
 * process-group member exits 43, and its stubborn member requires cgroup.kill.
 */

typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long u64;
typedef signed long i64;
typedef unsigned long usize;

#define SYS_read 0
#define SYS_write 1
#define SYS_pause 34
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
#define SYS_signalfd4 289
#define SYS_pipe2 293
#define SYS_getdents64 217
#define SYS_fstat 5
#define SYS_mount 165
#define SYS_mknod 133
#define SYS_unshare 272
#define SYS_newfstatat 262

#define O_RDONLY 0
#define O_WRONLY 1
#define O_CLOEXEC 02000000
#define O_NONBLOCK 04000
#define O_NOFOLLOW 0400000
#define O_DIRECTORY 0200000
#define SIG_BLOCK 0
#define SIGTERM 15
#define SIGNALLFD_INFO_BYTES 128
#define PID1_STATUS_MAX 8192
#define EINTR 4
#define EACCES 13
#define EPERM 1
#define EROFS 30
#define ENOSPC 28
#define S_IFMT 0170000
#define S_IFCHR 0020000
#define CLONE_NEWNS 0x00020000
#define AT_FDCWD -100
#define AT_SYMLINK_NOFOLLOW 0x100

#define MAIN_SUCCESS 42
#define DESCENDANT_SUCCESS 43
#define FAILURE_BASE 100
#define SIGNAL_ARMED_MARKER "palimpsest workload proof: signal handlers armed\n"
#define STOP_OBSERVED_MARKER "palimpsest workload proof: stop observed\n"

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

struct timespec_local { i64 sec; i64 nsec; };
struct stat_local {
    u64 dev; u64 ino; u64 nlink; u32 mode; u32 uid; u32 gid; u32 pad0; u64 rdev;
    i64 size; i64 blksize; i64 blocks;
    struct timespec_local atime; struct timespec_local mtime; struct timespec_local ctime;
    i64 reserved[3];
};

static usize slen(const char *value) {
    usize size = 0;
    while (value[size]) size++;
    return size;
}

static int write_all(int descriptor, const char *value) {
    usize size = slen(value), used = 0;
    while (used < size) {
        i64 count = sc3(SYS_write, descriptor, (i64)(value + used), size - used);
        if (count == -EINTR) continue;
        if (count <= 0 || (usize)count > size - used) return 0;
        used += (usize)count;
    }
    return 1;
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

static int read_file(const char *path, char *buffer, usize capacity, usize *used_out) {
    usize used = 0;
    int descriptor = (int)sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
    if (descriptor < 0) return 0;
    while (used < capacity) {
        i64 count = sc3(SYS_read, descriptor, (i64)(buffer + used), capacity - used);
        if (count == -EINTR) continue;
        if (count < 0 || (usize)count > capacity - used) { sc1(SYS_close, descriptor); return 0; }
        if (!count) { *used_out = used; return sc1(SYS_close, descriptor) == 0; }
        used += (usize)count;
    }
    sc1(SYS_close, descriptor);
    return 0;
}

static int verify_capabilityless_boundary(void) {
    static const char *lines[] = {
        "CapInh:\t0000000000000000\n", "CapPrm:\t0000000000000000\n",
        "CapEff:\t0000000000000000\n", "CapBnd:\t0000000000000000\n",
        "CapAmb:\t0000000000000000\n", "NoNewPrivs:\t1\n", "Seccomp:\t2\n",
    };
    char status[PID1_STATUS_MAX];
    usize used = 0, i;
    if (!read_file("/proc/self/status", status, sizeof(status), &used)) return 0;
    for (i = 0; i < sizeof(lines) / sizeof(lines[0]); i++)
        if (exact_line_count(status, used, lines[i]) != 1) return 0;
    return 1;
}

static int allowed_dev_name(const u8 *name, usize size) {
    static const char *allowed[] = {"null", "zero", "full", "random", "urandom", "tty"};
    usize i;
    for (i = 0; i < sizeof(allowed) / sizeof(allowed[0]); i++)
        if (size == slen(allowed[i])) {
            usize at;
            u8 difference = 0;
            for (at = 0; at < size; at++) difference |= name[at] ^ (u8)allowed[i][at];
            if (!difference) return (int)i + 1;
        }
    return 0;
}

static int verify_private_devices(void) {
    u8 entries[2048];
    u32 seen = 0;
    i64 directory = sc3(SYS_open, (i64)"/dev", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (directory < 0) return 0;
    for (;;) {
        i64 count = sc3(SYS_getdents64, directory, (i64)entries, sizeof(entries));
        usize offset = 0;
        if (count < 0) { sc1(SYS_close, directory); return 0; }
        if (!count) break;
        while (offset < (usize)count) {
            u32 reclen;
            usize length;
            int allowed;
            const u8 *entry = entries + offset;
            if ((usize)count - offset < 20) { sc1(SYS_close, directory); return 0; }
            reclen = (u32)entry[16] | ((u32)entry[17] << 8);
            if (reclen < 20 || reclen > (usize)count - offset) { sc1(SYS_close, directory); return 0; }
            for (length = 0; length < reclen - 19 && entry[19 + length]; length++) {}
            if (length == reclen - 19) { sc1(SYS_close, directory); return 0; }
            if (!((length == 1 && entry[19] == '.') ||
                  (length == 2 && entry[19] == '.' && entry[20] == '.'))) {
                allowed = allowed_dev_name(entry + 19, length);
                if (!allowed || (seen & (1U << (allowed - 1)))) { sc1(SYS_close, directory); return 0; }
                seen |= 1U << (allowed - 1);
            }
            offset += reclen;
        }
    }
    if (sc1(SYS_close, directory) != 0 || seen != 0x3f) return 0;
    {
        static const char *paths[] = {"/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty"};
        usize i;
        for (i = 0; i < sizeof(paths) / sizeof(paths[0]); i++) {
            struct stat_local st;
            if (sc4(SYS_newfstatat, AT_FDCWD, (i64)paths[i], (i64)&st, AT_SYMLINK_NOFOLLOW) != 0 ||
                (st.mode & S_IFMT) != S_IFCHR) return 0;
        }
    }
    {
        u8 byte = 0xff;
        i64 fd = sc3(SYS_open, (i64)"/dev/null", O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0);
        if (fd < 0 || sc3(SYS_write, fd, (i64)&byte, 1) != 1 || sc1(SYS_close, fd) != 0) return 0;
        fd = sc3(SYS_open, (i64)"/dev/zero", O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
        if (fd < 0 || sc3(SYS_read, fd, (i64)&byte, 1) != 1 || byte != 0 || sc1(SYS_close, fd) != 0) return 0;
        fd = sc3(SYS_open, (i64)"/dev/urandom", O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
        if (fd < 0 || sc3(SYS_read, fd, (i64)&byte, 1) != 1 || sc1(SYS_close, fd) != 0) return 0;
        fd = sc3(SYS_open, (i64)"/dev/full", O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0);
        if (fd < 0 || sc3(SYS_write, fd, (i64)&byte, 1) != -ENOSPC || sc1(SYS_close, fd) != 0) return 0;
    }
    return 1;
}

static int directory_is_empty(const char *path) {
    u8 entries[512];
    i64 directory = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (directory < 0) return 0;
    for (;;) {
        i64 count = sc3(SYS_getdents64, directory, (i64)entries, sizeof(entries));
        usize offset = 0;
        if (count < 0) { sc1(SYS_close, directory); return 0; }
        if (!count) break;
        while (offset < (usize)count) {
            const u8 *entry = entries + offset;
            u32 reclen;
            usize length;
            if ((usize)count - offset < 20) { sc1(SYS_close, directory); return 0; }
            reclen = (u32)entry[16] | ((u32)entry[17] << 8);
            if (reclen < 20 || reclen > (usize)count - offset) { sc1(SYS_close, directory); return 0; }
            for (length = 0; length < reclen - 19 && entry[19 + length]; length++) {}
            if (length == reclen - 19 ||
                !((length == 1 && entry[19] == '.') ||
                  (length == 2 && entry[19] == '.' && entry[20] == '.'))) {
                sc1(SYS_close, directory);
                return 0;
            }
            offset += reclen;
        }
    }
    return sc1(SYS_close, directory) == 0;
}

static int denied_open(const char *path, int flags) {
    i64 descriptor = sc3(SYS_open, (i64)path, flags | O_CLOEXEC | O_NOFOLLOW, 0);
    if (descriptor >= 0) { sc1(SYS_close, descriptor); return 0; }
    return descriptor == -EACCES || descriptor == -EPERM || descriptor == -EROFS;
}

static int authority_escape_failure(void) {
    i64 operation;
    if (!directory_is_empty("/proc/1/fd")) return 1;
    if (!directory_is_empty("/proc/1/fdinfo")) return 2;
    if (!denied_open("/proc/1/mem", O_RDONLY)) return 3;
    if (!denied_open("/proc/sys/kernel/randomize_va_space", O_WRONLY)) return 4;
    if (!directory_is_empty("/sys/class/virtio-ports")) return 5;
    if (!denied_open("/sys/kernel/uevent_seqnum", O_WRONLY)) return 6;
    operation = sc5(SYS_mount, 0, (i64)"/dev", 0, 0, 0);
    if (operation != -EPERM) return 7;
    operation = sc3(SYS_mknod, (i64)"/dev/escape", S_IFCHR | 0600, 0);
    if (operation != -EPERM) return 8;
    operation = sc1(SYS_unshare, CLONE_NEWNS);
    return operation == -EPERM ? 0 : 9;
}

static int verify_pid1_root_credentials(void) {
    static const char uid_line[] = "Uid:\t0\t0\t0\t0\n";
    static const char gid_line[] = "Gid:\t0\t0\t0\t0\n";
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

static int verify_cgroup_escape_denied(const char *path) {
    i64 descriptor = sc3(SYS_open, (i64)path, O_WRONLY | O_CLOEXEC, 0);
    if (descriptor >= 0) {
        sc1(SYS_close, descriptor);
        return 0;
    }
    return descriptor == -EACCES || descriptor == -EPERM || descriptor == -EROFS;
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

static int invocation_failure(u64 argc, char **argv, char **environment) {
    static const char sentinel[] = "palimpsest-oci-root-workload-proof-v1\n";
    char cwd[64];
    i64 cwd_size;
    int authority_error;
    int uid0_mode;
    if ((argc != 4 && argc != 5) || !same_text(argv[0], "/.__palimpsest_workload_proof_v1") ||
        !same_text(argv[1], "palimpsest-argv-one") || !same_text(argv[2], "") ||
        !same_text(argv[3], "line\nbreak") ||
        (argc == 5 && !same_text(argv[4], "palimpsest-uid0-isolation-v1"))) return 1;
    uid0_mode = argc == 5;
    if (!environment[0] || !environment[1] || environment[2] ||
        !same_text(environment[0], "PALIMPSEST_PROOF_ENV=value with spaces") ||
        !same_text(environment[1], "PALIMPSEST_PROOF_EMPTY=")) return 2;
    if (sc0(SYS_getpid) <= 1 || sc0(SYS_getppid) != 1 || sc0(SYS_getpgrp) != sc0(SYS_getpid)) return 3;
    if (sc0(SYS_getuid) != (uid0_mode ? 0 : 65534) ||
        sc0(SYS_getgid) != (uid0_mode ? 0 : 65534) || sc2(SYS_getgroups, 0, 0) != 0) return 4;
    cwd_size = sc2(SYS_getcwd, (i64)cwd, sizeof(cwd));
    if (cwd_size <= 0 || (usize)cwd_size > sizeof(cwd) || !same_text(cwd, "/proof/workdir")) return 5;
    if (!read_exact_file("/.__palimpsest_oci_root_workload_proof_v1", sentinel)) return 6;
    if (!read_exact_file("/proc/self/root/.__palimpsest_oci_root_workload_proof_v1", sentinel)) return 7;
    if (!read_exact_file("/proc/self/cgroup", "0::/palimpsest.workload\n")) return 8;
    if (!verify_cgroup_escape_denied("/sys/fs/cgroup/cgroup.procs")) return 9;
    if (!verify_cgroup_escape_denied("/sys/fs/cgroup/palimpsest.workload/cgroup.procs")) return 10;
    if (!verify_capabilityless_boundary()) return 11;
    if (!verify_private_devices()) return 12;
    authority_error = authority_escape_failure();
    if (authority_error) return 20 + authority_error;
    if (!verify_pid1_root_credentials()) return 14;
    return 0;
}

static __attribute__((noreturn)) void run_cooperative_descendant(int ready_writer,
                                                                 i64 expected_group) {
    int signal_fd;
    u8 ready = 1;
    if (sc0(SYS_getppid) <= 1 || sc0(SYS_getpgrp) != expected_group) exit_now(FAILURE_BASE + 5);
    signal_fd = new_sigterm_fd();
    if (signal_fd < 0 || sc3(SYS_write, ready_writer, (i64)&ready, 1) != 1 ||
        sc1(SYS_close, ready_writer) != 0) exit_now(FAILURE_BASE + 6);
    if (!wait_for_pid1_sigterm(signal_fd) || sc1(SYS_close, signal_fd) != 0)
        exit_now(FAILURE_BASE + 7);
    exit_now(DESCENDANT_SUCCESS);
}

static __attribute__((noreturn)) void run_stubborn_descendant(int ready_writer,
                                                              i64 expected_group) {
    u8 ready = 1;
    if (sc0(SYS_getppid) <= 1 || sc0(SYS_getpgrp) != expected_group ||
        sc3(SYS_write, ready_writer, (i64)&ready, 1) != 1 || sc1(SYS_close, ready_writer) != 0)
        exit_now(FAILURE_BASE + 11);
    for (;;) (void)sc0(SYS_pause);
}

static __attribute__((noreturn, used)) void start_c(u64 *stack) {
    u64 argc = stack[0];
    char **argv = (char **)(stack + 1);
    char **environment = argv + argc + 1;
    int ready_pipe[2];
    i64 main_pid;
    i64 cooperative, stubborn;
    int main_signal_fd;
    int invocation_error;
    u8 ready[2];
    usize ready_bytes = 0;
    invocation_error = invocation_failure(argc, argv, environment);
    if (invocation_error) exit_now(FAILURE_BASE + invocation_error);
    if (!block_sigterm()) exit_now(FAILURE_BASE + 2);
    if (sc2(SYS_pipe2, (i64)ready_pipe, O_CLOEXEC) != 0) exit_now(FAILURE_BASE + 3);
    main_pid = sc0(SYS_getpid);
    cooperative = sc0(SYS_fork);
    if (cooperative < 0) exit_now(FAILURE_BASE + 4);
    if (cooperative == 0) {
        sc1(SYS_close, ready_pipe[0]);
        run_cooperative_descendant(ready_pipe[1], main_pid);
    }
    stubborn = sc0(SYS_fork);
    if (stubborn < 0) exit_now(FAILURE_BASE + 4);
    if (stubborn == 0) {
        sc1(SYS_close, ready_pipe[0]);
        run_stubborn_descendant(ready_pipe[1], main_pid);
    }
    if (sc1(SYS_close, ready_pipe[1]) != 0) exit_now(FAILURE_BASE + 8);
    while (ready_bytes < sizeof(ready)) {
        i64 count = sc3(SYS_read, ready_pipe[0], (i64)(ready + ready_bytes), sizeof(ready) - ready_bytes);
        if (count <= 0 || (usize)count > sizeof(ready) - ready_bytes) exit_now(FAILURE_BASE + 8);
        ready_bytes += (usize)count;
    }
    if (ready[0] != 1 || ready[1] != 1 || sc1(SYS_close, ready_pipe[0]) != 0)
        exit_now(FAILURE_BASE + 8);
    main_signal_fd = new_sigterm_fd();
    if (main_signal_fd < 0 || !write_all(1, SIGNAL_ARMED_MARKER) ||
        !wait_for_pid1_sigterm(main_signal_fd) ||
        sc1(SYS_close, main_signal_fd) != 0)
        exit_now(FAILURE_BASE + 9);
    if (!write_all(1, STOP_OBSERVED_MARKER)) exit_now(FAILURE_BASE + 10);
    exit_now(MAIN_SUCCESS);
}

__attribute__((naked, noreturn, visibility("default"))) void _start(void) {
    __asm__ volatile("mov %rsp,%rdi\n"
                     "and $-16,%rsp\n"
                     "call start_c\n");
}
