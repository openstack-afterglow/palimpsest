from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from palimpsest_local.errors import ArtifactValidationError, UnsupportedPlatformError
from palimpsest_local.oci_convert import (
    FilesystemCandidate,
    TarTranslationLimits,
    _assert_device_nodes_inert,
    _mountinfo_entry,
    _squashfs_root_arguments,
    _verify_merged_tree,
    build_layer_filesystem,
    preflight_oci_filesystem_probe,
    translate_oci_tar_to_overlay_tar,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _tar(*members: tarfile.TarInfo) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for member in members:
            payload = io.BytesIO(b"x" * member.size) if member.isreg() else None
            archive.addfile(member, payload)
    return output.getvalue()


@pytest.mark.parametrize("name", [".wh.", ".wh..", ".wh...", "d/.wh..."])
def test_invalid_derived_whiteout_targets_are_rejected(name: str):
    member = tarfile.TarInfo(name)
    member.type = tarfile.REGTYPE
    with pytest.raises(ArtifactValidationError, match="Invalid OCI whiteout target"):
        translate_oci_tar_to_overlay_tar(_tar(member))


def test_root_opaque_whiteout_is_explicitly_translated():
    member = tarfile.TarInfo(".wh..wh..opq")
    member.type = tarfile.REGTYPE
    translated = translate_oci_tar_to_overlay_tar(_tar(member))
    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        root = archive.getmember(".")
        assert root.isdir()
        assert root.pax_headers["SCHILY.xattr.trusted.overlay.opaque"] == "y"


def test_later_whiteout_wins_over_an_earlier_opaque_directory_marker():
    opaque = tarfile.TarInfo("d/.wh..wh..opq")
    whiteout = tarfile.TarInfo(".wh.d")

    translated = translate_oci_tar_to_overlay_tar(_tar(opaque, whiteout))

    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        actual = archive.getmember("d")
        assert actual.ischr()
        assert "SCHILY.xattr.trusted.overlay.opaque" not in actual.pax_headers


def test_later_directory_metadata_preserves_an_earlier_opaque_marker():
    opaque = tarfile.TarInfo("d/.wh..wh..opq")
    directory = tarfile.TarInfo("d")
    directory.type = tarfile.DIRTYPE
    directory.mode = 0o700

    translated = translate_oci_tar_to_overlay_tar(_tar(opaque, directory))

    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        actual = archive.getmember("d")
        assert actual.isdir()
        assert actual.mode == 0o700
        assert actual.pax_headers["SCHILY.xattr.trusted.overlay.opaque"] == "y"


def test_source_cannot_inject_overlay_control_xattr():
    member = tarfile.TarInfo("owned")
    member.type = tarfile.DIRTYPE
    member.pax_headers = {"SCHILY.xattr.trusted.overlay.opaque": "y"}
    with pytest.raises(ArtifactValidationError, match="reserved trusted xattr"):
        translate_oci_tar_to_overlay_tar(_tar(member))

    trusted = tarfile.TarInfo("owned")
    trusted.type = tarfile.DIRTYPE
    trusted.pax_headers = {"SCHILY.xattr.trusted.user_control": "value"}
    with pytest.raises(ArtifactValidationError, match="reserved trusted xattr"):
        translate_oci_tar_to_overlay_tar(_tar(trusted))

    libarchive = tarfile.TarInfo("owned")
    libarchive.type = tarfile.DIRTYPE
    libarchive.pax_headers = {"LIBARCHIVE.xattr.trusted.overlay.opaque": "eQ=="}
    with pytest.raises(ArtifactValidationError, match="LIBARCHIVE xattr"):
        translate_oci_tar_to_overlay_tar(_tar(libarchive))

    pseudo_injection = tarfile.TarInfo("owned")
    pseudo_injection.type = tarfile.DIRTYPE
    pseudo_injection.pax_headers = {"SCHILY.xattr.user.safe\n/ x trusted.overlay.opaque": "y"}
    with pytest.raises(ArtifactValidationError, match="invalid xattr name"):
        translate_oci_tar_to_overlay_tar(_tar(pseudo_injection))


@pytest.mark.parametrize(
    "name,member_type,size,pax_headers",
    [
        (".wh.victim", tarfile.SYMTYPE, 0, {}),
        (".wh.victim", tarfile.REGTYPE, 1, {}),
        ("d/.wh..wh..opq", tarfile.DIRTYPE, 0, {}),
        (".wh.victim", tarfile.REGTYPE, 0, {"comment": "control"}),
    ],
)
def test_malformed_whiteout_control_markers_are_rejected(
    name: str,
    member_type: bytes,
    size: int,
    pax_headers: dict[str, str],
):
    marker = tarfile.TarInfo(name)
    marker.type = member_type
    marker.size = size
    marker.pax_headers = pax_headers
    with pytest.raises(ArtifactValidationError, match="whiteout marker"):
        translate_oci_tar_to_overlay_tar(_tar(marker))


def test_long_structural_pax_path_is_allowed_for_valid_whiteout():
    parent = "p" * 240
    marker = tarfile.TarInfo(f"{parent}/.wh.victim")
    marker.type = tarfile.REGTYPE
    translated = translate_oci_tar_to_overlay_tar(_tar(marker))
    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        assert archive.getmember(f"{parent}/victim").ischr()


def test_reserved_root_path_is_exact_not_a_prefix():
    reserved = tarfile.TarInfo(".palimpsest/secret")
    reserved.type = tarfile.REGTYPE
    with pytest.raises(ArtifactValidationError, match="Reserved path"):
        translate_oci_tar_to_overlay_tar(_tar(reserved))

    allowed = tarfile.TarInfo(".palimpsest-data")
    allowed.type = tarfile.REGTYPE
    translated = translate_oci_tar_to_overlay_tar(_tar(allowed))
    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        assert archive.getnames() == [".", ".palimpsest-data"]


def test_invalid_capability_blob_is_rejected():
    member = tarfile.TarInfo("cap")
    member.type = tarfile.REGTYPE
    member.pax_headers = {"SCHILY.xattr.security.capability": "cap_data"}
    with pytest.raises(ArtifactValidationError, match="Invalid security.capability"):
        translate_oci_tar_to_overlay_tar(_tar(member))


@pytest.mark.parametrize(
    "limits",
    [
        TarTranslationLimits(max_members=0),
        TarTranslationLimits(max_path_bytes=1),
        TarTranslationLimits(max_regular_file_bytes=0),
        TarTranslationLimits(max_total_regular_bytes=0),
        TarTranslationLimits(max_pax_bytes_per_member=0),
    ],
)
def test_archive_limits_fail_before_packer(limits: TarTranslationLimits):
    member = tarfile.TarInfo("file")
    member.type = tarfile.REGTYPE
    member.size = 1
    member.pax_headers = {"comment": "metadata"}
    with pytest.raises(ArtifactValidationError, match="limit"):
        translate_oci_tar_to_overlay_tar(_tar(member), limits=limits)


def test_dangling_and_chained_hardlinks_are_rejected():
    dangling = tarfile.TarInfo("dangling")
    dangling.type = tarfile.LNKTYPE
    dangling.linkname = "missing"
    with pytest.raises(ArtifactValidationError, match="same-layer regular file"):
        translate_oci_tar_to_overlay_tar(_tar(dangling))

    first = tarfile.TarInfo("first")
    first.type = tarfile.LNKTYPE
    first.linkname = "second"
    second = tarfile.TarInfo("second")
    second.type = tarfile.LNKTYPE
    second.linkname = "first"
    with pytest.raises(ArtifactValidationError, match="same-layer regular file"):
        translate_oci_tar_to_overlay_tar(_tar(first, second))


@pytest.mark.parametrize("target", ["/usr/bin/env", "../../shared/tool"])
def test_absolute_and_parent_relative_symlinks_remain_valid_oci_metadata(target: str):
    link = tarfile.TarInfo("bin/tool")
    link.type = tarfile.SYMTYPE
    link.linkname = target
    translated = translate_oci_tar_to_overlay_tar(_tar(link))
    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        assert archive.getmember("bin/tool").linkname == target


def test_structural_pax_cannot_restore_noncanonical_paths_or_hardlinks():
    member = tarfile.TarInfo("f")
    member.type = tarfile.REGTYPE
    member.pax_headers = {"path": "a/../f"}
    with pytest.raises(ArtifactValidationError, match="Invalid member path"):
        translate_oci_tar_to_overlay_tar(_tar(member))

    target = tarfile.TarInfo("target")
    target.type = tarfile.REGTYPE
    link = tarfile.TarInfo("link")
    link.type = tarfile.LNKTYPE
    link.linkname = "target"
    link.pax_headers = {"linkpath": "a/../target"}
    with pytest.raises(ArtifactValidationError, match="Invalid member path"):
        translate_oci_tar_to_overlay_tar(_tar(target, link))


def test_explicit_root_metadata_is_preserved():
    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    root.mode = 0o700
    root.uid = 123
    root.gid = 456
    root.mtime = 7
    root.pax_headers = {"SCHILY.xattr.user.root": "value"}
    translated = translate_oci_tar_to_overlay_tar(_tar(root))
    with tarfile.open(fileobj=io.BytesIO(translated), mode="r:*") as archive:
        actual = archive.getmember(".")
        assert (actual.mode, actual.uid, actual.gid, actual.mtime) == (0o700, 123, 456, 7)
        assert actual.pax_headers["SCHILY.xattr.user.root"] == "value"


def test_squashfs_root_arguments_preserve_root_inode_and_xattr():
    root = tarfile.TarInfo(".")
    root.type = tarfile.DIRTYPE
    root.mode = 0o750
    root.uid = 123
    root.gid = 456
    root.mtime = 7
    root.pax_headers = {"SCHILY.xattr.user.root": "value"}
    translated = translate_oci_tar_to_overlay_tar(_tar(root))
    assert _squashfs_root_arguments(translated) == [
        "-root-mode",
        "750",
        "-root-uid",
        "123",
        "-root-gid",
        "456",
        "-root-time",
        "7",
        "-p",
        "/ x user.root=0sdmFsdWU=",
    ]


@pytest.mark.parametrize(("field", "value"), [("uid", -1), ("gid", 2**32)])
def test_out_of_range_numeric_metadata_is_typed_failure(field: str, value: int):
    member = tarfile.TarInfo("bad")
    member.type = tarfile.REGTYPE
    setattr(member, field, value)
    with pytest.raises(ArtifactValidationError, match=f"{field} metadata"):
        translate_oci_tar_to_overlay_tar(_tar(member))


def test_nonfinite_pax_mtime_is_typed_failure():
    member = tarfile.TarInfo("bad")
    member.type = tarfile.REGTYPE
    member.pax_headers = {"mtime": "nan"}
    with pytest.raises(ArtifactValidationError, match="mtime metadata"):
        translate_oci_tar_to_overlay_tar(_tar(member))


def test_compressed_blob_requires_upstream_decompression_and_diff_id_verification():
    member = tarfile.TarInfo("file")
    member.type = tarfile.REGTYPE
    compressed = gzip.compress(_tar(member), mtime=0)
    with pytest.raises(ArtifactValidationError, match="decompressed and diff-id verified"):
        translate_oci_tar_to_overlay_tar(compressed)


def test_corrupt_middle_header_is_not_accepted_as_partial_archive():
    first = tarfile.TarInfo("a")
    first.type = tarfile.DIRTYPE
    second = tarfile.TarInfo("b")
    second.type = tarfile.DIRTYPE
    payload = bytearray(_tar(first, second))
    payload[tarfile.BLOCKSIZE] ^= 1
    with pytest.raises(ArtifactValidationError, match="checksum"):
        translate_oci_tar_to_overlay_tar(bytes(payload))


def test_candidate_build_requires_an_explicit_candidate():
    with pytest.raises(TypeError):
        build_layer_filesystem(b"not-used")  # type: ignore[call-arg]
    with patch("sys.platform", "darwin"):
        with pytest.raises(UnsupportedPlatformError):
            build_layer_filesystem(b"not-used", target_fs=FilesystemCandidate.SQUASHFS)


def test_preflight_rejects_missing_effective_capability_before_probe_mutation(tmp_path: Path):
    status = tmp_path / "status"
    status.write_text("CapEff:\t0000000000000000\n", encoding="utf-8")
    with (
        patch("sys.platform", "linux"),
        patch("os.geteuid", return_value=0),
        patch("shutil.which", return_value="/usr/bin/tool"),
        patch("palimpsest_local.oci_convert.Path.is_file", return_value=True),
        patch("palimpsest_local.oci_convert._effective_linux_capabilities", return_value=set()),
    ):
        with pytest.raises(UnsupportedPlatformError, match="CAP_SYS_ADMIN"):
            preflight_oci_filesystem_probe(FilesystemCandidate.SQUASHFS)


def test_preflight_binds_packer_version_to_stable_hash():
    with (
        patch("sys.platform", "linux"),
        patch("os.geteuid", return_value=0),
        patch("shutil.which", return_value="/usr/bin/tool"),
        patch("palimpsest_local.oci_convert.Path.is_file", return_value=True),
        patch("palimpsest_local.oci_convert._effective_linux_capabilities", return_value={21}),
        patch("palimpsest_local.oci_convert._read_packer_version", return_value="4.6.1"),
        patch("palimpsest_local.oci_convert._sha256_file", side_effect=["before", "after"]),
    ):
        with pytest.raises(ArtifactValidationError, match="changed while checking its version"):
            preflight_oci_filesystem_probe(FilesystemCandidate.SQUASHFS)


def test_mountinfo_parser_combines_vfs_and_superblock_options(tmp_path: Path):
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:32 / {mountpoint} ro,nodev,nosuid,noexec - squashfs /dev/loop0 ro,errors=continue\n",
        encoding="utf-8",
    )
    filesystem, options = _mountinfo_entry(mountpoint, mountinfo)
    assert filesystem == "squashfs"
    assert {"ro", "nodev", "nosuid", "noexec"}.issubset(options)


def test_mountinfo_distinguishes_absent_malformed_and_duplicate_entries(tmp_path: Path):
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    mountinfo = tmp_path / "mountinfo"

    mountinfo.write_text("", encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="not present"):
        _mountinfo_entry(mountpoint, mountinfo)

    mountinfo.write_text("malformed\n", encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="malformed"):
        _mountinfo_entry(mountpoint, mountinfo)

    entry = f"36 25 0:32 / {mountpoint} ro,nodev - squashfs /dev/loop0 ro\n"
    mountinfo.write_text(entry * 2, encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="duplicate"):
        _mountinfo_entry(mountpoint, mountinfo)


def test_device_denial_probe_rejects_symlink_before_open(tmp_path: Path):
    target = tmp_path / "host-target"
    target.write_bytes(b"host")
    base = tmp_path / "base"
    overlay = tmp_path / "overlay"
    base.mkdir()
    overlay.mkdir()
    (base / "char_dev").symlink_to(target)
    (overlay / "char_dev").symlink_to(target)
    with patch("os.open") as open_spy:
        with pytest.raises(ArtifactValidationError, match="metadata is unsafe"):
            _assert_device_nodes_inert(base, overlay)
    open_spy.assert_not_called()


@pytest.mark.parametrize("entry", [{}, {"type": "bogus"}, {"type": ["file"]}])
def test_merged_receipt_rejects_missing_or_unknown_entry_types(tmp_path: Path, entry: dict):
    (tmp_path / "f").write_bytes(b"data")
    ok, message = _verify_merged_tree(tmp_path, {"f": entry})
    assert ok is False
    assert "type" in message


def test_merged_receipt_requires_exact_file_metadata(tmp_path: Path):
    (tmp_path / "f").write_bytes(b"data")
    ok, message = _verify_merged_tree(tmp_path, {"f": {"type": "file"}})
    assert ok is False
    assert "required metadata" in message


@pytest.mark.parametrize(
    "metadata",
    [
        {"link_target": 1},
        {"link_target": "../escape"},
        {"link_target": "target", "hardlink_target": "target"},
        {"inode_group": 1},
    ],
)
def test_merged_receipt_rejects_invalid_optional_hardlink_metadata(tmp_path: Path, metadata: dict):
    path = tmp_path / "f"
    path.write_bytes(b"data")
    entry = {
        "type": "file",
        "mode": path.stat().st_mode,
        "uid": path.stat().st_uid,
        "gid": path.stat().st_gid,
        "mtime_ns": path.stat().st_mtime_ns,
        "xattrs_b64": {},
        "sha256": "0" * 64,
        **metadata,
    }
    ok, message = _verify_merged_tree(tmp_path, {".": entry, "f": entry})
    assert ok is False
    assert "metadata" in message


def test_privileged_workflow_is_non_skipping_and_runtime_remains_inactive():
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    job = workflow.split("  oci-fs-proof:\n", 1)[1].split("\n  unit-macos:", 1)[0]
    assert "\n    if:" not in job
    assert 'PALIMPSEST_REQUIRE_OCI_FS: "1"' in job
    assert "-m oci_fs tests/oci_fs" in job
    assert "Rebuild selected SquashFS in a separate process" in job
    assert 'cmp "$RUNNER_TEMP/oci-fs-evidence/squashfs.json"' in job
    assert "squashfs-replay.json" in job
    assert "if-no-files-found: error" in job

    source_root = REPOSITORY_ROOT / "src" / "palimpsest_local"
    importers = []
    for source_file in source_root.glob("*.py"):
        if source_file.name == "oci_convert.py":
            continue
        source = source_file.read_text(encoding="utf-8")
        if "oci_convert" in source:
            importers.append(source_file.name)
    assert importers == []
