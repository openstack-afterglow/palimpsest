"""Pure, deterministic base-migration structure comparison; no filesystem access.

Listings must satisfy the lister prerequisites in the structure-check contract. Each kept
layer is compared against the bases, never against earlier kept layers. This deliberately
errs toward conflicts. A clean result does not establish runtime compatibility.

For E listing entries, B base entries, L layers and maximum path depth D, validation and
flattening cost O(E*D + sum(visible lower entries per layer)*D). check costs
O(B log B + (B+E)*D + E log B + F log K), where F is the number of generated findings
and K=2000 is each fixed retention bound. Subtree queries use bisect and prefix counts,
not repeated tree scans, even for overlapping prefixes. Retained findings use O(K) space;
the validated listings and subtree index use O(B+E) space. Path-string operations also
cost their string lengths. No work depends on file size or content bytes.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

SCHEMA = "palimpsest.base-migration-check.v1"
KINDS = frozenset({"file", "dir", "symlink", "char", "block", "fifo", "socket"})
MAX_FINDINGS = 2000
MAX_NOTICES = 2000
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DEVICE = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")
STATEMENT = (
    "Structure check only: it compares paths, kinds, attributes and content identities of the old "
    "and new base against the paths the kept layers touch. It does not check ABI, package "
    "dependencies, or whether the migrated image runs; that verification is the user's."
)
# Shared package-manager state that install layers rewrite; conflicts here are expected and are
# reported separately so they are not mistaken for application-file conflicts.
PACKAGE_STATE_PREFIXES = (
    "var/lib/dpkg",
    "var/lib/apt",
    "var/cache/apt",
    "var/cache/debconf",
    "var/lib/rpm",
    "usr/lib/sysimage/rpm",
    "var/lib/dnf",
    "var/cache/dnf",
    "var/lib/yum",
    "var/cache/yum",
    "var/lib/pacman",
    "var/lib/apk",
    "lib/apk/db",
    "var/cache/apk",
    "var/cache/ldconfig",
    "etc/ld.so.cache",
)


class MigrationInputError(ValueError):
    """An input tree or layer is not a normalized, self-consistent listing."""


def _text(value: object) -> bool:
    return isinstance(value, str) and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


@dataclass(frozen=True, slots=True)
class Entry:
    """A relative entry; whiteouts have kind=None and no payload or attributes.

    content is a lowercase sha256 file-content identity; unavailable or unsupported identities
    remain representable but make comparisons incomplete. xattrs is an optional canonical
    digest as defined in the contract. Hard links are expanded into independent file entries.
    """

    path: str
    kind: str | None = None
    mode: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0
    content: str | None = None
    link: str = ""
    whiteout: bool = False
    opaque: bool = False
    xattrs: str | None = None

    def __post_init__(self) -> None:
        if (
            not _text(self.path)
            or not self.path
            or self.path.startswith("/")
            or "\0" in self.path
            or any(part in ("", ".", "..") or part.startswith(".wh.") for part in self.path.split("/"))
        ):
            raise MigrationInputError(f"path is not an admitted relative path: {self.path!r}")
        if type(self.whiteout) is not bool or type(self.opaque) is not bool:
            raise MigrationInputError("whiteout and opaque must be booleans")
        if any(type(value) is not int or value < 0 for value in (self.mode, self.uid, self.gid, self.size)):
            raise MigrationInputError("mode, uid, gid and size must be nonnegative integers")
        if self.mode > 0o7777:
            raise MigrationInputError("mode must contain only permission, setuid, setgid and sticky bits")
        if not _text(self.link) or "\0" in self.link:
            raise MigrationInputError("link must be NUL-free Unicode text")
        if self.content is not None and not _text(self.content):
            raise MigrationInputError("content identity must be Unicode text or None")
        if self.xattrs is not None and (not _text(self.xattrs) or not _DIGEST.fullmatch(self.xattrs)):
            raise MigrationInputError("xattrs must be a canonical sha256 identity or None")
        if self.whiteout:
            if (
                self.kind is not None
                or self.mode
                or self.uid
                or self.gid
                or self.size
                or self.content is not None
                or self.link
                or self.opaque
                or self.xattrs is not None
            ):
                raise MigrationInputError("whiteout must not carry kind, attributes or payload")
        elif not isinstance(self.kind, str) or self.kind not in KINDS:
            raise MigrationInputError(f"unknown entry kind: {self.kind!r}")
        if self.opaque and self.kind != "dir":
            raise MigrationInputError("only a directory can be opaque")
        if not self.whiteout:
            if self.kind != "file" and (self.size or self.content is not None):
                raise MigrationInputError("only files carry size and content")
            if self.kind in ("char", "block"):
                if not _DEVICE.fullmatch(self.link) or (self.kind == "char" and self.link == "0:0"):
                    raise MigrationInputError("device number must be major:minor; char 0:0 must be translated")
            elif self.kind != "symlink" and self.link:
                raise MigrationInputError("only symlinks and devices carry link")
            if self.kind == "symlink" and not self.link:
                raise MigrationInputError("symlink target must not be empty")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _ancestors(path: str) -> Iterable[str]:
    position = path.find("/")
    while position != -1:
        yield path[:position]
        position = path.find("/", position + 1)


def is_package_state(path: str) -> bool:
    return any(_under(path, prefix) for prefix in PACKAGE_STATE_PREFIXES)


def _listing(entries: Iterable[Entry], *, base: bool = False) -> dict[str, Entry]:
    result = {}
    for entry in entries:
        if not isinstance(entry, Entry):
            raise MigrationInputError("listings must contain Entry values")
        if entry.path in result:
            raise MigrationInputError(f"listing contains duplicate path: {entry.path!r}")
        if base and (entry.whiteout or entry.opaque):
            raise MigrationInputError("flattened base trees cannot contain whiteouts or opaque flags")
        result[entry.path] = entry
    for path in result:
        for ancestor in _ancestors(path):
            parent = result.get(ancestor)
            if parent is not None and (parent.whiteout or parent.kind != "dir"):
                raise MigrationInputError(f"entry below a non-directory: {path!r}")
    return result


def _base(tree: Mapping[str, Entry]) -> dict[str, Entry]:
    result = _listing(tree.values(), base=True)
    if any(key != entry.path for key, entry in tree.items()):
        raise MigrationInputError("base tree key differs from entry.path")
    return result


def flatten(layers: Iterable[Iterable[Entry]]) -> dict[str, Entry]:
    """Apply ordered overlays with one lower-tree filter pass per layer.

    Whiteouts and opacity affect lower entries only, not entries in the same listing.
    Missing directory ancestors are allowed, but explicit non-directory ancestors are not.
    """
    tree: dict[str, Entry] = {}
    for layer in layers:
        entries = _listing(layer)
        hide = {
            entry.path
            for entry in entries.values()
            if entry.whiteout
            or entry.opaque
            or entry.kind != "dir"
            or (entry.path in tree and tree[entry.path].kind != "dir")
        }
        tree = {
            path: entry
            for path, entry in tree.items()
            if path not in hide and not any(parent in hide for parent in _ancestors(path))
        }
        for entry in entries.values():
            if not entry.whiteout:
                tree[entry.path] = replace(entry, opaque=False)
        # A new child cannot traverse a lower symlink/file unless this layer replaces it by a dir.
        _listing(tree.values(), base=True)
    return tree


def _same(left: Entry, right: Entry) -> bool | None:
    """Compare full entries. Missing required identities take priority over known differences."""
    if (left.xattrs is None) != (right.xattrs is None):
        return None
    if any(
        entry.kind == "file" and (entry.content is None or not _DIGEST.fullmatch(entry.content))
        for entry in (left, right)
    ):
        return None
    return (left.kind, left.mode, left.uid, left.gid, left.xattrs, left.size, left.content, left.link) == (
        right.kind,
        right.mode,
        right.uid,
        right.gid,
        right.xattrs,
        right.size,
        right.content,
        right.link,
    )


def _summary(entry: Entry | None) -> dict | None:
    if entry is None:
        return None
    if entry.whiteout:
        return {"kind": "whiteout"}
    value = {"kind": entry.kind, "mode": entry.mode, "uid": entry.uid, "gid": entry.gid, "xattrs": entry.xattrs}
    if entry.kind == "file":
        value.update(size=entry.size, content=entry.content)
    if entry.kind in ("symlink", "char", "block"):
        value["link"] = entry.link
    if entry.opaque:
        value["opaque"] = True
    return value


class _Subtrees:
    """Sorted new-base paths and prefix counts for added, changed and unknown entries."""

    def __init__(self, old: Mapping[str, Entry], new: Mapping[str, Entry]):
        self.paths = sorted(new)
        self.counts = [(0, 0, 0)]
        for path in self.paths:
            same = _same(old[path], new[path]) if path in old else False
            previous = self.counts[-1]
            flags = (path not in old, path in old and same is False, same is None)
            self.counts.append(tuple(total + flag for total, flag in zip(previous, flags, strict=True)))

    def below(self, path: str) -> tuple[bool, bool, bool]:
        start = bisect_left(self.paths, path + "/")
        end = bisect_left(self.paths, path + "0")
        return tuple(
            end_count > start_count for start_count, end_count in zip(self.counts[start], self.counts[end], strict=True)
        )


def _difference(same: bool | None, changed: str, severity: str = "conflict") -> list[tuple[str, str]]:
    if same is None:
        return [("comparison-unavailable", "incomplete")]
    return [] if same else [(changed, severity)]


def _classify(
    entry: Entry, old: Mapping[str, Entry], new: Mapping[str, Entry], subtrees: _Subtrees
) -> set[tuple[str, str]]:
    before, after = old.get(entry.path), new.get(entry.path)
    findings: list[tuple[str, str]] = []
    if entry.whiteout:
        if after is None:
            findings = [("whiteout-target-gone", "notice")] if before is not None else []
        elif before is None:
            findings = [("whiteout-hides-new", "conflict")]
        else:
            findings = _difference(_same(before, after), "whiteout-target-changed", "notice")
        added, changed, unknown = subtrees.below(entry.path)
        if added:
            findings.append(("whiteout-hides-new", "conflict"))
        if changed:
            findings.append(("whiteout-target-changed", "notice"))
        if unknown:
            findings.append(("comparison-unavailable", "incomplete"))
        return set(findings)
    if after is None:
        if before is not None:
            findings.append(("base-removed", "notice"))
    elif (entry.kind == "dir") != (after.kind == "dir"):
        findings += _difference(_same(before, after) if before is not None else False, "kind-conflict")
    elif entry.kind == "dir":
        if before is None or before.kind != "dir":
            findings += _difference(_same(entry, after), "new-path-collision")
        else:
            same = _same(before, after)
            if same is None:
                findings.append(("comparison-unavailable", "incomplete"))
            elif not same:
                findings += _difference(_same(entry, after), "attributes-shadowed")
    elif before is None:
        same = _same(entry, after)
        findings += _difference(same, "new-path-collision") if same is not True else [("new-path-identical", "notice")]
    else:
        same = _same(before, after)
        if same is None:
            findings.append(("comparison-unavailable", "incomplete"))
        elif not same:
            identical = _same(entry, after)
            findings += (
                _difference(identical, "overwrite-changed")
                if identical is not True
                else [("overwrite-now-identical", "notice")]
            )
    if entry.opaque or entry.kind != "dir":
        added, changed, unknown = subtrees.below(entry.path)
        if added or changed:
            findings.append(("opaque-hides-changes", "conflict"))
        if unknown:
            findings.append(("comparison-unavailable", "incomplete"))
    return set(findings)


@dataclass
class _Retained:
    key: tuple
    value: dict = field(compare=False)

    def __lt__(self, other: _Retained) -> bool:
        return self.key > other.key  # Max heap: discard the largest deterministic finding key.


class _Findings:
    def __init__(self):
        self.counts = {"conflict": 0, "notice": 0, "incomplete": 0, "package_state_conflict": 0}
        self.important: list[_Retained] = []
        self.notices: list[_Retained] = []

    def add(
        self,
        index: int,
        layer_id: str,
        code: str,
        severity: str,
        path: str,
        old: Entry | None,
        new: Entry | None,
        entry: Entry | None,
    ) -> None:
        package_state = is_package_state(path)
        self.counts[severity] += 1
        if severity == "conflict" and package_state:
            self.counts["package_state_conflict"] += 1
        heap, bound = (self.notices, MAX_NOTICES) if severity == "notice" else (self.important, MAX_FINDINGS)
        key = (index, path, code)
        if len(heap) == bound and key >= heap[0].key:
            return
        value = {
            "layer_index": index,
            "layer": layer_id,
            "code": code,
            "severity": severity,
            "path": path,
            "package_state": package_state,
            "old": _summary(old),
            "new": _summary(new),
            "layer_entry": _summary(entry),
        }
        item = _Retained(key, value)
        if len(heap) < bound:
            heapq.heappush(heap, item)
        else:
            heapq.heapreplace(heap, item)


def check(
    old_base: Mapping[str, Entry],
    new_base: Mapping[str, Entry],
    layers: Sequence[tuple[str, Iterable[Entry]]],
    *,
    complete: bool = True,
) -> dict:
    """Compare kept layers against validated bases with fixed, separate finding bounds."""
    if type(complete) is not bool:
        raise MigrationInputError("complete must be a boolean")
    old, new = _base(old_base), _base(new_base)
    subtrees = _Subtrees(old, new)
    findings = _Findings()
    layer_rows = []
    for index, (layer_id, listing) in enumerate(layers):
        if not _text(layer_id) or not layer_id or "\0" in layer_id:
            raise MigrationInputError("layer identity must be nonempty NUL-free Unicode text")
        entries = _listing(listing)
        own_directories = {path for path, entry in entries.items() if entry.kind == "dir"}
        reported_ancestors: set[str] = set()
        for entry in entries.values():
            for ancestor in _ancestors(entry.path):
                if ancestor in own_directories or ancestor in reported_ancestors:
                    continue
                below = new.get(ancestor)
                if below is None or below.kind == "dir":
                    continue
                reported_ancestors.add(ancestor)
                earlier = old.get(ancestor)
                same = _same(earlier, below) if earlier is not None else False
                for code, severity in _difference(same, "ancestor-not-directory"):
                    findings.add(index, layer_id, code, severity, ancestor, earlier, below, None)
            for code, severity in _classify(entry, old, new, subtrees):
                findings.add(
                    index, layer_id, code, severity, entry.path, old.get(entry.path), new.get(entry.path), entry
                )
        layer_rows.append({"index": index, "id": layer_id, "entries": len(entries)})
    counts = findings.counts
    important_cut = counts["conflict"] + counts["incomplete"] > MAX_FINDINGS
    truncated = important_cut or counts["notice"] > MAX_NOTICES
    verdict = (
        "incomplete"
        if not complete or counts["incomplete"] or important_cut
        else "conflict"
        if counts["conflict"]
        else "clean"
    )
    retained = sorted(findings.important, key=lambda item: item.key) + sorted(
        findings.notices, key=lambda item: item.key
    )
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "requires_risk_acknowledgement": verdict != "clean",
        "inputs_complete": complete,
        "layers": layer_rows,
        "counts": counts,
        "findings": [item.value for item in retained],
        "findings_truncated": truncated,
        "statement": STATEMENT,
    }


def report_digest(report: Mapping) -> str:
    """Hash the complete report, including service-added members; no identity member is inserted."""
    raw = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = [
    "KINDS",
    "MAX_FINDINGS",
    "MAX_NOTICES",
    "PACKAGE_STATE_PREFIXES",
    "SCHEMA",
    "STATEMENT",
    "Entry",
    "MigrationInputError",
    "check",
    "flatten",
    "is_package_state",
    "report_digest",
]
