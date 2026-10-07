## MODIFIED Requirements

### Requirement: Preserve measured CI and protected execution boundaries
CI changes MUST retain comparable measured critical paths, hosted-only untrusted PR jobs, verified shard execution and successful publication verification. Native qualification SHALL be explicitly opt-in using canonical lowercase `PALIMPSEST_KVM_ENABLED=true` or `false`: enabled requires successful native proof; disabled permits an intentionally skipped native job without claiming qualification. Missing/non-boolean flags, native failures/cancellation/missing outcomes, unsuccessful release verification, cancelled workflows and untrusted publication contexts MUST NOT authorize publication. Test's actual Bash verdict SHALL compare literal flags; Release job conditions retain GitHub's case-insensitive comparison semantics and MUST NOT be claimed to reject uppercase boolean aliases. Separate owner approval MUST precede native runner exposure, native opt-out, release bypasses or remote resource mutation; the 2026-10-07 owner request authorizes this explicit native opt-out only.

#### Scenario: Untrusted PR requests native proof
- **WHEN** a fork or other untrusted PR reaches the test workflow
- **THEN** it runs only hosted validation, with no privileged runner or publication credential exposure

#### Scenario: CI improvement is proposed
- **WHEN** a workflow change claims a shorter critical path
- **THEN** before/after comparable runs are measured and actual median, p90 and sample size are recorded rather than a projection claimed as a result

#### Scenario: Owner explicitly disables native qualification
- **WHEN** a trusted dev/main push has literal flag `false` and native job result `skipped`
- **THEN** the native policy verdict succeeds while reporting that no native KVM proof ran
- **AND** all ordinary validation remains required and the native protected environment is not requested

#### Scenario: Disabled native dependency precedes a release
- **WHEN** a non-cancelled trusted repository v-tag push has `verify=success`, flag `false` and native result `skipped`
- **THEN** root publication is allowed despite that intentionally skipped dependency
- **AND** its notice states that the release has no native KVM proof

#### Scenario: Enabled or invalid qualification cannot pass without proof
- **WHEN** flag is `true` without native `success`, flag is missing/non-boolean, disabled native result is not `skipped`, or release verification is not `success`
- **THEN** the policy rejects the result and release publication cannot run

#### Scenario: Cancelled or untrusted publication
- **WHEN** the workflow is cancelled, the repository is not the trusted repository, the event is not push, or the ref is not a v-tag
- **THEN** root publication cannot run regardless of native or verification outcomes
