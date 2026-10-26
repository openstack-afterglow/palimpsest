## ADDED Requirements

### Requirement: Caller-visible complete named bundle leaves

`GET /v1/catalog/assets` SHALL use the original caller's existing native inventory authority and public/site/owned-project/shared visibility. It SHALL return the agreed AssetPage DTO, ordered by name/digest, bounded limit1–100/default50 and nonnegative offset, and optional literal case-insensitive name substring q of at most128 characters. Only named SquashFS leaves with complete visible acyclic ancestry and any declared visible valid cloud-image base SHALL appear. Download policy SHALL be independent and represented truthfully. No private descendant/ancestor, owner, stored configuration or origin-job disclosure is permitted.

Asset SHALL additionally expose local_supported and local_reason, independent of chain_complete/download_allowed. Metadata local support SHALL require a visible raw/qcow2 base and known matching x86_64/aarch64 architecture throughout base and chain. Missing base SHALL preserve discoverability of a complete reusable chain but set local_supported=false/local_reason=base-image-required. Unsupported base format, unknown architecture or inconsistent architecture SHALL respectively report base-image-format-unsupported, architecture-unknown or architecture-mismatch. Metadata eligibility SHALL NOT assert compatible caller host, actual bytes or runtime readiness. size_bytes SHALL sum layer content plus visible included base content, excluding archive metadata.

Pagination SHALL apply offset/limit to ordered SQL candidate leaves before ancestry validation. At most limit candidate rows SHALL be validated, with one SQL lookahead for has_more. Dropped invalid/private chains SHALL NOT trigger a scan of earlier or later candidates to fill the page. When has_more is true, next_offset SHALL equal offset plus candidate rows examined and SHALL advance even when items is empty. Clients SHALL follow next_offset rather than infer completion from items length. Every catalog ancestry walk SHALL reuse the native runtime graph MAX_DEPTH64; exceeding it SHALL explicitly fail413 rather than silently omit the over-limit graph.

#### Scenario: Empty candidate page before a valid later leaf
- **WHEN** all candidates on an early page have invalid/private ancestry but later candidates exist
- **THEN** items is empty, has_more is true and next_offset advances across examined candidates
- **AND** the next requests can reach the valid leaf without rescanning earlier candidates.

#### Scenario: Public ancestor has a private child
- **WHEN** a visible leaf has a child invisible to this caller
- **THEN** the visible leaf remains discoverable and no private child identity/count is returned.

#### Scenario: Hidden, missing or cyclic dependency
- **WHEN** a named candidate's required chain or declared base cannot be completely resolved within caller visibility
- **THEN** it does not appear as a complete asset; neither hidden identity nor a fabricated completion is returned.

### Requirement: Fresh human global operator inventory

`GET /v1/admin/catalog/layers` and `GET /v1/admin/catalog/layers/{digest}` SHALL require fresh native `require_global_admin`, sharing existing require_admin human/system validation and requiring `system:manage`, never service credentials or caller-role claims. Genuine system/unscoped humans SHALL NOT require a fabricated project target; original identity, home-domain enablement, service/delegation vetoes and native system authority remain mandatory. Present original project enablement SHALL still be validated. Global policy SHALL evaluate only system management with no made-up project credential or target, and existing target-bound endpoints SHALL remain unchanged. The list SHALL return CatalogLayerPage with source hub and bounded q/limit/offset; detail SHALL return CatalogLayerDetail with true root-to-parent ancestors excluding self, at most50 direct children plus children_has_more, and empty Hub profile bundles. Missing/cyclic chains SHALL be incomplete. Only these new operator endpoints broaden inventory globally.

Global operator visibility labels SHALL be absolute public/site/shared/private and independent of the original caller's project scope; existing consumer project-relative labels are unchanged.


#### Scenario: Authority revoked between reads
- **WHEN** a prior administrator loses system authority, changes subject or is denied system policy
- **THEN** the next list and detail requests fail before global data is returned.

### Requirement: Output-linked provenance without invented attestation

Recorded build origin SHALL require a completed server Hub-build output link; recorded Glance export SHALL require a completed image-export result link. Caller-provided metadata SHALL NOT establish attestation. Unlinked legacy registration origin SHALL remain unknown/unrecorded when durable registration evidence does not exist. DTOs SHALL exclude recipes, credentials, config_json and internal paths.

#### Scenario: Forged client build metadata
- **WHEN** an uploaded row contains client build/source keys but no completed server job/output link
- **THEN** origin remains unknown and those keys are not disclosed.

### Requirement: Search-attenuated private package inventory

The existing package inventory SHALL optionally match package name or tag with bounded case-insensitive literal substring q. SQL wildcard/escape characters SHALL be escaped. Project, namespace, package-type, key-scope and inventory-action ceilings SHALL remain. Nonempty search SHALL bind into pagination; absent/empty query SHALL preserve existing cursor compatibility and response shape.

#### Scenario: Search cursor reused with another query
- **WHEN** a caller reuses a nonempty-search cursor without that normalized query or with another query
- **THEN** the cursor is rejected and no other inventory is disclosed.

### Requirement: Read-only metadata projection

New catalog reads SHALL make only SQL reads and SHALL NOT synchronize CAS, bootstrap/migrate, call external registries or cloud services, or expose native VM operations. Structural metadata readiness SHALL NOT claim payload existence or runtime qualification. Existing bundle export remains the payload-validation authority.

#### Scenario: Metadata is ready but runtime is unqualified
- **WHEN** catalog metadata describes a complete compatible chain
- **THEN** metadata eligibility does not assert that CAS bytes, caller backend or a guest are ready
- **AND** new routes require a matching reviewed Hub rollout rather than presumed release availability.

### Requirement: Local verified bundle unpack

Working source SHALL expose `palimpsest bundle unpack ARCHIVE --output DIRECTORY` using the existing atomic verified layout extractor without Hub authentication or network calls. Output SHALL be absent or empty; existing nonempty output SHALL remain intact on rejection. Only canonical empty native blobs and blobs/sha256 directory markers SHALL be admitted without applying archive permissions; duplicate/noncanonical/link/special entries, malformed input and digest failures SHALL NOT publish a partial destination. Verified bundles SHALL resolve their matching declared base and ordered layers without manual layer selection. Documentation SHALL distinguish working source from published0.3.5, local metadata eligibility from actual host/base/architecture readiness, and synthetic-byte qualification from runtime VM proof.

#### Scenario: Native package archive contains canonical directory markers
- **WHEN** a valid native runtime-bundle archive includes canonical empty directory markers
- **THEN** source CLI verifies/unpacks it and runtime resolution finds the included base and ordered layers automatically
- **AND** no guest execution or published-client availability is inferred from extraction success.

#### Scenario: Unsafe or corrupted archive
- **WHEN** an archive contains unsafe/duplicate members, malformed bytes or digest mismatches
- **THEN** unpack fails and no partial destination is published.

