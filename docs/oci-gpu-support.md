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

## Proposed implementation sequence — not implemented

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
