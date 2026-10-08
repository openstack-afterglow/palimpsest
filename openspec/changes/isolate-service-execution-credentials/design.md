## Context

`POST /v1/image-exports` already validated the original token (`require_token` → `get_os_conn`) and current publish authority (`_legacy_writer` → `get_package_member_info`). The asynchronous worker then called `get_admin_connection_for_project(job.project_id)`. That password-scoped the service identity into the tenant and depended on a tenant role assignment. Installed SDKs: keystoneauth1 5.15.0 (`v3.Password(..., trust_id=...)`, `AccessInfoV3.trust_*`), python-keystoneclient 5.4.0 (`TrustManager.create(trustee_user, trustor_user, role_names, project, impersonation, expires_at)`, `delete`), openstacksdk 3.3.0 (`Connection(session=...)`). The Keystone trust API source shows that trust-scoped tokens are not blocked from trust management. `identity:delete_trust` defaults to admin-or-trustor, and an impersonating trust token presents the trustor's user ID.

## Goals / Non-Goals

**Goals:** requester-scoped deferred Glance I/O; current-authority revalidation before each attempt; verified finite delegation; durable cleanup that survives every terminal or abandoned path; a new export independent of earlier creators.

**Non-Goals:** native package original-token/key planes, local KVM runtime, build worker, CAS, `/v1/builds`, Afterglow integration, Kolla registration of service roles, production rollout, or removal of existing manual tenant grants.

## Decisions

- **Trust at admission, only for queued Glance work.** `_write_transaction` raises `_DelegationRequired` only when it would queue or reset work. Byte reuse never creates a Trust. The Trust is created with the caller's validated `AccessInfoPlugin` session and recorded `pending` before binding. The same transaction that queues or resets the export binds it `active`. Before binding, it retires any earlier active delegation for that export.
- **Trustee resolution.** Before every Trust creation and trust-token authentication, the Hub resolves `service_trustee_user_id()` by password-authenticating to `OS_PROJECT_NAME`. The worker refuses a stored trustee that differs.
- **Verification.** The creation response must match the exact trustor, trustee, project, `impersonation=true`, a non-empty subset of requested roles without admin/manager/service, and expiry no later than requested. A mismatch deletes the Trust and fails admission with 403. The worker's trust token must match the stored ID, trustor, trustee, impersonated user, project and expiry. Token roles must include the delegated roles, stay within their current Keystone implication closure (`auth.role_name_closure`) and exclude administrative names.
- **Revalidation.** The worker calls `validate_package_owner(trustor, project)` and requires the `palimpsest-publish_editor` capability and delegated roles in current effective roles. Validator 403 → `authorization_revoked`; 503 → `authorization_unavailable`. Both are terminal, preserving the earlier terminal treatment of Glance access failures and existing lease/attempt fences.
- **Cleanup state machine.** `pending → active → cleanup → deleted | expired`. Completion/error CAS, soft delete and attempt exhaustion retire delegations in the same transaction. The worker sweep promotes pending rows older than 15 minutes and active rows of finished exports. It leases each due row for 5 minutes and deletes through the Trust's own impersonating token. On failure it backs off exponentially, capped at 1 hour and at the Trust expiry. After expiry the row is marked `expired` without a Keystone call. Admission abandonment first tries immediate deletion with the requester token. Cleanup never touches export rows.
- **Schema.** Additive table only, matching the repository's `create_all` bootstrap. The existing `palimpsest_image_exports` columns are unchanged. Data migration copies the table when present.
- **Settings.** `PALIMPSEST_HUB_EXPORT_DELEGATED_ROLES` (default `["member"]`, administrative names rejected) and `PALIMPSEST_HUB_EXPORT_DELEGATION_TTL_SECONDS` (default 21600, 900–86400). Glance master's `download_image` default (`ADMIN_OR_PROJECT_MEMBER_DOWNLOAD_IMAGE`) requires `role:member` on every visibility path, so `reader` cannot download. Export admission already requires `member`, because `palimpsest-publish_editor` needs it. The trust token also carries Keystone-implied roles (`member` → `reader`); verification accepts the current implication closure only. Kolla mirrors both settings and passes validator/protected IDs to the worker.

## Risks / Trade-offs

- A Trust whose trustor was disabled or lost roles cannot issue tokens and therefore cannot be deleted by the Hub. It stays inert until expiry, then the Hub records it as `expired`. The operator's `keystone-manage trust_flush` removes the Keystone record.
- Queue delay longer than the TTL fails `delegation_expired`; requesters resubmit.
- Exports queued before rollout fail `delegation_required` instead of running.
- Deployed Keystone policy may diverge from the defaults. Trust creation, trust-scoped authentication and trustor deletion through an impersonating token must be verified natively before rollout.
- The Kolla registration still grants the service user `admin` in its own service project. This Palimpsest change does not alter that existing own-service grant.
