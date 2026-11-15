from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cli_contract import _snapshot_cli_state
from test_inventory import _resource_project, _resource_run, _setup_roots

from palimpsest_local import cli, inventory, lineage, state
from palimpsest_local.build import create_build_record
from palimpsest_local.errors import StateError
from palimpsest_local.hub import MEDIA_TYPE_LAYER_SQUASHFS
from palimpsest_local.oci_layout import ContentStore
from palimpsest_local.project import deterministic_service_name


def _artifact(store, name, kind="squashfs", **metadata):
    digest = "sha256:" + store.write_stream([name.encode()]).name
    store.write_metadata(digest, {"kind": kind, "arch": "x86_64", **metadata})
    return digest


def _tag(roots, name, digest):
    state.write_tag_record(
        roots,
        state.TagRecord(
            schema_version=1,
            tag=name,
            digest=digest,
            media_type=MEDIA_TYPE_LAYER_SQUASHFS,
            size_bytes=1,
            parent_digest=None,
            base_image_digest=None,
            source="build",
            created_at="2026-11-15T00:00:00Z",
        ),
    )


def _record(roots, record):
    directory = roots.builds / record["build_id"]
    directory.mkdir(mode=0o700)
    state.atomic_write_json(directory / "record.json", record)


@pytest.fixture
def stored(tmp_path):
    roots = _setup_roots(tmp_path)
    store = ContentStore(roots.store)
    base = _artifact(store, "base", "cloud-image", disk_format="raw")
    replacement = _artifact(store, "replacement", "cloud-image", disk_format="raw")
    root = _artifact(store, "root", base_image_digest=base)
    # Imported legacy descendants may name only a parent, not repeat the root's base.
    child = _artifact(store, "child", parent_digest=root)
    branch = _artifact(store, "branch", parent_digest=root)
    leaf = _artifact(store, "leaf", parent_digest=child)
    unrelated = _artifact(store, "unrelated", base_image_digest=replacement)
    _tag(roots, "base-old", base)
    _tag(roots, "base-new", replacement)
    _tag(roots, "python", root)
    _tag(roots, "torch", child)
    _record(
        roots,
        create_build_record(
            build_id="b-000000000001",
            base_digest=base,
            arch="x86_64",
            parent_digests=[root],
            recipe_sha256="sha256:" + "a" * 64,
            output_digest=child,
            output_tag="torch",
        ),
    )
    _record(
        roots,
        create_build_record(
            build_id="b-000000000002",
            base_digest=base,
            arch="x86_64",
            parent_digests=[root],
            recipe_sha256="sha256:" + "b" * 64,
            output_digest=branch,
            status="failed",
        ),
    )
    run_name = deterministic_service_name("project-bound", "app")
    run_id = _resource_run(roots, run_name, base={"digest": base}, layers=[root, {"digest": child}, leaf])
    _resource_project(roots, "project-bound", service="app", run_id=run_id)
    return roots, {
        "base": base,
        "replacement": replacement,
        "root": root,
        "child": child,
        "branch": branch,
        "leaf": leaf,
        "unrelated": unrelated,
        "run": run_name,
    }


def test_layer_and_base_descendants_follow_stored_parent_links_without_inventing_child_base(stored):
    roots, digests = stored
    layer_plan = inventory.lineage_impact(roots, digests["root"])
    assert [item["digest"] for item in layer_plan["affected"]] == [
        *sorted([digests["child"], digests["branch"]]),
        digests["leaf"],
    ]
    assert [item["depth"] for item in layer_plan["affected"]] == [1, 1, 2]
    base_plan = inventory.lineage_impact(roots, digests["base"], replacement=digests["replacement"])
    assert [item["digest"] for item in base_plan["affected"]] == [
        digests["root"],
        *sorted([digests["child"], digests["branch"]]),
        digests["leaf"],
    ]
    assert base_plan["replacement"]["structure"]["status"] == "passed"
    assert base_plan["warnings"] == []
    for item in base_plan["affected"]:
        assert {stage["stage"]: stage["status"] for stage in item["compatibility"]["stages"]} == {
            "structure": "passed",
            "filesystem": "unverified",
            "distribution_abi": "unverified",
            "kernel_device": "unverified",
            "smoke_test": "unverified",
        }
    assert ContentStore(roots.store).read_metadata(digests["child"]).get("base_image_digest") is None


def test_completed_build_recipe_and_bound_run_and_project_are_reported(stored):
    roots, digests = stored
    plan = inventory.lineage_impact(roots, "python")
    rows = {item["digest"]: item for item in plan["affected"]}
    assert rows[digests["child"]]["new_lineage"]["rebuild"] == {
        "state": "required",
        "recipe": "recorded",
        "cache": "not-reused",
    }
    for name in ("branch", "leaf"):
        assert rows[digests[name]]["new_lineage"]["rebuild"]["recipe"] == "not-recorded"
    assert rows[digests["child"]]["existing_lineage"] == {
        "state": "kept",
        "consumers": ["project:project-bound", f"run:{digests['run']}"],
    }
    assert rows[digests["branch"]]["existing_lineage"]["consumers"] == []


def test_buildkit_recipe_uses_recorded_dockerfile_digest_for_runtime_output(stored):
    roots, digests = stored
    _record(
        roots,
        {
            "schema_version": 2,
            "build_id": "bk-000000000003",
            "engine": "buildkit",
            "status": "success",
            "runtime_block_digest": digests["leaf"],
            "dockerfile_digest": "sha256:" + "c" * 64,
            "output_oci_manifest_digest": "sha256:" + "d" * 64,
        },
    )
    plan = inventory.lineage_impact(roots, "python")
    leaf = next(item for item in plan["affected"] if item["digest"] == digests["leaf"])
    assert leaf["new_lineage"]["rebuild"] == {
        "state": "required",
        "recipe": "recorded",
        "cache": "decided-by-buildkit",
    }


def test_tags_and_digests_resolve_to_the_same_plan_without_store_writes(stored):
    roots, digests = stored
    before = _snapshot_cli_state(roots.state)
    by_digest = inventory.lineage_impact(roots, digests["base"], replacement=digests["replacement"])
    by_tag = inventory.lineage_impact(roots, "base-old", replacement="base-new")
    assert by_tag == by_digest
    assert _snapshot_cli_state(roots.state) == before
    assert not roots.metadata_db.exists()


@pytest.mark.parametrize("reference", ["missing-tag", "sha256:" + "f" * 64])
def test_unknown_reference_is_normal_user_facing_error(stored, reference):
    roots, _ = stored
    with pytest.raises(StateError):
        inventory.lineage_impact(roots, reference)
    with pytest.raises(StateError):
        inventory.lineage_impact(roots, "python", replacement=reference)


def test_invalid_lineage_request_becomes_normal_user_facing_error(stored):
    roots, _ = stored
    with pytest.raises(StateError, match="replacement is the artifact being replaced"):
        inventory.lineage_impact(roots, "python", replacement="python")
    store = ContentStore(roots.store)
    unsupported = _artifact(store, "unsupported", "oci-image")
    with pytest.raises(StateError, match="only a layer or a base image"):
        inventory.lineage_impact(roots, unsupported)


@pytest.mark.parametrize("kind", ["artifact", "tag", "build", "run", "project"])
def test_corrupt_individual_records_warn_without_paths_or_contents_and_keep_valid_rows(stored, kind):
    roots, digests = stored
    if kind == "artifact":
        path = roots.store / "metadata" / ("e" * 64 + ".json")
    elif kind == "tag":
        path = roots.tags / "bad-tag.json"
    elif kind == "build":
        directory = roots.builds / "b-000000000004"
        directory.mkdir(mode=0o700)
        path = directory / "record.json"
    elif kind == "run":
        _resource_run(roots, "bad-run")
        path = roots.runs / "bad-run" / "state.json"
    else:
        path = _resource_project(roots, "bad-project")
    path.write_text('{"secret": "SENSITIVE_VALUE"', encoding="utf-8")
    before = _snapshot_cli_state(roots.state)
    plan = inventory.lineage_impact(roots, "python")
    assert len(plan["affected"]) == 3 and plan["warnings"]
    assert str(roots.state) not in repr(plan["warnings"])
    assert "SENSITIVE_VALUE" not in repr(plan["warnings"])
    assert _snapshot_cli_state(roots.state) == before


@pytest.mark.parametrize("kind", ["bad\0kind", "BAD", "x" * 33])
def test_invalid_artifact_labels_are_skipped_with_fixed_warning(stored, kind):
    roots, digests = stored
    store = ContentStore(roots.store)
    metadata = store.read_metadata(digests["branch"])
    metadata["kind"] = kind
    state.atomic_write_json(store.metadata_path(digests["branch"]), metadata)
    plan = inventory.lineage_impact(roots, "python")
    assert {item["digest"] for item in plan["affected"]} == {digests["child"], digests["leaf"]}
    assert plan["warnings"] == ["Some content-store metadata is unavailable or inconsistent"]


def test_layer_replacement_resolves_base_through_parents_without_writing_metadata(stored, monkeypatch):
    roots, digests = stored
    before = _snapshot_cli_state(roots.state)
    monkeypatch.setattr(
        inventory.local_catalog, "open_catalog", lambda *_args, **_kwargs: pytest.fail("catalog opened")
    )
    monkeypatch.setattr(inventory.runtime_dispatch, "reconcile", lambda *_args, **_kwargs: pytest.fail("reconciled"))
    monkeypatch.setattr(inventory, "artifact_reference_guard", lambda *_args: pytest.fail("mutation lock acquired"))
    plan = inventory.lineage_impact(roots, digests["child"], replacement=digests["branch"])
    assert plan["replacement"]["structure"]["status"] == "passed"
    assert [item["digest"] for item in plan["affected"]] == [digests["leaf"]]
    assert _snapshot_cli_state(roots.state) == before


def test_corrupt_recipe_identity_does_not_claim_recorded_recipe(stored):
    roots, digests = stored
    path = roots.builds / "b-000000000001" / "record.json"
    record = state.read_json(path)
    record["recipe_sha256"] = "not-a-digest"
    state.atomic_write_json(path, record)
    plan = inventory.lineage_impact(roots, "python")
    child = next(item for item in plan["affected"] if item["digest"] == digests["child"])
    assert child["new_lineage"]["rebuild"]["recipe"] == "not-recorded"
    assert plan["warnings"]


def test_cli_json_human_failed_structure_truncation_and_cloud_refusal(stored, tmp_path, monkeypatch, capsys):
    roots, digests = stored
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PALIMPSEST_STATE_DIR", raising=False)
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_args: pytest.fail("journal write reached"))
    monkeypatch.setattr(cli, "hub_client", lambda *_args: pytest.fail("Hub reached"))
    before = _snapshot_cli_state(roots.state)
    assert cli.main(["layer", "impact", "base-old", "--replacement", "base-new", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema"] == lineage.SCHEMA and plan["scope"] == "recorded-local-metadata"
    assert plan["warnings"] == [] and plan["changed"]["digest"] == digests["base"]
    assert plan["statement"] == lineage.STATEMENT
    assert cli.main(["layer", "impact", "base-old", "--replacement", "base-new"]) == 0
    human = capsys.readouterr().out
    assert lineage.STATEMENT in human
    assert "structure=passed filesystem=unverified" in human
    assert "rebuild: required (recipe recorded; build cache not-reused)" in human
    assert "rebuild: required (no recipe recorded)" in human
    assert "consumers stay bound: project:project-bound" in human
    assert "torch" in human and digests["child"][:19] in human
    assert _snapshot_cli_state(roots.state) == before
    # A wrong-kind replacement is a computed diagnostic plan, not a rejected admission.
    assert cli.main(["layer", "impact", "base-old", "--replacement", "python", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["replacement"]["structure"]["status"] == "failed"
    monkeypatch.setattr(lineage, "MAX_AFFECTED", 1)
    assert cli.main(["layer", "impact", "python"]) == 0
    assert "plan truncated" in capsys.readouterr().out
    assert cli.main(["--cloud", "unused", "layer", "impact", "python"]) == 1
    assert "--cloud does not apply to layer impact" in capsys.readouterr().err
    assert cli.main(["layer", "impact", "missing-tag"]) == 1
    assert capsys.readouterr().err == "state file not found\n"


def test_cli_missing_state_stays_absent(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "missing-state"))
    monkeypatch.delenv("PALIMPSEST_STATE_DIR", raising=False)
    monkeypatch.setattr(cli.host_journal, "begin", lambda *_args: pytest.fail("journal write reached"))
    assert cli.main(["layer", "impact", "sha256:" + "f" * 64, "--json"]) == 1
    assert "artifact is not present" in capsys.readouterr().err
    assert not (tmp_path / "missing-state").exists()
    assert not (tmp_path / "missing-config").exists()
