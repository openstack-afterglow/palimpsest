from __future__ import annotations

import errno
import os
import shutil
import sys

import pytest

from palimpsest_local.oci_commit import CommitError
from palimpsest_local.vz_commit import CLONE_NAME, clonefile, private_upper_clone

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
