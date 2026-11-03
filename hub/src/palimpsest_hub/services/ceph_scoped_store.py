"""Descriptor-pinned CephFS scopes; namespace layout is part of payload identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import UUID

from .hub_store import HubStoreError

_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_POOL = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def scope_for(actor, visibility, share_id):
    if not isinstance(share_id, str) or str(UUID(share_id)) != share_id:
        raise ValueError("invalid publication share")
    if not all(isinstance(actor.get(key), str) and actor[key] for key in ("user_id", "project_id")):
        raise ValueError("authenticated publication identity is required")
    if visibility == "public":
        return "public"
    if visibility not in ("private", "internal"):
        raise ValueError("invalid publication visibility")
    key, prefix = ("user_id", "user") if visibility == "private" else ("project_id", "project")
    return prefix + "-" + hashlib.sha256(actor[key].encode()).hexdigest()[:32]


def namespace_for(share_id, scope_key):
    if (
        not isinstance(share_id, str)
        or str(UUID(share_id)) != share_id
        or not isinstance(scope_key, str)
        or not re.fullmatch(r"public|(?:user|project)-[0-9a-f]{32}", scope_key)
    ):
        raise ValueError("invalid publication scope")
    return "pal-" + hashlib.sha256(share_id.encode()).hexdigest()[:24] + "-" + scope_key


def _identity(fd):
    metadata = os.fstat(fd)
    values = [
        line.split(":", 1)[1].strip()
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
        if line.startswith("mnt_id:")
    ]
    if len(values) != 1 or not values[0].isdigit():
        raise HubStoreError("scope descriptor lacks mount identity")
    return int(values[0]), metadata.st_dev, metadata.st_ino


def _mount_filesystem(fd, fs_name):
    """Kernel-observed fs selection on this descriptor's mount, not a path guess."""
    try:
        mount_id = _identity(fd)[0]
        selected = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields, filesystem = left.split(), right.split()
            if int(fields[0]) == mount_id:
                if filesystem[0] != "ceph":
                    raise ValueError
                options = fields[5].split(",") + filesystem[2].split(",")
                names = [option.split("=", 1)[1] for option in options if option.startswith(("mds_namespace=", "fs="))]
                selected.append(names)
        if len(selected) != 1 or not selected[0] or any(name != fs_name for name in selected[0]):
            raise ValueError
    except (OSError, ValueError, IndexError):
        raise HubStoreError("Ceph mount filesystem name is missing, contradictory or differs") from None


def _layout(fd, kind, pool, namespace, *, explicit=False):
    try:
        value = json.loads(os.getxattr(fd, f"ceph.{kind}.layout.json"))
        if not isinstance(value, dict) or value.get("pool_name") != pool or value.get("pool_namespace") != namespace:
            raise ValueError
        if explicit and value.get("inheritance") != "@set":
            raise ValueError
    except (OSError, ValueError, TypeError, UnicodeError):
        raise HubStoreError("Ceph payload pool or namespace layout is missing or differs") from None


class NamespaceStore:
    """Own the scope descriptors and keep the already-qualified base alive."""

    def __init__(self, base, binding, scope_key, fs_name, data_pool, *, create=True):
        if (
            not isinstance(fs_name, str)
            or not _TOKEN.fullmatch(fs_name)
            or not isinstance(data_pool, str)
            or not _POOL.fullmatch(data_pool)
        ):
            raise ValueError("exact Ceph filesystem and data pool are required")
        self.base, self.target_fds, self._fds = base, [], []
        self.closed = False
        share_id, base_export = binding["share_id"], binding["export"]
        namespace = namespace_for(share_id, scope_key)
        base_path = base.exported_path
        if not base_export.endswith(":" + base_path) or not base_path.startswith("/"):
            raise ValueError("publication base export differs")
        scope_path = base_path.rstrip("/") + "/cas/" + scope_key
        export = base_export[: -len(base_path)] + scope_path
        self.root = Path(base.root) / "cas" / scope_key
        self.binding = {
            "kind": "manila-namespace-v1",
            "share_id": share_id,
            "base_export": base_export,
            "export": export,
            "root": "blobs/sha256",
            "scope_key": scope_key,
            "fs_name": fs_name,
            "data_pool": data_pool,
            "namespace": namespace,
        }
        try:
            base.check_identity()
            _mount_filesystem(base.fd, fs_name)
            parent = base.fd
            for component in ("cas", scope_key):
                fresh = False
                if create:
                    try:
                        os.mkdir(component, mode=0o755, dir_fd=parent)
                        fresh = True
                    except FileExistsError:
                        pass
                child = os.open(component, _FLAGS, dir_fd=parent)
                self._fds.append(child)
                if _identity(child)[:2] != base.identity[:2]:
                    raise HubStoreError("publication scope filesystem changed")
                if component == scope_key:
                    if fresh:
                        if os.listdir(child):
                            raise HubStoreError("new publication scope is not empty")
                        # No descendants or files have been created in this scope.
                        os.setxattr(child, "ceph.dir.layout.pool", data_pool.encode())
                        os.setxattr(child, "ceph.dir.layout.pool_namespace", namespace.encode())
                        if os.listdir(child):
                            raise HubStoreError("publication scope changed during layout setup")
                        os.fsync(child)
                    _layout(child, "dir", data_pool, namespace, explicit=True)
                if create:
                    os.fsync(parent)
                parent = child
            self.fd = self._fds[-1]
            self.identity = _identity(self.fd)
            self.check_identity()
        except BaseException:
            for fd in reversed(self._fds):
                os.close(fd)
            self._fds.clear()
            raise

    def blob_path(self, digest):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("invalid payload digest")
        return self.root / "blobs" / "sha256" / digest[7:]

    def pin_target(self, directories):
        if not self.target_fds:
            self.target_fds = [os.dup(fd) for fd in directories[1:]]
        self.check_identity()

    def check_identity(self):
        if self.closed:
            raise HubStoreError("publication scope is closed")
        self.base.check_identity()
        _mount_filesystem(self.fd, self.binding["fs_name"])
        parents = [self.base.fd, *self._fds, *self.target_fds]
        names = ("cas", self.binding["scope_key"], "blobs", "sha256")
        try:
            for parent, child, component in zip(parents, parents[1:], names, strict=False):
                current = os.open(component, _FLAGS, dir_fd=parent)
                try:
                    if _identity(current) != _identity(child) or _identity(child)[:2] != self.base.identity[:2]:
                        raise HubStoreError("publication scope identity changed")
                finally:
                    os.close(current)
            for fd in [self.fd, *self.target_fds]:
                _layout(fd, "dir", self.binding["data_pool"], self.binding["namespace"], explicit=fd == self.fd)
        except (OSError, ValueError):
            raise HubStoreError("publication scope identity or layout changed") from None

    def check_payload_layout(self, path_or_fd):
        self.check_identity()
        owned = not isinstance(path_or_fd, int)
        fd = path_or_fd
        try:
            if owned:
                path = (
                    Path("blobs/sha256/" + path_or_fd[7:])
                    if isinstance(path_or_fd, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", path_or_fd)
                    else Path(path_or_fd)
                )
                relative = path.relative_to(self.root) if path.is_absolute() else path
                if (
                    len(relative.parts) != 3
                    or relative.parts[:2] != ("blobs", "sha256")
                    or not re.fullmatch(r"[0-9a-f]{64}", relative.name)
                ):
                    raise ValueError("invalid payload path")
                # Traverse through the pinned namespace, never by ambient path.
                parent = os.open("blobs", _FLAGS, dir_fd=self.fd)
                try:
                    leaf = os.open("sha256", _FLAGS, dir_fd=parent)
                    try:
                        self.pin_target([self.fd, parent, leaf])
                        fd = os.open(
                            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=leaf
                        )
                    finally:
                        os.close(leaf)
                finally:
                    os.close(parent)
            if not stat.S_ISREG(os.fstat(fd).st_mode) or _identity(fd)[:2] != self.identity[:2]:
                raise HubStoreError("publication payload filesystem changed")
            _layout(fd, "file", self.binding["data_pool"], self.binding["namespace"])
            self.check_identity()
        except (OSError, ValueError):
            raise HubStoreError("publication payload layout cannot be verified") from None
        finally:
            if owned and isinstance(fd, int):
                os.close(fd)

    def close(self):
        if not self.closed:
            self.closed = True
            for fd in reversed([*self._fds, *self.target_fds]):
                os.close(fd)
            self.base.close()


def scoped_destination(base_destination, actor, visibility, fs_name, data_pool, *, create=True):
    base, binding = base_destination
    if binding.get("kind") != "manila":
        raise HubStoreError("namespace publication requires a qualified Manila base")
    return NamespaceStore(
        base, binding, scope_for(actor, visibility, binding["share_id"]), fs_name, data_pool, create=create
    )
