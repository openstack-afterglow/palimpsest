# Docker/OCI Registry Profiles

Palimpsest keeps two remote services deliberately separate:

- **Palimpsest Hub `/v1`** stores bootable qcow2/raw images, SquashFS runtime blocks, bundles, and the mandatory online BuildKit cache archive. Hub commands use `PALIMPSEST_URL` and `PALIMPSEST_TOKEN`.
- **Docker/OCI registries `/v2`** store ordinary OCI images and optional BuildKit cache exports. Palimpsest delegates compatible image operations to the installed Docker CLI and uses Docker's existing credential configuration.

Palimpsest Hub is not a Docker Registry endpoint. Do not pass a Hub `/v1` URL to `palimpsest login`, `pull`, or `push` unless a separate OCI registry service is actually listening at that host.

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
  --namespace platform \
  --default

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
namespace = "library"

[registries.corp]
endpoint = "registry.example.com"
namespace = "platform"
mirrors = ["mirror.registry.example.com"]
ca = ["/etc/palimpsest/certs/corp-ca.pem"]
plain_http = false
tls_skip_verify = false
cache_from = ["type=registry,ref=registry.example.com/cache/palimpsest"]
cache_to = ["type=registry,ref=registry.example.com/cache/palimpsest,mode=max"]
```

The built-in `docker` table must remain present with endpoint `docker.io` and namespace `library`.

`registry add` also accepts repeated `--mirror`, `--ca`, `--cache-from`, and `--cache-to` options, plus `--plain-http`, `--tls-skip-verify`, and `--force`. CA paths must be absolute. `--plain-http` and `--tls-skip-verify` are mutually exclusive. Use either only for a registry whose transport policy you control.

Example with optional external BuildKit caches:

```bash
palimpsest registry add corp registry.example.com \
  --namespace platform \
  --cache-from type=registry,ref=registry.example.com/cache/palimpsest \
  --cache-to type=registry,ref=registry.example.com/cache/palimpsest,mode=max
```

Profile cache entries are appended to any repeated `palimpsest build --cache-from` and `--cache-to` arguments. They supplement the mandatory Hub cache; they do not replace it.

### Selection precedence

For an unqualified image reference, Palimpsest selects a registry in this order:

1. A registry already written in the image reference, such as `ghcr.io/acme/api:v1`.
2. The command's `--registry PROFILE` option.
3. `PALIMPSEST_REGISTRY`.
4. The `default` profile in `registries.toml`.

An explicit registry in a reference always wins. Registry endpoints are scheme-free `host[:port]` values; paths and embedded credentials are invalid.

Reference completion follows Docker conventions. A missing tag becomes `latest`. Under the built-in profile, `alpine` resolves to `docker.io/library/alpine:latest`. Under the `corp` example above, `api:v1` resolves to `registry.example.com/platform/api:v1`.

The selected profile affects Palimpsest CLI references, build output tags when `--push` or `--registry` is used, and configured external cache exporters. It does not rewrite `FROM` lines inside a Dockerfile. Write remote Dockerfile inputs as fully qualified, digest-pinned references.

## Authentication and Docker-compatible image commands

Palimpsest reuses Docker's existing configuration and credential helpers from `DOCKER_CONFIG`, or `~/.docker` when `DOCKER_CONFIG` is unset. It does not copy credentials into `registries.toml`, build receipts, command arguments, or a second Palimpsest credential store.

```bash
# Interactive login to the selected profile.
palimpsest login --registry corp

# Non-interactive login without placing the password in argv or shell history.
printf '%s\n' "$REGISTRY_PASSWORD" | \
  palimpsest login --registry corp --username ci-user --password-stdin

palimpsest logout --registry corp
```

A positional login/logout server, for example `palimpsest login registry.example.com`, overrides profile selection and cannot be combined with `--registry`.

The following commands mirror common Docker image commands and pass through Docker's exit status and terminal streams:

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

Top-level `history`, `rmi`, `save`, and `load` are aliases for `image history`, `image rm`, `image save`, and `image load`. Top-level `pull`, `push`, `tag`, `images`, `login`, and `logout`, plus `image inspect|history|rm|save|load`, operate on Docker/OCI images through Docker. Other `palimpsest image` subcommands (`ls`, `pull`, `push`, `verify`, and `import`) retain their existing Palimpsest Hub boot-image meaning.

For Docker commands that do not yet have a first-class Palimpsest spelling, use the generic passthrough:

```bash
palimpsest docker version
palimpsest docker image ls --digests
```

The passthrough uses the same existing `DOCKER_CONFIG`/`~/.docker` directory and runs without a shell. It does not apply Palimpsest registry-profile reference completion; pass Docker the exact reference you want. To prevent credential leakage or configuration bypass, a Docker-global `--config` option before the subcommand and all `docker login -p|--password` forms are rejected. A program argument named `--config` after commands such as `docker run IMAGE ...` remains untouched. Set `DOCKER_CONFIG` before invocation and use an interactive login or `--password-stdin` instead.

## Building and publishing

`palimpsest build` accepts repeated Docker-style `-t/--tag` values. `--push` publishes those OCI image tags through the selected Buildx builder; `--runtime-push` publishes the generated SquashFS runtime block to Palimpsest Hub.

```bash
palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --registry corp \
  -t api:v1 \
  -t api:stable \
  --pull \
  --runtime-base sha256:<boot-image-digest> \
  --runtime-tag api-runtime-v1 \
  --push \
  --runtime-push
```

`--load` additionally loads a Docker-format result into the local Docker image store. Repeated ad hoc external cache definitions use standard Buildx syntax:

```bash
palimpsest build . \
  --frontend dockerfile \
  -t registry.example.com/platform/api:v1 \
  --cache-from type=registry,ref=registry.example.com/cache/api \
  --cache-to type=registry,ref=registry.example.com/cache/api,mode=max \
  --push
```

An online build always performs the fail-closed Palimpsest Hub cache lookup and refresh. External cache imports/exports are additive accelerators. `--no-cache` is therefore permitted only in strict offline mode.

## BuildKit mirrors and private CAs

Profile mirror, CA, plain-HTTP, and TLS verification settings belong to the BuildKit daemon, not the client-side Docker command. Palimpsest can generate a secret-free BuildKit daemon configuration:

```bash
palimpsest registry buildkit-config --output ./buildkitd.toml

docker buildx create \
  --name palimpsest \
  --driver docker-container \
  --buildkitd-config ./buildkitd.toml
docker buildx inspect --builder palimpsest --bootstrap

BUILDX_BUILDER=palimpsest palimpsest build . \
  --frontend dockerfile -t api:v1 --registry corp
```

Generating the file does not modify or restart an existing builder. The mirror/CA settings take effect only after the generated file is applied to an explicitly created or otherwise configured BuildKit builder. They do not change Docker Engine/Desktop's pull/push trust store, insecure-registry list, or mirror settings; those daemon settings remain independently managed.

## Immutable inputs and strict offline mode

Online Dockerfiles must use immutable remote inputs:

```dockerfile
# syntax=docker/dockerfile:1@sha256:<frontend-manifest-digest>
FROM registry.example.com/platform/base@sha256:<manifest-digest>
```

Mutable remote `FROM` tags, ARG-expanded remote image sources, unpinned external stages, and unchecked remote `ADD` inputs are rejected. This prevents an unchanged Palimpsest cache key from reusing work after a registry tag moves. A registry profile cannot relax this rule.

Strict `--offline` mode does not load Palimpsest registry profiles, invoke registry authentication, or construct a Hub or remote-registry client. The Docker CLI still reads its selected `DOCKER_CONFIG` as needed to locate the configured context and Buildx builder; Palimpsest does not claim filesystem-level isolation from that existing Docker configuration. Network isolation and source validation prevent registry access during the solve. Offline mode rejects:

- `--registry`, `--pull`, `--push`, and `--runtime-push`;
- external `--cache-from` and `--cache-to` definitions;
- network-enabled build steps and remote/dynamic Dockerfile inputs.

Supply each Dockerfile base through a verified, digest-pinned local OCI layout with `--local-image`, use an already-bootstrapped local BuildKit builder whose network mode is `none`, and keep the runtime base in the local content store. See [BuildKit Cache and Block Runtime Workflow](buildkit-block-workflow.md#strict-offline-build) for the complete isolation contract.
