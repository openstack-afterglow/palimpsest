"""Static safety contract for SHA-specific development package releases."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "development-package.yml"
ALLOWED_BRANCHES = ["main", "dev", "codex/oci-root-phase1"]


def _run_steps(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_development_package_workflow_publishes_only_from_verified_allowed_branches() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the unquoted `on:` key to True.
    triggers = workflow[True]

    assert set(triggers) == {"workflow_dispatch", "push"}
    assert triggers["push"]["branches"] == ALLOWED_BRANCHES
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "development-package-${{ github.ref }}",
        "cancel-in-progress": False,
    }
    assert set(workflow["jobs"]) == {"verify", "publish"}

    verify = workflow["jobs"]["verify"]
    for branch in ALLOWED_BRANCHES:
        assert f"github.ref == 'refs/heads/{branch}'" in verify["if"]
    assert "permissions" not in verify
    verify_runs = "\n".join(_run_steps(verify))
    for required in (
        "scripts/check_architecture.py",
        "scripts/generate_cli_reference.py --check",
        "scripts/test_lanes.py list --check",
        "scripts/test_lanes.py run core-cli",
        "tests/unit/test_publish_development_package.py",
        'scripts/build_package.py --out-dir "$RUNNER_TEMP/palimpsest-dist"',
    ):
        assert required in verify_runs


def test_publish_job_verifies_checksums_before_the_non_destructive_helper() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    publish = workflow["jobs"]["publish"]

    assert publish["needs"] == "verify"
    assert publish["permissions"] == {"contents": "write"}
    assert [step["uses"] for step in publish["steps"] if "uses" in step] == [
        "actions/checkout@v4",
        "actions/download-artifact@v4",
    ]

    runs = _run_steps(publish)
    checksum = next(index for index, run in enumerate(runs) if "sha256sum --check SHA256SUMS" in run)
    publication = next(index for index, run in enumerate(runs) if "scripts/publish_development_package.py" in run)
    assert checksum < publication

    helper = runs[publication]
    assert '--repository "${GITHUB_REPOSITORY}"' in helper
    assert '--sha "${GITHUB_SHA}"' in helper
    assert "--dist-dir dist" in helper

    body = "\n".join(runs).lower()
    for forbidden in ("gh api", "gh release create", "--clobber", "--force", " delete"):
        assert forbidden not in body
    assert "pypa/gh-action-pypi-publish" not in WORKFLOW.read_text(encoding="utf-8")


def test_formal_release_workflow_remains_tag_gated() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'tags: ["v*"]' in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    publish = workflow.split("  publish:\n", 1)[1].split("\n  github-release:\n", 1)[0]
    assert "    needs: [verify, kvm-proof]\n" in publish
    assert "    if: needs.kvm-proof.result == 'success'\n" in publish
