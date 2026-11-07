from __future__ import annotations

from dataclasses import replace

import pytest

from palimpsest_local.oci_changeset import (
    ChangesetMember,
    ChangesetValidationError,
    EffectiveFilesystem,
    EntryKind,
    normalize_changeset,
)


def _member(
    ordinal: int,
    path: str,
    kind: EntryKind,
    *,
    payload: bytes | None = None,
    target: str = "",
    size: int | None = None,
    mode: int = 0o644,
    xattrs: tuple[tuple[str, str], ...] = (),
) -> ChangesetMember[bytes]:
    return ChangesetMember(
        ordinal=ordinal,
        path=path,
        kind=kind,
        size=len(payload) if size is None and payload is not None else (size or 0),
        mode=mode,
        uid=1,
        gid=2,
        mtime=ordinal,
        link_target=target,
        device_major=0,
        device_minor=0,
        xattrs=xattrs,
        payload=payload,
    )


def _file(ordinal: int, path: str, payload: bytes) -> ChangesetMember[bytes]:
    return _member(ordinal, path, EntryKind.FILE, payload=payload)


def _marker(ordinal: int, path: str) -> ChangesetMember[bytes]:
    return _member(ordinal, path, EntryKind.FILE, payload=b"")


def _ordered(*members: ChangesetMember[bytes]) -> tuple[ChangesetMember[bytes], ...]:
    return tuple(replace(member, ordinal=ordinal) for ordinal, member in enumerate(members))


def test_last_occurrence_wins_and_non_directory_replacement_purges_descendants() -> None:
    actual = normalize_changeset(
        (
            _file(0, "tree/old", b"old"),
            _file(1, "tree", b"replacement"),
            _file(2, "tree/new", b"new"),
        )
    ).by_path()

    assert "tree/old" not in actual
    assert actual["tree"].kind is EntryKind.DIRECTORY
    assert actual["tree"].synthetic
    assert actual["tree/new"].payload == b"new"


@pytest.mark.parametrize("marker_first", [True, False])
@pytest.mark.parametrize("marker_path", [".wh.d", "d/.wh..wh..opq"])
def test_markers_never_remove_same_layer_payload(marker_first: bool, marker_path: str) -> None:
    visible = _member(0, "d", EntryKind.FILE, payload=b"payload", mode=0o751, xattrs=(("user.content", "kept"),))
    marker = _marker(1, marker_path)
    members = _ordered(marker, visible) if marker_first else _ordered(visible, marker)
    actual = normalize_changeset(members).by_path()["d"]

    assert actual.kind is EntryKind.FILE
    assert actual.payload == b"payload"
    assert (actual.size, actual.mode, actual.uid, actual.gid, actual.mtime) == (7, 0o751, 1, 2, 0)
    assert actual.xattrs == (("user.content", "kept"),)
    assert not actual.opaque


@pytest.mark.parametrize("marker_first", [True, False])
@pytest.mark.parametrize("explicit_directory", [True, False])
def test_deleted_directory_recreation_hides_lower_children(marker_first: bool, explicit_directory: bool) -> None:
    ordinary = (
        _member(0, "d", EntryKind.DIRECTORY, mode=0o710, xattrs=(("user.directory", "kept"),)),
        _file(1, "d/new", b"new"),
    )
    if not explicit_directory:
        ordinary = ordinary[1:]
    marker = _marker(2, ".wh.d")
    members = _ordered(marker, *ordinary) if marker_first else _ordered(*ordinary, marker)
    actual = normalize_changeset(members).by_path()

    directory = actual["d"]
    assert directory.kind is EntryKind.DIRECTORY
    assert directory.opaque
    assert dict(directory.xattrs)["trusted.overlay.opaque"] == "y"
    assert directory.synthetic is not explicit_directory
    if explicit_directory:
        assert (directory.mode, directory.uid, directory.gid, directory.mtime) == (0o710, 1, 2, 0)
        assert dict(directory.xattrs)["user.directory"] == "kept"
    assert actual["d/new"].payload == b"new"


@pytest.mark.parametrize("marker_first", [True, False])
def test_opaque_directory_preserves_same_layer_children_and_unrelated_siblings(marker_first: bool) -> None:
    ordinary = (
        _member(0, "d", EntryKind.DIRECTORY, mode=0o711),
        _file(1, "d/one", b"one"),
        _file(2, "d/two", b"two"),
        _file(3, "sibling/kept", b"sibling"),
        _member(4, "d/symlink", EntryKind.SYMLINK, target="../sibling/kept", mode=0o777),
    )
    marker = _marker(5, "d/.wh..wh..opq")
    members = _ordered(marker, *ordinary) if marker_first else _ordered(*ordinary, marker)
    actual = normalize_changeset(members).by_path()

    assert actual["d"].opaque
    assert (actual["d"].mode, actual["d"].uid, actual["d"].gid) == (0o711, 1, 2)
    assert actual["d/one"].payload == b"one"
    assert actual["d/two"].payload == b"two"
    assert actual["sibling/kept"].payload == b"sibling"
    assert not actual["sibling"].opaque
    symlink = actual["d/symlink"]
    assert symlink.kind is EntryKind.SYMLINK
    assert (symlink.link_target, symlink.mode, symlink.uid, symlink.gid) == ("../sibling/kept", 0o777, 1, 2)


@pytest.mark.parametrize("reverse_markers", [True, False])
def test_nested_marker_effects_are_order_independent(reverse_markers: bool) -> None:
    markers = (_marker(0, ".wh.d"), _marker(1, "d/.wh.child"), _marker(2, "d/.wh..wh..opq"))
    if reverse_markers:
        markers = markers[::-1]
    actual = normalize_changeset(_ordered(*markers, _file(3, "d/child/new", b"new"))).by_path()

    assert actual["d"].kind is EntryKind.DIRECTORY
    assert actual["d"].opaque
    assert actual["d/child"].kind is EntryKind.DIRECTORY
    assert actual["d/child"].opaque
    assert actual["d/child/new"].payload == b"new"


@pytest.mark.parametrize("with_whiteout", [True, False])
@pytest.mark.parametrize("explicit_directory", [True, False])
@pytest.mark.parametrize("intermediate_kind", [EntryKind.FILE, EntryKind.SYMLINK, EntryKind.HARDLINK])
def test_directory_opacity_survives_intermediate_type_churn(
    with_whiteout: bool, explicit_directory: bool, intermediate_kind: EntryKind
) -> None:
    ordinary = [
        _file(0, "source", b"source"),
        _member(1, "d", EntryKind.DIRECTORY, mode=0o700),
        _file(2, "d/removed", b"removed"),
        _member(
            3,
            "d",
            intermediate_kind,
            payload=b"temporary" if intermediate_kind is EntryKind.FILE else None,
            target="source" if intermediate_kind is not EntryKind.FILE else "",
        ),
    ]
    if explicit_directory:
        ordinary.append(_member(4, "d", EntryKind.DIRECTORY, mode=0o750))
    ordinary.append(_file(5, "d/new", b"new"))
    if with_whiteout:
        ordinary.append(_marker(6, ".wh.d"))
    actual = normalize_changeset(_ordered(*ordinary)).by_path()

    assert actual["d"].kind is EntryKind.DIRECTORY
    assert actual["d"].opaque
    assert dict(actual["d"].xattrs)["trusted.overlay.opaque"] == "y"
    assert "d/removed" not in actual
    assert actual["d/new"].payload == b"new"
    assert actual["source"].payload == b"source"
    if explicit_directory:
        assert (actual["d"].mode, actual["d"].uid, actual["d"].gid) == (0o750, 1, 2)


def test_first_visible_child_supplies_metadata_for_an_opaque_placeholder() -> None:
    actual = normalize_changeset((_marker(0, "d/.wh..wh..opq"), _file(1, "d/child", b"child"))).by_path()["d"]

    assert actual.synthetic
    assert actual.opaque
    assert actual.mtime == 1


def test_reserved_whiteout_target_is_rejected_by_the_shared_reducer() -> None:
    with pytest.raises(ChangesetValidationError):
        normalize_changeset((_marker(0, ".wh..wh.foo"),))


def test_opaque_marker_is_revalidated_by_the_shared_reducer() -> None:
    invalid = _member(0, "d/.wh..wh..opq", EntryKind.DIRECTORY)

    with pytest.raises(ChangesetValidationError):
        normalize_changeset((invalid,))


def test_backward_hardlink_stays_bound_to_original_payload_after_target_churn() -> None:
    actual = normalize_changeset(
        (
            _file(0, "target", b"first"),
            _member(1, "link", EntryKind.HARDLINK, target="target"),
            _file(2, "target", b"second"),
        )
    ).by_path()

    assert actual["link"].kind is EntryKind.FILE
    assert actual["link"].payload == b"first"
    assert actual["target"].payload == b"second"


def test_forward_hardlink_binds_to_first_later_regular_occurrence() -> None:
    actual = normalize_changeset(
        (
            _member(0, "link", EntryKind.HARDLINK, target="target"),
            _file(1, "target", b"first"),
            _file(2, "target", b"second"),
        )
    ).by_path()

    assert actual["link"].kind is EntryKind.FILE
    assert actual["link"].payload == b"first"
    assert actual["target"].payload == b"second"


def test_hardlinks_keep_old_payload_and_dependency_order_after_target_replacement() -> None:
    normalized = normalize_changeset(
        (
            _file(0, "z-target", b"payload"),
            _member(1, "b-link", EntryKind.HARDLINK, target="z-target"),
            _member(2, "a-link", EntryKind.HARDLINK, target="z-target"),
            _file(3, "z-target", b"replacement"),
        )
    )
    actual = normalized.by_path()

    assert actual["a-link"].kind is EntryKind.FILE
    assert actual["a-link"].payload == b"payload"
    assert actual["b-link"].kind is EntryKind.HARDLINK
    assert actual["b-link"].link_target == "a-link"
    assert actual["z-target"].payload == b"replacement"
    assert [entry.path for entry in normalized.entries].index("a-link") < [
        entry.path for entry in normalized.entries
    ].index("b-link")


def test_non_carrier_hardlink_preserves_its_reviewed_xattrs() -> None:
    actual = normalize_changeset(
        (
            _file(0, "target", b"payload"),
            _member(
                1,
                "link",
                EntryKind.HARDLINK,
                target="target",
                xattrs=(("user.link", "value"),),
            ),
        )
    ).by_path()["link"]

    assert actual.kind is EntryKind.HARDLINK
    assert actual.xattrs == (("user.link", "value"),)


@pytest.mark.parametrize(
    "members",
    [
        (_member(0, "link", EntryKind.HARDLINK, target="missing"),),
        (
            _member(0, "first", EntryKind.HARDLINK, target="second"),
            _member(1, "second", EntryKind.HARDLINK, target="first"),
        ),
        (_member(0, "self", EntryKind.HARDLINK, target="self"),),
    ],
)
def test_invalid_hardlinks_fail_closed(members: tuple[ChangesetMember[bytes], ...]) -> None:
    with pytest.raises(ChangesetValidationError):
        normalize_changeset(members)


def test_normalization_is_deterministic() -> None:
    members = (
        _file(0, "z", b"z"),
        _file(1, "a/one", b"one"),
        _marker(2, "a/.wh.one"),
        _file(3, "a/two", b"two"),
    )

    assert normalize_changeset(members) == normalize_changeset(members)


def test_synthetic_ancestors_obey_normalized_entry_budget() -> None:
    members = (_file(0, "a/b/c/payload", b"actual"),)
    actual = normalize_changeset(members, max_entries=5).by_path()
    assert set(actual) == {".", "a", "a/b", "a/b/c", "a/b/c/payload"}
    assert actual["a/b/c/payload"].payload == b"actual"
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_entries=4)
    assert rejected.value.code == "oci-changeset-entry-limit"


def test_normalization_metadata_budget_includes_replacement_work() -> None:
    members = tuple(_file(ordinal, "same", str(ordinal).encode()) for ordinal in range(300))
    actual = normalize_changeset(members, max_metadata_bytes=200_000).by_path()
    assert actual["same"].payload == b"299"
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_metadata_bytes=64 * 1024)
    assert rejected.value.code == "oci-changeset-metadata-limit"


@pytest.mark.parametrize("limits", [{"max_entries": 0}, {"max_entries": True}, {"max_metadata_bytes": -1}])
def test_normalization_rejects_invalid_resource_limits(limits: dict[str, int]) -> None:
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset((_file(0, "payload", b"actual"),), **limits)
    assert rejected.value.code == "oci-changeset-limit"


@pytest.mark.parametrize("marker_first", [True, False])
@pytest.mark.parametrize("forward", [True, False])
def test_markers_preserve_hardlink_payload_binding_and_metadata(marker_first: bool, forward: bool) -> None:
    target = _member(0, "d/target", EntryKind.FILE, payload=b"first", mode=0o640, xattrs=(("user.payload", "kept"),))
    link = _member(1, "d/link", EntryKind.HARDLINK, target="d/target", mode=0o640)
    ordinary = (link, target) if forward else (target, link)
    markers = (_marker(2, "d/.wh.target"), _marker(3, "d/.wh..wh..opq"))
    members = _ordered(*markers, *ordinary) if marker_first else _ordered(*ordinary, *markers)
    actual = normalize_changeset(members).by_path()

    assert actual["d"].opaque
    assert actual["d/target"].kind is EntryKind.FILE
    assert actual["d/target"].payload == b"first"
    assert (actual["d/target"].mode, actual["d/target"].uid, actual["d/target"].gid) == (0o640, 1, 2)
    assert actual["d/target"].xattrs == (("user.payload", "kept"),)
    assert actual["d/link"].kind is EntryKind.HARDLINK
    assert actual["d/link"].link_target == "d/target"
    assert (actual["d/link"].mode, actual["d/link"].uid, actual["d/link"].gid) == (0o640, 1, 2)


@pytest.mark.parametrize("path", ["d/.wh.child", "d/.wh..wh..opq"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"kind": EntryKind.DIRECTORY},
        {"kind": EntryKind.FILE, "payload": b"not-empty"},
        {"kind": EntryKind.FILE, "payload": b"", "xattrs": (("user.invalid", "value"),)},
    ],
)
def test_all_markers_are_validated_before_any_effect_is_applied(path: str, metadata: dict[str, object]) -> None:
    invalid = _member(1, path, **metadata)
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset((_marker(0, "deep/parent/.wh.child"), invalid), max_entries=1)
    assert rejected.value.code == "oci-whiteout-marker"


@pytest.mark.parametrize("marker_path", ["a/b/c/.wh.child", "a/b/c/.wh..wh..opq"])
def test_deep_marker_effects_include_implied_parents_in_entry_limit(marker_path: str) -> None:
    members = (_marker(0, marker_path),)
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_entries=3)
    assert rejected.value.code == "oci-changeset-entry-limit"


def test_wide_marker_effect_index_is_bounded_before_reduction() -> None:
    members = tuple(_marker(index, f".wh.deleted-{index}") for index in range(64))
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_entries=32)
    assert rejected.value.code == "oci-changeset-entry-limit"


def test_duplicate_marker_prepass_work_is_metadata_bounded() -> None:
    members = tuple(_marker(index, ".wh.same") for index in range(300))
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_metadata_bytes=64 * 1024)
    assert rejected.value.code == "oci-changeset-metadata-limit"


def test_type_churn_barriers_remain_bounded_after_paths_are_purged() -> None:
    members = _ordered(
        _file(0, "d/first", b"first"),
        _file(1, "d", b"replacement"),
        _file(2, "d/second", b"second"),
        _file(3, "d", b"replacement"),
        _file(4, "d/third", b"third"),
    )
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_entries=3)
    assert rejected.value.code == "oci-changeset-entry-limit"


def test_marker_prepass_obeys_checkpoint_cancellation() -> None:
    calls = 0

    def checkpoint() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise TimeoutError("normalization deadline")

    members = tuple(_marker(index, f".wh.deleted-{index}") for index in range(64))
    with pytest.raises(TimeoutError, match="normalization deadline"):
        normalize_changeset(members, checkpoint=checkpoint)
    assert calls == 5


@pytest.mark.parametrize("marker_first", [True, False])
def test_root_opacity_preserves_explicit_root_metadata_and_payload(marker_first: bool) -> None:
    ordinary = (
        _member(0, ".", EntryKind.DIRECTORY, mode=0o750, xattrs=(("user.root", "kept"),)),
        _file(1, "payload", b"root-child"),
    )
    marker = _marker(2, ".wh..wh..opq")
    members = _ordered(marker, *ordinary) if marker_first else _ordered(*ordinary, marker)
    actual = normalize_changeset(members).by_path()

    root = actual["."]
    assert root.opaque
    assert (root.mode, root.uid, root.gid, root.mtime) == (0o750, 1, 2, 0)
    assert dict(root.xattrs) == {"user.root": "kept", "trusted.overlay.opaque": "y"}
    assert actual["payload"].payload == b"root-child"


def test_bounded_deep_deleted_directory_recreation_keeps_payload() -> None:
    parent = "/".join(f"level-{index}" for index in range(64))
    members = (
        _file(0, f"{parent}/deleted/new", b"deep"),
        _marker(1, f"{parent}/.wh.deleted"),
    )
    actual = normalize_changeset(members, max_entries=67, max_metadata_bytes=100_000).by_path()

    assert actual[f"{parent}/deleted"].opaque
    assert actual[f"{parent}/deleted/new"].payload == b"deep"
    with pytest.raises(ChangesetValidationError) as rejected:
        normalize_changeset(members, max_entries=66, max_metadata_bytes=100_000)
    assert rejected.value.code == "oci-changeset-entry-limit"


def test_bounded_wide_same_layer_payloads_survive_late_whiteouts() -> None:
    width = 32
    ordinary = tuple(_file(index, f"file-{index}", str(index).encode()) for index in range(width))
    markers = tuple(_marker(index, f".wh.file-{index}") for index in range(width))
    actual = normalize_changeset(
        _ordered(*ordinary, *markers), max_entries=width + 1, max_metadata_bytes=64 * 1024
    ).by_path()

    assert set(actual) == {".", *(f"file-{index}" for index in range(width))}
    for index in range(width):
        assert actual[f"file-{index}"].kind is EntryKind.FILE
        assert actual[f"file-{index}"].payload == str(index).encode()


@pytest.mark.parametrize("reverse_markers", [True, False])
def test_marker_only_deletion_dominates_same_path_opacity_without_resurrection(reverse_markers: bool) -> None:
    markers = (_marker(0, "d/.wh..wh..opq"), _marker(1, ".wh.d"))
    if reverse_markers:
        markers = markers[::-1]
    actual = normalize_changeset(_ordered(*markers)).by_path()

    assert set(actual) == {".", "d"}
    assert actual["d"].kind is EntryKind.WHITEOUT
    assert not actual["d"].opaque
    assert "trusted.overlay.opaque" not in dict(actual["d"].xattrs)


@pytest.mark.parametrize("reverse_markers", [True, False])
@pytest.mark.parametrize("descendant_marker", ["d/.wh..wh..opq", "d/sub/.wh..wh..opq", "d/sub/.wh.child"])
def test_marker_only_descendants_do_not_resurrect_a_deleted_ancestor(
    reverse_markers: bool, descendant_marker: str
) -> None:
    markers = (
        _marker(0, ".wh.d"),
        _marker(1, descendant_marker),
        _marker(2, "d-other/.wh.child"),
        _marker(3, "d0/.wh..wh..opq"),
    )
    if reverse_markers:
        markers = markers[::-1]
    actual = normalize_changeset(_ordered(*markers)).by_path()

    assert actual["d"].kind is EntryKind.WHITEOUT
    assert not any(path.startswith("d/") for path in actual)
    assert actual["d-other"].kind is EntryKind.DIRECTORY
    assert actual["d-other/child"].kind is EntryKind.WHITEOUT
    assert actual["d0"].kind is EntryKind.DIRECTORY
    assert actual["d0"].opaque


@pytest.mark.parametrize("reverse_markers", [True, False])
@pytest.mark.parametrize("markers_first", [True, False])
def test_ordinary_descendant_recreation_retains_suppressed_lower_effects(
    reverse_markers: bool, markers_first: bool
) -> None:
    markers = (
        _marker(0, ".wh.d"),
        _marker(1, "d/.wh..wh..opq"),
        _marker(2, "d/child/.wh..wh..opq"),
    )
    if reverse_markers:
        markers = markers[::-1]
    ordinary = (
        _member(3, "d/child", EntryKind.DIRECTORY, mode=0o710),
        _file(4, "d/child/new", b"recreated"),
    )
    members = _ordered(*markers, *ordinary) if markers_first else _ordered(*ordinary, *markers)
    actual = normalize_changeset(members).by_path()

    assert actual["d"].kind is EntryKind.DIRECTORY
    assert actual["d"].opaque
    assert actual["d/child"].kind is EntryKind.DIRECTORY
    assert actual["d/child"].opaque
    assert (actual["d/child"].mode, actual["d/child"].uid, actual["d/child"].gid) == (0o710, 1, 2)
    assert actual["d/child/new"].payload == b"recreated"


def test_repeated_forward_hardlink_keeps_the_live_pending_binding() -> None:
    actual = normalize_changeset(
        (
            _member(0, "link", EntryKind.HARDLINK, target="target"),
            _member(1, "link", EntryKind.HARDLINK, target="target"),
            _file(2, "target", b"first"),
            _file(3, "target", b"second"),
        )
    ).by_path()
    assert actual["link"].kind is EntryKind.FILE
    assert actual["link"].payload == b"first"
    assert actual["target"].payload == b"second"


def test_omitted_directories_inherit_effective_attributes_including_root() -> None:
    fs = EffectiveFilesystem()
    fs.apply(
        normalize_changeset(
            (
                replace(_member(0, ".", EntryKind.DIRECTORY, mode=0o710), uid=123, gid=456, mtime=1700),
                replace(
                    _member(1, "d", EntryKind.DIRECTORY, mode=0o2710),
                    uid=321,
                    gid=654,
                    mtime=1701,
                    xattrs=(("user.directory", "retained"),),
                ),
            )
        )
    )
    actual = fs.apply(normalize_changeset((_file(0, "d/new", b"new"),))).by_path()
    assert (actual["."].mode, actual["."].uid, actual["."].gid, actual["."].mtime) == (0o710, 123, 456, 1700)
    assert (actual["d"].mode, actual["d"].uid, actual["d"].gid, actual["d"].mtime) == (0o2710, 321, 654, 1701)
    assert actual["d"].xattrs == (("user.directory", "retained"),)
    explicit = fs.apply(
        normalize_changeset(
            (
                _member(0, "d", EntryKind.DIRECTORY, mode=0o750),
                _file(1, "d/next", b"next"),
            )
        )
    ).by_path()
    assert (explicit["d"].mode, explicit["d"].uid, explicit["d"].gid) == (0o750, 1, 2)


@pytest.mark.parametrize("barrier", ["delete", "type", "opaque"])
def test_directory_inheritance_obeys_lower_deletion_and_type_barriers(barrier: str) -> None:
    fs = EffectiveFilesystem()
    fs.apply(
        normalize_changeset(
            (
                _member(0, "d", EntryKind.DIRECTORY, mode=0o710),
                _member(1, "d/nested", EntryKind.DIRECTORY, mode=0o700),
                _file(2, "d/nested/old", b"old"),
            )
        )
    )
    effect = {
        "delete": _marker(0, ".wh.d"),
        "type": _file(0, "d", b"temporary"),
        "opaque": _marker(0, "d/.wh..wh..opq"),
    }[barrier]
    actual = fs.apply(normalize_changeset(_ordered(effect, _file(1, "d/nested/new", b"new")))).by_path()
    assert (actual["d/nested"].uid, actual["d/nested"].gid, actual["d/nested"].mode) == (0, 0, 0o755)
    assert "d/nested/old" not in fs.entries
    if barrier == "opaque":
        assert (actual["d"].uid, actual["d"].gid, actual["d"].mode) == (1, 2, 0o710)
    else:
        assert (actual["d"].uid, actual["d"].gid, actual["d"].mode) == (0, 0, 0o755)


def test_prior_opacity_is_not_inherited_by_a_later_implicit_directory() -> None:
    fs = EffectiveFilesystem()
    fs.apply(
        normalize_changeset(
            (
                _member(0, "d", EntryKind.DIRECTORY, mode=0o710, xattrs=(("user.overlay.application", "retained"),)),
                _file(1, "d/old", b"old"),
            )
        )
    )
    fs.apply(
        normalize_changeset(
            (
                _marker(0, "d/.wh..wh..opq"),
                _file(1, "d/current", b"current"),
            )
        )
    )
    latest = fs.apply(normalize_changeset((_file(0, "d/new", b"new"),))).by_path()
    assert not latest["d"].opaque
    assert latest["d"].xattrs == (("user.overlay.application", "retained"),)
    assert (latest["d"].mode, latest["d"].uid, latest["d"].gid) == (0o710, 1, 2)
    assert "d/old" not in fs.entries
    assert fs.entries["d/current"].payload == b"current"
    assert fs.entries["d/new"].payload == b"new"


def test_effective_prefix_bounds_replacement_work_and_observes_cancellation() -> None:
    fs = EffectiveFilesystem(max_entries=2)
    fs.apply(normalize_changeset((_file(0, "one", b"one"),)))
    with pytest.raises(ChangesetValidationError, match="entry-limit"):
        fs.apply(normalize_changeset((_file(0, "two", b"two"),)))
    fs = EffectiveFilesystem(max_metadata_bytes=1100)
    fs.apply(normalize_changeset((_file(0, "same", b"one"),)))
    fs.apply(normalize_changeset((_file(0, "same", b"two"),)))
    with pytest.raises(ChangesetValidationError, match="metadata-limit"):
        fs.apply(normalize_changeset((_file(0, "same", b"three"),)))

    def cancel() -> None:
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        EffectiveFilesystem(checkpoint=cancel).apply(normalize_changeset((_file(0, "one", b"one"),)))
