"""Opt-in CPU tensor proofs for pinned official ML images and public command overrides."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from palimpsest_local.oci_process import OCIProcessSpec
from palimpsest_local.oci_root_prepare import OCIRootPreparationTransaction
from palimpsest_local.state import read_run_ledger_snapshot, resolve_roots

_LEGACY_PATH = Path(__file__).with_name("test_oci_docker_hub_cli_live.py")
_SPEC = importlib.util.spec_from_file_location("palimpsest_ml_legacy_helpers", _LEGACY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
legacy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = legacy
_SPEC.loader.exec_module(legacy)

_PREFIX = "PALIMPSEST_OCI_ML_"
_KEEPALIVE = ("/bin/sleep", "infinity")
_PRIVATE_DISK_BUDGET = 40 * 1024 * 1024 * 1024
pytestmark = pytest.mark.kvm


@dataclass(frozen=True)
class MLCase:
    key: str
    original_reference: str
    manifest_digest: str
    python: str
    program: str
    output: re.Pattern[bytes]


CASES = (
    MLCase(
        "TENSORFLOW",
        "docker.io/tensorflow/tensorflow:2.21.0",
        "sha256:f325279f01a3e742a1285d8d736b7e600f72ccf8b55cc19ee0a90b8cbfce4c7a",
        "/usr/local/bin/python",
        "import tensorflow as f; f.config.threading.set_intra_op_parallelism_threads(1); f.config.threading.set_inter_op_parallelism_threads(1); a=f.constant([[1,2],[3,4]]); b=f.constant([[5,6],[7,8]]); c=f.matmul(a,b); d=f.DeviceSpec.from_string(c.device); assert d.device_type=='CPU' and d.device_index==0; print('ML_OK tensorflow',f.__version__,c.numpy().reshape(-1).tolist(),int(f.reduce_sum(c).numpy()),'CPU:0',f.config.list_physical_devices('GPU'))",
        re.compile(rb"^ML_OK tensorflow 2\.21\.0 \[19, 22, 43, 50\] 134 CPU:0 \[\]\n$"),
    ),
    MLCase(
        "PYTORCH",
        "docker.io/pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime",
        "sha256:dab81780fd94483b67b4b5679cc0024939b08e48540d39476d284cb29002ed69",
        "/opt/conda/bin/python",
        "import torch; torch.set_num_threads(1); torch.set_num_interop_threads(1); a=torch.tensor([[1,2],[3,4]]); b=torch.tensor([[5,6],[7,8]]); c=a@b; print('ML_OK pytorch',torch.__version__,c.reshape(-1).tolist(),int(c.sum()),c.device,torch.cuda.is_available())",
        re.compile(rb"^ML_OK pytorch 2\.8\.0(?:\+cu126)? \[19, 22, 43, 50\] 134 cpu False\n$"),
    ),
)


def _selection(case: MLCase):
    stem = _PREFIX + case.key + "_"
    if os.environ.get(stem + "LIVE") != "1":
        pytest.skip(f"set {stem}LIVE=1 for the independent CPU tensor proof")
    archive = Path(os.environ.get(stem + "IMAGE", ""))
    archive_digest = os.environ.get(stem + "ARCHIVE_SHA256", "")
    manifest = os.environ.get(stem + "MANIFEST_SHA256", "")
    assert archive.is_absolute() and archive.resolve(strict=True).is_file()
    assert legacy._DIGEST.fullmatch(archive_digest) and legacy._file_sha256(archive) == archive_digest
    assert manifest == case.manifest_digest
    return legacy.DockerHubImageSelection(archive.resolve(), archive_digest, manifest)


def _assert_cpu_only_domain(environment: dict[str, str], name: str) -> None:
    virsh = legacy.shutil.which("virsh", path=environment.get("PATH"))
    assert virsh is not None
    domain = legacy._bounded_command(
        [virsh, "-c", "qemu:///system", "dumpxml", name], environment=environment, timeout=15
    )
    legacy._success(domain)
    devices = ET.fromstring(domain.stdout)
    assert devices.findall("./devices/interface") == []
    assert devices.findall("./devices/hostdev") == []
    assert devices.findall("./devices/filesystem") == []


def _assert_override_provenance(environment: dict[str, str], name: str, image_process: OCIProcessSpec) -> None:
    snapshot = read_run_ledger_snapshot(resolve_roots(environment), name)
    transaction = OCIRootPreparationTransaction.from_dict(snapshot.state.get("oci_root"))
    provenance = transaction.boot_plan["process_provenance"]
    assert OCIProcessSpec.from_dict(provenance["image_process"]) == image_process
    assert tuple(provenance["image_command"]) == ("/bin/bash",)
    assert tuple(provenance["command_override"]) == _KEEPALIVE
    assert provenance["user_override"] is None
    entrypoint = tuple(provenance["image_entrypoint"])
    effective = OCIProcessSpec.from_dict(transaction.boot_plan["process"])
    assert effective == image_process.with_command(entrypoint, _KEEPALIVE)


def _setup(environment: dict[str, str], name: str):
    root = Path(environment.get("PALIMPSEST_OCI_ML_PROOF_ROOT", ""))
    assert root.is_absolute() and root.resolve(strict=True) == root
    info = root.stat(follow_symlinks=False)
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o711
    for _attempt in range(16):
        parent = root / ("m-" + uuid.uuid4().hex[:8])
        try:
            parent.lstat()
        except FileNotFoundError:
            break
    else:
        raise AssertionError("could not select a fresh ML runtime parent")
    selected = dict(environment)
    legacy._success(legacy._cli(selected, "oci", "init-runtime", parent))
    selected["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    selected["XDG_CONFIG_HOME"] = str(parent / "config")
    evidence = parent / "setup-evidence"
    evidence.mkdir(mode=0o700)
    selected["PALIMPSEST_PROOF_EVIDENCE_DIR"] = str(evidence)
    roots = resolve_roots(selected)
    assert len(os.fsencode(roots.runs / name / "io" / "lifecycle.sock")) <= 97
    return parent, selected


def _fresh_root_volume_baseline(roots) -> set[str]:
    try:
        roots.oci_root_volumes.lstat()
    except FileNotFoundError:
        return set()
    raise AssertionError("fresh ML runtime root-volume path already exists")


def _proof(case: MLCase) -> None:
    selection = _selection(case)
    name = "ml-" + case.key.lower() + "-" + uuid.uuid4().hex[:8]
    parent, environment = _setup(legacy._environment(), name)
    source_hash = legacy._file_sha256(selection.archive)
    roots = resolve_roots(environment)
    assert shutil.disk_usage(parent).free >= _PRIVATE_DISK_BUDGET
    root_volumes_before = _fresh_root_volume_baseline(roots)
    try:
        original = legacy._authenticate(selection, parent)
        assert original.argv[-1:] == ("/bin/bash",)
        launched = legacy._save(
            parent,
            "run",
            legacy._cli(
                environment,
                "run",
                selection.archive,
                "--manifest",
                selection.manifest_digest,
                "--name",
                name,
                "--memory",
                "8192",
                "--vcpus",
                "2",
                "--network",
                "none",
                "-d",
                "--",
                *_KEEPALIVE,
                timeout=900,
            ),
        )
        legacy._success(launched)
        assert launched.stdout == (name + "\n").encode()
        _assert_override_provenance(environment, name, original)
        root_volumes_running = {entry.name for entry in roots.oci_root_volumes.iterdir()}
        created_root_volumes = root_volumes_running - root_volumes_before
        assert len(created_root_volumes) == 1
        root_volume_path = roots.oci_root_volumes / created_root_volumes.pop()
        root_volume_info = root_volume_path.stat(follow_symlinks=False)
        assert stat.S_ISDIR(root_volume_info.st_mode) and root_volume_info.st_uid == os.geteuid()
        before = legacy._root_proof(environment, name)
        domain_uuid = before["domain"]["uuid"]
        _assert_cpu_only_domain(environment, name)
        calculation = legacy._save(
            parent,
            "tensor",
            legacy._cli(environment, "exec", name, "--", case.python, "-c", case.program, timeout=180),
        )
        legacy._success(calculation)
        assert case.output.fullmatch(calculation.stdout)

        identity = legacy._save(
            parent,
            "root",
            legacy._cli(environment, "exec", name, "--", "/bin/sh", "-c", "stat -c '%d %i' /", timeout=60),
        )
        legacy._success(identity)
        device, inode = (int(value) for value in identity.stdout.split())
        refusal = legacy._save(
            parent,
            "pid1-refusal",
            legacy._cli(
                environment,
                "exec",
                name,
                "--",
                "/bin/sh",
                "-c",
                "LC_ALL=C cat /proc/1/root/etc/os-release",
                timeout=60,
            ),
        )
        assert refusal.returncode != 0 and refusal.stdout == b"" and b"Permission denied" in refusal.stderr
        after = legacy._root_proof(environment, name)
        assert before["root_identity"] == after["root_identity"]
        assert (device, inode) == (after["root_identity"]["device"], after["root_identity"]["inode"])
        legacy._success(legacy._save(parent, "stop", legacy._cli(environment, "stop", name, timeout=90)))
        legacy._success(legacy._save(parent, "rm", legacy._cli(environment, "rm", name, timeout=90)))
        legacy._assert_domain_absent(environment, name, domain_uuid)
        assert not (roots.runs / name).exists()
        assert not root_volume_path.exists()
        assert {entry.name for entry in roots.oci_root_volumes.iterdir()} == root_volumes_before
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
    except BaseException:
        print(f"ML CPU proof failure preserved: {parent}", flush=True)
        raise
    finally:
        legacy._record_source_hashes(parent, source_hash, selection.archive)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key.lower())
def test_official_ml_image_cpu_tensor_with_public_command_override(case: MLCase):
    _proof(case)
