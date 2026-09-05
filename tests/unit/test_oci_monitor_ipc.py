"""Fresh-exec and local IPC boundary tests for the inert OCI monitor."""

from __future__ import annotations

import ast
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import palimpsest_local.oci_monitor_ipc as ipc
from palimpsest_local.oci_monitor import MonitorProcessIdentity, ProcessLiveness
from palimpsest_local.runtime_types import DispatchKey, ExistingRunRecord, RuntimeBackend, RuntimeKind

_RUN_ID = "11849d77-fdd8-4f65-92a0-bbc75ea80767"
_GENERATION = "21849d77-fdd8-4f65-92a0-bbc75ea80767"
_BOOT_ID = "31849d77-fdd8-4f65-92a0-bbc75ea80767"


def _binding(**changes: object) -> ipc.MonitorPreActivationBinding:
    values: dict[str, object] = {
        "record": ExistingRunRecord(
            "oci-demo",
            _RUN_ID,
            2,
            DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        ),
        "owner_uid": os.geteuid(),
        "plan_digest": "sha256:" + "a" * 64,
        "expected_definition_projection_digest": "sha256:" + "c" * 64,
        "stage1_artifact_digest": "sha256:" + "b" * 64,
        "domain_uuid": "51849d77-fdd8-4f65-92a0-bbc75ea80767",
        "boot_attempt_id": "41849d77-fdd8-4f65-92a0-bbc75ea80767",
        "libvirt_uri": "qemu:///system",
    }
    values.update(changes)
    return ipc.MonitorPreActivationBinding(**values)  # type: ignore[arg-type]


def _identity(**changes: object) -> ipc.MonitorExecIdentity:
    values: dict[str, object] = {"binding": _binding(), "generation": _GENERATION}
    values.update(changes)
    return ipc.MonitorExecIdentity(**values)  # type: ignore[arg-type]


def _process(pid: int = 101, *, ticks: int = 202) -> MonitorProcessIdentity:
    return MonitorProcessIdentity(pid, _BOOT_ID, ticks)


def _directory(path: Path) -> int:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _linux_peercred() -> bool:
    return sys.platform == "linux" and hasattr(socket, "SO_PEERCRED") and Path("/proc/self/fd").is_dir()


def test_identity_and_binding_digest_are_canonical_and_path_free() -> None:
    assert _binding().digest == ipc.MonitorPreActivationBinding.from_dict(_binding().to_dict()).digest
    assert set(_identity().handshake_dict()) == {"binding_digest", "generation", "run_id"}
    for changes in (
        {"generation": str(uuid.uuid4()).upper()},
        {"binding": _binding(plan_digest="sha256:" + "c" * 64)},
    ):
        if changes.keys() == {"binding"}:
            assert _identity(**changes).binding_digest != _identity().binding_digest
        else:
            with pytest.raises(ipc.MonitorIPCError) as captured:
                _identity(**changes)
            assert captured.value.category is ipc.MonitorIPCErrorCategory.INVALID_IDENTITY
    for changes in (
        {"plan_digest": "sha256:" + "A" * 64},
        {"expected_definition_projection_digest": "bad"},
        {"stage1_artifact_digest": "bad"},
        {"domain_uuid": "not-a-uuid"},
        {"owner_uid": os.geteuid() + 1},
        {"libvirt_uri": "qemu:///session"},
    ):
        with pytest.raises(ipc.MonitorIPCError) as captured:
            _binding(**changes)
        assert captured.value.category is ipc.MonitorIPCErrorCategory.INVALID_IDENTITY


def test_frames_are_bounded_canonical_and_fragment_safe() -> None:
    left, right = socket.socketpair()
    try:
        payload = ipc._canonical_bytes({"kind": "ping", "value": 1})
        framed = struct.pack(">I", len(payload)) + payload
        for byte in framed:
            left.send(bytes([byte]))
        assert ipc._recv_frame(right) == {"kind": "ping", "value": 1}

        left.send(struct.pack(">I", 2) + b"{}")
        assert ipc._recv_frame(right) == {}

        noncanonical = b'{"z":1, "a":2}'
        left.send(struct.pack(">I", len(noncanonical)) + noncanonical)
        with pytest.raises(ipc.MonitorIPCError) as malformed:
            ipc._recv_frame(right)
        assert malformed.value.category is ipc.MonitorIPCErrorCategory.INVALID_FRAME

        left.send(struct.pack(">I", ipc._MAX_FRAME_BYTES + 1))
        with pytest.raises(ipc.MonitorIPCError) as oversized:
            ipc._recv_frame(right)
        assert oversized.value.category is ipc.MonitorIPCErrorCategory.INVALID_FRAME
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("field", ["run_id", "generation", "binding_digest"])
def test_request_rejects_wrong_identity_binding(field: str) -> None:
    identity = _identity()
    request = ipc._request_message(ipc.MonitorIPCOperation.PING, identity, _process(), str(uuid.uuid4()))
    request[field] = "sha256:" + "b" * 64 if field == "binding_digest" else str(uuid.uuid4())
    with pytest.raises(ipc.MonitorIPCError) as captured:
        ipc._decode_request(request, identity)
    assert captured.value.category is ipc.MonitorIPCErrorCategory.BINDING_MISMATCH


@pytest.mark.parametrize("field", ["run_id", "generation", "binding_digest", "nonce", "writer"])
def test_spawn_receipt_rejects_wrong_binding_generation_nonce_or_process(field: str) -> None:
    identity = _identity()
    writer = _process()
    nonce = "1" * 64
    message = ipc._spawn_message("prepared", identity, nonce, writer)
    if field == "writer":
        message[field] = _process(ticks=203).to_dict()
    elif field == "binding_digest":
        message[field] = "sha256:" + "b" * 64
    elif field == "nonce":
        message[field] = "2" * 64
    else:
        message[field] = str(uuid.uuid4())
    with pytest.raises(ipc.MonitorIPCError) as captured:
        ipc._validate_spawn_message(message, "prepared", identity, nonce, writer)
    assert captured.value.category is ipc.MonitorIPCErrorCategory.BINDING_MISMATCH


@pytest.mark.parametrize(
    ("credentials", "liveness"),
    [
        ((102, os.geteuid(), os.getegid()), ProcessLiveness.LIVE),
        ((101, os.geteuid() + 1, os.getegid()), ProcessLiveness.LIVE),
        ((101, os.geteuid(), os.getegid()), ProcessLiveness.STALE),
        ((101, os.geteuid(), os.getegid()), ProcessLiveness.UNKNOWN),
    ],
)
def test_peer_authorization_rejects_wrong_pid_uid_and_pid_reuse(
    credentials: tuple[int, int, int],
    liveness: ProcessLiveness,
) -> None:
    left, right = socket.socketpair()
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc._authorize_peer(
                left,
                _process(),
                os.geteuid(),
                credential_reader=lambda _channel: credentials,
                liveness_probe=lambda _identity: liveness,
            )
        assert captured.value.category is ipc.MonitorIPCErrorCategory.UNAUTHORIZED_PEER
    finally:
        left.close()
        right.close()


def test_peer_authorization_accepts_exact_kernel_and_proc_identity() -> None:
    left, right = socket.socketpair()
    try:
        ipc._authorize_peer(
            left,
            _process(),
            os.geteuid(),
            credential_reader=lambda _channel: (101, os.geteuid(), os.getegid()),
            liveness_probe=lambda _identity: ProcessLiveness.LIVE,
        )
    finally:
        left.close()
        right.close()


def test_protocol_stop_is_fixed_private_operation_without_ready_or_domain_mutation_command() -> None:
    assert set(ipc.MonitorIPCOperation) == {
        ipc.MonitorIPCOperation.DESCRIBE,
        ipc.MonitorIPCOperation.PING,
        ipc.MonitorIPCOperation.SHUTDOWN,
        ipc.MonitorIPCOperation.STOP,
    }
    source = Path(ipc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(name.endswith(("oci_root_runtime", "kvm", "libvirt")) for name in imports)
    assert not any(isinstance(node, ast.Name) and node.id == "MonitorLease" for node in ast.walk(tree))


@pytest.mark.parametrize("extra", [{"signal": 9}, {"grace_seconds": 1}, {"stop_request_id": str(uuid.uuid4())}])
def test_private_stop_rejects_caller_signal_grace_and_guest_request_id(extra: dict[str, object]) -> None:
    request = ipc._request_message(ipc.MonitorIPCOperation.STOP, _identity(), _process(), str(uuid.uuid4()))
    assert ipc._decode_request(request, _identity())[0] is ipc.MonitorIPCOperation.STOP
    with pytest.raises(ipc.MonitorIPCError) as failure:
        ipc._decode_request({**request, **extra}, _identity())
    assert failure.value.category is ipc.MonitorIPCErrorCategory.INVALID_FRAME


@pytest.mark.parametrize(
    "state", ["stop-accepted", "stop-terminal", "stop-refused", "terminal", "accepted", None, [], {}]
)
def test_stop_client_accepts_only_exact_path_free_response_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: object,
) -> None:
    from types import SimpleNamespace

    directory_fd = _directory(tmp_path / "run")
    endpoint = ipc.MonitorExecEndpoint(_identity(), _process(), 12, 34)
    request_id = uuid.uuid4()

    class Channel:
        def __init__(self, *_args):
            pass

        def settimeout(self, _timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    response = ipc._response_message(ipc.MonitorIPCOperation.STOP, _identity(), _process(), str(request_id))
    response["state"] = state
    monkeypatch.setattr(ipc.socket, "socket", Channel)
    monkeypatch.setattr(ipc, "current_process_identity", _process)
    monkeypatch.setattr(ipc.uuid, "uuid4", lambda: request_id)
    monkeypatch.setattr(ipc, "_visible_socket", lambda *_args: SimpleNamespace(st_dev=12, st_ino=34))
    monkeypatch.setattr(ipc, "_authorize_peer", lambda *_args: None)
    monkeypatch.setattr(ipc, "_connect_socket", lambda *_args: None)
    monkeypatch.setattr(ipc, "_send_frame", lambda *_args: None)
    monkeypatch.setattr(ipc, "_recv_frame", lambda *_args: response)
    try:
        if state in ("stop-accepted", "stop-terminal", "stop-refused"):
            assert ipc.request_monitor(directory_fd, endpoint, ipc.MonitorIPCOperation.STOP).state == state
        else:
            with pytest.raises(ipc.MonitorIPCError) as failure:
                ipc.request_monitor(directory_fd, endpoint, ipc.MonitorIPCOperation.STOP)
            assert failure.value.category is ipc.MonitorIPCErrorCategory.BINDING_MISMATCH
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("fail", [False, True])
def test_stop_mailbox_is_forwarded_only_to_launch_worker_thread(fail: bool) -> None:
    import threading
    from types import SimpleNamespace

    from palimpsest_local.oci_monitor_control import MonitorStopControl

    events = []

    class Authority:
        def run(self, _fd, _binding, _lease, *, stop_control):
            assert threading.current_thread() is not threading.main_thread()
            assert type(stop_control) is MonitorStopControl
            events.append(stop_control)
            if fail:
                raise RuntimeError("private failure")

        def close(self):
            events.append("closed")

    worker = ipc._LaunchWorker(
        Authority(), 123, _binding(), SimpleNamespace(snapshot=SimpleNamespace(phase="committed"))
    )
    worker.start()
    worker.join()
    assert worker.done.is_set()
    assert worker.failed is fail
    assert events == [worker.stop_control, "closed"]
    assert worker.stop_control.request() == ("control-lost" if fail else "stop-refused")


def test_in_memory_endpoint_receipt_is_exact_canonical_and_rejects_tamper() -> None:
    endpoint = ipc.MonitorExecEndpoint(_identity(), _process(), 12, 34)
    payload = endpoint.to_bytes()
    assert ipc.MonitorExecEndpoint.from_bytes(payload) == endpoint
    raw = json.loads(payload)
    raw["binding_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ipc.MonitorIPCError) as drift:
        ipc.MonitorExecEndpoint.from_bytes(ipc._canonical_bytes(raw) + b"\n")
    assert drift.value.category is ipc.MonitorIPCErrorCategory.BINDING_MISMATCH
    raw = json.loads(payload)
    raw["socket"]["path"] = "forbidden"
    with pytest.raises(ipc.MonitorIPCError) as extra:
        ipc.MonitorExecEndpoint.from_bytes(ipc._canonical_bytes(raw) + b"\n")
    assert extra.value.category is ipc.MonitorIPCErrorCategory.INVALID_FRAME
    with pytest.raises(ipc.MonitorIPCError) as noncanonical:
        ipc.MonitorExecEndpoint.from_bytes(json.dumps(json.loads(payload), indent=2).encode() + b"\n")
    assert noncanonical.value.category is ipc.MonitorIPCErrorCategory.INVALID_FRAME


def test_spawn_contract_uses_fresh_module_exec_and_exact_fd_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fail_after_capture(argv: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((argv, kwargs))
        raise OSError("injected")

    monkeypatch.setattr(ipc, "_require_spawn_boundary", lambda: None)
    monkeypatch.setattr(ipc, "current_process_identity", lambda: _process())
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc.spawn_monitor_exec(directory_fd, _identity(), popen_factory=fail_after_capture)
        assert captured.value.category is ipc.MonitorIPCErrorCategory.SPAWN_FAILED
    finally:
        os.close(directory_fd)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[1:4] == ["-m", "palimpsest_local.oci_monitor_ipc", "--private-child-v2"]
    assert kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == (directory_fd, int(argv[5]))
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {"PATH": os.defpath, "PYTHONNOUSERSITE": "1"}


def test_spawn_boundary_rejects_loaded_libvirt_or_multiple_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc.sys, "platform", "linux")
    monkeypatch.setattr(ipc.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setitem(ipc.sys.modules, "libvirt", object())
    with pytest.raises(ipc.MonitorIPCError) as loaded:
        ipc._require_spawn_boundary()
    assert loaded.value.category is ipc.MonitorIPCErrorCategory.SPAWN_BOUNDARY
    monkeypatch.delitem(ipc.sys.modules, "libvirt")
    monkeypatch.setattr(ipc.threading, "active_count", lambda: 2)
    with pytest.raises(ipc.MonitorIPCError) as threaded:
        ipc._require_spawn_boundary()
    assert threaded.value.category is ipc.MonitorIPCErrorCategory.SPAWN_BOUNDARY


@pytest.mark.parametrize("failure", [None, "journal", "ack"])
def test_parent_activation_fence_follows_exact_committed_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    from types import SimpleNamespace

    from palimpsest_local.oci_monitor_launch import MonitorLaunchAuthority

    directory_fd = _directory(tmp_path / "run")
    identity, parent, writer, nonce = _identity(), _process(), _process(), "1" * 64
    events = []
    authority = object.__new__(MonitorLaunchAuthority)
    monkeypatch.setattr(MonitorLaunchAuthority, "validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(MonitorLaunchAuthority, "pass_fds", property(lambda _self: (401, 402)))
    monkeypatch.setattr(MonitorLaunchAuthority, "to_dict", lambda _self: {"private": "fd-authority"})

    class Channel:
        def __init__(self, *_args, **_kwargs):
            pass

        def fileno(self):
            return 400

        def settimeout(self, _timeout):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    prepared = ipc.MonitorPreactivationJournalSnapshot(
        identity,
        ipc._nonce_digest(nonce),
        "prepared",
        2,
        writer,
        ipc._socket_name_for_generation(identity.generation),
        12,
        34,
    )
    responses = iter(
        [
            {"kind": "bound", "schema": ipc._SPAWN_SCHEMA},
            ipc._spawn_message("prepared", identity, nonce, writer, (12, 34)),
            ipc._spawn_message("committed", identity, nonce, writer, (12, 34)),
            ipc._spawn_message("launch-accepted", identity, nonce, writer, (12, 34)),
        ]
    )

    def receive(_channel):
        value = next(responses)
        if failure == "ack" and value["kind"] == "launch-accepted":
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.CLOSED)
        return value

    def exact(_fd, snapshot):
        events.append("read-" + snapshot.phase)
        if failure == "journal" and snapshot.phase == "committed":
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.INVALID_JOURNAL)

    def spawn(_argv, **kwargs):
        assert kwargs["pass_fds"] == (directory_fd, 400, 401, 402)
        return SimpleNamespace(pid=writer.pid)

    monkeypatch.setattr(ipc, "_require_spawn_boundary", lambda: None)
    monkeypatch.setattr(ipc, "current_process_identity", lambda: parent)
    monkeypatch.setattr(ipc.os, "urandom", lambda _count: bytes.fromhex(nonce))
    monkeypatch.setattr(ipc.socket, "socketpair", lambda *_args: (Channel(), Channel()))
    monkeypatch.setattr(ipc.socket, "socket", Channel)
    monkeypatch.setattr(ipc, "_send_frame", lambda _channel, value: events.append(value.get("kind", "config")))
    monkeypatch.setattr(ipc, "_recv_frame", receive)
    monkeypatch.setattr(ipc, "_visible_socket", lambda *_args: SimpleNamespace(st_dev=12, st_ino=34))
    monkeypatch.setattr(ipc, "_connect_socket", lambda *_args: None)
    monkeypatch.setattr(ipc, "_authorize_peer", lambda *_args: None)
    monkeypatch.setattr(ipc, "_read_boot_id_for_spawn", lambda: writer.host_boot_id)
    monkeypatch.setattr(ipc, "_read_start_ticks_for_spawn", lambda _pid: writer.start_ticks)
    monkeypatch.setattr(ipc, "_read_preactivation_journal", lambda *_args: (prepared, b"unused"))
    monkeypatch.setattr(ipc, "_require_exact_journal_snapshot", exact)
    monkeypatch.setattr(ipc, "_terminate_failed_child", lambda *_args: pytest.fail("committed child must survive"))
    try:
        if failure:
            with pytest.raises(ipc.MonitorIPCError):
                ipc.spawn_monitor_exec(directory_fd, identity, launch_authority=authority, popen_factory=spawn)
        else:
            handle = ipc.spawn_monitor_exec(directory_fd, identity, launch_authority=authority, popen_factory=spawn)
            handle.close()
        assert events[:5] == ["config", "prepare", "read-prepared", "commit", "read-committed"]
        assert ("activate" in events) is (failure != "journal")
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("authority", [object(), {}, True])
def test_spawn_rejects_untyped_launch_authority_before_child_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: object,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "_require_spawn_boundary", lambda: None)
    try:
        with pytest.raises(ipc.MonitorIPCError) as failure:
            ipc.spawn_monitor_exec(
                directory_fd,
                _identity(),
                launch_authority=authority,
                popen_factory=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
            )
        assert failure.value.category is ipc.MonitorIPCErrorCategory.INVALID_IDENTITY
        assert list((tmp_path / "run").iterdir()) == []
    finally:
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_fresh_exec_rejects_invalid_launch_authority_before_ownership_claim(tmp_path: Path) -> None:
    directory_fd = _directory(tmp_path / "run")
    local, child = socket.socketpair()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "palimpsest_local.oci_monitor_ipc",
            "--private-child-v2",
            str(directory_fd),
            str(child.fileno()),
        ],
        close_fds=True,
        pass_fds=(directory_fd, child.fileno()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
    )
    child.close()
    try:
        local.settimeout(2)
        ipc._send_frame(
            local,
            {
                "binding": _identity().binding.to_dict(),
                "generation": _identity().generation,
                "nonce": "1" * 64,
                "parent": ipc.current_process_identity().to_dict(),
                "schema": ipc._CONFIG_SCHEMA,
                "timeout_ms": 100,
                "launch_authority": {},
            },
        )
        assert ipc._recv_frame(local) == {"schema": ipc._SPAWN_SCHEMA, "kind": "error", "category": "child-failed"}
        assert process.wait(timeout=2) == 70
        assert list((tmp_path / "run").iterdir()) == []
    finally:
        local.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(directory_fd)


def test_directory_requires_exact_owner_private_mode(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o755)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc._validate_directory(descriptor)
        assert captured.value.category is ipc.MonitorIPCErrorCategory.UNSAFE_DIRECTORY
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (FileNotFoundError("sensitive-path"), ipc.MonitorIPCErrorCategory.CLOSED),
        (ConnectionRefusedError("sensitive-path"), ipc.MonitorIPCErrorCategory.CLOSED),
        (TimeoutError("sensitive-path"), ipc.MonitorIPCErrorCategory.TIMEOUT),
    ],
)
def test_connect_race_errors_are_stable_and_do_not_reflect_paths(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    category: ipc.MonitorIPCErrorCategory,
) -> None:
    class FailingChannel:
        def connect(self, _address: str) -> None:
            raise failure

    monkeypatch.setattr(ipc, "_socket_address", lambda _fd, _name=ipc._SOCKET_NAME: "/sensitive/path")
    with pytest.raises(ipc.MonitorIPCError) as captured:
        ipc._connect_socket(FailingChannel(), 7)  # type: ignore[arg-type]
    assert captured.value.category is category
    assert "sensitive" not in str(captured.value)


def test_spawn_start_ticks_parser_handles_tricky_comm_and_rejects_wrong_pid_or_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = ["S", *("0" for _ in range(18)), "98765", "0"]
    content = "123 (name with ) marker) " + " ".join(fields)
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: __import__("io").StringIO(content))
    assert ipc._read_start_ticks_for_spawn(123) == 98765
    with pytest.raises(ipc.MonitorIPCError) as wrong_pid:
        ipc._read_start_ticks_for_spawn(124)
    assert wrong_pid.value.category is ipc.MonitorIPCErrorCategory.CHILD_FAILED
    zero_fields = ["S", *("0" for _ in range(20))]
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: __import__("io").StringIO("123 (name) " + " ".join(zero_fields)),
    )
    with pytest.raises(ipc.MonitorIPCError) as zero:
        ipc._read_start_ticks_for_spawn(123)
    assert zero.value.category is ipc.MonitorIPCErrorCategory.CHILD_FAILED


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_real_fresh_exec_reciprocal_handshake_and_typed_commands(tmp_path: Path) -> None:
    directory_fd = _directory(tmp_path / "run")
    handle: ipc.MonitorExecHandle | None = None
    try:
        handle = ipc.spawn_monitor_exec(directory_fd, _identity(), timeout=2)
        assert handle.endpoint.writer.pid == handle.pid
        described = handle.request(ipc.MonitorIPCOperation.DESCRIBE)
        assert (described.operation, described.state) == (ipc.MonitorIPCOperation.DESCRIBE, "committed")
        assert handle.request(ipc.MonitorIPCOperation.PING).state == "pong"
        restored = ipc.MonitorExecEndpoint.from_bytes(handle.endpoint.to_bytes())
        assert ipc.request_monitor(directory_fd, restored, ipc.MonitorIPCOperation.PING, timeout=2).state == "pong"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as malformed:
            malformed.settimeout(2)
            assert handle.endpoint.socket_name is not None
            malformed.connect(ipc._socket_address(directory_fd, handle.endpoint.socket_name))
            malformed.sendall(struct.pack(">I", ipc._MAX_FRAME_BYTES + 1))
        assert handle.request(ipc.MonitorIPCOperation.PING).state == "pong"
        handle.shutdown()
        assert not (tmp_path / "run" / ipc._socket_name_for_generation(_GENERATION)).exists()
    finally:
        if handle is not None and handle._process.poll() is None:
            handle._process.kill()
            handle._process.wait(timeout=2)
        if handle is not None:
            handle.close()
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_parent_abort_before_prepare_exits_and_removes_exact_socket(tmp_path: Path) -> None:
    directory_fd = _directory(tmp_path / "run")
    local, child = socket.socketpair()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "palimpsest_local.oci_monitor_ipc",
                "--private-child-v2",
                str(directory_fd),
                str(child.fileno()),
            ],
            close_fds=True,
            pass_fds=(directory_fd, child.fileno()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        )
        child.close()
        parent = ipc.current_process_identity()
        ipc._send_frame(
            local,
            {
                "binding": _identity().binding.to_dict(),
                "generation": _identity().generation,
                "nonce": "1" * 64,
                "parent": parent.to_dict(),
                "schema": ipc._CONFIG_SCHEMA,
                "timeout_ms": 100,
            },
        )
        assert ipc._recv_frame(local) == {"kind": "bound", "schema": ipc._SPAWN_SCHEMA}
        local.close()
        assert process.wait(timeout=2) == 70
        assert not (tmp_path / "run" / ipc._socket_name_for_generation(_GENERATION)).exists()
    finally:
        for channel in (local, child):
            try:
                channel.close()
            except OSError:
                pass
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_precommit_accept_timeout_is_stable_and_removes_socket(tmp_path: Path) -> None:
    directory_fd = _directory(tmp_path / "run")
    local, child = socket.socketpair()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "palimpsest_local.oci_monitor_ipc",
                "--private-child-v2",
                str(directory_fd),
                str(child.fileno()),
            ],
            close_fds=True,
            pass_fds=(directory_fd, child.fileno()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
        )
        child.close()
        ipc._send_frame(
            local,
            {
                "binding": _identity().binding.to_dict(),
                "generation": _identity().generation,
                "nonce": "1" * 64,
                "parent": ipc.current_process_identity().to_dict(),
                "schema": ipc._CONFIG_SCHEMA,
                "timeout_ms": 100,
            },
        )
        assert ipc._recv_frame(local) == {"kind": "bound", "schema": ipc._SPAWN_SCHEMA}
        error = ipc._recv_frame(local)
        assert error == {"category": "timeout", "kind": "error", "schema": ipc._SPAWN_SCHEMA}
        assert process.wait(timeout=2) == 70
        assert not (tmp_path / "run" / ipc._socket_name_for_generation(_GENERATION)).exists()
    finally:
        local.close()
        child.close()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_socket_collision_and_replacement_are_preserved_fail_closed(tmp_path: Path) -> None:
    collision_dir = tmp_path / "collision"
    collision_fd = _directory(collision_dir)
    collision = collision_dir / ipc._SOCKET_NAME
    collision.write_bytes(b"foreign")
    collision.chmod(0o600)
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc._BoundMonitorSocket(collision_fd)
        assert captured.value.category is ipc.MonitorIPCErrorCategory.SOCKET_COLLISION
        assert collision.read_bytes() == b"foreign"
    finally:
        os.close(collision_fd)

    replaced_dir = tmp_path / "replaced"
    replaced_fd = _directory(replaced_dir)
    bound = ipc._BoundMonitorSocket(replaced_fd)
    original = replaced_dir / "original.sock"
    public = replaced_dir / ipc._SOCKET_NAME
    public.rename(original)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(public))
    public.chmod(0o600)
    try:
        with pytest.raises(ipc.MonitorIPCError) as changed:
            bound.close()
        assert changed.value.category is ipc.MonitorIPCErrorCategory.SOCKET_CHANGED
        assert public.exists()
        assert original.exists()
    finally:
        replacement.close()
        os.close(replaced_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_crashed_child_leaves_stale_socket_and_next_spawn_refuses_collision(tmp_path: Path) -> None:
    directory_fd = _directory(tmp_path / "run")
    handle = ipc.spawn_monitor_exec(directory_fd, _identity(), timeout=2)
    try:
        socket_path = tmp_path / "run" / ipc._socket_name_for_generation(_GENERATION)
        assert socket_path.exists()
        os.kill(handle.pid, signal.SIGKILL)
        handle._process.wait(timeout=2)
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc.spawn_monitor_exec(directory_fd, _identity(generation=str(uuid.uuid4())), timeout=1)
        assert captured.value.category is ipc.MonitorIPCErrorCategory.CHILD_FAILED
        assert socket_path.exists()
    finally:
        handle.close()
        os.close(directory_fd)


def test_public_runtime_modules_do_not_import_monitor_ipc() -> None:
    package = Path(ipc.__file__).parent
    forbidden = ("__init__.py", "cli.py", "runtime.py", "runtime_dispatch.py", "oci_root_runtime.py", "kvm.py")
    importers: list[str] = []
    for name in forbidden:
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name.endswith("oci_monitor_ipc") for alias in node.names):
                importers.append(name)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("oci_monitor_ipc"):
                if name == "oci_root_runtime.py" and [alias.name for alias in node.names] == [
                    "MonitorPreActivationBinding",
                    "_PreactivationJournalLease",
                ]:
                    continue
                importers.append(name)
    assert importers == []


def test_wire_examples_contain_no_secret_mac_path_or_error_repr() -> None:
    identity = _identity()
    request = ipc._request_message(ipc.MonitorIPCOperation.DESCRIBE, identity, _process(), str(uuid.uuid4()))
    response = ipc._response_message(ipc.MonitorIPCOperation.DESCRIBE, identity, _process(), request["request_id"])
    spawn = ipc._spawn_message("prepared", identity, "1" * 64, _process())
    wire = json.dumps([request, response, spawn], sort_keys=True).lower()
    assert not any(term in wire for term in ("boot_key", "private_key", '"mac"', '"path"', "traceback", "repr"))
