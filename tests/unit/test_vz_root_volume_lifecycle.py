"""M3-09: explicit instance deletion removes only that instance's upper volume."""

from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest

from palimpsest_local import state
from palimpsest_local.errors import StateError
from palimpsest_local.oci_guest_filesystems import ext4_primary_superblock_checksum
from palimpsest_local.oci_store import ArtifactLeaseOwner
from palimpsest_local.vz_root_volume import claim_vz_root_volume, load_vz_root_volume, release_vz_root_volume

MIB = 1024 * 1024
SIZE = 16 * MIB
GRAPH = "sha256:" + "a" * 64
NONCE = "0" * 64


@pytest.fixture
def roots(tmp_path: Path) -> state.StatePaths:
    return state.init_roots({"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")})


@pytest.fixture
def lima_home(tmp_path: Path) -> Path:
    home = tmp_path / "lima"
    home.mkdir(mode=0o700)
    return home.resolve()


def _prepare(path: Path, *, size_bytes: int, filesystem_uuid: str, label: str, binding: dict) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.truncate(size_bytes)
    sb = bytearray(1024)
    sb[0:4] = (4096).to_bytes(4, "little")
    sb[4:8] = (size_bytes // 4096).to_bytes(4, "little")
    sb[24:28] = (2).to_bytes(4, "little")
    sb[32:36] = (32768).to_bytes(4, "little")
    sb[40:44] = (4096).to_bytes(4, "little")
    sb[56:58] = b"\x53\xef"
    sb[76:80] = (1).to_bytes(4, "little")
    sb[88:90] = (256).to_bytes(2, "little")
    sb[96:100] = (0x42).to_bytes(4, "little")
    sb[100:104] = (0x400).to_bytes(4, "little")
    sb[104:120] = uuid.UUID(filesystem_uuid).bytes
    sb[120:136] = label.encode("ascii").ljust(16, b"\0")
    sb[0x175] = 1
    sb[1020:1024] = ext4_primary_superblock_checksum(bytes(sb)).to_bytes(4, "little")
    with path.open("r+b") as stream:
        stream.seek(1024)
        stream.write(sb)
    return "ssh-ed25519 " + base64.b64encode(b"k" * 32).decode()


def _claim(roots, lima_home, name, *, retention="delete"):
    owner = ArtifactLeaseOwner(str(uuid.uuid4()), name, "root-lower")
    volume_id = str(uuid.uuid4())
    claim_vz_root_volume(
        roots,
        volume_id,
        size_bytes=SIZE,
        lower_graph_digest=GRAPH,
        retention_policy=retention,
        owner=owner,
        lima_home=lima_home,
        root_nonce=NONCE,
        prepare_disk=_prepare,
    )
    return volume_id, owner


def test_delete_removes_only_that_instances_upper_and_preserves_everything_else(roots, lima_home, tmp_path):
    a_id, a_owner = _claim(roots, lima_home, "alpha")
    b_id, b_owner = _claim(roots, lima_home, "beta")
    published_layer = tmp_path / "published-layer.sqsh"
    published_layer.write_bytes(b"published")
    named_volume = lima_home / "_disks" / "named-volume-sentinel"
    named_volume.mkdir(mode=0o700)
    (named_volume / "data").write_bytes(b"named")
    other_instance = lima_home / "plm-other-vm"
    other_instance.mkdir(mode=0o700)

    a_dir = lima_home / "_disks" / ("plm-root-" + a_id.replace("-", ""))
    b_dir = lima_home / "_disks" / ("plm-root-" + b_id.replace("-", ""))
    assert (a_dir / "datadisk").is_file() and (b_dir / "datadisk").is_file()

    result = release_vz_root_volume(
        roots, a_id, owner=a_owner, lower_graph_digest=GRAPH, provider_absent=True, delete=True
    )
    assert result is None
    assert not a_dir.exists()
    assert (b_dir / "datadisk").is_file()
    assert load_vz_root_volume(roots, b_id, owner=b_owner).record.status == "attached"
    assert published_layer.read_bytes() == b"published"
    assert (named_volume / "data").read_bytes() == b"named"
    assert other_instance.is_dir()
    with pytest.raises(StateError):
        load_vz_root_volume(roots, a_id)


def test_uncommitted_upper_is_retained_when_deletion_is_not_requested(roots, lima_home):
    volume_id, owner = _claim(roots, lima_home, "alpha", retention="retain")
    disk = lima_home / "_disks" / ("plm-root-" + volume_id.replace("-", "")) / "datadisk"
    before = disk.read_bytes()
    retained = release_vz_root_volume(roots, volume_id, owner=owner, lower_graph_digest=GRAPH, provider_absent=True)
    assert retained is not None and retained.status == "retained"
    assert disk.read_bytes() == before
    assert load_vz_root_volume(roots, volume_id).record.status == "retained"


def test_release_requires_provider_absence_exact_binding_and_a_clean_directory(roots, lima_home):
    volume_id, owner = _claim(roots, lima_home, "alpha")
    other_owner = ArtifactLeaseOwner(str(uuid.uuid4()), "intruder", "root-lower")
    disk_dir = lima_home / "_disks" / ("plm-root-" + volume_id.replace("-", ""))
    with pytest.raises(StateError):
        release_vz_root_volume(
            roots, volume_id, owner=owner, lower_graph_digest=GRAPH, provider_absent=False, delete=True
        )
    with pytest.raises(StateError):
        release_vz_root_volume(
            roots, volume_id, owner=other_owner, lower_graph_digest=GRAPH, provider_absent=True, delete=True
        )
    (disk_dir / "stray-consumer-lock").write_bytes(b"")
    with pytest.raises(StateError):
        release_vz_root_volume(
            roots, volume_id, owner=owner, lower_graph_digest=GRAPH, provider_absent=True, delete=True
        )
    assert (disk_dir / "datadisk").is_file()
