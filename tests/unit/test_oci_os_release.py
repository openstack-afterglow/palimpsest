"""Effective OS identity from real descriptor-bound OCI tar/gzip bytes."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from palimpsest_local.errors import ArtifactValidationError
from palimpsest_local.oci_os_release import DEFAULT_OS_READ_LIMITS, OSReadLimits, read_image_os_release
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_INDEX_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_GZIP_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
    canonical_json_bytes,
)
from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS

UBUNTU = b'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n'
DEBIAN = b'ID=debian\nVERSION_ID="12"\n'


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _tar(*members: tuple[str, bytes | str, str], pax_headers: dict[str, str] | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, value, kind in members:
            member = tarfile.TarInfo(path)
            member.pax_headers = dict(pax_headers or {})
            member.mode = 0o755 if kind == "directory" else 0o644
            if kind == "file":
                assert isinstance(value, bytes)
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.type = {
                    "symlink": tarfile.SYMTYPE,
                    "hardlink": tarfile.LNKTYPE,
                    "directory": tarfile.DIRTYPE,
                    "fifo": tarfile.FIFOTYPE,
                }[kind]
                member.linkname = value if isinstance(value, str) else ""
                archive.addfile(member)
    return output.getvalue()


def _file(path: str, payload: bytes = UBUNTU) -> tuple[str, bytes, str]:
    return path, payload, "file"


def _link(path: str, target: str, *, hard: bool = False) -> tuple[str, str, str]:
    return path, target, "hardlink" if hard else "symlink"


def _snapshot(
    tmp_path: Path,
    *layers: bytes,
    compressed: bool = True,
    architecture: str = "arm64",
    native: bool = False,
    wrong_diff_id: bool = False,
    index: bool = False,
):
    layout = tmp_path / "layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (layout / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}')

    def add(payload: bytes, media_type: str) -> Descriptor:
        descriptor = Descriptor(media_type=media_type, digest=_digest(payload), size=len(payload))
        (blobs / descriptor.digest.removeprefix("sha256:")).write_bytes(payload)
        return descriptor

    layer_descriptors = tuple(
        add(
            gzip.compress(payload, mtime=0) if compressed else payload,
            OCI_LAYER_GZIP_MEDIA_TYPE if compressed else OCI_LAYER_MEDIA_TYPE,
        )
        for payload in layers
    )
    config = add(
        canonical_json_bytes(
            {
                "architecture": architecture,
                "os": "linux",
                "rootfs": {
                    "type": "layers",
                    "diff_ids": [
                        "sha256:" + "0" * 64 if wrong_diff_id and ordinal == len(layers) - 1 else _digest(payload)
                        for ordinal, payload in enumerate(layers)
                    ],
                },
            }
        ),
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    root = add(
        canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
                "config": config.to_dict(),
                "layers": [item.to_dict() for item in layer_descriptors],
            }
        ),
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    if index:
        root = add(
            canonical_json_bytes(
                {
                    "schemaVersion": 2,
                    "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                    "manifests": [{**root.to_dict(), "platform": {"os": "linux", "architecture": architecture}}],
                }
            ),
            OCI_IMAGE_INDEX_MEDIA_TYPE,
        )
    (layout / "index.json").write_bytes(
        canonical_json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
                "manifests": [root.to_dict()],
            }
        )
    )
    cas = SourceCAS(tmp_path / "cas")
    source = LocalLayoutSource(layout, root.digest)
    snapshot = (
        source.snapshot(None, cas)
        if native
        else source.snapshot_acquisition(
            None,
            cas,
            platform=f"linux/{architecture}",
        )
    )
    return snapshot, cas


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_reads_verified_payload_and_binds_selected_graph(tmp_path, compressed, native):
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release")),
        compressed=compressed,
        native=native,
        architecture="amd64" if native else "arm64",
        index=True,
    )
    result = read_image_os_release(snapshot, cas)
    assert result.to_dict() == {
        "source_binding_digest": snapshot.binding_digest,
        "source_path": "/etc/os-release",
        "resolved_path": "/etc/os-release",
        "layer_ordinal": 0,
        "content_digest": _digest(UBUNTU),
        "os_id": "ubuntu",
        "version_id": "24.04",
    }


def test_etc_takes_exclusive_precedence(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release"), _file("usr/lib/os-release", DEBIAN)))
    assert read_image_os_release(snapshot, cas).os_id == "ubuntu"


@pytest.mark.parametrize(
    "upper",
    [
        _tar(_file("etc/.wh.os-release", b"")),
        _tar(_file("etc/.wh..wh..opq", b"")),
        _tar(_file(".wh.etc", b"")),
    ],
)
def test_whiteout_and_opaque_allow_absent_etc_fallback(tmp_path, upper):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release"), _file("usr/lib/os-release", DEBIAN)), upper)
    result = read_image_os_release(snapshot, cas)
    assert (result.source_path, result.os_id, result.layer_ordinal) == ("/usr/lib/os-release", "debian", 0)


@pytest.mark.parametrize(
    "upper",
    [
        _tar(_file(".wh.etc", b""), _file("etc/other", b"x")),
        _tar(_file("etc", b"replaced ancestor"), _file("etc/other", b"x")),
        _tar(_file("etc/.wh..wh..opq", b""), _file("etc/other", b"x")),
    ],
)
def test_deleted_lower_descendants_do_not_resurrect_after_synthetic_directory(tmp_path, upper):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release"), _file("usr/lib/os-release", DEBIAN)), upper)
    assert read_image_os_release(snapshot, cas).os_id == "debian"


def test_opaque_keeps_same_layer_visible_entries(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path, _tar(_file("etc/os-release", DEBIAN)), _tar(_file("etc/os-release"), _file("etc/.wh..wh..opq", b""))
    )
    result = read_image_os_release(snapshot, cas)
    assert (result.os_id, result.layer_ordinal) == ("ubuntu", 1)


def test_directory_update_preserves_lower_children(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release")), _tar(("etc", "", "directory")))
    assert read_image_os_release(snapshot, cas).layer_ordinal == 0


@pytest.mark.parametrize(
    "members,resolved",
    [
        ((_file("usr/lib/os-release"), _link("etc/os-release", "../usr/lib/os-release")), "/usr/lib/os-release"),
        ((_file("usr/lib/os-release"), _link("etc/os-release", "/usr/lib/os-release")), "/usr/lib/os-release"),
        ((_file("usr/etc/os-release"), _link("etc", "usr/etc")), "/usr/etc/os-release"),
        ((_file("release"), _link("etc/os-release", "../release")), "/release"),
        (
            (
                _file("usr/lib/os-release"),
                _link("aliases/release", "../usr/lib/os-release"),
                _link("etc/os-release", "../aliases/release"),
            ),
            "/usr/lib/os-release",
        ),
    ],
)
def test_safe_relative_absolute_and_ancestor_symlinks(tmp_path, members, resolved):
    snapshot, cas = _snapshot(tmp_path, _tar(*members))
    result = read_image_os_release(snapshot, cas)
    assert (result.source_path, result.resolved_path, result.os_id) == ("/etc/os-release", resolved, "ubuntu")


@pytest.mark.parametrize(
    "members",
    [
        (_link("etc/os-release", "missing"),),
        (_link("etc", "missing"),),
        (_link("etc/os-release", "os-release"),),
        (_link("etc/os-release", "/links/release"), _link("links/release", "/etc/os-release")),
        (_link("etc/os-release", "../../outside"),),
        (_file("release"), _link("etc/os-release", "/release/")),
        (_file("etc", b"not a directory"),),
        (("etc/os-release", "", "directory"),),
        (("etc/os-release", "", "fifo"),),
    ],
)
def test_present_invalid_etc_path_never_falls_back(tmp_path, members):
    snapshot, cas = _snapshot(tmp_path, _tar(*members, _file("usr/lib/os-release", DEBIAN)))
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_absent_leaf_below_existing_symlink_directory_can_fall_back(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path, _tar(("usr/etc", "", "directory"), _link("etc", "/usr/etc"), _file("usr/lib/os-release", DEBIAN))
    )
    assert read_image_os_release(snapshot, cas).source_path == "/usr/lib/os-release"


@pytest.mark.parametrize(
    "members",
    [
        (_file("carrier", UBUNTU), _link("etc/os-release", "carrier", hard=True), _file("carrier", DEBIAN)),
        (_link("etc/os-release", "carrier", hard=True), _file("carrier", UBUNTU), _file("carrier", DEBIAN)),
    ],
)
def test_hardlink_binds_payload_occurrence_not_final_target_path(tmp_path, members):
    snapshot, cas = _snapshot(tmp_path, _tar(*members), _tar(_file("carrier", DEBIAN)))
    result = read_image_os_release(snapshot, cas)
    assert (result.os_id, result.content_digest, result.layer_ordinal) == ("ubuntu", _digest(UBUNTU), 0)


@pytest.mark.parametrize("whiteout_first", [False, True])
@pytest.mark.parametrize("deleted_path", ["etc/os-release", "etc"])
def test_lower_only_whiteout_keeps_new_same_layer_os_bytes(tmp_path, whiteout_first, deleted_path):
    marker_path = "etc/.wh.os-release" if deleted_path == "etc/os-release" else ".wh.etc"
    visible = _file("etc/os-release")
    marker = _file(marker_path, b"")
    members = (marker, visible) if whiteout_first else (visible, marker)
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release", DEBIAN)), _tar(*members))
    result = read_image_os_release(snapshot, cas)
    assert (result.os_id, result.content_digest, result.layer_ordinal) == ("ubuntu", _digest(UBUNTU), 1)


def test_late_whiteout_preserves_same_layer_hardlink_os_payload(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release", DEBIAN)),
        _tar(_file("carrier"), _link("etc/os-release", "carrier", hard=True), _file(".wh.carrier", b"")),
    )
    result = read_image_os_release(snapshot, cas)
    assert (result.os_id, result.content_digest, result.layer_ordinal) == ("ubuntu", _digest(UBUNTU), 1)


def test_late_parent_whiteout_cannot_resurrect_lower_preferred_os_file(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release", DEBIAN), _file("usr/lib/os-release")),
        _tar(_file("etc/new-file", b"new upper data"), _file(".wh.etc", b"")),
    )
    result = read_image_os_release(snapshot, cas)
    assert (result.source_path, result.os_id, result.layer_ordinal) == ("/usr/lib/os-release", "ubuntu", 0)


def test_cross_layer_hardlink_is_explicitly_unsupported(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("carrier")), _tar(_link("etc/os-release", "carrier", hard=True)))
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_repeated_layer_occurrences_keep_last_visible_origin(tmp_path):
    layer = _tar(_file("etc/os-release"))
    snapshot, cas = _snapshot(tmp_path, layer, _tar(_file("etc/os-release", DEBIAN)), layer)
    assert snapshot.layers[0].descriptor == snapshot.layers[2].descriptor
    assert read_image_os_release(snapshot, cas).layer_ordinal == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"NAME=Ubuntu\nVERSION_ID=24.04\n",
        b"ID=ubuntu\n",
        b"ID_LIKE=ubuntu\nVERSION_ID=24.04\n",
        b"ID=ubuntu\nID=debian\nVERSION_ID=24.04\n",
        b"ID=ubuntu\nVERSION_ID=24.04\nVERSION_ID=12\n",
        b"ID=Ubuntu\nVERSION_ID=24.04\n",
        b"ID=ubuntu\nVERSION_ID=''\n",
        b"ID=ubuntu\nVERSION_ID=24/04\n",
        b"ID='ub'untu\nVERSION_ID=24.04\n",
        b'ID="ubuntu\nVERSION_ID=24.04\n',
        b"ID=$(echo ubuntu)\nVERSION_ID=24.04\n",
        b'ID="${DISTRO}"\nVERSION_ID=24.04\n',
        b"ID=ubuntu;true\nVERSION_ID=24.04\n",
        b"export ID=ubuntu\nVERSION_ID=24.04\n",
        b"ID=ubuntu\nVERSION_ID=24.04\nNAME=\xff\n",
        b"ID=ubuntu\nVERSION_ID=24.04\n\x00",
        b"ID=ubuntu\r\nVERSION_ID=24.04\r\n",
        b"ID=ubuntu\nVERSION_ID=24.04\nNAME=bad\x1bname\n",
    ],
)
def test_invalid_identity_and_shell_syntax_are_not_inferred_or_evaluated(tmp_path, payload):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release", payload), _file("usr/lib/os-release", DEBIAN)))
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_quoted_utf8_comments_and_literal_escaped_shell_characters_are_data(tmp_path):
    payload = (
        b"# Comment\n\nID='ubuntu'\nVERSION_ID=24.04\n"
        + 'PRETTY_NAME="Ubuntu édition"\n'.encode()
        + b'NOTES="literal \\$ \\` \\" \\\\"\n'
        + b"SINGLE='$(not a command)'\nESCAPED=some\\ text\n"
    )
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release", payload)))
    result = read_image_os_release(snapshot, cas)
    assert (result.os_id, result.version_id, result.content_digest) == ("ubuntu", "24.04", _digest(payload))


def test_missing_both_paths_fails(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("unrelated", b"data")))
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_every_layer_diffid_is_verified_even_after_os_file_found(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release")),
        _tar(_file("unrelated", b"data")),
        native=True,
        architecture="amd64",
        wrong_diff_id=True,
    )
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_corrupt_later_layer_is_verified_even_when_selected_file_is_in_first_layer(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release")), _tar(_file("other", b"data")))
    descriptor = snapshot.layers[1].descriptor
    target = tmp_path / "cas" / "blobs" / "sha256" / descriptor.digest.removeprefix("sha256:")
    payload = target.read_bytes()
    target.chmod(0o600)
    target.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    target.chmod(0o400)
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_whiteout_then_visible_child_uses_new_layer_identity(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release", DEBIAN)),
        _tar(_file(".wh.etc", b""), _file("etc/os-release")),
    )
    result = read_image_os_release(snapshot, cas)
    assert (result.os_id, result.layer_ordinal) == ("ubuntu", 1)


def test_root_opacity_hides_all_lower_os_paths(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file("etc/os-release"), _file("usr/lib/os-release", DEBIAN)),
        _tar(_file(".wh..wh..opq", b""), _file("unrelated", b"data")),
    )
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


@pytest.mark.parametrize("member", ["layer", "config", "root"])
def test_corrupted_source_cas_bytes_fail_closed(tmp_path, member):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release")), index=True)
    descriptor = {
        "layer": snapshot.layers[0].descriptor,
        "config": snapshot.config.descriptor,
        "root": snapshot.root.descriptor,
    }[member]
    target = tmp_path / "cas" / "blobs" / "sha256" / descriptor.digest.removeprefix("sha256:")
    content = target.read_bytes()
    target.chmod(0o600)
    target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    target.chmod(0o400)
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas)


def test_forged_snapshot_graph_is_not_authority(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release")))
    bad = "sha256:" + "1" * 64
    graph = replace(
        snapshot.image,
        config=replace(snapshot.image.config, diff_ids=(bad,)),
        layers=(replace(snapshot.image.layers[0], diff_id=bad),),
    )
    forged = replace(snapshot, image=graph)
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(forged, cas)


def test_forged_platform_is_rejected_against_actual_config_bytes(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release")), index=True)
    graph = replace(
        snapshot.image,
        config=replace(snapshot.image.config, architecture="amd64"),
        platform=replace(snapshot.image.platform, architecture="amd64"),
    )
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(replace(snapshot, image=graph), cas)


def test_foreign_cas_is_rejected(tmp_path):
    snapshot, _cas = _snapshot(tmp_path, _tar(_file("etc/os-release")))
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, SourceCAS(tmp_path / "other-cas"))


def test_retained_xattrs_and_repeated_occurrence_metadata_consume_shared_budget(tmp_path):
    layer = _tar(_file("etc/os-release"), pax_headers={"SCHILY.xattr.user.note": "x" * 4096})
    snapshot, cas = _snapshot(tmp_path, layer, layer)
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(
            snapshot,
            cas,
            limits=replace(DEFAULT_OS_READ_LIMITS, max_metadata_bytes=12_000),
        )


def test_long_pax_path_is_read_from_real_tar_metadata(tmp_path):
    path = "release/" + "x" * 120
    snapshot, cas = _snapshot(
        tmp_path,
        _tar(_file(path), _link("etc/os-release", "/" + path)),
    )
    result = read_image_os_release(snapshot, cas)
    assert (result.resolved_path, result.content_digest) == ("/" + path, _digest(UBUNTU))


def test_exact_file_and_total_decompression_boundaries_include_origin_replay(tmp_path):
    layer = _tar(_file("etc/os-release"))
    snapshot, cas = _snapshot(tmp_path, layer)
    limits = replace(DEFAULT_OS_READ_LIMITS, max_file_bytes=len(UBUNTU), max_uncompressed_bytes=2 * len(layer))
    assert read_image_os_release(snapshot, cas, limits=limits).os_id == "ubuntu"
    for too_small in (
        replace(limits, max_file_bytes=len(UBUNTU) - 1),
        replace(limits, max_uncompressed_bytes=2 * len(layer) - 1),
    ):
        with pytest.raises(ArtifactValidationError):
            read_image_os_release(snapshot, cas, limits=too_small)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_entries": 2},
        {"max_members": 1},
        {"max_metadata_bytes": 500},
        {"max_path_bytes": 14},
        {"timeout_seconds": 1e-300},
    ],
)
def test_whole_image_resource_limits_reject_real_inputs(tmp_path, changes):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("etc/os-release")))
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas, limits=replace(DEFAULT_OS_READ_LIMITS, **changes))


def test_layer_occurrence_limit_is_not_deduplicated(tmp_path):
    layer = _tar(_file("etc/os-release"))
    snapshot, cas = _snapshot(tmp_path, layer, layer)
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas, limits=replace(DEFAULT_OS_READ_LIMITS, max_layers=1))


def test_exact_entry_path_and_link_depth_boundaries(tmp_path):
    snapshot, cas = _snapshot(tmp_path, _tar(_file("release"), _link("etc/os-release", "/release")))
    limits = replace(
        DEFAULT_OS_READ_LIMITS, max_entries=4, max_path_bytes=len("/etc/os-release"), max_symlink_traversals=1
    )
    assert read_image_os_release(snapshot, cas, limits=limits).resolved_path == "/release"


def test_symlink_depth_budget_applies_to_whole_resolution(tmp_path):
    snapshot, cas = _snapshot(
        tmp_path, _tar(_file("release"), _link("alias", "/release"), _link("etc/os-release", "/alias"))
    )
    assert (
        read_image_os_release(snapshot, cas, limits=replace(DEFAULT_OS_READ_LIMITS, max_symlink_traversals=2)).os_id
        == "ubuntu"
    )
    with pytest.raises(ArtifactValidationError):
        read_image_os_release(snapshot, cas, limits=replace(DEFAULT_OS_READ_LIMITS, max_symlink_traversals=1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_layers", 0),
        ("max_entries", -1),
        ("max_members", True),
        ("max_metadata_bytes", 0),
        ("max_file_bytes", 1.5),
        ("max_uncompressed_bytes", 0),
        ("max_path_bytes", False),
        ("max_symlink_traversals", 0),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
    ],
)
def test_limits_require_finite_positive_policy(field, value):
    with pytest.raises(ArtifactValidationError):
        OSReadLimits(**{field: value})
