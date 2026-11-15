## ADDED Requirements

### Requirement: A parent replacement is planned without side effects
The lineage impact plan SHALL be computed from recorded metadata only. Computing it SHALL NOT build, convert, download, transfer, tag, start, stop or modify any artifact, run, project or cache entry, and SHALL leave the state root byte-for-byte unchanged.

#### Scenario: Plan for a parent layer
- **WHEN** a user asks for the impact of replacing a stored layer
- **THEN** the plan lists every recorded layer whose parent chain passes through it, nearest first, each exactly once, and nothing in the store has changed

#### Scenario: Plan for a base image
- **WHEN** a user asks for the impact of replacing a stored base image
- **THEN** the plan lists the root layers bound to that base and their recorded descendants, and no layer bound to another base

### Requirement: The existing lineage is reported as kept
Every affected layer SHALL be reported with its existing lineage state `kept` and with the recorded local consumers that reference it. The plan SHALL state that existing layers and their consumers stay bound to their current parent.

#### Scenario: A run uses an affected layer
- **WHEN** a recorded run references an affected layer
- **THEN** the plan names that run under the layer's existing lineage and reports no change to it

### Requirement: The actions of a new lineage are separate states
For each affected layer the plan SHALL report `rebuild`, `reconversion`, `transfer` and `deployment` as separate states. A state SHALL NOT be derived from another state. `rebuild` SHALL say whether a recorded recipe exists and how the build cache applies (`decided-by-buildkit`, `not-reused`, or `unknown`). States the plan did not inspect SHALL be reported as `not-inspected`, and states that do not apply SHALL be reported as `not-applicable`.

#### Scenario: Layer built from a recorded recipe
- **WHEN** an affected layer was produced by a completed local build record
- **THEN** its rebuild state is `required` with `recipe: recorded`, and its transfer and deployment states are `not-inspected`

#### Scenario: Committed or imported layer
- **WHEN** an affected layer has no build record
- **THEN** its rebuild state is `required` with `recipe: not-recorded`

### Requirement: The plan states its scope and bound
The plan SHALL name the scope of the metadata it read, SHALL be deterministic for the same records, SHALL terminate on cyclic or duplicate records, and SHALL report `truncated` when more descendants exist than its fixed bound.

#### Scenario: Cyclic parent records
- **WHEN** stored metadata contains a parent cycle
- **THEN** the plan lists each layer once and terminates

### Requirement: A computed plan is not an admission decision
A plan whose replacement fails or lacks structural admissibility SHALL still be returned successfully with that finding. Only unusable input (unknown reference, malformed digest, an artifact that cannot be a parent, a replacement equal to the replaced artifact) SHALL be an error.

#### Scenario: Replacement with another architecture
- **WHEN** the replacement declares a different architecture
- **THEN** the command succeeds and every affected layer's structure stage is `failed` with that reason
