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


def test_ignore_rules_from_the_repository_have_a_count_and_a_length_bound():
    # The rules are tried against every entry in a Hub API worker thread; a repository with
    # 40,000 rules kept that thread busy for minutes on a 2,000-file context.
    assert len(parse_ignore([f"r{i}" for i in range(1000)])) == 1000
    with pytest.raises(ContextError, match="too many ignore rules"):
        parse_ignore([f"r{i}" for i in range(1001)])
    assert parse_ignore(["a" * 4096]) == ("a" * 4096,)
    with pytest.raises(ContextError, match="longer than a path"):
        parse_ignore(["a" * 4097])


def test_a_rule_drops_what_it_matches_whole_and_everything_below_an_ignored_directory(tmp_path):
    # Each rule is tried against an entry's own path only; the parents were tried when they
    # were visited, and an ignored directory is never entered.
    make_tree(tmp_path)
    (tmp_path / "src" / "deep" / "er").mkdir(parents=True)
    (tmp_path / "src" / "deep" / "er" / "keep.py").write_text("k")
    (tmp_path / "src" / "deep" / "er" / "x.log").write_text("x")
    (tmp_path / "srcx").mkdir()
    (tmp_path / "srcx" / "other.py").write_text("o")
    assert "src/deep/er/keep.py" in names(build(tmp_path)[0])
    got = names(build(tmp_path, ignore=["src/deep"])[0])
    assert not any(name.startswith("src/deep") for name in got) and "src/app.py" in got
    got = names(build(tmp_path, ignore=["s*g"])[0])  # ``*`` crosses "/": src/deep/er/x.log
    assert "src/deep/er/x.log" not in got and "src/deep/er/keep.py" in got
    got = names(build(tmp_path, ignore=["src"])[0])
    assert not any(name == "src" or name.startswith("src/") for name in got) and "srcx/other.py" in got


def test_a_rule_is_anchored_at_the_context_root(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "app.py").write_text("root\n")
    got = names(build(tmp_path, ignore=["app.py"])[0])
    assert "app.py" not in got and "src/app.py" in got


def test_ignore_matching_has_a_work_budget_that_does_not_depend_on_the_host(tmp_path, monkeypatch):
    from palimpsest_hub.services import git_context

    # Trying a rule costs a fixed call plus about rule length times path length. One request with
    # 64 KiB of rules and long paths took 87 seconds for 2,000 files before this bound, and one
    # with a thousand one-character rules and a million short names 49 seconds.
    make_tree(tmp_path)
    rules = ["*" + "a" * 60 + "b"] * 10
    assert "src/app.py" in names(build(tmp_path, ignore=rules)[0])
    entries = names(build(tmp_path)[0])
    spent = sum(len(name) * sum(len(rule) for rule in rules) + 64 * len(rules) for name in entries)
    monkeypatch.setattr(git_context, "MAX_IGNORE_WORK", spent)
    assert "src/app.py" in names(build(tmp_path, ignore=rules)[0])
    monkeypatch.setattr(git_context, "MAX_IGNORE_WORK", spent - 1)
    with pytest.raises(ContextError, match="too much work"):
        build(tmp_path, ignore=rules)
    assert "src/app.py" in names(build(tmp_path)[0]), "a context without rules costs no matching work"


def test_entries_that_a_rule_drops_still_count_as_matching_work(tmp_path, monkeypatch):
    from palimpsest_hub.services import git_context

    # Dropped entries never reach the member limit, so the work budget is their only bound.
    for index in range(50):
        (tmp_path / f"n{index:02d}").write_text("x")
    assert names(build(tmp_path, ignore=["n*"])[0]) == []
    monkeypatch.setattr(git_context, "MAX_IGNORE_WORK", 50 * (3 * 2 + 64) - 1)
    with pytest.raises(ContextError, match="too much work"):
        build(tmp_path, ignore=["n*"])
    monkeypatch.setattr(git_context, "MAX_IGNORE_WORK", 50 * (3 * 2 + 64))
    assert names(build(tmp_path, ignore=["n*"])[0]) == []


def test_a_bracket_rule_beyond_latin_1_is_refused_before_it_is_compiled():
    # Compiling such classes walks every code point of their ranges: 17 seconds for 64 KiB.
    for rule in ("[\x01-\uffff]*.bin", "데이터/[ab].txt", "[a\u0100]"):
        with pytest.raises(ContextError, match="Latin-1"):
            parse_ignore([rule])
    accepted = ["[a-zé]*.txt", "[\x01-\xff]", "데이터/*.txt", "데이터[임시", "임시[]"]
    assert parse_ignore(accepted) == tuple(accepted), "Latin-1 classes, and a '[' that opens no class"


def test_packing_stops_at_the_first_entry_beyond_the_callers_limit(tmp_path):
    from palimpsest_hub.services.git_context import ContextTooLarge

    make_tree(tmp_path)
    total = len(names(build(tmp_path)[0]))
    buf = io.BytesIO()
    assert write_context_tar(tmp_path, buf, max_entries=total)[1] == total
    partial = io.BytesIO()
    with pytest.raises(ContextTooLarge):
        write_context_tar(tmp_path, partial, max_entries=total - 1)
    with tarfile.open(fileobj=io.BytesIO(partial.getvalue() + bytes(1024)), mode="r|") as tar:
        assert len(tar.getmembers()) <= total - 1, "nothing beyond the limit was written"


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


def test_a_backslash_in_a_name_is_refused_because_the_worker_cannot_extract_it(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "a\\b").write_text("x")
    with pytest.raises(ContextError, match="cannot be extracted"):
        build(tmp_path)


def test_the_tar_round_trips_through_the_worker_extractor_with_the_same_tree(tmp_path):
    from palimpsest_local.hub_image_builder import extract_context

    src = tmp_path / "src"
    src.mkdir()
    make_tree(src)
    (src / "run.sh").chmod(0o755)
    os.symlink("src/app.py", src / "link.py")
    archive = tmp_path / "context.tar"
    with archive.open("wb") as handle:
        write_context_tar(src, handle)
    out = tmp_path / "out"
    extract_context(archive, out, maximum_bytes=10**6, maximum_members=100)

    def tree(root):
        found = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                found[rel] = ("dir", None)
            else:
                found[rel] = ("file", path.read_text(), bool(path.stat().st_mode & 0o100))
        return found

    assert tree(out) == tree(src)
