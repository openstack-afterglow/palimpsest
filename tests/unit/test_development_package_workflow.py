"""Static safety contract for SHA-specific development package releases."""

from __future__ import annotations

import subprocess
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


def test_formal_release_permissions_and_publication_gate() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    assert workflow[True] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}

    def effective_permissions(job: dict) -> dict:
        # A job permissions map replaces (rather than augments) workflow permissions;
        # all unspecified scopes become none in GitHub Actions.
        return job.get("permissions", workflow["permissions"])

    jobs = workflow["jobs"]
    assert effective_permissions(jobs["verify"]) == {"contents": "read"}
    assert effective_permissions(jobs["kvm-proof"]) == {"contents": "read"}
    assert effective_permissions(jobs["publish"]) == {"id-token": "write"}
    assert effective_permissions(jobs["github-release"]) == {"contents": "write"}
    assert jobs["publish"]["needs"] == ["verify", "kvm-proof"]
    assert jobs["publish"]["if"] == "needs.kvm-proof.result == 'success'"
    assert jobs["github-release"]["needs"] == "publish"
    assert any(step.get("uses") == "pypa/gh-action-pypi-publish@release/v1" for step in jobs["publish"]["steps"])


def test_native_proofs_keep_secrets_on_prove_and_cleanup_only() -> None:
    for name, job_id in (("test.yml", "kvm"), ("release.yml", "kvm-proof")):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        job = workflow["jobs"][job_id]
        assert job["runs-on"] == "ubuntu-24.04"
        assert job["environment"] == "palimpsest-native-kvm"
        assert job["concurrency"] == {"group": "palimpsest-native-kvm", "cancel-in-progress": False}
        assert job["timeout-minutes"] > 45
        assert "env" not in job and "permissions" not in job
        assert job["steps"][0] == {"uses": "actions/checkout@v4", "with": {"persist-credentials": False}}
        prove = next(
            step for step in job["steps"] if "scripts/run_native_kvm_openstack.py prove" in step.get("run", "")
        )
        cleanup = next(
            step for step in job["steps"] if "scripts/run_native_kvm_openstack.py cleanup" in step.get("run", "")
        )
        assert prove["timeout-minutes"] == 45 and "--deadline-seconds 2700" in prove["run"]
        assert cleanup["if"] == "always()"
        assert prove["env"]["OS_AUTH_TYPE"] == cleanup["env"]["OS_AUTH_TYPE"] == "v3applicationcredential"
        assert prove["env"]["OS_INTERFACE"] == cleanup["env"]["OS_INTERFACE"] == "public"
        assert "GITHUB_TOKEN" not in prove["env"] and "GITHUB_TOKEN" not in cleanup["env"]
        for step in job["steps"]:
            if step not in (prove, cleanup):
                assert "${{ secrets." not in str(step)
        uploads = [step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4"]
        assert len(uploads) == 1 and uploads[0]["if"] == "always()"
        assert uploads[0]["with"]["if-no-files-found"] == "error"


def test_native_workflows_reject_missing_https_sources_and_evidence(tmp_path) -> None:
    for name, job_id in (("test.yml", "kvm"), ("release.yml", "kvm-proof")):
        job = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))["jobs"][job_id]
        acquisition = next(step["run"] for step in job["steps"] if step.get("name", "").startswith("Acquire pinned"))
        require = next(
            step["run"] for step in job["steps"] if step.get("name") == "Require native proof and cleanup receipts"
        )
        for urls in (("", ""), ("http://example.invalid/kernel", "https://example.invalid/config")):
            environment = {"KERNEL_URL": urls[0], "CONFIG_URL": urls[1], "RUNNER_TEMP": str(tmp_path)}
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-c", acquisition], env=environment, check=False
            )
            assert result.returncode != 0
        missing_evidence = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-c", require],
            env={"RUNNER_TEMP": str(tmp_path)},
            check=False,
        )
        assert missing_evidence.returncode != 0
