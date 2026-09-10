#include <stdio.h>
#include <string.h>

typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long usize;
typedef unsigned long long u64;
typedef long i64;

#define SYS_read 0
#define EINTR 4
#define EAGAIN 11
#define CONTROL_PAYLOAD_MAX 65532
#define LIFECYCLE_CONNECTED 1
#define LIFECYCLE_DISCONNECTED 2
#define LIFECYCLE_READY 2
#define LIFECYCLE_STOPPING 3
#define LIFECYCLE_TERMINAL 4
#define LIFECYCLE_PARTIAL_BUFFERED_MARKER "partial"
#define LIFECYCLE_STOP_DUPLICATE_MARKER "duplicate"

struct supervisor_result { int unused; };
struct lifecycle_session {
  int fd, poisoned, connection, connection_has_hello, state;
  int natural_terminal_frozen, natural_late_stop_allowed;
  int partial_frame_marker_emitted, initial_input_seen;
  u32 header_used, payload_expected, payload_used, reconnect_backoff_ms;
  int outbound_failed;
  u8 header[4];
  u64 frame_deadline, outbound_deadline, reconnect_not_before;
  u64 key_ack_wire_sequence;
};

static u8 control_payload[CONTROL_PAYLOAD_MAX];
static u64 clock_now = 100, cleanup_deadline;
static i64 read_results[8];
static const u8 *read_bytes[8];
static u32 read_sizes[8], read_at, read_count, pump_reads, writes;

static u64 monotonic_millis(void) { return clock_now; }
static u64 cap_control_deadline(u64 deadline) {
  return cleanup_deadline && (!deadline || cleanup_deadline < deadline) ? cleanup_deadline : deadline;
}
static i64 sc3(i64 call, i64 fd, i64 target, i64 size) {
  i64 result;
  (void)fd;
  if (call != SYS_read || read_at >= read_count) return -EAGAIN;
  result = read_results[read_at];
  if (result > 0) {
    if ((u64)result > (u64)size || (u32)result != read_sizes[read_at]) return -99;
    memcpy((void *)target, read_bytes[read_at], (usize)result);
  }
  read_at++;
  return result;
}
static void lifecycle_connection_lost(struct lifecycle_session *session) {
  session->connection = LIFECYCLE_DISCONNECTED;
}
static void write_all(int fd, const char *text) { (void)fd; (void)text; writes++; }

/* READ_CONTROL_FUNCTION */

static int read_control_frame(struct lifecycle_session *session, usize *size) {
  (void)session; (void)size; pump_reads++; return 1;
}
static int parse_signed_host(struct lifecycle_session *s, usize z, int k, u64 *r, u64 *w) {
  (void)s; (void)z; (void)k; (void)r; (void)w; return 1;
}
static int parse_hello(struct lifecycle_session *s, usize z) { (void)s; (void)z; return 1; }
static int lifecycle_current_snapshot(struct lifecycle_session *s, const struct supervisor_result *r) {
  (void)s; (void)r; return 1;
}
static int parse_stop(struct lifecycle_session *s, usize z) { (void)s; (void)z; return 2; }
static int parse_exec(struct lifecycle_session *s, usize z) { (void)s; (void)z; return 0; }

/* LIFECYCLE_PUMP_FUNCTION */

#define CHECK(x) do { if (!(x)) return __LINE__; } while (0)
static int run_case(const char *name) {
  struct lifecycle_session session;
  usize size = 0;
  memset(&session, 0, sizeof(session));
  read_at = read_count = pump_reads = writes = 0; clock_now = 100; cleanup_deadline = 0;
  session.fd = 7;
  if (!strcmp(name, "eintr-empty")) {
    read_results[0] = -EINTR; read_count = 1;
    CHECK(read_control_frame_actual(&session, &size) == 0);
    CHECK(!session.initial_input_seen && !session.header_used && !session.frame_deadline);
  } else if (!strcmp(name, "eintr-partial")) {
    static const u8 header[] = {0, 0, 0, 3}, payload[] = {'a'};
    read_results[0] = 4; read_bytes[0] = header; read_sizes[0] = 4;
    read_results[1] = 1; read_bytes[1] = payload; read_sizes[1] = 1;
    read_results[2] = -EINTR; read_count = 3;
    CHECK(read_control_frame_actual(&session, &size) == 0);
    CHECK(session.initial_input_seen && session.payload_expected == 3 && session.payload_used == 1);
    CHECK(session.frame_deadline == 5100 && control_payload[0] == 'a');
    read_results[3] = -EINTR; read_count = 4;
    CHECK(read_control_frame_actual(&session, &size) == 0);
    CHECK(session.payload_expected == 3 && session.payload_used == 1 && session.frame_deadline == 5100);
  } else if (!strcmp(name, "pump-yields-64")) {
    struct supervisor_result result = {0}; int stop = 0;
    session.connection_has_hello = 1; session.state = LIFECYCLE_READY;
    CHECK(lifecycle_pump(&session, &result, &stop) == 1);
    CHECK(pump_reads == 64 && writes == 64 && stop == 0);
  } else return 2;
  return 0;
}
int main(int argc, char **argv) {
  int line = argc == 2 ? run_case(argv[1]) : 2;
  if (line) fprintf(stderr, "%s failed at line %d\n", argc > 1 ? argv[1] : "argument", line);
  return line ? 1 : 0;
}
