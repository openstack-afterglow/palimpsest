"""Path-free deterministic SquashFS packing for staged OCI changesets."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .digest import normalize_digest
from .errors import ArtifactValidationError, UnsupportedPlatformError
from .oci_changeset import EntryKind
from .oci_converter import StagedLayer
from .oci_tar_emitter import TarEmissionReceipt, normalized_xattr_bytes

_IO_CHUNK = 1024 * 1024
_SQUASHFS_SUPERBLOCK_SIZE = 96
_SQUASHFS_MAGIC = 0x73717368
_UINT64_MAX = (1 << 64) - 1
SQUASHFS_BLOCK_DEVICE_ALIGNMENT = 512

SQUASHFS_PACK_POLICY_ID = "palimpsest.oci-squashfs-pack.v1"
SQUASHFS_PACKER_ARGV_CONTRACT_ID = "palimpsest.oci-squashfs-mksquashfs-argv.v2"
SQUASHFS_STRUCTURAL_VERIFIER_ID = "palimpsest.squashfs-superblock.v2"
SQUASHFS_TOOLCHAIN_ID = "palimpsest.oci-squashfs-toolchain.v1"


class SquashFSPackError(ArtifactValidationError):
    """Stable path-free failure from the staged SquashFS pack boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class SquashFSPackPolicy:
    max_normalized_tar_bytes: int = 33 * 1024**3
    max_image_bytes: int = 32 * 1024**3
    max_root_xattr_bytes: int = 64 * 1024
    packer_timeout_seconds: float = 120.0
    terminate_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        for value in (self.max_normalized_tar_bytes, self.max_image_bytes, self.max_root_xattr_bytes):
            if type(value) is not int or value <= 0:
                raise SquashFSPackError("oci-pack-policy", "artifact byte limits must be positive")
        for value in (self.packer_timeout_seconds, self.terminate_grace_seconds):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < float(value) < float("inf"):
                raise SquashFSPackError("oci-pack-policy", "packer time limits must be finite and positive")

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            {
                "domain": SQUASHFS_PACK_POLICY_ID,
                "max_normalized_tar_bytes": self.max_normalized_tar_bytes,
                "max_image_bytes": self.max_image_bytes,
                "max_root_xattr_bytes": self.max_root_xattr_bytes,
                "packer_timeout_seconds": float(self.packer_timeout_seconds),
                "terminate_grace_seconds": float(self.terminate_grace_seconds),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


DEFAULT_SQUASHFS_PACK_POLICY = SquashFSPackPolicy()


@dataclass(frozen=True, slots=True)
class SquashFSPackExecution:
    """Process/scratch boundary selected by an outer hard supervisor."""

    scratch_root: Path | None = field(default=None, repr=False)
    inherit_process_group: bool = False

    def __post_init__(self) -> None:
        if type(self.inherit_process_group) is not bool:
            raise SquashFSPackError("oci-pack-execution", "process-group policy is invalid")
        if not self.inherit_process_group:
            if self.scratch_root is not None:
                raise SquashFSPackError("oci-pack-execution", "standalone packing cannot use supervisor scratch")
            return
        if not isinstance(self.scratch_root, Path) or not self.scratch_root.is_absolute():
            raise SquashFSPackError("oci-pack-execution", "supervisor scratch root is invalid")
        try:
            opened = os.stat(self.scratch_root, follow_symlinks=False)
        except OSError:
            raise SquashFSPackError("oci-pack-execution", "supervisor scratch root is unavailable") from None
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
            raise SquashFSPackError("oci-pack-execution", "supervisor scratch root is unsafe")


DEFAULT_SQUASHFS_PACK_EXECUTION = SquashFSPackExecution()


@dataclass(frozen=True, slots=True)
class SquashFSToolchainIdentity:
    """Path-free identity for the executable and linked packer dependencies."""

    version: str
    executable_digest: str
    dependency_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version or "") is None:
            raise SquashFSPackError("oci-packer-version", "toolchain version is invalid")
        object.__setattr__(self, "executable_digest", normalize_digest(self.executable_digest))
        if not isinstance(self.dependency_digests, tuple):
            raise SquashFSPackError("oci-packer-toolchain", "dependency digests must be an immutable tuple")
        normalized = tuple(sorted(normalize_digest(value) for value in self.dependency_digests))
        if len(set(normalized)) != len(normalized):
            raise SquashFSPackError("oci-packer-toolchain", "dependency digests must be unique")
        object.__setattr__(self, "dependency_digests", normalized)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "dependency_digests": list(self.dependency_digests),
                "domain": SQUASHFS_TOOLCHAIN_ID,
                "executable_digest": self.executable_digest,
                "version": self.version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class _ToolchainFileBinding:
    path: Path = field(repr=False)
    digest: str
    signature: tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class VerifiedSquashFSToolchain:
    """Verified executable/dependency capability used for both cache keys and packing."""

    identity: SquashFSToolchainIdentity
    _packer_path: Path = field(repr=False)
    _dependencies: tuple[_ToolchainFileBinding, ...] = field(repr=False)

    def verify(self, packer_path: Path, expected_packer_sha256: str) -> None:
        expected = normalize_digest(f"sha256:{expected_packer_sha256}")
        try:
            selected = packer_path.resolve(strict=True)
        except OSError:
            raise SquashFSPackError("oci-packer-toolchain", "selected packer is unavailable") from None
        if selected != self._packer_path or expected != self.identity.executable_digest:
            raise SquashFSPackError("oci-packer-toolchain", "selected packer does not match its capability")
        executable = _bind_toolchain_file(selected)
        if executable.digest != expected:
            raise SquashFSPackError("oci-packer-toolchain", "selected packer bytes changed")
        paths = _discover_dependency_paths(selected)
        if paths != tuple(binding.path for binding in self._dependencies):
            raise SquashFSPackError("oci-packer-toolchain", "selected packer dependencies changed")
        current = tuple(_bind_toolchain_file(path) for path in paths)
        if current != self._dependencies:
            raise SquashFSPackError("oci-packer-toolchain", "selected packer dependency bytes changed")


def _bind_toolchain_file(path: Path) -> _ToolchainFileBinding:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd: int | None = None
    try:
        resolved = path.resolve(strict=True)
        fd = os.open(resolved, flags)
        opened = os.fstat(fd)
        visible = os.stat(resolved, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_mode & 0o022
            or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
        ):
            raise SquashFSPackError("oci-packer-toolchain", "toolchain file is unsafe")
        digest = _sha256_fd(fd, opened.st_size)
        after = os.fstat(fd)
        final = os.stat(resolved, follow_symlinks=False)
        signature = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        if (
            signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            )
            != signature
        ):
            raise SquashFSPackError("oci-packer-toolchain", "toolchain file changed during verification")
        return _ToolchainFileBinding(resolved, digest, signature)
    except SquashFSPackError:
        raise
    except OSError:
        raise SquashFSPackError("oci-packer-toolchain", "toolchain file cannot be verified") from None
    finally:
        _close_fd(fd)


def _discover_dependency_paths(packer_path: Path) -> tuple[Path, ...]:
    inspector_path = next((path for path in (Path("/usr/bin/ldd"), Path("/bin/ldd")) if path.is_file()), None)
    if inspector_path is None:
        raise SquashFSPackError("oci-packer-toolchain", "dynamic dependency inspector is unavailable")
    inspector = _bind_toolchain_file(inspector_path)
    try:
        inspector_owner = inspector.path.stat().st_uid
    except OSError:
        raise SquashFSPackError("oci-packer-toolchain", "dynamic dependency inspector is unavailable") from None
    if inspector_owner not in {0, os.geteuid()}:
        raise SquashFSPackError("oci-packer-toolchain", "dynamic dependency inspector is unsafe")
    try:
        result = subprocess.run(
            [os.fspath(inspector.path), os.fspath(packer_path)],
            check=False,
            capture_output=True,
            env=_fixed_env(),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SquashFSPackError("oci-packer-toolchain", "dynamic dependencies cannot be inspected") from None
    if _bind_toolchain_file(inspector.path) != inspector:
        raise SquashFSPackError("oci-packer-toolchain", "dynamic dependency inspector changed")
    output = result.stdout + b"\n" + result.stderr
    lowered = output.lower()
    if b"not a dynamic executable" in lowered or b"statically linked" in lowered:
        return ()
    if result.returncode != 0 or b"not found" in lowered:
        raise SquashFSPackError("oci-packer-toolchain", "dynamic dependency resolution failed")
    candidates: set[Path] = set()
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        candidate = line.split("=>", 1)[1].strip().split()[0] if "=>" in line else line.split()[0]
        if not candidate.startswith("/"):
            continue
        try:
            candidates.add(Path(candidate).resolve(strict=True))
        except OSError:
            raise SquashFSPackError("oci-packer-toolchain", "dynamic dependency is unavailable") from None
    return tuple(sorted(candidates, key=os.fspath))


def discover_squashfs_toolchain(
    packer_path: Path,
    *,
    expected_packer_sha256: str,
    policy: SquashFSPackPolicy = DEFAULT_SQUASHFS_PACK_POLICY,
) -> VerifiedSquashFSToolchain:
    """Discover and seal the real mksquashfs executable plus every linked file."""
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("SquashFS toolchain discovery requires Linux")
    if re.fullmatch(r"[0-9a-f]{64}", expected_packer_sha256 or "") is None:
        raise SquashFSPackError("oci-packer-digest", "expected packer SHA-256 is invalid")
    executable = _bind_toolchain_file(packer_path)
    if executable.digest != f"sha256:{expected_packer_sha256}":
        raise SquashFSPackError("oci-packer-digest", "selected packer digest does not match")
    try:
        result = subprocess.run(
            [os.fspath(executable.path), "-version"],
            check=False,
            capture_output=True,
            env=_fixed_env(),
            timeout=min(30.0, float(policy.packer_timeout_seconds)),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SquashFSPackError("oci-packer-version", "filesystem packer version check failed") from None
    match = re.search(rb"mksquashfs version (\d+)\.(\d+)(?:\.(\d+))?", result.stdout + result.stderr, re.I)
    if result.returncode != 0 or match is None:
        raise SquashFSPackError("oci-packer-version", "filesystem packer version is not identifiable")
    version_tuple = tuple(int(item or b"0") for item in match.groups())
    if version_tuple < (4, 6, 0):
        raise SquashFSPackError("oci-packer-version", "filesystem packer is older than 4.6.0")
    dependencies = tuple(_bind_toolchain_file(path) for path in _discover_dependency_paths(executable.path))
    identity = SquashFSToolchainIdentity(
        ".".join(str(item) for item in version_tuple),
        executable.digest,
        tuple(sorted({binding.digest for binding in dependencies})),
    )
    capability = VerifiedSquashFSToolchain(identity, executable.path, dependencies)
    capability.verify(packer_path, expected_packer_sha256)
    return capability


@dataclass(frozen=True, slots=True)
class PackedSquashFSReceipt:
    policy_id: str
    policy_fingerprint: str
    source_ordinal: int
    source_diff_id: str
    normalized_tar_digest: str
    normalized_tar_size: int
    entries: int
    packer_version: str
    packer_sha256: str
    image_digest: str
    image_size: int
    structural_verifier: str
    toolchain_fingerprint: str = ""
    toolchain_dependency_digests: tuple[str, ...] = ()


def _fixed_env() -> dict[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TZ": "UTC",
    }


def _sha256_fd(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            payload = os.pread(fd, min(_IO_CHUNK, size - offset), offset)
        except OSError:
            raise SquashFSPackError("oci-pack-output-io", "cannot read packed image") from None
        if not payload:
            raise SquashFSPackError("oci-pack-output-short", "packed image ended before its bound size")
        digest.update(payload)
        offset += len(payload)
    return f"sha256:{digest.hexdigest()}"


def _close_fd(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _verified_file_identity(fd: int) -> tuple[int, int, int, int, int]:
    try:
        current = os.fstat(fd)
    except OSError:
        raise SquashFSPackError("oci-packer-changed", "pinned packer identity is unavailable") from None
    if not stat.S_ISREG(current.st_mode) or current.st_uid != os.geteuid() or current.st_nlink != 1:
        raise SquashFSPackError("oci-packer-changed", "pinned packer identity is unsafe")
    return current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns


def _open_pinned_packer(directory_fd: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        return os.open("verified-mksquashfs", flags, dir_fd=directory_fd)
    except OSError:
        raise SquashFSPackError("oci-packer-changed", "pinned packer is unavailable") from None


def _verify_pinned_packer(packer_fd: int, directory_fd: int, expected_digest: str) -> None:
    try:
        before = _verified_file_identity(packer_fd)
        visible = os.stat("verified-mksquashfs", dir_fd=directory_fd, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != before[:2]:
            raise SquashFSPackError("oci-packer-changed", "pinned packer path binding changed")
        if _sha256_fd(packer_fd, before[2]) != f"sha256:{expected_digest}":
            raise SquashFSPackError("oci-packer-changed", "pinned packer content changed")
        if before != _verified_file_identity(packer_fd):
            raise SquashFSPackError("oci-packer-changed", "pinned packer changed during verification")
    except OSError:
        raise SquashFSPackError("oci-packer-changed", "pinned packer path binding is unavailable") from None


def _fd_path(fd: int) -> str:
    base = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    return f"{base}/{fd}"


def _pin_packer(source_path: Path, directory_fd: int) -> tuple[Path, str]:
    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise SquashFSPackError("oci-packer-path", "packer path must be absolute")
    try:
        source_path = source_path.resolve(strict=True)
    except OSError:
        raise SquashFSPackError("oci-packer-open", "cannot resolve the selected filesystem packer") from None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        source_fd = os.open(source_path, flags)
    except OSError:
        raise SquashFSPackError("oci-packer-open", "cannot open the selected filesystem packer") from None
    destination_fd = -1
    try:
        initial = os.fstat(source_fd)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size <= 0:
            raise SquashFSPackError("oci-packer-type", "filesystem packer is not a nonempty regular file")
        destination_fd = os.open(
            "verified-mksquashfs",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o500,
            dir_fd=directory_fd,
        )
        digest = hashlib.sha256()
        copied = 0
        while copied < initial.st_size:
            payload = os.read(source_fd, min(_IO_CHUNK, initial.st_size - copied))
            if not payload:
                raise SquashFSPackError("oci-packer-short", "filesystem packer changed while being pinned")
            if os.write(destination_fd, payload) != len(payload):
                raise SquashFSPackError("oci-packer-copy", "filesystem packer copy was incomplete")
            digest.update(payload)
            copied += len(payload)
        final = os.fstat(source_fd)
        if (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mtime_ns,
            initial.st_ctime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise SquashFSPackError("oci-packer-changed", "filesystem packer changed while being pinned")
        os.fchmod(destination_fd, 0o500)
        os.fsync(destination_fd)
        return Path("verified-mksquashfs"), digest.hexdigest()
    except OSError:
        raise SquashFSPackError("oci-packer-copy", "filesystem packer could not be pinned") from None
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _terminate_process(
    process: subprocess.Popen[bytes],
    grace_seconds: float,
    *,
    owns_process_group: bool,
) -> None:
    try:
        if owns_process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if owns_process_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    process.wait()


def _run_pinned(
    executable_fd: int,
    arguments: list[str],
    *,
    cwd_fd: int,
    stdin: BinaryIO | int,
    timeout_seconds: float,
    grace_seconds: float,
    capture_output: bool = False,
    execution: SquashFSPackExecution = DEFAULT_SQUASHFS_PACK_EXECUTION,
) -> tuple[int, bytes]:
    diagnostics: BinaryIO | None = None
    try:
        if capture_output:
            diagnostics = tempfile.TemporaryFile(mode="w+b")
        output_target: BinaryIO | int = diagnostics if diagnostics is not None else subprocess.DEVNULL
        try:
            process = subprocess.Popen(
                ["<verified-mksquashfs>", *arguments],
                executable=_fd_path(executable_fd),
                cwd=_fd_path(cwd_fd),
                env=_fixed_env(),
                stdin=stdin,
                stdout=output_target,
                stderr=output_target,
                close_fds=True,
                pass_fds=(executable_fd, cwd_fd),
                start_new_session=not execution.inherit_process_group,
            )
        except OSError:
            raise SquashFSPackError("oci-packer-spawn", "filesystem packer could not be started") from None
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process(
                process,
                grace_seconds,
                owns_process_group=not execution.inherit_process_group,
            )
            raise SquashFSPackError("oci-packer-timeout", "filesystem packer exceeded its deadline") from None
        except BaseException as primary:
            try:
                _terminate_process(
                    process,
                    grace_seconds,
                    owns_process_group=not execution.inherit_process_group,
                )
            except BaseException as cleanup:
                raise BaseExceptionGroup("filesystem packer interruption cleanup failed", [primary, cleanup]) from None
            raise
        if diagnostics is None:
            return return_code, b""
        diagnostics.seek(0, os.SEEK_END)
        diagnostic_size = diagnostics.tell()
        diagnostics.seek(max(0, diagnostic_size - 4096))
        diagnostic_tail = diagnostics.read(4096)
        return return_code, diagnostic_tail
    finally:
        if diagnostics is not None:
            diagnostics.close()


def _packer_version(
    executable_fd: int,
    cwd_fd: int,
    policy: SquashFSPackPolicy,
    execution: SquashFSPackExecution = DEFAULT_SQUASHFS_PACK_EXECUTION,
) -> str:
    return_code, output = _run_pinned(
        executable_fd,
        ["-version"],
        cwd_fd=cwd_fd,
        stdin=subprocess.DEVNULL,
        timeout_seconds=min(30.0, float(policy.packer_timeout_seconds)),
        grace_seconds=float(policy.terminate_grace_seconds),
        capture_output=True,
        execution=execution,
    )
    if return_code != 0:
        raise SquashFSPackError("oci-packer-version", "filesystem packer version check failed")
    match = re.search(rb"mksquashfs version (\d+)\.(\d+)(?:\.(\d+))?", output, re.I)
    if match is None:
        raise SquashFSPackError("oci-packer-version", "filesystem packer version is not identifiable")
    version = tuple(int(item or b"0") for item in match.groups())
    if version < (4, 6, 0):
        raise SquashFSPackError("oci-packer-version", "filesystem packer is older than 4.6.0")
    return ".".join(str(item) for item in version)


def _root_arguments(staged: StagedLayer, policy: SquashFSPackPolicy = DEFAULT_SQUASHFS_PACK_POLICY) -> list[str]:
    root = staged.changeset.by_path().get(".")
    if root is None or root.kind is not EntryKind.DIRECTORY:
        raise SquashFSPackError("oci-pack-root", "normalized root directory is unavailable")
    arguments = [
        "-root-mode",
        f"{root.mode & 0o7777:o}",
        "-root-uid",
        str(root.uid),
        "-root-gid",
        str(root.gid),
        "-root-time",
        str(root.mtime),
    ]
    root_xattr_bytes = 0
    for name, value in sorted(root.xattrs):
        try:
            raw_value = normalized_xattr_bytes(name, value)
        except ArtifactValidationError:
            raise SquashFSPackError("oci-pack-root", "root xattr is not byte-preserving metadata") from None
        root_xattr_bytes += len(name.encode("utf-8")) + len(raw_value)
        if root_xattr_bytes > policy.max_root_xattr_bytes:
            raise SquashFSPackError("oci-pack-root", "root xattrs exceed the packer argument policy")
        encoded = base64.b64encode(raw_value).decode("ascii")
        arguments.extend(("-p", f"/ x {name}=0s{encoded}"))
    return arguments


def verify_squashfs_fd(fd: int, image_size: int, maximum: int) -> None:
    """Verify one already-pinned SquashFS image without resolving a path.

    Derived-cache hits use the same structural gate as freshly packed output;
    callers must additionally verify the complete digest and stable file
    identity around this check.
    """
    if not SQUASHFS_BLOCK_DEVICE_ALIGNMENT <= image_size <= maximum or image_size % SQUASHFS_BLOCK_DEVICE_ALIGNMENT:
        raise SquashFSPackError("oci-pack-output-size", "packed image size is outside its policy")
    try:
        payload = os.pread(fd, _SQUASHFS_SUPERBLOCK_SIZE, 0)
    except OSError:
        raise SquashFSPackError("oci-pack-output-io", "cannot read the SquashFS superblock") from None
    if len(payload) != _SQUASHFS_SUPERBLOCK_SIZE:
        raise SquashFSPackError("oci-pack-superblock", "SquashFS superblock is truncated")
    values = struct.unpack("<5I6H8Q", payload)
    magic, inodes, mkfs_time, block_size, fragments = values[:5]
    compression, block_log, flags, id_count, major, minor = values[5:11]
    (
        root_inode,
        bytes_used,
        id_table_start,
        xattr_table_start,
        inode_table_start,
        directory_table_start,
        fragment_table_start,
        export_table_start,
    ) = values[11:]
    table_offsets = (
        id_table_start,
        xattr_table_start,
        inode_table_start,
        directory_table_start,
        fragment_table_start,
        export_table_start,
    )
    if magic != _SQUASHFS_MAGIC or inodes < 1 or mkfs_time != 0 or (major, minor) != (4, 0):
        raise SquashFSPackError("oci-pack-superblock", "SquashFS identity or deterministic time is invalid")
    if block_size < 4096 or block_size > 1024 * 1024 or block_size & (block_size - 1):
        raise SquashFSPackError("oci-pack-superblock", "SquashFS block size is invalid")
    if block_log != block_size.bit_length() - 1 or compression not in range(1, 7) or id_count < 1:
        raise SquashFSPackError("oci-pack-superblock", "SquashFS encoding metadata is invalid")
    if not _SQUASHFS_SUPERBLOCK_SIZE <= bytes_used <= image_size:
        raise SquashFSPackError("oci-pack-superblock", "SquashFS byte accounting is invalid")
    if any(offset != _UINT64_MAX and not _SQUASHFS_SUPERBLOCK_SIZE <= offset < bytes_used for offset in table_offsets):
        raise SquashFSPackError("oci-pack-superblock", "SquashFS table offset is invalid")
    required_tables = (id_table_start, inode_table_start, directory_table_start)
    if _UINT64_MAX in required_tables or len(set(required_tables)) != len(required_tables):
        raise SquashFSPackError("oci-pack-superblock", "SquashFS required tables are unavailable")
    if inode_table_start >= directory_table_start or inode_table_start + (root_inode >> 16) >= bytes_used:
        raise SquashFSPackError("oci-pack-superblock", "SquashFS root inode location is invalid")
    if (fragments == 0) != (fragment_table_start == _UINT64_MAX):
        raise SquashFSPackError("oci-pack-superblock", "SquashFS fragment table accounting is invalid")
    if bool(flags & 0x80) != (export_table_start != _UINT64_MAX):
        raise SquashFSPackError("oci-pack-superblock", "SquashFS export table accounting is invalid")
    padding_size = image_size - bytes_used
    if padding_size >= block_size:
        raise SquashFSPackError("oci-pack-superblock", "SquashFS image padding is excessive")
    if padding_size:
        try:
            padding = os.pread(fd, padding_size, bytes_used)
        except OSError:
            raise SquashFSPackError("oci-pack-output-io", "cannot read SquashFS image padding") from None
        if len(padding) != padding_size or any(padding):
            raise SquashFSPackError("oci-pack-superblock", "SquashFS image padding is invalid")


# Kept as a private compatibility alias for the existing focused fault tests.
_verify_superblock = verify_squashfs_fd


class LeasedSquashFS:
    """Single-use path-free read lease over one verified anonymous image FD."""

    __slots__ = ("_closed", "_file", "_owner_pid", "_owner_thread", "_receipt", "_started", "_verified")

    def __init__(self, image_file: BinaryIO, receipt: PackedSquashFSReceipt) -> None:
        self._file = image_file
        self._receipt = receipt
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._started = False
        self._verified = False
        self._closed = False

    @property
    def receipt(self) -> PackedSquashFSReceipt:
        return self._receipt

    def _assert_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            self._close()
            raise SquashFSPackError("oci-packed-owner", "packed image lease cannot cross a process boundary")
        if threading.get_ident() != self._owner_thread:
            raise SquashFSPackError("oci-packed-owner", "packed image lease cannot cross a thread boundary")
        if self._closed:
            raise SquashFSPackError("oci-packed-closed", "packed image lease is closed")

    def chunks(self, chunk_size: int = _IO_CHUNK) -> Iterator[bytes]:
        self._assert_owner()
        if type(chunk_size) is not int or not 1 <= chunk_size <= _IO_CHUNK:
            raise SquashFSPackError("oci-packed-chunk", "packed image chunk size is out of range")
        if self._started:
            raise SquashFSPackError("oci-packed-consumed", "packed image lease is single-use")
        self._started = True
        digest = hashlib.sha256()
        size = 0
        try:
            self._file.seek(0)
        except OSError:
            raise SquashFSPackError("oci-packed-io", "cannot rewind packed image") from None
        while True:
            try:
                payload = self._file.read(chunk_size)
            except OSError:
                raise SquashFSPackError("oci-packed-io", "cannot read packed image") from None
            if not payload:
                break
            digest.update(payload)
            size += len(payload)
            yield payload
        if size != self._receipt.image_size or f"sha256:{digest.hexdigest()}" != self._receipt.image_digest:
            raise SquashFSPackError("oci-packed-digest", "packed image changed before verified EOF")
        self._verified = True

    def _close(self) -> SquashFSPackError | None:
        if not self._closed:
            self._closed = True
            try:
                self._file.close()
            except OSError:
                return SquashFSPackError("oci-packed-cleanup", "packed image lease close failed")
        return None

    def __copy__(self) -> LeasedSquashFS:
        raise SquashFSPackError("oci-packed-copy", "packed image lease cannot be copied")

    def __deepcopy__(self, _memo: object) -> LeasedSquashFS:
        raise SquashFSPackError("oci-packed-copy", "packed image lease cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("packed image lease cannot be serialized")

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"LeasedSquashFS(receipt={self._receipt!r}, state={state!r})"


def _cleanup_private(directory: Path, directory_fd: int, names: tuple[str, ...]) -> list[BaseException]:
    errors: list[BaseException] = []
    for name in names:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            errors.append(SquashFSPackError("oci-pack-cleanup", "private artifact cleanup failed"))
    try:
        os.fsync(directory_fd)
    except OSError:
        errors.append(SquashFSPackError("oci-pack-cleanup", "private directory sync failed"))
    try:
        os.close(directory_fd)
    except OSError:
        errors.append(SquashFSPackError("oci-pack-cleanup", "private directory close failed"))
    try:
        directory.rmdir()
    except OSError:
        errors.append(SquashFSPackError("oci-pack-cleanup", "private directory removal failed"))
    return errors


@contextmanager
def pack_staged_squashfs(
    staged: StagedLayer,
    *,
    packer_path: Path,
    expected_packer_sha256: str,
    policy: SquashFSPackPolicy = DEFAULT_SQUASHFS_PACK_POLICY,
    toolchain: VerifiedSquashFSToolchain | None = None,
    execution: SquashFSPackExecution = DEFAULT_SQUASHFS_PACK_EXECUTION,
) -> Iterator[LeasedSquashFS]:
    """Emit, pack, structurally verify and lease one normalized SquashFS image."""
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise UnsupportedPlatformError("Staged SquashFS packing requires Linux FD-bound execution")
    if (
        not isinstance(staged, StagedLayer)
        or not isinstance(policy, SquashFSPackPolicy)
        or not isinstance(execution, SquashFSPackExecution)
    ):
        raise SquashFSPackError("oci-pack-input", "staged layer or pack policy is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", expected_packer_sha256 or "") is None:
        raise SquashFSPackError("oci-packer-digest", "expected packer SHA-256 is invalid")
    if toolchain is None:
        toolchain = discover_squashfs_toolchain(
            packer_path,
            expected_packer_sha256=expected_packer_sha256,
            policy=policy,
        )
    elif not isinstance(toolchain, VerifiedSquashFSToolchain):
        raise SquashFSPackError("oci-packer-toolchain", "verified toolchain capability is invalid")
    toolchain.verify(packer_path, expected_packer_sha256)

    try:
        directory = Path(
            tempfile.mkdtemp(
                prefix="palimpsest-oci-pack-",
                dir=execution.scratch_root,
            )
        )
    except OSError:
        raise SquashFSPackError("oci-pack-private", "private pack directory could not be created") from None
    directory_fd = -1
    pinned_fd = -1
    image_file: BinaryIO | None = None
    leased: LeasedSquashFS | None = None
    primary_error: BaseException | None = None
    try:
        try:
            directory.chmod(0o700)
            directory_fd = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            raise SquashFSPackError("oci-pack-private", "private pack directory is unavailable") from None
        pinned_name, pinned_digest = _pin_packer(packer_path, directory_fd)
        if pinned_digest != expected_packer_sha256:
            raise SquashFSPackError("oci-packer-digest", "pinned packer digest does not match the selected toolchain")
        pinned_fd = _open_pinned_packer(directory_fd)
        _verify_pinned_packer(pinned_fd, directory_fd, pinned_digest)
        version = _packer_version(pinned_fd, directory_fd, policy, execution)
        if toolchain.identity.version != version or toolchain.identity.executable_digest != f"sha256:{pinned_digest}":
            raise SquashFSPackError("oci-packer-toolchain", "selected packer does not match its toolchain identity")
        toolchain.verify(packer_path, expected_packer_sha256)
        _verify_pinned_packer(pinned_fd, directory_fd, pinned_digest)

        try:
            normalized_tar = tempfile.TemporaryFile(mode="w+b")
        except OSError:
            raise SquashFSPackError("oci-pack-spool", "normalized tar spool could not be created") from None
        with normalized_tar:
            spool_stat = os.fstat(normalized_tar.fileno())
            if not stat.S_ISREG(spool_stat.st_mode) or spool_stat.st_uid != os.geteuid() or spool_stat.st_nlink != 0:
                raise SquashFSPackError("oci-pack-spool", "normalized tar spool is unsafe")
            tar_receipt: TarEmissionReceipt = staged.emit_overlay_tar(
                normalized_tar,
                max_bytes=policy.max_normalized_tar_bytes,
            )
            try:
                normalized_tar.flush()
                os.fsync(normalized_tar.fileno())
                normalized_tar.seek(0)
            except OSError:
                raise SquashFSPackError("oci-pack-spool", "normalized tar spool could not be sealed") from None
            arguments = [
                "-",
                "layer.squashfs",
                "-tar",
                "-noappend",
                "-xattrs",
                "-comp",
                "zstd",
                "-Xcompression-level",
                "3",
                "-mkfs-time",
                "0",
                "-processors",
                "1",
                "-no-progress",
                *_root_arguments(staged, policy),
            ]
            return_code, _diagnostic_tail = _run_pinned(
                pinned_fd,
                arguments,
                cwd_fd=directory_fd,
                stdin=normalized_tar,
                timeout_seconds=float(policy.packer_timeout_seconds),
                grace_seconds=float(policy.terminate_grace_seconds),
                execution=execution,
            )
        if return_code != 0:
            raise SquashFSPackError("oci-packer-exit", "filesystem packer returned a nonzero status")
        _verify_pinned_packer(pinned_fd, directory_fd, pinned_digest)
        toolchain.verify(packer_path, expected_packer_sha256)

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            image_fd = os.open("layer.squashfs", flags, dir_fd=directory_fd)
        except OSError:
            raise SquashFSPackError("oci-pack-output-open", "filesystem packer output is unavailable") from None
        try:
            visible = os.stat("layer.squashfs", dir_fd=directory_fd, follow_symlinks=False)
            opened = os.fstat(image_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise SquashFSPackError("oci-pack-output-type", "filesystem packer output is unsafe")
            try:
                os.fchmod(image_fd, 0o400)
                os.unlink("layer.squashfs", dir_fd=directory_fd)
                os.fsync(directory_fd)
                sealed = os.fstat(image_fd)
            except OSError:
                raise SquashFSPackError(
                    "oci-pack-output-seal", "packed image could not be sealed anonymously"
                ) from None
            if sealed.st_nlink != 0 or stat.S_IMODE(sealed.st_mode) != 0o400:
                raise SquashFSPackError("oci-pack-output-seal", "packed image anonymous seal is invalid")
            stable_seal = (
                sealed.st_dev,
                sealed.st_ino,
                sealed.st_size,
                sealed.st_mtime_ns,
                sealed.st_ctime_ns,
                sealed.st_mode,
                sealed.st_nlink,
            )
            verify_squashfs_fd(image_fd, sealed.st_size, policy.max_image_bytes)
            if os.pread(image_fd, 2, 20) != b"\x06\x00":
                raise SquashFSPackError("oci-pack-superblock", "new packed image is not zstd-compressed")
            image_digest = _sha256_fd(image_fd, sealed.st_size)
            current = os.fstat(image_fd)
            if stable_seal != (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
                current.st_mode,
                current.st_nlink,
            ):
                raise SquashFSPackError("oci-pack-output-changed", "packed image changed during verification")
            try:
                os.unlink(pinned_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
                os.close(pinned_fd)
                pinned_fd = -1
                os.close(directory_fd)
                directory_fd = -1
                directory.rmdir()
            except OSError:
                raise SquashFSPackError("oci-pack-cleanup", "verified image could not leave private staging") from None
            image_file = os.fdopen(image_fd, "rb", closefd=True)
            receipt = PackedSquashFSReceipt(
                policy_id=SQUASHFS_PACK_POLICY_ID,
                policy_fingerprint=policy.fingerprint,
                source_ordinal=staged.receipt.ordinal,
                source_diff_id=staged.receipt.diff_id,
                normalized_tar_digest=tar_receipt.digest,
                normalized_tar_size=tar_receipt.size,
                entries=tar_receipt.entries,
                packer_version=version,
                packer_sha256=pinned_digest,
                image_digest=image_digest,
                image_size=sealed.st_size,
                structural_verifier=SQUASHFS_STRUCTURAL_VERIFIER_ID,
                toolchain_fingerprint=toolchain.identity.fingerprint,
                toolchain_dependency_digests=toolchain.identity.dependency_digests,
            )
            leased = LeasedSquashFS(image_file, receipt)
            yield leased
            if not leased._verified:
                raise SquashFSPackError("oci-packed-incomplete", "packed image lease exited before verified EOF")
        except BaseException:
            if image_file is None:
                os.close(image_fd)
            raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if leased is not None:
            close_error = leased._close()
            if close_error is not None:
                cleanup_errors.append(close_error)
        if pinned_fd >= 0:
            try:
                os.close(pinned_fd)
            except OSError:
                cleanup_errors.append(SquashFSPackError("oci-pack-cleanup", "pinned packer close failed"))
        if directory_fd >= 0:
            cleanup_errors.extend(
                _cleanup_private(
                    directory,
                    directory_fd,
                    ("layer.squashfs", "verified-mksquashfs"),
                )
            )
        elif directory.exists():
            try:
                directory.rmdir()
            except OSError:
                cleanup_errors.append(SquashFSPackError("oci-pack-cleanup", "private directory removal failed"))
        if cleanup_errors:
            failures = ([primary_error] if primary_error is not None else []) + cleanup_errors
            raise BaseExceptionGroup("staged SquashFS cleanup failed", failures)


__all__ = [
    "DEFAULT_SQUASHFS_PACK_POLICY",
    "DEFAULT_SQUASHFS_PACK_EXECUTION",
    "SQUASHFS_PACK_POLICY_ID",
    "SQUASHFS_PACKER_ARGV_CONTRACT_ID",
    "SQUASHFS_BLOCK_DEVICE_ALIGNMENT",
    "SQUASHFS_STRUCTURAL_VERIFIER_ID",
    "SQUASHFS_TOOLCHAIN_ID",
    "LeasedSquashFS",
    "PackedSquashFSReceipt",
    "SquashFSPackError",
    "SquashFSPackExecution",
    "SquashFSPackPolicy",
    "SquashFSToolchainIdentity",
    "VerifiedSquashFSToolchain",
    "discover_squashfs_toolchain",
    "pack_staged_squashfs",
    "verify_squashfs_fd",
]
