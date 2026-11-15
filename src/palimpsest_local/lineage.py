"""Read-only impact plan for replacing a parent layer or a base image.

A layer names its parent and base by digest, so a new parent never changes an existing
child. This module only answers "which recorded artifacts descend from this one, and what
would a new lineage on another parent need": it builds, converts, transfers and deploys
nothing, and it does not decide that a replacement is compatible. Rebuild, reconversion,
transfer and deployment are separate states because each is a separate action with its own
cache and its own failure. Pure: the caller supplies metadata records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import compatibility_stages

SCHEMA = "palimpsest.lineage-impact-plan.v1"
MAX_AFFECTED = 5000
LAYER, BASE = "squashfs", "cloud-image"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
STATEMENT = (
    "This plan reads recorded metadata only. Every listed artifact stays bound to its current "
    "parent and keeps working as before; nothing is rebuilt, converted, transferred or deployed. "
    "A new lineage exists only after each affected layer is built again on the new parent, and "
    "compatibility beyond the stages marked passed is not verified."
)


class LineageInputError(ValueError):
    """A record or a requested digest is not usable for a lineage plan."""


@dataclass(frozen=True)
class Artifact:
    """What the plan needs to know about one stored artifact.

    ``recipe`` is the digest of a recorded build recipe that produced the artifact, ``engine``
    the build engine of that record; both are ``None`` for committed or imported artifacts.
    """

    digest: str
    kind: str
    parent: str | None = None
    base: str | None = None
    arch: str | None = None
    names: tuple[str, ...] = ()
    recipe: str | None = None
    engine: str | None = None

    def __post_init__(self) -> None:
        for value in (self.digest, self.parent, self.base, self.recipe):
            if value is not None and (not isinstance(value, str) or _DIGEST.match(value) is None):
                raise LineageInputError("artifact digests must be sha256:<64 lowercase hex digits>")
        # kind and arch are repeated in diagnoses, so they must be plain labels, not free text.
        if self.digest is None or not isinstance(self.kind, str) or _LABEL.match(self.kind) is None:
            raise LineageInputError("an artifact needs a digest and a plain kind label")
        if self.arch is not None and (not isinstance(self.arch, str) or _LABEL.match(self.arch) is None):
            raise LineageInputError("an artifact architecture must be a plain label")


@dataclass(frozen=True)
class Consumer:
    """A recorded user of artifacts (a run or a project); it stays bound to what it recorded."""

    name: str
    digests: frozenset[str]


def _bound_base(artifact: Artifact, known: dict[str, Artifact]) -> str | None:
    """The base image a layer is bound to: its own record, or the nearest ancestor that records one."""
    seen: set[str] = set()
    current: Artifact | None = artifact
    while current is not None and current.digest not in seen:
        if current.base is not None:
            return current.base
        seen.add(current.digest)
        current = known.get(current.parent) if current.parent is not None else None
    return None


def _structure(
    old: Artifact, new: Artifact | None, replacement: str | None, known: dict[str, Artifact]
) -> dict[str, Any] | None:
    """Structural admissibility of the replacement in the old artifact's position."""
    if replacement is None:
        return None
    if new is None:
        return compatibility_stages.structure_stage(unverified=["the replacement has no metadata in this store"])
    failed, unknown = [], []
    if new.kind != old.kind:
        failed.append(f"the replacement is a {new.kind} artifact, not a {old.kind} artifact")
    if old.arch is None or new.arch is None:
        unknown.append("the architecture of the replaced artifact or of the replacement is not declared")
    elif old.arch != new.arch:
        failed.append(f"the replacement targets {new.arch}, the replaced artifact {old.arch}")
    if new.kind == LAYER and old.kind == LAYER:
        old_base, new_base = _bound_base(old, known), _bound_base(new, known)
        if new_base is None:
            unknown.append("the replacement layer's chain to a base image is not recorded in this store")
        elif old_base is not None and new_base != old_base:
            unknown.append("the replacement layer is bound to a different base image; the base changes as well")
    return compatibility_stages.structure_stage(failed=failed, unverified=unknown)


def _child_structure(replaced: dict[str, Any] | None, child: Artifact, new: Artifact | None) -> dict[str, Any] | None:
    if replaced is None:
        return None
    failed = [reason for reason in replaced["reasons"]] if replaced["status"] == compatibility_stages.FAILED else []
    unknown = (
        [reason for reason in replaced["reasons"]] if replaced["status"] == compatibility_stages.UNVERIFIED else []
    )
    if new is not None and new.arch is not None:
        if child.arch is None:
            unknown.append("this layer declares no architecture")
        elif child.arch != new.arch:
            failed.append(f"this layer targets {child.arch}, the replacement {new.arch}")
    return compatibility_stages.structure_stage(failed=failed, unverified=unknown)


def _rebuild(child: Artifact) -> dict[str, str]:
    cache = {
        "buildkit": "decided-by-buildkit",  # the solver reuses steps whose inputs did not change
        "palimpsestfile": "not-reused",  # the recipe runs again as a whole on the new parent
    }.get(child.engine or "", "unknown")
    return {
        "state": "required",
        "recipe": "recorded" if child.recipe else "not-recorded",
        "cache": cache,
    }


def impact_plan(
    artifacts: Iterable[Artifact],
    *,
    changed: str,
    replacement: str | None = None,
    consumers: Iterable[Consumer] = (),
) -> dict[str, Any]:
    """List every recorded descendant of ``changed`` and what a lineage on ``replacement`` would need."""
    for value in (changed, replacement):
        if value is not None and (not isinstance(value, str) or _DIGEST.match(value) is None):
            raise LineageInputError("digests must be sha256:<64 lowercase hex digits>")
    if replacement == changed:
        raise LineageInputError("the replacement is the artifact being replaced")
    known: dict[str, Artifact] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Artifact):
            raise LineageInputError("artifacts must be Artifact records")
        known.setdefault(artifact.digest, artifact)
    old = known.get(changed)
    if old is None:
        raise LineageInputError("the artifact to replace has no metadata in this store")
    if old.kind not in (LAYER, BASE):
        raise LineageInputError("only a layer or a base image can be replaced as a parent")
    new = known.get(replacement) if replacement is not None else None

    children: dict[str, list[Artifact]] = {}
    for artifact in known.values():
        if artifact.kind != LAYER or artifact.digest == changed:
            continue
        # A base image is the parent of the root layers bound to it; later layers reach it through their chain.
        parent = artifact.parent if artifact.parent is not None else artifact.base
        if parent is not None:
            children.setdefault(parent, []).append(artifact)
    users: dict[str, list[str]] = {}
    for consumer in consumers:
        for digest in consumer.digests:
            users.setdefault(digest, []).append(consumer.name)

    replaced = _structure(old, new, replacement, known)
    affected: list[dict[str, Any]] = []
    seen = {changed}
    frontier, depth, truncated = [changed], 0, False
    while frontier and not truncated:
        depth += 1
        following: list[str] = []
        for parent in frontier:
            for child in sorted(children.get(parent, ()), key=lambda item: item.digest):
                if child.digest in seen:
                    continue
                if len(affected) >= MAX_AFFECTED:
                    truncated = True
                    break
                seen.add(child.digest)
                following.append(child.digest)
                bound = sorted(set(users.get(child.digest, ())))
                structure = _child_structure(replaced, child, new)
                affected.append(
                    {
                        "digest": child.digest,
                        "names": sorted(child.names),
                        "arch": child.arch,
                        "depth": depth,
                        "parent": parent,
                        "existing_lineage": {"state": "kept", "consumers": bound},
                        "new_lineage": {
                            "rebuild": _rebuild(child),
                            # These layers are runtime SquashFS already; no OCI conversion stands between build and run.
                            "reconversion": {"state": "not-applicable"},
                            "transfer": {"state": "not-inspected"},
                            "deployment": {"state": "not-inspected"},
                        },
                        "compatibility": None
                        if structure is None
                        else compatibility_stages.diagnose(structure=structure),
                    }
                )
            if truncated:
                break
        frontier = following

    return {
        "schema": SCHEMA,
        "scope": "recorded-local-metadata",
        "changed": {
            "digest": old.digest,
            "kind": old.kind,
            "arch": old.arch,
            "names": sorted(old.names),
            "consumers": sorted(set(users.get(old.digest, ()))),
        },
        "replacement": None
        if replacement is None
        else {
            "digest": replacement,
            "recorded": new is not None,
            "kind": new.kind if new is not None else None,
            "arch": new.arch if new is not None else None,
            "structure": replaced,
        },
        "affected": affected,
        "summary": {
            "affected": len(affected),
            "rebuild_with_recipe": sum(
                1 for item in affected if item["new_lineage"]["rebuild"]["recipe"] == "recorded"
            ),
            "rebuild_without_recipe": sum(
                1 for item in affected if item["new_lineage"]["rebuild"]["recipe"] == "not-recorded"
            ),
            "with_consumers": sum(1 for item in affected if item["existing_lineage"]["consumers"]),
        },
        "truncated": truncated,
        "statement": STATEMENT,
    }


__all__ = [
    "BASE",
    "LAYER",
    "MAX_AFFECTED",
    "SCHEMA",
    "STATEMENT",
    "Artifact",
    "Consumer",
    "LineageInputError",
    "impact_plan",
]
