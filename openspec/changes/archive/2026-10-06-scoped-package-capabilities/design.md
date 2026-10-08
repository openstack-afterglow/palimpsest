## Context

Hub already validates original Keystone subjects and revalidates enabled owners and current effective assignments for package keys. The existing member/reader and can_write shortcut is too coarse. Package writes remain package-key-only; original tokens authorize control, metadata/download and legacy artifact publication.

## Goals / Non-Goals

**Goals:** one explicit leaf/parent capability matrix, token/current-assignment intersection, issue/use-time attenuation, preserved exact owner/project/package checks and synthetic HTTP coverage.

**Non-Goals:** cloud mutation, role seeding, global builder/GC changes, package deletion/publication policy APIs, VM launch, checks during integration.

## Decisions

- Resolve fresh effective assignments and the actual current role-ID implication DAG against a complete unique-global role directory. Accept only exact effective service leaves; no runtime preset expansion, suffix or project-role shortcuts. Presets initially provide reader/user/editor/admin bundles, but removing a parent edge immediately removes authority. Baseline effective member is required for non-reader caps; inventory additionally permits baseline reader. No service leaf creates base membership. Domain aliases, ambiguous global role bindings, unknown graph nodes, cycles and provider failures fail closed.
- Add `packages:inventory` for metadata/manifests. Retain existing `packages:read` as download plus metadata, `cache:read` as cache download, and write actions as publish. Publish-only leaves do not acquire download: remove write-requires-read DTO coupling so write-only keys can remain least privilege.
- Current effective service assignments provide owner action authority. Original tokens are further attenuated by their role IDs resolved through the same current graph; token identity/scope are never replaced. Keys expand download delegation to metadata before intersecting with current owner actions, so a reader downgrade retains authorized metadata but never content download or writes.
- Own-key metadata needs keys editor or keys admin; issuance needs keys editor and every requested action in current token authority; revocation needs keys admin. All key control remains original-token-only and owner-only.
- Legacy artifact inventory/download routes use corresponding service capabilities while preserving their existing visibility checks. Verified system-admin retains legacy global read and builder/GC authority, never a service admin. Native package keys cannot authorize legacy/VM operations.

## Risks / Trade-offs

- Existing ordinary-member automation loses service authority until separately assigned service roles; no live role migration is performed.
- Old keys remain stored but lose unauthorized actions immediately. No key schema migration is needed.
- Assignment lookup failures remain fail-closed 503. This change adds no cached authority.
- Test-defined synthetic acceptance is not production or real-cloud evidence; verification is reserved for the integrating owner.

## Executed local acceptance

The follow-on verification built canonical API/worker targets for linux/arm64
and linux/amd64 and passed182 registered-app HTTP checks per architecture. Real
SQLite/CAS bytes persist across restart/recreation; Redis is real. Keystone's
role-ID directory, subjects and current assignments are synthetic HTTP inputs
read through the real SDK. Root package/credential/CLI/BuildKit tests passed309
after source freeze. See the existing registry documentation for exact commands.

Production init_db's MySQL connect_timeout prevents canonical SQLite bootstrap;
both probes failed explicitly. SQLite remains an isolated smoke-only factory
with lifespan off and a lock-pinned development driver in a derived image. No
production database/dependency/Dockerfile change, MySQL/MariaDB deployment,
TLS, real cloud/KVM build or production role mutation is implied. Keep this
change active for the parent to integrate/review; no archival or publication
approval is inferred from native acceptance.

