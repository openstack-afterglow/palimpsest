from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import inspect
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import pytest

from palimpsest_local import cloud_cli
from palimpsest_local import nova_native_cloud as native
from palimpsest_local.cloud_env import SCHEMA, CloudEnv, CloudEnvError

PROJECT = "a" * 32
PACKAGE = "b" * 64
SOURCE = "sha256:" + "c" * 64
PROBE = "d" * 64
FLAVOR = "d48b03e3-9a78-40a8-91cb-75bc9dd9e1ed"
GPU_FLAVOR = "c6d23eb1-5a57-436a-abfa-d9be3d4308ed"
PORT = "11111111-1111-4111-8111-111111111111"
IMAGE = "22222222-2222-4222-8222-222222222222"
VOLUME = "33333333-3333-4333-8333-333333333333"
SERVER = "44444444-4444-4444-8444-444444444444"


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def registry_image():
    descriptor = {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:" + "e" * 64, "size": 20}
    compressed = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
        "digest": "sha256:" + "f" * 64,
        "size": 50,
    }
    config = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "1" * 64]},
        "config": {"Entrypoint": ["/opt/nvidia/nvidia_entrypoint.sh"], "Env": ["CUDA_VERSION=12.2.2"]},
    }
    return {
        "source": {
            "root_digest": SOURCE,
            "root_descriptor": {"digest": SOURCE},
            "manifest_digest": SOURCE,
            "manifest_descriptor": {"digest": SOURCE},
            "config_digest": descriptor["digest"],
            "config_descriptor": descriptor,
            "manifest": {"config": descriptor, "layers": [compressed]},
            "config": config,
        },
        "runtime": {
            "entrypoint": config["config"]["Entrypoint"],
            "env": config["config"]["Env"],
            "working_dir": "/",
            "user": "0",
        },
        "layers": [
            {
                "ordinal": 0,
                "digest": "sha256:" + "2" * 64,
                "size": 100,
                "compressed": compressed,
                "diff_id": "sha256:" + "1" * 64,
                "conversion_key": "sha256:" + "3" * 64,
                "receipt": "receipts/sha256/" + "4" * 64 + ".json",
            }
        ],
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    env = CloudEnv("test", root=tmp_path)
    conn = Obj(
        current_project_id=PROJECT,
        current_user_id="user",
        compute=Mock(),
        network=Mock(),
        image=Mock(),
        block_storage=Mock(),
        shared_file_system=Mock(),
    )
    env.state = {
        "schema": SCHEMA,
        "name": "test",
        "cloud": "test-cloud",
        "project_id": PROJECT,
        "user_id": "user",
        "marker": "5" * 32,
        "network_id": "network",
        "resources": {},
        "images": {"cuda": registry_image()},
        "worker_package_sha256": PACKAGE,
        "worker_ready": True,
    }
    env._connection = conn
    env.save()
    monkeypatch.setattr(cloud_cli, "_connect", Mock(return_value=conn))
    monkeypatch.setattr(native, "package_archive", lambda: (b"reviewed source", PACKAGE))
    controller = native.NativeCloud(env)
    controller.state["preparation"] = {
        "status": "prepared",
        "service_started": True,
        "package_sha256": PACKAGE,
        "probe_source_sha256": PROBE,
        "grant_id": "approved-proof",
        "service_unit": "palimpsest-native-test.service",
        "config": {"dns": ["8.8.8.8", "8.8.4.4"]},
    }
    expiry = int(time.time()) + 2000
    grant = {
        "schema": native.GRANT_SCHEMA,
        "grant_id": "approved-proof",
        "project_id": PROJECT,
        "environment": "test",
        "source_digest": SOURCE,
        "source_package_sha256": PACKAGE,
        "probe_source_sha256": PROBE,
        "expires_unix": expiry,
        "cleanup_expires_unix": expiry + 3600,
        "max_seconds": 1800,
        "max_gpu_seconds": 1800,
        "actions": ["prepare", "run", "cleanup"],
        "boot_gib": 40,
        "limits": {"cpu_servers": 1, "gpu_servers": 1, "ports": 2, "images": 2, "volumes": 2, "access_rules": 2},
        "flavors": {"cpu": FLAVOR, "gpu": GPU_FLAVOR},
    }
    path = private_json(tmp_path / "grant.json", grant)
    return controller, env, conn, grant, path


def proof(run):
    runtime = run["plan"]["image"]["runtime"]
    return {
        "schema": "palimpsest.nova-ceph-boot-result.v1",
        "run_id": run["run_id"],
        "server_id": run["server_id"],
        "success": True,
        "root": {
            "pid1": 1,
            "filesystem": "overlay",
            "device": 20,
            "inode": 2,
            "mount_id": "42",
            "mountinfo": {"id": "42", "target": "/", "fstype": "overlay"},
            "proc1_same_root": True,
            "proc1_device": 20,
            "proc1_inode": 2,
        },
        "checks": {
            "carrier_inventory": True,
            "instance_metadata": True,
            "plan_policy": True,
            "upper_private": True,
            "provider_readonly_errno": errno.EACCES,
            "layers": [
                {"sha256": layer["digest"].split(":")[-1], "size": layer["size"]}
                for layer in run["plan"]["image"]["layers"]
            ],
        },
        "command": {
            "exit_code": 0,
            "uid": 0,
            "gid": 0,
            "entrypoint": runtime["entrypoint"],
            "argv": runtime["entrypoint"] + run["command"],
            "cmd_present": "cmd" in runtime,
        },
        "gpu": {"enabled": False},
    }


def cpu_run():
    return {
        "run_id": "native-test",
        "server_id": SERVER,
        "source_digest": SOURCE,
        "phase": "cpu",
        "command": ["/bin/true"],
        "plan": {"image": registry_image()},
    }


def test_budget_counts_all_failed_reservations_without_refund(setup):
    controller, _, _, grant, path = setup
    _, ledger = controller._grant(path, "run", "cuda")
    controller._reserve(grant, ledger, "cpu", 600)
    assert ledger["used"]["cpu_servers"] == 1
    assert all(ledger["used"][key] == 1 for key in native.KINDS)
    with pytest.raises(CloudEnvError, match="exhausted"):
        controller._reserve(grant, ledger, "cpu", 600)
    controller._reserve(grant, ledger, "gpu", 1800)
    assert ledger["gpu_seconds"] == 1800
    assert all(ledger["used"][key] == 2 for key in native.KINDS)


@pytest.mark.parametrize(
    "key,value",
    [
        ("project_id", "f" * 32),
        ("environment", "foreign"),
        ("source_digest", "sha256:" + "0" * 64),
        ("source_package_sha256", "0" * 64),
        ("expires_unix", 1),
        ("actions", ["run"]),
    ],
)
def test_grant_scope_refuses_effects(setup, key, value):
    controller, _, conn, grant, path = setup
    grant[key] = value
    private_json(path, grant)
    with pytest.raises(CloudEnvError):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert not conn.compute.mock_calls and not conn.network.mock_calls and not conn.block_storage.mock_calls


def test_grant_file_mode_and_duplicate_keys_are_rejected(setup):
    controller, _, _, _, path = setup
    path.chmod(0o644)
    with pytest.raises(CloudEnvError, match="private"):
        controller._grant(path, "run", "cuda")
    path.chmod(0o600)
    path.write_text('{"schema":"one","schema":"two"}')
    with pytest.raises(CloudEnvError, match="duplicate"):
        native._private_json(path)


def test_approval_mutation_is_detected_before_effect_boundary(setup):
    controller, _, _, grant, path = setup
    controller._grant(path, "run", "cuda")
    grant["boot_gib"] = 41
    private_json(path, grant)
    with pytest.raises(CloudEnvError, match="changed"):
        controller._check_time({**grant, "boot_gib": 40})


def test_expiry_allows_only_bounded_cleanup_tail(setup, monkeypatch):
    controller, _, _, grant, path = setup
    expiry = int(time.time()) - 10
    grant.update(expires_unix=expiry, cleanup_expires_unix=expiry + 3600)
    private_json(path, grant)
    controller._grant(path, "cleanup")
    with pytest.raises(CloudEnvError, match="expiry"):
        controller._grant(path, "run", "cuda")
    monkeypatch.setattr(native.time, "time", lambda: expiry + 3601)
    with pytest.raises(CloudEnvError, match="expiry"):
        controller._grant(path, "cleanup")


def test_gpu_without_cpu_proof_never_looks_up_or_creates_flavor(setup):
    controller, _, conn, _, path = setup
    with pytest.raises(CloudEnvError, match="CPU"):
        controller.run(grant_path=path, tag="cuda", phase="gpu", timeout=1800, command=native.GPU_COMMAND)
    assert not conn.compute.mock_calls and not conn.network.mock_calls


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result["root"].update(pid1=99),
        lambda result: result["root"].update(filesystem="ext4"),
        lambda result: result["root"].update(proc1_inode=999),
        lambda result: result["root"]["mountinfo"].update(fstype="ext4"),
        lambda result: result["checks"].update(provider_readonly_errno=None),
        lambda result: result["checks"]["layers"][0].update(sha256="0" * 64),
        lambda result: result["command"].update(argv=["/bin/true"]),
        lambda result: result["command"].update(exit_code=1),
    ],
)
def test_cpu_gate_requires_native_root_and_original_entrypoint_observations(mutation):
    run = cpu_run()
    observation = proof(run)
    assert native.NativeCloud._successful(run, observation)
    mutation(observation)
    assert not native.NativeCloud._successful(run, observation)


def test_gpu_gate_requires_smi_and_driver_api_identity_and_exact_results():
    run = cpu_run()
    run.update(phase="gpu", command=native.GPU_COMMAND)
    observation = proof(run)
    observation["gpu"] = {
        "enabled": True,
        "kernel_release": "6.8.0-147-generic",
        "driver_version": "580.178.04",
        "expected_pci_alias": "RTX-2000Ada:1,AD107-audio:1",
        "uuid": "GPU-test",
        "smi_uuid": "GPU-test",
        "pci": "0000:00:06.0",
        "computation": {
            "success": True,
            "device_index": 0,
            "device_uuid": "GPU-test",
            "results": [2, 6, 12, 20, 30, 42, 56, 72, 90],
            "checksum": 330,
            "runtime_version": 12020,
            "cublas_results": [90, 114, 138, 54, 69, 84, 18, 24, 30],
            "cublas_checksum": 621,
        },
    }
    assert native.NativeCloud._successful(run, observation)
    for key, value in (("kernel_release", "6.8.0-57-generic"), ("smi_uuid", "GPU-other")):
        changed = copy.deepcopy(observation)
        changed["gpu"][key] = value
        assert not native.NativeCloud._successful(run, changed)
    observation["gpu"]["computation"]["results"][-1] = 91
    assert not native.NativeCloud._successful(run, observation)


def tunnel_rule(**changes):
    raw = {
        "id": "ssh-ingress",
        "project_id": PROJECT,
        "security_group_id": "sg",
        "direction": "ingress",
        "ethertype": "IPv4",
        "protocol": "tcp",
        "port_range_min": 22,
        "port_range_max": 22,
        "remote_group_id": "sg",
        "remote_ip_prefix": None,
        "remote_address_group_id": None,
        **changes,
    }
    attrs = {("ether_type" if key == "ethertype" else key): value for key, value in raw.items()}
    return Obj(**attrs, _body=Obj(attributes=raw))


@pytest.mark.parametrize("user", ["1000", "1000:1000", "nobody"])
def test_native_nonroot_image_is_refused_before_policy_reads_or_budget_reservation(setup, monkeypatch, user):
    controller, env, conn, _, path = setup
    env.state["images"]["cuda"]["runtime"]["user"] = user
    env.state["images"]["cuda"]["source"]["config"]["config"]["User"] = user
    reserve = Mock(wraps=controller._reserve)
    monkeypatch.setattr(controller, "_reserve", reserve)
    with pytest.raises(CloudEnvError, match="root"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    reserve.assert_not_called()
    conn.network.get_security_group.assert_not_called()
    conn.network.create_port.assert_not_called()
    conn.compute.create_server.assert_not_called()


def launch_fixture(setup, monkeypatch, *, volume_outcome="normal"):
    controller, env, conn, grant, path = setup
    events = []
    conn.compute.get_flavor.return_value = Obj(id=FLAVOR, name="cpu.2c_4g")
    conn.compute.fetch_flavor_extra_specs.return_value = Obj(extra_specs={})
    env.record("security_groups", "env", {"id": "sg", "status": "confirmed"})
    conn.network.get_security_group.return_value = Obj(id="sg", project_id=PROJECT, stateful=True)
    conn.network.security_group_rules.return_value = [
        tunnel_rule(),
        tunnel_rule(direction="egress", protocol=None, port_range_min=None, port_range_max=None, remote_group_id=None),
    ]
    conn.network.get_network.return_value = Obj(
        id="network", project_id=PROJECT, subnet_ids=["subnet"], is_port_security_enabled=True
    )
    env.record("servers", "worker", {"id": "worker", "status": "confirmed", "address": "172.30.101.63"})
    conn.compute.get_server.return_value = Obj(
        id="worker",
        project_id=PROJECT,
        metadata={
            "palimpsest_env_id": env.state["marker"],
            "palimpsest_cloud_env": env.name,
            "palimpsest_role": "worker",
        },
    )
    worker_port = Obj(
        id="worker-port",
        project_id=PROJECT,
        device_id="worker",
        network_id="network",
        fixed_ips=[{"subnet_id": "subnet", "ip_address": "192.168.0.10"}],
        security_group_ids=["sg"],
        is_port_security_enabled=True,
    )
    conn.network.ports.return_value = [worker_port]
    port = Obj(
        id=PORT,
        project_id=PROJECT,
        network_id="network",
        fixed_ips=[{"subnet_id": "subnet", "ip_address": "192.168.0.20"}],
        security_group_ids=["sg"],
        device_id="",
        description="",
    )

    def create_port(**attrs):
        port.description = attrs["description"]
        events.append("port_post")
        return Obj(id=PORT)

    conn.network.create_port.side_effect = create_port
    conn.network.get_port.side_effect = lambda identity: worker_port if identity == "worker-port" else port
    conn.network.get_subnet.return_value = Obj(
        id="subnet",
        project_id=PROJECT,
        network_id="network",
        is_dhcp_enabled=True,
        cidr="192.168.0.0/24",
        gateway_ip="192.168.0.1",
        dns_nameservers=[],
    )
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(controller, "_check_time", lambda approved: None)

    def build(address, role, run, plan, deadline):
        run["qcow2"] = {"path": "/private/carrier.qcow2", "size": 200, "sha256": "6" * 64}
        run["client_cert_sha256"] = "8" * 64
        return {**plan, "carrier_digest": "sha256:" + "7" * 64}

    monkeypatch.setattr(controller, "_build", build)

    def upload(address, role, run, approved):
        image = Obj(
            id=IMAGE,
            owner_id=PROJECT,
            visibility="private",
            disk_format="qcow2",
            container_format="bare",
            properties={**controller._metadata(role, run), "hw_firmware_type": "bios"},
            architecture="x86_64",
            hw_disk_bus="virtio",
            hw_vif_model="virtio",
            status="active",
            size=200,
            hash_algo="sha256",
            hash_value="6" * 64,
        )
        env.record("native_images", role, {"id": IMAGE, "status": "confirmed"})
        conn.image.get_image.return_value = image
        return image

    monkeypatch.setattr(controller, "_upload_carrier", upload)
    created_volume = {}
    volume_reads = ["creating", "available", "available"]

    def create_volume(**attrs):
        events.append("volume_post")
        created_volume.update(attrs)
        if volume_outcome == "unknown":
            raise TimeoutError("POST outcome unknown")
        return Obj(id=VOLUME if volume_outcome != "missing-id" else None)

    conn.block_storage.create_volume.side_effect = create_volume

    def get_volume(identity):
        assert identity == VOLUME
        status = volume_reads.pop(0) if volume_reads else "in-use"
        events.append("volume_get_" + status)
        return Obj(
            id=VOLUME,
            project_id=PROJECT,
            status=status,
            size=40,
            metadata=created_volume["metadata"],
            volume_image_metadata={
                "image_id": IMAGE,
                "architecture": "x86_64",
                "hw_firmware_type": "bios",
                "hw_disk_bus": "virtio",
                "hw_vif_model": "virtio",
            },
            attachments=[] if status != "in-use" else [{"server_id": SERVER}],
        )

    conn.block_storage.get_volume.side_effect = get_volume

    def create_server(**attrs):
        events.append("nova_post")
        assert events.count("volume_get_available") >= 2
        assert attrs["block_device_mapping_v2"] == [
            {
                "boot_index": 0,
                "uuid": VOLUME,
                "source_type": "volume",
                "destination_type": "volume",
                "delete_on_termination": False,
            }
        ]
        port.device_id = SERVER
        conn.compute.get_server.return_value = Obj(
            id=SERVER,
            project_id=PROJECT,
            metadata=attrs["metadata"],
            flavor={"id": FLAVOR},
            attached_volumes=[{"id": VOLUME}],
            status="ACTIVE",
        )
        return Obj(id=SERVER)

    conn.compute.create_server.side_effect = create_server

    def install_tunnel(address, role, local, deadline):
        assert conn.compute.create_server.call_count == 0
        events.append("tunnel_key_installed")

    monkeypatch.setattr(controller, "_install_tunnel_key", install_tunnel)
    env.record("shares", "layers", {"id": "share", "status": "confirmed", "export": "10.0.0.1:6789:/share"})
    conn.shared_file_system.get_share.return_value = Obj(
        project_id=PROJECT, metadata={"palimpsest_cloud_env": env.name, "palimpsest_env_id": env.state["marker"]}
    )
    conn.shared_file_system.export_locations.return_value = [Obj(path="10.0.0.1:6789:/share")]

    def ro_grant(role, level):
        assert level == "ro" and conn.compute.create_server.call_count == 0
        events.append("ro_access_active")
        env.record("access_rules", role, {"id": "rule", "status": "confirmed", "access_to": role, "level": "ro"})
        conn.shared_file_system.access_rules.return_value = [
            Obj(
                id="rule",
                access_to=role,
                access_level="ro",
                access_type="cephx",
                state="active",
                access_key="secret-not-in-carrier",
            )
        ]
        env._write_private(env._secret_path(role), "secret-not-in-carrier")

    monkeypatch.setattr(env, "grant", ro_grant)
    monkeypatch.setattr(controller, "_remote_json", Mock())
    monkeypatch.setattr(controller, "_read_result", lambda address, run: proof(run))
    monkeypatch.setattr(controller, "cleanup", Mock(return_value={"status": "cleaned"}))
    monkeypatch.setattr(native.time, "sleep", lambda duration: None)
    return controller, env, conn, grant, path, events


@pytest.mark.parametrize(
    "missing", ["protocol", "port_range_min", "port_range_max", "remote_group_id", "remote_ip_prefix"]
)
def test_native_tunnel_missing_sdk_null_fields_are_not_an_allow_all_policy(setup, monkeypatch, missing):
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    rule = tunnel_rule(remote_group_id=None, protocol=None, port_range_min=None, port_range_max=None)
    rule._body.attributes.pop(missing)
    conn.network.security_group_rules.return_value[0] = rule
    reserve = Mock(wraps=controller._reserve)
    monkeypatch.setattr(controller, "_reserve", reserve)
    with pytest.raises(CloudEnvError, match="incomplete"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    reserve.assert_not_called()
    assert events == []


@pytest.mark.parametrize(
    "changes",
    [
        {"remote_group_id": None, "remote_ip_prefix": "203.0.113.0/24"},
        {"direction": "egress"},
        {"port_range_min": 23, "port_range_max": 23},
        {"protocol": "udp"},
        {"ethertype": "IPv6"},
        {"remote_group_id": "other-group"},
        {"remote_group_id": None, "remote_ip_prefix": "192.168.0.0/25"},
        {"remote_group_id": None, "remote_ip_prefix": "::/0"},
        {"remote_group_id": None, "remote_address_group_id": "unsupported"},
        {"project_id": "foreign"},
        {"security_group_id": "foreign"},
    ],
)
def test_native_tunnel_rejects_unproven_rules_before_reservation(setup, monkeypatch, changes):
    controller, env, conn, _, path, events = launch_fixture(setup, monkeypatch)
    conn.network.security_group_rules.return_value[0] = tunnel_rule(**changes)
    reserve = Mock(wraps=controller._reserve)
    monkeypatch.setattr(controller, "_reserve", reserve)
    with pytest.raises(CloudEnvError):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    reserve.assert_not_called()
    assert not controller.state["runs"] and not events
    assert all(value == 0 for value in controller.state["grants"]["approved-proof"]["used"].values())
    conn.network.create_port.assert_not_called()
    conn.image.create_image.assert_not_called()
    conn.block_storage.create_volume.assert_not_called()
    conn.compute.create_server.assert_not_called()
    conn.network.create_security_group_rule.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"remote_group_id": None, "remote_ip_prefix": "192.168.0.0/24"},
        {"remote_group_id": None, "remote_ip_prefix": "192.168.0.0/16", "protocol": "6"},
        {"remote_group_id": None, "remote_ip_prefix": "0.0.0.0/0"},
        {"remote_group_id": None, "protocol": None, "port_range_min": None, "port_range_max": None},
        {"port_range_min": 1, "port_range_max": 1024},
    ],
)
def test_native_tunnel_proven_rules_allow_the_real_allocation_path(setup, monkeypatch, changes):
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    conn.network.security_group_rules.return_value[0] = tunnel_rule(**changes)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "success" and events.count("nova_post") == 1
    assert conn.network.security_group_rules.call_count == 3
    assert conn.network.create_port.call_args.kwargs["security_group_ids"] == ["sg"]
    conn.network.create_security_group_rule.assert_not_called()


@pytest.mark.parametrize(
    "authority",
    [
        "group",
        "network",
        "subnet",
        "worker-group",
        "missing-rules",
        "egress",
        "stateless",
        "worker-port-security",
        "network-port-security",
    ],
)
def test_native_tunnel_rejects_missing_or_foreign_authority_without_spending(setup, monkeypatch, authority):
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    if authority == "group":
        conn.network.get_security_group.return_value.project_id = "foreign"
    elif authority == "network":
        conn.network.get_network.return_value.project_id = "foreign"
    elif authority == "subnet":
        conn.network.get_subnet.return_value.project_id = "foreign"
    elif authority == "worker-group":
        conn.network.ports.return_value[0].security_group_ids = ["other-group"]
    elif authority == "missing-rules":
        conn.network.security_group_rules.return_value = []
    elif authority == "stateless":
        conn.network.get_security_group.return_value.stateful = False
    elif authority == "worker-port-security":
        conn.network.ports.return_value[0].is_port_security_enabled = False
    elif authority == "network-port-security":
        conn.network.get_network.return_value.is_port_security_enabled = False
    else:
        conn.network.security_group_rules.return_value.pop()
    reserve = Mock(wraps=controller._reserve)
    monkeypatch.setattr(controller, "_reserve", reserve)
    with pytest.raises(CloudEnvError):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    reserve.assert_not_called()
    assert events == []


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"remote_group_id": None, "remote_ip_prefix": "192.168.0.0/24"},
        {"remote_group_id": None, "protocol": None, "port_range_min": None, "port_range_max": None},
    ],
)
def test_native_tunnel_actual_sdk_resource_shapes_allow_allocation(setup, monkeypatch, changes):
    sdk_rule = pytest.importorskip("openstack.network.v2.security_group_rule").SecurityGroupRule
    sdk_network = pytest.importorskip("openstack.network.v2.network").Network
    sdk_group = pytest.importorskip("openstack.network.v2.security_group").SecurityGroup
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    conn.network.get_security_group.return_value = sdk_group.existing(id="sg", project_id=PROJECT, stateful=True)
    conn.network.get_network.return_value = sdk_network.existing(
        id="network", project_id=PROJECT, subnets=["subnet"], port_security_enabled=True
    )
    rules = conn.network.security_group_rules.return_value
    rules[0] = tunnel_rule(**changes)
    conn.network.security_group_rules.return_value = [sdk_rule.existing(**rule._body.attributes) for rule in rules]
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "success" and events.count("nova_post") == 1


def test_native_tunnel_actual_sdk_omitted_protocol_does_not_become_allow_all(setup, monkeypatch):
    sdk_rule = pytest.importorskip("openstack.network.v2.security_group_rule").SecurityGroupRule
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    raw = tunnel_rule(remote_group_id=None, protocol=None, port_range_min=None, port_range_max=None)._body.attributes
    raw.pop("protocol")
    rule = sdk_rule.existing(**raw)
    assert rule.protocol is None
    conn.network.security_group_rules.return_value[0] = rule
    reserve = Mock(wraps=controller._reserve)
    monkeypatch.setattr(controller, "_reserve", reserve)
    with pytest.raises(CloudEnvError, match="incomplete"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    reserve.assert_not_called()
    assert events == []


def test_native_tunnel_cidr_must_cover_every_selected_network_subnet(setup, monkeypatch):
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    subnet = conn.network.get_subnet.return_value
    other = Obj(**{**vars(subnet), "id": "other-subnet", "cidr": "192.168.1.0/24"})
    conn.network.get_network.return_value.subnet_ids = ["subnet", "other-subnet"]
    conn.network.get_subnet.side_effect = lambda identity: subnet if identity == "subnet" else other
    conn.network.security_group_rules.return_value[0] = tunnel_rule(
        remote_group_id=None, remote_ip_prefix="192.168.0.0/24"
    )
    with pytest.raises(CloudEnvError, match="no reservation"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert not events
    conn.network.security_group_rules.return_value.append(
        tunnel_rule(remote_group_id=None, remote_ip_prefix="192.168.1.0/24")
    )
    assert (
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])["status"]
        == "success"
    )


def test_native_tunnel_policy_is_rechecked_before_nova_without_refunding_the_grant(setup, monkeypatch):
    controller, _, conn, _, path, events = launch_fixture(setup, monkeypatch)
    rules = conn.network.security_group_rules.return_value
    conn.network.security_group_rules.side_effect = [rules, rules, []]
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["outcome"] == "held" and "volume_post" in events and "nova_post" not in events
    assert controller.state["grants"]["approved-proof"]["used"]["cpu_servers"] == 1
    conn.compute.create_server.assert_not_called()
    controller._remote_json.assert_not_called()


def test_one_nova_post_follows_exact_available_volume_and_enrollment_follows_binding(setup, monkeypatch):
    controller, env, conn, _, path, events = launch_fixture(setup, monkeypatch)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "success" and report["exit_code"] == 0
    assert events.count("volume_post") == 1 and events.count("nova_post") == 1
    assert conn.block_storage.volumes.call_count == 0
    enrollment = controller._remote_json.call_args.args[2]
    assert enrollment["plan"]["server_id"] == SERVER
    assert enrollment["ceph_access"]["secret"] == "secret-not-in-carrier"
    assert env.resource("native_servers", report["run_id"])["boot_volumes"] == [VOLUME]
    controller.cleanup.assert_called_once_with(grant_path=path, run_id=report["run_id"])


def test_builder_error_never_exposes_pipe_output(setup, monkeypatch):
    controller, _, _, grant, _, _, _, key_body = builder_fixture(setup, monkeypatch)
    role = "native-safe-error"
    run = {"run_id": role, "nonce": "private-nonce", "source_digest": SOURCE, "phase": "cpu"}
    controller.state["runs"][role] = run
    with pytest.raises(CloudEnvError, match="private diagnostic status retained") as failure:
        controller._build("172.30.101.63", role, run, {"run_id": role}, grant["expires_unix"])
    assert run["build_failure_capture"]["status"] == "captured"
    assert all(value not in str(failure.value) for value in ("Traceback", key_body, run["nonce"]))
    assert failure.value.__cause__ is None
    controller.cleanup.assert_not_called()


@pytest.mark.parametrize("exit_status", [255, -15])
def test_completed_ssh_failure_does_not_establish_remote_actor_exit(setup, monkeypatch, exit_status):
    controller, _, _, _, path, _, _, _ = builder_fixture(setup, monkeypatch, exit_status=exit_status, terminal=False)

    def cleanup(**kwargs):
        controller._carrier_terminal("172.30.101.63", controller.state["runs"][kwargs["run_id"]])

    controller.cleanup.side_effect = cleanup
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "cleanup_failed" and report["outcome"] == "held"
    assert not controller.state["runs"][report["run_id"]].get("carrier_build_terminal")
    diagnostic = json.loads(Path(report["build_failure_capture"]["path"]).read_bytes())
    assert diagnostic["transport_client_exit_code"] == exit_status and diagnostic["remote_outcome"] == "unconfirmed"


def builder_fixture(setup, monkeypatch, *, exit_status=1, interrupted=None, terminal=True):
    from palimpsest_local import nova_bootstrap_service
    from palimpsest_local.nova_ceph_boot import plan_policy_digest

    controller, env, conn, grant, path, events = launch_fixture(setup, monkeypatch)
    monkeypatch.setattr(controller, "_build", native.NativeCloud._build.__get__(controller))
    monkeypatch.setattr(controller, "_verify_prepared_artifacts", Mock())
    monkeypatch.setattr(controller, "_worker_fixed_address", lambda address: (address, ["pinned host key"]))
    monkeypatch.setattr(env, "ssh_argv", lambda address: ["ssh", address])
    preparation = controller.state["preparation"]
    controller.directory.mkdir(mode=0o700, parents=True)
    ca = controller.directory / "ca.cert"
    ca.write_bytes(b"public CA")
    preparation.update(ca_cert_file=str(ca), ca_key_file="unused")
    preparation["config"].update(bundle_path="/private/bundle.json", runtime_script_path="/private/runtime.py")
    key_body = "b3BhcXVlLXByaXZhdGUtY2Fycmllci1rZXk="
    key = "-----BEGIN " + "PRIVATE KEY-----\n" + key_body + "\n-----END " + "PRIVATE KEY-----\n"

    def certificate(local, ca_cert, ca_key, *, run_id):
        cert = local / "client.cert"
        cert.write_bytes(b"public certificate")
        private = local / ("client-" + run_id + ".key")
        env._write_private(private, key)
        return {"cert_file": str(cert), "key_file": str(private), "cert_sha256": "8" * 64}

    def tunnel(address, role, local, deadline):
        env._write_private(local / "ssh_private_key", key)

    monkeypatch.setattr(nova_bootstrap_service, "issue_client_certificate", certificate)
    monkeypatch.setattr(controller, "_tunnel_key", tunnel)
    monkeypatch.setattr(controller, "_verify_carrier_manifest", Mock())
    processes = []

    def popen(argv, **kwargs):
        command = argv[-1]
        run = next(iter(controller.state["runs"].values()))
        stdout, stderr, code = b"", b"", 0
        if "systemd-run" in command:
            events.append("builder")
            assert "--wait --pipe" in command
            assert "--property=KillSignal=SIGTERM" in command
            assert "--property=SendSIGKILL=no" in command
            stdout = (
                "진행\n" * 30000
                + run["nonce"]
                + "\n"
                + key_body
                + "\n"
                + "API_KEY=labelled-api-value\nhttps://user:url-pass@registry.invalid/\n"
                + "Authorization: Bearer labelled-auth-value\n"
                + "copying encodings/iso8859_10.py\n"
            ).encode()
            stderr = (
                "\x00" * 100000
                + key
                + "unlabelled "
                + key_body
                + "\n"
                + "password="
                + "labelled-password-value" * 10000
                + "\n"
                + "access_key=labelled-access-value\n"
                + "Traceback: OSError: copy failed\n"
            ).encode()
            code = exit_status
        elif "systemctl show" in command:
            events.append("terminal_check")
            stdout = (
                b"ActiveState=failed\nLoadState=loaded\n" if terminal else b"ActiveState=active\nLoadState=loaded\n"
            )
        elif "build-state.json" in command:
            stdout = b"FAILED_PRESERVED\n"
        elif "/carrier/result.json" in command:
            events.append("result_read")
            plan = controller._remote_json.call_args.args[2]
            built = {**plan, "carrier_digest": "sha256:" + "7" * 64}
            stdout = json.dumps(
                {
                    "schema": "palimpsest.nova-boot-carrier.v1",
                    "state": "BUILT_NOT_BOOT_PROVEN",
                    "plan": built,
                    "carrier_digest": built["carrier_digest"],
                    "manifest_digest": built["carrier_digest"],
                    "expected_plan_digest": plan_policy_digest(built),
                    "artifacts": {"raw_disk": {"path": run["remote"] + "/carrier/carrier.raw", "size": 4 * 1024**3}},
                }
            ).encode()
        elif "/carrier/artifacts.json" in command:
            stdout = b"{}"
        elif "sudo sha256sum" in command:
            stdout = (("6" * 64) + " file\n" + ("9" * 128) + " file\n200\n").encode()
        process = Mock(pid=1234 + len(processes), returncode=code)
        if "systemd-run" in command and interrupted is not None:
            process.communicate.side_effect = [interrupted, (b"", b"")]
        else:
            process.communicate.return_value = (stdout, stderr)
        processes.append((command, process))
        return process

    monkeypatch.setattr(native.subprocess, "Popen", popen)
    return controller, env, conn, grant, path, events, processes, key_body


@pytest.mark.parametrize("exit_status", [1, 255, -15])
def test_builder_nonzero_retains_private_redacted_diagnostic_before_exact_cleanup(setup, monkeypatch, exit_status):
    controller, env, conn, _, path, events, _, key_body = builder_fixture(setup, monkeypatch, exit_status=exit_status)
    monkeypatch.setattr(controller, "cleanup", native.NativeCloud.cleanup.__get__(controller))
    monkeypatch.setattr(controller, "_revoke_enrollment", Mock())
    monkeypatch.setattr(controller, "_remove_tunnel_key", Mock())

    def delete_port(identity, *, ignore_missing):
        assert identity == PORT and ignore_missing is False
        run = next(iter(controller.state["runs"].values()))
        capture = run["build_failure_capture"]
        assert json.loads(Path(capture["path"]).read_bytes())["transport_client_exit_code"] == exit_status
        persisted = json.loads(env.state_path.read_bytes())
        assert persisted["native"]["runs"][run["run_id"]]["build_failure_capture"] == capture
        events.append("exact_port_cleanup")
        from openstack.exceptions import NotFoundException

        conn.network.get_port.side_effect = NotFoundException("absent", http_status=404)

    conn.network.delete_port.side_effect = delete_port
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "cleaned" and report["outcome"] == "held"
    assert report["failure_phase"] == "creating" and report["error_type"] == "CloudEnvError"
    assert events.index("builder") < events.index("exact_port_cleanup") < events.index("terminal_check")
    capture = report["build_failure_capture"]
    assert set(capture) == {"status", "path", "sha256"} and capture["status"] == "captured"
    artifact = Path(capture["path"])
    payload = artifact.read_bytes()
    record = json.loads(payload)
    assert record["run_id"] == report["run_id"]
    assert record["transport_client_exit_code"] == exit_status and record["remote_outcome"] == "unconfirmed"
    assert record["qualification"] is False
    assert "iso8859_10.py" in record["stdout"] and "OSError: copy failed" in record["stderr"]
    assert len(record["stdout"].encode()) <= 64 * 1024 and len(record["stderr"].encode()) <= 64 * 1024
    assert len(payload) <= 1024 * 1024
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert hashlib.sha256(payload).hexdigest() == capture["sha256"]
    run = controller.state["runs"][report["run_id"]]
    for secret in (
        run["nonce"],
        key_body,
        "labelled-api-value",
        "url-pass",
        "labelled-auth-value",
        "labelled-password-value",
        "labelled-access-value",
    ):
        assert secret not in payload.decode() and secret not in json.dumps(report)
    assert "Traceback" not in json.dumps(report)
    assert run["carrier_build_terminal"] is True
    conn.compute.create_server.assert_not_called()
    conn.block_storage.create_volume.assert_not_called()
    conn.compute.get_server_console_output.assert_not_called()
    assert env.resource("native_ports", run["run_id"]) is None


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_builder_failure_diagnostic_never_clobbers_existing_path(setup, monkeypatch, existing):
    controller, _, _, _, path, _, _, _ = builder_fixture(setup, monkeypatch)
    original = controller._remote_json
    artifacts = []

    def preexisting(address, remote, plan):
        run_id = plan["run_id"]
        artifact = controller.directory / (run_id + ".build-failure.json")
        target = controller.directory / "historical-private"
        target.write_bytes(b"historical bytes")
        if existing == "symlink":
            artifact.symlink_to(target)
        else:
            artifact.write_bytes(b"historical bytes")
        artifacts.extend((artifact, target))
        original(address, remote, plan)

    monkeypatch.setattr(controller, "_remote_json", preexisting)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["build_failure_capture"] == {"status": "unavailable"}
    assert all(artifact.read_bytes() == b"historical bytes" for artifact in artifacts)
    controller.cleanup.assert_called_once_with(grant_path=path, run_id=report["run_id"])


def test_builder_success_has_no_failure_artifact_and_keeps_terminal_gate(setup, monkeypatch):
    controller, _, conn, _, path, events, _, _ = builder_fixture(setup, monkeypatch, exit_status=0)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "success" and report["exit_code"] == 0
    assert "build_failure_capture" not in report
    assert not list(controller.directory.glob("*.build-failure.json"))
    assert events.index("builder") < events.index("terminal_check") < events.index("result_read")
    conn.compute.create_server.assert_called_once()


@pytest.mark.parametrize("interrupted", [subprocess.TimeoutExpired("ssh", 1), KeyboardInterrupt()])
def test_builder_interruption_remains_unknown_hold_without_confirmed_exit_diagnostic(setup, monkeypatch, interrupted):
    controller, _, conn, _, path, _, processes, _ = builder_fixture(
        setup, monkeypatch, interrupted=interrupted, terminal=False
    )

    def cleanup(**kwargs):
        run = controller.state["runs"][kwargs["run_id"]]
        controller._carrier_terminal("172.30.101.63", run)

    controller.cleanup.side_effect = cleanup
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "cleanup_failed" and report["outcome"] == "held"
    assert "build_failure_capture" not in report
    assert not list(controller.directory.glob("*.build-failure.json"))
    run = controller.state["runs"][report["run_id"]]
    assert not run.get("carrier_build_terminal")
    assert controller.state["transport_holds"][-1]["remote_outcome"] == "unknown"
    process = next(process for command, process in processes if "systemd-run" in command)
    process.terminate.assert_called_once()
    process.kill.assert_not_called()
    assert process.communicate.call_args_list[-1].kwargs == {"timeout": 10}
    conn.compute.create_server.assert_not_called()


def test_result_timeout_preserves_redacted_private_console_before_cleanup(setup, monkeypatch):
    controller, env, conn, _, path, events = launch_fixture(setup, monkeypatch)
    del conn.compute.timeout  # Real SDK proxies have no per-proxy timeout attribute.
    clock = [time.time()]
    key_body = "b3BhcXVlLXByaXZhdGUta2V5LWJvZHk="
    from palimpsest_local.nova_ceph_boot import _failure_diagnostic

    diagnostic = _failure_diagnostic("prepare_identity_files", FileNotFoundError(2, "secret-not-in-carrier"))

    def missing_result(address, run):
        directory = controller.directory / run["run_id"]
        directory.mkdir(mode=0o700, parents=True)
        for name in ("ssh_private_key", "client-" + run["run_id"] + ".key"):
            env._write_private(
                directory / name, "-----BEGIN " + "PRIVATE KEY-----\n" + key_body + "\n-----END " + "PRIVATE KEY-----\n"
            )
        conn.compute.get_server_console_output.return_value = {
            "output": "진행\n" * 45000
            + "Linux kernel started\n"
            + key_body
            + "\n"
            + "secret-not-in-carrier\n"
            + run["nonce"]
            + "\nAuthorization: Bearer other-credential\n"
            + "palimpsest native boot HOLD: network_interface\n"
            + "palimpsest native boot diagnostic: "
            + json.dumps(diagnostic)
            + "\n"
        }
        clock[0] = run["deadline"] + 1

    def console(server, length):
        events.append("console_capture")
        return conn.compute.get_server_console_output.return_value

    def cleanup(**kwargs):
        events.append("cleanup")
        run = controller.state["runs"][kwargs["run_id"]]
        run.update(status="cleaned", enrolled=False, enrollment_intent=False)

    monkeypatch.setattr(native.time, "time", lambda: clock[0])
    monkeypatch.setattr(controller, "_read_result", missing_result)
    conn.compute.get_server_console_output.side_effect = console
    controller.cleanup.side_effect = cleanup
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "cleaned" and report["outcome"] == "held"
    assert report["failure_phase"] == "bootstrapping"
    assert report["nova_active_unix"] <= report["enrollment_published_unix"] < report["deadline"]
    assert events.index("console_capture") < events.index("cleanup")
    evidence_path = Path(report["console_capture"]["path"])
    console_record = json.loads(evidence_path.read_bytes())
    assert console_record["qualification"] is False
    assert "network_interface" in console_record["console"]
    retained_diagnostic = json.loads(console_record["console"].split("palimpsest native boot diagnostic: ", 1)[1])
    assert retained_diagnostic == {"stage": "prepare_identity_files", "error_type": "FileNotFoundError", "errno": 2}
    assert len(console_record["console"].encode()) <= 256 * 1024
    assert all(
        value not in evidence_path.read_text() for value in (key_body, "secret-not-in-carrier", "other-credential")
    )
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    before = evidence_path.read_bytes()
    assert controller._capture_failure_console(controller.state["runs"][report["run_id"]])["status"] == "unavailable"
    assert evidence_path.read_bytes() == before


@pytest.mark.parametrize(
    "response", [RuntimeError("secret provider text"), {"output": 123}, {"output": "x" * (1024 * 1024 + 1)}]
)
def test_console_failure_never_prevents_cleanup_or_leaks_provider_error(setup, monkeypatch, response):
    controller, _, conn, _, path, _ = launch_fixture(setup, monkeypatch)
    monkeypatch.setattr(controller, "_read_result", Mock(side_effect=CloudEnvError("result unavailable")))
    if isinstance(response, Exception):
        conn.compute.get_server_console_output.side_effect = response
    else:
        conn.compute.get_server_console_output.return_value = response
    prior_timeout = conn.compute.session.timeout
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["console_capture"]["status"] == "unavailable"
    assert "secret provider text" not in json.dumps(report)
    assert conn.compute.session.timeout is prior_timeout
    controller.cleanup.assert_called_once()


def test_console_cancellation_still_restores_transport_and_cleans(setup, monkeypatch):
    controller, _, conn, _, path, _ = launch_fixture(setup, monkeypatch)
    monkeypatch.setattr(controller, "_read_result", Mock(side_effect=CloudEnvError("result unavailable")))
    conn.compute.get_server_console_output.side_effect = KeyboardInterrupt()
    previous = conn.compute.session.timeout
    with pytest.raises(KeyboardInterrupt):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert conn.compute.session.timeout is previous
    controller.cleanup.assert_called_once()


def test_console_capture_does_not_read_foreign_server(setup):
    controller, _, conn, _, _ = setup
    conn.compute.get_server.return_value = Obj(id=SERVER, project_id="foreign")
    result = controller._capture_failure_console({"run_id": "native-owned", "server_id": SERVER, "nonce": "1" * 32})
    assert result["status"] == "unavailable"
    conn.compute.get_server_console_output.assert_not_called()


@pytest.mark.parametrize("outcome", ["unknown", "missing-id"])
def test_uncertain_volume_create_is_durable_hold_never_nova_or_replay(setup, monkeypatch, outcome):
    controller, env, conn, _, path, _ = launch_fixture(setup, monkeypatch, volume_outcome=outcome)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["status"] == "held" and report["exit_code"] == 1
    assert env.resource("native_volumes", report["run_id"])["status"] == "creating"
    conn.compute.create_server.assert_not_called()
    controller._remote_json.assert_not_called()
    with pytest.raises(CloudEnvError, match="prior native run"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert conn.block_storage.create_volume.call_count == 1


def test_cleanup_refuses_foreign_port_and_preserves_ledger(setup, monkeypatch):
    controller, env, conn, grant, path = setup
    run = {**cpu_run(), "grant_id": grant["grant_id"], "package_sha256": PACKAGE, "status": "held"}
    controller.state["runs"][run["run_id"]] = run
    env.record(
        "native_ports",
        run["run_id"],
        {
            "id": PORT,
            "status": "confirmed",
            "description": "owned",
            "fixed_ips": [{"ip_address": "192.168.0.20"}],
            "security_group_ids": ["sg"],
        },
    )
    conn.network.get_port.return_value = Obj(project_id="foreign", network_id="network", description="owned")
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(env, "_revoke", lambda role: None)
    report = controller.cleanup(grant_path=path, run_id=run["run_id"])
    assert report["status"] == "cleanup_failed"
    conn.network.delete_port.assert_not_called()
    assert env.resource("native_ports", run["run_id"])["id"] == PORT


def test_cleanup_never_calls_503_absent(setup, monkeypatch):
    controller, env, conn, grant, path = setup
    pytest.importorskip("openstack.exceptions")
    run = {**cpu_run(), "grant_id": grant["grant_id"], "package_sha256": PACKAGE, "status": "held"}
    controller.state["runs"][run["run_id"]] = run
    env.record("native_servers", run["run_id"], {"id": SERVER, "status": "confirmed", "boot_volumes": [VOLUME]})
    conn.compute.get_server.side_effect = RuntimeError("503 unavailable")
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(env, "_revoke", lambda role: None)
    report = controller.cleanup(grant_path=path, run_id=run["run_id"])
    assert report["status"] == "cleanup_failed"
    assert env.resource("native_servers", run["run_id"])["id"] == SERVER
    conn.compute.delete_server.assert_not_called()


def test_hashing_upload_stream_is_bounded_and_exact():
    payload = b"a" * (2 * 1024 * 1024 + 3)
    stream = native._HashingStream(io.BytesIO(payload))
    chunks = list(iter(lambda: stream.read(), b""))
    assert max(map(len, chunks)) <= 1024 * 1024
    assert b"".join(chunks) == payload and stream.count == len(payload)
    assert stream.sha256.hexdigest() == hashlib.sha256(payload).hexdigest()


def test_native_parser_routes_all_explicit_lifecycle_operations():
    parser = argparse.ArgumentParser()
    cloud_cli.add_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "cloud",
            "native",
            "run",
            "test",
            "cuda",
            "--grant",
            "grant.json",
            "--phase",
            "gpu",
            "--timeout",
            "1800",
            "--",
            "/run/palimpsest-probe/cuda-probe",
        ]
    )
    assert args.native_operation == "run" and args.command == native.GPU_COMMAND
    assert args.keep is False
    args = parser.parse_args(["cloud", "native", "cleanup", "test", "--grant", "grant.json", "--stop-service"])
    assert args.run_id is None and args.stop_service
    args = parser.parse_args(
        [
            "cloud",
            "native",
            "prepare",
            "test",
            "--grant",
            "grant.json",
            "--config",
            "prepare.json",
        ]
    )
    assert args.tag is None


def test_status_never_clears_cleanup_failure_on_a_successful_workload(setup, monkeypatch):
    controller, _, _, grant, _ = setup
    run = {
        **cpu_run(),
        "grant_id": grant["grant_id"],
        "package_sha256": PACKAGE,
        "status": "cleanup_failed",
        "outcome": "success",
        "exit_code": 1,
        "enrolled": True,
        "cleanup_errors": [{"resource": "server"}],
    }
    controller.state["runs"][run["run_id"]] = run
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(controller, "_read_result", lambda address, item: proof(item))
    report = controller.status(run_id=run["run_id"])
    assert report["status"] == "cleanup_failed" and report["exit_code"] == 1
    assert report["verified_result"]["success"]


def test_new_cleanup_only_approval_can_recover_an_expired_original_grant(setup):
    controller, _, conn, grant, path = setup
    controller._grant(path, "run", "cuda")
    run = {**cpu_run(), "grant_id": grant["grant_id"], "package_sha256": PACKAGE, "status": "held"}
    recovery = {
        **grant,
        "grant_id": "new-cleanup-only",
        "actions": ["cleanup"],
        "cleanup_of_grant_id": grant["grant_id"],
    }
    private_json(path, recovery)
    approved, _ = controller._grant(path, "cleanup")
    controller._cleanup_boundary(approved, path, run)
    assert not conn.compute.mock_calls and not conn.network.mock_calls
    with pytest.raises(CloudEnvError):
        controller._grant(path, "run", "cuda")


def test_native_ssh_timeout_sends_term_without_kill_and_records_unknown(setup, monkeypatch):
    controller, _, _, _, _ = setup
    process = Mock(pid=1234)
    process.communicate.side_effect = [subprocess.TimeoutExpired("ssh", 1), (b"", b"")]
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(CloudEnvError, match="remote outcome unknown"):
        controller._ssh("172.30.101.63", "private-operation", timeout=1)
    process.terminate.assert_called_once()
    process.kill.assert_not_called()
    assert controller.state["transport_holds"][-1]["remote_outcome"] == "unknown"


def test_worker_fixed_ip_reuses_only_fresh_worker_binding_and_console_pin(setup):
    controller, env, conn, _, _ = setup
    env.record("servers", "worker", {"id": SERVER, "status": "confirmed", "address": "172.30.101.63"})
    conn.compute.get_server.return_value = Obj(
        project_id=PROJECT,
        metadata={
            "palimpsest_env_id": env.state["marker"],
            "palimpsest_cloud_env": env.name,
            "palimpsest_role": "worker",
        },
    )
    port = Obj(
        id=PORT, project_id=PROJECT, network_id="network", device_id=SERVER, fixed_ips=[{"ip_address": "192.168.0.10"}]
    )
    conn.network.ports.return_value = [port, Obj(network_id="foreign", device_id="other")]
    conn.network.get_port.return_value = port
    env._write_private(env.known_hosts, "172.30.101.63 ssh-ed25519 pinned-key\nother ssh-ed25519 foreign-key\n")
    address, pins = controller._worker_fixed_address("172.30.101.63")
    assert address == "192.168.0.10" and pins == ["192.168.0.10 ssh-ed25519 pinned-key"]
    conn.network.get_port.return_value = Obj(project_id="foreign")
    with pytest.raises(CloudEnvError, match="project"):
        controller._worker_fixed_address("172.30.101.63")


@pytest.mark.parametrize(
    "outcome",
    ["success", "exception", "cancel", "deadline", "short", "wrong-hash", "bad-status", "term-hold"],
)
def test_carrier_upload_streams_only_the_worker_carrier_after_durable_image_id(setup, monkeypatch, outcome):
    controller, env, conn, grant, _ = setup
    payload = b"private carrier bytes"
    role = "native-upload"
    run = {
        "run_id": role,
        "grant_id": grant["grant_id"],
        "source_digest": SOURCE,
        "deadline": int(time.time()) + 60,
        "qcow2": {
            "path": "/private/carrier.qcow2",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    if outcome == "short":
        run["qcow2"]["size"] += 1
    if outcome == "wrong-hash":
        run["qcow2"]["sha256"] = "0" * 64
    original_timeout = (40, 41)
    conn.image.session.timeout = original_timeout
    conn.image.connect_retries = 2
    conn.image.status_code_retries = 5
    reader, writer = os.pipe()
    os.write(writer, payload)
    os.close(writer)
    process = Mock(stdout=os.fdopen(reader, "rb"))
    process.wait.return_value = 0
    process.poll.return_value = 0
    if outcome != "success":
        process.poll.return_value = None
    if outcome == "term-hold":
        process.pid = 1234
        process.wait.side_effect = subprocess.TimeoutExpired("ssh", 10)
    captured = []
    image = Obj(
        id=IMAGE,
        owner_id=PROJECT,
        visibility="private",
        disk_format="qcow2",
        container_format="bare",
        properties={**controller._metadata(role, run), "hw_firmware_type": "bios"},
        status="active",
        size=len(payload),
        architecture="x86_64",
        hw_disk_bus="virtio",
        hw_vif_model="virtio",
        hash_algo="sha256",
        hash_value=run["qcow2"]["sha256"],
    )

    def upload(url, *, data, headers, timeout, **options):
        assert env.resource("native_images", role)["id"] == IMAGE
        assert 0 < timeout <= run["deadline"] - time.time() + 0.1
        assert conn.image.session.timeout is original_timeout
        assert conn.image.connect_retries == 2
        assert conn.image.status_code_retries == 5
        if outcome in ("exception", "term-hold"):
            raise OSError(errno.ECONNRESET, "synthetic transport interruption")
        if outcome == "cancel":
            raise KeyboardInterrupt
        captured.extend(iter(lambda: data.read(4), b""))
        if outcome == "deadline":
            monkeypatch.setattr(native.time, "time", lambda: run["deadline"] + 1)
        return Obj(status_code=503 if outcome == "bad-status" else 204)

    conn.image.put.side_effect = upload
    conn.image.create_image.return_value = image
    conn.image.get_image.return_value = image
    popen = Mock(return_value=process)
    monkeypatch.setattr(native.subprocess, "Popen", popen)
    monkeypatch.setattr(controller, "_check_time", lambda approved: None)
    if outcome == "success":
        assert controller._upload_carrier("172.30.101.63", role, run, grant) is image
    else:
        error = (
            KeyboardInterrupt
            if outcome == "cancel"
            else OSError
            if outcome in ("exception", "term-hold")
            else CloudEnvError
        )
        with pytest.raises(error):
            controller._upload_carrier("172.30.101.63", role, run, grant)
        conn.image.get_image.assert_not_called()
        process.terminate.assert_called_once_with()
    assert conn.image.session.timeout is original_timeout
    assert conn.image.connect_retries == 2
    assert conn.image.status_code_retries == 5
    conn.image.put.assert_called_once()
    conn.image.create_image.assert_called_once()
    assert process.stdout.closed
    assert not process.kill.called
    if outcome == "term-hold":
        assert run["upload_transport_hold"] == {"pid": 1234, "retained_client": True}
    if outcome not in ("exception", "cancel", "term-hold"):
        assert b"".join(captured) == payload


@pytest.mark.parametrize("status,outcome", [("success", None), ("cleaned", "creating"), ("cleaned", "failed")])
def test_uncleaned_or_interrupted_prior_run_blocks_new_allocations(setup, status, outcome):
    controller, _, conn, _, path = setup
    controller.state["runs"]["prior"] = {"status": status, "outcome": outcome}
    with pytest.raises(CloudEnvError, match="prior native run"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    conn.compute.create_server.assert_not_called()
    conn.network.create_port.assert_not_called()


@pytest.mark.parametrize("kind", ["native_ports", "native_ssh_keys", "access_rules"])
def test_orphan_native_records_block_reservation_even_without_run_record(setup, kind):
    controller, env, conn, _, path = setup
    env.record(kind, "native-orphan", {"id": PORT, "status": "confirmed"})
    with pytest.raises(CloudEnvError, match="orphan native resources"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    conn.compute.get_flavor.assert_not_called()
    assert not controller.state["runs"]


def test_preparation_uses_outer_expiry_but_nova_enrollment_gets_new_allocation_window(setup, monkeypatch):
    controller, _, _, grant, path, events = launch_fixture(setup, monkeypatch)
    observed_seed_deadlines = []
    original_build = controller._build

    def build(address, role, run, plan, deadline):
        observed_seed_deadlines.append(deadline)
        return original_build(address, role, run, plan, deadline)

    monkeypatch.setattr(controller, "_build", build)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert observed_seed_deadlines == [grant["expires_unix"]]
    assert report["deadline"] == int(report["nova_post_unix"]) + 600
    assert report["deadline"] < report["seed_deadline"]
    assert events.index("tunnel_key_installed") < events.index("nova_post")
    assert controller._remote_json.call_args.args[2]["expires_unix"] == report["deadline"]
    assert events.index("ro_access_active") < events.index("nova_post")


def test_cinder_copy_cannot_consume_the_remaining_nova_window(setup, monkeypatch):
    controller, _, conn, grant, path, _ = launch_fixture(setup, monkeypatch)
    original_build = controller._build

    def build(address, role, run, plan, deadline):
        result = original_build(address, role, run, plan, deadline)
        monkeypatch.setattr(native.time, "time", lambda: grant["expires_unix"] - 100)
        return result

    monkeypatch.setattr(controller, "_build", build)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["outcome"] == "held" and report["exit_code"] == 1
    conn.compute.create_server.assert_not_called()
    assert "native_volumes" in report["retained_resources"]


@pytest.mark.parametrize("method,target", [("native_status", "status"), ("native_cleanup", "cleanup")])
def test_locked_native_recovery_operations_allow_pending_deletes(setup, monkeypatch, method, target):
    _, env, _, _, _ = setup
    env.record("native_ports", "native-held", {"id": PORT, "status": "deleting"})
    implementation = Mock(return_value={"status": "retained"})
    monkeypatch.setattr(native.NativeCloud, target, implementation)
    assert getattr(env, method)() == {"status": "retained"}
    implementation.assert_called_once()


def test_env_down_never_removes_worker_or_share_behind_native_resources(setup):
    _, env, conn, _, _ = setup
    env.record("native_ports", "native-retained", {"id": PORT, "status": "confirmed"})
    with pytest.raises(CloudEnvError, match="native consumers/bootstrap service remain"):
        env.down()
    assert not conn.compute.mock_calls and not conn.shared_file_system.mock_calls


@pytest.mark.parametrize(
    "field,value",
    [("hw_firmware_type", "uefi"), ("architecture", "aarch64"), ("hw_disk_bus", None), ("hw_vif_model", None)],
)
def test_volume_boot_profile_must_match_before_nova_post(setup, monkeypatch, field, value):
    controller, _, conn, _, path, _ = launch_fixture(setup, monkeypatch)
    original_get = conn.block_storage.get_volume.side_effect

    def get_volume(identity):
        volume = original_get(identity)
        volume.volume_image_metadata[field] = value
        return volume

    conn.block_storage.get_volume.side_effect = get_volume
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["outcome"] == "held"
    conn.compute.create_server.assert_not_called()


def test_tunnel_key_is_restricted_to_the_exact_consumer_fixed_ip(setup, monkeypatch):
    controller, env, _, _, _ = setup
    role = "native-key-scope"
    controller.state["runs"][role] = {"plan": {"network": {"address": "192.168.0.20"}}}
    env.record("servers", "worker", {"id": SERVER, "status": "confirmed"})
    local = env.directory / "key-scope"
    local.mkdir(mode=0o700)
    (local / "ssh_private_key.pub").write_text("ssh-ed25519 AAAA per-run\n")
    transport = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr(controller, "_ssh", transport)
    controller._install_tunnel_key("172.30.101.63", role, local, int(time.time()) + 600)
    line = transport.call_args.kwargs["input"].decode()
    assert line.startswith('from="192.168.0.20",restrict,port-forwarding,')
    assert 'permitopen="127.0.0.1:9443"' in line and 'command="/usr/bin/false"' in line
    assert env.resource("native_ssh_keys", role)["line"] == line


def run_key_removal(path, line, prefix=""):
    program = (
        prefix
        + "\n"
        + inspect.getsource(native._remove_authorized_key)
        + "\nimport sys\n_remove_authorized_key(sys.argv[1], sys.stdin.buffer.read())\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", program, str(path)], input=line, capture_output=True, check=False
    )


def test_tunnel_key_removal_executes_complete_worker_program_and_retains_unrelated_bytes(setup, monkeypatch, tmp_path):
    controller, env, _, _, _ = setup
    role = "native-remove"
    line = b"ssh-ed25519 AAAA palimpsest-native-exact"
    original = b"# preserved\r\nssh-ed25519 BBBB other\r\n" + line + b"\n" + line + b"-suffix\nlast-without-newline"
    path = tmp_path / "authorized_keys"
    path.write_bytes(original)
    path.chmod(0o640)
    before = path.stat()
    env.record("servers", "worker", {"id": "worker", "status": "confirmed"})
    env.record("native_ssh_keys", role, {"worker_id": "worker", "line": line.decode(), "status": "confirmed"})

    def local_transport(address, command, *, input):
        argv = shlex.split(command)
        assert argv[:3] == ["python3", "-I", "-c"]
        program = argv[3].replace("'/home/ubuntu/.ssh/authorized_keys'", repr(str(path)))
        result = subprocess.run([sys.executable, "-I", "-c", program], input=input, capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
        return result

    monkeypatch.setattr(controller, "_ssh", local_transport)
    controller._remove_tunnel_key("unused-local-fixture", role)
    assert path.read_bytes() == original.replace(line + b"\n", b"")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert (path.stat().st_uid, path.stat().st_gid) == (before.st_uid, before.st_gid)
    assert env.resource("native_ssh_keys", role) is None


def test_tunnel_key_removal_refuses_over_one_mib_without_changing_file_or_metadata(tmp_path):
    path = tmp_path / "authorized_keys"
    original = b"remove-me\n" + b"x" * (1024 * 1024) + b"\nlast-retained-key\n"
    path.write_bytes(original)
    path.chmod(0o604)
    before = path.stat()
    result = run_key_removal(path, b"remove-me")
    assert result.returncode != 0 and b"complete-read bound" in result.stderr
    assert path.read_bytes() == original
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mode,
        before.st_mtime_ns,
    )
    assert sorted(item.name for item in tmp_path.iterdir()) == ["authorized_keys"]


def test_tunnel_key_removal_handles_short_reads_without_dropping_the_tail(tmp_path):
    path = tmp_path / "authorized_keys"
    original = b"first-key\nremove-me\n" + b"retained-key\n" * 200
    path.write_bytes(original)
    result = run_key_removal(
        path, b"remove-me", "import os\n_read = os.read\nos.read = lambda fd, size: _read(fd, min(size, 7))\n"
    )
    assert result.returncode == 0, result.stderr.decode()
    assert path.read_bytes() == original.replace(b"remove-me\n", b"")


@pytest.mark.parametrize("change", ["premature-eof", "concurrent-append", "path-replacement"])
def test_tunnel_key_removal_refuses_truncated_or_changed_snapshot(tmp_path, change):
    path = tmp_path / "authorized_keys"
    original = b"remove-me\nretained-key\n"
    path.write_bytes(original)
    prefix = (
        "import os,sys\n"
        "_read = os.read\n"
        "_seen = False\n"
        "def changed_read(fd, size):\n"
        "    global _seen\n"
        "    data = _read(fd, size)\n"
    )
    if change == "premature-eof":
        prefix += "    return b''\n"
    elif change == "concurrent-append":
        prefix += (
            "    if not data and not _seen:\n"
            "        _seen = True\n"
            "        with open(sys.argv[1], 'ab') as stream: stream.write(b'concurrent-key\\n')\n"
            "    return data\n"
        )
    else:
        prefix += (
            "    if not data and not _seen:\n"
            "        _seen = True\n"
            "        os.rename(sys.argv[1], sys.argv[1] + '.original')\n"
            "        with open(sys.argv[1], 'wb') as stream: stream.write(b'replacement-key\\n')\n"
            "    return data\n"
        )
    result = run_key_removal(path, b"remove-me", prefix + "os.read = changed_read\n")
    assert result.returncode != 0 and (b"truncated" in result.stderr or b"changed" in result.stderr)
    expected = {
        "premature-eof": original,
        "concurrent-append": original + b"concurrent-key\n",
        "path-replacement": b"replacement-key\n",
    }
    assert path.read_bytes() == expected[change]
    assert not list(tmp_path.glob("*.native-remove-*"))


def test_tunnel_key_removal_staging_collision_never_clobbers_the_existing_file(tmp_path):
    path = tmp_path / "authorized_keys"
    original = b"remove-me\nretained-key\n"
    path.write_bytes(original)
    staging = tmp_path / "authorized_keys.native-remove-collision"
    staging.write_bytes(b"unrelated-staging-content")
    result = run_key_removal(
        path,
        b"remove-me",
        "import uuid\nfrom types import SimpleNamespace\nuuid.uuid4 = lambda: SimpleNamespace(hex='collision')\n",
    )
    assert result.returncode != 0
    assert path.read_bytes() == original
    assert staging.read_bytes() == b"unrelated-staging-content"


def test_tunnel_key_removal_handles_short_staging_writes(tmp_path):
    path = tmp_path / "authorized_keys"
    original = b"remove-me\n" + b"retained-key\n" * 100
    path.write_bytes(original)
    result = run_key_removal(
        path, b"remove-me", "import os\n_write = os.write\nos.write = lambda fd, data: _write(fd, data[:5])\n"
    )
    assert result.returncode == 0, result.stderr.decode()
    assert path.read_bytes() == original.replace(b"remove-me\n", b"")


def source_tree(tmp_path, monkeypatch, name, files):
    from palimpsest_local import cloud_env

    package = tmp_path / name / "palimpsest_local"
    package.mkdir(parents=True, mode=0o755)
    package.chmod(0o755)
    for relative, content in files.items():
        path = package / relative
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)
    with monkeypatch.context() as context:
        context.setattr(cloud_env, "__file__", str(package / "cloud_env.py"))
        data, digest = cloud_env.package_archive()
    return package, data, digest


def test_source_install_preserves_original_bytes_metadata_and_owned_cache_without_import(tmp_path, monkeypatch):
    old = {
        "__init__.py": b"raise AssertionError('predecessor must not be imported')\n",
        "nested/worker.py": b"ORIGINAL = True\n",
        "nested.py": b"PATH_PART_ORDER = True\n",
    }
    package, old_data, old_sha = source_tree(tmp_path, monkeypatch, "installed", old)
    cache = package / "__pycache__"
    cache.mkdir(mode=0o755)
    (cache / "__init__.cpython-312.pyc").write_bytes(b"owned cache is never imported")
    original = (package / "nested/worker.py").stat()
    _, new_data, new_sha = source_tree(tmp_path, monkeypatch, "reviewed", {"__init__.py": b"REVIEWED = True\n"})
    evidence = tmp_path / "private-source-evidence"
    evidence.mkdir(mode=0o700)
    result = native._preserve_and_replace_source(str(package), str(evidence), new_data, old_sha, new_sha)
    assert result["status"] == "activated" and result["installed_sha256"] == new_sha
    prior = evidence / ("source-before-" + old_sha + ".prior")
    assert prior.stat().st_mode & 0o777 == 0o700
    assert (prior / "source.tar").read_bytes() == old_data
    assert (prior / "package/__init__.py").read_bytes() == old["__init__.py"]
    assert (prior / "package/__pycache__/__init__.cpython-312.pyc").read_bytes() == b"owned cache is never imported"
    metadata = json.loads((prior / "metadata.json").read_text())["files"]["nested/worker.py"]
    assert metadata["mtime_ns"] == original.st_mtime_ns and metadata["uid"] == os.geteuid()
    assert metadata["sha256"] == hashlib.sha256(old["nested/worker.py"]).hexdigest()
    assert (package / "__init__.py").read_bytes() == b"REVIEWED = True\n"
    assert (native.Path(result["displaced"]) / "__init__.py").read_bytes() == old["__init__.py"]
    assert native.Path(result["staging"]).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("drift", ["extra", "symlink", "empty-directory", "loose-pyc", "cache-symlink"])
def test_source_install_refuses_unrecorded_predecessor_tree_without_replacement(tmp_path, monkeypatch, drift):
    package, _, old_sha = source_tree(tmp_path, monkeypatch, "installed", {"__init__.py": b"ORIGINAL = True\n"})
    _, new_data, new_sha = source_tree(tmp_path, monkeypatch, "reviewed", {"__init__.py": b"REVIEWED = True\n"})
    if drift == "extra":
        (package / "shadow.so").write_bytes(b"unexpected extension")
    elif drift == "symlink":
        (package / "alias.py").symlink_to(package / "__init__.py")
    elif drift == "empty-directory":
        (package / "shadow").mkdir()
    elif drift == "loose-pyc":
        (package / "shadow.pyc").write_bytes(b"unexpected bytecode")
    else:
        cache = package / "__pycache__"
        cache.mkdir()
        (cache / "shadow.pyc").symlink_to(package / "__init__.py")
    evidence = tmp_path / "private-source-evidence"
    evidence.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="HOLD"):
        native._preserve_and_replace_source(str(package), str(evidence), new_data, old_sha, new_sha)
    assert (package / "__init__.py").read_bytes() == b"ORIGINAL = True\n"
    assert list(evidence.iterdir()) == []


def test_source_install_never_overwrites_historical_prior(tmp_path, monkeypatch):
    package, _, old_sha = source_tree(tmp_path, monkeypatch, "installed", {"__init__.py": b"ORIGINAL = True\n"})
    _, new_data, new_sha = source_tree(tmp_path, monkeypatch, "reviewed", {"__init__.py": b"REVIEWED = True\n"})
    evidence = tmp_path / "private-source-evidence"
    evidence.mkdir(mode=0o700)
    prior = evidence / ("source-before-" + old_sha + ".prior")
    prior.mkdir(mode=0o700)
    (prior / "historical-receipt").write_bytes(b"preserve this predecessor evidence")
    with pytest.raises(FileExistsError):
        native._preserve_and_replace_source(str(package), str(evidence), new_data, old_sha, new_sha)
    assert (prior / "historical-receipt").read_bytes() == b"preserve this predecessor evidence"
    assert (package / "__init__.py").read_bytes() == b"ORIGINAL = True\n"
    assert json.loads((evidence / ("source-install-" + new_sha + ".json")).read_text())["status"] == "held"


def test_source_install_failed_cutover_preserves_backup_and_displaced_source_without_rollback(tmp_path, monkeypatch):
    package, old_data, old_sha = source_tree(tmp_path, monkeypatch, "installed", {"__init__.py": b"ORIGINAL = True\n"})
    _, new_data, new_sha = source_tree(tmp_path, monkeypatch, "reviewed", {"__init__.py": b"REVIEWED = True\n"})
    evidence = tmp_path / "private-source-evidence"
    evidence.mkdir(mode=0o700)
    original_rename = os.rename

    def interrupt_new_activation(source, destination, **kwargs):
        if native.Path(destination) == package:
            raise OSError("injected uncertain cutover")
        return original_rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", interrupt_new_activation)
    with pytest.raises(OSError, match="uncertain cutover"):
        native._preserve_and_replace_source(str(package), str(evidence), new_data, old_sha, new_sha)
    journal = json.loads((evidence / ("source-install-" + new_sha + ".json")).read_text())
    assert journal["status"] == "held" and not package.exists()
    assert (native.Path(journal["displaced"]) / "__init__.py").read_bytes() == b"ORIGINAL = True\n"
    assert (native.Path(journal["prior"]) / "source.tar").read_bytes() == old_data


def test_source_install_unknown_transport_keeps_local_intent_and_refuses_replay(setup, monkeypatch):
    controller, env, _, _, _ = setup
    new_sha = "e" * 64
    monkeypatch.setattr(native, "package_archive", lambda: (b"reviewed source archive", new_sha))
    transport = Mock(
        side_effect=[subprocess.CompletedProcess([], 0, b"", b""), CloudEnvError("remote outcome unknown")]
    )
    monkeypatch.setattr(controller, "_ssh", transport)
    with pytest.raises(CloudEnvError, match="unknown"):
        controller._install_source("172.30.101.63")
    assert env.state["worker_package_sha256"] == PACKAGE
    assert controller.state["preparation"]["source_install"]["status"] == "held"
    with pytest.raises(CloudEnvError, match="never replay"):
        controller._install_source("172.30.101.63")
    assert transport.call_count == 2


def test_certificate_process_hold_retains_handles_and_artifacts_until_owned_child_reaped(setup, tmp_path):
    from palimpsest_local.nova_bootstrap_service import CertificateProcessHold

    controller, _, _, _, _ = setup
    process = Mock(pid=5678)
    process.wait.side_effect = subprocess.TimeoutExpired("openssl", 0)
    error = CertificateProcessHold(process)
    error.staging_directory = tmp_path / "private-certificate-stage"
    error.staging_directory.mkdir(mode=0o700)
    private_key = error.staging_directory / "retained.key"
    private_key.write_bytes(b"private evidence remains")
    private_key.chmod(0o600)
    record = {}
    controller._remember_certificate_hold(error, record)
    with pytest.raises(CloudEnvError, match="unreaped"):
        controller._certificate_terminal(record)
    assert controller.certificate_holds[process.pid] is error
    assert record["certificate_hold"]["status"] == "unreaped" and private_key.exists()
    process.kill.assert_not_called()
    process.terminate.assert_not_called()
    process.wait.side_effect = None
    process.wait.return_value = 0
    controller._certificate_terminal(record)
    assert record["certificate_hold"]["status"] == "reaped" and private_key.exists()


def test_cleanup_cannot_hide_a_persisted_unowned_certificate_hold(setup, monkeypatch):
    controller, _, _, grant, path = setup
    run = {
        **cpu_run(),
        "grant_id": grant["grant_id"],
        "package_sha256": PACKAGE,
        "status": "held",
        "certificate_hold": {"pid": 5678, "status": "unreaped", "staging_directory": "/private/retained"},
    }
    controller.state["runs"][run["run_id"]] = run
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(controller.env, "_revoke", lambda role: None)
    report = controller.cleanup(grant_path=path, run_id=run["run_id"])
    assert report["status"] == "cleanup_failed" and report["exit_code"] == 1
    assert any(item["resource"] == "certificate_process" for item in report["cleanup_errors"])


@pytest.mark.parametrize("origin_matches", [True, False])
def test_source_install_confirms_runtime_import_origin_before_receipt_update(setup, monkeypatch, origin_matches):
    controller, env, _, _, _ = setup
    new_sha = "e" * 64
    package = "/usr/local/lib/python3.12/dist-packages/palimpsest_local"
    origin = package + "/__init__.py" if origin_matches else "/foreign/palimpsest_local.so"
    monkeypatch.setattr(native, "package_archive", lambda: (b"reviewed archive", new_sha))
    activated = {"status": "activated", "installed_sha256": new_sha, "package": package}
    transport = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess([], 0, json.dumps(activated).encode(), b""),
            subprocess.CompletedProcess([], 0 if origin_matches else 44, json.dumps({"origin": origin}).encode(), b""),
        ]
    )
    monkeypatch.setattr(controller, "_ssh", transport)
    if origin_matches:
        controller._install_source("172.30.101.63")
        assert env.state["worker_package_sha256"] == new_sha
        assert controller.state["preparation"]["source_install"]["result"]["import_origin"] == origin
    else:
        with pytest.raises(CloudEnvError, match="interpreter does not select"):
            controller._install_source("172.30.101.63")
        assert env.state["worker_package_sha256"] == PACKAGE
        assert controller.state["preparation"]["source_install"]["status"] == "held"


def prepared_artifact_bindings(controller):
    preparation = controller.state["preparation"]
    preparation["runtime_sha256"] = hashlib.sha256(
        native.Path(native.__file__).with_name("nova_ceph_boot.py").read_bytes()
    ).hexdigest()
    preparation["probe_sha256"] = "e" * 64
    preparation["probe_glibc_max"] = "2.34"
    preparation["probe_path"] = controller.remote + "/cuda-probe"
    preparation["config"]["runtime_script_path"] = controller.remote + "/nova_ceph_boot.py"
    return preparation


@pytest.mark.parametrize("changed", [None, "runtime", "probe"])
def test_prepared_artifacts_are_reverified_without_rewriting(setup, monkeypatch, changed):
    controller, _, _, _, _ = setup
    preparation = prepared_artifact_bindings(controller)
    runtime_sha, probe_sha = preparation["runtime_sha256"], preparation["probe_sha256"]
    hashes = ["0" * 64 if changed == "runtime" else runtime_sha, "0" * 64 if changed == "probe" else probe_sha]
    reader = Mock(side_effect=hashes)
    writer = Mock()
    monkeypatch.setattr(controller, "_remote_sha256", reader)
    monkeypatch.setattr(controller, "_remote_private", writer)
    monkeypatch.setattr(controller, "_remote_probe_glibc", lambda *args: "2.34")
    if changed is None:
        controller._verify_prepared_artifacts("172.30.101.63", preparation)
        assert reader.call_count == 2
    else:
        with pytest.raises(CloudEnvError, match="bytes changed"):
            controller._verify_prepared_artifacts("172.30.101.63", preparation)
    writer.assert_not_called()


@pytest.mark.parametrize("changed", [None, "runtime", "probe", "missing-probe", "manifest-pin"])
def test_carrier_manifest_binds_the_actual_runtime_and_probe_not_only_its_own_digest(setup, changed):
    controller, _, _, _, _ = setup
    preparation = prepared_artifact_bindings(controller)
    files = {
        "bootstrap/nova_ceph_boot.py": {"sha256": preparation["runtime_sha256"]},
        "carrier/probe/cuda-probe": {"sha256": preparation["probe_sha256"]},
    }
    if changed == "runtime":
        files["bootstrap/nova_ceph_boot.py"]["sha256"] = "0" * 64
    elif changed == "probe":
        files["carrier/probe/cuda-probe"]["sha256"] = "0" * 64
    elif changed == "missing-probe":
        files.pop("carrier/probe/cuda-probe")
    manifest = {"schema": "palimpsest.nova-boot-artifacts.v1", "files": files}
    result = {"carrier_digest": native._digest(manifest) if changed != "manifest-pin" else "sha256:" + "0" * 64}
    if changed is None:
        controller._verify_carrier_manifest({"phase": "gpu"}, result, manifest)
    else:
        with pytest.raises(CloudEnvError, match="HOLD"):
            controller._verify_carrier_manifest({"phase": "gpu"}, result, manifest)


def test_cpu_carrier_requires_reviewed_runtime_but_not_a_gpu_probe(setup):
    controller, _, _, _, _ = setup
    preparation = prepared_artifact_bindings(controller)
    manifest = {"files": {"bootstrap/nova_ceph_boot.py": {"sha256": preparation["runtime_sha256"]}}}
    controller._verify_carrier_manifest({"phase": "cpu"}, {"carrier_digest": native._digest(manifest)}, manifest)


def test_service_restart_refuses_changed_runtime_without_rewriting_or_starting(setup, tmp_path, monkeypatch):
    controller, _, _, grant, path = setup
    preparation = prepared_artifact_bindings(controller)
    preparation["service_started"] = False
    probe_source = tmp_path / "probe.c"
    probe_source.write_bytes(b"reviewed probe source")
    grant["probe_source_sha256"] = hashlib.sha256(probe_source.read_bytes()).hexdigest()
    preparation["probe_source_sha256"] = grant["probe_source_sha256"]
    private_json(path, grant)
    config = {
        "install_source": False,
        "start_service": True,
        "bundle_path": "/private/prepared-bundle",
        "runtime_script_path": controller.remote + "/nova_ceph_boot.py",
        "probe_source_file": str(probe_source),
        "dns": ["8.8.8.8", "8.8.4.4"],
    }
    preparation["config"] = {**config, "start_service": False}
    config_path = private_json(tmp_path / "prepare.json", config)
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(controller, "_remote_sha256", Mock(return_value="0" * 64))
    transport, writer = Mock(), Mock()
    monkeypatch.setattr(controller, "_ssh", transport)
    monkeypatch.setattr(controller, "_remote_private", writer)
    with pytest.raises(CloudEnvError, match="bytes changed"):
        controller.prepare(grant_path=path, tag="cuda", config_path=config_path)
    writer.assert_not_called()
    transport.assert_not_called()


@pytest.mark.parametrize(
    "symbols,expected",
    [
        (b"Name: GLIBC_2.2.5 Flags: none\nName: GLIBC_2.34", "2.34"),
        (b"000 UND memcpy@GLIBC_2.14\n000 UND puts@GLIBC_2.35", "2.35"),
    ],
)
def test_probe_abi_accepts_only_versions_supplied_by_original_image(symbols, expected):
    assert native.validate_probe_glibc(symbols) == expected


@pytest.mark.parametrize(
    "symbols",
    [
        b"Name: GLIBC_2.36",
        b"Name: GLIBC_2.39",
        b"Name: GLIBC_2.35.1",
        b"Name: GLIBC_PRIVATE",
        b"Name: GLIBC_ABI_DT_RELR",
        b"",
        b"\xffGLIBC_2.34",
    ],
)
def test_probe_abi_rejects_newer_unknown_or_unverified_requirements(symbols):
    with pytest.raises(CloudEnvError):
        native.validate_probe_glibc(symbols)


def test_prepared_probe_with_newer_abi_is_refused_before_builder(setup, monkeypatch):
    controller, _, _, _, _ = setup
    preparation = prepared_artifact_bindings(controller)
    monkeypatch.setattr(
        controller,
        "_remote_sha256",
        lambda address, path: preparation["runtime_sha256"] if path.endswith(".py") else preparation["probe_sha256"],
    )
    monkeypatch.setattr(
        controller, "_ssh", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, b"Name: GLIBC_2.39", b"")
    )
    writer = Mock()
    monkeypatch.setattr(controller, "_remote_private", writer)
    with pytest.raises(CloudEnvError):
        controller._verify_prepared_artifacts("172.30.101.63", preparation)
    writer.assert_not_called()


def recovered_case(setup):
    controller, env, conn, grant, path = setup
    _, original_ledger = controller._grant(path, "run", "cuda")
    controller._reserve(grant, original_ledger, "cpu", 600)
    run = {
        **cpu_run(),
        "status": "cleaned",
        "outcome": "held",
        "grant_id": grant["grant_id"],
        "nova_post_unix": time.time() - 2000,
        "package_sha256": PACKAGE,
        "enrolled": False,
        "enrollment_intent": False,
        "cleanup_errors": [],
        "budget_exceeded": True,
    }
    controller.state["runs"][run["run_id"]] = run
    for kind, identity in (
        ("native_servers", SERVER),
        ("native_volumes", VOLUME),
        ("native_ports", PORT),
        ("native_images", IMAGE),
    ):
        env.record(kind, run["run_id"], {"id": identity, "status": "deleting"})
        env.forget(kind, run["run_id"])
    from openstack.exceptions import NotFoundException

    for getter in (conn.compute.get_server, conn.block_storage.get_volume, conn.network.get_port, conn.image.get_image):
        getter.side_effect = NotFoundException("absent", http_status=404)
    grant["grant_id"] = "approved-recovery"
    grant["recovery"] = {
        "preparation_sha256": native._digest(controller.state["preparation"]),
        "history_sha256": native._digest({"history": env.state["history"]}),
        "runs": {run["run_id"]: native._digest(run)},
    }
    grant["cleanup_reserve_seconds"] = 180
    private_json(path, grant)
    return controller, env, conn, grant, path, run


def test_exact_recovery_preserves_failed_outcome_and_spent_original_budget(setup):
    controller, _, conn, grant, path, run = recovered_case(setup)
    original_run = copy.deepcopy(run)
    original_ledger = copy.deepcopy(controller.state["grants"]["approved-proof"])
    approved, new_ledger = controller._grant(path, "run", "cuda")
    controller._prior_runs(approved, check_absence=True)
    controller._reserve(approved, new_ledger, "cpu", 600)
    assert run == original_run
    assert controller.state["grants"]["approved-proof"] == original_ledger
    assert new_ledger["used"]["cpu_servers"] == 1
    conn.compute.get_server.assert_called_once_with(SERVER)
    conn.block_storage.get_volume.assert_called_once_with(VOLUME)
    conn.network.get_port.assert_called_once_with(PORT)
    conn.image.get_image.assert_called_once_with(IMAGE)
    conn.compute.create_server.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "same-grant",
        "pending",
        "unknown-run",
        "revocation",
        "upload-client",
        "history",
        "resource",
        "none-response",
        "unavailable",
    ],
)
def test_recovery_cannot_bypass_changed_unknown_or_retained_prior_state(setup, change):
    controller, env, conn, grant, _, run = recovered_case(setup)
    if change == "digest":
        run["error_type"] = "changed after review"
    elif change == "same-grant":
        grant["grant_id"] = run["grant_id"]
    elif change == "pending":
        run["status"] = "cleanup_failed"
    elif change == "unknown-run":
        grant["recovery"]["runs"]["native-unknown"] = "sha256:" + "0" * 64
    elif change == "revocation":
        run["enrollment_intent"] = True
        grant["recovery"]["runs"][run["run_id"]] = native._digest(run)
    elif change == "upload-client":
        run["upload_transport_hold"] = {"pid": 1234, "retained_client": True}
        grant["recovery"]["runs"][run["run_id"]] = native._digest(run)
    elif change == "history":
        env.state["history"] = []
    else:
        conn.compute.get_server.side_effect = (
            None if change != "unavailable" else TimeoutError("private provider detail")
        )
        conn.compute.get_server.return_value = Obj(id=SERVER) if change == "resource" else None
    with pytest.raises((CloudEnvError, TimeoutError)):
        controller._prior_runs(grant, check_absence=True)
    conn.compute.create_server.assert_not_called()
    conn.network.create_port.assert_not_called()


def test_recovery_acknowledgment_is_not_a_cpu_success_or_permission_for_another_failed_run(setup):
    controller, _, conn, grant, path, _ = recovered_case(setup)
    controller.state["preparation"]["recovery"] = grant["recovery"]
    with pytest.raises(CloudEnvError, match="verified native-root CPU"):
        controller.run(grant_path=path, tag="cuda", phase="gpu", timeout=600, command=native.GPU_COMMAND)
    controller.state["runs"]["native-new-failure"] = {
        "status": "cleaned",
        "outcome": "failed",
        "grant_id": grant["grant_id"],
    }
    with pytest.raises(CloudEnvError, match="new exact recovery approval"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    conn.network.create_port.assert_not_called()


@pytest.mark.parametrize("value", [None, {}, {"runs": {}}, {"runs": {}, "preparation_sha256": "sha256:" + "a" * 64}])
def test_recovery_grant_requires_exact_nonempty_bindings(setup, value):
    controller, _, _, grant, path = setup
    grant["recovery"] = value
    private_json(path, grant)
    with pytest.raises(CloudEnvError, match="exact prior preparation"):
        controller._grant(path, "run", "cuda")


def test_cleanup_reserve_is_inside_the_post_nova_budget_not_an_extension(setup, monkeypatch):
    controller, _, _, grant, path, _ = launch_fixture(setup, monkeypatch)
    grant["cleanup_reserve_seconds"] = 180
    private_json(path, grant)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    assert report["allocation_deadline"] == report["nova_post_unix"] + 600
    assert report["deadline"] == int(report["allocation_deadline"]) - 180
    assert controller._remote_json.call_args.args[2]["expires_unix"] == report["deadline"]
    assert report["allocation_seconds"] == 600


@pytest.mark.parametrize("reserve", [True, -1, 1800, "180"])
def test_invalid_cleanup_reserve_refuses_admission(setup, reserve):
    controller, _, conn, grant, path = setup
    grant["cleanup_reserve_seconds"] = reserve
    private_json(path, grant)
    with pytest.raises(CloudEnvError, match="cleanup reserve"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"])
    conn.network.create_port.assert_not_called()


def test_cpu_cleanup_budget_overrun_cannot_open_gpu_gate(setup, monkeypatch):
    controller, env, conn, grant, path = setup
    run = {
        **cpu_run(),
        "status": "success",
        "outcome": "success",
        "grant_id": grant["grant_id"],
        "package_sha256": PACKAGE,
        "nova_post_unix": time.time() - 700,
        "allocation_seconds": 600,
    }
    run["verified_result"] = proof(run)
    controller.state["runs"][run["run_id"]] = run
    env.record("native_servers", run["run_id"], {"id": SERVER, "status": "confirmed"})
    from openstack.exceptions import NotFoundException

    conn.compute.get_server.side_effect = NotFoundException("absent", http_status=404)
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    report = controller.cleanup(grant_path=path, run_id=run["run_id"])
    assert report["status"] == "cleaned" and report["exit_code"] == 1
    assert report["budget_exceeded"] is True and report["outcome"] == "budget_exceeded"
    with pytest.raises(CloudEnvError, match="prior native run failed"):
        controller.run(grant_path=path, tag="cuda", phase="gpu", timeout=600, command=native.GPU_COMMAND)


def recovery_prepare_config(setup, tmp_path, monkeypatch):
    controller, env, conn, grant, path, run = recovered_case(setup)
    prepared = controller.state["preparation"]
    prepared.update(service_started=False, service_start_intent=False)
    prepared["config"]["bundle_path"] = "/private/bundle"
    new_sha = "9" * 64
    source = tmp_path / "probe.c"
    source.write_bytes(b"reviewed probe source")
    grant.update(source_package_sha256=new_sha, probe_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    grant["recovery"]["preparation_sha256"] = native._digest(prepared)
    private_json(path, grant)
    monkeypatch.setattr(native, "package_archive", lambda: (b"new source", new_sha))
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(controller, "_check_time", lambda approved: None)
    transport = Mock(
        return_value=subprocess.CompletedProcess([], 0, b"MainPID=0\nActiveState=inactive\nSubState=dead\n", b"")
    )
    monkeypatch.setattr(controller, "_ssh", transport)
    config = {
        "install_source": True,
        "start_service": True,
        "bundle_path": "/private/bundle",
        "runtime_script_path": controller.base_remote + "/generation-" + new_sha + "/nova_ceph_boot.py",
        "probe_source_file": str(source),
        "dns": prepared["config"]["dns"],
    }
    return controller, env, conn, grant, path, run, config, transport


def test_uncertain_recovery_source_install_retains_prior_preparation_and_blocks_replay(setup, tmp_path, monkeypatch):
    controller, env, _, grant, path, run, config, _ = recovery_prepare_config(setup, tmp_path, monkeypatch)
    previous = copy.deepcopy(controller.state["preparation"])
    old_run = copy.deepcopy(run)
    installer = Mock(side_effect=CloudEnvError("uncertain source activation"))
    monkeypatch.setattr(controller, "_install_source", installer)
    config_path = private_json(tmp_path / "prepare.json", config)
    with pytest.raises(CloudEnvError, match="uncertain source activation"):
        controller.prepare(grant_path=path, tag="cuda", config_path=config_path)
    assert controller.state["preparation_history"][native._digest(previous)] == previous
    assert controller.state["preparation"]["status"] == "held"
    assert (
        controller.state["preparation"]["remote"]
        == controller.base_remote + "/generation-" + grant["source_package_sha256"]
    )
    assert env.state["worker_package_sha256"] == PACKAGE and run == old_run
    with pytest.raises(CloudEnvError, match="source replacement requires"):
        controller.prepare(grant_path=path, tag="cuda", config_path=config_path)
    assert installer.call_count == 1


@pytest.mark.parametrize("change", ["digest", "flag", "live-service", "bundle", "dns"])
def test_recovery_source_install_requires_exact_stopped_preparation(setup, tmp_path, monkeypatch, change):
    controller, _, _, grant, path, _, config, transport = recovery_prepare_config(setup, tmp_path, monkeypatch)
    if change == "digest":
        grant["recovery"]["preparation_sha256"] = "sha256:" + "0" * 64
        private_json(path, grant)
    elif change == "flag":
        controller.state["preparation"]["service_started"] = True
    elif change in ("bundle", "dns"):
        config["bundle_path" if change == "bundle" else "dns"] = (
            "/different/bundle" if change == "bundle" else ["1.1.1.1"]
        )
    else:
        transport.return_value.stdout = b"MainPID=123\nActiveState=active\nSubState=running\n"
    installer = Mock()
    monkeypatch.setattr(controller, "_install_source", installer)
    with pytest.raises(
        CloudEnvError,
        match="source replacement requires|freshly confirmed stopped|exact prepared kernel bundle and DNS",
    ):
        controller.prepare(grant_path=path, tag="cuda", config_path=private_json(tmp_path / "prepare.json", config))
    installer.assert_not_called()
    assert "preparation_history" not in controller.state


def test_recovery_cannot_promote_a_verified_but_over_budget_cpu_result_to_gpu(setup):
    controller, _, conn, grant, path, run = recovered_case(setup)
    run["outcome"] = "budget_exceeded"
    run["verified_result"] = proof(run)
    grant["recovery"]["runs"][run["run_id"]] = native._digest(run)
    controller.state["preparation"]["recovery"] = grant["recovery"]
    private_json(path, grant)
    with pytest.raises(CloudEnvError, match="verified native-root CPU"):
        controller.run(grant_path=path, tag="cuda", phase="gpu", timeout=600, command=native.GPU_COMMAND)
    conn.compute.get_flavor.assert_not_called()
    conn.network.create_port.assert_not_called()


@pytest.mark.parametrize("rule_state", ["absent", "id-present", "identity-present", "other-share"])
def test_recovery_checks_access_rule_absence_on_its_exact_original_share(setup, rule_state):
    controller, env, conn, grant, _, run = recovered_case(setup)
    share_id = "66666666-6666-4666-8666-666666666666"
    env.record("shares", "layers", {"id": share_id, "status": "confirmed"})
    env.record(
        "access_rules", run["run_id"], {"id": "original-rule", "share_id": share_id, "access_to": "original-identity"}
    )
    env.forget("access_rules", run["run_id"])
    if rule_state == "other-share":
        env.record("shares", "layers", {"id": "77777777-7777-4777-8777-777777777777", "status": "confirmed"})
    grant["recovery"]["history_sha256"] = native._digest({"history": env.state["history"]})
    conn.shared_file_system.get_share.return_value = Obj(
        project_id=PROJECT, metadata={"palimpsest_cloud_env": env.name, "palimpsest_env_id": env.state["marker"]}
    )
    conn.shared_file_system.access_rules.return_value = (
        []
        if rule_state in ("absent", "other-share")
        else [
            Obj(
                id="original-rule" if rule_state == "id-present" else "different-rule",
                access_to="different-identity" if rule_state == "id-present" else "original-identity",
            )
        ]
    )
    if rule_state == "absent":
        controller._prior_runs(grant, check_absence=True)
        conn.shared_file_system.access_rules.assert_called_once_with(share_id)
    else:
        with pytest.raises(CloudEnvError, match="access rule|share differs"):
            controller._prior_runs(grant, check_absence=True)
        if rule_state == "other-share":
            conn.shared_file_system.access_rules.assert_not_called()
    conn.network.create_port.assert_not_called()


@pytest.mark.parametrize(
    "expiry_stage", ["before-post", "after-post", "before-put", "source-read", "after-client-exit"]
)
def test_carrier_upload_refuses_expired_deadline_without_replay(setup, monkeypatch, expiry_stage):
    controller, env, conn, grant, _ = setup
    now = time.time()
    clock = [now]
    monkeypatch.setattr(native.time, "time", lambda: clock[0])
    monkeypatch.setattr(controller, "_check_time", lambda approved: None)
    run = {
        "run_id": "native-expired-upload",
        "grant_id": grant["grant_id"],
        "source_digest": SOURCE,
        "deadline": now + 10,
        "qcow2": {"path": "/private/carrier.qcow2", "size": 1, "sha256": hashlib.sha256(b"a").hexdigest()},
    }
    reader, writer = os.pipe()
    os.write(writer, b"a")
    os.close(writer)
    process = Mock(stdout=os.fdopen(reader, "rb"))
    process.poll.return_value = None
    image = Obj(id=IMAGE)

    def create(**kwargs):
        if expiry_stage == "after-post":
            clock[0] = now + 11
        return image

    def popen(*args, **kwargs):
        if expiry_stage == "before-put":
            clock[0] = now + 11
        return process

    def upload(url, *, data, **kwargs):
        if expiry_stage == "source-read":
            clock[0] = now + 11
        while data.read():
            pass
        return Obj(status_code=204)

    def wait(*args, **kwargs):
        if expiry_stage == "after-client-exit":
            clock[0] = now + 11
        return 0

    conn.image.put.side_effect = upload
    process.wait.side_effect = wait
    conn.image.create_image.side_effect = create
    original_timeout = (40, 41)
    conn.image.session.timeout = original_timeout
    conn.image.connect_retries = 2
    conn.image.status_code_retries = 5
    spawn = Mock(side_effect=popen)
    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    if expiry_stage == "before-post":
        clock[0] = now + 11
    try:
        with pytest.raises(CloudEnvError, match="deadline expired"):
            controller._upload_carrier("172.30.101.63", run["run_id"], run, grant)
        assert conn.image.create_image.call_count == (0 if expiry_stage == "before-post" else 1)
        assert conn.image.put.call_count == (1 if expiry_stage in ("source-read", "after-client-exit") else 0)
        conn.image.get_image.assert_not_called()
        assert conn.image.session.timeout is original_timeout
        assert conn.image.connect_retries == 2
        assert conn.image.status_code_retries == 5
        if spawn.called:
            assert process.stdout.closed
            process.terminate.assert_called_once_with()
            assert not process.kill.called
        else:
            assert not process.terminate.called
    finally:
        process.stdout.close()


@pytest.fixture
def glance_upload_loopback(monkeypatch):
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    pytest.importorskip("openstack")
    from keystoneauth1.noauth import NoAuth
    from keystoneauth1.session import Session
    from openstack.connection import Connection
    from urllib3.connection import HTTPConnection

    class ReauthenticatingNoAuth(NoAuth):
        def invalidate(self):
            result["reauths"] += 1
            return True

    # Force actual socket backpressure without buffering a whole test carrier.
    monkeypatch.setattr(
        HTTPConnection,
        "default_socket_options",
        [*HTTPConnection.default_socket_options, (socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)],
    )
    result = {"puts": 0, "received": 0, "terminal_chunk": False, "status": 204}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = json.dumps(
                {"versions": [{"id": "v2.16", "status": "CURRENT", "links": [{"rel": "self", "href": endpoint + "/"}]}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            self.connection.settimeout(3)
            result["puts"] += 1
            result["chunked"] = self.headers.get("Transfer-Encoding") == "chunked"
            result["content_length"] = self.headers.get("Content-Length")
            result["image_size"] = self.headers.get("X-OpenStack-Image-Size")
            time.sleep(0.3)
            digest = hashlib.sha256()
            try:
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    size = int(line.strip(), 16)
                    if size == 0:
                        result["terminal_chunk"] = self.rfile.readline() == b"\r\n"
                        break
                    chunk = self.rfile.read(size)
                    result["received"] += len(chunk)
                    digest.update(chunk)
                    if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                        break
                result["sha256"] = digest.hexdigest()
                if result["terminal_chunk"]:
                    self.send_response(result["status"])
                    if result["status"] == 307:
                        self.send_header("Location", endpoint + "/images/" + IMAGE + "/file")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            finally:
                done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}/v2"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = Session(auth=ReauthenticatingNoAuth(), timeout=0.05)
    connection = Connection(
        session=session,
        image_endpoint_override=endpoint,
        image_api_version="2",
        connect_retries=0,
        status_code_retries=5,
    )
    try:
        yield connection.image, result, done
    finally:
        connection.close()
        session.session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("outcome", ["scoped-success", "deadline", "service-unavailable", "unauthorized", "redirect"])
def test_actual_sdk_carrier_backpressure_and_no_put_replay(
    setup, monkeypatch, tmp_path, glance_upload_loopback, outcome
):
    from keystoneauth1.exceptions.connection import ConnectFailure, UnknownConnectionError
    from openstack.image.v2.image import Image

    controller, env, conn, grant, _ = setup
    proxy, result, done = glance_upload_loopback
    # Eight MiB on disk, generated and hashed one small block at a time.
    carrier = tmp_path / "carrier.qcow2"
    digest = hashlib.sha256()
    block = b"a" * 65536
    with carrier.open("wb") as output:
        for _ in range(128):
            output.write(block)
            digest.update(block)
    run = {
        "run_id": "native-loopback-upload",
        "grant_id": grant["grant_id"],
        "source_digest": SOURCE,
        "deadline": time.time() + 10,
        "qcow2": {"path": str(carrier), "size": carrier.stat().st_size, "sha256": digest.hexdigest()},
    }
    image = Image(id=IMAGE)
    conn.image = proxy
    create = Mock(return_value=image)
    get = Mock(return_value=image)
    monkeypatch.setattr(proxy, "create_image", create)
    monkeypatch.setattr(proxy, "get_image", get)
    monkeypatch.setattr(controller, "_check_image", lambda *args: None)
    image.status = "active"
    monkeypatch.setattr(controller, "_check_time", lambda approved: None)
    process = Mock(stdout=carrier.open("rb"))
    process.poll.return_value = None
    process.wait.return_value = 0
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=process))
    read_sizes = []
    original_read = native._HashingStream.read

    def bounded_read(stream, size=-1):
        chunk = original_read(stream, size)
        read_sizes.append(len(chunk))
        return chunk

    monkeypatch.setattr(native._HashingStream, "read", bounded_read)
    original_timeout = proxy.session.timeout
    original_retries = (proxy.connect_retries, proxy.status_code_retries)
    result["reauths"] = 0
    result["status"] = {"service-unavailable": 503, "unauthorized": 401, "redirect": 307}.get(outcome, 204)
    if outcome == "deadline":
        run["deadline"] = grant["expires_unix"] = time.time() + 0.15
    if outcome == "scoped-success":
        assert controller._upload_carrier("172.30.101.63", run["run_id"], run, grant) is image
        get.assert_called_once_with(IMAGE)
    else:
        expected = (
            (ConnectFailure, CloudEnvError)
            if outcome == "deadline"
            else (CloudEnvError, UnknownConnectionError)
            if outcome == "redirect"
            else CloudEnvError
        )
        with pytest.raises(expected):
            controller._upload_carrier("172.30.101.63", run["run_id"], run, grant)
        get.assert_not_called()
        details = run["upload_failure"]
        assert details["remote_outcome"] == "unconfirmed"
        assert 0 <= details["source_bytes_read"] <= details["expected_bytes"] == run["qcow2"]["size"]
        assert details["causes"] and all(set(value) == {"type", "errno"} for value in details["causes"])
    create.assert_called_once()
    assert env.resource("native_images", run["run_id"])["id"] == IMAGE
    assert process.stdout.closed
    process.terminate.assert_called_once_with()
    assert not process.kill.called
    assert done.wait(5)
    assert result["puts"] == 1
    assert result["chunked"] and result["content_length"] is None
    assert result["image_size"] == str(run["qcow2"]["size"])
    assert result["reauths"] == 0
    assert read_sizes and 0 < max(read_sizes) <= 1024 * 1024
    assert max(read_sizes) < run["qcow2"]["size"]
    if outcome != "deadline":
        assert result["terminal_chunk"]
        assert result["received"] == run["qcow2"]["size"]
        assert result["sha256"] == run["qcow2"]["sha256"]
    else:
        assert not result["terminal_chunk"]
        assert result["received"] < run["qcow2"]["size"]
    assert proxy.session.timeout is original_timeout
    assert (proxy.connect_retries, proxy.status_code_retries) == original_retries


@pytest.mark.parametrize("kind", ["native_ports", "native_servers", "native_volumes"])
def test_uncertain_native_delete_is_never_reissued_during_cleanup(setup, monkeypatch, kind):
    controller, env, conn, grant, path = setup
    errors = pytest.importorskip("openstack.exceptions")
    run = {
        **cpu_run(),
        "grant_id": grant["grant_id"],
        "package_sha256": PACKAGE,
        "status": "held",
        "outcome": "held",
        "nonce": "fixture-nonce",
        "boot_gib": grant["boot_gib"],
    }
    controller.state["runs"][run["run_id"]] = run
    monkeypatch.setattr(controller, "_worker", lambda: "172.30.101.63")
    monkeypatch.setattr(controller, "_check_port", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_bind_server", lambda *args, **kwargs: run["plan"])
    identity = {"native_servers": SERVER, "native_ports": PORT, "native_volumes": VOLUME}[kind]
    resource = Obj(
        id=identity,
        project_id=PROJECT,
        metadata=controller._metadata(run["run_id"], run),
        device_id="",
        status="available",
        attachments=[],
        size=grant["boot_gib"],
        volume_image_metadata={"image_id": IMAGE},
    )
    getter, deleter = {
        "native_servers": (conn.compute.get_server, conn.compute.delete_server),
        "native_ports": (conn.network.get_port, conn.network.delete_port),
        "native_volumes": (conn.block_storage.get_volume, conn.block_storage.delete_volume),
    }[kind]
    env.record(kind, run["run_id"], {"id": identity, "status": "confirmed", "image_id": IMAGE})
    getter.return_value = resource
    deleter.side_effect = TimeoutError("uncertain remote deletion")
    first = controller.cleanup(grant_path=path, run_id=run["run_id"])
    assert first["status"] == "cleanup_failed"
    assert env.resource(kind, run["run_id"])["status"] == "deleting"
    if kind == "native_volumes":
        resource.status = "deleting"
    getter.side_effect = [resource, errors.NotFoundException("absent", http_status=404)]
    second = controller.cleanup(grant_path=path, run_id=run["run_id"])
    assert second["status"] == "cleaned" and second["outcome"] == "held"
    assert env.resource(kind, run["run_id"]) is None
    assert deleter.call_count == 1


def test_native_keep_requires_explicit_grant_before_any_provider_read(setup):
    controller, _, conn, _, path = setup
    with pytest.raises(CloudEnvError, match="explicit operator grant"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"], keep=True)
    assert not conn.compute.mock_calls and not conn.network.mock_calls


def test_successful_kept_run_retains_real_allocation_and_blocks_next_consumer(setup, monkeypatch):
    controller, env, conn, grant, path, events = launch_fixture(setup, monkeypatch)
    grant["allow_retained"] = True
    private_json(path, grant)
    report = controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"], keep=True)
    assert report["status"] == "retained" and report["outcome"] == "success" and report["keep"] is True
    assert events.count("nova_post") == 1
    assert env.resource("native_servers", report["run_id"])["id"] == SERVER
    controller.cleanup.assert_not_called()
    with pytest.raises(CloudEnvError, match="prior native run"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=600, command=["/bin/true"], keep=True)
    assert conn.compute.create_server.call_count == 1


@pytest.mark.parametrize("project_scope", [False, True])
def test_published_local_native_image_uses_hub_projection_without_receipt_adoption(setup, monkeypatch, project_scope):
    from palimpsest_local.image_manifest import manifest_digest, native_image_projection, normalize_manifest

    controller, env, _, grant, path, _ = launch_fixture(setup, monkeypatch)
    manifest = normalize_manifest(
        {
            "schema": "palimpsest.image-manifest.v1",
            "mode": "oci-root",
            "platform": "linux/amd64",
            "os_family": "ubuntu",
            "os_version": "22.04",
            "source": {"kind": "local"},
            "runtime": {"entrypoint": [], "cmd": [], "env": [], "working_dir": "/", "user": "0"},
            "layers": [{"digest": "sha256:" + "2" * 64, "size_bytes": 100}],
        }
    )
    grant["source_digest"] = manifest_digest(manifest)
    if project_scope:
        project_image_grant(grant)
    else:
        grant["allow_path_scoped_ceph"] = True
    storage = {
        "kind": "manila-namespace-v1",
        "export": "10.0.0.1:6789:/share/cas/public",
        "fs_name": "cephfs",
        "data_pool": "cephfs_data",
        "namespace": "pal-test-public",
    }
    env._hub_published_image = {
        "project_id": PROJECT,
        "image_id": IMAGE,
        "manifest_digest": manifest_digest(manifest),
        "storage_binding": storage,
    }
    monkeypatch.setattr(env, "native_authorize_image", Mock(), raising=False)
    monkeypatch.setattr(
        env,
        "native_ceph_access",
        Mock(
            return_value={
                "export": storage["export"],
                "name": "synthetic-client",
                "secret": "synthetic-secret",
                "fs_name": storage["fs_name"],
            }
        ),
        raising=False,
    )
    controller.state["preparation"]["source_digest"] = grant["source_digest"]
    private_json(path, grant)
    original_receipts = copy.deepcopy(env.state["images"])

    def observed(address, run):
        result = proof(run)
        result["command"]["runtime_user"] = "0"
        return result

    monkeypatch.setattr(controller, "_read_result", observed)
    report = controller.run(
        grant_path=path, image=native_image_projection(manifest), phase="cpu", timeout=600, command=["/bin/true"]
    )
    assert report["outcome"] == "success"
    assert env.state["images"] == original_receipts
    run = controller.state["runs"][report["run_id"]]
    assert run["plan"]["image"]["source"]["manifest"] == manifest
    assert "root_document" not in run["plan"]["image"]["source"]
    assert env.native_authorize_image.call_count == 2
    env.native_ceph_access.assert_called_once_with(run)
    assert controller._remote_json.call_args.args[2]["ceph_access"]["name"] == "synthetic-client"
    assert run["source_digest"] == manifest_digest(manifest)
    assert run["plan"]["storage"] == {key: storage[key] for key in ("export", "fs_name", "data_pool", "namespace")}
    assert run.get("source_project_id") == (PROJECT if project_scope else None)


def test_published_native_result_honors_nonroot_numeric_identity():
    run = cpu_run()
    run["plan"]["image"]["source"] = {"kind": "published"}
    run["plan"]["image"]["runtime"]["user"] = "1000:1001"
    result = proof(run)
    result["command"].update(uid=1000, gid=1001, runtime_user="1000:1001")
    assert native.NativeCloud._successful(run, result)
    result["command"]["uid"] = 0
    assert not native.NativeCloud._successful(run, result)


def delivery_image(metadata=None):
    from palimpsest_local.image_manifest import native_image_projection, normalize_manifest

    return native_image_projection(
        normalize_manifest(
            {
                "schema": "palimpsest.image-manifest.v1",
                "mode": "oci-root",
                "platform": "linux/amd64",
                "os_family": "ubuntu",
                "os_version": "22.04",
                "source": {"kind": "local"},
                "runtime": {"entrypoint": [], "cmd": [], "env": ["BOUND=exact"], "working_dir": "/", "user": "0"},
                "metadata": metadata or {},
                "layers": [{"digest": "sha256:" + "2" * 64, "size_bytes": 100}],
            }
        )
    )


@pytest.mark.parametrize(
    "case", ["enrollment-bytes", "escaped-bytes", "metadata-key", "descriptor-key", "descriptor-string", "runtime-user"]
)
def test_native_delivery_rejects_unconsumable_projection_before_grant_or_provider_effects(setup, case):
    controller, env, conn, _, path = setup
    if case in {"descriptor-key", "descriptor-string"}:
        selected = copy.deepcopy(env.state["images"]["cuda"])
        selected["source"]["root_descriptor"]["annotations"] = (
            {"k" * 129: "legal OCI annotation"} if case == "descriptor-key" else {"annotation": "x" * 65537}
        )
        env.state["images"]["cuda"] = selected
        kwargs = {"tag": "cuda"}
    else:
        metadata = {
            "enrollment-bytes": {str(index): "a" * 16384 for index in range(16)},
            "escaped-bytes": {str(index): "\uffff" * 5000 for index in range(8)},
            "metadata-key": {"k" * 129: "legal publication metadata"},
            "runtime-user": {},
        }[case]
        selected = delivery_image(metadata)
        if case == "runtime-user":
            manifest = selected["source"]["manifest"]
            manifest["runtime"]["user"] = "1" * 257
            from palimpsest_local.image_manifest import native_image_projection

            selected = native_image_projection(manifest)
        kwargs = {"image": selected}
    before = copy.deepcopy(env.state)
    controller._save = Mock(side_effect=AssertionError("preflight must precede grant journal effects"))
    with pytest.raises(CloudEnvError, match="admission bounds"):
        controller.run(grant_path=path, phase="cpu", timeout=120, command=["/bin/true"], **kwargs)
    assert env.state == before
    controller._save.assert_not_called()
    conn.network.create_port.assert_not_called()
    conn.image.create_image.assert_not_called()
    conn.block_storage.create_volume.assert_not_called()
    conn.compute.create_server.assert_not_called()


def test_native_delivery_keeps_complete_metadata_under_actual_consumer_bounds():
    from palimpsest_local import nova_bootstrap_service as service

    image = delivery_image({"description": "bound canonical metadata", "details": ["a", "b"]})
    before = copy.deepcopy(image)
    native.native_delivery_preflight(image, ["/bin/true"], "cpu", PROJECT)
    assert image == before
    plan = native._native_plan(
        image,
        ["/bin/true"],
        "cpu",
        PROJECT,
        "native-" + "1" * 32,
        {"address": "10.0.0.2", "cidr": "10.0.0.0/24", "gateway": "10.0.0.1", "dns": ["8.8.8.8"]},
    )
    service.validate_plan(plan, plan["run_id"])
    assert plan["image"]["source"]["manifest"]["metadata"] == before["source"]["manifest"]["metadata"]


def test_private_journal_is_not_capped_by_grant_configuration_size(tmp_path):
    state = {"history": [{"result": "a" * 600000}, {"result": "b" * 600000}], "retained": {"id": SERVER}}
    path = private_json(tmp_path / "state.json", state)
    assert native._private_state_json(path) == state
    with pytest.raises(CloudEnvError, match="owned private"):
        native._private_json(path)
    path.chmod(0o644)
    with pytest.raises(CloudEnvError, match="owned private"):
        native._private_state_json(path)


@pytest.mark.parametrize("change", ["append", "replace"])
def test_private_journal_rejects_changed_snapshot_without_truncating_history(tmp_path, monkeypatch, change):
    path = private_json(tmp_path / "state.json", {"history": [{"result": "a" * 600000}] * 2})
    original_load = native.json.load

    def changed(stream, **kwargs):
        result = original_load(stream, **kwargs)
        if change == "append":
            with path.open("ab") as output:
                output.write(b"\n")
        else:
            replacement = private_json(tmp_path / "replacement.json", result)
            os.replace(replacement, path)
        return result

    monkeypatch.setattr(native.json, "load", changed)
    with pytest.raises(CloudEnvError, match="changed while reading; HOLD"):
        native._private_state_json(path)
    assert path.stat().st_size > 1024 * 1024


def test_nine_prepared_resolvers_rejected_before_grant_or_provider_effects(setup):
    controller, env, conn, _, path = setup
    controller.state["preparation"]["config"]["dns"] = [f"10.0.0.{index}" for index in range(1, 10)]
    before = copy.deepcopy(env.state)
    controller._save = Mock(side_effect=AssertionError("DNS admission must precede grant effects"))
    with pytest.raises(CloudEnvError, match="admission bounds"):
        controller.run(grant_path=path, tag="cuda", phase="cpu", timeout=120, command=["/bin/true"])
    assert env.state == before
    controller._save.assert_not_called()
    conn.network.create_port.assert_not_called()
    conn.image.create_image.assert_not_called()
    conn.block_storage.create_volume.assert_not_called()
    conn.compute.create_server.assert_not_called()


def test_native_delivery_accepts_eight_actual_prepared_resolvers():
    native.native_delivery_preflight(
        delivery_image(), ["/bin/true"], "cpu", PROJECT, dns=[f"10.0.0.{index}" for index in range(1, 9)]
    )


def project_image_grant(grant):
    grant["source_project_id"] = PROJECT
    grant["source_digest"] = None
    grant["limits"]["gpu_servers"] = 0
    grant["flavors"]["gpu"] = None
    grant["max_gpu_seconds"] = 0
    grant["allow_path_scoped_ceph"] = True
    return grant


def test_project_images_share_one_original_cumulative_ledger_after_cleaned_cases(setup, monkeypatch):
    controller, env, _, grant, path = setup
    project_image_grant(grant)
    grant["limits"]["cpu_servers"] = 2
    private_json(path, grant)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(env, "native_ceph_access", Mock(), raising=False)
    digests = []
    for index in range(2):
        image = delivery_image({"case": str(index)})
        digest = image["source"]["manifest_digest"]
        digests.append(digest)
        env._hub_published_image = {"project_id": PROJECT, "image_id": str(index), "manifest_digest": digest}
        admitted, ledger = controller._grant(path, "run", image=image, phase="cpu")
        controller._prior_runs(admitted)
        controller._reserve(admitted, ledger, "cpu", 120)
        role = "native-" + str(index) * 32
        controller.state["runs"][role] = {
            "run_id": role,
            "status": "cleaned",
            "outcome": "success",
            "phase": "cpu",
            "grant_id": grant["grant_id"],
            "source_project_id": PROJECT,
            "source_digest": digest,
            "package_sha256": PACKAGE,
        }
        controller._save()
        # Reopen from the persisted environment between image selections.
        reopened = CloudEnv("test", root=env.directory.parent)
        reopened._connection = env.conn
        reopened._hub_published_image = env._hub_published_image
        monkeypatch.setattr(reopened, "native_ceph_access", Mock(), raising=False)
        env = reopened
        controller = native.NativeCloud(env)
    assert digests[0] != digests[1]
    assert path.read_bytes() == original_bytes
    assert len(controller.state["grants"]) == 1
    assert controller.state["grants"][grant["grant_id"]]["used"]["cpu_servers"] == 2
    admitted, ledger = controller._grant(path, "run", image=image, phase="cpu")
    with pytest.raises(CloudEnvError, match="cumulative resource budget"):
        controller._reserve(admitted, ledger, "cpu", 120)
    assert ledger["used"]["cpu_servers"] == 2
    for run in controller.state["runs"].values():
        controller._cleanup_boundary(admitted, path, run)


@pytest.mark.parametrize(
    "case",
    [
        "foreign-project",
        "digest-not-null",
        "gpu-limit",
        "gpu-flavor",
        "gpu-seconds",
        "gpu-request",
        "no-authority",
        "foreign-authority",
        "wrong-manifest",
        "no-storage-approval",
        "no-issuer",
    ],
)
def test_project_image_authority_rejects_before_ledger_or_provider_effects(setup, monkeypatch, case):
    controller, env, conn, grant, path = setup
    project_image_grant(grant)
    image = delivery_image()
    env._hub_published_image = {
        "project_id": PROJECT,
        "image_id": IMAGE,
        "manifest_digest": image["source"]["manifest_digest"],
    }
    monkeypatch.setattr(env, "native_ceph_access", Mock(), raising=False)
    phase = "cpu"
    if case == "foreign-project":
        grant["source_project_id"] = "f" * 32
    elif case == "digest-not-null":
        grant["source_digest"] = SOURCE
    elif case == "gpu-limit":
        grant["limits"]["gpu_servers"] = 1
    elif case == "gpu-flavor":
        grant["flavors"]["gpu"] = GPU_FLAVOR
    elif case == "gpu-seconds":
        grant["max_gpu_seconds"] = 1
    elif case == "gpu-request":
        phase = "gpu"
    elif case == "no-authority":
        del env._hub_published_image
    elif case == "foreign-authority":
        env._hub_published_image["project_id"] = "f" * 32
    elif case == "wrong-manifest":
        env._hub_published_image["manifest_digest"] = SOURCE
    elif case == "no-storage-approval":
        del grant["allow_path_scoped_ceph"]
    elif case == "no-issuer":
        monkeypatch.setattr(env, "native_ceph_access", None)
    private_json(path, grant)
    controller._save = Mock(side_effect=AssertionError("invalid scope must precede ledger writes"))
    with pytest.raises(CloudEnvError):
        controller._grant(path, "run", image=image, phase=phase)
    assert controller.state["grants"] == {}
    controller._save.assert_not_called()
    conn.compute.create_server.assert_not_called()
    conn.shared_file_system.allow_access.assert_not_called()


def test_project_preparation_accepts_no_image_without_inventing_source_digest(setup):
    controller, _, _, grant, path = setup
    project_image_grant(grant)
    private_json(path, grant)
    admitted, ledger = controller._grant(path, "prepare")
    assert admitted["source_digest"] is None
    assert admitted == grant
    assert not any(ledger["used"].values())


def test_exact_registry_grant_retains_original_digest_semantics(setup):
    controller, _, _, grant, path = setup
    admitted, _ = controller._grant(path, "run", tag="cuda", phase="cpu")
    assert admitted == grant and "source_project_id" not in admitted
    assert controller._run_source_matches(admitted, {"source_digest": SOURCE})
    assert not controller._run_source_matches(admitted, {"source_digest": SOURCE, "source_project_id": PROJECT})


def test_namespace_diagnostics_redact_memory_key_without_legacy_secret_file(setup, monkeypatch):
    controller, env, _, _, _ = setup
    run = {"run_id": "native-memory-redaction", "nonce": "run-nonce", "carrier_build_unit": "synthetic.service"}
    controller.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = "AQBBopaqueCEPHclientvalue=="
    controller._native_ceph_secrets[run["run_id"]] = secret
    env.record(
        "access_rules",
        run["run_id"],
        {
            "id": "client.synthetic",
            "provider": "ceph-namespace-v1",
            "type": "ceph-path",
            "access_to": "synthetic",
        },
    )
    monkeypatch.setattr(env, "_secret_path", Mock(side_effect=AssertionError("namespace keys have no legacy file")))
    capture = controller._capture_build_failure(
        run,
        subprocess.CompletedProcess(["synthetic"], 1, stdout=secret.encode(), stderr=b"failed"),
        {"ssh_private_key": b"synthetic ssh", "client_key": b"synthetic client"},
    )
    assert capture["status"] == "captured"
    payload = Path(capture["path"]).read_text()
    assert secret not in payload and "[REDACTED]" in payload
    assert secret not in json.dumps(env.state)
    env._secret_path.assert_not_called()
    controller._native_ceph_secrets.clear()
    monkeypatch.setattr(env, "native_ceph_access", Mock(return_value={"secret": secret}), raising=False)
    assert controller._diagnostic_access_secret(run, env.resource("access_rules", run["run_id"])) == secret
    env.native_ceph_access.assert_called_once_with(run)


def test_namespace_recovery_dispatches_exact_record_to_read_only_absence_check(setup, monkeypatch):
    controller, env, conn, grant, _, run = recovered_case(setup)
    record = {
        "id": "client.pal-" + env.state["marker"] + "-" + run["run_id"],
        "provider": "ceph-namespace-v1",
        "type": "ceph-path",
        "run_role": run["run_id"],
        "marker": env.state["marker"],
        "project_id": PROJECT,
        "binding": {"kind": "manila-namespace-v1"},
        "binding_digest": "sealed-binding",
        "caps": {"osd": "allow r pool=data namespace=exact"},
    }
    env.record("access_rules", run["run_id"], record)
    env.forget("access_rules", run["run_id"])
    grant["recovery"]["history_sha256"] = native._digest({"history": env.state["history"]})
    absence = Mock()
    monkeypatch.setattr(env, "native_ceph_absent", absence, raising=False)
    controller._prior_runs(grant, check_absence=True)
    absence.assert_called_once_with(record)
    conn.shared_file_system.access_rules.assert_not_called()
