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
channel using `palimpsest.oci-lifecycle-control.v2`. The first `HELLO` is the
only unsigned envelope. After the workload child has forked and completed its
isolation handshake, PID 1 creates a per-boot key, proves that the child cannot
observe it, returns a self-authenticated `BOOTSTRAP`, and waits for signed
`KEY_ACK` before releasing the workload. Every later channel or console frame
uses direction- and carrier-separated HKDF-HMAC-SHA256 over exact canonical
bytes.

For fail-closed diagnosis, PID 1 emits fixed non-secret console markers only
after lifecycle channel readiness, valid initial `HELLO`, transmitted
`BOOTSTRAP`, and valid `KEY_ACK`, respectively. Positive native receipts require
each marker exactly once in that order before workload isolation/start. The
markers contain no protocol secret or run identity and do not authorize state.

Reconnect no longer depends on a proof-only plaintext EOF marker. PID 1 emits
a signed console-only `BOUNDARY_ACK` after `read(2)` returns zero. It commits a
boot-wide fresh boundary ID, the exact discarded parser counters, previous and
next connection identity, last accepted host wire, last attempted guest wire,
and lifecycle-state digest. The next signed `RECONNECT` binds that boundary ID
and its envelope digest; the resulting `SNAPSHOT` must exactly equal the state
admitted from the acknowledgement.

The six-connection qualification session loses the initial READY, accepts a
RECONNECT whose SNAPSHOT is lost, discards a partial STOP at EOF, retries the
same logical STOP on a fresh authenticated wire, deduplicates an exact same-ID
retry without sending a second signal, and reconnects through terminal state.
Natural-exit and STOP races retain only bounded authenticated wire candidates
until a later boundary commits one exact value. Complete, mixed, oversized,
stale, replayed, cross-carrier, or conflicting inputs fail closed.

Receipt v19 binds 43 native guest boots and one duplicate-name preboot
rejection (44 QEMU invocations), stage-1 plan/protocol v15, handoff v9,
init/consumer v17, initramfs manifest/ABI v19, supervisor v10, lifecycle broker
v3, workload isolation v3, lifecycle protocol v2, fixture policy/schema v11,
domain plan v14/core v8, and KVM proof v19. It additionally binds bounded
image-root passwd/group lookup, omitted primary-GID resolution, empty
supplementary groups, and shell-free PATH execution. Receipt projections verify the
canonical envelope and MAC before retaining only safe digests and public
bindings; raw boot keys and MAC tags are recursively excluded. The supervisor
owns an empty agent cgroup and one monotonic-ID exec-session leaf, attaches the
qualified process only to the leaf, and proves leaf-first then parent cleanup.
Before terminal state or TERMINAL is published, PID 1 reopens `/` as a no-follow
directory, verifies stable OverlayFS and `/proc/self/root` identity, completes
`syncfs`, and successfully closes both descriptors.
Parallel exec sessions remain explicitly unproven.

Public production dispatch still does not call the new private libvirt
`openChannel` handoff, dispatch runtime `STOP`,
or expose the protocol through `run`, `stop`, or `-d`, so Gate 2 remains opt-in
and skipped. Slice 29B activates lifecycle v2 only in the pre-production guest,
domain-plan, and native-KVM qualification path.

The private OCI-root connector registers libvirt's default event implementation
once, before opening its first connection, and binds the registration to the
current process. Lifecycle launch rejects ordinary pre-opened connections and
post-fork reuse. After `openChannel`, a stream callback plus a bounded 10-ms
timer drives `virEventRunDefaultImpl` during nonblocking waits. Receive waits
subscribe to readable/error/hangup; writable is added only while a send reports
backpressure, then removed to avoid a writable busy loop. The callback is
removed before stream abort/free on every normal or exceptional handoff path.
This remains a synchronous private qualification surface; public create,
start, run, and detached dispatch stay disabled.

A separate production-inert monitor-ownership foundation now records an exact,
secret-free per-run binding for the run owner, plan and definition projection,
stage-1 artifact, libvirt URI, domain UUID and active ID, guest boot attempt, and
the writer's host-boot/PID/start-tick incarnation. An owner-only flock permits
one writer. Canonical JSON transitions use file `fsync`, atomic publication, and
directory `fsync`; an uncertain post-replace sync poisons the live handle and
does not report a committed transition. A fork child drops inherited monitor
descriptors and permanently invalidates its copied handle.

Stale adoption is deliberately recovery ownership, not running lifecycle
recovery. The lifecycle v2 boot key is memory-only, so a replacement process
cannot reconstruct authenticated STOP/reconnect authority from the journal.
It may commit only `adopting` then `control-lost` for a future exact cleanup
path. No runtime, supervisor, dispatcher, CLI, or libvirt launch code imports
this module yet, so public run/`-d` remains disabled.

The native `qemu:///system` qualification has a test-only filesystem access
broker for libvirt's DAC QEMU identity. It is not a production authority. Its input is the
unique canonical `dac` security-model `baselabel type="kvm"` (`+uid:+gid`) from
libvirt capabilities, and every prospective ACL target must be a stable,
owner-held, non-symlink regular file or directory below a short owner-only
qualification root created directly under a strict root-owned `01777` `/tmp`.
The external qualified kernel is copied into that root so the source artifact's
ownership and permissions are never mutated.

The target validator pins the test root and walks each component with
`openat`/`O_NOFOLLOW`, comparing visible and opened identities. Its returned
stat is nevertheless only a snapshot after those descriptors close, not a
race-free mutation capability. The broker repeats the check, retains the target
descriptor, and invokes `getfacl`/`setfacl` through its inherited
`/proc/self/fd/<n>` name. This narrows component-substitution exposure, but does
not turn pathname-oriented `setfacl` into a native FD mutation API.

The owner-only kernel copy records the destination FD identity immediately
after its `O_EXCL` create. Every successful final path validation must return
that same device/inode and match a fresh destination-FD `fstat`. Failure cleanup
unlinks only a path still naming that exact owned regular inode. If the initial
destination `fstat` itself fails, `O_EXCL` alone cannot distinguish the created
entry from a later same-UID replacement, so the harness preserves the partial
path and reports an explicit identity-unavailable error.

Linux POSIX access ACLs expose the ACL mask through the file's group mode bits.
Consequently an effective named ACL for an owner-only `0700`, `0600`, or `0400`
target cannot also leave its complete mode unchanged: `setfacl --no-mask`
preserves the mode but reduces the named entry to no effective access. The
qualification-only connection/domain proxy therefore installs an exact named
ACL immediately before `virDomainCreate`, verifies the expected temporary ACL
mask/mode change, and restores the exact original basic ACL and mode only after
successful lifecycle completion, exact name/UUID/owner/projection/inactive
validation, persistent-domain undefine, and proof that both name and UUID are
absent. An external reactivation makes the inactive-only undefine fail before
restoration, retaining the ACL, held descriptors, domain, and backing tree.
Ambiguous create, launch, or cleanup does the same. The root is never an
automatic `TemporaryDirectory`. This is not permission to weaken production
modes or validators, and public OCI-root dispatch remains disabled.

Every OCI-root disk source also carries an exact per-source libvirt DAC
`relabel=no` contract. The inactive-domain projection requires and digests that
single canonical label for the writable root, stage-1 transport, and every
read-only lower; disappearance, duplication, or any attribute/content change
is definition drift. Cloud-image disks do not inherit this OCI-root policy.
For native qualification only, extensionless content-addressed lowers are
revalidated and copied beneath the owner-only test root as `<sha256>.raw`, and
the test redirects all build/commit/resolve lower-path checks to those same
staged copies. The `.raw` suffix matches the qualified server's
`virt-aa-helper` whitelist and the libvirt raw-disk driver without changing,
relabeling, or granting access to the shared CAS blobs. This is not a portable
AppArmor contract: a production AppArmor export/access boundary remains
unimplemented, so public OCI-root dispatch remains disabled.

The live qualification harness also injects one file-backed serial console
only through its test-local domain XML wrapper. It pre-creates
`console.log` as an owner-only file, requires the exact `path`/`append=on`
source with its single DAC `relabel=no` child and serial target, and grants the
libvirt DAC identity temporary `rw-` access through the same broker. The
no-relabel child is emitted only for an OCI-root file console; the default PTY
and cloud-image builder remain unchanged. A launch exception receives a
binary-safe, 128-KiB-bounded console-tail note before the existing domain/ACL
cleanup runs.
Successful qualification additionally requires the root-transition,
workload-started, and lifecycle-ready-committed stage-1 markers. The production
OCI-root builder and projection carry the no-relabel contract, while the file
path injection and evidence capture remain test-local; public dispatch is
unchanged and disabled.

On the qualified libvirt 10 host, an inactive domain with that file console
also contains one generated `<serial type="file">` peer. It is accepted only
when its source path, `append=on`, DAC no-relabel child, ISA-serial target, and
ISA-serial model exactly mirror the sole file console. Initial post-define
comparison normalizes the generated `serial=1` count only for that authored
file-console form. The stored projection retains the count and console
fingerprint, so later removal, duplication, source rebinding, or structural
change fails closed. A file serial remains forbidden for the default PTY.

Qualification-root deletion similarly binds the expected device/inode through
an open descriptor and a random quarantine rename, then walks and removes
children relative to held directory descriptors while rechecking the root
binding before child mutations. Each child is itself moved to a fresh 128-bit
random quarantine name, checked against its held descriptor, recursively
emptied when it is a directory, and checked again immediately before removal.
Every unlink/rmdir requires both zero link count through the held descriptor
and absence of its quarantine name. Any observed replacement of a public or
quarantine name is left untouched, not renamed back or deleted.
A successful root removal additionally requires the held directory's link
count to become zero and the quarantine name to be absent. This materially
narrows cleanup exposure, but POSIX pathname mutation cannot make the final
rename/unlink atomic with the preceding identity check. A fully hostile process
running as the same host UID could follow random names with inotify and race
that final syscall; the qualification harness does not claim protection from
that threat.
Per-domain AppArmor rules beyond this qualification-only extension adapter are
still an independent host qualification prerequisite; the test must never edit
system policy.
