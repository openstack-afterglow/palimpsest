## Why

The owner reports that repeated protected native KVM approval waits block CI/CD and explicitly authorizes automatic approval or disabling these attempts. Read-only inspection also finds both pinned kernel/config HTTPS URL variables absent, so automatic approval alone cannot make the current native job succeed; choose explicit disablement instead.

## What Changes

- Set the repository variable `PALIMPSEST_KVM_ENABLED=false` after validating the workflow cutover.
- Keep native jobs opt-in (`true`) with their existing trusted-ref allowlist, environment protection, secrets, pinned inputs, receipts and exact-owned cleanup.
- Make the Test native policy verdict accept only enabled/success or explicitly disabled/skipped. Name it `Native KVM qualification policy` and state that a disabled verdict is not native qualification.
- Allow tag publication only after `verify=success` and the same two-state policy, with explicit cancellation and trusted repository/event/ref guards so a skipped native dependency cannot skip permitted publication or mask a failed verification.
- Preserve the six strict hosted required checks and all non-native proofs. Do not publish a release, create a native VM, change environment protection or mutate production as verification.
- Update behavioral gating tests, current architecture/testing/operator guidance and the CI safety specification. Preserve dated failure evidence and concurrent work in the shared checkout.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `ci-safety-and-performance`: explicit owner-authorized native opt-out without claiming proof, while retaining fail-closed enabled qualification and publication verification.

## Impact

Changes are confined to Palimpsest Test/Release native policy, its behavioral workflow contracts, documentation and the repository opt-in variable. No runtime/API/package/version/credential/resource contract changes. Implement on isolated latest `origin/dev`, follow its effective strict PR rules, and leave `dev → main` merging to the owner. An older workflow on main or an existing tag does not gain the new policy merely from the variable write.

Before/after evidence must label run population and sample size; no projected speedup or forced runs to pad a sample. The actual disabled trusted-dev push must demonstrate native skip, an honest policy verdict, and no pending native environment deployment. Tag publication conditions are exercised locally without minting or publishing a release.

Before measurements (20 completed non-cancelled runs,10 push/10 PR, historical mixed native boundaries): critical-path median177s/p90 nearest-rank60072s; push163/84054s and PR187.5/295s. Rerun36169450623 is measured from its latest startedAt (167s), not original creation (579s). Native execution n14=139/140s; valid native wait n13=72/84036s, preserving the negative prior-attempt wait as excluded raw evidence. No sufficient after population or speedup claim.
