# Palimpsest Local Quickstart Guide

This guide covers common workflows using the `palimpsest` CLI tool for working with content-addressed boot images, SquashFS layers, OCI bundles, and local KVM virtual machine lifecycles.

---

## Prerequisites & Environment Setup

Set your Hub URL and Bearer token via environment variables:

```bash
export PALIMPSEST_URL="https://hub.afterglow.dev"
export PALIMPSEST_TOKEN="ag_token_example_12345"
```


---

## 1. Artifact Management (`image`, `layer`, `bundle`)

### Managing Boot Images (`image`)

Boot images are bootable `qcow2` or `raw` cloud images used as immutable base disks (`vda`).

```bash
# List available boot images on Hub (limit 1..200)
palimpsest image ls --arch x86_64 --limit 10

# Pull a boot image to the local content store
palimpsest image pull sha256:1111111111111111111111111111111111111111111111111111111111111111

# Optionally save a copy to a specific output directory (creates <output>/<hex>.qcow2)
palimpsest image pull sha256:1111111111111111111111111111111111111111111111111111111111111111 --output ./dist

# Verify a local file against a declared SHA-256 digest (exits 0 on success)
palimpsest image verify ./dist/1111111111111111111111111111111111111111111111111111111111111111.qcow2 \
  --digest sha256:1111111111111111111111111111111111111111111111111111111111111111

# Push a local boot image file to Hub
palimpsest image push ./ubuntu-base.qcow2 \
  --name "ubuntu-24.04-base" \
  --disk-format qcow2 \
  --arch x86_64 \
  --publish
```

### Managing SquashFS Layers (`layer`)

Layers are read-only SquashFS filesystems mounted as virtio-blk disks (`vdb`..`vdz`) and combined via OverlayFS inside the guest.

```bash
# List available layers on Hub
palimpsest layer ls --limit 20

# Pull a specific layer digest to local store
palimpsest layer pull sha256:2222222222222222222222222222222222222222222222222222222222222222

# Pack a local directory into a zstd-compressed SquashFS layer tag
palimpsest layer pack ./my-app-files --tag my-app-v1

# Push a packed tag (or raw SquashFS file path) to Hub
palimpsest layer push my-app-v1 \
  --base-image sha256:1111111111111111111111111111111111111111111111111111111111111111 \
  --publish
```

### Working with Bundles (`bundle`)

Bundles package a boot image and an ordered layer chain into an OCI-layout directory (`blobs/sha256/<hex>`, `index.json`, `oci-layout`).

```bash
# Download a complete stack bundle to an OCI directory
palimpsest bundle pull sha256:3333333333333333333333333333333333333333333333333333333333333333 \
  --output ./bundle-dir \
  --include-base

# Safely verify every descriptor and digest in an OCI bundle directory
palimpsest bundle verify ./bundle-dir
```

---

## 2. Running Local Virtual Machines (`run`)

Launch a KVM virtual machine from a base image digest or an OCI bundle directory:

```bash
# Run a stack using an explicit base image and ordered layer digests
palimpsest run sha256:1111111111111111111111111111111111111111111111111111111111111111 \
  --name web-dev \
  --layer sha256:2222222222222222222222222222222222222222222222222222222222222222 \
  --layer sha256:3333333333333333333333333333333333333333333333333333333333333333 \
  --memory 4096 \
  --vcpus 2 \
  --network default

# Or run directly from an extracted OCI bundle directory:
palimpsest run ./bundle-dir --name web-dev
```

### Runtime Architecture Overview
1. **Writable Root (`vda`):** A per-run qcow2 overlay (`<state>/runs/web-dev/overlay.qcow2`) is created over the immutable base image. Base images are **never attached read-write**.
2. **Layer Disks (`vdb`..`vdz`):** Up to 25 SquashFS layers are attached as read-only virtio disks with digest-derived serial numbers (`virtio-<serial>`).
3. **Guest Activation:** Inside the guest, cloud-init mounts layer disks read-only under `/mnt/palimpsest/lowerN` and builds an OverlayFS mount at `/opt/layers/merged` with `lowerdir` ordered leaf → root.

---

## 3. VM Inspection & Interaction

```bash
# List all active and stopped local runs
palimpsest ps

# View machine-readable JSON inspect ledger with safety warnings
palimpsest inspect web-dev

# Stream live serial console logs
palimpsest logs web-dev --follow

# Open an interactive SSH shell into the guest as user 'ubuntu'
palimpsest shell web-dev

# Execute non-interactive commands safely (uses base64 helper payload, no host shell parsing)
palimpsest exec web-dev -- python3 -c "import sys; print(sys.version)"
palimpsest exec web-dev -- ls -la /opt/layers/merged
```

---

## 4. Building Layers (`build` & `commit`)

### Building from a Palimpsestfile

Create a `Palimpsestfile` defining your layer steps:

```dockerfile
FROM sha256:1111111111111111111111111111111111111111111111111111111111111111
LAYER sha256:2222222222222222222222222222222222222222222222222222222222222222

WORKDIR /opt/app
ENV NODE_ENV=production

RUN apt-get update && apt-get install -y nodejs
RUN node -v > /opt/app/node-version.txt
```

Execute the build in a disposable KVM guest:

```bash
# Build a new SquashFS layer tag in an isolated guest (--network none by default)
palimpsest build \
  --base sha256:1111111111111111111111111111111111111111111111111111111111111111 \
  --layer sha256:2222222222222222222222222222222222222222222222222222222222222222 \
  --tag nodejs-layer \
  -f ./Palimpsestfile \
  --network default

```

`--network none` is the CLI default. Its builder has no libvirt interface and receives no SSH key or host credential; the package retrieves the completed SquashFS over a package-owned output-only virtio-serial channel. `--network default` uses the same constrained builder transport while attaching the named libvirt network for recipes that need package installation. Capture staging is guest-local tmpfs, so the practical writable-delta limit is constrained by builder memory. Real x86_64 Linux KVM isolation proof remains a release gate.

### Committing a Delta from a Running VM

If you have made changes inside a running guest at `/opt/layers/merged`, you can capture the guest writable delta (`upperdir`) into a fresh layer:

```bash
palimpsest commit web-dev --tag web-dev-custom-layer
```

---

## 5. Lifecycle Teardown (`stop` & `rm`)

```bash
# Stop a running VM via ACPI shutdown (falls back to force destroy after 30s)
palimpsest stop web-dev

# Remove run metadata ledger (retains disk overlay for inspection)
palimpsest rm web-dev

# Completely delete run ledger, writable overlay, seed ISO, and SSH keys
palimpsest rm web-dev --volumes
```

---

## KVM Runtime Requirements Notice

> **Important:** Commands that create or manage virtual machines (`run`, `build`, `commit`, `shell`, `exec`, `stop`, `rm`, `ps`, `inspect`, `logs`) require a Linux host with `/dev/kvm` access and `palimpsest-local[kvm]` installed.
>
> On hosts without KVM or when `libvirt-python` is absent:
> - `palimpsest build` and `palimpsest commit` raise operational errors indicating KVM runtime is unavailable.
> - Full release `v0.1.0` cutover is blocked until end-to-end execution proof is verified on a Linux KVM host (`pytest -m kvm`).
