"""OCI-root writable-volume ownership and retention contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local import oci_root_volume, project_volumes, state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_guest_filesystems import ext4_primary_superblock_checksum
from palimpsest_local.oci_store import ArtifactLeaseOwner

MIB = 1024 * 1024
VOLUME_SIZE = 16 * MIB
GRAPH_A = "sha256:" + "a" * 64
GRAPH_B = "sha256:" + "b" * 64


@pytest.fixture
def roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots(
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )


def _owner(name: str = "demo", run_id: str | None = None) -> ArtifactLeaseOwner:
    return ArtifactLeaseOwner(run_id or str(uuid.uuid4()), name, "root-lower")


def _completed(argv: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, stdout, "")


def _write_ext4_magic(path: Path, label: str, filesystem_uuid: str) -> None:
    superblock = bytearray(1024)
    superblock[0:4] = (4096).to_bytes(4, "little")
    superblock[4:8] = (VOLUME_SIZE // 4096).to_bytes(4, "little")
    superblock[24:28] = (2).to_bytes(4, "little")
    superblock[32:36] = (32768).to_bytes(4, "little")
    superblock[40:44] = (4096).to_bytes(4, "little")
    superblock[56:58] = b"\x53\xef"
    superblock[76:80] = (1).to_bytes(4, "little")
    superblock[88:90] = (256).to_bytes(2, "little")
    superblock[96:100] = (0x42).to_bytes(4, "little")
    superblock[100:104] = (0x400).to_bytes(4, "little")
    superblock[104:120] = uuid.UUID(filesystem_uuid).bytes
    superblock[120:136] = label.encode("ascii").ljust(16, b"\0")
    superblock[0x175] = 1
    superblock[1020:1024] = ext4_primary_superblock_checksum(bytes(superblock)).to_bytes(4, "little")
    with path.open("r+b") as stream:
        stream.seek(1024)
        stream.write(superblock)
        stream.flush()
        os.fsync(stream.fileno())


class FakeKvmTools:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.info_format = "raw"
        self.backing_file: str | None = None
        self.bad_checksum_type = False

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv == ["qemu-img", "--version"]:
            return _completed(argv, "qemu-img version 9.2\n")
        if argv == ["mkfs.ext4", "-V"]:
            return _completed(argv, "mke2fs 1.47\n")
        if argv[0] == "mkfs.ext4":
            path = Path(argv[-1])
            _write_ext4_magic(path, argv[argv.index("-L") + 1], argv[argv.index("-U") + 1])
            if self.bad_checksum_type:
                with path.open("r+b") as stream:
                    stream.seek(1024)
                    superblock = bytearray(stream.read(1024))
                    superblock[0x175] = 0
                    superblock[1020:1024] = ext4_primary_superblock_checksum(bytes(superblock)).to_bytes(4, "little")
                    stream.seek(1024)
                    stream.write(superblock)
            return _completed(argv)
        if argv[:3] == ["qemu-img", "info", "--output=json"]:
            path = Path(argv[-1])
            value: dict[str, object] = {"format": self.info_format, "virtual-size": path.stat().st_size}
            if self.backing_file is not None:
                value["backing-filename"] = self.backing_file
            return _completed(argv, json.dumps(value))
        raise AssertionError(f"unexpected command: {argv}")


def _claim(
    roots: state.StatePaths,
    tools: FakeKvmTools,
    owner: ArtifactLeaseOwner,
    *,
    volume_id: str | None = None,
    graph: str = GRAPH_A,
    retention: str = "delete",
) -> oci_root_volume.ClaimedOCIRootVolume:
    return oci_root_volume.claim_oci_root_volume(
        roots,
        volume_id or str(uuid.uuid4()),
        size_bytes=VOLUME_SIZE,
        lower_graph_digest=graph,
        retention_policy=retention,
        owner=owner,
        runner=tools,
    )


def test_new_root_volume_is_formatted_owned_and_idempotently_claimed(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    owner = _owner()
    volume_id = str(uuid.uuid4())

    first = _claim(roots, tools, owner, volume_id=volume_id)
    inode = first.path.stat().st_ino
    second = _claim(roots, tools, owner, volume_id=volume_id)

    assert first.created is True
    assert second.created is False
    assert second.claimed_from_retained is False
    assert second.path.stat().st_ino == inode
    assert second.record == first.record
    assert state.permission_bits(first.path) == 0o600
    assert first.path.stat().st_size == VOLUME_SIZE
    mkfs_call = next(call for call in tools.calls if call[0] == "mkfs.ext4" and "-U" in call)
    assert mkfs_call[mkfs_call.index("-U") + 1] == volume_id
    assert [mkfs_call[index + 1] for index, item in enumerate(mkfs_call) if item == "-O"] == [
        project_volumes.OCI_ROOT_EXT4_FEATURES
    ]
    assert mkfs_call[mkfs_call.index("-E") + 1] == project_volumes.OCI_ROOT_EXT4_EXTENDED_OPTIONS
    assert mkfs_call[mkfs_call.index("-b") + 1] == "4096"
    assert mkfs_call[mkfs_call.index("-I") + 1] == "256"
    assert mkfs_call[mkfs_call.index("-g") + 1] == "32768"
    assert mkfs_call[mkfs_call.index("-i") + 1] == "16384"
    assert oci_root_volume.load_oci_root_volume(roots, volume_id, runner=tools).record == first.record
    assert oci_root_volume.list_oci_root_volume_records(roots) == (first.record,)
    with pytest.raises(StateError, match="generation"):
        replace(first.record, generation=oci_root_volume.MAX_OCI_ROOT_VOLUME_GENERATION + 1)


@pytest.mark.parametrize("swap_kind", ["rename", "symlink"])
def test_root_volume_load_rejects_same_uid_path_swap_after_external_probe(
    roots: state.StatePaths,
    monkeypatch: pytest.MonkeyPatch,
    swap_kind: str,
) -> None:
    tools = FakeKvmTools()
    claimed = _claim(roots, tools, _owner())
    path = claimed.path
    saved = path.with_name(path.name + ".original")
    replacement = path.with_name(path.name + ".replacement")
    shutil.copyfile(path, replacement)
    replacement.chmod(0o600)
    original_verify = oci_root_volume._verify_kvm_path

    def swap_after_probe(*args: object, **kwargs: object) -> None:
        original_verify(*args, **kwargs)  # type: ignore[arg-type]
        os.replace(path, saved)
        if swap_kind == "rename":
            os.replace(replacement, path)
        else:
            path.symlink_to(saved.name)

    monkeypatch.setattr(oci_root_volume, "_verify_kvm_path", swap_after_probe)
    with pytest.raises(StateError, match="changed during verification"):
        oci_root_volume.load_oci_root_volume(roots, claimed.record.volume_id, runner=tools)

    if path.is_symlink() or path.exists():
        path.unlink()
    os.replace(saved, path)
    replacement.unlink(missing_ok=True)


def test_new_root_policy_failure_rolls_back_record_and_unpublished_data(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    tools.bad_checksum_type = True
    volume_id = str(uuid.uuid4())

    with pytest.raises(StateError, match="pinned policy"):
        _claim(roots, tools, _owner(), volume_id=volume_id)

    assert oci_root_volume.list_oci_root_volume_records(roots) == ()
    assert not any(path.name.endswith(".raw") for path in roots.oci_root_volumes.iterdir())


def test_new_root_owner_intent_is_durable_before_raw_creation(
    roots: state.StatePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = FakeKvmTools()
    owner = _owner()
    volume_id = str(uuid.uuid4())
    record_path = roots.oci_root_volumes / f"{volume_id.replace('-', '')}.json"
    original_ensure = oci_root_volume._ensure_ext4_raw_file_locked

    def observe_intent(*args: object, **kwargs: object) -> bool:
        value = state.read_json(record_path)
        assert value["status"] == "creating"
        assert value["attached_run_id"] == owner.run_id
        return original_ensure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(oci_root_volume, "_ensure_ext4_raw_file_locked", observe_intent)
    claimed = _claim(roots, tools, owner, volume_id=volume_id)

    assert claimed.record.status == "attached"
    assert claimed.record.generation == 2


def test_attached_volume_rejects_other_owner_and_lower_graph(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    volume_id = str(uuid.uuid4())
    _claim(roots, tools, _owner("first"), volume_id=volume_id)

    with pytest.raises(StateError, match="retained volume conflicts|attached to another"):
        _claim(roots, tools, _owner("second"), volume_id=volume_id)
    with pytest.raises(StateError, match="lower graph"):
        _claim(roots, tools, _owner("second"), volume_id=volume_id, graph=GRAPH_B)


def test_retained_volume_can_be_reused_only_for_the_same_graph(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    first_owner = _owner("first")
    second_owner = _owner("second")
    volume_id = str(uuid.uuid4())
    _claim(roots, tools, first_owner, volume_id=volume_id, retention="retain")

    retained = oci_root_volume.release_oci_root_volume(
        roots,
        volume_id,
        owner=first_owner,
        lower_graph_digest=GRAPH_A,
        runner=tools,
    )
    assert retained is not None and retained.status == "retained"

    with pytest.raises(StateError, match="lower graph"):
        _claim(roots, tools, second_owner, volume_id=volume_id, graph=GRAPH_B, retention="retain")
    reused = _claim(roots, tools, second_owner, volume_id=volume_id, retention="retain")
    assert reused.claimed_from_retained is True
    assert reused.record.generation == retained.generation + 1
    assert reused.record.attached_run_id == second_owner.run_id


def test_delete_release_is_idempotent_and_removes_record_and_bytes(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    owner = _owner()
    claimed = _claim(roots, tools, owner)

    assert (
        oci_root_volume.release_oci_root_volume(
            roots,
            claimed.record.volume_id,
            owner=owner,
            lower_graph_digest=GRAPH_A,
            runner=tools,
        )
        is None
    )
    assert not claimed.path.exists()
    assert (
        oci_root_volume.release_oci_root_volume(
            roots,
            claimed.record.volume_id,
            owner=owner,
            lower_graph_digest=GRAPH_A,
            runner=tools,
        )
        is None
    )


def test_delete_retry_removes_durable_quarantine_after_rename_crash(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    owner = _owner()
    claimed = _claim(roots, tools, owner)
    stem = claimed.record.volume_id.replace("-", "")
    record_path = roots.oci_root_volumes / f"{stem}.json"
    quarantine = roots.oci_root_volumes / f".{stem}-deleting.raw"
    deleting = replace(claimed.record, status="deleting", generation=claimed.record.generation + 1)
    state.atomic_write_json(record_path, deleting.to_dict())
    os.replace(claimed.path, quarantine)
    state.fsync_directory(roots.oci_root_volumes)

    result = oci_root_volume.release_oci_root_volume(
        roots,
        claimed.record.volume_id,
        owner=owner,
        lower_graph_digest=GRAPH_A,
        runner=tools,
    )

    assert result is None
    assert not quarantine.exists()
    assert not record_path.exists()
    assert not claimed.path.exists()


def test_rollback_preserves_preexisting_claim_and_restores_retained_reuse(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    owner = _owner("same")
    same = _claim(roots, tools, owner)
    retry = _claim(roots, tools, owner, volume_id=same.record.volume_id)

    oci_root_volume.rollback_oci_root_volume_claim(roots, retry, owner=owner, runner=tools)
    assert oci_root_volume.load_oci_root_volume(roots, same.record.volume_id, runner=tools).record.status == "attached"

    retained_owner = _owner("old")
    reuse_owner = _owner("new")
    retained_claim = _claim(roots, tools, retained_owner, retention="retain")
    oci_root_volume.release_oci_root_volume(
        roots,
        retained_claim.record.volume_id,
        owner=retained_owner,
        lower_graph_digest=GRAPH_A,
        runner=tools,
    )
    reused = _claim(
        roots,
        tools,
        reuse_owner,
        volume_id=retained_claim.record.volume_id,
        retention="retain",
    )
    oci_root_volume.rollback_oci_root_volume_claim(roots, reused, owner=reuse_owner, runner=tools)
    restored = oci_root_volume.load_oci_root_volume(roots, reused.record.volume_id, runner=tools).record
    assert restored.status == "retained"
    assert restored.attached_run_id is None


def test_records_fail_closed_on_noncanonical_json_and_orphan_raw(roots: state.StatePaths) -> None:
    tools = FakeKvmTools()
    claimed = _claim(roots, tools, _owner())
    record_path = roots.oci_root_volumes / f"{claimed.record.volume_id.replace('-', '')}.json"
    value = json.loads(record_path.read_text())
    record_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.chmod(record_path, 0o600)

    with pytest.raises(StateError, match="canonical JSON"):
        oci_root_volume.load_oci_root_volume(roots, claimed.record.volume_id, runner=tools)
    state.atomic_write_json(record_path, value)

    other_id = str(uuid.uuid4())
    orphan = roots.oci_root_volumes / f"{other_id.replace('-', '')}.raw"
    orphan.touch(mode=0o600)
    with pytest.raises(StateError, match="inconsistent"):
        oci_root_volume.list_oci_root_volume_records(roots)


@pytest.mark.parametrize("unsafe", ["format", "backing"])
def test_existing_physical_root_must_be_safe_raw_ext4(
    roots: state.StatePaths,
    unsafe: str,
) -> None:
    tools = FakeKvmTools()
    claimed = _claim(roots, tools, _owner())
    if unsafe == "format":
        tools.info_format = "qcow2"
    else:
        tools.backing_file = "/tmp/base.raw"

    with pytest.raises(StateError, match="raw|backing"):
        oci_root_volume.load_oci_root_volume(roots, claimed.record.volume_id, runner=tools)
