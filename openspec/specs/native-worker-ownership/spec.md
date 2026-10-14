# native-worker-ownership Specification

## Purpose
Define SQL authority for the Hub API build protocol and distributed export maintenance while keeping private execution credentials, durable worker journals and exact-owned cleanup separate from control-plane storage.
## Requirements
### Requirement: API SQL authority is independent of local flocks
The Hub API SHALL serialize HTTPS build admission/claim against shared SQL authority. Exactly one build SHALL be active globally, with a unique lease token and a persisted fixed UTC deadline. Independent API-local flocks SHALL NOT replace SQL exclusion. Build workers SHALL receive only HTTPS authority and private local scratch, never SQL/CAS/Redis/Keystone credentials.

#### Scenario: Independent API claim during a live build
- **WHEN** independent API processes share SQL and coherent artifact storage while holding distinct local flocks
- **THEN** a live build's status/output are preserved and no second build is claimed
- **AND** expired claims are terminally interrupted, never renewed into recipe replay.

#### Scenario: Stale owner returns
- **WHEN** an attempt loses or expires its SQL lease
- **THEN** its late publication and shared-state cleanup are fenced from the current claim
- **AND** cancellation drains any already-started blocking effect before relinquishing its fence.

### Requirement: Interrupted work is reconciled without repeating accepted mutation
Expired build claims SHALL be reconciled as interrupted rather than automatically executing the recipe again. API SQL recovery SHALL NOT signal or clean the worker VM's processes, loops or private scratch. Unverifiable local ownership SHALL fail closed and preserve evidence.

#### Scenario: Uncertain claim or execution
- **WHEN** a worker loses a claim response or restarts with a claimed/executing journal
- **THEN** it halts or performs exact-owned cleanup without claiming/re-executing that recipe
- **AND** only the original lease may read inputs or settle output before its deadline.

#### Scenario: Crashed owner and API reconciliation
- **WHEN** a private build worker crashes and its deadline expires
- **THEN** the API may reconcile the SQL row without reissuing the build
- **AND** private VM state and retained scratch remain reserved for exact-owned local cleanup.

#### Scenario: Worker restarts after terminal reconciliation
- **WHEN** its durable journal records an output/failure mutation with an uncertain response
- **THEN** it queries exact-lease status without replaying the mutation
- **AND** it establishes local process/loop cleanup before removing scratch or admitting another claim.

### Requirement: Shared export sweeps have native exclusive ownership
Export maintenance and delegation sweeps SHALL have renewable SQL role ownership with random tokens, fenced effects and token-owned release/completion. Per-export claims SHALL remain independent. Slow blocking provider deletion SHALL retain its fence through drain; stale completions SHALL NOT overwrite a new cleanup attempt.

#### Scenario: Competing maintenance workers
- **WHEN** workers concurrently invoke maintenance and delegation cleanup
- **THEN** each shared role has one effective owner at a time
- **AND** expired/crashed ownership is reclaimable without a stale release deleting the replacement's lease.

#### Scenario: Old scratch belongs to a live export
- **WHEN** scratch exceeds the maintenance age threshold but its SQL export attempt is live
- **THEN** maintenance preserves it regardless of filesystem age
- **AND** ambiguous scratch identity is retained rather than guessed.

### Requirement: Native proof scope is explicit
Behavior tests and the SQL/local-process smoke hook SHALL distinguish real SQL/process ownership assertions from synthetic guest/provider effects. The hook SHALL use disposable SQL state and independent local lock paths; it SHALL NOT require KVM or access production/cloud resources.

#### Scenario: Synthetic smoke on a non-KVM host
- **WHEN** the ownership hook runs against disposable MariaDB with local worker processes
- **THEN** actual SQL exclusion, live-claim preservation and expired-owner reconciliation assertions execute
- **AND** its result states that synthetic guest/provider boundaries prove neither guest build nor live provider policy.

