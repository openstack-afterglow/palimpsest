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

The next production-inert foundation establishes a fresh-exec monitor process
and owner-private local IPC without connecting it to that active-domain lease.
It must run while the parent is single-threaded and before libvirt is imported,
then execs a capability-free child with only the held run-directory FD and a
bootstrap channel inherited. An exact pre-activation binding schema is limited
to run identity, owner UID, plan/stage-1 digests, an explicitly expected
pre-define projection digest, preassigned domain and boot-attempt UUIDs,
lifecycle protocol, and fixed libvirt URI. Its digest, a generation, a
transient nonce, and both process incarnations bind `PREPARED` and `COMMIT`
receipts. COMMIT here means only that the IPC child is authenticated and
serving; it is not guest READY, VM ownership, or a runtime-state transition.

The IPC endpoint is a `0600` filesystem AF_UNIX socket below the held `0700`
run directory, addressed through a short `/proc/self/fd` path and pinned by
device/inode. This was chosen instead of Linux's abstract namespace so stale
socket collisions and replacement remain visible and fail closed. Neither the
socket name nor the nonce is treated as a secret: reciprocal authorization is
Linux `SO_PEERCRED` UID/PID plus host-boot UUID and `/proc` start ticks. Frames
are canonical and bounded. The path-free endpoint receipt is canonical and
serializable but returned only in memory; a live same-owner caller explicitly
handed those bytes can reconnect after exact socket and peer revalidation, but
restart discovery is not implemented. Before COMMIT, parent death/channel
closure or timeout exits and removes the exact socket. The only
commands are DESCRIBE, PING, and shutdown of the inert monitor process itself;
there is no STOP, READY, libvirt, lifecycle-key, or domain-mutation surface.

This resolves only the exec and IPC proof boundary. Because the existing 30E
binding requires an already-active domain ID and post-define projection, using
it after parent-owned `domain.create()` would leave a create-to-exec crash
window. A later slice must instead make the already-execed child own an
immutable pre-activation journal, register libvirt events, define/create the
domain, and publish the exact active binding. Public run/`-d` stays disabled.
That integration must durably publish and directory-fsync the endpoint receipt
before sending COMMIT. Until then, a parent crash after the in-memory COMMIT may
leave a live but undiscoverable child, so 30F makes no daemon-restart claim.
It must also keep `expected_definition_projection_digest` distinct from the
canonical actual projection digest that becomes knowable only after the child
performs libvirt define and reads normalized `XMLDesc`. The child must require
an exact match before promoting the claim into an active binding; 30F performs
no such promotion.

Slice 30G closes that IPC-child discovery window with a child-owned
preactivation journal. It uses the same owner journal and flock pathnames as
30E, but a strict v2 schema with `active_binding: null`; therefore v1 active
ownership and v2 preactivation ownership are mutually exclusive. The child
publishes and fsyncs `claiming` before binding its generation-derived socket,
then durably records `prepared` plus the socket device/inode before PREPARED and
`committed` before COMMITTED. The launcher descriptor-pins and exact-rereads
both records before progressing. Only a nonce digest is journaled; raw nonce,
boot key, MAC, path, and error text remain absent.

Restart discovery begins from the caller-trusted immutable binding, not a
remembered generation. It parses the canonical generation from the journal and
requires an exact live writer incarnation, socket inode, peer credentials,
DESCRIBE response, and unchanged journal. Live precommit and unknown-liveness
records are never mutated. Stale recovery acquires the shared lock, CAS-rereads
the record, publishes `adopting`, and removes only an exact recorded socket
through an `O_PATH`-pinned, generation-and-inode-derived quarantine using
Linux atomic no-clobber rename plus directory fsync. A restart can therefore
finish a quarantine interrupted by a process crash. Replacement or cleanup
ambiguity is preserved and ends in `control-lost`. In particular, `claiming`
has no recorded inode: a leftover pathname cannot be proven to be the child's
socket, so it is preserved as `control-lost` rather than deleted.
Graceful inert-monitor shutdown is `aborting` → exact socket unlink/fsync →
`abandoned`; an absent-socket abandoned record can start a new generation under
the same lock and monotonic revision history.

The IPC child remains an ownership foundation. Public runtime, dispatcher,
CLI, and active-domain lease paths do not import it. The private launch uses
only its immutable binding type in 30H below. This does not provide VM STOP,
READY through IPC, active-binding promotion, create/start/run/`-d`, or Gate 2
activation.

Slice 30H connects the private synchronous launch to an explicit monitor
binding. Preparation runs under the run lock and verifies an existing inactive
domain against the durable definition and re-resolved plan. It records the
actual normalized definition projection and a caller-selected boot attempt.
Launch revalidates all identity fields before allocating a stream and before
activation, and carries the same attempt through READY and TERMINAL. The live
libvirt qualification verifies that attempt in both the receipt and run ledger.

This preparation result is a snapshot, not a monitor ownership capability.
Its expected projection is the observed post-definition digest, not authored
XML's digest. Child-owned define/create, active journal promotion, lifecycle/IPC
event scheduling, public `run -d`, and Gate 2 require subsequent integration.

Slice 30I connects the same held v2 journal lease to the private synchronous
launch. Its `activating` intent is durable before create, and the verified live
domain ID promotes the immutable preactivation identity into `active_binding`.
READY and TERMINAL advance the journal after their durable run-state records.
The live qualification asserts the terminal journal and exact domain/boot
identity, without claiming that its launch runs in the fresh-exec IPC child.

The owner-only journal directory is `monitor-private` beneath the pinned run,
separate from the guest-accessible lifecycle directory. FD identity checks
reject a matching journal held in a different directory. Activation evidence
cannot use inert stale socket cleanup, abandoned transitions, or inert monitor
shutdown. Loss of journal authority prevents destructive launch cleanup;
terminal publication failure preserves an already-durable `exited` state.
Trusted authority transfer and child-owned libvirt/event execution remain
unimplemented, as do public `run -d`, authenticated VM STOP, and Gate 2.

Slice 30J adds an optional launch authority to the fresh-exec monitor. Explicit
inherited FDs bind the selected state/store/run roots and boot artifacts; strict
metadata/profile checks and boot/store verification precede child-owned libvirt
access. The parent must confirm COMMITTED and send a distinct authenticated
activation fence. A non-daemon worker then owns the existing bound, leased
launch while the IPC main thread remains responsive. Journal operations are
serialized, and authority is not released until worker completion.

The live test now qualifies both synchronous and child-owned variants. A clean
launcher exits before the child finishes an actual VM create; PING and live
discovery must still work. The test continues the worker and verifies the
terminal journal's child identity before retiring its transport. The terminal
journal is retained, not abandoned. Active SHUTDOWN is not supported and must
not be confused with authenticated guest STOP.

The child's test-only DAC adapter verifies the exact held broker target and
named-QEMU ACL. Boot copies additionally receive an exact original-owner read
grant so production read-only reopening remains possible after relabeling;
other broker targets retain their single QEMU grant.
Libvirt also relabels the copied kernel/initramfs while the VM
is active: the adapter accepts the exact QEMU owner only with an exact active
domain-instance proof, and the original owner only with an inactive proof after
creation. It rehashes both held boot files and keeps their inode, size, mtime,
link count, mode and ACL checks. Directory timestamps may change through normal
state publication; no arbitrary owner or content change is accepted.
Production authority validation does not relax permissions. Domain definition
still precedes the clean launcher; public foreground/detached dispatch and
local build-to-run Gate 2 remain disabled.

Slice 30K adds private authenticated guest STOP through the live child worker.
The IPC loop queues a single fixed SIGTERM only after durable READY. Duplicate
requests are idempotent and cannot extend its bounded deadline; acceptance is
not proof of delivery or exit. Only the worker accesses the boot key/session,
and every STOP write revalidates the exact domain and retained authority.
Signed TERMINAL feeds the existing durable exit mapping. An accepted STOP's
ambiguous send, EOF or timeout preserves cleanup-required/control-lost and never
uses force destroy as a timeout fallback.

The additional live case runs the checked-in signal-aware workload, waits until
its signal handlers are armed after launcher exit, repeats IPC STOP and PING,
and requires one signed STOP plus the matching TERMINAL with exit 42. Public
stop/run/`-d`, reconnect, production access provisioning and Gate 2 remain gated.

Slice 30L adds private inactive-only cleanup after the original monitor is
proven stale. It holds the existing monitor lock plus the pinned run lock,
keeps the original journal unchanged, and validates the independent binding,
durable definition/handoff, connection URI and exact persistent inactive VM
before undefine. Active/unknown ownership, uncaptured activation, mismatched
name/UUID/owner/projection, and initially missing domains are refused.

A separate run-state cleanup intent supports crash resume. Completion requires
both name and UUID absent; completed replay is absence-only and cannot remove
a reappeared VM. Existing exit/status evidence, sockets, source artifacts and
VM-specific root volumes remain untouched. Last-observed-inactive checks cannot
exclude an external administrator racing libvirt's non-conditional undefine.

The fourth live variant leaves a completed child's socket/journal stale, uses
this private cleanup on its exact inactive definition and repeats it to prove
idempotency and evidence preservation. Only the fixture subsequently retires
its exact socket and temporary tree. Production access provisioning, explicit resource reclamation,
public run/`-d` and Gate 2 remain gated.

Slice 30M adds private retained-root detachment after completed 30L cleanup.
Only a root originally prepared with retention policy `retain` is eligible.
The original monitor must remain stale, the old domain absent, and the exact
preparation/definition/volume generation and durable lower leases unchanged.
Intent and completion preserve the old run's lifecycle evidence. A completed
retention receipt is historical and cannot authorize detaching a newer VM's
attachment. Live qualification includes a second VM boot from the same
writable root under a new run identity.
Its executable exists only in the retained upper layer, inserted by the host
fixture after the first domain is absent and its pending ext4 journal has been
replayed before the offline edit. It is selected by a private process
override. This is a root-reuse proof, not a public command-override feature or
a test of guest-created application data persistence.

Retention deliberately keeps the old run ledger and lower lease set. An
OCI-root disk contains the OverlayFS upper and still needs its immutable lower
graph to boot. The new run must acquire its own lower leases before claiming
the retained disk; automatic retirement of the old pins, standalone disk
export, and removal of the old run are not provided by this boundary.

Slice 30N adds explicit lower-lease handoff to a different prepared run, before
its domain is planned or defined. The successor must own the same root inode
at the exact next attachment generation and hold a complete same-graph lease
set. Both run locks and the volume lock protect this binding; the store holds
both sets' use locks plus digest/index guards while retiring only the old pins.
Intent and completion are written outside digest guards to avoid lock inversion.
Only an existing intent permits exact partial-removal recovery.

Completed replay is historical and requires the old pins to remain absent;
it does not touch a later owner or require the recipient run to survive.
The original retention API refuses once its required old pins have retired.
Callers must finish an incomplete handoff before advancing the successor.
Live qualification now boots the upper-only executable after old-pin retirement,
with the new pins intact and still blocking collection. Disk data, lifecycle
evidence, and the original journal/socket remain outside this reclamation scope.

Slice 30O isolates QEMU-created `io/lifecycle.sock` and the host-precreated
`io/console.log` from the trusted run root. Commit exclusively creates and
fsyncs the directory and console, then binds both inode identities to the
run/domain plan in a trusted ledger receipt. Definition/activation and the
fresh-exec authority reject replaced resources; the prelaunch socket must be
absent. Console bytes and timestamps remain untrusted mutable output.
The I/O guard invalidates inherited authority after fork and does not close a
descriptor reused by the child during subsequent teardown.
I/O identity failures prevent normal lifecycle publication and never authorize
endpoint deletion. Existing exact-domain launch-failure cleanup remains a
separate policy, including the private synchronous path's verified VM cleanup.

Qualification now uses the production per-run console path. It verifies that
only `io` receives named-QEMU directory write access, the trusted run root is
traversal-only for QEMU, and `monitor-private` receives no grant. The retained
root successor has a separate, initially empty console. Domain plan/core
v15/v9 and launch authority v2 reject the old host path contracts; guest
protocol and non-OCI console paths are unchanged.

The [libvirt Unix socket contract](https://libvirt.org/formatdomain.html#unix-domain-socket-client-server)
defines `mode="bind"` as a local server endpoint. The existing
[security-label policy](https://libvirt.org/formatdomain.html#security-label)
is unchanged by this layout isolation.

Slice 30P adds a narrow production ACL boundary for the isolated directory
and console only. An explicit connection supplies the canonical DAC/KVM
principal (root/current owner are rejected). A run-locked durable intent binds
the monitor attempt, plan, I/O inode receipt and full baseline/granted ACLs
before console `rw-`, then directory `-wx` access is applied. Fixed Linux ACL
tools operate on inherited descriptors with sanitized environment; every
full-ACL replacement has exact readback and fsync. No default/extra ACL or
unknown inode is adopted. See the upstream
[setfacl full-ACL and mask contract](https://man7.org/linux/man-pages/man1/setfacl.1.html).

Runtime validation admits the resulting `0730`/`0660` only with the completed
access receipt and exact actual ACLs. Fresh-exec launch authority v3 carries
that receipt and verifies held FDs without acquiring another run lock.
No-grant paths retain owner-only validation. Interrupted intent/revocation
resumes only from the recorded grant or baseline; replay is read-only and
revoked access is terminal. Recovery requires completed 30L cleanup, original
STALE writer, exclusive existing journal lock and both domain name/UUID absent,
and removes directory access before console access. It never removes files,
socket/journal evidence, disks, leases or lifecycle outcomes. Early-abandon
revocation without a monitor journal remains unsupported.

The stale-cleanup child boot uses this production I/O grant with both targets
excluded from the fixture broker and no test I/O metadata adapter. Actual
ACLs are checked during activity; LIVE-writer revoke refusal after terminal,
restoration/replay after 30L, and console inode/bytes plus journal/socket
preservation are required. The natural-exit, STOP and retained-root successor
boots remain explicitly test-adapted. Ancestors, BOOT/shared artifacts, root
disks and relabel handling are fixture-only even for the production-I/O boot.
This does not promote the whole test broker to production; public dispatch,
full provisioning, endpoint removal and Gate 2 remain disabled.

Slice 30Q extends that same private access receipt to the VM-exclusive run
root. The only added QEMU right is named-user traversal `--x` with the exact
mask, producing `0710`; QEMU still cannot list or mutate entries, read the
owner-only ledger, or enter `monitor-private`. Grant makes the children usable
first (`console -> io -> run`), while recovery blocks traversal first
(`run -> io -> console`). Receipt v2 and fresh-exec authority v4 bind all three
targets. The old private v1/v3 formats are rejected without implicit migration.

The run target pins its device/inode, owner/group, directory type and complete
ACL but deliberately does not freeze link count, size or timestamps, since the
owner may create `monitor-private` before launch. Descendant link-count rules
remain unchanged. Partial grant/revoke resumes only from the exact ordered ACL
prefixes saved by its durable intent; all desired target FDs are fsynced before
an interrupted operation completes. Completed replay does not write ACLs,
target FDs or the ledger. Every external command/domain check is followed by
held and visible identity plus durable-member validation.

The stale-cleanup live child now removes run root, I/O directory and console
from the test ACL broker and uses production validation for all three. It
checks exact active grants, LIVE refusal, 30L+STALE recovery to `0700/0700/0600`
and preservation of ledger, monitor journal/socket, console and root volume.
The shared parent chain is still fixture-provided. So are BOOT/root-disk/lower
access and libvirt relabel behavior, and the other four actual boots continue
to use their explicit qualification adapters. This remains short of complete
production filesystem provisioning and does not activate public dispatch.

Slice 30R adds a separate shared traversal registry for exact `state` and
`runs` only. Per-run memberships bind run UUID/access UUID and immutable target
identities, not a mutable global revision. The first member grants `runs -> state`
search; the last departing member restores `state -> runs`. Non-final departure
does not write or fsync either ancestor and must leave the surviving VM valid.
An empty epoch and per-run departure evidence remain durable. Normal leave drops
its global member only after run-left is durable. Crash-orphaned left entries
remain read-only on replay, pending future explicit repair. No join repairs an
unrelated run. A 1 MiB preflight reserves pending/completed registry space and
headroom for the largest active member's future leave intent.
A permanent enrollment marker prevents a deleted registry from being mistaken
for legacy unmanaged state; only the original explicit join may recover a valid
marker-only baseline intent. Partial marker writes are preserved and refused.
The private flow is
`grant_oci_runtime_access -> join_oci_shared_traversal -> prepare/spawn`, followed
after terminal STALE 30L cleanup by `revoke_oci_runtime_access -> leave_oci_shared_traversal`.

Launch authority v5 requires an active membership for its own run when the
namespace is managed, while preserving runtime-access receipt v2. Old v4 frames
are rejected. Root initialization serializes with membership changes and verifies
the managed full ACL instead of closing it through `chmod`. Only unmanaged roots
retain legacy owner-held, non-symlink FD-based mode700 repair under the global lock;
initial enrollment additionally requires full baseline
ACLs and refuses ambiguous preexisting active ledgers. Preparation before the
per-run grant remains owner-only and does not constitute launch admission.

Portable acceptance covers two-member lifetime, exact partial prefixes,
ACL/fsync/ledger crash recovery, read-only replay, concurrent joins/leaves,
path/identity and full-ACL drift, initialization preservation and fresh-exec
membership checks. Native validation promotes the existing stale-cleanup child
to one real managed member and checks first grant, initialization preservation
and final restore with Linux ACLs; simultaneous two-VM native lifetime remains
future work. The registry
does not supply an external trust anchor against wholesale same-UID offline
replacement of state plus all evidence. Store/CAS, external parent paths, BOOT,
root disks, relabeling, public dispatch and Gate 2 remain outside this segment.

Slice 30S adds generation-bound access for the exact writable root raw file.
An immutable run receipt and a durable per-volume fence bind the original
monitor attempt, attached owner/generation, lower graph, filesystem UUID,
file inode/size and QEMU principal. Enrollment evidence makes missing permission
history a refusal rather than an ungranted fallback. Permission transitions
hold the existing run lock followed by the volume lock.
Unmanaged lifecycle transitions also verify owner-only file permissions, which
prevents losing both access-evidence files from releasing a granted disk. This
does not authenticate history after same-UID removal of all enrollment evidence
and restoration of the private baseline.

Grant publishes intent before the exact named-QEMU `rw-` ACL and completes
only after readback/fsync. Revoke requires the completed 30L cleanup, original
terminal STALE journal writer and both domain identifiers absent. Retain,
release/delete and successor claim require restored permissions. The retained
volume keeps its bytes and lower graph; a new generation has its own access
authority. Fresh-exec pins a managed root FD and validates the current fence,
while allowing guest content and timestamp changes.

The native stale-cleanup child removes the root raw file from both fixture
brokers, alongside the five 30R targets. It verifies product grants and
restoration, LIVE-writer refusal and preserved root bytes during revoke, then
boots the retained root through the existing successor qualification. That
successor still uses fixture access. Root-volume parent traversal, BOOT and
shared immutable exports, relabeling and public `run`/`run -d` remain subsequent
work; this slice does not activate Gate 2 or a two-VM native shared lifetime.

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
