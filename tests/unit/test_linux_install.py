"""Tests for conservative Linux service-account provisioning."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import linux_install
from palimpsest_local.errors import PalimpsestError


def _identity(uid: int = 991, gid: int = 992):
    group = SimpleNamespace(gr_name="palimpsest", gr_gid=gid)
    user = SimpleNamespace(
        pw_name="palimpsest",
        pw_uid=uid,
        pw_gid=gid,
        pw_dir="/var/lib/palimpsest",
        pw_shell="/usr/sbin/nologin",
    )
    return user, group


def test_provision_requires_linux_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux_install.sys, "platform", "darwin")
    with pytest.raises(PalimpsestError, match="only on Linux"):
        linux_install.provision()
    monkeypatch.setattr(linux_install.sys, "platform", "linux")
    monkeypatch.setattr(linux_install.os, "geteuid", lambda: 1000)
    with pytest.raises(PalimpsestError, match="must run as root"):
        linux_install.provision()


def test_existing_valid_identity_and_directories_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "var" / "lib" / "palimpsest"
    log_root = tmp_path / "var" / "log" / "palimpsest"
    state_root.mkdir(parents=True, mode=0o700)
    log_root.mkdir(parents=True, mode=0o700)
    os.chmod(state_root, 0o700)
    os.chmod(log_root, 0o700)
    user, group = _identity(os.getuid(), os.getgid())
    user.pw_dir = str(state_root)
    monkeypatch.setattr(linux_install, "STATE_ROOT", state_root)
    monkeypatch.setattr(linux_install, "LOG_ROOT", log_root)
    monkeypatch.setattr(linux_install, "HOME", state_root)
    monkeypatch.setattr(linux_install.sys, "platform", "linux")
    monkeypatch.setattr(linux_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(linux_install, "_validate_ancestor", lambda _path: None)
    monkeypatch.setattr(linux_install, "_lookup_group", lambda: group)
    monkeypatch.setattr(linux_install, "_lookup_user", lambda: user)
    monkeypatch.setattr(linux_install, "_run", lambda _command: pytest.fail("must not mutate accounts"))

    linux_install.provision()


def test_provision_creates_missing_identity_then_fixed_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    log_root = tmp_path / "log"
    user, group = _identity(os.getuid(), os.getgid())
    user.pw_dir = str(state_root)
    groups = iter((None, group))
    users = iter((None, user))
    commands: list[tuple[str, ...]] = []
    directories: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(linux_install, "STATE_ROOT", state_root)
    monkeypatch.setattr(linux_install, "LOG_ROOT", log_root)
    monkeypatch.setattr(linux_install, "HOME", state_root)
    monkeypatch.setattr(linux_install.sys, "platform", "linux")
    monkeypatch.setattr(linux_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(linux_install, "_validate_ancestor", lambda _path: None)
    monkeypatch.setattr(linux_install, "_lookup_group", lambda: next(groups))
    monkeypatch.setattr(linux_install, "_lookup_user", lambda: next(users))
    monkeypatch.setattr(linux_install, "_tool", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(linux_install.Path, "is_file", lambda _path: True)
    monkeypatch.setattr(linux_install, "_run", lambda command: commands.append(tuple(command)))
    monkeypatch.setattr(
        linux_install,
        "_ensure_directory",
        lambda path, uid, gid: directories.append((path, uid, gid)),
    )

    linux_install.provision()

    assert [Path(command[0]).name for command in commands] == ["groupadd", "useradd"]
    assert directories == [(state_root, user.pw_uid, group.gr_gid), (log_root, user.pw_uid, group.gr_gid)]


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("conflict", ["group", "user"])
def test_conflicting_identity_is_rejected_before_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    user, group = _identity()
    if conflict == "group":
        group.gr_gid = 0
    else:
        user.pw_shell = "/bin/bash"
    monkeypatch.setattr(linux_install.sys, "platform", "linux")
    monkeypatch.setattr(linux_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(linux_install, "_validate_ancestor", lambda _path: None)
    monkeypatch.setattr(linux_install, "_lookup_group", lambda: group)
    monkeypatch.setattr(linux_install, "_lookup_user", lambda: user)
    monkeypatch.setattr(linux_install, "_ensure_directory", lambda *_args: pytest.fail("must not touch dirs"))
    with pytest.raises(PalimpsestError, match="conflicts"):
        linux_install.provision()


def test_existing_directory_conflicts_are_never_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "palimpsest"
    path.mkdir(mode=0o755)
    monkeypatch.setattr(linux_install, "_validate_ancestor", lambda _path: None)
    with pytest.raises(PalimpsestError, match="conflicts"):
        linux_install._ensure_directory(path, os.getuid(), os.getgid())
    assert stat_mode(path) == 0o755


def test_symlink_directory_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(linux_install, "_validate_ancestor", lambda _path: None)
    with pytest.raises(PalimpsestError, match="conflicts"):
        linux_install._ensure_directory(linked, os.getuid(), os.getgid())


def test_writable_ancestor_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = SimpleNamespace(st_mode=0o40777, st_uid=0)
    monkeypatch.setattr(linux_install.os, "lstat", lambda _path: unsafe)
    with pytest.raises(PalimpsestError, match="untrusted"):
        linux_install._validate_ancestor(Path("/var/lib/palimpsest"))
