# Local OCI build-to-run acceptance

Palimpsest has two deliberately separate product gates for local OCI images.

## Gate 1: local product build

The product test in `tests/integration/test_buildkit_named_oci_context.py` invokes the Palimpsest CLI with a digest-pinned local OCI named context; an adjacent test retains direct Buildx interoperability evidence. The product test requires an already bootstrapped, network-none `docker-container` Buildx builder and proves all of the following:

- `palimpsest build` accepts the pinned local image without registry or Hub access;
- the solve runs with `--offline --network none`;
- Palimpsest emits an OCI archive whose index, manifest, config, and layer descriptors match their sizes and SHA-256 digests;
- both the archive layer and independently exported rootfs contain a per-test source sentinel; and
- stdout and the durable build receipt bind the output manifest, archive digest, local source manifest, offline mode, and network policy.

The `Local OCI image product build` CI job creates a fresh network-none builder and runs both tests. The release verification workflow repeats this gate before building distributions.

Run it with:

```sh
PALIMPSEST_BUILDKIT_E2E=1 \
PALIMPSEST_BUILDKIT_BUILDER=palimpsest-e2e \
uv run pytest -q tests/integration/test_buildkit_named_oci_context.py
```

## Intake/materialization checkpoint

The first-party bridge between Gate 1's OCI archive and the future boot plan is now explicit on Linux:

```sh
palimpsest oci materialize ./image.oci.tar \
  --manifest sha256:<index-or-manifest> \
  --output ./materialization.json
```

A standard OCI layout directory can replace the archive path. The command selects exactly `linux/amd64`, verifies the pinned descriptor graph into the private source CAS, and materializes every layer occurrence in manifest order through the hard-worker boundary. Repeated descriptors retain distinct ordinals. The output is a path-free derived-cache receipt, not a boot plan or runtime lease. On macOS, source intake is portable but real materialization intentionally fails at the Linux-only worker/toolchain boundary.

## Gate 2: OCI root `/` in a detached VM

Gate 2 is intentionally opt-in until the OCI-root KVM adapter is implemented. It is split across two hosts so the runtime proof cannot reach Docker:

1. On the isolated BuildKit host, `tests/e2e/prepare_local_oci_build.py` builds from a digest-pinned local OCI layout through `palimpsest build`.
2. The build job retains the OCI archive, independent rootfs proof, and a receipt binding the archive SHA-256, manifest digest, platform, and random marker.
3. CI transfers that immutable artifact directory to a separate KVM host without a Docker daemon/socket.
4. `tests/e2e/test_local_oci_build_run.py` verifies the transferred receipt and starts the archive through `palimpsest run ... --backend kvm -d`.
5. `palimpsest exec` runs the image-baked probe, which proves the random marker is visible both at `/` and through `/proc/1/root/`.
6. The test requires a running libvirt domain, stops and removes the VM, and proves the domain and run-owned state are gone while the immutable archive remains.

The gate must not be enabled merely because layer materialization succeeds. Local OCI archive/layout intake and ordered materialization now exist, but activation additionally requires durable boot-plan leases, a bootable OCI-root KVM request, host kernel/initramfs policy, VM-specific writable root volume ownership, the OCI init supervisor, detached lifecycle support, and `exec` readiness. The KVM runtime job rejects standard local Docker sockets, replaces `docker` in `PATH` with a failing audit shim, and points `DOCKER_HOST` at a nonexistent socket. It also requires a running libvirt domain with the run name and verifies that removal undefines it.

On the BuildKit host, provide a bootable local OCI base pinned as `PATH@sha256:<manifest>` and create the transfer artifact:

```sh
uv run python tests/e2e/prepare_local_oci_build.py \
  --base /srv/fixtures/base-layout@sha256:... \
  --platform linux/amd64 \
  --output-dir "$RUNNER_TEMP/oci-root-build"
```

After transferring that directory to the daemonless KVM host, run:

```sh
PALIMPSEST_OCI_ROOT_E2E=1 \
PALIMPSEST_OCI_ROOT_E2E_ARTIFACT_DIR=/srv/fixtures/oci-root-build \
PALIMPSEST_OCI_ROOT_E2E_LIBVIRT_URI=qemu:///system \
uv run pytest -q tests/e2e/test_local_oci_build_run.py
```

Until those runtime prerequisites exist, the default suite skips Gate 2. This skip is a visible missing capability, not evidence that local OCI images can already boot as `/`.

## Lifecycle channel contract checkpoint

OCI-root domain plans now reserve exactly one fixed virtio-serial lifecycle
channel using `palimpsest.oci-lifecycle-control.v1`. Its bounded canonical
wire contract binds the run, domain core, stage-1 artifact, fresh host nonce,
guest-issued boot generation, monotonic host request IDs, and boot-wide guest
event sequence. Reconnect rotates the nonce and requires an exact ready,
stopping, or terminal snapshot from the same boot generation; if a stop is in
flight, its snapshot also binds the outstanding original stop request until terminal.
Before the first READY establishes a generation, a lost-HELLO retry accepts
only READY or a ready-only snapshot for the current nonce and request. A
terminal snapshot during an ambiguous in-flight stop records either the exact
original STOP ID or null when that STOP was not delivered and the workload
instead exited naturally; every foreign ID is rejected.
If the snapshot reports ready, the original STOP was not delivered and may be
retransmitted with the same logical request ID, generation, and payload. The
future guest broker must handle that exact retransmission idempotently and
reject conflicting reuse.

The lifecycle and supervisor bindings use pre-production OCI-root domain plan
v8 and domain core v3. Earlier v4/v5/v6/v7 and core-v2 previews are rejected and must be rebuilt
before a future launch; loading one never migrates, deletes, or otherwise
changes its run state or transport artifact.

The v15 native qualification harness exercises a single connection, a
six-connection retained-root session, and a separate capabilityless UID 0
positive boot. The retained-root session loses the initial READY,
reconnects through READY/stopping/terminal snapshots, writes a partial STOP,
retries the complete same logical STOP, and then proves already-committed
same-ID deduplication without a second signal dispatch. Linux connects pin the
QEMU socket identity and require `SO_PEERCRED` to identify the spawned QEMU PID
and current UID. The qualified reconnect waits for an exact guest EOF-observed
console marker before opening each next connection, and the partial-STOP case
also waits until the guest reports the exact frame-minus-one buffer state.
These markers are proof-only coordination with the known workload and are not
production lifecycle authority. Production must provide a privileged in-band
boundary acknowledgement or equivalent barrier; arbitrary rapid reconnect is
not qualified. Ten separate lifecycle-negative guest boots cover two channel
discovery failures and eight exact malformed, stale, replayed, or conflicting
wire inputs; a separate non-boot QEMU invocation proves duplicate named ports
are rejected before stage 1 starts. Thus `reconnect_proven=true` and
`negative_input_proven=true`; natural workload terminal delivery remains
implemented but explicitly unqualified (`natural_terminal_proven=false`).

The UID 0 boot proves the pre-MAC lifecycle-authority isolation boundary before
workload release: the lifecycle fd is closed, PID 1 is non-dumpable, `/dev` is
an exact private safe-device tmpfs, procfs/sysfs/cgroup controls are read-only or
masked, all capability sets are empty, securebits are locked,
`NoNewPrivs=1`, and `Seccomp=2`. Authority syscalls and PID 1 fd/memory access
are denied while ordinary fork and safe-device I/O still work. This does not
claim a PID/user namespace or complete denial-of-service containment.

Receipt v15 binds 41 native guest boots and one duplicate-name preboot
rejection (42 QEMU invocations), stage-1 plan/protocol v11, handoff v5,
init/consumer v13, initramfs manifest/ABI v15, supervisor v6, lifecycle v2,
fixture policy/schema v9, and domain plan v10/core v4.

Slice 29A adds a separate production-inert v2 host candidate without changing
that v15 evidence or the active v1 guest/domain contract. The candidate uses a
per-boot key, direction/carrier-separated HKDF-HMAC-SHA256, a console-only
signed `BOUNDARY_ACK`, and exact canonical receipt projections that omit the
raw key and MAC. It distinguishes host-attempted wire sequences from those the
guest has proven accepted. Partial STOP recovery retains its logical STOP ID.
For partial RECONNECT, a signed ACK retaining the old connection identity
retries the same logical request, while an ACK committing the attempted
connection consumes it and starts a new logical recovery request. Both use
fresh wire/epoch/nonce/MAC data. A natural-terminal race preserves both possible
STOP acceptance wires until a later ACK commits one exact value; a STOP-caused
terminal permits only the attempted STOP wire. Signed receipt projections
verify the key internally rather than trusting a caller-supplied result.
Boundary parser evidence accepts only an empty parser, a one-to-three-byte
partial header, or an incomplete bounded payload.

This candidate remains inert until slice 29B atomically adds PID 1 key custody
and post-fork wipe proof, guest framing/MAC verification, console boundary
emission, negative native KVM coverage, regenerated deterministic assets, and
the complete protocol/broker/isolation/supervisor/plan/guest/initramfs/proof/
domain version cascade. Until that gate passes, neither v2 nor its boundary ACK
is a guest-runtime or native-KVM claim.

Production still does not call libvirt `openChannel`, dispatch runtime `STOP`,
or expose the protocol through `run`, `stop`, or `-d`, so Gate 2 remains opt-in
and skipped. The host nonce provides correlation and a replay challenge, not
cryptographic peer authentication; that still requires a future owned libvirt
channel plus a MAC or equivalent authenticated transport.
