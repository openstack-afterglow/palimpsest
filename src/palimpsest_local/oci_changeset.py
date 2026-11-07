"""Lower-only OCI marker effects and ordered visible-member normalization."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Generic, TypeVar

from .errors import ArtifactValidationError

PayloadT = TypeVar("PayloadT")

OCI_CHANGESET_NORMALIZATION_ID = "palimpsest.oci-changeset-normalization.v3"


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
class ChangesetMember(Generic[PayloadT]):
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
class NormalizedEntry(Generic[PayloadT]):
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
    inherit_directory_metadata: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedChangeset(Generic[PayloadT]):
    """Immutable deterministic emission-order changeset table."""

    entries: tuple[NormalizedEntry[PayloadT], ...]

    def by_path(self) -> dict[str, NormalizedEntry[PayloadT]]:
        return {entry.path: entry for entry in self.entries}


class EffectiveFilesystem(Generic[PayloadT]):
    """Bounded effective prefix, reduced solely from canonical changesets.

    Overlay control xattrs belong to a changeset, not to the visible directory
    metadata inherited by a later changeset.
    """

    def __init__(
        self,
        *,
        max_entries: int = 500_000,
        max_metadata_bytes: int = 64 * 1024**2,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ChangesetValidationError("oci-changeset-limit", "effective entry limit must be positive")
        if type(max_metadata_bytes) is not int or max_metadata_bytes <= 0:
            raise ChangesetValidationError("oci-changeset-limit", "effective metadata limit must be positive")
        self.entries: dict[str, NormalizedEntry[PayloadT]] = {}
        self.children: dict[str, set[str]] = {}
        self.max_entries = max_entries
        self.max_metadata_bytes = max_metadata_bytes
        self.metadata_bytes = 0
        self.checkpoint = checkpoint or (lambda: None)

    def _purge(self, path: str, *, include_root: bool = True) -> None:
        pending = list(self.children.get(path, ()))
        while pending:
            self.checkpoint()
            descendant = pending.pop()
            pending.extend(self.children.pop(descendant, ()))
            self.entries.pop(descendant, None)
        self.children.pop(path, None)
        if include_root and path != ".":
            self.entries.pop(path, None)
            parent = _parent(path)
            self.children.get(parent, set()).discard(path)

    def apply(
        self,
        changeset: NormalizedChangeset[PayloadT],
        *,
        retain_payloads: bool = True,
    ) -> NormalizedChangeset[PayloadT]:
        """Resolve omitted directory attributes and advance the effective view.

        The returned layer retains its own whiteouts/opacity and payload
        capabilities. The prefix retains neither lower-only controls nor, when
        requested, capabilities belonging to a soon-to-close staged layer.
        """
        for entry in changeset.entries:
            self.checkpoint()
            if entry.kind is EntryKind.WHITEOUT:
                self._purge(entry.path)
            elif entry.opaque:
                self._purge(entry.path, include_root=False)
        resolved: list[NormalizedEntry[PayloadT]] = []
        for entry in changeset.entries:
            self.checkpoint()
            if entry.kind is EntryKind.WHITEOUT:
                resolved.append(entry)
                continue
            prior = self.entries.get(entry.path)
            if (
                entry.kind is EntryKind.DIRECTORY
                and entry.inherit_directory_metadata
                and prior is not None
                and prior.kind is EntryKind.DIRECTORY
            ):
                attrs = dict(prior.xattrs)
                attrs.update(entry.xattrs)
                entry = replace(
                    entry,
                    mode=prior.mode,
                    uid=prior.uid,
                    gid=prior.gid,
                    mtime=prior.mtime,
                    xattrs=tuple(sorted(attrs.items())),
                )
            self.metadata_bytes += (
                256
                + len(entry.path.encode("utf-8"))
                + len(entry.link_target.encode("utf-8"))
                + sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in entry.xattrs)
            )
            if self.metadata_bytes > self.max_metadata_bytes:
                raise ChangesetValidationError(
                    "oci-changeset-metadata-limit", "effective prefix metadata exceeds its limit"
                )
            if entry.kind is not EntryKind.DIRECTORY or (prior is not None and prior.kind is not EntryKind.DIRECTORY):
                self._purge(entry.path)
            if entry.path not in self.entries and len(self.entries) >= self.max_entries:
                raise ChangesetValidationError(
                    "oci-changeset-entry-limit", "effective filesystem exceeds its entry limit"
                )
            resolved.append(entry)
            self.entries[entry.path] = replace(
                entry,
                payload=entry.payload if retain_payloads else None,
                opaque=False,
                inherit_directory_metadata=False,
                xattrs=tuple((key, value) for key, value in entry.xattrs if key != "trusted.overlay.opaque"),
            )
            parent = _parent(entry.path)
            if parent is not None:
                self.children.setdefault(parent, set()).add(entry.path)
        return NormalizedChangeset(tuple(resolved))


@dataclass(slots=True)
class _Node(Generic[PayloadT]):
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


def _synthetic_directory(path: str, mtime: int, ordinal: int) -> ChangesetMember[PayloadT]:
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


def normalize_changeset(
    members: tuple[ChangesetMember[PayloadT], ...],
    *,
    max_entries: int = 500_000,
    max_metadata_bytes: int = 64 * 1024**2,
    checkpoint: Callable[[], None] | None = None,
) -> NormalizedChangeset[PayloadT]:
    """Reduce exact physical occurrences into deterministic final filesystem state.

    Whiteout and opaque markers affect lower layers only, independent of their
    archive position. Ordinary members retain their original replacement order.

    Backward hardlinks bind to the regular payload occurrence visible at their
    own archive position.  Forward hardlinks bind to the first later regular
    occurrence at the requested target path.  Later target-path churn never
    retargets an already-bound link.
    """
    if not isinstance(members, tuple):
        raise ChangesetValidationError("oci-changeset-input", "members must be an immutable tuple")
    if type(max_entries) is not int or max_entries <= 0:
        raise ChangesetValidationError("oci-changeset-limit", "normalized entry limit must be positive")
    if type(max_metadata_bytes) is not int or max_metadata_bytes <= 0:
        raise ChangesetValidationError("oci-changeset-limit", "normalization metadata limit must be positive")
    if checkpoint is not None and not callable(checkpoint):
        raise ChangesetValidationError("oci-changeset-limit", "normalization checkpoint must be callable")

    def check_work() -> None:
        if checkpoint is not None:
            checkpoint()

    metadata_work = 0
    lower_effects: dict[str, ChangesetMember[PayloadT]] = {}

    def account_metadata(path: str, member: ChangesetMember[PayloadT]) -> None:
        nonlocal metadata_work
        try:
            amount = 256 + len(path.encode("utf-8")) + len(member.link_target.encode("utf-8"))
            amount += sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in member.xattrs)
        except UnicodeEncodeError:
            raise ChangesetValidationError("oci-changeset-metadata", "normalized metadata is not UTF-8") from None
        metadata_work += amount
        if metadata_work > max_metadata_bytes:
            raise ChangesetValidationError(
                "oci-changeset-metadata-limit", "cumulative normalized metadata exceeds its byte limit"
            )

    # Keep only one lower-layer effect per path. Deletion hides the lower path
    # itself, so it dominates opacity until an ordinary member recreates it.
    # Charge every physical occurrence: the validation prepass itself is bounded,
    # including repetitions that do not increase the final table or effect index.
    for index, member in enumerate(members):
        check_work()
        if not isinstance(member, ChangesetMember):
            raise ChangesetValidationError("oci-changeset-input", "members contain an invalid value")
        if member.ordinal != index:
            raise ChangesetValidationError("oci-changeset-input", "member ordinals are not contiguous archive order")
        account_metadata(member.path, member)
        basename = os.path.basename(member.path)
        if not basename.startswith(".wh."):
            continue
        opaque = basename == ".wh..wh..opq"
        target_name = basename.removeprefix(".wh.")
        if not opaque and (not target_name or target_name in {".", ".."} or target_name.startswith(".wh.")):
            raise ChangesetValidationError("oci-whiteout-target", "Invalid OCI whiteout target")
        if member.kind is not EntryKind.FILE or member.size != 0 or member.xattrs:
            description = "opaque marker" if opaque else "whiteout marker"
            raise ChangesetValidationError(
                "oci-whiteout-marker",
                f"OCI {description} must be an empty regular file without control metadata",
            )
        parent = os.path.dirname(member.path) or "."
        path = parent if opaque else (os.path.join(parent, target_name) if parent != "." else target_name)
        if path not in lower_effects and len(lower_effects) >= max_entries:
            raise ChangesetValidationError("oci-changeset-entry-limit", "lower-layer effects exceed the entry limit")
        if not opaque or path not in lower_effects:
            lower_effects[path] = member

    table: dict[str, _Node[PayloadT]] = {}
    children: dict[str, set[str]] = {}
    directory_barriers: set[str] = set()
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
            check_work()
            descendant = pending.pop()
            pending.extend(children.get(descendant, ()))
            remove_exact(descendant, keep_children=False)
        children.pop(path, None)
        if include_root:
            remove_exact(path, keep_children=False)

    def set_node(path: str, node: _Node[PayloadT], *, preserve_children: bool) -> None:
        if node.kind is EntryKind.DIRECTORY and (path in lower_effects or path in directory_barriers):
            # A deleted/replaced lower directory must not regain old children
            # when recreated, including after an intermediate file/type churn.
            node.opaque = True
        check_work()
        if path not in table and len(table) >= max_entries:
            raise ChangesetValidationError("oci-changeset-entry-limit", "normalized filesystem exceeds its entry limit")
        account_metadata(path, node.member)
        remove_exact(path, keep_children=preserve_children)
        table[path] = node
        register(path)

    def ensure_parents(
        path: str,
        member: ChangesetMember[PayloadT],
        *,
        refresh_opaque_placeholder: bool = True,
    ) -> None:
        directory = ""
        for component in path.split("/")[:-1]:
            check_work()
            directory = f"{directory}/{component}" if directory else component
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
            check_work()
            node = table.get(link_path)
            if node is None or node.pending_target != target_path:
                continue
            node.pending_target = None
            node.group_id = group_id

    # Slash-terminated sort keys put ancestors before contiguous descendant
    # subtrees (including names such as "d-other" that sort before "d/").
    # A single active prefix suppresses descendant-only markers beneath a
    # deletion without rescanning effects or fabricating visible directories.
    # Retain suppressed effects for ordinary directory recreation below.
    deleted_prefix: str | None = None
    for path in sorted(lower_effects, key=lambda path: f"{path}/"):
        check_work()
        if deleted_prefix is not None and path.startswith(deleted_prefix):
            continue
        deleted_prefix = None
        member = lower_effects[path]
        if os.path.basename(member.path) == ".wh..wh..opq":
            opaque_path = path
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
        else:
            target_path = path
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
            deleted_prefix = f"{path}/"

    for member in members:
        check_work()
        path = member.path
        if os.path.basename(path).startswith(".wh."):
            continue
        if member.kind is not EntryKind.DIRECTORY and path not in lower_effects and path not in directory_barriers:
            if len(lower_effects) + len(directory_barriers) >= max_entries:
                raise ChangesetValidationError(
                    "oci-changeset-entry-limit", "lower-layer effects exceed the entry limit"
                )
            account_metadata(path, member)
            directory_barriers.add(path)

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
                set_node(path, node, preserve_children=False)
            elif target is not None and target.kind is EntryKind.HARDLINK:
                raise ChangesetValidationError(
                    "oci-hardlink-chain",
                    "OCI hardlink target must be a same-layer regular file",
                )
            else:
                node = _Node(member=member, kind=EntryKind.HARDLINK, pending_target=member.link_target)
                set_node(path, node, preserve_children=False)
                pending_by_target.setdefault(member.link_target, set()).add(path)
        else:
            node = _Node(
                member=member,
                kind=member.kind,
                opaque=preserve_opaque,
            )
            set_node(path, node, preserve_children=member.kind is EntryKind.DIRECTORY)

    if "." not in table:
        root = _synthetic_directory(".", 0, len(members))
        set_node(".", _Node(member=root, kind=EntryKind.DIRECTORY, synthetic=True), preserve_children=True)

    unresolved = sorted(path for path, node in table.items() if node.pending_target is not None)
    if unresolved:
        raise ChangesetValidationError(
            "oci-hardlink-target",
            "OCI hardlink target must be a same-layer regular file",
        )

    group_paths: dict[int, list[str]] = {}
    for path, node in table.items():
        check_work()
        if node.group_id is not None:
            group_paths.setdefault(node.group_id, []).append(path)

    carriers: dict[int, str] = {}
    for group_id, paths in group_paths.items():
        check_work()
        regular_paths = sorted(path for path in paths if table[path].kind is EntryKind.FILE)
        carriers[group_id] = regular_paths[0] if regular_paths else sorted(paths)[0]

    normalized: dict[str, NormalizedEntry[PayloadT]] = {}
    for path, node in table.items():
        check_work()
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
            inherit_directory_metadata=(
                node.kind is EntryKind.DIRECTORY
                and node.synthetic
                and path not in directory_barriers
                and (path not in lower_effects or os.path.basename(lower_effects[path].path) == ".wh..wh..opq")
            ),
        )

    ordered_paths: list[str] = []
    emitted: set[str] = set()
    remaining = sorted(normalized)
    while remaining:
        deferred: list[str] = []
        for path in remaining:
            check_work()
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
    "EffectiveFilesystem",
    "NormalizedChangeset",
    "NormalizedEntry",
    "normalize_changeset",
]
