"""Portable contracts for the opt-in ML CPU proof."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
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


@pytest.mark.parametrize(
    "phase,returncode",
    [
        ("public-run-command", 17),
        ("root-proof", None),
        ("framework-exec-command", 23),
        ("cleanup-assertion", None),
    ],
)
def test_phase_receipt_records_fixed_failure_boundary(tmp_path: Path, phase: str, returncode: int | None):
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    state: list[object] = [phase, returncode]

    with pytest.raises(RuntimeError, match="original failure"):
        try:
            raise RuntimeError("original failure")
        except BaseException:
            proof._record_phase(evidence, "tensorflow", state, phase, "failed", returncode)
            raise

    receipt_path = evidence / "ml-phase.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt == {
        "framework": "tensorflow",
        "phase": phase,
        "returncode": returncode,
        "schema": "palimpsest.oci-ml-phase.v1",
        "status": "failed",
    }
    info = receipt_path.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1


@pytest.mark.parametrize("phase", ["public-run-command", "framework-exec-command", "stop", "remove"])
def test_nonzero_command_keeps_exact_phase_and_returncode_for_failure_receipt(tmp_path: Path, phase: str):
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    state: list[object] = ["setup", None]
    result = subprocess.CompletedProcess([], 19, b"private stdout", b"private stderr")

    with pytest.raises(AssertionError):
        try:
            proof._require_success(state, phase, result)
        except BaseException:
            proof._record_phase(evidence, "pytorch", state, str(state[0]), "failed", state[1])
            raise

    receipt = json.loads((evidence / "ml-phase.json").read_text())
    assert receipt["phase"] == phase and receipt["status"] == "failed" and receipt["returncode"] == 19
    assert "private" not in json.dumps(receipt)


def test_live_proof_wires_each_diagnostic_boundary_without_changing_success_assertions():
    source = _PROOF.read_text()
    for phase in (
        "public-run-command",
        "public-run-assertion",
        "provenance",
        "root-volume",
        "root-proof",
        "domain-check",
        "framework-exec-command",
        "framework-exec-assertion",
        "root-identity",
        "pid1-refusal",
        "root-proof-after",
        "stop",
        "remove",
        "cleanup-assertion",
        "source-hash-assertion",
        "source-preservation",
    ):
        assert f'"{phase}"' in source
    assert "legacy._success" in source and "case.output.fullmatch(calculation.stdout)" in source


def test_phase_receipt_write_failure_never_replaces_original_exception(tmp_path: Path, monkeypatch):
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    state: list[object] = ["root-proof", None]
    monkeypatch.setattr(proof, "_write_phase_receipt", lambda *_args: (_ for _ in ()).throw(OSError("secret")))

    with pytest.raises(LookupError, match="original"):
        try:
            raise LookupError("original")
        except BaseException:
            proof._record_phase(evidence, "pytorch", state, "root-proof", "failed")
            raise
    assert not (evidence / "ml-phase.json").exists()


def test_phase_receipt_rejects_untyped_or_unbounded_values(tmp_path: Path):
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    for phase, status, returncode in (
        ("unknown", "failed", None),
        ("root-proof", "raw", None),
        ("root-proof", "failed", True),
        ("root-proof", "failed", 256),
    ):
        with pytest.raises(AssertionError, match="invalid ML phase"):
            proof._write_phase_receipt(evidence, "tensorflow", phase, status, returncode)


@pytest.mark.parametrize("tamper", [None, "missing", "extra", "wrong-uuid", "directory", "symlink"])
def test_root_volume_proof_binds_exact_raw_and_record_files(tmp_path: Path, tamper: str | None):
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    volume_id = "12345678-1234-1234-1234-123456789abc"
    stem = volume_id.replace("-", "")
    raw = volumes / f"{stem}.raw"
    record = volumes / f"{stem}.json"
    raw.write_bytes(b"1234")
    record.write_bytes(b"{}")
    if tamper == "missing":
        record.unlink()
    elif tamper == "extra":
        (volumes / "extra").write_bytes(b"")
    elif tamper == "wrong-uuid":
        raw.rename(volumes / ("0" * 32 + ".raw"))
    elif tamper == "directory":
        raw.unlink()
        raw.mkdir()
    elif tamper == "symlink":
        raw.unlink()
        raw.symlink_to(record)
    roots = SimpleNamespace(oci_root_volumes=volumes)
    transaction = SimpleNamespace(volume_id=volume_id, volume_size_bytes=4)

    if tamper is None:
        assert proof._assert_new_root_volume_files(roots, set(), transaction) == (raw, record)
    else:
        with pytest.raises(AssertionError):
            proof._assert_new_root_volume_files(roots, set(), transaction)


@pytest.mark.parametrize(
    "failure,expected_phase,expected_rc,receipt_write_failure",
    [
        ("public-run", "public-run-command", 19, False),
        ("public-run", "public-run-command", 19, True),
        ("root-proof", "root-proof", None, False),
        ("tensor", "framework-exec-command", 19, False),
        ("cleanup", "cleanup-assertion", None, False),
    ],
)
def test_real_proof_failure_injection_writes_exact_phase_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_phase: str,
    expected_rc: int | None,
    receipt_write_failure: bool,
):
    parent = tmp_path / "runtime"
    evidence = parent / "setup-evidence"
    evidence.mkdir(parents=True, mode=0o700)
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"image")
    digest = "sha256:" + hashlib.sha256(b"image").hexdigest()
    roots = SimpleNamespace(runs=tmp_path / "runs", oci_root_volumes=tmp_path / "volumes")
    roots.runs.mkdir()
    roots.oci_root_volumes.mkdir()
    transaction = SimpleNamespace(volume_id="12345678-1234-1234-1234-123456789abc", volume_size_bytes=4)
    case = proof.CASES[0]
    selected_name = [""]

    monkeypatch.setattr(
        proof,
        "_selection",
        lambda _case: SimpleNamespace(archive=archive, archive_digest=digest, manifest_digest=case.manifest_digest),
    )
    monkeypatch.setattr(
        proof,
        "_setup",
        lambda _environment, _name: (parent, {"PALIMPSEST_PROOF_EVIDENCE_DIR": str(evidence)}),
    )
    monkeypatch.setattr(proof, "resolve_roots", lambda _environment: roots)
    monkeypatch.setattr(proof, "_fresh_root_volume_baseline", lambda _roots: set())
    monkeypatch.setattr(proof.shutil, "disk_usage", lambda _path: SimpleNamespace(free=proof._PRIVATE_DISK_BUDGET))
    monkeypatch.setattr(proof, "_assert_override_provenance", lambda *_args: transaction)
    monkeypatch.setattr(
        proof,
        "_assert_new_root_volume_files",
        lambda *_args: (tmp_path / "absent.raw", tmp_path / "absent.json"),
    )
    monkeypatch.setattr(proof, "_assert_cpu_only_domain", lambda *_args: None)
    monkeypatch.setattr(proof.legacy, "_environment", lambda: {})
    monkeypatch.setattr(proof.legacy, "_file_sha256", lambda _path: digest)
    monkeypatch.setattr(proof.legacy, "_authenticate", lambda *_args: SimpleNamespace(argv=("/bin/bash",)))
    monkeypatch.setattr(proof.legacy, "_save", lambda _parent, _label, result: result)
    monkeypatch.setattr(proof.legacy, "_record_source_hashes", lambda *_args: None)
    if receipt_write_failure:
        monkeypatch.setattr(
            proof,
            "_write_phase_receipt",
            lambda *_args: (_ for _ in ()).throw(OSError("private receipt failure")),
        )

    def cli(_environment, *args, **_kwargs):
        command = args[0]
        if command == "run":
            selected_name[0] = args[args.index("--name") + 1]
            return subprocess.CompletedProcess(
                args, 19 if failure == "public-run" else 0, (selected_name[0] + "\n").encode(), b""
            )
        if command == "exec" and case.python in args:
            output = b"" if failure == "tensor" else b"ML_OK tensorflow 2.21.0 [19, 22, 43, 50] 134 CPU:0 []\n"
            return subprocess.CompletedProcess(args, 19 if failure == "tensor" else 0, output, b"")
        if command == "exec" and "stat -c '%d %i' /" in args:
            return subprocess.CompletedProcess(args, 0, b"1 2\n", b"")
        if command == "exec":
            return subprocess.CompletedProcess(args, 1, b"", b"Permission denied")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    root_calls = [0]

    def root_proof(*_args):
        root_calls[0] += 1
        if failure == "root-proof" and root_calls[0] == 1:
            raise LookupError("injected")
        return {"domain": {"uuid": "00000000-0000-0000-0000-000000000001"}, "root_identity": {"device": 1, "inode": 2}}

    monkeypatch.setattr(proof.legacy, "_cli", cli)
    monkeypatch.setattr(proof.legacy, "_root_proof", root_proof)
    monkeypatch.setattr(
        proof.legacy,
        "_assert_domain_absent",
        lambda *_args: (_ for _ in ()).throw(LookupError("injected")) if failure == "cleanup" else None,
    )

    with pytest.raises((AssertionError, LookupError)):
        proof._proof(case)

    if receipt_write_failure:
        assert not (evidence / "ml-phase.json").exists()
        return
    receipt = json.loads((evidence / "ml-phase.json").read_text())
    assert receipt["phase"] == expected_phase
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == expected_rc
