"""Inactive OCI supervisor durability and session contracts."""

from __future__ import annotations

import ast
import os
import threading
import uuid
from pathlib import Path

import pytest

import palimpsest_local.oci_supervisor as supervisor
from palimpsest_local.oci_supervisor import (
    MAX_ACTIVE_SESSIONS,
    ProcessIncarnation,
    SupervisorCore,
    SupervisorError,
    SupervisorErrorCategory,
    SupervisorIdentity,
    SupervisorJournal,
    SupervisorPhase,
    SupervisorRecordKind,
    authorize_supervisor_peer,
    same_process_incarnation,
)
from palimpsest_local.runtime_types import (
    DispatchKey,
    ExistingRunRecord,
    ProcessCapabilityError,
    ProcessExit,
    ProcessExitCategory,
    ProcessOutputEvent,
    ProcessSignal,
    ProcessStatusEvent,
    ProcessStream,
    RunAttachmentMode,
    RuntimeBackend,
    RuntimeKind,
)


class FakeControlPort:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def signal(self, requested: ProcessSignal) -> None:
        self.calls.append(("signal", requested))

    def request_stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")


def _record(*, run_id: str = "862ffb44-6795-4618-b2d8-c0750439fac3") -> ExistingRunRecord:
    return ExistingRunRecord(
        "oci-demo",
        run_id,
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )


def _identity(*, generation: str = "962ffb44-6795-4618-b2d8-c0750439fac3") -> SupervisorIdentity:
    return SupervisorIdentity(_record(), generation, os.geteuid())


def _directory_fd(path: Path) -> int:
    path.mkdir(mode=0o700)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _journal(path: Path, identity: SupervisorIdentity | None = None) -> SupervisorJournal:
    directory_fd = _directory_fd(path)
    try:
        return SupervisorJournal(directory_fd, identity or _identity())
    finally:
        os.close(directory_fd)


def _core(path: Path) -> tuple[SupervisorCore, SupervisorJournal, FakeControlPort]:
    journal = _journal(path)
    port = FakeControlPort()
    return SupervisorCore(journal.identity, journal, port), journal, port


@pytest.mark.parametrize(
    "record,generation,owner_uid",
    [
        (
            ExistingRunRecord(
                "cloud",
                "862ffb44-6795-4618-b2d8-c0750439fac3",
                2,
                DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM),
            ),
            "962ffb44-6795-4618-b2d8-c0750439fac3",
            os.geteuid(),
        ),
        (_record(), "not-a-uuid", os.geteuid()),
        (_record(), "962FFB44-6795-4618-B2D8-C0750439FAC3", os.geteuid()),
        (_record(), "962ffb44-6795-4618-b2d8-c0750439fac3", os.geteuid() + 1),
    ],
)
def test_supervisor_identity_rejects_non_exact_authority(record, generation, owner_uid) -> None:
    with pytest.raises(SupervisorError) as captured:
        SupervisorIdentity(record, generation, owner_uid)
    assert captured.value.category is SupervisorErrorCategory.INVALID_IDENTITY
    assert "862ffb" not in str(captured.value)


def test_peer_authorization_and_process_incarnation_never_accept_partial_identity() -> None:
    identity = _identity()
    assert identity.public_id == identity.record.run_id
    assert authorize_supervisor_peer(
        identity,
        owner_uid=os.geteuid(),
        run_id=identity.record.run_id,
        generation=identity.generation,
    )
    assert not authorize_supervisor_peer(
        identity,
        owner_uid=os.geteuid() + 1,
        run_id=identity.record.run_id,
        generation=identity.generation,
    )
    assert not authorize_supervisor_peer(
        identity,
        owner_uid=os.geteuid(),
        run_id=str(uuid.uuid4()),
        generation=identity.generation,
    )
    assert not authorize_supervisor_peer(
        identity,
        owner_uid=os.geteuid(),
        run_id=identity.record.run_id,
        generation=str(uuid.uuid4()),
    )

    first = ProcessIncarnation(123, "boot-a", 456)
    assert same_process_incarnation(first, ProcessIncarnation(123, "boot-a", 456))
    assert not same_process_incarnation(first, ProcessIncarnation(123, "boot-b", 456))
    assert not same_process_incarnation(first, ProcessIncarnation(123, "boot-a", 457))
    assert not same_process_incarnation(first, ProcessIncarnation(124, "boot-a", 456))


def test_shared_ready_cursor_detached_and_foreground_fast_exit_replay(tmp_path: Path) -> None:
    core, _journal_value, port = _core(tmp_path / "journal")
    results: list[object] = []
    detached_waiter = threading.Thread(target=lambda: results.append(core.detached_result()))
    attached_waiter = threading.Thread(target=lambda: results.append(core.attached_result()))
    detached_waiter.start()
    attached_waiter.start()
    with pytest.raises(SupervisorError) as no_id:
        _ = core.public_id
    assert no_id.value.category is SupervisorErrorCategory.NOT_READY
    with pytest.raises(SupervisorError) as timed_out:
        core.wait_ready(timeout=0)
    assert timed_out.value.category is SupervisorErrorCategory.NOT_READY

    ready_cursor = core.mark_ready()
    detached_waiter.join(timeout=1)
    attached_waiter.join(timeout=1)
    assert not detached_waiter.is_alive()
    assert not attached_waiter.is_alive()
    detached = next(item for item in results if item.attachment_mode is RunAttachmentMode.DETACHED)
    waiting_attached = next(item for item in results if item.attachment_mode is RunAttachmentMode.ATTACHED)
    assert waiting_attached.session is not None
    assert waiting_attached.session.replay_cursor == ready_cursor
    waiting_attached.session.close()
    core.append_output(ProcessStream.STDOUT, b"prefix\x00\xff")
    terminal = ProcessExit(19, 19, None, ProcessExitCategory.EXITED)
    core.mark_exit(terminal)
    attached = core.attached_result()
    session = attached.session
    assert session is not None
    assert detached.attachment_mode is RunAttachmentMode.DETACHED
    assert attached.attachment_mode is RunAttachmentMode.ATTACHED
    assert core.ready_cursor == ready_cursor == session.replay_cursor
    assert core.public_id == detached.run_id

    events = tuple(session.events())
    assert events == (
        ProcessOutputEvent(ProcessStream.STDOUT, b"prefix\x00\xff"),
        ProcessStatusEvent(terminal),
    )
    assert session.wait() == terminal
    assert port.calls == []


def test_interleaved_invalid_utf8_and_nul_bytes_are_exact(tmp_path: Path) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    core.mark_ready()
    attached = core.attached_result()
    assert attached.session is not None
    core.append_output(ProcessStream.STDOUT, b"\xff\x00out")
    core.append_output(ProcessStream.STDERR, b"err\x00\xfe")
    terminal = ProcessExit(-15, None, 15, ProcessExitCategory.SIGNALED)
    core.mark_exit(terminal)

    assert tuple(attached.session.events()) == (
        ProcessOutputEvent(ProcessStream.STDOUT, b"\xff\x00out"),
        ProcessOutputEvent(ProcessStream.STDERR, b"err\x00\xfe"),
        ProcessStatusEvent(terminal),
    )


def test_readiness_failure_and_fsync_failure_never_expose_result_or_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_core, _journal_value, _port = _core(tmp_path / "failed")
    waiter_errors: list[SupervisorErrorCategory] = []

    def wait_for_failed_readiness() -> None:
        try:
            failed_core.detached_result()
        except SupervisorError as error:
            waiter_errors.append(error.category)

    failed_waiter = threading.Thread(target=wait_for_failed_readiness)
    failed_waiter.start()
    failed_core.fail_readiness()
    failed_waiter.join(timeout=1)
    assert waiter_errors == [SupervisorErrorCategory.READINESS_FAILED]
    for operation in (failed_core.detached_result, failed_core.attached_result, lambda: failed_core.public_id):
        with pytest.raises(SupervisorError) as captured:
            operation()
        assert captured.value.category is SupervisorErrorCategory.READINESS_FAILED

    fsync_core, _fsync_journal, _fsync_port = _core(tmp_path / "fsync")
    fsync_waiting = threading.Event()
    fsync_errors: list[SupervisorErrorCategory] = []

    def wait_for_fsync_readiness() -> None:
        fsync_waiting.set()
        try:
            fsync_core.detached_result()
        except SupervisorError as error:
            fsync_errors.append(error.category)

    fsync_waiter = threading.Thread(target=wait_for_fsync_readiness)
    fsync_waiter.start()
    assert fsync_waiting.wait(timeout=1)
    monkeypatch.setattr(supervisor.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("secret")))
    with pytest.raises(SupervisorError) as durability:
        fsync_core.mark_ready()
    assert durability.value.category is SupervisorErrorCategory.JOURNAL_IO
    fsync_waiter.join(timeout=1)
    assert not fsync_waiter.is_alive()
    assert fsync_errors == [SupervisorErrorCategory.JOURNAL_IO]
    assert fsync_core.ready_cursor is None
    with pytest.raises(SupervisorError) as no_result:
        fsync_core.wait_ready(timeout=0)
    assert no_result.value.category is SupervisorErrorCategory.JOURNAL_IO


def test_disconnect_is_detach_only_and_explicit_controls_are_forwarded(tmp_path: Path) -> None:
    core, journal, port = _core(tmp_path / "journal")
    core.mark_ready()
    first = core.attached_result().session
    assert first is not None
    first.close()
    first.close()
    assert port.calls == []

    second = core.attached_result().session
    assert second is not None
    second.signal(ProcessSignal.INTERRUPT)
    core.request_stop()
    assert port.calls == [("signal", ProcessSignal.INTERRUPT), "stop"]
    second.close()
    assert port.calls == [("signal", ProcessSignal.INTERRUPT), "stop"]
    core.close()
    core.close()
    assert port.calls == [("signal", ProcessSignal.INTERRUPT), "stop", "close"]
    with pytest.raises(SupervisorError) as closed_journal:
        _ = journal.cursor
    assert closed_journal.value.category is SupervisorErrorCategory.CLOSED


def test_session_capabilities_single_consumer_and_bounded_clients(tmp_path: Path) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    core.mark_ready()
    sessions = []
    for _ in range(MAX_ACTIVE_SESSIONS):
        session = core.attached_result().session
        assert session is not None
        sessions.append(session)
    with pytest.raises(SupervisorError) as capacity:
        core.attached_result()
    assert capacity.value.category is SupervisorErrorCategory.SESSION_CAPACITY

    session = sessions.pop()
    iterator = session.events()
    with pytest.raises(SupervisorError) as consumed:
        session.events()
    assert consumed.value.category is SupervisorErrorCategory.ALREADY_CONSUMED
    with pytest.raises(SupervisorError) as wait_consumed:
        session.wait()
    assert wait_consumed.value.category is SupervisorErrorCategory.ALREADY_CONSUMED
    session.close()
    assert tuple(iterator) == ()
    replacement = core.attached_result().session
    assert replacement is not None
    assert replacement.capabilities.stdin is False
    assert replacement.capabilities.tty is False
    assert replacement.capabilities.resize is False
    assert replacement.capabilities.signal is True
    with pytest.raises(ProcessCapabilityError):
        replacement.write_stdin(b"x")
    with pytest.raises(ProcessCapabilityError):
        replacement.close_stdin()
    with pytest.raises(ProcessCapabilityError):
        replacement.resize(24, 80)
    replacement.close()
    for remaining in sessions:
        remaining.close()


def test_event_iterator_cancellation_detaches_without_control_calls(tmp_path: Path) -> None:
    core, _journal_value, port = _core(tmp_path / "journal")
    core.mark_ready()
    session = core.attached_result().session
    assert session is not None
    core.append_output(ProcessStream.STDOUT, b"one")
    iterator = session.events()
    assert next(iterator) == ProcessOutputEvent(ProcessStream.STDOUT, b"one")
    iterator.close()
    assert port.calls == []
    replacements = [core.attached_result().session for _ in range(MAX_ACTIVE_SESSIONS)]
    assert all(item is not None for item in replacements)
    with pytest.raises(SupervisorError) as capacity:
        core.attached_result()
    assert capacity.value.category is SupervisorErrorCategory.SESSION_CAPACITY
    for replacement in replacements:
        assert replacement is not None
        replacement.close()


def test_degraded_session_drains_committed_bytes_then_raises_without_exit(tmp_path: Path) -> None:
    core, _journal_value, port = _core(tmp_path / "journal")
    core.mark_ready()
    session = core.attached_result().session
    assert session is not None
    core.append_output(ProcessStream.STDOUT, b"durable-before-loss")
    core.mark_control_lost()
    with pytest.raises(SupervisorError) as stop_lost:
        core.request_stop()
    assert stop_lost.value.category is SupervisorErrorCategory.CONTROL_LOST
    with pytest.raises(SupervisorError) as signal_lost:
        session.signal(ProcessSignal.INTERRUPT)
    assert signal_lost.value.category is SupervisorErrorCategory.CONTROL_LOST
    assert port.calls == []
    iterator = session.events()
    assert next(iterator) == ProcessOutputEvent(ProcessStream.STDOUT, b"durable-before-loss")
    with pytest.raises(SupervisorError) as degraded:
        next(iterator)
    assert degraded.value.category is SupervisorErrorCategory.CONTROL_LOST
    with pytest.raises(SupervisorError) as degraded_wait:
        session.wait()
    assert degraded_wait.value.category is SupervisorErrorCategory.CONTROL_LOST
    assert core.snapshot.phase is SupervisorPhase.DEGRADED
    assert core.snapshot.exit is None
    assert port.calls == []
    with pytest.raises(SupervisorError) as no_fabricated_result:
        core.detached_result()
    assert no_fabricated_result.value.category is SupervisorErrorCategory.CONTROL_LOST


def test_post_ready_journal_failure_wakes_session_as_typed_degradation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    core.mark_ready()
    session = core.attached_result().session
    assert session is not None
    errors: list[SupervisorErrorCategory] = []
    waiting = threading.Event()

    def consume() -> None:
        waiting.set()
        try:
            tuple(session.events())
        except SupervisorError as error:
            errors.append(error.category)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert waiting.wait(timeout=1)
    monkeypatch.setattr(supervisor.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("secret")))
    with pytest.raises(SupervisorError) as failed_append:
        core.append_output(ProcessStream.STDOUT, b"not-durable")
    assert failed_append.value.category is SupervisorErrorCategory.JOURNAL_IO
    consumer.join(timeout=1)
    assert not consumer.is_alive()
    assert errors == [SupervisorErrorCategory.JOURNAL_IO]
    assert core.snapshot.phase is SupervisorPhase.DEGRADED
    assert core.snapshot.exit is None


def test_non_io_journal_failure_wakes_readiness_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    errors: list[SupervisorErrorCategory] = []
    waiting = threading.Event()

    def wait_for_result() -> None:
        waiting.set()
        try:
            core.detached_result()
        except SupervisorError as error:
            errors.append(error.category)

    waiter = threading.Thread(target=wait_for_result)
    waiter.start()
    assert waiting.wait(timeout=1)

    def corrupt_append(*_args, **_kwargs):
        raise SupervisorError(SupervisorErrorCategory.JOURNAL_CORRUPT)

    monkeypatch.setattr(SupervisorJournal, "append", corrupt_append)
    with pytest.raises(SupervisorError) as commit_error:
        core.mark_ready()
    assert commit_error.value.category is SupervisorErrorCategory.JOURNAL_CORRUPT
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert errors == [SupervisorErrorCategory.JOURNAL_CORRUPT]


def test_closed_snapshot_preserves_last_durable_cursor(tmp_path: Path) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    ready_cursor = core.mark_ready()
    core.close()
    assert core.snapshot.phase is SupervisorPhase.CLOSED
    assert core.snapshot.journal_cursor == ready_cursor


def test_core_refuses_implicit_adoption_of_existing_journal(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "journal")
    journal.append(SupervisorRecordKind.READY)
    with pytest.raises(SupervisorError) as captured:
        SupervisorCore(journal.identity, journal, FakeControlPort())
    assert captured.value.category is SupervisorErrorCategory.ADOPTION_FORBIDDEN


def test_transport_failure_is_never_accepted_as_process_exit(tmp_path: Path) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    core.mark_ready()
    transport_failure = ProcessExit(70, 70, None, ProcessExitCategory.TRANSPORT_ERROR)
    with pytest.raises(SupervisorError) as captured:
        core.mark_exit(transport_failure)
    assert captured.value.category is SupervisorErrorCategory.INVALID_TRANSITION
    assert core.snapshot.phase is SupervisorPhase.READY


def test_wait_claims_the_single_terminal_consumer(tmp_path: Path) -> None:
    core, _journal_value, _port = _core(tmp_path / "journal")
    core.mark_ready()
    session = core.attached_result().session
    assert session is not None
    terminal = ProcessExit(0, 0, None, ProcessExitCategory.EXITED)
    core.mark_exit(terminal)
    assert session.wait() == terminal
    with pytest.raises(SupervisorError) as consumed:
        session.events()
    assert consumed.value.category is SupervisorErrorCategory.ALREADY_CONSUMED


@pytest.mark.parametrize("attack", ["wrong-mode", "symlink", "hardlink", "fifo"])
def test_journal_rejects_unsafe_file_types_and_links(tmp_path: Path, attack: str) -> None:
    directory = tmp_path / attack
    directory.mkdir(mode=0o700)
    target = directory / supervisor._JOURNAL_NAME
    source = directory / "source"
    if attack == "wrong-mode":
        target.write_bytes(b"")
        target.chmod(0o644)
    elif attack == "symlink":
        source.write_bytes(b"")
        target.symlink_to(source)
    elif attack == "hardlink":
        source.write_bytes(b"")
        source.chmod(0o600)
        os.link(source, target)
    else:
        os.mkfifo(target, 0o600)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(SupervisorError) as captured:
            SupervisorJournal(directory_fd, _identity())
        assert captured.value.category is SupervisorErrorCategory.INVALID_JOURNAL
    finally:
        os.close(directory_fd)


def test_journal_rejects_wrong_directory_mode_and_inode_replacement(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong"
    wrong.mkdir(mode=0o755)
    wrong.chmod(0o755)
    wrong_fd = os.open(wrong, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(SupervisorError):
            SupervisorJournal(wrong_fd, _identity())
    finally:
        os.close(wrong_fd)

    directory = tmp_path / "replace"
    journal = _journal(directory)
    target = directory / supervisor._JOURNAL_NAME
    target.rename(directory / "original")
    target.write_bytes(supervisor._JOURNAL_HEADER)
    target.chmod(0o600)
    with pytest.raises(SupervisorError) as replaced:
        _ = journal.cursor
    assert replaced.value.category is SupervisorErrorCategory.INVALID_JOURNAL


def test_journal_holds_one_os_exclusive_writer_until_close(tmp_path: Path) -> None:
    directory = tmp_path / "exclusive"
    first = _journal(directory)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(SupervisorError) as busy:
            SupervisorJournal(directory_fd, first.identity)
        assert busy.value.category is SupervisorErrorCategory.JOURNAL_BUSY
        first.close()
        replacement = SupervisorJournal(directory_fd, first.identity)
        replacement.close()
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("attack", ["checksum", "torn", "foreign", "reordered", "duplicate"])
def test_journal_fails_closed_for_corrupt_foreign_and_nonmonotonic_records(tmp_path: Path, attack: str) -> None:
    directory = tmp_path / attack
    identity = _identity()
    journal = _journal(directory, identity)
    journal.append(SupervisorRecordKind.READY)
    journal.append(SupervisorRecordKind.OUTPUT, stream=ProcessStream.STDOUT, data=b"x")
    journal.close()
    path = directory / supervisor._JOURNAL_NAME
    content = path.read_bytes()
    header_length = len(supervisor._JOURNAL_HEADER)
    first_payload_length = supervisor._FRAME_LENGTH.unpack(
        content[header_length : header_length + supervisor._FRAME_LENGTH.size]
    )[0]
    first_frame_length = supervisor._FRAME_LENGTH.size + first_payload_length + supervisor._CHECKSUM_BYTES
    first = content[header_length : header_length + first_frame_length]
    second = content[header_length + first_frame_length :]
    if attack == "checksum":
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    elif attack == "torn":
        path.write_bytes(content[:-1])
    elif attack == "reordered":
        path.write_bytes(content[:header_length] + second + first)
    elif attack == "duplicate":
        path.write_bytes(content[:header_length] + first + first)
    foreign_identity = (
        SupervisorIdentity(identity.record, str(uuid.uuid4()), os.geteuid()) if attack == "foreign" else identity
    )
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(SupervisorError) as captured:
            SupervisorJournal(directory_fd, foreign_identity)
        assert captured.value.category is SupervisorErrorCategory.JOURNAL_CORRUPT
    finally:
        os.close(directory_fd)


def test_journal_rejects_duplicate_semantic_transition_and_oversized_output(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "journal")
    journal.append(SupervisorRecordKind.READY)
    with pytest.raises(SupervisorError) as duplicate:
        journal.append(SupervisorRecordKind.READY)
    assert duplicate.value.category is SupervisorErrorCategory.INVALID_TRANSITION
    with pytest.raises(SupervisorError) as oversized:
        journal.append(
            SupervisorRecordKind.OUTPUT,
            stream=ProcessStream.STDOUT,
            data=b"x" * (64 * 1024 + 1),
        )
    assert oversized.value.category in {
        SupervisorErrorCategory.INVALID_TRANSITION,
        SupervisorErrorCategory.JOURNAL_CORRUPT,
    }
    journal.close()
    journal.close()


def test_journal_pull_is_bounded_and_rechecks_checksum_after_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "journal"
    journal = _journal(directory)
    ready = journal.append(SupervisorRecordKind.READY)
    first = journal.append(SupervisorRecordKind.OUTPUT, stream=ProcessStream.STDOUT, data=b"x")
    second = journal.append(SupervisorRecordKind.OUTPUT, stream=ProcessStream.STDERR, data=b"y")
    assert journal.records_after(ready.cursor, limit=1) == (first,)
    assert journal.records_after(first.cursor, limit=1) == (second,)

    original_reload = SupervisorJournal._reload
    tampered = False

    def reload_then_tamper(self) -> None:
        nonlocal tampered
        original_reload(self)
        if not tampered:
            path = directory / supervisor._JOURNAL_NAME
            content = path.read_bytes()
            changed = content.replace(b"eA==", b"eQ==", 1)
            assert changed != content
            path.write_bytes(changed)
            path.chmod(0o600)
            tampered = True

    monkeypatch.setattr(SupervisorJournal, "_reload", reload_then_tamper)
    with pytest.raises(SupervisorError) as checksum:
        journal.records_after(ready.cursor, limit=1)
    assert checksum.value.category is SupervisorErrorCategory.JOURNAL_CORRUPT


@pytest.mark.parametrize(
    ("field", "replacement"),
    [(b'"version":1', b'"version":true'), (b'"cursor":1', b'"cursor":1.0')],
)
def test_journal_schema_rejects_bool_and_float_integer_fields(
    tmp_path: Path,
    field: bytes,
    replacement: bytes,
) -> None:
    journal = _journal(tmp_path / "journal")
    payload = journal._payload_for(1, SupervisorRecordKind.READY)
    malformed = payload.replace(field, replacement, 1)
    assert malformed != payload
    with pytest.raises(SupervisorError) as captured:
        journal._decode_payload(malformed, 1)
    assert captured.value.category is SupervisorErrorCategory.JOURNAL_CORRUPT


def test_architecture_boundary_has_no_runtime_or_transport_integration_imports() -> None:
    source_path = Path(supervisor.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"cli", "runtime_dispatch", "state", "oci_runtime", "protocol", "guest", "kvm", "libvirt", "socket"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[-1])
    assert imported.isdisjoint(forbidden)
    assert not any(isinstance(node, (ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith)) for node in ast.walk(tree))
    assert "subprocess" not in imported
