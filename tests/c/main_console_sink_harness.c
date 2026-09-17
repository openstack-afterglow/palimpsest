#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "guest/stage1/main_output_pump.h"

typedef unsigned int u32;
typedef unsigned long u64;
typedef long i64;

#define SYS_getpid 39
#define SYS_close 3
#define SYS_fstat 5
#define SYS_fcntl 72
#define SYS_open 2
#define F_GETFD 1
#define F_GETFL 3
#define FD_CLOEXEC 1
#define O_WRONLY 1
#define O_RDWR 2
#define O_ACCMODE 3
#define O_NONBLOCK 04000
#define O_CLOEXEC 02000000
#define O_NOFOLLOW 0400000
#define O_NOCTTY 0400
#define ELOOP 40
#define S_IFMT 0170000
#define S_IFCHR 0020000

struct stat_local {
  u64 dev, ino, nlink;
  u32 mode, uid, gid, pad;
  u64 rdev;
  i64 rest[12];
};

struct main_console_sink_local {
  int fd, original_status_flags, original_descriptor_flags;
  struct stat_local identity;
};

static struct main_console_sink_local main_console_sink = {.fd = -1};
static struct main_console_queue main_console_queue;
static int main_console_queue_active;
static u64 main_console_flush_deadline;
static int scenario;
static int closes[16];
static int calls;
static int fstats;
static int revalidating;
static int invalid_call;

#define CHECK(condition)                                                       \
  do {                                                                         \
    if (!(condition)) {                                                        \
      fprintf(stderr, "check failed at line %d\n", __LINE__);                  \
      return 1;                                                                \
    }                                                                          \
  } while (0)

static void fill_identity(struct stat_local *identity, int fd) {
  memset(identity, 0, sizeof(*identity));
  identity->dev = 7;
  identity->ino = 9;
  identity->rdev = (5 << 8) | 1;
  identity->mode = S_IFCHR | 0600;
  if (scenario == 5 && fd == 7)
    identity->ino++;
  if (scenario == 6)
    identity->mode = S_IFCHR | 0644;
  if (scenario == 16 && revalidating && fd == 1)
    identity->ino++;
  if (scenario == 18 && fd == 7)
    identity->dev++;
  if (scenario == 19 && fd == 7)
    identity->rdev++;
  if (scenario == 20 && fd == 7)
    identity->uid++;
  if (scenario == 21 && fd == 7)
    identity->gid++;
  if (scenario == 22 && fd == 7)
    identity->mode = 0100000 | 0600;
  if (scenario == 29 && fd == 7)
    identity->rdev |= (u64)1 << 32;
  if (scenario == 30 && fd == 7)
    identity->rdev |= (u64)1 << 12;
}

static i64 sc0(i64 number) {
  calls++;
  return number == SYS_getpid && scenario != 1 ? 1 : 2;
}

static i64 sc1(i64 number, i64 fd) {
  calls++;
  if (number != SYS_close)
    return -5;
  if (fd >= 0 && fd < 16)
    closes[fd]++;
  return scenario == 17 ? -5 : 0;
}

static i64 sc2(i64 number, i64 fd, i64 output) {
  calls++;
  fstats++;
  if (number != SYS_fstat || scenario == 2 || (scenario == 23 && fstats == 2) ||
      (scenario == 24 && fstats == 3) ||
      (scenario == 25 && revalidating && fd == 7))
    return -5;
  fill_identity((struct stat_local *)output, (int)fd);
  return 0;
}

static i64 sc3(i64 number, i64 fd, i64 command, i64 argument) {
  (void)argument;
  calls++;
  if (number == SYS_open) {
    if (strcmp((const char *)fd, "/proc/self/fd/1") != 0 || argument != 0)
      invalid_call = 1;
    if (command & (i64)O_NOFOLLOW) {
      if (command !=
          (O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_NOCTTY | O_NOFOLLOW))
        invalid_call = 1;
      if (scenario == 3)
        return 8;
      if (scenario >= 35 && scenario <= 37)
        return scenario - 35;
      if (scenario == 4)
        return -13;
      return -ELOOP;
    }
    if (command != (O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_NOCTTY))
      invalid_call = 1;
    if (scenario == 7)
      return -13;
    if (scenario >= 8 && scenario <= 10)
      return scenario - 8;
    return 7;
  }
  if (number != SYS_fcntl)
    return -5;
  if (scenario == 11)
    return -5;
  if (scenario == 26 && fd == 7 && command == F_GETFL)
    return -5;
  if (scenario == 27 && fd == 7 && command == F_GETFD)
    return -5;
  if (scenario == 28 && revalidating && fd == 1)
    return -5;
  if (command == F_GETFL) {
    if (fd == 1) {
      if (scenario == 12)
        return O_WRONLY | O_NONBLOCK;
      if (scenario == 32)
        return 0;
      if (scenario == 34 && revalidating)
        return O_RDWR;
      return scenario == 13 ? O_RDWR : O_WRONLY;
    }
    if (scenario == 14)
      return O_RDWR | O_NONBLOCK;
    if (scenario == 33)
      return O_WRONLY;
    return O_WRONLY | O_NONBLOCK;
  }
  if (command == F_GETFD) {
    if (fd == 1)
      return 0;
    return scenario == 15 ? 0 : FD_CLOEXEC;
  }
  return -5;
}

/*PRODUCTION_FUNCTIONS*/

static int run_success(void) {
  int before;
  CHECK(acquire_main_console_sink());
  CHECK(main_console_sink.fd == 7);
  before = calls;
  CHECK(!acquire_main_console_sink());
  CHECK(calls == before + 1 && main_console_sink.fd == 7);
  if (scenario == 16 || scenario == 25 || scenario == 28 || scenario == 34)
    revalidating = 1;
  CHECK(revalidate_main_console_sink() ==
        (scenario != 16 && scenario != 25 && scenario != 28 && scenario != 34));
  CHECK(close_main_console_sink() == (scenario != 17));
  CHECK(main_console_sink.fd == -1);
  CHECK(closes[7] == 1);
  CHECK(close_main_console_sink());
  CHECK(closes[7] == 1);
  return 0;
}

static int run(void) {
  int before;
  int expected_reopened_close;
  if (scenario == 31)
    main_console_sink.fd = 7;
  if (scenario == 0 || scenario == 13 || scenario == 16 || scenario == 17 ||
      scenario == 25 || scenario == 28 || scenario == 34)
    return run_success();
  CHECK(!acquire_main_console_sink());
  CHECK(main_console_sink.fd == (scenario == 31 ? 7 : -1));
  if (scenario == 3) {
    CHECK(closes[8] == 1);
  } else {
    CHECK(closes[0] == 0 && closes[1] == 0 && closes[2] == 0);
  }
  expected_reopened_close = scenario == 5 || scenario == 14 || scenario == 15 ||
                            (scenario >= 18 && scenario <= 24) ||
                            scenario == 26 || scenario == 27 ||
                            scenario == 29 || scenario == 30 || scenario == 33;
  CHECK(closes[7] == expected_reopened_close);
  before = calls;
  if (scenario == 31)
    main_console_sink.fd = -1;
  CHECK(close_main_console_sink());
  CHECK(calls == before);
  CHECK(!invalid_call);
  return 0;
}

int main(int argc, char **argv) {
  int result;
  if (argc != 2)
    return 2;
  scenario = atoi(argv[1]);
  result = run();
  return result || invalid_call || calls > 32;
}
