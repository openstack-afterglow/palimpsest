from __future__ import annotations

import hashlib
import json
import random

import pytest

from palimpsest_local.base_migration import (
    MAX_FINDINGS,
    MAX_NOTICES,
    SCHEMA,
    Entry,
    MigrationInputError,
    check,
    flatten,
    report_digest,
)


def digest(content):
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def f(path, content="c1", **kw):
    return Entry(
        path,
        "file",
        kw.pop("mode", 0o644),
        size=kw.pop("size", len(content or "")),
        content=None if content is None else digest(content),
        **kw,
    )


def d(path, mode=0o755, **kw):
    return Entry(path, "dir", mode, **kw)


def link(path, target):
    return Entry(path, "symlink", 0o777, link=target)


def wh(path):
    return Entry(path, whiteout=True)


def tree(*entries):
    return {entry.path: entry for entry in entries}


def codes(report):
    return [(item["code"], item["path"]) for item in report["findings"]]


def test_a_layer_that_only_adds_paths_neither_base_has_is_clean():
    old = tree(d("etc"), f("etc/os-release", "22.04.1"))
    new = tree(d("etc"), f("etc/os-release", "22.04.2"))
    report = check(old, new, [("app", [d("opt", 0o755), d("opt/app"), f("opt/app/main.py", "print")])])
    assert report["schema"] == SCHEMA and report["verdict"] == "clean"
    assert report["requires_risk_acknowledgement"] is False and report["findings"] == []
    assert report["layers"] == [{"index": 0, "id": "app", "entries": 3}]


def test_overwriting_a_base_file_matters_only_when_the_base_changed_it():
    layer = [("cfg", [d("etc"), f("etc/app.conf", "mine")])]
    unchanged = tree(d("etc"), f("etc/app.conf", "v1"))
    assert check(unchanged, dict(unchanged), layer)["verdict"] == "clean"
    report = check(unchanged, tree(d("etc"), f("etc/app.conf", "v2")), layer)
    assert report["verdict"] == "conflict" and codes(report) == [("overwrite-changed", "etc/app.conf")]
    finding = report["findings"][0]
    assert finding["old"]["content"] == digest("v1") and finding["new"]["content"] == digest("v2")
    assert finding["layer_entry"]["content"] == digest("mine") and finding["package_state"] is False
    # The new base now ships exactly what the layer carries: reported, but not a conflict.
    same = check(unchanged, tree(d("etc"), f("etc/app.conf", "mine")), layer)
    assert same["verdict"] == "clean" and codes(same) == [("overwrite-now-identical", "etc/app.conf")]


def test_a_path_the_layer_added_collides_when_the_new_base_starts_shipping_it():
    layer = [("tool", [d("usr"), d("usr/bin"), f("usr/bin/tool", "layer-build")])]
    old = tree(d("usr"), d("usr/bin"))
    report = check(old, tree(d("usr"), d("usr/bin"), f("usr/bin/tool", "distro-build")), layer)
    assert report["verdict"] == "conflict" and codes(report) == [("new-path-collision", "usr/bin/tool")]
    identical = check(old, tree(d("usr"), d("usr/bin"), f("usr/bin/tool", "layer-build")), layer)
    assert identical["verdict"] == "clean" and codes(identical) == [("new-path-identical", "usr/bin/tool")]


def test_a_directory_in_the_layer_over_a_new_symlink_is_a_kind_conflict():
    # The usr-merge shape: the old base had a real /lib, the new one links it to usr/lib.
    layer = [("libs", [d("lib"), f("lib/libfoo.so", "so")])]
    old = tree(d("lib"), d("usr"), d("usr/lib"))
    new = tree(link("lib", "usr/lib"), d("usr"), d("usr/lib"))
    report = check(old, new, layer)
    assert report["verdict"] == "conflict" and ("kind-conflict", "lib") in codes(report)
    # When the old base already had the same link, the old combination shadowed it too.
    assert check(dict(new), new, layer)["verdict"] == "clean"


def test_an_entry_below_a_non_directory_of_the_new_base_is_reported_once_per_ancestor():
    layer = [("libs", [f("lib/a.so", "a"), f("lib/b.so", "b")])]  # no directory entry of its own
    report = check(tree(d("lib")), tree(link("lib", "usr/lib")), layer)
    assert report["verdict"] == "conflict" and codes(report) == [("ancestor-not-directory", "lib")]


def test_non_directory_over_base_directory_checks_attributes_and_hidden_children():
    layer = [("x", [f("opt/data", "blob")])]
    report = check(tree(d("opt")), tree(d("opt"), d("opt/data"), f("opt/data/new", "n")), layer)
    assert report["verdict"] == "conflict"
    assert codes(report) == [("kind-conflict", "opt/data"), ("opaque-hides-changes", "opt/data")]
    before = tree(d("opt"), d("opt/data"), f("opt/data/old", "o"))
    changed = check(before, tree(d("opt"), d("opt/data"), f("opt/data/new", "n")), layer)
    assert changed["verdict"] == "conflict" and codes(changed) == [("opaque-hides-changes", "opt/data")]
    attributes = check(before, tree(d("opt"), d("opt/data", 0o700, uid=1000)), layer)
    assert codes(attributes) == [("kind-conflict", "opt/data")]


def test_whiteouts_conflict_only_when_they_would_delete_something_new():
    layer = [("rm", [d("etc"), wh("etc/legacy.conf"), wh("etc/extra.conf")])]
    old = tree(d("etc"), f("etc/legacy.conf", "v1"))
    gone = check(old, tree(d("etc")), layer)
    assert gone["verdict"] == "clean" and codes(gone) == [("whiteout-target-gone", "etc/legacy.conf")]
    changed = check(old, tree(d("etc"), f("etc/legacy.conf", "v2")), layer)
    assert changed["verdict"] == "clean" and codes(changed) == [("whiteout-target-changed", "etc/legacy.conf")]
    new_file = check(old, tree(d("etc"), f("etc/legacy.conf", "v1"), f("etc/extra.conf", "new")), layer)
    assert new_file["verdict"] == "conflict" and codes(new_file) == [("whiteout-hides-new", "etc/extra.conf")]


def test_an_opaque_directory_conflicts_when_it_would_hide_new_or_changed_base_content():
    layer = [("own", [d("srv", opaque=True), f("srv/index", "mine")])]
    old = tree(d("srv"), f("srv/default", "v1"))
    assert check(old, dict(old), layer)["verdict"] == "clean"
    report = check(old, tree(d("srv"), f("srv/default", "v1"), f("srv/added", "new")), layer)
    assert report["verdict"] == "conflict" and codes(report) == [("opaque-hides-changes", "srv")]
    assert check(old, tree(d("srv"), f("srv/default", "v2")), layer)["verdict"] == "conflict"


def test_directory_attributes_changed_by_the_base_and_overridden_by_the_layer_conflict():
    layer = [("x", [d("var", 0o755), d("var/spool", 0o755), f("var/spool/job", "j")])]
    old = tree(d("var"), d("var/spool", 0o755))
    report = check(old, tree(d("var"), d("var/spool", 0o1777)), layer)
    assert report["verdict"] == "conflict" and codes(report) == [("attributes-shadowed", "var/spool")]
    # The layer already carries the new attributes: nothing is overridden.
    agreeing = [("x", [d("var"), d("var/spool", 0o1777)])]
    assert check(old, tree(d("var"), d("var/spool", 0o1777)), agreeing)["verdict"] == "clean"


def test_package_state_conflicts_are_counted_separately():
    layer = [("apt", [d("var"), d("var/lib"), d("var/lib/dpkg"), f("var/lib/dpkg/status", "layer"), f("etc/app", "a")])]
    old = tree(d("var"), d("var/lib"), d("var/lib/dpkg"), f("var/lib/dpkg/status", "old"), f("etc/app", "1"))
    new = tree(d("var"), d("var/lib"), d("var/lib/dpkg"), f("var/lib/dpkg/status", "new"), f("etc/app", "2"))
    report = check(old, new, layer)
    assert report["verdict"] == "conflict"
    assert report["counts"] == {"conflict": 2, "notice": 0, "incomplete": 0, "package_state_conflict": 1}
    assert {item["path"]: item["package_state"] for item in report["findings"]} == {
        "var/lib/dpkg/status": True,
        "etc/app": False,
    }


def test_missing_content_identity_or_partial_input_is_incomplete_never_clean():
    layer = [("cfg", [f("etc/app.conf", "mine")])]
    unknown = check(tree(f("etc/app.conf", None, size=4)), tree(f("etc/app.conf", None, size=4)), layer)
    assert unknown["verdict"] == "incomplete" and unknown["requires_risk_acknowledgement"] is True
    assert codes(unknown) == [("comparison-unavailable", "etc/app.conf")]
    # A definite size difference does not compensate for missing required identities.
    sized = check(tree(f("etc/app.conf", None, size=4)), tree(f("etc/app.conf", None, size=9)), layer)
    assert sized["verdict"] == "incomplete"
    partial = check({}, {}, [("app", [f("opt/x", "1")])], complete=False)
    assert partial["verdict"] == "incomplete" and partial["findings"] == [] and partial["inputs_complete"] is False


def test_a_truncated_finding_list_is_incomplete_even_when_every_finding_is_a_conflict():
    size = MAX_FINDINGS + 2
    layer = [("many", [f(f"etc/f{index:04}", "mine") for index in range(size)])]
    old = tree(*[f(f"etc/f{index:04}", "v1") for index in range(size)])
    new = tree(*[f(f"etc/f{index:04}", "v2") for index in range(size)])
    report = check(old, new, layer)
    assert report["verdict"] == "incomplete" and report["findings_truncated"] is True
    assert len(report["findings"]) == MAX_FINDINGS and report["counts"]["conflict"] == size
    assert [item["path"] for item in report["findings"]] == [f"etc/f{index:04}" for index in range(MAX_FINDINGS)]


def test_each_kept_layer_is_reported_with_its_order_and_the_report_identity_is_stable():
    old, new = tree(f("a", "1"), f("b", "1")), tree(f("a", "2"), f("b", "2"))
    layers = [("first", [f("b", "x")]), ("second", [f("a", "y")])]
    report = check(old, new, layers)
    assert [(item["layer_index"], item["layer"], item["path"]) for item in report["findings"]] == [
        (0, "first", "b"),
        (1, "second", "a"),
    ]
    again = check(
        dict(reversed(list(old.items()))), new, [(name, list(reversed(list(items)))) for name, items in layers]
    )
    assert report_digest(again) == report_digest(report) and report_digest(report).startswith("sha256:")
    assert report_digest(check(old, old, layers)) != report_digest(report)


def test_flatten_applies_whiteouts_opaque_directories_and_kind_changes_in_layer_order():
    base = [d("etc"), f("etc/a", "1"), f("etc/b", "1"), d("srv"), f("srv/x", "1"), d("lib"), f("lib/l", "1")]
    upper = [d("etc"), wh("etc/a"), f("etc/c", "2"), d("srv", opaque=True), f("srv/y", "2"), link("lib", "usr/lib")]
    flat = flatten([base, upper])
    assert sorted(flat) == ["etc", "etc/b", "etc/c", "lib", "srv", "srv/y"]
    assert flat["lib"].kind == "symlink" and flat["srv"].opaque is False


@pytest.mark.parametrize("path", ["", "/etc", "etc/../x", "./etc", "..", "a//b", "a/\0b"])
def test_paths_that_are_not_normalized_relative_paths_are_refused(path):
    with pytest.raises(MigrationInputError):
        Entry(path, "file")


def test_malformed_entries_and_duplicate_layer_paths_are_refused():
    with pytest.raises(MigrationInputError):
        Entry("a", "door")
    with pytest.raises(MigrationInputError):
        Entry("a", "file", opaque=True)
    with pytest.raises(MigrationInputError):
        check({}, {}, [("dup", [f("a", "1"), f("a", "2")])])


@pytest.mark.parametrize("replacement", [wh, lambda path: link(path, "/config"), lambda path: f(path, "blob")])
def test_directory_hiding_checks_new_changed_and_unknown_descendants(replacement):
    old = tree(d("etc"), d("etc/app"), f("etc/app/a", "1"), f("etc/app/u", None))
    new = tree(d("etc"), d("etc/app"), f("etc/app/a", "2"), f("etc/app/u", None), f("etc/app/fix", "new"))
    report = check(old, new, [("hide", [d("etc"), replacement("etc/app")])])
    assert report["verdict"] == "incomplete"
    expected = (
        [
            ("comparison-unavailable", "etc/app"),
            ("whiteout-hides-new", "etc/app"),
            ("whiteout-target-changed", "etc/app"),
        ]
        if replacement is wh
        else [("comparison-unavailable", "etc/app"), ("opaque-hides-changes", "etc/app")]
    )
    assert codes(report) == expected


def test_whiteout_compares_own_directory_attributes_and_aggregates_descendants():
    old = tree(d("srv"), d("srv/www"), f("srv/www/a", "old"))
    new = tree(d("srv"), d("srv/www", 0o750), f("srv/www/a", "new"), f("srv/www/index.html", "new"))
    report = check(old, new, [("rm", [d("srv"), wh("srv/www")])])
    assert report["verdict"] == "conflict"
    assert codes(report) == [("whiteout-hides-new", "srv/www"), ("whiteout-target-changed", "srv/www")]
    assert report["findings"][0]["layer_entry"] == {"kind": "whiteout"}
    assert report["findings"][1]["old"]["mode"] == 0o755
    assert report["findings"][1]["new"]["mode"] == 0o750


def test_changed_whiteout_descendant_is_notice_and_removed_descendants_are_ignored():
    old = tree(d("a"), f("a/file", "old"), f("a/gone"))
    new = tree(d("a"), f("a/file", "new"))
    report = check(old, new, [("rm", [wh("a")])])
    assert report["verdict"] == "clean" and codes(report) == [("whiteout-target-changed", "a")]
    assert check(old, tree(d("a")), [("rm", [wh("a")])])["findings"] == []


def test_opaque_unknown_subtree_does_not_disappear_beside_known_conflict():
    old = tree(d("a"), f("a/u", None))
    new = tree(d("a"), f("a/u", None), f("a/new"))
    report = check(old, new, [("opaque", [d("a", opaque=True)])])
    assert report["verdict"] == "incomplete"
    assert codes(report) == [("comparison-unavailable", "a"), ("opaque-hides-changes", "a")]
    assert report["findings"][0]["layer_entry"]["opaque"] is True
    assert check(old, old, [("opaque", [d("a", opaque=True)])])["verdict"] == "incomplete"


def test_unavailable_ancestor_is_reported_once_with_no_layer_entry():
    old = tree(f("a", None, size=2))
    new = tree(f("a", None, size=9))
    report = check(old, new, [("child", [f("a/one"), f("a/two")])])
    assert report["verdict"] == "incomplete"
    assert codes(report) == [("comparison-unavailable", "a")]
    assert report["findings"][0]["layer_entry"] is None


def test_new_base_directory_becomes_symlink_and_layer_child_cannot_follow_it():
    old = tree(d("a"), f("a/old"))
    new = tree(link("a", "target"), d("target"), f("target/old"))
    assert codes(check(old, new, [("own", [d("a"), f("a/child")])])) == [("kind-conflict", "a")]
    child = check(old, new, [("child", [f("a/new")])])
    assert child["verdict"] == "conflict" and codes(child) == [("ancestor-not-directory", "a")]


def test_flatten_directory_to_non_directory_hides_all_children_and_preserves_xattrs():
    flat = flatten([[d("a"), d("a/sub"), f("a/sub/file")], [link("a", "target")]])
    assert flat == tree(link("a", "target"))
    flat = flatten([[link("a", "target")], [d("a", xattrs=digest("{}")), f("a/file")]])
    assert flat["a"].xattrs == digest("{}") and flat["a/file"].kind == "file"


@pytest.mark.parametrize(
    "path", [".", "/", "a/", "a/.", "a/../b", ".wh.foo", "a/.wh..wh..opq", ".wh.hidden/a", "a/\udcff"]
)
def test_additional_invalid_marker_root_and_non_utf8_paths_are_refused(path):
    with pytest.raises(MigrationInputError):
        Entry(path, "file")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "file"},
        {"kind": "door"},
        {"mode": 1},
        {"uid": 1},
        {"gid": 1},
        {"size": 1},
        {"content": ""},
        {"link": "x"},
        {"opaque": True},
        {"xattrs": digest("{}")},
    ],
)
def test_whiteout_rejects_every_kind_attribute_or_payload(kwargs):
    with pytest.raises(MigrationInputError):
        Entry("a", whiteout=True, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "char", "link": "0:0"},
        {"kind": "block", "link": "00:1"},
        {"kind": "char", "link": "-1:0"},
        {"kind": "char"},
        {"kind": "symlink"},
        {"kind": "dir", "link": "x"},
        {"kind": "dir", "content": "x"},
        {"kind": "dir", "size": 1},
        {"kind": "file", "mode": 0o100644},
        {"kind": "file", "uid": -1},
        {"kind": "file", "gid": True},
        {"kind": "file", "size": 1.5},
        {"kind": "file", "whiteout": 1},
        {"kind": "dir", "opaque": 1},
        {"kind": "file", "link": "x\0y"},
        {"kind": "file", "content": b"bytes"},
        {"kind": "file", "xattrs": "sha256:bad"},
    ],
)
def test_invalid_entry_fields_are_refused(kwargs):
    with pytest.raises(MigrationInputError):
        Entry("a", **kwargs)


@pytest.mark.parametrize(
    "operation",
    [
        lambda: flatten([[f("a"), f("a", "other")]]),
        lambda: check({"wrong": f("a")}, {}, []),
        lambda: check(tree(wh("a")), {}, []),
        lambda: check({}, tree(d("a", opaque=True)), []),
        lambda: check(tree(link("a", "x"), f("a/b")), {}, []),
        lambda: check({}, tree(f("a"), f("a/b")), []),
        lambda: check({}, {}, [("bad", [f("a"), f("a/b")])]),
        lambda: check({}, {}, [("bad", [link("a", "x"), f("a/b")])]),
        lambda: check({}, {}, [("bad", [wh("a"), wh("a/b")])]),
        lambda: flatten([[link("a", "x")], [f("a/b")]]),
        lambda: flatten([[f("a"), f("a/b")]]),
        lambda: check({}, {}, [("", [])]),
        lambda: check({}, {}, [("bad\udcff", [])]),
        lambda: check({}, {}, [], complete=1),
        lambda: flatten([["not an entry"]]),
    ],
)
def test_malformed_listings_are_refused(operation):
    with pytest.raises(MigrationInputError):
        operation()


def test_opaque_and_child_whiteout_apply_to_lower_only():
    old = tree(d("a"), f("a/gone"), f("a/changed", "old"))
    new = tree(d("a"), f("a/gone"), f("a/changed", "new"), f("a/fix"))
    layer = [d("a", opaque=True), wh("a/gone"), f("a/own")]
    report = check(old, new, [("mix", layer)])
    assert report["verdict"] == "conflict" and codes(report) == [("opaque-hides-changes", "a")]
    assert flatten([list(new.values()), layer]) == tree(d("a"), f("a/own"))


def test_kept_layers_compare_bases_not_earlier_kept_layers():
    old, new = tree(f("foo", "v1")), tree(f("foo", "v2"))
    layers = [("delete", [wh("foo")]), ("add", [f("foo", "mine")])]
    report = check(old, new, layers)
    assert report["verdict"] == "conflict"
    assert codes(report) == [("overwrite-changed", "foo"), ("whiteout-target-changed", "foo")]
    assert flatten([list(old.values()), *[entries for _, entries in layers]]) == flatten(
        [list(new.values()), *[entries for _, entries in layers]]
    )


@pytest.mark.parametrize("identity", [None, "md5:" + "a" * 32, "sha256:bad", "sha256:" + "A" * 64, "plain"])
def test_noncanonical_content_identities_are_unavailable_even_if_attributes_differ(identity):
    invalid = Entry("a", "file", 0o600, size=1, content=identity)
    report = check(tree(invalid), tree(f("a", "new")), [("own", [f("a", "mine")])])
    assert report["verdict"] == "incomplete" and codes(report) == [("comparison-unavailable", "a")]


@pytest.mark.parametrize("kind", ["file", "dir", "symlink", "char", "block", "fifo", "socket"])
def test_xattrs_change_and_one_sided_identity_are_compared_for_every_kind(kind):
    def entry(xattrs):
        return Entry(
            "a",
            kind,
            0o755,
            size=1 if kind == "file" else 0,
            content=digest("x") if kind == "file" else None,
            link="target" if kind == "symlink" else "1:3" if kind in ("char", "block") else "",
            xattrs=xattrs,
        )

    old, new, own = entry(digest("old")), entry(digest("new")), entry(digest("mine"))
    changed = check(tree(old), tree(new), [("x", [own])])
    assert changed["verdict"] == "conflict"
    assert codes(changed) == [("attributes-shadowed" if kind == "dir" else "overwrite-changed", "a")]
    assert check(tree(entry(None)), tree(new), [("x", [own])])["verdict"] == "incomplete"
    assert check(tree(old), tree(entry(None)), [("x", [own])])["verdict"] == "incomplete"
    assert check(tree(old), tree(new), [("x", [new])])["verdict"] == "clean"
    assert check(tree(old), tree(new), [("x", [entry(None)])])["verdict"] == "incomplete"


@pytest.mark.parametrize("mode", [0o1644, 0o2644, 0o4644, 0o600])
def test_all_permission_and_special_mode_bits_matter(mode):
    report = check(tree(f("a", "x")), tree(f("a", "x", mode=mode)), [("x", [f("a", "x")])])
    assert report["verdict"] == "conflict"


def test_devices_and_summary_encoding():
    old = tree(Entry("dev", "char", 0o600, link="1:3"))
    new = tree(Entry("dev", "char", 0o600, link="1:5"))
    report = check(old, new, [("devices", list(old.values()))])
    assert report["verdict"] == "conflict"
    assert report["findings"][0]["new"] == {
        "kind": "char",
        "mode": 0o600,
        "uid": 0,
        "gid": 0,
        "xattrs": None,
        "link": "1:5",
    }
    assert check({}, tree(link("a", "target")), [("l", [link("a", "other")])])["findings"][0]["new"]["link"] == "target"


@pytest.mark.parametrize("prefix", ["usr/lib/sysimage/rpm", "var/lib/yum", "var/cache/yum", "var/lib/pacman"])
def test_added_package_state_paths_and_component_boundaries(prefix):
    paths = [prefix, prefix + "/db", prefix + "-not-state"]
    for path in paths:
        report = check(tree(f(path, "old")), tree(f(path, "new")), [("package", [f(path, "mine")])])
        expected = int(path != prefix + "-not-state")
        assert report["counts"]["conflict"] == 1 and report["counts"]["package_state_conflict"] == expected
        assert report["findings"][0]["package_state"] is bool(expected)


@pytest.mark.parametrize("with_conflict", [False, True])
def test_notice_bound_is_separate_and_never_changes_verdict(with_conflict):
    paths = [f"a{index:04}" for index in range(MAX_NOTICES + 2)]
    old, new = tree(*(f(path, "old") for path in paths)), tree(*(f(path, "new") for path in paths))
    entries = [f(path, "new") for path in reversed(paths)]
    if with_conflict:
        old["z"], new["z"] = f("z", "old"), f("z", "new")
        entries.append(f("z", "mine"))
    report = check(old, new, [("many", entries)])
    assert report["verdict"] == ("conflict" if with_conflict else "clean")
    assert report["findings_truncated"] is True
    assert report["counts"] == {
        "conflict": int(with_conflict),
        "notice": MAX_NOTICES + 2,
        "incomplete": 0,
        "package_state_conflict": 0,
    }
    assert [item["path"] for item in report["findings"]] == (["z"] if with_conflict else []) + paths[:MAX_NOTICES]


def test_mixed_important_bound_retains_incomplete_and_conflict_before_notices():
    paths = [f"a{index:04}" for index in range(MAX_FINDINGS)]
    old, new = tree(*(f(path, "old") for path in paths)), tree(*(f(path, "new") for path in paths))
    old["0"], new["0"] = f("0", None), f("0", None)
    report = check(old, new, [("many", [f("z"), *[f(path, "mine") for path in reversed(paths)], f("0")])])
    assert report["verdict"] == "incomplete"
    assert report["counts"]["incomplete"] == 1 and report["counts"]["conflict"] == MAX_FINDINGS
    assert codes(report) == [("comparison-unavailable", "0")] + [("overwrite-changed", path) for path in paths[:-1]]


def test_unicode_paths_are_exact_case_sensitive_and_sort_by_code_point():
    paths = ["é", "e\u0301", "A", "a", "\U0001f600"]
    old, new = tree(*(f(path, "old") for path in paths)), tree(*(f(path, "new") for path in paths))
    report = check(old, new, [("unicode", [f(path, "mine") for path in paths])])
    assert [item["path"] for item in report["findings"]] == sorted(paths)
    assert set(flatten([list(old.values())])) == set(paths)


def test_shuffled_listings_preserve_flatten_report_and_identity():
    rng = random.Random(42)
    old = [d("a"), f("a/x", "old"), d("b"), f("b/gone"), f("é", "old")]
    new = [d("a", 0o700), f("a/x", "new"), f("a/fix"), d("b"), f("é", "new")]
    kept = [d("a", opaque=True), f("a/x", "mine"), wh("b"), f("é", "mine")]
    expected = check(tree(*old), tree(*new), [("own", kept)])
    flat = flatten([old, kept])
    for _ in range(20):
        rng.shuffle(old)
        rng.shuffle(new)
        rng.shuffle(kept)
        actual = check(tree(*old), tree(*new), [("own", kept)])
        assert actual == expected and report_digest(actual) == report_digest(expected)
        assert flatten([old, kept]) == flat


def test_subtree_and_flatten_work_is_not_whiteouts_times_base_size(monkeypatch):
    import palimpsest_local.base_migration as module

    lower = [d("a"), *[f(f"a/f{index}") for index in range(1000)]]
    whiteouts = [wh(f"a/f{index}") for index in range(100)]
    under_calls = 0
    original_under = module._under

    def counted_under(*args):
        nonlocal under_calls
        under_calls += 1
        return original_under(*args)

    monkeypatch.setattr(module, "_under", counted_under)
    assert len(flatten([lower, whiteouts])) == 901
    assert under_calls < 4 * (len(lower) + len(whiteouts))
    # Deeply overlapping opaque prefixes must not compare each subtree again.
    directories = [d("/".join(["a"] * depth)) for depth in range(1, 101)]
    base = tree(*directories, *[f(directories[-1].path + f"/f{index}") for index in range(100)])
    calls = 0
    original_same = module._same

    def counted_same(*args):
        nonlocal calls
        calls += 1
        return original_same(*args)

    monkeypatch.setattr(module, "_same", counted_same)
    kept = [d(entry.path, opaque=True) for entry in directories]
    assert check(base, base, [("deep", kept)])["verdict"] == "clean"
    assert calls <= len(base) + 3 * len(kept)


def test_exact_full_report_json_and_fixed_digest():
    expected_json = """{"schema":"palimpsest.base-migration-check.v1","verdict":"conflict","requires_risk_acknowledgement":true,"inputs_complete":true,"layers":[{"index":0,"id":"remove","entries":1}],"counts":{"conflict":1,"notice":0,"incomplete":0,"package_state_conflict":0},"findings":[{"layer_index":0,"layer":"remove","code":"whiteout-hides-new","severity":"conflict","path":"a","package_state":false,"old":null,"new":{"kind":"dir","mode":493,"uid":0,"gid":0,"xattrs":null},"layer_entry":{"kind":"whiteout"}}],"findings_truncated":false,"statement":"Structure check only: it compares paths, kinds, attributes and content identities of the old and new base against the paths the kept layers touch. It does not check ABI, package dependencies, or whether the migrated image runs; that verification is the user's."}"""
    report = check({}, tree(d("a")), [("remove", [wh("a")])])
    assert report == json.loads(expected_json)
    assert report_digest(report) == "sha256:d046b83abcef7729a7aa095f4b94f79de8d6891a52d8b56b9fd84fa384fb8f03"


def test_digest_covers_service_members_and_canonical_escaping():
    report = check({}, {}, [])
    enriched = dict(
        report,
        existing_image_manifest_digest=digest("existing"),
        old_base_manifest_digest=digest("old"),
        new_base_manifest_digest=digest("new"),
        existing_os={"family": "ubuntu", "version": "22.04"},
        old_base_os={"family": "ubuntu", "version": "22.04"},
        new_base_os={"family": "ubuntu", "version": "24.04"},
        os_release_changed=True,
        future_member={"é": "\U0001f600/\n"},
    )
    raw = json.dumps(enriched, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode(
        "utf-8"
    )
    assert b"\\u00e9" in raw and b"\\ud83d\\ude00/\\n" in raw
    assert report_digest(enriched) == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert report_digest(enriched) != report_digest(report)
    for key in enriched.keys() - report.keys():
        changed = {name: value for name, value in enriched.items() if name != key}
        assert report_digest(changed) != report_digest(enriched)
    with pytest.raises(ValueError):
        report_digest({"bad": float("nan")})


@pytest.mark.parametrize("replacement", [lambda: f("a"), lambda: link("a", "target")])
@pytest.mark.parametrize("change", ["added", "changed", "unknown"])
def test_non_directory_hides_implicit_directory_in_sparse_base_listings(replacement, change):
    old = {} if change == "added" else tree(f("a/child", None if change == "unknown" else "old"))
    new = tree(f("a/child", None if change == "unknown" else "new"))
    report = check(old, new, [("sparse", [replacement()])])
    expected = "comparison-unavailable" if change == "unknown" else "opaque-hides-changes"
    assert codes(report) == [(expected, "a")]
    assert report["verdict"] == ("incomplete" if change == "unknown" else "conflict")


def test_non_string_entry_kind_is_refused_with_migration_input_error():
    with pytest.raises(MigrationInputError):
        Entry("a", [])
