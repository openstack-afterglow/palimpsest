#include <stdio.h>
#include <string.h>

typedef long i64;
typedef unsigned int u32;
typedef unsigned long long u64;

#define WNOHANG 1
#define EINTR 4
#define ECHILD 10
#define SIGKILL 9
#define POLLIN 1
#define POLLOUT 4
#define SYS_wait4 1
#define SYS_poll 2
#define SYS_read 3
#define LIFECYCLE_READY 2
#define LIFECYCLE_CONNECTED 1
#define LIFECYCLE_DISCONNECTED 2

struct pollfd_local { int fd; short events; short revents; };
struct signalfd_siginfo_local { u32 signo; };
struct workload_agent { int unused; };
struct exec_session { int unused; };
struct supervisor_result {
  u32 main_status, cooperative_status, forced_status, reaped, forwarded;
  u32 pid1_uid, pid1_gid, pid1_groups, main_exit_code, main_signal;
};
struct lifecycle_session {
  int fd, state, connection, natural_terminal_frozen, poisoned;
  u64 reconnect_backoff_ms, reconnect_not_before;
};
struct main_output_pump { int unused; };
struct main_console_queue { int unused; };
struct main_output_local { struct main_output_pump pump; };
struct remote_exec_local { int active, pending; i64 pid; u32 wire_used; };

static struct remote_exec_local remote_exec;
static struct main_output_local main_output;
static struct main_console_queue main_console_queue;
static u64 main_workload_stop_deadline, main_workload_cleanup_deadline, main_console_flush_deadline;
static u64 clock_value, kill_time;
static i64 waits[32];
static int wait_status[32], wait_count, wait_at;
static int drained_after, service_calls, cancel_ok, service_ok, remote_ok, lifecycle_ok;
static int poll_ok, kill_ok, remove_ok, kill_calls, remove_calls, close_calls;
static int poll_calls, wait_flags_bad, poll_timeout_bad, max_poll_timeout, record_main, wait_default;

static u64 monotonic_millis(void) {
  u64 value = clock_value;
  return value;
}
static int cancel_remote_exec(int reason) { (void)reason; return cancel_ok; }
static i64 sc4(i64 call, i64 pid, i64 status, i64 flags, i64 unused) {
  (void)pid; (void)unused;
  if (call != SYS_wait4) return -99;
  if (flags != WNOHANG) wait_flags_bad = 1;
  if (wait_at >= wait_count) return kill_calls ? -ECHILD : wait_default;
  if (waits[wait_at] > 0) *(int *)status = wait_status[wait_at];
  return waits[wait_at++];
}
static i64 sc3(i64 call, i64 a, i64 b, i64 c) {
  (void)a; (void)b;
  if (call == SYS_poll) {
    poll_calls++;
    if ((int)c < 0) poll_timeout_bad = 1;
    else if ((int)c > max_poll_timeout) max_poll_timeout = (int)c;
    if (poll_ok && clock_value && c > 0) clock_value += (u64)c;
    return poll_ok ? 0 : -5;
  }
  if (call == SYS_read) return -EINTR;
  return -99;
}
static void remote_exec_reaped(i64 pid, int status) { (void)pid; (void)status; }
static u32 workload_status(int status) {
  return (status & 0x7f) ? 128u + (u32)(status & 0x7f) : (u32)((status >> 8) & 255);
}
/* RECORD_REAPED_FUNCTION */
static int service_main_output(void) { service_calls++; return service_ok; }
static int pump_remote_exec(struct workload_agent *a, struct lifecycle_session *l) {
  (void)a; (void)l; return remote_ok;
}
static int main_output_console_drained(const struct main_output_pump *p,
                                       const struct main_console_queue *q) {
  (void)p; (void)q; return service_calls >= drained_after;
}
static int kill_exec_session(struct exec_session *s) {
  (void)s; kill_calls++; if (!kill_time) kill_time = clock_value; return kill_ok;
}
static int remove_empty_exec_session_and_agent(struct workload_agent *a,
                                                struct exec_session *s) {
  (void)a; (void)s; remove_calls++; return remove_ok;
}
static void set_main_console_flush_deadline(u64 deadline) {
  if (deadline && (!main_console_flush_deadline || deadline < main_console_flush_deadline))
    main_console_flush_deadline = deadline;
}
static u32 main_output_poll(struct pollfd_local *p, u32 capacity) {
  (void)p; (void)capacity; return 0;
}
static int close_main_output(void) { close_calls++; return 1; }
static int lifecycle_pump(struct lifecycle_session *l, const struct supervisor_result *r,
                          int *stop) {
  (void)l; (void)r; (void)stop; return lifecycle_ok;
}
static int lifecycle_reconnect_due(const struct lifecycle_session *l) {
  return l->connection != LIFECYCLE_DISCONNECTED ||
         !l->reconnect_not_before || clock_value >= l->reconnect_not_before;
}
static void lifecycle_schedule_reconnect(struct lifecycle_session *l) {
  l->reconnect_not_before = clock_value + l->reconnect_backoff_ms;
}

/* TERMINATE_FUNCTION */

#define CHECK(x) do { if (!(x)) return __LINE__; } while (0)
static void reset_fixture(void) {
  memset(&remote_exec, 0, sizeof(remote_exec));
  memset(waits, 0, sizeof(waits)); memset(wait_status, 0, sizeof(wait_status));
  wait_count = wait_at = service_calls = kill_calls = remove_calls = close_calls = 0;
  poll_calls = wait_flags_bad = poll_timeout_bad = max_poll_timeout = record_main = 0;
  main_workload_stop_deadline = main_workload_cleanup_deadline = main_console_flush_deadline = 0;
  clock_value = 100; kill_time = 0; wait_default = -ECHILD;
  drained_after = 1; cancel_ok = service_ok = remote_ok = lifecycle_ok = poll_ok = 1;
  kill_ok = remove_ok = 1;
}
static int run_case(const char *name) {
  struct workload_agent agent = {0}; struct exec_session session = {0};
  struct supervisor_result result; struct lifecycle_session lifecycle;
  int rc; memset(&result, 0, sizeof(result)); memset(&lifecycle, 0, sizeof(lifecycle));
  lifecycle.fd = 9; lifecycle.state = LIFECYCLE_READY;
  lifecycle.connection = LIFECYCLE_CONNECTED; lifecycle.reconnect_backoff_ms = 10;
  reset_fixture();
  if (!strcmp(name, "queued-echild")) {
    drained_after = 3; waits[0] = waits[1] = waits[2] = -ECHILD; wait_count = 3;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 1 && service_calls >= 3 && kill_calls == 1 && remove_calls == 1);
  } else if (!strcmp(name, "drained-early")) {
    waits[0] = -ECHILD; wait_count = 1;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 1 && kill_calls == 1 && remove_calls == 1);
  } else if (!strcmp(name, "alive-five-seconds")) {
    wait_default = 0; drained_after = 1;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 1);
    CHECK(kill_calls == 1 && remove_calls == 1 && rc == 1 && kill_time >= 5100);
    CHECK(clock_value - kill_time <= 1000);
  } else if (!strcmp(name, "shared-deadline")) {
    main_workload_stop_deadline = 600; wait_default = 0;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 1 && kill_time >= 600 && kill_time < 1000);
    CHECK(main_console_flush_deadline == kill_time + 1000);
  } else if (!strcmp(name, "undrained")) {
    drained_after = 1000; wait_default = 0;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 0 && kill_calls == 1 && remove_calls == 1 && close_calls == 1);
    CHECK(clock_value >= kill_time + 1000);
  } else if (!strcmp(name, "output-error")) {
    service_ok = 0; wait_default = 0;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 0 && kill_calls == 1 && remove_calls == 1 && close_calls == 1);
  } else if (!strcmp(name, "cancel-error")) {
    cancel_ok = 0; wait_default = 0;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 0 && kill_calls == 1 && remove_calls == 1 && close_calls == 1);
  } else if (!strcmp(name, "lifecycle-error")) {
    lifecycle_ok = 0; wait_default = 0;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, &lifecycle, 0);
    CHECK(rc == 2 && kill_calls == 1 && remove_calls == 1 && close_calls == 1);
  } else if (!strcmp(name, "poll-error")) {
    poll_ok = 0; wait_default = 0; drained_after = 1000;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 0 && kill_calls == 1 && remove_calls == 1 && close_calls == 1);
    CHECK(clock_value == 100 && poll_calls == 2);
  } else if (!strcmp(name, "clock-error")) {
    clock_value = 0;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 0 && kill_calls == 1 && remove_calls == 1 && close_calls == 1);
  } else if (!strcmp(name, "main-status")) {
    waits[0] = 42; wait_status[0] = 9; waits[1] = -ECHILD; wait_count = 2;
    rc = terminate_and_reap(42, 8, &agent, &session, &result, 0, 0);
    CHECK(rc == 1 && result.main_status == 137 && result.main_signal == 9);
    record_reaped_child(&result, 77, 42, 9);
    CHECK(result.forced_status == 137);
    record_reaped_child(&result, 78, 42, 7 << 8);
    CHECK(result.cooperative_status == 7);
  } else return 2;
  CHECK(!wait_flags_bad && !poll_timeout_bad && poll_calls < 200 && max_poll_timeout <= 100);
  return 0;
}
int main(int argc, char **argv) {
  int line = argc == 2 ? run_case(argv[1]) : 2;
  if (line) fprintf(stderr, "%s failed at line %d\n", argc > 1 ? argv[1] : "argument", line);
  return line ? 1 : 0;
}
