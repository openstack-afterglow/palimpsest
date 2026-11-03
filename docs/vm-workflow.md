# VM workflow

How to build and run a local Palimpsest VM from an Ubuntu cloud image: import a
base image, build a SquashFS layer in a disposable guest, run a VM with that
layer attached, verify it, and clean it up.

For a first end-to-end run, use the scripted walkthrough in
[Hello VM](../examples/hello-vm/README.md). This guide is the manual, canonical
command reference for the same flow.

## Runtime model

```text
guest VM
  /opt/layers/merged        OverlayFS view (read-only layers + writable upper)
  /mnt/palimpsest/lowerN    one read-only SquashFS layer per mount
  /                         writable qcow2 overlay of the immutable base image
```

| Artifact | Mutability | Where it lives |
|---|---|---|
| Base cloud image | Immutable, content-addressed | `store/blobs/sha256/<hex>` |
| SquashFS layer | Immutable, content-addressed | `store/blobs/sha256/<hex>` |
| Run overlay, seed, keys | Per-run, disposable | `runs/<name>/` |
| Build record and console log | Per-build | `builds/<build-id>/` |

A layer never grows the guest device count per Dockerfile instruction: one
built layer is one SquashFS artifact, attached once.

The external-volume alternative keeps immutable blobs, RO layer mounts and writable
runtime mountpoints beneath `/srv/palimpsest`; private upper/work live on each
consumer's boot disk (or explicitly owned writable scratch), never in that store.

## Platforms and backends

| Host | Backend | Image arch | Layer attachment |
|---|---|---|---|
| macOS arm64 | `lima-vz` (default) | `aarch64` | SquashFS copied into the guest, loop-mounted read-only |
| macOS arm64 | `libvirt-hvf` (experimental) | `aarch64` | QEMU/HVF via `qemu:///session`, SLIRP `hostfwd` |
| macOS arm64 | `lima-krunkit` (experimental) | `aarch64` | SquashFS copied into the guest; virtio-gpu Venus Vulkan to host Metal |
| Linux x86_64 | `kvm` | `x86_64` | Read-only `virtio-blk` disks `vdb`..`vdz` |
| Linux aarch64 | `kvm` | `aarch64` | Read-only `virtio-blk` disks `vdb`..`vdz` |

`--backend auto` (the default) picks `kvm` on Linux and `lima-vz` on macOS
arm64, and requires the image architecture to match the host. Backend
prerequisites are listed in [install.md](install.md).

`libvirt-hvf` and Linux KVM need the optional `[kvm]` extra
(`libvirt-python`). The default macOS Lima/VZ path does not.

For a macOS HVF guest, pass `--backend libvirt-hvf` explicitly. Its `virt`
machine uses GICv3 and a virtio-mmio NIC with QEMU user-mode networking, so
libvirt's PCI root-port allocation does not collide with the forwarded SSH
device. UEFI variables live under the owned run tree and are removed with
`palimpsest rm NAME --volumes`; the network does not need a libvirt `default`
network. This is a conventional cloud-image path, not Linux OCI-root.

For macOS GPU acceleration on Linux guests, pass `--backend lima-krunkit` explicitly. It launches via `krunkit` and `libkrun`, exposing a `virtio-gpu` device whose Vulkan calls are remoted to host Metal via Mesa Venus. Default selection (`auto`) remains `lima-vz`; direct Hypervisor.framework integration remains an optional alternative if libvirt is bypassed. This does not expose CUDA or the Apple Neural Engine to Linux guests.

The first boot emits readiness after project initialization and records a
bootstrap marker. Guest helpers write to the serial port captured by libvirt:
`/dev/ttyS0` for x86_64, `/dev/ttyAMA0` for aarch64. `/dev/console` may
resolve to VGA instead of that serial port on x86 cloud images.

On later boots with cloud-init enabled, `palimpsest-ready.service` is pulled
in by `cloud-init.target` after layer activation and `cloud-final.service`.
If the guest was bootstrapped and then cloud-init was disabled with
`/etc/cloud/cloud-init.disabled`, a separate `multi-user.target` unit emits
readiness after layer activation. The fallback does not cover other methods
of disabling cloud-init. Attaching the cloud-final-dependent unit directly
to `multi-user.target` creates an ordering cycle on Ubuntu.

## External-volume runtime provider workflow

`palimpsest layer cloud-init --config runtime.json` exports guest boot policy;
it does not create a Nova VM, allocate storage, attach a volume, download Hub
blobs, or configure user accounts. The local `palimpsest run` workflow in the
numbered sections below remains separate. See the
[manifest contract and example](cli/usage.md#external-copy-on-write-runtime-user-data)
for the complete input schema.

Acquire and convert reusable OCI inputs with the explicit
[`oci cache --root PATH` persistent cache](registry-intake.md#permanent-shared-layer-cache)
before preparing consumer storage. It keeps source blobs, derived recipes and
consumer records after the last release. A writable CephFS acquisition cache or
single-writer RBD filesystem is not the finalized read-only multiattach volume;
do not write a live consumer ext4 store to add another image. This command does
not add Manila guest mounting or automatic Nova attachment.

For a provider-managed cloud VM:

1. Pack and qualify the native runtime and its dependencies as SquashFS layers
   for the chosen base image and `x86_64` or `aarch64` guest. A matching CPU
   architecture alone does not establish ABI, GPU, CUDA, Vulkan, driver, or
   kernel compatibility. Include explicit runtime checks in the manifest.
2. With external provider/storage tooling, create and populate an ext4 block
   volume. Place each blob at `blobs/sha256/<hex>` relative to that filesystem's
   root and record its actual canonical filesystem UUID. Blob files must be
   regular, not symlinks, and match their SHA-256 names. Preserve root ownership
   and protect the volume from untrusted writers. This version selects
   externally managed **Cinder block storage**, not a Manila share; no Cinder,
   Nova, or Manila provisioning backend is implemented by the exporter.
   For a Cinder multiattach store, finalize a clean immutable filesystem and
   precreate root-owned, non-group/world-writable `blobs/sha256`,
   `layers/<hex>` and `runtimes/<name>` directories. Layer/stack mountpoints
   must be empty. Set manifest `storage_read_only: true` and make **all**
   provider attachments block-level read-only before either guest boots.
   No writable guest or external writer may share that filesystem. This uses
   `ro,noload,nosuid,nodev`, not clustered writable ext4; `noload` must not
   substitute for clean filesystem preparation. Each guest has private boot-disk
   upper/work, overlay mount state, native configuration, homes and caches.
3. Write the manifest with that UUID, base-to-top layer digests, named stacks,
   explicit exported subtrees beneath `/opt` or `/usr/local`, PATH directories,
   library directories, and checks. Any activation/check argv is trusted
   operator-authored root boot policy, not an instruction taken from Hub
   metadata. Keep secrets and SSH account policy outside this manifest.
4. Generate a new user-data fragment:

   ```sh
   palimpsest layer cloud-init --config ./runtime.json --output ./runtime-cloud-init.yaml
   ```

   Compose its `write_files` and boot commands with your independently managed
   cloud-init account/SSH configuration, using proper cloud-init YAML merging
   or MIME composition rather than concatenating YAML documents or replacing
   the generated lists. The exporter itself includes no users or credentials.
5. Use external Nova/provider tooling to create the VM with the composed
   user-data and attach the prepared volume as a data disk available on first
   boot. Select a cloud image with cloud-init, systemd, ext4, SquashFS,
   OverlayFS, loop-device support, and the required native runtime prerequisites.
   Do not expect this command to install missing guest packages or create the
   external filesystem.
6. Watch the provider console for activation/check failures. After successful
   activation, use an ordinary first SSH login and run the configured tools
   directly. No sudo/chroot wrapper, manual environment-entry script, or
   home-directory runtime root is needed.

At boot, `/srv/palimpsest` is mounted by filesystem UUID without formatting.
Verified blobs become read-only `nosuid,nodev` SquashFS mounts at
`/srv/palimpsest/layers/<hex>`; writable OverlayFS stacks appear at
`/srv/palimpsest/runtimes/<name>` with the top layer first in the lowerdir list
and consumer-private same-filesystem upper/work under `/var/lib/palimpsest/consumer`.
Only explicitly declared root-owned prefixes bind-export that writable merged
view into the guest's native filesystem. PATH registration
covers login shells and interactive Bash; library directories are registered
with `ldconfig`, so native executables do not depend on an SSH wrapper.

The runtime systemd service requires the external mount and runs the trusted
activation commands followed by checks. SSH daemon activation/login requires
successful runtime activation; the socket may listen earlier without enabling
login. Missing storage, bad digests, architecture
mismatch, failed checks, or unknown/conflicting mounts fail closed rather than
silently offering a partially configured login. Keep provider-console recovery
available. On restart, activation verifies existing mount identities and does
not adopt unrelated exports, repair storage, or format the volume.

## 1. Import a base image

```bash
BASE=$(palimpsest image import /tmp/ubuntu-24.04-server-cloudimg-arm64.img \
  --disk-format qcow2 \
  --arch aarch64 \
  --os-variant ubuntu24.04)
echo "$BASE"
```

Use `--arch x86_64` with an amd64 image. `image import` hashes the file, copies
it into the content store, and records `disk_format`, `arch`, and `os_variant`.
It prints the digest only; it does not boot or otherwise validate the image, so
a non-bootable or wrong-architecture file fails later at `run`.

Ubuntu release images and their `SHA256SUMS` list are published at
`https://cloud-images.ubuntu.com/releases/noble/release/`.

## 2. Build a layer

A `Palimpsestfile` pins the exact base digest, so generate it instead of
hand-copying digests:

```bash
cat > /tmp/Palimpsestfile <<EOF
FROM $BASE
WORKDIR /opt/hello-vm
RUN echo "Hello from a real Palimpsest VM" > message.txt
RUN uname -m > arch.txt
EOF

LAYER=$(palimpsest build \
  --frontend palimpsestfile \
  --base "$BASE" \
  --tag hello-vm-layer \
  --file /tmp/Palimpsestfile \
  --network none)
echo "$LAYER"
```

Recipe grammar:

- `FROM sha256:<64hex>` must be the first instruction and must equal `--base`.
- Optional `LAYER sha256:<64hex>` lines declare ordered parent layers, at most
  25, before any `ENV`, `WORKDIR`, or `RUN`. Any `--layer` values passed on the
  command line must match those lines exactly.
- `ENV`, `WORKDIR` (absolute, no `.`/`..`), and at least one `RUN` follow.
- `COPY`, `ADD`, `ARG`, `USER`, `CMD`, `ENTRYPOINT`, `EXPOSE`, `VOLUME`,
  `LABEL`, `SHELL`, heredocs, `RUN --mount`, multi-stage `FROM`, and
  `FROM scratch`/bare `ubuntu:*` are rejected.
- Tags match `^[a-z0-9][a-z0-9.+-]{0,63}$`.

The build runs in a disposable guest: `--network none` (the default) gives the
builder no interface, and `--network default` attaches the named network for
recipes that install packages. Output is a verified SquashFS blob in the content
store plus a tag; the command prints the layer digest.

The layer descriptor and build record retain `arch` from the verified base
(`aarch64` here), including Lima/macOS builds. Commit output uses the recorded
guest base too. Known foreign-architecture parents are refused before a build
or VM launch. BuildKit runtime packs retain their chosen `--platform`; the
host architecture is never substituted for that target. `palimpsest store ls
--compatible` lists complete local chains matching this client, and
`palimpsest store check hello-vm-layer` explains incompatible or unknown
dependencies without booting a VM. See the [full metadata-check contract](cli/usage.md#target-architecture-and-client-compatibility).

Layer digests are content-addressed but not reproducible across runs:
`mksquashfs` records image creation time. Verify a layer by its returned digest
and extracted contents, not by comparing digests between builds.

Dockerfile/BuildKit builds are a separate, experimental frontend
(`palimpsest build . --frontend dockerfile ...`) with its own cache and runtime
block contract; see
[BuildKit cache and block runtime workflow](buildkit-block-workflow.md).

## 3. Run a VM

```bash
palimpsest run "$BASE" \
  --name hello-vm \
  --layer "$LAYER" \
  --memory 2048 \
  --vcpus 2 \
  --network default \
  --backend auto
```

- `--name` is required and matches `^[a-z0-9][a-z0-9-]{0,62}$`.
- Defaults are `--memory 4096`, `--vcpus 2`, `--network default`,
  `--backend auto`.
- Repeat `--layer` in root-to-leaf order: the first layer must belong to the
  base image, each later layer must continue the previous one. Maximum 25
  layers, no duplicates.
- Lima rejects `--network none`. On Linux, `--network none` attaches no
  interface, so the run has no SSH endpoint and `shell`/`exec` cannot work.
- The printed value is `limactl shell <name>` on Lima, the guest IP on Linux
  KVM, and `None` when no guest IP exists (for example `libvirt-hvf`, whose
  endpoint is a localhost port recorded in the ledger).

## 4. Use the guest

```bash
palimpsest exec hello-vm -- uname -m
palimpsest exec hello-vm -- cat /opt/layers/merged/opt/hello-vm/message.txt
palimpsest shell hello-vm
```

`exec` passes its argv after `--` to the guest without host shell parsing. Built
layer content is visible under `/opt/layers/merged`.

## 5. Observe state

```bash
palimpsest ps
palimpsest inspect hello-vm
palimpsest logs hello-vm
```

`inspect` prints a versioned, allowlisted JSON snapshot of durable state. It
includes owner and runtime identity, lifecycle status/revision, public base and
layer metadata, resource sizing, ports, volumes, and the guest SSH endpoint.
Host paths, backend-internal identifiers, environment values, cleanup metadata,
and raw errors are excluded. `logs` reads the owner-only retained
`runs/<name>/console.log` for every cloud backend; `--follow` tails that same
pinned file while the run is active. Log bytes are preserved exactly until the
CLI or UI rendering boundary, and reading logs never invokes libvirt, Lima, or
an in-guest journal.

On every host, `ps`, `inspect`, and `logs` are state-only operations that do not
require a live hypervisor capability. An operator may still use Lima's own
tools separately when an explicitly live, in-guest journal is required.

## 6. Stop and clean up

```bash
palimpsest stop hello-vm
palimpsest rm hello-vm --volumes
```

`stop` requests an orderly shutdown and force-destroys the domain if it does not
exit in time; it is idempotent. `rm` retains root/backing bytes, firmware/native
configuration and ordered base/layer/attachment topology before removing the VM.
Without `--volumes` the run directory/name remains reserved; with `--volumes`
only safely owned instance root/runtime/topology is purged. Independent named
data disks survive both forms and can be attached to a replacement conventional
VM with `--volume NAME:/guest/path[:ro]`. Stopped VMs keep exclusive reservations.

Neither form deletes shared content-store artifacts. A retained instance still
pins its layer dependencies. Deleting its owned archive in the Volumes dashboard
clears the archive pointer only on its exact matching removed run; it does not
purge the source run directory or free the name. Run `palimpsest rm NAME --volumes`
afterwards to purge the owned source root/topology and release its remaining
base/layer references before removing shared artifacts. A same-name replacement
run is never changed by deletion of an older archive. Archive deletion rechecks
native identity and canonical live/persistent disk references, including path
aliases. Invalid retention metadata or uncertain native inventory blocks deletion,
including force. Independent named data disks survive both cleanup stages.
Base images and layers stay reusable:

```bash
palimpsest store ls --kind layer
palimpsest store rm sha256:<digest>
```

## Local-driven OpenStack layers on Manila CephFS

`palimpsest cloud` can drive OpenStack directly from this machine through openstacksdk and a named
`clouds.yaml` cloud, with no Hub in the path. Point the SDK at the file with `OS_CLIENT_CONFIG_FILE`
when it is not in a default location.

```bash
# 1. Share + build worker (Ubuntu 24.04, boot-from-volume, floating IP, SSH host keys pinned from the console)
palimpsest cloud env up dev --cloud openstack --network Default --external-network private_provider \
  --image <ubuntu-24.04-image> --worker-flavor cpu.4c_16g --security-group <existing-ssh-group> --dns 8.8.8.8
# 2. Dockerfile -> BuildKit on the worker -> ordered SquashFS layers -> write-once CephFS blobs
palimpsest cloud dockerfile-build dev ./context -t my-image
# 3. Fresh consumer VM: read-only cephx rule, RO loop-mounted layers, overlay at /opt/layers/merged
palimpsest cloud layer-run dev my-image --flavor cpu.2c_4g -- python -c 'import numpy'
palimpsest cloud env down dev
```

- The share holds `blobs/sha256/<hex>` (mode 0444, atomically published without replacement)
  and immutable `images/<tag>.json` bindings. Reusing a tag with different source/layers/runtime
  fails; identical binding reuse preserves its original timestamp. Layer metadata is also kept
  in the local environment state; this flow does not register layers in the Hub database.
- The worker mounts the share with a read-write cephx identity; each consumer gets its own
  read-only access rule. The consumer probes a write on the share and refuses to continue if the
  storage allows it. Every lower is loop-mounted read-only; writes go to a private upper directory.
- The command runs as root via `chroot` into the merged root with the image `Env` and `WorkingDir`.
  Nonroot image `User` is rejected rather than ignored; the command is explicit, not automatic
  Entrypoint/Cmd execution. This is not isolation beyond chroot.
- This conventional chroot command runner requires `/usr/bin/env` and `/bin/sh`
  inside the selected image; distroless images without them are unsupported.
  Artifact acquisition may retain nonroot image metadata, but execution remains
  root-only. Native commands additionally require an existing security-group
  policy permitting the consumer-to-worker TCP22 tunnel; admission never adds
  ingress rules on the caller's behalf.
- Environment state, the SSH key, pinned host keys and cephx keys live under
  `<state>/cloud-envs/<name>/` with mode 0600. `env down` deletes only recorded resources; a reused
  `--security-group` is never modified or deleted.
- Packages are installed over SSH after boot. `--dns` overrides guest resolvers when the subnet's
  DHCP DNS cannot resolve public mirrors.
- The worker serializes builds, retains each job's logs/OCI archive, and reports its
  `evidence_directory`. Uncertain TERM shutdown leaves a HOLD and preserves the socket/job;
  do not clear that evidence or replay the operation without ownership reconciliation.
- Use this root/chroot execution path only for trusted Dockerfiles and commands on an isolated
  worker. It is not a multi-tenant build sandbox or an OCI-root boot implementation.
- `.dockerignore` currently accepts literal root-relative paths, comments and blank lines only;
  unsupported glob/negation syntax fails before context transfer instead of silently uploading
  excluded files. Literal-IP Ceph monitor exports are supported; hostname exports fail closed.
- Environment operations serialize and bind to the recorded project/user and immutable settings.
  Unknown create outcomes retain their intent and refuse replay. Cleanup requires exact ownership
  and provider absence; command or cleanup failures return a nonzero CLI status and retain reports.
  Legacy/unbound state needs explicit review, not automatic adoption. `dockerfile-build` refuses
  worker-package drift. `env up` installs into a fresh worker only and refuses replacement;
  existing worker code requires an explicitly approved preserving source upgrade.

### Direct registry intake

`palimpsest cloud image-pull <env> <registry/repository@sha256:digest> -t <name>`
executes acquisition on the Nova worker; the client does not download or upload
the image payload and no Dockerfile/BuildKit rebuild occurs. Use a platform-specific
linux/amd64 manifest digest when qualifying the CUDA path.

Within the existing worker/cache lock, each verified conversion commits its cache
entry, publishes or verifies the unique CephFS digest blob, and fsyncs a per-layer
receipt and ordered job record **before the next conversion**. Only the complete
sequence can publish the immutable image name. Original manifest/config,
compressed descriptors, DiffIDs, order and conversion provenance remain recorded.
An existing valid digest is verified without another payload copy to CephFS.
Interrupted or uncertain jobs retain their journals and require reconciliation;
the client does not retry them automatically.

This command is the acquisition/publication component, not a Nova native-root boot
command. For the owner's selected actual-root-plus-GPU acceptance, ordinary
`cloud layer-run` remains insufficient. See the [native GPU gates](oci-gpu-support.md#registrycephfs-acceptance-selected-on-2026-11-01).

### Experimental native-root proof commands

`cloud layer-run` above is not native-root acceptance. The separate
`palimpsest cloud native` commands require an explicit mode-0600 approval JSON
and prepared, version-pinned kernel/driver inputs. `native prepare` is the only
source replacement path: it verifies the recorded predecessor, retains bytes and
metadata as `.prior`, verifies the new source and import origin, and starts the
owned loopback mTLS unit only when that action is explicitly selected.

```bash
palimpsest cloud native prepare registry-native-01 cuda-12.2.2-cudnn8 \
  --grant "$NATIVE_GRANT" --config "$NATIVE_PREPARE"
palimpsest cloud native run registry-native-01 cuda-12.2.2-cudnn8 \
  --grant "$NATIVE_GRANT" --phase cpu --timeout 1800 -- /bin/sh -c 'test "$CUDA_VERSION" = 12.2.2'
# Only a verified and cleaned native CPU result can admit this GPU operation:
palimpsest cloud native run registry-native-01 cuda-12.2.2-cudnn8 \
  --grant "$NATIVE_GRANT" --phase gpu --timeout 1800 -- /run/palimpsest-probe/cuda-probe
palimpsest cloud native status registry-native-01
```

- Grant `source_digest` binds the original OCI image digest; `source_package_sha256`
  and `probe_source_sha256` separately bind the reviewed implementation/probe.
  Exact project/environment, flavor IDs, per-kind cumulative creation counts,
  boot size, admission expiry, allocation window and bounded cleanup tail are
  mandatory. Failed/uncertain attempts are not refunded or automatically retried.
- A new execution after a cleaned failure requires a separate explicit recovery
  approval: `recovery.preparation_sha256` is the canonical predecessor-preparation
  digest; `recovery.runs` maps every cleaned failed/over-budget run ID to its full
  canonical digest; `recovery.history_sha256` hashes canonical
  `{"history": [...]}` containing ordered environment-history rows for exactly
  those roles. All use `sha256:`-prefixed digests. Changed/missing history,
  unrevoked delivery, unknown/non404 provider responses or native leftovers
  reject admission. No original run/grant/counter is reset or refunded.
- Recovery source installation requires the predecessor unit freshly stopped and
  a new package generation. Old preparation/CA/runtime/probe/unit/source evidence
  remains intact; new files use `generation-<package-sha256>` beneath the owned
  controller and worker native directories. Reuse the exact prepared bundle path
  and DNS; the new config explicitly names the generation runtime path.
- Optional `cleanup_reserve_seconds` leaves time **inside** the allocation window
  for console capture and ordinary cleanup, e.g.180seconds out of1800, not an
  extra allowance. The reported allocation deadline and guest execution deadline
  are distinct. Provider/controller failures can still exceed the target;
  measured overruns are failures, never qualifying CPU gates after recovery.
- Factory HTTP mutations are one-shot even when the SDK defaults to retries:
  POST/PUT/PATCH/DELETE disable connection/status replay, implicit reauth and
  redirect-following. Mutation3xx is refused rather than treated as completion.
  Metadata GET/HEAD discovery remains unchanged.
- Large carrier data has one explicit Glance PUT. Only that request gets a scalar
  socket wait of at most600seconds and at most the remaining pre-Nova deadline;
  metadata keeps its40second default. This is not an absolute HTTP deadline,
  does not change allocation expiry, and never licenses another PUT. A short
  connect timeout would also shorten body-write tolerance.
- `upload_failure` records only bounded exception classes/errnos, source bytes
  read, expected bytes and status, never raw messages/headers/URLs. Source bytes
  read are not a server-delivery acknowledgment. Partial/failed uploads remain
  unqualified and exact-owned cleanup still applies.
- A completed nonzero carrier-builder transport retains private0600 sanitized
  stdout/stderr tails (64KiB each) in `<run-id>.build-failure.json`; the public
  report exposes only capture status/path/hash. The client exit code is not
  remote-process exit proof; existing terminal/custody gates still apply.
- The builder copies live host Python/tools and fails closed on changing files.
  Before a new approved case, require naturally idle package updaters, no held
  dpkg locks, an empty `dpkg --audit` and a single installed Python-package
  cohort. Do not pause/disable APT, configure pending packages, loosen identity
  checks or reuse a partial failed carrier without separately scoped authority.
- An existing deleting intent is observed with GET until404, never dispatched
  again automatically; an attached/error_deleting volume still holds. Cinder
  origin is checked against the durable volume image intent. If the first
  DELETE never reached its provider, a paid resource may remain and requires
  separately authorized manual disposition. A ConnectFailure or a non-deleting
  GET is not a no-effect certificate. Retained upload clients also block recovery.
- Preparation config explicitly chooses source installation and service startup,
  the verified bundle/runtime/probe paths, and DNS. It contains no cloud password.
  The bootstrap service has no OpenStack credentials and the carrier has no CephX
  credential; a separately enrolled per-consumer RO grant is delivered over mTLS.
- The carrier is BIOS/kernel/initramfs only, not a conventional Ubuntu guest root.
  Initramfs PID1 verifies layers and moves their overlay onto actual `/`.
  NVIDIA userspace is injected separately; the original image remains unchanged.
- A PID1 failure emits a fixed operation label, allowlisted exception type and
  bounded errno alongside its unchanged HOLD code. No exception message, path,
  key or traceback is emitted. Read the sanitized private pre-cleanup console;
  operation context diagnoses a failure and never qualifies the actual root.
- New consumers normally receive exact-owned cleanup after their result. If the
  controller dies, cloud resources are **not** autonomously deleted: key/enrollment
  expiry is not a released GPU. Use `native cleanup` with current bounded authority;
  an expired original cleanup tail needs a new cleanup-only recovery grant.
  Unreaped certificate actors with lost process custody need manual disposition.
- Source checks and carrier construction do not prove a boot. Do not count a
  conventional guest chroot, a console-only marker, ACTIVE, or SMI alone as the
  selected actual-root/GPU result. See [native GPU boundaries](oci-gpu-support.md).


## State locations

The unconfigured Linux default root is `/var/lib/palimpsest/`; an explicit
`XDG_STATE_HOME` selects `${XDG_STATE_HOME}/palimpsest/`, and other platforms
default to `~/.local/state/palimpsest/`:

```text
store/          content-addressed base images and layers
runs/           per-run ledgers, overlays, seeds, keys
builds/         build records and console logs
tags/           local layer tags
volumes/        project-owned block volumes
projects/       declarative project ledgers
```

Override the root with an absolute `PALIMPSEST_STATE_HOME`, which takes
precedence over the config file and XDG defaults. This is the clean way to keep
experiments out of your working store:

```bash
PALIMPSEST_STATE_HOME=/tmp/palimpsest-demo palimpsest store show
```

`palimpsest store move --to <dir>` relocates the current root;
`palimpsest store set --to <dir>` records an existing root.

## Troubleshooting

**Build fails.** Read the recorded console output:

```bash
palimpsest store show    # prints the active state_root
```

The failing build is the newest directory under `<state_root>/builds/`. Its
`console.log` holds the guest builder output; `record.json` carries the recipe
hash, base digest, network, and final status.

**`run name '<name>' already exists` or a removed name is held.** Free it:

```bash
palimpsest rm <name> --volumes
```

**Architecture mismatch.** `auto` refuses to boot an image whose architecture
differs from the host. Import an `aarch64` image on macOS arm64 and Linux
aarch64, and an `x86_64` image on Linux x86_64.

**`shell`/`exec` cannot connect.** On Linux, confirm the run does not use
`--network none` and that the libvirt `default` network is active. On both
platforms, first boot needs time for cloud-init; retry after checking
`palimpsest logs <name>`.

**Lima rejects the run.** `lima-vz` requires Lima 2.1+, an `aarch64` image, and
a network other than `none`.

**Linux KVM prerequisites.** `/dev/kvm` access, `qemu-system-*` matching the
image architecture, `qemu-img`, `cloud-localds`, `mksquashfs`, OpenSSH, an
active `qemu:///system` connection and `default` network, and the `[kvm]` extra.

## SDK publication, visibility and native warm reuse

The `0.3.11` source pairs `palimpsest-client` and `palimpsest-hub`.
Publication and native execution are separate API operations; installing the
client does not prepare a worker, mount storage or allocate a VM.

### One parent, many children

The Hub stores physical payload identity separately from logical layer identity.
A logical node has one nullable `parent_id`; many children can reference that
node. Its immutable ID binds the parent node, payload digest/size, platform,
execution mode and base/OS compatibility. Repeated bytes under another parent
produce another logical ID, not another parent on the same node.

The canonical `palimpsest.image-manifest.v1` records root-to-leaf nodes,
SquashFS digest/size, explicit OS family/version, platform, runtime, descriptive
metadata and available source/conversion provenance. OS fields are declarations,
not automatic distro detection or attestation. Registry workers retain original
compressed descriptors/DiffIDs and conversion receipts; local artifacts do not
invent registry documents. Full runtime environment values remain server-side;
catalog responses have `projection: public` and environment names instead.
They are not a re-hashable replacement for the exact stored manifest.

### Same publication engine for local and worker output

- `HubClient.import_registry_image(request)` and `build_dockerfile(request)`
  require `request_id`, name, `os_family`, `os_version` and the respective input.
  `visibility` defaults to `private`; bounded descriptive `metadata` is optional.
  Poll the accepted job ID. Only a new canonical completed job has a real
  `published_image_id`; historical completed receipts are not manufactured images.
- `HubClient.push_image(manifest, blobs, name=..., request_id=..., visibility=...)`
  streams supplied digest-to-`Path` entries and registers the complete image.
  The mapping may omit authorized existing payloads. A zero-upload hit still
  performs graph, image, permission and storage registration.
- `push_image_extension(base_image_id, layers, blobs, name=..., request_id=...,
  visibility=..., metadata=...)` uploads only new local build outputs. The
  server resolves the authorized full base, preserves its private runtime,
  appends the new descriptors and derives their parents. The client does not
  reconstruct runtime values from a redacted catalog response.

Both routes use the same verified publication and SQL registration engine.
New payloads cannot be exposed through the old project-wide layer/blob grants.

| Visibility | Read/reuse audience |
| --- | --- |
| `private` | Exact creator user, including that user's other authenticated project scopes |
| `internal` | Authenticated members authorized in the originating project |
| `public` | All authenticated callers with Palimpsest inventory/download authority |

Visibility is immutable for a publication. Publishing another reference does
not change the old one. Borrowed restrictions survive intermediate publications;
another creator cannot automatically turn a project's restricted inherited
input into a public image. Explicit labels are visible to the selected audience:
do not put credentials in descriptive metadata. Authorized execution necessarily
receives the full runtime, even though catalog reads omit environment values.

`list_published_images()` and `get_published_image(id)` discover these images.
`get_layer_node(node_id)`, `get_layer_children(node_id)` and
`get_layer_ancestors(node_id)` discover only visible graph membership.
`project_id`/`image_id` query qualifiers disambiguate references; ancestors are
returned root-to-parent, excluding the queried node.
Legacy `list_images()` still lists cloud-image artifacts, not these publications.

### Namespace-backed native execution

For a configured Manila store, the publisher places each complete image in one
scope under `cas/public`, `cas/user-<identity hash>` or
`cas/project-<identity hash>`. Directory layout selects an exact data pool and
RADOS namespace **before files are created**, and every file layout is checked.
Bytes deduplicate within a scope. Publication into another security scope copies
the payload once; cross-scope hardlinks are not used.

MDS path caps alone are insufficient: they do not restrict raw RADOS reads.
The guest's unique credential has both a read-only scope path and an exact
read-only OSD pool/namespace cap. See [Ceph's isolation requirement](https://docs.ceph.com/en/latest/cephfs/client-auth/).
The operator issuer key never enters the guest or the public SDK.

`create_native_instance` selects a published image UUID and an enabled operator
profile. A project-image profile uses one explicit CPU-only `source_project_id`
grant with `source_digest: null`; all different images consume that same original
cumulative budget. Narrow exact-manifest grants remain supported. Source,
kernel/runtime/probe, identity, share, finite allocation/cleanup expiry and
resource limits remain pinned. `allow_retained` and `allow_path_scoped_ceph` must
be explicitly approved; startup never generates those approvals.

The API returns a durable `creating`/`deleting` intent with HTTP202 before slow
provider work. A single background operation revalidates the original human
and opens its own caller-scoped connection; it is not a restart/retry queue.
GET/list only read SQL. A crash or unknown effect remains pending/HOLD, never
permission to repeat allocation. Keep original request IDs on any lost response.

Warm reuse skips registry acquisition, conversion and republishing. It still
creates a new carrier/boot volume/VM, issues its bounded namespace credential,
mounts and verifies existing SquashFS files, and assembles a private upper/work.
The native PID1 changes the VM's actual root; the older `/instances` HTTPS/chroot
backend remains explicit and is never a fallback for this operation.

Successful native execution currently means a bounded command completed and
the retained VM is idle under PID1, not long-lived application readiness,
memory-snapshot resume, attestation or automatic cleanup after controller death.
Delete before the original allocation/cleanup deadlines, then require
`deleted`/authoritative provider absence before the next test consumer.
Ceph auth-record deletion is not a claim of immediate mounted-session eviction.

```python
import hashlib
import time
from pathlib import Path
from uuid import uuid4

from palimpsest_local import HubClient


def publish_local_child(client: HubClient, base_id: str, artifact: Path):
    with artifact.open("rb") as stream:
        digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    return client.push_image_extension(
        base_id,
        [{"digest": digest, "size_bytes": artifact.stat().st_size}],
        {digest: artifact},
        name="local-child",
        request_id=str(uuid4()),
        visibility="private",
        metadata={"build_origin": "local"},
    )


def wait_native(client: HubClient, instance_id: str, expected: str, deadline: float):
    while True:
        result = client.get_native_instance(instance_id)
        if result["status"] == expected:
            return result
        if result["status"] == "held":
            raise RuntimeError(f"inspect exact held instance {instance_id}; do not replay")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"inspect original instance {instance_id} and cleanup authority")
        time.sleep(2)
```

Use the returned image ID in `create_native_instance` with a fresh request ID,
the operator-selected `profile`, explicit command, approved timeout and
`keep=True`; poll that exact instance ID for `retained`. Call
`delete_native_instance(id)` once and poll it for `deleted`. Do not construct a
new create request to recover an uncertain old request.

**Evidence boundary:** local real SquashFS/HTTP/SQLite/SDK smoke and source
regressions are not real CephFS namespace, raw-RADOS denial or Nova warm-start
qualification. Final source and exact deployment/resource approval are required
before that separate proof. See the latest development handoff.

## Related documentation

- [Hello VM walkthrough](../examples/hello-vm/README.md)
- [Installing Palimpsest Local](install.md)
- [Quickstart guide](quickstart.md)
- [Declarative multi-VM projects](projects.md)
- [BuildKit cache and block runtime workflow](buildkit-block-workflow.md)
