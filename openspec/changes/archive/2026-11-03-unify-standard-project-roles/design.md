## Context

Hub already validates exact original Keystone subjects, current native role-ID graphs and current project membership, and intersects token/current-owner and key/current-owner action decisions. Existing service-role defaults, personal-only package deletion and system-only project build/layer gates prevent standard member workflows. The checkout contains independent WIP and historical release evidence that must remain intact.

## Goals / Non-Goals

**Goals:** One standard current project role model across native Hub APIs, issued-key owners, export workers and capability consumers; reader query/download only; exact ownership, private/project visibility and issued credential ceilings preserved; genuine `openid` federation recognized in both validators.

**Non-Goals:** No live IAM grant/role deletion, native membership provenance rewrite, local CLI sandbox-policy change, schema redesign, provider privilege uplift, deployment or release. No new tag/cache API invented beyond existing surfaces.

## Decisions

- Use current native reader/member/manager/admin roles after the existing exact role-ID graph, including legitimate group/domain/custom inference. Do not hardcode project aliases or add a legacy-root graph veto; legacy-only names without effective core roles fail the core floor.
- Apply a mandatory read/write native ceiling before operator policy evaluation. YAML can narrow actions but cannot uplift reader or absent core authority. Internal capability labels stay stable for API/browser compatibility.
- Carry a hidden request-local original member token in registry actors, revalidating original subject and current roles at existing publication/deletion boundaries. Keys retain stored scope and never gain key administration or metadata deletion. Project build and layer APIs use their semantic project actions; published shared catalog layers still require genuine system authority.
- Admit `openid` as a recognized native human method in ordinary and catalog validators. Existing reduction markers, system assignment proof and protected/service identities remain fail closed.

## Risks / Trade-offs

- Existing service-only requesters lose default access until native core membership is present; no source migration grants it. Obtain renewed subject tokens when their issued core-role ceiling lacks a newly granted role.
- Existing operator rules cannot use `@` to override the reader ceiling, intentionally tightening the administrative contract.
- Broad concurrent WIP and historical proofs are not verification of these bytes. Parent-owned coherent checks/stamp and any authorized deployment remain separate.
