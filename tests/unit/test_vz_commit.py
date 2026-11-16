from __future__ import annotations

import errno
import hashlib
import io
import itertools
import os
import shutil
import sys
import tarfile
from contextlib import contextmanager

import pytest

from palimpsest_local.oci_capture import CaptureError, convert_upper_to_layer
from palimpsest_local.oci_commit import CommitError
from palimpsest_local.vz_commit import CLONE_NAME, _scan_parent, clonefile, parent_layer_paths, private_upper_clone

SIZE = 64 * 1024


def make_source(tmp_path, size=SIZE):
    source = tmp_path / "vol" / "datadisk"
    source.parent.mkdir()
    source.write_bytes(os.urandom(size))
    os.chmod(source, 0o600)
    return source


def copy_clone(source, destination):
    shutil.copyfile(source, destination)


def refusing_clone(source, destination):
    raise OSError(errno.ENOTSUP, "Operation not supported")


def test_clone_is_private_cleaned_up_and_leaves_the_source_untouched(tmp_path):
    source = make_source(tmp_path)
    data = source.read_bytes()
    before = os.lstat(source)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    seen = []
    with private_upper_clone(source, scratch, expected_size=SIZE, clone=copy_clone) as clone:
        seen.append(clone)
        assert clone.name == CLONE_NAME and clone.read_bytes() == data
        assert clone.parent.parent == scratch and oct(clone.parent.stat().st_mode & 0o777) == "0o700"
    assert not seen[0].exists() and not seen[0].parent.exists()
    after = os.lstat(source)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert source.read_bytes() == data and list(scratch.iterdir()) == []


def test_failure_inside_the_block_removes_only_the_clone_never_the_source_or_siblings(tmp_path):
    source = make_source(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    neighbour = scratch / "other-case"
    neighbour.mkdir()
    (neighbour / "keep").write_text("x")
    with pytest.raises(RuntimeError), private_upper_clone(source, scratch, expected_size=SIZE, clone=copy_clone):
        raise RuntimeError("capture failed")
    assert source.exists() and (neighbour / "keep").read_text() == "x"
    assert [item.name for item in scratch.iterdir()] == ["other-case"]


def test_a_filesystem_that_cannot_clone_is_refused_without_any_byte_copy(tmp_path):
    source = make_source(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with (
        pytest.raises(CommitError) as raised,
        private_upper_clone(source, scratch, expected_size=SIZE, clone=refusing_clone),
    ):
        pytest.fail("the body must not run")
    assert raised.value.code == "clone-unsupported"
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("size", [SIZE - 1, SIZE + 1])
def test_unexpected_size_is_refused_before_any_clone(tmp_path, size):
    source = make_source(tmp_path, size)
    called = []
    with (
        pytest.raises(CommitError) as raised,
        private_upper_clone(source, tmp_path, expected_size=SIZE, clone=lambda a, b: called.append(1)),
    ):
        pytest.fail("the body must not run")
    assert raised.value.code == "source-identity" and called == []


def test_symlink_or_directory_source_is_refused(tmp_path):
    real = make_source(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    for bad in (link, tmp_path):
        with (
            pytest.raises(CommitError) as raised,
            private_upper_clone(bad, tmp_path, expected_size=SIZE, clone=copy_clone),
        ):
            pytest.fail("the body must not run")
        assert raised.value.code == "source-identity"


def test_a_source_that_changes_during_capture_fails_the_capture_and_still_cleans_up(tmp_path):
    source = make_source(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with (
        pytest.raises(CommitError) as raised,
        private_upper_clone(source, scratch, expected_size=SIZE, clone=copy_clone),
    ):
        with source.open("r+b") as handle:
            handle.write(b"x")
    assert raised.value.code == "source-changed"
    assert list(scratch.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises APFS clonefile")
def test_real_clonefile_shares_content_and_does_not_alter_the_source(tmp_path):
    source = make_source(tmp_path)
    before = os.lstat(source)
    data = source.read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with private_upper_clone(source, scratch, expected_size=SIZE) as clone:
        assert clone.read_bytes() == data
        with clone.open("r+b") as handle:  # writing the clone must never reach the source
            handle.write(b"\x00" * 16)
        assert source.read_bytes() == data
    after = os.lstat(source)
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def test_clonefile_refuses_an_existing_destination(tmp_path):
    source = make_source(tmp_path)
    destination = tmp_path / "taken"
    destination.write_bytes(b"x")
    with pytest.raises(OSError):
        clonefile(source, destination)
    assert destination.read_bytes() == b"x"


def _parent(tmp_path, *layers):
    """A parent image whose layer tars list their members in exactly the given order."""
    manifest, blobs = {"layers": []}, {}
    for index, members in enumerate(layers):
        path = tmp_path / f"layer{index}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in members:
                info = tarfile.TarInfo(name)
                if data is None:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["layers"].append({"digest": digest})
        blobs[digest] = path
    return manifest, blobs


LOWER = [("etc", None), ("etc/ssh", None), ("etc/ssh/old_host_key", b"old"), ("srv", None), ("srv/lower", b"l")]
# One changeset: /etc/ssh is replaced wholesale (opaque) and gains a new key; /srv is deleted and
# recreated with different content. OCI markers act on lower layers only, whatever their position.
REPLACING = [
    ("etc/ssh", None),
    ("etc/ssh/ssh_host_ed25519_key", b"PRIVATE"),
    ("etc/ssh/.wh..wh..opq", b""),
    ("srv", None),
    ("srv/new", b"n"),
    (".wh.srv", b""),
]
REPLACED_VIEW = {"etc", "etc/ssh", "etc/ssh/ssh_host_ed25519_key", "srv", "srv/new"}


def test_markers_listed_after_same_layer_entries_hide_only_the_lower_layers(tmp_path):
    manifest, blobs = _parent(tmp_path, LOWER, REPLACING)
    assert parent_layer_paths(manifest, blobs) == REPLACED_VIEW


def test_the_visible_parent_view_does_not_depend_on_member_order_inside_a_layer(tmp_path):
    views = set()
    for index, order in enumerate(itertools.permutations(REPLACING)):
        if index % 37:  # 720 orders; a spread of 20 keeps the test fast
            continue
        scratch = tmp_path / str(index)
        scratch.mkdir()
        manifest, blobs = _parent(scratch, LOWER, list(order))
        views.add(frozenset(parent_layer_paths(manifest, blobs)))
    assert views == {frozenset(REPLACED_VIEW)}


def test_a_root_opaque_marker_keeps_the_entries_of_its_own_layer(tmp_path):
    manifest, blobs = _parent(tmp_path, LOWER, [(".plmignore", b"cache/\n"), ("app", b"a"), (".wh..wh..opq", b"")])
    visible, ignore = _scan_parent(manifest, blobs)
    assert visible == {".plmignore", "app"}
    assert ignore == b"cache/\n"


def test_a_later_layer_still_deletes_what_an_earlier_layer_added(tmp_path):
    manifest, blobs = _parent(tmp_path, LOWER, REPLACING, [("etc/ssh/.wh.ssh_host_ed25519_key", b""), (".wh.srv", b"")])
    assert parent_layer_paths(manifest, blobs) == {"etc", "etc/ssh"}


DEEP = [*LOWER, ("etc/ssh/keys", None), ("etc/ssh/keys/deep_key", b"k"), ("srv/a", None), ("srv/a/b", b"b")]


def test_markers_hide_lower_entries_at_any_depth_below_them(tmp_path):
    manifest, blobs = _parent(tmp_path, DEEP, [("etc/ssh", None), ("etc/ssh/.wh..wh..opq", b""), (".wh.srv", b"")])
    assert parent_layer_paths(manifest, blobs) == {"etc", "etc/ssh"}


@pytest.mark.parametrize("replacing", [[("etc/ssh", b"now a file")], [("srv/a", b"file"), ("etc/ssh", b"file")]])
def test_a_file_that_replaces_a_lower_directory_removes_the_directory_entries(tmp_path, replacing):
    manifest, blobs = _parent(tmp_path, DEEP, replacing)
    replaced = {name for name, _ in replacing}
    visible = parent_layer_paths(manifest, blobs)
    assert replaced <= visible
    assert not [path for path in visible if any(path.startswith(name + "/") for name in replaced)]
    assert {"etc", "srv", "srv/lower"} <= visible


def test_a_directory_listed_without_its_own_entry_is_still_replaced_by_a_file(tmp_path):
    manifest, blobs = _parent(tmp_path, [("opt/app/bin", b"b")], [("opt/app", b"file")])
    assert parent_layer_paths(manifest, blobs) == {"opt/app"}


def test_within_one_layer_a_later_file_replaces_entries_listed_below_its_path_earlier(tmp_path):
    manifest, blobs = _parent(tmp_path, [("opt/a/b", b"b"), ("opt/a", b"file"), ("opt/c/d", b"d")])
    assert parent_layer_paths(manifest, blobs) == {"opt/a", "opt/c/d"}


def test_a_plmignore_listed_before_its_own_whiteout_in_one_layer_is_the_visible_one(tmp_path):
    lower = [(".plmignore", b"old/\n")]
    manifest, blobs = _parent(tmp_path, lower, [(".plmignore", b"new/\n"), (".wh..plmignore", b"")])
    assert _scan_parent(manifest, blobs) == ({".plmignore"}, b"new/\n")
    manifest, blobs = _parent(tmp_path, lower, [(".wh..plmignore", b"")])
    assert _scan_parent(manifest, blobs) == (set(), None)


@contextmanager
def _no_payload(payload):
    yield io.BytesIO(b"")


def test_commit_hides_a_parent_identity_file_whose_layer_lists_it_before_the_marker(tmp_path):
    # The consequence the order defect had: the parent's host key was not seen, so the committed
    # layer carried no whiteout for it and the key stayed visible in the new image.
    manifest, blobs = _parent(tmp_path, LOWER, REPLACING)
    receipt = convert_upper_to_layer([], io.BytesIO(), _no_payload, parent_paths=parent_layer_paths(manifest, blobs))
    assert "etc/ssh/ssh_host_ed25519_key" in receipt.hidden


def test_commit_still_refuses_unclassified_parent_authorized_keys_listed_before_a_marker(tmp_path):
    layer = [
        ("root", None),
        ("root/.ssh", None),
        ("root/.ssh/authorized_keys", b"ssh-ed25519 AAAA"),
        ("root/.wh..wh..opq", b""),
    ]
    manifest, blobs = _parent(tmp_path, LOWER, layer)
    with pytest.raises(CaptureError) as refused:
        convert_upper_to_layer([], io.BytesIO(), _no_payload, parent_paths=parent_layer_paths(manifest, blobs))
    assert refused.value.code == "capture-provenance"
