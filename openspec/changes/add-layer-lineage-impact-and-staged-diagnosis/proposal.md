## Why

A layer names its parent and its base by digest, so replacing a parent never changes an existing child. What is missing is the view a user needs before choosing a new parent: which recorded layers descend from the old one, what a lineage on the new parent would require, and which compatibility questions nothing has answered. Today the only compatibility result is one platform-metadata status (`compatible` / `incompatible` / `unknown`), which is easy to read as more than it is, and there is no descendant listing at all (the Hub exposes ancestors and direct children only).

## What Changes

- Add a read-only **lineage impact plan** over recorded local metadata: every descendant of a layer or base image, nearest first, with the existing lineage reported as kept and the four actions of a new lineage (rebuild, reconversion, transfer, deployment) reported as separate states. The plan builds, converts, transfers and deploys nothing.
- Add a **staged compatibility diagnosis** with five ordered stages (structure, filesystem, distribution/ABI, kernel/device, smoke test). Each stage reports only what was evaluated; everything else is `unverified` with the reason. There is no overall "compatible" verdict.
- Expose the plan as `palimpsest layer impact REFERENCE [--replacement REFERENCE] [--json]`.
- Reuse the base-migration structure check (`base-migration-structure-check`) as the evidence for the filesystem stage; no second comparison is introduced.
- Record two reviews without implementing them: declared `provides` / `requires` / `conflicts` metadata, and a composition lock. Both need a metadata schema decision and are listed under "Needs approval" in the design.
- No API, database schema, authorization or trust-model change. No Hub change. Existing commands and their output are unchanged.

## Capabilities

### New Capabilities
- `layer-lineage-impact`: descendant listing and per-action states for a parent or base replacement, from recorded local metadata, without side effects.
- `staged-compatibility-diagnosis`: per-stage results with explicit `unverified`, composed from the existing platform check and the base-migration structure check.

### Modified Capabilities
None. `base-migration-structure-check` requirements are unchanged; its report is consumed as-is.

## Impact

Client only: new `src/palimpsest_local/lineage.py` and `compatibility_stages.py`, one read-only function in `inventory.py`, one subcommand in `cli.py`, unit tests, CLI reference. Spec checklist items M5.1 and M5.5 are the consumers; this change does not complete any of them. Hub reverse-lookup, acknowledgement gates, declared-dependency metadata and a composition lock are out of scope and need separate approval.
