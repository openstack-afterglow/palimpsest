# Palimpsest Local

`palimpsest-local` is a Python 3.12+ CLI for managing Palimpsest boot images, SquashFS layers, OCI-layout bundles, and local layered virtual machines.

It provides the `palimpsest` command and keeps local artifacts, tags, run state, and build records under XDG state directories. The core package has no required Python runtime dependencies; Linux KVM support is an optional extra.

## Status

- **macOS Apple Silicon:** runnable prototype using Lima's VZ backend and ARM64 Ubuntu cloud images.
- **Linux x86_64:** KVM/libvirt runtime implementation is available, but the `0.1.0` release and Afterglow package cutover still require a clean-host KVM integration proof.
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
builds/      build records and console output
tags/        local layer tags
transfers/   Hub transfer progress
```

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

## Command groups

```text
palimpsest image  ls|pull|verify|import|push
palimpsest layer  ls|pull|pack|push
palimpsest bundle pull|verify
palimpsest build
palimpsest run
palimpsest ps|inspect|logs|shell|exec|stop|rm|commit
```

Use `palimpsest <command> --help` for exact arguments.

## Development

```sh
uv sync --frozen --extra dev
uv run ruff format --check .
uv run ruff check .
uv run python -m pytest tests/unit tests/integration -q
uv build
```

## Project references

- [Implementation plan](IMPLEMENTATION_PLAN.md)
- [Installation details](docs/install.md)
- [Quickstart](docs/quickstart.md)
- [Compatibility notes](docs/compatibility.md)
