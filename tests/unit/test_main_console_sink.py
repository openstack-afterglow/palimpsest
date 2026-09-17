import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _function(source, signature):
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for offset in range(opening, len(source)):
        depth += source[offset] == "{"
        depth -= source[offset] == "}"
        if depth == 0:
            return source[start : offset + 1]
    raise AssertionError(signature)


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    source = (ROOT / "guest/stage1/init.c").read_text()
    functions = "\n".join(
        _function(source, name)
        for name in (
            "static u32 dev_major",
            "static u32 dev_minor",
            "static int same_console_identity",
            "static int valid_kernel_console",
            "static int close_main_console_sink",
            "static int acquire_main_console_sink",
            "static int revalidate_main_console_sink",
        )
    )
    template = (ROOT / "tests/c/main_console_sink_harness.c").read_text()
    generated = tmp_path_factory.mktemp("console-sink") / "harness.c"
    generated.write_text(template.replace("/*PRODUCTION_FUNCTIONS*/", functions))
    output = generated.with_suffix("")
    result = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-I",
            str(ROOT),
            str(generated),
            "-o",
            str(output),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode()
    return output


SCENARIOS = (
    "success",
    "wrong-pid",
    "fstat-error",
    "nofollow-returned-fd",
    "nofollow-wrong-error",
    "reopened-identity-drift",
    "invalid-device-mode",
    "follow-open-error",
    "follow-returned-stdin",
    "follow-returned-stdout",
    "follow-returned-stderr",
    "fcntl-error",
    "original-nonblocking",
    "original-read-write",
    "reopened-read-write",
    "reopened-no-cloexec",
    "revalidation-drift",
    "close-error-detaches",
    "reopened-device-drift",
    "reopened-rdev-drift",
    "reopened-owner-drift",
    "reopened-group-drift",
    "reopened-type-drift",
    "reopened-fstat-error",
    "original-refstat-error",
    "revalidation-held-fstat-error",
    "reopened-getfl-error",
    "reopened-getfd-error",
    "revalidation-original-flags-error",
    "reopened-high-major-drift",
    "reopened-high-minor-drift",
    "already-acquired",
    "original-read-only",
    "reopened-missing-nonblock",
    "revalidation-original-mode-drift",
    "nofollow-returned-stdin",
    "nofollow-returned-stdout",
    "nofollow-returned-stderr",
)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_production_console_sink_faults(harness, scenario):
    scenario_number = SCENARIOS.index(scenario)
    result = subprocess.run([str(harness), str(scenario_number)], timeout=10)
    assert result.returncode == 0


def test_production_diagnostic_routing_actual(tmp_path):
    source = (ROOT / "guest/stage1/init.c").read_text()
    functions = "\n".join(
        _function(source, name)
        for name in (
            "static usize slen",
            "static void write_all_direct",
            "static void write_all(int fd",
        )
    )
    harness_source = tmp_path / "diagnostic-routing.c"
    harness_source.write_text(
        textwrap.dedent(
            r"""
            #include <string.h>
            #include "guest/stage1/main_output_pump.h"
            typedef signed long i64; typedef unsigned long usize;
            #define SYS_write 1
            struct main_console_sink_local { int fd; };
            static struct main_console_sink_local main_console_sink = {.fd = -1};
            static struct main_console_queue main_console_queue;
            static int main_console_queue_active;
            static int main_console_diagnostics_retired;
            static unsigned char direct[64]; static usize direct_used;
            static i64 sc3(i64 call, i64 fd, i64 bytes, i64 size) {
              (void)fd; if (call != SYS_write || size < 0 || direct_used + (usize)size > sizeof(direct)) return -5;
              memcpy(direct + direct_used, (const void *)bytes, (usize)size); direct_used += (usize)size; return size;
            }
            """
        )
        + functions
        + textwrap.dedent(
            r"""
            int main(void) {
              write_all(1, "direct");
              if (direct_used != 6 || memcmp(direct, "direct", 6)) return 1;
              main_console_queue_init(&main_console_queue); main_console_queue_active = 1; main_console_sink.fd = 7;
              write_all(2, "queued");
              if (direct_used != 6 || main_console_queue.used != 6 || memcmp(main_console_queue.bytes, "queued", 6)) return 2;
              main_console_sink.fd = -1; write_all(1, "discarded");
              if (direct_used != 6 || main_console_queue.used != 6) return 3;
              main_console_sink.fd = 7; main_console_diagnostics_retired = 1; write_all(1, "retired");
              if (direct_used != 6 || main_console_queue.used != 6) return 4;
              main_console_diagnostics_retired = 0; main_console_queue.used = MAIN_CONSOLE_QUEUE_BYTES;
              write_all(1, "overflow");
              if (!main_console_queue.failed || main_console_queue_valid(&main_console_queue)) return 5;
              return 0;
            }
            """
        )
    )
    binary = tmp_path / "diagnostic-routing"
    built = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-I",
            str(ROOT),
            str(harness_source),
            "-o",
            str(binary),
        ],
        capture_output=True,
        timeout=20,
    )
    assert built.returncode == 0, built.stderr.decode()
    ran = subprocess.run([str(binary)], capture_output=True, timeout=5)
    assert ran.returncode == 0, ran.stderr.decode()
