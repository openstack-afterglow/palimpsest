import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    root = Path(__file__).resolve().parents[2]
    tests = sorted((root / "hub" / "tests" / "js").glob("*.test.mjs"))
    assert tests, "No dashboard JavaScript tests found"
    result = subprocess.run(
        [node, "--test", *(str(path) for path in tests)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
