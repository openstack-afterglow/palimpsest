# Installing Palimpsest Local

`palimpsest-local` is a standalone Python 3.12+ library and CLI (`palimpsest`) for managing verified local OCI artifacts, building SquashFS layers in disposable KVM guests, and orchestrating layered QEMU/libvirt virtual machines for Afterglow Palimpsest.

---

## System & Host Requirements

Running local KVM virtual machines requires a Linux x86_64 host with hardware virtualization enabled.

### 1. Hardware & Operating System
- **Architecture:** `x86_64` (Linux KVM execution host). *Note: macOS and non-x86_64 platforms support artifact inspection, pulling, verification, and layer packing, but cannot execute KVM domains.*
- **Kernel:** Linux 5.4+ with KVM modules loaded (`/dev/kvm` accessible to the user running `palimpsest`).
- **Python:** Python `>= 3.12`.

### 2. Host System Packages & Daemon Prerequisites
- **QEMU / libvirt:** `qemu-system-x86_64`, `libvirtd` (or `virtqemud`), `libvirt-clients`. The user must have permission to connect to `qemu:///system` and use the `default` libvirt network.
- **Disk Utilities:**
  - `qemu-img` (provided by `qemu-utils`) for qcow2 overlay creation and validation.
  - `cloud-localds` (provided by `cloud-image-utils`) for NoCloud seed ISO generation.
  - `mksquashfs` (provided by `squashfs-tools` with `zstd` support) for packing layer filesystems.
- **SSH Client:** OpenSSH `ssh` and `scp` binaries for guest readiness checks, `shell`, `exec`, and layer commit extraction.

---

## Installation

### Base Package (Stdlib-only Core)
The base distribution has zero required runtime Python dependencies. It provides the full CLI for artifact verification, bundle management, layer packing, and Hub interaction:

```bash
pip install .
# or using uv:
uv pip install .
```

### KVM Runtime Extra (`[kvm]`)
To enable VM execution (`run`, `build`, `commit`, `shell`, `exec`, `stop`, `rm`, `ps`, `inspect`), install with the `kvm` optional dependency extra:

```bash
pip install '.[kvm]'
# or using uv:
uv tool install 'palimpsest-local[kvm]'
```

The `[kvm]` extra installs `libvirt-python>=10.0.0`. `libvirt-python` is imported dynamically only during KVM domain operations (`palimpsest_local.kvm`), allowing pure artifact workflows to function in environments without libvirt installed.

### Development Installation
For running the test suite and linters:

```bash
uv sync --extra dev --extra kvm
```

---

## Package vs. KVM-Extra Boundary

| Capability | Base Package (`palimpsest-local`) | KVM Extra (`palimpsest-local[kvm]`) |
|---|---|---|
| Python dependencies | None (stdlib only) | `libvirt-python>=10.0.0` |
| Platform support | Linux, macOS, BSD | Linux x86_64 with `/dev/kvm` |
| Commands available | `image`, `layer`, `bundle` | `run`, `build`, `commit`, `ps`, `inspect`, `logs`, `shell`, `exec`, `stop`, `rm` |
| Primary use case | Hub interaction, CI artifact verification, Afterglow API integration | Local VM lifecycle, local layer building & committing |

Afterglow API containers depend on `palimpsest-local==0.1.0` without the `[kvm]` extra, keeping container images lightweight and free of C/libvirt system library overhead.

---

## Environment Variables

| Variable | Description | Default / Fallback |
|---|---|---|
| `PALIMPSEST_URL` | Base URL of Afterglow Hub API (e.g. `https://hub.afterglow.dev`) | `--url` CLI argument |
| `PALIMPSEST_TOKEN` | Bearer token for Hub authentication | **Required for Hub requests** (No CLI flag exists, preventing token leaks in `ps` output) |
| `XDG_CONFIG_HOME` | Configuration root directory | `~/.config` |
| `XDG_STATE_HOME` | Local state and store root directory | `~/.local/state` |

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

### State & Store Layout
Root directory: `${XDG_STATE_HOME:-~/.local/state}/palimpsest/` (Permissions: `0700`).

```text
~/.local/state/palimpsest/
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
├── locks/                          # Exclusive process lock files (dir: 0700)
│   └── <name>.lock                 # Run lock file (file: 0600)
├── transfers/                      # Resumable upload session checkpoints (dir: 0700)
│   └── <digest_hex>.json           # Transfer metadata record (file: 0600)
├── tags/                           # Local tag records (dir: 0700)
│   └── <tag>.json                  # Schema v1 tag record (file: 0600)
└── builds/                         # Disposable build records (dir: 0700)
    └── <build-id>/                 # Per-build log and ledger (dir: 0700)
        └── record.json             # Schema v1 build record (file: 0600)
```

---

## KVM Release Gate Notice

> **Mandatory Release Gate:** Standalone release `v0.1.0` on PyPI and cutover of Afterglow dependencies remain **blocked** pending full integration proof on a physical Linux x86_64 KVM host (`pytest -m kvm`). The implementation is verified in pure unit and mock integration environments, but final publication requires end-to-end execution proof on hardware virtualization.
