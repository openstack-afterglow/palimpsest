# Palimpsest contributor entrypoint

Read both specs: [contributor workflow](openspec/specs/contributor-workflow/spec.md) (resume, architecture, verification, history) and [CI safeguards, rules 1–12](openspec/specs/ci-safeguards/spec.md) (including owner decisions). [`agent.md`](agent.md) grants no separate approval.

## Required sequence

1. Read [`README.md` Development](README.md#development), dated [`handoff`](docs/development-handoff.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), both specs and affected docs. Check branch/HEAD, staged/unstaged diff and recent commits against current source; separate others’ changes. Dated evidence is not current state.
2. Confirm explicit approval for the exact ref and action. Branch-only publication does not authorize `dev`/`main` merge, further publishing, helper transfer, deployment or shared resources. “Continue,” documentation and reviewer signoff grant none. Report unavailable Astra/Sol/reviewer roles.
3. Start with impacted tests; distinguish test-defined, passed and live/native/GPU proof. Review source before architecture updates; follow marker/check procedure only when applicable. Record actual SHA, failures and approvals in handoff; never copy secrets or turn plans into proof.
4. Before CI edits follow all twelve [safeguards](openspec/specs/ci-safeguards/spec.md): measure, preserve publishing gates and surface unresolved shard-count, publishing and persistent KVM PR conflicts. Never make a skipped proof green without owner decision.

**Remote/GPU safety:** Without specific current authorization, no publication, private helper transfer, installation, new remote/native run or GPU/VM mutation. Read-only PCI/IOMMU/driver checks do not authorize unbind/reset/reassignment or failed-VM deletion. Check exact SHA, ownership, baseline and approved target first; portable or CPU-only proof is not CUDA/passthrough proof. Retain dated handoff evidence.
