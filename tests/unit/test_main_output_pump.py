import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = "partial fair source-eagain source-eintr full-buffer compact-refill eof sink-eagain sink-eintr sink-bad sink-zero sink-large read-bad read-large invalid real-pipe console-queue console-queue-failures console-queue-order-capacity console-queue-real-pipe combined-drained".split()


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    output = tmp_path_factory.mktemp("main-output-pump") / "harness"
    result = subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-I",
            str(ROOT),
            str(ROOT / "tests/c/main_output_pump_harness.c"),
            "-o",
            str(output),
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return output


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_main_output_pump_scenario(harness, scenario):
    result = subprocess.run([str(harness), scenario], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""


def test_component_is_integrated_into_production_stage1():
    source = (ROOT / "guest/stage1/init.c").read_text()
    assert '#include "main_output_pump.h"' in source
    assert "main_output_pump_tick" in source
    assert "main_console_enqueue_data" in source
    assert "main_console_flush_tick" in source
    assert "main_output_console_drained" in source
