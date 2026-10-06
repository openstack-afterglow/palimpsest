"""Unit tests for palimpsest_local.cloudinit and palimpsest_local.guest."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

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
    assert cloudinit.READY_SCRIPT_PATH in ud
    assert cloudinit.READY_UNIT_PATH in ud
    assert cloudinit.READY_SENTINEL in ud
    assert ud.count("exec >>/dev/ttyS0 2>&1") == 2
    assert "echo PALIMPSEST_READY=1 >>/dev/ttyS0" in ud
    assert "/dev/console" not in ud


@pytest.fixture
def generated_first_boot(tmp_path: Path):
    """Install actual generated scripts under tmp_path; emulate only guest tools.

    The mount executable records real argv and can fail without touching a host
    mount. The systemctl executable runs the generated service's ExecStart and
    keeps its success/failure state, rather than assuming activation succeeded.
    """

    def prepare(*, read_only: bool, with_commands: bool, failed_mount: str | None):
        from palimpsest_local.refs import HostDirectoryShare

        (tmp_path / "shared").mkdir()
        share = HostDirectoryShare(tmp_path.resolve(), "shared", str(tmp_path / "guest-share"), read_only)
        console = tmp_path / "console"
        marker = tmp_path / "bootstrapped"
        user_command = tmp_path / "user-command-ran"
        mount_log = tmp_path / "mounts.jsonl"
        active = tmp_path / "activation-active"
        commands = (("touch", str(user_command)),) if with_commands else ()
        config = yaml.safe_load(
            cloudinit.build_user_data(
                client_public_key="ssh-ed25519 AAAAClient client@host",
                host_private_key="test-key",
                host_public_key="ssh-ed25519 AAAAHost host@guest",
                activation_script=shlex.join(
                    ("mount", "-t", "squashfs", "-o", "ro", "/dev/vdb", str(tmp_path / "layer"))
                ),
                host_shares=(share,),
                cloud_init=SimpleNamespace(runcmd=commands),
            )
        )
        paths = {item["path"]: tmp_path / Path(item["path"]).name for item in config["write_files"]}
        paths.update({cloudinit.CONSOLE_DEVICE: console, cloudinit.BOOTSTRAP_MARKER_PATH: marker})

        def relocate(text: str) -> str:
            for guest_path, local_path in sorted(paths.items(), key=lambda item: len(item[0]), reverse=True):
                text = text.replace(guest_path, str(local_path))
            return text

        for item in config["write_files"]:
            path = paths[item["path"]]
            path.write_text(relocate(item["content"]))
            path.chmod(int(item["permissions"], 8))

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        def executable(name: str, body: str) -> None:
            path = bin_dir / name
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o755)

        executable(
            "mount",
            f"""import json, sys
from pathlib import Path
with Path({str(mount_log)!r}).open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
kind = sys.argv[sys.argv.index('-t') + 1]
sys.exit(1 if kind == {failed_mount!r} else 0)
""",
        )
        executable(
            "systemctl",
            f"""import configparser, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
active = Path({str(active)!r})
if args == ['daemon-reload']:
    sys.exit(0)
if args == ['enable', '--now', {cloudinit.ACTIVATION_UNIT_NAME!r}]:
    active.unlink(missing_ok=True)
    unit = configparser.ConfigParser()
    unit.read({str(paths[cloudinit.ACTIVATION_UNIT_PATH])!r})
    result = subprocess.run([unit['Service']['ExecStart']], check=False)
    if result.returncode == 0:
        active.touch()
    sys.exit(result.returncode)
if args == ['is-active', '--quiet', {cloudinit.ACTIVATION_UNIT_NAME!r}]:
    sys.exit(0 if active.exists() else 3)
if args in ([ 'enable', {cloudinit.READY_UNIT_NAME!r}],
            [ 'enable', {cloudinit.READY_FALLBACK_UNIT_NAME!r}]):
    sys.exit(0)
sys.exit(2)
""",
        )
        # Support GNU install's -D on macOS too, retaining real file creation.
        executable(
            "install",
            f"""import os, sys
from pathlib import Path
args = sys.argv[1:]
if '-D' in args:
    Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
    args.remove('-D')
os.execv({shutil.which("install")!r}, ['install', *args])
""",
        )
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}

        def run(entrypoint: str) -> subprocess.CompletedProcess[str]:
            if entrypoint == "project-init":
                # Simulate an independently completed (possibly failed) unit.
                subprocess.run(
                    [str(bin_dir / "systemctl"), "enable", "--now", cloudinit.ACTIVATION_UNIT_NAME],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                argv = [str(paths[cloudinit.PROJECT_INIT_PATH])]
            else:
                # cloud-init runs its generated runcmd shell WITHOUT errexit.
                wrapper = tmp_path / "runcmd"
                wrapper.write_text("#!/bin/sh\n" + "\n".join(relocate(command) for command in config["runcmd"]) + "\n")
                argv = ["/bin/sh", str(wrapper)]
            return subprocess.run(argv, env=env, capture_output=True, text=True, check=False, timeout=10)

        return SimpleNamespace(
            run=run,
            console=console,
            marker=marker,
            user_command=user_command,
            mount_log=mount_log,
            active=active,
            share=share,
        )

    return prepare


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("with_commands", [False, True])
@pytest.mark.parametrize("entrypoint", ["runcmd", "project-init"])
@pytest.mark.parametrize("failed_mount", ["squashfs", "virtiofs"])
def test_generated_first_boot_mount_failure_blocks_commands_and_readiness(
    generated_first_boot,
    read_only: bool,
    with_commands: bool,
    entrypoint: str,
    failed_mount: str,
):
    boot = generated_first_boot(read_only=read_only, with_commands=with_commands, failed_mount=failed_mount)

    result = boot.run(entrypoint)

    assert result.returncode != 0
    mounts = [json.loads(line) for line in boot.mount_log.read_text().splitlines()]
    assert any(args[args.index("-t") + 1] == failed_mount for args in mounts)
    assert not boot.active.exists()
    assert not boot.user_command.exists()
    assert not boot.marker.exists()
    assert cloudinit.READY_SENTINEL not in boot.console.read_text()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("with_commands", [False, True])
@pytest.mark.parametrize("entrypoint", ["runcmd", "project-init"])
def test_generated_first_boot_success_runs_commands_and_writes_readiness(
    generated_first_boot,
    read_only: bool,
    with_commands: bool,
    entrypoint: str,
):
    boot = generated_first_boot(read_only=read_only, with_commands=with_commands, failed_mount=None)

    result = boot.run(entrypoint)

    assert result.returncode == 0, result.stderr
    mounts = [json.loads(line) for line in boot.mount_log.read_text().splitlines()]
    assert [args[args.index("-t") + 1] for args in mounts] == ["squashfs", "virtiofs"]
    assert mounts[-1] == [
        "-t",
        "virtiofs",
        "-o",
        "ro" if read_only else "rw",
        boot.share.guest_tag,
        boot.share.mount_path,
    ]
    assert boot.active.exists()
    assert boot.user_command.exists() == with_commands
    assert boot.marker.is_file()
    assert cloudinit.READY_SENTINEL in boot.console.read_text().splitlines()


def test_arm_user_data_writes_readiness_to_virt_serial():
    user_data = cloudinit.build_user_data(
        client_public_key="ssh-ed25519 AAAAClient client@host",
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
        host_public_key="ssh-ed25519 AAAAHost host@guest",
        activation_script="true\n",
        arch="aarch64",
    )

    assert user_data.count("exec >>/dev/ttyAMA0 2>&1") == 2
    assert "/dev/ttyS0" not in user_data
    assert "echo PALIMPSEST_READY=1 >>/dev/ttyAMA0" in user_data
    assert "/dev/console" not in user_data


def test_user_data_appends_guest_environment_without_shell_interpolation():
    user_data = cloudinit.build_user_data(
        client_public_key="ssh-ed25519 AAAAClient client@host",
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
        host_public_key="ssh-ed25519 AAAAHost host@guest",
        activation_script="set -euo pipefail\ntrue\n",
        environment=(("APP_ENV", "production"), ("LITERAL", '$HOME and "quotes"')),
    )

    assert "  - path: /etc/environment" in user_data
    assert "    append: true" in user_data
    assert 'APP_ENV="production"' in user_data
    assert 'LITERAL="' in user_data
    assert "$HOME" in user_data


def test_user_data_rejects_multiline_environment():
    with pytest.raises(GuestError, match="single-line"):
        cloudinit.build_user_data(
            client_public_key="ssh-ed25519 AAAAClient client@host",
            host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
            host_public_key="ssh-ed25519 AAAAHost host@guest",
            activation_script="true\n",
            environment=(("BAD", "line1\nline2"),),
        )


def test_typed_cloud_init_is_compiled_before_final_readiness():
    custom = SimpleNamespace(
        packages=("curl",),
        write_files=(SimpleNamespace(path="/etc/demo.conf", content="mode=prod", permissions="0640"),),
        runcmd=(("printf", "%s", "hello; not-a-shell"),),
    )
    user_data = cloudinit.build_user_data(
        client_public_key="ssh-ed25519 AAAAClient client@host",
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
        host_public_key="ssh-ed25519 AAAAHost host@guest",
        activation_script="true\n",
        cloud_init=custom,
    )

    assert '  - "curl"' in user_data
    assert 'path: "/etc/demo.conf"' in user_data
    assert "printf %s 'hello; not-a-shell'" in user_data
    project_script = user_data[user_data.index(cloudinit.PROJECT_INIT_PATH) :]
    assert project_script.index("hello; not-a-shell") < project_script.index(cloudinit.READY_SENTINEL)
    activation_script = user_data[
        user_data.index(cloudinit.ACTIVATION_SCRIPT_PATH) : user_data.index(cloudinit.ACTIVATION_UNIT_PATH)
    ]
    assert cloudinit.READY_SENTINEL not in activation_script
    assert f"After={cloudinit.ACTIVATION_UNIT_NAME} cloud-final.service" in user_data
    assert "WantedBy=cloud-init.target" in user_data
    assert f"systemctl enable {cloudinit.READY_UNIT_NAME}" in user_data
    assert f"systemctl enable --now {cloudinit.READY_UNIT_NAME}" not in user_data
    assert f"ConditionPathExists={cloudinit.BOOTSTRAP_MARKER_PATH}" in user_data
    assert "ConditionPathExists=/etc/cloud/cloud-init.disabled" in user_data
    assert f"After={cloudinit.ACTIVATION_UNIT_NAME}\n" in user_data
    assert f"systemctl enable {cloudinit.READY_FALLBACK_UNIT_NAME}" in user_data
    assert "WantedBy=multi-user.target" in user_data
    assert project_script.index("hello; not-a-shell") < project_script.index(cloudinit.BOOTSTRAP_MARKER_PATH)
    assert project_script.index(cloudinit.BOOTSTRAP_MARKER_PATH) < project_script.index(cloudinit.READY_SENTINEL)


def test_typed_cloud_init_cannot_overwrite_runtime_paths():
    custom = SimpleNamespace(
        packages=(),
        write_files=(SimpleNamespace(path=cloudinit.ACTIVATION_SCRIPT_PATH, content="bad", permissions="0755"),),
        runcmd=(),
    )
    with pytest.raises(GuestError, match="reserved"):
        cloudinit.build_user_data(
            client_public_key="ssh-ed25519 AAAAClient client@host",
            host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nkey\n-----END OPENSSH PRIVATE KEY-----\n",
            host_public_key="ssh-ed25519 AAAAHost host@guest",
            activation_script="true\n",
            cloud_init=custom,
        )


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
