"""Explicit, conservative host qualification for the first public OCI backend.

Kernel/config digests pin the operator's qualified pair; they do not prove that
the kernel was compiled from that config. Existing ancestors are never changed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ._oci_stage1_kvm_proof import verify_kernel_config, verify_kvm_api
from .errors import StateError
from .oci_initramfs import build_bootstrap_initramfs
from .oci_root_kvm import _verify_host_boot_artifact, verify_first_party_bootstrap_initramfs, verify_host_boot_artifacts


@dataclass(frozen=True, slots=True)
class OCIHostConfig:
    kernel: Path
    kernel_digest: str
    kernel_config: Path
    kernel_config_digest: str
    packer: Path

    def __post_init__(self):
        for path in (self.kernel, self.kernel_config, self.packer):
            if not isinstance(path, Path) or not path.is_absolute() or "\0" in str(path):
                raise StateError("OCI host artifact paths must be explicit and absolute")
        for digest in (self.kernel_digest, self.kernel_config_digest):
            if type(digest) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise StateError("OCI host requires canonical kernel and config digests")

    @classmethod
    def from_environment(cls, environment=None):
        env = os.environ if environment is None else environment
        names = ("KERNEL", "KERNEL_DIGEST", "KERNEL_CONFIG", "KERNEL_CONFIG_DIGEST", "PACKER")
        values = []
        for name in names:
            value = env.get("PALIMPSEST_OCI_" + name)
            if type(value) is not str or not value:
                raise StateError("OCI host configuration is missing PALIMPSEST_OCI_" + name)
            values.append(value)
        return cls(Path(values[0]), values[1], Path(values[2]), values[3], Path(values[4]))


def verify_runtime_parent(path: Path, *, pin: bool = False) -> int | None:
    """Require a no-symlink, already searchable chain with simple POSIX ACLs.

    Named/default ACLs are deliberately unsupported here; product-owned state
    below this parent uses its own exact named-QEMU ACL receipts.
    """
    if type(pin) is not bool or not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise StateError("OCI runtime parent must be an absolute canonical path")
    descriptors = []
    stamps = []
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_ctime_ns")
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(descriptor)
        for component in (None, *path.parts[1:]):
            if component is not None:
                descriptor = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
                )
                descriptors.append(descriptor)
            before = os.fstat(descriptor)
            mode = stat.S_IMODE(before.st_mode)
            if before.st_uid not in {0, os.geteuid()} or mode & 0o111 != 0o111:
                raise StateError(
                    "OCI runtime ancestors must already permit QEMU search; use a dedicated runtime parent"
                )
            if mode & 0o022 and not (before.st_uid == 0 and mode & stat.S_ISVTX):
                raise StateError("OCI runtime ancestor is writable by another principal")
            result = subprocess.run(
                ["getfacl", "-cpn", "--", f"/proc/self/fd/{descriptor}"],
                pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                env={"PATH": os.defpath, "LC_ALL": "C"},
                check=False,
            )
            if result.returncode != 0 or len(result.stdout) > 4096:
                raise StateError("OCI runtime ancestor ACL could not be verified")
            lines = result.stdout.decode("ascii").strip().splitlines()
            if (
                len(lines) != 3
                or any(
                    re.fullmatch(prefix + r"[r-][w-][x-]", line) is None
                    for prefix, line in zip(("user::", "group::", "other::"), lines, strict=True)
                )
                or not all(line.endswith("x") for line in lines)
            ):
                raise StateError(
                    "OCI runtime ancestors require simple search-enabled ACLs; named/default ACLs are unsupported"
                )
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_ctime_ns,
            ):
                raise StateError("OCI runtime ancestor changed during verification")
            stamps.append(tuple(getattr(before, key) for key in fields))
        # Verify the visible chain again so renaming an already-open ancestor
        # never makes a detached directory qualify a different runtime path.
        visible = path
        for descriptor, stamp in zip(reversed(descriptors), reversed(stamps), strict=True):
            held, current = os.fstat(descriptor), visible.lstat()
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino) or tuple(
                getattr(held, key) for key in fields
            ) != stamp:
                raise StateError("OCI runtime ancestor changed during verification")
            visible = visible.parent
        return os.dup(descriptors[-1]) if pin else None
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise StateError("OCI runtime parent cannot be safely verified") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def create_runtime_parent(path: Path) -> Path:
    """Create only a new explicit search-only parent; never adopt/chmod one."""
    descriptor = verify_runtime_parent(path.parent, pin=True)
    child = -1
    try:
        held_parent, visible_parent = os.fstat(descriptor), path.parent.lstat()
        if (held_parent.st_dev, held_parent.st_ino) != (visible_parent.st_dev, visible_parent.st_ino):
            raise StateError("OCI runtime parent changed before creation")
        os.mkdir(path.name, 0o700, dir_fd=descriptor)
        expected = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
        held = os.fstat(child)
        if (
            (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino)
            or os.listdir(child)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o700
        ):
            raise StateError("new OCI runtime parent ownership is invalid")
        os.fchmod(child, 0o711)
        os.fsync(child)
        os.fsync(descriptor)
        verify_runtime_parent(path)
        return path
    except OSError:
        raise StateError("OCI runtime parent creation failed; existing or partial paths are preserved") from None
    finally:
        if child >= 0:
            os.close(child)
        os.close(descriptor)


def preflight_oci_host(config: OCIHostConfig, roots, name: str) -> None:
    config.__post_init__()
    verify_kvm_api()
    for executable in ("qemu-system-x86_64", "qemu-img", "mkfs.ext4", "getfacl", "setfacl"):
        if shutil.which(executable, path=os.defpath) is None:
            raise StateError("OCI host tool is unavailable: " + executable)
    verify_runtime_parent(roots.state.parent)
    # Lifecycle pathname must leave libvirt's temporary binding suffix room.
    if len(os.fsencode(roots.runs / name / "io" / "lifecycle.sock")) > 97:
        raise StateError("OCI runtime path is too long for the lifecycle socket")
    _verify_host_boot_artifact(
        config.kernel, kind="kernel", maximum=512 * 1024 * 1024, expected_digest=config.kernel_digest
    )
    if verify_kernel_config(config.kernel_config).digest != config.kernel_config_digest:
        raise StateError("OCI host kernel config digest changed")
    probe = subprocess.run(
        [sys.executable, "-c", "import libvirt"],
        cwd=Path(__file__).resolve().parent.parent,
        env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0:
        raise StateError("OCI monitor Python must import libvirt without PYTHONPATH")


@contextmanager
def first_party_boot(config: OCIHostConfig, roots):
    """Keep the packaged source initramfs alive until run-owned BOOT is copied."""
    built = build_bootstrap_initramfs()
    with tempfile.TemporaryDirectory(prefix="oci-boot-", dir=roots.state) as scratch:
        path = Path(scratch) / "initramfs"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(built.payload)
                stream.flush()
            os.fchmod(fd, 0o400)
            os.fsync(fd)
        finally:
            os.close(fd)
        initramfs = verify_first_party_bootstrap_initramfs(path, built.manifest)
        yield verify_host_boot_artifacts(
            config.kernel,
            path,
            expected_kernel_digest=config.kernel_digest,
            expected_initramfs_digest=initramfs.digest,
        )
