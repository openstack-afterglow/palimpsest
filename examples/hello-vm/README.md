# Hello VM Walkthrough

This tutorial demonstrates the complete workflow for building and running a Palimpsest VM: downloading an official Ubuntu cloud image, building a custom layer inside a disposable builder, running the VM with that layer attached, and executing commands in the guest.

## Prerequisites

- **macOS arm64 (Apple Silicon):** Lima 2.1+ installed (`brew install lima`).
- **Linux x86_64:** `/dev/kvm` hardware access, `libvirt`, and `qemu-system-x86_64`.
- **Linux aarch64:** `/dev/kvm` hardware access, `libvirt`, `qemu-system-aarch64`, and EFI firmware.
- **Python:** Python 3.12+ with the repository environment set up:
  ```bash
  uv sync --extra dev              # macOS Apple Silicon (Lima/VZ)
  uv sync --extra dev --extra kvm  # Linux, adds libvirt-python
  ```
- **State isolation (optional):** export an absolute `PALIMPSEST_STATE_HOME` to
  keep example artifacts out of your working store.

---

## Step 1: Download an Ubuntu Cloud Image

Download the official Ubuntu 24.04 (`noble`) release image matching your host platform.

| Host platform | Runner architecture | Image file |
|---|---|---|
| macOS arm64 | `aarch64` (`lima-vz`) | `ubuntu-24.04-server-cloudimg-arm64.img` |
| Linux x86_64 | `x86_64` (`kvm`) | `ubuntu-24.04-server-cloudimg-amd64.img` |
| Linux aarch64 | `aarch64` (`kvm`) | `ubuntu-24.04-server-cloudimg-arm64.img` |

```bash
cd /tmp
IMAGE=ubuntu-24.04-server-cloudimg-arm64.img   # Linux x86_64: ...-amd64.img
curl -L -O "https://cloud-images.ubuntu.com/releases/noble/release/$IMAGE"
```

Verify the download against the official checksum list. Filter for the exact
file name; a bare `grep arm64` also matches archives you did not download and
makes the check fail.

```bash
# macOS
curl -sL https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS \
  | grep "[*]$IMAGE$" | shasum -a 256 -c -

# Linux
curl -sL https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS \
  | grep "[*]$IMAGE$" | sha256sum -c -
```

Expected output: `ubuntu-24.04-server-cloudimg-arm64.img: OK`.

---

## Step 2: Run the Example

The runner imports the cloud image, builds a custom SquashFS layer in a
disposable builder guest, starts a 2048 MiB / 2 vCPU VM with that layer
attached, and verifies the result from inside the guest. Pass the image you
downloaded for your own platform.

```bash
# From the repository root; default VM name is 'hello-vm'
./examples/hello-vm/run.sh /tmp/ubuntu-24.04-server-cloudimg-arm64.img

# Optional second argument: a custom run name
./examples/hello-vm/run.sh /tmp/ubuntu-24.04-server-cloudimg-arm64.img my-demo-vm
```

On a macOS arm64 host the tail of a successful run looks like this. Layer
digests are content-addressed and depend on build time, so your digest will
differ:

```text
==> Host: Darwin/aarch64 -> arch=aarch64 backend=lima-vz
==> Imported base image digest: sha256:4a281a921b8d...
==> Building SquashFS layer (tag: hello-vm-layer, network: none)
==> Built layer digest: sha256:<generated>
==> VM is running: limactl shell hello-vm
==> Verifying live guest architecture (uname -m)
aarch64
==> Verifying built layer message (/opt/layers/merged/opt/hello-vm/message.txt)
Hello from a real Palimpsest VM
```

---

## Step 3: Understand What the Runner Does

The runner automates these steps:

1. **Host & Architecture Mapping:** Maps macOS arm64 to `aarch64`/`lima-vz`, Linux x86_64 to `x86_64`/`kvm`, and Linux aarch64 to `aarch64`/`kvm`.
2. **Image Import:** Registers the image in the content-addressed store (`palimpsest image import`).
3. **Palimpsestfile Build:** Generates a recipe to write `/opt/hello-vm/message.txt` and `/opt/hello-vm/arch.txt`, then runs `palimpsest build` with `--network none` in a disposable builder guest.
4. **VM Startup:** Launches the guest VM (`palimpsest run`) with 2048 MiB RAM, 2 vCPUs, the `--network default` profile, and the built layer attached.
5. **Layer Verification:** Uses `palimpsest exec` to read the files from `/opt/layers/merged/` inside the guest.

---

## Step 4: Interact with the VM

The VM remains running after the script completes. Run lifecycle commands from the repository root:

### Check Layer Files & Architecture

```bash
# Read the message file written by the layer
uv run palimpsest exec hello-vm -- cat /opt/layers/merged/opt/hello-vm/message.txt
# Output: Hello from a real Palimpsest VM

# Read the architecture captured during layer build
uv run palimpsest exec hello-vm -- cat /opt/layers/merged/opt/hello-vm/arch.txt

# Query live guest architecture
uv run palimpsest exec hello-vm -- uname -m
```

### Inspect Status & Configuration

```bash
# List all active VMs and backend assignments
uv run palimpsest ps

# View the allowlisted durable-state snapshot, SSH endpoint, and attached layers
uv run palimpsest inspect hello-vm

# Read the retained Palimpsest console/provisioning log for this run
uv run palimpsest logs hello-vm
```

On every host, `ps` and `inspect` report persisted state rather than querying the
live backend. On macOS, `logs` reads the retained Palimpsest console log, not the
live Lima guest journal. For the in-guest journal use Lima directly:

```bash
limactl shell hello-vm journalctl -b --no-pager
```

### Open an Interactive Shell

```bash
uv run palimpsest shell hello-vm
```

Inside the guest shell, type `exit` to return to your host terminal.

---

## Step 5: Cleanup

When you are finished, stop and remove the VM:

```bash
# Graceful stop
uv run palimpsest stop hello-vm

# Remove VM ledger and delete run storage (ephemeral overlay, seed ISO, SSH keys)
uv run palimpsest rm hello-vm --volumes
```

---

## Next Steps

- See [VM Workflow Guide](../../docs/vm-workflow.md) for full manual workflows, layer architecture, and backend details.
- See [Installing Palimpsest Local](../../docs/install.md) for detailed platform requirements and storage layouts.
