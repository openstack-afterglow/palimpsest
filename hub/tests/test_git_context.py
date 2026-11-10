from __future__ import annotations

import io
import os
import tarfile

import pytest

from palimpsest_hub.services.git_context import ContextError, parse_ignore, write_context_tar


def build(root, ignore=()):
    buf = io.BytesIO()
    digest, count = write_context_tar(root, buf, ignore_lines=ignore)
    return buf.getvalue(), digest, count


def names(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return [m.name for m in tar.getmembers()]


def make_tree(root):
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "tmp").mkdir()
    (root / "tmp" / "scratch.txt").write_text("drop me\n")
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(root / "run.sh", 0o700)


def test_the_same_tree_gives_the_same_bytes_and_digest_after_timestamps_and_modes_change(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    make_tree(first)
    make_tree(second)
    os.utime(second / "src" / "app.py", (1_700_000_000, 1_700_000_000))
    os.chmod(second / "Dockerfile", 0o600)
    a, digest_a, count_a = build(first)
    b, digest_b, count_b = build(second)
    assert a == b
    assert digest_a == digest_b
    assert count_a == count_b


def test_content_changes_change_the_digest(tmp_path):
    make_tree(tmp_path)
    _, before, _ = build(tmp_path)
    (tmp_path / "src" / "app.py").write_text("print('bye')\n")
    _, after, _ = build(tmp_path)
    assert before != after


def test_ignored_paths_are_absent_and_kept_paths_are_present_in_the_same_build(tmp_path):
    make_tree(tmp_path)
    data, _, _ = build(tmp_path, ignore=["tmp/", "# comment", ""])
    got = names(data)
    assert "tmp" not in got and "tmp/scratch.txt" not in got
    assert "src/app.py" in got and "Dockerfile" in got


def test_glob_ignore_matches_files_at_any_depth_it_is_written_for(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "src" / "debug.log").write_text("x")
    data, _, _ = build(tmp_path, ignore=["*.log"])
    got = names(data)
    assert "src/debug.log" not in got
    assert "src/app.py" in got


def test_entries_have_zeroed_ownership_and_normalised_modes(tmp_path):
    make_tree(tmp_path)
    data, _, _ = build(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = {m.name: m for m in tar.getmembers()}
    for member in members.values():
        assert (member.uid, member.gid, member.uname, member.gname, member.mtime) == (0, 0, "", "", 0)
    assert members["Dockerfile"].mode == 0o644
    assert members["run.sh"].mode == 0o755
    assert members["src"].isdir()


def test_a_symlink_inside_the_context_is_stored_as_a_link_and_not_followed(tmp_path):
    make_tree(tmp_path)
    os.symlink("src/app.py", tmp_path / "link.py")
    data, _, _ = build(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.getmember("link.py")
    assert member.issym()
    assert member.linkname == "src/app.py"


@pytest.mark.parametrize("target", ["../outside.txt", "src/../../outside.txt", "/etc/passwd"])
def test_a_symlink_leaving_the_context_is_refused(tmp_path, target):
    make_tree(tmp_path)
    os.symlink(target, tmp_path / "escape")
    with pytest.raises(ContextError):
        build(tmp_path)


def test_a_fifo_is_refused(tmp_path):
    make_tree(tmp_path)
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ContextError, match="special files"):
        build(tmp_path)


def test_negated_ignore_rules_are_refused_not_guessed():
    with pytest.raises(ContextError, match="negated"):
        parse_ignore(["*.log", "!keep.log"])


@pytest.mark.parametrize("rule", ["**/.env", "src/**/*.pem", "**"])
def test_double_star_rules_are_refused_because_they_could_miss_root_secrets(rule):
    with pytest.raises(ContextError, match="'\*\*'"):
        parse_ignore([rule])


@pytest.mark.parametrize("rule", ["../secret", "a/../../b"])
def test_ignore_rules_cannot_climb_out_of_the_context(rule):
    with pytest.raises(ContextError):
        parse_ignore([rule])


def test_a_root_level_secret_is_not_stored_when_the_rule_is_refused(tmp_path):
    make_tree(tmp_path)
    (tmp_path / ".env").write_text("SECRET=1\n")
    with pytest.raises(ContextError):
        build(tmp_path, ignore=["**/.env"])


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(ContextError):
        build(f)
