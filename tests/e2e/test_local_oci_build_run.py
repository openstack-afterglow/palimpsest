"""Opt-in product acceptance for a locally built OCI image becoming VM root `/`."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.oci_root_e2e,
    pytest.mark.skipif(
        os.environ.get("PALIMPSEST_OCI_ROOT_E2E") != "1",
        reason="set PALIMPSEST_OCI_ROOT_E2E=1 on the OCI-root KVM acceptance host",
    ),
]

_SUCCESS = "PALIMPSEST_OCI_ROOT_OK"


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
    """Consume a Palimpsest-built artifact on a Docker-daemonless KVM host."""
    artifact_value = os.environ.get("PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR")
    if not artifact_value:
        pytest.fail("PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR must name the transferred build-gate artifact")
    artifact_dir = Path(artifact_value).resolve(strict=True)
    archive = artifact_dir / "image.oci.tar"
    receipt_path = artifact_dir / "acceptance.json"
    assert archive.is_file() and receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert set(receipt) == {"archive_sha256", "manifest_digest", "marker", "platform", "schema"}
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v1"
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
    assert not any(path.exists() for path in local_docker_sockets), (
        "OCI-root runtime acceptance requires a Docker-daemonless KVM host; build through a remote network-none builder"
    )
    runtime_environment = {
        **environment,
        "DOCKER_HOST": "unix:///palimpsest-e2e-forbidden-docker.sock",
        "PATH": os.fspath(forbidden_tools) + os.pathsep + environment["PATH"],
    }
    virsh = shutil.which("virsh", path=runtime_environment["PATH"])
    assert virsh is not None, "OCI-root KVM acceptance requires virsh"
    libvirt_uri = os.environ.get("PALIMPSEST_OCI_ROOT_E2E_LIBVIRT_URI", "qemu:///system")

    try:
        launched = _run(
            ["run", os.fspath(archive), "--name", run_name, "--backend", "kvm", "-d"],
            environment=runtime_environment,
            timeout=180,
        )
        assert launched.returncode == 0, launched.stderr
        domain = subprocess.run(
            [virsh, "-c", libvirt_uri, "dominfo", run_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=runtime_environment,
        )
        assert domain.returncode == 0, domain.stderr
        assert "running" in domain.stdout.lower()
        executed = _run(
            ["exec", run_name, "--", "/usr/local/bin/palimpsest-e2e-probe"],
            environment=runtime_environment,
            timeout=60,
        )
        assert executed.returncode == 0, executed.stderr
        assert executed.stdout.strip() == f"{_SUCCESS}:{marker}"
        assert not docker_audit.exists(), "OCI-root run/exec must not delegate execution to Docker"
    finally:
        stopped = _run(["stop", run_name], environment=runtime_environment, timeout=60)
        removed = _run(["rm", run_name], environment=runtime_environment, timeout=60)

    assert stopped.returncode == 0, stopped.stderr
    assert removed.returncode == 0, removed.stderr
    removed_domain = subprocess.run(
        [virsh, "-c", libvirt_uri, "dominfo", run_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=runtime_environment,
    )
    assert removed_domain.returncode != 0, "removing the run must undefine its libvirt domain"

    assert archive.is_file(), "removing a VM must not remove its immutable local source image"
    assert not (tmp_path / "xdg-state" / "palimpsest" / "runs" / run_name).exists()
