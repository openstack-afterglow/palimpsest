"""Execute the exact production main-console-sink callsite fragments."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _block(source: str, anchor: str) -> str:
    start = source.index(anchor)
    opening = source.index("{", start)
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    raise AssertionError(f"unterminated production block: {anchor}")


def _function(source: str, signature: str) -> str:
    return _block(source, signature)


def _line(source: str, text: str) -> str:
    matches = [line.strip() for line in source.splitlines() if line.strip() == text]
    assert matches == [text]
    return text


@pytest.fixture(scope="module")
def callsite_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repository = Path(__file__).resolve().parents[2]
    production = (repository / "guest/stage1/init.c").read_text(encoding="utf-8")
    template = (repository / "tests/c/main_console_sink_callsites.c").read_text(encoding="utf-8")
    supervise = _function(production, "static int supervise_workload")
    exec_function = _function(production, "static __attribute__((noreturn)) void exec_child")
    start = _function(production, "static __attribute__((noreturn, used)) void start_c")
    main_child_block = _block(supervise, "if (main_pid == 0) {")
    main_child = _line(
        main_child_block,
        "if (!close_main_console_sink()) child_fail(error_pipe[1], 42, EIO);",
    ).replace("error_pipe[1]", "error_fd")
    exec_child = _line(exec_function, "if (!close_main_console_sink()) child_fail(error, 42, EIO);")
    parent_close = _block(
        supervise,
        "if (!main_console_queue_empty(&main_console_queue) || !revalidate_main_console_sink()) {",
    )
    assert main_child_block.index("if (!close_main_console_sink()) child_fail") < main_child_block.index(
        "prepare_workload_isolation(process"
    )
    assert exec_function.index("if (!close_main_console_sink()) child_fail") < exec_function.index("SYS_dup3")
    assert supervise.index("quiesce_terminal_root()") < supervise.index(parent_close)
    assert supervise.index("workload_terminal(result)") < supervise.index(parent_close)
    assert supervise.index(parent_close) < supervise.index("main_console_diagnostics_retired = 1")
    assert supervise.index(parent_close) < supervise.index("lifecycle->state = LIFECYCLE_TERMINAL")
    revalidation_failure = _block(start, "if (!revalidate_main_console_sink()) {")
    lifecycle_failure = _block(start, "if (!prepare_lifecycle(&lifecycle)) {")
    assert "(void)close_main_console_sink();" in revalidation_failure
    assert revalidation_failure.index("close_main_console_sink") < revalidation_failure.index("wait_closed")
    assert "(void)close_main_console_sink();" not in lifecycle_failure
    assert lifecycle_failure.index("lifecycle_rejected") < lifecycle_failure.index("wait_closed")
    generated = (
        template.replace("/* MAIN_CHILD_CLOSE */", main_child)
        .replace("/* EXEC_CHILD_CLOSE */", exec_child)
        .replace("/* PARENT_TERMINAL_CLOSE */", parent_close)
        .replace("/* EARLY_START_CLOSE */", "(void)code;")
    )
    directory = tmp_path_factory.mktemp("main-console-sink-callsites")
    source = directory / "callsites.c"
    binary = directory / "callsites"
    source.write_text(generated, encoding="utf-8")
    completed = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(binary),
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return binary


@pytest.mark.parametrize(
    "scenario",
    range(6),
    ids=(
        "main-child-close-first",
        "main-child-close-failure",
        "exec-child-close-first",
        "exec-child-close-failure",
        "terminal-close-failure",
        "terminal-close-first",
    ),
)
def test_production_sink_callsite(scenario: int, callsite_harness: Path) -> None:
    completed = subprocess.run(
        [str(callsite_harness), str(scenario)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, f"scenario {scenario}: stdout={completed.stdout!r} stderr={completed.stderr!r}"
