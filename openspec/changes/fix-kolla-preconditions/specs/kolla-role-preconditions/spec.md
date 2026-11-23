## ADDED Requirements

### Requirement: Use the configured MariaDB shard for Kolla database preconditions

The Palimpsest Kolla role SHALL use `root_shard_<id>` for database administration when ProxySQL is enabled, and SHALL retain `database_user` when ProxySQL is disabled. The role SHALL provide the Palimpsest service database user and shard mapping to Kolla's ProxySQL configuration role.

#### Scenario: ProxySQL-enabled shard administration

- **WHEN** the Palimpsest database preconditions run with ProxySQL enabled
- **THEN** database creation and user provisioning authenticate through the configured `root_shard_<id>` account
- **AND** ProxySQL routes the Palimpsest service user to the configured shard

#### Scenario: ProxySQL-disabled database administration

- **WHEN** the Palimpsest database preconditions run without ProxySQL
- **THEN** database creation and user provisioning use Kolla's `database_user`

### Requirement: Use the named Keystone cloud profile for reader provisioning

The reader identity and system-role assignment modules SHALL receive the existing `openstack_auth` mapping and SHALL pass `openstack_auth_cloud` when Kolla defines it. When the running Kolla version does not define that variable, the `cloud` argument SHALL be omitted so the complete legacy auth mapping remains usable.

#### Scenario: Kolla exposes the named cloud profile

- **WHEN** the role creates the dedicated reader identity and assigns its system reader role on Kolla 2026.1
- **THEN** both modules receive the configured auth mapping and named cloud profile
- **AND** credentials remain protected by `no_log` and TLS validation settings remain unchanged

#### Scenario: Kolla provides the complete legacy auth mapping

- **WHEN** the role runs on a Kolla version without `openstack_auth_cloud`
- **THEN** both modules omit the undefined `cloud` argument and use the complete `openstack_auth` mapping
