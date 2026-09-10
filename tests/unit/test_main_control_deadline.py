from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


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
    raise AssertionError(f"unterminated function: {signature}")


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    production = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    deadlines = "\n\n".join(
        (
            _function(production, "static u64 cap_control_deadline"),
            _function(production, "static int control_read_deadline_status"),
        )
    )
    read_frame = _function(production, "static int read_control_frame").replace(
        "read_control_frame", "read_control_frame_actual", 1
    )
    pump = _function(production, "static int lifecycle_pump")
    assert "for (frames = 0; frames < 64; frames++)" in pump
    assert "if (n == -EINTR) return 0;" in read_frame
    template = (ROOT / "tests/c/main_control_deadline_harness.c").read_text(encoding="utf-8")
    generated = (
        template.replace("/* DEADLINE_FUNCTIONS */", deadlines)
        .replace("/* READ_CONTROL_FUNCTION */", read_frame)
        .replace("/* LIFECYCLE_PUMP_FUNCTION */", pump)
    )
    directory = tmp_path_factory.mktemp("main-control-deadline")
    source = directory / "harness.c"
    binary = directory / "harness"
    source.write_text(generated, encoding="utf-8")
    result = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.parametrize(
    "scenario",
    [
        "eintr-empty",
        "eintr-partial",
        "slice-empty",
        "slice-partial",
        "active-first-partial",
        "protocol-expired",
        "pump-yields-64",
        "clock-zero",
    ],
)
def test_actual_control_deadline_paths(harness: Path, scenario: str) -> None:
    result = subprocess.run([str(harness), scenario], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, result.stderr


def test_previous_conflated_deadline_reproduces_native_failure(tmp_path: Path) -> None:
    production = (ROOT / "guest/stage1/init.c").read_text(encoding="utf-8")
    deadlines = _function(production, "static u64 cap_control_deadline")
    read_frame = _function(production, "static int read_control_frame").replace(
        "read_control_frame", "read_control_frame_actual", 1
    )
    corrected = """int deadline_status = control_read_deadline_status(
            session->frame_deadline, monotonic_millis());
        if (deadline_status <= 0) return deadline_status;"""
    conflated = """u64 effective_deadline = cap_control_deadline(session->frame_deadline);
        if (effective_deadline) {
            u64 now = monotonic_millis();
            if (!now || now >= effective_deadline) return -1;
        }"""
    assert corrected in read_frame
    read_frame = read_frame.replace(corrected, conflated).replace(
        "session->frame_deadline = now + 5000;",
        "session->frame_deadline = cap_control_deadline(now + 5000);",
    )
    pump = _function(production, "static int lifecycle_pump")
    template = (ROOT / "tests/c/main_control_deadline_harness.c").read_text(encoding="utf-8")
    generated = (
        template.replace("/* DEADLINE_FUNCTIONS */", deadlines)
        .replace("/* READ_CONTROL_FUNCTION */", read_frame)
        .replace("/* LIFECYCLE_PUMP_FUNCTION */", pump)
    )
    source, binary = tmp_path / "legacy.c", tmp_path / "legacy"
    source.write_text(generated, encoding="utf-8")
    compiled = subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic", str(source), "-o", str(binary)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert compiled.returncode == 0, compiled.stderr
    reproduced = subprocess.run(
        [str(binary), "slice-empty"], capture_output=True, text=True, check=False, timeout=10
    )
    assert reproduced.returncode != 0
    assert "slice-empty failed" in reproduced.stderr
