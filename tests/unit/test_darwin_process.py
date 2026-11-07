from __future__ import annotations

import copy
import ctypes
import errno
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import darwin_process, vz_native_runtime
from palimpsest_local.errors import StateError


@pytest.mark.parametrize("pid", [False, True, 0, -1, 2**31, "1"])
def test_invalid_pid_never_becomes_process_absence(pid) -> None:
    with pytest.raises(StateError):
        darwin_process.read_process(pid)


@pytest.mark.parametrize("result,error", [(0, errno.EPERM), (0, 0), (1, 0)])
def test_inaccessible_or_partial_kernel_metadata_is_not_absence(monkeypatch, result, error) -> None:
    def query(*_args):
        ctypes.set_errno(error)
        return result

    monkeypatch.setattr(darwin_process, "_libraries", lambda: (SimpleNamespace(proc_pidinfo=query), None, 4096))
    with pytest.raises(StateError):
        darwin_process.read_process(41)


def test_conclusive_kernel_esrch_is_process_absence(monkeypatch) -> None:
    def query(*_args):
        ctypes.set_errno(errno.ESRCH)
        return 0

    monkeypatch.setattr(darwin_process, "_libraries", lambda: (SimpleNamespace(proc_pidinfo=query), None, 4096))
    assert darwin_process.read_process(41) is None


def test_birth_change_between_complete_reads_is_not_stable_identity(monkeypatch) -> None:
    first = darwin_process.DarwinProcessIdentity(41, 1, os.geteuid(), 41, 1000, "/owned/program", ("program",))
    values = iter((first, replace(first, start_time_ns=2000)))
    monkeypatch.setattr(darwin_process, "_read_once", lambda _pid: next(values))
    with pytest.raises(StateError):
        darwin_process.read_process(41)


def test_identity_repr_never_discloses_process_arguments() -> None:
    identity = darwin_process.DarwinProcessIdentity(
        41, 1, os.geteuid(), 41, 1000, "/owned/program", ("program", "PRIVATE_ARGUMENT")
    )
    assert "PRIVATE_ARGUMENT" not in repr(identity)
    assert identity.argv == ("program", "PRIVATE_ARGUMENT")


def test_inventory_retains_unknown_executable_but_exact_actor_read_rejects_it(monkeypatch) -> None:
    pid = os.getpid()
    identity = darwin_process.DarwinProcessIdentity(
        pid, 1, os.geteuid(), pid, 1000, None, ("detached-helper", "plm-owned-instance")
    )

    def enumerate_pids(_kind, _uid, buffer, _size):
        if buffer is not None:
            buffer[0] = pid
        return ctypes.sizeof(ctypes.c_int)

    monkeypatch.setattr(
        darwin_process, "_libraries", lambda: (SimpleNamespace(proc_listpids=enumerate_pids), None, 4096)
    )
    monkeypatch.setattr(darwin_process, "_read_once", lambda _pid: identity)
    assert darwin_process.list_processes() == (identity,)
    assert identity.argv[-1] == "plm-owned-instance"
    with pytest.raises(StateError):
        darwin_process.read_process(pid)


@pytest.mark.parametrize("error", [errno.EPERM, errno.EACCES, errno.EIO, 0])
def test_unavailable_vnode_path_errors_are_not_unknown_identity(monkeypatch, error) -> None:
    def query(*_args):
        ctypes.set_errno(error)
        return 0

    monkeypatch.setattr(darwin_process, "_libraries", lambda: (SimpleNamespace(proc_pidpath=query), None, 4096))
    with pytest.raises(StateError):
        darwin_process._path(41)


def test_enoent_vnode_path_is_explicit_unknown_not_a_fallback_name(monkeypatch) -> None:
    def query(*_args):
        ctypes.set_errno(errno.ENOENT)
        return 0

    monkeypatch.setattr(darwin_process, "_libraries", lambda: (SimpleNamespace(proc_pidpath=query), None, 4096))
    assert darwin_process._path(41) is None


def test_unknown_executable_still_requires_stable_full_arguments(monkeypatch) -> None:
    first = darwin_process.DarwinProcessIdentity(41, 1, os.geteuid(), 41, 1000, None, ("helper",))
    values = iter((first, replace(first, argv=("helper", "plm-owned-instance"))))
    monkeypatch.setattr(darwin_process, "_read_once", lambda _pid: next(values))
    with pytest.raises(StateError):
        darwin_process._read_identity(41)


def test_unknown_executable_never_suppresses_unreadable_arguments(monkeypatch) -> None:
    info = SimpleNamespace(pid=41, ppid=1, uid=os.geteuid(), pgid=41, start_sec=1, start_usec=0)
    monkeypatch.setattr(darwin_process, "_info", lambda _pid: info)
    monkeypatch.setattr(darwin_process, "_path", lambda _pid: None)

    def unreadable(_pid):
        raise StateError("argv unavailable")

    monkeypatch.setattr(darwin_process, "_argv", unreadable)
    with pytest.raises(StateError):
        darwin_process._read_once(41)


def _custody_actor():
    return darwin_process.DarwinProcessIdentity(
        41, 1, os.geteuid(), 41, 1000, "/owned/limactl", ("/owned/limactl", "hostagent", "plm-case")
    )


@pytest.mark.parametrize("absent", [False, True])
@pytest.mark.parametrize("relation", ["descendant", "group", "instance", "directory"])
def test_unknown_executable_retains_every_related_native_custody_fence(absent, relation) -> None:
    expected = _custody_actor()
    unknown = replace(expected, pid=42, pgid=42, executable=None, argv=("helper",))
    if relation == "descendant":
        unknown = replace(unknown, ppid=expected.pid)
    elif relation == "group":
        unknown = replace(unknown, pgid=expected.pgid)
    elif relation == "instance":
        unknown = replace(unknown, argv=("helper", "plm-case"))
    else:
        unknown = replace(unknown, argv=("helper", "--socket=/private/plm-case/ha.sock"))
    processes = (*(() if absent else (expected,)), unknown)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        vz_native_runtime._require_failed_start_processes(
            expected, None if absent else expected, processes, "plm-case", "/private/plm-case", absent=absent
        )


def test_unknown_original_executable_is_not_an_exact_native_actor() -> None:
    expected = _custody_actor()
    unknown = replace(expected, executable=None)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        vz_native_runtime._require_failed_start_processes(
            expected, unknown, (unknown,), "plm-case", "/private/plm-case", absent=False
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the real Darwin process ABI")
def test_real_process_birth_and_natural_exit_are_distinct() -> None:
    code = 'import sys; print("ready", flush=True); sys.stdin.buffer.read(1)'
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert child.stdout.readline() == b"ready\n"
        identity = darwin_process.read_process(child.pid)
        assert identity is not None
        assert (identity.pid, identity.ppid, identity.uid) == (child.pid, os.getpid(), os.geteuid())
        assert 0 < identity.start_time_ns <= time.time_ns()
        assert identity.argv == (sys.executable, "-c", code)
        assert darwin_process.read_process(child.pid) == identity
        child.communicate(b"x", timeout=10)
        assert child.returncode == 0
        assert darwin_process.read_process(child.pid) is None
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=10)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def sigterm_case(monkeypatch):
    actor = _custody_actor()
    attempt = {"id": "9f334a73-94d3-449c-ab58-4f8b80df1f97"}
    case = SimpleNamespace(
        actor=actor,
        current=actor,
        data={
            "vz_native": {
                "start_attempt": attempt,
                "failed_start_cleanup": {
                    "start_attempt": copy.deepcopy(attempt),
                    "actor": vz_native_runtime._failed_start_actor_wire(actor),
                    "stop": "attempted",
                },
            }
        },
        writes=[],
        sent=[],
        write_calls=0,
        fail_write_at=None,
        fail_absence=False,
        provider_status="Stopped",
    )

    def observe(*_args, absent, **_kwargs):
        if absent and case.fail_absence:
            raise vz_native_runtime.VZNativeHold("endpoint remains")
        return {"actor_absent": absent}

    def write(_authority, _status, data):
        case.write_calls += 1
        if case.write_calls == case.fail_write_at:
            raise StateError("uncertain durable write")
        case.writes.append(copy.deepcopy(data))

    def send(pid, number):
        case.sent.append((pid, number))
        case.current = None

    monkeypatch.setattr(vz_native_runtime, "_failed_start_observation", observe)
    monkeypatch.setattr(vz_native_runtime, "_load_checked", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(vz_native_runtime, "_write", write)
    monkeypatch.setattr(vz_native_runtime, "read_process", lambda _pid: case.current)
    monkeypatch.setattr(
        vz_native_runtime, "_owned_instance", lambda *_args, **_kwargs: {"status": case.provider_status}
    )
    monkeypatch.setattr(vz_native_runtime.os, "kill", send)

    def run(*, allowed=True, requested=signal.SIGTERM):
        with vz_native_runtime._failed_start_cleanup_scope(
            run_id="f04f1c21-4892-4c02-943d-72b28778763b",
            deadline_unix=time.time() + 60,
            actor=actor,
            network_digest="sha256:" + "a" * 64,
            network_stamp=(0,) * 8,
            instance_names=("plm-case",),
            operation=vz_native_runtime.RuntimeOperation.STOP,
            signal_request=requested if allowed else None,
        ):
            vz_native_runtime._failed_start_signal(
                SimpleNamespace(verify_binding=lambda: None), None, None, None, case.data
            )

    case.run = run
    return case


@pytest.mark.parametrize("stop_phase", ["attempted", "returned"])
def test_single_sigterm_preserves_failed_stop_and_cannot_be_replayed(sigterm_case, stop_phase):
    case = sigterm_case
    case.data["vz_native"]["failed_start_cleanup"]["stop"] = stop_phase
    case.run()
    recovery = case.writes[-1]["vz_native"]["failed_start_cleanup"]
    assert recovery["stop"] == stop_phase
    assert recovery["termination"]["phase"] == "settled"
    assert case.sent == [(case.actor.pid, signal.SIGTERM)]
    assert vz_native_runtime._failed_start_settled(recovery)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert case.sent == [(case.actor.pid, signal.SIGTERM)]


def test_normal_stop_scope_never_grants_sigterm(sigterm_case):
    case = sigterm_case
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run(allowed=False)
    assert case.sent == []
    assert case.writes == []


@pytest.mark.parametrize("change", ["no-stop", "already-stopped", "prior-signal", "delete", "wrong-actor"])
def test_sigterm_rejects_unbound_or_consumed_shutdown_authority(sigterm_case, change):
    case = sigterm_case
    recovery = case.data["vz_native"]["failed_start_cleanup"]
    if change == "no-stop":
        recovery.pop("stop")
    elif change == "already-stopped":
        recovery["stop"] = "settled"
    elif change == "prior-signal":
        recovery["termination"] = {"phase": "attempted"}
    elif change == "delete":
        recovery["delete"] = "attempted"
    else:
        recovery["actor"]["start_time_ns"] += 1
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert case.sent == []
    assert case.writes == []


def test_sigterm_rechecks_original_identity_after_durable_intent(sigterm_case):
    case = sigterm_case
    case.current = replace(case.actor, start_time_ns=case.actor.start_time_ns + 1)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert case.sent == []
    assert case.writes[-1]["vz_native"]["failed_start_cleanup"]["termination"]["phase"] == "attempted"


@pytest.mark.parametrize("write_number", [1, 2, 3])
def test_uncertain_termination_write_never_replays_a_signal(sigterm_case, write_number):
    case = sigterm_case
    case.fail_write_at = write_number
    with pytest.raises(StateError):
        case.run()
    assert case.sent == ([] if write_number == 1 else [(case.actor.pid, signal.SIGTERM)])
    if case.writes:
        case.data = copy.deepcopy(case.writes[-1])
        with pytest.raises(vz_native_runtime.VZNativeHold):
            case.run()
        assert case.sent == [(case.actor.pid, signal.SIGTERM)]
        assert not vz_native_runtime._failed_start_settled(case.data["vz_native"]["failed_start_cleanup"])


@pytest.mark.parametrize("failure", ["endpoint", "provider"])
def test_signal_delivery_does_not_prove_shutdown_settlement(sigterm_case, failure):
    case = sigterm_case
    if failure == "endpoint":
        case.fail_absence = True
    else:
        case.provider_status = "Running"
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    recovery = case.writes[-1]["vz_native"]["failed_start_cleanup"]
    assert case.sent == [(case.actor.pid, signal.SIGTERM)]
    assert recovery["stop"] == "attempted"
    assert recovery["termination"]["phase"] == "sent"
    assert not vz_native_runtime._failed_start_settled(recovery)


def test_sigterm_signal_error_retains_durable_attempt_and_forbids_replay(sigterm_case, monkeypatch):
    case = sigterm_case
    attempts = []

    def failed_send(pid, number):
        attempts.append((pid, number))
        raise OSError(errno.ESRCH, "process disappeared")

    monkeypatch.setattr(vz_native_runtime.os, "kill", failed_send)
    with pytest.raises(OSError):
        case.run()
    case.data = copy.deepcopy(case.writes[-1])
    assert case.data["vz_native"]["failed_start_cleanup"]["termination"]["phase"] == "attempted"
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert attempts == [(case.actor.pid, signal.SIGTERM)]


@pytest.mark.parametrize("expire_at", ["identity-read", "final-authority"])
def test_sigterm_phase_deadline_cannot_be_extended_by_slow_observation(sigterm_case, monkeypatch, expire_at):
    case = sigterm_case
    clock = [time.time()]
    monkeypatch.setattr(vz_native_runtime.time, "time", lambda: clock[0])
    loads = [0]

    def load(*_args, **_kwargs):
        loads[0] += 1
        if expire_at == "final-authority" and loads[0] == 2:
            clock[0] += 120

    def read(_pid):
        if expire_at == "identity-read":
            clock[0] += 120
        return case.current

    monkeypatch.setattr(vz_native_runtime, "_load_checked", load)
    monkeypatch.setattr(vz_native_runtime, "read_process", read)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert case.sent == ([] if expire_at == "identity-read" else [(case.actor.pid, signal.SIGTERM)])
    recovery = case.writes[-1]["vz_native"]["failed_start_cleanup"]
    assert not vz_native_runtime._failed_start_settled(recovery)


@pytest.mark.parametrize("target", ["other-run", "no-longer-held"])
def test_exact_cleanup_scope_never_falls_through_to_ordinary_stop(sigterm_case, monkeypatch, target):
    case = sigterm_case
    run_id = "f04f1c21-4892-4c02-943d-72b28778763b"
    authority = SimpleNamespace(record=SimpleNamespace(run_id=run_id))
    if target == "other-run":
        authority.record.run_id = "3592451c-60ca-48a2-b7da-c87dcdac354d"
        case.data["vz_native"]["hold"] = "launch-incomplete"

    @contextmanager
    def existing(*_args, **_kwargs):
        yield authority

    def custody(*_args, **_kwargs):
        pytest.fail("cleanup scope must reject this target before provider custody")

    monkeypatch.setattr(vz_native_runtime, "locked_existing_run", existing)
    monkeypatch.setattr(vz_native_runtime, "_config_for", lambda _authority: None)
    monkeypatch.setattr(vz_native_runtime, "_data", lambda _authority: case.data)
    monkeypatch.setattr(vz_native_runtime, "_custody", custody)
    with vz_native_runtime._failed_start_cleanup_scope(
        run_id=run_id,
        deadline_unix=time.time() + 60,
        actor=case.actor,
        network_digest="sha256:" + "a" * 64,
        network_stamp=(0,) * 8,
        instance_names=("plm-case",),
        operation=vz_native_runtime.RuntimeOperation.STOP,
        signal_request=signal.SIGTERM,
    ):
        with pytest.raises(vz_native_runtime.VZNativeHold):
            vz_native_runtime.stop_oci_run(None, "case")
    assert case.sent == []
    assert case.writes == []


@pytest.fixture
def force_case(sigterm_case, monkeypatch):
    case = sigterm_case
    case.data["vz_native"]["failed_start_cleanup"]["termination"] = {"signal": "SIGTERM", "phase": "sent"}
    monkeypatch.setattr(vz_native_runtime, "_failed_start_endpoint_pins", lambda *_args: {})
    monkeypatch.setattr(vz_native_runtime, "_reconcile_failed_start_stale_endpoints", lambda *_args: None)
    return case


def test_forced_signal_has_its_own_fence_and_preserves_spent_sigterm(force_case):
    case = force_case
    case.run(requested=signal.SIGKILL)
    recovery = case.writes[-1]["vz_native"]["failed_start_cleanup"]
    assert recovery["stop"] == "attempted"
    assert recovery["termination"] == {"signal": "SIGTERM", "phase": "sent"}
    assert recovery["forced_termination"]["signal"] == "SIGKILL"
    assert recovery["forced_termination"]["phase"] == "settled"
    assert case.sent == [(case.actor.pid, signal.SIGKILL)]
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run(requested=signal.SIGKILL)
    assert case.sent == [(case.actor.pid, signal.SIGKILL)]


@pytest.mark.parametrize("phase", [None, "attempted", "settled"])
def test_forced_signal_requires_prior_unsettled_sigterm_delivery(force_case, phase):
    case = force_case
    recovery = case.data["vz_native"]["failed_start_cleanup"]
    if phase is None:
        recovery.pop("termination")
    else:
        recovery["termination"]["phase"] = phase
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run(requested=signal.SIGKILL)
    assert case.sent == []
    assert case.writes == []


@pytest.mark.parametrize("requested", [True, 9, signal.SIGINT])
def test_signal_scope_never_accepts_ambiguous_or_unapproved_signal(sigterm_case, requested):
    case = sigterm_case
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run(requested=requested)
    assert case.sent == []
    assert case.writes == []


def test_forced_delivery_cannot_settle_when_stale_cleanup_fails(force_case, monkeypatch):
    case = force_case

    def blocked(*_args):
        raise vz_native_runtime.VZNativeHold("stale endpoint changed")

    monkeypatch.setattr(vz_native_runtime, "_reconcile_failed_start_stale_endpoints", blocked)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run(requested=signal.SIGKILL)
    recovery = case.writes[-1]["vz_native"]["failed_start_cleanup"]
    assert recovery["forced_termination"]["phase"] == "sent"
    assert not vz_native_runtime._failed_start_settled(recovery)
    assert case.sent == [(case.actor.pid, signal.SIGKILL)]


@pytest.fixture
def stale_endpoints(monkeypatch):
    from palimpsest_local import state

    with tempfile.TemporaryDirectory(prefix="plmf-", dir=str(Path("/tmp").resolve())) as temporary:
        home = Path(temporary)
        directory = home / "case"
        directory.mkdir(mode=0o700)
        actor = replace(
            _custody_actor(),
            argv=("/owned/limactl", "hostagent", "case", str(directory / "ha.pid"), str(directory / "ha.sock")),
        )
        guard = SimpleNamespace(actor=actor, signal_request=signal.SIGKILL)
        for name in ("ha.pid", "vz.pid"):
            (directory / name).write_text(str(actor.pid) + "\n")
            (directory / name).chmod(0o600)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.bind(str(directory / "ha.sock"))
            (directory / "ha.sock").chmod(0o700)
        (directory / "keep.txt").write_text("preserve")
        pins = {
            name: vz_native_runtime._failed_start_endpoint_pin(
                directory, name, (directory / name).lstat(), guard, inactive=False
            )
            for name in ("ha.pid", "vz.pid", "ha.sock")
        }
        info = directory.stat()
        forced = {"signal": "SIGKILL", "phase": "sent", "before_endpoints": pins}
        data = {
            "vz_native": {
                "instance": "case",
                "instance_identity": [info.st_dev, info.st_ino],
                "failed_start_cleanup": {"forced_termination": forced},
            }
        }
        case = SimpleNamespace(
            directory=directory,
            home=home,
            actor=actor,
            pins=pins,
            data=data,
            tamper=None,
            record=home / "record.json",
            config=SimpleNamespace(home=home),
        )

        def observe(*_args, **_kwargs):
            current = {}
            for path in directory.iterdir():
                if path.name == "keep.txt":
                    continue
                current[path.name] = vz_native_runtime._failed_start_endpoint_pin(
                    directory, path.name, path.lstat(), guard, inactive=True, original=pins.get(path.name)
                )
            return {"stale_endpoints": current}

        def write(_authority, _status, value):
            state.atomic_write_json(case.record, value)
            if case.tamper is not None:
                tamper, case.tamper = case.tamper, None
                tamper()

        case.source_observation = vz_native_runtime._failed_start_observation
        case.fixture_observation = observe
        monkeypatch.setattr(vz_native_runtime, "_failed_start_observation", observe)
        monkeypatch.setattr(vz_native_runtime, "_load_checked", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(vz_native_runtime, "_write", write)
        monkeypatch.setattr(vz_native_runtime, "read_process", lambda _pid: None)
        monkeypatch.setattr(vz_native_runtime, "list_processes", lambda: ())

        def run():
            with vz_native_runtime._failed_start_cleanup_scope(
                run_id="f04f1c21-4892-4c02-943d-72b28778763b",
                deadline_unix=time.time() + 60,
                actor=actor,
                network_digest="sha256:" + "a" * 64,
                network_stamp=(0,) * 8,
                instance_names=("case",),
                operation=vz_native_runtime.RuntimeOperation.STOP,
                signal_request=signal.SIGKILL,
            ):
                vz_native_runtime._reconcile_failed_start_stale_endpoints(case.authority, None, case.config, None, data)

        case.run = run
        case.authority = SimpleNamespace(
            verify_binding=lambda: None,
            record=SimpleNamespace(run_id="f04f1c21-4892-4c02-943d-72b28778763b", name="case"),
        )
        yield case


def test_exact_stale_metadata_cleanup_preserves_nonendpoint_files_and_durable_history(stale_endpoints):
    from palimpsest_local import state

    case = stale_endpoints
    case.run()
    assert {path.name for path in case.directory.iterdir()} == {"keep.txt"}
    assert (case.directory / "keep.txt").read_text() == "preserve"
    cleanup = state.read_json(case.record)["vz_native"]["failed_start_cleanup"]["forced_termination"]["stale_cleanup"]
    assert cleanup["phase"] == "settled"
    assert set(cleanup["removed"]) == {"ha.pid", "vz.pid", "ha.sock"}
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()


@pytest.mark.parametrize("tamper", ["directory", "pid-file"])
def test_stale_metadata_replacement_after_intent_never_unlinks_new_authority(stale_endpoints, tamper):
    case = stale_endpoints
    if tamper == "directory":
        old = case.home / "old"

        def replace_directory():
            case.directory.rename(old)
            case.directory.mkdir(mode=0o700)
            (case.directory / "keep.txt").write_text("new owner")

        case.tamper = replace_directory
    else:

        def replace_pid():
            path = case.directory / "ha.pid"
            path.unlink()
            path.write_text(str(case.actor.pid) + "\n")
            path.chmod(0o600)

        case.tamper = replace_pid
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    if tamper == "directory":
        assert (case.directory / "keep.txt").read_text() == "new owner"
        assert {p.name for p in old.iterdir()} == {"ha.pid", "vz.pid", "ha.sock", "keep.txt"}
    else:
        assert {p.name for p in case.directory.iterdir()} == {"ha.pid", "vz.pid", "ha.sock", "keep.txt"}


def test_stale_socket_must_be_inactive_and_original(stale_endpoints):
    case = stale_endpoints
    socket_path = case.directory / "ha.sock"
    socket_path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        socket_path.chmod(0o700)
        server.listen(1)
        with pytest.raises(vz_native_runtime.VZNativeHold):
            case.run()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert socket_path.exists()
    assert (case.directory / "ha.pid").exists()


def _install_complete_observation_fixture(case, monkeypatch):
    case.config.tools = {"limactl": Path(case.actor.executable)}
    case.data["vz_native"]["root_volume_id"] = "bdb3c42e-0410-4a6e-987a-bb293235993a"
    upper = case.home / "upper"
    upper.mkdir(mode=0o700)
    (upper / "datadisk").write_bytes(b"fixture")
    root = SimpleNamespace(record=SimpleNamespace(storage=SimpleNamespace(directory=upper)))
    case.after_instance = None
    case.after_root = None
    case.provider = {"status": "Stopped", "hostAgentPID": 0, "driverPID": 0, "config": {"networks": []}}

    def instance(*_args, **_kwargs):
        info = case.directory.stat()
        assert [info.st_dev, info.st_ino] == case.data["vz_native"]["instance_identity"]
        if case.after_instance is not None:
            hook, case.after_instance = case.after_instance, None
            hook()
        return case.provider

    def root_volume(*_args, **_kwargs):
        if case.after_root is not None:
            hook, case.after_root = case.after_root, None
            hook()
        return root

    monkeypatch.setattr(vz_native_runtime, "_failed_start_cleanup_hold", lambda *_args: None)
    monkeypatch.setattr(vz_native_runtime, "_failed_start_network", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(vz_native_runtime, "_owned_instance", instance)
    monkeypatch.setattr(vz_native_runtime, "load_vz_root_volume", root_volume)
    monkeypatch.setattr(vz_native_runtime, "_require_ssh_port_free", lambda *_args: None)
    monkeypatch.setattr(vz_native_runtime, "_failed_start_observation", case.source_observation)


def test_observation_never_accepts_empty_replacement_instance_directory(stale_endpoints, monkeypatch):
    case = stale_endpoints
    _install_complete_observation_fixture(case, monkeypatch)
    old = case.home / "old"

    def exchange():
        case.directory.rename(old)
        case.directory.mkdir(mode=0o700)

    case.after_instance = exchange
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert {p.name for p in old.iterdir()} == {"ha.pid", "vz.pid", "ha.sock", "keep.txt"}
    assert list(case.directory.iterdir()) == []


@pytest.mark.parametrize("relation", ["group", "directory"])
def test_related_actor_introduced_during_root_inspection_blocks_all_unlinks(stale_endpoints, monkeypatch, relation):
    case = stale_endpoints
    _install_complete_observation_fixture(case, monkeypatch)
    actors = []
    unknown = replace(case.actor, pid=42, ppid=1, pgid=42, executable=None, argv=("detached",))
    if relation == "group":
        unknown = replace(unknown, pgid=case.actor.pgid)
    else:
        unknown = replace(unknown, argv=("detached", str(case.directory / "ha.sock")))
    case.after_root = lambda: actors.append(unknown)
    monkeypatch.setattr(vz_native_runtime, "list_processes", lambda: tuple(actors))
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert {p.name for p in case.directory.iterdir()} == {"ha.pid", "vz.pid", "ha.sock", "keep.txt"}


def test_visible_path_must_still_match_retained_unlink_descriptor(stale_endpoints, monkeypatch):
    case = stale_endpoints
    old = case.home / "old"

    def inventory():
        case.directory.rename(old)
        case.directory.mkdir(mode=0o700)
        return ()

    monkeypatch.setattr(vz_native_runtime, "list_processes", inventory)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert {p.name for p in old.iterdir()} == {"ha.pid", "vz.pid", "ha.sock", "keep.txt"}
    assert list(case.directory.iterdir()) == []


@pytest.mark.parametrize("slow_read", ["process", "leaf"])
def test_slow_final_unlink_checks_cannot_outlive_action_deadline(stale_endpoints, monkeypatch, slow_read):
    case = stale_endpoints
    clock = [time.time()]
    monkeypatch.setattr(vz_native_runtime.time, "time", lambda: clock[0])
    if slow_read == "process":

        def process(_pid):
            clock[0] += 120
            return None

        monkeypatch.setattr(vz_native_runtime, "read_process", process)
    else:
        original_stat = os.stat

        def leaf(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                clock[0] += 120
            return result

        monkeypatch.setattr(vz_native_runtime.os, "stat", leaf)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.run()
    assert {p.name for p in case.directory.iterdir()} == {"ha.pid", "vz.pid", "ha.sock", "keep.txt"}


@pytest.fixture
def normal_default_endpoints(stale_endpoints, monkeypatch):
    from palimpsest_local import state

    case = stale_endpoints
    case.source_network = vz_native_runtime._failed_start_network
    case.source_port_check = vz_native_runtime._require_ssh_port_free
    _install_complete_observation_fixture(case, monkeypatch)
    for name in ("ha.pid", "vz.pid", "ha.sock"):
        (case.directory / name).unlink()
    started = time.time() - 60
    for name in ("default_ep.sock", "default_fd.sock"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.bind(str(case.directory / name))
            (case.directory / name).chmod(0o755)
    attempt = {"id": "0caa89a3-c4b7-419d-a7b5-b3629f578932", "started_unix": started, "log": "original"}
    native = case.data["vz_native"]
    native.update(
        start_attempt=attempt,
        hold="launch-incomplete",
        failed_start_cleanup={
            "start_attempt": copy.deepcopy(attempt),
            "actor": vz_native_runtime._failed_start_actor_wire(case.actor),
            "stop": "returned",
            "before_stop": {
                "phase": "before-stop",
                "observed_unix": time.time(),
                "original_pid": case.actor.pid,
                "actor_absent": False,
            },
        },
    )
    case.writes = []
    case.actions = []

    def write(_authority, status, data):
        state.atomic_write_json(case.record, data)
        case.writes.append((status, copy.deepcopy(data)))

    def forbidden(*args, **kwargs):
        case.actions.append((args, kwargs))
        raise AssertionError("normal settlement must not issue a mutation")

    monkeypatch.setattr(vz_native_runtime, "_write", write)
    monkeypatch.setattr(vz_native_runtime, "_lima", forbidden)
    monkeypatch.setattr(vz_native_runtime.os, "kill", forbidden)

    @contextmanager
    def scope(operation=vz_native_runtime.RuntimeOperation.STOP):
        with vz_native_runtime._failed_start_cleanup_scope(
            run_id=case.authority.record.run_id,
            deadline_unix=time.time() + 60,
            actor=case.actor,
            network_digest="sha256:" + "a" * 64,
            network_stamp=(0,) * 8,
            instance_names=("case",),
            operation=operation,
        ):
            yield

    def settle(operation=vz_native_runtime.RuntimeOperation.STOP):
        with scope(operation):
            vz_native_runtime._failed_start_stop(case.authority, None, case.config, None, case.data)

    case.scope = scope
    case.settle = settle
    yield case


def test_returned_normal_stop_settles_current_inactive_pair_without_replaying(normal_default_endpoints, monkeypatch):
    from palimpsest_local import state

    case = normal_default_endpoints
    original = {path.name: vz_native_runtime._stamp(path.lstat()) for path in case.directory.iterdir()}

    source_unlink = os.unlink

    def forbid_unlink(path, *args, **kwargs):
        assert Path(path).name not in {"default_ep.sock", "default_fd.sock"}
        return source_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(vz_native_runtime.os, "unlink", forbid_unlink)
        case.settle()
        case.data = state.read_json(case.record)
        case.settle(vz_native_runtime.RuntimeOperation.RM)
    recovery = case.data["vz_native"]["failed_start_cleanup"]
    assert recovery["stop"] == "settled"
    assert recovery["after_stop"]["default_network_endpoint_basis"] == "current-after-normal-stop"
    assert set(recovery["after_stop"]["default_network_endpoints"]) == {"default_ep.sock", "default_fd.sock"}
    assert {path.name: vz_native_runtime._stamp(path.lstat()) for path in case.directory.iterdir()} == original
    assert case.actions == []
    assert len(case.writes) == 2


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "related",
        "running",
        "provider-pid",
        "shared",
        "nat",
        "attempted",
        "termination",
        "forced",
        "missing-pins",
    ],
)
def test_normal_default_pair_requires_complete_original_quiescence(normal_default_endpoints, monkeypatch, change):
    case = normal_default_endpoints
    recovery = case.data["vz_native"]["failed_start_cleanup"]
    if change == "actor":
        monkeypatch.setattr(vz_native_runtime, "read_process", lambda _pid: case.actor)
    elif change == "related":
        actor = replace(case.actor, pid=42, ppid=1)
        monkeypatch.setattr(vz_native_runtime, "list_processes", lambda: (actor,))
    elif change == "running":
        case.provider["status"] = "Running"
    elif change == "provider-pid":
        case.provider["driverPID"] = case.actor.pid
    elif change in {"shared", "nat"}:
        case.provider["config"]["networks"] = [{"lima": "shared"}] if change == "shared" else [{"vzNAT": True}]
    elif change == "attempted":
        recovery["stop"] = "attempted"
    elif change in {"termination", "forced"}:
        recovery["termination" if change == "termination" else "forced_termination"] = {"phase": "sent"}
    else:
        recovery["stop"] = "settled"
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.settle()
    assert case.actions == []
    assert case.writes == []
    assert (case.directory / "default_ep.sock").exists()


@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM])
def test_active_default_socket_never_becomes_normal_stop_custody(normal_default_endpoints, kind):
    case = normal_default_endpoints
    path = case.directory / "default_ep.sock"
    path.unlink()
    with socket.socket(socket.AF_UNIX, kind) as server:
        server.bind(str(path))
        path.chmod(0o755)
        if kind == socket.SOCK_STREAM:
            server.listen(1)
        case.data["vz_native"]["failed_start_cleanup"]["before_stop"]["observed_unix"] = time.time()
        with pytest.raises(vz_native_runtime.VZNativeHold):
            case.settle()
    assert case.writes == []
    assert case.actions == []


@pytest.mark.parametrize("change", ["regular", "symlink", "mode", "links", "unknown", "post-stop", "too-old"])
def test_normal_default_snapshot_rejects_unowned_or_unqualified_leaves(normal_default_endpoints, change):
    case = normal_default_endpoints
    path = case.directory / "default_ep.sock"
    if change in {"regular", "symlink"}:
        path.unlink()
        if change == "regular":
            path.write_bytes(b"not a socket")
        else:
            path.symlink_to(case.directory / "default_fd.sock")
    elif change == "mode":
        path.chmod(0o777)
    elif change == "links":
        os.link(path, case.directory / "extra-link")
    elif change == "unknown":
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.bind(str(case.directory / "other.sock"))
    elif change == "post-stop":
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 2_000_000_000))
    else:
        os.utime(path, ns=(time.time_ns(), int((time.time() - 120) * 1_000_000_000)))
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.settle()
    assert case.actions == []
    assert case.writes == []


def test_persisted_default_pin_replacement_and_reappearance_hold(normal_default_endpoints):
    case = normal_default_endpoints
    case.settle()
    path = case.directory / "default_ep.sock"
    path.rename(case.directory / "saved-endpoint")
    # Absence is stronger evidence and is persisted, so even the original inode cannot return.
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.settle()
    (case.directory / "saved-endpoint").rename(case.home / "saved-endpoint")
    case.settle()
    assert (
        "default_ep.sock"
        not in case.data["vz_native"]["failed_start_cleanup"]["after_stop"]["default_network_endpoints"]
    )
    (case.home / "saved-endpoint").rename(path)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.settle()
    assert case.actions == []


def test_default_endpoint_replacement_during_root_observation_is_not_adopted(normal_default_endpoints):
    case = normal_default_endpoints
    path = case.directory / "default_fd.sock"

    def exchange():
        path.rename(case.home / "original-fd")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.bind(str(path))
            path.chmod(0o755)

    case.after_root = exchange
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.settle()
    assert case.writes == []
    assert (case.home / "original-fd").exists()


@pytest.mark.parametrize("failure", ["error", "deadline", "directory"])
def test_default_socket_probe_requires_stable_bounded_evidence(normal_default_endpoints, monkeypatch, failure):
    case = normal_default_endpoints
    original_socket = socket.socket
    clock = [time.time()]
    monkeypatch.setattr(vz_native_runtime.time, "time", lambda: clock[0])

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            if failure == "error":
                raise OSError(errno.EACCES, "denied")
            if failure == "deadline":
                clock[0] += 120
            else:
                case.directory.rename(case.home / "old-directory")
                case.directory.mkdir(mode=0o700)
            raise OSError(errno.ECONNREFUSED, "closed")

    def socket_factory(family, kind, *args, **kwargs):
        if family == socket.AF_UNIX:
            return Probe()
        return original_socket(family, kind, *args, **kwargs)

    monkeypatch.setattr(vz_native_runtime.socket, "socket", socket_factory)
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.settle()
    assert case.writes == []
    assert case.actions == []


@pytest.mark.parametrize("provider_disappears", [True, False])
def test_normal_settlement_then_one_shot_rm_releases_only_after_provider_absence(
    normal_default_endpoints, monkeypatch, provider_disappears
):
    case = normal_default_endpoints
    case.settle()
    released = []
    deleted = case.home / "provider-deleted"
    case.data["vz_native"]["plan"] = {"root_graph_digest": "sha256:" + "d" * 64}
    case.authority.snapshot = SimpleNamespace(state={"status": "failed"})
    store = SimpleNamespace(release_lease_set=lambda _lease: released.append("lease"))

    @contextmanager
    def locked(*_args, **_kwargs):
        yield case.authority

    @contextmanager
    def custody(*_args, **_kwargs):
        yield None

    def delete(_config, argv, _custody, **_kwargs):
        assert argv == ["delete", "--tty=false", "case"]
        case.actions.append(tuple(argv))
        assert case.data["vz_native"]["failed_start_cleanup"]["delete"] == "attempted"
        assert not released
        if provider_disappears:
            case.directory.rename(deleted)  # Provider owns ordinary deletion, not endpoint reconciliation.

    def release(*_args, **_kwargs):
        assert not case.directory.exists()
        released.append("resource")

    monkeypatch.setattr(vz_native_runtime, "locked_existing_run", locked)
    monkeypatch.setattr(vz_native_runtime, "_custody", custody)
    monkeypatch.setattr(vz_native_runtime, "_config_for", lambda _authority: case.config)
    monkeypatch.setattr(vz_native_runtime, "_data", lambda _authority: case.data)
    monkeypatch.setattr(
        vz_native_runtime, "_load_checked", lambda *_args, **_kwargs: (case.data, store, None, None, None, None)
    )
    monkeypatch.setattr(vz_native_runtime, "_lima", delete)
    monkeypatch.setattr(
        vz_native_runtime,
        "_instances",
        lambda *_args, **_kwargs: {} if not case.directory.exists() else {"case": case.provider},
    )
    monkeypatch.setattr(vz_native_runtime, "release_vz_lower_export", release)
    monkeypatch.setattr(vz_native_runtime, "release_vz_root_volume", release)
    monkeypatch.setattr(vz_native_runtime, "_lifecycle_result", lambda *_args: None)
    monkeypatch.setattr(vz_native_runtime, "_hold", lambda *_args: None)
    with case.scope(vz_native_runtime.RuntimeOperation.RM):
        if provider_disappears:
            vz_native_runtime.rm_oci_run(None, "case")
        else:
            with pytest.raises(vz_native_runtime.VZNativeHold):
                vz_native_runtime.rm_oci_run(None, "case")
        with pytest.raises(vz_native_runtime.VZNativeHold):
            vz_native_runtime._failed_start_stop(case.authority, None, case.config, None, case.data)
    assert case.actions == [("delete", "--tty=false", "case")]
    assert released == (["resource", "lease", "resource"] if provider_disappears else [])


@pytest.mark.parametrize("change", ["digest", "stamp", "named-network", "namespace"])
def test_default_endpoint_settlement_keeps_original_network_pins(normal_default_endpoints, monkeypatch, change):
    case = normal_default_endpoints
    config_dir = case.home / "_config"
    config_dir.mkdir(mode=0o700)
    network = config_dir / "networks.yaml"
    network.write_text(
        'paths:\n  socketVMNet: "/opt/socket_vmnet/bin/socket_vmnet"\n'
        "  varRun: /private/var/run/lima\n  sudoers: /private/etc/sudoers.d/lima\n"
        "group: admin\nnetworks:\n  user-v2:\n    mode: user-v2\n"
        "    gateway: 192.168.104.1\n    netmask: 255.255.255.0\n"
        "  shared:\n    mode: shared\n    gateway: 192.168.105.1\n"
        "    dhcpEnd: 192.168.105.254\n    netmask: 255.255.255.0\n"
        "  bridged:\n    mode: bridged\n    interface: en0\n"
        "  host:\n    mode: host\n    gateway: 192.168.106.1\n"
        "    dhcpEnd: 192.168.106.254\n    netmask: 255.255.255.0\n"
    )
    network.chmod(0o600)
    _, digest, stamp = vz_native_runtime._file(network, owned=True)
    if change == "digest":
        network.write_text(network.read_text().replace("group: admin", "group: other"))
    elif change == "stamp":
        os.utime(network, ns=(time.time_ns(), time.time_ns() + 1_000_000_000))
    elif change == "named-network":
        case.provider["config"]["networks"] = [{"lima": "user-v2"}]
    else:
        (case.home / "_networks").mkdir(mode=0o700)
    case.provider.update(name="case", vmType="vz", arch="aarch64")
    monkeypatch.setattr(vz_native_runtime, "_instances", lambda *_args, **_kwargs: {"case": case.provider})
    exists = vz_native_runtime._failed_start_path_exists
    monkeypatch.setattr(
        vz_native_runtime,
        "_failed_start_path_exists",
        lambda path: exists(path) if path.is_relative_to(case.home) else False,
    )
    monkeypatch.setattr(vz_native_runtime, "_failed_start_network", case.source_network)
    with vz_native_runtime._failed_start_cleanup_scope(
        run_id=case.authority.record.run_id,
        deadline_unix=time.time() + 60,
        actor=case.actor,
        network_digest=digest,
        network_stamp=stamp,
        instance_names=("case",),
        operation=vz_native_runtime.RuntimeOperation.STOP,
    ):
        with pytest.raises(vz_native_runtime.VZNativeHold):
            vz_native_runtime._failed_start_stop(case.authority, None, case.config, None, case.data)
    assert case.writes == []
    assert case.actions == []


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("upper", vz_native_runtime.VZNativeHold),
        ("inputs", vz_native_runtime.VZNativeHold),
        ("port", StateError),
        ("uid", vz_native_runtime.VZNativeHold),
    ],
)
def test_normal_network_custody_never_bypasses_other_cleanup_authority(
    normal_default_endpoints, monkeypatch, failure, expected_error
):
    case = normal_default_endpoints
    listener = None
    if failure == "upper":
        (case.home / "upper" / "foreign-consumer").write_text("retain")
    elif failure == "inputs":

        def changed_inputs(*_args, **_kwargs):
            raise vz_native_runtime.VZNativeHold("original input changed")

        monkeypatch.setattr(vz_native_runtime, "_load_checked", changed_inputs)
    elif failure == "port":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        case.data["vz_native"]["ssh_local_port"] = listener.getsockname()[1]
        monkeypatch.setattr(vz_native_runtime, "_rendered_config", lambda _native: {"portForwards": []})
        monkeypatch.setattr(vz_native_runtime, "_require_ssh_port_free", case.source_port_check)
    else:
        source_stat = os.stat

        def foreign_uid(path, *args, **kwargs):
            info = source_stat(path, *args, **kwargs)
            if path == "default_ep.sock" and kwargs.get("dir_fd") is not None:
                fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                fields["st_uid"] += 1
                return SimpleNamespace(**fields)
            return info

        monkeypatch.setattr(vz_native_runtime.os, "stat", foreign_uid)
    try:
        with pytest.raises(expected_error):
            case.settle()
    finally:
        if listener is not None:
            listener.close()
    assert case.writes == []
    assert case.actions == []
    assert case.data["vz_native"]["failed_start_cleanup"]["stop"] == "returned"


def test_last_default_probe_rechecks_previously_probed_leaf(normal_default_endpoints, monkeypatch):
    case = normal_default_endpoints
    case.settle()
    native = case.data["vz_native"]
    pins = copy.deepcopy(native["failed_start_cleanup"]["after_stop"]["default_network_endpoints"])
    directory_stamp = vz_native_runtime._stamp(case.directory.stat())
    source_socket = socket.socket
    probed = []

    class Probe:
        def __init__(self, family, kind):
            self.channel = source_socket(family, kind)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.channel.close()
            return False

        def settimeout(self, timeout):
            self.channel.settimeout(timeout)

        def connect(self, path):
            try:
                self.channel.connect(path)
            finally:
                probed.append(Path(path))
                if len(probed) == 4:
                    # Mutate the other leaf, not the one whose probe just completed.
                    assert probed[0] != probed[-1]
                    probed[0].chmod(0o700)
                    assert vz_native_runtime._stamp(case.directory.stat()) == directory_stamp

    monkeypatch.setattr(vz_native_runtime.socket, "socket", Probe)
    with case.scope():
        with pytest.raises(vz_native_runtime.VZNativeHold):
            vz_native_runtime._failed_start_default_network_pins(
                case.directory, native, vz_native_runtime._FAILED_START_CLEANUP.get(), previous=pins
            )
    assert len(probed) == 4
    assert len(case.writes) == 1
    assert native["failed_start_cleanup"]["after_stop"]["default_network_endpoints"] == pins
    assert case.actions == []


@pytest.fixture
def ordinary_stop_case(tmp_path, monkeypatch):
    """Real canonical ledger/custody files; provider and boot pins are host seams."""
    from palimpsest_local import state
    from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind

    home = tmp_path.resolve() / "lima"
    home.mkdir(mode=0o700)
    custody = home / "_palimpsest-native"
    custody.mkdir(mode=0o700)
    upper = home / "upper"
    upper.mkdir(mode=0o700)
    (upper / "datadisk").write_bytes(b"retained upper")
    roots = state.StatePaths(tmp_path.resolve() / "config", tmp_path.resolve() / "state")
    state.init_resolved_roots(roots)
    key = DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.LIMA_VZ)
    with state.reserve_new_run(roots, "ordinary", key) as reservation:
        run_id = reservation.record.run_id
        instance = "plm-" + run_id.replace("-", "")[:12]
        directory = home / instance
        directory.mkdir(mode=0o700)
        (directory / "keep").write_bytes(b"provider root")
        identity = directory.stat()
        native = {
            "hold": "stop-incomplete",
            "instance": instance,
            "instance_identity": [identity.st_dev, identity.st_ino],
            "root_volume_id": "bdb3c42e-0410-4a6e-987a-bb293235993a",
            "start_attempt": {"id": "0caa89a3-c4b7-419d-a7b5-b3629f578932"},
            "plan_sha256": "1" * 64,
            "ssh_local_port": 59571,
            "plan": {
                "run_id": run_id,
                "root_filesystem_uuid": "bdb3c42e-0410-4a6e-987a-bb293235993a",
                "root_nonce": "2" * 64,
                "layers": [],
                "mode": "dev",
                "restart": "no",
            },
        }
        status = {
            "schema": "palimpsest.vz-root-status.v1",
            "platform_abi": vz_native_runtime.PLATFORM_ABI,
            "run_id": run_id,
            "plan_sha256": native["plan_sha256"],
            "root_filesystem_uuid": native["plan"]["root_filesystem_uuid"],
            "root_nonce": native["plan"]["root_nonce"],
            "root_filesystem": "overlay",
            "root_device": 29,
            "root_inode": 2,
            "pid1_root_device": 29,
            "pid1_root_inode": 2,
            "lower_mounts": [],
            "bootstrap_tcp_open": False,
            "phase": "stop-ready",
            "explicit_stop_fenced": True,
            "user_session_count": 0,
            "user_sessions_known": True,
            "main": {"state": "exited", "pid": 0, "exit_code": 0, "output_complete": True},
        }
        reservation.write_state("failed", {"vz_native": native, "vz_status": status})
    marker = custody / ("hold-" + run_id + ".json")
    vz_native_runtime._new_file(
        marker,
        vz_native_runtime._json({"schema": vz_native_runtime._SCHEMA, "run_id": run_id, "reason": "stop-incomplete"}),
    )
    case = SimpleNamespace(
        roots=roots,
        home=home,
        directory=directory,
        upper=upper,
        marker=marker,
        run_id=run_id,
        record=state.read_run_dispatch_record(roots, "ordinary"),
        processes=(),
        commands=[],
        provider={"status": "Stopped", "dir": str(directory)},
        provider_reads=0,
        provider_hook=None,
        pin_failure=False,
        pin_reads=0,
        control=None,
    )
    case.config = SimpleNamespace(home=home, verify=lambda **_kw: None)
    case.read = lambda: state.read_json(state.run_paths(roots, "ordinary").state)

    def update(status=None, change=None):
        with state.locked_existing_run(roots, "ordinary", expected=case.record) as authority:
            data = authority.mutable_state()
            if change is not None:
                change(data)
            return vz_native_runtime._write(authority, status or data["status"], data)

    def instance(*_args, **_kwargs):
        case.provider_reads += 1
        if case.provider_hook is not None:
            case.provider_hook(case.provider_reads)
        return copy.deepcopy(case.provider)

    def pins(authority, _roots, config, _custody, *, data=None, stopped_hold=False, **_kwargs):
        case.pin_reads += 1
        if case.pin_failure:
            raise vz_native_runtime.VZNativeHold("original boot/source pin changed")
        data = authority.mutable_state() if data is None else data
        if stopped_hold:
            vz_native_runtime._ordinary_stop_hold(authority, config, data)
        return data, None, None, None, None, None

    def forbidden(*args, **kwargs):
        case.commands.append((args, kwargs))
        raise AssertionError("settlement must not operate on the provider or signal a process")

    monkeypatch.setattr(vz_native_runtime, "_config_for", lambda _authority: case.config)
    monkeypatch.setattr(vz_native_runtime, "_bound_host_config", lambda _native: case.config)
    monkeypatch.setattr(vz_native_runtime, "_verify_host_authority", lambda *_args: None)
    monkeypatch.setattr(vz_native_runtime, "_load_checked", pins)
    monkeypatch.setattr(vz_native_runtime, "_owned_instance", instance)
    monkeypatch.setattr(vz_native_runtime, "list_processes", lambda: case.processes)
    monkeypatch.setattr(
        vz_native_runtime,
        "load_vz_root_volume",
        lambda *_args, **_kw: SimpleNamespace(record=SimpleNamespace(storage=SimpleNamespace(directory=upper))),
    )
    case.port_checker = vz_native_runtime._require_ssh_port_free
    monkeypatch.setattr(vz_native_runtime, "_require_ssh_port_free", lambda _native: None)
    monkeypatch.setattr(vz_native_runtime, "_lima", forbidden)
    monkeypatch.setattr(vz_native_runtime, "_control", forbidden)
    monkeypatch.setattr(vz_native_runtime.os, "kill", forbidden)
    case.update = update
    case.stop = lambda: vz_native_runtime.stop_oci_run(roots, "ordinary", expected_record=case.record)
    case.start = lambda: vz_native_runtime.start_oci_run(roots, "ordinary", expected_record=case.record)
    return case


def test_public_stop_settles_exact_ordinary_hold_without_replaying_shutdown(ordinary_stop_case):
    case = ordinary_stop_case
    before = case.read()
    result = case.stop()
    after = case.read()
    assert (result.previous_status, result.current_status) == ("failed", "stopped")
    assert after["vz_native"]["hold"] is None
    assert after["vz_native"]["stop_settlement"]["phase"] == "settled"
    assert after["vz_status"] == before["vz_status"]
    assert after["vz_native"]["start_attempt"] == before["vz_native"]["start_attempt"]
    assert not case.marker.exists()
    assert (case.upper / "datadisk").read_bytes() == b"retained upper"
    assert (case.directory / "keep").read_bytes() == b"provider root"
    assert case.commands == []


@pytest.mark.parametrize("status", ["Running", "Broken", "Stopping", "", None])
def test_ordinary_hold_never_adopts_nonstopped_provider(ordinary_stop_case, status):
    case = ordinary_stop_case
    case.provider["status"] = status
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


@pytest.mark.parametrize(
    "change",
    [
        "pid",
        "null-pid",
        "bool-pid",
        "autostart",
        "directory",
        "upper",
        "endpoint",
        "actor",
        "unknown-actor",
        "source-pin",
        "session",
        "active",
        "other-hold",
        "quiescence",
        "wrong-plan",
        "failed-start",
    ],
)
def test_ordinary_hold_preserves_uncertain_authority_without_cleanup(ordinary_stop_case, change):
    case = ordinary_stop_case
    if change == "pid":
        case.provider["hostAgentPID"] = 900
    elif change == "null-pid":
        case.provider["driverPID"] = None
    elif change == "bool-pid":
        case.provider["driverPID"] = False
    elif change == "autostart":
        case.provider["autoStartedIdentifier"] = "foreign"
    elif change == "directory":
        case.directory.rename(case.home / "original")
        case.directory.mkdir(mode=0o700)
    elif change == "upper":
        (case.upper / "in_use_by").symlink_to(case.directory)
    elif change == "endpoint":
        (case.directory / "ha.pid").write_text("900")
    elif change in {"actor", "unknown-actor"}:
        case.processes = (
            darwin_process.DarwinProcessIdentity(
                900,
                1,
                os.geteuid(),
                900,
                1000,
                None if change == "unknown-actor" else "/pinned/lima",
                ("detached-helper", str(case.directory / "ha.sock")),
            ),
        )
    elif change == "source-pin":
        case.pin_failure = True
    elif change in {"session", "active", "other-hold"}:
        name = {"session": "session-foreign", "active": "active.json", "other-hold": "hold-foreign.json"}[change]
        (case.marker.parent / name).write_bytes(b"unsettled")
    elif change == "quiescence":
        case.update(change=lambda data: data["vz_status"].update(user_session_count=1))
    elif change == "wrong-plan":
        case.update(change=lambda data: data["vz_status"].update(plan_sha256="f" * 64))
    else:
        case.update(change=lambda data: data["vz_native"].update(failed_start_cleanup={"stop": "attempted"}))
    before = case.read()
    with pytest.raises((StateError, vz_native_runtime.VZNativeHold)):
        case.stop()
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


@pytest.mark.parametrize("race", ["provider-restarts", "actor-appears", "upper-reclaimed"])
def test_ordinary_settlement_rechecks_absence_before_durable_adoption(ordinary_stop_case, race):
    case = ordinary_stop_case

    def change(reads):
        if reads != 2:
            return
        if race == "provider-restarts":
            case.provider["status"] = "Running"
        elif race == "actor-appears":
            case.processes = (
                darwin_process.DarwinProcessIdentity(
                    900,
                    1,
                    os.geteuid(),
                    900,
                    1000,
                    None,
                    ("actor", str(case.directory)),
                ),
            )
        else:
            (case.upper / "in_use_by").symlink_to(case.directory)

    case.provider_hook = change
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


@pytest.mark.parametrize("boundary", ["before-release", "after-release"])
def test_ordinary_settlement_resumes_only_its_durable_marker_release(ordinary_stop_case, monkeypatch, boundary):
    case = ordinary_stop_case
    source_unlink = os.unlink
    source_write = vz_native_runtime._write

    def unlink(path, *args, **kwargs):
        if path == case.marker.name:
            raise StateError("interrupted marker release")
        return source_unlink(path, *args, **kwargs)

    def write(authority, status, data):
        if data.get("vz_native", {}).get("stop_settlement", {}).get("phase") == "settled":
            raise StateError("interrupted final settlement write")
        return source_write(authority, status, data)

    if boundary == "before-release":
        monkeypatch.setattr(vz_native_runtime.os, "unlink", unlink)
    else:
        monkeypatch.setattr(vz_native_runtime, "_write", write)
    with pytest.raises(StateError):
        case.stop()
    midway = case.read()
    assert midway["status"] == "stopped"
    assert midway["vz_native"]["stop_settlement"]["phase"] == "verified"
    assert case.marker.exists() is (boundary == "before-release")
    monkeypatch.setattr(vz_native_runtime.os, "unlink", source_unlink)
    monkeypatch.setattr(vz_native_runtime, "_write", source_write)
    assert case.stop().current_status == "stopped"
    assert case.read()["vz_native"]["stop_settlement"]["phase"] == "settled"
    assert not case.marker.exists() and case.commands == []


def test_ordinary_settlement_never_releases_replaced_verified_marker(ordinary_stop_case, monkeypatch):
    case = ordinary_stop_case
    source_unlink = os.unlink

    def interrupt(path, *args, **kwargs):
        if path == case.marker.name:
            raise StateError("interrupted")
        return source_unlink(path, *args, **kwargs)

    monkeypatch.setattr(vz_native_runtime.os, "unlink", interrupt)
    with pytest.raises(StateError):
        case.stop()
    monkeypatch.setattr(vz_native_runtime.os, "unlink", source_unlink)
    payload = case.marker.read_bytes()
    case.marker.rename(case.home / "retained-marker")
    vz_native_runtime._new_file(case.marker, payload)
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


def test_public_start_reconciles_clean_dev_idle_exit_and_retains_upper(ordinary_stop_case, monkeypatch):
    case = ordinary_stop_case
    case.marker.unlink()
    case.update("exited", lambda data: data["vz_native"].update(hold=None))
    original = case.read()

    def start(authority, _roots, _config, _custody, data):
        assert authority.snapshot.state["status"] == "stopped"
        assert (case.upper / "datadisk").read_bytes() == b"retained upper"
        return vz_native_runtime._write(authority, "running", data)

    monkeypatch.setattr(vz_native_runtime, "_start", start)
    monkeypatch.setattr(vz_native_runtime, "_account_disks", lambda *_args: None)
    result = case.start()
    assert (result.previous_status, result.current_status) == ("exited", "running")
    assert case.read()["vz_native"]["root_volume_id"] == original["vz_native"]["root_volume_id"]
    assert case.commands == []


@pytest.mark.parametrize("change", ["prod", "running", "broken", "actor", "upper", "pin", "held", "wrong-root"])
def test_dev_idle_start_never_adopts_unclean_or_other_exit(ordinary_stop_case, monkeypatch, change):
    case = ordinary_stop_case
    case.marker.unlink()
    case.update("exited", lambda data: data["vz_native"].update(hold=None))
    if change == "prod":
        case.update(change=lambda data: data["vz_native"]["plan"].update(mode="prod"))
    elif change in {"running", "broken"}:
        case.provider["status"] = change.capitalize()
    elif change == "actor":
        case.processes = (
            darwin_process.DarwinProcessIdentity(900, 1, os.geteuid(), 900, 1000, None, ("x", str(case.directory))),
        )
    elif change == "upper":
        (case.upper / "in_use_by").symlink_to(case.directory)
    elif change == "pin":
        case.pin_failure = True
    elif change == "held":
        vz_native_runtime._new_file(
            case.marker,
            vz_native_runtime._json(
                {"schema": vz_native_runtime._SCHEMA, "run_id": case.run_id, "reason": "stop-incomplete"},
            ),
        )
    else:
        case.update(change=lambda data: data["vz_status"].update(root_nonce="f" * 64))
    monkeypatch.setattr(vz_native_runtime, "_start", lambda *_args: pytest.fail("must not start"))
    with pytest.raises((StateError, vz_native_runtime.VZNativeHold)):
        case.start()
    assert case.read()["status"] != "running" and case.commands == []


@pytest.mark.parametrize(
    "sequence,success",
    [
        (["Running", "Broken", "Running", "Stopped"], True),
        (["Running", "Broken"], False),
        (["Running", "Unknown"], False),
        (["Broken"], False),
    ],
)
def test_authenticated_poweroff_observes_broken_only_within_original_deadline(
    ordinary_stop_case,
    monkeypatch,
    sequence,
    success,
):
    case = ordinary_stop_case
    case.marker.unlink()
    case.update("running", lambda data: data["vz_native"].update(hold=None))
    controls = []
    clock = [0.0]
    statuses = iter(sequence)
    last = [sequence[-1]]

    def instance(*_args, **_kwargs):
        last[0] = next(statuses, last[0])
        return {**case.provider, "status": last[0]}

    def control(_config, _native, operation, _custody, **_kwargs):
        controls.append(operation)
        if operation == "stop":
            return {"accepted": True, "phase": "stopping"}
        if operation == "poweroff":
            return {"accepted": True, "phase": "powering-off"}
        return case.read()["vz_status"]

    monkeypatch.setattr(vz_native_runtime, "_owned_instance", instance)
    monkeypatch.setattr(vz_native_runtime, "_control", control)
    monkeypatch.setattr(vz_native_runtime, "_capture_logs", lambda *_args, **_kw: None)
    monkeypatch.setattr(vz_native_runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(vz_native_runtime.time, "sleep", lambda _duration: clock.__setitem__(0, clock[0] + 20))
    if success:
        assert case.stop().current_status == "stopped"
        assert controls == ["stop", "status", "poweroff"]
    else:
        with pytest.raises(vz_native_runtime.VZNativeHold):
            case.stop()
        assert case.read()["status"] == "failed"
        assert case.read()["vz_native"]["hold"] == "stop-incomplete"
        assert controls == ([] if sequence[0] == "Broken" else ["stop", "status", "poweroff"])
    assert clock[0] <= 60
    assert case.commands == []


@pytest.mark.parametrize("change", ["missing", "reason", "run", "live-autostart"])
def test_ordinary_settlement_requires_original_marker_and_no_autostart(ordinary_stop_case, change):
    case = ordinary_stop_case
    if change == "live-autostart":
        agents = case.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / ("io.lima-vm.autostart." + case.directory.name + ".plist")).write_bytes(b"retain")
    else:
        case.marker.unlink()
        if change != "missing":
            vz_native_runtime._new_file(
                case.marker,
                vz_native_runtime._json(
                    {
                        "schema": vz_native_runtime._SCHEMA,
                        "run_id": case.run_id if change == "reason" else "f04f1c21-4892-4c02-943d-72b28778763b",
                        "reason": "launch-incomplete" if change == "reason" else "stop-incomplete",
                    }
                ),
            )
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read() == before and case.commands == []
    assert case.marker.exists() is (change != "missing")


@pytest.mark.parametrize("change", ["boot-pins", "marker"])
def test_ordinary_settlement_rejects_changes_between_observation_and_commit(
    ordinary_stop_case,
    monkeypatch,
    change,
):
    case = ordinary_stop_case
    source_load = vz_native_runtime._load_checked

    def checked(*args, **kwargs):
        if case.pin_reads == 1:
            if change == "boot-pins":
                case.pin_failure = True
            else:
                payload = case.marker.read_bytes()
                case.marker.rename(case.home / "old-marker")
                vz_native_runtime._new_file(case.marker, payload)
        return source_load(*args, **kwargs)

    monkeypatch.setattr(vz_native_runtime, "_load_checked", checked)
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


def test_poweroff_deadline_cannot_be_extended_by_a_late_stopped_observation(ordinary_stop_case, monkeypatch):
    case = ordinary_stop_case
    case.marker.unlink()
    case.update("running", lambda data: data["vz_native"].update(hold=None))
    clock = [0.0]
    reads = [0]
    controls = []

    def instance(*_args, **_kwargs):
        reads[0] += 1
        if reads[0] > 1:
            clock[0] = 61.0
        return {**case.provider, "status": "Running" if reads[0] == 1 else "Stopped"}

    def control(_config, _native, operation, _custody, **_kwargs):
        controls.append(operation)
        if operation == "stop":
            return {"accepted": True, "phase": "stopping"}
        if operation == "poweroff":
            return {"accepted": True, "phase": "powering-off"}
        return case.read()["vz_status"]

    monkeypatch.setattr(vz_native_runtime, "_owned_instance", instance)
    monkeypatch.setattr(vz_native_runtime, "_control", control)
    monkeypatch.setattr(vz_native_runtime, "_capture_logs", lambda *_args, **_kw: None)
    monkeypatch.setattr(vz_native_runtime.time, "monotonic", lambda: clock[0])
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read()["status"] == "failed"
    assert case.read()["vz_native"]["hold"] == "stop-incomplete"
    assert controls == ["stop", "status", "poweroff"] and case.commands == []


def test_ordinary_stop_hold_is_not_failed_launch_removal_authority(ordinary_stop_case, monkeypatch):
    case = ordinary_stop_case
    before = case.read()
    monkeypatch.setattr(
        vz_native_runtime,
        "_require_failed_start_operation",
        lambda *_args: pytest.fail("a completed boot's stop HOLD is not failed-start cleanup"),
    )
    with pytest.raises(vz_native_runtime.VZNativeHold):
        vz_native_runtime.rm_oci_run(case.roots, "ordinary", expected_record=case.record)
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


@pytest.mark.parametrize("operation", ["stop", "refresh"])
def test_unrelated_stopped_endpoint_reuse_never_creates_hold(ordinary_stop_case, monkeypatch, operation):
    case = ordinary_stop_case
    case.marker.unlink()
    monkeypatch.setattr(vz_native_runtime, "_require_ssh_port_free", case.port_checker)
    monkeypatch.setattr(vz_native_runtime, "_rendered_config", lambda _native: {"portForwards": []})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen(1)
        port = unrelated.getsockname()[1]
        case.update("stopped", lambda data: data["vz_native"].update(hold=None, ssh_local_port=port))
        if operation == "stop":
            assert case.stop().current_status == "stopped"
        else:
            assert (
                vz_native_runtime.refresh_run(case.roots, "ordinary", expected_record=case.record)["status"]
                == "stopped"
            )
    after = case.read()
    assert after["status"] == "stopped" and after["vz_native"]["hold"] is None
    assert not case.marker.exists() and case.commands == []


@pytest.mark.parametrize("status", ["stopped", "exited"])
@pytest.mark.parametrize(
    "change",
    ["shape", "phase", "unfinished", "run", "plan", "attempt", "marker", "stamp", "digest", "extra", "observation"],
)
def test_start_preserves_unknown_or_unfinished_stop_journal(ordinary_stop_case, status, change):
    case = ordinary_stop_case
    case.stop()

    def corrupt(data):
        native = data["vz_native"]
        recovery = native["stop_settlement"]
        if change == "shape":
            native["stop_settlement"] = []
        elif change == "phase":
            recovery["phase"] = "unknown"
        elif change == "unfinished":
            recovery["phase"] = "verified"
        elif change == "run":
            recovery["run_id"] = "f04f1c21-4892-4c02-943d-72b28778763b"
        elif change == "plan":
            recovery["plan_sha256"] = "f" * 64
        elif change == "attempt":
            recovery["start_attempt"] = {"id": "different"}
        elif change == "marker":
            recovery["marker_path"] = str(case.home / "unrelated-marker")
        elif change == "stamp":
            recovery["marker_stamp"][0] = True
        elif change == "extra":
            recovery["pending_operation"] = "start"
        elif change == "observation":
            recovery["observation"]["provider_status"] = "Running"
        else:
            recovery["marker_digest"] = "unbound"

    case.update(status, corrupt)
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.start()
    assert case.read() == before
    assert not case.marker.exists() and case.commands == []


@pytest.mark.parametrize("change", ["provider", "actor", "upper"])
def test_ordinary_stop_cannot_ignore_changes_during_final_resource_reload(ordinary_stop_case, monkeypatch, change):
    case = ordinary_stop_case
    original_loader = vz_native_runtime._load_checked

    def late_change(*args, **kwargs):
        result = original_loader(*args, **kwargs)
        if case.pin_reads == 2:
            if change == "provider":
                case.provider["status"] = "Running"
            elif change == "actor":
                case.processes = (
                    darwin_process.DarwinProcessIdentity(
                        900,
                        1,
                        os.geteuid(),
                        900,
                        1000,
                        None,
                        ("actor", str(case.directory)),
                    ),
                )
            else:
                (case.upper / "in_use_by_late_consumer").write_bytes(b"owned elsewhere")
        return result

    monkeypatch.setattr(vz_native_runtime, "_load_checked", late_change)
    before = case.read()
    with pytest.raises(vz_native_runtime.VZNativeHold):
        case.stop()
    assert case.read() == before
    assert case.marker.exists() and case.commands == []


@pytest.mark.parametrize("status", ["stopped", "exited"])
def test_occupied_retained_port_rejects_start_before_any_state_change(ordinary_stop_case, monkeypatch, status):
    case = ordinary_stop_case
    case.marker.unlink()
    monkeypatch.setattr(vz_native_runtime, "_require_ssh_port_free", case.port_checker)
    monkeypatch.setattr(vz_native_runtime, "_rendered_config", lambda _native: {"portForwards": []})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen(1)
        port = unrelated.getsockname()[1]
        case.update(status, lambda data: data["vz_native"].update(hold=None, ssh_local_port=port))
        before = case.read()
        with pytest.raises(StateError):
            case.start()
        assert case.read() == before
    assert not case.marker.exists() and case.commands == []


def test_port_reuse_after_admission_does_not_create_an_unissued_start_hold(ordinary_stop_case, monkeypatch):
    case = ordinary_stop_case
    case.marker.unlink()
    monkeypatch.setattr(vz_native_runtime, "_require_ssh_port_free", case.port_checker)
    monkeypatch.setattr(vz_native_runtime, "_rendered_config", lambda _native: {"portForwards": []})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unrelated:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        case.update("stopped", lambda data: data["vz_native"].update(hold=None, ssh_local_port=port))

        def reuse(*args, **kwargs):
            unrelated.bind(("127.0.0.1", port))
            unrelated.listen(1)

        monkeypatch.setattr(vz_native_runtime, "_account_disks", reuse)
        before = case.read()
        with pytest.raises(StateError):
            case.start()
        assert case.read() == before
    assert not case.marker.exists() and case.commands == []
