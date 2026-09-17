"""Anonymous HTTPS registry acquisition into a verified local OCI archive."""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import PalimpsestError
from .oci_image import OCIImageRef
from .oci_source import LocalArchiveSource, SourceCAS
from .registry import default_registry_config, resolve_image_reference
from .state import StatePaths

SUPPORTED_PLATFORM = "linux/amd64"
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_POLL_SECONDS = 0.05


class RegistryIntakeError(PalimpsestError):
    """An anonymous TLS registry reference could not become a verified local archive."""


@dataclass(frozen=True, slots=True)
class RegistryPullReceipt:
    reference: str
    output: Path
    platform: str
    root_digest: str
    manifest_digest: str
    source_cas_id: str
    source_binding_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_digest": self.manifest_digest,
            "output": os.fspath(self.output),
            "platform": self.platform,
            "reference": self.reference,
            "root_digest": self.root_digest,
            "source_binding_digest": self.source_binding_digest,
            "source_cas_id": self.source_cas_id,
        }


def _has_explicit_registry(reference: str) -> bool:
    name = reference.partition("@")[0]
    if "/" not in name:
        return False
    authority = name.split("/", 1)[0]
    return "." in authority or ":" in authority or authority.lower() == "localhost" or authority.startswith("[")


def resolve_anonymous_reference(reference: str):
    """Resolve only explicit, scheme-free registry references."""
    if not isinstance(reference, str) or not _has_explicit_registry(reference):
        raise RegistryIntakeError("OCI registry pull requires a fully qualified registry/repository reference")
    try:
        return resolve_image_reference(reference, default_registry_config())
    except PalimpsestError as exc:
        raise RegistryIntakeError(str(exc)) from None


def skopeo_copy_argv(executable: Path, reference: str, destination: Path) -> tuple[str, ...]:
    return (
        os.fspath(executable),
        "--override-os",
        "linux",
        "--override-arch",
        "amd64",
        "copy",
        "--multi-arch=system",
        "--preserve-digests",
        "--src-no-creds",
        "--src-tls-verify=true",
        f"docker://{reference}",
        f"oci-archive:{destination}",
    )


def _safe_output_parent(output: Path) -> tuple[Path, os.stat_result]:
    target = output.expanduser().absolute()
    if not target.name or any(component in {"", ".", ".."} for component in target.parts[1:]):
        raise RegistryIntakeError("OCI archive output path must be absolute and canonical")
    try:
        if os.path.lexists(target):
            raise RegistryIntakeError("OCI archive output already exists")
        parent = target.parent.resolve(strict=True)
        visible = parent.stat(follow_symlinks=False)
    except RegistryIntakeError:
        raise
    except OSError:
        raise RegistryIntakeError("OCI archive output parent is unavailable") from None
    if not stat.S_ISDIR(visible.st_mode) or visible.st_uid != os.geteuid() or visible.st_mode & 0o022:
        raise RegistryIntakeError("OCI archive output parent must be owner-bound and not group/world writable")
    if target.parent != parent:
        raise RegistryIntakeError("OCI archive output parent must not traverse symbolic links")
    return target, visible


def _remove_owned_tree(parent_fd: int, name: str) -> None:
    """Remove one generated directory through its held parent authority."""
    try:
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        for entry in os.scandir(directory_fd):
            try:
                if entry.is_dir(follow_symlinks=False):
                    _remove_owned_tree(directory_fd, entry.name)
                else:
                    os.unlink(entry.name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
    finally:
        os.close(directory_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _isolated_environment(private_home: Path) -> dict[str, str]:
    private_directories = {
        "TMPDIR": private_home / "tmp",
        "XDG_CACHE_HOME": private_home / "cache",
        "XDG_CONFIG_HOME": private_home / "config",
        "XDG_RUNTIME_DIR": private_home / "runtime",
    }
    for directory in private_directories.values():
        directory.mkdir(mode=0o700)
    environment = {
        "HOME": os.fspath(private_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **{name: os.fspath(path) for name, path in private_directories.items()},
    }
    for key in ("LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _staged_bytes(staging: Path) -> int:
    total = 0
    try:
        for root, directories, files in os.walk(staging, followlinks=False):
            for name in directories:
                try:
                    metadata = os.lstat(Path(root) / name)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise RegistryIntakeError("registry client created an unsafe staging entry")
            for name in files:
                try:
                    metadata = os.lstat(Path(root) / name)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise RegistryIntakeError("registry client created an unsafe staging entry")
                total += metadata.st_size
    except RegistryIntakeError:
        raise
    except OSError:
        raise RegistryIntakeError("cannot inspect registry pull staging") from None
    return total


def _run_bounded_copy(
    argv: tuple[str, ...], archive: Path, private_home: Path, *, timeout_seconds: float, maximum_bytes: int
) -> None:
    if not 0 < timeout_seconds <= 3600:
        raise RegistryIntakeError("registry pull timeout must be between 0 and 3600 seconds")
    if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= MAX_ARCHIVE_BYTES:
        raise RegistryIntakeError("registry pull archive limit is invalid")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_isolated_environment(private_home),
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise RegistryIntakeError("cannot execute the required skopeo registry client") from None
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None:
            if _staged_bytes(private_home) > maximum_bytes:
                raise RegistryIntakeError("registry response exceeds the OCI archive size limit")
            if time.monotonic() >= deadline:
                raise RegistryIntakeError("registry pull timed out")
            time.sleep(_POLL_SECONDS)
        if process.returncode != 0:
            raise RegistryIntakeError("skopeo could not copy the anonymous registry image")
        if _staged_bytes(private_home) > maximum_bytes:
            raise RegistryIntakeError("registry response exceeds the OCI archive size limit")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def pull_anonymous_oci_archive(
    reference: str,
    output: Path,
    roots: StatePaths,
    *,
    platform: str = SUPPORTED_PLATFORM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    maximum_bytes: int = MAX_ARCHIVE_BYTES,
    skopeo: Path | None = None,
) -> RegistryPullReceipt:
    """Acquire anonymously, verify through SourceCAS, then publish without overwrite."""
    if platform != SUPPORTED_PLATFORM:
        raise RegistryIntakeError(f"registry pull supports only {SUPPORTED_PLATFORM}")
    resolved = resolve_anonymous_reference(reference)
    executable_text = shutil.which("skopeo") if skopeo is None else os.fspath(skopeo)
    if executable_text is None:
        raise RegistryIntakeError("skopeo is required for OCI registry pull")
    try:
        executable = Path(executable_text).expanduser().resolve(strict=True)
    except OSError:
        raise RegistryIntakeError("skopeo executable is unavailable") from None
    if not executable.is_file():
        raise RegistryIntakeError("skopeo executable is not a regular file")
    target, parent_before = _safe_output_parent(output)
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    staging: Path | None = None
    try:
        held_parent = os.fstat(parent_fd)
        if (held_parent.st_dev, held_parent.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            raise RegistryIntakeError("OCI archive output parent changed before registry pull")
        staging = Path(tempfile.mkdtemp(prefix=".palimpsest-oci-pull-", dir=target.parent))
        os.chmod(staging, 0o700)
        staged_visible = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if (staged_visible.st_dev, staged_visible.st_ino) != (staging.stat().st_dev, staging.stat().st_ino):
            raise RegistryIntakeError("OCI archive staging parent changed during creation")
        archive = staging / "image.oci.tar"
        _run_bounded_copy(
            skopeo_copy_argv(executable, resolved.canonical, archive),
            archive,
            staging,
            timeout_seconds=timeout_seconds,
            maximum_bytes=maximum_bytes,
        )
        os.chmod(archive, 0o600, follow_symlinks=False)
        source_cas = SourceCAS(roots.oci_source_cas)
        image_ref = OCIImageRef(resolved.endpoint, resolved.repository, resolved.canonical)
        snapshot = LocalArchiveSource(archive).snapshot(image_ref, source_cas)
        parent_after = target.parent.stat(follow_symlinks=False)
        if (parent_after.st_dev, parent_after.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            raise RegistryIntakeError("OCI archive output parent changed during registry pull")
        staging_fd = os.open(staging.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            os.link(archive.name, target.name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            os.chmod(target.name, 0o600, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(staging_fd)
        os.fsync(parent_fd)
        published_parent = target.parent.stat(follow_symlinks=False)
        if (published_parent.st_dev, published_parent.st_ino) != (parent_before.st_dev, parent_before.st_ino):
            os.unlink(target.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            raise RegistryIntakeError("OCI archive output parent changed during publication")
        return RegistryPullReceipt(
            reference=resolved.canonical,
            output=target,
            platform=platform,
            root_digest=snapshot.root.descriptor.digest,
            manifest_digest=snapshot.manifest.descriptor.digest,
            source_cas_id=snapshot.cas_id,
            source_binding_digest=snapshot.binding_digest,
        )
    except FileExistsError:
        raise RegistryIntakeError("OCI archive output already exists") from None
    except OSError:
        raise RegistryIntakeError("registry pull filesystem boundary failed") from None
    finally:
        if staging is not None:
            _remove_owned_tree(parent_fd, staging.name)
        os.close(parent_fd)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ARCHIVE_BYTES",
    "RegistryIntakeError",
    "RegistryPullReceipt",
    "SUPPORTED_PLATFORM",
    "pull_anonymous_oci_archive",
    "resolve_anonymous_reference",
    "skopeo_copy_argv",
]
