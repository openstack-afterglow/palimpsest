## Why

M51-02 through M51-05 need a product path from verified published payloads to a new runnable combination. The accepted `base-migration-structure-check` capability defines the comparison, admission rules and acknowledgement, but the repository currently implements only the pure comparison. A release revision change and a same-family OS release change must both be inspectable without mutating the original or presenting a structural result as runtime proof.

## What Changes

- Add a Hub-owned, bounded check over the stored, translated SquashFS layers of three exact published `oci-root` manifests. Prove the old payload prefix, compare the kept suffix where both bases can be enumerated, and seal the report with its coverage and declared OS transition before registration.
- Register an accepted result as a new immutable published combination with the replacement base's OS version, its ordered payloads followed by the unchanged kept payloads, and links to its origin, both bases and the sealed report. Keep the original and its consumers bound to their existing identities.
- Use one risk policy on CLI, Hub API and dashboard. Keep inherited non-clean risk even when a re-migration's comparison is clean. Require dedicated acknowledgement at registration and for every fresh run or resume of an effectively non-clean result. Refuse no-op base replacement, non-clean extensions and automatic restart of non-clean workloads. Keep ordinary authorized payload reuse and existing authority, integrity, quota and support checks.
- Show coverage limits, package-state conflicts, the user's responsibility for runtime verification and both the new and original combination identities. Provide an authorized way back to the original without retagging or rollback.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `base-migration-structure-check`: add requirements for artifact-backed inspection, exact report binding, immutable publication and consumer admission. Amend the accepted verdict and acknowledgement requirements to distinguish a pure comparison from the sealed effective gate. The pure report schema and field values do not change.

## Impact

Future implementation touches Hub publication, storage inspection, supported instance admission, SDK/CLI, and the dashboard launch flow. Local execution needs a measured path that consumes the exact published lowers or proves equality before launch; current OCI source-closure re-materialization is not that proof. The [design](design.md) maps these gaps and isolates the owner decisions.

Design only under `spec.md:19` while the M4 dependency is blocked. No code, release, deployment, live verification, operating authorization, or checklist transition is included. Application-mode cloud disk bases, arbitrary layer composition, package repair, mandatory boot testing, and automatic runtime-compatibility claims stay out of scope.
