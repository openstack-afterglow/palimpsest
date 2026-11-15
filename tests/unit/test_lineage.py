from __future__ import annotations

import pytest

from palimpsest_local import base_migration, compatibility_stages
from palimpsest_local.lineage import Artifact, Consumer, LineageInputError, impact_plan


def d(char: str) -> str:
    return "sha256:" + char * 64


BASE_OLD, BASE_NEW = d("a"), d("b")
PYTHON, TORCH, VLLM, TOOLS, OTHER, PYTHON_NEW = d("1"), d("2"), d("3"), d("4"), d("5"), d("6")


def store(**changes) -> list[Artifact]:
    """Ubuntu -> python -> torch -> vllm, a second child of python, and an unrelated chain."""
    rows = {
        BASE_OLD: Artifact(BASE_OLD, "cloud-image", arch="x86_64", names=("ubuntu:24.04",)),
        BASE_NEW: Artifact(BASE_NEW, "cloud-image", arch="x86_64", names=("ubuntu:24.04-r2",)),
        PYTHON: Artifact(PYTHON, "squashfs", base=BASE_OLD, arch="x86_64", recipe=d("c"), engine="palimpsestfile"),
        TORCH: Artifact(
            TORCH, "squashfs", parent=PYTHON, base=BASE_OLD, arch="x86_64", recipe=d("d"), engine="palimpsestfile"
        ),
        VLLM: Artifact(VLLM, "squashfs", parent=TORCH, base=BASE_OLD, arch="x86_64"),
        TOOLS: Artifact(TOOLS, "squashfs", parent=PYTHON, base=BASE_OLD, arch="x86_64", names=("tools:1",)),
        OTHER: Artifact(OTHER, "squashfs", base=BASE_NEW, arch="x86_64"),
        PYTHON_NEW: Artifact(PYTHON_NEW, "squashfs", base=BASE_NEW, arch="x86_64"),
    }
    rows.update(changes)
    return list(rows.values())


def digests(plan) -> list[str]:
    return [item["digest"] for item in plan["affected"]]


def test_a_changed_layer_lists_every_descendant_once_nearest_first_and_nothing_else():
    plan = impact_plan(store(), changed=PYTHON)
    assert digests(plan) == [TORCH, TOOLS, VLLM]
    assert [item["depth"] for item in plan["affected"]] == [1, 1, 2]
    assert [item["parent"] for item in plan["affected"]] == [PYTHON, PYTHON, TORCH]
    assert plan["replacement"] is None
    assert all(item["compatibility"] is None for item in plan["affected"])


def test_a_changed_base_image_reaches_root_layers_and_their_chains_but_not_other_bases():
    plan = impact_plan(store(), changed=BASE_OLD)
    assert digests(plan) == [PYTHON, TORCH, TOOLS, VLLM]
    assert OTHER not in digests(plan) and PYTHON_NEW not in digests(plan)


def test_existing_lineage_is_kept_and_the_four_actions_of_a_new_lineage_are_separate_states():
    runs = [Consumer("run-a", frozenset({BASE_OLD, PYTHON, TORCH})), Consumer("project-b", frozenset({TORCH}))]
    plan = impact_plan(store(), changed=PYTHON, replacement=PYTHON_NEW, consumers=runs)
    torch, tools, vllm = plan["affected"]
    # Consumers stay bound to what they recorded; the plan reports them and changes nothing.
    assert torch["existing_lineage"] == {"state": "kept", "consumers": ["project-b", "run-a"]}
    assert plan["changed"]["consumers"] == ["run-a"]
    assert torch["new_lineage"] == {
        # Only the recipe's digest is recorded, and a Palimpsestfile names its parents by digest.
        "rebuild": {"state": "required", "recipe": "digest-recorded", "recipe_edit": "required", "cache": "not-reused"},
        "reconversion": {"state": "not-applicable"},
        "transfer": {"state": "not-inspected"},
        "deployment": {"state": "not-inspected"},
    }
    # A committed or imported layer has no recipe: it cannot be rebuilt from a record.
    assert vllm["new_lineage"]["rebuild"] == {"state": "required", "recipe": "not-recorded", "cache": "unknown"}
    assert plan["summary"] == {
        "affected": 3,
        "rebuild_required": 3,
        "recipe_digest_recorded": 1,
        "reconversion_required": 0,
        "with_consumers": 1,
        "unreached": 0,
    }
    assert tools["existing_lineage"]["consumers"] == []


def test_a_structurally_admissible_replacement_passes_structure_only_and_leaves_the_rest_unverified():
    plan = impact_plan(store(), changed=BASE_OLD, replacement=BASE_NEW)
    assert plan["replacement"]["structure"] == {"status": "passed", "reasons": []}
    for item in plan["affected"]:
        stages = {stage["stage"]: stage["status"] for stage in item["compatibility"]["stages"]}
        assert stages == {
            "structure": "passed",
            "filesystem": "unverified",
            "distribution_abi": "unverified",
            "kernel_device": "unverified",
            "smoke_test": "unverified",
        }
        assert "compatible" not in item["compatibility"]


@pytest.mark.parametrize(
    ("replacement", "status", "fragment"),
    [
        (Artifact(BASE_NEW, "cloud-image", arch="aarch64"), "failed", "targets aarch64"),
        (Artifact(BASE_NEW, "squashfs", arch="x86_64"), "failed", "not a cloud-image artifact"),
        (Artifact(BASE_NEW, "cloud-image"), "unverified", "architecture"),
        (None, "unverified", "no metadata"),
    ],
)
def test_a_replacement_that_contradicts_or_lacks_structure_is_reported_on_every_affected_layer(
    replacement, status, fragment
):
    rows = [row for row in store() if row.digest != BASE_NEW] + ([replacement] if replacement else [])
    plan = impact_plan(rows, changed=BASE_OLD, replacement=BASE_NEW)
    assert plan["replacement"]["structure"]["status"] == status
    assert plan["replacement"]["recorded"] is (replacement is not None)
    for item in plan["affected"]:
        structure = item["compatibility"]["stages"][0]
        assert structure["stage"] == "structure" and structure["status"] == status
        assert any(fragment in reason for reason in structure["reasons"])


def test_a_runtime_pack_needs_a_new_pack_for_another_base_not_a_new_build():
    # The base image is bound by the pack, not read by the Dockerfile build: the two caches are separate.
    pack = Artifact(d("7"), "squashfs", base=BASE_OLD, arch="x86_64", recipe=d("e"), engine="buildkit")
    plan = impact_plan([*store(), pack], changed=BASE_OLD, replacement=BASE_NEW)
    new_lineage = next(item for item in plan["affected"] if item["digest"] == pack.digest)["new_lineage"]
    assert new_lineage["rebuild"] == {
        "state": "inputs-unchanged",
        "recipe": "digest-recorded",
        "cache": "decided-by-buildkit",
    }
    assert new_lineage["reconversion"] == {"state": "required", "cache": "keyed-by-base-image"}
    assert plan["summary"]["reconversion_required"] == 1
    assert plan["summary"]["rebuild_required"] == plan["summary"]["affected"] - 1


@pytest.mark.parametrize("replacement", [TORCH, VLLM])
def test_a_replacement_that_descends_from_the_replaced_layer_fails_structure(replacement):
    plan = impact_plan(store(), changed=PYTHON, replacement=replacement)
    assert plan["replacement"]["structure"]["status"] == "failed"
    assert all(item["compatibility"]["stages"][0]["status"] == "failed" for item in plan["affected"])


ORPHAN = d("7")


@pytest.mark.parametrize(
    ("changes", "replacement"),
    [
        ({}, PYTHON_NEW),  # bound to another base: the base changes as well
        ({PYTHON: Artifact(PYTHON, "squashfs", arch="x86_64")}, PYTHON_NEW),  # the replaced chain records no base
        ({PYTHON_NEW: Artifact(PYTHON_NEW, "squashfs", arch="x86_64")}, PYTHON_NEW),  # the replacement records none
        # A base on the layer itself does not stand in for a parent that is not stored.
        ({ORPHAN: Artifact(ORPHAN, "squashfs", parent=d("8"), base=BASE_OLD, arch="x86_64")}, ORPHAN),
        ({PYTHON_NEW: Artifact(PYTHON_NEW, "unknown", base=BASE_OLD, arch="x86_64")}, PYTHON_NEW),
    ],
)
def test_a_layer_replacement_whose_chain_or_base_cannot_be_compared_is_unverified_not_passed(changes, replacement):
    plan = impact_plan(store(**changes), changed=PYTHON, replacement=replacement)
    assert plan["replacement"]["structure"]["status"] == "unverified"


def test_a_layer_base_is_read_through_its_chain_when_only_the_root_layer_records_it():
    # Stored metadata may bind the base on the root layer only; children then carry a parent and no base.
    rows = store(
        **{
            TORCH: Artifact(TORCH, "squashfs", parent=PYTHON, arch="x86_64"),
            TOOLS: Artifact(TOOLS, "squashfs", parent=PYTHON, arch="x86_64"),
        }
    )
    same_base = impact_plan(rows, changed=TORCH, replacement=TOOLS)
    assert same_base["replacement"]["structure"] == {"status": "passed", "reasons": []}


def test_a_layer_that_records_the_changed_artifact_but_is_out_of_reach_is_reported_not_dropped():
    # The layer between them is not stored here, so parent links cannot reach it.
    under_base = Artifact(ORPHAN, "squashfs", parent=d("8"), base=BASE_OLD, arch="x86_64")
    plan = impact_plan([*store(), under_base], changed=BASE_OLD)
    assert ORPHAN not in digests(plan)
    assert plan["unreached"] == [ORPHAN] and plan["summary"]["unreached"] == 1
    under_layer = Artifact(ORPHAN, "squashfs", parent=d("8"), arch="x86_64", ancestors=(PYTHON, d("8")))
    plan = impact_plan([*store(), under_layer], changed=PYTHON)
    assert plan["unreached"] == [ORPHAN]
    assert impact_plan(store(), changed=BASE_OLD)["unreached"] == []


def test_cycles_and_duplicate_records_do_not_repeat_or_loop():
    looped = store(**{PYTHON: Artifact(PYTHON, "squashfs", parent=VLLM, base=BASE_OLD, arch="x86_64")})
    plan = impact_plan(looped + looped, changed=TORCH)
    assert digests(plan) == [VLLM, PYTHON, TOOLS]


@pytest.mark.parametrize(
    "call",
    [
        lambda: impact_plan(store(), changed="sha256:short"),
        lambda: impact_plan(store(), changed=d("f")),
        lambda: impact_plan(store(), changed=PYTHON, replacement=PYTHON),
        lambda: impact_plan([Artifact(d("9"), "oci-image")], changed=d("9")),
        lambda: Artifact("latest", "squashfs"),
        lambda: Artifact(d("9"), "squashfs\nINFO forged"),
        lambda: Artifact(d("9"), "squashfs", arch="x86 64; rm"),
    ],
)
def test_unusable_requests_are_refused(call):
    with pytest.raises(LineageInputError):
        call()


def test_more_descendants_than_the_bound_is_reported_as_truncated(monkeypatch):
    monkeypatch.setattr("palimpsest_local.lineage.MAX_AFFECTED", 2)
    plan = impact_plan(store(), changed=BASE_OLD)
    assert len(plan["affected"]) == 2 and plan["truncated"] is True


# --- staged diagnosis ---------------------------------------------------------------------------


def entry(path: str, kind: str = "file", **kwargs) -> base_migration.Entry:
    if kind == "file":
        kwargs.setdefault("content", d("e"))
        kwargs.setdefault("size", 1)
    return base_migration.Entry(path=path, kind=kind, **kwargs)


def structure_report(*, layer, complete=True) -> dict:
    old = base_migration.flatten([[entry("etc", "dir"), entry("etc/app.conf")]])
    new = base_migration.flatten([[entry("etc", "dir"), entry("etc/app.conf", content=d("f"))]])
    return base_migration.check(old, new, [("layer", layer)], complete=complete)


def test_filesystem_stage_follows_the_structure_check_verdict_and_never_the_other_stages():
    clean = compatibility_stages.diagnose(
        structure={"status": "passed", "reasons": []}, filesystem=structure_report(layer=[entry("opt", "dir")])
    )
    assert [stage["status"] for stage in clean["stages"]] == [
        "passed",
        "passed",
        "unverified",
        "unverified",
        "unverified",
    ]
    assert clean["summary"] == {"passed": 2, "failed": 0, "unverified": 3}
    assert clean["stages"][1]["evidence"]["report_digest"].startswith("sha256:")

    conflict = compatibility_stages.diagnose(
        filesystem=structure_report(layer=[entry("etc", "dir"), entry("etc/app.conf")])
    )
    assert conflict["stages"][1]["status"] == "failed"
    assert "risk acknowledgement" in conflict["stages"][1]["reasons"][0]
    assert conflict["stages"][0]["status"] == "unverified"

    partial = compatibility_stages.diagnose(filesystem=structure_report(layer=[entry("opt", "dir")], complete=False))
    assert partial["stages"][1]["status"] == "unverified"


def test_only_a_stage_result_counts_as_structure_evidence():
    # A platform-check result ("compatible") also reflects the client host and backend; it is not a stage status.
    for foreign in ({"status": "compatible", "reasons": []}, {"status": "incompatible"}, "passed", ["passed"], {}):
        assert compatibility_stages.diagnose(structure=foreign)["stages"][0]["status"] == "unverified"
    passed = compatibility_stages.diagnose(structure={"status": "passed", "reasons": "not a list"})["stages"][0]
    assert passed["status"] == "passed" and passed["reasons"] == []


def test_a_found_conflict_fails_the_filesystem_stage_even_when_the_check_was_incomplete():
    report = structure_report(layer=[entry("etc", "dir"), entry("etc/app.conf")], complete=False)
    assert report["verdict"] == "incomplete" and report["counts"]["conflict"] == 1
    assert compatibility_stages.diagnose(filesystem=report)["stages"][1]["status"] == "failed"


def test_a_report_that_does_not_agree_with_itself_or_is_not_a_report_never_passes():
    clean = structure_report(layer=[entry("opt", "dir")])
    for report, status in (
        ({**clean, "counts": {**clean["counts"], "conflict": 1}}, "failed"),
        ({**clean, "inputs_complete": False}, "unverified"),
        ({**clean, "counts": "none"}, "unverified"),
        ({**clean, "extra": float("nan")}, "unverified"),
        ({**clean, "schema": "something-else"}, "unverified"),
        (["clean"], "unverified"),
    ):
        assert compatibility_stages.diagnose(filesystem=report)["stages"][1]["status"] == status
    assert compatibility_stages.diagnose(structure="x", filesystem=3, distribution=3)["summary"]["passed"] == 0


def test_a_declared_release_change_is_reported_but_the_abi_stage_stays_unverified():
    changed = compatibility_stages.diagnose(distribution={"old": "ubuntu 22.04", "new": "ubuntu 24.04"})["stages"][2]
    assert changed["status"] == "unverified"
    assert changed["reasons"][0] == "declared release changes: ubuntu 22.04 -> ubuntu 24.04"
    same = compatibility_stages.diagnose(distribution={"old": "ubuntu 24.04", "new": "ubuntu 24.04"})["stages"][2]
    assert same["status"] == "unverified" and same["reasons"][0] == "declared release is unchanged: ubuntu 24.04"
    # Metadata text that is not a short plain name is not repeated in a diagnosis.
    hostile = compatibility_stages.diagnose(distribution={"old": "x\nINFO forged", "new": "ubuntu 24.04"})["stages"][2]
    assert hostile["reasons"] == ["Palimpsest does not evaluate package dependencies or library ABI"]
