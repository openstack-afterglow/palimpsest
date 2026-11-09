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


def test_marker_of_a_dead_owner_and_dead_actor_is_settled_not_held(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    with rt._custody(_config(tmp_path)) as root:
        assert root == tmp_path / "_palimpsest-native"
    assert not path.exists()


def test_marker_whose_actor_still_runs_stays_held(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path = _marker(tmp_path, actor_pid=process.pid)
        with pytest.raises(rt.VZNativeHold, match="survived its owner"):
            with rt._custody(_config(tmp_path)):
                pass
        assert path.exists()
    finally:
        process.kill()
        process.wait()


def test_marker_without_a_recorded_actor_stays_held(tmp_path):
    path = _marker(tmp_path)
    with pytest.raises(rt.VZNativeHold, match="survived its owner"):
        with rt._custody(_config(tmp_path)):
            pass
    assert path.exists()


def test_read_only_probe_never_settles_a_marker(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    with pytest.raises(rt.VZNativeHold):
        with rt._custody(_config(tmp_path), read_only_probe=True):
            pass
    assert path.exists()


def test_marker_still_locked_by_a_live_owner_is_left_alone(tmp_path):
    path = _marker(tmp_path, actor_pid=os.getpid())
    owner_fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with rt._custody(_config(tmp_path)):
            pass
        assert path.exists()
    finally:
        os.close(owner_fd)
