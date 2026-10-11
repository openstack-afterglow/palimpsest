# Scoped package capabilities

## Purpose
Separate authorized package metadata, content download, publishing and owned key administration while attenuating tokens and delegated keys to current Keystone owner authority.
## Requirements
### Requirement: Exact Palimpsest service capability matrix
Hub SHALL accept exact effective `palimpsest-inventory_reader`, `palimpsest-download_user`, `palimpsest-publish_editor`, `palimpsest-keys_editor`, `palimpsest-keys_admin` leaves. Parent assignments (`palimpsest_reader`, `palimpsest_user`, `palimpsest_editor`, `palimpsest_admin`) SHALL resolve through Keystone's current role-ID implication DAG, not hardcoded bundles. Presets initially link lower grades and their leaves, but operators can remove edges. Role bindings SHALL use a fresh complete directory of unique global IDs. Baseline effective member SHALL be required for non-reader leaves; inventory SHALL additionally accept baseline reader. Project/native roles alone grant no service entitlement, and domain aliases, ambiguous bindings or unavailable directory/graph SHALL grant no authority.

#### Scenario: Direct native token grades
- **WHEN** a reader, user, editor or admin calls native Hub directly
- **THEN** reader can access authorized metadata/manifests but not content or credentials; user additionally downloads but cannot write; editor additionally publishes and issues subset keys; admin additionally revokes own keys
- **AND** exact project/namespace and original caller identity remain enforced.

#### Scenario: Independent leaves
- **WHEN** a publish-only or key-editor-only principal calls an action
- **THEN** only the named leaf capability is granted; publish does not grant download and key issue does not grant publish.

#### Scenario: Removed implication with retained parent
- **WHEN** an operator removes a parent-to-leaf edge while the parent remains assigned
- **THEN** the corresponding feature is revoked for the existing token and key unless another current actual assignment path grants it.

### Requirement: Current authority limits tokens and keys
Hub SHALL revalidate current effective named Keystone assignments and enabled owner/project using its read-only validator. Issue-time key actions SHALL be a subset of both original-token and current owner capabilities. Use-time key authority SHALL be the intersection of delegated actions and current owner capabilities.

#### Scenario: Downgrade during upload
- **WHEN** a publisher's role is downgraded after starting a transfer
- **THEN** the same token and key cannot publish, append, finalize or change tags beyond current authority
- **AND** still-authorized read actions remain available.

#### Scenario: Namespace and own-key isolation
- **WHEN** a caller accesses another namespace, an undelegated package, or another owner's key
- **THEN** existing scope/ownership checks deny the request even for a service admin.

### Requirement: Preserve global and credential boundaries

Hub SHALL preserve verified-system-admin global builder/GC gates and key-only native package writes, without granting service admin global authority or allowing package keys to authorize OpenStack/VM launch. System administrator recognition SHALL use a fresh direct user/system-all assignment query through the separate reader, not project-oriented effective expansion, and SHALL still require an exact subject user, system-all scope and named admin role.

#### Scenario: Service admin and package key cannot launch

- **WHEN** service admin tokens or package keys call global builder/GC or VM operations
- **THEN** no new authority is granted.

#### Scenario: Direct system administrator survives project-oriented expansion

- **WHEN** a project-scoped subject user has a direct admin assignment on system-all while effective expansion omits that assignment
- **THEN** Hub recognizes the administrator through the direct reader lookup without exchanging or re-scoping the original subject token.

#### Scenario: Revocation removes global authority despite project admin

- **WHEN** the direct system-all admin assignment is removed while the original token retains a project admin role
- **THEN** the next validation no longer recognizes a system administrator and privileged requests are denied with 403.

### Requirement: Isolated HTTP regression setup
Regression cases SHALL exercise actual native FastAPI HTTP routes and a synthetic HTTP Keystone boundary with temporary SQL/CAS, without production Hub mutation.

#### Scenario: Safe smoke execution
- **WHEN** the integrating owner executes the documented smoke setup
- **THEN** only loopback synthetic identity and temporary storage are used
- **AND** production identity, package data and deployment remain unchanged.

#### Scenario: Isolated registered-app container acceptance
- **WHEN** the scoped container runner executes the built API/worker images on linux/arm64 and linux/amd64
- **THEN** it exercises the unmodified registered Hub app through HTTP using the real SDK against synthetic current Keystone, real Redis and named SQLite/blob storage
- **AND** original upload/download bytes and owned range-resume bytes remain equal after container restart and recreation; inventory is not content-download authority, and inventory plus write publishes without read
- **AND** current-owner downgrade revokes issued keys and export tickets while retained inventory remains available; verified-system-only builder/GC gates remain separate
- **AND** the report distinguishes the smoke-only SQLite factory/lifespan-off setup from canonical MySQL bootstrap/deployment and records the observed SQLite connect_timeout incompatibility without claiming real cloud/KVM execution.

### Requirement: Original-token console capability projection

Hub SHALL expose `GET /v1/me` using the original project-scoped subject identity and roles, with no token/password/auth-reference fields and `Cache-Control: no-store` on success and errors. Its independent inventory/download/upload capabilities SHALL be computed from fresh current service leaves. Verified system administrators SHALL retain legacy inventory/download/build/delete authority without gaining upload or native member controls; membership denial SHALL expose no member capabilities, and identity outages SHALL remain failures.

#### Scenario: Retained token loses membership

- **WHEN** current member assignments are revoked while the original subject token remains valid
- **THEN** the identity and original roles remain unchanged but member capability flags are false on the next `/v1/me` request.

#### Scenario: Publish-only repeat upload

- **WHEN** a publish-only caller starts an upload for an already registered visible digest
- **THEN** the server creates a normal upload session, verifies the submitted bytes and metadata at finalization, and does not return an inventory descriptor; only an inventory-authorized caller can take the verified-descriptor completion shortcut.

### Requirement: Same-origin console credential isolation

The Hub console SHALL keep original Keystone tokens and native package keys in separate memory-only connection domains and SHALL use each only for its existing endpoint authority. Connection changes SHALL cancel tab-controlled transfers without automatically retrying publication or deleting server state. Download saving SHALL verify descriptor size and SHA-256 before committing bytes.

#### Scenario: Native key is not legacy authority

- **WHEN** a native package key is connected for native browsing or publication
- **THEN** it is never sent to legacy image/layer or original-member control endpoints, and disconnect or reload discards tab-held credentials and issued secrets.

#### Scenario: Native key pasted into the legacy connection

- **WHEN** a `ppk_v1_` native key is submitted in the legacy Keystone token field
- **THEN** the console clears the submitted secret and rejects it before assigning legacy credentials or sending an identity request.

#### Scenario: Server upload session disappears

- **WHEN** status, resume or abort returns HTTP 404 for the active legacy upload session
- **THEN** the console releases the local file/metadata controls and reports that the session is unavailable and finalization may already have committed, without automatically retrying publication.

### Requirement: Visible filtered management pages

Layer/image inventory SHALL apply visibility and supported metadata predicates before nonnegative offset and limit 1–200, ordered by descending unique row ID. Layer inventory SHALL support architecture, OS variant, disk format and Ubuntu base predicates in addition to existing digest/name/type fields; image inventory SHALL remain cloud-image-only.

#### Scenario: Filtered offset excludes unrelated private rows

- **WHEN** a caller advances a filtered page past its first matching row
- **THEN** unrelated metadata rows and unauthorized private rows do not consume offset, and visible rows retain their existing ownership redaction.

