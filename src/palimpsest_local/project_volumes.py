"""Fail-closed project volume lifecycle for KVM and Lima backends.

KVM volumes are owner-only sparse raw files with an ext4 filesystem.  They are
formatted under a private temporary name and only made visible after all
verification succeeds.  Lima volumes are standalone ``limactl disk`` objects;
because Lima has no labels for application ownership, a local owner receipt is
required before an existing disk can be reused or deleted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import state
from .errors import ArtifactValidationError, LifecycleError, StateError
from .oci_guest_filesystems import (
    EXT4_FEATURE_RO_COMPAT_METADATA_CSUM,
    EXT4_SUPERBLOCK_BYTES,
    EXT4_SUPERBLOCK_OFFSET,
    verify_ext4_superblock,
)
from .state import StatePaths

_LOGICAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_LIMA_BACKEND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LIMA_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\b")
_MIB = 1024 * 1024
MIN_VOLUME_BYTES = 16 * _MIB
MAX_VOLUME_BYTES = 16 * 1024 * 1024 * 1024 * 1024
OCI_ROOT_EXT4_FEATURES = (
    "none,has_journal,ext_attr,resize_inode,dir_index,filetype,extent,64bit,flex_bg,"
    "sparse_super,large_file,huge_file,dir_nlink,extra_isize,metadata_csum"
)
OCI_ROOT_EXT4_EXTENDED_OPTIONS = "lazy_itable_init=0,lazy_journal_init=0"
_EXT_SUPERBLOCK_MAGIC_OFFSET = 1024 + 56
_EXT_SUPERBLOCK_MAGIC = b"\x53\xef"
_EXT_SUPERBLOCK_INCOMPAT_OFFSET = 1024 + 96
_EXT_SUPERBLOCK_LABEL_OFFSET = 1024 + 120
_EXT_SUPERBLOCK_LABEL_BYTES = 16
_EXT4_FEATURE_INCOMPAT_EXTENTS = 0x40
_LIMA_RECEIPT_SCHEMA_VERSION = 1
_LIMA_RECEIPT_BACKEND = "lima-disk-v2.1"
_COMMAND_TIMEOUT_SECONDS = 120.0

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class KvmVolume:
    """A verified project-owned KVM block volume."""

    project: str
    name: str
    path: Path
    size_bytes: int
    filesystem: str = "ext4"
    created: bool = False


@dataclass(frozen=True)
class LimaDiskInfo:
    """The stable fields consumed from ``limactl disk list --json``."""

    name: str
    size_bytes: int
    disk_format: str
    directory: str | None = None
    attached_instance: str | None = None


@dataclass(frozen=True)
class LimaVolume:
    """A verified project-owned Lima standalone disk."""

    project: str
    name: str
    backend_name: str
    size_bytes: int
    disk_format: str = "raw"
    created: bool = False


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise LifecycleError(f"required volume tool not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LifecycleError(f"volume command timed out: {' '.join(argv[:3])}") from exc


def _run_required(runner: CommandRunner, argv: list[str], action: str) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(argv)
    except FileNotFoundError as exc:
        raise LifecycleError(f"required volume tool not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LifecycleError(f"{action} timed out") from exc
    if not isinstance(result, subprocess.CompletedProcess):
        raise LifecycleError(f"{action} runner returned an invalid result")
    if result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        detail = stderr.strip() or stdout.strip() or f"exit status {result.returncode}"
        raise LifecycleError(f"{action} failed: {detail}")
    return result


def _validate_name(value: str, field: str) -> str:
    if not isinstance(value, str) or _LOGICAL_NAME_RE.fullmatch(value) is None:
        raise StateError(f"invalid {field}; expected ^[a-z0-9][a-z0-9_.-]{{0,62}}$")
    return value


def validate_lima_backend_name(value: str, field: str = "Lima backend name") -> str:
    """Validate an actual Lima network/disk identifier, not a logical YAML name."""

    if not isinstance(value, str) or _LIMA_BACKEND_NAME_RE.fullmatch(value) is None:
        raise StateError(f"invalid {field}; expected ^[a-z0-9][a-z0-9-]{{0,62}}$")
    return value


def _validate_size(size_bytes: int) -> int:
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise StateError("volume size_bytes must be an integer")
    if not MIN_VOLUME_BYTES <= size_bytes <= MAX_VOLUME_BYTES:
        raise StateError(f"volume size must be between {MIN_VOLUME_BYTES} and {MAX_VOLUME_BYTES} bytes")
    if size_bytes % _MIB != 0:
        raise StateError("volume size must be aligned to 1 MiB")
    return size_bytes


def _project_volume_directory(roots: StatePaths, project: str) -> Path:
    _validate_name(project, "project name")
    lexical = roots.volumes / project
    if lexical.is_symlink():
        raise StateError(f"project volume directory must not be a symlink: {lexical}")
    directory = state.project_paths(roots, project).volumes
    try:
        directory.resolve(strict=False).relative_to(roots.volumes.resolve(strict=False))
    except ValueError as exc:
        raise StateError(f"project volume directory escapes the volume root: {directory}") from exc
    if directory.exists() or directory.is_symlink():
        _verify_private_directory(directory, "project volume directory")
    return directory


def _verify_private_directory(path: Path, context: str) -> None:
    if path.is_symlink():
        raise StateError(f"{context} must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise StateError(f"cannot stat {context}: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise StateError(f"{context} is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise StateError(f"{context} is not owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise StateError(f"{context} permissions must be 0700: {path}")


def _volume_lock_path(roots: StatePaths, project: str, name: str, backend: str) -> Path:
    _validate_name(name, "volume name")
    return roots.locks / f"project-volume-{backend}-{project}-{name}.lock"


def kvm_volume_path(roots: StatePaths, project: str, name: str) -> Path:
    """Return the only accepted host path for one managed KVM volume."""
    _validate_name(name, "volume name")
    directory = _project_volume_directory(roots, project)
    path = directory / f"{name}.raw"
    if path.is_symlink():
        raise StateError(f"KVM volume path must not be a symlink: {path}")
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except ValueError as exc:
        raise StateError(f"KVM volume path escapes its project directory: {path}") from exc
    return path


def _qemu_info(path: Path, runner: CommandRunner) -> dict[str, Any]:
    result = _run_required(
        runner,
        ["qemu-img", "info", "--output=json", str(path)],
        f"inspect KVM volume {path.name}",
    )
    if not isinstance(result.stdout, str):
        raise LifecycleError("qemu-img info did not return text output")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"qemu-img returned invalid JSON for volume {path.name}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"qemu-img returned a non-object for volume {path.name}")
    return value


def _has_ext4_superblock(path: Path, *, offset: int = 0, expected_label: str | None = None) -> bool:
    try:
        with path.open("rb") as stream:
            stream.seek(offset + _EXT_SUPERBLOCK_MAGIC_OFFSET)
            if stream.read(len(_EXT_SUPERBLOCK_MAGIC)) != _EXT_SUPERBLOCK_MAGIC:
                return False
            stream.seek(offset + _EXT_SUPERBLOCK_INCOMPAT_OFFSET)
            incompat = int.from_bytes(stream.read(4), byteorder="little")
            if not incompat & _EXT4_FEATURE_INCOMPAT_EXTENTS:
                return False
            if expected_label is not None:
                encoded = expected_label.encode("ascii")
                if len(encoded) > _EXT_SUPERBLOCK_LABEL_BYTES:
                    return False
                stream.seek(offset + _EXT_SUPERBLOCK_LABEL_OFFSET)
                label = stream.read(_EXT_SUPERBLOCK_LABEL_BYTES).rstrip(b"\0")
                if label != encoded:
                    return False
            return True
    except OSError as exc:
        raise LifecycleError(f"cannot inspect ext4 superblock for {path}") from exc


def kvm_volume_label(project: str, name: str) -> str:
    """Return the exact ext4 label proving a KVM volume's project identity."""

    _validate_name(project, "project name")
    _validate_name(name, "volume name")
    return "pali-" + hashlib.sha256(f"palimpsest-kvm-volume-v1\0{project}\0{name}".encode()).hexdigest()[:8]


def _verify_kvm_path(
    path: Path,
    size_bytes: int,
    expected_label: str,
    runner: CommandRunner,
    *,
    access_validator: Callable[[], None] | None = None,
) -> None:
    if access_validator is not None:
        access_validator()
    if path.is_symlink():
        raise StateError(f"KVM volume path must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise StateError(f"KVM volume does not exist: {path}") from exc
    except OSError as exc:
        raise StateError(f"cannot stat KVM volume: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise StateError(f"KVM volume is not a regular file: {path}")
    if metadata.st_uid != os.getuid():
        raise StateError(f"KVM volume is not owned by the current user: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if access_validator is None and (mode & 0o077 or mode & 0o600 != 0o600):
        raise StateError(f"KVM volume permissions must be owner-only and writable: {path}")
    if metadata.st_nlink != 1:
        raise StateError(f"KVM volume must not be hard-linked: {path}")
    if metadata.st_size != size_bytes:
        raise StateError(f"KVM volume size conflict for {path.name}: expected {size_bytes}, found {metadata.st_size}")
    info = _qemu_info(path, runner)
    if info.get("format") != "raw":
        raise StateError(f"KVM volume format conflict for {path.name}: expected raw")
    if info.get("virtual-size") != size_bytes:
        raise StateError(f"KVM volume virtual size conflict for {path.name}")
    if info.get("backing-filename") is not None or info.get("full-backing-filename") is not None:
        raise StateError(f"KVM raw volume unexpectedly has a backing file: {path.name}")
    if not _has_ext4_superblock(path, expected_label=expected_label):
        raise StateError(f"KVM volume is not ext4 with expected label {expected_label!r}: {path.name}")
    if access_validator is not None:
        access_validator()


def verify_kvm_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    runner: CommandRunner = _default_runner,
) -> KvmVolume:
    """Verify, without modifying, an existing project KVM volume."""
    size_bytes = _validate_size(size_bytes)
    path = kvm_volume_path(roots, project, name)
    _verify_kvm_path(path, size_bytes, kvm_volume_label(project, name), runner)
    return KvmVolume(project=project, name=name, path=path, size_bytes=size_bytes)


def _preflight_kvm_tools(runner: CommandRunner) -> None:
    _run_required(runner, ["qemu-img", "--version"], "preflight qemu-img")
    _run_required(runner, ["mkfs.ext4", "-V"], "preflight mkfs.ext4")


def preflight_kvm_volume_support(*, runner: CommandRunner = _default_runner) -> None:
    """Read-only check that the tools required to create KVM volumes exist."""

    _preflight_kvm_tools(runner)


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise StateError(f"cannot durably sync volume file: {path}") from exc


def _verify_new_oci_root_ext4(path: Path, size_bytes: int, filesystem_uuid: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        visible = path.stat(follow_symlinks=False)
        superblock = os.pread(descriptor, EXT4_SUPERBLOCK_BYTES, EXT4_SUPERBLOCK_OFFSET)
        identity = verify_ext4_superblock(
            superblock,
            device_size=size_bytes,
            volume_id=filesystem_uuid,
            filesystem_uuid=filesystem_uuid,
        )
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        metadata = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != size_bytes
            or not identity.feature_ro_compat & EXT4_FEATURE_RO_COMPAT_METADATA_CSUM
            or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino)
            or (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != metadata
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise StateError("created OCI-root ext4 volume changed during verification")
    except ArtifactValidationError:
        raise StateError("created OCI-root ext4 volume violates the pinned policy") from None
    except OSError:
        raise StateError("created OCI-root ext4 volume cannot be pinned") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _ensure_ext4_raw_file_locked(
    path: Path,
    size_bytes: int,
    label: str,
    logical_name: str,
    runner: CommandRunner,
    creation_temp_path: Path | None = None,
    filesystem_uuid: str | None = None,
    parent_validator: Callable[[], None] | None = None,
) -> bool:
    """Create or verify one locked raw ext4 artifact at an owner-bound path."""
    if parent_validator is not None:
        parent_validator()
    if creation_temp_path is not None:
        if creation_temp_path.parent != path.parent or creation_temp_path == path:
            raise StateError("KVM volume creation temporary path is invalid")
        temporary_exists = creation_temp_path.exists() or creation_temp_path.is_symlink()
        if temporary_exists and (path.exists() or path.is_symlink()):
            temporary_entry = creation_temp_path.stat(follow_symlinks=False)
            published_entry = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(temporary_entry.st_mode)
                or (temporary_entry.st_dev, temporary_entry.st_ino) != (published_entry.st_dev, published_entry.st_ino)
                or temporary_entry.st_nlink != 2
                or published_entry.st_nlink != 2
            ):
                raise StateError("KVM volume creation publication is inconsistent")
            creation_temp_path.unlink()
            state.fsync_directory(path.parent)
            _verify_kvm_path(path, size_bytes, label, runner)
            if filesystem_uuid is not None:
                _verify_new_oci_root_ext4(path, size_bytes, filesystem_uuid)
            if parent_validator is not None:
                parent_validator()
            return True
        if temporary_exists:
            temporary_entry = creation_temp_path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(temporary_entry.st_mode)
                or temporary_entry.st_uid != os.geteuid()
                or temporary_entry.st_nlink != 1
            ):
                raise StateError("KVM volume creation temporary is unsafe")
            creation_temp_path.unlink()
            state.fsync_directory(path.parent)
    if path.exists() or path.is_symlink():
        _verify_kvm_path(path, size_bytes, label, runner)
        if filesystem_uuid is not None:
            _verify_new_oci_root_ext4(path, size_bytes, filesystem_uuid)
        if parent_validator is not None:
            parent_validator()
        return False

    _preflight_kvm_tools(runner)
    if parent_validator is None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
    else:
        parent_validator()
    if creation_temp_path is None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{logical_name}-",
            suffix=".raw.tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
    else:
        temporary = creation_temp_path
        try:
            descriptor = os.open(
                temporary,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            raise StateError(f"cannot create KVM volume temporary: {temporary}") from None
    published_identity: tuple[int, int] | None = None
    try:
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, size_bytes)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        mkfs_command = ["mkfs.ext4", "-F", "-q", "-L", label]
        if filesystem_uuid is not None:
            try:
                canonical_uuid = str(uuid.UUID(filesystem_uuid))
            except (AttributeError, TypeError, ValueError):
                raise StateError("KVM volume filesystem UUID is invalid") from None
            if canonical_uuid != filesystem_uuid:
                raise StateError("KVM volume filesystem UUID is not canonical")
            mkfs_command.extend(
                (
                    "-U",
                    canonical_uuid,
                    "-b",
                    "4096",
                    "-I",
                    "256",
                    "-g",
                    "32768",
                    "-i",
                    "16384",
                    "-m",
                    "0",
                    "-O",
                    OCI_ROOT_EXT4_FEATURES,
                    "-E",
                    OCI_ROOT_EXT4_EXTENDED_OPTIONS,
                )
            )
        mkfs_command.append(str(temporary))
        _run_required(
            runner,
            mkfs_command,
            f"format KVM volume {logical_name}",
        )
        _fsync_file(temporary)
        _verify_kvm_path(temporary, size_bytes, label, runner)
        if filesystem_uuid is not None:
            _verify_new_oci_root_ext4(temporary, size_bytes, filesystem_uuid)
        if parent_validator is not None:
            parent_validator()

        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise StateError(f"KVM volume appeared concurrently and was not overwritten: {path}") from exc
        published = path.stat()
        published_identity = (published.st_dev, published.st_ino)
        state.fsync_directory(path.parent)
        temporary.unlink()
        state.fsync_directory(path.parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if published_identity is not None:
            try:
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == published_identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        temporary.unlink(missing_ok=True)
        raise
    if parent_validator is not None:
        parent_validator()
    return True


def _delete_ext4_raw_file_locked(
    path: Path,
    size_bytes: int,
    label: str,
    logical_name: str,
    runner: CommandRunner,
    quarantine_validator: Callable[[Path, Path], None] | None = None,
    quarantine_path: Path | None = None,
    access_validator: Callable[[Path], None] | None = None,
) -> bool:
    """Verify and quarantine-delete one locked raw ext4 artifact."""
    quarantine = quarantine_path
    if quarantine is not None:
        if quarantine.parent != path.parent or quarantine == path:
            raise StateError("KVM volume quarantine path is invalid")
        if quarantine.exists() or quarantine.is_symlink():
            if path.exists() or path.is_symlink():
                raise StateError(f"KVM volume and quarantine both exist: {path}")
            if access_validator is not None:
                access_validator(quarantine)
            _verify_kvm_path(quarantine, size_bytes, label, runner)
            if access_validator is not None:
                access_validator(quarantine)
            quarantine.unlink()
            state.fsync_directory(path.parent)
            return True
    if not path.exists() and not path.is_symlink():
        return False
    if access_validator is not None:
        access_validator(path)
    _verify_kvm_path(path, size_bytes, label, runner)
    if access_validator is not None:
        access_validator(path)
    quarantine = quarantine or path.with_name(f".{logical_name}-delete-{uuid.uuid4().hex}.raw")
    try:
        os.replace(path, quarantine)
        state.fsync_directory(path.parent)
        if quarantine_validator is not None:
            quarantine_validator(path, quarantine)
        if path.exists() or path.is_symlink():
            raise StateError(f"KVM volume path was recreated during deletion: {path}")
        _verify_kvm_path(quarantine, size_bytes, label, runner)
        if access_validator is not None:
            access_validator(quarantine)
        quarantine.unlink()
        state.fsync_directory(path.parent)
        return True
    except Exception as exc:
        if quarantine.exists() or quarantine.is_symlink():
            if not path.exists() and not path.is_symlink():
                try:
                    os.replace(quarantine, path)
                    state.fsync_directory(path.parent)
                except OSError as restore_exc:
                    raise StateError(
                        f"KVM volume deletion failed and quarantine could not be restored: {quarantine}"
                    ) from restore_exc
            else:
                raise StateError(
                    f"KVM volume deletion failed after a concurrent path replacement; "
                    f"original data remains quarantined at {quarantine}"
                ) from exc
        raise


def ensure_kvm_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    runner: CommandRunner = _default_runner,
) -> KvmVolume:
    """Create a sparse raw ext4 KVM volume, or verify an exact existing one.

    An existing target is never passed to ``mkfs.ext4`` and is never replaced.
    """
    size_bytes = _validate_size(size_bytes)
    path = kvm_volume_path(roots, project, name)
    lock_path = _volume_lock_path(roots, project, name, "kvm")
    with state.file_lock(lock_path):
        created = _ensure_ext4_raw_file_locked(
            path,
            size_bytes,
            kvm_volume_label(project, name),
            name,
            runner,
        )
        return KvmVolume(
            project=project,
            name=name,
            path=path,
            size_bytes=size_bytes,
            created=created,
        )


def delete_kvm_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    runner: CommandRunner = _default_runner,
    quarantine_validator: Callable[[Path, Path], None] | None = None,
) -> bool:
    """Delete only an exact, contained raw ext4 KVM volume.

    The project directory is current-user-owned mode 0700, which defines the
    local threat boundary.  Within that boundary the file is first atomically
    renamed to a random quarantine name.  An adapter may then rescan backend
    references to both the old and quarantine paths before the final unlink.
    """
    size_bytes = _validate_size(size_bytes)
    path = kvm_volume_path(roots, project, name)
    lock_path = _volume_lock_path(roots, project, name, "kvm")
    with state.file_lock(lock_path):
        return _delete_ext4_raw_file_locked(
            path,
            size_bytes,
            kvm_volume_label(project, name),
            name,
            runner,
            quarantine_validator,
        )


def lima_backend_name(project: str, name: str) -> str:
    """Return a deterministic Lima name that fits ``lima-NAME`` in an ext4 label."""
    _validate_name(project, "project name")
    _validate_name(name, "volume name")
    digest = hashlib.sha256(f"palimpsest-lima-volume-v2\0{project}\0{name}".encode()).digest()
    backend_name = base64.b32encode(digest).decode("ascii").lower()[:11]
    if _LIMA_BACKEND_NAME_RE.fullmatch(backend_name) is None:  # defensive invariant
        raise StateError("generated Lima volume name is invalid")
    return backend_name


def _lima_owner_path(roots: StatePaths, project: str, name: str) -> Path:
    _validate_name(name, "volume name")
    return _project_volume_directory(roots, project) / f"{name}.lima-owner.json"


def _parse_lima_disk_object(value: object) -> LimaDiskInfo:
    if not isinstance(value, dict):
        raise LifecycleError("Lima disk list contains a non-object entry")
    name = value.get("name")
    size_bytes = value.get("size")
    disk_format = value.get("format")
    if not isinstance(name, str) or not name:
        raise LifecycleError("Lima disk list entry has an invalid name")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise LifecycleError(f"Lima disk list entry {name!r} has an invalid size")
    if not isinstance(disk_format, str) or disk_format not in {"raw", "qcow2"}:
        raise LifecycleError(f"Lima disk list entry {name!r} has an invalid format")
    directory = value.get("dir")
    attached_instance = value.get("instance")
    if directory is not None and not isinstance(directory, str):
        raise LifecycleError(f"Lima disk list entry {name!r} has an invalid directory")
    if attached_instance is not None and not isinstance(attached_instance, str):
        raise LifecycleError(f"Lima disk list entry {name!r} has an invalid instance")
    return LimaDiskInfo(
        name=name,
        size_bytes=size_bytes,
        disk_format=disk_format,
        directory=directory,
        attached_instance=attached_instance or None,
    )


def _decode_lima_disk_list(output: str) -> list[LimaDiskInfo]:
    raw = output.strip()
    if not raw:
        return []
    values: list[object]
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        values = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise LifecycleError("Lima disk list returned invalid JSON lines") from exc
    else:
        values = decoded if isinstance(decoded, list) else [decoded]
    disks = [_parse_lima_disk_object(item) for item in values]
    names = [disk.name for disk in disks]
    if len(set(names)) != len(names):
        raise LifecycleError("Lima disk list returned duplicate disk names")
    return disks


def list_lima_disks(*, runner: CommandRunner = _default_runner) -> tuple[LimaDiskInfo, ...]:
    """List Lima 2.1 standalone disks using its machine-readable JSON-lines output."""
    result = _run_required(runner, ["limactl", "disk", "list", "--json"], "list Lima disks")
    if not isinstance(result.stdout, str):
        raise LifecycleError("Lima disk list did not return text output")
    return tuple(_decode_lima_disk_list(result.stdout))


def _lima_disk_map(runner: CommandRunner) -> dict[str, LimaDiskInfo]:
    return {disk.name: disk for disk in list_lima_disks(runner=runner)}


def _preflight_lima_21(runner: CommandRunner) -> None:
    result = _run_required(runner, ["limactl", "--version"], "preflight Lima")
    output = "\n".join(value for value in (result.stdout, result.stderr) if isinstance(value, str) and value)
    match = _LIMA_VERSION_RE.search(output)
    if match is None:
        raise LifecycleError("cannot determine Lima version; version 2.1 or newer in the 2.x series is required")
    version = tuple(int(part) for part in match.groups())
    if version < (2, 1, 0) or version >= (3, 0, 0):
        raise LifecycleError(
            f"unsupported Lima version {'.'.join(match.groups())}; version 2.1 or newer in the 2.x series is required"
        )


def _lima_receipt(project: str, name: str, backend_name: str, size_bytes: int) -> dict[str, Any]:
    return {
        "schema_version": _LIMA_RECEIPT_SCHEMA_VERSION,
        "backend": _LIMA_RECEIPT_BACKEND,
        "project": project,
        "name": name,
        "backend_name": backend_name,
        "size_bytes": size_bytes,
        "disk_format": "raw",
    }


def _read_lima_receipt(path: Path, expected: dict[str, Any]) -> bool:
    if not path.exists():
        if path.is_symlink():
            raise StateError(f"Lima volume owner receipt must not be a symlink: {path}")
        return False
    if path.is_symlink():
        raise StateError(f"Lima volume owner receipt must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise StateError(f"cannot stat Lima volume owner receipt: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise StateError(f"Lima volume owner receipt is not a regular file: {path}")
    if metadata.st_uid != os.getuid():
        raise StateError(f"Lima volume owner receipt is not owned by the current user: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StateError(f"Lima volume owner receipt permissions must be 0600: {path}")
    _verify_private_directory(path.parent, "project volume directory")
    try:
        actual = state.read_json(path)
    except StateError as exc:
        raise StateError(f"invalid Lima volume owner receipt: {path}") from exc
    if actual != expected:
        raise StateError(f"Lima volume owner receipt conflicts with requested volume: {path.name}")
    return True


def _write_lima_receipt(
    path: Path,
    project: str,
    name: str,
    backend_name: str,
    size_bytes: int,
) -> None:
    expected = _lima_receipt(project, name, backend_name, size_bytes)
    state.atomic_write_json(path, expected)
    _verify_private_directory(path.parent, "project volume directory")
    # atomic_write_json promises 0600; verify it here because this receipt is
    # the authority used for future attach/delete operations.
    _read_lima_receipt(path, expected)


def _verify_lima_disk(disk: LimaDiskInfo, expected_name: str, size_bytes: int) -> None:
    if disk.name != expected_name:
        raise StateError("Lima disk identity mismatch")
    if disk.size_bytes != size_bytes:
        raise StateError(
            f"Lima volume size conflict for {expected_name}: expected {size_bytes}, found {disk.size_bytes}"
        )
    if disk.disk_format != "raw":
        raise StateError(f"Lima volume format conflict for {expected_name}: expected raw")


def preflight_lima_volume_support(*, runner: CommandRunner = _default_runner) -> None:
    """Read-only check for the supported Lima standalone-disk contract."""

    _preflight_lima_21(runner)
    list_lima_disks(runner=runner)


def _allowed_attachment(
    disk: LimaDiskInfo,
    allowed_instance: str | Collection[str] | None,
) -> None:
    if isinstance(allowed_instance, str):
        allowed_instances = {allowed_instance}
    else:
        allowed_instances = set(allowed_instance or ())
    if disk.attached_instance and disk.attached_instance not in allowed_instances:
        raise StateError(f"Lima disk {disk.name!r} is attached to foreign instance {disk.attached_instance!r}")


def verify_lima_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    allow_missing: bool = False,
    allowed_instance: str | Collection[str] | None = None,
    runner: CommandRunner = _default_runner,
) -> LimaVolume | None:
    """Read-only verification of a managed Lima disk and exact owner receipt."""

    size_bytes = _validate_size(size_bytes)
    backend_name = lima_backend_name(project, name)
    owner_path = _lima_owner_path(roots, project, name)
    expected_receipt = _lima_receipt(project, name, backend_name, size_bytes)
    _preflight_lima_21(runner)
    with state.file_lock(_volume_lock_path(roots, project, name, "lima")):
        disk = _lima_disk_map(runner).get(backend_name)
        owned = _read_lima_receipt(owner_path, expected_receipt)
        if disk is None and not owned and allow_missing:
            return None
        if disk is not None and not owned:
            raise StateError(
                f"Lima disk {backend_name!r} already exists without Palimpsest ownership; refusing to adopt it"
            )
        if disk is None and owned:
            raise StateError(
                f"Lima ownership receipt exists but disk {backend_name!r} is missing; refusing automatic recreation"
            )
        if disk is None:
            raise StateError(f"managed Lima disk does not exist: {backend_name}")
        _verify_lima_disk(disk, backend_name, size_bytes)
        _allowed_attachment(disk, allowed_instance)
        return LimaVolume(project, name, backend_name, size_bytes)


def preflight_delete_lima_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    allowed_instances: Collection[str] = (),
    runner: CommandRunner = _default_runner,
) -> None:
    """Read-only proof that a later named-disk deletion is ownership-safe."""

    size_bytes = _validate_size(size_bytes)
    backend_name = lima_backend_name(project, name)
    owner_path = _lima_owner_path(roots, project, name)
    expected_receipt = _lima_receipt(project, name, backend_name, size_bytes)
    _preflight_lima_21(runner)
    with state.file_lock(_volume_lock_path(roots, project, name, "lima")):
        disk = _lima_disk_map(runner).get(backend_name)
        owned = _read_lima_receipt(owner_path, expected_receipt)
        if disk is None:
            return
        if not owned:
            raise StateError(f"Lima disk {backend_name!r} exists without Palimpsest ownership; refusing to delete it")
        _verify_lima_disk(disk, backend_name, size_bytes)
        _allowed_attachment(disk, allowed_instances)


def ensure_lima_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    runner: CommandRunner = _default_runner,
) -> LimaVolume:
    """Create or verify a project-owned Lima 2.1 standalone raw disk.

    A deterministic-name disk without the exact local owner receipt is treated
    as external and is never adopted.
    """
    size_bytes = _validate_size(size_bytes)
    backend_name = lima_backend_name(project, name)
    owner_path = _lima_owner_path(roots, project, name)
    expected_receipt = _lima_receipt(project, name, backend_name, size_bytes)
    lock_path = _volume_lock_path(roots, project, name, "lima")
    with state.file_lock(lock_path):
        disks = _lima_disk_map(runner)
        disk = disks.get(backend_name)
        owned = _read_lima_receipt(owner_path, expected_receipt)
        if disk is not None and not owned:
            raise StateError(
                f"Lima disk {backend_name!r} already exists without Palimpsest ownership; refusing to adopt it"
            )
        if disk is None and owned:
            raise StateError(
                f"Lima ownership receipt exists but disk {backend_name!r} is missing; refusing automatic recreation"
            )
        if disk is not None:
            _verify_lima_disk(disk, backend_name, size_bytes)
            return LimaVolume(project, name, backend_name, size_bytes)

        _preflight_lima_21(runner)
        size = f"{size_bytes // _MIB}MiB"
        _run_required(
            runner,
            [
                "limactl",
                "disk",
                "create",
                backend_name,
                "--size",
                size,
                "--format",
                "raw",
                "--tty=false",
            ],
            f"create Lima volume {backend_name}",
        )
        try:
            created_disk = _lima_disk_map(runner).get(backend_name)
            if created_disk is None:
                raise LifecycleError(f"Lima did not report newly created disk {backend_name!r}")
            _verify_lima_disk(created_disk, backend_name, size_bytes)
            _write_lima_receipt(owner_path, project, name, backend_name, size_bytes)
        except Exception as exc:
            raise LifecycleError(
                f"Lima created {backend_name!r} but ownership verification was not committed; "
                "the disk was retained for manual recovery"
            ) from exc
        return LimaVolume(project, name, backend_name, size_bytes, created=True)


def delete_lima_volume(
    roots: StatePaths,
    project: str,
    name: str,
    size_bytes: int,
    *,
    runner: CommandRunner = _default_runner,
) -> bool:
    """Delete a Lima disk only when its exact owner receipt and disk metadata match."""
    size_bytes = _validate_size(size_bytes)
    backend_name = lima_backend_name(project, name)
    owner_path = _lima_owner_path(roots, project, name)
    expected_receipt = _lima_receipt(project, name, backend_name, size_bytes)
    lock_path = _volume_lock_path(roots, project, name, "lima")
    with state.file_lock(lock_path):
        disks = _lima_disk_map(runner)
        disk = disks.get(backend_name)
        owned = _read_lima_receipt(owner_path, expected_receipt)
        if not owned:
            if disk is not None:
                raise StateError(
                    f"Lima disk {backend_name!r} exists without Palimpsest ownership; refusing to delete it"
                )
            return False
        if disk is None:
            owner_path.unlink()
            state.fsync_directory(owner_path.parent)
            return False
        _verify_lima_disk(disk, backend_name, size_bytes)
        _allowed_attachment(disk, None)
        _preflight_lima_21(runner)
        _run_required(
            runner,
            ["limactl", "disk", "delete", backend_name, "--tty=false"],
            f"delete Lima volume {backend_name}",
        )
        # A successful delete consumes this ownership proof.  Remove it before
        # the postcondition query so a same-name disk recreated by another
        # actor can never inherit our authority to delete it.
        owner_path.unlink()
        state.fsync_directory(owner_path.parent)
        if backend_name in _lima_disk_map(runner):
            raise LifecycleError(
                f"Lima still reports disk {backend_name!r} after deletion; it is now treated as external"
            )
        return True
