## Context

Base origin/dev `19761a0` stores global free/paid visibility policies and exact subject tier assignments. `register_image` checks quota after request replay while holding the global image mutex. Existing admin policy/tier updates use the same mutex. Offline bootstrap registers SQLAlchemy image metadata and runs `create_all`; offline data migration validates the original quota-table pair.

## Goals / Non-Goals

Goals: isolated user/project limits, reversible inheritance, complete attributed override history, one enforce/usage decision path and supported API/SDK/CLI controls.

Non-goals: billing changes, altered tier seeds, publication or resource deletion, new instance projections, lifecycle/trust changes, automatic request replay or live rollout in this source change.

## Decisions

- Use one append-only `palimpsest_hub_image_quota_overrides` table keyed by `(subject_kind, subject_id, visibility, revision)`. Latest `new_limits` is effective. A null object means inherit; null values inside an object mean unlimited.
- Record `old_limits` as the prior override state, not a historical snapshot of tier policy. Retain every set/removal, including repeats. `updated_by` and UTC `updated_at` identify each action.
- Use byte-exact subject lookups. Return a definite 409 for case-only key collisions under case-insensitive database collations.
- Share the publication mutex for setting/removing overrides. The existing tier check/insert body and count/chain/source error details remain unchanged after policy selection.
- Resolve effective policy centrally for enforce and usage. With no override row, its three limit values equal the original tier policy. A differential regression compares actual admission outcomes and serialized refusal bodies to the original tier lookup across both tiers and every visibility. This does not claim identical SQL traffic: the new table read remains required.
- Keep current project-token human `require_admin` / `system:manage` gates. No scope or delegation exception is added.
- Bootstrap adds an independent table without foreign keys into existing rows. Offline migration accepts old sources without it and validates complete revision chains. MariaDB reflects JSON aliases as LONGTEXT, so decode before validation and bind according to the destination's reflected column type.
- Leave the accepted OpenSpec capability untouched until archive. Add the new requirement through this active change delta.

## Rollout and rollback

Drain every writer, take the paired SQL/CAS/config backup, then run the reviewed new API image's offline bootstrap before any new app reads the table. Roll all app cohorts together. Existing policy/tier/image rows remain unchanged. Old applications can leave the extra table present during app-only rollback but enforce tier-only limits; reconcile admission before reopening ingress. Preserve audit state. A data restore still requires separately approved paired recovery.

## Evidence and merge gates

Local checks and the SHA-bound source review are recorded in the task handoff; they are not actual MariaDB migration or live M4-16 proof. The release, canonical rollout, temporary-member set/read/403/remove/audit demonstration and independent checklist ruling remain outside this source slice.

At base `19761a0`, `spec.md:550` requires M2-17 correspondence reapplication and `spec.md:617` explicitly names the changed `image_quota` module in M3-08's revert condition. `spec.md:622` retains the older publication-path condition. M2-09..14 and H4 must be assessed against preserved accounting, unchanged default policies and additive override behavior. Main owns those independent rulings and any checkbox changes before merge; this change makes none.
