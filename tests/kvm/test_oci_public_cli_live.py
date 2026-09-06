"""Opt-in public local OCI CLI smoke; deliberately not a Gate 2 replacement.

No private launcher, test ACL broker, or monkeypatch participates. Use a Linux
system-site-packages venv with this checkout installed and libvirt importable.
Failures preserve the exact runtime/evidence directory printed by this test.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest

_ENABLE = "PALIMPSEST_OCI_PUBLIC_CLI_LIVE"
_HOST_KEYS = ("KERNEL", "KERNEL_DIGEST", "KERNEL_CONFIG", "KERNEL_CONFIG_DIGEST", "PACKER")
_ROOT_MARKER = b"PALIMPSEST_PUBLIC_OCI_ROOT"
_READY_MARKER = b"PALIMPSEST_PUBLIC_SERVICE_READY"
_STOP_MARKER = b"PALIMPSEST_PUBLIC_STOP_OBSERVED"


def _require_host():
    if os.environ.get(_ENABLE) != "1":
        pytest.skip(f"set {_ENABLE}=1 on the qualified Linux/x86_64 KVM host")
    assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
    for key in _HOST_KEYS:
        assert os.environ.get("PALIMPSEST_OCI_" + key), f"missing PALIMPSEST_OCI_{key}"
    assert shutil.which("cc"), "the native smoke fixture needs host cc"
    assert Path("/dev/kvm").exists()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _blob(layout, content, media_type):
    digest = hashlib.sha256(content).hexdigest()
    (layout / "blobs" / "sha256" / digest).write_bytes(content)
    return {"digest": "sha256:" + digest, "mediaType": media_type, "size": len(content)}


def _image_layout(parent, executable, mode):
    layout = parent / (mode + "-layout")
    (layout / "blobs" / "sha256").mkdir(parents=True)
    contents = io.BytesIO()
    with tarfile.open(fileobj=contents, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in ("bin", "dev", "proc", "sys", "run", "tmp", "etc"):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.DIRTYPE
            entry.mode = 0o755
            archive.addfile(entry)
        for name, payload, permissions in (
            ("bin/public-proof", executable.read_bytes(), 0o755),
            ("oci-public-root", b"OCI_IS_REAL_ROOT\n", 0o444),
            ("etc/passwd", b"root:x:0:0:root:/:/bin/public-proof\n", 0o644),
            ("etc/group", b"root:x:0:\n", 0o644),
        ):
            entry = tarfile.TarInfo(name)
            entry.mode = permissions
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    layer = _blob(layout, contents.getvalue(), "application/vnd.oci.image.layer.v1.tar")
    config = _blob(
        layout,
        _json(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": [layer["digest"]]},
                "config": {
                    "Entrypoint": ["/bin/public-proof"],
                    "Cmd": [mode],
                    "Env": ["PATH=/bin"],
                    "WorkingDir": "/",
                    "User": "0:0",
                    "StopSignal": "SIGTERM",
                },
            }
        ),
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = _blob(
        layout,
        _json(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config,
                "layers": [layer],
            }
        ),
        "application/vnd.oci.image.manifest.v1+json",
    )
    (layout / "oci-layout").write_bytes(_json({"imageLayoutVersion": "1.0.0"}))
    (layout / "index.json").write_bytes(
        _json({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [manifest]})
    )
    return layout


def _cli(environment, *args, timeout=90):
    return subprocess.run(
        [sys.executable, "-m", "palimpsest_local.cli", *map(str, args)],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _success(result):
    assert result.returncode == 0, (
        result.returncode,
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )


def _wait_console(path, marker, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and marker in path.read_bytes():
            return
        time.sleep(0.05)
    pytest.fail(f"missing public workload marker {marker!r}; preserve {path}")


def test_public_fixture_layout_is_a_bootable_authenticated_local_image(tmp_path):
    from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS

    executable = tmp_path / "proof"
    executable.write_bytes(b"fixture ELF bytes are compiled only on the native host")
    for mode in ("foreground", "service"):
        layout = _image_layout(tmp_path, executable, mode)
        snapshot = LocalLayoutSource(layout.resolve()).snapshot(None, SourceCAS((tmp_path / "cas").resolve()))
        process = snapshot.image.config.process
        assert process.argv == ("/bin/public-proof", mode)
        assert process.bootable and process.stop_signal == 15
        assert (process.user.user, process.user.group) == ("0", "0")


def test_public_oci_foreground_detached_stop_and_rm():
    _require_host()
    parent = Path("/tmp") / ("p-pub-" + uuid.uuid4().hex[:8])
    assert not parent.exists()
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONNOUSERSITE"] = "1"
    print(f"public OCI CLI evidence: {parent}", flush=True)
    initialized = _cli(environment, "oci", "init-runtime", parent)
    _success(initialized)
    assert initialized.stdout == f"PALIMPSEST_STATE_HOME={parent}/state\n".encode()
    owned = parent.lstat()
    environment["PALIMPSEST_STATE_HOME"] = str(parent / "state")
    environment["XDG_CONFIG_HOME"] = str(parent / "config")
    completed = False
    try:
        executable = parent / "public-proof"
        source = Path(__file__).with_name("assets") / "public-cli-proof.c"
        compiled = subprocess.run(
            [
                "cc",
                "-Os",
                "-nostdlib",
                "-static",
                "-no-pie",
                "-fno-builtin",
                "-fno-stack-protector",
                "-fno-asynchronous-unwind-tables",
                "-Wl,--build-id=none",
                "-o",
                str(executable),
                str(source),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        _success(compiled)
        foreground = _image_layout(parent, executable, "foreground")
        service = _image_layout(parent, executable, "service")
        archive = parent / "service.oci.tar"
        with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as bundle:
            for entry in sorted(service.rglob("*")):
                bundle.add(entry, arcname=str(entry.relative_to(service)), recursive=False)
        result = _cli(
            environment,
            "run",
            foreground,
            "--runtime-kind",
            "oci-root",
            "--name",
            "public-fg",
            "--memory",
            "512",
            "--vcpus",
            "1",
            timeout=120,
        )
        (parent / "foreground.stdout").write_bytes(result.stdout)
        (parent / "foreground.stderr").write_bytes(result.stderr)
        assert result.returncode == 23, (result.returncode, result.stderr.decode(errors="replace"))
        assert _ROOT_MARKER in result.stdout
        _success(_cli(environment, "rm", "public-fg"))
        assert not (parent / "state" / "runs" / "public-fg").exists()
        detached = _cli(
            environment, "run", archive, "--name", "public-bg", "--memory", "512", "--vcpus", "1", "-d", timeout=120
        )
        (parent / "detached.stdout").write_bytes(detached.stdout)
        (parent / "detached.stderr").write_bytes(detached.stderr)
        _success(detached)
        assert detached.stdout == b"public-bg\n"
        console = parent / "state" / "runs" / "public-bg" / "io" / "console.log"
        _wait_console(console, _READY_MARKER)
        assert _ROOT_MARKER in console.read_bytes()
        _success(_cli(environment, "stop", "public-bg"))
        _wait_console(console, _STOP_MARKER)
        _success(_cli(environment, "rm", "public-bg"))
        assert not (parent / "state" / "runs" / "public-bg").exists()
        foreground_output = parent / "interrupt.stdout"
        with foreground_output.open("wb") as output, (parent / "interrupt.stderr").open("wb") as errors:
            interrupted = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "palimpsest_local.cli",
                    "run",
                    str(archive),
                    "--name",
                    "public-int",
                    "--memory",
                    "512",
                    "--vcpus",
                    "1",
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
            )
            print(f"public foreground SIGINT CLI pid: {interrupted.pid}", flush=True)
            # Wait for output delivered through the real foreground session,
            # not just a guest marker preceding the CLI's session attachment.
            _wait_console(foreground_output, _READY_MARKER, timeout=120)
            assert interrupted.poll() is None
            interrupted.send_signal(signal.SIGINT)
            assert interrupted.wait(timeout=45) == 42
        assert _STOP_MARKER in foreground_output.read_bytes()
        _success(_cli(environment, "rm", "public-int"))
        assert not tuple((parent / "state" / "runs").iterdir())
        completed = True
    finally:
        if completed:
            visible = parent.lstat()
            assert stat.S_ISDIR(visible.st_mode) and visible.st_uid == os.geteuid()
            assert (visible.st_dev, visible.st_ino) == (owned.st_dev, owned.st_ino)
            shutil.rmtree(parent)
        else:
            print(f"public OCI CLI failure evidence preserved: {parent}", flush=True)
