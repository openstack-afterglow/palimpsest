"""Staged compatibility diagnosis: every stage reports only what was actually evaluated.

The stages are separate verification levels. A passed stage says nothing about the stages
after it, and there is no overall "compatible" verdict. A stage that nothing evaluated is
``unverified`` with the reason; it is never read as safe. Pure: no filesystem, network or
runtime access, and no path contents or credentials in the output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import base_migration

SCHEMA = "palimpsest.compatibility-diagnosis.v1"
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
PASSED, FAILED, UNVERIFIED = "passed", "failed", "unverified"
# Order is the order of dependence: a later stage is only meaningful when the earlier ones hold.
STAGES: tuple[tuple[str, str], ...] = (
    ("structure", "parent chain, digests, artifact kinds, base binding and declared architecture"),
    ("filesystem", "path collisions, overwrites, whiteouts and opaque directories between a base and the kept layers"),
    ("distribution_abi", "distribution release, libc and library ABI, package dependencies"),
    ("kernel_device", "guest kernel, drivers, devices and GPU"),
    ("smoke_test", "an actual run of the composed environment"),
)
STATEMENT = (
    "Each stage is a separate check. A passed stage does not establish the stages after it, "
    "and unverified means not evaluated, not safe. Only running the composed environment "
    "shows that an application works."
)
_NOT_EVALUATED = {
    "distribution_abi": "Palimpsest does not evaluate package dependencies or library ABI",
    "kernel_device": "guest kernel, driver and device support is evaluated only by running on the target",
    "smoke_test": "no run of this combination is recorded",
}


def _reasons(values: Iterable[object]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def structure_stage(*, failed: Iterable[str] = (), unverified: Iterable[str] = ()) -> dict[str, Any]:
    """A structure result from separate lists of definite contradictions and unknowns."""
    failed, unverified = _reasons(failed), _reasons(unverified)
    status = FAILED if failed else UNVERIFIED if unverified else PASSED
    return {"status": status, "reasons": failed + unverified}


def _from_platform_check(result: Mapping[str, Any]) -> dict[str, Any]:
    """Map ``artifact_compatibility.check_artifact`` (compatible/incompatible/unknown) to a stage."""
    status = {"compatible": PASSED, "incompatible": FAILED}.get(result.get("status"), UNVERIFIED)
    reasons = _reasons(result.get("reasons") or ())
    if status == UNVERIFIED and not reasons:
        reasons = ["the platform metadata check gave no usable result"]
    return {"status": status, "reasons": reasons}


def _from_structure_check(report: Mapping[str, Any]) -> dict[str, Any]:
    """Map a ``base_migration.check`` report to the filesystem stage; anything else is unverified."""
    counts = report.get("counts")
    verdict = report.get("verdict")
    if report.get("schema") != base_migration.SCHEMA or not isinstance(counts, Mapping):
        return {"status": UNVERIFIED, "reasons": ["the supplied structure-check report is not a recognised report"]}
    evidence = {
        "schema": base_migration.SCHEMA,
        "report_digest": base_migration.report_digest(report),
        "counts": {key: counts[key] for key in sorted(counts) if type(counts[key]) is int},
    }
    if verdict == "clean":
        return {"status": PASSED, "reasons": [], "evidence": evidence}
    if verdict == "conflict":
        reason = "the structure check found conflicts; running this combination needs an explicit risk acknowledgement"
        return {"status": FAILED, "reasons": [reason], "evidence": evidence}
    return {
        "status": UNVERIFIED,
        "reasons": ["the structure check could not be completed for every kept layer"],
        "evidence": evidence,
    }


def diagnose(
    *,
    structure: Mapping[str, Any] | None = None,
    filesystem: Mapping[str, Any] | None = None,
    distribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the five stages from the results that exist.

    ``structure`` is a ``structure_stage`` result or a ``check_artifact`` result;
    ``filesystem`` is a ``base_migration.check`` report; ``distribution`` may carry the declared
    ``old`` and ``new`` release names, which are reported but never turn the ABI stage to passed.
    """
    found: dict[str, dict[str, Any]] = {}
    if structure is None:
        found["structure"] = {"status": UNVERIFIED, "reasons": ["no structure evaluation was supplied"]}
    elif "scope" in structure:
        found["structure"] = _from_platform_check(structure)
    elif structure.get("status") in (PASSED, FAILED, UNVERIFIED):
        found["structure"] = {"status": structure["status"], "reasons": _reasons(structure.get("reasons") or ())}
    else:
        found["structure"] = {"status": UNVERIFIED, "reasons": ["the structure evaluation has no usable status"]}
    if filesystem is None:
        found["filesystem"] = {"status": UNVERIFIED, "reasons": ["no structure check was run for this combination"]}
    else:
        found["filesystem"] = _from_structure_check(filesystem)
    abi = [_NOT_EVALUATED["distribution_abi"]]
    if distribution is not None:
        old, new = distribution.get("old"), distribution.get("new")
        # Release names come from artifact metadata: only short plain names are repeated.
        if all(isinstance(name, str) and _RELEASE.fullmatch(name) for name in (old, new)):
            abi.insert(
                0,
                f"declared release is unchanged: {new}" if old == new else f"declared release changes: {old} -> {new}",
            )
    found["distribution_abi"] = {"status": UNVERIFIED, "reasons": abi}
    for name in ("kernel_device", "smoke_test"):
        found[name] = {"status": UNVERIFIED, "reasons": [_NOT_EVALUATED[name]]}
    stages = [{"stage": name, "checks": checks, **found[name]} for name, checks in STAGES]
    summary = {status: sum(1 for item in stages if item["status"] == status) for status in (PASSED, FAILED, UNVERIFIED)}
    return {"schema": SCHEMA, "stages": stages, "summary": summary, "statement": STATEMENT}


__all__ = ["FAILED", "PASSED", "SCHEMA", "STAGES", "STATEMENT", "UNVERIFIED", "diagnose", "structure_stage"]
