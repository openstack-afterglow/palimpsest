# GPU support boundary and implementation path

GPU use is **not implemented or live-qualified in the OCI-root runtime**.
Downloading a CUDA/PyTorch/TensorFlow image does not assign a GPU, install a
guest kernel driver, or prove CUDA computation. CPU framework qualification
is a separate check.

## Why a device mount is insufficient

A container shares its host kernel; a Palimpsest VM has a separate kernel.
For Docker, TensorFlow documents the host NVIDIA driver and NVIDIA Container
Toolkit as GPU prerequisites. Those host device/library injections cannot
simply be reused across a VM boundary. A GPU-consuming VM needs an assigned
PCI/vGPU device, a compatible driver in its own kernel, and compatible
userspace driver/CUDA libraries. [TensorFlow Docker prerequisites](https://www.tensorflow.org/install/docker).

The current source has several deliberate boundaries:

- `oci_root_runtime._validate_devices_surface` rejects unauthored device
  classes, including `hostdev`; the public request has no GPU field.
- The authenticated boot plan has no GPU identity or allocation contract.
- Stage-1 constructs a private workload `/dev` with six fixed character
  devices and three standard/self-FD aliases. NVIDIA devices are not exposed.
- The packaged stage-1/runtime qualification does not install or load a
  kernel-matched NVIDIA driver bundle.

Changing only libvirt XML or bind-mounting host `/dev/nvidia*` would bypass
these contracts and is not a supported solution. Guest VFIO is not required
merely to consume a passed-through GPU: VFIO/IOMMU responsibilities belong to
the assigning hypervisor. Reassigning a device to a nested guest is a
different problem.

## Assignment and sharing choices

| Method | What can share the physical GPU? | Provisioning boundary |
| --- | --- | --- |
| Full PCI passthrough | One VM exclusively | Host IOMMU/VFIO preparation, complete assignable group, libvirt PCI `hostdev`, guest driver |
| Vendor vGPU / supported SR-IOV virtual functions | Multiple VMs through separately assigned virtual devices | Supported GPU, hypervisor, vendor host/guest software and applicable licensing; Nova resource scheduling |
| MIG-backed vGPU | Multiple VMs with supported hardware partitions | MIG-capable hardware plus the supported vGPU integration; a MIG instance alone is not a generic PCI device |
| CUDA multi-process sharing / MPS | Cooperating processes using the same GPU-owning OS | An application-sharing service inside the owning host or VM; not a generic cross-VM PCI-sharing mechanism |

Full passthrough assigns the whole device to one guest. Nova uses PCI aliases
requested through flavor extra specs such as `pci_passthrough:alias`; vGPU
uses its own resource inventories and flavor requests. The exact configuration
is release- and hardware-dependent. [Nova PCI passthrough](https://docs.openstack.org/nova/latest/admin/pci-passthrough.html),
[Nova virtual GPU administration](https://docs.openstack.org/nova/latest/admin/virtual-gpu.html),
[libvirt device XML](https://libvirt.org/formatdomain.html#host-device-assignment).

MIG and official vGPU support must be checked for the exact GPU, not inferred
from the CUDA or Ampere name. In particular, GeForce RTX 3080 Ti is not on the
published MIG or vGPU supported-product lists. MIG-capable GPUs can also use
MIG inside supported Linux guests with full GPU passthrough; that does not
establish nested vGPU support. [MIG GPUs](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-gpus.html),
[MIG virtualization](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/virtualization.html),
[vGPU products](https://docs.nvidia.com/vgpu/gpus-supported-by-vgpu.html).
MPS is a CUDA multi-process service with its own sharing/resource controls,
not an independent VM hardware boundary. [NVIDIA MPS](https://docs.nvidia.com/deploy/mps/latest/index.html).

## OpenStack topology matters

1. **OpenStack GPU instance is the workload VM:** Nova assigns the physical
   GPU or supported vGPU once. Its guest driver and CUDA computation must be
   tested in that instance. Booting Palimpsest's authenticated OCI-root layout
   as this instance would require a dedicated Nova/boot-artifact integration;
   the current local libvirt adapter does not provide it.
2. **Palimpsest runs inside an OpenStack GPU instance:** this adds an L2 VM.
   A GPU visible to the L1 OS is not automatically available to L2. CPU nested
   KVM capability alone does not prove nested device assignment. Full-device
   reassignment needs separately qualified L0/L1 virtual-IOMMU, DMA/reset and
   hypervisor support. NVIDIA's vGPU documentation generally excludes nested
   virtualization, so this must not be advertised as supported vGPU use.
   [NVIDIA nested-virtualization limitation](https://docs.nvidia.com/vgpu/19.0/grid-vgpu-release-notes-generic-linux-kvm/known-product-limitations.html).

Running CUDA containers directly inside a GPU-enabled OpenStack instance is
a different, single-kernel application deployment; it is not proof that a
nested Palimpsest VM can use that GPU.

### Selected target: the Nova instance itself owns the OCI root

The user selected topology 1: the OpenStack GPU instance is the workload VM.
Nested Palimpsest/L2 assignment is not a prerequisite for this target. Nova
and its compute operator own physical GPU allocation and host rebinding;
Palimpsest must consume the assigned device inside that instance. This choice
does not authorize compute-host changes or qualify any existing GPU instance.

The first integration should establish a CPU-only bootable image before adding
a guest GPU runtime. The intended artifact is a self-contained boot disk,
published through Glance and optionally used to initialize an exclusive Cinder
root volume. A `raw`/`qcow2` format label alone supplies neither a bootloader nor
an OCI-root transition. The current Hub export service works in the opposite
direction (Glance image to Hub/downloadable disk conversion), not OCI image to
Nova-ready boot disk. [Glance disk formats](https://docs.openstack.org/glance/latest/user/formats.html).

The following are implementation gates, not existing commands or features:

| Gate | Required deliverable and acceptance |
| --- | --- |
| Portable boot disk | Qualified firmware/bootloader, kernel/initramfs, immutable OCI-derived content and per-instance writable root; boot from disk without local `-kernel`/`-initrd` paths |
| Cloud bootstrap authority | A reviewed replacement for local domain/virtio-disk serial bindings and the host monitor channel; reject changed content and do not reuse a local run's transport or bake shared control secrets into a reusable image |
| Nova CPU instance | OCI content becomes the instance's actual `/`; verify workload output, root identity, PID 1 protection, reboot behavior and owner-bound teardown on a real target cloud |
| GPU guest runtime | Integrity-checked kernel-matched driver and compatible userspace libraries, explicitly admitted guest device access; no workload-wide module-loading privilege |
| GPU execution | Nova-assigned device enumeration followed by actual CUDA/explicit-GPU matrix calculation and synchronization, with CPU fallback rejected; verify protections and release independently |
| Root-volume lifecycle | Explicit delete-on-termination or retain policy; reuse a retained root only after prior attachment ends and image/layout compatibility is checked; shared data volumes remain a separate contract |

The local adapter currently authors kernel/initrd/cmdline, authenticated
individual virtio block roles, and a host-connected lifecycle channel. A disk
container conversion cannot carry these host facilities into ordinary Nova
boot automatically. The bootstrap and management transport must therefore be
designed and reviewed explicitly; neither cloud-init, config-drive nor a serial
console is assumed to provide the existing authenticated control contract.

For a Cinder root, Nova's block-device mapping exposes deletion policy. This
maps to the requested VM-owned versus retained boot disk behavior, but does not
make concurrent multi-attach of a writable root safe. Templates may be shared;
each running instance needs its own writable root. A later instance may reuse
an explicitly retained, detached root after compatibility checks.
[Nova boot from volume and deletion policy](https://docs.openstack.org/nova/latest/user/launch-instance-from-volume.html).

Before cloud-native testing, record the actual OpenStack release, approved
project/endpoint, image firmware and disk-bus requirements, existing GPU flavor
and assigned GPU/driver requirements. These inputs are not known for the target
cloud yet. Do not create flavors, alter compute configuration, upload a new
image or launch a paid instance merely from this design selection.

## Retained local-hypervisor alternative — not the selected Nova path

The sequence below is an alternative for a Palimpsest-managed local hypervisor,
not work required for the selected Nova-instance topology. Its host-side
allocation, libvirt XML and rebinding steps must not be applied by Palimpsest
to Nova-managed compute hosts. Nested assignment would need its own additional
qualification. Only the read-only inventory primitive below is implemented;
allocation, assignment, guest GPU runtime and CUDA qualification are neither
implemented nor live-qualified.

The internal PCI preflight collector is a read-only inventory primitive for
the first step below. Given one canonical PCI BDF, it reports bounded vendor,
device, subsystem, class and revision facts; the current driver; boot-display
state; reset-file presence; and the complete bounded IOMMU-group member list.
Optional facts are explicitly bound, unbound/absent, or unknown. It returns no
eligibility or assignability verdict. The collector never reads PCI resource,
configuration, ROM or enable surfaces and never probes, loads, unbinds, binds,
resets, authors XML or touches a domain. Its snapshot is not transactional, so
detected identity substitution or malformed/bounded input fails closed and a successful
report is only a point-in-time observation.

Linux deliberately exposes PCI bus entries and IOMMU-group members as sysfs
symlinks. The collector follows only those known link positions after checking
their canonical targets and pinning the target directories; it caps bytes
actually read rather than trusting sysfs `st_size`. See the kernel's
[sysfs reading and layout contract](https://docs.kernel.org/filesystems/sysfs.html),
[PCI sysfs interface](https://docs.kernel.org/PCI/sysfs-pci.html), and
[VFIO IOMMU-group ownership model](https://docs.kernel.org/driver-api/vfio.html).

1. Read-only GPU preflight: vendor/device identity, current driver/consumers,
   complete IOMMU group, boot-display status, reset support, virtualization
   level and vendor-supported sharing modes. An idle utilization sample does
   not authorize taking a host display GPU away from its driver.
2. Explicit exclusive GPU allocation: typed request, durable device/group
   lease, exact domain XML projection, rollback and owner-bound release.
   Host driver rebinding, display disruption and OpenStack operator changes
   require a separately approved maintenance action. No automatic takeover.
3. Qualified guest GPU runtime: kernel-matched, integrity-checked driver
   modules and compatible userspace libraries; trusted bootstrap loading;
   authenticated GPU identity and narrow workload device access. Preserve
   PID 1 isolation instead of granting workload-wide module/device authority.
4. Native proof on one device: enumeration and driver initialization, then
   PyTorch CUDA tensor allocation/matrix multiplication/synchronization or
   TensorFlow explicit GPU placement with CPU fallback disabled. Check the
   numeric result and actual device, root/PID 1 protections, normal stop and
   exclusive allocation release. `nvidia-smi` alone is insufficient.
5. Only after the exclusive path passes, qualify supported vGPU/MIG profiles,
   allocation limits, concurrent workloads and isolation. Treat the nested
   OpenStack path as a separate gate, not an inherited capability.

No GPU driver was unbound, no PCI device was reassigned, and no GPU VM or
OpenStack integration success is claimed by this design document.
The first private read-only hardware-helper transfer was blocked before
execution, so it produced no fresh host PCI or GPU facts.
