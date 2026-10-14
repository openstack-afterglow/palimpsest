## Context

Hub build startup calls interrupted recovery after acquiring a store-path flock. That flock does not coordinate workers with distinct local lock paths. Recovery reads all `building` rows and scans shared private trees. Export jobs already use SQL owner/expiry claims, but age-based scratch sweeping and unfenced Trust-cleanup completion leave shared maintenance gaps.

## Goals / Non-Goals

**Goals:** preserve live jobs under independent worker startup; reconcile expired jobs without repeating a recipe; fence shared effects and late completion; retain node-local guest isolation; provide executable consumers and a disposable-SQL/multiprocess ownership hook.

**Non-Goals:** a generic scheduler, provider create retries, unknown-intent replay, resource-isolation changes, runtime-engine/local-CLI changes, arbitrary artifact backends, production rollout or actual KVM build qualification.

## Decisions


Guest process launch is itself an accepted mutation: preparation alone is not a launch fence. Launch runs under a freshly checked SQL row fence; bounded guest wait then releases that transaction while heartbeat renewal continues. Cancellation drains a just-launched child before ownership cleanup, and late publication/cleanup remains fenced. Synthetic consumers replace both launch and wait explicitly, not the production process-isolation machinery.

1. Keep job-specific SQL `SKIP LOCKED` claims. Add nullable build `lease_expires_at` and `execution_node`, with a unique attempt token in the existing `lease_owner`. Renewable attempt ownership extends through publication and private cleanup. A deployment singleton was rejected because independent jobs must continue across controllers.
2. Reconcile expired builds without rerunning the accepted recipe. The execution node owns process identity and guest teardown; a remote reconciler may settle SQL but must retain another node's private tree. Unknown legacy ownership fails closed rather than adopting an unverifiable guest.
3. Use the native SQL owner/expiry convention for shared maintenance roles in `palimpsest_worker_leases(name, token, expires_at)`. Unique-key insertion plus conditional takeover is process-safe; random tokens fence renew/release. Row locks surround effects that do not accept a token. An async heartbeat alone is insufficient: a delayed thread can outlive lease expiry or task cancellation. Blocking effects must drain while the SQL fence remains held.
4. Export scratch collection uses parsed job identity plus a freshly locked SQL row, never filesystem age as ownership. The shared digest locks continue protecting artifact publication/reference commit and garbage collection. SQL role ownership complements, not replaces, those artifact locks.
5. Offline bootstrap creates the role table and adds nullable ownership columns to existing build tables. Do not invent ownership or copy transient role leases during data migration. Stop all old API/build/export workers before bootstrap; mixed-version workers can still run the old unsafe sweep and are unsupported.
6. The smoke hook uses real MariaDB and local OS processes/flocks, but synthetic executor/provider boundaries. SQLite portable tests exercise behavior, not MariaDB `SKIP LOCKED` exclusion. No synthetic result is guest-build proof.

## Risks / Trade-offs

- SQL unavailable or ownership uncertain → fail closed, preserve trees and surface the failure; do not retry accepted build execution.
- An uninterruptible effect can delay takeover → retain the row fence through effect drain; safety takes precedence over prompt failover.
- A dead remote node can leave guest state → retain node-local cleanup evidence until that node returns or an explicitly authorized operator handles its resources.
- Shared artifact locking remains a deployment prerequisite → mount the same coherent store with cross-node digest locks; independent local worker flocks are not digest-lock replacements.
- Application lease clocks require synchronized hosts → maintain clock synchronization; row fences still serialize unfenced destructive effects.
- The implementation slice ran no checks; subsequent parent verification locally passed focused Hub 93/2 skipped, fresh-empty-MariaDB multiprocess smoke 2 (earlier pre-lint run also 2), and full Hub 378/2 skipped after fixing 16 timestamp failures. Eight lease files were formatted; credential-isolation Ruff I001 and seven unformatted files remain outside this change. See [current verification and boundaries](../../../docs/testing.md#native-hub-worker-ownership). These are test-passed, not live-verified results; KVM guest builds, live provider deletion, production and mixed-version rollout remain unexercised. Do not upgrade old qualification receipts.
