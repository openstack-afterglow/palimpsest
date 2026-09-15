"""Strict codec and Linux descriptor-bound OCI exec record storage."""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from palimpsest_local import oci_exec_record as records
from palimpsest_local.oci_exec_session import OCIExecCompletionObservation
from palimpsest_local.runtime_types import ProcessExit, ProcessExitCategory


def observation(*, acknowledgement="unconfirmed", terminal=None, reason="completed", stdout=11, stderr=7):
    if terminal is None:
        terminal = ProcessExit(23, 23, None, ProcessExitCategory.EXITED)
    return OCIExecCompletionObservation(terminal, reason, stdout, stderr, acknowledgement)


@pytest.mark.parametrize(
    "value",
    [
        observation(),
        observation(terminal=ProcessExit(-15, None, 15, ProcessExitCategory.SIGNALED), reason="timeout"),
        OCIExecCompletionObservation(None, "cancelled", 0, 0, "unconfirmed"),
    ],
)
def test_codec_roundtrips_only_exact_minimal_observation(value):
    record_id = str(uuid.uuid4())
    payload = records._encode_artifact(record_id, "observed", value)
    assert len(payload) <= records.MAX_ARTIFACT_BYTES
    assert records._decode_artifact(payload, expected_phase="observed") == (record_id, value)
    assert b"argv" not in payload and b"token" not in payload and b"environment" not in payload


def test_pending_codec_has_exact_schema_and_independent_canonical_uuid():
    record_id = str(uuid.uuid4())
    payload = records._encode_artifact(record_id, "pending", None)
    assert json.loads(payload) == {
        "schema": "palimpsest.oci-exec-record",
        "version": 1,
        "record_id": record_id,
        "phase": "pending",
        "observation": None,
    }
    assert records._decode_artifact(payload, expected_phase="pending") == (record_id, None)


def _canonical(value) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode() + b"\n"


@pytest.mark.parametrize(
    "damage",
    ["unknown", "version", "bool-version", "phase", "id", "bool-count", "float", "nan", "noncanonical"],
)
def test_codec_fails_closed_on_schema_scalar_and_canonical_encoding_damage(damage):
    value = json.loads(records._encode_artifact(str(uuid.uuid4()), "observed", observation()))
    if damage == "unknown":
        value["argv"] = ["secret"]
    elif damage == "version":
        value["version"] = 2
    elif damage == "bool-version":
        value["version"] = True
    elif damage == "phase":
        value["phase"] = "confirmed"
    elif damage == "id":
        value["record_id"] = str(uuid.uuid1())
    elif damage == "bool-count":
        value["observation"]["stdout_bytes"] = True
    elif damage == "float":
        value["observation"]["stderr_bytes"] = 1.0
    elif damage == "nan":
        payload = records._encode_artifact(str(uuid.uuid4()), "pending", None).replace(b"null", b"NaN", 1)
    elif damage == "noncanonical":
        payload = json.dumps(value).encode()
    if damage not in {"nan", "noncanonical"}:
        payload = _canonical(value)
    with pytest.raises((TypeError, ValueError)):
        records._decode_artifact(payload, expected_phase="observed")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"x","schema":"y"}\n',
        b"\xff",
        b"[" * 100 + b"]" * 100,
        b'{"version":999999999999999999999999999999999999999999999999999}\n',
        b"{}" + b" " * records.MAX_ARTIFACT_BYTES,
    ],
)
def test_codec_rejects_duplicate_invalid_utf8_deep_large_and_oversized_payloads(payload):
    with pytest.raises((TypeError, ValueError)):
        records._decode_artifact(payload, expected_phase="pending")


def test_observation_invariants_are_revalidated_and_ack_phase_is_exact():
    valid = observation()
    with pytest.raises((TypeError, ValueError)):
        records._encode_artifact(str(uuid.uuid4()), "confirmed", valid)
    with pytest.raises((TypeError, ValueError)):
        records._encode_artifact(str(uuid.uuid4()), "observed", replace(valid, stdout_bytes=True))
    with pytest.raises((TypeError, ValueError)):
        observation(terminal=ProcessExit(256, 256, None, ProcessExitCategory.EXITED))


def test_snapshot_is_frozen_and_labels_claim_without_live_authority():
    record_id = str(uuid.uuid4())
    snapshot = records.OCIExecRecordSnapshot(record_id, "observed", observation())
    with pytest.raises(FrozenInstanceError):
        snapshot.phase = "confirmed"
    value = snapshot.to_dict()
    assert value["classification"] == "local-historical-metadata"
    assert "no run or monitor authority" in value["guidance"]
    assert set(value) == {"schema", "version", "record_id", "phase", "observation", "classification", "guidance"}
    with pytest.raises(ValueError):
        records.OCIExecRecordSnapshot(record_id, "observed", replace(observation(), acknowledgement="confirmed"))


@pytest.mark.parametrize(
    ("phase", "value"),
    [
        ("pending", observation()),
        ("observed", None),
        ("observed", observation(acknowledgement="confirmed")),
        ("confirmed", observation()),
    ],
)
def test_snapshot_phase_requires_exact_acknowledgement(phase, value):
    with pytest.raises(ValueError):
        records.OCIExecRecordSnapshot(str(uuid.uuid4()), phase, value)


def test_snapshot_revalidates_mutated_terminal_consistency():
    value = observation()
    object.__setattr__(value.terminal, "returncode", 22)
    with pytest.raises((TypeError, ValueError)):
        records.OCIExecRecordSnapshot(str(uuid.uuid4()), "observed", value)


def test_error_diagnostics_are_fixed_stage_allowlisted_and_path_free():
    for stage in records.OCIExecRecordError._MESSAGES:
        error = records.OCIExecRecordError(stage)
        assert error.stage == stage and "/private/secret" not in str(error)
    with pytest.raises(ValueError):
        records.OCIExecRecordError("/private/secret")


def test_path_codec_bounds_components_bytes_and_parent_traversal():
    assert records._path_components("/private/record") == ("private", "record")
    for path in (
        "relative",
        "/private/../record",
        "/private//record",
        "/" + "/".join(["x"] * 65),
        "/" + "x" * 256,
        "/" + "/".join(["x" * 64] * 64),
    ):
        with pytest.raises(OSError):
            records._path_components(path)


def test_file_metadata_validator_rejects_wrong_owner_without_privilege(tmp_path):
    target = tmp_path / "artifact"
    target.write_bytes(b"x")
    target.chmod(0o600)
    metadata = list(target.stat())
    metadata[4] = os.geteuid() + 1
    with pytest.raises(OSError):
        records._safe_file(os.stat_result(metadata), uid=os.geteuid())


def test_storage_entrypoints_sanitize_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(records.sys, "platform", "not-linux")
    with pytest.raises(records.OCIExecRecordError) as reserve_error:
        records.OCIExecRecordWriter.reserve(tmp_path / "record", managed_state=tmp_path)
    with pytest.raises(records.OCIExecRecordError) as read_error:
        records.read_exec_record(tmp_path / "record")
    assert reserve_error.value.stage == "reserve" and read_error.value.stage == "read"


def test_storage_entrypoints_preserve_arbitrary_base_exception_identity(monkeypatch, tmp_path):
    class Cancelled(BaseException):
        pass

    for entrypoint in (
        lambda: records.OCIExecRecordWriter.reserve(tmp_path / "record", managed_state=tmp_path),
        lambda: records.read_exec_record(tmp_path / "record"),
    ):
        interruption = Cancelled()

        def interrupt(interruption=interruption):
            raise interruption

        monkeypatch.setattr(records, "_require_linux", interrupt)
        with pytest.raises(Cancelled) as raised:
            entrypoint()
        assert raised.value is interruption


def test_publish_uuid_failure_is_path_free_and_poisons_writer(monkeypatch, tmp_path):
    chain = records._DirectoryChain([os.open(tmp_path, os.O_RDONLY)], [])
    writer = records.OCIExecRecordWriter(chain, str(uuid.uuid4()))
    monkeypatch.setattr(records.uuid, "uuid4", lambda: (_ for _ in ()).throw(OSError("/secret entropy")))

    with pytest.raises(records.OCIExecRecordError) as raised:
        writer._publish("pending", None)

    assert raised.value.stage == "pending" and "/secret" not in str(raised.value)
    with pytest.raises(records.OCIExecRecordError, match="uncertain"):
        writer._publish("pending", None)
    writer.close()


def test_publish_uuid_base_exception_preserves_identity_and_closes_writer(monkeypatch, tmp_path):
    class Cancelled(BaseException):
        pass

    directory_fd = os.open(tmp_path, os.O_RDONLY)
    chain = records._DirectoryChain([directory_fd], [])
    writer = records.OCIExecRecordWriter(chain, str(uuid.uuid4()))
    interruption = Cancelled()
    monkeypatch.setattr(records.uuid, "uuid4", lambda: (_ for _ in ()).throw(interruption))

    with pytest.raises(Cancelled) as raised:
        writer._publish("pending", None)

    assert raised.value is interruption and writer._closed and chain.fds == []
    with pytest.raises(OSError):
        os.fstat(directory_fd)


def test_missing_renameat2_support_fails_closed_without_fallback(monkeypatch):
    monkeypatch.setattr(records.ctypes, "CDLL", lambda *_args, **_kwargs: object())
    with pytest.raises(OSError) as raised:
        records._rename_noreplace(-1, "source", "target")
    assert raised.value.errno == errno.ENOSYS


@pytest.fixture
def linux_private(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux descriptor and renameat2 qualification requires Linux")
    parent = tmp_path / "private"
    managed = tmp_path / "managed"
    parent.mkdir(mode=0o700)
    managed.mkdir(mode=0o700)
    parent.chmod(0o700)
    managed.chmod(0o700)
    return parent, managed


def reserve(linux_private, name="record"):
    parent, managed = linux_private
    return records.OCIExecRecordWriter.reserve(parent / name, managed_state=managed)


def open_fd_count():
    return len(os.listdir("/proc/self/fd"))


def named_user_acl():
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identity)
        for tag, permissions, identity in (
            (1, 6, 0xFFFFFFFF),
            (2, 4, os.geteuid() + 1),
            (4, 0, 0xFFFFFFFF),
            (16, 0, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        )
    )


def test_linux_writer_publishes_each_phase_with_exact_modes_and_reader_is_conservative(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert records.read_exec_record(path).phase == "pending"
    unconfirmed = observation()
    writer.publish_observed(unconfirmed)
    assert records.read_exec_record(path).observation == unconfirmed
    confirmed = replace(unconfirmed, acknowledgement="confirmed")
    writer.publish_confirmed(confirmed)
    writer.close()
    snapshot = records.read_exec_record(path)
    assert snapshot.phase == "confirmed" and snapshot.observation == confirmed
    assert {item.name for item in path.iterdir()} == {"pending.json", "observed.json", "confirmed.json"}
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in path.iterdir())


@pytest.mark.parametrize("failure", ["write", "file-fsync", "directory-fsync", "interrupt"])
def test_linux_pending_publication_failure_releases_every_reserved_descriptor(monkeypatch, linux_private, failure):
    before = open_fd_count()
    target = linux_private[0] / failure
    real_fsync = records.os.fsync
    directory_syncs = 0

    if failure in {"write", "interrupt"}:
        interruption = KeyboardInterrupt() if failure == "interrupt" else OSError("write secret")

        def fail_write(_fd, _payload):
            raise interruption

        monkeypatch.setattr(records.os, "write", fail_write)
    elif failure == "file-fsync":

        def fail_fsync(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("file sync secret")
            return real_fsync(fd)

        monkeypatch.setattr(records.os, "fsync", fail_fsync)
    else:

        def fail_fsync(fd):
            nonlocal directory_syncs
            metadata = os.fstat(fd)
            if target.exists():
                record = target.stat()
                if (metadata.st_dev, metadata.st_ino) == (record.st_dev, record.st_ino):
                    directory_syncs += 1
                    if directory_syncs == 2:
                        raise OSError("directory sync secret")
            return real_fsync(fd)

        monkeypatch.setattr(records.os, "fsync", fail_fsync)

    expected = KeyboardInterrupt if failure == "interrupt" else records.OCIExecRecordError
    with pytest.raises(expected) as raised:
        records.OCIExecRecordWriter.reserve(target, managed_state=linux_private[1])
    if failure != "interrupt":
        assert raised.value.stage == "pending" and "secret" not in str(raised.value)
    assert open_fd_count() == before


def test_linux_reserve_uuid_failure_closes_descriptors_and_sanitizes(monkeypatch, linux_private):
    before = open_fd_count()
    target = linux_private[0] / "uuid-failure"
    monkeypatch.setattr(records.uuid, "uuid4", lambda: (_ for _ in ()).throw(OSError("/secret entropy")))

    with pytest.raises(records.OCIExecRecordError) as raised:
        records.OCIExecRecordWriter.reserve(target, managed_state=linux_private[1])

    assert raised.value.stage == "reserve" and "/secret" not in str(raised.value)
    assert target.is_dir() and list(target.iterdir()) == []
    assert open_fd_count() == before


def test_linux_reader_works_in_a_separate_process_after_writer_close(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    writer.publish_observed(observation())
    writer.close()
    program = "from palimpsest_local.oci_exec_record import read_exec_record; import sys; print(read_exec_record(sys.argv[1]).phase)"
    result = subprocess.run([sys.executable, "-c", program, str(path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0 and result.stdout == "observed\n" and result.stderr == ""


def test_linux_existing_record_and_final_artifacts_are_never_adopted_or_clobbered(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    original = b"not our artifact"
    (path / "observed.json").write_bytes(original)
    (path / "observed.json").chmod(0o600)
    with pytest.raises(records.OCIExecRecordError) as error:
        writer.publish_observed(observation())
    assert error.value.stage == "observed" and (path / "observed.json").read_bytes() == original
    with pytest.raises(records.OCIExecRecordError, match="uncertain"):
        writer.publish_observed(observation())
    writer.close()
    with pytest.raises(records.OCIExecRecordError):
        records.OCIExecRecordWriter.reserve(path, managed_state=linux_private[1])


def test_linux_injected_no_clobber_collision_preserves_winner(monkeypatch, linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    value = observation()
    winner = records._encode_artifact(writer.record_id, "observed", value)
    real_rename = records._rename_noreplace

    def collide(directory_fd, source, target):
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            offset = 0
            while offset < len(winner):
                offset += os.write(fd, winner[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        real_rename(directory_fd, source, target)

    monkeypatch.setattr(records, "_rename_noreplace", collide)
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_observed(value)
    assert raised.value.stage == "observed"
    writer.close()
    assert (path / "observed.json").read_bytes() == winner
    assert not list(path.glob(".observed.*.tmp"))


def test_linux_reserve_rejects_distinct_inode_replacement_before_open(monkeypatch, linux_private):
    before = open_fd_count()
    parent, managed = linux_private
    path = parent / "creation-race"
    original = parent / "creation-race-original"
    real_open = records.os.open
    replaced = False
    original_identity = replacement_identity = None

    def replace_before_open(name, flags, *args, **kwargs):
        nonlocal original_identity, replaced, replacement_identity
        if name == path.name and flags & os.O_DIRECTORY and not replaced:
            replaced = True
            directory_fd = kwargs["dir_fd"]
            # Unlink/recreate can recycle an inode and its timestamps; retaining the
            # original under a sibling name makes this a distinct-identity injection.
            original_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            original_identity = (original_metadata.st_dev, original_metadata.st_ino)
            os.rename(name, original.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.mkdir(name, 0o700, dir_fd=kwargs["dir_fd"])
            replacement_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            replacement_identity = (replacement_metadata.st_dev, replacement_metadata.st_ino)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(records.os, "open", replace_before_open)
    with pytest.raises(records.OCIExecRecordError) as raised:
        records.OCIExecRecordWriter.reserve(path, managed_state=managed)
    assert raised.value.stage == "reserve"
    assert replaced and original_identity is not None and replacement_identity is not None
    assert original_identity != replacement_identity
    assert (original.stat().st_dev, original.stat().st_ino) == original_identity
    assert original.is_dir() and list(original.iterdir()) == []
    assert path.is_dir() and list(path.iterdir()) == []
    assert not (path / "pending.json").exists()
    assert open_fd_count() == before


@pytest.mark.parametrize("bad", ["relative", "dotdot", "symlink", "managed", "parent-mode"])
def test_linux_reserve_rejects_unsafe_paths_and_managed_state_ancestry(linux_private, bad):
    parent, managed = linux_private
    path = parent / "record"
    if bad == "relative":
        path = Path("relative")
    elif bad == "dotdot":
        path = Path(str(parent) + "/../private/record")
    elif bad == "symlink":
        link = parent.parent / "link"
        link.symlink_to(parent, target_is_directory=True)
        path = link / "record"
    elif bad == "managed":
        managed_child = managed / "private"
        managed_child.mkdir(mode=0o700)
        path = managed_child / "record"
    else:
        parent.chmod(0o750)
    with pytest.raises(records.OCIExecRecordError) as error:
        records.OCIExecRecordWriter.reserve(path, managed_state=managed)
    assert error.value.stage == "reserve" and str(path) not in str(error.value)


@pytest.mark.parametrize(
    "damage", ["unknown", "oversize", "hardlink", "mode", "fifo", "symlink", "corrupt", "conflict", "temp-name"]
)
def test_linux_reader_rejects_hostile_metadata_corruption_and_conflicts(linux_private, damage):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    writer.close()
    pending = path / "pending.json"
    if damage == "unknown":
        target = path / "unknown"
        target.write_bytes(b"x")
        target.chmod(0o600)
    elif damage == "oversize":
        pending.write_bytes(b"x" * (records.MAX_ARTIFACT_BYTES + 1))
    elif damage == "hardlink":
        os.link(pending, path / "copy")
    elif damage == "mode":
        pending.chmod(0o640)
    elif damage == "fifo":
        pending.unlink()
        os.mkfifo(pending, mode=0o600)
    elif damage == "symlink":
        pending.unlink()
        pending.symlink_to("missing")
    elif damage == "corrupt":
        pending.write_bytes(b"{broken")
    elif damage == "conflict":
        other_id = str(uuid.uuid4())
        (path / "observed.json").write_bytes(records._encode_artifact(other_id, "observed", observation()))
        (path / "observed.json").chmod(0o600)
    else:
        target = path / ".pending.not-a-uuid.tmp"
        target.write_bytes(b"")
        target.chmod(0o600)
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(path)


def test_linux_recognized_partial_temporary_is_validated_but_never_promoted(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    writer.close()
    temporary = path / f".observed.{uuid.uuid4().hex}.tmp"
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    assert records.read_exec_record(path).phase == "pending"
    temporary.write_bytes(b"x" * (records.MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(path)


def test_linux_reader_never_downgrades_malformed_later_phase_or_unbounded_entries(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    writer.publish_observed(observation())
    writer.close()
    (path / "observed.json").write_bytes(b"{malformed")
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(path)

    pending_only = reserve(linux_private, "many")
    many_path = linux_private[0] / "many"
    pending_only.close()
    for _index in range(9):
        temporary = many_path / f".pending.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(b"")
        temporary.chmod(0o600)
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(many_path)


def test_linux_reader_enforces_total_entry_bound_independently(monkeypatch, linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    writer.close()
    monkeypatch.setattr(records, "_MAX_TEMPORARIES", 32)
    for _index in range(records._MAX_DIRECTORY_ENTRIES):
        temporary = path / f".pending.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(b"")
        temporary.chmod(0o600)
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(path)


def test_linux_named_acl_is_rejected_when_filesystem_supports_posix_acls(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    writer.close()
    pending = path / "pending.json"
    # Linux POSIX ACL xattr v2: owner, named user, group, mask, other.
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identity)
        for tag, permissions, identity in (
            (1, 6, 0xFFFFFFFF),
            (2, 4, os.geteuid()),
            (4, 0, 0xFFFFFFFF),
            (16, 0, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        )
    )
    try:
        os.setxattr(pending, "system.posix_acl_access", acl)
    except OSError as error:
        pytest.skip(f"filesystem does not permit POSIX ACL test setup: errno {error.errno}")
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(path)


def test_linux_named_acl_on_new_artifact_fd_is_rejected(monkeypatch, linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    pending = path / "pending.json"
    pending_bytes = pending.read_bytes()
    pending_metadata = pending.lstat()
    pending_identity = (pending_metadata.st_dev, pending_metadata.st_ino)
    pending_acls = {
        name: os.getxattr(pending, name)
        for name in os.listxattr(pending)
        if name in {"system.posix_acl_access", "system.posix_acl_default"}
    }
    real_check = records._check_no_acl
    mutated = False
    target_identity = None
    target_entry_identity = None

    def add_acl_to_new_file(fd):
        nonlocal mutated, target_entry_identity, target_identity
        if stat.S_ISREG(os.fstat(fd).st_mode) and not mutated:
            temporaries = list(path.glob(".observed.*.tmp"))
            if temporaries:
                assert len(temporaries) == 1
                temporary_metadata = temporaries[0].lstat()
                descriptor_metadata = os.fstat(fd)
                temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
                descriptor_identity = (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                if descriptor_identity == temporary_identity:
                    try:
                        os.setxattr(fd, "system.posix_acl_access", named_user_acl())
                    except OSError as error:
                        pytest.skip(f"filesystem does not permit POSIX ACL test setup: errno {error.errno}")
                    mutated = True
                    target_entry_identity = temporary_identity
                    target_identity = descriptor_identity
        return real_check(fd)

    monkeypatch.setattr(records, "_check_no_acl", add_acl_to_new_file)
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_observed(observation())
    assert raised.value.stage == "observed" and mutated
    assert target_identity is not None and target_identity == target_entry_identity
    assert target_identity != pending_identity
    writer.close()
    assert pending.read_bytes() == pending_bytes
    current_pending = pending.lstat()
    assert (current_pending.st_dev, current_pending.st_ino) == pending_identity
    assert {
        name: os.getxattr(pending, name)
        for name in os.listxattr(pending)
        if name in {"system.posix_acl_access", "system.posix_acl_default"}
    } == pending_acls
    assert not (path / "observed.json").exists()


def test_linux_named_acl_mutation_after_write_before_publication_is_rejected(monkeypatch, linux_private):
    writer = reserve(linux_private)
    real_validate = records.OCIExecRecordWriter._validate_temporary
    mutated = False

    def mutate_before_validation(self, name, fd, identity, payload_size):
        nonlocal mutated
        if not mutated:
            try:
                os.setxattr(fd, "system.posix_acl_access", named_user_acl())
            except OSError as error:
                pytest.skip(f"filesystem does not permit POSIX ACL test setup: errno {error.errno}")
            mutated = True
        return real_validate(self, name, fd, identity, payload_size)

    monkeypatch.setattr(records.OCIExecRecordWriter, "_validate_temporary", mutate_before_validation)
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_observed(observation())
    assert raised.value.stage == "observed" and mutated
    writer.close()
    assert not (linux_private[0] / "record" / "observed.json").exists()


@pytest.mark.parametrize("boundary", ["parent-access", "parent-default", "record-access", "record-default"])
def test_linux_actual_named_and_default_directory_acls_are_rejected(linux_private, boundary):
    writer = reserve(linux_private)
    parent = linux_private[0]
    path = parent / "record"
    target = parent if boundary.startswith("parent") else path
    acl = f"d:u:{os.geteuid() + 1}:---" if boundary.endswith("default") else f"u:{os.geteuid() + 1}:---,m::---"
    try:
        result = subprocess.run(["setfacl", "-n", "-m", acl, str(target)], text=True, capture_output=True, check=False)
    except FileNotFoundError:
        pytest.skip("setfacl is unavailable")
    if result.returncode:
        pytest.skip(f"filesystem does not permit POSIX ACL test setup: {result.stderr.strip()}")
    with pytest.raises(records.OCIExecRecordError):
        writer.publish_observed(observation())
    writer.close()
    with pytest.raises(records.OCIExecRecordError):
        records.read_exec_record(path)


def test_linux_reader_revalidates_path_after_final_enumeration(monkeypatch, linux_private):
    writer = reserve(linux_private)
    writer.close()
    parent = linux_private[0]
    path = parent / "record"
    detached = parent / "detached-after-enumeration"
    real_enumerate = records._enumerate
    calls = 0

    def replace_after_enumeration(directory_fd):
        nonlocal calls
        result = real_enumerate(directory_fd)
        calls += 1
        if calls == 2:
            path.rename(detached)
            path.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(records, "_enumerate", replace_after_enumeration)
    with pytest.raises(records.OCIExecRecordError) as raised:
        records.read_exec_record(path)
    assert raised.value.stage == "changed"


def test_linux_replaced_temporary_is_neither_promoted_nor_cleaned(monkeypatch, linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    real_validate = records.OCIExecRecordWriter._validate_temporary
    validations = 0
    replacement = b"replacement-entry"

    def replace_before_final_validation(self, name, fd, identity, payload_size):
        nonlocal validations
        validations += 1
        if validations == 2:
            temporary = path / name
            temporary.unlink()
            temporary.write_bytes(replacement)
            temporary.chmod(0o600)
        return real_validate(self, name, fd, identity, payload_size)

    monkeypatch.setattr(records.OCIExecRecordWriter, "_validate_temporary", replace_before_final_validation)
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_observed(observation())
    assert raised.value.stage == "observed"
    writer.close()
    assert not (path / "observed.json").exists()
    temporaries = list(path.glob(".observed.*.tmp"))
    assert len(temporaries) == 1 and temporaries[0].read_bytes() == replacement


def test_linux_detached_writer_chain_cannot_publish_into_replacement(linux_private):
    writer = reserve(linux_private)
    parent = linux_private[0]
    path = parent / "record"
    detached = parent / "detached"
    path.rename(detached)
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    with pytest.raises(records.OCIExecRecordError):
        writer.publish_observed(observation())
    writer.close()
    assert not (path / "observed.json").exists()
    assert {item.name for item in detached.iterdir()} == {"pending.json"}


@pytest.mark.parametrize("boundary", ["parent", "record"])
def test_linux_writer_revalidates_current_private_directory_modes(linux_private, boundary):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    changed = linux_private[0] if boundary == "parent" else path
    changed.chmod(0o750)
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_observed(observation())
    assert raised.value.stage == "observed"
    writer.close()
    assert not (path / "observed.json").exists()


def test_linux_unrelated_sibling_changes_do_not_invalidate_pinned_chain(linux_private):
    writer = reserve(linux_private)
    sibling = linux_private[0].parent / "unrelated-sibling"
    sibling.mkdir(mode=0o700)
    writer.publish_observed(observation())
    writer.close()
    assert records.read_exec_record(linux_private[0] / "record").phase == "observed"


@pytest.mark.parametrize(
    ("predecessor", "damage"),
    [("pending", "delete"), ("pending", "corrupt"), ("pending", "replace"), ("observed", "delete")],
)
def test_linux_writer_blocks_missing_changed_or_replaced_predecessors(linux_private, predecessor, damage):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    if predecessor == "observed":
        writer.publish_observed(observation())
    artifact = path / f"{predecessor}.json"
    if damage == "delete":
        artifact.unlink()
    elif damage == "corrupt":
        artifact.write_bytes(b"{corrupt\n")
    else:
        replacement = path / "replacement"
        replacement.write_bytes(artifact.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, artifact)
    next_phase = "observed" if predecessor == "pending" else "confirmed"
    value = observation() if next_phase == "observed" else observation(acknowledgement="confirmed")
    publish = writer.publish_observed if next_phase == "observed" else writer.publish_confirmed
    with pytest.raises(records.OCIExecRecordError) as raised:
        publish(value)
    assert raised.value.stage == next_phase
    writer.close()
    assert not (path / f"{next_phase}.json").exists()


def test_linux_partial_write_is_completed_and_file_fsync_failure_poisoned(monkeypatch, linux_private):
    writer = reserve(linux_private)
    real_write = records.os.write
    monkeypatch.setattr(records.os, "write", lambda fd, value: real_write(fd, value[: max(1, len(value) // 2)]))
    writer.publish_observed(observation())
    writer.close()
    assert records.read_exec_record(linux_private[0] / "record").phase == "observed"

    failed = reserve(linux_private, "failed")
    real_fsync = records.os.fsync

    def fail_regular(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("/private/secret")
        return real_fsync(fd)

    monkeypatch.setattr(records.os, "write", real_write)
    monkeypatch.setattr(records.os, "fsync", fail_regular)
    with pytest.raises(records.OCIExecRecordError) as error:
        failed.publish_observed(observation())
    assert error.value.stage == "observed" and "/private/secret" not in str(error.value)
    with pytest.raises(records.OCIExecRecordError, match="uncertain"):
        failed.publish_observed(observation())
    failed.close()


def test_linux_failure_cleanup_preserves_replaced_temporary_entry(monkeypatch, linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    real_fsync = records.os.fsync
    replacement = b"replacement-owned-by-someone-else"

    def replace_then_fail(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            temporary = next(path.glob(".observed.*.tmp"))
            temporary.unlink()
            temporary.write_bytes(replacement)
            temporary.chmod(0o600)
            raise OSError("publication failed")
        return real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", replace_then_fail)
    with pytest.raises(records.OCIExecRecordError):
        writer.publish_observed(observation())
    writer.close()
    temporaries = list(path.glob(".observed.*.tmp"))
    assert len(temporaries) == 1 and temporaries[0].read_bytes() == replacement


def test_linux_directory_and_parent_fsync_failures_are_not_downgraded(monkeypatch, linux_private):
    writer = reserve(linux_private)
    real_fsync = records.os.fsync
    record_identity = (linux_private[0] / "record").stat()

    def fail_next_directory(fd):
        metadata = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == (record_identity.st_dev, record_identity.st_ino):
            raise OSError("directory secret")
        return real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", fail_next_directory)
    with pytest.raises(records.OCIExecRecordError) as error:
        writer.publish_observed(observation())
    assert error.value.stage == "observed"
    writer.close()
    assert (linux_private[0] / "record" / "observed.json").exists()
    assert records.read_exec_record(linux_private[0] / "record").phase == "observed"

    parent_identity = linux_private[0].stat()

    def fail_parent(fd):
        metadata = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == (parent_identity.st_dev, parent_identity.st_ino):
            raise OSError("parent secret")
        return real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", fail_parent)
    with pytest.raises(records.OCIExecRecordError) as reserve_error:
        records.OCIExecRecordWriter.reserve(linux_private[0] / "parent-failed", managed_state=linux_private[1])
    assert reserve_error.value.stage == "reserve"


@pytest.mark.parametrize("barrier", ["file", "directory"])
def test_linux_confirmed_barrier_failure_preserves_prior_artifacts(monkeypatch, linux_private, barrier):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    unconfirmed = observation()
    writer.publish_observed(unconfirmed)
    pending_payload = (path / "pending.json").read_bytes()
    observed_payload = (path / "observed.json").read_bytes()
    record_identity = path.stat()
    real_fsync = records.os.fsync

    def fail_selected_barrier(fd):
        metadata = os.fstat(fd)
        is_record_directory = (metadata.st_dev, metadata.st_ino) == (
            record_identity.st_dev,
            record_identity.st_ino,
        )
        if barrier == "file" and stat.S_ISREG(metadata.st_mode):
            raise OSError("confirmed file sync secret")
        if barrier == "directory" and is_record_directory:
            raise OSError("confirmed directory sync secret")
        return real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", fail_selected_barrier)
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_confirmed(replace(unconfirmed, acknowledgement="confirmed"))
    assert raised.value.stage == "confirmed" and "secret" not in str(raised.value)
    assert (path / "pending.json").read_bytes() == pending_payload
    assert (path / "observed.json").read_bytes() == observed_payload
    assert (path / "confirmed.json").exists() is (barrier == "directory")
    assert records.read_exec_record(path).phase == ("confirmed" if barrier == "directory" else "observed")
    with pytest.raises(records.OCIExecRecordError, match="uncertain"):
        writer.publish_confirmed(replace(unconfirmed, acknowledgement="confirmed"))
    writer.close()


def test_linux_confirmed_mismatch_refuses_without_overwriting_observed(linux_private):
    writer = reserve(linux_private)
    path = linux_private[0] / "record"
    unconfirmed = observation()
    writer.publish_observed(unconfirmed)
    observed_payload = (path / "observed.json").read_bytes()
    mismatched = replace(unconfirmed, acknowledgement="confirmed", stdout_bytes=unconfirmed.stdout_bytes + 1)

    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_confirmed(mismatched)

    assert raised.value.stage == "confirmed"
    assert (path / "observed.json").read_bytes() == observed_payload
    assert not (path / "confirmed.json").exists()
    writer.close()


def test_linux_sync_order_includes_initial_parent_barrier_and_publication_barriers(monkeypatch, linux_private):
    parent, managed = linux_private
    path = parent / "ordered"
    parent_identity = parent.stat()
    events = []
    real_fsync = records.os.fsync
    real_rename = records._rename_noreplace

    def record_fsync(fd):
        metadata = os.fstat(fd)
        if stat.S_ISREG(metadata.st_mode):
            events.append("file-fsync")
        elif (metadata.st_dev, metadata.st_ino) == (parent_identity.st_dev, parent_identity.st_ino):
            events.append("parent-fsync")
        elif path.exists() and (metadata.st_dev, metadata.st_ino) == (path.stat().st_dev, path.stat().st_ino):
            events.append("record-fsync")
        return real_fsync(fd)

    def record_rename(directory_fd, source, target):
        events.append(f"rename-{target}")
        return real_rename(directory_fd, source, target)

    monkeypatch.setattr(records.os, "fsync", record_fsync)
    monkeypatch.setattr(records, "_rename_noreplace", record_rename)
    writer = records.OCIExecRecordWriter.reserve(path, managed_state=managed)
    assert events.index("record-fsync") < events.index("parent-fsync") < events.index("rename-pending.json")

    events.clear()
    writer.publish_observed(observation())
    assert events.index("file-fsync") < events.index("rename-observed.json") < events.index("record-fsync")
    writer.close()


def test_linux_interrupt_propagates_closes_and_close_is_idempotent(monkeypatch, linux_private):
    before = open_fd_count()
    writer = reserve(linux_private)

    def interrupt(_fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(records.os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        writer.publish_observed(observation())
    assert writer._closed
    assert open_fd_count() == before
    writer.close()
    with pytest.raises(records.OCIExecRecordError, match="closed"):
        writer.publish_observed(observation())


@pytest.mark.parametrize("stage", ["observed", "confirmed"])
def test_linux_arbitrary_base_exception_propagates_unchanged_and_closes(monkeypatch, linux_private, stage):
    class Cancelled(BaseException):
        pass

    before = open_fd_count()
    writer = reserve(linux_private)
    if stage == "confirmed":
        writer.publish_observed(observation())
    interruption = Cancelled()
    if stage == "observed":
        monkeypatch.setattr(records.os, "write", lambda *_args: (_ for _ in ()).throw(interruption))

        def publish():
            writer.publish_observed(observation())

    else:
        monkeypatch.setattr(
            records, "_validate_observation", lambda *_args, **_kwargs: (_ for _ in ()).throw(interruption)
        )

        def publish():
            writer.publish_confirmed(observation(acknowledgement="confirmed"))

    with pytest.raises(Cancelled) as raised:
        publish()
    assert raised.value is interruption and writer._closed
    assert open_fd_count() == before


def test_linux_publication_validation_errors_are_fixed_and_path_free(monkeypatch, linux_private):
    writer = reserve(linux_private)
    monkeypatch.setattr(records, "_encode_artifact", lambda *_args: (_ for _ in ()).throw(ValueError("/secret")))
    with pytest.raises(records.OCIExecRecordError) as raised:
        writer.publish_observed(observation())
    assert raised.value.stage == "observed" and "/secret" not in str(raised.value)
    writer.close()


def test_linux_writer_rejects_fork_and_releases_descriptors(linux_private):
    before = len(os.listdir("/proc/self/fd"))
    writer = reserve(linux_private)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            writer.publish_observed(observation())
        except records.OCIExecRecordError as error:
            os.write(write_fd, error.stage.encode())
        finally:
            writer.close()
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 32)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert status == 0 and result == b"forked"
    writer.close()
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize(
    ("published_phase", "boundary", "expected_phase"),
    [
        ("observed", "file-fsync", "pending"),
        ("observed", "rename", "observed"),
        ("confirmed", "file-fsync", "observed"),
        ("confirmed", "rename", "confirmed"),
    ],
)
def test_linux_process_kill_at_publication_boundaries_preserves_conservative_snapshot(
    linux_private, published_phase, boundary, expected_phase
):
    parent, managed = linux_private
    path = parent / f"{published_phase}-{boundary}"
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            writer = records.OCIExecRecordWriter.reserve(path, managed_state=managed)
            unconfirmed = observation()
            if published_phase == "confirmed":
                writer.publish_observed(unconfirmed)
            if boundary == "file-fsync":
                real_fsync = records.os.fsync

                def stop_after_file_fsync(fd):
                    result = real_fsync(fd)
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        os.write(write_fd, b"ready")
                        os.kill(os.getpid(), signal.SIGSTOP)
                    return result

                records.os.fsync = stop_after_file_fsync
            else:
                real_rename = records._rename_noreplace

                def stop_after_rename(directory_fd, source, target):
                    real_rename(directory_fd, source, target)
                    os.write(write_fd, b"ready")
                    os.kill(os.getpid(), signal.SIGSTOP)

                records._rename_noreplace = stop_after_rename
            if published_phase == "observed":
                writer.publish_observed(unconfirmed)
            else:
                writer.publish_confirmed(replace(unconfirmed, acknowledgement="confirmed"))
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(97)

    os.close(write_fd)
    reaped = False
    try:
        readable, _, _ = select.select([read_fd], [], [], 5.0)
        assert readable, "child did not reach publication boundary before deadline"
        assert os.read(read_fd, 5) == b"ready"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        reaped = True
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    finally:
        os.close(read_fd)
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                waited, _status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    reaped = True
                    break
                time.sleep(0.01)
            if not reaped:
                pytest.fail("test child could not be reaped")
    assert records.read_exec_record(path).phase == expected_phase


def test_linux_record_storage_never_constructs_or_uses_original_client(monkeypatch, linux_private):
    from palimpsest_local import oci_exec_session

    def forbidden(*_args, **_kwargs):
        raise AssertionError("live client/session API was called")

    monkeypatch.setattr(oci_exec_session, "MonitorClient", forbidden)
    monkeypatch.setattr(oci_exec_session, "locked_existing_run", forbidden)
    writer = reserve(linux_private)
    writer.publish_observed(observation())
    writer.close()
    assert records.read_exec_record(linux_private[0] / "record").phase == "observed"
