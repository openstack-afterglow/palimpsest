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
| OCI root | `oci pull`, `oci materialize`, `run IMAGE`, OCI evidence commands | local-first resolution, Skopeo intake, private source CAS, Linux KVM runtime | Docker registry credentials for automatic acquisition; anonymous HTTPS for `oci pull`; local host/runtime permissions |

Do not pass a Hub `/v1` endpoint to Docker commands. `run IMAGE` resolves local
sources before automatic OCI acquisition; the runtime consumes only verified
local bytes, never a remote registry or Docker's mutable image store directly.

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
2. Create a Python 3.11+ virtual environment.
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
palimpsest image push ./ubuntu.qcow2 --name ubuntu-dev --arch x86_64 --publish
```

`image import` is local-store ingestion, not upload. `image push` is the Hub
upload. `image verify` checks local bytes and requires an expected digest.

`image push` uses an already-recorded architecture or requires explicit `--arch`;
it never silently relabels ARM images as x86_64. For per-client inventory and
read-only chain checks, use `image/layer ls --compatible`, `store ls
--compatible`, and `store/image/layer check`; see the
[compatibility contract](usage.md#target-architecture-and-client-compatibility).

### Layer flow

```sh
palimpsest layer ls --limit 20
palimpsest layer pull "$LAYER_DIGEST" --output ./layers
palimpsest layer pack ./rootfs-overlay --tag app-v1 --arch x86_64
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

### Hub browser build (separate service)

1. Open `https://<your-hub>/app` and enter a project-scoped Keystone token.
   This tab holds the token only in memory; use HTTPS outside localhost.
2. Upload or choose a visible x86_64 raw/qcow2 cloud-image base and record its
   SHA-256 digest. If using parent layers, list their digests root-first; each
   parent must link to the preceding layer and the first must name this base.
3. As a Keystone **system administrator**, submit the output name, full
   Palimpsestfile text, base digest, and ordered layer digests in the Build
   panel. The `FROM`/`LAYER` instructions must match the submitted digests.
   The API queues a job; only the separate Linux KVM build worker runs `RUN`
   instructions in a disposable, network-disabled guest.
4. Refresh the build list until it reports `complete`; then download the
   private SquashFS artifact. The same result is visible to this project via
   `/v1/layers/{digest}/blob` and the existing `palimpsest layer pull` flow.

An unconfigured worker returns 503. A queued response does not prove guest
startup or build success. The local `palimpsest build` command below does not
submit to `/v1/builds`. [Hub deployment requirements](../install.md#hub-service-settings)
describe the separate host, database, and shared blob path.

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

These are **local CLI** builds on the selected host, not Hub server-build
requests. Use the Hub browser flow above when the isolated build must run on
the separately provisioned KVM service host.

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
- `--no-cache` requires offline or local cache mode. Online native-cache builds reuse Hub cache.
- `--cache-mode local` requires OCI output and rejects native/external cache authorities; it does not forbid remote inputs by itself.
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
`libvirt-hvf` or `lima-krunkit` explicitly. Conventional runs retain the cloud image as the guest
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
palimpsest web --port 8080 --no-browser
```

The default `--port 0` chooses an available loopback port; explicit ports are
1024–65535. Open the printed `http://127.0.0.1:PORT/?token=...` URL; the
process stays in the foreground until Ctrl-C/SIGINT or SIGTERM. Without
`--no-browser`, it also opens a browser. Installation and other CLI commands
never start a web service. No Hub connection is needed: the dashboard reads
the same local inventory as CLI commands and shows overview, VM, volume,
layer/image, network, build, and storage views with five-second refresh.

It is read-only unless started with `--allow-control`, which enables image
build/run, consoles, VM lifecycle, image import, local CAS tag editing/removal,
and state-root controls. Build forms can edit an inline Dockerfile and select
local host cache without a Hub connection. Local cache mode is not an offline
guarantee; select offline mode to forbid remote inputs. Build and run requests
return an operation ID immediately and expose bounded completion/output through
the operations view. Run shares CLI local-first resolution and platform checks.
Shutting down the dashboard cancels owned pre-launch preparation, not an already
launched VM. Stop/Remove remain explicit lifecycle actions.

Conventional VM shell consoles provide PTY input and resize. OCI-root consoles
provide non-TTY guest exec with an explicit command, capability-aware controls
and typed exit/signal results; they do not attach to PID 1. Disconnect and tab
unload close only the owned console transport. Successful BuildKit OCI exports
and retained acquired sources appear as local images without runtime packing;
missing archives retain their provenance and availability label.
Volume and network rows are stored metadata or committed configuration, not
live attachment or listener proof; see the
[usage guide](usage.md#local-store-and-ui) for each view's source.

### Install shell completion

```sh
palimpsest completion bash > "$HOME/.local/share/bash-completion/completions/palimpsest"
palimpsest completion zsh > "$HOME/.zfunc/_palimpsest"
palimpsest completion fish > "$HOME/.config/fish/completions/palimpsest.fish"
```

Creating those parent directories and sourcing completion are explicit user
shell operations. `palimpsest __complete` is an internal protocol and is not a
public command.

## 9. Native Nova builds and internal layer workers

This unreleased path does not use local libvirt, nested KVM, or host PCI rebind.
Install the reviewed working wheel with the `cloud` extra on the controller;
install the same wheel and Skopeo/mksquashfs on the internal Linux worker.
The existing `run` backend defaults remain unchanged. The Hub's legacy build
API is not silently redirected to this executor and no Hub deployment is implied.

### Create a cloud-init build

```sh
palimpsest build --executor nova --nova-config ./nova-build.json -t torch-runtime
# The same explicit controller workflow is also available as:
palimpsest cloud build ./nova-build.json --name torch-runtime
```

Choose one of these invocations for a new job, not both. An existing create
intent is deliberately not replayable after a lost response. Inspect the
receipt and exact provider identity instead of creating an unrecorded retry.

The operator-authored JSON has these required sections:

| Section | Exact contract |
| --- | --- |
| `cloud`, `project_id`, `receipts`, `assets_root` | SDK profile name, exact Keystone target, existing private controller receipt directory, and worker catalog below `/srv/palimpsest` |
| `spec` | `schema=palimpsest.nova-build.v1`, unique `job_id`, same `project_id`, explicit `arch` and `base_image_id`, `commands` and nonempty `checks` argv arrays |
| `spec.storage` | Exact `volume_id`, full `filesystem_uuid`, integer `size_gib`, boolean `initialize`; formatting occurs only for an explicitly approved serial/size-matched blank new disk |
| `spec.parent_layers` | Optional base-to-leaf `{digest,size}` descriptors already present on the attached filesystem; mismatches refuse before installation |
| `launch` | Matching `job_id`/image, name, exact GPU flavor/network/project-owned security group IDs and retained boot size; data volume binding comes from `spec.storage` |
| `cloud_init` | Explicit account/SSH policy and same-wheel worker bootstrap; never cloud tokens, application credentials or registry activation commands |
| `ssh` | `user`, existing private `identity_file`, `known_hosts`, absolute worker `python`, optional `jump`. The known-hosts entry uses `spec.job_id` as `HostKeyAlias`, so trust is pinned before a private IP is allocated. |

For emitter-only review, use `palimpsest cloud build-user-data ./build-spec.json
--output ./new-user-data.yaml`. It emits account-free cloud-init and refuses
overwrite. Capture uses the canonical overlay/chroot cleaning policy but puts
upper/work/output on attached ext4 instead of memory. Checks execute before
success is recorded. Kernel-specific NVIDIA modules and CUDA/cuDNN compatibility
remain actual workload requirements, not properties inferred from an image tag.

### Acquire and inspect OCI layers inside the worker

After verifying the intended external mount, create empty mode0700 directories
for `/srv/palimpsest/cache`, `/srv/palimpsest/assets` and `/srv/palimpsest/jobs`.
Initialize once, never after a missing mount:

```sh
palimpsest oci cache --root /srv/palimpsest/cache init
palimpsest cloud assets --root /srv/palimpsest/assets --project-id "$PROJECT_ID" init
palimpsest cloud registry-worker \
  --cache-root /srv/palimpsest/cache --assets-root /srv/palimpsest/assets \
  --jobs-root /srv/palimpsest/jobs ./registry-job.json
palimpsest cloud assets --root /srv/palimpsest/assets --project-id "$PROJECT_ID" ls
palimpsest cloud assets --root /srv/palimpsest/assets --project-id "$PROJECT_ID" verify "$ASSET_ID"
```

`registry-job.json` contains `schema=palimpsest.registry-worker-job.v1`, unique
`job_id`, exact `project_id` and fully qualified OCI `reference`; optional
`timeout_seconds` is 1–3600 per stage, `packer` is an absolute worker executable,
and `consumer` records an exact permanent cache binding. This command executes
the real canonical acquisition/materialization on that worker, preserving original
manifest/config/compressed layers and recording each contextual parent occurrence,
DiffID, conversion key and SquashFS result. Its catalog is not a second blob store.
Completed identical jobs verify/reuse their bytes; failed/uncertain jobs are retained
and require a new explicit job ID. There is no CPU-image execution or implicit
Docker/Hub authentication fallback. Current OCI conversion is Linux amd64 only.

The outer acquisition/image deadline does not relax the versioned intake300-second
or packer120-second policies. Canonical worker tar spools stay in its pinned private
scratch on the selected retained-storage filesystem, not the boot disk's global
`/tmp`; no caller-provided temporary path or credentials are inherited. Treat a
policy-limit failure as a failed job and inspect its evidence before selecting
adequate worker resources, not as permission to bypass the isolated converter.

The worker binds `mksquashfs` and every linked library (including the C library)
before conversion, re-verifies them while it runs, and includes their digests in each
derived-layer key. Package managers must not replace them during a job. On the
2026-10-22 replacement worker a TensorFlow job failed with the generic boundary
error about two seconds after unattended upgrades began replacing `libc6`; the same
request succeeded as a new job once the apt timers were stopped and the running
package transaction had finished. The boundary reports no stable code to the
controller, so that cause is inferred from timing and counters, not captured.
Mask `apt-daily*.timer` or use an immutable image on dedicated workers. A toolchain
change is a new conversion identity: layers lacking a record under it are derived
again (identical SquashFS bytes are reused by physical digest) and the asset ID
differs. Inspect any retained job, successful or failed, later with:

```sh
python -m palimpsest_local.registry_worker \
  --cache-root /srv/palimpsest/cache --assets-root /srv/palimpsest/assets \
  --jobs-root /srv/palimpsest/jobs inspect "$JOB_ID" --project-id "$PROJECT_ID"
```

A successful job additionally re-inspects its cached graph and fully verifies the
registered asset's bytes.

### Prepare shared immutable lowers and private writable consumers

`cloud prepare-runtime --assets-root /srv/palimpsest/assets --project-id
"$PROJECT_ID" ./runtime-policy.json` runs on the single-writer worker. Policy
maps `stacks` names to verified asset identities and supplies the filesystem UUID,
exports/native paths/library paths and trusted activation/check argv. It returns
an account-free manifest and cloud-init, precreates only empty mountpoints and
uses canonical `blob_paths` in place—no duplicate layer copies or hardlinks that
invalidate the source CAS link-count invariant.

Cleanly unmount the worker filesystem, detach its writer and mark the provider
multiattach volume read-only before consumers. Keep original/derived/catalog bytes.

An `umount` in the shell is not proof of release. The capture worker now runs in a
private mount namespace, but earlier capture workers, operator shells and services
can still hold stale copies of the store or its overlay/`/sys`/`/dev` binds (observed
before that change: `systemd-logind` on the 2026-10-22 worker). Such a copy keeps the
ext4 superblock alive and its journal unflushed even though `umount` succeeded in
the main namespace. Before detaching the writer, list every `/proc/*/mountinfo` that
still names the store, run `nsenter --target PID --mount -- umount -R
/srv/palimpsest` for each holder, then require `dumpe2fs -h` to show
`Filesystem state: clean`, no `needs_recovery` feature and `Journal start: 0`. A VM
deleted with `needs_recovery` set leaves a journal that read-only no-replay
consumers cannot use.

Bind this already sealed volume using `cloud assets … bind-store`, then use
`cloud launch-batch ./consumer-batch.json` from a controller that can verify the
same pre-mounted catalog/bytes. The explicit batch contains `cloud`, `project_id`,
`receipts`, `assets_root`, `asset_id`, `volume_id`, and `instances` with complete
instance requests plus cloud-init paths. Verification and atomic consumer reservation
precede concurrent creation. Creation returns `created-not-ready`, never guest/GPU
success. Every guest independently requires full UUID, blockRO=1, ext4 no-replay,
content hashes and read-only lower identities, then mounts private writable
OverlayFS stacks and exports using boot-local upper/work. Optional
`consumer_state_root` selects an explicitly owned separate ext4/XFS scratch
filesystem; it is not a provisioning request. State binds the exact guest, store,
ordered lowers, writable filesystem UUID and upper/work inodes. V2 policies reject
legacy RO stack mounts: quiesce/unmount old exports and stacks before applying a
new consumer policy outside a frozen v1 store. Never multiattach writable ext4.

`cloud assets … release CONSUMER` only removes metadata. Last-consumer release,
instance stop/delete and failed acquisition do not remove permanent layer bytes.
An explicit `cloud instance --config … delete JOB` additionally verifies fresh
delete-on-termination=false attachment flags before deleting that exact Nova VM.
It does not delete its boot/data volumes.

### Register locally built OCI outputs

The source-only `oci_build_assets.LocalBuildEvidence` and
`register_local_oci_build` Python API accepts the actual paired successful
BuildKit-v2 source/package records and original OCI archives, not fabricated
registry requests or job receipts. It verifies build keys/specs, archive sizes and
digests before and after canonical acquisition, the unchanged package Dockerfile,
and the exact local-export artifact passed into the assembly. That handoff must
also equal the artifact resolved from the first solve's canonical source OCI
layers; rootfs-export bytes alone are not trusted. Full source commit, both
context/Dockerfile/record identities and the handoff digest bind `source.kind =
oci-build`. Original runtime layer occurrences retain compressed, DiffID, recipe
and physical SquashFS identities; existing LayerCache bytes are reused in place.
Only native `nova-build` assets opt out of zero mkfs time; `oci-build` stays strict.
The original source context must be the exact pristine Git worktree top-level:
HEAD must equal the pinned commit and tracked, untracked and ignored dirt all
fail before/after registration. Keep OCI archives and local build exports outside
that context. The original `COMMIT` file must agree; it is not authority by itself.

Source handoff scanning reuses bounded normalized changesets in the registration
caller and ambient temporary scratch. It is **not** the hard materializer worker's
RLIMIT/hard-deadline boundary; runtime conversion still uses that unchanged hard
worker. These local receipts are not signed Git/build attestations. Callers own
source acquisition/authentication and retain real build evidence. Catalog support
for repeated physical output digests does not imply external runtime support:
check actual lower digest uniqueness before preparation; never silently dedup.
No new public CLI or build/provisioning side effects are introduced by registration.

### Sequential GPU measurement

Every new comparison first runs the [disk/network environment benchmark](../benchmarks.md).
The GPU plan supplies `environment_benchmark` (API-verified CPU qualification guest)
and explicit `environment_release_argv`; the script requires successful disk/HTTPS/internal
TCP measurements, then confirms no project VM remains before starting serial GPU consumers.
Provision qualification peers separately, within their approved scope. Historical timings
without this preflight remain historical and are never relabelled as newly qualified.
New receipts record request-time flavor specifications and Nova allocation flavor separately.


`scripts/benchmark_nova_gpu.py --config ./benchmark.json` requires all eight
Torch/TensorFlow × cloud-init/Nova-layer/registry-layer/Ansible cases, five
repetition rounds, `max_parallel_instances=1` and retained boot volumes. Every
case supplies complete original user-data, a real native `readiness_argv` and,
for Ansible, the actual installation playbook. Readiness emits JSON with exact
`framework`, `uid`, `boot_id`, `gpu_uuid`, `cpu_fallback=false` after real GPU math.
The tool polls ordinary-user `nvidia-smi` while cloud-init/Ansible installation is
still progressing, and records that first success separately from later framework
GPU readiness. It confirms boot/device/user identities through pinned SSH without
login/profile, sudo, chroot or private environment overrides.

Set an explicit `launch.boot_gib`; each new boot is checked against both remaining
Cinder gigabytes and volume-count quota. Type-specific provider limits must also be
provisioned by the operator. An optional `management_floating_ip_id` reuses only an
unbound project-owned floating IP and exactly one owned server port; it never
allocates a new IP. Results record `fip_bound_monotonic_seconds` and
`fip_binding_seconds` separately. Request-to-readiness still includes routing delay;
do not attribute that delay solely to the installation/runtime method.
Readiness probes retain their latest actual `.stdout`, `.stderr` and `.status.json`
beside the run result, including when the deadline expires; a failed original
image is not replaced by a dependency-supplemented image to complete the matrix.
An observed first-NVIDIA endpoint is retained even if the next identity collection
fails. Such runs remain failed, with `failure_request_elapsed_seconds`, the actual
subprocess return code and private stdout/stderr preserved; framework readiness is
never inferred from successful `nvidia-smi` alone.

Raw per-run results and one-second disk/network samples remain in the private
evidence root. Summary retains failures/missing repetitions instead of inventing
five successes. Disk/network rates are guest virtual-device deltas, not Ceph
physical I/O, provider image-copy speed or link-capacity measurements. Cold
acquisition/build preparation must be reported separately from warm consumer boot.
Cleanup deletes only each exact new VM and retains every boot/data volume;
uncertain cleanup, accepted-create response loss and quota exhaustion stop the
next allocation. GTX1080Ti attempts failed before guest readiness. The separately
approved RTX3060LHR condition passed probe and builder checks, but fresh consumers
and the five-repetition matrix are not yet qualified; see the handoff.

