# Project-scoped package registry contract

Contract: `palimpsest.project-packages.v1`. Status: **implemented in local source (2026-10-02), not deployed**. A local isolated acceptance passed (see the [handoff](development-handoff.md)); production Hub/Afterglow rollout, the reader identity and the `openstack-afterglow` binding still need separate approval.

## 1. Goal and current boundary

A project member builds a package locally, authenticates with a restricted package key, pushes `cloud.dmslab.re.kr/openstack-afterglow/test:v1`, and sees that exact package/tag/digest in the selected project's Palimpsest page. Neither an administrator token nor a configured service password can authorize publication.

Verified source baselines: Palimpsest `dev` `dc8a164fd7ad4243f0e01bf7e48e613608674ccb`; Afterglow `dev` `0f59e0ee8e7cea6d36db33f1d4e2380fe4fac2b7`, both with preserved pre-existing working-tree changes. Source inspection and read-only CLI help are not production API verification.

| Pre-implementation baseline (historical; not current source) | Evidence at baseline | Contract added by the local implementation |
| --- | --- | --- |
| Top-level `push`/`login` call Docker | `src/palimpsest_local/registry.py:667-722`; `docs/registries.md:86-122` | Native package transport and scoped key login |
| Hub stores cloud images, SquashFS and BuildKit cache | `hub/src/palimpsest_hub/api/hub.py:168-245` | Ordinary OCI image publication |
| Global blob descriptor plus project grants | `hub/src/palimpsest_hub/models.py:18-65` | Namespace, package, version and tag records |
| Hub exchanges the submitted token through `v3.Token` | `hub/src/palimpsest_hub/auth.py:76-112` | Validation of the original subject without project re-scope |
| HubClient can send a project header, but CLI callers omit it | `src/palimpsest_local/hub.py:136-187`; `cli.py:2099` | Server-bound project authorization, not a client header workaround |
| Afterglow `/admin/libraries` lists its own layer/build DB | `frontend/src/routes/admin/libraries/+page.svelte:392-407,950-994` | Hub package inventory for ordinary project members |
| Afterglow user layer API requires published and sealed rows | `backend/app/api/palimpsest/layers.py:50-64` | Private project packages visible without global publication |
| Afterglow Hub BFF forwards the authenticated browser's scope | `backend/app/api/palimpsest/hub.py:29-41`; `services/service_proxy.py:232-254` | Restricted package-key gateway and package read/control APIs |

The Hub remains a native `/v1` service. Docker-compatible **reference syntax** does not claim Docker Distribution `/v2` compatibility. Existing Docker/OCI registry profiles continue to use their actual `/v2` registries. No request falls back from native package publication to Docker, Keystone admin, another project or a different host.

## 2. References, namespaces and ownership

Canonical reference: `AUTHORITY/NAMESPACE/PACKAGE:TAG`; immutable resolution: `AUTHORITY/NAMESPACE/PACKAGE@sha256:DIGEST`.

Example: `cloud.dmslab.re.kr/openstack-afterglow/test:v1` means authority `cloud.dmslab.re.kr`, project namespace `openstack-afterglow`, package `test`, tag `v1`. The tag is a version alias; it does **not** choose the project. The first path component chooses the namespace, which the server maps to one immutable Keystone project ID.

- Reuse the existing registry authority/repository/tag parsing rules. A native namespace is one lower-case repository component, 1–63 ASCII characters. A package is one or more lower-case repository components; the combined `namespace/package` is at most 255 characters. Tags use the existing Docker tag grammar and 128-character maximum. Digests are SHA-256 only, lower-case canonical hex.
- Scheme, credentials, query, fragment, empty path components, `.`/`..`, backslash, encoded separators and ambiguous tag+digest references are rejected. HTTP paths carry the package name as a query parameter, not a greedy path that can consume a tag or upload suffix.
- Namespace mapping is a Hub SQL record, not a browser label, `X-Project-Id`, filename, cache scope or freely supplied metadata. One project has one namespace and vice versa; display-name changes do not move it. Keystone project/user IDs are opaque, exact ASCII identifiers matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Preserve case and bytes in comparison/storage/wire fields; do not UUID-normalize them. Federated/LDAP shadow-user IDs can be 64 hexadecimal characters. UUID normalization applies only to Hub-generated key/package/upload IDs.
- Namespace registration is a non-admin, authenticated project-control operation using the verified project ID/name. Auto namespace is `p-<id>` for a 32-character lower-case hex project ID; otherwise `p-h-<first 56 hex characters of SHA-256 of the ASCII project ID>`. Both forms are reserved for that exact project. A readable namespace such as `openstack-afterglow` needs an operator-reviewed configuration binding to the exact project ID; names are not globally unique across Keystone domains. Return the selected namespace explicitly; never silently rewrite a push reference or displace another mapping.
- The namespace endpoint is idempotent for an already-bound project. No rename, arbitrary alias takeover or transfer API is part of this contract. Required deny-lists contain exact immutable protected project and administrative/service user IDs; an empty or invalid list disables package authority fail-closed. Names (`admin`, `service`, `system`) are not the protection boundary.
- Project membership and role authorization are verified against Keystone. Browser-selected project names and client headers do not prove membership. The current selected project must match the original token's project for control operations.
- A package is private to its project. Every authorized current member can browse that project's packages; a package key can read only its declared package scope. Private packages need neither `is_published=true` nor a legacy sealed-layer flag to appear. Public sharing is not introduced here.

## 3. Identity and authorization

### 3.1 No administrator upload path

There are two explicit authentication modes, never fallback alternatives:

1. **Member control/read identity:** original project-scoped Keystone token in `X-Auth-Token`, validated without exchanging/re-scoping it. Used to register the project's namespace, issue/list/revoke the caller's own keys, and browse that project's package inventory. The browser BFF validates its JWT separately and sends the original Keystone token, not the JWT, upstream.
2. **Package data identity:** Hub-issued opaque key in `Authorization: Bearer`. Used for CLI upload, package/tag publication, pull and explicitly authorized build-cache transfer. Raw Keystone tokens, Afterglow JWTs and service credentials do not authorize native package writes.

An original token carrying `admin`, `manager` or `service`, system/domain scope, a configured administrative/service principal, or an infrastructure/admin project is denied key issuance and package publication. Protected principals/projects use exact immutable IDs from trusted platform policy. A known admin/service account remains forbidden even with a reduced token in an ordinary project. System-admin is not a package-authority bypass. Native `member`/`reader` and project management roles alone grant no service entitlement.

Authorization resolves a fresh global Keystone role directory and actual role-ID implication graph with current effective assignments. Only unique global role IDs can bind service capabilities; domain aliases, duplicate global bindings, missing roles/graphs, cycles and directory outages fail closed. Presets initially link `palimpsest_admin` → editor → user → reader and each grade to its leaves, but these are not hardcoded runtime expansions: deleting an implication revokes that feature even while the parent assignment/token remains. Baseline effective `member` is required for non-reader leaves; effective `member` or `reader` permits the inventory leaf. Leaves do not imply base membership.

Initial presets also link each non-reader leaf to `palimpsest_reader`, providing inventory through an actual editable Keystone dependency. The Hub does not synthesize that dependency: removing it removes inventory unless another current path still grants it. A reader-only baseline paired with non-reader service authority fails closed rather than silently granting user/editor actions.

| Exact effective leaf | Allowed authority |
| --- | --- |
| `palimpsest-inventory_reader` | Authorized package metadata/manifests and legacy artifact inventory; no content or credential download |
| `palimpsest-download_user` | Authorized package/blob/layer/cache downloads and supporting metadata; no writes |
| `palimpsest-publish_editor` | Namespace registration, package/tag/cache publication, native legacy artifact writes; no implicit download or key management |
| `palimpsest-keys_editor` | Own-key metadata and issuance, only for actions currently held by the original token and owner |
| `palimpsest-keys_admin` | Own-key metadata and revocation, never another user's key |

Original-token service authority is intersected with current assignments, retaining the original user/project/token/auth reference. A role upgrade absent from the original subject requires a renewed token; downgrades and removed graph edges are enforced immediately on the next authorization. Package-key actions are intersected with current owner actions at every request and again at publication commit. Legacy verified global builder/GC and its artifact read path remain separate; `palimpsest_admin` never authorizes VM launch or global operations.

Keystone token validation MUST use `GET /v3/auth/tokens` with the presented token as `X-Subject-Token`, authenticated by the Hub's read-only validator identity, and obtain that original subject's project/user/roles/expiry. Never use `v3.Token(...).get_access()` token-method authentication for validation. A supplied `X-Project-Id` is only a consistency assertion: mismatch with the validated original project is denied. No default-project inference, user re-login, replacement subject token or configured admin password fallback is allowed. Existing Hub read/export consumers retain that original subject/token when they require an OpenStack connection; a package key grants no Nova/Cinder/Manila/Glance authority.

A dedicated system-scoped `reader` validator needs Keystone `identity:validate_token`, `identity:list_role_assignments`, `identity:get_user`, `identity:get_project`, `identity:list_roles`, `identity:get_role` and `identity:list_role_inference_rules` read policy. Original-subject validation and role-directory/graph reads never exchange or re-scope the caller's token. A global-only role listing may omit domain roles referenced by the inference listing: retrieve their trusted ID records to distinguish domain edges, ignore those edges for builtin global service authority, and deny unknown/missing global references. Explicit `truncated=true` and next-page links fail closed. Validator credentials remain separate from Glance export credentials and never own package keys. No live policy is mutated. The [Keystone policy mapping](https://docs.openstack.org/keystone/2026.2/getting-started/policy_mapping.html) names the `/v3/role_inferences` target `identity:list_role_inference_rules` (not `identity:list_role_inferences`).

### 3.2 Package keys

A key binds `key_id`, `owner_user_id`, `project_id`, `namespace`, exact package names or explicit whole-project scope, actions, expiry and revocation. It is not a Keystone application credential: possession cannot invoke other OpenStack services.

- Permissions: `packages:inventory` (metadata/manifests), `packages:read` (download plus metadata), `packages:write`, `cache:read`, `cache:write`. Write actions do not imply read and may be issued without read to a publish-only principal. Each requested action must be a subset of current caller capabilities. Package publication does not imply cache read or globally public visibility.
- Native client login accepts the effective inventory action. `resolve` requires inventory, and CLI/client push requires inventory plus publish for tag compare-and-set rather than content download. A deliberately write-only key is valid for direct upload calls but cannot resolve a tag; normal publish presets can delegate inventory plus publish without download.
- Scope request is either `{ "packages": ["test"] }` or `{ "all_packages": true }`, never both. Exact package lists contain 1–32 unique canonical names. No wildcard, prefix match or child-package inheritance. Whole-project scope is an explicit UI/issuer choice, not a default.
- Default lifetime is 30 days; issuer may choose 1–90 days. CI automation uses a non-admin project member/robot owner and the same bounds. No never-expiring or self-renewing keys.
- Generate 32 random secret bytes. The wire credential is `ppk_v1_<key UUID as 32 lower-case hex>.<43-character unpadded base64url secret>`; creation's `secret` field returns that complete credential exactly once. Send it as `Authorization: Bearer <credential>`. Lookup by the embedded public key ID; persist only SHA-256 of the decoded random bytes and compare in constant time. Validate format/secret before disclosing expiry/revocation status. No credential in SQL plaintext, URLs, logs, events, receipts or list/detail responses. Issuance uses `Cache-Control: no-store`.
- Issuance/list/revocation remains original-token-only and owner-only in the authenticated current project. Issuance requires keys editor, list requires keys editor or keys admin, revocation requires keys admin. A key cannot issue another key. Rotation is issue then explicit revoke, with no grace/fallback credential.
- Each authorization checks key active/expiry/revocation and fresh enabled owner/project plus effective role-ID graph authority. `/auth/me` returns effective, not obsolete delegated, actions. Re-check on finalization/tag commit, not only initiation; removal/downgrade during transfer prevents publication. Keystone failure is 503, never cached approval. Legacy export download tickets also revalidate their bound owner's download capability at redemption.
- A session is bound to the initiating key ID **and** owner/project/package. Another member's key cannot append/finalize/abort it. After revocation a new key starts a new session; it does not inherit the old one.

### Isolated HTTP smoke (defined, not executed during integration)

From `hub/`, after integration, run:

```sh
uv run pytest -q tests/test_auth.py -k native_http
uv run pytest -q tests/test_auth.py tests/test_packages.py tests/test_hub_api.py tests/test_builds.py
```

`test_auth.py::native_http` starts a real Uvicorn Hub on an ephemeral loopback socket and uses the existing loopback synthetic Keystone HTTP server through the real Keystone SDK. Each case has a temporary SQLite database and CAS; production app lifespan, cloud credentials, Redis, workers, containers and live Hub endpoints are not used. Cases publish real bounded OCI archives, exercise leaf/parent metadata/download/write/key control, current graph-edge removal, owner downgrade, original subject/project assertions and builder/GC denial. The synthetic directory owns mutable actual implication edges; parent-only claims are not assumed capabilities. Existing package cases retain own-key, exact package/namespace, CAS and immutable version/tag coverage. These commands have not been run for this change and are not production qualification.

### Docker-backed registered HTTP acceptance

The 2026-10-07 scoped verification built the canonical `docker/hub/Dockerfile`
API and export-worker targets for `linux/arm64` and `linux/amd64`, then exercised
the unmodified registered `palimpsest_hub.main:app` routes, middleware and error
handlers in local containers. Each architecture passed **182 checks** (364
total); the root client/credential/CLI/BuildKit selection passed **309 tests**.
This is isolated acceptance, not a production rollout or real cloud/KVM build.

Exact commands, from the repository root:

```sh
docker buildx build --platform linux/arm64 --file docker/hub/Dockerfile --target palimpsest-hub-api --load --tag palimpsest-scope-api:20261007-arm64 .
docker buildx build --platform linux/arm64 --file docker/hub/Dockerfile --target palimpsest-hub-worker --load --tag palimpsest-scope-worker:20261007-arm64 .
docker buildx build --platform linux/amd64 --file docker/hub/Dockerfile --target palimpsest-hub-api --load --tag palimpsest-scope-api:20261007-amd64 .
docker buildx build --platform linux/amd64 --file docker/hub/Dockerfile --target palimpsest-hub-worker --load --tag palimpsest-scope-worker:20261007-amd64 .
python3 scripts/smoke_package_capabilities.py run --api-image palimpsest-scope-api:20261007-arm64 --worker-image palimpsest-scope-worker:20261007-arm64 --platform linux/arm64 --evidence-dir build/scoped-package-capabilities-smoke/20261007-arm64
python3 scripts/smoke_package_capabilities.py run --api-image palimpsest-scope-api:20261007-amd64 --worker-image palimpsest-scope-worker:20261007-amd64 --platform linux/amd64 --evidence-dir build/scoped-package-capabilities-smoke/20261007-amd64
uv run --frozen --extra dev python -m pytest -q tests/unit/test_packages.py tests/unit/test_package_credentials.py tests/unit/test_cli_registry.py tests/unit/test_buildkit.py
```

Reruns require a new or empty evidence directory; omitting `--evidence-dir`
creates a unique directory. The runner requires Docker and the built images,
downloads the hash-pinned `aiosqlite` wheel from `hub/uv.lock` (or accepts the
same verified wheel through `--aiosqlite-wheel`), and uses real Redis on a
unique private network. It retains its SQL/blob volumes and sanitizes reports
and logs; only its disposable containers/network/smoke-derived image are
removed. It does not contact production endpoints or use caller cloud secrets.

**Storage/lifespan distinction:** canonical images deliberately omit the
development-only SQLite driver. A smoke-only image derived from each built API
installs exactly `aiosqlite==0.22.1` from the lockfile. The harness creates the
SQLite engine/schema/session factory and installs it in `palimpsest_hub.database`,
then serves the production app with Uvicorn `lifespan=off`. Authentication,
registered routes, SQL services and blob storage are not mocked or replaced.
Both actual canonical bootstrap probes exited 1 with
`TypeError: 'connect_timeout' is an invalid keyword argument for Connection()`:
`init_db` uses MySQL connection arguments. This finding is retained, not fixed
or hidden by substituting MariaDB. Therefore the smoke proves persistent
SQLite/CAS route behavior, **not** the canonical production database/lifespan,
MariaDB, Redis credentials, TLS transport or deployment health.

Observed route proof includes:

- Real offset-owned uploads, stale-offset 409 and another key's session 404;
  immutable publication 201 and idempotent finalize 200. Original archive,
  layer, legacy artifact and seeded export bytes match; two owned Range 206
  responses reproduce the original bytes, including after restart/recreation.
- Inventory metadata 200 with content download 403; exact inventory+write
  keys publish 201 without read/download authority. Undelegated actions/scopes,
  wrong project assertions, protected identities and foreign resources deny.
- Own-key revocation 204 followed by 401; current owner downgrade revokes
  download, publication and previously issued Redis export tickets while
  preserving inventory. Removing the current graph's publish edge revokes
  write authority without suppressing unrelated download. Ambiguous/dangling
  role metadata fails closed 503, including a provider metadata 404.
- Native inventory/download/export-ticket lookups preserve actual SQL resource
  visibility. The completed export row alone is seeded with real CAS bytes;
  no Glance download, conversion or OpenStack execution occurred.
- Tenant service-admin/Keystone admin and package keys cannot build or GC.
  Verified system-admin passes the build boundary (503 because no builder is
  configured) and deletes only this run's proof-owned GC layer; retained bytes
  remain readable. No KVM builder or cloud executor is exercised.
- The real Keystone SDK performs fresh reads against a synthetic unique-global
  role-ID directory and mutable assignments/edges. Each run records 92 subject
  validations and zero token exchanges/rescopes or caller-token actor calls.
- SQL and 11 digest-verified CAS files reread identically after both a container
  restart and full recreation using the same named SQLite/blob volumes.

Local receipts are `build/scoped-package-capabilities-smoke/20261007-{arm64,amd64}/report.json`
and sanitized logs. Canonical API/worker probes imported Hub **0.3.1** on
`aarch64`/`x86_64`; each API exposed 58 registered routes. Worker probes also
validated `qemu-img 10.0.13` formats without a cloud conversion. The root test
receipt is `root-tests.txt` (309 passed in 1.54s), alongside `image-versions.txt`.
The parent-selected Hub162 run remains separate; it was not rerun to confirm.

Retained evidence volumes (the identifiers use the host's UTC run timestamp):

| Architecture | SQLite volume | Blob volume |
| --- | --- | --- |
| arm64 | `palimpsest-scope-smoke-20261006t200227z-f9e281-sql` | `palimpsest-scope-smoke-20261006t200227z-f9e281-blobs` |
| amd64 | `palimpsest-scope-smoke-20261006t200314z-bd2434-sql` | `palimpsest-scope-smoke-20261006t200314z-bd2434-blobs` |


The 2026-10-08 full-source integration preserves independent cache leaves:
standalone `NativePackageClient.upload_cache` requires `cache:write` only;
online BuildKit still preflights both `cache:read` and `cache:write`. New
client/CLI/Hub regressions define write-only upload, read-only resolve/download,
exact package/platform/builder scope and current-owner attenuation, but were
not executed during preparation. Final root and Hub regression is required.

The existing Docker runner contains no cache-transfer scenario and executes no
native CLI. Its plain-HTTP synthetic endpoint cannot prove native HTTPS trust
or credential-helper behavior. Final acceptance must separately exercise real
cache HTTP upload/resolve/download/downgrade and reread cache metadata/bytes
after restart/recreation; installed CLI login/push/pull/logout through a trusted
HTTPS profile and exact namespace helper; and opt-in real BuildKit cache reuse
with pinned inputs. Canonical database/lifespan, credentialed Redis, live
Keystone and the deployed Afterglow gateway remain separately qualified, never
inferred from the current synthetic registered-app script.

### 3.3 Client credential storage

Reuse the existing Docker credential-helper protocol and `DOCKER_CONFIG` selection; do not invent a plaintext `palimpsest-tokens.json`. For native Hub keys, the helper lookup key is the HTTPS native API base plus namespace, distinct from the Docker registry host credential. Profile TOML stores only protocol/authority/API base/namespace, never the key.

Native persistent login requires a configured credential helper. No base64 `auth` JSON or legacy `PALIMPSEST_TOKEN` fallback for package data. CI may provide an ephemeral `PALIMPSEST_PACKAGE_KEY` to the native client; it is checked against the exact authority/API base/namespace before use and is not persisted. Key input is interactive or `--password-stdin`, never password/token argv. Redirects cannot forward credentials to another authority. Production HTTPS verification cannot be disabled for this transport.

## 4. Package contents and publication

Two package types:

| Type | Input | Immutable version identity | UI meaning |
| --- | --- | --- | --- |
| `oci-image` | Validated OCI image-layout directory/tar, image manifest or explicit image index and its complete reachable graph | Original selected manifest/index SHA-256 | Image package; platform/runtime compatibility is separate |
| `runtime-bundle` | Existing Palimpsest OCI-layout bundle dialect containing base/layer descriptors and complete verified parent/config graph | Selected bundle root descriptor SHA-256 | Palimpsest runtime package; execution support still checked separately |

`buildkit-cache` is **never** a package type, runnable version, tag target, sealed runtime layer or successful push result. Cache records use a separate project/package/cache-key path and do not appear in package inventory/counters.

The client snapshots a local source before upload, calculates its archive transport digest/size and root identity, and verifies reachable descriptors. A source with several roots requires explicit `--manifest`; no first-entry guess or implicit merging of separately pushed amd64/arm64 images. A real OCI image index may represent both architectures; pushing a second single-platform image to the same tag replaces the alias, not merges it.

The server independently verifies tar/layout structure, path/member/size bounds, selected root, all reachable descriptor sizes/digests, layer DiffIDs, config platform/process metadata and Palimpsest bundle parent/base invariants. OCI source manifests/config/layers retain their original bytes/digests. Build provenance is explicitly **client-reported**, not Hub attestation of a build run. A completed upload is not evidence of bootability, service readiness, signature trust or Cinder/Manila parity.

Reuse the existing CAS, digest locks, resumable upload offset/fsync ordering and bounded bundle parsing. Add ordinary OCI validation; do not feed ordinary OCI images into `HubLayerMeta` as SquashFS/cache or relax that validator to make them fit.

Publication steps:

1. Authorize the exact namespace/project/package/key before creating a session or checking global blob existence.
2. Stream into staging with last-acknowledged offsets; no package/tag visibility yet.
3. Verify the complete graph and archive transport digest, re-check actor/membership/key and tag precondition.
4. Under sorted digest locks and a package/tag lock, publish verified CAS bytes and commit version/graph/package/tag metadata in one SQL transaction. Commit completes before a successful response/list visibility.
5. Rollback only this operation's new/unreferenced staging/CAS files. Never delete a pre-existing/shared blob or another project's grant.

Content-addressed deduplication does not confer access. No global `exists(digest)` shortcut creates a package, transfers a grant or proves ownership. Same-project/package/version re-push is idempotent after fresh authorization; another project must independently supply a valid authorized package graph.

Tags are mutable aliases with compare-and-set, while versions are immutable. The client reads a tag before transfer and carries `expected_tag_digest` (null means absent). Concurrent change produces 412 and does not silently overwrite. Same tag/root returns 200 idempotent success; creation/update returns 201 after commit. Existing versions remain resolvable by digest. No implicit `latest` rewrite for an explicitly supplied tag.

## 5. Persistence changes

Use the existing Hub SQLAlchemy/SQL/CAS patterns, not a second Afterglow package database. New authoritative records:

| Table/record | Fields and constraints |
| --- | --- |
| Namespace | `project_id` PK (exact Keystone ID, binary-collated VARCHAR(64)), `namespace` unique/binary-collated, verified display name, created_at; no nullable ownership |
| Package | UUID PK, project_id FK, canonical name, package_type, created_at, updated_at; unique `(project_id, name)`; type fixed after first publication |
| Version | package_id FK + root_digest composite PK, root media type, immutable descriptor graph JSON, platforms JSON, archive transport digest/size, validated total reachable bytes, client provenance JSON, pushed_by, pushed_key_id, pushed_at |
| Tag | package_id FK + tag composite PK, version root FK, updated_at, updated_by, revision; compare-and-set inside the publication transaction |
| Key | UUID PK, project_id FK, owner_user_id, secret_hash, name, package scope JSON, actions JSON, created_at, expires_at, revoked_at; hash/secret excluded from serialization |
| Package upload | extend/reuse existing upload mechanics with non-null project/package/key/owner, package_type, selected root, archive digest/size, tag and expected_tag_digest, received offset, status, created/updated/expiry; unique operation binding |

The graph records every reachable blob needed to prevent legacy layer/CAS deletion or staging cleanup from unlinking published package contents. Update deletion/reference checks and migration table enumeration. Namespace/tag comparison is case-sensitive after canonical normalization; tags differing by case remain distinct. Existing layer rows/grants are not silently rewritten as packages or transferred between projects. Historical/cache-only records are not backfilled as complete images.

## 6. Native API reference

Hub paths are relative to native `/v1`. Public gateway paths are relative to `https://cloud.dmslab.re.kr/api/v1/palimpsest/hub` and replace native `/v1`, not append another `/v1`.

For package operations below, `namespace` is a path parameter and `package=test` (or URL-encoded canonical nested name) is a required query parameter wherever `{package}` is described. This avoids greedy repository-path ambiguity.

### 6.1 Control and key APIs

| Method/path | Authentication | Request/response |
| --- | --- | --- |
| `GET /projects/current` | Member original token | Only the original token's active project: exact ID/name, nullable namespace and read/write capabilities; not other projects or an admin-wide catalog |
| `PUT /projects/{project_id}/namespace` | Original member token with matching project | Empty body; register/return verified mapping by the rule in section 2; 201 new, 200 existing |
| `POST /projects/{namespace}/keys` | Original member token; same project, non-admin | Key creation below; 201 secret-once response |
| `GET /projects/{namespace}/keys` | Original member token | Caller-owned key metadata only, including revoked/expired status, no secret/hash |
| `DELETE /projects/{namespace}/keys/{key_id}` | Original member token; owner only | 204 idempotent revoke; foreign key/project 404 |
| `GET /auth/me` | Package key | Validated actor/key/project/namespace/scopes/expiry; does not re-scope/mint token |

`ProjectContext`: `project_id`, `project_name`, nullable `namespace`, `capabilities` (`packages_read`, `packages_write`, `keys_issue`). Afterglow retains its existing project selector; switching obtains a new original project-scoped token before this lookup. Namespace creation is an explicit first-use member action, never a read-side effect or implicit key-issuance permission.

Key-create JSON:

```json
{
  "name": "ci-test-build-push",
  "scope": {"packages": ["test"]},
  "actions": ["packages:read", "packages:write", "cache:read", "cache:write"],
  "expires_in_days": 30
}
```

Key metadata fields: `key_id` UUID, `name`, `owner_user_id`, `project_id`, `namespace`, `scope`, `actions`, `created_at`, `expires_at`, `revoked_at` (nullable). Creation returns `{ "key": KeyMetadata, "secret": string }`. Only creation includes `secret`; UI copy/show-once state is discarded on navigation/project switch. `GET /auth/me` adds `actor_type: "package-key"`; token validation and the response never disclose service credentials.

The example deliberately permits online build-cache transfer and both package transfer directions for `test`. A push/pull key requests `packages:read`/`packages:write` (with inventory available from download delegation and current owner authority); a publish-without-download key requests `packages:inventory`/`packages:write`; an inventory-only key requests just `packages:inventory`. Effective inventory plus `packages:read` is required for pull. The issuer never adds undeclared cache/write authority.

### 6.2 Inventory and resolution APIs

Member read identity or matching `packages:read` key:

| Method/path | Meaning |
| --- | --- |
| `GET /projects/{namespace}/packages` | Name-ordered list; `limit` default 50/max 100, opaque keyset `cursor`, optional `package_type`; response `{project_id, namespace, items, next_cursor}` |
| `GET /projects/{namespace}/package?package={package}` | Summary plus tags/platforms/latest push; no legacy sealed/public filter |
| `GET /projects/{namespace}/versions?package={package}` | Immutable versions, descending pushed_at/root_digest, same pagination bounds |
| `GET /projects/{namespace}/resolve?package={package}&tag={tag}` | Resolve one alias to exact root/graph/platforms; returns project_id/namespace/package/tag/digest and `ETag` equal to quoted root digest |
| `GET /projects/{namespace}/versions/{digest}?package={package}` | Version metadata and original root descriptor; foreign/missing 404 |
| `GET /projects/{namespace}/versions/{digest}/download?package={package}` | Verified OCI-layout package stream for authorized pull; `Cache-Control: private, no-store`; no unauthenticated export-ticket substitution |
| `GET /projects/{namespace}/versions/{digest}/blobs/{blob_digest}?package={package}` | Only blobs reachable from that authorized version; range support follows current blob reader; never global CAS-by-digest access |

`PackageSummary`: `package_id`, `project_id`, `namespace`, `name`, `package_type`, `visibility: "project"`, `tags` (tag/digest pairs), `platforms` (`os`, `architecture`, optional variant), `version_count`, `latest_pushed_at`, `latest_pushed_by`. Each item carries project_id so a UI can fence a stale response. Version metadata includes root digest/media type, verified graph, archive digest/size, provenance, pushed actor and time. It does not contain credentials, worker paths or raw process environment values; original OCI config bytes remain available to authorized package pull.

### 6.3 Upload and atomic tag publication

All writes require matching `packages:write` key; raw Keystone/admin/JWT/service auth is rejected.

| Method/path | Contract |
| --- | --- |
| `POST /projects/{namespace}/uploads?package={package}` | Authorize and create staged session, 201; body below; return UploadSession |
| `GET /projects/{namespace}/uploads/{id}?package={package}` | Owner/key-bound status + acknowledged offset; foreign session 404 |
| `PATCH /projects/{namespace}/uploads/{id}?package={package}` | `application/octet-stream`, required `Upload-Offset`; append/fsync/ack, 204 + new header; wrong offset 409 + current header |
| `PUT /projects/{namespace}/uploads/{id}?package={package}` | Empty JSON body, verify and atomic publish; 201 new/update, 200 same tag/root; 412 if tag changed |
| `DELETE /projects/{namespace}/uploads/{id}?package={package}` | 204 abort this staged session only; cannot delete an existing version/shared CAS blob |

Upload-start JSON (the digest values are symbolic documentation examples, not live artifacts):

```json
{
  "package_type": "oci-image",
  "tag": "v1",
  "root_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "archive_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "archive_size_bytes": 220399616,
  "expected_tag_digest": null,
  "provenance": {"source_revision": "example-revision", "build_id": "example-local-build"}
}
```

Every request object rejects undeclared fields. Upload-start requires all shown fields except optional `provenance`; `expected_tag_digest` is explicitly null or one canonical SHA-256 digest. Provenance permits only `source_revision`/`build_id` strings, each at most 128 characters; neither is verified by the Hub. Project, owner, key ID, publication actor and trust/visibility are server-owned, never writable upload metadata. All reachable package bytes must be supplied; the server never fetches descriptor URLs to fill a missing OCI graph.

UploadSession: `upload_id`, bound `project_id`, `namespace`, `package`, `key_id`, `received_bytes`, `expires_at`, `status` (`uploading`, `validating`, `complete`, `failed`, `aborted`), nullable `result`. Finalization is synchronous; other status requests cannot observe a partially published tag. Existing per-project four-active-upload and 24-hour idle bounds are retained; configured byte/member/parser limits are enforced before publication and advertised by the API's documented deployment settings. Validation/permission failure leaves no package/tag/grant mutation and no other operation's cleanup.

PublishResult:

```json
{
  "project_id": "11111111111141118111111111111111",
  "namespace": "openstack-afterglow",
  "package": "test",
  "tag": "v1",
  "digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "package_type": "oci-image",
  "visibility": "project",
  "platforms": [{"os": "linux", "architecture": "amd64"}],
  "already_published": false,
  "web_url": "https://cloud.dmslab.re.kr/palimpsest/packages?namespace=openstack-afterglow&package=test&digest=sha256%3A1111111111111111111111111111111111111111111111111111111111111111"
}
```

Server computes the URL from configured trusted public origin and canonical result, never a client callback URL. CLI verifies response ownership/reference/root equals its preflight target before reporting success. A commit acknowledged but followed by transport failure can be resolved through the authenticated tag/digest read; do not redo publication against a different project.

### 6.4 Cache authorization and errors

Cache read/write uses the same verified project/package identity and explicit `cache:*` actions, but separate resources. Reuse the existing BuildKit archive and exact-key/same-scope fallback semantics; add exact project ID/package to the lookup partition and bind archive metadata to it. `build_key`, `cache_scope`, platform and builder fingerprint retain the existing BuildKit validator/grammar. No caller-supplied scope string assigns ownership.

| Method/path | Permission and behavior |
| --- | --- |
| `GET /projects/{namespace}/cache/resolve?package={package}&build_key={key}&cache_scope={scope}&platform={platform}&builder_fingerprint={fingerprint}` | `cache:read`; exact-key hit first, otherwise latest same-scope/platform/fingerprint archive in this project/package. Return CacheReceipt with `resolution: exact` or `scope`; 404 is an authoritative miss, other failures do not permit local/cold fallback |
| `GET /projects/{namespace}/cache/archives/{digest}?package={package}` | `cache:read`; stream only an archive associated with that authorized project/package, verify digest client-side |
| `POST /projects/{namespace}/cache/uploads?package={package}` | `cache:write`; body contains build_key/cache_scope/platform/builder_fingerprint/archive_digest/archive_size_bytes; 201 owner/key-bound UploadSession |
| `GET/PATCH/PUT/DELETE /projects/{namespace}/cache/uploads/{id}?package={package}` | `cache:write`; same session ownership, status, offset/fsync, finalization-time authority and abort rules as package uploads; PUT validates the archive/binding and returns a CacheReceipt, never a tag/version |

`CacheReceipt`: `project_id`, `namespace`, `package`, `build_key`, `cache_scope`, `platform`, `builder_fingerprint`, `archive_digest`, `archive_size_bytes`, `created_at`, `created_by`; resolve adds `resolution`. Cache writes retain the same per-project active/idle limits and bind the existing cache-key descriptor inside the archive to the request. These are client-reported build inputs, not a server attestation. Cache finalization is not PublishResult or an inventory entry. Remove unqualified HTTP cache/blob registration write paths at cutover; update every CLI, Hub `/app` and Afterglow caller rather than keeping a token/admin compatibility bypass.

Error envelope: `{ "error": { "code": string, "message": string, "request_id": string } }`; no raw token, upstream traceback or secret-bearing URL. 401 `AUTH_REQUIRED`/`KEY_INVALID`/`KEY_EXPIRED`/`KEY_REVOKED`; 403 `ADMIN_CREDENTIAL_FORBIDDEN`/`PROJECT_SCOPE_MISMATCH`/`PACKAGE_SCOPE_DENIED`/`ACTION_DENIED`; foreign object or unowned session 404; namespace/type/offset conflict 409; tag compare-and-set 412; limits 413/429; unsupported layout/graph/digest 422; identity/Hub dependency unavailable 503. Rejection happens before upload/grant/publication side effects. Missing package reads do not reveal another project's inventory.

## 7. CLI contract and examples

New registry-profile `protocol` is `oci` or `palimpsest`; existing v1 profiles are interpreted as `oci` without secret changes. A native profile has matching authority, HTTPS `api_base`, and optional default namespace. Docker-only mirrors/cache exporters/insecure transport are rejected on a native profile. A configured authority must map to one unambiguous protocol; an unconfigured authority stays ordinary Docker/OCI, never native.

Workflow (after the operator deploys the Hub/gateway, binds the namespace and issues a key):

```sh
palimpsest registry add cloud cloud.dmslab.re.kr \
  --protocol palimpsest \
  --api-base https://cloud.dmslab.re.kr/api/v1/palimpsest/hub \
  --namespace openstack-afterglow

# Generate an exact-test build/push key with package and cache read/write actions.
# The password-stdin input comes from a secret manager, not a literal key.
palimpsest login --registry cloud --username "$PACKAGE_KEY_ID" --password-stdin

# Online native build: BuildKit exports locally, mandatory cache uses the key, then native push.
palimpsest build . --frontend dockerfile -f Dockerfile \
  --tag cloud.dmslab.re.kr/openstack-afterglow/test:v1 --push

# Or publish an explicit local layout/archive later.
palimpsest push cloud.dmslab.re.kr/openstack-afterglow/test:v1 --input ./test.oci.tar
palimpsest pull cloud.dmslab.re.kr/openstack-afterglow/test:v1 --output ./pulled.oci.tar
```

- Native `login` parses the wire credential, requires `--username` to match its public key UUID, calls `/auth/me`, validates authority/project/namespace/action binding, then stores the key in the namespace-qualified credential-helper entry. There is no Docker login or Keystone token minting on this branch.
- Native `build` stores a typed local reference/receipt to its validated archive/root, so `palimpsest push REFERENCE` can resolve the build output without `docker load`. `--input` selects an explicit verified layout/tar; `--manifest` selects an explicit root. Local artifact references and identifier-only SquashFS runtime tags remain distinct.
- Online builds have mandatory Hub cache transfer today. Native profile builds must obtain explicit `cache:read/write` grants and project/package binding before any cache lookup/write; package-only keys cannot authorize those writes. The example build key needs both cache actions in addition to package actions, or build with the existing strict offline/local-input contract and push later. Do not silently downgrade cache authorization to `PALIMPSEST_TOKEN`.
- For native `build --push`, BuildKit first exports locally; the client runs the same package publication pipeline afterward. Do not send a native Hub URL into Buildx `type=registry`. On failure, preserve the valid local archive/receipt and return nonzero; a cache upload is not push success.
- For `runtime-bundle`, a standalone SquashFS blob is not a complete package. Assemble/verify the required base/config/ordered layer graph through the existing bundle machinery before push; an OCI image's automatic command/user semantics must not be claimed for a cloud-image overlay.
- Fully qualified references containing another namespace are denied when the key lacks that exact project/package binding, regardless of profile defaults or supplied project headers.
- Pull resolves a tag once to digest, verifies the complete downloaded graph and records the immutable result. A namespace/digest remote reference does not automatically boot a local VM or create an OpenStack resource.
- Existing Docker registry `push/login/build --push` remains routed to Docker/Buildx only for an `oci` profile. Unsupported native `--all-tags` is rejected, not sent to Docker.

## 8. Afterglow application plan

### 8.1 Browser/API/gateway boundaries

Afterglow keeps its JWT/session/blacklist/timeout/token-binding validation and explicit project-switch path. Package browser dependencies compare the exact bounded JWT/session project/user IDs and forward the original session Keystone token without token exchange or session-token replacement. Control/read routers are `/api/v1/palimpsest/packages` and `/api/v1/palimpsest/package-keys`, with no router-local prefix; `main.py` mounts them. `X-Project-Id` asserts consistency only, never overrides the JWT subject or selects another project's endpoint.

Browser BFF contract (paths below include the sole `/api/v1` mount):

| Method/path | Scoped upstream |
| --- | --- |
| `GET /api/v1/palimpsest/packages/context` | `GET /v1/projects/current` with original caller token |
| `PUT /api/v1/palimpsest/packages/namespace` | `PUT /v1/projects/{validated exact project ID}/namespace`; explicit registration, empty body |
| `GET /api/v1/palimpsest/packages` | Current namespace's `/packages`; same limit/cursor/type filters |
| `GET /api/v1/palimpsest/packages/detail`, `/resolve`, `/versions`, `/versions/{digest}` | Matching native detail/resolve/version read; same package/tag/digest queries |
| `GET /api/v1/palimpsest/packages/versions/{digest}/download` | Authorized native version download with original caller token; no export ticket |
| `POST/GET /api/v1/palimpsest/package-keys` | Current namespace's key issue/list; same body/secret-once/no-store response |
| `DELETE /api/v1/palimpsest/package-keys/{key_id}` | Caller-owned current-project revoke |

The BFF derives namespace from `ProjectContext` for the validated exact current project ID; it never accepts a different project in a browser body/query. A deep-link namespace must match that context before package fetch. With no namespace, show an explicit registration action for an eligible member, not a false inventory failure; keys wait for registration. Preserve native envelope/status and verify upstream response ownership before exposure.

The native CLI uses `/api/v1/palimpsest/hub/...` with a package key. Add a narrowly enumerated native data gateway for `/auth/me`, authorized package inventory/read/download and package/cache upload lifecycle. It forwards the key unchanged to a trusted configured Hub endpoint; it does **not** interpret that key as an Afterglow JWT, inject service `X-Auth-Token`, discover an endpoint using admin credentials or expose arbitrary Hub admin/export/build paths. Missing trusted route/Hub is 503. Browser JWT routes retain the existing caller-token proxy. If both credential types are supplied, reject the ambiguity.

The mapping is public `/api/v1/palimpsest/hub/projects/...` → upstream `/v1/projects/...`. `HubClient`'s present automatic `/v1` prefix must not produce `/v1/v1` or public `/hub/v1/...`; a native package client uses the configured API base directly.

### 8.2 User-visible page

Add `/palimpsest/packages` as a project-member page using the existing authenticated navigation/design system. Link it from the screenshot's `/admin/libraries` page and the normal Palimpsest navigation; keep existing administrator Dockerfile/SSH builder controls administrator-only. Administrator mode is not required to browse or push project packages and grants no package authority.

- Selected project banner shows verified project name, namespace and UUID, or an explicit not-yet-registered namespace state. Fetch Hub inventory, not `/api/v1/admin/libraries/artifacts` or the published+sealed legacy layer list.
- Package table: namespace/name, type, tag/digest, platform, verified size, pushed user/time and copy-reference/detail/download actions. Package details show version history and client provenance. A private uploaded OCI image is labelled an image package, not a sealed Manila layer/running VM.
- Key tab: name, exact package/project scope, actions, expiry and revoke; creation returns a once-only copy view. No admin credentials, token text, hashes or remote service password appear in examples/UI.
- After a successful CLI push, refresh/re-entry of the selected project shows the committed result without another admin publish/approve/import action. A publish URL opens the exact package/version; unauthorized projects remain unavailable.
- Project switch completes Keystone rescope/JWT replacement before package fetch. Abort/fence outstanding requests and discard package/key/once-only-secret state. Check response project UUID/request generation before render; an old response cannot populate the new project's table.
- Inventory errors render an error and retry action, not a fabricated zero count or a successful layer row. Existing screenshot FROM-resolution/history errors are not hidden by package inventory work.
- VM instantiate/Manila/Cinder import is a separate explicit operation with its own backend qualification/approval. Do not auto-convert/publish to Glance, make an unqualified “run” button, or count uploaded packages as existing VMs.

### 8.3 Ordered changes and gates

1. **Identity first:** fix original-token validation; forbid admin/service package authority; verify A-token/header-B mismatch denies before any write. No publication enabled yet.
2. **Hub metadata and keys:** migration for namespace/key/package/version/tag/upload records; membership-bound issuer/revocation; graph validator/CAS-reference checks; package/cache permission separation; gated package publication API.
3. **Native client:** typed profile, credential helper, source snapshot/local references, actual `push/pull`, online cache binding and build-then-publish. Migrate all unqualified upload callers including Hub `/app`; remove obsolete HTTP write/auth paths.
4. **Afterglow BFF + UI:** member read/control routers, allowlisted key gateway, project-aware inventory/keys page and screenshot cross-link. Hub remains the only package/key/tag database; no Notion key or legacy LayerArtifact mirror.
5. **Local end-to-end proof:** ordinary member key → real deterministic build → push → selected-project API + browser row → pull/hash equality; cross-project/admin/read-only/revoked/expired rejection and stale project-switch checks. Use isolated local/test storage, not existing candidate rows.
6. **Separately approved rollout:** reviewed exact refs/images, bounded non-admin proof project/user/key, migrations/backups, HTTPS trusted endpoint and token-validation read permissions. Deploy Hub then Afterglow/native client; verify actual production UI only after explicit approval. Existing approval receipts do not authorize this new rollout or reuse admin-project cache writes.

Rollback disables package writes at the gateway/Hub while preserving committed records and shared CAS bytes; do not restore the old default-project/admin write route. Afterglow returns an explicit unavailable state. Namespace/version/key schema is retained for forward repair; destructive down-migration/credential cleanup needs separate authorization. Prior proof cache rows are neither transferred into a package nor deleted by this change.

## 9. Observable acceptance criteria

These are required behavior criteria. The dated handoff records which isolated local checks actually passed and their source snapshots; that evidence does not establish production rollout, native execution or current provider qualification. Candidate integration adds fresh portable/source HTTP proof separately.

1. An ordinary member can issue a `test` read/write key in its active project, authenticate, build and push `AUTHORITY/its-namespace/test:v1` without an administrator credential or Docker image load.
2. Creation returns the secret once; later list/detail responses and SQL/audit/CLI receipts contain no secret/plaintext key. Helper failure has no plaintext fallback.
3. Namespace resolves to one exact immutable project ID. Original A token plus B header/path, A key plus B namespace, forged project_id and protected/admin/service targets fail before sessions/CAS/grants/tags mutate. A valid 64-hex federated owner works; case-only identity mismatches are rejected.
4. Admin/service/system credentials are denied even with `member` present; a protected administrative/service principal remains denied with a member-only token. A package key cannot mint another key or call OpenStack/admin build/export APIs.
5. A reader key cannot upload; exact-package key cannot publish sibling/child package; a package writer without `cache:write` cannot upload mandatory build cache. Cache success does not appear as a package.
6. Expired/revoked key, disabled owner/project or removed membership denies new requests and finalization of an already started transfer. Another member/key cannot resume that session. Identity outage is a failure, not an admin/cache fallback.
7. Interrupted upload resumes only from acknowledged bytes. Bad offset/hash/missing graph/config/platform or attempted client assignment of server-owned project/actor/trust fields cannot publish a tag/partial package. Client-reported source revision/build ID never becomes a verified-build assertion. A complete unknown package is never inferred from a cache digest.
8. Same fully qualified tag/root re-push is idempotent. Concurrent different-root tag writes use compare-and-set; the losing finalization returns 412 and cannot delete shared data. Two authorized projects can independently publish equal bytes without transferring ownership or exposing private names.
9. Published OCI image/index bytes retain their original digest/config/layers/DiffIDs; pull returns the verified same root. Multi-platform indexes stay indexes; a second single-platform push is not an implicit merge.
10. Immediately after acknowledged commit, the matching selected-project list/detail/resolve and `/palimpsest/packages` show namespace/test:v1 and exact digest/type/platform/pushed actor. No `is_published` admin toggle or legacy sealed row is needed. Another project sees no row/version/blob.
11. CLI key gateway and browser JWT BFF reach the same scoped Hub resources without `/v1/v1`, admin credential injection, secret-bearing redirects or broad proxy access. Project switching fences stale responses and clears secret state.
12. Existing ordinary Docker registry transport and privileged server-build separation remain intact. Namespace/key/package migration preserves legacy artifacts and rejects attempts to delete CAS still referenced by package versions. No source/test/deployment readiness is claimed until these scenarios are exercised.

## Related

- [Current registry behavior](registries.md)
- [Current Dockerfile cache/runtime representations](buildkit-block-workflow.md)
- [Current native API compatibility](compatibility.md)
- [Development handoff and historical scope incident](development-handoff.md)
- Afterglow change: `openspec/changes/project-scoped-palimpsest-packages/` (separate repository; proposal/checklist, not implementation proof).
