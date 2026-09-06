from __future__ import annotations

import json
import os
import resource
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import cli
from palimpsest_local import oci_resource_status as status
from palimpsest_local.errors import UnsupportedPlatformError


def _status(pid: int, *, uids=(1000, 1000, 1000, 1000), threads=1) -> bytes:
    return (
        f"Name:\tredacted\nPid:\t{pid}\nTgid:\t{pid}\n"
        f"Uid:\t{' '.join(str(value) for value in uids)}\nThreads:\t{threads}\n"
    ).encode()


def _proc(root: Path, pid: int, payload: bytes) -> Path:
    directory = root / str(pid)
    directory.mkdir()
    (directory / "status").write_bytes(payload)
    return directory


def _report(root: Path, limits=(300, 400), uid=1000):
    return status._build_resource_status(
        proc_root=os.fspath(root), getrlimit=lambda _resource: limits, getuid=lambda: uid, platform="linux"
    )


def test_counts_processes_once_and_threads_for_real_uid_not_effective_uid(tmp_path):
    _proc(tmp_path, 10, _status(10, uids=(1000, 2000, 2000, 2000), threads=7))
    _proc(tmp_path, 11, _status(11, uids=(2000, 1000, 1000, 1000), threads=9))
    report = _report(tmp_path)
    observed = report["procfs_observation"]
    assert observed["matching_processes_observed"] == 1
    assert observed["matching_threads_observed"] == 7
    assert report["identity_scope"] == "visible_process_leader_real_uid_thread_counts"
    assert report["admission_verdict"] is None


def test_non_ascii_unallowlisted_status_field_is_ignored(tmp_path):
    _proc(tmp_path, 10, b"Name:\t\xff\n" + _status(10).split(b"\n", 1)[1])
    assert _report(tmp_path)["procfs_observation"]["matching_threads_observed"] == 1


@pytest.mark.parametrize(
    ("limits", "soft", "hard"),
    [
        ((300, 400), 256, 256),
        ((100, 200), 100, 200),
        ((resource.RLIM_INFINITY, resource.RLIM_INFINITY), 256, 256),
        ((resource.RLIM_INFINITY, 80), 80, 80),
        ((40, resource.RLIM_INFINITY), 40, 256),
    ],
)
def test_prospective_limits_match_worker_limit_semantics(tmp_path, limits, soft, hard):
    report = _report(tmp_path, limits=limits)
    assert report["worker"] == {
        "configured_ceiling": 256,
        "prospective_soft": soft,
        "prospective_hard": hard,
        "prospective_effective_ceiling": soft,
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"Pid:\t10\nTgid:\t10\nUid:\t1000 1000 1000 1000\n",
        _status(10) + b"Threads:\t1\n",
        b"Pid:\t10\nTgid:\t10\nUid:\t1000 1000 1000 1000\nThreads:\t0\n",
        b"Pid:\t10\nTgid:\t9\nUid:\t1000 1000 1000 1000\nThreads:\t1\n",
        b"Pid:\t10\nTgid:\t10\nUid:\t1000 1000 1000\nThreads:\t1\n",
        b"Pid:\t10\nTgid:\t10\nUid:\t1000 1000 1000 1000\nThreads:\t1",
        b"Pid:\t+10\nTgid:\t10\nUid:\t1000 1000 1000 1000\nThreads:\t1\n",
        b"Pid:\t10\nTgid:\t10\nUid:\t1_000 1000 1000 1000\nThreads:\t1\n",
        b"Pid:\t010\nTgid:\t10\nUid:\t1000 1000 1000 1000\nThreads:\t1\n",
    ],
)
def test_malformed_or_truncated_status_is_partial_not_healthy(tmp_path, payload):
    _proc(tmp_path, 10, payload)
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["partial"] is True
    assert observed["rejected_entries"] == 1
    assert observed["matching_processes_observed"] == 0


def test_oversize_symlink_and_fifo_are_rejected_without_follow_or_hang(tmp_path):
    _proc(tmp_path, 10, b"x" * (status._MAX_STATUS_BYTES + 1))
    target = _proc(tmp_path, 11, _status(11))
    (tmp_path / "12").symlink_to(target, target_is_directory=True)
    directory = tmp_path / "13"
    directory.mkdir()
    (directory / "status").symlink_to(target / "status")
    fifo_dir = tmp_path / "14"
    fifo_dir.mkdir()
    os.mkfifo(fifo_dir / "status")
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["partial"] is True
    assert observed["matching_processes_observed"] == 1
    assert observed["rejected_entries"] == 4


def test_entry_cap_counts_nonnumeric_entries_and_marks_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "_MAX_PROC_ENTRIES", 2)
    (tmp_path / "aaa").mkdir()
    (tmp_path / "bbb").mkdir()
    _proc(tmp_path, 10, _status(10))
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["entries_examined"] == 2
    assert observed["partial"] is True


@pytest.mark.parametrize("failure", [PermissionError(13, "redacted"), FileNotFoundError(2, "redacted")])
def test_vanishing_and_invisible_entries_are_explicitly_partial(tmp_path, monkeypatch, failure):
    _proc(tmp_path, 10, _status(10))
    real_open = status.os.open

    def inaccessible(path, flags, *args, **kwargs):
        if path == "10":
            raise failure
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(status.os, "open", inaccessible)
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["unavailable_entries"] == 1
    assert observed["partial"] is True


def test_unsupported_platform_is_typed_and_does_not_scan(tmp_path):
    with pytest.raises(UnsupportedPlatformError, match="only on Linux"):
        status._build_resource_status(platform="darwin", proc_root=os.fspath(tmp_path / "absent"))


def test_unavailable_proc_root_is_partial_without_exposing_path(tmp_path):
    report = _report(tmp_path / "secret-absent")
    observed = report["procfs_observation"]
    assert observed["scan_unavailable"] is True
    assert observed["partial"] is True
    assert observed["matching_processes_observed"] is None
    assert observed["matching_threads_observed"] is None
    assert "secret-absent" not in json.dumps(report)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_proc_root_symlink_or_fifo_is_unavailable_without_hang(tmp_path, kind):
    root = tmp_path / "proc"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    else:
        os.mkfifo(root)
    observed = _report(root)["procfs_observation"]
    assert observed["scan_unavailable"] is True
    assert observed["matching_threads_observed"] is None


def test_short_reads_continue_to_eof(tmp_path, monkeypatch):
    _proc(tmp_path, 10, _status(10, threads=3))
    real_read = status.os.read

    def short_read(fd, count):
        return real_read(fd, min(count, 5))

    monkeypatch.setattr(status.os, "read", short_read)
    assert _report(tmp_path)["procfs_observation"]["matching_threads_observed"] == 3


def test_short_reads_reject_oversize_after_valid_prefix_when_stat_size_is_zero(tmp_path, monkeypatch):
    _proc(tmp_path, 10, _status(10) + b"x" * status._MAX_STATUS_BYTES)
    real_read = status.os.read
    real_fstat = status.os.fstat

    def zero_size(fd):
        actual = real_fstat(fd)
        return SimpleNamespace(st_mode=actual.st_mode, st_size=0)

    monkeypatch.setattr(status.os, "fstat", zero_size)
    monkeypatch.setattr(status.os, "read", lambda fd, count: real_read(fd, min(count, 7)))
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["rejected_entries"] == 1
    assert observed["matching_threads_observed"] == 0


def test_scandir_creation_failure_is_sanitized_and_closes_proc_fd(tmp_path, monkeypatch):
    captured = []

    def fail(fd):
        captured.append(fd)
        raise OSError(5, "secret raw failure")

    monkeypatch.setattr(status.os, "scandir", fail)
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["scan_unavailable"] is True
    assert observed["matching_threads_observed"] is None
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_scandir_iteration_failure_preserves_lower_bound_and_closes_fds(tmp_path, monkeypatch):
    _proc(tmp_path, 10, _status(10, threads=4))
    real_scandir = status.os.scandir
    captured = []

    class FailingIterator:
        def __init__(self, fd):
            captured.append(fd)
            self.inner = real_scandir(fd)
            self.yielded = False
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True
            self.inner.close()

        def __iter__(self):
            return self

        def __next__(self):
            if self.yielded:
                raise OSError(5, "secret raw failure")
            self.yielded = True
            return next(self.inner)

    wrapper = None

    def make(fd):
        nonlocal wrapper
        wrapper = FailingIterator(fd)
        return wrapper

    monkeypatch.setattr(status.os, "scandir", make)
    observed = _report(tmp_path)["procfs_observation"]
    assert observed["scan_unavailable"] is True
    assert observed["partial"] is True
    assert observed["matching_threads_observed"] == 4
    assert wrapper is not None and wrapper.closed is True
    with pytest.raises(OSError):
        os.fstat(captured[0])


def test_public_cli_routes_before_state_and_does_not_launch_or_set_limits(tmp_path, monkeypatch, capsys):
    _proc(tmp_path, 10, _status(10))
    monkeypatch.setattr(status, "_PROC_ROOT", os.fspath(tmp_path))
    monkeypatch.setattr(status, "resource_status", lambda: _report(tmp_path))
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("state initialized"))
    monkeypatch.setattr(cli, "resolve_roots", lambda: pytest.fail("state resolved"))
    monkeypatch.setattr(resource, "setrlimit", lambda *args: pytest.fail("limit changed"))
    monkeypatch.setattr(__import__("subprocess"), "Popen", lambda *args, **kwargs: pytest.fail("process launched"))
    assert cli.main(["oci", "resource-status"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == status.RESOURCE_STATUS_SCHEMA
    assert not list(tmp_path.glob("config*")) and not list(tmp_path.glob("state*"))


def test_public_cli_unsupported_failure_is_before_state_and_has_no_stdout(monkeypatch, capsys):
    monkeypatch.setattr(status, "resource_status", lambda: (_ for _ in ()).throw(UnsupportedPlatformError("no")))
    monkeypatch.setattr(cli, "init_roots", lambda: pytest.fail("state initialized"))
    assert cli.main(["oci", "resource-status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "no\n"
