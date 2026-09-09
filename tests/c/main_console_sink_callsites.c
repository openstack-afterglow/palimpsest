#include <setjmp.h>
#include <stdlib.h>
#include <string.h>

#define EIO 5

typedef signed long i64;
typedef unsigned int u32;

struct child_error_local {
    u32 stage;
    u32 error;
};

struct workload_agent {
    int unused;
};

struct exec_session {
    int unused;
};

static int close_result;
static int close_calls;
static int child_failure_stage;
static int workload_failure_stage;
static int event_number;
static int close_event;
static int isolation_event;
static int duplicate_event;
static int quiesce_event;
static int terminal_event;
static jmp_buf child_failure;

static int close_main_console_sink(void) {
    close_calls++;
    close_event = ++event_number;
    return close_result;
}

static __attribute__((noreturn)) void child_fail(int fd, u32 stage, i64 error) {
    (void)fd;
    (void)error;
    child_failure_stage = (int)stage;
    longjmp(child_failure, 1);
}

static void set_workload_failure(struct child_error_local *failure, u32 stage,
                                 i64 error) {
    failure->stage = stage;
    failure->error = (u32)error;
    workload_failure_stage = (int)stage;
}

static void close_workload_agent(struct workload_agent *agent,
                                 struct exec_session *session) {
    (void)agent;
    (void)session;
}

static i64 sc1(i64 call, i64 argument) {
    (void)call;
    (void)argument;
    return 0;
}

static i64 sc4(i64 call, i64 first, i64 second, i64 third, i64 fourth) {
    (void)call;
    (void)first;
    (void)second;
    (void)third;
    (void)fourth;
    return 0;
}

#define SYS_close 3
#define SYS_rt_sigprocmask 14
#undef SIG_SETMASK
#define SIG_SETMASK 2

static int main_child_path(int error_fd) {
/* MAIN_CHILD_CLOSE */
    isolation_event = ++event_number;
    return 1;
}

static int exec_child_path(int error) {
/* EXEC_CHILD_CLOSE */
    duplicate_event = ++event_number;
    return 1;
}

static int normal_terminal_path(void) {
    struct child_error_local local_failure = {0, 0};
    struct child_error_local *failure = &local_failure;
    struct workload_agent agent = {0};
    struct exec_session session = {0};
    i64 signal_fd = 9;
    i64 empty_mask = 0;
/* PARENT_TERMINAL_CLOSE */
    quiesce_event = ++event_number;
    terminal_event = ++event_number;
    return 1;
}

static int early_start_path(int code) {
/* EARLY_START_CLOSE */
    return close_calls;
}

static void reset_state(int result) {
    close_result = result;
    close_calls = 0;
    child_failure_stage = 0;
    workload_failure_stage = 0;
    event_number = 0;
    close_event = 0;
    isolation_event = 0;
    duplicate_event = 0;
    quiesce_event = 0;
    terminal_event = 0;
}

static int child_scenario(int main_child, int result) {
    int returned;
    reset_state(result);
    if (setjmp(child_failure)) {
        return result == 0 && close_calls == 1 && child_failure_stage == 42 &&
               isolation_event == 0 && duplicate_event == 0
                   ? 0
                   : 1;
    }
    returned = main_child ? main_child_path(17) : exec_child_path(18);
    if (!result || !returned || close_calls != 1 || close_event != 1)
        return 2;
    if (main_child)
        return isolation_event == 2 && duplicate_event == 0 ? 0 : 3;
    return duplicate_event == 2 && isolation_event == 0 ? 0 : 4;
}

int main(int argc, char **argv) {
    int scenario;
    if (argc != 2) return 90;
    scenario = atoi(argv[1]);
    if (scenario == 0) return child_scenario(1, 1);
    if (scenario == 1) return child_scenario(1, 0);
    if (scenario == 2) return child_scenario(0, 1);
    if (scenario == 3) return child_scenario(0, 0);
    if (scenario == 4) {
        reset_state(0);
        return normal_terminal_path() == -1 && close_calls == 1 &&
                       child_failure_stage == 0 && workload_failure_stage == 42 &&
                       quiesce_event == 0 && terminal_event == 0
                   ? 0
                   : 5;
    }
    if (scenario == 5) {
        reset_state(1);
        return normal_terminal_path() == 1 && close_event == 1 &&
                       quiesce_event == 2 && terminal_event == 3
                   ? 0
                   : 6;
    }
    if (scenario == 6) {
        reset_state(1);
        return early_start_path(7) == 1 ? 0 : 7;
    }
    if (scenario == 7) {
        reset_state(1);
        return early_start_path(0) == 0 ? 0 : 8;
    }
    return 91;
}
