# Palimpsest CLI usage guide

This guide explains what the commands do and where similarly named commands cross different trust and storage boundaries. The generated [syntax reference](reference.md) lists every parser-visible command, positional, option, choice, and default.

## Invocation and configuration

```sh
palimpsest [--url HUB_URL] COMMAND ...
palimpsest --version
palimpsest completion bash
```

`--url` is a global Hub URL override. Hub commands resolve the URL in this order: `--url`, `PALIMPSEST_URL`, then `config.toml` (`url` or `[hub].url`). They require `PALIMPSEST_TOKEN`; do not put tokens in examples or configuration committed to source control. Registry wrapper commands reuse Docker's existing config and credential directory: `DOCKER_CONFIG` when set, otherwise `$HOME/.docker`.

Options are parsed in the command position shown in the reference. `palimpsest docker ...` is special: everything after `docker` is passed to the Docker CLI. `palimpsest __complete` is an internal completion protocol, not a public command.

## Three image namespaces

Do not treat these similarly named surfaces as interchangeable:

| Surface | Examples | Backend and credentials |
| --- | --- | --- |
| Palimpsest Hub artifacts | `image ls`, `image pull`, `image push`, `layer *`, `bundle *` | Native Palimpsest Hub `/v1`; `PALIMPSEST_TOKEN` |
| Docker wrapper | `pull`, `push`, `tag`, `images`, `history`, `rmi`, `save`, `load`, and `image inspect/history/rm/save/load` | Installed Docker CLI and external OCI registries; Docker credentials |
| Local OCI root | `oci pull`, `oci materialize`, `run ./image.oci.tar --runtime-kind oci-root` | Anonymous TLS registry acquisition into a verified local archive; private source CAS and Linux KVM runtime |

Palimpsest Hub is not an OCI Distribution `/v2` registry. A fully qualified anonymously accessible reference such as `quay.io/example/nginx:stable` can be acquired with `palimpsest oci pull ... --output ./nginx.oci.tar`; `run` still accepts only the resulting verified local source. See [anonymous TLS OCI registry intake](../registry-intake.md) for its authentication, TLS, endpoint, platform, path, size, and timeout limits.

## Hub cloud images, layers, and bundles

`image ls` queries cloud-image metadata and accepts server-side filters plus `--limit` (default 50, valid 1–200). `image pull DIGEST` verifies the Hub object is a complete cloud image, stores it locally, and optionally copies it beneath `--output`. `image verify PATH --digest DIGEST` checks local bytes. `image import` adds an existing qcow2/raw image to the local store; it does not upload it. `image push` uploads a local cloud image and requires `--name`; `--publish` controls Hub visibility.

```sh
palimpsest --url https://hub.example image ls --arch x86_64 --disk-format qcow2
palimpsest image pull "$IMAGE_DIGEST" --output ./downloads
palimpsest image import ./ubuntu.qcow2 --disk-format qcow2 --arch x86_64 --os-variant ubuntu24.04
palimpsest image push ./ubuntu.qcow2 --name ubuntu-dev --publish
```

`layer pack DIRECTORY --tag TAG` produces a local SquashFS layer with the fixed zstd recipe. `layer push VALUE` accepts a local tag/digest and optionally supplies Hub chain metadata; `--parent`, `--base-image`, and `--ubuntu-base` describe that chain. `layer ls` has the same 1–200 `--limit` restriction. `bundle pull` exports a Hub chain as an OCI image-layout directory and can include its base image. `bundle verify` verifies an extracted bundle directory.

```sh
palimpsest layer pack ./rootfs-overlay --tag app-v1
palimpsest layer push app-v1 --name app-v1 --base-image "$IMAGE_DIGEST"
palimpsest bundle pull "$LAYER_DIGEST" --output ./bundle --include-base
palimpsest bundle verify ./bundle
```

`bundle unpack ARCHIVE --output DIRECTORY` verifies a downloaded runtime-bundle tar locally and publishes the extracted directory atomically. The destination must be absent or empty; corrupt digests, unsafe archive members and partial extraction are rejected. It does not contact Hub or execute a guest. A runnable bundle must declare its cloud-image base architecture; ordered SquashFS layers are selected automatically and incompatible architecture metadata is refused.

```sh
palimpsest bundle unpack ./palimpsest-runtime.oci.tar --output ./palimpsest-bundle
palimpsest bundle verify ./palimpsest-bundle
```

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
- `--local-image` uses `ALIAS=/absolute/layout@sha256:DIGEST`; the layout and pinned manifest graph are verified. `--runtime-block-size` defaults semantically to 131072 bytes and must be a power of two from 4096 through 1048576.
- `--output` is an OCI archive path, not arbitrary BuildKit exporter syntax; omission creates an invocation-unique archive beneath local build state.
- Dockerfile builds reject legacy `--base`/`--layer`; use `--runtime-base` and `--local-image` instead.
- `--progress` defaults to `plain`; build networking defaults to `none`.

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

Cloud-image runs accept a local/Hub image digest or a Palimpsest bundle directory. Extra `--layer` values must continue the verified SquashFS parent chain. Backend `auto` selects KVM on Linux and Lima/VZ on macOS; experimental `libvirt-hvf` must be selected explicitly. Conventional cloud runs attach the image and layers but do not pivot the guest root.

OCI-root runs accept local OCI archives/layouts, verified build/package/retained bindings, or OCI registry references acquired through Skopeo. The public runtime is limited to Linux x86_64/amd64 KVM with `qemu:///system` and qualified absolute kernel/config/packer paths with digest proof. Unsupported hosts fail capability checks before Docker export, registry acquisition or runtime-state mutation. `--network` selects `nat` (the default when omitted), `host-only`, or `none`, and OCI-root-only `--publish` adds explicit loopback-by-default forwarding; see [OCI-root guest networking](../oci-network.md). The runtime remains capabilityless and is not a general hostile-container sandbox.

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

```sh
palimpsest ps
palimpsest inspect app
palimpsest logs app --follow
palimpsest exec --completion-record ./exec.json app -- /usr/bin/id
palimpsest shell dev
palimpsest stop app
palimpsest rm app
```

`rm --volumes` explicitly purges safely owned conventional instance root/runtime/topology, never independent named data disks. It is rejected for OCI-root runs; OCI root deletion/retention follows the owned run policy. OCI-root also rejects `start`, `shell`, and `commit`; use `exec` for additional processes and `stop`/`rm` for lifecycle control. `commit NAME --tag TAG` commits a conventional runtime layer.

OCI diagnostic commands emit JSON: `root-proof NAME`, `exec-status NAME`, `exec-record PATH`, `resource-status`, `root-volumes`, and `root-volume UUID`. `oci init-runtime PATH` securely creates a runtime parent and prints the `PALIMPSEST_STATE_HOME=...` assignment. `oci pull FULLREF --output ARCHIVE` uses installed Skopeo to acquire one anonymously accessible HTTPS `linux/amd64` image, verifies it through the private source CAS, and only then publishes a new local archive. The caller-selected endpoint may be Internet, private-network, or local. `oci materialize SOURCE` snapshots and verifies a local archive/layout, converts layers with the selected SquashFS packer, and emits a receipt (or writes it to a new `--output` file). Their timeout defaults to 300 seconds and must be positive and finite.

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

The printed URL carries a per-process token. The page removes it from the address bar and keeps it in the tab session, so reloading works while the process runs; a restarted process needs its new URL. Assets and `/api/v1` require the token as a bearer header. Run it as the UID that owns the selected state root. For a remote host, keep the loopback bind and forward it, for example `ssh -L 8080:127.0.0.1:8080 HOST`.

The dashboard is read-only by default: every `POST`/`DELETE` returns `403` before any side effect, and the page hides management controls. Read-only does not mean the VM view is a static snapshot: it live-reconciles each run against its backend, and when observed status differs the reconciliation records that status in the run ledger. `--allow-control` enables image build/run, connected consoles, VM start/stop/remove, cloud-image import, verified local CAS tag assignment, unreferenced CAS artifact removal, and state-root move/set with confirmations. Tags cannot silently be reassigned to another digest. Artifact bytes and build identities remain immutable; build-record and retained-source OCI archives are not edited or deleted through the CAS controls.

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
OCI foreground disconnect detaches the consumer, not its submitted command:
the original owner drains output and acknowledges completion under the existing
guest deadline and transport grace. Normal dashboard SIGINT/SIGTERM stops new
OCI admission and waits for both console and detached probe owners (at most 32)
before process exit. Unconfirmed ACKs are reported as unresolved; shutdown
never takes over a result, retries the command or stops the VM.

Polling reads time out after fifteen seconds; control requests instead remain busy until the server returns, because VM starts and storage moves can outlive a polling interval. Closing a VM drawer clears its optional volume-deletion choice before inspecting another VM. The keyboard skip link reaches the active view's main content without changing the resource view.

While the tab is visible it refreshes resource views every five seconds without overlapping requests, and the host summary and state-directory usage every thirty seconds (and on manual refresh); it pauses when hidden or on request, and a failed refresh keeps the last successful data marked as stale. Views and their provenance:

| View | Source | Not a claim of |
| --- | --- | --- |
| VMs | live Lima/libvirt host observation plus selected-state reconciliation; explicit detail probes read the Linux guest journal, `/proc` counters and `df` through an authenticated transport | automatic deletion, adopted ownership of another state, or measurements from an unavailable transport |
| Volumes | host primary/additional disks and selected project/OCI root-volume records; backend-reported capacity, backing-file length and allocated host bytes are distinct measurements | verified filesystem contents or capacity when the backend did not report it |
| Layers and images | local CAS metadata/tags and run/project references, successful BuildKit exports and retained sources; separate explicit Hub/Docker Hub catalog requests | automatic remote refresh, layer-file integrity from metadata alone, or OCI-root runnability |
| Networks | backend network definitions and VM attachments plus selected-state committed OCI plans; Lima configuration is marked configured, not live traffic | successful packet delivery, guest address discovery, or an open published listener |
| Builds and storage | local build records, the engine's console file (`console.log` for Palimpsestfile, `buildkit.log` for BuildKit; tails read at most 4 MiB), and state-directory usage | — |

`palimpsest ps` and the web VM, volume and network views discover resources
across the local Lima/libvirt backend, even when they were launched with a
different `PALIMPSEST_STATE_HOME`. Only exact backend/name/run-ID matches in the
selected state can start or stop a VM; CLI lifecycle commands remain scoped to
that state. Discovery never imports another state directory or adopts a resource.
Logs, measurements and opt-in consoles use independently reported access
capabilities and revalidate the selected identity. Artifacts, builds and state
storage remain scoped to the selected state. Docker containers are not VM
inventory. Unknown metadata and failed backend queries remain unavailable
observations or warnings.

Web **Remove** also supports external Lima VZ/QEMU/krunkit and libvirt KVM/HVF VMs. It requires
`--allow-control`, the selected stable inventory ID, the original native removal
identity and exact typed VM name. A Lima identity binds its physical instance
directory/device/inode and optional run marker; libvirt uses its native domain
UUID. Missing, changed, stale or colliding identities fail closed. Unmarked VMs
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
  `linux/amd64`-only and OCI-root execution remains Linux amd64 KVM-only, even
  though metadata inspection exposes other platforms. Read-only mode permits
  connection, search and inspection, but rejects every transfer mutation.

