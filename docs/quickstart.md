# Palimpsest Local Quickstart Guide

This guide covers common workflows using the `palimpsest` CLI tool for working with content-addressed boot images, SquashFS layers, OCI bundles, Docker/OCI registries, and local KVM virtual machine lifecycles.

---

## Prerequisites & Environment Setup

Set your Hub URL and Bearer token via environment variables:

```bash
export PALIMPSEST_URL="https://hub.afterglow.dev"
export PALIMPSEST_TOKEN="ag_token_example_12345"
```

These variables authenticate Palimpsest Hub's native `/v1` artifact and cache API. Hub is separate from any Docker/OCI `/v2` registry configured below.

### Shell Completion

Generate dynamic shell completion scripts for `zsh` (primary for macOS), `bash`, or `fish`. Completion queries the live CLI `argparse` tree dynamically and suppresses unrelated filesystem path suggestions. The `palimpsest` executable (or active virtual environment) must be active and on your `PATH`. Installing the package does not silently modify shell configuration files.
The Zsh and Bash startup forms require `palimpsest` on `PATH` when the shell starts. With the project-local virtual environment, run the current-shell activation after `source .venv/bin/activate`.

#### Activation (Current Shell vs. Persistent)

**Zsh (macOS / Linux):**
```bash
# Current shell session:
autoload -Uz compinit && compinit
eval "$(palimpsest completion zsh)"

# Persistent setup: add both lines to ~/.zshrc
autoload -Uz compinit && compinit
eval "$(palimpsest completion zsh)"
```

**Bash & Fish Alternatives:**
```bash
# Bash - Current session:
source <(palimpsest completion bash)
# Bash - Persistent: add this guarded line to ~/.bashrc
if command -v palimpsest >/dev/null 2>&1; then source <(palimpsest completion bash); fi

# Fish - Current session:
palimpsest completion fish | source
# Fish - Persistent:
mkdir -p ~/.config/fish/completions
palimpsest completion fish > ~/.config/fish/completions/palimpsest.fish
```

#### Completion Examples

- `palimpsest <Tab><Tab>` → suggests top-level command groups (`image`, `layer`, `bundle`, `build`, `run`, `compose`, `ui`, `store`, etc.)
- `palimpsest image <Tab><Tab>` → suggests `image` subcommands (`inspect`, `history`, `rm`, `save`, `load`, `ls`, `pull`, `verify`, `import`, `push`)
- `palimpsest run --backend <Tab><Tab>` → suggests backend choices (`auto`, `kvm`, `lima-vz`, `libvirt-hvf`)

---

## 1. Artifact Management (`image`, `layer`, `bundle`)

### Managing Boot Images (`image`)

Boot images are bootable `qcow2` or `raw` cloud images used as immutable base disks (`vda`).

These `palimpsest image ls|pull|push|verify|import` operations use Palimpsest Hub. The separately documented `palimpsest image inspect` operation inspects a Docker/OCI image through Docker.

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

## 2. Docker/OCI Registry Images

The built-in `docker` profile resolves unqualified image names through Docker Hub. Add an external profile and optionally make it the default:

```bash
palimpsest registry add corp registry.example.com \
  --namespace platform \
  --default

palimpsest registry ls
palimpsest registry inspect corp
```

For an unqualified image, selection order is: a registry written in the reference, `--registry`, `PALIMPSEST_REGISTRY`, then the configured default. With the profile above, `api:v1` becomes `registry.example.com/platform/api:v1`.

Authenticate and use Docker-compatible image commands:

```bash
palimpsest login --registry corp

# For CI, keep the password out of process arguments and shell history.
printf '%s\n' "$REGISTRY_PASSWORD" | \
  palimpsest login --registry corp --username ci-user --password-stdin

palimpsest pull api:v1 --registry corp
palimpsest tag local-api:dev api:v1 --registry corp
palimpsest push api:v1 --registry corp
palimpsest images --digests
palimpsest image inspect api:v1 --registry corp
palimpsest image history registry.example.com/platform/api:v1
palimpsest image save registry.example.com/platform/api:v1 -o ./api.tar
palimpsest image load -i ./api.tar
palimpsest logout --registry corp
```

Palimpsest delegates these operations to the installed Docker CLI and reuses Docker's credential helpers from `DOCKER_CONFIG` or `~/.docker`. Credentials are never stored in the Palimpsest registry profile. `history`, `rmi`, `save`, and `load` are top-level aliases for the equivalent `image` subcommands. For other Docker operations, `palimpsest docker ...` passes arguments directly to Docker using the same credential directory, but does not resolve Palimpsest profiles; it rejects a Docker-global `--config` override before the subcommand and all `login -p|--password` forms.

See [Docker/OCI Registry Profiles](registries.md) for mirrors, private CAs, external caches, and exact reference rules. Generated BuildKit mirror/CA configuration does not alter Docker Engine/Desktop's pull/push trust or insecure-registry configuration.

---

## 3. Running Local Virtual Machines (`run`)

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

## 4. Running a Multi-VM Project (`compose`)

Create `palimpsest.yml` with one boot-image digest per VM service, dependency order, environment, typed cloud-init, and named block storage:

```yaml
version: "1"
name: demo
volumes:
  database:
    driver: block
    size: 20GiB
services:
  db:
    image: sha256:1111111111111111111111111111111111111111111111111111111111111111
    volumes: ["database:/var/lib/database"]
  api:
    image: sha256:2222222222222222222222222222222222222222222222222222222222222222
    depends_on: [db]
    ports: ["127.0.0.1:18080:8080"]
    environment:
      APP_MODE: ${APP_MODE:-development}
    cloud_init:
      inline:
        packages: [curl]
        runcmd:
          - [systemctl, enable, --now, demo-api]
```

Validate and operate it:

```bash
palimpsest compose config --quiet
palimpsest compose up -d
palimpsest compose ps
palimpsest compose logs api
palimpsest compose exec api -- uname -a
palimpsest compose port api 8080
palimpsest compose down
```

`down` preserves the named block volume; add `--volumes` to delete exact project-owned volumes. Host bind/NFS mounts are not accepted. Lima implements static TCP port forwarding. Linux KVM currently fails closed when a project declares `ports`, because the existing libvirt network interface does not provide safe per-domain inbound forwarding.

See [Declarative multi-VM projects](projects.md) for all supported keys, `.env`/`--env-file` precedence, typed cloud-init, lifecycle reconciliation, and explicit Compose differences.

---

## 5. VM Inspection & Interaction

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

## 6. Building Layers (`build` & `commit`)

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

### Building a Dockerfile with BuildKit (Experimental Interface)

> The local build/cache/runtime-pack path is implemented. Production release still requires the clean-host Linux KVM and concurrency gates described below.

An online build downloads and imports a verified Hub cache before executing an authoritative miss, uploads the refreshed Hub cache automatically, and creates one verified SquashFS runtime block. Optional registry caches are additive to that mandatory Hub path. In the following example, `--push` publishes the OCI image to the selected registry and `--runtime-push` uploads the runtime block to Hub:

```bash
RUNTIME_BASE=sha256:<boot-image-digest>

palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --registry corp \
  --tag demo:v1 \
  --platform linux/amd64 \
  --runtime-base "$RUNTIME_BASE" \
  --runtime-tag demo-runtime \
  --push \
  --runtime-push
```

`--platform linux/amd64` requires an `x86_64` runtime base; `linux/arm64` requires `aarch64`. Palimpsest resolves and checks the base before starting BuildKit.

Hub authentication, timeout, 5xx, malformed cache metadata, and digest mismatch fail the online build. They do not silently trigger local execution.

Online input identities must be immutable. Pin each fully qualified remote `FROM` and any external Dockerfile frontend, for example `FROM registry.example.com/platform/base@sha256:<manifest-digest>` and `# syntax=docker/dockerfile:1@sha256:<frontend-manifest-digest>`. Registry profiles do not rewrite Dockerfile sources. Remote HTTP `ADD` also requires `--checksum=sha256:<digest>`; remote Git `ADD` is rejected, and external `COPY/ADD --from` or `RUN --mount ... from=` sources must be digest-pinned. Mutable tags and ARG-expanded remote image sources are rejected before inspecting Buildx or querying Hub.

Use repeated standard Buildx cache specifications when another registry should also participate:

```bash
palimpsest build . \
  --frontend dockerfile \
  --tag demo:v1 \
  --registry corp \
  --cache-from type=registry,ref=registry.example.com/cache/demo \
  --cache-to type=registry,ref=registry.example.com/cache/demo,mode=max \
  --push
```

The Hub lookup and refreshed Hub cache upload still occur. Profile-level `cache_from`/`cache_to` entries are merged with these command-line entries.

To build without Hub or a registry, use a digest-pinned local OCI layout. Every Dockerfile `FROM` must resolve through a local alias. Strict isolation also requires selecting an already-bootstrapped local `docker-container` Buildx builder created with `--driver-opt network=none`; see the [installation guide](install.md#3-buildkit-requirements-for-dockerfile-builds).

```bash
BUILDX_BUILDER=palimpsest-offline palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --tag demo \
  --runtime-base "$RUNTIME_BASE" \
  --runtime-tag demo-runtime \
  --offline \
  --local-image local-base=/absolute/path/to/base-layout@sha256:<manifest-digest> \
  --network none
```

Strict offline mode does not load Palimpsest registry profiles or invoke registry authentication. Docker may still read its selected `DOCKER_CONFIG` to locate the local context and Buildx builder. The mode rejects `--registry`, `--pull`, `--push`, `--runtime-push`, external `--cache-from`/`--cache-to`, and network-enabled build steps.

Run the emitted runtime digest through the existing KVM block interface, or upload the local runtime tag explicitly when the build did not use `--runtime-push`:

```bash
RUNTIME_DIGEST=sha256:<runtime-squashfs-digest>

palimpsest run "$RUNTIME_BASE" \
  --layer "$RUNTIME_DIGEST" \
  --name demo

palimpsest exec demo -- ls -la /opt/layers/merged

palimpsest layer push demo-runtime \
  --base-image "$RUNTIME_BASE"
```

BuildKit cache records preserve logical Dockerfile reuse. Local cache scopes use an atomic `current.json` pointer to complete immutable generations and serialize same-scope solves, so an interrupted promotion cannot replace a usable cache with a partial directory. Runtime conversion has a separate base/platform/packer-bound key: a verified hit skips `mksquashfs`, while the runtime digest still identifies the exact single SquashFS block attached to the VM. See [BuildKit Cache and Block Runtime Workflow](buildkit-block-workflow.md) for the cache resolution rules, offline isolation contract, performance receipts, and acceptance gates.

### Committing a Delta from a Running VM

If you have made changes inside a running guest at `/opt/layers/merged`, you can capture the guest writable delta (`upperdir`) into a fresh layer:

```bash
palimpsest commit web-dev --tag web-dev-custom-layer
```

---

## 7. Lifecycle Teardown (`stop` & `rm`)

```bash
# Stop a running VM via ACPI shutdown (falls back to force destroy after 30s)
palimpsest stop web-dev

# Remove run metadata ledger (retains disk overlay for inspection)
palimpsest rm web-dev

# Completely delete run ledger, writable overlay, seed ISO, and SSH keys
palimpsest rm web-dev --volumes
```

---


## Next Steps

- **[VM workflow guide](vm-workflow.md):** manual import/build/run/exec/cleanup reference, backend matrix, state locations, and troubleshooting.
- **[Hello VM walkthrough](../examples/hello-vm/README.md):** scripted end-to-end tutorial starting from an official Ubuntu cloud image.
- **[Declarative multi-VM projects](projects.md):** `palimpsest.yml` orchestration for several VMs.

## KVM Runtime Requirements Notice

> **Important:** On Linux, commands that create or manage virtual machines (`run`, `compose`, Palimpsestfile guest `build`, `commit`, `shell`, `exec`, `stop`, `rm`, `ps`, `inspect`, `logs`) require `/dev/kvm` access and `palimpsest-local[kvm]`. On Apple Silicon, supported `run`/`compose` operations use Lima/VZ instead. Dockerfile/Buildx builds do not use libvirt; runtime packing additionally requires `mksquashfs`.
>
> On hosts without KVM or when `libvirt-python` is absent:
> - Palimpsestfile guest builds and `palimpsest commit` raise operational errors indicating KVM runtime is unavailable; Dockerfile/Buildx builds remain available.
> - Full release `v0.1.0` cutover is blocked until end-to-end execution proof is verified on a Linux KVM host (`pytest -m kvm`).
