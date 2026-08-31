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

### Local image build-to-run acceptance gates

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

### Next implementation order

1. Centralize all physical artifact deletion/GC behind descriptor-pinned store mutations that honor durable OCI occurrence leases.
2. Add first-party local OCI archive/layout intake and materialize every ordered occurrence through the hard-worker API.
3. Define the OCI-root boot plan: immutable lower leases, VM-specific writable root volume, reusable retained boot volume, kernel/initramfs, stage-1 mount/pivot, and init supervisor.
4. Implement foreground-default `run` and detached `run -d`, then lifecycle/exec/log readiness for OCI-root/KVM.
5. Activate the opt-in local build-to-run gate on a qualified self-hosted Linux KVM runner and require it before claiming that an OCI image becomes VM root `/`.
