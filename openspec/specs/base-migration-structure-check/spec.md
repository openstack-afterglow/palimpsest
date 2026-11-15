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

### Requirement: Listings are explicit and self-consistent

The lister SHALL supply exact, case-sensitive relative Unicode paths without normalization. Canonically equivalent spellings SHALL remain distinct. It SHALL refuse names that cannot decode as strict UTF-8. The pure function SHALL refuse surrogate code points, NUL, absolute paths, root spellings `""`, `"."`, `"/"`, empty components, `.` or `..` components, and any component starting with `.wh.`.

Listers SHALL enumerate the stored, translated payload that the runtime mounts, not only the logical changes that produced it. Listings SHALL include synthesized or inherited directory parents present in that payload, with the attributes the runtime applies. A sparse listing SHALL be used only where the stored payload itself is genuinely sparse; listers SHALL NOT omit stored parents to avoid attribute comparisons.

An `Entry` SHALL have `path`, `kind`, `mode`, `uid`, `gid`, `size`, `content`, `link`, `whiteout`, `opaque`, `xattrs`. Defaults SHALL be `kind=None`, integer fields zero, `content=None`, `link=""`, flags false and `xattrs=None`. Normal kinds SHALL be `file`, `dir`, `symlink`, `char`, `block`, `fifo`, `socket`. Integer fields SHALL be nonnegative integers, not booleans. Mode SHALL contain only permission, setuid, setgid and sticky bits (`0o7777`); file-type bits SHALL be refused, not masked. Only files SHALL carry nonzero size or non-null content. Only symlinks and devices SHALL carry nonempty `link`. Symlink targets SHALL be nonempty NUL-free Unicode text. Device numbers SHALL use unsigned decimal `major:minor`, without leading zeros except zero itself.

A whiteout SHALL have `whiteout=True`, `kind=None` and all fields at defaults except its path. An opaque directory SHALL have `kind="dir"`, `opaque=True` and normal directory attributes. Listers SHALL translate marker filenames, native whiteout devices and overlay control xattrs into these flags. A `char 0:0` entry SHALL be refused as untranslated whiteout. A whiteout carrying a kind, attributes or payload SHALL be refused.

Duplicate paths within a listing, base keys different from `entry.path`, whiteout or opaque flags in flattened bases, and children below an explicitly listed non-directory or whiteout in the same listing SHALL be refused with `MigrationInputError`. Missing directory ancestors SHALL be allowed. Flattening SHALL refuse visible children below unreplaced lower non-directories. No acknowledgement SHALL override refusals. Layer identities SHALL be nonempty NUL-free Unicode strings without surrogates; `complete` SHALL be boolean.

The root directory is not representable. Listers SHALL refuse root opacity in any input. They SHALL also refuse migration unless every kept layer's root mode, uid, gid and canonical xattrs identity equal the new base's effective root metadata; equality that cannot be established SHALL be refused, not omitted or acknowledged. This check SHALL use the stored, translated root metadata that the runtime applies, including synthesized or inherited metadata. The pure function SHALL refuse explicit root entries; it cannot detect omitted root changes or perform this root admission check. Hard links SHALL be expanded by the lister and compared independently by name. Timestamps, including mtime, SHALL be ignored.

#### Scenario: Untranslated or contradictory listing
- **WHEN** a listing contains `.wh.app`, a `char 0:0`, duplicate paths or a child below its own symlink
- **THEN** it is refused with `MigrationInputError`, regardless of acknowledgement

### Requirement: Comparisons include content and extended attributes

File content identity SHALL be `sha256:` followed by 64 lowercase hexadecimal digits, the SHA-256 of complete file bytes. Both compared files SHALL supply it. Missing or unsupported identities SHALL make the comparison unavailable, even when other attributes differ. Unsupported identity strings SHALL remain representable; comparing them SHALL yield `comparison-unavailable`.

Optional `xattrs` SHALL be null or a canonical `sha256:<64 lowercase hex>` identity; invalid digests SHALL be refused. The digest SHALL hash the canonical JSON encoding defined below of a mapping from every xattr name to its RFC 4648 padded standard-base64 value. All namespaces SHALL be included, especially `security.capability`, POSIX ACLs (`system.posix_acl_access`, `system.posix_acl_default`) and SELinux (`security.selinux`); translated overlay control attributes SHALL be excluded. Supplied digests SHALL cover the complete set, including an empty set. Null SHALL mean no identity supplied, not the empty set.

Both present xattrs identities SHALL be compared; one-sided presence SHALL make comparison unavailable. Both null SHALL skip xattrs comparison in the pure function. A lister SHALL set `complete=True` only when every non-whiteout entry in both base listings and every kept-layer listing has a canonical xattrs digest, including the empty-set digest when it has no xattrs; otherwise it SHALL set `complete=False`. A lister unable to enumerate required attributes or contents completely SHALL also set `complete=False`. Missing file-content or one-sided xattrs identities SHALL take priority over known differences. Otherwise equality SHALL compare kind, mode, uid, gid, xattrs, file size and content, and symlink target or device number. Directory equality SHALL compare attributes, not children or opacity.

#### Scenario: Identity unavailable
- **WHEN** a compared file lacks canonical content identity, or xattrs identity exists on only one side
- **THEN** comparison yields `comparison-unavailable` and the verdict is `incomplete`

### Requirement: Structure check compares kept layers against bases

Flattening SHALL process layers in order. Whiteouts, opaque directories and non-directories SHALL hide lower entries at and below their path. A directory replacing a lower non-directory SHALL also hide that lower entry and descendants. Ordinary directories SHALL merge children. All non-whiteout entries of that layer SHALL then be applied with opaque flags cleared. Hiding SHALL affect lower layers only, not that layer's own entries. Listing iteration order SHALL not affect the visible tree.

Each kept layer SHALL be compared against the bases only, never earlier kept layers. This safely may report conflicts absent from the complete stacked result. Each entry SHALL use these target rules, in order, plus the descendant rules below:

1. Whiteout: target absent from both bases SHALL yield no target finding; old-only SHALL yield notice `whiteout-target-gone`; new-only SHALL yield conflict `whiteout-hides-new`; unavailable old/new comparison SHALL yield incomplete `comparison-unavailable`; changed target SHALL yield notice `whiteout-target-changed`; equal target SHALL yield none.
2. Other entry absent from new base: old presence SHALL yield notice `base-removed`; absence from both SHALL yield none.
3. Kept/new difference between directory and non-directory: equal old/new entries SHALL yield none; changed entries or missing old entry SHALL yield conflict `kind-conflict`; unavailable old/new comparison SHALL yield `comparison-unavailable`. Changed directory attributes hidden by a kept non-directory SHALL follow this rule.
4. Kept/new directories, old absent or non-directory: different kept/new attributes SHALL yield conflict `new-path-collision`, unavailable comparison SHALL yield `comparison-unavailable`, equality SHALL yield none, not a notice.
5. All three directories: equal old/new attributes SHALL yield none; unavailable comparison SHALL yield `comparison-unavailable`; otherwise unequal kept/new attributes SHALL yield conflict `attributes-shadowed`, unavailable kept/new comparison SHALL yield `comparison-unavailable`, and equality SHALL yield none.
6. Kept/new non-directories, old absent: equal kept/new entries SHALL yield notice `new-path-identical`, unequal entries SHALL yield conflict `new-path-collision`, unavailable comparison SHALL yield `comparison-unavailable`.
7. Other non-directories: equal old/new entries SHALL yield none; unavailable old/new comparison SHALL yield `comparison-unavailable`; otherwise equal kept/new entries SHALL yield notice `overwrite-now-identical`, unequal entries SHALL yield conflict `overwrite-changed`, unavailable kept/new comparison SHALL yield `comparison-unavailable`.

Every whiteout SHALL also compare every strict new-base descendant to its old-base entry. Added descendants SHALL yield `whiteout-hides-new`, changed descendants SHALL yield notice `whiteout-target-changed`, unavailable comparisons SHALL yield `comparison-unavailable`. Findings SHALL aggregate at the whiteout target, at most once per code including its target findings. Removed descendants SHALL yield none.

Every opaque kept directory and every kept non-directory SHALL query strict new-base descendants. Non-directories SHALL hide like opaque directories even when the directory at their path is implicit because its entry was omitted from a sparse listing. Added or changed descendants SHALL yield conflict `opaque-hides-changes` at the hiding entry. Unavailable descendant comparisons SHALL additionally yield `comparison-unavailable`, even alongside known conflicts. Removed descendants SHALL yield none. Kept children SHALL not exempt lower descendants from comparison.

Each strict ancestor that is a new-base non-directory SHALL be compared against its old-base entry unless the kept layer supplies a directory there. Equality SHALL yield none, changed or added ancestor SHALL yield conflict `ancestor-not-directory`, unavailable comparison SHALL yield `comparison-unavailable`. Each ancestor SHALL be reported at most once per layer with `layer_entry=null`. Other findings SHALL use the triggering kept entry. Codes at one path SHALL not duplicate within a layer.

Flattening SHALL use one lower-tree filter pass per layer with hide-prefix sets and ancestor checks: O(N·depth) per layer. Comparison SHALL use a sorted new-base path index, bisect and prefix counts for added, changed and unavailable entries; overlapping subtree queries SHALL not rescan bases. For B base entries, E kept entries and F generated findings, work SHALL be O(B log B + (B+E)·depth + E log B + F log 2000), excluding string-length costs. Retained findings SHALL use bounded constant space; listings and index SHALL use O(B+E) space.

#### Scenario: Whiteout hides a new child
- **WHEN** both bases have `etc/app`, only the new base has `etc/app/security-fix`, and a kept whiteout hides `etc/app`
- **THEN** conflict `whiteout-hides-new` is reported at `etc/app`

#### Scenario: Symlink hides directory changes
- **WHEN** a kept symlink replaces a directory and the new base changes directory attributes and adds a child
- **THEN** it yields `kind-conflict` and `opaque-hides-changes`

#### Scenario: Earlier kept layer removed a path
- **WHEN** one kept layer whiteouts a changed base file and another re-adds a different copy
- **THEN** the later layer still yields `overwrite-changed`, even if both full stacks agree

### Requirement: Verdicts and fixed finding bounds

The report SHALL retain the first 2000 conflict-or-incomplete findings as one group, then the first 2000 notices separately. Within each group order SHALL be layer index, path by Unicode code point, then code by Unicode code point. Bounds SHALL not be caller-configurable. Counts SHALL include all findings before cuts. `findings_truncated` SHALL be true if either group is cut.

The verdict SHALL be `incomplete` when `complete=False`, any incomplete finding exists, or conflict-or-incomplete findings are cut. Otherwise it SHALL be `conflict` if conflicts exist, else `clean`. Cutting notices alone SHALL not change verdict. A risk acknowledgement SHALL be required exactly when verdict is not `clean`. An unenumerable image SHALL produce incomplete input, not a claim of completeness.

#### Scenario: Notices do not suppress conflict details
- **WHEN** 2001 notices and one conflict exist
- **THEN** the conflict precedes 2000 notices, counts remain complete, truncation is true and verdict is `conflict`

### Requirement: Report format and identity

The pure report SHALL have exactly these JSON members:

- `schema`: string `palimpsest.base-migration-check.v1`.
- `verdict`: string `clean`, `conflict` or `incomplete`.
- `requires_risk_acknowledgement`, `inputs_complete`, `findings_truncated`: booleans. `inputs_complete` SHALL equal `complete`.
- `layers`: ordered array of objects with integer zero-based `index`, string `id`, integer `entries` counting every listed entry, including whiteouts.
- `counts`: nonnegative integer `conflict`, `notice`, `incomplete`, `package_state_conflict`. `package_state_conflict` SHALL be a subset of `conflict`, not a disjoint severity.
- `findings`: array ordered and bounded above.
- `statement`: exactly `Structure check only: it compares paths, kinds, attributes and content identities of the old and new base against the paths the kept layers touch. It does not check ABI, package dependencies, or whether the migrated image runs; that verification is the user's.`

Each finding SHALL have exactly integer `layer_index`, string `layer`, string `code`, string `severity` (`conflict`, `notice`, `incomplete`), string `path`, boolean `package_state`, and `old`, `new`, `layer_entry` summaries or null. Summaries SHALL omit path. Whiteout summary SHALL be exactly `{"kind":"whiteout"}`. Other summaries SHALL contain `kind`, integer `mode`, integer `uid`, integer `gid`, and `xattrs` (digest string or null). Files SHALL add integer `size` and `content` (supplied identity string or null). Symlinks and devices SHALL add string `link`; devices SHALL encode `major:minor`. Opaque directories SHALL add `opaque:true`; otherwise `opaque` SHALL be absent. No other summary keys SHALL occur.

`package_state` SHALL be true exactly at or below these paths, not mere string prefixes: `var/lib/dpkg`, `var/lib/apt`, `var/cache/apt`, `var/cache/debconf`, `var/lib/rpm`, `usr/lib/sysimage/rpm`, `var/lib/dnf`, `var/cache/dnf`, `var/lib/yum`, `var/cache/yum`, `var/lib/pacman`, `var/lib/apk`, `lib/apk/db`, `var/cache/apk`, `var/cache/ldconfig`, `etc/ld.so.cache`.

The service SHALL add strings `existing_image_manifest_digest`, `old_base_manifest_digest`, `new_base_manifest_digest`; objects `existing_os`, `old_base_os`, `new_base_os`, each with string `family` and `version`; and boolean `os_release_changed`, true exactly when existing and new-base versions differ. Identity SHALL cover the complete report, including these and any later service-added members. Identity SHALL be stored outside the report, not inserted as a self-reference. The pure digest function SHALL hash every supplied member, without filtering. Service enrichment and validation are outside `check`.

Canonical JSON SHALL use UTF-8 bytes of Python `json.dumps` with `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`, `allow_nan=False`: object keys sorted by Unicode code point, no whitespace outside strings, ASCII escaping for non-ASCII characters (lowercase `\u` hexadecimal escapes and surrogate pairs above U+FFFF), JSON escapes for quote, backslash and controls, no slash escaping. Report values SHALL be objects, arrays, strings, booleans, integers or null, not floats. Identity SHALL be `sha256:` followed by lowercase SHA-256 hex of those bytes. This is not RFC 8785.

#### Scenario: Identity includes service metadata
- **WHEN** listing iteration order changes without logical input or service-member changes
- **THEN** report and identity remain unchanged
- **WHEN** any service member changes
- **THEN** the full report digest is recomputed

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
