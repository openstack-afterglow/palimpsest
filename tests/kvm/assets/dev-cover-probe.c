#define _start guest_boot_start
#include "/repo/guest/stage1/init.c"
#undef _start

#define DEV_COVER_PREFIX "PALIMPSEST_DEV_COVER_V1 "
#define O_CREAT_LOCAL 0100
#define O_EXCL_LOCAL 0200

static int cmdline_has_fail(void) {
    u8 value[512];
    const char needle[] = "palimpsest.dev_cover_fail=1";
    i64 fd = sc3(SYS_open, (i64)"/proc/cmdline", O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
    i64 size;
    usize i, j;
    if (fd < 0) return -1;
    size = sc3(SYS_read, fd, (i64)value, sizeof(value));
    if (sc1(SYS_close, fd) != 0 || size <= 0) return -1;
    for (i = 0; i + sizeof(needle) - 1 <= (usize)size; i++) {
        for (j = 0; j < sizeof(needle) - 1 && value[i + j] == (u8)needle[j]; j++) {}
        if (j == sizeof(needle) - 1) return 1;
    }
    return 0;
}

static __attribute__((noreturn, used)) void probe_main(void) {
    struct stat_local target_identity, ignored, parent_before, parent_after, current;
    struct statfs_local fs;
    struct held_filesystem trusted = {.fd = -1};
    enum safe_dir_reason reason = SAFE_DIR_REASON_UNKNOWN;
    int target_fd = -1, marker_fd = -1, closed_marker_fd = -1, closed_target_fd = -1;
    i64 operation;
    int fail, status = -1, parent_dev_fd = -1, current_dev_fd = -1;
    i64 child;

    if (sc5(SYS_mount, (i64)"proc", (i64)"/proc", (i64)"proc",
            MS_NOSUID | MS_NODEV | MS_NOEXEC, 0) != 0) goto failed;
    fail = cmdline_has_fail();
    if (fail < 0 || sc5(SYS_mount, (i64)"tmpfs", (i64)"/dev", (i64)"tmpfs",
                         MS_NOSUID | MS_NODEV | MS_NOEXEC,
                         (i64)"mode=0755,size=64k,nr_inodes=16") != 0 ||
        sc2(SYS_mkdir, (i64)"/dev/image-child", 0755) != 0) goto failed;
    marker_fd = sc3(SYS_open, (i64)"/dev/image-marker",
                    O_WRONLY | O_CREAT_LOCAL | O_EXCL_LOCAL | O_CLOEXEC | O_NOFOLLOW, 0400);
    if (marker_fd < 0 || sc3(SYS_write, marker_fd, (i64)"image-owned\n", 12) != 12 ||
        sc1(SYS_close, marker_fd) != 0) goto failed;
    marker_fd = -1;
    operation = sc5(SYS_mount, (i64)"tmpfs", (i64)"/dev/image-child", (i64)"tmpfs",
                    MS_NOSUID | MS_NODEV | MS_NOEXEC, (i64)"mode=0755,size=4k,nr_inodes=2");
    if (operation != 0) goto failed;
    marker_fd = sc3(SYS_open, (i64)"/dev/image-marker", O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
    if (marker_fd < 0 ||
        !transition_target_policy_checked("/dev", TRANSITION_TARGET_DEV, 0,
                                          &target_fd, &target_identity, &reason)) goto failed;
    if (fail) {
        if (sc2(SYS_chmod, (i64)"/dev", 0555) != 0 ||
            transition_target_ready_checked("/dev", TRANSITION_TARGET_DEV, (int)target_fd,
                                             &target_identity, 0x01021994)) goto failed;
        sc1(SYS_close, marker_fd);
        sc1(SYS_close, target_fd);
        write_all(1, DEV_COVER_PREFIX "REJECT\n");
        exit_now(0);
    }
    if (!transition_target_ready_checked("/dev", TRANSITION_TARGET_DEV, target_fd,
                                         &target_identity, 0x01021994) ||
        sc1(SYS_close, marker_fd) != 0 || sc1(SYS_close, target_fd) != 0) goto failed;
    closed_marker_fd = marker_fd;
    closed_target_fd = target_fd;
    marker_fd = target_fd = -1;
    if (sc2(SYS_fstat, closed_marker_fd, (i64)&ignored) != -9 ||
        sc2(SYS_fstat, closed_target_fd, (i64)&ignored) != -9) goto failed;
    if (sc5(SYS_mount, (i64)"devtmpfs", (i64)"/trusted", (i64)"devtmpfs",
            MS_NOSUID | MS_NOEXEC, (i64)"mode=0755,size=64k,nr_inodes=32") != 0 ||
        !hold_filesystem("/trusted", 0x01021994, &trusted) ||
        sc5(SYS_mount, (i64)"/trusted", (i64)"/dev", 0, MS_MOVE, 0) != 0 ||
        !verify_held_filesystem("/dev", &trusted) ||
        sc3(SYS_open, (i64)"/dev/image-marker", O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0) != -ENOENT ||
        sc3(SYS_open, (i64)"/dev/image-child", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0) != -ENOENT)
        goto failed;
    if (sc1(SYS_close, trusted.fd) != 0) goto failed;
    trusted.fd = -1;
    write_all(1, DEV_COVER_PREFIX "TRUSTED_DEVTMPFS\n");

    parent_dev_fd = sc3(SYS_open, (i64)"/dev", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (parent_dev_fd < 0 || sc2(SYS_fstat, parent_dev_fd, (i64)&parent_before) != 0) goto failed;
    child = sc0(SYS_fork);
    if (child < 0) goto failed;
    if (child == 0) {
        if (sc3(SYS_close_range, 3, 0xffffffffU, 0) != 0 ||
            sc2(SYS_fstat, closed_marker_fd, (i64)&ignored) != -9 ||
            sc2(SYS_fstat, closed_target_fd, (i64)&ignored) != -9 ||
            sc1(SYS_unshare, CLONE_NEWNS) != 0 ||
            sc5(SYS_mount, 0, (i64)"/", 0, MS_REC | MS_PRIVATE, 0) != 0 ||
            sc5(SYS_mount, (i64)"tmpfs", (i64)"/dev", (i64)"tmpfs",
                MS_NOSUID | MS_NOEXEC, (i64)"mode=0755,size=64k,nr_inodes=16") != 0 ||
            !make_safe_workload_device("/dev/null", 1, 3) ||
            !make_safe_workload_device("/dev/zero", 1, 5) ||
            !make_safe_workload_device("/dev/full", 1, 7) ||
            !make_safe_workload_device("/dev/random", 1, 8) ||
            !make_safe_workload_device("/dev/urandom", 1, 9) ||
            !make_safe_workload_device("/dev/tty", 5, 0) ||
            !make_safe_workload_stdio_aliases() || !safe_workload_dev_entries()) exit_now(2);
        write_all(1, DEV_COVER_PREFIX "CHILD_TMPFS_6_PLUS_2\n");
        exit_now(0);
    }
    if (sc4(SYS_wait4, child, (i64)&status, 0, 0) != child || status != 0 ||
        sc2(SYS_fstat, parent_dev_fd, (i64)&parent_after) != 0 ||
        parent_before.dev != parent_after.dev || parent_before.ino != parent_after.ino ||
        parent_before.mode != parent_after.mode) goto failed;
    current_dev_fd = sc3(SYS_open, (i64)"/dev", O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY, 0);
    if (current_dev_fd < 0 || sc2(SYS_fstat, current_dev_fd, (i64)&current) != 0 ||
        current.dev != parent_before.dev || current.ino != parent_before.ino ||
        sc2(SYS_fstatfs, current_dev_fd, (i64)&fs) != 0 || fs.type != 0x01021994 ||
        sc1(SYS_close, current_dev_fd) != 0 || sc1(SYS_close, parent_dev_fd) != 0) goto failed;
    current_dev_fd = parent_dev_fd = -1;
    write_all(1, DEV_COVER_PREFIX "PARENT_DEVTMPFS_UNCHANGED\n");
    write_all(1, DEV_COVER_PREFIX "PASS\n");
    exit_now(0);
failed:
    if (marker_fd >= 0) sc1(SYS_close, marker_fd);
    if (target_fd >= 0) sc1(SYS_close, target_fd);
    if (current_dev_fd >= 0) sc1(SYS_close, current_dev_fd);
    if (parent_dev_fd >= 0) sc1(SYS_close, parent_dev_fd);
    if (trusted.fd >= 0) sc1(SYS_close, trusted.fd);
    write_all(1, DEV_COVER_PREFIX "FAIL\n");
    exit_now(1);
}

__attribute__((naked, noreturn, visibility("default"))) void probe_start(void) {
    __asm__ volatile("and $-16,%rsp\ncall probe_main\n");
}
