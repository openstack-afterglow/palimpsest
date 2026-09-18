from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "check_afterglow_drift.py"
MANIFEST_PATH = REPOSITORY_ROOT / "tracking" / "afterglow-palimpsest.json"


def _load_checker_module():
    spec = importlib.util.spec_from_file_location("afterglow_drift_checker", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(root: Path, relative_path: str, content: str) -> dict[str, str]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "path": relative_path,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def test_checker_accepts_matching_contract_and_rejects_source_drift(tmp_path: Path):
    checker = _load_checker_module()
    upstream_root = tmp_path / "upstream"
    local_root = tmp_path / "local"
    upstream_artifact = _artifact(upstream_root, "api/hub.py", "route = '/layers'\n")
    local_artifact = _artifact(local_root, "client.py", "client = '/layers'\n")
    manifest = {
        "schema_version": 1,
        "upstream": {"repository": "example/upstream", "ref": "main", "baseline_commit": "a" * 40},
        "contracts": [
            {
                "id": "hub",
                "status": "compatible",
                "analysis": "Synthetic protocol contract.",
                "upstream": [{**upstream_artifact, "must_contain": ["/layers"]}],
                "counterparts": [{**local_artifact, "must_contain": ["/layers"]}],
            }
        ],
    }

    assert checker.verify_contract(manifest, upstream_root, local_root) == []

    (upstream_root / "api" / "hub.py").write_text("route = '/images'\n", encoding="utf-8")
    errors = checker.verify_contract(manifest, upstream_root, local_root)
    assert errors == [
        "hub upstream: content drift in api/hub.py",
        "hub upstream: api/hub.py is missing required marker '/layers'",
    ]


def test_checker_rejects_forbidden_markers_and_absent_service_creation(tmp_path: Path):
    checker = _load_checker_module()
    upstream_root = tmp_path / "upstream"
    local_root = tmp_path / "local"
    upstream_artifact = _artifact(upstream_root, "api/hub.py", "route = '/layers'\n")
    local_artifact = _artifact(local_root, "client.py", "client = '/layers'\n")
    manifest = {
        "schema_version": 1,
        "upstream": {"repository": "example/upstream", "ref": "main", "baseline_commit": "b" * 40},
        "absence_rules": [{"glob": "services/palimpsest*", "expected_matches": 0}],
        "contracts": [
            {
                "id": "hub",
                "status": "compatible",
                "analysis": "Synthetic protocol contract.",
                "upstream": [{"path": upstream_artifact["path"], "must_not_contain": ["legacy = True"]}],
                "counterparts": [local_artifact],
            }
        ],
    }

    assert checker.verify_contract(manifest, upstream_root, local_root) == []

    (upstream_root / "api" / "hub.py").write_text("legacy = True\n", encoding="utf-8")
    (upstream_root / "services" / "palimpsest").mkdir(parents=True)
    errors = checker.verify_contract(manifest, upstream_root, local_root)
    assert "hub upstream: api/hub.py contains forbidden marker 'legacy = True'" in errors
    assert "upstream: services/palimpsest* matched 1 paths; expected 0" in errors

    manifest["contracts"][0]["upstream"][0]["must_not_contain"] = ""
    errors = checker.verify_contract(manifest, upstream_root, local_root)
    assert "hub upstream: api/hub.py has a non-list must_not_contain" in errors


def test_manifest_records_the_current_hub_protocol_gap():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["upstream"]["baseline_commit"] == "d0250db689631f095dab2ac78ddad89651422c6b"

    contracts = {contract["id"]: contract for contract in manifest["contracts"]}
    hub_contract = contracts["hub-http-v1"]
    assert hub_contract["status"] == "implemented"
    assert "Upload-Offset" in hub_contract["analysis"]
    assert 'HUB_API_PREFIX = "/v1"' in hub_contract["counterparts"][0]["must_contain"]

    union_contract = contracts["union-layer-operations"]
    assert union_contract["status"] == "afterglow-owned"
    assert union_contract["counterparts"] == []
