# Palimpsest Local handoff

## Decision

Create this directory as the home of the standalone Python project, **`palimpsest-local`**. It owns verified local artifacts, an Apple-Silicon-native Lima/VZ prototype, the Linux KVM runtime, OCI-layout handling, and Docker-like `palimpsest` command-line interface. Afterglow keeps its authenticated Hub/API implementation and later imports exact **required `palimpsest-local==0.1.0`** (with `libvirt-python` as an optional `[kvm]` extra) through a narrow adapter.

The macOS prototype is runnable on Apple Silicon with Lima. Standalone release `v0.1.0` on PyPI and Afterglow cutover remain pending real x86_64 Linux KVM proof (`pytest -m kvm`).

## Why this project exists

Afterglow already has the ingredients, but not a usable local workflow:

- `scripts/palimpsest.py` provides Hub-oriented `images`, `layers`, `pull`, `pack`, `push`, and `bundle` commands, but no local VM lifecycle, shell, build-session, state, or cleanup commands.
- `backend/app/services/palimpsest_kvm.py` can generate libvirt XML, make NoCloud seed input, attach SquashFS layers as RO virtio-blk disks, and define/start/destroy domains. It is backend-owned and has no user-facing CLI/runtime state.
- `docs/palimpsest-local-kvm-runbook.md` currently requires manual `qemu-img`, `cloud-localds`, `virt-install`, guest SSH, delta extraction, packing, and upload.

The standalone project turns that manual sequence into a safe local tool without putting libvirt/QEMU dependencies in Afterglow's API container.

## Non-negotiable runtime model

```text
verified bootable qcow2/raw base blob (immutable backing file)
  └─ per-build/per-run qcow2 overlay (the only RW vda)
       └─ guest boots normally: bootloader, kernel, initramfs, /etc, /var

verified SquashFS package/toolchain blobs
  └─ RO virtio-blk disks (vdb..vdz)
       └─ guest mounts by /dev/disk/by-id/virtio-<serial>
            └─ OverlayFS merged view at /opt/layers/merged
                 ├─ upperdir/workdir: guest-local writable FS
                 └─ lowerdir: leaf → root order
```

**Never attach a content-addressed base qcow2/raw blob RW.** Each run and build first creates a new qcow2 overlay and attaches only that overlay as `vda`. This preserves blob digest validity and makes base images reusable.

**Do not use Ubuntu official `base.squashfs` as a runtime lower root layer.** The official cloud-image SquashFS is a rootfs artifact, not a boot disk. Its Resolute manifest has no `linux-image-*` or `linux-modules-*` packages. Adding it below `/usr` would not remove the boot volume and can hide `/usr/lib/modules/$(uname -r)`, while adding NFS/SquashFS/cache overhead. Keep the bootable qcow2/raw base plus package deltas.

## Existing Afterglow compatibility contract

The external project must remain compatible with these facts:

| Contract | Afterglow source |
|---|---|
| Blob identity is `sha256:<64hex>` and blobs are stored at `blobs/sha256/<hex>`. | `backend/app/services/palimpsest_hub_store.py`; `docs/palimpsest.md` |
| Layer input is root → leaf; OverlayFS needs leaf → root because leftmost lowerdir wins. | `backend/app/services/palimpsest_kvm.py`; `docs/palimpsest.md` |
| Root boot disk is qcow2 and layers are raw RO virtio disks. | `backend/tests/test_palimpsest_kvm.py` |
| Virtio serial is digest-derived and guest lookup must use `/dev/disk/by-id/virtio-<serial>`, never `/dev/vdX`. | `backend/app/services/palimpsest_kvm.py`; `backend/tests/test_palimpsest_kvm.py` |
| Maximum supported layer disks is 25 (`vdb`–`vdz`). | `backend/app/services/palimpsest_kvm.py` |
| `upperdir` and `workdir` must be local writable storage, not NFS/CephFS/virtiofs. | `docs/palimpsest.md` |
| Hub download/upload must verify blob SHA-256; bad downloads are removed rather than used. | `scripts/palimpsest.py`; `backend/app/services/palimpsest_hub_store.py` |

## First-release command surface

```text
palimpsest image ls
palimpsest image pull <digest>
palimpsest image import <path> --disk-format qcow2|raw --arch x86_64|aarch64

palimpsest layer ls
palimpsest layer pull <digest>
palimpsest layer pack <directory> --tag <name>
palimpsest layer push <tag|path>

palimpsest bundle pull <leaf-digest> --include-base --output DIR
palimpsest bundle verify <directory>

palimpsest build --base <image-digest> [-f Palimpsestfile]
palimpsest run <image-or-bundle> [--layer sha256:...] --name <name>
palimpsest ps
palimpsest inspect <name>
palimpsest logs <name> [--follow]
palimpsest shell <name>
palimpsest exec <name> -- <command> [args...]
palimpsest stop <name>
palimpsest rm <name> [--volumes]
palimpsest commit <name> --tag <layer-name>

## macOS Apple Silicon prototype

The native path requires Apple Silicon and [Lima](https://lima-vm.io/):

```sh
brew install lima
```

Use an ARM64 Ubuntu cloud image. Once an Afterglow-compatible Hub publishes it, `run` and `build` automatically pull a missing verified `cloud-image` blob into the content store. Select the default Hub with `PALIMPSEST_URL`/`PALIMPSEST_TOKEN`, or select a Hub per command:

```sh
palimpsest --url https://hub.example image pull sha256:<ubuntu-arm64-digest>
palimpsest --url https://hub.example run sha256:<ubuntu-arm64-digest> --name ubuntu-arm --memory 4096 --vcpus 2
```

Until that Hub image is available, import an official Ubuntu ARM64 cloud image explicitly:

```sh
palimpsest image import ./ubuntu-24.04-server-cloudimg-arm64.img \
  --disk-format qcow2 --arch aarch64 --os-variant ubuntu-24.04
palimpsest run sha256:<printed-digest> --name ubuntu-arm
palimpsest inspect ubuntu-arm
palimpsest shell ubuntu-arm
palimpsest exec ubuntu-arm -- uname -m
```

`inspect` records the guest IPv4 address and Lima's host-local SSH endpoint (`127.0.0.1:<ssh_local_port>`); `palimpsest shell` delegates to `limactl shell`, Lima's authenticated SSH console. The VZ backend enables managed NAT and cannot run a user VM with `--network none`.

Builds use the same fixed guest worker and recover the same verified `output.squashfs` contract as Linux KVM builds:

```sh
palimpsest build --base sha256:<ubuntu-arm64-digest> --tag tools -f Palimpsestfile --network none
palimpsest run sha256:<ubuntu-arm64-digest> --layer sha256:<built-layer-digest> --name tools-arm
palimpsest exec tools-arm -- ls /opt/layers/merged
```

The prototype attaches verified layers through a guest-local SquashFS + OverlayFS mount at `/opt/layers/merged`. It does not implement `palimpsest commit` on macOS; use `build` to create portable content-store and Hub-uploadable layer artifacts.

## Implementation plan

See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). The plan is copied from Afterglow's approval-pending plan and incorporates the immutable-base-overlay correction.

## Current source references

- Afterglow plan source: `../afterglow/.omc/plans/palimpsest-local-cli.md`
- Local KVM primitive: `../afterglow/backend/app/services/palimpsest_kvm.py`
- Existing CLI: `../afterglow/scripts/palimpsest.py`
- Local runbook: `../afterglow/docs/palimpsest-local-kvm-runbook.md`
- Existing unit contracts: `../afterglow/backend/tests/test_palimpsest_kvm.py`
- OpenSpec history and uncompleted local KVM proof: `../afterglow/openspec/changes/palimpsest-layered-vm/tasks.md`
