## Context

Run 37725879373 has verify=success, kvm-proof=skipped under explicit owner opt-out, publish=success, github-release=skipped. The final job depends on publish but has no status-function condition. Root package hashes in PyPI match the exact tagged run distributions.

## Goals / Non-Goals

**Goals:** Complete GitHub release creation after permitted successful root publication; retain denial on cancellation or unsuccessful publication; repair the missing existing release without changing its source or bytes.

**Non-Goals:** Native qualification, broader publication permissions, new versions, tag movement, Hub wheel publishing, dependency/runtime changes or production deployment.

## Decisions

Use `!cancelled() && needs.publish.result == 'success'` on the final GitHub job. An explicit status function prevents unintended skip propagation, while successful publish remains the sole authority delegated from the existing trusted/tag/verify/native/environment policy. `always()` alone would permit cancelled or unsuccessful publication and is rejected.

Reuse the existing restricted GitHub-condition evaluator to exercise real YAML conditions over successful/skipped native ancestry and unsuccessful/cancelled publisher outcomes. Manually attach only the exact tag-run distributions to v0.3.1 after hash parity with PyPI; do not rerun PyPI or use a development prerelease build.

## Risks / Trade-offs

The corrected final job will be used by future tags; the immutable old tag retains its old workflow. Existing tag source and PyPI artifacts remain untouched. The local evaluator covers the repo's expression subset, not GitHub infrastructure; the recorded failed hosted run and actual GitHub Release/asset completion are separate runtime evidence. No speed improvement or native success is claimed.
