## 1. Delegation implementation

- [x] 1.1 Replace `get_admin_connection_for_project` with trustee resolution, verified Trust creation, trust-scoped connection and trust deletion in `openstack.py`; remove every caller/import.
- [x] 1.2 Add additive `palimpsest_image_export_delegations` model, migration table entry, delegated-role/TTL settings and `auth.role_name_closure`.
- [x] 1.3 Bind a requester Trust only when admission queues or resets Glance work; retire earlier creators' delegations; delete or durably record abandoned Trusts.
- [x] 1.4 Revalidate current authority and verify the trust token before worker Glance I/O; fail terminally without fallback.
- [x] 1.5 Retire delegations atomically on completion, error, soft deletion and attempt exhaustion; add the worker cleanup sweep with lease, backoff and expiry.

## 2. Configuration, regression and docs

- [x] 2.1 Kolla defaults/precheck: delegated roles, TTL, worker validator and protected-ID inputs.
- [x] 2.3 Update install/testing/architecture/changelog/handoff with source-reviewed, test-defined evidence labels.
- [x] 2.4 Parent: run the Hub lane and the synthetic-Keystone SDK/HTTP smoke; record actual results. Done 2026-10-07: the full Hub lane passed 285; `test_export_delegation.py` passed 28; Kolla contracts passed 45; the lane manifest check and `test_test_lanes` passed 106. A live Keystone trust flow is not covered and remains under 2.5.
- [ ] 2.5 Parent/operator: verify deployed Keystone trust policy and the impersonating trustor delete natively, align Afterglow's Kolla overlay, and decide on existing manual tenant grants separately.
