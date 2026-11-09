from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from palimpsest_local.oci_capture import UpperEntry, UpperKind, convert_upper_to_layer
from palimpsest_local.oci_commit import (
    PARENT_ANNOTATION,
    CommitError,
    GzipTee,
    build_committed_image,
    canonical_json,
    gzip_layer,
    write_commit_layout,
)

_MEDIA = "application/vnd.oci.image.layer.v1.tar+gzip"


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _parent(tmp_path: Path):
    layers, diff_ids, blobs = [], [], {}
    for index, files in enumerate(({"a": b"1"}, {"b": b"2"})):
        blob, layer = gzip_layer(_tar(files))
        layers.append({"mediaType": _MEDIA, "digest": layer.digest, "size": layer.size})
        diff_ids.append(layer.diff_id)
        path = tmp_path / f"parent-{index}"
        path.write_bytes(blob)
        blobs[layer.digest] = path
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": layers,
        "config": {"mediaType": "x", "digest": "sha256:" + "c" * 64, "size": 1},
    }
    manifest_bytes = canonical_json(manifest)
    manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {"Env": ["A=1"], "Cmd": ["./probe"], "User": "1000:1000", "WorkingDir": "/opt/x"},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [{"created_by": "base"}, {"created_by": "app"}],
    }
    return manifest, config, blobs, manifest_digest


def test_gzip_tee_streams_and_diff_id_is_the_stable_identity():
    tar = b"tar-bytes" * 100000
    sink = io.BytesIO()
    tee = GzipTee(sink)
    for offset in range(0, len(tar), 4096):
        tee.write(tar[offset : offset + 4096])
    layer = tee.finish()
    assert gzip.decompress(sink.getvalue()) == tar
    assert layer.diff_id == "sha256:" + hashlib.sha256(tar).hexdigest()
    assert layer.digest == "sha256:" + hashlib.sha256(sink.getvalue()).hexdigest()
    assert layer.size == len(sink.getvalue())
    with pytest.raises(CommitError):
        tee.write(b"late")


def test_commit_appends_one_layer_keeps_order_and_inherits_runtime_config(tmp_path):
    manifest, config, _, parent_digest = _parent(tmp_path)
    _, layer = gzip_layer(b"new-layer")
    image = build_committed_image(
        manifest, config, layer, created="2026-11-08T00:00:00Z", parent_manifest_digest=parent_digest
    )
    new_manifest = json.loads(image.manifest)
    new_config = json.loads(image.config)
    assert [i["digest"] for i in new_manifest["layers"]][:2] == [i["digest"] for i in manifest["layers"]]
    assert new_manifest["layers"][-1]["digest"] == layer.digest
    assert new_config["rootfs"]["diff_ids"][-1] == layer.diff_id
    assert new_config["config"] == config["config"]
    assert new_config["architecture"] == "arm64"
    assert len(new_config["history"]) == 3
    assert new_manifest["annotations"][PARENT_ANNOTATION] == parent_digest
    assert len(manifest["layers"]) == 2 and len(config["rootfs"]["diff_ids"]) == 2


def test_commit_rejects_bad_parents_and_duplicate_layers(tmp_path):
    manifest, config, _, parent_digest = _parent(tmp_path)
    _, layer = gzip_layer(b"x")
    broken = {**config, "rootfs": {"type": "layers", "diff_ids": config["rootfs"]["diff_ids"][:1]}}
    with pytest.raises(CommitError) as err:
        build_committed_image(manifest, broken, layer, created="t", parent_manifest_digest=parent_digest)
    assert err.value.code == "commit-parent"
    with pytest.raises(CommitError) as err:
        build_committed_image(
            {**manifest, "layers": []}, config, layer, created="t", parent_manifest_digest=parent_digest
        )
    assert err.value.code == "commit-parent"
    dup = {**manifest, "layers": [{**manifest["layers"][0], "digest": layer.digest}, manifest["layers"][1]]}
    with pytest.raises(CommitError) as err:
        build_committed_image(dup, config, layer, created="t", parent_manifest_digest=parent_digest)
    assert err.value.code == "commit-layer"


def test_parent_manifest_digest_required_and_never_silently_reserialized(tmp_path):
    manifest, config, _, parent_digest = _parent(tmp_path)
    _, layer = gzip_layer(b"new-layer")

    # Missing/None parent manifest digest is rejected
    with pytest.raises(CommitError) as err:
        build_committed_image(manifest, config, layer, created="2026-11-08T00:00:00Z", parent_manifest_digest=None)
    assert err.value.code == "commit-parent"

    # Malformed digest is rejected
    with pytest.raises(CommitError) as err:
        build_committed_image(
            manifest, config, layer, created="2026-11-08T00:00:00Z", parent_manifest_digest="sha256:bad"
        )
    assert err.value.code == "commit-parent"

    # Verified digest is faithfully preserved
    image = build_committed_image(
        manifest, config, layer, created="2026-11-08T00:00:00Z", parent_manifest_digest=parent_digest
    )
    result_manifest = json.loads(image.manifest)
    assert result_manifest["annotations"][PARENT_ANNOTATION] == parent_digest


def test_layout_is_accepted_by_the_existing_local_layout_source(tmp_path):
    from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS

    manifest, config, blobs, parent_digest = _parent(tmp_path)
    entries = [
        UpperEntry("opt/app/new.txt", UpperKind.FILE, size=5, payload=b"hello"),
        UpperEntry("etc/ssh/ssh_host_rsa_key", UpperKind.FILE, size=1, payload=b"k"),
    ]
    layer_path = tmp_path / "layer.gz"
    with layer_path.open("wb") as handle:
        tee = GzipTee(handle)
        from contextlib import contextmanager

        @contextmanager
        def opener(payload):
            yield io.BytesIO(payload)

        convert_upper_to_layer(entries, tee, opener)
        layer = tee.finish()
    spare = tmp_path / "layer-spare.gz"
    spare.write_bytes(layer_path.read_bytes())
    dest = tmp_path / "out" / "layout"
    dest.parent.mkdir()
    image = write_commit_layout(
        dest,
        parent_manifest=manifest,
        parent_config=config,
        parent_blob=lambda digest: blobs[digest],
        layer_blob=layer_path,
        layer=layer,
        created="2026-11-08T00:00:00Z",
        parent_manifest_digest=parent_digest,
    )
    for item in json.loads(image.manifest)["layers"]:
        assert (dest / "blobs" / "sha256" / item["digest"].split(":")[1]).is_file()
    cas_root = tmp_path / "cas"
    snapshot = LocalLayoutSource(layout=dest.resolve(), root_digest=None).snapshot_acquisition(
        None, SourceCAS(cas_root), platform="linux/arm64"
    )
    assert snapshot.manifest.descriptor.digest == image.manifest_digest
    assert len(snapshot.layers) == 3
    assert snapshot.layers[-1].descriptor.digest == layer.digest
    assert snapshot.image.layers[-1].diff_id == layer.diff_id
    with pytest.raises(CommitError):
        write_commit_layout(
            dest,
            parent_manifest=manifest,
            parent_config=config,
            parent_blob=lambda digest: blobs[digest],
            layer_blob=spare,
            layer=layer,
            created="t",
            parent_manifest_digest=parent_digest,
        )
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    with pytest.raises(CommitError):
        write_commit_layout(
            wrong / "layout",
            parent_manifest=manifest,
            parent_config=config,
            parent_blob=lambda digest: spare,
            layer_blob=spare,
            layer=layer,
            created="t",
            parent_manifest_digest=parent_digest,
        )
    assert not (wrong / "layout").exists()


def test_failure_preserves_input_layer_blob(tmp_path):
    manifest, config, _, parent_digest = _parent(tmp_path)
    layer_path = tmp_path / "layer.tar.gz"
    with layer_path.open("wb") as handle:
        tee = GzipTee(handle)
        tee.write(b"payload-bytes" * 100)
        layer = tee.finish()
    original_bytes = layer_path.read_bytes()

    dest = tmp_path / "out" / "layout-fail"
    with pytest.raises(CommitError) as err:
        write_commit_layout(
            dest,
            parent_manifest=manifest,
            parent_config=config,
            parent_blob=lambda digest: tmp_path / "nonexistent-blob",
            layer_blob=layer_path,
            layer=layer,
            created="2026-11-08T00:00:00Z",
            parent_manifest_digest=parent_digest,
        )
    assert err.value.code == "commit-parent"
    # Staging layout was removed
    assert not dest.exists()
    # Source layer blob was NOT destroyed or renamed
    assert layer_path.is_file()
    assert layer_path.read_bytes() == original_bytes


def test_missing_parent_blob_raises_commit_error_rather_than_filenotfound(tmp_path):
    manifest, config, blobs, parent_digest = _parent(tmp_path)
    layer_path = tmp_path / "layer-missing-parent.tar.gz"
    with layer_path.open("wb") as handle:
        layer = GzipTee(handle).finish()
    original_bytes = layer_path.read_bytes()

    # Blobs contains layer 0, but layer 1 is missing on disk
    first_digest = manifest["layers"][0]["digest"]
    partial_blobs = {first_digest: blobs[first_digest]}

    dest = tmp_path / "layout-missing-parent"
    with pytest.raises(CommitError) as err:
        write_commit_layout(
            dest,
            parent_manifest=manifest,
            parent_config=config,
            parent_blob=lambda digest: partial_blobs.get(digest, tmp_path / "missing-layer-blob"),
            layer_blob=layer_path,
            layer=layer,
            created="2026-11-08T00:00:00Z",
            parent_manifest_digest=parent_digest,
        )
    assert err.value.code == "commit-parent"
    assert "missing" in str(err.value).lower()
    # Staging cleaned up, original input layer preserved
    assert not dest.exists()
    assert layer_path.read_bytes() == original_bytes


def test_malicious_parent_descriptor_rejected_before_any_blob_access(tmp_path):
    manifest, config, blobs, parent_digest = _parent(tmp_path)
    bad_manifest = dict(manifest)
    # Valid layers 0 and 1 exist in blobs, but layer 2 contains malicious path traversal
    bad_manifest["layers"] = [
        *manifest["layers"],
        {"mediaType": _MEDIA, "digest": "sha256:../../etc/passwd", "size": 10},
    ]
    bad_config = dict(config)
    bad_config["rootfs"] = {"type": "layers", "diff_ids": [*config["rootfs"]["diff_ids"], "sha256:" + "f" * 64]}

    layer_path = tmp_path / "layer-trav.tar.gz"
    with layer_path.open("wb") as handle:
        layer = GzipTee(handle).finish()
    original_bytes = layer_path.read_bytes()

    accessed_blobs: list[str] = []

    def tracking_parent_blob(digest: str) -> Path:
        accessed_blobs.append(digest)
        return blobs.get(digest, tmp_path / "nonexistent-blob")

    dest = tmp_path / "layout-trav"
    with pytest.raises(CommitError) as error:
        write_commit_layout(
            dest,
            parent_manifest=bad_manifest,
            parent_config=bad_config,
            parent_blob=tracking_parent_blob,
            layer_blob=layer_path,
            layer=layer,
            created="2026-11-08T00:00:00Z",
            parent_manifest_digest=parent_digest,
        )
    assert error.value.code == "commit-parent"
    # CRITICAL INVARIANT: No blobs accessed because all descriptors validated upfront
    assert len(accessed_blobs) == 0
    assert not dest.exists()
    assert layer_path.read_bytes() == original_bytes


def test_no_destination_overwrite_on_race(tmp_path):
    manifest, config, blobs, parent_digest = _parent(tmp_path)
    layer_path = tmp_path / "layer-race.tar.gz"
    with layer_path.open("wb") as handle:
        layer = GzipTee(handle).finish()

    dest = tmp_path / "layout-dest"
    dest.mkdir()
    sentinel = dest / "pre-existing.txt"
    sentinel.write_text("must-not-be-overwritten")

    with pytest.raises(CommitError) as err:
        write_commit_layout(
            dest,
            parent_manifest=manifest,
            parent_config=config,
            parent_blob=lambda digest: blobs[digest],
            layer_blob=layer_path,
            layer=layer,
            created="2026-11-08T00:00:00Z",
            parent_manifest_digest=parent_digest,
        )
    assert err.value.code == "commit-layout"
    # Destination content was preserved intact
    assert dest.exists()
    assert sentinel.read_text() == "must-not-be-overwritten"


@pytest.mark.parametrize("state", ["running", "starting", "stopping", "unknown", "", "STOPPED"])
def test_capture_admission_refuses_everything_but_an_exactly_stopped_instance(state):
    from palimpsest_local.oci_commit import admit_capture

    with pytest.raises(CommitError) as raised:
        admit_capture(instance_state=state, source_hold=False)
    assert "stop" in str(raised.value)


def test_capture_admission_refuses_a_held_source_and_admits_a_clean_stopped_one():
    from palimpsest_local.oci_commit import admit_capture

    with pytest.raises(CommitError):
        admit_capture(instance_state="stopped", source_hold=True)
    assert admit_capture(instance_state="stopped", source_hold=False) is None
