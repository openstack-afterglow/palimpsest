## ADDED Requirements

### Requirement: Compatibility is reported per stage
A compatibility diagnosis SHALL report five stages in this order: `structure` (parent chain, digests, artifact kinds, base binding, declared architecture), `filesystem` (path collisions, overwrites, whiteouts and opaque directories between a base and the kept layers), `distribution_abi` (distribution release, libc and library ABI, package dependencies), `kernel_device` (guest kernel, drivers, devices, GPU) and `smoke_test` (an actual run). Each stage SHALL have exactly one status: `passed`, `failed` or `unverified`.

#### Scenario: Only structure was evaluated
- **WHEN** a replacement is structurally admissible and no other evidence exists
- **THEN** `structure` is `passed` and the other four stages are `unverified`, each with its reason

### Requirement: Nothing unevaluated is reported as passed
A stage SHALL be `passed` only from evidence produced for that stage. A stage with no evidence SHALL be `unverified` and SHALL state why. The diagnosis SHALL NOT contain an overall compatible verdict, and a passed stage SHALL NOT change the status of any other stage.

#### Scenario: Clean structure check
- **WHEN** a base-migration structure check report with verdict `clean` is supplied
- **THEN** `filesystem` is `passed` with the report digest as evidence, and `distribution_abi`, `kernel_device` and `smoke_test` remain `unverified`

#### Scenario: Declared release change
- **WHEN** the old and new base declare different releases
- **THEN** the change is named in the reasons of `distribution_abi` and the stage stays `unverified`

### Requirement: Structure-check results map to the filesystem stage
A structure check report in which a conflict was found SHALL make `filesystem` `failed` with the reason that running the combination needs an explicit risk acknowledgement, also when other parts of the check were incomplete. A report SHALL make the stage `passed` only when its verdict is `clean`, its inputs are complete and its conflict and incomplete counts are zero. Every other report, and any document that is not a recognised report, SHALL make the stage `unverified`; malformed input SHALL NOT raise and SHALL NOT pass.

#### Scenario: Conflicting overwrite
- **WHEN** the structure check reports a conflict
- **THEN** `filesystem` is `failed`, the reason names the acknowledgement, and the stage does not say the combination cannot run

#### Scenario: Conflict found in an incomplete check
- **WHEN** the report verdict is `incomplete` and its conflict count is above zero
- **THEN** `filesystem` is `failed`

#### Scenario: Report that does not agree with itself
- **WHEN** a report says `clean` but counts a conflict, or says its inputs are incomplete, or is not a mapping
- **THEN** `filesystem` is not `passed`

### Requirement: Only stage evidence is accepted for the structure stage
The structure stage SHALL accept only a result that carries one of the three stage statuses. A platform-compatibility result (`compatible` / `incompatible` / `unknown`), which also reflects the client host and backend, SHALL NOT be mapped to a stage status.

#### Scenario: Platform result supplied as structure
- **WHEN** a result with status `compatible` is supplied as structure evidence
- **THEN** the structure stage is `unverified`

### Requirement: Diagnoses carry no secrets or artifact free text
Reasons SHALL be fixed sentences with architectures, artifact kinds and short plain release names only. A diagnosis SHALL NOT contain file paths from artifacts, recipe text, credentials, tokens or record contents.

#### Scenario: Release name that is not a plain name
- **WHEN** a declared release name contains control characters or is overlong
- **THEN** it is not repeated in the diagnosis
