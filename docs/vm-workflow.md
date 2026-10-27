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

## Related documentation

- [Hello VM walkthrough](../examples/hello-vm/README.md)
- [Installing Palimpsest Local](install.md)
- [Quickstart guide](quickstart.md)
- [Declarative multi-VM projects](projects.md)
- [BuildKit cache and block runtime workflow](buildkit-block-workflow.md)
