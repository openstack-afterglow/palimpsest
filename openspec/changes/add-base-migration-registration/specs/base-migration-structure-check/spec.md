## ADDED Requirements

### Requirement: The service inspects exact published runtime payloads

The migration service SHALL authorize the exact existing combination, old base and new base and verify their sealed published manifest identities, ordered membership, mode, platform, supported OS and referenced payload digests and sizes. It SHALL derive the kept suffix from the exact ordered old-base prefix. It SHALL enumerate the stored translated SquashFS bytes that a consumer will mount, with separate root admission evidence, not infer a listing from OCI source tar, catalogue metadata or conversion receipts. It SHALL use the accepted listing, flattening, root and canonical-identity rules on both base stacks and all kept occurrences before claiming complete coverage. It SHALL support same-release revisions and supported same-family release changes without changing either case's evidence standard.

If the existing combination is a managed migration, the old-base UUID and published manifest digest SHALL equal the recorded replacement-base pin. Matching the existing payload prefix alone SHALL NOT substitute. The old and replacement bases SHALL differ in published manifest digest and ordered payload digest sequence. A republished copy with an identical sequence is not a base change.

The service SHALL run inspection without booting the artifact, mounting a tenant image in the API host kernel, or extracting a host filesystem tree. The reader SHALL be isolated and bounded; it SHALL not follow symlinks or open device nodes. The service SHALL establish required root evidence independently of non-root enumeration. Verified bytes with partial or wholly unavailable non-root coverage SHALL produce an authoritative `incomplete` report when root and all other hard checks pass. The report SHALL identify the coverage gap and only include findings supported by the observed coverage. Missing required root evidence, malformed or contradictory listings, unauthorized inputs, unsupported OS/platform and missing or invalid payload bytes SHALL be refusals, not acknowledgeable incomplete comparisons. An unavailable service that cannot produce a trusted report SHALL not permit registration.

Inspection admission SHALL bound concurrent work across the Hub deployment, per project and per authenticated subject. It SHALL bound request rates and cached report storage. Reuse of a sealed check SHALL require the exact ordered pins, inspection target, policy and reader version, authenticated scope and current read authority.

If a base has incomplete topology or a visible base entry lacks a canonical xattrs identity, the service SHALL suppress path comparison before invoking the pure check. It SHALL preserve all kept occurrence identities with empty comparison listings and `complete=False`. With comparable bases, it SHALL omit comparison of any kept occurrence with incomplete topology while retaining that occurrence's identity and index. It SHALL report the excluded coverage, not treat missing paths as known absent, and preserve the resulting pure counts, verdict, truncation and finding order unchanged.

#### Scenario: Same-release revision
- **WHEN** a new published base has a different manifest digest and the same supported declared OS family, version and platform
- **THEN** the service verifies and compares the exact stored payloads and reports `os_release_changed: false`

#### Scenario: Same-family OS release change
- **WHEN** the new base has a different supported declared release in the same OS family and platform
- **THEN** the service performs the same stored-payload check and reports the old and new release with `os_release_changed: true`

#### Scenario: Non-root inspection is partial or wholly unavailable after root admission
- **WHEN** input integrity, root evidence and all other hard checks pass but non-root inspection stops early or cannot start
- **THEN** the service reports `inputs_complete: false` and `incomplete`, never `clean`, even when it has no non-root entries
- **AND** registration requires dedicated acknowledgement bound to the exact authoritative report

#### Scenario: A partial base cannot create an absence-based finding
- **WHEN** a missing part of the old base listing might contain an apparent new package-state path
- **THEN** the incomplete report identifies the excluded comparison instead of reporting an unsupported collision, and zero findings do not mean zero conflicts

#### Scenario: Root evidence, payload integrity or trusted report is unavailable
- **WHEN** required evidence of root opacity or kept-root equality cannot be established, published payload integrity fails, or the service cannot seal a trusted report
- **THEN** the service does not register the migration regardless of acknowledgement

### Requirement: Registration binds the check to a separate immutable publication

The check operation SHALL not register an image or create a runtime resource. It SHALL seal and store its report before returning it with a scoped reference and digest. Registration SHALL bind the authenticated request to that exact unexpired record and its three pinned inputs, without rerunning inspection. The service SHALL verify the seal, digest and current policy, authority, destination quota and stored input integrity before committing. A stale or unprovable binding SHALL not register an image. Acknowledgement SHALL apply only to the exact reviewed result. The committed result SHALL retain the authoritative report independently of the preview's expiry.

Successful registration SHALL seal a new published identity with the replacement base's OS version and its ordered layers followed by all kept occurrences unchanged by payload digest and size. The publication, graph membership, storage references and migration record SHALL become visible together. The new result SHALL record exact publication UUID and manifest digest pins for the origin and both bases, full report identity, both verdicts, contributing migration bindings, and authenticated acknowledger and time when required. Migration authority SHALL NOT be writable through ordinary image metadata. The original and its current runtime bindings SHALL NOT be mutated, replaced, retagged or consumed. Authority to read or reuse a payload SHALL NOT be inferred from CAS deduplication. Provenance and diagnostics SHALL respect all applicable source visibility and reuse restrictions.

The service SHALL classify pre-feature publication rows as legacy ordinary at cutover, without changing their manifests. A legacy row without a server migration record SHALL NOT gain migration authority or be refused solely because its caller-written `source.metadata.migration` key exists. For new publications, only the server's migration path SHALL set that key. A managed migration SHALL have consistent server-classified origin, marker and record; missing or contradictory required fields SHALL NOT fall back to legacy classification.

On roll-forward after rollback to a writer that predates origin classification, the service SHALL enroll as legacy ordinary only verified ordinary publications created in the fenced rollback interval. It SHALL verify that each enrolled row is absent from the trusted pre-rollback inventory and has no server migration record or origin class. It SHALL preserve the manifest bytes and treat caller-written markers as inert. A pre-existing or unproven row with missing provenance SHALL NOT be reclassified. If the operator cannot isolate managed publications from old writers and establish exact rollback origin, publication SHALL stay disabled during rollback.

#### Scenario: Result and origin remain separate
- **WHEN** an authorized caller registers an accepted combination using an exact matching report
- **THEN** its new publication resolves to the replacement-base payloads followed by the kept payloads in original order, with linked immutable provenance
- **AND** the original remains readable, pullable and runnable under its existing runtime and access checks

#### Scenario: A time-limited check can be registered
- **WHEN** a check ends with an authoritative incomplete report and an authorized caller submits its exact unexpired pin with dedicated acknowledgement
- **THEN** the service admits registration if all mandatory checks pass, without replacing the report by rerunning the time-limited inspection

#### Scenario: Stale or repeated submission
- **WHEN** the supplied report identity does not match the service's result for the exact inputs
- **THEN** no new publication becomes visible even if acknowledgement is supplied
- **WHEN** the same authorized caller replays its identical committed request
- **THEN** it returns the same publication without a second write; reusing that request identity with different intent or inputs is rejected

### Requirement: The launch authority binds the admission decision

Before runtime side effects, every fresh run or instance creation from a managed migration SHALL bind the admission decision to the exact publication UUID, manifest digest and sealed effective risk. The binding SHALL survive alternate managed selectors and relabeling; a consumer SHALL NOT run different lower bytes under the binding. Exact transport replay and asynchronous phases of one already admitted launch SHALL NOT be treated as new runs. A separate launch SHALL NOT inherit another operation's admission.

The gate SHALL apply to server-recorded migration lineage. The service SHALL compute the effective risk verdict as the strictest of the new comparison verdict and any validated effective verdicts of the existing combination and replacement base, in descending order `incomplete`, `conflict`, `clean`. The inspection-only old base SHALL NOT add a runtime verdict. The enriched, sealed report SHALL include the effective verdict and exact contributing migration bindings with prior report digests, without changing the pure comparison verdict, acknowledgement field, findings or counts. The service SHALL retain the prior digests through authenticated provenance links and revalidate the contributing seals at registration. A clean comparison SHALL NOT clear an inherited non-clean gate, including from the existing combination when the kept suffix is empty.

An extension or other non-migration server-derived publication of an effectively non-clean runtime ancestor SHALL be refused even with acknowledgement. Ordinary publication authorized to reuse payloads by digest SHALL retain its existing behavior. It SHALL NOT be labeled a managed migration or confirmed clean solely because its payloads match.

Starting or unshelving a stopped or shelved retained instance SHALL be a fresh run, with fresh acknowledgement for an effectively non-clean binding before any provider or lifecycle mutation. An omitted automatic workload restart policy for an effectively non-clean result SHALL resolve to `no`; an explicit `unless-stopped` SHALL be refused even with acknowledgement. The service SHALL NOT emit an automatic restart directive or capability for an effectively non-clean result. Effectively clean and ordinary images SHALL retain their existing restart rules.

#### Scenario: Non-clean result needs new intent at every surface
- **WHEN** a prior acknowledgement allowed registration of an incomplete or conflicting combination
- **THEN** CLI, direct Hub API and dashboard deny a fresh launch without a new dedicated acknowledgement, even when a generic confirmation is supplied
- **AND** providing the dedicated acknowledgement allows only the compatibility-risk step after the ordinary checks pass

#### Scenario: Extensions do not drop a non-clean gate
- **WHEN** a publisher requests a server-derived extension of a conflicting or incomplete migrated publication
- **THEN** the service refuses to publish the extension even with acknowledgement

#### Scenario: Re-migration cannot clear inherited risk
- **WHEN** a caller re-migrates an effectively conflicting combination from its exact recorded base to a different admissible base and the new comparison is clean
- **THEN** the new comparison stays `clean`, but the effective risk stays `conflict`, the committed report links the prior report digest, and registration and each fresh run require their own acknowledgement

#### Scenario: A different prefix match is not the recorded migration base
- **WHEN** a caller supplies an old base that matches the prefix but not the managed combination's recorded replacement-base pin
- **THEN** the service refuses the migration regardless of acknowledgement

#### Scenario: An unchanged base is not a replacement
- **WHEN** the old and replacement base pins have the same manifest digest or identical ordered payload digests, even under different UUIDs and labels
- **THEN** the service refuses the migration regardless of acknowledgement

#### Scenario: A legacy label is not migration authority
- **WHEN** a legacy ordinary image has a caller-written `source.metadata.migration` key and no server migration record
- **THEN** it retains its ordinary read and launch behavior
- **AND** a managed image missing required migration provenance does not become ordinary

#### Scenario: Resume needs fresh intent and automatic restart is unavailable
- **WHEN** a retained non-clean instance is stopped or shelved after an acknowledged create
- **THEN** a resume without new acknowledgement does not start or unshelve it
- **AND** a non-clean create with an omitted restart policy starts with restart disabled, while explicit `unless-stopped` is refused

#### Scenario: Acknowledgement cannot widen authority
- **WHEN** authentication, ownership, visibility, a payload integrity check, quota, OS support or platform admission would fail
- **THEN** the request remains refused with no registration or launch even with dedicated acknowledgement

### Requirement: Product surfaces explain limits and expose original reuse

The CLI, Hub API and dashboard SHALL display or return the canonical structural-only statement, both verdicts, inherited risk when present, coverage, separate package-state conflict count for the current comparison and release-change status. They SHALL NOT represent a clean comparison or zero observed package-state conflicts as proof of compatibility, including ABI, package dependencies or runtime behavior. They SHALL state that runtime verification remains the user's responsibility. They SHALL distinguish the new and existing identities and show an authorized route to read, pull or run the original under its existing rules without an overwrite, rollback or recreate of the original artifact.

#### Scenario: Clean result and unchanged original
- **WHEN** a clean migration is accepted and its result is shown
- **THEN** the result exposes the original's distinct identity and applicable authorized reuse paths, separately identifies the new result, includes the package-state conflict count and explains that runtime verification remains the user's responsibility

## MODIFIED Requirements

### Requirement: Verdicts and fixed finding bounds

The report SHALL retain the first 2000 conflict-or-incomplete findings as one group, then the first 2000 notices separately. Within each group order SHALL be layer index, path by Unicode code point, then code by Unicode code point. Bounds SHALL not be caller-configurable. Counts SHALL include all findings before cuts. `findings_truncated` SHALL be true if either group is cut.

The pure comparison `verdict` SHALL be `incomplete` when `complete=False`, any incomplete finding exists, or conflict-or-incomplete findings are cut. Otherwise it SHALL be `conflict` if conflicts exist, else `clean`. Cutting notices alone SHALL not change `verdict`. The pure `requires_risk_acknowledgement` field SHALL be true exactly when the pure comparison verdict is not `clean`; it describes only this comparison and SHALL NOT be the admission decision. An unenumerable image SHALL produce incomplete input, not a claim of completeness.

Surfaces and compatibility status adapters SHALL distinguish comparison evidence from the sealed effective gate. They SHALL NOT present a comparison `clean`, a false pure acknowledgement flag, or a `PASSED` filesystem comparison stage as clearance to register or execute a managed migration.

#### Scenario: Notices do not suppress conflict details
- **WHEN** 2001 notices and one conflict exist
- **THEN** the conflict precedes 2000 notices, counts remain complete, truncation is true and the comparison verdict is `conflict`

#### Scenario: A passed filesystem stage is not launch clearance
- **WHEN** complete evidence yields a clean comparison and a passed filesystem stage, but the sealed effective verdict is non-clean
- **THEN** the surfaces distinguish the passed comparison from the non-clean gate and still require dedicated acknowledgement

### Requirement: Risk acknowledgement is explicit, specific and narrow

The service SHALL use the exact sealed `effective_risk_verdict`, not the pure comparison verdict or its `requires_risk_acknowledgement` field, to decide whether a managed migration needs acknowledgement. An otherwise admissible registration or fresh run or instance creation SHALL require acknowledgement exactly when its effective verdict is not `clean`. Unavailable or invalid required provenance SHALL NOT be treated as `clean`. A migration registration, or a fresh run or instance creation from a publication the server classifies as a managed migration, whose migration provenance is missing, malformed, contradictory or not backed by a valid sealed report, or that fails any hard check, is not otherwise admissible: it SHALL be refused regardless of acknowledgement and SHALL NOT be treated as an ordinary image.

Compatibility risk SHALL be acknowledged through the dedicated control: the CLI flag `--yes-i-understand-the-risk`, the Hub API's strict JSON boolean `acknowledge_compatibility_risk: true`, and a dashboard confirmation that states the same risk for the exact selected result and operation. The three SHALL have the same meaning. No general force, yes, confirm, recreate option or prior operation's recorded acknowledgement SHALL count. Acknowledgement SHALL NOT bypass authentication, authorization, ownership, visibility, payload integrity, quota/resource limits, supported-OS, platform or separate operator checks, and SHALL NOT turn a refused migration into an accepted one.

#### Scenario: Conflict without acknowledgement
- **WHEN** the effective risk verdict is `conflict` and the caller passes only a general force or yes option
- **THEN** the migration is blocked with the report available to the caller

#### Scenario: Acknowledged conflict
- **WHEN** the effective risk verdict is `conflict`, all mandatory checks pass, and the caller acknowledges the compatibility risk
- **THEN** the migrated image is registered with acknowledgement recorded, and every later fresh run requires the acknowledgement again

#### Scenario: Acknowledgement does not widen authority
- **WHEN** the caller acknowledges the risk but lacks permission, exceeds quota or names an unsupported OS
- **THEN** the request fails exactly as it would without the acknowledgement

#### Scenario: Invalid provenance is refused even with acknowledgement
- **WHEN** a registration or fresh run names a managed migration whose provenance is missing, malformed, contradictory or not backed by a valid sealed report, and the caller acknowledges the risk
- **THEN** the request is refused before any write or runtime side effect, and the image is not treated as ordinary or clean
