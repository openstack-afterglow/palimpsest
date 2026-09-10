from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = "queued-echild drained-early alive-five-seconds shared-deadline undrained output-error cancel-error lifecycle-error poll-error clock-error main-status".split()


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for offset in range(opening, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    raise AssertionError("unterminated production function")


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    production = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    function = _function(production, "static int terminate_and_reap")
    record = _function(production, "static void record_reaped_child")
    assert "SYS_wait4, -1, (i64)&status, WNOHANG" in function
    assert "SYS_wait4, -1, (i64)&status, 0, 0" not in function
    assert "main_workload_stop_deadline ? main_workload_stop_deadline" in function
    assert "now + 1000" in function
    template = (ROOT / "tests/c/main_output_terminate_harness.c").read_text(encoding="utf-8")
    generated = template.replace("/* RECORD_REAPED_FUNCTION */", record).replace(
        "/* TERMINATE_FUNCTION */", function
    )
    directory = tmp_path_factory.mktemp("main-output-terminate")
    source = directory / "harness.c"
    binary = directory / "harness"
    source.write_text(generated, encoding="utf-8")
    result = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic", str(source), "-o", str(binary)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_actual_terminate_and_reap(harness: Path, scenario: str) -> None:
    result = subprocess.run([str(harness), scenario], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
