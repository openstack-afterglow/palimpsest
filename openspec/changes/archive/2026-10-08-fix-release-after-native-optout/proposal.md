## Why

Release run 37725879373 published the exact v0.3.1 distributions to PyPI, but its GitHub Release job was skipped after the intentionally disabled KVM dependency. The final job has no explicit status function, so GitHub's implicit success gate also considers the skipped dependency.

## What Changes

- Gate GitHub Release creation on a non-cancelled workflow and successful existing PyPI publication, overriding only the unintended implicit skip.
- Retain trusted v-tag, verification, native opt-in, environment and permission gates on publication.
- Add behavior coverage for successful publication after native skip and fail-closed failed/skipped/cancelled publication.
- Complete the missing v0.3.1 GitHub Release with the verified tag-run wheel/sdist, without moving the tag or republishing PyPI.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ci-safety-and-performance`: GitHub Release completion follows successful permitted root publication even when native qualification was explicitly disabled.

## Impact

One Release job condition and its policy regression; architecture/release/install/handoff records. No runtime, package version, dependency, image or native qualification change. Production rollout remains held for project/user service-grade review.
