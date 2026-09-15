"""Opt-in CPU tensor proofs for pinned official ML images and public command overrides."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
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
_PHASES = frozenset(
    {
        "setup",
        "preflight",
        "authenticate",
        "public-run-command",
        "public-run-assertion",
        "provenance",
        "root-volume",
        "root-proof",
        "root-proof-after",
        "domain-check",
        "framework-exec-command",
        "framework-exec-assertion",
        "service-readiness",
        "service-probe-command",
        "service-probe-assertion",
        "root-identity",
        "pid1-refusal",
        "stop",
        "remove",
        "cleanup-assertion",
        "source-hash-assertion",
        "source-preservation",
        "complete",
    }
)
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
        "/usr/bin/python",
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

_PYTORCH_SERVICE_READY = b"ML_SERVICE_READY pytorch transformer-encoder-6x256 cpu"
_PYTORCH_SERVICE_OUTPUT = re.compile(
    rb"^ML_SERVICE_OK pytorch 2\.8\.0(?:\+cu126)? transformer-encoder-6x256 "
    rb"\[1, 128, 256\] 4 cpu False [0-9a-f]{64}\n$"
)
_PYTORCH_SERVICE_PROGRAM = """import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import torch

torch.set_num_threads(2)
torch.set_num_interop_threads(1)
torch.manual_seed(0)
layer = torch.nn.TransformerEncoderLayer(
    d_model=256,
    nhead=8,
    dim_feedforward=1024,
    dropout=0.0,
    activation="gelu",
    batch_first=True,
)
model = torch.nn.TransformerEncoder(layer, num_layers=6).eval()
sample = torch.linspace(-1.0, 1.0, steps=128 * 256, dtype=torch.float32).reshape(1, 128, 256)
request_count = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, _format, *_args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/healthz":
            self.send_json(404, {"error": "not-found"})
            return
        self.send_json(
            200,
            {
                "cuda": torch.cuda.is_available(),
                "device": "cpu",
                "framework": "pytorch",
                "model": "transformer-encoder-6x256",
                "status": "ready",
                "version": torch.__version__,
            },
        )

    def do_POST(self):
        global request_count
        if self.path != "/infer":
            self.send_json(404, {"error": "not-found"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 1 <= length <= 256:
                raise ValueError
            request = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "invalid-request"})
            return
        if request != {"iterations": 4, "scale": 1.0}:
            self.send_json(400, {"error": "unsupported-request"})
            return
        value = sample * request["scale"]
        with torch.inference_mode():
            for _iteration in range(request["iterations"]):
                value = model(value)
        request_count += 1
        self.send_json(
            200,
            {
                "cuda": torch.cuda.is_available(),
                "device": str(value.device),
                "finite": bool(torch.isfinite(value).all().item()),
                "iterations": request["iterations"],
                "model": "transformer-encoder-6x256",
                "output_sha256": hashlib.sha256(value.contiguous().numpy().tobytes()).hexdigest(),
                "requests": request_count,
                "shape": list(value.shape),
            },
        )


server = HTTPServer(("127.0.0.1", 18080), Handler)
print("ML_SERVICE_READY pytorch transformer-encoder-6x256 cpu", flush=True)
server.serve_forever()
"""
_PYTORCH_SERVICE_PROBE = """import json
import re
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:18080/healthz", timeout=10) as response:
    health = json.load(response)
assert response.status == 200
assert health == {
    "cuda": False,
    "device": "cpu",
    "framework": "pytorch",
    "model": "transformer-encoder-6x256",
    "status": "ready",
    "version": health["version"],
}
payload = json.dumps({"iterations": 4, "scale": 1.0}, sort_keys=True).encode()


def infer():
    request = urllib.request.Request(
        "http://127.0.0.1:18080/infer",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)
    assert response.status == 200
    return result


first = infer()
second = infer()
for expected_requests, result in enumerate((first, second), start=1):
    assert result["cuda"] is False
    assert result["device"] == "cpu"
    assert result["finite"] is True
    assert result["iterations"] == 4
    assert result["model"] == "transformer-encoder-6x256"
    assert result["requests"] == expected_requests
    assert result["shape"] == [1, 128, 256]
    assert re.fullmatch(r"[0-9a-f]{64}", result["output_sha256"])
assert first["output_sha256"] == second["output_sha256"]
print(
    "ML_SERVICE_OK pytorch",
    health["version"],
    first["model"],
    first["shape"],
    first["iterations"],
    first["device"],
    first["cuda"],
    first["output_sha256"],
)
"""


def _write_phase_receipt(evidence: Path, framework: str, phase: str, status: str, returncode: int | None) -> None:
    if (
        framework not in {"tensorflow", "pytorch"}
        or phase not in _PHASES
        or status not in {"entered", "failed", "passed"}
    ):
        raise AssertionError("invalid ML phase receipt")
    if returncode is not None and (type(returncode) is not int or not -255 <= returncode <= 255):
        raise AssertionError("invalid ML phase return code")
    directory_fd = os.open(evidence, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    temporary = ".ml-phase-" + uuid.uuid4().hex
    descriptor = -1
    try:
        info = os.fstat(directory_fd)
        assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
        try:
            current = os.stat("ml-phase.json", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            assert stat.S_ISREG(current.st_mode) and current.st_uid == os.geteuid()
            assert stat.S_IMODE(current.st_mode) == 0o600 and current.st_nlink == 1
        payload = (
            json.dumps(
                {
                    "framework": framework,
                    "phase": phase,
                    "returncode": returncode,
                    "schema": "palimpsest.oci-ml-phase.v1",
                    "status": status,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        assert len(payload) <= 512
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if type(written) is not int or written <= 0:
                raise OSError("ML phase receipt write failed")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, "ml-phase.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _record_phase(
    evidence: Path,
    framework: str,
    state: list[object],
    phase: str,
    status: str,
    returncode: int | None = None,
) -> None:
    state[:] = [phase, returncode]
    try:
        _write_phase_receipt(evidence, framework, phase, status, returncode)
    except BaseException:
        pass


def _require_success(state: list[object], phase: str, result: subprocess.CompletedProcess[bytes]) -> None:
    state[:] = [phase, result.returncode if type(result.returncode) is int else None]
    legacy._success(result)


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


def _assert_override_provenance(
    environment: dict[str, str],
    name: str,
    image_process: OCIProcessSpec,
    command_override: tuple[str, ...] = _KEEPALIVE,
) -> OCIRootPreparationTransaction:
    snapshot = read_run_ledger_snapshot(resolve_roots(environment), name)
    transaction = OCIRootPreparationTransaction.from_dict(snapshot.state.get("oci_root"))
    provenance = transaction.boot_plan["process_provenance"]
    assert OCIProcessSpec.from_dict(provenance["image_process"]) == image_process
    assert tuple(provenance["image_command"]) == ("/bin/bash",)
    assert tuple(provenance["command_override"]) == command_override
    assert provenance["user_override"] is None
    entrypoint = tuple(provenance["image_entrypoint"])
    effective = OCIProcessSpec.from_dict(transaction.boot_plan["process"])
    assert effective == image_process.with_command(entrypoint, command_override)
    return transaction


def _assert_new_root_volume_files(roots, before: set[str], transaction: OCIRootPreparationTransaction):
    stem = transaction.volume_id.replace("-", "")
    expected = {f"{stem}.raw", f"{stem}.json"}
    current = {entry.name for entry in roots.oci_root_volumes.iterdir()}
    assert current - before == expected
    raw = roots.oci_root_volumes / f"{stem}.raw"
    record = roots.oci_root_volumes / f"{stem}.json"
    raw_info = raw.stat(follow_symlinks=False)
    record_info = record.stat(follow_symlinks=False)
    assert stat.S_ISREG(raw_info.st_mode) and raw_info.st_uid == os.geteuid() and raw_info.st_nlink == 1
    assert raw_info.st_size == transaction.volume_size_bytes
    assert stat.S_ISREG(record_info.st_mode) and record_info.st_uid == os.geteuid() and record_info.st_nlink == 1
    return raw, record


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
    evidence = Path(environment["PALIMPSEST_PROOF_EVIDENCE_DIR"])
    phase_state: list[object] = ["setup", None]
    _record_phase(evidence, case.key.lower(), phase_state, "setup", "passed")
    source_hash: str | None = None
    try:
        _record_phase(evidence, case.key.lower(), phase_state, "preflight", "entered")
        source_hash = legacy._file_sha256(selection.archive)
        roots = resolve_roots(environment)
        assert shutil.disk_usage(parent).free >= _PRIVATE_DISK_BUDGET
        root_volumes_before = _fresh_root_volume_baseline(roots)
        _record_phase(evidence, case.key.lower(), phase_state, "authenticate", "entered")
        original = legacy._authenticate(selection, parent)
        assert original.argv[-1:] == ("/bin/bash",)
        _record_phase(evidence, case.key.lower(), phase_state, "public-run-command", "entered")
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
        _require_success(phase_state, "public-run-command", launched)
        _record_phase(evidence, case.key.lower(), phase_state, "public-run-assertion", "entered", launched.returncode)
        assert launched.stdout == (name + "\n").encode()
        _record_phase(evidence, case.key.lower(), phase_state, "provenance", "entered")
        transaction = _assert_override_provenance(environment, name, original)
        _record_phase(evidence, case.key.lower(), phase_state, "root-volume", "entered")
        root_volume_path, root_volume_record = _assert_new_root_volume_files(roots, root_volumes_before, transaction)
        _record_phase(evidence, case.key.lower(), phase_state, "root-proof", "entered")
        before = legacy._root_proof(environment, name)
        domain_uuid = before["domain"]["uuid"]
        _record_phase(evidence, case.key.lower(), phase_state, "domain-check", "entered")
        _assert_cpu_only_domain(environment, name)
        _record_phase(evidence, case.key.lower(), phase_state, "framework-exec-command", "entered")
        calculation = legacy._save(
            parent,
            "tensor",
            legacy._cli(
                environment, "exec", "--timeout", "150", name, "--", case.python, "-c", case.program, timeout=180
            ),
        )
        _require_success(phase_state, "framework-exec-command", calculation)
        _record_phase(
            evidence,
            case.key.lower(),
            phase_state,
            "framework-exec-assertion",
            "entered",
            calculation.returncode,
        )
        assert case.output.fullmatch(calculation.stdout)

        _record_phase(evidence, case.key.lower(), phase_state, "root-identity", "entered")
        identity = legacy._save(
            parent,
            "root",
            legacy._cli(environment, "exec", name, "--", "/bin/sh", "-c", "stat -c '%d %i' /", timeout=60),
        )
        _require_success(phase_state, "root-identity", identity)
        device, inode = (int(value) for value in identity.stdout.split())
        _record_phase(evidence, case.key.lower(), phase_state, "pid1-refusal", "entered")
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
        _record_phase(evidence, case.key.lower(), phase_state, "root-proof-after", "entered")
        after = legacy._root_proof(environment, name)
        assert before["root_identity"] == after["root_identity"]
        assert (device, inode) == (after["root_identity"]["device"], after["root_identity"]["inode"])
        _record_phase(evidence, case.key.lower(), phase_state, "stop", "entered")
        stopped = legacy._save(parent, "stop", legacy._cli(environment, "stop", name, timeout=90))
        _require_success(phase_state, "stop", stopped)
        _record_phase(evidence, case.key.lower(), phase_state, "remove", "entered")
        removed = legacy._save(parent, "rm", legacy._cli(environment, "rm", name, timeout=90))
        _require_success(phase_state, "remove", removed)
        _record_phase(evidence, case.key.lower(), phase_state, "cleanup-assertion", "entered")
        legacy._assert_domain_absent(environment, name, domain_uuid)
        assert not (roots.runs / name).exists()
        assert not root_volume_path.exists()
        assert not root_volume_record.exists()
        assert {entry.name for entry in roots.oci_root_volumes.iterdir()} == root_volumes_before
        _record_phase(evidence, case.key.lower(), phase_state, "source-hash-assertion", "entered")
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        _record_phase(evidence, case.key.lower(), phase_state, "source-preservation", "entered")
    except BaseException:
        _record_phase(
            evidence,
            case.key.lower(),
            phase_state,
            str(phase_state[0]),
            "failed",
            phase_state[1] if type(phase_state[1]) is int else None,
        )
        print(f"ML CPU proof failure preserved: {parent}", flush=True)
        raise
    finally:
        if source_hash is not None:
            try:
                legacy._record_source_hashes(parent, source_hash, selection.archive)
            except BaseException:
                _record_phase(evidence, case.key.lower(), phase_state, "source-preservation", "failed")
                raise
    _record_phase(evidence, case.key.lower(), phase_state, "complete", "passed")


def _pytorch_service_proof() -> None:
    case = next(selected for selected in CASES if selected.key == "PYTORCH")
    selection = _selection(case)
    name = "ml-pytorch-service-" + uuid.uuid4().hex[:8]
    parent, environment = _setup(legacy._environment(), name)
    evidence = Path(environment["PALIMPSEST_PROOF_EVIDENCE_DIR"])
    phase_state: list[object] = ["setup", None]
    _record_phase(evidence, "pytorch", phase_state, "setup", "passed")
    source_hash: str | None = None
    service_command = (case.python, "-u", "-c", _PYTORCH_SERVICE_PROGRAM)
    try:
        _record_phase(evidence, "pytorch", phase_state, "preflight", "entered")
        source_hash = legacy._file_sha256(selection.archive)
        roots = resolve_roots(environment)
        assert shutil.disk_usage(parent).free >= _PRIVATE_DISK_BUDGET
        root_volumes_before = _fresh_root_volume_baseline(roots)
        _record_phase(evidence, "pytorch", phase_state, "authenticate", "entered")
        original = legacy._authenticate(selection, parent)
        assert original.argv[-1:] == ("/bin/bash",)
        _record_phase(evidence, "pytorch", phase_state, "public-run-command", "entered")
        launched = legacy._save(
            parent,
            "service-run",
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
                *service_command,
                timeout=900,
            ),
        )
        _require_success(phase_state, "public-run-command", launched)
        _record_phase(evidence, "pytorch", phase_state, "public-run-assertion", "entered", launched.returncode)
        assert launched.stdout == (name + "\n").encode()
        _record_phase(evidence, "pytorch", phase_state, "provenance", "entered")
        transaction = _assert_override_provenance(environment, name, original, service_command)
        _record_phase(evidence, "pytorch", phase_state, "root-volume", "entered")
        root_volume_path, root_volume_record = _assert_new_root_volume_files(roots, root_volumes_before, transaction)
        _record_phase(evidence, "pytorch", phase_state, "root-proof", "entered")
        before = legacy._root_proof(environment, name)
        domain_uuid = before["domain"]["uuid"]
        _record_phase(evidence, "pytorch", phase_state, "domain-check", "entered")
        _assert_cpu_only_domain(environment, name)
        _record_phase(evidence, "pytorch", phase_state, "service-readiness", "entered")
        legacy._wait_console(roots.runs / name / "io" / "console.log", _PYTORCH_SERVICE_READY, timeout=120)
        _record_phase(evidence, "pytorch", phase_state, "service-probe-command", "entered")
        probe = legacy._save(
            parent,
            "service-probe",
            legacy._cli(
                environment,
                "exec",
                "--timeout",
                "300",
                name,
                "--",
                case.python,
                "-c",
                _PYTORCH_SERVICE_PROBE,
                timeout=330,
            ),
        )
        _require_success(phase_state, "service-probe-command", probe)
        _record_phase(evidence, "pytorch", phase_state, "service-probe-assertion", "entered", probe.returncode)
        assert _PYTORCH_SERVICE_OUTPUT.fullmatch(probe.stdout)
        _record_phase(evidence, "pytorch", phase_state, "root-identity", "entered")
        identity = legacy._save(
            parent,
            "service-root",
            legacy._cli(environment, "exec", name, "--", "/bin/sh", "-c", "stat -c '%d %i' /", timeout=60),
        )
        _require_success(phase_state, "root-identity", identity)
        device, inode = (int(value) for value in identity.stdout.split())
        _record_phase(evidence, "pytorch", phase_state, "pid1-refusal", "entered")
        refusal = legacy._save(
            parent,
            "service-pid1-refusal",
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
        _record_phase(evidence, "pytorch", phase_state, "root-proof-after", "entered")
        after = legacy._root_proof(environment, name)
        assert before["root_identity"] == after["root_identity"]
        assert (device, inode) == (after["root_identity"]["device"], after["root_identity"]["inode"])
        _record_phase(evidence, "pytorch", phase_state, "stop", "entered")
        stopped = legacy._save(parent, "service-stop", legacy._cli(environment, "stop", name, timeout=90))
        _require_success(phase_state, "stop", stopped)
        _record_phase(evidence, "pytorch", phase_state, "remove", "entered")
        removed = legacy._save(parent, "service-rm", legacy._cli(environment, "rm", name, timeout=90))
        _require_success(phase_state, "remove", removed)
        _record_phase(evidence, "pytorch", phase_state, "cleanup-assertion", "entered")
        legacy._assert_domain_absent(environment, name, domain_uuid)
        assert not (roots.runs / name).exists()
        assert not root_volume_path.exists()
        assert not root_volume_record.exists()
        assert {entry.name for entry in roots.oci_root_volumes.iterdir()} == root_volumes_before
        _record_phase(evidence, "pytorch", phase_state, "source-hash-assertion", "entered")
        assert legacy._file_sha256(selection.archive) == source_hash == selection.archive_digest
        _record_phase(evidence, "pytorch", phase_state, "source-preservation", "entered")
    except BaseException:
        _record_phase(
            evidence,
            "pytorch",
            phase_state,
            str(phase_state[0]),
            "failed",
            phase_state[1] if type(phase_state[1]) is int else None,
        )
        print(f"PyTorch service proof failure preserved: {parent}", flush=True)
        raise
    finally:
        if source_hash is not None:
            try:
                legacy._record_source_hashes(parent, source_hash, selection.archive)
            except BaseException:
                _record_phase(evidence, "pytorch", phase_state, "source-preservation", "failed")
                raise
    _record_phase(evidence, "pytorch", phase_state, "complete", "passed")


def test_official_pytorch_cpu_http_inference_service():
    _pytorch_service_proof()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key.lower())
def test_official_ml_image_cpu_tensor_with_public_command_override(case: MLCase):
    _proof(case)
