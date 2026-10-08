## Why

The Hub export worker previously authenticated the `OS_USERNAME`/`OS_PASSWORD` service identity scoped to a caller-selected tenant project (`get_admin_connection_for_project`). Every exported tenant therefore had to grant that service identity a project role it could use for any later work. The user approved replacing tenant membership of service identities with caller-authorized execution across Drover, Waygate, Lumen and Palimpsest. This record is the Palimpsest-local slice of the parent Afterglow change with the same name.

## What Changes

- **BREAKING**: deferred Glance image exports execute only through a bounded Keystone Trust created by the requester at admission: trustor = requester, trustee = service identity, project = admitted project, `impersonation=true`, least configured roles (default `member`, required by Glance's default download policy) and a finite expiry. Exports queued before rollout have no delegation and fail `delegation_required`.
- Remove `get_admin_connection_for_project` and every caller; the service password authenticates only to its own service project (to resolve the trustee ID) or as a trust trustee without project selectors.
- Persist only the verified delegation reference/scope in an additive `palimpsest_image_export_delegations` table; never the requester's token or password.
- Revalidate the requester's current enabled user/project, `palimpsest-publish_editor` authority and delegated role before worker I/O. Verify the trust token's ID, trustor, trustee, impersonated user, project, roles and expiry. Revoked, expired or swapped scopes fail terminally without fallback.
- Durable Trust cleanup covers terminal success/failure, soft deletion, exhausted attempts and abandoned admissions. Cleanup retries never rerun an export.
- Add delegated-role and TTL settings, Kolla defaults/precheck, and validator inputs for the export worker; update install/testing/architecture documentation.

## Capabilities

### New Capabilities
- `export-delegation`: requester-created, bounded Keystone Trust as the sole authority for deferred Hub Glance exports, with current-authority revalidation and durable cleanup.

### Modified Capabilities
None. `scoped-package-capabilities` requirements (native package original-token/key planes) are unchanged.

## Impact

Hub `openstack.py`, `services/image_exports.py`, `auth.py` (role closure helper), `config.py`, `models.py`, `migrate.py`, `worker.py`. Hub tests `test_export_delegation.py`, `test_image_exports.py`, `test_migrate.py`, and the `scripts/test_lanes.py` hub manifest. Kolla role defaults/precheck. Docs: install, testing, architecture, changelog and handoff. Operators need Keystone policy that allows trustors to create/delete their own trusts and trust-scoped authentication. No production Keystone, role assignment, deployment or image version change is part of this change.
