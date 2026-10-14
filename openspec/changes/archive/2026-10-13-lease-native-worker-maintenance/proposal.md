## Why

Build-worker startup currently treats every SQL `building` row as interrupted while its flock protects only one filesystem lock path. Independent workers sharing SQL can therefore terminalize live claims and delete their private outputs; export maintenance also removes scratch by age without consulting ownership.

## What Changes

- Keep transactional `FOR UPDATE SKIP LOCKED` build/export claims as job distribution, adding renewable build attempt ownership and fenced recovery/cleanup.
- Record the execution node and retain local KVM/process cleanup as a node-local role; remote expiry reconciliation never repeats a recipe or signals another host's guest.
- Lease shared export maintenance/delegation roles through native SQL, renew their ownership, fence un-tokenized effects, and compare ownership on cleanup completion/release.
- Add behavior consumers and an opt-in shared-MariaDB/local-process smoke hook with independent flocks and synthetic guest/provider boundaries.
- **BREAKING (worker rollout):** stop all old API/workers and bootstrap ownership columns/table before starting the new workers. No mixed-version recovery topology is supported.

## Capabilities

### New Capabilities
- `native-worker-ownership`: shared Hub job/recovery/maintenance ownership with local guest isolation and honest proof boundaries.

### Modified Capabilities
None. Caller/project capabilities and requester Trust delegation policy are unchanged.

## Impact

Hub build/export services and entrypoints, SQL models/bootstrap, focused Hub tests, native OpenSpec and architecture/testing/operation documentation. No runtime-engine, local CLI, live checkout, cloud creation policy, provider unknown-intent recovery or generic scheduler dependency changes. Shared artifact storage and digest locks remain required; this does not claim arbitrary object-store locking support or actual guest-build proof.
