"""Durable preactivation journal tests for the inert OCI monitor IPC."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import palimpsest_local.oci_monitor as monitor
import palimpsest_local.oci_monitor_ipc as ipc
from palimpsest_local.runtime_types import DispatchKey, ExistingRunRecord, RuntimeBackend, RuntimeKind

_RUN_ID = "11849d77-fdd8-4f65-92a0-bbc75ea80767"
_GENERATION = "21849d77-fdd8-4f65-92a0-bbc75ea80767"
_BOOT_ID = "31849d77-fdd8-4f65-92a0-bbc75ea80767"


def _binding() -> ipc.MonitorPreActivationBinding:
    return ipc.MonitorPreActivationBinding(
        ExistingRunRecord(
            "oci-demo",
            _RUN_ID,
            2,
            DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
        ),
        os.geteuid(),
        "sha256:" + "a" * 64,
        "sha256:" + "c" * 64,
        "sha256:" + "b" * 64,
        "51849d77-fdd8-4f65-92a0-bbc75ea80767",
        "41849d77-fdd8-4f65-92a0-bbc75ea80767",
        "qemu:///system",
    )


def _identity(generation: str = _GENERATION) -> ipc.MonitorExecIdentity:
    return ipc.MonitorExecIdentity(_binding(), generation)


def _writer() -> monitor.MonitorProcessIdentity:
    return monitor.MonitorProcessIdentity(os.getpid(), _BOOT_ID, 202)


def _active_binding() -> monitor.MonitorBinding:
    binding = _binding()
    return monitor.MonitorBinding(
        binding.record,
        binding.owner_uid,
        binding.plan_digest,
        binding.expected_definition_projection_digest,
        binding.stage1_artifact_digest,
        binding.domain_uuid,
        7,
        binding.boot_attempt_id,
        binding.libvirt_uri,
    )


def _directory(path: Path) -> int:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _linux_peercred() -> bool:
    return sys.platform == "linux" and hasattr(socket, "SO_PEERCRED") and hasattr(os, "O_PATH")


def _advance_lease(lease: ipc._PreactivationJournalLease, phase: str) -> None:
    if phase == "claiming":
        return
    lease.mark_prepared(12, 34)
    if phase == "prepared":
        return
    lease.mark_committed()
    if phase == "committed":
        return
    lease.mark_activating()
    if phase == "activating":
        return
    if phase == "activation-control-lost":
        lease.mark_control_lost()
        return
    lease.promote_active(_active_binding())
    if phase == "active":
        return
    if phase == "active-control-lost":
        lease.mark_control_lost()
        return
    lease.mark_ready()
    if phase != "ready":
        lease.mark_terminal()


def test_snapshot_cannot_observe_publication_before_in_memory_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, "committed")
    published, release, reading, finished = (threading.Event() for _ in range(4))
    original_fsync = os.fsync
    snapshots, failures = [], []

    def pause_after_publish(fd: int) -> None:
        original_fsync(fd)
        if fd == lease._directory_fd:
            published.set()
            assert release.wait(2)

    def transition() -> None:
        try:
            lease.mark_activating()
        except BaseException as exc:
            failures.append(exc)

    def read() -> None:
        reading.set()
        try:
            snapshots.append(lease.snapshot)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(ipc.os, "fsync", pause_after_publish)
    writer, reader = threading.Thread(target=transition), threading.Thread(target=read)
    try:
        writer.start()
        assert published.wait(2)
        reader.start()
        assert reading.wait(2)
        assert not finished.wait(0.05)
        release.set()
        writer.join(2)
        reader.join(2)
        assert not failures
        assert len(snapshots) == 1 and snapshots[0].phase == "activating"
        assert not lease._poisoned
    finally:
        release.set()
        writer.join(2)
        if reader.ident is not None:
            reader.join(2)
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize(
    "phase,done,expected",
    [
        ("committed", False, "launch-pending"),
        ("committed", True, "launch-failed"),
        ("activating", False, "activating"),
        ("active", False, "active"),
        ("ready", False, "ready"),
        ("terminal", False, "terminal"),
        ("terminal", True, "terminal"),
    ],
)
def test_ipc_worker_describe_and_transport_shutdown_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    done: bool,
    expected: str,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, phase)
    completion = threading.Event()
    if done:
        completion.set()
    worker = SimpleNamespace(done=completion)
    operations = iter(
        [ipc.MonitorIPCOperation.DESCRIBE, ipc.MonitorIPCOperation.PING, ipc.MonitorIPCOperation.SHUTDOWN]
    )
    replies = []

    class Channel:
        def settimeout(self, _timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class Listener:
        def settimeout(self, _timeout):
            pass

        def accept(self):
            if len(replies) == 3:
                raise OSError("end test service")
            return Channel(), None

    bound = SimpleNamespace(validate=lambda: None, listener=Listener())
    monkeypatch.setattr(ipc, "_recv_frame", lambda _channel: {})
    monkeypatch.setattr(ipc, "_decode_request", lambda *_args: (next(operations), str(uuid.uuid4()), _writer()))
    monkeypatch.setattr(ipc, "_authorize_peer", lambda *_args: None)
    monkeypatch.setattr(ipc, "_send_frame", lambda _channel, value: replies.append(value))
    try:
        allowed = done and phase in {"committed", "terminal"}
        if allowed:
            assert ipc._serve_committed(bound, _identity(), _writer(), 1, lease, worker) is (phase == "terminal")
        else:
            with pytest.raises(ipc.MonitorIPCError):
                ipc._serve_committed(bound, _identity(), _writer(), 1, lease, worker)
        assert [reply["state"] for reply in replies] == [
            expected,
            "pong",
            "shutting-down" if allowed else "shutdown-refused",
        ]
        assert lease.snapshot.phase == ("aborting" if done and phase == "committed" else phase)
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize("phase", ["committed", "activating", "active", "ready", "terminal"])
def test_discovery_accepts_only_same_owner_monotonic_activation_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, phase)
    endpoint = lease.snapshot.endpoint

    def request(_fd, _endpoint, operation, **_kwargs):
        for previous, transition in [
            ("committed", lease.mark_activating),
            ("activating", lambda: lease.promote_active(_active_binding())),
            ("active", lease.mark_ready),
            ("ready", lease.mark_terminal),
        ]:
            if lease.snapshot.phase == previous:
                transition()
        return ipc.MonitorIPCReply(operation, "terminal", _writer())

    monkeypatch.setattr(ipc, "request_monitor", request)
    try:
        assert (
            ipc.discover_monitor_exec(directory_fd, _binding(), liveness_probe=lambda _: monitor.ProcessLiveness.LIVE)
            == endpoint
        )
        assert lease.snapshot.phase == "terminal"
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize("change", ["nonce", "revision", "socket", "state"])
def test_discovery_does_not_ignore_drift_while_allowing_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, "committed")
    before = lease.snapshot
    _advance = lease.mark_activating()
    del _advance
    after = lease.snapshot
    if change == "nonce":
        after = replace(after, nonce_digest="sha256:" + "f" * 64)
    elif change == "revision":
        after = replace(after, revision=after.revision + 1)
    elif change == "socket":
        after = replace(after, socket_inode=after.socket_inode + 1)
    values = iter(
        [
            (before, ipc._canonical_bytes(before.to_dict()) + b"\n"),
            (after, ipc._canonical_bytes(after.to_dict()) + b"\n"),
        ]
    )
    monkeypatch.setattr(ipc, "_read_preactivation_journal", lambda *_args: next(values))
    monkeypatch.setattr(
        ipc,
        "request_monitor",
        lambda *_args, **_kwargs: ipc.MonitorIPCReply(
            ipc.MonitorIPCOperation.DESCRIBE, "terminal" if change == "state" else "activating", _writer()
        ),
    )
    try:
        with pytest.raises(ipc.MonitorIPCError) as failure:
            ipc.discover_monitor_exec(directory_fd, _binding(), liveness_probe=lambda _: monitor.ProcessLiveness.LIVE)
        assert failure.value.category is ipc.MonitorIPCErrorCategory.INVALID_JOURNAL
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize("fence", ["valid", "wrong", "closed", "lost-accepted-ack"])
def test_child_requires_post_committed_activation_fence_and_keeps_lost_ack_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fence: str,
) -> None:
    from palimpsest_local.oci_monitor_launch import MonitorLaunchAuthority

    directory_fd = _directory(tmp_path / "run")
    identity, nonce = _identity(), "1" * 64
    parent = monitor.MonitorProcessIdentity(os.getpid() + 10000, _BOOT_ID, 101)
    events = []
    config = {
        "binding": identity.binding.to_dict(),
        "generation": identity.generation,
        "nonce": nonce,
        "parent": parent.to_dict(),
        "schema": ipc._CONFIG_SCHEMA,
        "timeout_ms": 100,
        "launch_authority": {"private": "test"},
    }
    frames = iter(
        [
            config,
            ipc._spawn_message("prepare", identity, nonce, parent),
            ipc._spawn_message("commit", identity, nonce, parent),
            ipc._spawn_message("activate" if fence != "wrong" else "commit", identity, nonce, parent, (12, 34)),
        ]
    )

    class Channel:
        def settimeout(self, _timeout):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class Authority:
        def validate(self, **_kwargs):
            events.append("validate")

        def close(self):
            events.append("close-authority")

    authority = Authority()

    class Bound:
        identity = (12, 34)

        def __init__(self, *_args):
            pass

        def validate(self):
            pass

        def unlink_exact_and_fsync(self):
            events.append("unlink")

        def close(self, **_kwargs):
            pass

    class Worker:
        def __init__(self, received, _fd, _binding, lease):
            assert received is authority
            self.lease = lease

        def start(self):
            assert events[-2:] == ["authorize", "validate"]
            assert self.lease.snapshot.phase == "committed"
            events.append("worker-start")
            self.lease.mark_activating()
            self.lease.promote_active(_active_binding())
            self.lease.mark_ready()
            self.lease.mark_terminal()

        def join(self):
            pass

    def receive(_channel):
        value = next(frames)
        if "committed" in events and value.get("kind") == "activate" and fence == "closed":
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.CLOSED)
        return value

    def send(_channel, message):
        events.append(message["kind"])
        if message["kind"] == "committed":
            loaded = ipc._read_preactivation_journal(directory_fd, identity)
            assert loaded is not None and loaded[0].phase == "committed"
            assert "worker-start" not in events
        if message["kind"] == "launch-accepted" and fence == "lost-accepted-ack":
            raise ipc.MonitorIPCError(ipc.MonitorIPCErrorCategory.CLOSED)

    def serve(_bound, _identity, _writer, _timeout, lease, worker):
        events.append("serve")
        if worker is None:
            assert lease.snapshot.phase == "committed"
            lease.mark_aborting()
            return False
        assert lease.snapshot.phase == "terminal"
        return True

    monkeypatch.setattr(MonitorLaunchAuthority, "from_dict", classmethod(lambda _cls, value, **kwargs: authority))
    monkeypatch.setattr(ipc.socket, "socket", lambda **_kwargs: Channel())
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    monkeypatch.setattr(ipc, "_recv_frame", receive)
    monkeypatch.setattr(ipc, "_send_frame", send)
    monkeypatch.setattr(ipc, "_accept_precommit", lambda *_args: Channel())
    monkeypatch.setattr(ipc, "_BoundMonitorSocket", Bound)
    monkeypatch.setattr(ipc, "_authorize_peer", lambda *_args: events.append("authorize"))
    monkeypatch.setattr(ipc, "_LaunchWorker", Worker)
    monkeypatch.setattr(ipc, "_serve_committed", serve)
    try:
        assert ipc._child_main(directory_fd, 123) == 0
        launched = fence in {"valid", "lost-accepted-ack"}
        assert ("worker-start" in events) is launched
        loaded = ipc._read_preactivation_journal(directory_fd, identity)
        assert loaded is not None and loaded[0].phase == ("terminal" if launched else "abandoned")
        assert events.index("committed") < events.index("serve")
        if launched:
            assert events.index("worker-start") < events.index("launch-accepted") < events.index("serve")
    finally:
        os.close(directory_fd)


def test_same_v2_lease_promotes_and_preserves_active_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    try:
        _advance_lease(lease, "committed")
        lease.validate_launch_binding(_binding(), directory_fd=directory_fd)
        assert lease.mark_activating().active_binding is None
        lease.validate_launch_binding(_binding(), directory_fd=directory_fd, activating=True)
        active = lease.promote_active(_active_binding())
        assert (active.phase, active.revision, active.active_binding) == ("active", 5, _active_binding())
        for transition, phase in (
            (lease.mark_ready, "ready"),
            (lease.mark_terminal, "terminal"),
            (lease.mark_control_lost, "control-lost"),
        ):
            snapshot = transition()
            assert snapshot.phase == phase
            assert snapshot.active_binding == _active_binding()
            assert snapshot.identity == _identity()
            assert snapshot.writer == _writer()
            loaded = ipc._read_preactivation_journal(directory_fd, _identity())
            assert loaded is not None and loaded[0] == snapshot
            assert json.loads(loaded[1])["active_binding"] == _active_binding().to_dict()
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize("phase", ["claiming", "prepared", "committed", "activating", "active", "ready", "terminal"])
def test_activation_transition_graph_refuses_skips_and_inert_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    try:
        _advance_lease(lease, phase)
        before = (directory / monitor._JOURNAL_NAME).read_bytes()
        transitions = [
            (lease.mark_activating, "committed"),
            (lambda: lease.promote_active(_active_binding()), "activating"),
            (lease.mark_ready, "active"),
            (lease.mark_terminal, "ready"),
        ]
        if phase in ipc._ACTIVATION_PHASES:
            transitions += [(lease.mark_aborting, "never"), (lease.mark_abandoned, "never")]
        for transition, allowed in transitions:
            if phase == allowed:
                continue
            with pytest.raises(ipc.MonitorIPCError) as error:
                transition()
            assert error.value.category is ipc.MonitorIPCErrorCategory.INVALID_TRANSITION
            assert (directory / monitor._JOURNAL_NAME).read_bytes() == before
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_uid", float(os.geteuid())),
        ("owner_uid", True),
        ("domain_id", 7.0),
        ("domain_id", True),
        ("domain_id", 0),
        ("domain_id", 2**31),
        ("plan_digest", "sha256:" + "f" * 64),
        ("definition_projection_digest", "sha256:" + "f" * 64),
        ("stage1_artifact_digest", "sha256:" + "f" * 64),
        ("domain_uuid", str(uuid.uuid4())),
        ("boot_attempt_id", str(uuid.uuid4())),
        ("run_id", str(uuid.uuid4())),
        ("name", "another-run"),
        ("backend", "qemu"),
        ("runtime_kind", "container"),
        ("libvirt_uri", "qemu:///session"),
        ("lifecycle_protocol", "other"),
        ("unexpected", "field"),
    ],
)
def test_active_journal_rejects_noncanonical_or_mismatched_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, "active")
    lease.close()
    journal = directory / monitor._JOURNAL_NAME
    raw = json.loads(journal.read_bytes())
    raw["active_binding"][field] = value
    content = ipc._canonical_bytes(raw) + b"\n"
    journal.write_bytes(content)
    try:
        with pytest.raises(ipc.MonitorIPCError) as error:
            ipc._read_preactivation_journal(directory_fd, _binding())
        assert error.value.category is ipc.MonitorIPCErrorCategory.INVALID_JOURNAL
        assert journal.read_bytes() == content
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize(
    ("phase", "has_binding"),
    [
        ("active", False),
        ("ready", False),
        ("terminal", False),
        ("claiming", True),
        ("prepared", True),
        ("committed", True),
        ("activating", True),
        ("aborting", True),
        ("adopting", True),
        ("abandoned", True),
    ],
)
def test_journal_phase_and_active_binding_must_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    has_binding: bool,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    try:
        with pytest.raises(ipc.MonitorIPCError):
            replace(
                lease.snapshot,
                phase=phase,
                socket_device=12,
                socket_inode=34,
                active_binding=_active_binding() if has_binding else None,
            )
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.parametrize(
    "phase", ["activating", "active", "ready", "terminal", "activation-control-lost", "active-control-lost"]
)
def test_stale_activation_is_never_adopted_rearmed_or_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, phase)
    snapshot = lease.snapshot
    lease.close()
    journal = directory / monitor._JOURNAL_NAME
    before = journal.read_bytes()
    socket_path = directory / snapshot.socket_name
    socket_path.write_bytes(b"activation evidence")
    monkeypatch.setattr(ipc, "request_monitor", lambda *_args, **_kwargs: pytest.fail("must not send shutdown"))
    monkeypatch.setattr(ipc, "_cleanup_stale_socket", lambda *_args: pytest.fail("must not clean activation"))
    try:
        operations = [
            lambda: ipc.reconcile_stale_monitor_exec(
                directory_fd, _binding(), liveness_probe=lambda _writer: monitor.ProcessLiveness.STALE
            ),
            lambda: ipc._PreactivationJournalLease.adopt_stale(
                directory_fd, _identity(), snapshot, before, _writer(), lambda _writer: monitor.ProcessLiveness.STALE
            ),
            lambda: ipc._PreactivationJournalLease.create(
                directory_fd, _identity(str(uuid.uuid4())), "2" * 64, _writer()
            ),
            lambda: ipc.shutdown_monitor_exec(directory_fd, snapshot.endpoint),
        ]
        for operation in operations:
            with pytest.raises(ipc.MonitorIPCError):
                operation()
            assert journal.read_bytes() == before
            assert socket_path.read_bytes() == b"activation evidence"
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("activation", [False, True])
@pytest.mark.parametrize("failure", ["file-fsync", "directory-fsync"])
def test_activation_publish_failure_poisons_lease_without_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activation: bool,
    failure: str,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    _advance_lease(lease, "activating" if activation else "committed")
    before = (directory / monitor._JOURNAL_NAME).read_bytes()
    original_fsync = os.fsync

    def fail_fsync(descriptor: int) -> None:
        is_directory = descriptor == lease._directory_fd
        if is_directory == (failure == "directory-fsync"):
            raise OSError("injected fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(ipc.os, "fsync", fail_fsync)
    try:
        with pytest.raises(ipc.MonitorIPCError) as error:
            lease.promote_active(_active_binding()) if activation else lease.mark_activating()
        assert error.value.category is ipc.MonitorIPCErrorCategory.JOURNAL_IO
        after = (directory / monitor._JOURNAL_NAME).read_bytes()
        if failure == "file-fsync":
            assert after == before
        else:
            assert json.loads(after)["phase"] == ("active" if activation else "activating")
        for operation in (lease.mark_aborting, lease.mark_abandoned, lease.mark_control_lost):
            with pytest.raises(ipc.MonitorIPCError) as poisoned:
                operation()
            assert poisoned.value.category is ipc.MonitorIPCErrorCategory.POISONED
            assert (directory / monitor._JOURNAL_NAME).read_bytes() == after
    finally:
        lease.close()
        os.close(directory_fd)


def test_launch_lease_binding_directory_phase_and_writer_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    wrong_fd = _directory(tmp_path / "wrong")
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    try:
        _advance_lease(lease, "committed")
        lease.validate_launch_binding(_binding(), directory_fd=directory_fd)
        for binding, fd, activating in [
            (_binding(), wrong_fd, False),
            (replace(_binding(), boot_attempt_id=str(uuid.uuid4())), directory_fd, False),
            (_binding(), directory_fd, 1),
            (_binding(), directory_fd, True),
        ]:
            with pytest.raises(ipc.MonitorIPCError):
                lease.validate_launch_binding(binding, directory_fd=fd, activating=activating)
        forged = _binding()
        object.__setattr__(forged, "owner_uid", float(os.geteuid()))
        with pytest.raises(ipc.MonitorIPCError):
            lease.validate_launch_binding(forged, directory_fd=directory_fd)
        lease.mark_activating()
        lease.validate_launch_binding(_binding(), directory_fd=directory_fd, activating=True)
        monkeypatch.setattr(ipc, "current_process_identity", lambda: replace(_writer(), start_ticks=999))
        with pytest.raises(ipc.MonitorIPCError) as error:
            lease.validate_directory_binding(directory_fd)
        assert error.value.category is ipc.MonitorIPCErrorCategory.UNAUTHORIZED_PEER
        with pytest.raises(ipc.MonitorIPCError) as error:
            lease.mark_control_lost()
        assert error.value.category is ipc.MonitorIPCErrorCategory.POISONED
    finally:
        lease.close()
        os.close(directory_fd)
        os.close(wrong_fd)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_uid", float(os.geteuid())),
        ("domain_id", True),
        ("domain_id", 7.0),
        ("definition_projection_digest", "sha256:" + "f" * 64),
    ],
)
def test_promote_active_revalidates_forged_binding_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    try:
        _advance_lease(lease, "activating")
        before = (directory / monitor._JOURNAL_NAME).read_bytes()
        binding = _active_binding()
        object.__setattr__(binding, field, value)
        with pytest.raises(ipc.MonitorIPCError):
            lease.promote_active(binding)
        assert lease.snapshot.phase == "activating"
        assert (directory / monitor._JOURNAL_NAME).read_bytes() == before
    finally:
        lease.close()
        os.close(directory_fd)


def test_active_lease_journal_tamper_poisons_authority_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    try:
        _advance_lease(lease, "active")
        journal = directory / monitor._JOURNAL_NAME
        raw = json.loads(journal.read_bytes())
        raw["active_binding"]["domain_id"] += 1
        tampered = ipc._canonical_bytes(raw) + b"\n"
        journal.write_bytes(tampered)
        with pytest.raises(ipc.MonitorIPCError) as error:
            lease.validate_directory_binding(directory_fd)
        assert error.value.category is ipc.MonitorIPCErrorCategory.INVALID_JOURNAL
        with pytest.raises(ipc.MonitorIPCError) as error:
            lease.mark_ready()
        assert error.value.category is ipc.MonitorIPCErrorCategory.POISONED
        assert journal.read_bytes() == tampered
    finally:
        lease.close()
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux O_PATH sockets")
@pytest.mark.parametrize("promoted", [False, True])
def test_stale_activation_keeps_real_socket_inode_and_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    promoted: bool,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    monkeypatch.setattr(ipc, "current_process_identity", _writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, _writer())
    bound = ipc._BoundMonitorSocket(directory_fd, lease.snapshot.socket_name)
    lease.mark_prepared(*bound.identity)
    lease.mark_committed()
    lease.mark_activating()
    if promoted:
        lease.promote_active(_active_binding())
    snapshot = lease.snapshot
    before = (directory / monitor._JOURNAL_NAME).read_bytes()
    socket_path = directory / snapshot.socket_name
    inode = socket_path.stat().st_ino
    bound.close(preserve_path=True)
    lease.close()
    try:
        with pytest.raises(ipc.MonitorIPCError) as error:
            ipc.reconcile_stale_monitor_exec(
                directory_fd, _binding(), liveness_probe=lambda _writer: monitor.ProcessLiveness.STALE
            )
        assert error.value.category is ipc.MonitorIPCErrorCategory.CONTROL_LOST
        assert socket_path.stat().st_ino == inode
        assert (directory / monitor._JOURNAL_NAME).read_bytes() == before
    finally:
        socket_path.unlink()
        os.close(directory_fd)


def test_v2_journal_is_canonical_path_free_and_nonce_digest_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    writer = _writer()
    monkeypatch.setattr(ipc, "current_process_identity", lambda: writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, writer)
    try:
        snapshot = lease.snapshot
        assert (snapshot.phase, snapshot.revision) == ("claiming", 1)
        assert snapshot.socket_name == ipc._socket_name_for_generation(_GENERATION)
        raw = json.loads((directory / monitor._JOURNAL_NAME).read_bytes())
        assert raw["schema"] == ipc._PREACTIVATION_JOURNAL_SCHEMA
        assert raw["active_binding"] is None
        assert raw["nonce_digest"] == ipc._nonce_digest("1" * 64)
        assert "1" * 64 not in (directory / monitor._JOURNAL_NAME).read_text()
        loaded = ipc._read_preactivation_journal(directory_fd, _binding())
        assert loaded is not None and loaded[0] == snapshot
        prepared = lease.mark_prepared(12, 34)
        assert (prepared.phase, prepared.revision, prepared.socket_device, prepared.socket_inode) == (
            "prepared",
            2,
            12,
            34,
        )
        assert lease.mark_committed().phase == "committed"
    finally:
        lease.close()
        os.close(directory_fd)


def test_v2_journal_normalizes_unhashable_phase_to_invalid_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    writer = _writer()
    monkeypatch.setattr(ipc, "current_process_identity", lambda: writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, writer)
    lease.close()
    journal = directory / monitor._JOURNAL_NAME
    raw = json.loads(journal.read_bytes())
    raw["phase"] = []
    journal.write_bytes(ipc._canonical_bytes(raw) + b"\n")
    journal.chmod(0o600)
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc._read_preactivation_journal(directory_fd, _binding())
        assert captured.value.category is ipc.MonitorIPCErrorCategory.INVALID_JOURNAL
    finally:
        os.close(directory_fd)


def test_v1_and_v2_use_the_same_exclusive_lock_and_journal(tmp_path: Path) -> None:
    first_fd = _directory(tmp_path / "v2-first")
    v2 = ipc._PreactivationJournalLease.create(first_fd, _identity(), "1" * 64, _writer())
    try:
        with pytest.raises(monitor.MonitorError) as busy_v1:
            monitor.MonitorLease.create(first_fd, _active_binding(), current_process=_writer)
        assert busy_v1.value.category is monitor.MonitorErrorCategory.JOURNAL_BUSY
    finally:
        v2.close()
        os.close(first_fd)

    second_fd = _directory(tmp_path / "v1-first")
    v1 = monitor.MonitorLease.create(second_fd, _active_binding(), current_process=_writer)
    try:
        with pytest.raises(ipc.MonitorIPCError) as busy_v2:
            ipc._PreactivationJournalLease.create(
                second_fd,
                _identity(),
                "1" * 64,
                _writer(),
            )
        assert busy_v2.value.category is ipc.MonitorIPCErrorCategory.JOURNAL_BUSY
    finally:
        v1.close()
        os.close(second_fd)


def test_abandoned_same_binding_rearms_new_generation_under_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = _directory(tmp_path / "run")
    writer = _writer()
    monkeypatch.setattr(ipc, "current_process_identity", lambda: writer)
    first = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, writer)
    first.mark_aborting()
    abandoned = first.mark_abandoned()
    first.close()
    next_identity = _identity(str(uuid.uuid4()))
    second = ipc._PreactivationJournalLease.create(directory_fd, next_identity, "2" * 64, writer)
    try:
        assert second.snapshot.phase == "claiming"
        assert second.snapshot.revision == abandoned.revision + 1
        assert second.snapshot.identity == next_identity
    finally:
        second.close()
        os.close(directory_fd)


def test_abandoned_with_unexpected_socket_is_not_false_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    writer = _writer()
    monkeypatch.setattr(ipc, "current_process_identity", lambda: writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, writer)
    lease.mark_aborting()
    lease.mark_abandoned()
    lease.close()
    unexpected = directory / ipc._socket_name_for_generation(_GENERATION)
    unexpected.write_bytes(b"preserve")
    unexpected.chmod(0o600)
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc.reconcile_stale_monitor_exec(directory_fd, _binding())
        assert captured.value.category is ipc.MonitorIPCErrorCategory.SOCKET_CHANGED
        assert unexpected.read_bytes() == b"preserve"
    finally:
        os.close(directory_fd)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux renameat2")
def test_quarantine_rename_is_atomic_no_clobber(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    source = directory / "source"
    destination = directory / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    try:
        with pytest.raises(FileExistsError):
            ipc._rename_noreplace(directory_fd, source.name, destination.name)
        assert source.read_bytes() == b"source"
        assert destination.read_bytes() == b"destination"
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize(
    ("liveness", "category"),
    [
        (monitor.ProcessLiveness.LIVE, ipc.MonitorIPCErrorCategory.WRITER_LIVE),
        (monitor.ProcessLiveness.UNKNOWN, ipc.MonitorIPCErrorCategory.WRITER_UNKNOWN),
    ],
)
def test_reconcile_live_or_unknown_writer_never_mutates(
    tmp_path: Path,
    liveness: monitor.ProcessLiveness,
    category: ipc.MonitorIPCErrorCategory,
) -> None:
    directory = tmp_path / liveness.value
    directory_fd = _directory(directory)
    lease = ipc._PreactivationJournalLease.create(
        directory_fd,
        _identity(),
        "1" * 64,
        _writer(),
    )
    lease.close()
    before = (directory / monitor._JOURNAL_NAME).read_bytes()
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc.reconcile_stale_monitor_exec(
                directory_fd,
                _binding(),
                liveness_probe=lambda _writer: liveness,
            )
        assert captured.value.category is category
        assert (directory / monitor._JOURNAL_NAME).read_bytes() == before
    finally:
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux O_PATH sockets")
def test_stale_recorded_socket_is_quarantined_and_abandoned(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    handle = ipc.spawn_monitor_exec(directory_fd, _identity(), timeout=2)
    try:
        handle._process.kill()
        assert handle._process.wait(timeout=2) == -signal.SIGKILL
        assert monitor.probe_process_liveness(handle.endpoint.writer) is monitor.ProcessLiveness.STALE
        reconciled = ipc.reconcile_stale_monitor_exec(directory_fd, _binding())
        assert reconciled.phase == "abandoned"
        assert reconciled.writer == ipc.current_process_identity()
        assert not (directory / reconciled.socket_name).exists()
    finally:
        if handle._process.poll() is None:
            handle._process.kill()
            handle._process.wait(timeout=2)
        handle.close()
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux O_PATH sockets")
def test_stale_reconcile_finishes_interrupted_deterministic_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    stale_writer = _writer()
    adopter = monitor.MonitorProcessIdentity(os.getpid(), "61849d77-fdd8-4f65-92a0-bbc75ea80767", 303)
    monkeypatch.setattr(ipc, "current_process_identity", lambda: stale_writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, stale_writer)
    bound = ipc._BoundMonitorSocket(directory_fd, lease.snapshot.socket_name)
    committed = lease.mark_prepared(*bound.identity)
    committed = lease.mark_committed()
    bound.close(preserve_path=True)
    lease.close()
    expected = (committed.socket_device, committed.socket_inode)
    assert all(value is not None for value in expected)
    quarantine = ipc._socket_quarantine_name(committed.socket_name, expected)  # type: ignore[arg-type]
    os.rename(committed.socket_name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    monkeypatch.setattr(ipc, "current_process_identity", lambda: adopter)
    try:
        reconciled = ipc.reconcile_stale_monitor_exec(
            directory_fd,
            _binding(),
            current_process=lambda: adopter,
            liveness_probe=lambda _writer: monitor.ProcessLiveness.STALE,
        )
        assert reconciled.phase == "abandoned"
        assert not (directory / quarantine).exists()
        assert not (directory / committed.socket_name).exists()
    finally:
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux O_PATH sockets")
def test_graceful_cleanup_preserves_preexisting_deterministic_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    writer = _writer()
    monkeypatch.setattr(ipc, "current_process_identity", lambda: writer)
    lease = ipc._PreactivationJournalLease.create(directory_fd, _identity(), "1" * 64, writer)
    bound = ipc._BoundMonitorSocket(directory_fd, lease.snapshot.socket_name)
    lease.mark_prepared(*bound.identity)
    lease.mark_committed()
    quarantine = ipc._socket_quarantine_name(bound._socket_name, bound.identity)
    foreign = directory / quarantine
    foreign.write_bytes(b"preserve")
    foreign.chmod(0o600)
    try:
        with pytest.raises(ipc.MonitorIPCError) as captured:
            bound.unlink_exact_and_fsync()
        assert captured.value.category is ipc.MonitorIPCErrorCategory.SOCKET_CHANGED
        assert foreign.read_bytes() == b"preserve"
        assert (directory / bound._socket_name).exists()
    finally:
        bound.close()
        lease.close()
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux O_PATH sockets")
def test_claiming_socket_without_durable_inode_is_preserved_control_lost(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    local, child = socket.socketpair()
    local.settimeout(2)
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
    )
    child.close()
    socket_path = directory / ipc._socket_name_for_generation(_GENERATION)
    try:
        ipc._send_frame(
            local,
            {
                "binding": _binding().to_dict(),
                "generation": _GENERATION,
                "nonce": "1" * 64,
                "parent": ipc.current_process_identity().to_dict(),
                "schema": ipc._CONFIG_SCHEMA,
                "timeout_ms": 5000,
            },
        )
        assert ipc._recv_frame(local) == {"kind": "bound", "schema": ipc._SPAWN_SCHEMA}
        claiming = ipc._read_preactivation_journal(directory_fd, _binding())
        assert claiming is not None and claiming[0].phase == "claiming"
        assert claiming[0].socket_inode is None
        process.kill()
        assert process.wait(timeout=2) == -signal.SIGKILL
        with pytest.raises(ipc.MonitorIPCError) as captured:
            ipc.reconcile_stale_monitor_exec(directory_fd, _binding())
        assert captured.value.category is ipc.MonitorIPCErrorCategory.CONTROL_LOST
        assert socket_path.exists()
        loaded = ipc._read_preactivation_journal(directory_fd, _binding())
        assert loaded is not None and loaded[0].phase == "control-lost"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        local.close()
        socket_path.unlink(missing_ok=True)
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred() or not hasattr(os, "fork"), reason="requires Linux fork and O_PATH")
def test_fork_child_drops_inherited_journal_listener_and_run_directory_fds(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    lease = ipc._PreactivationJournalLease.create(
        directory_fd,
        _identity(),
        "1" * 64,
        ipc.current_process_identity(),
    )
    bound = ipc._BoundMonitorSocket(directory_fd, lease.snapshot.socket_name)
    child = os.fork()
    if child == 0:
        try:
            _ = lease.snapshot
        except ipc.MonitorIPCError as failure:
            lease_closed = failure.category in {
                ipc.MonitorIPCErrorCategory.CLOSED,
                ipc.MonitorIPCErrorCategory.POISONED,
            }
        else:
            lease_closed = False
        try:
            bound.validate()
        except ipc.MonitorIPCError as failure:
            socket_closed = failure.category is ipc.MonitorIPCErrorCategory.POISONED
        else:
            socket_closed = False
        os._exit(0 if lease_closed and socket_closed and lease._directory_fd == bound._directory_fd == -1 else 71)
    try:
        _pid, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert lease.snapshot.phase == "claiming"
        bound.validate()
    finally:
        bound.close()
        lease.close()
        os.close(directory_fd)


def test_legacy_v1_endpoint_receipt_remains_exactly_decodable() -> None:
    identity = _identity()
    writer = _writer()
    payload = (
        ipc._canonical_bytes(
            {
                "binding": identity.binding.to_dict(),
                "binding_digest": identity.binding_digest,
                "generation": identity.generation,
                "schema": ipc._RECEIPT_SCHEMA_V1,
                "socket": {"device": 12, "inode": 34},
                "writer": writer.to_dict(),
            }
        )
        + b"\n"
    )
    endpoint = ipc.MonitorExecEndpoint.from_bytes(payload)
    assert endpoint.socket_name == ipc._SOCKET_NAME
    assert endpoint.receipt_schema == ipc._RECEIPT_SCHEMA_V1
    assert endpoint.to_bytes() == payload


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_live_spawn_publishes_committed_discovery_and_graceful_abandonment(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    handle: ipc.MonitorExecHandle | None = None
    try:
        handle = ipc.spawn_monitor_exec(directory_fd, _identity(), timeout=2)
        loaded = ipc._read_preactivation_journal(directory_fd, _binding())
        assert loaded is not None
        assert loaded[0].phase == "committed"
        assert loaded[0].endpoint == handle.endpoint
        assert ipc.discover_monitor_exec(directory_fd, _binding(), timeout=2) == handle.endpoint
        endpoint = handle.endpoint
        abandoned = ipc.shutdown_monitor_exec(directory_fd, endpoint, timeout=2)
        assert abandoned.phase == "abandoned"
        assert handle._process.wait(timeout=2) == 0
        handle.close()
        handle = None
        next_identity = _identity(str(uuid.uuid4()))
        next_handle = ipc.spawn_monitor_exec(directory_fd, next_identity, timeout=2)
        try:
            rearmed = ipc._read_preactivation_journal(directory_fd, next_identity)
            assert rearmed is not None
            assert rearmed[0].phase == "committed"
            assert rearmed[0].revision == abandoned.revision + 3
            next_handle.shutdown()
        finally:
            if next_handle._process.poll() is None:
                next_handle._process.kill()
                next_handle._process.wait(timeout=2)
            next_handle.close()
    finally:
        if handle is not None and handle._process.poll() is None:
            os.kill(handle.pid, signal.SIGKILL)
            handle._process.wait(timeout=2)
        if handle is not None:
            handle.close()
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_parent_exit_after_commit_is_discoverable_from_binding_only(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    generation = str(uuid.uuid4())
    program = """
import json
import os
import sys
from palimpsest_local.oci_monitor_ipc import (
    MonitorExecIdentity,
    MonitorPreActivationBinding,
    spawn_monitor_exec,
)
directory_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
binding = MonitorPreActivationBinding.from_dict(json.loads(sys.argv[2]))
spawn_monitor_exec(directory_fd, MonitorExecIdentity(binding, sys.argv[3]), timeout=2)
os._exit(0)
"""
    helper = subprocess.Popen(
        [sys.executable, "-c", program, str(directory), json.dumps(_binding().to_dict()), generation],
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint: ipc.MonitorExecEndpoint | None = None
    try:
        assert helper.wait(timeout=5) == 0
        endpoint = ipc.discover_monitor_exec(directory_fd, _binding(), timeout=2)
        assert endpoint.identity.generation == generation
        assert ipc.request_monitor(directory_fd, endpoint, ipc.MonitorIPCOperation.PING, timeout=2).state == "pong"
        assert ipc.shutdown_monitor_exec(directory_fd, endpoint, timeout=2).phase == "abandoned"
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=2)
        if endpoint is not None:
            try:
                os.kill(endpoint.writer.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(directory_fd)


@pytest.mark.skipif(not _linux_peercred(), reason="requires Linux /proc and SO_PEERCRED")
def test_committed_ack_control_path_loss_keeps_child_discoverable(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    directory_fd = _directory(directory)
    local, child = socket.socketpair()
    process: subprocess.Popen[bytes] | None = None
    endpoint: ipc.MonitorExecEndpoint | None = None
    nonce = "3" * 64
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
        )
        child.close()
        parent = ipc.current_process_identity()
        identity = _identity()
        ipc._send_frame(
            local,
            {
                "binding": identity.binding.to_dict(),
                "generation": identity.generation,
                "nonce": nonce,
                "parent": parent.to_dict(),
                "schema": ipc._CONFIG_SCHEMA,
                "timeout_ms": 2000,
            },
        )
        assert ipc._recv_frame(local) == {"kind": "bound", "schema": ipc._SPAWN_SCHEMA}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(2)
            channel.connect(ipc._socket_address(directory_fd, ipc._socket_name_for_generation(_GENERATION)))
            ipc._send_frame(channel, ipc._spawn_message("prepare", identity, nonce, parent))
            prepared = ipc._recv_frame(channel)
            writer = ipc._process_from_dict(prepared["writer"])
            socket_value = prepared["socket"]
            socket_identity = (socket_value["device"], socket_value["inode"])
            ipc._validate_spawn_message(prepared, "prepared", identity, nonce, writer, socket_identity)
            ipc._send_frame(channel, ipc._spawn_message("commit", identity, nonce, parent))
            channel.shutdown(socket.SHUT_RDWR)
        deadline = time.monotonic() + 2
        while True:
            try:
                endpoint = ipc.discover_monitor_exec(directory_fd, _binding(), timeout=0.2)
                break
            except ipc.MonitorIPCError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        assert endpoint.writer == writer
        assert ipc.shutdown_monitor_exec(directory_fd, endpoint, timeout=2).phase == "abandoned"
        assert process.wait(timeout=2) == 0
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
