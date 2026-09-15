import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _function(source: str, signature: str) -> str:
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
    functions = "\n\n".join(
        (
            _function(source, "static main_output_count write_main_console_nonblocking"),
            _function(source, "static void flush_main_console_tick"),
            _function(source, "static int service_terminal_console_tick"),
        )
    )
    template = (ROOT / "tests/c/main_console_lifecycle_harness.c").read_text()
    directory = tmp_path_factory.mktemp("main-console-lifecycle")
    c_file, binary = directory / "harness.c", directory / "harness"
    c_file.write_text(template.replace("/* PRODUCTION_FUNCTIONS */", functions))
    # The extracted component intentionally includes only the queue half of the
    # shared header; production's full-source compile retains unused warnings.
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
            str(c_file),
            "-o",
            str(binary),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    return binary


@pytest.mark.parametrize("scenario", range(5))
def test_terminal_console_tick_actual_partial_and_failure_paths(harness, scenario):
    result = subprocess.run([str(harness), str(scenario)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_terminal_lifecycle_retains_sink_and_signed_boundary_cannot_use_diagnostic_discard():
    source = (ROOT / "guest/stage1/init.c").read_text()
    signed = _function(source, "static int write_signed_message")
    terminal = _function(source, "static __attribute__((noreturn)) void service_terminal_lifecycle")
    supervise = _function(source, "static int supervise_workload")
    assert "main_console_enqueue_diagnostic" in signed and "main_console_boundary_deadline" in signed
    assert "queued = main_console_queue_active && main_console_sink.fd >= 0" in signed
    assert "queued && main_console_diagnostics_retired" in signed
    assert supervise.index("main_console_diagnostics_retired = 1") < supervise.index("LIFECYCLE_TERMINAL")
    assert "close_main_console_sink" not in supervise[supervise.index("main_console_diagnostics_retired = 1") :]
    assert "service_terminal_console_tick()" in terminal
    failed = terminal[terminal.index("failed:") :]
    assert failed.index("wipe_lifecycle_secret") < failed.index("close_main_console_sink")


def test_control_deadline_uses_active_phase_absolute_cap(tmp_path):
    source = (ROOT / "guest/stage1/init.c").read_text()
    helper = _function(source, "static u64 cap_control_deadline")
    c_file, binary = tmp_path / "deadline.c", tmp_path / "deadline"
    c_file.write_text(
        """
typedef unsigned long long u64;
static int main_console_diagnostics_retired;
static u64 main_workload_stop_deadline;
static u64 main_workload_cleanup_deadline;
"""
        + helper
        + """
int main(void) {
    if (cap_control_deadline(5000) != 5000) return 1;
    main_workload_stop_deadline = 100;
    if (cap_control_deadline(5000) != 100) return 2;
    main_workload_cleanup_deadline = 50;
    if (cap_control_deadline(5000) != 50) return 3;
    main_workload_stop_deadline = 10;
    if (cap_control_deadline(5000) != 50) return 4;
    main_workload_cleanup_deadline = 0;
    main_console_diagnostics_retired = 1;
    if (cap_control_deadline(5000) != 5000) return 5;
    return 0;
}
"""
    )
    result = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(c_file), "-o", str(binary)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    result = subprocess.run([str(binary)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
