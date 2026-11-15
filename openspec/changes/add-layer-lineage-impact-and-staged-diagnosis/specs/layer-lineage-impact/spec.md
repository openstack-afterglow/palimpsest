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
Every affected layer SHALL be reported with its existing lineage state `kept` and with the recorded local consumers that reference it (runs, projects and retained instances). The plan SHALL state that existing layers and their consumers stay bound to their current parent.

#### Scenario: A run uses an affected layer
- **WHEN** a recorded run references an affected layer
- **THEN** the plan names that run under the layer's existing lineage and reports no change to it

### Requirement: The actions of a new lineage are separate states
For each affected layer the plan SHALL report `rebuild`, `reconversion`, `transfer` and `deployment` as separate states. A state SHALL NOT be derived from another state. `rebuild` SHALL say whether a recipe digest is recorded, whether the recipe must be edited to name the new parent, and how the build cache applies (`decided-by-buildkit`, `not-reused`, or `unknown`). The build cache and the conversion cache SHALL be reported separately. States the plan did not inspect SHALL be reported as `not-inspected`, and states that do not apply SHALL be reported as `not-applicable`.

#### Scenario: Layer built from a recorded Palimpsestfile recipe
- **WHEN** an affected layer was produced by a completed local `Palimpsestfile` build record
- **THEN** its rebuild state is `required` with `recipe: digest-recorded`, `recipe_edit: required` and `cache: not-reused`, and its transfer and deployment states are `not-inspected`

#### Scenario: Committed or imported layer
- **WHEN** an affected layer has no build record
- **THEN** its rebuild state is `required` with `recipe: not-recorded`

#### Scenario: BuildKit runtime pack under a changed base image
- **WHEN** an affected layer is a BuildKit runtime pack bound to the changed base image
- **THEN** its rebuild state is `inputs-unchanged` with `cache: decided-by-buildkit`, and its reconversion state is `required` with `cache: keyed-by-base-image`

### Requirement: The plan states its scope and bound
The plan SHALL name the scope of the metadata it read, SHALL be deterministic for the same records, SHALL terminate on cyclic or duplicate records, and SHALL report `truncated` when more descendants exist than its fixed bound. A stored layer whose own record names the changed artifact but which cannot be reached through stored parent records SHALL be listed as `unreached` and SHALL NOT be omitted silently.

#### Scenario: Cyclic parent records
- **WHEN** stored metadata contains a parent cycle
- **THEN** the plan lists each layer once and terminates

#### Scenario: A layer between the changed artifact and a descendant is not stored
- **WHEN** a stored layer records the changed base image, or lists the changed layer among its recorded ancestors, and its direct parent is not stored
- **THEN** the plan lists that layer under `unreached` and not under `affected`

### Requirement: A computed plan is not an admission decision
A plan whose replacement fails or lacks structural admissibility SHALL still be returned successfully with that finding. Only unusable input (unknown reference, malformed digest, an artifact that cannot be a parent, a replacement equal to the replaced artifact) SHALL be an error.

#### Scenario: Replacement with another architecture
- **WHEN** the replacement declares a different architecture
- **THEN** the command succeeds and every affected layer's structure stage is `failed` with that reason

#### Scenario: Replacement that descends from the replaced layer
- **WHEN** the replacement's recorded parent chain contains the layer being replaced
- **THEN** the structure stage is `failed`

#### Scenario: Chain or base that cannot be compared
- **WHEN** the replacement is bound to another base, or either chain records no base, or the replacement's parent chain is not completely stored
- **THEN** the structure stage is `unverified`, never `passed`
