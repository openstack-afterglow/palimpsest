"""Behavioral coverage of the permanent shared-source cache contract."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from palimpsest_local import cli, layer_cache, registry_intake
from palimpsest_local.artifact_store import ArtifactStore
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.layer_cache import LayerCache, LayerCacheError
from palimpsest_local.oci_packer import SquashFSPackProfile
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
)
from palimpsest_local.oci_source import SourceCAS
from palimpsest_local.oci_store import OCIStore


def _sha(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _layout(
    root: Path,
    *,
    command: str = "first",
    repeat: bool = False,
    payload: bytes = b"retained payload\n",
    gzip_mtime: int = 0,
) -> tuple[Path, str, str]:
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(payload))
    uncompressed = raw_tar.getvalue()
    compressed = gzip.compress(uncompressed, mtime=gzip_mtime)

    def add(value, media_type):
        payload = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        digest = _sha(payload)
        (blobs / digest.split(":")[1]).write_bytes(payload)
        return {"mediaType": media_type, "digest": digest, "size": len(payload)}

    layer = add(compressed, OCI_LAYER_GZIP_MEDIA_TYPE)
    count = 2 if repeat else 1
    config = add(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Cmd": ["/bin/echo", command]},
            "rootfs": {"type": "layers", "diff_ids": [_sha(uncompressed)] * count},
        },
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    manifest = add(
        {"schemaVersion": 2, "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE, "config": config, "layers": [layer] * count},
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (root / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE, "manifests": [manifest]})
    )
    return root, manifest["digest"], layer["digest"]


def _cache(root: Path) -> LayerCache:
    root.mkdir(mode=0o700)
    return LayerCache(root, initialize=True)


def _bytes(cache: LayerCache) -> dict[str, bytes]:
    paths = [
        *cache.roots.oci_source_cas.joinpath("blobs/sha256").iterdir(),
        *cache.roots.store.joinpath("blobs/sha256").iterdir(),
    ]
    return {os.fspath(path.relative_to(cache.root)): path.read_bytes() for path in paths}


def _retain_process(root: str, digest: str, consumer: str, queue) -> None:
    try:
        cache = LayerCache(Path(root))
        cache.retain(digest, consumer)
        queue.put(None)
    except BaseException as exc:
        queue.put(repr(exc))


def test_import_reopen_consumers_and_zero_consumer_bytes_persist(tmp_path):
    source, root, compressed = _layout(tmp_path / "layout", repeat=True)
    cache = _cache(tmp_path / "cache")
    imported = cache.import_source(source)
    assert imported["root_digest"] == root
    assert imported["image"]["layers"][0]["compressed"]["digest"] == compressed
    assert len(imported["image"]["layers"]) == 2
    before = _bytes(cache)
    assert len(before) == 3  # manifest, config, one shared compressed payload
    cache.retain(root, "vm-one")
    marker = cache.roots.state / "layer-cache.json"
    first_catalog = marker.read_bytes()
    cache.retain(root, "vm-one")
    assert marker.read_bytes() == first_catalog
    reopened = LayerCache(cache.root)
    assert reopened.inspect()["cache_id"] == imported["cache_id"]
    assert reopened.retain(root, "vm-two")["consumer_ids"] == ["vm-one", "vm-two"]
    reopened.release("vm-one")
    assert LayerCache(cache.root).inspect(root)["consumer_ids"] == ["vm-two"]
    reopened.release("vm-two")
    assert reopened.inspect(root)["consumer_ids"] == []
    assert reopened.release("vm-two")["released"] is False
    assert _bytes(reopened) == before
    wire = json.loads(marker.read_bytes())
    assert "source-cas-v1:" not in json.dumps(wire)
    assert "oci-store-v1:" not in json.dumps(wire)


def test_distinct_roots_share_compressed_bytes_and_refuse_consumer_rebinding(tmp_path):
    one, first, compressed = _layout(tmp_path / "one")
    two, second, same = _layout(tmp_path / "two", command="second")
    cache = _cache(tmp_path / "cache")
    cache.import_source(one)
    cache.import_source(two)
    assert compressed == same and first != second
    cache.retain(first, "instance-a")
    cache.retain(second, "instance-b")
    with pytest.raises(LayerCacheError, match="already bound"):
        cache.retain(second, "instance-a")
    view = cache.inspect()
    assert view["consumers"] == {"instance-a": first, "instance-b": second}
    assert len(_bytes(cache)) == 5
    cache.release("instance-a")
    cache.release("instance-b")
    assert len(_bytes(cache)) == 5


def test_archive_import_populates_the_same_source_cas(tmp_path):
    source, root, _compressed = _layout(tmp_path / "layout")
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as stream:
        for path in source.rglob("*"):
            if path.is_file():
                stream.add(path, arcname=path.relative_to(source), recursive=False)
    cache = _cache(tmp_path / "cache")
    assert cache.import_source(archive, manifest_digest=root)["root_digest"] == root
    before = _bytes(cache)
    assert cache.import_source(source)["root_digest"] == root
    assert _bytes(cache) == before


@pytest.mark.parametrize("identifier", ["", "../vm", "vm/name", "vm name", "x" * 129, "vm\nname"])
def test_invalid_consumer_cannot_mutate_catalog(tmp_path, identifier):
    source, root, _compressed = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    before = (cache.roots.state / "layer-cache.json").read_bytes()
    with pytest.raises(LayerCacheError):
        cache.retain(root, identifier)
    with pytest.raises(LayerCacheError):
        cache.release(identifier)
    assert (cache.roots.state / "layer-cache.json").read_bytes() == before


def test_immutable_pin_and_explicit_offline_never_call_skopeo(tmp_path, monkeypatch):
    source, root, _compressed = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline or immutable hit must not use a registry helper")

    monkeypatch.setattr(registry_intake, "pull_anonymous_oci_layout", forbidden)
    for offline in (False, True):
        assert cache.pull(f"registry.example.com/team/image@{root}", offline=offline)["root_digest"] == root
    with pytest.raises(LayerCacheError, match="offline"):
        cache.pull("registry.example.com/team/image:uncached", offline=True)


@pytest.mark.parametrize("offline", [False, True])
def test_corrupt_immutable_reference_mapping_refuses_other_cached_root(tmp_path, monkeypatch, offline):
    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    absent = "sha256:" + "a" * 64
    assert absent != root
    reference = f"registry.example.com/team/image@{absent}"
    marker = cache.roots.state / "layer-cache.json"
    catalog = json.loads(marker.read_bytes())
    catalog["references"][reference] = root
    marker.write_text(json.dumps(catalog))

    def forbidden(*args, **kwargs):
        raise AssertionError("corrupt immutable mapping must refuse before registry access")

    monkeypatch.setattr(registry_intake, "pull_anonymous_oci_layout", forbidden)
    with pytest.raises(LayerCacheError, match="immutable reference"):
        cache.pull(reference, offline=offline)
    with pytest.raises(LayerCacheError, match="immutable reference"):
        LayerCache(cache.root)


def test_mutable_tag_refresh_keeps_old_root_and_offline_reopens_saved_descriptor(tmp_path, monkeypatch):
    one, first, _ = _layout(tmp_path / "one")
    two, second, _ = _layout(tmp_path / "two", command="second")
    cache = _cache(tmp_path / "cache")
    queue = [one, two]
    calls = []

    def transfer(reference, roots, **kwargs):
        # Replace only transport behavior: real LocalLayoutSource/SourceCAS import
        # and cache verification remain in use, including overwritten index.json.
        from palimpsest_local.oci_source import LocalLayoutSource

        snapshot = LocalLayoutSource(queue.pop(0)).snapshot(None, SourceCAS(roots.oci_source_cas))
        entry = {**snapshot.root.descriptor.to_dict(), "annotations": {"org.opencontainers.image.ref.name": "selected"}}
        index = roots.oci_source_cas / "index.json"
        index.write_text(json.dumps({"schemaVersion": 2, "manifests": [entry]}))
        index.chmod(0o600)
        calls.append(reference)
        return registry_intake.RegistryPullReceipt(
            reference,
            roots.oci_source_cas,
            "linux/amd64",
            snapshot.root.descriptor.digest,
            snapshot.manifest.descriptor.digest,
            snapshot.cas_id,
            snapshot.binding_digest,
        )

    monkeypatch.setattr(registry_intake, "pull_anonymous_oci_layout", transfer)
    reference = "registry.example.com/team/image:stable"
    assert cache.pull(reference)["root_digest"] == first
    cache.retain(first, "older-instance")
    assert cache.pull(reference)["root_digest"] == second
    assert len(calls) == 2
    assert LayerCache(cache.root).pull(reference, offline=True)["root_digest"] == second
    assert cache.inspect(first)["consumer_ids"] == ["older-instance"]
    assert len(calls) == 2


@pytest.mark.parametrize("fault", ["missing", "malformed", "duplicate", "symlink", "hardlink", "public"])
def test_catalog_corruption_is_refused_without_repair(tmp_path, fault):
    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    marker = cache.roots.state / "layer-cache.json"
    saved = marker.read_bytes()
    if fault == "missing":
        marker.unlink()
    elif fault == "malformed":
        marker.write_text("{")
    elif fault == "duplicate":
        marker.write_text('{"schema":"one","schema":"two"}')
    elif fault == "symlink":
        other = tmp_path / "outside"
        other.write_bytes(saved)
        marker.unlink()
        marker.symlink_to(other)
    elif fault == "hardlink":
        os.link(marker, tmp_path / "alias")
    else:
        marker.chmod(0o644)
    with pytest.raises(ArtifactValidationError):
        cache.retain(root, "new-instance")
    with pytest.raises(ArtifactValidationError):
        LayerCache(cache.root)
    assert fault != "missing" or not marker.exists()
    assert fault != "symlink" or marker.is_symlink()


@pytest.mark.parametrize("fault", ["missing", "corrupt", "symlink"])
def test_source_damage_is_refused_even_when_import_could_repair_it(tmp_path, fault):
    source, root, compressed = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    blob = cache.roots.oci_source_cas / "blobs/sha256" / compressed.split(":")[1]
    if fault == "corrupt":
        blob.chmod(0o600)
        original = blob.read_bytes()
        blob.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        blob.chmod(0o400)
    else:
        blob.unlink()
        if fault == "symlink":
            blob.symlink_to(source / "blobs/sha256" / compressed.split(":")[1])
    for action in (lambda: cache.inspect(root), lambda: cache.retain(root, "vm"), lambda: cache.import_source(source)):
        with pytest.raises(ArtifactValidationError):
            action()
    assert fault != "missing" or not blob.exists()
    assert fault != "symlink" or blob.is_symlink()


def test_unrelated_import_does_not_rehash_other_retained_layer_payloads(tmp_path):
    one, first, compressed = _layout(tmp_path / "one", payload=b"first unrelated payload")
    two, second, _ = _layout(tmp_path / "two", payload=b"second distinct payload")
    cache = _cache(tmp_path / "cache")
    cache.import_source(one)
    blob = cache.roots.oci_source_cas / "blobs/sha256" / compressed.split(":")[1]
    original = blob.read_bytes()
    blob.chmod(0o600)
    blob.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    blob.chmod(0o400)
    assert cache.import_source(two)["root_digest"] == second
    assert cache.retain(second, "unrelated-instance")["root_digest"] == second
    with pytest.raises(ArtifactValidationError):
        cache.inspect(first)


def test_empty_mountpoint_needs_explicit_initialization_and_never_falls_back(tmp_path):
    root = tmp_path / "mounted-cache"
    root.mkdir(mode=0o700)
    with pytest.raises(LayerCacheError):
        LayerCache(root)
    assert list(root.iterdir()) == []
    initialized = LayerCache(root, initialize=True)
    identity = initialized.inspect()["cache_id"]
    assert LayerCache(root, initialize=True).inspect()["cache_id"] == identity
    (initialized.roots.state / "layer-cache.json").unlink()
    for initialize in (False, True):
        with pytest.raises(LayerCacheError):
            LayerCache(root, initialize=initialize)
    assert not (initialized.roots.state / "layer-cache.json").exists()


def test_root_must_preexist_and_permissions_are_never_repaired(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(LayerCacheError):
        LayerCache(missing)
    assert not missing.exists()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(LayerCacheError):
        LayerCache(public)
    assert public.stat().st_mode & 0o777 == 0o755
    assert list(public.iterdir()) == []
    private = _cache(tmp_path / "private")
    alias = tmp_path / "alias"
    alias.symlink_to(private.root, target_is_directory=True)
    with pytest.raises(LayerCacheError):
        LayerCache(alias)


def test_state_directory_substitution_and_missing_child_are_refused(tmp_path):
    cache = _cache(tmp_path / "cache")
    old = cache.root / "old-state"
    cache.roots.state.rename(old)
    cache.roots.state.mkdir(mode=0o700)
    with pytest.raises(LayerCacheError):
        cache.inspect()
    cache.roots.state.rmdir()
    old.rename(cache.roots.state)
    child = cache.roots.oci_source_cas / "locks"
    child.rmdir()
    with pytest.raises(LayerCacheError):
        LayerCache(cache.root)
    assert not child.exists()


@pytest.mark.parametrize("fault", ["missing", "replacement", "symlink"])
def test_existing_lock_substitution_is_refused(tmp_path, fault):
    cache = _cache(tmp_path / "cache")
    lock = cache.root / ".layer-cache.lock"
    old = cache.root / "original-lock"
    lock.rename(old)
    if fault == "replacement":
        lock.write_bytes(b"")
        lock.chmod(0o600)
    elif fault == "symlink":
        lock.symlink_to(old)
    with pytest.raises(LayerCacheError):
        cache.inspect()
    assert fault != "missing" or not lock.exists()


@pytest.mark.parametrize("mode", [0o770, 0o777, 0o2770])
def test_renameable_cache_ancestor_refuses_before_any_state_mutation(tmp_path, mode):
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o700)
    cache = _cache(parent / "cache")
    marker = cache.roots.state / "layer-cache.json"
    original = marker.read_bytes()
    parent.chmod(mode)
    with pytest.raises(LayerCacheError, match="ancestors"):
        LayerCache(cache.root)
    with pytest.raises(LayerCacheError, match="ancestors"):
        cache.inspect()
    assert marker.read_bytes() == original


def test_trusted_sticky_parent_preserves_private_cache_operation(tmp_path):
    parent = tmp_path / "sticky-parent"
    parent.mkdir(mode=0o700)
    cache = _cache(parent / "cache")
    parent.chmod(0o1777)
    source, root, _ = _layout(tmp_path / "layout")
    assert cache.import_source(source)["root_digest"] == root
    assert LayerCache(cache.root).retain(root, "instance")["consumer_ids"] == ["instance"]


def test_failed_atomic_publication_keeps_previous_catalog_and_all_bytes(tmp_path, monkeypatch):
    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    marker = cache.roots.state / "layer-cache.json"
    before = marker.read_bytes()
    bytes_before = _bytes(cache)

    def fail_replace(*args, **kwargs):
        raise OSError("injected atomic publication failure")

    monkeypatch.setattr(layer_cache.os, "replace", fail_replace)
    with pytest.raises(LayerCacheError):
        cache.retain(root, "not-committed")
    assert marker.read_bytes() == before
    assert cache.inspect()["consumers"] == {}
    assert _bytes(cache) == bytes_before
    assert list(cache.roots.state.glob(".layer-cache-*")) == []


def test_catalog_graph_contradiction_is_not_a_false_hit(tmp_path):
    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    marker = cache.roots.state / "layer-cache.json"
    catalog = json.loads(marker.read_bytes())
    catalog["images"][root]["layers"][0]["diff_id"] = "sha256:" + "a" * 64
    marker.write_text(json.dumps(catalog))
    with pytest.raises(LayerCacheError, match="contradicts"):
        cache.inspect(root)


def test_cross_process_consumer_updates_are_serialized_without_lost_records(tmp_path):
    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_retain_process, args=(os.fspath(cache.root), root, f"instance-{i}", queue))
        for i in range(4)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
        assert [queue.get(timeout=2) for _ in processes] == [None] * len(processes)
        assert cache.inspect(root)["consumer_ids"] == [f"instance-{i}" for i in range(4)]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


def test_cache_marker_blocks_generic_deletion_without_active_leases(tmp_path):
    cache = _cache(tmp_path / "cache")
    physical = ArtifactStore(cache.roots.store)
    payload = b"permanently retained layer bytes"
    digest = _sha(payload)
    physical.publish_blob((payload,), expected_digest=digest, sealed_mode=0o400)
    before = _bytes(cache)
    store = OCIStore(cache.roots)
    with pytest.raises(ArtifactValidationError):
        physical.delete_blob(digest, retention_guard=lambda: store.assert_artifact_unleased(digest))
    assert _bytes(cache) == before
    assert physical.verify_blob(digest).size == len(payload)


def test_cli_import_retain_release_and_inspect_use_only_cache_state(tmp_path, monkeypatch, capsys):
    source, root, _ = _layout(tmp_path / "layout")
    cache_root = tmp_path / "cache"
    cache_root.mkdir(mode=0o700)

    def forbidden(*args, **kwargs):
        raise AssertionError("cache command touched global application state")

    monkeypatch.setattr(cli, "init_roots", forbidden)
    monkeypatch.setattr(cli, "resolve_roots", forbidden)
    monkeypatch.setattr(cli.host_journal, "begin", forbidden)
    prefix = ["oci", "cache", "--root", os.fspath(cache_root)]

    def run(arguments):
        assert cli.main([*prefix, *arguments]) == 0
        return json.loads(capsys.readouterr().out)

    assert run(["init"])["images"] == {}
    assert run(["import", os.fspath(source)])["root_digest"] == root
    run(["retain", root, "--consumer", "first-vm"])
    assert run(["retain", root, "--consumer", "second-vm"])["consumer_ids"] == ["first-vm", "second-vm"]
    run(["release", "--consumer", "first-vm"])
    run(["release", "--consumer", "second-vm"])
    view = run(["inspect", "--manifest", root])
    assert view["consumer_ids"] == [] and view["retention"] == "permanent"
    assert all(Path(path).is_file() for path in view["source_blob_paths"])


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("mksquashfs") is None,
    reason="real canonical hard materializer requires Linux and mksquashfs >=4.6",
)
@pytest.mark.parametrize("pack_profile", list(SquashFSPackProfile))
def test_real_materialization_reuses_recipe_after_reopen_and_changed_host_authority(
    tmp_path, monkeypatch, pack_profile
):
    source, root, compressed = _layout(tmp_path / "layout", repeat=True)
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    packer = Path(shutil.which("mksquashfs")).resolve()
    alias = tmp_path / "relative-packer"
    alias.symlink_to(packer)
    monkeypatch.chdir(tmp_path)
    first = cache.materialize(root, packer_path=Path("relative-packer"), pack_profile=pack_profile)
    assert [result["cache_result"] for result in first["receipt"]["results"]] == ["cold_miss", "warm_hit"]
    assert len(first["derived"]) == 1
    assert next(iter(first["derived"].values()))["key"]["pack_policy_fingerprint"] == pack_profile.policy.fingerprint
    assert first["blob_paths"][0] == first["blob_paths"][1]
    output = Path(first["blob_paths"][0])
    assert _sha(output.read_bytes()) == next(iter(first["derived"].values()))["image_digest"]
    assert next(iter(first["derived"].values()))["compressed_digest"] == compressed
    cache.retain(root, "first-vm")
    cache.retain(root, "second-vm")
    cache.release("first-vm")
    cache.release("second-vm")
    before = _bytes(cache)
    second = LayerCache(cache.root).materialize(
        root, packer_path=packer, pack_profile=pack_profile, timeout_seconds=240
    )
    assert all(result["cache_result"] == "warm_hit" for result in second["receipt"]["results"])
    moved = tmp_path / "other-host"
    shutil.copytree(cache.root, moved)
    copied = LayerCache(moved)
    assert copied._source.identity != cache._source.identity
    assert copied._store.identity != cache._store.identity
    third = copied.materialize(root, packer_path=packer, pack_profile=pack_profile, timeout_seconds=180)
    assert all(result["cache_result"] == "warm_hit" for result in third["receipt"]["results"])
    assert third["receipt"]["source_snapshot_binding_digest"] != first["receipt"]["source_snapshot_binding_digest"]
    assert _bytes(copied) == before
    damaged = Path(third["blob_paths"][0])
    damaged.unlink()
    with pytest.raises(ArtifactValidationError):
        copied.materialize(root, packer_path=packer, pack_profile=pack_profile)
    assert not damaged.exists()


@pytest.mark.parametrize("fault", ["valid", "missing", "corrupt"])
def test_different_compressed_recipe_cannot_replace_retained_output(tmp_path, fault):
    import test_oci_store as fixtures

    from palimpsest_local.oci_source import LocalLayoutSource
    from palimpsest_local.oci_store import DerivedLayerOccurrence

    one, first_root, first_compressed = _layout(tmp_path / "one")
    two, second_root, second_compressed = _layout(tmp_path / "two", gzip_mtime=1)
    cache = _cache(tmp_path / "cache")
    cache.import_source(one)
    cache.import_source(two)
    first_snapshot = LocalLayoutSource(one).snapshot(None, SourceCAS(cache.roots.oci_source_cas))
    second_snapshot = LocalLayoutSource(two).snapshot(None, SourceCAS(cache.roots.oci_source_cas))
    first = DerivedLayerOccurrence.from_image(first_snapshot, 0)
    second = DerivedLayerOccurrence.from_image(second_snapshot, 0)
    first_key, second_key = fixtures._key(first), fixtures._key(second)
    assert first_compressed != second_compressed
    assert first.diff_id == second.diff_id
    assert first_key.digest != second_key.digest

    store = OCIStore(cache.roots, repair_min_age_seconds=0)
    image = fixtures._squashfs()
    result = store.materialize(first, first_key, fixtures._producer(first, [], image))
    marker = cache.roots.state / "layer-cache.json"
    catalog = json.loads(marker.read_bytes())
    catalog["derived"][first_key.digest] = {
        "key": first_key.to_dict(),
        "key_digest": first_key.digest,
        "compressed_digest": first.compressed_digest,
        "diff_id": first.diff_id,
        "image_digest": result.image_digest,
        "image_size": result.image_size,
    }
    marker.write_text(json.dumps(catalog, sort_keys=True, separators=(",", ":")))
    cache.retain(first_root, "running-instance")
    assert cache.inspect(second_root)["derived_blob_paths"] == []
    catalog_before = marker.read_bytes()
    blob = cache.roots.store / "blobs" / "sha256" / result.image_digest.removeprefix("sha256:")
    inode = blob.stat().st_ino
    if fault == "missing":
        blob.unlink()
    elif fault == "corrupt":
        blob.chmod(0o600)
        damaged = image[:-1] + bytes([image[-1] ^ 1])
        blob.write_bytes(damaged)
        blob.chmod(0o400)

    with cache._locked():
        if fault == "valid":
            reused = store.materialize(second, second_key, fixtures._producer(second, [], image))
            assert reused.image_digest == result.image_digest
            assert blob.read_bytes() == image
        else:
            with pytest.raises(ArtifactValidationError):
                store.materialize(second, second_key, fixtures._producer(second, [], image))
    assert marker.read_bytes() == catalog_before
    if fault == "missing":
        assert not blob.exists()
    else:
        assert blob.stat().st_ino == inode
        if fault == "corrupt":
            assert blob.read_bytes() == damaged


@pytest.mark.parametrize("pack_profile", [None, SquashFSPackProfile.SERVICE_IMAGE])
def test_materialize_forwards_fixed_profile_and_remaining_deadline(tmp_path, monkeypatch, pack_profile):
    from types import SimpleNamespace

    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    packer = tmp_path / "packer"
    packer.write_bytes(b"pinned-packer")
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(layer_cache, "time", SimpleNamespace(monotonic=lambda: clock.now))
    selected = pack_profile or SquashFSPackProfile.CANONICAL
    toolchain = object()

    def discover(path, **kwargs):
        assert path == packer.resolve()
        assert kwargs["expected_packer_sha256"] == hashlib.sha256(b"pinned-packer").hexdigest()
        assert kwargs["policy"] is selected.policy
        clock.now += 12.5
        return toolchain

    class ObservedMaterialization(Exception):
        pass

    def materialize(snapshot, **kwargs):
        assert kwargs["pack_profile"] is selected
        assert kwargs["timeout_seconds"] == 287.5
        assert kwargs["toolchain"] is toolchain
        raise ObservedMaterialization

    monkeypatch.setattr(layer_cache, "discover_squashfs_toolchain", discover)
    monkeypatch.setattr(layer_cache, "materialize_image_hard", materialize)
    arguments = {} if pack_profile is None else {"pack_profile": pack_profile}
    with pytest.raises(ObservedMaterialization):
        cache.materialize(root, packer_path=packer, timeout_seconds=300, **arguments)


def test_materialize_pre_worker_expiry_preserves_coded_failure_summary(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from palimpsest_local import hub_image_builder
    from palimpsest_local.oci_materializer import OCIHardWorkerError

    source, root, _ = _layout(tmp_path / "layout")
    cache = _cache(tmp_path / "cache")
    cache.import_source(source)
    packer = tmp_path / "packer"
    packer.write_bytes(b"pinned-packer")
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(layer_cache, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def discover(*args, **kwargs):
        clock.now = 301
        return object()

    monkeypatch.setattr(layer_cache, "discover_squashfs_toolchain", discover)
    monkeypatch.setattr(layer_cache, "materialize_image_hard", lambda *a, **k: pytest.fail("worker started"))
    with pytest.raises(OCIHardWorkerError) as failure:
        cache.materialize(root, packer_path=packer, pack_profile=SquashFSPackProfile.SERVICE_IMAGE)
    summary = json.loads(hub_image_builder._failure_summary(failure.value, "execution"))
    assert summary["code"] == "oci-worker-timeout"
    assert summary["stage"] == "materialization"
    assert summary["category"] == "OCIHardWorkerError"
