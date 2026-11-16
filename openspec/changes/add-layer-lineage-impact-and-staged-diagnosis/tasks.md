## 1. Pure contracts

- [x] 1.1 `compatibility_stages.diagnose`: five stages, `passed` / `failed` / `unverified`, no overall verdict; a `base_migration.check` report maps to the filesystem stage; malformed input stays `unverified`.
- [x] 1.2 `lineage.impact_plan`: recorded descendants of a layer or base image, existing lineage kept, four separate new-lineage states (build cache and pack cache apart), `unreached` layers reported, bounded and cycle-safe.
- [x] 1.3 Unit tests for both (`tests/unit/test_lineage.py`).

## 2. Local surface

- [x] 2.1 `inventory.lineage_impact`: read-only reader that builds the records from stored artifact metadata, build records, run ledgers and retained-instance references.
- [x] 2.2 `palimpsest layer impact REFERENCE [--replacement REFERENCE] [--json]` and its CLI reference entry.
- [x] 2.3 Store-level tests: descendants through real metadata, recipe digest recorded / not recorded, runtime pack, bound consumers, tag and digest resolve alike, unreached layers, store unchanged after the call.
- [x] 2.4 Independent review findings applied (not an Astra/Sol signoff): replacement descending from the replaced layer fails structure; unknown base or incomplete chain is `unverified`; runtime-pack reconversion; conflict in an incomplete check fails the filesystem stage.

## 3. Not in this change (each needs its own approval)

- [ ] 3.1 Listing adapter that produces `base_migration.check` input from stored layers, so the filesystem stage has real evidence (M51-02).
- [ ] 3.2 Risk acknowledgement gate on CLI, Hub API and dashboard (M51-03, M51-04).
- [ ] 3.3 Hub descendant / consumer lookup endpoint with the existing visibility rules.
- [ ] 3.4 Declared `provides` / `requires` / `conflicts` metadata.
- [ ] 3.5 Composition lock.
- [ ] 3.6 Smoke-test result record bound to a composition identity.
