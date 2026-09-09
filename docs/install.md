# Installing Palimpsest Local

`palimpsest-local` is a standalone Python 3.12+ library and CLI (`palimpsest`) for managing verified local OCI artifacts, building SquashFS layers in disposable KVM guests, and orchestrating layered QEMU/libvirt virtual machines for Afterglow Palimpsest.

---

## System & Host Requirements

Running local virtual machines is supported across Linux x86_64, Linux aarch64, and macOS Apple Silicon.

### 1. Hardware & Operating System
- **Linux x86_64:** Production host with `/dev/kvm` hardware acceleration, `qemu-system-x86_64`, `libvirtd`, and `qemu:///system`.
- **Linux aarch64:** Production host with `/dev/kvm` hardware acceleration, `qemu-system-aarch64`, `virt` machine, EFI firmware (e.g., `qemu-efi-aarch64` / `AAVMF`), `libvirtd`, and `qemu:///system`.
- **macOS arm64 (Default):** Apple Silicon host using Lima 2.1+ and the VZ backend (`lima-vz`).
- **macOS arm64 (Experimental):** Apple Silicon host using QEMU/libvirt with Hypervisor.framework acceleration (`libvirt-hvf`). Requires `brew install libvirt qemu`. SLIRP user-mode networking (`hostfwd`) is used because libvirt network drivers are unsupported on macOS; `--network` must be `none` or `default`.
- **Python:** Python `>= 3.12`.

### 2. Host System Packages & Hardware Verification

#### Linux (x86_64 & aarch64)
- **QEMU / libvirt:** `qemu-system-x86_64` (x86_64) or `qemu-system-aarch64` (aarch64), `libvirtd` (or `virtqemud`), `libvirt-clients`. User must have `/dev/kvm` access and permission to connect to `qemu:///system` with the `default` network.
- **Disk Utilities:** `qemu-img` (qcow2 overlays), `mkfs.ext4` (project volumes), `cloud-localds` (NoCloud seed ISOs), `mksquashfs` and `unsquashfs` (`squashfs-tools` 4.5+ with `zstd`).
- **SSH Client:** OpenSSH `ssh` and `scp` binaries.

**Linux aarch64 Hardware Verification:**
```bash
# Import an arm64 cloud image and run a domain using the default network
DIGEST=$(palimpsest image import ./ubuntu-24.04-server-cloudimg-arm64.img --disk-format qcow2 --arch aarch64 --os-variant ubuntu-24.04)
palimpsest run "$DIGEST" --name arm-probe --network default
palimpsest exec arm-probe -- uname -m   # Verify stdout prints: aarch64
virsh dumpxml arm-probe                # Verify XML contains machine='virt' and EFI firmware
```

#### macOS (arm64)
- **Lima (Default backend):** Lima 2.1 or newer for persistent `limactl disk` volumes, `additionalDisks`, and static TCP port forwarding.
  ```bash
  brew install lima
  limactl --version
  ```
- **libvirt-hvf (Experimental backend):** Homebrew `libvirt` and `qemu` for Hypervisor.framework acceleration:
  ```bash
  brew install libvirt qemu
  brew services start libvirt
  ```

**macOS libvirt-hvf Hardware Verification:**
```bash
# Start libvirt service, import image, and run with --backend libvirt-hvf
brew services start libvirt
DIGEST=$(palimpsest image import ./ubuntu-24.04-server-cloudimg-arm64.img --disk-format qcow2 --arch aarch64 --os-variant ubuntu-24.04)
palimpsest run "$DIGEST" --name hvf-probe --backend libvirt-hvf --network none
virsh -c qemu:///session dumpxml hvf-probe  # Verify domain type='hvf' and -netdev user,hostfwd=...
palimpsest exec hvf-probe -- uname -m      # Verify guest execution succeeds over local port
```
### 3. BuildKit Requirements for Dockerfile Builds

The Dockerfile frontend invokes `docker buildx build`; install Docker Engine/Desktop with the Buildx plugin and a running BuildKit-backed builder. The Docker CLI is also the execution backend for Palimpsest's Docker-compatible image commands and `palimpsest docker ...` passthrough. BuildKit cache records remain separate from the SquashFS runtime block attached to a VM.

Palimpsest exports an OCI archive, so the currently selected builder must use an OCI-export-capable `docker-container`, `kubernetes`, or `remote` driver. The default `docker` driver does not support the OCI exporter. Before each build, Palimpsest runs the read-only `docker buildx inspect` command and fails with setup guidance when the selected driver is incompatible; it never creates, bootstraps, or changes the selected builder.

To keep Docker Desktop's selected builder unchanged, create a separate builder and select it only for the Palimpsest process:

```bash
docker buildx create --name palimpsest --driver docker-container
docker buildx inspect --builder palimpsest --bootstrap
BUILDX_BUILDER=palimpsest palimpsest build . --frontend dockerfile -f Dockerfile --tag demo
```

An existing `kubernetes` or `remote` builder can be supplied through `BUILDX_BUILDER` in the same way. See Docker's [OCI exporter](https://docs.docker.com/build/exporters/oci-docker/) and [build driver](https://docs.docker.com/build/builders/drivers/) documentation for the upstream capability matrix.

Registry profiles can describe BuildKit mirrors, private CA files, and transport policy. Generate a daemon configuration and apply it when explicitly creating the dedicated builder:

```bash
palimpsest registry buildkit-config --output ./buildkitd.toml

docker buildx create \
  --name palimpsest \
  --driver docker-container \
  --buildkitd-config ./buildkitd.toml
docker buildx inspect --builder palimpsest --bootstrap
```

Generating `buildkitd.toml` does not modify an existing builder. Mirror, CA, `plain_http`, and TLS-skip settings take effect only after that file is supplied to a new or otherwise explicitly configured BuildKit daemon. The generated file contains no credentials and does not change Docker Engine/Desktop's pull/push trust store, insecure-registry list, or daemon mirror configuration.

For an online build, configure `PALIMPSEST_URL` and `PALIMPSEST_TOKEN`. The resolver consults Hub before executing a cache miss and treats Hub authentication, transport, metadata, and digest errors as fatal.

For strict `--offline` builds, provision all of the following locally before starting:

- a dedicated, already-bootstrapped single-node local Buildx builder created with `--driver docker-container --driver-opt network=none`;
- a digest-pinned OCI layout for each Dockerfile `FROM` alias;
- the immutable qcow2/raw runtime base in the local content store;
- any BuildKit cache intended for reuse;
- a local build context.

Create and bootstrap the offline builder while the required BuildKit image is still available, then select it only for the Palimpsest process:

```bash
docker buildx create --name palimpsest-offline \
  --driver docker-container \
  --driver-opt network=none
docker buildx inspect --builder palimpsest-offline --bootstrap
BUILDX_BUILDER=palimpsest-offline palimpsest build . \
  --frontend dockerfile --offline --network none --tag demo
```

Offline mode verifies that the builder has exactly one node, that its endpoint matches the current local Unix/named-pipe Docker context, and that its sole running BuildKit container's Docker network mode is `none`. It also enforces `--network none` for build steps, rejects remote or dynamic Dockerfile sources, verifies the transitive blobs of each pinned OCI layout, and does not load Palimpsest registry profiles, invoke registry authentication, or use Hub, a remote registry, or a remote cache exporter. Docker may still read its selected `DOCKER_CONFIG` to locate the local context and builder. It rejects `--registry`, `--pull`, `--push`, `--runtime-push`, and external `--cache-from`/`--cache-to` options. The command and verification contract are in [BuildKit Cache and Block Runtime Workflow](buildkit-block-workflow.md).

---

## Installation

### Base Package (Stdlib-only Core)
The base distribution has zero required runtime Python dependencies. It provides the full CLI for artifact verification, bundle management, layer packing, and Hub interaction:

```bash
pip install .
# or using uv:
uv pip install .
```

### libvirt Runtime Extra (`[kvm]`)
To enable VM execution on **Linux x86_64 and aarch64** using libvirt/KVM, install with the `kvm` optional dependency extra:

```bash
pip install '.[kvm]'
# or using uv:
uv tool install 'palimpsest-local[kvm]'
```

The `[kvm]` extra installs `libvirt-python>=10.0.0`. `libvirt-python` is imported dynamically only during libvirt domain operations (`palimpsest_local.kvm`), allowing pure artifact workflows to function in environments without libvirt installed.

**macOS Apple Silicon** VM execution uses Lima/VZ by default, which is included in the base package and needs no extra. The experimental `libvirt-hvf` backend does use libvirt, so it requires this extra plus Homebrew `libvirt`/`qemu`. See [System & Host Requirements](#system--host-requirements) for setup details and the [VM workflow guide](vm-workflow.md) for the end-to-end command reference.

### Development Installation
For running the test suite and linters:

```bash
uv sync --extra dev --extra kvm
```

### Dedicated Linux management account

For a single-operator system installation, provision the fixed management
identity and storage roots with:

```bash
sudo /opt/palimpsest/bin/python -I -m palimpsest_local.linux_install
```

Replace `/opt/palimpsest/bin/python` with the administrator-owned interpreter
where the package is installed. Do not run this root command from an editable
user checkout or an untrusted current directory.

The command creates the no-login `palimpsest` system user and primary group,
then creates `/var/lib/palimpsest` and `/var/log/palimpsest` with owner and group
`palimpsest:palimpsest` and mode `0700`. It is idempotent only when existing
accounts and directories match that exact contract. It rejects symlinks,
conflicting ownership or modes, and unsafe writable ancestors; it does not
recursively change ownership, migrate existing data, or install sudo rules.

Run Palimpsest management operations as the dedicated `palimpsest` service UID
so the existing owner-only state and inode checks remain effective. Group
ownership identifies the managed service boundary; mode `0700` means it is not
direct multi-user write access. The installer does not add the account to
Docker, libvirt, KVM, or any other privileged group. Those runtime permissions
must be reviewed and granted separately for the intended deployment.

For example, with the package installed in the trusted path above, run a normal
management command under the dedicated identity with a clean home and without
inherited state-root overrides:

```bash
sudo -H -u palimpsest env -u PALIMPSEST_STATE_HOME -u PALIMPSEST_LOG_HOME \
  -u XDG_STATE_HOME -u XDG_CONFIG_HOME \
  /opt/palimpsest/bin/python -I -m palimpsest_local.cli store show
```

### Shell Completion Setup
Palimpsest includes dynamic shell completion scripts for `zsh`, `bash`, and `fish`. Completion candidates follow the live CLI `argparse` tree dynamically and suppress unrelated filesystem suggestions.

The `palimpsest` executable (or active virtual environment) must be active and on your `PATH`. Installing the package does not silently modify shell configuration files.
The Zsh and Bash startup forms require `palimpsest` on `PATH` when the shell starts. For a project-local virtual environment, run the current-shell activation after `source .venv/bin/activate`.

#### Zsh (macOS / Linux Primary)
```bash
# Current shell session:
autoload -Uz compinit && compinit
eval "$(palimpsest completion zsh)"

# Persistent setup: add both lines to ~/.zshrc
autoload -Uz compinit && compinit
eval "$(palimpsest completion zsh)"
```

#### Bash & Fish Alternatives
```bash
# Bash - Current shell session:
source <(palimpsest completion bash)

# Bash - Persistent setup: add this guarded line to ~/.bashrc
if command -v palimpsest >/dev/null 2>&1; then source <(palimpsest completion bash); fi

# Fish - Current shell session:
palimpsest completion fish | source

# Fish - Persistent setup:
mkdir -p ~/.config/fish/completions
palimpsest completion fish > ~/.config/fish/completions/palimpsest.fish
```

#### Expected Tab Completion Behavior
- `palimpsest <Tab><Tab>` → lists top-level command groups (`image`, `layer`, `bundle`, `build`, `run`, `compose`, `ui`, `store`, etc.)
- `palimpsest image <Tab><Tab>` → lists `image` subcommands (`inspect`, `history`, `rm`, `save`, `load`, `ls`, `pull`, `verify`, `import`, `push`)
- `palimpsest run --backend <Tab><Tab>` → lists valid choices (`auto`, `kvm`, `lima-vz`, `libvirt-hvf`)

---
## Standalone Hub bootstrap and migration

The Hub package is an independent service under `hub/`. Bootstrap creates tables in an empty destination; data migration copies supported rows from a different source database and does not copy filesystem blobs. Keep these operations separate:

```bash
cd hub
uv sync --frozen --extra dev
uv run palimpsest-hub-bootstrap
uv run palimpsest-hub-migrate-data \
  --source-url "$SOURCE_DATABASE_URL" \
  --destination-url "$DESTINATION_DATABASE_URL"
```

Run `uv run palimpsest-hub` for the API on port `8020` and `uv run palimpsest-hub-worker` for asynchronous Glance exports. Configure the Hub's database, local blob path, Redis URL, and OpenStack settings through the deployment environment; do not commit those values. The API authenticates project-scoped Keystone tokens in `X-Auth-Token`.

See [Living architecture](../ARCHITECTURE.md) for the local/Hub boundary and the exact maintenance check.


## Package vs. KVM-Extra Boundary

| Capability | Base Package (`palimpsest-local`) | KVM Extra (`palimpsest-local[kvm]`) |
|---|---|---|
| Python dependencies | None (stdlib only) | `libvirt-python>=10.0.0` |
| Platform support | Linux, macOS, BSD | Linux x86_64 with `/dev/kvm` |
| Commands available | Hub `image`/`layer`/`bundle`, registry profiles, Docker-compatible image commands, Dockerfile/Buildx `build` | `run`, `compose`, Palimpsestfile guest `build`, `commit`, `ps`, `inspect`, `logs`, `shell`, `exec`, `stop`, `rm` |
| Primary use case | Hub interaction, CI artifact verification, Dockerfile builds | Local VM lifecycle, guest-delta building, and committing |

Afterglow API containers depend on `palimpsest-local==0.1.0` without the `[kvm]` extra, keeping container images lightweight and free of C/libvirt system library overhead.

---

## Environment Variables

| Variable | Description | Default / Fallback |
|---|---|---|
| `PALIMPSEST_URL` | Base URL of the native Palimpsest Hub `/v1` API | `--url` CLI argument |
| `PALIMPSEST_TOKEN` | Project-scoped Keystone token sent in `X-Auth-Token` | **Required for Hub requests**; supply through a secret manager or process environment, never in docs/state |
| `PALIMPSEST_REGISTRY` | Registry profile used for unqualified image references | `default` in `registries.toml` |
| `PALIMPSEST_LOG_HOME` | Explicit owner-private host command journal root | Linux: `/var/log/palimpsest`; other platforms: journaling disabled when unset |
| `DOCKER_CONFIG` | Existing Docker configuration and credential-helper directory | `~/.docker` |
| `XDG_CONFIG_HOME` | Configuration root directory | `~/.config` |
| `XDG_STATE_HOME` | Explicit per-user state and store parent | Linux: `/var/lib/palimpsest` when unset; other platforms: `~/.local/state/palimpsest` |

---

## XDG Paths & Permission Expectations

All configuration, state, and cryptographic key directories are strictly owner-only.

### Configuration
- Path: `${XDG_CONFIG_HOME:-~/.config}/palimpsest/config.toml`
- Permissions: Directory `0700`, File `0600`.

```toml
[hub]
url = "https://hub.example.invalid"
```

`--url` takes precedence over `PALIMPSEST_URL`, which takes precedence over this non-secret configuration value.

### Registry profiles

- Path: `${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml`
- Permissions: Directory `0700`, File `0600`.
- Contents: endpoints, namespaces, mirrors, absolute CA paths, transport flags, and optional external BuildKit cache definitions. Secrets and inline credentials are rejected.

The built-in `docker` profile uses `docker.io` with namespace `library`. Configure and select an external registry without editing TOML manually:

```bash
palimpsest registry add corp registry.example.com \
  --namespace platform \
  --default
palimpsest registry inspect corp

palimpsest login --registry corp
```

Registry selection for unqualified image references is: an explicit registry in the reference, the command's `--registry`, `PALIMPSEST_REGISTRY`, then the configured default. Credentials stay in Docker's existing `DOCKER_CONFIG`/`~/.docker` credential store. Palimpsest never writes them to `registries.toml`; for automation, pipe a password to `palimpsest login --username USER --password-stdin`.

See [Docker/OCI Registry Profiles](registries.md) for the full command and BuildKit configuration contract.

### State & Store Layout
Root directory (permissions `0700`):

- Linux, when no environment or config override is present: `/var/lib/palimpsest/`.
- With an explicit `XDG_STATE_HOME`: `${XDG_STATE_HOME}/palimpsest/`.
- Other platforms without an override: `~/.local/state/palimpsest/`.

`PALIMPSEST_STATE_HOME` and configured `storage.state_root` take precedence over
these defaults. Palimpsest does not create the `/var/lib` parent, migrate an
existing state root, or delete legacy data as part of default resolution. A
single-operator Linux installation must preprovision `/var/lib/palimpsest` for
the Palimpsest account. See [Linux storage and logging rollout](linux-storage-logging.md)
for the asset boundary and the implemented fail-open `/var/log/palimpsest`
host command journal. Raw VM consoles remain pinned below their run state and
are not moved into this journal.
If `~/.local/state/palimpsest` already exists, resolution fails closed instead
of hiding it; set `XDG_STATE_HOME=$HOME/.local/state` to keep selecting it.

```text
<selected-state-root>/
├── store/                          # Content-addressed artifact store (dir: 0700)
│   └── blobs/sha256/<hex>          # Verified immutable blob files (file: 0444)
├── runs/                           # Local VM run ledgers (dir: 0700)
│   └── <name>/                     # Per-run directory (dir: 0700)
│       ├── owner.json              # Schema v1 ownership & UUID record (file: 0600)
│       ├── state.json              # Atomic lifecycle state ledger (file: 0600)
│       ├── overlay.qcow2           # Writable qcow2 overlay for vda (file: 0600)
│       ├── seed.iso                # NoCloud seed ISO configuration disk (file: 0600)
│       ├── console.log             # Domain serial console log (file: 0600)
│       └── ssh/                    # Owner-only SSH directory (dir: 0700)
│           ├── id_ed25519          # Client private key (file: 0600)
│           ├── id_ed25519.pub      # Client public key (file: 0644)
│           ├── host_id_ed25519     # Guest host private key (file: 0600)
│           └── known_hosts         # Strict SSH host key pin (file: 0600)
├── projects/                       # palimpsest.yml ownership/reconcile ledgers
│   └── <project>/state.json        # service UUID/backend/config bindings (0600)
├── volumes/                        # persistent project block storage
│   └── <project>/                  # KVM raw ext4 images or Lima owner receipts
├── locks/                          # Exclusive process lock files (dir: 0700)
│   └── <name>.lock                 # Run lock file (file: 0600)
├── transfers/                      # Resumable upload session checkpoints (dir: 0700)
│   └── <digest_hex>.json           # Transfer metadata record (file: 0600)
├── tags/                           # Local runtime-layer tag records (dir: 0700)
│   └── <tag>.json                  # Immutable schema v1 tag record (file: 0600)
├── build-cache/                    # BuildKit local-exporter cache (dir: 0700)
│   └── <scope>/
│       ├── current.json            # Atomic pointer to the committed generation
│       └── generations/<build-id>/ # Complete locally exported cache generation
└── builds/                         # Disposable build records (dir: 0700)
    └── <build-id>/                 # Per-build log, metadata, cache archive, and ledger
        └── record.json             # Legacy schema v1 or BuildKit schema v2 receipt (file: 0600)
```

---

## Local Management GUI & Storage Administration

### 1. Web Management Dashboard (`palimpsest ui`)
Serve a local dashboard web UI on `127.0.0.1`:

```bash
# Start server on an ephemeral port and open browser
palimpsest ui

# Specify a custom port without opening browser
palimpsest ui --port 8765 --no-browser
```

The web dashboard provides four tabs:
-+ **VMs:** View active/stopped VMs, hypervisor backend badges, layer breakdown, resource allocation, SSH endpoints, and live console logs.
-+ **Artifacts:** Inspect images and SquashFS layers, view size and reference counts, import cloud images, and delete unreferenced blobs.
-+ **Builds:** History of Palimpsestfile and BuildKit builds, build duration, execution phase timings, and build logs.
-+ **Storage:** Current state root path, storage source (`env`, `config`, or `default`), per-directory byte breakdown, free disk space, and relocation controls.

### 2. CLI Storage Management (`palimpsest store`)
Manage content-addressed artifacts and state root configuration:

```bash
# Show state root path, config source, and directory space utilization
palimpsest store show --format json

# List stored images and SquashFS layers
palimpsest store ls --kind all

# Remove an unreferenced artifact (fails closed if referenced by active VM runs or projects)
palimpsest store rm sha256:<digest>

# Relocate state root to a new directory (safeguard: requires no active runs or projects)
palimpsest store move --to /mnt/fast-storage/palimpsest

# Point config to an existing state root directory
palimpsest store set --to /mnt/fast-storage/palimpsest
```

**Storage Relocation Safeguards & Lima Notice:**
-+ `store move` verifies that destination path is absolute and that `runs/` and `projects/` ledgers contain no active instances before copying.
-+ **Lima-managed disks non-relocation notice:** `limactl disk` volumes are managed by Lima (`~/.lima`) and are **not** relocated when moving the Palimpsest state root.

---

## KVM Release Gate Notice

> **Mandatory Release Gate:** Standalone release `v0.1.0` on PyPI and cutover of Afterglow dependencies remain **blocked** pending full integration proof on a physical Linux x86_64 KVM host (`pytest -m kvm`). The implementation is verified in pure unit and mock integration environments, but final publication requires end-to-end execution proof on hardware virtualization.
