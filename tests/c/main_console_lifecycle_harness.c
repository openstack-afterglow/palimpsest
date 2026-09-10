#include <stdlib.h>
#include <string.h>
#include "guest/stage1/main_output_pump.h"

typedef unsigned char u8; typedef unsigned int u32; typedef unsigned long u64;
typedef signed long i64; typedef unsigned long usize;
#define SYS_write 1
#define EIO 5
struct main_console_sink_local { int fd; };
static struct main_console_sink_local main_console_sink = {7};
static struct main_console_queue main_console_queue;
static int main_console_queue_active = 1;
static u64 main_console_boundary_deadline;
static u64 clock_now = 100;
static int writes, permanent;
static unsigned char delivered[32768]; static usize delivered_used;
static u64 monotonic_millis(void) { return clock_now; }
static i64 sc3(i64 call, i64 fd, i64 bytes, i64 size) {
    usize n = (usize)size;
    if (call != SYS_write || fd != 7) return -EIO;
    writes++;
    if (permanent) return -EIO;
    if (n > 257) n = 257;
    memcpy(delivered + delivered_used, (void *)(uintptr_t)bytes, n);
    delivered_used += n;
    return (i64)n;
}

/* PRODUCTION_FUNCTIONS */

int main(int argc, char **argv) {
    unsigned char frame[2500]; int scenario = argc > 1 ? atoi(argv[1]) : 0;
    memset(frame, 'B', sizeof(frame)); main_console_queue_init(&main_console_queue);
    if (!main_console_enqueue_diagnostic(&main_console_queue, frame, sizeof(frame))) return 10;
    main_console_boundary_deadline = 5100;
    if (scenario == 1) permanent = 1;
    if (scenario == 2) clock_now = 5100;
    if (scenario == 3) main_console_sink.fd = -1;
    if (scenario == 4) clock_now = 0;
    if (!scenario) {
        while (!main_console_queue_empty(&main_console_queue)) {
            int before = writes;
            if (!service_terminal_console_tick() || writes - before > 1) return 11;
            if (delivered_used && delivered_used % 257 != 0 && !main_console_queue_empty(&main_console_queue)) return 12;
        }
        return delivered_used != sizeof(frame) || memcmp(delivered, frame, sizeof(frame)) ||
               main_console_boundary_deadline != 0;
    }
    return service_terminal_console_tick() != 0 ||
           ((scenario == 2 || scenario == 4) && writes != 0);
}
