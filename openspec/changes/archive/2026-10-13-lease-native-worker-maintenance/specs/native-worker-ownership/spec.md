## ADDED Requirements

### Requirement: SQL claims distribute jobs independently of local flocks
Hub workers SHALL use atomic SQL `FOR UPDATE SKIP LOCKED` claims as authoritative per-job distribution. Build attempts SHALL have unique ownership tokens, renewable expiry and recorded execution-node identity. A local process lock SHALL NOT authorize recovery of another live SQL claim.

#### Scenario: Independent worker startup during a live build
- **WHEN** two or three workers share SQL and artifact storage but hold distinct local flocks
- **THEN** startup and periodic recovery leave a live build's status, output and scratch intact
- **AND** other queued jobs remain claimable without a deployment-wide build singleton.

#### Scenario: Stale owner returns
- **WHEN** an attempt loses or expires its SQL lease
- **THEN** its late publication and shared-state cleanup are fenced from the current claim
- **AND** cancellation drains any already-started blocking effect before relinquishing its fence.

### Requirement: Interrupted work is reconciled without repeating accepted mutation
Expired build claims SHALL be reconciled as interrupted rather than automatically executing the recipe again. Shared SQL recovery SHALL NOT signal or clean another node's KVM/process resources. Unverifiable local ownership SHALL fail closed and preserve evidence.

#### Scenario: Lease changes between preparation and guest launch
- **WHEN** an attempt loses ownership after writing its input tree but before launching the builder
- **THEN** a fresh SQL row fence rejects process launch and output/log truncation
- **AND** the stale attempt does not issue the accepted guest mutation.

#### Scenario: Crashed owner and remote reconciler
- **WHEN** a build owner crashes and its lease expires
- **THEN** another worker may reconcile the SQL row without reissuing the build
- **AND** remote guest state and its retained private tree are reserved for verified owning-node cleanup.

#### Scenario: Owning node restarts after remote reconciliation
- **WHEN** the recorded node restarts and observes its retained terminal tree
- **THEN** it claims recovery exclusively and performs identity-verified local cleanup without losing a published artifact.

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
