"""Portable contracts for the opt-in ML CPU proof."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROOF = Path(__file__).resolve().parents[1] / "kvm" / "test_oci_ml_cpu_live.py"
_SPEC = importlib.util.spec_from_file_location("oci_ml_cpu_live_proof", _PROOF)
assert _SPEC is not None and _SPEC.loader is not None
proof = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proof
_SPEC.loader.exec_module(proof)


def test_live_proof_collects_standalone_without_test_directory_on_pythonpath():
    project = _PROOF.parents[2]
    environment = {
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/nonexistent-untrusted-pythonpath",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", str(_PROOF)],
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4096:]
    assert result.stdout.count(b"test_official_ml_image_cpu_tensor_with_public_command_override") == 2


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


def test_setup_leaves_runtime_creation_to_init_runtime_and_keeps_evidence_private(
    monkeypatch: pytest.MonkeyPatch,
):
    temporary = tempfile.TemporaryDirectory(prefix="pml-", dir="/tmp")
    root = Path(temporary.name).resolve(strict=True)
    os.chmod(root, 0o711)
    observations: list[Path] = []
    measured: list[Path] = []

    def cli(_environment, *arguments, **_kwargs):
        assert arguments[:2] == ("oci", "init-runtime")
        parent = arguments[2]
        observations.append(parent)
        assert not parent.exists()
        parent.mkdir(mode=0o711)
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(proof.legacy, "_cli", cli)
    monkeypatch.setattr(
        proof,
        "resolve_roots",
        lambda environment: SimpleNamespace(
            runs=Path(environment["PALIMPSEST_STATE_HOME"]) / "runs",
            oci_root_volumes=Path(environment["PALIMPSEST_STATE_HOME"]) / "root-volumes",
        ),
    )
    try:
        parent, environment = proof._setup({"PALIMPSEST_OCI_ML_PROOF_ROOT": str(root)}, "m-t-12345678")
        measured.append(Path(environment["PALIMPSEST_STATE_HOME"]) / "runs" / "m-t-12345678" / "io" / "lifecycle.sock")

        assert observations == [parent]
        assert parent.parent == root and parent.name.startswith("m-")
        assert parent.stat().st_mode & 0o777 == 0o711
        evidence = Path(environment["PALIMPSEST_PROOF_EVIDENCE_DIR"])
        assert evidence.parent == parent and evidence.stat().st_mode & 0o777 == 0o700
        assert len(os.fsencode(measured[0])) <= 97
        roots = proof.resolve_roots(environment)
        assert proof._fresh_root_volume_baseline(roots) == set()
        roots.oci_root_volumes.mkdir(parents=True)
        with pytest.raises(AssertionError):
            proof._fresh_root_volume_baseline(roots)
        roots.oci_root_volumes.rmdir()
        roots.oci_root_volumes.symlink_to(parent / "missing-root-volumes")
        with pytest.raises(AssertionError):
            proof._fresh_root_volume_baseline(roots)
    finally:
        temporary.cleanup()
