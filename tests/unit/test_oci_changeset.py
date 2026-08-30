from __future__ import annotations

import pytest

from palimpsest_local.oci_changeset import (
    ChangesetMember,
    ChangesetValidationError,
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


@pytest.mark.parametrize(
    ("tail", "expected_kind", "expected_opaque"),
    [
        (_marker(1, ".wh.d"), EntryKind.WHITEOUT, False),
        (_file(1, "d", b"file"), EntryKind.FILE, False),
        (_member(1, "d", EntryKind.DIRECTORY, mode=0o700), EntryKind.DIRECTORY, True),
    ],
)
def test_opaque_state_obeys_later_occurrences(
    tail: ChangesetMember[bytes], expected_kind: EntryKind, expected_opaque: bool
) -> None:
    actual = normalize_changeset((_marker(0, "d/.wh..wh..opq"), tail)).by_path()["d"]

    assert actual.kind is expected_kind
    assert actual.opaque is expected_opaque


def test_later_opaque_marker_replaces_a_non_directory_with_an_opaque_directory() -> None:
    actual = normalize_changeset((_file(0, "d", b"file"), _marker(1, "d/.wh..wh..opq"))).by_path()["d"]

    assert actual.kind is EntryKind.DIRECTORY
    assert actual.opaque
    assert dict(actual.xattrs)["trusted.overlay.opaque"] == "y"


def test_first_later_child_supplies_metadata_for_an_opaque_placeholder() -> None:
    actual = normalize_changeset((_marker(0, "d/.wh..wh..opq"), _file(1, "d/child", b"child"))).by_path()["d"]

    assert actual.synthetic
    assert actual.opaque
    assert actual.mtime == 1


def test_reserved_whiteout_target_is_rejected_by_the_shared_reducer() -> None:
    with pytest.raises(ChangesetValidationError, match="Invalid OCI whiteout target"):
        normalize_changeset((_marker(0, ".wh..wh.foo"),))


def test_opaque_marker_is_revalidated_by_the_shared_reducer() -> None:
    invalid = _member(0, "d/.wh..wh..opq", EntryKind.DIRECTORY)

    with pytest.raises(ChangesetValidationError, match="empty regular file"):
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


def test_surviving_hardlinks_use_a_deterministic_carrier_and_dependency_order() -> None:
    normalized = normalize_changeset(
        (
            _file(0, "z-target", b"payload"),
            _member(1, "b-link", EntryKind.HARDLINK, target="z-target"),
            _member(2, "a-link", EntryKind.HARDLINK, target="z-target"),
            _marker(3, ".wh.z-target"),
        )
    )
    actual = normalized.by_path()

    assert actual["a-link"].kind is EntryKind.FILE
    assert actual["a-link"].payload == b"payload"
    assert actual["b-link"].kind is EntryKind.HARDLINK
    assert actual["b-link"].link_target == "a-link"
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
    with pytest.raises(ChangesetValidationError, match="same-layer regular file|itself"):
        normalize_changeset(members)


def test_normalization_is_deterministic() -> None:
    members = (
        _file(0, "z", b"z"),
        _file(1, "a/one", b"one"),
        _marker(2, "a/.wh.one"),
        _file(3, "a/two", b"two"),
    )

    assert normalize_changeset(members) == normalize_changeset(members)
