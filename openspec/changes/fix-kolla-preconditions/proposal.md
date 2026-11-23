## Why

The canonical Kolla reconfigure failed in Palimpsest database and Keystone preconditions. With ProxySQL enabled, the role attempted database administration as `kolla_root` instead of Kolla's shard-root user. Reader provisioning also omitted the named cloud profile that carries `auth_url`.

## What Changes

- Select Kolla's `root_shard_<id>` administrator when ProxySQL is enabled, and retain `database_user` when ProxySQL is disabled.
- Provide the Palimpsest database user's ProxySQL shard mapping from existing role credentials and the configured default shard.
- Pass `openstack_auth_cloud` to the Keystone reader modules when Kolla defines it, while preserving legacy Kolla's complete `openstack_auth` fallback.
- Synchronize root and Hub metadata, the root CLI version, the release guard, and the Kolla image default at 0.3.33; add regression coverage and deployment documentation.

## Capabilities

### New Capabilities

- `kolla-role-preconditions`: shard-aware database administration and complete Keystone cloud-profile provisioning.

### Modified Capabilities

- `ci-safety-and-performance`: validate the root CLI version with the immutable release tag.

## Impact

Only the root Kolla role defaults and precondition tasks change behavior. No Hub schema or runtime code changes. Shard-root provisioning must use Kolla's `mysql_user` module against a direct database backend before ProxySQL-routed Palimpsest preconditions; managed MariaDB uses its post-deploy task, while preconfigured external databases require an explicitly authorized toolbox operation. Release and live deployment remain separate gates.
