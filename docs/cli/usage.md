# Palimpsest CLI usage guide

This guide explains what the commands do and where similarly named commands cross different trust and storage boundaries. The generated [syntax reference](reference.md) lists every parser-visible command, positional, option, choice, and default.

## Invocation and configuration

```sh
palimpsest [--url HUB_URL] COMMAND ...
palimpsest --version
palimpsest completion bash
```

`--url` is a global Hub URL override. Hub commands resolve the URL in this order: `--url`, `PALIMPSEST_URL`, then `config.toml` (`url` or `[hub].url`). They require `PALIMPSEST_TOKEN`; do not put tokens in examples or configuration committed to source control. Registry wrapper commands reuse Docker's existing config and credential directory: `DOCKER_CONFIG` when set, otherwise `$HOME/.docker`.
`--cloud NAME` authenticates Hub commands with the Keystone token and project of that `clouds.yaml` cloud instead of `PALIMPSEST_TOKEN`; the project ID is sent as `X-Project-Id` so Hub does not fall back to the user's default project. `--cloud` applies to `image`/`layer`/`bundle` Hub commands, `published push|pull`, `instance` commands, and `run`/`build`/`compose` when they reach Hub. Other commands reject it instead of ignoring it, so a write never silently falls back to `PALIMPSEST_TOKEN`. `--cloud` cannot be combined with `--anonymous` or `--image-key-file`.

Options are parsed in the command position shown in the reference. `palimpsest docker ...` is special: everything after `docker` is passed to the Docker CLI. `palimpsest __complete` is an internal completion protocol, not a public command.

## Three image namespaces

Do not treat these similarly named surfaces as interchangeable:

| Surface | Examples | Backend and credentials |
| --- | --- | --- |
| Palimpsest Hub artifacts | `image ls`, `image pull`, `image push`, `image git-build`, `image git-job`, `layer ls/check/pack/pull/push`, `bundle *`, `published push/pull/status` | Native Palimpsest Hub `/v1`; `PALIMPSEST_TOKEN` for Hub operations (local `layer pack` needs none); `published` also accepts an image API key and anonymous public pulls |
| Hub instances | `instance ls/status/create/advance/stop/rm` | Native Palimpsest Hub `/v1/instances`; `PALIMPSEST_TOKEN` or `--cloud`. See [Hub instances](#hub-instances). |
| Docker wrapper | `pull`, `push`, `tag`, `images`, `history`, `rmi`, `save`, `load`, and `image inspect/history/rm/save/load` | Installed Docker CLI and external OCI registries; Docker credentials |
| Local OCI root | `oci pull`, `oci materialize`, `run ./image.oci.tar --runtime-kind oci-root` | Anonymous TLS registry acquisition into a verified local archive; private source CAS and Linux KVM runtime |
| Permanent shared OCI cache | `oci cache --root PATH init/pull/import/materialize/retain/release/inspect` | Explicit private directory on pre-mounted persistent storage; anonymous verified TLS for pulls, current local CAS/materializer authority |
| External runtime user-data | `layer cloud-init --config runtime.json` | Local JSON-to-cloud-init export only; no credentials, network, or state initialization |

Palimpsest Hub is not an OCI Distribution `/v2` registry. A fully qualified anonymously accessible reference such as `quay.io/example/nginx:stable` can be acquired with `palimpsest oci pull ... --output ./nginx.oci.tar`; `run` still accepts only the resulting verified local source. See [anonymous TLS OCI registry intake](../registry-intake.md) for its authentication, TLS, endpoint, platform, path, size, and timeout limits.

For repeated acquisition across images/instances, use the explicit
[permanent shared layer cache](../registry-intake.md#permanent-shared-layer-cache).
It retains compressed and derived digest-addressed bytes after the last
consumer is released; it does not automatically deploy a cache or attach disks.

## Hub cloud images, layers, and bundles

`image ls` queries cloud-image metadata and accepts server-side filters plus `--limit` (default 50, valid 1–200). `image pull DIGEST` verifies the Hub object is a complete cloud image, stores it locally, and optionally copies it beneath `--output`. `image verify PATH --digest DIGEST` checks local bytes. `image import` adds an existing qcow2/raw image to the local store; it does not upload it. `image push` uploads a local cloud image and requires `--name`; `--publish` controls Hub visibility.

```sh
palimpsest --url https://hub.example image ls --arch x86_64 --disk-format qcow2
palimpsest image pull "$IMAGE_DIGEST" --output ./downloads
palimpsest image import ./ubuntu.qcow2 --disk-format qcow2 --arch x86_64 --os-variant ubuntu24.04
palimpsest image push ./ubuntu.qcow2 --name ubuntu-dev --arch x86_64 --publish
```

`layer pack DIRECTORY --tag TAG` produces a local SquashFS layer with the fixed zstd recipe. `layer push VALUE` accepts a local tag/digest and optionally supplies Hub chain metadata; `--parent`, `--base-image`, and `--ubuntu-base` describe that chain. `layer ls` has the same 1–200 `--limit` restriction. `bundle pull` exports a Hub chain as an OCI image-layout directory and can include its base image. `bundle verify` verifies an extracted bundle directory.

```sh
palimpsest layer pack ./rootfs-overlay --tag app-v1 --arch x86_64
palimpsest layer push app-v1 --name app-v1 --base-image "$IMAGE_DIGEST"
palimpsest bundle pull "$LAYER_DIGEST" --output ./bundle --include-base
palimpsest bundle verify ./bundle
```

`bundle unpack ARCHIVE --output DIRECTORY` verifies a downloaded runtime-bundle tar locally and publishes the extracted directory atomically. The destination must be absent or empty; corrupt digests, unsafe archive members and partial extraction are rejected. It does not contact Hub or execute a guest. A runnable bundle must declare its cloud-image base architecture; ordered SquashFS layers are selected automatically and incompatible architecture metadata is refused.

```sh
palimpsest bundle unpack ./palimpsest-runtime.oci.tar --output ./palimpsest-bundle
palimpsest bundle verify ./palimpsest-bundle
palimpsest run ./palimpsest-bundle --name bundled
```

Run only on a compatible host with documented backend prerequisites and a matching raw/qcow2 base plus known matching base/layer architecture. The verified bundle resolves base and ordered layers automatically; no manual `--layer` selection is required. No-base chains remain reusable/discoverable but not locally runnable. Catalog local_supported is metadata eligibility, not byte/guest readiness; download authority remains independent of public/site/project/shared visibility. New catalog routes require matching Hub rollout. Neither rollout nor VM boot was performed for source qualification.

### Published images: local catalog ↔ Hub

`published push IMAGE --name NAME [--visibility private|internal|public]` publishes one completely materialized local catalog image (`IMAGE` is its `sha256:` image id) as a Hub published image. `NAME` must match `^[a-z0-9][a-z0-9.+-]{0,63}$` (checked before anything is uploaded); one name is one image per scope. Visibility defaults to `private`; `--visibility public` additionally requires `--confirm-public`. The manifest is built from the catalog's ordered per-layer SquashFS conversions, including the original base layers with their compressed descriptors and DiffIDs, and carries only content-derived conversion facts, so the same image published from two state roots has the same manifest digest. `--os-family`/`--os-version` record actual OS facts (default `unknown`, never guessed).

By default the push also uploads the image's original OCI source closure (root index if any, image manifest, config and every compressed layer, re-verified from the local source CAS), which makes the image runnable after a pull. Before uploading, the command prints who can read it: the config, including environment variable **values**, becomes readable by everyone who can read the image — only you for `private`, every member of your project for `internal`, anyone anonymously for `public`. The public manifest projection hides those values, but the closure does not. `--no-source` publishes layers and metadata only; such an image cannot be run after a pull.

Publication status lives in the local catalog. A row is written as `pending` with a request id before the first network call; `pending` also means an interrupted attempt whose outcome is unknown. The row becomes `published` only after the Hub's returned image id, owner project, manifest digest, name, visibility and layer chain match the local manifest. A definitive 4xx refusal is recorded as `failed`; a 5xx, timeout, transport error or mismatching response as `unknown`. Local bytes that change after they were verified are reported as a local integrity error (recorded as `failed`, nothing registered), not as a Hub outcome. Re-running the same command reuses the same request id, so the Hub replays an already committed publication. An unpublished generation (`pending`, `failed`, `unknown`) belongs to the credential that began it (an image key by its public `ppi_v1_` id; every credential without a key id counts as one owner): another credential never retries, adopts or ends it. A Hub `request_id conflicts` answer (in practice between Keystone-token users, for example another user's token retrying your token's row) or a second mismatching response ends that request id; the next attempt starts a new generation with a new id. Every `published` generation of the same intent is revalidated with the current credential, newest first, and the first that still matches (image, manifest digest, name, visibility, owner project and closure) is reused, so alternating credentials of different projects do not publish duplicates. A deleted image or a credential that cannot see it (HTTP 404) makes a generation stale; only when none matches is a new generation started, and concurrent runs of the same credential share that one new generation. A credential that may publish but not read (HTTP 403 on the read, for example an image key with `images:publish` but not `images:read`) cannot validate any row by reading, so it only tracks publications that this same image key recorded: it revalidates one of them by replaying its stored Hub request id through the normal publication request (the Hub answers it idempotently per project and user); a replay answer that matches the intent is adopted without another request, and the row is rebound if the Hub re-registered the image in the same project, while a non-matching answer makes the row stale. A credential that cannot read never replays or adopts publications recorded by other credentials (a read-capable credential reuses any matching image it can read), and a credential without an image-key id that cannot read refuses and asks for `images:read`. Quota refusals name the limit: HTTP 403 `image_count_limit`, HTTP 413 `image_chain_size_limit` (distinct layer bytes) or `image_source_size_limit` (distinct closure bytes); GiB = 2^30.

Replay details: a credential's own unresolved attempt is retried with its own request id, even when another credential has published a newer generation, and while it exists no replay runs; the exception is a published row that revalidates by reading with that credential, whose image is reused instead (a credential without a key id that cannot read refuses when any published row exists). A refused or uncertain replay never starts a new generation: a definitive refusal (a 4xx other than 404 and 409, for example 403 or 429) is reported as refused before anything was registered and leaves the row unchanged; an uncertain failure (5xx, timeout, transport error) is reported as outcome unknown, never as "nothing changed", and re-running replays the same request id. HTTP 404 (this key has not staged the payloads) starts a new generation that re-sends the same Hub request id, so the Hub answers an image it already holds instead of registering another. HTTP 409 ends the row's request id. By design these situations can still register a second Hub image: switching credentials while an attempt is uncertain (the other credential cannot see that attempt and publishes its own; switching back retries the original unless the original credential can read the other credential's image, which it then reuses); two different image keys of one user; publish-only keys of different users of one project (including `internal` images of the same name); a publish-only key next to a read-capable credential, because a key that cannot read cannot recognise images published by other credentials; Keystone tokens, which have no key id and therefore share unresolved attempts across users and projects (another user's retry ends the request id with a 409, a token of another project registers the id in its own project); and the same image published from two state roots, because tracking lives in each root's catalog. Using one image key per publication avoids all of them; a credential with `images:read` avoids only the publish-only cases. A Hub that answers replays with a different manifest (a broken Hub; the real Hub does not) makes the key start a fresh generation each time, and a refused attempt retried later can then register an extra image (about one per two runs while the Hub keeps answering that way).

Known limit: the arm64 pull precheck counts only the declared compressed closure bytes against the 8 GiB local OCI source intake limit; decompressed layer bytes are counted separately against the same limit during registration, so a registration refusal after the download is possible. It is reported as `source_registration_error` with the downloaded layout kept, not as a failed pull.

`published pull HUB_IMAGE_ID --state-root DIR [--name NAME] [--anonymous]` downloads every SquashFS payload into `DIR`'s store and, when the image carries its source closure, every closure blob, each with SHA-256 and size verification (a corrupt, short or oversized blob leaves no file). The image's manifest must commit to its closure (manifest, config and root digests, and each layer's original compressed digest and size by ordinal); root/manifest/config documents above 1 MiB are refused before download. It verifies the OCI chain itself (root index lists the manifest; manifest lists the config and every layer by digest and size; config platform and DiffIDs match; documents with repeated JSON keys are refused), rebuilds a standard OCI image layout at `DIR/published/<image-id>/layout` atomically (read-only files in owner-only directories, no symlinks), registers that layout through the normal local OCI source intake, records the pull in `DIR`'s catalog as source `published` (Hub URL, image id, manifest digest, closure, layout) and prints the exact `plm run` command with its host requirement (Linux x86_64 with KVM for amd64; a qualified Ubuntu 24.04 arm64 Lima VZ profile on macOS for arm64). Downloaded SquashFS layers are kept. It never starts a VM or converts layers, and touches no other state root. Before anything is downloaded, declared sizes are checked against a client cap, the local OCI source intake limit (8 GiB for arm64 acquisition) and the state root's free space (payloads plus the closure twice: layout and source CAS copy). Concurrent pulls of one image into the same state root share one layout; staging directories in which nothing (directory or blob file) was modified for a day are treated as abandoned and removed by the next pull, so a slow active download is kept. Repeating a pull of identical content is a no-op; a differing identity, chain or closure for an already registered image id is refused. There is no command to remove a pulled image yet. `store rm` refuses to delete payloads that a pulled published image references; `store move` relocates pulled layouts with the rest of the state root. `published status [--state-root DIR]` prints local publication rows and pulled images without contacting Hub.

Credentials for `published`: `--image-key-file PATH` (owner-only file holding one `ppi_v1_` image API key), then `PALIMPSEST_IMAGE_KEY`, then `PALIMPSEST_TOKEN` (printable ASCII without spaces). Exactly one is sent. `pull` without any credential, or with `--anonymous`, reads `public` images anonymously; private/internal images then answer as not found. Credentials are never written to the catalog and never printed, including in error messages; unexpected errors are reported by type only. The Hub URL must not embed credentials, redirects are never followed, and credentials are not sent over plain `http://` to a non-loopback host unless `--allow-insecure-http` is given.

```sh
palimpsest published push "$LOCAL_IMAGE_ID" --name app --os-family ubuntu --os-version 24.04
palimpsest published pull "$HUB_IMAGE_ID" --state-root "$HOME/palimpsest-consumer/state" --anonymous
palimpsest published status --state-root "$HOME/palimpsest-consumer/state"
```

### Hub instances

`instance` drives the same Hub `/v1/instances` endpoints as `HubClient.create_instance`, `advance_instance`, `stop_instance` and `delete_instance`; it adds no server behavior.

- `instance create` reserves the boot volume only. The server is created by a separate `instance advance INSTANCE_ID --operation create-server --request-id ... --boot-volume-id ...`, which binds the effect to the volume that `create`/`status` reported. `--operation delete-volume` releases a reserved volume that never got a server.
- `instance status` is a point-in-time read. When the guest has reported a result, it contains `result.stdout` and `result.stderr` (each at most 8192 characters), `exit_code` and `error_code`; there is no streaming log endpoint.
- `instance stop` stops the server and keeps the instance. `instance rm` is the only command that deletes an instance and its guest-private upper layer. Neither is retried automatically.
- `instance ls --limit` accepts 1-100 (the Hub route maximum).
- Local arguments (UUIDs, JSON argv arrays) are validated before any credential is read or Hub call is made. `--cloud` is accepted.
- The Hub exposes no SSH-connection-info or resume route, so the CLI has none.

```sh
palimpsest instance create --request-id "$REQ" --name dev-box --image-id "$IMAGE" --flavor-id m1.small \
  --network-id "$NET" --boot-gib 20 --layer-digest "$LAYER"
palimpsest instance advance "$ID" --operation create-server --request-id "$REQ" --boot-volume-id "$VOL"
palimpsest instance status "$ID"
palimpsest instance stop "$ID"
palimpsest instance rm "$ID"
```


### External copy-on-write runtime user-data

`layer cloud-init --config PATH [--output PATH]` validates a local JSON manifest
and calls `build_external_layer_user_data` to generate `#cloud-config`. Without
`--output`, stdout is exactly the generated configuration, without a receipt or
an extra newline. `--output` writes an owner-only file, refuses an existing
destination, and prints its path. There is no `--force` option. Invalid JSON or
manifest policy fails before any Palimpsest state initialization, journal,
network request, or authentication.

For example, save this as `runtime.json`. The UUID and digests are illustrative:
replace them with the prepared filesystem UUID and hashes of your actual
SquashFS blobs. The packed layers must contain the shown runtime subtree and
all its required native dependencies for the chosen guest image and architecture.

```json
{
  "schema": "palimpsest.external-layer-runtime.v2",
  "arch": "x86_64",
  "filesystem_uuid": "12345678-1234-4123-8123-123456789abc",
  "stacks": [
    {
      "name": "example",
      "layers": [
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      ]
    }
  ],
  "exports": [
    {"stack": "example", "path": "opt/example-runtime", "target": "/opt/example-runtime"}
  ],
  "paths": ["/opt/example-runtime/bin"],
  "library_paths": ["/opt/example-runtime/lib"],
  "activation_commands": [],
  "checks": [
    ["/opt/example-runtime/bin/python3", "-c", "import ssl; print(ssl.OPENSSL_VERSION)"]
  ]
}
```

```sh
palimpsest layer cloud-init --config ./runtime.json --output ./runtime-cloud-init.yaml
```

The versioned manifest accepts only these fields:

| Field | Contract |
| --- | --- |
| `schema` | Exactly `palimpsest.external-layer-runtime.v2`; old v1 read-only runtime policies must be regenerated explicitly. |
| `arch` | `x86_64` or `aarch64`; activation rejects a different guest architecture. |
| `filesystem_uuid` | Canonical UUID identifying an already populated ext4 filesystem; never a device name or a formatting request. |
| `storage_read_only` | Optional boolean, default `false`. `true` mounts the ext4 store `ro,noload,nosuid,nodev`, requires a read-only block device, and refuses journal replay. Use only a clean, finalized filesystem with all blob/layer/runtime mountpoints already present. Every provider attachment must be read-only; no external writer may modify shared bytes. |
| `consumer_state_root` | Optional, default `/var/lib/palimpsest/consumer` on the guest's writable boot filesystem. An explicit path strictly under `/mnt` or `/var/lib` must use a separate, operator-owned whole writable ext4/XFS block filesystem. Never shared across consumers, in the lower store, on a network filesystem, through symlinks or bind aliases. The exporter does not allocate or format scratch. |
| `stacks` | Nonempty list of objects containing only unique lowercase-slug `name` and nonempty `layers`, ordered base to top. Layers are canonical `sha256:<64 lowercase hex>` digests. Repeated physical digests within one stack are refused (kernel lower-dentry overlap); catalog occurrence graphs may preserve repetitions but cannot be silently deduplicated for consumption. Mount options must fit the bounded kernel input. |
| `exports` | List of objects containing only `stack`, safe relative subtree `path`, and absolute `target` beneath `/opt` or `/usr/local`. Stack references must exist; export targets must not overlap. |
| `paths`, `library_paths` | Lists of absolute directories inside a configured export or `/srv/palimpsest/runtimes/<stack>`. No home-directory runtime roots, traversal, or arbitrary host directories. |
| `activation_commands`, `checks` | Lists of nonempty argv arrays of strings without NUL or newline; no implicit shell expansion. These are explicitly trusted operator-authored root boot policy, never automatically imported Hub commands. Checks run after activation commands. |
| `blob_paths` | Optional digest-to-relative-store-path map for canonical cache reuse. Only declared digests are allowed and each path ends in its full hex digest; no copies or hardlinks. |
| `command_links` | Optional native command-name to executable path map within a configured stack/export; generated `/usr/local/bin` links refuse unrelated existing files and escaping source links. |

Optional lists may be empty. Unknown keys, unsafe paths, collisions, and invalid
digests are errors. The exporter does not inspect binary ABI or certify GPU,
CUDA, Vulkan, driver, or guest-kernel compatibility; pack and qualify those
dependencies explicitly.

The guest mounts the external ext4 filesystem at `/srv/palimpsest`, verifies
regular non-symlink blobs at `blobs/sha256/<hex>`, and checks each SHA-256 before
mounting it read-only with `nosuid,nodev` at `layers/<hex>`. Stacks appear at
`runtimes/<name>` as writable OverlayFS mounts with top-first read-only lowerdirs
and consumer-private upper/work directories, including single-layer stacks.
Declared root-owned native prefixes bind-export this same writable merged view.
Existing mounts/exports must match the exact expected identity; unrelated mounts
are not adopted or overwritten. Upper/work share one filesystem and live beneath
root-private per-stack state; an owner record binds the stack, ordered lower
digests, store UUID, state filesystem UUID, upper/work inodes, machine-id and VM
UUID. A DMI product UUID or aarch64 device-tree `vm,uuid` is required; absence fails
closed. Changed identities, missing/replaced recorded state and legacy RO views
are refused rather than repaired. Writes/copy-up/deletion whiteouts never modify
the SquashFS blobs. Space consumption is bounded by the selected boot/scratch
volume's capacity, not an implicit memory tmpfs or inherited `TMPDIR`.

For multiple guests sharing the same Cinder multiattach volume, set
`storage_read_only: true` and precreate `blobs/sha256`, every `layers/<hex>`, and
every `runtimes/<name>` directory before finalizing the clean filesystem.
Mountpoint directories must be empty, root-owned and not group/world-writable.
Each guest owns its own loop/overlay mount state, upper/work and native configuration;
the shared filesystem contains only immutable bytes and empty mountpoints.
Do not multiattach a writable ext4 filesystem or use `noload` to hide a dirty
filesystem. A guest-local read-only mount alone does not prove the other
attachments are read-only; verify provider attachment policy separately.

For an already frozen v1 store, write the v2 policy and generated activation files
outside that store. Quiesce all runtime users, then unmount the old exports and
runtime stack mounts before applying the generated helper and systemd files;
keep the verified RO lower loops and provider seal. Activation does not unmount
or replace old views automatically. Restarting the new service verifies and reuses
the same private state, retaining copy-up changes. To discard a consumer, quiesce
its users, unmount exports then runtime, and remove only that consumer's owned
state; never erase or rewrite canonical blobs. Boot-volume deletion also discards
default state. This is an image-derived native view, not OCI-root isolation/parity.

`paths` are registered through `/etc/profile.d/palimpsest-runtime.sh`, including
interactive Bash, and `library_paths` through
`/etc/ld.so.conf.d/palimpsest-runtime.conf` and `ldconfig`. After successful boot
activation, an ordinary first SSH login can invoke the configured native tools
without sudo, chroot, a manual entry script, or a user-home runtime tree. The
systemd runtime service requires the external mount and successful checks. SSH
daemon activation/login requires successful runtime activation; the socket may
listen earlier but cannot start a working daemon before that prerequisite.
Failure remains closed to SSH, with provider console recovery available. Restarts verify expected
existing mounts rather than formatting or repairing storage.

This command is **not a Nova provisioning backend**. Prepare and attach the
prepopulated volume with external provider tooling, compose the generated
cloud-config with your separately managed SSH/account policy, and supply that
user-data when creating the VM. This version selects an externally managed
Cinder-style ext4 block volume; it does not provision Cinder resources or mount
Manila shares, and no Manila provisioning backend is implemented. See the
[provider workflow](../vm-workflow.md#external-volume-runtime-provider-workflow).

### Target architecture and client compatibility

Cloud images and VM-built SquashFS layers use canonical `arch: x86_64` or
`arch: aarch64`. Palimpsestfile builds and commits take the target from the
verified guest base, not the host running the CLI; BuildKit runtime packs keep
their selected Linux platform (including supported variants). Upload/download
and per-artifact bundle configs preserve the declaration. Known conflicting
architectures cannot silently replace an existing local blob descriptor or mix
within a runtime stack.

For arbitrary directories/files, `layer pack/push --arch` is a declaration by
the publisher, not binary analysis. Omission leaves a legacy layer unknown.
`image push` reuses the file's recorded architecture; an unrecorded image
requires `--arch`, and a conflicting override fails before publication.

```sh
# Local inventory; no Hub or virtualization tools required.
palimpsest store ls --compatible --format json
palimpsest store ls --kind layer --arch aarch64
palimpsest store check app-v1 --backend lima-krunkit

# Hub inventory and full dependency checks; metadata only, no blob download.
palimpsest image ls --compatible --format json
palimpsest layer ls --compatible --backend lima-krunkit --format json
palimpsest image check "$IMAGE_DIGEST"
palimpsest layer check "$LAYER_DIGEST"
```

`check` emits JSON with `status` (`compatible`, `incompatible`, or `unknown`),
`compatible`, declared architecture/platform, client/backend, dependency
descriptors and reasons. Exit status is 0 only for `compatible`, otherwise 1.
A layer needs a complete root-bound cloud-image base and known matching target
on every parent; missing/invisible metadata, missing local bytes, or a legacy
unlabelled layer never count as compatible. `--arch` alone can browse foreign
targets; `--compatible` evaluates the current runtime client and optional
`--backend` without probing or starting VMs. Hub architecture filters apply
server-side; compatible CLI queries continue through candidate pages until the
requested number of complete compatible results is found or inventory ends.

These are platform-metadata checks, not GPU/Vulkan/CUDA, guest ABI, hardware
availability or boot qualification. Runtime preflight remains mandatory.
OCI-root supports Linux amd64 KVM and an explicitly configured, qualified
Ubuntu24.04 arm64 Lima VZ profile on macOS. Native VZ requires its pinned boot
assets and Linux worker; matching an image's CPU architecture alone does not
establish execution admission. Registry acquisition support remains separate.

The Docker-compatible `image inspect`, `image history`, `image rm`, `image save`, and `image load` commands are aliases for Docker-backed operations; they do not inspect or mutate Hub `/v1` cloud-image records.

## Registry profiles and Docker wrapper commands

`registry add NAME ENDPOINT` records endpoint resolution, namespace, mirrors, CA files, transport flags, and BuildKit cache endpoints. The `docker` profile defaults its namespace to `library`; other names default to an empty namespace. `--default` selects the new profile, and `--force` replaces an existing profile. `registry buildkit-config --output FILE` refuses to overwrite unless `--force` is present and writes an owner-only file.

```sh
palimpsest registry add docker docker.io --namespace library --default
palimpsest registry add internal registry.example --ca /etc/company/company-ca.pem --mirror mirror.example
palimpsest registry ls --format json
palimpsest registry buildkit-config --output ./buildkitd.toml
```

`login`, `logout`, `pull`, `push`, and `tag` resolve short references through a selected registry profile. A positional login/logout server and `--registry` are mutually exclusive. `pull -a` and `push -a` require an untagged repository. `images`, `history`, `rmi`, `save`, and `load` preserve Docker CLI meanings. Repeat `--platform` where the reference permits multiple platform selections.

```sh
printf '%s\n' "$REGISTRY_PASSWORD" | palimpsest login --registry internal -u ci --password-stdin
palimpsest pull --registry docker nginx:1.27
palimpsest images --digests --filter dangling=false
palimpsest docker system df
```

The wrapper requires a working `docker` executable/daemon. `palimpsest docker` passes arguments through while pinning Docker's existing config directory. It rejects a competing Docker global `--config` and rejects `docker login --password`/`-p`; use `DOCKER_CONFIG` and `--password-stdin`.

## Build

`build` selects `palimpsestfile` or `dockerfile`; `auto` chooses the legacy frontend when `--base` or `--layer` is present, or when no context is given and the recipe is named `Palimpsestfile`; otherwise it chooses Dockerfile/BuildKit. Every build requires at least one `--tag`.

Legacy Palimpsestfile builds require exactly one tag and `--base`, reject a context positional, and accept `--layer` plus `--network`. They reject every Dockerfile/BuildKit-only option listed in the generated reference.

```sh
palimpsest build --frontend palimpsestfile -f Palimpsestfile \
  --base "$IMAGE_DIGEST" --layer "$LAYER_DIGEST" --tag app-layer
```

Dockerfile builds accept a context, multiple tags, `-f`, platform/target/build args, named local OCI contexts, registry and cache selection, output controls, and optional runtime-block production.

The default `--cache-mode native` requires an authorized Hub cache profile and package. `--cache-mode local` uses only the managed host cache and an OCI output profile; it needs no Hub URL or token and rejects native/external cache authorities. Local cache mode is not offline mode: remote Dockerfile inputs remain possible unless `--offline` is also selected.

```sh
palimpsest build . -f Dockerfile -t example/app:dev --cache-mode local --registry docker
palimpsest build . -f Dockerfile -t example/app:dev \
  --platform linux/amd64 --build-arg MODE=dev \
  --local-image base=/srv/oci/base@"$MANIFEST_DIGEST" --load
palimpsest build . -t example/app:release --push \
  --runtime-tag app-runtime --runtime-base "$IMAGE_DIGEST" --runtime-block-size 131072 --runtime-push
```

Build restrictions enforced before execution:

- `--offline` requires `--network none` and rejects `--pull`, `--push`, `--runtime-push`, `--registry`, and external `--cache-from`/`--cache-to`.
- `--no-cache` requires `--offline` or `--cache-mode local`; online native-cache builds must reuse Hub cache.
- `--runtime-tag` and `--runtime-base` must appear together; `--runtime-push` also requires both.
- `--local-image` accepts `ALIAS=/absolute/layout@sha256:DIGEST`, `ALIAS=/absolute/image.oci.tar@sha256:DIGEST`, or `ALIAS=AUTHORITY/NAMESPACE/PACKAGE@sha256:DIGEST` after an explicit native `pull`. Archive/reference inputs use a private verified layout held through solve/runtime packing, then removed. Exact retained receipts bind archive hash/size, root and package type; mutable tags, runtime bundles and OnBuild triggers are refused. This does not make Hub a Docker `/v2` registry or grant server-build authority. `--runtime-block-size` defaults semantically to 131072 bytes and must be a power of two from 4096 through 1048576.
- `--output` is an OCI archive path, not arbitrary BuildKit exporter syntax; omission creates an invocation-unique archive beneath local build state.
- Dockerfile builds reject legacy `--base`/`--layer`; use `--runtime-base` and `--local-image` instead.
- `--progress` defaults to `plain`; build networking defaults to `none`.

### Local Dockerfile `.plmignore`

Local Dockerfile builds apply only the context-root `.plmignore`, with paths
relative to the **context**, not the directory containing `-f`. The baseline is
`<actual Dockerfile filename>.dockerignore` adjacent to that Dockerfile when
present (including an empty file); otherwise it is the context-root
`.dockerignore`. `.plmignore` appends after the baseline: the last matching
pattern wins, and `!` can reinstate baseline-excluded files. Use Docker-compatible
ancestor exceptions when reinstating files below an excluded directory.

BuildKit evaluates Docker ignore grammar, including comments, blank lines,
wildcards/`**`, `!` exceptions and CRLF. This is not complete Git-ignore syntax;
parent/nested `.plmignore` files are not recursively loaded. Selected ignore
files must be regular non-symlink UTF-8 files, at most 1 MiB each, without NUL.
Invalid inputs fail closed.

Palimpsest sends Buildx the unchanged original context and, only when root
`.plmignore` exists, a temporary exact-byte Dockerfile plus merged adjacent
ignore. It never copies/filters the context or writes back to user sources.
The temporary definition is removed after success or failure. Without root
`.plmignore`, the original `-f` path and Docker ignore selection remain intact.
Effective ignore bytes enter the build identity, including a selected
Dockerfile-specific ignore outside the context.

This scope is the **local Dockerfile frontend**, independent of local/native
cache mode. It does not implement remote Git, Hub worker, or commit-phase
ignores; those require a later cutover. Source-policy admission/transport,
strict-offline topology and native/runtime/publication gates remain mandatory.
See [the workflow contract](../buildkit-block-workflow.md#local-dockerfile-context-ignores).

### Local build → Palimpsest Hub → another client

For a macOS ARM64 client, build a matching conventional cloud-image runtime layer,
not an unsupported local OCI-root VM. Docker/Buildx and `mksquashfs` must be
available; use a Dockerfile context compatible with the selected ARM64 cloud base.
Set `PALIMPSEST_URL` and `PALIMPSEST_TOKEN` to the Hub and your original
project-scoped Keystone token first. Current inventory/download/publish authority
is checked independently; an administrator role alone is not publication authority.

```sh
export PALIMPSEST_URL=https://palimpsest.dmslab.re.kr
# Set PALIMPSEST_TOKEN privately, never as a literal in shell history or this file.
BASE_DIGEST=sha256:1d6bffe64b848468ac97f821d369a4846d983de1800ccf6b5ec8853e85cefc55

# This recorded Hub ARM64 base must still be visible to your original project.
palimpsest image ls --arch aarch64 --disk-format qcow2 --limit 20
palimpsest image pull "$BASE_DIGEST"

# Run in your Dockerfile context. This is a local BuildKit build, not a Hub build.
palimpsest build . -f Dockerfile --frontend dockerfile \
  --platform linux/arm64 --cache-mode local --registry docker \
  -t myapp:dev --runtime-tag myapp-runtime --runtime-base "$BASE_DIGEST"

# Upload prints the complete content digest. Keep it for subsequent consumers.
LAYER_DIGEST=$(palimpsest layer push myapp-runtime \
  --name myapp-runtime --base-image "$BASE_DIGEST")
palimpsest layer ls --name myapp-runtime --limit 20
palimpsest layer pull "$LAYER_DIGEST" --output ./downloads
palimpsest bundle pull "$LAYER_DIGEST" --include-base --output ./myapp-bundle
palimpsest bundle verify ./myapp-bundle
```

The layer-only pull does not fetch a VM's base/parents; `bundle pull --include-base`
is the full-chain transfer. Outputs above are directories; existing destinations
must satisfy the normal verification/non-overwrite rules. Hub object names are not
download identities: use the full digest printed by Push or copied from web Details.
The current released CLI list projection may omit a Hub `blob_digest`; do not
infer it from a name. These commands describe the current contract; the connected
catalog acceptance exercised real reads/downloads, not a new build or upload.

To build only a container archive, omit `--runtime-tag` and `--runtime-base` and
optionally set `--output ./myapp.oci.tar`. That OCI archive is not a legacy
SquashFS layer accepted by `layer push`; native package publication uses its
separate registry profile/project-key protocol. `--push` targets the OCI/native
output registry, while `--runtime-push` publishes the SquashFS runtime layer to
Palimpsest Hub. Docker Hub is not the legacy Palimpsest `/v1` artifact namespace.


## Run: cloud image versus OCI root

```sh
palimpsest run IMAGE_DIGEST --name dev --memory 4096 --vcpus 2
palimpsest run ./bundle --name bundled --layer "$LAYER_DIGEST"
palimpsest run ./app.oci.tar --runtime-kind oci-root --name app
palimpsest run ./app.oci.tar --runtime-kind oci-root --name app -d
palimpsest run alpine:3.20 --name app
palimpsest run app:v1 --registry corp --name app
```

Without an explicit runtime kind, `run IMAGE` checks local cloud CAS/tags, successful BuildKit OCI exports, native OCI package receipts, retained acquired archives, and then every eligible local Docker name before querying configured OCI registries. Layout inference uses manifest/config semantics, not the shared `oci-layout` marker. Conflicting local Docker image IDs are an error. Acquired archives are verified and retained in owner-only storage for later local reuse; local build/package bindings take precedence over this retained reference cache.

An explicit authority or `--registry` restricts reference resolution to that authority/profile, including local named bindings; explicit local paths/digests remain local. Unqualified remote lookup tries the configured default (or `PALIMPSEST_REGISTRY`) first, then other OCI aliases in sorted order. Only a genuine image miss advances to another profile: Docker daemon, authentication, TLS, transport and verification failures stop. Native Palimpsest package profiles are not Docker `/v2` registries; pull those packages explicitly before running their local OCI receipt. See [automatic run acquisition](../registries.md#automatic-run-acquisition).

Cloud-image runs accept a local/Hub image digest or a Palimpsest bundle directory. Extra `--layer` values must continue the verified SquashFS parent chain. Backend `auto` selects KVM on Linux and Lima/VZ on macOS; experimental `libvirt-hvf` and `lima-krunkit` must be selected explicitly. Conventional cloud runs attach the image and layers but do not pivot the guest root.

OCI-root runs accept local OCI archives/layouts, verified build/package/retained
bindings, or OCI registry references acquired through Skopeo. Linux amd64 KVM uses
`qemu:///system` and qualified kernel/config/packer paths. The unreleased native
macOS profile additionally supports explicitly configured, qualified Ubuntu24.04
arm64 roots on Lima VZ with pinned boot assets and its Linux worker. Matching an
image's architecture alone does not qualify execution. Unsupported hosts fail
before source export/acquisition or runtime-state mutation. Networking remains
profile-specific; see [OCI-root guest networking](../oci-network.md). Neither
profile is a general hostile-container sandbox.

Without `-d`, OCI `run` waits for the image workload, streams its output, and returns its exit status; it does not imply `palimpsest rm` or deletion of durable run state. `-d` returns the run name only after authenticated READY and leaves the VM running. Detach is OCI-root-only. `--manifest` pins an otherwise ambiguous root descriptor. `--user USER[:GROUP]` is OCI-root-only and changes the image user selection without adding capabilities, supplementary groups, or ownership changes. A literal `-- COMMAND [ARG...]` replaces only the local OCI image Cmd while preserving Entrypoint; see the [command override contract](../oci-run-command.md). It is literal argv and does not add a shell, environment override, TTY, keepalive, GPU, or device access.

`--root-retention` defaults semantically to `delete`. `retain` preserves only the VM-exclusive writable OCI root volume during removal. It is not a shared data volume. Reuse requires its exact UUID through `--root-volume` and compatible lower graph/size/exclusive attachment.

Acquisition and execution are separate: `--platform linux/amd64|linux/arm64` can
select verified archive bytes without promising native execution. An explicit
experimental `--force-architecture` run uses Lima/QEMU as a VM carrier, not the
native OCI-root stage-1. Supply `--vm-image /absolute/carrier.qcow2` and its exact
`--vm-image-digest sha256:...`; the carrier must be a self-contained raw/QCOW2
disk with neither backing files nor external data files. This mode is
foreground-only and requires a disposable root (`--root-retention delete`),
without independent named-disk attachments. The web producer follows the same
foreground/disposable policy rather than submitting an invalid detached retained
run. Ordinary native/conventional policies are unchanged; no cross-architecture
guest qualification follows from archive acquisition or carrier validation.


Parser limits are 256–1,048,576 MiB for `--memory` (default 4096) and 1–256 for `--vcpus` (default 2).

Conventional cloud-image VMs accept independent named data disks. Create the disk
first, then attach it at VM creation; existing arbitrary disks are never adopted
or reformatted:

```bash
palimpsest volume create data --backend lima-vz --size 10GiB
palimpsest run CLOUD_IMAGE --name app --volume data:/srv/data
palimpsest volumes --format json
palimpsest volume inspect data --backend lima-vz
palimpsest rm app
palimpsest run CLOUD_IMAGE --name replacement --volume data:/srv/data
```

Use `kvm` or `libvirt-hvf` for their conventional runtimes. `:ro` makes a mount
read-only but still exclusive. Stopped VMs reserve their disks; only verified
VM removal releases them. `volume rm NAME --backend BACKEND` explicitly deletes
a detached disk. Native OCI-root and experimental carrier named-volume
attachments are rejected; they do not silently omit the requested storage.
Conventional removal retains the root, firmware/native configuration and ordered
base/layer/attachment topology by default. `rm --volumes` explicitly purges only
safely owned instance root/runtime/topology, not these independent data disks or
shared CAS layers. The Volumes dashboard can delete an owned retained archive
after exact-name and fresh native-detachment checks. External libvirt archives
are reference-only and cannot authorize deletion of arbitrary source files.
Referenced layers remain protected until all attached/retained dependencies are
gone; force cannot bypass that gate. Invalid retained metadata blocks deletion.

## Runtime lifecycle and observation

`ps`, `inspect NAME`, `logs NAME [--follow]`, `start NAME`, `stop NAME`, and `rm NAME` act on managed runs. `ps` includes configured publications in its `PORTS` column as `HOST_IP:HOST_PORT->GUEST_PORT/PROTOCOL`; `inspect` emits the stable runtime record and, for OCI-root runs with a committed domain plan, typed `detail.network`, `detail.ports`, and `detail.guest_ip` fields. These are durable configured endpoints, not proof that a stopped or failed run still has a live listener. `shell` requests an interactive runtime session; `exec NAME -- COMMAND...` is non-interactive and requires a command. `--completion-record PATH` asks OCI exec to persist its bounded completion record. Direct PID 1 access is refused.

Native VZ `exec --timeout SECONDS` limits only the guest command, not host
preflight/load/status time or mutex acquisition. A native pass shares at most
five seconds of added contended run/custody waiting; ordinary provider work does
not spend it. A sanitized `busy` refusal means no fresh observation or access
grant, not VM shutdown. `inspect` reads the last durable record; public CLI `ps`
refreshes it. Native `--completion-record` is refused because there is no KVM
mailbox ACK to record.

Once admitted, native `logs --follow` retains its original pinned console through
pure mutex busy, including another VM's namespace operation, with one bounded
allowance per EOF poll. It does not replay a provider or guest command. Initial
admission/snapshots fail closed, and identity, console, actor/resource HOLD or
uncertain provider failures terminate the stream. Cancellation during mutex
acquisition can wait up to that poll's remaining five-second allowance.

```sh
palimpsest ps
palimpsest inspect app
palimpsest logs app --follow
palimpsest exec --completion-record ./exec.json app -- /usr/bin/id
palimpsest shell dev
palimpsest stop app
palimpsest rm app
```

`rm --volumes` purges safely owned conventional instance root/runtime/topology,
never independent named disks, and is refused for OCI-root runs. OCI-root
deletion/retention follows the owned run policy. KVM OCI-root retains its existing
`start`, `shell` and `commit` refusals. The qualified native VZ profile supports
retained `start` and interactive `shell`; native commit is outside the current
qualification. Use `exec` for additional commands and `stop`/`rm` for lifecycle.
`commit NAME --tag TAG` commits a conventional runtime layer.

OCI diagnostic commands emit JSON: `root-proof NAME`, `exec-status NAME`, `exec-record PATH`, `resource-status`, `root-volumes`, and `root-volume UUID`. `oci init-runtime PATH` securely creates a runtime parent and prints the `PALIMPSEST_STATE_HOME=...` assignment. `oci pull FULLREF --output ARCHIVE` uses installed Skopeo to acquire one anonymously accessible HTTPS `linux/amd64` image, verifies it through the private source CAS, and only then publishes a new local archive. The caller-selected endpoint may be Internet, private-network, or local. `oci materialize SOURCE` snapshots and verifies a local archive/layout, converts layers with the selected SquashFS packer, and emits a receipt (or writes it to a new `--output` file). Their timeout defaults to 300 seconds and must be positive and finite.

Unreleased `oci qualify SOURCE --platform linux/arm64 --backend lima-vz`
inspects an original local archive/layout through the existing source CAS,
selects a reviewed Ubuntu24.04 direct-linux source profile from effective
`os-release`, and prints exact graph/OS/profile identities. `--manifest` pins
the original root/index; `--maximum-bytes` bounds intake and decompression
passes; `--timeout` is one cooperative deadline. Unknown/unsupported or malformed
identity fails with no successful qualification output. No force/risk bypass
exists. This is source-only (`execution_admitted: false`), not an implemented
VZ user-root launch or a replacement for native execution preflight.
See [source qualification semantics and limits](../registry-intake.md#original-source-osprofile-qualification).

`oci materialize SOURCE --platform linux/arm64` is an explicit Linux-hosted
filesystem conversion path, not native execution admission. It preserves the
complete original graph and ordered SquashFS occurrences; output has no runtime
lease. Default `linux/amd64` resolution is unchanged. A successful ARM conversion
cannot enter the legacy KVM plan/root-preparation path or widen Hub publication.
See [filesystem conversion scope](../registry-intake.md#explicit-arm-filesystem-materialization).

## Compose-shaped projects

Compose commands implement a strict `palimpsest.yml` subset, not arbitrary Docker Compose. Global project options precede the subcommand. Discovery checks `palimpsest.yml`, then `palimpsest.yaml`. `--project-directory` controls resource containment; `--env-file` is repeatable and must remain inside that directory. Project naming can come from `-p`, `PALIMPSEST_PROJECT_NAME`, or `COMPOSE_PROJECT_NAME`.

```sh
palimpsest compose -f palimpsest.yml config --services
palimpsest compose -p demo up -d api worker
palimpsest compose ps --format json
palimpsest compose logs -f api
palimpsest compose exec api -- /usr/bin/id
palimpsest compose port api 8080 --protocol tcp
palimpsest compose down --volumes
```

`up` rejects combining `--no-recreate` and `--force-recreate`. Without `-d`, it follows logs after reconciliation; with `-d`, it returns after startup. `exec` requires a command. Service filters are optional for `up`, `ps`, `logs`, and `stop`. Linux KVM project port publication and shared writable volumes are intentionally rejected by the current subset.

## Local store and UI

`store show` reports the selected roots and `store ls` inventories images/layers. `store rm DIGEST` always refuses referenced or leased objects; the accepted `--force` flag does not bypass those safety checks. `store move --to PATH` migrates the selected store; `--keep-source` retains the old bytes. `store set --to PATH` changes the configured location without moving existing data.

```sh
palimpsest ps
palimpsest volumes --format json
palimpsest networks --format json
palimpsest store show --format json
palimpsest store ls --kind layer
palimpsest store move --to /srv/palimpsest/store --keep-source
palimpsest web --port 8080 --no-browser
palimpsest web --allow-control
```

`volumes` and `networks` are read-only list commands. Their default tables show backend, observation status, disk measurements or network addressing, and attached VM names; `--format json` preserves IDs, provenance, attachments and warnings for automation.

`ps`, `volumes`, `networks` and `compose ps` align headings and all displayed
rows with two spaces between columns. Widths follow actual terminal cells,
including wide characters and combining marks; long names are not truncated.
Missing values remain `-`, and zero-byte measurements remain `0`. JSON output
is unaffected by table presentation.

`web --port 0` (the default) chooses an available port. Explicit ports must be 1024–65535. State roots are owner-only. Ordinary Linux users default to `~/.local/state/palimpsest`; root and the dedicated `palimpsest` account retain `/var/lib/palimpsest`. Explicit env/config/XDG overrides remain supported; existing state is not silently migrated.

`web` replaces the former `ui` command. It is a foreground process, not a daemon or an installation-time service: ordinary CLI commands never start it. The packaged dashboard binds only `127.0.0.1` and stops serving when the command exits, including Ctrl-C/SIGINT or SIGTERM. It opens a browser by default; `--no-browser` only prints the URL. If no browser is available, open that printed URL manually. No Hub URL, token or connection is required; remote Hub requests happen only on explicit connection, catalog or transfer actions.

The web artifact list provides architecture and client-platform status filters;
details show the client/backend and refusal reasons. The authenticated local API
supports `GET /api/v1/store/artifacts?arch=aarch64&compatible=true&backend=lima-krunkit`
and `GET /api/v1/store/artifacts/REFERENCE/compatibility` (URL-encode the digest
or local tag). Remote Hub/registry filters select platform **candidates on the
current page** and retain the next-page cursor; an empty filtered page is not
proof that no compatible version exists. Downloads verify the full dependency
chain before offering Run.

The printed URL carries a per-process token. The page removes it from the address bar and keeps it in the tab session, so reloading works while the process runs; a restarted process needs its new URL. Assets and `/api/v1` require the token as a bearer header. Run it as the UID that owns the selected state root. For a remote host, keep the loopback bind and forward it, for example `ssh -L 8080:127.0.0.1:8080 HOST`.

The dashboard is read-only by default: every `POST`/`DELETE` returns `403` before any side effect, and the page hides management controls. Read-only does not mean the VM view is a static snapshot: it live-reconciles each run against its backend, and when observed status differs the reconciliation records that status in the run ledger. `--allow-control` enables image build/run, connected consoles, VM start/stop/remove, cloud-image import, verified local CAS tag assignment, unreferenced CAS artifact removal, and state-root move/set with confirmations. Tags cannot silently be reassigned to another digest. Artifact bytes and build identities remain immutable; build-record and retained-source OCI archives are not edited or deleted through the CAS controls.

VM removal is available from both list and detail views after exact-name
confirmation. A host-only row does not need a run ledger in the selected state
directory or a historical Palimpsest UUID: it needs a fresh, verified native
instance identity. The DELETE request uses the encoded stable row ID plus
`expected_instance_id`, its original `expected_run_id` (including null for an
unmarked instance), exact `confirmation` and `volumes: false`. It revalidates
the backend identity immediately before mutation. Stale, ambiguous, replaced,
protected or physically unsafe identities and OCI-root VMs remain ineligible.
Host-only removal never adopts a run or edits another state's ledger and has
no run-file deletion option. Lima deletes its own instance directory/disks;
independent/additional disks remain outside that removal. Libvirt removal
retains disk files, NVRAM and persistent emulator-TPM state. Transient emulator
TPM domains, or a persistent TPM definition without required retention support,
are refused before stopping. Start/stop and optional owned-run-tree deletion
still require an exact selected managed run. Restart a previously running
`palimpsest web` process after upgrading source; refreshing its page alone does
not reload Python lifecycle handlers.

Build and run forms submit `POST /api/v1/builds` and `POST /api/v1/vms`, returning `202` with operation IDs. `GET /api/v1/operations` exposes completion and bounded output while views refresh without replacing their output panels. At most two operations are active, with 32 retained records and a 128 KiB raw output tail per operation. The build form supports inline Dockerfile editing and local-cache builds with no Hub connection. Run uses the same CLI source resolver and platform checks. Closing the dashboard cancels owned preparation tools, not an already launched VM; use Stop/Remove for VM lifecycle changes.

The VM console opens a session through `POST /api/v1/vms/{identifier}/console`,
using the row's stable ID and original `expected_run_id`. Conventional VM shells
use a PTY with resize and terminal input; OCI-root access is non-TTY guest exec,
not PID 1 attachment, and requires a command. Selected owned runs use their
existing runtime transport. Marked, physically verified external Lima VZ
instances can use their backend-provided transport without importing a ledger;
unmarked, stale or replaced identities, and external libvirt domains without
authenticated credentials, expose an explicit unavailable reason instead.
Interactive access still requires `--allow-control`. Unsupported controls are
disabled from the session capabilities. Completion distinguishes exit codes
from signals, and bounded sessions expire when idle. Disconnect, drawer close
and tab unload close only the owned console transport, never the VM.
OCI foreground disconnect detaches the consumer, not its submitted command.
The original non-daemon owner drains output and performs underlying cleanup;
normal dashboard SIGINT/SIGTERM stops new OCI admission and waits for console
and detached probe owners (at most 32) before process exit. KVM retains original
mailbox ACK accounting under its existing guest deadline/transport grace.
Canonical native Lima-VZ retains the same lifetime owner but has no mailbox ACK;
process completion neither fabricates one nor counts as an unconfirmed ACK.
Probe timers never perform native cleanup on a daemon thread. Unconfirmed
required ACKs remain unresolved; shutdown never takes over a result, retries
the command or stops the VM.

Polling reads time out after fifteen seconds; control requests instead remain busy until the server returns, because VM starts and storage moves can outlive a polling interval. Closing a VM drawer clears its optional volume-deletion choice before inspecting another VM. The keyboard skip link reaches the active view's main content without changing the resource view.

While the tab is visible it refreshes resource views every five seconds without overlapping requests, and the host summary and state-directory usage every thirty seconds (and on manual refresh); it pauses when hidden or on request, and a failed refresh keeps the last successful data marked as stale. Views and their provenance:

| View | Source | Not a claim of |
| --- | --- | --- |
| VMs | live Lima/libvirt host observation plus selected-state reconciliation; explicit detail probes read the Linux guest journal, `/proc` counters and `df` through an authenticated transport | service readiness, adopted ownership or lifecycle authority from discovery alone, or measurements from an unavailable transport |
| Volumes | host primary/additional disks and selected project/OCI root-volume records; backend-reported capacity, backing-file length and allocated host bytes are distinct measurements | verified filesystem contents or capacity when the backend did not report it |
| Layers and images | local CAS metadata/tags and run/project references, successful BuildKit exports and retained sources; separate explicit Hub/Docker Hub catalog requests | automatic remote refresh, layer-file integrity from metadata alone, or OCI-root runnability |
| Networks | backend network definitions and VM attachments plus selected-state committed OCI plans; Lima configuration is marked configured, not live traffic | successful packet delivery, guest address discovery, or an open published listener |
| Builds and storage | local build records, the engine's console file (`console.log` for Palimpsestfile, `buildkit.log` for BuildKit; tails read at most 4 MiB), and state-directory usage | — |

`palimpsest ps` and the web VM, volume and network views discover resources
across the local Lima/libvirt backend, even when they were launched with a
different `PALIMPSEST_STATE_HOME`. Only exact backend/name/run-ID matches in the
selected state can start or stop a VM; CLI lifecycle commands remain scoped to
that state. Discovery never imports another state directory or adopts a resource.
For a freshly reconciled managed native Lima-VZ guest, VM `status` describes
physical liveness while `runtime_status` preserves canonical main-process state.
Thus `ps` can show a running guest after its main exits; runtime `inspect`
still reports that exit. Pinned reconciliation supplies this baseline even when
ambient Lima tools/home differ. Supplementary provider coalescing requires the
same home device/inode and complete identity; it never supplies generic guest
credentials or upgrades pinned stopped state.
Fresh removed native history retains its independently pinned home identity even
when no live alias can be granted, so another Lima home's ordinary VM cannot
revoke its retained console. Busy/stale snapshots do not gain that exemption or
transport authority.
Logs, measurements and opt-in consoles use independently reported access
capabilities and revalidate the selected identity. Artifacts, builds and state
storage remain scoped to the selected state. Docker containers are not VM
inventory. Unknown metadata and failed backend queries remain unavailable
observations or warnings.

Web **Remove** also supports external Lima VZ/QEMU/krunkit and libvirt KVM/HVF VMs. It requires
`--allow-control`, the selected stable inventory ID, the original native removal
identity and exact typed VM name. A Lima identity binds its physical instance
directory/device/inode, safe owned `lima.yaml` bytes, protected native state and
optional run marker; libvirt uses its native domain UUID, native backend and
lifecycle-marker classification. Missing, changed, stale or colliding identities
fail closed. Unmarked VMs
can be removed without obtaining guest credentials; this does not unlock their
consoles or start/stop actions. OCI-root removal still requires its original
managed runtime.

Default Lima removal stops the VM, snapshots the root/backing closure and native
firmware/configuration plus ordered topology, then deletes its native instance.
Explicit purge skips retention only for safely owned instance files. Libvirt
removal preserves external disk/NVRAM/emulator TPM sources as reference-only
topology; unsafe external purge is rejected before stopping. Unreadable/malformed
domain metadata and OCI-root contracts disable external removal. Persistent
emulator TPMs require explicit native KEEP_TPM support; transient emulator TPMs
must already declare `persistent_state='yes'`. Unsupported retention is rejected
before stopping. External removal never deletes another state's run tree or
independent data volumes. Completion reports actual storage policy/archive ID.
Recheck after any backend failure; a failed or ambiguous operation is not
reported as successful.

Removal stays in `host_vm_control.py` through the shared inventory contract;
it does not adopt a foreign run or introduce a second lifecycle authority.
Descriptor-pinned Lima deletion must establish that the selected directory is
gone even if it was renamed during deletion. Libvirt retention preflight reads
inactive domain metadata, including TPM state, before any stop/undefine effect.
These guards preserve the default root/topology retention and separate explicit
owned-instance purge; console access and selected-run start/stop are unchanged.

VM detail probes run only for the open VM, not every resource in list refreshes.
Running logs are the bounded guest system journal; stopped selected libvirt
runs show their identity-pinned retained console, and stopped Lima instances
can show their native serial log when available. Guest probes have a ten-second
transport deadline and a 512 KiB output cap. Measurements distinguish guest
memory and filesystem use from configured RAM and host disk allocation. CPU
usage and network/disk rates require consecutive samples; first samples,
reboots, counter resets and paused sampling do not display invented rates.
Block-device counters are shown per device rather than summed with partitions.

Recorded OCI exports and retained acquired sources remain visible when their archive is missing or unavailable; availability means a regular local file with matching recorded size was observed, not that its digest was reverified. Polling does not rehash whole image archives. Running a selected source does reverify its archive. Recorded Docker output tags are provenance, separate from editable local CAS tags. The image detail links to its originating build when present and separates OCI manifest, OCI archive and SquashFS runtime-block digests.

### Connected Hub and Docker Hub catalogs

Open **Layers and images** and choose **Palimpsest Hub** or **Docker Hub**. No
Afterglow backend, remote Hub deployment change or browser CORS exception is
needed: the browser calls the authenticated loopback app, and that Python client
contacts the selected verified HTTPS service. Remote catalogs are never polled.

- **Palimpsest Hub:** enter the Hub URL and original Keystone token, then Connect.
  The optional project ID asserts the original scope; it cannot select a different
  project. `/v1/me` supplies the actual identity, project and independent current
  capabilities. Search cloud images/SquashFS by name or digest prefix and filter
  architecture, disk format or parent. Details resolve the full base/parent chain,
  preserving errors for inaccessible or inconsistent dependencies.
- **Docker Hub:** public repository search needs no Hub configuration; select a
  repository for bounded tag pages, or enter `docker.io/library/alpine:latest`
  directly. Inspect an index, then select a platform to read its manifest/config
  and ordered layer digests, media types and compressed sizes. Tag filtering is
  per server page, not a claim that every matching tag was searched. Changing
  search fields resets pagination; Back restores the repository search.
- **Authentication:** the password field clears after Connect. The override stays
  only in JavaScript memory, disappears on reload/disconnect, and travels only in
  scoped local headers. A transfer captures its own child-process environment;
  it never changes `os.environ` or writes credentials into argv/receipts. Docker
  uses only its selected config/helper; native package profiles use their own
  scoped helper keys. Remote authorization/network errors do not log out the
  local dashboard bearer session.
- **Inspection versus acquisition:** metadata needs neither Docker nor Skopeo.
  Docker manifest/config bytes are SHA-256/size checked with bounded HTTPS reads;
  one digest-bound config request may take a credential-free hop to an allowlisted
  Docker CDN. Layer descriptors are not downloaded/verified layer files. Download
  needs `--allow-control` plus the existing transport prerequisites (Skopeo for
  registry acquisition). A selected Docker platform downloads its immutable child
  manifest, while further inspection retains the pinned parent index.
- **VM setup:** a completed verified transfer can fill the VM form with its base
  and ordered layers. It closes the detail modal and preserves keyboard focus;
  a separate explicit Run is still required. OCI acquisition remains
  `linux/amd64`-only. OCI-root runs on Linux amd64 KVM or an explicitly configured,
  qualified Ubuntu24.04 arm64 Lima VZ profile on macOS; inspecting another
  platform's metadata does not qualify it for execution. Read-only mode permits
  connection, search and inspection, but rejects every transfer mutation.

