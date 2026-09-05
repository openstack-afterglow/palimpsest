"""A test-only QEMU ACL adapter must not become a general authority bypass."""

from __future__ import annotations

import os
import runpy
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local.errors import StateError
from palimpsest_local.oci_monitor_launch import _validate_entry_metadata

_HELPER = runpy.run_path(str(Path(__file__).parents[1] / "kvm" / "oci_monitor_launch_helper.py"))


@dataclass(frozen=True)
class ACL:
    user: str
    named_users: tuple[tuple[int, str], ...]
    group: str
    mask: str | None
    other: str


def _acl_mode(value):
    return sum(bit for char, bit in zip(value, (4, 2, 1), strict=True) if char != "-")


def _stat(*, directory=False, **changes):
    fields = dict(
        st_dev=1,
        st_ino=2,
        st_uid=os.geteuid(),
        st_gid=os.getegid(),
        st_nlink=1,
        st_mode=(stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o400),
        st_size=10,
        st_mtime_ns=20,
        st_ctime_ns=30,
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


class Broker:
    def __init__(self, original, granted, directory):
        self.uid = os.geteuid() + 1
        self.applied = True
        self.targets = [
            SimpleNamespace(
                opened=original,
                original_acl=ACL("rwx" if directory else "r--", (), "---", None, "---"),
                permission="-wx" if directory else "r--",
            )
        ]
        self.current = granted
        self.acl = ACL(
            "rwx" if directory else "r--",
            ((self.uid, self.targets[0].permission),),
            "---",
            self.targets[0].permission,
            "---",
        )

    def _verify_held(self, target):
        if any(
            getattr(self.current, field) != getattr(target.opened, field)
            for field in ("st_dev", "st_ino", "st_uid", "st_gid", "st_nlink")
        ):
            raise ValueError("changed held identity")
        return self.current

    def _getfacl(self, target):
        return self.acl


def _setup(directory=False):
    original = _stat(directory=directory)
    granted = _stat(directory=directory, st_mode=original.st_mode | (0o030 if directory else 0o040), st_ctime_ns=40)
    broker = Broker(original, granted, directory)
    context = {"broker": broker, "granted": {(1, 2): granted}}
    adapter = _HELPER["_qualification_metadata_adapter"](_validate_entry_metadata, context, ACL, _acl_mode)
    entry = {
        "device": original.st_dev,
        "inode": original.st_ino,
        "uid": original.st_uid,
        "gid": original.st_gid,
        "nlink": original.st_nlink,
        "mode": original.st_mode,
        "size": original.st_size,
        "mtime_ns": original.st_mtime_ns,
        "ctime_ns": original.st_ctime_ns,
    }
    return adapter, broker, entry, original, granted


@pytest.mark.parametrize("directory", [False, True])
def test_only_exact_recorded_named_qemu_grant_is_accepted(directory):
    adapter, _broker, entry, _original, granted = _setup(directory)
    key = "run" if directory else "kernel"
    with pytest.raises(StateError):
        _validate_entry_metadata(key, entry, granted, granted, os.geteuid())
    adapter(key, entry, granted, granted, os.geteuid())


@pytest.mark.parametrize(
    "change",
    [
        {"named_users": ()},
        {"named_users": ((99999, "r--"),)},
        {"mask": "rwx"},
        {"other": "r--"},
        {"group": "r--"},
        {"user": "rw-"},
    ],
)
def test_unexpected_acl_is_rejected(change):
    adapter, broker, entry, _original, granted = _setup()
    broker.acl = replace(broker.acl, **change)
    with pytest.raises(ValueError, match="ACL changed"):
        adapter("kernel", entry, granted, granted, os.geteuid())


@pytest.mark.parametrize(
    "field,delta",
    [("st_mode", 1), ("st_ctime_ns", 1), ("st_size", 1), ("st_mtime_ns", 1), ("st_gid", 1), ("st_nlink", 1)],
)
@pytest.mark.parametrize("side", ["opened", "visible", "broker"])
def test_non_acl_metadata_and_granted_boot_ctime_remain_pinned(field, delta, side):
    adapter, broker, entry, _original, granted = _setup()
    changed = SimpleNamespace(**vars(granted))
    setattr(changed, field, getattr(changed, field) + delta)
    opened = changed if side == "opened" else granted
    visible = changed if side == "visible" else granted
    if side == "broker":
        broker.current = changed
        # Held-path broker validates identity only; byte metadata authority is the
        # opened+visible snapshot outside it, so represent that same file state.
        if field in {"st_size", "st_mtime_ns"}:
            opened = changed
    with pytest.raises((StateError, ValueError)):
        adapter("kernel", entry, opened, visible, os.geteuid())


def test_directory_ctime_can_change_after_grant_for_normal_state_writes():
    adapter, broker, entry, _original, granted = _setup(directory=True)
    current = SimpleNamespace(**{**vars(granted), "st_ctime_ns": granted.st_ctime_ns + 100})
    broker.current = current
    adapter("run", entry, current, current, os.geteuid())


@pytest.mark.parametrize("case", ["no-broker", "not-applied", "not-target", "monitor"])
def test_nonbroker_and_monitor_entries_always_use_strict_policy(case):
    adapter, broker, entry, _original, granted = _setup(directory=True)
    if case == "no-broker":
        adapter = _HELPER["_qualification_metadata_adapter"](_validate_entry_metadata, {}, ACL, _acl_mode)
    elif case == "not-applied":
        broker.applied = False
    elif case == "not-target":
        broker.targets = []
    with pytest.raises(StateError):
        adapter("monitor" if case == "monitor" else "run", entry, granted, granted, os.geteuid())


@pytest.mark.parametrize(
    "address", ["/private/run/lifecycle.sock", b"/private/run/lifecycle.sock", Path("/private/run/lifecycle.sock")]
)
def test_child_direct_lifecycle_connection_guard_rejects_private_socket(address):
    guard = _HELPER["_guard_lifecycle_connect"](
        lambda *_: pytest.fail("socket reached"), Path("/private/run/lifecycle.sock")
    )
    with pytest.raises(AssertionError, match="direct lifecycle"):
        guard(object(), address)


def test_child_connection_guard_preserves_unrelated_ipc_and_network_calls():
    received = []
    guard = _HELPER["_guard_lifecycle_connect"](
        lambda channel, address: received.append((channel, address)), Path("/private/run/lifecycle.sock")
    )
    channel = object()
    for address in ("/private/run/monitor.sock", ("localhost", 1234)):
        guard(channel, address)
    assert received == [(channel, "/private/run/monitor.sock"), (channel, ("localhost", 1234))]
