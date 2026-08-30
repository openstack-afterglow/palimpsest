"""Tests for OCI layer filesystem translation, lowerdir budget, and OverlayFS probe."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from palimpsest_local.errors import ArtifactValidationError, UnsupportedPlatformError
from palimpsest_local.oci_convert import (
    SELECTED_OCI_LAYER_FILESYSTEM,
    FilesystemCandidate,
    _probe_fixture_digest,
    _verify_merged_tree,
    build_layer_filesystem,
    check_lowerdir_page_budget,
    check_platform_support,
    get_oci_layer_filesystem,
    preflight_oci_filesystem_probe,
    probe_oci_filesystem_candidate,
    to_base36,
    translate_oci_tar_to_overlay_tar,
)
from palimpsest_local.oci_converter import stage_layer
from palimpsest_local.oci_image import OCIImageRef
from palimpsest_local.oci_packer import PackedSquashFSReceipt, pack_staged_squashfs
from palimpsest_local.oci_provenance import (
    OCI_IMAGE_CONFIG_MEDIA_TYPE,
    OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPE,
    Descriptor,
)
from palimpsest_local.oci_source import LocalLayoutSource, SourceCAS

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "oci-root"

_TRANSLATED_FIXTURE_SHA256 = {
    "base_layer.tar": "4fec3742dd8a0a1fa483d2cd4fccbb04d1686591c1a4913a7665d45a18966cd6",
    "leaf_layer.tar": "85ffae029be971381794177129eb6973d408fc018b05de85e5809e94a41bbf79",
}


def _descriptor(payload: bytes, media_type: str) -> Descriptor:
    return Descriptor(
        media_type=media_type,
        digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size=len(payload),
    )


def _pack_staged_fixture(
    root: Path,
    payload: bytes,
    *,
    packer: Path,
    packer_sha256: str,
) -> tuple[str, PackedSquashFSReceipt]:
    layout = root / "layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
    layer = _descriptor(payload, OCI_LAYER_MEDIA_TYPE)
    (blobs / layer.digest.removeprefix("sha256:")).write_bytes(payload)

    def add_json(value: object, media_type: str) -> Descriptor:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        descriptor = _descriptor(encoded, media_type)
        (blobs / descriptor.digest.removeprefix("sha256:")).write_bytes(encoded)
        return descriptor

    diff_id = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    config = add_json(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        },
        OCI_IMAGE_CONFIG_MEDIA_TYPE,
    )
    manifest = add_json(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            "config": config.to_dict(),
            "layers": [layer.to_dict()],
        },
        OCI_IMAGE_MANIFEST_MEDIA_TYPE,
    )
    (layout / "index.json").write_text(
        json.dumps({"schemaVersion": 2, "manifests": [manifest.to_dict()]}, separators=(",", ":")),
        encoding="utf-8",
    )
    cas = SourceCAS(root / "cas")
    reference = OCIImageRef(
        registry="registry.example.com",
        repository="fixture/oci-root",
        requested_reference="registry.example.com/fixture/oci-root:latest",
    )
    image = LocalLayoutSource.parse(f"oci-layout://{layout}@{manifest.digest}").snapshot(reference, cas)
    image_digest = hashlib.sha256()
    with cas.lease_layer(image, 0) as source, stage_layer(source) as staged:
        with pack_staged_squashfs(
            staged,
            packer_path=packer,
            expected_packer_sha256=packer_sha256,
        ) as packed:
            receipt = packed.receipt
            for chunk in packed.chunks():
                image_digest.update(chunk)
    return image_digest.hexdigest(), receipt


def test_non_linux_rejection_before_subprocess():
    """Verify non-Linux platform rejects OCI filesystem operations BEFORE any subprocess execution."""
    with patch("sys.platform", "darwin"):
        with patch("subprocess.run") as mock_run:
            with pytest.raises(UnsupportedPlatformError, match="unsupported"):
                check_platform_support()

            with pytest.raises(UnsupportedPlatformError, match="unsupported"):
                build_layer_filesystem(b"dummy", target_fs=FilesystemCandidate.SQUASHFS)

            with pytest.raises(UnsupportedPlatformError, match="unsupported"):
                preflight_oci_filesystem_probe(FilesystemCandidate.SQUASHFS)

            mock_run.assert_not_called()


def test_base36_conversion():
    """Verify base36 index encoding for lowerdir paths."""
    assert to_base36(0) == "0"
    assert to_base36(1) == "1"
    assert to_base36(9) == "9"
    assert to_base36(10) == "a"
    assert to_base36(35) == "z"
    assert to_base36(36) == "10"
    assert to_base36(127) == "3j"

    with pytest.raises(ValueError, match="non-negative"):
        to_base36(-1)


def test_lowerdir_page_budget_128_layers():
    """Verify 128-lowerdir option string stays well below 75% page-size budget."""
    is_valid, length, budget = check_lowerdir_page_budget(128)
    assert is_valid is True
    assert length > 0
    assert length <= budget
    # E.g. ~770 bytes for 128 layers vs ~3072 byte budget for 4KiB page size
    assert length < budget * 0.5


def test_tar_translation_whiteouts_and_xattrs():
    """Verify OCI tar translation converts whiteouts and PAX xattrs correctly."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # /dir1
        d1 = tarfile.TarInfo("dir1")
        d1.type = tarfile.DIRTYPE
        tar.addfile(d1)

        # /dir1/.wh.deleted.txt (ordinary whiteout)
        wh = tarfile.TarInfo("dir1/.wh.deleted.txt")
        wh.type = tarfile.REGTYPE
        tar.addfile(wh)

        # /dir_opq/.wh..wh..opq (opaque whiteout)
        opq = tarfile.TarInfo("dir_opq/.wh..wh..opq")
        opq.type = tarfile.REGTYPE
        tar.addfile(opq)

    translated_bytes = translate_oci_tar_to_overlay_tar(in_buf.getvalue())
    out_buf = io.BytesIO(translated_bytes)

    with tarfile.open(fileobj=out_buf, mode="r:*") as out_tar:
        names = set(out_tar.getnames())
        assert "dir1/deleted.txt" in names
        assert ".wh.deleted.txt" not in names
        assert ".wh..wh..opq" not in names
        assert "dir_opq" in names

        # Check character device 0:0 for ordinary whiteout
        del_info = out_tar.getmember("dir1/deleted.txt")
        assert del_info.ischr()
        assert del_info.devmajor == 0
        assert del_info.devminor == 0

        assert "SCHILY.xattr.trusted.overlay.opaque" not in (del_info.pax_headers or {})

        # Check opaque xattr on parent directory
        opq_info = out_tar.getmember("dir_opq")
        assert opq_info.pax_headers.get("SCHILY.xattr.trusted.overlay.opaque") == "y"


def test_tar_translation_same_layer_whiteout_overridden_by_real_target():
    """Verify same-layer ordinary whiteout is omitted when a real target wins later in the layer."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # Ordinary whiteout .wh.dir_to_file
        wh = tarfile.TarInfo(".wh.dir_to_file")
        wh.type = tarfile.REGTYPE
        tar.addfile(wh)

        # Followed by real regular file dir_to_file
        f = tarfile.TarInfo("dir_to_file")
        f.type = tarfile.REGTYPE
        f.size = 5
        tar.addfile(f, io.BytesIO(b"hello"))

    translated_bytes = translate_oci_tar_to_overlay_tar(in_buf.getvalue())
    out_buf = io.BytesIO(translated_bytes)

    with tarfile.open(fileobj=out_buf, mode="r:*") as out_tar:
        members = [m for m in out_tar.getmembers() if m.name == "dir_to_file"]
        assert len(members) == 1, f"Expected exactly 1 member for dir_to_file, found {len(members)}"
        assert members[0].isreg(), "dir_to_file should be a regular file, not a whiteout character device"
        assert not members[0].ischr(), "Whiteout character device must be omitted when real target wins"


def test_tar_translation_file_to_directory_transition_synthesizes_parents():
    """Verify file-to-directory transition synthesizes parent directory entries when absent."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # Nested file without explicit parent directory header
        f = tarfile.TarInfo("file_to_dir/nested.txt")
        f.type = tarfile.REGTYPE
        f.size = 4
        tar.addfile(f, io.BytesIO(b"test"))

    translated_bytes = translate_oci_tar_to_overlay_tar(in_buf.getvalue())
    out_buf = io.BytesIO(translated_bytes)

    with tarfile.open(fileobj=out_buf, mode="r:*") as out_tar:
        names = out_tar.getnames()
        assert "file_to_dir" in names, "Parent directory file_to_dir should be synthesized"
        assert "file_to_dir/nested.txt" in names

        parent_info = out_tar.getmember("file_to_dir")
        assert parent_info.isdir(), "file_to_dir must be a directory entry"


def test_tar_translation_forward_hardlink_emitted_after_target():
    """Verify forward hardlink is emitted after its regular target file."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        # Forward hardlink forward_link_1.txt -> forward_link_2.txt
        lnk = tarfile.TarInfo("forward_link_1.txt")
        lnk.type = tarfile.LNKTYPE
        lnk.linkname = "forward_link_2.txt"
        tar.addfile(lnk)

        # Followed by target regular file forward_link_2.txt
        target = tarfile.TarInfo("forward_link_2.txt")
        target.type = tarfile.REGTYPE
        target.size = 4
        tar.addfile(target, io.BytesIO(b"data"))

    translated_bytes = translate_oci_tar_to_overlay_tar(in_buf.getvalue())
    out_buf = io.BytesIO(translated_bytes)

    with tarfile.open(fileobj=out_buf, mode="r:*") as out_tar:
        members = out_tar.getmembers()
        names = [m.name for m in members]
        assert "forward_link_1.txt" in names
        assert "forward_link_2.txt" in names

        idx_target = names.index("forward_link_2.txt")
        idx_link = names.index("forward_link_1.txt")
        assert idx_target < idx_link, f"Target ({idx_target}) must be emitted before forward hardlink ({idx_link})"


def test_tar_translation_leaf_fixture_structure():
    """Verify translated leaf_layer.tar fixture includes correct dir_to_file, synthesized file_to_dir, and ordered hardlinks."""
    leaf_tar_path = FIXTURES_DIR / "leaf_layer.tar"
    assert leaf_tar_path.is_file()

    leaf_tar = leaf_tar_path.read_bytes()
    translated_bytes = translate_oci_tar_to_overlay_tar(leaf_tar)
    out_buf = io.BytesIO(translated_bytes)

    with tarfile.open(fileobj=out_buf, mode="r:*") as out_tar:
        members = out_tar.getmembers()
        names = [m.name for m in members]

        dir_to_file_members = [m for m in members if m.name == "dir_to_file"]
        assert len(dir_to_file_members) == 1, "dir_to_file must appear exactly once"
        assert dir_to_file_members[0].isreg(), "dir_to_file must be a regular file"

        file_to_dir_members = [m for m in members if m.name == "file_to_dir"]
        assert len(file_to_dir_members) == 1, "file_to_dir directory must be synthesized"
        assert file_to_dir_members[0].isdir(), "file_to_dir must be a directory"

        idx_target = names.index("forward_link_2.txt")
        idx_link = names.index("forward_link_1.txt")
        assert idx_target < idx_link, "forward_link_2.txt target must be emitted before forward_link_1.txt hardlink"


@pytest.mark.parametrize("fixture_name", sorted(_TRANSLATED_FIXTURE_SHA256))
def test_translated_fixture_bytes_remain_exact(fixture_name: str):
    translated = translate_oci_tar_to_overlay_tar((FIXTURES_DIR / fixture_name).read_bytes())

    assert hashlib.sha256(translated).hexdigest() == _TRANSLATED_FIXTURE_SHA256[fixture_name]


@pytest.mark.oci_fs
def test_layer_filesystem_semantics(tmp_path: Path):
    """Prove the selected candidate on privileged Linux without fallback."""
    candidate = SELECTED_OCI_LAYER_FILESYSTEM
    required = os.environ.get("PALIMPSEST_REQUIRE_OCI_FS") == "1"
    try:
        prerequisites = preflight_oci_filesystem_probe(candidate)
    except UnsupportedPlatformError as exc:
        reason = f"{candidate.value} OCI filesystem proof unavailable: {exc}"
        if required:
            pytest.fail(reason)
        pytest.skip(reason)

    base_tar_path = FIXTURES_DIR / "base_layer.tar"
    leaf_tar_path = FIXTURES_DIR / "leaf_layer.tar"
    receipt_path = FIXTURES_DIR / "expected_receipt.json"

    if not base_tar_path.exists() or not leaf_tar_path.exists() or not receipt_path.exists():
        pytest.fail("Fixture files in tests/fixtures/oci-root/ missing")

    base_tar = base_tar_path.read_bytes()
    leaf_tar = leaf_tar_path.read_bytes()
    receipt = json.loads(receipt_path.read_text())

    evidence = probe_oci_filesystem_candidate(candidate, base_tar, leaf_tar, receipt)
    assert evidence.candidate == candidate.value
    assert get_oci_layer_filesystem() is FilesystemCandidate.SQUASHFS
    assert evidence.fixture_digest.startswith("sha256:")
    assert len(evidence.base_image_sha256) == 64
    assert len(evidence.leaf_image_sha256) == 64
    staged_base_digest, staged_base = _pack_staged_fixture(
        tmp_path / "staged-base",
        base_tar,
        packer=Path(prerequisites.packer),
        packer_sha256=prerequisites.packer_sha256,
    )
    staged_leaf_digest, staged_leaf = _pack_staged_fixture(
        tmp_path / "staged-leaf",
        leaf_tar,
        packer=Path(prerequisites.packer),
        packer_sha256=prerequisites.packer_sha256,
    )
    assert staged_base.normalized_tar_digest == f"sha256:{_TRANSLATED_FIXTURE_SHA256['base_layer.tar']}"
    assert staged_leaf.normalized_tar_digest == f"sha256:{_TRANSLATED_FIXTURE_SHA256['leaf_layer.tar']}"
    assert staged_base_digest == evidence.base_image_sha256
    assert staged_leaf_digest == evidence.leaf_image_sha256
    evidence_dir = os.environ.get("PALIMPSEST_OCI_FS_EVIDENCE_DIR")
    if evidence_dir:
        output_dir = Path(evidence_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{candidate.value}.json").write_text(evidence.to_json(), encoding="utf-8")


@pytest.mark.oci_fs
def test_erofs_reference_retains_timestamp_failure():
    """Retain why EROFS 1.7.1 is not the selected Phase 1 backend."""
    candidate = FilesystemCandidate.EROFS
    required = os.environ.get("PALIMPSEST_REQUIRE_OCI_FS") == "1"
    try:
        prerequisites = preflight_oci_filesystem_probe(candidate)
    except UnsupportedPlatformError as exc:
        reason = f"erofs comparison unavailable: {exc}"
        if required:
            pytest.fail(reason)
        pytest.skip(reason)

    base_tar = (FIXTURES_DIR / "base_layer.tar").read_bytes()
    leaf_tar = (FIXTURES_DIR / "leaf_layer.tar").read_bytes()
    receipt = json.loads((FIXTURES_DIR / "expected_receipt.json").read_text())
    with pytest.raises(ArtifactValidationError, match="mtime mismatch"):
        probe_oci_filesystem_candidate(candidate, base_tar, leaf_tar, receipt)
    evidence_dir = os.environ.get("PALIMPSEST_OCI_FS_EVIDENCE_DIR")
    if evidence_dir:
        output_dir = Path(evidence_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        failure_evidence = {
            "architecture": prerequisites.architecture,
            "candidate": candidate.value,
            "failure_category": "timestamp-semantics",
            "fixture_digest": _probe_fixture_digest(base_tar, leaf_tar, receipt),
            "kernel_release": prerequisites.kernel_release,
            "packer_sha256": prerequisites.packer_sha256,
            "packer_version": prerequisites.packer_version,
            "schema_version": 1,
            "status": "failed",
        }
        (output_dir / "erofs.json").write_text(
            json.dumps(failure_evidence, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


def test_verify_merged_tree_pure_unit_tests(tmp_path):
    if not hasattr(os, "listxattr"):
        pytest.skip("exact merged-tree xattr verification requires os.listxattr")
    import hashlib

    def build_mock_tree(root_dir: Path) -> dict:
        root_dir.mkdir(parents=True, exist_ok=True)
        d1 = root_dir / "dir1"
        d1.mkdir()
        f1 = d1 / "file1.txt"
        f1.write_bytes(b"hello world")
        sym = root_dir / "symlink.txt"
        sym.symlink_to("dir1/file1.txt")

        h1 = root_dir / "hardlink1.txt"
        h1.write_bytes(b"hardlink data")
        h2 = root_dir / "hardlink2.txt"
        os.link(h1, h2)

        st_f1 = f1.lstat()
        st_d1 = d1.lstat()
        st_sym = sym.lstat()
        st_root = root_dir.lstat()

        return {
            ".": {
                "type": "dir",
                "mode": st_root.st_mode,
                "uid": st_root.st_uid,
                "gid": st_root.st_gid,
                "mtime_ns": st_root.st_mtime_ns,
                "xattrs_b64": {},
            },
            "dir1": {
                "type": "dir",
                "mode": st_d1.st_mode,
                "uid": st_d1.st_uid,
                "gid": st_d1.st_gid,
                "mtime_ns": st_d1.st_mtime_ns,
                "xattrs_b64": {},
            },
            "dir1/file1.txt": {
                "type": "file",
                "mode": st_f1.st_mode,
                "uid": st_f1.st_uid,
                "gid": st_f1.st_gid,
                "mtime_ns": st_f1.st_mtime_ns,
                "sha256": hashlib.sha256(b"hello world").hexdigest(),
                "xattrs_b64": {},
            },
            "symlink.txt": {
                "type": "symlink",
                "mode": st_sym.st_mode,
                "uid": st_sym.st_uid,
                "gid": st_sym.st_gid,
                "mtime_ns": st_sym.st_mtime_ns,
                "target": "dir1/file1.txt",
                "xattrs_b64": {},
            },
            "hardlink1.txt": {
                "type": "file",
                "mode": h1.lstat().st_mode,
                "uid": h1.lstat().st_uid,
                "gid": h1.lstat().st_gid,
                "mtime_ns": h1.lstat().st_mtime_ns,
                "sha256": hashlib.sha256(b"hardlink data").hexdigest(),
                "xattrs_b64": {},
            },
            "hardlink2.txt": {
                "type": "file",
                "mode": h2.lstat().st_mode,
                "uid": h2.lstat().st_uid,
                "gid": h2.lstat().st_gid,
                "mtime_ns": h2.lstat().st_mtime_ns,
                "sha256": hashlib.sha256(b"hardlink data").hexdigest(),
                "link_target": "hardlink1.txt",
                "xattrs_b64": {},
            },
        }

    # 1. Valid verification
    dir1 = tmp_path / "valid_tree"
    receipt = build_mock_tree(dir1)
    ok, msg = _verify_merged_tree(dir1, receipt)
    assert ok is True, f"Expected verification success but got error: {msg}"

    # 2. Test unexpected entries (whiteout failure simulation)
    dir2 = tmp_path / "unexpected_tree"
    receipt2 = build_mock_tree(dir2)
    unexp = dir2 / "unexpected.txt"
    unexp.write_bytes(b"unexpected")
    ok, msg = _verify_merged_tree(dir2, receipt2)
    assert ok is False
    assert "Unexpected/unhidden entries" in msg

    # 3. Test missing expected entries
    dir3 = tmp_path / "missing_tree"
    receipt3 = build_mock_tree(dir3)
    bad_receipt = dict(receipt3)
    bad_receipt["missing.txt"] = {
        "type": "file",
        "mode": 0o644,
        "uid": 0,
        "gid": 0,
        "mtime_ns": 0,
        "sha256": "0" * 64,
        "xattrs_b64": {},
    }
    ok, msg = _verify_merged_tree(dir3, bad_receipt)
    assert ok is False
    assert "Missing expected entries" in msg

    # 4. Test missing link target existence (returns False cleanly with path non-existence error)
    dir4 = tmp_path / "missing_target_tree"
    receipt4 = build_mock_tree(dir4)
    bad_target_receipt = json.loads(json.dumps(receipt4))
    bad_target_receipt["hardlink2.txt"]["link_target"] = "nonexistent_target.txt"
    ok, msg = _verify_merged_tree(dir4, bad_target_receipt)
    assert ok is False
    assert "does not exist" in msg

    # 5. Test mode mismatch
    dir5 = tmp_path / "mode_tree"
    receipt5 = build_mock_tree(dir5)
    bad_mode_receipt = json.loads(json.dumps(receipt5))
    bad_mode_receipt["dir1/file1.txt"]["mode"] = 0o777
    ok, msg = _verify_merged_tree(dir5, bad_mode_receipt)
    assert ok is False
    assert "mode mismatch" in msg

    # 6. Test hardlink inode mismatch
    dir6 = tmp_path / "hardlink_inode_tree"
    dir6.mkdir()
    sep1 = dir6 / "sep1.txt"
    sep2 = dir6 / "sep2.txt"
    sep1.write_bytes(b"same content")
    sep2.write_bytes(b"same content")
    sep_receipt = {
        ".": {
            "type": "dir",
            "mode": dir6.stat().st_mode,
            "uid": dir6.stat().st_uid,
            "gid": dir6.stat().st_gid,
            "mtime_ns": dir6.stat().st_mtime_ns,
            "xattrs_b64": {},
        },
        "sep1.txt": {
            "type": "file",
            "mode": sep1.stat().st_mode,
            "uid": sep1.stat().st_uid,
            "gid": sep1.stat().st_gid,
            "mtime_ns": sep1.stat().st_mtime_ns,
            "sha256": hashlib.sha256(b"same content").hexdigest(),
            "xattrs_b64": {},
        },
        "sep2.txt": {
            "type": "file",
            "mode": sep2.stat().st_mode,
            "uid": sep2.stat().st_uid,
            "gid": sep2.stat().st_gid,
            "mtime_ns": sep2.stat().st_mtime_ns,
            "sha256": hashlib.sha256(b"same content").hexdigest(),
            "link_target": "sep1.txt",
            "xattrs_b64": {},
        },
    }
    ok, msg = _verify_merged_tree(dir6, sep_receipt)
    assert ok is False
    assert "hardlink inode mismatch" in msg

    # 7. Test xattr verification if host supports setxattr/getxattr
    if hasattr(os, "setxattr") and hasattr(os, "getxattr"):
        dir7 = tmp_path / "xattr_tree"
        dir7.mkdir()
        xattr_file = dir7 / "xattr_file.txt"
        xattr_file.write_bytes(b"xattr test")
        try:
            os.setxattr(xattr_file, "user.testnote", b"hello_xattr")
            xattr_stat = xattr_file.stat()
            root_stat = dir7.stat()
            xattr_receipt = {
                ".": {
                    "type": "dir",
                    "mode": root_stat.st_mode,
                    "uid": root_stat.st_uid,
                    "gid": root_stat.st_gid,
                    "mtime_ns": root_stat.st_mtime_ns,
                    "xattrs_b64": {},
                },
                "xattr_file.txt": {
                    "type": "file",
                    "mode": xattr_stat.st_mode,
                    "uid": xattr_stat.st_uid,
                    "gid": xattr_stat.st_gid,
                    "mtime_ns": xattr_stat.st_mtime_ns,
                    "sha256": hashlib.sha256(b"xattr test").hexdigest(),
                    "xattrs_b64": {"user.testnote": "aGVsbG9feGF0dHI="},
                },
            }
            ok, msg = _verify_merged_tree(dir7, xattr_receipt)
            assert ok is True, f"Expected xattr verification to succeed, got: {msg}"

            bad_xattr_receipt = {
                ".": xattr_receipt["."],
                "xattr_file.txt": {
                    "type": "file",
                    "mode": xattr_stat.st_mode,
                    "uid": xattr_stat.st_uid,
                    "gid": xattr_stat.st_gid,
                    "mtime_ns": xattr_stat.st_mtime_ns,
                    "sha256": hashlib.sha256(b"xattr test").hexdigest(),
                    "xattrs_b64": {"user.testnote": "d3JvbmdfdmFsdWU="},
                },
            }
            ok, msg = _verify_merged_tree(dir7, bad_xattr_receipt)
            assert ok is False
            assert "xattr user.testnote mismatch" in msg
        except OSError:
            pass  # Filesystem may not support user xattrs in some test environments


def test_build_layer_filesystem_cleanup_on_failure(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    valid_tar = buf.getvalue()
    private_parent = tmp_path / "generated-parent"
    private_parent.mkdir(mode=0o700)

    with patch("sys.platform", "linux"):
        with patch("shutil.which", return_value="/usr/bin/mksquashfs"):
            mock_proc = MagicMock(returncode=1, stderr=b"mksquashfs error")
            with (
                patch("subprocess.run", return_value=mock_proc),
                patch("tempfile.mkdtemp", return_value=str(private_parent)),
            ):
                from palimpsest_local.errors import ArtifactValidationError

                with pytest.raises(ArtifactValidationError, match="mksquashfs failed"):
                    build_layer_filesystem(valid_tar, target_fs="squashfs")
    assert not private_parent.exists()


def test_build_layer_filesystem_atomically_publishes_from_private_staging(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    output = tmp_path / "layer.squashfs"

    def fake_pack(command, **_kwargs):
        Path(command[2]).write_bytes(b"hsqs\0")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch("sys.platform", "linux"),
        patch("shutil.which", return_value="/usr/bin/mksquashfs"),
        patch("palimpsest_local.oci_convert._run_checked", side_effect=fake_pack),
    ):
        result = build_layer_filesystem(buf.getvalue(), target_fs="squashfs", out_path=output)

    assert result == output
    assert output.read_bytes() == b"hsqs\0"
    assert list(tmp_path.glob(".layer.squashfs.*")) == []


def test_build_layer_filesystem_refuses_output_symlink_inserted_during_pack(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    output = tmp_path / "layer.squashfs"
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")

    def fake_pack(command, **_kwargs):
        Path(command[2]).write_bytes(b"hsqs\0")
        output.symlink_to(victim)
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch("sys.platform", "linux"),
        patch("shutil.which", return_value="/usr/bin/mksquashfs"),
        patch("palimpsest_local.oci_convert._run_checked", side_effect=fake_pack),
        pytest.raises(ArtifactValidationError, match="appeared before atomic publish"),
    ):
        build_layer_filesystem(buf.getvalue(), target_fs="squashfs", out_path=output)

    assert output.is_symlink()
    assert victim.read_bytes() == b"untouched"
    assert list(tmp_path.glob(".layer.squashfs.*")) == []
