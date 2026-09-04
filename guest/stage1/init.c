/* SPDX-License-Identifier: MIT
 * Palimpsest OCI guest stage-1 transport consumer.
 *
 * Freestanding Linux x86_64: no libc and no system headers.  This program
 * authenticates the stage-1 plan, mounts its filesystems, assembles an
 * OverlayFS root, moves that mount onto / with an initramfs-safe switch-root
 * choreography, then executes and supervises the admitted OCI workload.  It
 * deliberately performs no pivot_root syscall or production host lifecycle.
 */

typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long u64;
typedef signed long i64;
typedef unsigned long usize;

struct span { const char *p; usize n; };

#define SYS_read 0
#define SYS_write 1
#define SYS_open 2
#define SYS_close 3
#define SYS_fstat 5
#define SYS_lseek 8
#define SYS_pread64 17
#define SYS_ioctl 16
#define SYS_poll 7
#define SYS_rt_sigprocmask 14
#define SYS_pause 34
#define SYS_getpid 39
#define SYS_fork 57
#define SYS_execve 59
#define SYS_exit 60
#define SYS_wait4 61
#define SYS_kill 62
#define SYS_chdir 80
#define SYS_mkdir 83
#define SYS_readlink 89
#define SYS_chmod 90
#define SYS_setpgid 109
#define SYS_getgroups 115
#define SYS_setgroups 116
#define SYS_setresuid 117
#define SYS_getresuid 118
#define SYS_setresgid 119
#define SYS_getresgid 120
#define SYS_mount 165
#define SYS_chroot 161
#define SYS_signalfd4 289
#define SYS_pipe2 293
#define SYS_syncfs 306
#define SYS_statfs 137
#define SYS_fstatfs 138
#define SYS_getdents64 217
#define SYS_openat 257
#define SYS_mkdirat 258
#define SYS_unlinkat 263
#define SYS_clock_gettime 228
#define SYS_getrandom 318
#define SYS_mknod 133
#define SYS_capget 125
#define SYS_capset 126
#define SYS_prctl 157
#define SYS_unshare 272
#define SYS_seccomp 317
#define SYS_clone 56
#define SYS_clone3 435
#define SYS_umount2 166
#define SYS_ptrace 101
#define SYS_process_vm_readv 310
#define SYS_process_vm_writev 311
#define SYS_pidfd_getfd 438
#define SYS_open_by_handle_at 304
#define SYS_pivot_root 155
#define SYS_reboot 169
#define SYS_swapon 167
#define SYS_swapoff 168
#define SYS_init_module 175
#define SYS_delete_module 176
#define SYS_finit_module 313
#define SYS_kexec_load 246
#define SYS_bpf 321
#define SYS_perf_event_open 298
#define SYS_add_key 248
#define SYS_request_key 249
#define SYS_keyctl 250
#define SYS_userfaultfd 323
#define SYS_io_uring_setup 425
#define SYS_io_uring_enter 426
#define SYS_io_uring_register 427
#define SYS_open_tree 428
#define SYS_move_mount 429
#define SYS_fsopen 430
#define SYS_fsconfig 431
#define SYS_fsmount 432
#define SYS_fspick 433
#define SYS_mount_setattr 442
#define SYS_setns 308
#define SYS_mknodat 259
#define SYS_newfstatat 262

#define O_RDONLY 0
#define O_WRONLY 1
#define O_RDWR 2
#define O_NONBLOCK 04000
#define O_NOCTTY 0400
#define O_CLOEXEC 02000000
#define O_NOFOLLOW 0400000
#define O_DIRECTORY 0200000
#define POLLIN 1
#define POLLOUT 4
#define POLLERR 8
#define POLLHUP 16
#define SIG_BLOCK 0
#define SIG_SETMASK 2
#define SIGKILL 9
#define SIGCHLD 17
#define SIGSTOP 19
#define WNOHANG 1
#define SEEK_SET 0
#define AT_REMOVEDIR 0x200
#define AT_FDCWD -100
#define AT_SYMLINK_NOFOLLOW 0x100
#define S_IFMT 0170000
#define S_IFREG 0100000
#define S_IFBLK 0060000
#define S_IFCHR 0020000
#define S_IFDIR 0040000
#define MS_RDONLY 1
#define MS_NOSUID 2
#define MS_NODEV 4
#define MS_NOEXEC 8
#define MS_REMOUNT 32
#define MS_BIND 4096
#define MS_REC 16384
#define MS_PRIVATE 262144
#define MS_MOVE 8192
#define EEXIST 17
#define EBUSY 16
#define EIO 5
#define EAGAIN 11
#define EINTR 4
#define ESRCH 3
#define ECHILD 10
#define EINVAL 22
#define EPERM 1
#define ENOENT 2
#define EACCES 13
#define ENOTDIR 20
#define ENAMETOOLONG 36
#define CLOCK_MONOTONIC 1
#define GRND_NONBLOCK 1
#define BLKROGET 0x125e
#define BLKGETSIZE64 0x80081272

#define CLONE_NEWNS 0x00020000
#define CLONE_NEWCGROUP 0x02000000
#define CLONE_NEWUTS 0x04000000
#define CLONE_NEWIPC 0x08000000
#define CLONE_NEWUSER 0x10000000
#define CLONE_NEWPID 0x20000000
#define CLONE_NEWNET 0x40000000

#define PR_GET_DUMPABLE 3
#define PR_SET_DUMPABLE 4
#define PR_CAPBSET_READ 23
#define PR_CAPBSET_DROP 24
#define PR_GET_SECUREBITS 27
#define PR_SET_SECUREBITS 28
#define PR_SET_NO_NEW_PRIVS 38
#define PR_GET_NO_NEW_PRIVS 39
#define PR_GET_SECCOMP 21
#define PR_CAP_AMBIENT 47
#define PR_CAP_AMBIENT_IS_SET 1
#define PR_CAP_AMBIENT_CLEAR_ALL 4
#define WORKLOAD_SECUREBITS 239

#define LINUX_CAPABILITY_VERSION_3 0x20080522
#define SECCOMP_SET_MODE_FILTER 1
#define SECCOMP_FILTER_FLAG_TSYNC 1
#define SECCOMP_MODE_FILTER 2
#define BPF_LD 0x00
#define BPF_W 0x00
#define BPF_ABS 0x20
#define BPF_JMP 0x05
#define BPF_JEQ 0x10
#define BPF_JSET 0x40
#define BPF_ALU 0x04
#define BPF_AND 0x50
#define BPF_K 0x00
#define BPF_RET 0x06
#define SECCOMP_RET_KILL_PROCESS 0x80000000U
#define SECCOMP_RET_ERRNO 0x00050000U
#define SECCOMP_RET_ALLOW 0x7fff0000U
#define AUDIT_ARCH_X86_64 0xc000003eU
#define X32_SYSCALL_BIT 0x40000000U

#define CMDLINE_MAX 4096
#define PAYLOAD_MAX (2 * 1024 * 1024)
#define ARTIFACT_MAX (PAYLOAD_MAX + 4096)
#define PATH_MAX_LOCAL 4096
#define LOWER_MAX 24
#define PROBE_MAX 8
#define PROBE_BYTES_MAX (64 * 1024)
#define ARG_MAX_LOCAL 4096
#define ENV_MAX_LOCAL 4096
#define STRING_MAX_LOCAL (32 * 1024)
#define PROCESS_MAX_LOCAL (256 * 1024)
#define ACCOUNT_DATABASE_MAX_LOCAL (64 * 1024)
#define OCI_DEFAULT_PATH_LOCAL "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
#define GENERATION_DIGITS_MAX 4096
#define FILESYSTEM_VERIFY_BYTES_MAX 34359738368ul
#define FILESYSTEM_IO_BYTES (1024 * 1024)

#define EXIT_USAGE 64
#define EXIT_CMDLINE 65
#define EXIT_DISCOVERY 66
#define EXIT_TRANSPORT 67
#define EXIT_PLAN 68
#define EXIT_FILESYSTEM 69
#define EXIT_ASSEMBLY 70
#define EXIT_ROOT_TRANSITION 71
#define EXIT_WORKLOAD 72

#define WORKLOAD_STARTED_MARKER "palimpsest guest stage1: workload started; root is slash; supervisor active\n"
#define WORKLOAD_ISOLATION_MARKER "palimpsest guest stage1: workload isolation committed; lifecycle authority retained by pid1\n"
#define ROOT_TRANSITION_MARKER "palimpsest guest stage1: root transition complete; root is slash; workload pending\n"
#define WORKLOAD_TERMINAL_PREFIX "palimpsest guest stage1: workload terminal; main_status="
#define WORKLOAD_REJECTED_PREFIX "palimpsest guest stage1: workload launch rejected; stage="
#define WORKLOAD_CLEANUP_REJECTED_PREFIX "palimpsest guest stage1: workload cleanup rejected; stage="
#define LIFECYCLE_REJECTED_PREFIX "palimpsest guest stage1: lifecycle rejected; stage="
#define LIFECYCLE_CHANNEL_NAME "org.palimpsest.oci.lifecycle.0"
#define LIFECYCLE_PROTOCOL "palimpsest.oci-lifecycle-control.v2"
#define LIFECYCLE_PUBLIC_STATE "palimpsest.oci-lifecycle-public-state.v1"
#define LIFECYCLE_CHANNEL_CARRIER "channel-frame"
#define LIFECYCLE_CONSOLE_CARRIER "console-line"
#define LIFECYCLE_BOUNDARY_PREFIX "palimpsest guest stage1: lifecycle boundary ack "
#define CONTROL_PAYLOAD_MAX 65532
#define CONTROL_GUEST_BODY_MAX 2048
#define CONTROL_GUEST_OUTPUT_MAX 4096
#define LIFECYCLE_CONNECTION_MAX 16
#define LIFECYCLE_REQUEST_LEDGER_MAX 17
#define LIFECYCLE_DISCONNECTED 0
#define LIFECYCLE_CONNECTED 1
#define LIFECYCLE_NEW 0
#define LIFECYCLE_READY 1
#define LIFECYCLE_STOPPING 2
#define LIFECYCLE_TERMINAL 3
#define LIFECYCLE_READY_COMMITTED_MARKER "palimpsest guest stage1: lifecycle ready committed\n"
#define LIFECYCLE_PARTIAL_BUFFERED_MARKER "palimpsest guest stage1: lifecycle partial frame buffered\n"
#define LIFECYCLE_STOP_DISPATCHED_MARKER "palimpsest guest stage1: lifecycle stop dispatched\n"
#define LIFECYCLE_STOP_DUPLICATE_MARKER "palimpsest guest stage1: lifecycle stop duplicate accepted\n"
#define WORKLOAD_STATUS_NONE 4294967295U

struct timespec_local {
    i64 sec;
    i64 nsec;
};

struct stat_local {
    u64 dev;
    u64 ino;
    u64 nlink;
    u32 mode;
    u32 uid;
    u32 gid;
    u32 pad0;
    u64 rdev;
    i64 size;
    i64 blksize;
    i64 blocks;
    struct timespec_local atime;
    struct timespec_local mtime;
    struct timespec_local ctime;
    i64 reserved[3];
};

struct statfs_local {
    i64 type;
    i64 block_size;
    u64 blocks;
    u64 blocks_free;
    u64 blocks_available;
    u64 files;
    u64 files_free;
    struct { int value[2]; } fsid;
    i64 name_length;
    i64 fragment_size;
    i64 flags;
    i64 spare[4];
};

struct pollfd_local {
    int fd;
    short events;
    short revents;
};

struct signalfd_siginfo_local {
    u32 signo;
    u8 rest[124];
};

struct child_error_local {
    u32 stage;
    u32 error;
};

struct capability_header_local {
    u32 version;
    int pid;
};

struct capability_data_local {
    u32 effective;
    u32 permitted;
    u32 inheritable;
};

struct sock_filter_local {
    unsigned short code;
    u8 jt;
    u8 jf;
    u32 k;
};

struct sock_fprog_local {
    unsigned short len;
    struct sock_filter_local *filter;
};

#define BPF_STMT_LOCAL(code_value, k_value) \
    { (unsigned short)(code_value), 0, 0, (u32)(k_value) }
#define BPF_JUMP_LOCAL(code_value, k_value, jt_value, jf_value) \
    { (unsigned short)(code_value), (u8)(jt_value), (u8)(jf_value), (u32)(k_value) }

struct supervisor_result {
    u32 main_status;
    u32 cooperative_status;
    u32 forced_status;
    u32 reaped;
    u32 forwarded;
    u32 pid1_uid;
    u32 pid1_gid;
    u32 pid1_groups;
    u32 main_exit_code;
    u32 main_signal;
};

struct lifecycle_binding {
    char run_id[37];
    char core[72];
    char stage1[72];
};

struct lifecycle_session {
    int fd;
    int poisoned;
    int connection;
    int connection_has_hello;
    int state;
    int natural_late_stop_allowed;
    int partial_frame_marker_emitted;
    u32 nonce_count;
    u32 request_count;
    u32 header_used;
    u32 payload_expected;
    u32 payload_used;
    u32 reconnect_backoff_ms;
    int outbound_failed;
    u32 stop_delivered_count;
    u32 terminal_exit_code;
    u32 terminal_signal;
    u8 header[4];
    u64 next_sequence;
    u64 frame_deadline;
    u64 outbound_deadline;
    u64 last_hello_request_id;
    u64 hello_request_id;
    u64 stop_request_id;
    u64 epoch;
    u64 last_accepted_host_wire;
    u64 connection_opener_request_id;
    u64 bootstrap_wire_sequence;
    u64 key_ack_wire_sequence;
    char host_nonce[65];
    char boot_attempt_id[37];
    char boot_generation[37];
    char boundary_id[37];
    char boundary_digest[72];
    u8 boot_key[32];
    char key_id[72];
    char used_nonces[LIFECYCLE_CONNECTION_MAX][65];
    char used_boundary_ids[LIFECYCLE_CONNECTION_MAX][37];
    u32 boundary_count;
    u64 seen_request_ids[LIFECYCLE_REQUEST_LEDGER_MAX];
};

static struct lifecycle_binding lifecycle_binding;

struct workload_cgroup {
    int root_fd;
    int dir_fd;
    int procs_fd;
    int kill_fd;
    int events_fd;
};

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
    __asm__ volatile("syscall" : "=a"(r) : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10) : "rcx", "r11", "memory");
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

static inline i64 prctl_local(i64 option, i64 argument) {
    return sc5(SYS_prctl, option, argument, 0, 0, 0);
}

void *memcpy(void *dst, const void *src, usize n) {
    u8 *d = (u8 *)dst;
    const u8 *s = (const u8 *)src;
    usize i;
    for (i = 0; i < n; i++) d[i] = s[i];
    return dst;
}

void *memset(void *dst, int value, usize n) {
    u8 *d = (u8 *)dst;
    usize i;
    for (i = 0; i < n; i++) d[i] = (u8)value;
    return dst;
}

static usize slen(const char *s) {
    usize n = 0;
    while (s[n]) n++;
    return n;
}

static int bytes_equal(const void *a0, const void *b0, usize n) {
    const u8 *a = (const u8 *)a0;
    const u8 *b = (const u8 *)b0;
    usize i;
    u8 diff = 0;
    for (i = 0; i < n; i++) diff |= a[i] ^ b[i];
    return diff == 0;
}

static int bytes_all_zero(const void *p0, usize n) {
    const u8 *p = (const u8 *)p0; usize i; u8 any = 0;
    for (i = 0; i < n; i++) any |= p[i];
    return any == 0;
}

static int text_equal(const char *a, const char *b) {
    usize an = slen(a), bn = slen(b);
    return an == bn && bytes_equal(a, b, an);
}

static void write_all(int fd, const char *s) {
    usize left = slen(s);
    while (left) {
        i64 n = sc3(SYS_write, fd, (i64)s, (i64)left);
        if (n <= 0) return;
        s += n;
        left -= (usize)n;
    }
}

static void write_u32(int fd, u32 value) {
    char digits[10];
    usize used = 0;
    do {
        digits[used++] = (char)('0' + value % 10);
        value /= 10;
    } while (value && used < sizeof(digits));
    while (used) {
        char byte[2] = {digits[--used], 0};
        write_all(fd, byte);
    }
}

static void workload_rejected(u32 stage, u32 error) {
    write_all(2, WORKLOAD_REJECTED_PREFIX);
    write_u32(2, stage);
    write_all(2, "; errno=");
    write_u32(2, error);
    write_all(2, "; started and terminal disabled; waiting fail-closed\n");
}

static void workload_terminal(const struct supervisor_result *result) {
    write_all(1, WORKLOAD_TERMINAL_PREFIX);
    write_u32(1, result->main_status);
    write_all(1, "; cooperative_status=");
    write_u32(1, result->cooperative_status);
    write_all(1, "; forced_status=");
    write_u32(1, result->forced_status);
    write_all(1, "; reaped=");
    write_u32(1, result->reaped);
    write_all(1, "; forwarded=");
    write_u32(1, result->forwarded);
    write_all(1, "; pid1_uid=");
    write_u32(1, result->pid1_uid);
    write_all(1, "; pid1_gid=");
    write_u32(1, result->pid1_gid);
    write_all(1, "; pid1_groups=");
    write_u32(1, result->pid1_groups);
    write_all(1, "; cleanup=cgroup.kill; cgroup_populated=0");
    write_all(1, "; waiting fail-closed\n");
}

static void workload_cleanup_rejected(u32 stage, u32 error) {
    write_all(2, WORKLOAD_CLEANUP_REJECTED_PREFIX);
    write_u32(2, stage);
    write_all(2, "; errno=");
    write_u32(2, error);
    write_all(2, "; terminal disabled; waiting fail-closed\n");
}

static void lifecycle_rejected(u32 stage, u32 error) {
    write_all(2, LIFECYCLE_REJECTED_PREFIX);
    write_u32(2, stage);
    write_all(2, "; errno=");
    write_u32(2, error);
    write_all(2, "; terminal disabled; waiting fail-closed\n");
}

static __attribute__((noreturn)) void exit_now(int code) {
    sc1(SYS_exit, code);
    for (;;) {}
}

static __attribute__((noreturn)) void wait_closed(const char *message) {
    write_all(2, message);
    for (;;) sc0(SYS_pause);
}

static u32 rotr32(u32 x, u32 n) { return (x >> n) | (x << (32 - n)); }

struct sha256_ctx {
    u32 h[8];
    u64 total;
    u8 block[64];
    u32 used;
};

static char hex_digit(u8 value);

static const u32 sha_k[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static void sha_transform(struct sha256_ctx *c, const u8 *p) {
    u32 w[64], a, b, d, e, f, g, h, i, cc;
    for (i = 0; i < 16; i++)
        w[i] = ((u32)p[i * 4] << 24) | ((u32)p[i * 4 + 1] << 16) | ((u32)p[i * 4 + 2] << 8) | p[i * 4 + 3];
    for (i = 16; i < 64; i++) {
        u32 s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        u32 s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = c->h[0]; b = c->h[1]; cc = c->h[2]; d = c->h[3];
    e = c->h[4]; f = c->h[5]; g = c->h[6]; h = c->h[7];
    for (i = 0; i < 64; i++) {
        u32 s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        u32 ch = (e & f) ^ ((~e) & g);
        u32 t1 = h + s1 + ch + sha_k[i] + w[i];
        u32 s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        u32 maj = (a & b) ^ (a & cc) ^ (b & cc);
        u32 t2 = s0 + maj;
        h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha_init(struct sha256_ctx *c) {
    static const u32 initial[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(c->h, initial, sizeof(initial));
    c->total = 0;
    c->used = 0;
}

static void sha_update(struct sha256_ctx *c, const void *data0, usize n) {
    const u8 *data = (const u8 *)data0;
    c->total += n;
    while (n) {
        usize take = 64 - c->used;
        if (take > n) take = n;
        memcpy(c->block + c->used, data, take);
        c->used += (u32)take;
        data += take;
        n -= take;
        if (c->used == 64) {
            sha_transform(c, c->block);
            c->used = 0;
        }
    }
}

static void sha_final(struct sha256_ctx *c, u8 out[32]) {
    u64 bits = c->total * 8;
    u32 i;
    c->block[c->used++] = 0x80;
    if (c->used > 56) {
        while (c->used < 64) c->block[c->used++] = 0;
        sha_transform(c, c->block);
        c->used = 0;
    }
    while (c->used < 56) c->block[c->used++] = 0;
    for (i = 0; i < 8; i++) c->block[56 + i] = (u8)(bits >> (56 - i * 8));
    sha_transform(c, c->block);
    for (i = 0; i < 8; i++) {
        out[i * 4] = (u8)(c->h[i] >> 24);
        out[i * 4 + 1] = (u8)(c->h[i] >> 16);
        out[i * 4 + 2] = (u8)(c->h[i] >> 8);
        out[i * 4 + 3] = (u8)c->h[i];
    }
}

static void sha_bytes(const void *p, usize n, u8 out[32]) {
    struct sha256_ctx c;
    sha_init(&c);
    sha_update(&c, p, n);
    sha_final(&c, out);
}

static void secure_zero(void *p0, usize n) {
    volatile u8 *p = (volatile u8 *)p0;
    while (n--) *p++ = 0;
}

static void hmac_sha256_parts(const u8 key[32], const void *a, usize an,
                              const void *b, usize bn, const void *d, usize dn,
                              u8 out[32]) {
    u8 inner_key[64], outer_key[64], inner[32];
    struct sha256_ctx c;
    usize i;
    for (i = 0; i < 64; i++) {
        u8 value = i < 32 ? key[i] : 0;
        inner_key[i] = value ^ 0x36;
        outer_key[i] = value ^ 0x5c;
    }
    sha_init(&c); sha_update(&c, inner_key, 64);
    if (an) sha_update(&c, a, an);
    if (bn) sha_update(&c, b, bn);
    if (dn) sha_update(&c, d, dn);
    sha_final(&c, inner);
    sha_init(&c); sha_update(&c, outer_key, 64); sha_update(&c, inner, 32); sha_final(&c, out);
    secure_zero(inner_key, sizeof(inner_key)); secure_zero(outer_key, sizeof(outer_key));
    secure_zero(inner, sizeof(inner)); secure_zero(&c, sizeof(c));
}

static void hmac_sha256(const u8 key[32], const void *p, usize n, u8 out[32]) {
    hmac_sha256_parts(key, p, n, 0, 0, 0, 0, out);
}

static void hex32(const u8 value[32], char out[65]) {
    usize i;
    for (i = 0; i < 32; i++) {
        out[i * 2] = hex_digit(value[i] >> 4);
        out[i * 2 + 1] = hex_digit(value[i] & 15);
    }
    out[64] = 0;
}

static char hex_digit(u8 value) { return (char)(value < 10 ? '0' + value : 'a' + value - 10); }

static void digest_text(const void *p, usize n, char out[72]) {
    u8 digest[32];
    usize i;
    sha_bytes(p, n, digest);
    memcpy(out, "sha256:", 7);
    for (i = 0; i < 32; i++) {
        out[7 + i * 2] = hex_digit(digest[i] >> 4);
        out[8 + i * 2] = hex_digit(digest[i] & 15);
    }
    out[71] = 0;
}

static int is_hex(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }

static int valid_digest(const char *s, usize n) {
    usize i;
    if (n != 71 || !bytes_equal(s, "sha256:", 7)) return 0;
    for (i = 7; i < 71; i++) if (!is_hex(s[i])) return 0;
    return 1;
}

static u8 hex_value(char c) { return (u8)(c <= '9' ? c - '0' : c - 'a' + 10); }

static int valid_uuid_span(struct span s);

static int digest_bytes(struct span s, u8 out[32]) {
    usize i;
    if (!valid_digest(s.p, s.n)) return 0;
    for (i = 0; i < 32; i++) out[i] = (u8)((hex_value(s.p[7 + i * 2]) << 4) | hex_value(s.p[8 + i * 2]));
    return 1;
}

static int uuid_bytes(struct span s, u8 out[16]) {
    usize i, used = 0;
    if (!valid_uuid_span(s)) return 0;
    for (i = 0; i < s.n;) {
        if (s.p[i] == '-') { i++; continue; }
        out[used++] = (u8)((hex_value(s.p[i]) << 4) | hex_value(s.p[i + 1]));
        i += 2;
    }
    return used == 16;
}

static void root_label(struct span volume_id, char out[17]) {
    struct sha256_ctx c;
    u8 digest[32];
    usize i;
    const char prefix[] = "palimpsest-oci-root-volume-v1\0";
    memcpy(out, "pali-root-", 10);
    sha_init(&c); sha_update(&c, prefix, sizeof(prefix) - 1); sha_update(&c, volume_id.p, volume_id.n); sha_final(&c, digest);
    for (i = 0; i < 3; i++) { out[10 + i * 2] = hex_digit(digest[i] >> 4); out[11 + i * 2] = hex_digit(digest[i] & 15); }
    out[16] = 0;
}

static int valid_serial(const char *s, usize n) {
    usize i;
    if (n != 20) return 0;
    for (i = 0; i < 20; i++) if (!is_hex(s[i])) return 0;
    return 1;
}

static int append_text(char *dst, usize *used, const char *s) {
    usize n = slen(s);
    if (*used + n + 1 > PATH_MAX_LOCAL) return 0;
    memcpy(dst + *used, s, n);
    *used += n;
    dst[*used] = 0;
    return 1;
}

static int fixture_path(char out[PATH_MAX_LOCAL], const char *root, const char *suffix) {
    usize used = 0;
    out[0] = 0;
    if (!append_text(out, &used, root)) return 0;
    if (used && out[used - 1] == '/') used--;
    out[used] = 0;
    return append_text(out, &used, suffix);
}

static i64 open_read(const char *path, int nofollow) {
    int flags = O_RDONLY | O_CLOEXEC | O_NONBLOCK;
    if (nofollow) flags |= O_NOFOLLOW;
    return sc3(SYS_open, (i64)path, flags, 0);
}

static i64 read_bounded_file(const char *path, u8 *out, usize cap, int nofollow, struct stat_local *st_out) {
    i64 fd = open_read(path, nofollow), total = 0;
    struct stat_local st;
    if (fd < 0) return -1;
    if (sc2(SYS_fstat, fd, (i64)&st) < 0) { sc1(SYS_close, fd); return -1; }
    while ((usize)total < cap) {
        i64 n = sc3(SYS_read, fd, (i64)(out + total), cap - (usize)total);
        if (n < 0) { sc1(SYS_close, fd); return -1; }
        if (n == 0) break;
        total += n;
    }
    if ((usize)total == cap) {
        u8 extra;
        if (sc3(SYS_read, fd, (i64)&extra, 1) != 0) { sc1(SYS_close, fd); return -1; }
    }
    if (st_out) *st_out = st;
    sc1(SYS_close, fd);
    return total;
}

struct bindings {
    char resource[72];
    char core[72];
    char transport[72];
    char transport_serial[21];
    char root_serial[21];
    char lowers[LOWER_MAX][21];
    u32 lower_count;
};

static int starts(const char *p, usize n, const char *prefix) {
    usize pn = slen(prefix);
    return n >= pn && bytes_equal(p, prefix, pn);
}

static int copy_span(char *dst, usize cap, struct span s) {
    if (s.n + 1 > cap) return 0;
    memcpy(dst, s.p, s.n);
    dst[s.n] = 0;
    return 1;
}

static int parse_cmdline(char *buf, usize n, struct bindings *b) {
    struct span fields[6];
    const char *keys[6] = {"palimpsest.resource=", "palimpsest.core=", "palimpsest.stage1=",
                           "palimpsest.stage1dev=", "palimpsest.root=", "palimpsest.lowers="};
    u32 seen = 0, i;
    usize pos = 0;
    if (!n || n > CMDLINE_MAX) return 0;
    memset(fields, 0, sizeof(fields));
    while (pos < n) {
        usize start, end;
        while (pos < n && (buf[pos] == ' ' || buf[pos] == '\n' || buf[pos] == '\t')) pos++;
        if (pos == n) break;
        start = pos;
        while (pos < n && buf[pos] != ' ' && buf[pos] != '\n' && buf[pos] != '\t') {
            u8 c = (u8)buf[pos];
            if (!c || c >= 0x80) return 0;
            pos++;
        }
        end = pos;
        if (!starts(buf + start, end - start, "palimpsest.")) continue;
        for (i = 0; i < 6; i++) {
            usize kn = slen(keys[i]);
            if (end - start > kn && bytes_equal(buf + start, keys[i], kn)) break;
        }
        if (i == 6 || (seen & (1u << i))) return 0;
        seen |= 1u << i;
        fields[i].p = buf + start + slen(keys[i]);
        fields[i].n = end - start - slen(keys[i]);
    }
    if (seen != 0x3f || !valid_digest(fields[0].p, fields[0].n) ||
        !valid_digest(fields[1].p, fields[1].n) || !valid_digest(fields[2].p, fields[2].n)) return 0;
    if (!copy_span(b->resource, sizeof(b->resource), fields[0]) ||
        !copy_span(b->core, sizeof(b->core), fields[1]) ||
        !copy_span(b->transport, sizeof(b->transport), fields[2])) return 0;
    if (!starts(fields[3].p, fields[3].n, "virtio-") || fields[3].n != 27 ||
        !valid_serial(fields[3].p + 7, 20)) return 0;
    if (!starts(fields[4].p, fields[4].n, "virtio-") || fields[4].n != 27 ||
        !valid_serial(fields[4].p + 7, 20)) return 0;
    memcpy(b->transport_serial, fields[3].p + 7, 20); b->transport_serial[20] = 0;
    memcpy(b->root_serial, fields[4].p + 7, 20); b->root_serial[20] = 0;
    b->lower_count = 0;
    pos = 0;
    while (pos < fields[5].n) {
        usize end = pos;
        while (end < fields[5].n && fields[5].p[end] != ',') end++;
        if (b->lower_count >= LOWER_MAX || end - pos != 27 ||
            !bytes_equal(fields[5].p + pos, "virtio-", 7) || !valid_serial(fields[5].p + pos + 7, 20)) return 0;
        memcpy(b->lowers[b->lower_count], fields[5].p + pos + 7, 20);
        b->lowers[b->lower_count][20] = 0;
        b->lower_count++;
        if (end == fields[5].n) { pos = end; break; }
        if (end + 1 == fields[5].n) return 0;
        pos = end + 1;
    }
    if (!b->lower_count) return 0;
    for (i = 0; i < b->lower_count; i++) {
        u32 j;
        if (text_equal(b->lowers[i], b->root_serial) || text_equal(b->lowers[i], b->transport_serial)) return 0;
        for (j = 0; j < i; j++) if (text_equal(b->lowers[i], b->lowers[j])) return 0;
    }
    if (text_equal(b->root_serial, b->transport_serial)) return 0;
    {
        struct sha256_ctx c;
        u8 digest[32];
        char expected[21];
        const char prefix[] = "palimpsest-oci-root-stage1-transport-v1\0";
        sha_init(&c);
        sha_update(&c, prefix, sizeof(prefix) - 1);
        sha_update(&c, b->transport, 71);
        sha_final(&c, digest);
        for (i = 0; i < 10; i++) { expected[i * 2] = hex_digit(digest[i] >> 4); expected[i * 2 + 1] = hex_digit(digest[i] & 15); }
        expected[20] = 0;
        if (!text_equal(expected, b->transport_serial)) return 0;
    }
    return 1;
}

struct parser {
    const u8 *p;
    const u8 *end;
    usize process_bytes;
    struct span env_names[ENV_MAX_LOCAL];
    u32 env_count;
};

struct guest_process {
    char *argv[ARG_MAX_LOCAL + 1];
    char *envp[ENV_MAX_LOCAL + 1];
    char *cwd;
    char *user_name;
    char *group_name;
    u32 argc;
    u32 envc;
    u32 uid;
    u32 gid;
    int user_numeric;
    int group_numeric;
    int group_present;
    u32 stop_signal;
    usize used;
    char arena[PROCESS_MAX_LOCAL + 1];
};

static u8 passwd_database[ACCOUNT_DATABASE_MAX_LOCAL];
static u8 group_database[ACCOUNT_DATABASE_MAX_LOCAL];

struct expected_device {
    char serial[21];
    u64 size;
    int read_only;
    u8 digest[32];
    int has_digest;
    u8 filesystem_uuid[16];
    char label[17];
};

struct expected_device_set {
    struct expected_device root;
    struct expected_device lowers[LOWER_MAX];
    u32 lower_count;
    struct {
        char path[256];
        u8 digest[32];
        u64 size;
        u32 top_ordinal;
    } probes[PROBE_MAX];
    u32 probe_count;
};

static int take_char(struct parser *j, u8 c) {
    if (j->p >= j->end || *j->p != c) return 0;
    j->p++;
    return 1;
}

static int utf8_advance(const u8 *p, const u8 *end, usize *count) {
    u32 cp;
    if (*p < 0x80) { *count = 1; return *p >= 0x20; }
    if (*p >= 0xc2 && *p <= 0xdf && p + 1 < end && (p[1] & 0xc0) == 0x80) { *count = 2; return 1; }
    if (*p >= 0xe0 && *p <= 0xef && p + 2 < end && (p[1] & 0xc0) == 0x80 && (p[2] & 0xc0) == 0x80) {
        cp = ((u32)(p[0] & 15) << 12) | ((u32)(p[1] & 63) << 6) | (p[2] & 63);
        if (cp < 0x800 || (cp >= 0xd800 && cp <= 0xdfff)) return 0;
        *count = 3; return 1;
    }
    if (*p >= 0xf0 && *p <= 0xf4 && p + 3 < end && (p[1] & 0xc0) == 0x80 &&
        (p[2] & 0xc0) == 0x80 && (p[3] & 0xc0) == 0x80) {
        cp = ((u32)(p[0] & 7) << 18) | ((u32)(p[1] & 63) << 12) | ((u32)(p[2] & 63) << 6) | (p[3] & 63);
        if (cp < 0x10000 || cp > 0x10ffff) return 0;
        *count = 4; return 1;
    }
    return 0;
}

static int json_string(struct parser *j, struct span *raw, usize *decoded, int allow_nul) {
    const u8 *start;
    usize out = 0;
    if (!take_char(j, '"')) return 0;
    start = j->p;
    while (j->p < j->end && *j->p != '"') {
        if (*j->p == '\\') {
            u8 e;
            if (++j->p >= j->end) return 0;
            e = *j->p++;
            if (e == '"' || e == '\\' || e == 'b' || e == 'f' || e == 'n' || e == 'r' || e == 't') out++;
            else if (e == 'u') {
                u8 value;
                if (j->end - j->p < 4 || j->p[0] != '0' || j->p[1] != '0' ||
                    !is_hex((char)j->p[2]) || !is_hex((char)j->p[3])) return 0;
                value = (u8)((j->p[2] <= '9' ? j->p[2] - '0' : j->p[2] - 'a' + 10) * 16 +
                             (j->p[3] <= '9' ? j->p[3] - '0' : j->p[3] - 'a' + 10));
                if (value >= 0x20 || value == 8 || value == 9 || value == 10 || value == 12 || value == 13 ||
                    (!allow_nul && value == 0)) return 0;
                j->p += 4; out++;
            } else return 0;
        } else {
            usize width;
            if (!utf8_advance(j->p, j->end, &width) || (!allow_nul && *j->p == 0)) return 0;
            j->p += width; out += width;
        }
    }
    if (j->p >= j->end) return 0;
    if (raw) { raw->p = (const char *)start; raw->n = (usize)(j->p - start); }
    if (decoded) *decoded = out;
    j->p++;
    return 1;
}

static int key(struct parser *j, const char *expected) {
    struct span s;
    usize n = slen(expected);
    if (!json_string(j, &s, 0, 0) || s.n != n || !bytes_equal(s.p, expected, n) || !take_char(j, ':')) return 0;
    return 1;
}

static int exact_string(struct parser *j, const char *expected) {
    struct span s;
    usize n = slen(expected);
    return json_string(j, &s, 0, 0) && s.n == n && bytes_equal(s.p, expected, n);
}

static int exact_literal(struct parser *j, const char *expected) {
    usize n = slen(expected);
    if ((usize)(j->end - j->p) < n || !bytes_equal(j->p, expected, n)) return 0;
    j->p += n;
    return 1;
}

static int plain_string(struct parser *j, struct span *s, usize *decoded) {
    const char *p;
    usize i;
    if (!json_string(j, s, decoded, 0)) return 0;
    p = s->p;
    for (i = 0; i < s->n; i++) if (p[i] == '\\' || (u8)p[i] >= 0x80) return 0;
    return 1;
}

static int uint_value(struct parser *j, u64 *value) {
    u64 v = 0;
    const u8 *start = j->p;
    if (start >= j->end || *start < '0' || *start > '9') return 0;
    if (*start == '0' && start + 1 < j->end && start[1] >= '0' && start[1] <= '9') return 0;
    while (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
        u32 d = *j->p++ - '0';
        if (v > (~(u64)0 - d) / 10) return 0;
        v = v * 10 + d;
    }
    *value = v;
    return j->p > start;
}

static int positive_decimal(struct parser *j) {
    const u8 *start = j->p;
    if (start >= j->end || *start < '1' || *start > '9') return 0;
    while (j->p < j->end && *j->p >= '0' && *j->p <= '9') j->p++;
    return j->p > start && (usize)(j->p - start) <= GENERATION_DIGITS_MAX;
}

static int derived_serial_matches(const char *name_space, struct span identity, const char *serial) {
    struct sha256_ctx c;
    u8 digest[32];
    char expected[21];
    u32 i;
    const char prefix[] = "palimpsest-oci-root-";
    const char suffix[] = "-v1\0";
    sha_init(&c);
    sha_update(&c, prefix, sizeof(prefix) - 1);
    sha_update(&c, name_space, slen(name_space));
    sha_update(&c, suffix, sizeof(suffix) - 1);
    sha_update(&c, identity.p, identity.n);
    sha_final(&c, digest);
    for (i = 0; i < 10; i++) { expected[i * 2] = hex_digit(digest[i] >> 4); expected[i * 2 + 1] = hex_digit(digest[i] & 15); }
    expected[20] = 0;
    return text_equal(expected, serial);
}

static int exact_string_array3(struct parser *j, const char *a, const char *b, const char *c) {
    return take_char(j, '[') && exact_string(j, a) && take_char(j, ',') && exact_string(j, b) &&
           take_char(j, ',') && exact_string(j, c) && take_char(j, ']');
}

static int valid_probe_path(struct span s) {
    usize i;
    if (s.n < 2 || s.n > 255 || s.p[0] != '/' || s.p[s.n - 1] == '/') return 0;
    for (i = 1; i < s.n; i++) {
        u8 c = (u8)s.p[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-')) return 0;
    }
    return !((s.n == 2 && s.p[1] == '.') ||
             (s.n == 3 && s.p[1] == '.' && s.p[2] == '.'));
}

static int valid_uuid_span(struct span s) {
    usize i;
    if (s.n != 36) return 0;
    for (i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (s.p[i] != '-') return 0; }
        else if (!is_hex(s.p[i])) return 0;
    }
    return 1;
}

static int valid_run_name(struct span s) {
    usize i;
    if (!s.n || s.n > 63 || !((s.p[0] >= 'a' && s.p[0] <= 'z') ||
                              (s.p[0] >= '0' && s.p[0] <= '9'))) return 0;
    for (i = 0; i < s.n; i++) if (!((s.p[i] >= 'a' && s.p[i] <= 'z') ||
                                      (s.p[i] >= '0' && s.p[i] <= '9') || s.p[i] == '-')) return 0;
    return 1;
}

static int valid_env_name(struct span s) {
    usize i;
    if (!s.n || !((s.p[0] >= 'A' && s.p[0] <= 'Z') || (s.p[0] >= 'a' && s.p[0] <= 'z') || s.p[0] == '_')) return 0;
    for (i = 1; i < s.n; i++) if (!((s.p[i] >= 'A' && s.p[i] <= 'Z') ||
        (s.p[i] >= 'a' && s.p[i] <= 'z') || (s.p[i] >= '0' && s.p[i] <= '9') || s.p[i] == '_')) return 0;
    return 1;
}

static int valid_cwd(struct span s) {
    usize i;
    if (!s.n || s.p[0] != '/') return 0;
    if (s.n > 1 && s.p[s.n - 1] == '/') return 0;
    for (i = 0; i < s.n; i++) {
        if (i && s.p[i] == '/' && s.p[i - 1] == '/') return 0;
        if ((i == 0 || s.p[i - 1] == '/') && s.p[i] == '.' &&
            (i + 1 == s.n || s.p[i + 1] == '/' || (s.p[i + 1] == '.' && (i + 2 == s.n || s.p[i + 2] == '/')))) return 0;
    }
    return 1;
}

static int decode_process_string(struct guest_process *process, struct span raw, char **result) {
    usize i = 0, start = process->used;
    if (start > PROCESS_MAX_LOCAL) return 0;
    while (i < raw.n) {
        u8 value = (u8)raw.p[i++];
        if (value == '\\') {
            u8 escaped;
            if (i >= raw.n) return 0;
            escaped = (u8)raw.p[i++];
            if (escaped == '"' || escaped == '\\') value = escaped;
            else if (escaped == 'b') value = 8;
            else if (escaped == 'f') value = 12;
            else if (escaped == 'n') value = 10;
            else if (escaped == 'r') value = 13;
            else if (escaped == 't') value = 9;
            else if (escaped == 'u') {
                if (raw.n - i < 4 || raw.p[i] != '0' || raw.p[i + 1] != '0' ||
                    !is_hex(raw.p[i + 2]) || !is_hex(raw.p[i + 3])) return 0;
                value = (u8)((raw.p[i + 2] <= '9' ? raw.p[i + 2] - '0' : raw.p[i + 2] - 'a' + 10) * 16 +
                             (raw.p[i + 3] <= '9' ? raw.p[i + 3] - '0' : raw.p[i + 3] - 'a' + 10));
                i += 4;
            } else return 0;
        }
        if (!value || process->used >= PROCESS_MAX_LOCAL) return 0;
        process->arena[process->used++] = (char)value;
    }
    if (process->used >= sizeof(process->arena)) return 0;
    process->arena[process->used++] = 0;
    *result = process->arena + start;
    return 1;
}

static int numeric_account(struct span s, u32 *result) {
    u64 value = 0;
    usize i;
    if (!s.n || s.n > 10 || (s.n > 1 && s.p[0] == '0')) return 0;
    for (i = 0; i < s.n; i++) {
        if (s.p[i] < '0' || s.p[i] > '9') return 0;
        if (value > (4294967294ULL - (u64)(s.p[i] - '0')) / 10ULL) return 0;
        value = value * 10 + (u64)(s.p[i] - '0');
    }
    *result = (u32)value;
    return 1;
}

static int valid_account_name(struct span s) {
    usize i;
    if (!s.n || s.n > 32 || !((s.p[0] >= 'A' && s.p[0] <= 'Z') ||
        (s.p[0] >= 'a' && s.p[0] <= 'z') || s.p[0] == '_')) return 0;
    for (i = 1; i < s.n; i++)
        if (!((s.p[i] >= 'A' && s.p[i] <= 'Z') || (s.p[i] >= 'a' && s.p[i] <= 'z') ||
              (s.p[i] >= '0' && s.p[i] <= '9') || s.p[i] == '_' || s.p[i] == '.' || s.p[i] == '-'))
            return 0;
    return 1;
}

static int append_environment(struct guest_process *process, struct span name, struct span value, char **result) {
    usize i, start = process->used;
    char *decoded;
    for (i = 0; i < name.n; i++) {
        if (process->used >= PROCESS_MAX_LOCAL) return 0;
        process->arena[process->used++] = name.p[i];
    }
    if (process->used >= PROCESS_MAX_LOCAL) return 0;
    process->arena[process->used++] = '=';
    if (!decode_process_string(process, value, &decoded)) return 0;
    (void)decoded;
    *result = process->arena + start;
    return 1;
}

static int parse_process(struct parser *j, struct guest_process *process) {
    u32 argc = 0, i;
    int path_seen = 0;
    u64 number;
    struct span s;
    usize decoded;
    memset(process, 0, sizeof(*process));
    if (!take_char(j, '{') || !key(j, "argv") || !take_char(j, '[')) return 0;
    if (!take_char(j, ']')) {
        for (;;) {
            if (argc >= ARG_MAX_LOCAL || !json_string(j, &s, &decoded, 0) || decoded > STRING_MAX_LOCAL) return 0;
            if (argc == 0 && decoded == 0) return 0;
            if (!decode_process_string(process, s, &process->argv[argc])) return 0;
            j->process_bytes += decoded + 1;
            argc++;
            if (take_char(j, ']')) break;
            if (!take_char(j, ',')) return 0;
        }
    }
    if (!argc || !take_char(j, ',') || !key(j, "cwd") || !json_string(j, &s, &decoded, 0) ||
        decoded > STRING_MAX_LOCAL || !valid_cwd(s)) return 0;
    if (!decode_process_string(process, s, &process->cwd)) return 0;
    j->process_bytes += decoded;
    if (!take_char(j, ',') || !key(j, "environment") || !take_char(j, '[')) return 0;
    if (!take_char(j, ']')) {
        for (;;) {
            struct span name, value;
            usize value_decoded;
            if (j->env_count >= ENV_MAX_LOCAL || !take_char(j, '{') || !key(j, "name") ||
                !plain_string(j, &name, &decoded) || !valid_env_name(name) || !take_char(j, ',') ||
                !key(j, "value") || !json_string(j, &value, &value_decoded, 0) ||
                value_decoded > STRING_MAX_LOCAL || !take_char(j, '}')) return 0;
            for (i = 0; i < j->env_count; i++)
                if (j->env_names[i].n == name.n && bytes_equal(j->env_names[i].p, name.p, name.n)) return 0;
            if (name.n == 4 && bytes_equal(name.p, "PATH", 4)) path_seen = 1;
            j->env_names[j->env_count++] = name;
            if (!append_environment(process, name, value, &process->envp[process->envc])) return 0;
            process->envc++;
            j->process_bytes += decoded + value_decoded + 2;
            if (take_char(j, ']')) break;
            if (!take_char(j, ',')) return 0;
        }
    }
    if (!take_char(j, ',') || !key(j, "stop_signal") || !uint_value(j, &number) || number < 1 || number > 64 ||
        !take_char(j, ',') || !key(j, "user") || !take_char(j, '{') || !key(j, "group")) return 0;
    process->stop_signal = (u32)number;
    if (exact_literal(j, "null")) {
        process->group_present = 0;
        j->process_bytes += 4 + 23;
    } else {
        if (!plain_string(j, &s, &decoded) ||
            (!numeric_account(s, &process->gid) && !valid_account_name(s)) ||
            !decode_process_string(process, s, &process->group_name)) return 0;
        process->group_present = 1;
        process->group_numeric = numeric_account(s, &process->gid);
        j->process_bytes += decoded + 25;
    }
    if (!take_char(j, ',') || !key(j, "user") || !plain_string(j, &s, &decoded) ||
        (!numeric_account(s, &process->uid) && !valid_account_name(s)) ||
        !decode_process_string(process, s, &process->user_name) ||
        !take_char(j, '}') || !take_char(j, '}')) return 0;
    process->user_numeric = numeric_account(s, &process->uid);
    j->process_bytes += decoded;
    process->argc = argc;
    process->argv[argc] = 0;
    process->envp[process->envc] = 0;
    return path_seen && j->process_bytes <= PROCESS_MAX_LOCAL && process->used <= PROCESS_MAX_LOCAL + 1;
}

static int parse_plan(const u8 *payload, usize size, const struct bindings *b, struct expected_device_set *devices,
                      struct guest_process *process) {
    struct parser j;
    u32 layer_count = 0, i;
    u64 verification_bytes = 0;
    char layer_serials[LOWER_MAX][21];
    char occurrence_digest[72];
    char root_serial[21];
    struct span s;
    u64 number;
    j.p = payload; j.end = payload + size; j.process_bytes = 0; j.env_count = 0;
    if (!take_char(&j, '{') || !key(&j, "assembly") || !take_char(&j, '{') ||
        !key(&j, "device_policy") || !exact_string(&j, "virtio-serial-sysfs.v1") ||
        !take_char(&j, ',') || !key(&j, "layers") || !take_char(&j, '[')) return 0;
    if (!take_char(&j, ']')) {
        for (;;) {
            if (layer_count >= LOWER_MAX || !take_char(&j, '{') || !key(&j, "filesystem") ||
                !exact_string(&j, "squashfs")) return 0;
            if (!take_char(&j, ',') || !key(&j, "image_digest") || !plain_string(&j, &s, 0) ||
                !digest_bytes(s, devices->lowers[layer_count].digest) || !take_char(&j, ',') ||
                !key(&j, "mount_options") ||
                !exact_string_array3(&j, "ro", "nodev", "nosuid") || !take_char(&j, ',') ||
                !key(&j, "occurrence_digest") || !plain_string(&j, &s, 0) || !valid_digest(s.p, s.n) ||
                !copy_span(occurrence_digest, sizeof(occurrence_digest), s) || !take_char(&j, ',') ||
                !key(&j, "ordinal") || !uint_value(&j, &number) || number != layer_count ||
                !take_char(&j, ',') || !key(&j, "serial") || !plain_string(&j, &s, 0) ||
                !valid_serial(s.p, s.n)) return 0;
            memcpy(layer_serials[layer_count], s.p, 20); layer_serials[layer_count][20] = 0;
            memcpy(devices->lowers[layer_count].serial, s.p, 20); devices->lowers[layer_count].serial[20] = 0;
            if (!derived_serial_matches("lower", (struct span){occurrence_digest, 71},
                                        devices->lowers[layer_count].serial)) return 0;
            devices->lowers[layer_count].read_only = 1;
            devices->lowers[layer_count].has_digest = 1;
            if (!take_char(&j, ',') || !key(&j, "size_bytes") || !uint_value(&j, &number) ||
                number < 512 || number > 34359738368ul || number % 512 || !take_char(&j, '}')) return 0;
            devices->lowers[layer_count].size = number;
            if (verification_bytes > FILESYSTEM_VERIFY_BYTES_MAX - number) return 0;
            verification_bytes += number;
            layer_count++;
            if (take_char(&j, ']')) break;
            if (!take_char(&j, ',')) return 0;
        }
    }
    if (!layer_count || layer_count != b->lower_count || !take_char(&j, ',') ||
        !key(&j, "lowerdir_ordinals") || !take_char(&j, '[')) return 0;
    for (i = 0; i < layer_count; i++) {
        if (i && !take_char(&j, ',')) return 0;
        if (!uint_value(&j, &number) || number != layer_count - 1 - i) return 0;
    }
    if (!take_char(&j, ']') || !take_char(&j, ',') || !key(&j, "overlay_mount_options") ||
        !exact_string_array3(&j, "rw", "nodev", "nosuid") || !take_char(&j, ',') ||
        !key(&j, "probes") || !take_char(&j, '[')) return 0;
    if (!take_char(&j, ']')) {
        for (;;) {
            u64 probe_size;
            u32 probe_index;
            if (devices->probe_count >= PROBE_MAX || !take_char(&j, '{') || !key(&j, "digest") ||
                !plain_string(&j, &s, 0) || !digest_bytes(s, devices->probes[devices->probe_count].digest) ||
                !take_char(&j, ',') || !key(&j, "path") || !plain_string(&j, &s, 0) ||
                !valid_probe_path(s) || !copy_span(devices->probes[devices->probe_count].path,
                                                    sizeof(devices->probes[devices->probe_count].path), s) ||
                !take_char(&j, ',') || !key(&j, "size_bytes") || !uint_value(&j, &number) ||
                number < 1 || number > PROBE_BYTES_MAX) return 0;
            probe_size = number;
            for (probe_index = 0; probe_index < devices->probe_count; probe_index++)
                if (text_equal(devices->probes[probe_index].path,
                               devices->probes[devices->probe_count].path)) return 0;
            if (!take_char(&j, ',') ||
                !key(&j, "top_ordinal") || !uint_value(&j, &number) ||
                number != layer_count - 1 || !take_char(&j, '}')) return 0;
            devices->probes[devices->probe_count].size = probe_size;
            devices->probes[devices->probe_count].top_ordinal = layer_count - 1;
            devices->probe_count++;
            if (take_char(&j, ']')) break;
            if (!take_char(&j, ',')) return 0;
        }
    }
    if (!take_char(&j, ',') ||
        !key(&j, "root") || !take_char(&j, '{') || !key(&j, "filesystem") || !exact_string(&j, "ext4") ||
        !take_char(&j, ',') || !key(&j, "filesystem_uuid") || !plain_string(&j, &s, 0) ||
        !uuid_bytes(s, devices->root.filesystem_uuid) ||
        !take_char(&j, ',') || !key(&j, "generation") || !positive_decimal(&j) ||
        !take_char(&j, ',') || !key(&j, "mount_options") || !exact_string_array3(&j, "rw", "nodev", "nosuid") ||
        !take_char(&j, ',') || !key(&j, "serial") || !plain_string(&j, &s, 0) || !valid_serial(s.p, s.n) ||
        s.n != 20 || !bytes_equal(s.p, b->root_serial, 20)) return 0;
    memcpy(devices->root.serial, s.p, 20); devices->root.serial[20] = 0; devices->root.read_only = 0;
    memcpy(root_serial, s.p, 20); root_serial[20] = 0;
    if (!take_char(&j, ',') || !key(&j, "size_bytes") || !uint_value(&j, &number) ||
        number < 16777216 || number > 17592186044416ul || number % 1048576) return 0;
    devices->root.size = number;
    if (!take_char(&j, ',') ||
        !key(&j, "volume_id") || !plain_string(&j, &s, 0) || !valid_uuid_span(s) ||
        !derived_serial_matches("root", s, root_serial) || !take_char(&j, '}') ||
        !take_char(&j, ',') || !key(&j, "root_layout") ||
        !exact_string(&j, "overlay-upper-work.v1") || !take_char(&j, '}')) return 0;
    root_label(s, devices->root.label);
    for (i = 0; i < layer_count; i++) if (!text_equal(layer_serials[i], b->lowers[i])) return 0;
    if (!take_char(&j, ',') || !key(&j, "boot_plan_digest") || !plain_string(&j, &s, 0) ||
        s.n != 71 || !bytes_equal(s.p, b->resource, 71) || !take_char(&j, ',') ||
        !key(&j, "domain_core_digest") || !plain_string(&j, &s, 0) || s.n != 71 ||
        !bytes_equal(s.p, b->core, 71) || !take_char(&j, ',') || !key(&j, "handoff") ||
        !exact_string(&j, "first-party-pid1-supervisor.v7") || !take_char(&j, ',') ||
        !key(&j, "isolation") ||
        !exact_string(&j, "palimpsest.workload-lifecycle-authority-isolation.v2") || !take_char(&j, ',') ||
        !key(&j, "phase") || !exact_string(&j, "stage1-contract") || !take_char(&j, ',') ||
        !key(&j, "process") || !parse_process(&j, process) || !take_char(&j, ',') ||
        !key(&j, "process_policy") ||
        !exact_string(&j, "image-root-account-path-capabilityless-isolated-user-group.v3") ||
        !take_char(&j, ',') || !key(&j, "protocol") || !exact_string(&j, "palimpsest.guest-stage1.v13") ||
        !take_char(&j, ',') || !key(&j, "run") || !take_char(&j, '{') || !key(&j, "name") ||
        !plain_string(&j, &s, 0) || !valid_run_name(s) || !take_char(&j, ',') || !key(&j, "run_id") ||
        !plain_string(&j, &s, 0) || !valid_uuid_span(s) || !take_char(&j, '}') || !take_char(&j, ',') ||
        !copy_span(lifecycle_binding.run_id, sizeof(lifecycle_binding.run_id), s) ||
        !key(&j, "schema") || !exact_string(&j, "palimpsest.oci-stage1-plan.v13") || !take_char(&j, '}') ||
        j.p != j.end) return 0;
    memcpy(lifecycle_binding.core, b->core, sizeof(lifecycle_binding.core));
    memcpy(lifecycle_binding.stage1, b->transport, sizeof(lifecycle_binding.stage1));
    devices->lower_count = layer_count;
    return 1;
}

static u8 artifact[ARTIFACT_MAX];
static char cmdline[CMDLINE_MAX + 1];
static u8 attribute[128];
static u8 filesystem_io[FILESYSTEM_IO_BYTES];
static struct guest_process workload;

static u32 little16(const u8 *p) { return (u32)p[0] | ((u32)p[1] << 8); }
static u32 little32(const u8 *p) { return (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24); }
static u64 little64(const u8 *p) { return (u64)little32(p) | ((u64)little32(p + 4) << 32); }

static int pread_exact(int fd, u8 *out, usize size, u64 offset) {
    usize used = 0;
    while (used < size) {
        i64 n = sc4(SYS_pread64, fd, (i64)(out + used), (i64)(size - used), (i64)(offset + used));
        if (n <= 0 || (usize)n > size - used) return 0;
        used += (usize)n;
    }
    return 1;
}

static u32 crc32c(u32 value, const u8 *payload, usize size) {
    usize i;
    for (i = 0; i < size; i++) {
        u32 bit;
        value ^= payload[i];
        for (bit = 0; bit < 8; bit++) value = (value >> 1) ^ (value & 1 ? 0x82f63b78u : 0);
    }
    return value;
}

static u64 ceil_div_u64(u64 value, u64 divisor) { return value ? 1 + (value - 1) / divisor : 0; }

static int verify_ext4_fd(int fd, const struct expected_device *expected) {
    const u8 *s = filesystem_io;
    u32 log_block, block_size, compat, incompat, ro_compat, blocks_high, first_data;
    u32 blocks_per_group, inodes, inodes_per_group, inode_size, descriptor_size;
    u64 blocks, groups, inode_groups;
    if (!pread_exact(fd, filesystem_io, 1024, 1024)) return 0;
    if (little16(s + 56) != 0xef53 || little32(s + 76) != 1) return 0;
    log_block = little32(s + 24);
    if (log_block > 6) return 0;
    block_size = 1024u << log_block;
    compat = little32(s + 92); incompat = little32(s + 96); ro_compat = little32(s + 100);
    if ((incompat & 0x42) != 0x42 || (compat & ~0x3fu) || (incompat & ~0x2c6u) || (ro_compat & ~0x47bu)) return 0;
    blocks_high = little32(s + 336);
    if (!(incompat & 0x80) && blocks_high) return 0;
    blocks = little32(s + 4) | ((incompat & 0x80) ? ((u64)blocks_high << 32) : 0);
    if (!blocks || blocks > ~(u64)0 / block_size || blocks * block_size != expected->size) return 0;
    first_data = little32(s + 20); blocks_per_group = little32(s + 32);
    inodes = little32(s); inodes_per_group = little32(s + 40);
    if (first_data >= blocks || first_data != (block_size == 1024 ? 1u : 0u) ||
        !blocks_per_group || blocks_per_group > block_size * 8u || !inodes || !inodes_per_group) return 0;
    groups = ceil_div_u64(blocks - first_data, blocks_per_group);
    inode_groups = ceil_div_u64(inodes, inodes_per_group);
    if (!groups || groups != inode_groups) return 0;
    inode_size = little16(s + 88);
    if (inode_size < 128 || inode_size > block_size || (inode_size & (inode_size - 1))) return 0;
    if (incompat & 0x80) {
        descriptor_size = little16(s + 254);
        if (descriptor_size < 64 || descriptor_size > block_size || (descriptor_size & 7)) return 0;
    }
    if (!bytes_equal(s + 104, expected->filesystem_uuid, 16)) return 0;
    if (!bytes_equal(s + 120, expected->label, 16)) return 0;
    if (ro_compat & 0x400) {
        if (s[0x175] != 1) return 0;
        if (crc32c(0xffffffffu, s, 1020) != little32(s + 1020)) return 0;
    }
    return 1;
}

static int verify_squashfs_structure_fd(int fd, const struct expected_device *expected) {
    const u8 *s = filesystem_io;
    u32 inodes, block_size, fragments, compression, block_log, flags, id_count, i;
    u64 root_inode, bytes_used, offsets[6], required[3], padding, root_offset;
    if (!pread_exact(fd, filesystem_io, 96, 0)) return 0;
    inodes = little32(s + 4); block_size = little32(s + 12); fragments = little32(s + 16);
    compression = little16(s + 20); block_log = little16(s + 22); flags = little16(s + 24);
    id_count = little16(s + 26);
    if (little32(s) != 0x73717368u || !inodes || little32(s + 8) ||
        little16(s + 28) != 4 || little16(s + 30) != 0) return 0;
    if (block_size < 4096 || block_size > 1024 * 1024 || (block_size & (block_size - 1))) return 0;
    for (i = 0; (1u << i) != block_size && i < 31; i++) {}
    if (i != block_log || compression < 1 || compression > 6 || !id_count) return 0;
    root_inode = little64(s + 32); bytes_used = little64(s + 40);
    for (i = 0; i < 6; i++) offsets[i] = little64(s + 48 + i * 8);
    if (bytes_used < 96 || bytes_used > expected->size) return 0;
    for (i = 0; i < 6; i++) if (offsets[i] != ~(u64)0 && (offsets[i] < 96 || offsets[i] >= bytes_used)) return 0;
    required[0] = offsets[0]; required[1] = offsets[2]; required[2] = offsets[3];
    if (required[0] == ~(u64)0 || required[1] == ~(u64)0 || required[2] == ~(u64)0 ||
        required[0] == required[1] || required[0] == required[2] || required[1] == required[2]) return 0;
    if (required[1] >= required[2] || (root_inode >> 16) >= bytes_used - required[1]) return 0;
    root_offset = required[1] + (root_inode >> 16);
    if (root_offset >= bytes_used) return 0;
    if ((fragments == 0) != (offsets[4] == ~(u64)0)) return 0;
    if (!!(flags & 0x80) != (offsets[5] != ~(u64)0)) return 0;
    padding = expected->size - bytes_used;
    if (padding >= block_size) return 0;
    while (padding) {
        usize chunk = padding > FILESYSTEM_IO_BYTES ? FILESYSTEM_IO_BYTES : (usize)padding;
        if (!pread_exact(fd, filesystem_io, chunk, bytes_used)) return 0;
        for (i = 0; i < chunk; i++) if (filesystem_io[i]) return 0;
        bytes_used += chunk; padding -= chunk;
    }
    return 1;
}

static int verify_lower_digest_fd(int fd, const struct expected_device *expected) {
    struct sha256_ctx c;
    u8 digest[32];
    u64 offset = 0;
    if (!expected->has_digest || expected->size > FILESYSTEM_VERIFY_BYTES_MAX) return 0;
    sha_init(&c);
    while (offset < expected->size) {
        usize chunk = expected->size - offset > FILESYSTEM_IO_BYTES ? FILESYSTEM_IO_BYTES : (usize)(expected->size - offset);
        if (!pread_exact(fd, filesystem_io, chunk, offset)) return 0;
        sha_update(&c, filesystem_io, chunk); offset += chunk;
    }
    sha_final(&c, digest);
    return bytes_equal(digest, expected->digest, 32);
}

static int open_fixture_role(const char *root, const char *suffix, const struct expected_device *expected,
                             int require_nonwritable) {
    char path[PATH_MAX_LOCAL];
    struct stat_local before, after;
    i64 fd;
    int verified;
    if (!fixture_path(path, root, suffix)) return 0;
    fd = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&before) < 0 || (before.mode & S_IFMT) != S_IFREG ||
        before.nlink != 1 || before.size < 0 || (u64)before.size != expected->size || (before.mode & 0022) ||
        (require_nonwritable && (before.mode & 0222))) {
        if (fd >= 0) sc1(SYS_close, fd);
        return 0;
    }
    verified = require_nonwritable ?
        (verify_squashfs_structure_fd((int)fd, expected) && verify_lower_digest_fd((int)fd, expected)) :
        verify_ext4_fd((int)fd, expected);
    if (!verified || sc2(SYS_fstat, fd, (i64)&after) < 0 || before.dev != after.dev ||
        before.ino != after.ino || before.mode != after.mode || before.nlink != after.nlink ||
        before.size != after.size) {
        sc1(SYS_close, fd);
        return 0;
    }
    sc1(SYS_close, fd);
    return 1;
}

static int verify_fixture_filesystems(const char *root, const struct expected_device_set *expected) {
    char suffix[16];
    u32 i;
    if (!open_fixture_role(root, "/root.raw", &expected->root, 0)) return 0;
    for (i = 0; i < expected->lower_count; i++) {
        memset(suffix, 0, sizeof(suffix));
        memcpy(suffix, "/lower-", 7);
        if (i < 10) {
            suffix[7] = (char)('0' + i);
            memcpy(suffix + 8, ".raw", 5);
        } else {
            suffix[7] = (char)('0' + i / 10);
            suffix[8] = (char)('0' + i % 10);
            memcpy(suffix + 9, ".raw", 5);
        }
        if (!open_fixture_role(root, suffix, &expected->lowers[i], 1)) return 0;
    }
    return 1;
}

static int parse_u32_decimal(const u8 *p, usize n, u32 *value) {
    u64 v = 0;
    usize i;
    if (!n) return 0;
    for (i = 0; i < n; i++) { if (p[i] < '0' || p[i] > '9') return 0; v = v * 10 + p[i] - '0'; if (v > 0xffffffffu) return 0; }
    *value = (u32)v;
    return 1;
}

static u32 dev_major(u64 dev) { return (u32)(((dev >> 8) & 0xfff) | ((dev >> 32) & 0xfffff000)); }
static u32 dev_minor(u64 dev) { return (u32)((dev & 0xff) | ((dev >> 12) & 0xffffff00)); }

struct discovered {
    char name[4];
    char path[PATH_MAX_LOCAL];
    char serial_path[PATH_MAX_LOCAL];
    char ro_path[PATH_MAX_LOCAL];
    char dev_path[PATH_MAX_LOCAL];
    char driver_path[PATH_MAX_LOCAL];
    u64 size;
    u32 major;
    u32 minor;
    u64 identity_dev;
    u64 identity_ino;
    int fixture;
};

static int read_exact_attr(const char *path, const char *expected) {
    i64 n = read_bounded_file(path, attribute, sizeof(attribute), 1, 0);
    usize en = slen(expected);
    return n == (i64)en && bytes_equal(attribute, expected, en);
}

static int discover(const char *root, int fixture, const struct bindings *b, struct discovered *found) {
    char base[PATH_MAX_LOCAL], path[PATH_MAX_LOCAL], selected_name[4] = {0};
    u32 matches = 0, letter;
    if (fixture) {
        if (!fixture_path(base, root, "/sys/class/block")) return 0;
    } else memcpy(base, "/sys/class/block", 17);
    for (letter = 0; letter < 26; letter++) {
        usize used = slen(base);
        i64 n;
        if (used + 5 >= sizeof(path)) return 0;
        memcpy(path, base, used); path[used++] = '/'; path[used++] = 'v'; path[used++] = 'd'; path[used++] = (char)('a' + letter); path[used] = 0;
        if (!fixture) {
            char link[PATH_MAX_LOCAL];
            usize k;
            n = sc3(SYS_readlink, (i64)path, (i64)link, sizeof(link));
            if (n < 0) continue;
            if (n < 10 || !starts(link, (usize)n, "../../devices/")) return 0;
            for (k = 14; k < (usize)n;) {
                usize component = k;
                while (k < (usize)n && link[k] != '/') k++;
                if (k == component || (k - component == 1 && link[component] == '.') ||
                    (k - component == 2 && link[component] == '.' && link[component + 1] == '.')) return 0;
                k++;
            }
        }
        if (used + 8 >= sizeof(path)) return 0;
        memcpy(path + used, "/serial", 8); path[used + 7] = 0;
        n = read_bounded_file(path, attribute, 64, 1, 0);
        if (n < 0) continue;
        if (n == 21 && attribute[20] == '\n') n = 20;
        if (n != 20 || !valid_serial((char *)attribute, 20)) continue;
        if (!bytes_equal(attribute, b->transport_serial, 20)) continue;
        matches++;
        selected_name[0] = 'v'; selected_name[1] = 'd'; selected_name[2] = (char)('a' + letter);
    }
    if (matches != 1) return 0;
    {
        usize used = slen(base);
        memcpy(path, base, used); path[used++] = '/'; memcpy(path + used, selected_name, 3); used += 3; path[used] = 0;
        if (fixture) {
            memcpy(path + used, "/driver", 8); path[used + 7] = 0;
            if (!read_exact_attr(path, "virtio_blk\n")) return 0;
        } else {
            char link[PATH_MAX_LOCAL];
            memcpy(path + used, "/device/driver", 15); path[used + 14] = 0;
            i64 n = sc3(SYS_readlink, (i64)path, (i64)link, sizeof(link));
            if (n < 11 || !bytes_equal(link + n - 11, "/virtio_blk", 11)) return 0;
            memcpy(found->driver_path, path, slen(path) + 1);
        }
        memcpy(path + used, "/ro", 4); path[used + 3] = 0;
        if (!read_exact_attr(path, "1\n")) return 0;
        memcpy(path + used, "/dev", 5); path[used + 4] = 0;
        {
            i64 n = read_bounded_file(path, attribute, 32, 1, 0);
            usize colon = 0, end;
            u32 major, minor;
            if (n < 4 || attribute[n - 1] != '\n') return 0;
            end = (usize)n - 1;
            while (colon < end && attribute[colon] != ':') colon++;
            if (colon == end || !parse_u32_decimal(attribute, colon, &major) ||
                !parse_u32_decimal(attribute + colon + 1, end - colon - 1, &minor)) return 0;
            if (fixture) {
                if (!fixture_path(found->path, root, "/dev/")) return 0;
                usize fp = slen(found->path); memcpy(found->path + fp, selected_name, 4);
                found->fixture = 1;
                found->major = major;
                found->minor = minor;
            } else {
                struct stat_local st;
                int ro = 0;
                u64 bytes = 0;
                i64 fd;
                memcpy(found->path, "/dev/", 5); memcpy(found->path + 5, selected_name, 4);
                fd = open_read(found->path, 1);
                if (fd < 0 || sc2(SYS_fstat, fd, (i64)&st) < 0 || (st.mode & S_IFMT) != S_IFBLK ||
                    dev_major(st.rdev) != major || dev_minor(st.rdev) != minor ||
                    sc3(SYS_ioctl, fd, BLKROGET, (i64)&ro) < 0 || ro != 1 ||
                    sc3(SYS_ioctl, fd, BLKGETSIZE64, (i64)&bytes) < 0) { if (fd >= 0) sc1(SYS_close, fd); return 0; }
                sc1(SYS_close, fd);
                found->fixture = 0;
                found->size = bytes;
                found->major = major;
                found->minor = minor;
                found->identity_dev = st.dev;
                found->identity_ino = st.ino;
            }
        }
        memcpy(path, base, slen(base)); used = slen(base); path[used++] = '/'; memcpy(path + used, selected_name, 3); used += 3;
        memcpy(path + used, "/serial", 8); path[used + 7] = 0;
        {
            i64 n = read_bounded_file(path, attribute, 64, 1, 0);
            if (n == 21 && attribute[20] == '\n') n = 20;
            if (n != 20 || !bytes_equal(attribute, b->transport_serial, 20)) return 0;
        }
        memcpy(found->serial_path, path, slen(path) + 1);
        used = slen(base); memcpy(path, base, used); path[used++] = '/'; memcpy(path + used, selected_name, 3); used += 3;
        memcpy(path + used, "/ro", 4); path[used + 3] = 0; memcpy(found->ro_path, path, slen(path) + 1);
        used = slen(base); memcpy(path, base, used); path[used++] = '/'; memcpy(path + used, selected_name, 3); used += 3;
        memcpy(path + used, "/dev", 5); path[used + 4] = 0; memcpy(found->dev_path, path, slen(path) + 1);
        memcpy(found->name, selected_name, 4);
    }
    return 1;
}

struct opened_role {
    struct discovered device;
    char serial[21];
    u64 expected_size;
    int expected_ro;
    int fd;
};

static int driver_is_virtio_blk(const char *path) {
    char link[PATH_MAX_LOCAL];
    i64 n = sc3(SYS_readlink, (i64)path, (i64)link, sizeof(link));
    return n >= 11 && bytes_equal(link + n - 11, "/virtio_blk", 11);
}

static int parse_dev_attribute(const char *path, u32 *major, u32 *minor) {
    i64 n = read_bounded_file(path, attribute, 32, 1, 0);
    usize colon = 0, end;
    if (n < 4 || attribute[n - 1] != '\n') return 0;
    end = (usize)n - 1;
    while (colon < end && attribute[colon] != ':') colon++;
    return colon != end && parse_u32_decimal(attribute, colon, major) &&
           parse_u32_decimal(attribute + colon + 1, end - colon - 1, minor);
}

static int discover_live_role(const struct expected_device *expected, struct opened_role *opened) {
    char path[PATH_MAX_LOCAL], selected[4] = {0};
    u32 matches = 0, letter, major, minor;
    for (letter = 0; letter < 26; letter++) {
        char link[PATH_MAX_LOCAL];
        i64 n;
        usize k;
        memcpy(path, "/sys/class/block/vd", 19); path[19] = (char)('a' + letter); path[20] = 0;
        n = sc3(SYS_readlink, (i64)path, (i64)link, sizeof(link));
        if (n < 0) continue;
        if (n < 10 || !starts(link, (usize)n, "../../devices/")) return 0;
        for (k = 14; k < (usize)n;) {
            usize component = k;
            while (k < (usize)n && link[k] != '/') k++;
            if (k == component || (k - component == 1 && link[component] == '.') ||
                (k - component == 2 && link[component] == '.' && link[component + 1] == '.')) return 0;
            k++;
        }
        memcpy(path + 20, "/serial", 8);
        n = read_bounded_file(path, attribute, 64, 1, 0);
        if (n == 21 && attribute[20] == '\n') n = 20;
        if (n == 20 && bytes_equal(attribute, expected->serial, 20)) {
            matches++;
            selected[0] = 'v'; selected[1] = 'd'; selected[2] = (char)('a' + letter);
        }
    }
    if (matches != 1) return 0;
    memcpy(path, "/sys/class/block/", 17); memcpy(path + 17, selected, 3); path[20] = 0;
    {
        char link[PATH_MAX_LOCAL];
        usize used = 20;
        i64 n;
        memcpy(path + used, "/device/driver", 15);
        n = sc3(SYS_readlink, (i64)path, (i64)link, sizeof(link));
        if (n < 11 || !bytes_equal(link + n - 11, "/virtio_blk", 11)) return 0;
        memcpy(opened->device.driver_path, path, slen(path) + 1);
        memcpy(path + used, "/ro", 4);
        if (!read_exact_attr(path, expected->read_only ? "1\n" : "0\n")) return 0;
        memcpy(opened->device.ro_path, path, slen(path) + 1);
        memcpy(path + used, "/dev", 5);
        if (!parse_dev_attribute(path, &major, &minor)) return 0;
        memcpy(opened->device.dev_path, path, slen(path) + 1);
        memcpy(path + used, "/serial", 8);
        n = read_bounded_file(path, attribute, 64, 1, 0);
        if (n == 21 && attribute[20] == '\n') n = 20;
        if (n != 20 || !bytes_equal(attribute, expected->serial, 20)) return 0;
        memcpy(opened->device.serial_path, path, slen(path) + 1);
    }
    memcpy(opened->device.path, "/dev/", 5); memcpy(opened->device.path + 5, selected, 4);
    opened->fd = (int)open_read(opened->device.path, 1);
    if (opened->fd < 0) return 0;
    {
        struct stat_local st;
        int ro = -1;
        u64 bytes = 0;
        if (sc2(SYS_fstat, opened->fd, (i64)&st) < 0 || (st.mode & S_IFMT) != S_IFBLK ||
            dev_major(st.rdev) != major || dev_minor(st.rdev) != minor ||
            sc3(SYS_ioctl, opened->fd, BLKROGET, (i64)&ro) < 0 || ro != expected->read_only ||
            sc3(SYS_ioctl, opened->fd, BLKGETSIZE64, (i64)&bytes) < 0 || bytes != expected->size) {
            sc1(SYS_close, opened->fd); opened->fd = -1; return 0;
        }
    }
    memcpy(opened->serial, expected->serial, 21);
    memcpy(opened->device.name, selected, 4);
    opened->device.major = major; opened->device.minor = minor; opened->device.size = expected->size;
    {
        struct stat_local st;
        if (sc2(SYS_fstat, opened->fd, (i64)&st) < 0) { sc1(SYS_close, opened->fd); opened->fd = -1; return 0; }
        opened->device.identity_dev = st.dev; opened->device.identity_ino = st.ino;
    }
    opened->expected_size = expected->size; opened->expected_ro = expected->read_only;
    return 1;
}

static int recheck_open_role(const struct opened_role *opened) {
    struct stat_local st;
    int ro = -1;
    u64 bytes = 0;
    u32 major, minor;
    i64 n;
    if (opened->fd < 0 || sc2(SYS_fstat, opened->fd, (i64)&st) < 0 ||
        (st.mode & S_IFMT) != S_IFBLK || dev_major(st.rdev) != opened->device.major ||
        dev_minor(st.rdev) != opened->device.minor ||
        st.dev != opened->device.identity_dev || st.ino != opened->device.identity_ino ||
        sc3(SYS_ioctl, opened->fd, BLKROGET, (i64)&ro) < 0 || ro != opened->expected_ro ||
        sc3(SYS_ioctl, opened->fd, BLKGETSIZE64, (i64)&bytes) < 0 || bytes != opened->expected_size ||
        !driver_is_virtio_blk(opened->device.driver_path) ||
        !read_exact_attr(opened->device.ro_path, opened->expected_ro ? "1\n" : "0\n") ||
        !parse_dev_attribute(opened->device.dev_path, &major, &minor) ||
        major != opened->device.major || minor != opened->device.minor) return 0;
    n = read_bounded_file(opened->device.serial_path, attribute, 64, 1, 0);
    if (n == 21 && attribute[20] == '\n') n = 20;
    return n == 20 && bytes_equal(attribute, opened->serial, 20);
}

static int exact_vd_disk_count(u32 expected) {
    u8 entries[4096];
    u32 count = 0;
    i64 fd = sc3(SYS_open, (i64)"/sys/class/block", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (fd < 0) return 0;
    for (;;) {
        i64 n = sc3(SYS_getdents64, fd, (i64)entries, sizeof(entries));
        usize offset = 0;
        if (n < 0) { sc1(SYS_close, fd); return 0; }
        if (!n) break;
        while (offset < (usize)n) {
            const u8 *entry = entries + offset;
            usize i, name_bytes;
            u32 reclen;
            if ((usize)n - offset < 20) { sc1(SYS_close, fd); return 0; }
            reclen = (u32)entry[16] | ((u32)entry[17] << 8);
            if (reclen < 20 || reclen > (usize)n - offset) { sc1(SYS_close, fd); return 0; }
            name_bytes = reclen - 19;
            for (i = 0; i < name_bytes && entry[19 + i]; i++) {}
            if (i == name_bytes) { sc1(SYS_close, fd); return 0; }
            if (i >= 2 && entry[19] == 'v' && entry[20] == 'd') {
                usize suffix;
                if (i < 3) { sc1(SYS_close, fd); return 0; }
                for (suffix = 2; suffix < i; suffix++)
                    if (entry[19 + suffix] < 'a' || entry[19 + suffix] > 'z') {
                        sc1(SYS_close, fd); return 0;
                    }
                count++;
            }
            offset += reclen;
        }
    }
    sc1(SYS_close, fd);
    return count == expected;
}

static int verify_transport(const struct discovered *device, const struct bindings *b,
                            struct expected_device_set *devices, struct guest_process *process, int *kept_fd) {
    struct stat_local st;
    i64 fd = open_read(device->path, 1), n;
    u64 payload_size, total, offset = 0;
    u8 payload_digest[32];
    char artifact_digest[72];
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&st) < 0) { if (fd >= 0) sc1(SYS_close, fd); return EXIT_TRANSPORT; }
    if (device->fixture && ((st.mode & S_IFMT) != S_IFREG || st.nlink != 1 || (st.mode & 0022))) {
        sc1(SYS_close, fd); return EXIT_TRANSPORT;
    }
    if (!device->fixture) {
        int ro = 0;
        u64 bytes = 0;
        if ((st.mode & S_IFMT) != S_IFBLK || st.dev != device->identity_dev || st.ino != device->identity_ino ||
            dev_major(st.rdev) != device->major || dev_minor(st.rdev) != device->minor ||
            sc3(SYS_ioctl, fd, BLKROGET, (i64)&ro) < 0 || ro != 1 ||
            sc3(SYS_ioctl, fd, BLKGETSIZE64, (i64)&bytes) < 0 || bytes != device->size) {
            sc1(SYS_close, fd); return EXIT_TRANSPORT;
        }
    }
    while (offset < 64) {
        n = sc4(SYS_pread64, fd, (i64)(artifact + offset), 64 - offset, offset);
        if (n <= 0) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
        offset += (u64)n;
    }
    if (!bytes_equal(artifact, "PALIMPSEST-S1\0\0\0", 16) || little32(artifact + 16) != 1 ||
        little32(artifact + 20) != 64) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
    payload_size = little64(artifact + 24);
    if (!payload_size || payload_size > PAYLOAD_MAX) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
    total = (64 + payload_size + 4095) & ~(u64)4095;
    if (total > ARTIFACT_MAX || (!device->fixture && device->size != total) ||
        (device->fixture && (u64)st.size != total)) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
    while (offset < total) {
        n = sc4(SYS_pread64, fd, (i64)(artifact + offset), total - offset, offset);
        if (n <= 0) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
        offset += (u64)n;
    }
    if (!device->fixture) {
        struct stat_local after;
        int ro = 0;
        u64 bytes = 0;
        if (sc2(SYS_fstat, fd, (i64)&after) < 0 || after.dev != st.dev || after.ino != st.ino ||
            after.rdev != st.rdev || (after.mode & S_IFMT) != S_IFBLK ||
            sc3(SYS_ioctl, fd, BLKROGET, (i64)&ro) < 0 || ro != 1 ||
            sc3(SYS_ioctl, fd, BLKGETSIZE64, (i64)&bytes) < 0 || bytes != device->size ||
            !read_exact_attr(device->ro_path, "1\n")) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
        {
            i64 an = read_bounded_file(device->serial_path, attribute, 64, 1, 0);
            if (an == 21 && attribute[20] == '\n') an = 20;
            if (an != 20 || !bytes_equal(attribute, b->transport_serial, 20)) {
                sc1(SYS_close, fd); return EXIT_TRANSPORT;
            }
        }
        {
            i64 an = read_bounded_file(device->dev_path, attribute, 32, 1, 0);
            usize colon = 0, end;
            u32 major, minor;
            if (an < 4 || attribute[an - 1] != '\n') { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
            end = (usize)an - 1;
            while (colon < end && attribute[colon] != ':') colon++;
            if (colon == end || !parse_u32_decimal(attribute, colon, &major) ||
                !parse_u32_decimal(attribute + colon + 1, end - colon - 1, &minor) ||
                major != device->major || minor != device->minor) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
        }
    }
    digest_text(artifact, total, artifact_digest);
    if (!text_equal(artifact_digest, b->transport)) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
    sha_bytes(artifact + 64, payload_size, payload_digest);
    if (!bytes_equal(payload_digest, artifact + 32, 32)) { sc1(SYS_close, fd); return EXIT_TRANSPORT; }
    for (offset = 64 + payload_size; offset < total; offset++) if (artifact[offset]) {
        sc1(SYS_close, fd); return EXIT_TRANSPORT;
    }
    if (!parse_plan(artifact + 64, payload_size, b, devices, process)) { sc1(SYS_close, fd); return EXIT_PLAN; }
    if (device->fixture) sc1(SYS_close, fd); else *kept_fd = (int)fd;
    return 0;
}

static int mkdir_ok(const char *path) {
    i64 r = sc2(SYS_mkdir, (i64)path, 0755);
    return r == 0 || r == -EEXIST;
}

static int mount_ok(const char *source, const char *target, const char *type, u64 flags, i64 magic) {
    i64 r = sc5(SYS_mount, (i64)source, (i64)target, (i64)type, flags, 0);
    struct statfs_local fs;
    if (r != 0 && r != -EBUSY) return 0;
    return sc2(SYS_statfs, (i64)target, (i64)&fs) == 0 && fs.type == magic;
}

static int prepare_live(void) {
    return mkdir_ok("/proc") && mkdir_ok("/sys") && mkdir_ok("/dev") &&
           mount_ok("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, 0x9fa0) &&
           mount_ok("sysfs", "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, 0x62656572) &&
           mount_ok("devtmpfs", "/dev", "devtmpfs", MS_NOSUID | MS_NOEXEC, 0x01021994);
}

#define MOUNTINFO_MAX (64 * 1024)
#define EXT4_MAGIC 0xef53
#define SQUASHFS_MAGIC 0x73717368
#define OVERLAYFS_MAGIC 0x794c7630
#define CGROUP2_MAGIC 0x63677270

static u8 mountinfo[MOUNTINFO_MAX + 1];

static int append_u32(char *out, usize *used, u32 value) {
    char digits[10];
    usize count = 0, i;
    do { digits[count++] = (char)('0' + value % 10); value /= 10; } while (value);
    if (*used + count + 1 > PATH_MAX_LOCAL) return 0;
    for (i = 0; i < count; i++) out[(*used)++] = digits[count - 1 - i];
    out[*used] = 0;
    return 1;
}

static u8 control_payload[CONTROL_PAYLOAD_MAX];
static u8 control_output[CONTROL_GUEST_OUTPUT_MAX];
static struct parser control_parser;

static u64 monotonic_millis(void) {
    struct timespec_local now;
    if (sc2(SYS_clock_gettime, CLOCK_MONOTONIC, (i64)&now) != 0 || now.sec < 0 || now.nsec < 0)
        return 0;
    return (u64)now.sec * 1000 + (u64)now.nsec / 1000000;
}

static int wait_control_fd(int fd, short events, u64 deadline) {
    struct pollfd_local item;
    for (;;) {
        u64 now = monotonic_millis();
        int timeout;
        i64 n;
        if (!now || now >= deadline) return 0;
        timeout = (int)(deadline - now > 100 ? 100 : deadline - now);
        item.fd = fd; item.events = events; item.revents = 0;
        n = sc3(SYS_poll, (i64)&item, 1, timeout);
        if (n == -EINTR) continue;
        if (n > 0 && (item.revents & events)) return 1;
        if (n < 0 || (item.revents & (POLLERR | POLLHUP))) return 0;
    }
}

static int control_io_exact(int fd, u8 *buffer, usize size, int writing) {
    usize used = 0;
    u64 now = monotonic_millis();
    u64 deadline = now ? now + 5000 : 0;
    if (!deadline) return 0;
    while (used < size) {
        i64 n = writing ? sc3(SYS_write, fd, (i64)(buffer + used), size - used)
                        : sc3(SYS_read, fd, (i64)(buffer + used), size - used);
        if (n > 0 && (usize)n <= size - used) { used += (usize)n; continue; }
        if (n == -EINTR) continue;
        if (n == -EAGAIN && wait_control_fd(fd, writing ? POLLOUT : POLLIN, deadline)) continue;
        return 0;
    }
    return 1;
}

static int vport_name(const u8 *name, usize size) {
    usize i = 5;
    if (size < 8 || !bytes_equal(name, "vport", 5)) return 0;
    if (name[i] < '0' || name[i] > '9') return 0;
    while (i < size && name[i] >= '0' && name[i] <= '9') i++;
    if (i >= size || name[i++] != 'p' || i >= size) return 0;
    while (i < size && name[i] >= '0' && name[i] <= '9') i++;
    return i == size;
}

static int discover_lifecycle_channel(void) {
    u8 entries[4096];
    char selected[64] = {0};
    char name_path[128], dev_path[128], node_path[80];
    u32 matches = 0, major = 0, minor = 0;
    i64 directory = sc3(SYS_open, (i64)"/sys/class/virtio-ports",
                        O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (directory < 0) return -1;
    for (;;) {
        i64 n = sc3(SYS_getdents64, directory, (i64)entries, sizeof(entries));
        usize offset = 0;
        if (n < 0) { sc1(SYS_close, directory); return -1; }
        if (!n) break;
        while (offset < (usize)n) {
            const u8 *entry = entries + offset;
            usize length, used;
            u32 reclen;
            if ((usize)n - offset < 20) { sc1(SYS_close, directory); return -1; }
            reclen = (u32)entry[16] | ((u32)entry[17] << 8);
            if (reclen < 20 || reclen > (usize)n - offset) { sc1(SYS_close, directory); return -1; }
            for (length = 0; length < reclen - 19 && entry[19 + length]; length++) {}
            if (length == reclen - 19) { sc1(SYS_close, directory); return -1; }
            if (vport_name(entry + 19, length)) {
                if (length + 32 >= sizeof(name_path)) { sc1(SYS_close, directory); return -1; }
                memcpy(name_path, "/sys/class/virtio-ports/", 24); used = 24;
                memcpy(name_path + used, entry + 19, length); used += length;
                memcpy(name_path + used, "/name", 6);
                if (read_exact_attr(name_path, LIFECYCLE_CHANNEL_NAME "\n")) {
                    matches++;
                    if (matches > 1 || length + 1 > sizeof(selected)) { sc1(SYS_close, directory); return -1; }
                    memcpy(selected, entry + 19, length); selected[length] = 0;
                }
            }
            offset += reclen;
        }
    }
    sc1(SYS_close, directory);
    if (matches != 1) return -1;
    memcpy(name_path, "/sys/class/virtio-ports/", 24);
    memcpy(name_path + 24, selected, slen(selected));
    memcpy(name_path + 24 + slen(selected), "/name", 6);
    memcpy(dev_path, "/sys/class/virtio-ports/", 24);
    memcpy(dev_path + 24, selected, slen(selected));
    memcpy(dev_path + 24 + slen(selected), "/dev", 5);
    if (!parse_dev_attribute(dev_path, &major, &minor)) return -1;
    memcpy(node_path, "/dev/", 5); memcpy(node_path + 5, selected, slen(selected) + 1);
    {
        struct stat_local st;
        i64 fd = sc3(SYS_open, (i64)node_path,
                     O_RDWR | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW | O_NOCTTY, 0);
        if (fd < 0 || sc2(SYS_fstat, fd, (i64)&st) < 0 || (st.mode & S_IFMT) != S_IFCHR ||
            dev_major(st.rdev) != major || dev_minor(st.rdev) != minor ||
            !read_exact_attr(name_path, LIFECYCLE_CHANNEL_NAME "\n") ||
            !parse_dev_attribute(dev_path, &major, &minor) || dev_major(st.rdev) != major ||
            dev_minor(st.rdev) != minor) {
            if (fd >= 0) sc1(SYS_close, fd);
            return -1;
        }
        return (int)fd;
    }
}

static int generate_boot_generation(char out[37]) {
    u8 bytes[16];
    usize used = 0, i, text = 0;
    u64 now = monotonic_millis();
    u64 deadline = now ? now + 5000 : 0;
    while (used < sizeof(bytes)) {
        i64 n = sc3(SYS_getrandom, (i64)(bytes + used), sizeof(bytes) - used, GRND_NONBLOCK);
        if (n > 0 && (usize)n <= sizeof(bytes) - used) { used += (usize)n; continue; }
        if (n == -EINTR) continue;
        if (n == -EAGAIN) {
            struct pollfd_local none;
            if (!deadline || monotonic_millis() >= deadline) return 0;
            none.fd = -1; none.events = 0; none.revents = 0;
            sc3(SYS_poll, (i64)&none, 0, 10);
            continue;
        }
        return 0;
    }
    bytes[6] = (u8)((bytes[6] & 15) | 0x40);
    bytes[8] = (u8)((bytes[8] & 63) | 0x80);
    for (i = 0; i < sizeof(bytes); i++) {
        if (i == 4 || i == 6 || i == 8 || i == 10) out[text++] = '-';
        out[text++] = hex_digit(bytes[i] >> 4);
        out[text++] = hex_digit(bytes[i] & 15);
    }
    out[text] = 0;
    return text == 36;
}

static int send_boundary_ack(struct lifecycle_session *session, u32 header_used,
                             u32 payload_used, u32 payload_expected);

static void lifecycle_connection_lost(struct lifecycle_session *session) {
    int admitted_peer_boundary = session->connection == LIFECYCLE_CONNECTED;
    u32 discarded_header = session->header_used;
    u32 discarded_payload = session->payload_used;
    u32 discarded_expected = session->payload_expected;
    session->connection = LIFECYCLE_DISCONNECTED;
    session->connection_has_hello = 0;
    session->natural_late_stop_allowed = 0;
    session->header_used = 0;
    session->payload_expected = 0;
    session->payload_used = 0;
    session->partial_frame_marker_emitted = 0;
    session->frame_deadline = 0;
    session->outbound_failed = 0;
    session->outbound_deadline = 0;
    if (admitted_peer_boundary && session->key_ack_wire_sequence) {
        session->epoch++;
        if (!send_boundary_ack(session, discarded_header, discarded_payload, discarded_expected))
            session->poisoned = 1;
    }
}

/* Never let a connected peer make the outer supervisor wait past the
 * connection-local partial-frame deadline.  read_control_frame() owns the
 * actual rejection; this helper only guarantees that it is called again. */
static int lifecycle_poll_timeout(const struct lifecycle_session *session, int fallback) {
    u64 now, deadline = session->frame_deadline, remaining;
    if (session->connection == LIFECYCLE_DISCONNECTED)
        return (int)session->reconnect_backoff_ms;
    if (session->outbound_failed && (!deadline || session->outbound_deadline < deadline))
        deadline = session->outbound_deadline;
    if (!deadline) return fallback;
    now = monotonic_millis();
    if (!now || now >= deadline) return 0;
    remaining = deadline - now;
    if (fallback >= 0 && remaining > (u64)fallback) return fallback;
    return (int)remaining;
}

/* Return 1 for one complete frame, 0 for no data, -2 for a peer boundary,
 * and -1 for a complete invalid length or I/O failure. An incomplete frame is
 * connection-local and discarded only after read(2) reports the peer boundary. */
static int read_control_frame(struct lifecycle_session *session, usize *payload_size) {
    for (;;) {
        u8 *target;
        usize needed;
        i64 n;
        if (session->payload_expected) {
            target = control_payload + session->payload_used;
            needed = session->payload_expected - session->payload_used;
        } else {
            target = session->header + session->header_used;
            needed = sizeof(session->header) - session->header_used;
        }
        n = sc3(SYS_read, session->fd, (i64)target, needed);
        if (n > 0 && (usize)n <= needed) {
            if (!session->frame_deadline) {
                u64 now = monotonic_millis();
                if (!now) return -1;
                session->frame_deadline = now + 5000;
            }
            session->connection = LIFECYCLE_CONNECTED;
            session->reconnect_backoff_ms = 10;
            if (session->payload_expected) {
                session->payload_used += (u32)n;
                if (session->payload_used == session->payload_expected) {
                    *payload_size = session->payload_expected;
                    session->header_used = 0;
                    session->payload_expected = 0;
                    session->payload_used = 0;
                    session->partial_frame_marker_emitted = 0;
                    session->frame_deadline = 0;
                    return 1;
                }
            } else {
                session->header_used += (u32)n;
                if (session->header_used == sizeof(session->header)) {
                    u32 size = ((u32)session->header[0] << 24) | ((u32)session->header[1] << 16) |
                               ((u32)session->header[2] << 8) | session->header[3];
                    if (!size || size > CONTROL_PAYLOAD_MAX) return -1;
                    session->payload_expected = size;
                    session->payload_used = 0;
                    session->header_used = 0;
                }
            }
            continue;
        }
        if (n == -EINTR) continue;
        if (n == -EAGAIN) {
            if (session->payload_expected && session->payload_used + 1 == session->payload_expected &&
                !session->partial_frame_marker_emitted) {
                write_all(1, LIFECYCLE_PARTIAL_BUFFERED_MARKER);
                session->partial_frame_marker_emitted = 1;
            }
            return session->frame_deadline && monotonic_millis() >= session->frame_deadline ? -1 : 0;
        }
        if (n == 0) {
            lifecycle_connection_lost(session);
            return -2;
        }
        return -1;
    }
}

static int nonce_seen(const struct lifecycle_session *session, const char nonce[65]) {
    u32 i;
    for (i = 0; i < session->nonce_count; i++)
        if (bytes_equal(session->used_nonces[i], nonce, 64)) return 1;
    return 0;
}

static int request_seen(const struct lifecycle_session *session, u64 request_id) {
    u32 i;
    for (i = 0; i < session->request_count; i++)
        if (session->seen_request_ids[i] == request_id) return 1;
    return 0;
}

static int remember_nonce_request(struct lifecycle_session *session, const char nonce[65], u64 request_id) {
    if (session->nonce_count >= LIFECYCLE_CONNECTION_MAX ||
        session->request_count >= LIFECYCLE_REQUEST_LEDGER_MAX ||
        nonce_seen(session, nonce) || request_seen(session, request_id)) return 0;
    memcpy(session->used_nonces[session->nonce_count++], nonce, 65);
    session->seen_request_ids[session->request_count++] = request_id;
    return 1;
}

static int verify_lifecycle_mac(const struct lifecycle_session *session,
                                const char *body, usize body_size,
                                const struct span key_id, const struct span tag,
                                const char *direction, const char *carrier);

static int parse_hello(struct lifecycle_session *session, usize size) {
    struct span value, nonce, attempt;
    char candidate[65], attempt_text[37];
    u64 request_id, epoch, wire;
    usize i;
    struct parser *j = &control_parser;
    memset(j, 0, sizeof(*j)); j->p = control_payload; j->end = control_payload + size;
    if (!take_char(j, '{') || !key(j, "body") || !take_char(j, '{') ||
        !key(j, "boot_attempt_id") || !plain_string(j, &attempt, 0) || !valid_uuid_span(attempt) ||
        !take_char(j, ',') || !key(j, "domain_core_digest") || !plain_string(j, &value, 0) ||
        value.n != 71 || !bytes_equal(value.p, lifecycle_binding.core, 71) || !take_char(j, ',') ||
        !key(j, "epoch") || !uint_value(j, &epoch) || epoch != 1 || !take_char(j, ',') ||
        !key(j, "host_nonce") || !plain_string(j, &nonce, 0) || nonce.n != 64) return 0;
    for (i = 0; i < nonce.n; i++) if (!is_hex((char)nonce.p[i])) return 0;
    memcpy(candidate, nonce.p, 64); candidate[64] = 0;
    memcpy(attempt_text, attempt.p, 36); attempt_text[36] = 0;
    if (!take_char(j, ',') || !key(j, "kind") || !exact_string(j, "HELLO") || !take_char(j, ',') ||
        !key(j, "payload") || !take_char(j, '{') || !take_char(j, '}') || !take_char(j, ',') ||
        !key(j, "request_id") || !uint_value(j, &request_id) || !request_id ||
        request_id > 0x7fffffffffffffffULL || !take_char(j, ',') ||
        !key(j, "run_id") || !plain_string(j, &value, 0) || value.n != 36 ||
        !bytes_equal(value.p, lifecycle_binding.run_id, 36) || !take_char(j, ',') ||
        !key(j, "schema") || !exact_string(j, LIFECYCLE_PROTOCOL) || !take_char(j, ',') ||
        !key(j, "stage1_artifact_digest") || !plain_string(j, &value, 0) || value.n != 71 ||
        !bytes_equal(value.p, lifecycle_binding.stage1, 71) || !take_char(j, ',') ||
        !key(j, "wire_sequence") || !uint_value(j, &wire) || wire != 1 ||
        !take_char(j, '}') || !take_char(j, ',') || !key(j, "mac") ||
        !exact_literal(j, "null") || !take_char(j, '}') || j->p != j->end ||
        session->connection_has_hello || session->last_hello_request_id ||
        !remember_nonce_request(session, candidate, request_id)) return 0;
    memcpy(session->host_nonce, candidate, 65);
    memcpy(session->boot_attempt_id, attempt_text, 37);
    session->last_hello_request_id = request_id;
    session->hello_request_id = request_id;
    session->connection_opener_request_id = request_id;
    session->last_accepted_host_wire = wire;
    session->epoch = 1;
    session->connection_has_hello = 1;
    session->connection = LIFECYCLE_CONNECTED;
    return 1;
}

static int parse_signed_host(struct lifecycle_session *session, usize size, int expected_kind,
                             u64 *request_id_out, u64 *wire_out) {
    struct span value, key_id, tag, boundary_id, boundary_digest;
    const u8 *body_start, *body_end;
    char nonce_candidate[65];
    u64 epoch, request_id = 0, reply_to, wire, signal_value = 0;
    const char *kind = expected_kind == 0 ? "KEY_ACK" : (expected_kind == 1 ? "RECONNECT" : "STOP");
    struct parser *j = &control_parser;
    memset(j, 0, sizeof(*j)); j->p = control_payload; j->end = control_payload + size;
    if ((expected_kind != 1 && !session->connection_has_hello) ||
        !take_char(j, '{') || !key(j, "body")) return 0;
    body_start = j->p;
    if (!take_char(j, '{') || !key(j, "boot_attempt_id") || !plain_string(j, &value, 0) ||
        value.n != 36 || !bytes_equal(value.p, session->boot_attempt_id, 36) || !take_char(j, ',') ||
        !key(j, "boot_generation") || !plain_string(j, &value, 0) || value.n != 36 ||
        !bytes_equal(value.p, session->boot_generation, 36) || !take_char(j, ',') ||
        !key(j, "domain_core_digest") || !plain_string(j, &value, 0) || value.n != 71 ||
        !bytes_equal(value.p, lifecycle_binding.core, 71) || !take_char(j, ',') ||
        !key(j, "epoch") || !uint_value(j, &epoch) || epoch != session->epoch || !take_char(j, ',') ||
        !key(j, "host_nonce") || !plain_string(j, &value, 0) || value.n != 64) return 0;
    memcpy(nonce_candidate, value.p, 64); nonce_candidate[64] = 0;
    if ((expected_kind != 1 && !bytes_equal(nonce_candidate, session->host_nonce, 64)) ||
        (expected_kind == 1 && nonce_seen(session, nonce_candidate)) || !take_char(j, ',') ||
        !key(j, "kind") || !exact_string(j, kind) || !take_char(j, ',') || !key(j, "payload") ||
        !take_char(j, '{')) return 0;
    if (expected_kind == 1) {
        if (!key(j, "boundary_ack_digest") || !plain_string(j, &boundary_digest, 0) ||
            boundary_digest.n != 71 || !bytes_equal(boundary_digest.p, session->boundary_digest, 71) ||
            !take_char(j, ',') ||
            !key(j, "boundary_id") || !plain_string(j, &boundary_id, 0) ||
            boundary_id.n != 36 || !bytes_equal(boundary_id.p, session->boundary_id, 36)) return 0;
    } else if (expected_kind == 2) {
        if (!key(j, "signal") || !uint_value(j, &signal_value) || signal_value != 15) return 0;
    }
    if (!take_char(j, '}')) return 0;
    if (!take_char(j, ',') || !key(j, "reply_to")) return 0;
    if (expected_kind == 0) {
        if (!uint_value(j, &reply_to) || reply_to != session->bootstrap_wire_sequence) return 0;
    } else if (!exact_literal(j, "null")) return 0;
    if (expected_kind != 0) {
        if (!take_char(j, ',') || !key(j, "request_id") || !uint_value(j, &request_id) ||
            !request_id || request_id > 0x7fffffffffffffffULL) return 0;
    }
    if (!take_char(j, ',') || !key(j, "run_id") || !plain_string(j, &value, 0) || value.n != 36 ||
        !bytes_equal(value.p, lifecycle_binding.run_id, 36) || !take_char(j, ',') ||
        !key(j, "schema") || !exact_string(j, LIFECYCLE_PROTOCOL) || !take_char(j, ',') ||
        !key(j, "stage1_artifact_digest") || !plain_string(j, &value, 0) || value.n != 71 ||
        !bytes_equal(value.p, lifecycle_binding.stage1, 71) || !take_char(j, ',') ||
        !key(j, "wire_sequence") || !uint_value(j, &wire) || !wire || wire > 0x7fffffffffffffffULL ||
        !take_char(j, '}')) return 0;
    body_end = j->p;
    if (!take_char(j, ',') || !key(j, "mac") || !take_char(j, '{') ||
        !key(j, "key_id") || !plain_string(j, &key_id, 0) || !take_char(j, ',') ||
        !key(j, "tag") || !plain_string(j, &tag, 0) || !take_char(j, '}') ||
        !take_char(j, '}') || j->p != j->end ||
        !verify_lifecycle_mac(session, (const char *)body_start, (usize)(body_end - body_start), key_id, tag,
                              "host-to-guest", LIFECYCLE_CHANNEL_CARRIER)) return 0;
    if (wire <= session->last_accepted_host_wire) return 0;
    if (expected_kind == 0) {
        if (epoch != 1 || session->last_accepted_host_wire == 0x7fffffffffffffffULL ||
            wire != session->last_accepted_host_wire + 1) return 0;
    } else if (expected_kind == 1) {
        /* The console BOUNDARY_ACK is the sole authority that opened this epoch.
         * The exact digest/id are validated by the host-side v2 session and the
         * MAC binds them here; a stale nonce/epoch cannot enter this branch. */
        usize i;
        for (i = 0; i < 64; i++) if (!is_hex(nonce_candidate[i])) return 0;
        if (request_seen(session, request_id) ||
            !remember_nonce_request(session, nonce_candidate, request_id)) return 0;
        memcpy(session->host_nonce, nonce_candidate, 65);
        session->connection_opener_request_id = request_id;
        session->connection_has_hello = 1;
        session->connection = LIFECYCLE_CONNECTED;
        session->last_accepted_host_wire = wire;
        session->boundary_id[0] = 0;
        session->boundary_digest[0] = 0;
    }
    if (request_id_out) *request_id_out = request_id;
    if (wire_out) *wire_out = wire;
    return 1;
}

static int parse_stop(struct lifecycle_session *session, usize size) {
    u64 request_id, wire;
    if (!parse_signed_host(session, size, 2, &request_id, &wire)) return 0;
    if (session->stop_request_id) {
        if (request_id != session->stop_request_id || session->state != LIFECYCLE_STOPPING) return 0;
        session->last_accepted_host_wire = wire;
        return 2;
    }
    /* A canonical STOP that lost the race with an already-reaped main process
     * is drained without changing the frozen natural terminal cause.  Invalid
     * or replayed input remains fail-closed. */
    if (session->state == LIFECYCLE_TERMINAL) {
        if (!session->natural_late_stop_allowed || request_seen(session, request_id) ||
            session->request_count >= LIFECYCLE_REQUEST_LEDGER_MAX) return 0;
        session->natural_late_stop_allowed = 0;
        session->seen_request_ids[session->request_count++] = request_id;
        session->last_accepted_host_wire = wire;
        return 3;
    }
    if (session->state != LIFECYCLE_READY || request_seen(session, request_id) ||
        session->request_count >= LIFECYCLE_REQUEST_LEDGER_MAX) return 0;
    session->seen_request_ids[session->request_count++] = request_id;
    session->stop_request_id = request_id;
    session->last_accepted_host_wire = wire;
    session->state = LIFECYCLE_STOPPING;
    session->stop_delivered_count++;
    return 1;
}

static int append_control(u8 *out, usize cap, usize *used, const char *value) {
    usize size = slen(value);
    if (*used + size > cap) return 0;
    memcpy(out + *used, value, size); *used += size; return 1;
}

static int append_control_u64(u8 *out, usize cap, usize *used, u64 value) {
    char digits[20]; usize count = 0;
    do { digits[count++] = (char)('0' + value % 10); value /= 10; } while (value && count < sizeof(digits));
    if (*used + count > cap) return 0;
    while (count) out[(*used)++] = (u8)digits[--count];
    return 1;
}

/* V2 closed-world maxima: terminal BOUNDARY_ACK body <=1114 and console
 * envelope <=1336 bytes; the compile-time slack is deliberate schema guard. */
_Static_assert(CONTROL_GUEST_BODY_MAX >= 1114, "lifecycle body bound is too small");
_Static_assert(CONTROL_GUEST_OUTPUT_MAX >= 1336, "lifecycle envelope bound is too small");
static u8 control_body[CONTROL_GUEST_BODY_MAX];

static int lifecycle_mac(const struct lifecycle_session *session, const char *body, usize body_size,
                         const char *direction, const char *carrier, u8 tag[32]) {
    static const char salt_input[] = LIFECYCLE_PROTOCOL "\0hkdf-salt\0";
    u8 salt[32], prk[32], subkey[32], length[4];
    u8 info[1024], input_prefix[256];
    usize used = 0, prefix_used = 0;
#define INFO_TEXT(value) do { if (!append_control(info, sizeof(info), &used, value)) return 0; } while (0)
#define INFO_BYTES(value) do { usize n_ = sizeof(value) - 1; if (used + n_ > sizeof(info)) return 0; memcpy(info + used, value, n_); used += n_; } while (0)
    sha_bytes(salt_input, sizeof(salt_input) - 1, salt);
    hmac_sha256(salt, session->boot_key, sizeof(session->boot_key), prk);
    INFO_BYTES(LIFECYCLE_PROTOCOL "\0subkey\0"); INFO_TEXT(direction); INFO_BYTES("\0");
    INFO_TEXT(carrier); INFO_BYTES("\0"); INFO_TEXT("{\"boot_attempt_id\":\""); INFO_TEXT(session->boot_attempt_id);
    INFO_TEXT("\",\"boot_generation\":\""); INFO_TEXT(session->boot_generation);
    INFO_TEXT("\",\"domain_core_digest\":\""); INFO_TEXT(lifecycle_binding.core);
    INFO_TEXT("\",\"run_id\":\""); INFO_TEXT(lifecycle_binding.run_id);
    INFO_TEXT("\",\"stage1_artifact_digest\":\""); INFO_TEXT(lifecycle_binding.stage1);
    INFO_TEXT("\"}");
    if (used == sizeof(info)) return 0;
    info[used++] = 1;
    hmac_sha256(prk, info, used, subkey);
#undef INFO_TEXT
#undef INFO_BYTES
#define PREFIX_TEXT(value) do { if (!append_control(input_prefix, sizeof(input_prefix), &prefix_used, value)) return 0; } while (0)
#define PREFIX_BYTES(value) do { usize n_ = sizeof(value) - 1; if (prefix_used + n_ > sizeof(input_prefix)) return 0; memcpy(input_prefix + prefix_used, value, n_); prefix_used += n_; } while (0)
    PREFIX_BYTES(LIFECYCLE_PROTOCOL "\0frame\0"); PREFIX_TEXT(direction); PREFIX_BYTES("\0");
    PREFIX_TEXT(carrier); PREFIX_BYTES("\0");
#undef PREFIX_TEXT
#undef PREFIX_BYTES
    length[0] = (u8)(body_size >> 24); length[1] = (u8)(body_size >> 16);
    length[2] = (u8)(body_size >> 8); length[3] = (u8)body_size;
    hmac_sha256_parts(subkey, input_prefix, prefix_used, length, 4, body, body_size, tag);
    secure_zero(salt, sizeof(salt)); secure_zero(prk, sizeof(prk)); secure_zero(subkey, sizeof(subkey));
    secure_zero(info, sizeof(info)); secure_zero(input_prefix, sizeof(input_prefix));
    return 1;
}

static int lifecycle_crypto_kat(void) {
    static const char body[] =
        "{\"boot_attempt_id\":\"aca88126-d991-4de8-b66b-90dc07904dff\","
        "\"boot_generation\":\"b22b1c81-dfa4-478a-b352-27b5b35fe5b7\","
        "\"domain_core_digest\":\"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
        "\"epoch\":1,\"host_nonce\":\"1111111111111111111111111111111111111111111111111111111111111111\","
        "\"kind\":\"READY\",\"payload\":{},\"reply_to\":2,"
        "\"run_id\":\"f6f546e2-e734-4920-9eff-1762b348a249\","
        "\"schema\":\"palimpsest.oci-lifecycle-control.v2\","
        "\"stage1_artifact_digest\":\"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\","
        "\"wire_sequence\":2}";
    static const char expected_tag[] = "dcc930fbefed70f26cb33075a2c5d28d8c9d714357cd6267e8c8ed4209d72227";
    struct lifecycle_binding saved = lifecycle_binding;
    struct lifecycle_session session;
    u8 tag[32], expected[32]; usize i; int valid;
    memset(&session, 0, sizeof(session));
    memcpy(session.boot_attempt_id, "aca88126-d991-4de8-b66b-90dc07904dff", 37);
    memcpy(session.boot_generation, "b22b1c81-dfa4-478a-b352-27b5b35fe5b7", 37);
    for (i = 0; i < 32; i++) session.boot_key[i] = (u8)i;
    memcpy(lifecycle_binding.run_id, "f6f546e2-e734-4920-9eff-1762b348a249", 37);
    memcpy(lifecycle_binding.core,
           "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 72);
    memcpy(lifecycle_binding.stage1,
           "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 72);
    for (i = 0; i < 32; i++) expected[i] = (u8)((hex_value(expected_tag[i * 2]) << 4) |
                                                hex_value(expected_tag[i * 2 + 1]));
    valid = lifecycle_mac(&session, body, sizeof(body) - 1, "guest-to-host",
                          LIFECYCLE_CHANNEL_CARRIER, tag) && bytes_equal(tag, expected, 32);
    lifecycle_binding = saved;
    secure_zero(&session, sizeof(session)); secure_zero(tag, sizeof(tag)); secure_zero(expected, sizeof(expected));
    return valid;
}

static int verify_lifecycle_mac(const struct lifecycle_session *session,
                                const char *body, usize body_size,
                                const struct span key_id, const struct span tag,
                                const char *direction, const char *carrier) {
    u8 expected[32], supplied[32];
    usize i;
    int key_ok, tag_ok;
    if (key_id.n != 71 || tag.n != 64) return 0;
    for (i = 0; i < 64; i++) if (!is_hex(tag.p[i])) return 0;
    for (i = 0; i < 32; i++) supplied[i] = (u8)((hex_value(tag.p[i * 2]) << 4) | hex_value(tag.p[i * 2 + 1]));
    if (!lifecycle_mac(session, body, body_size, direction, carrier, expected)) return 0;
    key_ok = bytes_equal(key_id.p, session->key_id, 71);
    tag_ok = bytes_equal(expected, supplied, 32);
    secure_zero(expected, sizeof(expected)); secure_zero(supplied, sizeof(supplied));
    return key_ok && tag_ok;
}

static int write_signed_message(struct lifecycle_session *session, const u8 *body, usize body_size,
                                const char *direction, const char *carrier, int console) {
    u8 tag[32]; char tag_text[65]; usize used = console ? 0 : 4;
    if (!lifecycle_mac(session, (const char *)body, body_size, direction, carrier, tag)) return 0;
    hex32(tag, tag_text);
    if (console && !append_control(control_output, sizeof(control_output), &used, LIFECYCLE_BOUNDARY_PREFIX)) return 0;
    if (!append_control(control_output, sizeof(control_output), &used, "{\"body\":") ||
        used + body_size > sizeof(control_output)) return 0;
    memcpy(control_output + used, body, body_size); used += body_size;
    if (!append_control(control_output, sizeof(control_output), &used, ",\"mac\":{\"key_id\":\"") ||
        !append_control(control_output, sizeof(control_output), &used, session->key_id) ||
        !append_control(control_output, sizeof(control_output), &used, "\",\"tag\":\"") ||
        !append_control(control_output, sizeof(control_output), &used, tag_text) ||
        !append_control(control_output, sizeof(control_output), &used, "\"}}")) return 0;
    secure_zero(tag, sizeof(tag)); secure_zero(tag_text, sizeof(tag_text));
    if (console) {
        i64 n;
        usize prefix_size = slen(LIFECYCLE_BOUNDARY_PREFIX);
        digest_text(control_output + prefix_size, used - prefix_size, session->boundary_digest);
        if (!append_control(control_output, sizeof(control_output), &used, "\n")) return 0;
        n = sc3(SYS_write, 1, (i64)control_output, used);
        secure_zero(control_output, used);
        return n == (i64)used;
    }
    {
        u32 payload = (u32)(used - 4);
        control_output[0] = (u8)(payload >> 24); control_output[1] = (u8)(payload >> 16);
        control_output[2] = (u8)(payload >> 8); control_output[3] = (u8)payload;
    }
    if (!session->connection_has_hello || !control_io_exact(session->fd, control_output, used, 1)) {
        u64 now = monotonic_millis();
        session->outbound_failed = 1; session->outbound_deadline = now ? now + 5000 : 0;
        secure_zero(control_output, used);
        return 0;
    }
    secure_zero(control_output, used);
    session->outbound_failed = 0; session->outbound_deadline = 0;
    return 1;
}

static int generate_boot_secret(struct lifecycle_session *session) {
    static const char key_prefix[] = LIFECYCLE_PROTOCOL "\0key-id\0";
    u8 digest[32]; struct sha256_ctx c; usize used = 0, i;
    u64 now = monotonic_millis(), deadline = now ? now + 5000 : 0;
    while (used < sizeof(session->boot_key)) {
        i64 n = sc3(SYS_getrandom, (i64)(session->boot_key + used), sizeof(session->boot_key) - used, GRND_NONBLOCK);
        if (n > 0 && (usize)n <= sizeof(session->boot_key) - used) { used += (usize)n; continue; }
        if (n == -EINTR) continue;
        if (n == -EAGAIN && deadline && monotonic_millis() < deadline) {
            struct pollfd_local none; none.fd = -1; none.events = 0; none.revents = 0;
            sc3(SYS_poll, (i64)&none, 0, 10); continue;
        }
        secure_zero(session->boot_key, sizeof(session->boot_key)); return 0;
    }
    sha_init(&c); sha_update(&c, key_prefix, sizeof(key_prefix) - 1);
    sha_update(&c, session->boot_key, sizeof(session->boot_key)); sha_final(&c, digest);
    memcpy(session->key_id, "sha256:", 7);
    for (i = 0; i < 32; i++) { session->key_id[7 + i * 2] = hex_digit(digest[i] >> 4); session->key_id[8 + i * 2] = hex_digit(digest[i] & 15); }
    session->key_id[71] = 0;
    secure_zero(digest, sizeof(digest)); secure_zero(&c, sizeof(c));
    if (!generate_boot_generation(session->boot_generation)) {
        secure_zero(session->boot_key, sizeof(session->boot_key));
        secure_zero(session->key_id, sizeof(session->key_id));
        return 0;
    }
    return 1;
}

static int send_bootstrap(struct lifecycle_session *session) {
    usize used = 0, i; u64 sequence = session->next_sequence++;
    session->bootstrap_wire_sequence = sequence;
#define BODY_TEXT(value) do { if (!append_control(control_body, sizeof(control_body), &used, value)) return 0; } while (0)
    BODY_TEXT("{\"boot_attempt_id\":\""); BODY_TEXT(session->boot_attempt_id);
    BODY_TEXT("\",\"boot_generation\":\""); BODY_TEXT(session->boot_generation);
    BODY_TEXT("\",\"domain_core_digest\":\""); BODY_TEXT(lifecycle_binding.core);
    BODY_TEXT("\",\"epoch\":1,\"host_nonce\":\""); BODY_TEXT(session->host_nonce);
    BODY_TEXT("\",\"kind\":\"BOOTSTRAP\",\"payload\":{\"boot_key\":\"");
    if (used + 64 > sizeof(control_body)) return 0;
    for (i = 0; i < 32; i++) { control_body[used++] = (u8)hex_digit(session->boot_key[i] >> 4); control_body[used++] = (u8)hex_digit(session->boot_key[i] & 15); }
    BODY_TEXT("\"},\"reply_to\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, session->hello_request_id)) return 0;
    BODY_TEXT(",\"run_id\":\""); BODY_TEXT(lifecycle_binding.run_id); BODY_TEXT("\",\"schema\":\"");
    BODY_TEXT(LIFECYCLE_PROTOCOL); BODY_TEXT("\",\"stage1_artifact_digest\":\""); BODY_TEXT(lifecycle_binding.stage1);
    BODY_TEXT("\",\"wire_sequence\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, sequence)) return 0;
    BODY_TEXT("}");
#undef BODY_TEXT
    i = (usize)write_signed_message(session, control_body, used, "guest-to-host", LIFECYCLE_CHANNEL_CARRIER, 0);
    secure_zero(control_body, used); return (int)i;
}

static int send_control_message(struct lifecycle_session *session, int kind,
                                const struct supervisor_result *result) {
    usize used = 0;
    u64 sequence = session->next_sequence++;
#define CONTROL_TEXT(value) do { if (!append_control(control_body, sizeof(control_body), &used, value)) return 0; } while (0)
    CONTROL_TEXT("{\"boot_attempt_id\":\""); CONTROL_TEXT(session->boot_attempt_id);
    CONTROL_TEXT("\",\"boot_generation\":\""); CONTROL_TEXT(session->boot_generation);
    CONTROL_TEXT("\",\"domain_core_digest\":\""); CONTROL_TEXT(lifecycle_binding.core);
    CONTROL_TEXT("\",\"epoch\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, session->epoch)) return 0;
    CONTROL_TEXT(",\"host_nonce\":\""); CONTROL_TEXT(session->host_nonce);
    if (kind == 0) CONTROL_TEXT("\",\"kind\":\"READY\",\"payload\":{}");
    else if (kind == 1) CONTROL_TEXT("\",\"kind\":\"SNAPSHOT\",\"payload\":{\"state\":\"ready\",\"stop_request_id\":null,\"terminal\":null}");
    else if (kind == 2) {
        CONTROL_TEXT("\",\"kind\":\"SNAPSHOT\",\"payload\":{\"state\":\"stopping\",\"stop_request_id\":");
        if (!append_control_u64(control_body, sizeof(control_body), &used, session->stop_request_id)) return 0;
        CONTROL_TEXT(",\"terminal\":null}");
    } else if (kind == 3) CONTROL_TEXT("\",\"kind\":\"TERMINAL\",\"payload\":{\"terminal\":{");
    else CONTROL_TEXT("\",\"kind\":\"SNAPSHOT\",\"payload\":{\"state\":\"terminal\",\"stop_request_id\":");
    if (kind == 4) {
        if (session->stop_request_id) {
            if (!append_control_u64(control_body, sizeof(control_body), &used, session->stop_request_id)) return 0;
        } else CONTROL_TEXT("null");
        CONTROL_TEXT(",\"terminal\":{");
    }
    if (kind == 3 || kind == 4) {
        if (result->main_signal) { CONTROL_TEXT("\"exit_code\":null,\"signal\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, result->main_signal)) return 0; }
        else { CONTROL_TEXT("\"exit_code\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, result->main_exit_code)) return 0; CONTROL_TEXT(",\"signal\":null"); }
        CONTROL_TEXT(kind == 3 ? "}}" : "}}");
    }
    CONTROL_TEXT(",\"reply_to\":");
    if (kind == 3 && !session->stop_request_id) CONTROL_TEXT("null");
    else if (!append_control_u64(control_body, sizeof(control_body), &used,
                                 kind == 3 ? session->stop_request_id :
                                 (kind == 0 ? session->key_ack_wire_sequence : session->connection_opener_request_id))) return 0;
    CONTROL_TEXT(",\"run_id\":\""); CONTROL_TEXT(lifecycle_binding.run_id);
    CONTROL_TEXT("\",\"schema\":\""); CONTROL_TEXT(LIFECYCLE_PROTOCOL);
    CONTROL_TEXT("\",\"stage1_artifact_digest\":\""); CONTROL_TEXT(lifecycle_binding.stage1);
    CONTROL_TEXT("\",\"wire_sequence\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, sequence)) return 0;
    CONTROL_TEXT("}");
#undef CONTROL_TEXT
    kind = write_signed_message(session, control_body, used, "guest-to-host", LIFECYCLE_CHANNEL_CARRIER, 0);
    secure_zero(control_body, used); return kind;
}

static int append_lifecycle_state(u8 *out, usize cap, usize *used,
                                  const struct lifecycle_session *session, int with_schema) {
    if (!append_control(out, cap, used, "{")) return 0;
    if (with_schema && (!append_control(out, cap, used, "\"schema\":\"") ||
        !append_control(out, cap, used, LIFECYCLE_PUBLIC_STATE) ||
        !append_control(out, cap, used, "\","))) return 0;
    if (!append_control(out, cap, used, "\"state\":\"")) return 0;
    if (session->state == LIFECYCLE_READY) {
        if (!append_control(out, cap, used, "ready\",\"stop_request_id\":null,\"terminal\":null}")) return 0;
    } else if (session->state == LIFECYCLE_STOPPING) {
        if (!append_control(out, cap, used, "stopping\",\"stop_request_id\":")) return 0;
        if (!append_control_u64(out, cap, used, session->stop_request_id) ||
            !append_control(out, cap, used, ",\"terminal\":null}")) return 0;
    } else if (session->state == LIFECYCLE_TERMINAL) {
        if (!append_control(out, cap, used, "terminal\",\"stop_request_id\":")) return 0;
        if (session->stop_request_id) {
            if (!append_control_u64(out, cap, used, session->stop_request_id)) return 0;
        } else if (!append_control(out, cap, used, "null")) return 0;
        if (!append_control(out, cap, used, ",\"terminal\":{")) return 0;
        if (session->terminal_signal) {
            if (!append_control(out, cap, used, "\"exit_code\":null,\"signal\":") ||
                !append_control_u64(out, cap, used, session->terminal_signal)) return 0;
        } else if (!append_control(out, cap, used, "\"exit_code\":") ||
                   !append_control_u64(out, cap, used, session->terminal_exit_code) ||
                   !append_control(out, cap, used, ",\"signal\":null")) return 0;
        if (!append_control(out, cap, used, "}}")) return 0;
    } else return 0;
    return 1;
}

static int send_boundary_ack(struct lifecycle_session *session, u32 header_used,
                             u32 payload_used, u32 payload_expected) {
    u8 state[256]; char state_digest[72]; usize state_used = 0, used = 0;
    u64 sequence = session->next_sequence++;
    if (!((header_used == 0 && payload_used == 0 && payload_expected == 0) ||
          (header_used >= 1 && header_used <= 3 && payload_used == 0 && payload_expected == 0) ||
          (header_used == 0 && payload_expected >= 1 && payload_expected <= CONTROL_PAYLOAD_MAX &&
           payload_used < payload_expected))) return 0;
    if (session->boundary_count >= LIFECYCLE_CONNECTION_MAX) return 0;
    for (;;) {
        u32 i; int duplicate = 0;
        if (!generate_boot_generation(session->boundary_id)) return 0;
        for (i = 0; i < session->boundary_count; i++)
            if (bytes_equal(session->used_boundary_ids[i], session->boundary_id, 36)) duplicate = 1;
        if (!duplicate) break;
    }
    memcpy(session->used_boundary_ids[session->boundary_count++], session->boundary_id, 37);
    if (!append_lifecycle_state(state, sizeof(state), &state_used, session, 1)) return 0;
    digest_text(state, state_used, state_digest);
#define BOUNDARY_TEXT(value) do { if (!append_control(control_body, sizeof(control_body), &used, value)) return 0; } while (0)
    BOUNDARY_TEXT("{\"boot_attempt_id\":\""); BOUNDARY_TEXT(session->boot_attempt_id);
    BOUNDARY_TEXT("\",\"boot_generation\":\""); BOUNDARY_TEXT(session->boot_generation);
    BOUNDARY_TEXT("\",\"domain_core_digest\":\""); BOUNDARY_TEXT(lifecycle_binding.core);
    BOUNDARY_TEXT("\",\"epoch\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, session->epoch)) return 0;
    BOUNDARY_TEXT(",\"host_nonce\":\""); BOUNDARY_TEXT(session->host_nonce);
    BOUNDARY_TEXT("\",\"kind\":\"BOUNDARY_ACK\",\"payload\":{\"boundary_id\":\"");
    BOUNDARY_TEXT(session->boundary_id); BOUNDARY_TEXT("\",\"discarded_header_bytes\":");
    if (!append_control_u64(control_body, sizeof(control_body), &used, header_used)) return 0;
    BOUNDARY_TEXT(",\"discarded_payload_bytes\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, payload_used)) return 0;
    BOUNDARY_TEXT(",\"discarded_payload_expected\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, payload_expected)) return 0;
    BOUNDARY_TEXT(",\"last_accepted_h2g_wire_sequence\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, session->last_accepted_host_wire)) return 0;
    BOUNDARY_TEXT(",\"last_attempted_g2h_wire_sequence\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, sequence)) return 0;
    BOUNDARY_TEXT(",\"lifecycle_state\":"); if (!append_lifecycle_state(control_body, sizeof(control_body), &used, session, 0)) return 0;
    BOUNDARY_TEXT(",\"previous_epoch\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, session->epoch - 1)) return 0;
    BOUNDARY_TEXT(",\"state_digest\":\""); BOUNDARY_TEXT(state_digest); BOUNDARY_TEXT("\"},\"reply_to\":");
    if (!append_control_u64(control_body, sizeof(control_body), &used, session->connection_opener_request_id)) return 0;
    BOUNDARY_TEXT(",\"run_id\":\""); BOUNDARY_TEXT(lifecycle_binding.run_id); BOUNDARY_TEXT("\",\"schema\":\"");
    BOUNDARY_TEXT(LIFECYCLE_PROTOCOL); BOUNDARY_TEXT("\",\"stage1_artifact_digest\":\""); BOUNDARY_TEXT(lifecycle_binding.stage1);
    BOUNDARY_TEXT("\",\"wire_sequence\":"); if (!append_control_u64(control_body, sizeof(control_body), &used, sequence)) return 0;
    BOUNDARY_TEXT("}");
#undef BOUNDARY_TEXT
    header_used = (u32)write_signed_message(session, control_body, used, "guest-to-host", LIFECYCLE_CONSOLE_CARRIER, 1);
    secure_zero(state, sizeof(state)); secure_zero(state_digest, sizeof(state_digest));
    secure_zero(control_body, used); return (int)header_used;
}

static int lifecycle_current_snapshot(struct lifecycle_session *session,
                                      const struct supervisor_result *result) {
    if (session->state == LIFECYCLE_READY) return send_control_message(session, 1, result) || !session->poisoned;
    if (session->state == LIFECYCLE_STOPPING) return send_control_message(session, 2, result) || !session->poisoned;
    if (session->state == LIFECYCLE_TERMINAL) return send_control_message(session, 4, result) || !session->poisoned;
    return 1;
}

/* Pump every currently available complete frame.  Outbound loss commits the
 * state/sequence attempt but is not an input boundary: only read(2)==0 may
 * discard the current parser, HELLO, and nonce context. */
static int lifecycle_pump(struct lifecycle_session *session,
                          const struct supervisor_result *result, int *dispatch_stop) {
    for (;;) {
        usize size = 0;
        int frame = read_control_frame(session, &size);
        if (frame == -2) return 1;
        if (frame == 0) {
            if (session->outbound_failed && (!session->outbound_deadline ||
                monotonic_millis() >= session->outbound_deadline)) {
                session->poisoned = 1;
                return 0;
            }
            return 1;
        }
        if (frame < 0) { session->poisoned = 1; return 0; }
        if (session->outbound_failed) { session->poisoned = 1; return 0; }
        if (!session->connection_has_hello) {
            if (session->key_ack_wire_sequence) {
                u64 request_id, wire;
                if (!parse_signed_host(session, size, 1, &request_id, &wire)) {
                    session->poisoned = 1; return 0;
                }
            } else if (!parse_hello(session, size)) { session->poisoned = 1; return 0; }
            if (!lifecycle_current_snapshot(session, result)) return 0;
        } else {
            int stop = parse_stop(session, size);
            if (!stop) { session->poisoned = 1; return 0; }
            if (stop == 1) *dispatch_stop = 1;
            else if (stop == 2) write_all(1, LIFECYCLE_STOP_DUPLICATE_MARKER);
        }
    }
}

static int prepare_lifecycle(struct lifecycle_session *session) {
    usize size = 0;
    u64 deadline;
    memset(session, 0, sizeof(*session)); session->fd = -1;
    if (!lifecycle_crypto_kat()) { session->poisoned = 1; return 0; }
    session->fd = discover_lifecycle_channel();
    session->next_sequence = 1;
    session->reconnect_backoff_ms = 10;
    deadline = monotonic_millis() + 5000;
    if (session->fd < 0) {
        session->poisoned = 1;
        if (session->fd >= 0) sc1(SYS_close, session->fd);
        session->fd = -1;
        return 0;
    }
    while (monotonic_millis() < deadline) {
        int frame = read_control_frame(session, &size);
        if (frame == 1) {
            if (parse_hello(session, size)) return 1;
            break;
        }
        if (frame == -1) break;
        {
            struct pollfd_local none;
            int delay = (int)session->reconnect_backoff_ms;
            none.fd = -1; none.events = 0; none.revents = 0;
            sc3(SYS_poll, (i64)&none, 0, delay);
            if (session->reconnect_backoff_ms < 100) {
                session->reconnect_backoff_ms *= 2;
                if (session->reconnect_backoff_ms > 100) session->reconnect_backoff_ms = 100;
            }
        }
    }
    session->poisoned = 1;
    sc1(SYS_close, session->fd);
    session->fd = -1;
    return 0;
}

static int authenticate_lifecycle_bootstrap(struct lifecycle_session *session) {
    usize size = 0; u64 deadline;
    if (!generate_boot_secret(session) || !send_bootstrap(session)) goto rejected;
    deadline = monotonic_millis() + 5000;
    while (monotonic_millis() < deadline) {
        int frame = read_control_frame(session, &size);
        if (frame == 1) {
            u64 request_id = 0, wire = 0;
            if (!parse_signed_host(session, size, 0, &request_id, &wire)) break;
            session->last_accepted_host_wire = wire;
            session->key_ack_wire_sequence = wire;
            return 1;
        }
        if (frame < 0) break;
        {
            struct pollfd_local item; item.fd = session->fd; item.events = POLLIN; item.revents = 0;
            sc3(SYS_poll, (i64)&item, 1, 10);
        }
    }
rejected:
    secure_zero(session->boot_key, sizeof(session->boot_key));
    secure_zero(session->key_id, sizeof(session->key_id));
    secure_zero(session->boot_generation, sizeof(session->boot_generation));
    return 0;
}

static void wipe_lifecycle_secret(struct lifecycle_session *session) {
    secure_zero(session->boot_key, sizeof(session->boot_key));
    secure_zero(session->key_id, sizeof(session->key_id));
    secure_zero(session->boot_generation, sizeof(session->boot_generation));
}

static __attribute__((noreturn)) void service_terminal_lifecycle(
    struct lifecycle_session *session, const struct supervisor_result *result) {
    for (;;) {
        struct pollfd_local pollfd;
        int dispatch_stop = 0;
        int was_disconnected = session->connection == LIFECYCLE_DISCONNECTED;
        pollfd.fd = session->fd; pollfd.events = POLLIN; pollfd.revents = 0;
        if (was_disconnected) {
            struct pollfd_local none;
            none.fd = -1; none.events = 0; none.revents = 0;
            sc3(SYS_poll, (i64)&none, 0, (int)session->reconnect_backoff_ms);
        } else {
            i64 n = sc3(SYS_poll, (i64)&pollfd, 1,
                        lifecycle_poll_timeout(session, -1));
            if (n < 0 && n != -EINTR) {
                wipe_lifecycle_secret(session);
                lifecycle_rejected(21, EIO);
                for (;;) sc0(SYS_pause);
            }
        }
        if (!lifecycle_pump(session, result, &dispatch_stop) || dispatch_stop) {
            wipe_lifecycle_secret(session);
            lifecycle_rejected(21, EIO);
            for (;;) sc0(SYS_pause);
        }
        if (was_disconnected && session->connection == LIFECYCLE_DISCONNECTED &&
            session->reconnect_backoff_ms < 100) {
            session->reconnect_backoff_ms *= 2;
            if (session->reconnect_backoff_ms > 100) session->reconnect_backoff_ms = 100;
        }
    }
}

static int safe_dir(const char *path, int create, int require_empty, int expected_mode, int *kept_fd) {
    struct stat_local st;
    i64 fd, r;
    if (create) {
        r = sc2(SYS_mkdir, (i64)path, expected_mode ? expected_mode : 0755);
        if (r != 0 && r != -EEXIST) return 0;
    }
    fd = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&st) < 0 || (st.mode & S_IFMT) != S_IFDIR ||
        (expected_mode && (st.uid != 0 || st.gid != 0 || (st.mode & 07777) != (u32)expected_mode))) {
        if (fd >= 0) sc1(SYS_close, fd);
        return 0;
    }
    if (require_empty) {
        u8 entries[1024];
        for (;;) {
            i64 n = sc3(SYS_getdents64, fd, (i64)entries, sizeof(entries));
            usize offset = 0;
            if (n < 0) { sc1(SYS_close, fd); return 0; }
            if (!n) break;
            while (offset < (usize)n) {
                const u8 *entry = entries + offset;
                usize name_bytes, i;
                u32 reclen;
                if ((usize)n - offset < 20) { sc1(SYS_close, fd); return 0; }
                reclen = (u32)entry[16] | ((u32)entry[17] << 8);
                if (reclen < 20 || reclen > (usize)n - offset) { sc1(SYS_close, fd); return 0; }
                name_bytes = reclen - 19;
                for (i = 0; i < name_bytes && entry[19 + i]; i++) {}
                if (i == name_bytes || !((i == 1 && entry[19] == '.') ||
                    (i == 2 && entry[19] == '.' && entry[20] == '.'))) {
                    sc1(SYS_close, fd); return 0;
                }
                offset += reclen;
            }
        }
    }
    *kept_fd = (int)fd;
    return 1;
}

static int stable_dir(const char *path, int fd, i64 magic) {
    struct stat_local before, after;
    struct statfs_local fs;
    i64 check;
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&before) < 0 || (before.mode & S_IFMT) != S_IFDIR ||
        sc2(SYS_fstatfs, fd, (i64)&fs) < 0 || fs.type != magic) return 0;
    check = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (check < 0 || sc2(SYS_fstat, check, (i64)&after) < 0 ||
        before.dev != after.dev || before.ino != after.ino || before.mode != after.mode) {
        if (check >= 0) sc1(SYS_close, check);
        return 0;
    }
    sc1(SYS_close, check);
    return 1;
}

static int proc_fd_path(char out[PATH_MAX_LOCAL], int fd) {
    usize used = 0;
    out[0] = 0;
    return fd >= 0 && append_text(out, &used, "/proc/self/fd/") && append_u32(out, &used, (u32)fd);
}

static int proc_fd_binds_role(const char *path, const struct opened_role *role) {
    struct stat_local opened, original;
    i64 fd = open_read(path, 0);
    int valid;
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&opened) < 0 ||
        sc2(SYS_fstat, role->fd, (i64)&original) < 0) {
        if (fd >= 0) sc1(SYS_close, fd);
        return 0;
    }
    valid = (opened.mode & S_IFMT) == S_IFBLK && opened.dev == original.dev &&
            opened.ino == original.ino && opened.rdev == original.rdev;
    sc1(SYS_close, fd);
    return valid;
}

static int mount_open_role(const struct opened_role *role, const char *target, const char *type,
                           u64 flags, i64 magic, int *mount_fd) {
    char source[PATH_MAX_LOCAL];
    int covered = -1;
    if (!safe_dir(target, 1, 1, 0, &covered) || !proc_fd_path(source, role->fd) ||
        !proc_fd_binds_role(source, role) ||
        !recheck_open_role(role)) {
        if (covered >= 0) sc1(SYS_close, covered);
        return 0;
    }
    if (sc5(SYS_mount, (i64)source, (i64)target, (i64)type, flags, 0) != 0) {
        sc1(SYS_close, covered);
        return 0;
    }
    sc1(SYS_close, covered);
    if (!recheck_open_role(role) || !proc_fd_binds_role(source, role) ||
        !safe_dir(target, 0, 0, 0, mount_fd) ||
        !stable_dir(target, *mount_fd, magic)) {
        if (*mount_fd >= 0) sc1(SYS_close, *mount_fd);
        *mount_fd = -1;
        return 0;
    }
    return 1;
}

static int span_equal(struct span s, const char *text) {
    usize n = slen(text);
    return s.n == n && bytes_equal(s.p, text, n);
}

static int comma_option(struct span options, const char *wanted) {
    usize start = 0, n = slen(wanted);
    while (start < options.n) {
        usize end = start;
        while (end < options.n && options.p[end] != ',') end++;
        if (end - start == n && bytes_equal(options.p + start, wanted, n)) return 1;
        start = end + 1;
    }
    return 0;
}

static int verify_mountinfo(const char *mountpoint, const char *filesystem, int read_only,
                            int bind_device, u32 major, u32 minor, const char *overlay_data) {
    i64 bytes = read_bounded_file("/proc/self/mountinfo", mountinfo, MOUNTINFO_MAX, 1, 0);
    usize pos = 0;
    u32 matches = 0;
    if (bytes <= 0) return 0;
    while (pos < (usize)bytes) {
        struct span fields[64];
        usize end = pos, count = 0, cursor, dash = 64;
        while (end < (usize)bytes && mountinfo[end] != '\n') end++;
        if (end == (usize)bytes) return 0;
        cursor = pos;
        while (cursor < end) {
            usize start;
            while (cursor < end && mountinfo[cursor] == ' ') cursor++;
            if (cursor == end) break;
            start = cursor;
            while (cursor < end && mountinfo[cursor] != ' ') cursor++;
            if (count >= 64) return 0;
            fields[count++] = (struct span){(const char *)mountinfo + start, cursor - start};
        }
        if (count < 10) return 0;
        if (span_equal(fields[4], mountpoint)) {
            usize i, colon = 0;
            u32 found_major, found_minor;
            matches++;
            while (colon < fields[2].n && fields[2].p[colon] != ':') colon++;
            if (colon == fields[2].n || !parse_u32_decimal((const u8 *)fields[2].p, colon, &found_major) ||
                !parse_u32_decimal((const u8 *)fields[2].p + colon + 1, fields[2].n - colon - 1, &found_minor) ||
                (bind_device && (found_major != major || found_minor != minor)) ||
                !comma_option(fields[5], read_only ? "ro" : "rw") ||
                comma_option(fields[5], read_only ? "rw" : "ro") ||
                !comma_option(fields[5], "nodev") || !comma_option(fields[5], "nosuid")) return 0;
            for (i = 6; i < count; i++) if (span_equal(fields[i], "-")) { dash = i; break; }
            if (dash == 64 || dash + 3 >= count || !span_equal(fields[dash + 1], filesystem)) return 0;
            if (overlay_data) {
                struct span super = fields[dash + 3];
                usize wanted = slen(overlay_data), at;
                int found = 0;
                for (at = 0; at + wanted <= super.n; at++)
                    if ((at == 0 || super.p[at - 1] == ',') && bytes_equal(super.p + at, overlay_data, wanted) &&
                        (at + wanted == super.n || super.p[at + wanted] == ',')) { found = 1; break; }
                if (!found) return 0;
            }
        }
        pos = end + 1;
    }
    return matches == 1;
}

static int lower_path(char out[PATH_MAX_LOCAL], u32 ordinal) {
    usize used = 0;
    out[0] = 0;
    return ordinal < LOWER_MAX && append_text(out, &used, "/run/palimpsest/lowers/") &&
           append_u32(out, &used, ordinal);
}

static int build_overlay_data(char out[PATH_MAX_LOCAL], u32 lower_count) {
    usize used = 0;
    u32 i;
    out[0] = 0;
    if (!lower_count || !append_text(out, &used, "lowerdir=")) return 0;
    for (i = lower_count; i > 0; i--) {
        char path[PATH_MAX_LOCAL];
        if (i != lower_count && !append_text(out, &used, ":")) return 0;
        if (!lower_path(path, i - 1) || !append_text(out, &used, path)) return 0;
    }
    return append_text(out, &used, ",upperdir=/run/palimpsest/root/.palimpsest/upper") &&
           append_text(out, &used, ",workdir=/run/palimpsest/root/.palimpsest/work");
}

static int verify_probe_at(const struct expected_device_set *expected, u32 index, const char *base) {
    char path[PATH_MAX_LOCAL];
    usize used = 0;
    struct stat_local before, after;
    struct sha256_ctx hash;
    u8 digest[32];
    u64 offset = 0;
    i64 fd;
    if (index >= expected->probe_count ||
        !append_text(path, &used, base) ||
        !append_text(path, &used, expected->probes[index].path)) return 0;
    fd = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&before) < 0 ||
        (before.mode & S_IFMT) != S_IFREG || before.nlink != 1 || before.size < 0 ||
        (u64)before.size != expected->probes[index].size) {
        if (fd >= 0) sc1(SYS_close, fd);
        return 0;
    }
    sha_init(&hash);
    while (offset < expected->probes[index].size) {
        usize chunk = expected->probes[index].size - offset > FILESYSTEM_IO_BYTES ?
            FILESYSTEM_IO_BYTES : (usize)(expected->probes[index].size - offset);
        if (!pread_exact((int)fd, filesystem_io, chunk, offset)) { sc1(SYS_close, fd); return 0; }
        sha_update(&hash, filesystem_io, chunk);
        offset += chunk;
    }
    sha_final(&hash, digest);
    if (sc2(SYS_fstat, fd, (i64)&after) < 0 || before.dev != after.dev || before.ino != after.ino ||
        before.size != after.size || before.mode != after.mode || before.nlink != after.nlink ||
        !bytes_equal(digest, expected->probes[index].digest, 32)) {
        sc1(SYS_close, fd);
        return 0;
    }
    sc1(SYS_close, fd);
    return 1;
}

static void close_staging_fds(int root_fd, int lower_fds[LOWER_MAX], u32 lower_count,
                              int state_fd, int upper_fd, int work_fd) {
    u32 i;
    if (root_fd >= 0) sc1(SYS_close, root_fd);
    for (i = 0; i < lower_count; i++) if (lower_fds[i] >= 0) sc1(SYS_close, lower_fds[i]);
    if (state_fd >= 0) sc1(SYS_close, state_fd);
    if (upper_fd >= 0) sc1(SYS_close, upper_fd);
    if (work_fd >= 0) sc1(SYS_close, work_fd);
}

static int assemble_staging_root(const struct expected_device_set *expected, struct opened_role *roles,
                                 const struct opened_role *transport, int *kept_merged_fd) {
    static const char *base_dirs[] = {"/run", "/run/palimpsest", "/run/palimpsest/root",
                                      "/run/palimpsest/lowers", "/run/palimpsest/merged"};
    int base_fds[5] = {-1, -1, -1, -1, -1};
    int root_fd = -1, lower_fds[LOWER_MAX], state_fd = -1, upper_fd = -1, work_fd = -1, merged_fd = -1;
    char path[PATH_MAX_LOCAL], overlay_data[PATH_MAX_LOCAL];
    u32 i;
    for (i = 0; i < LOWER_MAX; i++) lower_fds[i] = -1;
    if (sc5(SYS_mount, 0, (i64)"/", 0, MS_REC | MS_PRIVATE, 0) != 0) return 0;
    for (i = 0; i < 5; i++) if (!safe_dir(base_dirs[i], 1, i >= 2, 0, &base_fds[i])) return 0;
    for (i = 0; i < 5; i++) sc1(SYS_close, base_fds[i]);
    if (!mount_open_role(&roles[0], "/run/palimpsest/root", "ext4", MS_NODEV | MS_NOSUID,
                         EXT4_MAGIC, &root_fd) ||
        !verify_mountinfo("/run/palimpsest/root", "ext4", 0, 1,
                          roles[0].device.major, roles[0].device.minor, 0)) return 0;
    if (!safe_dir("/run/palimpsest/root/.palimpsest", 1, 0, 0700, &state_fd) ||
        !safe_dir("/run/palimpsest/root/.palimpsest/upper", 1, 0, 0755, &upper_fd) ||
        /* A prior OverlayFS mount may leave its kernel-owned work/ entry. */
        !safe_dir("/run/palimpsest/root/.palimpsest/work", 1, 0, 0700, &work_fd)) return 0;
    for (i = 0; i < expected->lower_count; i++) {
        if (!lower_path(path, i) || !mount_open_role(&roles[i + 1], path, "squashfs",
                MS_RDONLY | MS_NODEV | MS_NOSUID, SQUASHFS_MAGIC, &lower_fds[i]) ||
            !verify_mountinfo(path, "squashfs", 1, 1, roles[i + 1].device.major,
                              roles[i + 1].device.minor, 0)) return 0;
    }
    if (!build_overlay_data(overlay_data, expected->lower_count) ||
        !recheck_open_role(transport) || !recheck_open_role(&roles[0])) return 0;
    for (i = 0; i < expected->lower_count; i++) if (!recheck_open_role(&roles[i + 1])) return 0;
    if (!stable_dir("/run/palimpsest/root", root_fd, EXT4_MAGIC) ||
        !stable_dir("/run/palimpsest/root/.palimpsest", state_fd, EXT4_MAGIC) ||
        !stable_dir("/run/palimpsest/root/.palimpsest/upper", upper_fd, EXT4_MAGIC) ||
        !stable_dir("/run/palimpsest/root/.palimpsest/work", work_fd, EXT4_MAGIC) ||
        sc5(SYS_mount, (i64)"overlay", (i64)"/run/palimpsest/merged", (i64)"overlay",
            MS_NODEV | MS_NOSUID, (i64)overlay_data) != 0 ||
        !safe_dir("/run/palimpsest/merged", 0, 0, 0, &merged_fd) ||
        !stable_dir("/run/palimpsest/merged", merged_fd, OVERLAYFS_MAGIC) ||
        !verify_mountinfo("/run/palimpsest/merged", "overlay", 0, 0, 0, 0, overlay_data)) return 0;
    if (!recheck_open_role(transport) || !recheck_open_role(&roles[0]) ||
        !exact_vd_disk_count(expected->lower_count + 2)) return 0;
    for (i = 0; i < expected->lower_count; i++)
        if (!recheck_open_role(&roles[i + 1]) || !lower_path(path, i) ||
            !stable_dir(path, lower_fds[i], SQUASHFS_MAGIC)) return 0;
    for (i = 0; i < expected->probe_count; i++)
        if (!verify_probe_at(expected, i, "/run/palimpsest/merged")) return 0;
    if (sc1(SYS_syncfs, root_fd) != 0 || !recheck_open_role(&roles[0]) ||
        !stable_dir("/run/palimpsest/root", root_fd, EXT4_MAGIC) ||
        !stable_dir("/run/palimpsest/merged", merged_fd, OVERLAYFS_MAGIC)) return 0;
    close_staging_fds(root_fd, lower_fds, expected->lower_count, state_fd, upper_fd, work_fd);
    *kept_merged_fd = merged_fd;
    return 1;
}

static int transition_target_dir(const char *path, int *kept_fd) {
    return safe_dir(path, 1, 1, 0755, kept_fd) &&
           stable_dir(path, *kept_fd, OVERLAYFS_MAGIC);
}

static int transition_target_ready(const char *path, int retained_fd) {
    struct stat_local retained, current;
    struct statfs_local fs;
    int current_fd = -1;
    int valid = safe_dir(path, 0, 1, 0755, &current_fd) &&
        sc2(SYS_fstat, retained_fd, (i64)&retained) == 0 &&
        sc2(SYS_fstat, current_fd, (i64)&current) == 0 &&
        retained.dev == current.dev && retained.ino == current.ino &&
        retained.mode == current.mode && retained.uid == current.uid && retained.gid == current.gid &&
        sc2(SYS_fstatfs, current_fd, (i64)&fs) == 0 && fs.type == OVERLAYFS_MAGIC;
    if (current_fd >= 0) sc1(SYS_close, current_fd);
    return valid;
}

struct held_filesystem {
    int fd;
    struct stat_local identity;
    i64 magic;
};

static int hold_filesystem(const char *path, i64 magic, struct held_filesystem *held) {
    struct statfs_local fs;
    i64 fd = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (fd < 0 || sc2(SYS_fstat, fd, (i64)&held->identity) < 0 ||
        (held->identity.mode & S_IFMT) != S_IFDIR ||
        sc2(SYS_fstatfs, fd, (i64)&fs) < 0 || fs.type != magic) {
        if (fd >= 0) sc1(SYS_close, fd);
        return 0;
    }
    held->fd = (int)fd;
    held->magic = magic;
    return 1;
}

static int verify_held_filesystem(const char *path, struct held_filesystem *held) {
    struct stat_local current, retained;
    struct statfs_local fs;
    i64 fd = sc3(SYS_open, (i64)path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    int valid = held->fd >= 0 && fd >= 0 &&
        sc2(SYS_fstat, held->fd, (i64)&retained) == 0 &&
        sc2(SYS_fstat, fd, (i64)&current) == 0 &&
        retained.dev == held->identity.dev && retained.ino == held->identity.ino &&
        retained.mode == held->identity.mode && current.dev == held->identity.dev &&
        current.ino == held->identity.ino && current.mode == held->identity.mode &&
        sc2(SYS_fstatfs, fd, (i64)&fs) == 0 && fs.type == held->magic;
    if (fd >= 0) sc1(SYS_close, fd);
    return valid;
}

static int verify_root_identity(int merged_fd, const struct stat_local *merged_identity) {
    struct stat_local slash, proc_root, retained;
    struct statfs_local fs;
    i64 slash_fd = sc3(SYS_open, (i64)"/", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    i64 proc_root_fd = sc3(SYS_open, (i64)"/proc/self/root", O_RDONLY | O_CLOEXEC | O_DIRECTORY, 0);
    int valid = slash_fd >= 0 && proc_root_fd >= 0 &&
        sc2(SYS_fstat, slash_fd, (i64)&slash) == 0 &&
        sc2(SYS_fstat, proc_root_fd, (i64)&proc_root) == 0 &&
        sc2(SYS_fstat, merged_fd, (i64)&retained) == 0 &&
        retained.dev == merged_identity->dev && retained.ino == merged_identity->ino &&
        retained.mode == merged_identity->mode && slash.dev == merged_identity->dev &&
        slash.ino == merged_identity->ino && slash.mode == merged_identity->mode &&
        slash.dev == proc_root.dev && slash.ino == proc_root.ino && slash.mode == proc_root.mode &&
        sc2(SYS_fstatfs, slash_fd, (i64)&fs) == 0 && fs.type == OVERLAYFS_MAGIC &&
        sc1(SYS_syncfs, slash_fd) == 0;
    if (proc_root_fd >= 0) sc1(SYS_close, proc_root_fd);
    if (slash_fd >= 0) sc1(SYS_close, slash_fd);
    return valid;
}

static int transition_root(struct expected_device_set *expected, struct opened_role *roles,
                           struct opened_role *transport, int merged_fd) {
    struct held_filesystem dev = {.fd = -1}, sys = {.fd = -1}, proc = {.fd = -1};
    struct stat_local merged_identity;
    int dev_target = -1, sys_target = -1, proc_target = -1;
    u32 i;
    int valid;
    if (merged_fd < 0 || sc2(SYS_fstat, merged_fd, (i64)&merged_identity) < 0 ||
        (merged_identity.mode & S_IFMT) != S_IFDIR ||
        !hold_filesystem("/dev", 0x01021994, &dev) ||
        !hold_filesystem("/sys", 0x62656572, &sys) ||
        !hold_filesystem("/proc", 0x9fa0, &proc) ||
        !transition_target_dir("/run/palimpsest/merged/proc", &proc_target) ||
        !transition_target_dir("/run/palimpsest/merged/sys", &sys_target) ||
        !transition_target_dir("/run/palimpsest/merged/dev", &dev_target)) goto rejected;
    if (!transition_target_ready("/run/palimpsest/merged/dev", dev_target)) goto rejected;
    sc1(SYS_close, dev_target); dev_target = -1;
    if (sc5(SYS_mount, (i64)"/dev", (i64)"/run/palimpsest/merged/dev", 0, MS_MOVE, 0) != 0) goto rejected;
    if (!transition_target_ready("/run/palimpsest/merged/sys", sys_target)) goto rejected;
    sc1(SYS_close, sys_target); sys_target = -1;
    if (sc5(SYS_mount, (i64)"/sys", (i64)"/run/palimpsest/merged/sys", 0, MS_MOVE, 0) != 0) goto rejected;
    if (!transition_target_ready("/run/palimpsest/merged/proc", proc_target)) goto rejected;
    sc1(SYS_close, proc_target); proc_target = -1;
    if (sc5(SYS_mount, (i64)"/proc", (i64)"/run/palimpsest/merged/proc", 0, MS_MOVE, 0) != 0 ||
        sc1(SYS_chdir, (i64)"/run/palimpsest/merged") != 0 ||
        sc5(SYS_mount, (i64)".", (i64)"/", 0, MS_MOVE, 0) != 0 ||
        sc1(SYS_chroot, (i64)".") != 0 || sc1(SYS_chdir, (i64)"/") != 0) goto rejected;
    valid = sc0(SYS_getpid) == 1 && verify_root_identity(merged_fd, &merged_identity) &&
        verify_mountinfo("/", "overlay", 0, 0, 0, 0, 0) &&
        verify_held_filesystem("/dev", &dev) &&
        verify_held_filesystem("/sys", &sys) &&
        verify_held_filesystem("/proc", &proc) &&
        recheck_open_role(transport) && recheck_open_role(&roles[0]) &&
        exact_vd_disk_count(expected->lower_count + 2);
    for (i = 0; valid && i < expected->lower_count; i++)
        if (!recheck_open_role(&roles[i + 1])) valid = 0;
    for (i = 0; valid && i < expected->probe_count; i++)
        if (!verify_probe_at(expected, i, "")) valid = 0;
    sc1(SYS_close, dev.fd);
    sc1(SYS_close, sys.fd);
    sc1(SYS_close, proc.fd);
    sc1(SYS_close, merged_fd);
    return valid;
rejected:
    if (dev_target >= 0) sc1(SYS_close, dev_target);
    if (sys_target >= 0) sc1(SYS_close, sys_target);
    if (proc_target >= 0) sc1(SYS_close, proc_target);
    if (dev.fd >= 0) sc1(SYS_close, dev.fd);
    if (sys.fd >= 0) sc1(SYS_close, sys.fd);
    if (proc.fd >= 0) sc1(SYS_close, proc.fd);
    if (merged_fd >= 0) sc1(SYS_close, merged_fd);
    return 0;
}

static u64 supervised_signal_mask(void) {
    return ((u64)1 << (1 - 1)) | ((u64)1 << (2 - 1)) | ((u64)1 << (3 - 1)) |
           ((u64)1 << (10 - 1)) | ((u64)1 << (12 - 1)) | ((u64)1 << (15 - 1)) |
           ((u64)1 << (SIGCHLD - 1));
}

static u32 workload_status(int status) {
    u32 signal_number = (u32)status & 0x7f;
    return signal_number ? 128 + signal_number : ((u32)status >> 8) & 0xff;
}

static void set_workload_failure(struct child_error_local *failure, u32 stage, i64 error) {
    failure->stage = stage;
    failure->error = error < 0 ? (u32)(-error) : (u32)error;
}

static int read_account_database(const char *name, u8 *out, usize *used, int required) {
    struct stat_local directory_stat, st;
    i64 directory = sc3(SYS_open, (i64)"/etc",
                        O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    i64 descriptor;
    usize total = 0, i;
    if (directory == -ENOENT) return required ? 0 : 2;
    if (directory < 0 || sc2(SYS_fstat, directory, (i64)&directory_stat) != 0 ||
        (directory_stat.mode & S_IFMT) != S_IFDIR || directory_stat.uid != 0 ||
        (directory_stat.mode & 0022)) {
        if (directory >= 0) sc1(SYS_close, directory);
        return 0;
    }
    descriptor = sc4(SYS_openat, directory, (i64)name,
                     O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW, 0);
    if (descriptor == -ENOENT) { sc1(SYS_close, directory); return required ? 0 : 2; }
    if (descriptor < 0 || sc2(SYS_fstat, descriptor, (i64)&st) != 0 ||
        (st.mode & S_IFMT) != S_IFREG || st.uid != 0 || st.nlink != 1 ||
        (st.mode & 0022) || st.size < 0 || st.size > ACCOUNT_DATABASE_MAX_LOCAL) {
        if (descriptor >= 0) sc1(SYS_close, descriptor);
        sc1(SYS_close, directory);
        return 0;
    }
    while (total < (usize)st.size) {
        i64 count = sc3(SYS_read, descriptor, (i64)(out + total), (usize)st.size - total);
        if (count == -EINTR) continue;
        if (count <= 0 || (usize)count > (usize)st.size - total) {
            sc1(SYS_close, descriptor); sc1(SYS_close, directory); return 0;
        }
        total += (usize)count;
    }
    if (sc1(SYS_close, descriptor) != 0 || sc1(SYS_close, directory) != 0 ||
        (total && out[total - 1] != '\n')) return 0;
    for (i = 0; i < total; i++) if (!out[i] || out[i] >= 0x80) return 0;
    *used = total;
    return 1;
}

static int account_field(const u8 *line, usize line_size, u32 index, struct span *out) {
    usize start = 0, i;
    u32 field = 0;
    for (i = 0; i <= line_size; i++) {
        if (i != line_size && line[i] != ':') continue;
        if (field == index) { out->p = (const char *)line + start; out->n = i - start; return 1; }
        field++;
        start = i + 1;
    }
    return 0;
}

static int account_line_fields(const u8 *line, usize line_size, u32 expected) {
    usize i;
    u32 fields = 1;
    for (i = 0; i < line_size; i++) if (line[i] == ':') fields++;
    return fields == expected;
}

static int account_name_equal(const char *expected, struct span actual) {
    usize size = slen(expected);
    return size == actual.n && bytes_equal(expected, actual.p, size);
}

static int resolve_workload_identity(struct guest_process *process) {
    usize passwd_size = 0, group_size = 0, offset, matches = 0;
    int passwd_state;
    u32 primary_gid = 0;
    if (!process || !process->user_name || (process->group_present && !process->group_name)) return 0;
    if (process->user_numeric && process->group_present && process->group_numeric) return 1;
    passwd_state = 2;
    if (!process->user_numeric || !process->group_present)
        passwd_state = read_account_database("passwd", passwd_database, &passwd_size,
                                             !process->user_numeric);
    if (!passwd_state) return 0;
    if (passwd_state == 1) {
        for (offset = 0; offset < passwd_size;) {
            usize end = offset;
            struct span name, uid_text, gid_text;
            u32 uid, gid;
            while (end < passwd_size && passwd_database[end] != '\n') end++;
            if (end == offset || passwd_database[offset] == '#') { offset = end + 1; continue; }
            if (!account_line_fields(passwd_database + offset, end - offset, 7) ||
                !account_field(passwd_database + offset, end - offset, 0, &name) || !valid_account_name(name) ||
                !account_field(passwd_database + offset, end - offset, 2, &uid_text) || !numeric_account(uid_text, &uid) ||
                !account_field(passwd_database + offset, end - offset, 3, &gid_text) || !numeric_account(gid_text, &gid))
                return 0;
            if ((process->user_numeric && uid == process->uid) ||
                (!process->user_numeric && account_name_equal(process->user_name, name))) {
                matches++;
                process->uid = uid;
                primary_gid = gid;
            }
            offset = end + 1;
        }
    }
    if ((!process->user_numeric && matches != 1) || matches > 1) return 0;
    if (!process->group_present) { process->gid = matches ? primary_gid : 0; return 1; }
    if (process->group_numeric) return 1;
    if (read_account_database("group", group_database, &group_size, 1) != 1) return 0;
    matches = 0;
    for (offset = 0; offset < group_size;) {
        usize end = offset;
        struct span name, gid_text;
        u32 gid;
        while (end < group_size && group_database[end] != '\n') end++;
        if (end == offset || group_database[offset] == '#') { offset = end + 1; continue; }
        if (!account_line_fields(group_database + offset, end - offset, 4) ||
            !account_field(group_database + offset, end - offset, 0, &name) || !valid_account_name(name) ||
            !account_field(group_database + offset, end - offset, 2, &gid_text) || !numeric_account(gid_text, &gid))
            return 0;
        if (account_name_equal(process->group_name, name)) { matches++; process->gid = gid; }
        offset = end + 1;
    }
    return matches == 1;
}

static i64 exec_workload(struct guest_process *process) {
    const char *path = OCI_DEFAULT_PATH_LOCAL;
    usize i;
    int denied = 0;
    char candidate[PATH_MAX_LOCAL];
    if (!process || !process->argv[0] || !process->argv[0][0]) return -EINVAL;
    for (i = 0; process->argv[0][i]; i++)
        if (process->argv[0][i] == '/') return sc3(SYS_execve, (i64)process->argv[0],
                                                   (i64)process->argv, (i64)process->envp);
    for (i = 0; i < process->envc; i++)
        if (process->envp[i][0] == 'P' && process->envp[i][1] == 'A' && process->envp[i][2] == 'T' &&
            process->envp[i][3] == 'H' && process->envp[i][4] == '=') path = process->envp[i] + 5;
    for (;;) {
        usize path_size = 0, command_size = slen(process->argv[0]), used = 0;
        i64 operation;
        while (path[path_size] && path[path_size] != ':') path_size++;
        if (path_size) {
            if (path_size + 1 + command_size + 1 > sizeof(candidate)) return -ENAMETOOLONG;
            memcpy(candidate, path, path_size); used = path_size; candidate[used++] = '/';
        } else if (command_size + 1 > sizeof(candidate)) return -ENAMETOOLONG;
        memcpy(candidate + used, process->argv[0], command_size + 1);
        operation = sc3(SYS_execve, (i64)candidate, (i64)process->argv, (i64)process->envp);
        if (operation == -EACCES) denied = 1;
        else if (operation != -ENOENT && operation != -ENOTDIR) return operation;
        path += path_size;
        if (!*path) break;
        path++;
    }
    return denied ? -EACCES : -ENOENT;
}

static int drop_workload_credentials(struct guest_process *process,
                                     struct child_error_local *failure) {
    u32 real_id = 0, effective_id = 0, saved_id = 0;
    i64 operation = sc2(SYS_setgroups, 0, 0);
    if (operation != 0) {
        set_workload_failure(failure, 3, operation);
        return 0;
    }
    operation = sc2(SYS_getgroups, 0, 0);
    if (operation != 0) {
        set_workload_failure(failure, 3, operation < 0 ? operation : EIO);
        return 0;
    }
    operation = sc3(SYS_setresgid, process->gid, process->gid, process->gid);
    if (operation != 0) {
        set_workload_failure(failure, 4, operation);
        return 0;
    }
    operation = sc3(SYS_getresgid, (i64)&real_id, (i64)&effective_id, (i64)&saved_id);
    if (operation != 0 || real_id != process->gid || effective_id != process->gid || saved_id != process->gid) {
        set_workload_failure(failure, 4, operation != 0 ? operation : EIO);
        return 0;
    }
    operation = sc3(SYS_setresuid, process->uid, process->uid, process->uid);
    if (operation != 0) {
        set_workload_failure(failure, 5, operation);
        return 0;
    }
    real_id = effective_id = saved_id = 0;
    operation = sc3(SYS_getresuid, (i64)&real_id, (i64)&effective_id, (i64)&saved_id);
    if (operation != 0 || real_id != process->uid || effective_id != process->uid || saved_id != process->uid) {
        set_workload_failure(failure, 5, operation != 0 ? operation : EIO);
        return 0;
    }
    return 1;
}

static int verify_root_supervisor(struct supervisor_result *result) {
    u32 real_id = 1, effective_id = 1, saved_id = 1;
    i64 groups = sc2(SYS_getgroups, 0, 0);
    if (groups != 0 || sc3(SYS_getresuid, (i64)&real_id, (i64)&effective_id, (i64)&saved_id) != 0 ||
        real_id != 0 || effective_id != 0 || saved_id != 0) return 0;
    real_id = effective_id = saved_id = 1;
    if (sc3(SYS_getresgid, (i64)&real_id, (i64)&effective_id, (i64)&saved_id) != 0 ||
        real_id != 0 || effective_id != 0 || saved_id != 0) return 0;
    result->pid1_uid = 0;
    result->pid1_gid = 0;
    result->pid1_groups = 0;
    return 1;
}

static int exact_line_once_local(const u8 *payload, usize size, const char *expected) {
    usize expected_size = slen(expected), start = 0, i, j;
    int matches = 0;
    for (i = 0; i < size; i++) {
        u8 difference = 0;
        usize line_size;
        if (payload[i] != '\n') continue;
        line_size = i - start + 1;
        if (line_size == expected_size) {
            for (j = 0; j < line_size; j++) difference |= payload[start + j] ^ (u8)expected[j];
            if (!difference) matches++;
        }
        start = i + 1;
    }
    return matches == 1;
}

static u64 make_device_number(u32 major, u32 minor) {
    return (minor & 0xffU) | ((u64)(major & 0xfffU) << 8) |
           ((u64)(minor & ~0xffU) << 12) | ((u64)(major & ~0xfffU) << 32);
}

static int make_safe_workload_device(const char *path, u32 major, u32 minor) {
    struct stat_local st;
    if (sc3(SYS_mknod, (i64)path, S_IFCHR | 0666, make_device_number(major, minor)) != 0)
        return 0;
    if (sc2(SYS_chmod, (i64)path, 0666) != 0) return 0;
    return sc4(SYS_newfstatat, AT_FDCWD, (i64)path, (i64)&st, AT_SYMLINK_NOFOLLOW) == 0 &&
           (st.mode & S_IFMT) == S_IFCHR && (st.mode & 07777) == 0666 &&
           dev_major(st.rdev) == major && dev_minor(st.rdev) == minor;
}

static int safe_workload_dev_entries(void) {
    static const char *allowed[] = {"null", "zero", "full", "random", "urandom", "tty"};
    u8 entries[2048];
    u32 seen = 0;
    i64 directory = sc3(SYS_open, (i64)"/dev", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (directory < 0) return 0;
    for (;;) {
        i64 n = sc3(SYS_getdents64, directory, (i64)entries, sizeof(entries));
        usize offset = 0;
        if (n < 0) { sc1(SYS_close, directory); return 0; }
        if (!n) break;
        while (offset < (usize)n) {
            const u8 *entry = entries + offset;
            usize length, i;
            u32 reclen;
            int match = -1;
            if ((usize)n - offset < 20) { sc1(SYS_close, directory); return 0; }
            reclen = (u32)entry[16] | ((u32)entry[17] << 8);
            if (reclen < 20 || reclen > (usize)n - offset) { sc1(SYS_close, directory); return 0; }
            for (length = 0; length < reclen - 19 && entry[19 + length]; length++) {}
            if (length == reclen - 19) { sc1(SYS_close, directory); return 0; }
            if (!((length == 1 && entry[19] == '.') ||
                  (length == 2 && entry[19] == '.' && entry[20] == '.'))) {
                for (i = 0; i < sizeof(allowed) / sizeof(allowed[0]); i++)
                    if (length == slen(allowed[i]) && bytes_equal(entry + 19, allowed[i], length)) match = (int)i;
                if (match < 0 || (seen & (1U << match))) { sc1(SYS_close, directory); return 0; }
                seen |= 1U << match;
            }
            offset += reclen;
        }
    }
    if (sc1(SYS_close, directory) != 0) return 0;
    return seen == (1U << (sizeof(allowed) / sizeof(allowed[0]))) - 1;
}

static i64 install_read_only_cgroup_view(void) {
    static const char *staging = "/dev/.palimpsest-cgroup-view";
    int staging_fd = -1;
    i64 operation = sc2(SYS_mkdir, (i64)staging, 0700);
    if (operation != 0 || !safe_dir(staging, 0, 1, 0700, &staging_fd))
        return operation != 0 ? operation : -EIO;
    operation = sc1(SYS_close, staging_fd);
    if (operation != 0) return operation;
    operation = sc5(SYS_mount, (i64)"/sys/fs/cgroup", (i64)staging, 0,
                    MS_BIND | MS_REC, 0);
    if (operation != 0) return operation;
    operation = sc5(SYS_mount, 0, (i64)staging, 0,
                    MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, 0);
    if (operation != 0) return operation;
    operation = sc2(SYS_umount2, (i64)"/sys/fs/cgroup", 0);
    if (operation != 0) return operation;
    operation = sc5(SYS_mount, (i64)staging, (i64)"/sys/fs/cgroup", 0, MS_MOVE, 0);
    if (operation != 0) return operation;
    operation = sc3(SYS_unlinkat, AT_FDCWD, (i64)staging, AT_REMOVEDIR);
    if (operation != 0 || !safe_workload_dev_entries() ||
        !verify_mountinfo("/sys/fs/cgroup", "cgroup2", 1, 0, 0, 0, 0))
        return operation != 0 ? operation : -EIO;
    return 0;
}

static int prepare_workload_mount_boundary(struct child_error_local *failure) {
    int empty = -1;
    u32 stage = 23;
    i64 operation = sc1(SYS_unshare, CLONE_NEWNS);
    if (operation != 0) goto rejected;
    operation = sc5(SYS_mount, 0, (i64)"/", 0, MS_REC | MS_PRIVATE, 0);
    if (operation != 0) goto rejected;
    stage = 27;
    operation = sc5(SYS_mount, (i64)"tmpfs", (i64)"/dev", (i64)"tmpfs",
                    MS_NOSUID | MS_NOEXEC, (i64)"mode=0755,size=64k,nr_inodes=16");
    if (operation != 0 ||
        !make_safe_workload_device("/dev/null", 1, 3) ||
        !make_safe_workload_device("/dev/zero", 1, 5) ||
        !make_safe_workload_device("/dev/full", 1, 7) ||
        !make_safe_workload_device("/dev/random", 1, 8) ||
        !make_safe_workload_device("/dev/urandom", 1, 9) ||
        !make_safe_workload_device("/dev/tty", 5, 0) ||
        !safe_workload_dev_entries()) {
        operation = -EIO;
        goto rejected;
    }
    stage = 28;
    operation = sc5(SYS_mount, (i64)"tmpfs", (i64)"/proc/1/fd", (i64)"tmpfs",
                    MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
                    (i64)"mode=0555,size=4k,nr_inodes=2");
    if (operation != 0 || !safe_dir("/proc/1/fd", 0, 1, 0, &empty) ||
        !verify_mountinfo("/proc/1/fd", "tmpfs", 1, 0, 0, 0, 0)) goto rejected;
    sc1(SYS_close, empty); empty = -1;
    operation = sc5(SYS_mount, (i64)"tmpfs", (i64)"/proc/1/fdinfo", (i64)"tmpfs",
                    MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
                    (i64)"mode=0555,size=4k,nr_inodes=2");
    if (operation != 0 || !safe_dir("/proc/1/fdinfo", 0, 1, 0, &empty) ||
        !verify_mountinfo("/proc/1/fdinfo", "tmpfs", 1, 0, 0, 0, 0)) goto rejected;
    sc1(SYS_close, empty); empty = -1;
    operation = sc5(SYS_mount, 0, (i64)"/proc", 0,
                    MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, 0);
    if (operation != 0 || !verify_mountinfo("/proc", "proc", 1, 0, 0, 0, 0)) goto rejected;
    stage = 29;
    operation = sc5(SYS_mount, (i64)"tmpfs", (i64)"/sys/class/virtio-ports", (i64)"tmpfs",
                    MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
                    (i64)"mode=0555,size=4k,nr_inodes=2");
    if (operation != 0 || !safe_dir("/sys/class/virtio-ports", 0, 1, 0, &empty)) goto rejected;
    sc1(SYS_close, empty); empty = -1;
    stage = 30;
    operation = install_read_only_cgroup_view();
    if (operation != 0) goto rejected;
    stage = 31;
    operation = sc5(SYS_mount, 0, (i64)"/sys", 0,
                    MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, 0);
    if (operation != 0 || !verify_mountinfo("/sys", "sysfs", 1, 0, 0, 0, 0)) goto rejected;
    return 1;
rejected:
    if (empty >= 0) sc1(SYS_close, empty);
    set_workload_failure(failure, stage, operation != 0 ? operation : EIO);
    return 0;
}

static int read_cap_last_cap(u32 *last) {
    u8 text[32];
    i64 count = read_bounded_file("/proc/sys/kernel/cap_last_cap", text, sizeof(text), 1, 0);
    usize size;
    u32 value;
    if (count < 2 || text[count - 1] != '\n') return 0;
    size = (usize)count - 1;
    if (!parse_u32_decimal(text, size, &value) || value > 63) return 0;
    *last = value;
    return 1;
}

static int prepare_workload_securebits(struct child_error_local *failure) {
    u32 last, capability;
    i64 operation;
    if (!read_cap_last_cap(&last)) {
        set_workload_failure(failure, 24, EIO);
        return 0;
    }
    for (capability = 0; capability <= last; capability++) {
        operation = prctl_local(PR_CAPBSET_DROP, capability);
        if (operation != 0 || prctl_local(PR_CAPBSET_READ, capability) != 0) {
            set_workload_failure(failure, 24, operation != 0 ? operation : EIO);
            return 0;
        }
    }
    operation = prctl_local(PR_SET_SECUREBITS, WORKLOAD_SECUREBITS);
    if (operation != 0 || prctl_local(PR_GET_SECUREBITS, 0) != WORKLOAD_SECUREBITS) {
        set_workload_failure(failure, 24, operation != 0 ? operation : EIO);
        return 0;
    }
    return 1;
}

static int clear_workload_capabilities(struct child_error_local *failure) {
    struct capability_header_local header = {LINUX_CAPABILITY_VERSION_3, 0};
    struct capability_data_local data[2];
    u32 last, capability;
    i64 operation;
    memset(data, 0, sizeof(data));
    operation = sc2(SYS_capset, (i64)&header, (i64)data);
    if (operation != 0) goto rejected;
    memset(data, 0xff, sizeof(data));
    operation = sc2(SYS_capget, (i64)&header, (i64)data);
    if (operation != 0 || data[0].effective || data[0].permitted || data[0].inheritable ||
        data[1].effective || data[1].permitted || data[1].inheritable) {
        operation = operation != 0 ? operation : -EIO;
        goto rejected;
    }
    operation = sc5(SYS_prctl, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0);
    if (operation != 0 || !read_cap_last_cap(&last)) {
        operation = operation != 0 ? operation : -EIO;
        goto rejected;
    }
    for (capability = 0; capability <= last; capability++) {
        if (sc5(SYS_prctl, PR_CAP_AMBIENT, PR_CAP_AMBIENT_IS_SET, capability, 0, 0) != 0 ||
            prctl_local(PR_CAPBSET_READ, capability) != 0) {
            operation = -EIO;
            goto rejected;
        }
    }
    operation = prctl_local(PR_SET_NO_NEW_PRIVS, 1);
    if (operation != 0 || prctl_local(PR_GET_NO_NEW_PRIVS, 0) != 1) {
        operation = operation != 0 ? operation : -EIO;
        goto rejected;
    }
    return 1;
rejected:
    set_workload_failure(failure, 25, operation != 0 ? operation : EIO);
    return 0;
}

static int install_workload_seccomp(struct child_error_local *failure) {
    static struct sock_filter_local filter[] = {
        BPF_STMT_LOCAL(BPF_LD | BPF_W | BPF_ABS, 4),
        BPF_JUMP_LOCAL(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT_LOCAL(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT_LOCAL(BPF_LD | BPF_W | BPF_ABS, 0),
        BPF_JUMP_LOCAL(BPF_JMP | BPF_JSET | BPF_K, X32_SYSCALL_BIT, 0, 1),
        BPF_STMT_LOCAL(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
#define DENY_SYSCALL(number) \
        BPF_JUMP_LOCAL(BPF_JMP | BPF_JEQ | BPF_K, number, 0, 1), \
        BPF_STMT_LOCAL(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
        DENY_SYSCALL(SYS_unshare),
        DENY_SYSCALL(SYS_setns),
        DENY_SYSCALL(SYS_mount),
        DENY_SYSCALL(SYS_umount2),
        DENY_SYSCALL(SYS_mknod),
        DENY_SYSCALL(SYS_mknodat),
        DENY_SYSCALL(SYS_ptrace),
        DENY_SYSCALL(SYS_process_vm_readv),
        DENY_SYSCALL(SYS_process_vm_writev),
        DENY_SYSCALL(SYS_pidfd_getfd),
        DENY_SYSCALL(SYS_open_by_handle_at),
        DENY_SYSCALL(SYS_chroot),
        DENY_SYSCALL(SYS_pivot_root),
        DENY_SYSCALL(SYS_reboot),
        DENY_SYSCALL(SYS_swapon),
        DENY_SYSCALL(SYS_swapoff),
        DENY_SYSCALL(SYS_init_module),
        DENY_SYSCALL(SYS_delete_module),
        DENY_SYSCALL(SYS_finit_module),
        DENY_SYSCALL(SYS_kexec_load),
        DENY_SYSCALL(SYS_bpf),
        DENY_SYSCALL(SYS_perf_event_open),
        DENY_SYSCALL(SYS_add_key),
        DENY_SYSCALL(SYS_request_key),
        DENY_SYSCALL(SYS_keyctl),
        DENY_SYSCALL(SYS_userfaultfd),
        DENY_SYSCALL(SYS_io_uring_setup),
        DENY_SYSCALL(SYS_io_uring_enter),
        DENY_SYSCALL(SYS_io_uring_register),
        DENY_SYSCALL(SYS_open_tree),
        DENY_SYSCALL(SYS_move_mount),
        DENY_SYSCALL(SYS_fsopen),
        DENY_SYSCALL(SYS_fsconfig),
        DENY_SYSCALL(SYS_fsmount),
        DENY_SYSCALL(SYS_fspick),
        DENY_SYSCALL(SYS_mount_setattr),
#undef DENY_SYSCALL
        BPF_JUMP_LOCAL(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT_LOCAL(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | 38),
        BPF_JUMP_LOCAL(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 4),
        BPF_STMT_LOCAL(BPF_LD | BPF_W | BPF_ABS, 16),
        BPF_STMT_LOCAL(BPF_ALU | BPF_AND | BPF_K,
                       CLONE_NEWNS | CLONE_NEWCGROUP | CLONE_NEWUTS | CLONE_NEWIPC |
                       CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET),
        BPF_JUMP_LOCAL(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, 0),
        BPF_STMT_LOCAL(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT_LOCAL(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog_local program = {
        (unsigned short)(sizeof(filter) / sizeof(filter[0])), filter
    };
    i64 operation = sc3(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, (i64)&program);
    if (operation != 0 || prctl_local(PR_GET_SECCOMP, 0) != SECCOMP_MODE_FILTER) {
        set_workload_failure(failure, 26, operation != 0 ? operation : EIO);
        return 0;
    }
    return 1;
}

static int prepare_workload_isolation(struct guest_process *process,
                                      struct child_error_local *failure) {
    return prepare_workload_mount_boundary(failure) &&
           prepare_workload_securebits(failure) &&
           drop_workload_credentials(process, failure) &&
           clear_workload_capabilities(failure) &&
           install_workload_seccomp(failure);
}

static int append_status_id_line(char out[64], const char *label, u32 value) {
    usize used = 0;
    out[0] = 0;
    return append_text(out, &used, label) && append_u32(out, &used, value) &&
           append_text(out, &used, "\t") && append_u32(out, &used, value) &&
           append_text(out, &used, "\t") && append_u32(out, &used, value) &&
           append_text(out, &used, "\t") && append_u32(out, &used, value) &&
           append_text(out, &used, "\n");
}

static int verify_workload_isolation_status(i64 pid, const struct guest_process *process) {
    u8 status[16384];
    char path[64], uid_line[64], gid_line[64];
    usize used = 0;
    i64 count;
    path[0] = 0;
    if (pid <= 1 || pid > 0xffffffffU || !append_text(path, &used, "/proc/") ||
        !append_u32(path, &used, (u32)pid) || !append_text(path, &used, "/status") ||
        !append_status_id_line(uid_line, "Uid:\t", process->uid) ||
        !append_status_id_line(gid_line, "Gid:\t", process->gid)) return 0;
    count = read_bounded_file(path, status, sizeof(status), 1, 0);
    return count > 0 &&
        exact_line_once_local(status, (usize)count, uid_line) &&
        exact_line_once_local(status, (usize)count, gid_line) &&
        exact_line_once_local(status, (usize)count, "Groups:\t \n") &&
        exact_line_once_local(status, (usize)count, "CapInh:\t0000000000000000\n") &&
        exact_line_once_local(status, (usize)count, "CapPrm:\t0000000000000000\n") &&
        exact_line_once_local(status, (usize)count, "CapEff:\t0000000000000000\n") &&
        exact_line_once_local(status, (usize)count, "CapBnd:\t0000000000000000\n") &&
        exact_line_once_local(status, (usize)count, "CapAmb:\t0000000000000000\n") &&
        exact_line_once_local(status, (usize)count, "NoNewPrivs:\t1\n") &&
        exact_line_once_local(status, (usize)count, "Seccomp:\t2\n");
}

static i64 openat_local(int directory_fd, const char *name, int flags) {
    return sc4(SYS_openat, directory_fd, (i64)name, flags, 0);
}

static int read_pinned_control(int fd, u8 *out, usize capacity, usize *used_out) {
    usize used = 0;
    if (fd < 0 || sc3(SYS_lseek, fd, 0, SEEK_SET) < 0) return 0;
    while (used < capacity) {
        i64 count = sc3(SYS_read, fd, (i64)(out + used), capacity - used);
        if (count == -EINTR) continue;
        if (count < 0 || (usize)count > capacity - used) return 0;
        if (!count) break;
        used += (usize)count;
    }
    if (used == capacity) {
        u8 extra;
        if (sc3(SYS_read, fd, (i64)&extra, 1) != 0) return 0;
    }
    *used_out = used;
    return 1;
}

static int cgroup_populated_zero(struct workload_cgroup *cgroup) {
    u8 events[256];
    usize used = 0;
    return read_pinned_control(cgroup->events_fd, events, sizeof(events), &used) &&
           exact_line_once_local(events, used, "populated 0\n");
}

static void close_workload_cgroup(struct workload_cgroup *cgroup) {
    if (cgroup->procs_fd >= 0) sc1(SYS_close, cgroup->procs_fd);
    if (cgroup->kill_fd >= 0) sc1(SYS_close, cgroup->kill_fd);
    if (cgroup->events_fd >= 0) sc1(SYS_close, cgroup->events_fd);
    if (cgroup->dir_fd >= 0) sc1(SYS_close, cgroup->dir_fd);
    if (cgroup->root_fd >= 0) sc1(SYS_close, cgroup->root_fd);
    cgroup->root_fd = cgroup->dir_fd = cgroup->procs_fd = cgroup->kill_fd = cgroup->events_fd = -1;
}

static int prepare_workload_cgroup(struct workload_cgroup *cgroup) {
    struct statfs_local fs;
    struct stat_local st;
    i64 created;
    cgroup->root_fd = cgroup->dir_fd = cgroup->procs_fd = cgroup->kill_fd = cgroup->events_fd = -1;
    if (!mkdir_ok("/sys/fs/cgroup") ||
        !mount_ok("cgroup2", "/sys/fs/cgroup", "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC, CGROUP2_MAGIC) ||
        !safe_dir("/sys/fs/cgroup", 0, 0, 0, &cgroup->root_fd)) goto rejected;
    created = sc3(SYS_mkdirat, cgroup->root_fd, (i64)"palimpsest.workload", 0755);
    if (created != 0 && created != -EEXIST) goto rejected;
    cgroup->dir_fd = (int)openat_local(cgroup->root_fd, "palimpsest.workload",
                                      O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY);
    if (cgroup->dir_fd < 0 || sc2(SYS_fstat, cgroup->dir_fd, (i64)&st) != 0 ||
        (st.mode & S_IFMT) != S_IFDIR || st.uid != 0 || st.gid != 0 || (st.mode & 07777) != 0755 ||
        sc2(SYS_fstatfs, cgroup->dir_fd, (i64)&fs) != 0 || fs.type != CGROUP2_MAGIC) goto rejected;
    cgroup->procs_fd = (int)openat_local(cgroup->dir_fd, "cgroup.procs", O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    cgroup->kill_fd = (int)openat_local(cgroup->dir_fd, "cgroup.kill", O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
    cgroup->events_fd = (int)openat_local(cgroup->dir_fd, "cgroup.events", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (cgroup->procs_fd < 0 || cgroup->kill_fd < 0 || cgroup->events_fd < 0 ||
        !cgroup_populated_zero(cgroup)) goto rejected;
    return 1;
rejected:
    close_workload_cgroup(cgroup);
    return 0;
}

static int move_pid_to_workload_cgroup(struct workload_cgroup *cgroup, i64 pid) {
    char pid_text[16], proc_path[64];
    usize used = 0, proc_used = 0;
    if (pid <= 1 || pid > 0xffffffffu || !append_u32(pid_text, &used, (u32)pid) ||
        used + 2 > sizeof(pid_text)) return 0;
    pid_text[used++] = '\n';
    pid_text[used] = 0;
    if (cgroup->procs_fd < 0 || sc3(SYS_write, cgroup->procs_fd, (i64)pid_text, used) != (i64)used) return 0;
    proc_path[0] = 0;
    if (!append_text(proc_path, &proc_used, "/proc/") || !append_u32(proc_path, &proc_used, (u32)pid) ||
        !append_text(proc_path, &proc_used, "/cgroup")) return 0;
    return read_exact_attr(proc_path, "0::/palimpsest.workload\n");
}

static int kill_workload_cgroup(struct workload_cgroup *cgroup) {
    return cgroup->kill_fd >= 0 && sc3(SYS_write, cgroup->kill_fd, (i64)"1\n", 2) == 2;
}

static int remove_empty_workload_cgroup(struct workload_cgroup *cgroup) {
    u8 procs[1];
    usize used = 1;
    int valid = cgroup_populated_zero(cgroup) &&
        read_pinned_control(cgroup->procs_fd, procs, sizeof(procs), &used) && used == 0;
    if (cgroup->procs_fd >= 0 && sc1(SYS_close, cgroup->procs_fd) != 0) valid = 0;
    if (cgroup->kill_fd >= 0 && sc1(SYS_close, cgroup->kill_fd) != 0) valid = 0;
    if (cgroup->events_fd >= 0 && sc1(SYS_close, cgroup->events_fd) != 0) valid = 0;
    if (cgroup->dir_fd >= 0 && sc1(SYS_close, cgroup->dir_fd) != 0) valid = 0;
    cgroup->procs_fd = cgroup->kill_fd = cgroup->events_fd = cgroup->dir_fd = -1;
    if (valid && sc3(SYS_unlinkat, cgroup->root_fd, (i64)"palimpsest.workload", AT_REMOVEDIR) != 0) valid = 0;
    if (cgroup->root_fd >= 0 && sc1(SYS_close, cgroup->root_fd) != 0) valid = 0;
    cgroup->root_fd = -1;
    return valid;
}

static void record_reaped_child(struct supervisor_result *result, i64 reaped, i64 main_pid, int status) {
    u32 normalized = workload_status(status);
    result->reaped++;
    if (reaped != main_pid) {
        if (normalized == 128 + SIGKILL) result->forced_status = normalized;
        else result->cooperative_status = normalized;
    }
}

static __attribute__((noreturn)) void child_fail(int fd, u32 stage, i64 error) {
    struct child_error_local failure;
    set_workload_failure(&failure, stage, error);
    sc3(SYS_write, fd, (i64)&failure, sizeof(failure));
    exit_now(127);
}

static int terminate_and_reap(i64 main_pid, int signal_fd, struct workload_cgroup *cgroup,
                              struct supervisor_result *result, struct lifecycle_session *lifecycle) {
    int status;
    int lifecycle_failed = 0;
    u64 deadline = monotonic_millis() + 5000;
    struct pollfd_local pollfds[2];
    struct signalfd_siginfo_local info;
    /* Allow children already handling the forwarded signal to report their
     * own terminal status before enforcing the bounded teardown policy. */
    pollfds[0].fd = signal_fd; pollfds[0].events = POLLIN;
    pollfds[1].fd = lifecycle ? lifecycle->fd : -1; pollfds[1].events = POLLIN;
    while (monotonic_millis() < deadline) {
        int dispatch_stop = 0;
        i64 reaped = sc4(SYS_wait4, -1, (i64)&status, WNOHANG, 0);
        if (reaped > 0) { record_reaped_child(result, reaped, main_pid, status); continue; }
        if (reaped == -ECHILD) {
            int cleaned = kill_workload_cgroup(cgroup) && remove_empty_workload_cgroup(cgroup);
            return cleaned ? (lifecycle_failed ? 2 : 1) : 0;
        }
        if (reaped < 0 && reaped != -EINTR) return 0;
        pollfds[0].revents = 0; pollfds[1].revents = 0;
        {
            int count = lifecycle && lifecycle->state >= LIFECYCLE_READY &&
                        lifecycle->connection == LIFECYCLE_CONNECTED ? 2 : 1;
            int timeout = lifecycle && lifecycle->state >= LIFECYCLE_READY &&
                          lifecycle->connection == LIFECYCLE_DISCONNECTED
                              ? (int)lifecycle->reconnect_backoff_ms : 100;
            i64 polled = sc3(SYS_poll, (i64)pollfds, count, timeout);
            if (polled < 0 && polled != -EINTR) return 0;
        }
        if (pollfds[0].revents & POLLIN) sc3(SYS_read, signal_fd, (i64)&info, sizeof(info));
        if (lifecycle && lifecycle->state >= LIFECYCLE_READY) {
            int was_disconnected = lifecycle->connection == LIFECYCLE_DISCONNECTED;
            if (!lifecycle_failed && !lifecycle_pump(lifecycle, result, &dispatch_stop))
                lifecycle_failed = 1;
            if (was_disconnected && lifecycle->connection == LIFECYCLE_DISCONNECTED &&
                lifecycle->reconnect_backoff_ms < 100) {
                lifecycle->reconnect_backoff_ms *= 2;
                if (lifecycle->reconnect_backoff_ms > 100) lifecycle->reconnect_backoff_ms = 100;
            }
        }
    }
    if (!kill_workload_cgroup(cgroup)) return 0;
    for (;;) {
        i64 reaped = sc4(SYS_wait4, -1, (i64)&status, 0, 0);
        if (reaped > 0) { record_reaped_child(result, reaped, main_pid, status); continue; }
        if (reaped == -EINTR) continue;
        if (reaped == -ECHILD) break;
        return 0;
    }
    if (!remove_empty_workload_cgroup(cgroup)) return 0;
    return lifecycle_failed ? 2 : 1;
}

static int supervise_workload(struct guest_process *process, struct child_error_local *failure,
                              struct supervisor_result *result,
                              struct lifecycle_session *lifecycle) {
    u64 mask = supervised_signal_mask(), empty_mask = 0;
    int error_pipe[2], isolation_pipe[2], release_pipe[2], status = 0, main_done = 0;
    struct workload_cgroup cgroup;
    i64 signal_fd, main_pid, n;
    struct pollfd_local pollfds[2];
    struct signalfd_siginfo_local info;
    usize error_bytes = 0;
    i64 error_read = 0;
    memset(failure, 0, sizeof(*failure));
    memset(result, 0, sizeof(*result));
    result->cooperative_status = WORKLOAD_STATUS_NONE;
    result->forced_status = WORKLOAD_STATUS_NONE;
    if (!process || !process->argc || !process->argv[0] || !process->argv[0][0] || !process->cwd ||
        !lifecycle || lifecycle->fd < 0) {
        set_workload_failure(failure, 8, EINVAL);
        return 0;
    }
    if (!resolve_workload_identity(process)) {
        set_workload_failure(failure, 36, EINVAL);
        return 0;
    }
    n = sc4(SYS_rt_sigprocmask, SIG_BLOCK, (i64)&mask, 0, 8);
    if (n != 0) {
        set_workload_failure(failure, 8, n);
        return 0;
    }
    signal_fd = sc4(SYS_signalfd4, -1, (i64)&mask, 8, O_CLOEXEC);
    if (signal_fd < 0) {
        set_workload_failure(failure, 9, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    n = sc2(SYS_pipe2, (i64)error_pipe, O_CLOEXEC);
    if (n != 0) {
        set_workload_failure(failure, 10, n);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    n = sc2(SYS_pipe2, (i64)release_pipe, O_CLOEXEC);
    if (n != 0) {
        set_workload_failure(failure, 13, n);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, error_pipe[1]);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    n = sc2(SYS_pipe2, (i64)isolation_pipe, O_CLOEXEC);
    if (n != 0) {
        set_workload_failure(failure, 33, n);
        sc1(SYS_close, release_pipe[0]);
        sc1(SYS_close, release_pipe[1]);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, error_pipe[1]);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    if (!prepare_workload_cgroup(&cgroup)) {
        set_workload_failure(failure, 14, EIO);
        sc1(SYS_close, release_pipe[0]);
        sc1(SYS_close, release_pipe[1]);
        sc1(SYS_close, isolation_pipe[0]);
        sc1(SYS_close, isolation_pipe[1]);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, error_pipe[1]);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    if (!verify_root_supervisor(result)) {
        set_workload_failure(failure, 15, EIO);
        if (!remove_empty_workload_cgroup(&cgroup)) set_workload_failure(failure, 18, EIO);
        sc1(SYS_close, release_pipe[0]);
        sc1(SYS_close, release_pipe[1]);
        sc1(SYS_close, isolation_pipe[0]);
        sc1(SYS_close, isolation_pipe[1]);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, error_pipe[1]);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    n = prctl_local(PR_SET_DUMPABLE, 0);
    if (n != 0 || prctl_local(PR_GET_DUMPABLE, 0) != 0) {
        set_workload_failure(failure, 15, n != 0 ? n : EIO);
        if (!remove_empty_workload_cgroup(&cgroup)) set_workload_failure(failure, 18, EIO);
        sc1(SYS_close, release_pipe[0]);
        sc1(SYS_close, release_pipe[1]);
        sc1(SYS_close, isolation_pipe[0]);
        sc1(SYS_close, isolation_pipe[1]);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, error_pipe[1]);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    main_pid = sc0(SYS_fork);
    if (main_pid == 0) {
        i64 operation;
        u8 isolation_ready = 1, release = 0;
        if (sc1(SYS_close, lifecycle->fd) != 0) child_fail(error_pipe[1], 32, EIO);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, isolation_pipe[0]);
        sc1(SYS_close, release_pipe[1]);
        sc1(SYS_close, signal_fd);
        close_workload_cgroup(&cgroup);
        operation = sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        if (operation != 0) child_fail(error_pipe[1], 1, operation);
        operation = sc2(SYS_setpgid, 0, 0);
        if (operation != 0) child_fail(error_pipe[1], 2, operation);
        {
            struct child_error_local isolation_failure;
            if (!prepare_workload_isolation(process, &isolation_failure))
                child_fail(error_pipe[1], isolation_failure.stage, isolation_failure.error);
        }
        if (!bytes_all_zero(lifecycle->boot_key, sizeof(lifecycle->boot_key)) ||
            !bytes_all_zero(lifecycle->key_id, sizeof(lifecycle->key_id)) ||
            !bytes_all_zero(lifecycle->boot_generation, sizeof(lifecycle->boot_generation)) ||
            lifecycle->bootstrap_wire_sequence != 0 || lifecycle->key_ack_wire_sequence != 0)
            child_fail(error_pipe[1], 35, EIO);
        operation = sc3(SYS_write, isolation_pipe[1], (i64)&isolation_ready, 1);
        if (operation != 1 || sc1(SYS_close, isolation_pipe[1]) != 0)
            child_fail(error_pipe[1], 33, operation < 0 ? operation : EIO);
        operation = sc3(SYS_read, release_pipe[0], (i64)&release, 1);
        if (operation != 1 || release != 1) child_fail(error_pipe[1], 13, operation < 0 ? operation : EIO);
        if (sc1(SYS_close, release_pipe[0]) != 0) child_fail(error_pipe[1], 13, EIO);
        operation = sc1(SYS_chdir, (i64)process->cwd);
        if (operation != 0) child_fail(error_pipe[1], 6, operation);
        operation = exec_workload(process);
        child_fail(error_pipe[1], 7, operation);
    }
    sc1(SYS_close, error_pipe[1]);
    sc1(SYS_close, isolation_pipe[1]);
    sc1(SYS_close, release_pipe[0]);
    if (main_pid < 0) {
        set_workload_failure(failure, 11, main_pid);
        sc1(SYS_close, release_pipe[1]);
        sc1(SYS_close, isolation_pipe[0]);
        sc1(SYS_close, error_pipe[0]);
        if (!remove_empty_workload_cgroup(&cgroup)) set_workload_failure(failure, 18, EIO);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    {
        struct child_error_local early_failure;
        u8 isolation_ready = 0;
        u32 isolation_stage = 0;
        usize early_error_bytes = 0;
        i64 early_error_read = 0;
        i64 close_result;
        int child_failure_ready = 0;
        n = sc3(SYS_read, isolation_pipe[0], (i64)&isolation_ready, 1);
        close_result = sc1(SYS_close, isolation_pipe[0]);
        if (n == 0) {
            while (early_error_bytes < sizeof(early_failure)) {
                early_error_read = sc3(SYS_read, error_pipe[0],
                                       (i64)((u8 *)&early_failure + early_error_bytes),
                                       sizeof(early_failure) - early_error_bytes);
                if (early_error_read <= 0) break;
                early_error_bytes += (usize)early_error_read;
            }
            child_failure_ready = early_error_bytes == sizeof(early_failure);
        }
        if (close_result != 0 || n != 1 || isolation_ready != 1)
            isolation_stage = 33;
        else if (!verify_workload_isolation_status(main_pid, process))
            isolation_stage = 34;
        if (isolation_stage) {
            if (child_failure_ready)
                *failure = early_failure;
            else
                set_workload_failure(failure, isolation_stage,
                                     n < 0 ? n : (close_result < 0 ? close_result : EIO));
            sc1(SYS_close, release_pipe[1]);
            n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
            sc1(SYS_close, error_pipe[0]);
            if (!n) set_workload_failure(failure, 18, EIO);
            close_workload_cgroup(&cgroup);
            sc1(SYS_close, signal_fd);
            sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
            return n ? 0 : -1;
        }
    }
    n = sc2(SYS_setpgid, main_pid, main_pid);
    if (n != 0 || !move_pid_to_workload_cgroup(&cgroup, main_pid)) {
        set_workload_failure(failure, 16, n != 0 ? n : EIO);
        sc1(SYS_close, release_pipe[1]);
        n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
        sc1(SYS_close, error_pipe[0]);
        if (!n) set_workload_failure(failure, 18, EIO);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return n ? 0 : -1;
    }
    if (!authenticate_lifecycle_bootstrap(lifecycle)) {
        set_workload_failure(failure, 20, EIO);
        sc1(SYS_close, release_pipe[1]);
        n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, 0);
        sc1(SYS_close, error_pipe[0]);
        if (!n) set_workload_failure(failure, 18, EIO);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return n ? -2 : -1;
    }
    write_all(1, WORKLOAD_ISOLATION_MARKER);
    {
        u8 release = 1;
        n = sc3(SYS_write, release_pipe[1], (i64)&release, 1);
    }
    if (sc1(SYS_close, release_pipe[1]) != 0 || n != 1) {
        set_workload_failure(failure, 17, n < 0 ? n : EIO);
        n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
        if (!n) set_workload_failure(failure, 18, EIO);
        sc1(SYS_close, error_pipe[0]);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return n ? 0 : -1;
    }
    while (error_bytes < sizeof(*failure)) {
        n = sc3(SYS_read, error_pipe[0], (i64)((u8 *)failure + error_bytes), sizeof(*failure) - error_bytes);
        if (n <= 0) { error_read = n; break; }
        error_bytes += (usize)n;
    }
    sc1(SYS_close, error_pipe[0]);
    if (error_bytes != 0 || error_read < 0) {
        if (error_bytes != sizeof(*failure))
            set_workload_failure(failure, 12, error_read < 0 ? error_read : EIO);
        n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
        if (!n) set_workload_failure(failure, 18, EIO);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return n ? 0 : -1;
    }
    write_all(1, WORKLOAD_STARTED_MARKER);
    lifecycle->state = LIFECYCLE_READY;
    (void)send_control_message(lifecycle, 0, result);
    write_all(1, LIFECYCLE_READY_COMMITTED_MARKER);
    pollfds[0].fd = (int)signal_fd;
    pollfds[0].events = POLLIN;
    pollfds[1].fd = lifecycle->fd;
    pollfds[1].events = POLLIN;
    while (!main_done) {
        int dispatch_stop = 0;
        for (;;) {
            i64 reaped = sc4(SYS_wait4, -1, (i64)&status, WNOHANG, 0);
            if (reaped > 0) record_reaped_child(result, reaped, main_pid, status);
            if (reaped == main_pid) {
                main_done = 1;
                result->main_status = workload_status(status);
                if ((status & 0x7f) == 0) result->main_exit_code = (u32)((status >> 8) & 0xff);
                else result->main_signal = (u32)(status & 0x7f);
            }
            if (reaped <= 0) break;
        }
        if (main_done) break;
        pollfds[0].revents = 0; pollfds[1].revents = 0;
        n = sc3(SYS_poll, (i64)pollfds,
                lifecycle->connection == LIFECYCLE_CONNECTED ? 2 : 1,
                lifecycle_poll_timeout(lifecycle, -1));
        if (n == -EINTR) continue;
        if (n < 0) {
            set_workload_failure(failure, 21, n);
            n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
            if (!n) set_workload_failure(failure, 18, EIO);
            close_workload_cgroup(&cgroup);
            sc1(SYS_close, signal_fd);
            sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
            return n ? -2 : -1;
        }
        if (lifecycle->connection == LIFECYCLE_CONNECTED || n == 0) {
            int was_disconnected = lifecycle->connection == LIFECYCLE_DISCONNECTED;
            if (!lifecycle_pump(lifecycle, result, &dispatch_stop)) {
                set_workload_failure(failure, 21, EIO);
                n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
                if (!n) set_workload_failure(failure, 18, EIO);
                close_workload_cgroup(&cgroup);
                sc1(SYS_close, signal_fd);
                sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
                return n ? -2 : -1;
            }
            if (was_disconnected && lifecycle->connection == LIFECYCLE_DISCONNECTED &&
                lifecycle->reconnect_backoff_ms < 100) {
                lifecycle->reconnect_backoff_ms *= 2;
                if (lifecycle->reconnect_backoff_ms > 100) lifecycle->reconnect_backoff_ms = 100;
            }
        }
        if (dispatch_stop) {
            n = sc2(SYS_kill, -main_pid, process->stop_signal);
            if (n == -ESRCH) {
                /* STOP was authenticated and committed, but the workload may
                 * win the narrow race between the preceding WNOHANG probe and
                 * signal delivery.  Confirm that exact main process is now
                 * waitable before treating this as its natural terminal
                 * status under the accepted STOP identity. */
                do {
                    n = sc4(SYS_wait4, main_pid, (i64)&status, 0, 0);
                } while (n == -EINTR);
                if (n == main_pid) {
                    record_reaped_child(result, n, main_pid, status);
                    main_done = 1;
                    result->main_status = workload_status(status);
                    if ((status & 0x7f) == 0) result->main_exit_code = (u32)((status >> 8) & 0xff);
                    else result->main_signal = (u32)(status & 0x7f);
                    continue;
                }
            }
            if (n != 0) {
                lifecycle->poisoned = 1;
                set_workload_failure(failure, 21, EIO);
                n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, lifecycle);
                if (!n) set_workload_failure(failure, 18, EIO);
                close_workload_cgroup(&cgroup);
                sc1(SYS_close, signal_fd);
                sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
                return n ? -2 : -1;
            }
            result->forwarded = process->stop_signal;
            write_all(1, LIFECYCLE_STOP_DISPATCHED_MARKER);
        }
        if (pollfds[0].revents & POLLIN) {
            n = sc3(SYS_read, signal_fd, (i64)&info, sizeof(info));
            if (n != sizeof(info) || info.signo == SIGCHLD || info.signo < 1 || info.signo > 64) continue;
            {
                u32 forwarded = info.signo == 15 ? process->stop_signal : info.signo;
                sc2(SYS_kill, -main_pid, forwarded);
                result->forwarded = forwarded;
            }
        }
    }
    if (!lifecycle->stop_request_id) {
        /* The main workload won the natural-exit/late-STOP race.  Freeze that
         * terminal cause before teardown and do not parse a STOP during the
         * cleanup window; its terminal reply_to must remain null. */
        lifecycle->state = LIFECYCLE_TERMINAL;
        lifecycle->natural_late_stop_allowed = lifecycle->connection_has_hello;
        lifecycle->terminal_exit_code = result->main_exit_code;
        lifecycle->terminal_signal = result->main_signal;
        n = sc2(SYS_kill, -main_pid, process->stop_signal);
        if (n != 0 && n != -ESRCH) {
            set_workload_failure(failure, 21, EIO);
            n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result, 0);
            if (!n) set_workload_failure(failure, 18, EIO);
            close_workload_cgroup(&cgroup);
            sc1(SYS_close, signal_fd);
            sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
            return n ? -2 : -1;
        }
    }
    n = terminate_and_reap(main_pid, (int)signal_fd, &cgroup, result,
                           lifecycle->stop_request_id ? lifecycle : 0);
    if (!n) {
        set_workload_failure(failure, 18, EIO);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return -1;
    }
    if (n == 2) {
        set_workload_failure(failure, 21, EIO);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return -2;
    }
    if (!verify_root_supervisor(result)) {
        set_workload_failure(failure, 19, EIO);
        close_workload_cgroup(&cgroup);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return -1;
    }
    if (lifecycle->stop_request_id) {
        lifecycle->state = LIFECYCLE_TERMINAL;
        lifecycle->terminal_exit_code = result->main_exit_code;
        lifecycle->terminal_signal = result->main_signal;
    }
    if (lifecycle->connection_has_hello) (void)send_control_message(lifecycle, 3, result);
    close_workload_cgroup(&cgroup);
    sc1(SYS_close, signal_fd);
    sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
    return 1;
}

static int run_consumer(const char *fixture_root, int fixture_mode) {
    struct bindings b;
    struct discovered device;
    struct expected_device_set expected;
    struct opened_role roles[LOWER_MAX + 1], transport_role;
    char path[PATH_MAX_LOCAL];
    i64 n;
    u32 i, j;
    int transport_fd = -1;
    int merged_fd = -1;
    int fixture = fixture_mode != 0;
    memset(&b, 0, sizeof(b));
    memset(&device, 0, sizeof(device));
    memset(&expected, 0, sizeof(expected));
    memset(roles, 0, sizeof(roles));
    for (i = 0; i < LOWER_MAX + 1; i++) roles[i].fd = -1;
    if (fixture) {
        if (!fixture_path(path, fixture_root, "/proc/cmdline")) return EXIT_USAGE;
    } else memcpy(path, "/proc/cmdline", 14);
    n = read_bounded_file(path, (u8 *)cmdline, CMDLINE_MAX, 1, 0);
    if (n <= 0 || !parse_cmdline(cmdline, (usize)n, &b)) return EXIT_CMDLINE;
    if (!discover(fixture_root, fixture, &b, &device)) return EXIT_DISCOVERY;
    {
        int code = verify_transport(&device, &b, &expected, &workload, &transport_fd);
        if (code) return code;
        if (fixture_mode == 1) return 0;
        if (fixture_mode == 2) return verify_fixture_filesystems(fixture_root, &expected) ? 0 : EXIT_FILESYSTEM;
    }
    if (!discover_live_role(&expected.root, &roles[0])) return EXIT_DISCOVERY;
    for (i = 0; i < expected.lower_count; i++)
        if (!discover_live_role(&expected.lowers[i], &roles[i + 1])) return EXIT_DISCOVERY;
    if (!exact_vd_disk_count(expected.lower_count + 2)) return EXIT_DISCOVERY;
    memset(&transport_role, 0, sizeof(transport_role));
    transport_role.device = device;
    memcpy(transport_role.serial, b.transport_serial, 21);
    transport_role.expected_size = device.size;
    transport_role.expected_ro = 1;
    transport_role.fd = transport_fd;
    for (i = 0; i < expected.lower_count + 1; i++) {
        if (text_equal(roles[i].device.name, device.name) ||
            (roles[i].device.major == device.major && roles[i].device.minor == device.minor) ||
            (roles[i].device.identity_dev == device.identity_dev && roles[i].device.identity_ino == device.identity_ino))
            return EXIT_DISCOVERY;
        for (j = 0; j < i; j++)
            if (text_equal(roles[i].device.name, roles[j].device.name) ||
                (roles[i].device.major == roles[j].device.major && roles[i].device.minor == roles[j].device.minor) ||
                (roles[i].device.identity_dev == roles[j].device.identity_dev &&
                 roles[i].device.identity_ino == roles[j].device.identity_ino))
                return EXIT_DISCOVERY;
    }
    if (!recheck_open_role(&transport_role)) return EXIT_DISCOVERY;
    for (i = 0; i < expected.lower_count + 1; i++)
        if (!recheck_open_role(&roles[i])) return EXIT_DISCOVERY;
    if (!exact_vd_disk_count(expected.lower_count + 2)) return EXIT_DISCOVERY;
    if (!verify_ext4_fd(roles[0].fd, &expected.root)) return EXIT_FILESYSTEM;
    for (i = 0; i < expected.lower_count; i++)
        if (!verify_squashfs_structure_fd(roles[i + 1].fd, &expected.lowers[i]) ||
            !verify_lower_digest_fd(roles[i + 1].fd, &expected.lowers[i])) return EXIT_FILESYSTEM;
    if (!recheck_open_role(&transport_role)) return EXIT_DISCOVERY;
    for (i = 0; i < expected.lower_count + 1; i++)
        if (!recheck_open_role(&roles[i])) return EXIT_DISCOVERY;
    if (!exact_vd_disk_count(expected.lower_count + 2)) return EXIT_DISCOVERY;
    if (!assemble_staging_root(&expected, roles, &transport_role, &merged_fd)) return EXIT_ASSEMBLY;
    return transition_root(&expected, roles, &transport_role, merged_fd) ? 0 : EXIT_ROOT_TRANSITION;
}

static __attribute__((noreturn, used)) void start_c(u64 *stack) {
    u64 argc = stack[0];
    char **argv = (char **)(stack + 1);
    i64 pid = sc0(SYS_getpid);
    int fixture_mode = 0;
    int code;
    struct child_error_local workload_failure;
    struct supervisor_result workload_result;
    struct lifecycle_session lifecycle;
    if (argc == 3 && pid != 1) {
        if (text_equal(argv[1], "--fixture-v1")) fixture_mode = 1;
        else if (text_equal(argv[1], "--fixture-v2")) fixture_mode = 2;
    }
    if (fixture_mode) {
        code = run_consumer(argv[2], fixture_mode);
        if (!code) write_all(1, "palimpsest guest stage1 fixture: verified\n");
        else write_all(2, "palimpsest guest stage1 fixture: rejected\n");
        exit_now(code);
    }
    if (pid != 1) exit_now(EXIT_USAGE);
    if (argc != 1 || !prepare_live()) wait_closed("palimpsest guest stage1: bootstrap preparation failed; waiting fail-closed\n");
    code = run_consumer(0, 0);
    if (code == EXIT_FILESYSTEM) wait_closed("palimpsest guest stage1: filesystem contract rejected; mount disabled; waiting fail-closed\n");
    if (code == EXIT_ASSEMBLY) wait_closed("palimpsest guest stage1: mount or staging assembly rejected; root is not slash; pivot and workload disabled; waiting fail-closed\n");
    if (code == EXIT_ROOT_TRANSITION) wait_closed("palimpsest guest stage1: root transition rejected; root state is indeterminate; workload disabled; waiting fail-closed\n");
    if (code) wait_closed("palimpsest guest stage1: pre-mount contract rejected; waiting fail-closed\n");
    write_all(1, ROOT_TRANSITION_MARKER);
    if (!prepare_lifecycle(&lifecycle)) {
        lifecycle_rejected(20, EIO);
        for (;;) sc0(SYS_pause);
    }
    code = supervise_workload(&workload, &workload_failure, &workload_result, &lifecycle);
    if (!code) {
        sc1(SYS_close, lifecycle.fd);
        wipe_lifecycle_secret(&lifecycle);
        workload_rejected(workload_failure.stage, workload_failure.error);
        for (;;) sc0(SYS_pause);
    }
    if (code == -1) {
        sc1(SYS_close, lifecycle.fd);
        wipe_lifecycle_secret(&lifecycle);
        workload_cleanup_rejected(workload_failure.stage, workload_failure.error);
        for (;;) sc0(SYS_pause);
    }
    if (code == -2) {
        sc1(SYS_close, lifecycle.fd);
        wipe_lifecycle_secret(&lifecycle);
        lifecycle_rejected(workload_failure.stage, workload_failure.error);
        for (;;) sc0(SYS_pause);
    }
    workload_terminal(&workload_result);
    service_terminal_lifecycle(&lifecycle, &workload_result);
}

__attribute__((naked, noreturn, visibility("default"))) void _start(void) {
    __asm__ volatile("mov %rsp,%rdi\n"
                     "and $-16,%rsp\n"
                     "call start_c\n");
}
