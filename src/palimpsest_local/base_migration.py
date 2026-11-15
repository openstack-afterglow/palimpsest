"""Structure check for moving existing layers onto a new base image.

Pure comparison, no I/O: callers supply the flattened old and new base trees and the entries of
each layer that is kept, in order. The check answers one question: does a path the layers touch
meet something different in the new base than it met in the old one? It says nothing about
ABI, dependencies or whether the result runs.

Verdicts: ``clean`` (every comparison was possible and none conflicts), ``conflict``, and
``incomplete`` (an input was partial, a comparison was impossible, or the finding list was cut).
Only ``clean`` may run without an explicit risk acknowledgement; ``incomplete`` is never clean.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

SCHEMA = "palimpsest.base-migration-check.v1"
KINDS = frozenset({"file", "dir", "symlink", "char", "block", "fifo", "socket"})
MAX_FINDINGS = 2000
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
    "var/lib/dnf",
    "var/cache/dnf",
    "var/lib/apk",
    "lib/apk/db",
    "var/cache/apk",
    "var/cache/ldconfig",
    "etc/ld.so.cache",
)


class MigrationInputError(ValueError):
    """An input tree or layer is not a normalized, self-consistent listing."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One filesystem entry. ``content`` is an opaque identity (for example a digest) or None."""

    path: str
    kind: str
    mode: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0
    content: str | None = None
    link: str = ""
    whiteout: bool = False
    opaque: bool = False

    def __post_init__(self) -> None:
        path = self.path
        if (
            not path
            or path.startswith("/")
            or "\0" in path
            or posixpath.normpath(path) != path
            or path == "."
            or path.startswith("../")
            or path == ".."
        ):
            raise MigrationInputError(f"path is not a normalized relative path: {path!r}")
        if self.whiteout:
            if self.opaque:
                raise MigrationInputError("a whiteout cannot be opaque")
        elif self.kind not in KINDS:
            raise MigrationInputError(f"unknown entry kind: {self.kind!r}")
        if self.opaque and self.kind != "dir":
            raise MigrationInputError("only a directory can be opaque")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def is_package_state(path: str) -> bool:
    return any(_under(path, prefix) for prefix in PACKAGE_STATE_PREFIXES)


def flatten(layers: Iterable[Iterable[Entry]]) -> dict[str, Entry]:
    """Apply ordered layers with overlay semantics and return the visible tree."""
    tree: dict[str, Entry] = {}
    for layer in layers:
        entries = sorted(layer, key=lambda item: item.path)
        for entry in entries:
            if entry.whiteout:
                tree = {path: item for path, item in tree.items() if not _under(path, entry.path)}
            elif entry.opaque or (entry.path in tree and (tree[entry.path].kind == "dir") != (entry.kind == "dir")):
                # An opaque directory, or a kind change between directory and non-directory, hides
                # everything the lower layers had at and below the path.
                tree = {path: item for path, item in tree.items() if not _under(path, entry.path)}
        for entry in entries:
            if not entry.whiteout:
                tree[entry.path] = Entry(
                    entry.path, entry.kind, entry.mode, entry.uid, entry.gid, entry.size, entry.content, entry.link
                )
    return tree


def _attributes(entry: Entry) -> tuple:
    return (entry.kind, entry.mode, entry.uid, entry.gid)


def _same(left: Entry, right: Entry) -> bool | None:
    """True/False when the two entries can be compared, None when content identity is missing."""
    if _attributes(left) != _attributes(right):
        return False
    if left.kind == "symlink":
        return left.link == right.link
    if left.kind == "file":
        if left.size != right.size:
            return False
        if left.content is None or right.content is None:
            return None
        return left.content == right.content
    if left.kind in ("char", "block"):
        return left.link == right.link  # device numbers travel in ``link`` as "major:minor"
    return True


def _summary(entry: Entry | None) -> dict | None:
    if entry is None:
        return None
    value = {"kind": "whiteout" if entry.whiteout else entry.kind}
    if not entry.whiteout:
        value.update(mode=entry.mode, uid=entry.uid, gid=entry.gid)
        if entry.kind == "file":
            value.update(size=entry.size, content=entry.content)
        if entry.link:
            value["link"] = entry.link
        if entry.opaque:
            value["opaque"] = True
    return value


def _classify(entry: Entry, old: Mapping[str, Entry], new: Mapping[str, Entry]) -> list[tuple[str, str]]:
    """(code, severity) findings for one layer entry; severity is conflict, notice or incomplete."""
    before, after = old.get(entry.path), new.get(entry.path)
    if entry.whiteout:
        if after is None:
            return [("whiteout-target-gone", "notice")] if before is not None else []
        if before is None:
            return [("whiteout-hides-new", "conflict")]
        same = _same(before, after)
        if same is None:
            return [("comparison-unavailable", "incomplete")]
        return [] if same else [("whiteout-target-changed", "notice")]
    findings: list[tuple[str, str]] = []
    if entry.kind == "dir":
        if after is not None and after.kind != "dir":
            same = _same(before, after) if before is not None else False
            if same is None:
                findings.append(("comparison-unavailable", "incomplete"))
            elif not same:
                findings.append(("kind-conflict", "conflict"))
        elif after is not None:
            if before is None or before.kind != "dir":
                if _attributes(entry) != _attributes(after):
                    findings.append(("new-path-collision", "conflict"))
            elif _attributes(before) != _attributes(after) and _attributes(entry) != _attributes(after):
                findings.append(("attributes-shadowed", "conflict"))
        elif before is not None:
            findings.append(("base-removed", "notice"))
        if entry.opaque:
            below_old = {path: item for path, item in old.items() if path != entry.path and _under(path, entry.path)}
            below_new = {path: item for path, item in new.items() if path != entry.path and _under(path, entry.path)}
            verdict: bool | None = True
            for path, item in below_new.items():
                same = _same(below_old[path], item) if path in below_old else False
                if same is False:
                    verdict = False
                    break
                if same is None:
                    verdict = None
            if verdict is False:
                findings.append(("opaque-hides-changes", "conflict"))
            elif verdict is None:
                findings.append(("comparison-unavailable", "incomplete"))
        return findings
    if after is None:
        return [("base-removed", "notice")] if before is not None else []
    if after.kind == "dir":
        # A non-directory in the layer hides the whole base directory. That was already the
        # case with the old base when it had a directory there too.
        return [] if before is not None and before.kind == "dir" else [("kind-conflict", "conflict")]
    if before is None:
        same = _same(entry, after)
        if same is None:
            return [("comparison-unavailable", "incomplete")]
        return [("new-path-identical", "notice")] if same else [("new-path-collision", "conflict")]
    same = _same(before, after)
    if same is None:
        return [("comparison-unavailable", "incomplete")]
    if same:
        return []
    identical = _same(entry, after)
    if identical is None:
        return [("comparison-unavailable", "incomplete")]
    return [("overwrite-now-identical", "notice")] if identical else [("overwrite-changed", "conflict")]


def check(
    old_base: Mapping[str, Entry],
    new_base: Mapping[str, Entry],
    layers: Sequence[tuple[str, Iterable[Entry]]],
    *,
    complete: bool = True,
    max_findings: int = MAX_FINDINGS,
) -> dict:
    """Compare ``layers`` (ordered ``(layer id, entries)``) against the old and new base trees.

    ``complete=False`` states that some input could not be listed in full; the verdict is then
    ``incomplete`` whatever else is found.
    """
    findings: list[dict] = []
    layer_rows = []
    for index, (layer_id, entries) in enumerate(layers):
        ordered = sorted(entries, key=lambda item: item.path)
        seen: set[str] = set()
        own_directories = {item.path for item in ordered if not item.whiteout and item.kind == "dir"}
        reported_ancestors: set[str] = set()
        for entry in ordered:
            if entry.path in seen:
                raise MigrationInputError(f"layer {layer_id} lists {entry.path!r} twice")
            seen.add(entry.path)
            results = list(_classify(entry, old_base, new_base))
            for ancestor in _ancestors(entry.path):
                if ancestor in own_directories or ancestor in reported_ancestors:
                    continue
                below = new_base.get(ancestor)
                if below is None or below.kind == "dir":
                    continue
                reported_ancestors.add(ancestor)
                earlier = old_base.get(ancestor)
                unchanged = _same(earlier, below) if earlier is not None else False
                if unchanged:
                    continue  # the old base had the same non-directory there
                findings.append(
                    {
                        "layer_index": index,
                        "layer": layer_id,
                        "code": "ancestor-not-directory" if unchanged is False else "comparison-unavailable",
                        "severity": "conflict" if unchanged is False else "incomplete",
                        "path": ancestor,
                        "package_state": is_package_state(ancestor),
                        "old": _summary(earlier),
                        "new": _summary(below),
                        "layer_entry": None,
                    }
                )
            for code, severity in results:
                findings.append(
                    {
                        "layer_index": index,
                        "layer": layer_id,
                        "code": code,
                        "severity": severity,
                        "path": entry.path,
                        "package_state": is_package_state(entry.path),
                        "old": _summary(old_base.get(entry.path)),
                        "new": _summary(new_base.get(entry.path)),
                        "layer_entry": _summary(entry),
                    }
                )
        layer_rows.append({"index": index, "id": layer_id, "entries": len(ordered)})
    findings.sort(key=lambda item: (item["layer_index"], item["path"], item["code"]))
    counts = {"conflict": 0, "notice": 0, "incomplete": 0, "package_state_conflict": 0}
    for item in findings:
        counts[item["severity"]] += 1
        if item["severity"] == "conflict" and item["package_state"]:
            counts["package_state_conflict"] += 1
    truncated = len(findings) > max_findings
    if not complete or counts["incomplete"] or truncated:
        verdict = "incomplete"
    elif counts["conflict"]:
        verdict = "conflict"
    else:
        verdict = "clean"
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "requires_risk_acknowledgement": verdict != "clean",
        "inputs_complete": bool(complete),
        "layers": layer_rows,
        "counts": counts,
        "findings": findings[:max_findings],
        "findings_truncated": truncated,
        "statement": STATEMENT,
    }


def report_digest(report: Mapping) -> str:
    """Identity of a report: SHA-256 of its canonical JSON."""
    raw = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = [
    "KINDS",
    "MAX_FINDINGS",
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
