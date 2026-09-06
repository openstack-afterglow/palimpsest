"""Explicit inherited filesystem authority for the private exec monitor.

The bootstrap frame contains data, never executable callbacks or ambient paths.
Descriptors remain held until synchronous launch and failure cleanup finish.
"""

from __future__ import annotations

import copy
import fcntl
import math
import os
import re
import stat
from pathlib import Path
from typing import Any

from .errors import PalimpsestError, StateError
from .oci_monitor_control import MonitorStopControl
from .oci_monitor_ipc import MonitorPreActivationBinding
from .oci_root_kvm import VerifiedHostBootArtifacts, verify_host_boot_artifacts
from .oci_runtime_io import runtime_io_guard, runtime_io_paths
from .oci_store import OCIStore
from .platforms import DomainProfile
from .state import StatePaths, locked_existing_run

_SCHEMA = "palimpsest.monitor-launch-authority.v6"
_PROFILE_FIELDS = {
    "backend",
    "domain_type",
    "arch",
    "machine",
    "emulator",
    "uri",
    "firmware",
    "autoselect_firmware",
    "network_mode",
    "seed_tool",
    "seed_bus",
}
_ENTRY_FIELDS = {"fd", "path", "device", "inode", "uid", "gid", "nlink", "mode", "size", "mtime_ns", "ctime_ns"}


def _invalid() -> StateError:
    return StateError("OCI monitor launch authority is invalid or changed")


def _path(value: object) -> Path:
    if type(value) is not str or not value or "\0" in value or len(value) > 4096:
        raise _invalid()
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts or value == "/":
        raise _invalid()
    return path


def _open(path: Path, *, directory: bool) -> int:
    """Reject symlinks in every path component, including ancestors."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, component in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or index < len(path.parts) - 2:
                flags |= os.O_DIRECTORY
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _entry(path: Path, fd: int) -> dict[str, Any]:
    info = os.fstat(fd)
    return {
        "fd": fd,
        "path": str(path),
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "nlink": info.st_nlink,
        "mode": info.st_mode,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _validate_entry_metadata(
    key: str,
    entry: dict[str, Any],
    opened: os.stat_result,
    visible: os.stat_result,
    owner_uid: int,
) -> None:
    """Check immutable metadata independently of path traversal and FD rights."""
    is_directory = key not in {"kernel", "initramfs", "runtime_console"}
    fields = ("device", "inode", "uid", "gid", "mode") if is_directory else tuple(_ENTRY_FIELDS - {"fd", "path"})
    if key == "runtime_console":
        # QEMU output is not trusted state: content, size and timestamps may change.
        fields = ("device", "inode", "uid", "gid", "mode", "nlink")
    for info in (opened, visible):
        values = {
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "nlink": info.st_nlink,
            "mode": info.st_mode,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
        if any(values[field] != entry[field] for field in fields):
            raise _invalid()
        if is_directory:
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
                raise _invalid()
            if key not in {"config", "state", "runs", "run", "runtime_packs"} and stat.S_IMODE(info.st_mode) != 0o700:
                raise _invalid()
        elif key == "runtime_console":
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise _invalid()
        elif (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, owner_uid}
            or info.st_mode & 0o022
            or info.st_nlink != 1
        ):
            raise _invalid()


def _paths(roots: StatePaths, name: str) -> dict[str, Path]:
    runtime_paths = runtime_io_paths(roots.runs / name)
    result = {
        "config": roots.config,
        "state": roots.state,
        "runs": roots.runs,
        "run": roots.runs / name,
        "monitor": roots.runs / name / "monitor-private",
        "runtime_io": runtime_paths.root,
        "runtime_console": runtime_paths.console_log,
        "store": roots.store,
        "runtime_packs": roots.runtime_packs,
        "derived": roots.oci_derived_store,
        "blobs_parent": roots.store / "blobs",
        "blobs": roots.store / "blobs" / "sha256",
        "artifact_locks": roots.store / "oci-artifact-locks-v1",
    }
    for name in ("records", "keys", "occurrences", "leases", "lease-sets", "locks"):
        result["derived_" + name] = roots.oci_derived_store / name
    return result


def _profile(value: object) -> DomainProfile:
    if type(value) is not dict or set(value) != _PROFILE_FIELDS:
        raise _invalid()
    # The current OCI-root contract supports this complete BIOS KVM profile only.
    expected = {
        "backend": "kvm",
        "domain_type": "kvm",
        "arch": "x86_64",
        "machine": "q35",
        "uri": "qemu:///system",
        "firmware": None,
        "autoselect_firmware": False,
        "network_mode": "libvirt-network",
        "seed_tool": "cloud-localds",
        "seed_bus": "sata",
    }
    if any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items()):
        raise _invalid()
    return DomainProfile(**{**value, "emulator": _path(value["emulator"])})


def _timeout(value: object) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= 3600:
        raise _invalid()
    return float(value)


class MonitorLaunchAuthority:
    """Own only the explicit descriptors in one validated private bootstrap."""

    def __init__(self, frame: dict[str, Any]) -> None:
        self._frame = copy.deepcopy(frame)
        self._closed = False

    @classmethod
    def from_dict(cls, value: object, *, excluded_fds: tuple[int, ...] = ()) -> MonitorLaunchAuthority:
        if (
            type(value) is not dict
            or set(value)
            != {
                "schema",
                "binding",
                "entries",
                "boot",
                "store_identity",
                "profile",
                "timeout_seconds",
                "terminal_timeout_seconds",
                "runtime_access",
                "shared_traversal",
                "root_access",
            }
            or value["schema"] != _SCHEMA
        ):
            raise _invalid()
        try:
            binding = MonitorPreActivationBinding.from_dict(value["binding"])
            entries = value["entries"]
            if type(entries) is not dict or not {"config", "state"} <= set(entries):
                raise _invalid()
            if any(type(entry) is not dict or set(entry) != _ENTRY_FIELDS for entry in entries.values()):
                raise _invalid()
            roots = StatePaths(_path(entries["config"]["path"]), _path(entries["state"]["path"]))
            paths = _paths(roots, binding.record.name)
            if value["root_access"] is not None:
                from .oci_root_access import RootAccessReceipt
                from .oci_root_volume import _paths as volume_paths

                member = RootAccessReceipt.from_dict(value["root_access"])
                paths["root_disk"] = volume_paths(roots, member.volume.volume_id)[0]
            if set(entries) != {*paths, "kernel", "initramfs"}:
                raise _invalid()
            descriptors: set[int] = set()
            for key, entry in entries.items():
                fd = entry["fd"]
                if type(fd) is not int or fd < 3 or fd in descriptors or fd in excluded_fds:
                    raise _invalid()
                descriptors.add(fd)
                path = _path(entry["path"])
                if key in paths and paths[key] != path:
                    raise _invalid()
                if any(type(entry[field]) is not int or entry[field] < 0 for field in _ENTRY_FIELDS - {"fd", "path"}):
                    raise _invalid()
            boot = value["boot"]
            if type(boot) is not dict or set(boot) != {"architecture", "policy", "kernel", "initramfs"}:
                raise _invalid()
            for key in ("kernel", "initramfs"):
                artifact = boot[key]
                if type(artifact) is not dict or set(artifact) != {"digest", "size_bytes"}:
                    raise _invalid()
                if (
                    type(artifact["digest"]) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]) is None
                    or type(artifact["size_bytes"]) is not int
                    or artifact["size_bytes"] != entries[key]["size"]
                ):
                    raise _invalid()
            if (
                type(value["store_identity"]) is not str
                or re.fullmatch(r"oci-store-v1:[0-9a-f]{64}", value["store_identity"]) is None
            ):
                raise _invalid()
            _profile(value["profile"])
            _timeout(value["timeout_seconds"])
            _timeout(value["terminal_timeout_seconds"])
            authority = cls(value)
            authority.validate()
            authority._rebuild()
            return authority
        except (OSError, KeyError, TypeError, ValueError, PalimpsestError):
            # Failed parsing must not close untrusted descriptor numbers. The exec
            # child exits; the preparing parent closes only descriptors it opened.
            raise _invalid() from None

    @property
    def pass_fds(self) -> tuple[int, ...]:
        if self._closed:
            raise _invalid()
        return tuple(entry["fd"] for entry in self._frame["entries"].values())

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return copy.deepcopy(self._frame)

    def validate(self, directory_fd: int | None = None, binding: MonitorPreActivationBinding | None = None) -> None:
        if self._closed:
            raise _invalid()
        try:
            selected = MonitorPreActivationBinding.from_dict(self._frame["binding"])
            if binding is not None:
                MonitorPreActivationBinding.__post_init__(binding)
            if binding is not None and (
                type(binding) is not MonitorPreActivationBinding or binding.to_dict() != selected.to_dict()
            ):
                raise _invalid()
            access = self._frame["runtime_access"]
            root_access = self._frame["root_access"]
            from .oci_root_access import verify_root_launch_member

            verify_root_launch_member(
                StatePaths(
                    _path(self._frame["entries"]["config"]["path"]), _path(self._frame["entries"]["state"]["path"])
                ),
                root_access,
                self._frame["entries"]["run"]["fd"],
                binding=selected,
            )
            if root_access is not None:
                from .oci_root_access import verify_root_launch_access

                verify_root_launch_access(
                    StatePaths(
                        _path(self._frame["entries"]["config"]["path"]), _path(self._frame["entries"]["state"]["path"])
                    ),
                    root_access,
                    self._frame["entries"]["root_disk"]["fd"],
                    binding=selected,
                )
            from .oci_shared_traversal import verify_shared_traversal

            verify_shared_traversal(
                StatePaths(
                    _path(self._frame["entries"]["config"]["path"]), _path(self._frame["entries"]["state"]["path"])
                ),
                self._frame["shared_traversal"],
                binding=selected,
                access=access,
                state_fd=self._frame["entries"]["state"]["fd"],
                runs_fd=self._frame["entries"]["runs"]["fd"],
            )
            if access is not None:
                from .oci_runtime_access import RuntimeAccessReceipt, verify_runtime_access

                access = RuntimeAccessReceipt.from_dict(access)
                if access.binding != selected:
                    raise _invalid()
                io_entry = self._frame["entries"]["runtime_io"]
                console_entry = self._frame["entries"]["runtime_console"]
                verify_runtime_access(
                    access,
                    access.runtime_io,
                    io_entry["fd"],
                    console_entry["fd"],
                    os.stat(io_entry["path"], follow_symlinks=False),
                    os.stat(console_entry["path"], follow_symlinks=False),
                    run_directory_fd=self._frame["entries"]["run"]["fd"],
                    runs_directory_fd=self._frame["entries"]["runs"]["fd"],
                )
            for key, entry in self._frame["entries"].items():
                is_directory = key not in {"kernel", "initramfs", "runtime_console", "root_disk"}
                opened = os.fstat(entry["fd"])
                flags = fcntl.fcntl(entry["fd"], fcntl.F_GETFL)
                if flags & os.O_ACCMODE != os.O_RDONLY or flags & getattr(os, "O_PATH", 0):
                    raise _invalid()
                visible_fd = _open(_path(entry["path"]), directory=is_directory)
                try:
                    visible = os.fstat(visible_fd)
                finally:
                    os.close(visible_fd)
                if (access is not None and key in {"runtime_io", "runtime_console"}) or (
                    root_access is not None and key == "root_disk"
                ):
                    # Full ACL+target policy above is mandatory, not a generic writable-mode exception.
                    for info in (opened, visible):
                        if (info.st_dev, info.st_ino) != (entry["device"], entry["inode"]):
                            raise _invalid()
                        if (info.st_uid, info.st_gid, info.st_mode) != (entry["uid"], entry["gid"], entry["mode"]):
                            raise _invalid()
                else:
                    _validate_entry_metadata(key, entry, opened, visible, selected.owner_uid)
            if directory_fd is not None:
                if type(directory_fd) is not int or directory_fd < 3:
                    raise _invalid()
                actual = os.fstat(directory_fd)
                expected = self._frame["entries"]["monitor"]
                if (actual.st_dev, actual.st_ino) != (expected["device"], expected["inode"]):
                    raise _invalid()
            from .oci_root_access import verify_root_launch_tail

            verify_root_launch_tail(
                StatePaths(
                    _path(self._frame["entries"]["config"]["path"]), _path(self._frame["entries"]["state"]["path"])
                ),
                root_access,
                self._frame["entries"]["run"]["fd"],
                self._frame["entries"].get("root_disk", {}).get("fd"),
                binding=selected,
            )
        except (OSError, KeyError, TypeError, ValueError, PalimpsestError):
            raise _invalid() from None

    def _rebuild(self) -> tuple[StatePaths, OCIStore, VerifiedHostBootArtifacts, DomainProfile]:
        self.validate()
        entries = self._frame["entries"]
        roots = StatePaths(Path(entries["config"]["path"]), Path(entries["state"]["path"]))
        boot = verify_host_boot_artifacts(
            Path(entries["kernel"]["path"]),
            Path(entries["initramfs"]["path"]),
            expected_kernel_digest=self._frame["boot"]["kernel"]["digest"],
            expected_initramfs_digest=self._frame["boot"]["initramfs"]["digest"],
        )
        if boot.to_dict() != self._frame["boot"]:
            raise _invalid()
        store = OCIStore(roots)
        if store.identity != self._frame["store_identity"]:
            raise _invalid()
        self.validate()
        return roots, store, boot, _profile(self._frame["profile"])

    def run(
        self,
        directory_fd: int,
        binding: MonitorPreActivationBinding,
        lease: Any,
        *,
        stop_control: MonitorStopControl | None = None,
    ) -> Any:
        # This import is intentionally worker-local: no libvirt/event state is
        # inherited from the spawning process or initialized by the IPC loop.
        from .oci_root_runtime import connect_oci_root_libvirt, launch_defined_oci_root_domain

        connection = None
        try:
            if stop_control is not None and type(stop_control) is not MonitorStopControl:
                raise _invalid()
            self.validate(directory_fd, binding)
            roots, store, boot, profile = self._rebuild()
            connection = connect_oci_root_libvirt(binding.libvirt_uri)
            self.validate(directory_fd, binding)
            return launch_defined_oci_root_domain(
                roots,
                binding.record.name,
                store,
                boot,
                profile,
                conn=connection,
                monitor_binding=binding,
                monitor_lease=lease,
                timeout_seconds=self._frame["timeout_seconds"],
                terminal_timeout_seconds=self._frame["terminal_timeout_seconds"],
                authority_guard=lambda: self.validate(directory_fd, binding),
                **({"stop_control": stop_control} if stop_control is not None else {}),
            )
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self.close()

    def close(self) -> None:
        if self._closed:
            return
        descriptors = self.pass_fds
        self._closed = True
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> MonitorLaunchAuthority:
        self.validate()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def prepare_monitor_launch_authority(
    roots: StatePaths,
    store: OCIStore,
    boot_artifacts: VerifiedHostBootArtifacts,
    profile: DomainProfile,
    binding: MonitorPreActivationBinding,
    *,
    timeout_seconds: float = 45,
    terminal_timeout_seconds: float = 45,
) -> MonitorLaunchAuthority:
    """Pin a caller-selected filesystem set for an optional fresh-exec launch."""
    opened: list[int] = []
    try:
        if (
            type(roots) is not StatePaths
            or type(store) is not OCIStore
            or type(boot_artifacts) is not VerifiedHostBootArtifacts
            or type(profile) is not DomainProfile
            or type(binding) is not MonitorPreActivationBinding
        ):
            raise _invalid()
        paths = _paths(roots, binding.record.name)
        paths.update(kernel=boot_artifacts.kernel.path, initramfs=boot_artifacts.initramfs.path)
        entries = {}
        with locked_existing_run(roots, binding.record.name) as mutation:
            with runtime_io_guard(mutation, plan_digest=binding.plan_digest, require_socket_absent=True) as runtime_io:
                runtime_access = mutation.mutable_state().get("oci_runtime_access")
                shared_traversal = mutation.mutable_state().get("oci_shared_traversal")
                root_access = mutation.mutable_state().get("oci_root_access")
                if root_access is not None:
                    from .oci_root_access import RootAccessReceipt
                    from .oci_root_volume import _paths as volume_paths

                    member = RootAccessReceipt.from_dict(root_access)
                    paths["root_disk"] = volume_paths(roots, member.volume.volume_id)[0]
                for name, path in paths.items():
                    path = _path(str(path))
                    fd = _open(path, directory=name not in {"kernel", "initramfs", "runtime_console", "root_disk"})
                    opened.append(fd)
                    entries[name] = _entry(path, fd)
                runtime_io.verify(require_socket_absent=True)
        frame = {
            "schema": _SCHEMA,
            "binding": binding.to_dict(),
            "entries": entries,
            "boot": boot_artifacts.to_dict(),
            "store_identity": store.identity,
            "profile": {
                key: str(getattr(profile, key)) if key == "emulator" else getattr(profile, key)
                for key in _PROFILE_FIELDS
            },
            "timeout_seconds": _timeout(timeout_seconds),
            "terminal_timeout_seconds": _timeout(terminal_timeout_seconds),
            "runtime_access": runtime_access,
            "shared_traversal": shared_traversal,
            "root_access": root_access,
        }
        result = MonitorLaunchAuthority.from_dict(frame)
        for name in ("kernel", "initramfs"):
            artifact = getattr(boot_artifacts, name)
            if (artifact.device, artifact.inode) != (entries[name]["device"], entries[name]["inode"]):
                raise _invalid()
        opened.clear()
        return result
    except (OSError, KeyError, TypeError, ValueError, PalimpsestError):
        raise _invalid() from None
    finally:
        for fd in opened:
            os.close(fd)
