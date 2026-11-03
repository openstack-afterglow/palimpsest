from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _compile_probe(tmp_path: Path) -> Path:
    """Compile the production executable, never a replacement GPU implementation."""
    if sys.platform not in {"linux", "darwin"}:
        pytest.skip("CUDA probe requires a POSIX compiler and dynamic loader")
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if compiler is None:
        pytest.skip("no local C compiler available")
    executable = tmp_path / "cuda-probe"
    compiled = subprocess.run(
        [
            compiler,
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            str(ROOT / "guest/nova/cuda_probe.c"),
            "-o",
            str(executable),
            *(["-ldl"] if sys.platform == "linux" else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    return executable


def _assert_refusal(executable: Path, error: str) -> None:
    result = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        check=False,
        timeout=35,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    assert len(result.stdout) < 2048
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout) == {"success": False, "error": error}


def test_compiled_probe_refuses_a_missing_real_cuda_driver(tmp_path: Path) -> None:
    try:
        ctypes.CDLL("libcuda.so.1")
    except OSError:
        pass
    else:
        pytest.skip("a real CUDA driver is available; this test requires its actual absence")
    _assert_refusal(_compile_probe(tmp_path), "CUDA driver unavailable")


def test_compiled_probe_refuses_missing_original_image_libraries(tmp_path: Path) -> None:
    """A real loader failure must not be accepted as image CUDA qualification."""
    if sys.platform != "linux":
        pytest.skip("original-image CUDA libraries are Linux shared objects")
    try:
        driver = ctypes.CDLL("libcuda.so.1")
    except OSError:
        pytest.skip("a real driver library is required to reach the image-library refusal")
    libraries = (
        ("libcudart.so.12", "CUDA image runtime unavailable"),
        ("libcublasLt.so.12", "CUDA image cuBLASLt unavailable"),
        ("libcublas.so.12", "CUDA image cuBLAS unavailable"),
    )
    loaded = [driver]
    for filename, error in libraries:
        try:
            loaded.append(ctypes.CDLL(f"/usr/local/cuda/lib64/{filename}"))
        except OSError:
            _assert_refusal(_compile_probe(tmp_path), error)
            return
    pytest.skip("all original-image CUDA libraries are available; no actual absence to test")
