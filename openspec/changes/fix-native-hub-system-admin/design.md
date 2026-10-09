## Context

Hub validates original project-scoped subject tokens with a separate system reader. The privileged build/GC path checks the subject user's separate system role assignment. Production's effective expansion returns project assignments while omitting a directly assigned system admin; the direct query returns the assignment.

## Goals / Non-Goals

Recognize and freshly revoke directly assigned system administrators without changing subject scope, package capability graphs or validator authority. No tenant grants, credential fallback, schema/endpoint change, KVM activation or multi-node local CAS.

## Decisions

Remove only `effective=True` from the existing `user=USER, system="all", include_names=True` lookup. Keep exact user ID, system-all scope and named-admin response checks and fail-closed failure handling. Effective membership checks for ordinary package owners remain unchanged.

Use the existing synthetic Keystone HTTP fixture with the installed client to reproduce the observed distinction. The same original token loses administrator recognition immediately when its direct assignment is removed, despite retaining a project admin role.

Synchronize root/Hub versions, locks and packaged Kolla tag to 0.3.2; retain the separately pinned historical source-build ref. Release and deploy only within the owner's explicit 0.3.2 approval.

## Risks / Trade-offs

This repairs direct user assignments; it does not claim new support for inherited system group authority. The reader must have native token/directory policy, and unavailable validation remains denied. Publication/native CI policy is unchanged; deliberately skipped KVM is not qualification.

## Migration Plan

No SQL migration or caller change. Qualify frozen stopped backups and exact images, preserve controller1-only storage, configure protected IDs and a managed reader secret, then execute genconfig/pull/prechecks/reconfigure exclusively from the deployment server. Use the captured exact old operator/config/image/database state if authorized rollback becomes necessary.

## Open Questions

None for the single-query fix. Production reader-policy and canonical cutover acceptance are deployment checks, not assumed from source tests.
