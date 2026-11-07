"""Explicit, custody-preserving transport to an operator-selected Linux Lima worker.

Wire v1: uint32 big-endian canonical-JSON length + JSON, followed only where
announced by exact binary byte counts. Source blobs and SquashFS images stream;
raw disks use ordered (offset,length) JSON extent headers followed by bytes.
Neither endpoint accepts the other endpoint's CAS, occurrence or lease identity.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import selectors
import stat
import struct
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import ArtifactValidationError
from .web_job_client import (
    defer_preparation_termination,
    defer_web_cancellation,
    no_force_preparation_termination,
    require_preparation_custody_owner,
    retain_preparation_custody,
)

SCHEMA = "palimpsest.vz-worker.v1"
CHUNK = 1024 * 1024
MAX_JSON = CHUNK
MAX_PRIVATE_STDERR = 64 * 1024
MAX_SOURCE = 8 * 1024**3
MAX_PAYLOAD = 32 * 1024**3
MAX_DISK = 1024 * 1024**3
MAX_PHYSICAL = 2 * 1024**3
_HELD = []


class VZWorkerHold(ArtifactValidationError):
    """Request/actor custody is retained; operator reconciliation is required."""

    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self._diagnostics = dict(diagnostics) if diagnostics is not None else None

    def private_diagnostics(self):
        """Return JSON-safe private child evidence; never put it in public messages."""
        return dict(self._diagnostics) if self._diagnostics is not None else None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ArtifactValidationError("duplicate worker JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, UnicodeError):
        raise ArtifactValidationError("invalid worker JSON") from None
    if not isinstance(value, dict):
        raise ArtifactValidationError("worker JSON must be an object")
    return value


def _exact(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ArtifactValidationError("worker object fields differ from contract")
    return value


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or ".." in Path(value).parts:
        raise ArtifactValidationError("worker paths must be explicit absolute paths")
    return Path(value)


def _directory(path, *, private=True):
    """Open every ancestor without following links, retaining the selected directory."""
    path = _path(str(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        metadata = os.fstat(fd)
        if private and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ArtifactValidationError("worker directory must be owned and mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(path, *, private=False, executable=False):
    parent = _directory(path.parent, private=False)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (private and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600))
        or (executable and (metadata.st_mode & 0o022 or not metadata.st_mode & 0o111))
    ):
        os.close(fd)
        raise ArtifactValidationError("worker file ownership, links or mode is unsafe")
    return fd


def _signature(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _digest_file(path, expected=None, *, executable=False):
    fd = _file(path, executable=executable)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while chunk := os.read(fd, CHUNK):
            digest.update(chunk)
        if _signature(before) != _signature(os.fstat(fd)) or _signature(before) != _signature(path.lstat()):
            raise ArtifactValidationError("worker file changed while hashing")
        actual = "sha256:" + digest.hexdigest()
        if expected is not None and expected != actual:
            raise ArtifactValidationError("worker executable digest differs")
        return actual
    finally:
        os.close(fd)


def package_fingerprint(root: Path) -> str:
    """Hash the exact source tree; bytecode is never used by the isolated actor."""
    result = []

    def visit(directory, relative):
        fd = _directory(directory, private=False)
        try:
            for name in sorted(os.listdir(fd)):
                item = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if name == "__pycache__":
                    cache = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        opened = os.fstat(cache)
                        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if (
                            not stat.S_ISDIR(item.st_mode)
                            or opened.st_uid != os.geteuid()
                            or opened.st_mode & 0o022
                            or (item.st_dev, item.st_ino) != (opened.st_dev, opened.st_ino)
                            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                        ):
                            raise ArtifactValidationError("worker bytecode cache directory is unsafe")
                    finally:
                        os.close(cache)
                    continue
                path = directory / name
                label = relative + name
                if stat.S_ISDIR(item.st_mode):
                    visit(path, label + "/")
                elif stat.S_ISREG(item.st_mode) and item.st_nlink == 1 and not item.st_mode & 0o022:
                    result.append([label, _digest_file(path)])
                else:
                    raise ArtifactValidationError("worker source tree has unsafe entries")
        finally:
            os.close(fd)

    visit(root, "")
    return "sha256:" + hashlib.sha256(_json({"files": result})).hexdigest()


# Executed by the selected interpreter before importing any first-party module.
# -I -S and a fresh bytecode prefix exclude user site, environment and old caches.
_BOOTSTRAP = r"""
import hashlib,importlib.util,json,os,runpy,stat,sys
root,expected,nonce=sys.argv[1:]
sys.dont_write_bytecode=True
sys.pycache_prefix='/nonexistent-palimpsest-vz-'+nonce
if os.path.lexists(sys.pycache_prefix): raise RuntimeError('worker bytecode prefix already exists')
items=[]
def signature(s):
 return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_uid,s.st_mode,s.st_nlink)
def visit(directory,relative):
 fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  for name in sorted(os.listdir(fd)):
   s=os.stat(name,dir_fd=fd,follow_symlinks=False)
   if name=='__pycache__':
    c=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
    try:
     o=os.fstat(c); v=os.stat(name,dir_fd=fd,follow_symlinks=False)
     if not stat.S_ISDIR(s.st_mode) or o.st_uid!=os.geteuid() or o.st_mode&0o022 or (s.st_dev,s.st_ino)!=(o.st_dev,o.st_ino) or (v.st_dev,v.st_ino)!=(o.st_dev,o.st_ino): raise RuntimeError('unsafe bytecode cache')
    finally: os.close(c)
    continue
   p=directory+'/'+name
   if stat.S_ISDIR(s.st_mode): visit(p,relative+name+'/')
   else:
    if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_mode&0o022: raise RuntimeError('unsafe source')
    f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
    try:
     before=signature(os.fstat(f))
     if before!=signature(s): raise RuntimeError('changed source before read')
     h=hashlib.sha256()
     while True:
      b=os.read(f,1048576)
      if not b: break
      h.update(b)
     if before!=signature(os.fstat(f)) or before!=signature(os.stat(name,dir_fd=fd,follow_symlinks=False)): raise RuntimeError('changed source')
    finally: os.close(f)
    items.append([relative+name,'sha256:'+h.hexdigest()])
 finally: os.close(fd)
visit(root,'')
actual='sha256:'+hashlib.sha256(json.dumps({'files':items},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
if actual!=expected: raise RuntimeError('worker source fingerprint differs')
spec=importlib.util.spec_from_file_location('palimpsest_local',root+'/__init__.py',submodule_search_locations=[root])
if spec is None or spec.loader is None: raise RuntimeError('selected worker package cannot be loaded')
package=importlib.util.module_from_spec(spec)
sys.modules['palimpsest_local']=package
spec.loader.exec_module(package)
sys.argv=['palimpsest_local.vz_worker_server']
runpy.run_module('palimpsest_local.vz_worker_server',run_name='__main__')
"""


class _Transport:
    def __init__(self, process, deadline):
        self.process, self.deadline = process, deadline
        self.pending = bytearray()
        self.errors = 0
        self.stderr_tail = bytearray()
        self.stdout_eof = self.stderr_eof = False
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)

    def _capture_stderr(self):
        data = os.read(self.process.stderr.fileno(), MAX_PRIVATE_STDERR)
        if not data:
            self.stderr_eof = True
            return False
        self.errors += len(data)
        self.stderr_tail.extend(data)
        del self.stderr_tail[:-MAX_PRIVATE_STDERR]
        return True

    def private_diagnostics(self):
        """Snapshot already available evidence without waiting, signalling or replay."""
        while not self.stderr_eof and self.errors <= MAX_JSON:
            try:
                if not self._capture_stderr():
                    break
            except (OSError, ValueError):
                break
        return {
            "pid": self.process.pid,
            "returncode": self.process.poll(),
            "stderr_tail_base64": base64.b64encode(self.stderr_tail).decode("ascii"),
            "stderr_bytes_observed": self.errors,
            "stderr_truncated": self.errors > len(self.stderr_tail),
            "stderr_eof": self.stderr_eof,
        }

    def _hold(self, message):
        return VZWorkerHold(message, diagnostics=self.private_diagnostics())

    def _wait(self, writing=False, *, allow_eof=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self._hold("worker deadline expired; actor and request retained without signalling")
        if self.stdout_eof and not allow_eof:
            raise self._hold("worker transport ended before verified completion")
        with selectors.DefaultSelector() as selector:
            if not self.stderr_eof:
                selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
            if not self.stdout_eof:
                selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            if writing:
                selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
            for key, _ in selector.select(min(remaining, 1.0)):
                if key.data == "stderr":
                    self._capture_stderr()
                    if self.errors > MAX_JSON:
                        raise self._hold("worker stderr exceeded bound; custody retained")
                elif key.data == "stdout":
                    data = os.read(key.fd, 65536)
                    if not data:
                        self.stdout_eof = True
                        if not allow_eof:
                            raise self._hold("worker transport ended before verified completion")
                    self.pending.extend(data)
                    if len(self.pending) > 2 * CHUNK:
                        raise self._hold("unsolicited worker output exceeded bound")

    def write(self, payload):
        view = memoryview(payload)
        while view:
            if time.monotonic() >= self.deadline:
                raise self._hold("worker write deadline expired; custody retained")
            try:
                count = os.write(self.process.stdin.fileno(), view)
                view = view[count:]
            except BlockingIOError:
                self._wait(writing=True)
            except OSError:
                raise self._hold("worker transport write failed; custody retained") from None

    def read(self, count):
        while len(self.pending) < count:
            self._wait()
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def send(self, value):
        payload = _json(value)
        if len(payload) > MAX_JSON:
            raise ArtifactValidationError("worker request metadata exceeds bound")
        self.write(struct.pack(">I", len(payload)))
        self.write(payload)

    def receive(self):
        (length,) = struct.unpack(">I", self.read(4))
        if not 0 < length <= MAX_JSON:
            raise self._hold("worker frame length exceeds bound")
        return _decode(self.read(length))

    def finish(self):
        self.process.stdin.close()
        while self.process.poll() is None:
            if time.monotonic() >= self.deadline:
                raise self._hold("worker exit is uncertain; custody retained")
            self._wait(allow_eof=True)
        self.private_diagnostics()
        if self.errors > MAX_JSON:
            raise self._hold("worker stderr exceeded bound; custody retained")
        if self.process.returncode != 0 or self.pending:
            raise self._hold("worker completion or trailing bytes are invalid")
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            raise self._hold("worker preparation group settlement is unknown") from None
        else:
            raise self._hold("worker preparation group still has a live descendant")
        # Read EOF after reaping, including bytes not yet observed by the selector.
        for name, stream in (("stdout_eof", self.process.stdout), ("stderr_eof", self.process.stderr)):
            extra = stream.read(CHUNK)
            if extra != b"":
                raise self._hold("worker emitted trailing output or retained a live output pipe")
            setattr(self, name, True)
            stream.close()


@dataclass(frozen=True)
class VZWorker:
    config: dict
    config_path: Path
    config_signature: tuple

    @classmethod
    def from_environment(cls):
        selected = os.environ.get("PALIMPSEST_VZ_WORKER_CONFIG")
        if not selected:
            raise ArtifactValidationError("PALIMPSEST_VZ_WORKER_CONFIG must select an explicit owned worker")
        path = _path(selected)
        fd = _file(path, private=True)
        try:
            metadata = os.fstat(fd)
            payload = os.read(fd, MAX_JSON + 1)
            if len(payload) > MAX_JSON or _signature(metadata) != _signature(os.fstat(fd)):
                raise ArtifactValidationError("worker config exceeds bound or changed")
        finally:
            os.close(fd)
        config = _decode(payload)
        _exact(config, {"schema", "lima_home", "instance", "limactl", "guest", "custody_root"})
        if config["schema"] != SCHEMA:
            raise ArtifactValidationError("worker config schema is invalid")
        _exact(config["instance"], {"name", "directory_device", "directory_inode", "arch", "vm_type"})
        instance = config["instance"]
        if (
            not isinstance(instance["name"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", instance["name"])
            or instance["arch"] != "aarch64"
            or instance["vm_type"] != "vz"
            or any(type(instance[k]) is not int or instance[k] < 0 for k in ("directory_device", "directory_inode"))
        ):
            raise ArtifactValidationError("worker instance binding is invalid")
        _exact(config["limactl"], {"path", "sha256"})
        guest = _exact(
            config["guest"],
            {
                "python",
                "python_sha256",
                "package_root",
                "package_fingerprint",
                "packer",
                "packer_sha256",
                "toolchain",
                "dropbearkey",
                "dropbearkey_sha256",
                "mkfs_ext4",
                "mkfs_ext4_sha256",
                "debugfs",
                "debugfs_sha256",
                "scratch_root",
            },
        )
        _exact(guest["toolchain"], {"version", "executable_digest", "dependency_digests"})
        for key in ("python", "package_root", "packer", "dropbearkey", "mkfs_ext4", "debugfs", "scratch_root"):
            _path(guest[key])
        for value in (
            config["limactl"]["sha256"],
            guest["python_sha256"],
            guest["package_fingerprint"],
            guest["packer_sha256"],
            guest["dropbearkey_sha256"],
            guest["mkfs_ext4_sha256"],
            guest["debugfs_sha256"],
        ):
            if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ArtifactValidationError("worker fingerprint is invalid")
        from .oci_packer import SquashFSToolchainIdentity

        identity = SquashFSToolchainIdentity(
            guest["toolchain"]["version"],
            guest["toolchain"]["executable_digest"],
            tuple(guest["toolchain"]["dependency_digests"]),
        )
        if identity.executable_digest != guest["packer_sha256"]:
            raise ArtifactValidationError("worker packer identity is inconsistent")
        for key in ("lima_home", "custody_root"):
            os.close(_directory(_path(config[key])))
        _digest_file(_path(config["limactl"]["path"]), config["limactl"]["sha256"], executable=True)
        return cls(config, path, _signature(metadata))

    def _check_instance(self):
        if _signature(self.config_path.lstat()) != self.config_signature:
            raise VZWorkerHold("worker config identity changed")
        _digest_file(_path(self.config["limactl"]["path"]), self.config["limactl"]["sha256"], executable=True)
        home = _path(self.config["lima_home"])
        instance = self.config["instance"]
        fd = _directory(home / instance["name"])
        try:
            metadata = os.fstat(fd)
            if (metadata.st_dev, metadata.st_ino) != (instance["directory_device"], instance["directory_inode"]):
                raise VZWorkerHold("selected worker instance directory identity changed")
        finally:
            os.close(fd)

    def _environment(self):
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": self.config["lima_home"],
            "LANG": "C",
            "LC_ALL": "C",
            "LIMA_HOME": self.config["lima_home"],
        }

    def _spawn(self, arguments):
        return subprocess.Popen(
            [self.config["limactl"]["path"], *arguments],
            env=self._environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    @contextmanager
    def transaction(self, operation, *, timeout_seconds=300.0):
        """Keep the non-daemon controller alive until settlement or HOLD reconciliation."""
        with no_force_preparation_termination():
            with self._transaction(operation, timeout_seconds=timeout_seconds) as transaction:
                yield transaction

    @contextmanager
    def _transaction(self, operation, *, timeout_seconds=300.0):
        require_preparation_custody_owner()
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise ArtifactValidationError("worker timeout must be finite and between 0 and 3600 seconds")
        deadline = time.monotonic() + timeout_seconds
        root = _path(self.config["custody_root"])
        rootfd = _directory(root)
        lockfd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=rootfd)
        process = None
        transport = None
        active = False
        try:
            metadata = os.fstat(lockfd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise VZWorkerHold("unsafe worker custody lock")
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if "active.json" in os.listdir(rootfd):
                raise VZWorkerHold("previous worker request retains custody; no retry permitted")
            self._check_instance()
            nonce = uuid.uuid4().hex
            fd = os.open("active.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=rootfd)
            try:
                os.write(
                    fd,
                    _json({"schema": SCHEMA, "nonce": nonce, "operation": operation, "config": str(self.config_path)}),
                )
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(rootfd)
            active = True
            with defer_web_cancellation(), defer_preparation_termination():
                process = self._spawn(["list", "--json", self.config["instance"]["name"]])
            transport = _Transport(process, deadline)
            # Lima list --json is newline-delimited, not this module's framed protocol.
            output = bytearray()
            while process.poll() is None or not (transport.stdout_eof and transport.stderr_eof):
                transport._wait(allow_eof=True)
                output.extend(transport.pending)
                transport.pending.clear()
                if len(output) > MAX_JSON:
                    raise VZWorkerHold("Lima readonly instance output exceeds bound")
            output.extend(transport.pending)
            transport.pending.clear()
            extra = process.stdout.read()
            if extra:
                output.extend(extra)
            if process.returncode or len(output) > MAX_JSON:
                raise VZWorkerHold("cannot establish selected Lima worker Running state")
            record = _decode(bytes(output).strip())
            instance = self.config["instance"]
            if (
                record.get("name") != instance["name"]
                or record.get("status") != "Running"
                or record.get("arch") != instance["arch"]
                or record.get("vmType") != instance["vm_type"]
                or record.get("dir") != str(_path(self.config["lima_home"]) / instance["name"])
            ):
                raise VZWorkerHold("actual Lima worker identity or Running state differs")
            self._check_instance()
            transport.finish()
            guest = self.config["guest"]
            # The interpreter itself is operator-pinned; bootstrap validates its package before import.
            bootstrap = (
                "import hashlib,os,sys; p=sys.argv[1]; f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); h=hashlib.sha256(); "
            )
            bootstrap += "exec('while True:\\n b=os.read(f,1048576)\\n if not b: break\\n h.update(b)'); "
            bootstrap += "assert 'sha256:'+h.hexdigest()==sys.argv[2]; os.close(f); "
            bootstrap += "sys.argv=sys.argv[2:]; exec(" + repr(_BOOTSTRAP) + ")"
            # _BOOTSTRAP receives package root, fingerprint, nonce as argv[1:].
            with defer_web_cancellation(), defer_preparation_termination():
                process = self._spawn(
                    [
                        "shell",
                        "--workdir=/",
                        instance["name"],
                        "--",
                        guest["python"],
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        bootstrap,
                        guest["python"],
                        guest["python_sha256"],
                        guest["package_root"],
                        guest["package_fingerprint"],
                        nonce,
                    ]
                )
            transport = _Transport(process, deadline)
            transport.send(
                {
                    "schema": SCHEMA,
                    "nonce": nonce,
                    "operation": operation,
                    "guest": guest,
                    "timeout_seconds": timeout_seconds,
                }
            )
            yield transport, nonce
            self._check_instance()
            transport.send({"ack": nonce})
            if transport.receive() != {"cleaned": nonce}:
                raise VZWorkerHold("worker staging cleanup was not proven")
            transport.finish()
            os.unlink("active.json", dir_fd=rootfd)
            os.fsync(rootfd)
            active = False
        except BaseException as error:
            with defer_web_cancellation(), defer_preparation_termination():
                if active:
                    diagnostics = transport.private_diagnostics() if transport is not None else None
                    if process is not None:
                        _HELD.append((process, rootfd, lockfd))
                        settled = False
                        try:
                            if process.poll() is not None:
                                try:
                                    os.killpg(process.pid, 0)
                                except ProcessLookupError:
                                    settled = (
                                        transport is not None
                                        and transport.process is process
                                        and transport.stdout_eof
                                        and transport.stderr_eof
                                    )
                        except BaseException:
                            pass
                        if not settled:
                            retain_preparation_custody(process, rootfd, lockfd)
                        rootfd = lockfd = -1
                    raise VZWorkerHold(
                        "worker request retained in active.json; reconcile exact actor/staging before reuse",
                        diagnostics=diagnostics,
                    ) from error
                raise
        finally:
            if lockfd >= 0:
                os.close(lockfd)
            if rootfd >= 0:
                os.close(rootfd)

    def prepare_root_disk(
        self, output: Path, *, size_bytes: int, filesystem_uuid: str, label: str, binding: dict
    ) -> str:
        require_preparation_custody_owner()
        _validate_disk_request(size_bytes, filesystem_uuid, label, binding)
        output = _path(str(output))
        parent = _directory(output.parent)
        fd = os.open(output.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        initial = os.fstat(fd)
        try:
            os.ftruncate(fd, size_bytes)
            with self.transaction("root-disk") as (transport, nonce):
                transport.send(
                    {"size_bytes": size_bytes, "filesystem_uuid": filesystem_uuid, "label": label, "binding": binding}
                )
                header = _exact(transport.receive(), {"public_key", "size_bytes", "filesystem_uuid"})
                if header["size_bytes"] != size_bytes or header["filesystem_uuid"] != filesystem_uuid:
                    raise VZWorkerHold("root disk result binding differs")
                public_key = _public_key(header["public_key"])
                end = physical = count = 0
                while True:
                    extent = transport.receive()
                    if extent == {"end": nonce}:
                        break
                    _exact(extent, {"offset", "length"})
                    offset, length = extent["offset"], extent["length"]
                    if (
                        type(offset) is not int
                        or type(length) is not int
                        or offset < end
                        or not 0 < length <= CHUNK
                        or offset + length > size_bytes
                    ):
                        raise VZWorkerHold("raw disk extent order or bound is invalid")
                    physical += length
                    count += 1
                    if physical > MAX_PHYSICAL or count > 131072:
                        raise VZWorkerHold("raw disk transfer exceeds physical/member bound")
                    payload = transport.read(length)
                    done = 0
                    while done < length:
                        done += os.pwrite(fd, payload[done:], offset + done)
                    end = offset + length
                os.fsync(fd)
                verify_ext4(fd, size_bytes, filesystem_uuid, label)
                visible = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
                current = os.fstat(fd)
                if (
                    (initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino)
                    or _signature(visible) != _signature(current)
                    or current.st_nlink != 1
                    or current.st_uid != os.geteuid()
                    or stat.S_IMODE(current.st_mode) != 0o600
                ):
                    raise VZWorkerHold("new root disk identity changed")
                os.fsync(parent)
                return public_key
        finally:
            # A partial/uncertain image is deliberately retained and never reused.
            os.close(fd)
            os.close(parent)


def _public_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", value):
        raise ArtifactValidationError("worker host public key is not canonical ed25519")
    try:
        raw = base64.b64decode(value.split()[1], validate=True)
    except ValueError:
        raise ArtifactValidationError("worker host public key encoding is invalid") from None
    if len(raw) != 51 or raw[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ArtifactValidationError("worker host public key structure is invalid")
    return value


def _validate_disk_request(size, filesystem_uuid, label, binding):
    if type(size) is not int or not 64 * 1024**2 <= size <= MAX_DISK or size % 1024**2:
        raise ArtifactValidationError("raw ext4 size is invalid")
    try:
        if str(uuid.UUID(filesystem_uuid)) != filesystem_uuid:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ArtifactValidationError("raw ext4 UUID must be canonical") from None
    from .oci_guest_filesystems import ext4_volume_label

    if label != ext4_volume_label(filesystem_uuid):
        raise ArtifactValidationError("raw ext4 label differs from canonical root volume identity")
    _exact(binding, {"schema", "run_id", "root_volume_id", "root_graph_digest", "root_nonce", "phase"})
    try:
        valid_run = str(uuid.UUID(binding["run_id"])) == binding["run_id"]
    except (ValueError, TypeError, AttributeError):
        valid_run = False
    if (
        binding["schema"] != "palimpsest.vz-upper-binding.v1"
        or binding["phase"] != "prepared"
        or not valid_run
        or binding["root_volume_id"] != filesystem_uuid
        or not isinstance(binding["root_graph_digest"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding["root_graph_digest"])
        or not isinstance(binding["root_nonce"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", binding["root_nonce"])
    ):
        raise ArtifactValidationError("raw ext4 binding marker differs from shared contract")


def verify_ext4(fd, size, filesystem_uuid, label):
    from .oci_guest_filesystems import EXT4_FEATURE_RO_COMPAT_METADATA_CSUM, verify_ext4_superblock

    identity = verify_ext4_superblock(
        os.pread(fd, 1024, 1024), device_size=size, volume_id=filesystem_uuid, filesystem_uuid=filesystem_uuid
    )
    if (
        os.fstat(fd).st_size != size
        or identity.label != label
        or not identity.feature_ro_compat & EXT4_FEATURE_RO_COMPAT_METADATA_CSUM
    ):
        raise ArtifactValidationError("worker ext4 size, canonical label or metadata checksum feature differs")


def _record(cls, value):
    _exact(value, {field.name for field in fields(cls)})
    return cls(**value)


def materialize_vz_source(request, *, roots, qualification, worker=None, timeout_seconds=300.0):
    require_preparation_custody_owner()
    import tempfile

    from .native_root_source import NativeRootSourceQualification, revalidate_native_root_source
    from .oci_changeset import OCI_CHANGESET_NORMALIZATION_ID
    from .oci_converter import DEFAULT_LAYER_CONVERSION_LIMITS, LAYER_INTAKE_POLICY_ID, LayerIntakeReceipt
    from .oci_materializer import OCIImageMaterializationReceipt
    from .oci_packer import (
        DEFAULT_SQUASHFS_PACK_POLICY,
        SQUASHFS_PACK_POLICY_ID,
        SQUASHFS_PACKER_ARGV_CONTRACT_ID,
        SQUASHFS_STRUCTURAL_VERIFIER_ID,
        LeasedSquashFS,
        PackedSquashFSReceipt,
        verify_squashfs_fd,
    )
    from .oci_run_request import LocalOCIRunRequest, PreparedLocalOCIRun
    from .oci_source import SourceCAS
    from .oci_store import DerivedLayerOccurrence, DerivedSquashFSKey, OCIStore, parent_chain_digest
    from .oci_tar_emitter import OCI_NORMALIZED_TAR_EMISSION_ID
    from .state import StatePaths

    if (
        not isinstance(request, LocalOCIRunRequest)
        or not isinstance(roots, StatePaths)
        or not isinstance(qualification, NativeRootSourceQualification)
        or request.platform != "linux/arm64"
        or qualification.profile.backend != "lima-vz"
        or qualification.profile.platform != request.platform
    ):
        raise ArtifactValidationError("VZ conversion requires typed arm64 source qualification")
    source = qualification.snapshot
    if request.manifest_digest is not None and request.manifest_digest != source.root.descriptor.digest:
        raise ArtifactValidationError("request root pin differs from qualified original source")
    cas = SourceCAS.open_existing(roots.oci_source_cas, expected_cas_id=source.cas_id)
    revalidate_native_root_source(qualification, cas)
    cas.verify_image(source)
    if not 0 < len(source.layers) <= 128:
        raise ArtifactValidationError("VZ source layer count exceeds bound")
    worker = worker or VZWorker.from_environment()
    store = OCIStore(roots)
    results = []
    with worker.transaction("materialize", timeout_seconds=timeout_seconds) as (transport, nonce):
        members = []
        seen = set()
        for snapshot in (source.root, source.manifest, source.config, *source.layers):
            if snapshot.descriptor.digest not in seen:
                members.append(snapshot)
                seen.add(snapshot.descriptor.digest)
        if sum(item.descriptor.size for item in members) > MAX_SOURCE:
            raise ArtifactValidationError("VZ original source exceeds transfer bound")
        transport.send(
            {
                "root": source.root.descriptor.to_dict(),
                "manifest": source.manifest.descriptor.to_dict(),
                "platform": request.platform,
                "members": [item.descriptor.to_dict() for item in members],
            }
        )
        layer_ordinals = {item.descriptor.digest: i for i, item in enumerate(source.layers)}
        for snapshot in members:
            ordinal = layer_ordinals.get(snapshot.descriptor.digest)
            if ordinal is None:
                transport.write(cas.read_metadata(snapshot))
            else:
                with cas.lease_layer(source, ordinal) as lease:
                    for chunk in lease.chunks():
                        transport.write(chunk)
        if transport.receive() != {
            "graph": {
                "root": source.root.descriptor.to_dict(),
                "manifest": source.manifest.descriptor.to_dict(),
                "config": source.config.descriptor.to_dict(),
                "layers": [item.descriptor.to_dict() for item in source.layers],
                "diff_ids": [item.diff_id for item in source.image.layers],
                "platform": request.platform,
                "process": source.image.config.process.to_dict(),
            }
        }:
            raise VZWorkerHold("worker original graph/config/process differs from qualified source")
        physical = 0
        for ordinal in range(len(source.layers)):
            wire = _exact(transport.receive(), {"ordinal", "key", "intake", "packed"})
            if wire["ordinal"] != ordinal:
                raise VZWorkerHold("worker changed ordered layer occurrences")
            occurrence = DerivedLayerOccurrence.from_image(source, ordinal)
            key = DerivedSquashFSKey.from_dict(wire["key"])
            identity = worker.config["guest"]["toolchain"]
            if (
                not key.matches(occurrence)
                or key.parent_chain_digest != parent_chain_digest(source, ordinal)
                or key.intake_policy_id != LAYER_INTAKE_POLICY_ID
                or key.intake_policy_fingerprint != DEFAULT_LAYER_CONVERSION_LIMITS.fingerprint
                or key.normalization_contract_id != OCI_CHANGESET_NORMALIZATION_ID
                or key.tar_emission_contract_id != OCI_NORMALIZED_TAR_EMISSION_ID
                or key.pack_policy_id != SQUASHFS_PACK_POLICY_ID
                or key.pack_policy_fingerprint != DEFAULT_SQUASHFS_PACK_POLICY.fingerprint
                or key.packer_contract_id != SQUASHFS_PACKER_ARGV_CONTRACT_ID
                or key.structural_verifier != SQUASHFS_STRUCTURAL_VERIFIER_ID
                or key.packer_version != identity["version"]
                or key.packer_executable_digest != identity["executable_digest"]
                or key.packer_dependency_digests != tuple(sorted(identity["dependency_digests"]))
            ):
                raise VZWorkerHold("worker recipe differs from host source/parent/contracts/toolchain")
            intake = _record(LayerIntakeReceipt, wire["intake"])
            packed_value = dict(wire["packed"])
            packed_value["toolchain_dependency_digests"] = tuple(packed_value["toolchain_dependency_digests"])
            packed = _record(PackedSquashFSReceipt, packed_value)
            store._validate_producer(occurrence, key, intake, packed)
            if type(packed.image_size) is not int or not 0 < packed.image_size <= MAX_PAYLOAD:
                raise VZWorkerHold("worker physical layer size exceeds bound")
            physical += packed.image_size
            if physical > MAX_PAYLOAD:
                raise VZWorkerHold("worker cumulative physical bytes exceed bound")
            with tempfile.TemporaryFile(mode="w+b", dir=roots.runtime_packs) as payload:
                remaining = packed.image_size
                digest = hashlib.sha256()
                while remaining:
                    chunk = transport.read(min(CHUNK, remaining))
                    payload.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                payload.flush()
                if "sha256:" + digest.hexdigest() != packed.image_digest:
                    raise VZWorkerHold("worker physical SquashFS digest differs")
                verify_squashfs_fd(payload.fileno(), packed.image_size, MAX_PAYLOAD)
                if os.pread(payload.fileno(), 2, 20) != b"\x06\x00":
                    raise VZWorkerHold("worker canonical packed image is not zstd-compressed")

                @contextmanager
                def producer(_payload=payload, _packed=packed, _intake=intake):
                    lease = LeasedSquashFS(_payload, _packed)
                    try:
                        yield _intake, lease
                        if not lease._verified:
                            raise VZWorkerHold("worker payload import did not reach verified EOF")
                    finally:
                        close_error = lease._close()
                        if close_error is not None:
                            raise close_error

                results.append(store.materialize_observed(occurrence, key, producer))
        if transport.receive() != {"end": nonce}:
            raise VZWorkerHold("worker source result terminator differs")
        store.validate_receipt_occurrences(
            tuple(result.receipt for result in results),
            tuple(DerivedLayerOccurrence.from_image(source, i) for i in range(len(source.layers))),
        )
    receipt = OCIImageMaterializationReceipt(
        source.binding_digest,
        source.image.digest,
        source.root.descriptor,
        source.image.manifest_descriptor.digest,
        source.config.descriptor,
        source.image.platform.os,
        source.image.platform.architecture,
        tuple(item.descriptor for item in source.layers),
        tuple(item.diff_id for item in source.image.layers),
        source.image.config.process,
        tuple(results),
    )
    return PreparedLocalOCIRun(request, receipt, cas, source.config)
