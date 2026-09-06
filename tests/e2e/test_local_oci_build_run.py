"""Opt-in product acceptance for a locally built OCI image becoming VM root `/`."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from palimpsest_local import state

pytestmark = [
    pytest.mark.oci_root_e2e,
    pytest.mark.skipif(
        os.environ.get("PALIMPSEST_OCI_ROOT_E2E") != "1",
        reason="set PALIMPSEST_OCI_ROOT_E2E=1 on the OCI-root KVM acceptance host",
    ),
]

_SUCCESS = "PALIMPSEST_OCI_ROOT_OK"
_EVIDENCE_LIMIT = 65536


def _bounded(value: str | bytes | None) -> str:
    encoded = value if isinstance(value, bytes) else (value or "").encode("utf-8", errors="replace")
    normalized = encoded[:_EVIDENCE_LIMIT].decode("utf-8", errors="replace").encode("utf-8")
    return normalized[:_EVIDENCE_LIMIT].decode("utf-8", errors="ignore")


def _record_command(
    directory: Path,
    label: str,
    arguments: list[str],
    call: Callable[[], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    evidence: dict[str, object] = {"arguments": arguments, "stream_limit_bytes": _EVIDENCE_LIMIT}
    primary: BaseException | None = None
    try:
        result = call()
        evidence.update(
            returncode=result.returncode,
            stdout=_bounded(result.stdout),
            stderr=_bounded(result.stderr),
        )
        return result
    except BaseException as exc:
        primary = exc
        evidence.update(error=_bounded(str(exc)), error_type=type(exc).__name__)
        if isinstance(exc, subprocess.TimeoutExpired):
            evidence.update(stdout=_bounded(exc.stdout), stderr=_bounded(exc.stderr))
        raise
    finally:
        try:
            (directory / (label + ".json")).write_text(json.dumps(evidence, ensure_ascii=True), encoding="utf-8")
        except Exception as exc:
            if primary is None:
                raise
            primary.add_note(
                f"Command evidence persistence failed ({label}): {type(exc).__name__}: {_bounded(str(exc))}"
            )


def _run(
    arguments: list[str], *, environment: dict[str, str], timeout: float = 300
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "palimpsest_local.cli", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )


def test_local_build_runs_detached_with_oci_root_as_vm_root(tmp_path: Path) -> None:
    """Consume a Palimpsest-built artifact on KVM, including Docker-present hosts."""
    artifact_value = os.environ.get("PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR")
    if not artifact_value:
        pytest.fail("PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR must name the transferred build-gate artifact")
    artifact_dir = Path(artifact_value).resolve(strict=True)
    archive = artifact_dir / "image.oci.tar"
    receipt_path = artifact_dir / "acceptance.json"
    assert archive.is_file() and receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {"archive_sha256", "manifest_digest", "marker", "platform", "schema"}
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v2"
    assert receipt["archive_sha256"] == f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"
    assert isinstance(receipt["manifest_digest"], str) and receipt["manifest_digest"].startswith("sha256:")
    marker = receipt["marker"]
    assert isinstance(marker, str) and marker.startswith("palimpsest-local-build-")
    assert isinstance(receipt["platform"], str) and receipt["platform"].startswith("linux/")
    run_name = "oci-root-e2e-" + uuid.uuid4().hex[:12]
    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": os.fspath(tmp_path / "xdg-config"),
        "XDG_STATE_HOME": os.fspath(tmp_path / "xdg-state"),
    }

    forbidden_tools = tmp_path / "forbidden-runtime-tools"
    forbidden_tools.mkdir()
    docker_audit = tmp_path / "docker-runtime-audit"
    docker_shim = forbidden_tools / "docker"
    docker_shim.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {os.fspath(docker_audit)!r}\nexit 97\n",
        encoding="utf-8",
    )
    docker_shim.chmod(0o755)
    local_docker_sockets = [
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path.home() / ".docker/run/docker.sock",
    ]
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        local_docker_sockets.append(Path(xdg_runtime) / "docker.sock")
    runtime_environment = {
        **environment,
        "DOCKER_HOST": "unix:///palimpsest-e2e-forbidden-docker.sock",
        "PATH": os.fspath(forbidden_tools) + os.pathsep + environment["PATH"],
    }
    virsh = shutil.which("virsh", path=runtime_environment["PATH"])
    assert virsh is not None, "OCI-root KVM acceptance requires virsh"
    libvirt_uri = os.environ.get("PALIMPSEST_OCI_ROOT_E2E_LIBVIRT_URI", "qemu:///system")
    roots = state.resolve_roots(runtime_environment)
    evidence = tmp_path / "command-evidence"
    evidence.mkdir()
    (evidence / "runtime.json").write_text(
        json.dumps(
            {
                "run_name": run_name,
                "state_root": os.fspath(roots.state),
                "docker_host": runtime_environment["DOCKER_HOST"],
                "observed_docker_sockets": [os.fspath(path) for path in local_docker_sockets if path.exists()],
                "docker_guard_scope": "PATH docker CLI and configured DOCKER_HOST; not all socket connections",
            }
        ),
        encoding="utf-8",
    )

    def command(label: str, arguments: list[str], timeout: float = 60):
        return _record_command(
            evidence, label, arguments, lambda: _run(arguments, environment=runtime_environment, timeout=timeout)
        )

    def domain_command(label: str, arguments: list[str]):
        argv = [virsh, "-c", libvirt_uri, *arguments]
        return _record_command(
            evidence,
            label,
            argv,
            lambda: subprocess.run(
                argv, check=False, capture_output=True, text=True, timeout=30, env=runtime_environment
            ),
        )

    primary: BaseException | None = None
    try:
        launched = command(
            "run",
            ["run", os.fspath(archive), "--name", run_name, "--backend", "kvm", "-d"],
            timeout=180,
        )
        assert launched.returncode == 0, launched.stderr
        domain = domain_command("domain-running", ["dominfo", run_name])
        assert domain.returncode == 0, domain.stderr
        assert "running" in domain.stdout.lower()
        proof_before_result = command("root-proof-before", ["oci", "root-proof", run_name])
        assert proof_before_result.returncode == 0, proof_before_result.stderr
        proof_before = json.loads(proof_before_result.stdout)
        executed = command(
            "exec",
            ["exec", run_name, "--", "/usr/local/bin/palimpsest-e2e-probe"],
            timeout=60,
        )
        assert executed.returncode == 0, executed.stderr
        match = re.fullmatch(rf"{re.escape(_SUCCESS)}:{re.escape(marker)}:(0|[1-9][0-9]*):([1-9][0-9]*)\n", executed.stdout)
        assert match is not None
        device_text, inode_text = match.groups()
        device, inode = int(device_text), int(inode_text)
        assert device <= (1 << 64) - 1 and inode <= (1 << 64) - 1
        denied = command(
            "pid1-root-denied",
            [
                "exec", run_name, "--", "/bin/sh", "-c",
                "message=$(cat /proc/1/root/palimpsest-e2e-root-marker 2>&1); status=$?; "
                "if test \"$status\" -eq 0; then exit 95; fi; "
                "case \"$message\" in *'Permission denied'*) exit 0;; *) exit 96;; esac",
            ],
        )
        assert denied.returncode == 0, denied.stderr
        proof_result = command("root-proof-after", ["oci", "root-proof", run_name])
        assert proof_result.returncode == 0, proof_result.stderr
        proof = json.loads(proof_result.stdout)
        assert proof["schema"] == "palimpsest.oci-root-proof.v1"
        assert proof["run"]["name"] == run_name
        assert {key: proof[key] for key in ("run", "boot", "domain")} == {
            key: proof_before[key] for key in ("run", "boot", "domain")
        }
        assert proof_before["root_identity"] == proof["root_identity"]
        assert proof["root_identity"] == {
            "schema": "palimpsest.oci-root-identity.v1",
            "pid": 1,
            "filesystem": "overlayfs",
            "device": device,
            "inode": inode,
        }
    except BaseException as exc:
        primary = exc

    checks: dict[str, str] = {}

    def check(label: str, operation: Callable[[], None]) -> None:
        try:
            operation()
            checks[label] = "passed"
        except Exception as exc:
            checks[label] = f"failed: {type(exc).__name__}: {_bounded(str(exc))}"

    def successful_command(label: str) -> None:
        result = command(label, [label, run_name])
        assert result.returncode == 0, result.stderr

    def domain_absent() -> None:
        result = domain_command("domain-removed", ["list", "--all", "--name"])
        assert result.returncode == 0, result.stderr
        assert run_name not in result.stdout.splitlines(), "removing the run must undefine its libvirt domain"

    def run_absent() -> None:
        # lexists also rejects a dangling replacement entry at the effective root.
        assert not os.path.lexists(roots.runs / run_name), f"run entry remains in effective state root {roots.state}"

    def archive_preserved() -> None:
        assert archive.is_file(), "removing a VM must not remove its immutable local source image"
        assert receipt["archive_sha256"] == f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"

    def docker_not_invoked() -> None:
        assert not docker_audit.exists(), "runtime commands must not invoke the guarded Docker CLI"

    check("stop", lambda: successful_command("stop"))
    check("rm", lambda: successful_command("rm"))
    check("domain-absent", domain_absent)
    check("run-absent", run_absent)
    check("archive-preserved", archive_preserved)
    check("docker-cli-not-invoked", docker_not_invoked)
    summary = f"Gate 2 evidence: {evidence}\n" + "\n".join(f"{label}: {result}" for label, result in checks.items())
    try:
        (evidence / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
    except Exception as exc:
        failure = f"Cleanup evidence persistence failed: {type(exc).__name__}: {_bounded(str(exc))}"
        if primary is None:
            exc.add_note(summary)
            raise
        primary.add_note(failure)
    if primary is not None:
        primary.add_note(summary)
        raise primary.with_traceback(primary.__traceback__)
    assert all(result == "passed" for result in checks.values()), summary
