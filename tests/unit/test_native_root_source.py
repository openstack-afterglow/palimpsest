"""Native-root source qualification uses original verified graph and file bytes."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import replace

import pytest
from test_oci_source import Layout, _write_oci_archive

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.native_root_source import qualify_native_root_source, revalidate_native_root_source
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
)
from palimpsest_local.oci_source import LocalArchiveSource, LocalLayoutSource, SnapshottedOCIArchive, SourceCAS


def os_layout(
    root,
    *,
    os_bytes=b'ID=ubuntu\nVERSION_ID="24.04"\n',
    architecture="arm64",
    plain=False,
    indexed=False,
    repeated=False,
    bad_diff_id=False,
):
    """Build real OCI descriptors and a regular os-release in an actual layer tar."""
    raw_stream = io.BytesIO()
    with tarfile.open(fileobj=raw_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in ("etc",):
            directory = tarfile.TarInfo(name)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
        member = tarfile.TarInfo("etc/os-release")
        member.mode = 0o644
        member.size = len(os_bytes)
        archive.addfile(member, io.BytesIO(os_bytes))
    raw = raw_stream.getvalue()
    layout = Layout(root)
    layer = layout.add(
        raw if plain else gzip.compress(raw, mtime=0), OCI_LAYER_MEDIA_TYPE if plain else OCI_LAYER_GZIP_MEDIA_TYPE
    )
    layers = [layer, layer] if repeated else [layer]
    diff_id = "sha256:" + hashlib.sha256(raw).hexdigest()
    config = layout.add(
        {
            "architecture": architecture,
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64 if bad_diff_id else diff_id] * len(layers)},
            "config": {"Entrypoint": ["/must-not-execute-image-entrypoint"], "Cmd": ["literal"]},
        },
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    manifest = layout.add(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "config": config.to_dict(),
            "layers": [item.to_dict() for item in layers],
        },
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    selected_root = manifest
    if indexed:
        selected_root = layout.add(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                "manifests": [{**manifest.to_dict(), "platform": {"os": "linux", "architecture": architecture}}],
            },
            OCI_IMAGE_INDEX_MEDIA_TYPE,
        )
    layout.top(selected_root)
    return layout, selected_root, manifest, config, layer, diff_id


def qualify(source, cas, **kwargs):
    return qualify_native_root_source(source, cas, platform="linux/arm64", backend="lima-vz", **kwargs)


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
def test_original_source_qualification_preserves_exact_graph_os_and_profile(tmp_path, archived, plain, indexed):
    layout, root, manifest, config, layer, diff_id = os_layout(
        tmp_path / "layout", plain=plain, indexed=indexed, repeated=True
    )
    source = LocalLayoutSource(layout.root, root.digest)
    if archived:
        path = tmp_path / "original.oci.tar"
        _write_oci_archive(layout.root, path)
        source = LocalArchiveSource(path, root.digest)
    cas = SourceCAS(tmp_path / "cas")
    result = qualify(source, cas)
    assert isinstance(result.snapshot, SnapshottedOCIArchive)
    assert result.snapshot.root.descriptor == root
    assert result.snapshot.manifest.descriptor == manifest
    assert result.snapshot.config.descriptor == config
    assert [item.descriptor for item in result.snapshot.layers] == [layer, layer]
    assert [item.diff_id for item in result.snapshot.image.layers] == [diff_id, diff_id]
    assert result.os_release.os_id == "ubuntu"
    assert result.os_release.version_id == "24.04"
    assert (
        result.os_release.content_digest == "sha256:" + hashlib.sha256(b'ID=ubuntu\nVERSION_ID="24.04"\n').hexdigest()
    )
    assert result.os_release.layer_ordinal == 1
    assert result.os_release.source_binding_digest == result.snapshot.binding_digest
    payload = result.to_dict()
    assert payload["execution_admitted"] is False
    assert payload["scope"] == "source-os-profile-only"
    assert payload["platform"] == "linux/arm64"
    assert payload["index_descriptor"] == (root.to_dict() if indexed else None)
    assert result.profile.boot_method == "direct-linux"
    assert revalidate_native_root_source(result, cas) == result
    # Neither the original image process nor any runtime owner was used.
    assert sorted(item.name for item in tmp_path.iterdir()) == (
        ["cas", "layout", "original.oci.tar"] if archived else ["cas", "layout"]
    )


@pytest.mark.parametrize(
    "os_bytes",
    [
        b"ID=debian\nVERSION_ID=12\n",
        b"ID=ubuntu\nVERSION_ID=22.04\n",
        b"ID_LIKE=ubuntu\nVERSION_ID=24.04\n",
        b"ID=ubuntu\nVERSION_ID=$(echo 24.04)\n",
        b"ID=ubuntu\nID=ubuntu\nVERSION_ID=24.04\n",
        b"ID=ubuntu\n",
    ],
)
def test_source_rejects_unsupported_or_malformed_actual_os_identity(tmp_path, os_bytes):
    layout, *_ = os_layout(tmp_path / "layout", os_bytes=os_bytes)
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(layout.root), SourceCAS(tmp_path / "cas"))


@pytest.mark.parametrize("failure", ["pin", "arch", "diffid", "compressed"])
def test_source_rejects_wrong_root_arch_and_layer_identities(tmp_path, failure):
    layout, root, _manifest, _config, layer, _diff_id = os_layout(
        tmp_path / "layout",
        architecture="amd64" if failure == "arch" else "arm64",
        bad_diff_id=failure == "diffid",
    )
    if failure == "compressed":
        blob = layout.blobs / layer.digest.split(":", 1)[1]
        blob.write_bytes(b"x" * layer.size)
    source = LocalLayoutSource(layout.root, "sha256:" + "f" * 64 if failure == "pin" else root.digest)
    with pytest.raises(ArtifactValidationError):
        qualify(source, SourceCAS(tmp_path / "cas"))


def test_original_top_index_bytes_remain_pinned_without_reencoding(tmp_path):
    layout, root, *_ = os_layout(tmp_path / "layout")
    top = layout.root / "index.json"
    document = json.loads(top.read_bytes())
    document["manifests"][0]["platform"] = {"os": "linux", "architecture": "arm64"}
    original = b"\n" + json.dumps(document).encode() + b"\n"
    top.write_bytes(original)
    pin = "sha256:" + hashlib.sha256(original).hexdigest()
    result = qualify(LocalLayoutSource(layout.root, pin), SourceCAS(tmp_path / "cas"))
    assert result.snapshot.root.descriptor.digest == pin
    assert result.snapshot.image.index_descriptor.digest == pin
    assert result.snapshot.root.descriptor.digest != root.digest


def test_ambiguous_root_requires_explicit_pin(tmp_path):
    layout, root, *_ = os_layout(tmp_path / "layout")
    top = layout.root / "index.json"
    top.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [root.to_dict(), {**root.to_dict(), "digest": "sha256:" + "f" * 64}],
            }
        )
    )
    cas = SourceCAS(tmp_path / "cas")
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(layout.root), cas)
    assert qualify(LocalLayoutSource(layout.root, root.digest), cas).snapshot.root.descriptor == root


def test_revalidation_reopens_pinned_cas_not_mutable_original_source(tmp_path):
    layout, root, _manifest, _config, layer, _diff_id = os_layout(tmp_path / "layout")
    cas = SourceCAS(tmp_path / "cas")
    result = qualify(LocalLayoutSource(layout.root, root.digest), cas)
    (layout.blobs / layer.digest.split(":", 1)[1]).write_bytes(b"original changed after snapshot")
    assert revalidate_native_root_source(result, cas) == result
    pinned_blob = tmp_path / "cas" / "blobs" / "sha256" / layer.digest.split(":", 1)[1]
    pinned_blob.chmod(0o600)
    try:
        pinned_blob.write_bytes(b"x" * layer.size)
    finally:
        pinned_blob.chmod(0o400)
    with pytest.raises(ArtifactValidationError):
        revalidate_native_root_source(result, cas)


def test_descriptive_receipt_and_forged_profile_are_not_authority(tmp_path):
    layout, *_ = os_layout(tmp_path / "layout")
    cas = SourceCAS(tmp_path / "cas")
    result = qualify(LocalLayoutSource(layout.root), cas)
    with pytest.raises(ArtifactValidationError):
        revalidate_native_root_source(json.loads(json.dumps(result.to_dict())), cas)
    forged = replace(result, profile=replace(result.profile, profile_id="self-declared", boot_method="efi"))
    with pytest.raises(ArtifactValidationError):
        revalidate_native_root_source(forged, cas)
    forged_os = replace(result, os_release=replace(result.os_release, content_digest="sha256:" + "0" * 64))
    with pytest.raises(ArtifactValidationError):
        revalidate_native_root_source(forged_os, cas)


@pytest.mark.parametrize("bound", [0, -1, 2**33 + 1, True])
def test_source_byte_bounds_are_enforced(tmp_path, bound):
    layout, *_ = os_layout(tmp_path / "layout")
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(layout.root), SourceCAS(tmp_path / "cas"), maximum_bytes=bound)


@pytest.mark.parametrize("bound", [0, -1, 3601, float("inf"), float("nan"), True])
def test_source_time_bounds_are_finite_and_enforced(tmp_path, bound):
    layout, *_ = os_layout(tmp_path / "layout")
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(layout.root), SourceCAS(tmp_path / "cas"), timeout_seconds=bound)


def test_real_source_fails_when_acquisition_byte_budget_is_exhausted(tmp_path):
    layout, *_ = os_layout(tmp_path / "layout")
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(layout.root), SourceCAS(tmp_path / "cas"), maximum_bytes=100)


def test_source_intake_does_not_follow_a_layout_alias(tmp_path):
    layout, *_ = os_layout(tmp_path / "layout")
    alias = tmp_path / "alias"
    alias.symlink_to(layout.root, target_is_directory=True)
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(alias), SourceCAS(tmp_path / "cas"))


@pytest.mark.parametrize(
    "entry",
    [
        (".palimpsest", b"image-owned data", "file"),
        (".palimpsest", "", "directory"),
        (".palimpsest", "etc", "symlink"),
        (".palimpsest/persisted.txt", b"implicit parent", "file"),
    ],
)
def test_native_qualification_rejects_reserved_image_namespace_before_runtime(tmp_path, entry):
    from test_oci_os_release import _file, _snapshot, _tar

    _snapshot(tmp_path, _tar(_file("etc/os-release"), entry))
    with pytest.raises(ArtifactValidationError):
        qualify(LocalLayoutSource(tmp_path / "layout"), SourceCAS(tmp_path / "cas"))


def test_reserved_namespace_is_a_component_boundary(tmp_path):
    from test_oci_os_release import _file, _snapshot, _tar

    _, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release"), _file(".palimpsest-cache/user.txt", b"keep")),
    )
    result = qualify(LocalLayoutSource(tmp_path / "layout"), cas)
    assert result.os_release.os_id == "ubuntu"
    assert revalidate_native_root_source(result, cas) == result
