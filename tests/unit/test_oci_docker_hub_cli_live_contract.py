"""Portable contract checks for the opt-in Docker Hub native proof."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROOF_PATH = Path(__file__).resolve().parents[1] / "kvm" / "test_oci_docker_hub_cli_live.py"
_SPEC = importlib.util.spec_from_file_location("docker_hub_cli_live_proof", _PROOF_PATH)
assert _SPEC is not None and _SPEC.loader is not None
proof = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proof
_SPEC.loader.exec_module(proof)


def _environment(path: Path, kind: str) -> dict[str, str]:
    return {
        f"PALIMPSEST_OCI_DOCKER_HUB_{kind}_LIVE": "1",
        f"PALIMPSEST_OCI_DOCKER_HUB_{kind}_IMAGE": str(path),
        f"PALIMPSEST_OCI_DOCKER_HUB_{kind}_ARCHIVE_SHA256": proof._file_sha256(path),
        f"PALIMPSEST_OCI_DOCKER_HUB_{kind}_MANIFEST_SHA256": "sha256:" + "a" * 64,
    }


@pytest.mark.parametrize("kind", ["HELLO", "REDIS", "REDIS_USER", "NGINX"])
def test_selection_requires_independent_opt_in_and_all_pins(tmp_path, kind):
    with pytest.raises(pytest.skip.Exception):
        proof._selection(kind, {})
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"unchanged external archive")
    environment = _environment(archive, kind)
    for key in ("IMAGE", "ARCHIVE_SHA256", "MANIFEST_SHA256"):
        incomplete = dict(environment)
        del incomplete[f"PALIMPSEST_OCI_DOCKER_HUB_{kind}_{key}"]
        with pytest.raises((AssertionError, FileNotFoundError)):
            proof._selection(kind, incomplete)


@pytest.mark.parametrize("kind", ["HELLO", "REDIS", "REDIS_USER", "NGINX"])
def test_selection_accepts_only_absolute_hash_bound_local_archive(tmp_path, kind):
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"unchanged external archive")
    environment = _environment(archive, kind)
    selected = proof._selection(kind, environment)
    assert selected.archive == archive.resolve()
    assert selected.archive_digest == "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    assert selected.manifest_digest == "sha256:" + "a" * 64
    environment[f"PALIMPSEST_OCI_DOCKER_HUB_{kind}_ARCHIVE_SHA256"] = "sha256:" + "b" * 64
    with pytest.raises(AssertionError):
        proof._selection(kind, environment)


def test_selection_rejects_relative_paths_and_noncanonical_manifest_pin(tmp_path, monkeypatch):
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"archive")
    environment = _environment(archive, "HELLO")
    monkeypatch.chdir(tmp_path)
    environment["PALIMPSEST_OCI_DOCKER_HUB_HELLO_IMAGE"] = archive.name
    with pytest.raises(AssertionError, match="absolute"):
        proof._selection("HELLO", environment)
    environment["PALIMPSEST_OCI_DOCKER_HUB_HELLO_IMAGE"] = str(archive)
    environment["PALIMPSEST_OCI_DOCKER_HUB_HELLO_MANIFEST_SHA256"] = "SHA256:" + "a" * 64
    with pytest.raises(AssertionError, match="canonical"):
        proof._selection("HELLO", environment)


def test_native_proof_uses_public_local_cli_and_never_docker_for_workloads():
    source = Path(proof.__file__).read_text()
    assert '"run",\n                selection.archive' in source
    assert '"--manifest",\n                selection.manifest_digest' in source
    assert "LocalArchiveSource(selection.archive, selection.manifest_digest)" in source
    assert "docker pull" not in source and "docker run" not in source and "docker save" not in source


def test_redis_user_selection_is_separate_and_default_cannot_acquire_override(tmp_path):
    archive = tmp_path / "redis.oci.tar"
    archive.write_bytes(b"unchanged redis archive")
    environment = _environment(archive, "REDIS")
    with pytest.raises(pytest.skip.Exception):
        proof._selection("REDIS_USER", environment)
    environment.update(_environment(archive, "REDIS_USER"))
    default = proof._selection("REDIS", environment)
    explicit = proof._selection("REDIS_USER", environment)
    assert default == explicit

    default_arguments = proof._detached_run_arguments(default, "hub-redis-proof")
    explicit_arguments = proof._detached_run_arguments(explicit, "hub-redis-user-proof", user_override="redis")
    assert default_arguments == (
        "run",
        archive.resolve(),
        "--manifest",
        "sha256:" + "a" * 64,
        "--name",
        "hub-redis-proof",
        "--memory",
        "512",
        "--vcpus",
        "1",
        "-d",
    )
    assert explicit_arguments == (
        "run",
        archive.resolve(),
        "--manifest",
        "sha256:" + "a" * 64,
        "--name",
        "hub-redis-user-proof",
        "--memory",
        "512",
        "--vcpus",
        "1",
        "-d",
        "--user",
        "redis",
    )


def _explicit_user_status(*, user: str = "redis", uid: int = 999, gid: int = 998) -> bytes:
    return (
        f"user={user}\nuid={uid}\ngid={gid}\naccount_uid={uid}\naccount_gid={gid}\n"
        "CapInh=0000000000000000\nCapPrm=0000000000000000\n"
        "CapEff=0000000000000000\nCapBnd=0000000000000000\n"
        "CapAmb=0000000000000000\nNoNewPrivs=1\nSeccomp=2\n"
    ).encode()


def test_explicit_user_status_requires_named_nonroot_identity_and_capabilityless_policy():
    proof._assert_explicit_user_status(_explicit_user_status(), "redis")
    for payload in (
        _explicit_user_status(user="root"),
        _explicit_user_status(uid=0),
        _explicit_user_status().replace(b"account_gid=998", b"account_gid=997"),
        _explicit_user_status().replace(b"CapEff=0000000000000000", b"CapEff=0000000000000001"),
        _explicit_user_status().replace(b"NoNewPrivs=1", b"NoNewPrivs=0"),
        _explicit_user_status().replace(b"Seccomp=2", b"Seccomp=0"),
    ):
        with pytest.raises(AssertionError):
            proof._assert_explicit_user_status(payload, "redis")


def test_authentication_uses_separate_proof_cas_when_fresh_runtime_has_no_runtime_packs(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o711)
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"archive")
    selection = proof.DockerHubImageSelection(archive, proof._file_sha256(archive), "sha256:" + "a" * 64)
    seen = []
    process = SimpleNamespace(require_bootable=lambda: None)
    image = SimpleNamespace(
        manifest_descriptor=SimpleNamespace(digest=selection.manifest_digest), config=SimpleNamespace(process=process)
    )

    class FakeArchive:
        def __init__(self, selected_archive, selected_manifest):
            assert (selected_archive, selected_manifest) == (archive, selection.manifest_digest)

        def snapshot(self, reference, cas):
            assert reference is None
            seen.append(cas)
            return SimpleNamespace(image=image)

    monkeypatch.setattr(proof, "LocalArchiveSource", FakeArchive)
    monkeypatch.setattr(proof, "SourceCAS", lambda path: path)
    assert proof._authenticate(selection, runtime) is process
    assert seen == [runtime / "proof-source-cas"]
    assert not (runtime / "state").exists()


def test_bounded_command_timeout_preserves_partial_stdout_stderr_and_metadata(tmp_path):
    environment = dict(os.environ)
    environment["PALIMPSEST_PROOF_EVIDENCE_DIR"] = str(tmp_path)
    command = [
        sys.executable,
        "-c",
        "import os,signal,sys,time; marker=sys.argv[1]; "
        "signal.signal(signal.SIGTERM,lambda *_: open(marker,'w').write('TERM')); "
        "print(f'partial-out pid={os.getpid()}',flush=True); "
        "print('partial-err',file=sys.stderr,flush=True); time.sleep(10)",
        str(tmp_path / "term-handler-ran"),
    ]
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        proof._bounded_command(command, environment=environment, timeout=0.5)
    assert b"partial-out" in caught.value.output
    assert b"partial-err" in caught.value.stderr
    child_pid = int(caught.value.output.decode().split("pid=")[1].split()[0])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not (tmp_path / "term-handler-ran").exists()
    metadata = list(tmp_path.glob("command-failure-*.json"))
    assert len(metadata) == 1
    report = json.loads(metadata[0].read_text())
    assert report["reason"] == "timeout" and report["timeout_seconds"] == 0.5
    assert b"partial-out" in next(tmp_path.glob("command-failure-*.stdout")).read_bytes()
    assert b"partial-err" in next(tmp_path.glob("command-failure-*.stderr")).read_bytes()


def test_bounded_command_overflow_terminates_child_and_preserves_bounded_output(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_MAX_COMMAND_OUTPUT", 1024)
    environment = dict(os.environ)
    environment["PALIMPSEST_PROOF_EVIDENCE_DIR"] = str(tmp_path)
    command = [
        sys.executable,
        "-c",
        "import sys,time; print('partial-err',file=sys.stderr,flush=True); sys.stdout.write('o'*4096); sys.stdout.flush(); time.sleep(10)",
    ]
    with pytest.raises(AssertionError, match="exceeded"):
        proof._bounded_command(command, environment=environment, timeout=2)
    metadata = list(tmp_path.glob("command-failure-*.json"))
    assert len(metadata) == 1 and json.loads(metadata[0].read_text())["reason"] == "output-overflow"
    assert len(next(tmp_path.glob("command-failure-*.stdout")).read_bytes()) <= 1024
    stderr = next(tmp_path.glob("command-failure-*.stderr")).read_bytes()
    assert b"partial-err" in stderr and len(stderr) <= 1024


def test_cleanup_failure_cannot_replace_timeout_after_partial_evidence(tmp_path, monkeypatch):
    environment = dict(os.environ)
    environment["PALIMPSEST_PROOF_EVIDENCE_DIR"] = str(tmp_path)
    command = [sys.executable, "-c", "import os,time; print(os.getpid(),flush=True); time.sleep(10)"]
    real_kill = proof._kill_owned

    def kill_then_report_failure(process):
        real_kill(process)
        raise OSError("injected after reap")

    monkeypatch.setattr(proof, "_kill_owned", kill_then_report_failure)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        proof._bounded_command(command, environment=environment, timeout=0.5)
    child_pid = int(caught.value.output)
    metadata = list(tmp_path.glob("command-failure-*.json"))
    assert len(metadata) == 1 and json.loads(metadata[0].read_text())["reason"] == "timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_console_readiness_refuses_overlimit_file_without_unbounded_read(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_MAX_CONSOLE_BYTES", 8)
    console = tmp_path / "console.log"
    console.write_bytes(b"123456789READY")
    with pytest.raises(pytest.fail.Exception, match="console exceeds 8 bytes"):
        proof._wait_console(console, b"READY", timeout=0.1)
