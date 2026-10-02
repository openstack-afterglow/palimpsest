# Registry profiles: native packages and Docker/OCI

Palimpsest separates three remote contracts:

- **Native project packages and BuildKit cache** use a `protocol = "palimpsest"` profile, its HTTPS `api_base`, and a project/package/action-bound package key. Current source passed isolated local acceptance, not deployed-cloud qualification.
- **Legacy Hub artifacts** (boot images, SquashFS runtime blocks and bundles) use `PALIMPSEST_URL` and the original Keystone token in `PALIMPSEST_TOKEN`. That token is not native package or BuildKit-cache write authority.
- **Docker/OCI registries** use a `protocol = "oci"` profile and Distribution `/v2` through Docker/Buildx, with Docker credentials.

Hub's native `/v1` API is not a Docker `/v2` registry. Configure its native profile explicitly; do not rely on a hostname to infer the protocol. The [project package contract](project-package-registry.md) records the design; this guide describes the consumer cutover in source. The [2026-10-02 candidate integration](development-handoff.md#candidate-integration--2026-10-02) records portable and Hub gates plus real BuildKit → native deferred push → byte-identical pull and remote exact-cache reuse over loopback HTTPS. That proof used synthetic Keystone and isolated SQLite; it does not qualify production identity policy, migrations, native VM execution or deployment.

## Profile configuration

Registry profiles are stored in:

```text
${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml
```

Palimpsest creates the directory with mode `0700` and the file with mode `0600`. The file is configuration only: credentials, tokens, passwords, private keys, and secret-bearing cache URLs are rejected.

The built-in `docker` profile points to `docker.io`, uses the `library` namespace for a one-component repository name, and is the default until another profile is selected.

```bash
palimpsest registry ls
palimpsest registry inspect docker

palimpsest registry add corp registry.example.com \
  --protocol oci --namespace platform --default

palimpsest registry use corp
palimpsest registry inspect
palimpsest registry rm corp
```

The CLI is the preferred writer because it validates and atomically replaces the file. The equivalent secret-free TOML shape is:

```toml
schema_version = 1
default = "corp"

[registries.docker]
endpoint = "docker.io"
protocol = "oci"
namespace = "library"

[registries.corp]
endpoint = "registry.example.com"
protocol = "oci"
namespace = "platform"
mirrors = ["mirror.registry.example.com"]
ca = ["/etc/palimpsest/certs/corp-ca.pem"]
plain_http = false
tls_skip_verify = false
cache_from = ["type=registry,ref=registry.example.com/cache/palimpsest"]
cache_to = ["type=registry,ref=registry.example.com/cache/palimpsest,mode=max"]
```

The built-in `docker` table must remain present with endpoint `docker.io` and namespace `library`.

`registry add --protocol oci|palimpsest` defaults to `oci`; old profiles without a protocol remain OCI profiles under `schema_version = 1`. Both protocols accept repeated `--ca` absolute paths. Native profiles require `--api-base HTTPS_URL` on exactly the same `host[:port]` authority as `endpoint`, with no credentials, query, fragment or ambiguous path. A native default namespace is optional but must be one lower-case repository component.

```bash
palimpsest registry add hub packages.example.invalid \
  --protocol palimpsest \
  --api-base https://packages.example.invalid/v1 \
  --namespace project-apps
```

```toml
[registries.hub]
endpoint = "packages.example.invalid"
protocol = "palimpsest"
api_base = "https://packages.example.invalid/v1"
namespace = "project-apps"
ca = ["/etc/palimpsest/certs/hub-ca.pem"]
```

Native HTTPS verifies the server with the system CA trust plus configured CA files, requires TLS 1.2 or newer, and refuses redirects. Native profiles reject `--mirror`, `--plain-http`, `--tls-skip-verify`, `--cache-from` and `--cache-to`; there is no insecure native mode.

OCI profiles additionally accept repeated `--mirror`, `--cache-from` and `--cache-to`, plus `--plain-http`, `--tls-skip-verify` and `--force`. Plain HTTP and TLS-skip are mutually exclusive. Profile and command-line Buildx cache definitions supplement mandatory native cache participation only for OCI output builds; they never replace it.

### Selection precedence

For an unqualified reference, profile selection is `--registry PROFILE`, then `PALIMPSEST_REGISTRY`, then the configured default. A fully qualified reference whose authority has no configured profile stays an ordinary Docker/OCI reference, exactly as before. That covers `pull`, `push`, `tag`, `image inspect`, `login`, `logout` and build tags. A configured authority must map to exactly one protocol; if it has more than one, the command fails rather than guessing. An explicit `--registry` must match a qualified reference's authority. If native aliases for one authority have different API bases, select an alias explicitly.

Endpoints are scheme-free `host[:port]` values; paths and embedded credentials are invalid. A positional login/logout server cannot be combined with `--registry`. If it names a configured profile or authority, that profile's protocol applies. Otherwise login/logout go to Docker as before. Native transport always needs a configured profile, because it needs the API base.

Reference completion follows Docker conventions. A missing tag becomes `latest`. Under the built-in profile, `alpine` resolves to `docker.io/library/alpine:latest`. Under the `corp` example above, `api:v1` resolves to `registry.example.com/platform/api:v1`.

Profiles do not rewrite Dockerfile `FROM` lines. Remote Dockerfile inputs must remain fully qualified and digest-pinned. Native references require `namespace/package:tag` or `namespace/package@sha256:...` (not both tag and digest); a configured default namespace completes a one-component package name.

## Native authentication, build and transfer

### Operator binding and secret-once key issuance

Before using `cloud.dmslab.re.kr/openstack-afterglow/test:v1`, the operator must configure the trusted public origin and bind `openstack-afterglow` to the **actual immutable Keystone project UUID**, then register that namespace with an ordinary member's original project-scoped token. A display name, client `X-Project-Id` header or familiar hostname is not a binding. This example is conditional, not evidence that this authority is deployed or that a binding exists. See [Hub service settings](install.md#hub-service-settings) for protected identities, validator policy and federated membership prerequisites.

Control calls use the original member token (`X-Auth-Token`), never a package key:

1. `GET /v1/projects/current` returns the original project and any registered namespace.
2. `PUT /v1/projects/{project_id}/namespace` registers the operator-configured alias (or the deterministic project namespace); it cannot rename/rebind an existing namespace.
3. `POST /v1/projects/openstack-afterglow/keys` issues the following exact-package key:

```json
{
  "name": "test-build-publish",
  "scope": {"packages": ["test"]},
  "actions": ["packages:read", "packages:write", "cache:read", "cache:write"],
  "expires_in_days": 30
}
```

The response contains public metadata under `key` and the complete credential under `secret` **once**. Write actions require their matching read actions; expiry is 1–90 days. Keep the secret in approved secret handling, not argv, shell history, TOML, receipts or logs. List/revoke calls never recover it. Keys bind owner, project, exact packages and actions; every use rechecks current owner membership and protected-principal policy. A package-only key cannot authorize an online build's mandatory cache.

### Credential-helper configuration and login

Native login requires an installed Docker credential helper configured in `${DOCKER_CONFIG:-$HOME/.docker}/config.json`. Lookup uses the exact key `api_base.rstrip('/') + '/projects/' + namespace`, then `credsStore`; it never falls back to host-only entries, `auths` or plaintext. In the conditional DMS Lab example, the native CLI uses the public gateway API base `https://cloud.dmslab.re.kr/api/v1/palimpsest/hub`. The gateway maps public `/api/v1/palimpsest/hub/projects/...` to upstream Hub `/v1/projects/...`; the client uses the API base directly, with no extra `/v1`. That allowlisted Afterglow key gateway is a separate dependency and has not been deployed.

```json
{
  "credHelpers": {
    "https://cloud.dmslab.re.kr/api/v1/palimpsest/hub/projects/openstack-afterglow": "osxkeychain"
  }
}
```

This requires `docker-credential-osxkeychain` on `PATH`; choose the installed helper appropriate to the host. Helpers run without a shell and receive secrets on stdin only; package-key and legacy-token environment variables are stripped from helper processes. Native login validates `/auth/me` before storing the key. `--username` is the key's public UUID, in canonical 32-hex or hyphenated form, not a Keystone username. `--namespace` on login/logout overrides the profile default.

```bash
# Only after the operator UUID binding, namespace registration, key issuance
# and deployment of the separate Afterglow key gateway.
palimpsest registry add dmslab cloud.dmslab.re.kr \
  --protocol palimpsest --api-base https://cloud.dmslab.re.kr/api/v1/palimpsest/hub \
  --namespace openstack-afterglow

# PUBLIC_KEY_UUID is public metadata; inject PACKAGE_KEY_SECRET securely.
printf '%s\n' "$PACKAGE_KEY_SECRET" | palimpsest login --registry dmslab \
  --namespace openstack-afterglow --username "$PUBLIC_KEY_UUID" --password-stdin
unset PACKAGE_KEY_SECRET

# Dockerfile remote bases/frontend must already be digest-pinned.
palimpsest build . --frontend dockerfile -f Dockerfile \
  --registry dmslab --tag cloud.dmslab.re.kr/openstack-afterglow/test:v1 \
  --output ./test.oci.tar
palimpsest push cloud.dmslab.re.kr/openstack-afterglow/test:v1 --registry dmslab
palimpsest pull cloud.dmslab.re.kr/openstack-afterglow/test:v1 \
  --registry dmslab --output ./test-pulled.oci.tar

palimpsest logout --registry dmslab --namespace openstack-afterglow
```

Logout erases only that helper entry; revoke the key through the member control API to invalidate it server-side. For ephemeral automation the native client can use `PALIMPSEST_PACKAGE_KEY` instead of helper lookup; it is checked through `/auth/me`, is not persisted, and never substitutes `PALIMPSEST_TOKEN`. Login itself still requires the helper and stdin/prompt.

### Local typed receipts, not Docker images

Native builds export `type=oci`, freeze verified archive bytes, and write owner-only (`0600`) `palimpsest-local-package-reference-v1` receipts under `state/package-references/<sha256(canonical-reference)>.json`. Retained archives live at `state/package-artifacts/<archive-digest-hex>.oci.tar`; successful build records remain at `builds/<build-id>/record.json`. Receipts bind authority/API base, namespace/package, root digest, archive digest/size, package type and available project/build/publication identity. They are separate from SquashFS runtime-layer tags and Docker's image store. Later push re-snapshots and checks the recorded identities; a later cache-upload or publication failure does not erase a valid native export/receipt.

Native `push --input PATH` accepts a regular OCI-layout tar archive or layout directory instead of a build receipt. Use `--manifest sha256:...` to select a root when the index has several roots. Archive input preserves transport bytes; directory input creates a deterministic archive without rewriting OCI blobs. Push requires a tag and uses compare-and-set publication. Pull accepts a tag or digest, resolves a tag once, downloads the original archive and checks digest, size and the selected graph before publication to `--output PATH`. Its default destination is `state/package-artifacts/<sha256(canonical-reference)>.oci.tar`, and its receipt uses the immutable digest reference.

Native transfers preserve the complete selected graph and reject `--all-tags` and `--platform`. Native builds permit multiple tags only for one exact namespace/package and reject `--load` and Docker cache exporters. `build --push` publishes through the native package API after export/cache refresh, not Buildx `type=registry`. Native pull never runs `docker load`; native `tag` is rejected. Docker inventory, inspect/history/save/load/remove and generic passthrough remain Docker operations, not native package inventory.

The dependency-free client can verify raw and gzip OCI layers on its Python 3.11+ floor. Zstd OCI layer DiffID verification specifically requires Python 3.14's `compression.zstd`; older interpreters fail closed rather than skipping verification. The Hub's separate zstd dependency does not remove this client-side limitation.

## OCI authentication and Docker-compatible image commands

OCI profiles reuse Docker's existing configuration and credential helpers from `DOCKER_CONFIG`, or `~/.docker` when unset. Credentials are not copied into registry profiles or build receipts.

```bash
# Interactive login to the selected profile.
palimpsest login --registry corp

# Non-interactive login without placing the password in argv or shell history.
printf '%s\n' "$REGISTRY_PASSWORD" | \
  palimpsest login --registry corp --username ci-user --password-stdin

palimpsest logout --registry corp
```

For OCI profiles and unconfigured authorities, the following commands pass through Docker's exit status and terminal streams:

```bash
palimpsest pull alpine:3.20
palimpsest pull api:v1 --registry corp --platform linux/amd64

palimpsest tag local-api:dev api:v1 --registry corp
palimpsest push api:v1 --registry corp

palimpsest images --digests
palimpsest images --filter reference='registry.example.com/platform/*'
palimpsest image inspect api:v1 --registry corp

palimpsest image history registry.example.com/platform/api:v1
palimpsest image save registry.example.com/platform/api:v1 --output ./api.tar
palimpsest image load --input ./api.tar
palimpsest image rm registry.example.com/platform/api:v1
```

Top-level `history`, `rmi`, `save` and `load` alias their `image` forms. On OCI profiles, `pull`, `push`, `tag`, `login` and `logout` retain Docker behavior. `images` and `image inspect|history|rm|save|load` remain Docker-only. Other `palimpsest image` subcommands (`ls`, `pull`, `push`, `verify`, `import`) retain their legacy Hub boot-image meaning.

For Docker commands that do not yet have a first-class Palimpsest spelling, use the generic passthrough:

```bash
palimpsest docker version
palimpsest docker image ls --digests
```

The passthrough uses the same existing `DOCKER_CONFIG`/`~/.docker` directory and runs without a shell. It does not apply Palimpsest registry-profile reference completion; pass Docker the exact reference you want. To prevent credential leakage or configuration bypass, a Docker-global `--config` option before the subcommand and all `docker login -p|--password` forms are rejected. A program argument named `--config` after commands such as `docker run IMAGE ...` remains untouched. Set `DOCKER_CONFIG` before invocation and use an interactive login or `--password-stdin` instead.

## Building and publishing

OCI builds keep the existing profile selection (explicit alias, then `PALIMPSEST_REGISTRY`, then default) for profile caches. They canonicalize output tags against that profile only when `--push` or `--registry` is given. Native builds always canonicalize their tags. For OCI profiles, repeated `-t/--tag` values and `build --push` publish via Buildx `type=registry`, and `--load` keeps Docker-format loading. `--runtime-push` is a separate legacy Hub SquashFS upload that uses the original `PALIMPSEST_TOKEN`, for either output protocol.

```bash
palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --registry corp \
  --cache-registry hub \
  --cache-package project-apps/api \
  -t api:v1 \
  -t api:stable \
  --pull \
  --runtime-base sha256:<boot-image-digest> \
  --runtime-tag api-runtime-v1 \
  --push \
  --runtime-push
```

For OCI output only, `--load` loads a Docker-format result into the Docker store. Repeated external cache definitions use standard Buildx syntax:

```bash
palimpsest build . \
  --frontend dockerfile \
  -t registry.example.com/platform/api:v1 \
  --cache-registry hub --cache-package project-apps/api \
  --cache-from type=registry,ref=registry.example.com/cache/api \
  --cache-to type=registry,ref=registry.example.com/cache/api,mode=max \
  --push
```

Every online build must authorize `cache:read` and `cache:write` on an exact native package before builder preflight or build-directory creation. Native output defaults to its selected profile and namespace/package; `--cache-registry NATIVE_ALIAS` and `--cache-package NAMESPACE/PACKAGE` can select the cache explicitly. Online OCI output **requires both flags**, even if `PALIMPSEST_TOKEN` is set. Cache packages have no tag/digest selector. No unqualified legacy-token cache writes remain; `HubClient.push_blob` refuses BuildKit-cache kind/media type. External cache imports/exports are OCI-only additive accelerators. `--no-cache` is allowed only in strict offline mode.

## BuildKit mirrors and private CAs

For OCI profiles, mirror, CA, plain-HTTP and TLS-skip settings belong to the BuildKit daemon, not client-side Docker commands. Native profiles are omitted from this generated config; their CAs instead verify native HTTPS directly:

```bash
palimpsest registry buildkit-config --output ./buildkitd.toml

docker buildx create \
  --name palimpsest \
  --driver docker-container \
  --buildkitd-config ./buildkitd.toml
docker buildx inspect --builder palimpsest --bootstrap

BUILDX_BUILDER=palimpsest palimpsest build . \
  --frontend dockerfile -t api:v1 --registry corp \
  --cache-registry hub --cache-package project-apps/api
```

Generating the file does not modify or restart an existing builder. The mirror/CA settings take effect only after the generated file is applied to an explicitly created or otherwise configured BuildKit builder. They do not change Docker Engine/Desktop's pull/push trust store, insecure-registry list, or mirror settings; those daemon settings remain independently managed.

## Immutable inputs and strict offline mode

Online Dockerfiles must use immutable remote inputs:

```dockerfile
# syntax=docker/dockerfile:1@sha256:<frontend-manifest-digest>
FROM registry.example.com/platform/base@sha256:<manifest-digest>
```

Mutable remote `FROM` tags, ARG-expanded remote image sources, unpinned external stages, and unchecked remote `ADD` inputs are rejected. This prevents an unchanged Palimpsest cache key from reusing work after a registry tag moves. A registry profile cannot relax this rule.

Strict `--offline` mode never loads `registries.toml` or resolves registry profiles. It never invokes registry authentication and never constructs a Hub or remote-registry client, so an offline build writes no typed native package reference. To publish its archive later, use native `push --input ARCHIVE --manifest sha256:...`. Docker may still read its selected `DOCKER_CONFIG` to locate the existing local builder; this is not filesystem-level isolation from Docker configuration. Network isolation and source validation prevent registry access during the solve. Offline mode rejects:

- `--registry`, `--cache-registry`, `--cache-package`, `--pull`, `--push` and `--runtime-push`;
- external `--cache-from` and `--cache-to` definitions;
- network-enabled build steps and remote/dynamic Dockerfile inputs.

Supply each Dockerfile base through a verified, digest-pinned local OCI layout with `--local-image`, use an already-bootstrapped local BuildKit builder whose network mode is `none`, and keep the runtime base in the local content store. See [BuildKit Cache and Block Runtime Workflow](buildkit-block-workflow.md#strict-offline-build) for the complete isolation contract.
