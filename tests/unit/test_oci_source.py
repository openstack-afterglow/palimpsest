"""Secure local OCI-layout to private source-CAS contracts."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import stat
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

import pytest

import palimpsest_local.oci_materializer as oci_materializer
import palimpsest_local.oci_source as oci_source
from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_image import OCIImageRef
from palimpsest_local.oci_provenance import (
    DOCKER_IMAGE_CONFIG_MEDIA_TYPE,
    DOCKER_IMAGE_MANIFEST_MEDIA_TYPE,
    DOCKER_LAYER_GZIP_MEDIA_TYPE,
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    Descriptor,
)
from palimpsest_local.oci_source import (
    LocalArchiveSource,
    LocalLayoutSource,
    SnapshotCheckpoint,
    SnapshotStage,
    SourceCAS,
)
from palimpsest_local.oci_store import DerivedLayerReceipt, MaterializationResult


def _ref(digest: str | None = None) -> OCIImageRef:
    suffix = f"@{digest}" if digest is not None else ":stable"
    return OCIImageRef(
        registry="registry.example.com",
        repository="team/app",
        requested_reference=f"registry.example.com/team/app{suffix}",
    )


class Layout:
    def __init__(self, root: Path):
        self.root = root
        self.blobs = root / "blobs" / "sha256"
        self.blobs.mkdir(parents=True)
        (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")

    def add(self, value: object, media_type: str) -> Descriptor:
        payload = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        (self.blobs / digest.split(":", 1)[1]).write_bytes(payload)
        return Descriptor(media_type=media_type, digest=digest, size=len(payload))

    def top(self, descriptor: Descriptor, *, duplicates: int = 1) -> None:
        (self.root / "index.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                    "manifests": [descriptor.to_dict() for _ in range(duplicates)],
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def source(self, descriptor: Descriptor, checkpoint=None) -> LocalLayoutSource:
        uri = f"oci-layout://{self.root}@{descriptor.digest}"
        if checkpoint is None:
            return LocalLayoutSource.parse(uri)
        return LocalLayoutSource.parse(uri, checkpoint=checkpoint)


def _direct_layout(
    root: Path,
    *,
    layer_payloads: tuple[bytes, ...] = (b"layer",),
    repeated: bool = False,
    docker: bool = False,
) -> tuple[Layout, Descriptor, Descriptor, tuple[Descriptor, ...]]:
    layout = Layout(root)
    layer_media_type = DOCKER_LAYER_GZIP_MEDIA_TYPE if docker else OCI_LAYER_GZIP_MEDIA_TYPE
    config_media_type = DOCKER_IMAGE_CONFIG_MEDIA_TYPE if docker else OCI_IMAGE_CONFIG_MEDIA_TYPE
    manifest_media_type = DOCKER_IMAGE_MANIFEST_MEDIA_TYPE if docker else OCI_IMAGE_MANIFEST_MEDIA_TYPE
    layers = tuple(layout.add(payload, layer_media_type) for payload in layer_payloads)
    if repeated:
        layers = (layers[0], layers[0])
    diff_ids = [f"sha256:{index + 1:064x}" for index in range(len(layers))]
    config = layout.add(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        },
        config_media_type,
    )
    manifest = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": manifest_media_type,
            "config": config.to_dict(),
            "layers": [layer.to_dict() for layer in layers],
        },
        manifest_media_type,
    )
    layout.top(manifest)
    return layout, manifest, config, layers


def _index_layout(root: Path) -> tuple[Layout, Descriptor, Descriptor, Descriptor, Descriptor]:
    layout, amd_manifest, amd_config, layers = _direct_layout(root)
    arm_config = layout.add(
        {
            "architecture": "arm64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]},
        },
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    missing_arm_manifest = Descriptor(
        media_type=OCI_IMAGE_MANIFEST_MEDIA_TYPE,
        digest="sha256:" + "e" * 64,
        size=123,
    )
    index = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
            "manifests": [
                {
                    **missing_arm_manifest.to_dict(),
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
                {**amd_manifest.to_dict(), "platform": {"os": "linux", "architecture": "amd64"}},
            ],
        },
        OCI_IMAGE_INDEX_MEDIA_TYPE,
    )
    layout.top(index)
    # The unused config is present only to prove that a nonselected graph is
    # irrelevant; its manifest blob is intentionally absent.
    assert (layout.blobs / arm_config.digest.split(":", 1)[1]).is_file()
    return layout, index, amd_manifest, amd_config, layers[0]


def _cas(tmp_path: Path) -> SourceCAS:
    return SourceCAS(tmp_path / "source-cas")


def _target(cas_root: Path, descriptor: Descriptor) -> Path:
    return cas_root / "blobs" / "sha256" / descriptor.digest.split(":", 1)[1]


def _write_oci_archive(layout: Path, archive: Path) -> None:
    with tarfile.open(archive, "w") as stream:
        for source in sorted(path for path in layout.rglob("*") if path.is_file()):
            stream.add(source, arcname=source.relative_to(layout), recursive=False)


def _derived_result(image, ordinal: int) -> MaterializationResult:
    def digest(label: str) -> str:
        return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"

    return MaterializationResult(
        receipt=DerivedLayerReceipt(
            store_id="oci-store-v1:" + "1" * 64,
            occurrence_digest=digest(f"occurrence-{ordinal}"),
            record_digest=digest(f"record-{ordinal}"),
            key_digest=digest(f"key-{ordinal}"),
            source_snapshot_binding_digest=image.binding_digest,
            source_image_digest=image.image.digest,
            ordinal=ordinal,
            image_digest=digest(f"image-{ordinal}"),
            image_size=(ordinal + 1) * 512,
        ),
        cache_result="cold_miss",
    )


def test_open_existing_reuses_identity_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "source-cas"
    created = SourceCAS(root)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    reopened = SourceCAS.open_existing(root, expected_cas_id=created.identity)

    assert reopened.identity == created.identity
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before


def test_open_existing_missing_root_or_child_never_creates_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ArtifactValidationError):
        SourceCAS.open_existing(missing, expected_cas_id="source-cas-v1:" + "0" * 64)
    assert not missing.exists()

    root = tmp_path / "source-cas"
    created = SourceCAS(root)
    shutil.rmtree(root / "locks")
    with pytest.raises(ArtifactValidationError):
        SourceCAS.open_existing(root, expected_cas_id=created.identity)
    assert not (root / "locks").exists()


def test_open_existing_rejects_identity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "source-cas"
    SourceCAS(root)

    with pytest.raises(ArtifactValidationError, match="identity does not match"):
        SourceCAS.open_existing(root, expected_cas_id="source-cas-v1:" + "0" * 64)


def _process_snapshot_worker(uri: str, cas_root: str, barrier, results) -> None:
    source = LocalLayoutSource.parse(uri)

    def checkpoint(item: SnapshotCheckpoint) -> None:
        if item.stage is SnapshotStage.AFTER_TEMP_FSYNC and item.digest == source.root_digest:
            barrier.wait(timeout=10)

    source = LocalLayoutSource.parse(uri, checkpoint=checkpoint)
    try:
        result = source.snapshot(_ref(), SourceCAS(Path(cas_root)))
        results.put(("ok", result.binding_digest))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_direct_layout_snapshots_complete_graph_without_paths(tmp_path: Path) -> None:
    layout, manifest, config, layers = _direct_layout(tmp_path / "layout")
    cas = _cas(tmp_path)

    result = layout.source(manifest).snapshot(_ref(manifest.digest), cas)

    assert result.image.index_descriptor is None
    assert result.root == result.manifest
    assert result.manifest.descriptor == manifest
    assert result.config.descriptor == config
    assert tuple(item.descriptor for item in result.layers) == layers
    assert result.binding_digest.startswith("sha256:")
    serialized = json.dumps(result.to_dict(), sort_keys=True)
    assert os.fspath(layout.root) not in serialized
    assert os.fspath(tmp_path / "source-cas") not in serialized
    assert os.fspath(layout.root) not in repr(result)
    cas.verify_image(result)


@pytest.mark.parametrize("archive_input", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
def test_local_auto_selection_pins_the_unique_root_in_verified_snapshot(tmp_path, archive_input, indexed):
    if indexed:
        layout, root, manifest, _config, _layer = _index_layout(tmp_path / "layout")
    else:
        layout, root, _config, _layers = _direct_layout(tmp_path / "layout")
        manifest = root
    if archive_input:
        archive = tmp_path / "image.tar"
        _write_oci_archive(layout.root, archive)
        before = archive.read_bytes()
        source = LocalArchiveSource(archive)
    else:
        source = LocalLayoutSource(layout.root)
    cas = _cas(tmp_path)
    result = source.snapshot(None, cas)
    explicit = (
        LocalArchiveSource(archive, root.digest) if archive_input else LocalLayoutSource(layout.root, root.digest)
    ).snapshot(None, cas)
    assert result.root.descriptor == root
    assert result.manifest.descriptor == manifest
    assert result.image.reference.requested_reference.endswith("@" + root.digest)
    assert result.binding_digest == explicit.binding_digest
    assert source.root_digest is None  # discovery is a snapshot, not mutable request state
    cas.verify_image(result)
    if archive_input:
        assert archive.read_bytes() == before


@pytest.mark.parametrize("archive_input", [False, True])
@pytest.mark.parametrize("entries", ["empty", "duplicate", "different", "unsupported", "embedded"])
def test_local_auto_selection_rejects_ambiguous_or_unsupported_roots_before_import(tmp_path, archive_input, entries):
    layout, root, _config, _layers = _direct_layout(tmp_path / "layout")
    other = {**root.to_dict(), "digest": "sha256:" + "f" * 64}
    manifests = {
        "empty": [],
        "duplicate": [root.to_dict(), root.to_dict()],
        "different": [root.to_dict(), other],
        "unsupported": [{**root.to_dict(), "mediaType": "application/octet-stream"}],
        "embedded": [{**root.to_dict(), "data": "not-a-blob"}],
    }[entries]
    (layout.root / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": manifests}))
    if archive_input:
        archive = tmp_path / "image.tar"
        _write_oci_archive(layout.root, archive)
        source = LocalArchiveSource(archive)
    else:
        source = LocalLayoutSource(layout.root)
    cas = _cas(tmp_path)
    with pytest.raises(ArtifactValidationError):
        source.snapshot(None, cas)
    assert list((tmp_path / "source-cas" / "blobs" / "sha256").iterdir()) == []


def test_explicit_pin_still_selects_one_root_from_ambiguous_layout(tmp_path):
    layout, root, _config, _layers = _direct_layout(tmp_path / "layout")
    other = {**root.to_dict(), "digest": "sha256:" + "f" * 64}
    (layout.root / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [other, root.to_dict()]}))
    result = LocalLayoutSource(layout.root, root.digest).snapshot(None, _cas(tmp_path))
    assert result.root.descriptor == root


def test_auto_selected_root_is_content_verified_not_just_index_metadata(tmp_path):
    layout, root, _config, _layers = _direct_layout(tmp_path / "layout")
    blob = layout.blobs / root.digest.removeprefix("sha256:")
    blob.write_bytes(b"x" * root.size)
    with pytest.raises(ArtifactValidationError):
        LocalLayoutSource(layout.root).snapshot(None, _cas(tmp_path))


def test_index_snapshots_only_selected_platform_graph(tmp_path: Path) -> None:
    layout, index, manifest, config, layer = _index_layout(tmp_path / "layout")

    result = layout.source(index).snapshot(_ref(index.digest), _cas(tmp_path))

    assert result.image.index_descriptor == index
    assert result.root.descriptor == index
    assert result.manifest.descriptor == manifest
    assert result.config.descriptor == config
    assert tuple(item.descriptor for item in result.layers) == (layer,)


@pytest.mark.parametrize("docker,layer_count", [(True, 1), (False, 128)])
def test_docker_v2_and_maximum_layer_graphs_close_in_source_cas(tmp_path: Path, docker: bool, layer_count: int) -> None:
    payloads = tuple(f"layer-{index}".encode() for index in range(layer_count))
    layout, manifest, _, layers = _direct_layout(
        tmp_path / "layout",
        layer_payloads=payloads,
        docker=docker,
    )
    cas = _cas(tmp_path)

    result = layout.source(manifest).snapshot(_ref(), cas)

    assert tuple(item.descriptor for item in result.layers) == layers
    cas.verify_image(result)


def test_repeated_layer_occurrence_uses_one_physical_cas_blob(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout", repeated=True)
    promoted: list[str] = []

    def checkpoint(item: SnapshotCheckpoint) -> None:
        if item.stage is SnapshotStage.AFTER_PROMOTE and item.digest is not None:
            promoted.append(item.digest)

    result = layout.source(manifest, checkpoint).snapshot(_ref(), _cas(tmp_path))

    assert len(result.layers) == 2
    assert result.layers[0] == result.layers[1]
    assert [item.descriptor for item in result.layers] == [layers[0], layers[0]]
    assert promoted.count(layers[0].digest) == 1


@pytest.mark.parametrize(
    "value",
    [
        "layout@sha256:" + "a" * 64,
        "oci-layout://relative@sha256:" + "a" * 64,
        "oci-layout:///tmp/layout",
        "oci-layout:///tmp/layout@sha256:ABC",
        "oci-layout:///tmp/layout@sha256:" + "a" * 64 + "?x=1",
        "oci-layout:///tmp/layout@sha256:" + "a" * 64 + "#fragment",
        "oci-layout:///tmp/layout\x00x@sha256:" + "a" * 64,
    ],
)
def test_local_layout_uri_rejects_unsafe_or_unpinned_values(value: str) -> None:
    with pytest.raises(ArtifactValidationError):
        LocalLayoutSource.parse(value)


def test_local_layout_uri_uses_last_at_as_pin_delimiter(tmp_path: Path) -> None:
    root = tmp_path / "layout@version"
    layout, manifest, _, _ = _direct_layout(root)
    source = LocalLayoutSource.parse(f"oci-layout://{root}@{manifest.digest}")
    assert source.layout == root
    assert source.snapshot(_ref(), _cas(tmp_path)).manifest.descriptor == manifest


def test_forced_component_fallback_matches_secure_source_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    monkeypatch.setattr(oci_source, "_openat2_directory", lambda _root_fd, _relative: None)

    result = layout.source(manifest).snapshot(_ref(), _cas(tmp_path))

    assert result.manifest.descriptor == manifest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux openat2 fast path")
def test_linux_openat2_and_forced_fallback_select_same_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        probe = oci_source._openat2_directory(root_fd, os.fspath(layout.root).lstrip("/"))
    finally:
        os.close(root_fd)
    if probe is None:
        pytest.skip("kernel does not expose openat2")
    os.close(probe)

    fast = layout.source(manifest).snapshot(_ref(), SourceCAS(tmp_path / "fast-cas"))
    monkeypatch.setattr(oci_source, "_openat2_directory", lambda _root_fd, _relative: None)
    fallback = layout.source(manifest).snapshot(_ref(), SourceCAS(tmp_path / "fallback-cas"))

    assert fast.image.digest == fallback.image.digest
    assert tuple(item.descriptor for item in fast.layers) == tuple(item.descriptor for item in fallback.layers)


def test_layout_marker_and_top_index_are_strict_and_root_is_unique(tmp_path: Path) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    cas = _cas(tmp_path)
    (layout.root / "oci-layout").write_text('{"imageLayoutVersion":"0.9.0"}', encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="layout version"):
        layout.source(manifest).snapshot(_ref(), cas)

    (layout.root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
    layout.top(manifest, duplicates=2)
    with pytest.raises(ArtifactValidationError, match="exactly once"):
        layout.source(manifest).snapshot(_ref(), cas)
    layout.top(manifest, duplicates=0)
    with pytest.raises(ArtifactValidationError, match="exactly once"):
        layout.source(manifest).snapshot(_ref(), cas)


def test_malformed_nonselected_top_descriptor_and_wrong_top_media_type_are_rejected(tmp_path: Path) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    top_path = layout.root / "index.json"
    top = json.loads(top_path.read_bytes())
    top["manifests"].append({"digest": "sha256:" + "e" * 64})
    top_path.write_text(json.dumps(top), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match=r"manifests\[1\] is malformed"):
        layout.source(manifest).snapshot(_ref(), _cas(tmp_path))

    top["manifests"].pop()
    top["mediaType"] = "application/example.index"
    top_path.write_text(json.dumps(top), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="unsupported mediaType"):
        layout.source(manifest).snapshot(_ref(), _cas(tmp_path))


def test_source_cas_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "alias-parent"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ArtifactValidationError, match="securely open"):
        SourceCAS(alias / "cas")
    assert list(real_parent.iterdir()) == []


def test_component_checkpoint_exception_closes_opened_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    real_open = oci_source.os.open
    opened_child_fd: int | None = None

    def recording_open(path, flags, *args, **kwargs):
        nonlocal opened_child_fd
        result = real_open(path, flags, *args, **kwargs)
        if path == "child":
            opened_child_fd = result
        return result

    def fail_checkpoint(_item: SnapshotCheckpoint) -> None:
        raise RuntimeError("checkpoint failure")

    monkeypatch.setattr(oci_source.os, "open", recording_open)
    try:
        with pytest.raises(RuntimeError, match="checkpoint failure"):
            oci_source._open_child_directory(parent_fd, "child", fail_checkpoint)
    finally:
        os.close(parent_fd)
    assert opened_child_fd is not None
    with pytest.raises(OSError):
        os.fstat(opened_child_fd)


@pytest.mark.parametrize(
    "target",
    ["layout", "oci-layout", "index.json", "blobs", "sha256", "root-blob", "config", "layer"],
)
def test_layout_and_every_fixed_component_reject_symlinks(tmp_path: Path, target: str) -> None:
    real = tmp_path / "real"
    layout, manifest, config, layers = _direct_layout(real)
    external = tmp_path / "external"
    external.mkdir()
    source_root = layout.root
    if target == "layout":
        alias = tmp_path / "layout-link"
        alias.symlink_to(layout.root, target_is_directory=True)
        source_root = alias
    elif target in {"oci-layout", "index.json"}:
        original = layout.root / target
        moved = external / target
        original.rename(moved)
        original.symlink_to(moved)
    elif target == "blobs":
        original = layout.root / "blobs"
        moved = external / "blobs"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    elif target == "sha256":
        original = layout.root / "blobs" / "sha256"
        moved = external / "sha256"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    else:
        descriptor = {"root-blob": manifest, "config": config, "layer": layers[0]}[target]
        original = layout.blobs / descriptor.digest.split(":", 1)[1]
        moved = external / original.name
        original.rename(moved)
        original.symlink_to(moved)
    source = LocalLayoutSource(layout=source_root, root_digest=manifest.digest)

    with pytest.raises(ArtifactValidationError):
        source.snapshot(_ref(), _cas(tmp_path))


def test_fifo_blob_is_rejected_without_blocking(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer_path = layout.blobs / layers[0].digest.split(":", 1)[1]
    layer_path.unlink()
    os.mkfifo(layer_path)

    with pytest.raises(ArtifactValidationError, match="unsafe|securely open"):
        layout.source(manifest).snapshot(_ref(), _cas(tmp_path))


def test_fifo_leaf_open_uses_nonblocking_nofollow_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer_name = layers[0].digest.split(":", 1)[1]
    layer_path = layout.blobs / layer_name
    layer_path.unlink()
    os.mkfifo(layer_path)
    real_open = oci_source.os.open
    observed: list[int] = []

    def recording_open(path, flags, *args, **kwargs):
        if path == layer_name:
            observed.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(oci_source.os, "open", recording_open)
    with pytest.raises(ArtifactValidationError):
        layout.source(manifest).snapshot(_ref(), _cas(tmp_path))
    assert observed
    required = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    assert observed[0] & required == required


def test_injected_device_fd_is_rejected_by_leaf_fstat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer_name = layers[0].digest.split(":", 1)[1]
    real_open = oci_source.os.open
    device_fd = real_open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    injected = False

    def device_open(path, flags, *args, **kwargs):
        nonlocal injected
        if path == layer_name and not injected:
            injected = True
            return os.dup(device_fd)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(oci_source.os, "open", device_open)
    try:
        with pytest.raises(ArtifactValidationError, match="unsafe"):
            layout.source(manifest).snapshot(_ref(), _cas(tmp_path))
    finally:
        os.close(device_fd)
    assert injected is True


def test_unix_socket_blob_is_rejected_as_nonregular(tmp_path: Path) -> None:
    short_root = Path(tempfile.mkdtemp(dir="/tmp", prefix="oci-")).resolve()
    try:
        layout, manifest, _, layers = _direct_layout(short_root)
        layer_path = layout.blobs / layers[0].digest.split(":", 1)[1]
        layer_path.unlink()
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(os.fspath(layer_path))
            with pytest.raises(ArtifactValidationError, match="unsafe|securely open"):
                layout.source(manifest).snapshot(_ref(), _cas(tmp_path))
        finally:
            listener.close()
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


@pytest.mark.parametrize("mutation", ["rewrite", "truncate", "grow", "chmod"])
def test_same_inode_mutation_during_copy_fails_without_publishing_layer(tmp_path: Path, mutation: str) -> None:
    payload = b"a" * (1024 * 1024 + 17)
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout", layer_payloads=(payload,))
    layer = layers[0]
    layer_path = layout.blobs / layer.digest.split(":", 1)[1]
    changed = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal changed
        if changed or item.stage is not SnapshotStage.AFTER_COPY_CHUNK or item.digest != layer.digest:
            return
        changed = True
        if mutation == "rewrite":
            layer_path.write_bytes(b"b" * len(payload))
        elif mutation == "truncate":
            layer_path.write_bytes(b"a")
        elif mutation == "grow":
            layer_path.write_bytes(payload + b"more")
        else:
            layer_path.chmod(0o000)

    cas_root = tmp_path / "source-cas"
    try:
        with pytest.raises(ArtifactValidationError, match="changed|verification|grew"):
            layout.source(manifest, checkpoint).snapshot(_ref(), SourceCAS(cas_root))
    finally:
        layer_path.chmod(0o600)
    assert not _target(cas_root, layer).exists()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_hardlinked_source_blob_is_rejected(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer_path = layout.blobs / layers[0].digest.split(":", 1)[1]
    os.link(layer_path, tmp_path / "alias")
    with pytest.raises(ArtifactValidationError, match="unsafe"):
        layout.source(manifest).snapshot(_ref(), _cas(tmp_path))


def test_hardlink_created_after_initial_fstat_is_detected(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]
    layer_path = layout.blobs / layer.digest.split(":", 1)[1]
    alias = tmp_path / "late-hardlink"
    linked = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal linked
        if item.stage is SnapshotStage.AFTER_INITIAL_FSTAT and item.digest == layer.digest and not linked:
            linked = True
            os.link(layer_path, alias)
            with alias.open("ab") as stream:
                stream.write(b"mutation")

    cas_root = tmp_path / "source-cas"
    with pytest.raises(ArtifactValidationError, match="changed|grew|verification"):
        layout.source(manifest, checkpoint).snapshot(_ref(), SourceCAS(cas_root))
    assert linked is True
    assert not _target(cas_root, layer).exists()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_root_directory_replacement_after_open_keeps_pinned_authority(tmp_path: Path) -> None:
    root = tmp_path / "layout"
    layout, manifest, _, _ = _direct_layout(root)
    moved = tmp_path / "opened-layout"
    replaced = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal replaced
        if item.stage is SnapshotStage.AFTER_ROOT_OPEN and not replaced:
            replaced = True
            root.rename(moved)
            root.mkdir()
            (root / "sentinel").write_text("replacement", encoding="utf-8")

    result = layout.source(manifest, checkpoint).snapshot(_ref(), _cas(tmp_path))
    assert result.manifest.descriptor == manifest
    assert (root / "sentinel").read_text(encoding="utf-8") == "replacement"


@pytest.mark.parametrize("component", ["blobs", "sha256"])
def test_fixed_component_replacement_after_open_keeps_pinned_authority(tmp_path: Path, component: str) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    replaced = False
    moved = tmp_path / f"opened-{component}"

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal replaced
        if replaced or item.stage is not SnapshotStage.AFTER_COMPONENT_OPEN or item.component != component:
            return
        replaced = True
        if component == "blobs":
            original = layout.root / "blobs"
            original.rename(moved)
            original.mkdir()
        else:
            original = layout.root / "blobs" / "sha256"
            original.rename(moved)
            original.mkdir()
        (original / "sentinel").write_text("replacement", encoding="utf-8")

    result = layout.source(manifest, checkpoint).snapshot(_ref(), _cas(tmp_path))
    assert result.manifest.descriptor == manifest
    assert replaced is True


@pytest.mark.parametrize("failure", ["size", "digest"])
def test_initial_source_size_and_digest_mismatch_fail_closed(tmp_path: Path, failure: str) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]
    path = layout.blobs / layer.digest.split(":", 1)[1]
    if failure == "size":
        path.write_bytes(b"different-size")
    else:
        path.write_bytes(b"other")
        raw = json.loads((layout.blobs / manifest.digest.split(":", 1)[1]).read_bytes())
        raw["layers"][0]["size"] = len(b"other")
        new_manifest = layout.add(raw, OCI_IMAGE_MANIFEST_MEDIA_TYPE)
        layout.top(new_manifest)
        manifest = new_manifest
    cas_root = tmp_path / "source-cas"
    with pytest.raises(ArtifactValidationError, match="wrong size|verification"):
        layout.source(manifest).snapshot(_ref(), SourceCAS(cas_root))
    assert not _target(cas_root, layer).exists()


def test_leaf_rename_after_open_never_publishes_replacement_bytes(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]
    layer_path = layout.blobs / layer.digest.split(":", 1)[1]
    moved = tmp_path / "opened-layer"
    replaced = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal replaced
        if item.stage is SnapshotStage.AFTER_BLOB_OPEN and item.digest == layer.digest and not replaced:
            replaced = True
            layer_path.rename(moved)
            layer_path.write_bytes(b"replacement")

    cas_root = tmp_path / "source-cas"
    with pytest.raises(ArtifactValidationError, match="changed"):
        layout.source(manifest, checkpoint).snapshot(_ref(), SourceCAS(cas_root))
    assert layer_path.read_bytes() == b"replacement"
    assert not _target(cas_root, layer).exists()


@pytest.mark.parametrize("poison", ["corrupt", "symlink", "fifo"])
def test_invalid_cas_target_is_atomically_repaired(tmp_path: Path, poison: str) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    cas_root = tmp_path / "source-cas"
    cas = SourceCAS(cas_root)
    target = _target(cas_root, layers[0])
    if poison == "corrupt":
        target.write_bytes(b"bad")
        target.chmod(0o400)
    elif poison == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
    else:
        os.mkfifo(target)

    result = layout.source(manifest).snapshot(_ref(), cas)

    assert stat.S_ISREG(os.lstat(target).st_mode)
    assert target.read_bytes() == b"layer"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    cas.verify_image(result)


def test_interrupted_copy_removes_temporary_and_publishes_no_layer(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]

    def checkpoint(item: SnapshotCheckpoint) -> None:
        if item.stage is SnapshotStage.AFTER_COPY_CHUNK and item.digest == layer.digest:
            raise RuntimeError("producer interrupted")

    cas_root = tmp_path / "source-cas"
    with pytest.raises(RuntimeError, match="interrupted"):
        layout.source(manifest, checkpoint).snapshot(_ref(), SourceCAS(cas_root))
    assert not _target(cas_root, layer).exists()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_source_read_error_cleans_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]
    layer_inode = (layout.blobs / layer.digest.split(":", 1)[1]).stat().st_ino
    real_read = oci_source.os.read
    injected = False

    def failing_read(file_fd: int, size: int) -> bytes:
        nonlocal injected
        if not injected and os.fstat(file_fd).st_ino == layer_inode:
            injected = True
            raise OSError("injected source read failure")
        return real_read(file_fd, size)

    monkeypatch.setattr(oci_source.os, "read", failing_read)
    cas_root = tmp_path / "source-cas"
    with pytest.raises(ArtifactValidationError, match="cannot read OCI blob"):
        layout.source(manifest).snapshot(_ref(), SourceCAS(cas_root))
    assert injected is True
    assert not _target(cas_root, layer).exists()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_temp_fsync_error_cleans_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]
    real_fsync = oci_source.os.fsync
    armed = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal armed
        if item.stage is SnapshotStage.BEFORE_FINAL_FSTAT and item.digest == layer.digest:
            armed = True

    def failing_fsync(file_fd: int) -> None:
        nonlocal armed
        if armed:
            armed = False
            raise OSError("injected temp fsync failure")
        real_fsync(file_fd)

    monkeypatch.setattr(oci_source.os, "fsync", failing_fsync)
    cas_root = tmp_path / "source-cas"
    with pytest.raises(ArtifactValidationError, match="durably stage"):
        layout.source(manifest, checkpoint).snapshot(_ref(), SourceCAS(cas_root))
    assert not _target(cas_root, layer).exists()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_digest_lock_failure_cleans_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")

    def fail_lock(_file_fd: int, _operation: int) -> None:
        raise OSError("injected flock failure")

    monkeypatch.setattr(oci_source.fcntl, "flock", fail_lock)
    cas_root = tmp_path / "source-cas"
    with pytest.raises(ArtifactValidationError, match="digest lock"):
        layout.source(manifest).snapshot(_ref(), SourceCAS(cas_root))
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_two_concurrent_callers_promote_each_digest_once(tmp_path: Path) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    cas = _cas(tmp_path)
    barrier = threading.Barrier(2)
    promoted: list[str] = []
    promotion_lock = threading.Lock()

    def checkpoint(item: SnapshotCheckpoint) -> None:
        if item.stage is SnapshotStage.AFTER_TEMP_FSYNC and item.digest == manifest.digest:
            barrier.wait(timeout=5)
        if item.stage is SnapshotStage.AFTER_PROMOTE and item.digest is not None:
            with promotion_lock:
                promoted.append(item.digest)

    source = layout.source(manifest, checkpoint)
    results = []
    errors = []

    def worker() -> None:
        try:
            results.append(source.snapshot(_ref(), cas))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0].binding_digest == results[1].binding_digest
    assert promoted.count(manifest.digest) == 1
    cas.verify_image(results[0])
    assert not list((tmp_path / "source-cas" / "blobs" / "sha256").glob(".source-tmp-*"))


def test_two_processes_share_digest_lock_and_one_cas_target(tmp_path: Path) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    uri = f"oci-layout://{layout.root}@{manifest.digest}"
    cas_root = tmp_path / "source-cas"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_snapshot_worker,
            args=(uri, os.fspath(cas_root), barrier, results),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
        outcomes = [results.get(timeout=2), results.get(timeout=2)]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()

    assert all(process.exitcode == 0 for process in processes)
    assert [outcome[0] for outcome in outcomes] == ["ok", "ok"]
    assert outcomes[0][1] == outcomes[1][1]
    target = _target(cas_root, manifest)
    assert stat.S_ISREG(target.stat().st_mode)
    assert target.read_bytes() == (layout.blobs / target.name).read_bytes()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_source_deletion_after_promotion_does_not_affect_cas_parse_or_later_reads(tmp_path: Path) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    root_blob = layout.blobs / manifest.digest.split(":", 1)[1]
    removed = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal removed
        if item.stage is SnapshotStage.BEFORE_CAS_REOPEN and item.digest == manifest.digest and not removed:
            removed = True
            root_blob.unlink()

    cas = _cas(tmp_path)
    result = layout.source(manifest, checkpoint).snapshot(_ref(), cas)
    shutil.rmtree(layout.root)

    cas.verify_image(result)
    assert cas.read_metadata(result.manifest)


def test_cas_mutation_after_snapshot_is_detected(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    cas_root = tmp_path / "source-cas"
    cas = SourceCAS(cas_root)
    result = layout.source(manifest).snapshot(_ref(), cas)
    target = _target(cas_root, layers[0])
    target.chmod(0o600)
    target.write_bytes(b"corrupt")
    target.chmod(0o400)

    with pytest.raises(ArtifactValidationError, match="missing or corrupt"):
        cas.verify_image(result)


def test_replace_failure_cleans_temporary_and_returns_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    cas_root = tmp_path / "source-cas"

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(oci_source.os, "replace", fail_replace)
    with pytest.raises(ArtifactValidationError, match="atomically promote"):
        layout.source(manifest).snapshot(_ref(), SourceCAS(cas_root))
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))


def test_parent_fsync_failure_after_replace_retains_verified_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    cas_root = tmp_path / "source-cas"
    cas = SourceCAS(cas_root)
    real_fsync = oci_source.os.fsync
    armed = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal armed
        if item.stage is SnapshotStage.BEFORE_PROMOTE and item.digest == manifest.digest:
            armed = True

    def fail_promoted_parent_once(file_fd: int) -> None:
        nonlocal armed
        if armed:
            armed = False
            raise OSError("injected parent fsync failure")
        real_fsync(file_fd)

    monkeypatch.setattr(oci_source.os, "fsync", fail_promoted_parent_once)
    with pytest.raises(ArtifactValidationError, match="atomically promote"):
        layout.source(manifest, checkpoint).snapshot(_ref(), cas)

    target = _target(cas_root, manifest)
    assert target.read_bytes() == (layout.blobs / target.name).read_bytes()
    assert not list((cas_root / "blobs" / "sha256").glob(".source-tmp-*"))

    monkeypatch.setattr(oci_source.os, "fsync", real_fsync)
    result = layout.source(manifest).snapshot(_ref(), cas)
    cas.verify_image(result)


def test_cas_root_replacement_never_cleans_up_or_creates_inside_replacement(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout")
    layer = layers[0]
    cas_root = tmp_path / "source-cas"
    moved = tmp_path / "opened-cas"
    cas = SourceCAS(cas_root)
    replaced = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal replaced
        if item.stage is SnapshotStage.AFTER_TEMP_FSYNC and item.digest == layer.digest and not replaced:
            replaced = True
            cas_root.rename(moved)
            cas_root.mkdir(mode=0o700)

    with pytest.raises(ArtifactValidationError, match="CAS authority changed"):
        layout.source(manifest, checkpoint).snapshot(_ref(), cas)

    assert list(cas_root.iterdir()) == []
    assert not list((moved / "blobs" / "sha256").glob(".source-tmp-*"))
    assert not _target(moved, layer).exists()


@pytest.mark.parametrize("component", ["blobs", "sha256", "locks"])
def test_cas_component_replacement_prevents_receipt_and_preserves_replacement(tmp_path: Path, component: str) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    cas_root = tmp_path / "source-cas"
    cas = SourceCAS(cas_root)
    moved = tmp_path / f"opened-{component}"
    replaced = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal replaced
        if item.stage is not SnapshotStage.AFTER_TEMP_FSYNC or item.digest != manifest.digest or replaced:
            return
        replaced = True
        if component == "blobs":
            original = cas_root / "blobs"
        elif component == "sha256":
            original = cas_root / "blobs" / "sha256"
        else:
            original = cas_root / "locks"
        original.rename(moved)
        original.mkdir(mode=0o700)

    with pytest.raises(ArtifactValidationError, match="CAS authority changed"):
        layout.source(manifest, checkpoint).snapshot(_ref(), cas)

    assert replaced is True
    if component == "blobs":
        assert list((cas_root / "blobs").iterdir()) == []
        old_blob_dir = moved / "sha256"
    elif component == "sha256":
        assert list((cas_root / "blobs" / "sha256").iterdir()) == []
        old_blob_dir = moved
    else:
        assert list((cas_root / "locks").iterdir()) == []
        old_blob_dir = cas_root / "blobs" / "sha256"
    assert not list(old_blob_dir.glob(".source-tmp-*"))


def test_local_archive_selects_same_ordered_graph_as_layout(tmp_path: Path) -> None:
    layout, manifest, config, layers = _direct_layout(
        tmp_path / "layout",
        layer_payloads=(b"first", b"second"),
    )
    archive = tmp_path / "image.oci.tar"
    _write_oci_archive(layout.root, archive)

    from_layout = layout.source(manifest).snapshot(_ref(), SourceCAS(tmp_path / "layout-cas"))
    from_archive = LocalArchiveSource.parse(f"oci-archive://{archive}@{manifest.digest}").snapshot(
        _ref(), SourceCAS(tmp_path / "archive-cas")
    )

    assert from_archive.image.digest == from_layout.image.digest
    assert from_archive.manifest.descriptor == manifest
    assert from_archive.config.descriptor == config
    assert tuple(item.descriptor for item in from_archive.layers) == layers
    assert [item.ordinal for item in from_archive.image.layers] == [0, 1]


def test_local_archive_preserves_repeated_descriptor_occurrences(tmp_path: Path) -> None:
    layout, manifest, _, layers = _direct_layout(tmp_path / "layout", repeated=True)
    archive = tmp_path / "repeated.oci.tar"
    _write_oci_archive(layout.root, archive)

    image = LocalArchiveSource(archive=archive, root_digest=manifest.digest).snapshot(
        _ref(), SourceCAS(tmp_path / "archive-cas")
    )

    assert len(image.layers) == 2
    assert image.layers[0] == image.layers[1]
    assert tuple(item.descriptor for item in image.layers) == layers
    assert [item.ordinal for item in image.image.layers] == [0, 1]


def test_local_archive_index_selects_only_exact_linux_amd64_graph(tmp_path: Path) -> None:
    layout, index, manifest, config, layer = _index_layout(tmp_path / "layout")
    archive = tmp_path / "indexed.oci.tar"
    _write_oci_archive(layout.root, archive)

    image = LocalArchiveSource(archive=archive, root_digest=index.digest).snapshot(
        _ref(index.digest), SourceCAS(tmp_path / "archive-cas")
    )

    assert image.root.descriptor == index
    assert image.manifest.descriptor == manifest
    assert image.config.descriptor == config
    assert tuple(item.descriptor for item in image.layers) == (layer,)


def test_local_archive_same_inode_mutation_before_result_returns_no_snapshot(tmp_path: Path) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    archive = tmp_path / "mutable.oci.tar"
    _write_oci_archive(layout.root, archive)
    changed = False

    def checkpoint(item: SnapshotCheckpoint) -> None:
        nonlocal changed
        if item.stage is SnapshotStage.BEFORE_RESULT and not changed:
            changed = True
            with archive.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                stream.write(b"x")
                stream.flush()
                os.fsync(stream.fileno())

    source = LocalArchiveSource(archive, manifest.digest, checkpoint)
    with pytest.raises(ArtifactValidationError, match="changed during snapshot"):
        source.snapshot(_ref(), SourceCAS(tmp_path / "archive-cas"))
    assert changed is True


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "unexpected", "compressed"])
def test_local_archive_rejects_ambiguous_or_non_plain_members(tmp_path: Path, kind: str) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout")
    archive = tmp_path / "bad.oci.tar"
    if kind == "compressed":
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(layout.root / "oci-layout", arcname="oci-layout")
    else:
        _write_oci_archive(layout.root, archive)
        with tarfile.open(archive, "a") as stream:
            if kind == "duplicate":
                stream.add(layout.root / "oci-layout", arcname="oci-layout", recursive=False)
            elif kind == "symlink":
                member = tarfile.TarInfo("alias")
                member.type = tarfile.SYMTYPE
                member.linkname = "oci-layout"
                stream.addfile(member)
            else:
                member = tarfile.TarInfo("repositories")
                member.size = 0
                stream.addfile(member)

    with pytest.raises(ArtifactValidationError):
        LocalArchiveSource(archive=archive, root_digest=manifest.digest).snapshot(
            _ref(), SourceCAS(tmp_path / "archive-cas")
        )


def test_materialize_image_hard_preserves_every_occurrence_and_canonical_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout", repeated=True)
    image = layout.source(manifest).snapshot(_ref(), SourceCAS(tmp_path / "source-cas"))
    calls: list[tuple[int, float]] = []

    def fake_materialize(selected, ordinal, **kwargs):
        assert selected is image
        calls.append((ordinal, kwargs["timeout_seconds"]))
        return _derived_result(image, ordinal)

    monkeypatch.setattr(oci_materializer, "materialize_layer_hard", fake_materialize)
    receipt = oci_materializer.materialize_image_hard(
        image,
        source_cas_root=tmp_path / "source-cas",
        roots=object(),
        store=object(),
        packer_path=Path("/usr/bin/mksquashfs"),
        toolchain=object(),
        timeout_seconds=10.0,
    )

    assert [ordinal for ordinal, _ in calls] == [0, 1]
    assert all(0 < remaining <= 10.0 for _, remaining in calls)
    assert [result.receipt.ordinal for result in receipt.results] == [0, 1]
    assert receipt.source_snapshot_binding_digest == image.binding_digest
    assert receipt.manifest_digest == manifest.digest
    payload = receipt.to_dict()
    assert payload["retention"] == "none"
    assert payload["root_descriptor"] == manifest.to_dict()
    assert payload["config_descriptor"] == image.config.descriptor.to_dict()
    assert [layer["ordinal"] for layer in payload["layers"]] == [0, 1]
    assert payload["layers"][0]["compressed"] == payload["layers"][1]["compressed"]
    expected_digest = (
        "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert receipt.digest == expected_digest
    serialized = json.dumps(payload, sort_keys=True)
    assert os.fspath(tmp_path) not in serialized


def test_materialize_image_hard_stops_after_first_failed_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, manifest, _, _ = _direct_layout(tmp_path / "layout", layer_payloads=(b"one", b"two", b"three"))
    image = layout.source(manifest).snapshot(_ref(), SourceCAS(tmp_path / "source-cas"))
    calls: list[int] = []

    def fake_materialize(selected, ordinal, **_kwargs):
        calls.append(ordinal)
        if ordinal == 1:
            raise oci_materializer.OCIHardWorkerError("oci-worker-test", "injected failure")
        return _derived_result(selected, ordinal)

    monkeypatch.setattr(oci_materializer, "materialize_layer_hard", fake_materialize)
    with pytest.raises(oci_materializer.OCIHardWorkerError, match="injected failure"):
        oci_materializer.materialize_image_hard(
            image,
            source_cas_root=tmp_path / "source-cas",
            roots=object(),
            store=object(),
            packer_path=Path("/usr/bin/mksquashfs"),
            toolchain=object(),
        )

    assert calls == [0, 1]
