# Scoped package capabilities

## Purpose
Separate authorized package metadata, content download, publishing and owned key administration while attenuating tokens and delegated keys to current Keystone owner authority.
## Requirements
### Requirement: Service-local policy over trusted current Keystone roles
Hub SHALL evaluate registered service-local oslo.policy actions against the intersection of original-token and fresh current-owner authority. Exact unique global role IDs and their current Keystone implication graph SHALL establish trusted role names; domain aliases, ambiguous bindings, partial collections and unavailable metadata SHALL fail closed. Service grades (`palimpsest_reader`, `palimpsest_user`, `palimpsest_editor`, `palimpsest_admin`) SHALL work directly without requiring service implication edges or baseline member/reader grants. Exact fine-grained Palimpsest leaves SHALL remain independently usable. Ordinary native member/reader alone SHALL grant no service actions except for a separately verified personal-project owner. Project admin/manager grants SHALL be evaluated by policy, not confused with system authority; project manager has no default service grant. Capability labels SHALL project allowed actions, not fabricate Keystone roles.

#### Scenario: Direct native token grades
- **WHEN** a reader, user, editor or admin calls native Hub directly
- **THEN** reader can access authorized metadata/manifests but not content or credentials; user additionally downloads but cannot write; editor additionally publishes and issues subset keys; admin additionally revokes own keys
- **AND** exact project/namespace and original caller identity remain enforced.

#### Scenario: Independent leaves
- **WHEN** a publish-only or key-editor-only principal calls an action
- **THEN** only the named leaf capability is granted; publish does not grant download and key issue does not grant publish.

#### Scenario: Grade-only authority and operator policy changes
- **WHEN** a service grade remains assigned but its service implication edges are absent
- **THEN** its registered default actions remain available without synthesized leaves.
- **WHEN** an operator YAML override denies an action or the current trusted role is revoked
- **THEN** retained tokens and keys lose that action while unrelated authorized actions remain available; key ceilings SHALL also constrain operator grants.

#### Scenario: Invalid operator policy never retains a stale grant
- **WHEN** a configured non-default policy file is missing, a policy document is malformed, or native parser/rule validation fails
- **THEN** authorization SHALL fail closed with 503 and discard the failed enforcer cache rather than retain partially loaded or stale grants.


### Requirement: Current authority limits tokens and keys
Hub SHALL revalidate current effective named Keystone assignments and enabled owner/project using its separate read-only validator, which SHALL reject administrative, manager or service validator credentials. Issue-time key actions SHALL be a subset of both original-token and current owner actions. Use-time key authority SHALL intersect declared key actions with fresh current owner actions. Package keys SHALL never delegate key administration, system management or metadata deletion. Ordinary keys SHALL never be upgraded into human-system keys by a later owner promotion; explicitly human-admin-issued keys SHALL retain server-owned issuer provenance and revalidate that human's current system authority.

#### Scenario: Downgrade during upload
- **WHEN** a publisher's role is downgraded after starting a transfer
- **THEN** the same token and key cannot publish, append, finalize or change tags beyond current authority
- **AND** still-authorized read actions remain available.

#### Scenario: Namespace and own-key isolation
- **WHEN** a caller accesses another namespace, an undelegated package, or another owner's key
- **THEN** existing scope/ownership checks deny the request even for a service admin.

#### Scenario: Native key owner is promoted on system-all

- **WHEN** an existing ordinary package-key owner receives a direct or current-group system-all admin, manager or service assignment that project-oriented effective expansion omits
- **THEN** separate raw system lookups deny ordinary member-key use and upload finalization
- **AND** a verified human system administrator MAY separately use the explicit administrative token path or deliberately issue a new bounded key; an old ordinary key SHALL not inherit it.


### Requirement: Preserve global and credential boundaries

Hub SHALL preserve verified-human-system-admin builder/GC gates without granting service grades, project admin/manager grants or package keys global authority. Native package writes MAY use a bounded package key, a currently authorized original human project token, or an independently revalidated human system administrator with a logical Hub target. System recognition SHALL use fresh raw exact-system user grants, current group membership and raw exact-system group grants through the separate reader and trusted role-ID inference, never project-oriented effective expansion. Human authentication methods and original delegation/binding/access-rule restrictions SHALL be checked; service/trustee/validator identities and machine/delegated credentials SHALL not become human administrators. Logical targets SHALL never exchange or re-scope the original OpenStack token.

#### Scenario: Service admin and package key cannot launch

- **WHEN** service admin tokens or package keys call global builder/GC or VM operations
- **THEN** no new authority is granted.

#### Scenario: Raw system authority survives project-oriented expansion

- **WHEN** a human subject has a direct or current-group system-all admin grant, including a uniquely bound global role implying admin, while effective expansion omits system grants
- **THEN** Hub recognizes current system authority through raw reader lookups without exchanging the original token
- **AND** an unscoped or system-scoped human SHALL supply an explicit logical target for project-local management, while genuinely global catalog reads SHALL require no fabricated target.

#### Scenario: Revocation removes global authority despite project admin

- **WHEN** the last actual direct/group system-all admin path is removed while the original token retains a project admin role
- **THEN** the next validation no longer recognizes a system administrator and privileged requests are denied; missing or incomplete native metadata SHALL remain failures, not cached grants.

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
- **AND** the report distinguishes the smoke-only SQLite factory/lifespan-off setup from canonical MySQL bootstrap/deployment, records canonical bootstrap observations, and does not claim real cloud or build-executor execution.

### Requirement: Original-token console capability projection

Hub SHALL expose `GET /v1/me` with original subject identity and roles, no token/password/auth-reference fields, and `Cache-Control: no-store` on success and errors. Independent inventory/download/upload flags SHALL project fresh service-local allowed actions. Human system administrators SHALL use fresh policy-controlled authority and additionally receive an explicit logical target identity when applicable; administrative upload SHALL not require fabricated service membership. Build/delete flags SHALL require separately verified system-management authority. Membership denial SHALL expose no member capabilities, and identity outages SHALL remain failures. `GET /v1/policy/capabilities` SHALL expose current actions and capability labels, including an empty action set for an authenticated unentitled ordinary member.

#### Scenario: Retained token loses membership

- **WHEN** current member assignments are revoked while the original subject token remains valid
- **THEN** the identity and original roles remain unchanged but member capability flags are false on the next `/v1/me` request.

#### Scenario: Publish-only repeat upload

- **WHEN** a publish-only caller starts an upload for an already registered visible digest
- **THEN** the server creates a normal upload session, verifies the submitted bytes and metadata at finalization, and does not return an inventory descriptor; only an inventory-authorized caller can take the verified-descriptor completion shortcut.

### Requirement: Personal ownership and explicit human administrative authority

Personal ownership SHALL require fresh enabled native user, project and domains; the exact project marker `afterglow_personal_user_id` SHALL identify the caller, and the project domain SHALL be exactly `Personal` with an absent or exact `personal` domain-kind marker. A unique non-escalating global member role and direct, non-group, non-inherited membership provenance SHALL establish personal authority. Personal ownership MAY grant project-local policy actions, including nondelegable metadata deletion, but SHALL never grant system management. Policy denies and protected/service/delegated identity boundaries SHALL remain effective.

#### Scenario: Personal member grants remain bounded

- **WHEN** the original human member token and fresh native personal-project provenance agree
- **THEN** project-local default actions are available without service grades, while system operations remain denied
- **AND** bounded keys retain exact package/action ceilings, cannot manage keys or delete metadata, and lose authority when personal ownership or original credentials are revoked.

#### Scenario: Human system administrator targets a project

- **WHEN** a fresh verified human system administrator selects an enabled logical target project
- **THEN** policy-authorized native and legacy publication uses that target while audit attribution records the actual human and original token scope remains unchanged
- **AND** explicitly issued keys retain bounded actions, exact namespace/package ownership and server-owned human issuer provenance; key lists/revocation remain own-key-only.

#### Scenario: Service grade and delegated credentials never become system authority

- **WHEN** project service grades, tenant administrative grants, machine credentials, Trusts or package keys request global build/GC or a foreign logical target
- **THEN** no system or cross-project authority is inferred.

#### Scenario: Deferred Trust export keeps its independent boundary

- **WHEN** a human has publication authority but carries administrative or service roles, system authority, or a logical target different from the original token project
- **THEN** deferred Trust-export admission SHALL remain denied; native/legacy publication and human-admin key features SHALL not relax separately qualified export ownership, delegation or worker credential isolation.


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

