"""Boot exports seal private copies before the domain plan selects paths."""

import json
import os
import stat
from types import SimpleNamespace

import pytest
import test_oci_store as fixtures

from palimpsest_local import oci_boot_exports as exports
from palimpsest_local import state
from palimpsest_local.errors import StateError


@pytest.fixture
def case(tmp_path):
    roots, store = fixtures._short_oci_store()
    runner = fixtures._RootVolumeTools()
    kernel = tmp_path / "source-kernel"
    data = bytearray(0x206)
    data[0x202:0x206] = b"HdrS"
    kernel.write_bytes(data)
    initramfs = tmp_path / "source-initramfs"
    initramfs.write_bytes(b"070701payload")
    for path in (kernel, initramfs):
        path.chmod(0o600)
    boot = fixtures.verify_host_boot_artifacts(kernel.resolve(), initramfs.resolve())
    profile = fixtures.platforms.resolve_domain_profile(fixtures.platforms.BACKEND_KVM, "x86_64")
    name = "boot-export-test"
    with state.reserve_new_run(roots, name, fixtures._oci_dispatch()) as reservation:
        prepared = fixtures.prepare_oci_root_run(
            reservation,
            fixtures._image_materialization(store),
            store,
            root_volume_size_bytes=fixtures._ROOT_VOLUME_SIZE,
            runner=runner,
        )
    conn = fixtures._DefinitionConnection()
    conn.getCapabilities = lambda: (
        "<capabilities><host><secmodel><model>dac</model><doi>0</doi>"
        '<baselabel type="kvm">+12345:+12346</baselabel></secmodel></host></capabilities>'
    )
    root = roots.runs / name
    return SimpleNamespace(
        roots=roots,
        store=store,
        runner=runner,
        boot=boot,
        source_boot=boot,
        profile=profile,
        prepared=prepared,
        conn=conn,
        name=name,
        run_root=root,
        state=root / "state.json",
        export_paths={"kernel": root / "boot-kernel", "initramfs": root / "boot-initramfs"},
    )


def publish(case, **kwargs):
    return exports.publish_oci_boot_exports(case.roots, case.prepared, case.source_boot, conn=case.conn, **kwargs)


def load(case):
    return exports.load_oci_boot_exports(case.roots, case.name)


def _snapshot(path):
    info = path.stat(follow_symlinks=False)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        path.read_bytes(),
    )


def test_export_pair_is_copied_and_sealed_without_touching_sources(case):
    source = {role: _snapshot(getattr(case.source_boot, role).path) for role in case.export_paths}
    receipt = publish(case)
    assert receipt.phase == "ready"
    assert (receipt.qemu_uid, receipt.qemu_gid) == (12345, 12346)
    assert exports.BootExportReceipt.from_dict(receipt.to_dict()) == receipt
    selected = load(case)
    assert selected.to_dict() == case.source_boot.to_dict()
    for role, path in case.export_paths.items():
        original = getattr(case.source_boot, role)
        target = getattr(receipt, role)
        assert _snapshot(original.path) == source[role]
        assert path.read_bytes() == original.path.read_bytes()
        assert path.stat().st_ino != original.inode
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
        assert path.stat().st_nlink == 1
        assert (target.device, target.inode) == _snapshot(path)[:2]
        assert target.digest == original.digest and target.size_bytes == original.size_bytes
        assert getattr(selected, role).path == path
        assert str(original.path) not in json.dumps(receipt.to_dict())


def test_completed_exports_plan_without_original_sources(case):
    receipt = publish(case)
    for role in case.export_paths:
        getattr(case.source_boot, role).path.unlink()
    selected = load(case)
    assert publish(case) == receipt
    plan = fixtures.build_oci_root_domain_plan(
        case.roots,
        case.prepared,
        case.store,
        selected,
        case.profile,
        runner=case.runner,
    )
    committed = fixtures.commit_oci_root_domain_plan(case.roots, plan, case.store, runner=case.runner)
    assert committed.boot_artifacts == selected.to_dict()


def test_ready_replay_does_not_rewrite_export_inodes_or_ledger(case, monkeypatch):
    receipt = publish(case)
    before = {role: _snapshot(path) for role, path in case.export_paths.items()}
    ledger = case.state.read_bytes()
    identities = {value[:2] for value in before.values()}
    original_fsync = os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) not in identities
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    assert publish(case) == receipt
    assert {role: _snapshot(path) for role, path in case.export_paths.items()} == before
    assert case.state.read_bytes() == ledger


@pytest.mark.parametrize("role", ["kernel", "initramfs"])
@pytest.mark.parametrize("kind", ["file", "symlink", "hardlink"])
def test_unowned_reserved_target_is_never_adopted(case, role, kind):
    target = case.export_paths[role]
    if kind == "file":
        target.write_bytes(b"unowned reserved target")
        target.chmod(0o600)
    elif kind == "symlink":
        target.symlink_to(getattr(case.source_boot, role).path)
    else:
        target.hardlink_to(getattr(case.source_boot, role).path)
    before = _snapshot(target)
    with pytest.raises(StateError):
        publish(case)
    assert _snapshot(target) == before


@pytest.mark.parametrize("role", ["kernel", "initramfs"])
@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_sealed_file_fsync_crash_reuses_only_recorded_pair_inodes(case, monkeypatch, role, mode):
    original_fsync = os.fsync
    fired = False

    def fsync(fd):
        nonlocal fired
        info = os.fstat(fd)
        path = case.export_paths[role]
        if (
            path.exists()
            and info.st_size > 0
            and stat.S_IMODE(info.st_mode) == mode
            and (info.st_dev, info.st_ino) == _snapshot(path)[:2]
            and not fired
        ):
            fired = True
            raise OSError("boot export fsync interrupted")
        return original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(StateError):
            publish(case)
    assert fired
    intent = json.loads(case.state.read_bytes())["oci_boot_exports"]
    assert intent["phase"] == "intent"
    before = {name: _snapshot(path)[:2] for name, path in case.export_paths.items()}
    sealed = {name: _snapshot(path) for name, path in case.export_paths.items() if path.stat().st_mode & 0o777 == 0o400}
    assert (role in sealed) == (mode == 0o400)
    synced = set()

    def retry_fsync(fd):
        identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        if identity in before.values():
            synced.add(identity)
        return original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", retry_fsync)
        receipt = publish(case)
    assert receipt.phase == "ready" and receipt.export_id == intent["export_id"]
    assert {name: _snapshot(path)[:2] for name, path in case.export_paths.items()} == before
    assert synced == set(before.values())
    assert {name: _snapshot(case.export_paths[name]) for name in sealed} == sealed
    load(case)


def test_stale_preparation_cannot_replay_new_run_ready_exports(case):
    publish(case)
    old_prepared = case.prepared
    case.run_root.rename(case.run_root.with_name("previous-run-evidence"))
    with state.reserve_new_run(case.roots, case.name, fixtures._oci_dispatch()) as reservation:
        case.prepared = fixtures.prepare_oci_root_run(
            reservation,
            fixtures._image_materialization(case.store),
            case.store,
            root_volume_size_bytes=fixtures._ROOT_VOLUME_SIZE,
            runner=case.runner,
        )
    receipt = publish(case)
    assert receipt.run_id != old_prepared.transaction.owner.run_id
    before = {role: _snapshot(path) for role, path in case.export_paths.items()}, case.state.read_bytes()
    with pytest.raises(StateError):
        exports.publish_oci_boot_exports(case.roots, old_prepared, case.source_boot, conn=case.conn)
    assert ({role: _snapshot(path) for role, path in case.export_paths.items()}, case.state.read_bytes()) == before


@pytest.mark.parametrize("role", ["kernel", "initramfs"])
def test_partial_copy_cannot_resume_into_replaced_inode(case, monkeypatch, role):
    original_write = os.write
    fired = False

    def write(fd, data):
        nonlocal fired
        info = os.fstat(fd)
        path = case.export_paths[role]
        if path.exists() and (info.st_dev, info.st_ino) == _snapshot(path)[:2]:
            fired = True
            original_write(fd, data[:1])
            raise OSError("interrupted boot copy")
        return original_write(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", write)
        with pytest.raises(StateError):
            publish(case)
    assert fired
    assert json.loads(case.state.read_bytes())["oci_boot_exports"]["phase"] == "intent"
    target = case.export_paths[role]
    target.rename(target.with_suffix(".interrupted"))
    target.write_bytes(getattr(case.source_boot, role).path.read_bytes())
    target.chmod(0o600)
    before = _snapshot(target), case.state.read_bytes()
    with pytest.raises(StateError):
        publish(case)
    assert (_snapshot(target), case.state.read_bytes()) == before


@pytest.mark.parametrize("damage", ["inode", "hardlink", "bytes", "owner-write"])
@pytest.mark.parametrize("role", ["kernel", "initramfs"])
def test_ready_pair_rejects_identity_content_or_sealing_drift(case, role, damage):
    publish(case)
    path = case.export_paths[role]
    if damage == "inode":
        original = path.with_suffix(".original")
        path.rename(original)
        path.write_bytes(original.read_bytes())
        path.chmod(0o400)
    elif damage == "hardlink":
        path.with_suffix(".link").hardlink_to(path)
    else:
        path.chmod(0o600)
        if damage == "bytes":
            with path.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                value = stream.read(1)
                stream.seek(-1, os.SEEK_END)
                stream.write(bytes([value[0] ^ 1]))
            path.chmod(0o400)
    before = _snapshot(path), case.state.read_bytes()
    with pytest.raises(StateError):
        load(case)
    with pytest.raises(StateError):
        publish(case)
    assert (_snapshot(path), case.state.read_bytes()) == before
