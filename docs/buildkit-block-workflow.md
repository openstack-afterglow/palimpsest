# BuildKit Cache and Block Runtime Workflow

This document defines the target Dockerfile workflow for Palimpsest. It separates BuildKit's logical build cache from the SquashFS artifact attached to a VM, specifies online and strict-offline resolution rules, and defines the evidence required before the workflow is treated as production-ready.

> **Source status:** local Buildx solve, scoped mandatory native cache archives, native project-package export/publication, OCI-profile Docker/Buildx publication, strict-offline inputs, rootfs export and SquashFS runtime packing are present in source. Portable and Hub gates passed; isolated loopback HTTPS acceptance exercised real native build → deferred push → byte-identical pull and remote exact-cache reuse. See the [dated candidate checkpoint](development-handoff.md#candidate-integration--2026-10-02) for synthetic-identity/SQLite and deployment limits. Existing `Palimpsestfile` builds remain a separate VM frontend. Clean-host Linux KVM, high-concurrency and macOS native block attachment remain distinct gates.

The two frontends have intentionally separate option contracts. Selecting `--frontend palimpsestfile` rejects every Dockerfile/BuildKit-only flag, including explicit default-valued flags such as `--platform linux/amd64`, instead of silently ignoring them.

## Selected Buildx builder preflight

Online Dockerfile builds first authorize `cache:read` and `cache:write` for the exact native cache package, then inspect the builder already selected by Buildx or `BUILDX_BUILDER`. The inspected name is pinned into `docker buildx build --builder <name>`, so a concurrent global `docker buildx use` cannot switch the executor between preflight and solve. Inspection is read-only: Palimpsest does not create, select or bootstrap a builder. Unauthorized package-only keys fail before inspection or build-directory creation.

The OCI output required by this workflow is supported by the `docker-container`, `kubernetes`, and `remote` drivers. The default `docker` driver is rejected before Hub cache resolution or the Buildx solve because it cannot export `type=oci`. The successful build receipt records the inspected `buildx_driver`.

Use a dedicated builder without changing the global selection:

```bash
docker buildx create --name palimpsest --driver docker-container
docker buildx inspect --builder palimpsest --bootstrap
BUILDX_BUILDER=palimpsest palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --tag demo \
  --cache-registry hub --cache-package project-apps/demo
```

The online example assumes a native `hub` profile, registered `project-apps` namespace and a key authorizing cache read/write for exact package `demo`, as described in [registry profiles](registries.md); a published runnable package is not required to write its cache. The same builder environment override can name an existing `kubernetes` or `remote` builder. If inspection fails, the error includes the selected-driver requirement and this non-disruptive setup path.

Strict offline mode has a narrower builder contract. It accepts only an already-bootstrapped, single-node local `docker-container` builder whose sole endpoint matches the current local Unix/named-pipe Docker context and whose sole BuildKit container is attached to Docker network mode `none`; multi-node, remote, and Kubernetes builders cannot provide client-verifiable air-gap evidence. Provision that builder before disconnecting the host:

```bash
docker buildx create --name palimpsest-offline \
  --driver docker-container \
  --driver-opt network=none
docker buildx inspect --builder palimpsest-offline --bootstrap
BUILDX_BUILDER=palimpsest-offline palimpsest build . \
  --frontend dockerfile --offline --network none --tag demo
```

Palimpsest inspects the existing node, endpoint, and builder container and fails closed if the topology is not exactly one local node/container, its Docker network mode is not `none`, or its current `NetworkSettings.Networks` contains any later-added bridge/overlay attachment; the build command itself never creates or bootstraps it.

## Outcome

A Dockerfile build produces two different kinds of reusable data and can publish a third representation:

1. **BuildKit cache records** preserve fine-grained build work. They let a later solve skip unchanged Dockerfile vertices.
2. **A runtime block** is one deterministic, read-only SquashFS filesystem image. KVM attaches it as a single `virtio-blk` disk.
3. **An OCI output** is a verified archive. A native profile retains immutable-byte-bound local references and publishes through the project-package API; it never loads Docker. An OCI profile can additionally load Docker or export to a Distribution registry. This is not the runtime block attached to the VM.

These artifacts may refer to the same source files, but they are not interchangeable and do not share an identity.

```text
Dockerfile + context + local/Hub cache + optional registry cache
                  |
                  v
          BuildKit LLB solve
        (logical vertex cache)
                  |
                  v
             OCI output
             /          \
            v            v
 native package / OCI registry   deterministic compaction
                              |
                              v
                  one verified SquashFS block
                              |
                              v
                  KVM: read-only virtio-blk
```

Keeping these representations separate gives BuildKit enough detail to reuse individual steps without making VM device count, mount count, or OverlayFS lookup depth grow with the number of Dockerfile instructions.

## Remote service boundaries

Native packages/cache, legacy Hub artifacts and Docker/OCI registries use distinct authority and credentials:

| Service | Protocol | Stores | Configuration |
|---|---|---|---|
| Native project registry | HTTPS `api_base`, `/v1/projects/{namespace}/...` | Verified `oci-image` / `runtime-bundle` versions, tags and separately scoped mandatory cache archives | `protocol = "palimpsest"` profile; package key through exact API-base/project helper entry or ephemeral `PALIMPSEST_PACKAGE_KEY` |
| Legacy Palimpsest Hub | Native `/v1/images`, `/layers`, `/bundles` | Boot images, SquashFS runtime blocks and bundles | `PALIMPSEST_URL`; original `PALIMPSEST_TOKEN` |
| Docker/OCI registry | Distribution `/v2` via Docker/Buildx | OCI images and optional external BuildKit registry caches | `protocol = "oci"` profile; Docker credentials |

Hub is not a `/v2` registry. Every online solve uses explicit native package-key cache authority; the old `HubClient.push_blob` rejects BuildKit-cache writes, so a legacy token cannot supply an unqualified cache upload. Runtime SquashFS lookup/upload uses a separate original-token `runtime_hub_client`, never the native key. This does not grant native package keys privileged server-build authority; `/v1/builds` remains system-admin-only.

Profiles live in `${XDG_CONFIG_HOME:-~/.config}/palimpsest/registries.toml`. Unqualified selection is `--registry`, then `PALIMPSEST_REGISTRY`, then default. A fully qualified tag whose authority has no configured profile stays an ordinary OCI reference, as before. A configured authority must map to one protocol, and an explicit alias must match the authority. OCI builds canonicalize tags against the selected profile only with `--push` or `--registry`; native builds always do. Native HTTPS requires the same authority as `endpoint`, verifies system/profile CAs and refuses redirects. Its helper key is `api_base.rstrip('/') + '/projects/' + namespace`, not the host-only Docker credential entry. See [registry profiles](registries.md) for secret-once issuance, login and binding prerequisites.

OCI mirror/CA/plain-HTTP/TLS-skip settings affect BuildKit only through the generated daemon config after it is applied to a separately configured builder. Native profiles reject mirrors/insecure transport/Docker exporters, are omitted from this daemon config, and apply their CAs directly to native HTTPS.

## Identity and cache contract

Palimpsest uses distinct digests for distinct questions:

| Identity | Answers | Used for |
|---|---|---|
| BuildKit cache key | Can this solve vertex reuse an earlier result? | Logical build reuse |
| OCI blob digest | Are these exact bytes already present and intact? | Content-addressed storage and transfer |
| OCI manifest digest | Which config and ordered OCI layers form the image? | Build output identity |
| Runtime-pack policy digest | Which compaction, cleanup, filesystem, and compression rules were used? | Conversion-cache validity |
| Runtime SquashFS digest | Are these exact block-image bytes already present and intact? | VM attachment and Hub upload |
| Runtime base digest | Which immutable qcow2/raw boot image supplies the VM? | Per-run qcow2 backing image |
| Registry configuration digest | Which secret-free profile/cache configuration selected this output? | Build receipt and reproducibility evidence |

A BuildKit cache key is not a layer blob hash. Palimpsest's canonical pre-build key locates a portable local-exporter archive; its SHA-256 verifies the transported bytes, and BuildKit remains the authority that validates and consumes the cache records inside it.

The canonical BuildKit key includes the complete local context, Dockerfile, platform, target, network policy, build-argument digest, pinned local-image descriptors, builder fingerprint and `cache_scope`. It excludes output tags and downstream VM base/SquashFS policy so those do not invalidate reusable vertices. Online Dockerfiles must pin every fully qualified remote `FROM` and external `# syntax=` frontend as `@sha256:<64hex>`; mutable tags and ARG-expanded image sources are rejected before the solve and builder inspection. Cache-key and scope fallback remain bound to the authenticated project/package partition. Profiles never rewrite Dockerfile inputs.

The native cache descriptor remains `palimpsest-buildkit-cache-archive-v1` and requires seven binding fields: `project_id`, `namespace`, `package`, `build_key`, `cache_scope`, `platform`, `builder_fingerprint`. `oci_manifest_digest` is optional provenance, not cache authority. Download, upload receipt and wrapper validation compare all binding fields. Keystone project/user IDs are exact opaque bounded ASCII strings (up to 64 characters, including federated 64-hex identities), not UUID-normalized or case-folded values; UUID normalization is only for Hub-generated key/session IDs.

The initial implementation transports one deterministic tar per exported cache. This preserves instruction-level execution reuse after import, but it does not yet deduplicate transfer bytes across two different cache archives. The next storage-efficiency milestone is to publish the BuildKit cache manifest and referenced OCI blobs separately (or expose an OCI registry cache endpoint) so Hub transfers fetch only missing digests. Benchmark reports must distinguish “vertices reused” from “bytes avoided”; they are not the same result in the archive-based implementation.

For runtime compaction, the conversion key must include at least:

- the source OCI manifest digest and ordered uncompressed layer identities;
- the target architecture;
- the runtime-pack policy and tool version;
- SquashFS compression, block size, cleanup policy, and normalization policy;
- the runtime profile or ABI constraint that affects activation.

The implemented v2 runtime descriptor binds the source OCI manifest, the SHA-256 of the exact metadata-preserving rootfs tar consumed by the packer, runtime base, platform/architecture, SquashFS policy, block size, canonical root directory ownership/mode (`0:0`, `0755`), and the exact packer toolchain identity. That identity hashes the resolved `mksquashfs` executable plus every dynamically linked zstd library in addition to recording its reported version, so distro patches or compressor-library changes cannot share a conversion key. The descriptor is embedded at `.palimpsest/runtime-pack.json` before compaction, so changing the input bytes, base, or pack policy changes both the conversion key and resulting block bytes. A verified local key hit skips compaction; otherwise an online build queries Hub by that exact key and verifies the descriptor and block before reuse.

When a runtime block is requested, the CLI resolves the immutable runtime base before BuildKit starts and requires its cloud-image architecture to match the build platform: `linux/amd64` maps to `x86_64`, and `linux/arm64[/variant]` maps to `aarch64`. The runtime artifact metadata records both spellings so a mismatched block/base pair fails at build time instead of at guest execution.

## Online build: Hub-first, verified, and fail-closed

The online default is Hub-first cache resolution. "Hub-first" means the Hub cache index is consulted before Palimpsest permits BuildKit to solve. An exact-key hit, or the latest same-scope archive used for partial reuse, is downloaded and SHA-256 verified even when an older local scope cache exists. Only an authoritative Hub miss permits fallback to the local scope cache or a cold solve.

Each online local cache partition is single-flight from context hashing through remote resolution, solve, upload and promotion. It is keyed by the selected native API base, namespace, authenticated project, exact package, scope, platform and builder fingerprint. Different native services sharing a host cannot reuse each other's local generations; a scope label alone is not project isolation. Exported caches move into `generations/<build-id>/` before atomic replacement of `current.json`; a crash preserves the previous authoritative generation.

For native output, cache profile/package default to the output profile and namespace/package; explicit `--cache-registry NATIVE_ALIAS` and `--cache-package NAMESPACE/PACKAGE` can select another authorized cache partition. Online OCI output requires **both flags** and never uses `PALIMPSEST_TOKEN` as cache fallback. A key must have both `cache:read` and `cache:write`, even without `--push`. Native builds reject Docker cache exporters; OCI-profile and command-line exporters remain additive to the native imported cache and local exporter, never bypassing resolution or refresh.

```bash
RUNTIME_BASE=sha256:<boot-image-digest>

palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --registry corp \
  --cache-registry hub --cache-package project-apps/demo \
  --tag demo:v1 \
  --tag demo:stable \
  --platform linux/amd64 \
  --runtime-base "$RUNTIME_BASE" \
  --runtime-tag demo-runtime \
  --cache-from type=registry,ref=registry.example.com/cache/demo \
  --cache-to type=registry,ref=registry.example.com/cache/demo,mode=max \
  --push \
  --runtime-push
```

The source execution order is:

1. Resolve output protocol and exact cache authority; authorize cache actions (and package actions when native `--push` is requested) before local build state or builder activity.
2. Resolve/check a requested runtime base, validate immutable Dockerfile inputs, inspect/pin the Buildx builder and compute the canonical build key.
3. Query native exact-key cache, then compatible same-partition scope fallback. Download/verify any hit before import; only an authoritative remote miss permits local fallback or cold execution.
4. Solve and export OCI plus local cache. For OCI output, Docker `--load` and Distribution `--push` exporters are part of this solve, not delayed native publication.
5. For native output, freeze the verified archive and write typed local references immediately after export, before later cache upload can fail.
6. Wrap the exported cache with its seven-field binding, upload through the native cache API, verify the receipt and promote the local generation.
7. If requested, reuse/create and verify the SquashFS runtime pack, store its runtime tag, and upload it through the separate original-token Hub client for `--runtime-push`.
8. For native `--push`, re-snapshot the retained export and publish each tag for the one exact namespace/package via package API compare-and-set. Cache or publication failure does not erase an already-valid native archive/reference.

The online resolver is fail-closed:

- authentication failure, timeout, malformed metadata, HTTP 5xx, or an incomplete cache descriptor stops the build;
- corrupt downloaded bytes are deleted and never passed to BuildKit or attached to a VM;
- a Hub failure is not silently converted into a cache miss;
- rebuilding without Hub participation requires the explicit offline mode described below.

This rule prevents a transient Hub problem from triggering an expensive or nondeterministic rebuild that looks like a valid cache miss.

SHA-256 proves byte integrity, not publisher intent. Cache-write keys and their currently verified owners are part of that exact project/package's trust domain; keep those actions restricted to trusted build identities. Package-read/write actions alone cannot authorize caches, and caches are not runnable packages or package-inventory success. Signed provenance is not claimed by this implementation.

### Publish outputs after a local build

Without `--push`, the OCI output is not published to a registry. Without `--runtime-push`, the runtime block stays local. An online build still refreshes the mandatory Hub BuildKit cache in either case. The runtime layer can be uploaded explicitly:

```bash
palimpsest layer push demo-runtime \
  --base-image "$RUNTIME_BASE"
```

For a BuildKit runtime tag, this deferred path reads the verified CAS sidecar and preserves its runtime-pack conversion key and normalized architecture in the Hub descriptor; it rejects missing or conflicting base/parent metadata instead of silently registering a generic or mis-architected SquashFS layer.

An upload declares the SHA-256 digest first. If the Hub already has the registered blob, the payload transfer is skipped only after the client verifies that its name, kind, media type, chain/base identity, and architecture are compatible with the requested descriptor. Incompatible reuse fails explicitly instead of reporting a false successful registration. Otherwise, the client resumes at the Hub-confirmed byte offset and finalizes metadata only after the Hub rehashes the complete content. The current Hub schema still permits one canonical descriptor per blob digest; first-class multi-tag/ref aliases remain a follow-up schema change.

Native tags have typed local references under `state/package-references/<sha256(canonical-reference)>.json` (schema `palimpsest-local-package-reference-v1`, mode `0600`); retained exports are `state/package-artifacts/<archive-digest-hex>.oci.tar`. Each pins root and archive identities plus authority/API base, namespace/package and available project/build/publication metadata. A later native `push` uses that reference, or explicit `--input LAYOUT_OR_ARCHIVE` with `--manifest` for ambiguous roots. Native `pull --output ARCHIVE` verifies and returns original transport bytes, not Docker loading. Native builds reject `--load` and keep these references separate from runtime-layer tags. On OCI profiles, `--load` still loads Docker-format output. Neither native nor Docker image publication substitutes for `--runtime-push`.

## Strict offline build

Offline mode is an execution policy, not merely a missing Hub URL. It runs with no Hub or registry resolution, never loads `registries.toml` and never looks up credentials, and BuildKit `RUN` networking is disabled. An offline build therefore records no typed native package reference. To publish its archive later, use native `push --input ARCHIVE --manifest sha256:...`.

Prepare a verified OCI layout on local storage and pin the image identity in the argument itself:

```bash
RUNTIME_BASE=sha256:<locally-imported-boot-image-digest>

BUILDX_BUILDER=palimpsest-offline palimpsest build . \
  --frontend dockerfile \
  -f Dockerfile \
  --tag demo \
  --runtime-base "$RUNTIME_BASE" \
  --runtime-tag demo-runtime \
  --offline \
  --local-image local-base=/absolute/path/to/base-layout@sha256:<manifest-digest> \
  --network none
```

The corresponding Dockerfile uses the declared alias instead of a remote registry reference:

```dockerfile
FROM local-base
WORKDIR /opt/app
COPY . .
RUN ./scripts/build.sh
```

Strict offline mode requires all of the following:

- the build context is local;
- every `FROM` reference is `scratch`, a prior stage, or a digest-pinned `--local-image` alias;
- every reachable OCI image config has no `OnBuild` trigger;
- every `COPY`/`ADD --from` and `RUN --mount ... from=` names a declared local-image alias or prior stage;
- the runtime base digest is already in the local store;
- only the local BuildKit cache is imported;
- `--network none` is enforced for build steps;
- the selected local `docker-container` BuildKit daemon is already bootstrapped with Docker network mode `none`;
- Palimpsest registry profiles are not loaded and registry authentication is not invoked; Docker may still read its selected `DOCKER_CONFIG` to locate the already-configured local context and builder;
- the Hub client, remote registry resolver, and remote cache exporter are not constructed;
- `--registry`, `--cache-registry`, `--cache-package`, `--pull`, `--push`, `--runtime-push`, external `--cache-from`/`--cache-to`, and network-enabled build steps are rejected before solving.

Missing local input is an error that names the unresolved alias or digest. Offline mode never reaches out to make the build succeed.

## Run the generated runtime block

The build records `demo-runtime` as a local tag and prints or records its immutable digest. Run the block with the existing KVM interface:

```bash
RUNTIME_DIGEST=sha256:<runtime-squashfs-digest>

palimpsest run "$RUNTIME_BASE" \
  --layer "$RUNTIME_DIGEST" \
  --name demo

palimpsest exec demo -- ls -la /opt/layers/merged
```

The initial block-runtime target keeps the current v1 activation path: the SquashFS filesystem is mounted under `/mnt/palimpsest/lower0` and exposed through `/opt/layers/merged`. It does not pivot `/`, replace `/usr`, or claim full OCI rootfs equivalence.

## Block transport and backend boundaries

The Linux KVM implementation already uses block transport:

- the immutable qcow2/raw boot image backs a per-run writable qcow2 overlay attached as `vda`;
- SquashFS layer files are attached read-only as raw `virtio-blk` disks;
- the guest resolves each disk through its digest-derived `/dev/disk/by-id/virtio-*` serial;
- OverlayFS upper/work directories live on the guest's local writable disk.

There is no active NFS layer-attachment path in this repository. Work described as "NFS to block" therefore applies to external Hub/OpenStack deployment paths and to consolidating many existing block layers into one runtime block, not to replacing an NFS path in the local KVM code.

The macOS Lima backend has a different proof level. It copies SquashFS files into the guest with SCP, mounts them through loop devices, and then builds the same merged OverlayFS view. Lima is useful for functional development on Apple Silicon, but it is not evidence for production block attachment, zero-copy transfer, or Linux KVM startup performance.

Attachment is therefore execution metadata, not an intrinsic property of a SquashFS blob. Runtime artifact metadata records its platform and architecture but does not claim an `attach_bus`. Each VM run state records the selected path: Linux KVM uses direct `virtio-blk` plus a read-only SquashFS mount, while Lima records SCP delivery plus a read-only loop mount.

### VMM sequencing

Keep QEMU/libvirt as the correctness and compatibility baseline because that is the implemented lifecycle today. Once clean-host block and workload-ready receipts are stable, add [Cloud Hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor) as the first production A/B candidate: its deliberately small modern-cloud [device model](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/device_model.md) supports `virtio-blk`, x86-64, and AArch64 without requiring Palimpsest to change its block artifact contract. Treat [Firecracker](https://firecracker-microvm.github.io/) as a later specialized profile for direct-kernel, tightly controlled Linux guests rather than as the default replacement for general cloud-image compatibility. VMM selection must be decided from the same workload-ready, RSS, I/O, and concurrency receipts—not from an isolated process-start number.

## Performance research plan

The main performance question is not only "How fast did the VM boot?" It is "How long until the requested workload succeeds?"

```text
workload_ready = resolve + cache_lookup + fetch + verify
               + build_or_export + compact + block_prepare
               + VMM_start + kernel_ready + device_ready
               + mount + activate + process_start + first_success
```

### Experiment matrix

Run every performance result with enough repetitions to report p50, p95, and p99. Record cold and warm conditions separately.

| Axis | Required cases |
|---|---|
| Cache source | no cache, local BuildKit cache, Hub cache with empty local store, Hub cache with local blobs, additive registry cache |
| Connectivity | online success, authoritative Hub miss, Hub failure, strict offline |
| Runtime representation | legacy ordered block layers, one compacted SquashFS runtime block, monolithic qcow2 baseline |
| Read-only filesystem | SquashFS production default; EROFS candidate with equivalent source/policy binding |
| Dockerfile shape | 1, 5, 25, and 100 logical vertices; repeated unchanged prefix; changed early and late vertex |
| Workload | small CLI, many-small-file Python/Node application, large binary/toolchain image |
| Compression | fixed production default plus selected SquashFS block-size/compression candidates |
| Cache temperature | empty local CAS, populated CAS, cold host page cache, warm page cache |
| Concurrency | 1, 10, 50, and 100 simultaneous resolves/starts where the host can sustain them |
| Transfer condition | local only, Hub LAN, bandwidth/latency constrained Hub fixture |

An NFS comparison may be run as an isolated research fixture when the external deployment environment still exposes it. It must not be described as an active local Palimpsest code path.

### Metrics and receipt fields

Every build and run should emit a machine-readable receipt. At minimum it records:

**Identity and environment**

- `schema_version`, `operation_id`, `mode` (`online` or `offline`), `outcome`, and `cache_temperature`;
- host architecture, kernel, filesystem, CPU count, memory, and storage device class;
- Palimpsest, BuildKit, Dockerfile frontend, SquashFS tool, QEMU, libvirt, and guest image versions;
- source OCI manifest, runtime-base, runtime-pack policy, and runtime-block digests;
- output tags, selected registry profile/config digest, OCI push/load state, pull policy, and one-way digests of external cache definitions.

**Build and cache timing, in milliseconds**

- `context_scan_ms`, `dockerfile_parse_ms`, and `local_cache_lookup_ms`;
- `hub_cache_resolve_ms`, `cache_pull_ms`, and `cache_verify_ms`;
- `llb_solve_ms`, `executor_ms`, and `oci_export_ms`;
- `runtime_pack_create_ms`, `runtime_pack_verify_ms`, `hub_upload_ms`, `manifest_publish_ms`, and `total_ms`.

**Build and transfer efficiency**

- BuildKit vertices total, local hits, Hub hits, misses, and executed;
- blobs checked, present, downloaded, rejected, uploaded, resumed, and deduplicated;
- context bytes, local bytes reused, Hub bytes downloaded/uploaded, OCI bytes, uncompressed filesystem bytes, runtime-block bytes, and deduplicated bytes saved;
- compression ratio and runtime-pack creation peak RSS/CPU time.

**Run timing, in milliseconds**

- `resolve_ms`, `fetch_ms`, `verify_ms`, and `block_prepare_ms`;
- `libvirt_define_ms`, `vmm_start_ms`, `kernel_ready_ms`, and `device_ready_ms`;
- `mount_ms`, `overlay_activate_ms`, `entrypoint_start_ms`, `first_success_ms`, and `workload_ready_ms`.

**Attachment facts**

- source logical-layer count, attached runtime-block count, and total VM disk count;
- bus, read-only state, digest-derived serial, filesystem type, block size, and compression;
- upper/work backing filesystem and whether it is local;
- page-fault, bytes-read, and host/guest CPU counters when available.

Do not combine missing phases into a single boot number. A fast VMM start can hide a slow cache pull, runtime conversion, or first application import.

## Acceptance gates

The workflow is complete only when all applicable gates pass.

### Cache and integrity

- An online Hub cache hit executes zero BuildKit vertices covered by that hit.
- An online exact/scope Hub hit is downloaded and verified before import; a local scope cache is used only after an authoritative Hub miss.
- External cache imports and exports are additive; configuring one cannot disable mandatory Hub resolution and upload.
- Only an authoritative Hub miss permits local execution in online mode.
- Hub authentication, timeout, 5xx, malformed cache metadata, or digest mismatch stops the build.
- A corrupt or partial blob is never promoted, imported, attached, or uploaded as complete.
- Identical source inputs and pack policy produce the same runtime-block digest.
- Concurrent builds of the same cache key use single-flight locking rather than duplicate work.
- Package-only keys cannot authorize mandatory online caches; protected identities and unbound/mismatched project/package partitions fail closed before build effects.
- Native cache descriptor/receipt bindings match all seven fields, and identical scope labels in different projects/packages cannot reuse each other's local generations.

### Offline isolation

- A strict-offline acceptance test succeeds from a local OCI layout and local cache while outbound TCP and DNS are denied.
- The test proves that no Hub client or remote registry resolver was constructed.
- A missing local base, OCI blob, or cache input fails before BuildKit execution or VM creation.
- `--offline` rejects `--registry`, `--cache-registry`, `--cache-package`, `--pull`, both push flags, remote cache definitions and network-enabled build steps; it never loads registry profiles.

### Block runtime

- A compacted image with any supported number of logical Dockerfile layers attaches exactly one read-only runtime block.
- A runtime base whose architecture does not match `--platform` is rejected before BuildKit starts.
- Libvirt XML contains a raw read-only `virtio` disk with the expected digest-derived serial and no NFS or host-filesystem attachment.
- The guest mounts the expected SquashFS digest read-only and keeps OverlayFS upper/work on local writable storage.
- The immutable runtime base and runtime block have the same digest before and after the run.
- Linux KVM, not Lima copy/loop execution, supplies the production block proof.

### Upload and reproduction

- Already-registered content produces zero payload upload bytes.
- Interrupted uploads resume only from the Hub-confirmed offset.
- Cache/output metadata becomes visible only after all referenced blobs exist and verify.
- After clearing the local store, the same online build imports the Hub cache, executes no covered build vertex, attaches the runtime block, and passes the workload check.
- A separately exported local OCI layout reproduces the build, attach, and workload check under strict offline conditions.
- A real Dockerfile native build → deferred push → pull retains the selected root identity and exact original exported archive bytes, without Docker load/registry exporters or administrator write fallback. The 2026-10-02 isolated local proof exercised this path with synthetic identity; deployed-cloud acceptance remains separate.

### Performance release gate

- Receipts include every applicable phase and byte counter listed above.
- Benchmarks publish p50, p95, p99, sample count, cache temperature, host description, and raw receipts.
- One-block activation is compared with the legacy multi-block path at the same source content and concurrency.
- A release-specific p95 regression budget is chosen after the first clean-host baseline and enforced in CI or the KVM release job. Until that baseline exists, performance claims remain hypotheses rather than release claims.

## Troubleshooting

### The online build does not fall back when Hub is unavailable

This is intentional. Online mode is fail-closed so a Hub failure cannot masquerade as a cache miss. Restore Hub access or rerun with `--offline` after provisioning every required local input.

### Offline mode reports an unresolved `FROM`

Add a digest-pinned `--local-image alias=/absolute/path/layout@sha256:...` mapping and use that alias in the Dockerfile. Verify that every blob referenced by the OCI layout exists locally.

### A registry mirror or private CA setting has no effect

`palimpsest registry buildkit-config` only generates a secret-free `buildkitd.toml`; it does not mutate the selected builder. Create or explicitly configure a BuildKit builder with that file, bootstrap it, and select it through `BUILDX_BUILDER`.

### `--push` did not upload the runtime block

`--push` publishes the OCI image. Add `--runtime-push` with `--runtime-tag` and `--runtime-base` to upload the SquashFS runtime block to Palimpsest Hub, or use `palimpsest layer push` afterward.

### A Lima test passes but the block performance gate fails

Lima copies layer files into the guest and mounts them through loop devices. Repeat the proof on Linux x86_64 KVM and inspect the libvirt XML and `/dev/disk/by-id` device.

### The runtime block does not expose a complete container rootfs

The initial workflow activates application/toolchain content at `/opt/layers/merged`. Root pivot and full OCI rootfs semantics remain outside the v1 runtime contract.

## Related documentation

- [Installation](install.md)
- [Quickstart](quickstart.md)
- [Compatibility and integration contract](compatibility.md)
- [Native package and Docker/OCI registry profiles](registries.md)
- [Implementation plan](../IMPLEMENTATION_PLAN.md)
