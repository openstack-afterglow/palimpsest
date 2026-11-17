## Why

Owner form 13 requires administrators to raise an ordinary user's or project's publication quota without changing billing tiers or every subject's policy. Existing free/paid tier assignment and global policy updates cannot express that isolated change. Plan section 10.2 names this addition M4-16; source completion does not complete its release or live-proof requirements.

## What Changes

- Add visibility-specific overrides of all three limits (`max_images`, `max_chain_bytes`, `max_source_bytes`) for an exact user (private/public) or project (internal), including free-tier subjects.
- Add one independent append-only override/audit table. Every set/removal records admin, UTC time and old/new override values; removing restores current tier inheritance.
- Resolve the same effective limits in publication enforcement and both subject-own/admin usage views; expose `source: tier|override` per scope.
- Add `system:manage` API set/remove/history, SDK methods and `palimpsest admin image-quota set|remove|usage|history`, plus `published quota` for own usage. Preserve existing authentication and credential fences.
- Preserve existing publications when limits fall, unchanged tier/seed rows and all original quota accounting semantics. An absent override keeps the original tier admission outcomes and error bodies.
- Route the additive schema through offline bootstrap/migration with writer drain and paired SQL/CAS/config backup. App-only rollback can retain the table, but old code ignores overrides.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `scoped-package-capabilities`: add audited per-subject published-image quota overrides behind current human system administration. The accepted specification remains unchanged until this change is archived; this directory contains the added requirement delta.

## Impact

Hub image models, quota enforcement/usage, admin routes, bootstrap metadata and offline migration; SDK/CLI, tests, generated CLI reference, architecture and installation guidance. No version bump, global policy/seed rewrite, image deletion, instance/session/lifecycle/trust change, push, release, deployment or production quota mutation.

Before merge, Main must obtain independent rulings for the `spec.md` conditions at origin/dev `19761a0`: M2-17 line 550 names HubClient/CLI published/Hub quota and requires correspondence reapplication; M3-08 line 617 explicitly names `image_quota`. The older M3-08 publication-path condition at line 622 and M2-09..14/H4 also need the impact comparison. This proposal does not preserve or clear checklist checks by itself. `spec.md` and `docs/development-handoff.md` remain parent-owned and untouched.
