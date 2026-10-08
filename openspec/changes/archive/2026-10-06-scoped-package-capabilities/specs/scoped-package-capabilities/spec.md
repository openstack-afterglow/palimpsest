## ADDED Requirements

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
Hub SHALL preserve verified-system-admin global builder/GC gates and key-only native package writes, without granting service admin global authority or allowing package keys to authorize OpenStack/VM launch.

#### Scenario: Service admin and package key cannot launch
- **WHEN** service admin tokens or package keys call global builder/GC or VM operations
- **THEN** no new authority is granted.

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

