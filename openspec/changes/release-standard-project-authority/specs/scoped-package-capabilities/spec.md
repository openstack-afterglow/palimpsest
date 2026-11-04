## ADDED Requirements

### Requirement: Standard native project role ceiling
Hub SHALL permit exact current native member, manager and project admin to manage owned project resources and reader-or-above to read nonsecret visible resources, without Palimpsest service roles or personal metadata. Native original-token and current-owner authority SHALL intersect. Operator policy SHALL only narrow that core ceiling. Exact current custom/group/domain implication paths SHALL survive without accepting domain-local builtin name lookalikes.

#### Scenario: Core roles without dedicated service roles
- **WHEN** an original human token and fresh membership carry member or manager
- **THEN** owned package/cache/layer management and project build admission are available while reader permits only authorized visible reads.

#### Scenario: Native domain implication is removed
- **WHEN** an exact domain-role path to a global core role is withdrawn
- **THEN** the next native request loses that authority; matching a domain role name never supplies it.

#### Scenario: Keys and global catalogs remain separate
- **WHEN** an issued package key or project manager requests key administration, metadata deletion, private foreign data, global catalog mutation or worker callbacks
- **THEN** original key scope, private ownership, human system authority and machine-lease gates remain enforced; no new privilege is inferred.

### Requirement: Truthful reduced credential projection
A valid reduced original project credential MAY query executable native capabilities without acquiring human-route authority. When human-only resource routes deny that credential, the projection SHALL return empty actions and capabilities. Validation SHALL still use the exact original token and reject its revocation.

#### Scenario: Reduced member subject
- **WHEN** a reduced original token has a member role but cannot execute human-only native routes
- **THEN** policy preview returns empty executable authority and resource control remains denied.

#### Scenario: Reduced original is revoked
- **WHEN** that original token becomes invalid
- **THEN** the preview rejects it rather than using current owner membership as a replacement identity.
