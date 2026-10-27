from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.nova_assets import AssetCatalog


def _blob(path: Path, payload: bytes):
    payload = payload.ljust(64, b"\0")
    used = 96 + len(payload)
    header = struct.pack(
        "<5I6H8Q",
        0x73717368,
        1,
        0,
        131072,
        0,
        1,
        17,
        0,
        1,
        4,
        0,
        0,
        used,
        144,
        2**64 - 1,
        96,
        112,
        2**64 - 1,
        2**64 - 1,
    )
    content = header + payload + b"\0" * (((used + 511) // 512) * 512 - used)
    path.write_bytes(content)
    path.chmod(0o400)
    return {"digest": "sha256:" + hashlib.sha256(content).hexdigest(), "size": len(content)}


def _catalog(tmp_path):
    root = tmp_path / "assets"
    root.mkdir(mode=0o700)
    return AssetCatalog(root, "project-one", initialize=True)


def _source(label="a"):
    return {"kind": "nova-build", "base_image_id": "base-one", "recipe_digest": "sha256:" + label * 64}


def test_immutable_assets_contextual_parents_and_zero_consumer_retention(tmp_path):
    catalog = _catalog(tmp_path)
    first, second = _blob(tmp_path / "one", b"one"), _blob(tmp_path / "two", b"two")
    a = catalog.register(
        arch="x86_64",
        source=_source(),
        layers=[first, second],
        blob_paths=[tmp_path / "one", tmp_path / "two"],
        name="torch",
    )
    b = catalog.register(
        arch="x86_64",
        source=_source("b"),
        layers=[second, first],
        blob_paths=[tmp_path / "two", tmp_path / "one"],
        name="tensorflow",
    )
    assert a["descriptor"]["layers"][1]["parent_occurrence"] == a["descriptor"]["layers"][0]["occurrence_id"]
    assert a["descriptor"]["layers"][0]["parent_occurrence"] is None
    assert a["descriptor"]["layers"][1]["digest"] == b["descriptor"]["layers"][0]["digest"]
    assert a["descriptor"]["layers"][1]["occurrence_id"] != b["descriptor"]["layers"][0]["occurrence_id"]
    volume = "12345678-1234-4567-8abc-123456789abc"
    fs = "12345678-1234-4567-8abc-123456789abd"
    catalog.bind_store(a["asset_id"], volume_id=volume, filesystem_uuid=fs)
    binding = catalog.bind_store(b["asset_id"], volume_id=volume, filesystem_uuid=fs)
    assert set(binding["asset_ids"]) == {a["asset_id"], b["asset_id"]}
    before = {p: (p.stat().st_ino, p.read_bytes()) for p in [tmp_path / "one", tmp_path / "two"]}
    catalog.retain_many(a["asset_id"], ["vm-a", "vm-b"], volume_id=volume)
    catalog.release("vm-a")
    catalog.release("vm-b")
    reopened = AssetCatalog(catalog.root, "project-one")
    assert reopened.inspect(verify=True)["consumers"] == {}
    assert {p: (p.stat().st_ino, p.read_bytes()) for p in before} == before
    assert set(reopened.inspect()["assets"]) == {a["asset_id"], b["asset_id"]}


def test_failed_verification_and_name_rebinding_never_publish_success(tmp_path):
    catalog = _catalog(tmp_path)
    blob = _blob(tmp_path / "blob", b"valid")
    record = catalog.register(
        arch="x86_64", source=_source(), layers=[blob], blob_paths=[tmp_path / "blob"], name="immutable"
    )
    before = (catalog.root / "assets.json").read_bytes()
    with pytest.raises(ArtifactValidationError, match="already bound"):
        catalog.register(
            arch="x86_64", source=_source("b"), layers=[blob], blob_paths=[tmp_path / "blob"], name="immutable"
        )
    assert (catalog.root / "assets.json").read_bytes() == before
    path = tmp_path / "blob"
    path.chmod(0o600)
    path.write_bytes(b"corrupt".ljust(blob["size"], b"!"))
    path.chmod(0o400)
    with pytest.raises(ArtifactValidationError, match="hash"):
        catalog.inspect(record["asset_id"], verify=True)
    assert (catalog.root / "assets.json").read_bytes() == before


def test_batch_rebinding_is_atomic_and_mount_loss_never_initializes_fallback(tmp_path):
    catalog = _catalog(tmp_path)
    layer = _blob(tmp_path / "blob", b"valid")
    one = catalog.register(arch="x86_64", source=_source(), layers=[layer], blob_paths=[tmp_path / "blob"])
    two = catalog.register(arch="x86_64", source=_source("b"), layers=[layer], blob_paths=[tmp_path / "blob"])
    volume = "12345678-1234-4567-8abc-123456789abc"
    fs = "12345678-1234-4567-8abc-123456789abd"
    for record in (one, two):
        catalog.bind_store(record["asset_id"], volume_id=volume, filesystem_uuid=fs)
    catalog.retain_many(one["asset_id"], ["occupied"], volume_id=volume)
    with pytest.raises(ArtifactValidationError, match="already bound"):
        catalog.retain_many(two["asset_id"], ["new", "occupied"], volume_id=volume)
    assert set(catalog.inspect()["consumers"]) == {"occupied"}
    catalog.root.rename(tmp_path / "unmounted")
    catalog.root.mkdir(mode=0o700)
    with pytest.raises((ArtifactValidationError, OSError)):
        catalog.inspect()
    with pytest.raises((ArtifactValidationError, OSError)):
        AssetCatalog(catalog.root, "project-one")
    assert list(catalog.root.iterdir()) == []


def test_project_fence_and_catalog_graph_tampering_are_refused(tmp_path):
    catalog = _catalog(tmp_path)
    layer = _blob(tmp_path / "blob", b"valid")
    catalog.register(arch="x86_64", source=_source(), layers=[layer], blob_paths=[tmp_path / "blob"])
    with pytest.raises(ArtifactValidationError, match="project"):
        AssetCatalog(catalog.root, "project-two")
    path = catalog.root / "assets.json"
    wire = json.loads(path.read_bytes())
    next(iter(wire["assets"].values()))["descriptor"]["layers"][0]["parent_occurrence"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(wire))
    path.chmod(0o600)
    with pytest.raises(ArtifactValidationError, match="identity"):
        catalog.inspect()


def test_native_capture_time_is_preserved_without_weakening_normalized_oci_checks(tmp_path):
    from palimpsest_local.nova_assets import verify_blob

    catalog = _catalog(tmp_path)
    path = tmp_path / "native-capture"
    layer = _blob(path, b"installed native package")
    content = bytearray(path.read_bytes())
    struct.pack_into("<I", content, 8, 1790000000)
    path.chmod(0o600)
    path.write_bytes(content)
    path.chmod(0o400)
    layer["digest"] = "sha256:" + hashlib.sha256(content).hexdigest()
    registered = catalog.register(arch="x86_64", source=_source(), layers=[layer], blob_paths=[path])
    assert catalog.inspect(registered["asset_id"], verify=True)["verified"] is True
    assert path.read_bytes() == bytes(content)
    with pytest.raises(ArtifactValidationError, match="deterministic time"):
        verify_blob(path, layer["digest"], layer["size"])


@pytest.mark.parametrize("state_root", [None, "/mnt/owned-scratch/runtime"])
def test_runtime_preparation_emits_v2_without_creating_consumer_state(tmp_path, monkeypatch, state_root):
    from types import SimpleNamespace

    from palimpsest_local import nova_runtime

    store = tmp_path / "store"
    store.mkdir()
    blob = store / ("a" * 64)
    blob.write_bytes(b"verified by catalog")
    inspected = []
    directories = []
    filesystem_uuid = "12345678-1234-4567-8abc-123456789abd"

    class Catalog:
        def inspect(self, identity, *, verify):
            inspected.append((identity, verify))
            return {
                "assets": {
                    identity: {
                        "descriptor": {"arch": "x86_64", "layers": [{"digest": "sha256:" + "a" * 64}]},
                        "blobs": {"sha256:" + "a" * 64: {"path": str(blob)}},
                    }
                }
            }

    monkeypatch.setattr(nova_runtime, "RUNTIME_ROOT", str(store))
    monkeypatch.setattr(nova_runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(nova_runtime, "_directory", directories.append)
    monkeypatch.setattr(
        nova_runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"filesystems": [{"target": str(store), "fstype": "ext4", "uuid": filesystem_uuid}]})
        ),
    )
    before = (blob.stat().st_ino, blob.read_bytes())
    config = {"filesystem_uuid": filesystem_uuid, "stacks": {"native": "asset-one"}}
    if state_root is not None:
        config["consumer_state_root"] = state_root
    result = nova_runtime.prepare_runtime(Catalog(), config)
    assert inspected == [("asset-one", True)]
    assert result["manifest"]["schema"] == "palimpsest.external-layer-runtime.v2"
    assert result["manifest"]["storage_read_only"] is True
    assert result["manifest"]["consumer_state_root"] == (state_root or "/var/lib/palimpsest/consumer")
    assert directories == [store / "layers" / ("a" * 64), store / "runtimes/native"]
    assert (blob.stat().st_ino, blob.read_bytes()) == before
    assert "upperdir=" in result["cloud_init"]
