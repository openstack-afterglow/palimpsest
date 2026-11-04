## Why

Native project managers and members must manage their owned Hub resources without dedicated Palimpsest roles, and readers must read nonsecret visible resources. Published 0.3.10 and the deployed 57ab180 cohort do not implement that contract; the approved latest-dev transition also includes the already merged SDK/publication schema cutover.

## What Changes

- Replace service-grade defaults and personal-owner authorization fallback with mandatory current native core-role ceilings, intersected with the original subject and issued key ceiling.
- Follow exact native custom/group/domain implication paths without treating domain-local builtin spellings as global roles; preserve independently verified human system administration.
- Permit original human project writers to publish/cache/delete owned metadata and queue project builds, with original-token revalidation at existing mutation boundaries. Keep global catalog, machine callbacks, private visibility and credential boundaries separate.
- Return an empty executable capability projection for valid reduced credentials that cannot execute human-only native routes, without suppressing original-token validation.
- Prepare synchronized client/Hub/Kolla 0.3.12 and numeric immutable tag support. **BREAKING:** service-only defaults and personal-metadata-only fallback no longer grant resource authority.
- Carry all merged origin/dev 6e4dff7 source, including SDK/publication/storage/native lifecycle changes, as explicitly approved. Do not import uncommitted goal-m1 or primary-checkout work beyond the reviewed authority delta.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `scoped-package-capabilities`: standard native project-role ceilings, exact implications, project management, truthful reduced-credential preview, unchanged key/private/system boundaries.
- `ci-safety-and-performance`: accept numeric release tags alongside historical v tags while retaining exact-version, trusted-ref, artifact/native qualification and publisher gates.

## Impact

Hub auth/policy, legacy/native registry and build routes, authorization regressions, release workflows, synchronized distribution metadata, Kolla image defaults, public contracts and cutover guidance. The authority delta has no schema changes. The approved inherited SDK transition requires stopped writers, compatible context ownership/keys, preserved historical job visibility, paired SQL/CAS backup and isolated restore; MariaDB DDL recovery is not transaction rollback. No publication, remote policy/IAM changes, worker rollout or operational deployment is performed by this change-preparation slice.
