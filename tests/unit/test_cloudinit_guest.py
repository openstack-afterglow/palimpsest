"""Unit tests for palimpsest_local.cloudinit and palimpsest_local.guest."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import stat
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


def test_serial_builder_layer_root_uses_base_root_not_private_capture_mode(tmp_path):
    namespace: dict[str, object] = {"__name__": "serial_builder_test"}
    exec(compile(cloudinit._BUILD_WORKER_SOURCE, "<serial-builder>", "exec"), namespace)
    base_root, upper = tmp_path / "base-root", tmp_path / "upper"
    base_root.mkdir()
    base_root.chmod(0o755)
    upper.mkdir(mode=0o700)
    namespace["_seed_upper_root"](str(upper), str(base_root))  # type: ignore[operator]
    assert stat.S_IMODE(upper.stat().st_mode) == 0o755
    assert (upper.stat().st_uid, upper.stat().st_gid) == (base_root.stat().st_uid, base_root.stat().st_gid)


@pytest.mark.parametrize("unshare_result", [0, -1])
def test_capture_worker_mount_namespace_is_private_and_fails_closed(monkeypatch, unshare_result):
    namespace = {"__name__": "serial_builder_test"}
    exec(compile(cloudinit._BUILD_WORKER_SOURCE, "<serial-builder>", "exec"), namespace)
    calls = []

    def unshare(flags):
        calls.append(flags)
        return unshare_result

    monkeypatch.setattr(namespace["ctypes"], "CDLL", lambda *args, **kwargs: SimpleNamespace(unshare=unshare))
    monkeypatch.setattr(namespace["ctypes"], "get_errno", lambda: 1)
    namespace["_run"] = calls.append
    if unshare_result:
        with pytest.raises(OSError, match="unshare mount namespace"):
            namespace["_private_mount_namespace"]()
        assert calls == [0x00020000]
    else:
        namespace["_private_mount_namespace"]()
        assert calls == [0x00020000, ["mount", "--make-rprivate", "/"]]


@pytest.mark.parametrize("temporary_capture", [None, False, True])
def test_capture_worker_keeps_tmpfs_default_but_allows_attached_storage(tmp_path, monkeypatch, temporary_capture):
    capture, output = tmp_path / "capture", tmp_path / "output.squashfs"
    monkeypatch.setenv("PALIMPSEST_BUILD_CAPTURE", str(capture))
    monkeypatch.setenv("PALIMPSEST_BUILD_OUTPUT", str(output))
    namespace = {"__name__": "serial_builder_test"}
    exec(compile(cloudinit._BUILD_WORKER_SOURCE, "<serial-builder>", "exec"), namespace)
    job = {"network": "none", "parent_mounts": [], "runs": []}
    if temporary_capture is not None:
        job["capture_tmpfs"] = temporary_capture
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job))
    namespace["JOB_PATH"] = str(job_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "mksquashfs":
            output.write_bytes(b"hsqs-capture-bytes")
        return SimpleNamespace(returncode=0)

    namespace["_private_mount_namespace"] = lambda: calls.append("private-mount-namespace")
    namespace["_run"] = run
    namespace["_seed_upper_root"] = lambda *args: None
    namespace["_mount_chroot_support"] = lambda *args: None
    namespace["_unmount_chroot_support"] = lambda **kwargs: None
    namespace["_local_capture_check"] = lambda: None
    namespace["_clean"] = lambda *args: None
    monkeypatch.setattr(namespace["subprocess"], "run", run)
    size, digest = namespace["_build"]()
    assert calls[0] == "private-mount-namespace"
    assert size == len(b"hsqs-capture-bytes")
    assert len(digest) == 64
    tmpfs_mount = ["mount", "-t", "tmpfs", "-o", "mode=0700", "tmpfs", str(capture)]
    assert (tmpfs_mount in calls) is (temporary_capture is not False)
    assert (["umount", str(capture)] in calls) is (temporary_capture is not False)
    assert ["umount", str(capture / "merged")] in calls


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


@pytest.fixture
def external_manifest():
    return {
        "schema": "palimpsest.external-layer-runtime.v2",
        "arch": "x86_64",
        "filesystem_uuid": "12345678-1234-5678-9abc-123456789abc",
        "stacks": [{"name": "native", "layers": ["sha256:" + "a" * 64, "sha256:" + "b" * 64]}],
        "exports": [{"stack": "native", "path": "opt/native", "target": "/opt/native"}],
        "paths": ["/opt/native/bin"],
        "library_paths": ["/opt/native/lib"],
        "activation_commands": [],
        "checks": [],
    }


@pytest.fixture
def external_guest(external_manifest):
    from palimpsest_local.external_layers import HELPER_PATH, build_external_layer_user_data

    config = yaml.safe_load(build_external_layer_user_data(external_manifest))
    source = next(item["content"] for item in config["write_files"] if item["path"] == HELPER_PATH)
    namespace = {"__name__": "external_guest_under_test"}
    exec(compile(source, HELPER_PATH, "exec"), namespace)
    return namespace


@pytest.mark.parametrize("manifest", [None, [], "invalid", {}, {"schema": "other"}])
def test_external_exporter_rejects_invalid_object(manifest):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "palimpsest.external-layer-runtime.v1"),
        ("arch", "amd64"),
        ("arch", []),
        ("filesystem_uuid", "12345678123456789abc123456789abc"),
        ("filesystem_uuid", "12345678-1234-5678-9ABC-123456789ABC"),
        ("filesystem_uuid", None),
        ("storage_read_only", "true"),
        ("storage_read_only", 1),
        ("storage_read_only", None),
        ("stacks", []),
        ("stacks", {}),
        ("exports", {}),
        ("paths", ["/home/ubuntu/runtime/bin"]),
        ("library_paths", ["/usr/lib"]),
        ("paths", ["/opt/native-other/bin"]),
        ("paths", ["/opt/native/bin", "/opt/native/bin"]),
        ("checks", [["/bin/echo", "injected\ncommand"]]),
        ("activation_commands", [["/bin/echo", "nul\x00arg"]]),
        ("checks", ["echo not argv"]),
        ("checks", [[]]),
        ("checks", [[""]]),
        ("unexpected", True),
    ],
)
def test_external_exporter_rejects_invalid_policy(external_manifest, field, value):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest[field] = value
    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(external_manifest)


@pytest.mark.parametrize(
    "stack",
    [
        {"name": "Upper", "layers": ["sha256:" + "a" * 64]},
        {"name": "../escape", "layers": ["sha256:" + "a" * 64]},
        {"name": "native", "layers": []},
        {"name": "native", "layers": ["sha256:" + "A" * 64]},
        {"name": "native", "layers": ["sha256:" + "a" * 63]},
        {"name": "native", "layers": ["sha256:" + "a" * 64] * 2},
        {"name": "native", "layers": ["sha256:" + "a" * 64], "command": ["false"]},
    ],
)
def test_external_exporter_rejects_invalid_stack(external_manifest, stack):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest["stacks"] = [stack]
    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(external_manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../native"),
        ("path", "/opt/native"),
        ("path", "opt//native"),
        ("path", "opt/./native"),
        ("path", "opt/native:other"),
        ("target", "/opt"),
        ("target", "/usr/local"),
        ("target", "/home/ubuntu/native"),
        ("target", "/usr/local/../lib"),
        ("stack", "missing"),
        ("extra", "invalid"),
    ],
)
def test_external_exporter_rejects_unsafe_export(external_manifest, field, value):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest["exports"][0][field] = value
    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(external_manifest)


@pytest.mark.parametrize("target", ["/opt/native", "/opt/native/child", "/opt"])
def test_external_exporter_rejects_colliding_exports(external_manifest, target):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest["exports"].append({"stack": "native", "path": "other", "target": target})
    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(external_manifest)


def test_external_exporter_rejects_duplicate_stack_names(external_manifest):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest["stacks"].append(dict(external_manifest["stacks"][0]))
    with pytest.raises(ArtifactValidationError, match="duplicate stack"):
        build_external_layer_user_data(external_manifest)


def test_external_guest_checks_architecture_before_storage_or_mounts(external_guest):
    external_guest["platform"] = SimpleNamespace(machine=lambda: "aarch64")
    external_guest["verify_storage"] = lambda policy: pytest.fail("storage inspected before architecture")
    external_guest["run"] = lambda argv, **kwargs: pytest.fail("mount attempted before architecture")
    with pytest.raises(RuntimeError, match="architecture mismatch"):
        external_guest["activate_mounts"](external_guest["POLICY"])


def test_external_guest_hashes_actual_blob_and_rejects_symlinks(external_guest, tmp_path):
    import hashlib

    blob = tmp_path / "blob"
    blob.write_bytes(b"actual packed SquashFS candidate")
    blob.chmod(0o644)
    digest = hashlib.sha256(blob.read_bytes()).hexdigest()
    info = blob.stat()
    storage = {"device": f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"}

    class RootOwnedOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def fstat(self, descriptor):
            actual = os.fstat(descriptor)
            fields = ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            return SimpleNamespace(st_uid=0, **{field: getattr(actual, field) for field in fields})

    # Portable tests emulate root ownership only, never hashing or file opening.
    external_guest["os"] = RootOwnedOS()
    external_guest["directory"] = lambda path, **kwargs: path
    external_guest["verify_blob"](blob, digest, storage)
    with pytest.raises(RuntimeError, match="digest mismatch"):
        external_guest["verify_blob"](blob, "0" * 64, storage)
    with pytest.raises(RuntimeError, match="not on the external filesystem"):
        external_guest["verify_blob"](blob, digest, {"device": "999:999"})
    alias = tmp_path / "alias"
    alias.symlink_to(blob)
    with pytest.raises(OSError):
        external_guest["verify_blob"](alias, digest, storage)
    blob.chmod(0o666)
    with pytest.raises(RuntimeError, match="immutable-to-users"):
        external_guest["verify_blob"](blob, digest, storage)


@pytest.mark.parametrize(
    "target,refused",
    [
        ("/proc/sys/fs/binfmt_misc", False),
        ("/srv/palimpsest", True),
        ("/srv/palimpsest/layers/blob", True),
        ("/opt/native", True),
        ("/opt/native/lib", True),
    ],
)
def test_external_guest_scopes_stacked_mount_refusal_to_runtime(external_guest, tmp_path, target, refused):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"40 1 0:40 / {target} rw,nosuid,nodev - autofs systemd-1 rw\n"
        f"41 40 0:41 / {target} rw,nosuid,nodev - binfmt_misc binfmt_misc rw\n"
    )
    external_guest["Path"] = lambda value: mountinfo if value == "/proc/self/mountinfo" else Path(value)
    if refused:
        with pytest.raises(RuntimeError, match="stacked mounts"):
            external_guest["mounts"]()
    else:
        assert external_guest["mounts"]()[target]["type"] == "binfmt_misc"


@pytest.mark.parametrize(
    "reported_uuid,device,root",
    [
        ("wrong-uuid", "8:16", "/"),
        ("12345678-1234-5678-9abc-123456789abc", "8:17", "/"),
        ("12345678-1234-5678-9abc-123456789abc", "8:16", "/subdirectory"),
    ],
)
def test_external_guest_refuses_wrong_storage_identity(external_guest, reported_uuid, device, root):
    import stat

    class Device:
        def __truediv__(self, other):
            return self

        def resolve(self, **kwargs):
            return self

        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 16))

        def __str__(self):
            return "/dev/vdb"

    external_guest["Path"] = lambda path: Device()
    external_guest["directory"] = lambda path, **kwargs: path
    external_guest["run"] = lambda argv, **kwargs: reported_uuid if "UUID" in argv else "ext4"
    external_guest["require_mount"] = lambda *args, **kwargs: {"device": device, "root": root}
    with pytest.raises(RuntimeError, match="UUID mismatch|expected UUID filesystem"):
        external_guest["verify_storage"](external_guest["POLICY"])


@pytest.mark.parametrize(
    "readonly,options,journal,block_readonly,error",
    [
        (True, {"ro", "nosuid", "nodev"}, "norecovery", "1", None),
        (True, {"ro", "nosuid", "nodev"}, "noload", "1", None),
        (True, {"rw", "nosuid", "nodev"}, "norecovery", "1", "mount options"),
        (True, {"ro", "nosuid", "nodev"}, "", "1", "journal replay"),
        (True, {"ro", "nosuid", "nodev"}, "norecovery", "0", "read-only block device"),
        (False, {"rw", "nosuid", "nodev"}, "", "0", None),
    ],
)
def test_external_guest_shared_storage_requires_no_replay_and_readonly_device(
    external_guest, readonly, options, journal, block_readonly, error
):
    import stat

    class DevicePath:
        def __truediv__(self, other):
            return self

        def resolve(self, **kwargs):
            return self

        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 16))

        def read_text(self):
            return block_readonly

        def __str__(self):
            return "/dev/vdb"

    entry = {"device": "8:16", "root": "/", "type": "ext4", "options": options, "super": [journal]}
    external_guest["Path"] = lambda path: DevicePath()
    external_guest["directory"] = lambda path, **kwargs: path
    external_guest["mounts"] = lambda: {str(external_guest["ROOT"]): entry}
    external_guest["run"] = lambda argv, **kwargs: (
        external_guest["POLICY"]["filesystem_uuid"] if "UUID" in argv else "ext4"
    )
    external_guest["POLICY"]["storage_read_only"] = readonly
    if error:
        with pytest.raises(RuntimeError, match=error):
            external_guest["verify_storage"](external_guest["POLICY"])
    else:
        assert external_guest["verify_storage"](external_guest["POLICY"]) == entry


def test_external_guest_uses_precreated_empty_mountpoints_without_writing(external_guest):
    import stat

    class PrecreatedPath(type(Path())):
        def exists(self):
            return True

        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

        def mkdir(self, *args, **kwargs):
            pytest.fail("mkdir attempted on the read-only shared filesystem")

        def iterdir(self):
            return iter(())

    external_guest["Path"] = PrecreatedPath
    external_guest["empty_target"]("/srv/palimpsest/layers/" + "a" * 64)
    external_guest["empty_target"]("/srv/palimpsest/runtimes/native")


@pytest.fixture
def external_mount_kernel(external_guest):
    """Emulate mount syscalls; execute the generated planner and identity checks."""
    namespace = external_guest
    state = {}
    commands = []
    root = namespace["ROOT"]
    source = root / "runtimes/native/opt/native"
    inode = {str(source): (91, 42), "/opt/native": (91, 42)}

    class GuestPath(type(Path())):
        def stat(self, **kwargs):
            if str(self) in inode:
                dev, ino = inode[str(self)]
                return SimpleNamespace(st_dev=dev, st_ino=ino)
            return super().stat(**kwargs)

    namespace["Path"] = GuestPath
    namespace["platform"] = SimpleNamespace(machine=lambda: "x86_64")
    namespace["verify_storage"] = lambda policy: {"device": "8:16"}
    namespace["verify_blob"] = lambda *args: None
    namespace["directory"] = lambda path, **kwargs: GuestPath(path)
    namespace["empty_target"] = lambda path: None
    namespace["consumer_state"] = lambda policy, storage: (Path("/var/lib/palimpsest/consumer"), {"machine": "test"})
    namespace["stack_state"] = lambda root, identity, stack: (
        root / stack["name"] / "upper",
        root / stack["name"] / "work",
    )
    namespace["mounts"] = lambda: state
    namespace["verify_layer"] = lambda path, blob: namespace["require_mount"](path, "squashfs")

    def run(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "--bind":
            if argv[2] in state:
                state[argv[-1]] = dict(state[argv[2]])
            else:
                state[argv[-1]] = dict(state[str(root / "runtimes/native")], root="/opt/native")
        elif argv[1] == "-o":
            state[argv[-1]]["options"] = {"rw", "nosuid", "nodev"}
        else:
            kind = argv[argv.index("-t") + 1]
            options = argv[argv.index("-o") + 1].split(",")
            state[argv[-1]] = {
                "type": kind,
                "options": set(options),
                "root": "/",
                "device": "0:91",
                "source": "overlay" if kind == "overlay" else "/dev/loop0",
                "super": options,
            }

    namespace["run"] = run
    return namespace, state, commands, inode


def test_external_guest_mounts_base_to_top_writable_and_restarts_idempotently(external_mount_kernel):
    namespace, state, commands, inode = external_mount_kernel
    namespace["activate_mounts"](namespace["POLICY"])
    overlay = next(command for command in commands if "overlay" in command)
    lower_option = overlay[overlay.index("-o") + 1]
    assert (
        lower_option
        == "rw,nosuid,nodev,lowerdir="
        + str(namespace["ROOT"] / "layers" / ("b" * 64))
        + ":"
        + str(namespace["ROOT"] / "layers" / ("a" * 64))
        + ",upperdir=/var/lib/palimpsest/consumer/native/upper,workdir=/var/lib/palimpsest/consumer/native/work"
    )
    assert {"rw", "nosuid", "nodev"} <= state["/opt/native"]["options"]
    commands.clear()
    namespace["activate_mounts"](namespace["POLICY"])
    assert commands == []


@pytest.mark.parametrize(
    "corruption",
    [
        "overlay-lowers",
        "overlay-readonly",
        "overlay-upper",
        "overlay-work",
        "export-device",
        "export-inode",
        "nested-mount",
        "state-mount",
    ],
)
def test_external_guest_refuses_existing_unknown_mounts(external_mount_kernel, corruption):
    namespace, state, commands, inode = external_mount_kernel
    namespace["activate_mounts"](namespace["POLICY"])
    overlay = state[str(namespace["ROOT"] / "runtimes/native")]
    if corruption == "overlay-lowers":
        overlay["super"] = ["lowerdir=/some/other/layer"]
    elif corruption == "overlay-readonly":
        overlay["options"] = {"ro", "nosuid", "nodev"}
    elif corruption in {"overlay-upper", "overlay-work"}:
        overlay["super"] = [
            item
            for item in overlay["super"]
            if not item.startswith("upperdir=" if corruption == "overlay-upper" else "workdir=")
        ]
    elif corruption == "state-mount":
        state["/var/lib/palimpsest/consumer/native/upper"] = {"type": "tmpfs"}
    elif corruption == "export-device":
        state["/opt/native"]["device"] = "9:9"
    elif corruption == "export-inode":
        inode["/opt/native"] = (91, 999)
    else:
        state["/opt/native/bin"] = {"type": "tmpfs"}
    commands.clear()
    with pytest.raises(RuntimeError, match="different stack|mount options|different source|unexpected nested"):
        namespace["activate_mounts"](namespace["POLICY"])
    assert commands == []


def test_external_guest_refuses_loop_backed_by_different_blob(external_guest, tmp_path):
    blob, other = tmp_path / "expected", tmp_path / "other"
    blob.touch()
    other.touch()
    sysfs = tmp_path / "sysfs"
    loop = sysfs / "7:4/loop"
    loop.mkdir(parents=True)
    (loop.parent / "ro").write_text("1\n")
    (loop / "backing_file").write_text(str(other) + "\n")
    (loop / "offset").write_text("0\n")
    (loop / "sizelimit").write_text("0\n")
    external_guest["Path"] = lambda path: sysfs if str(path) == "/sys/dev/block" else Path(path)
    external_guest["require_mount"] = lambda *args: {"root": "/", "device": "7:4"}
    with pytest.raises(RuntimeError, match="different blob"):
        external_guest["verify_layer"](tmp_path / "layer", blob)
    (loop / "backing_file").write_text(str(blob) + "\n")
    external_guest["verify_layer"](tmp_path / "layer", blob)
    (loop.parent / "ro").write_text("0\n")
    with pytest.raises(RuntimeError, match="writable"):
        external_guest["verify_layer"](tmp_path / "layer", blob)


def test_external_guest_activation_failure_prevents_runtime_checks(external_guest, tmp_path):
    checked = tmp_path / "check-ran"
    events = []
    external_guest["os"] = SimpleNamespace(geteuid=lambda: 0)
    external_guest["activate_mounts"] = lambda policy: events.append("mounts")
    external_guest["configure"] = lambda policy: events.append("configure") or {"PATH": "/usr/bin:/bin"}
    policy = external_guest["POLICY"]
    policy["activation_commands"] = [[sys.executable, "-c", "raise SystemExit(23)"]]
    policy["checks"] = [[sys.executable, "-c", f"from pathlib import Path; Path({str(checked)!r}).touch()"]]
    with pytest.raises(subprocess.CalledProcessError) as error:
        external_guest["main"](policy)
    assert error.value.returncode == 23
    assert events == ["mounts", "configure"]
    assert not checked.exists()


@pytest.mark.parametrize("activation_fails", [False, True])
def test_external_runcmd_starts_ssh_only_after_successful_activation(external_manifest, tmp_path, activation_fails):
    from palimpsest_local.external_layers import UNIT_NAME, build_external_layer_user_data

    log = tmp_path / "systemctl.jsonl"
    executable = tmp_path / "systemctl"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"with Path({str(log)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"sys.exit(9 if {activation_fails!r} and sys.argv[1:] == ['start', {UNIT_NAME!r}] else 0)\n"
    )
    executable.chmod(0o755)
    config = yaml.safe_load(build_external_layer_user_data(external_manifest))
    result = subprocess.run(config["runcmd"][0], env={"PATH": str(tmp_path)}, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert (result.returncode != 0) == activation_fails
    assert (["start", "ssh.service"] in calls) == (not activation_fails)


@pytest.mark.parametrize("mode,uid", [(0o120777, 0), (0o040775, 0), (0o040755, 1000)])
def test_external_guest_refuses_symlink_or_user_writable_prefix(external_guest, mode, uid):
    class GuestPath(type(Path())):
        def lstat(self):
            if str(self) == "/srv/palimpsest":
                return SimpleNamespace(st_mode=mode, st_uid=uid)
            return SimpleNamespace(st_mode=0o040755, st_uid=0)

    external_guest["Path"] = GuestPath
    with pytest.raises(RuntimeError, match="real, root-owned"):
        external_guest["directory"]("/srv/palimpsest/layers")


def test_external_guest_never_covers_nonempty_unmounted_export(external_guest, tmp_path):
    (tmp_path / "unrelated").write_text("keep me")
    external_guest["directory"] = lambda path, **kwargs: tmp_path
    with pytest.raises(RuntimeError, match="cover nonempty"):
        external_guest["empty_target"](tmp_path)
    assert (tmp_path / "unrelated").read_text() == "keep me"


def test_external_guest_configures_native_linker_and_interactive_path_idempotently(external_guest, tmp_path):
    class RootPath(type(Path())):
        def lstat(self):
            info = super().lstat()
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)

    profile = RootPath(tmp_path / "profile.sh")
    libraries = RootPath(tmp_path / "libraries.conf")
    bashrc = RootPath(tmp_path / "bash.bashrc")
    bashrc.write_text("# existing interactive shell policy\n")
    external_guest.update(PROFILE=profile, LIBRARIES=libraries, BASHRC=bashrc)
    external_guest["directory"] = lambda path, **kwargs: path
    calls = []
    external_guest["run"] = lambda argv, **kwargs: calls.append(argv)
    environment = external_guest["configure"](external_guest["POLICY"])
    before = [path.read_text() for path in (profile, libraries, bashrc)]
    assert external_guest["configure"](external_guest["POLICY"]) == environment
    assert [path.read_text() for path in (profile, libraries, bashrc)] == before
    assert libraries.read_text().splitlines()[-1] == "/opt/native/lib"
    result = subprocess.run(
        ["/bin/sh", "-c", f". {shlex.quote(str(profile))}; . {shlex.quote(str(profile))}; printf '%s' \"$PATH\""],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert result.stdout == "/opt/native/bin:/usr/bin:/bin"
    assert environment["PATH"] == "/opt/native/bin:" + external_guest["BASE_PATH"]
    # Exercise interactive Bash's rc-file startup, not an entry wrapper.
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--rcfile", str(bashrc), "-ic", "printf '%s' \"$PATH\""],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout == "/opt/native/bin:/usr/bin:/bin"
    libraries.write_text("unrelated existing linker policy\n")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        external_guest["configure"](external_guest["POLICY"])
    assert libraries.read_text() == "unrelated existing linker policy\n"


def test_external_guest_commands_and_checks_inherit_manifest_path(external_guest, tmp_path):
    executable = tmp_path / "native-tool"
    result = tmp_path / "native-tool-output"
    executable.write_text("#!/bin/sh\nprintf native-library-ready\n")
    executable.chmod(0o755)
    external_guest["os"] = SimpleNamespace(geteuid=lambda: 0)
    external_guest["activate_mounts"] = lambda policy: None
    external_guest["configure"] = lambda policy: {"PATH": str(tmp_path) + ":/usr/bin:/bin"}
    policy = external_guest["POLICY"]
    policy["activation_commands"] = [["native-tool"]]
    policy["checks"] = [["/bin/sh", "-ec", f"native-tool > {shlex.quote(str(result))}"]]
    external_guest["main"](policy)
    assert result.read_text() == "native-library-ready"


def test_external_guest_single_layer_uses_private_writable_overlay_and_restarts(external_mount_kernel):
    namespace, state, commands, _inode = external_mount_kernel
    namespace["POLICY"]["stacks"][0]["layers"] = ["sha256:" + "a" * 64]
    namespace["activate_mounts"](namespace["POLICY"])
    runtime = str(namespace["ROOT"] / "runtimes/native")
    layer = str(namespace["ROOT"] / "layers" / ("a" * 64))
    assert ["mount", "--bind", layer, runtime] not in commands
    assert any("overlay" in command for command in commands)
    assert state[runtime]["type"] == state["/opt/native"]["type"] == "overlay"
    assert {"rw", "nosuid", "nodev"} <= state[runtime]["options"]
    assert {"rw", "nosuid", "nodev"} <= state["/opt/native"]["options"]
    commands.clear()
    namespace["activate_mounts"](namespace["POLICY"])
    assert commands == []


@pytest.mark.parametrize("corruption", ["runtime-device", "runtime-root", "export-type", "export-root"])
def test_external_guest_single_layer_rejects_unrelated_existing_mounts(external_mount_kernel, corruption):
    namespace, state, commands, _inode = external_mount_kernel
    namespace["POLICY"]["stacks"][0]["layers"] = ["sha256:" + "a" * 64]
    namespace["activate_mounts"](namespace["POLICY"])
    runtime = state[str(namespace["ROOT"] / "runtimes/native")]
    if corruption == "runtime-device":
        runtime["device"] = "9:9"
    elif corruption == "runtime-root":
        runtime["root"] = "/unexpected-subtree"
    elif corruption == "export-type":
        state["/opt/native"]["type"] = "squashfs"
    else:
        state["/opt/native"]["root"] = "/unrelated-export"
    commands.clear()
    with pytest.raises(RuntimeError, match="different source|unexpected filesystem|different stack"):
        namespace["activate_mounts"](namespace["POLICY"])
    assert commands == []


def test_external_bootcmd_keeps_service_based_ssh_available_on_later_boots(external_manifest, tmp_path, monkeypatch):
    import pathlib

    from palimpsest_local.external_layers import build_external_layer_user_data

    config = yaml.safe_load(build_external_layer_user_data(external_manifest))
    state = tmp_path / "ssh-state"
    state.write_text("active")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        f"state = Path({str(state)!r})\n"
        "if sys.argv[1:] == ['stop', 'ssh.service']: state.write_text('stopped')\n"
        "if sys.argv[1:] == ['start', 'ssh.service']: state.write_text('active')\n"
    )
    systemctl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")

    def sandbox_path(value):
        value = str(value)
        return tmp_path / value.lstrip("/") if value.startswith(("/etc/systemd/", "/run/systemd/")) else Path(value)

    def bootcmd():
        for command in config["bootcmd"]:
            if command[:2] == ["/usr/bin/python3", "-c"]:
                with monkeypatch.context() as context:
                    context.setattr(pathlib, "Path", sandbox_path)
                    exec(compile(command[2], "generated-bootcmd", "exec"), {})
            else:
                subprocess.run(command, check=True)

    bootcmd()
    assert state.read_text() == "stopped"
    persistent = sandbox_path("/etc/systemd/system/ssh.service.d/palimpsest-runtime.conf")
    persistent.parent.mkdir(parents=True)
    persistent.write_text(
        next(
            item["content"]
            for item in config["write_files"]
            if item["path"] == "/etc/systemd/system/ssh.service.d/palimpsest-runtime.conf"
        )
    )
    subprocess.run(config["runcmd"][0], check=True)
    assert state.read_text() == "active"
    # scripts_user/runcmd is PER_INSTANCE: subsequent boots run bootcmd only.
    bootcmd()
    assert state.read_text() == "active"


def test_external_native_command_links_work_without_login_profile_and_refuse_replacement(external_guest, tmp_path):
    class RootPath(type(Path())):
        def lstat(self):
            info = super().lstat()
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)

        def stat(self, **kwargs):
            info = super().stat(**kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)

    runtime = tmp_path / "native"
    runtime.mkdir()
    executable = runtime / "nvidia-smi"
    executable.write_text("#!/bin/sh\nprintf 'native-gpu-ready'\n")
    executable.chmod(0o755)
    command_dir = RootPath(tmp_path / "commands")
    command_dir.mkdir()
    profile = RootPath(tmp_path / "profile.sh")
    libraries = RootPath(tmp_path / "linker.conf")
    bashrc = RootPath(tmp_path / "bashrc")
    bashrc.write_text("# base policy\n")
    external_guest.update(
        Path=RootPath, PROFILE=profile, LIBRARIES=libraries, BASHRC=bashrc, COMMAND_DIRECTORY=command_dir
    )
    external_guest["directory"] = lambda path, **kwargs: path
    external_guest["run"] = lambda argv, **kwargs: None
    policy = external_guest["POLICY"]
    policy["exports"][0]["target"] = str(runtime)
    policy["command_links"] = {"nvidia-smi": str(executable)}
    external_guest["configure"](policy)
    result = subprocess.run(
        ["/bin/sh", "-c", "nvidia-smi"],
        env={"PATH": str(command_dir) + ":/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "native-gpu-ready"
    external_guest["configure"](policy)
    link = command_dir / "nvidia-smi"
    link.unlink()
    link.symlink_to("/unrelated/tool")
    with pytest.raises(RuntimeError, match="unrelated"):
        external_guest["configure"](policy)
    assert str(link.readlink()) == "/unrelated/tool"


@pytest.mark.parametrize("links", [{"../escape": "/opt/native/bin/tool"}, {"tool": "/bin/sh"}, []])
def test_external_command_links_cannot_escape_declared_native_runtime(external_manifest, links):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest["command_links"] = links
    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(external_manifest)


@pytest.mark.parametrize(
    "paths",
    [
        {"sha256:" + "f" * 64: "cache/" + "f" * 64},
        {"sha256:" + "a" * 64: "../" + "a" * 64},
        {"sha256:" + "a" * 64: "cache/wrong"},
    ],
)
def test_external_blob_paths_require_declared_digest_and_safe_relative_identity(external_manifest, paths):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    external_manifest["blob_paths"] = paths
    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(external_manifest)


def test_external_mounts_canonical_cache_paths_without_copying_or_relinking(external_mount_kernel):
    namespace, state, commands, inode = external_mount_kernel
    digest = "sha256:" + "a" * 64
    namespace["POLICY"]["blob_paths"] = {digest: "cache/state/store/blobs/sha256/" + "a" * 64}
    verified = []
    namespace["verify_blob"] = lambda path, digest, storage: verified.append(str(path))
    namespace["activate_mounts"](namespace["POLICY"])
    assert verified[0] == "/srv/palimpsest/cache/state/store/blobs/sha256/" + "a" * 64
    assert any("cache/state/store/blobs/sha256/" + "a" * 64 in command for argv in commands for command in argv)


def _reject_external_manifest(manifest):
    from palimpsest_local.errors import ArtifactValidationError
    from palimpsest_local.external_layers import build_external_layer_user_data

    with pytest.raises(ArtifactValidationError):
        build_external_layer_user_data(manifest)


def test_external_single_layer_uses_private_writable_overlay(external_mount_kernel):
    namespace, state, commands, _ = external_mount_kernel
    namespace["POLICY"]["stacks"][0]["layers"] = ["sha256:" + "a" * 64]
    namespace["activate_mounts"](namespace["POLICY"])
    runtime = state[str(namespace["ROOT"] / "runtimes/native")]
    assert runtime["type"] == "overlay"
    assert {"rw", "nosuid", "nodev"} <= runtime["options"]
    assert any(item.startswith("upperdir=") for item in runtime["super"])
    assert not any(command[1] == "--bind" and command[-1].endswith("/runtimes/native") for command in commands)


@pytest.mark.parametrize(
    "state_root", ["/srv/palimpsest/upper", "/tmp/upper", "/mnt/../upper", "/mnt/link,upper", "/mnt/" + "a" * 256]
)
def test_external_rejects_unsafe_consumer_state_root(external_manifest, state_root):
    external_manifest["consumer_state_root"] = state_root
    _reject_external_manifest(external_manifest)


def test_external_rejects_v1_readonly_policy(external_manifest):
    external_manifest["schema"] = "palimpsest.external-layer-runtime.v1"
    _reject_external_manifest(external_manifest)


@pytest.mark.parametrize("change", ["layers", "machine", "vm", "state_uuid", "upper", "work", "unrecorded", "symlink"])
def test_external_consumer_state_is_private_and_bound_to_exact_identity(external_guest, tmp_path, change):
    import stat

    class RootPath(type(Path())):
        def lstat(self):
            info = super().lstat()
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size, st_nlink=info.st_nlink)

    def directory(path, *, create=False):
        path = RootPath(path)
        if create:
            path.mkdir(mode=0o755, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise RuntimeError("unsafe physical directory")
        return path

    class RootOwnedOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def fstat(self, descriptor):
            info = os.fstat(descriptor)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_size=info.st_size, st_nlink=info.st_nlink)

    external_guest["os"] = RootOwnedOS()
    external_guest["directory"] = directory
    root = RootPath(tmp_path)
    identity = {
        "machine": "a" * 32,
        "vm": "12345678-1234-5678-9abc-123456789abc",
        "state_uuid": "87654321-1234-5678-9abc-123456789abc",
    }
    stack = {"name": "native", "layers": ["sha256:" + "a" * 64]}
    upper, work = external_guest["stack_state"](root, identity, stack)
    assert (upper.parent.stat().st_mode & 0o077) == 0
    (upper / "private-write").write_text("retained")
    assert external_guest["stack_state"](root, identity, stack) == (upper, work)
    assert (upper / "private-write").read_text() == "retained"
    if change == "layers":
        stack["layers"] = ["sha256:" + "b" * 64]
    elif change in identity:
        identity[change] = "different"
    elif change in {"upper", "work"}:
        path = upper if change == "upper" else work
        path.rename(path.with_name(change + "-old"))
        path.mkdir()
    elif change == "unrecorded":
        (upper.parent / "identity.json").unlink()
    else:
        owner = upper.parent / "identity.json"
        owner.rename(owner.with_name("saved.json"))
        owner.symlink_to("saved.json")
    with pytest.raises((RuntimeError, OSError)):
        external_guest["stack_state"](root, identity, stack)


@pytest.mark.parametrize(
    "kind,device,options", [("ext4", "8:16", {"rw"}), ("nfs", "0:1", {"rw"}), ("ext4", "8:1", {"ro"})]
)
def test_external_consumer_rejects_store_alias_or_bad_storage_before_mkdir(
    external_guest, tmp_path, kind, device, options
):
    root = tmp_path / "not-created"
    policy = {**external_guest["POLICY"], "consumer_state_root": str(root)}
    external_guest["ROOT"] = tmp_path
    external_guest["directory"] = lambda path, **kwargs: (
        pytest.fail("created state before qualification") if kwargs.get("create") else path
    )
    external_guest["mounts"] = lambda: {"/": {"type": kind, "device": device, "options": options, "root": "/"}}
    with pytest.raises(RuntimeError, match="lower store|writable ext4"):
        external_guest["consumer_state"](policy, {"device": "8:16"})
    assert not root.exists()


@pytest.fixture
def external_state_filesystem(external_guest):
    created = []
    identity_available = {"vm": True}
    state_uuid = "87654321-1234-5678-9abc-123456789abc"
    table = {"/": {"type": "ext4", "device": "8:1", "options": {"rw"}, "root": "/"}}

    class GuestPath(type(Path())):
        def exists(self):
            return not str(self).endswith("/new")

        def is_symlink(self):
            return False

        def stat(self, **kwargs):
            return SimpleNamespace(st_dev=2 if str(self).startswith("/srv/") else 1)

        def is_file(self):
            return str(self) == "/etc/machine-id" or (
                str(self) == "/sys/class/dmi/id/product_uuid" and identity_available["vm"]
            )

        def read_text(self):
            return "a" * 32 if str(self) == "/etc/machine-id" else "12345678-1234-5678-9abc-123456789abc"

    def directory(path, *, create=False):
        if create:
            created.append(str(path))
        return GuestPath(path)

    external_guest.update(
        Path=GuestPath,
        ROOT=GuestPath("/srv/palimpsest"),
        directory=directory,
        mounts=lambda: table,
        run=lambda *args, **kwargs: state_uuid,
    )
    return external_guest, table, created, identity_available, state_uuid


@pytest.mark.parametrize("alternate", [False, True])
def test_external_consumer_qualifies_owned_state_and_binds_stable_uuid(external_state_filesystem, alternate):
    namespace, table, created, _, state_uuid = external_state_filesystem
    policy = dict(namespace["POLICY"])
    mount = "/mnt/scratch" if alternate else "/"
    if alternate:
        policy["consumer_state_root"] = mount + "/new"
        table[mount] = {"type": "xfs", "device": "8:32", "options": {"rw"}, "root": "/"}
    root, identity = namespace["consumer_state"](policy, {"device": "8:16"})
    assert str(root) == policy["consumer_state_root"]
    assert created == [str(root)]
    assert identity["state_uuid"] == state_uuid
    table[mount]["device"] = "8:48"
    assert namespace["consumer_state"](policy, {"device": "8:16"})[1] == identity


@pytest.mark.parametrize("failure", ["missing", "subtree", "readonly", "store", "identity"])
def test_external_consumer_refuses_unqualified_scratch_without_mutation(external_state_filesystem, failure):
    namespace, table, created, available, _ = external_state_filesystem
    policy = {**namespace["POLICY"], "consumer_state_root": "/mnt/scratch/new"}
    table["/mnt/scratch"] = {"type": "ext4", "device": "8:32", "options": {"rw"}, "root": "/"}
    if failure == "missing":
        del table["/mnt/scratch"]
    elif failure == "subtree":
        table["/mnt/scratch"]["root"] = "/aliased-subtree"
    elif failure == "readonly":
        table["/mnt/scratch"]["options"] = {"ro"}
    elif failure == "store":
        table["/mnt/scratch"]["device"] = "8:16"
    else:
        available["vm"] = False
    with pytest.raises(RuntimeError):
        namespace["consumer_state"](policy, {"device": "8:16"})
    assert created == []
