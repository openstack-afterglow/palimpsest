"""Guest runner resume path, run as a real subprocess against fake mount tooling.

The runner is the exact `GUEST_RUNNER` bytes, with only its fixed state directory, architecture
check and chroot hook redirected so it can run unprivileged. `mount`, `umount`, `losetup`,
`truncate` and `mkfs.ext4` are recording fakes. This proves the runner's decisions (what it
verifies, what it refuses to recreate, whom it reports to). It does NOT qualify a real kernel
OverlayFS/SquashFS/loop mount or a real Nova unshelve.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from palimpsest_hub.services.instance_bootstrap import GUEST_RUNNER

INSTANCE_ID = "11111111-2222-4333-8444-555555555555"
BOOT_CAPABILITY = "bootstrap-capability-" + "b" * 24
SESSION_CAPABILITY = "session-capability-" + "s" * 24
LAYER = b"fixture-squashfs-bytes" * 64
LAYER_DIGEST = "sha256:" + hashlib.sha256(LAYER).hexdigest()
WRITABLE_BYTES = 1024**3
MARKER = b"UPPER-MARKER-MUST-SURVIVE"


class Collector(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _record(self, body=b""):
        self.server.requests.append(
            {"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )

    def do_GET(self):
        self._record()
        if self.path.endswith("/layers/" + LAYER_DIGEST):
            self.send_response(200)
            self.send_header("Content-Length", str(len(LAYER)))
            self.end_headers()
            self.wfile.write(LAYER)
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._record(body)
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def hub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


FAKE_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
case "$(basename "$0")" in
  losetup)
    case "$*" in
      *--find*) echo /dev/loop9 ;;
      *--output*) echo "$FAKE_BACKING" ;;
    esac ;;
esac
exit 0
"""


class Guest:
    def __init__(self, tmp_path: Path, server, free_bytes: int | None = None):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path / "state"
        self.root.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for tool in ("mount", "umount", "losetup", "mkfs.ext4", "truncate"):
            (self.bin / tool).write_text(FAKE_TOOL)
            (self.bin / tool).chmod(0o755)
        self.log = tmp_path / "tools.log"
        self.log.write_text("")
        self.server = server
        source = GUEST_RUNNER.replace("/var/lib/palimpsest-instance", str(self.root))
        source = source.replace("('x86_64', 'amd64')", "('x86_64', 'amd64', 'arm64', 'aarch64')")
        source = source.replace("def enter_runtime():", "def enter_runtime():\n    return", 1)
        assert str(self.root) in source and "return\n" in source and "'arm64'" in source
        assert "/var/lib/palimpsest-instance" not in source
        if free_bytes is not None:
            fake = f"type('S', (), {{'f_bavail': {free_bytes}, 'f_frsize': 1}})()"
            assert "os.statvfs(ROOT)" in source
            source = source.replace("os.statvfs(ROOT)", fake)
        (self.root / "runner.py").write_text(source)
        self.spec = {
            "instance_id": INSTANCE_ID,
            "origin": f"http://127.0.0.1:{server.server_port}",
            "mode": "oci-chroot",
            "layers": [{"digest": LAYER_DIGEST, "size_bytes": len(LAYER)}],
            "writable_bytes": WRITABLE_BYTES,
            "runtime": {},
            "command": ["/bin/sh", "-c", "sleep 1"],
            "readiness_argv": ["/bin/sh", "-c", "exit 0"],
        }

    def write_session(self, instance_id=INSTANCE_ID):
        state = {"capability": SESSION_CAPABILITY, "instance_id": instance_id, "origin": self.spec["origin"]}
        (self.root / "session.json").write_text(json.dumps(state))

    def first_boot_files(self):
        spec = {**self.spec, "capability": BOOT_CAPABILITY, "expires_at": 4_000_000_000}
        (self.root / "spec.json").write_text(json.dumps(spec))

    def retain_state(self, *, layer=LAYER, writable_size=WRITABLE_BYTES, upper=True, work=True):
        (self.root / "resume.json").write_text(json.dumps(self.spec))
        (self.root / "layer-0.squashfs").write_bytes(layer)
        with open(self.root / "writable.ext4", "wb") as backing:
            backing.write(MARKER)
            backing.truncate(writable_size)
        (self.root / "writable").mkdir()
        for name, present in (("upper", upper), ("work", work)):
            if present:
                (self.root / "writable" / name).mkdir()
        # The unit is enabled, so the original (long expired) input is still on disk.
        (self.root / "spec.json").write_text(json.dumps({**self.spec, "capability": BOOT_CAPABILITY, "expires_at": 1}))

    def run(self):
        env = {
            "PATH": str(self.bin) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
            "FAKE_LOG": str(self.log),
            "FAKE_BACKING": str(self.root / "writable.ext4"),
            "HOME": str(self.root),
        }
        return subprocess.run(
            [sys.executable, str(self.root / "runner.py")], env=env, capture_output=True, timeout=60, check=False
        )

    def tools(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def puts(self):
        return [item for item in self.server.requests if item["method"] == "PUT"]

    def backing_head(self):
        with open(self.root / "writable.ext4", "rb") as backing:
            return backing.read(len(MARKER))


def tool_names(guest: Guest) -> list[str]:
    return [line.split()[0] for line in guest.tools()]


def test_resume_remounts_the_retained_root_without_downloading_or_formatting(tmp_path, hub_server):
    guest = Guest(tmp_path, hub_server)
    guest.write_session()
    guest.retain_state()
    outcome = guest.run()
    assert outcome.returncode == 0, outcome.stderr.decode()
    assert "mkfs.ext4" not in tool_names(guest) and "truncate" not in tool_names(guest)
    assert guest.backing_head() == MARKER
    assert not [item for item in hub_server.requests if item["method"] == "GET"]
    assert any(line.startswith("losetup --find --show " + str(guest.root / "writable.ext4")) for line in guest.tools())
    assert any("upperdir=writable/upper,workdir=writable/work" in line for line in guest.tools())
    (report,) = guest.puts()
    assert report["path"] == f"/v1/instance-bootstrap/{INSTANCE_ID}/resume-result"
    assert report["auth"] == "Bearer " + SESSION_CAPABILITY
    assert json.loads(report["body"])["status"] == "ready"
    assert (guest.root / "resume.json").exists() and not (guest.root / "spec.json").exists()


@pytest.mark.parametrize(
    "damage",
    [
        {"layer": LAYER[:-1] + b"X"},
        {"layer": LAYER[:-1]},
        {"upper": False},
        {"work": False},
        {"writable_size": WRITABLE_BYTES + 4096},
    ],
    ids=["layer-bytes-changed", "layer-truncated", "upper-missing", "work-missing", "writable-size-changed"],
)
def test_resume_refuses_damaged_retained_state_and_never_recreates_it(tmp_path, hub_server, damage):
    guest = Guest(tmp_path, hub_server)
    guest.write_session()
    guest.retain_state(**damage)
    guest.run()
    assert "mkfs.ext4" not in tool_names(guest) and "truncate" not in tool_names(guest)
    assert guest.backing_head() == MARKER
    assert not any("overlay" in line for line in guest.tools())
    (report,) = guest.puts()
    assert report["path"].endswith("/resume-result")
    body = json.loads(report["body"])
    assert body["status"] == "error" and body["error_code"] == "guest_execution_failed"


@pytest.mark.skipif(not hasattr(os, "posix_fallocate"), reason="the first-boot allocation needs Linux")
def test_first_boot_retains_exactly_what_a_resume_needs_and_a_resume_then_uses_it(tmp_path, hub_server):
    guest = Guest(tmp_path, hub_server)
    guest.write_session()
    guest.first_boot_files()
    first = guest.run()
    assert first.returncode == 0, first.stderr.decode()
    (report,) = guest.puts()
    assert report["path"].endswith("/result") and report["auth"] == "Bearer " + BOOT_CAPABILITY
    kept = json.loads((guest.root / "resume.json").read_text())
    assert "capability" not in kept and "expires_at" not in kept and kept["layers"] == guest.spec["layers"]
    assert oct((guest.root / "resume.json").stat().st_mode & 0o777) == "0o600"
    assert not (guest.root / "resume.json.tmp").exists()
    # Reboot: the retained layer and writable file are exactly what the first boot left.
    hub_server.requests.clear()
    guest.log.write_text("")
    (guest.root / "writable.ext4").write_bytes(MARKER + (guest.root / "writable.ext4").read_bytes()[len(MARKER) :])
    (guest.root / "spec.json").write_text(json.dumps({**guest.spec, "capability": BOOT_CAPABILITY, "expires_at": 1}))
    for name in ("lower-0", "merged"):
        # The first run leaves its private-namespace mountpoints behind, as a real boot does.
        assert (guest.root / name).is_dir()
    second = guest.run()
    assert second.returncode == 0, second.stderr.decode()
    assert "mkfs.ext4" not in tool_names(guest) and guest.backing_head() == MARKER
    (report,) = guest.puts()
    assert report["path"].endswith("/resume-result") and report["auth"] == "Bearer " + SESSION_CAPABILITY
    assert not [item for item in hub_server.requests if item["method"] == "GET"]


def test_a_service_instance_without_the_session_reporter_retains_nothing(tmp_path, hub_server):
    if not hasattr(os, "posix_fallocate"):
        pytest.skip("the first-boot allocation needs Linux")
    guest = Guest(tmp_path, hub_server)
    guest.first_boot_files()
    assert guest.run().returncode == 0
    assert not (guest.root / "resume.json").exists()


def test_resume_does_not_demand_first_boot_free_space_but_a_first_boot_does(tmp_path, hub_server):
    # The retained layers and the fully allocated writable file already occupy the disk.
    resumed = Guest(tmp_path / "resume", hub_server, free_bytes=0)
    resumed.write_session()
    resumed.retain_state()
    assert resumed.run().returncode == 0
    assert json.loads(resumed.puts()[0]["body"])["status"] == "ready"
    hub_server.requests.clear()
    first = Guest(tmp_path / "first", hub_server, free_bytes=0)
    first.write_session()
    first.first_boot_files()
    first.run()
    (report,) = first.puts()
    assert json.loads(report["body"])["status"] == "error" and not first.tools()


def test_resume_refuses_a_planted_symlink_in_place_of_a_state_directory(tmp_path, hub_server):
    guest = Guest(tmp_path, hub_server)
    guest.write_session()
    guest.retain_state()
    (guest.root / "lower-0").symlink_to(tmp_path)
    guest.run()
    assert not any("squashfs" in line for line in guest.tools())
    (report,) = guest.puts()
    assert json.loads(report["body"])["status"] == "error"


def test_resume_state_from_another_instance_or_origin_is_not_used(tmp_path, hub_server):
    guest = Guest(tmp_path, hub_server)
    guest.write_session(instance_id="99999999-2222-4333-8444-555555555555")
    guest.retain_state()
    assert guest.run().returncode != 0
    assert not guest.puts() and not guest.tools()
