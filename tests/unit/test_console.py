"""Console protocol tests; dispatch authority is isolated, transport is real.

No VM or host runtime service is required. Test-owned child argv never comes
from the console request: monkeypatched adapters choose it independently.
"""

import base64
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from palimpsest_local import console
from palimpsest_local.process_session import LocalProcessSession
from palimpsest_local.runtime_types import (
    DispatchKey,
    ExpectedRunIdentity,
    ProcessCapabilities,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessStatusEvent,
    ProcessStream,
    RuntimeBackend,
    RuntimeKind,
)


@pytest.fixture
def manager(monkeypatch):
    current = console.ConsoleManager()
    record = SimpleNamespace(
        name="vm",
        run_id="11111111-1111-4111-8111-111111111111",
        dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
    )
    monkeypatch.setattr(
        console.vm_access.Access,
        "resolve",
        classmethod(
            lambda cls, roots, name, **kw: cls(
                roots,
                {
                    "id": "vm",
                    "name": "vm",
                    "run_id": record.run_id,
                    "backend": "kvm",
                    "status": "running",
                    "managed": True,
                },
                record,
            )
        ),
    )
    yield current
    current.close_all()


def adapter(monkeypatch, source, *, tty=False, stdin=False):
    owned = []

    def start(name, *args, roots, expected_identity):
        assert name == "vm"
        assert expected_identity == ExpectedRunIdentity(
            "vm",
            "11111111-1111-4111-8111-111111111111",
            DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
        )
        session = LocalProcessSession((sys.executable, "-u", "-c", source), tty=tty, stdin=stdin)
        owned.append(session)
        return session

    monkeypatch.setattr(console.vm_access.runtime_dispatch, "shell", start)
    monkeypatch.setattr(console.vm_access.runtime_dispatch, "exec", start)
    return owned


def drain(manager, session_id, cursor=0):
    events = []
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = manager.read(session_id, cursor, wait_seconds=0.2)
        events.extend(response["events"])
        cursor = response["cursor"]
        if response["closed"]:
            return events, cursor
    pytest.fail("console did not finish")


def bytes_from(events, stream=None):
    return b"".join(
        base64.b64decode(event["data"], validate=True)
        for event in events
        if event["kind"] == "output" and (stream is None or event["stream"] == stream)
    )


def test_real_binary_output_exit_and_replay(manager, monkeypatch):
    data = bytes(range(256)) + b"\xf0\x9f\x92\xa9\xff\x00"
    adapter(monkeypatch, f"import os; os.write(1, {data!r}); os.write(2, b'err'); raise SystemExit(7)")
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    events, cursor = drain(manager, opened["id"])
    assert bytes_from(events, "stdout") == data
    assert bytes_from(events, "stderr") == b"err"
    assert [event["sequence"] for event in events] == list(range(1, cursor + 1))
    assert events[-1] == {
        "sequence": cursor,
        "kind": "exit",
        "returncode": 7,
        "exit_code": 7,
        "signal_number": None,
        "category": "exited",
    }
    assert manager.read(opened["id"], 0, 0)["events"] == events
    assert manager.read(opened["id"], cursor, 0) == {"id": opened["id"], "events": [], "cursor": cursor, "closed": True}
    assert not manager.has_active()


def test_exact_arbitrary_event_boundaries_and_one_consumer(manager, monkeypatch):
    # A finite typed event producer tests boundaries that OS pipe reads may merge.
    class Source:
        capabilities = ProcessCapabilities(False, False, False, False)
        consumed = 0
        closed = False

        def events(self):
            self.consumed += 1
            assert self.consumed == 1
            for stream, chunk in [
                (ProcessStream.STDOUT, b"\xf0"),
                (ProcessStream.STDERR, b"\xff\0"),
                (ProcessStream.STDOUT, b"\x9f\x92"),
                (ProcessStream.STDOUT, b"\xa9"),
            ]:
                yield ProcessOutputEvent(stream, chunk)
            yield ProcessStatusEvent(ProcessExit(0, 0, None, ProcessExitCategory.EXITED))

        def close(self):
            self.closed = True

    source = Source()
    monkeypatch.setattr(console.vm_access.runtime_dispatch, "exec", lambda *args, **kw: source)
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    events, _ = drain(manager, opened["id"])
    assert [base64.b64decode(e["data"]) for e in events[:-1]] == [b"\xf0", b"\xff\0", b"\x9f\x92", b"\xa9"]
    assert bytes_from(events, "stdout") == b"\xf0\x9f\x92\xa9"
    manager.read(opened["id"], 0, 0)
    assert source.consumed == 1 and source.closed


def test_real_pty_echo_resize_and_status(manager, monkeypatch):
    source = (
        "import os, fcntl, termios, struct\n"
        "print('READY', flush=True)\n"
        "line = input()\n"
        "rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\\0'*8))\n"
        "print('SIZE:%d:%d' % (rows, cols), flush=True)\n"
        "print('INPUT:' + line, flush=True)\n"
        "raise SystemExit(9)\n"
    )
    adapter(monkeypatch, source, tty=True, stdin=True)
    opened = manager.open(None, "vm")
    assert opened["capabilities"] == {"stdin": True, "tty": True, "resize": True, "signal": True}
    seen = []
    cursor = 0
    deadline = time.monotonic() + 8
    while b"READY" not in bytes_from(seen):
        assert time.monotonic() < deadline
        response = manager.read(opened["id"], cursor, 0.2)
        seen.extend(response["events"])
        cursor = response["cursor"]
    manager.resize(opened["id"], 33, 101)
    manager.write(opened["id"], b"hello-pty\n")
    events, _ = drain(manager, opened["id"], cursor)
    output = bytes_from(seen + events)
    assert b"hello-pty\r\n" in output  # actual terminal line discipline echo
    assert b"SIZE:33:101\r\n" in output
    assert b"INPUT:hello-pty\r\n" in output
    assert all(e["stream"] == "pty" for e in seen + events if e["kind"] == "output")
    assert events[-1]["exit_code"] == 9


def test_lagging_and_future_cursors_fail_explicitly(manager, monkeypatch):
    manager.MAX_BYTES = 8
    adapter(monkeypatch, "import os; os.write(1, b'0123456789abcdef')")
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    current = manager._sessions[opened["id"]]
    current.thread.join(8)
    assert not current.thread.is_alive()
    with pytest.raises(console.ConsoleError) as gap:
        manager.read(opened["id"], 0, 0)
    assert gap.value.category == "cursor-gap"
    final = manager.read(opened["id"], current.cursor - 1, 0)
    assert final["events"][-1]["kind"] == "exit"
    with pytest.raises(console.ConsoleError) as future:
        manager.read(opened["id"], current.cursor + 1, 0)
    assert future.value.category == "invalid"


def test_close_all_reaps_owned_process_and_wakes_reader(manager, monkeypatch):
    owned = adapter(monkeypatch, "import time; time.sleep(60)", stdin=True)
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    session_id = opened["id"]
    pid = owned[0]._process.pid
    result = []
    reader = threading.Thread(target=lambda: result.append(manager.read(session_id, 0, 2)))
    reader.start()
    manager.close_all()
    reader.join(3)
    assert not reader.is_alive() and result
    assert owned[0]._process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    events, cursor = drain(manager, session_id, result[0]["cursor"])
    all_events = result[0]["events"] + events
    exit_event = next(e for e in all_events if e["kind"] == "exit")
    assert exit_event["returncode"] == owned[0]._process.returncode
    assert exit_event["category"] == "cancelled"
    assert manager.read(session_id, cursor, 0)["closed"]
    with pytest.raises(console.ConsoleError, match="closed"):
        manager.write(session_id, b"x")
    with pytest.raises(console.ConsoleError, match="closed"):
        manager.open(None, "vm")


def test_validation_limits_and_safe_failure(manager, monkeypatch):
    calls = []
    monkeypatch.setattr(console.vm_access.runtime_dispatch, "shell", lambda *args, **kw: calls.append(args))
    for mode, argv in [("exec", []), ("exec", ["a\0"]), ("exec", "sh"), ("shell", ["host-command"]), ([], ())]:
        with pytest.raises(console.ConsoleError):
            manager.open(None, "vm", mode, argv)
    assert not calls
    for session_id in ["../vm", "A" * 32, "0" * 31]:
        with pytest.raises(console.ConsoleError):
            manager.read(session_id)
    for after, wait in [(True, 0), (-1, 0), (0, float("nan")), (0, 3)]:
        with pytest.raises(console.ConsoleError):
            manager.read("0" * 32, after, wait)
    adapter(monkeypatch, "import time; time.sleep(60)")
    manager.MAX_SESSIONS = 1
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    with pytest.raises(console.ConsoleError) as limit:
        manager.open(None, "vm")
    assert limit.value.category == "limit"
    with pytest.raises(console.ConsoleError) as capability:
        manager.write(opened["id"], b"x")
    assert capability.value.category == "capability"
    with pytest.raises(console.ConsoleError):
        manager.write(opened["id"], b"x" * (manager.MAX_INPUT_BYTES + 1))
    for rows, columns in [(True, 80), (0, 80), (24, 65536)]:
        with pytest.raises(console.ConsoleError) as invalid:
            manager.resize(opened["id"], rows, columns)
        assert invalid.value.category == "invalid"
    manager.close(opened["id"])


def test_dispatch_errors_do_not_expose_host_details(manager, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("secret /host/path password=hidden")

    monkeypatch.setattr(console.vm_access.runtime_dispatch, "shell", fail)
    with pytest.raises(console.ConsoleError) as error:
        manager.open(None, "vm")
    assert str(error.value) == "Cannot open runtime console"
    assert not manager.has_active()


def test_idle_disconnect_stops_owned_transport(manager, monkeypatch):
    manager.IDLE_SECONDS = 0
    owned = adapter(monkeypatch, "import time; time.sleep(60)", stdin=True)
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    deadline = time.monotonic() + 8
    while opened["id"] in manager._sessions and time.monotonic() < deadline:
        time.sleep(0.02)
    assert opened["id"] not in manager._sessions
    assert owned[0]._process.poll() is not None
    with pytest.raises(console.ConsoleError) as missing:
        manager.read(opened["id"], 0, 0)
    assert missing.value.category == "missing"


def test_context_bound_authority_released_after_completion(manager, monkeypatch):
    from contextlib import contextmanager

    from palimpsest_local.process_session import bind_process_session_context

    released = threading.Event()

    @contextmanager
    def authority():
        try:
            yield
        finally:
            released.set()

    context = authority()
    context.__enter__()
    session = LocalProcessSession((sys.executable, "-c", "print('owned')"), tty=False, stdin=False)
    bound = bind_process_session_context(session, context)
    monkeypatch.setattr(console.vm_access.runtime_dispatch, "exec", lambda *args, **kw: bound)
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    events, _ = drain(manager, opened["id"])
    assert bytes_from(events) == b"owned\n"
    assert released.is_set()


def test_control_busy_and_reader_caps_fail_without_queueing(manager, monkeypatch):
    adapter(monkeypatch, "import time; time.sleep(60)", stdin=True)
    opened = manager.open(None, "vm", "exec", ["guest-command"])
    current = manager._sessions[opened["id"]]
    current.input_lock.acquire()
    try:
        with pytest.raises(console.ConsoleError) as busy:
            manager.write(opened["id"], b"x")
        assert busy.value.category == "limit"
    finally:
        current.input_lock.release()
    with current.condition:
        current.readers = manager.MAX_READERS
    try:
        with pytest.raises(console.ConsoleError) as busy:
            manager.read(opened["id"], 0, 0)
        assert busy.value.category == "limit"
    finally:
        with current.condition:
            current.readers = 0


def test_open_with_client_session_id_and_pre_cancellation(manager, monkeypatch):
    adapter(monkeypatch, "print('hi')")
    explicit_id = "11112222333344445555666677778888"
    opened = manager.open(None, "vm", "exec", ["cmd"], session_id=explicit_id)
    assert opened["id"] == explicit_id

    with pytest.raises(console.ConsoleError, match="already used"):
        manager.open(None, "vm", "exec", ["cmd"], session_id=explicit_id)

    # Pre-cancellation: close arrives before open finishes (e.g. rapid close or beforeunload)
    cancelled_id = "aaaabbbbccccddddeeeeffff00001111"
    manager.close(cancelled_id)
    with pytest.raises(console.ConsoleError, match="cancelled"):
        manager.open(None, "vm", "exec", ["cmd"], session_id=cancelled_id)


def _external_console(tmp_path, monkeypatch, *, status="running", stale=False):
    from palimpsest_local import inventory, vm_access

    directory = tmp_path / "redis"
    directory.mkdir()
    run_id = "11111111-1111-4111-8111-111111111111"
    (tmp_path / "guest-environment").write_text(f"PALIMPSEST_RUN_ID={run_id}\n")
    row = {
        "id": "host:lima-vz:redis",
        "name": "redis",
        "run_id": run_id,
        "backend": "lima-vz",
        "status": status,
        "managed": False,
        "stale": stale,
    }
    row["access"] = vm_access.capabilities(row)
    monkeypatch.setattr(inventory, "get_vm", lambda roots, identifier: row)
    instance = {
        "name": "redis",
        "dir": str(directory),
        "vmType": "vz",
        "status": "Running" if status == "running" else "Stopped",
        "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
    }
    monkeypatch.setattr(vm_access, "_instance", lambda name: dict(instance))
    return row, directory, instance


def _spawn_lima_console(tmp_path, argv, code, **kw):
    import shlex

    gate = argv[6].replace("/etc/environment", shlex.quote(str(tmp_path / "guest-environment")))
    return LocalProcessSession(("sh", "-c", gate, "sh", sys.executable, "-u", "-c", code), tty=kw["tty"], stdin=True)


def test_external_lima_console_disconnect_reaps_transport_without_adoption(tmp_path, monkeypatch):
    from palimpsest_local import vm_access

    row, directory, _ = _external_console(tmp_path, monkeypatch)
    owned = []

    def spawn(argv, **kw):
        process = _spawn_lima_console(
            tmp_path, argv, "import time; print('guest shell ready', flush=True); time.sleep(60)", **kw
        )
        owned.append(process)
        return process

    monkeypatch.setattr(vm_access, "spawn_process_session", spawn)
    monkeypatch.setattr(
        vm_access.runtime_dispatch,
        "resolve_existing_run",
        lambda *a, **kw: pytest.fail("external shell must not resolve/adopt a ledger"),
    )
    current = console.ConsoleManager()
    try:
        opened = current.open(None, row["id"], expected_run_id=row["run_id"])
        assert opened["capabilities"]["tty"] is True
        result = current.read(opened["id"], wait_seconds=2)
        assert b"guest shell ready" in bytes_from(result["events"])
        current.resize(opened["id"], 31, 100)
        current.close(opened["id"])
        assert owned[0]._process.poll() is not None
        assert directory.is_dir()
        assert not (tmp_path / "runs").exists()
    finally:
        current.close_all()


@pytest.mark.parametrize("change", ["stopped", "stale", "expected", "live-marker", "guest-marker", "physical"])
def test_external_console_denies_stopped_stale_and_identity_replacement(tmp_path, monkeypatch, change):
    from palimpsest_local import vm_access

    row, directory, instance = _external_console(
        tmp_path, monkeypatch, status="stopped" if change == "stopped" else "running", stale=change == "stale"
    )
    expected = row["run_id"]
    if change == "expected":
        expected = "22222222-2222-4222-8222-222222222222"
    if change == "live-marker":
        instance["config"]["env"]["PALIMPSEST_RUN_ID"] = "22222222-2222-4222-8222-222222222222"
    if change == "guest-marker":
        (tmp_path / "guest-environment").write_text("PALIMPSEST_RUN_ID=22222222-2222-4222-8222-222222222222\n")
    owned = []

    def spawn(argv, **kw):
        if change == "physical":
            directory.rename(tmp_path / "replaced")
            directory.mkdir()
        else:
            assert change == "guest-marker"
        process = _spawn_lima_console(
            tmp_path, argv, f"from pathlib import Path; Path({str(tmp_path / 'command-ran')!r}).touch()", **kw
        )
        owned.append(process)
        return process

    monkeypatch.setattr(vm_access, "spawn_process_session", spawn)
    current = console.ConsoleManager()
    try:
        with pytest.raises(console.ConsoleError) as failure:
            current.open(None, row["id"], expected_run_id=expected)
        assert failure.value.category == ("stopped" if change == "stopped" else "identity")
        assert not current.has_active()
        assert all(process._process.poll() is not None for process in owned)
        assert not (tmp_path / "command-ran").exists()
    finally:
        current.close_all()


def test_external_console_requires_observed_marker(tmp_path, monkeypatch):
    row, _, _ = _external_console(tmp_path, monkeypatch)
    current = console.ConsoleManager()
    try:
        with pytest.raises(console.ConsoleError, match="requires the observed run identity"):
            current.open(None, row["id"])
    finally:
        current.close_all()
