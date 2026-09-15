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

The subsequent TensorFlow-only native attempt at `a6a1d84` failed at
`framework-exec-command` with return code 1, as confirmed by its bounded
`ml-phase.json` receipt. Public detached run, provenance, root-volume checks,
the initial authenticated root proof and CPU-only domain checks completed
before that phase. Framework output, the independent app-root comparison,
PID 1 refusal and normal stop/remove did not complete. An exit code alone
does not distinguish a guest Python error from host exec/control failure.

Postflight preserved the explicitly pinned 17 inactive foreign/baseline
domains, 16 original archive pins and zero-active state, with no additional
domain remaining. The wrapper nevertheless rejected resource attribution
with `RuntimeError`, so the new runtime's ledger/domain ownership relation
was not established. This is not cleanup success, nor evidence that failure preceded VM creation;
the phase receipt establishes later progress. No manual stop/remove or
adoption followed this accounting failure, and the remaining evidence is
preserved for bounded diagnosis.

A subsequent approved classifier found only the fixed host
`monitor_client_timed_out` marker in that TensorFlow command's saved stderr.
The other 15 selected Python/TensorFlow and host-error markers were absent;
absence of those markers is not proof that Python ran or that no guest error
occurred. At `a6a1d84`, `MonitorClient._stable_errors` mapped its own deadline,
an IPC timeout, and a run-lock timeout to the same message. The result therefore identifies
the public exec monitor-client timeout boundary, not which wait expired or
why. No interpreter, timeout, guest privilege or cleanup policy was changed,
and no new native run followed this diagnostic.

Current monitor-client errors preserve that original path-free timeout
guidance and append one fixed `timeout-source` value: `client-deadline`,
`ipc-timeout`, or `run-lock-timeout`. The value identifies only the host wait
boundary that reported the timeout. It does not establish whether the guest
command started or completed, and it does not change the existing deadlines,
same-request retry, locking, authority, or cleanup behavior. Historical runs
that emitted only the generic message cannot be classified retroactively.

At `aad3d492`, the timeout-origin client and exec-session selection passed
105 checks locally and exactly 105 with no other outcomes on the Linux
checkout. Public CLI propagation and malformed enum rejection are included.
The local architecture-guard regression passed 13 checks and the GitHub
development package succeeded. No native VM was launched for these focused
checks; the prior TensorFlow failure and PyTorch qualification limits remain.

At `5b94a728`, the coordinator, monitor IPC, monitor launch and run-adapter
selection passed 228 checks locally with six Linux-only skips, then exactly
234 checks with no other outcomes on the pinned Linux checkout. Its GitHub
development package succeeded. This qualifies the selected diagnostic-code
regressions, not a fresh PyTorch/TensorFlow native execution or GPU support.

At `9239dbd`, both pinned cases ran once each on the exact Linux checkout, and
neither reached the CPU tensor, root-identity, or PID 1 assertions.

TensorFlow's detached public run returned its name with exit 0 after 116.6
seconds, and its guest console recorded root transition, workload start, and a
committed READY. The following public `exec` failed after 5.09 seconds with the
single fixed marker `timeout-source=run-lock-timeout`; its saved stdout was
empty and the `OCI exec did not complete: timeout` guidance was absent, so the
30-second guest exec deadline was not the boundary that expired. The failing
wait was a host run-lock acquisition. `MonitorClient.exec_request` takes that
lock both before and after each mailbox exchange, so whether the guest command
had already been admitted is not established, and the lock holder is not
identified by any preserved receipt. The run ledger separately recorded
`status` `failed` with the generic `OCI-root launch failed`, a handoff phase of
`failed` while its lifecycle receipt still read `ready`, and a monitor owner
journal of `control-lost` at revision 7. Those receipts establish that the
detached worker lost control after durable READY; their ordering against the
exec lock wait is not recorded, so the causal relation remains unestablished.
No new domain remained, and the pinned 17 inactive domains, 16 archive digests
and zero-active postflight were preserved.

PyTorch failed earlier, at `public-run-command`, after 300 seconds of wall
time, with the fixed coordinator code `[parent-response:timeout]`: the parent's
bounded coordinator-spawn response expired. Its run ledger stopped at `defined`
with no handoff and no monitor owner journal. The new `ml-pytorch-69afe41a`
domain (UUID `d20cd3df-768b-400a-a35c-ddbe011630f0`) is retained inactive with
autostart disabled, giving 18 total domains, zero active and unchanged archive
digests. It must not be stopped, removed or adopted.

Neither observed boundary is a guest execution deadline: PyTorch expired in the
coordinator-spawn handshake, and TensorFlow expired on a host run-lock wait.
The open diagnosis is that a post-READY worker failure collapses into one
generic message, and that the fixed coordinator-spawn (15 s), launch-authority
(60 s) and run-lock (5 s) bounds have not been evaluated against
multi-hundred-megabyte and multi-gigabyte materialization on this host.
The next diagnostic implementation now preserves a bounded post-READY worker
failure receipt in the run ledger and reports the Linux kernel's best-effort
`flock` holder PID on a run-lock timeout. The receipt contains only fixed
stage/source/category values; raw exception text, paths, argv, and guest output
remain excluded. The PID is a point-in-time diagnostic, not durable process
identity or proof of causality. Neither change alters the guest deadline,
monitor handshake, run-lock duration, retry, resource preservation, or cleanup
authority. A new exact-SHA native retry is still required to obtain these facts
for the pinned TensorFlow failure; the historical runs cannot be reclassified.

At exact checkout `06697bde83f4e0734955320577a59cc9c7e06f27`, the
timeout implementation from `5e9473a` and its test-isolation follow-up were
reverified on the same Linux host. The focused selections passed 605 checks
with four skips, then 186 additional checks; the packaged stage-1 completed
its 43-boot KVM control matrix in 122.35 seconds (one pytest node passed).

The pinned TensorFlow case then failed after 126.39 seconds at
`framework-exec-command`. Its saved stdout was empty and stderr contained only
`timeout-source=run-lock-timeout`. The calculation was issued with public
`exec --timeout 150`, so increasing the guest execution deadline from 30 to
150 seconds did not remove the separately bounded host run-lock failure. Since
the client takes that lock before and after mailbox exchange, the preserved
evidence still does not establish guest-command admission, execution, or
causal ordering. The archive hash was unchanged, no new domain remained, and
the exact 18-domain, zero-active, 16-archive baseline was preserved.

The pinned PyTorch case failed after 303.18 seconds at `public-run-command`.
Its saved stdout was empty; stderr contained the fixed
`[parent-response:timeout]` coordinator outcome plus a contemporaneous
`Domain not found` diagnostic for `ml-pytorch-8be2db32`. The later postflight
found that domain retained shut off and persistent with autostart disabled,
UUID `bfed8772-c656-41ac-8514-164c3e7bb00b`, and no interface, hostdev, or
host-filesystem device. That timing difference does not establish when the
definition became visible or why the coordinator response expired. The
archive hash was unchanged. Final postflight matched all 18 prior domain
names, UUIDs, states, and autostart settings plus this one retained domain:
19 inactive domains, zero active domains, and all 16 full archive SHA-256
values unchanged. Neither case reached a CPU tensor result, root-identity
comparison, or PID 1 refusal; no retained domain was stopped, undefined, or
adopted.

At exact checkout `2bb3a2dd70ad1b7e71765eb44a6bf43e4b0ed5cf`, the post-READY
failure receipt and Linux flock-holder PID diagnostics were verified natively
on `pieroot-server` (187 focused state/monitor/lifecycle/store checks passed in
47.78 seconds, followed by 166 monitor IPC, PCI, lane, and guard checks in
9.13 seconds). The pinned TensorFlow case failed after 125.65 seconds at
`framework-exec-command`. Its saved stdout was empty, but stderr recorded
`timeout-source=run-lock-timeout` and the exact kernel lock holder
`run-lock-holder-pid=1727406`. Host process observation confirmed that PID
1727406 was the detached monitor child worker (`palimpsest_local.oci_monitor_ipc
--private-child-v2 3 5`). Concurrently, the run ledger recorded
`status=failed`, `oci_root_handoff.phase=failed` despite receiving a durable
`ready` lifecycle receipt, and the new typed failure receipt:
`oci_root_launch_failure={"stage": "post-ready-worker", "source":
"lifecycle-transport", "category": "timeout", "schema":
"palimpsest.oci-root-launch-failure.v1"}`. The observed flock holder PID and
the post-READY worker timeout receipt are separate facts; they do not establish
that the transport timeout preceded the lock wait or that the lock was held
during failure handling. In source, exec invokes `before_stop_send` under the run
lock, and `_send_all` or `_recv_frame` can subsequently raise transport
`TIMEOUT`, so lock contention may precede the worker failure or share a common
root cause. Causal ordering, execution admission, and the specific transport
timeout site remain unresolved. The VM domain was cleanly cleaned up; no
new domain remained, and the 19-domain zero-active baseline was preserved.
The pinned PyTorch case failed after 297.04 seconds at `public-run-command` with
`[parent-response:timeout]` and empty stdout, leaving its ledger at
`status=defined` and retaining inactive domain `ml-pytorch-484e1dda` (UUID
`74cc9561-61cc-45d1-a070-a4262b6c73a9`, shut off, persistent, autostart
disabled). Postflight matched all 19 prior domain names, UUIDs, states, and
autostart settings plus this one retained domain (20 inactive domains, zero
active, all 16 archive SHA-256 values unchanged). Neither case reached CPU
tensor proof, root-identity check, or PID 1 refusal; no retained domain was
stopped, undefined, or adopted.

One preceding SSH orchestration attempt exited pytest code 4 before collection
because the remote working directory was not applied; zero tests ran and no
domain was created. It is not counted as a native case result.

The small-image control lane attempted for comparison could not run: the pinned
build artifact's `acceptance.json` still declares
`palimpsest.oci-root-build-run-acceptance.v1` while
`tests/kvm/test_oci_exec_cli_live.py` requires v2, so it failed during input
validation without creating a VM.

The two cases run sequentially with 8 GiB RAM, two vCPUs, and `network none`.
Public `exec` performs a deterministic 2-by-2 matrix multiplication with
single-thread framework settings, checks the exact result and sum, CPU device,
framework version, and absence of an available GPU, then verifies authenticated
root identity, direct PID 1 root refusal, NIC-, hostdev-, and
host-filesystem-free domain XML, normal stop/remove, and original archive
preservation.

The framework calculation is issued as `exec --timeout 150` so the guest
execution deadline (150 s) stays inside the harness's 180-second outer command
bound; the identity, PID 1 and lifecycle commands keep the default 30-second
deadline.

The production OCI-root launch window now gives the clean monitor child up to
30 seconds for its bounded spawn handshake and gives the parent coordinator up
to 60 seconds to receive and authenticate that result. This replaces the prior
15/30-second pair that the large PyTorch image repeatedly reached before guest
READY. It adds no retry, process termination, cleanup authority, device, or GPU
path; ambiguous outcomes still retain exact evidence. A native exact-SHA rerun,
not the local timeout-contract tests, determines whether the PyTorch CPU proof
can now reach the framework command.

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
