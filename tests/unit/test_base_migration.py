from __future__ import annotations

import pytest

from palimpsest_local.base_migration import (
    SCHEMA,
    Entry,
    MigrationInputError,
    check,
    flatten,
    report_digest,
)


def f(path, content="c1", **kw):
    return Entry(path, "file", kw.pop("mode", 0o644), size=kw.pop("size", len(content or "")), content=content, **kw)


def d(path, mode=0o755, **kw):
    return Entry(path, "dir", mode, **kw)


def link(path, target):
    return Entry(path, "symlink", 0o777, link=target)


def wh(path):
    return Entry(path, "file", whiteout=True)


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
    assert finding["old"]["content"] == "v1" and finding["new"]["content"] == "v2"
    assert finding["layer_entry"]["content"] == "mine" and finding["package_state"] is False
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


def test_a_file_in_the_layer_over_a_new_base_directory_is_a_kind_conflict_unless_it_was_one_before():
    layer = [("x", [f("opt/data", "blob")])]
    report = check(tree(d("opt")), tree(d("opt"), d("opt/data"), f("opt/data/new", "n")), layer)
    assert report["verdict"] == "conflict" and codes(report) == [("kind-conflict", "opt/data")]
    before = tree(d("opt"), d("opt/data"), f("opt/data/old", "o"))
    assert check(before, tree(d("opt"), d("opt/data"), f("opt/data/new", "n")), layer)["verdict"] == "clean"


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
    # A different size is a definite change even without content identities.
    sized = check(tree(f("etc/app.conf", None, size=4)), tree(f("etc/app.conf", None, size=9)), layer)
    assert sized["verdict"] == "conflict"
    partial = check({}, {}, [("app", [f("opt/x", "1")])], complete=False)
    assert partial["verdict"] == "incomplete" and partial["findings"] == [] and partial["inputs_complete"] is False


def test_a_truncated_finding_list_is_incomplete_even_when_every_finding_is_a_conflict():
    layer = [("many", [f(f"etc/f{index}", "mine") for index in range(5)])]
    old = tree(*[f(f"etc/f{index}", "v1") for index in range(5)])
    new = tree(*[f(f"etc/f{index}", "v2") for index in range(5)])
    report = check(old, new, layer, max_findings=3)
    assert report["verdict"] == "incomplete" and report["findings_truncated"] is True
    assert len(report["findings"]) == 3 and report["counts"]["conflict"] == 5


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
