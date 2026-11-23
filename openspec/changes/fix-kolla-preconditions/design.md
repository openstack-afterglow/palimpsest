## Context

Kolla's stock database roles use `root_shard_<id>` for administrative SQL through ProxySQL and define per-service database users through `<service>_database_shard`. The Palimpsest role defaulted to `database_user` and did not define its shard mapping. Kolla's `openstack_auth` mapping carries password data, while `openstack_auth_cloud` supplies the selected clouds.yaml profile and `auth_url`.

## Goals / Non-Goals

**Goals:** Make Palimpsest database creation and user provisioning follow Kolla's shard contract; make both Keystone reader modules use the named cloud profile; preserve focused regression tests and release evidence.

**Non-Goals:** No database schema migration, Hub API change, credential rotation, manual SQL grant, or alternate authentication fallback.

## Decisions

- Derive `palimpsest_database_shard_id` from Kolla's `mariadb_default_database_shard_id`.
- Use `root_shard_<id>` only when `enable_proxysql` is true; otherwise use Kolla's `database_user`.
- Define the ProxySQL user mapping from the existing Palimpsest DB username/password variables. Operator-provided `palimpsest_database_shard` values continue to override role defaults.
- Pass `cloud: "{{ openstack_auth_cloud | default(omit) }}"` to both reader tasks. Kolla 2026.1 receives its named profile; older Kolla versions omit the undefined cloud parameter and continue using their full auth mapping.
- Synchronize client/Hub metadata and Kolla image defaults at 0.3.33; make tag validation and installed-package smoke reject a stale client CLI version.

## Risks / Trade-offs

The shard-root account has database-wide privileges. Provision it only through Kolla's `kolla_toolbox` `mysql_user` module: the stock MariaDB post-deploy path when Kolla manages the cluster, or a separately authorized direct-backend task when the database is preconfigured externally. The role's portable tests do not prove ProxySQL runtime state or Keystone API success; those require post-release Kolla checks.
