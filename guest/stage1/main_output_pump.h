#ifndef PALIMPSEST_MAIN_OUTPUT_PUMP_H
#define PALIMPSEST_MAIN_OUTPUT_PUMP_H

/* Freestanding main-output and unified console-queue state machines used by
 * production init.c.  The caller owns polling, descriptors, STOP, reaping,
 * deadlines, sink identity and terminal publication. */
#define MAIN_OUTPUT_STREAMS 2u
#define MAIN_OUTPUT_BUFFER_BYTES 4096u
#define MAIN_OUTPUT_QUANTUM 1024u
#define MAIN_CONSOLE_QUEUE_BYTES 16384u
#define MAIN_OUTPUT_AGAIN 11l
#define MAIN_OUTPUT_INTR 4l

typedef unsigned long main_output_size;
typedef long main_output_count;
typedef main_output_count (*main_output_read_fn)(void *, unsigned int, unsigned char *, main_output_size);
typedef main_output_count (*main_output_write_fn)(void *, const unsigned char *, main_output_size);

struct main_console_queue {
    unsigned char bytes[MAIN_CONSOLE_QUEUE_BYTES];
    main_output_size offset;
    main_output_size used;
    unsigned int failed;
};

static void main_console_queue_init(struct main_console_queue *queue) {
    main_output_size i;
    unsigned char *bytes = (unsigned char *)queue;
    for (i = 0; i < sizeof(*queue); i++) bytes[i] = 0;
}

static int main_console_queue_valid(const struct main_console_queue *queue) {
    return queue && !queue->failed && queue->offset <= queue->used &&
           queue->used <= MAIN_CONSOLE_QUEUE_BYTES;
}

/* Input bytes must not alias queue storage.  Workload data applies
 * backpressure without poisoning the queue. */
static int main_console_enqueue_data(struct main_console_queue *queue,
                                     const unsigned char *bytes,
                                     main_output_size size) {
    main_output_size remaining, i;
    if (!queue || queue->failed) return 0;
    if (queue->offset > queue->used || queue->used > MAIN_CONSOLE_QUEUE_BYTES || (!bytes && size)) {
        queue->failed = 1;
        return 0;
    }
    remaining = queue->used - queue->offset;
    if (size > MAIN_CONSOLE_QUEUE_BYTES - remaining) return 0;
    if (queue->offset) {
        for (i = 0; i < remaining; i++) queue->bytes[i] = queue->bytes[queue->offset + i];
        queue->offset = 0; queue->used = remaining;
    }
    for (i = 0; i < size; i++) queue->bytes[queue->used + i] = bytes[i];
    queue->used += size;
    return 1;
}

/* Input bytes must not alias queue storage.  Diagnostics are supervisor-owned:
 * overflow is a sticky integrity failure. */
static int main_console_enqueue_diagnostic(struct main_console_queue *queue,
                                           const unsigned char *bytes,
                                           main_output_size size) {
    if (!queue || queue->failed) return 0;
    if (!main_console_enqueue_data(queue, bytes, size)) {
        queue->failed = 1;
        return 0;
    }
    return 1;
}

/* At most one nonblocking write and one quantum per call. */
static int main_console_flush_tick(struct main_console_queue *queue, void *context,
                                   main_output_write_fn write_sink) {
    main_output_size available;
    main_output_count count;
    if (!main_console_queue_valid(queue) || !write_sink) {
        if (queue) queue->failed = 1;
        return 0;
    }
    if (queue->offset == queue->used) { queue->offset = queue->used = 0; return 1; }
    available = queue->used - queue->offset;
    if (available > MAIN_OUTPUT_QUANTUM) available = MAIN_OUTPUT_QUANTUM;
    count = write_sink(context, queue->bytes + queue->offset, available);
    if (count == -MAIN_OUTPUT_AGAIN || count == -MAIN_OUTPUT_INTR) return 1;
    if (count <= 0 || (main_output_size)count > available) {
        queue->failed = 1;
        return 0;
    }
    queue->offset += (main_output_size)count;
    if (queue->offset == queue->used) queue->offset = queue->used = 0;
    return 1;
}

static int main_console_queue_empty(const struct main_console_queue *queue) {
    return main_console_queue_valid(queue) && queue->offset == queue->used;
}

/* Callbacks are trusted, nonblocking and non-reentrant.  They must honor the
 * supplied size, use the count/error ABI above, and must not mutate the pump.
 * The component cannot bound a callback that blocks. */

struct main_output_stream {
    unsigned char bytes[MAIN_OUTPUT_BUFFER_BYTES];
    main_output_size offset;
    main_output_size used;
    unsigned int eof;
};

struct main_output_pump {
    struct main_output_stream stream[MAIN_OUTPUT_STREAMS];
    unsigned int next_read;
    unsigned int next_write;
    unsigned int failed;
};

/* pump must point to writable storage for one complete main_output_pump. */
static void main_output_pump_init(struct main_output_pump *pump) {
    main_output_size i;
    unsigned char *bytes = (unsigned char *)pump;
    for (i = 0; i < sizeof(*pump); i++) bytes[i] = 0;
}

static int main_output_transient(main_output_count count) {
    return count == -MAIN_OUTPUT_AGAIN || count == -MAIN_OUTPUT_INTR;
}

/* A return of one means only that no permanent failure occurred; it does not
 * promise progress.  One tick performs at most one sink attempt and at most
 * one source attempt per stream.  Each successful transfer is at most
 * MAIN_OUTPUT_QUANTUM bytes.
 * Trying the second source after a transient first-source result prevents a
 * stalled stream from starving its peer without spinning on either stream. */
static int main_output_pump_tick(struct main_output_pump *pump, void *context,
                                 main_output_read_fn read_source,
                                 main_output_write_fn write_sink) {
    unsigned int attempt;
    if (!pump) return 0;
    if (pump->failed) return 0;
    if (!read_source || !write_sink) {
        pump->failed = 1;
        return 0;
    }
    if (pump->next_read >= MAIN_OUTPUT_STREAMS || pump->next_write >= MAIN_OUTPUT_STREAMS) {
        pump->failed = 1; return 0;
    }
    for (attempt = 0; attempt < MAIN_OUTPUT_STREAMS; attempt++) {
        struct main_output_stream *checked = &pump->stream[attempt];
        if (checked->offset > checked->used || checked->used > MAIN_OUTPUT_BUFFER_BYTES || checked->eof > 1u) {
            pump->failed = 1; return 0;
        }
    }
    for (attempt = 0; attempt < MAIN_OUTPUT_STREAMS; attempt++) {
        unsigned int index = (pump->next_write + attempt) % MAIN_OUTPUT_STREAMS;
        struct main_output_stream *stream = &pump->stream[index];
        if (stream->offset < stream->used) {
            main_output_size available = stream->used - stream->offset;
            main_output_count count;
            if (available > MAIN_OUTPUT_QUANTUM) available = MAIN_OUTPUT_QUANTUM;
            count = write_sink(context, stream->bytes + stream->offset, available);
            if (count <= 0 || (main_output_size)count > available) {
                if (main_output_transient(count)) break;
                pump->failed = 1; return 0;
            }
            stream->offset += (main_output_size)count;
            if (stream->offset == stream->used) stream->offset = stream->used = 0;
            pump->next_write = (index + 1u) % MAIN_OUTPUT_STREAMS;
            break;
        }
    }
    for (attempt = 0; attempt < MAIN_OUTPUT_STREAMS; attempt++) {
        unsigned int index = (pump->next_read + attempt) % MAIN_OUTPUT_STREAMS;
        struct main_output_stream *stream = &pump->stream[index];
        main_output_size capacity;
        main_output_count count;
        if (stream->eof) continue;
        if (stream->offset) {
            main_output_size remaining = stream->used - stream->offset, i;
            for (i = 0; i < remaining; i++) stream->bytes[i] = stream->bytes[stream->offset + i];
            stream->offset = 0; stream->used = remaining;
        }
        if (stream->used == MAIN_OUTPUT_BUFFER_BYTES) continue;
        capacity = MAIN_OUTPUT_BUFFER_BYTES - stream->used;
        if (capacity > MAIN_OUTPUT_QUANTUM) capacity = MAIN_OUTPUT_QUANTUM;
        count = read_source(context, index, stream->bytes + stream->used, capacity);
        if (count == 0) {
            stream->eof = 1;
            pump->next_read = (index + 1u) % MAIN_OUTPUT_STREAMS;
            break;
        }
        if (count < 0) {
            if (main_output_transient(count)) continue;
            pump->failed = 1; return 0;
        }
        if ((main_output_size)count > capacity) {
            pump->failed = 1; return 0;
        }
        stream->used += (main_output_size)count;
        pump->next_read = (index + 1u) % MAIN_OUTPUT_STREAMS;
        break;
    }
    return 1;
}

static int main_output_pump_drained(const struct main_output_pump *pump) {
    unsigned int i;
    if (!pump || pump->failed) return 0;
    if (pump->next_read >= MAIN_OUTPUT_STREAMS || pump->next_write >= MAIN_OUTPUT_STREAMS) return 0;
    for (i = 0; i < MAIN_OUTPUT_STREAMS; i++)
        if (pump->stream[i].eof != 1u || pump->stream[i].used > MAIN_OUTPUT_BUFFER_BYTES ||
            pump->stream[i].offset > pump->stream[i].used || pump->stream[i].offset != pump->stream[i].used) return 0;
    return 1;
}

static int main_output_console_drained(const struct main_output_pump *pump,
                                       const struct main_console_queue *queue) {
    return main_output_pump_drained(pump) && main_console_queue_empty(queue);
}

#endif
