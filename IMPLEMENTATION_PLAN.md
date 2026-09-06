# Palimpsest Local implementation plan

**Status:** approved; core implementation complete; standalone v0.1.0 release & Afterglow cutover blocked pending real KVM proof
**Repository:** `palimpsest-local`  
**Runtime:** Python 3.12+, Linux KVM/libvirt only for v1

## Goal

Ship an independently installable `palimpsest` CLI that lets a developer pull a verified base/layer stack from Afterglow Hub, build layers in a disposable local KVM VM, run composed stacks locally, enter/inspect them, commit an upper delta as a new SquashFS layer, and push the result back to Hub.

The project must reuse Afterglow's existing content-addressed artifact protocol without importing its FastAPI app, database models, deployment settings, or OpenStack credentials.

## Repository shape

```text
palimpsest-local/
  pyproject.toml
  src/palimpsest_local/
    __init__.py
    errors.py
    digest.py          # parse/stream/verify sha256:<64hex>
    oci_layout.py      # safe blobs/sha256 layout and bundle extraction
    refs.py            # ImageRef, LayerRef, StackRef, RunSpec, BuildSpec
    hub.py             # authenticated HTTP client; upload/download/bundle APIs
    cloudinit.py       # NoCloud metadata/user-data and seed ISO commands
    kvm.py             # libvirt DomainSpec/XML/lifecycle; optional libvirt import
    guest.py           # console/SSH readiness and argument-vector command execution
    build.py           # disposable qcow2 builder and delta capture
    runtime.py         # run/stop/remove/inspect orchestration
    state.py           # atomic, owner-only local state and locks
    cli.py             # argparse command tree
  tests/unit/
  tests/integration/
  tests/kvm/
  docs/
```

Dependencies:

- Base package: Python standard library only where practical.
- `libvirt-python` belongs to optional `kvm` extra and is imported only by KVM operations.
- Host tools required for KVM commands: QEMU/KVM, libvirt daemon/client, `cloud-image-utils`, `squashfs-tools`, and an SSH client.
- Build with Hatchling/uv; use Ruff and pytest.

## Public API boundary

Expose stable immutable objects only:

```python
ImageRef(digest, disk_format, arch, os_variant, local_path)
LayerRef(digest, media_type, local_path)
StackRef(base: ImageRef, layers: tuple[LayerRef, ...])
RunSpec(name, stack, memory_mib, vcpus, network, writable_overlay, seed)
BuildSpec(base, parent_layers, recipe, network, output_name)
```

Rules:

- A verified base `ImageRef.local_path` is immutable.
- `run` and `build` create a new qcow2 overlay in package-owned state before defining a domain.
- The overlay is the only RW `vda`; base qcow2/raw is never attached RW.
- Layers are immutable RO virtio-blk disks.
- The package never accepts an Afterglow config object or raw OpenStack credential.

## Local state and ownership

```text
${XDG_CONFIG_HOME:-~/.config}/palimpsest/config.toml
${XDG_STATE_HOME:-~/.local/state}/palimpsest/
  store/blobs/sha256/<hex>
  runs/<name>/
    owner.json
    state.json
    overlay.qcow2
    seed.iso
    console.log
    ssh/
  locks/<name>.lock
  transfers/<digest>.json
  tags/<tag>.json
  builds/<build-id>/record.json
```

- Config/state/key directories use owner-only permissions.
- `PALIMPSEST_URL` and `PALIMPSEST_TOKEN` support CI/non-interactive use.
- Do not save bearer tokens in state files. A future OS-keychain backend may be opt-in.
- All managed libvirt domains receive a package metadata marker and a run UUID.
- `rm` refuses any domain, disk, seed, key, or path not tied to a validated package-owned state record.

## Runtime sequence

```text
palimpsest run <image-or-bundle> --layer <digest> --name dev

1. Resolve base and complete parent chain by immutable digest.
2. Ensure each local blob exists, has a safe path, and rehashes to its declared digest.
3. Create `<state>/overlays/dev.qcow2` backed by the immutable verified base blob.
4. Generate NoCloud seed data and a per-run SSH keypair.
5. Define a libvirt domain:
   - vda: per-run qcow2 overlay, RW
   - vdb..vdz: SquashFS layers, RO raw virtio disks
   - seed ISO: RO CD-ROM
   - layer serial: digest-derived, max 20 characters
6. Start the domain and wait for guest readiness.
7. Guest finds each layer by `/dev/disk/by-id/virtio-<serial>`, mounts it read-only,
   then builds OverlayFS with lowerdir ordered leaf → root and local upper/work.
8. Persist state only after the domain reaches the defined/start boundary.
```

The merged application environment remains `/opt/layers/merged`; no root pivot or `/usr` replacement is part of v1.

## Command groups

### Artifact operations

```text
palimpsest image ls [--ubuntu-base ...] [--arch ...]
palimpsest image pull <digest> [--output DIR]
palimpsest image verify <path> --digest sha256:...
palimpsest layer ls [--name ...] [--parent sha256:...]
palimpsest layer pull <digest>
palimpsest layer pack <directory> --tag <name>
palimpsest layer push <tag|path> [--parent sha256:...]
palimpsest bundle pull <leaf-digest> --include-base --output DIR
palimpsest bundle verify <directory>
```

- Pull streams data to a temporary file, verifies SHA-256, then atomically promotes it.
- Upload uses the existing Hub session protocol and does not silently accept an existing unregistered blob.
- Bundle extraction rejects unsafe tar paths and verifies every declared blob.

### Runtime operations

```text
palimpsest run <image-or-bundle> [--layer sha256:...] --name <name>
palimpsest ps
palimpsest inspect <name>
palimpsest logs <name> [--follow]
palimpsest shell <name>
palimpsest exec <name> -- <command> [args...]
palimpsest stop <name>
palimpsest rm <name> [--volumes]
```

- `exec` uses an argument vector and SSH; it does not compose the user command into a host shell.
- `shell` uses the per-run key and discovered guest address.
- `logs` reads the package-owned serial console/domain log only.
- `rm --volumes` removes the run overlay, seed, and generated key; it never touches the immutable base/layer cache.

### Builder operations

```text
palimpsest build --base <image-digest> [-f Palimpsestfile] [--layer sha256:...]
palimpsest commit <name> --tag <layer-name>
```

- `build` makes a disposable qcow2 overlay, bootstraps a guest, executes a constrained recipe inside that guest, captures only the designated local upper delta, cleans volatile paths, creates a SquashFS blob, and verifies its digest.
- `commit` captures an explicitly named package-owned run only. It fails if the upper/work filesystem is network-backed or if run state cannot prove the selected parent chain.
- First-release `Palimpsestfile` grammar: `FROM`, `RUN`, `ENV`, `WORKDIR`, `LAYER`.
- Reject `COPY`, `ADD`, multi-stage builds, host paths, privileged mounts, implicit secret forwarding, and arbitrary device passthrough.
- Default build networking is `none`; package download needs explicit `--network default`.

## Phases

### 0. Contract tests and bootstrap

1. Set up the package, console entry point, optional KVM extra, lint/test config, and release metadata.
2. Port the existing pure tests before porting implementation:
   - digest and blob-path validation
   - layer serial derivation and 25-disk limit
   - root RW / layer RO XML rules
   - NoCloud seed command uses argument lists, not a shell
   - guest by-id lookup, layer order, local upper/work
3. Add golden fixtures for domain XML, seed data, OCI layout, and activation scripts.
4. Write compatibility documentation describing the immutable-base-overlay invariant.

**Exit criterion:** fixtures match the Afterglow contract while importing no Afterglow package.

### 1. Artifact client and compatibility CLI

1. Port pure digest, OCI-layout, and Hub client logic.
2. Implement `image`, `layer`, and `bundle` commands as compatible replacements for the current Afterglow script.
3. Add fake-HTTP tests for resumable upload, upload short-circuit, malformed/foreign data, Range pull, digest mismatch cleanup, and media-type distinctions.

**Exit criterion:** every pulled or unpacked blob is verified and unsafe paths are rejected.

### 2. KVM lifecycle

1. Port/reshape libvirt XML, NoCloud, domain lifecycle, and layer activation primitives.
2. Add atomic local state, locks, ownership marker checks, crash recovery, console readiness, and SSH readiness.
3. Implement `run`, `ps`, `inspect`, `logs`, `shell`, `exec`, `stop`, `rm`.
4. Test against a fake libvirt adapter, then run a real local proof with a bootable Ubuntu qcow2 and three SquashFS layers.

**Exit criterion:** `run → exec → stop → rm` completes cleanly; base and layer files are unchanged; foreign domains are refused.

### 3. Builder and commit

1. Implement recipe parsing and validation.
2. Create disposable builder overlays and execute recipe steps in the guest.
3. Capture and clean deltas, pack SquashFS, persist immutable parent/base/build metadata, and make publication explicit with `layer push`.
4. Prove a locally built Python layer works in a fresh `run` stack.

**Exit criterion:** failed builds clean temporary domains/overlays; successful blobs pass digest verification before any upload.

### 4. Afterglow adapter

1. Publish/tag a stable package release.
2. Add it to Afterglow as an exact required `palimpsest-local==0.1.0` dependency; API/container images do not install its `[kvm]` extra.
3. Delete `backend/app/services/palimpsest_kvm.py` outright.
4. Retain a one-minor-release wrapper for `scripts/palimpsest.py` that provides migration guidance.
5. Update Afterglow's local KVM runbook and add a compatibility CI job.

**Exit criterion:** Afterglow starts without libvirt installed, and pinned-package compatibility tests cover its API boundary.

### 5. Release proof

On a clean Linux KVM host:

```text
image/bundle pull
→ run three-layer stack
→ inspect by-id disks and OverlayFS
→ exec Python from merged path
→ commit delta
→ push
→ clean local state
→ pull and run the resulting stack again
```

Collect separate timings for pull, bundle extraction, seed creation, boot-to-SSH, overlay activation, first import, commit, and push. Do not represent this work as a Cinder/Nova boot-volume optimization.

## Security guardrails

- SHA-256 verification before attachment and before upload completion.
- Closed validation for paths, tags, names, digest values, and domain identifiers.
- No host directory sharing, agent forwarding, environment secret injection, cloud credential injection, or privileged device passthrough.
- Builder code is local-user trusted guest code only.
- State mutations are lock-guarded and atomically written.
- Remote `qemu+ssh://` hosts, multi-host scheduling, GUI, root overlay/pivot, Ubuntu base SquashFS runtime use, OpenStack virtio transport, and OCI `/v2` registry compatibility are out of v1 scope.

## Verification matrix

| Layer | Evidence |
|---|---|
| Unit | digest/path validation, OCI safety, command parsing, XML/seed goldens, layer order, no-shell commands |
| Integration | fake Hub upload/download, state locking, interrupted operations, foreign-domain refusal, crash recovery |
| Real KVM | three-layer boot, by-id discovery, RO layer mounts, OverlayFS, `exec`, `commit`, push/pull reproduction |
| Security | traversal/metacharacter rejection, unverified blob refusal, owner-only key/state permissions, no-secret injection |
| Afterglow | public API-only import, optional dependency behavior, existing Hub/KVM regression selectors |

## Source evidence in Afterglow

- `scripts/palimpsest.py:264-309`
- `docs/palimpsest-local-kvm-runbook.md:39-187`
- `backend/app/services/palimpsest_kvm.py:54-304`
- `backend/tests/test_palimpsest_kvm.py:57-275`
- `backend/app/services/palimpsest_hub_store.py:37-50`
- `openspec/changes/palimpsest-layered-vm/tasks.md:116-188`

## OCI-root modernization continuation (2026-08-31)

The original v0.1 plan above describes the cloud-image plus mounted-layer runtime and remains historical context. The current OCI-root program treats Docker/OCI image layers as source artifacts, derives deterministic read-only SquashFS lower layers, and will eventually make their merged tree the VM's actual `/`. It must not claim that runtime behavior until the boot acceptance gate below passes.

### PR 4 slice 6: exec-owned hard materialization boundary

Implemented and verified:

- A Linux-only parent supervisor gives one complete layer materialization attempt a single monotonic wall-clock deadline. The worker starts in a new session/process group; timeout uses TERM, a bounded grace interval, KILL, and reap. Late success is discarded.
- Parent/worker messages are bounded to 256 KiB and use exact-field, duplicate-key-free, finite, canonical UTF-8 JSON. A UUID nonce and canonical request digest bind the response. Failures expose only stable categories.
- The exec worker owns `SourceCAS lease → staged intake → deterministic pack → derived CAS/record/key/occurrence publication`. Source graph, occurrence, recipe key, store identity, source-CAS identity, packer bytes, and dependency-bound toolchain identity are reconstructed or revalidated inside the worker.
- `SourceCAS.open_existing` never creates a missing root or component. A warm derived-cache hit does not enter the lazy source/toolchain producer.
- Supervised `mksquashfs` inherits the worker process group and creates its private pack directory below the worker's owner-only scratch. Standalone pack behavior retains its own process group.
- The worker applies core, CPU, open-file, file-size, address-space, and process-count resource limits. This is not described as network isolation; a later kernel-enforced sandbox must fail closed before any `network=none` claim is made for the converter.
- Scratch cleanup is tied to process reap. If a Linux task cannot be reaped immediately, a background reaper retains the scratch authority and removes it only after the process exits.

Verification recorded for this slice:

- Ruff lint and format, compile, package sdist/wheel, and `git diff --check`: pass.
- Local unit and non-privileged OCI suite: 2151 passed, 9 skipped, 3 deselected.
- Product-level local BuildKit gate: 2 passed with `PALIMPSEST_BUILDKIT_E2E=1`.
- Privileged Linux `mksquashfs 4.6.1` container: hard-worker cold materialization followed by warm hit passed; the leased artifact bytes matched the receipt digest.
- `mksquashfs 4.5` is rejected by the existing minimum-version preflight, as intended.

### PR 4 slice 7: descriptor-pinned physical mutations and lease-safe deletion

Implemented and verified:

- `ArtifactStore` is now the sole production authority for shared `${state}/store/blobs/sha256` publication and deletion. Generic legacy blobs are written through the same owner-bound directories, per-digest `flock`, no-follow descriptors, hash verification, sealing, rename, and parent-directory `fsync` boundary as OCI-derived artifacts.
- Re-publishing an already-valid digest consumes and verifies the proposed bytes but preserves the existing inode. This prevents legacy `ContentStore` ingestion from invalidating an active OCI same-FD reader. Corrupt or missing targets can still be repaired under the digest lock.
- Physical deletion requires an explicit retention guard, re-hashes the pinned target descriptor, verifies its directory binding, unlinks through the directory FD, and durably syncs the parent. The legacy unguarded `ContentStore.delete` path now fails closed.
- `OCIStore.assert_artifact_unleased` acquires the durable lease-index lock while the caller holds the artifact digest lock, strictly validates every committed lease, owner, receipt, occurrence, and record binding, and refuses deletion when the target is retained. The lock order matches lease acquisition (`artifact digest → lease index`), so acquisition and deletion have a single winner without a dangling durable lease.
- Run/tag reference commits and removal scans share a state-root reference lock. Run writers validate referenced local/shared targets before committing; tag writers reject missing targets; inventory strictly validates the `base`/`base_digest`/`layers` ledger shapes. A reference commit can therefore win before the scan, or observe the completed deletion and fail, but cannot land in the scan-to-unlink gap.
- Inventory removal no longer unlinks CAS paths directly. Physical unlink and metadata/tag cleanup complete under the same digest lock, invalid run/tag ledgers and malformed OCI retention metadata block removal, and removed metadata/tag directory entries are explicitly synced.
- Inventory canonicalizes configured state/config roots once at the removal boundary, so platform-level directory aliases such as macOS `/var` → `/private/var` use the same lock and descriptor-pinned store authority instead of failing or splitting synchronization domains.
- Tag payloads are validated and bound to their filename. Removal pins owner-bound `tags` and `store/metadata` directories with no-follow FDs, reads tag ledgers through that authority, and removes only the scanned/digest-derived entries with dirfd-relative unlink plus `fsync`; nested directory symlink substitution cannot redirect cleanup outside the state root.
- CLI image/layer pulls now download to private staging outside the CAS namespace and then call `ContentStore.ingest_file`; the transport client's `os.replace` can no longer target the final shared blob path. Existing unsealed legacy downloads are re-fetched and repaired through the same boundary.
- Adversarial tests cover both acquisition/deletion orderings, active and recoverable leases, malformed lease/run ledgers, late run-reference commits, same-digest cleanup/republish, legacy same-digest inode stability, CLI pull staging, poisoned target repair, missing retention guards, and unlink/`fsync` fault reporting.

Verification recorded for this slice:

- Ruff lint/format, compile, package sdist/wheel, and `git diff --check`: pass.
- Complete local `tests` tree: 2173 passed, 15 skipped; the skips are environment-gated Linux/KVM/product cases.
- Product-level local BuildKit gate: 2 passed with `PALIMPSEST_BUILDKIT_E2E=1`.

### PR 4 slice 8: first-party local OCI intake and ordered image materialization

Implemented:

- `LocalArchiveSource` accepts a digest-pinned, uncompressed standard OCI image-layout tar. It pins the archive as a no-follow regular file, rejects links/devices/duplicates/noncanonical names and unexpected files, bounds member count and plain-tar expansion, stages only the OCI layout whitelist, then reuses `LocalLayoutSource` for strict JSON, platform, descriptor size/SHA-256, and selected-graph verification.
- Layout and archive intake both preserve manifest layer occurrence order. A repeated descriptor is stored once in the source CAS but remains two ordinal occurrences in the selected image.
- `materialize_image_hard` invokes the existing Linux exec-worker boundary for every ordinal under one image-wide monotonic deadline. It does not digest-deduplicate occurrences. Its canonical path-free receipt binds source snapshot/image, selected manifest, exact `linux/amd64` platform, store-bound per-occurrence receipts, order, and invocation-local cold/warm result.
- Partial success is explicitly immutable derived-cache state, not a runtime lease. If a later occurrence fails, earlier cache entries may remain for retry; this slice does not claim boot retention or activation.
- The additive CLI entry point is:

  ```text
  palimpsest oci materialize IMAGE.OCI.TAR \
    --manifest sha256:<pinned-index-or-manifest> \
    [--packer /usr/bin/mksquashfs] [--timeout 300] [--output receipt.json]
  ```

  A layout directory is accepted in the same position. Existing `run` semantics are unchanged.
- Portable tests cover archive/layout equivalence, exact `linux/amd64` index selection, repeated occurrences, archive mutation and ambiguous-member rejection, CLI dispatch, path-free receipt output, ordered orchestration, and fail-stop behavior. The Linux OCI filesystem suite adds a real two-layer image-wide cold-then-warm hard-worker proof.

Verification recorded for this slice:

- Complete local `tests` tree: 2184 passed, 16 skipped; skips are the intentionally gated Linux/KVM/BuildKit/filesystem cases on macOS.
- Focused source/CLI/completion/state/worker/filesystem suite after path-alias correction: 321 passed, 6 skipped.
- Ruff lint and format, Python compile, `git diff --check`, sdist, and wheel build: pass.
- The repository-wide bare `pytest` command additionally discovers the separately packaged Hub tests and cannot collect them in the root-only environment without Hub dependencies; the established root product boundary is `pytest tests`.

Scope boundary: this receipt is not yet an OCI-root boot plan. Durable lower leases, VM-specific writable root volume, retained boot-volume reuse, kernel/initramfs, stage-1 pivot, init supervision, and foreground/`-d` runtime lifecycle remain subsequent slices. Gate 2 stays inactive.

### PR 4 slice 9: crash-recoverable immutable-lower boot-plan reservation

Implemented:

- `OCIStore.acquire_lease_set` binds the complete ordinal-preserving materialization receipt tuple, run owner, and canonical boot-plan digest to one immutable lease-set intent. The set and every member lease ID are deterministic, so a retry after interruption converges on the same records instead of duplicating ownership.
- Publication holds all unique artifact digest guards in canonical order, validates every occurrence/artifact, then commits the intent before member leases under the lease-index lock. The retention guard validates both intents and individual leases, so even a crash after the intent but before the first member prevents physical GC of every planned lower.
- A complete set can be loaded only after strict owner, plan, source, occurrence, ordinal, lease, and artifact validation. A partial set can be rolled back from its durable intent; interrupted release removes the intent last and can retry over already-absent members.
- `list_lease_set_intents` enumerates complete and partial intents with exact owner/run identity and present-member state, including after process restart. A reconciler can therefore resume a known plan or roll back an orphan instead of leaving intent-only GC retention undiscoverable. Durable run-ledger phase commit is intentionally deferred to the writable-root ownership transaction.
- `DurableDerivedLayerLease.detach` and `release_recoverable_lease` make durable retention without an open reader explicit. Release serializes with the per-lease use lock and does not require streaming the whole SquashFS merely to roll back ownership.
- `OCIBootPlanIntent` is a canonical path-free contract for one `oci-root`/KVM run. It preserves every layer occurrence, binds source graph/config/platform/derived receipts, declares only the `lower-reserved` phase, and records the VM-specific writable-root policy without claiming that a writable volume exists yet.
- `PreparedOCIBootPlan` is returned only when the exact ordered lower lease set is complete. Recovery and release APIs reject owner/plan rebinding.
- Fault-injection tests cover intent-only and partial-member crashes, deterministic retry, explicit partial rollback, interrupted release retry, malformed-intent fail-closed behavior, shared physical artifacts at multiple ordinals, GC retention, path-free plans, and detached single-lease cleanup.

Verification recorded for this slice:

- Complete local `tests` tree: 2197 passed, 16 skipped, 1 existing fork deprecation warning; skips are environment-gated Linux/KVM/BuildKit/filesystem cases.
- Focused OCI store/boot-plan suite: 46 passed.
- Ruff lint/format, Python compile, `git diff --check`, sdist, and wheel build: pass.

Scope boundary: this slice reserves the immutable lower graph but does not create a root disk, emit a libvirt domain, select a kernel/initramfs, assemble/pivot `/`, supervise image init, or implement foreground/`-d`. Gate 2 remains inactive.

### PR 4 slice 10: VM-specific writable root ownership and run-ledger preparation

Implemented:

- OCI-root writable ext4 disks now live in a dedicated owner-only `${state}/oci-root-volumes` authority, separate from shareable project volumes. Each volume has a canonical UUID, deterministic filesystem label, exact size and lower-graph binding, explicit `delete` or `retain` policy, generation, lifecycle state, and at most one attached run owner.
- New volume creation commits a `creating` owner intent before publishing raw ext4 bytes, then promotes it to `attached`. The physical artifact is sparse raw ext4 with no backing file and is verified before reuse. Owner records use canonical JSON and descriptor-relative atomic publication. A deterministic deletion quarantine makes rename-before-unlink crashes recoverable.
- The root disk is VM-specific by default. Teardown removes a `delete` root with the run; `retain` detaches it for later use. Reuse requires an explicit retained volume ID plus exact size and immutable lower-graph digest, and the retained volume remains single-writer rather than becoming a shared root disk.
- `OCIBootPlanIntent.lower_graph_digest` identifies the immutable OCI graph independently of run identity and invocation-local cold/warm cache results. This allows a retained writable root to be accepted only with the same ordered source/config/platform/layer/DiffID/derived-receipt graph.
- `prepare_oci_root_run` writes a path-free `resources-planned` run-ledger record before acquiring resources, acquires the deterministic lower lease-set and exclusive root volume, then commits `resources-ready`. Ordinary failures roll back only the exact claim provenance and lower intent; incomplete rollback is recorded as `rollback-required`.
- Restart reconciliation cross-validates the embedded boot plan, ordered receipt/occurrence tuple, loaded lower lease members, run owner, lower-graph digest, and root-volume attachment. A planned transaction is rolled back exactly; a ready transaction is recovered without formatting or duplicating either resource.
- Teardown has durable `release-required` and `released` phases. Restart can finish after either root detach/delete or lower release has already succeeded. Already-retained roots and already-absent deleted roots are terminal idempotent states.
- This slice intentionally stops before libvirt domain definition. The raw ext4 root volume is ownership/transaction infrastructure and is not yet attached as the bootable OCI `/` disk.

Verification recorded for this slice:

- Complete local `tests` tree: 2218 passed, 16 skipped, with one existing macOS fork deprecation warning.
- Focused OCI root/store/project-volume/state suite: 161 passed.
- Crash/fault coverage includes creating-intent recovery, claim failure after lower reservation, resources-planned crash after both resources exist, retained-claim rollback, release after root completion but lower failure, already-retained release retry, and deterministic deletion-quarantine recovery.
- Ruff lint/format, Python compile, `git diff --check`, and sdist/wheel build pass.

Scope boundary: the OCI graph and VM-specific writable root now have one crash-recoverable run-ledger preparation transaction. No libvirt domain is emitted, no host kernel/initramfs is selected, and no guest has mounted, assembled, or pivoted this graph to `/`. Gate 2 remains inactive.

### PR 4 slice 11: host boot-artifact policy and path-free KVM domain handoff

Implemented:

- OCI-root now has a separate typed libvirt contract instead of overloading the cloud-image domain shape. It uses direct x86_64 KVM kernel/initramfs boot, a writable raw ext4 root at `vda`, and ordinal-preserving read-only raw lower disks from `vdb` onward. It emits no firmware boot entry, qcow2 root, NoCloud seed CD-ROM, guest agent, or cloud activation script.
- Host kernel and initramfs selection is explicit; ambient `/boot` discovery is not allowed. The policy pins each no-follow regular file while hashing, accepts only root/current-user ownership with a single link and no group/world write bits, bounds size, validates the x86 boot-protocol `HdrS` marker and a supported initramfs header, and supports exact expected-digest revalidation.
- Disk serials are deterministic logical identities rather than physical-image prefixes. The root serial is derived from its volume UUID; every lower serial is derived from its occurrence digest. Repeated OCI occurrences may therefore share one SquashFS CAS file while retaining distinct guest identities and exact order.
- `OCIRootDomainPlan` is a canonical, digest-bound, path-free handoff. It binds the run, preparation-plan digest, durable lower lease-set, lower-graph digest, boot-artifact digests/sizes/policy, root volume UUID/generation/size/serial, ordered lower occurrence/image identities, compute shape, network, and fixed future stage-1 command line.
- Planning reloads and strictly validates the exact durable lease set and attached writable root before resolving local paths. Nested plan data is recursively immutable. Committing the plan uses the pinned existing-run mutation boundary and revalidates the exact preparation, lease members, root, boot artifacts, spec, profile, and XML before appending it to the ledger. Loading re-derives deterministic disk serials and the fixed command line before accepting the canonical plan digest and run binding.
- The existing cloud-image XML builder remains unchanged. The path-bearing resolved XML is explicitly an ephemeral preview, not a launch authority; the future libvirt define/start consumer must resolve and revalidate all paths at its own mutation boundary. OCI-root `RUN` capability and libvirt define/start are still disabled, so this contract cannot be mistaken for successful guest root assembly.

Verification recorded for this slice:

- Focused KVM/OCI-store suite: 140 passed.
- Complete local `tests` tree: 2224 passed, 16 skipped, with one existing macOS fork deprecation warning.
- Ruff lint/format, Python compile, `git diff --check`, and sdist/wheel build pass. Adversarial coverage includes wrong-platform rejection, reordered lower rejection, boot-artifact symlink/permission/digest rejection, nested-plan mutation, shared physical lower bytes with occurrence-unique serials and `shareable`, foreign root-owner rejection, and path-free ledger recovery.
- Independent code review and verifier: P0 0 / P1 0. OCI-root `RUN` remains fail-closed and existing x86/aarch64/HVF cloud XML goldens remain unchanged.

Scope boundary: this slice emits an ephemeral libvirt XML preview and durably binds only its path-free plan. It does not authorize or call libvirt, provide the stage-1 `/init`, mount or assemble the lowers, pivot the OCI tree to `/`, launch image init as PID 1, or enable foreground/`-d`. Gate 2 remains inactive.

### PR 4 slice 12: OCI process identity and guest stage-1 contract

Implemented:

- OCI image `Entrypoint`, `Cmd`, `Env`, `WorkingDir`, `User`, and `StopSignal` are now parsed into one canonical, shell-free process contract. Argument boundaries, empty arguments, newlines, and shell metacharacters remain literal. The contract rejects NULs, duplicate or malformed environment names, noncanonical users and paths, unsupported signals, `ArgsEscaped`, and bounded-input violations.
- An image with no `Entrypoint` and no `Cmd` remains valid source material but fails closed when promoted to an OCI-root boot plan. The process contract is carried through image selection, hard materialization, boot-plan reservation, run preparation, KVM domain planning, and the future guest handoff.
- Derived lower receipts now state their filesystem type explicitly. The current closed policy accepts only `squashfs`; domain and stage-1 plans therefore never infer `mount -t` from a path, filename, or host state.
- Worker responses and durable lease/lease-set records that embed the expanded receipt use v2 schemas. Existing v1 durable records have an explicit legacy decoder that supplies only the historically guaranteed `squashfs` type while preserving their original deterministic set/member identities, so restart discovery, GC retention, and release remain available after upgrade.
- `OCIStage1Plan` is a canonical, path-free input contract for the future first-party `/init`. It binds the run and boot/domain plan digests, writable ext4 root identity and generation, ordered SquashFS device serials, mount policies, reverse lowerdir order for OverlayFS, and exact image process identity.
- Stage-1 deserialization independently validates canonical UUIDs/digests, run names, disk serial uniqueness, generation, filesystem and mount policy, layer-count bound, overlay order, and a bootable process. It also requires the already validated expected domain plan and rejects any wire contract that cannot be re-derived exactly from it. Nested root and layer state is recursively immutable.
- The fixed kernel command line now includes the resource-plan digest. This is a future guest lookup/binding input only; this slice does not define a transport or claim that `/init` can retrieve the plan.
- The process contract is included in the boot intent while the lower graph retains the exact OCI source/config provenance policy from slice 10. A separately published config therefore remains a distinct retained-root identity even when its layer descriptors match; this slice does not broaden retained-root reuse semantics.

Scope boundary: this is a typed contract, not an initramfs implementation. Palimpsest still does not build or install `/init`, discover and mount guest disks, create OverlayFS, pivot/chroot into the merged OCI root, resolve named users inside that root, run or supervise image init, define/start libvirt, or enable foreground/`-d`. Gate 2 remains inactive.

### PR 4 slice 13: deterministic first-party bootstrap initramfs provenance

Implemented:

- Palimpsest now emits a tool-independent, uncompressed canonical `newc` archive entirely from Python. Entry order, inode sequence, root ownership, mode, link count, zero timestamps/device fields/checksum, hexadecimal encoding, NUL termination, four-byte zero padding, one terminal `TRAILER!!!`, and no trailing bytes are fixed by `palimpsest.initramfs.newc.v1`.
- The portable parser accepts only that closed subset. It bounds archive/member/path sizes and entry count, rejects missing parents, links/devices, absolute or noncanonical paths, duplicates/reordering, malformed/truncated headers, nonzero padding, metadata drift, missing/duplicate/non-final trailers, concatenated archives, and trailing bytes. Acceptance is confirmed by byte-for-byte canonical re-emission.
- The initial first-party `/init` is a deterministic standalone x86_64 ELF emitted by Palimpsest itself, with no compiler, libc, interpreter, dynamic section, or runtime dependency. The ELF verifier requires ELF64 little-endian `ET_EXEC`, x86_64, bounded program headers and segments, an entry in a file-backed readable/executable `PT_LOAD`, no `PT_INTERP`/`PT_DYNAMIC`, no writable+executable load, and no executable stack.
- This `/init` is intentionally a **fail-closed bootstrap**, not the root assembler: it writes a fixed diagnostic and sleeps. Its embedded stage-1 ABI records `capability=bootstrap-fail-closed`, `root_assembly=false`, and `plan_transport=unimplemented`. The artifact therefore cannot be represented as OCI-root boot readiness.
- A canonical path-free manifest binds architecture, archive format/generator, complete entry path/mode/size/digest receipts, stage-1 ABI and binary digests, static linkage, capability, and plan-transport state. Verification requires the exact first-party `/init` and ABI bytes, not merely a self-consistent attacker-supplied manifest.
- `verify_first_party_bootstrap_initramfs` reuses the existing no-follow, owner/mode/link-count, same-FD hash and metadata-stability host boot-artifact boundary, but adds a 64 MiB bound and full archive/manifest/ELF verification while the descriptor remains pinned. A fake `070701` prefix is no longer sufficient at this first-party boundary.

Scope boundary: this slice creates and verifies a real deterministic initramfs containing an executable `/init`, but that `/init` deliberately does not discover devices, mount ext4/SquashFS, assemble OverlayFS, pivot, resolve users, execute/supervise the OCI process, or signal readiness. It is not selected by the OCI-root domain planner, does not implement a per-run digest-bound plan transport, and does not authorize libvirt. Generic explicitly supplied host initramfs preview behavior remains compatible. Gate 2 remains inactive.

### PR 4 slice 14: cycle-free per-run stage-1 plan transport

Implemented:

- The digest graph is explicit and acyclic: the durable OCI resource-plan digest `B` feeds a transport-independent domain-core digest `C`; the canonical stage-1 payload binds `B`, `C`, run identity, root generation/serial, ordered lower serials and process; its raw transport artifact has digest `T`; the final domain plan digest `D` binds the complete transport receipt and cmdline. `D` is never embedded back into the transport.
- `OCIStage1Plan` v2 replaces the cyclic final-domain digest with `domain_core_digest`. Its wire still requires exact reconstruction from an expected domain plan, and the transport verifier compares against an independently constructed typed stage-1 projection rather than trusting a self-consistent payload.
- The per-run transport is a bounded deterministic raw envelope: a fixed 64-byte little-endian header binds magic, version, header size, payload length and SHA-256; canonical UTF-8 JSON follows; the artifact is zero-padded to exactly one 4 KiB boundary. Receipt fields bind schema, format, device policy, payload/artifact digest and exact sizes.
- OCI-root reserves `vdb` for the run-specific read-only, non-shareable transport disk and shifts ordered immutable lowers to `vdc..vdz`. OCI-root therefore supports at most 24 lowers while the generic cloud VM disk limit remains unchanged. Kernel cmdline fields separately bind resource/core/transport digests and the exact virtio serial; no host path is accepted by the guest contract.
- Domain planning keeps only the path-free receipt, target and serial. At the pinned run-ledger mutation boundary, commit deterministically reconstructs the artifact, atomically publishes the owner-readable sealed (`0400`) `stage1-plan.raw`, verifies no-follow regular-file ownership/mode/link/size plus same-FD structure/digest and final metadata stability, and only then commits the domain plan. A verification failure leaves the prior `resources-ready` ledger retryable; a retry replaces the uncommitted run-owned artifact with the same deterministic bytes.
- Every durable domain-plan load re-verifies the physical transport against the receipt and independently reconstructed stage-1 plan; a changed mode, inode shape, size, digest or payload is rejected. Future define/start must repeat this check at its own final handoff boundary. As with all owner-controlled state, this is fail-closed validation rather than an OS sandbox against a deliberately hostile same-UID process that can chmod or replace its own files.
- Domain-plan decoding independently recomputes `C`, the complete stage-1 payload and exact `T` receipt. Cross-run replay, altered root/lower/process/core, receipt/serial/cmdline drift, malformed header/length/hash/JSON/padding, symlink/FIFO/hardlink/writable file and same-size byte tampering fail closed.

Scope boundary: this is the **host delivery contract** only. The bootstrap `/init` still records `plan_transport=unimplemented`, does not discover or read `vdb`, and remains fail-closed. The XML remains a non-launching preview; no libvirt define/start, guest mount/pivot, PID 1 supervisor, foreground/`-d`, readiness, exec/log, or Gate 2 activation is included.

### PR 4 slice 15: portable guest stage-1 consumer contract and provenance

Implemented:

- `oci_guest_stage1` is the first-party portable reference contract for guest consumption. It parses at most 4096 bytes of ASCII, NUL-free kernel command line, treats every `palimpsest.*` key as closed-world and unique, and requires the complete resource/core/transport/transport-device/root/lower binding set. Digests, the `virtio-` namespace, the 20-hex serial policy, the 24-lower bound and uniqueness across transport, root and lowers are exact.
- Block discovery is serial-based rather than positional. The portable boundary follows canonical `/sys/class/block/vd*` links only into `/sys/devices`, then requires a single exact disk-level serial match, bounded no-follow sysfs attributes and exact sysfs `ro=1`. Missing, duplicate, writable, malformed and ambiguous candidates fail closed. Proving the resolved driver chain is virtio and binding the returned `/dev` node to the inspected sysfs identity with Linux block-device ioctls remains part of the freestanding binary/runtime boundary.
- The guest transport verifier independently checks the cmdline artifact digest, fixed little-endian envelope header, version and size policy, payload SHA-256, exact 4 KiB extent and zero padding. JSON must be UTF-8, duplicate-key-free and byte-canonical. The complete typed stage-1 schema, assembly/root/layer/process semantics and reverse lower order are revalidated before resource `B`, domain core `C`, transport `T`, root serial and ordered lower serials are cross-bound to the kernel command line.
- Initramfs manifest v2 now carries a canonical `guest-stage1-consumer.json` contract and digest. The bootstrap ABI binds that receipt while explicitly recording `embedded_consumer=false`, `plan_transport=unimplemented`, and `root_assembly=false`. The deterministic static x86_64 `/init` diagnostic likewise says the consumer is not embedded and waits permanently, so provenance cannot falsely claim that Python reference behavior executes inside the guest.

Scope boundary: this slice closes and tests the guest-side **consumer contract**, but does not yet ship a freestanding x86_64 implementation of its SHA-256, JSON and sysfs/block-node checks. The embedded `/init` therefore remains a permanent fail-closed wait and the OCI-root planner still does not select or launch it. No device is mounted, no OverlayFS or actual `/` is assembled, no pivot/process supervision/foreground/`-d` lifecycle is enabled, and Gate 2 remains inactive. The next slice must build a source-controlled reproducible freestanding binary without requiring a compiler at normal package runtime, execute it on Linux x86_64 fixtures, and bind `/dev` block identity before any mount work begins.

### PR 4 slice 16: freestanding Linux x86_64 transport consumer

Implemented:

- The first-party `/init` is now built from source-controlled `guest/stage1/init.c` with raw Linux x86_64 syscalls and no libc or system headers. It embeds SHA-256 and a bounded deterministic JSON parser matching the Python wire constants. The parser requires compact sorted-key canonical JSON, canonical UTF-8 and escapes, complete stage-1 schema/policy/process semantics, a shared 4096-decimal-digit root-generation bound, and exact `B`/`C`/`T`/root/ordered-lower bindings. Digit-prefixed run names, large valid environment names, Unicode process strings and the full numeric uid/gid ceiling remain in parity with the producer.
- The build pins the exact linux/amd64 GCC 14.3.0 Bookworm manifest digest, disables network access, uses a read-only container as the invoking UID/GID, and fixes locale/timezone/home/epoch and path mappings. A deterministic seal rejects interpreter/dynamic/WX/malformed segments and executable stack, preserves the complete program-header/load extent, removes section headers and writes the exact static ELF. Normal package/initramfs construction only reads the packaged raw asset, so neither Docker nor a compiler is a runtime dependency.
- The initramfs manifest and ABI bind source, build recipe, seal recipe, toolchain, consumer contract and ELF digests. `embedded_consumer=true`, transport capability is real, and `root_assembly=false` remains explicit. Canonical `dev`, `proc` and `sys` mountpoints are included in the `newc` archive.
- Live PID 1 creates and verifies proc/sysfs/devtmpfs pseudo-filesystems, including filesystem magic after an existing mount, then reads the six closed cmdline bindings. It requires one canonical `vd*` sysfs link under `devices`, exact serial, `virtio_blk`, `ro=1`, major/minor identity, a block node, `BLKROGET=1` and exact `BLKGETSIZE64`. It repeats descriptor/ioctl and serial/ro/dev checks after the bounded read before accepting the envelope and plan.
- The same ELF exposes `--fixture-v1 ROOT` only outside PID 1; non-PID1 invocations can never enter the live mount path. It verifies a single-link, non-writable regular transport fixture and returns stable typed exit codes. Unprivileged, capability-free Docker scratch linux/amd64 differential tests prove canonical Unicode/control data and large environment names accepted by both Python and ELF, while noncanonical JSON escapes, trailing lower separators and writable inputs are rejected by both. Two independent pinned builds reproduce the packaged bytes exactly. A required Linux x86_64 CI job and release verification pull the pinned toolchain, execute the ELF and perform the rebuild comparison. The fixture exercises the shared parser/envelope core but cannot execute the live `BLKROGET`/`BLKGETSIZE64` branch; that branch still requires qualified KVM or privileged Linux block-device evidence before any mount work is authorized.

Scope boundary: a verified plan still ends in a permanent fail-closed wait. This slice does not mount ext4/SquashFS, assemble OverlayFS, make the OCI root actual `/`, pivot, resolve users, supervise or execute the image workload, authorize libvirt, implement foreground/`-d`, or activate Gate 2.

### PR 4 slice 17: native-KVM actual PID 1 qualification gate

Implemented:

- A private, production-inert qualification harness direct-boots the exact packaged x86_64 initramfs and deterministic raw stage-1 transport with QEMU `-accel kvm -cpu host`. Qualification requires native Linux x86_64, a character `/dev/kvm` opened read/write, KVM API version 12, an executable ELF QEMU binary, a bounded Linux bzImage, and an owner/root-controlled kernel config. Initrd, ELF, devtmpfs, proc/sysfs, PCI, serial console and virtio block requirements must all be built in (`=y`); module-only kernels fail closed.
- The proof uses argv-only process creation, no monitor/display/network, QEMU sandboxing, a private `0700` workspace, exclusive sealed artifacts, bounded console capture, a process-group timeout and cleanup. The transport is verified before and after boot. Success requires the post-verification marker exactly once, no preparation/rejection marker, and a still-running QEMU after the marker, which uniquely places the packaged `/init` in its permanent live PID 1 wait after sysfs driver/serial/ro, block major/minor, `BLKROGET`, `BLKGETSIZE64`, envelope and full plan binding checks.
- A negative control reboots the same transport bytes with guest-visible write access. It must remain alive after exactly one rejection marker and must never emit the success marker. This prevents host file mode or positional `vdb` assumptions from standing in for the guest `BLKROGET` result.
- The canonical proof receipt binds kernel/config, initramfs manifest/artifact, packaged stage-1 ELF, complete transport receipt/serial, exact cmdline, QEMU version and bounded console digest. Owner-only console, receipt and writable-control console are retained as CI artifacts. Pure tests cover kernel/config metadata and built-in policy, the KVM-only/networkless command, exact marker/liveness handling, and exclusive evidence publication.
- The existing self-hosted KVM PR and release jobs now set qualified mode explicitly. An always-running PR aggregator fails when the KVM job is disabled, skipped or unsuccessful, so missing prerequisites cannot become a green merge check. Release publication remains dependent on the KVM proof job.

Qualification state: the harness and required gate are source-controlled and locally policy-tested. This macOS arm64 development host has no native x86_64 KVM, so no new qualified runtime receipt is claimed from the local run; the self-hosted native Linux/KVM job must execute and retain it.

Scope boundary: this proof exercises only the actual PID 1 transport consumer. Root and lower serials are cross-bound in the authenticated plan but their block nodes are not yet discovered or mounted. No ext4/SquashFS mount, OverlayFS, pivot, workload PID 1 supervisor, production libvirt define/start, foreground/`-d`, readiness, exec/log, or Gate 2 activation is included. OCI rootfs is still not claimed as actual `/`.

### PR 4 slice 18: authenticated pre-mount block-device set

Implemented:

- `OCIStage1Plan` v3 preserves the writable root byte size and each ordered lower's image digest, occurrence digest and byte size. Root and lower serials are independently re-derived with the same domain-separated identities used by domain planning. Root volume bounds/alignment and SquashFS store bounds/sector alignment are validated in both producer and freestanding consumer.
- The portable reference projects an ordered root/lower role contract and requires an exact, unique serial topology with exact sysfs read-only roles. It explicitly leaves block-node major/minor, `fstat`, `BLKROGET`, and `BLKGETSIZE64` to the Linux consumer boundary.
- Live PID 1 authenticates the transport first, then discovers one writable root and every ordered read-only lower by virtio serial. It requires `virtio_blk`, exact sysfs `ro`, major/minor, block node, exact ioctl size, unique guest name/major-minor/inode identity and no extra `vd*` disk. All descriptors remain open through a final sysfs/fstat/ioctl recheck before the exact pre-mount marker and permanent fail-closed wait.
- The KVM qualification harness attaches a zero-filled writable 16 MiB root and two ordered read-only lower artifacts in a deliberately permuted device order. Its v2 receipt binds the complete path-free topology digest and records `pre_mount_devices=true` while `filesystem_verified`, `content_verified`, `mount_attempted`, and `root_assembly` remain false. Pure negative receipt/contract tests cover read-only role inversion, size mismatch, missing/wrong/duplicate/extra topology; the live writable-transport rejection control remains required.
- Source/build/seal/toolchain/ELF/initramfs provenance is refreshed and the non-PID1 fixture continues to exercise only the shared regular-file envelope/parser semantics.

Scope boundary: this slice authenticates block identity only. It does not read filesystem magic or filesystem structure, hash mounted content, mount ext4/SquashFS, assemble OverlayFS, pivot, execute/supervise a workload, authorize production libvirt launch, implement foreground/`-d`, or activate Gate 2.

Qualification state: the v2 harness, packaged ELF and required CI gate are source-controlled and locally verified through parser/differential tests. This macOS arm64 host cannot execute native x86_64 KVM, the repository currently has no connected `linux/x64/kvm` runner, and no actual v2 KVM receipt was collected in this slice. The topology mutation matrix is pure contract evidence, not a substitute for native-KVM negative boots.

### PR 4 slice 19: native-KVM pre-mount rejection matrix

Implemented:

- The native-KVM harness now executes the positive permuted topology and thirteen independent negative boots from one private temporary root while reusing the same pinned QEMU, kernel and packaged initramfs. The controls are writable transport; missing, wrong-serial, read-only, smaller and larger root; missing, wrong-serial, writable, smaller and larger lower; duplicate serial; and extra disk.
- Each negative control is an exact path-free contract for backing digest, size and owner-only mode plus attachment role, read-only flag and serial. Every actual backing and the pinned boot artifacts are rechecked before and after its boot. Acceptance requires exactly one rejection marker, zero success/preparation markers and a live QEMU/PID 1 after the marker; no control can qualify the positive path.
- The v3 canonical receipt binds every control contract, console digest/size, exact marker counts and post-marker liveness attestation. Results expose a name-to-console mapping, and exclusive `0400` evidence includes every `negative-<case>.bin` together with the positive console and receipt.
- Pure tests cover all QEMU argv/topology mutations, contract and receipt missing/extra/tamper boundaries, marker counts and reserved evidence names. The required PR and release KVM jobs execute the full matrix and retain all evidence.

Scope boundary: this remains a block-identity proof. It does not inspect filesystem magic or structure, verify filesystem content, mount any disk, assemble OverlayFS, pivot, execute a workload, or enable production VM launch.

Qualification state: the v3 harness and CI gate are source-controlled and locally policy-tested. This macOS arm64 host cannot execute native x86_64 KVM, so no actual v3 positive or negative runtime receipt is claimed locally; collection requires the qualified self-hosted Linux x86_64/KVM runner.

### PR 4 slice 20: mount-free filesystem-set authentication

Implemented:

- Root-volume loading now preserves the ext4 filesystem UUID actually stored in the volume and projects it through domain planning into stage-1 plan v4. Newly created OCI-root volumes use an explicit `none,<allow-list>` feature set, fixed block/inode/group geometry, disabled lazy initialization, deterministic UUID, and zero reserved-block percentage instead of host `mke2fs.conf` defaults; the complete policy is verified on one pinned no-follow FD before publication. Retained legacy volumes keep their existing UUID and may omit `metadata_csum` when their bounded feature policy is otherwise valid. When `metadata_csum` is present, Python and PID 1 require checksum type 1 (CRC32C) and a valid primary-superblock checksum. The guest also requires primary magic/dynamic revision, exact derived label, exact block/device geometry, bounded extents-capable features, and sane group/inode/descriptor geometry. Mutable root content is deliberately not hashed or claimed verified.
- Each lower retains its OCI image digest in the plan. PID 1 ports the host SquashFS v4 superblock policy, including encoding/table/root-inode/fragment/export/padding checks, and streams SHA-256 over the complete read-only device under an aggregate 32 GiB verification budget. All already-open device descriptors and identities are rechecked after filesystem reads.
- Positive KVM proof backings are source-controlled, digest-bound outputs from real `mkfs.ext4` 1.47.0 and `mksquashfs` 4.7.5, not synthetic headers or zero disks. Proof receipt v4 distinguishes `root_filesystem_verified=true`, `root_content_verified=false`, and lower filesystem/content verification, while keeping `mount_attempted=false` and `root_assembly=false`.
- The existing thirteen block-topology negative boots remain required. Six additional same-topology KVM boots independently corrupt root magic/label/geometry or lower magic/structure/content digest. Root controls recompute a valid `metadata_csum` after the targeted change. Lower magic/structure controls use a per-control plan, transport serial and cmdline whose image digest matches the mutated whole device; only the digest-mismatch control retains the original plan digest. Each must emit exactly the filesystem rejection marker, emit no topology/success/preparation marker, and remain alive in the permanent PID 1 wait. Exclusive owner-only evidence retains every console and is written receipt-last.
- Portable tests parse the committed actual filesystems on macOS without mkfs tools. The packaged ELF's non-PID1 `--fixture-v2` mode reuses its exact filesystem parsers against regular-file fixtures and explicit exit status; it is differential parser evidence and does not replace the live block ioctl/KVM proof.

Scope boundary: no root/lower `mount(2)`, filesystem magic through a mounted view, OverlayFS, pivot, workload process, production libvirt launch, foreground/`-d` lifecycle, or Gate 2 activation is implemented here.

Qualification state: proof v4 and both negative matrices are source-controlled and portable-policy/Docker-fixture tested. This macOS arm64 host cannot run native x86_64 KVM, so it makes no actual positive or negative v4 KVM receipt claim; the required self-hosted runner must collect that evidence.

### PR 4 slice 21: authenticated staging OverlayFS assembly

Implemented:

- Stage-1 plan/protocol v5 binds the path-free `overlay-upper-work.v1` root layout. Live PID 1 uses the already authenticated open block descriptors as `/proc/self/fd/<fd>` mount sources, makes `/` recursively private, mounts the ext4 root rw and every SquashFS lower ro at deterministic `/run/palimpsest` staging paths, and preserves highest-layer-first OverlayFS ordering.
- The writable root stores persistent overlay state in `.palimpsest/upper` (0755 so the merged root remains traversable) and `.palimpsest/work` (0700). Existing non-directory or wrong-mode state is rejected without cleanup; kernel-owned workdir contents left by a prior mount are preserved and OverlayFS decides whether they are reusable. Staging and mounted directory descriptors remain open and are checked with no-follow identity, `statfs`, bounded mountinfo role/flags/order validation, and final block FD/sysfs/ioctl revalidation.
- Initramfs ABI/consumer and KVM receipt v5 distinguish successful staging assembly from making it `/`: mount and overlay flags are true while `root_is_slash`, `pivot_root`, and `workload_started` remain false. The mutable root has separate seed and post-run digests; immutable transport/lower equality remains required.

Scope boundary: the assembled OCI tree remains `/run/palimpsest/merged`. No pivot/chroot, image process execution, PID 1 supervision, production libvirt launch, foreground/`-d`, or Gate 2 activation is included.

Qualification state: portable policy, packaged ELF fixture, and proof-harness tests can run on this macOS host, but actual mount/OverlayFS evidence still requires the qualified Linux x86_64 native-KVM runner. No local KVM success is claimed.

### Local image build-to-run acceptance gates

### PR 4 slice 22: merged precedence and retained-root qualification

Implemented:

- Stage-1 plan/protocol v6 adds up to eight authenticated, bounded root-level
  assembly probes; normal production plans encode an empty list. The live PID
  1 verifies exact size and SHA-256 through the assembled tree before its
  success marker, while leaf symlinks and nested paths fail closed.
- The two KVM lowers are real `mksquashfs 4.7.5` zstd-level-3 outputs built
  from committed inputs. Their exact source bytes, builder argv/tool digest,
  compression id, image bytes, and highest-ordinal collision sentinel are
  manifest-bound. New OCI packing uses the matching explicit zstd policy.
- Proof v6 boots the same mutable root backing twice, calls `syncfs` on its
  mounted ext4 filesystem before each success marker, and binds seed,
  boot-one-post/boot-two-pre, and boot-two-post digests. It proves synchronized
  retained-root reassembly, not graceful shutdown or crash recovery. Three additional boots isolate
  post-overlay missing/size/digest probe rejection with independent roots.

Qualification state: contracts and portable tests are source-controlled. This
macOS host cannot run native x86_64 KVM, so no v6 runtime receipt is claimed.
Pivot, workload execution/supervision, production launch and Gate 2 remain
disabled.

### PR 4 slice 23: authenticated initramfs switch-root checkpoint

Implemented:

- Stage-1 plan/protocol v6 and its supervisor-required handoff remain
  unchanged. After authenticated mount, OverlayFS assembly, and proof probes,
  the packaged PID 1 closes staging directory descriptors while retaining the
  authenticated merged-root identity. It validates no-follow `/dev`, `/sys`,
  and `/proc` targets, retains the original pseudo-filesystem mount identities,
  moves those mounts into the merged tree, then moves OverlayFS onto `/` and
  enters it with `chroot(2)`.
- This is the explicit `palimpsest.stage1-root-transition.v1`
  `move-mount-chroot` contract required by an initramfs initial rootfs. It does
  not call or claim `pivot_root(2)`, and does not claim the covered initial
  rootfs was unmounted or reclaimed. A failure after any irreversible move has
  a dedicated indeterminate-root-state marker and permanent fail-closed wait;
  no rollback is attempted or implied.
- Before the success marker, PID 1 proves it is still PID 1, `/` is OverlayFS
  and matches the exact pre-transition merged inode, `/proc/self/root` matches
  `/`, devtmpfs/sysfs/proc match their retained pre-move inode and filesystem
  identities, authenticated probes still pass from `/`, and all transport,
  root, and lower block identities pass final sysfs/ioctl checks.
- Initramfs manifest v9, bootstrap ABI v9, consumer/init contract v7, and KVM
  receipt v8 bind `root_is_slash=true`, `switch_root=true`,
  `pivot_root=false`, and `workload_started=false`. Existing topology,
  filesystem, and assembly negative controls remain pre-transition and forbid
  the transition-rejection marker. The packaged deterministic static ELF and
  its exact source/binary provenance are refreshed.
- Transition targets are held by no-follow descriptors and must be exact
  root-owned 0755 empty directories on the authenticated OverlayFS both during
  preparation and immediately before their individual move. Three independent
  real-zstd-SquashFS controls replace the highest lower with a regular `dev`,
  `sys`, or `proc`; valid assembly then reaches exactly the dedicated
  transition rejection marker, and receipt v8 binds each control's distinct
  plan/transport/lower plus mutable-root seed and post-run digest.

Scope boundary: the OCI filesystem is now the PID 1 `/` only in this
qualification bootstrap. No image process is executed, no supervisor is
started, and production libvirt define/start, foreground/`-d`, readiness,
exec/log, Gate 2, and initramfs-root reclamation remain disabled.

Qualification state: portable receipt/contract and packaged-ELF tests run on
this macOS host, but the move-mount/chroot sequence itself requires the
qualified Linux x86_64 native-KVM runner. No local v8 KVM success is claimed.

### PR 4 slice 24: authenticated PID 1 supervisor checkpoint

Implemented:

- Stage-1 plan/protocol v8 replaces the pending handoff with
  `first-party-pid1-supervisor.v2` and binds the first executable process
  policy: absolute `argv[0]`, canonical numeric user, and explicit canonical
  numeric group. General OCI image parsing still preserves named identities,
  omitted groups, and relative commands, but OCI-root boot preparation rejects
  those forms until image-root passwd/group and PATH semantics exist.
- The freestanding PID 1 decodes authenticated JSON strings into a bounded
  256 KiB arena, constructs null-terminated argv and `name=value` environment
  vectors, and never inherits bootstrap environment. After root transition it
  blocks the supervised signals and creates a `signalfd` and error pipe while
  privileged. PID 1 then clears supplementary groups, permanently applies and
  verifies the workload's real/effective/saved gid and uid, and only then
  forks a new workload process group. The child inherits that exact identity,
  applies cwd, and calls `execve`. A close-on-exec pipe reports exact setup
  stage and errno; only EOF confirms that exec succeeded.
- PID 1 forwards the allow-listed signals to the workload process group,
  translating external SIGTERM to the image stop signal, and uses
  `wait4(-1)` for the main process and adopted descendants. The trusted
  terminal marker records main status, last non-main status, reap count, last
  forwarded signal, and PID 1's verified uid/gid/empty-group state before
  permanent terminal fail-closed wait.
- The qualification workload is a separately reproducible pinned static
  x86_64 ELF stored in the highest real zstd SquashFS lower. It validates the
  exact argv/environment/cwd/65534:65534/no-supplementary-group contract,
  parent PID 1, process group, and OCI-root sentinel through both `/` and
  `/proc/self/root`. Using the workload's own procfs root link avoids requiring
  ptrace permission to dereference PID 1 after dropping to UID/GID 65534; PID 1
  independently verifies its own root before launch. The helper also boundedly
  parses `/proc/1/status` and requires all four PID 1 UID/GID values to be
  65534 with no supplementary groups. It creates one descendant and
  self-stimulates PID 1 with
  SIGTERM, and preserves descendant exit 43 with `waitid(WNOWAIT)` before main
  exit 42 so PID 1 must establish both statuses itself.
- KVM receipt v10 and fixture policy/schema v5 retain positive and second-boot
  terminal consoles plus all prior controls. Three new independent writable
  roots/plans/transports/cmdlines isolate missing executable, non-executable
  target, and missing cwd; each proves root transition completed, exact
  stage/errno launch rejection occurred once, no started/terminal marker was
  emitted, and PID 1 remained alive.
- Initramfs manifest/bootstrap ABI v11 and consumer/init contract v9 bind
  `workload_started=true` only after confirmed exec. The nested root-transition
  contract remains `workload_started=false` because that checkpoint precedes
  launch.

Scope boundary at slice 24: this was a dedicated qualification guest, not
production lifecycle. Slice 26 below supersedes its whole-guest cleanup and
permanent PID 1 credential drop with a root broker and dedicated cgroup. Named
user/group resolution, omitted group semantics, PATH search, host stop/control
transport, VM poweroff and exit mapping, production libvirt define/start,
foreground/`-d`, exec/log readiness, and Gate 2 remain disabled.
Detached stop, multi-user exec, and agent lifecycle require a separate
production broker plus an owned, peer-authenticated host-to-guest lifecycle
channel.

Qualification state: all contracts, fixtures, reproducible ELFs, portable
tests, and the v10 receipt collector are source-controlled. Native Linux
x86_64 KVM qualification was collected remotely at commit `0dd47c7`: all 30
boots passed and the receipt SHA-256 is
`9e23fd9857338d16fae28e29cb344de3bf9c00911201f733bd38c7445adbc2e9`.

### PR 4 slice 25: nonce-correlated reconnectable lifecycle wire contract

Implemented:

- A production-inert, transport-neutral lifecycle protocol defines canonical
  JSON messages inside a four-byte big-endian length prefix, with the complete
  frame bounded to 64 KiB. Duplicate keys, noncanonical UTF-8 JSON, unknown or
  kind-inappropriate fields, invalid types, oversized declarations, truncated
  streams, and trailing bytes fail closed; the incremental decoder has the
  same acceptance language as one-shot decoding.
- Every message binds the canonical run UUID, domain-core digest, stage-1
  artifact digest, and a fresh 256-bit host nonce. The guest issues a canonical
  boot-generation UUID in its first `READY`; the host then requires that exact
  generation on `STOP`, `TERMINAL`, and reconnect `SNAPSHOT` messages. A
  reconnect rotates the host nonce and accepts only an exact current ready,
  stopping, or terminal snapshot. A stopping snapshot binds both the new
  `HELLO` and the still-outstanding original `STOP`, so reconnect cannot issue
  a second stop or lose terminal correlation.
- If the first `HELLO` response is lost before the host learns a boot
  generation, its fresh-nonce retry accepts either `READY` or a ready-only
  snapshot correlated to the current request. Stopping and terminal snapshots
  remain unavailable until READY authority exists. If a stop-sent reconnect
  observes terminal, its stop binding must be either the exact original STOP
  ID (delivered) or null (undelivered followed by natural exit); foreign IDs
  are rejected and the observed cause is retained for later snapshots.
- Newly created host request IDs increase across `HELLO`, reconnect, and
  `STOP`; only the explicitly permitted retransmission reuses its existing
  logical STOP ID. Guest event sequence numbers increase across the entire
  boot, including reconnects. Solicited responses bind the triggering request
  explicitly. Replayed nonces, generations, sequences, requests, cross-run
  bindings, and invalid lifecycle transitions are rejected.
- `STOP` is one stable logical operation. If a reconnect snapshot proves it
  was not delivered by reporting ready, the host may retransmit the exact same
  request ID, boot generation, and payload under the new connection nonce. A
  guest broker must deduplicate that exact retransmission idempotently and
  reject reuse of the request ID with different operation data. Once delivery
  is observed, stopping and stop-caused terminal snapshots continue to bind
  the original STOP request.
- OCI-root domain XML alone now contains one fixed named virtio-serial
  lifecycle channel and one explicit virtio-serial controller. Libvirt chooses
  the backing endpoint because no user-controlled socket path is emitted. The
  fixed channel name, protocol, and transport are bound into domain metadata,
  domain-core v3, and domain-plan v6. Existing cloud/build domain XML is
  unchanged.
- Pre-production domain-plan v4/v5 and domain-core v2 lack current lifecycle/supervisor
  binding and are intentionally invalidated rather than migrated. They must be
  rebuilt before any future launch boundary. Decode/load rejection is
  read-only and does not rewrite or delete run state or transport artifacts;
  the existing internal preparation release path remains independent of
  domain-plan decoding.

Scope boundary: no code opens the libvirt channel, sends or receives these
frames, dispatches stop, maps exit status, or reconnects in production. The
guest supervisor and `runtime_dispatch`/CLI are unchanged, OCI-root runtime
dispatch remains typed-unavailable, and Gate 2 remains inactive. A future
slice must implement a narrowly privileged guest broker, an owned libvirt
channel and cryptographic peer authentication (or an equivalent MAC-bound
channel), and the final host/libvirt handoff before this contract can become
lifecycle authority. The nonce is only a correlation and replay challenge; it
does not cryptographically authenticate either peer.

### PR 4 slice 26: root PID 1 cgroup-v2 workload broker

- PID 1 now remains root as a narrow single-workload broker. After the OCI-root
  transition it mounts and verifies cgroup v2, creates the fixed
  `/sys/fs/cgroup/palimpsest.workload` subtree, and pins its directory plus
  `cgroup.procs`, `cgroup.kill`, and `cgroup.events` file descriptors before
  forking. `CONFIG_CGROUPS=y` is a native-KVM admission requirement.
- A release pipe prevents the child from dropping credentials, executing, or
  creating descendants until root PID 1 has attached its PID through the
  pinned `cgroup.procs` descriptor and independently verified membership.
  Only the child then applies and verifies empty supplementary groups and the
  admitted numeric GID/UID; PID 1 retains verified root credentials. The proof
  additionally requires UID 65534 write-open attempts against parent and own
  `cgroup.procs` to fail with `EACCES` or `EPERM`.
- The proof workload exits its main process naturally with status 42 after
  starting one cooperative and one stubborn descendant. PID 1 sends the OCI
  stop signal to the process group, records cooperative exit 43, waits the
  grace interval, and uses pinned `cgroup.kill` to force the stubborn status
  137. It then reaps with `wait4` through `ECHILD`, proves `populated 0` and
  empty `cgroup.procs`, and removes the subtree. Cleanup uncertainty emits an
  explicit rejection and cannot publish the terminal success marker.
- Initramfs manifest v12, bootstrap ABI v12, init/consumer v10, supervisor v3,
  stage-1 plan v8, OCI-root domain plan v6, KVM receipt v11, and filesystem
  fixture policy/schema v6 bind the new gate, credentials, cgroup path,
  deterministic statuses, cleanup proof, reproducible ELFs, and four rebuilt
  SquashFS fixtures. Native v11 receipt collection remains pending.

Scope boundary: this broker qualifies one workload only. The committed
virtio-serial lifecycle protocol/channel remains unopened and production-inert;
`runtime_dispatch` and CLI launch remain unavailable. Detached stop, exec, and
agent lifecycle still require a separate production privileged broker plus an
  owned, authenticated host lifecycle channel. The cgroup is a containment and
  cleanup boundary, not a security sandbox against an admitted hostile root or
  capability-bearing workload.

### PR 4 slice 27A: native single-connection lifecycle qualification

- The qualification PID 1 now opens exactly one uniquely discovered
  `org.palimpsest.oci.lifecycle.0` virtio port, verifies its sysfs and character
  device identity, and runs the bounded canonical v1 `HELLO → READY → STOP →
  TERMINAL` exchange. Boot generation is a kernel-random RFC 4122 v4 UUID.
  The implementation is fail-closed for malformed, stale-binding, duplicate,
  truncated, or oversized input, but the native receipt currently exercises
  only the valid exchange; its exact `negative_input_proven=false` field keeps
  the corresponding guest-C negative runtime matrix explicitly unproven.
- The native proof uses a private owner-bound QEMU Unix socket with `wait=off`,
  virtio-serial, and virtio RNG. The host performs partial nonblocking I/O with
  the shared decoder/session, waits for both READY and the proof-only
  signal-armed scheduling marker before STOP, and requires TERMINAL before the
  parent console marker while QEMU/PID 1 remain alive. Receipt v12 retains
  path-free frame digests/sizes/directions plus nonce, boot generation,
  request/reply, and sequence evidence for two distinct positive boots.
- Stage-1 plan/protocol v9, handoff v3, init/consumer v11, initramfs manifest
  and ABI v13, supervisor v4, lifecycle broker v1, fixture policy/schema v7,
  domain plan v7, and receipt v12 bind this checkpoint. Domain plan v6 is a
  pre-production invalidated input and is rejected read-only with a rebuild
  instruction; domain core remains v3.

Scope boundary: this proves one native connection only. `reconnect_proven` is
false; SNAPSHOT and STOP retransmission are not guest-runtime claims. The
production libvirt/runtime dispatch and CLI remain disabled, and nonce
correlation is not cryptographic peer authentication.

### PR 4 slice 27B: reconnect and lifecycle-negative qualification

- The qualification broker retains boot-wide lifecycle state across bounded
  virtio-serial peer boundaries. A six-connection composite loses the initial
  READY, proves ready/stopping/terminal SNAPSHOT recovery, discards a partial
  STOP only at EOF, retries that complete logical STOP, and accepts one exact
  already-committed duplicate without dispatching the signal twice. Host
  request allocation remains monotonic while an outstanding STOP retains its
  earlier ID across a later reconnect HELLO.
- The proof host opens the next connection only after an exact admitted-peer
  EOF marker, and closes the partial STOP connection only after the guest has
  reported its exact frame-minus-one buffered state. These stdout markers are
  proof-only coordination with the controlled workload, not production
  authority. Production reconnect requires a privileged in-band boundary ACK
  or equivalent barrier; arbitrary rapid reconnect remains explicitly
  unqualified.
- Input parsing has a five-second connection-local partial-frame deadline even
  when no further readiness event arrives. Outbound partial/write loss commits
  its sequence attempt but preserves the old input parser, HELLO, and nonce
  until read EOF. Reconnect delay grows 10/20/40/80/100 ms, and bounded nonce
  and request ledgers cover all 16 admitted connections plus one STOP.
- Linux host connections revalidate the pinned Unix-socket dev/inode/uid/type
  and require `SO_PEERCRED` to match the spawned QEMU PID and current UID.
  Ten independent guest boots cover two channel-discovery and eight exact wire
  negatives. Their raw consoles, actual offending bytes, mutable-root
  seed/post digests, and immutable backing checks are receipt-bound. A separate
  41st QEMU invocation proves duplicate lifecycle names fail before guest boot;
  the 40 guest boots remain distinct from that preboot rejection.
- Natural terminal cause is frozen if the main process wins a concurrent STOP,
  including an empty process group (`ESRCH`). A canonical late STOP is drained
  at most once on the already-active connection without changing null
  `reply_to`; EOF, reconnect, malformed input, or a second STOP closes that
  exception. This code path is not an additional native proof boot, so receipt
  v14 says `natural_terminal_proven=false`.
- Stage-1 plan/protocol v10, handoff v4, init/consumer v12, initramfs
  manifest/ABI v14, supervisor v5, lifecycle broker v2, fixture policy/schema
  v8, domain plan v8, and KVM receipt v14 bind this checkpoint.

Scope boundary: the production libvirt/runtime dispatch and CLI still do not
own or open this lifecycle channel, and Gate 2 remains inactive. The nonce is
correlation/replay state, not cryptographic peer authentication. Native v14
evidence must be collected on the qualified Linux x86_64 KVM runner; local
policy tests and reproducible binaries are not a substitute.

### PR 4 slice 28B: capabilityless UID 0 lifecycle-authority boundary

- Numeric UID/GID `0:0` is still an admitted OCI process identity, but it is
  now deliberately capabilityless. Before the workload is released, the child
  closes its inherited lifecycle descriptor, creates a private mount
  namespace, replaces `/dev` with a private tmpfs containing exactly
  `null`, `zero`, `full`, `random`, `urandom`, and `tty`, masks the virtio-port
  sysfs control path, and makes `/proc`, `/sys`, plus `/sys/fs/cgroup`
  read-only while preserving ordinary proc reads.
- Capability bounding, ambient, permitted, effective, and inheritable sets are
  emptied; securebits are locked and `no_new_privs` is set and verified. A
  narrow post-setup seccomp filter denies namespace, mount, device-node,
  introspection, kernel-loading, x32 ABI entry, and newer mount authority syscalls while
  allowing ordinary fork/clone. `clone3` reports `ENOSYS` so libc may fall back
  to an ordinary clone.
- PID 1 sets and verifies `PR_SET_DUMPABLE=0` before fork. A dedicated exact
  child-ready/error handshake makes isolation parent-verifiable before PID 1
  attaches the child to its cgroup and sends the existing release byte. The
  isolation marker therefore precedes `WORKLOAD_STARTED` and lifecycle READY;
  any missing, malformed, or out-of-order acknowledgement fails closed.
- The proof workload verifies `Cap*=0`, `NoNewPrivs=1`, `Seccomp=2`, exact and
  functional safe devices, ordinary fork, denied mount/mknod/unshare/control
  writes, hidden lifecycle device discovery, and inaccessible PID 1 fd/memory.
  The native matrix adds one explicit UID 0 positive boot without removing any
  existing case: 41 guest boots and one duplicate-name preboot rejection, 42
  QEMU invocations total. Workload stdout remains supplementary; PID 1 terminal
  state and the lifecycle exchange remain qualification authority.
- Stage-1 plan/protocol v11, handoff v5, process policy v2, init/consumer v13,
  initramfs manifest/ABI v15, supervisor v6, lifecycle broker v2, fixture
  policy/schema v9, domain plan v10 (domain core v4), and KVM receipt v15 bind
  this checkpoint.

Scope boundary: this is a pre-MAC lifecycle-authority confidentiality and
integrity boundary, not a complete hostile-root availability sandbox. It does
not add a PID or user namespace, so an admitted UID 0 process may still consume
resources or signal peers allowed by the shared PID namespace. Production
create/start/run/`-d` remains disabled, and native v15 evidence must be
collected on the qualified Linux x86_64 KVM runner.

### PR 4 slice 29A: authenticated lifecycle v2 host candidate

- A separate, production-inert `palimpsest.oci-lifecycle-control.v2` host
  candidate now defines the closed-world canonical envelope, incremental frame
  codec, per-boot key identifier, direction/carrier-separated HKDF-HMAC-SHA256,
  and constant-time authentication checks. The only unsigned message is the
  initial `HELLO`; `BOOTSTRAP`, `KEY_ACK`, `RECONNECT`, `READY`, `SNAPSHOT`,
  `STOP`, and `TERMINAL` use the private channel carrier, while signed
  `BOUNDARY_ACK` is console-only.
- The host state machine separates attempted host wire sequence from the last
  sequence proven accepted by PID 1. A partial STOP therefore recovers through
  an exact ready ACK/SNAPSHOT and retries the same logical STOP ID with a new
  wire sequence. If natural termination races a STOP, the host retains the old
  accepted wire and attempted STOP wire as bounded candidates until a signed
  ACK commits one; a terminal carrying the STOP ID requires the STOP wire
  exactly. A partial RECONNECT similarly distinguishes an unaccepted request
  from an accepted request with a lost SNAPSHOT and uses the committed identity
  to select same-logical or new-logical recovery under fresh wire data.
- `BOUNDARY_ACK` binds boot/run/artifact identity, previous accepted connection
  opener and nonce, lifecycle-state digest, host/guest sequences, and the exact
  parser discard state. Only empty, a one-to-three-byte partial header, or an
  incomplete bounded payload are reachable; complete or mixed parser states
  fail closed. Receipt projections retain only canonical body/envelope digests,
  key ID, bindings, counters, carrier, direction, and an internally derived
  verification result. Signed projections require and verify the boot key;
  callers cannot assert success. They exclude the raw boot key and MAC tag.
- The v2 candidate is documented and unit-qualified independently of active
  v1. It does not change the guest binary, domain/plan versions, deterministic
  stage-1 assets, the retained native v15 receipt, or production dispatch.

Scope boundary: slice 29A is not lifecycle activation. The active guest and
native KVM proof still use v1, and production create/start/run/`-d` remains
disabled. Slice 29B must implement PID 1 key custody and post-fork wipe proof,
the guest v2 codec/MAC/state machine and console boundary path, regenerated
assets, negative native KVM cases, and the complete fail-closed version cascade
as one atomic activation gate.

### PR 4 slice 29B: authenticated lifecycle v2 guest/KVM activation

- The first-party guest PID 1 now owns the complete v2 bootstrap and lifecycle
  state machine. It creates the per-boot key only after the workload child has
  completed isolation and PID 1 has attached it to the workload cgroup, proves
  the child's copy-on-write key storage remains zero, and does not release the
  workload until signed `KEY_ACK` has been verified.
- Channel messages use the exact `{body,mac}` envelope and separated
  HKDF-HMAC-SHA256 keys. Console `BOUNDARY_ACK` messages are authenticated and
  bind parser discard state, the connection transition, accepted/attempted wire
  sequences, and the exact lifecycle projection. Only read EOF creates a
  boundary; HUP is advisory.
- The native proof covers a lost initial READY, an accepted RECONNECT with a
  lost SNAPSHOT, a partial STOP, same-logical STOP retry on a fresh wire,
  same-ID deduplication without a second signal, and terminal reconnect.
  Authenticated negative controls cover stale, replayed, cross-binding, and
  conflicting messages. Evidence retains only verified safe projections and
  recursively excludes raw boot keys and MAC tags.
- Stage-1 plan/protocol v12, handoff v6, init/consumer v14, initramfs
  manifest/ABI v16, supervisor v7, lifecycle broker v3, workload isolation v2,
  domain plan v11/core v5, and KVM receipt v16 bind this checkpoint. The sealed
  guest asset is rebuilt reproducibly from the pinned toolchain.

Scope boundary: slice 29B activates v2 only in the pre-production guest/domain
and native-KVM qualification path. Production create/start/run/`-d`, libvirt
channel ownership, runtime STOP dispatch, and Gate 2 remain disabled.

### PR 4 slice 29C: image-root identity and shell-free PATH execution

- Stage-1 plan/protocol v13 and handoff v7 admit named users/groups, omitted
  primary groups, and non-absolute argv. The process contract inserts the fixed
  container default PATH when the image omits it; no host bootstrap environment
  is inherited.
- Guest PID 1 resolves accounts only from bounded, root-owned, not
  group/other-writable,
  no-follow regular `/etc/passwd` and `/etc/group` files after the OCI root is
  `/`. Named records must be unique. Omitted group uses the passwd primary GID;
  a numeric UID absent from passwd retains Docker's GID 0 fallback. Explicit
  numeric groups bypass the group database.
- Supplementary groups remain deliberately empty under the existing isolation
  contract. This is a fail-closed security subset and does not claim Docker's
  supplementary membership expansion.
- After credential drop and cwd application, the child performs direct
  `execve` for argv containing `/`, otherwise ordered PATH candidate attempts.
  No shell is invoked. ENOENT/ENOTDIR continue, EACCES is retained, and other
  errors fail immediately; an empty PATH element explicitly names the workload
  cwd.
- The deterministic native proof uses a named UID with omitted group and a
  relative argv whose first PATH candidate misses. The UID 0 isolation proof
  separately resolves explicit named user/group. Missing-name controls fail at
  the exact pre-fork account-resolution stage. Fixture policy/schema v10,
  init/consumer v15, initramfs manifest/ABI v17, supervisor v8, domain
  plan/core v12/v6, and KVM receipt v17 bind the checkpoint.

Scope boundary: production create/start/run/`-d`, runtime STOP dispatch, agent
and exec-session ownership, supplementary group expansion, and Gate 2 remain
disabled.

### PR 4 slice 29D: qualified agent and exec-session cgroup ownership

- PID 1 now owns an empty `/palimpsest.agent` cgroup parent and a primary
  `/palimpsest.agent/exec-00000001` leaf. Both directories and each node's
  `cgroup.procs`, `cgroup.kill`, and `cgroup.events` are no-follow pinned and
  verified as cgroup v2, root-owned exact-mode objects. Existing names are
  rejected with no adoption. PID 1 remains at `0::/`, the parent has no direct
  processes, and the admitted workload is attached only to the leaf.
- Exec-session IDs are guest-internal monotonic u32 values, rendered with at
  least eight decimal digits without aliasing the nine- and ten-digit range.
  This qualification permits exactly one active session (`id=1`) and records
  `parallel_exec_sessions_proven=false`; it does not expose a runtime exec API.
- The attach gate verifies exact `/proc/PID/cgroup` membership, leaf
  `populated 1`, recursive parent `populated 1`, empty parent `cgroup.procs`,
  and PID 1 outside the hierarchy before releasing the child. The proof
  workload independently denies write-open of root, parent, and leaf
  `cgroup.procs` through its private read-only cgroup view.
- Cleanup signals only the pinned leaf `cgroup.kill`, reaps through `ECHILD`,
  proves the leaf empty and removes it, then proves the parent recursively
  empty with no direct processes and removes it. The terminal marker binds
  both zero-population results and both removals; any uncertainty disables
  terminal success.
- Stage-1 plan/protocol v14, handoff v8, workload isolation v3,
  init/consumer v16, initramfs manifest/ABI v18, supervisor v9, domain
  plan/core v13/v7, fixture policy/schema v11, and KVM proof/receipt v18 bind
  this checkpoint. Lifecycle protocol v2, lifecycle broker v3, and process
  policy v3 are unchanged. A checked-in fixture rebuild tool binds the updated
  proof ELF into all four affected SquashFS images reproducibly; `-no-xattrs`
  excludes host metadata such as macOS provenance attributes from those bytes.

Scope boundary: production create/start/run/`-d`, host runtime STOP, runtime
exec/log readiness, parallel exec sessions, supplementary group expansion,
and Gate 2 remain disabled.

### PR 4 slice 30A: exact-domain libvirt lifecycle handoff and terminal root quiesce

- A private, production-inert host launch boundary now allocates a nonblocking
  libvirt stream, durably records an `activating` intent before `create()`,
  captures the resulting domain ID, and opens only the committed named
  virtio-serial lifecycle channel without `VIR_DOMAIN_CHANNEL_FORCE`.
- The host drives the authenticated v2 `HELLO → BOOTSTRAP → KEY_ACK → READY →
  TERMINAL` exchange. READY and terminal receipts are secret-free, and any
  already-observed trailing or truncated wire after TERMINAL fails closed.
- Every state transition revalidates the exact run, plan digest, libvirt URI,
  UUID, active domain ID, ownership marker, and full persistent inactive XML.
  Cleanup destroys or undefines only that exact boot instance; restart,
  identity drift, or ambiguous creation leaves the domain untouched and records
  cleanup as not attempted or required.
- Definition ledger v2 binds the capability-validated canonical libvirt machine
  alias and the complete post-define semantic projection digest. Bounded
  libvirt defaults (`currentMemory`, hard-disk boot selection, and redundant
  same-namespace lifecycle metadata elision, disabled audio, and the q35 iTCO
  reset watchdog) normalize only when their exact committed meaning is
  unchanged; all later operations require the recorded projection digest.
- Guest PID 1 publishes TERMINAL only after workload and cgroup cleanup, stable
  no-follow identity checks for OverlayFS `/` and `/proc/self/root`, successful
  `syncfs`, and successful descriptor closes. KVM proof/receipt v19 binds the
  exact quiesce marker ordering across all positive consoles and forbids it in
  every negative console.
- An opt-in `qemu:///system` qualification now exercises the real private
  prepare, committed plan, collision rejection, define, launch, natural exit,
  retained ext4 mutation, exact domain cleanup, and lease/volume release path.
  Its cleanup independently requires matching name/UUID, owner marker, domain
  ID, and inactive XML before any destructive libvirt call.
- Stage-1 plan/protocol v15, handoff v9, init/consumer v17, initramfs
  manifest/ABI v19, supervisor v10, domain plan/core v14/v8, and KVM
  proof/receipt v19 bind this checkpoint. Lifecycle protocol v2, broker v3,
  workload isolation v3, fixture policy/schema v11, and process policy v3 are
  unchanged.

Scope boundary: this launch path remains a synchronous private qualification
surface. Public create/start/run/`-d`, a restart-safe monitor, runtime STOP,
exec/log readiness, and Gate 2 remain disabled.

### PR 4 slice 30E: restart-bound monitor ownership foundation

- A new production-inert OCI-root monitor ownership journal binds the exact
  durable run owner, host UID, plan digest, definition-projection digest,
  stage-1 artifact digest, libvirt URI, domain UUID and active domain ID, guest
  boot-attempt UUID, and writer process incarnation. The process incarnation is
  the Linux host boot UUID plus PID plus `/proc` start ticks, so a recycled PID
  or host reboot cannot impersonate the recorded writer.
- The owner-only per-run lock file is the live single-writer lease. The exact,
  canonical, secret-free JSON journal is the restart intent. Each transition is
  published through an owner-only temporary inode, file `fsync`, atomic
  link/replace, and directory `fsync`. Failure before replacement preserves the
  preceding journal; failure after replacement never reports success and
  poisons the still-lock-holding handle because either old or new state may be
  durable after a crash. Fork children immediately close inherited lock and
  directory descriptors and poison their copied handles, so they cannot retain
  or impersonate the parent's monitor authority.
- Explicit adoption first acquires the OS lease and accepts only the same exact
  launch binding whose prior process is provably stale. A live writer, unknown
  process state, malformed journal, terminal state, or binding drift fails
  closed. Adoption commits only `adopting` recovery ownership.
- Lifecycle v2's per-boot authentication key remains memory-only. Consequently
  a replacement monitor cannot authenticate a running guest merely from this
  journal: `adopting` cannot transition to `running` and may only commit
  `control-lost` for a future exact cleanup/reconciliation path. This is an
  ownership foundation, not recovery execution, reconnect, STOP, or a daemon.

Scope boundary: no existing runtime, supervisor, dispatcher, CLI, or libvirt
launch module imports this foundation. Public create/start/run/`-d`, monitor
process spawning, authenticated reconnect/STOP, adoption cleanup, logs/exec,
and Gate 2 remain disabled.

### PR 4 slice 30F: fresh-exec and reciprocal IPC foundation

- A second production-inert module defines the process/IPC boundary that must
  precede any future monitor-owned libvirt connection. It permits spawning only
  on Linux, while single-threaded and before the Python libvirt module is
  loaded, and uses a real `python -m` exec with `close_fds=True` plus an exact
  two-FD `pass_fds` inventory. No environment secrets, filesystem path, boot
  key, MAC, or error representation crosses or is stored by this boundary.
- The immutable pre-activation binding contains only the run identity, owner
  UID, plan/stage-1 digests, an explicitly expected pre-define projection
  digest, preassigned domain and boot-attempt UUIDs, lifecycle protocol, and
  fixed libvirt URI. It deliberately has no active domain ID. A canonical
  digest of that fixed schema, a fresh generation, and a transient nonce bind
  the two-phase `PREPARED` then `COMMIT` spawn receipts.
  The canonical, serializable endpoint receipt additionally binds the child
  host-boot/PID/start-tick incarnation and exact socket device/inode. This
  slice returns it in memory and does not publish it durably.
- The child has no domain, libvirt, lifecycle, or `MonitorLease` import and no
  mutation command. It first creates a filesystem AF_UNIX socket inside the
  caller-held `0700` run directory through a short `/proc/self/fd` address,
  fixes it to `0600`, and pins its inode. Filesystem namespace plus DAC and
  inode revalidation were chosen over an abstract socket so stale endpoints
  and pathname replacement are observable and fail closed. The pathname,
  generation, and nonce are correlation values, not authentication secrets.
- Parent and fresh-exec child authenticate each other through Linux
  `SO_PEERCRED` UID/PID plus host boot UUID and `/proc` start ticks. Before
  COMMIT, config-channel closure, parent disappearance, malformed input, or a
  bounded timeout removes only the exact child socket and exits. Committed IPC
  frames are canonical and size bounded; the only semantic commands are
  `DESCRIBE`, `PING`, and monitor-process `SHUTDOWN`. A live same-owner caller
  explicitly handed the serialized endpoint can reconnect after exact peer and
  socket validation; daemon restart discovery is not claimed.

Scope boundary: 30F does not use the 30E active-domain lease and does not
launch, define, start, stop, or inspect a VM. `COMMIT` only moves the child into
its in-memory serving state; it is not durable receipt publication, guest
readiness, or durable run state. The active domain ID cannot safely be known
before libvirt define/create, so the next integration
must let this already-execed child own a new pre-activation journal and perform
libvirt event registration plus define/create itself before atomically
publishing the active-domain binding. It must also atomically persist and
directory-fsync the endpoint receipt before COMMIT; without that publication,
a parent crash after the in-memory COMMIT can leave a live but undiscoverable
IPC child. The next schema must also distinguish the preactivation expected
`expected_definition_projection_digest` carried here from the canonical actual
projection digest, which only the child can derive after its own libvirt
define/`XMLDesc` normalization. The child must require that exact match before
promoting the claim to the active binding; 30F does not perform or claim that
promotion. Public create/start/run/`-d`, STOP, readiness, restart recovery, and
Gate 2 remain disabled.

### PR 4 slice 30G: child-owned durable preactivation claim

- The fresh-exec child, rather than its launcher, now owns the per-run monitor
  lock for its entire lifetime. Preactivation journal v2 deliberately reuses
  the exact 30E `oci-monitor-owner-v1.lock` and
  `oci-monitor-owner-v1.json` pathnames, so a v1 active-domain owner and a v2
  preactivation owner cannot coexist. The v1 decoder and transition contract
  remain unchanged.
- Journal v2 binds the exact preactivation binding, generation, child
  host-boot/PID/start-tick incarnation, generation-derived socket name,
  revision, and a SHA-256 digest of the transient nonce. `active_binding` is
  fixed to null; raw nonce, lifecycle key, MAC, filesystem path, and error
  representation are forbidden. The child durably publishes `claiming`
  before binding its socket, `prepared` with the exact socket device/inode
  before PREPARED, and `committed` before COMMITTED. Each publication uses an
  owner-only temporary inode, file fsync, atomic link/replace, directory fsync,
  and exact canonical reread.
- The parent exact-rereads the descriptor-pinned `prepared` revision before
  sending COMMIT and the next exact `committed` revision before returning a
  handle. Once COMMIT was sent, launcher failure never kills the child merely
  because its acknowledgement path failed; the durable record is the recovery
  authority. A restart caller supplies the trusted immutable binding, derives
  generation from the journal, then requires live process incarnation, exact
  socket inode, reciprocal peer credentials, DESCRIBE, and an unchanged
  journal before discovery succeeds.
- Reconciliation mutates nothing for live or unknown writers. A provably stale
  writer is taken over only after acquiring the same lock and CAS-rereading the
  same journal. A recorded socket inode is opened with `O_PATH`, moved to a
  generation-and-inode-derived quarantine with atomic no-clobber rename,
  revalidated, unlinked, and directory-fsynced before `abandoned`; a restart
  can finish an interrupted quarantine. Replacement or cleanup ambiguity is
  preserved as `control-lost`. A stale `claiming` record has no durable inode:
  absence is safe, but any pathname is preserved and becomes `control-lost`.
  Graceful shutdown publishes `aborting`, removes and fsyncs the exact socket,
  then publishes `abandoned`. An exact abandoned record with an absent old
  socket can be rearmed as a new generation under the shared lock without
  resetting revision history.

Scope boundary: 30G is still production-inert. It does not import from the
runtime, dispatcher, CLI, libvirt launch, or active `MonitorLease` integration
paths and does not define/create/start/stop a VM. Public create/start/run/`-d`,
guest readiness, lifecycle STOP, active-binding promotion, and Gate 2 remain
disabled.

### PR 4 slice 30H: verified definition-to-launch binding

The private runtime now prepares a monitor binding from an already-defined,
verified inactive domain. Under the run lock it re-resolves the committed plan,
checks the durable definition and live inactive XML, and captures the actual
normalized projection digest, domain UUID, run identity, stage-1 digest, URI,
owner UID, and a caller-selected canonical boot-attempt UUID.

The returned binding is a snapshot, not proof of monitor journal ownership.
Bound launch compares it with freshly resolved state before stream allocation
and again before activation. A mismatch cannot create a domain or publish a
starting intent. The lifecycle session uses the selected boot attempt through
READY and TERMINAL. Existing unbound synchronous qualification remains
available, and the real libvirt qualification exercises the bound path.

Here `expected_definition_projection_digest` means the actual normalized
digest captured after definition. It is not a prediction from authored XML:
libvirt can resolve machine aliases and insert strictly validated defaults.
A future child-owned definition protocol must distinguish pre-definition
intent from this post-definition observation.

Scope: this is the identity bridge needed before child execution integration.
It does not yet transfer state/store/boot authority to the child, promote the
owner journal to an active binding, or multiplex lifecycle and IPC events.
Public run/`-d`, authenticated runtime STOP, and Gate 2 remain disabled.

### PR 4 slice 30I: active journal promotion in the private launch

The bound synchronous launch can now retain the same v2 journal lease through
`committed → activating → active → ready → terminal`. Activation intent is
durable before domain creation; the active binding adds only the exact verified
domain ID to the preactivation identity. No release/reacquire of the owner lock
or conversion to the independent v1 lease occurs.

The journal lives in `monitor-private`, a host-owner-only `0700` directory
under the pinned run. Launch compares its descriptor identity with the lease
directory before stream allocation and at activation/lifecycle mutation
boundaries. It is distinct from the guest-accessible lifecycle socket directory.
A same-binding journal in an unrelated directory cannot authorize launch.

Activation evidence is never eligible for inert-monitor stale cleanup or
abandonment. Journal authority loss blocks destructive domain cleanup and
preserves cleanup-required evidence. Failure to publish terminal ownership
after a durable exited run does not rewrite that run as a failed launch.
The live libvirt qualification uses a real held journal through TERMINAL,
including the selected boot attempt and verified active domain ID.

Scope: the qualification process still owns this synchronous launch. The
fresh-exec IPC child remains inert: trusted root/store/boot FD transfer,
child-owned libvirt execution and lifecycle/IPC multiplexing are still next.
Public run/`-d`, authenticated VM STOP, and Gate 2 remain disabled.

### PR 4 slice 30J: fresh-exec child-owned private launch

An optional explicit launch authority now pins caller-selected state/store/run
directories and read-only boot artifacts across exec. The private bootstrap
frame contains a strict profile, resource identities, timeouts, and inherited
FD numbers; paths and descriptors are never inferred from a journal or exposed
in IPC replies. The child revalidates paths, descriptor identity, metadata,
boot digests and store identity before opening libvirt. An authority guard is
also checked at subsequent launch and cleanup mutation boundaries.

The parent verifies durable COMMITTED before sending a separate authenticated
activation fence. Only that fence starts the non-daemon worker, which owns the
libvirt connection, event driver, stream and lifecycle key. The IPC main thread
continues PING/DESCRIBE service. A per-lease reentrant mutex protects journal
publication and observation; slow response IO does not hold that mutex. Main
thread failure cannot release the lease or resource FDs while the worker can
still mutate the VM.

Discovery accepts only identity-preserving forward activation revisions.
SHUTDOWN remains forbidden during pending/active execution; after successful
TERMINAL and worker completion it retires only the IPC transport, preserving the
terminal journal instead of marking it abandoned. This is not guest STOP.

The live qualification adds a clean launcher that exits after returning the
child endpoint. A test-only create barrier lets the observer prove that the
child owns an active VM and answers IPC without its launcher. The test then
requires READY/TERMINAL, the exact child writer and active identity, and terminal
transport retirement. Its existing filesystem/DAC qualification adapters run
inside the child too. The metadata adapter requires the exact broker ACL and
adds two explicit read grants (QEMU and the original owner) only to boot copies.
It permits boot-copy DAC ownership changes only after matching the active/inactive
domain instance and rehashing the held boot file. Production permission policy
remains strict; the host's boot-file relabel behavior is not silently trusted.

Scope: the domain is still defined and its normalized binding prepared before
the clean launch process. Public run/`-d`, authenticated VM STOP, production
filesystem access provisioning, and Gate 2 remain disabled. The next runtime
step is bounded guest STOP delivery through the live worker's memory-only key,
followed by the public foreground/detached adapter and end-to-end build/run gate.

### PR 4 slice 30K: authenticated guest STOP in the live worker

The private IPC STOP operation submits one semantic SIGTERM request to a bounded,
child-local control slot. Only the lifecycle worker holds the v2 session and
boot key, constructs the authenticated STOP, and writes the libvirt stream.
Admission requires durable READY. Repeated authenticated requests coalesce;
they neither send another STOP nor renew the fixed 30-second admission deadline.
An acceptance reply
means queued, not delivered or terminated. A lost IPC response leaves the outcome
unknown and may be retried against the same live monitor.

Before each STOP stream write, including partial-write/backpressure retries,
the worker revalidates the held authority, journal, durable handoff, and exact
active domain instance. Already-buffered guest input takes priority, allowing
natural authenticated TERMINAL to win without a fabricated STOP cause. The
existing authenticated terminal receipt determines the process exit result;
only the durable exited ledger and terminal journal authorize a terminal reply.

Failure after accepting STOP preserves cleanup-required/control-lost evidence.
Timeout, EOF, and ambiguous writes do not escalate to domain.destroy(). The
existing exact-domain teardown after an authenticated TERMINAL is unchanged.
No key or queued request is reconstructed from a dead monitor's journal.

The live qualification retains both natural-exit variants and adds child-owned
STOP against the existing signal-aware workload proof. After the launcher exits,
the test waits for signal readiness, repeats STOP while checking PING, and
requires exactly one signed STOP, a matching TERMINAL reply, exit 42, and the
guest's single stop-observed marker before exact terminal cleanup.

Scope: this is private live-worker control, not public stop/run/`-d` dispatch.
Reconnect, stale-owner recovery/cleanup, production filesystem access brokering,
and local build-to-run Gate 2 remain unimplemented or disabled.

### PR 4 slice 30L: inactive-only stale monitor domain cleanup

Private recovery accepts independently selected roots, the expected immutable
monitor binding, and an explicit libvirt connection. It never derives paths
from the journal or reopens boot images, and has no create/start/destroy path.
The original monitor must be provably stale, not live or unknown. Recovery
holds the existing per-monitor lock and the pinned run lock, and continually
revalidates the original journal bytes, writer identity, run/definition/handoff
binding, and connection URI. It does not adopt or rewrite the original journal.

Only captured active bindings are eligible. An exact persistent domain must
match both name and UUID, its owner marker and stored inactive XML projection,
and be inactive with ID -1 immediately before undefine. The cleanup never
signals an active VM. Libvirt has no atomic inactive-only undefine operation:
these are last-observed-inactive checks under Palimpsest's locks, not exclusion
of external administrator races. Any ambiguous result retains all resources.

A separate `oci_monitor_inactive_cleanup` run-state record commits an intent
before undefine and completion only after both name and UUID are absent. An
initially absent domain without a matching prior intent is rejected. Retrying
an interrupted intent revalidates the same stale authority; a completed receipt
only rechecks absence and never authorizes deletion of a reappeared domain.
Existing status, process-exit/handoff records, journal, socket, volumes and source
artifacts are preserved. Domain absence is not a synthetic process-exit result.

The live qualification adds a child-owned natural-exit case that retires only
the completed test monitor, preserving its stale socket/journal. The private
cleanup must then remove the exact inactive definition, preserve its evidence,
and return the same completed receipt on replay before the test restores DAC
access. After these preservation assertions, fixture-only exact socket cleanup
allows normal temporary-tree teardown; the production API never removes it.
Existing natural-exit and authenticated STOP variants remain intact.

Scope: active-domain force cleanup, uncaptured activation recovery, socket and
disk/volume reclamation, production filesystem access provisioning, public
stop/run/`-d`, and Gate 2 remain gated. Running control cannot be recovered from
a dead monitor's stored journal or receipts.

### PR 4 slice 30M: guarded retained-root detachment and reuse

After completed inactive-definition cleanup, an explicit private retention API
detaches only a root originally prepared with `retention_policy=retain`. It
requires the same stale monitor authority, original journal, completed 30L
receipt, and continued absence of the old domain by both name and UUID. It
does not convert a delete-policy root into a reusable retained root.

The old preparation transaction, committed domain plan, exact volume owner and
generation, and durable lower lease set must agree. A separate retention
intent/completion record preserves the rest of the old run ledger. Volume
record and backing inode checks occur under the volume lock; the retained
transition clears the owner and advances the generation once. Interrupted
retention resumes only against the same expected backing inode and transition.
A completed receipt is historical evidence: replay must not detach or modify a
disk already claimed by a newer VM.

The root disk is an OverlayFS writable upper, not a self-contained copy of the
image. The old run ledger and original lower leases therefore remain pinned
after retention. Reuse prepares a new run with the same lower graph and acquires
that run's own lower leases before exclusively claiming the retained root.
Retirement or transfer of the old pins requires a later explicit ownership
handoff; this slice does not remove the old run or release its leases.

Live qualification extends the stale-monitor case with retained-root detachment
and a second real VM boot from the same writable disk under a new run identity.
After the first VM is absent, the fixture replays any pending ext4 journal
before its offline edit and inserts a unique upper-only proof executable;
the second run selects it through a private process override. This
proves upper-layer reuse at `/`, not public override support or persistence of
guest-created application data.
Definition, ACL, and temporary-fixture cleanup remain separately validated.
Production access provisioning, root deletion, socket/run reclamation, public
foreground/`-d` dispatch, and local build-to-run Gate 2 remain gated.

### PR 4 slice 30N: explicit retained-root lower lease handoff

A private handoff retires the old run's lower leases only after a distinct,
caller-selected successor has prepared the same graph and exclusively claimed
the same retained root. The successor must still be `creating/resources-ready`,
before domain planning/definition or monitor activation. Both run locks and
the exact volume lock fence the original evidence and successor attachment.
The writable disk, its ownership/generation, both lifecycle states, original
journal/socket, and immutable image data are not modified by handoff.

An old-ledger intent records exact source and successor lease snapshots before
retirement. The store locks the union of lease-use identities, then image
digests, then its lease index. It verifies the complete successor set inside
that boundary before unlinking only the captured old members and old intent.
Partial removal can resume only from the saved handoff intent; drift stops
further removal. Ledger writes stay outside artifact digest guards to avoid
inverting the artifact-reference lock order.

Completion records a historical transfer. Replay proves the old pins remain
absent but does not open the disk or depend on the successor still existing or
owning that volume. The original 30M retention API remains fail-closed after
its original pins retire; callers use the completed handoff receipt instead.
An incomplete handoff must finish before defining or otherwise advancing the
successor, because changed recipient state is deliberately not adopted.

Live qualification performs handoff before the second real VM boot, verifies
only the old pins disappear while the successor and immutable bytes remain,
and verifies the successor pins block artifact collection. The upper-only
executable then runs from the same root. This is not old-run/socket removal,
root deletion, production filesystem access provisioning, or public dispatch.

### PR 4 slice 30O: isolate QEMU runtime I/O from trusted run state

The lifecycle socket and OCI console now use the run's dedicated `io/`
directory. A QEMU socket server needs directory write access; granting that on
the trusted run root would also permit replacing its ledger and monitor entries.
The qualified ACL broker therefore grants directory write/search only on `io`,
traversal only on its trusted ancestors, and no access to `monitor-private`.
This is a prerequisite for production access provisioning, not its activation.

Domain-plan commit exclusively creates `io` at `0700` and its empty
`console.log` at `0600`, fsyncs them, and records both inode identities with the
run and plan binding in a separate trusted `oci_runtime_io` receipt. Existing
or ambiguous paths are not adopted, chmod-repaired, or recursively removed.
Failed publication preserves the uncommitted files and fails closed on retry.
Subsequent definition and launch reopen only the recorded directory and console
under the held run mutation lock; the lifecycle socket must be absent before
activation. The private fresh-exec bootstrap also pins both I/O resources.
Existing receipts are validated before opening their endpoints; fresh creation
rechecks the captured directory inode before creating the console. The I/O
guard is process-local and closes inherited descriptors after fork without
closing file-descriptor numbers reused by the child.

Console contents, length, and timestamps are untrusted mutable output, never
lifecycle authority. File type, owner, permissions, link count and recorded
inode identity remain checked. Production metadata checks continue to require
owner-only modes. Qualification alone verifies the exact named-QEMU ACL grant
before adapting its mode bits; no generic group-write exemption is introduced.
The existing boot-file relabel adapter remains test-only.
I/O drift blocks normal lifecycle publication; the pre-existing exact-domain
launch-failure cleanup policy is unchanged. In particular, the private
synchronous path may still clean its independently verified VM after an I/O
failure, but does not unlink or adopt the changed I/O files.

The host domain plan/core advance to v15/v9 and the private launch authority to
v2, rejecting the prior contracts instead of silently reinterpreting their
endpoints. Guest stage-1/lifecycle protocol and ordinary cloud/Lima console
paths are unchanged. Live qualification uses the production console path,
checks the restricted grants while the child-owned VM is active, and verifies
the retained-root successor has its own initially empty console.

Durable ACL grant/recovery, socket/run reclamation, public foreground/`run -d`,
and Gate 2 remain subsequent work. Shared lower artifacts and existing VM
definitions are outside this path-isolation change.

### PR 4 slice 30P: durable grants for the isolated runtime I/O

The private access boundary covers exactly `io/` and `io/console.log`. It
derives the non-root, non-owner QEMU UID/GID from the explicit libvirt
connection's canonical DAC/KVM capability. Under the existing run lock it
binds the monitor attempt, committed plan, I/O receipt, target identities and
complete baseline/granted ACLs in a separate `oci_runtime_access` intent
before any permission mutation. Console `rw-` is granted first, then directory
`-wx`; exact full-ACL readback and fsync precede completion. Unknown ACLs,
default ACLs, replacements and unexpected modes are refused, not repaired.

The Linux backend uses fixed executables, a sanitized environment and
inherited `/proc/self/fd` arguments, with an explicit mask and full ACL
replacement. Interrupted intent permits only the saved baseline or exact
grant. Completed replay verifies without rewriting. Runtime I/O checks and
fresh-exec authority v3 accept expanded mode bits only with the completed
receipt and exact actual ACLs; owner-only checks remain for ungranted runs.
Authority validation uses already-held descriptors without taking a run lock.

Revocation is a separate durable `revoking`/`revoked` operation: completed 30L
cleanup, the original STALE writer, exclusive existing journal ownership and
both domain name/UUID absent are required throughout. Directory access is
removed before console access. Only recorded grant/baseline states may resume;
completed revocation cannot be implicitly regranted. Neither endpoint, journal,
socket, lifecycle outcome, disk nor lower lease is deleted by this operation.
No-journal early-abandon revocation is intentionally unsupported: a prepared
but unspawned launch authority needs a separate invalidation contract.

Native qualification selects production I/O grants for the stale-cleanup
child boot, omitting both targets from the test broker and using no test I/O
validator adapter for that child. It checks actual ACLs while active, LIVE
writer refusal after terminal, exact restoration after 30L, idempotent replay
and console/journal/socket preservation. The other natural-exit, STOP and
retained-root successor boots retain explicitly test-only access adapters.
Ancestors, BOOT artifacts, root disks, lower exports and libvirt relabeling
are still qualification-only on every path. This is not complete production
filesystem provisioning or public OCI-root dispatch.

### PR 4 slice 30Q: make the VM-exclusive run root traversal-only

The same private access transaction now includes the exact VM-owned run
directory. Its only QEMU permission is named-user `--x` with an exact `--x`
mask, temporarily exposing mode `0710`; read, write, listing, entry mutation
and access to the owner-only ledger or `monitor-private` remain unavailable.
The grant sequence is console `rw-`, I/O directory `-wx`, then run traversal
last. Revocation removes run traversal first, then restores the I/O directory
and console. This ensures a partial grant cannot expose an unusable parent
before its children are ready and a partial revoke blocks guest traversal
before restoring descendants.

Access receipt v2 binds all three inodes and launch authority v4 carries that
receipt. Prior private v1/v3 contracts fail closed instead of being silently
reinterpreted; no public user state has been activated on this path. The run
target pins device, inode, owner, group, type and complete ACL while permitting
normal directory link-count, size and timestamp changes caused by owner-side
monitor setup. I/O-directory and console link-count rules stay strict. All
verification uses the already-pinned run/entry descriptors, repeats visible
identity checks after external ACL/libvirt calls and never reacquires the run
lock.

Fresh or interrupted grant accepts only the exact prefix states
`BBB -> BBG -> BGG -> GGG` for `(run, io, console)`; revocation accepts only
`GGG -> BGG -> BBG -> BBB`. Unknown mixtures, principals, ACLs or namespace
changes are preserved and refused. Incomplete operations fsync all three
desired target descriptors before completion even when a preceding attempt
already changed an ACL. Completed grant/revoke replay remains read-only.
Revocation retains the 30P requirement for completed 30L cleanup, the original
terminal STALE writer, the exclusive existing monitor journal and simultaneous
domain name/UUID absence. It deletes no run entry, journal, socket, I/O file,
disk or lease.

Native qualification removes the exact run root, I/O directory and console
from the fixture broker for the selected stale-cleanup child. It verifies the
three production ACLs while the real VM is active and the exact owner-only
baselines after recovery. Unrelated ledger fields and the existing journal,
socket, console and root-volume identities remain preserved; the access and
lifecycle ledger updates are intentional. Shared `state`/`runs` and host
ancestors, BOOT artifacts, root disk,
lower exports and libvirt relabel handling remain explicitly fixture-only.
The other four boots remain unchanged test-adapted coverage. This slice is one
more production access segment, not complete host-path provisioning.

### PR 4 slice 30R: share exact state/runs traversal by membership

The next private segment manages only the exact `StatePaths.state` and
`StatePaths.runs` directories. A separate owner-only namespace registry under
`locks` binds the two directory identities, complete baseline/granted ACLs and
the canonical DAC QEMU UID/GID. It retains explicit members keyed by run UUID
and access UUID, retaining an empty epoch and immutable per-run departure evidence.
This is not a refcount and does not change runtime-access receipt v2.

The explicit call order is per-run access grant, shared traversal join, then
monitor preparation/launch. Cleanup first proves the existing 30L terminal
STALE boundary and restores per-run access, then leaves the shared namespace.
The global namespace flock precedes any run flock. First join grants `runs`
search before `state` search; final leave restores `state` before `runs`.
Other joins/leaves never write or fsync ancestor ACL targets. Durable registry
intent precedes per-run intent and ACL changes; only the exact ordered ACL
prefixes resume. Final registry publication precedes final per-run publication,
so that crash window can finish without repeating a completed permission change.
Completed replay is verification-only.
Normal leave publishes registry-left, then run-left, and removes its own global
member before returning. A crash may retain a left tombstone; replay accepts its
exact presence or absence without writing. Repair of rare crash tombstones is a
future explicit operation, never an unrelated VM's implicit mutation. Registry
writes and join admission reserve space for completion and a future leave intent
for the largest active member within the 1 MiB read cap.

An owner-only permanent enrollment marker precedes the first registry and ACL
mutation. Missing either half of enrolled evidence makes initialization and
read-only launch validation fail closed, even in an empty epoch. Only the exact
original join can resume a complete marker-only crash intent at full baseline;
a partially written marker remains preserved and refused rather than adopted.

Fresh-exec authority v5 carries the immutable per-run membership and reuses its
existing state/runs FDs. A different VM's join or leave does not invalidate that
identity. Managed launch requires the caller's own active membership; merely
finding shared paths traversable does not authorize an unregistered run.
Owner-only preparation without a per-run grant remains available so that a
second VM can commit/define before obtaining its own grant and membership.
Private v4 launch frames fail closed; no implicit migration is provided.

Root initialization holds the same namespace lock, validates managed full ACLs
and preserves them instead of applying `chmod700`. Only an unmanaged namespace
retains legacy initialization repair: owner-held non-symlink directories are
set to mode700 through pinned FDs under the global lock. Its first explicit join
additionally checks full baseline ACLs and refuses other ambiguous active ledgers. Terminal
retained ledgers are preserved. Final path components cannot be symlinks, while
configured system ancestors such as macOS `/var` may be aliases. Wholesale
same-UID offline replacement of the entire state tree and its registry is not
claimed to be detectable without an external trust anchor.

Portable qualification exercises two members, non-final leave preserving the
survivor, final restore, interrupted ACL/fsync/ledger writes, membership
tampering and fresh-exec validation. Native qualification promotes the existing
stale-cleanup child to one real managed member, proving the first grant,
initialization preservation and final restore with Linux ACLs; it does not yet
claim a simultaneous two-VM native lifetime.
Shared store/CAS paths, external ancestors, BOOT and root-disk access, relabeling,
public dispatch, endpoint deletion and Gate 2 remain outside this slice.

### PR 4 slice 30S: bind root-file access to exclusive volume ownership

The next private access segment is the exact writable root raw file. Its
immutable run receipt binds the monitor attempt, attached volume generation,
run owner, lower graph, filesystem UUID, file identity and canonical QEMU DAC
principal. A separate durable volume fence records permission transitions;
an enrollment marker prevents loss of that fence from silently selecting the
legacy ungranted path. The existing run-then-volume lock order is preserved.
Even an ungranted lifecycle transition requires an owner-only raw file, so
losing both access-evidence files cannot release a still-granted disk. There is
no external history anchor against same-UID removal of all enrollment evidence
combined with restoration of the owner-only baseline.

Grant records intent before replacing the full baseline ACL with exactly one
named-QEMU `rw-` grant. Readback and fsync precede completion. Revocation requires
completed 30L cleanup, the original terminal STALE writer, exclusive journal
authority and both domain identifiers absent. It restores the exact baseline
before allowing retention, deletion or a successor claim. Interrupted operations
resume only from their recorded ACL states, and completed replay is read-only.
An interrupted deletion may finish from the exact quarantined inode or an
already-absent raw file with the recorded deleting generation. The permanent
access evidence remains after deletion; that volume UUID is not recreated.

Managed loading and fresh-exec launch validate the receipt, fence and current
attachment explicitly. Launch pins the root file descriptor, while permitting
guest writes to change file contents and timestamps. A retained root's next
attachment generation cannot inherit the old VM's launch authority. Legacy
ungranted preparation remains available; no general group-writable exception
is added to unrelated volume verification.
Private launch authority v6 includes the root receipt and matches it against
the current run ledger even when the frame carries no root receipt. Earlier
v5 frames are rejected; no public state migration is involved.

Native qualification promotes the stale-cleanup child's exact root file to the
sixth product-owned ACL target in both parent and child. It checks the real ACL,
LIVE-writer refusal, baseline restoration without changing root bytes, and the
existing retained-root successor boot. The successor still uses the fixture
access adapter. Root-volume ancestors, BOOT/shared exports and libvirt relabeling
remain qualification-only. This does not enable public dispatch or Gate 2.

### PR 4 slice 30T: share root-volume parent traversal

The existing shared traversal namespace now covers three exact directories:
`state`, `runs` and `state/oci-root-volumes`. The new `root_volumes` target uses
only named-QEMU search permission (`--x`, mode0710); it grants no listing or
entry creation/deletion rights. Per-file raw access remains governed by the
30S generation fence, and volume metadata files remain owner-only.

All three directories share one membership set, epoch and namespace lock.
First join grants `root_volumes -> runs -> state`; final leave restores
`state -> runs -> root_volumes`. Non-final departures preserve the survivor's
access without ACL writes or target fsync. Ordered partial states, permanent
enrollment, bounded registry admission and immutable member identities retain
the existing recovery rules. Managed initialization validates and preserves
the third directory rather than applying legacy chmod repair.
Root-file creation also validates and preserves the managed parent, so preparing
a second VM cannot reset its mode to0700 and cut off the first VM. This scoped
OCI policy leaves ordinary project-volume initialization unchanged.

The private registry/member schema becomes v2 and launch authority becomes v7.
Launch carries the root-volume directory FD and verifies all three exact
targets with its own active membership. A different VM's join/leave does not
invalidate a survivor's authority. Earlier v1 namespace/member and v6 launch
contracts fail closed; no public state migration is involved.

The explicit completion order is root-file revoke, per-run I/O revoke, shared
leave, then retention/reclaim. Shared leave refuses an outstanding managed
root-file grant before changing membership or ancestor ACLs. This prevents
a later namespace join from exposing a departed run's leftover raw grant.
Completed left replay remains historical and read-only after a later root
retention, successor claim or deletion.

Native qualification removes the root-volume directory from both fixture
brokers as the seventh product target. It checks exact ACLs during the existing
stale-cleanup child, initialization preservation and final restore, then keeps
the retained-root successor proof. The successor still uses fixture access;
simultaneous two-member lifetime is covered by portable tests. External host
ancestors, BOOT/shared immutable exports, relabel policy, public dispatch and
Gate 2 remain subsequent work.

### PR 4 slice 30U: manage read-only stage-1 transport access

The first immutable BOOT access slice covers only the exact run-owned
`stage1-plan.raw`. Its baseline remains owner-read-only mode0400. A separate
read-only ACL policy grants the selected QEMU identity exactly `r--` with
mode0440, never owner or QEMU write permission. Existing mutable root and I/O
grant policies remain separate.

A run-bound access receipt ties the monitor attempt, committed domain plan,
transport receipt, content digest and held file identity to durable grant and
revocation phases. Full transport framing and provenance checks remain in
place. The unmanaged file verifier still requires its original owner-only
policy; mode0440 is not accepted without explicit managed authority.

Fresh-exec authority v8 carries the managed stage-1 transport FD and receipt.
Current run evidence is checked against omitted/null/old frames, with a final
callback-free validation after external lookups. Earlier private v7 frames
fail closed; this is not a public state migration.
The immutable metadata snapshot, including ctime, bridges the original ACL
check and later callbacks so an ACL principal change with unchanged mode and
bytes is also refused. Final content verification does not substitute for this
metadata continuity check.

Teardown revokes stage-1 access after completed inactive-domain cleanup and
proof that the terminal journal writer is stale, before runtime traversal is
retired. The workflow is stage-1 revoke, root revoke, runtime I/O revoke,
shared leave, then retain/reclaim. Shared departure refuses an outstanding
managed stage-1 grant; completed left replay remains historical.

Native qualification promotes this file to the eighth product ACL target in
both parent and fresh-exec child. It checks active read-only ACLs, live-writer
revocation refusal, final baseline restoration and unchanged file identity and
bytes. The retained-root successor still uses fixture access.

Kernel/initramfs and lower-image exports remain subsequent work: canonical
CAS objects must not be chmod-opened to QEMU, shared exports need their own
membership and reclamation contract, and BOOT ownership/relabeling must be
selected before committing the domain projection. The existing stage-1 disk
continues to use explicit source DAC `relabel=no`; this does not resolve BOOT
relabel policy or enable public dispatch or Gate 2.

### PR 4 slice 30V: run-owned kernel and initramfs exports

Before domain planning, publish independent `boot-kernel` and `boot-initramfs`
files in the owning run directory. A durable pair receipt binds the run UUID,
resource plan, digests, sizes, exact file identities and selected canonical
QEMU DAC principal. Publication never changes the source files or creates
hardlinks into them. Completed publication selects these files for subsequent
planning and resolution, without depending on the original source paths.
Partial publication resumes only the recorded pair; missing authority or
unexpected reserved-path files must not fall back to caller-owned artifacts.

The run-owned pair has a separate read-only ACL lifetime, with durable
intent/granted/revoking/revoked phases and owner0400/QEMU0440 policies.
Fresh-exec authority v9 binds publication and access receipts to the existing
kernel/initramfs descriptors. Full hashes and immutable metadata, including
ctime across callbacks, remain part of validation. Old private v8 frames fail
closed; this is not a public state migration.

The selected DAC principal is fixed before the domain projection through an
exact top-level static DAC label and `relabel=no`. This is DAC-specific, not a
request to disable AppArmor or SELinux. The deployed host must qualify the
resulting inactive XML and actual boot without changing export ownership or
the immutable metadata. See the official [libvirt security-label contract](https://libvirt.org/formatdomain.html#security-label).
Libvirt forbids per-source label overrides when domain-level relabeling is
disabled: this exact global policy therefore omits disk and console DAC
overrides. Legacy domains without the global policy retain their mandatory
per-source `relabel=no` labels. The two XML forms are validated separately.
Post-define comparison also recognizes the exact generated file-serial mirror
for both forms: the global-policy console lacks the legacy per-source label
tuple. Only the initial generated serial count is normalized; the strict
mirror parser and subsequent stored projection still reject device drift.

The initial inherited-socket CONFIG envelope has a separate 1 MiB limit for
the complete v9 descriptor/receipt authority. Its exact canonical bytes are
validated before creating a socket or child, and the child enforces the same
limit before reading the body. The fixed role set has at most 24 descriptor
paths; individual lower disks are reloaded from the ledger, not serialized
into this envelope. Ordinary control frames and journals remain capped at
16 KiB, with unchanged typed parsing, identity and descriptor validation.

After inactive-domain cleanup and proof that the original terminal writer is
stale, revoke BOOT access before stage-1, root, runtime I/O and shared traversal
retirement. Native qualification excludes the pair from both fixture brokers,
bringing the stale-cleanup child to ten product ACL targets. The retained-root
successor still uses its separate legacy fixture boot inputs.

Shared lower-image exports remain a distinct next slice: their membership,
lower leases and GC must cover multiple VM lifetimes. Canonical CAS objects,
external host ancestors, public OCI-root dispatch and Gate 2 stay unchanged.

Gate 1 is active now. `tests/integration/test_buildkit_named_oci_context.py` runs the Palimpsest CLI with a unique digest-pinned local OCI named context under strict offline/network-none BuildKit policy and `--no-cache`, verifies every output OCI descriptor/blob plus the layer sentinel, checks the independently exported rootfs, and binds stdout to the durable manifest/archive receipt. PR and release workflows create a network-none builder and run this gate.

Gate 2 is present but opt-in and intentionally skipped until the OCI-root KVM path exists. Its build and runtime halves are split so the KVM proof runs on a Docker-daemonless host. `tests/e2e/prepare_local_oci_build.py` creates a Palimpsest-built OCI archive plus a receipt bound to its SHA-256, manifest, platform, and random marker; CI transfers that directory to the runtime-only `tests/e2e/test_local_oci_build_run.py` gate:

```text
palimpsest build local-pinned-base → OCI archive
→ transfer immutable archive + acceptance receipt to daemonless KVM host
→ palimpsest run ARCHIVE --backend kvm -d
→ palimpsest exec probe
→ prove image marker is at / and PID 1 root is /
→ stop → rm
→ prove run-owned state is removed while immutable source image remains
```

Gate 2 activation requires all of the following, not merely successful layer conversion: local OCI archive/layout intake, bootable OCI-root run request and KVM adapter, host kernel/initramfs policy, guest stage-1/root assembly, init supervision, detached `-d` lifecycle, exec readiness, VM-specific writable root disk/volume ownership, and safe stop/remove recovery.

### PR 4 slice 30W: shorter test feedback and public-runtime priority

At the user's request, per-edit verification uses explicit functional test
lanes and changed-file recommendations instead of repeating the complete
server suite. CI still covers all portable cases through deterministic case
shards (function identity plus parameter indices, not random display IDs),
including large modules; unknown test files fail classification.
Native, BuildKit and privileged proofs remain separate explicit gates. Full
regression remains an integration/release requirement. See
[the testing workflow](docs/testing.md).

### PR 4 slice 30X: local auto-pin and run-owned lower delivery

Local archive/layout snapshots can select exactly one root descriptor without
a caller-supplied digest; the selected root is still fully content-verified.
`oci materialize` exposes that path, while ambiguous root lists require
`--manifest`. A missing pin is not permission to guess between image entries.

Before domain planning, the run-owned lower provider copies distinct leased
SquashFS digests into sealed owner-only files beside the BOOT exports. Durable
receipts bind the exact target inodes, resource plan, lease graph and static
QEMU principal. Domain resolution and fresh-exec authority consume those
copies, not fixture-remapped paths or chmod-open CAS objects. Repeated lower
occurrences retain their ordered disks and leases while sharing only their
own run's physical copy. Explicit lower ACL grant/revoke and the shared
traversal departure fence retain the existing inactive-domain/STALE proof.

The private monitor authority advances to v10 with the exact lower descriptor
set; the initial CONFIG envelope remains bounded. Old private frames fail
closed. This is a prerequisite connection, not public `run/-d` or Gate 2
activation. The public coordinator, session and failure-cleanup integration
remain the next work, ahead of shared-cache optimization.

### PR 4 slice 30Y: fresh coordinator and explicit service lifetime

The private coordinator accepts exact pinned launch authority from an existing
threaded/libvirt caller and delivers it through a bounded private socket and
descriptor allowlist to a clean Python child. It preserves the original monitor
spawn boundary, checks the returned generation against authenticated discovery,
and reaps the coordinator before returning. Acceptance is not guest READY;
uncertain outcomes preserve the exact monitor journal and ownership evidence.

Explicit `terminal_timeout_seconds=None` permits a service to run until terminal
completion or authenticated STOP. The default remains 45 seconds, with bounded
boot and STOP deadlines independent of service lifetime. The native coordinated
case uses a real imported-libvirt/threaded caller without the old launch helper
or first-boot worker/path monkeypatches. It waits for READY, exercises STOP and
the existing exact stale/inactive cleanup and retained-root second boot. The
outer qualification directory still needs its fixture-only traversal grant;
this is not yet public host setup or public `run/-d` acceptance.

The coordinator test file belongs to the explicit `oci-monitor` lane. Per-edit
verification remains selected tests; public CLI and Gate 2 gates stay closed.

### PR 4 slice 30Z: monitor-backed foreground and completed STOP

`MonitorClient` binds the existing run, private-directory inode, generation,
writer, socket, monotonic journal and lifecycle identity. READY may race natural
terminal completion, which is returned explicitly rather than disguised as a
live detached run. A STOP acceptance is not a result: completed STOP requires
the matching terminal ledger/journal and authenticated worker-completion reply.
Actual guest exit/signal fields are validated and returned without synthesizing
a successful status from console text. Ambiguity preserves the run's evidence.

The noninteractive `OCIMonitorProcessSession` follows bounded VM-console reads
through the existing runtime I/O receipt and access guards, then drains output
before a single actual process status. Output is the combined VM console,
including boot diagnostics, not independent guest stdout/stderr. Reader close
only detaches. INT/TERM handling only enqueues a coalesced lifecycle STOP;
the event loop sends it outside run locks to avoid signal-handler reentrancy.
Stdin, TTY, resize, HANGUP and additional guest exec remain unsupported.

Optional bounded existing-run lock acquisition preserves the default blocking
behavior for old callers. Private client IPC uses a single exchange deadline
instead of extending its budget for each partial peer write. The native
coordinated case reads console bytes while the guest is live, uses a separate
unpatched process to call completed STOP, and compares the session exit with
the real terminal receipt before existing cleanup and retained-root reuse.

These are product components for the public adapter, not public CLI activation.
Host BOOT/runtime-root setup, typed OCI request/dispatch, run flags and remove
orchestration/recovery still precede public `run/-d` and unchanged Gate 2.

### Next implementation order: two public acceptance milestones

The next implementation is a vertical public lifecycle, not completion of
all future shared-cache machinery before a user can run a VM. The initial
qualified target is local OCI archive/layout input on Linux x86_64 KVM.
Docker Hub intake and multi-VM data volumes remain goals, not removed scope.
The complete rationale and executable acceptance criteria are in
[the public-runtime critical path](docs/oci-public-runtime-roadmap.md).

1. **Public run lifecycle.** Resolve and pin the local OCI manifest, prepare
   the first-party BOOT inputs and run-owned sealed lower copies, and connect
   the existing plan/define/access/monitor machinery to public foreground
   `run`, `run -d`, authenticated `stop`, and exact `rm`. Keep logical lower
   digest/occurrence graphs and lease sets unchanged. Per-run lower copies
   avoid making shared-export registry/GC a prerequisite for this milestone;
   never hardlink or chmod-open canonical CAS. Validate an explicitly prepared
   traversable runtime root rather than silently changing `$HOME` permissions.
   Definition/ACL setup and fresh monitor spawning retain the existing clean
   process boundary. READY, exit/signal forwarding, exact cleanup, access
   revocation and retained-root ownership are acceptance requirements, not
   deferred after enabling the corresponding public operation.
2. **Noninteractive exec and Gate 2.** Implement one authenticated additional
   guest command with argv, bounded stdout/stderr and exit status, then pass
   the existing local-build archive → public `run -d` → `exec` root probe →
   `stop` → `rm` gate on a Docker-daemonless KVM host. Existing lifecycle v2
   has no remote EXEC operation: its main-workload cgroup name is not an exec
   implementation. Do not replace this gate with a boot-only test or return
   the expected probe output from the host.

Shared lower exports/GC optimization, multi-VM data-volume sharing, TTY and
parallel exec follow these milestones. Preserve lower pins whenever ownership
handoff is incomplete. Authenticated control still requires the live monitor's
in-memory boot key; a dead journal alone cannot recover running control.
Public operation gates remain closed until their own vertical proof passes;
test partitioning by itself does not enable them.

### PR 4 five-stage public lifecycle integration

1. Explicit host kernel/config pins, clean libvirt Python and conservative
   ancestor admission; create-only `oci init-runtime` never chmods a home.
2. Typed local OCI request and real snapshot/materialization, preserving the
   image process contract and existing cloud-image RunSpec boundary.
3. Normal completed-run removal: terminal/domain/monitor evidence before
   access revocation, shared traversal departure, root/lease release and delete.
4. Product launch adapter joins BOOT/lower exports, exact native projection,
   grants, fresh coordinator, authenticated READY and foreground session.
   Queued startup cancellation does not interrupt lock/receipt mutations.
5. Public local OCI `run`, `-d`, `ps`, `stop`, `rm` dispatch and a separately
   opt-in public CLI native proof, including actual OverlayFS `/`, finite exit,
   detached STOP, foreground SIGINT and normal cleanup.

Only Linux amd64 KVM/system libvirt, no network, no process overrides and
combined VM-console output are currently in this public scope. Additional
guest exec and the unchanged Gate 2 remain the next milestone. Pre-activation
grant-failure/stale-socket recovery, public retained-root UX and multi-VM data
volumes remain explicitly pending; failures preserve owned resources.

### PR 4 parallel batch: bounded additional guest exec

Guest process supervision, host mailbox/IPC/process sessions, protocol/transport
integration and independent review were split into concurrent work slices.
The authenticated additional-command engine is implemented and passed its real
KVM engine proof at `02af2879bd79f19cdbfb02cd687d965e78283d55` (22.02 s).
It preserves separate stdout/stderr and real status, literal bounded argv,
one active command, image credentials and private mounts, finite time/output
limits, descendant cleanup and STOP precedence. Acknowledged sessions close
their local clients without stopping the VM. A lost reader leaves a bounded
unacknowledged result rather than silently rerunning it.

Public noninteractive `exec` is connected only after that engine proof. Its
CLI-only native file is independently selectable from engine, existing public
lifecycle and unchanged Gate 2 tests. Full suites are not a per-edit requirement;
selected tests and the explicit proofs retain separate evidence.

Gate 2 still requires a Docker-daemonless host and its original PID 1 root
probe. The current pieroot-server has Docker sockets, and dereferencing
`/proc/1/root` conflicts with the deliberately protected supervisor. Do not
hide sockets, stop unrelated services or relax PID 1 protections for a green
test. See [the exec contract and evidence](docs/oci-additional-exec.md).
Actual public CLI and Gate 2 results are recorded separately from engine proof.

### Follow-up: Docker-coexistent Gate 2 and PID 1 protection report

The user now permits Docker on the KVM host, even if a future workflow requires
it. This supersedes the earlier daemonless-host prerequisite, not the original
PID 1 root probe or guest security policy. Do not introduce an unnecessary
Docker dependency into the current native KVM run/exec implementation.

Batch the host-acceptance update, failure/cleanup evidence improvements, and a
read-only PID 1 protection audit. Gate 2 should retain its Docker CLI fallback
audit, allow socket presence, use actual selected state roots and verify cleanup
even after probe failure. No Docker services are stopped and no sockets hidden.

The [Korean PID 1 report](docs/oci-pid1-protection.ko.md) distinguishes guest
supervisor authority from image rootfs and UID 0, documents the actual layered
controls and existing proof limits, and presents nonweakening follow-up options.
Changing the root-proof criterion or providing a full-root VM mode remains a
separate decision; this explanation request does not authorize those changes.

The Docker-coexistent follow-up is implemented and independently reviewed at
`61cc1bf99ed5653155f530727bcaad50cbd151ab`, with 69 focused tests passing both
locally and on the exact-SHA server checkout. The explicit Gate 2 now reaches
the original guest probe and fails at protected `/proc/1/root`, not the Docker
host prerequisite. All six failure-cleanup/source-preservation checks passed.
See the acceptance document for preserved evidence; Gate 2 is not complete.

### Follow-up: resource failure ownership and exec refusal diagnostics

Proceed independently of the unresolved PID 1 proof decision: distinguish
worker/packer resource spawn failures and make partial helper-thread startup
transactional, while preserving the existing resource limits and exact worker
cleanup authority. In parallel, distinguish exec lifecycle/occupied refusals
and reject malformed mailbox state without submitting or acknowledging work.

These are diagnostics and failure-path corrections, not result takeover,
automatic replay, relaxed guest isolation, or a changed Gate 2 probe. Verify
the selected worker/packer and exec tests separately, then push and execute the
public exec native proof on the exact server SHA. Full suites are not the
per-edit requirement; recovery UX and root-proof policy remain separate work.

Implemented and independently reviewed at
`a4903d0e24572058689c285c0b3326809dc628d8`. The exact pushed server checkout
passed 246 selected tests (14.63 s) and the separate cold public exec CLI native
proof (20.13 s). Local focused checks passed 239 with seven Linux-only skips;
server checks covered those seven. Gate 2's known protection/probe conflict
remains unresolved. The user-approved prior Docker/PID 1 summary was recorded
and read back in Notion section 17.85, without operational paths or process IDs.
