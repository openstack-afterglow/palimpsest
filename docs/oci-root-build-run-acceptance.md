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

The lifecycle binding bumps the pre-production OCI-root domain plan to v5 and
domain core to v3. Earlier v4/core-v2 previews are rejected and must be rebuilt
before a future launch; loading one never migrates, deletes, or otherwise
changes its run state or transport artifact.

This is topology and validation only. Palimpsest does not yet call libvirt
`openChannel`, run a guest lifecycle broker, dispatch `STOP`, reconnect a real
domain, or expose the protocol through `run`, `stop`, or `-d`. Gate 2 therefore
remains opt-in and skipped. The host nonce provides correlation and a replay
challenge, not cryptographic peer authentication; that requires a future
owned libvirt channel plus a MAC or equivalent authenticated transport.
