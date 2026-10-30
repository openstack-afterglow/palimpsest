"""Credential-free one-shot OCI acquisition and isolated BuildKit execution.

The worker journal owns this private directory. BuildKit has no Docker socket,
no shared cache, no forwarded secrets, and a kernel-owned per-job cgroup. A
separate cleanup invocation must prove that cgroup empty before publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import re
import resource
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .hub_builder import (
    SAFE_ENV,
    _child_limits,
    _command,
    _Executor,
    _fence_helper,
    _load_manifest,
    _mark_builder,
    _mark_helper,
    _private_mounts,
    _save,
    cleanup_build,
)
from .layer_cache import LayerCache
from .oci_image import strict_json_object
from .registry_intake import resolve_anonymous_reference

DEFAULT_HOSTS = ("docker.io", "registry-1.docker.io", "ghcr.io", "quay.io")
MAX_LAYERS = 128
JOURNAL_RESERVE_BYTES = 8 * 1024**2
LOG_TAIL_BYTES = 65536


def _allocate(path: Path, size: int) -> None:
    """Reserve real backing blocks; sparse/truncated files are not a quota."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.posix_fallocate(fd, 0, size)
        os.fsync(fd)
        info = os.fstat(fd)
        if info.st_size != size or info.st_blocks * 512 < size:
            raise RuntimeError("scratch requires fully allocated backing blocks")
    finally:
        os.close(fd)


def _release_journal_reserve(directory: Path) -> None:
    # The only pre-journal cleanup operation: reclaim our fixed private file so
    # a kill during preallocation cannot prevent writing the cleanup identity.
    reserve = directory / "journal.reserve"
    try:
        info = reserve.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise RuntimeError("unsafe journal capacity reserve")
    reserve.unlink()


@contextmanager
def _scratch(directory: Path, capacity: int, maximum: int, deadline: float):
    # The output reserve is not visible to acquisition/build tools. Packing
    # into it never frees backing blocks or grows the persistent worker volume.
    if type(capacity) is not int or not 256 * 1024**2 <= capacity <= 64 * 1024**3:
        raise ValueError("invalid image scratch capacity")
    filesystem = _command(["findmnt", "-n", "-o", "FSTYPE", "-T", str(directory)], deadline, capture=True).strip()
    if filesystem not in {b"ext4", b"xfs", b"tmpfs"}:
        raise RuntimeError("image scratch requires non-CoW preallocated local storage")
    output_bytes = min(maximum, capacity // 4)
    image = directory / "scratch.ext4"
    reserve = directory / "journal.reserve"
    _allocate(reserve, JOURNAL_RESERVE_BYTES)
    try:
        _allocate(directory / "outputs.pack", output_bytes)
        _allocate(image, capacity - output_bytes)
    finally:
        # Release ONLY after the bounded data files are allocated (or setup
        # fails). This independently leaves journal/cleanup metadata headroom;
        # untrusted writes can never consume it through either fixed-size file.
        _release_journal_reserve(directory)
    executor = _Executor(directory, max(0.001, deadline - time.monotonic()))
    try:
        executor.command(
            [
                "mke2fs",
                "-q",
                "-t",
                "ext4",
                "-F",
                "-m",
                "0",
                "-E",
                "nodiscard,lazy_itable_init=0,lazy_journal_init=0",
                str(image),
            ],
            file_limit=capacity,
        )
        if image.stat().st_blocks * 512 < capacity - output_bytes:
            raise RuntimeError("scratch formatting released reserved blocks")
        device = executor.loop(image, readonly=False)
        root = directory / "scratch"
        executor.mount(device, root, "ext4", "rw,nodev,nosuid,nodiscard")
        yield root, output_bytes, executor
    finally:
        executor.close()


@contextmanager
def _temporary_root(root: Path):
    temporary = root / "tmp"
    temporary.mkdir(mode=0o700)
    old_environment = os.environ.get("TMPDIR")
    old_safe = SAFE_ENV.get("TMPDIR")
    old_tempdir = tempfile.tempdir
    os.environ["TMPDIR"] = SAFE_ENV["TMPDIR"] = str(temporary)
    tempfile.tempdir = str(temporary)
    try:
        yield
    finally:
        tempfile.tempdir = old_tempdir
        if old_environment is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_environment
        if old_safe is None:
            SAFE_ENV.pop("TMPDIR", None)
        else:
            SAFE_ENV["TMPDIR"] = old_safe


def normalize_reference(reference: str, hosts=DEFAULT_HOSTS) -> str:
    """Resolve Docker Hub shorthand through the existing anonymous resolver."""
    if not isinstance(reference, str) or not reference or len(reference) > 1024:
        raise ValueError("invalid registry reference")
    if any(ord(c) < 33 or ord(c) == 127 for c in reference) or "://" in reference:
        raise ValueError("invalid registry reference")
    first = reference.split("/", 1)[0]
    if "/" not in reference or not ("." in first or ":" in first or first == "localhost"):
        reference = "docker.io/" + ("library/" if "/" not in reference else "") + reference
    authority = reference.split("/", 1)[0].lower()
    if authority not in hosts or authority != reference.split("/", 1)[0]:
        raise ValueError("registry authority is not allowed")
    return resolve_anonymous_reference(reference).canonical


def extract_context(archive: Path, target: Path, *, maximum_bytes: int, maximum_members: int) -> None:
    """Extract bounded files and deferred internal links; never tarfile.extract."""
    target.mkdir(mode=0o700)
    seen: set[str] = set()
    total = 0
    directories: list[tuple[Path, int]] = []
    links: dict[str, str] = {}
    with tarfile.open(archive, mode="r:") as stream:
        for count, member in enumerate(stream, 1):
            path = PurePosixPath(member.name)
            name = str(path)
            if (
                count > maximum_members
                or not member.name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or "\0" in member.name
                or name in seen
                or (name in (".", "") and not member.isdir())
                or len(name.encode()) > 4096
                or not (member.isfile() or member.isdir() or member.issym())
                or member.sparse is not None
            ):
                raise ValueError("unsafe build context")
            seen.add(name)
            total += member.size
            if member.size < 0 or total > maximum_bytes:
                raise ValueError("build context exceeds limit")
            if name == ".":
                continue
            destination = target.joinpath(*path.parts)
            if member.issym():
                link = member.linkname
                normalized = posixpath.normpath(posixpath.join(str(path.parent), link))
                if (
                    not link
                    or link.startswith("/")
                    or "\\" in link
                    or "\0" in link
                    or len(link.encode()) > 4096
                    or normalized == ".."
                    or normalized.startswith("../")
                ):
                    raise ValueError("unsafe build context link")
                links[name] = link
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if member.isdir():
                destination.mkdir(mode=0o700, exist_ok=True)
                directories.append((destination, member.mode & 0o777))
                continue
            source = stream.extractfile(member)
            if source is None:
                raise ValueError("invalid build context")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with source, os.fdopen(fd, "wb") as output:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("truncated build context")
                    output.write(chunk)
                    remaining -= len(chunk)
                os.fchmod(output.fileno(), member.mode & 0o777)
    for name in seen:
        if any(str(parent) in links for parent in PurePosixPath(name).parents):
            raise ValueError("build context writes beneath symlink")
    for name, link in links.items():
        destination = target.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.symlink_to(link)
    for name in links:
        # Strict resolution rejects cycles on every supported Python version;
        # Python 3.13 non-strict resolution can silently return a cyclic path.
        # Dangling internal links remain legitimate source content.
        destination = target.joinpath(*PurePosixPath(name).parts)
        try:
            resolved = destination.resolve(strict=True)
        except FileNotFoundError:
            resolved = destination.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError("unsafe build context link") from exc
        if not resolved.is_relative_to(target.resolve()):
            raise ValueError("build context link escapes root")
    for path, mode in reversed(directories):
        path.chmod(mode)


def source_policy(hosts) -> dict:
    # DENY any registry authority outside the exact operator allowlist. Local
    # sources are supplied solely by buildctl's two explicit --local bindings.
    allowed = "|".join(re.escape(host) for host in hosts)
    return {
        "version": 1,
        "rules": [
            {"action": "DENY", "selector": {"identifier": "*"}},
            {"action": "ALLOW", "selector": {"identifier": "local://*"}},
            {
                "action": "ALLOW",
                "selector": {"identifier": f"^docker-image://({allowed})/[^\\s]+$", "match_type": "REGEX"},
            },
        ],
    }


def _worker_limits(root: Path) -> None:
    # The canonical single-job worker container already has operator-set
    # memory/CPU/PID ceilings. Do not move its processes or enable controllers
    # on a shared parent merely to install a second, conflicting set of limits.
    memory = (root / "memory.max").read_text().strip()
    pids = (root / "pids.max").read_text().strip()
    cpu = (root / "cpu.max").read_text().split()
    if (
        not memory.isdecimal()
        or int(memory) <= 0
        or not pids.isdecimal()
        or int(pids) <= 0
        or len(cpu) != 2
        or any(not value.isdecimal() or int(value) <= 0 for value in cpu)
    ):
        raise RuntimeError("finite worker memory CPU and PID limits required")


def _cgroup(directory: Path, root: Path) -> tuple[Path, dict]:
    if not root.is_absolute() or root.resolve(strict=True) != root or not (root / "cgroup.controllers").is_file():
        raise RuntimeError("delegated cgroup v2 root required")
    if not root.is_relative_to(Path("/sys/fs/cgroup")) or Path("/proc/self/cgroup").read_text().strip() != "0::/":
        raise RuntimeError("private worker cgroup namespace required")
    _worker_limits(root)
    name = "palimpsest-image-" + uuid.uuid4().hex
    group = root / name
    # Intent precedes creation. An interrupted mkdir is reconciled by this exact
    # random basename, never by enumerating or killing a shared subtree.
    record = {"root": str(root), "root_inode": root.stat().st_ino, "name": name, "inode": None}
    _save(directory / "image-resources.json", record)
    group.mkdir(mode=0o700)
    record["inode"] = group.stat().st_ino
    _save(directory / "image-resources.json", record)
    if not (group / "cgroup.kill").exists():
        raise RuntimeError("kernel cgroup.kill support required")
    return group, record


def cleanup_image(directory: Path) -> None:
    # A daemon may have setsid descendants retaining the private mount namespace.
    # Fence those before cleanup_build attempts any exact owned loop detach.
    marker = directory / "image-resources.json"
    if not marker.exists():
        cleanup_build(directory)
        return
    record = json.loads(marker.read_text())
    root = Path(record["root"])
    if (
        not re.fullmatch(r"palimpsest-image-[0-9a-f]{32}", record["name"])
        or not root.is_relative_to(Path("/sys/fs/cgroup"))
        or root.resolve(strict=True) != root
        or root.stat().st_ino != record["root_inode"]
    ):
        raise RuntimeError("invalid image cgroup ledger")
    group = root / record["name"]
    try:
        info = group.lstat()
    except FileNotFoundError:
        cleanup_build(directory)
        marker.unlink()
        return
    if not stat.S_ISDIR(info.st_mode) or (record["inode"] is not None and info.st_ino != record["inode"]):
        raise RuntimeError("image cgroup ownership changed")
    (group / "cgroup.kill").write_text("1")
    deadline = time.monotonic() + 15
    while "populated 1" in (group / "cgroup.events").read_text():
        if time.monotonic() >= deadline:
            raise RuntimeError("image cgroup cleanup uncertain")
        time.sleep(0.05)
    # runc creates nested cgroups; remove deepest only after kernel emptiness.
    for child in sorted(group.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            child.rmdir()
    group.rmdir()
    cleanup_build(directory)
    marker.unlink()


def _private_log(process, path: Path) -> threading.Thread:
    # Open on the host-side job directory, never inside disposable mounted
    # scratch. Persist each bounded tail so a killed executor still leaves it.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)

    def drain():
        tail = bytearray()
        with os.fdopen(fd, "wb", buffering=0) as output, process.stderr:
            for chunk in iter(lambda: process.stderr.read1(8192), b""):
                tail.extend(chunk)
                del tail[:-LOG_TAIL_BYTES]
                output.seek(0)
                output.write(tail)
                output.truncate()

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return thread


def _overlay_command(argv: list[str], deadline: float, log_directory: Path) -> None:
    """Retain actual mount-tool failures outside the disposable scratch."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("overlayfs probe deadline expired")
    tool = argv[0]
    parent = os.getpid()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=SAFE_ENV,
        close_fds=True,
        preexec_fn=lambda: _child_limits(parent, LOG_TAIL_BYTES),
        umask=0o077,
    )
    drain = None
    timed_out = False
    try:
        drain = _private_log(process, log_directory / f"overlayfs-{tool}.log")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            raise
        if process.returncode != 0:
            raise RuntimeError("overlayfs probe command failed") from subprocess.CalledProcessError(
                process.returncode, tool
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if drain is not None:
            drain.join(timeout=5)
        _save(log_directory / f"overlayfs-{tool}.json", {"returncode": process.returncode, "timed_out": timed_out})
        if drain is not None and drain.is_alive():
            raise RuntimeError("overlayfs probe log cleanup uncertain")
        if process.returncode != 0 or timed_out:
            # The parent retains buildkitd.log before disposing this directory,
            # not the probe sidecars. No daemon has started on this path. Keep
            # the first failure if a subsequent cleanup command also fails.
            status = (
                json.dumps(
                    {
                        "stage": "overlay_snapshotter",
                        "tool": tool,
                        "returncode": process.returncode,
                        "timed_out": timed_out,
                    }
                ).encode("ascii")
                + b"\n"
            )
            tail = (log_directory / f"overlayfs-{tool}.log").read_bytes() if drain is not None else b""
            try:
                fd = os.open(
                    log_directory / "buildkitd.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as retained:
                    retained.write(status + tail[-(LOG_TAIL_BYTES - len(status)) :])
                    retained.flush()
                    os.fsync(retained.fileno())


def _require_overlayfs(directory: Path, deadline: float, log_directory: Path) -> None:
    """Fail closed unless this job's ext4 supports real overlay copy-up/whiteouts."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("BuildKit overlayfs requires privileged root Linux execution")
    for tool in ("buildkitd", "buildctl", "mount", "umount", "findmnt"):
        if shutil.which(tool) is None:
            raise RuntimeError("BuildKit overlayfs execution tool unavailable")
    if not os.access("/usr/bin/buildkit-runc", os.X_OK):
        raise RuntimeError("BuildKit overlayfs runtime unavailable")
    filesystem = _command(["findmnt", "-n", "-o", "FSTYPE", "-T", str(directory)], deadline, capture=True).strip()
    if filesystem != b"ext4":
        raise RuntimeError("BuildKit overlayfs requires the quota-bound ext4 scratch")
    probe = directory / "overlayfs-probe"
    probe.mkdir(mode=0o700)
    lower, upper, work, merged = (probe / name for name in ("lower", "upper", "work", "merged"))
    for path in (lower, upper, work, merged):
        path.mkdir(mode=0o700)
    (lower / "copy-up").write_bytes(b"lower")
    (lower / "deleted").write_bytes(b"lower")
    mounted = False
    try:
        # An actual mount checks kernel/module, CAP_SYS_ADMIN, LSM and backing
        # filesystem support together; /proc/filesystems alone proves none of it.
        _overlay_command(
            [
                "mount",
                "-t",
                "overlay",
                "-o",
                f"lowerdir={lower},upperdir={upper},workdir={work}",
                "overlay",
                str(merged),
            ],
            deadline,
            log_directory,
        )
        mounted = True
        if (merged / "copy-up").read_bytes() != b"lower":
            raise RuntimeError("overlayfs lower layer unavailable")
        (merged / "copy-up").write_bytes(b"upper")
        (merged / "deleted").unlink()
        if (
            (upper / "copy-up").read_bytes() != b"upper"
            or (lower / "copy-up").read_bytes() != b"lower"
            or (merged / "deleted").exists()
            or (lower / "deleted").read_bytes() != b"lower"
        ):
            raise RuntimeError("overlayfs copy-up or whiteout unavailable")
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("BuildKit overlayfs snapshotter precondition failed; native fallback prohibited") from exc
    finally:
        # Never recurse into a mounted overlay on failure. Interrupted mounts
        # belong to the private namespace; the existing cleanup fence owns it.
        if mounted or os.path.ismount(merged):
            try:
                _overlay_command(["umount", str(merged)], time.monotonic() + 15, log_directory)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("BuildKit overlayfs probe cleanup uncertain") from exc
        shutil.rmtree(probe)


def _buildkit(manifest: dict, directory: Path, deadline: float, log_directory: Path) -> Path:
    # Native snapshots recursively copy the accumulated root at each layer.
    # Require overlayfs, never auto/native fallback or a larger job deadline.
    _require_overlayfs(directory, deadline, log_directory)
    context = directory / "context"
    descriptor = manifest.get("context")
    if descriptor is not None:
        archive = Path(manifest["_context_archive"])
        if archive.stat().st_size != descriptor["size_bytes"] or _hash(archive) != descriptor["digest"]:
            raise ValueError("context descriptor mismatch")
        extract_context(
            archive,
            context,
            maximum_bytes=manifest["max_context_expanded_bytes"],
            maximum_members=manifest["max_context_members"],
        )
    else:
        context.mkdir(mode=0o700)
    definition = directory / "definition"
    definition.mkdir(mode=0o700)
    # Client-supplied context cannot replace the admitted Dockerfile.
    (definition / "Dockerfile").write_text(manifest["request"]["dockerfile"])
    policy = directory / "source-policy.json"
    _save(policy, source_policy(manifest["registry_hosts"]))
    group = Path(manifest["_execution_cgroup"])
    config = directory / "buildkitd.toml"
    cgroup_parent = "/" + str(group.relative_to(Path("/sys/fs/cgroup")))
    config.write_text(
        "[worker.oci]\ndefaultCgroupParent = "
        + json.dumps(cgroup_parent)
        + "\nmax-parallelism = 2\n[cdi]\ndisabled = true\n"
        + '[frontend."gateway.v0"]\nenabled = false\n'
    )
    # /run is a per-executor private tmpfs. Persistent job roots can exceed
    # AF_UNIX's 104-byte socket limit before BuildKit even starts.
    socket = Path("/run/palimpsest-buildkit.sock")
    root = directory / "buildkit"
    root.mkdir(mode=0o700)
    parent = os.getpid()

    def child_setup():
        _child_limits(parent, manifest["max_output_bytes"])
        (group / "cgroup.procs").write_text(str(os.getpid()))

    # Built-in Dockerfile frontend only: no arbitrary gateway/frontend image.
    daemon = subprocess.Popen(
        [
            "buildkitd",
            "--config",
            str(config),
            "--root",
            str(root),
            "--addr",
            "unix://" + str(socket),
            "--oci-worker=true",
            "--containerd-worker=false",
            "--oci-worker-snapshotter=overlayfs",
            "--oci-worker-binary=/usr/bin/buildkit-runc",
            "--oci-worker-net=bridge",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=SAFE_ENV,
        close_fds=True,
        preexec_fn=child_setup,
        umask=0o077,
    )
    daemon_log = _private_log(daemon, log_directory / "buildkitd.log")
    try:
        while not socket.exists():
            if daemon.poll() is not None:
                raise RuntimeError("BuildKit unavailable")
            if time.monotonic() >= deadline:
                raise TimeoutError("image build deadline expired")
            time.sleep(0.05)
        output = directory / "result.oci.tar"
        argv = [
            "buildctl",
            "--addr",
            "unix://" + str(socket),
            "build",
            "--frontend",
            "dockerfile.v0",
            "--opt",
            "platform=linux/amd64",
            "--opt",
            "force-network-mode=" + manifest["run_network"],
            "--opt",
            "no-cache=",
            "--opt",
            "cmdline=",
            "--local",
            "context=" + str(context),
            "--local",
            "dockerfile=" + str(definition),
            "--source-policy-file",
            str(policy),
            "--output",
            "type=oci,dest=" + str(output) + ",oci-mediatypes=true",
        ]
        request = manifest["request"]
        if request.get("target"):
            argv += ["--opt", "target=" + request["target"]]
        for key, value in request.get("build_args", {}).items():
            if key.startswith("BUILDKIT_"):
                raise ValueError("reserved build argument")
            argv += ["--opt", "build-arg:" + key + "=" + value]
        client = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=SAFE_ENV,
            close_fds=True,
            umask=0o077,
        )
        client_log = _private_log(client, log_directory / "buildctl.log")
        try:
            client.wait(timeout=max(0.001, deadline - time.monotonic()))
            if client.returncode != 0:
                raise RuntimeError("Dockerfile build failed")
        finally:
            if client.poll() is None:
                client.kill()
                client.wait(timeout=5)
            client_log.join(timeout=5)
            if client_log.is_alive():
                raise RuntimeError("build client cleanup uncertain")
        if output.stat().st_size > manifest["max_output_bytes"]:
            raise ValueError("OCI output exceeds limit")
        return output
    finally:
        if daemon.poll() is None:
            daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)
        daemon_log.join(timeout=5)
        if daemon_log.is_alive():
            raise RuntimeError("build daemon cleanup uncertain")
        # Kernel cgroup fence is performed by the distinct cleanup helper; until
        # then no output is publishable, including on successful daemon exit.


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def materialized_result(materialized: dict, directory: Path, maximum: int) -> dict:
    receipt = materialized["receipt"]
    paths = materialized["blob_paths"]
    if not 0 < len(paths) <= MAX_LAYERS or len(paths) != len(receipt["results"]):
        raise ValueError("invalid materialized layer count")
    config_descriptor = materialized["image"]["config_descriptor"]
    # Use the retained path returned by LayerCache rather than a second CAS layout.
    config_path = next(
        Path(path)
        for path in materialized["source_blob_paths"]
        if Path(path).name == config_descriptor["digest"].split(":")[1]
    )
    if _hash(config_path) != config_descriptor["digest"] or config_path.stat().st_size != config_descriptor["size"]:
        raise ValueError("config descriptor mismatch")
    config = strict_json_object(config_path.read_bytes(), "verified image config").get("config") or {}
    result = {
        "layers": [],
        "source": {
            "root_digest": receipt["root_descriptor"]["digest"],
            "manifest_digest": receipt["manifest_digest"],
            "config_digest": config_descriptor["digest"],
        },
        "runtime": {
            "entrypoint": config.get("Entrypoint") or [],
            "cmd": config.get("Cmd") or [],
            "env": config.get("Env") or [],
            "working_dir": config.get("WorkingDir") or "/",
            "user": config.get("User") or "0",
        },
    }
    total = 0
    for ordinal, source in enumerate(paths):
        path = Path(source)
        size = path.stat().st_size
        total += size
        if size <= 0 or total > maximum:
            raise ValueError("converted output exceeds limit")
        digest = _hash(path)
        canonical = receipt["results"][ordinal]["receipt"]
        if canonical["image_digest"] != digest or canonical["image_size"] != size or canonical["ordinal"] != ordinal:
            raise ValueError("materialized receipt does not match output")
        # Write only into the preallocated private output reserve.
        fd = os.open(directory / "outputs.pack", os.O_RDWR | os.O_NOFOLLOW)
        with path.open("rb") as incoming, os.fdopen(fd, "r+b") as outgoing:
            info = os.fstat(outgoing.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != maximum:
                raise ValueError("invalid output reserve")
            outgoing.seek(total - size)
            remaining = size
            while remaining:
                chunk = incoming.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("truncated materialized output")
                outgoing.write(chunk)
                remaining -= len(chunk)
            if incoming.read(1):
                raise ValueError("materialized output size changed")
            outgoing.flush()
            os.fsync(outgoing.fileno())
        result["layers"].append({"digest": digest, "size_bytes": size})
    return result


def run_image(manifest: dict, directory: Path) -> dict:
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64") or os.geteuid() != 0:
        raise RuntimeError("image execution requires root Linux amd64 service VM")
    if manifest["platform"] != "linux/amd64" or manifest["run_network"] not in ("none", "default"):
        raise ValueError("unsupported image execution policy")
    # Acquisition and materialization intentionally create independent sessions.
    # Fence the ENTIRE credential-free execution before any tool can spawn;
    # setsid cannot escape cgroup membership. The HTTPS worker and separate
    # cleanup helper stay outside, so they can kill/reap this exact subtree.
    group, _ = _cgroup(directory, Path(manifest["cgroup_root"]))
    (group / "cgroup.procs").write_text(str(os.getpid()))
    manifest["_execution_cgroup"] = str(group)
    capacity = manifest["scratch_bytes"]
    # Setup must allocate/format the full image, not merely one output file.
    resource.setrlimit(resource.RLIMIT_FSIZE, (capacity, capacity))
    _private_mounts()
    deadline = time.monotonic() + manifest["timeout_seconds"]
    with _scratch(directory, capacity, manifest["max_output_bytes"], deadline) as (scratch, maximum, executor):
        manifest = {**manifest, "max_output_bytes": maximum, "_context_archive": str(directory / "context.tar")}
        with _temporary_root(scratch):
            # /run is separately RAM-bounded. Payload/cache/CNI/temp use ext4.
            executor.mounts.append(Path("/run"))
            executor.command(["mount", "-t", "tmpfs", "-o", "mode=0755,size=16m", "tmpfs", "/run"])
            cni_state = scratch / "cni-state"
            cni_state.mkdir(mode=0o700)
            cni_target = Path("/var/lib/cni")
            cni_target.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Intent before bind; reverse teardown unmounts this before ext4.
            executor.mounts.append(cni_target)
            executor.command(["mount", "--bind", str(cni_state), str(cni_target)])
            root = scratch / "cache"
            root.mkdir(mode=0o700)
            cache = LayerCache(root, initialize=True)
            if manifest["kind"] == "registry":
                reference = normalize_reference(manifest["request"]["reference"], manifest["registry_hosts"])
                imported = cache.pull(reference, timeout_seconds=max(0.001, deadline - time.monotonic()))
            elif manifest["kind"] == "dockerfile":
                archive = _buildkit(manifest, scratch, deadline, directory)
                imported = cache.import_source(archive)
            else:
                raise ValueError("invalid image job kind")
            materialized = cache.materialize(
                imported["root_digest"], timeout_seconds=max(0.001, deadline - time.monotonic())
            )
            result = materialized_result(materialized, directory, maximum)
    _save(directory / "result.json", result)
    return result


def preflight_image_host(directory: Path, cgroup_root: Path) -> None:
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64") or os.geteuid() != 0:
        raise RuntimeError("image execution requires root Linux amd64 service VM")
    for tool in (
        "buildctl",
        "buildkitd",
        "buildkit-runc",
        "skopeo",
        "mksquashfs",
        "mke2fs",
        "losetup",
        "mount",
        "umount",
        "findmnt",
    ):
        if shutil.which(tool) is None:
            raise RuntimeError("required image execution tool unavailable")
    _cgroup(directory, cgroup_root)
    cleanup_image(directory)


def _failure_summary(exc: Exception, stage: str) -> str:
    # Never stringify exceptions, argv, paths, recipes or custom class names.
    # This credential-free child receives no worker key; private stderr conveys
    # only a fixed stage/category and numeric OS/tool status to its parent.
    category = "Exception"
    stages = {
        "_cgroup": "cgroup",
        "_private_mounts": "mount_namespace",
        "_scratch": "scratch",
        "_allocate": "scratch_allocation",
        "_temporary_root": "temporary_storage",
        "_buildkit": "buildkit",
        "_require_overlayfs": "overlay_snapshotter",
        "extract_context": "context_extraction",
        "normalize_reference": "registry_reference",
        "materialized_result": "output_packing",
    }
    number = None
    seen = set()
    cause = exc
    for _ in range(16):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        for kind in (TimeoutError, subprocess.TimeoutExpired, OSError, ValueError, RuntimeError):
            if isinstance(cause, kind):
                category = kind.__name__
                break
        if isinstance(cause, OSError) and type(cause.errno) is int:
            number = cause.errno
        trace = cause.__traceback__
        for _ in range(128):
            if trace is None:
                break
            # Only our own code objects can assign a diagnostic stage.
            frame = trace.tb_frame
            name = frame.f_code.co_name
            function = globals().get(name)
            function = getattr(function, "__wrapped__", function)
            if name in stages and getattr(function, "__code__", None) is frame.f_code:
                stage = stages[name]
            trace = trace.tb_next
        cause = cause.__cause__ or cause.__context__
    return json.dumps(
        {
            "stage": stage,
            "category": category,
            "errno": number,
        }
    )


def main() -> None:
    stage = "arguments"
    try:
        if len(sys.argv) == 5 and sys.argv[1] == "preflight":
            directory = Path(sys.argv[3]).resolve(strict=True)
            stage = "preflight"
            marker = _mark_helper(directory, int(sys.argv[2]))
            preflight_image_host(directory, Path(sys.argv[4]))
            marker.unlink()
            return
        if len(sys.argv) == 4 and sys.argv[1] == "cleanup":
            directory = Path(sys.argv[2]).resolve(strict=True)
            stage = "cleanup"
            _fence_helper(int(sys.argv[3]))
            _release_journal_reserve(directory)
            marker = _mark_helper(directory, int(sys.argv[3]))
            cleanup_image(directory)
            marker.unlink()
            return
        if len(sys.argv) != 3:
            raise ValueError("expected private manifest and parent")
        path = Path(sys.argv[1])
        stage = "manifest"
        _mark_builder(path.parent, int(sys.argv[2]))
        manifest = _load_manifest(path)
        stage = "execution"
        run_image(manifest, path.parent)
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        print(_failure_summary(exc, stage), file=sys.stderr)
        raise SystemExit(124) from None
    except Exception as exc:
        print(_failure_summary(exc, stage), file=sys.stderr)
        raise SystemExit(1) from None
    print('{"result_file":"result.json"}')


if __name__ == "__main__":
    main()
