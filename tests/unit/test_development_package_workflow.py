"""Static safety contract for SHA-specific development package releases."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "development-package.yml"


def test_development_package_workflow_is_sha_specific_and_non_clobbering() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" not in workflow
    assert "      - main\n      - dev\n      - codex/oci-root-phase1\n" in workflow
    assert "python3 scripts/check_architecture.py" in workflow
    assert "scripts/generate_cli_reference.py --check" in workflow
    assert "scripts/test_lanes.py list --check" in workflow
    assert "scripts/test_lanes.py run core-cli" in workflow
    assert 'scripts/build_package.py --out-dir "$RUNNER_TEMP/palimpsest-dist"' in workflow

    publish = workflow.split("  publish:\n", 1)[1]
    assert "    needs: verify\n" in publish
    assert "      contents: write\n" in publish
    assert "package-${{ github.sha }}" in publish
    assert "GH_REPO: ${{ github.repository }}" in publish
    assert "sha256sum --check SHA256SUMS" in publish
    assert 'git/refs"' in publish
    assert '--field "ref=refs/tags/${TAG}"' in publish
    assert '--field "sha=${GITHUB_SHA}"' in publish
    assert 'gh release create "$TAG" dist/*' in publish
    assert "--verify-tag" in publish
    assert "--prerelease" in publish
    assert "--latest=false" in publish
    assert "--clobber" not in publish
    assert "pypa/gh-action-pypi-publish" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.ref == 'refs/heads/dev'" in workflow
    assert "github.ref == 'refs/heads/codex/oci-root-phase1'" in workflow


def test_formal_release_workflow_remains_tag_gated() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'tags: ["v*"]' in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    publish = workflow.split("  publish:\n", 1)[1].split("\n  github-release:\n", 1)[0]
    assert "    needs: [verify, kvm-proof]\n" in publish
    assert "    if: needs.kvm-proof.result == 'success'\n" in publish
