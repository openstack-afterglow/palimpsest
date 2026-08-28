"""Deterministic NoCloud ``user-data``/``meta-data`` generation for Palimpsest guests.

Pure text generation — stdlib only, no libvirt, no subprocess. Given per-run key
material and a pre-built layer activation script (see
:func:`palimpsest_local.kvm.build_layer_activation_script`), this module renders the
exact ``#cloud-config`` bytes that seed a run's NoCloud ISO: the fixed ``ubuntu`` guest
user with the run's client public key installed, the run's host Ed25519 key pinned so
SSH host verification works from boot, the layer-activation systemd service, and the
``/usr/local/libexec/palimpsest-exec`` helper that lets
:mod:`palimpsest_local.guest`'s ``exec`` path avoid ever composing a remote shell
string.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path, PurePosixPath

from .errors import LifecycleError as GuestError

GUEST_USER = "ubuntu"
EXEC_HELPER_PATH = "/usr/local/libexec/palimpsest-exec"
ACTIVATION_SCRIPT_PATH = "/usr/local/libexec/palimpsest-activate"
ACTIVATION_UNIT_PATH = "/etc/systemd/system/palimpsest-activate.service"
ACTIVATION_UNIT_NAME = "palimpsest-activate.service"
READY_SCRIPT_PATH = "/usr/local/libexec/palimpsest-ready"
READY_UNIT_PATH = "/etc/systemd/system/palimpsest-ready.service"
READY_UNIT_NAME = "palimpsest-ready.service"
CONSOLE_DEVICE = "/dev/ttyS0"
BUILD_CHANNEL_NAME = "org.afterglow.palimpsest.builder.v1"
BUILD_JOB_PATH = "/etc/palimpsest/build-job.json"
BUILD_WORKER_PATH = "/usr/local/libexec/palimpsest-builder"
BUILD_UNIT_PATH = "/etc/systemd/system/palimpsest-builder.service"
BUILD_UNIT_NAME = "palimpsest-builder.service"
PROJECT_INIT_PATH = "/usr/local/libexec/palimpsest-project-init"
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_GUEST_PATHS = (
    "/dev",
    "/proc",
    "/sys",
    "/etc/palimpsest",
    "/opt/layers",
    "/mnt/palimpsest",
    EXEC_HELPER_PATH,
    ACTIVATION_SCRIPT_PATH,
    ACTIVATION_UNIT_PATH,
    READY_SCRIPT_PATH,
    READY_UNIT_PATH,
    PROJECT_INIT_PATH,
)

_BUILD_WORKER_SOURCE = r'''#!/usr/bin/env python3
"""One-shot, seed-defined Palimpsest builder.

The host never sends commands over the virtio channel.  This process executes the
fixed recipe embedded in the read-only NoCloud seed and emits a framed result only
after it has produced the verified candidate SquashFS image.
"""

import hashlib
import json
import os
import shutil
import shlex
import subprocess
import sys
import traceback


CHANNEL = os.environ.get("PALIMPSEST_BUILD_CHANNEL")
RESULT_PATH = os.environ.get("PALIMPSEST_BUILD_RESULT", "/tmp/palimpsest-build-result.json")
JOB_PATH = "/etc/palimpsest/build-job.json"
CAPTURE = "/mnt/palimpsest/capture"
_STAGE = "initializing"
_RUN_LINE = None
OUTPUT = "/tmp/palimpsest-output.squashfs"


def _run(argv, *, env=None):
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL, env=env)


def _write_all(stream, data):
    view = memoryview(data)
    while view:
        count = stream.write(view)
        if not count:
            raise RuntimeError("virtio serial write failed")
        view = view[count:]


def _mount_chroot_support(job):
    merged = CAPTURE + "/merged"
    _run(["mount", "-t", "proc", "proc", merged + "/proc"])
    _run(["mount", "--rbind", "/sys", merged + "/sys"])
    _run(["mount", "--make-rslave", merged + "/sys"])
    _run(["mount", "--rbind", "/dev", merged + "/dev"])
    _run(["mount", "--make-rslave", merged + "/dev"])
    _run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", merged + "/run"])
    if job["network"] == "default":
        resolver = merged + "/etc/resolv.conf"
        if os.path.lexists(resolver):
            os.unlink(resolver)
        shutil.copyfile("/etc/resolv.conf", resolver)


def _unmount_chroot_support(*, strict=False):
    merged = CAPTURE + "/merged"
    failed = False
    for path in (merged + "/run", merged + "/dev", merged + "/sys", merged + "/proc"):
        result = subprocess.run(["umount", "-R", path], stdin=subprocess.DEVNULL, check=False)
        failed = failed or result.returncode != 0
    if strict and failed:
        raise RuntimeError("failed to unmount chroot support filesystems")


def _clear(path):
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    for entry in os.scandir(path):
        child = entry.path
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            os.unlink(child)
        else:
            shutil.rmtree(child)


def _clean(upper):
    for relative in ("tmp", "run", "var/tmp", "var/cache/apt", "var/lib/apt/lists", "var/log", "root"):
        _clear(os.path.join(upper, relative))
    for relative in ("etc/machine-id", "etc/resolv.conf", "etc/hosts"):
        _clear(os.path.join(upper, relative))


def _local_capture_check():
    fstype_upper = subprocess.check_output(["findmnt", "-no", "FSTYPE", "-T", CAPTURE + "/upper"], text=True).strip()
    fstype_work = subprocess.check_output(["findmnt", "-no", "FSTYPE", "-T", CAPTURE + "/work"], text=True).strip()
    for fstype in (fstype_upper, fstype_work):
        if fstype in {"nfs", "nfs4", "ceph", "virtiofs"} or fstype.startswith("fuse."):
            raise RuntimeError("capture filesystem is not local")
    if os.stat(CAPTURE + "/upper").st_dev != os.stat(CAPTURE + "/work").st_dev:
        raise RuntimeError("capture upper and work directories differ")


def _build():
    global _RUN_LINE, _STAGE
    with open(JOB_PATH, encoding="utf-8") as fp:
        job = json.load(fp)
    parents = job["parent_mounts"]
    lowerdir = ":".join(reversed(parents)) + ":/" if parents else "/"
    os.makedirs(CAPTURE, mode=0o700, exist_ok=True)
    _run(["mount", "-t", "tmpfs", "-o", "mode=0700", "tmpfs", CAPTURE])
    try:
        for path in ("upper", "work", "merged"):
            os.makedirs(os.path.join(CAPTURE, path), mode=0o700, exist_ok=True)
        mounted = False
        try:
            _STAGE = "mount"
            _run(["mount", "-t", "overlay", "overlay", "-o", "lowerdir=%s,upperdir=%s/upper,workdir=%s/work" % (lowerdir, CAPTURE, CAPTURE), CAPTURE + "/merged"])
            mounted = True
            _STAGE = "chroot-support"
            _mount_chroot_support(job)
            for instruction in job["runs"]:
                _STAGE = "run"
                _RUN_LINE = instruction["line"]
                env = {"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
                exported = " ".join("%s=%s" % (key, shlex.quote(value)) for key, value in instruction["env"].items())
                _run(["mkdir", "-p", CAPTURE + "/merged" + instruction["workdir"]])
                command = (("export %s; " % exported) if exported else "") + "cd %s && %s" % (shlex.quote(instruction["workdir"]), instruction["command"])
                _run(["chroot", CAPTURE + "/merged", "/bin/bash", "-lc", command], env=env)
            _run(["sync"])
            _unmount_chroot_support(strict=True)
        finally:
            _unmount_chroot_support()
            if mounted:
                subprocess.run(["umount", CAPTURE + "/merged"], stdin=subprocess.DEVNULL, check=False)
        _STAGE = "capture-check"
        _local_capture_check()
        _STAGE = "clean"
        _clean(CAPTURE + "/upper")
        _STAGE = "pack"
        _run(["mksquashfs", CAPTURE + "/upper", OUTPUT, "-comp", "zstd", "-Xcompression-level", "3", "-noappend", "-no-exports"])
    finally:
        subprocess.run(["umount", CAPTURE], stdin=subprocess.DEVNULL, check=False)
    with open(OUTPUT, "rb") as fp:
        if fp.read(4) != b"hsqs":
            raise RuntimeError("mksquashfs output has invalid magic")
    size = os.path.getsize(OUTPUT)
    digest = hashlib.sha256()
    with open(OUTPUT, "rb") as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(block)
    return size, digest.hexdigest()


def _emit(result, output=None):
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if CHANNEL:
        with open(CHANNEL, "wb", buffering=0) as channel:
            _write_all(channel, encoded)
            if output is not None:
                with open(output, "rb") as fp:
                    while block := fp.read(32 * 1024):
                        _write_all(channel, block)
        return
    with open(RESULT_PATH, "wb") as result_file:
        _write_all(result_file, encoded)

def main():
    try:
        size, digest = _build()
        _emit({"status": "ok", "size": size, "sha256": digest, "version": 1}, OUTPUT)
    except Exception:
        traceback.print_exc()
        error = {"status": "error", "stage": _STAGE, "version": 1}
        if _RUN_LINE is not None:
            error["line"] = _RUN_LINE
        _emit(error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
READY_SENTINEL = "PALIMPSEST_READY=1"

# Runs standalone on the guest (no palimpsest_local package there): decodes the
# URL-safe base64 JSON argv payload passed as the sole argument and execs it
# directly. Keep in lockstep with palimpsest_local.guest.encode_exec_payload.
_EXEC_HELPER_SOURCE = '''#!/usr/bin/env python3
"""Palimpsest guest exec helper.

Decodes a URL-safe base64 JSON argv payload passed as the sole command-line
argument and execs it directly via os.execvpe, replacing this process. No shell
is ever invoked on the guest for `palimpsest exec` traffic; only this fixed
path plus an alphabet-constrained payload cross the SSH remote-command
boundary, so OpenSSH handing the remote command to the login shell for
parsing cannot reintroduce quoting bugs.
"""

import base64
import json
import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: palimpsest-exec <payload>\\n")
        return 2
    payload = sys.argv[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        argv = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        sys.stderr.write(f"palimpsest-exec: invalid payload: {exc}\\n")
        return 1
    if not isinstance(argv, list) or not argv:
        sys.stderr.write("palimpsest-exec: argv must be a nonempty list\\n")
        return 1
    if not all(isinstance(item, str) and "\\x00" not in item for item in argv):
        sys.stderr.write("palimpsest-exec: argv items must be NUL-free strings\\n")
        return 1
    os.execvpe(argv[0], argv, os.environ)
    return 1  # pragma: no cover -- execvpe never returns on success


if __name__ == "__main__":
    sys.exit(main())
'''


def read_key_material(value: Path | str) -> str:
    """Return raw text for key material supplied as a file path or literal text."""
    text = value.read_text(encoding="utf-8") if isinstance(value, Path) else value
    if not text.strip():
        raise GuestError("key material must be nonempty")
    return text


def read_public_key_line(value: Path | str) -> str:
    """Return a normalized single-line SSH public key value."""
    text = read_key_material(value).strip()
    if "\n" in text or "\r" in text or "\x00" in text:
        raise GuestError("public key material must be a single line")
    return text


def _literal_block(text: str, indent: int) -> str:
    """Render ``text`` as the body of a YAML block-literal scalar at ``indent`` spaces."""
    prefix = " " * indent
    return "\n".join(f"{prefix}{line}" if line else "" for line in text.splitlines())


def build_meta_data(instance_id: str, *, hostname: str = "palimpsest") -> str:
    """Render NoCloud ``meta-data`` content."""
    for label, value in (("instance_id", instance_id), ("hostname", hostname)):
        if not value or any(ch in value for ch in "\n\r\x00"):
            raise GuestError(f"{label} must be a nonempty single-line value")
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"


def build_activation_unit(activation_script: str, *, emit_ready: bool = True) -> tuple[str, str]:
    """Return ``(helper_script_text, systemd_unit_text)`` for layer activation.

    Wraps ``activation_script`` (e.g. from
    :func:`palimpsest_local.kvm.build_layer_activation_script`) so every line it emits
    is appended to the console device, then writes the readiness sentinel once it
    completes without error.
    """
    if not activation_script.strip():
        raise GuestError("activation_script must be nonempty")
    body = activation_script if activation_script.endswith("\n") else activation_script + "\n"
    helper_script = "#!/bin/bash\nset -euo pipefail\nexec >>" + CONSOLE_DEVICE + " 2>&1\n" + body
    if emit_ready:
        helper_script += f"echo {READY_SENTINEL}\n"
    unit_text = (
        "[Unit]\n"
        "Description=Palimpsest layer activation\n"
        "After=local-fs.target\n"
        "Before=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={ACTIVATION_SCRIPT_PATH}\n"
        "RemainAfterExit=yes\n"
        "StandardOutput=journal+console\n"
        "StandardError=journal+console\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return helper_script, unit_text


def build_user_data(
    *,
    client_public_key: Path | str,
    host_private_key: Path | str,
    host_public_key: Path | str,
    activation_script: str,
    environment: tuple[tuple[str, str], ...] = (),
    cloud_init: object | None = None,
) -> str:
    """Render the full NoCloud ``user-data`` ``#cloud-config`` document for one run.

    ``client_public_key``/``host_private_key``/``host_public_key`` accept either the
    literal key text or a ``Path`` to read it from — callers may hold generated key
    material in memory or on disk under a run's ``ssh/`` directory.
    """
    client_key = read_public_key_line(client_public_key)
    host_public = read_public_key_line(host_public_key)
    host_private = read_key_material(host_private_key).rstrip("\n")
    helper_script, unit_text = build_activation_unit(activation_script, emit_ready=False)
    ready_script = f"#!/bin/bash\nset -euo pipefail\necho {READY_SENTINEL} >>{CONSOLE_DEVICE}\n"
    ready_unit = (
        "[Unit]\n"
        "Description=Palimpsest boot readiness\n"
        f"Requires={ACTIVATION_UNIT_NAME}\n"
        f"After={ACTIVATION_UNIT_NAME} cloud-final.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={READY_SCRIPT_PATH}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    environment_lines: list[str] = []
    seen_environment: set[str] = set()
    for name, value in environment:
        if (
            _ENVIRONMENT_NAME_RE.fullmatch(name) is None
            or name in seen_environment
            or any(character in value for character in ("\x00", "\n", "\r"))
        ):
            raise GuestError("environment entries must have unique names and single-line values")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        environment_lines.append(f'{name}="{escaped}"')
        seen_environment.add(name)

    packages = tuple(getattr(cloud_init, "packages", ())) if cloud_init is not None else ()
    write_files = tuple(getattr(cloud_init, "write_files", ())) if cloud_init is not None else ()
    commands = tuple(getattr(cloud_init, "runcmd", ())) if cloud_init is not None else ()
    if not all(isinstance(package, str) and package and "\x00" not in package for package in packages):
        raise GuestError("cloud-init packages entries must be nonempty NUL-free strings")
    project_script_lines = ["#!/bin/bash", "set -euo pipefail", f"exec >>{CONSOLE_DEVICE} 2>&1"]
    for command in commands:
        if not isinstance(command, tuple) or not command or not all(isinstance(argument, str) for argument in command):
            raise GuestError("cloud-init runcmd entries must be nonempty argv tuples")
        project_script_lines.append(shlex.join(command))
    project_script_lines.append(f"echo {READY_SENTINEL}")
    project_script = "\n".join(project_script_lines) + "\n"

    lines = [
        "#cloud-config",
        "users:",
        f"  - name: {GUEST_USER}",
        "    lock_passwd: true",
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    groups: [sudo]",
        "    ssh_authorized_keys:",
        f"      - {client_key}",
        "",
        "ssh_deletekeys: false",
        "ssh_keys:",
        "  ed25519_private: |",
        _literal_block(host_private, 4),
        f"  ed25519_public: {host_public}",
        "",
        "write_files:",
        f"  - path: {EXEC_HELPER_PATH}",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        _literal_block(_EXEC_HELPER_SOURCE.rstrip("\n"), 6),
        f"  - path: {ACTIVATION_SCRIPT_PATH}",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        _literal_block(helper_script.rstrip("\n"), 6),
        f"  - path: {ACTIVATION_UNIT_PATH}",
        "    permissions: '0644'",
        "    owner: root:root",
        "    content: |",
        _literal_block(unit_text.rstrip("\n"), 6),
        f"  - path: {READY_SCRIPT_PATH}",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        _literal_block(ready_script.rstrip("\n"), 6),
        f"  - path: {READY_UNIT_PATH}",
        "    permissions: '0644'",
        "    owner: root:root",
        "    content: |",
        _literal_block(ready_unit.rstrip("\n"), 6),
        f"  - path: {PROJECT_INIT_PATH}",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        _literal_block(project_script.rstrip("\n"), 6),
    ]
    for item in write_files:
        path = getattr(item, "path", None)
        content = getattr(item, "content", None)
        permissions = getattr(item, "permissions", None)
        if not all(isinstance(value, str) for value in (path, content, permissions)):
            raise GuestError("cloud-init write_files entries are invalid")
        pure_path = PurePosixPath(path)
        if not pure_path.is_absolute() or ".." in pure_path.parts:
            raise GuestError("cloud-init write_files paths must be normalized absolute guest paths")
        normalized_path = str(pure_path)
        if any(
            normalized_path == reserved or normalized_path.startswith(reserved + "/")
            for reserved in _RESERVED_GUEST_PATHS
        ):
            raise GuestError(f"cloud-init write_files path is reserved by Palimpsest: {normalized_path}")
        if re.fullmatch(r"0[0-7]{3}", permissions) is None:
            raise GuestError("cloud-init write_files permissions must be a four-digit octal string")
        lines.extend(
            [
                f"  - path: {json.dumps(normalized_path)}",
                f"    permissions: {json.dumps(permissions)}",
                "    owner: root:root",
                "    content: |",
                _literal_block(content, 6),
            ]
        )
    if environment_lines:
        lines.extend(
            [
                "  - path: /etc/environment",
                "    permissions: '0644'",
                "    owner: root:root",
                "    append: true",
                "    content: |",
                _literal_block("\n".join(environment_lines), 6),
            ]
        )
    if packages:
        lines.extend(["", "packages:", *(f"  - {json.dumps(package)}" for package in packages)])
    lines.extend(
        [
            "",
            "runcmd:",
            "  - systemctl daemon-reload",
            f"  - systemctl enable --now {ACTIVATION_UNIT_NAME}",
            f"  - systemctl enable {READY_UNIT_NAME}",
            f"  - {PROJECT_INIT_PATH}",
            "",
        ]
    )
    return "\n".join(lines)


def build_serial_builder_user_data(*, activation_script: str, job: dict[str, object]) -> str:
    """Render credential-free NoCloud data for a seed-defined serial-only builder."""
    activation_helper, activation_unit = build_activation_unit(activation_script)
    job_json = json.dumps(job, sort_keys=True, separators=(",", ":"))
    worker = _BUILD_WORKER_SOURCE.rstrip("\n")
    builder_unit = (
        "[Unit]\n"
        "Description=Palimpsest serial-only builder\n"
        f"Requires={ACTIVATION_UNIT_NAME}\n"
        f"After={ACTIVATION_UNIT_NAME}\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=PALIMPSEST_BUILD_CHANNEL=/dev/virtio-ports/{BUILD_CHANNEL_NAME}\n"
        f"ExecStart={BUILD_WORKER_PATH}\n"
        "StandardOutput=journal+console\n"
        "StandardError=journal+console\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    lines = [
        "#cloud-config",
        "ssh_pwauth: false",
        "disable_root: true",
        "write_files:",
        f"  - path: {BUILD_JOB_PATH}",
        "    permissions: '0600'",
        "    owner: root:root",
        "    content: |",
        _literal_block(job_json, 6),
        f"  - path: {BUILD_WORKER_PATH}",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        _literal_block(worker, 6),
        f"  - path: {ACTIVATION_SCRIPT_PATH}",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        _literal_block(activation_helper.rstrip("\n"), 6),
        f"  - path: {ACTIVATION_UNIT_PATH}",
        "    permissions: '0644'",
        "    owner: root:root",
        "    content: |",
        _literal_block(activation_unit.rstrip("\n"), 6),
        f"  - path: {BUILD_UNIT_PATH}",
        "    permissions: '0644'",
        "    owner: root:root",
        "    content: |",
        _literal_block(builder_unit.rstrip("\n"), 6),
        "",
        "runcmd:",
        "  - systemctl daemon-reload",
        f"  - systemctl enable --now {ACTIVATION_UNIT_NAME}",
        f"  - systemctl enable --now {BUILD_UNIT_NAME}",
        "",
    ]
    return "\n".join(lines)
