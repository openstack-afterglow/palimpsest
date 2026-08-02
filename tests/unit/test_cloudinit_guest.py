"""Unit tests for palimpsest_local.cloudinit and palimpsest_local.guest."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from palimpsest_local import cloudinit, guest
from palimpsest_local.errors import LifecycleError as GuestError


def test_meta_data_generation():
    md = cloudinit.build_meta_data("run-123456", hostname="test-guest")
    assert md == "instance-id: run-123456\nlocal-hostname: test-guest\n"


def test_meta_data_rejects_multiline_or_empty():
    with pytest.raises(GuestError):
        cloudinit.build_meta_data("bad\nid")
    with pytest.raises(GuestError):
        cloudinit.build_meta_data("", hostname="valid")


def test_user_data_structure():
    client_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIClientKeyExample client@host"
    host_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKeyExample host@guest"
    host_priv = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    activation = "set -euo pipefail\necho activating\n"

    ud = cloudinit.build_user_data(
        client_public_key=client_pub,
        host_private_key=host_priv,
        host_public_key=host_pub,
        activation_script=activation,
    )

    assert ud.startswith("#cloud-config\n")
    assert "name: ubuntu" in ud
    assert "sudo: ALL=(ALL) NOPASSWD:ALL" in ud
    assert client_pub in ud
    assert host_pub in ud
    assert "b3BlbnNzaC1rZXktdjE" in ud
    assert cloudinit.EXEC_HELPER_PATH in ud
    assert cloudinit.ACTIVATION_SCRIPT_PATH in ud
    assert cloudinit.ACTIVATION_UNIT_PATH in ud
    assert cloudinit.READY_SENTINEL in ud
    assert cloudinit.CONSOLE_DEVICE in ud


def test_serial_builder_seed_is_credential_free_and_transport_handles_short_writes():
    user_data = cloudinit.build_serial_builder_user_data(
        activation_script="set -euo pipefail\necho activating\n",
        job={
            "network": "none",
            "parent_mounts": [],
            "runs": [{"line": 2, "command": "true", "env": {}, "workdir": "/"}],
        },
    )
    assert "ssh_authorized_keys:" not in user_data
    assert "ed25519_private:" not in user_data
    assert cloudinit.READY_SENTINEL in user_data
    assert '["mount", "--rbind", "/dev"' in user_data
    assert "while block := fp.read(32 * 1024)" in user_data

    namespace: dict[str, object] = {"__name__": "serial_builder_test"}
    exec(compile(cloudinit._BUILD_WORKER_SOURCE, "<serial-builder>", "exec"), namespace)

    class ShortWriter:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, value: memoryview) -> int:
            count = min(len(value), 7)
            self.data.extend(value[:count])
            return count

    writer = ShortWriter()
    namespace["_write_all"](writer, b"serial-output")  # type: ignore[operator]
    assert bytes(writer.data) == b"serial-output"


def test_exec_payload_encoding_roundtrip():
    argv = ["/opt/layers/merged/usr/bin/python3", "-c", "import sys; print(sys.argv)", "foo bar", ""]
    payload = guest.encode_exec_payload(argv)

    # Alphabet must strictly be base64url characters without padding or whitespace
    assert re.fullmatch(r"^[A-Za-z0-9_-]+\Z", payload) is not None

    padded = payload + "=" * (-len(payload) % 4)
    raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded == argv


def test_exec_payload_rejects_invalid():
    with pytest.raises(GuestError, match="nonempty argv"):
        guest.encode_exec_payload([])
    with pytest.raises(GuestError, match="NUL-free"):
        guest.encode_exec_payload(["python", "arg\x00bad"])


def test_known_hosts_entry():
    pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKeyExample host@guest"
    entry22 = guest.build_known_hosts_entry("10.0.0.5", pub)
    assert entry22 == f"10.0.0.5 {pub}\n"

    entry2222 = guest.build_known_hosts_entry("10.0.0.5", pub, port=2222)
    assert entry2222 == f"[10.0.0.5]:2222 {pub}\n"


def test_ssh_command_builders():
    identity = Path("/run/palimpsest/ssh/id_ed25519")
    known_hosts = Path("/run/palimpsest/ssh/known_hosts")

    shell_cmd = guest.build_shell_command("10.0.0.5", identity=identity, known_hosts=known_hosts)
    assert shell_cmd == [
        "ssh",
        "-tt",
        "-i",
        "/run/palimpsest/ssh/id_ed25519",
        "-o",
        "UserKnownHostsFile=/run/palimpsest/ssh/known_hosts",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-p",
        "22",
        "ubuntu@10.0.0.5",
    ]

    exec_cmd = guest.build_exec_command("10.0.0.5", ["echo", "hello"], identity=identity, known_hosts=known_hosts)
    assert exec_cmd[0] == "ssh"
    assert exec_cmd[-2] == cloudinit.EXEC_HELPER_PATH
    assert exec_cmd[-1] == guest.encode_exec_payload(["echo", "hello"])


def test_scp_download_command_security():
    identity = Path("/run/palimpsest/ssh/id_ed25519")
    known_hosts = Path("/run/palimpsest/ssh/known_hosts")
    local_target = Path("/tmp/local.squashfs")

    scp_cmd = guest.build_scp_download_command(
        "10.0.0.5", "/opt/layers/layer.squashfs", local_target, identity=identity, known_hosts=known_hosts
    )
    assert scp_cmd[0] == "scp"
    assert scp_cmd[1] == "-s"  # SFTP subsystem pinned
    assert scp_cmd[-2] == "ubuntu@10.0.0.5:/opt/layers/layer.squashfs"

    # Traversal and metacharacters rejected
    with pytest.raises(GuestError, match="invalid remote path"):
        guest.build_scp_download_command(
            "10.0.0.5", "/tmp/../etc/shadow", local_target, identity=identity, known_hosts=known_hosts
        )
    with pytest.raises(GuestError, match="invalid remote path"):
        guest.build_scp_download_command(
            "10.0.0.5", "/tmp/file;reboot", local_target, identity=identity, known_hosts=known_hosts
        )
