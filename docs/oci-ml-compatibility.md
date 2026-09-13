# OCI machine-learning image compatibility

## Execution checkpoints

Both pinned archives below were acquired and verified through the public
anonymous registry intake. Acquisition does not establish VM compatibility.
At `581061f`, the first native attempt stopped during pytest collection because
the ML proof used a bare sibling-module import. No VM was created; the existing
15 inactive domains and 16 archive pins passed preservation checks. The follow-up
uses an explicit file-based helper import and adds an independent collect-only
regression. CPU execution results are recorded separately when available.

After private setup establishes its evidence directory, the proof best-effort
replaces a private `ml-phase.json` diagnostic receipt. Selection or setup
failure therefore has no receipt guarantee. The receipt contains only fixed
framework, phase, and status enums plus a strict integer return code when the
immediately preceding bounded command returned one. It does not retain argv,
paths, exception text, stdout, or stderr. Nonzero commands retain their command
phase and return code; separately named subsequent assertion phases are
recorded where defined. Receipt-write failure never replaces the original proof
failure. The receipt is diagnostic evidence, not a success result or cleanup
authority.

Fresh monitor-coordinator failures expose only a fixed coarse stage and the
existing `MonitorIPCErrorCategory` value. The child response distinguishes
request validation, authority validation, monitor spawn, and response send;
the parent can additionally identify response, discovery, and coordinator-exit
boundaries. Unknown failures collapse to `child-failed`, malformed codes are
rejected, and no child stderr, exception text, path, argv, or credential is
forwarded. These codes improve the next proof's diagnosis but cannot recover a
category discarded by an older run and do not alter timeout or cleanup policy.

At `049a978`, the corrected tests collected, but neither framework reached a
guest result. TensorFlow stopped before VM definition at the literal-backslash
layer intake boundary. PyTorch completed conversion, then its libvirt
connection expired during the long export/rehash interval before definition.
The follow-up startup-event contract services that same validated connection
during slow preparation, forbids reconnect, and stops fully before lifecycle
event handling. Failure handling is phase-specific: health loss after a
`defineXML` attempt but before its durable record uses the existing exact
cleanup; loss after the durable public definition retains the exact inactive
domain and `defined` ledger; a bound-monitor failure after activation intent or
creation quarantines the connection and records exact cleanup-required
evidence. Portable tests do not turn either prior native failure into a
compatibility pass.

At `132c6c5`, TensorFlow again returned pytest code 1 with retained evidence,
no wrapper accounting error, and preserved host baselines. A bounded reviewed
classifier mapped the `AssertionError` to the proof's root-volume-count check,
before root proof or framework execution. The proof incorrectly expected one
new directory, while the production volume contract publishes the authenticated
volume UUID as two regular siblings, `<uuidhex>.raw` and `<uuidhex>.json`. The
corrected proof binds those exact files to the durable preparation transaction,
checks the raw size and safe file identities, and requires both to disappear on
successful cleanup. This diagnosis and test correction are not a TensorFlow
framework or VM compatibility pass.

The service is limited to the public preparation connection and the fresh
bound-monitor connection. It uses a 10 ms event timer, a one-second startup and
join bound, and a 100 ms serialized event-lock wait; those bounds do not make a
blocked libvirt syscall a hard wall-clock deadline. It never reconnects and is
fully stopped before lifecycle stream pumping. Quarantined connections are not
closed or used for cleanup. Before quarantine, definition failures follow the
phase-specific exact-cleanup or durable-definition retention rules above.
Guest code and the monitor protocol are unchanged.

## CPU proof contract

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

TensorFlow's pinned image also exercises a Linux filename containing a literal
backslash in layer ordinal 4. Intake preserves that character rather than
treating it as a separator; slash traversal, reserved-tree, and other path
rejections remain in force. A successful portable or packer test defines this
compatibility behavior, while an actual TensorFlow VM result remains a separate
native proof.

The first synthetic pack/readback attempt at `3f8e79e` packed successfully but
failed before any VM work because `unsquashfs -cat` treated the literal
backslash as part of its default wildcard selector. Source and installed-tool
help diagnosis led to adding the exact-name `-no-wildcards` mode; this is a
test-harness correction, not a converter, intake-policy, cache, or pack-format
change. At that checkpoint the corrected node had not run, so the failed
attempt remains failure evidence rather than a compatibility pass.

The exact Linux checkout at `3f8e79e` passed the focused server selection with
763 passed and no other outcomes. At the test-only follow-up `132c6c5`, a fresh
synthetic pack and exact-name readback passed once; GitHub packages succeeded
for both revisions. The TensorFlow native case at `132c6c5` reached VM creation
but returned pytest rc1. Its newly owned domain was stopped and retained
inactive with no accounting errors, its journal was present, and the 15 prior
domains plus that new domain, all 16 archive pins, and zero-active postflight
were preserved. The exact failing command and whether guest root transition,
CPU execution, authenticated root, or PID 1 refusal were reached remain
unknown. A request to retrieve further redacted remote diagnostics was denied
or aborted, so it was not retried; explicit narrow approval remains pending.
This result establishes none of those guest assertions. The PyTorch case at the
same revision also returned pytest rc1, but its wrapper could not attribute the
resource: the inner attribution and snapshot failed, as did the outer
attribution and postflight. Its journal was present. A separate read-only
inventory observed one new shut-off persistent domain with autostart disabled
and recorded its UUID and inactive XML digest privately, but that observation
does not establish owned-resource attribution or whole-inventory/archive
preservation. The domain is not adopted as owned and was not rerun, removed, or
queried further. Both native failure causes require a separately approved,
limited diagnostic before any fix is proposed. No GPU action was performed.

The subsequent user-approved read-only diagnostic found both preserved runtimes
without returning their paths or raw logs. TensorFlow had the expected detached
name output and a schema/UUID-matching monitor journal, but no tensor or later
probe files. PyTorch had no successful detached-name output and no preactivation
journal; its fixed diagnostic marker identified the monitor handshake. This
explains why the wrapper's binding loader failed and then rejected the unexpected
domain during its inventory check. It does not establish the underlying
handshake error or justify increasing a timeout. Neither framework reached the
CPU tensor/root/PID1 proof at this checkpoint. The PyTorch domain remains
unattributed: it must never be stopped or removed based on these diagnostics.
An explicitly pinned foreign-domain preservation baseline for later tests is
not adoption, ownership proof, or retroactive historical postflight success.

At `a6a1d84`, the root-volume test correction and fixed phase receipts passed
105 selected checks both locally and on the exact Linux checkout, with no other
server test outcomes. The GitHub development package also succeeded. These are
focused test/package results, not a new native framework pass.

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
