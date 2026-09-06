"""Fresh coordinator data, descriptor, endpoint and ambiguous-failure boundaries."""

import os
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import test_oci_monitor_ipc as ipc_tests
import test_oci_monitor_launch as launch_tests

from palimpsest_local import oci_monitor_coordinator as coordinator
from palimpsest_local import oci_monitor_ipc as ipc
from palimpsest_local import oci_monitor_launch as launch
from palimpsest_local.errors import StateError


@pytest.fixture
def case(tmp_path):
    inputs = launch_tests.inputs.__wrapped__(tmp_path)
    with launch.prepare_monitor_launch_authority(*inputs) as authority:
        identity = ipc.MonitorExecIdentity(inputs[-1], ipc_tests._GENERATION)
        endpoint = ipc.MonitorExecEndpoint(identity, ipc_tests._process(), 1, 2)
        yield SimpleNamespace(inputs=inputs, authority=authority, identity=identity, endpoint=endpoint)


def request(case):
    return coordinator.MonitorCoordinatorRequest(case.identity.generation, "a" * 64, 5000, case.authority.to_dict())


def test_request_actual_socket_roundtrip(case):
    expected = request(case)
    left, right = socket.socketpair()
    sender = threading.Thread(target=ipc._send_all, args=(left, expected.to_bytes()))
    sender.start()
    try:
        actual = coordinator.MonitorCoordinatorRequest.from_dict(
            ipc._recv_bounded_frame(right, coordinator._MAX_REQUEST_BYTES)
        )
        assert actual == expected
    finally:
        sender.join(5)
        assert not sender.is_alive()
        left.close()
        right.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "other"),
        ("nonce", "a"),
        ("generation", "invalid"),
        ("timeout_ms", True),
        ("timeout_ms", 99),
        ("timeout_ms", 30001),
        ("authority", None),
        ("argv", ["evil"]),
    ],
)
def test_request_closed_schema_and_scalar_limits(case, field, value):
    data = request(case).to_dict()
    data[field] = value
    with pytest.raises(StateError):
        coordinator.MonitorCoordinatorRequest.from_dict(data)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--help"],
        ["--private-coordinator-v1", "03"],
        ["--private-coordinator-v1", "9" * 5000],
        ["--private-coordinator-v1", "2147483648"],
        ["--private-coordinator-v1", 3],
        ["--private-coordinator-v1", "2"],
        "wrong",
    ],
)
def test_private_argv_refuses_noncanonical_fd_without_action(args, monkeypatch):
    monkeypatch.setattr(coordinator, "_child_main", lambda *_: pytest.fail("invalid argv reached child"))
    assert coordinator.main(args) == 2


def test_oversized_request_refused_before_socket_or_process(case, monkeypatch):
    monkeypatch.setattr(
        launch.MonitorLaunchAuthority, "to_dict", lambda _: {"padding": "x" * coordinator._MAX_REQUEST_BYTES}
    )
    monkeypatch.setattr(socket, "socketpair", lambda *_: pytest.fail("oversized request created socket"))
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("oversized request created child"))
    with pytest.raises(StateError):
        coordinator.spawn_monitor_coordinator(case.identity, case.authority)


def install_peer(case, monkeypatch, *, damage=None):
    observed = {}

    class Process:
        def __init__(self, argv, **kwargs):
            observed.update(argv=argv, kwargs=kwargs)
            channel = socket.socket(fileno=os.dup(kwargs["pass_fds"][0]))
            self.thread = threading.Thread(target=self.reply, args=(channel,))
            self.thread.start()

        def reply(self, channel):
            with channel:
                value = ipc._recv_bounded_frame(channel, coordinator._MAX_REQUEST_BYTES)
                observed["request"] = value
                if damage == "lost":
                    return
                response = coordinator._response(value["nonce"], case.endpoint)
                if damage == "nonce":
                    response["nonce"] = "b" * 64
                if damage == "ready":
                    response["state"] = "ready"
                if damage == "generation":
                    response["endpoint"]["generation"] = "00000000-0000-4000-8000-000000000004"
                ipc._send_frame(channel, response)

        def wait(self, timeout):
            self.thread.join(timeout)
            assert not self.thread.is_alive()
            return 0

        def terminate(self):
            pytest.fail("uncertain coordinator must not terminate a process")

        def kill(self):
            pytest.fail("uncertain coordinator must not kill a process")

    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr(ipc, "discover_monitor_exec", lambda *_a, **_k: case.endpoint)
    return observed


def test_parent_may_have_libvirt_and_threads_but_forwards_no_ambient_environment(case, monkeypatch):
    observed = install_peer(case, monkeypatch)
    monkeypatch.setitem(sys.modules, "libvirt", object())
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("SECRET_VALUE", "never forwarded")
    event = threading.Event()
    thread = threading.Thread(target=event.wait)
    thread.start()
    try:
        result = coordinator.spawn_monitor_coordinator(case.identity, case.authority)
    finally:
        event.set()
        thread.join(2)
    assert result == case.endpoint
    assert observed["argv"][:4] == [
        sys.executable,
        "-m",
        "palimpsest_local.oci_monitor_coordinator",
        "--private-coordinator-v1",
    ]
    assert observed["kwargs"]["env"] == {"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}
    assert observed["kwargs"]["cwd"] == coordinator._PACKAGE_ROOT
    assert observed["kwargs"]["close_fds"] and observed["kwargs"]["start_new_session"]
    assert observed["kwargs"]["pass_fds"][1:] == case.authority.pass_fds
    assert observed["request"]["authority"] == case.authority.to_dict()
    case.authority.validate()


@pytest.mark.parametrize("damage", ["lost", "nonce", "generation", "ready", "discover"])
def test_ambiguous_response_preserves_evidence_and_never_kills(case, monkeypatch, damage):
    path = case.inputs[0].runs / case.identity.binding.record.name
    before = (path / "state.json").read_bytes()
    install_peer(case, monkeypatch, damage=damage)
    if damage == "discover":
        monkeypatch.setattr(ipc, "discover_monitor_exec", lambda *_a, **_k: None)
    with pytest.raises(StateError, match="uncertain"):
        coordinator.spawn_monitor_coordinator(case.identity, case.authority)
    assert (path / "state.json").read_bytes() == before
    case.authority.validate()


@pytest.mark.parametrize("timeout", [None, True, float("nan"), float("inf"), "5", 0.09, 30.01])
def test_spawn_timeout_must_remain_finite(case, monkeypatch, timeout):
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("bad timeout spawned"))
    with pytest.raises(StateError):
        coordinator.spawn_monitor_coordinator(case.identity, case.authority, timeout=timeout)


@pytest.mark.parametrize("lost_response", [False, True])
def test_child_reconstructs_then_spawns_and_detaches_without_shutdown(case, monkeypatch, lost_response):
    left, right = socket.socketpair()
    value = request(case).to_dict()
    descriptors = []
    for entry in value["authority"]["entries"].values():
        entry["fd"] = os.dup(entry["fd"])
        descriptors.append(entry["fd"])
    events = []
    monkeypatch.setattr(ipc, "_require_spawn_boundary", lambda: events.append("clean-boundary"))

    def spawn(directory_fd, identity, *, timeout, launch_authority):
        launch_authority.validate(directory_fd=directory_fd, binding=identity.binding)
        assert directory_fd not in launch_authority.pass_fds
        assert identity == case.identity and timeout == 5
        events.append("spawn")
        return SimpleNamespace(endpoint=case.endpoint, close=lambda: events.append("detach"))

    monkeypatch.setattr(ipc, "spawn_monitor_exec", spawn)
    sender = threading.Thread(target=ipc._send_all, args=(left, ipc._canonical_bytes(value)))
    sender.start()
    if lost_response:

        def broken_response(*_args):
            raise BrokenPipeError("coordinator response lost after monitor accepted")

        monkeypatch.setattr(ipc, "_send_frame", broken_response)
    try:
        result = coordinator._child_main(right.detach())
        if not lost_response:
            assert ipc._recv_frame(left) == coordinator._response(value["nonce"], case.endpoint)
        assert result == (1 if lost_response else 0)
        assert events == ["clean-boundary", "spawn", "detach"]
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        case.authority.validate()
    finally:
        sender.join(5)
        assert not sender.is_alive()
        left.close()
        right.close()


@pytest.mark.parametrize("damage", ["bad-authority", "wrong-schema", "oversized-header"])
def test_actual_fresh_private_entry_refuses_before_monitor_mutation(case, damage):
    left, right = socket.socketpair()
    value = request(case).to_dict()
    value["authority"]["entries"]["run"]["inode"] += 1
    if damage == "wrong-schema":
        value["schema"] = "other"
    root = case.inputs[0].runs / case.identity.binding.record.name
    before = (root / "state.json").read_bytes()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "palimpsest_local.oci_monitor_coordinator",
            "--private-coordinator-v1",
            str(right.fileno()),
        ],
        pass_fds=(right.fileno(), *case.authority.pass_fds),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=coordinator._PACKAGE_ROOT,
        env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
    )
    right.close()
    left.settimeout(10)
    try:
        try:
            if damage == "oversized-header":
                left.sendall(ipc._FRAME_LENGTH.pack(coordinator._MAX_REQUEST_BYTES + 1))
            else:
                ipc._send_all(left, ipc._canonical_bytes(value))
            try:
                response = ipc._recv_frame(left)
                assert response["state"] == "refused" and response["endpoint"] is None
            except ipc.MonitorIPCError:
                assert sys.platform != "linux", "Linux child should return its closed refusal response"
        except (BrokenPipeError, ipc.MonitorIPCError):
            assert sys.platform != "linux"
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 1 and not stdout and not stderr
    finally:
        left.close()
    assert (root / "state.json").read_bytes() == before
    assert list((root / "monitor-private").iterdir()) == []
