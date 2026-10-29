"""One-shot direct Linux VM executor for private, pinned Hub build inputs.

The service VM is the privileged kernel boundary, not a hostile-code sandbox.
RUN sees only the image overlay in bubblewrap; worker credentials never enter it.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import platform
import re
import resource
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from .build import CLEAN_TARGETS_V1, MAX_PALIMPSESTFILE_BYTES, parse_palimpsestfile_text, verify_build_integrity
from .digest import digest_file, require_digest, require_file_digest
from .oci_layout import MEDIA_TYPE_LAYER_SQUASHFS

SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
RUN_ENV = {"PATH": SAFE_ENV["PATH"], "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
RUN_CAPS = ("CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_SETUID", "CAP_SETGID")
TOOLS = ("qemu-img", "mount", "umount", "losetup", "blkid", "findmnt", "mksquashfs", "bwrap", "true")
MAX_DISK_BYTES = 32 * 1024**3
MANIFEST_LIMIT = 6 * 1024**2 + 65536
LOOP_SYSFS = Path("/sys/block")
LOOP_GET_STATUS64 = 0x4C05
LOOP_INFO64_BYTES = 232


def _save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="ascii") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _identity(pid: int) -> dict[str, Any]:
    text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return {
        "pid": pid,
        "start_ticks": text.rsplit(") ", 1)[1].split()[19],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(),
    }


def _fence(expected_parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot fence child to parent")
    if os.getppid() != expected_parent:
        os._exit(1)


def _child_limits(parent: int, file_limit: int) -> None:
    _fence(parent)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "cannot restrict privilege acquisition")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
    if hard != resource.RLIM_INFINITY:
        file_limit = min(file_limit, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))


def _command(
    argv: list[str],
    deadline: float,
    *,
    allowed: tuple[int, ...] = (0,),
    capture: bool = False,
    file_limit: int = MAX_DISK_BYTES,
    umask: int = 0o077,
) -> bytes:
    """Drain bounded metadata only; recipe output is deliberately discarded."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("build deadline expired")
    parent = os.getpid()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=SAFE_ENV,
        close_fds=True,
        preexec_fn=lambda: _child_limits(parent, file_limit),
        umask=umask,
    )
    output = bytearray()
    try:
        if capture:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("build deadline expired")
                    for key, _ in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > 65536:
                                raise RuntimeError("tool metadata exceeds limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode not in allowed:
            raise RuntimeError("build tool failed")
        return bytes(output)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _private_mounts() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x00020000) != 0:  # CLONE_NEWNS, deliberately no UID remapping
        raise OSError(ctypes.get_errno(), "mount namespace unavailable")
    _command(["mount", "--make-rprivate", "/"], time.monotonic() + 10)


def _loop_identity(device: str) -> tuple[int, int] | None:
    """Kernel (st_dev, st_ino) of the backing file, or None when detached.

    sysfs `backing_file` text is not an identity: once the executor's private mount
    namespace dies it renders a different, namespace-relative path.
    """
    if re.fullmatch(r"/dev/loop[0-9]+", device) is None:
        raise RuntimeError("invalid owned loop device")
    try:
        fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise RuntimeError("owned loop device node is unavailable") from exc
    try:
        status = bytearray(LOOP_INFO64_BYTES)
        try:
            fcntl.ioctl(fd, LOOP_GET_STATUS64, status)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                return None
            raise
        backing_device, backing_inode = struct.unpack_from("=QQ", status)
        return backing_device, backing_inode
    finally:
        os.close(fd)


def _detach_owned(record: dict[str, Any], job_dir: Path) -> None:
    if not Path(record["path"]).is_relative_to(job_dir):
        raise RuntimeError("owned loop record is outside its job")
    current = _loop_identity(record["device"])
    if current is None:
        return
    if current != (record["dev"], record["ino"]):
        raise RuntimeError("owned loop mapping changed; refusing detach")
    _command(["losetup", "--detach", record["device"]], time.monotonic() + 15)
    if _loop_identity(record["device"]) is not None:
        raise RuntimeError("owned loop remains attached")


class _Executor:
    def __init__(self, job_dir: Path, timeout: float):
        self.job_dir = job_dir
        self.deadline = time.monotonic() + timeout
        self.loops: list[dict[str, Any]] = []
        self.mounts: list[Path] = []

    def command(self, argv: list[str], **kwargs: Any) -> bytes:
        return _command(argv, self.deadline, **kwargs)

    def loop(self, path: Path, *, partitions: bool = False, readonly: bool = True) -> str:
        device = self.command(["losetup", "--find"], capture=True).decode("ascii").strip()
        if re.fullmatch(r"/dev/loop[0-9]+", device) is None:
            raise RuntimeError("invalid free loop device")
        info = path.stat(follow_symlinks=False)
        record = {"device": device, "path": str(path), "dev": info.st_dev, "ino": info.st_ino}
        self.loops.append(record)
        # Intent is durable BEFORE attach, closing the kill-between-attach-and-journal gap.
        _save(self.job_dir / "resources.json", {"loops": self.loops})
        self.command(
            [
                "losetup",
                *(["--read-only"] if readonly else []),
                *(["--partscan"] if partitions else []),
                device,
                str(path),
            ]
        )
        if _loop_identity(device) != (info.st_dev, info.st_ino):
            raise RuntimeError("loop attachment identity mismatch")
        return device

    def mount(self, device: str, target: Path, kind: str, options: str) -> None:
        target.mkdir(mode=0o700)
        # Record before mount: namespace teardown handles interrupted mount syscalls.
        self.mounts.append(target)
        self.command(["mount", "-t", kind, "-o", options, device, str(target)])

    def unmount(self, target: Path) -> None:
        _command(["umount", str(target)], time.monotonic() + 15)
        self.mounts.remove(target)

    def close(self) -> None:
        failures = []
        for target in reversed(self.mounts[:]):
            try:
                self.unmount(target)
            except Exception as exc:
                failures.append(exc)
        for record in reversed(self.loops):
            try:
                _detach_owned(record, self.job_dir)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError("build resource cleanup failed") from failures[0]
        (self.job_dir / "resources.json").unlink(missing_ok=True)


def _partition_nodes(executor: _Executor, device: str) -> list[str]:
    records = [record for record in executor.loops if record["device"] == device]
    if len(records) != 1 or _loop_identity(device) != (records[0]["dev"], records[0]["ino"]):
        raise RuntimeError("partition discovery requires an exact owned loop")
    disk = LOOP_SYSFS / Path(device).name
    partitions = []
    for entry in disk.iterdir():
        partition = entry / "partition"
        if not partition.is_file():
            continue
        number = partition.read_text(encoding="ascii").strip()
        if not number.isdecimal() or int(number) <= 0 or entry.name != f"{disk.name}p{int(number)}":
            raise RuntimeError("ambiguous owned-loop partition name")
        partitions.append((int(number), entry))
    if len(partitions) > 128 or len({number for number, _ in partitions}) != len(partitions):
        raise RuntimeError("ambiguous owned-loop partitions")
    entries = [entry for _, entry in sorted(partitions)] if partitions else [disk]
    nodes = executor.job_dir / "devices"
    nodes.mkdir(mode=0o700)
    result = []
    identities = set()
    for entry in entries:
        numbers = entry.joinpath("dev").read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9]+:[0-9]+", numbers) is None:
            raise RuntimeError("invalid owned-loop device identity")
        major, minor = map(int, numbers.split(":"))
        identity = os.makedev(major, minor)
        if major <= 0 or identity in identities:
            raise RuntimeError("ambiguous owned-loop device identity")
        identities.add(identity)
        node = nodes / entry.name
        # Kernel sysfs supplies the exact partition identity. No udev or shared
        # /dev writes are needed, and nodes are never bound into RUN's /dev.
        os.mknod(node, stat.S_IFBLK | 0o600, identity)
        result.append(str(node))
    return result


def _root_partition(executor: _Executor, device: str) -> Path:
    candidates = _partition_nodes(executor, device)
    roots: list[Path] = []
    for index, candidate in enumerate(candidates):
        kind = executor.command(
            ["blkid", "-p", "-s", "TYPE", "-o", "value", candidate], capture=True, allowed=(0, 2)
        ).strip()
        if kind != b"ext4":
            continue
        target = executor.job_dir / f"base-{index}"
        executor.mount(candidate, target, "ext4", "ro,noload,nodev,nosuid")
        # Never follow image symlinks from the privileged host during discovery.
        directories = []
        for name in ("etc", "usr", "bin"):
            try:
                if stat.S_ISDIR((target / name).lstat().st_mode):
                    directories.append(name)
            except FileNotFoundError:
                pass
        if "etc" in directories and ("usr" in directories or "bin" in directories):
            roots.append(target)
        else:
            executor.unmount(target)
    if len(roots) != 1:
        raise RuntimeError("base must contain exactly one ext4 cloud root")
    return roots[0]


def _sandbox_command(merged: Path, instruction: Any) -> list[str]:
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--cap-drop",
        "ALL",
    ]
    for capability in RUN_CAPS:
        argv += ["--cap-add", capability]
    argv += [
        "--bind",
        str(merged),
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/sys",
        "--clearenv",
    ]
    for key, value in {**RUN_ENV, **instruction.env}.items():
        argv += ["--setenv", key, value]
    # WORKDIR creation and traversal happen ONLY after entering the sandbox.
    directory = shlex.quote(instruction.workdir)
    argv += [
        "--chdir",
        "/",
        "--",
        "/bin/bash",
        "-lc",
        f"umask 022; mkdir -p -- {directory} && cd -- {directory} && {instruction.command}",
    ]
    return argv


def _copy_root_metadata(source: Path, upper: Path) -> None:
    """Separate private job permissions from the effective image's root inode."""
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        upper_fd = os.open(upper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(source_fd)
            os.fchown(upper_fd, info.st_uid, info.st_gid)
            for name in os.listxattr(source_fd):
                os.setxattr(upper_fd, name, os.getxattr(source_fd, name))
            os.fchmod(upper_fd, stat.S_IMODE(info.st_mode))
            os.utime(upper_fd, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(upper_fd)
    finally:
        os.close(source_fd)


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MANIFEST_LIMIT + 1)
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("manifest JSON exceeds limit")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    return manifest


def _clean_upper(upper: Path) -> None:
    """Apply palimpsest-clean-v1 without ever following an upper symlink on host."""
    for relative in CLEAN_TARGETS_V1:
        fds = [os.open(upper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
        try:
            parts = relative.split("/")
            for part in parts[:-1]:
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            parent, name = fds[-1], parts[-1]
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    for child in os.listdir(fd):
                        child_info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISDIR(child_info.st_mode):
                            shutil.rmtree(child, dir_fd=fd)
                        else:
                            os.unlink(child, dir_fd=fd)
                finally:
                    os.close(fd)
            else:
                os.unlink(name, dir_fd=parent)
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError as exc:
            if exc.errno != errno.ELOOP:  # intermediate recipe-created symlink
                raise
        finally:
            for fd in reversed(fds):
                os.close(fd)


def _verify_inputs(manifest: dict[str, Any], job_dir: Path) -> tuple[Any, Path, list[Path]]:
    text = manifest["recipe"]
    if type(text) is not str or not 0 < len(text.encode("utf-8")) <= MAX_PALIMPSESTFILE_BYTES:
        raise ValueError("recipe UTF-8 size exceeds limit")
    recipe = parse_palimpsestfile_text(manifest["recipe"])
    base = manifest["base"]
    digest = require_digest(base["digest"])
    descriptors = manifest["layers"]
    digests = tuple(require_digest(item["digest"]) for item in descriptors)
    verify_build_integrity(recipe, cli_base=digest, cli_layers=digests)
    if base["disk_format"] not in {"raw", "qcow2"}:
        raise ValueError("unsupported cloud image format")
    if base["arch"] != platform.machine():
        raise ValueError("base architecture differs from worker VM")
    previous = None
    paths = [Path(base["path"])]
    for item, layer_digest in zip(descriptors, digests, strict=True):
        if (
            item["media_type"] != MEDIA_TYPE_LAYER_SQUASHFS
            or item["parent_digest"] != previous
            or (previous is None and item["base_image_digest"] != digest)
            or item["base_image_digest"] not in (None, digest)
        ):
            raise ValueError("Hub layer media type, base or parent chain mismatch")
        paths.append(Path(item["path"]))
        previous = layer_digest
    for source, expected in zip(paths, (digest, *digests), strict=True):
        if (
            not source.is_absolute()
            or not source.is_relative_to(job_dir)
            or source.resolve(strict=True) != source
            or not stat.S_ISREG(source.lstat().st_mode)
        ):
            raise ValueError("input must be a private pinned regular file")
        require_file_digest(source, expected)
    return recipe, paths[0], paths[1:]


def run_build(manifest: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    job_dir = job_dir.resolve(strict=True)
    recipe, base, layers = _verify_inputs(manifest, job_dir)
    timeout = float(manifest.get("timeout_seconds", 600))
    if not 0 < timeout <= 3600:
        raise ValueError("invalid remaining build timeout")
    output_limit = manifest.get("max_output_bytes", MAX_DISK_BYTES)
    if type(output_limit) is not int or output_limit <= 0:
        raise ValueError("invalid output size limit")
    disk_limit = manifest.get("max_input_bytes", MAX_DISK_BYTES)
    if type(disk_limit) is not int or disk_limit <= 0:
        raise ValueError("invalid input expansion size limit")
    # All recipe/chain/digest validation precedes any namespace or loop/mount action.
    _private_mounts()
    executor = _Executor(job_dir, timeout)
    try:
        filesystem = executor.command(["findmnt", "-n", "-o", "FSTYPE", "-T", str(job_dir)], capture=True).strip()
        if filesystem not in {b"ext4", b"xfs", b"btrfs", b"tmpfs"}:
            raise RuntimeError("build scratch must be a supported local filesystem")
        info = json.loads(
            executor.command(
                ["qemu-img", "info", "--output=json", "-f", manifest["base"]["disk_format"], str(base)], capture=True
            )
        )
        format_data = info.get("format-specific", {}).get("data", {})
        if info.get("backing-filename") or format_data.get("data-file") or not 0 < info["virtual-size"] <= disk_limit:
            raise ValueError("base external backing/data file or virtual size not allowed")
        if shutil.disk_usage(job_dir).free < info["virtual-size"]:
            raise RuntimeError("insufficient private scratch for base")
        if manifest["base"]["disk_format"] == "qcow2":
            raw = job_dir / "base-converted.raw"
            executor.command(
                ["qemu-img", "convert", "-f", "qcow2", "-O", "raw", str(base), str(raw)], file_limit=disk_limit
            )
            base = raw
        root = _root_partition(executor, executor.loop(base, partitions=True))
        parents = []
        for index, layer in enumerate(layers):
            target = job_dir / f"layer-{index}"
            executor.mount(executor.loop(layer), target, "squashfs", "ro,nodev,nosuid")
            parents.append(target)
        capture = job_dir / "capture"
        executor.mount("tmpfs", capture, "tmpfs", f"size={disk_limit},mode=0700,nodev,nosuid")
        upper, work = (capture / name for name in ("upper", "work"))
        merged = job_dir / "merged"
        upper.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        # Every layer's root inode masks lower roots in the merged view. Seed it
        # from the base image `/` so a stale parent root (0.3.3-era builders
        # captured a private 0700 root) is never propagated; RUN changes to `/`
        # are still captured by the upper itself.
        _copy_root_metadata(root, upper)
        if upper.stat().st_dev != work.stat().st_dev:
            raise RuntimeError("overlay upper/work must share a filesystem")
        lower = ":".join(str(path) for path in [*reversed(parents), root])
        if any(character in str(job_dir) for character in ":,\\\n"):
            raise ValueError("scratch path contains mount option delimiters")
        executor.mount(
            "overlay", merged, "overlay", f"lowerdir={lower},upperdir={upper},workdir={work},index=off,nodev,nosuid"
        )
        for instruction in recipe.runs:
            executor.command(_sandbox_command(merged, instruction), file_limit=disk_limit, umask=0o022)
        executor.unmount(merged)
        _clean_upper(upper)
        output = job_dir / "result.sqsh"
        executor.command(
            [
                "mksquashfs",
                str(upper),
                str(output),
                "-comp",
                "zstd",
                "-Xcompression-level",
                "3",
                "-noappend",
                "-no-exports",
                "-processors",
                "2",
            ],
            file_limit=output_limit,
        )
        with output.open("rb") as handle:
            if handle.read(4) != b"hsqs":
                raise RuntimeError("invalid SquashFS output")
        result = {"digest": digest_file(output), "size_bytes": output.stat().st_size}
    finally:
        executor.close()
    return result


def _group_live(pid: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
        except FileNotFoundError:
            continue
        if int(fields[2]) == pid and fields[0] not in ("Z", "X"):
            return True
    return False


def _stop_recorded(identity: dict[str, Any]) -> None:
    pid = identity["pid"]
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("invalid recorded process identity")
    try:
        current = _identity(pid)
    except FileNotFoundError:
        current = None
    if current != identity:
        # A record naming our PID with another lifetime is stale: Linux does not
        # reuse a PID still referenced as a process-group ID, so its group is gone.
        if pid != os.getpid() and _group_live(pid):
            raise RuntimeError("recorded process group ownership is uncertain")
        return
    if pid == os.getpid():
        raise RuntimeError("refusing to stop the current helper")
    if os.getpgid(pid) != pid:
        raise RuntimeError("recorded process group changed")
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    deadline = time.monotonic() + 15
    while _group_live(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError("recorded process group did not stop")
        time.sleep(0.05)


def _stop_helpers(directory: Path) -> None:
    for marker in directory.glob("helper-*.pid"):
        identity = json.loads(marker.read_text(encoding="ascii"))
        if identity == _identity(os.getpid()):
            continue
        _stop_recorded(identity)
        marker.unlink()


def _mark_helper(directory: Path, expected_parent: int) -> Path:
    _fence_helper(expected_parent)
    marker = directory / f"helper-{uuid.uuid4().hex}.pid"
    # Never replace builder.pid: it remains the authority for original mounts.
    _save(marker, _identity(os.getpid()))
    return marker


def cleanup_build(job_dir: Path) -> None:
    """Reconcile only this journal's exact process identity and loop backing files."""
    job_dir = job_dir.resolve(strict=True)
    marker = job_dir / "builder.pid"
    if marker.exists():
        _stop_recorded(json.loads(marker.read_text(encoding="ascii")))
    _stop_helpers(job_dir)
    journal = job_dir / "resources.json"
    if journal.exists():
        for record in reversed(json.loads(journal.read_text(encoding="ascii"))["loops"]):
            _detach_owned(record, job_dir)
        journal.unlink()
    marker.unlink(missing_ok=True)


def _fence_helper(expected_parent: int) -> None:
    if sys.platform != "linux" or expected_parent <= 0 or os.getsid(0) != os.getpid():
        raise RuntimeError("helper requires an isolated Linux worker session")
    _fence(expected_parent)


def _mark_builder(job_dir: Path, expected_parent: int) -> None:
    _fence_helper(expected_parent)
    # Recipe root retains numeric UID 0. Hide privileged executor address space
    # from same-UID proc/ptrace access even before bubblewrap's PID isolation.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "cannot seal builder process memory")
    _save(job_dir / "builder.pid", _identity(os.getpid()))


def preflight_build_host() -> None:
    """Require direct mount/loop tools and privileges, never KVM or libvirt."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("direct builder requires root in its Linux service VM")
    for tool in TOOLS:
        executable = shutil.which(tool, path=SAFE_ENV["PATH"])
        if executable is None:
            raise RuntimeError("a required direct build tool is unavailable")
        resolved = Path(executable).resolve(strict=True)
        for entry in (resolved, *resolved.parents):
            info = entry.stat()
            if info.st_uid != 0 or info.st_mode & 0o022 or (entry == resolved and not stat.S_ISREG(info.st_mode)):
                raise RuntimeError("a required build tool is mutable")
    _private_mounts()
    # A real private tmpfs mount checks mount capability without host state changes.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="palimpsest-preflight-") as directory:
        deadline = time.monotonic() + 15
        _command(["mount", "-t", "tmpfs", "-o", "size=4096,mode=0700", "tmpfs", directory], deadline)
        _command(["umount", directory], deadline)
    # Probe the installed bubblewrap parser and namespace/capability setup before
    # claiming a job. No recipe, worker environment or worker FD enters this probe.
    smoke = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--cap-drop",
        "ALL",
    ]
    for capability in RUN_CAPS:
        smoke += ["--cap-add", capability]
    smoke += [
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--clearenv",
        "--",
        "/usr/bin/true",
    ]
    _command(smoke, time.monotonic() + 15)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "preflight":
        try:
            scratch = Path(sys.argv[3]).resolve(strict=True)
            marker = _mark_helper(scratch, int(sys.argv[2]))
            _stop_helpers(scratch)
            preflight_build_host()
            marker.unlink()
        except Exception:
            raise SystemExit("builder host preflight failed") from None
        return
    if len(sys.argv) == 4 and sys.argv[1] == "cleanup":
        try:
            directory = Path(sys.argv[2]).resolve(strict=True)
            marker = _mark_helper(directory, int(sys.argv[3]))
            cleanup_build(directory)
            marker.unlink()
        except Exception:
            raise SystemExit("build cleanup failed") from None
        return
    if len(sys.argv) != 3:
        raise SystemExit("expected private job manifest and worker pid")
    manifest_path = Path(sys.argv[1])
    try:
        _mark_builder(manifest_path.parent, int(sys.argv[2]))
        manifest = _load_manifest(manifest_path)
        result = run_build(manifest, manifest_path.parent)
    except (TimeoutError, subprocess.TimeoutExpired):
        raise SystemExit(124) from None
    except Exception:
        raise SystemExit("build failed") from None
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
