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
A structure check report with verdict `conflict` SHALL make `filesystem` `failed` with the reason that running the combination needs an explicit risk acknowledgement. A report with verdict `incomplete`, or a document that is not a recognised report, SHALL make the stage `unverified`.

#### Scenario: Conflicting overwrite
- **WHEN** the structure check reports a conflict
- **THEN** `filesystem` is `failed`, the reason names the acknowledgement, and the stage does not say the combination cannot run

### Requirement: Diagnoses carry no secrets or artifact free text
Reasons SHALL be fixed sentences with architectures, artifact kinds and short plain release names only. A diagnosis SHALL NOT contain file paths from artifacts, recipe text, credentials, tokens or record contents.

#### Scenario: Release name that is not a plain name
- **WHEN** a declared release name contains control characters or is overlong
- **THEN** it is not repeated in the diagnosis
