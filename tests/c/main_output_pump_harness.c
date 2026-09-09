#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "guest/stage1/main_output_pump.h"

#define CHECK(x)                                                               \
  do {                                                                         \
    if (!(x))                                                                  \
      return __LINE__;                                                         \
  } while (0)
#define CAP 16384u
enum fault { OK, AGAIN, INTR, BAD, ZERO, LARGE };

struct fixture {
  struct main_output_pump *pump;
  const unsigned char *input[2];
  main_output_size length[2], position[2], output_length[2];
  unsigned char output[2][CAP];
  unsigned int reads[2], writes, read_order[32], write_order[32];
  enum fault read_fault[2], write_fault;
  main_output_size read_limit, write_limit;
  unsigned int callback_contract_failed;
};

static main_output_count fault(enum fault value, main_output_size size) {
  if (value == AGAIN)
    return -MAIN_OUTPUT_AGAIN;
  if (value == INTR)
    return -MAIN_OUTPUT_INTR;
  if (value == BAD)
    return -5;
  if (value == ZERO)
    return 0;
  if (value == LARGE)
    return (main_output_count)(size + 1u);
  return 1;
}

static main_output_count source(void *raw, unsigned int index,
                                unsigned char *bytes, main_output_size size) {
  struct fixture *f = raw;
  main_output_size count;
  if (!size || size > MAIN_OUTPUT_QUANTUM) {
    f->callback_contract_failed = 1;
    return -5;
  }
  CHECK(index < 2u);
  if (f->reads[0] + f->reads[1] < 32u)
    f->read_order[f->reads[0] + f->reads[1]] = index;
  f->reads[index]++;
  if (f->read_fault[index] != OK)
    return fault(f->read_fault[index], size);
  count = f->length[index] - f->position[index];
  if (!count)
    return 0;
  if (count > size)
    count = size;
  if (f->read_limit && count > f->read_limit)
    count = f->read_limit;
  memcpy(bytes, f->input[index] + f->position[index], count);
  f->position[index] += count;
  return (main_output_count)count;
}

static unsigned int stream_for(struct fixture *f, const unsigned char *bytes) {
  unsigned int index;
  for (index = 0; index < 2u; index++)
    if (bytes == f->pump->stream[index].bytes + f->pump->stream[index].offset)
      return index;
  return 2u;
}

static main_output_count sink(void *raw, const unsigned char *bytes,
                              main_output_size size) {
  struct fixture *f = raw;
  unsigned int index = stream_for(f, bytes);
  main_output_size count = size;
  if (!size || size > MAIN_OUTPUT_QUANTUM) {
    f->callback_contract_failed = 1;
    return -5;
  }
  CHECK(index < 2u);
  if (f->writes < 32u)
    f->write_order[f->writes] = index;
  f->writes++;
  if (f->write_fault != OK)
    return fault(f->write_fault, size);
  if (f->write_limit && count > f->write_limit)
    count = f->write_limit;
  CHECK(f->output_length[index] + count <= CAP);
  memcpy(f->output[index] + f->output_length[index], bytes, count);
  f->output_length[index] += count;
  return (main_output_count)count;
}

static void setup(struct fixture *f, struct main_output_pump *pump,
                  const void *a, main_output_size an, const void *b,
                  main_output_size bn) {
  memset(f, 0, sizeof(*f));
  main_output_pump_init(pump);
  f->pump = pump;
  f->input[0] = a;
  f->length[0] = an;
  f->input[1] = b;
  f->length[1] = bn;
}

static int tick(struct main_output_pump *pump, struct fixture *fixture) {
  unsigned int reads0 = fixture->reads[0];
  unsigned int reads1 = fixture->reads[1];
  unsigned int writes = fixture->writes;
  int result = main_output_pump_tick(pump, fixture, source, sink);
  if (fixture->callback_contract_failed || fixture->writes - writes > 1u ||
      fixture->reads[0] - reads0 > 1u || fixture->reads[1] - reads1 > 1u) {
    return 0;
  }
  return result;
}

static int drain(struct fixture *f) {
  unsigned int i;
  for (i = 0; i < 256u && !main_output_pump_drained(f->pump); i++)
    CHECK(tick(f->pump, f));
  CHECK(main_output_pump_drained(f->pump));
  return 0;
}

static int partial(void) {
  const unsigned char a[] = {0, 1, 2, 0xff, 4}, b[] = {'x', 0, 'y'};
  struct main_output_pump p;
  struct fixture f;
  setup(&f, &p, a, sizeof(a), b, sizeof(b));
  f.read_limit = 2;
  f.write_limit = 3;
  CHECK(drain(&f) == 0);
  CHECK(f.output_length[0] == sizeof(a) && !memcmp(f.output[0], a, sizeof(a)));
  CHECK(f.output_length[1] == sizeof(b) && !memcmp(f.output[1], b, sizeof(b)));
  return 0;
}

static int fair(void) {
  unsigned char a[2048], b[2048];
  struct main_output_pump p;
  struct fixture f;
  memset(a, 'a', sizeof(a));
  memset(b, 'b', sizeof(b));
  setup(&f, &p, 0, 0, 0, 0);
  memcpy(p.stream[0].bytes, a, sizeof(a));
  memcpy(p.stream[1].bytes, b, sizeof(b));
  p.stream[0].used = p.stream[1].used = sizeof(a);
  p.stream[0].eof = p.stream[1].eof = 1;
  CHECK(tick(&p, &f));
  CHECK(tick(&p, &f));
  CHECK(tick(&p, &f));
  CHECK(f.write_order[0] == 0 && f.write_order[1] == 1 &&
        f.write_order[2] == 0);
  return 0;
}

static int source_transient(enum fault value) {
  const unsigned char peer[] = "peer";
  struct main_output_pump p;
  struct fixture f;
  setup(&f, &p, 0, 0, peer, sizeof(peer) - 1);
  f.read_fault[0] = value;
  CHECK(tick(&p, &f));
  CHECK(f.reads[0] == 1 && f.reads[1] == 1 &&
        p.stream[1].used == sizeof(peer) - 1);
  return 0;
}

static int full_buffer(void) {
  struct main_output_pump p;
  struct fixture f;
  setup(&f, &p, 0, 0, 0, 0);
  p.stream[0].used = p.stream[1].used = 4096;
  f.write_fault = AGAIN;
  CHECK(tick(&p, &f));
  CHECK(f.writes == 1 && f.reads[0] + f.reads[1] == 0);
  return 0;
}

static int compact_refill(void) {
  unsigned char input[6000];
  struct main_output_pump p;
  struct fixture f;
  unsigned int i;
  for (i = 0; i < sizeof(input); i++)
    input[i] = (unsigned char)(i % 251u);
  setup(&f, &p, input, sizeof(input), 0, 0);
  memcpy(p.stream[0].bytes, input, MAIN_OUTPUT_BUFFER_BYTES);
  p.stream[0].used = MAIN_OUTPUT_BUFFER_BYTES;
  f.position[0] = MAIN_OUTPUT_BUFFER_BYTES;
  f.write_limit = 512;
  CHECK(tick(&p, &f));
  CHECK(p.stream[0].offset == 0 &&
        p.stream[0].used == MAIN_OUTPUT_BUFFER_BYTES);
  CHECK(f.position[0] == MAIN_OUTPUT_BUFFER_BYTES + 512u);
  CHECK(!memcmp(p.stream[0].bytes, input + 512u, MAIN_OUTPUT_BUFFER_BYTES));
  CHECK(drain(&f) == 0);
  CHECK(f.output_length[0] == sizeof(input));
  CHECK(!memcmp(f.output[0], input, sizeof(input)));
  return 0;
}

static int eof_cases(void) {
  const unsigned char peer[] = "continues";
  struct main_output_pump p;
  struct fixture f;
  setup(&f, &p, 0, 0, 0, 0);
  memcpy(p.stream[0].bytes, "held", 4);
  p.stream[0].used = 4;
  p.stream[0].eof = p.stream[1].eof = 1;
  f.write_fault = AGAIN;
  CHECK(tick(&p, &f) && !main_output_pump_drained(&p));
  f.write_fault = OK;
  CHECK(tick(&p, &f));
  CHECK(main_output_pump_drained(&p));
  setup(&f, &p, 0, 0, peer, sizeof(peer) - 1);
  CHECK(drain(&f) == 0);
  CHECK(f.output_length[1] == sizeof(peer) - 1 &&
        !memcmp(f.output[1], peer, sizeof(peer) - 1));
  return 0;
}

static int sink_transient(enum fault value) {
  struct main_output_pump p;
  struct fixture f;
  setup(&f, &p, 0, 0, 0, 0);
  memcpy(p.stream[0].bytes, "data", 4);
  p.stream[0].used = 4;
  p.stream[0].eof = p.stream[1].eof = 1;
  f.write_fault = value;
  CHECK(tick(&p, &f));
  CHECK(!p.failed && p.stream[0].offset == 0 && p.stream[0].used == 4);
  return 0;
}

static int sticky(enum fault value, int read_side) {
  struct main_output_pump p;
  struct fixture f;
  unsigned int calls;
  setup(&f, &p, 0, 0, 0, 0);
  if (read_side)
    f.read_fault[0] = value;
  else {
    p.stream[0].bytes[0] = 'x';
    p.stream[0].used = 1;
    f.write_fault = value;
  }
  CHECK(!tick(&p, &f) && p.failed);
  calls = f.writes + f.reads[0] + f.reads[1];
  f.write_fault = f.read_fault[0] = OK;
  CHECK(!tick(&p, &f));
  CHECK(calls == f.writes + f.reads[0] + f.reads[1]);
  return 0;
}

static int invalid(void) {
  struct main_output_pump p;
  struct fixture f;
  unsigned int i;
  for (i = 0; i < 8; i++) {
    setup(&f, &p, 0, 0, 0, 0);
    if (i == 0)
      p.next_read = 2;
    if (i == 1)
      p.next_write = 2;
    if (i == 2)
      p.stream[0].offset = 1;
    if (i == 3)
      p.stream[0].used = 4097;
    if (i == 4)
      p.stream[0].eof = 2;
    if (i == 5)
      p.stream[1].offset = 1;
    if (i == 6)
      p.stream[1].eof = 2;
    if (i == 7)
      p.stream[1].used = 4097;
    CHECK(!tick(&p, &f));
    CHECK(p.failed && !f.writes && !f.reads[0] && !f.reads[1]);
  }
  setup(&f, &p, 0, 0, 0, 0);
  p.stream[0].eof = p.stream[1].eof = 1;
  CHECK(main_output_pump_drained(&p));
  CHECK(!main_output_pump_tick(0, &f, source, sink));
  CHECK(!f.writes && !f.reads[0] && !f.reads[1]);
  setup(&f, &p, 0, 0, 0, 0);
  p.stream[0].eof = p.stream[1].eof = 1;
  CHECK(main_output_pump_drained(&p));
  CHECK(!main_output_pump_tick(&p, &f, 0, sink));
  CHECK(p.failed && !main_output_pump_drained(&p));
  CHECK(!f.writes && !f.reads[0] && !f.reads[1]);
  setup(&f, &p, 0, 0, 0, 0);
  p.stream[0].eof = p.stream[1].eof = 1;
  CHECK(main_output_pump_drained(&p));
  CHECK(!main_output_pump_tick(&p, &f, source, 0));
  CHECK(p.failed && !main_output_pump_drained(&p));
  CHECK(!f.writes && !f.reads[0] && !f.reads[1]);
  setup(&f, &p, 0, 0, 0, 0);
  p.stream[0].eof = p.stream[1].eof = 1;
  CHECK(main_output_pump_drained(&p));
  p.stream[1].used = MAIN_OUTPUT_BUFFER_BYTES + 1u;
  CHECK(!main_output_pump_drained(&p));
  CHECK(!f.writes && !f.reads[0] && !f.reads[1]);
  return 0;
}

struct real {
  int source[2], sink;
  unsigned int writes;
};
static main_output_count real_read(void *raw, unsigned int i, unsigned char *b,
                                   main_output_size n) {
  ssize_t r = read(((struct real *)raw)->source[i], b, n);
  if (r >= 0)
    return r;
  if (errno == EAGAIN || errno == EWOULDBLOCK)
    return -11;
  if (errno == EINTR)
    return -4;
  return -errno;
}
static main_output_count real_write(void *raw, const unsigned char *b,
                                    main_output_size n) {
  struct real *fixture = raw;
  ssize_t r;
  fixture->writes++;
  r = write(fixture->sink, b, n);
  if (r >= 0)
    return r;
  if (errno == EAGAIN || errno == EWOULDBLOCK)
    return -11;
  if (errno == EINTR)
    return -4;
  return -errno;
}
static int nonblock(int fd) {
  int f = fcntl(fd, F_GETFL);
  return f < 0 ? -1 : fcntl(fd, F_SETFL, f | O_NONBLOCK);
}
static int real_pipe(void) {
  const unsigned char payload[] = "exact-pipe";
  unsigned char fill[4096], buf[8192];
  int s[2][2], out[2];
  ssize_t n;
  unsigned int i;
  struct main_output_pump p;
  struct real r;
  memset(fill, 'f', sizeof(fill));
  CHECK(!pipe(s[0]) && !pipe(s[1]) && !pipe(out));
  CHECK(!nonblock(s[0][0]) && !nonblock(s[1][0]) && !nonblock(out[0]) &&
        !nonblock(out[1]));
  while (write(out[1], fill, sizeof(fill)) > 0) {
  }
  CHECK(errno == EAGAIN || errno == EWOULDBLOCK);
  CHECK(write(s[0][1], payload, sizeof(payload) - 1) ==
        (ssize_t)sizeof(payload) - 1);
  close(s[0][1]);
  close(s[1][1]);
  main_output_pump_init(&p);
  memset(&r, 0, sizeof(r));
  r.source[0] = s[0][0];
  r.source[1] = s[1][0];
  r.sink = out[1];
  CHECK(main_output_pump_tick(&p, &r, real_read, real_write));
  CHECK(p.stream[0].used == sizeof(payload) - 1);
  CHECK(main_output_pump_tick(&p, &r, real_read, real_write));
  CHECK(r.writes == 1 && p.stream[0].offset == 0 &&
        p.stream[0].used == sizeof(payload) - 1);
  while (read(out[0], buf, sizeof(buf)) > 0) {
  }
  for (i = 0; i < 32 && !main_output_pump_drained(&p); i++)
    CHECK(main_output_pump_tick(&p, &r, real_read, real_write));
  CHECK(main_output_pump_drained(&p));
  close(out[1]);
  n = read(out[0], buf, sizeof(buf));
  CHECK(n == (ssize_t)sizeof(payload) - 1 &&
        !memcmp(buf, payload, sizeof(payload) - 1));
  close(s[0][0]);
  close(s[1][0]);
  close(out[0]);
  return 0;
}

int main(int argc, char **argv) {
  int line = 0;
  if (argc != 2)
    return 2;
  if (!strcmp(argv[1], "partial"))
    line = partial();
  else if (!strcmp(argv[1], "fair"))
    line = fair();
  else if (!strcmp(argv[1], "source-eagain"))
    line = source_transient(AGAIN);
  else if (!strcmp(argv[1], "source-eintr"))
    line = source_transient(INTR);
  else if (!strcmp(argv[1], "full-buffer"))
    line = full_buffer();
  else if (!strcmp(argv[1], "compact-refill"))
    line = compact_refill();
  else if (!strcmp(argv[1], "eof"))
    line = eof_cases();
  else if (!strcmp(argv[1], "sink-eagain"))
    line = sink_transient(AGAIN);
  else if (!strcmp(argv[1], "sink-eintr"))
    line = sink_transient(INTR);
  else if (!strcmp(argv[1], "sink-bad"))
    line = sticky(BAD, 0);
  else if (!strcmp(argv[1], "sink-zero"))
    line = sticky(ZERO, 0);
  else if (!strcmp(argv[1], "sink-large"))
    line = sticky(LARGE, 0);
  else if (!strcmp(argv[1], "read-bad"))
    line = sticky(BAD, 1);
  else if (!strcmp(argv[1], "read-large"))
    line = sticky(LARGE, 1);
  else if (!strcmp(argv[1], "invalid"))
    line = invalid();
  else if (!strcmp(argv[1], "real-pipe"))
    line = real_pipe();
  else
    return 3;
  if (line)
    fprintf(stderr, "%s failed at line %d\n", argv[1], line);
  return line ? 1 : 0;
}
