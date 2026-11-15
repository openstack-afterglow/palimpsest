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
LAYER, BASE, UNRECORDED = "squashfs", "cloud-image", "unknown"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
STATEMENT = (
    "This plan reads recorded metadata only. Every listed artifact stays bound to its current "
    "parent and keeps working as before; nothing is rebuilt, converted, transferred or deployed. "
    "A new lineage exists only after each affected layer is produced again for the new parent, and "
    "compatibility beyond the stages marked passed is not verified."
)


class LineageInputError(ValueError):
    """A record or a requested digest is not usable for a lineage plan."""


@dataclass(frozen=True)
class Artifact:
    """What the plan needs to know about one stored artifact.

    ``recipe`` is the recorded digest of the recipe that produced the artifact and ``engine``
    the engine of that build record (``palimpsestfile`` or ``buildkit``); both are ``None``
    for committed or imported artifacts. A ``buildkit`` artifact is a runtime pack: an OCI
    build result converted to one SquashFS block for one base image. ``ancestors`` are the
    lower layers the artifact's own record lists, when it lists them.
    """

    digest: str
    kind: str
    parent: str | None = None
    base: str | None = None
    arch: str | None = None
    names: tuple[str, ...] = ()
    recipe: str | None = None
    engine: str | None = None
    ancestors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ancestors, tuple):
            raise LineageInputError("recorded ancestors must be a tuple of digests")
        for value in (self.digest, self.parent, self.base, self.recipe, *self.ancestors):
            if value is not None and (not isinstance(value, str) or _DIGEST.match(value) is None):
                raise LineageInputError("artifact digests must be sha256:<64 lowercase hex digits>")
        # kind and arch are repeated in diagnoses, so they must be plain labels, not free text.
        if self.digest is None or not isinstance(self.kind, str) or _LABEL.match(self.kind) is None:
            raise LineageInputError("an artifact needs a digest and a plain kind label")
        if self.arch is not None and (not isinstance(self.arch, str) or _LABEL.match(self.arch) is None):
            raise LineageInputError("an artifact architecture must be a plain label")


@dataclass(frozen=True)
class Consumer:
    """A recorded user of artifacts (a run, a project, a retained instance); it stays bound to what it recorded."""

    name: str
    digests: frozenset[str]


@dataclass(frozen=True)
class _Chain:
    """A layer's recorded way down to its base image."""

    ancestors: frozenset[str]
    base: str | None
    complete: bool


def _chain(artifact: Artifact, known: dict[str, Artifact]) -> _Chain:
    """Walk recorded parent links. The base is the nearest one recorded; complete means a root layer was reached."""
    ancestors: set[str] = set()
    base: str | None = None
    current = artifact
    while True:
        if base is None:
            base = current.base
        if current.parent is None:
            return _Chain(frozenset(ancestors), base, True)
        following = known.get(current.parent)
        if following is None or following.digest in ancestors or following.digest == artifact.digest:
            return _Chain(frozenset(ancestors | {current.parent}), base, False)
        ancestors.add(following.digest)
        current = following


def _structure(
    old: Artifact, new: Artifact | None, replacement: str | None, known: dict[str, Artifact]
) -> dict[str, Any] | None:
    """Structural admissibility of the replacement in the old artifact's position."""
    if replacement is None:
        return None
    if new is None:
        return compatibility_stages.structure_stage(unverified=["the replacement has no metadata in this store"])
    failed, unknown = [], []
    if new.kind == UNRECORDED:
        unknown.append("the replacement records no artifact kind")
    elif new.kind != old.kind:
        failed.append(f"the replacement is a {new.kind} artifact, not a {old.kind} artifact")
    if old.arch is None or new.arch is None:
        unknown.append("the architecture of the replaced artifact or of the replacement is not declared")
    elif old.arch != new.arch:
        failed.append(f"the replacement targets {new.arch}, the replaced artifact {old.arch}")
    if new.kind == LAYER and old.kind == LAYER:
        before, after = _chain(old, known), _chain(new, known)
        if old.digest in after.ancestors:
            failed.append(
                "the replacement descends from the replaced layer; a layer cannot be rebuilt on its own descendant"
            )
        if not after.complete:
            unknown.append("the replacement layer's parent chain is not completely recorded in this store")
        if after.base is None:
            unknown.append("the replacement layer's chain records no base image")
        elif before.base is None:
            unknown.append("the replaced layer's chain records no base image, so the base binding cannot be compared")
        elif after.base != before.base:
            unknown.append("the replacement layer is bound to a different base image; the base changes as well")
    return compatibility_stages.structure_stage(failed=failed, unverified=unknown)


def _child_structure(replaced: dict[str, Any] | None, child: Artifact, new: Artifact | None) -> dict[str, Any] | None:
    if replaced is None:
        return None
    failed = list(replaced["reasons"]) if replaced["status"] == compatibility_stages.FAILED else []
    unknown = list(replaced["reasons"]) if replaced["status"] == compatibility_stages.UNVERIFIED else []
    if new is not None and new.arch is not None:
        if child.arch is None:
            unknown.append("this layer declares no architecture")
        elif child.arch != new.arch:
            failed.append(f"this layer targets {child.arch}, the replacement {new.arch}")
    return compatibility_stages.structure_stage(failed=failed, unverified=unknown)


def _new_lineage(child: Artifact) -> dict[str, dict[str, str]]:
    """What producing this layer for another parent involves; nothing here says it will succeed."""
    if child.engine == "buildkit":
        # A runtime pack is the conversion of an OCI build result for one base image. The base is not
        # an input of the Dockerfile build, so another base changes the pack, not the build: the build
        # command runs again, BuildKit's own cache decides which steps execute, and the pack has a
        # different key because that key binds the base image.
        rebuild = {
            "state": "inputs-unchanged",
            "recipe": "digest-recorded" if child.recipe else "not-recorded",
            "cache": "decided-by-buildkit",
        }
        reconversion = {"state": "required", "cache": "keyed-by-base-image"}
    elif child.recipe is not None:
        # A Palimpsestfile names FROM and LAYER by digest and runs as a whole in a fresh builder.
        # Only the recipe's digest is recorded, and the recipe itself must name the new parent.
        rebuild = {"state": "required", "recipe": "digest-recorded", "recipe_edit": "required", "cache": "not-reused"}
        reconversion = {"state": "not-applicable"}
    else:
        # Committed or imported: there is no record to build from.
        rebuild = {"state": "required", "recipe": "not-recorded", "cache": "unknown"}
        reconversion = {"state": "not-applicable"}
    return {
        "rebuild": rebuild,
        "reconversion": reconversion,
        "transfer": {"state": "not-inspected"},
        "deployment": {"state": "not-inspected"},
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
                structure = _child_structure(replaced, child, new)
                affected.append(
                    {
                        "digest": child.digest,
                        "names": sorted(child.names),
                        "arch": child.arch,
                        "depth": depth,
                        "parent": parent,
                        "existing_lineage": {"state": "kept", "consumers": sorted(set(users.get(child.digest, ())))},
                        "new_lineage": _new_lineage(child),
                        "compatibility": None
                        if structure is None
                        else compatibility_stages.diagnose(structure=structure),
                    }
                )
            if truncated:
                break
        frontier = following

    # Parent links are followed one stored record at a time, so a layer whose own record names the changed
    # artifact is out of reach when a layer between them is not stored here. It is reported, not dropped.
    unreached = (
        []
        if truncated
        else sorted(
            artifact.digest
            for artifact in known.values()
            if artifact.kind == LAYER
            and artifact.digest not in seen
            and (changed in artifact.ancestors or (old.kind == BASE and _chain(artifact, known).base == changed))
        )
    )

    def count(action: str, state: str) -> int:
        return sum(1 for item in affected if item["new_lineage"][action]["state"] == state)

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
        "unreached": unreached[:MAX_AFFECTED],
        "summary": {
            "affected": len(affected),
            "rebuild_required": count("rebuild", "required"),
            "recipe_digest_recorded": sum(
                1 for item in affected if item["new_lineage"]["rebuild"]["recipe"] == "digest-recorded"
            ),
            "reconversion_required": count("reconversion", "required"),
            "with_consumers": sum(1 for item in affected if item["existing_lineage"]["consumers"]),
            "unreached": len(unreached),
        },
        "truncated": truncated or len(unreached) > MAX_AFFECTED,
        "statement": STATEMENT,
    }


__all__ = [
    "BASE",
    "LAYER",
    "MAX_AFFECTED",
    "SCHEMA",
    "STATEMENT",
    "UNRECORDED",
    "Artifact",
    "Consumer",
    "LineageInputError",
    "impact_plan",
]
