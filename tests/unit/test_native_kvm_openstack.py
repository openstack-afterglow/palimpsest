"""Behavioral ownership, timeout, signal, and isolation tests for scripts/run_native_kvm_openstack.py."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "run_native_kvm_openstack",
    ROOT / "scripts" / "run_native_kvm_openstack.py",
)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)

CI_PROJECT_ID = "53ec2dd9a1f7471fb6a2b174595fa232"
FOREIGN_PROJECT_ID = "99999999-8888-7777-6666-555555555555"
ADMIN_PROJECT_ID = "e4f14870-23f0-47b5-9901-f28a91876e7b"
CI_IMAGE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CI_USER_ID = "ci-provisioner-user-1"
FOREIGN_USER_ID = "other-operator-user-2"
COMMIT_SHA = "cf541223cd2a43fa4125652554ce18b8dffa83d1"
PUBLIC_EGRESS_IP = "1.1.1.1"  # Valid public address; the fake cloud never opens network connections.
VM_PORT_IP = "117.16.137.88"

_RAW_ED25519_HOST_KEY = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + (b"\x42" * 32)
HOST_KEY_B64 = base64.b64encode(_RAW_ED25519_HOST_KEY).decode("ascii")
HOST_KEY_FINGERPRINT = "SHA256:" + base64.b64encode(hashlib.sha256(_RAW_ED25519_HOST_KEY).digest()).decode(
    "ascii"
).rstrip("=")

_OTHER_RAW_ED25519_KEY = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00 " + (b"\x99" * 32)
OTHER_HOST_KEY_B64 = base64.b64encode(_OTHER_RAW_ED25519_KEY).decode("ascii")


class NotFoundException(Exception):
    status_code = 404


@dataclass
class FakeImage:
    id: str
    status: str = "active"
    hash_algo: str = "sha512"
    hash_value: str = helper.APPROVED_IMAGE_SHA512
    size: int = 3_758_096_384
    virtual_size: int = 3_758_096_384
    owner_id: str = CI_PROJECT_ID
    visibility: str = "private"


@dataclass
class FakeFlavor:
    id: str = helper.APPROVED_FLAVOR_ID
    name: str = helper.APPROVED_FLAVOR_NAME
    vcpus: int = helper.APPROVED_FLAVOR_VCPUS
    ram: int = helper.APPROVED_FLAVOR_RAM_MIB


@dataclass
class FakeNetworkModel:
    id: str = helper.APPROVED_NETWORK_ID
    status: str = "ACTIVE"


@dataclass
class FakeKeypair:
    name: str
    public_key: str
    user_id: str


@dataclass
class FakeSecurityGroup:
    id: str
    name: str
    project_id: str
    description: str
    tags: list[str] = field(default_factory=list)
    rules: list[dict[str, object]] = field(default_factory=list)


@dataclass
class FakePort:
    id: str
    name: str
    network_id: str
    project_id: str
    security_group_ids: list[str]
    description: str
    fixed_ips: list[dict[str, str]]
    device_id: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class FakeVolume:
    id: str
    name: str
    project_id: str
    size: int
    image_id: str
    volume_type: str
    metadata: dict[str, str]
    description: str
    status: str = "available"
    attachments: list[dict[str, str]] = field(default_factory=list)


@dataclass
class FakeServer:
    id: str
    name: str
    project_id: str
    flavor_id: str
    key_name: str
    port_id: str
    volume_id: str
    metadata: dict[str, str]
    status: str = "ACTIVE"


class FakeOpenStackCloud:
    """Stateful in-memory OpenStack cloud model for ownership and lifecycle regression tests."""

    def __init__(
        self,
        *,
        current_project_id: str = CI_PROJECT_ID,
        current_user_id: str = CI_USER_ID,
        console_output: str | None = None,
    ) -> None:
        self.current_project_id = current_project_id
        self.current_user_id = current_user_id
        self.console_output = (
            console_output
            if console_output is not None
            else (
                "-----BEGIN SSH HOST KEY FINGERPRINTS-----\n"
                f"256 {HOST_KEY_FINGERPRINT} root@ubuntu (ED25519)\n"
                "-----END SSH HOST KEY FINGERPRINTS-----\n"
                "-----BEGIN SSH HOST KEY KEYS-----\n"
                f"ssh-ed25519 {HOST_KEY_B64} root@ubuntu\n"
                "-----END SSH HOST KEY KEYS-----\n"
            )
        )
        self._counter = 0
        self.images: dict[str, FakeImage] = {CI_IMAGE_ID: FakeImage(id=CI_IMAGE_ID)}
        self.flavors: dict[str, FakeFlavor] = {helper.APPROVED_FLAVOR_ID: FakeFlavor()}
        self.networks: dict[str, FakeNetworkModel] = {helper.APPROVED_NETWORK_ID: FakeNetworkModel()}
        self.keypairs: dict[str, FakeKeypair] = {}
        self.security_groups: dict[str, FakeSecurityGroup] = {}
        self.ports: dict[str, FakePort] = {}
        self.volumes: dict[str, FakeVolume] = {}
        self.servers: dict[str, FakeServer] = {}

        self.fail_create_server: Exception | None = None
        self.fail_create_volume: Exception | None = None
        self.fail_delete_server: Exception | None = None
        self.fail_delete_volume: Exception | None = None
        self.retain_server_on_delete = False
        self.server_never_active = False
        self.on_after_create_server: Any = None
        self.volumev3_catalog_endpoint = f"https://cinder.dmslab.re.kr/v3/{current_project_id.replace('-', '')}"

        self.session = self._SessionAPI(self)
        self.image = self._ImageAPI(self)
        self.compute = self._ComputeAPI(self)
        self.network = self._NetworkAPI(self)
        self.block_storage = self._BlockStorageAPI(self)

    class _SessionAPI:
        def __init__(self, cloud: FakeOpenStackCloud) -> None:
            self._cloud = cloud

        def get_endpoint(self, *, service_type: str, interface: str, region_name: str) -> str:
            assert service_type == "volumev3"
            assert interface == "public"
            assert region_name == "RegionOne"
            return self._cloud.volumev3_catalog_endpoint

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    class _ImageAPI:
        def __init__(self, cloud: FakeOpenStackCloud) -> None:
            self._cloud = cloud

        def get_image(self, image_id: str) -> FakeImage:
            if image_id not in self._cloud.images:
                raise NotFoundException(f"image {image_id} not found")
            return self._cloud.images[image_id]

    class _ComputeAPI:
        def __init__(self, cloud: FakeOpenStackCloud) -> None:
            self._cloud = cloud

        def get_flavor(self, flavor_id: str) -> FakeFlavor:
            if flavor_id not in self._cloud.flavors:
                raise NotFoundException(f"flavor {flavor_id} not found")
            return self._cloud.flavors[flavor_id]

        def create_keypair(self, *, name: str, public_key: str) -> FakeKeypair:
            kp = FakeKeypair(name=name, public_key=public_key, user_id=self._cloud.current_user_id)
            self._cloud.keypairs[name] = kp
            return kp

        def get_keypair(self, name: str) -> FakeKeypair:
            if name not in self._cloud.keypairs:
                raise NotFoundException(f"keypair {name} not found")
            return self._cloud.keypairs[name]

        def delete_keypair(self, name: str, *, ignore_missing: bool = False) -> None:
            if name not in self._cloud.keypairs:
                if ignore_missing:
                    return
                raise NotFoundException(f"keypair {name} not found")
            del self._cloud.keypairs[name]

        def create_server(
            self,
            *,
            name: str,
            flavor_id: str,
            key_name: str,
            networks: list[dict[str, str]],
            block_device_mapping_v2: list[dict[str, object]],
            metadata: dict[str, str],
            config_drive: bool = True,
        ) -> FakeServer:
            assert config_drive is True
            if self._cloud.fail_create_server is not None:
                raise self._cloud.fail_create_server
            port_id = networks[0]["port"]
            volume_id = str(block_device_mapping_v2[0]["uuid"])
            server_id = self._cloud._next_id("srv")
            status = "BUILD" if self._cloud.server_never_active else "ACTIVE"
            server = FakeServer(
                id=server_id,
                name=name,
                project_id=self._cloud.current_project_id,
                flavor_id=flavor_id,
                key_name=key_name,
                port_id=port_id,
                volume_id=volume_id,
                metadata=dict(metadata),
                status=status,
            )
            self._cloud.servers[server_id] = server
            if port_id in self._cloud.ports:
                self._cloud.ports[port_id].device_id = server_id
            if volume_id in self._cloud.volumes:
                self._cloud.volumes[volume_id].status = "in-use"
                self._cloud.volumes[volume_id].attachments = [{"server_id": server_id}]
            if callable(self._cloud.on_after_create_server):
                self._cloud.on_after_create_server(server)
            return server

        def get_server(self, server_id: str) -> FakeServer:
            if server_id not in self._cloud.servers:
                raise NotFoundException(f"server {server_id} not found")
            return self._cloud.servers[server_id]

        def servers(self, *, details: bool = True) -> list[FakeServer]:
            assert details is True
            return list(self._cloud.servers.values())

        def delete_server(self, server_id: str, *, ignore_missing: bool = False) -> None:
            if self._cloud.fail_delete_server is not None:
                raise self._cloud.fail_delete_server
            if server_id not in self._cloud.servers:
                if ignore_missing:
                    return
                raise NotFoundException(f"server {server_id} not found")
            if self._cloud.retain_server_on_delete:
                return
            server = self._cloud.servers.pop(server_id)
            if server.port_id in self._cloud.ports:
                self._cloud.ports[server.port_id].device_id = ""
            if server.volume_id in self._cloud.volumes:
                self._cloud.volumes[server.volume_id].status = "available"
                self._cloud.volumes[server.volume_id].attachments = []

        def get_server_console_output(self, server_id: str) -> dict[str, str]:
            if server_id not in self._cloud.servers:
                raise NotFoundException(f"server {server_id} not found")
            return {"output": self._cloud.console_output}

    class _NetworkAPI:
        def __init__(self, cloud: FakeOpenStackCloud) -> None:
            self._cloud = cloud

        def get_network(self, network_id: str) -> FakeNetworkModel:
            if network_id not in self._cloud.networks:
                raise NotFoundException(f"network {network_id} not found")
            return self._cloud.networks[network_id]

        def create_security_group(self, *, name: str, description: str) -> FakeSecurityGroup:
            if len(description.encode("utf-8")) > 255:
                raise ValueError("Neutron description exceeds 255 bytes")
            sg_id = self._cloud._next_id("sg")
            sg = FakeSecurityGroup(
                id=sg_id,
                name=name,
                project_id=self._cloud.current_project_id,
                description=description,
            )
            self._cloud.security_groups[sg_id] = sg
            return sg

        def create_security_group_rule(self, **kwargs: object) -> dict[str, object]:
            sg_id = str(kwargs["security_group_id"])
            self._cloud.security_groups[sg_id].rules.append(dict(kwargs))
            return dict(kwargs)

        def get_security_group(self, sg_id: str) -> FakeSecurityGroup:
            if sg_id not in self._cloud.security_groups:
                raise NotFoundException(f"security group {sg_id} not found")
            return self._cloud.security_groups[sg_id]

        def security_groups(self, **_kwargs: object) -> list[FakeSecurityGroup]:
            return list(self._cloud.security_groups.values())

        def delete_security_group(self, sg_id: str, *, ignore_missing: bool = False) -> None:
            if sg_id not in self._cloud.security_groups:
                if ignore_missing:
                    return
                raise NotFoundException(f"security group {sg_id} not found")
            del self._cloud.security_groups[sg_id]

        def create_port(
            self,
            *,
            name: str,
            network_id: str,
            security_group_ids: list[str],
            description: str,
        ) -> FakePort:
            if len(description.encode("utf-8")) > 255:
                raise ValueError("Neutron description exceeds 255 bytes")
            port_id = self._cloud._next_id("port")
            port = FakePort(
                id=port_id,
                name=name,
                network_id=network_id,
                project_id=self._cloud.current_project_id,
                security_group_ids=list(security_group_ids),
                description=description,
                fixed_ips=[{"ip_address": VM_PORT_IP}],
            )
            self._cloud.ports[port_id] = port
            return port

        def get_port(self, port_id: str) -> FakePort:
            if port_id not in self._cloud.ports:
                raise NotFoundException(f"port {port_id} not found")
            return self._cloud.ports[port_id]

        def ports(self, **_kwargs: object) -> list[FakePort]:
            # Mirrors the production Neutron quirk where server-side filter args
            # return all visible ports across the cloud.
            return list(self._cloud.ports.values())

        def delete_port(self, port_id: str, *, ignore_missing: bool = False) -> None:
            if port_id not in self._cloud.ports:
                if ignore_missing:
                    return
                raise NotFoundException(f"port {port_id} not found")
            del self._cloud.ports[port_id]

        def set_tags(self, resource: FakeSecurityGroup | FakePort, tags: list[str]) -> None:
            if any(len(tag) > 60 or "/" in tag for tag in tags):
                raise ValueError("invalid Neutron tag")
            resource.tags = list(tags)

    class _BlockStorageAPI:
        def __init__(self, cloud: FakeOpenStackCloud) -> None:
            self._cloud = cloud

        def get_endpoint(self) -> str:
            # OpenStack SDK 3.3.0 block_storage.get_endpoint() returns version discovery root without project.
            return "https://cinder.dmslab.re.kr/v3"

        def create_volume(
            self,
            *,
            name: str,
            size: int,
            image_id: str,
            volume_type: str,
            metadata: dict[str, str],
            description: str,
        ) -> FakeVolume:
            if len(description.encode("utf-8")) > 255:
                raise ValueError("Cinder description exceeds 255 bytes")
            if self._cloud.fail_create_volume is not None:
                raise self._cloud.fail_create_volume
            volume_id = self._cloud._next_id("vol")
            volume = FakeVolume(
                id=volume_id,
                name=name,
                project_id=self._cloud.current_project_id,
                size=size,
                image_id=image_id,
                volume_type=volume_type,
                metadata=dict(metadata),
                description=description,
            )
            self._cloud.volumes[volume_id] = volume
            return volume

        def get_volume(self, volume_id: str) -> FakeVolume:
            if volume_id not in self._cloud.volumes:
                raise NotFoundException(f"volume {volume_id} not found")
            return self._cloud.volumes[volume_id]

        def volumes(self, *, details: bool = True) -> list[FakeVolume]:
            assert details is True
            return list(self._cloud.volumes.values())

        def delete_volume(self, volume_id: str, *, ignore_missing: bool = False) -> None:
            if self._cloud.fail_delete_volume is not None:
                raise self._cloud.fail_delete_volume
            if volume_id not in self._cloud.volumes:
                if ignore_missing:
                    return
                raise NotFoundException(f"volume {volume_id} not found")
            if self._cloud.volumes[volume_id].status in {"creating", "downloading", "in-use"}:
                raise ValueError("Cinder volume not ready for deletion")
            del self._cloud.volumes[volume_id]


def _write_test_inputs(tmp_path: Path) -> tuple[Path, str, Path, str, Path, str]:
    source_tar = tmp_path / "source.tar"
    source_bytes = b"canonical-source-tar-payload"
    source_tar.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    kernel_path = tmp_path / "vmlinuz"
    kernel_bytes = (b"\x00" * 0x202) + b"HdrS" + b"pinned-linux-bzimage"
    kernel_path.write_bytes(kernel_bytes)
    kernel_sha256 = hashlib.sha256(kernel_bytes).hexdigest()

    config_path = tmp_path / "kernel.config"
    config_bytes = b"CONFIG_64BIT=y\nCONFIG_KVM=y\n"
    config_path.write_bytes(config_bytes)
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()

    return source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256


def _base_env(kernel_path: Path, config_path: Path) -> dict[str, str]:
    return {
        "OS_AUTH_URL": "https://keystone.dmslab.re.kr/v3",
        "OS_AUTH_TYPE": "v3applicationcredential",
        "OS_APPLICATION_CREDENTIAL_ID": "app-cred-id-1234",
        "OS_APPLICATION_CREDENTIAL_SECRET": "super-secret-credential-value",
        "OS_REGION_NAME": "RegionOne",
        "GITHUB_RUN_ID": "9001",
        "GITHUB_RUN_ATTEMPT": "1",
        helper.KERNEL_ENV: os.fspath(kernel_path),
        helper.KERNEL_CONFIG_ENV: os.fspath(config_path),
    }


def _make_request(
    tmp_path: Path, source_tar: Path, actual_source_sha256: str, **overrides: object
) -> helper.ProveRequest:
    values: dict[str, object] = {
        "repository": helper.APPROVED_REPOSITORY,
        "event": "push",
        "ref": "refs/heads/dev",
        "sha": COMMIT_SHA,
        "source_tar": source_tar,
        "source_sha256": actual_source_sha256,
        "project_id": CI_PROJECT_ID,
        "image_id": CI_IMAGE_ID,
        "flavor_id": helper.APPROVED_FLAVOR_ID,
        "network_id": helper.APPROVED_NETWORK_ID,
        "evidence_dir": tmp_path / "native-openstack-evidence",
        "deadline_seconds": 2700,
    }
    values.update(overrides)
    return helper.ProveRequest(**values)  # type: ignore[arg-type]


def _build_valid_evidence_tar_bytes(kernel_sha256: str, config_sha256: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for directory_name in ("oci-fs-evidence", "stage1-kvm-evidence"):
            dir_info = tarfile.TarInfo(directory_name)
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o700
            archive.addfile(dir_info)

        squashfs_payload = json.dumps({"candidate": "squashfs", "passed": True}, sort_keys=True).encode("utf-8")
        erofs_payload = json.dumps({"candidate": "erofs", "passed": False}, sort_keys=True).encode("utf-8")
        for rel_name, payload in (
            ("oci-fs-evidence/squashfs.json", squashfs_payload),
            ("oci-fs-evidence/squashfs-replay.json", squashfs_payload),
            ("oci-fs-evidence/erofs.json", erofs_payload),
        ):
            info = tarfile.TarInfo(rel_name)
            info.size = len(payload)
            info.mode = 0o400
            archive.addfile(info, io.BytesIO(payload))

        stage1_receipt = {
            "schema": "palimpsest.oci-stage1-kvm-proof.v20",
            "executed_boots": 43,
            "qemu_invocations": 44,
            "kernel": {
                "artifact_digest": f"sha256:{kernel_sha256}",
                "config_digest": f"sha256:{config_sha256}",
            },
            "qualification": {
                "accelerator": "kvm",
                "architecture": "x86_64",
                "cpu": "host",
                "kvm_api_version": 12,
                "live_pid1": True,
            },
        }
        for name in helper.EXPECTED_STAGE1_KVM_EVIDENCE_FILES:
            if name == "receipt.json":
                payload = (json.dumps(stage1_receipt, sort_keys=True) + "\n").encode("utf-8")
            else:
                payload = f"evidence:{name}\n".encode()
            info = tarfile.TarInfo(f"stage1-kvm-evidence/{name}")
            info.size = len(payload)
            info.mode = 0o400
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class FakeRemoteHarness:
    """Simulates local ssh-keygen/ssh-keyscan/ssh/scp execution against the disposable VM."""

    def __init__(
        self,
        *,
        kernel_sha256: str,
        config_sha256: str,
        scanned_host_key_b64: str = HOST_KEY_B64,
        remote_proof_error: str | None = None,
        timeout_on_remote_proof: bool = False,
        signal_on_remote_proof: int | None = None,
        evidence_bundle: bytes | None = None,
    ) -> None:
        self.kernel_sha256 = kernel_sha256
        self.config_sha256 = config_sha256
        self.scanned_host_key_b64 = scanned_host_key_b64
        self.remote_proof_error = remote_proof_error
        self.timeout_on_remote_proof = timeout_on_remote_proof
        self.signal_on_remote_proof = signal_on_remote_proof
        self.evidence_bundle = evidence_bundle
        self.uploaded_files: dict[str, bytes] = {}
        self.remote_proof_executed = False
        self.ssh_invocations_after_keyscan = 0

    def __call__(self, command: tuple[str, ...], timeout_seconds: float) -> helper.CommandResult:
        assert timeout_seconds > 0
        exe = command[0]
        if exe == "ssh-keygen":
            key_file = Path(command[command.index("-f") + 1])
            key_file.write_text(
                "-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE-KEY-SECRET\n-----END OPENSSH PRIVATE KEY-----\n",
                encoding="utf-8",
            )
            key_file.chmod(0o600)
            Path(f"{os.fspath(key_file)}.pub").write_text(
                f"ssh-ed25519 {HOST_KEY_B64} test-ephemeral\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, b"", b"")

        if exe == "ssh-keyscan":
            target_ip = command[-1]
            line = f"{target_ip} ssh-ed25519 {self.scanned_host_key_b64}\n".encode()
            return subprocess.CompletedProcess(command, 0, line, b"")

        if exe == "scp":
            self.ssh_invocations_after_keyscan += 1
            source_arg = command[-2]
            dest_arg = command[-1]
            if ":" in dest_arg:
                remote_name = dest_arg.rsplit("/", 1)[-1]
                self.uploaded_files[remote_name] = Path(source_arg).read_bytes()
                return subprocess.CompletedProcess(command, 0, b"", b"")
            bundle = self.evidence_bundle
            if bundle is None:
                bundle = _build_valid_evidence_tar_bytes(self.kernel_sha256, self.config_sha256)
            Path(dest_arg).write_bytes(bundle)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        if exe == "ssh":
            self.ssh_invocations_after_keyscan += 1
            remote_cmd = command[-1]
            if "pytest -m stage1_kvm" in remote_cmd:
                if self.signal_on_remote_proof is not None:
                    os.kill(os.getpid(), self.signal_on_remote_proof)
                if self.timeout_on_remote_proof:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                if self.remote_proof_error is not None:
                    return subprocess.CompletedProcess(command, 1, b"", self.remote_proof_error.encode("utf-8"))
                self.remote_proof_executed = True
            return subprocess.CompletedProcess(command, 0, b"", b"")

        raise AssertionError(f"unexpected command: {command}")


@pytest.mark.parametrize(
    ("overrides", "error_match"),
    [
        ({"repository": "fork-owner/palimpsest"}, "unapproved repository"),
        ({"event": "pull_request"}, "unapproved workflow event"),
        ({"event": "pull_request_target"}, "unapproved workflow event"),
        ({"event": "workflow_run"}, "unapproved workflow event"),
        ({"ref": "refs/pull/12/merge"}, "unapproved ref"),
        ({"ref": "refs/heads/feature"}, "unapproved ref"),
        ({"ref": "refs/tags/0.2.4"}, "unapproved ref"),
        ({"ref": "refs/tags/v0.2.4..evil"}, "unapproved ref"),
        ({"sha": "not-a-sha"}, "40-character hexadecimal"),
        ({"project_id": ADMIN_PROJECT_ID}, "refusing to use admin or candidate Hub project"),
        ({"flavor_id": "1d0efc12-2d9f-4104-8828-3fd2cecb4877"}, "flavor-id must be pinned cpu.2c_8g"),
        ({"network_id": "02dd84ca-653d-4fd6-a425-05b870786c87"}, "network-id must be pinned public_provider"),
        ({"source_sha256": "0" * 64}, "source archive SHA-256 does not match"),
        ({"deadline_seconds": 0}, "deadline-seconds"),
        ({"deadline_seconds": 3600}, "deadline-seconds"),
    ],
)
def test_prove_rejects_unapproved_inputs_before_cloud_connection_or_manifest(
    tmp_path: Path,
    overrides: dict[str, object],
    error_match: str,
) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256, **overrides)
    cloud_called = False

    def _forbidden_cloud(_env: object) -> Any:
        nonlocal cloud_called
        cloud_called = True
        raise AssertionError("cloud connection must not be opened when pre-cloud validation fails")

    with pytest.raises(helper.NativeOpenStackError, match=error_match):
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=_forbidden_cloud,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )

    assert cloud_called is False
    assert not (request.evidence_dir / "resource-manifest.json").exists()


def test_prove_rejects_unapproved_kernel_or_config_hash_against_production_pins_before_cloud(
    tmp_path: Path,
) -> None:
    source_tar, source_sha256, kernel_path, _, config_path, _ = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)
    cloud_called = False

    def _forbidden_cloud(_env: object) -> Any:
        nonlocal cloud_called
        cloud_called = True
        raise AssertionError("cloud connection must not be opened when kernel hash is unapproved")

    with pytest.raises(helper.NativeOpenStackError, match="KVM kernel SHA-256 does not match approved pin"):
        helper.prove_native_kvm(request, env=env, cloud_factory=_forbidden_cloud)

    assert cloud_called is False
    assert not (request.evidence_dir / "resource-manifest.json").exists()


def test_cli_rejects_secret_bearing_argv_flags() -> None:
    rc = helper.main(["prove", "--os-password", "secret"])
    assert rc == 1


@pytest.mark.parametrize("bad_ip", ["127.0.0.1", "10.100.100.50", "192.168.1.10", "0.0.0.0", "not-an-ip"])
def test_prove_rejects_non_public_egress_ipv4_before_creating_resources(tmp_path: Path, bad_ip: str) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)
    cloud = FakeOpenStackCloud()

    with pytest.raises(helper.NativeOpenStackError, match="egress"):
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _e: cloud,
            egress_ip_resolver=lambda _t: bad_ip,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )

    assert cloud.servers == {}
    assert cloud.volumes == {}
    assert cloud.ports == {}
    assert cloud.security_groups == {}
    assert cloud.keypairs == {}
    assert not (request.evidence_dir / "resource-manifest.json").exists()


def test_prove_executes_full_lifecycle_verifies_evidence_and_preserves_foreign_resources(tmp_path: Path) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256, ref="refs/tags/v0.2.4")
    cloud = FakeOpenStackCloud()

    # Pre-populate unrelated resources that must remain untouched.
    cloud.servers["srv-foreign"] = FakeServer(
        id="srv-foreign",
        name="existing-hub-vm",
        project_id=FOREIGN_PROJECT_ID,
        flavor_id=helper.APPROVED_FLAVOR_ID,
        key_name="foreign-key",
        port_id="port-foreign",
        volume_id="vol-foreign",
        metadata={"palimpsest_owner": "other"},
    )
    cloud.volumes["vol-foreign"] = FakeVolume(
        id="vol-foreign",
        name="existing-hub-vol",
        project_id=FOREIGN_PROJECT_ID,
        size=20,
        image_id=CI_IMAGE_ID,
        volume_type="ceph_hdd",
        metadata={"palimpsest_owner": "other"},
        description="foreign",
    )
    for idx in range(365):
        port_id = f"port-unrelated-{idx}"
        cloud.ports[port_id] = FakePort(
            id=port_id,
            name=f"unrelated-{idx}",
            network_id=helper.APPROVED_NETWORK_ID,
            project_id=FOREIGN_PROJECT_ID,
            security_group_ids=["sg-other-tenant"],
            description="other tenant port",
            fixed_ips=[{"ip_address": "172.30.0.10"}],
        )

    harness = FakeRemoteHarness(kernel_sha256=kernel_sha256, config_sha256=config_sha256)
    receipt = helper.prove_native_kvm(
        request,
        env=env,
        cloud_factory=lambda _e: cloud,
        command_runner=harness,
        egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
        expected_kernel_sha256=kernel_sha256,
        expected_kernel_config_sha256=config_sha256,
    )

    assert harness.remote_proof_executed is True
    assert set(harness.uploaded_files) == {"source.tar", "vmlinuz", "kernel.config"}
    assert receipt["executed_boots"] == 43
    assert receipt["qemu_invocations"] == 44
    assert receipt["source_sha256"] == source_sha256
    assert receipt["kernel_sha256"] == kernel_sha256
    assert receipt["kernel_config_sha256"] == config_sha256
    assert receipt["ssh_host_key_fingerprint"] == HOST_KEY_FINGERPRINT

    manifest_path = request.evidence_dir / "resource-manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest_data) == helper.MANIFEST_FIELDS
    assert manifest_data["schema"] == helper.MANIFEST_SCHEMA
    assert manifest_data["cleanup_verified"] is True
    assert "PRIVATE KEY" not in manifest_path.read_text(encoding="utf-8")
    assert "super-secret-credential-value" not in manifest_path.read_text(encoding="utf-8")

    # Owned resources are deleted; foreign resources remain intact.
    assert set(cloud.servers) == {"srv-foreign"}
    assert set(cloud.volumes) == {"vol-foreign"}
    assert len(cloud.ports) == 365
    assert cloud.security_groups == {}
    assert cloud.keypairs == {}


def test_cleanup_missing_manifest_is_safe_noop_without_touching_cloud(tmp_path: Path) -> None:
    cloud = FakeOpenStackCloud()
    cloud.servers["srv-existing"] = FakeServer(
        id="srv-existing",
        name="keep-me",
        project_id=CI_PROJECT_ID,
        flavor_id=helper.APPROVED_FLAVOR_ID,
        key_name="keep-key",
        port_id="port-keep",
        volume_id="vol-keep",
        metadata={},
    )
    missing_path = tmp_path / "native-openstack-evidence" / "resource-manifest.json"

    result = helper.cleanup_manifest(
        missing_path,
        env={"OS_AUTH_URL": "https://keystone.dmslab.re.kr/v3"},
        cloud_factory=lambda _e: (_ for _ in ()).throw(AssertionError("cloud must not be called for missing manifest")),
    )

    assert result is None
    assert "srv-existing" in cloud.servers


def test_cleanup_refuses_mismatched_authenticated_project_or_foreign_resource_owner(tmp_path: Path) -> None:
    _, source_sha256, kernel_path, _, config_path, _ = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    owner = helper.OwnerIdentity(
        repository=helper.APPROVED_REPOSITORY,
        run_id="9001",
        run_attempt="1",
        source_sha=COMMIT_SHA,
        source_sha256=source_sha256,
        project_id=CI_PROJECT_ID,
    )
    manifest_path = tmp_path / "resource-manifest.json"
    helper._write_manifest(
        manifest_path,
        helper.ResourceManifest(
            schema=helper.MANIFEST_SCHEMA,
            repository=owner.repository,
            run_id=owner.run_id,
            run_attempt=owner.run_attempt,
            source_sha=owner.source_sha,
            source_sha256=owner.source_sha256,
            project_id=CI_PROJECT_ID,
            server_id="srv-0001",
            volume_id="vol-0001",
            port_id="port-0001",
            security_group_id="sg-0001",
            keypair_name=owner.keypair_name,
            created_at="2026-09-27T00:00:00Z",
            cleanup_verified=False,
        ),
    )

    # 1. Authenticated project mismatch refuses before any resource deletion.
    wrong_project_cloud = FakeOpenStackCloud(current_project_id=FOREIGN_PROJECT_ID)
    wrong_project_cloud.servers["srv-0001"] = FakeServer(
        id="srv-0001",
        name=owner.server_name,
        project_id=CI_PROJECT_ID,
        flavor_id=helper.APPROVED_FLAVOR_ID,
        key_name=owner.keypair_name,
        port_id="port-0001",
        volume_id="vol-0001",
        metadata=owner.metadata,
    )
    with pytest.raises(helper.NativeOpenStackError, match="authenticated OpenStack project does not match"):
        helper.cleanup_manifest(manifest_path, env=env, cloud_factory=lambda _e: wrong_project_cloud)
    assert "srv-0001" in wrong_project_cloud.servers

    # 2. Server with foreign owner metadata is refused and preserved.
    foreign_owner_cloud = FakeOpenStackCloud(current_project_id=CI_PROJECT_ID)
    bad_metadata = dict(owner.metadata)
    bad_metadata["palimpsest_owner"] = "openstack-afterglow/palimpsest/9999/1"
    foreign_owner_cloud.servers["srv-0001"] = FakeServer(
        id="srv-0001",
        name=owner.server_name,
        project_id=CI_PROJECT_ID,
        flavor_id=helper.APPROVED_FLAVOR_ID,
        key_name=owner.keypair_name,
        port_id="port-0001",
        volume_id="vol-0001",
        metadata=bad_metadata,
    )
    with pytest.raises(helper.NativeOpenStackError):
        helper.cleanup_manifest(manifest_path, env=env, cloud_factory=lambda _e: foreign_owner_cloud)
    assert "srv-0001" in foreign_owner_cloud.servers
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["cleanup_verified"] is False


def test_cleanup_refuses_foreign_keypair_user_and_sg_bound_to_foreign_port(tmp_path: Path) -> None:
    _, source_sha256, kernel_path, _, config_path, _ = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    owner = helper.OwnerIdentity(
        repository=helper.APPROVED_REPOSITORY,
        run_id="9001",
        run_attempt="1",
        source_sha=COMMIT_SHA,
        source_sha256=source_sha256,
        project_id=CI_PROJECT_ID,
    )
    manifest_path = tmp_path / "resource-manifest.json"
    helper._write_manifest(
        manifest_path,
        helper.ResourceManifest(
            schema=helper.MANIFEST_SCHEMA,
            repository=owner.repository,
            run_id=owner.run_id,
            run_attempt=owner.run_attempt,
            source_sha=owner.source_sha,
            source_sha256=owner.source_sha256,
            project_id=CI_PROJECT_ID,
            server_id=None,
            volume_id=None,
            port_id=None,
            security_group_id="sg-0001",
            keypair_name=owner.keypair_name,
            created_at="2026-09-27T00:00:00Z",
            cleanup_verified=False,
        ),
    )

    cloud = FakeOpenStackCloud(current_project_id=CI_PROJECT_ID, current_user_id=CI_USER_ID)
    cloud.security_groups["sg-0001"] = FakeSecurityGroup(
        id="sg-0001",
        name=owner.security_group_name,
        project_id=CI_PROJECT_ID,
        description=owner.description,
        tags=list(owner.tags),
    )
    cloud.ports["port-foreign-binding"] = FakePort(
        id="port-foreign-binding",
        name="foreign-port",
        network_id=helper.APPROVED_NETWORK_ID,
        project_id=CI_PROJECT_ID,
        security_group_ids=["sg-0001"],
        description="foreign port binding sg-0001",
        fixed_ips=[{"ip_address": VM_PORT_IP}],
    )
    cloud.keypairs[owner.keypair_name] = FakeKeypair(
        name=owner.keypair_name,
        public_key=f"ssh-ed25519 {HOST_KEY_B64}",
        user_id=CI_USER_ID,
    )

    with pytest.raises(helper.NativeOpenStackError, match="still bound to ports"):
        helper.cleanup_manifest(manifest_path, env=env, cloud_factory=lambda _e: cloud)
    assert "sg-0001" in cloud.security_groups

    # A foreign-user keypair refuses all destructive operations before deletion.
    del cloud.ports["port-foreign-binding"]
    cloud.keypairs[owner.keypair_name].user_id = FOREIGN_USER_ID
    with pytest.raises(helper.NativeOpenStackError, match="user_id or name mismatch"):
        helper.cleanup_manifest(manifest_path, env=env, cloud_factory=lambda _e: cloud)
    assert "sg-0001" in cloud.security_groups
    assert owner.keypair_name in cloud.keypairs
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["cleanup_verified"] is False


def test_partial_create_failure_cleans_up_earlier_owned_resources(tmp_path: Path) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)
    cloud = FakeOpenStackCloud()
    cloud.fail_create_server = RuntimeError("Compute host quota exceeded")
    harness = FakeRemoteHarness(kernel_sha256=kernel_sha256, config_sha256=config_sha256)

    with pytest.raises(RuntimeError, match="Compute host quota exceeded"):
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _e: cloud,
            command_runner=harness,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )

    assert cloud.servers == {}
    assert cloud.volumes == {}
    assert cloud.ports == {}
    assert cloud.security_groups == {}
    assert cloud.keypairs == {}
    manifest_data = json.loads((request.evidence_dir / "resource-manifest.json").read_text(encoding="utf-8"))
    assert manifest_data["cleanup_verified"] is False
    assert manifest_data["inflight"] == "server_id"
    assert not (request.evidence_dir / "native-proof-receipt.json").exists()


def test_cleanup_recovers_creation_inflight_interruption_gap_via_owner_tags_only(tmp_path: Path) -> None:
    _, source_sha256, kernel_path, _, config_path, _ = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    owner = helper.OwnerIdentity(
        repository=helper.APPROVED_REPOSITORY,
        run_id="9001",
        run_attempt="1",
        source_sha=COMMIT_SHA,
        source_sha256=source_sha256,
        project_id=CI_PROJECT_ID,
    )
    manifest_path = tmp_path / "resource-manifest.json"
    # Pre-creation manifest written before returned IDs were persisted.
    helper._write_manifest(
        manifest_path,
        helper.ResourceManifest(
            schema=helper.MANIFEST_SCHEMA,
            repository=owner.repository,
            run_id=owner.run_id,
            run_attempt=owner.run_attempt,
            source_sha=owner.source_sha,
            source_sha256=owner.source_sha256,
            project_id=CI_PROJECT_ID,
            server_id=None,
            volume_id=None,
            port_id=None,
            security_group_id=None,
            keypair_name=None,
            created_at="2026-09-27T00:00:00Z",
            cleanup_verified=False,
            inflight="server_id",
        ),
    )

    cloud = FakeOpenStackCloud(current_project_id=CI_PROJECT_ID, current_user_id=CI_USER_ID)
    kp = cloud.compute.create_keypair(name=owner.keypair_name, public_key=f"ssh-ed25519 {HOST_KEY_B64}")
    sg = cloud.network.create_security_group(name=owner.security_group_name, description=owner.description)
    cloud.network.set_tags(sg, list(owner.tags))
    port = cloud.network.create_port(
        name=owner.port_name,
        network_id=helper.APPROVED_NETWORK_ID,
        security_group_ids=[sg.id],
        description=owner.description,
    )
    cloud.network.set_tags(port, list(owner.tags))
    vol = cloud.block_storage.create_volume(
        name=owner.volume_name,
        size=20,
        image_id=CI_IMAGE_ID,
        volume_type="ceph_hdd",
        metadata=owner.metadata,
        description=owner.description,
    )
    srv = cloud.compute.create_server(
        name=owner.server_name,
        flavor_id=helper.APPROVED_FLAVOR_ID,
        key_name=kp.name,
        networks=[{"port": port.id}],
        block_device_mapping_v2=[{"uuid": vol.id}],
        metadata=owner.metadata,
    )

    # Another run's resources in the same project must NOT be touched.
    cloud.servers["srv-other-run"] = FakeServer(
        id="srv-other-run",
        name="palimpsest-ci-9002-1-other-vm",
        project_id=CI_PROJECT_ID,
        flavor_id=helper.APPROVED_FLAVOR_ID,
        key_name="other-key",
        port_id="port-other",
        volume_id="vol-other",
        metadata={"palimpsest_owner": "openstack-afterglow/palimpsest/9002/1"},
    )

    cleaned = helper.cleanup_manifest(manifest_path, env=env, cloud_factory=lambda _e: cloud)
    assert cleaned is not None
    assert cleaned.cleanup_verified is True
    assert cleaned.server_id == srv.id
    assert cleaned.volume_id == vol.id
    assert cleaned.port_id == port.id
    assert cleaned.security_group_id == sg.id
    assert cleaned.keypair_name == kp.name
    assert set(cloud.servers) == {"srv-other-run"}
    assert cloud.volumes == {}
    assert cloud.ports == {}
    assert cloud.security_groups == {}
    assert cloud.keypairs == {}


def test_ssh_host_key_mismatch_fails_before_remote_ssh_and_cleans_up(tmp_path: Path) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)
    cloud = FakeOpenStackCloud()
    harness = FakeRemoteHarness(
        kernel_sha256=kernel_sha256,
        config_sha256=config_sha256,
        scanned_host_key_b64=OTHER_HOST_KEY_B64,
    )

    with pytest.raises(helper.NativeOpenStackError, match="does not match Nova console"):
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _e: cloud,
            command_runner=harness,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )

    assert harness.ssh_invocations_after_keyscan == 0
    assert cloud.servers == {}
    assert cloud.volumes == {}
    assert cloud.ports == {}
    assert cloud.security_groups == {}
    assert cloud.keypairs == {}
    manifest_data = json.loads((request.evidence_dir / "resource-manifest.json").read_text(encoding="utf-8"))
    assert manifest_data["cleanup_verified"] is True


def test_remote_proof_timeout_and_cloud_wait_timeout_clean_up_owned_resources(tmp_path: Path) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)

    # 1. Remote subprocess timeout
    request_one = _make_request(tmp_path / "run1", source_tar, source_sha256)
    cloud_one = FakeOpenStackCloud()
    harness_one = FakeRemoteHarness(
        kernel_sha256=kernel_sha256,
        config_sha256=config_sha256,
        timeout_on_remote_proof=True,
    )
    with pytest.raises(helper.NativeProofTimeoutError, match="deadline exceeded"):
        helper.prove_native_kvm(
            request_one,
            env=env,
            cloud_factory=lambda _e: cloud_one,
            command_runner=harness_one,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )
    assert cloud_one.servers == {}
    assert cloud_one.volumes == {}
    assert (
        json.loads((request_one.evidence_dir / "resource-manifest.json").read_text(encoding="utf-8"))[
            "cleanup_verified"
        ]
        is True
    )

    # 2. Cloud wait timeout when server stays in BUILD past deadline
    request_two = _make_request(tmp_path / "run2", source_tar, source_sha256, deadline_seconds=5)
    cloud_two = FakeOpenStackCloud()
    cloud_two.server_never_active = True
    now = 100.0

    def _clock() -> float:
        return now

    def _sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    harness_two = FakeRemoteHarness(kernel_sha256=kernel_sha256, config_sha256=config_sha256)
    with pytest.raises(helper.NativeProofTimeoutError, match="waiting for server"):
        helper.prove_native_kvm(
            request_two,
            env=env,
            cloud_factory=lambda _e: cloud_two,
            command_runner=harness_two,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            clock=_clock,
            sleeper=_sleep,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )
    assert cloud_two.servers == {}
    assert cloud_two.volumes == {}
    assert (
        json.loads((request_two.evidence_dir / "resource-manifest.json").read_text(encoding="utf-8"))[
            "cleanup_verified"
        ]
        is True
    )


def test_cleanup_failure_leaves_cleanup_verified_false_and_redacts_secrets(tmp_path: Path) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)
    cloud = FakeOpenStackCloud()
    cloud.fail_delete_volume = RuntimeError("Cinder backend failure token=super-secret-credential-value")
    harness = FakeRemoteHarness(kernel_sha256=kernel_sha256, config_sha256=config_sha256)

    with pytest.raises(helper.NativeOpenStackError) as exc_info:
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _e: cloud,
            command_runner=harness,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )

    assert "super-secret-credential-value" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    manifest_data = json.loads((request.evidence_dir / "resource-manifest.json").read_text(encoding="utf-8"))
    assert manifest_data["cleanup_verified"] is False


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_interruption_reclaims_owned_resources_via_manifest(tmp_path: Path, signum: int) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)
    cloud = FakeOpenStackCloud()
    harness = FakeRemoteHarness(
        kernel_sha256=kernel_sha256,
        config_sha256=config_sha256,
        signal_on_remote_proof=signum,
    )

    with pytest.raises(helper.NativeProofInterrupted, match="interrupted by signal"):
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _e: cloud,
            command_runner=harness,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )

    assert cloud.servers == {}
    assert cloud.volumes == {}
    assert cloud.ports == {}
    assert cloud.security_groups == {}
    assert cloud.keypairs == {}
    manifest_data = json.loads((request.evidence_dir / "resource-manifest.json").read_text(encoding="utf-8"))
    assert manifest_data["cleanup_verified"] is True
    assert not (request.evidence_dir / "native-proof-receipt.json").exists()


def test_catalog_volumev3_endpoint_validation_accepts_discovery_root_and_rejects_foreign_catalog(
    tmp_path: Path,
) -> None:
    source_tar, source_sha256, kernel_path, kernel_sha256, config_path, config_sha256 = _write_test_inputs(tmp_path)
    env = _base_env(kernel_path, config_path)
    request = _make_request(tmp_path, source_tar, source_sha256)

    cloud = FakeOpenStackCloud(current_project_id=CI_PROJECT_ID)
    assert cloud.block_storage.get_endpoint() == "https://cinder.dmslab.re.kr/v3"
    cloud.volumev3_catalog_endpoint = f"https://cinder.dmslab.re.kr/v3/{ADMIN_PROJECT_ID.replace('-', '')}"

    with pytest.raises(helper.NativeOpenStackError, match="volumev3 catalog endpoint points to forbidden project"):
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _e: cloud,
            egress_ip_resolver=lambda _t: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_sha256,
            expected_kernel_config_sha256=config_sha256,
        )
    assert not (request.evidence_dir / "resource-manifest.json").exists()


def _cleanup_fixture(tmp_path: Path):
    _, digest, kernel, _, config, _ = _write_test_inputs(tmp_path)
    owner = helper.OwnerIdentity(helper.APPROVED_REPOSITORY, "9001", "1", COMMIT_SHA, digest, CI_PROJECT_ID)
    manifest = helper.ResourceManifest(
        schema=helper.MANIFEST_SCHEMA,
        repository=owner.repository,
        run_id=owner.run_id,
        run_attempt=owner.run_attempt,
        source_sha=owner.source_sha,
        source_sha256=owner.source_sha256,
        project_id=owner.project_id,
        server_id=None,
        volume_id=None,
        port_id=None,
        security_group_id=None,
        keypair_name=None,
        created_at="2026-09-27T00:00:00Z",
        cleanup_verified=False,
    )
    path = tmp_path / "manifest.json"
    helper._write_manifest(path, manifest)
    return owner, manifest, path, _base_env(kernel, config), FakeOpenStackCloud()


@pytest.mark.parametrize("status", ["creating", "downloading"])
def test_cleanup_waits_for_cinder_copy_before_deleting(tmp_path: Path, status: str) -> None:
    owner, manifest, path, env, cloud = _cleanup_fixture(tmp_path)
    volume = cloud.block_storage.create_volume(
        name=owner.volume_name,
        size=20,
        image_id=CI_IMAGE_ID,
        volume_type="ceph_hdd",
        metadata=owner.metadata,
        description=owner.description,
    )
    volume.status = status
    helper._write_manifest(path, helper.replace(manifest, volume_id=volume.id))
    now = 0.0

    def advance(seconds):
        nonlocal now
        now += seconds
        volume.status = "available"

    result = helper.cleanup_manifest(path, env=env, cloud_factory=lambda _: cloud, clock=lambda: now, sleeper=advance)
    assert result.cleanup_verified
    assert volume.id not in cloud.volumes
    assert now == helper.POLL_INTERVAL_SECONDS


@pytest.mark.parametrize("corruption", ["owner", "project", "recorded_id"])
def test_inflight_discovery_refuses_collisions_instead_of_claiming_absence(tmp_path: Path, corruption: str) -> None:
    owner, manifest, path, env, cloud = _cleanup_fixture(tmp_path)
    volume = cloud.block_storage.create_volume(
        name=owner.volume_name,
        size=20,
        image_id=CI_IMAGE_ID,
        volume_type="ceph_hdd",
        metadata=owner.metadata,
        description=owner.description,
    )
    if corruption == "owner":
        volume.metadata["palimpsest_owner"] = "foreign"
    elif corruption == "project":
        volume.project_id = FOREIGN_PROJECT_ID
    else:
        helper._write_manifest(path, helper.replace(manifest, volume_id="stale-id"))
    with pytest.raises(helper.NativeOpenStackError):
        helper.cleanup_manifest(path, env=env, cloud_factory=lambda _: cloud)
    assert volume.id in cloud.volumes
    assert not helper._load_manifest(path).cleanup_verified


def test_lost_create_response_is_recovered_without_leaking_sdk_exception(tmp_path: Path) -> None:
    source, digest, kernel, kernel_digest, config, config_digest = _write_test_inputs(tmp_path)
    env = _base_env(kernel, config)
    request = _make_request(tmp_path, source, digest)
    cloud = FakeOpenStackCloud()

    def disconnect(_server):
        stored = json.loads((request.evidence_dir / "resource-manifest.json").read_text())
        assert stored["inflight"] == "server_id"
        assert stored["server_id"] is None
        raise RuntimeError(env["OS_APPLICATION_CREDENTIAL_SECRET"])

    cloud.on_after_create_server = disconnect
    with pytest.raises(helper.NativeOpenStackError) as failure:
        helper.prove_native_kvm(
            request,
            env=env,
            cloud_factory=lambda _: cloud,
            command_runner=FakeRemoteHarness(kernel_sha256=kernel_digest, config_sha256=config_digest),
            egress_ip_resolver=lambda _: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kernel_digest,
            expected_kernel_config_sha256=config_digest,
        )
    assert env["OS_APPLICATION_CREDENTIAL_SECRET"] not in str(failure.value)
    assert (
        not cloud.servers and not cloud.volumes and not cloud.ports and not cloud.security_groups and not cloud.keypairs
    )
    manifest = helper._load_manifest(request.evidence_dir / "resource-manifest.json")
    assert manifest.cleanup_verified and manifest.inflight is None and manifest.server_id is not None


def test_local_children_do_not_inherit_cloud_or_github_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_SECRET", "cloud-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    result = helper._default_run_command(
        (sys.executable, "-c", "import os,json; print(json.dumps(sorted(os.environ)))"),
        10,
    )
    child_keys = json.loads(result.stdout)
    assert "OS_APPLICATION_CREDENTIAL_SECRET" not in child_keys
    assert "GITHUB_TOKEN" not in child_keys


@pytest.mark.parametrize("setting", [{"OS_VERIFY": "false"}, {"OS_INSECURE": "yes"}])
def test_disabled_tls_refuses_cloud_connection(tmp_path: Path, setting) -> None:
    _, _, path, env, cloud = _cleanup_fixture(tmp_path)
    env.update(setting)
    with pytest.raises(helper.NativeOpenStackError):
        helper.cleanup_manifest(path, env=env, cloud_factory=lambda _: cloud)
    assert not helper._load_manifest(path).cleanup_verified


@pytest.mark.parametrize("kind", ["symlink", "oversized", "unsafe_id"])
def test_manifest_unsafe_file_or_resource_id_never_authorizes_cleanup(tmp_path: Path, kind: str) -> None:
    _, manifest, path, env, cloud = _cleanup_fixture(tmp_path)
    if kind == "symlink":
        link = tmp_path / "linked.json"
        link.symlink_to(path)
        path = link
    elif kind == "oversized":
        path.write_text(" " * 16385 + json.dumps(manifest.to_dict()))
    else:
        helper._write_manifest(path, helper.replace(manifest, server_id="../foreign"))
    with pytest.raises(helper.NativeOpenStackError):
        helper.cleanup_manifest(path, env=env, cloud_factory=lambda _: cloud)


def test_maximum_run_identity_fits_cloud_resource_schemas(tmp_path: Path) -> None:
    owner, _, _, _, cloud = _cleanup_fixture(tmp_path)
    owner = helper.replace(owner, run_id="9" * 20, run_attempt="9" * 20)
    group = cloud.network.create_security_group(name=owner.security_group_name, description=owner.description)
    cloud.network.set_tags(group, list(owner.tags))
    assert helper._matches_owner_description_and_tags(group, owner)
    altered = helper.replace(owner, source_sha="0" * 40)
    assert not helper._matches_owner_description_and_tags(group, altered)


@pytest.mark.parametrize(
    "endpoint", [None, "https://cinder.dmslab.re.kr/v3", f"https://cinder.dmslab.re.kr/v3/{FOREIGN_PROJECT_ID}"]
)
def test_catalog_validation_fails_closed_on_absent_or_unscoped_endpoint(tmp_path: Path, endpoint) -> None:
    _, _, path, env, cloud = _cleanup_fixture(tmp_path)
    cloud.volumev3_catalog_endpoint = endpoint
    if endpoint is None:
        cloud.session = None
    with pytest.raises(helper.NativeOpenStackError):
        helper.cleanup_manifest(path, env=env, cloud_factory=lambda _: cloud)
    assert not helper._load_manifest(path).cleanup_verified


@pytest.mark.parametrize("kernel_digest, config_digest", [("0" * 64, "b" * 64), ("a" * 64, "0" * 64)])
def test_canonical_evidence_rejects_unpinned_kernel_pair(tmp_path: Path, kernel_digest, config_digest) -> None:
    bundle = tmp_path / "bundle.tar"
    bundle.write_bytes(_build_valid_evidence_tar_bytes(kernel_digest, config_digest))
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    with pytest.raises(helper.NativeOpenStackError):
        helper._extract_and_verify_evidence_bundle(
            bundle,
            evidence,
            expected_kernel_sha256="a" * 64,
            expected_kernel_config_sha256="b" * 64,
        )


def test_existing_ci_volume_blocks_another_proof_before_create(tmp_path: Path) -> None:
    source, digest, kernel, kd, config, cd = _write_test_inputs(tmp_path)
    request = _make_request(tmp_path, source, digest)
    cloud = FakeOpenStackCloud()
    retained = cloud.block_storage.create_volume(
        name="retained-resource",
        size=20,
        image_id=CI_IMAGE_ID,
        volume_type="ceph_hdd",
        metadata={},
        description="retained",
    )
    with pytest.raises(helper.NativeOpenStackError):
        helper.prove_native_kvm(
            request,
            env=_base_env(kernel, config),
            cloud_factory=lambda _: cloud,
            expected_kernel_sha256=kd,
            expected_kernel_config_sha256=cd,
        )
    assert set(cloud.volumes) == {retained.id}
    assert not cloud.servers and not cloud.keypairs
    assert not request.evidence_dir.exists()


def test_blocked_cloud_create_is_deadline_bounded_and_recovered(tmp_path: Path) -> None:
    source, digest, kernel, kd, config, cd = _write_test_inputs(tmp_path)
    request = _make_request(tmp_path, source, digest, deadline_seconds=1)
    cloud = FakeOpenStackCloud()

    def blocked_response(_server):
        time.sleep(3)

    cloud.on_after_create_server = blocked_response
    with pytest.raises(helper.NativeProofTimeoutError):
        helper.prove_native_kvm(
            request,
            env=_base_env(kernel, config),
            cloud_factory=lambda _: cloud,
            command_runner=FakeRemoteHarness(kernel_sha256=kd, config_sha256=cd),
            egress_ip_resolver=lambda _: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kd,
            expected_kernel_config_sha256=cd,
        )
    assert (
        not cloud.servers and not cloud.volumes and not cloud.ports and not cloud.security_groups and not cloud.keypairs
    )
    assert helper._load_manifest(request.evidence_dir / "resource-manifest.json").cleanup_verified


def test_failed_remote_proof_preserves_partial_captures_without_success_receipt(tmp_path: Path) -> None:
    source, digest, kernel, kd, config, cd = _write_test_inputs(tmp_path)
    request = _make_request(tmp_path, source, digest)
    cloud = FakeOpenStackCloud()
    captures = {
        "oci-fs-evidence/squashfs.json": b'{"passed": true}\n',
        "stage1-kvm-evidence/console.bin": b"guest boot failed before receipt\n",
        "stage1-kvm-evidence/retained-console.bin": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in captures.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    harness = FakeRemoteHarness(
        kernel_sha256=kd,
        config_sha256=cd,
        remote_proof_error="stage1 guest failed",
        evidence_bundle=buffer.getvalue(),
    )
    with pytest.raises(helper.NativeOpenStackError, match="stage1 guest failed"):
        helper.prove_native_kvm(
            request,
            env=_base_env(kernel, config),
            cloud_factory=lambda _: cloud,
            command_runner=harness,
            egress_ip_resolver=lambda _: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kd,
            expected_kernel_config_sha256=cd,
        )
    for name, payload in captures.items():
        assert (request.evidence_dir / name).read_bytes() == payload
    assert not (request.evidence_dir / "native-proof-receipt.json").exists()
    assert not (request.evidence_dir / "stage1-kvm-evidence/receipt.json").exists()
    assert helper._load_manifest(request.evidence_dir / "resource-manifest.json").cleanup_verified
    assert (
        not cloud.servers and not cloud.volumes and not cloud.ports and not cloud.security_groups and not cloud.keypairs
    )


@pytest.mark.parametrize("unsafe_kind", ["symlink", "absolute", "traversal", "oversized", "duplicate"])
def test_partial_evidence_rejects_unsafe_archive_before_publishing_captures(tmp_path: Path, unsafe_kind: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        good = tarfile.TarInfo("stage1-kvm-evidence/console.bin")
        good.size = 4
        archive.addfile(good, io.BytesIO(b"boot"))
        bad = tarfile.TarInfo("stage1-kvm-evidence/retained-console.bin")
        if unsafe_kind == "symlink":
            bad.type = tarfile.SYMTYPE
            bad.linkname = "../../id_ed25519"
        elif unsafe_kind == "absolute":
            bad.name = "/stage1-kvm-evidence/retained-console.bin"
        elif unsafe_kind == "traversal":
            bad.name = "stage1-kvm-evidence/../../id_ed25519"
        elif unsafe_kind == "oversized":
            bad.size = 16 * 1024 * 1024 + 1
        else:
            bad.name = good.name
        archive.addfile(bad)
    bundle = tmp_path / "bundle.tar"
    bundle.write_bytes(buffer.getvalue())
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    with pytest.raises(helper.NativeOpenStackError):
        helper._extract_and_verify_evidence_bundle(
            bundle,
            evidence,
            expected_kernel_sha256="a" * 64,
            expected_kernel_config_sha256="b" * 64,
            require_complete=False,
        )
    assert not (evidence / good.name).exists()
    assert not (tmp_path / "id_ed25519").exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_successful_cleanup_finishes_teardown_but_fails_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    source, digest, kernel, kd, config, cd = _write_test_inputs(tmp_path)
    request = _make_request(tmp_path, source, digest)
    cloud = FakeOpenStackCloud()
    delete_server = cloud.compute.delete_server
    previous_handler = signal.getsignal(signum)

    def interrupted_delete(server_id: str, *, ignore_missing: bool = False) -> None:
        os.kill(os.getpid(), signum)
        delete_server(server_id, ignore_missing=ignore_missing)

    monkeypatch.setattr(cloud.compute, "delete_server", interrupted_delete)
    with pytest.raises(helper.NativeProofInterrupted, match="cleanup interrupted by signal"):
        helper.prove_native_kvm(
            request,
            env=_base_env(kernel, config),
            cloud_factory=lambda _: cloud,
            command_runner=FakeRemoteHarness(kernel_sha256=kd, config_sha256=cd),
            egress_ip_resolver=lambda _: PUBLIC_EGRESS_IP,
            expected_kernel_sha256=kd,
            expected_kernel_config_sha256=cd,
        )
    assert (
        not cloud.servers and not cloud.volumes and not cloud.ports and not cloud.security_groups and not cloud.keypairs
    )
    assert helper._load_manifest(request.evidence_dir / "resource-manifest.json").cleanup_verified
    assert signal.getsignal(signum) == previous_handler


def test_lost_manifest_after_cloud_creation_is_not_a_precreation_noop(tmp_path: Path) -> None:
    owner, manifest, path, env, cloud = _cleanup_fixture(tmp_path)
    helper._write_manifest(path, helper.replace(manifest, inflight="security_group_id"))
    security_group = cloud.network.create_security_group(name=owner.security_group_name, description=owner.description)
    helper._write_manifest(path, helper.replace(manifest, security_group_id=security_group.id))
    path.unlink()
    with pytest.raises(helper.NativeOpenStackError, match="missing after creation intent"):
        helper.cleanup_manifest(path, env=env, cloud_factory=lambda _: cloud)
    assert set(cloud.security_groups) == {security_group.id}
