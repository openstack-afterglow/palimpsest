## Context

Remote dev 6e4dff7 includes the merged unpublished 0.3.11 SDK/publication lifecycle. The deployed 57ab180 cohort and published v0.3.10 differ from that source. Primary, SDK-release and goal-m1 worktrees are concurrent and remain untouched; preparation occurs on an isolated dev clone. Parent owns publication and live operations.

## Goals / Non-Goals

**Goals:** Current native manager/member/admin management; reader nonsecret visible reads; exact role-ID custom/group/domain implications; original subject/current owner/key intersection; truthful reduced-credential projection; complete 0.3.12 release and latest-dev migration prerequisites.

**Non-Goals:** No native IAM changes, invented delegation grants, key escalation, private/global boundary relaxation, uncommitted goal-m1 intake, production mutation, new GPU/native campaign or automatic schema rollback.

## Decisions

- Evaluate existing oslo.policy actions only after a mandatory core-role ceiling. Unlike service-grade aliases or personal metadata fallbacks, this is a single native contract and YAML cannot uplift a reader.
- Traverse native implication edges by exact IDs, including domain intermediates; emit only uniquely bound global role names. A domain role named admin is not itself global admin. Existing exact-system raw grants and human credential restrictions still decide system authority.
- Retain request-local original member tokens for mutation revalidation; stored keys keep their issue-time action/package ceiling. Never mint broader delegated roles. Deferred export uses only configured least roles currently held through the native graph; manager-only means a manager token, not a fabricated member assignment.
- Keep reduced credentials denied by human resource dependencies. A valid original reduced token may query the policy projection, which returns no executable human actions; invalid/revoked originals still fail validation.
- Base 0.3.12 on latest remote dev as explicitly approved, not an older deployed branch. Skip the reserved unpublished 0.3.11 numerical slot without moving a tag. Preserve the merged SDK/publication source while excluding separate dirty worktrees.
- Numeric X.Y.Z release triggers coexist with historical v tags. Before artifact creation require exact client/Hub/tag version equality; trusted repository/push, verify/native policy and least-privilege publishers remain mandatory.

## Risks / Trade-offs

- Service-only requesters without effective core authority lose defaults; renew original tokens when their issue ceiling is narrower than current membership. No migration grants roles.
- Current manager-to-member native implications permit bounded export delegation; a deployment without that implication and no independently possessed member role must fail rather than escalate.
- Latest-dev schema changes require all-writer stop, paired SQL/CAS backup and an isolated restore. Unknown context creators fail before DDL. Legacy jobs become internal execution receipts, not fabricated published images. MariaDB DDL implicitly commits; rollback requires stopped writers and paired old SQL/CAS plus exact old images/config.
- Dedicated HTTPS build-worker claims/journals are a separate cohort from controller API/export workers. Stop/drain that worker while the old API is available; preserve uncertain claims and never reset leases to make rollout pass.
- Source tests and synthetic HTTP do not establish production IAM, Ceph namespace isolation, guest readiness or published multiarchitecture images. Parent-owned operational receipts must remain separate.
