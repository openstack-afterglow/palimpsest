# Palimpsest Local

`palimpsest-local` is a Python 3.12+ CLI for managing Palimpsest boot images, SquashFS layers, OCI-layout bundles, and local layered virtual machines.

It provides the `palimpsest` command and keeps local artifacts, tags, run state, and build records under XDG state directories. The core package has no required Python runtime dependencies; Linux KVM support is an optional extra.

## Status

- **macOS Apple Silicon:** default runtime using Lima/VZ (`lima-vz`), with experimental QEMU/libvirt Hypervisor.framework support (`libvirt-hvf`).
- **Linux:** KVM/libvirt runtime support for `x86_64` and `aarch64` (`virt` machine + EFI). Standalone release `0.1.0` requires clean-host KVM integration proof.
- **Declarative projects:** a strict `palimpsest.yml` workflow reconciles multiple VM services with dependencies, environment, typed cloud-init, persistent block volumes, networks, and Lima TCP forwarding.
- **Version:** `0.1.0.dev0`.

## Install

### Development checkout

```sh
uv sync --extra dev
uv run palimpsest --help
```

### Package installation

```sh
pip install .
# or
uv pip install .
```

To use the Linux KVM runtime, install the optional extra:

```sh
pip install '.[kvm]'
```

## Hub configuration & Standalone Service

Palimpsest Hub runs as a standalone FastAPI service on port 8020 using OpenStack Keystone token authentication (`X-Auth-Token` and optional `X-Project-Id`).

Hub's native `/v1` API stores Palimpsest boot images, SquashFS runtime blocks, bundles, and BuildKit cache archives. It is not a Docker/OCI `/v2` registry. Ordinary OCI image commands use a separately configured registry profile.

### Entrypoints & Docker Targets

- **API Worker:** `uvicorn palimpsest_hub.main:app --host 0.0.0.0 --port 8020` (Docker target `palimpsest-hub-api`)
- **Async Export Worker:** `python -m palimpsest_hub.worker` (Docker target `palimpsest-hub-worker`)
- **Database Bootstrap:** `python -m palimpsest_hub.bootstrap` or `python -m palimpsest_hub.migrate`

### Client Hub Configuration

Hub CLI commands use a base URL (`PALIMPSEST_URL` or `--url`) and Keystone token (`PALIMPSEST_TOKEN` or environment) to call native `/v1` Hub endpoints over `X-Auth-Token`.

```sh
export PALIMPSEST_URL="http://hub.example:8020"
export PALIMPSEST_TOKEN="gAAAAAB..."

palimpsest image ls --arch aarch64
palimpsest --url http://another-hub.example:8020 image ls
```

`PALIMPSEST_URL` overrides the URL in `${XDG_CONFIG_HOME:-~/.config}/palimpsest/config.toml`; an explicit `--url` takes precedence over both.

## Docker/OCI registry configuration

The built-in `docker` profile resolves unqualified references through `docker.io`; external registries can be configured independently:

```sh
palimpsest registry add corp registry.example.com \
  --namespace platform \
  --default

palimpsest registry ls
palimpsest login --registry corp
palimpsest pull api:v1 --registry corp
palimpsest tag local-api:dev api:v1 --registry corp
palimpsest push api:v1 --registry corp
palimpsest images --digests
palimpsest image inspect api:v1 --registry corp
palimpsest image history registry.example.com/platform/api:v1
palimpsest image save registry.example.com/platform/api:v1 -o ./api.tar
palimpsest image load -i ./api.tar
```

Profiles are stored without secrets in `${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml`. Selection order for an unqualified reference is: a registry written in the reference, `--registry`, `PALIMPSEST_REGISTRY`, then the configured default. Palimpsest reuses the existing Docker credential store from `DOCKER_CONFIG` or `~/.docker`; use `login --password-stdin` for non-interactive authentication. `palimpsest docker ...` provides a generic Docker passthrough for commands without a first-class wrapper while blocking Docker-global `--config` overrides before the subcommand and password-bearing login arguments.

See [Docker/OCI registry profiles](docs/registries.md) for cache settings, private CAs/mirrors, Docker-compatible command coverage, and offline restrictions.

## Artifact workflow

```sh
# List and pull a boot image.
palimpsest image ls --arch aarch64 --limit 10
palimpsest image pull sha256:<image-digest>

# Import a locally downloaded cloud image.
palimpsest image import ./ubuntu-24.04-server-cloudimg-arm64.img \
  --disk-format qcow2 --arch aarch64 --os-variant ubuntu-24.04

# Create and publish a layer.
palimpsest layer pack ./rootfs --tag app-v1
palimpsest layer push app-v1 --base-image sha256:<image-digest>

# Download or inspect an OCI-layout bundle.
palimpsest bundle pull sha256:<leaf-layer-digest> --include-base --output ./bundle
palimpsest bundle verify ./bundle
```

Local artifacts live below `${XDG_STATE_HOME:-~/.local/state}/palimpsest/`:

```text
store/       content-addressed blobs and metadata
runs/        local VM state
projects/    declarative project ownership and reconciliation ledgers
volumes/     project-owned KVM block images and Lima disk receipts
builds/      build records and console output
build-cache/ BuildKit local-exporter cache by scope
runtime-packs/ base/platform/packer-bound SquashFS conversion indexes
tags/        local layer tags
transfers/   Hub transfer progress
```

## Dockerfile cache and runtime-block workflow

The Dockerfile workflow keeps BuildKit's logical vertex cache separate from the runtime artifact. BuildKit reuses unchanged build work; Palimpsest feeds BuildKit's metadata-preserving rootfs tar directly into SquashFS, binds the block to its boot-base/platform contract, and the Linux KVM runtime attaches the verified result as a read-only `virtio-blk` disk.

```sh
palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --registry corp \
  --tag demo:v1 \
  --runtime-base sha256:<boot-image-digest> \
  --runtime-tag demo-runtime \
  --push \
  --runtime-push
```

Online mode requires digest-pinned remote `FROM` and external Dockerfile frontend references, resolves and downloads a digest-verified cache archive through Hub before executing an authoritative miss, uploads the refreshed Hub cache automatically, and fails closed when Hub cannot answer. Repeated external `--cache-from`/`--cache-to` definitions and profile caches are additive; they never replace the mandatory Hub cache. Same-scope builds serialize cache resolution, solving, and crash-safe generation promotion. `--push` publishes the OCI image through Buildx, while `--runtime-push` uploads the generated SquashFS runtime block to Hub.

Strict `--offline` mode uses only verified local OCI layouts, local cache, and `--network none`. It does not load Palimpsest registry profiles, invoke registry authentication, construct a Hub/registry client, or permit `--registry`, `--pull`, either push flag, or external cache backends. Docker may still read its selected `DOCKER_CONFIG` to locate the local context and Buildx builder. Remote inputs remain immutable: registry profiles do not rewrite Dockerfile `FROM` lines, and remote images must be fully qualified and digest-pinned. See [BuildKit cache and block runtime workflow](docs/buildkit-block-workflow.md) for the local run/upload flow, performance matrix, and remaining KVM acceptance gates.

## macOS Apple Silicon

Install [Lima](https://lima-vm.io/) and use an ARM64 Ubuntu cloud image.

```sh
brew install lima

palimpsest image import ./ubuntu-24.04-server-cloudimg-arm64.img \
  --disk-format qcow2 --arch aarch64 --os-variant ubuntu-24.04

palimpsest run sha256:<image-digest> --name ubuntu-arm --memory 4096 --vcpus 2
palimpsest inspect ubuntu-arm
palimpsest shell ubuntu-arm
palimpsest exec ubuntu-arm -- uname -m
```

The VZ backend provides managed NAT. `inspect` reports the guest IPv4 address and Lima's host-local SSH endpoint. `shell` opens a Lima shell; `exec` runs one command in the guest.

A macOS build uses a disposable Lima guest and produces the same portable SquashFS layer artifact used by the content store:

```sh
palimpsest build \
  --base sha256:<image-digest> \
  --tag tools-arm \
  -f ./Palimpsestfile \
  --network none

palimpsest run sha256:<image-digest> \
  --layer sha256:<built-layer-digest> \
  --name tools-arm
palimpsest exec tools-arm -- ls /opt/layers/merged
```

`commit` is not available for Lima-managed macOS runs; create a new portable layer with `build`.

## Linux KVM

The KVM runtime requires:

- Linux x86_64 with `/dev/kvm`
- QEMU, libvirt, `qemu-img`, `cloud-localds`, `mksquashfs`, OpenSSH
- a configured `qemu:///system` connection and `default` libvirt network
- `palimpsest-local[kvm]`

Run a base image and ordered layers:

```sh
palimpsest run sha256:<image-digest> \
  --name web-dev \
  --layer sha256:<root-layer-digest> \
  --layer sha256:<leaf-layer-digest> \
  --memory 4096 \
  --vcpus 2 \
  --network default

palimpsest ps
palimpsest inspect web-dev
palimpsest logs web-dev --follow
palimpsest shell web-dev
palimpsest exec web-dev -- ls /opt/layers/merged
palimpsest stop web-dev
palimpsest rm web-dev --volumes
```

Layers are exposed inside the guest at `/opt/layers/merged` in leaf-to-root overlay order.

## Multi-VM projects (`palimpsest.yml`)

Use the Compose-shaped project workflow when several VMs belong together:

```yaml
version: "1"
name: demo
volumes:
  data: {driver: block, size: 20GiB}
services:
  db:
    image: sha256:<boot-image-digest>
    volumes: ["data:/var/lib/data"]
  api:
    image: sha256:<boot-image-digest>
    layers: [sha256:<runtime-layer-digest>]
    depends_on: [db]
    ports: ["127.0.0.1:18080:8080"]
    environment:
      APP_ENV: ${APP_ENV:-development}
```

```sh
palimpsest compose config --quiet
palimpsest compose up -d
palimpsest compose ps
palimpsest compose exec api -- uname -a
palimpsest compose down             # keep persistent block volumes
palimpsest compose down --volumes   # delete owned volumes too
```

The schema deliberately rejects unsupported Compose fields. Named storage is a single-writer block device, never NFS or a host bind. Lima supports static TCP forwarding; the current Linux libvirt network path rejects `ports` until a verified `passt` implementation is available. See [Declarative multi-VM projects](docs/projects.md) for the complete schema, cloud-init subset, interpolation rules, and backend differences.

## Building layers

A `Palimpsestfile` declares one base image, optional parent layers, environment values, a working directory, and one or more `RUN` commands.

```dockerfile
FROM sha256:<image-digest>
LAYER sha256:<parent-layer-digest>
WORKDIR /opt/app
ENV APP_ENV=production
RUN mkdir -p /opt/app && printf 'hello\n' > /opt/app/message.txt
```

Build it with the same base and parent chain:

```sh
palimpsest build \
  --base sha256:<image-digest> \
  --layer sha256:<parent-layer-digest> \
  --tag app-layer \
  -f ./Palimpsestfile \
  --network default
```

The command prints the generated layer digest. Use it with `run --layer` or `layer push`.

## Runnable Examples

### Quick rootfs layer example

Pack a directory tree into a SquashFS layer and register it in the local content store:

```sh
# From the repository root
./examples/hello-layer/run.sh

# The runner also works when invoked by path from another directory
/path/to/palimpsest/examples/hello-layer/run.sh my-custom-layer
```

The script packs `./examples/hello-layer/rootfs/` (containing `/opt/palimpsest-example/hello.txt`), outputs the resulting `sha256:` layer digest, and lists layer artifacts via `palimpsest store ls --kind layer` to prove registration in the local content store. `mksquashfs` records image creation time, so repeated runs produce new digests; set an absolute `PALIMPSEST_STATE_HOME` to keep repeated experiments out of your working store.

### Complete VM workflow example

Import a cloud image, build a layer in a disposable guest, and boot a VM with that layer attached:

```sh
# From the repository root; pass the image matching your host architecture
./examples/hello-vm/run.sh /path/to/ubuntu-24.04-server-cloudimg-arm64.img

# Optional second argument: a custom run name
./examples/hello-vm/run.sh /path/to/ubuntu-24.04-server-cloudimg-arm64.img my-demo-vm
```

The runner maps the host to an architecture and backend (macOS arm64 to `aarch64`/`lima-vz`, Linux to `kvm`), imports the image, builds a no-network Palimpsestfile layer, starts a 2048 MiB / 2 vCPU VM, verifies the layer under `/opt/layers/merged`, and leaves the VM running with cleanup commands printed. See the [Hello VM walkthrough](examples/hello-vm/README.md) for the step-by-step tutorial and the [VM workflow guide](docs/vm-workflow.md) for the manual command reference.

## Command groups

```text
palimpsest registry ls|add|use|rm|inspect|buildkit-config
palimpsest login|logout|pull|push|tag|images
palimpsest history|rmi|save|load             # Docker top-level aliases
palimpsest docker <docker-cli-arguments...>  # generic passthrough
palimpsest image  inspect|history|rm|save|load # Docker/OCI image
palimpsest image  ls|pull|verify|import|push # Hub boot image
palimpsest layer  ls|pull|pack|push
palimpsest bundle pull|verify
palimpsest build
palimpsest run
palimpsest compose config|up|down|ps|logs|exec|stop|port
palimpsest ps|inspect|logs|shell|exec|stop|rm|commit
palimpsest ui                                # web management dashboard
palimpsest store show|ls|rm|move|set         # storage state & artifact management
palimpsest completion zsh|bash|fish          # shell completion generator
```

Use `palimpsest <command> --help` for exact arguments.

## Shell completion

Palimpsest provides dynamic shell completion for `zsh`, `bash`, and `fish`. Completion follows the live CLI `argparse` tree dynamically and suppresses unrelated filesystem suggestions. The `palimpsest` executable (or active virtual environment) must be active and on your `PATH`. Installing the package does not silently modify shell configuration files.

### Current shell activation

```sh
# Zsh (macOS / Linux):
autoload -Uz compinit && compinit
eval "$(palimpsest completion zsh)"

# Bash:
source <(palimpsest completion bash)

# Fish:
palimpsest completion fish | source
```

### Persistent setup

To make completion persistent across terminal sessions, add the matching lines to your shell configuration:
The Zsh and Bash startup forms require `palimpsest` on `PATH` when the shell starts. With a project-local virtual environment, use the current-shell activation after `source .venv/bin/activate`.

```sh
# Zsh: add both lines to ~/.zshrc
autoload -Uz compinit && compinit
eval "$(palimpsest completion zsh)"

# Bash: add this guarded line to ~/.bashrc
if command -v palimpsest >/dev/null 2>&1; then source <(palimpsest completion bash); fi

# Fish:
mkdir -p ~/.config/fish/completions
palimpsest completion fish > ~/.config/fish/completions/palimpsest.fish
```

### Completion expectations

Pressing `<Tab><Tab>` completes commands, subcommands, and flags directly matching the live CLI tree:

- `palimpsest <Tab><Tab>` → suggests top-level command groups (`image`, `layer`, `bundle`, `build`, `run`, `compose`, `ui`, `store`, etc.)
- `palimpsest image <Tab><Tab>` → suggests subcommands (`inspect`, `history`, `rm`, `save`, `load`, `ls`, `pull`, `verify`, `import`, `push`)
- `palimpsest run --backend <Tab><Tab>` → suggests backend choices (`auto`, `kvm`, `lima-vz`, `libvirt-hvf`)

## Development

```sh
uv sync --frozen --extra dev
uv run ruff format --check .
uv run ruff check .
uv run python scripts/test_lanes.py list --check
uv run python scripts/test_lanes.py plan --changed HEAD
uv run python scripts/test_lanes.py run --changed HEAD --dry-run
uv build
```

Use the suggested functional lanes during development, rather than rerunning
every test after each edit. For example, `run oci-monitor` exercises monitor
contracts, and `run portable --shard 1/6` runs one deterministic sixth of the
portable test cases. `run full` remains an explicit broad regression command;
native KVM, privileged filesystem, BuildKit and Gate 2 proofs are separate
opt-in lanes, not substitutes for portable tests. See the
[test workflow](docs/testing.md) for selection rules and release checks.

## Project references

- [Implementation plan](IMPLEMENTATION_PLAN.md)
- [Installation details](docs/install.md)
- [Quickstart](docs/quickstart.md)
- [Compatibility notes](docs/compatibility.md)
- [Docker/OCI registry profiles](docs/registries.md)
- [BuildKit cache and block runtime workflow](docs/buildkit-block-workflow.md)
