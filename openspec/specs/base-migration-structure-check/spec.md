# base-migration-structure-check Specification

## Purpose
Define how existing image layers are moved onto a new base image of the same operating system: the inputs that identify the old base, the new base and the layers that are kept; the structure check that compares them; the report format; and what an explicit risk acknowledgement does and does not allow. Migration is offered on the premise that the result may not work. The check is structural only and is never a statement that the migrated image runs.

## Requirements
### Requirement: Migration inputs are identified by content

A migration SHALL take an existing published image, an old base and a new base, each identified by its published manifest digest. The old base's ordered layer payload digests SHALL be an exact prefix of the existing image's ordered layer payload digests; the layers after that prefix are the kept layers, in their original order. The new base SHALL be a published image with mode `oci-root`, the same platform and the same declared OS family as the existing image. A different declared OS version is permitted and SHALL be reported as an OS release change.

A migration SHALL be refused, and no acknowledgement SHALL override the refusal, when the old base is not such a prefix, when either image is not `oci-root`, when the platform differs, when the declared OS family differs or is not a supported OS, when the caller may not read an input image, or when a payload fails its digest or size check.

#### Scenario: Old base is not a prefix
- **WHEN** the named old base's layers are not the first layers of the existing image in the same order
- **THEN** the migration is refused before any check runs and a risk acknowledgement does not change that

#### Scenario: OS release change
- **WHEN** the new base declares the same OS family and a different OS version
- **THEN** the migration proceeds to the structure check and the report states the release change

### Requirement: Structure check compares what the kept layers touch

The check SHALL flatten the old base and the new base with overlay semantics (later layers win; whiteouts and opaque directories hide lower content; a directory replacing a non-directory or the reverse hides what was below) and SHALL compare, for every entry of every kept layer, what that path met in the old base with what it meets in the new base. Entries are compared by kind, mode, owner, group and, for regular files, size and content identity; symbolic links by target; device nodes by device number.

Findings SHALL use these codes. Conflict findings:

- `overwrite-changed`: the layer replaces a file that both bases have, the new base's copy differs from the old one, and the layer's copy differs from the new one.
- `new-path-collision`: the layer adds a path the old base did not have and the new base now provides with different content or attributes.
- `kind-conflict`: the layer's entry and the new base's entry at the same path differ between directory and non-directory, and the old base did not already have that same entry.
- `ancestor-not-directory`: the layer has an entry below a path that is a non-directory in the new base, the layer carries no directory entry of its own for it, and the old base did not already have that same non-directory.
- `opaque-hides-changes`: an opaque directory of the layer would hide content that is new or changed in the new base.
- `attributes-shadowed`: the new base changed a directory's mode or ownership and the layer's directory entry carries different attributes.
- `whiteout-hides-new`: a whiteout of the layer would delete a path that only the new base provides.

Notice findings, which do not change the verdict: `overwrite-now-identical`, `new-path-identical`, `base-removed`, `whiteout-target-gone`, `whiteout-target-changed`.

A comparison that cannot be made, for example a regular file without a content identity on one side, SHALL be the finding `comparison-unavailable` with severity `incomplete`.

Every finding SHALL carry the kept layer's index and identity, the path, the old-base entry, the new-base entry, the layer entry, and whether the path is package-manager state (package databases, package caches and the dynamic-linker cache). Package-state conflicts are still conflicts; they are counted separately because install layers rewrite those shared files.

#### Scenario: Layer adds only new paths
- **WHEN** every entry of the kept layers is at a path neither base has
- **THEN** there are no findings and the verdict is `clean`

#### Scenario: Base file changed under an overwriting layer
- **WHEN** a kept layer replaces a file that the new base ships with different content than the old base
- **THEN** the finding is `overwrite-changed` with both base entries and the layer entry, and the verdict is `conflict`

#### Scenario: Directory became a symbolic link in the new base
- **WHEN** a kept layer carries a directory at a path that is a symbolic link in the new base and was a directory in the old base
- **THEN** the finding is `kind-conflict` and the verdict is `conflict`

#### Scenario: Package database rewritten by an install layer
- **WHEN** a kept layer replaces the package database and both bases ship different copies
- **THEN** the finding is a conflict marked as package state and counted in the separate package-state total

### Requirement: Verdicts never treat an unfinished check as clean

The verdict SHALL be `clean` only when every input was listed completely, every comparison was possible, the finding list was not cut and there is no conflict finding. It SHALL be `incomplete` when any input could not be listed in full, when any `comparison-unavailable` finding exists, or when the finding list exceeds its bound and is cut. Otherwise, with at least one conflict finding, it SHALL be `conflict`. An image whose mode or format cannot be enumerated SHALL yield `incomplete`, not `clean`.

#### Scenario: Content identity missing
- **WHEN** a file the layer replaces has the same size in both bases and no content identity is available
- **THEN** the verdict is `incomplete` and an acknowledgement is required

#### Scenario: Finding list cut
- **WHEN** more findings exist than the report bound
- **THEN** the report keeps the bounded prefix, states that it was cut, keeps the full counts and the verdict is `incomplete`

### Requirement: Report format

The report SHALL be a JSON object with schema `palimpsest.base-migration-check.v1` and these members: `verdict` (`clean`, `conflict` or `incomplete`); `requires_risk_acknowledgement` (true unless the verdict is `clean`); `inputs_complete`; `layers` (index, identity and entry count of each kept layer, in order); `counts` (`conflict`, `notice`, `incomplete`, `package_state_conflict`); `findings` ordered by layer index, path and code; `findings_truncated`; and `statement`, the fixed text that the check is structural only and that ABI, dependency and run verification are the user's. A service that produces the report SHALL add the manifest digests of the existing image, the old base and the new base, their declared OS family and version, and whether the OS release changed. The report's identity is the SHA-256 of its canonical JSON; the same inputs SHALL give the same identity regardless of input ordering.

#### Scenario: Deterministic identity
- **WHEN** the same trees and layers are supplied in a different iteration order
- **THEN** the report identity is unchanged

### Requirement: Risk acknowledgement is explicit, specific and narrow

A migration whose verdict is not `clean` SHALL be blocked unless the caller acknowledges the compatibility risk through the dedicated control: the CLI flag `--yes-i-understand-the-risk`, the Hub API request member `acknowledge_compatibility_risk: true`, and a dashboard confirmation that states the same risk. The three SHALL have the same meaning. No general force, yes, confirm or recreate option SHALL count as that acknowledgement.

The acknowledgement SHALL be required both to register a migrated image whose verdict is not `clean` and to run or create an instance from an image whose recorded migration verdict is not `clean`. It SHALL NOT bypass authentication, authorization, ownership, visibility, payload integrity, quota, supported-OS or platform checks, and it SHALL NOT turn a refused migration into an accepted one.

#### Scenario: Conflict without acknowledgement
- **WHEN** the verdict is `conflict` and the caller passes only a general force or yes option
- **THEN** the migration is blocked with the report available to the caller

#### Scenario: Acknowledged conflict
- **WHEN** the verdict is `conflict` and the caller acknowledges the compatibility risk
- **THEN** the migrated image is registered with the acknowledgement recorded, and every later run of it requires the acknowledgement again

#### Scenario: Acknowledgement does not widen authority
- **WHEN** the caller acknowledges the risk but lacks permission, exceeds quota or names an unsupported OS
- **THEN** the request fails exactly as it would without the acknowledgement

### Requirement: Migration registers a new image and keeps the original

An accepted migration SHALL register a new immutable published image whose layers are the new base's layers followed by the kept layers, unchanged and in their original order. The original image SHALL remain registered and usable. The new image SHALL record the existing image's manifest digest, the old and new base manifest digests, the report identity, the verdict and, when one was required, who acknowledged the risk and when. It SHALL declare the new base's OS version.

#### Scenario: Original stays usable
- **WHEN** a migration is registered
- **THEN** the original image can still be read, pulled and run, and the new image names it as its origin

### Requirement: Limits are shown with every result

Every surface that shows a migration result SHALL show the report's statement, the separate package-state conflict count and the fact that a `clean` verdict is not a compatibility guarantee.

#### Scenario: Clean result
- **WHEN** a migration result with verdict `clean` is shown
- **THEN** the statement that run verification remains the user's is shown with it

Implementation status (2026-11-15; informative). Implemented: the comparison and report of the structure check as a pure function over supplied listings (`palimpsest_local.base_migration`: `flatten`, `check`, `report_digest`), with unit tests. Not implemented: listing the entries of stored layers, the migration request on any surface (CLI, Hub API, dashboard), the acknowledgement controls, registration of a migrated image and the run-time gate. Images in mode `application` (cloud base image plus layers) are outside this contract: their base is a disk image that is not enumerated.
