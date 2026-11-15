# Storage-backend comparison experiment plan

Status: design only. No experiment has run, and no production resource has been used.
Prepared on 2026-11-15 against the supplied Palimpsest stage-1 findings for `dev 7557727`.
This document defines measurements, not results, performance predictions, or a backend selection.
OverlayBD is a candidate execution/storage backend, not a replacement for composition policy.
No wholesale switch to OverlayBD is proposed.

## 1. Evidence and invariant inputs

All upstream documents listed below were re-read on 2026-11-15 from their linked primary sources.
The date is the evidence pin; moving branch URLs are not immutable commit pins.
Before execution, archive those documents and record exact source commits, package versions, and binary SHA-256 digests.
Published upstream benchmark tables are not measurements of Palimpsest and are not imported into this plan.
The supplied findings are code evidence, not a test pass or live qualification.
Any Palimpsest integration described below beyond those findings is proposed and **not verified**.

### Primary-source register
- **S1:** [OCI image configuration](https://github.com/opencontainers/image-spec/blob/main/config.md), “Layer DiffID” and “Layer ChainID / Definition.”
- **S2:** [OCI descriptors](https://github.com/opencontainers/image-spec/blob/main/descriptor.md), “Digests / Verification” and “SHA-256.”
- **S3:** [ACI userspace convertor](https://github.com/containerd/accelerated-container-image/blob/main/docs/USERSPACE_CONVERTOR.md), “Requirement,” “Layer/Manifest Deduplication,” and “Referrers API support.”
- **S4:** [BuildKit backend instructions](https://github.com/moby/buildkit/blob/master/docs/overlaybd.md), “Build Overlaybd Images.”
- **S5:** [OverlayBD README](https://github.com/containerd/overlaybd/blob/main/README.md), “System Requirements,” “Configuration,” “credential config,” and “Standalone Usage.”
- **S6:** [Standalone usage](https://github.com/containerd/overlaybd/blob/main/docs/standalone-usage.md), “Config file,” both kernel backends, “Notes,” and “Writable layer.”
- **S7:** [TurboOCI](https://github.com/containerd/accelerated-container-image/blob/main/docs/TURBO_OCI.md), “No image conversion,” “Configure,” and “Build.”
- **S8:** [Trace prefetch](https://github.com/containerd/accelerated-container-image/blob/main/docs/trace-prefetch.md), “Prefetch Mode,” “Terminology,” and “Example Usage.”
- **S9:** [Live snapshot](https://github.com/containerd/overlaybd/blob/main/docs/live-snapshot.md), “New Config Format,” “Failure Semantics and Crash Recovery,” and “Limitations.”
- **S10:** [ACI README](https://github.com/containerd/accelerated-container-image/blob/main/README.md), “Components,” “Release Version Support,” and “Overview.”
- **S11:** [ZFile format](https://github.com/containerd/overlaybd/blob/main/src/overlaybd/zfile/format_spec.md), “header,” “flags,” and “index.”
- **S12:** [File cache](https://github.com/containerd/overlaybd/blob/main/docs/cache.md), “Cache implementations.”
- **S13:** [QEMU image utility](https://www.qemu.org/docs/master/tools/qemu-img.html), `create`, `convert`, and `info --backing-chain`.
- **S14:** [Ceph RBD snapshots](https://docs.ceph.com/en/latest/rbd/rbd-snapshot/), consistency warning, “Layering,” and “Flattening a Cloned Image.”

### Palimpsest evidence anchors
These references identify the source sections recorded in the supplied 2026-11-15 findings, not new source audits.

- **P1:** [`oci_store.py`](../../src/palimpsest_local/oci_store.py), `DerivedSquashFSKey`, lines 276–397; parent-prefix test in [`test_oci_store.py`](../../tests/unit/test_oci_store.py).
- **P2:** [`buildkit.py`](../../src/palimpsest_local/buildkit.py), lines 541–573, 1831–1900, 2335–2359; [`block workflow`](../buildkit-block-workflow.md), lines 143–145, 201–217.
- **P3:** [`cli.py`](../../src/palimpsest_local/cli.py), lines 591–621; [`init.c`](../../guest/stage1/init.c); [`platform.c`](../../guest/vz/platform.c), lines 204–207, 294–306.
- **P4:** [`oci_converter.py`](../../src/palimpsest_local/oci_converter.py), lines 154–161, 347–379, 535–587; [`oci_changeset.py`](../../src/palimpsest_local/oci_changeset.py).
- **P5:** [`layer_cache.py`](../../src/palimpsest_local/layer_cache.py), lines 177–178, 423–450, 795–823; [`oci_store.py`](../../src/palimpsest_local/oci_store.py), lines 1036–1092, 1500–1579.
- **P6:** [`instance_bootstrap.py`](../../hub/src/palimpsest_hub/services/instance_bootstrap.py), lines 329–389; [`instances.py`](../../hub/src/palimpsest_hub/services/instances.py), lines 358–459, 566–677.

### Composition policy remains identical
A generation is one immutable, ordered source chain plus execution configuration and a recorded platform.
Pin the platform-specific manifest, config, compressed layer descriptors, ordered DiffIDs, and each prefix ChainID.
Resolve tags only during input admission; subsequent trials use digests and cannot follow tag movement.
Require source-faithful order, whiteouts, opaque directories, ownership, modes, timestamps, xattrs, and hardlinks; record unsupported cases instead of relaxing fidelity.
A parent change produces a new generation; it never edits an existing child, VM, or writable disk.
Metadata agreement, filesystem fidelity, ABI/kernel/driver compatibility, and application behavior are separate gates.
A passing ChainID check is not a passing application test. [S1; P1, P3, P4]
BuildKit command-cache reuse and runtime-format conversion reuse remain separate measurements. [P1, P2]

OCI defines `DiffID(L)` as the digest of the uncompressed layer tar, not necessarily the compressed blob digest.
It defines `ChainID(L0) = DiffID(L0)`.
It defines `ChainID(L0|...|Ln) = Digest(ChainID(L0|...|Ln-1) + " " + DiffID(Ln))`. [S1]
ACI's `overlaybd_layers` primary key is `(host, repo, chain_id)`, with `chain_id` described as the normal layer's chain-id; equality with OCI's recursion is **unknown** until pinned converter source confirms it. [S3]
**Parent-context cache identity is prior art, not a contribution of Palimpsest or this experiment.** [S1, S3]
Palimpsest's existing conversion key also binds the preceding descriptors/DiffIDs and conversion-tool contracts. [P1]

## 2. Backend mappings

Every backend receives the same admitted source bytes and the same ordered parent policy.
Keep a provenance ledger mapping source manifest, descriptors, DiffIDs, and prefix ChainIDs to derived artifacts.
A source identity retained in that ledger is not a claim that converted bytes retain the same digest.
No backend may reorder independent layers or bypass composition/compatibility gates.
VM attachment adapters for B3–B6 in this comparison are proposed and **not verified**.

### B1. Per-layer SquashFS plus guest OverlayFS
Map each admitted prefix to its existing parent-context-derived SquashFS artifact. [P1]
Retain source digests, DiffIDs, and ChainIDs as provenance; hash each SquashFS separately.
The SquashFS digest is not its source tar DiffID or compressed descriptor digest.
Conversion normalizes OCI changesets; the guest mounts read-only lowers leaf-first over the boot root; record each path's writable-upper placement. [P3, P4]
The guest sees a merged filesystem, not a single composited block image. [P3]
B1 cannot express cross-layer hardlinks, retains only `user.*`/`security.capability` xattrs, and has lower-count/option limits; OCI-root UID/GID fidelity is not live-proven. [P3, P4, P6; supplied findings]
Do not silently substitute a flattened image when B1 reaches its documented limit.

### B2. BuildKit one-pack merged-rootfs SquashFS
Use the same pinned OCI result as input, not a fresh Dockerfile build that might change dependencies.
Flatten the pinned chain using a new, **not verified** adapter, then use the existing one-pack packer; qualify whiteout, opaque-directory, and hardlink handling before timing. [P2, P4]
Retain the full source chain in provenance; the merged tar and SquashFS have new digests.
Original DiffIDs and ChainIDs remain source identifiers, not identities of the flattened artifact. [S1; P2]
The guest sees one read-only filesystem with a private upper; this experiment adapter is **not verified**.
The artifact cannot expose independently mounted source layers or preserve layer boundaries for selective runtime replacement.
Measure flattening and packing costs; do not classify either as BuildKit command execution. [P2]

### B3. OverlayBD local, all derived blobs present
Proposed mapping: convert each ordered source prefix to a native block layer and retain the chain ledger. [S3, S10]
Conversion is blocked until pinned source confirms non-argv credential paths for both converter and dedup database; S3 documents argv credentials, not a safe alternative. [S3]
Forbid `--plain`, `--insecure`, secrets in argv, and secrets in repository files; approved runtime credential material lives outside the repository. [S3, S5]
Use the same converter options and derived blobs for B3 and native B4; only placement/cache policy changes. [S3, S6]
ACI stores the normal layer's documented chain-id and derived `data_digest`; do not assume the former's exact computation without source confirmation. [S3]
Preserve original digest/DiffID/ChainID in provenance; do not label converted layers with original content identities. [S1, S3]
Standalone `lowers` are bottom-to-top; local entries use `file`, and the result is a virtual block device. [S6]
Proposed VM exposure uses one block device with ext4 and a private writable top; guest attachment is **not verified**. [S3, S6]
Freeze `--fstype=ext4`, `--mkfs`, and `--vsize` explicitly with matched capacity; S3 defaults are ext4 and 64 GB, not evidence of this experiment's configuration. [S3]
Use a fresh dedup database for each converter build/option set and one repository per workload's generations; record exact row hits and repo scope. [S3; P1]
There is only one writable layer, always at the top; arbitrary changeset rebasing is not an advertised interface. [S6]
Block-layer composition does not establish arbitrary source-layer compatibility; retain the common gates. [S1, S3, S6]

### B4. OverlayBD registry-backed, without and with trace prefetch
Native B4 uses B3's bytes: image-level `repoBlobUrl`, per-lower `digest`/`size`/download `dir`, and separate global `cacheConfig.cacheDir`. [S5, S6]
The guest-facing proposal is a block device containing ext4 from the explicitly frozen converter options, not guest OverlayFS lowers. [S3, S6]
Preserve the same source identity ledger and separately pin the derived manifest and blob digests. [S1, S3, S6]
Run B4-N without a trace and B4-T with a separately pinned trace artifact. [S8]
The trace stays uppermost; remove inherited traces when deriving an image, and record a new trace only for B4-T. [S8]
Trace recording creates a temporary container; declare its time limit and record its actual termination cause (timeout or signal). [S8]
Network-enabled recording is a separately labeled preparation step: use disposable hosts with egress limited to the isolated registry, no reachable production data/credentials/endpoints. [S8]
Count replay time/bytes inside Fetch; report recording/publishing/storage un-amortized, a break-even start count, and any amortization using a pre-registered reuse count. [S8]
Use a training run distinct from measured requests; test a changed request distribution to detect trace overfitting.
S6/S8 provide no standalone trace procedure: B4-T is blocked until pinned-source record/replay mechanics are found and qualified; S8's examples use containerd/snapshotter. [S6, S8]
No claim of improved startup follows from the existence of trace support. [S8]

B4-U is an optional, separately labeled TurboOCI sub-arm, not native B4 with conversion costs hidden. [S7]
TurboOCI reads the original unmodified OCI image using a separate ext4 metadata image. [S7]
**Analysis:** original-image signatures do not cover the ext4 metadata image; pin and fully verify it as unsigned derived content unless separately signed under an explicit policy. [S2, S7]
Its original blob digests and DiffIDs remain source identities; pin the additional metadata image independently. [S1, S7]
The docs describe metadata size as usually about 3%; measure actual bytes and do not adopt that figure as a result. [S7]
Freeze gzip cache enablement, directory, size, and refill size; charge actual decompression/cache costs rather than assuming the optional cache is always enabled. [S5, S7]
Format `0.1.0-turbo.ociv1` requires OverlayBD >= v0.6.10 according to ACI's version table. [S10]
Unknown: equivalence of native and TurboOCI filesystem semantics for these exact fixtures; test both independently. [S3, S7]
Every B4-N/B4-T/B4-U trial carries one of two proposed trust-tier labels; these are experiment rules, not upstream guarantees. [S2, S6, S8, S11]
**T-A, admissible:** verify complete source and derived blobs before any guest read; charge every required download and verification.
Native B4-N T-A uses a harness full-blob fetch/hash into immutable `file` lowers with background downloads disabled; it has no lazy read path and C2 duplicates B3's local execution control. [S2, S6; proposed harness]
B4-T/U T-A stay blocked until a fully verified local-consumption path is qualified; do not assume registry `digest` fields implement that gate. [S6, S8, S11]
**T-M, measurement-only lazy:** separately approved disposable hosts and synthetic workloads may expose the pre-digest window for analysis. [S6, S10, S11]
T-M records the first guest read, full-digest completion, trusted components, and any bytes consumed before verification.
T-M must still perform eventual full-blob verification; CRC, TLS, and registry-side hashing are not replacements. [S2, S11]
T-M is **NOT comparable** to T-A and **NOT deployable**; it is never product-admissible or adoption evidence.
If T-M approval is absent, mark pre-digest cells blocked; this plan grants no permission to run either tier.

### B5. qcow2 with an immutable backing file
Build prefix N by applying layer N on a fresh writable child of frozen prefix N−1; record each child's allocated bytes, rather than independently allocating each prefix.
Use immutable backing files and a private qcow2 child per trial; before each start hash the closed backing chain, enforce read-only plus immutable flags, and charge verification. [S13]
Preserve OCI digests/DiffIDs/ChainIDs in provenance; qcow2 files receive their own hashes and backing identities.
The guest sees a block device containing the same filesystem tree, not OCI tar changesets.
Backing pointers do not themselves verify OCI parent identity or implement whiteout application. [S1, S13]
Neither safe nor `-u` rebase means applying a new OCI parent under existing child changes; a changed parent requires a new prepared chain. [S13]
Record filesystem allocation/metadata differences; require semantic fidelity, not identical block bytes across formats.

### B6. Ceph RBD clone, only on an isolated existing test cluster
Materialize each prefix into format-2 RBD, unmount or fsfreeze before snapshot, protect before cloning, and pin the exact parent snapshot. [S14]
Record pool/image/snapshot/parent/features/release plus a materialization `rbd export` SHA-256; re-export/recheck immutable parent content and protection before each trial, charging verification. [S14; proposed policy]
Pin source identities in provenance; RBD object/snapshot identifiers are not OCI digests, DiffIDs, or ChainIDs.
The guest sees a block device containing the admitted filesystem; OCI semantics require preparation above RBD. [S1, S14]
Clones depend on parent snapshots; flattening removes that dependency by copying parent data. [S14]
Keep referenced parents protected and immutable; account for snapshot retention, replication, and eventual snaptrim. [S14]
Without a cluster containing no production pools, clients, or OSDs and explicit approval, B6 is unavailable; pool-scoped credentials alone are insufficient.
B6 is the storage mechanism only. How Nova, Cinder and Glance would expose such clones to an instance (one read-only clone volume per layer, a user-owned writable volume) is reviewed separately in [openstack-shared-readonly-layers.md](openstack-shared-readonly-layers.md); its functional check through ordinary tenant APIs is not a B6 measurement and does not lift the isolated-cluster rule above.

### Native BuildKit support is a separate build-path check
ACI states support for OverlayBD image building since BuildKit v0.22; this is a build feature, not runtime or VM-attachment qualification. [S10]
BuildKit requires active `overlaybd-tcmu` and `overlaybd-snapshotter` for its documented path. [S4]
Start `buildkitd` with `--oci-worker-snapshotter=overlaybd` and the documented proxy-snapshotter socket. [S4]
BuildKit documents that an OverlayBD `FROM` image yields OverlayBD output; image output needs `oci-mediatypes=true,compression=uncompressed`. [S4]
A build produced this way is not automatically byte-identical to the common pinned source image. [S1, S4]
Treat this as an optional build experiment; exclude it from execution comparisons unless common inputs and fidelity are established.
Do not confuse rootless userspace conversion requirements with runtime device or native BuildKit requirements. [S3, S4, S6]

## 3. Experimental design and workloads
Everything below is a proposed procedure; none of these trials has run.
Freeze input manifests, commands, seeds, readiness checks, request bodies, timeouts, and measurement scripts before execution.
Use Linux amd64 for the common VM cohort; do not compare a macOS VZ run directly with Linux KVM timings.
Fix guest kernel, vCPUs, RAM, firmware, network, and application configuration; record each arm's boot root, initrd, root assembly, and writable-upper location.
Charge B1/B2 runtime-base qcow2 overlay preparation where present; B1 OCI-root's exact boot path is **not verified**; record differences and require a same-initrd SquashFS/block-root matched control. [P2, P3]
Fix primary B3/B4 frontend as TCMU; ublk/ublkd are separate exploratory factors, never pooled; pin `--cache-dir`/`--instance-id` where applicable because per-device and centralized cache topology differs. [S6]
Keep writable capacity, durability flags, filesystem mount options, and workload datasets fixed.
Do not conceal format-required filesystem differences; report them as confounders and add matched controls where possible.

### Workload set
1. **Small service:** pin a small HTTP service image; verify a response body digest and one read-only asset.
   Readiness means a successful health response; the first measured request uses a distinct endpoint.
2. **Python-import-heavy:** pin packages and import order; record import completion and a deterministic inference-free HTTP response.
   Disable uncontrolled bytecode writes or reset them identically; capture imported-file counts and bytes.
3. **ML large-file image:** pin CPU framework and synthetic model weights; read sparse and full-file working sets separately.
   Validate fixed numerical output and model checksum; no GPU allocation, model download, or external dataset is permitted.
4. **Write-heavy:** pin a transactional SQLite service plus a seeded file workload with rename, overwrite, delete, and fsync.
   Validate committed transaction count and content after restart; separate application files from immutable source data.

Before timing, compare final trees: content hashes, uid/gid, modes, xattrs, capabilities, device nodes, and hardlink inode groups.
Include whiteout, opaque-directory, same/cross-layer hardlinks, `security.selinux`, `trusted.*`, and `system.posix_acl_*` xattr fidelity fixtures. [P4; proposed fixtures]
Classify unsupported semantics explicitly; do not weaken fixtures to manufacture a common passing subset.
Run common supported images as performance cohorts; report rejected images in a separate coverage table.
Test parent changes as new generations: unchanged chain, changed early prefix, changed leaf, and configuration-only change.
Record actual conversion reuse and BuildKit reuse separately; never infer command reruns from a conversion cache miss. [P1, P2]

### Cache and network matrix
- **C0-N, never converted:** only pinned source OCI content exists in the private source registry; no workload-derived artifacts exist in any host/store/cluster namespace.
- C0-N includes every arm's conversion/preparation, B4 publication/push, and B6 import/snapshot/protection/clone before first response; it is the primary cold endpoint.
- **C0-P, derived published/host cold:** immutable derived inputs already exist in the arm's declared staging store/registry/cluster, but host/range/page caches are empty; preparation is reported separately.
- **C1, source-warm:** verified source OCI blobs exist locally, but workload-derived artifacts are absent everywhere; include conversion/publication as in C0-N.
- **C2, artifact-warm:** verified derived artifacts present; guest and host page caches cold; per-instance writable state fresh.
- **C3, working-set-warm:** one unmeasured startup primes the declared cache; the next instance still has fresh writable state.
- **C4, partial/evicted:** create a disposable cache-root copy with a recorded subset of fetched ranges omitted; never evict from shared/permanent caches.
- Split every B4 cache/network/trace cell by trust tier; never aggregate T-A and T-M samples. [S2, S6, S8, S11]
- T-A C0-N/C0-P/C4 must obtain complete derived bytes and verify them before guest reads; B6 verifies its pinned snapshot export instead of pretending to cache registry ranges.
- Pre-digest startup/replay and post-ready digest completion belong only to approved T-M diagnostics; T-A can replay traces after verification. [S2, S8]
- Record converter database state, registry/CDN cache, filesystem cache, RBD client/OSD cache, and QEMU caches independently.
- A cold state requires evidence of reset/isolation; an uncontrollable cache is “unknown,” not “cold.”
- Obtain B1 cold state with fresh disposable state roots or clones of frozen roots; never delete entries from a shared/permanent layer cache. [P5]
- Other cold caches also use fresh scratch roots, including registry/gzip/ublk caches and lower download directories; never clear shared caches. [S5, S6]
- Verification itself warms caches: retain verified artifacts, reset page cache afterward, and time verification separately.
- Never drop caches on a shared host; use disposable dedicated hosts or explicitly approved per-trial host resets.
- Test synchronized cohorts of 1, 4, and 16 starts, only within approved capacity; record queue and service times separately.
- Network profiles are fixed: 1 Gbit/s and 1 ms RTT; 100 Mbit/s and 50 ms RTT; 25 Mbit/s and 150 ms RTT with 1% loss.
- Add registry HTTP 503 at a seeded 10% rate, a 30-second outage, denied credentials, and a broken range response.
- Apply network faults only on the isolated registry path, never the management/control path.
- Use the same profiles for all registry fetches; classify local/B6 data paths separately rather than inventing registry traffic.
- OverlayBD downloads may fetch whole blobs after launch; freeze delay/delayExtra/rate, with disabled/immediate/delayed download as separate arms. [S5]
- Before another trial require torn-down prior devices, no live download tasks, idle snaptrim, and flushed dirty pages; otherwise invalidate and stop scheduling. [S5, S6, S14]
- Record prefetch concurrency and cache budgets; never allow an unbounded B4 cache while capping B1. [S5, S8]
- Freeze `cacheConfig.cacheType=file` (`ocf`/`download` are separate unqualified variants) and `p2pConfig.enable=false`; do not silently enable dadip2p. [S5]

### Repetitions and order
Use 30 pilots per eligible cell only to validate instrumentation/resource bounds; freeze an expanded cell manifest and exact resource totals before approval or execution.
Confirmatory cells are T-A {B1,B2,B3,B4-N,B5,B6-if-available} × four workloads × {C0-N,C2} × baseline network × cohort 1, with downloads disabled: 1,000 cohorts/cell.
Exploratory normal-performance cells are the remaining eligible backend/cache/network/cohort/download tuples, including separately approved T-M and qualified B4-T/U: also 1,000 cohorts/cell.
The approval manifest must enumerate every tuple, excluded reason, frontend, boot path, n, and maximum starts/bytes/runtime; no implicit Cartesian-product campaign is authorized.
Parent-change/reuse cases each receive 30 independent conversions per workload/arm/change type; conversion-preparation measurements receive 30 repetitions per arm/input/policy.
Split confirmatory cohorts across at least five time blocks; pre-register the full tuple list and its total before approval, not after seeing outcomes.
A concurrent cohort is one independent unit; do not treat its correlated guests as independent repetitions.
Use a recorded random seed to randomize backend order within each workload/cache/network/time block.
Balance order with a Latin square; restore declared states through fresh disposable roots or frozen-root clones, never by deleting shared/permanent cache entries.
Report normal-performance p50/p95/p99 only from the 1,000-cohort cells, with counts and block-bootstrap intervals; publish raw tails and interval width because p99 still rests on few samples.
For 30-sample pilots, conversion/reuse, fault, and lifecycle cells report p50, the full sample/outcome list, and maximum/censoring; p95/p99 are unavailable, not inferred from the largest observation.
Keep failures/timeouts in the denominator; report censored latency bounds rather than excluding unsuccessful starts.
Do not extrapolate missing cells; unavailable B6 or blocked B4 remains unavailable or blocked.

## 4. Measurement boundaries and collection

Use one coordinator monotonic clock for end-to-end intervals; correlate guest events through timestamped serial/agent messages.
Record clock synchronization error for cross-host events; never subtract unsynchronized wall clocks.
Emit structured events with run ID, generation, backend, cache state, phase, timestamp, and byte counters.
Measure each phase independently even when phases overlap; summed phase times need not equal end-to-end time.

| Phase | Start and end | Collection |
| --- | --- | --- |
| Fetch | First request until required bytes arrive; record post-ready traffic separately. | Client counters plus registry access logs, HTTP range/status/length, and NIC deltas. |
| Verification | Descriptor size/hash work begins until source and derived admission checks finish. | Dedicated monotonic spans, byte counts, digest receipts, and verifier CPU. |
| Conversion | Verified source admitted until derived format is atomically published. | Converter child exit, output hashes/sizes, peak scratch bytes, and cache-hit records. |
| Device/filesystem preparation | Admitted artifacts until devices and mounts are ready for VM use. | Adapter events, device inventory, mount completion, and errors. |
| VM boot | Hypervisor start request until a fixed guest-ready serial event. | Hypervisor lifecycle events plus the same guest boot marker. |
| Application-ready | Guest-ready until the fixed health probe succeeds. | Coordinator probes with identical interval and validated payload. |
| First request | One predeclared post-readiness request sent until full validated response. | Client monotonic timestamps, status, bytes, and content digest. |
| Steady-state I/O | Fixed five-minute warm-up followed by ten-minute workload. | Application throughput/latency and fio JSON for a separate declared I/O fixture. |

Conversion costs are arm-specific: B1 normalize/pack; B2 flatten/pack; B3/native B4 convert; B4-U build/publish metadata; B4-T record/publish trace; B5 apply to backing children; B6 materialize/snapshot/protect/clone. [P1, P2; S3, S7, S8, S13, S14]
Report every preparation's wall time, CPU, network/allocated bytes, and n un-amortized beside start latency; avoid double-counting clone/device spans.
Report end-to-end source-cold admission-to-first-response and artifact-warm start-to-first-response separately.
Do not redefine “fetch complete” as “all data downloaded” for one backend and “indexes loaded” for another.
For lazy T-M diagnostics, include bytes/time through readiness, first request, steady state, and a fixed 15-minute post-ready horizon. [S5, S10]
Record full materialization and digest completion beyond that horizon, or “not completed” at the declared timeout; T-A digest completion precedes guest reads. [S2, S5]
Apply one harness verification policy to every T-A backend, not whatever its product path happens to check: source descriptor/DiffID admission once per immutable input generation.
Before every T-A start re-hash all complete read-only derived inputs, boot artifacts, and parent bindings; include those checks in end-to-end time, then establish the declared page-cache state.
For B5 this includes all closed backing files; for B6 use pinned-snapshot export checks; T-M's delayed derived verification remains explicitly non-comparable. [S2, S13, S14; proposed policy]

Collect user/system CPU seconds with cgroup v2 `cpu.stat` and process accounting per converter, daemon, QEMU, and workload.
Also collect host-wide `/proc/stat`, softirq/kworker/writeback CPU, and registry plus OSD/MON CPU, memory, and network over each trial window, separately from process cgroups.
Collect peak RSS, cgroup `memory.current`/`memory.peak`, page-cache attribution, and system memory at 100 ms intervals.
Collect host block bytes/IOPS/latency through cgroup `io.stat`, device counters, and a fixed-version tracing tool.
Collect allocated and apparent host bytes separately using `stat`/`du`; inventory shared immutable and private writable data.
Count source blobs, converted blobs, traces, metadata, scratch, caches, logs, and retained parents at each phase boundary.
Measure registry logical unique-blob bytes and actual backend allocation, including replicas/versioning; record reference counts.
For RBD, record `rbd du` and isolated-pool allocation, replication/erasure coding, retained snapshots, and snaptrim delay. [S14]
Report shared-base cost once plus marginal cost at 1/4/16 instances; never charge it once per clone in one backend only.
Preserve per-run raw samples, failures, tool versions, settings, and checksums; redact credentials and private registry tokens.
Record CPU model/microcode, NUMA, RAM, storage model/firmware, filesystem, NIC, kernel/config/modules, QEMU/libvirt, and Ceph versions.
Pin CPU governor, affinity, hardware acceleration settings, security mitigations, and background load; log thermal throttling.
All instrumentation is planned, **not verified** in Palimpsest; missing counters must be reported as missing, never estimated silently.

## 5. Remote-lazy trust and operations contract

This analysis is separate from timing. A fast run cannot waive an integrity or lifecycle requirement.
Official documentation is evidence of an interface, not proof of its behavior in Nova or this deployment.
The following tests are proposals for the isolated environment, not claims that they passed.

### Integrity: cryptographic admission is mandatory

**Official:** ZFile has per-block CRC32 only when `verify` is set; index CRC is checked when digest flag bit 4 is set; standalone compression is optional. [S6, S11]
Record each blob's actual format, `verify`, and digest flags; absent flags/non-ZFile data imply no documented ZFile per-read protection, not automatic CRC coverage. [S6, S7, S11]
OCI descriptor verification hashes complete content; SHA-256 support is mandatory, while the spec phrases consumption checks as SHOULD. [S2]
**Analysis:** CRC is not a collision-resistant authenticity proof and cannot replace the whole-blob SHA-256 gate. [S2, S11]
Before a full digest check, range readers depend on the registry, TLS endpoint, converter, metadata/index, cache, and host remaining trustworthy.
**Unknown:** whether the selected runtime verifies a complete blob, when it does so, and whether corrupt ranges reach a guest first. [S5, S6, S10, S11]
**Test:** flip payload/index bytes, truncate blobs, return wrong ranges, and alter data with matching CRC; require fail-closed admission.
For product admission and T-A, verify source compressed blobs, uncompressed DiffIDs, and every derived blob before guest consumption.
A server-side digest check or a source signature does not prove newly fetched converted ranges match a client's expected derived digest.
Cold T-A B4 is blocked until this unchanged contract is satisfied; downloading everything first remains in its timing. [S2, S6, S11]
T-M may observe synthetic pre-digest guest reads only under its separate approval; measure the exposure window and eventual verification.
T-M outputs cannot count as accepted application starts or justify product use; do not mix them with equal-trust comparisons.
No authenticated per-block proof scheme is established by these sources; do not invent one or relax full-digest verification. [S2, S11]

### Credentials and scope
**Official:** credentials must exist before launch; absent a valid refreshed credential, OverlayBD “keeps reloading until a valid credential is set.” [S5]
File/HTTP/HTTPS/UDS modes are documented; helper socket permissions are caller-managed; protected credential material must remain outside the repository. [S5]
**Unknown:** tenant binding, redirects, revocation latency, redaction, and guest I/O behavior during indefinite credential reload. [S5]
**Test/contract:** use synthetic short-lived pull-only credentials scoped to one repository and generation; never pass secrets in argv.
Test expiry/revocation, wrong-repository requests, helper denial, TLS errors, and cross-tenant access; an unbounded guest hang blocks acceptance.
Require allowlisted registry/helper origins, protected credential files/sockets, no guest credentials, and redacted logs.

### Cache poisoning, corruption, and sharing
**Official:** S6's ublk notes prohibit shared cache directories between per-device daemons because file-cache locking is in-process only; S12 specifies no locking contract. [S6, S12]
The documented centralized ublk daemon shares one ImageService/cache tree under an exclusive lock. [S6]
**Unknown:** cross-tenant authorization, cache-key completeness, crash-atomic range publication, and poisoned-cache recovery. [S6, S12]
**Test/contract:** isolate tenants and daemon cache roots; share only fully verified immutable artifacts under an explicit authorization check.
P2P is disabled: S5 documents a dadip2p proxy, not a qualified tenant-sharing/authentication contract; enabling that peer data path would require a separate trust analysis and poisoning tests. [S5]
Inject stale/truncated cache extents, same-name different-digest content, eviction during reads, and concurrent device creation.
Require corruption detection before guest consumption, no unauthorized cache hit, and measured clean recovery without deleting referenced data.

### Daemon crash, restart, and offline start
**Official:** the README recommends service restart after unexpected crashes; this is not a zero-I/O-loss guarantee. [S5]
The standalone one-device ublk process loses its device when killed; unmount before teardown to avoid aborted journal writes. [S6]
**Unknown:** TCMU versus ublk guest I/O errors, timeouts, durability, device identity, and restart recovery under this VM attachment. [S5, S6]
**Test/contract:** in disposable VMs only, stop/crash each selected daemon during reads and fsync; verify acknowledged writes and bounded failure.
Offline behavior is **unknown** for partial caches; local-file configurations and background full downloads are documented. [S5, S6]
Test egress-denied startup/workload on C0-N/C0-P/C2/C3/C4 by tier; T-A incomplete closure must block before execution, while T-M observes only approved synthetic failures.
Require a declared verified offline dependency closure; a successful health check alone does not establish offline completeness.

### OpenStack Nova device lifetime

**Official scope:** generic VM/block/SCSI applicability and standalone devices are documented, not a Nova/Cinder lifecycle contract. [S5, S6]
Nova integration for this plan is **not verified**; every row below is an unresolved acceptance test, not supported behavior.

| Operation | Official standalone statement | Unknown | Isolated test and required evidence |
| --- | --- | --- | --- |
| Attach/detach | TCMU devices may be exported over iSCSI; duplicate writable mounts are rejected; daemon-owned ublk devices require API deletion. [S6] | Nova ownership, hotplug, persistent attachment, and teardown ordering. | Bind generation/device/VM IDs; reject double writers; quiesce before detach; confirm authoritative absence. |
| Guest reboot | No guest-reboot contract is stated in the reviewed standalone/snapshot docs. [S6, S9] | Host device persistence and filesystem/write recovery. | Reboot with committed transactions; verify identity and acknowledged data before/after. |
| Live migration | TCMU devices can be used remotely through iSCSI; no Nova migration protocol is specified. [S6] | Destination daemon/cache/auth setup and writable ownership transfer. | Migrate between isolated computes; record fencing, reads, writes, and no dual writer. |
| Snapshot | Live snapshot seals the old upper and adds a different writable upper; process-crash recovery has a config commit point. [S9] | Nova snapshot mapping, application consistency, and restore dependency closure. | Freeze workload, snapshot, restore elsewhere, and prove immutable parents remain reachable. |
| Shelve/unshelve | No shelve/unshelve contract is stated in the reviewed standalone/snapshot docs. [S6, S9] | Device reclamation and reconstruction on another host. | Unshelve with source compute unavailable; verify generation and durable writable data. |
| Resize/revert | `overlaybd-ublkd` online resize is grow-only, writable images only, and requires `UBLK_F_UPDATE_SIZE` (mainline >= 6.11). [S6] | Nova resize/revert mapping, device reconstruction, and rollback ownership. | Distinguish flavor resize from volume growth; test confirm/revert and permitted device growth; verify one owner and data. |
| Host restart | Snapshot recovery covers process crashes only; host power loss is out of scope. [S9] | Unclean-host recovery and stale device/control records. | Use disposable hosts; verify fail-closed fencing and reconcile device/VM/reference inventories. |

OverlayBD live snapshots are documented, but are not automatically Nova snapshots or application-consistent backups. [S9]
The config update is the documented commit point; recovery covers process crashes, not host power loss. [S9]
Hybrid/sparse uppers require callers to pause writes; warpfile uppers are unsupported; per-device snapshots serialize. [S9]
Enable the loopback snapshot API only on disposable hosts with local access restricted; S5/S9 document no authentication, so assume any local process can call it until proven otherwise.
The documented snapshot example uses a TCMU device ID; ublk live-snapshot support is **unknown** and requires pinned-source confirmation and qualification. [S9]
Test quiescence, interrupted seal/config updates, retry idempotence, and restore; do not infer power-loss guarantees. [S9]

### Garbage collection, references, and observability

**Official:** cache eviction/configuration and startup result files exist; they do not establish a Palimpsest generation-reference GC. [S5, S6, S12]
**Unknown:** coordinated reclamation across converted layers, traces, metadata, registry blobs, caches, devices, and Nova attachments. [S3, S6, S8, S12]
**Test/contract:** record references from admitted generations, active devices, retained snapshots, in-flight starts, and writable disks.
Simulate deletion/restart races; retain anything referenced or uncertain; require authoritative absence before reclaiming owned artifacts.
**Official:** logs, audit output, and a configurable Prometheus exporter are documented. [S5]
**Unknown:** counter coverage, tenant attribution, phase boundaries, and visibility of CRC/full-digest failures. [S5, S11]
**Test/contract:** reconcile exported counters with registry logs and injected failures; require generation/device/run correlation without secrets.

## 6. Preconditions, approvals, and stop rules
This document authorizes no host, kernel, registry, cloud, VM, device, or credential mutation.
Obtain explicit approval for exact hosts, source ref, kernel modules, private registry, tenant, volume budget, duration, and cleanup.
Use dedicated non-production Linux hosts, private registry/storage, synthetic identities/data, and no production tenants or credentials.
Use two identical compute hosts for migration tests; otherwise mark migration unavailable.
Runtime needs approved root privileges: TCMU (`target_core_user`, configfs, `tcm_loop`) or ublk (`ublk_drv`, mainline >= 6.0 or documented backport); ublkd's control socket is root-only. [S6]
The ublk online-device-growth subtest additionally needs `UBLK_F_UPDATE_SIZE` (mainline >= 6.11); Nova flavor resize is a separate operation. [S6]
S6's 5.10 verification/pending-mainline-regression caveat applies to ublk only; record its backport DISCARD absence and ~2-second teardown as workload/timing confounders. [S6]
The standalone converter is experimental and needs no containerd/TCMU/root; trace preparation needs containerd/ctr (>=1.6 for labels), overlaybd-snapshotter, and `rpull --download-blobs` before recording. [S3, S8]
Require isolated cache directories, bounded daemon resources, scoped pull/push identities, and a disposable management/control plane. [S5, S6]
B6 requires an existing Ceph cluster with no production pools, clients, or OSDs, plus pool-scoped permission; shared-production infrastructure is forbidden.
Approve concurrency as a campaign exception where needed; ordinary cleanup gates still block uncertain residual resources.
Delete only exact-owned disposable consumers after evidence capture; preserve foreign, uncertain, retained, and HOLD-marked resources.

Stop immediately on digest mismatch, semantic mismatch, unauthorized data access, lost acknowledged writes, or incorrect generation identity.
Stop on kernel fault, host instability, production reachability, escaped resource limits, unredacted credentials, or unexplained dual ownership.
Stop further allocations on incomplete cleanup, an unresolved HOLD, or capacity below the approved reserve.
Stop an affected cell when its cache/network state cannot be established; preserve evidence and label it invalid, not successful.
Missing required lifecycle or integrity guarantees block backend acceptance even if isolated timing samples exist.

“No advantage” is a possible decision, not an expected result.
Pre-register each workload's sole primary endpoint: T-A C0-N admission-request-to-first-validated-response p95 versus B1, including every preparation/publication/verification step, with 10% useful-change and 5% regression thresholds.
Use Bonferroni-adjusted block-bootstrap intervals for family-wise 95% coverage across the frozen primary comparisons; C2 and every other endpoint are exploratory.
Report no demonstrated practical advantage when the confidence interval does not establish that threshold under the same trust contract.
Also report no acceptable advantage when any gain requires weaker verification, lost semantics, unsafe operations, or unapproved resources.
Report tradeoffs separately when startup, steady-state, bytes, CPU, memory, or retained storage move in different directions.
Wide intervals or missing eligible cells mean inconclusive, not equivalence; never claim superiority from upstream numbers alone.

## 7. Mapping to existing Palimpsest limits
The findings record KVM's 24-lower limit, VZ's 128-lower limit, and Hub's mount-option string below 4096 bytes. [P3, P6]
They also record unsupported cross-layer hardlinks, assumed cross-host flock behavior, and no permanent-cache TTL/LRU/refcount GC. [P4, P5]

| Backend | Relationship to those limits; all new integration remains not verified |
| --- | --- |
| B1 | Retains lower/option/hardlink/flock/GC limits, only `user.*`/`security.capability` xattrs, and not-live-proven OCI-root UID/GID fidelity. [P3–P6; supplied findings] |
| B2 | One pack changes mount fanout, not source-chain validity; test hardlinks, preserve flock assumptions, and add no implicit GC claim. [P2, P4, P5] |
| B3 | Block stacking differs from guest lower mounts; official hardlink support is not Palimpsest qualification; cache locks are not cross-host flock. [S6, S10; P3–P5] |
| B4 | Same B3 questions plus registry/cache/trace references; documented cache eviction does not supply generation GC. [S5, S6, S8, S12; P5] |
| B5 | Backing chains replace guest multi-lower assembly only in the proposed adapter; application hardlinks and parent verification still need tests. [S13; P3, P4] |
| B6 | RBD clone references are distinct from OCI lineage and Palimpsest cache references; snapshot protection does not implement global GC. [S14; P1, P5] |

A backend change cannot retroactively raise product limits, repair cross-host locking, or supply safe reclamation without corresponding implementation and evidence.
The final report must distinguish existing implementation, proposed adapters, measured outcomes, unresolved unknowns, and rejected configurations.
