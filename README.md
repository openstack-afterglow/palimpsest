# Palimpsest Local

`palimpsest-client` 0.2.3 is the root Python 3.11+ distribution for managing
Palimpsest boot images, SquashFS layers, OCI-layout bundles, and local layered
virtual machines. The Python module remains `palimpsest_local` and the CLI
remains `palimpsest`. Local artifacts, tags, run state, and build records live
under XDG state directories. The base package has no required Python runtime
dependencies; Linux KVM support is an opt-in extra.

## Status

- **macOS Apple Silicon:** supported default runtime through Lima 2.1+ and VZ (`lima-vz`); QEMU/libvirt Hypervisor.framework (`libvirt-hvf`) is experimental.
- **Linux:** supported libvirt/KVM runtime for conventional cloud-image VMs on `x86_64` and `aarch64`; the OCI-root runtime is narrower and supports Linux `x86_64`/`amd64` KVM only.
- **Declarative projects:** a strict `palimpsest.yml` workflow reconciles multiple VM services with dependencies, environment, typed cloud-init, persistent block volumes, networks, and Lima TCP forwarding.
- **Published root release:** [`palimpsest-client` 0.2.3 on PyPI](https://pypi.org/project/palimpsest-client/0.2.3/) and [GitHub Release `v0.2.3`](https://github.com/openstack-afterglow/palimpsest/releases/tag/v0.2.3). The independently versioned `palimpsest-hub` Python distribution remains 0.2.0.

## Install from PyPI

Requires Python 3.11 or newer:

```sh
python3.12 -m pip install "palimpsest-client==0.2.3"
palimpsest --version
```

For an isolated one-off invocation, `uvx --from palimpsest-client==0.2.3 palimpsest --version` also reports `0.2.3`.

## Install directly from GitHub

Palimpsest Local supports direct VCS installation from this repository. Python
3.11+, Git, and outbound HTTPS access to GitHub are required.

Install the CLI into an existing Python 3.11+ environment with pip:

```sh
python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

For a CLI that is independent of the current project and the host's system
Python, use an isolated uv tool environment:

```sh
uv python install 3.11
uv tool install --python 3.11 \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
palimpsest --version
```

To add Palimpsest as a uv project dependency, the project's complete
`requires-python` range must be 3.11 or newer. Initialize a new dependency-only
project explicitly instead of accepting `uv init`'s system-Python default:

```sh
uv python install 3.11
uv init --bare --python 3.11
uv python pin 3.11
uv add "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest"
uv run python -c "import importlib.metadata as m; print(m.version('palimpsest-client'))"
uv run palimpsest --version
```

For an existing project whose `pyproject.toml` says
`requires-python = ">=3.10"`, either raise that value to `">=3.11"` before
`uv add` or use the
isolated `uv tool install` path. `uv python pin` selects an interpreter but does
not narrow the project's supported range; `--frozen` also does not make Python
3.10 compatible. If a local project defines its own `palimpsest` command, the
metadata probe above distinguishes `palimpsest-client` from that command.

`uv add` records `palimpsest-client` as a Git source in `pyproject.toml` and
locks the resolved commit in `uv.lock`. The unpinned commands follow the
repository's default branch. For a reproducible installation, pin the full
40-character commit SHA that you reviewed:

```sh
PALIMPSEST_REF="FULL_40_CHARACTER_COMMIT_SHA"

python3.12 -m pip install \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"

uv add \
  "palimpsest-client @ git+https://github.com/openstack-afterglow/palimpsest@${PALIMPSEST_REF}"
```

Choose the distribution that matches the role:

```sh
# Linux libvirt/KVM or experimental macOS libvirt/HVF support.
python3.12 -m pip install --upgrade \
  "palimpsest-client[kvm] @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}"

# Standalone Hub API/worker package from the same reviewed ref.
python3.12 -m venv "$HOME/.venvs/palimpsest-hub"
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install --upgrade pip
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip install \
  "palimpsest-hub @ git+https://github.com/openstack-afterglow/palimpsest.git@${PALIMPSEST_REF}#subdirectory=hub"
"$HOME/.venvs/palimpsest-hub/bin/python" -m pip show palimpsest-hub
```

`palimpsest-client` provides the `palimpsest` CLI and has no required Python
runtime dependency. Its `[kvm]` extra adds `libvirt-python>=10.0.0` only; it does
not install QEMU, libvirt, firmware, host permissions, Lima, Docker/Buildx,
Skopeo, or other host executables. `palimpsest-hub` is a separate server
distribution with FastAPI, OpenStack, Redis, and SQL dependencies. Use the same
commit ref for Local and Hub to avoid source skew.

On Linux, select a private `XDG_STATE_HOME` for user-owned artifact workflows or
use the explicit administrator-managed account and storage provisioner. Package
installation never creates accounts, changes privileged groups, writes sudoers
policy, or initializes mutable state.

See the [short install guide](install.md), [detailed package/platform/configuration
guide](docs/install.md), [command workflows](docs/cli/workflows.md), and
[generated syntax reference](docs/cli/reference.md). Repository contributors use
the checkout-specific commands under [Development](#development); those are not
the end-user installation path.

The root wheel also ships the `palimpsest` Kolla-Ansible role at
`share/kolla-ansible/ansible/roles/palimpsest`. It does not declare or install
Kolla-Ansible, Ansible, Hub, or other server dependencies; deployments must pin
Kolla-Ansible independently. This source role defaults to Hub image tag `0.2.0`,
while source builds use the configured checkout SHA. Verify both Hub images
before deploying this default; the root package version does not automatically
set the independent Hub Python distribution version.

A repository tag labels both `ghcr.io/openstack-afterglow/palimpsest-hub-api`
and `ghcr.io/openstack-afterglow/palimpsest-hub-worker` with tags derived from
that repository tag, not from the independent Hub wheel version. The
`v0.2.3` [root release workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630018)
passed artifact verification and native stage-1 KVM proof, published through the
`palimpsest-client` trusted publisher, and created the GitHub Release. The
separate [Hub image workflow](https://github.com/openstack-afterglow/palimpsest/actions/runs/36081630008)
published API and worker images for that tag. A release does not by itself
establish that an operator has deployed those images.

## Hub configuration & Standalone Service

Palimpsest Hub runs as a standalone FastAPI service on port 8020 using OpenStack Keystone token authentication (`X-Auth-Token` and optional `X-Project-Id`). Open `/app` for its same-origin web console: use a project-scoped token to upload, search, and download artifacts; server-side Palimpsestfile builds additionally require a Keystone system administrator.

Hub's native `/v1` API stores Palimpsest boot images, SquashFS runtime blocks, bundles, and BuildKit cache archives. `POST /v1/builds` queues an isolated VM build; `GET /v1/builds` and `GET /v1/builds/{id}` report its project-scoped state. A separate Linux KVM host worker consumes jobs and registers successful outputs as private layers. The API container does not execute recipes. Hub is not a Docker/OCI `/v2` registry; ordinary OCI image commands use a separately configured registry profile.

### Entrypoints & Docker Targets

- **Hub API:** `uvicorn palimpsest_hub.main:app --host 0.0.0.0 --port 8020` (Docker target `palimpsest-hub-api`; `/app` serves the web console).
- **Async Export Worker:** `python -m palimpsest_hub.worker` (Docker target `palimpsest-hub-worker`).
- **Isolated Build Worker:** `palimpsest-hub-build-worker` on a separately provisioned Linux `/dev/kvm` host, with `PALIMPSEST_HUB_BUILDER_PYTHON` set to an absolute interpreter containing `palimpsest-client[kvm]` at the same reviewed source ref. Share its SQL database and Hub blob path with the API; do not place the worker or a Docker socket inside the API container.
- **Database Bootstrap:** `python -m palimpsest_hub.bootstrap` creates the destination schema.
- **Data Migration:** `python -m palimpsest_hub.migrate --source-url "$SOURCE_DATABASE_URL" --destination-url "$DESTINATION_DATABASE_URL"` copies source tables into an empty initialized destination; it is not bootstrap.

The canonical Hub container build uses the repository root as context and `docker/hub/Dockerfile` as its Dockerfile:

```sh
docker build --file docker/hub/Dockerfile --target palimpsest-hub-api .
docker build --file docker/hub/Dockerfile --target palimpsest-hub-worker .
```

### Client Hub Configuration

Hub CLI commands use a base URL (`PALIMPSEST_URL` or `--url`) and a Keystone token from `PALIMPSEST_TOKEN` to call native `/v1` Hub endpoints over `X-Auth-Token`. Keep the token in a secret manager or process environment; do not paste a token into documentation, shell history, or state files.

```sh
export PALIMPSEST_URL="http://hub.example:8020"
# Set PALIMPSEST_TOKEN from your secret-management mechanism.

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
See also the source-based [Docker Hub intake analysis](docs/docker-hub-intake-analysis.md)
for the current local-archive boundary and unqualified gaps.

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

Local artifacts live below the selected state root. The unconfigured Linux
default is `/var/lib/palimpsest`; an explicit `XDG_STATE_HOME` selects
`${XDG_STATE_HOME}/palimpsest`, and other platforms default to
`~/.local/state/palimpsest`:

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

For a local standard OCI archive/layout, the experimental OCI-root converter
can verify and materialize its layers without Docker:

```sh
# Anonymous TLS acquisition requires Skopeo 1.13+ and an explicit registry.
palimpsest oci pull quay.io/example/application:v1 --output ./image.oci.tar
palimpsest oci materialize ./image.oci.tar --output ./materialization.json
# If the local index lists multiple roots, choose one explicitly:
palimpsest oci materialize ./image-layout --manifest 'sha256:<root-digest>'
```

This requires the qualified SquashFS packer (`/usr/bin/mksquashfs` by default)
and currently supports `linux/amd64`. It produces verified layer artifacts,
not a running VM by itself. Public local OCI-root `run/-d` and the protected-root
Gate 2 have separate qualified proofs on Linux/x86_64 KVM; see the
[public-runtime roadmap](docs/oci-public-runtime-roadmap.md). Docker Hub
references are not accepted directly by OCI-root `run`; `oci pull` first
terminates acquisition at the verified local archive boundary, while the Docker
`pull` wrapper does not bridge into that runtime. See the
[anonymous registry intake contract](docs/registry-intake.md) and
[Docker Hub compatibility](docs/oci-docker-hub-compatibility.md) for the
digest-preserving local-archive workflow and its real-image verification scope.

OCI-root runs accept an explicit `--user USER[:GROUP]` (name or canonical
numeric ID), for example `palimpsest run ./redis.oci.tar --name redis-demo
--user redis -d`. This changes only the launch identity, not the source image,
entrypoint or arguments. Without the option, the image's configured user is
unchanged. PID 1 protections and the capabilityless workload policy remain
enabled; this is not privileged mode or general Docker compatibility. See
[explicit OCI run users](docs/oci-run-user.md) for the contract and limits.

OCI-root `exec` accepts `--timeout SECONDS` (integer 1–600, default 30) to set
the guest execution deadline for that one command; the option is rejected for
cloud-image runs. Retry, locking, privilege, cleanup and the 64 KiB output
limit are unchanged, and a longer deadline does not change host monitor or run
lock bounds.

OCI-root runs select one virtual network with `--network nat|host-only|none`
and publish guest ports with the repeatable
`--publish [HOST_IP:]HOST_PORT:GUEST_PORT[/tcp|/udp]` (`-p`):

```sh
palimpsest run ./service.oci.tar --name api -d \
  --network nat --publish 127.0.0.1:18080:8080
```

Omitting `--network` now means `nat`: the guest gets a private `10.0.2.15/24`
address with outbound NAT and DNS. This is an explicit breaking change from the
previous no-NIC default, so use `--network none` to keep the old isolation.
`host-only` keeps the private address but blocks every outbound path. Inbound
traffic only reaches published ports, host addresses default to `127.0.0.1`,
and external exposure requires writing `0.0.0.0` explicitly. Palimpsest creates
no libvirt network, bridge, firewall rule or DNS service: the NIC and every
host listener belong to the VM's own QEMU process. `palimpsest ps` includes
configured publications in its `PORTS` column, `palimpsest inspect NAME` emits
them as typed JSON, and `palimpsest oci network NAME` performs the stronger
OCI-specific committed-plan verification and external-exposure classification.
Configured publications on a stopped run are not live-listener claims. See the
[network contract](docs/oci-network.md) for limits, including no IPv6, no
privileged host ports and no VM-to-VM network. IPv6 host publication and a
shared VM network are separately gated future contracts; neither is implied by
the current per-VM network modes.

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

The experimental QEMU/libvirt alternative requires the host tools and `[kvm]`
extra in [the install guide](docs/install.md). It is opt-in, runs conventional
ARM64 cloud images through `qemu:///session`, and leaves Lima/VZ as the default:

```sh
virsh -c qemu:///session list --all
palimpsest run sha256:<image-digest> --name ubuntu-hvf \
  --memory 2048 --vcpus 2 --backend libvirt-hvf
palimpsest exec ubuntu-hvf -- uname -m
palimpsest stop ubuntu-hvf
palimpsest rm ubuntu-hvf --volumes
```

This does not enable the Linux-only OCI-root runtime on macOS.

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
- `palimpsest-client[kvm]`

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

The branch PR's native KVM stage-1 gate verifies OCI-root guest root switching
and PID 1; it does not prove the revised conventional cloud-image readiness
path after merge. The serial-bound first-boot and cloud-init-disabled reboot
changes still need a native post-merge cloud-image boot/reboot check before
being treated as a deployed runtime qualification. Release builds and
deployment are outside this documentation update.

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
palimpsest oci materialize
palimpsest build
palimpsest run
palimpsest start
palimpsest compose config|up|down|ps|logs|exec|stop|port
palimpsest ps|inspect|logs|shell|exec|stop|rm|commit
palimpsest ui                                # web management dashboard
palimpsest store show|ls|rm|move|set         # storage state & artifact management
palimpsest completion zsh|bash|fish          # shell completion generator
```

Use `palimpsest <command> --help` for exact arguments. The checked generated
[CLI reference](docs/cli/README.md) records the complete command tree, and the
[command workflow catalog](docs/cli/workflows.md) documents the per-command
order, configuration, and platform limits with Archify workflow diagrams.

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

### Resume ongoing development

Start with the [development handoff](docs/development-handoff.md) for the
current source, verification results, approval boundaries, and ordered next
tasks. Read [AGENTS.md](AGENTS.md) for contributor rules and
[ARCHITECTURE.md](ARCHITECTURE.md) for the source contracts before changing code.
[agent.md](agent.md) is a short entrypoint to those documents, not a separate
policy.

The 2026-09-23 Hub hardening commit is published at
`60fa42febfb8acd9af04e87c9905a4e2a1841d13`; the handoff records CI and
package evidence. Publication occurred without the explicit approval required
by AGENTS.md and does not authorize further remote or privileged operations.
A separately approved 2026-09-24 dedicated Nova KVM probe verified one native
two-layer/two-RUN Hub build. A subsequent, separately approved dedicated KVM
probe verified actual guest timeout and build-worker SIGKILL/restart recovery;
its owned VM, volume and security group were removed. The current layer-detail
response fix has portable HTTP proof, not native detail-response readback.
Neither KVM probe qualifies production account separation or power-loss recovery.
See the handoff for distinct source snapshots, and obtain specific approval
before publication, shared-server mutation, or another installation.

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

- [Living architecture](ARCHITECTURE.md) — current source map, runtime boundaries, and update procedure

- [Implementation plan](IMPLEMENTATION_PLAN.md)
- [Installation details](docs/install.md)
- [Quickstart](docs/quickstart.md)
- [Compatibility notes](docs/compatibility.md)
- [Docker/OCI registry profiles](docs/registries.md)
- [BuildKit cache and block runtime workflow](docs/buildkit-block-workflow.md)
