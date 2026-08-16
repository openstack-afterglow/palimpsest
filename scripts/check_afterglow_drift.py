#!/usr/bin/env python3
"""Verify the Palimpsest extraction contract against an Afterglow checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

VALID_STATUSES = {
    "afterglow-owned",
    "blocked-upstream-protocol",
    "compatible",
    "diverged-stricter-local",
    "implemented",
    "partial",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _errors_for_artifact(artifact: dict[str, Any], root: Path, label: str) -> list[str]:
    path_value = artifact.get("path")
    if not isinstance(path_value, str) or not path_value:
        return [f"{label}: artifact path is missing"]

    path = root / path_value
    if not path.is_file():
        return [f"{label}: missing {path_value}"]

    errors: list[str] = []
    marker_sets: dict[str, list[str]] = {}
    for field in ("must_contain", "must_not_contain"):
        raw_markers = artifact.get(field, [])
        if not isinstance(raw_markers, list):
            errors.append(f"{label}: {path_value} has a non-list {field}")
            marker_sets[field] = []
            continue
        marker_sets[field] = []
        for marker in raw_markers:
            if not isinstance(marker, str) or not marker:
                errors.append(f"{label}: {path_value} has an invalid {field} marker")
            else:
                marker_sets[field].append(marker)

    required_markers = marker_sets["must_contain"]
    forbidden_markers = marker_sets["must_not_contain"]
    expected_hash = artifact.get("sha256")
    if expected_hash is None:
        if not required_markers and not forbidden_markers:
            errors.append(f"{label}: {path_value} needs sha256 or at least one marker")
    elif not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
        errors.append(f"{label}: {path_value} has no valid sha256")
    elif _sha256(path) != expected_hash:
        errors.append(f"{label}: content drift in {path_value}")

    try:
        content = _text(path)
    except UnicodeDecodeError:
        return [*errors, f"{label}: {path_value} is not UTF-8 text"]

    for marker in required_markers:
        if marker not in content:
            errors.append(f"{label}: {path_value} is missing required marker {marker!r}")
    for marker in forbidden_markers:
        if marker in content:
            errors.append(f"{label}: {path_value} contains forbidden marker {marker!r}")
    return errors


def verify_contract(manifest: dict[str, Any], upstream_root: Path, local_root: Path) -> list[str]:
    """Return contract-drift errors without printing, for unit-test reuse."""
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("manifest: unsupported schema_version")

    upstream = manifest.get("upstream")
    if not isinstance(upstream, dict):
        errors.append("manifest: upstream is missing")
    else:
        for key in ("repository", "ref", "baseline_commit"):
            if not isinstance(upstream.get(key), str) or not upstream[key]:
                errors.append(f"manifest: upstream.{key} is missing")

    seen_contract_ids: set[str] = set()
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        return [*errors, "manifest: contracts must be a non-empty list"]

    for contract in contracts:
        if not isinstance(contract, dict):
            errors.append("manifest: contract entry is not an object")
            continue
        contract_id = contract.get("id")
        if not isinstance(contract_id, str) or not contract_id:
            errors.append("manifest: contract id is missing")
            continue
        if contract_id in seen_contract_ids:
            errors.append(f"manifest: duplicate contract id {contract_id}")
        seen_contract_ids.add(contract_id)

        status = contract.get("status")
        if status not in VALID_STATUSES:
            errors.append(f"{contract_id}: unsupported status {status!r}")
        if not isinstance(contract.get("analysis"), str) or not contract["analysis"]:
            errors.append(f"{contract_id}: analysis is missing")

        source_artifacts = contract.get("upstream")
        counterpart_artifacts = contract.get("counterparts")
        if not isinstance(source_artifacts, list) or not source_artifacts:
            errors.append(f"{contract_id}: upstream artifacts are missing")
        else:
            for artifact in source_artifacts:
                if not isinstance(artifact, dict):
                    errors.append(f"{contract_id}: upstream artifact is not an object")
                else:
                    errors.extend(_errors_for_artifact(artifact, upstream_root, f"{contract_id} upstream"))
        if not isinstance(counterpart_artifacts, list):
            errors.append(f"{contract_id}: counterparts must be a list")
        else:
            for artifact in counterpart_artifacts:
                if not isinstance(artifact, dict):
                    errors.append(f"{contract_id}: counterpart artifact is not an object")
                else:
                    errors.extend(_errors_for_artifact(artifact, local_root, f"{contract_id} counterpart"))

    absence_rules = manifest.get("absence_rules", [])
    if not isinstance(absence_rules, list):
        errors.append("manifest: absence_rules must be a list")
    else:
        for rule in absence_rules:
            if not isinstance(rule, dict):
                errors.append("manifest: absence rule is not an object")
                continue
            pattern = rule.get("glob")
            expected_matches = rule.get("expected_matches")
            if not isinstance(pattern, str) or not pattern:
                errors.append("manifest: absence rule glob is missing")
                continue
            if not isinstance(expected_matches, int) or expected_matches < 0:
                errors.append(f"manifest: absence rule {pattern!r} has an invalid expected_matches")
                continue
            actual_matches = sum(1 for path in upstream_root.glob(pattern) if path.exists())
            if actual_matches != expected_matches:
                errors.append(f"upstream: {pattern} matched {actual_matches} paths; expected {expected_matches}")
    return errors


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"manifest {path} must contain a JSON object")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tracking/afterglow-palimpsest.json"),
        help="contract manifest path (default: %(default)s)",
    )
    parser.add_argument(
        "--upstream-root",
        type=Path,
        required=True,
        help="clean checkout of the tracked Afterglow revision",
    )
    parser.add_argument(
        "--local-root",
        type=Path,
        default=Path.cwd(),
        help="Palimpsest repository root (default: current directory)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = _load_manifest(args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not args.upstream_root.is_dir():
        print(f"ERROR: upstream root does not exist: {args.upstream_root}", file=sys.stderr)
        return 2
    if not args.local_root.is_dir():
        print(f"ERROR: local root does not exist: {args.local_root}", file=sys.stderr)
        return 2

    errors = verify_contract(manifest, args.upstream_root, args.local_root)
    if errors:
        print("Afterglow Palimpsest contract drift detected:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    upstream = manifest["upstream"]
    print(
        "Afterglow Palimpsest contract verified "
        f"against {upstream['repository']}@{upstream['baseline_commit']} ({upstream['ref']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
