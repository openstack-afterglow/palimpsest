"""Archive-order OCI changeset normalization shared by proof and converter paths."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import StrEnum

from .errors import ArtifactValidationError


class ChangesetValidationError(ArtifactValidationError):
    """A validated physical member sequence has contradictory semantics."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class EntryKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    CHAR = "char"
    BLOCK = "block"
    FIFO = "fifo"
    WHITEOUT = "whiteout"


@dataclass(frozen=True, slots=True)
class ChangesetMember[PayloadT]:
    """One validated physical tar member in exact archive order."""

    ordinal: int
    path: str
    kind: EntryKind
    size: int
    mode: int
    uid: int
    gid: int
    mtime: int
    link_target: str
    device_major: int
    device_minor: int
    xattrs: tuple[tuple[str, str], ...]
    payload: PayloadT | None

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ChangesetValidationError("oci-changeset-member", "member ordinal is invalid")
        if not isinstance(self.path, str) or not self.path:
            raise ChangesetValidationError("oci-changeset-member", "member path is invalid")
        if not isinstance(self.kind, EntryKind):
            raise ChangesetValidationError("oci-changeset-member", "member kind is invalid")
        integer_values = (self.size, self.mode, self.uid, self.gid, self.mtime, self.device_major, self.device_minor)
        if any(type(value) is not int or value < 0 for value in integer_values):
            raise ChangesetValidationError("oci-changeset-member", "member numeric metadata is invalid")
        if not isinstance(self.link_target, str):
            raise ChangesetValidationError("oci-changeset-member", "member link target is invalid")
        if not isinstance(self.xattrs, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            for item in self.xattrs
        ):
            raise ChangesetValidationError("oci-changeset-member", "member xattrs are invalid")
        if self.kind is EntryKind.FILE and self.payload is None:
            raise ChangesetValidationError("oci-changeset-payload", "regular member payload binding is missing")
        if self.kind is not EntryKind.FILE and self.payload is not None:
            raise ChangesetValidationError("oci-changeset-payload", "non-regular member owns a payload binding")


@dataclass(frozen=True, slots=True)
class NormalizedEntry[PayloadT]:
    """One final path entry after ordered replacement and link resolution."""

    path: str
    kind: EntryKind
    size: int
    mode: int
    uid: int
    gid: int
    mtime: int
    link_target: str
    device_major: int
    device_minor: int
    xattrs: tuple[tuple[str, str], ...]
    payload: PayloadT | None
    opaque: bool
    synthetic: bool
    source_ordinal: int | None


@dataclass(frozen=True, slots=True)
class NormalizedChangeset[PayloadT]:
    """Immutable deterministic emission-order changeset table."""

    entries: tuple[NormalizedEntry[PayloadT], ...]

    def by_path(self) -> dict[str, NormalizedEntry[PayloadT]]:
        return {entry.path: entry for entry in self.entries}


@dataclass(slots=True)
class _Node[PayloadT]:
    member: ChangesetMember[PayloadT]
    kind: EntryKind
    group_id: int | None = None
    pending_target: str | None = None
    opaque: bool = False
    opaque_placeholder: bool = False
    synthetic: bool = False


def _parent(path: str) -> str | None:
    if path == ".":
        return None
    value = os.path.dirname(path)
    return value or "."


def _synthetic_directory[PayloadT](path: str, mtime: int, ordinal: int) -> ChangesetMember[PayloadT]:
    return ChangesetMember(
        ordinal=ordinal,
        path=path,
        kind=EntryKind.DIRECTORY,
        size=0,
        mode=0o755,
        uid=0,
        gid=0,
        mtime=mtime,
        link_target="",
        device_major=0,
        device_minor=0,
        xattrs=(),
        payload=None,
    )


def normalize_changeset[PayloadT](
    members: tuple[ChangesetMember[PayloadT], ...],
) -> NormalizedChangeset[PayloadT]:
    """Reduce exact physical occurrences into deterministic final filesystem state.

    Backward hardlinks bind to the regular payload occurrence visible at their
    own archive position.  Forward hardlinks bind to the first later regular
    occurrence at the requested target path.  Later target-path churn never
    retargets an already-bound link.
    """
    if not isinstance(members, tuple):
        raise ChangesetValidationError("oci-changeset-input", "members must be an immutable tuple")
    if any(not isinstance(member, ChangesetMember) for member in members):
        raise ChangesetValidationError("oci-changeset-input", "members contain an invalid value")
    if any(member.ordinal != index for index, member in enumerate(members)):
        raise ChangesetValidationError("oci-changeset-input", "member ordinals are not contiguous archive order")

    table: dict[str, _Node[PayloadT]] = {}
    children: dict[str, set[str]] = {}
    pending_by_target: dict[str, set[str]] = {}
    groups: dict[int, ChangesetMember[PayloadT]] = {}

    def register(path: str) -> None:
        parent = _parent(path)
        if parent is not None:
            children.setdefault(parent, set()).add(path)

    def cancel_pending(path: str, node: _Node[PayloadT]) -> None:
        if node.pending_target is None:
            return
        waiting = pending_by_target.get(node.pending_target)
        if waiting is not None:
            waiting.discard(path)
            if not waiting:
                pending_by_target.pop(node.pending_target, None)

    def remove_exact(path: str, *, keep_children: bool) -> None:
        prior = table.pop(path, None)
        if prior is not None:
            cancel_pending(path, prior)
        if not keep_children:
            children.pop(path, None)
            parent = _parent(path)
            if parent is not None:
                children.get(parent, set()).discard(path)

    def purge_subtree(path: str, *, include_root: bool) -> None:
        pending = list(children.get(path, ()))
        while pending:
            descendant = pending.pop()
            pending.extend(children.get(descendant, ()))
            remove_exact(descendant, keep_children=False)
        children.pop(path, None)
        if include_root:
            remove_exact(path, keep_children=False)

    def set_node(path: str, node: _Node[PayloadT], *, preserve_children: bool) -> None:
        remove_exact(path, keep_children=preserve_children)
        table[path] = node
        register(path)

    def ensure_parents(
        path: str,
        member: ChangesetMember[PayloadT],
        *,
        refresh_opaque_placeholder: bool = True,
    ) -> None:
        parent = _parent(path)
        chain: list[str] = []
        while parent not in {None, "."}:
            chain.append(parent)
            parent = _parent(parent)
        for directory in reversed(chain):
            current = table.get(directory)
            if current is not None and current.kind is EntryKind.DIRECTORY:
                if current.opaque_placeholder and refresh_opaque_placeholder:
                    current.member = _synthetic_directory(directory, member.mtime, member.ordinal)
                    current.opaque_placeholder = False
                continue
            purge_subtree(directory, include_root=True)
            synthetic = _synthetic_directory(directory, member.mtime, member.ordinal)
            set_node(
                directory,
                _Node(member=synthetic, kind=EntryKind.DIRECTORY, synthetic=True),
                preserve_children=True,
            )

    def resolve_forward(target_path: str, group_id: int) -> None:
        for link_path in tuple(sorted(pending_by_target.pop(target_path, set()))):
            node = table.get(link_path)
            if node is None or node.pending_target != target_path:
                continue
            node.pending_target = None
            node.group_id = group_id

    for member in members:
        path = member.path
        basename = os.path.basename(path)
        parent = os.path.dirname(path) or "."

        if basename == ".wh..wh..opq":
            if member.kind is not EntryKind.FILE or member.size != 0 or member.xattrs:
                raise ChangesetValidationError(
                    "oci-whiteout-marker",
                    "OCI opaque marker must be an empty regular file without control metadata",
                )
            opaque_path = parent
            if opaque_path != ".":
                ensure_parents(opaque_path, member, refresh_opaque_placeholder=False)
            current = table.get(opaque_path)
            if current is None or current.kind is not EntryKind.DIRECTORY:
                purge_subtree(opaque_path, include_root=True)
                synthetic = _synthetic_directory(opaque_path, 0, member.ordinal)
                current = _Node(
                    member=synthetic,
                    kind=EntryKind.DIRECTORY,
                    opaque_placeholder=opaque_path != ".",
                    synthetic=True,
                )
                set_node(opaque_path, current, preserve_children=True)
            current.opaque = True
            continue

        if basename.startswith(".wh."):
            target_name = basename.removeprefix(".wh.")
            if not target_name or target_name in {".", ".."} or target_name.startswith(".wh."):
                raise ChangesetValidationError("oci-whiteout-target", "Invalid OCI whiteout target")
            if member.kind is not EntryKind.FILE or member.size != 0 or member.xattrs:
                raise ChangesetValidationError(
                    "oci-whiteout-marker",
                    "OCI whiteout marker must be an empty regular file without control metadata",
                )
            target_path = os.path.join(parent, target_name) if parent != "." else target_name
            ensure_parents(target_path, member, refresh_opaque_placeholder=False)
            purge_subtree(target_path, include_root=True)
            whiteout_member = replace(
                member,
                path=target_path,
                kind=EntryKind.WHITEOUT,
                mode=0o600,
                uid=0,
                gid=0,
                link_target="",
                device_major=0,
                device_minor=0,
                xattrs=(),
                payload=None,
            )
            set_node(
                target_path,
                _Node(member=whiteout_member, kind=EntryKind.WHITEOUT),
                preserve_children=False,
            )
            continue

        if path != ".":
            ensure_parents(path, member)

        if member.kind is not EntryKind.DIRECTORY:
            purge_subtree(path, include_root=False)

        prior = table.get(path)
        preserve_opaque = (
            member.kind is EntryKind.DIRECTORY
            and prior is not None
            and prior.kind is EntryKind.DIRECTORY
            and prior.opaque
        )

        if member.kind is EntryKind.FILE:
            group_id = member.ordinal
            groups[group_id] = member
            node = _Node(member=member, kind=EntryKind.FILE, group_id=group_id)
            set_node(path, node, preserve_children=False)
            resolve_forward(path, group_id)
        elif member.kind is EntryKind.HARDLINK:
            if member.link_target == path:
                raise ChangesetValidationError("oci-hardlink-self", "OCI hardlink cannot target itself")
            target = table.get(member.link_target)
            if target is not None and target.kind is EntryKind.FILE:
                node = _Node(member=member, kind=EntryKind.HARDLINK, group_id=target.group_id)
            elif target is not None and target.kind is EntryKind.HARDLINK:
                raise ChangesetValidationError(
                    "oci-hardlink-chain",
                    "OCI hardlink target must be a same-layer regular file",
                )
            else:
                node = _Node(member=member, kind=EntryKind.HARDLINK, pending_target=member.link_target)
                pending_by_target.setdefault(member.link_target, set()).add(path)
            set_node(path, node, preserve_children=False)
        else:
            node = _Node(
                member=member,
                kind=member.kind,
                opaque=preserve_opaque,
            )
            set_node(path, node, preserve_children=member.kind is EntryKind.DIRECTORY)

    if "." not in table:
        root = _synthetic_directory(".", 0, len(members))
        table["."] = _Node(member=root, kind=EntryKind.DIRECTORY, synthetic=True)

    unresolved = sorted(path for path, node in table.items() if node.pending_target is not None)
    if unresolved:
        raise ChangesetValidationError(
            "oci-hardlink-target",
            "OCI hardlink target must be a same-layer regular file",
        )

    group_paths: dict[int, list[str]] = {}
    for path, node in table.items():
        if node.group_id is not None:
            group_paths.setdefault(node.group_id, []).append(path)

    carriers: dict[int, str] = {}
    for group_id, paths in group_paths.items():
        regular_paths = sorted(path for path in paths if table[path].kind is EntryKind.FILE)
        carriers[group_id] = regular_paths[0] if regular_paths else sorted(paths)[0]

    normalized: dict[str, NormalizedEntry[PayloadT]] = {}
    for path, node in table.items():
        member = node.member
        xattrs = dict(member.xattrs)
        if node.opaque:
            if node.kind is not EntryKind.DIRECTORY:
                raise ChangesetValidationError("oci-opaque-state", "opaque state requires a directory")
            xattrs["trusted.overlay.opaque"] = "y"

        if node.group_id is not None:
            source = groups.get(node.group_id)
            if source is None:
                raise ChangesetValidationError("oci-hardlink-group", "hardlink payload group is missing")
            carrier = carriers[node.group_id]
            if path == carrier:
                normalized[path] = NormalizedEntry(
                    path=path,
                    kind=EntryKind.FILE,
                    size=source.size,
                    mode=source.mode,
                    uid=source.uid,
                    gid=source.gid,
                    mtime=source.mtime,
                    link_target="",
                    device_major=0,
                    device_minor=0,
                    xattrs=source.xattrs,
                    payload=source.payload,
                    opaque=False,
                    synthetic=path != source.path,
                    source_ordinal=source.ordinal,
                )
            else:
                normalized[path] = NormalizedEntry(
                    path=path,
                    kind=EntryKind.HARDLINK,
                    size=0,
                    mode=member.mode,
                    uid=member.uid,
                    gid=member.gid,
                    mtime=member.mtime,
                    link_target=carrier,
                    device_major=0,
                    device_minor=0,
                    xattrs=member.xattrs,
                    payload=None,
                    opaque=False,
                    synthetic=False,
                    source_ordinal=member.ordinal,
                )
            continue

        normalized[path] = NormalizedEntry(
            path=path,
            kind=node.kind,
            size=member.size,
            mode=member.mode,
            uid=member.uid,
            gid=member.gid,
            mtime=member.mtime,
            link_target=member.link_target,
            device_major=member.device_major,
            device_minor=member.device_minor,
            xattrs=tuple(sorted(xattrs.items())),
            payload=None,
            opaque=node.opaque,
            synthetic=node.synthetic,
            source_ordinal=member.ordinal if member.ordinal < len(members) else None,
        )

    ordered_paths: list[str] = []
    emitted: set[str] = set()
    remaining = sorted(normalized)
    while remaining:
        deferred: list[str] = []
        for path in remaining:
            entry = normalized[path]
            if entry.kind is EntryKind.HARDLINK and entry.link_target not in emitted:
                deferred.append(path)
                continue
            ordered_paths.append(path)
            emitted.add(path)
        if len(deferred) == len(remaining):
            raise ChangesetValidationError("oci-hardlink-order", "hardlink emission order cannot be resolved")
        remaining = deferred

    return NormalizedChangeset(entries=tuple(normalized[path] for path in ordered_paths))


__all__ = [
    "ChangesetMember",
    "ChangesetValidationError",
    "EntryKind",
    "NormalizedChangeset",
    "NormalizedEntry",
    "normalize_changeset",
]
