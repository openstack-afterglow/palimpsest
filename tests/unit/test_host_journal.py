import json
import multiprocessing
from pathlib import Path

import pytest

from palimpsest_local import cli, host_journal
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
