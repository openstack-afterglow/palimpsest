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

from palimpsest_local import console, state
from palimpsest_local.errors import RuntimeBusyError
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

    def start(name, *args, roots, expected_identity, _observation_budget=None):
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


def _external_console(tmp_path, monkeypatch, *, status="running", stale=False, backend="lima-vz"):
    from palimpsest_local import inventory, vm_access

    directory = tmp_path / "redis"
    directory.mkdir()
    run_id = "11111111-1111-4111-8111-111111111111"
    (tmp_path / "guest-environment").write_text(f"PALIMPSEST_RUN_ID={run_id}\n")
    row = {
        "id": f"host:{backend}:redis",
        "name": "redis",
        "run_id": run_id,
        "backend": backend,
        "status": status,
        "managed": False,
        "stale": stale,
    }
    row["access"] = vm_access.capabilities(row)
    monkeypatch.setattr(inventory, "get_vm", lambda roots, identifier, **_kwargs: row)
    instance = {
        "name": "redis",
        "dir": str(directory),
        "vmType": "krunkit" if backend == "lima-krunkit" else "vz",
        "status": "Running" if status == "running" else "Stopped",
        "config": {"env": {"PALIMPSEST_RUN_ID": run_id}},
    }
    monkeypatch.setattr(vm_access, "_instance", lambda name: dict(instance))
    return row, directory, instance


def _spawn_lima_console(tmp_path, argv, code, **kw):
    import shlex

    gate = argv[6].replace("/etc/environment", shlex.quote(str(tmp_path / "guest-environment")))
    return LocalProcessSession(("sh", "-c", gate, "sh", sys.executable, "-u", "-c", code), tty=kw["tty"], stdin=True)


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
@pytest.mark.parametrize("managed", [False, True])
def test_external_lima_console_disconnect_reaps_transport_without_adoption(tmp_path, monkeypatch, backend, managed):
    from palimpsest_local import vm_access

    row, directory, _ = _external_console(tmp_path, monkeypatch, backend=backend)
    if managed:
        from contextlib import nullcontext

        row["managed"] = True
        record = SimpleNamespace(
            name=row["name"],
            run_id=row["run_id"],
            dispatch_key=DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend(backend)),
        )
        monkeypatch.setattr(vm_access.runtime_dispatch, "resolve_existing_run", lambda *a, **kw: record)
        monkeypatch.setattr(vm_access.state, "locked_existing_run", lambda *a, **kw: nullcontext())
        monkeypatch.setattr(vm_access.state, "require_bound_run_dispatch_record", lambda *a: None)
    owned = []

    def spawn(argv, **kw):
        process = _spawn_lima_console(
            tmp_path, argv, "import time; print('guest shell ready', flush=True); time.sleep(60)", **kw
        )
        owned.append(process)
        return process

    monkeypatch.setattr(vm_access, "spawn_process_session", spawn)
    if not managed:
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


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
@pytest.mark.parametrize(
    "change",
    [
        "stopped",
        "stale",
        "expected",
        "live-marker",
        "guest-marker",
        "physical",
        "vm-type",
        "spawn-vm-type",
        "ready-vm-type",
    ],
)
def test_external_console_denies_stopped_stale_and_identity_replacement(tmp_path, monkeypatch, change, backend):
    from palimpsest_local import vm_access

    row, directory, instance = _external_console(
        tmp_path,
        monkeypatch,
        status="stopped" if change == "stopped" else "running",
        stale=change == "stale",
        backend=backend,
    )
    replacement_type = "vz" if backend == "lima-krunkit" else "krunkit"
    if change == "vm-type":
        instance["vmType"] = replacement_type
    ready_observed = []
    go_writes = []
    if change == "ready-vm-type":
        original_events = LocalProcessSession.events
        original_write = LocalProcessSession.write_stdin

        def replace_on_ready(session):
            output = b""
            for event in original_events(session):
                if isinstance(event, ProcessOutputEvent) and event.stream in {ProcessStream.STDOUT, ProcessStream.PTY}:
                    output += event.data
                    start = output.find(b"\x1ePALIMPSEST_READY:")
                    if start >= 0 and b"\x1f" in output[start:]:
                        ready_observed.append(True)
                        instance["vmType"] = replacement_type
                yield event

        def record_go(session, data):
            go_writes.append(data)
            return original_write(session, data)

        monkeypatch.setattr(LocalProcessSession, "events", replace_on_ready)
        monkeypatch.setattr(LocalProcessSession, "write_stdin", record_go)
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
        elif change == "spawn-vm-type":
            instance["vmType"] = replacement_type
        else:
            assert change in {"guest-marker", "ready-vm-type"}
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
        if change == "ready-vm-type":
            assert ready_observed
            assert go_writes == []
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


@pytest.mark.parametrize(
    "instance",
    [
        {"vmType": "qemu"},
        {},
        {"vmType": "krunkit", "config": {"vmType": "vz"}},
    ],
)
def test_lima_access_refuses_unknown_or_contradictory_native_type(monkeypatch, instance):
    import json

    from palimpsest_local import vm_access

    monkeypatch.setattr(vm_access, "spawn_process_session", lambda *a, **kw: object())
    monkeypatch.setattr(vm_access, "_collect", lambda *a, **kw: json.dumps({"name": "redis", **instance}).encode())
    with pytest.raises(vm_access.VMAccessError) as refused:
        vm_access._instance("redis")
    assert refused.value.category == "identity"


@pytest.mark.parametrize("backend", ["lima-vz", "lima-krunkit"])
def test_lima_stopped_console_and_running_journal_use_same_backend_fence(tmp_path, monkeypatch, backend):
    from palimpsest_local import vm_access

    row, directory, instance = _external_console(tmp_path, monkeypatch, backend=backend, status="stopped")
    (directory / "serial.log").write_text("old line\nlatest line\n")
    access = vm_access.Access.resolve(None, row["id"], expected_run_id=row["run_id"])
    assert access.logs(1) == {"log": "latest line", "source": "lima-serial-console", "truncated": True}
    row["status"] = "running"
    instance["status"] = "Running"
    commands = []

    def spawn(argv, **kwargs):
        commands.append(tuple(argv[8:]))
        return _spawn_lima_console(tmp_path, argv, "print('journal sample')", **kwargs)

    monkeypatch.setattr(vm_access, "spawn_process_session", spawn)
    assert access.logs(1)["log"].strip() == "journal sample"
    assert commands == [("sudo", "-n", "journalctl", "--no-pager", "--quiet", "-n", "1", "-o", "short-iso")]
    instance["vmType"] = "vz" if backend == "lima-krunkit" else "krunkit"
    with pytest.raises(vm_access.VMAccessError) as refused:
        access.probe(("true",))
    assert refused.value.category == "identity"
    assert len(commands) == 1


@pytest.mark.parametrize("runtime_kind", ["oci-root", "unknown"])
@pytest.mark.parametrize("status", ["running", "stopped"])
def test_observed_native_rows_deny_all_generic_detail_access(tmp_path, monkeypatch, runtime_kind, status):
    from palimpsest_local import inventory, vm_access

    row, _, _ = _external_console(tmp_path, monkeypatch, status=status)
    row["runtime_kind"] = runtime_kind
    row["access"] = vm_access.capabilities(row)

    def forbidden(*args, **kwargs):
        pytest.fail("observed native identity opened a generic transport")

    monkeypatch.setattr(vm_access, "spawn_process_session", forbidden)
    measurements = inventory.vm_metrics(None, row["id"], expected_run_id=row["run_id"])
    logs = inventory.vm_logs(None, row["id"], expected_run_id=row["run_id"])
    assert measurements["status"] == logs["status"] == "unavailable"
    assert measurements["reason"] == logs["reason"] == "identity"
    current = console.ConsoleManager()
    try:
        with pytest.raises(console.ConsoleError):
            current.open(None, row["id"], "exec", ["true"], expected_run_id=row["run_id"])
    finally:
        current.close_all()


@pytest.mark.parametrize("native_identity", ["bound", "ambiguous"])
@pytest.mark.parametrize("operation", ["shell", "exec", "metrics", "logs"])
def test_live_native_identity_rejects_stale_cloud_access_selection(tmp_path, monkeypatch, native_identity, operation):
    from palimpsest_local import inventory, vm_access

    row, directory, instance = _external_console(
        tmp_path, monkeypatch, status="stopped" if operation == "logs" else "running"
    )
    name = "plm-" + ("111111111111" if native_identity == "bound" else "222222222222")
    row.update(name=name, id="host:lima-vz:" + name, runtime_kind="cloud-image")
    instance["name"] = name
    row["access"] = vm_access.capabilities(row)
    (directory / "serial.log").write_text("native control console must stay private\n")

    def forbidden(*args, **kwargs):
        pytest.fail("stale cloud classification opened native generic access")

    monkeypatch.setattr(vm_access, "spawn_process_session", forbidden)
    if operation == "metrics":
        result = inventory.vm_metrics(None, row["id"], expected_run_id=row["run_id"])
        assert result["status"] == "unavailable" and result["reason"] == "identity"
    elif operation == "logs":
        result = inventory.vm_logs(None, row["id"], expected_run_id=row["run_id"])
        assert result["status"] == "unavailable" and result["reason"] == "identity"
        assert result["log"] == ""
    else:
        current = console.ConsoleManager()
        try:
            with pytest.raises(console.ConsoleError):
                current.open(
                    None,
                    row["id"],
                    operation,
                    ["true"] if operation == "exec" else [],
                    expected_run_id=row["run_id"],
                )
        finally:
            current.close_all()


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
def test_native_exited_main_public_metrics_and_console_exec_use_owned_dispatch(tmp_path, monkeypatch):
    from test_inventory import _LINUX_METRICS, _native_exited_vm

    from palimpsest_local import inventory, vm_access
    from palimpsest_local.vm_exec_session import ExecOwners

    roots, name, run_id, observed = _native_exited_vm(tmp_path, monkeypatch)
    before = (roots.runs / name / "state.json").read_bytes()
    completed = []
    exec_owners = ExecOwners()

    def submit(selected, argv, *, roots, expected_identity, timeout_ms, _observation_budget=None):
        assert selected == name
        assert expected_identity == ExpectedRunIdentity(
            name, run_id, DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.LIMA_VZ)
        )
        program = f"import os; os.write(1, {_LINUX_METRICS!r})" if timeout_ms is not None else "print(19 + 5 + 7)"
        source = LocalProcessSession((sys.executable, "-u", "-c", program), tty=False, stdin=False)
        completed.append(source)
        return source

    def forbidden(*args, **kwargs):
        pytest.fail("native canonical access borrowed generic Lima or host credentials")

    monkeypatch.setattr(vm_access.runtime_dispatch, "exec", submit)
    monkeypatch.setattr(vm_access, "spawn_process_session", forbidden)
    monkeypatch.setattr(vm_access, "_instance", forbidden)
    metrics = inventory.vm_metrics(roots, name, expected_run_id=run_id, exec_owners=exec_owners)
    assert metrics["status"] == "available"
    current = console.ConsoleManager(exec_owners=exec_owners)
    try:
        opened = current.open(roots, name, "exec", ["printf", "proof"], expected_run_id=run_id)
        events, _ = drain(current, opened["id"])
        assert bytes_from(events) == b"31\n"
        assert events[-1]["exit_code"] == 0

        # A new detail/console request observes provider stop despite retained main exit.
        monkeypatch.setattr(vm_access.runtime_dispatch, "exec", forbidden)
        observed["status"] = "stopped"
        stopped = inventory.vm_metrics(roots, name, expected_run_id=run_id, exec_owners=exec_owners)
        assert stopped["status"] == "unavailable" and stopped["reason"] == "stopped"
        with pytest.raises(console.ConsoleError):
            current.open(roots, name, "exec", ["true"], expected_run_id=run_id)
        assert all(source._process.poll() is not None for source in completed)
        assert (roots.runs / name / "state.json").read_bytes() == before
    finally:
        current.close_all()
        exec_owners.stop_admitting()
        assert exec_owners.wait() == 0


def test_kvm_access_keeps_required_unknown_mailbox_ack_accounting(monkeypatch):
    from palimpsest_local import vm_access
    from palimpsest_local.runtime_types import ExistingRunRecord
    from palimpsest_local.vm_exec_session import ExecOwners

    record = ExistingRunRecord(
        "mailbox-run",
        "11111111-1111-4111-8111-111111111111",
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )
    owners = ExecOwners()
    source = LocalProcessSession((sys.executable, "-B", "-c", "print(19+5+7)"), tty=False, stdin=False)
    monkeypatch.setattr(vm_access.runtime_dispatch, "exec", lambda *args, **kwargs: source)
    access = vm_access.Access(
        None,
        {"name": record.name, "backend": "kvm", "runtime_kind": "oci-root", "status": "running", "managed": True},
        record,
        owners,
    )
    try:
        assert vm_access._collect(access.open("exec", ("probe",))) == b"31\n"
        owners.stop_admitting()
        assert owners.wait() == 1
    finally:
        source.close()


@pytest.mark.parametrize("mode", ["shell", "exec"])
@pytest.mark.parametrize("failure", ["run-lock", "namespace-lock"])
def test_busy_console_admission_is_sanitized_and_does_not_consume_client_id(manager, monkeypatch, mode, failure):
    def busy(*_args, **_kwargs):
        if failure == "run-lock":
            raise state.RunLockTimeoutError(123456)
        raise RuntimeBusyError("private namespace /secret/lock holder 123456")

    monkeypatch.setattr(console.vm_access.runtime_dispatch, mode, busy)
    session_id = "11223344556677889900aabbccddeeff"
    argv = ["guest-command"] if mode == "exec" else ()
    with pytest.raises(console.ConsoleError) as caught:
        manager.open(None, "vm", mode, argv, session_id=session_id)
    assert caught.value.category == "busy"
    assert "123456" not in str(caught.value)
    assert "/secret" not in str(caught.value)
    assert not manager.has_active()
    adapter(monkeypatch, "import os; os.write(1, b'recovered-console\\n')")
    opened = manager.open(None, "vm", mode, argv, session_id=session_id)
    events, _ = drain(manager, opened["id"])
    assert bytes_from(events) == b"recovered-console\n"
    assert events[-1]["kind"] == "exit" and events[-1]["exit_code"] == 0


@pytest.mark.parametrize("operation", ["metrics", "shell", "exec"])
def test_real_native_namespace_admission_busy_never_grants_guest_or_console(tmp_path, monkeypatch, operation):
    import fcntl

    from test_inventory import _native_custody_fixture, _native_exited_vm

    from palimpsest_local import inventory
    from palimpsest_local.runtime_types import CapabilityCheck

    roots, name, run_id, _observed = _native_exited_vm(tmp_path, monkeypatch)
    native, config, custody = _native_custody_fixture(tmp_path, monkeypatch)
    ledger = roots.runs / name / "state.json"
    before = ledger.read_bytes()
    clock = [0.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(
        native.platforms,
        "_check_capability",
        lambda requirement, **_kwargs: CapabilityCheck(requirement.capability_id, "fixture-host", True),
    )
    monkeypatch.setattr(native, "host_config_for_run", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(native, "_config_for", lambda _authority: config)

    def forbidden(*_args, **_kwargs):
        pytest.fail("busy namespace admitted provider or guest work")

    monkeypatch.setattr(native, "_load", forbidden)
    monkeypatch.setattr(native, "_control", forbidden)
    monkeypatch.setattr(native, "NativeProcessSession", forbidden)
    monkeypatch.setattr(console.vm_access, "spawn_process_session", forbidden)
    holder = os.open(custody / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    current = console.ConsoleManager()
    try:
        if operation == "metrics":
            result = inventory.vm_metrics(roots, name, expected_run_id=run_id)
            assert result["status"] == "unavailable" and result["reason"] == "busy"
            assert result["cpu"] is None and result["memory"] is None and result["measured_at"] is None
            assert str(tmp_path) not in repr(result)
        else:
            with pytest.raises(console.ConsoleError) as caught:
                current.open(roots, name, operation, ["true"] if operation == "exec" else (), expected_run_id=run_id)
            assert caught.value.category == "busy" and str(tmp_path) not in str(caught.value)
            assert not current.has_active()
        assert clock[0] == pytest.approx(5)
        assert ledger.read_bytes() == before
        assert not (custody / "active.json").exists()
        assert not list(custody.glob("*.actor.json")) and not list(custody.glob("session-*.json"))
    finally:
        current.close_all()
        os.close(holder)
