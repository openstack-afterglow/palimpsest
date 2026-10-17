"""Unit tests for bounded read-only host resource observation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from palimpsest_local import host_inventory, platforms


@pytest.fixture(autouse=True)
def _isolated_host_resources():
    """Override conftest's autouse mock so test_host_inventory exercises real host_inventory.observe."""
    yield


def test_observe_offline_backends(monkeypatch: pytest.MonkeyPatch):
    """When limactl and libvirt are not available, observe returns empty lists without warnings."""
    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    assert res["vms"] == []
    assert res["volumes"] == []
    assert res["networks"] == []
    assert res["warnings"] == []


def test_observe_installed_libvirt_connection_failure_warns(monkeypatch: pytest.MonkeyPatch):
    """When libvirt is installed but connection fails, a safe warning is emitted."""
    mock_libvirt = MagicMock()
    mock_libvirt.openReadOnly.side_effect = Exception("libvirt connection socket refused")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: None)  # Lima absent
    monkeypatch.setattr(host_inventory.platforms, "detect_host", lambda: platforms.HostPlatform("Linux", "x86_64"))

    with patch.dict("sys.modules", {"libvirt": mock_libvirt}):
        res = host_inventory.observe()

    assert res["vms"] == []
    assert len(res["warnings"]) == 1
    assert res["warnings"][0] == "libvirt backend observation failed"


def test_observe_lima_stopped_and_running_vms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Running and stopped Lima VMs are observed with canonical inventory fields and stable IDs."""
    running_dir = tmp_path / "running-vm"
    running_dir.mkdir()
    running_disk = running_dir / "disk"
    running_disk.write_bytes(b"x" * 4096)

    stopped_dir = tmp_path / "stopped-vm"
    stopped_dir.mkdir()
    stopped_disk = stopped_dir / "disk"
    stopped_disk.write_bytes(b"y" * 2048)

    fake_instances = [
        {
            "name": "vm-running-1",
            "status": "Running",
            "arch": "aarch64",
            "memory": 2 * 1024 * 1024 * 1024,
            "cpus": 2,
            "disk": 30 * 1024 * 1024 * 1024,
            "dir": str(running_dir),
            "sshAddress": "127.0.0.1",
            "sshLocalPort": 60001,
            "config": {
                "env": {"PALIMPSEST_RUN_ID": "11111111-2222-3333-4444-555555555555"},
                "networks": [{"vzNAT": True}],
                "portForwards": [{"guestPort": 80, "hostPort": 8080, "hostIP": "127.0.0.1", "proto": "tcp"}],
            },
        },
        {
            "name": "vm-stopped-2",
            "status": "Stopped",
            "arch": "aarch64",
            "memory": 1024 * 1024 * 1024,
            "cpus": 1,
            "disk": 10 * 1024 * 1024 * 1024,
            "dir": str(stopped_dir),
            "sshAddress": "127.0.0.1",
            "sshLocalPort": 0,
            "config": {
                "env": {"PALIMPSEST_RUN_ID": "66666666-7777-8888-9999-000000000000"},
                "networks": [{"vzNAT": True}],
                "portForwards": [],
            },
        },
    ]

    stdout_bytes = "\n".join(json.dumps(inst) for inst in fake_instances)

    def fake_run(argv, **kwargs):
        if "list" in argv and "--format" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=stdout_bytes, stderr="")
        if "network" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "disk" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    assert len(res["vms"]) == 2
    assert res["warnings"] == []

    vm1 = next(v for v in res["vms"] if v["name"] == "vm-running-1")
    assert vm1["id"] == "host:lima-vz:vm-running-1"
    assert vm1["run_id"] == "11111111-2222-3333-4444-555555555555"
    assert vm1["backend"] == "lima-vz"
    assert vm1["runtime_kind"] == "cloud-image"
    assert vm1["status"] == "running"
    assert vm1["memory_mib"] == 2048
    assert vm1["vcpus"] == 2
    assert vm1["network"] == "vzNAT"
    assert vm1["ports"] == [{"guest_port": 80, "host_ip": "127.0.0.1", "host_port": 8080, "protocol": "tcp"}]
    assert vm1["ssh"] == {"host": "127.0.0.1", "port": 60001}
    assert vm1["managed"] is False
    assert vm1["source"] == "host-backend"
    assert vm1["project"] is None

    vm2 = next(v for v in res["vms"] if v["name"] == "vm-stopped-2")
    assert vm2["id"] == "host:lima-vz:vm-stopped-2"
    assert vm2["run_id"] == "66666666-7777-8888-9999-000000000000"
    assert vm2["runtime_kind"] == "cloud-image"
    assert vm2["status"] == "stopped"
    assert vm2["memory_mib"] == 1024
    assert vm2["vcpus"] == 1
    assert vm2["ports"] == []
    assert vm2["managed"] is False

    # Check primary disks with :primary suffix
    assert len(res["volumes"]) == 2
    d1 = next(d for d in res["volumes"] if d["name"] == "vm-running-1")
    assert d1["id"] == "disk:lima-vz:vm-running-1:primary"
    assert d1["kind"] == "vm-disk"
    assert d1["status"] == "attached"
    assert d1["size_bytes"] == 30 * 1024 * 1024 * 1024
    assert d1["file_size_bytes"] == 4096
    assert d1["source"] == "host-backend"
    assert d1["attachments"] == [
        {
            "name": "vm-running-1",
            "run_id": "11111111-2222-3333-4444-555555555555",
            "mount_path": "/",
            "read_only": False,
        }
    ]


def test_observe_secret_absence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Raw configuration, env secrets, auth scripts, and keys are strictly excluded from projection."""
    inst_dir = tmp_path / "secret-vm"
    inst_dir.mkdir()
    (inst_dir / "disk").write_bytes(b"data")

    sensitive_tokens = [
        "SUPER_SECRET_PASSWORD_123",
        "API_TOKEN_XYZ_SECRET",
        "cloud-init-sensitive-user-script",
        "private_rsa_key_material",
        "bearer-auth-header-987",
    ]

    fake_instance = {
        "name": "secret-vm",
        "status": "Running",
        "arch": "aarch64",
        "memory": 1024 * 1024 * 1024,
        "cpus": 1,
        "disk": 10 * 1024 * 1024 * 1024,
        "dir": str(inst_dir),
        "sshAddress": "127.0.0.1",
        "sshLocalPort": 2222,
        "config": {
            "env": {
                "PALIMPSEST_RUN_ID": "11111111-1111-1111-1111-111111111111",
                "PASSWORD": sensitive_tokens[0],
                "TOKEN": sensitive_tokens[1],
            },
            "provision": [{"mode": "system", "script": sensitive_tokens[2]}],
            "ssh": {
                "loadDotSSHPubKeys": False,
                "privateKey": sensitive_tokens[3],
            },
            "caCerts": [sensitive_tokens[4]],
        },
    }

    def fake_run(argv, **kwargs):
        if "list" in argv and "--format" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fake_instance), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    dumped = json.dumps(res)

    for token in sensitive_tokens:
        assert token not in dumped, f"Secret token {token!r} was leaked into host inventory projection!"


def test_observe_failed_identity_invalid_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Invalid or missing PALIMPSEST_RUN_ID markers must not hide actual external VMs."""
    cases = [
        ("vm-no-marker", {}, None),
        ("vm-bad-uuid", {"PALIMPSEST_RUN_ID": "not-a-valid-uuid"}, None),
        ("vm-upper-uuid", {"PALIMPSEST_RUN_ID": "11111111-2222-3333-4444-55555555555A"}, None),
        ("vm-int-marker", {"PALIMPSEST_RUN_ID": 12345}, None),
        (
            "vm-valid",
            {"PALIMPSEST_RUN_ID": "11111111-2222-3333-4444-55555555555a"},
            "11111111-2222-3333-4444-55555555555a",
        ),
    ]

    fake_instances = []
    for name, env_dict, _ in cases:
        d = tmp_path / name
        d.mkdir()
        (d / "disk").write_bytes(b"data")
        fake_instances.append(
            {
                "name": name,
                "status": "Running",
                "arch": "aarch64",
                "memory": 1024 * 1024 * 1024,
                "cpus": 1,
                "disk": 10 * 1024 * 1024 * 1024,
                "dir": str(d),
                "config": {"env": env_dict},
            }
        )

    def fake_run(argv, **kwargs):
        if "list" in argv and "--format" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="\n".join(json.dumps(i) for i in fake_instances), stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    assert len(res["vms"]) == 5

    for name, _, expected_run_id in cases:
        vm = next(v for v in res["vms"] if v["name"] == name)
        assert vm["run_id"] == expected_run_id
        if expected_run_id is not None:
            assert vm["runtime_kind"] == "cloud-image"
        else:
            assert vm["runtime_kind"] == "external"


def test_observe_capacity_distinct_from_file_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Primary disk virtual capacity is distinct from apparent file size and never overwritten by it."""
    vm_dir = tmp_path / "disk-test-vm"
    vm_dir.mkdir()
    disk_file = vm_dir / "disk"
    disk_file.write_bytes(b"A" * 8192)

    fake_inst = {
        "name": "disk-test-vm",
        "status": "Running",
        "arch": "aarch64",
        "memory": 1024 * 1024 * 1024,
        "cpus": 1,
        "disk": 50 * 1024 * 1024 * 1024,  # 50 GiB virtual capacity
        "dir": str(vm_dir),
        "config": {
            "env": {"PALIMPSEST_RUN_ID": "22222222-2222-2222-2222-222222222222"},
        },
    }

    def fake_run(argv, **kwargs):
        if "list" in argv and "--format" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fake_inst), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    assert len(res["volumes"]) == 1
    disk = res["volumes"][0]
    assert disk["kind"] == "vm-disk"
    assert disk["name"] == "disk-test-vm"
    assert disk["size_bytes"] == 50 * 1024 * 1024 * 1024
    assert disk["file_size_bytes"] == 8192
    assert disk["size_bytes"] != disk["file_size_bytes"]
    assert disk["status"] == "attached"


def test_observe_same_name_disk_and_volume_no_collision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A VM primary disk and an unrelated additional volume sharing the same name remain separate."""
    vm_dir = tmp_path / "shared-name"
    vm_dir.mkdir()
    (vm_dir / "disk").write_bytes(b"root-data")

    vol_dir = tmp_path / "vol-shared"
    vol_dir.mkdir()
    (vol_dir / "datadisk").write_bytes(b"vol-data")

    fake_inst = {
        "name": "shared-name",
        "status": "Running",
        "arch": "aarch64",
        "memory": 1024 * 1024 * 1024,
        "cpus": 1,
        "disk": 20 * 1024 * 1024 * 1024,
        "dir": str(vm_dir),
        "config": {
            "env": {"PALIMPSEST_RUN_ID": "33333333-3333-3333-3333-333333333333"},
            "additionalDisks": [],  # Volume is unattached to this VM
        },
    }

    fake_disks = [
        {"name": "shared-name", "size": "10GiB", "dir": str(vol_dir)},
    ]

    def fake_run(argv, **kwargs):
        if "list" in argv and "--format" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fake_inst), stderr="")
        if "disk" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fake_disks[0]), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    # Must preserve BOTH: the primary vm-disk and the unattached volume
    assert len(res["volumes"]) == 2

    v_primary = next(v for v in res["volumes"] if v["kind"] == "vm-disk")
    assert v_primary["id"] == "disk:lima-vz:shared-name:primary"
    assert v_primary["name"] == "shared-name"
    assert v_primary["size_bytes"] == 20 * 1024 * 1024 * 1024
    assert len(v_primary["attachments"]) == 1

    v_extra = next(v for v in res["volumes"] if v["kind"] == "volume")
    assert v_extra["id"] == "volume:lima-vz:shared-name"
    assert v_extra["name"] == "shared-name"
    assert v_extra["size_bytes"] == 10 * 1024 * 1024 * 1024
    assert v_extra["status"] == "detached"
    assert len(v_extra["attachments"]) == 0


def test_observe_lima_networks_configured_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Lima networks report status 'configured' and vzNAT is synthesized only when configured."""
    vm_dir = tmp_path / "net-vm"
    vm_dir.mkdir()
    (vm_dir / "disk").write_bytes(b"data")

    fake_inst = {
        "name": "net-vm",
        "status": "Running",
        "arch": "aarch64",
        "memory": 1024 * 1024 * 1024,
        "cpus": 1,
        "disk": 10 * 1024 * 1024 * 1024,
        "dir": str(vm_dir),
        "config": {
            "env": {"PALIMPSEST_RUN_ID": "44444444-4444-4444-4444-444444444444"},
            "networks": [{"vzNAT": True}, {"lima": "custom-lan"}],
            "portForwards": [
                {"guestPort": 80, "hostPort": 8080, "hostIP": "127.0.0.1", "proto": "tcp"},
                {"guestPort": 999999, "hostPort": 80, "hostIP": "invalid-ip", "proto": "xyz"},
            ],
        },
    }

    fake_nets = [{"name": "custom-lan", "mode": "shared", "gateway": "192.168.105.1", "netmask": "255.255.255.0"}]

    def fake_run(argv, **kwargs):
        if "list" in argv and "--format" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fake_inst), stderr="")
        if "network" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fake_nets[0]), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    vznat = next(n for n in res["networks"] if n["name"] == "vzNAT")
    assert vznat["kind"] == "lima"
    assert vznat["backend"] == "lima-vz"
    assert vznat["status"] == "configured"
    assert len(vznat["attachments"]) == 1
    att = vznat["attachments"][0]
    assert att["name"] == "net-vm"
    assert att["ports"] == [{"guest_port": 80, "host_ip": "127.0.0.1", "host_port": 8080, "protocol": "tcp"}]

    custom_net = next(n for n in res["networks"] if n["name"] == "custom-lan")
    assert custom_net["status"] == "configured"
    assert custom_net["subnet"] == "192.168.105.0/24"
    assert custom_net["gateway"] == "192.168.105.1"
    assert len(custom_net["attachments"]) == 1


def test_observe_malformed_json_emits_safe_warnings(monkeypatch: pytest.MonkeyPatch):
    """Non-empty unparseable output from limactl emits safe warnings, not silent empty success."""

    def fake_corrupt_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="FATAL: daemon database corrupted\n", stderr="")

    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: "/usr/local/bin/limactl")
    monkeypatch.setattr(host_inventory.subprocess, "run", fake_corrupt_run)
    monkeypatch.setattr(host_inventory, "_observe_libvirt", lambda: ([], [], [], []))

    res = host_inventory.observe()
    assert res["vms"] == []
    assert len(res["warnings"]) == 1
    assert res["warnings"][0] == "Lima backend observation failed"


@pytest.mark.parametrize("runtime_kind", ["cloud-image", "oci-root"])
def test_observe_libvirt_multi_disk_and_blockinfo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_kind):
    """Libvirt multi-disk domain marks exactly one primary disk; capacity comes from blockInfo."""
    root_disk_file = tmp_path / "root_disk.raw"
    root_disk_file.write_bytes(b"ROOT" * 100)
    data_disk_file = tmp_path / "data_disk.raw"
    data_disk_file.write_bytes(b"DATA" * 200)
    contract = f"contract='sha256:{'a' * 64}'" if runtime_kind == "oci-root" else ""

    domain_xml = f"""
    <domain type='kvm'>
      <name>kvm-multi-disk-guest</name>
      <metadata>
        <pal:run xmlns:pal='{host_inventory.kvm.DOMAIN_MARKER_NAMESPACE}'
          id='55555555-5555-5555-5555-555555555555' schema='1'
          version='{host_inventory.kvm.DOMAIN_MARKER_VERSION}' {contract}/>
      </metadata>
      <memory unit='KiB'>2097152</memory>
      <vcpu>2</vcpu>
      <os>
        <type arch='x86_64'>hvm</type>
      </os>
      <devices>
        <disk type='file' device='disk'>
          <source file='{root_disk_file}'/>
          <target dev='vda' bus='virtio'/>
        </disk>
        <disk type='file' device='disk'>
          <source file='{data_disk_file}'/>
          <target dev='vdb' bus='virtio'/>
        </disk>
        <interface type='network'>
          <source network='default'/>
        </interface>
      </devices>
    </domain>
    """

    mock_dom = MagicMock()
    mock_dom.name.return_value = "kvm-multi-disk-guest"
    mock_dom.isActive.return_value = 1
    mock_dom.state.return_value = (1, 1)
    mock_dom.info.return_value = (1, 2097152, 2097152, 2, 1000)
    mock_dom.XMLDesc.return_value = domain_xml

    def fake_block_info(dev, flags):
        if dev == "vda":
            return (40 * 1024 * 1024 * 1024, 400, 400)  # 40 GiB virtual capacity
        if dev == "vdb":
            return (100 * 1024 * 1024 * 1024, 800, 800)  # 100 GiB virtual capacity
        return (0, 0, 0)

    mock_dom.blockInfo.side_effect = fake_block_info

    mock_conn = MagicMock()
    mock_conn.listAllDomains.return_value = [mock_dom]
    mock_conn.listAllNetworks.return_value = []
    root_volume = MagicMock()
    root_volume.path.return_value = str(root_disk_file)
    root_volume.name.return_value = "root_disk.raw"
    data_volume = MagicMock()
    data_volume.path.return_value = str(data_disk_file)
    data_volume.name.return_value = "data_disk.raw"
    pool = MagicMock()
    pool.isActive.return_value = True
    pool.listAllVolumes.return_value = [root_volume, data_volume]
    mock_conn.listAllStoragePools.return_value = [pool]

    mock_libvirt = MagicMock()
    mock_libvirt.openReadOnly.return_value = mock_conn

    monkeypatch.setattr(host_inventory.platforms, "detect_host", lambda: platforms.HostPlatform("Linux", "x86_64"))
    monkeypatch.setattr(host_inventory.shutil, "which", lambda cmd: None)

    with patch.dict("sys.modules", {"libvirt": mock_libvirt}):
        with patch.object(host_inventory.kvm, "get_domain_run_id", return_value="55555555-5555-5555-5555-555555555555"):
            res = host_inventory.observe()

    assert len(res["vms"]) == 1
    assert res["vms"][0]["runtime_kind"] == runtime_kind
    assert len(res["volumes"]) == 2

    # Exactly one primary disk
    v_primary = next(v for v in res["volumes"] if v["kind"] == "vm-disk")
    assert v_primary["id"] == "disk:kvm:kvm-multi-disk-guest:primary"
    assert v_primary["size_bytes"] == 40 * 1024 * 1024 * 1024  # From blockInfo
    assert v_primary["file_size_bytes"] == len(b"ROOT" * 100)
    assert v_primary["attachments"][0]["mount_path"] == "/"

    # Secondary volume
    v_sec = next(v for v in res["volumes"] if v["kind"] == "volume")
    assert v_sec["size_bytes"] == 100 * 1024 * 1024 * 1024  # From blockInfo
    assert v_sec["file_size_bytes"] == len(b"DATA" * 200)
    assert v_sec["attachments"][0]["mount_path"] == ""
    # Path digest prevents leaking the raw filesystem path in ID
    assert str(data_disk_file) not in v_sec["id"]


@pytest.fixture
def external_libvirt(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from palimpsest_local import host_vm_control, state

    roots = state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})

    class NativeError(RuntimeError):
        def get_error_code(self):
            return 42

    native = {
        "uuid": "11111111-1111-4111-8111-111111111111",
        "name": "Guest café:one",
        "active": True,
        "persistent": True,
        "exists": True,
        "fail_destroy": False,
        "flags": None,
        "tpm": False,
        "tpm_persistent": False,
        "metadata": "",
        "xml_error": False,
    }
    disk = tmp_path / "shared-disk.raw"
    disk.write_bytes(b"shared disk")
    nvram = tmp_path / "firmware.fd"
    nvram.write_bytes(b"firmware")
    tpm = tmp_path / "tpm.state"
    tpm.write_bytes(b"sealed TPM state")

    def destroy():
        if native["fail_destroy"]:
            raise NativeError("private driver diagnostics")
        native["active"] = False
        if native["tpm"] and not native["persistent"] and not native["tpm_persistent"]:
            tpm.unlink()
        if not native["persistent"]:
            native["exists"] = False

    def undefine(flags):
        native["flags"] = flags
        native["exists"] = False
        if native["tpm"] and not flags & 64 and not native["tpm_persistent"]:
            tpm.unlink()

    def lookup(identity):
        if native["exists"] and identity == native["uuid"]:
            return domain
        raise NativeError("no domain")

    def xml_desc(flags=0):
        if native["xml_error"]:
            raise NativeError("XML unavailable")
        if "xml_override" in native:
            return native["xml_override"]
        policy = " persistent_state='yes'" if native["tpm_persistent"] else ""
        tpm_xml = f"<tpm><backend type='emulator'{policy}/></tpm>" if native["tpm"] else ""
        return (
            f"<domain><name>{native['name']}</name><uuid>{native['uuid']}</uuid>"
            f"<metadata>{native['metadata']}</metadata><os><nvram>{nvram}</nvram></os>"
            f"<devices><disk type='file' device='disk'><source file='{disk}'/><target dev='vda'/></disk>{tpm_xml}</devices></domain>"
        )

    domain = SimpleNamespace(
        UUIDString=lambda: native["uuid"],
        name=lambda: native["name"],
        isActive=lambda: native["active"],
        isPersistent=lambda: native["persistent"],
        destroy=destroy,
        undefineFlags=undefine,
        state=lambda: (1 if native["active"] else 5, 0),
        info=lambda: (1, 1024, 1024, 1, 0),
        XMLDesc=xml_desc,
    )
    conn = SimpleNamespace(
        lookupByUUIDString=lookup,
        close=lambda: None,
        listAllDomains=lambda: [domain] if native["exists"] else [],
        listAllNetworks=lambda: [],
        listAllStoragePools=lambda: [],
    )
    libvirt = SimpleNamespace(
        libvirtError=NativeError,
        VIR_ERR_NO_DOMAIN=42,
        VIR_DOMAIN_UNDEFINE_KEEP_NVRAM=8,
        VIR_DOMAIN_UNDEFINE_MANAGED_SAVE=1,
        VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA=2,
        VIR_DOMAIN_UNDEFINE_KEEP_TPM=64,
        openReadOnly=lambda uri: conn,
    )
    monkeypatch.setitem(sys.modules, "libvirt", libvirt)
    monkeypatch.setattr(host_vm_control.kvm, "_libvirt", lambda: libvirt)
    monkeypatch.setattr(host_vm_control.kvm, "connect", lambda uri: conn)
    monkeypatch.setattr(host_inventory, "_observe_lima", lambda: ([], [], [], []))
    return {
        "native": native,
        "disk": disk,
        "nvram": nvram,
        "tpm": tpm,
        "domain": domain,
        "libvirt": libvirt,
        "roots": roots,
    }


@pytest.mark.parametrize(
    ("backend", "system", "machine"), [("kvm", "Linux", "x86_64"), ("libvirt-hvf", "Darwin", "aarch64")]
)
@pytest.mark.parametrize(("active", "persistent"), [(True, True), (False, True), (True, False)])
def test_observed_libvirt_removal_preserves_disks_and_nvram(
    external_libvirt, monkeypatch, backend, system, machine, active, persistent
):
    from palimpsest_local import host_vm_control

    native = external_libvirt["native"]
    native.update(active=active, persistent=persistent)
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform(system, machine))
    row = host_inventory.observe()["vms"][0]
    assert row["backend"] == backend and row["run_id"] is None
    assert row["removal_identity"] == native["uuid"]
    receipt = host_vm_control.remove_vm(row, expected_identity=row["removal_identity"], roots=external_libvirt["roots"])
    assert receipt["status"] == "removed" and receipt["storage_policy"] == "libvirt-disks-retained"
    assert not native["exists"] and not native["active"]
    assert native["flags"] == (11 if persistent else None)
    assert external_libvirt["disk"].read_bytes() == b"shared disk"
    assert external_libvirt["nvram"].read_bytes() == b"firmware"
    assert host_inventory.observe()["vms"] == []


@pytest.mark.parametrize("change", ["uuid", "name", "destroy-failure", "oci-root"])
def test_libvirt_removal_identity_or_backend_failure_never_reports_success(external_libvirt, monkeypatch, change):
    from palimpsest_local import host_vm_control
    from palimpsest_local.errors import StateError

    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = external_libvirt["native"]
    row = host_inventory.observe()["vms"][0]
    if change == "uuid":
        native["uuid"] = "22222222-2222-4222-8222-222222222222"
    elif change == "name":
        native["name"] = "replacement"
    elif change == "oci-root":
        row["runtime_kind"] = "oci-root"
    else:
        native["fail_destroy"] = True
    with pytest.raises(StateError):
        host_vm_control.remove_vm(row, expected_identity=row["removal_identity"], roots=external_libvirt["roots"])
    assert native["exists"] and native["active"] and native["flags"] is None
    assert external_libvirt["disk"].read_bytes() == b"shared disk"


@pytest.mark.parametrize("damage", ["symlink", "shared-writable", "wrong-home", "wrong-marker"])
def test_lima_removal_identity_rejects_unsafe_native_directories(tmp_path, monkeypatch, damage):
    from palimpsest_local import host_vm_control

    home = tmp_path.resolve()
    monkeypatch.setenv("LIMA_HOME", str(home))
    native = home / "native"
    native.mkdir(mode=0o700)
    instance = {"name": "native", "vmType": "vz", "dir": str(native), "config": {}}
    assert host_vm_control.lima_removal_identity(instance) is not None
    if damage == "symlink":
        actual = native.with_name("actual")
        native.rename(actual)
        native.symlink_to(actual, target_is_directory=True)
    elif damage == "shared-writable":
        native.chmod(0o777)
    elif damage == "wrong-home":
        monkeypatch.setenv("LIMA_HOME", str(home / "different"))
    else:
        instance["config"] = {"env": {"PALIMPSEST_RUN_ID": "malformed"}}
    assert host_vm_control.lima_removal_identity(instance) is None


@pytest.mark.parametrize(
    "damage", ["XML-unavailable", "malformed-XML", "wrong-root", "invalid-marker", "OCI-contract", "duplicate-marker"]
)
def test_libvirt_unknown_or_oci_classification_never_exposes_removal(external_libvirt, monkeypatch, damage):
    from palimpsest_local import inventory

    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = external_libvirt["native"]
    marker = (
        f"<pal:run xmlns:pal='{host_inventory.kvm.DOMAIN_MARKER_NAMESPACE}' schema='1' "
        f"version='{host_inventory.kvm.DOMAIN_MARKER_VERSION}' id='{native['uuid']}'/>"
    )
    if damage == "XML-unavailable":
        native["xml_error"] = True
    elif damage == "malformed-XML":
        native["xml_override"] = "<domain>"
    elif damage == "wrong-root":
        native["xml_override"] = "<invalid/>"
    elif damage == "invalid-marker":
        native["metadata"] = marker.replace("schema='1'", "schema='99'")
    elif damage == "OCI-contract":
        native["metadata"] = marker.replace("/>", " contract='sha256:" + "a" * 64 + "'/>")
    else:
        native["metadata"] = marker + marker
    row = host_inventory.observe()["vms"][0]
    assert row["removal_identity"] is None
    assert not inventory._vm_lifecycle_capabilities(row)["remove"]
    assert native["active"] and native["exists"]


@pytest.mark.parametrize(
    "change", ["unreadable-XML", "malformed-XML", "OCI-contract", "unsafe-transient-TPM", "missing-keep-TPM"]
)
def test_libvirt_preflight_rejects_changed_classification_or_unsafe_tpm_before_stop(
    external_libvirt, monkeypatch, change
):
    from palimpsest_local import host_vm_control
    from palimpsest_local.errors import StateError

    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = external_libvirt["native"]
    row = host_inventory.observe()["vms"][0]
    if change == "unreadable-XML":
        native["xml_error"] = True
    elif change == "malformed-XML":
        native["xml_override"] = "<domain>"
    elif change == "OCI-contract":
        native["metadata"] = (
            f"<pal:run xmlns:pal='{host_inventory.kvm.DOMAIN_MARKER_NAMESPACE}' schema='1' "
            f"version='{host_inventory.kvm.DOMAIN_MARKER_VERSION}' id='{native['uuid']}' "
            f"contract='sha256:{'a' * 64}'/>"
        )
    else:
        native["tpm"] = True
        if change == "unsafe-transient-TPM":
            native["persistent"] = False
        else:
            del external_libvirt["libvirt"].VIR_DOMAIN_UNDEFINE_KEEP_TPM
    with pytest.raises(StateError):
        host_vm_control.remove_vm(row, expected_identity=row["removal_identity"], roots=external_libvirt["roots"])
    assert native["active"] and native["exists"] and native["flags"] is None
    assert external_libvirt["tpm"].read_bytes() == b"sealed TPM state"
    assert external_libvirt["disk"].read_bytes() == b"shared disk"


@pytest.mark.parametrize(("persistent", "tpm_persistent"), [(True, False), (True, True), (False, True)])
def test_libvirt_removal_explicitly_retains_emulator_tpm_state(
    external_libvirt, monkeypatch, persistent, tpm_persistent
):
    from palimpsest_local import host_vm_control

    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = external_libvirt["native"]
    native.update(tpm=True, persistent=persistent, tpm_persistent=tpm_persistent)
    row = host_inventory.observe()["vms"][0]
    result = host_vm_control.remove_vm(row, expected_identity=row["removal_identity"])
    assert result["status"] == "removed" and not native["exists"]
    assert external_libvirt["tpm"].read_bytes() == b"sealed TPM state"
    assert external_libvirt["disk"].read_bytes() == b"shared disk"


@pytest.mark.parametrize(("before", "after"), [(None, "first"), ("first", None), ("first", "second")])
@pytest.mark.parametrize("change_at", [1, 2])
def test_libvirt_marker_presence_and_uuid_changes_are_fenced_before_stop(
    external_libvirt, monkeypatch, before, after, change_at
):
    from palimpsest_local import host_vm_control
    from palimpsest_local.errors import StateError

    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = external_libvirt["native"]
    identities = {"first": "33333333-3333-4333-8333-333333333333", "second": "44444444-4444-4444-8444-444444444444"}

    def marker(label):
        if label is None:
            return ""
        return (
            f"<pal:run xmlns:pal='{host_inventory.kvm.DOMAIN_MARKER_NAMESPACE}' schema='1' "
            f"version='{host_inventory.kvm.DOMAIN_MARKER_VERSION}' id='{identities[label]}'/>"
        )

    native["metadata"] = marker(before)
    row = host_inventory.observe()["vms"][0]
    assert row["run_id"] == (identities[before] if before else None)
    original = external_libvirt["domain"].XMLDesc
    calls = 0

    def changed(flags=0):
        nonlocal calls
        calls += 1
        if calls == change_at:
            native["metadata"] = marker(after)
        return original(flags)

    monkeypatch.setattr(external_libvirt["domain"], "XMLDesc", changed)
    with pytest.raises(StateError, match="run marker changed"):
        host_vm_control.remove_vm(row, expected_identity=row["removal_identity"])
    assert native["active"] and native["exists"] and native["flags"] is None
    assert external_libvirt["disk"].read_bytes() == b"shared disk"


def test_stopped_libvirt_missing_disk_remains_removable_without_native_block_query(
    external_libvirt, monkeypatch, tmp_path
):
    from palimpsest_local import inventory

    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Darwin", "aarch64"))
    native = external_libvirt["native"]
    native["active"] = False
    definition = external_libvirt["domain"].XMLDesc(0)
    missing = tmp_path / "missing-overlay.qcow2"
    native["xml_override"] = definition.replace(str(external_libvirt["disk"]), str(missing))
    monkeypatch.setattr(
        external_libvirt["domain"], "blockInfo", lambda *a: pytest.fail("missing inactive disk queried"), raising=False
    )
    observed = host_inventory.observe()
    assert inventory._vm_lifecycle_capabilities(observed["vms"][0])["remove"]
    assert observed["volumes"][0]["status"] == "unavailable"
    assert observed["volumes"][0]["size_bytes"] is None


def test_stopped_libvirt_local_stat_denial_still_uses_native_capacity(external_libvirt, monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "detect_host", lambda: platforms.HostPlatform("Linux", "x86_64"))
    external_libvirt["native"]["active"] = False
    protected = tmp_path / "daemon-readable.qcow2"
    protected.write_bytes(b"disk")
    definition = external_libvirt["domain"].XMLDesc(0)
    external_libvirt["native"]["xml_override"] = definition.replace(
        "<devices>", f"<devices><disk type='file' device='disk'><source file='{protected}'/><target dev='vda'/></disk>"
    )
    actual_stat = host_inventory.os.lstat

    def denied(path, *args, **kwargs):
        if str(path) == str(protected):
            raise PermissionError("only the libvirt daemon can inspect this path")
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(host_inventory.os, "lstat", denied)
    monkeypatch.setattr(external_libvirt["domain"], "blockInfo", lambda *a: (4096, 128, 256), raising=False)
    volume = host_inventory.observe()["volumes"][0]
    assert volume["size_bytes"] == 4096 and volume["allocated_bytes"] == 128
    assert volume["file_size_bytes"] is None and volume["status"] == "unavailable"
