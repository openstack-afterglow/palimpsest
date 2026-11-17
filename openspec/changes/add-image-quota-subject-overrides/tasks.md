## 1. Product implementation

- [x] 1.1 Add exact user/private-public and project/internal overrides for all three limits without changing tier policies or seeds.
- [x] 1.2 Record every set/removal in an append-only admin/time/old/new ledger; removing restores current tier inheritance.
- [x] 1.3 Apply effective limits in the actual publication enforce hook and both usage views; expose tier/override source without deleting existing images.
- [x] 1.4 Add bounded human-system-admin API controls/history and supported SDK/CLI methods without automatic mutation retries.
- [x] 1.5 Register the additive table for offline bootstrap and preserve/validate migration history, including MariaDB JSON-as-LONGTEXT reflection.

## 2. Source qualification

- [x] 2.1 Add consumer regressions for enforcement, subject/scope independence, lowering/removal, audit, auth, validation and publication mutex ordering.
- [x] 2.2 Run relevant Hub/root tests, Ruff lint/format, CLI reference generation/check and architecture review/stamp/guard.
- [x] 2.3 Obtain independent source review and fix the MariaDB migration P2 (reviewed head `af03daa939fe7e583517cf7f807044e78bf24a15`, no open P0–P2).
- [x] 2.4 Complete the supplementary no-override byte-for-byte admission regression and package this addition as an active OpenSpec delta, leaving the accepted spec unchanged.
- [ ] 2.5 Obtain Main's independent pre-merge checklist rulings for M2-17, M3-08 and M2 quota/H4 impact; preserve or uncheck evidence as ruled.

## 3. Operational completion (outside this source slice)

- [ ] 3.1 Publish the reviewed immutable release and perform the canonical, backup-gated bootstrap/rollout.
- [ ] 3.2 Demonstrate the approved temporary free-tier member's usage, admin override, own/admin source, non-admin denial, removal and attributed history on the deployed release.
- [ ] 3.3 Obtain the independent M4-16 checklist ruling; do not substitute local unit results for live proof.
