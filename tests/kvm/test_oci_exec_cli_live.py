"""Opt-in public CLI exec and v2 root-proof checks after native qualification.

The baked application proves its own root identity; a separate direct PID 1
root access attempt must remain denied by the unchanged supervisor isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from .test_oci_public_cli_live import _cli, _success

pytestmark = [
    pytest.mark.kvm,
    pytest.mark.skipif(
        os.environ.get("PALIMPSEST_OCI_EXEC_CLI_LIVE") != "1",
        reason="set PALIMPSEST_OCI_EXEC_CLI_LIVE=1 after public OCI EXEC capability qualification",
    ),
]


@dataclass(frozen=True, slots=True)
class _ExecCLIProofTarget:
    parent: Path
    name: str

    def launch(self, environment, archive):
        return _cli(environment, "run", archive, "--name", self.name, "-d", timeout=180)

    def exec_status(self, environment):
        return _cli(environment, "oci", "exec-status", self.name)

    def root_proof(self, environment):
        return _cli(environment, "oci", "root-proof", self.name)

    def execute(self, environment, *argv):
        return _cli(environment, "exec", self.name, "--", *argv, timeout=60)

    def domain_info(self, environment, virsh):
        return subprocess.run(
            [virsh, "-c", "qemu:///system", "dominfo", self.name],
            env=environment,
            capture_output=True,
            check=False,
            timeout=15,
        )

    def stop(self, environment):
        return _cli(environment, "stop", self.name, timeout=60)

    def remove(self, environment):
        return _cli(environment, "rm", self.name, timeout=60)

    @property
    def run_state(self):
        return self.parent / "state" / "runs" / self.name


def _fresh_exec_cli_target():
    suffix = uuid.uuid4().hex[:8]
    return _ExecCLIProofTarget(
        parent=Path("/tmp") / ("p-execcli-" + suffix),
        name="exec-cli-" + suffix,
    )


def test_public_exec_preserves_literal_argv_split_streams_exit_and_vm_lifecycle():
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    image_value = os.environ.get("PALIMPSEST_OCI_EXEC_LIVE_IMAGE")
    assert image_value, "PALIMPSEST_OCI_EXEC_LIVE_IMAGE must name the Palimpsest-built OCI archive"
    archive = Path(image_value).resolve(strict=True)
    assert archive.is_file()
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = json.loads(archive.with_name("acceptance.json").read_text())
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v2"
    assert receipt["archive_sha256"] == "sha256:" + archive_digest
    marker = receipt["marker"]
    assert type(marker) is str and re.fullmatch("palimpsest-local-build-[0-9a-f]{32}", marker)
    target = _fresh_exec_cli_target()
    parent = target.parent
    assert not parent.exists()
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    print(f"public OCI exec evidence: {parent}", flush=True)
    _success(_cli(environment, "oci", "init-runtime", parent))
    owned = parent.lstat()
    environment["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    environment["XDG_CONFIG_HOME"] = str(parent / "config")
    name = target.name
    completed = False
    try:
        launched = target.launch(environment, archive)
        (parent / "launch.stdout").write_bytes(launched.stdout)
        (parent / "launch.stderr").write_bytes(launched.stderr)
        _success(launched)
        assert launched.stdout == (name + "\n").encode()
        status_before = target.exec_status(environment)
        _success(status_before)
        assert json.loads(status_before.stdout) == {
            "schema": "palimpsest.oci-exec-status.v1",
            "state": "ready",
            "occupied": False,
            "guidance": "Ready and unoccupied is a point-in-time observation, not a guarantee the next exec will succeed.",
        }
        proof_before = target.root_proof(environment)
        _success(proof_before)
        before_report = json.loads(proof_before.stdout)

        def execute(label, *argv):
            result = target.execute(environment, *argv)
            (parent / (label + ".stdout")).write_bytes(result.stdout)
            (parent / (label + ".stderr")).write_bytes(result.stderr)
            return result

        literal = "literal $HOME; $(uname) with spaces"
        result = execute(
            "split",
            "/bin/sh",
            "-c",
            'printf "%s\\n" "$1"; printf "separate guest stderr\\n" >&2; exit 17',
            "exec-probe",
            literal,
        )
        assert result.returncode == 17, result.stderr.decode(errors="replace")
        assert result.stdout == (literal + "\n").encode()
        assert result.stderr == b"separate guest stderr\n"

        result = execute(
            "image-root",
            "/bin/sh",
            "-c",
            f'test "$(cat /palimpsest-e2e-root-marker)" = {marker} && '
            f'test "$(cat /proc/self/root/palimpsest-e2e-root-marker)" = {marker} && '
            f"printf 'PUBLIC_EXEC_IMAGE_ROOT_OK:{marker}\\n'",
        )
        _success(result)
        assert result.stdout == f"PUBLIC_EXEC_IMAGE_ROOT_OK:{marker}\n".encode() and result.stderr == b""
        probe = execute("root-probe", "/usr/local/bin/palimpsest-e2e-probe")
        _success(probe)
        matched = re.fullmatch(
            rb"PALIMPSEST_OCI_ROOT_OK:" + re.escape(marker.encode()) + rb":(0|[1-9][0-9]*):([1-9][0-9]*)\n",
            probe.stdout,
        )
        assert matched is not None
        device, inode = (int(value) for value in matched.groups())
        assert device <= (1 << 64) - 1 and inode <= (1 << 64) - 1
        isolation = execute(
            "pid1-isolation",
            "/bin/sh",
            "-c",
            "LC_ALL=C cat /proc/1/root/palimpsest-e2e-root-marker",
        )
        assert isolation.returncode != 0 and isolation.stdout == b""
        assert b"Permission denied" in isolation.stderr
        missing = execute("missing-command", "/palimpsest-no-such-executable")
        assert missing.returncode == 127 and missing.stdout == b""
        result = execute("after-error", "/bin/sh", "-c", "printf 'still-running'")
        _success(result)
        assert result.stdout == b"still-running" and result.stderr == b""
        status_after = target.exec_status(environment)
        _success(status_after)
        assert json.loads(status_after.stdout) == {
            "schema": "palimpsest.oci-exec-status.v1",
            "state": "ready",
            "occupied": False,
            "guidance": "Ready and unoccupied is a point-in-time observation, not a guarantee the next exec will succeed.",
        }
        proof_after = target.root_proof(environment)
        _success(proof_after)
        after_report = json.loads(proof_after.stdout)
        assert {key: before_report[key] for key in ("run", "boot", "domain")} == {
            key: after_report[key] for key in ("run", "boot", "domain")
        }
        assert before_report["root_identity"] == after_report["root_identity"]
        assert after_report["root_identity"]["device"] == device
        assert after_report["root_identity"]["inode"] == inode

        virsh = shutil.which("virsh", path=environment.get("PATH"))
        assert virsh is not None

        def domain_info():
            return target.domain_info(environment, virsh)

        running = domain_info()
        _success(running)
        assert b"running" in running.stdout.lower()
        _success(target.stop(environment))
        _success(target.remove(environment))
        assert domain_info().returncode != 0
        assert not target.run_state.exists()
        assert archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest() == archive_digest
        completed = True
    finally:
        if completed:
            visible = parent.lstat()
            assert stat.S_ISDIR(visible.st_mode) and visible.st_uid == os.geteuid()
            assert (visible.st_dev, visible.st_ino) == (owned.st_dev, owned.st_ino)
            shutil.rmtree(parent)
        else:
            print(f"public OCI exec failure evidence preserved: {parent}", flush=True)
