"""Durable preactivation journal tests for the inert OCI monitor IPC."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

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
