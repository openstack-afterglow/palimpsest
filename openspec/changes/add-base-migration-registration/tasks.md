## 1. Owner decisions and contract review

- [ ] 1.1 Confirm managed launch lanes and OS pairs, runtime configuration, provenance/report audiences, unavailable inspection, lineage and no-op policy, and non-clean automatic restart rules. Keep the M4 dependency and GATE explicit.
- [ ] 1.2 Review the source/caller map against the exact implementation base and assign the Hub, CLI/SDK, local runtime and dashboard boundaries. Verify every supported selector binds the same immutable published identity.

## 2. Stored payload inspection design (M51-02)

- [ ] 2.1 Specify the authorized digest-and-size-pinned storage reader and isolated SquashFS inspection limits, including subject/project/deployment admission, exact-pin reuse, expiration and bounded report storage. Define stored-artifact fixtures for roots, parents, names, control markers, special kinds, hard links, contents and xattrs.
- [ ] 2.2 Specify independent root admission, topology and identity coverage, and canonical report enrichment. Define same-release, release-change, conflicting, and partial/unavailable inspection cases. Include partial base and kept topologies that would manufacture findings if consumed, while checking honest counts and preserving original occurrence order. Separate hard refusals from incomplete results.

## 3. Immutable registration and reuse design (M51-02, M51-05)

- [ ] 3.1 Review the immutable manifest and server-owned source binding, preview seal and exact registration pin, audit schema, publication transaction and replay fingerprint. Cover time-limited report registration without reinspection, cross-scope and stale preview refusal, same-request replay after preview expiry, and original preservation.
- [ ] 3.2 Define authorized report projections and original reuse links. Plan a read/pull/run proof for the unchanged original and verify pre-existing consumers still resolve their original digest.

## 4. Risk gate and result presentation design (M51-03 through M51-05)

- [ ] 4.1 Specify matching CLI, Hub API and dashboard acknowledgement semantics for registration, fresh run/create/start/resume and first-launch continuation. Cover exact replay, local pull/launch custody and guest restart policy.
- [ ] 4.2 Specify the parity and denial matrix: clean/conflict/incomplete, strict acknowledgement, stale binding, generic confirmation, forged metadata, legacy caller-written key, non-clean extension refusal, and ordinary authorized digest reuse. Cover same-manifest and same-payload refusal, incorrect recorded base pin, and a clean re-migration that retains both inherited non-clean risk and prior report links. Require fresh resume intent and test omitted versus explicit automatic restart. Verify authority, ownership, visibility, integrity, quota, OS/platform and operator checks; no allocation on refusal; exact ordered runtime payloads on acceptance.
- [ ] 4.3 Review release changes, separate comparison and effective risk verdicts, inherited risk, package-state counts and coverage. Verify a false pure acknowledgement flag or `PASSED` filesystem stage cannot be shown as gate clearance. Show user responsibility for runtime verification even when both verdicts are clean.

## 5. Evidence plan

- [ ] 5.1 Define portable contract and storage fixtures, independent source review, scoped runtime qualification and mixed-version rollback/roll-forward criteria. Cover verified ordinary rows created during rollback, inert legacy labels and refusal to reclassify an existing managed row with missing provenance. Do not apply GATE or change roadmap checkboxes based on design or portable tests alone.
