"""Opt-in public CLI exec proof; run only after native engine qualification.

This is intentionally separate from unchanged Gate 2, whose PID 1 root probe
has a distinct supervisor-isolation acceptance conflict.
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


def test_public_exec_preserves_literal_argv_split_streams_exit_and_vm_lifecycle():
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    image_value = os.environ.get("PALIMPSEST_OCI_EXEC_LIVE_IMAGE")
    assert image_value, "PALIMPSEST_OCI_EXEC_LIVE_IMAGE must name the Palimpsest-built OCI archive"
    archive = Path(image_value).resolve(strict=True)
    assert archive.is_file()
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = json.loads(archive.with_name("acceptance.json").read_text())
    assert receipt["schema"] == "palimpsest.oci-root-build-run-acceptance.v1"
    assert receipt["archive_sha256"] == "sha256:" + archive_digest
    marker = receipt["marker"]
    assert type(marker) is str and re.fullmatch("palimpsest-local-build-[0-9a-f]{32}", marker)
    parent = Path("/tmp") / ("p-execcli-" + uuid.uuid4().hex[:8])
    assert not parent.exists()
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    print(f"public OCI exec evidence: {parent}", flush=True)
    _success(_cli(environment, "oci", "init-runtime", parent))
    owned = parent.lstat()
    environment["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    environment["XDG_CONFIG_HOME"] = str(parent / "config")
    name = "exec-cli"
    completed = False
    try:
        launched = _cli(environment, "run", archive, "--name", name, "-d", timeout=180)
        (parent / "launch.stdout").write_bytes(launched.stdout)
        (parent / "launch.stderr").write_bytes(launched.stderr)
        _success(launched)
        assert launched.stdout == (name + "\n").encode()

        def execute(label, *argv):
            result = _cli(environment, "exec", name, "--", *argv, timeout=60)
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
        missing = execute("missing-command", "/palimpsest-no-such-executable")
        assert missing.returncode == 127 and missing.stdout == b""
        result = execute("after-error", "/bin/sh", "-c", "printf 'still-running'")
        _success(result)
        assert result.stdout == b"still-running" and result.stderr == b""

        virsh = shutil.which("virsh", path=environment.get("PATH"))
        assert virsh is not None

        def domain_info():
            return subprocess.run(
                [virsh, "-c", "qemu:///system", "dominfo", name],
                env=environment,
                capture_output=True,
                check=False,
                timeout=15,
            )

        running = domain_info()
        _success(running)
        assert b"running" in running.stdout.lower()
        _success(_cli(environment, "stop", name, timeout=60))
        _success(_cli(environment, "rm", name, timeout=60))
        assert domain_info().returncode != 0
        assert not (parent / "state" / "runs" / name).exists()
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
