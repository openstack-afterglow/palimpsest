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

## Platforms and backends

| Host | Backend | Image arch | Layer attachment |
|---|---|---|---|
| macOS arm64 | `lima-vz` (default) | `aarch64` | SquashFS copied into the guest, loop-mounted read-only |
| macOS arm64 | `libvirt-hvf` (experimental) | `aarch64` | QEMU/HVF via `qemu:///session`, SLIRP `hostfwd` |
| Linux x86_64 | `kvm` | `x86_64` | Read-only `virtio-blk` disks `vdb`..`vdz` |
| Linux aarch64 | `kvm` | `aarch64` | Read-only `virtio-blk` disks `vdb`..`vdz` |

`--backend auto` (the default) picks `kvm` on Linux and `lima-vz` on macOS
arm64, and requires the image architecture to match the host. Backend
prerequisites are listed in [install.md](install.md).

`libvirt-hvf` and Linux KVM need the optional `[kvm]` extra
(`libvirt-python`). The default macOS Lima/VZ path does not.

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
exit in time; it is idempotent. `rm` destroys and undefines the VM. Without
`--volumes` the run directory and its name remain reserved, and reusing the name
fails with a message telling you to rerun with `--volumes`; with `--volumes` the
run directory, overlay, seed, and keys are deleted.

Neither form deletes content-store artifacts. Base images and layers stay
reusable:

```bash
palimpsest store ls --kind layer
palimpsest store rm sha256:<digest>
```

## State locations

Default root is `${XDG_STATE_HOME:-~/.local/state}/palimpsest/`:

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
