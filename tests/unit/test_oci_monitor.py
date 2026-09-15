"""Restart-boundary tests for the production-inert OCI-root monitor owner."""

from __future__ import annotations

import ast
import gc
import json
import os
import uuid
from pathlib import Path

import pytest

import palimpsest_local.oci_monitor as monitor
from palimpsest_local.oci_monitor import (
    MonitorBinding,
    MonitorError,
    MonitorErrorCategory,
    MonitorLease,
    MonitorProcessIdentity,
    ProcessLiveness,
)
from palimpsest_local.runtime_types import DispatchKey, ExistingRunRecord, RuntimeBackend, RuntimeKind

_RUN_ID = "862ffb44-6795-4618-b2d8-c0750439fac3"
_DOMAIN_UUID = "962ffb44-6795-4618-b2d8-c0750439fac3"
_BOOT_ATTEMPT_ID = "a62ffb44-6795-4618-b2d8-c0750439fac3"
_HOST_BOOT_ID = "b62ffb44-6795-4618-b2d8-c0750439fac3"
_GENERATION_1 = "c62ffb44-6795-4618-b2d8-c0750439fac3"
_GENERATION_2 = "d62ffb44-6795-4618-b2d8-c0750439fac3"


def _record() -> ExistingRunRecord:
    return ExistingRunRecord(
        "oci-demo",
        _RUN_ID,
        2,
        DispatchKey(RuntimeKind.OCI_ROOT, RuntimeBackend.KVM),
    )


def _binding(**changes: object) -> MonitorBinding:
    values: dict[str, object] = {
        "record": _record(),
        "owner_uid": os.geteuid(),
        "plan_digest": "sha256:" + "1" * 64,
        "definition_projection_digest": "sha256:" + "2" * 64,
        "stage1_artifact_digest": "sha256:" + "3" * 64,
        "domain_uuid": _DOMAIN_UUID,
        "domain_id": 7,
        "boot_attempt_id": _BOOT_ATTEMPT_ID,
        "libvirt_uri": "qemu:///system",
    }
    values.update(changes)
    return MonitorBinding(**values)  # type: ignore[arg-type]


def _process(pid: int = 101, *, boot_id: str = _HOST_BOOT_ID, start_ticks: int = 202) -> MonitorProcessIdentity:
    return MonitorProcessIdentity(pid, boot_id, start_ticks)


def _directory_fd(path: Path) -> int:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _create(path: Path, process: MonitorProcessIdentity | None = None) -> MonitorLease:
    current = process or _process()
    directory_fd = _directory_fd(path)
    try:
        return MonitorLease.create(
            directory_fd,
            _binding(),
            current_process=lambda: current,
            generation_factory=lambda: uuid.UUID(_GENERATION_1),
        )
    finally:
        os.close(directory_fd)


def _journal_bytes(path: Path) -> bytes:
    return (path / monitor._JOURNAL_NAME).read_bytes()


def test_create_running_terminal_transitions_are_exact_and_secret_free(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    lease = _create(directory)
    assert lease.snapshot.phase == "starting"
    assert lease.snapshot.revision == 1
    assert lease.snapshot.monitor_generation == _GENERATION_1
    running = lease.mark_running()
    assert (running.phase, running.revision) == ("running", 2)
    terminal = lease.mark_terminal()
    assert (terminal.phase, terminal.revision) == ("terminal", 3)
    payload = json.loads(_journal_bytes(directory))
    assert payload == terminal.to_dict()
    assert payload["binding"]["plan_digest"] == "sha256:" + "1" * 64
    assert payload["binding"]["definition_projection_digest"] == "sha256:" + "2" * 64
    assert payload["binding"]["stage1_artifact_digest"] == "sha256:" + "3" * 64
    assert payload["binding"]["domain_uuid"] == _DOMAIN_UUID
    assert payload["binding"]["domain_id"] == 7
    assert payload["binding"]["boot_attempt_id"] == _BOOT_ATTEMPT_ID
    assert payload["binding"]["lifecycle_protocol"] == "palimpsest.oci-lifecycle-control.v2"
    assert not any(word in _journal_bytes(directory).lower() for word in (b"secret", b"private_key", b'"mac"'))
    with pytest.raises(MonitorError) as duplicate:
        lease.mark_terminal()
    assert duplicate.value.category is MonitorErrorCategory.INVALID_TRANSITION
    lease.close()


def test_os_lock_is_the_single_writer_and_existing_journal_requires_explicit_adoption(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    first = _create(directory)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as busy:
            MonitorLease.create(directory_fd, _binding(), current_process=lambda: _process())
        assert busy.value.category is MonitorErrorCategory.JOURNAL_BUSY
        first.close()
        with pytest.raises(MonitorError) as explicit:
            MonitorLease.create(directory_fd, _binding(), current_process=lambda: _process())
        assert explicit.value.category is MonitorErrorCategory.ADOPTION_FORBIDDEN
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize(
    ("liveness", "category"),
    [
        (ProcessLiveness.LIVE, MonitorErrorCategory.WRITER_LIVE),
        (ProcessLiveness.UNKNOWN, MonitorErrorCategory.WRITER_UNKNOWN),
        ("stale", MonitorErrorCategory.WRITER_UNKNOWN),
    ],
)
def test_adoption_rejects_live_unknown_and_non_typed_writer_state(
    tmp_path: Path,
    liveness: object,
    category: MonitorErrorCategory,
) -> None:
    directory = tmp_path / "run"
    first = _create(directory)
    first.close()
    before = _journal_bytes(directory)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as captured:
            MonitorLease.adopt(
                directory_fd,
                _binding(),
                current_process=lambda: _process(303, start_ticks=404),
                liveness_probe=lambda _old: liveness,  # type: ignore[return-value]
            )
        assert captured.value.category is category
        assert _journal_bytes(directory) == before
    finally:
        os.close(directory_fd)


def test_exact_stale_adoption_is_cleanup_only_and_cannot_recover_running_authority(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    old = _create(directory)
    old.mark_running()
    old.close()
    new_process = _process(303, start_ticks=404)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        adopted = MonitorLease.adopt(
            directory_fd,
            _binding(),
            current_process=lambda: new_process,
            liveness_probe=lambda prior: ProcessLiveness.STALE if prior == _process() else ProcessLiveness.UNKNOWN,
            generation_factory=lambda: uuid.UUID(_GENERATION_2),
        )
    finally:
        os.close(directory_fd)
    assert adopted.snapshot.phase == "adopting"
    assert adopted.snapshot.revision == 3
    assert adopted.snapshot.monitor_generation == _GENERATION_2
    assert adopted.snapshot.writer == new_process
    with pytest.raises(MonitorError) as no_key_recovery:
        adopted.mark_running()
    assert no_key_recovery.value.category is MonitorErrorCategory.INVALID_TRANSITION
    lost = adopted.mark_control_lost()
    assert (lost.phase, lost.revision) == ("control-lost", 4)
    adopted.close()

    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as terminal_adoption:
            MonitorLease.adopt(
                directory_fd,
                _binding(),
                current_process=lambda: _process(505, start_ticks=606),
                liveness_probe=lambda _old: ProcessLiveness.STALE,
            )
        assert terminal_adoption.value.category is MonitorErrorCategory.ADOPTION_FORBIDDEN
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("run_id", str(uuid.uuid4())),
        ("plan_digest", "sha256:" + "a" * 64),
        ("definition_projection_digest", "sha256:" + "b" * 64),
        ("stage1_artifact_digest", "sha256:" + "c" * 64),
        ("domain_uuid", str(uuid.uuid4())),
        ("domain_id", 8),
        ("boot_attempt_id", str(uuid.uuid4())),
        ("libvirt_uri", "qemu:///session"),
        ("lifecycle_protocol", "palimpsest.oci-lifecycle-control.v1"),
    ],
)
def test_adoption_rejects_every_exact_launch_binding_change(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    directory = tmp_path / field
    first = _create(directory)
    first.close()
    path = directory / monitor._JOURNAL_NAME
    raw = json.loads(path.read_bytes())
    raw["binding"][field] = replacement
    path.write_bytes(monitor._canonical_bytes(raw))
    path.chmod(0o600)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as captured:
            MonitorLease.adopt(
                directory_fd,
                _binding(),
                current_process=lambda: _process(303),
                liveness_probe=lambda _old: ProcessLiveness.STALE,
            )
        assert captured.value.category is MonitorErrorCategory.INVALID_JOURNAL
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("attack", ["unknown-secret-field", "noncanonical", "symlink", "wrong-mode"])
def test_journal_is_exact_owner_only_canonical_and_secret_shaped_fields_are_not_admitted(
    tmp_path: Path,
    attack: str,
) -> None:
    directory = tmp_path / attack
    first = _create(directory)
    first.close()
    path = directory / monitor._JOURNAL_NAME
    if attack == "unknown-secret-field":
        raw = json.loads(path.read_bytes())
        raw["bootstrap_secret"] = "not-stored"
        path.write_bytes(monitor._canonical_bytes(raw))
    elif attack == "noncanonical":
        path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2), encoding="utf-8")
    elif attack == "symlink":
        target = directory / "elsewhere"
        path.rename(target)
        path.symlink_to(target)
    else:
        path.chmod(0o644)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as captured:
            MonitorLease.adopt(
                directory_fd,
                _binding(),
                current_process=lambda: _process(303),
                liveness_probe=lambda _old: ProcessLiveness.STALE,
            )
        assert captured.value.category is MonitorErrorCategory.INVALID_JOURNAL
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("target", ["lock-symlink", "lock-hardlink", "journal-hardlink"])
def test_lock_and_journal_link_attacks_fail_closed(tmp_path: Path, target: str) -> None:
    directory = tmp_path / target
    first = _create(directory)
    first.close()
    lock_path = directory / monitor._LOCK_NAME
    journal_path = directory / monitor._JOURNAL_NAME
    if target == "lock-symlink":
        replacement = directory / "replacement-lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        lock_path.unlink()
        lock_path.symlink_to(replacement)
    elif target == "lock-hardlink":
        os.link(lock_path, directory / "second-lock-link")
    else:
        os.link(journal_path, directory / "second-journal-link")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as captured:
            MonitorLease.adopt(
                directory_fd,
                _binding(),
                current_process=lambda: _process(303),
                liveness_probe=lambda _old: ProcessLiveness.STALE,
            )
        assert captured.value.category is MonitorErrorCategory.INVALID_JOURNAL
    finally:
        os.close(directory_fd)


def test_live_lock_path_replacement_latches_poison(tmp_path: Path) -> None:
    directory = tmp_path / "replaced-lock"
    lease = _create(directory)
    lock_path = directory / monitor._LOCK_NAME
    replacement = directory / "new-lock"
    replacement.write_bytes(b"")
    replacement.chmod(0o600)
    replacement.replace(lock_path)
    with pytest.raises(MonitorError) as changed:
        _ = lease.snapshot
    assert changed.value.category is MonitorErrorCategory.INVALID_JOURNAL
    with pytest.raises(MonitorError) as latched:
        _ = lease.snapshot
    assert latched.value.category is MonitorErrorCategory.POISONED
    lease.close()


def test_monitor_directory_must_be_owner_private(tmp_path: Path) -> None:
    directory = tmp_path / "wide-directory"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(MonitorError) as captured:
            MonitorLease.create(directory_fd, _binding(), current_process=lambda: _process())
        assert captured.value.category is MonitorErrorCategory.INVALID_JOURNAL
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("failure", ["file-fsync", "replace"])
def test_prepublication_transition_failure_preserves_old_journal_and_poisons_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    directory = tmp_path / failure
    lease = _create(directory)
    before = _journal_bytes(directory)
    if failure == "file-fsync":
        original_fsync = monitor.os.fsync
        calls = 0

        def fail_first(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected")
            original_fsync(descriptor)

        monkeypatch.setattr(monitor.os, "fsync", fail_first)
    else:
        monkeypatch.setattr(monitor.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(MonitorError) as failed:
        lease.mark_running()
    assert failed.value.category is MonitorErrorCategory.JOURNAL_IO
    assert _journal_bytes(directory) == before
    assert not tuple(directory.glob(".oci-monitor-*.tmp"))
    with pytest.raises(MonitorError) as poisoned:
        _ = lease.snapshot
    assert poisoned.value.category is MonitorErrorCategory.POISONED
    lease.close()


def test_post_replace_directory_fsync_failure_never_reports_commit_and_visible_state_is_old_or_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "dir-fsync"
    lease = _create(directory)
    before = _journal_bytes(directory)
    expected = monitor.MonitorJournalSnapshot(
        lease.snapshot.binding,
        lease.snapshot.monitor_generation,
        "running",
        2,
        lease.snapshot.writer,
    )
    expected_bytes = monitor._canonical_bytes(expected.to_dict())
    original_fsync = monitor.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        original_fsync(descriptor)

    monkeypatch.setattr(monitor.os, "fsync", fail_directory_fsync)
    with pytest.raises(MonitorError) as failed:
        lease.mark_running()
    assert failed.value.category is MonitorErrorCategory.JOURNAL_IO
    assert _journal_bytes(directory) in {before, expected_bytes}
    with pytest.raises(MonitorError) as poisoned:
        _ = lease.snapshot
    assert poisoned.value.category is MonitorErrorCategory.POISONED
    lease.close()


def test_initial_file_fsync_failure_leaves_no_journal_or_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "initial-fsync"
    directory.mkdir(mode=0o700)
    lock_path = directory / monitor._LOCK_NAME
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(monitor.os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("injected")))
    try:
        with pytest.raises(MonitorError) as failed:
            MonitorLease.create(
                directory_fd,
                _binding(),
                current_process=lambda: _process(),
                generation_factory=lambda: uuid.UUID(_GENERATION_1),
            )
        assert failed.value.category is MonitorErrorCategory.JOURNAL_IO
    finally:
        os.close(directory_fd)
    assert not (directory / monitor._JOURNAL_NAME).exists()
    assert not tuple(directory.glob(".oci-monitor-*.tmp"))


def test_initial_post_link_directory_fsync_failure_never_reports_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "initial-dir-fsync"
    directory.mkdir(mode=0o700)
    lock_path = directory / monitor._LOCK_NAME
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    real_fsync = monitor.os.fsync
    calls = 0

    def fail_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        real_fsync(descriptor)

    monkeypatch.setattr(monitor.os, "fsync", fail_second_fsync)
    try:
        with pytest.raises(MonitorError) as captured:
            MonitorLease.create(
                directory_fd,
                _binding(),
                current_process=lambda: _process(),
                generation_factory=lambda: uuid.UUID(_GENERATION_1),
            )
        assert captured.value.category is MonitorErrorCategory.JOURNAL_IO
    finally:
        os.close(directory_fd)
    assert (directory / monitor._JOURNAL_NAME).exists()
    assert not tuple(directory.glob(".oci-monitor-*.tmp"))


def test_journal_read_close_failure_is_stable_and_latches_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _create(tmp_path / "close-failure")
    real_close = monitor.os.close
    failed_descriptor: list[int] = []

    def fail_once(descriptor: int) -> None:
        if not failed_descriptor:
            failed_descriptor.append(descriptor)
            raise OSError("injected")
        real_close(descriptor)

    monkeypatch.setattr(monitor.os, "close", fail_once)
    with pytest.raises(MonitorError) as captured:
        _ = lease.snapshot
    assert captured.value.category is MonitorErrorCategory.JOURNAL_IO
    with pytest.raises(MonitorError) as latched:
        _ = lease.snapshot
    assert latched.value.category is MonitorErrorCategory.POISONED
    monkeypatch.setattr(monitor.os, "close", real_close)
    real_close(failed_descriptor[0])
    lease.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_child_cannot_retain_or_reuse_parent_monitor_descriptors(tmp_path: Path) -> None:
    directory = tmp_path / "fork"
    lease = _create(directory)
    child_ready_read, child_ready_write = os.pipe()
    child_exit_read, child_exit_write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(child_ready_read)
            os.close(child_exit_write)
            try:
                _ = lease.snapshot
            except MonitorError as exc:
                poisoned = exc.category is MonitorErrorCategory.PROCESS_MISMATCH
            else:
                poisoned = False
            opened = [os.open(os.devnull, os.O_RDONLY) for _ in range(32)]
            del lease
            gc.collect()
            descriptors_intact = all(os.fstat(descriptor).st_mode for descriptor in opened)
            os.write(child_ready_write, b"ok" if poisoned and descriptors_intact else b"bad")
            os.read(child_exit_read, 1)
        except BaseException as exc:
            os.write(child_ready_write, repr(exc).encode()[:200])
        finally:
            os._exit(0)
    os.close(child_ready_write)
    os.close(child_exit_read)
    try:
        assert os.read(child_ready_read, 200) == b"ok"
        lease.close()
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            adopted = MonitorLease.adopt(
                directory_fd,
                _binding(),
                current_process=lambda: _process(303, start_ticks=404),
                liveness_probe=lambda _old: ProcessLiveness.STALE,
                generation_factory=lambda: uuid.UUID(_GENERATION_2),
            )
        finally:
            os.close(directory_fd)
        adopted.mark_control_lost()
        adopted.close()
    finally:
        os.write(child_exit_write, b"x")
        os.close(child_exit_write)
        os.close(child_ready_read)
        os.waitpid(child, 0)


def test_process_identity_change_blocks_transition_without_journal_mutation(tmp_path: Path) -> None:
    directory = tmp_path / "run"
    current = [_process()]
    directory_fd = _directory_fd(directory)
    try:
        lease = MonitorLease.create(
            directory_fd,
            _binding(),
            current_process=lambda: current[0],
            generation_factory=lambda: uuid.UUID(_GENERATION_1),
        )
    finally:
        os.close(directory_fd)
    before = _journal_bytes(directory)
    current[0] = _process(start_ticks=203)
    with pytest.raises(MonitorError) as captured:
        lease.mark_running()
    assert captured.value.category is MonitorErrorCategory.PROCESS_MISMATCH
    assert _journal_bytes(directory) == before
    current[0] = _process()
    with pytest.raises(MonitorError) as latched:
        _ = lease.snapshot
    assert latched.value.category is MonitorErrorCategory.POISONED
    lease.close()


def test_live_journal_mutation_latches_poison_even_if_original_bytes_return(tmp_path: Path) -> None:
    directory = tmp_path / "tamper"
    lease = _create(directory)
    path = directory / monitor._JOURNAL_NAME
    original = path.read_bytes()
    raw = json.loads(original)
    raw["phase"] = "running"
    path.write_bytes(monitor._canonical_bytes(raw))
    path.chmod(0o600)
    with pytest.raises(MonitorError) as changed:
        _ = lease.snapshot
    assert changed.value.category is MonitorErrorCategory.INVALID_JOURNAL
    path.write_bytes(original)
    path.chmod(0o600)
    with pytest.raises(MonitorError) as latched:
        _ = lease.snapshot
    assert latched.value.category is MonitorErrorCategory.POISONED
    lease.close()


def test_proc_start_ticks_parser_handles_spaces_and_parentheses_in_comm() -> None:
    fields = ["S", *("0" for _ in range(18)), "98765", "0"]
    content = "123 (name with ) spaces) " + " ".join(fields)
    assert monitor._parse_proc_start_ticks(content, 123) == 98765
    with pytest.raises(ValueError):
        monitor._parse_proc_start_ticks(content, 124)


@pytest.mark.parametrize(
    ("boot_id", "ticks", "expected"),
    [
        ("e62ffb44-6795-4618-b2d8-c0750439fac3", 202, ProcessLiveness.STALE),
        (_HOST_BOOT_ID, 202, ProcessLiveness.LIVE),
        (_HOST_BOOT_ID, 203, ProcessLiveness.STALE),
        (_HOST_BOOT_ID, FileNotFoundError(), ProcessLiveness.STALE),
        (_HOST_BOOT_ID, PermissionError(), ProcessLiveness.UNKNOWN),
        (_HOST_BOOT_ID, ValueError(), ProcessLiveness.UNKNOWN),
    ],
)
def test_process_liveness_requires_exact_host_boot_and_start_ticks(
    monkeypatch: pytest.MonkeyPatch,
    boot_id: str,
    ticks: object,
    expected: ProcessLiveness,
) -> None:
    monkeypatch.setattr(monitor, "_read_boot_id", lambda: boot_id)

    def read_ticks(_pid: int) -> int:
        if isinstance(ticks, BaseException):
            raise ticks
        assert type(ticks) is int
        return ticks

    monkeypatch.setattr(monitor, "_read_process_start_ticks", read_ticks)
    assert monitor.probe_process_liveness(_process()) is expected


def test_active_lease_remains_unreachable_from_public_or_synchronous_runtime() -> None:
    package = Path(monitor.__file__).parent
    forbidden_importers = {
        "__init__.py",
        "cli.py",
        "runtime.py",
        "runtime_dispatch.py",
        "oci_root_runtime.py",
        "oci_supervisor.py",
    }
    importers: list[str] = []
    for name in forbidden_importers:
        path = package / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name.endswith("oci_monitor") for alias in node.names):
                importers.append(name)
            if isinstance(node, ast.ImportFrom) and (
                (node.module is not None and node.module.endswith("oci_monitor"))
                or any(alias.name == "oci_monitor" for alias in node.names)
            ):
                if (
                    name == "oci_root_runtime.py"
                    and node.module == "oci_monitor"
                    and {alias.name for alias in node.names} == {"MonitorBinding"}
                ):
                    # Private launch shares the immutable active identity, not
                    # the independent v1 lease/lock acquisition path.
                    continue
                importers.append(name)
    assert importers == []
