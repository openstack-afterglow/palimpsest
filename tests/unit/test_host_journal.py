import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from palimpsest_local import cli, host_journal, state
from palimpsest_local.errors import PalimpsestError


def _write(root: str, start, results, ordinal: int, lock_seconds: float) -> None:
    host_journal._LOCK_SECONDS = lock_seconds
    start.wait(5)
    try:
        host_journal._append(Path(root), "start", "ps", f"{ordinal:032x}", None)
    except Exception as exc:
        results.put((ordinal, type(exc).__name__, str(exc)))
    else:
        results.put((ordinal, "ok", ""))


def test_journal_records_ordered_safe_lifecycle(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path))
    journal = host_journal.begin("run")
    journal.finish("success")
    records = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert [item["sequence"] for item in records] == [1, 2]
    assert [(item["phase"], item.get("result")) for item in records] == [("start", None), ("end", "success")]
    assert all(
        set(item) <= {"schema", "sequence", "invocation_id", "observed_at", "monotonic_ns", "phase", "family", "result"}
        for item in records
    )


@pytest.fixture
def user_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(host_journal.sys, "platform", "linux")
    monkeypatch.setattr(state, "uses_system_defaults", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for name in ("PALIMPSEST_LOG_HOME", "PALIMPSEST_STATE_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / ".local" / "state" / "palimpsest"


@pytest.mark.parametrize("existing", [False, True])
def test_default_user_store_commands_record_real_private_journal(user_defaults, capsys, existing):
    root = user_defaults
    preserved = root / "retained-metadata"
    if existing:
        root.mkdir(parents=True, mode=0o700)
        preserved.write_bytes(b"existing state must not move")
    assert host_journal._root() == (root / "logs", False, True)
    assert not (root / "logs").exists()
    assert cli.main(["store", "show", "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["state_root"] == str(root)
    assert cli.main(["store", "ls", "--kind", "image", "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and json.loads(captured.out) == []
    log = root / "logs" / "commands.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [item["sequence"] for item in records] == [1, 2, 3, 4]
    assert [(item["phase"], item.get("result")) for item in records] == [
        ("start", None),
        ("end", "success"),
        ("start", None),
        ("end", "success"),
    ]
    assert all(item["family"] == "store" for item in records)
    assert records[0]["invocation_id"] == records[1]["invocation_id"]
    assert records[2]["invocation_id"] == records[3]["invocation_id"]
    assert records[0]["invocation_id"] != records[2]["invocation_id"]
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert log.stat().st_uid == log.parent.stat().st_uid == os.geteuid()
    if existing:
        assert preserved.read_bytes() == b"existing state must not move"


@pytest.mark.parametrize("selection", ["xdg", "config", "env"])
def test_default_journal_follows_selected_state_namespace(user_defaults, tmp_path, monkeypatch, capsys, selection):
    selected = tmp_path / "selected"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    if selection == "xdg":
        selected = tmp_path / "xdg" / "palimpsest"
    else:
        config = tmp_path / "cfg" / "palimpsest"
        config.mkdir(parents=True)
        config_root = selected if selection == "config" else tmp_path / "configured"
        (config / "config.toml").write_text(f'[storage]\nstate_root = "{config_root}"\n')
        if selection == "env":
            monkeypatch.setenv("PALIMPSEST_STATE_HOME", str(selected))
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err == ""
    records = [json.loads(line) for line in (selected / "logs" / "commands.jsonl").read_text().splitlines()]
    assert [(item["phase"], item.get("result")) for item in records] == [("start", None), ("end", "success")]
    assert not user_defaults.exists()


def test_invalid_state_config_never_falls_back_to_default_user_journal(user_defaults, tmp_path, capsys):
    config = tmp_path / "cfg" / "palimpsest"
    config.mkdir(parents=True)
    config_file = config / "config.toml"
    config_file.write_bytes(b"[storage\ninvalid config")
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert not user_defaults.exists()
    assert config_file.read_bytes() == b"[storage\ninvalid config"


@pytest.mark.parametrize("component", ["state", "logs"])
def test_default_user_journal_refuses_directory_symlinks(user_defaults, tmp_path, capsys, component):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = user_defaults if component == "state" else user_defaults / "logs"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert list(outside.iterdir()) == []
    assert link.is_symlink()


@pytest.mark.parametrize("component", ["state", "logs", "file"])
def test_default_user_journal_refuses_foreign_ownership(user_defaults, monkeypatch, capsys, component):
    logs = user_defaults / "logs"
    logs.mkdir(parents=True, mode=0o700)
    path = logs / "commands.jsonl"
    path.write_bytes(b"")
    path.chmod(0o600)
    foreign = {"state": user_defaults, "logs": logs, "file": path}[component].stat()
    real_fstat = os.fstat

    def foreign_owner(fd):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == (foreign.st_dev, foreign.st_ino):
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(host_journal.os, "fstat", foreign_owner)
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert path.read_bytes() == b""
    assert stat.S_IMODE(logs.stat().st_mode) == 0o700


@pytest.mark.parametrize("component", ["ancestor", "logs"])
def test_default_user_journal_never_repairs_unsafe_directory_modes(user_defaults, capsys, component):
    path = user_defaults if component == "ancestor" else user_defaults / "logs"
    mode = 0o770 if component == "ancestor" else 0o755
    path.mkdir(parents=True, mode=mode)
    path.chmod(mode)
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert not (user_defaults / "logs" / "commands.jsonl").exists()


def test_default_user_journal_preserves_corrupt_records(user_defaults, capsys):
    logs = user_defaults / "logs"
    logs.mkdir(parents=True, mode=0o700)
    path = logs / "commands.jsonl"
    damaged = b"not a journal record\n"
    path.write_bytes(damaged)
    path.chmod(0o600)
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert path.read_bytes() == damaged


def test_darwin_journal_remains_opt_in(user_defaults, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host_journal.sys, "platform", "darwin")
    host_journal.begin("store").finish("success")
    assert not user_defaults.exists() and capsys.readouterr().err == ""
    tmp_path.chmod(0o700)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path))
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err == ""
    records = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert [item["phase"] for item in records] == ["start", "end"]


def test_system_default_journal_requires_preprovisioning(user_defaults, tmp_path, monkeypatch, capsys):
    protected = tmp_path / "protected-system-journal"
    monkeypatch.setattr(state, "uses_system_defaults", lambda: True)
    # Put the protected system location in the test namespace, retaining its policy.
    monkeypatch.setattr(
        host_journal, "Path", lambda value: protected if value == "/var/log/palimpsest" else Path(value)
    )
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert not protected.exists() and not user_defaults.exists()
    protected.mkdir(mode=0o700)
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err == ""
    records = [json.loads(line) for line in (protected / "commands.jsonl").read_text().splitlines()]
    assert [item["phase"] for item in records] == ["start", "end"]


def test_unknown_account_journal_failure_warns_without_user_fallback(user_defaults, monkeypatch, capsys):
    def unknown_identity():
        raise state.StateError("cannot determine effective account")

    monkeypatch.setattr(state, "uses_system_defaults", unknown_identity)
    host_journal.begin("store").finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert not user_defaults.exists()


def test_default_journal_end_never_recreates_a_moved_state_root(user_defaults, capsys):
    journal = host_journal.begin("store")
    assert capsys.readouterr().err == ""
    moved = user_defaults.with_name("moved-state")
    before = (user_defaults / "logs" / "commands.jsonl").read_bytes()
    user_defaults.rename(moved)
    journal.finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    assert not user_defaults.exists()
    assert (moved / "logs" / "commands.jsonl").read_bytes() == before


def test_failure_warns_once_per_invocation_and_recovers(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(missing))
    first = host_journal.begin("ps")
    first.finish("success")
    assert capsys.readouterr().err.count("host journal is unavailable") == 1
    missing.mkdir(mode=0o700)
    second = host_journal.begin("ps")
    second.finish("success")
    assert capsys.readouterr().err == ""


def test_concurrent_successes_have_one_global_sequence_with_test_only_budget(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(host_journal, "_LOCK_SECONDS", 1.0)
    start = multiprocessing.Event()
    results = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_write, args=(str(tmp_path), start, results, ordinal, 1.0))
        for ordinal in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    observations = sorted(results.get(timeout=1) for _ in processes)
    assert observations == [(ordinal, "ok", "") for ordinal in range(4)]
    values = [json.loads(line)["sequence"] for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert values == [1, 2, 3, 4]


def test_file_creation_is_exclusive_and_only_eexist_opens_existing(monkeypatch):
    calls = []

    def opened(name, flags, mode=0o777, *, dir_fd=None):
        calls.append((flags, mode, dir_fd))
        if len(calls) == 1:
            raise FileExistsError
        return 19

    monkeypatch.setattr(host_journal.os, "open", opened)
    assert host_journal._open_file(7) == (19, False)
    assert calls[0][0] & host_journal.os.O_CREAT and calls[0][0] & host_journal.os.O_EXCL
    assert not calls[1][0] & host_journal.os.O_CREAT and not calls[1][0] & host_journal.os.O_EXCL
    assert calls[0][2] == calls[1][2] == 7


def test_file_creation_does_not_retry_enoent(monkeypatch):
    calls = 0

    def missing(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise FileNotFoundError

    monkeypatch.setattr(host_journal.os, "open", missing)
    with pytest.raises(FileNotFoundError):
        host_journal._open_file(7)
    assert calls == 1


def test_symlink_is_fail_open_with_warning(tmp_path, capsys):
    tmp_path.chmod(0o700)
    (tmp_path / "target").write_text("")
    (tmp_path / "commands.jsonl").symlink_to("target")
    journal = host_journal.CommandJournal("logs", tmp_path)
    journal._record("start")
    assert "host journal is unavailable" in capsys.readouterr().err


def test_invalid_override_and_incomplete_tail_warn_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", "relative")
    host_journal.begin("ps")
    assert "host journal is unavailable" in capsys.readouterr().err
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path))
    tmp_path.chmod(0o700)
    journal_file = tmp_path / "commands.jsonl"
    journal_file.write_bytes(b'{"schema":"palimpsest.host-command-journal.v1","sequence":1}')
    journal_file.chmod(0o600)
    before = journal_file.read_bytes()
    host_journal.begin("ps")
    assert journal_file.read_bytes() == before
    assert "host journal is unavailable" in capsys.readouterr().err


def test_warning_stream_failure_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path / "missing"))
    monkeypatch.setattr(
        host_journal, "print", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stderr")), raising=False
    )
    host_journal.begin("ps").finish("success")


def test_partial_append_is_warned_and_never_concatenated(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    real_write = host_journal.os.write
    monkeypatch.setattr(host_journal.os, "write", lambda fd, data: real_write(fd, data[:7]))
    host_journal.CommandJournal("ps", tmp_path, invocation_id="a" * 32)._record("start")
    partial = (tmp_path / "commands.jsonl").read_bytes()
    assert partial and not partial.endswith(b"\n")
    monkeypatch.setattr(host_journal.os, "write", real_write)
    host_journal.CommandJournal("ps", tmp_path, invocation_id="b" * 32)._record("start")
    assert (tmp_path / "commands.jsonl").read_bytes() == partial
    assert capsys.readouterr().err.count("host journal is unavailable") == 2


@pytest.mark.parametrize("sequence", [True, 2**64 - 1])
def test_invalid_previous_sequence_is_rejected(tmp_path, capsys, sequence):
    tmp_path.chmod(0o700)
    path = tmp_path / "commands.jsonl"
    path.write_text(json.dumps({"schema": host_journal._SCHEMA, "sequence": sequence}) + "\n")
    path.chmod(0o600)
    before = path.read_bytes()
    host_journal.CommandJournal("ps", tmp_path, invocation_id="c" * 32)._record("start")
    assert path.read_bytes() == before and "host journal is unavailable" in capsys.readouterr().err


def test_unsafe_mode_and_hardlink_are_rejected(tmp_path, capsys):
    tmp_path.chmod(0o700)
    path = tmp_path / "commands.jsonl"
    path.write_text("")
    path.chmod(0o644)
    host_journal.CommandJournal("ps", tmp_path, invocation_id="d" * 32)._record("start")
    assert path.read_bytes() == b""
    path.chmod(0o600)
    (tmp_path / "other").hardlink_to(path)
    host_journal.CommandJournal("ps", tmp_path, invocation_id="e" * 32)._record("start")
    assert path.read_bytes() == b"" and capsys.readouterr().err.count("host journal is unavailable") == 2


def test_actual_record_excludes_argument_and_environment_secrets(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path))
    monkeypatch.setenv("PALIMPSEST_TOKEN", "environment-secret")
    monkeypatch.setattr(cli, "dispatch_args", lambda args: 0)
    assert cli.main(["docker", "login", "argument-secret"]) == 0
    payload = (tmp_path / "commands.jsonl").read_bytes()
    assert b"environment-secret" not in payload and b"argument-secret" not in payload and b"login" not in payload


@pytest.mark.parametrize("fault", ["lock", "fsync", "size"])
def test_bounded_or_durability_failure_is_fail_open(tmp_path, monkeypatch, capsys, fault):
    tmp_path.chmod(0o700)
    if fault == "lock":
        monkeypatch.setattr(host_journal.fcntl, "flock", lambda *args: (_ for _ in ()).throw(BlockingIOError()))
        ticks = iter((0.0, 1.0))
        monkeypatch.setattr(host_journal.time, "monotonic", lambda: next(ticks, 1.0))
    elif fault == "fsync":
        monkeypatch.setattr(host_journal.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
    else:
        monkeypatch.setattr(host_journal, "_MAX_FILE", 0)
    journal = host_journal.CommandJournal("ps", tmp_path, invocation_id="f" * 32)
    journal._record("start")
    assert "host journal is unavailable" in capsys.readouterr().err


def test_cli_start_journal_failure_preserves_dispatch_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path / "missing"))
    monkeypatch.setattr(cli, "dispatch_args", lambda args: 7)
    assert cli.main(["ps"]) == 7
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count("host journal is unavailable") == 1


def test_cli_end_journal_failure_preserves_success_and_warns_again_next_invocation(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "dispatch_args", lambda args: 0)
    original = host_journal._append
    calls = 0

    def fail_end(root, phase, family, invocation_id, result):
        nonlocal calls
        calls += 1
        if phase == "end":
            raise OSError("fixed test failure")
        return original(root, phase, family, invocation_id, result)

    monkeypatch.setattr(host_journal, "_append", fail_end)
    assert cli.main(["ps"]) == 0
    assert cli.main(["ps"]) == 0
    assert capsys.readouterr().err.count("host journal is unavailable") == 2


def test_cli_journal_failure_does_not_replace_command_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PALIMPSEST_LOG_HOME", str(tmp_path / "missing"))

    def fail(args):
        raise PalimpsestError("original command error")

    monkeypatch.setattr(cli, "dispatch_args", fail)
    assert cli.main(["ps"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "original command error" in captured.err and captured.err.count("host journal is unavailable") == 1
