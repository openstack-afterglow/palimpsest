## Why

The dashboard needs real native named-bundle discovery and a separately authorized global operator inventory. Per-layer search must not masquerade as an image-like complete bundle, caller metadata must not masquerade as build attestation, and private package name/tag search must retain existing project/key ceilings.

## What Changes

- Add read-only `GET /v1/catalog/assets` for current-caller-visible named complete SquashFS leaves, with bounded literal case-insensitive name search and offset pagination.
- Add fresh human global inventory/detail at `GET /v1/admin/catalog/layers` and `GET /v1/admin/catalog/layers/{digest}` through `require_global_admin`, sharing native human/system checks with existing require_admin and requiring system:manage without fabricating a project for system/unscoped humans. Preserve target-bound existing endpoints. Return exact graph ancestry, bounded direct children and explicit recorded/unknown origins without stored config, recipes or paths.
- Add optional bounded name/tag substring `q` to native project package inventory, bound into cursors without changing absent-query cursor compatibility or project/key/action restrictions.
- Add isolated real SQL/router regression fixtures and a loopback-only synthetic-identity HTTP runner. No operational lifespan, CAS, cloud writes, migrations, external registry calls or VM endpoints.

## Capabilities

### New Capabilities
- `dashboard-asset-catalog`: native visible bundle discovery and independently gated global operator graph/provenance reads.

### Modified Capabilities
- Existing private package inventory gains an optional filter only; default responses and authority remain unchanged.

## Impact

New Hub catalog DTO/router/service; native main router registration; existing package inventory endpoint/service; Hub test lane manifest and isolated fixtures; architecture, package contract, testing, changelog and handoff. No SQL/model changes, deployment, commit, publication or production verification. Source is based on preserved dirty `dev` at `aa397a0fd976e35302974a347748a9773e3084bf`, not a clean release.
