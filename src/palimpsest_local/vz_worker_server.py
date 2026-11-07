"""One-shot Linux endpoint for the explicit VZ worker bridge; never a VM manager.

Run only through the configured isolated-interpreter bootstrap. A durable active
marker fences the selected scratch root. EOF, deadline or unknown actor cleanup
retains it and the exact request directory; neither endpoint retries or kills.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import math
import os
import selectors
import shutil
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

from .errors import ArtifactValidationError
from .vz_worker import (
    CHUNK,
    MAX_JSON,
    MAX_PAYLOAD,
    MAX_PHYSICAL,
    MAX_SOURCE,
    SCHEMA,
    VZWorkerHold,
    _decode,
    _digest_file,
    _directory,
    _exact,
    _file,
    _json,
    _path,
    _public_key,
    _signature,
    _validate_disk_request,
    package_fingerprint,
    verify_ext4,
)
from .web_job_client import (
    defer_preparation_termination,
    no_force_preparation_termination,
    park_preparation_custody,
    preparation_custody_held,
    retain_preparation_custody,
    run_preparation_tool,
)


class _Channel:
    def __init__(self, deadline):
        self.deadline = deadline
        os.set_blocking(0, False)
        os.set_blocking(1, False)

    def _wait(self, fd, event):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise VZWorkerHold("worker deadline expired; staging/actor custody retained")
        with selectors.DefaultSelector() as selector:
            selector.register(fd, event)
            selector.select(min(remaining, 1.0))

    def read(self, length):
        result = bytearray()
        while len(result) < length:
            self._wait(0, selectors.EVENT_READ)
            try:
                part = os.read(0, min(CHUNK, length - len(result)))
            except BlockingIOError:
                continue
            if not part:
                raise VZWorkerHold("controller channel ended; request retained")
            result.extend(part)
        return bytes(result)

    def write(self, payload):
        view = memoryview(payload)
        while view:
            self._wait(1, selectors.EVENT_WRITE)
            try:
                count = os.write(1, view)
            except BlockingIOError:
                continue
            view = view[count:]

    def send(self, value):
        payload = _json(value)
        if not 0 < len(payload) <= MAX_JSON:
            raise VZWorkerHold("worker metadata exceeds bound")
        self.write(struct.pack(">I", len(payload)))
        self.write(payload)

    def receive(self):
        (length,) = struct.unpack(">I", self.read(4))
        if not 0 < length <= MAX_JSON:
            raise VZWorkerHold("controller frame exceeds bound")
        return _decode(self.read(length))


def _new_file(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def _command(arguments, deadline, *, pass_fds=()):
    """Run one exact offline preparation group with bounded TERM-only cleanup."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VZWorkerHold("offline tool deadline expired before launch")
    result = run_preparation_tool(
        arguments,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=remaining,
        check=False,
        no_force=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
        pass_fds=pass_fds,
    )
    if result.returncode:
        raise VZWorkerHold("offline selected tool failed; staging retained")
    return result.stdout


def _verify_static_dropbear(path):
    fd = _file(path, executable=True)
    try:
        header = os.pread(fd, 64, 0)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 183:
            raise ArtifactValidationError("dropbearkey must be an ARM64 little-endian ELF64 executable")
        (offset,) = struct.unpack_from("<Q", header, 32)
        entry_size, count = struct.unpack_from("<HH", header, 54)
        if entry_size != 56 or not 0 < count <= 128 or offset + count * entry_size > os.fstat(fd).st_size:
            raise ArtifactValidationError("dropbearkey ELF program table is invalid")
        for ordinal in range(count):
            record = os.pread(fd, entry_size, offset + ordinal * entry_size)
            if struct.unpack_from("<I", record)[0] in (2, 3):
                raise ArtifactValidationError("dropbearkey must be static without PT_DYNAMIC/PT_INTERP")
    finally:
        os.close(fd)


def _root_disk(channel, stage, guest, nonce):
    import re

    from .project_volumes import OCI_ROOT_EXT4_EXTENDED_OPTIONS, OCI_ROOT_EXT4_FEATURES

    request = _exact(channel.receive(), {"size_bytes", "filesystem_uuid", "label", "binding"})
    size, filesystem_uuid, label = request["size_bytes"], request["filesystem_uuid"], request["label"]
    _validate_disk_request(size, filesystem_uuid, label, request["binding"])
    for key in ("dropbearkey", "mkfs_ext4", "debugfs"):
        _digest_file(_path(guest[key]), guest[key + "_sha256"], executable=True)
    _verify_static_dropbear(_path(guest["dropbearkey"]))
    staging = stage / "filesystem"
    staging.mkdir(mode=0o700)
    platform = staging / "platform-state"
    platform.mkdir(mode=0o700)
    marker = platform / "binding.json"
    _new_file(marker, _json(request["binding"]))
    key = platform / "ssh_host_ed25519_key"
    result = _command([guest["dropbearkey"], "-t", "ed25519", "-f", str(key)], channel.deadline)
    fd = _file(key)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.geteuid() or not 0 < metadata.st_size <= 16384:
            raise VZWorkerHold("generated private host key is unsafe")
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    lines = [line.strip() for line in result.decode("ascii").splitlines() if line.startswith("ssh-ed25519 ")]
    if len(lines) != 1:
        raise VZWorkerHold("dropbearkey did not return one host public key")
    public_key = _public_key(" ".join(lines[0].split()[:2]))
    # Pin generated public material independently from the selected private key.
    check = _command([guest["dropbearkey"], "-y", "-f", str(key)], channel.deadline)
    if not any(" ".join(line.split()[:2]) == public_key for line in check.decode("ascii").splitlines()):
        raise VZWorkerHold("generated key public identity differs")
    image = stage / "root.raw"
    disk = os.open(image, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        initial = os.fstat(disk)
        os.ftruncate(disk, size)
        # No privileged mount: e2fsprogs imports the private marker/key tree.
        _command(
            [
                guest["mkfs_ext4"],
                "-q",
                "-F",
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
                "-U",
                filesystem_uuid,
                "-L",
                label,
                "-E",
                OCI_ROOT_EXT4_EXTENDED_OPTIONS + ",root_owner=0:0",
                "-d",
                str(staging),
                str(image),
            ],
            channel.deadline,
        )
        # mkfs -d preserves the unprivileged producer's imported UIDs. Change
        # only these new offline image inodes, never the host staging tree.
        inodes = (
            ("/", "directory", 0o700),
            ("/platform-state", "directory", 0o700),
            ("/platform-state/binding.json", "regular", 0o600),
            ("/platform-state/ssh_host_ed25519_key", "regular", 0o600),
        )
        commands = stage / "root-inode-fields"
        lines = []
        for path, kind, mode in inodes:
            inode_mode = (0o40000 if kind == "directory" else 0o100000) | mode
            lines.extend(
                (
                    f"set_inode_field {path} uid 0",
                    f"set_inode_field {path} gid 0",
                    f"set_inode_field {path} mode 0{inode_mode:o}",
                )
            )
        _new_file(commands, ("\n".join(lines) + "\n").encode("ascii"))
        image_fd_path = f"/proc/self/fd/{disk}"
        _command(
            [guest["debugfs"], "-w", "-f", str(commands), image_fd_path],
            channel.deadline,
            pass_fds=(disk,),
        )
        for path, kind, mode in inodes:
            observed = _command(
                [guest["debugfs"], "-R", f"stat {path}", image_fd_path],
                channel.deadline,
                pass_fds=(disk,),
            )
            typed_mode = re.search(rb"\bType:\s+(directory|regular)\s+Mode:\s+([0-7]+)\b", observed)
            if (
                typed_mode is None
                or typed_mode[1].decode("ascii") != kind
                or int(typed_mode[2], 8) != mode
                or re.search(rb"\bUser:\s+0\s+Group:\s+0\b", observed) is None
            ):
                raise VZWorkerHold("offline root platform inode ownership/mode differs")
        os.fsync(disk)
        verify_ext4(disk, size, filesystem_uuid, label)
        if (initial.st_dev, initial.st_ino) != (os.fstat(disk).st_dev, os.fstat(disk).st_ino) or _signature(
            os.fstat(disk)
        ) != _signature(image.lstat()):
            raise VZWorkerHold("offline raw disk identity changed")
        channel.send({"public_key": public_key, "size_bytes": size, "filesystem_uuid": filesystem_uuid})
        position = physical = count = 0
        before = _signature(os.fstat(disk))
        while position < size:
            if time.monotonic() >= channel.deadline:
                raise VZWorkerHold("disk transfer deadline expired")
            try:
                start = os.lseek(disk, position, os.SEEK_DATA)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    break
                raise VZWorkerHold("worker filesystem must support sparse SEEK_DATA/SEEK_HOLE") from error
            end = min(os.lseek(disk, start, os.SEEK_HOLE), size)
            if start < position or end <= start or end > size:
                raise VZWorkerHold("worker sparse extent map is invalid")
            while start < end:
                payload = os.pread(disk, min(CHUNK, end - start), start)
                if not payload:
                    raise VZWorkerHold("worker raw extent was truncated")
                # Filesystems can allocate unwritten blocks; never send multi-GiB zeros.
                if any(payload):
                    physical += len(payload)
                    count += 1
                    if physical > MAX_PHYSICAL or count > 131072:
                        raise VZWorkerHold("worker raw extents exceed physical/member bound")
                    channel.send({"offset": start, "length": len(payload)})
                    channel.write(payload)
                start += len(payload)
            position = end
        if before != _signature(os.fstat(disk)) or before != _signature(image.lstat()):
            raise VZWorkerHold("worker raw image changed during transfer")
        channel.send({"end": nonce})
    finally:
        with defer_preparation_termination():
            if preparation_custody_held():
                retain_preparation_custody(disk)
            else:
                os.close(disk)


def _descriptor(value):
    from .oci_image import Descriptor

    _exact(value, {"mediaType", "digest", "size"})
    return Descriptor(media_type=value["mediaType"], digest=value["digest"], size=value["size"])


def _materialize(channel, stage, guest, nonce):
    from .artifact_store import ArtifactStore
    from .oci_materializer import materialize_layer_hard
    from .oci_packer import discover_squashfs_toolchain
    from .oci_source import LocalLayoutSource, SourceCAS
    from .oci_store import OCIStore
    from .state import StatePaths

    request = _exact(channel.receive(), {"root", "manifest", "platform", "members"})
    if (
        request["platform"] != "linux/arm64"
        or not isinstance(request["members"], list)
        or not 0 < len(request["members"]) <= 131
    ):
        raise ArtifactValidationError("worker original graph platform/member count is invalid")
    root, manifest = _descriptor(request["root"]), _descriptor(request["manifest"])
    descriptors = tuple(_descriptor(value) for value in request["members"])
    if (
        root not in descriptors
        or manifest not in descriptors
        or len({value.digest for value in descriptors}) != len(descriptors)
        or sum(value.size for value in descriptors) > MAX_SOURCE
    ):
        raise ArtifactValidationError("worker original graph descriptor set is invalid")
    layout = stage / "layout"
    layout.mkdir(mode=0o700)
    (layout / "blobs").mkdir(mode=0o700)
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(mode=0o700)
    for descriptor in descriptors:
        if descriptor.size < 0 or descriptor.size > MAX_SOURCE:
            raise ArtifactValidationError("worker source member size exceeds bound")
        target = blobs / descriptor.digest.removeprefix("sha256:")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            digest = hashlib.sha256()
            remaining = descriptor.size
            while remaining:
                payload = channel.read(min(CHUNK, remaining))
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view) :]
                digest.update(payload)
                remaining -= len(payload)
            if "sha256:" + digest.hexdigest() != descriptor.digest:
                raise VZWorkerHold("worker received source bytes with a different original digest")
            os.fsync(fd)
        finally:
            os.close(fd)
    _new_file(layout / "oci-layout", _json({"imageLayoutVersion": "1.0.0"}))
    _new_file(layout / "index.json", _json({"schemaVersion": 2, "manifests": [root.to_dict()]}))
    config = stage / "config"
    state = stage / "state"
    config.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (state / "runtime-packs").mkdir(mode=0o700)
    roots = StatePaths(config, state)
    cas = SourceCAS(roots.oci_source_cas)
    remaining = channel.deadline - time.monotonic()
    source = LocalLayoutSource(layout, root.digest).snapshot_acquisition(
        None, cas, platform="linux/arm64", timeout_seconds=remaining, maximum_bytes=MAX_SOURCE
    )
    if source.manifest.descriptor != manifest or source.root.descriptor != root or not 0 < len(source.layers) <= 128:
        raise VZWorkerHold("worker replay selected a different original graph")
    allowed = {item.descriptor for item in (source.root, source.manifest, source.config, *source.layers)}
    if set(descriptors) != allowed:
        raise VZWorkerHold("worker source graph has missing or extraneous members")
    _digest_file(_path(guest["packer"]), guest["packer_sha256"], executable=True)
    toolchain = discover_squashfs_toolchain(
        _path(guest["packer"]),
        expected_packer_sha256=guest["packer_sha256"].removeprefix("sha256:"),
        deadline=channel.deadline,
        no_force=True,
    )
    identity = guest["toolchain"]
    if (
        toolchain.identity.version != identity["version"]
        or toolchain.identity.executable_digest != identity["executable_digest"]
        or toolchain.identity.dependency_digests != tuple(sorted(identity["dependency_digests"]))
    ):
        raise VZWorkerHold("worker discovered toolchain differs from pinned identity")
    channel.send(
        {
            "graph": {
                "root": source.root.descriptor.to_dict(),
                "manifest": source.manifest.descriptor.to_dict(),
                "config": source.config.descriptor.to_dict(),
                "layers": [item.descriptor.to_dict() for item in source.layers],
                "diff_ids": [item.diff_id for item in source.image.layers],
                "platform": "linux/arm64",
                "process": source.image.config.process.to_dict(),
            }
        }
    )
    store = OCIStore(roots)
    artifacts = ArtifactStore(roots.store)
    physical = 0
    for ordinal in range(len(source.layers)):
        remaining = channel.deadline - time.monotonic()
        if remaining <= 0:
            raise VZWorkerHold("worker conversion deadline expired")
        result = materialize_layer_hard(
            source,
            ordinal,
            source_cas_root=roots.oci_source_cas,
            roots=roots,
            store=store,
            packer_path=_path(guest["packer"]),
            toolchain=toolchain,
            timeout_seconds=remaining,
            no_force=True,
        )
        record = store.read_producer_record(result.receipt)
        physical += result.receipt.image_size
        if physical > MAX_PAYLOAD:
            raise VZWorkerHold("worker cumulative converted payload exceeds bound")
        channel.send(
            {
                "ordinal": ordinal,
                "key": record["key"],
                "intake": record["intake_receipt"],
                "packed": record["packed_receipt"],
            }
        )
        with artifacts.open_squashfs(
            result.receipt.image_digest, result.receipt.image_size, maximum=MAX_PAYLOAD
        ) as payload:
            while True:
                chunk = payload.read(CHUNK)
                if not chunk:
                    break
                channel.write(chunk)
    channel.send({"end": nonce})


def main():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ArtifactValidationError("VZ worker requires explicitly selected unprivileged Linux UID")
    os.umask(0o077)
    channel = _Channel(time.monotonic() + 30)
    request = _exact(channel.receive(), {"schema", "nonce", "operation", "guest", "timeout_seconds"})
    import re

    if (
        request["schema"] != SCHEMA
        or not isinstance(request["nonce"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", request["nonce"])
        or request["operation"] not in {"root-disk", "materialize"}
        or type(request["timeout_seconds"]) not in (int, float)
        or not math.isfinite(request["timeout_seconds"])
        or not 0 < request["timeout_seconds"] <= 3600
    ):
        raise ArtifactValidationError("worker request envelope is invalid")
    with no_force_preparation_termination():
        _run_request(channel, request)


def _run_request(channel, request):
    channel.deadline = time.monotonic() + request["timeout_seconds"]
    nonce = request["nonce"]
    guest = request["guest"]
    package = Path(__file__).resolve().parent
    if str(package) != guest["package_root"] or package_fingerprint(package) != guest["package_fingerprint"]:
        raise ArtifactValidationError("selected worker source package differs")
    _digest_file(_path(guest["python"]), guest["python_sha256"], executable=True)
    scratch = _path(guest["scratch_root"])
    rootfd = _directory(scratch)
    lockfd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=rootfd)
    try:
        lock = os.fstat(lockfd)
        if (
            not stat.S_ISREG(lock.st_mode)
            or lock.st_uid != os.geteuid()
            or lock.st_nlink != 1
            or stat.S_IMODE(lock.st_mode) != 0o600
        ):
            raise VZWorkerHold("unsafe worker scratch lock")
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if "active.json" in os.listdir(rootfd):
            raise VZWorkerHold("previous worker actor/staging retains custody")
        _new_file(
            scratch / "active.json",
            _json({"schema": SCHEMA, "nonce": nonce, "pid": os.getpid(), "operation": request["operation"]}),
        )
        os.fsync(rootfd)
        os.mkdir(nonce, 0o700, dir_fd=rootfd)
        stage = scratch / nonce
        stagefd = _directory(stage)
        initial = os.fstat(stagefd)
        try:
            if request["operation"] == "root-disk":
                _root_disk(channel, stage, guest, nonce)
            else:
                _materialize(channel, stage, guest, nonce)
            if channel.receive() != {"ack": nonce}:
                raise VZWorkerHold("controller did not verify transferred output")
            if _signature(initial)[:2] != _signature(os.stat(nonce, dir_fd=rootfd, follow_symlinks=False))[:2]:
                raise VZWorkerHold("worker staging directory identity changed")
            # Canonical hard workers return only after settled cleanup; unknown cleanup
            # raises and deliberately never reaches this exact-owned removal.
            shutil.rmtree(nonce, dir_fd=rootfd)
            os.unlink("active.json", dir_fd=rootfd)
            os.fsync(rootfd)
            channel.send({"cleaned": nonce})
        except BaseException:
            with defer_preparation_termination():
                if preparation_custody_held():
                    retain_preparation_custody(stagefd, rootfd, lockfd)
                    park_preparation_custody()
                raise
        finally:
            os.close(stagefd)
    finally:
        os.close(lockfd)
        os.close(rootfd)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # Deliberately no traceback containing source filenames, credentials or
        # arbitrary image strings on the transport. Durable custody remains.
        os.write(2, b"VZ worker HOLD: request/actor/staging retained; exact operator reconciliation required\n")
        raise SystemExit(1) from None
