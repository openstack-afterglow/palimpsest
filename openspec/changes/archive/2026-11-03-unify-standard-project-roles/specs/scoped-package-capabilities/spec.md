## MODIFIED Requirements

### Requirement: Service-local policy over trusted current Keystone roles
Hub SHALL evaluate service-local policy against original-token and fresh current-owner authority under a mandatory standard native role ceiling. Current unique native reader/member/manager/admin roles SHALL authorize visible reads; member/manager/admin SHALL authorize project management, including package/cache publication, own-key issue/list/revoke, project builds and exact-project package/layer deletion. Existing canonical project roles and legitimate native group/domain/custom inference SHALL use the current native role-ID graph, not local aliases or new provenance filters. Legacy Palimpsest grades/leaves and personal-owner metadata alone SHALL NOT create authority. Capability labels SHALL remain internal presentation labels, not assignable IAM roles. Operator YAML SHALL narrow but SHALL NOT uplift the core role ceiling.

Native current owner-home and project domains SHALL remain enabled independently of personal metadata or role provenance. Disabled domains SHALL deny access; missing, mismatched or unknown enabled-state domain metadata SHALL fail closed as unavailable authority, not downgrade into ordinary member permission.

#### Scenario: Standard reader and member
- **WHEN** current native reader calls metadata/content/build-status routes
- **THEN** only existing visible authorized resources are returned
- **AND** reader cannot publish, delete, admit builds or issue/revoke keys, even with permissive operator rules.
- **WHEN** current native member/manager/admin calls project management routes
- **THEN** the role is sufficient without service grades, subject to original issued-role, current membership and resource boundaries.

#### Scenario: Disabled native domain cannot borrow standard membership
- **WHEN** an otherwise valid current core member's native home or project domain is disabled
- **THEN** original token and key-owner authorization deny403 without consulting personal-owner fallback.
- **WHEN** the required native domain enabled state is unavailable or unknown
- **THEN** authorization fails503 rather than authorizing a default role path.


#### Scenario: Native graph and retained data
- **WHEN** a canonical project/group/custom role has legitimate current native core-role implications
- **THEN** effective core authority remains usable without local role synthesis.
- **WHEN** only service-role names without effective core authority are present
- **THEN** default actions are denied and retained native IDs/assignments are not mutated.

#### Scenario: Original and current ceilings
- **WHEN** current membership is downgraded or original issued roles lack a new management role
- **THEN** intersected authority cannot exceed either ceiling and stored key package/action limits remain mandatory.

#### Scenario: Invalid operator policy never retains a stale grant
- **WHEN** configured policy is missing, malformed or invalid under native parser/rule validation
- **THEN** authorization fails closed with503 rather than retain stale or partially loaded grants.

### Requirement: Preserve global and credential boundaries
Hub SHALL retain genuine human system-only shared catalog/global policy/control-plane authority and machine-only build callback leases. Project-native build admission SHALL require write authority and project build reads SHALL require inventory; exact-project layer deletion SHALL require delete authority and additionally system authority for published shared-catalog rows. Foreign project/user private resources SHALL remain isolated. Native package writes MAY use bounded keys or authorized original human project tokens; keys SHALL NOT administer keys, delete metadata, select logical targets or launch builds. Recognized human federation including native `openid` SHALL NOT suppress Trust/application-credential/OAuth/bind/access-rule restrictions or mint replacement subjects.

#### Scenario: Member project flow cannot cross global boundary
- **WHEN** a member publishes, builds or deletes a project resource
- **THEN** only the exact project/owner-visible resource can be managed with fresh authority before effects/commit
- **AND** global catalogs, foreign project selection and machine worker claim/callback routes remain denied.

#### Scenario: Recognized federated human remains constrained
- **WHEN** a valid original human subject has native `openid` and `token` methods
- **THEN** ordinary and catalog validators recognize the method set while retaining actual system/project authority checks.
- **WHEN** any reduced credential marker is present
- **THEN** no broad human administration is gained, including an explicitly empty marker.

#### Scenario: Service admin and package key cannot launch
- **WHEN** a service-only principal or bounded package key requests global administration, machine worker claims or callbacks
- **THEN** neither receives global/system or machine lease authority.

#### Scenario: Raw system authority survives project-oriented expansion
- **WHEN** a human subject has a direct or current-group system-all admin grant, including a uniquely bound global role implying admin, while effective expansion omits system grants
- **THEN** Hub recognizes current system authority through raw reader lookups without exchanging the original token
- **AND** an unscoped or system-scoped human supplies an explicit logical target for project-local management, while genuinely global catalog reads require no fabricated target.

#### Scenario: Revocation removes global authority despite project admin
- **WHEN** the last actual direct/group system-all admin path is removed while the original token retains a project admin role
- **THEN** the next validation no longer recognizes a system administrator and global privileged requests are denied; missing or incomplete native metadata remains a failure, not a cached grant.


### Requirement: Original-token console capability projection
Hub SHALL expose original identity and fresh action-derived inventory/download/upload/build/delete flags without credentials and with no-store responses. Build and project deletion flags SHALL derive from project write/delete actions, not a system-only UI gate. Project readers SHALL see build status without build admission. Human system administrators SHALL retain explicit target identity where applicable. Native key and original-token console connections SHALL remain separate memory-only domains.

#### Scenario: Reader observes project build state
- **WHEN** a project reader connects to the Hub console
- **THEN** build status may be read but the build form and project mutation actions remain denied.

#### Scenario: Retained token loses membership
- **WHEN** current member assignments are revoked while the original subject token remains valid
- **THEN** identity and original roles remain unchanged but project capability flags are false on the next `/v1/me` request.

#### Scenario: Publish-only repeat upload
- **WHEN** operator policy permits publication but denies inventory and that caller starts an upload for an already registered visible digest
- **THEN** the server creates a normal upload session, verifies submitted bytes and metadata at finalization, and does not return an inventory descriptor; only an inventory-authorized caller can take the verified-descriptor completion shortcut.


### Requirement: Personal ownership and explicit human administrative authority
Personal-project metadata SHALL NOT be an authority fallback. Personal members SHALL use the same standard native core roles, current membership and credential/resource ceilings as other project members. Independent verified human system administrators MAY use explicit logical targets without fabricated requester project grants, while keys remain own-user scoped and attenuated. Deferred export admission SHALL preserve original project, bounded delegated roles and worker credential isolation without rejecting ordinary project manager/admin merely for that project role.

#### Scenario: Personal marker does not replace core membership
- **WHEN** personal metadata exists without native core membership
- **THEN** it grants no actions.
- **WHEN** a personal marker changes while legitimate current member authority remains
- **THEN** the marker alone does not revoke or expand core authority.

#### Scenario: Human target and deferred export remain distinct
- **WHEN** a human system administrator selects a logical target
- **THEN** original provider scope is unchanged and global authorization remains independently verified.
- **WHEN** an ordinary project member/manager/admin requests deferred export
- **THEN** only original-project bounded delegation is allowed; system/service/reduced credentials and foreign logical targets cannot borrow project export authority.

#### Scenario: Human system administrator targets a project
- **WHEN** a fresh verified human system administrator selects an enabled logical target project
- **THEN** policy-authorized native and legacy publication uses that target while audit attribution records the actual human and original token scope remains unchanged
- **AND** explicitly issued keys retain bounded actions, exact namespace/package ownership and server-owned human issuer provenance; key lists/revocation remain own-key-only.

#### Scenario: Service grade and delegated credentials never become system authority
- **WHEN** project service grades, tenant administrative grants, machine credentials, Trusts or package keys request global administration or a foreign logical target
- **THEN** no system or cross-project authority is inferred.

#### Scenario: Deferred Trust export keeps its independent boundary
- **WHEN** an ordinary project member/manager/admin requests publication under its original project
- **THEN** bounded delegated-role and current-owner checks still constrain export admission without a project-admin/manager veto.
- **WHEN** the subject is system/service/reduced or selects a foreign logical target
- **THEN** deferred export admission remains denied; project publication and key features do not relax export ownership, delegation or worker credential isolation.


### Requirement: Isolated HTTP regression setup
Regression cases SHALL exercise actual native FastAPI HTTP routes and a synthetic HTTP Keystone boundary with temporary SQL/CAS, without production Hub mutation.

#### Scenario: Safe smoke execution
- **WHEN** the integrating owner executes the documented smoke setup
- **THEN** only loopback synthetic identity and temporary storage are used
- **AND** production identity, package data and deployment remain unchanged.

#### Scenario: Isolated registered-app container acceptance
- **WHEN** the scoped container runner executes the built API/worker images on linux/arm64 and linux/amd64
- **THEN** it exercises the unmodified registered Hub app through HTTP using the real SDK against synthetic current Keystone, real Redis and named SQLite/blob storage
- **AND** original upload/download bytes and owned range-resume bytes remain equal after container restart and recreation; policy-attenuated inventory is not content-download authority, and independently authorized writes need not require inventory
- **AND** current-owner downgrade removes writes while retained reader content access remains available, and issued keys/export tickets lose authority when their required current native membership or declared ceilings no longer hold
- **AND** project build admission requires project write authority, while global GC/shared catalog gates and machine-only worker leases remain separate
- **AND** the report distinguishes the smoke-only SQLite factory/lifespan-off setup from canonical MySQL bootstrap/deployment, records canonical bootstrap observations, and does not claim real cloud or build-executor execution.

