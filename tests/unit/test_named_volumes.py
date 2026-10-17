"""Independent disk ownership, exclusive lifecycle and real byte retention."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from palimpsest_local import named_volumes as volumes
from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.runtime_types import RunVolumeIntent

_SIZE = 16 * 1024 * 1024


@pytest.fixture
def disks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roots = state.StatePaths(tmp_path / "config", tmp_path / "state")
    formats = []
    domains = []

    def format_once(path, size, label, name, runner, *, filesystem_uuid=None, **kwargs):
        assert not path.exists(), "formatter must never touch an existing data disk"
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.ftruncate(descriptor, size)
            os.pwrite(descriptor, b"\x53\xef", 1024 + 56)
            os.pwrite(descriptor, (0x40).to_bytes(4, "little"), 1024 + 96)
            os.pwrite(descriptor, label.encode().ljust(16, b"\0"), 1024 + 120)
            os.pwrite(descriptor, uuid.UUID(filesystem_uuid).bytes, 1024 + 104)
        finally:
            os.close(descriptor)
        formats.append(path)
        return True

    def runner(argv):
        assert argv[:3] == ["qemu-img", "info", "--output=json"]
        path = Path(argv[-1])
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"format": "raw", "virtual-size": path.stat().st_size}), ""
        )

    monkeypatch.setattr(volumes.physical, "_ensure_ext4_raw_file_locked", format_once)
    monkeypatch.setattr(volumes.physical, "_default_runner", runner)
    monkeypatch.setattr(volumes.physical, "preflight_kvm_volume_support", lambda **_kwargs: None)
    monkeypatch.setattr(volumes.physical, "preflight_lima_volume_support", lambda **_kwargs: None)
    monkeypatch.setattr(
        volumes.physical, "_qemu_info", lambda path, _runner: {"format": "raw", "virtual-size": path.stat().st_size}
    )
    monkeypatch.setattr(
        volumes.kvm, "connect", lambda uri: SimpleNamespace(listAllDomains=lambda flags: domains, close=lambda: None)
    )
    monkeypatch.setattr(volumes.kvm, "_libvirt", lambda: SimpleNamespace(VIR_DOMAIN_XML_INACTIVE=2))
    return roots, runner, formats, domains


def create(disks, name="data", backend="kvm"):
    roots, runner, _, _ = disks
    return volumes.create_named_volume(roots, name=name, backend=backend, size_bytes=_SIZE, runner=runner)


def reserve(disks, *, name="first", run_id="pending-first", disk="data", read_only=False):
    roots, runner, _, _ = disks
    return volumes.reserve_named_volumes(
        roots,
        name=name,
        run_id=run_id,
        backend="kvm",
        intents=(RunVolumeIntent(disk, "/srv/data", read_only=read_only),),
        runner=runner,
    )


def test_empty_inventory_is_read_only_and_disks_exist_without_vms(disks):
    roots, _, formats, _ = disks
    assert volumes.list_named_volumes(roots) == []
    assert not roots.state.exists()
    row = create(disks)
    assert row["id"] == "named:kvm:data"
    assert row["status"] == "available"
    assert row["attachments"] == []
    assert not roots.runs.exists()
    assert volumes.list_named_volumes(roots) == [row]
    assert len(formats) == 1


def test_two_sequential_vm_lifecycles_keep_sentinel_and_format_once(disks, monkeypatch):
    roots, runner, formats, _ = disks
    create(disks)
    attachment = reserve(disks)[0]
    descriptor = os.open(attachment.host_path, os.O_RDWR)
    try:
        os.pwrite(descriptor, b"persistent-user-sentinel", 4096)
    finally:
        os.close(descriptor)
    current = SimpleNamespace(
        record=SimpleNamespace(run_id="first-id", dispatch_key=SimpleNamespace(backend=SimpleNamespace(value="kvm"))),
        state={
            "status": "running",
            "volumes": [
                {"name": "data", "host_path": str(attachment.host_path), "mount_path": "/srv/data", "read_only": False}
            ],
        },
    )
    monkeypatch.setattr(volumes.state, "read_run_ledger_snapshot", lambda *_args: current)
    monkeypatch.setattr(volumes.state, "run_entry_present_or_ambiguous", lambda *_args: True)
    volumes.bind_named_volume_reservations(roots, name="first", reservation_id="pending-first", run_id="first-id")
    current.state["status"] = "stopped"
    with pytest.raises(StateError, match="already reserved"):
        reserve(disks, name="second", run_id="pending-second", read_only=True)
    with pytest.raises(StateError, match="exact run removal"):
        volumes.release_named_volume_reservations(roots, name="first", run_id="first-id", runner=runner)
    with pytest.raises(StateError, match="reserved"):
        volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner)
    current.state["status"] = "removed"
    volumes.release_named_volume_reservations(roots, name="first", run_id="first-id", runner=runner)
    again = reserve(disks, name="second", run_id="pending-second")[0]
    assert again.host_path == attachment.host_path
    with again.host_path.open("rb") as stream:
        stream.seek(4096)
        assert stream.read(24) == b"persistent-user-sentinel"
    assert len(formats) == 1
    assert not again.format


def test_read_only_reservation_is_also_exclusive_and_never_formats(disks):
    roots, _, _, _ = disks
    create(disks)
    attached = reserve(disks, read_only=True)[0]
    assert attached.read_only and not attached.format
    assert volumes.list_named_volumes(roots)[0]["attachments"][0]["read_only"] is True
    with pytest.raises(StateError, match="already reserved"):
        reserve(disks, name="other", run_id="other", read_only=True)


def test_batch_reservation_is_all_or_nothing(disks):
    roots, runner, _, _ = disks
    create(disks)
    with pytest.raises(StateError, match="must be created"):
        volumes.reserve_named_volumes(
            roots,
            name="first",
            run_id="pending-first",
            backend="kvm",
            intents=(RunVolumeIntent("data", "/data"), RunVolumeIntent("missing", "/other")),
            runner=runner,
        )
    assert volumes.list_named_volumes(roots)[0]["attachments"] == []


def test_unexpected_native_reference_blocks_attachment_and_deletion(disks):
    roots, runner, _, domains = disks
    create(disks)
    entry = volumes._read(roots)["volumes"]["named:kvm:data"]
    path = volumes._path(roots, entry, runner)
    domains.append(
        SimpleNamespace(
            isPersistent=lambda: 0,
            XMLDesc=lambda flags: f"<domain><devices><disk><source file='{path}'/></disk></devices></domain>",
        )
    )
    with pytest.raises(StateError, match="referenced by a native domain"):
        reserve(disks)
    with pytest.raises(StateError, match="referenced by a native domain"):
        volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner)
    assert path.exists()
    assert volumes.list_named_volumes(roots)[0]["attachments"] == []


def test_replaced_physical_disk_is_never_adopted_or_removed(disks):
    roots, runner, formats, _ = disks
    create(disks)
    entry = volumes._read(roots)["volumes"]["named:kvm:data"]
    path = volumes._path(roots, entry, runner)
    retained = path.with_suffix(".old")
    path.rename(retained)
    path.write_bytes(b"foreign-disk")
    path.chmod(0o600)
    with pytest.raises(StateError, match="identity changed"):
        reserve(disks)
    with pytest.raises(StateError, match="identity changed"):
        volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner)
    assert path.read_bytes() == b"foreign-disk"
    assert retained.exists() and len(formats) == 1
    assert volumes.list_named_volumes(roots)[0]["status"] == "unavailable"


def test_exact_unattached_disk_deletion_does_not_remove_other_disk(disks):
    roots, runner, _, _ = disks
    create(disks)
    create(disks, "other")
    result = volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner)
    assert result["status"] == "removed"
    assert [row["name"] for row in volumes.list_named_volumes(roots)] == ["other"]


def test_malformed_ledger_refuses_inventory_attach_and_delete(disks):
    roots, runner, _, _ = disks
    create(disks)
    state.atomic_write_json(roots.state / "named-volumes" / "ledger.json", {"schema": "foreign", "volumes": {}})
    for operation in (
        lambda: volumes.list_named_volumes(roots),
        lambda: reserve(disks),
        lambda: volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner),
    ):
        with pytest.raises(StateError, match="invalid named volume ledger"):
            operation()


def test_cross_process_claims_have_one_winner(disks):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires fork to inherit isolated physical backend fixtures")
    roots, _, _, _ = disks
    create(disks)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()

    def claim(name):
        start.wait(10)
        try:
            reserve(disks, name=name, run_id=name)
        except StateError:
            results.put("refused")
        else:
            results.put("reserved")

    processes = [context.Process(target=claim, args=(name,)) for name in ("first", "second")]
    for process in processes:
        process.start()
    start.set()
    try:
        assert sorted(results.get(timeout=10) for _ in processes) == ["refused", "reserved"]
    finally:
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(10)
    assert all(process.exitcode == 0 for process in processes)
    assert len(volumes.list_named_volumes(roots)[0]["attachments"]) == 1


def test_lima_precreation_formats_once_and_attachment_never_requests_mkfs(disks, tmp_path, monkeypatch):
    roots, runner, formats, _ = disks
    home = tmp_path / "lima"
    monkeypatch.setenv("LIMA_HOME", str(home))
    native = {}

    def ensure(_roots, project, name, size, *, runner):
        backend_name = volumes.physical.lima_backend_name(project, name)
        directory = home / "_disks" / backend_name
        directory.mkdir(parents=True, mode=0o700)
        descriptor = os.open(directory / "datadisk", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.ftruncate(descriptor, size)
        finally:
            os.close(descriptor)
        native[backend_name] = volumes.physical.LimaDiskInfo(backend_name, size, "raw", str(directory))
        return volumes.physical.LimaVolume(project, name, backend_name, size, created=True)

    monkeypatch.setattr(volumes.physical, "ensure_lima_volume", ensure)
    monkeypatch.setattr(volumes.physical, "list_lima_disks", lambda **kwargs: tuple(native.values()))
    row = create(disks, backend="lima-vz")
    attachment = volumes.reserve_named_volumes(
        roots,
        name="lima-first",
        run_id="pending-lima",
        backend="lima-vz",
        intents=(RunVolumeIntent("data", "/srv/data", read_only=True),),
        runner=runner,
    )[0]
    assert row["status"] == "available"
    assert attachment.backend_name in native
    assert attachment.host_path is None and attachment.read_only and not attachment.format
    disk = Path(native[attachment.backend_name].directory) / "datadisk"
    assert volumes.physical._has_ext4_superblock(disk, expected_label="lima-" + attachment.backend_name)
    assert len(formats) == 1


def test_pending_claim_can_roll_back_only_without_declared_or_native_attachment(disks, monkeypatch):
    roots, runner, _, _ = disks
    create(disks)
    attachment = reserve(disks)[0]
    current = SimpleNamespace(
        record=SimpleNamespace(run_id="competing-run"), state={"status": "running", "volumes": []}
    )
    monkeypatch.setattr(volumes.state, "run_entry_present_or_ambiguous", lambda *_args: True)
    monkeypatch.setattr(volumes.state, "read_run_ledger_snapshot", lambda *_args: current)
    current.state["volumes"] = [{"host_path": str(attachment.host_path)}]
    with pytest.raises(StateError, match="exact run removal"):
        volumes.release_named_volume_reservations(roots, name="first", run_id="pending-first", runner=runner)
    current.state["volumes"] = []
    volumes.release_named_volume_reservations(roots, name="first", run_id="pending-first", runner=runner)
    assert volumes.list_named_volumes(roots)[0]["status"] == "available"


def test_restart_refuses_replaced_reserved_disk(disks, monkeypatch):
    roots, runner, _, _ = disks
    create(disks)
    attachment = reserve(disks)[0]
    original = attachment.host_path.with_suffix(".original")
    attachment.host_path.rename(original)
    attachment.host_path.write_bytes(b"foreign")
    attachment.host_path.chmod(0o600)
    with pytest.raises(StateError, match="identity changed"):
        volumes.validate_named_volume_reservations(roots, name="first", run_id="pending-first", runner=runner)
    assert original.exists()


@pytest.mark.parametrize("outcome", ["running", "precommit-failure", "committed-failure"])
def test_dispatch_binds_actual_disks_and_rolls_back_only_clean_failure(disks, tmp_path, monkeypatch, outcome):
    import hashlib

    from palimpsest_local import cloud_runtime, runtime_dispatch
    from palimpsest_local.refs import ImageRef, RunSpec, StackRef
    from palimpsest_local.runtime_types import (
        DispatchKey,
        ResolvedRunRequest,
        RunRequestProvenanceStage,
        RuntimeBackend,
        RuntimeKind,
    )

    roots, _, formats, _ = disks
    create(disks)
    image = tmp_path / "base.qcow2"
    image.write_bytes(b"isolated-cloud-base")
    base = ImageRef("sha256:" + hashlib.sha256(image.read_bytes()).hexdigest(), "qcow2", "x86_64", None, image)
    spec = RunSpec("first", StackRef(base, ()))
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    logical = runtime_dispatch._issue_run_request_provenance(
        ResolvedRunRequest(key, spec, (RunVolumeIntent("data", "/srv/data", read_only=True),), attachments_bound=False),
        RunRequestProvenanceStage.LOGICAL,
    )
    snapshots = {}
    monkeypatch.setattr(volumes.state, "run_entry_present_or_ambiguous", lambda _roots, name: name in snapshots)
    monkeypatch.setattr(volumes.state, "read_run_ledger_snapshot", lambda _roots, name: snapshots[name])

    def adapter(request, _roots):
        assert request.attachments_bound
        attached = request.spec.volumes[0]
        assert attached.host_path.is_file() and attached.mount_path == "/srv/data"
        assert attached.read_only and not attached.format
        native_disks = cloud_runtime._build_kvm_volume_disks(request.spec, [])
        script = volumes.kvm.build_layer_activation_script([], volumes=native_disks)
        assert "/dev/disk/by-id/virtio-" + native_disks[0].serial in script
        assert 'mount -t ext4 -o ro,noload "$DEV" /srv/data' in script
        assert "mkfs" not in script
        if outcome == "precommit-failure":
            raise StateError("synthetic launch failure")
        record = SimpleNamespace(run_id="actual-run-id", dispatch_key=key)
        snapshots["first"] = SimpleNamespace(
            record=record,
            state={
                "status": "running" if outcome == "running" else "failed",
                "volumes": [
                    {
                        "name": attached.name,
                        "host_path": str(attached.host_path),
                        "mount_path": attached.mount_path,
                        "read_only": attached.read_only,
                    }
                ],
            },
        )
        if outcome == "committed-failure":
            raise StateError("synthetic launch failure")
        return SimpleNamespace(record=record)

    monkeypatch.setattr(runtime_dispatch, "_run_bound_request", adapter)
    if outcome == "running":
        result = runtime_dispatch._run_with_named_volumes(logical, roots)
        assert result.record.run_id == "actual-run-id"
    else:
        with pytest.raises(StateError, match="synthetic launch failure"):
            runtime_dispatch._run_with_named_volumes(logical, roots)
    row = volumes.list_named_volumes(roots)[0]
    if outcome == "precommit-failure":
        assert row["attachments"] == [] and row["status"] == "available"
    else:
        assert row["attachments"][0]["run_id"] == "actual-run-id"
        assert row["status"] == "attached"
    assert len(formats) == 1


@pytest.mark.parametrize("reference", ["live-backing", "persistent-only", "persistent-backing"])
def test_native_backing_and_persistent_only_references_refuse_delete_and_attach(disks, reference):
    roots, runner, _, domains = disks
    create(disks)
    entry = volumes._read(roots)["volumes"]["named:kvm:data"]
    path = volumes._path(roots, entry, runner)
    empty = "<domain><devices/></domain>"
    direct = f"<domain><devices><disk><source file='{path}'/></disk></devices></domain>"
    backing = (
        "<domain><devices><disk><source file='/unrelated/overlay.qcow2'/>"
        "<backingStore><source file='/unrelated/lower.qcow2'/>"
        f"<backingStore><source file='{path}'/></backingStore>"
        "</backingStore></disk></devices></domain>"
    )
    inspected = []

    def definition(flags):
        inspected.append(flags)
        if reference == "live-backing":
            return backing if flags == 0 else empty
        if flags == 0:
            return empty
        return direct if reference == "persistent-only" else backing

    domains.append(SimpleNamespace(isPersistent=lambda: 1, XMLDesc=definition))
    for operation in (
        lambda: reserve(disks),
        lambda: volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner),
    ):
        with pytest.raises(StateError, match="referenced by a native domain"):
            operation()
    assert 0 in inspected and 2 in inspected
    assert path.exists()
    assert volumes.list_named_volumes(roots)[0]["attachments"] == []


def test_unreadable_persistent_definition_fails_closed(disks):
    roots, runner, _, domains = disks
    create(disks)

    def definition(flags):
        if flags == 2:
            raise RuntimeError("synthetic persistent configuration read failure")
        return "<domain><devices/></domain>"

    domains.append(SimpleNamespace(isPersistent=lambda: 1, XMLDesc=definition))
    for operation in (
        lambda: reserve(disks),
        lambda: volumes.delete_named_volume(roots, name="data", backend="kvm", runner=runner),
    ):
        with pytest.raises(StateError, match="cannot inspect live and persistent"):
            operation()
    assert volumes.list_named_volumes(roots)[0]["status"] == "available"


@pytest.mark.parametrize("purge", [False, True])
def test_managed_removal_releases_real_reservation_but_preserves_disk(disks, purge):
    from palimpsest_local import cloud_runtime, runtime_dispatch
    from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind

    roots, runner, _, _ = disks
    create(disks)
    attachment = reserve(disks)[0]
    with attachment.host_path.open("r+b") as disk:
        disk.seek(4096)
        disk.write(b"retained-data-sentinel")
    state.init_resolved_roots(roots)
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend.KVM)
    with state.reserve_new_run(roots, "first", key) as reservation:
        reservation.paths.overlay.write_bytes(b"retained-root-sentinel")
        reservation.write_state(
            "stopped",
            {
                "layers": [],
                "volumes": [
                    {
                        "name": "data",
                        "host_path": str(attachment.host_path),
                        "mount_path": "/srv/data",
                        "read_only": False,
                    }
                ],
            },
        )
        run_id = reservation.record.run_id
    volumes.bind_named_volume_reservations(roots, name="first", reservation_id="pending-first", run_id=run_id)

    def missing(_name):
        raise KeyError("Domain not found")

    connection = SimpleNamespace(lookupByName=missing, listAllDomains=lambda _flags: [])
    record = state.read_run_dispatch_record(roots, "first")
    result = cloud_runtime.rm("first", roots=roots, conn=connection, volumes=purge, _expected_record=record)
    receipt = runtime_dispatch._normalize_lifecycle_outcome(result)
    assert receipt.status == "removed"
    assert receipt.storage_policy == (
        "instance-root-and-topology-purged" if purge else "instance-root-and-topology-retained"
    )
    assert (receipt.retained_instance is None) is purge
    object.__setattr__(
        result,
        "storage_policy",
        "instance-root-and-topology-retained" if purge else "instance-root-and-topology-purged",
    )
    with pytest.raises(StateError, match="invalid lifecycle receipt"):
        runtime_dispatch._normalize_lifecycle_outcome(result)
    assert volumes.list_named_volumes(roots)[0]["attachments"] == []
    assert state.run_paths(roots, "first").root.exists() is not purge
    reused = reserve(disks, name="second", run_id="pending-second")[0]
    with reused.host_path.open("rb") as disk:
        disk.seek(4096)
        assert disk.read(22) == b"retained-data-sentinel"
    assert reused.host_path == attachment.host_path
