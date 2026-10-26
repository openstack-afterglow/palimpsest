# Native catalog and bundle-unpack verification — 2026-10-25

## Source and authority boundary

Shared `/Users/pieroot/code/palimpsest` remains dirty `dev` based on `aa397a0fd976e35302974a347748a9773e3084bf`. Initial and post-format/pre-archive checkout scope: **6 staged, 124 unstaged, 64 untracked entries** using identical default short-status counting. The staged diff remained six files, 855 insertions/66 deletions. Subsequent archive adds the explicitly requested main capability spec and relocates its change artifacts; those intentional new documentation paths are not included in that preservation checkpoint. Existing unrelated source/index/config/state is preserved. No deployment, publication, commit, schema/IAM or operational database changes.

Qualification snapshot: `/var/folders/1m/xfc726k14szbjnrlq5t68zjw0000gn/T/afterglow-palimpsest-dashboard-6m5hqfl9/palimpsest`.
Evidence root: `/Users/pieroot/.local/share/afterglow-proofs/palimpsest-asset-dashboard-20261025/`.

## Executed evidence

| Evidence | Result | Exact limitation |
| --- | --- | --- |
| `native-hub-final.log` | **710 passed, 2 skipped**, 268.36s | Complete separate Hub environment before final formatting; includes real loopback Keystone/Hub HTTP auth and actual SQL/router catalog/package tests. Skips are not qualified native cases. |
| `native-cli-reviewed.log` | **54 passed**, 1.43s | Focused CLI/layout/unpack baseline before final formatting. |
| `native-core-cli-root-env.log` | **2046 passed, 2 skipped, 27 failed**, 114.44s; 2075 selected | Ansible was installed, but root .venv/bin was not on PATH, so every failure is FileNotFoundError for ansible-playbook. Original failure retained; not renamed a pass. Earlier native-core-cli.log used the wrong Hub environment without Jinja2 and is not core proof. |
| `native-kolla-missing-27-real-ansible.log` | **27 passed**, 18.30s | Only original failed parametrizations, unchanged Kolla contracts, genuine existing root `.venv/bin/ansible-playbook` Core2.19.14/Python3.11.15/Jinja3.1.6. No fake binary or operator configuration. Combined core evidence is **2073 passed/2 skipped across two runs**, not one green full rerun. |
| `native-catalog-postformat.log` | **22 passed**, 2.54s | Actual canonical Hub source and Hub venv after final fixture/import/format corrections. |
| `native-unpack-postformat.log` | **29 passed**, 0.42s | Actual canonical source and root venv after final formatting. |
| `native-final-scoped-checks-postformat.log` | All commands exit0 | Actual source `bundle unpack --help`; CLI reference current; every test classified with exact/disjoint mixed nodes; Ruff lint and format check on11 explicit files. Does not claim repository-wide lint. |
| `local-cli-positive.log` | Actual native CLI extraction and runtime-stack resolution | One qcow2 x86_64 base plus two ordered layers, no manual layer selection. Synthetic payloads; **no hypervisor/guest execution**. |
| `native-scoped-architecture-selected-index.log` | Staged guard stamp/check passed; **17 source files** | Alternate index was initially absent and includes only17 explicit integration files plus architecture document. Not a whole dirty checkout review. |
| `native-final-source-scope.json` | Every selected snapshot file matches canonical bytes | Exact per-file hashes and scoped guard digest, not a release/source-tree identity. |
| `native-final-shared-cli-contracts.log` | **313 passed**, 5.24s | Final canonical CLI contract/reference/lane regression files after formatting, root venv. |
| `native-final-hub-toolchain-checks.log` | Lint and format check exit0 | Canonical five Hub catalog files, run from hub/ with its own venv/config. |
| `native-real-ansible-environment.log` | Real installed Core2.19.14 | Version/environment receipt; original failure cause was PATH, not absence of installation. |

## Final style correction and review

Initial final scoped check (`native-final-scoped-checks.log`) found import/style issues, retained as original evidence. `native-scoped-format-corrections.log` names every autofix/format target explicitly: CLI, catalog DTO/service/fixture/test, lane manifest and unpack test. No unscoped `ruff check --fix .` ran. Fixture SIM117 merges nested async contexts without changing lifetime/order; other corrections are formatting/import layout only. No material functional bug was found. Post-format focused tests above qualify those final canonical bytes; the full710/54/core counts remain clearly identified as the pre-format baseline, not a newly repeated whole suite.

**Non-owned hunk restoration:** Whole-file formatting temporarily touched preexisting Nova arguments/validation/import/print in `src/palimpsest_local/cli.py` and build-registry/benchmark layout in `scripts/test_lanes.py`. Every such whitespace/import-layout delta was recovered from the original formatter/import diagnostic log and surgically restored, returning the initial source snapshot tags1F82/1D64 exactly. No HEAD/checkout/reset or whole-file replacement was used. No unrelated formatting delta remains. Final CLI contract/reference/lane/unpack regressions passed **342cases22.58s**, actual help/reference/lane and9owned-file Ruff checks passed (`native-final-restored-source-checks.log`). Known preexisting CLI/lane formatting/import debt is intentionally excluded from the final style gate; source fingerprint still includes both files. Earlier11file/313case checks describe the temporary formatted state, not final source acceptance.


Reviewed catalog DTO/router/SQL graph and bounded candidate paging, absolute public/site/shared/private admin visibility, fresh human-system context and search/cursor integration; unpack parser/handler, canonical marker checks, atomic digest verification and automatic base/layer runtime resolution; fixtures, reference and lane ownership.

Final restored-source architecture digest: `bc918dbd56e96a552458345d7f6c2c9a4852bb94213bcf3d17846f5d78c32431` (**17files**, `native-scoped-architecture-restored-source.log`). GIT_INDEX_FILE is the evidence-root native-catalog-unpack-only.index. Reproduce from the isolated snapshot with that environment: root Python scripts/check_architecture.py --staged. Every scoped snapshot file matches final canonical bytes in native-final-source-scope.json. Earlier formatted-byte76b30155digest is superseded. Initial native-scoped-architecture.log inherited507sourcefiles and is retained but excluded. Shared repository index/review marker never stamped or staged.

## Behavioral and release boundaries

- Candidate pagination validates at most limit candidates plus one SQL lookahead, follows advancing next_offset even for empty items and does not refill/rescan. Native depth64 overrun explicitly fails413.
- Local metadata eligibility requires a visible raw/qcow2 base and known matching x86_64/aarch64 architecture across base/layers. No-base complete reusable chains remain discoverable but not locally runnable. Content size includes base/layers, not archive metadata.
- Consumer public/site/project/shared visibility and independent download authority stay separate from absolute global-admin labels and fresh native human system authority. Missing SQL/auth/provider authority never becomes a successful empty inventory.
- `bundle unpack` is **working-source-only**, not asserted present in published0.3.5. Help was exercised against actual source. New catalog endpoints require a matching reviewed Hub rollout; none occurred.
- Synthetic identity, SQLite/CAS bytes, archive resolution and metadata eligibility do not establish genuine filesystem payloads, compatible caller backend, VM startup, cloud execution or runtime readiness.
- This closes native source acceptance only. Parent owns Afterglow gate/browser acceptance. Independent release approval, publication, Hub rollout and real runtime/VM qualification remain separate, unperformed boundaries.

## OpenSpec closure

`openspec validate add-dashboard-asset-catalog --strict` passed. After all tasks completed and final native evidence above existed, `openspec archive add-dashboard-asset-catalog --yes` succeeded **without skip-specs/no-validate**: created `openspec/specs/dashboard-asset-catalog/spec.md`, synchronized six added requirements, and archived the change at `openspec/changes/archive/2026-10-25-add-dashboard-asset-catalog/`. This is local source/document closure, not release or rollout.

