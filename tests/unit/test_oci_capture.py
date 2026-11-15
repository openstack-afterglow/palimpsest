from __future__ import annotations

import hashlib
import io
import tarfile
from contextlib import contextmanager

import pytest

from palimpsest_local.oci_capture import (
    CaptureError,
    IgnoreMatcher,
    UpperEntry,
    UpperKind,
    convert_upper_to_layer,
    normalize_capture_path,
)


@contextmanager
def _open(payload):
    yield io.BytesIO(payload)


def _file(path: str, data: bytes = b"x", **kwargs) -> UpperEntry:
    return UpperEntry(path, UpperKind.FILE, size=len(data), payload=data, **kwargs)


def _layer(entries, **kwargs):
    sink = io.BytesIO()
    receipt = convert_upper_to_layer(entries, sink, _open, **kwargs)
    members = {}
    with tarfile.open(fileobj=io.BytesIO(sink.getvalue())) as archive:
        for member in archive:
            members[member.name] = (member, archive.extractfile(member).read() if member.isreg() else None)
    return receipt, sink.getvalue(), members


def test_overlay_semantics_become_oci_markers_and_placeholders_are_dropped():
    entries = [
        _file("opt/app/main.py", b"print(1)", mode=0o755, uid=1000, gid=1000),
        UpperEntry("usr/share/doc/old", UpperKind.WHITEOUT),
        UpperEntry("var/cache/app", UpperKind.DIRECTORY, mode=0o755, xattrs=(("trusted.overlay.opaque", b"y"),)),
        _file("etc/machine-id", b""),
        _file(".palimpsest/state/x", b"secret"),
    ]
    receipt, raw, members = _layer(entries, generated_placeholders=["etc/machine-id"])
    assert set(members) == {
        "opt/app/main.py",
        "usr/share/doc/.wh.old",
        "var/cache/app",
        "var/cache/app/.wh..wh..opq",
    }
    assert members["opt/app/main.py"][1] == b"print(1)"
    assert members["opt/app/main.py"][0].uid == 1000
    assert set(receipt.excluded) == {"etc/machine-id", ".palimpsest/state/x"}
    assert receipt.diff_id == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_user_content_at_platform_bound_identity_path_is_kept_unless_recorded_as_placeholder():
    _, _, members = _layer([_file("etc/hostname", b"mybox\n")])
    assert members["etc/hostname"][1] == b"mybox\n"


def test_reinclusion_cannot_defeat_mandatory_exclusion_and_parent_identity_is_hidden():
    entries = [
        _file("etc/ssh/ssh_host_ed25519_key", b"PRIVATE"),
        _file("etc/ssh/sshd_config", b"Port 22"),
        _file("var/lib/cloud/instance/x", b"state"),
    ]
    receipt, _, members = _layer(
        entries,
        plmignore="!etc/ssh/ssh_host_ed25519_key\n!var/lib/cloud\n",
        parent_paths=[
            "etc/ssh/ssh_host_ed25519_key",
            "etc/ssh/ssh_host_rsa_key.pub",
            "var/lib/cloud",
            "var/lib/cloud/instance",
            "etc/passwd",
        ],
    )
    assert "etc/ssh/ssh_host_ed25519_key" not in members
    assert "var/lib/cloud/instance/x" not in members
    assert members["etc/ssh/sshd_config"][1] == b"Port 22"
    assert set(receipt.hidden) == {"etc/ssh/ssh_host_ed25519_key", "etc/ssh/ssh_host_rsa_key.pub", "var/lib/cloud"}
    assert {"etc/ssh/.wh.ssh_host_ed25519_key", "etc/ssh/.wh.ssh_host_rsa_key.pub", "var/lib/.wh.cloud"} <= set(members)
    assert "etc/.wh.passwd" not in members


def test_plmignore_excludes_only_new_upper_files_and_never_whiteouts_parent_files():
    entries = [_file("srv/data/big.bin", b"1"), _file("srv/data/keep.txt", b"2"), _file("home/u/note", b"3")]
    receipt, _, members = _layer(
        entries,
        plmignore="# comment\nsrv/data/*\n!srv/data/keep.txt\n",
        parent_paths=["srv/data/old.bin"],
    )
    assert set(members) == {"srv/data/keep.txt", "home/u/note"}
    assert receipt.ignored == ("srv/data/big.bin",)
    assert not receipt.hidden
    assert not any(".wh." in name for name in members)


def test_directory_rule_covers_descendants_and_reinclusion_wins_when_later():
    matcher = IgnoreMatcher("build\n**/*.log\n!build/keep/**\n")
    assert matcher.ignored("build/out/a.o")
    assert not matcher.ignored("build/keep/a.o")
    assert matcher.ignored("var/app/x.log")
    assert not matcher.ignored("var/app/x.txt")


_MANY_WILDCARD_CASES = [
    ("**/build/**/*.o", "src/build/x/y/a.o", True),
    ("**/build/**/*.o", "src/build/a.o", True),
    ("**/build/**/*.o", "build/a.o", True),
    ("**/build/**/*.o", "build/a.o/below/it", True),  # a covered directory covers what is below it
    ("**/build/**/*.o", "src/build/a.c", False),
    ("**/build/**/*.o", "src/builder/a.o", False),
    ("**/build/**/*.o", "src/build/a.ox/y", False),  # a prefix must end at a component boundary
    ("a*b*c*d", "axbxcxd", True),
    ("a*b*c*d", "axbxcxd/e", True),
    ("a*b*c*d", "axb/cxd", False),
    ("a*b*c*d", "abcd\n/z", True),  # the newline is read by ``*`` and still ends a matched component
    ("a*b*c*d", "abcd\nq/z", False),
    ("**x**y**z", "a/x/b/y/c/z", True),
    ("**x**y**z", "a/z/b/y/c/x", False),
    ("*/?[ab]/**/*.[!c]", "d/qa/deep/er/f.h", True),
    ("*/?[ab]/**/*.[!c]", "d/qa/f.c", False),
    ("**/a/**/b/*", "a/b/c\n", True),  # one trailing newline is ignored at the end of the pattern
    ("**/a/**/b/*", "a/b/c\n/d", True),  # also when that component is a covered directory
    ("**/a/**/b/**/c", "a/b/c\n", True),
    ("**/a/**/b/**/c", "a/b/c\nx", False),  # only a newline that ends the component is ignored
    ("**/a/**/b/*", "a/x\ny/b/c", False),  # ``**`` does not cross a newline
    ("a?b*c*", "axbc", True),  # ``?`` and a class break a literal run: "ab" is not required text
    ("a[x]b*c*", "axbc", True),
    ("a?b*c*", "ab", False),
]


@pytest.mark.parametrize("pattern,path,expected", _MANY_WILDCARD_CASES)
def test_rules_with_many_wildcards_cover_the_same_paths_on_both_matching_routes(pattern, path, expected):
    from palimpsest_local import oci_capture

    tokens = oci_capture._glob_tokens(pattern)
    assert sum(kind in oci_capture._UNBOUNDED for kind, _ in tokens) > oci_capture._MAX_REGEX_WILDCARDS
    assert bool(oci_capture._covering_regex(tokens).match(path)) is expected
    assert oci_capture._LinearGlob(tokens, [1000]).covers(path) is expected
    assert oci_capture._LinearGlob(tokens, [0]).covers(path) is expected, "without any remembered transition"
    assert IgnoreMatcher(pattern + "\n").ignored(path) is expected


@pytest.mark.parametrize(
    "rule",
    [
        "**/**/b",
        "**a**b",
        "/".join(["**", "a"] * 12) + "/b",
        "a*a*a*a*a*b",
        "**a**a**a**b",
        "****" + "[a/]" * 1023,
        "**" + "[a/]" * 1023 + "b",
        "**a" * 1365,
    ],
    ids=[
        "two-globdirs",
        "two-globstars",
        "twelve-globdirs",
        "five-stars",
        "four-globstars",
        "two-wildcards-long-class-run",
        "one-wildcard-long-class-run",
        "longest-rule-of-wildcards",
    ],
)
def test_pathological_rules_are_evaluated_in_bounded_time_on_maximum_length_paths(rule):
    import time

    # Measured before the bounds, per path: about ten seconds for the first rule, more than
    # fifteen for the third, and fifteen for a two-wildcard rule followed by a long class run.
    deep = "/".join(["a"] * 2047) + "/a"  # 4095 bytes, 2048 components
    wide = "/".join(["a" * 255] * 15)  # maximum-length components
    matcher = IgnoreMatcher(rule + "\n")
    started = time.monotonic()
    for path in (deep, wide, deep + "x", wide + "/b"):
        matcher.ignored(path)
    assert time.monotonic() - started < 5.0


def test_ignore_rules_have_count_length_and_time_bounds_and_typed_refusals():
    with pytest.raises(CaptureError) as error:
        IgnoreMatcher("x\n" * 10_001)
    assert error.value.code == "plmignore" and "too many rules" in str(error.value)
    assert IgnoreMatcher("x\n" * 10_000).ignored("x")

    with pytest.raises(CaptureError) as error:
        IgnoreMatcher("a" * 4097 + "\n")
    assert error.value.code == "plmignore" and "longer than a path" in str(error.value)
    assert IgnoreMatcher("a" * 4096 + "\n").ignored("a" * 4096)

    spent = IgnoreMatcher("*.log\n", budget_seconds=-1.0)
    with pytest.raises(CaptureError) as error:
        spent.ignored("a.log")
    assert error.value.code == "plmignore" and "too long" in str(error.value)

    # The rule length limit counts bytes: 1366 three-byte characters are 4098 bytes.
    with pytest.raises(CaptureError) as error:
        IgnoreMatcher("\u4e00" * 1366 + "\n")
    assert "longer than a path" in str(error.value)
    assert IgnoreMatcher("\u4e00" * 1365 + "\n").ignored("\u4e00" * 1365)

    with pytest.raises(CaptureError) as error:
        IgnoreMatcher("[z-a]\n")
    assert error.value.code == "plmignore" and "invalid character class" in str(error.value)


@pytest.mark.parametrize(
    "rule",
    ["[!]", "[^]", "[!][ab]", "*[!][ab]", "a*b*[!][ab]", "[!][](a|a)*c]", "**/[!][](a|a)*c]", "[!][]|b]", "x[^]y]"],
)
def test_a_bracket_that_would_stay_open_as_regex_text_is_refused_for_every_rule(rule):
    # ``[!]`` becomes the regex text ``[^]``, a class that runs on into what follows. With a later
    # ``[]...]`` a rule carried alternation and nested repeats to the regex engine: exponential
    # time on a 26-character name, and matches that depended on how the regex was wrapped.
    with pytest.raises(CaptureError) as error:
        IgnoreMatcher(rule + "\n")
    assert error.value.code == "plmignore" and "ambiguous character class" in str(error.value)


@pytest.mark.parametrize("rule", ["[+--]", "[a--b]", "*z*[+--]", "x[!a--z]"])
def test_a_class_that_spells_a_set_difference_is_refused(rule):
    # ``--`` in a class is reserved by the regex engine (it warns today). It cannot be escaped
    # without changing ranges, so the rule is refused rather than left to change meaning.
    with pytest.raises(CaptureError) as error:
        IgnoreMatcher(rule + "\n")
    assert error.value.code == "plmignore" and "ambiguous character class" in str(error.value)
    assert IgnoreMatcher("[a-c-]\n").ignored("-") and IgnoreMatcher("[-a]\n").ignored("-")


def test_brackets_that_hold_a_closing_bracket_first_are_plain_classes():
    # ``[]a]`` is the class of "]" and "a"; its text never leaves the class, whatever it spells.
    assert IgnoreMatcher("[]a]\n").ignored("]") and IgnoreMatcher("[]a]\n").ignored("a")
    assert not IgnoreMatcher("[]a]\n").ignored("b")
    spelled = IgnoreMatcher("[](a|a)*c]\n")
    assert all(spelled.ignored(char) for char in "](a|)*c") and not spelled.ignored("aac") and not spelled.ignored("x")


@pytest.mark.parametrize("route", ["", "*z*"], ids=["regex-route", "simulation-route"])
def test_a_class_body_holds_only_literal_characters_and_ranges(route, recwarn):
    import warnings

    warnings.simplefilter("error")  # nested-set and set-operator FutureWarnings would fail here

    def ignored(rule, text):
        return IgnoreMatcher(route + rule + "\n").ignored(("zz" if route else "") + text)

    # A backslash in a class is a backslash, never a regex escape such as ``\d``.
    assert ignored("[\\d]", "\\") and ignored("[\\d]", "d") and not ignored("[\\d]", "5")
    # "[", "&", "|" and "~" mean themselves, also doubled.
    assert all(ignored("x[a&&b]", "x" + char) for char in "a&b") and not ignored("x[a&&b]", "xc")
    assert all(ignored("[a||b~~c]", char) for char in "a|b~c") and not ignored("[a||b~~c]", "d")
    assert ignored("[[:alpha:]", ":") and ignored("[[:alpha:]", "[") and not ignored("[[:alpha:]", "z")
    assert ignored("[a-c]", "b") and not ignored("[a-c]", "d")


def test_remembered_transitions_do_not_confuse_characters_beyond_the_basic_plane(monkeypatch):
    import random

    from palimpsest_local import oci_capture

    # U+10061 and "a" differ by exactly 0x10000; a transition key that folded them would let one
    # state's step answer for another's.
    rule = "**a**\U00010061**z\n"
    remembering = IgnoreMatcher(rule)
    monkeypatch.setattr(oci_capture, "_MAX_CACHED_STATE", 0)
    plain = IgnoreMatcher(rule)
    rng = random.Random(20261115)
    for _ in range(3000):
        path = "".join(rng.choice(["a", "\U00010061", "z", "b", "/"]) for _ in range(rng.randint(1, 9)))
        assert remembering.ignored(path) == plain.ignored(path), path
    assert remembering._rules[0][1].__self__._moves and not plain._rules[0][1].__self__._moves


def test_the_time_budget_stops_between_rules_of_one_path(monkeypatch):
    from palimpsest_local import oci_capture

    clock = [100.0]
    calls = []

    def slow(path):
        calls.append(path)
        clock[0] += 0.05
        return False

    monkeypatch.setattr(oci_capture.time, "monotonic", lambda: clock[0])
    matcher = IgnoreMatcher("placeholder\n", budget_seconds=0.12)
    matcher._rules = [(False, slow)] * 20
    with pytest.raises(CaptureError) as error:
        matcher.ignored("some/path")
    assert error.value.code == "plmignore" and len(calls) == 3, (
        "the third rule passes the budget; seventeen must not run"
    )


def test_what_the_matcher_remembers_is_bounded_by_states_not_by_entries(monkeypatch):
    from palimpsest_local import oci_capture

    # Each remembered state set of this rule holds thousands of states. Counting entries instead of
    # their sizes let a 38 KB file hold fifty million states (3.5 GiB) in one commit.
    monkeypatch.setattr(oci_capture, "_MAX_CACHED_STATE", 20_000)
    matcher = IgnoreMatcher(("**a" * 1365) + "\n" + ("**b" * 1365) + "\n")
    wide = "/".join(["a" * 255] * 16)
    assert matcher.ignored(wide) and not matcher.ignored("b/b")
    remembered = 0
    for _, covers in matcher._rules:
        glob = covers.__self__
        remembered += sum(len(states) for states in glob._sets) + len(glob._moves)
    assert 0 < remembered <= 20_000
    # Matching stays correct once nothing more is remembered.
    assert matcher.ignored(wide + "/below") and matcher.ignored("b" * 1365) and not matcher.ignored("b" * 1364)


def test_remembered_transitions_count_against_the_same_allowance(monkeypatch):
    from palimpsest_local import oci_capture

    # A rule with small state sets still remembers one transition per distinct character.
    monkeypatch.setattr(oci_capture, "_MAX_CACHED_STATE", 300)
    matcher = IgnoreMatcher("****z\n")
    for start in range(0x4E00, 0x4E00 + 4000, 40):
        assert not matcher.ignored("".join(chr(start + offset) for offset in range(40)) + "zy")
    glob = matcher._rules[0][1].__self__
    assert 0 < len(glob._moves) + sum(len(states) for states in glob._sets) <= 300
    assert matcher.ignored("\u4e00\u4e01z") and not matcher.ignored("\u4e00\u4e01zy")


@pytest.mark.parametrize("name", ["opt/.wh.app", "etc/ssh/.wh..wh..opq", ".wh.rootfile", "srv/.wh.dir/inner"])
def test_an_ordinary_entry_with_a_reserved_whiteout_name_refuses_the_commit(name):
    # Overlayfs gives ``.wh.`` names no meaning, so a guest can hold such files; written into the
    # layer unchanged they would delete parent content (``opt/app``) or hide a whole directory.
    with pytest.raises(CaptureError) as error:
        _layer([_file(name, b"x"), _file("opt/keep", b"1")])
    assert error.value.code == "capture-path" and "whiteout prefix" in str(error.value)
    # The user can leave such a file out explicitly; a real deletion is still written as a whiteout.
    receipt, _, members = _layer(
        [_file(name, b"x"), _file("opt/keep", b"1"), UpperEntry("opt/gone", UpperKind.WHITEOUT)],
        plmignore=name.removesuffix("/inner") + "\n",
    )
    assert name not in members and "opt/.wh.gone" in members


def test_a_reserved_name_in_the_package_database_cannot_be_excluded_and_still_refuses():
    # Package-database paths are kept whatever the rules say, so the only way out is removing the file.
    for rules in ("var/lib/dpkg/.wh.status-old\n", "var/lib/dpkg/\n", "**/.wh.*\n"):
        with pytest.raises(CaptureError) as error:
            _layer([_file("var/lib/dpkg/.wh.status-old", b"x"), _file("var/lib/dpkg/status", b"s")], plmignore=rules)
        assert error.value.code == "capture-path"


def test_plmignore_capture_root_patterns_and_reinclusion():
    receipt, _, members = _layer(
        [_file("build"), _file("tempa"), _file("subdir/tempa"), _file("subdir/app.log")],
        plmignore="build/\ntemp?\n**/*.log\n",
    )
    assert set(members) == {"subdir/tempa"}
    assert set(receipt.ignored) == {"build", "tempa", "subdir/app.log"}

    receipt, _, members = _layer(
        [
            UpperEntry("build", UpperKind.DIRECTORY),
            _file("build/out/a.o"),
            _file("build/keep/a.o"),
            _file("build/keep/drop.log"),
            _file("foo/bar/nested"),
            _file("subdir/build"),
        ],
        plmignore="build/\n!build/keep/\n**/*.log\n/foo/bar/\n",
    )
    assert set(members) == {"build/keep/a.o", "subdir/build"}
    assert set(receipt.ignored) == {"build", "build/out/a.o", "build/keep/drop.log", "foo/bar/nested"}


@pytest.mark.parametrize("pattern", ["foo/bar", "/foo/bar/", "foo/bar/"])
def test_plmignore_slashes_do_not_change_file_or_directory_matching(pattern):
    receipt, _, members = _layer(
        [_file("foo/bar"), _file("other/foo/bar")],
        plmignore=pattern,
    )
    assert set(members) == {"other/foo/bar"}
    assert receipt.ignored == ("foo/bar",)

    receipt, _, members = _layer(
        [UpperEntry("foo/bar", UpperKind.DIRECTORY), _file("foo/bar/child")],
        plmignore=pattern,
    )
    assert not members
    assert set(receipt.ignored) == {"foo/bar", "foo/bar/child"}


@pytest.mark.parametrize("name", ["trusted.overlay.redirect", "trusted.overlay.metacopy", "trusted.overlay.nlink"])
def test_overlay_redirect_and_metacopy_are_rejected(name):
    with pytest.raises(CaptureError) as error:
        _layer([UpperEntry("a", UpperKind.DIRECTORY, xattrs=((name, b"/b"),))])
    assert error.value.code == "capture-overlay-metadata"


@pytest.mark.parametrize("name", ["trusted.overlay.impure", "trusted.overlay.origin", "user.overlay.impure"])
def test_overlay_copy_up_bookkeeping_is_dropped_and_never_written_to_the_layer(name):
    entries = [
        UpperEntry("opt", UpperKind.DIRECTORY, mode=0o755, xattrs=((name, b"y"), ("user.keep", b"1"))),
        _file("opt/a.txt", b"x"),
    ]
    _, _, members = _layer(entries)
    assert set(members) == {"opt", "opt/a.txt"}
    headers = members["opt"][0].pax_headers
    assert not any("overlay" in key for key in headers)
    assert any("user.keep" in key for key in headers)


def test_special_files_and_bad_paths_are_rejected():
    with pytest.raises(CaptureError):
        _layer([UpperEntry("dev/x", UpperKind.OTHER)])
    for bad in ["/abs", "../up", "a/../b", "a//b", "", "a\0b", "./a"]:
        with pytest.raises(CaptureError):
            normalize_capture_path(bad)


def test_hardlink_requires_emitted_earlier_target_and_output_is_deterministic():
    entries = [_file("a/one", b"d"), UpperEntry("b/two", UpperKind.HARDLINK, link_target="a/one")]
    first = _layer(entries)
    second = _layer(list(reversed(entries)))
    assert first[1] == second[1]
    assert first[2]["b/two"][0].islnk()
    with pytest.raises(CaptureError) as error:
        _layer([_file("z/one", b"d"), UpperEntry("a/two", UpperKind.HARDLINK, link_target="z/one")])
    assert error.value.code == "capture-hardlink"
    with pytest.raises(CaptureError):
        _layer(
            [_file("a/one", b"d"), UpperEntry("b/two", UpperKind.HARDLINK, link_target="a/one")],
            plmignore="a/one",
        )


def test_byte_limit_is_enforced_without_partial_success():
    with pytest.raises(CaptureError) as error:
        _layer([_file("big", b"x" * 4096)], max_bytes=1024)
    assert error.value.code == "capture-limit"


def test_user_overlay_and_opaque_metadata_validation():
    # user.overlay.opaque on directory accepted and converts to .wh..wh..opq
    _, _, members = _layer([UpperEntry("app/cache", UpperKind.DIRECTORY, xattrs=(("user.overlay.opaque", b"y"),))])
    assert "app/cache/.wh..wh..opq" in members

    # Opaque on regular file rejected
    with pytest.raises(CaptureError) as error:
        _layer([_file("app/data.txt", b"secret", xattrs=(("trusted.overlay.opaque", b"y"),))])
    assert error.value.code == "capture-overlay-metadata"

    # Invalid opaque value rejected
    with pytest.raises(CaptureError) as error:
        _layer([UpperEntry("app/cache", UpperKind.DIRECTORY, xattrs=(("trusted.overlay.opaque", b"x"),))])
    assert error.value.code == "capture-overlay-metadata"

    # user.overlay.redirect rejected
    with pytest.raises(CaptureError) as error:
        _layer([UpperEntry("app/cache", UpperKind.DIRECTORY, xattrs=(("user.overlay.redirect", b"/other"),))])
    assert error.value.code == "capture-overlay-metadata"


def test_tar_member_ordering_is_strictly_canonical():
    # .wh.old must sort before new.txt in the same directory because '.' < 'n' in ASCII
    entries = [
        _file("opt/app/new.txt", b"new"),
        UpperEntry("opt/app/old", UpperKind.WHITEOUT),
    ]
    _, raw, _ = _layer(entries)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        names = archive.getnames()
    assert names == ["opt/app/.wh.old", "opt/app/new.txt"]


def test_plmignore_resolution_from_upper_and_parent():
    # Upper /.plmignore file is read automatically when plmignore=None
    entries = [
        _file(".plmignore", b"ignored.txt\n"),
        _file("ignored.txt", b"drop"),
        _file("kept.txt", b"keep"),
    ]
    receipt, _, members = _layer(entries)
    assert set(members) == {".plmignore", "kept.txt"}
    assert receipt.ignored == ("ignored.txt",)

    # When .plmignore is absent from upper, parent_plmignore is consulted
    receipt, _, members = _layer(
        [_file("temp.bin", b"x"), _file("stay.txt", b"y")],
        parent_plmignore="temp.bin\n",
    )
    assert set(members) == {"stay.txt"}
    assert receipt.ignored == ("temp.bin",)


def test_payload_size_mismatch_fails_closed():
    # Reported size 10 but stream yields 4 bytes -> fail closed
    bad_entry = UpperEntry("corrupt.bin", UpperKind.FILE, size=10, payload=b"1234")
    with pytest.raises(CaptureError):
        _layer([bad_entry])


def test_all_mandatory_identity_paths_are_hidden_when_in_parent():
    parent_paths = [
        "etc/ssh/ssh_host_rsa_key",
        "var/lib/cloud/instance/boot.log",
        "var/lib/dhcp/dhclient.leases",
        "root/.ssh/authorized_keys",
        "home/ubuntu/.ssh/authorized_keys",
        "etc/machine-id",  # platform bound: no whiteout
        "etc/hostname",  # platform bound: no whiteout
    ]
    receipt, _, members = _layer(
        [_file("app/run", b"1")],
        parent_paths=parent_paths,
        injected_placeholders=["root/.ssh/authorized_keys", "home/ubuntu/.ssh/authorized_keys"],
    )
    assert "etc/ssh/.wh.ssh_host_rsa_key" in members
    assert "var/lib/.wh.cloud" in members
    assert "var/lib/dhcp/.wh.dhclient.leases" in members
    assert "root/.ssh/.wh.authorized_keys" in members
    assert "home/ubuntu/.ssh/.wh.authorized_keys" in members
    # Platform-bound files need no whiteout (spec.md:639)
    assert "etc/.wh.machine-id" not in members
    assert "etc/.wh.hostname" not in members


def test_unclassified_authorized_keys_in_upper_fails_closed():
    # An unclassified authorized_keys in upper fails closed to prevent leaking credentials
    with pytest.raises(CaptureError) as error:
        _layer([_file("root/.ssh/authorized_keys", b"ssh-ed25519 AAA...")])
    assert error.value.code == "capture-provenance"


def test_unclassified_authorized_keys_in_parent_fails_closed():
    # An unclassified authorized_keys in parent fails closed to prevent inheriting injected credentials
    with pytest.raises(CaptureError) as error:
        _layer([_file("app.py", b"1")], parent_paths=["home/ubuntu/.ssh/authorized_keys"])
    assert error.value.code == "capture-provenance"


def test_user_authored_authorized_keys_are_preserved_with_explicit_classification():
    # Explicitly classified user_authored_keys are preserved in upper and not whiteouted in parent
    user_key = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUserKey user@box\n"
    entries = [
        _file("root/.ssh/authorized_keys", user_key),
        _file("home/developer/.ssh/authorized_keys", user_key),
        _file("app/main.py", b"pass"),
    ]
    parent_paths = [
        "home/operator/.ssh/authorized_keys",
        "etc/ssh/ssh_host_rsa_key",
    ]
    receipt, _, members = _layer(
        entries,
        user_authored_keys=[
            "root/.ssh/authorized_keys",
            "home/developer/.ssh/authorized_keys",
            "home/operator/.ssh/authorized_keys",
        ],
        parent_paths=parent_paths,
    )
    assert "root/.ssh/authorized_keys" in members
    assert members["root/.ssh/authorized_keys"][1] == user_key
    assert "home/developer/.ssh/authorized_keys" in members
    assert members["home/developer/.ssh/authorized_keys"][1] == user_key
    assert "root/.ssh/authorized_keys" not in receipt.excluded
    assert "home/developer/.ssh/authorized_keys" not in receipt.excluded

    # Parent user-authored authorized_keys has no whiteout
    assert "home/operator/.ssh/.wh.authorized_keys" not in members
    assert "home/operator/.ssh/authorized_keys" not in receipt.hidden
    # Parent SSH host key is still hidden
    assert "etc/ssh/.wh.ssh_host_rsa_key" in members
    assert "etc/ssh/ssh_host_rsa_key" in receipt.hidden


def test_injected_authorized_keys_are_excluded_and_hidden_with_explicit_classification():
    # Explicitly classified injected_placeholders are excluded from upper and whiteouted in parent
    entries = [
        _file("root/.ssh/authorized_keys", b"priv"),
        _file("home/ubuntu/.ssh/authorized_keys", b"priv"),
        _file("app/code.py", b"ok"),
    ]
    receipt, _, members = _layer(
        entries,
        injected_placeholders=["root/.ssh/authorized_keys", "home/ubuntu/.ssh/authorized_keys"],
        parent_paths=["root/.ssh/authorized_keys"],
    )
    assert "root/.ssh/authorized_keys" not in members
    assert "home/ubuntu/.ssh/authorized_keys" not in members
    assert "app/code.py" in members
    assert set(receipt.excluded) == {"root/.ssh/authorized_keys", "home/ubuntu/.ssh/authorized_keys"}
    # Parent path with injected classification is hidden
    assert "root/.ssh/.wh.authorized_keys" in members
    assert "root/.ssh/authorized_keys" in receipt.hidden


def test_guest_claim_alone_cannot_classify_authorized_keys():
    # Guest-provided provenance claim alone without caller classification must fail closed
    entry = UpperEntry("root/.ssh/authorized_keys", UpperKind.FILE, size=4, payload=b"priv", provenance="user")
    with pytest.raises(CaptureError) as error:
        _layer([entry])
    assert error.value.code == "capture-provenance"


def test_nonstandard_home_root_authorized_keys_requires_classification():
    # Conservative matching catches nonstandard home roots
    with pytest.raises(CaptureError) as error:
        _layer([_file("export/home/alice/.ssh/authorized_keys", b"key")])
    assert error.value.code == "capture-provenance"

    with pytest.raises(CaptureError) as error:
        _layer([_file("app.py", b"1")], parent_paths=["var/lib/service/.ssh/authorized_keys"])
    assert error.value.code == "capture-provenance"

    # With explicit user classification, nonstandard root is preserved
    _, _, members = _layer(
        [_file("export/home/alice/.ssh/authorized_keys", b"key")],
        user_authored_keys=["export/home/alice/.ssh/authorized_keys"],
    )
    assert "export/home/alice/.ssh/authorized_keys" in members


def test_conflicting_classification_fails_closed():
    with pytest.raises(CaptureError) as error:
        _layer(
            [_file("root/.ssh/authorized_keys", b"key")],
            injected_placeholders=["root/.ssh/authorized_keys"],
            user_authored_keys=["root/.ssh/authorized_keys"],
        )
    assert error.value.code == "capture-provenance"


def test_mandatory_precedence_prevents_reinclusion_of_injected_keys():
    entries = [_file("root/.ssh/authorized_keys", b"priv")]
    receipt, _, members = _layer(
        entries,
        injected_placeholders=["root/.ssh/authorized_keys"],
        plmignore="!root/.ssh/authorized_keys\n",
    )
    assert "root/.ssh/authorized_keys" not in members
    assert receipt.excluded == ("root/.ssh/authorized_keys",)


def test_unrecognized_entry_provenance_fails_closed():
    entry = UpperEntry("etc/hostname", UpperKind.FILE, size=1, payload=b"a", provenance="unverified-guess")
    with pytest.raises(CaptureError) as error:
        _layer([entry])
    assert error.value.code == "capture-provenance"


def test_cloud_descendants_in_parent_collapse_to_ancestor_whiteout():
    # Multiple descendants under var/lib/cloud in parent must produce exactly
    # one whiteout at var/lib/.wh.cloud and not individual child whiteouts
    parent_paths = [
        "var/lib/cloud/instance/boot.log",
        "var/lib/cloud/data/status.json",
        "var/lib/cloud/handlers",
    ]
    receipt, _, members = _layer([_file("app.txt", b"x")], parent_paths=parent_paths)
    assert "var/lib/.wh.cloud" in members
    assert "var/lib/cloud/instance/.wh.boot.log" not in members
    assert receipt.hidden == ("var/lib/cloud",)


def test_plmignore_size_limits_fail_closed():
    oversized = b"a\n" * (600 * 1024)  # ~1.2 MiB > 1 MiB limit
    # Upper .plmignore
    entries = [_file(".plmignore", oversized)]
    with pytest.raises(CaptureError) as error:
        _layer(entries)
    assert error.value.code == "plmignore"

    # Parent .plmignore
    with pytest.raises(CaptureError) as error:
        _layer([_file("app.py", b"1")], parent_plmignore=oversized)
    assert error.value.code == "plmignore"

    # Direct IgnoreMatcher
    with pytest.raises(CaptureError) as error:
        IgnoreMatcher(oversized)
    assert error.value.code == "plmignore"


def test_a_plmignore_that_is_not_utf8_is_a_typed_refusal_from_either_source():
    for arguments in (
        {"entries": [_file(".plmignore", b"opt/\xff\n"), _file("app.py", b"1")]},
        {"entries": [_file("app.py", b"1")], "parent_plmignore": b"opt/\xff\n"},
    ):
        with pytest.raises(CaptureError) as error:
            _layer(arguments.pop("entries"), **arguments)
        assert error.value.code == "plmignore"


def test_streamed_payload_size_excess_fails_closed():
    # Declared size 4 but stream yields 8 bytes -> fail closed
    entry = UpperEntry("overflow.bin", UpperKind.FILE, size=4, payload=b"12345678")
    with pytest.raises(CaptureError) as error:
        _layer([entry])
    assert error.value.code == "capture-payload"


def test_logs_caches_tmp_and_package_database_are_included_by_default():
    entries = [
        _file("var/log/app.log", b"log"),
        _file("var/cache/app/index", b"cache"),
        _file("tmp/work.bin", b"tmp"),
        _file("var/lib/dpkg/status", b"Package: gcc\n"),
    ]
    receipt, _, members = _layer(entries)
    assert set(members) == {
        name for name in ("var/log/app.log", "var/cache/app/index", "tmp/work.bin", "var/lib/dpkg/status")
    }
    assert not receipt.ignored
    assert not receipt.retained


def test_plmignore_may_drop_logs_and_caches_but_never_the_package_database():
    entries = [
        _file("var/log/app.log", b"log"),
        _file("var/cache/app/index", b"cache"),
        _file("var/lib/dpkg/status", b"Package: gcc\n"),
        _file("var/lib/dpkg/info/gcc.list", b"/usr/bin/gcc\n"),
        _file("var/lib/other/data", b"keep"),
    ]
    receipt, _, members = _layer(entries, plmignore="var/log\nvar/cache\nvar/lib/dpkg\nvar/lib/dpkg/**\n")
    assert set(members) == {"var/lib/dpkg/status", "var/lib/dpkg/info/gcc.list", "var/lib/other/data"}
    assert set(receipt.ignored) == {"var/log/app.log", "var/cache/app/index"}
    assert set(receipt.retained) == {"var/lib/dpkg/status", "var/lib/dpkg/info/gcc.list"}
