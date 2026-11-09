"""Orphaned native VZ SSH session markers after the owning client died."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from palimpsest_local import vz_native_runtime as rt


def _config(tmp_path):
    return SimpleNamespace(home=tmp_path, verify=lambda **_: None)


def _marker(tmp_path, *, actor_pid=None, owner_pid=1, name="session-" + "ab" * 16 + ".json"):
    root = tmp_path / "_palimpsest-native"
    root.mkdir(mode=0o700, exist_ok=True)
    first = {"schema": rt._SCHEMA, "run_id": "r", "run_name": "x", "owner_pid": owner_pid, "operation": "user-ssh"}
    body = json.dumps(first, sort_keys=True)
    if actor_pid is not None:
        body += "\n" + json.dumps({"schema": rt._SCHEMA, "actor_pid": actor_pid})
    path = root / name
    path.write_text(body)
    path.chmod(0o600)
    return path


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(rt, "_require_native_custody_owner", lambda: None)


def test_default_custody_still_holds_a_marker_whose_owner_and_actor_died(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    with pytest.raises(rt.VZNativeHold, match="survived its owner"):
        with rt._custody(_config(tmp_path)):
            pass
    assert path.exists()


def test_orphan_collection_returns_only_dead_actor_markers_and_keeps_them(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    orphans: list[str] = []
    with rt._custody(_config(tmp_path), orphan_markers=orphans):
        pass
    assert orphans == [path.name] and path.exists()


def test_marker_whose_actor_still_runs_stays_held_even_when_collecting(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path = _marker(tmp_path, actor_pid=process.pid)
        with pytest.raises(rt.VZNativeHold, match="survived its owner"):
            with rt._custody(_config(tmp_path), orphan_markers=[]):
                pass
        assert path.exists()
    finally:
        process.kill()
        process.wait()


def test_marker_without_a_recorded_actor_stays_held_even_when_collecting(tmp_path):
    path = _marker(tmp_path)
    with pytest.raises(rt.VZNativeHold, match="survived its owner"):
        with rt._custody(_config(tmp_path), orphan_markers=[]):
            pass
    assert path.exists()


def test_read_only_probe_never_collects_a_marker(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    with pytest.raises(rt.VZNativeHold):
        with rt._custody(_config(tmp_path), read_only_probe=True, orphan_markers=[]):
            pass
    assert path.exists()


def test_marker_still_locked_by_a_live_owner_is_left_alone(tmp_path):
    path = _marker(tmp_path, actor_pid=os.getpid())
    owner_fd = os.open(path, os.O_RDWR)
    orphans: list[str] = []
    try:
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with rt._custody(_config(tmp_path), orphan_markers=orphans):
            pass
        assert orphans == [] and path.exists()
    finally:
        os.close(owner_fd)


@pytest.mark.parametrize(
    "status",
    [
        {"user_sessions_known": True, "user_session_count": 1},
        {"user_sessions_known": False, "user_session_count": 0},
        {"user_sessions_known": None},
        {},
    ],
)
def test_dead_host_actor_with_a_live_or_unknown_guest_session_stays_held(tmp_path, status):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    with pytest.raises(rt.VZNativeHold, match="not proven gone"):
        rt._settle_orphan_markers(tmp_path / "_palimpsest-native", [path.name], status)
    assert path.exists()


def test_dead_host_actor_with_a_proven_empty_guest_is_settled(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    rt._settle_orphan_markers(
        tmp_path / "_palimpsest-native", [path.name], {"user_sessions_known": True, "user_session_count": 0}
    )
    assert not path.exists()


def test_no_orphans_needs_no_guest_proof(tmp_path):
    rt._settle_orphan_markers(tmp_path, [], {})
