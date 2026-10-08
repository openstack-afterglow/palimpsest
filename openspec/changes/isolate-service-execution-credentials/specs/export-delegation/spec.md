## ADDED Requirements

### Requirement: Requester-created bounded export delegation
Admission of an export needing new Glance work SHALL create a Keystone Trust with the requester's validated original token. The Trust SHALL use trustor = requester, trustee = the service identity resolved from its own service-project authentication, project = the token's project, and `impersonation=true`. It SHALL delegate only configured non-administrative roles that the requester currently holds, with a finite expiry. Hub SHALL verify Keystone's response and persist only the Trust reference and verified scope, never a requester token or password. Byte reuse SHALL NOT create a Trust.

#### Scenario: Queued export binds its own Trust
- **WHEN** a current publisher requests an export that needs Glance work
- **THEN** the queued export is bound to a new Trust with exactly that requester, project, service trustee, impersonation, least roles and finite expiry.

#### Scenario: Wider or unpossessed delegation is refused
- **WHEN** Keystone returns a Trust wider than requested, or the requester does not currently hold the delegated role
- **THEN** admission fails, any created Trust is deleted and no export is queued.

### Requirement: Deferred I/O only within current admitted delegation
Before Glance I/O, the worker SHALL revalidate the requester's current enabled user and project, export publish authority and delegated roles. The worker SHALL authenticate only by trust ID without project selectors. It SHALL verify the trust token's trust ID, trustor, trustee, impersonated user, project, roles and expiry against the stored delegation. Revoked, expired, swapped or missing delegations SHALL fail terminally. The worker SHALL NOT fall back to a service password scoped to the tenant or to any role assignment.

#### Scenario: Revoked or swapped scope
- **WHEN** the requester loses publish authority or a delegated role, is disabled, the Trust is deleted, or the stored or returned scope differs
- **THEN** the export fails terminally before any Glance call and no tenant-scoped service authentication occurs.

#### Scenario: Legacy queued export
- **WHEN** an export queued before cutover has no active delegation
- **THEN** it fails `delegation_required` instead of executing.

### Requirement: Durable Trust cleanup
Hub SHALL durably queue Trust deletion when an export completes, fails, is soft-deleted, exhausts attempts or its admission is abandoned. Cleanup SHALL retry with backoff and SHALL be bounded by Trust expiry. A cleanup failure SHALL NOT rerun or modify the export result. A new export SHALL bind only the current requester's Trust and retire any earlier creator's delegation.

#### Scenario: Cleanup outage after success
- **WHEN** Keystone deletion fails after a successful export
- **THEN** the export stays complete, the delegation remains queued for cleanup and a later sweep deletes it without downloading again.

#### Scenario: Abandoned admission
- **WHEN** a Trust is created but admission does not bind it
- **THEN** it is deleted immediately with the requester token or recorded for worker cleanup; a crashed admission's pending record is swept after a bounded grace.

### Requirement: Unrelated execution domains unchanged
Native package original-token and package-key planes, local KVM runtime, build worker and CAS domains SHALL NOT accept trustee tokens or require cloud delegation.

#### Scenario: Package API remains original-token
- **WHEN** a native package route is called
- **THEN** authorization uses the original caller token or package key exactly as before.
