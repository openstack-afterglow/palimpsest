"""Synthetic filesystem/native-command proofs; never touch a real hypervisor."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from palimpsest_local import (
    cli,
    cloud_runtime,
    host_inventory,
    host_vm_control,
    inventory,
    kvm,
    lima,
    retained_instances,
    runtime_dispatch,
    state,
)
from palimpsest_local.errors import StateError
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.runtime_types import DispatchKey, RuntimeBackend, RuntimeKind


@pytest.fixture
def native_lima(tmp_path, monkeypatch):
    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})
    home = tmp_path / "lima"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("LIMA_HOME", str(home))
    monkeypatch.setattr(
        host_vm_control.platforms, "detect_host", lambda: host_vm_control.platforms.HostPlatform("Darwin", "aarch64")
    )
    directory = home / "native"
    directory.mkdir(mode=0o700)
    (directory / "basedisk").write_bytes(b"immutable base")
    (directory / "diffdisk").write_bytes(b"root sentinel")
    (directory / "lima.yaml").write_text("vmType: vz\n")
    data = tmp_path / "data.raw"
    data.write_bytes(b"named volume sentinel")
    shared = tmp_path / "shared.squashfs"
    shared.write_bytes(b"shared layer sentinel")
    instance = {
        "name": "native",
        "dir": str(directory),
        "vmType": "vz",
        "status": "Running",
        "config": {"additionalDisks": [{"name": "data", "format": False}], "images": [{"location": str(shared)}]},
    }
    native = {"present": True, "delete_failure": False, "calls": []}
    monkeypatch.setattr(lima, "_list_instances", lambda: [instance] if native["present"] else [])

    def command(argv, *, timeout_seconds=60):
        native["calls"].append(argv)
        if argv[1] == "stop":
            instance["status"] = "Stopped"
        elif argv[1] == "delete":
            shutil.rmtree(directory)
            native["present"] = False
            if native["delete_failure"]:
                return subprocess.CompletedProcess(argv, 1, "", "lost response")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", command)
    identity = host_vm_control.lima_removal_identity(instance)
    row = {
        "id": "host:lima-vz:native",
        "name": "native",
        "backend": "lima-vz",
        "status": "running",
        "runtime_kind": "external",
        "run_id": None,
        "managed": False,
        "source": "host-backend",
        "removal_identity": identity,
    }
    return SimpleNamespace(
        roots=roots,
        home=home,
        directory=directory,
        data=data,
        shared=shared,
        instance=instance,
        native=native,
        row=row,
        identity=identity,
    )


def test_default_native_rm_retains_root_bytes_config_and_ordered_layers(native_lima):
    env = native_lima
    base, lower, upper = ("sha256:" + char * 64 for char in "abc")
    archive = host_vm_control._remove_lima(
        env.row,
        env.identity,
        roots=env.roots,
        volumes=False,
        ledger={"base_digest": base, "layers": [{"digest": lower}, {"digest": upper}]},
    )
    assert not env.directory.exists()
    row = retained_instances.list_retained_instances(env.roots)[0]
    assert row["id"] == archive and row["status"] == "available"
    assert row["layers"] == [{"digest": lower}, {"digest": upper}]
    directory = env.roots.state / "retained-instances" / archive.split(":", 1)[1]
    assert (directory / "diffdisk").read_bytes() == b"root sentinel"
    assert (directory / "basedisk").read_bytes() == b"immutable base"
    native = json.loads((directory / "native.json").read_text())
    assert native["config"]["additionalDisks"][0]["name"] == "data"
    assert set(retained_instances.artifact_references(env.roots)) == {base, lower, upper}
    assert env.data.read_bytes() == b"named volume sentinel"
    assert env.shared.read_bytes() == b"shared layer sentinel"


def test_explicit_native_instance_purge_never_deletes_data_or_shared_layers(native_lima):
    env = native_lima
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots, volumes=True)
    assert receipt["storage_policy"] == "instance-root-and-topology-purged"
    assert retained_instances.list_retained_instances(env.roots) == []
    assert not env.directory.exists()
    assert env.data.read_bytes() == b"named volume sentinel"
    assert env.shared.read_bytes() == b"shared layer sentinel"


def test_archive_delete_requires_exact_name_and_owned_inode(native_lima):
    env = native_lima
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    identifier = receipt["retained_instance"]
    directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
    with pytest.raises(StateError, match="exactly"):
        retained_instances.delete_retained_instance(env.roots, identifier, confirmation="Native")
    root = directory / "diffdisk"
    original = root.read_bytes()
    displaced = env.home / "saved-root"
    root.rename(displaced)
    root.write_bytes(original)
    with pytest.raises(StateError, match="identity changed"):
        retained_instances.delete_retained_instance(env.roots, identifier, confirmation="native")
    root.unlink()
    displaced.rename(root)
    result = retained_instances.delete_retained_instance(env.roots, identifier, confirmation="native")
    assert result["status"] == "removed"
    assert not directory.exists()
    assert retained_instances.artifact_references(env.roots) == {}
    assert env.data.exists() and env.shared.exists()


def test_failed_native_removal_never_discards_the_sole_saved_root(native_lima):
    env = native_lima
    env.native["delete_failure"] = True
    with pytest.raises(StateError, match="not discarded"):
        host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    assert not env.directory.exists()  # Simulate a native delete with a lost/error response.
    row = retained_instances.list_retained_instances(env.roots)[0]
    assert row["status"] == "unavailable"
    directory = env.roots.state / "retained-instances" / row["id"].split(":", 1)[1]
    assert (directory / "diffdisk").read_bytes() == b"root sentinel"
    env.native["present"] = True
    with pytest.raises(StateError, match="unconfirmed"):
        retained_instances.delete_retained_instance(env.roots, row["id"], confirmation="native")
    assert (directory / "diffdisk").read_bytes() == b"root sentinel"
    env.native["present"] = False
    retained_instances.delete_retained_instance(env.roots, row["id"], confirmation="native")
    assert not directory.exists()


@pytest.mark.parametrize("damage", ["invalid-json", "missing-root", "bad-layers", "record-symlink"])
def test_invalid_retention_record_blocks_artifact_reference_queries(native_lima, damage):
    env = native_lima
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    directory = env.roots.state / "retained-instances" / receipt["retained_instance"].split(":", 1)[1]
    manifest = directory / "record.json"
    if damage == "invalid-json":
        manifest.write_text("{")
    elif damage == "missing-root":
        (directory / "diffdisk").unlink()
    elif damage == "bad-layers":
        record = json.loads(manifest.read_text())
        record["topology"]["layers"] = ["not-an-ordered-layer-record"]
        state.atomic_write_json(manifest, record)
    else:
        saved = env.home / "record.json"
        manifest.rename(saved)
        manifest.symlink_to(saved)
    with pytest.raises(StateError):
        retained_instances.artifact_references(env.roots)


def test_snapshot_failure_aborts_before_native_delete(native_lima, monkeypatch):
    env = native_lima

    def no_space(*args, **kwargs):
        raise OSError("no space")

    monkeypatch.setattr(retained_instances, "_copy", no_space)
    with pytest.raises(StateError):
        host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    assert env.directory.joinpath("diffdisk").read_bytes() == b"root sentinel"
    assert not any(call[1] == "delete" for call in env.native["calls"])


def test_archive_delete_refuses_new_native_attachment(native_lima, monkeypatch):
    env = native_lima
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    identifier = receipt["retained_instance"]
    directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
    monkeypatch.setattr(
        lima,
        "_list_instances",
        lambda: [{"name": "new", "config": {"images": [{"location": str(directory / "diffdisk")}]}}],
    )
    with pytest.raises(StateError, match="referenced"):
        retained_instances.delete_retained_instance(env.roots, identifier, confirmation="native")
    assert (directory / "diffdisk").read_bytes() == b"root sentinel"


@pytest.mark.parametrize("vm_type", ["vz", "qemu", "krunkit"])
def test_observed_lima_removal_identity_includes_exact_native_vm_type(native_lima, vm_type):
    env = native_lima
    env.instance["vmType"] = vm_type
    identity = host_vm_control.lima_removal_identity(env.instance)
    assert identity is not None
    if vm_type != "vz":
        assert identity != env.identity
        with pytest.raises(StateError, match="identity changed"):
            host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
        assert not env.native["calls"]


def test_managed_lima_default_rm_publishes_independent_retention_manifest(native_lima):
    env = native_lima
    rpaths = state.run_paths(env.roots, "native")
    rpaths.root.mkdir(mode=0o700)
    owner = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"backend": "lima-vz", "layers": [], "volumes": []})
    env.instance["config"]["env"] = {"PALIMPSEST_RUN_ID": owner.run_id}
    result = lima.rm("native", roots=env.roots)
    assert result["status"] == "removed"
    row = retained_instances.list_retained_instances(env.roots)[0]
    assert result["retained_instance"] == row["id"]
    assert row["run_id"] == owner.run_id
    # Even removal of a stale source ledger cannot discard the protected root.
    shutil.rmtree(rpaths.root)
    assert retained_instances.list_retained_instances(env.roots)[0]["id"] == row["id"]


def test_external_libvirt_purge_refuses_unknown_ownership_before_stop(native_lima, monkeypatch):
    env = native_lima
    domain_uuid = str(uuid.uuid4())
    calls = []
    definition = f"<domain type='kvm'><name>native</name><uuid>{domain_uuid}</uuid><devices><disk type='file' device='disk'><source file='{env.data}'/></disk></devices></domain>"
    domain = SimpleNamespace(
        UUIDString=lambda: domain_uuid,
        name=lambda: "native",
        XMLDesc=lambda flags: definition,
        isPersistent=lambda: True,
        isActive=lambda: True,
        destroy=lambda: calls.append("destroy"),
    )
    connection = SimpleNamespace(lookupByUUIDString=lambda value: domain, close=lambda: None)
    monkeypatch.setattr(kvm, "connect", lambda uri: connection)
    monkeypatch.setattr(
        kvm,
        "_libvirt",
        lambda: SimpleNamespace(
            VIR_DOMAIN_UNDEFINE_KEEP_NVRAM=1,
            VIR_DOMAIN_UNDEFINE_MANAGED_SAVE=2,
            VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA=4,
            VIR_DOMAIN_XML_INACTIVE=2,
        ),
    )
    monkeypatch.setattr(host_vm_control.platforms, "detect_host", lambda: SimpleNamespace(system="Linux"))
    row = {**env.row, "id": "host:kvm:native", "backend": "kvm", "removal_identity": domain_uuid}
    with pytest.raises(StateError, match="before stopping"):
        host_vm_control.remove_vm(row, expected_identity=domain_uuid, roots=env.roots, volumes=True)
    assert calls == [] and env.data.read_bytes() == b"named volume sentinel"


def test_managed_libvirt_unsafe_root_and_tpm_purge_fail_before_stop(native_lima):
    env = native_lima
    rpaths = state.run_paths(env.roots, "native")
    foreign = ET.fromstring(
        f"<domain><devices><disk device='disk'><source file='{env.data}'/></disk></devices></domain>"
    )
    with pytest.raises(StateError, match="ownership is unknown"):
        cloud_runtime._instance_removal_policy(SimpleNamespace(), foreign, rpaths, purge=True)
    tpm = ET.fromstring("<domain><devices><tpm><backend type='emulator'/></tpm></devices></domain>")
    with pytest.raises(StateError, match="TPM ownership"):
        cloud_runtime._instance_removal_policy(SimpleNamespace(), tpm, rpaths, purge=True)
    assert env.data.read_bytes() == b"named volume sentinel"


def test_qcow_retention_reconnects_private_copy_not_shared_backing(native_lima, monkeypatch):
    env = native_lima
    (env.directory / "diffdisk").write_bytes(b"QFI\xfbroot sentinel")
    commands = []

    def qemu(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "info":
            chain = [
                {"filename": str(env.directory / "diffdisk"), "format": "qcow2"},
                {"filename": str(env.shared), "format": "raw"},
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(chain), "")
        assert argv[1] == "rebase" and argv[-1] != str(env.directory / "diffdisk")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(retained_instances.subprocess, "run", qemu)
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    directory = env.roots.state / "retained-instances" / receipt["retained_instance"].split(":", 1)[1]
    assert (directory / "shared.squashfs").read_bytes() == b"shared layer sentinel"
    assert retained_instances.list_retained_instances(env.roots)[0]["topology"]["archived_backing_chain"] == [
        {"file": "diffdisk", "backing": "shared.squashfs", "backing_format": "raw"}
    ]
    assert any(argv[1] == "rebase" and argv[-2] == "shared.squashfs" for argv in commands)
    assert env.shared.read_bytes() == b"shared layer sentinel"


def test_managed_cloud_default_retains_root_then_purge_keeps_named_data(native_lima, monkeypatch):
    env = native_lima
    rpaths = state.run_paths(env.roots, "cloud")
    rpaths.root.mkdir(mode=0o700)
    rpaths.overlay.write_bytes(b"cloud writable root")
    owner = state.write_owner_record(rpaths)
    volumes = [{"name": "data", "host_path": str(env.data), "mount_path": "/data", "read_only": False}]
    state.write_run_state(rpaths, status="running", data={"backend": "kvm", "layers": [], "volumes": volumes})
    marker = (
        f"<pal:run xmlns:pal='{kvm.DOMAIN_MARKER_NAMESPACE}' id='{owner.run_id}' "
        f"schema='1' version='{kvm.DOMAIN_MARKER_VERSION}'/>"
    )
    definition = (
        f"<domain><name>cloud</name><metadata>{marker}</metadata><devices>"
        f"<disk type='file' device='disk'><source file='{rpaths.overlay}'/><target dev='vda'/></disk>"
        f"<disk type='file' device='disk'><source file='{env.data}'/><target dev='vdb'/></disk>"
        f"<disk type='file' device='disk'><source file='{env.shared}'/><readonly/></disk>"
        "</devices></domain>"
    )
    native = {"active": True, "present": True}

    def destroy():
        native["active"] = False

    def undefine():
        native["present"] = False

    domain = SimpleNamespace(
        XMLDesc=lambda *args: definition,
        UUIDString=lambda: str(uuid.UUID(int=1)),
        isActive=lambda: native["active"],
        isPersistent=lambda: True,
        destroy=destroy,
        undefine=undefine,
    )

    def lookup(name):
        if not native["present"]:
            raise KeyError("Domain not found")
        return domain

    conn = SimpleNamespace(lookupByName=lookup, listAllDomains=lambda flags=0: [domain] if native["present"] else [])
    monkeypatch.setattr(kvm, "_libvirt", lambda: SimpleNamespace())
    result = cloud_runtime.rm("cloud", roots=env.roots, conn=conn)
    assert result["status"] == "removed" and not native["present"]
    row = retained_instances.list_retained_instances(env.roots)[0]
    directory = env.roots.state / "retained-instances" / row["id"].split(":", 1)[1]
    assert (directory / rpaths.overlay.name).read_bytes() == b"cloud writable root"
    assert row["topology"]["volumes"] == volumes
    assert env.data.read_bytes() == b"named volume sentinel"
    result = cloud_runtime.rm("cloud", roots=env.roots, conn=conn, volumes=True)
    assert result["status"] == "removed" and not rpaths.root.exists()
    assert retained_instances.list_retained_instances(env.roots) == []
    assert env.data.read_bytes() == b"named volume sentinel"
    assert env.shared.read_bytes() == b"shared layer sentinel"


@pytest.mark.parametrize("vm_type", ["qemu", "krunkit"])
def test_host_observation_labels_exact_lima_backend(native_lima, monkeypatch, vm_type):
    env = native_lima
    env.instance["vmType"] = vm_type
    monkeypatch.setattr(host_inventory.shutil, "which", lambda binary: "/synthetic/limactl")

    def query(argv, **kwargs):
        payload = [env.instance] if argv[1] == "list" else []
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(host_inventory.subprocess, "run", query)
    vms, volumes, networks, warnings = host_inventory._observe_lima()
    assert vms[0]["backend"] == f"lima-{vm_type}"
    assert vms[0]["id"] == f"host:lima-{vm_type}:native"
    assert vms[0]["removal_identity"] is not None
    assert volumes[0]["backend"] == f"lima-{vm_type}"


def test_reference_only_libvirt_archive_preserves_paths_without_adoption(native_lima):
    env = native_lima
    definition = ET.fromstring(
        f"<domain><os><nvram>{env.shared}</nvram></os><devices>"
        f"<disk><source file='{env.data}'/><target dev='vda'/></disk>"
        "<tpm><backend type='emulator' persistent_state='yes'/></tpm></devices></domain>"
    )
    identifier = retained_instances.retain_libvirt(
        env.roots,
        name="external",
        backend="kvm",
        identity=str(uuid.uuid4()),
        definition=definition,
    )
    retained_instances.mark_native_removed(env.roots, identifier)
    row = retained_instances.list_retained_instances(env.roots)[0]
    assert row["reference_only"] and not row["deletable"]
    assert str(env.data) in row["topology"]["native_disks"][0]
    assert row["topology"]["nvram"] == str(env.shared)
    assert "persistent_state" in row["topology"]["tpm"][0]
    with pytest.raises(StateError, match="not owned"):
        retained_instances.delete_retained_instance(env.roots, identifier, confirmation="external")
    assert env.data.read_bytes() == b"named volume sentinel"
    assert env.shared.read_bytes() == b"shared layer sentinel"


def test_lima_default_retains_base_symlink_bytes_and_efi_directory(native_lima):
    import tarfile

    env = native_lima
    base = env.directory / "basedisk"
    base.unlink()
    base.symlink_to(env.shared)
    firmware = env.directory / "vz-efi"
    firmware.mkdir(mode=0o700)
    (firmware / "NVRAM").write_bytes(b"EFI sentinel")
    (env.directory / "vz-identifier").write_bytes(b"machine identity")
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    directory = env.roots.state / "retained-instances" / receipt["retained_instance"].split(":", 1)[1]
    assert (directory / "basedisk").read_bytes() == b"shared layer sentinel"
    assert (directory / "vz-identifier").read_bytes() == b"machine identity"
    with tarfile.open(directory / "vz-efi.tar") as snapshot:
        assert snapshot.extractfile("vz-efi/NVRAM").read() == b"EFI sentinel"
    assert env.shared.read_bytes() == b"shared layer sentinel"
    assert retained_instances.list_retained_instances(env.roots)[0]["topology"]["native_symlinks"] == [
        {"file": "basedisk", "target": str(env.shared)}
    ]


def test_backing_inspection_holds_actual_artifact_reference_lock(native_lima, monkeypatch):
    import fcntl
    import os

    env = native_lima
    closure = retained_instances._disk_closure
    inspected = []

    def inspect(sources, aliases):
        descriptor = os.open(env.roots.locks / "artifact-references-v1.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        inspected.append(True)
        return closure(sources, aliases)

    monkeypatch.setattr(retained_instances, "_disk_closure", inspect)
    receipt = host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots)
    assert inspected == [True]
    assert retained_instances.list_retained_instances(env.roots)[0]["id"] == receipt["retained_instance"]


def test_retained_delete_refuses_persistent_only_libvirt_reference(native_lima, monkeypatch):
    env = native_lima
    identifier = retained_instances.retain_libvirt(
        env.roots,
        name="cloud",
        backend="kvm",
        identity=str(uuid.uuid4()),
        definition=ET.fromstring("<domain><devices/></domain>"),
        owned_sources=[env.data],
    )
    retained_instances.mark_native_removed(env.roots, identifier)
    directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
    domain = SimpleNamespace(
        UUIDString=lambda: str(uuid.UUID(int=2)),
        isPersistent=lambda: 1,
        XMLDesc=lambda flags: (
            "<domain><devices/></domain>"
            if flags == 0
            else (f"<domain><devices><disk><source file='{directory / env.data.name}'/></disk></devices></domain>")
        ),
    )
    connection = SimpleNamespace(listAllDomains=lambda _flags: [domain], close=lambda: None)
    monkeypatch.setattr(kvm, "_libvirt", lambda: SimpleNamespace(VIR_DOMAIN_XML_INACTIVE=2))
    with pytest.raises(StateError, match="referenced"):
        retained_instances.delete_retained_instance(env.roots, identifier, confirmation="cloud", _connection=connection)
    assert (directory / env.data.name).read_bytes() == b"named volume sentinel"


def test_managed_lima_removed_status_still_publishes_new_retention(native_lima):
    env = native_lima
    rpaths = state.run_paths(env.roots, "native")
    rpaths.root.mkdir(mode=0o700)
    owner = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="removed", data={"backend": "lima-vz", "layers": [], "volumes": []})
    env.instance["config"]["env"] = {"PALIMPSEST_RUN_ID": owner.run_id}
    result = lima.rm("native", roots=env.roots)
    durable = state.read_run_state(rpaths)
    assert result["retained_instance"] == durable["retained_instance"]
    assert durable["retained_instance"] == retained_instances.list_retained_instances(env.roots)[0]["id"]
    assert durable["storage_policy"] == "instance-root-and-topology-retained"


@pytest.mark.parametrize("backend", ["kvm", "libvirt-hvf", "lima-vz"])
def test_managed_rm_dashboard_archive_delete_then_cli_purge_releases_layers(native_lima, monkeypatch, capsys, backend):
    env = native_lima
    store = ContentStore(env.roots.store)
    digests = ["sha256:" + store.write_stream([payload]).name for payload in (b"base", b"lower", b"upper")]
    rpaths = state.run_paths(env.roots, "native")
    rpaths.root.mkdir(mode=0o700)
    owner = state.write_owner_record(rpaths)
    rpaths.overlay.write_bytes(b"managed root sentinel")
    state.write_run_state(
        rpaths,
        status="running",
        data={
            "backend": backend,
            "base_digest": digests[0],
            "layers": [{"digest": digest} for digest in digests[1:]],
            "volumes": [{"name": "data", "host_path": str(env.data), "mount_path": "/data", "read_only": False}],
        },
    )
    adapter = lima if backend == "lima-vz" else cloud_runtime
    if backend == "lima-vz":
        env.instance["config"]["env"] = {"PALIMPSEST_RUN_ID": owner.run_id}
    else:
        present = [True]
        active = [True]
        marker = (
            f"<pal:run xmlns:pal='{kvm.DOMAIN_MARKER_NAMESPACE}' id='{owner.run_id}' "
            f"schema='1' version='{kvm.DOMAIN_MARKER_VERSION}'/>"
        )
        definition = (
            f"<domain><name>native</name><metadata>{marker}</metadata><devices>"
            f"<disk type='file' device='disk'><source file='{rpaths.overlay}'/></disk>"
            "</devices></domain>"
        )
        domain = SimpleNamespace(
            XMLDesc=lambda *args: definition,
            UUIDString=lambda: str(uuid.UUID(int=1)),
            isActive=lambda: active[0],
            isPersistent=lambda: 1,
            destroy=lambda: active.__setitem__(0, False),
            undefine=lambda: present.__setitem__(0, False),
        )

        def lookup(name):
            assert name == "native"
            if not present[0]:
                raise KeyError("Domain not found")
            return domain

        connection = SimpleNamespace(
            lookupByName=lookup,
            listAllDomains=lambda flags: [domain] if present[0] else [],
            close=lambda: None,
        )
        monkeypatch.setattr(kvm, "connect", lambda uri: connection)
        monkeypatch.setattr(kvm, "_libvirt", lambda: SimpleNamespace())
        monkeypatch.setattr(
            cloud_runtime,
            "_resolve_ledger_profile",
            lambda ledger: SimpleNamespace(backend=backend, uri="qemu:///fixture"),
        )

    # Exercise real CLI dispatch/receipts and runtime/native removal; only host
    # capability discovery and the native transport are synthetic.
    monkeypatch.setattr(cli, "resolve_roots", lambda: env.roots)
    monkeypatch.setattr(cli.host_journal, "begin", lambda operation: SimpleNamespace(finish=lambda status: None))
    monkeypatch.setattr(runtime_dispatch, "_preflight_existing_adapter", lambda *args: adapter)
    assert cli.main(["rm", "native"]) == 0
    assert "removed native" in capsys.readouterr().out
    row = retained_instances.list_retained_instances(env.roots)[0]
    assert row["run_id"] == owner.run_id
    assert row["layers"] == [{"digest": digest} for digest in digests[1:]]
    assert set(retained_instances.artifact_references(env.roots)) == set(digests)
    before = state.read_run_state(rpaths)
    assert before["retained_instance"] == row["id"]
    with pytest.raises(StateError, match="still used by"):
        inventory.remove_artifact(env.roots, digests[1], force=True)

    guard = state.artifact_reference_guard
    guarded = [False]

    @contextmanager
    def nonreentrant_guard(roots):
        assert not guarded[0], "artifact guard was nested"
        with guard(roots):
            guarded[0] = True
            try:
                yield
            finally:
                guarded[0] = False

    writer = state._write_existing_run_mutation_state
    written = []

    def checked_write(mutation, status, data):
        assert guarded[0]
        descriptor = os.open(env.roots.locks / "native.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        assert mutation.record.run_id == owner.run_id
        written.append(data.get("retained_instance"))
        return writer(mutation, status, data)

    monkeypatch.setattr(state, "artifact_reference_guard", nonreentrant_guard)
    monkeypatch.setattr(state, "_write_existing_run_mutation_state", checked_write)
    result = inventory.delete_volume(env.roots, row["id"], confirmation="native")
    assert result["status"] == "removed"
    assert written == [None]
    after = state.read_run_state(rpaths)
    assert after["retained_instance"] is None
    assert after["layers"] == before["layers"] and after["base_digest"] == before["base_digest"]
    assert retained_instances.list_retained_instances(env.roots) == []
    assert retained_instances.artifact_references(env.roots) == {}
    assert rpaths.overlay.read_bytes() == b"managed root sentinel"
    # Source topology still protects backing artifacts until explicit run purge.
    for digest in digests:
        with pytest.raises(StateError, match="still used by: native"):
            inventory.remove_artifact(env.roots, digest, force=True)
    # Repeating default removal may re-archive the surviving owned cloud root.
    # That durable removed->removed rewrite must be accepted by public dispatch.
    before_repeat = state.read_run_ledger_snapshot(env.roots, "native")
    assert cli.main(["rm", "native"]) == 0
    assert "removed native" in capsys.readouterr().out
    after_repeat = state.read_run_ledger_snapshot(env.roots, "native")
    assert after_repeat.state["status"] == "removed"
    if backend != "lima-vz":
        recreated = retained_instances.list_retained_instances(env.roots)[0]
        assert recreated["run_id"] == owner.run_id
        assert after_repeat.state["retained_instance"] == recreated["id"]
        assert state.lifecycle_revision(after_repeat) == state.lifecycle_revision(before_repeat) + 1
    assert cli.main(["rm", "native", "--volumes"]) == 0
    assert "removed native" in capsys.readouterr().out
    assert not rpaths.root.exists()
    assert env.data.read_bytes() == b"named volume sentinel"
    assert env.shared.read_bytes() == b"shared layer sentinel"
    for digest in digests:
        assert inventory.remove_artifact(env.roots, digest)["digest"] == digest
        assert not store.exists(digest)
    key = DispatchKey(RuntimeKind.CLOUD_IMAGE, RuntimeBackend(backend))
    with state.reserve_new_run(env.roots, "native", key) as replacement:
        replacement.write_state("stopped", {"layers": []})
        assert replacement.record.run_id != owner.run_id


@pytest.fixture
def managed_lima_archive(native_lima):
    env = native_lima
    rpaths = state.run_paths(env.roots, "native")
    rpaths.root.mkdir(mode=0o700)
    owner = state.write_owner_record(rpaths)
    state.write_run_state(rpaths, status="running", data={"backend": "lima-vz", "layers": [], "volumes": []})
    env.instance["config"]["env"] = {"PALIMPSEST_RUN_ID": owner.run_id}
    identifier = lima.rm("native", roots=env.roots)["retained_instance"]
    return env, rpaths, owner, identifier


@pytest.mark.parametrize("damage", ["confirmation", "active-run", "native-reference", "archive-inode"])
def test_archive_delete_failure_preserves_managed_pointer(managed_lima_archive, monkeypatch, damage):
    env, rpaths, owner, identifier = managed_lima_archive
    confirmation = "native"
    if damage == "confirmation":
        confirmation = "Native"
    elif damage == "active-run":
        with state.locked_existing_run(env.roots, "native") as mutation:
            mutation.write_state("running", mutation.mutable_state())
    elif damage == "native-reference":
        directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
        monkeypatch.setattr(
            lima, "_list_instances", lambda: [{"name": "other", "config": {"root": str(directory / "diffdisk")}}]
        )
    else:
        directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
        original = directory / "diffdisk"
        original.rename(env.home / "saved-root")
        original.write_bytes(b"foreign replacement")
    before = rpaths.state.read_bytes()
    with pytest.raises(StateError):
        inventory.delete_volume(env.roots, identifier, confirmation=confirmation)
    assert rpaths.state.read_bytes() == before
    assert state.read_run_state(rpaths)["retained_instance"] == identifier
    assert state.read_run_dispatch_record(env.roots, "native").run_id == owner.run_id


def test_archive_delete_never_edits_same_name_replacement_run(managed_lima_archive):
    env, rpaths, owner, identifier = managed_lima_archive
    state.atomic_write_json(rpaths.owner, {"schema_version": 1, "name": "native", "run_id": str(uuid.uuid4())})
    state.write_run_state(rpaths, status="stopped", data={"backend": "lima-vz", "layers": [], "volumes": []})
    before = rpaths.state.read_bytes()
    assert inventory.delete_volume(env.roots, identifier, confirmation="native")["status"] == "removed"
    assert rpaths.state.read_bytes() == before
    assert state.read_run_dispatch_record(env.roots, "native").run_id != owner.run_id


def test_archive_delete_rejects_run_change_before_lock(managed_lima_archive, monkeypatch):
    env, rpaths, owner, identifier = managed_lima_archive
    original = state.locked_existing_run

    @contextmanager
    def changed_before_lock(roots, name, **kwargs):
        with original(roots, name) as mutation:
            mutation.write_state("removed", {**mutation.mutable_state(), "error": "concurrent revision"})
        with original(roots, name, **kwargs) as mutation:
            yield mutation

    monkeypatch.setattr(state, "locked_existing_run", changed_before_lock)
    with pytest.raises(StateError, match="changed before lifecycle mutation"):
        inventory.delete_volume(env.roots, identifier, confirmation="native")
    assert state.read_run_state(rpaths)["retained_instance"] == identifier
    assert retained_instances.list_retained_instances(env.roots)[0]["run_id"] == owner.run_id


def test_interrupted_archive_unlink_does_not_leave_stale_run_pointer(managed_lima_archive, monkeypatch):
    from pathlib import Path

    env, rpaths, _, identifier = managed_lima_archive
    directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
    unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == directory / "basedisk":
            raise OSError("synthetic archive deletion failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError, match="synthetic archive deletion failure"):
        inventory.delete_volume(env.roots, identifier, confirmation="native")
    assert state.read_run_state(rpaths)["retained_instance"] is None
    assert retained_instances.list_retained_instances(env.roots)[0]["id"] == identifier
    monkeypatch.setattr(Path, "unlink", unlink)
    assert inventory.delete_volume(env.roots, identifier, confirmation="native")["status"] == "removed"


@pytest.mark.parametrize("uncertainty", ["inventory", "persistence"])
def test_archive_delete_requires_exact_native_inventory_and_persistence(native_lima, uncertainty):
    env = native_lima
    identifier = retained_instances.retain_libvirt(
        env.roots,
        name="cloud",
        backend="kvm",
        identity=str(uuid.UUID(int=1)),
        definition=ET.fromstring("<domain><devices/></domain>"),
        owned_sources=[env.data],
    )
    retained_instances.mark_native_removed(env.roots, identifier)
    domain = SimpleNamespace(
        UUIDString=lambda: str(uuid.UUID(int=2)),
        XMLDesc=lambda flags: "<domain><devices/></domain>",
        isPersistent=lambda: False,
    )
    connection = SimpleNamespace(listAllDomains=lambda flags: {} if uncertainty == "inventory" else [domain])
    with pytest.raises(StateError, match="inventory|persistence"):
        retained_instances.delete_retained_instance(env.roots, identifier, confirmation="cloud", _connection=connection)
    assert retained_instances.list_retained_instances(env.roots)[0]["id"] == identifier
    assert env.data.read_bytes() == b"named volume sentinel"


@pytest.mark.parametrize("backend", ["lima-vz", "kvm", "libvirt-hvf"])
@pytest.mark.parametrize("alias", ["dot-segment", "symlink", "file-uri"])
def test_archive_delete_preserves_native_alias_references(native_lima, monkeypatch, backend, alias):
    env = native_lima
    digest = "sha256:" + "a" * 64
    if backend == "lima-vz":
        identifier = host_vm_control._remove_lima(
            env.row, env.identity, roots=env.roots, volumes=False, ledger={"layers": [{"digest": digest}]}
        )
        root_name = "diffdisk"
    else:
        identifier = retained_instances.retain_libvirt(
            env.roots,
            name="native",
            backend=backend,
            identity=str(uuid.UUID(int=1)),
            definition=ET.fromstring("<domain><devices/></domain>"),
            ledger={"layers": [{"digest": digest}]},
            owned_sources=[env.data],
        )
        retained_instances.mark_native_removed(env.roots, identifier)
        root_name = env.data.name
    directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
    if alias == "symlink":
        link = env.home / "archive-alias"
        link.symlink_to(directory, target_is_directory=True)
        source = str(link / root_name)
    else:
        source = f"{directory.parent}/./{directory.name}/{root_name}"
        if alias == "file-uri":
            source = "file://" + source
    assert str(directory) not in source
    if backend == "lima-vz":
        monkeypatch.setattr(lima, "_list_instances", lambda: [{"name": "replacement", "config": {"root": source}}])
    else:
        domain = SimpleNamespace(
            UUIDString=lambda: str(uuid.UUID(int=2)),
            isPersistent=lambda: 0,
            XMLDesc=lambda flags: f"<domain><devices><disk><source file='{source}'/></disk></devices></domain>",
        )
        connection = SimpleNamespace(listAllDomains=lambda flags: [domain], close=lambda: None)
        monkeypatch.setattr(kvm, "connect", lambda uri: connection)
    original = (directory / root_name).read_bytes()
    with pytest.raises(StateError, match="referenced"):
        inventory.delete_volume(env.roots, identifier, confirmation="native")
    assert (directory / root_name).read_bytes() == original
    assert digest in retained_instances.artifact_references(env.roots)
    assert env.data.read_bytes() == b"named volume sentinel"


def test_run_locked_archive_delete_refuses_foreign_archive_identity(managed_lima_archive):
    env, rpaths, _, identifier = managed_lima_archive
    directory = env.roots.state / "retained-instances" / identifier.split(":", 1)[1]
    record = json.loads((directory / "record.json").read_text())
    state.atomic_write_json(directory / "record.json", {**record, "run_id": str(uuid.uuid4())})
    before = rpaths.state.read_bytes()
    with state.locked_existing_run(env.roots, "native") as mutation:
        with pytest.raises(StateError, match="exact removed run"):
            retained_instances.delete_retained_instance_for_run(env.roots, identifier, mutation=mutation)
    assert rpaths.state.read_bytes() == before
    assert (directory / "diffdisk").read_bytes() == b"root sentinel"


@pytest.mark.parametrize(
    "damage", ["config-bytes", "config-object", "protected", "null-marker", "config-symlink", "config-hardlink"]
)
def test_native_lima_selection_binds_configuration_before_mutation(native_lima, damage):
    env = native_lima
    config = env.directory / "lima.yaml"
    if damage == "config-bytes":
        config.write_text("vmType: vz\ncpus: 8\n")
    elif damage == "config-object":
        env.instance["config"]["cpus"] = 8
    elif damage == "protected":
        env.instance["protected"] = True
    elif damage == "null-marker":
        env.instance["config"]["env"] = {"PALIMPSEST_RUN_ID": None}
    elif damage == "config-symlink":
        config.rename(env.home / "other.yaml")
        config.symlink_to(env.home / "other.yaml")
    else:
        os.link(config, env.home / "other.yaml")
    with pytest.raises(StateError, match="identity changed"):
        host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots, volumes=True)
    assert not env.native["calls"]
    assert env.data.read_bytes() == b"named volume sentinel"


def test_native_lima_renamed_survivor_never_reports_removal(native_lima, monkeypatch):
    env = native_lima
    renamed = env.home / "renamed"

    def command(argv, **kwargs):
        assert argv[1] == "delete"
        env.directory.rename(renamed)
        env.native["present"] = False
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(lima, "_run_command", command)
    with pytest.raises(StateError, match="absence could not be verified"):
        host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots, volumes=True)
    assert (renamed / "diffdisk").read_bytes() == b"root sentinel"
    assert env.data.read_bytes() == b"named volume sentinel"


def test_native_lima_incompatible_host_never_reaches_command(native_lima, monkeypatch):
    env = native_lima
    monkeypatch.setattr(
        host_vm_control.platforms, "detect_host", lambda: SimpleNamespace(system="Linux", machine="x86_64")
    )
    with pytest.raises(StateError, match="not available"):
        host_vm_control.remove_vm(env.row, expected_identity=env.identity, roots=env.roots, volumes=True)
    assert not env.native["calls"]


@pytest.mark.parametrize("damage", ["managed", "source", "name", "id", "run_id"])
def test_native_removal_rejects_malformed_observed_selection(native_lima, damage):
    env = native_lima
    invalid = {"managed": True, "source": "run-ledger", "name": "../native", "id": "native", "run_id": "malformed"}
    row = {**env.row, damage: invalid[damage]}
    with pytest.raises(StateError):
        host_vm_control.remove_vm(row, expected_identity=env.identity, roots=env.roots, volumes=True)
    assert not env.native["calls"]
