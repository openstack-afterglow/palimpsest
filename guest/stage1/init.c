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
#define SYS_setuid 105
#define SYS_setgid 106
#define SYS_setpgid 109
#define SYS_setgroups 116
#define SYS_mount 165
#define SYS_chroot 161
#define SYS_signalfd4 289
#define SYS_pipe2 293
#define SYS_syncfs 306
#define SYS_statfs 137
#define SYS_fstatfs 138
#define SYS_getdents64 217

#define O_RDONLY 0
#define O_NONBLOCK 04000
#define O_CLOEXEC 02000000
#define O_NOFOLLOW 0400000
#define O_DIRECTORY 0200000
#define POLLIN 1
#define SIG_BLOCK 0
#define SIG_SETMASK 2
#define SIGKILL 9
#define SIGCHLD 17
#define SIGSTOP 19
#define WNOHANG 1
#define S_IFMT 0170000
#define S_IFREG 0100000
#define S_IFBLK 0060000
#define S_IFDIR 0040000
#define MS_RDONLY 1
#define MS_NOSUID 2
#define MS_NODEV 4
#define MS_NOEXEC 8
#define MS_REC 16384
#define MS_PRIVATE 262144
#define MS_MOVE 8192
#define EEXIST 17
#define EBUSY 16
#define EIO 5
#define EINTR 4
#define ECHILD 10
#define EINVAL 22
#define BLKROGET 0x125e
#define BLKGETSIZE64 0x80081272

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
#define ROOT_TRANSITION_MARKER "palimpsest guest stage1: root transition complete; root is slash; workload pending\n"
#define WORKLOAD_TERMINAL_PREFIX "palimpsest guest stage1: workload terminal; main_status="
#define WORKLOAD_REJECTED_PREFIX "palimpsest guest stage1: workload launch rejected; stage="
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

struct supervisor_result {
    u32 main_status;
    u32 last_nonmain_status;
    u32 reaped;
    u32 forwarded;
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
    write_all(1, "; descendant_status=");
    write_u32(1, result->last_nonmain_status);
    write_all(1, "; reaped=");
    write_u32(1, result->reaped);
    write_all(1, "; forwarded=");
    write_u32(1, result->forwarded);
    write_all(1, "; waiting fail-closed\n");
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
    u32 argc;
    u32 envc;
    u32 uid;
    u32 gid;
    u32 stop_signal;
    usize used;
    char arena[PROCESS_MAX_LOCAL + 1];
};

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
            if (argc == 0 && process->argv[0][0] != '/') return 0;
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
    if (!plain_string(j, &s, &decoded) || !numeric_account(s, &process->gid)) return 0;
    j->process_bytes += decoded + 25;
    if (!take_char(j, ',') || !key(j, "user") || !plain_string(j, &s, &decoded) ||
        !numeric_account(s, &process->uid) ||
        !take_char(j, '}') || !take_char(j, '}')) return 0;
    j->process_bytes += decoded;
    process->argc = argc;
    process->argv[argc] = 0;
    process->envp[process->envc] = 0;
    return j->process_bytes <= PROCESS_MAX_LOCAL && process->used <= PROCESS_MAX_LOCAL + 1;
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
        !exact_string(&j, "first-party-pid1-supervisor.v1") || !take_char(&j, ',') ||
        !key(&j, "phase") || !exact_string(&j, "stage1-contract") || !take_char(&j, ',') ||
        !key(&j, "process") || !parse_process(&j, process) || !take_char(&j, ',') ||
        !key(&j, "process_policy") || !exact_string(&j, "absolute-argv0-numeric-explicit-user-group.v1") ||
        !take_char(&j, ',') || !key(&j, "protocol") || !exact_string(&j, "palimpsest.guest-stage1.v7") ||
        !take_char(&j, ',') || !key(&j, "run") || !take_char(&j, '{') || !key(&j, "name") ||
        !plain_string(&j, &s, 0) || !valid_run_name(s) || !take_char(&j, ',') || !key(&j, "run_id") ||
        !plain_string(&j, &s, 0) || !valid_uuid_span(s) || !take_char(&j, '}') || !take_char(&j, ',') ||
        !key(&j, "schema") || !exact_string(&j, "palimpsest.oci-stage1-plan.v7") || !take_char(&j, '}') ||
        j.p != j.end) return 0;
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

static void record_reaped_child(struct supervisor_result *result, i64 reaped, i64 main_pid, int status) {
    result->reaped++;
    if (reaped != main_pid) result->last_nonmain_status = workload_status(status);
}

static __attribute__((noreturn)) void child_fail(int fd, u32 stage, i64 error) {
    struct child_error_local failure;
    set_workload_failure(&failure, stage, error);
    sc3(SYS_write, fd, (i64)&failure, sizeof(failure));
    exit_now(127);
}

static void terminate_and_reap(i64 main_pid, int signal_fd, struct supervisor_result *result) {
    int status;
    u32 grace_polls = 0;
    struct pollfd_local pollfd;
    struct signalfd_siginfo_local info;
    /* Allow children already handling the forwarded signal to report their
     * own terminal status before enforcing the bounded teardown policy. */
    pollfd.fd = signal_fd;
    pollfd.events = POLLIN;
    while (grace_polls++ < 16) {
        i64 reaped = sc4(SYS_wait4, -1, (i64)&status, WNOHANG, 0);
        if (reaped > 0) { record_reaped_child(result, reaped, main_pid, status); continue; }
        if (reaped == -ECHILD) return;
        if (reaped < 0 && reaped != -EINTR) break;
        pollfd.revents = 0;
        if (sc3(SYS_poll, (i64)&pollfd, 1, 250) <= 0) break;
        if (pollfd.revents & POLLIN) sc3(SYS_read, signal_fd, (i64)&info, sizeof(info));
        else break;
    }
    sc2(SYS_kill, -main_pid, SIGKILL);
    sc2(SYS_kill, -1, SIGKILL);
    for (;;) {
        i64 reaped = sc4(SYS_wait4, -1, (i64)&status, WNOHANG, 0);
        if (reaped > 0) { record_reaped_child(result, reaped, main_pid, status); continue; }
        if (reaped == -ECHILD) return;
        if (reaped < 0 && reaped != -EINTR) return;
        pollfd.revents = 0;
        if (sc3(SYS_poll, (i64)&pollfd, 1, -1) < 0) continue;
        if (pollfd.revents & POLLIN) sc3(SYS_read, signal_fd, (i64)&info, sizeof(info));
        sc2(SYS_kill, -main_pid, SIGKILL);
        sc2(SYS_kill, -1, SIGKILL);
    }
}

static int supervise_workload(struct guest_process *process, struct child_error_local *failure,
                              struct supervisor_result *result) {
    u64 mask = supervised_signal_mask(), empty_mask = 0;
    int error_pipe[2], status = 0, main_done = 0;
    i64 signal_fd, main_pid, n;
    struct pollfd_local pollfd;
    struct signalfd_siginfo_local info;
    usize error_bytes = 0;
    i64 error_read = 0;
    memset(failure, 0, sizeof(*failure));
    memset(result, 0, sizeof(*result));
    result->last_nonmain_status = WORKLOAD_STATUS_NONE;
    if (!process || !process->argc || !process->argv[0] || process->argv[0][0] != '/' || !process->cwd) {
        set_workload_failure(failure, 8, EINVAL);
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
    main_pid = sc0(SYS_fork);
    if (main_pid == 0) {
        i64 operation;
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, signal_fd);
        operation = sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        if (operation != 0) child_fail(error_pipe[1], 1, operation);
        operation = sc2(SYS_setpgid, 0, 0);
        if (operation != 0) child_fail(error_pipe[1], 2, operation);
        operation = sc2(SYS_setgroups, 0, 0);
        if (operation != 0) child_fail(error_pipe[1], 3, operation);
        operation = sc1(SYS_setgid, process->gid);
        if (operation != 0) child_fail(error_pipe[1], 4, operation);
        operation = sc1(SYS_setuid, process->uid);
        if (operation != 0) child_fail(error_pipe[1], 5, operation);
        operation = sc1(SYS_chdir, (i64)process->cwd);
        if (operation != 0) child_fail(error_pipe[1], 6, operation);
        operation = sc3(SYS_execve, (i64)process->argv[0], (i64)process->argv, (i64)process->envp);
        child_fail(error_pipe[1], 7, operation);
    }
    sc1(SYS_close, error_pipe[1]);
    if (main_pid < 0) {
        set_workload_failure(failure, 11, main_pid);
        sc1(SYS_close, error_pipe[0]);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        return 0;
    }
    sc2(SYS_setpgid, main_pid, main_pid);
    while (error_bytes < sizeof(*failure)) {
        n = sc3(SYS_read, error_pipe[0], (i64)((u8 *)failure + error_bytes), sizeof(*failure) - error_bytes);
        if (n <= 0) { error_read = n; break; }
        error_bytes += (usize)n;
    }
    sc1(SYS_close, error_pipe[0]);
    if (error_bytes != 0 || error_read < 0) {
        terminate_and_reap(main_pid, (int)signal_fd, result);
        sc1(SYS_close, signal_fd);
        sc4(SYS_rt_sigprocmask, SIG_SETMASK, (i64)&empty_mask, 0, 8);
        if (error_bytes != sizeof(*failure))
            set_workload_failure(failure, 12, error_read < 0 ? error_read : EIO);
        return 0;
    }
    write_all(1, WORKLOAD_STARTED_MARKER);
    pollfd.fd = (int)signal_fd;
    pollfd.events = POLLIN;
    while (!main_done) {
        for (;;) {
            i64 reaped = sc4(SYS_wait4, -1, (i64)&status, WNOHANG, 0);
            if (reaped > 0) record_reaped_child(result, reaped, main_pid, status);
            if (reaped == main_pid) { main_done = 1; result->main_status = workload_status(status); }
            if (reaped <= 0) break;
        }
        if (main_done) break;
        pollfd.revents = 0;
        n = sc3(SYS_poll, (i64)&pollfd, 1, -1);
        if (n < 0) continue;
        if (!(pollfd.revents & POLLIN)) continue;
        n = sc3(SYS_read, signal_fd, (i64)&info, sizeof(info));
        if (n != sizeof(info) || info.signo == SIGCHLD || info.signo < 1 || info.signo > 64) continue;
        {
            u32 forwarded = info.signo == 15 ? process->stop_signal : info.signo;
            sc2(SYS_kill, -main_pid, forwarded);
            result->forwarded = forwarded;
        }
    }
    terminate_and_reap(main_pid, (int)signal_fd, result);
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
    if (!supervise_workload(&workload, &workload_failure, &workload_result)) {
        workload_rejected(workload_failure.stage, workload_failure.error);
        for (;;) sc0(SYS_pause);
    }
    workload_terminal(&workload_result);
    for (;;) sc0(SYS_pause);
}

__attribute__((naked, noreturn, visibility("default"))) void _start(void) {
    __asm__ volatile("mov %rsp,%rdi\n"
                     "and $-16,%rsp\n"
                     "call start_c\n");
}
