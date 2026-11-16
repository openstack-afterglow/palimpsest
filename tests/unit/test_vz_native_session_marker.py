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


def _marker(tmp_path, *, actor_pid=None, owner_pid=1, name="session-" + "ab" * 16 + ".json", run_id="r", run_name="x"):
    root = tmp_path / "_palimpsest-native"
    root.mkdir(mode=0o700, exist_ok=True)
    first = {"schema": rt._SCHEMA, "run_name": run_name, "owner_pid": owner_pid, "operation": "user-ssh"}
    if run_id is not None:
        first["run_id"] = run_id
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


def test_orphan_collection_returns_only_dead_actor_markers_with_their_run_and_keeps_them(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid())
    orphans: list[tuple[str, str, str]] = []
    with rt._custody(_config(tmp_path), orphan_markers=orphans):
        pass
    assert orphans == [(path.name, "r", "x")] and path.exists()


def test_a_dead_actor_marker_that_names_no_run_stays_held(tmp_path):
    path = _marker(tmp_path, actor_pid=_dead_pid(), run_id=None)
    with pytest.raises(rt.VZNativeHold, match="survived its owner"):
        with rt._custody(_config(tmp_path), orphan_markers=[]):
            pass
    assert path.exists()


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


def _session_harness(monkeypatch, tmp_path, *, provider_status, guest_status=None, run_id="r"):
    """Drive `_session` up to its guest status request with every other dependency replaced."""
    from contextlib import contextmanager

    authority = SimpleNamespace(record=SimpleNamespace(run_id=run_id))
    calls = []

    @contextmanager
    def locked(*_args, **_kwargs):
        yield authority

    def control(*_args, **_kwargs):
        calls.append("status")
        if guest_status is None:
            raise rt.StateError("guest is not running")
        return guest_status

    def owned_instance(*_args, **_kwargs):
        calls.append("instance")
        return {"status": provider_status}

    monkeypatch.setattr(rt, "locked_existing_run", locked)
    monkeypatch.setattr(rt, "_config_for", lambda _authority: _config(tmp_path))
    monkeypatch.setattr(rt, "_load", lambda *_args, **_kwargs: ({"vz_native": {}},))
    monkeypatch.setattr(rt, "_owned_instance", owned_instance)
    monkeypatch.setattr(rt, "_control", control)
    monkeypatch.setattr(rt, "_validate_status", lambda _native, value, **_kwargs: value)
    return calls


def test_a_session_does_not_settle_another_runs_marker_on_its_own_guest_status(monkeypatch, tmp_path):
    # The custody directory is shared by every run of one Lima home. This run's empty guest
    # proves nothing about a session that may still run in the other run's guest.
    other = _marker(tmp_path, actor_pid=_dead_pid(), run_id="other", run_name="other-run")
    calls = _session_harness(
        monkeypatch,
        tmp_path,
        provider_status="Running",
        guest_status={"user_sessions_known": True, "user_session_count": 0},
    )
    with pytest.raises(rt.VZNativeHold, match="another run survived its owner.*'other-run'"):
        rt._session("x", roots=None, expected=None)
    assert other.exists() and calls == []


def test_a_session_settles_its_own_marker_when_its_provider_is_stopped(monkeypatch, tmp_path):
    # A stopped guest cannot answer a status request; without this the marker held the whole
    # namespace with no command able to release it.
    own = _marker(tmp_path, actor_pid=_dead_pid())
    calls = _session_harness(monkeypatch, tmp_path, provider_status="Stopped")
    with pytest.raises(rt.StateError, match="guest is not running"):
        rt._session("x", roots=None, expected=None)
    assert not own.exists() and calls == ["instance", "status"]


def test_a_session_keeps_its_own_marker_while_its_running_guest_still_has_a_user_session(monkeypatch, tmp_path):
    own = _marker(tmp_path, actor_pid=_dead_pid())
    _session_harness(
        monkeypatch,
        tmp_path,
        provider_status="Running",
        guest_status={"user_sessions_known": True, "user_session_count": 1},
    )
    with pytest.raises(rt.VZNativeHold, match="not proven gone"):
        rt._session("x", roots=None, expected=None)
    assert own.exists()


def test_two_runs_that_each_left_a_marker_do_not_block_each_other(monkeypatch, tmp_path):
    # Both clients were killed (a host reboot is enough). Refusing on the other run's marker
    # before settling the own one left both markers in place with no command able to remove them.
    own = _marker(tmp_path, actor_pid=_dead_pid(), name="session-" + "aa" * 16 + ".json")
    other = _marker(
        tmp_path, actor_pid=_dead_pid(), run_id="other", run_name="other-run", name="session-" + "bb" * 16 + ".json"
    )
    _session_harness(monkeypatch, tmp_path, provider_status="Stopped")
    with pytest.raises(rt.VZNativeHold, match="another run survived its owner.*'other-run'"):
        rt._session("x", roots=None, expected=None)
    assert not own.exists() and other.exists()
    calls = _session_harness(monkeypatch, tmp_path, provider_status="Stopped", run_id="other")
    with pytest.raises(rt.StateError, match="guest is not running"):
        rt._session("other-run", roots=None, expected=None)
    assert not other.exists() and calls == ["instance", "status"]


def test_a_running_guest_with_a_session_keeps_its_marker_even_beside_another_runs_marker(monkeypatch, tmp_path):
    own = _marker(tmp_path, actor_pid=_dead_pid(), name="session-" + "aa" * 16 + ".json")
    other = _marker(
        tmp_path, actor_pid=_dead_pid(), run_id="other", run_name="other-run", name="session-" + "bb" * 16 + ".json"
    )
    _session_harness(
        monkeypatch,
        tmp_path,
        provider_status="Running",
        guest_status={"user_sessions_known": True, "user_session_count": 1},
    )
    with pytest.raises(rt.VZNativeHold, match="not proven gone"):
        rt._session("x", roots=None, expected=None)
    assert own.exists() and other.exists()
