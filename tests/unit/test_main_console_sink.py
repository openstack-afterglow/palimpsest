import subprocess
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
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(generated), "-o", str(output)],
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
