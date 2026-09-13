# OCI machine-learning image compatibility

Palimpsest's first ML compatibility proof is deliberately CPU-only and opt-in.
It covers pinned official TensorFlow 2.21 CPU and PyTorch 2.8 CUDA-runtime
images after registry acquisition, without adding a GPU device, external
network, host directory, capability, or interactive shell.

Both selected images authenticate `/bin/bash` as their default command. That
process exits on the non-interactive main-process boundary. The proof therefore
uses the public local-run command override to replace `Cmd` with fixed
`/bin/sleep infinity`; the authenticated image `Entrypoint` remains in front of
that command. Boot-plan v4 provenance binds the original process vectors and
the override. Image config, layers, DiffIDs, and archive remain unchanged.

The two cases run sequentially with 8 GiB RAM, two vCPUs, and `network none`.
Public `exec` performs a deterministic 2-by-2 matrix multiplication with
single-thread framework settings, checks the exact result and sum, CPU device,
framework version, and absence of an available GPU, then verifies authenticated
root identity, direct PID 1 root refusal, NIC-, hostdev-, and
host-filesystem-free domain XML, normal stop/remove, and original archive
preservation.

The anonymous TLS registry metadata selection used to acquire the proof inputs
is fixed to Linux/amd64:

| Framework image | Manifest digest | Compressed layer bytes |
| --- | --- | ---: |
| `docker.io/tensorflow/tensorflow:2.21.0` | `sha256:f325279f01a3e742a1285d8d736b7e600f72ccf8b55cc19ee0a90b8cbfce4c7a` | 616,394,536 |
| `docker.io/pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime` | `sha256:dab81780fd94483b67b4b5679cc0024939b08e48540d39476d284cb29002ed69` | 3,748,077,342 |

Enable a case only with its complete set of private proof inputs:

```text
PALIMPSEST_OCI_ML_TENSORFLOW_LIVE=1
PALIMPSEST_OCI_ML_TENSORFLOW_IMAGE=/absolute/tensorflow.oci.tar
PALIMPSEST_OCI_ML_TENSORFLOW_ARCHIVE_SHA256=sha256:<digest>
PALIMPSEST_OCI_ML_TENSORFLOW_MANIFEST_SHA256=sha256:<digest>
```

Use `PYTORCH` in place of `TENSORFLOW` for the second case. The ordinary native
OCI boot/kernel/packer prerequisites still apply. Failed VM state is preserved
for diagnosis; success removes only the proof-owned run. Before materialization,
the proof requires at least 40 GiB free on its private runtime filesystem; the
native harness additionally monitors the proof-owned tree against that 40 GiB
budget because compressed registry size does not bound materialized storage.

Run the cases independently after supplying their exact inputs and the normal
native boot variables. `PALIMPSEST_OCI_ML_PROOF_ROOT` must name a canonical,
caller-owned runtime parent created through `palimpsest oci init-runtime`; the
resulting `0711` directory and each ancestor must retain all search bits so the
native QEMU identity can traverse to the proof-owned runtime. Use a short path
to keep the lifecycle socket within the platform bound. Do not place this root
beneath a private `0700` evidence directory.

```bash
palimpsest oci init-runtime /tmp/pml-example
export PALIMPSEST_OCI_ML_PROOF_ROOT=/tmp/pml-example
mkdir -m 700 /tmp/pml-journal-example
export PALIMPSEST_LOG_HOME=/tmp/pml-journal-example
```

```bash
uv run pytest -q tests/kvm/test_oci_ml_cpu_live.py::test_official_ml_image_cpu_tensor_with_public_command_override[tensorflow]
uv run pytest -q tests/kvm/test_oci_ml_cpu_live.py::test_official_ml_image_cpu_tensor_with_public_command_override[pytorch]
```

This is not a full development-VM or GPU result. OCI-root currently lacks
external networking, host project mounts, an interactive shell, and public
environment/working-directory overrides. GPU support additionally needs
a reviewed virtual-device assignment or sharing contract, guest driver/runtime
compatibility, IOMMU isolation, reset and ownership behavior, and OpenStack
resource-provider integration. This proof performs no VFIO binding, host driver
change, PCI attachment, or GPU claim.

The selected TensorFlow tag follows TensorFlow's official Docker install
documentation; its published image metadata identifies the 2.21.0 Linux/amd64
manifest used by the proof. PyTorch's published tags identify the selected 2.8
CUDA 12.6 runtime image. Registry tags can move, so neither tag is evidence:
the proof always requires separately recorded manifest and archive digests.

- [TensorFlow Docker installation guide](https://www.tensorflow.org/install/docker)
- [TensorFlow 2.21.0 image metadata](https://hub.docker.com/layers/tensorflow/tensorflow/2.21.0/images/sha256-f325279f01a3e742a1285d8d736b7e600f72ccf8b55cc19ee0a90b8cbfce4c7a)
- [PyTorch official image tags](https://hub.docker.com/r/pytorch/pytorch/tags?page=2)
