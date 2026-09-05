"""A test-only QEMU ACL adapter must not become a general authority bypass."""

from __future__ import annotations

import hashlib
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
    with pytest.raises(ValueError, match="metadata rejected") as captured:
        adapter("kernel", entry, granted, granted, os.geteuid())
    assert "ACL changed" in str(captured.value.__cause__)


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


@pytest.mark.parametrize("tamper", [None, "inode", "owner", "mode", "acl"])
def test_runtime_console_can_grow_but_retains_exact_identity_and_named_grant(tamper):
    original = _stat(st_mode=stat.S_IFREG | 0o600, st_size=0)
    granted = _stat(st_mode=stat.S_IFREG | 0o660, st_size=0, st_ctime_ns=40)
    broker = Broker(original, granted, False)
    broker.targets[0].permission = "rw-"
    broker.targets[0].original_acl = ACL("rw-", (), "---", None, "---")
    broker.acl = ACL("rw-", ((broker.uid, "rw-"),), "---", "rw-", "---")
    current = SimpleNamespace(**{**vars(granted), "st_size": 90, "st_mtime_ns": 100, "st_ctime_ns": 101})
    broker.current = current
    entry = {
        name: getattr(original, field)
        for name, field in (
            ("device", "st_dev"),
            ("inode", "st_ino"),
            ("uid", "st_uid"),
            ("gid", "st_gid"),
            ("nlink", "st_nlink"),
            ("mode", "st_mode"),
            ("size", "st_size"),
            ("mtime_ns", "st_mtime_ns"),
            ("ctime_ns", "st_ctime_ns"),
        )
    }
    context = {"broker": broker, "granted": {(1, 2): granted}}
    adapter = _HELPER["_qualification_metadata_adapter"](_validate_entry_metadata, context, ACL, _acl_mode)
    if tamper == "acl":
        broker.acl = replace(broker.acl, named_users=((broker.uid + 1, "rw-"),))
    elif tamper is not None:
        field = {"inode": "st_ino", "owner": "st_uid", "mode": "st_mode"}[tamper]
        setattr(current, field, getattr(current, field) + 1)
    if tamper is None:
        adapter("runtime_console", entry, current, current, os.geteuid())
    else:
        with pytest.raises((StateError, ValueError)):
            adapter("runtime_console", entry, current, current, os.geteuid())


@pytest.mark.parametrize("case", ["no-broker", "not-applied", "not-target", "monitor"])
def test_nonbroker_and_monitor_entries_always_use_strict_policy(case):
    adapter, broker, entry, _original, granted = _setup(directory=True)
    if case == "no-broker":
        adapter = _HELPER["_qualification_metadata_adapter"](_validate_entry_metadata, {}, ACL, _acl_mode)
    elif case == "not-applied":
        broker.applied = False
    elif case == "not-target":
        broker.targets = []
    with pytest.raises(ValueError, match="metadata rejected") as captured:
        adapter("monitor" if case == "monitor" else "run", entry, granted, granted, os.geteuid())
    assert isinstance(captured.value.__cause__, StateError)


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


def test_metadata_diagnostics_identify_key_and_numeric_changes_without_paths():
    adapter, _broker, entry, _original, granted = _setup()
    entry["path"] = "/private/secret/artifact"
    entry["secret"] = "never include frame payload"
    visible = SimpleNamespace(**{**vars(granted), "st_size": granted.st_size + 1})
    with pytest.raises(ValueError, match="metadata rejected") as captured:
        adapter("kernel", entry, granted, visible, os.geteuid())
    message = str(captured.value)
    assert '"key": "kernel"' in message
    assert '"size": [10, 11]' in message
    assert "secret" not in message and "/private" not in message and "payload" not in message


def test_exception_evidence_includes_suppressed_context_without_locals():
    def fail():
        secret_local = "do-not-print-this-local-value"
        assert secret_local
        try:
            raise ValueError("inner metadata failure")
        except ValueError:
            raise StateError("outer stable failure") from None

    try:
        fail()
    except StateError as error:
        evidence = _HELPER["_exception_evidence"](error)
    assert "inner metadata failure" in evidence and "outer stable failure" in evidence
    assert "Exception context 1" in evidence
    assert "do-not-print-this-local-value" not in evidence


def test_exception_evidence_bounds_and_deduplicates_context_chain():
    exceptions = [ValueError(f"marker-{index}") for index in range(12)]
    for left, right in zip(exceptions, exceptions[1:], strict=False):
        left.__context__ = right
    evidence = _HELPER["_exception_evidence"](exceptions[0])
    assert "marker-7" in evidence and "marker-8" not in evidence
    exceptions[1].__context__ = exceptions[0]
    evidence = _HELPER["_exception_evidence"](exceptions[0])
    assert evidence.count("Exception context") == 2


def _boot_setup(monkeypatch, *, active=1):
    _adapter, broker, entry, _original, granted = _setup()
    policy = {
        "uid": os.geteuid() + 1,
        "gid": os.getegid() + 1,
        "reader_uid": os.geteuid(),
        "prove_activity": lambda: active,
        "digests": {"kernel": "sha256:" + hashlib.sha256(b"x" * 10).hexdigest()},
    }
    observed = SimpleNamespace(
        **{
            **vars(granted),
            "st_uid": policy["uid"] if active else entry["uid"],
            "st_gid": policy["gid"] if active else entry["gid"],
            "st_ctime_ns": granted.st_ctime_ns + 10,
        }
    )
    target = broker.targets[0]
    broker.acl = replace(broker.acl, named_users=tuple(sorted(((policy["uid"], "r--"), (policy["reader_uid"], "r--")))))
    target.descriptor = 123
    target.path = SimpleNamespace(lstat=lambda: observed)
    fake_os = SimpleNamespace(fstat=lambda _: observed, pread=lambda _fd, size, _offset: b"x" * size)
    monkeypatch.setitem(_HELPER["_validate_qualified_boot_relabel"].__globals__, "os", fake_os)
    context = {"broker": broker, "granted": {(1, 2): granted}, "boot_relabel": policy}
    adapter = _HELPER["_qualification_metadata_adapter"](_validate_entry_metadata, context, ACL, _acl_mode)
    return adapter, broker, entry, observed, policy, fake_os


@pytest.mark.parametrize("active", [0, 1])
def test_boot_relabel_requires_exact_active_qemu_or_inactive_original_owner(monkeypatch, active):
    adapter, _broker, entry, observed, _policy, _fake_os = _boot_setup(monkeypatch, active=active)
    with pytest.raises(StateError):
        _validate_entry_metadata("kernel", entry, observed, observed, os.geteuid())
    adapter("kernel", entry, observed, observed, os.geteuid())


@pytest.mark.parametrize("active", [0, 1])
@pytest.mark.parametrize(
    "field", ["st_uid", "st_gid", "st_ino", "st_dev", "st_mode", "st_size", "st_nlink", "st_mtime_ns"]
)
def test_boot_relabel_rejects_unapproved_owner_or_immutable_metadata(monkeypatch, active, field):
    adapter, _broker, entry, observed, _policy, _fake_os = _boot_setup(monkeypatch, active=active)
    setattr(observed, field, getattr(observed, field) + 1)
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


@pytest.mark.parametrize("active", [0, 1])
def test_boot_relabel_cannot_use_other_phase_owner(monkeypatch, active):
    adapter, _broker, entry, observed, policy, _fake_os = _boot_setup(monkeypatch, active=active)
    observed.st_uid, observed.st_gid = (entry["uid"], entry["gid"]) if active else (policy["uid"], policy["gid"])
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


@pytest.mark.parametrize("activity", [True, -1, 2, None])
def test_boot_relabel_rejects_ambiguous_domain_activity(monkeypatch, activity):
    adapter, _broker, entry, observed, policy, _fake_os = _boot_setup(monkeypatch)
    policy["prove_activity"] = lambda: activity
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


def test_boot_relabel_rechecks_bound_domain_after_hash(monkeypatch):
    adapter, _broker, entry, observed, policy, _fake_os = _boot_setup(monkeypatch)
    activities = iter([1, 0])
    policy["prove_activity"] = lambda: next(activities)
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


def test_boot_relabel_rechecks_hash_even_if_size_mtime_preserved(monkeypatch):
    adapter, _broker, entry, observed, _policy, fake_os = _boot_setup(monkeypatch)
    fake_os.pread = lambda _fd, size, _offset: b"y" * size
    with pytest.raises(ValueError, match="metadata rejected") as captured:
        adapter("kernel", entry, observed, observed, os.geteuid())
    assert "digest changed" in str(captured.value.__cause__)


def test_boot_relabel_rejects_ctime_change_during_hash(monkeypatch):
    adapter, _broker, entry, observed, _policy, fake_os = _boot_setup(monkeypatch)

    def read(_fd, size, _offset):
        fake_os.fstat = lambda _: SimpleNamespace(**{**vars(observed), "st_ctime_ns": observed.st_ctime_ns + 1})
        return b"x" * size

    fake_os.pread = read
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


def test_boot_relabel_keeps_exact_acl_check(monkeypatch):
    adapter, broker, entry, observed, _policy, _fake_os = _boot_setup(monkeypatch)
    broker.acl = replace(broker.acl, named_users=((9999, "rwx"),))
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


@pytest.mark.parametrize("change", ["extra-user", "missing-reader", "reader-write"])
def test_boot_relabel_reader_acl_is_explicit_and_exclusive(monkeypatch, change):
    adapter, broker, entry, observed, policy, _fake_os = _boot_setup(monkeypatch)
    if change == "extra-user":
        users = (*broker.acl.named_users, (99999, "r--"))
    elif change == "missing-reader":
        users = ((policy["uid"], "r--"),)
    else:
        users = ((policy["uid"], "r--"), (policy["reader_uid"], "rw-"))
    broker.acl = replace(broker.acl, named_users=tuple(sorted(users)))
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("kernel", entry, observed, observed, os.geteuid())


def test_boot_relabel_policy_does_not_apply_to_nonboot_entries(monkeypatch):
    adapter, _broker, entry, observed, _policy, _fake_os = _boot_setup(monkeypatch)
    with pytest.raises(ValueError, match="metadata rejected"):
        adapter("run", entry, observed, observed, os.geteuid())
