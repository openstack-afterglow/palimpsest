# Palimpsest command workflows

This guide turns the parser-visible CLI into operational workflows. It explains
which backend each command reaches, the required configuration, the safe command
order, resulting state, and platform limits. For every positional and option,
use the generated [syntax reference](reference.md); for concise behavior notes,
use the [usage guide](usage.md).

All examples assume the `palimpsest` executable from the VCS installation in the
[detailed install guide](../install.md). Place global options such as `--url`
before the command. Values such as digests, UUIDs, paths, and names are
placeholders, not known-good production inputs.

## Workflow diagrams

Each diagram is stored as both editable Archify workflow JSON and a standalone
HTML viewer. The authored diagram content is Korean to match the requested
operator-facing workflow; Archify's fixed viewer controls and document language
fallback remain English because no supported locale override is declared.

| Workflow | Standalone HTML | Editable specification |
| --- | --- | --- |
| GitHub VCS install and initial configuration | [HTML](../diagrams/palimpsest-install-config.workflow.html) | [JSON](../diagrams/palimpsest-install-config.workflow.json) |
| Hub images, layers, and bundles | [HTML](../diagrams/palimpsest-hub-artifacts.workflow.html) | [JSON](../diagrams/palimpsest-hub-artifacts.workflow.json) |
| Registry profiles and Docker wrappers | [HTML](../diagrams/palimpsest-registry-docker.workflow.html) | [JSON](../diagrams/palimpsest-registry-docker.workflow.json) |
| Palimpsestfile and Dockerfile builds | [HTML](../diagrams/palimpsest-build.workflow.html) | [JSON](../diagrams/palimpsest-build.workflow.json) |
| Conventional cloud-image VM lifecycle | [HTML](../diagrams/palimpsest-cloud-vm-lifecycle.workflow.html) | [JSON](../diagrams/palimpsest-cloud-vm-lifecycle.workflow.json) |
| Linux amd64 OCI-root runtime | [HTML](../diagrams/palimpsest-oci-root.workflow.html) | [JSON](../diagrams/palimpsest-oci-root.workflow.json) |
| Compose-shaped projects | [HTML](../diagrams/palimpsest-compose.workflow.html) | [JSON](../diagrams/palimpsest-compose.workflow.json) |
| Local store, UI, and completion | [HTML](../diagrams/palimpsest-store-ui-completion.workflow.html) | [JSON](../diagrams/palimpsest-store-ui-completion.workflow.json) |

## Configuration and trust boundaries

Three similarly named image surfaces are intentionally separate:

| Surface | Commands | Backend | Credential boundary |
| --- | --- | --- | --- |
| Palimpsest Hub artifacts | `image ls/pull/verify/import/push`, `layer *`, `bundle *` | Native Hub `/v1` | `PALIMPSEST_TOKEN` |
| Docker image operations | `pull`, `push`, `tag`, `images`, `history`, `rmi`, `save`, `load`, `image inspect/history/rm/save/load`, `docker` | Installed Docker CLI and OCI registries | Docker credential store |
| Local OCI root | `oci pull`, `oci materialize`, `run ... --runtime-kind oci-root`, OCI evidence commands | Skopeo intake, private source CAS, Linux KVM runtime | Anonymous HTTPS intake plus local host/runtime permissions |

Do not pass a Hub `/v1` endpoint to Docker commands. Do not pass a registry
reference directly to `run`; `oci pull` must first publish a verified local OCI
archive.

### Setting precedence

| Concern | Resolution order |
| --- | --- |
| Hub URL | global `--url`, `PALIMPSEST_URL`, `config.toml` `url` or `[hub].url` |
| Hub token | `PALIMPSEST_TOKEN` only |
| Local state | `PALIMPSEST_STATE_HOME`, `[storage].state_root`, explicit `XDG_STATE_HOME/palimpsest`, platform default |
| Registry profile | registry in reference, `--registry`, `PALIMPSEST_REGISTRY`, configured default |
| Docker credentials | `DOCKER_CONFIG`, otherwise `$HOME/.docker` |
| Compose project | `-p`, `PALIMPSEST_PROJECT_NAME`, `COMPOSE_PROJECT_NAME`, model/default name |
| Host journal | Linux `/var/log/palimpsest`; explicit absolute private `PALIMPSEST_LOG_HOME`; disabled outside Linux unless overridden |

Secrets do not belong in `config.toml`, `registries.toml`, workflow JSON, command
receipts, documentation, or shell history.

## 1. Install and establish configuration boundaries

See the [install/config diagram](../diagrams/palimpsest-install-config.workflow.html).

1. Select a reviewed full Git commit SHA.
2. Create a Python 3.12+ virtual environment.
3. Install `palimpsest-local` from the GitHub VCS URL; add `[kvm]` only for a
   libvirt backend.
4. Install `palimpsest-hub` from `#subdirectory=hub` only on Hub service hosts.
5. Provision the external runtime separately: Lima/VZ on macOS; KVM/libvirt and
   host tools on Linux; database/Redis/OpenStack for Hub.
6. Verify the CLI entrypoint, then verify the selected state/config/log roots.

```sh
PALIMPSEST_REF="FULL_40_CHARACTER_COMMIT_SHA"
python3.12 -m venv "$HOME/.venvs/palimpsest"
"$HOME/.venvs/palimpsest/bin/python" -m pip install \
  "palimpsest-local @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"
"$HOME/.venvs/palimpsest/bin/palimpsest" --version

export XDG_STATE_HOME="$HOME/.local/state"
"$HOME/.venvs/palimpsest/bin/palimpsest" store show --format json
```

Expected state change: Python code is installed in the virtual environment.
`store show` resolves paths; installation itself does not create host accounts,
configure hypervisors, or migrate existing state.

## 2. Hub cloud images, layers, and bundles

See the [Hub artifact diagram](../diagrams/palimpsest-hub-artifacts.workflow.html).

### Configure the Hub session

```sh
export PALIMPSEST_URL="https://hub.example.invalid"
# Inject PALIMPSEST_TOKEN through the approved secret mechanism.
palimpsest image ls --arch x86_64 --disk-format qcow2 --limit 10
```

An explicit `palimpsest --url ... image ls` overrides the environment for one
invocation. Every Hub command requires a token.

### Cloud-image flow

```sh
# Discover metadata and choose a digest.
palimpsest image ls --arch x86_64 --disk-format qcow2 --limit 10

# Pull to the managed store and optionally copy below an output directory.
palimpsest image pull "$IMAGE_DIGEST" --output ./downloads

# Verify a local path against the expected digest.
palimpsest image verify ./downloads/image.qcow2 --digest "$IMAGE_DIGEST"

# Import an existing local qcow2/raw image into the Local store.
palimpsest image import ./ubuntu.qcow2 \
  --disk-format qcow2 --arch x86_64 --os-variant ubuntu24.04

# Upload a local cloud image to Hub.
palimpsest image push ./ubuntu.qcow2 --name ubuntu-dev --publish
```

`image import` is local-store ingestion, not upload. `image push` is the Hub
upload. `image verify` checks local bytes and requires an expected digest.

### Layer flow

```sh
palimpsest layer ls --limit 20
palimpsest layer pull "$LAYER_DIGEST" --output ./layers
palimpsest layer pack ./rootfs-overlay --tag app-v1
palimpsest layer push app-v1 --name app-v1 --base-image "$IMAGE_DIGEST"
```

`layer pack` creates a local fixed-recipe SquashFS layer. `--parent`,
`--base-image`, and `--ubuntu-base` describe the chain used by Hub; they do not
silently rewrite layer content.

### Bundle flow

```sh
palimpsest bundle pull "$LAYER_DIGEST" --output ./bundle --include-base
palimpsest bundle verify ./bundle
```

The output is an OCI image-layout directory representing a Palimpsest chain.
It is not the OCI-root archive accepted from anonymous registries unless the
separate OCI-root intake contract is satisfied.

## 3. Registry profiles and Docker-compatible commands

See the [registry/Docker diagram](../diagrams/palimpsest-registry-docker.workflow.html).

### Create and select a profile

```sh
palimpsest registry add internal registry.example.invalid \
  --namespace platform \
  --ca /etc/company/company-ca.pem \
  --mirror mirror.example.invalid \
  --default
palimpsest registry ls --format json
palimpsest registry inspect internal
palimpsest registry use internal
```

`registries.toml` stores endpoint, namespace, mirror, CA, transport, and cache
policy only. It must not contain credentials. `registry rm NAME` removes a
mutable profile; the built-in Docker profile remains protected.

### Authenticate and transfer images

```sh
printf '%s\n' "$REGISTRY_PASSWORD" | \
  palimpsest login --registry internal -u ci --password-stdin

palimpsest pull api:v1 --registry internal
palimpsest images --digests --filter dangling=false
palimpsest history registry.example.invalid/platform/api:v1
palimpsest image inspect api:v1 --registry internal

palimpsest tag local-api:dev api:v1 --registry internal
palimpsest push api:v1 --registry internal
palimpsest logout --registry internal
```

A positional login/logout server and `--registry` are mutually exclusive.
`pull -a` and `push -a` require an untagged repository.

### Archive, remove, and pass through

```sh
palimpsest save registry.example.invalid/platform/api:v1 -o ./api.tar
palimpsest load -i ./api.tar
palimpsest image save registry.example.invalid/platform/api:v1 -o ./api-copy.tar
palimpsest image load -i ./api-copy.tar
palimpsest rmi registry.example.invalid/platform/api:v1
palimpsest docker system df
```

Top-level `images/history/rmi/save/load` and `image
inspect/history/rm/save/load` preserve Docker semantics. `palimpsest docker ...`
passes the remaining arguments to Docker while pinning Docker's existing config
directory. It rejects competing global `--config` and password-bearing login
arguments.

### Generate BuildKit registry configuration

```sh
palimpsest registry buildkit-config --output ./buildkitd.toml
```

The command writes an owner-only file and refuses overwrite unless `--force` is
explicit. Applying that file to a BuildKit daemon is an external administrator
operation.

## 4. Build artifacts

See the [build diagram](../diagrams/palimpsest-build.workflow.html).

Every build requires at least one tag. `--frontend auto` chooses the native
Palimpsestfile path when legacy inputs are present; otherwise it chooses the
Dockerfile/BuildKit path.

### Palimpsestfile frontend

```sh
palimpsest build --frontend palimpsestfile -f Palimpsestfile \
  --base "$IMAGE_DIGEST" \
  --layer "$LAYER_DIGEST" \
  --tag app-layer
```

This frontend requires exactly one tag and one `--base`, accepts repeatable
`--layer`, rejects a context positional, and rejects Dockerfile-only options.

### Dockerfile frontend

```sh
palimpsest build . -f Dockerfile -t example/app:dev \
  --platform linux/amd64 \
  --build-arg MODE=dev \
  --local-image base=/srv/oci/base@"$MANIFEST_DIGEST" \
  --load
```

For a registry result plus a Palimpsest runtime block:

```sh
palimpsest build . -t example/app:release --push \
  --runtime-tag app-runtime \
  --runtime-base "$IMAGE_DIGEST" \
  --runtime-block-size 131072 \
  --runtime-push
```

Key invariants:

- Offline mode requires `--network none`; it rejects pull/push, registry, and
  external cache endpoints.
- `--no-cache` is offline-only. Online builds reuse Hub cache.
- `--runtime-tag` and `--runtime-base` appear together; `--runtime-push`
  requires both.
- `--local-image` pins an absolute OCI layout and manifest digest.
- `--output` names an OCI archive path, not raw BuildKit exporter syntax.
- The externally managed Buildx builder must support OCI export.

## 5. Conventional cloud-image VM lifecycle

See the [cloud VM lifecycle diagram](../diagrams/palimpsest-cloud-vm-lifecycle.workflow.html).

### Launch

```sh
palimpsest run "$IMAGE_DIGEST" --name dev --memory 4096 --vcpus 2
palimpsest run ./bundle --name bundled --layer "$LAYER_DIGEST"
```

Backend `auto` selects Linux KVM or macOS Lima/VZ. Select experimental
`libvirt-hvf` explicitly. Conventional runs retain the cloud image as the guest
root and attach validated Palimpsest layers; they do not pivot `/` to OCI
content.

### Observe and operate

```sh
palimpsest ps
palimpsest inspect dev
palimpsest logs dev --follow
palimpsest shell dev
palimpsest exec dev -- /usr/bin/id
```

`ps` and `inspect` read managed inventory. `logs` reads captured console output.
`shell` is interactive; `exec` requires a command and is non-interactive.

### Stop, restart, commit, and remove

```sh
palimpsest stop dev
palimpsest start dev
palimpsest commit dev --tag dev-snapshot
palimpsest rm dev
```

`commit` applies to conventional runtimes. `rm --volumes` is the
conventional/project volume removal request. Review owned resources in
`inspect` before removal.

## 6. Linux amd64 OCI-root runtime

See the [OCI-root diagram](../diagrams/palimpsest-oci-root.workflow.html).

This workflow is Linux `x86_64`/`amd64` KVM only. It is capabilityless and is
not a general hostile-container sandbox.

### Create an isolated runtime and acquire an image

```sh
palimpsest oci init-runtime /srv/palimpsest-runs/demo
# Export the printed PALIMPSEST_STATE_HOME assignment.

palimpsest oci pull quay.io/example/app:stable \
  --output ./app.oci.tar
```

`oci pull` requires Skopeo 1.13+, anonymous HTTPS access, and a
`linux/amd64` manifest. It verifies through the private source CAS before
publishing the caller-selected archive.

### Materialize and run

```sh
palimpsest oci materialize ./app.oci.tar --output ./materialize-receipt.json

# Isolated run: no NIC at all.
palimpsest run ./app.oci.tar \
  --runtime-kind oci-root \
  --name app \
  --network none \
  -d

# Networked run: user-mode NAT with one explicit loopback publication.
palimpsest run ./service.oci.tar \
  --runtime-kind oci-root \
  --name api \
  --network nat \
  --publish 127.0.0.1:18080:8080 \
  -d
```

`--network` selects `nat` (the default when omitted), `host-only`, or `none`.
Omitting `--network` therefore grants outbound user-mode NAT; pass
`--network none` for the historical no-NIC isolation. `--publish` is
OCI-root-only, repeatable, defaults its host address to `127.0.0.1`, requires
host ports 1024–65535, and rejects IPv6 literals. See [OCI-root guest
networking](../oci-network.md) for the full contract.

A foreground run omits `-d`, streams the image workload output, and returns its
exit status. A literal `-- COMMAND ...` replaces only image Cmd while preserving
Entrypoint. `--user USER[:GROUP]` changes the selected OCI user without adding
capabilities, supplementary groups, or ownership changes.

### Inspect root, exec, network, resources, and volumes

```sh
palimpsest oci root-proof app
palimpsest exec --completion-record ./exec.json app -- /usr/bin/id
palimpsest oci exec-status app
palimpsest oci exec-record ./exec.json
palimpsest oci network app
palimpsest oci resource-status
palimpsest oci root-volumes
palimpsest oci root-volume "$ROOT_VOLUME_UUID"
```

`root-proof` preserves the distinction that materialized OCI content is guest
`/`. Direct PID 1 access remains refused. Network, exec, resource, and volume
commands report recorded evidence; they do not prove a stopped VM still has a
live listener.

`--root-retention retain` preserves only the VM-exclusive writable OCI root
volume during removal. Reuse requires the exact UUID, compatible lower graph
and size, and exclusive attachment. OCI-root rejects `start`, `shell`, `commit`,
and conventional `rm --volumes`; use `exec`, `stop`, `rm`, and the OCI root
retention contract.

## 7. Compose-shaped projects

See the [Compose diagram](../diagrams/palimpsest-compose.workflow.html).

This is a strict `palimpsest.yml` subset, not arbitrary Docker Compose. Global
project options precede the subcommand.

```sh
# Validate and inspect the normalized service set.
palimpsest compose -f palimpsest.yml config --services

# Reconcile selected services and return after startup.
palimpsest compose -p demo up -d api worker

# Observe and operate on the project.
palimpsest compose -p demo ps --format json
palimpsest compose -p demo logs -f api
palimpsest compose -p demo exec api -- /usr/bin/id
palimpsest compose -p demo port api 8080 --protocol tcp

# Preserve stopped service records, or remove the project and its requested volumes.
palimpsest compose -p demo stop api worker
palimpsest compose -p demo down --volumes
```

Discovery checks `palimpsest.yml`, then `palimpsest.yaml`.
`--project-directory` contains resource paths; repeatable `--env-file` paths must
remain inside it. `up` rejects simultaneous `--no-recreate` and
`--force-recreate`. Lima supports TCP project publication. Linux cloud-image
project ports and shared writable volumes are currently rejected. OCI-root
per-VM SLIRP publication follows its own contract.

## 8. Local store, dashboard, and completion

See the [store/UI/completion diagram](../diagrams/palimpsest-store-ui-completion.workflow.html).

### Inspect and mutate managed state

```sh
palimpsest store show --format json
palimpsest store ls --kind layer
palimpsest store set --to /srv/palimpsest/store
palimpsest store move --to /srv/palimpsest/store --keep-source
palimpsest store rm "$DIGEST"
```

`store set` changes configuration without moving bytes. `store move` migrates
the selected store and can retain source bytes. `store rm` refuses referenced or
leased objects; `--force` does not bypass those safety checks.

### Start the local dashboard

```sh
palimpsest ui --port 8080 --no-browser
```

The default `--port 0` chooses an available loopback port; explicit ports are
1024–65535. The dashboard reads and mutates through the same Local inventory
layer as CLI commands.

### Install shell completion

```sh
palimpsest completion bash > "$HOME/.local/share/bash-completion/completions/palimpsest"
palimpsest completion zsh > "$HOME/.zfunc/_palimpsest"
palimpsest completion fish > "$HOME/.config/fish/completions/palimpsest.fish"
```

Creating those parent directories and sourcing completion are explicit user
shell operations. `palimpsest __complete` is an internal protocol and is not a
public command.
