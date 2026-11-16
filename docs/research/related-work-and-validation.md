# Related work and validation design

## Scope and evidence boundary

This document prepares a comparison and experimental protocol, not an evaluation report.
The research subject is defining, building, composing, verifying, sharing, and lifecycle-managing research and cloud execution-environment layers.
OCI is an important representation of those environments, not the purpose of the system.
No novelty, measured speedup, storage advantage, or completed experiment is claimed here.

Palimpsest statements below use the supplied stage-1 findings for `dev 7557727`, dated 2026-11-15.
The findings distinguish enforced source contracts, named tests, incomplete work, and historical live-evidence limits.
This document does not convert a source finding or the existence of a test into a test-passed claim.
Other Palimpsest behavior is **not verified**; proposed experiments below are plans, not evidence.
Public primary sources were opened and read for this comparison on 2026-11-15.
The current OverlayBD documentation is distinct from the historical DADI paper; the documentation revisions read here are pinned below and experiment revisions must also be recorded.

The branch that carries this document (`feat/layer-lineage-diagnostics-20261115`, unmerged, not released) adds three things after those findings. They are named here so that "today" is not misread, and they do not change any claim boundary below:

- a read-only local plan, `palimpsest layer impact`, that lists the recorded descendants of a layer or base image in one local store and reports rebuild, reconversion, transfer and deployment as separate states; it rebuilds nothing and does not see a Hub or another host;
- a staged diagnosis inside that plan in which only the structure stage (kinds, declared architecture, base binding) has evidence; the filesystem, ABI, kernel/device and smoke-test stages are reported as `unverified`;
- a fix for the VZ commit whiteout-order defect [P9], with unit tests; it has not been exercised on a VM.

Automatic descendant rebuild, a Hub-side reverse lookup, declared provides/requires/conflicts, a composition lock, a smoke-result store, and any rejection of a plan remain not implemented.

A **generation** means a selected immutable environment definition and its exact referenced artifacts, not a snapshot of application data.
A **build step** executes a recipe action; a **conversion** changes an existing artifact's representation without rebuilding the application.
A **compatibility check** must name its scope: identity, filesystem structure, platform metadata, ABI, kernel/driver, or application behavior.
A successful structure check is not a successful runtime test.
A successful runtime test covers only its workload, environment, and observation window.

## Primary sources and local evidence locators

- [S] Potter and Nieh, [*Improving Virtual Appliance Management through Virtual Layered File Systems*](https://www.cs.columbia.edu/~nieh/pubs/lisa2011_vlfs.pdf), LISA 2011. Page references below use PDF pages 1–14.
- [D] Li et al., [*DADI: Block-Level Image Service for Agile and Elastic Application Deployment*](https://www.usenix.org/system/files/atc20-li-huiba.pdf), ATC 2020, proceedings pp. 727–740; [conference record](https://www.usenix.org/conference/atc20/presentation/li-huiba).
- [N] Dolstra, de Jonge, and Visser, [*Nix: A Safe and Policy-Free System for Software Deployment*](https://www.usenix.org/legacy/publications/library/proceedings/lisa04/tech/full_papers/dolstra/dolstra_html/index.html), LISA 2004, pp. 79–92. Citations use HTML section titles.
- [H] Dolstra and Hemel, [*Purely Functional System Configuration Management*](https://www.usenix.org/legacy/events/hotos07/tech/full_papers/dolstra/dolstra.pdf), HotOS 2007, §§2–4, PDF pp. 2–6.
- [I] Dolstra and Löh, [*NixOS: A Purely Functional Linux Distribution*](https://edolstra.github.io/pubs/nixos-icfp2008-final.pdf), ICFP 2008, §§3, 5–6, PDF pp. 2–5, 7–12.
- [G] GNU Guix Reference Manual 1.5.0, [§19, “Security Updates”](https://guix.gnu.org/manual/1.5.0/en/html_node/Security-Updates.html). This primary documentation was readable; no unread-source substitution is used.
- [O1] Accelerated Container Image, revision `0806b5ec1b3ce3262b8ab9c9ba499f6bbe8b4ae0`, [“Standalone Userspace Image Convertor,” “Layer/Manifest Deduplication”](https://github.com/containerd/accelerated-container-image/blob/0806b5ec1b3ce3262b8ab9c9ba499f6bbe8b4ae0/docs/USERSPACE_CONVERTOR.md#layermanifest-deduplication).
- [O2] Accelerated Container Image, revision `0806b5ec1b3ce3262b8ab9c9ba499f6bbe8b4ae0`, [“Overlaybd - TurboOCIv1,” “Feature,” “Configure,” and “Performance”](https://github.com/containerd/accelerated-container-image/blob/0806b5ec1b3ce3262b8ab9c9ba499f6bbe8b4ae0/docs/TURBO_OCI.md).
- [O3] OverlayBD, revision `f084bf59027744c25055f17f53ce726c82ff7d47`, [README, “System Requirements” and “Configuration”](https://github.com/containerd/overlaybd/blob/f084bf59027744c25055f17f53ce726c82ff7d47/README.md), and [“Standalone Usage,” “Config file,” “UBLK backend,” and “Writable layer”](https://github.com/containerd/overlaybd/blob/f084bf59027744c25055f17f53ce726c82ff7d47/docs/standalone-usage.md).
- [O4] Accelerated Container Image, revision `0806b5ec1b3ce3262b8ab9c9ba499f6bbe8b4ae0`, [README, “Components,” “Release Version Support,” and “Overview”](https://github.com/containerd/accelerated-container-image/blob/0806b5ec1b3ce3262b8ab9c9ba499f6bbe8b4ae0/README.md); BuildKit, revision `e4d319d7cc5ffb9463f75cf6502bd05c5cfe5ecf`, [“Build Overlaybd Images”](https://github.com/moby/buildkit/blob/e4d319d7cc5ffb9463f75cf6502bd05c5cfe5ecf/docs/overlaybd.md#build-overlaybd-images).
- [OCI] OCI Image Specification, revision `4feed4a6674e59d301b9294f187ac46acb2de99f`, [“Image Layer Filesystem Changeset”](https://github.com/opencontainers/image-spec/blob/4feed4a6674e59d301b9294f187ac46acb2de99f/layer.md), especially “File Attributes,” “Hardlinks,” “Applying Changesets,” and “Whiteouts”; [“Layer ChainID”](https://github.com/opencontainers/image-spec/blob/4feed4a6674e59d301b9294f187ac46acb2de99f/config.md#layer-chainid).

The following local locators reproduce the scope of the supplied findings, not additional implementation claims:

- [P1] [`build.py`](../../src/palimpsest_local/build.py):177–229, 253–268, 295–317, 334–379, 403–446; [`cli.py`](../../src/palimpsest_local/cli.py):591–621. Digest inputs, ordered parent resolution, base binding, and non-moving tags.
- [P2] [`artifact_compatibility.py`](../../src/palimpsest_local/artifact_compatibility.py):44–55, 70–202. Three-valued, platform-metadata-only compatibility; unknown architecture is not inferred.
- [P3] [`oci_store.py`](../../src/palimpsest_local/oci_store.py):276–320, 362–397; [`oci_changeset.py`](../../src/palimpsest_local/oci_changeset.py):151–219. Parent-prefix conversion identity and normalization inputs.
- [P3-test] [`test_oci_store.py`](../../tests/unit/test_oci_store.py), `test_parent_prefix_controls_cache_without_binding_config_or_future_layers`. Named regression evidence in the findings; not executed for this document.
- [P4] [`buildkit.py`](../../src/palimpsest_local/buildkit.py):541–573, 1831–1900, 2335–2359; [`BuildKit workflow`](../buildkit-block-workflow.md):143–145, 201–217. Build cache and merged-rootfs runtime pack are separate.
- [P5] [`oci_converter.py`](../../src/palimpsest_local/oci_converter.py):154–161, 347–379, 535–587; [`oci_changeset.py`](../../src/palimpsest_local/oci_changeset.py). Conversion semantics and metadata restrictions.
- [P6] [`layer_cache.py`](../../src/palimpsest_local/layer_cache.py):177–178, 423–450, 795–823; [`oci_store.py`](../../src/palimpsest_local/oci_store.py):1036–1092, 1500–1579. Immutable consumer binding, single-flight locking, atomic publication.
- [P7] [`published_images.py`](../../hub/src/palimpsest_hub/services/published_images.py):660–688, 822–875, 929–974; [`instances.py`](../../hub/src/palimpsest_hub/services/instances.py):358–459, 566–677. Published manifests and creation-time instance pins.
- [P8] [`base_migration.py`](../../src/palimpsest_local/base_migration.py) and [`structure-check specification`](../../openspec/specs/base-migration-structure-check/spec.md). In-progress pure checker, not a wired end-to-end migration workflow.
- [P9] [`vz_commit.py`](../../src/palimpsest_local/vz_commit.py):242–284. Source-identified whiteout-order defect in the supplied findings; no runtime reproduction is claimed here.

## Related work

### Strata: appliance composition and centrally propagated namespace updates

Strata addresses provisioning, maintenance, sharing, and recovery across many virtual appliances, rather than only image transfer. [S, §§1, 3–4, pp. 1–6.]
Its composition unit is a named, versioned layer containing files, metadata, and optional integration scripts; software, configuration, and private layers have different roles. [S, §§3, 5.1, pp. 3–4, 7.]
An LDF lists `repository/layer version`; `=` holds a version, and `@` references another appliance definition. [S, §5, pp. 6–7, Figs. 2–3.]
Explicitly selected layers and implicitly selected dependencies are distinguished in the LDF. [S, §5, p. 6.]

`Depends` requires co-inclusion, while `Pre-Depends` additionally orders integration scripts; version constraints restrict which layer units satisfy a dependency, and `|` alternatives let any of several layers satisfy it. [S, §5.2, pp. 7–8.]
`Conflicts` excludes incompatible layer units; `Provides` supplies unversioned virtual functionality such as `httpd`. [S, §5.2, p. 8.]
Strata uses Debian-style metadata and Smart to resolve the declared constraints, then updates the LDF rather than unpacking packages into each appliance. [S, §5.2, p. 8.]
This is declared dependency resolution, not a proof that arbitrary binaries have compatible ABIs. [S, §§5.2–5.3, pp. 8–9.]
The paper explains that a changed library ABI requires appropriate dependency naming and conflicts, and that package-derived layers from different distributions cannot generally be mixed. [S, §§5.2–5.3, pp. 8–9.]

Software layers that contain the same file pathname cannot be composed together, avoiding software-layer-order ambiguity. [S, §3, p. 4; §5.2, p. 8.]
Configuration and private layers can obscure lower files; this is not a ban on every overlap across every layer category. [S, §3, p. 4.]
Hardlinks cannot cross layers, although symlinks can refer across them by pathname. [S, §5.1, p. 7.]

A new repository layer does not overwrite the old version; changing a template propagates selected layers to its provisioned appliances. [S, §§3, 4.4, pp. 4–5.]
VLFS namespace replacement is atomic, and suspended appliances incorporate changes when resumed. [S, §§4.4, 6.2, pp. 5, 11–12.]
Removing a layer unlinks it from future namespace lookup but retains access for already-open files. [S, §5.5, pp. 10–11.]
Running processes can therefore keep using old files; namespace replacement is not an assertion that their mapped code or memory was patched. [S, §5.5, pp. 10–11.]
Integration scripts can stop and start daemons; some changes, notably kernel upgrades, require a reboot. [S, §§5.1, 5.5, pp. 7, 10.]

Runtime writes use a private writable layer, with per-shared-layer whiteout handling and a `revert` operation. [S, §5.5, p. 10.]
Recovery can replace a compromised private layer while retaining the old one for selective data recovery; this is not automatic validation of retained application data. [S, §4.5, pp. 5–6.]
The design relies on repository access controls and independently enforced SAN read-only access, not merely guest cooperation. [S, §5.4, pp. 9–10.]
The prototype extends UnionFS as a loadable module for Linux 2.6 and evaluates VMware ESX 3.0 with shared SAN storage. [S, §6, p. 11.]

The evaluation covers five appliance types, provisioning, updating 50 suspended appliances for a `tar` security patch, storage, Postmark, kernel compilation, and Apache requests. [S, §§6.1–6.4, pp. 11–13.]
It reports provisioning time comparable to QCOW2 copies and far below raw copies, much lower per-VA update time than package-manager updates of 50 suspended VAs (1.041 s versus 73.2 s), an order-of-magnitude storage reduction over raw images at 50 VAs but more storage than one disk image for a single VA, substantial Postmark overhead, and about 5% or less kernel-build and Apache overhead. [S, §§6.1–6.4, pp. 11–13.]
Its provisioning-copy timing is not an application-ready boot-time comparison. [S, §6.1, p. 11.]
Those experiments do not address cross-distribution ABI safety or a general mutable-database rollback protocol; OCI conversion was outside the paper's time and scope. [S, §§5.3, 6, pp. 9, 11–13.]
These are boundaries of the reported evaluation, not defects inferred from missing experiments.

### DADI: block-layer delivery and container deployment

DADI addresses download/unpack delays and burst deployment through block-level remote images with optional peer-to-peer distribution. [D, §§1, 3, 3.5, pp. 727–734.]
Each layer records changed blocks underneath a chosen filesystem; OverlayBD merges the ordered block changes, with the latest write winning. [D, §§3.1–3.2, pp. 730–731.]
The paper does not discuss applying a block layer to a different parent; since a layer is a block delta under a given filesystem image, no rebasing property follows. [INFERENCE from D, §3.1, p. 730.]
Filesystem interpretation belongs to the consumer; the image service exposes block reads rather than implementing POSIX file operations itself. [D, §§1, 4.1, pp. 727–728, 734.]

Data can be read on demand from the registry, P2P agents, or shared storage, or whole blobs can be downloaded locally; ZFile supports chunkwise decompression, and trace-based prefetching and P2P distribution address startup and fan-out. [D, §§3.3, 3.5, 4.4, pp. 731–735.]
Conversion walks OCI-style `.tgz` layers bottom-up, creates each writable layer, and unpacks file changes while handling deletion whiteouts. [D, §4.3, p. 735.]
The paper describes whiteout handling but does not publish an exhaustive opaque-directory, tar-order, xattr, and ownership conformance matrix. [D, §4.3; §5, pp. 735–738.]
Its writable container layer is log-structured and can be committed into a read-only block layer. [D, §3.4, pp. 732–733.]

The paper argues that the block interface reduces the guest-facing attack surface and describes CRC32 checks for fixed-size blob blocks at each receiving node. [D, §§3.5, 4.1, p. 734.]
CRC32 detects accidental corruption; that mechanism alone must not be described as cryptographic publisher authentication or protection against malicious content replacement. [D, §3.5, p. 734.]
The described design integrates both cgroups and QEMU runtimes; proposed support for additional runtimes is not evaluated support. [D, §§1, 4.1, pp. 728, 734.]

The study measures container startup, prefetching, batch startup, I/O, image sizes, index behavior, and image building. [D, §§3.2–3.3, 5, pp. 731–732, 735–738.]
The large-scale run starts 10 containers on each of 1,000 VMs created on Alibaba's public cloud as container hosts; it reports container cold-start latency, not VM creation or boot time. [D, §5.3, p. 737.]
Its 100,000-container figure is a projection from a constructed P2P path, not that many directly instantiated containers. [D, §5.3, p. 737.]
The build-speed experiment excludes image commit/compression time, which must not disappear from a new end-to-end comparison. [D, §5.5, p. 738.]
The evaluation does not establish new VM boot latency, arbitrary leaf rebasing, or ABI diagnosis. [D, §§4.1, 5, pp. 734–738.]
These are scope boundaries of the paper's goals and evaluation, not defects inferred from missing experiments.

### Current OverlayBD: related implementation, not identical historical evidence

The conversion deduplication table keys by `(host, repo, chain_id)`; parent-aware conversion identity is established prior art. [O1, “Layer/Manifest Deduplication”; OCI, “Layer ChainID.”]
OCI ChainID recursively hashes the preceding chain identity and the next DiffID, rather than identifying only leaf tar bytes. [OCI, “Layer ChainID,” “Definition.”]
BuildKit support is documented from v0.22, with active OverlayBD services, snapshotter configuration, an OverlayBD `FROM`, and uncompressed OCI-media-type output. [O4, “Components”; BuildKit, “Build Overlaybd Images.”]
These are deployment requirements, not evidence that Palimpsest currently integrates that path; such integration is **not verified**.

TurboOCI loads the original unmodified OCI image plus a separate ext4 metadata image; its documentation presents this as avoiding the signature invalidation caused by conversion. [O2, “No image conversion.”]
The meta image is a new artifact outside the original signatures. [INFERENCE from O2, “No image conversion.”]
The stated roughly 3% metadata size is a documentation characterization, not an upper bound or an independently reproduced result. [O2, “No image conversion,” “Performance.”]
Gzip data uses a local decompressed cache, so metadata-image size alone is not total local storage or transfer cost. [O2, “Configure,” “Performance.”]
The format `0.1.0-turbo.ociv1` requires OverlayBD v0.6.10 or later. [O4, “Release Version Support.”]

Runtime needs TCMU or ublk support; standalone configuration supports local or registry images and one writable layer at the top. [O3, “System Requirements,” “Config file,” “Writable layer.”]
The ublk documentation reports a 5.10 backport-kernel runtime check and says full mainline-6.x regression remains pending. [O3, “UBLK backend,” “Notes.”]
File-cache locking is in-process; separate daemons must not share a cache directory. [O3, “UBLK backend,” “Notes.”]
Background downloading can fetch whole blobs after launch, with the documented default delay of 600 seconds. [O3, “Configuration,” `download`.]
The documented ZFile checksums protect on-demand reads against corruption; they do not alone establish malicious-publisher resistance. [O4, “Overview.”]
VM applicability is documented. [O3, README introduction.]
The supplied findings report no official Nova/Cinder integration found; that absence is **not independently verified** here.
These are scope boundaries of the documentation and available evidence, not defects inferred from missing experiments.

### Nix and NixOS: immutable dependency graphs and retained generations

Nix addresses deployment consistency, variants, complete dependencies, and source/binary sharing rather than OCI changeset ordering. [N, “Motivation,” “Overview.”]
Its composition units are immutable store objects and derivations; deployment transfers the closure under references. [N, “The Store,” “Building Components.”]
Store names incorporate build inputs, so changing a dependency yields a different dependent derivation rather than overwriting an existing object. [N, “The Store”; H, §2, p. 3.]
Previously built derivations can be reused, and binary substitutes avoid local compilation when the exact requested build is available. [N, “Substitutes,” “Deployment Policies”; I, §3, pp. 4–5.]
A core compiler or C-library change can nevertheless force extensive rebuilding; configuration-only changes can be much cheaper. [H, §4, pp. 5–6; I, §3, p. 5.]
This is not evidence that every dependency update has the same cost. [H, §4, pp. 5–6.]

Nix scans output references for runtime dependencies and allows expression assertions for consistency, such as matching OpenSSL instances. [N, “Nix Expressions,” “Assertions,” “Building Components.”]
The 2004 paper acknowledges undeclared dependencies outside the store and reliance on the running kernel. [N, “Experience,” “Software deployment.”]
A dependency closure is not a proof of application correctness or an arbitrary ABI substitution checker. [N, “Assertions,” “Experience”; I, §6.2, pp. 10–12.]
The 2004 paper expects input hashing to make reuse of other users' builds safe against tampering. [N, “Conclusion.”]
This document does not adopt that expectation: an input hash does not authenticate externally supplied output bytes [INFERENCE], and identical derivations produced differing bytes in the 2008 purity test. [I, §6.2, pp. 11–12.]

Nix generations switch user-environment symlinks atomically and retain previous environments until removed and garbage-collected. [N, “User Environment Policies.”]
NixOS extends this approach to static system configuration, kernels, service descriptions, and activation scripts. [H, §3, pp. 3–4; I, §5, pp. 7–10.]
Activation starts, stops, or restarts affected services; a pure build and a running-system transition are different operations. [H, §3, p. 4; I, §5, pp. 9–10.]
Mutable `/var` data remains stateful and outside the purely functional configuration model; rolling back configuration does not rewind that data. [H, §4, p. 5; I, §6.2, p. 11.]

The 2004 paper reports experience with roughly 180 packages, service deployment, and build farms, not a container/VM startup benchmark. [N, “Experience.”]
The 2007 paper evaluates feasibility and configuration/store sizes and explicitly discusses mutable-state and worst-case rebuild/storage costs. [H, §4, pp. 5–6.]
The 2008 paper tests purity with two builds of 485 non-fetch derivations and reports remaining byte differences, many associated with timestamps. [I, §6.2, pp. 11–12.]
It explicitly distinguishes its functional build model from complete enforcement of deterministic builder behavior. [I, §6.2, “Build actions,” pp. 11–12.]
Those results do not establish bit-identical rebuilding of every package or transactional recovery of mutable application databases. [I, §6.2, pp. 11–12.]

### Guix grafts: the ABI-compatible security-patch comparison

Guix grafts address urgent updates when rebuilding an entire dependent graph would delay deployment. [G, §19.]
A package's `replacement` points to the fixed package, and recursive grafting rewrites referring packages instead of recompiling every dependent. [G, §19.]
The manual requires equal name/version lengths for rewritten paths; shared-library replacements must preserve SONAME and binary compatibility. [G, §19.]
That compatibility is a precondition, not a solver-derived proof. [G, §19.]
The manual supplies profile/system reference checks and suggests `lsof` to inspect running processes, distinguishing selected packages from currently executing code. [G, §19.]
This mechanism is a pertinent baseline for compatible patches, not evidence that ABI-breaking rebases are safe. [G, §19.]
The manual's qualitative graft-cost statements are not a controlled Palimpsest comparison and are not adopted as measured results here. [G, §19.]

## Comparison with current Palimpsest guarantees

Each row compares a mechanism in its stated scope, not a rank ordering of whole systems.
For Guix, only the graft mechanism read above is compared; other Guix guarantees are not inferred.

| System | Unit of composition | Parent/dependency change | Compatibility checked, and by whom |
| --- | --- | --- | --- |
| Strata | Versioned software/configuration layers and private layer. [S, §§3, 5.1.] | New layer version and changed LDF/template; template change propagates. [S, §§4.4, 5.] | Smart resolves maintainer-declared Depends/Pre-Depends/Conflicts/Provides; software pathname conflicts are excluded. [S, §§3, 5.2–5.3.] |
| DADI | Ordered block changes beneath a selected filesystem. [D, §§3.1–3.2.] | New block layer/build or bottom-up tar conversion. [D, §4.3.] Applying a layer to an arbitrary different parent is not established. [INFERENCE from D, §3.1.] | Runtime interprets filesystem; paper does not claim application-ABI diagnosis. [D, §§4.1, 5.] |
| Current OverlayBD | Ordered block layers, or original OCI data plus TurboOCI metadata. [O2; O3.] | Converter dedup uses ChainID, binding prior layer context. [O1; OCI, “Layer ChainID.”] | Filesystem/backend prerequisites are documented; application ABI proof is not claimed by these sources. [O2–O4.] |
| Nix/NixOS | Derivations, immutable store objects, closures, system generations. [N, “The Store”; I, §§3, 5.] | New input-dependent derivations; old objects remain. [H, §2; I, §3.] | Explicit inputs, reference scanning, assertions, and build success; authors document impurity limits. [N, “Assertions”; I, §6.2.] |
| Guix grafts | Replacement package and recursively rewritten referring packages. [G, §19.] | `replacement` mapping creates grafted references, not a universal recompile. [G, §19.] | Packager must preserve binary compatibility and SONAME; no ABI-proof claim. [G, §19.] |
| Palimpsest today | SHA-256 `FROM`/ordered `LAYER`, SquashFS runtime layers, ordered Hub manifest membership. [P1, P7.] | Explicit immutable parent inputs; conversion key binds preceding descriptors and DiffIDs. No descendant rebuild orchestrator. [P1, P3; findings “Not implemented.”] | CLI checks parent/base chain, format, and architecture when both declare it. Compatibility verdict covers platform metadata only. [P1, P2.] |

| System | Update of running systems | Mutable state | Delivery backend | Trust/integrity boundary |
| --- | --- | --- | --- | --- |
| Strata | Live namespace replacement retains opened old files; scripts may restart daemons; kernel changes need reboot. [S, §§5.1, 5.5.] | Private writable layer; replacement/recovery and manual data salvage. [S, §§4.5, 5.5.] | Shared SAN/NFS directories. [S, §3, p. 4; §5.4.] Remote repositories mirrored before composition. [S, §5.4.] | Repository permissions and independently enforced read-only SAN access. [S, §5.4.] |
| DADI | Delivery supports new containers; no claim of patching existing process memory. [D, §§4–5.] | Writable block layer and commit, not database rollback. [D, §3.4.] | Registry, optional P2P, shared storage, or local blobs; on-demand block reads. [D, §§3.5, 4.4.] | Receiving nodes check CRC32; narrower block interface is a design argument, not cryptographic origin proof. [D, §§3.5, 4.1.] |
| Current OverlayBD | Device-based runtime; sources do not establish arbitrary live parent replacement. [O3.] | One top writable layer; explicit commit. [O3, “Writable layer.”] | Local/registry standalone paths; TCMU/ublk; background whole-blob download can follow lazy reads. [O3.] | ZFile corruption checks; TurboOCI avoids modifying original image bytes. [O2, O4.] Original signatures do not cover the separate metadata image. [INFERENCE from O2.] |
| Nix/NixOS | Profile switch plus explicit activation/service transitions. [N, “User Environment Policies”; I, §5.] | `/var` remains stateful and separate from configuration rollback. [H, §4; I, §6.2.] | Local store, closure copying, binary substitutes. [N, “Sharing Component Builds,” “Substitutes.”] | Input identity and reference closure do not alone authenticate substitute contents. [INFERENCE.] The 2004 authors expect input hashing to make reuse safe. [N, “Conclusion.”] |
| Guix grafts | Grafted selection does not establish replacement of running process code. [G, §19, `lsof` discussion.] | Mutable-data recovery is outside this graft description. [G, §19.] | Store-path rewriting; full delivery transport is outside this comparison. [G, §19.] | Compatible replacement is assumed; this section does not establish a complete supply-chain trust policy. [G, §19.] |
| Palimpsest today | Instance creation pins digests/base/provenance/manifest; later publishes do not change them. No auto-update claim. [P7.] | Capture records final upper state, not history. [Findings “Limits / caveats.”] Migration and rollback are not assumed safe. [Findings, owner principle 5.] | SquashFS plus guest OverlayFS assembly across the four source-described paths; shared-cache flock semantics remain conditional. [P5, P6; findings “Guest assembly.”] | Digest-bound inputs, multi-input conversion key, immutable consumer binding, atomic publication. Signatures or per-read cryptographic authentication are **not verified** here. [P1, P3, P6.] |

## Claim boundary

### Statements supportable today within the supplied evidence

- Local recipe input digests are enforced; the CLI `run base --layer` resolver enforces ordered parent links, while `StackRef` itself does not verify them and Hub registration tolerates missing or hidden parents; architecture is compared only when both sides declare it. [P1, P2; findings “Limits / caveats.”]
- An existing local tag cannot silently move to different build bytes. [P1.]
- Conversion identity includes the parent prefix and conversion contracts; configuration and future layers do not enter that prefix. [P3, P3-test.]
- The named prefix-cache regression exists in the findings; this document does not report executing it. [P3-test.]
- BuildKit vertex reuse and OCI-to-SquashFS conversion reuse are different mechanisms; after a parent change, BuildKit's solver decides vertex reuse, so not every command necessarily reruns. [P3, P4.]
- The converter source passes mode/uid/gid/mtime/devices and `user.*`/`security.capability` xattrs to `mksquashfs -tar -xattrs` and normalizes whiteouts as lower-only effects; cross-layer hardlinks are unsupported, and end-to-end UID/GID fidelity is not live-proven. [P5; findings “Limits / caveats.”]
- Cache publication and consumer binding have source-enforced immutability/atomicity properties, subject to working flock semantics. [P6.]
- Hub publication records ordered immutable membership, and instances pin creation-time identities. [P7.]
- An in-progress pure, deterministic structure checker exists; listing adapters, Hub API/dashboard surfaces, risk acknowledgement, and registration are not implemented. [P8.] The carrying branch adds a local read-only plan that can display such a report as its filesystem stage, but nothing produces the checker's input from stored layers yet.
- The supplied findings identify a VZ commit whiteout-order defect; generated-marker-first layers are outside that stated trigger. [P9.] The carrying branch fixes it at unit-test level only.

### Hypotheses requiring experiments

- Scoped diagnostics might detect hazardous rebases earlier without mislabeling ABI-compatible or unrelated changes.
- Dependency-aware rebuilding might limit rerun work; parent-prefix conversion invalidation might still dominate another workload's cost.
- A different delivery backend might lower time-to-readiness, total bytes, or cost under some cache and concurrency conditions.
- Explicit generation selection might improve recovery of executable state without improving recovery of migrated mutable data.
- Separately recorded tool/environment inputs might improve reproducibility; bit-identical output remains an experimental question.
- A preserved structure and matching platform metadata might correlate with runtime success, but neither implies it.

### Claims that must not be made

- “First parent-aware conversion cache”: OverlayBD dedup already uses ChainID. [O1; OCI, “Layer ChainID.”]
- “First immutable-parent rebuild model” or “first retained-generation rollback”: Nix/NixOS already describe both. [N, “The Store,” “User Environment Policies”; H, §§2–3.]
- “First OCI-on-VM” or equivalent novelty: DADI already describes QEMU integration and block-based VM applicability. [D, §§1, 4.1, 6.]
- “A solver proves ABI compatibility”: Strata constraints and Guix graft prerequisites do not provide that proof. [S, §5.2; G, §19.]
- “Palimpsest proves ABI/kernel/driver compatibility,” “unknown means safe,” or “structure pass means runtime pass.” [P2, P8.]
- “Automatic descendant rebuild,” “Hub-wide or cross-host reverse impact queries,” “provides/requires/conflicts solver,” or “smoke-result store” exists today. [Findings “Not implemented.”] The carrying branch's plan lists descendants recorded in one local store only.
- “Every parent change reruns every build command,” or “conversion-cache hit proves a build-cache hit.” [P3, P4.]
- “Reproducible means bit-identical,” “commit preserves execution history,” or “rollback restores application data.” [Findings “Limits / caveats.”]
- “All OCI filesystem fidelity is live-proven”: a historical UID/GID gate failed, and the findings do not establish downstream live fidelity. [Findings “Limits / caveats.”]
- Any unmeasured speedup, cost reduction, storage saving, safety superiority, or universal performance ordering.

## Validation design: proposed experiments, not results

### Common protocol and independent measurements

Pin source revisions, compiler/packer versions and bytes, kernels, filesystem features, architectures, input digests, and backend settings.
Record all recipe inputs and expected outputs in a test-owned manifest; this is experimental instrumentation, not a current Palimpsest guarantee.
Use matched workload content and readiness definitions across executed systems; use Strata only as a literature comparison, not an executed baseline, and do not compare its LDF-write timings with guest application readiness.
For source-based baselines, use native package definitions; do not pretend Nix store composition is identical to rebasing an OCI leaf.
For fixed-leaf/new-parent arms, the CLI rejects a layer whose recorded parent differs from the selected chain. [P1.]
Do not assume a native rebase operation: it is **not verified** by these findings; report that native experimental arm as not implemented, or label externally assembled OCI fixtures and their conversion as harness operations.
For Palimpsest structure diagnostics, feed test-supplied listings to the in-progress checker; the carrying branch's local plan reports the structure stage only, and Hub-side diagnosis and any plan rejection are not implemented. [P8.]
Run synthetic fixtures first, then a research workload and a stateful cloud service, with clearly separated conclusions.
Use clean, build-warm, conversion-warm, data-cache-warm, and mixed-cache trials; retain each state description with the result.
Measure build and conversion caches independently; never infer one from the other or count output presence as a cache hit.
Use repeated paired trials, randomized order, fixed concurrency tiers, raw samples, medians, tail latency, and uncertainty intervals.
A failed trial remains in the record; report its last completed phase rather than silently retrying it into success.

Every scenario below reports these quantities separately, including zeros, failures, unsupported operations, and unavailable measurements:

- **C — Correctness/detection:** oracle outcome, diagnostic category, false accept/reject, unknown, and which verification stage produced each verdict.
- **B — Rebuild amplification:** recipe steps/vertices executed and reused, plus CPU/wall time; count compilation separately from reference rewriting.
- **R — Reconversion amplification:** source layers actually reconverted, input/output bytes processed, and conversion CPU/wall time.
- **X — Bytes transferred:** observed wire bytes by registry/peer/shared-storage path, before readiness and through the full trial horizon.
- **S — Bytes stored:** logical and physically allocated bytes, deduplicated/shared bytes, metadata, temporaries, old generations, and cache high-water marks.
- **D — Deployment/run cost:** plan/publish/fetch/assemble/boot/readiness times, steady-state workload latency/throughput, CPU, RAM, I/O, and resource-seconds.
- **G — Generation recovery:** ability/time to select the old exact artifacts and regain old workload readiness; for Palimpsest, propose creating a new instance from the old pinned digests, not an in-place switch. Creation-time pins are established [P7]; in-place generation switching is **not verified** and must be reported as not implemented in this protocol.
- **M — Mutable-data recovery:** backup/snapshot identity, quiescence, restore time, lost/duplicated writes, checksums, schema readability, and application invariants.

For B and R, report absolute counts and denominators; ratios with zero eligible steps or layers are undefined, not infinite speedups.
For X and S, measure actual traffic/allocation rather than summing advertised image sizes or counting only metadata images.
Include background download, gzip caches, shared-server storage, retained generations, and steady-state reads in the accounting window.
For D, report backend startup and teardown as well as readiness; publish any monetary conversion separately from measured resource quantities.
For G and M, test old code with untouched old data, then old code after new-generation writes, using separate recoverable copies.
Stop on uncertain cleanup or integrity failures; no experiment may modify existing environments or production data.

### Scenario matrix

#### 1. ABI-breaking parent change

- **Variation and oracle:** Replace a required OpenSSL SONAME major, or use a controlled removed symbol/version; keep the old leaf binary fixed before rebuilding.
- Compare independent ELF/symbol inspection with loader failure and application assertions; then rebuild against the new parent and repeat runtime checks.
- **C:** Record metadata/structure verdicts separately from missing-library, symbol-version, startup, and semantic runtime failures; a structure pass is not runtime success.
- **B:** Count dependent recompilation and cached independent vertices; include the failed fixed-leaf attempt without treating it as a rebuilt success.
- **R:** Count invalidated prefix-dependent conversions and their processed bytes, independently of compilation.
- **X/S:** Measure all transferred bytes and allocated old/new artifacts, intermediate roots, caches, and retained data copies separately.
- **D:** Measure failed deployment cost, rebuilt-generation readiness, and steady-state work under equal resources; Palimpsest has no plan-rejection path, so record not implemented rather than a rejection time. [P8.]
- **G/M:** Recover the old generation and verify old readiness; separately restore old data and test whether new-schema writes remain readable.
- **Execution mode:** Pure fixtures and ELF inspection need no VM; loader/workload and stateful-service trials require an isolated compatible runtime.

#### 2. ABI-compatible security patch

- **Variation and oracle:** Patch an exercised vulnerability without changing SONAME/export contracts; use a fixed harmless regression input and functional tests.
- Compare full dependent rebuild, an externally assembled fixed-leaf/new-parent OCI fixture, and native Guix grafting where applicable; the fixture is a harness operation, not a Palimpsest-native rebase or graft feature.
- **C:** Verify the fix actually executes, not only that the patched library exists; record structure/platform, ABI-tool, and runtime verdicts independently.
- **B:** Count compilation, reused steps, and graft rewrite work separately; do not count rewritten dependents as recompilations.
- **R:** Count parent-context reconversions and processed bytes even when no dependent source recompiles.
- **X/S:** Measure wire bytes and allocated patch, rewrite, conversion, retained-generation, and cache storage separately.
- **D:** Measure time to patched service readiness, restart/downtime, resource use, and steady-state behavior.
- **G/M:** Recover the previous generation as an explicitly vulnerable test baseline; separately verify mutable-data integrity on a disposable copy.
- **Execution mode:** Manifest/reference checks need no VM; patch efficacy and executing-library identity need an isolated workload environment.

#### 3. Unrelated parent change

- **Variation and oracle:** Change a documentation file outside a synthetic program's declared and observed dependency paths; keep the leaf source/recipe fixed.
- Use an independently enumerated filesystem diff and fixed application outputs; limit “unrelated” to this deliberately bounded workload.
- **C:** Feed test-supplied listings to the in-progress structure checker and compare its scoped findings with independently observed workload behavior; no product surface produces those listings yet, and absence of observed access is bounded evidence, not proof. [P8.]
- **B:** Count actual step reuse and re-execution rather than assuming all descendants rebuild or none should.
- **R:** Count prefix-key-driven reconversions and processed bytes even when the runtime output is unchanged.
- **X/S:** Measure transferred bytes and incremental physical storage, including parents, derived artifacts, and warm-cache reuse.
- **D:** Measure update preparation/readiness and repeated workload cost under matched cache states.
- **G/M:** Verify old-generation selection; separately compare mutable-data checksums and application invariants before and after recovery.
- **Execution mode:** Hash/key/listing experiments need no VM; behavioral independence requires execution in an isolated runtime.

#### 4. Same leaf on different parents

- **Variation and oracle:** Apply identical leaf tar bytes to two parents with different directory modes and lower-path contents; also vary only config and later layers.
- Build expected merged inventories with an independent OCI changeset application and explicit expected metadata. [OCI, “Applying Changesets,” “Whiteouts.”]
- **C:** Check distinct parent-context identities and correct final roots; separately test each application's runtime assertions, never equating matching leaf bytes with compatibility.
- **B:** Report actual rebuild steps, including zero when reusing a prebuilt leaf; do not rename reconversion as a source build.
- **R:** Count misses/hits and reconverted bytes for changed prefixes versus config-only and future-layer controls. [P3-test defines the current test boundary.]
- **X/S:** Measure leaf-blob sharing, separately stored derived artifacts, and actual traffic rather than assuming identical leaves imply identical runtime packs.
- **D:** Measure conversion/assembly and each parent's readiness/run cost separately.
- **G/M:** Recover each original exact chain independently; separately restore its matching mutable-data copy and reject cross-pair assumptions.
- **Execution mode:** Key and inventory oracles need no VM; mounted-root/application tests require an isolated Linux runtime or disposable guest.

#### 5. Whiteout and opaque-directory semantics

- **Variation and oracle:** Permute markers before/after same-layer siblings, delete lower files, obscure directories, and recreate paths in the same layer.
- Require markers to hide only lower entries and remain invisible; opaque markers must preserve same-layer additions regardless of archive order. [OCI, “Whiteouts,” “Opaque Whiteout.”]
- **C:** Compare normalized inventory, mounted root, and capture round-trip; keep structure, filesystem, and workload verdicts separate. Include the source-identified VZ ordering trigger. [P9.]
- **B:** Count fixture construction/build steps and reuse independently of semantic normalization work.
- **R:** Count each reordered changeset's conversion and processed bytes; distinguish a changed tar digest from a changed final filesystem.
- **X/S:** Measure transfer and allocated marker/derived/capture artifacts and caches separately.
- **D:** Measure normalization/assembly overhead and workload behavior accessing deleted/recreated paths.
- **G/M:** Recover the previous visible namespace; separately recover deleted mutable files from a retained data copy, not from generation selection alone.
- **Execution mode:** Tar/normalizer/inventory checks need no VM; mount/capture checks need isolation, with a disposable VZ guest for the VZ-specific path.

#### 6. Hardlinks

- **Variation and oracle:** Use same-layer hardlinks, updates to linked files, and a separate cross-layer-reference case; record supported and rejected cases explicitly.
- Compare device/inode equivalence, link counts, contents, and post-write behavior, not merely equal file hashes. [OCI, “Hardlinks.”]
- **C:** Require same-layer fidelity; record cross-layer unsupported behavior without silently flattening it into a success. [P5.]
- **B:** Count content-producing steps, reused steps, and any explicit materialization separately.
- **R:** Count conversions and bytes read/written for each link variant, including rejected conversions.
- **X/S:** Measure wire and allocated bytes; logical duplicate file size is not proof of duplicate physical storage.
- **D:** Measure assembly and link/update workload latency plus I/O/resource use.
- **G/M:** Restore the old generation's link topology; separately restore mutable linked data and verify topology after recovery.
- **Execution mode:** Tar topology checks need no VM; inode/write semantics require an isolated supporting filesystem, not necessarily a VM.

#### 7. Permissions and ownership

- **Variation and oracle:** Vary numeric UID/GID, directory modes, executable/set-ID bits, and ownership-only changes across parents and leaves.
- Compare numeric metadata and expected allow/deny operations as specified users; names alone are not the oracle. [OCI, “File Attributes,” “Changeset over existing files.”]
- **C:** Separate metadata fidelity from enforced access and identity behavior; include non-root denial cases and record privilege-related skips.
- **B:** Count ownership/mode-producing recipe steps and actual cache reuse, not just changed file contents.
- **R:** Count conversion and processed bytes for metadata-only changes as well as content changes.
- **X/S:** Measure metadata-change traffic and allocated artifacts/cache/copy-up bytes separately from logical file sizes.
- **D:** Measure build/conversion/assembly cost and realistic access-check/workload overhead.
- **G/M:** Recover old generation metadata; separately restore mutable ownership and verify user access and application invariants.
- **Execution mode:** Listing comparisons need no VM; real ownership/set-ID tests need a disposable privileged or properly mapped isolated Linux environment.

#### 8. Extended attributes

- **Variation and oracle:** Vary binary `user.*` values, `security.capability`, directory xattrs, and an out-of-supported-scope namespace as a negative case. [P5.]
- Compare raw xattr bytes and expected capability-dependent behavior; report filesystem and privilege prerequisites. [OCI, “File Attributes.”]
- **C:** Separate archive retention, converted/mounted fidelity, and capability enforcement; unsupported xattrs must not masquerade as preserved attributes.
- **B:** Count xattr-producing/reused steps and any privilege-related build rejection.
- **R:** Count conversions and bytes processed for xattr-only changes, with parent-context variants.
- **X/S:** Measure traffic and physical storage including metadata blocks, temporary roots, and caches.
- **D:** Measure conversion/assembly cost and capability-dependent workload behavior under a controlled privilege model.
- **G/M:** Recover the prior generation's xattrs; separately restore mutable-file xattrs and verify application/capability behavior after restore.
- **Execution mode:** Parsed xattr/listing fixtures need no VM; capability enforcement requires an isolated compatible Linux kernel/filesystem and safe privilege bounds.

### Isolation, recovery, and threats to validity

“No VM” does not mean “safe on the developer's root filesystem”: use test-owned directories and avoid executing untrusted artifacts.
Host-level mounting and metadata operations require disposable namespaces/filesystems; use a disposable VM when equivalent safe isolation is unavailable.
Guest boot, guest-kernel/driver behavior, and backend lifecycle claims require that actual guest/backend; chroot success cannot substitute for them.
Kernel/ABI/driver coverage is a separate axis; include matched and mismatched architecture/kernel labels without inferring missing values.

Before stateful trials, create an application-consistent test backup and prove restoration on a separate copy.
Introduce controlled acknowledged writes after switching generations; test schema-forward and schema-backward readability independently.
Record the last durable acknowledged write, restored transaction count, and all missing/duplicate records.
A generation switch that restores executables but cannot read current data is a generation-recovery success only if reported with that data failure.
Report manual recovery actions, unavailable backups, retained old artifacts, and cleanup failures; do not hide them in deployment time.

Major threats include workload selection, hidden dependencies, incomplete oracles, differing package definitions, and unequal readiness criteria.
Cache warmth, registry locality, background fetch, compression, filesystem allocation, deduplication, and shared-server costs can reverse apparent savings.
Privilege mappings, xattr policy, filesystem features, guest kernels, and architecture support limit fidelity generalization.
Nix/Guix source transformations and OCI parent replacement solve different subproblems; compare common outcomes without erasing those differences.
Historical paper hardware and current software versions do not support direct speed ratios across publications.
An author's observed benefits remain that author's results, not measurements reproduced by this document.
Synthetic ABI failures may be easier than real ecosystem breakage; include real workloads without treating a small sample as universal safety.
Recovery tests can miss delayed data corruption, external-service state, distributed writes, and schema changes; state their observation window.
For proposed Palimpsest workflows lacking implementation, report **not implemented / not run**, not a fabricated result or simulated product guarantee.

Publish raw manifests, oracle outputs, phase counters, per-trial samples, and a reproducible command record with secrets removed.
Keep implemented, designed, and hypothesized behavior in distinct result tables.

## Host-level pilot observation (2026-11-15; synthetic listings, no VM, not an experiment result)

One run of the pure functions in the carrying branch (`base_migration.check` and `compatibility_stages.diagnose`) on four hand-written listings. It shows what the structure check can and cannot separate; it measures nothing and says nothing about real images.

| Synthetic case | Base change | Kept layer | Structure-check verdict | Staged result |
| --- | --- | --- | --- | --- |
| 1. ABI-breaking | `usr/lib/libssl.so.1.1` removed, `libssl.so.3` added | adds `opt/app/bin` only | `clean` | filesystem `passed`; ABI, kernel/device, smoke test `unverified` |
| 2. ABI-compatible patch | `libssl.so.1.1` content changed | adds `opt/app/bin` only | `clean` | same as case 1 |
| 3. Unrelated change | `usr/share/README` added | adds `opt/app/bin` only | `clean` | same as case 1 |
| 5. Overwrite and whiteout | as case 1 | overwrites `etc/os-release`, deletes `libssl.so.1.1` | `conflict` (1 conflict `overwrite-changed`, 1 notice `whiteout-target-gone`) | filesystem `failed`; later stages `unverified` |

Cases 1, 2 and 3 are indistinguishable to the structure check: a binary that needs the removed library is not visible in path listings. This is the reason the ABI stage is never reported as passed, and the reason scenario 1 needs a runtime oracle. The listings are synthetic: file names stand in for libraries, and no binary was built or run.
Only after these experiments may a paper state a scoped measured result; this document states none.
