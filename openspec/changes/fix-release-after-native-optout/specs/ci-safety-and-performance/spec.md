## ADDED Requirements

### Requirement: Complete GitHub release after permitted root publication

The final GitHub Release job SHALL require a non-cancelled workflow and successful root publication. An explicitly disabled native dependency SHALL NOT suppress completion after the existing trusted-ref, verification, native-policy and protected publisher gates have permitted root publication. No failed, skipped, cancelled or missing publisher result SHALL authorize GitHub Release creation.

#### Scenario: Native opt-out with successful publication

- **WHEN** native qualification was explicitly disabled and skipped, verification succeeded and permitted root publication succeeded
- **THEN** the GitHub Release job is eligible to attach the same verified root distributions
- **AND** no native qualification is claimed

#### Scenario: Publication unavailable or workflow cancelled

- **WHEN** root publication failed, was cancelled or skipped, has no result, or the workflow is cancelled
- **THEN** the GitHub Release job is ineligible

#### Scenario: Repair an existing immutable release

- **WHEN** a completed permitted tag run published distributions to PyPI but its final GitHub job was incorrectly skipped
- **THEN** an authorized repair attaches only hash-matched tag-run distributions without moving the tag, overwriting assets or republishing PyPI
