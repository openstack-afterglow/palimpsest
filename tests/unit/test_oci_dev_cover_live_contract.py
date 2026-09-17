"""Portable source contract for the opt-in populated-image /dev proof."""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_PROOF_PATH = ROOT / "tests/kvm/test_oci_dev_cover_live.py"
_PACKAGE_NAME = "dev_cover_live_contract_fixture"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_PROOF_PATH.parent)]
sys.modules[_PACKAGE_NAME] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.proof", _PROOF_PATH)
assert _SPEC is not None and _SPEC.loader is not None
proof = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proof
_SPEC.loader.exec_module(proof)
_build_probe_initramfs = proof._build_probe_initramfs


def test_native_dev_cover_initramfs_is_constructible_without_opt_in() -> None:
    archive = _build_probe_initramfs(b"standalone-probe")
    assert archive.startswith(b"070701")
    assert b"image-marker" not in archive
    assert b"image-child" not in archive
    assert archive.endswith(b"\0" * 4)
