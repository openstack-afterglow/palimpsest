## 1. Native ownership storage

- [x] 1.1 Add native SQL role leases and build expiry/execution-node columns.
- [x] 1.2 Add repeatable offline bootstrap upgrades and legacy-row preservation consumer.

## 2. Build lifecycle

- [x] 2.1 Fence build attempts, renewal, publication and cleanup with SQL ownership.
- [x] 2.2 Reconcile expired claims without recipe replay and preserve node-local teardown authority.
- [x] 2.3 Add live-startup, expiry, stale-owner and cleanup behavior consumers.

## 3. Export maintenance

- [x] 3.1 Add renewable token-owned native SQL maintenance roles and effect fences.
- [x] 3.2 Fence delegation cleanup and preserve live/ambiguous export scratch.
- [x] 3.3 Add competing-role, slow-delete, stale-completion and scratch consumers.

## 4. Handoff

- [x] 4.1 Add disposable-MariaDB/local-process ownership smoke hook with independent flock paths and synthetic execution boundaries.
- [x] 4.2 Update architecture, operator/testing documentation and changelog with exact selectors and rollout/proof boundaries.

Verification record — 2026-10-14: the implementation slice ran no checks. Subsequent parent verification on a frozen clone of the current working tree passed focused Hub **93 passed, 2 skipped**, fresh-empty-MariaDB multiprocess smoke **2 passed** (earlier pre-lint run also **2 passed**), and full Hub **378 passed, 2 skipped**. The initial 16 timestamp failures were fixed with SQL-side synchronization, UTC normalization and five regressions. Eight lease-change files were formatted; only `tests/test_auth.py` I001 and seven unformatted credential-isolation files remain outside this change, blocking whole-Hub lint/format gates. Results are locally test-passed, not live-verified: KVM guest builds, live Glance/provider deletion, production and mixed-version rollout were not exercised. See [testing evidence and boundaries](../../../docs/testing.md#native-hub-worker-ownership); parent isolated-clone architecture stamping is recorded separately, with the shared marker unchanged.
