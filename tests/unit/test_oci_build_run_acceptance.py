"""Exercise Gate 2 orchestration without launching guests or changing its probe."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def case(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "e2e" / "test_local_oci_build_run.py"
    spec = importlib.util.spec_from_file_location("oci_build_run_acceptance", path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    archive = artifact / "image.oci.tar"
    archive.write_bytes(b"immutable local OCI image")
    marker = "palimpsest-local-build-" + "a" * 32
    (artifact / "acceptance.json").write_text(
        json.dumps(
            {
                "archive_sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
                "manifest_digest": "sha256:" + "b" * 64,
                "marker": marker,
                "platform": "linux/amd64",
                "schema": "palimpsest.oci-root-build-run-acceptance.v2",
            }
        )
    )
    root = tmp_path / "configured-state"
    work = tmp_path / "acceptance"
    work.mkdir()
    value = SimpleNamespace(
        gate=gate,
        root=root,
        work=work,
        archive=archive,
        calls=[],
        probe_error=None,
        stop_error=None,
        leave_run=False,
        mutate_archive=False,
        docker_invoked=False,
        domain_error=False,
        domain_present=False,
        run_name=None,
        proof_reads=0,
        proof_identity_change=False,
        pid1_access_allowed=False,
    )
    monkeypatch.setenv("PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR", str(artifact))
    monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(root))
    monkeypatch.setattr(gate.shutil, "which", lambda *a, **k: "/usr/bin/virsh")

    def run(arguments, *, environment, timeout):
        value.calls.append(arguments[0])
        assert environment["DOCKER_HOST"] == "unix:///palimpsest-e2e-forbidden-docker.sock"
        assert Path(environment["PATH"].split(":")[0], "docker").is_file()
        if arguments[0] == "run":
            value.run_name = arguments[arguments.index("--name") + 1]
            (root / "runs" / value.run_name).mkdir(parents=True)
        elif arguments[:2] == ["oci", "root-proof"]:
            value.proof_reads += 1
            device = 8 if value.proof_identity_change and value.proof_reads == 2 else 7
            proof = {
                "schema": "palimpsest.oci-root-proof.v1",
                "run": {"name": value.run_name, "run_id": "run"},
                "boot": {"attempt_id": "attempt", "generation": "generation"},
                "domain": {"id": 7, "uuid": "domain"},
                "root_identity": {"schema": "palimpsest.oci-root-identity.v1", "pid": 1,
                                  "filesystem": "overlayfs", "device": device, "inode": 11},
            }
            return subprocess.CompletedProcess(arguments, 0, json.dumps(proof), "")
        elif arguments[0] == "exec" and arguments[-1] == "/usr/local/bin/palimpsest-e2e-probe":
            if isinstance(value.probe_error, BaseException):
                raise value.probe_error
            if value.probe_error:
                return subprocess.CompletedProcess(arguments, 1, "", value.probe_error)
            return subprocess.CompletedProcess(arguments, 0, f"PALIMPSEST_OCI_ROOT_OK:{marker}:7:11\n", "")
        elif arguments[0] == "exec":
            return subprocess.CompletedProcess(arguments, 95 if value.pid1_access_allowed else 0, "", "")
        elif arguments[0] == "stop" and value.stop_error:
            raise value.stop_error
        elif arguments[0] == "rm":
            if not value.leave_run:
                (root / "runs" / value.run_name).rmdir()
            if value.mutate_archive:
                archive.write_bytes(b"changed source")
            if value.docker_invoked:
                (work / "docker-runtime-audit").write_text("forbidden command")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def virsh(arguments, **kwargs):
        operation = arguments[3]
        value.calls.append(operation)
        if operation == "dominfo":
            return subprocess.CompletedProcess(arguments, 0, "State: running\n", "")
        assert arguments[3:] == ["list", "--all", "--name"]
        return subprocess.CompletedProcess(
            arguments,
            int(value.domain_error),
            (value.run_name + "\n") if value.domain_present else "",
            "libvirt failure",
        )

    monkeypatch.setattr(gate, "_run", run)
    monkeypatch.setattr(gate.subprocess, "run", virsh)
    value.execute = lambda: gate.test_local_build_runs_detached_with_oci_root_as_vm_root(work)
    value.checks = lambda: json.loads((work / "command-evidence" / "checks.json").read_text())
    return value


def test_docker_present_host_reaches_original_probe_and_records_guard_scope(case, monkeypatch):
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: True if str(path) == "/run/docker.sock" else exists(path))
    case.execute()
    assert case.calls == ["run", "dominfo", "oci", "exec", "exec", "oci", "stop", "rm", "list"]
    assert set(case.checks().values()) == {"passed"}
    evidence = json.loads((case.work / "command-evidence" / "runtime.json").read_text())
    assert evidence["state_root"] == str(case.root)
    assert "/run/docker.sock" in evidence["observed_docker_sockets"]
    assert "not all socket connections" in evidence["docker_guard_scope"]


def test_probe_failure_preserves_primary_error_and_reports_all_cleanup_checks(case):
    case.probe_error = "cat: /proc/1/root/palimpsest-e2e-root-marker: Permission denied"
    with pytest.raises(AssertionError, match="Permission denied") as raised:
        case.execute()
    assert case.calls[-3:] == ["stop", "rm", "list"]
    assert set(case.checks().values()) == {"passed"}
    assert "archive-preserved: passed" in raised.value.__notes__[0]
    assert json.loads((case.work / "command-evidence" / "exec.json").read_text())["returncode"] == 1


def test_gate_rejects_root_identity_change_across_bracketed_proofs(case):
    case.proof_identity_change = True
    with pytest.raises(AssertionError):
        case.execute()


def test_gate_rejects_unexpected_pid1_root_access(case):
    case.pid1_access_allowed = True
    with pytest.raises(AssertionError):
        case.execute()


def test_stop_timeout_does_not_hide_probe_failure_or_skip_rm_and_other_checks(case):
    case.probe_error = "original probe failure"
    case.stop_error = subprocess.TimeoutExpired(["stop"], 60, output=b"partial stop output")
    with pytest.raises(AssertionError, match="original probe failure") as raised:
        case.execute()
    assert case.calls[-3:] == ["stop", "rm", "list"]
    assert case.checks()["stop"].startswith("failed: TimeoutExpired")
    assert case.checks()["rm"] == "passed"
    assert "stop: failed" in raised.value.__notes__[0]
    evidence = json.loads((case.work / "command-evidence" / "stop.json").read_text())
    assert evidence["stdout"] == "partial stop output"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["exec"], 60), KeyboardInterrupt()])
def test_probe_exception_is_reraised_after_all_cleanup_checks(case, error):
    case.probe_error = error
    with pytest.raises(type(error)) as raised:
        case.execute()
    assert raised.value is error
    assert case.calls[-3:] == ["stop", "rm", "list"]
    assert set(case.checks().values()) == {"passed"}


@pytest.mark.parametrize(
    ("attribute", "check"),
    [
        ("leave_run", "run-absent"),
        ("mutate_archive", "archive-preserved"),
        ("docker_invoked", "docker-cli-not-invoked"),
        ("domain_error", "domain-absent"),
        ("domain_present", "domain-absent"),
    ],
)
def test_successful_probe_does_not_hide_cleanup_or_guard_failure(case, attribute, check):
    setattr(case, attribute, True)
    with pytest.raises(AssertionError, match=f"{check}: failed"):
        case.execute()
    assert case.checks()[check].startswith("failed:")
    assert len(case.checks()) == 6


def test_persisted_command_streams_are_bounded(case):
    case.probe_error = "x" * (case.gate._EVIDENCE_LIMIT * 2)
    with pytest.raises(AssertionError):
        case.execute()
    evidence = json.loads((case.work / "command-evidence" / "exec.json").read_text())
    assert len(evidence["stderr"].encode()) == case.gate._EVIDENCE_LIMIT
    assert evidence["stream_limit_bytes"] == case.gate._EVIDENCE_LIMIT


@pytest.mark.parametrize("suffix", ["한글".encode(), b"\xff\xff", b"\xf0\x9f\x98\x80"])
def test_evidence_byte_cap_survives_multibyte_and_invalid_boundaries(case, suffix):
    value = b"x" * (case.gate._EVIDENCE_LIMIT - 1) + suffix
    bounded = case.gate._bounded(value)
    assert len(bounded.encode("utf-8")) <= case.gate._EVIDENCE_LIMIT
    assert bounded.startswith("x" * (case.gate._EVIDENCE_LIMIT - 1))


def _refuse_evidence_write(monkeypatch, filename):
    original = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == filename:
            raise OSError("diagnostic disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["exec"], 60), KeyboardInterrupt()])
def test_command_evidence_failure_preserves_original_exception(case, monkeypatch, error):
    case.probe_error = error
    _refuse_evidence_write(monkeypatch, "exec.json")
    with pytest.raises(type(error)) as raised:
        case.execute()
    assert raised.value is error
    assert any("Command evidence persistence failed (exec)" in note for note in error.__notes__)
    assert set(case.checks().values()) == {"passed"}


def test_command_evidence_failure_without_original_exception_fails_and_cleans(case, monkeypatch):
    _refuse_evidence_write(monkeypatch, "exec.json")
    with pytest.raises(OSError, match="diagnostic disk full"):
        case.execute()
    assert set(case.checks().values()) == {"passed"}


@pytest.mark.parametrize("probe_failed", [False, True])
def test_cleanup_evidence_failure_preserves_primary_and_reports_checks(case, monkeypatch, probe_failed):
    if probe_failed:
        case.probe_error = "original probe failure"
    _refuse_evidence_write(monkeypatch, "checks.json")
    with pytest.raises(AssertionError if probe_failed else OSError) as raised:
        case.execute()
    assert str(raised.value) == ("original probe failure" if probe_failed else "diagnostic disk full")
    notes = "\n".join(raised.value.__notes__)
    assert "archive-preserved: passed" in notes and "rm: passed" in notes
    if probe_failed:
        assert "Cleanup evidence persistence failed" in notes
