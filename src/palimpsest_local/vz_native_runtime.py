"""Owned, no-force Lima VZ transport for an actual OCI PID1 root.

The private ledger is authority, not a readiness receipt. Every provider effect is
preceded by reopening source/store/export/disk/config authority. Uncertain actors
retain a durable HOLD. Exact stopped observations can settle ordinary-stop HOLD;
failed-start cleanup remains separately authorized and cannot adopt a running VM.
No host service, carrier userspace, SSH trust-on-first-use, or NAT fallback is used.
Actor-producing calls require a main/non-daemon lifetime owner. Session owners
must remain alive through close or HOLD; safe daemon-thread shutdown is not claimed.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import termios
import threading
import time
import uuid
import zlib
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import platforms
from .darwin_process import DarwinProcessIdentity, list_processes, read_process
from .errors import LifecycleError, RuntimeBusyError, StateError
from .native_root_source import qualify_native_root_source, revalidate_native_root_source
from .oci_exec_control import validate_exec_request
from .oci_exec_session import effective_exec_timeout_ms
from .oci_image import OCIImageRef, resolve_archive_image
from .oci_network import OCINetworkConfig
from .oci_os_release import read_image_os_release
from .oci_process import OCIProcessSpec, image_process_vectors
from .oci_provenance import Descriptor
from .oci_run_adapter import OCILaunchResult
from .oci_run_request import LocalOCIRunRequest
from .oci_source import LocalArchiveSource, LocalLayoutSource, SnapshottedOCIArchive, SourceCAS, SourceSnapshot
from .oci_store import ArtifactLeaseOwner, OCIStore
from .refs import HostDirectoryShare
from .runtime_types import (
    ExecRequest,
    ExistingRunRecord,
    LifecycleCursor,
    LifecycleResult,
    ProcessCapabilities,
    ProcessCapabilityError,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessStatusEvent,
    ProcessStream,
    RuntimeOperation,
)
from .state import (
    RUN_OBSERVATION_TIMEOUT_SECONDS,
    RunObservationWaitBudget,
    StatePaths,
    init_resolved_roots,
    locked_existing_run,
    reserve_new_run,
    validate_tag,
)
from .vz_lower_exports import (
    lima_vz_mount_tag,
    load_vz_lower_export,
    publish_vz_lower_export,
    release_vz_lower_export,
    verify_vz_lower_export,
)
from .vz_root_volume import claim_vz_root_volume, load_vz_root_volume, release_vz_root_volume
from .vz_runtime_assets import MAX_METADATA_BYTES, PLATFORM_ABI, load_vz_runtime_assets
from .vz_worker import VZWorker, materialize_vz_source
from .web_job_client import (
    defer_preparation_termination,
    defer_web_cancellation,
    no_force_preparation_termination,
    park_preparation_custody,
    preparation_custody_held,
    require_preparation_custody_owner,
    retain_preparation_custody,
)

_SCHEMA = "palimpsest.vz-native-run.v1"
_HOST_SCHEMA = "palimpsest.vz-host.v1"
_MAX_JSON = 1024 * 1024
_DISK_LIMIT = 32 * 1024**3
_CIDATA_MAX_BYTES = 128 * 1024**2
_CIDATA_METADATA_BYTES = 32 * 1024**2
_PROXY_VARIABLES = (
    "ftp_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "FTP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)
_MARKER = "PALIMPSEST_RUN_ID"
_RESERVED = {"schema_version", "runtime_kind", "backend", "name", "run_id", "status", "lifecycle_revision"}
_HELD: list[subprocess.Popen] = []
_FAILED_START_CLEANUP = ContextVar("palimpsest_vz_failed_start_cleanup", default=None)


class VZNativeHold(LifecycleError):
    """Exact actor/resource reconciliation is required; automatic retry is forbidden."""


class _VZStartAdmissionError(StateError):
    """Known rejection before a start intent or provider operation."""


class VZCatalogRegistrationError(StateError):
    """The VM already exists; a metadata failure must not invite create replay."""

    def __init__(self, record):
        self.record = record
        super().__init__(
            f"native VZ run {record.name} ({record.run_id}) was created; catalog registration failed; do not replay create"
        )


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _decode(payload, *, maximum_bytes=_MAX_JSON):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise StateError("native VZ JSON contains duplicate fields")
            value[key] = item
        return value

    if len(payload) > maximum_bytes:
        raise StateError("native VZ JSON exceeds bound")
    try:
        return json.loads(
            payload, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise StateError("native VZ JSON is invalid") from error


def _path(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or str(Path(value)) != value
        or ".." in Path(value).parts
        or "\0" in value
    ):
        raise StateError("native VZ path must be canonical and absolute")
    return Path(value)


def _stamp(info):
    return [
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def _directory(path, *, private=True):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in _path(str(path)).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise StateError("native VZ directory must be owned and private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(path, *, private=False, executable=False, owned=False, limit=None, capture=True):
    parent = _directory(path.parent, private=False)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise StateError("native VZ file is not a single-link regular file")
        if private and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077):
            raise StateError("native VZ private file permissions are unsafe")
        if owned and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o022):
            raise StateError("native VZ owned public artifact permissions are unsafe")
        if executable and (
            not before.st_mode & 0o111 or before.st_mode & 0o022 or before.st_uid not in {0, os.geteuid()}
        ):
            raise StateError("native VZ executable permissions are unsafe")
        if limit is not None and before.st_size > limit:
            raise StateError("native VZ file exceeds bound")
        sha = hashlib.sha256()
        content = bytearray()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            if capture:
                content.extend(chunk)
        if _stamp(before) != _stamp(os.fstat(fd)) or _stamp(before) != _stamp(path.lstat()):
            raise StateError("native VZ file changed while opening")
        return bytes(content), "sha256:" + sha.hexdigest(), _stamp(before)
    finally:
        os.close(fd)


def _new_file(path, payload, mode=0o600):
    parent = _directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=parent
        )
        try:
            os.fchmod(fd, mode)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise StateError("native VZ file write was incomplete")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)


def _public_key(payload):
    try:
        parts = payload.decode().strip().split()
        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise ValueError()
        raw = base64.b64decode(parts[1], validate=True)
        if len(raw) != 51 or raw[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
            raise ValueError()
        return " ".join(parts[:2])
    except (ValueError, UnicodeError) as error:
        raise StateError("native VZ SSH public key is invalid") from error


@dataclass(frozen=True)
class VZHostConfig:
    path: Path
    digest: str
    stamp: tuple
    value: dict
    tools: dict
    tool_stamps: dict
    home_identity: tuple

    @classmethod
    def from_environment(cls):
        selected = os.environ.get("PALIMPSEST_VZ_CONFIG")
        if not selected:
            raise StateError("PALIMPSEST_VZ_CONFIG must select an explicit private native host configuration")
        return cls.load(_path(selected))

    @classmethod
    def load(cls, path):
        os.close(_directory(path.parent))
        raw, digest, stamp = _file(path, private=True, limit=_MAX_JSON)
        value = _decode(raw)
        expected = {
            "schema",
            "lima_home",
            "limactl",
            "assets",
            "carrier",
            "carrier_disk_bytes",
            "qemu_img",
            "ssh",
            "ssh_keygen",
            "expires_unix",
        }
        if not isinstance(value, dict) or set(value) != expected or value["schema"] != _HOST_SCHEMA:
            raise StateError("native VZ host configuration fields differ from contract")
        home = _path(value["lima_home"])
        homefd = _directory(home)
        try:
            home_identity = tuple(_stamp(os.fstat(homefd))[:2])
        finally:
            os.close(homefd)
        tools, stamps = {}, {}
        for name in ("limactl", "qemu_img", "ssh", "ssh_keygen"):
            reference = value[name]
            if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
                raise StateError("native VZ tools require explicit path and sha256 pins")
            resolved = _path(reference["path"]).resolve(strict=True)
            _, actual, identity = _file(resolved, executable=True, capture=False)
            if actual != reference["sha256"]:
                raise StateError("native VZ tool digest differs")
            tools[name], stamps[name] = resolved, tuple(identity)
        carrier = value["carrier"]
        if not isinstance(carrier, dict) or set(carrier) != {"path", "sha256"}:
            raise StateError("native VZ carrier requires explicit path and sha256")
        _path(carrier["path"])
        _path(value["assets"])
        if (
            type(value["carrier_disk_bytes"]) is not int
            or not 64 * 1024**2 <= value["carrier_disk_bytes"] < _DISK_LIMIT
        ):
            raise StateError("native VZ primary disk size is outside aggregate profile")
        expiry = value["expires_unix"]
        if expiry is not None and (type(expiry) is not int or expiry <= 0):
            raise StateError("native VZ fixed configuration deadline is invalid")
        return cls(path, digest, tuple(stamp), value, tools, stamps, home_identity)

    @property
    def home(self):
        return _path(self.value["lima_home"])

    def verify(self, *, allow_expired=False):
        raw, digest, stamp = _file(self.path, private=True, limit=_MAX_JSON)
        if digest != self.digest or tuple(stamp) != self.stamp:
            raise VZNativeHold("native VZ host configuration was replaced")
        if _decode(raw) != self.value:
            raise VZNativeHold("native VZ in-memory host configuration differs from pinned bytes")
        fd = _directory(self.home)
        try:
            if tuple(_stamp(os.fstat(fd))[:2]) != self.home_identity:
                raise VZNativeHold("native VZ private Lima home changed")
        finally:
            os.close(fd)
        for filename in ("default.yaml", "override.yaml", "base.yaml", "default.yml", "override.yml", "base.yml"):
            if os.path.lexists(self.home / "_config" / filename):
                raise VZNativeHold("native VZ forbids implicit Lima defaults, overrides and external bases")
        for name, path in self.tools.items():
            _, digest, stamp = _file(path, executable=True, capture=False)
            if digest != self.value[name]["sha256"] or tuple(stamp) != self.tool_stamps[name]:
                raise VZNativeHold("native VZ tool identity changed")
        expiry = self.value["expires_unix"]
        if not allow_expired and expiry is not None and time.time() >= expiry:
            raise StateError("native VZ fixed configuration deadline has expired")

    def environment(self):
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "LANG": "C",
            "LC_ALL": "C",
            "LIMA_HOME": str(self.home),
        }


def _host_authority(config):
    return {
        "config_stamp": list(config.stamp),
        "home_identity": list(config.home_identity),
        "tools": {name: str(path) for name, path in config.tools.items()},
        "tool_stamps": {name: list(stamp) for name, stamp in config.tool_stamps.items()},
    }


def _verify_host_authority(config, native):
    if (
        config.digest != native.get("host_config_digest")
        or str(config.path) != native.get("host_config_path")
        or _host_authority(config) != native.get("host_authority")
    ):
        raise VZNativeHold("native VZ historical host configuration, home or tool binding changed")


def _bound_host_config(native):
    """Reopen original captured tool paths/identities, never resolve/re-pin anew."""
    try:
        pinned = native["host_authority"]
        names = {"limactl", "qemu_img", "ssh", "ssh_keygen"}
        if (
            not isinstance(pinned, dict)
            or set(pinned) != {"config_stamp", "home_identity", "tools", "tool_stamps"}
            or not isinstance(pinned["tools"], dict)
            or not isinstance(pinned["tool_stamps"], dict)
            or set(pinned["tools"]) != names
            or set(pinned["tool_stamps"]) != names
            or len(pinned["config_stamp"]) != 8
            or len(pinned["home_identity"]) != 2
            or any(len(stamp) != 8 for stamp in pinned["tool_stamps"].values())
            or any(
                type(item) is not int
                for stamp in (pinned["config_stamp"], pinned["home_identity"], *pinned["tool_stamps"].values())
                for item in stamp
            )
        ):
            raise StateError("native VZ historical host authority is malformed")
        path = _path(native["host_config_path"])
        raw, digest, stamp = _file(path, private=True, limit=_MAX_JSON)
        if digest != native["host_config_digest"] or stamp != pinned["config_stamp"]:
            raise VZNativeHold("native VZ original host configuration file changed")
        config = VZHostConfig(
            path,
            digest,
            tuple(pinned["config_stamp"]),
            _decode(raw),
            {name: _path(value) for name, value in pinned["tools"].items()},
            {name: tuple(value) for name, value in pinned["tool_stamps"].items()},
            tuple(pinned["home_identity"]),
        )
        _verify_host_authority(config, native)
        config.verify(allow_expired=True)
        return config
    except (KeyError, TypeError, ValueError) as error:
        raise StateError("native VZ original host authority is unavailable; adoption is forbidden") from error


def host_config_for_run(roots, name, *, expected_record=None, _observation_budget=None):
    """Read exact retained host authority without environment/provider operations."""
    with locked_existing_run(
        roots,
        name,
        expected=expected_record,
        observation_budget=_observation_budget,
    ) as authority:
        key = authority.record.dispatch_key
        if key.runtime_kind.value != "oci-root" or key.backend.value != "lima-vz":
            raise StateError("run does not own native VZ OCI host authority")
        return _bound_host_config(authority.mutable_state().get("vz_native", {}))


def _require_native_custody_owner():
    try:
        require_preparation_custody_owner()
    except ValueError as error:
        raise StateError(
            "native VZ actors require a main/non-daemon lifetime owner; daemon-thread admission is unsupported"
        ) from error


def _resource_hold_marker(path):
    payload, digest, stamp = _file(path, private=True, limit=_MAX_JSON)
    marker = _decode(payload)
    if (
        not isinstance(marker, dict)
        or set(marker) != {"schema", "run_id", "reason"}
        or marker["schema"] != _SCHEMA
        or not isinstance(marker["run_id"], str)
        or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", marker["run_id"])
        or path.name != "hold-" + marker["run_id"] + ".json"
        or not isinstance(marker["reason"], str)
        or marker["reason"]
        not in {
            "launch-incomplete",
            "start-incomplete",
            "stop-incomplete",
            "removal-incomplete",
            "retained-authority-changed",
        }
    ):
        raise VZNativeHold("native VZ HOLD is not an exact resource-only marker")
    return marker, digest, stamp


def _verify_custody_binding(root, directory_fd, lock_fd, expected_lock):
    """Bind the acquired mutex and held directory to their current visible names."""
    try:
        visible_directory = _directory(root)
        try:
            held = os.fstat(directory_fd)
            visible = os.fstat(visible_directory)
            if (
                not stat.S_ISDIR(held.st_mode)
                or held.st_uid != os.geteuid()
                or stat.S_IMODE(held.st_mode) != 0o700
                or (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino)
            ):
                raise VZNativeHold("native VZ custody namespace changed while acquiring its lock")
        finally:
            os.close(visible_directory)
        for observed in (
            os.fstat(lock_fd),
            os.stat("lock", dir_fd=directory_fd, follow_symlinks=False),
        ):
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_nlink != 1
                or (observed.st_dev, observed.st_ino) != (expected_lock.st_dev, expected_lock.st_ino)
            ):
                raise VZNativeHold("native VZ custody lock changed while acquiring it")
    except (OSError, StateError) as error:
        raise VZNativeHold("native VZ custody binding is unavailable after acquisition") from error


@contextmanager
def _custody(
    config, *, lock_timeout=0, lock_deadline=None, observation_budget=None, cleanup_hold=None, read_only_probe=False
):
    """Fence actors; only exact cleanup/stop settlement or pinned probes pass HOLD."""
    _require_native_custody_owner()
    if lock_deadline is not None and (
        type(lock_deadline) not in (int, float) or not math.isfinite(lock_deadline) or lock_deadline < 0
    ):
        raise StateError("native VZ custody deadline must be finite and nonnegative")
    if observation_budget is not None and not isinstance(observation_budget, RunObservationWaitBudget):
        raise StateError("invalid observation wait allowance")
    config.verify(allow_expired=True)
    root = config.home / "_palimpsest-native"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    fd = _directory(root)
    lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise VZNativeHold("native VZ custody lock is unsafe")
        deadline = time.monotonic() + lock_timeout
        if lock_deadline is not None:
            deadline = min(deadline, lock_deadline)
        waited_at = None
        try:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if waited_at is None:
                        waited_at = time.monotonic()
                    remaining = deadline - time.monotonic()
                    if observation_budget is not None:
                        remaining = min(remaining, observation_budget.seconds_left(waited_at))
                    if remaining <= 0:
                        raise RuntimeBusyError("native VZ namespace operation still owns its custody lock") from None
                    time.sleep(min(0.1, remaining))
        finally:
            if observation_budget is not None and waited_at is not None:
                observation_budget.charge_wait(waited_at)
        _verify_custody_binding(root, fd, lock, info)
        if os.path.lexists(root / "active.json"):
            raise VZNativeHold(
                "previous native VZ actor retains custody; reconcile active.json before another operation"
            )
        holds = [name for name in os.listdir(fd) if name.startswith("hold-")]
        if cleanup_hold is not None:
            marker_path, marker_digest, marker_stamp = cleanup_hold
            if marker_path.parent != root or holds != [marker_path.name]:
                raise VZNativeHold("native VZ cleanup requires only its exact resource HOLD")
            _, digest, stamp = _resource_hold_marker(marker_path)
            if (digest, stamp) != (marker_digest, marker_stamp):
                raise VZNativeHold("native VZ cleanup resource HOLD changed")
        elif read_only_probe:
            for name in holds:
                _resource_hold_marker(root / name)
        elif holds:
            raise VZNativeHold("native VZ namespace retains a resource HOLD; reconciliation required")
        if any(name.endswith(".actor.json") for name in os.listdir(fd)):
            raise VZNativeHold("native VZ actor journal retains unsettled custody")
        if cleanup_hold is not None and any(name.startswith("session-") for name in os.listdir(fd)):
            raise VZNativeHold("native VZ process/session custody blocks resource-only cleanup")
        for name in os.listdir(fd):
            if not name.startswith("session-"):
                continue
            try:
                actor_fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                continue
            try:
                actor = os.fstat(actor_fd)
                if (
                    not stat.S_ISREG(actor.st_mode)
                    or actor.st_uid != os.geteuid()
                    or actor.st_nlink != 1
                    or stat.S_IMODE(actor.st_mode) != 0o600
                ):
                    raise VZNativeHold("native VZ SSH custody marker is unsafe")
                try:
                    fcntl.flock(actor_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    visible = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (visible.st_dev, visible.st_ino) != (actor.st_dev, actor.st_ino):
                    raise VZNativeHold("native VZ SSH custody marker changed")
                raise VZNativeHold("native VZ SSH actor custody survived its owner; reconciliation required")
            finally:
                os.close(actor_fd)
        yield root
    finally:
        with _actor_termination_scope(), defer_web_cancellation(), defer_preparation_termination():
            if preparation_custody_held():
                retain_preparation_custody(config, root, lock, fd)
                park_preparation_custody()
            os.close(lock)
            os.close(fd)


@contextmanager
def source_acquisition_custody(config):
    """Fence external native-source actors in the same durable host namespace."""
    if not isinstance(config, VZHostConfig):
        raise StateError("native VZ source acquisition requires a typed host configuration")
    config.verify()
    with _custody(config) as custody:
        config.verify()
        yield custody


@contextmanager
def _actor_termination_scope():
    # Python delivers asynchronous handlers on the main thread. Do not mutate
    # process-global handlers from an unrelated caller thread.
    if threading.current_thread() is threading.main_thread():
        with no_force_preparation_termination():
            yield
    else:
        yield


def _bounded(config, argv, *, custody, operation, timeout=60, input_bytes=None, allow_expired=False):
    _require_native_custody_owner()
    with _actor_termination_scope():
        return _bounded_actor(
            config,
            argv,
            custody=custody,
            operation=operation,
            timeout=timeout,
            input_bytes=input_bytes,
            allow_expired=allow_expired,
        )


def _bounded_actor(config, argv, *, custody, operation, timeout=60, input_bytes=None, allow_expired=False):
    config.verify(allow_expired=allow_expired)
    expiry = None if allow_expired else config.value["expires_unix"]
    deadline = time.monotonic() + timeout
    if expiry is not None:
        remaining = expiry - time.time()
        if remaining <= 0:
            raise StateError("native VZ fixed configuration deadline has expired")
        deadline = min(deadline, time.monotonic() + remaining)
    recovery = _FAILED_START_CLEANUP.get()
    if recovery is not None:
        deadline = min(deadline, recovery.monotonic_deadline)
        recovery.check_deadline()
    journal = custody / "active.json"
    nonce = uuid.uuid4().hex
    _new_file(
        journal,
        _json(
            {"schema": _SCHEMA, "nonce": nonce, "operation": operation, "argv0": argv[0], "started_unix": time.time()}
        ),
    )
    process = None
    actor_settled = False
    try:
        with defer_web_cancellation(), defer_preparation_termination():
            if recovery is not None:
                recovery.check_deadline()
            process = subprocess.Popen(
                argv,
                env=config.environment(),
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            # Publish the exact PID before deferred TERM/HUP can unwind ownership.
            _new_file(
                custody / (nonce + ".actor.json"), _json({"pid": process.pid, "nonce": nonce, "operation": operation})
            )
        selector = selectors.DefaultSelector()
        output = [bytearray(), bytearray()]
        for index, stream in enumerate((process.stdout, process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        pending = memoryview(input_bytes or b"")
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 2)
        try:
            while selector.get_map() or process.poll() is None:
                if (
                    time.monotonic() >= deadline
                    or expiry is not None
                    and time.time() >= expiry
                    or recovery is not None
                    and time.time() >= recovery.deadline_unix
                ):
                    if recovery is None:
                        if process.poll() is None:
                            process.terminate()
                        raise VZNativeHold("native VZ bounded actor timed out; TERM only, custody retained")
                    raise VZNativeHold("native VZ bounded actor timed out; no signals, custody retained, no replay")
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    if key.data == 2:
                        if pending:
                            try:
                                pending = pending[os.write(key.fd, pending[:65536]) :]
                            except BlockingIOError:
                                continue
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    else:
                        output[key.data].extend(chunk)
                        if len(output[key.data]) > 8 * _MAX_JSON:
                            if process.poll() is None and _FAILED_START_CLEANUP.get() is None:
                                process.terminate()
                            raise VZNativeHold("native VZ actor output exceeded bound; custody retained")
            result = (process.wait(), bytes(output[0]), bytes(output[1]))
        finally:
            selector.close()
        # Lima2.2.1 BackgroundSysProcAttr.Setpgid gives the intended provider
        # hostagent its own group, not this bounded CLI actor's group.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise VZNativeHold("native VZ actor process-group settlement is unknown") from error
        else:
            raise VZNativeHold("native VZ actor left live process-group descendants; custody retained")
        actor_settled = True
        journal.unlink()
        (custody / (nonce + ".actor.json")).unlink()
        fd = _directory(custody)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return result
    except BaseException as failure:
        with defer_web_cancellation(), defer_preparation_termination():
            if process is not None and not actor_settled:
                _HELD.append(process)
                retain_preparation_custody(process, custody, journal)
            elif process is None and isinstance(failure, OSError):
                # Popen has reaped a pre-exec failure. Never remove a different
                # retained request merely because its filename is active.json.
                try:
                    pending = _decode(_file(journal, private=True, limit=_MAX_JSON)[0])
                    if pending.get("schema") == _SCHEMA and pending.get("nonce") == nonce:
                        journal.unlink()
                        parent = _directory(custody)
                        try:
                            os.fsync(parent)
                        finally:
                            os.close(parent)
                except (OSError, StateError):
                    pass
        raise


def _lima(config, args, custody, *, timeout=60, allow_expired=False):
    code, stdout, _stderr = _bounded(
        config,
        [str(config.tools["limactl"]), *args],
        custody=custody,
        operation="lima:" + args[0],
        timeout=timeout,
        allow_expired=allow_expired,
    )
    if code:
        raise VZNativeHold("native VZ Lima operation failed; exact resources retained, no retry")
    return stdout


def preflight_pinned_lima(
    config=None,
    *,
    custody=None,
    timeout_seconds=30,
    network=None,
    allow_expired=False,
    custody_timeout=0,
    _observation_budget=None,
):
    """Qualify the native host/tool/network through its pinned no-force actor.

    A supplied custody Path means the caller already holds the canonical source
    acquisition context, before publishing its own phase active.json.
    Independent callers fail fast unless an observation explicitly opts into
    bounded custody acquisition; provider commands are never replayed.
    """
    selected = VZHostConfig.from_environment() if config is None else config
    if not isinstance(selected, VZHostConfig):
        raise StateError("native VZ pinned preflight requires a typed host configuration")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        raise StateError("native VZ preflight timeout must be finite and within 300 seconds")
    if type(allow_expired) is not bool:
        raise StateError("native VZ preflight expiry policy must be a boolean")
    if (
        type(custody_timeout) not in (int, float)
        or not math.isfinite(custody_timeout)
        or not 0 <= custody_timeout <= RUN_OBSERVATION_TIMEOUT_SECONDS
    ):
        raise StateError("native VZ preflight custody timeout must be finite and within the observation bound")
    selected.verify(allow_expired=allow_expired)
    if platforms.select_backend("aarch64", requested="lima-vz", runtime_kind="oci-root") != "lima-vz":
        raise StateError("native VZ host qualification failed")
    if network is not None:
        if isinstance(network, OCINetworkConfig):
            network.__post_init__()
            if any(port.guest_port == 22 for port in network.published_ports):
                raise StateError(
                    "native VZ guest port 22 is reserved for Lima bootstrap/control; publishing it is unsupported"
                )
            mode = network.mode
        elif type(network) is str:
            mode = network
        else:
            raise StateError("native VZ networking must use canonical OCI policy")
        if mode != "nat":
            raise StateError("native VZ first profile requires usernet NAT for genuine Lima bootstrap")

    def probe(owned):
        version = (
            _lima(selected, ["--version"], owned, timeout=timeout_seconds, allow_expired=allow_expired).decode().strip()
        )
        if version not in {"limactl version 2.2.1", "limactl version v2.2.1"}:
            raise StateError("native VZ requires exact Lima 2.2.1")

    if custody is None:
        # Observation callers may wait for the existing exclusive owner without
        # replaying the probe or bypassing any post-acquisition custody guard.
        with _custody(
            selected,
            lock_timeout=min(timeout_seconds, custody_timeout),
            observation_budget=_observation_budget,
            read_only_probe=True,
        ) as owned:
            selected.verify(allow_expired=allow_expired)
            probe(owned)
    else:
        owned = _path(str(custody))
        if owned != selected.home / "_palimpsest-native":
            raise StateError("native VZ preflight custody belongs to another namespace")
        os.close(_directory(owned))
        probe(owned)
    return selected


def _observation_timeout(deadline):
    if deadline is None:
        return 60
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VZNativeHold("native VZ orderly stop observation deadline expired; no force issued")
    return min(60, remaining)


def _config_json(config, path, custody, *, allow_expired=False, private=True, deadline=None):
    # Only caller-pinned generated or persisted bytes enter Lima's embedding parser.
    _, digest, stamp = _file(path, private=private, owned=True, limit=_MAX_JSON)
    raw = _lima(
        config,
        ["template", "yq", str(path), "to_json(0)"],
        custody,
        allow_expired=allow_expired,
        timeout=_observation_timeout(deadline),
    )
    _, after_digest, after_stamp = _file(path, private=private, owned=True, limit=_MAX_JSON)
    if (digest, stamp) != (after_digest, after_stamp):
        raise VZNativeHold("native VZ configuration changed during default-filled inspection")
    value = _decode(raw)
    if not isinstance(value, dict):
        raise VZNativeHold("native VZ default-filled JSON is not an object")
    return value


def _instances(config, custody, *, allow_expired=False, deadline=None):
    payload = _lima(
        config,
        ["list", "--json"],
        custody,
        allow_expired=allow_expired,
        timeout=_observation_timeout(deadline),
    )
    values = [_decode(line) for line in payload.splitlines() if line.strip()]
    if len(values) == 1 and isinstance(values[0], list):
        values = values[0]
    if any(not isinstance(value, dict) or not isinstance(value.get("name"), str) for value in values) or len(
        {v["name"] for v in values}
    ) != len(values):
        raise VZNativeHold("native VZ provider list is ambiguous")
    return {value["name"]: value for value in values}


def _disk_info(config, path, custody):
    code, raw, _ = _bounded(
        config,
        [str(config.tools["qemu_img"]), "info", "--output=json", str(path)],
        custody=custody,
        operation="disk-info",
    )
    if code:
        raise StateError("native VZ carrier inspection failed")
    info = _decode(raw)
    if (
        not isinstance(info, dict)
        or info.get("format") not in {"raw", "qcow2"}
        or type(info.get("virtual-size")) is not int
        or info["virtual-size"] <= 0
        or any(key in info for key in ("backing-filename", "full-backing-filename", "data-file"))
    ):
        raise StateError("native VZ carrier has unsupported format, size or external storage")
    specific = info.get("format-specific", {}).get("data", {})
    if specific.get("data-file") or specific.get("corrupt") or specific.get("dirty-flag"):
        raise StateError("native VZ carrier external/dirty storage is unsupported")
    return info


def _qualify_cidata_inputs(config, assets):
    """Bound the exact installed resource Lima2.2.1 will put in CIDATA.

    The resolved executable makes both usrlocal.SelfDirs entries identical.
    Its sibling share/lima gzip (preferred) or raw ARM agent must equal the
    already verified initramfs component. The closed 39-file/7-directory
    Linux/VZ template profile has a conservative 32MiB non-agent envelope,
    including ISO9660/RockRidge padding and continuation records.
    """
    try:
        asset_fd = _directory(assets.root, private=False)
        try:
            info = os.fstat(asset_fd)
            if (
                info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o500
                or (info.st_dev, info.st_ino) != (assets.root_device, assets.root_inode)
            ):
                raise VZNativeHold("native VZ asset directory changed before CIDATA qualification")
        finally:
            os.close(asset_fd)
        manifest_raw, manifest_digest, _ = _file(assets.root / "manifest.json", private=True, limit=MAX_METADATA_BYTES)
        if manifest_digest != assets.manifest_digest:
            raise VZNativeHold("native VZ asset manifest changed before CIDATA qualification")
        manifest = _decode(manifest_raw, maximum_bytes=MAX_METADATA_BYTES)
        component = manifest["components"]["guestagent"]
        expected_size, expected_digest = component["size"], component["digest"]
        if (
            type(expected_size) is not int
            or not 0 < expected_size <= _CIDATA_MAX_BYTES - _CIDATA_METADATA_BYTES
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest) is None
        ):
            raise StateError("native VZ guestagent exceeds the closed CIDATA input bound")
        share = config.tools["limactl"].parent.parent / "share" / "lima"
        share_fd = _directory(share, private=False)
        try:
            info = os.fstat(share_fd)
            if info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
                raise StateError("native VZ installed Lima resource directory is not trusted")
            share_identity = [info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)]
        finally:
            os.close(share_fd)
        selected = None
        for filename in ("lima-guestagent.Linux-aarch64.gz", "lima-guestagent.Linux-aarch64"):
            candidate = share / filename
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            selected = candidate
            break
        if selected is None:
            raise StateError("native VZ requires the matching installed Lima ARM guestagent; no download attempted")
        raw, compressed_digest, stamp = _file(selected, limit=_CIDATA_MAX_BYTES)
        if stamp[2] not in {0, os.geteuid()} or stat.S_IMODE(stamp[3]) & 0o022:
            raise StateError("native VZ installed guestagent ownership or permissions are unsafe")
        digest = hashlib.sha256()
        total = 0
        compressed = selected.suffix == ".gz"
        reader = gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") if compressed else io.BytesIO(raw)
        with reader:
            while True:
                chunk = reader.read(min(1024 * 1024, expected_size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    raise StateError("native VZ installed guestagent exceeds its verified decoded size")
                digest.update(chunk)
        actual = "sha256:" + digest.hexdigest()
        if total != expected_size or actual != expected_digest:
            raise StateError("native VZ installed CIDATA guestagent differs from the verified runtime component")
        private_key = config.home / "_config" / "user"
        public_key = config.home / "_config" / "user.pub"
        _, _, private_stamp = _file(private_key, private=True, limit=65536, capture=False)
        public, public_digest, public_stamp = _file(public_key, owned=True, limit=8192)
        _public_key(public)
        required = total + _CIDATA_METADATA_BYTES
        reserved = 64 * 1024**2 if required <= 64 * 1024**2 else _CIDATA_MAX_BYTES
        return {
            "schema": "palimpsest.vz-cidata-inputs.v1",
            "profile": "lima-2.2.1-linux-vz-closed-inputs",
            "limactl_digest": config.value["limactl"]["sha256"],
            "share": str(share),
            "share_identity": share_identity,
            "guestagent": {
                "path": str(selected),
                "stamp": stamp,
                "digest": compressed_digest,
                "encoding": "gzip" if compressed else "raw",
                "decoded_digest": actual,
                "decoded_bytes": total,
            },
            "campaign_identity_stamp": private_stamp,
            "campaign_public_key": {"path": str(public_key), "stamp": public_stamp, "digest": public_digest},
            "non_agent_bound_bytes": _CIDATA_METADATA_BYTES,
            "reserved_bytes": reserved,
        }
    except (OSError, EOFError, zlib.error) as error:
        raise StateError(
            "native VZ installed CIDATA inputs are unavailable or malformed; no download attempted"
        ) from error


def _choose_ssh_port(network):
    """Select a private explicit port without colliding with any publication."""
    excluded = {port.host_port for port in network.published_ports} | {
        port.guest_port for port in network.published_ports
    }
    with ExitStack() as held:
        for _ in range(len(excluded) + 1):
            probe = held.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            if port >= 1024 and port not in excluded:
                return port
        raise StateError("native VZ could not select a non-conflicting private SSH port")


def _rendered_config(native):
    raw, digest, stamp = _file(_path(native["generated_config_path"]), private=True, limit=_MAX_JSON)
    if digest != native["generated_config_digest"] or list(stamp) != native["generated_config_stamp"]:
        raise VZNativeHold("native VZ authored configuration file changed")
    value = _decode(raw)
    if not isinstance(value, dict):
        raise VZNativeHold("native VZ authored configuration is not an object")
    return value


def _require_ssh_port_free(native):
    port = native.get("ssh_local_port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise VZNativeHold("native VZ retained SSH port binding is invalid")
    for rule in _rendered_config(native)["portForwards"]:
        if not rule.get("ignore", False) and (
            rule.get("guestPort") == 22 or port in {rule.get("hostPort"), rule.get("guestPort")}
        ):
            raise VZNativeHold("native VZ retained publication conflicts with its control endpoint")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise _VZStartAdmissionError(
                "native VZ retained SSH port is in use; no alternate port or retry selected"
            ) from error


def _preflight(config, custody, request):
    preflight_pinned_lima(config, custody=custody, network=request.network)
    assets = load_vz_runtime_assets(_path(config.value["assets"]))
    if assets.lima_version != "2.2.1":
        raise StateError("native VZ assets differ from Lima version")
    cidata = _qualify_cidata_inputs(config, assets)
    carrier = _path(config.value["carrier"]["path"])
    _, digest, stamp = _file(carrier, owned=True, capture=False)
    if digest != config.value["carrier"]["sha256"]:
        raise StateError("native VZ carrier digest differs")
    disk = _disk_info(config, carrier, custody)
    if disk["virtual-size"] > config.value["carrier_disk_bytes"]:
        raise StateError("native VZ carrier exceeds selected primary disk size")
    if config.value["carrier_disk_bytes"] + request.root_size_bytes + cidata["reserved_bytes"] > _DISK_LIMIT:
        raise StateError("native VZ aggregate carrier + root + CIDATA exceeds 32 GiB (not 32 GiB per disk)")
    if request.root_volume_id is not None:
        raise StateError("native VZ retained roots resume their original run; rebinding to a new run is unsupported")
    return assets, stamp, cidata


def _snapshot_wire(snapshot):
    return {**snapshot.to_dict(), "reference": snapshot.image.reference.to_dict()}


def _reopen_source(roots, wire, qualification_wire):
    cas = SourceCAS.open_existing(roots.oci_source_cas, expected_cas_id=wire["cas_id"])

    def member(value):
        return SourceSnapshot(Descriptor.from_dict(value["descriptor"]), value["cas_id"])

    root, manifest, config = (member(wire[key]) for key in ("root", "manifest", "config"))
    reference = OCIImageRef(**wire["reference"])
    image = resolve_archive_image(
        reference, root.descriptor, lambda d: cas.read_metadata(SourceSnapshot(d, cas.identity)), platform="linux/arm64"
    )
    snapshot = SnapshottedOCIArchive(image, root, manifest, config, tuple(member(value) for value in wire["layers"]))
    if snapshot.binding_digest != wire["binding_digest"]:
        raise StateError("native VZ source snapshot changed")
    cas.verify_image(snapshot)
    from .native_root_source import NativeRootSourceQualification

    os_release = read_image_os_release(snapshot, cas)
    profile = platforms.select_native_root_boot_profile(
        os_id=os_release.os_id, version_id=os_release.version_id, platform="linux/arm64", backend="lima-vz"
    )
    qualification = NativeRootSourceQualification(snapshot, os_release, profile)
    revalidate_native_root_source(qualification, cas)
    if qualification.to_dict() != qualification_wire:
        raise StateError("native VZ source qualification binding changed")
    return snapshot


def _share_authority_boundary(path, roots, config):
    protected = [
        roots.state,
        roots.config,
        config.home,
        _path(config.value["assets"]),
        _path(config.value["carrier"]["path"]),
        config.path,
    ]
    protected.extend(config.tools.values())
    protected.append(config.tools["limactl"].parent.parent / "share" / "lima")
    if os.environ.get("PALIMPSEST_VZ_WORKER_CONFIG"):
        worker = VZWorker.from_environment()
        protected.extend((worker.config_path, _path(worker.config["lima_home"]), _path(worker.config["custody_root"])))
    source_chain = []
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        source_chain.append((info.st_dev, info.st_ino))
    source_identity = source_chain[0]
    for authority in protected:
        canonical = authority.resolve(strict=False)
        source_parts = tuple(part.casefold() for part in path.parts)
        authority_parts = tuple(part.casefold() for part in canonical.parts)
        common = min(len(source_parts), len(authority_parts))
        if source_parts[:common] == authority_parts[:common]:
            raise StateError(
                "native VZ host source overlaps private state, keys, immutable artifacts or boot authority"
            )
        authority_chain = []
        authority_identity = None
        for candidate in (canonical, *canonical.parents):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            identity = (info.st_dev, info.st_ino)
            authority_chain.append(identity)
            if candidate == canonical:
                authority_identity = identity
        if authority_identity in source_chain or source_identity in authority_chain:
            raise StateError("native VZ host source aliases private authority through filesystem identity")


def _shares(request, roots, config):
    result = []
    for share in getattr(request, "host_shares", ()):
        if not isinstance(share, HostDirectoryShare):
            raise StateError("native VZ host shares require canonical typed policy")
        share.validate_source()
        _share_authority_boundary(share.host_path, roots, config)
        target = share.mount_path
        if len(target.encode("utf-8")) > 4095:
            raise StateError("native VZ host share target exceeds the guest path bound")
        protected = ("/.palimpsest", "/proc", "/sys", "/dev", "/run")
        if target == "/" or any(
            target == p or target.startswith(p + "/") or p.startswith(target + "/") for p in protected
        ):
            raise StateError("native VZ host share overlaps platform/control storage")
        if any(
            target == s["mount_path"]
            or target.startswith(s["mount_path"] + "/")
            or s["mount_path"].startswith(target + "/")
            for s in result
        ):
            raise StateError("native VZ host share targets overlap")
        path = share.host_path
        info = path.lstat()
        result.append(
            {
                "project_root": str(share.project_root),
                "source": share.source,
                "location": str(path),
                "mount_path": target,
                "read_only": share.read_only,
                "tag": lima_vz_mount_tag(path, target),
                "identity": [info.st_dev, info.st_ino],
            }
        )
    return result


def _verify_shares(shares, roots, config):
    for value in shares:
        share = HostDirectoryShare(
            _path(value["project_root"]), value["source"], value["mount_path"], value["read_only"]
        )
        share.validate_source()
        _share_authority_boundary(share.host_path, roots, config)
        info = share.host_path.lstat()
        if [info.st_dev, info.st_ino] != value["identity"] or lima_vz_mount_tag(
            share.host_path, share.mount_path
        ) != value["tag"]:
            raise VZNativeHold("native VZ explicit host share was replaced")


def _render_config(config, request, native, assets, carrier):
    seed = native["seed_path"]
    lower = native["lower_path"]
    forwards = [
        {"guestPort": p.guest_port, "hostPort": p.host_port, "hostIP": p.host_ip, "proto": p.protocol}
        for p in request.network.published_ports
    ]
    # Explicit final ignore prevents Lima's default automatic exposure of image ports.
    forwards.append({"guestIP": "0.0.0.0", "guestIPMustBeZero": False, "guestPortRange": [1, 65535], "ignore": True})
    mounts = [
        {"location": seed, "mountPoint": "/.palimpsest/seed", "writable": False},
        {"location": lower, "mountPoint": "/.palimpsest/lower-input", "writable": False},
    ]
    mounts.extend(
        {"location": s["location"], "mountPoint": s["mount_path"], "writable": not s["read_only"]}
        for s in native["host_shares"]
    )
    return {
        "minimumLimaVersion": "2.2.1",
        "vmType": "vz",
        "arch": "aarch64",
        "plain": False,
        "cpus": request.vcpus,
        "memory": str(request.memory_mib) + "MiB",
        "disk": str(config.value["carrier_disk_bytes"]),
        "os": "Linux",
        "images": [
            {
                "location": str(carrier),
                "arch": "aarch64",
                "digest": config.value["carrier"]["sha256"],
                "kernel": {
                    "location": str(assets.kernel),
                    "digest": _file(assets.kernel, capture=False)[1],
                    "cmdline": "console=hvc0 rdinit=/init net.ifnames=0 biosdevname=0 plm.seed_tag="
                    + native["seed_tag"]
                    + " plm.plan_sha256="
                    + native["plan_sha256"],
                },
                "initrd": {"location": str(assets.initramfs), "digest": _file(assets.initramfs, capture=False)[1]},
            }
        ],
        "mountType": "virtiofs",
        "mounts": mounts,
        "networks": [],
        "vmOpts": {"vz": {"diskImageFormat": "raw", "rosetta": {"enabled": False, "binfmt": False}}},
        "propagateProxyEnv": False,
        "upgradePackages": False,
        "mountInotify": False,
        "timezone": "UTC",
        "user": {
            "name": "plmctl",
            "uid": 1000,
            "home": "/home/plmctl",
            "shell": "/bin/bash",
            "comment": "Palimpsest Lima control",
            "passwordlessSudo": True,
        },
        "ssh": {
            "localPort": native["ssh_local_port"],
            "loadDotSSHPubKeys": False,
            "forwardAgent": False,
            "forwardX11": False,
            "overVsock": True,
        },
        "containerd": {"system": False, "user": False},
        "hostResolver": {"enabled": False},
        "dns": ["8.8.8.8", "8.8.4.4"],
        "env": {_MARKER: native["plan"]["run_id"], **{name: "" for name in _PROXY_VARIABLES}},
        "caCerts": {"files": [], "certs": [], "removeDefaults": False},
        "param": {},
        "additionalDisks": [{"name": native["disk_name"], "format": False, "fsType": "ext4"}],
        "provision": [],
        "probes": [],
        "copyToHost": [],
        "portForwards": forwards,
    }


def _verify_config(actual, expected):
    if not isinstance(actual, dict):
        raise VZNativeHold("native VZ effective configuration is not an object")

    def contains(value, required):
        if isinstance(required, dict):
            return isinstance(value, dict) and all(
                (key not in value and item == []) or (key in value and contains(value[key], item))
                for key, item in required.items()
            )
        if isinstance(required, list):
            return (
                isinstance(value, list)
                and len(value) == len(required)
                and all(contains(a, b) for a, b in zip(value, required, strict=True))
            )
        return type(value) is type(required) and value == required

    def byte_size(value):
        if type(value) is int and value >= 0:
            return value
        if not isinstance(value, str):
            raise VZNativeHold("native VZ effective resource size is invalid")
        match = re.fullmatch(r"([0-9]+)(B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)?", value)
        if match is None:
            raise VZNativeHold("native VZ effective resource size is invalid")
        powers = {
            None: 1,
            "B": 1,
            "KiB": 1024,
            "MiB": 1024**2,
            "GiB": 1024**3,
            "TiB": 1024**4,
            "kB": 1000,
            "MB": 1000**2,
            "GB": 1000**3,
            "TB": 1000**4,
        }
        return int(match[1]) * powers[match[2]]

    if any(byte_size(actual.get(key)) != byte_size(expected[key]) for key in ("memory", "disk")):
        raise VZNativeHold("native VZ effective resource allocation differs")
    if any(rule.get("static", False) is not False for rule in actual.get("portForwards", [])):
        raise VZNativeHold("native VZ port publications require guestagent forwarding, not SSH forwarding")

    for key, value in expected.items():
        if key in {"minimumLimaVersion", "memory", "disk", "provision", "probes", "copyToHost", "networks", "param"}:
            continue
        if not contains(actual.get(key), value):
            raise VZNativeHold("native VZ effective configuration changed protected " + key)
    for key in ("provision", "probes", "copyToHost", "networks"):
        if actual.get(key) not in (None, []):
            raise VZNativeHold("native VZ effective configuration contains unapproved effects")
    if actual.get("env") != expected["env"]:
        raise VZNativeHold("native VZ effective environment contains unapproved values")
    if actual.get("param") not in (None, {}):
        raise VZNativeHold("native VZ effective CIDATA parameters contain unapproved values")
    for mount in actual["mounts"]:
        if mount.get("mountType") not in (None, "virtiofs"):
            raise VZNativeHold("native VZ mount transport differs from virtiofs")
    if actual.get("firmware", {}).get("legacyBIOS") or actual.get("video", {}).get("display") not in (None, "none"):
        raise VZNativeHold("native VZ unapproved firmware/display")
    for name in ("containerd", "ssh", "hostResolver"):
        if not contains(actual.get(name), expected[name]):
            raise VZNativeHold("native VZ effective service/access policy differs")
    rosetta = actual.get("vmOpts", {}).get("vz", {}).get("rosetta", {})
    if rosetta.get("enabled") is not False or rosetta.get("binfmt") is not False:
        raise VZNativeHold("native VZ Rosetta is not explicitly disabled")
    if actual.get("rosetta") not in (None, {}) and actual["rosetta"].get("enabled") is not False:
        raise VZNativeHold("native VZ legacy Rosetta sharing is enabled")


def _data(authority, payload=None):
    if payload is None:
        payload = authority.mutable_state()
    return {key: value for key, value in payload.items() if key not in _RESERVED}


def _write(authority, status, data):
    authority.verify_binding()
    return authority.write_state(status, _data(authority, data))


def _hold(authority, data, reason):
    native = data["vz_native"]
    has_resources = native.get("lease_set_id") is not None or native.get("instance_identity") is not None
    native["hold"] = reason if has_resources else None
    if has_resources:
        try:
            config = _bound_host_config(native)
            marker = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
            if not os.path.lexists(marker):
                _new_file(marker, _json({"schema": _SCHEMA, "run_id": authority.record.run_id, "reason": reason}))
        except BaseException:
            pass
    try:
        _write(authority, "failed", data)
    except BaseException:
        # The canonical reservation/mutation retains its own failed-write evidence.
        pass


def _record(authority):
    if isinstance(authority.record, ExistingRunRecord):
        return authority.record
    return ExistingRunRecord(authority.record.name, authority.record.run_id, 2, authority.dispatch_key)


def read_vz_plan(snapshot):
    """Pure state-only projection; never starts a process or opens a guest channel."""
    state = snapshot.state if hasattr(snapshot, "state") else snapshot
    if not isinstance(state, Mapping) or state.get("backend") != "lima-vz" or state.get("runtime_kind") != "oci-root":
        raise StateError("run is not a canonical native VZ OCI root")
    native = state.get("vz_native")
    if not isinstance(native, Mapping) or native.get("schema") != _SCHEMA:
        raise StateError("native VZ run binding is missing")

    # Thaw immutable state snapshots without borrowing their mutable structures.
    def thaw(value):
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [thaw(item) for item in value]
        return value

    plan = thaw(native.get("plan"))
    if (
        not isinstance(plan, dict)
        or plan.get("schema") != "palimpsest.vz-root-plan.v1"
        or plan.get("run_id") != state.get("run_id")
        or plan.get("run_name") != state.get("name")
        or hashlib.sha256(_json(plan)).hexdigest() != native.get("plan_sha256")
        or plan.get("root_volume_id") != native.get("root_volume_id")
    ):
        raise StateError("native VZ plan does not match canonical run identity")
    OCIProcessSpec.from_dict(plan["process"])
    return plan


def _failed_start_actor_wire(actor):
    return {
        "pid": actor.pid,
        "ppid": actor.ppid,
        "uid": actor.uid,
        "pgid": actor.pgid,
        "start_time_ns": actor.start_time_ns,
        "executable": actor.executable,
        "argv": list(actor.argv),
    }


@dataclass(frozen=True)
class _FailedStartCleanup:
    """Exact original actor and cleanup-phase deadline, never new permission."""

    run_id: str
    deadline_unix: float
    monotonic_deadline: float
    actor: DarwinProcessIdentity
    network_digest: str
    network_stamp: tuple
    instance_names: tuple
    operation: RuntimeOperation
    carrier_ctime_operations: frozenset[RuntimeOperation]
    signal_request: signal.Signals | None

    def check_deadline(self):
        if time.time() >= self.deadline_unix or time.monotonic() >= self.monotonic_deadline:
            raise VZNativeHold("native VZ failed-start cleanup phase deadline expired")


@contextmanager
def _failed_start_cleanup_scope(
    *,
    run_id,
    deadline_unix,
    actor,
    network_digest,
    network_stamp,
    instance_names,
    operation,
    carrier_ctime_operations=(),
    signal_request=None,
):
    """Parent-only binding for canonical stop/RM; never admits start or replay."""
    if (
        _FAILED_START_CLEANUP.get() is not None
        or type(deadline_unix) not in (int, float)
        or not math.isfinite(deadline_unix)
        or deadline_unix <= time.time()
        or not isinstance(actor, DarwinProcessIdentity)
        or actor.pid <= 0
        or actor.uid != os.geteuid()
        or actor.start_time_ns <= 0
        or not isinstance(network_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", network_digest) is None
        or not isinstance(network_stamp, (list, tuple))
        or len(network_stamp) != 8
        or any(type(item) is not int for item in network_stamp)
        or not isinstance(instance_names, (tuple, list))
        or not instance_names
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            for name in instance_names
        )
        or len(set(instance_names)) != len(instance_names)
        or type(operation) is not RuntimeOperation
        or operation not in {RuntimeOperation.STOP, RuntimeOperation.RM}
        or signal_request is not None
        and (
            type(signal_request) is not signal.Signals
            or signal_request not in {signal.SIGTERM, signal.SIGKILL}
            or operation is not RuntimeOperation.STOP
        )
        or not isinstance(carrier_ctime_operations, (tuple, list, frozenset))
        or any(
            type(item) is not RuntimeOperation or item not in {RuntimeOperation.STOP, RuntimeOperation.RM}
            for item in carrier_ctime_operations
        )
    ):
        raise VZNativeHold("native VZ failed-start cleanup lacks exact original actor/network/deadline pins")
    if str(uuid.UUID(run_id)) != run_id:
        raise VZNativeHold("native VZ failed-start cleanup run identity is invalid")
    _path(actor.executable)
    guard = _FailedStartCleanup(
        run_id,
        deadline_unix,
        time.monotonic() + deadline_unix - time.time(),
        actor,
        network_digest,
        tuple(network_stamp),
        tuple(instance_names),
        operation,
        frozenset(carrier_ctime_operations),
        signal_request,
    )
    token = _FAILED_START_CLEANUP.set(guard)
    try:
        yield
    finally:
        _FAILED_START_CLEANUP.reset(token)


def _require_failed_start_operation(operation):
    guard = _FAILED_START_CLEANUP.get()
    if guard is None or guard.operation is not operation:
        raise VZNativeHold("native VZ failed-start cleanup scope does not permit this operation")
    guard.check_deadline()
    return guard


def _failed_start_cleanup_hold(authority, config, data):
    native = data.get("vz_native", {})
    guard = _FAILED_START_CLEANUP.get()
    if guard is None or guard.run_id != authority.record.run_id:
        raise VZNativeHold("native VZ failed-start cleanup requires its exact original actor/network/deadline scope")
    guard.check_deadline()
    _verify_host_authority(config, native)
    config.verify(allow_expired=True)
    attempt = native.get("start_attempt")
    if (
        authority.snapshot.state["status"] != "failed"
        or native.get("hold") != "launch-incomplete"
        or native.get("instance") != "plm-" + authority.record.run_id.replace("-", "")[:12]
        or not isinstance(native.get("instance_identity"), list)
        or len(native["instance_identity"]) != 2
        or any(type(item) is not int for item in native["instance_identity"])
        or not isinstance(attempt, dict)
        or set(attempt) != {"id", "started_unix", "log"}
        or not isinstance(attempt["id"], str)
        or type(attempt["started_unix"]) not in (int, float)
        or not math.isfinite(attempt["started_unix"])
        or not 0 < attempt["started_unix"] <= time.time()
        or guard.actor.start_time_ns / 1_000_000_000 < attempt["started_unix"]
        or any(key in native for key in ("pre_handoff_status", "vsock_forwarder"))
        or "vz_status" in data
        or native.get("access", {}).get("host") is not None
        or native.get("access", {}).get("port") is not None
        or "ssh_config" in native.get("access", {})
        or native.get("boot_copies", {}).get("phase") != "ready"
    ):
        raise VZNativeHold("native VZ failed-start provenance, original attempt or pre-handoff binding differs")
    if str(uuid.UUID(attempt["id"])) != attempt["id"]:
        raise VZNativeHold("native VZ failed-start attempt identity is invalid")
    marker_path = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
    marker, digest, stamp = _resource_hold_marker(marker_path)
    if marker != {"schema": _SCHEMA, "run_id": authority.record.run_id, "reason": "launch-incomplete"}:
        raise VZNativeHold("native VZ failed-start cleanup requires the original launch HOLD")
    authority.verify_binding()
    return marker_path, digest, stamp


def _failed_start_path_exists(path):
    """Only ENOENT proves absence; inaccessible authority remains HOLD."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise VZNativeHold("native VZ cleanup path absence is unknown: " + str(path)) from error
    return True


def _failed_start_network(config, native, custody, *, provider_absent=False):
    """Closed Lima2.2.1 default network shape; unknown YAML means HOLD."""
    guard = _FAILED_START_CLEANUP.get()
    path = config.home / "_config" / "networks.yaml"
    raw, digest, stamp = _file(path, owned=True, limit=_MAX_JSON)
    if digest != guard.network_digest or tuple(stamp) != guard.network_stamp:
        raise VZNativeHold("native VZ effective network configuration differs from its cleanup pin")
    # Not a general YAML parser: aliases, duplicates and alternate paths fail.
    expected = (
        "paths:",
        '  socketVMNet: "/opt/socket_vmnet/bin/socket_vmnet"',
        "  varRun: /private/var/run/lima",
        "  sudoers: /private/etc/sudoers.d/lima",
        "group: admin",
        "networks:",
        "  user-v2:",
        "    mode: user-v2",
        "    gateway: 192.168.104.1",
        "    netmask: 255.255.255.0",
        "  shared:",
        "    mode: shared",
        "    gateway: 192.168.105.1",
        "    dhcpEnd: 192.168.105.254",
        "    netmask: 255.255.255.0",
        "  bridged:",
        "    mode: bridged",
        "    interface: en0",
        "  host:",
        "    mode: host",
        "    gateway: 192.168.106.1",
        "    dhcpEnd: 192.168.106.254",
        "    netmask: 255.255.255.0",
    )
    try:
        lines = tuple(
            line.rstrip()
            for line in raw.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except UnicodeError as error:
        raise VZNativeHold("native VZ effective networks YAML is not the closed default profile") from error
    if lines != expected:
        raise VZNativeHold("native VZ effective networks YAML has unqualified semantics")
    absent = [Path("/opt/socket_vmnet/bin/socket_vmnet"), config.home / "_networks"]
    absent.extend(
        Path("/private/var/run/lima") / (name + "_socket_vmnet.pid") for name in ("shared", "bridged", "host")
    )
    if any(_failed_start_path_exists(candidate) for candidate in absent):
        raise VZNativeHold("native VZ network reconciliation could affect a daemon or user network")
    instances = _instances(config, custody, allow_expired=True)
    expected_names = set(guard.instance_names)
    if native["instance"] not in expected_names:
        raise VZNativeHold("native VZ original instance is missing from the scoped provider inventory")
    if provider_absent:
        expected_names.remove(native["instance"])
    if set(instances) != expected_names or any(
        instance.get("vmType") != "vz" or instance.get("arch") != "aarch64" for instance in instances.values()
    ):
        raise VZNativeHold("native VZ exact provider inventory/type changed during cleanup")
    for instance in instances.values():
        effective = instance.get("config")
        if not isinstance(effective, dict):
            raise VZNativeHold("native VZ provider has unknown effective network configuration")
        networks = effective.get("networks")
        in_process_nat = (
            isinstance(networks, list)
            and len(networks) == 1
            and isinstance(networks[0], dict)
            and set(networks[0]).issubset({"vzNAT", "macAddress", "interface", "metric"})
            and networks[0].get("vzNAT") is True
            and (
                "macAddress" not in networks[0]
                or (
                    isinstance(networks[0]["macAddress"], str)
                    and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", networks[0]["macAddress"]) is not None
                )
            )
            and (
                "interface" not in networks[0]
                or (
                    isinstance(networks[0]["interface"], str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,14}", networks[0]["interface"]) is not None
                )
            )
            and (
                "metric" not in networks[0]
                or (type(networks[0]["metric"]) is int and 0 <= networks[0]["metric"] <= 2**32 - 1)
            )
        )
        if networks not in (None, []) and not in_process_nat:
            raise VZNativeHold("native VZ network reconciliation could start another instance's named network")
        if instance.get("name") != native["instance"]:
            disks = effective.get("additionalDisks", [])
            if not isinstance(disks, list) or any(not isinstance(disk, dict) for disk in disks):
                raise VZNativeHold("native VZ foreign instance disk consumers are not fully known")
            if any(disk.get("name") == native["disk_name"] for disk in disks):
                raise VZNativeHold("native VZ upper disk is referenced by another provider instance")
    if _file(path, owned=True, limit=_MAX_JSON)[1:] != (digest, stamp):
        raise VZNativeHold("native VZ effective networks changed during no-action inspection")
    return instances


def _require_failed_start_processes(expected, current, processes, instance, directory, *, absent):
    """Classify every inventory entry; an unknown executable never hides an actor."""
    descendants = {expected.pid}
    while True:
        expanded = descendants | {actor.pid for actor in processes if actor.ppid in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    related = [
        actor
        for actor in processes
        if actor.pid != os.getpid()
        and (
            actor.pid in descendants
            or actor.pgid == expected.pgid
            or any(item == instance or str(directory) in item for item in actor.argv)
        )
    ]
    if absent:
        if current is not None or related:
            raise VZNativeHold(
                "native VZ original PID, related actor or detached descendant remains; reuse is not absence"
            )
    elif current != expected or related != [expected]:
        raise VZNativeHold("native VZ exact original positive actor identity changed or has additional actors")


def _failed_start_endpoint_pin(directory, name, info, guard, *, inactive, original=None):
    """Bind original PID files/socket; only an inactive original is removable."""
    if guard.signal_request is not signal.SIGKILL:
        raise VZNativeHold("native VZ stale endpoint cleanup requires exact forced-cleanup scope")
    if info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022:
        raise VZNativeHold("native VZ stale endpoint ownership or permissions differ")
    path = directory / name
    if name in {"ha.pid", "vz.pid"}:
        raw, digest, stamp = _file(path, owned=True, limit=64)
        if raw.strip() != str(guard.actor.pid).encode("ascii") or stamp != _stamp(info):
            raise VZNativeHold("native VZ stale PID file is not the exact original actor")
        pin = {"stamp": list(stamp), "digest": digest}
    else:
        if name != "ha.sock" or not stat.S_ISSOCK(info.st_mode):
            raise VZNativeHold("native VZ unknown stale endpoint cannot be removed")
        if inactive:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(0.2)
                try:
                    channel.connect(str(path))
                except OSError as error:
                    if error.errno != errno.ECONNREFUSED:
                        raise VZNativeHold("native VZ stale socket inactivity is not proven") from error
                else:
                    raise VZNativeHold("native VZ hostagent socket is still active")
        if _stamp(path.lstat()) != _stamp(info):
            raise VZNativeHold("native VZ stale socket identity changed")
        pin = {"stamp": list(_stamp(info))}
    if inactive and pin != original:
        raise VZNativeHold("native VZ stale endpoint no longer matches its pre-signal original")
    return pin


def _failed_start_endpoint_pins(config, native, guard):
    directory = config.home / native["instance"]
    fd = _directory(directory)
    try:
        if list(_stamp(os.fstat(fd))[:2]) != native["instance_identity"]:
            raise VZNativeHold("native VZ original endpoint directory identity changed")
        return {
            name: _failed_start_endpoint_pin(
                directory, name, os.stat(name, dir_fd=fd, follow_symlinks=False), guard, inactive=False
            )
            for name in ("ha.pid", "vz.pid", "ha.sock")
        }
    finally:
        os.close(fd)


def _failed_start_default_network_pins(directory, native, guard, *, previous=None):
    """Snapshot inactive Lima2.2.1 in-process default gvisor leaves, never unlink."""
    recovery = native["failed_start_cleanup"]
    before = recovery.get("before_stop", {})
    started = native["start_attempt"]["started_unix"]
    stopped_after = before.get("observed_unix")
    if (
        guard.signal_request is not None
        or recovery.get("stop") not in {"returned", "settled"}
        or any(key in recovery for key in ("termination", "forced_termination"))
        or before.get("phase") != "before-stop"
        or before.get("actor_absent") is not False
        or before.get("original_pid") != guard.actor.pid
        or type(stopped_after) not in (int, float)
        or not math.isfinite(stopped_after)
        or not started <= stopped_after <= time.time()
        or previous is not None
        and (not isinstance(previous, dict) or not set(previous).issubset({"default_ep.sock", "default_fd.sock"}))
    ):
        raise VZNativeHold("native VZ default network snapshot lacks ordinary returned-stop provenance")
    guard.check_deadline()
    try:
        fd = _directory(directory)
        try:
            directory_stamp = _stamp(os.fstat(fd))
            if directory_stamp[:2] != native["instance_identity"]:
                raise VZNativeHold("native VZ default network directory identity changed")

            def recheck(name=None, stamp=None):
                guard.check_deadline()
                visible = _directory(directory)
                try:
                    if _stamp(os.fstat(visible)) != directory_stamp or _stamp(os.fstat(fd)) != directory_stamp:
                        raise VZNativeHold("native VZ default network directory changed during inspection")
                finally:
                    os.close(visible)
                if name is not None and _stamp(os.stat(name, dir_fd=fd, follow_symlinks=False)) != stamp:
                    raise VZNativeHold("native VZ default network socket changed during inspection")
                guard.check_deadline()

            pins = {}
            for name in os.listdir(fd):
                guard.check_deadline()
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not (stat.S_ISSOCK(info.st_mode) or name.endswith((".sock", ".pid")) or name == "sock"):
                    continue
                if (
                    name not in {"default_ep.sock", "default_fd.sock"}
                    or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o755
                ):
                    raise VZNativeHold("native VZ unknown or unsafe default network endpoint remains")
                stamp = _stamp(info)
                pin = {"stamp": stamp}
                timestamps = [info.st_mtime_ns / 1_000_000_000, info.st_ctime_ns / 1_000_000_000]
                if hasattr(info, "st_birthtime"):
                    pin["birthtime"] = info.st_birthtime
                    timestamps.append(info.st_birthtime)
                if previous is None:
                    if any(not started <= value <= stopped_after for value in timestamps):
                        raise VZNativeHold(
                            "native VZ default network endpoint is outside the original pre-stop lifetime"
                        )
                elif previous.get(name) != pin:
                    raise VZNativeHold("native VZ default network endpoint changed or reappeared")
                for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                    recheck(name, stamp)
                    with socket.socket(socket.AF_UNIX, kind) as channel:
                        channel.settimeout(0.2)
                        try:
                            channel.connect(str(directory / name))
                        except OSError as error:
                            if error.errno != errno.ECONNREFUSED:
                                raise VZNativeHold("native VZ default network socket inactivity is unknown") from error
                        else:
                            raise VZNativeHold("native VZ default network socket remains active")
                    recheck(name, stamp)
                pins[name] = pin
            if previous is None and pins and set(pins) != {"default_ep.sock", "default_fd.sock"}:
                raise VZNativeHold("native VZ default network snapshot is not the supported endpoint pair")
            for name, pin in pins.items():
                recheck(name, pin["stamp"])
            recheck()
            return pins
        finally:
            os.close(fd)
    except OSError as error:
        raise VZNativeHold("native VZ default network endpoint observation is incomplete") from error


def _failed_start_observation(authority, roots, config, custody, data, *, phase, absent, allow_stale=False):
    """Read concrete native process identity, provider and endpoint authority."""
    native = data["vz_native"]
    guard = _FAILED_START_CLEANUP.get()
    _failed_start_cleanup_hold(authority, config, data)
    expected = guard.actor
    directory = config.home / native["instance"]
    if allow_stale and (
        not absent
        or guard.signal_request is not signal.SIGKILL
        or native.get("failed_start_cleanup", {}).get("forced_termination", {}).get("phase") != "sent"
    ):
        raise VZNativeHold("native VZ stale endpoint observation lacks a sent one-shot forced cleanup")
    stale_endpoints = {}
    recovery = native.get("failed_start_cleanup", {})
    normal_network = (
        absent
        and phase != "after-delete"
        and recovery.get("stop") in {"returned", "settled"}
        and not any(key in recovery for key in ("termination", "forced_termination"))
        and guard.signal_request is None
    )
    network_pins = None
    previous_network_pins = None
    if normal_network:
        previous_observation = recovery.get("after_stop")
        if previous_observation is None:
            if phase != "after-stop" or recovery["stop"] != "returned":
                raise VZNativeHold("native VZ default network snapshot cannot be adopted after settlement")
        else:
            if (
                not isinstance(previous_observation, dict)
                or previous_observation.get("default_network_endpoint_basis") != "current-after-normal-stop"
                or not isinstance(previous_observation.get("default_network_endpoints"), dict)
            ):
                raise VZNativeHold("native VZ ordinary stop lacks persisted current endpoint pins")
            previous_network_pins = previous_observation["default_network_endpoints"]
    if (
        expected.executable != str(config.tools["limactl"])
        or not expected.argv
        or expected.argv[0] != expected.executable
        or "hostagent" not in expected.argv
        or native["instance"] not in expected.argv
        or str(directory / "ha.pid") not in expected.argv
        or str(directory / "ha.sock") not in expected.argv
    ):
        raise VZNativeHold("native VZ original actor does not bind the exact pinned hostagent command")
    current = read_process(expected.pid)
    processes = list_processes()
    _require_failed_start_processes(expected, current, processes, native["instance"], directory, absent=absent)
    instances = _failed_start_network(config, native, custody, provider_absent=phase == "after-delete")
    instance = instances.get(native["instance"])
    if phase == "after-delete":
        if instance is not None or _failed_start_path_exists(directory):
            raise VZNativeHold("native VZ provider or directory remains after ordinary delete")
    else:
        instance = _owned_instance(config, native, custody, allow_expired=True)
        expected_pid = 0 if absent else expected.pid
        # Lima2.2.1 JSON omits zero PID fields; explicit null is not absence.
        host_pid = instance.get("hostAgentPID", 0 if absent else None)
        driver_pid = instance.get("driverPID", 0 if absent else None)
        if (
            type(host_pid) is not int
            or type(driver_pid) is not int
            or host_pid != expected_pid
            or driver_pid != expected_pid
            or instance.get("autoStartedIdentifier") not in (None, "")
        ):
            raise VZNativeHold("native VZ provider actor PID/autostart identity differs")
        if absent and instance.get("status") != "Stopped":
            raise VZNativeHold("native VZ absent actor lacks authoritative Stopped provider state")
        if normal_network:
            effective = instance.get("config")
            if not isinstance(effective, dict) or effective.get("networks") not in (None, []):
                raise VZNativeHold("native VZ default gvisor endpoints require an unconfigured per-instance network")
    plist = config.home / "Library" / "LaunchAgents" / ("io.lima-vm.autostart." + native["instance"] + ".plist")
    if _failed_start_path_exists(plist):
        raise VZNativeHold("native VZ cleanup would mutate a launchd autostart service")
    if _failed_start_path_exists(directory):
        fd = _directory(directory)
        try:
            if list(_stamp(os.fstat(fd))[:2]) != native["instance_identity"]:
                raise VZNativeHold("native VZ endpoint observation directory identity changed")
            if normal_network:
                network_pins = _failed_start_default_network_pins(
                    directory, native, guard, previous=previous_network_pins
                )
            for name in os.listdir(fd):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if name.endswith(".drv.pid") or name.endswith(".pid") and name not in {"ha.pid", "vz.pid"}:
                    raise VZNativeHold("native VZ external or unknown driver PID custody blocks cleanup")
                if absent and (stat.S_ISSOCK(info.st_mode) or name.endswith((".sock", ".pid")) or name == "sock"):
                    if normal_network:
                        continue  # The complete endpoint inventory was checked above.
                    if not allow_stale:
                        raise VZNativeHold("native VZ provider PID/socket endpoint remains after actor exit")
                    original = native["failed_start_cleanup"]["forced_termination"].get("before_endpoints", {})
                    stale_endpoints[name] = _failed_start_endpoint_pin(
                        directory, name, info, guard, inactive=True, original=original.get(name)
                    )
            if not absent:
                for name in ("ha.pid", "vz.pid"):
                    raw = _file(directory / name, owned=True, limit=64)[0]
                    if raw.strip() != str(expected.pid).encode("ascii"):
                        raise VZNativeHold("native VZ original actor PID file differs")
        finally:
            os.close(fd)
    if read_process(expected.pid) != current:
        raise VZNativeHold("native VZ original actor changed during process/provider inspection")
    guard.check_deadline()
    if absent:
        _require_ssh_port_free(native)
    observation = {"phase": phase, "observed_unix": time.time(), "original_pid": expected.pid, "actor_absent": absent}
    if allow_stale:
        observation["stale_endpoints"] = stale_endpoints
    if normal_network:
        observation["default_network_endpoint_basis"] = "current-after-normal-stop"
        observation["default_network_endpoints"] = network_pins
    root = load_vz_root_volume(
        roots,
        native["root_volume_id"],
        owner=ArtifactLeaseOwner(authority.record.run_id, authority.record.name, "root-lower"),
    )
    fd = _directory(root.record.storage.directory)
    try:
        names = set(os.listdir(fd))
        if names != {"datadisk"} or not absent:
            if names != {"datadisk", "in_use_by"}:
                raise VZNativeHold("native VZ upper has unknown files or lacks its exact original lock")
            info = os.stat("in_use_by", dir_fd=fd, follow_symlinks=False)
            if (
                not stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or os.readlink("in_use_by", dir_fd=fd) != str(config.home / native["instance"])
                or _stamp(os.stat("in_use_by", dir_fd=fd, follow_symlinks=False)) != _stamp(info)
            ):
                raise VZNativeHold("native VZ upper consumer link is not exactly owned")
    finally:
        os.close(fd)
    authority.verify_binding()
    if phase == "after-delete":
        if _failed_start_path_exists(directory):
            raise VZNativeHold("native VZ provider directory reappeared during absence observation")
    else:
        directory_fd = _directory(directory)
        try:
            if list(_stamp(os.fstat(directory_fd))[:2]) != native["instance_identity"]:
                raise VZNativeHold("native VZ endpoint directory changed during observation")
        finally:
            os.close(directory_fd)
        if normal_network:
            final_network_pins = _failed_start_default_network_pins(directory, native, guard, previous=network_pins)
            observation["default_network_endpoints"] = final_network_pins
    final_actor = read_process(expected.pid)
    if final_actor != current:
        raise VZNativeHold("native VZ original actor changed after resource observation")
    _require_failed_start_processes(
        expected, final_actor, list_processes(), native["instance"], directory, absent=absent
    )
    if read_process(expected.pid) != final_actor:
        raise VZNativeHold("native VZ original actor changed during final process inventory")
    if normal_network:
        observation["default_network_endpoints"] = _failed_start_default_network_pins(
            directory, native, guard, previous=observation["default_network_endpoints"]
        )
    guard.check_deadline()
    return observation


def _prestart_cleanup_hold(authority, config, data):
    """Authorize deletion only, never adoption/start, of an original failed create."""
    native = data.get("vz_native", {})
    _verify_host_authority(config, native)
    config.verify(allow_expired=True)
    if (
        authority.snapshot.state["status"] != "failed"
        or native.get("hold") not in {"launch-incomplete", "retained-authority-changed"}
        or native.get("instance") != "plm-" + authority.record.run_id.replace("-", "")[:12]
        or not isinstance(native.get("instance_identity"), list)
        or len(native["instance_identity"]) != 2
        or any(type(item) is not int for item in native["instance_identity"])
        or any(key in native for key in ("start_attempt", "pre_handoff_status", "vsock_forwarder"))
        or "vz_status" in data
        or native.get("access", {}).get("host") is not None
        or native.get("access", {}).get("port") is not None
        or "ssh_config" in native.get("access", {})
    ):
        raise VZNativeHold("native VZ cleanup requires exact failed-prestart provenance without a start attempt")
    marker_path = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
    marker, digest, stamp = _resource_hold_marker(marker_path)
    if marker["run_id"] != authority.record.run_id or (marker["reason"], native["hold"]) not in {
        ("launch-incomplete", "launch-incomplete"),
        ("launch-incomplete", "retained-authority-changed"),
    }:
        raise VZNativeHold("native VZ cleanup requires its original failed-create resource HOLD")
    authority.verify_binding()
    return marker_path, digest, stamp


def _require_stopped_root_exclusive(directory):
    """Exclude live or foreign upper custody before stopped adoption/deletion."""
    fd = _directory(directory)
    try:
        if set(os.listdir(fd)) != {"datadisk"}:
            raise VZNativeHold("native VZ stopped root has a consumer lock or unknown files")
    finally:
        os.close(fd)


def _load_checked(
    authority,
    roots,
    config,
    custody,
    *,
    allow_expired=False,
    data=None,
    cleanup_only=False,
    failed_start=False,
    stopped_hold=False,
):
    data = _data(authority) if data is None else data
    native = data.get("vz_native", {})
    state = {
        **data,
        "backend": "lima-vz",
        "runtime_kind": "oci-root",
        "run_id": authority.record.run_id,
        "name": authority.record.name,
    }
    read_vz_plan(state)
    _stop_settlement(authority, config, native)
    if stopped_hold:
        _ordinary_stop_hold(authority, config, data)
    elif failed_start:
        _failed_start_cleanup_hold(authority, config, data)
    elif cleanup_only:
        _prestart_cleanup_hold(authority, config, data)
    elif native.get("hold"):
        raise VZNativeHold("native VZ run retains HOLD: " + native["hold"])
    _verify_host_authority(config, native)
    config.verify(allow_expired=allow_expired)
    if _record(authority).dispatch_key.backend.value != "lima-vz":
        raise StateError("native VZ authority belongs to another backend")
    carrier = native["carrier"]
    _, digest, stamp = _file(_path(carrier["path"]), private=True, capture=False)
    expected_stamp = carrier["stamp"]
    guard = _FAILED_START_CLEANUP.get()
    history_allowed = failed_start and guard is not None and guard.operation in guard.carrier_ctime_operations
    if cleanup_only or history_allowed:
        # Operation-specific destruction/stop eligibility never restores boot
        # continuity or replaces _file's full current-read stability.
        if (
            not isinstance(expected_stamp, list)
            or len(expected_stamp) != 8
            or any(type(item) is not int for item in expected_stamp)
        ):
            raise VZNativeHold("native VZ cleanup carrier stamp is malformed")
        carrier_matches = stamp[:7] == expected_stamp[:7]
    else:
        carrier_matches = stamp == expected_stamp
    if digest != carrier["digest"] or not carrier_matches:
        raise VZNativeHold("native VZ pinned carrier copy changed")
    if history_allowed:
        native["failed_start_carrier_history"] = {
            "operation": guard.operation.value,
            "digest": digest,
            "original_stamp": list(expected_stamp),
            "current_stamp": list(stamp),
        }
    _reopen_source(roots, native["source_snapshot"], data["source_qualification"])
    assets = load_vz_runtime_assets(_path(config.value["assets"]))
    if assets.manifest_digest != native["assets_digest"]:
        raise VZNativeHold("native VZ boot asset generation changed")
    if _qualify_cidata_inputs(config, assets) != native.get("cidata_inputs"):
        raise VZNativeHold("native VZ installed CIDATA inputs changed since original qualification")
    owner = ArtifactLeaseOwner(authority.record.run_id, authority.record.name, "root-lower")
    store = OCIStore(roots)
    lease = store.load_lease_set(native["lease_set_id"], owner, plan_digest=native["lease_plan_digest"])
    receipts = tuple(member.receipt for member in lease.members)
    export = load_vz_lower_export(roots, store, receipts, owner, lease)
    verify_vz_lower_export(export, store, receipts, owner, lease)
    if export.binding_digest != native["lower_export_binding"] or str(export.root) != native["lower_path"]:
        raise VZNativeHold("native VZ lower projection differs")
    disk = load_vz_root_volume(roots, native["root_volume_id"], owner=owner)
    if (
        disk.record.lower_graph_digest != native["plan"]["root_graph_digest"]
        or disk.record.storage.disk_name != native["disk_name"]
    ):
        raise VZNativeHold("native VZ canonical root disk binding differs")
    seed = _path(native["seed_path"])
    fd = _directory(seed)
    try:
        info = os.fstat(fd)
        if [info.st_dev, info.st_ino] != native["seed_identity"] or set(os.listdir(fd)) != {"plan.json"}:
            raise VZNativeHold("native VZ exact seed directory changed")
    finally:
        os.close(fd)
    payload, digest, stamp = _file(seed / "plan.json", private=True, limit=_MAX_JSON)
    if digest != "sha256:" + native["plan_sha256"] or stamp != native["seed_stamp"] or payload != _json(native["plan"]):
        raise VZNativeHold("native VZ exact seed bytes changed")
    for name, expected in native["access"]["files"].items():
        is_private = expected["private"]
        _, digest, stamp = _file(_path(name), private=is_private, owned=True, limit=65536 if is_private else 8192)
        if digest != expected["digest"] or stamp != expected["stamp"]:
            raise VZNativeHold("native VZ pinned SSH trust/key bytes changed")
    _verify_shares(native["host_shares"], roots, config)
    if failed_start:
        reference = native.get("effective_config_reference")
        if not isinstance(reference, dict) or set(reference) != {"path", "digest", "stamp"}:
            raise VZNativeHold("native VZ failed-start filled configuration authority is missing")
        path = _path(reference["path"])
        _, digest, stamp = _file(path, private=True, limit=_MAX_JSON)
        if digest != reference["digest"] or stamp != reference["stamp"]:
            raise VZNativeHold("native VZ failed-start filled configuration bytes changed")
        _verify_config(_config_json(config, path, custody, allow_expired=True), _rendered_config(native))
    authority.verify_binding()
    if native["instance_identity"] is not None:
        instance = _owned_instance(config, native, custody, allow_expired=allow_expired)
        if stopped_hold and instance.get("status") != "Stopped":
            raise VZNativeHold("native VZ stop settlement observed a non-stopped provider")
        if cleanup_only:
            directory = config.home / native["instance"]
            if (
                instance.get("status") != "Stopped"
                or instance.get("dir") != str(directory)
                or not set(os.listdir(directory)).issubset(
                    {
                        "disk",
                        "kernel",
                        "initrd",
                        "kernel.cmdline",
                        "cloud-config.yaml",
                        "vz-identifier",
                        "lima-version",
                        "lima.yaml",
                    }
                )
            ):
                raise VZNativeHold("native VZ failed-prestart cleanup has live/unknown provider or boot-actor evidence")
        if native.get("boot_copies") is not None:
            _verify_boot_copies(config, native)
    return data, store, owner, lease, receipts, export


def _load(authority, roots, config, custody, *, allow_expired=False, data=None, cleanup_only=False):
    current = _data(authority) if data is None else data
    try:
        return _load_checked(
            authority, roots, config, custody, allow_expired=allow_expired, data=current, cleanup_only=cleanup_only
        )
    except VZNativeHold:
        _hold(authority, current, "retained-authority-changed")
        raise


def _owned_instance(config, native, custody, *, allow_expired=False, deadline=None):
    instance = _instances(config, custody, allow_expired=allow_expired, deadline=deadline).get(native["instance"])
    if instance is None:
        raise VZNativeHold("native VZ exact owned provider instance is absent")
    path = config.home / native["instance"]
    fd = _directory(path)
    try:
        info = os.fstat(fd)
        if [info.st_dev, info.st_ino] != native["instance_identity"]:
            raise VZNativeHold("native VZ instance directory identity changed")
    finally:
        os.close(fd)
    if (
        instance.get("name") != native["instance"]
        or instance.get("vmType") != "vz"
        or instance.get("arch") != "aarch64"
    ):
        raise VZNativeHold("native VZ provider identity/type changed")
    actual = instance.get("config")
    expected_config = _rendered_config(native)
    _verify_config(actual, expected_config)
    _, digest, stamp = _file(path / "lima.yaml", owned=True, limit=_MAX_JSON)
    if digest != native["persisted_config_digest"] or list(stamp) != native["persisted_config_stamp"]:
        raise VZNativeHold("native VZ persisted Lima configuration changed")
    _verify_config(
        _config_json(
            config,
            path / "lima.yaml",
            custody,
            allow_expired=allow_expired,
            private=False,
            deadline=deadline,
        ),
        expected_config,
    )
    _observation_timeout(deadline)
    return instance


def _ssh_argv(config, native, *, control=False, tty=False, command=None):
    access = native["access"]
    port = access.get("port")
    if type(port) is not int or not 1 <= port <= 65535 or access.get("host") != "127.0.0.1":
        raise VZNativeHold("native VZ access has no verified vsock-forwarder endpoint")
    argv = [
        str(config.tools["ssh"]),
        "-F",
        "/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + access["known_hosts"],
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "HostKeyAlias=" + access["host_alias"],
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-i",
        access["control_identity"] if control else access["identity"],
        "-p",
        str(port),
        "-tt" if tty else "-T",
        "root@127.0.0.1",
    ]
    if command is not None:
        argv.append(command)
    return argv


def _control(config, native, operation, custody, *, allow_expired=False, fields=None):
    encoded = base64.urlsafe_b64encode(_json({"operation": operation, **(fields or {})})).decode().rstrip("=")
    code, raw, _ = _bounded(
        config,
        _ssh_argv(config, native, control=True, command="plm-control-v1 " + encoded),
        custody=custody,
        operation="control:" + operation,
        timeout=40,
        allow_expired=allow_expired,
    )
    if code:
        raise VZNativeHold("native VZ authenticated control failed; no fallback or retry")
    return _decode(raw)


def _validate_status(native, value, *, handed_off, allow_stopping=False):
    plan = native["plan"]
    if (
        not isinstance(value, dict)
        or value.get("schema") != "palimpsest.vz-root-status.v1"
        or value.get("platform_abi") != PLATFORM_ABI
        or value.get("run_id") != plan["run_id"]
        or value.get("plan_sha256") != native["plan_sha256"]
        or value.get("root_filesystem_uuid") != plan["root_filesystem_uuid"]
        or value.get("root_nonce") != plan["root_nonce"]
    ):
        raise VZNativeHold("native VZ authenticated root status identity differs")
    if value.get("root_filesystem") != "overlay":
        raise VZNativeHold("native VZ authenticated PID1 root is not OverlayFS")
    for key in ("root_device", "root_inode", "pid1_root_device", "pid1_root_inode"):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise VZNativeHold("native VZ root status lacks filesystem identity")
    if (value["root_device"], value["root_inode"]) != (value["pid1_root_device"], value["pid1_root_inode"]):
        raise VZNativeHold("native VZ PID1 is not rooted in the image OverlayFS")
    lowers = value.get("lower_mounts")
    if not isinstance(lowers, list) or len(lowers) != len(plan["layers"]):
        raise VZNativeHold("native VZ root status lower occurrence count differs")
    for expected, actual in zip(plan["layers"], lowers, strict=True):
        if (
            actual.get("ordinal") != expected["ordinal"]
            or actual.get("digest") != expected["digest"]
            or actual.get("mountpoint") != f"/.palimpsest/lowers/{expected['ordinal']:03d}"
            or actual.get("readonly") is not True
            or type(actual.get("device")) is not int
            or type(actual.get("inode")) is not int
        ):
            raise VZNativeHold("native VZ authenticated lower mount order/identity differs")
    if handed_off:
        phases = {"ready", "stopping", "stop-ready"} if allow_stopping else {"ready"}
        if value.get("bootstrap_tcp_open") is not False or value.get("phase") not in phases:
            raise VZNativeHold("native VZ bootstrap TCP remained open or root handoff failed")
    elif (
        value.get("bootstrap_tcp_open") is not True
        or value.get("phase") != "awaiting-handoff"
        or value.get("main", {}).get("state") != "not-started"
    ):
        raise VZNativeHold("native VZ main started before authenticated handoff")
    if (
        value.get("user_sessions_known") is not True
        or type(value.get("user_session_count")) is not int
        or not 0 <= value["user_session_count"] <= 64
    ):
        raise VZNativeHold("native VZ guest session custody is unknown")
    main = value.get("main")
    if (
        not isinstance(main, dict)
        or main.get("state") not in {"not-started", "running", "exited", "absent"}
        or type(main.get("pid")) is not int
        or main["pid"] < 0
        or main.get("exit_code") is not None
        and (type(main["exit_code"]) is not int or not 0 <= main["exit_code"] <= 255)
    ):
        raise VZNativeHold("native VZ main observation is invalid")
    return value


def _vsock_event(config, native, attempt, instance):
    """Authenticate this attempt's genuine Lima2.2.1 structured vsock event."""
    from datetime import datetime

    path = config.home / native["instance"] / "ha.stdout.log"
    raw, digest, stamp = _file(path, owned=True, limit=8 * _MAX_JSON)
    old = attempt["log"]
    transition = "new-file"
    event_bytes = raw
    if old is not None and stamp[:2] == old["identity"]:
        if len(raw) >= old["size"] and _digest(raw[: old["size"]]) == old["digest"]:
            transition = "verified-append"
            event_bytes = raw[old["size"] :]
        elif stamp != old["stamp"]:
            transition = "truncate-or-rewrite-in-place"
        else:
            raise VZNativeHold("native VZ hostagent event log has no observed start-attempt change")
    port = instance.get("sshLocalPort")
    if port != native["ssh_local_port"]:
        raise VZNativeHold("native VZ provider SSH endpoint differs from its explicit non-conflicting port")
    if instance.get("sshAddress") != "127.0.0.1":
        raise VZNativeHold("native VZ vsock-forwarder listener is not loopback")
    if type(port) is not int or not 1 <= port <= 65535:
        raise VZNativeHold("native VZ provider did not expose its SSH forwarder port")
    expected_address = "127.0.0.1:" + str(port)
    selected = None
    for line in event_bytes.splitlines():
        value = _decode(line)
        if not isinstance(value, dict):
            raise VZNativeHold("native VZ hostagent event is not an object")
        try:
            observed = datetime.fromisoformat(value["time"].replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError()
            when = observed.timestamp()
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise VZNativeHold("native VZ hostagent event has no authenticated attempt time") from error
        if when < attempt["started_unix"] or when > time.time() + 1:
            continue
        event = value.get("status", {}).get("vsock")
        if event is None:
            continue
        if not isinstance(event, dict) or event.get("type") != "started":
            raise VZNativeHold("native VZ vsock adapter failed or was skipped; NAT fallback forbidden")
        if event.get("hostAddr") != expected_address or event.get("vsockPort") != 22:
            raise VZNativeHold("native VZ vsock event differs from exact owned SSH endpoint")
        selected = value
    if selected is None:
        raise VZNativeHold("native VZ exact start attempt lacks genuine vsock SSH forwarder event")
    native["access"].update(host="127.0.0.1", port=port)
    return {
        "attempt_id": attempt["id"],
        "event_digest": _digest(_json(selected)),
        "host_address": expected_address,
        "log_identity": stamp[:2],
        "log_bytes": stamp[5],
        "log_digest": digest,
        "log_transition": transition,
        "event_time": selected["time"],
    }


def _publish_ssh_config(authority, native):
    access = native["access"]

    def quoted(value):
        return json.dumps(str(value), ensure_ascii=False)

    payload = (
        "Host native-vz\n"
        "  HostName 127.0.0.1\n"
        f"  Port {access['port']}\n"
        "  User root\n"
        f"  IdentityFile {quoted(access['identity'])}\n"
        f"  HostKeyAlias {quoted(access['host_alias'])}\n"
        f"  UserKnownHostsFile {quoted(access['known_hosts'])}\n"
        "  GlobalKnownHostsFile /dev/null\n"
        "  StrictHostKeyChecking yes\n"
        "  IdentitiesOnly yes\n"
        "  IdentityAgent none\n"
        "  ForwardAgent no\n"
        "  ForwardX11 no\n"
        "  ClearAllForwardings yes\n"
        "  ControlMaster no\n"
        "  ControlPath none\n"
        "  ProxyCommand none\n"
        "  ProxyJump none\n"
        "  PermitLocalCommand no\n"
    ).encode()
    authority.write_file("ssh/config", payload)
    path = authority.paths.root / "ssh" / "config"
    _, digest, stamp = _file(path, private=True, limit=65536)
    access["ssh_config"] = str(path)
    access["files"][str(path)] = {"digest": digest, "stamp": stamp, "private": True}


def _start(authority, roots, config, custody, data):
    native = data["vz_native"]
    instance = _owned_instance(config, native, custody)
    if instance.get("status") != "Stopped":
        raise VZNativeHold("native VZ start requires the exact normal Stopped provider state")
    _require_ssh_port_free(native)
    logpath = config.home / native["instance"] / "ha.stdout.log"
    oldlog = None
    if os.path.lexists(logpath):
        raw, digest, stamp = _file(logpath, owned=True, limit=8 * _MAX_JSON)
        oldlog = {"identity": stamp[:2], "size": len(raw), "digest": digest, "stamp": stamp}
    attempt = {"id": str(uuid.uuid4()), "started_unix": time.time(), "log": oldlog}
    native["start_attempt"] = attempt
    _write(authority, "defined", data)
    _load(authority, roots, config, custody, data=data)
    _verify_boot_copies(config, native)
    authority.verify_binding()
    _lima(config, ["start", "--tty=false", "--timeout", "180s", native["instance"]], custody, timeout=210)
    instance = _owned_instance(config, native, custody)
    _account_disks(config, native, custody, require_primary=True)
    if instance.get("status") != "Running":
        raise VZNativeHold("native VZ provider did not reach Running")
    native["vsock_forwarder"] = _vsock_event(config, native, attempt, instance)
    _publish_ssh_config(authority, native)
    before = _validate_status(native, _control(config, native, "status", custody), handed_off=False)
    native["pre_handoff_status"] = before
    resumed = _control(config, native, "resume", custody)
    if resumed.get("accepted") is not True:
        raise VZNativeHold("native VZ authenticated new-boot resume was not accepted")
    response = _control(config, native, "handoff", custody)
    if response.get("accepted") is not True or response.get("bootstrap_tcp_open") is not False:
        raise VZNativeHold("native VZ authenticated handoff was not accepted")
    status = _validate_status(native, _control(config, native, "status", custody), handed_off=True)
    data["vz_status"] = status
    return _write(authority, _status_name(status, native), data)


def _status_name(value, native):
    restart_pending = native["plan"]["restart"] == "unless-stopped" and not value.get("explicit_stop_fenced", False)
    return "exited" if value["main"]["state"] == "exited" and not restart_pending else "running"


def run_local_oci(roots: StatePaths, request: LocalOCIRunRequest, host_config=None):
    if type(request) is not LocalOCIRunRequest or request.backend != "lima-vz" or request.platform != "linux/arm64":
        raise StateError("native VZ launch requires a canonical arm64 Lima VZ request")
    request.__post_init__()
    config = VZHostConfig.from_environment() if host_config is None else host_config
    if not isinstance(config, VZHostConfig):
        raise StateError("native VZ host configuration must be typed")
    with _custody(config) as custody:
        assets, _carrier_stamp, cidata = _preflight(config, custody, request)
        # Preflight/source authority stays under namespace custody. Any run-name
        # lock needed in this opposite ordering must be try-once, never waiting
        # on a reader that already owns the run and is waiting for custody.
        init_resolved_roots(roots, _run_lock_deadline=time.monotonic())
        cas = SourceCAS(roots.oci_source_cas)
        source = (
            LocalLayoutSource(request.source, request.manifest_digest)
            if request.source.is_dir()
            else LocalArchiveSource(request.source, request.manifest_digest)
        )
        qualification = qualify_native_root_source(source, cas, platform=request.platform, backend=request.backend)
        if qualification.snapshot.image.config.process.stop_signal == 9:
            raise StateError(
                "native VZ no-force profile does not support image StopSignal SIGKILL; no signal substitution is made"
            )
        shares = _shares(request, roots, config)
        config.verify()
        ssh_local_port = _choose_ssh_port(request.network)
        mode, restart = getattr(request, "mode", "dev"), getattr(request, "restart", "no")
        if mode not in {"dev", "prod"} or restart not in {"no", "unless-stopped"}:
            raise StateError("native VZ run mode/restart policy is invalid")
        process = qualification.snapshot.image.config.process
        if request.user_override is not None:
            process = process.with_user(request.user_override)
        if request.command_override is not None:
            rawconfig = _decode(cas.read_metadata(qualification.snapshot.config))
            process_config = rawconfig.get("config")
            entrypoint, _ = image_process_vectors({} if process_config is None else process_config)
            process = process.with_command(entrypoint, request.command_override)
        if mode == "prod":
            process.require_bootable()
        if not process.bootable and not request.detached and not os.isatty(0):
            raise StateError(
                "native VZ dev without a main command needs an interactive local TTY; use --detach otherwise"
            )
        worker = VZWorker.from_environment()
        selected = materialize_vz_source(request, roots=roots, qualification=qualification, worker=worker)
        revalidate_native_root_source(qualification, cas)
        store = OCIStore(roots)
        with reserve_new_run(roots, request.name, request.dispatch_key, _lock_deadline=time.monotonic()) as authority:
            run_id = authority.record.run_id
            instance = "plm-" + run_id.replace("-", "")[:12]
            if len(os.fsencode(config.home / instance / ("ssh.sock." + "0" * 16))) >= 104:
                raise StateError("native VZ configured Lima home exceeds the Darwin socket path bound")
            owner = ArtifactLeaseOwner(run_id, request.name, "root-lower")
            native = {
                "schema": _SCHEMA,
                "host_config_path": str(config.path),
                "host_config_digest": config.digest,
                "host_authority": _host_authority(config),
                "instance": instance,
                "instance_identity": None,
                "assets_digest": assets.manifest_digest,
                "cidata_inputs": cidata,
                "ssh_local_port": ssh_local_port,
                "root_volume_id": str(uuid.uuid4()),
                "root_nonce": os.urandom(32).hex(),
                "host_shares": shares,
                "hold": None,
                "source_snapshot": _snapshot_wire(qualification.snapshot),
                "clock_policy": {
                    "host_admission": "fixed wall-clock expiry checked before effects and handoff",
                    "guest_clock": "CIDATA start timestamp; boot lag possible; not UTC attestation",
                },
            }
            data = {
                "vz_native": native,
                "oci_materialization": selected.receipt.to_dict(),
                "source_qualification": qualification.to_dict(),
                "source_image_digest": selected.receipt.source_image_digest,
                "source_snapshot_binding_digest": selected.receipt.source_snapshot_binding_digest,
                "manifest_digest": selected.receipt.manifest_digest,
                "image_reference": str(request.source),
                "memory_mib": request.memory_mib,
                "vcpus": request.vcpus,
                "network": request.network.mode,
                "ports": [port.to_dict() for port in request.network.published_ports],
                "base_arch": "aarch64",
                "layers": [{"digest": result.receipt.image_digest} for result in selected.receipt.results],
            }
            _write(authority, "creating", data)
            try:
                private = authority.paths.root / "vz-private"
                private.mkdir(mode=0o700)
                console = authority.paths.root / "console.log"
                _new_file(console, b"")
                _, console_digest, console_stamp = _file(console, private=True, limit=2 * _MAX_JSON)
                native["console_identity"] = console_stamp[:2]
                data["vz_logs"] = {
                    "source": "not-observed",
                    "capture_state": "not-observed",
                    "final": False,
                    "offsets": {"stdout": 0, "stderr": 0},
                    "streams": {},
                    "console": {"stamp": console_stamp, "sha256": console_digest, "bytes": 0},
                    "unattended_final_tail": "unverified",
                }
                _write(authority, "creating", data)
                seed = private / "seed"
                seed.mkdir(mode=0o700)
                sshdir = authority.paths.root / "ssh"
                sshdir.mkdir(mode=0o700)
                key = sshdir / "id_ed25519"
                code, _, _ = _bounded(
                    config,
                    [str(config.tools["ssh_keygen"]), "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)],
                    custody=custody,
                    operation="new-user-key",
                    timeout=20,
                )
                if code:
                    raise VZNativeHold("native VZ unique user identity generation failed")
                os.chmod(key.with_suffix(".pub"), 0o600)
                user_key = _public_key(_file(key.with_suffix(".pub"), private=True, limit=8192)[0])
                control_identity = config.home / "_config" / "user"
                _file(control_identity, private=True, limit=65536)
                control_public = _public_key(_file(config.home / "_config" / "user.pub", limit=8192)[0])
                code, actual_public, _ = _bounded(
                    config,
                    [str(config.tools["ssh_keygen"]), "-y", "-f", str(control_identity)],
                    custody=custody,
                    operation="verify-control-key",
                    timeout=20,
                )
                if code or _public_key(actual_public) != control_public:
                    raise StateError("native VZ selected campaign control key pair does not match")
                receipts = tuple(result.receipt for result in selected.receipt.results)
                graph = _digest(
                    _json(
                        {
                            "source_snapshot_binding_digest": selected.receipt.source_snapshot_binding_digest,
                            "layers": [receipt.to_dict() for receipt in receipts],
                        }
                    )
                )
                native["lease_plan_digest"] = _digest(
                    _json({"run_id": run_id, "root_graph_digest": graph, "root_volume_id": native["root_volume_id"]})
                )
                config.verify()
                lease = store.acquire_lease_set(receipts, owner, plan_digest=native["lease_plan_digest"])
                native["lease_set_id"] = lease.lease_set_id
                _write(authority, "creating", data)
                export = publish_vz_lower_export(roots, store, receipts, owner, lease)
                native.update(lower_path=str(export.root), lower_export_binding=export.binding_digest)
                _write(authority, "creating", data)
                config.verify()
                root, host_key = claim_vz_root_volume(
                    roots,
                    native["root_volume_id"],
                    size_bytes=request.root_size_bytes,
                    lower_graph_digest=graph,
                    retention_policy=request.root_retention,
                    owner=owner,
                    lima_home=config.home,
                    root_nonce=native["root_nonce"],
                    prepare_disk=worker.prepare_root_disk,
                )
                native["disk_name"] = root.record.storage.disk_name
                native["guest_host_public_key"] = host_key
                native["seed_path"] = str(seed)
                native["seed_tag"] = lima_vz_mount_tag(seed, "/.palimpsest/seed")
                plan = {
                    "schema": "palimpsest.vz-root-plan.v1",
                    "run_id": run_id,
                    "run_name": request.name,
                    "root_volume_id": native["root_volume_id"],
                    "root_graph_digest": graph,
                    "source_snapshot_binding_digest": selected.receipt.source_snapshot_binding_digest,
                    "root_filesystem_uuid": native["root_volume_id"],
                    "root_nonce": native["root_nonce"],
                    "lower_share_tag": export.tag,
                    "layers": [
                        {"ordinal": f.ordinal, "filename": f.filename, "digest": f.digest, "size": f.size}
                        for f in export.files
                    ],
                    "process": process.to_dict(),
                    "mode": mode,
                    "restart": restart,
                    "idle_grace_seconds": 300,
                    "expires_unix": config.value["expires_unix"],
                    "network": {"dns": ["8.8.8.8", "8.8.4.4"]},
                    "access": {
                        "control_authorized_key": control_public,
                        "user_authorized_key": user_key,
                        "host_public_key": host_key,
                    },
                    "host_shares": [
                        {"tag": s["tag"], "mount_path": s["mount_path"], "read_only": s["read_only"]} for s in shares
                    ],
                }
                plan_bytes = _json(plan)
                if len(plan_bytes) > _MAX_JSON:
                    raise StateError("native VZ authenticated seed plan exceeds one MiB")
                native["plan"] = plan
                native["plan_sha256"] = hashlib.sha256(plan_bytes).hexdigest()
                _new_file(seed / "plan.json", plan_bytes, 0o400)
                native["seed_identity"] = [seed.lstat().st_dev, seed.lstat().st_ino]
                native["seed_stamp"] = _file(seed / "plan.json", private=True, limit=_MAX_JSON)[2]
                alias = "plm-vz-" + run_id
                known_hosts = sshdir / "known_hosts"
                _new_file(known_hosts, (alias + " " + host_key + "\n").encode())
                native["access"] = {
                    "identity": str(key),
                    "control_identity": str(control_identity),
                    "known_hosts": str(known_hosts),
                    "host_alias": alias,
                    "host": None,
                    "port": None,
                    "files": {},
                }
                control_public_path = config.home / "_config" / "user.pub"
                for path in (key, key.with_suffix(".pub"), known_hosts, control_identity, control_public_path):
                    is_private = path != control_public_path
                    _, digest, stamp = _file(path, private=is_private, owned=True, limit=65536 if is_private else 8192)
                    native["access"]["files"][str(path)] = {"digest": digest, "stamp": stamp, "private": is_private}
                carrier = private / "carrier.img"
                source_path = _path(config.value["carrier"]["path"])
                _copy_verified_boot_input(source_path, carrier, config.value["carrier"]["sha256"])
                _, copied_digest, copied_stamp = _file(carrier, private=True, capture=False)
                native["carrier"] = {"path": str(carrier), "digest": copied_digest, "stamp": copied_stamp}
                rendered = _render_config(config, request, native, assets, carrier)
                configpath = authority.paths.root / "vz-lima.yaml"
                authority.write_file("vz-lima.yaml", _json(rendered))
                _, generated_digest, generated_stamp = _file(configpath, private=True, limit=_MAX_JSON)
                if generated_digest != _digest(_json(rendered)):
                    raise VZNativeHold("native VZ generated configuration differs from the authored bytes")
                native["generated_config_path"] = str(configpath)
                native["generated_config_digest"] = generated_digest
                native["generated_config_stamp"] = list(generated_stamp)
                filled_raw = _lima(config, ["validate", "--fill", str(configpath)], custody)
                filled_path = private / ("filled-" + hashlib.sha256(filled_raw).hexdigest() + ".yaml")
                _new_file(filled_path, filled_raw)
                filled = _config_json(config, filled_path, custody)
                _verify_config(filled, rendered)
                _, effective_digest, effective_stamp = _file(filled_path, private=True, limit=_MAX_JSON)
                native["effective_config_reference"] = {
                    "path": str(filled_path),
                    "digest": effective_digest,
                    "stamp": list(effective_stamp),
                }
                native["accounting"] = {
                    "carrier_virtual_bytes": config.value["carrier_disk_bytes"],
                    "root_virtual_bytes": request.root_size_bytes,
                    "cidata_reserved_bytes": cidata["reserved_bytes"],
                    "cidata_bytes": None,
                    "attached_disk_bytes": config.value["carrier_disk_bytes"]
                    + request.root_size_bytes
                    + cidata["reserved_bytes"],
                    "limit_bytes": _DISK_LIMIT,
                    "carrier_copy_bytes": carrier.lstat().st_blocks * 512,
                    "lower_projection_bytes": export.allocated_bytes,
                    "assets_bytes": assets.kernel.lstat().st_size + assets.initramfs.lstat().st_size,
                }
                _write(authority, "creating", data)
                if instance in _instances(config, custody) or os.path.lexists(config.home / instance):
                    raise VZNativeHold("native VZ target instance already exists; adoption forbidden")
                _load(authority, roots, config, custody, data=data)
                authority.verify_binding()
                _, actual_digest, actual_stamp = _file(configpath, private=True, limit=_MAX_JSON)
                if (
                    actual_digest != native["generated_config_digest"]
                    or list(actual_stamp) != native["generated_config_stamp"]
                ):
                    raise VZNativeHold("native VZ generated provider input changed before create")
                _lima(config, ["create", "--tty=false", "--name", instance, str(configpath)], custody, timeout=90)
                directory = config.home / instance
                fd = _directory(directory)
                try:
                    info = os.fstat(fd)
                    native["instance_identity"] = [info.st_dev, info.st_ino]
                finally:
                    os.close(fd)
                _, digest, persisted_stamp = _file(directory / "lima.yaml", owned=True, limit=_MAX_JSON)
                _verify_config(_config_json(config, directory / "lima.yaml", custody, private=False), rendered)
                native["persisted_config_digest"] = digest
                native["persisted_config_stamp"] = list(persisted_stamp)
                _write(authority, "defined", data)
                _admit_created_boot_copies(authority, config, data, assets, custody)
                _account_disks(config, native, custody)
                _write(authority, "defined", data)
                _start(authority, roots, config, custody, data)
                record = _record(authority)
            except BaseException:
                _hold(authority, data, "launch-incomplete")
                raise
    from .inventory import record_oci_materialization

    try:
        record_oci_materialization(roots, qualification.snapshot, selected.receipt)
    except Exception as error:
        raise VZCatalogRegistrationError(record) from error
    session = None
    if not request.detached:
        session = (
            _session(request.name, roots=roots, expected=record, follow=True)
            if process.bootable
            else shell_session(request.name, roots=roots, _expected_record=record)
        )
    return OCILaunchResult(record, {"transport": "ssh-vsock", "instance": instance}, session)


def _copy_verified_boot_input(source, target, expected, *, mode=0o600):
    _, digest, before = _file(source, owned=True, capture=False)
    if digest != expected:
        raise StateError("native VZ boot input changed before copy")
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    dst = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(dst, mode)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(src, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(dst, view) :]
        os.fsync(dst)
        if (
            "sha256:" + digest.hexdigest() != expected
            or _stamp(os.fstat(src)) != before
            or _stamp(source.lstat()) != before
        ):
            raise VZNativeHold("native VZ boot input changed while copying")
    finally:
        os.close(src)
        os.close(dst)


def _admit_created_boot_copies(authority, config, data, assets, custody):
    """Seal only outputs of this successful, new create-only CLI invocation."""
    native = data["vz_native"]
    instance = _owned_instance(config, native, custody)
    if (
        instance.get("status") != "Stopped"
        or native.get("boot_copies") is not None
        or native.get("hold")
        or native.get("start_attempt")
    ):
        raise VZNativeHold("native VZ boot admission requires a new unheld never-started instance")
    if load_vz_runtime_assets(_path(config.value["assets"])).manifest_digest != assets.manifest_digest:
        raise VZNativeHold("native VZ source boot assets changed before admission")
    directory = config.home / native["instance"]
    instance_fd = _directory(directory)
    disk_fd = None
    try:
        opened = os.fstat(instance_fd)
        if [opened.st_dev, opened.st_ino] != native["instance_identity"]:
            raise VZNativeHold("native VZ instance changed before boot admission")
        if {"image", "iso", "diffdisk", "basedisk", "cidata.iso"}.intersection(os.listdir(instance_fd)):
            raise VZNativeHold("native VZ new provider instance contains unexpected boot inputs")
        size = config.value["carrier_disk_bytes"]
        if size + native["accounting"]["root_virtual_bytes"] + native["cidata_inputs"]["reserved_bytes"] > _DISK_LIMIT:
            raise StateError("native VZ aggregate disk reservation exceeds 32 GiB")
        carrier = _path(native["carrier"]["path"])
        _, digest, stamp = _file(carrier, private=True, capture=False)
        if digest != native["carrier"]["digest"] or stamp != native["carrier"]["stamp"]:
            raise VZNativeHold("native VZ pinned carrier changed before provider-output comparison")
        source_info = _disk_info(config, carrier, custody)
        if source_info["virtual-size"] > size:
            raise VZNativeHold("native VZ source carrier exceeds the primary reservation")
        disk_fd = os.open("disk", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=instance_fd)
        current = os.fstat(disk_fd)
        visible = os.stat("disk", dir_fd=instance_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) not in {0o600, 0o644}
            or current.st_size != size
            or _stamp(current) != _stamp(visible)
        ):
            raise VZNativeHold("native VZ provider primary shape differs from its exact reservation")
        primary = directory / "disk"
        actual_info = _disk_info(config, primary, custody)
        if actual_info["format"] != "raw" or actual_info["virtual-size"] != size:
            raise VZNativeHold("native VZ provider primary is not the exact-sized raw attachment")
        code, _, _ = _bounded(
            config,
            [
                str(config.tools["qemu_img"]),
                "compare",
                "-f",
                source_info["format"],
                "-F",
                "raw",
                str(carrier),
                str(primary),
            ],
            custody=custody,
            operation="compare-provider-raw-primary",
            timeout=180,
        )
        if code:
            raise VZNativeHold("native VZ provider primary differs from its pinned zero-extended carrier")
        if (
            _stamp(os.fstat(disk_fd)) != _stamp(current)
            or _stamp(primary.lstat()) != _stamp(current)
            or _stamp(carrier.lstat()) != native["carrier"]["stamp"]
        ):
            raise VZNativeHold("native VZ primary or carrier changed during semantic comparison")
        record = {
            "schema": "palimpsest.vz-boot-copies.v1",
            "phase": "ready",
            "run_id": authority.record.run_id,
            "plan_sha256": native["plan_sha256"],
            "assets_digest": assets.manifest_digest,
            "instance_identity": native["instance_identity"],
            "files": {},
            "primary": {
                "phase": "ready",
                "identity": [current.st_dev, current.st_ino],
                "format": "raw",
                "virtual_bytes": size,
                "mode": stat.S_IMODE(current.st_mode),
                "source_digest": native["carrier"]["digest"],
                "source_virtual_bytes": source_info["virtual-size"],
                "producer": "lima-create-prepare",
                "producer_digest": config.value["limactl"]["sha256"],
                "comparison_tool_digest": config.value["qemu_img"]["sha256"],
                "comparison": "equal-with-zero-extension",
            },
        }
        image = _rendered_config(native)["images"][0]
        expected_files = {
            "kernel": (image["kernel"]["digest"], 0o400),
            "initrd": (image["initrd"]["digest"], 0o400),
            "kernel.cmdline": (_digest(image["kernel"]["cmdline"].encode()), 0o644),
        }
        for filename, (expected, mode) in expected_files.items():
            _, digest, stamp = _file(directory / filename, owned=True, capture=False)
            if digest != expected or stat.S_IMODE(stamp[3]) != mode:
                raise VZNativeHold("native VZ provider boot input differs from its authored asset or mode")
            record["files"][filename] = {"digest": digest, "stamp": stamp}
        native["primary_disk_identity"] = record["primary"]["identity"]
        native["boot_copies"] = record
        native["accounting"].update(
            primary_preparation_allocated_bytes=current.st_blocks * 512,
            boot_copies_allocated_bytes=sum((directory / name).lstat().st_blocks * 512 for name in record["files"]),
        )
        _verify_boot_copies(config, native)
        _write(authority, "defined", data)
    finally:
        if disk_fd is not None:
            os.close(disk_fd)
        os.close(instance_fd)


def _verify_boot_copies(config, native):
    """Reopen retained real LinuxBootLoader inputs; never repair or regenerate."""
    record = native.get("boot_copies")
    if (
        not isinstance(record, dict)
        or record.get("schema") != "palimpsest.vz-boot-copies.v1"
        or record.get("phase") != "ready"
        or record.get("run_id") != native["plan"]["run_id"]
        or record.get("plan_sha256") != native["plan_sha256"]
        or record.get("assets_digest") != native["assets_digest"]
        or record.get("instance_identity") != native["instance_identity"]
        or set(record.get("files", {})) != {"kernel", "initrd", "kernel.cmdline"}
    ):
        raise VZNativeHold("native VZ retained boot-copy authority is incomplete or changed")
    directory = config.home / native["instance"]
    parent = _directory(directory)
    primary_fd = None
    try:
        info = os.fstat(parent)
        if [info.st_dev, info.st_ino] != native["instance_identity"]:
            raise VZNativeHold("native VZ boot-copy directory identity changed")
        if {"image", "iso", "diffdisk", "basedisk"}.intersection(os.listdir(parent)):
            raise VZNativeHold("native VZ instance contains unapproved image, installer or legacy disk inputs")
        image = _rendered_config(native)["images"][0]
        expected = {
            "kernel": image["kernel"]["digest"],
            "initrd": image["initrd"]["digest"],
            "kernel.cmdline": _digest(image["kernel"]["cmdline"].encode()),
        }
        for filename, digest in expected.items():
            _, actual, stamp = _file(directory / filename, owned=True, capture=False)
            if (
                actual != digest
                or actual != record["files"][filename]["digest"]
                or stamp != record["files"][filename]["stamp"]
            ):
                raise VZNativeHold("native VZ actual retained LinuxBootLoader input changed")
        primary = record.get("primary")
        if (
            not isinstance(primary, dict)
            or primary.get("phase") != "ready"
            or primary.get("format") != "raw"
            or primary.get("identity") != native["primary_disk_identity"]
            or primary.get("virtual_bytes") != config.value["carrier_disk_bytes"]
            or primary.get("source_digest") != native["carrier"]["digest"]
            or primary.get("producer") != "lima-create-prepare"
            or primary.get("producer_digest") != config.value["limactl"]["sha256"]
            or primary.get("comparison_tool_digest") != config.value["qemu_img"]["sha256"]
            or primary.get("comparison") != "equal-with-zero-extension"
        ):
            raise VZNativeHold("native VZ retained raw-primary conversion binding changed")
        primary_fd = os.open("disk", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(primary_fd)
        visible = os.stat("disk", dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or primary.get("mode") not in {0o600, 0o644}
            or stat.S_IMODE(info.st_mode) != primary["mode"]
            or info.st_size != primary["virtual_bytes"]
            or [info.st_dev, info.st_ino] != primary["identity"]
            or _stamp(info) != _stamp(visible)
        ):
            raise VZNativeHold("native VZ retained raw-primary identity or size changed")
        if (
            info.st_size + native["accounting"]["root_virtual_bytes"] + native["cidata_inputs"]["reserved_bytes"]
            > _DISK_LIMIT
        ):
            raise VZNativeHold("native VZ retained attached-disk reservation exceeds 32 GiB")
    finally:
        if primary_fd is not None:
            os.close(primary_fd)
        os.close(parent)


def _account_disks(config, native, custody, *, require_primary=False):
    directory = config.home / native["instance"]
    primary = directory / "disk"
    accounting = native["accounting"]
    virtual_bytes = accounting["carrier_virtual_bytes"]
    if os.path.lexists(primary):
        fd = os.open(primary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise VZNativeHold("native VZ primary disk file authority is unsafe")
            identity = [metadata.st_dev, metadata.st_ino]
            if native.get("primary_disk_identity") is not None and identity != native["primary_disk_identity"]:
                raise VZNativeHold("native VZ primary disk identity changed")
            native["primary_disk_identity"] = identity
        finally:
            os.close(fd)
        info = _disk_info(config, primary, custody)
        if info["format"] != "raw" or info["virtual-size"] != config.value["carrier_disk_bytes"]:
            raise VZNativeHold("native VZ actual primary format or virtual size changed before start")
        virtual_bytes = info["virtual-size"]
    elif require_primary or native.get("primary_disk_identity") is not None:
        raise VZNativeHold("native VZ retained primary disk is missing; regeneration forbidden")
    cidata = directory / "cidata.iso"
    cidata_bytes = accounting["cidata_reserved_bytes"]
    if os.path.lexists(cidata):
        _, _, cidata_stamp = _file(cidata, owned=True, limit=accounting["cidata_reserved_bytes"], capture=False)
        cidata_bytes = cidata_stamp[5]
    total = virtual_bytes + accounting["root_virtual_bytes"] + cidata_bytes
    if total > _DISK_LIMIT:
        raise VZNativeHold("native VZ all-attached-disk aggregate exceeds 32 GiB")
    accounting.update(
        cidata_bytes=cidata_bytes,
        attached_disk_bytes=total,
        primary_observed=native.get("primary_disk_identity") is not None,
    )


def _config_for(authority):
    native = authority.mutable_state().get("vz_native", {})
    return _bound_host_config(native)


def _lifecycle_result(authority, operation, previous, status):
    policy = "instance-root-and-topology-retained" if status != "removed" else None
    if status == "removed" and authority.snapshot.state.get("vz_native", {}).get("removed_root_retained") is False:
        policy = "instance-root-and-topology-purged"
    return LifecycleResult(
        authority.record,
        operation,
        previous,
        status,
        LifecycleCursor(authority.record, authority.snapshot.state["lifecycle_revision"]),
        storage_policy=policy,
    )


def _retained_stopped_observation(authority, roots, config, custody, data, *, deadline=None):
    """Prove normal provider/actor/upper absence without issuing stop or repair."""
    native = data["vz_native"]
    directory = config.home / native["instance"]

    def provider():
        instance = _owned_instance(config, native, custody, allow_expired=True, deadline=deadline)
        if (
            instance.get("status") != "Stopped"
            or instance.get("dir") != str(directory)
            or any(
                type(instance.get(key, 0)) is not int or instance.get(key, 0) != 0
                for key in ("hostAgentPID", "driverPID")
            )
            or instance.get("autoStartedIdentifier") not in (None, "")
        ):
            raise VZNativeHold("native VZ retained stop lacks normal Stopped provider/actor absence")

    def actors():
        if any(
            actor.pid != os.getpid() and any(arg == native["instance"] or str(directory) in arg for arg in actor.argv)
            for actor in list_processes()
        ):
            raise VZNativeHold("native VZ retained stop has a related live or unidentified actor")
        fd = _directory(directory)
        try:
            if list(_stamp(os.fstat(fd))[:2]) != native["instance_identity"]:
                raise VZNativeHold("native VZ retained stop directory identity changed")
            for name in os.listdir(fd):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISSOCK(info.st_mode) or name.endswith((".sock", ".pid")) or name == "sock":
                    raise VZNativeHold("native VZ retained stop has unsettled PID/socket custody")
        finally:
            os.close(fd)
        plist = config.home / "Library" / "LaunchAgents" / ("io.lima-vm.autostart." + native["instance"] + ".plist")
        if _failed_start_path_exists(plist):
            raise VZNativeHold("native VZ retained stop has launchd autostart custody")
        root = load_vz_root_volume(
            roots,
            native["root_volume_id"],
            owner=ArtifactLeaseOwner(authority.record.run_id, authority.record.name, "root-lower"),
        )
        _require_stopped_root_exclusive(root.record.storage.directory)

    provider()
    actors()
    authority.verify_binding()
    provider()
    actors()
    provider()
    _observation_timeout(deadline)
    return {
        "provider_status": "Stopped",
        "instance_identity": list(native["instance_identity"]),
        "actors_absent": True,
        "upper_exclusive": True,
        "observed_unix": time.time(),
    }


def _stop_settlement(authority, config, native):
    recovery = native.get("stop_settlement")
    if recovery is None:
        return None
    marker = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
    if (
        not isinstance(recovery, dict)
        or set(recovery)
        != {
            "phase",
            "run_id",
            "start_attempt",
            "plan_sha256",
            "marker_path",
            "marker_digest",
            "marker_stamp",
            "observation",
        }
        or recovery.get("phase") not in {"verified", "settled"}
        or recovery.get("run_id") != authority.record.run_id
        or not isinstance(native.get("start_attempt"), dict)
        or recovery.get("start_attempt") != native["start_attempt"]
        or recovery.get("plan_sha256") != native["plan_sha256"]
        or recovery.get("marker_path") != str(marker)
        or not isinstance(recovery.get("marker_stamp"), list)
        or len(recovery["marker_stamp"]) != 8
        or any(type(item) is not int for item in recovery["marker_stamp"])
        or not isinstance(recovery.get("marker_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", recovery["marker_digest"]) is None
    ):
        raise VZNativeHold("native VZ ordinary stop settlement journal changed")
    observation = recovery["observation"]
    if (
        not isinstance(observation, dict)
        or observation.get("provider_status") != "Stopped"
        or observation.get("instance_identity") != native["instance_identity"]
        or observation.get("actors_absent") is not True
        or observation.get("upper_exclusive") is not True
    ):
        raise VZNativeHold("native VZ ordinary stop settlement observation changed")
    return recovery


def _ordinary_stop_hold(authority, config, data):
    """Bind only an ordinary stop's original HOLD, never failed launch authority."""
    native = data["vz_native"]
    status = authority.snapshot.state["status"]
    recovery = _stop_settlement(authority, config, native)
    verified = isinstance(recovery, dict) and recovery.get("phase") == "verified"
    if (
        _FAILED_START_CLEANUP.get() is not None
        or native.get("instance") != "plm-" + authority.record.run_id.replace("-", "")[:12]
        or not isinstance(native.get("start_attempt"), dict)
        or "failed_start_cleanup" in native
        or not (
            status == "failed"
            and native.get("hold") == "stop-incomplete"
            or verified
            and status == "stopped"
            and native.get("hold") is None
        )
    ):
        raise VZNativeHold("native VZ settlement requires exact ordinary stop-incomplete authority")
    _verify_host_authority(config, native)
    status_value = _validate_status(native, data.get("vz_status"), handed_off=True, allow_stopping=True)
    if (
        status_value["phase"] != "stop-ready"
        or status_value.get("explicit_stop_fenced") is not True
        or status_value["user_session_count"] != 0
        or status_value["main"]["state"] not in {"exited", "absent", "not-started"}
        or status_value["main"].get("output_complete") is not True
    ):
        raise VZNativeHold("native VZ ordinary stop lacks persisted authenticated quiescence")
    marker = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
    if recovery is not None and not verified:
        raise VZNativeHold("native VZ ordinary stop settlement is already complete")
    if not _failed_start_path_exists(marker):
        if verified and status == "stopped" and native.get("hold") is None:
            return None  # Durable verified settlement preceded its exact marker release.
        raise VZNativeHold("native VZ ordinary stop original HOLD is absent")
    value, digest, stamp = _resource_hold_marker(marker)
    if value != {"schema": _SCHEMA, "run_id": authority.record.run_id, "reason": "stop-incomplete"}:
        raise VZNativeHold("native VZ ordinary stop original HOLD differs")
    if verified and (digest != recovery["marker_digest"] or stamp != recovery["marker_stamp"]):
        raise VZNativeHold("native VZ ordinary stop HOLD changed after verification")
    authority.verify_binding()
    return marker, digest, stamp


def _settle_ordinary_stop(authority, roots, config, data):
    marker = _ordinary_stop_hold(authority, config, data)
    with _custody(config, cleanup_hold=marker) as custody:
        _load_checked(authority, roots, config, custody, allow_expired=True, data=data, stopped_hold=True)
        observation = _retained_stopped_observation(authority, roots, config, custody, data)
        _load_checked(authority, roots, config, custody, allow_expired=True, data=data, stopped_hold=True)
        observation = _retained_stopped_observation(authority, roots, config, custody, data)
        if _ordinary_stop_hold(authority, config, data) != marker:
            raise VZNativeHold("native VZ ordinary stop HOLD changed during observation")
        native = data["vz_native"]
        if marker is not None:
            path, digest, stamp = marker
            native["stop_settlement"] = {
                "phase": "verified",
                "run_id": authority.record.run_id,
                "start_attempt": native["start_attempt"],
                "plan_sha256": native["plan_sha256"],
                "marker_path": str(path),
                "marker_digest": digest,
                "marker_stamp": list(stamp),
                "observation": observation,
            }
        native["hold"] = None
        _write(authority, "stopped", data)
        if marker is not None:
            if _ordinary_stop_hold(authority, config, data) != marker:
                raise VZNativeHold("native VZ ordinary stop HOLD changed before release")
            parent = _directory(custody)
            try:
                if _stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != stamp:
                    raise VZNativeHold("native VZ ordinary stop HOLD identity changed before release")
                authority.verify_binding()
                os.unlink(path.name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        native["stop_settlement"]["phase"] = "settled"
        return _write(authority, "stopped", data)


def start_oci_run(roots, name, *, expected_record=None):
    with locked_existing_run(roots, name, expected=expected_record) as authority:
        config = _config_for(authority)
        data = _data(authority)
        recovery = _stop_settlement(authority, config, data.get("vz_native", {}))
        if recovery is not None:
            marker = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
            if (
                recovery["phase"] != "settled"
                or authority.snapshot.state["status"] != "stopped"
                or data["vz_native"].get("hold") is not None
                or _failed_start_path_exists(marker)
            ):
                raise VZNativeHold("native VZ start requires completed ordinary stop settlement")
        with _custody(config) as custody:
            data, *_ = _load(authority, roots, config, custody, data=data)
            previous = authority.snapshot.state["status"]
            _require_ssh_port_free(data["vz_native"])
            if previous == "exited" and data["vz_native"]["plan"]["mode"] == "dev":
                _validate_status(data["vz_native"], data.get("vz_status"), handed_off=True, allow_stopping=True)
                _retained_stopped_observation(authority, roots, config, custody, data)
                _write(authority, "stopped", data)
            elif previous != "stopped":
                raise StateError("native VZ start requires its retained stopped run")
            try:
                data["vz_native"].pop("stop_settlement", None)
                _account_disks(config, data["vz_native"], custody)
                result = _start(authority, roots, config, custody, data)
                return _lifecycle_result(authority, RuntimeOperation.START, previous, result["status"])
            except _VZStartAdmissionError:
                raise
            except BaseException:
                _hold(authority, data, "start-incomplete")
                raise


def _stop(authority, roots, config, custody, data):
    native = data["vz_native"]
    instance = _owned_instance(config, native, custody, allow_expired=True)
    if instance.get("status") == "Stopped":
        _retained_stopped_observation(authority, roots, config, custody, data)
        return _write(authority, "stopped", data)
    if instance.get("status") != "Running":
        raise VZNativeHold("native VZ stop requires exact Running or Stopped state")
    response = _control(config, native, "stop", custody, allow_expired=True)
    if response.get("accepted") is not True or response.get("phase") not in {"stopping", "stop-ready"}:
        raise VZNativeHold("native VZ authenticated graceful stop was not accepted")
    deadline = time.monotonic() + 60
    while True:
        status = _validate_status(
            native,
            _control(config, native, "status", custody, allow_expired=True),
            handed_off=True,
            allow_stopping=True,
        )
        data["vz_status"] = status
        if status["phase"] == "stop-ready":
            if status["user_session_count"] != 0 or status["main"]["state"] == "running":
                raise VZNativeHold("native VZ stop-ready has live workload/session ownership")
            break
        if time.monotonic() >= deadline:
            raise VZNativeHold("native VZ graceful workload quiescence timed out; no force issued")
        time.sleep(0.25)
    _capture_logs(authority, config, data, custody, final=True)
    _write(authority, "running", data)
    response = _control(config, native, "poweroff", custody, allow_expired=True)
    if response.get("accepted") is not True or response.get("phase") != "powering-off":
        raise VZNativeHold("native VZ authenticated poweroff was not accepted")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        instance = _owned_instance(config, native, custody, allow_expired=True, deadline=deadline)
        if instance.get("status") == "Stopped":
            _retained_stopped_observation(authority, roots, config, custody, data, deadline=deadline)
            return _write(authority, "stopped", data)
        # Lima2.2.1 may report Broken while its VZ/hostagent actors exit.
        # This is an observation window after authenticated poweroff, NOT a
        # stopped classification or permission to issue another operation.
        observed = instance.get("status")
        if observed not in {"Running", "Broken"}:
            detail = repr(observed[:80]) if isinstance(observed, str) else type(observed).__name__
            raise VZNativeHold("native VZ orderly stop entered unknown provider state: " + detail)
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    raise VZNativeHold("native VZ orderly stop did not reach Stopped; no force issued")


def _failed_start_recovery_bound(native, guard):
    recovery = native.get("failed_start_cleanup")
    return (
        isinstance(recovery, dict)
        and recovery.get("start_attempt") == native["start_attempt"]
        and recovery.get("actor") == _failed_start_actor_wire(guard.actor)
        and "delete" not in recovery
    )


def _failed_start_settled(recovery):
    if not isinstance(recovery, dict):
        return False
    if recovery.get("stop") == "settled":
        return not any(key in recovery for key in ("termination", "forced_termination"))
    termination = recovery.get("termination")
    if recovery.get("stop") not in {"attempted", "returned"} or not isinstance(termination, dict):
        return False
    if termination.get("signal") != "SIGTERM":
        return False
    if termination.get("phase") == "settled":
        return "forced_termination" not in recovery
    forced = recovery.get("forced_termination")
    return (
        termination.get("phase") == "sent"
        and isinstance(forced, dict)
        and forced.get("signal") == "SIGKILL"
        and forced.get("phase") == "settled"
    )


def _reconcile_failed_start_stale_endpoints(authority, roots, config, custody, data):
    guard = _require_failed_start_operation(RuntimeOperation.STOP)
    native = data["vz_native"]
    forced = native["failed_start_cleanup"]["forced_termination"]
    if guard.signal_request is not signal.SIGKILL or "stale_cleanup" in forced:
        raise VZNativeHold("native VZ stale endpoint cleanup cannot be replayed")
    before = _failed_start_observation(
        authority, roots, config, custody, data, phase="after-stop", absent=True, allow_stale=True
    )
    _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
    entries = before["stale_endpoints"]
    forced["stale_cleanup"] = {"phase": "attempted", "entries": entries, "removed": []}
    _write(authority, "failed", data)
    directory = config.home / native["instance"]
    fd = _directory(directory)
    try:
        if list(_stamp(os.fstat(fd))[:2]) != native["instance_identity"]:
            raise VZNativeHold("native VZ stale endpoint directory identity changed")
        for name, expected in sorted(entries.items()):
            _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
            current = _failed_start_observation(
                authority, roots, config, custody, data, phase="after-stop", absent=True, allow_stale=True
            )
            remaining = {key: value for key, value in entries.items() if key not in forced["stale_cleanup"]["removed"]}
            if current["stale_endpoints"] != remaining:
                raise VZNativeHold("native VZ stale endpoint set changed before unlink")
            authority.verify_binding()
            guard.check_deadline()
            pin = _failed_start_endpoint_pin(
                directory,
                name,
                os.stat(name, dir_fd=fd, follow_symlinks=False),
                guard,
                inactive=True,
                original=expected,
            )
            current_actor = read_process(guard.actor.pid)
            _require_failed_start_processes(
                guard.actor, current_actor, list_processes(), native["instance"], directory, absent=True
            )
            visible_fd = _directory(directory)
            try:
                if (
                    _stamp(os.fstat(visible_fd))[:2] != _stamp(os.fstat(fd))[:2]
                    or list(_stamp(os.fstat(fd))[:2]) != native["instance_identity"]
                ):
                    raise VZNativeHold("native VZ stale unlink directory no longer matches visible authority")
            finally:
                os.close(visible_fd)
            if read_process(guard.actor.pid) is not None:
                raise VZNativeHold("native VZ original PID no longer proves absence before stale unlink")
            if list(_stamp(os.stat(name, dir_fd=fd, follow_symlinks=False))) != pin["stamp"]:
                raise VZNativeHold("native VZ stale endpoint changed before unlink")
            guard.check_deadline()
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
            forced["stale_cleanup"]["removed"].append(name)
            _write(authority, "failed", data)
    finally:
        os.close(fd)
    guard.check_deadline()
    forced["stale_cleanup"]["phase"] = "settled"
    _write(authority, "failed", data)


def _failed_start_signal(authority, roots, config, custody, data):
    """A separately authorized one-shot exception, never a normal-stop fallback."""
    guard = _require_failed_start_operation(RuntimeOperation.STOP)
    requested = guard.signal_request
    if requested not in {signal.SIGTERM, signal.SIGKILL}:
        raise VZNativeHold("native VZ signal requires its separate exact-action approval")
    native = data["vz_native"]
    recovery = native.get("failed_start_cleanup")
    force = requested is signal.SIGKILL
    key = "forced_termination" if force else "termination"
    if (
        not _failed_start_recovery_bound(native, guard)
        or recovery.get("stop") not in {"attempted", "returned"}
        or key in recovery
        or force
        and (
            not isinstance(recovery.get("termination"), dict)
            or recovery["termination"].get("signal") != "SIGTERM"
            or recovery["termination"].get("phase") != "sent"
        )
    ):
        raise VZNativeHold("native VZ signal requires an unreplayed exact failed shutdown phase")
    before = _failed_start_observation(authority, roots, config, custody, data, phase="before-stop", absent=False)
    _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
    recovery[key] = {"signal": requested.name, "phase": "attempted", "before": before}
    if force:
        recovery[key]["before_endpoints"] = _failed_start_endpoint_pins(config, native, guard)
    _write(authority, "failed", data)
    _failed_start_observation(authority, roots, config, custody, data, phase="before-stop", absent=False)
    if force and _failed_start_endpoint_pins(config, native, guard) != recovery[key]["before_endpoints"]:
        raise VZNativeHold("native VZ original endpoints changed before single forced signal")
    authority.verify_binding()
    guard.check_deadline()
    if read_process(guard.actor.pid) != guard.actor:
        raise VZNativeHold("native VZ original actor changed before single signal")
    guard.check_deadline()
    os.kill(guard.actor.pid, requested)
    recovery[key]["phase"] = "sent"
    _write(authority, "failed", data)
    settle_deadline = min(time.monotonic() + 15, guard.monotonic_deadline)
    while True:
        current = read_process(guard.actor.pid)
        if current is None:
            break
        if current != guard.actor:
            raise VZNativeHold("native VZ original PID was reused after single signal")
        guard.check_deadline()
        if time.monotonic() >= settle_deadline:
            raise VZNativeHold("native VZ single signal did not settle its actor; no retry")
        time.sleep(min(0.1, max(0, settle_deadline - time.monotonic())))
    if force:
        _reconcile_failed_start_stale_endpoints(authority, roots, config, custody, data)
    after = _failed_start_observation(authority, roots, config, custody, data, phase="after-stop", absent=True)
    if _owned_instance(config, native, custody, allow_expired=True).get("status") != "Stopped":
        raise VZNativeHold("native VZ single signal did not settle its provider")
    _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
    guard.check_deadline()
    recovery[key].update(phase="settled", after=after)
    _write(authority, "failed", data)


def _failed_start_stop(authority, roots, config, custody, data):
    native = data["vz_native"]
    recovery = native.get("failed_start_cleanup")
    if recovery is not None:
        guard = _FAILED_START_CLEANUP.get()
        if not _failed_start_recovery_bound(native, guard):
            raise VZNativeHold("native VZ failed-start recovery identity changed")
        reconcile_returned = (
            recovery.get("stop") == "returned"
            and guard.signal_request is None
            and not any(key in recovery for key in ("termination", "forced_termination"))
        )
        if not reconcile_returned and not _failed_start_settled(recovery):
            raise VZNativeHold("native VZ failed-start stop was already attempted; blind replay forbidden")
        _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
        after = _failed_start_observation(
            authority,
            roots,
            config,
            custody,
            data,
            phase="after-stop" if reconcile_returned else "stopped",
            absent=True,
        )
        guard.check_deadline()
        if reconcile_returned or not any(key in recovery for key in ("termination", "forced_termination")):
            recovery.update(stop="settled", after_stop=after)
            _write(authority, "failed", data)
        return
    before = _failed_start_observation(authority, roots, config, custody, data, phase="before-stop", absent=False)
    _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
    native["failed_start_cleanup"] = {
        "start_attempt": _decode(_json(native["start_attempt"])),
        "actor": _failed_start_actor_wire(_FAILED_START_CLEANUP.get().actor),
        "stop": "attempted",
        "before_stop": before,
    }
    _write(authority, "failed", data)
    _failed_start_observation(authority, roots, config, custody, data, phase="before-stop", absent=False)
    authority.verify_binding()
    _lima(
        config,
        ["stop", "--tty=false", native["instance"]],
        custody,
        timeout=200,
        allow_expired=True,
    )
    native["failed_start_cleanup"]["stop"] = "returned"
    _write(authority, "failed", data)
    settle_deadline = min(time.monotonic() + 10, _FAILED_START_CLEANUP.get().monotonic_deadline)
    while True:
        current = read_process(_FAILED_START_CLEANUP.get().actor.pid)
        if current is None:
            break
        if current != _FAILED_START_CLEANUP.get().actor:
            raise VZNativeHold("native VZ original PID was reused while awaiting orderly hostagent exit")
        _FAILED_START_CLEANUP.get().check_deadline()
        if time.monotonic() >= settle_deadline:
            raise VZNativeHold("native VZ ordinary stop returned but original actor remains; HOLD retained")
        time.sleep(min(0.1, max(0, settle_deadline - time.monotonic())))
    # A CLI success is not actor settlement. Returned-but-unsettled stays held.
    _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
    after = _failed_start_observation(authority, roots, config, custody, data, phase="after-stop", absent=True)
    instance = _owned_instance(config, native, custody, allow_expired=True)
    if instance.get("status") != "Stopped":
        raise VZNativeHold("native VZ failed-start ordinary stop did not settle its provider")
    _FAILED_START_CLEANUP.get().check_deadline()
    native["failed_start_cleanup"].update(stop="settled", after_stop=after)
    _write(authority, "failed", data)


def stop_oci_run(roots, name, *, expected_record=None):
    with locked_existing_run(roots, name, expected=expected_record) as authority:
        config = _config_for(authority)
        data = _data(authority)
        failed_start = (
            data.get("vz_native", {}).get("hold") == "launch-incomplete" and "start_attempt" in data["vz_native"]
        )
        guard = _FAILED_START_CLEANUP.get()
        if guard is not None:
            _require_failed_start_operation(RuntimeOperation.STOP)
            if guard.run_id != authority.record.run_id or not failed_start:
                raise VZNativeHold("native VZ cleanup scope cannot stop a different or non-held run")
        native = data.get("vz_native", {})
        recovery = _stop_settlement(authority, config, native)
        if native.get("hold") == "stop-incomplete" or (
            isinstance(recovery, dict) and recovery.get("phase") == "verified"
        ):
            previous = authority.snapshot.state["status"]
            result = _settle_ordinary_stop(authority, roots, config, data)
            return _lifecycle_result(authority, RuntimeOperation.STOP, previous, result["status"])
        if failed_start:
            _require_failed_start_operation(RuntimeOperation.STOP)
            marker = _failed_start_cleanup_hold(authority, config, data)
            with _custody(config, cleanup_hold=marker) as custody:
                _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
                if _FAILED_START_CLEANUP.get().signal_request is not None:
                    _failed_start_signal(authority, roots, config, custody, data)
                else:
                    _failed_start_stop(authority, roots, config, custody, data)
                return _lifecycle_result(authority, RuntimeOperation.STOP, "failed", "failed")
        with _custody(config) as custody:
            data, *_ = _load(authority, roots, config, custody, allow_expired=True)
            previous = authority.snapshot.state["status"]
            try:
                _stop(authority, roots, config, custody, data)
                return _lifecycle_result(authority, RuntimeOperation.STOP, previous, "stopped")
            except BaseException:
                _hold(authority, data, "stop-incomplete")
                raise


def rm_oci_run(roots, name, *, expected_record=None):
    with locked_existing_run(roots, name, expected=expected_record) as authority:
        previous = authority.snapshot.state["status"]
        if previous == "removed":
            config = _config_for(authority)
            marker = config.home / "_palimpsest-native" / ("hold-" + authority.record.run_id + ".json")
            if authority.mutable_state().get("vz_native", {}).get("hold") or os.path.lexists(marker):
                raise VZNativeHold("native VZ removed run retains HOLD; final cleanup settlement is unknown")
            return _lifecycle_result(authority, RuntimeOperation.RM, previous, "removed")
        config = _config_for(authority)
        data = _data(authority)
        failed_start = (
            data.get("vz_native", {}).get("hold") == "launch-incomplete" and "start_attempt" in data["vz_native"]
        )
        cleanup_only = bool(data.get("vz_native", {}).get("hold")) and not failed_start
        if failed_start:
            _require_failed_start_operation(RuntimeOperation.RM)
            if not _failed_start_settled(data["vz_native"].get("failed_start_cleanup")):
                raise VZNativeHold("native VZ failed-start removal requires separately settled actor shutdown")
        cleanup_hold = (
            _failed_start_cleanup_hold(authority, config, data)
            if failed_start
            else _prestart_cleanup_hold(authority, config, data)
            if cleanup_only
            else None
        )
        with _custody(config, cleanup_hold=cleanup_hold) as custody:
            loader = _load_checked if failed_start else _load
            options = {"failed_start": True} if failed_start else {"cleanup_only": cleanup_only}
            data, store, owner, lease, receipts, export = loader(
                authority, roots, config, custody, allow_expired=True, data=data, **options
            )
            native = data["vz_native"]
            try:
                if failed_start:
                    _failed_start_stop(authority, roots, config, custody, data)
                elif not cleanup_only:
                    _stop(authority, roots, config, custody, data)
                instance = _owned_instance(config, native, custody, allow_expired=True)
                if instance.get("status") != "Stopped":
                    raise VZNativeHold("native VZ removal requires exact normal Stopped state")
                if failed_start:
                    if _failed_start_cleanup_hold(authority, config, data) != cleanup_hold:
                        raise VZNativeHold("native VZ original launch HOLD changed before deletion")
                    before_delete = _failed_start_observation(
                        authority, roots, config, custody, data, phase="before-delete", absent=True
                    )
                    if "default_network_endpoints" in before_delete:
                        native["failed_start_cleanup"]["after_stop"] = before_delete
                    _load_checked(authority, roots, config, custody, allow_expired=True, data=data, failed_start=True)
                    native["failed_start_cleanup"]["delete"] = "attempted"
                    _write(authority, "failed", data)
                    before_delete = _failed_start_observation(
                        authority, roots, config, custody, data, phase="before-delete", absent=True
                    )
                    if "default_network_endpoints" in before_delete:
                        native["failed_start_cleanup"]["after_stop"] = before_delete
                        _write(authority, "failed", data)
                elif cleanup_hold is not None:
                    if _prestart_cleanup_hold(authority, config, data) != cleanup_hold:
                        raise VZNativeHold("native VZ cleanup HOLD changed before provider deletion")
                    prestart_root = load_vz_root_volume(roots, native["root_volume_id"], owner=owner)
                    _require_stopped_root_exclusive(prestart_root.record.storage.directory)
                if not failed_start:
                    _write(authority, "stopped", data)
                authority.verify_binding()
                _lima(config, ["delete", "--tty=false", native["instance"]], custody, timeout=90, allow_expired=True)
                if native["instance"] in _instances(config, custody, allow_expired=True) or os.path.lexists(
                    config.home / native["instance"]
                ):
                    raise VZNativeHold("native VZ exact provider absence was not proven")
                if failed_start:
                    _failed_start_observation(
                        authority, roots, config, custody, data, phase="after-delete", absent=True
                    )
                    native["failed_start_cleanup"]["delete"] = "settled"
                    _write(authority, "failed", data)
                root = load_vz_root_volume(roots, native["root_volume_id"], owner=owner)
                if set(os.listdir(root.record.storage.directory)) != {"datadisk"}:
                    raise VZNativeHold("native VZ named disk still has in-use/unknown consumers")
                if failed_start:
                    _FAILED_START_CLEANUP.get().check_deadline()
                release_vz_lower_export(export, store, receipts, owner, lease, provider_absent=True)
                if failed_start:
                    _FAILED_START_CLEANUP.get().check_deadline()
                store.release_lease_set(lease)
                if failed_start:
                    _FAILED_START_CLEANUP.get().check_deadline()
                retained = release_vz_root_volume(
                    roots,
                    native["root_volume_id"],
                    owner=owner,
                    lower_graph_digest=native["plan"]["root_graph_digest"],
                    provider_absent=True,
                )
                if failed_start:
                    _FAILED_START_CLEANUP.get().check_deadline()
                native["removed_root_retained"] = retained is not None
                native["hold"] = None
                _write(authority, "removed", data)
                if cleanup_hold is not None:
                    marker_path, marker_digest, marker_stamp = cleanup_hold
                    _, digest, stamp = _resource_hold_marker(marker_path)
                    if (digest, stamp) != (marker_digest, marker_stamp):
                        raise VZNativeHold("native VZ cleanup HOLD changed before final release")
                    parent = _directory(custody)
                    try:
                        visible = os.stat(marker_path.name, dir_fd=parent, follow_symlinks=False)
                        if _stamp(visible) != marker_stamp:
                            raise VZNativeHold("native VZ cleanup HOLD identity changed before final release")
                        if failed_start:
                            _FAILED_START_CLEANUP.get().check_deadline()
                        os.unlink(marker_path.name, dir_fd=parent)
                        os.fsync(parent)
                    finally:
                        os.close(parent)
                return _lifecycle_result(authority, RuntimeOperation.RM, previous, "removed")
            except BaseException:
                _hold(authority, data, "launch-incomplete" if failed_start else "removal-incomplete")
                raise


def _clear_log_append_intent(authority, path, expected):
    if _file(path, private=True, limit=_MAX_JSON)[0] != expected:
        raise VZNativeHold("native VZ log append intent changed")
    authority.verify_binding()
    parent = _directory(path.parent)
    try:
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _capture_logs(authority, config, data, custody, *, final):
    native = data["vz_native"]
    console = authority.paths.root / "console.log"
    before, before_digest, before_stamp = _file(console, private=True, limit=2 * _MAX_JSON)
    previous = data.get("vz_logs")
    if (
        not isinstance(previous, dict)
        or before_stamp[:2] != native.get("console_identity")
        or previous.get("console") != {"stamp": before_stamp, "sha256": before_digest, "bytes": len(before)}
    ):
        raise VZNativeHold("native VZ retained console identity or committed prefix changed")
    previous_offsets = previous.get("offsets")
    if (
        not isinstance(previous_offsets, dict)
        or set(previous_offsets) != {"stdout", "stderr"}
        or any(type(value) is not int or not 0 <= value <= _MAX_JSON for value in previous_offsets.values())
    ):
        raise StateError("native VZ retained log cursors are invalid")
    intent_path = authority.paths.root / "vz-private" / "log-append-intent.json"
    if os.path.lexists(intent_path):
        raw, _, _ = _file(intent_path, private=True, limit=_MAX_JSON)
        pending = _decode(raw)
        if (
            not isinstance(pending, dict)
            or pending.get("schema") != "palimpsest.vz-log-append.v1"
            or pending.get("run_id") != authority.record.run_id
            or pending.get("plan_sha256") != native["plan_sha256"]
            or pending.get("offsets_after") != previous_offsets
            or pending.get("console_after")
            != {
                "identity": native["console_identity"],
                "sha256": before_digest,
                "bytes": len(before),
            }
        ):
            raise VZNativeHold("native VZ log append has no committed cursor; retained bytes are not replayed")
        _clear_log_append_intent(authority, intent_path, raw)
    streams = {}
    facts = {}
    deadline = time.monotonic() + 300
    for stream in ("stdout", "stderr"):
        output = bytearray()
        offset = 0
        size = None
        truncated = False
        while True:
            if time.monotonic() >= deadline:
                raise VZNativeHold("native VZ bounded authenticated log capture timed out")
            value = _control(
                config, native, "logs", custody, allow_expired=True, fields={"stream": stream, "offset": offset}
            )
            if (
                not isinstance(value, dict)
                or value.get("schema") != "palimpsest.vz-root-logs.v1"
                or value.get("run_id") != native["plan"]["run_id"]
                or value.get("plan_sha256") != native["plan_sha256"]
                or value.get("stream") != stream
                or value.get("offset") != offset
                or type(value.get("size")) is not int
                or not offset <= value["size"] <= _MAX_JSON
                or type(value.get("next_offset")) is not int
                or type(value.get("truncated")) is not bool
            ):
                raise VZNativeHold("native VZ authenticated log identity/bounds differ")
            if size is None:
                size = value["size"]
            elif value["size"] < size or final and value["size"] != size:
                raise VZNativeHold("native VZ authenticated log snapshot changed after quiescence")
            try:
                payload = base64.b64decode(value["data_base64"], validate=True)
            except (KeyError, ValueError, TypeError) as error:
                raise VZNativeHold("native VZ authenticated log payload is invalid") from error
            if (
                len(payload) > 16384
                or value["next_offset"] != offset + len(payload)
                or value["next_offset"] > value["size"]
            ):
                raise VZNativeHold("native VZ authenticated log chunk range differs")
            keep = min(len(payload), size - offset)
            output.extend(payload[:keep])
            offset += keep
            truncated = truncated or value["truncated"]
            if offset == size:
                break
            if not payload:
                raise VZNativeHold("native VZ authenticated log made no progress")
        captured = bytes(output)
        prior_offset = previous_offsets[stream]
        prior_fact = previous.get("streams", {}).get(stream)
        if prior_offset > len(captured):
            raise VZNativeHold("native VZ guest log prefix shrank; retained output is not replaced")
        if prior_offset and (
            not isinstance(prior_fact, dict) or _digest(captured[:prior_offset]) != prior_fact.get("sha256")
        ):
            raise VZNativeHold("native VZ authenticated historical log prefix changed")
        streams[stream] = captured
        facts[stream] = {"bytes": len(captured), "sha256": _digest(captured), "truncated": truncated}
        authority.write_file("vz-private/main." + stream, captured)
    delta = streams["stdout"][previous_offsets["stdout"] :] + streams["stderr"][previous_offsets["stderr"] :]
    offsets = {stream: len(payload) for stream, payload in streams.items()}
    if len(before) + len(delta) > 2 * _MAX_JSON:
        raise VZNativeHold("native VZ retained console exceeds its aggregate output bound")
    full_digest = hashlib.sha256(before)
    full_digest.update(delta)
    after_digest = "sha256:" + full_digest.hexdigest()
    intent_raw = None
    if delta:
        intent_raw = _json(
            {
                "schema": "palimpsest.vz-log-append.v1",
                "run_id": authority.record.run_id,
                "plan_sha256": native["plan_sha256"],
                "console_before_stamp": before_stamp,
                "delta_size": len(delta),
                "delta_digest": _digest(delta),
                "offsets_after": offsets,
                "console_after": {
                    "identity": native["console_identity"],
                    "sha256": after_digest,
                    "bytes": len(before) + len(delta),
                },
            }
        )
        _new_file(intent_path, intent_raw)
        # Canonical append_file fsyncs the same pinned inode; never replace it.
        authority.append_file("console.log", delta)
    _, actual_digest, after_stamp = _file(console, private=True, limit=2 * _MAX_JSON)
    if (
        after_stamp[:2] != native["console_identity"]
        or actual_digest != after_digest
        or after_stamp[5] != len(before) + len(delta)
    ):
        raise VZNativeHold("native VZ console changed during authenticated append")
    data["vz_logs"] = {
        "source": "authenticated-guest-platform-state",
        "capture_state": "observed-stop-ready-final" if final else "observed-live-prefix",
        "final": final,
        "streams": facts,
        "offsets": offsets,
        "captured_unix": time.time(),
        "console": {"stamp": after_stamp, "sha256": actual_digest, "bytes": after_stamp[5]},
        "ordering": "per-refresh-stdout-delta-then-stderr-delta; prior-bytes-never-reordered",
        "unattended_final_tail": "unverified",
    }
    # Commit cursors before removing the intent; a crash cannot silently duplicate
    # a previously appended prefix on the next refresh.
    _write(authority, authority.snapshot.state["status"], data)
    if intent_raw is not None:
        _clear_log_append_intent(authority, intent_path, intent_raw)


def refresh_logs(roots, name, *, expected_record=None, _observation_budget=None):
    """Refresh live logs, or read terminal retained bytes without provider access."""
    if _observation_budget is None:
        _observation_budget = RunObservationWaitBudget()
    with locked_existing_run(
        roots, name, expected=expected_record, observation_budget=_observation_budget
    ) as authority:
        if authority.snapshot.state["status"] in {"stopped", "removed"}:
            return authority.mutable_state()
    return refresh_run(roots, name, expected_record=expected_record, _observation_budget=_observation_budget)


def refresh_run(roots, name, *, expected_record=None, _observation_budget=None):
    if _observation_budget is None:
        _observation_budget = RunObservationWaitBudget()
    with locked_existing_run(
        roots, name, expected=expected_record, observation_budget=_observation_budget
    ) as authority:
        if authority.snapshot.state["status"] == "removed":
            return authority.mutable_state()
        config = _config_for(authority)
        with _custody(
            config,
            lock_timeout=RUN_OBSERVATION_TIMEOUT_SECONDS,
            observation_budget=_observation_budget,
        ) as custody:
            data, *_ = _load(authority, roots, config, custody, allow_expired=True)
            native = data["vz_native"]
            instance = _owned_instance(config, native, custody, allow_expired=True)
            if instance.get("status") == "Stopped":
                _retained_stopped_observation(authority, roots, config, custody, data)
                return _write(authority, "stopped", data)
            if instance.get("status") != "Running":
                raise VZNativeHold("native VZ refresh observed unknown provider state")
            status = _validate_status(
                native, _control(config, native, "status", custody, allow_expired=True), handed_off=True
            )
            data["vz_status"] = status
            _capture_logs(authority, config, data, custody, final=False)
            return _write(authority, _status_name(status, native), data)


class NativeProcessSession:
    """Byte-transparent SSH owner. Close sends TERM only and preserves uncertainty.

    A background drainer is unnecessary: the single consumer owns output. A close
    that cannot reap is a HOLD, not a claim that guest descendants were stopped.
    Callers retain a non-daemon lifetime owner even when cleanup crosses threads.
    """

    def __init__(self, config, native, argv, *, tty, hold_callback, retain_callback):
        _require_native_custody_owner()
        self._config, self._native = config, native
        self._tty, self._hold_callback, self._retain_callback = tty, hold_callback, retain_callback
        self._master = None
        self._closed = False
        self._consumer = False
        self._lock = threading.Lock()
        self._cancelled = False
        self._result = None
        self._selector = selectors.DefaultSelector()
        slave = None
        try:
            if tty:
                master, slave = os.openpty()
                self._master = master
                self._process = subprocess.Popen(
                    argv,
                    env=config.environment(),
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                    close_fds=True,
                )
                os.close(slave)
                slave = None
                os.set_blocking(master, False)
                self._selector.register(master, selectors.EVENT_READ, ProcessStream.PTY)
            else:
                self._process = subprocess.Popen(
                    argv,
                    env=config.environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                for stream, label in (
                    (self._process.stdout, ProcessStream.STDOUT),
                    (self._process.stderr, ProcessStream.STDERR),
                ):
                    os.set_blocking(stream.fileno(), False)
                    self._selector.register(stream, selectors.EVENT_READ, label)
        except BaseException:
            if slave is not None:
                os.close(slave)
            process = getattr(self, "_process", None)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        _HELD.append(process)
                        retain_preparation_custody(self, process)
                retain_callback(process.pid)
            elif self._master is not None:
                os.close(self._master)
                self._master = None
            raise

    @property
    def capabilities(self):
        return ProcessCapabilities(self._tty, self._tty, self._tty, False)

    def _require_group_settled(self):
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            pass
        _HELD.append(self._process)
        retain_preparation_custody(self, self._process)
        self._retain_callback(self._process.pid)
        park_preparation_custody()
        raise VZNativeHold("native VZ SSH group custody remains unresolved")

    def _exit(self):
        with self._lock, _actor_termination_scope(), defer_web_cancellation(), defer_preparation_termination():
            if self._result is None:
                code = self._process.poll()
                if code is None:
                    raise LifecycleError("native VZ SSH actor is still active")
                self._require_group_settled()
                category = (
                    ProcessExitCategory.CANCELLED
                    if self._cancelled
                    else (ProcessExitCategory.SIGNALED if code < 0 else ProcessExitCategory.EXITED)
                )
                self._hold_callback(self._process.pid, ambiguous=code < 0 or code == 255 or self._cancelled)
                self._result = ProcessExit(code, code if code >= 0 else None, -code if code < 0 else None, category)
            return self._result

    def events(self):
        with self._lock:
            if self._consumer:
                raise LifecycleError("native VZ process events already have a consumer")
            self._consumer = True
        while self._selector.get_map() or self._process.poll() is None:
            for key, _ in self._selector.select(0.1):
                try:
                    payload = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError as error:
                    if not self._tty or error.errno != errno.EIO:
                        raise
                    payload = b""
                if payload:
                    yield ProcessOutputEvent(key.data, payload)
                else:
                    self._selector.unregister(key.fileobj)
            if self._closed and self._process.poll() is None:
                raise VZNativeHold("native VZ SSH actor retained after close")
        yield ProcessStatusEvent(self._exit())

    def write_stdin(self, data):
        if not self._tty:
            raise ProcessCapabilityError("stdin")
        if not isinstance(data, bytes):
            raise TypeError("native VZ stdin requires bytes")
        fd = self._master
        view = memoryview(data)
        while view:
            try:
                count = os.write(fd, view[:65536])
                view = view[count:]
            except BlockingIOError:
                import select

                select.select([], [fd], [], 0.1)

    def close_stdin(self):
        if not self._tty:
            raise ProcessCapabilityError("stdin")
        self.write_stdin(b"\x04")

    def resize(self, rows, columns):
        if not self._tty:
            raise ProcessCapabilityError("resize")
        if type(rows) is not int or type(columns) is not int or not 1 <= rows <= 65535 or not 1 <= columns <= 65535:
            raise ValueError("terminal dimensions are invalid")
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGWINCH)

    def signal(self, requested):
        raise ProcessCapabilityError("signal")

    def wait(self):
        if not self._consumer:
            for _event in self.events():
                pass
        self._process.wait()
        return self._exit()

    def close(self):
        with _actor_termination_scope(), defer_web_cancellation(), defer_preparation_termination():
            self._close_owned()

    def _close_owned(self):
        if self._closed:
            return
        self._closed = True
        if self._process.poll() is None:
            self._cancelled = True
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _HELD.append(self._process)
                retain_preparation_custody(self, self._process)
                self._retain_callback(self._process.pid)
                park_preparation_custody()
                raise VZNativeHold("native VZ SSH actor did not exit after TERM; custody retained") from None
        self._exit()
        self._selector.close()
        if self._master is not None:
            os.close(self._master)
            self._master = None
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _session(
    name,
    *,
    roots,
    expected,
    request=None,
    tty=False,
    follow=False,
    _observation_budget=None,
):
    _require_native_custody_owner()
    if _observation_budget is None:
        _observation_budget = RunObservationWaitBudget()
    # Match every lifecycle/refresh owner: run-name lock before namespace custody.
    # Waiting admits one command; it never resubmits an SSH or guest operation.
    with locked_existing_run(roots, name, expected=expected, observation_budget=_observation_budget) as authority:
        config = _config_for(authority)
        with _custody(
            config,
            lock_timeout=RUN_OBSERVATION_TIMEOUT_SECONDS,
            observation_budget=_observation_budget,
        ) as custody:
            data, *_ = _load(authority, roots, config, custody)
            native = data["vz_native"]
            # Host preparation and this fixed-duration status actor do not spend
            # the guest execution timer or the shared mutex-contention allowance.
            _validate_status(native, _control(config, native, "status", custody), handed_off=True)
            command = "plm-follow-v1" if follow else None
            if request is not None:
                payload = _json({"argv": list(request.argv), "timeout_ms": request.timeout_ms})
                if len(payload) > 256 * 1024:
                    raise StateError("native VZ structured exec exceeds canonical process bound")
                command = "plm-exec-v1 " + base64.urlsafe_b64encode(payload).decode().rstrip("=")
            marker = custody / ("session-" + uuid.uuid4().hex + ".json")
            _new_file(
                marker,
                _json(
                    {
                        "schema": _SCHEMA,
                        "run_id": authority.record.run_id,
                        "run_name": name,
                        "owner_pid": os.getpid(),
                        "operation": "user-ssh",
                        "created_unix": time.time(),
                    }
                ),
            )
            marker_fd = os.open(marker, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker_info = {"digest": _file(marker, private=True, limit=65536)[1], "released": False, "ready": False}

            def release_actor():
                if marker_info["released"]:
                    return
                if _file(marker, private=True, limit=65536)[1] != marker_info["digest"]:
                    raise VZNativeHold("native VZ SSH custody marker changed")
                marker.unlink()
                os.close(marker_fd)
                marker_info["released"] = True
                fd = _directory(custody)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

            hold_marker = custody / ("hold-" + marker.stem + ".json")

            def retain_actor(pid):
                if not os.path.lexists(hold_marker):
                    _new_file(
                        hold_marker,
                        _json(
                            {
                                "schema": _SCHEMA,
                                "operation": "user-ssh-retained",
                                "pid": pid,
                                "run_id": authority.record.run_id,
                                "run_name": name,
                            }
                        ),
                    )

            def hold_actor(pid, *, ambiguous=True):
                if marker_info["ready"]:
                    try:
                        with _custody(config, lock_timeout=360) as status_custody:
                            instance = _owned_instance(config, native, status_custody, allow_expired=True)
                            if instance.get("status") == "Stopped":
                                release_actor()
                                return
                            status = _validate_status(
                                native,
                                _control(config, native, "status", status_custody, allow_expired=True),
                                handed_off=True,
                                allow_stopping=True,
                            )
                            if status["user_sessions_known"] is True and (
                                not ambiguous or status["user_session_count"] == 0
                            ):
                                release_actor()
                                return
                    except (OSError, LifecycleError, StateError):
                        pass
                retain_actor(pid)

            authority.verify_binding()
            session = None
            try:
                with _actor_termination_scope(), defer_web_cancellation(), defer_preparation_termination():
                    session = NativeProcessSession(
                        config,
                        native,
                        _ssh_argv(config, native, tty=tty, command=command),
                        tty=tty,
                        hold_callback=hold_actor,
                        retain_callback=retain_actor,
                    )
                    os.lseek(marker_fd, 0, os.SEEK_END)
                    payload = memoryview(b"\n" + _json({"schema": _SCHEMA, "actor_pid": session._process.pid}))
                    while payload:
                        payload = payload[os.write(marker_fd, payload) :]
                    os.fsync(marker_fd)
                    marker_info["digest"] = _file(marker, private=True, limit=65536)[1]
                    marker_info["ready"] = True
                return session
            except BaseException:
                with _actor_termination_scope(), defer_web_cancellation(), defer_preparation_termination():
                    if session is not None:
                        if session._process.poll() is None:
                            session._process.terminate()
                            try:
                                session._process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                _HELD.append(session._process)
                                retain_preparation_custody(session, session._process, custody, hold_marker)
                        retain_actor(session._process.pid)
                    if not os.path.lexists(hold_marker):
                        release_actor()
                    elif not marker_info["released"]:
                        os.close(marker_fd)
                raise


def exec_session(
    name,
    request: ExecRequest,
    *,
    roots,
    _expected_record,
    _record_writer=None,
    _observation_budget=None,
):
    if type(request) is not ExecRequest:
        raise StateError("native VZ exec requires canonical ExecRequest")
    if _record_writer is not None:
        raise StateError("native VZ structured exec recording is not supported by this profile")
    timeout_ms = effective_exec_timeout_ms(request)
    argv = validate_exec_request(request.argv, timeout_ms)
    resolved = ExecRequest(tuple(argv), timeout_ms)
    return _session(
        name,
        roots=roots,
        expected=_expected_record,
        request=resolved,
        _observation_budget=_observation_budget,
    )


def shell_session(name, *, roots, _expected_record, _observation_budget=None):
    return _session(name, roots=roots, expected=_expected_record, tty=True, _observation_budget=_observation_budget)


# Platform-bound identity files the guest injects on every boot (guest/vz/platform.c): they are
# never inherited by a commit, whatever the upper holds at those paths.
_COMMIT_GENERATED_PLACEHOLDERS = ("etc/machine-id", "var/lib/dbus/machine-id", "etc/hostname", "etc/resolv.conf")


def _commit_tool(variable, *, required):
    """Resolve an e2fsprogs binary from an absolute, non-writable path named by the environment."""
    from .oci_commit import CommitError

    selected = os.environ.get(variable)
    if not selected:
        if required:
            raise CommitError(
                "commit-tool", f"{variable} must name the absolute path of the e2fsprogs binary used to read the upper"
            )
        return None
    path = Path(selected)
    try:
        info = os.stat(path)
    except OSError:
        raise CommitError("commit-tool", f"{variable} does not name an existing file") from None
    if (
        not path.is_absolute()
        or not stat.S_ISREG(info.st_mode)
        or not os.access(path, os.X_OK)
        or info.st_uid not in (0, os.geteuid())
        or info.st_mode & 0o022
    ):
        raise CommitError("commit-tool", f"{variable} must be an absolute executable not writable by group or others")
    return path


def commit(name, tag, *, roots, _expected_record):
    """Capture a stopped native VZ run's upper as a new OCI layout on top of its source image.

    Read-only toward the run: the run's state, disk and provider are never changed. Only a
    run that is exactly stopped, unheld and fully re-verified (the same checks removal uses)
    is accepted.
    """
    from . import vz_commit
    from .oci_commit import CommitError, admit_capture

    validate_tag(tag)
    debugfs = _commit_tool("PALIMPSEST_COMMIT_DEBUGFS", required=True)
    e2fsck = _commit_tool("PALIMPSEST_COMMIT_E2FSCK", required=True)
    with locked_existing_run(roots, name, expected=_expected_record) as authority:
        config = _config_for(authority)
        data = _data(authority)
        native = data.get("vz_native", {})
        admit_capture(instance_state=authority.snapshot.state["status"], source_hold=bool(native.get("hold")))
        commits = roots.state / "oci-commits"
        os.makedirs(commits, mode=0o700, exist_ok=True)
        destination = commits / tag
        if os.path.lexists(destination):
            raise CommitError("commit-tag-exists", "a commit with this tag already exists; nothing was changed")
        scratch = commits / (".scratch-" + os.urandom(8).hex())
        os.mkdir(scratch, 0o700)
        try:
            with _custody(config) as custody:
                data, _store, owner, _lease, _receipts, _export = _load_checked(
                    authority, roots, config, custody, allow_expired=True, data=data
                )
                _retained_stopped_observation(authority, roots, config, custody, data)
                snapshot = _reopen_source(roots, native["source_snapshot"], data["source_qualification"])
                disk = load_vz_root_volume(roots, native["root_volume_id"], owner=owner)
                cas = SourceCAS.open_existing(roots.oci_source_cas, expected_cas_id=snapshot.cas_id)

                def stream(ordinal):
                    @contextmanager
                    def opener():
                        with cas.lease_layer(snapshot, ordinal) as lease:
                            yield lease.chunks()

                    return opener

                streams = {}
                for ordinal, layer in enumerate(snapshot.layers):
                    streams.setdefault(layer.descriptor.digest, stream(ordinal))
                result = vz_commit.commit_stopped_disk(
                    disk=disk.record.storage.directory / "datadisk",
                    disk_size=disk.record.size_bytes,
                    instance_state=authority.snapshot.state["status"],
                    source_hold=bool(native.get("hold")),
                    expected_binding={
                        "run_id": authority.record.run_id,
                        "root_volume_id": native["root_volume_id"],
                        "root_graph_digest": native["plan"]["root_graph_digest"],
                        "root_nonce": native["root_nonce"],
                    },
                    scratch_parent=scratch,
                    debugfs=debugfs,
                    destination=destination,
                    parent_manifest_bytes=cas.read_metadata(snapshot.manifest),
                    parent_config_bytes=cas.read_metadata(snapshot.config),
                    parent_manifest_digest=snapshot.manifest.descriptor.digest,
                    base_image_digest=snapshot.root.descriptor.digest,
                    layer_streams=streams,
                    created=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    generated_placeholders=_COMMIT_GENERATED_PLACEHOLDERS,
                    e2fsck=e2fsck,
                )
        finally:
            shutil.rmtree(scratch, ignore_errors=False)
    return {"tag": tag, **result, "source": "commit"}
