from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from palimpsest_local.oci_commit import CommitError, canonical_json, gzip_layer
from palimpsest_local.vz_commit import capture_to_layout

_MEDIA = "application/vnd.oci.image.layer.v1.tar+gzip"


def _tool(name):
    found = shutil.which(name)
    if found:
        return Path(found)
    for base in ("/opt/homebrew/opt/e2fsprogs/sbin", "/usr/sbin", "/sbin"):
        if Path(base, name).exists():
            return Path(base, name)
    return None


MKE2FS, DEBUGFS = _tool("mke2fs"), _tool("debugfs")
pytestmark = pytest.mark.skipif(MKE2FS is None or DEBUGFS is None, reason="e2fsprogs is not installed")
SIZE = 16 * 1024 * 1024
BINDING = {
    "schema": "palimpsest.vz-upper-binding.v1",
    "run_id": "437d0eb7-99ec-4756-9f00-68d8025f8830",
    "root_volume_id": "871e755d-6fb1-4990-8d61-f8d756f77058",
    "root_graph_digest": "sha256:" + "ab" * 32,
    "root_nonce": "b3" * 32,
}
EXPECTED = {key: BINDING[key] for key in ("run_id", "root_volume_id", "root_graph_digest", "root_nonce")}


def _tar(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _parent(tmp_path):
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
    digest = "sha256:" + hashlib.sha256(canonical_json(manifest)).hexdigest()
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {"Cmd": ["./probe"], "User": "1000:1000", "WorkingDir": "/opt/x"},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [{"created_by": "base"}, {"created_by": "app"}],
    }
    return manifest, config, blobs, digest


@pytest.fixture
def disk(tmp_path):
    tree = tmp_path / "tree"
    upper = tree / "upper"
    (upper / "opt" / "app").mkdir(parents=True)
    (upper / "opt" / "app" / "new.txt").write_bytes(b"committed change\n")
    (upper / "etc").mkdir()
    (upper / "etc" / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (tree / "work").mkdir()
    (tree / "platform-state").mkdir()
    (tree / "platform-state" / "binding.json").write_text(json.dumps({**BINDING, "phase": "initialized"}))
    image = tmp_path / "vol" / "datadisk"
    image.parent.mkdir()
    with image.open("wb") as handle:
        handle.truncate(SIZE)
    subprocess.run([str(MKE2FS), "-q", "-t", "ext4", "-d", str(tree), str(image)], check=True, capture_output=True)
    os.chmod(image, 0o600)
    return image


def copy_clone(source, destination):
    shutil.copyfile(source, destination)


def run(tmp_path, disk, *, state="stopped", hold=False, destination=None, options=None, binding=None):
    manifest, config, blobs, digest = _parent(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return capture_to_layout(
        disk=disk,
        disk_size=SIZE,
        instance_state=state,
        source_hold=hold,
        scratch_parent=scratch,
        debugfs=DEBUGFS,
        destination=destination or tmp_path / "out" / "layout",
        parent_manifest=manifest,
        parent_config=config,
        parent_blob=lambda d: blobs[d],
        parent_manifest_digest=digest,
        created="2026-11-09T00:00:00Z",
        clone=copy_clone,
        capture_options=options,
        expected_binding=binding or EXPECTED,
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stopped_instance_becomes_a_new_layer_on_the_parent_chain_without_touching_the_source(tmp_path, disk):
    before = sha(disk)
    (tmp_path / "out").mkdir()
    committed, receipt = run(tmp_path, disk, options={"generated_placeholders": ["etc/machine-id"]})
    assert sha(disk) == before
    assert list((tmp_path / "scratch").iterdir()) == []
    layout = tmp_path / "out" / "layout"
    manifest = json.loads((layout / "blobs" / "sha256" / committed.manifest_digest.split(":")[1]).read_text())
    assert len(manifest["layers"]) == 3
    assert [item["digest"] for item in manifest["layers"][:2]] == [
        item["digest"] for item in _parent(tmp_path)[0]["layers"]
    ]
    new_layer = layout / "blobs" / "sha256" / manifest["layers"][2]["digest"].split(":")[1]
    with tarfile.open(new_layer, mode="r:gz") as tar:
        names = tar.getnames()
        assert tar.extractfile("opt/app/new.txt").read() == b"committed change\n"
    assert "etc/machine-id" not in names, "a recorded generated placeholder must not be inherited"
    assert receipt.entries > 0


def test_without_recorded_provenance_an_identity_file_is_kept_so_the_runtime_must_supply_it(tmp_path, disk):
    (tmp_path / "out").mkdir()
    committed, _ = run(tmp_path, disk)
    layout = tmp_path / "out" / "layout"
    manifest = json.loads((layout / "blobs" / "sha256" / committed.manifest_digest.split(":")[1]).read_text())
    with tarfile.open(layout / "blobs" / "sha256" / manifest["layers"][2]["digest"].split(":")[1], mode="r:gz") as tar:
        assert "etc/machine-id" in tar.getnames()


@pytest.mark.parametrize("state", ["running", "starting", "stopping", "unknown", ""])
def test_a_not_stopped_instance_is_refused_before_the_disk_is_touched(tmp_path, disk, state):
    before = os.lstat(disk)
    with pytest.raises(CommitError) as raised:
        run(tmp_path, disk, state=state)
    assert raised.value.code == "instance-not-stopped"
    assert (os.lstat(disk).st_ino, os.lstat(disk).st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not (tmp_path / "scratch").exists() or list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "out" / "layout").exists()


def test_a_held_instance_is_refused(tmp_path, disk):
    with pytest.raises(CommitError) as raised:
        run(tmp_path, disk, hold=True)
    assert raised.value.code == "source-hold"


def test_an_existing_destination_fails_the_capture_and_leaves_everything_in_place(tmp_path, disk):
    destination = tmp_path / "out" / "layout"
    destination.mkdir(parents=True)
    (destination / "keep").write_text("x")
    before = sha(disk)
    with pytest.raises(CommitError):
        run(tmp_path, disk, destination=destination)
    assert (destination / "keep").read_text() == "x" and sha(disk) == before
    assert list((tmp_path / "scratch").iterdir()) == []


E2FSCK = _tool("e2fsck")


def mark_journal_dirty(disk):
    subprocess.run([str(DEBUGFS), "-w", "-R", "feature needs_recovery", str(disk)], check=True, capture_output=True)


def test_a_dirty_journal_is_refused_by_default_and_the_source_is_untouched(tmp_path, disk):
    mark_journal_dirty(disk)
    before = sha(disk)
    with pytest.raises(CommitError) as raised:
        run(tmp_path, disk)
    assert raised.value.code == "journal-dirty"
    assert sha(disk) == before and list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "out" / "layout").exists()


@pytest.mark.skipif(E2FSCK is None, reason="e2fsck is not installed")
def test_a_dirty_journal_is_recovered_on_the_private_clone_only(tmp_path, disk):
    mark_journal_dirty(disk)
    before = sha(disk)
    (tmp_path / "out").mkdir()
    (tmp_path / "scratch").mkdir()
    manifest, config, blobs, digest = _parent(tmp_path)
    committed, _ = capture_to_layout(
        disk=disk,
        disk_size=SIZE,
        instance_state="stopped",
        source_hold=False,
        scratch_parent=tmp_path / "scratch",
        debugfs=DEBUGFS,
        destination=tmp_path / "out" / "layout",
        parent_manifest=manifest,
        parent_config=config,
        parent_blob=lambda d: blobs[d],
        parent_manifest_digest=digest,
        created="2026-11-09T00:00:00Z",
        clone=copy_clone,
        e2fsck=E2FSCK,
        expected_binding=EXPECTED,
    )
    assert committed.manifest_digest.startswith("sha256:")
    assert sha(disk) == before, "recovery must never modify the source disk"
    assert list((tmp_path / "scratch").iterdir()) == []


def test_read_upper_itself_refuses_a_dirty_journal(tmp_path, disk):
    from palimpsest_local.ext4_upper import JournalNotClean, read_upper

    mark_journal_dirty(disk)
    with pytest.raises(JournalNotClean):
        read_upper(disk, DEBUGFS)


def rewrite_binding(disk, tmp_path, **changes):
    blob = tmp_path / "binding.new"
    blob.write_text(json.dumps({**BINDING, "phase": "initialized", **changes}))
    script = tmp_path / "binding.cmd"
    script.write_text(f"rm /platform-state/binding.json\nwrite {blob} /platform-state/binding.json\n")
    subprocess.run([str(DEBUGFS), "-w", "-f", str(script), str(disk)], check=True, capture_output=True)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"phase": "prepared"}, "binding-phase"),
        ({"run_id": "00000000-0000-4000-8000-000000000000"}, "binding-mismatch"),
        ({"root_volume_id": "00000000-0000-4000-8000-000000000000"}, "binding-mismatch"),
        ({"root_nonce": "00" * 32}, "binding-mismatch"),
        ({"root_graph_digest": "sha256:" + "00" * 32}, "binding-mismatch"),
    ],
)
def test_a_disk_that_is_unbooted_or_belongs_to_another_run_is_refused(tmp_path, disk, changes, code):
    rewrite_binding(disk, tmp_path, **changes)
    before = sha(disk)
    with pytest.raises(CommitError) as raised:
        run(tmp_path, disk)
    assert raised.value.code == code and sha(disk) == before
    assert list((tmp_path / "scratch").iterdir()) == [] and not (tmp_path / "out" / "layout").exists()


def test_a_missing_binding_record_fails_closed(tmp_path, disk):
    subprocess.run(
        [str(DEBUGFS), "-w", "-R", "rm /platform-state/binding.json", str(disk)], check=True, capture_output=True
    )
    from palimpsest_local.oci_capture import CaptureError

    with pytest.raises(CaptureError):
        run(tmp_path, disk)


def test_the_expected_binding_must_name_exactly_run_volume_graph_and_nonce(tmp_path, disk):
    with pytest.raises(CommitError) as raised:
        run(tmp_path, disk, binding={"run_id": EXPECTED["run_id"]})
    assert raised.value.code == "binding-expected"


def _real_parent(tmp_path):
    """A parent whose manifest names the real digest of its config bytes."""
    from contextlib import contextmanager

    layers, diff_ids, streams = [], [], {}
    for files in ({"a": b"1"}, {"b": b"2"}):
        blob, layer = gzip_layer(_tar(files))
        layers.append({"mediaType": _MEDIA, "digest": layer.digest, "size": layer.size})
        diff_ids.append(layer.diff_id)

        @contextmanager
        def opener(data=blob):
            yield iter([data[:5], data[5:]])

        streams[layer.digest] = opener
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {"Cmd": ["./probe"]},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [{"created_by": "base"}, {"created_by": "app"}],
    }
    config_bytes = canonical_json(config)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": layers,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": "sha256:" + hashlib.sha256(config_bytes).hexdigest(),
            "size": len(config_bytes),
        },
    }
    manifest_bytes = canonical_json(manifest)
    return manifest_bytes, config_bytes, "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(), streams


def commit(tmp_path, disk, *, parent=None, **overrides):
    from palimpsest_local.vz_commit import commit_stopped_disk

    manifest_bytes, config_bytes, digest, streams = parent or _real_parent(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    arguments = {
        "disk": disk,
        "disk_size": SIZE,
        "instance_state": "stopped",
        "source_hold": False,
        "expected_binding": EXPECTED,
        "scratch_parent": scratch,
        "debugfs": DEBUGFS,
        "destination": tmp_path / "out" / "layout",
        "parent_manifest_bytes": manifest_bytes,
        "parent_config_bytes": config_bytes,
        "parent_manifest_digest": digest,
        "base_image_digest": "sha256:" + "9" * 64,
        "layer_streams": streams,
        "created": "2026-11-09T00:00:00Z",
        "clone": copy_clone,
    }
    arguments.update(overrides)
    return commit_stopped_disk(**arguments)


def test_commit_stopped_disk_returns_the_receipt_fields_and_cleans_every_scratch(tmp_path, disk):
    (tmp_path / "out").mkdir()
    before = sha(disk)
    parent = _real_parent(tmp_path)
    receipt = commit(tmp_path, disk, parent=parent, generated_placeholders=["etc/machine-id"])
    assert set(receipt) == {"digest", "size_bytes", "parent_digest", "base_image_digest"}
    assert receipt["parent_digest"] == parent[2] and receipt["base_image_digest"] == "sha256:" + "9" * 64
    assert receipt["digest"].startswith("sha256:") and receipt["size_bytes"] > 0
    assert sha(disk) == before and list((tmp_path / "scratch").iterdir()) == []
    layout = tmp_path / "out" / "layout"
    manifest = json.loads((layout / "blobs" / "sha256" / receipt["digest"].split(":")[1]).read_text())
    config = json.loads((layout / "blobs" / "sha256" / manifest["config"]["digest"].split(":")[1]).read_text())
    assert len(manifest["layers"]) == 3 and len(config["rootfs"]["diff_ids"]) == 3
    assert config["config"]["Cmd"] == ["./probe"], "the run configuration must be inherited"
    with tarfile.open(layout / "blobs" / "sha256" / manifest["layers"][2]["digest"].split(":")[1], mode="r:gz") as tar:
        assert "etc/machine-id" not in tar.getnames()


def test_a_swapped_parent_manifest_or_config_is_refused_before_any_capture(tmp_path, disk):
    manifest_bytes, config_bytes, digest, streams = _real_parent(tmp_path)
    for overrides in (
        {"parent_manifest_bytes": manifest_bytes + b" "},
        {"parent_config_bytes": config_bytes + b" "},
        {"parent_manifest_digest": "sha256:" + "0" * 64},
    ):
        with pytest.raises(CommitError) as raised:
            commit(tmp_path, disk, parent=(manifest_bytes, config_bytes, digest, streams), **overrides)
        assert raised.value.code == "commit-parent"
    assert not (tmp_path / "scratch").exists() or list((tmp_path / "scratch").iterdir()) == []
    assert not (tmp_path / "out" / "layout").exists()


def test_a_corrupt_or_missing_parent_layer_stream_fails_and_leaves_no_trace(tmp_path, disk):
    from contextlib import contextmanager

    manifest_bytes, config_bytes, digest, streams = _real_parent(tmp_path)
    first = next(iter(streams))

    @contextmanager
    def corrupt():
        yield iter([b"not the layer"])

    for broken in ({**streams, first: corrupt}, {key: value for key, value in streams.items() if key != first}):
        with pytest.raises(CommitError) as raised:
            commit(tmp_path, disk, parent=(manifest_bytes, config_bytes, digest, broken))
        assert raised.value.code == "commit-parent"
        assert list((tmp_path / "scratch").iterdir()) == [] and not (tmp_path / "out" / "layout").exists()


def test_commit_stopped_disk_refuses_a_running_instance_before_reading_the_parent(tmp_path, disk):
    with pytest.raises(CommitError) as raised:
        commit(tmp_path, disk, instance_state="running", parent=(b"not json", b"x", "sha256:" + "0" * 64, {}))
    assert raised.value.code == "instance-not-stopped"


def test_the_committed_layout_is_accepted_by_the_run_source_path_with_the_parent_chain(tmp_path, disk):
    from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS

    (tmp_path / "out").mkdir()
    parent = _real_parent(tmp_path)
    receipt = commit(tmp_path, disk, parent=parent, generated_placeholders=["etc/machine-id"])
    layout = (tmp_path / "out" / "layout").resolve()
    snapshot = LocalLayoutSource(layout=layout, root_digest=None).snapshot_acquisition(
        None, SourceCAS(tmp_path / "cas"), platform="linux/arm64"
    )
    assert snapshot.manifest.descriptor.digest == receipt["digest"]
    assert [item.descriptor.digest for item in snapshot.layers[:2]] == list(parent[3])
    assert len(snapshot.layers) == 3 and snapshot.image.config.architecture == "arm64"
