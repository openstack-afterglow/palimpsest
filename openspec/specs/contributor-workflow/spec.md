# Contributor workflow specification

## Purpose

This specification migrates the operating obligations previously in root `AGENTS.md` (resume, architecture maintenance, verification, protected material). `AGENTS.md` and `agent.md` are entrypoints, not independent approvals. Consult [`README.md` Development](../../../README.md#development), the dated [`development-handoff.md`](../../../docs/development-handoff.md), [`ARCHITECTURE.md`](../../../ARCHITECTURE.md), affected detailed documents, and the [CI safety/performance specification](../ci-safety-and-performance/spec.md). Historical checkpoints describe their own date and source snapshot, not current server state or new authorization. Reconcile these obligations with the current checkout ref and source before acting; the documented Nova cutover is not permission for another ref, resource or remote action.

## Reference

### Resume and authority boundaries

- Before work, compare the handoff and architecture to actual Git/source state. Inspect `git status --short`, `git diff --stat`, `git diff --cached --stat`, `git log -5 --oneline`; distinguish staged, unstaged, and latest commit. Do not mix earlier unapproved evidence with a new commit. Check branch/ref and exact scope before an action: prior branch-only approval does not authorize `dev`/`main` publication, merge, package release, or shared-resource changes.
- The requested roles are Astra for planning/orchestration/management, Sol for development, with independent review. If that model/review cannot be obtained on the host, report the constraint rather than silently declaring a substitute approved. Review does not grant user authorization for commit, push, SSH, helper execution, or GPU/VM mutation.
- Preserve the handoff's GitHub publication and remote helper transfer blocks until *specific* explicit authorization. A documentation or continuation request is not authorization, and another route to the same blocked outcome is not allowed. Previously approved actions in dated checkpoints authorize only their recorded ref, resources, and scope; do not replay them as standing approval. The private helper transfer/execution is separate even from branch publication. Remote installation/deployment and native/cloud proof require separately specified resources and permission.
- GPU inspection is not GPU allocation. Read-only PCI/IOMMU/driver/in-use inventory is separate from driver unbind/reset, device reassignment, or deleting a failed VM. Before any newly authorized server work, refresh read-only baseline, exact reviewed SHA, ownership, prerequisites, and resource boundaries; do not infer actual GPU passthrough or CUDA success from portable tests, PCI preflight, local KVM, or an earlier CPU-only proof. The selected OpenStack GPU instance booting an OCI rootfs is a design choice, not authorization to manipulate a Nova host or proof that local nested KVM works.
- At handoff, update next steps, evidence SHA, checks actually run and failures, and pending approvals. Do not overwrite older results with new success claims or record secret values. `test-defined`, `test-passed`, and `live-verified` are different claims; a plan, roadmap, historical baseline, dry run, or docs edit does not prove execution.

### Architecture maintenance

- Read root `ARCHITECTURE.md` and affected detailed docs before changing source. For code/config/schema/dependency/deployment/test changes, read reachable source and current tests first; update affected architecture narrative, code map, contracts/limits and detailed docs in the same change. Even when a bugfix/refactor has no structural impact, record the decision and reason in the *latest* `ARCHITECTURE.md` Maintenance review summary. Prefer actual source to an obsolete plan when they disagree.
- Only after real source review, stamp the appropriate working/staged review marker using the procedure in `ARCHITECTURE.md` Maintenance. Do not stamp for a docs-only migration that explicitly forbids stamping. Never copy credentials, passwords, bearer tokens, or private keys into documentation, marker, or logs. Before a normal completion/commit, check working scope with `python3 scripts/check_architecture.py` or staged submission with `python3 scripts/check_architecture.py --staged`; the guard does not generate prose or edit source. A scoped instruction forbidding verification takes precedence for that task; report it rather than claim an unrun check.

### Verification entrypoints and protected material

- During development, run the exact changed node or impacted test lane first. Use `uv run python scripts/test_lanes.py list --check` for the portable manifest, `uv run python scripts/test_lanes.py plan --changed HEAD` for a changed-file plan, `uv run python scripts/test_lanes.py run core-cli` for the core lane, `uv run pytest -q tests/unit/test_architecture_guard.py` for architecture guard regression, and `cd hub && uv run pytest -v` for Hub tests in its separate environment. Native KVM, privileged filesystem, guest binary, BuildKit, and Gate 2 lanes are explicit opt-in proofs with prerequisites in `ARCHITECTURE.md` and `docs/testing.md`; portable pass is not native success.
- Run native/ML/GPU proofs sequentially within their explicit prerequisite and approval scope; never report a portable pass as a live proof.
- `IMPLEMENTATION_PLAN.md`, `docs/oci-docker-hub-compatibility.md`, `docs/oci-public-runtime-roadmap.md`, and `tracking/afterglow-palimpsest.json` are historical/qualification records. Read for context, but neither upgrade their evidence to current implementation nor alter them without explicitly scoped work.

## Historical resume boundary from `agent.md`

The 2026-09-14 PCI preflight checkpoint recorded local implementation, review and selective tests only: no commit/push, server inspection, GPU passthrough or CUDA success was established there. Consult later handoff checkpoints before treating any of those states as current. While remote transfer is blocked, do not retry it through another path; work locally within authorization. After separate approval, inspect the server's current PCI/IOMMU/driver/in-use inventory before deciding any GPU reassignment scope. The chosen long-term topology has the Nova GPU instance itself boot the OCI-root workload, not nested L2 passthrough; local KVM PCI experiments do not grant approval for Nova host manipulation.

## Requirements

### Requirement: Preserve scoped authority
Contributors SHALL preserve the resume and authorization boundaries above.

#### Scenario: Resume with blocked remote work
- **WHEN** a session resumes with a documentation or continuation request
- **THEN** read the entry documents and latest dated checkpoints, compare Git/source state and separate staged/unstaged ownership before choosing local work
- **AND** do not publish, transfer the helper, run native proofs or mutate GPUs/VMs without specific current approval.

#### Scenario: Unpublished candidate and staged user work

- **GIVEN** a `dev` worktree with staged user changes and the 2026-09-27 unpublished 0.2.4 candidate
- **WHEN** a contributor resumes from an older handoff entry
- **THEN** they inspect staged and unstaged state, preserve the existing index, read the newer checkpoint and do not claim publication or new permission.

#### Scenario: Portable evidence without remote approval

- **GIVEN** portable tests or a manual native proof succeeded at a recorded source snapshot
- **WHEN** a contributor prepares a remote helper transfer, GPU reassignment or publication
- **THEN** they retain the separate approval gate and do not reinterpret the prior result as authority or live proof for a new checkout.

### Requirement: Maintain architecture and evidence honestly
Contributors SHALL apply the architecture procedure and retain evidence scope.

#### Scenario: Source changes without new live proof
- **WHEN** source changes but only local checks were run
- **THEN** update affected architecture contracts and the maintenance summary, stamp only after actual review, and record only executed checks
- **AND** retain historical failures and do not promote plans, portable success or CPU-only proof into live GPU or deployment verification.

#### Scenario: Documentation-only migration under an explicit no-stamp instruction

- **GIVEN** contributor guidance moves into these specifications without changing source or workflows
- **WHEN** a migration note is appended to architecture and handoff
- **THEN** the dated source evidence and JSON review marker remain unchanged, and the note does not claim a new test, stamp or remote validation.
