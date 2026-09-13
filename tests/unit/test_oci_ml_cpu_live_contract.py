"""Portable contracts for the opt-in ML CPU proof."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_PROOF = Path(__file__).resolve().parents[1] / "kvm" / "test_oci_ml_cpu_live.py"
sys.path.insert(0, str(_PROOF.parent))
_SPEC = importlib.util.spec_from_file_location("oci_ml_cpu_live_proof", _PROOF)
assert _SPEC is not None and _SPEC.loader is not None
proof = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proof
_SPEC.loader.exec_module(proof)


def test_each_framework_requires_independent_complete_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"archive")
    for case in proof.CASES:
        stem = proof._PREFIX + case.key + "_"
        with pytest.raises(pytest.skip.Exception):
            proof._selection(case)
        environment = {
            stem + "LIVE": "1",
            stem + "IMAGE": str(archive),
            stem + "ARCHIVE_SHA256": "sha256:" + hashlib.sha256(b"archive").hexdigest(),
            stem + "MANIFEST_SHA256": case.manifest_digest,
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        assert proof._selection(case).archive == archive
        monkeypatch.setenv(stem + "MANIFEST_SHA256", "sha256:" + "a" * 64)
        with pytest.raises(AssertionError):
            proof._selection(case)
        for key in environment:
            monkeypatch.delenv(key)


def test_proof_uses_original_archive_public_override_and_bounded_cpu_resources():
    source = _PROOF.read_text()
    assert '"--memory",\n                "8192"' in source
    assert '"--vcpus",\n                "2"' in source
    assert '"--network",\n                "none"' in source
    assert proof._KEEPALIVE == ("/bin/sleep", "infinity")
    assert "_assert_override_provenance" in source
    assert "_PRIVATE_DISK_BUDGET = 40 * 1024 * 1024 * 1024" in source
    assert "set_num_threads(1)" in source and "set_intra_op_parallelism_threads(1)" in source
    assert "[[1,2],[3,4]]" in source and "[[5,6],[7,8]]" in source
    assert "134" in source and "19, 22, 43, 50" in source
    assert "torch.cuda.is_available()" in source and "list_physical_devices('GPU')" in source
    assert "DeviceSpec.from_string(c.device)" in source
    assert "d.device_type=='CPU' and d.device_index==0" in source
    assert "'CPU:0'" in source
    assert "/device:CPU:0" not in source
    assert "docker pull" not in source and "skopeo" not in source
    assert "--gpus" not in source and "vfio" not in source.lower()
    assert 'findall("./devices/hostdev") == []' in source
    assert 'findall("./devices/filesystem") == []' in source
    assert "assert not root_volume_path.exists()" in source
    assert "assert not (roots.runs / name).exists()" in source
    assert "derived" not in source.lower()
