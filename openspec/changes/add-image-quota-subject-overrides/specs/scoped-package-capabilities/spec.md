## ADDED Requirements

### Requirement: Audited subject-specific published-image quota overrides

The existing tier policy, publication visibility, current-owner authority, unique-name counting and chain/source accounting requirements SHALL remain in force. A fresh human system administrator with a project-scoped original token and `system:manage` SHALL be able to set and remove overrides of `max_images`, `max_chain_bytes` and `max_source_bytes` for an exact user (private/public) or project (internal), including free-tier subjects. Overrides SHALL NOT relabel billing tiers, change global policy/seed rows or affect another subject. All three fields SHALL be required; null field values SHALL mean unlimited. Removing the override SHALL restore the current tier policy. Lowering SHALL never delete existing publications; subsequent admission SHALL use effective limits, retaining the existing-name count exception. Every set/removal SHALL append an immutable revision recording administrator user, UTC time and old/new override values. Null old/new objects SHALL denote tier inheritance, not unlimited fields. The latest revision SHALL supply the effective policy to both publication `enforce` and usage accounting. Admin and subject-own usage SHALL expose effective values and `source: tier|override`. Admin changes SHALL serialize with publication check-and-insert through the same global image mutex. Bounded history, set and removal routes under `/v1/admin/image-quota/` SHALL require `system:manage`; ordinary members, delegated/machine credentials and system/domain-scoped tokens SHALL NOT acquire that authority. Supported SDK and CLI SHALL expose this procedure without automatic mutation replay.

#### Scenario: Raise one free user's cap, then restore inheritance

- **GIVEN** two free-tier users inherit the private count limit of two
- **WHEN** an authorized human admin sets one user's private override to three
- **THEN** enforce and both usage views use three with source `override` only for that user
- **AND** the other user's private limit, public scope and tier policies remain unchanged.
- **WHEN** the admin removes the override
- **THEN** current tier limits return with source `tier`, and audit history retains both attributed changes.

#### Scenario: Lower limits without deleting images

- **GIVEN** a subject has publications above a newly lowered override
- **WHEN** usage is read
- **THEN** existing publications remain and usage reports over-limit state
- **AND** only new admission is judged against effective count/chain/source limits.

#### Scenario: Additive rollout and application rollback

- **WHEN** an operator performs the reviewed offline bootstrap after writer drain and paired SQL/CAS backup
- **THEN** bootstrap creates an independent append-only override table without changing existing tier or image rows.
- **WHEN** the old application is restored without deleting that table
- **THEN** it remains schema-compatible but enforces tier-only limits; operators reconcile that admission change before reopening ingress.
