"""Experimental BIOS carrier construction, not a boot or security-parity proof.

Run on the authorized root Linux/amd64 preparation worker. Only a newly created
output disk is formatted; the prepared APT extraction and the host installation
are read-only inputs. Failed/uncertain resource ownership leaves a HOLD journal.
The private output contains credentials and must never be published wholesale.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

SCHEMA = "palimpsest.nova-boot-artifacts.v1"
RESULT_SCHEMA = "palimpsest.nova-boot-carrier.v1"
KERNEL_RELEASE = "6.8.0-147-generic"
DRIVER_VERSION = "580.178.04"
KERNEL_SHA256 = "41ba36d860aa0b0cd995f4ca8856d80249b244577e704929b55be13fbd1ebd2f"
DISK_BYTES = 4 * 1024**3
MAX_FILE_BYTES = 2 * 1024**3
MAX_ARCHIVE_BYTES = 2 * 1024**3 - 1
MAX_FILES = 100000
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
RUNTIME_TOOLS = ("busybox", "ip", "blkid", "mount", "umount", "losetup", "modprobe", "modinfo", "mount.ceph", "ssh")
BUILD_TOOLS = (
    "ldd",
    "modinfo",
    "depmod",
    "sfdisk",
    "mkfs.ext4",
    "grub-install",
    "findmnt",
    "losetup",
    "mount",
    "umount",
    "blkid",
)


class CarrierError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def archive_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not value.isascii()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or value.startswith("/")
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or value == "TRAILER!!!"
    ):
        raise CarrierError("invalid bounded archive path")
    return value


def _regular(path: Path, maximum: int = MAX_FILE_BYTES) -> os.stat_result:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= maximum:
        raise CarrierError(f"not a bounded regular source: {path}")
    return info


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_source(path: Path, maximum: int = MAX_FILE_BYTES) -> BinaryIO:
    before = _regular(path, maximum)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    if _identity(os.fstat(fd)) != _identity(before):
        os.close(fd)
        raise CarrierError("source changed while opening")
    return os.fdopen(fd, "rb")


def file_identity(path: Path) -> dict[str, Any]:
    with _open_source(path, DISK_BYTES) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise CarrierError("source changed while hashing")
    return {"sha256": digest, "size": before.st_size, "mode": stat.S_IMODE(before.st_mode)}


def _save(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def command(argv: list[str], *, data: bytes | None = None) -> bytes:
    result = subprocess.run(argv, input=data, capture_output=True, env=SAFE_ENV, check=False)
    if result.returncode:
        raise CarrierError(
            f"helper failed ({result.returncode}): {argv[0]}: {result.stderr[-4096:].decode('utf-8', 'replace')}"
        )
    return result.stdout


@dataclass(frozen=True)
class CpioEntry:
    path: str
    mode: int
    source: Path | bytes = b""
    rdevmajor: int = 0
    rdevminor: int = 0


def _record_header(inode: int, entry: CpioEntry, size: int) -> bytes:
    name = (entry.path if entry.path == "TRAILER!!!" else archive_path(entry.path)).encode("ascii") + b"\0"
    fields = (
        inode,
        entry.mode,
        0,
        0,
        2 if stat.S_ISDIR(entry.mode) else 1,
        0,
        size,
        0,
        0,
        entry.rdevmajor,
        entry.rdevminor,
        len(name),
        0,
    )
    if any(not 0 <= field <= 0xFFFFFFFF for field in fields):
        raise CarrierError("newc field overflow")
    raw = b"070701" + b"".join(f"{field:08x}".encode("ascii") for field in fields) + name
    return raw + b"\0" * (-len(raw) % 4)


def write_newc(entries: list[CpioEntry], output: BinaryIO) -> int:
    """Stream a canonical root-owned newc archive, including dev/console 5:1."""
    if not 1 <= len(entries) <= MAX_FILES:
        raise CarrierError("invalid archive entry count")
    ordered = sorted(entries, key=lambda item: item.path)
    names = [archive_path(item.path) for item in ordered]
    if len(set(names)) != len(names):
        raise CarrierError("duplicate archive path")
    directories = {item.path for item in ordered if stat.S_ISDIR(item.mode)}
    for item in ordered:
        path = PurePosixPath(item.path)
        if any(str(parent) not in directories for parent in path.parents if str(parent) != "."):
            raise CarrierError("archive parent missing")
        if stat.S_IFMT(item.mode) not in (stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK, stat.S_IFCHR):
            raise CarrierError("unsupported archive entry type")
        if stat.S_ISCHR(item.mode) and (item.path, item.rdevmajor, item.rdevminor) != ("dev/console", 5, 1):
            raise CarrierError("only root console character device is admitted")
    total = 0

    def emit(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if total > MAX_ARCHIVE_BYTES:
            raise CarrierError("initramfs exceeds bound")
        output.write(data)

    for inode, item in enumerate(ordered, 1):
        size = _regular(item.source).st_size if isinstance(item.source, Path) else len(item.source)
        if size > MAX_FILE_BYTES or ((stat.S_ISDIR(item.mode) or stat.S_ISCHR(item.mode)) and size):
            raise CarrierError("invalid archive entry size")
        emit(_record_header(inode, item, size))
        if isinstance(item.source, Path):
            with _open_source(item.source) as source:
                before = os.fstat(source.fileno())
                if before.st_size != size:
                    raise CarrierError("archive source changed")
                remaining = size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise CarrierError("short archive source")
                    emit(chunk)
                    remaining -= len(chunk)
                if _identity(os.fstat(source.fileno())) != _identity(before):
                    raise CarrierError("archive source changed")
        else:
            emit(item.source)
        emit(b"\0" * (-size % 4))
    # TRAILER!!! is reserved to the writer, not accepted as an input pathname.
    emit(_record_header(len(ordered) + 1, CpioEntry("TRAILER!!!", 0), 0))
    emit(b"\0" * (-total % 512))
    return total


def write_initramfs(root: Path, target: Path) -> None:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = archive_path(path.relative_to(root).as_posix())
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            entries.append(CpioEntry(relative, stat.S_IFDIR | stat.S_IMODE(info.st_mode)))
        elif stat.S_ISREG(info.st_mode):
            entries.append(CpioEntry(relative, stat.S_IFREG | stat.S_IMODE(info.st_mode), path))
        elif stat.S_ISLNK(info.st_mode):
            link = os.readlink(path)
            if not link.isascii() or "\0" in link or len(link) > 1024:
                raise CarrierError("invalid archive symlink")
            entries.append(CpioEntry(relative, stat.S_IFLNK | 0o777, link.encode("ascii")))
        else:
            raise CarrierError("unexpected staged special file")
    entries.append(CpioEntry("dev/console", stat.S_IFCHR | 0o600, rdevmajor=5, rdevminor=1))
    with target.open("xb") as raw:
        os.chmod(target, 0o600)
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as compressed:
            write_newc(entries, compressed)
        raw.flush()
        os.fsync(raw.fileno())


def _copy(source: Path, destination: Path, *, mode: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if destination.exists() or destination.is_symlink():
        if file_identity(source)["sha256"] != file_identity(destination)["sha256"]:
            raise CarrierError(f"conflicting staged file: {destination}")
        return
    with _open_source(source) as incoming, destination.open("xb") as outgoing:
        before = os.fstat(incoming.fileno())
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        if _identity(os.fstat(incoming.fileno())) != _identity(before):
            raise CarrierError("source changed during copy")
    os.chmod(destination, mode if mode is not None else stat.S_IMODE(before.st_mode) & 0o755)


def _resolved_file(source: Path, boundary: Path | None = None) -> Path:
    resolved = source.resolve(strict=True)
    if boundary is not None and not resolved.is_relative_to(boundary):
        raise CarrierError("prepared-bundle symlink escapes input root")
    _regular(resolved)
    return resolved


def _elf(path: Path) -> bool:
    with _open_source(path) as stream:
        header = stream.read(20)
    if not header.startswith(b"\x7fELF"):
        return False
    if len(header) != 20 or header[4:6] != b"\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 62:
        raise CarrierError(f"ELF is not Linux amd64: {path}")
    return True


def shared_dependencies(path: Path, run: Callable[..., bytes] = command, *, bundle: Path | None = None) -> list[Path]:
    if not _elf(path):
        return []
    # ldd must be run only on selected trusted host tools / prepared signed APT artifacts.
    try:
        argv = ["ldd", str(path)]
        if bundle is not None:
            libraries = ":".join(
                str(bundle / directory) for directory in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu")
            )
            argv = ["env", "LD_LIBRARY_PATH=" + libraries, *argv]
        output = run(argv).decode("utf-8", "strict")
    except CarrierError as exc:
        if "statically linked" in str(exc) or "not a dynamic executable" in str(exc):
            return []
        raise
    if "not found" in output:
        raise CarrierError(f"unresolved shared dependency for {path}")
    result = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("linux-vdso", "statically linked")):
            continue
        match = re.fullmatch(r"(?:\S+\s+=>\s+)?(/[^\s]+)\s+\(0x[0-9a-fA-F]+\)", line)
        if match is None:
            raise CarrierError(f"unrecognized ldd dependency: {line}")
        dependency = Path(match[1])
        if (
            bundle is not None
            and dependency.name.startswith(("libcuda", "libnvidia"))
            and not dependency.is_relative_to(bundle)
        ):
            raise CarrierError("NVIDIA dependency escaped prepared bundle")
        _regular(_resolved_file(dependency))
        result.append(dependency)
    return result


def _copy_executable(
    source: Path,
    root: Path,
    destination: str,
    run: Callable[..., bytes],
    *,
    bundle: Path | None = None,
    system_root: Path | None = None,
) -> None:
    pending = [(source, destination)]
    visited: set[str] = set()
    while pending:
        path, relative = pending.pop()
        archive_path(relative)
        if relative in visited:
            continue
        visited.add(relative)
        resolved = _resolved_file(path, bundle if bundle is not None and path.is_relative_to(bundle) else None)
        _copy(resolved, root / relative)
        for dependency in shared_dependencies(resolved, run, bundle=bundle):
            target = (
                dependency.relative_to(bundle).as_posix()
                if bundle is not None and dependency.is_relative_to(bundle)
                else dependency.as_posix().lstrip("/")
            )
            if system_root is not None and not dependency.name.startswith(("libcuda", "libnvidia")):
                # Bootstrap tools use the worker's libc closure in initramfs;
                # image processes retain their original libc. Never inject host
                # glibc into /usr/local/nvidia/lib64 in the source OCI root.
                _copy_executable(dependency, system_root, target, run, bundle=bundle)
            else:
                pending.append((dependency, target))


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    boundary: Path,
    run: Callable[..., bytes] | None = None,
    closure_root: Path | None = None,
    skip: Callable[[Path], bool] = lambda _: False,
) -> None:
    if not source.is_dir() or source.is_symlink():
        raise CarrierError(f"missing regular input directory: {source}")
    count = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs if not skip(base / name))
        for name in dirs[:]:
            candidate = base / name
            if candidate.is_symlink():
                raise CarrierError(f"directory symlink not admitted in source tree: {candidate}")
        for name in sorted(files):
            candidate = base / name
            if skip(candidate):
                continue
            relative = archive_path(candidate.relative_to(source).as_posix())
            resolved = _resolved_file(candidate, boundary)
            target = destination / relative
            _copy(resolved, target)
            count += 1
            if count > MAX_FILES:
                raise CarrierError("source tree has too many files")
            if run is not None and closure_root is not None and _elf(resolved):
                for dependency in shared_dependencies(resolved, run):
                    _copy_executable(dependency, closure_root, dependency.as_posix().lstrip("/"), run)


def _source_image_digest(source):
    if isinstance(source, dict) and source.get("kind") == "published":
        digest = "sha256:" + hashlib.sha256(canonical_json(source.get("manifest"))).hexdigest()
        if source.get("manifest_digest") != digest:
            raise CarrierError("published source manifest digest mismatch")
        return digest
    return source.get("root_digest") if isinstance(source, dict) else source


def load_runtime_policy(
    runtime_script: Path,
    plan: dict[str, Any],
    *,
    seed: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> str:
    """Use exactly the carried runtime's pure canonical-policy helper."""
    _regular(runtime_script, 1024 * 1024)
    name = "_palimpsest_carrier_runtime_policy"
    spec = importlib.util.spec_from_file_location(name, runtime_script)
    if spec is None or spec.loader is None:
        raise CarrierError("cannot load selected runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
        helper = getattr(module, "plan_policy_digest", None)
        if not callable(helper):
            raise CarrierError("runtime lacks plan_policy_digest")
        digest = helper(plan)
        if seed is not None:
            module.validate_seed({**seed, "expected_plan_digest": digest})
            source = plan.get("image", {}).get("source")
            if isinstance(source, dict) and source.get("kind") == "published":
                module.validate_image_projection(plan["image"], seed["expected_image_digest"])
        if manifest is not None:
            module.validate_artifacts_manifest(manifest)
    finally:
        sys.dont_write_bytecode = bytecode
        sys.modules.pop(name, None)
    if not isinstance(digest, str) or re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", digest) is None:
        raise CarrierError("runtime returned invalid plan-policy digest")
    return digest


def loop_identity(device: str) -> tuple[int, int] | None:
    if re.fullmatch(r"/dev/loop[0-9]+", device) is None:
        raise CarrierError("invalid loop node")
    fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        status = bytearray(232)
        try:
            fcntl.ioctl(fd, 0x4C05, status)  # LOOP_GET_STATUS64
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                return None
            raise
        return struct.unpack_from("=QQ", status)
    finally:
        os.close(fd)


class OwnedDisk:
    """Durable intent before attach/mount; refuse uncertain cleanup, never force."""

    def __init__(
        self,
        disk: Path,
        journal: Path,
        *,
        run: Callable[..., bytes] = command,
        inspect: Callable[[str], tuple[int, int] | None] = loop_identity,
    ):
        info = _regular(disk, DISK_BYTES)
        self.disk, self.journal, self.run, self.inspect = disk, journal, run, inspect
        self.backing = (info.st_dev, info.st_ino)
        self.device: str | None = None
        self.mounts: list[dict[str, Any]] = []
        self.status = "PREPARED"
        self.persist()

    def persist(self) -> None:
        _save(
            self.journal,
            {
                "schema": RESULT_SCHEMA,
                "state": self.status,
                "disk": str(self.disk),
                "backing": list(self.backing),
                "loop": self.device,
                "mounts": self.mounts,
            },
        )

    def require_owned(self) -> str:
        if self.device is None or self.inspect(self.device) != self.backing:
            raise CarrierError("foreign or uncertain loop mapping; HOLD")
        info = _regular(self.disk, DISK_BYTES)
        if (info.st_dev, info.st_ino) != self.backing:
            raise CarrierError("disk backing path changed; HOLD")
        return self.device

    def attach(self) -> str:
        device = self.run(["losetup", "--find"]).decode("ascii").strip()
        if re.fullmatch(r"/dev/loop[0-9]+", device) is None or self.inspect(device) is not None:
            raise CarrierError("selected loop is not free")
        self.device = device
        self.status = "ATTACH_INTENT"
        self.persist()
        self.run(["losetup", "--partscan", device, str(self.disk)])
        self.require_owned()
        self.status = "ATTACHED"
        self.persist()
        return device

    def partition(self, number: int) -> str:
        device = self.require_owned()
        if number not in (1, 2):
            raise CarrierError("invalid owned partition")
        node = f"{device}p{number}"
        if not stat.S_ISBLK(os.stat(node, follow_symlinks=False).st_mode):
            raise CarrierError("owned partition is not a block device")
        expected = Path("/sys/class/block") / Path(device).name
        parent = (Path("/sys/class/block") / Path(node).name).resolve().parent
        if parent != expected.resolve():
            raise CarrierError("partition not parented by exact owned loop")
        return node

    def mount(self, number: int, target: Path) -> None:
        node = self.partition(number)
        target.mkdir(mode=0o700)
        info = target.stat()
        record = {
            "target": str(target),
            "source": node,
            "target_dev": info.st_dev,
            "target_ino": info.st_ino,
            "rdev": os.stat(node).st_rdev,
        }
        self.mounts.append(record)
        self.persist()
        self.require_owned()
        self.run(["mount", "-t", "ext4", "-o", "nodev,nosuid", node, str(target)])
        self.persist()

    def close(self) -> None:
        try:
            for record in reversed(self.mounts[:]):
                self.require_owned()
                result = json.loads(
                    self.run(["findmnt", "--json", "--mountpoint", record["target"], "--output", "TARGET,MAJ:MIN"])
                )
                rows = result.get("filesystems", [])
                expected = f"{os.major(record['rdev'])}:{os.minor(record['rdev'])}"
                if len(rows) != 1 or rows[0].get("target") != record["target"] or rows[0].get("maj:min") != expected:
                    raise CarrierError("mount ownership uncertain; HOLD")
                self.run(["umount", record["target"]])
                info = Path(record["target"]).stat()
                if (info.st_dev, info.st_ino) != (record["target_dev"], record["target_ino"]):
                    raise CarrierError("mount target identity changed; HOLD")
                self.mounts.remove(record)
                self.persist()
            if self.device is not None:
                current = self.inspect(self.device)
                if current is not None:
                    self.require_owned()
                    self.run(["losetup", "--detach", self.device])
                    if self.inspect(self.device) is not None:
                        raise CarrierError("owned loop still attached; HOLD")
            self.status = "RELEASED"
            self.persist()
        except BaseException:
            self.status = "HOLD"
            self.persist()
            raise


def _tool(name: str) -> Path:
    value = shutil.which(name, path=SAFE_ENV["PATH"])
    if value is None:
        raise CarrierError(f"required helper unavailable: {name}")
    return _resolved_file(Path(value))


def _module_asset_excluded(path: Path) -> bool:
    if path.name in ("build", "source") or path.name.endswith(".sig"):
        return True
    return path.name == "bits" and path.parent.name.startswith("nvidia")


def module_files(directory: Path) -> list[Path]:
    """Select actual loadable modules, never package assembler/signature inputs."""
    selected = []
    for current, directories, files in os.walk(directory, followlinks=False):
        base = Path(current)
        directories[:] = sorted(name for name in directories if not _module_asset_excluded(base / name))
        for name in sorted(files):
            path = base / name
            if name.endswith((".ko", ".ko.zst", ".ko.xz", ".ko.gz")) and not _module_asset_excluded(path):
                if any(
                    part.startswith("nvidia-") and part != "nvidia-580"
                    for part in path.relative_to(directory).parts[:-1]
                ):
                    raise CarrierError("competing NVIDIA module family in prepared bundle")
                selected.append(path)
    return sorted(selected)


def _prepare_assets(bundle: Path, runtime: Path, root: Path, driver: Path, run: Callable[..., bytes]) -> None:
    kernel = bundle / "boot" / f"vmlinuz-{KERNEL_RELEASE}"
    if file_identity(kernel)["sha256"] != KERNEL_SHA256:
        raise CarrierError("prepared image kernel SHA256 mismatch")
    modules = bundle / "lib/modules" / KERNEL_RELEASE
    nvidia = module_files(modules / "kernel/nvidia-580")
    if not nvidia:
        raise CarrierError("prepared matching NVIDIA modules missing")
    for module in nvidia:
        source = _resolved_file(module, bundle)
        if run(["modinfo", "-F", "version", str(source)]).decode().strip() != DRIVER_VERSION:
            raise CarrierError("NVIDIA module version mismatch")
        if run(["modinfo", "-F", "signer", str(source)]).decode().strip().find("Canonical") < 0:
            raise CarrierError("NVIDIA module is not Canonical signed")
    for module in module_files(modules):
        source = _resolved_file(module, bundle)
        vermagic = run(["modinfo", "-F", "vermagic", str(source)]).decode().split()
        if not vermagic or vermagic[0] != KERNEL_RELEASE:
            raise CarrierError("prepared kernel module ABI mismatch")
    _copy_tree(modules, root / "lib/modules" / KERNEL_RELEASE, boundary=bundle, skip=_module_asset_excluded)
    for module in module_files(root / "lib/modules" / KERNEL_RELEASE):
        vermagic = run(["modinfo", "-F", "vermagic", str(module)]).decode().split()
        if not vermagic or vermagic[0] != KERNEL_RELEASE:
            raise CarrierError("staged kernel module ABI mismatch")
        if module.parent.name == "nvidia-580":
            if run(["modinfo", "-F", "version", str(module)]).decode().strip() != DRIVER_VERSION:
                raise CarrierError("staged NVIDIA module version mismatch")
            if "Canonical" not in run(["modinfo", "-F", "signer", str(module)]).decode():
                raise CarrierError("staged NVIDIA module signer mismatch")
    run(["depmod", "-b", str(root), KERNEL_RELEASE])
    _copy(kernel, root / "bootstrap/kernel", mode=0o644)
    if file_identity(root / "bootstrap/kernel")["sha256"] != KERNEL_SHA256:
        raise CarrierError("staged kernel SHA256 mismatch")
    _copy(runtime, root / "bootstrap/nova_ceph_boot.py", mode=0o644)
    # The NVIDIA userspace is outside the initramfs and never installed on host.
    _copy_executable(
        _resolved_file(bundle / "usr/bin/nvidia-smi", bundle),
        driver,
        "usr/bin/nvidia-smi",
        run,
        bundle=bundle,
        system_root=root,
    )
    library_root = bundle / "usr/lib/x86_64-linux-gnu"
    selected = sorted(
        path
        for path in library_root.iterdir()
        if path.name.startswith(
            (
                "libcuda.so",
                "libnvidia-ml.so",
                "libnvidia-ptxjitcompiler.so",
                "libnvidia-nvvm.so",
                "libnvidia-gpucomp.so",
                "libnvidia-fatbinaryloader.so",
            )
        )
    )
    if not any(path.name.startswith("libcuda.so") for path in selected) or not any(
        path.name.startswith("libnvidia-ml.so") for path in selected
    ):
        raise CarrierError("prepared CUDA/NVML userspace libraries missing")
    for source in selected:
        if source.is_dir():
            continue
        resolved = _resolved_file(source, bundle)
        if DRIVER_VERSION not in resolved.name:
            raise CarrierError("prepared NVIDIA userspace version mismatch")
        _copy_executable(
            _resolved_file(source, bundle),
            driver,
            "usr/lib/x86_64-linux-gnu/" + source.name,
            run,
            bundle=bundle,
            system_root=root,
        )
    _copy_tree(bundle / "lib/firmware", driver / "lib/firmware", boundary=bundle)


def _stdlib_excluded(path: Path) -> bool:
    # No ambient host customization, packages, caches, or development linker
    # aliases. Debian's customization/config symlinks intentionally escape stdlib.
    return path.name in (
        "__pycache__",
        "site-packages",
        "dist-packages",
        "test",
        "tests",
        "sitecustomize.py",
        "usercustomize.py",
    ) or path.name.startswith("config-")


def _prepare_host_runtime(root: Path, run: Callable[..., bytes]) -> None:
    applets = set(run([str(_tool("busybox")), "--list"]).decode("ascii").split())
    if not {"sh", "ip", "ifconfig", "udhcpc", "mkdir", "sleep", "cat"} <= applets:
        raise CarrierError("busybox lacks required bootstrap applets")
    interpreter = _resolved_file(Path(sys.executable))
    _copy_executable(interpreter, root, interpreter.as_posix().lstrip("/"), run)
    _copy(interpreter, root / "usr/bin/python3")
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    _copy_tree(
        stdlib, root / stdlib.as_posix().lstrip("/"), boundary=stdlib, run=run, closure_root=root, skip=_stdlib_excluded
    )
    dynamic = Path(sysconfig.get_config_var("DESTSHARED") or "")
    if not dynamic.is_absolute() or not dynamic.is_dir():
        raise CarrierError("Python extension-library directory unavailable")
    if not dynamic.is_relative_to(stdlib):
        _copy_tree(dynamic, root / dynamic.as_posix().lstrip("/"), boundary=dynamic, run=run, closure_root=root)
    for name in RUNTIME_TOOLS:
        source = _tool(name)
        destination = "usr/bin/" + name
        _copy_executable(source, root, destination, run)
        # Both standard PATH layouts and explicit /sbin paths work in PID1.
        for directory in ("bin", "sbin", "usr/sbin"):
            _copy(source, root / directory / name)
    for name in ("sh", "mkdir", "sleep", "cat", "udhcpc"):
        (root / "bin" / name).symlink_to("/usr/bin/busybox")
    etc = root / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
    (etc / "group").write_text("root:x:0:\n")
    (etc / "nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
    (etc / "hosts").write_text("127.0.0.1 localhost\n::1 localhost\n")
    # NSS libraries are dlopened rather than included in ldd's closure.
    for name in ("libnss_files.so.2", "libnss_dns.so.2"):
        candidates = [Path("/lib/x86_64-linux-gnu") / name, Path("/usr/lib/x86_64-linux-gnu") / name]
        for candidate in candidates:
            if candidate.exists():
                _copy_executable(candidate, root, candidate.as_posix().lstrip("/"), run)
                break


def _seed_files(directory: Path, root: Path) -> dict[str, Any]:
    info = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise CarrierError("seed input must be an owned private directory")
    seed_path = directory / "seed.json"
    if _regular(seed_path, 65536).st_mode & 0o077:
        raise CarrierError("seed JSON must be private")
    seed = json.loads(seed_path.read_bytes())
    if not isinstance(seed, dict):
        raise CarrierError("seed must be an object")
    forbidden = ("ceph_secret", "ceph_key", "ceph_access", "secret")
    if any(key in seed for key in forbidden):
        raise CarrierError("Ceph credentials must not enter boot media")
    for field in ("worker_known_hosts", "ssh_private_key", "client_cert", "client_key", "ca_file"):
        value = seed.get(field)
        if not isinstance(value, str) or not value.startswith("/bootstrap/"):
            raise CarrierError(f"seed {field} must name a /bootstrap file")
        name = archive_path(value.removeprefix("/bootstrap/"))
        if "/" in name or name in ("seed.json", "artifacts.json", "nova_ceph_boot.py", "kernel"):
            raise CarrierError("seed filename reserved or nested")
        source = directory / name
        source_info = _regular(source, 65536)
        if source_info.st_mode & 0o077 or source_info.st_uid != os.getuid():
            raise CarrierError("seed credentials must be owned private regular files")
        _copy(source, root / "bootstrap" / name, mode=0o600)
    if seed.get("bootstrap_url") != "https://127.0.0.1:9443":
        raise CarrierError("bootstrap must use the restricted local SSH tunnel")
    seed["artifacts_manifest"] = "/bootstrap/artifacts.json"
    return seed


def _check_host_key(seed: dict[str, Any], root: Path, run: Callable[..., bytes]) -> None:
    supported = set(run([str(_tool("ssh")), "-Q", "key"]).decode("ascii").split())
    host = seed.get("worker_address")
    path = root / seed["worker_known_hosts"].lstrip("/")
    with _open_source(path, 65536) as stream:
        lines = stream.read().decode("ascii").splitlines()
    bound = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3 or fields[1] not in supported:
            raise CarrierError("unsupported pinned SSH host key")
        try:
            raw = base64.b64decode(fields[2], validate=True)
        except ValueError as exc:
            raise CarrierError("invalid pinned SSH host key encoding") from exc
        key_type = fields[1].encode("ascii")
        if len(raw) < 4 or struct.unpack_from(">I", raw)[0] != len(key_type) or raw[4 : 4 + len(key_type)] != key_type:
            raise CarrierError("pinned SSH host key type mismatch")
        if host in fields[0].split(",") or f"[{host}]:22" in fields[0].split(","):
            bound = True
    if not bound:
        raise CarrierError("known_hosts does not pin the exact worker address")


def _prepare_builder_inputs(root: Path, output: Path) -> Path:
    grub_source = Path("/usr/lib/grub/i386-pc")
    grub_copy = output / "grub-modules"
    _copy_tree(grub_source, grub_copy, boundary=grub_source.resolve(strict=True))
    inputs = {
        "helpers": {name: file_identity(_tool(name)) for name in sorted(set((*RUNTIME_TOOLS, *BUILD_TOOLS)))},
        "grub_modules": {
            path.relative_to(grub_copy).as_posix(): file_identity(path)
            for path in sorted(grub_copy.rglob("*"))
            if path.is_file()
        },
    }
    _save(root / "bootstrap/build-inputs.json", inputs)
    return grub_copy


def _manifest(root: Path, driver: Path, excluded: set[str], carrier_files: Path | None = None) -> dict[str, Any]:
    files = {}
    locations = [(root, ""), (driver, "driver/")]
    if carrier_files is not None:
        locations.append((carrier_files, "carrier/"))
    for base, prefix in locations:
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or path.is_dir():
                continue
            name = archive_path(prefix + path.relative_to(base).as_posix())
            if name not in excluded:
                files[name] = file_identity(path)
    return {"schema": SCHEMA, "kernel_release": KERNEL_RELEASE, "driver_version": DRIVER_VERSION, "files": files}


def ensure_carrier_capacity(paths: tuple[Path, ...], *, partition_bytes: int = 4192256 * 512) -> int:
    """Conservative ext4 block accounting, before any partition or mount effect.

    Reserve 256 MiB for filesystem metadata, journal, GRUB core/configuration,
    and slack. Every payload file and directory rounds up to a 4 KiB block;
    the entire staged GRUB input is counted even if only a subset is installed.
    """
    required = 256 * 1024**2
    for source in paths:
        candidates = [source, *source.rglob("*")] if source.is_dir() else [source]
        for path in candidates:
            info = path.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                required += 4096
            elif stat.S_ISREG(info.st_mode):
                required += max(4096, (info.st_size + 4095) // 4096 * 4096)
            else:
                raise CarrierError("nonregular carrier payload")
            if required > partition_bytes:
                raise CarrierError("carrier payload exceeds PAL-CARRIER partition budget; no disk formatted")
    return required


def build_carrier(
    bundle_path: Path,
    runtime_script_path: Path,
    seed_files_dir: Path,
    plan_template: dict[str, Any] | Path,
    output_dir: Path,
    *,
    run: Callable[..., bytes] = command,
    probe_path: Path | None = None,
) -> dict[str, Any]:
    """Build a versioned, private experimental carrier; no Glance or VM action."""
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64") or os.geteuid() != 0:
        raise CarrierError("carrier preparation requires root Linux amd64")
    for name in (*RUNTIME_TOOLS, *BUILD_TOOLS):
        _tool(name)
    bundle = bundle_path.resolve(strict=True)
    if not bundle.is_dir() or bundle_path.is_symlink():
        raise CarrierError("prepared bundle must be a directory, not a link")
    runtime = _resolved_file(runtime_script_path)
    if output_dir.exists() or output_dir.is_symlink() or not output_dir.is_absolute():
        raise CarrierError("output must be an absolute new directory")
    if output_dir.is_relative_to(bundle) or output_dir.is_relative_to(seed_files_dir.resolve(strict=True)):
        raise CarrierError("output must not overlap source inputs")
    output_dir.mkdir(mode=0o700)
    root, driver = output_dir / "initramfs-tree", output_dir / "driver"
    root.mkdir(mode=0o700)
    driver.mkdir(mode=0o700)
    for name in ("dev", "proc", "sys", "run", "tmp", "root", "carrier", "driver", "newroot", "bootstrap"):
        (root / name).mkdir(mode=0o755)
    _save(output_dir / "build-state.json", {"schema": RESULT_SCHEMA, "state": "PREPARING", "boot_proven": False})
    owner = None
    try:
        _prepare_assets(bundle, runtime, root, driver, run)
        _prepare_host_runtime(root, run)
        grub_modules = _prepare_builder_inputs(root, output_dir)
        plan = (
            json.loads(plan_template.read_bytes())
            if isinstance(plan_template, Path)
            else json.loads(canonical_json(plan_template))
        )
        carrier_files = output_dir / "carrier-files"
        carrier_files.mkdir(mode=0o700)
        if probe_path is not None:
            probe = _resolved_file(probe_path)
            if not _elf(probe) or not _regular(probe).st_mode & 0o111:
                raise CarrierError("CUDA probe must be a Linux amd64 executable")
            shared_dependencies(probe, run)
            _copy(probe, carrier_files / "probe/cuda-probe", mode=0o555)
        if plan.get("gpu", {}).get("enabled") and probe_path is None:
            raise CarrierError("GPU plan requires an explicit prepared CUDA probe")
        init = root / "init"
        init.write_text(
            "#!/bin/sh\nexport PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexec /usr/bin/python3 -I -B /bootstrap/nova_ceph_boot.py --seed /bootstrap/seed.json\n"
        )
        init.chmod(0o755)
        seed = _seed_files(seed_files_dir, root)
        excluded = {"bootstrap/seed.json", "bootstrap/artifacts.json"}
        _check_host_key(seed, root, run)
        excluded.update(
            value.lstrip("/")
            for key, value in seed.items()
            if key in ("ssh_private_key", "client_cert", "client_key", "ca_file")
        )
        manifest = _manifest(root, driver, excluded, carrier_files)
        manifest_bytes = canonical_json(manifest)
        if len(manifest_bytes) > 32 * 1024**2:
            raise CarrierError("artifact inventory exceeds bound")
        carrier_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        plan["carrier_digest"] = carrier_digest
        if plan.get("run_id") != seed.get("run_id") or plan.get("project_id") != seed.get("project_id"):
            raise CarrierError("seed/plan identity mismatch")
        if (
            plan.get("gpu", {}).get("kernel_release") != KERNEL_RELEASE
            or plan.get("gpu", {}).get("driver_version") != DRIVER_VERSION
        ):
            raise CarrierError("plan kernel/driver mismatch")
        source = plan.get("image", {}).get("source")
        image_digest = _source_image_digest(source)
        if image_digest != seed.get("expected_image_digest"):
            raise CarrierError("seed/plan source-image pin mismatch")
        seed["carrier_digest"] = carrier_digest
        policy = load_runtime_policy(root / "bootstrap/nova_ceph_boot.py", plan, seed=seed, manifest=manifest)
        seed["expected_plan_digest"] = policy
        _save(root / "bootstrap/artifacts.json", manifest)
        _save(root / "bootstrap/seed.json", seed)
        _save(output_dir / "artifacts.json", manifest)
        _save(output_dir / "plan-template.json", plan)
        kernel = output_dir / "kernel"
        _copy(root / "bootstrap/kernel", kernel, mode=0o600)
        initramfs = output_dir / "initramfs.gz"
        write_initramfs(root, initramfs)
        ensure_carrier_capacity((kernel, initramfs, driver, carrier_files, grub_modules))
        disk = output_dir / "carrier.raw"
        fd = os.open(disk, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.ftruncate(fd, DISK_BYTES)
            os.fsync(fd)
        finally:
            os.close(fd)
        owner = OwnedDisk(disk, output_dir / "resources.json", run=run)
        # DOS table: 1MiB BIOS embedding gap, 2047MiB carrier, remaining upper.
        run(
            ["sfdisk", str(disk)],
            data=b"label: dos\nunit: sectors\n\nstart=2048,size=4192256,type=83,bootable\nstart=4194304,size=4194304,type=83\n",
        )
        owner.attach()
        for number, label in ((1, "PAL-CARRIER"), (2, "PAL-UPPER")):
            node = owner.partition(number)
            owner.require_owned()
            run(["mkfs.ext4", "-q", "-L", label, "-E", "lazy_itable_init=0,lazy_journal_init=0", node])
            observed = run(["blkid", "-s", "LABEL", "-o", "value", node]).decode().strip()
            if observed != label:
                raise CarrierError("formatted partition label mismatch")
        mounted = output_dir / "carrier-mount"
        owner.mount(1, mounted)
        boot = mounted / "boot"
        boot.mkdir(mode=0o755)
        _copy(kernel, boot / "vmlinuz", mode=0o644)
        _copy(initramfs, boot / "initramfs.gz", mode=0o600)
        _copy_tree(driver, mounted / "driver", boundary=driver)
        _copy_tree(carrier_files, mounted, boundary=carrier_files)
        grub = boot / "grub"
        grub.mkdir(mode=0o755)
        (grub / "grub.cfg").write_text(
            'set timeout=0\nset default=0\nserial --unit=0 --speed=115200\nterminal_input serial\nterminal_output serial\nmenuentry "Palimpsest experimental native carrier" {\n search --no-floppy --label PAL-CARRIER --set=root\n linux /boot/vmlinuz console=ttyS0,115200n8 net.ifnames=0 biosdevname=0 panic=0 rdinit=/init\n initrd /boot/initramfs.gz\n}\n'
        )
        owner.require_owned()
        run(
            [
                "grub-install",
                "--target=i386-pc",
                "--directory=" + str(grub_modules),
                "--no-floppy",
                "--boot-directory=" + str(boot),
                owner.device,
            ]
        )
        owner.close()
        artifacts = {
            name: {"path": str(path), **file_identity(path)}
            for name, path in (("kernel", kernel), ("initramfs", initramfs), ("raw_disk", disk))
        }
        result = {
            "schema": RESULT_SCHEMA,
            "state": "BUILT_NOT_BOOT_PROVEN",
            "boot_proven": False,
            "carrier_digest": carrier_digest,
            "manifest_digest": carrier_digest,
            "expected_plan_digest": policy,
            "plan": plan,
            "artifacts": artifacts,
            "kernel_release": KERNEL_RELEASE,
            "driver_version": DRIVER_VERSION,
            "credential_media": True,
            "resources_journal": str(owner.journal),
        }
        _save(output_dir / "result.json", result)
        _save(
            output_dir / "build-state.json",
            {"schema": RESULT_SCHEMA, "state": "BUILT_NOT_BOOT_PROVEN", "boot_proven": False},
        )
        return result
    except BaseException:
        if owner is not None and owner.status not in ("RELEASED", "HOLD"):
            try:
                owner.close()
            except BaseException:
                pass
        _save(
            output_dir / "build-state.json",
            {
                "schema": RESULT_SCHEMA,
                "state": "HOLD" if owner is not None and owner.status != "RELEASED" else "FAILED_PRESERVED",
                "boot_proven": False,
            },
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--runtime-script", required=True, type=Path)
    parser.add_argument("--seed-files", required=True, type=Path)
    parser.add_argument("--plan-template", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--probe", type=Path, help="Prepared Linux amd64 CUDA probe (required for a GPU plan)")
    args = parser.parse_args(argv)
    result = build_carrier(
        args.bundle, args.runtime_script, args.seed_files, args.plan_template, args.output_dir, probe_path=args.probe
    )
    print(canonical_json(result).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
