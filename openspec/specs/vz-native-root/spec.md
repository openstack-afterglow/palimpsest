# vz-native-root Specification

## Purpose

Define the reviewed exact-base and actual-root contracts for local Lima VZ arm64 development environments. A requirement or source implementation is not itself live qualification: implementation, tests and actual profile-specific evidence are recorded separately in the canonical Goal and development handoff. Local approval does not authorize cloud, device, issuer, publication or deployment effects.

## Requirements

### Requirement: Cloud userspace and OCI userspace have distinct authority

The implementation SHALL reuse the canonical image manifest's `application` and `oci-root` distinction. An application root SHALL use the exact immutable projection of the cloud root selected explicitly by its Dockerfile ancestry. An OCI root SHALL preserve the complete selected OCI graph, including original base layers and repeated occurrences. A cloud image used for kernel or platform boot assets SHALL NOT become an unrelated userspace lower of an OCI root.

Ordinary supported Dockerfiles SHALL remain eligible through OCI-root mode without FROM rewriting. Imported registry images SHALL retain their verified graph and actual source identity without inventing unavailable Dockerfile genealogy. Unsupported architecture or OS SHALL be rejected before workload VM effects; graph support alone SHALL NOT widen a runtime backend or native grant.

#### Scenario: Unrelated FROM and cloud runtime base
- **WHEN** a Dockerfile uses an ordinary registry FROM unrelated to a supplied cloud root
- **THEN** its inherited OCI base SHALL NOT be discarded or silently rebased
- **AND** an explicitly requested application binding SHALL be rejected unless exact projection ancestry is established.

#### Scenario: External OCI root uses cloud boot assets
- **WHEN** the selected OCI image needs a compatible kernel/initrd from a cloud boot profile
- **THEN** every original OCI filesystem occurrence remains in the workload lower chain
- **AND** that cloud userspace is not added as another lower.

### Requirement: Application ancestry binds the exact root projection

The projection SHALL bind the original disk digest, format and architecture; selected root filesystem and separate-mount policy; versioned projection recipe; projection OCI root, manifest, config, ordered descriptors and DiffIDs; and derived immutable root descriptor. The runtime root SHALL match that same projection authority. Disk identity or matching OS release alone SHALL NOT prove equivalence.

Before extracting an application suffix, the implementation SHALL bind the explicit final-stage input, selected target and solve evidence and verify the output's projection prefix descriptors and DiffIDs. Prefix similarity, context availability or a cache key alone SHALL NOT prove ancestry. Recompression SHALL require a separately proven equivalence contract or be rejected. A valid base-only result SHALL retain its final runtime/configuration identity without manufacturing a dummy layer.

#### Scenario: Same disk digest but changed projection
- **WHEN** root selection, projection recipe, derived root or final-stage ancestry differs from the pinned application base
- **THEN** the application binding SHALL be rejected
- **AND** no existing VM upper is reused against the changed root.

#### Scenario: No filesystem suffix
- **WHEN** the final image contains only the verified projection prefix but changes its image configuration
- **THEN** the base-only image remains representable with its actual final configuration and identity
- **AND** no fake filesystem layer is introduced.

### Requirement: Artifact transport is explicit and read-only

The selected candidate is direct kernel/initrd boot on Lima VZ with an explicitly read-only virtiofs artifact share. Selected tool version, resolved host location, distinct guest mount point and actual Lima tag derivation SHALL be qualified before use. Guest paths SHALL NOT be assumed equal to host paths. Named writable VZ disks or guest read-only bind mounts SHALL NOT be advertised as read-only shared block attachments.

Host custody SHALL cover share attachment and the entire backing-file/mount lifetime. An initial hash or retained payload FD not consumed by Lima/VZ SHALL NOT alone establish custody. Guest-channel read-only enforcement SHALL NOT be represented as protection against a malicious host writer or as attestation.

#### Scenario: Missing or changed early-boot input
- **WHEN** the share, artifact identity, kernel feature or guest device relationship is absent, changed or ambiguous
- **THEN** root assembly SHALL fail closed with bounded evidence
- **AND** no carrier running state or alternate writable transport is reported as native-root success.

#### M1-14 selected transport and custody design

The first profile selects Lima2.2.1 VZ, `plain: false`, `mountType: virtiofs`,
an explicit canonical host export path, a distinct explicit guest mount point,
and `writable: false`. Validate the effective/default-filled configuration, not
only the rendered YAML; reject extra/unapproved shares, changed paths/writability
or plain mode before VM effects. Disable and validate Rosetta sharing explicitly,
including `vmOpts.vz.rosetta`, not only `mounts[]`. Do not edit global Lima defaults
or borrow another user's VM/home/keys to satisfy this contract.

| Boundary | Binding and required behavior |
|---|---|
| Original source → host payload | Revalidate source qualification, ordered typed receipts and canonical OCIStore bytes before runtime preparation. Linux worker output must be imported/revalidated through canonical source/store APIs; copying a worker DB/store identity/lease does not rehome authority. |
| Canonical store → export projection | A private allowlisted projection contains only the selected unique SquashFS payloads, named by digest. Its key binds the store identity, ordered physical digest/size chain and export contract. It is a derived transport view, not another blob authority; same-chain VMs may share its exact backing files. Account any copy overhead explicitly. |
| Export custody | Existing run-owned ordered durable lease sets retain canonical bytes. Private owner/no-follow directory identities and single-link regular payload identities, atomic completed projection publication, effective Lima config and active run references protect the path-based export throughout attachment/mount/resume. An unrelated FD/initial hash is insufficient; cooperative host mutation/GC must honor custody. No malicious same-UID/host-writer guarantee is claimed. |
| Host directory → VZ device | Compute `lima-` plus the first8 SHA-256 bytes of `resolved_location + ":" + resolved_mountPoint`. The selected VZ source passes `!writable` to `NewSharedDirectory` and attaches the resulting virtiofs device before VM start. A YAML label is not an arbitrary guest device tag. |
| VZ device → guest path | Early platform init mounts that exact tag at `/.palimpsest/lower-input`, verifies each expected file hash/size, and mounts each occurrence read-only at `/.palimpsest/lowers/<ordinal>`. Original ordinal order is retained separately; OverlayFS `lowerdir` uses highest ordinal first. No host path is used as a guest path. |
| Root transition/lifetime | Keep the private platform/input/lower/state mounts at the same guest-absolute namespace across PID1 root transition. Stop preserves upper and lower custody for resume. Release transport references/leases only after exact owned VM absence and all consumers are accounted for; incomplete/uncertain cleanup holds custody and blocks new cases and resume. |

Immediately before every VM create, first start and resume start, revalidate the
retained projection directory's device/inode/owner/no-follow identity, durable
publication-completion binding, exact payload allowlist, regular-file identities/
hashes/sizes, ordered leases and run references. Hold cooperative custody through
attachment and retain it for the backing-file/mount lifetime. Missing, recreated,
replaced, incomplete or ambiguously retained projections SHALL fail before that
provider operation; resume SHALL only reopen retained authority, never silently
rebuild/adopt it. Unknown cleanup also blocks resume.

This is an explicit native pre-effect gate: Lima2.2.1's VZ share constructor calls
`MkdirAll` for a missing source directory before `NewSharedDirectory`. Unchanged
YAML/tag is therefore not evidence that the retained projection still exists,
and Lima auto-creation or later guest hashing SHALL NOT replace this host gate.

The reserved platform namespace must be checked for image collisions before
effects; silently hiding user image data is not allowed. Host source mounts are
separate explicit opt-ins, never a default artifact-share substitute, and cannot
overlap protected platform/control/lower/upper or pseudo-filesystem paths.
Guest-channel write denial must be demonstrated later against the actual VZ
share, not inferred from SquashFS immutability or a guest read-only bind alone.
VZ-specific transport bindings SHALL preserve canonical receipt/lease authority
without weakening or treating existing QEMU-DAC boot/lower export receipts as VZ
receipts. Their distinct-QEMU-UID and KVM preparation guards remain unchanged.

Source basis is Lima tag `v2.2.1`, annotated tag
`bd99f2bcb55c37b18a33ce8a6e289d98ed27b85d`, commit
`27bc4c4b101e27994e01e36566a0f2091bbca507`:
[schema](https://github.com/lima-vm/lima/blob/27bc4c4b101e27994e01e36566a0f2091bbca507/pkg/limatype/lima_yaml.go),
[path/tag/plain defaults](https://github.com/lima-vm/lima/blob/27bc4c4b101e27994e01e36566a0f2091bbca507/pkg/limayaml/defaults.go),
[VZ boot/disks/shares](https://github.com/lima-vm/lima/blob/27bc4c4b101e27994e01e36566a0f2091bbca507/pkg/driver/vz/vm_darwin.go).
Installed `limactl version2.2.1` and actual `validate --fill` preserve the
explicit distinct paths/read-only share; a plain-mode contrast removes mounts.
Five real verified derived payloads (35491840bytes) supplied schema input data,
not a native root. The observed tag is `lima-4f213fc473c0c7df` for that exact
owned proof path. Kernel/initrd, early mount/write denial, actual root and access
remain separate M1-15/16/17 proofs. Source inspection is not binary attestation.


### Requirement: Persistent upper has an authoritative first-use and resume binding

Initialization and resume SHALL bind the authoritative VM/run, exact owned host disk attachment, expected guest filesystem identity and immutable base/ordered chain. Upper/work SHALL share a suitable private guest-local filesystem. First initialization SHALL require explicit first-use authorization for that owned storage. Resume SHALL match the retained binding without formatting or clearing populated upper/work. Labels or enumeration alone SHALL NOT establish identity; unsupported VZ serial assumptions SHALL NOT be introduced.

#### Scenario: Replaced or misattached filesystem
- **WHEN** the only labeled filesystem is a replacement, belongs to another attachment or does not match the retained run/root binding
- **THEN** it SHALL NOT be initialized or used as upper/work
- **AND** new resource operations halt rather than guessing ownership.

#### Scenario: Stop and start an existing development VM
- **WHEN** the exact owned VM resumes with matching disk and immutable chain
- **THEN** populated upper/work and user files SHALL remain intact
- **AND** the implementation SHALL NOT copy a fresh-only upper policy or force-format storage.

#### M1-14 selected upper attachment design

Use a per-run private raw ext4 backing disk through the canonical run/root-volume
ownership boundary and Lima's named-disk physical operations. Ordinary user data
volumes retain their independent lifecycle; they are not disposable VM uppers.
Prepare/format only an explicitly authorized newly created empty owned file
before attachment. Configure `additionalDisks[].format: false` from the first
start; the conventional first-boot `format: true`/forced-stop rewrite is excluded.
The custom initramfs PID1 never executes `cidata.TEMPLATE.d/boot.Linux/05-lima-disks.sh`.
It scans `/sys/class/block` for the single ext4 filesystem UUID bound in the boot plan,
mounts that device directly at `/.palimpsest/state`, and writes its own
`/run/lima-ssh-ready` and `/run/lima-boot-done` only after the verified root/network/access predicate.
Whole-disk raw ext4 is intentional: Lima host `store.InspectDisk` accepts raw via `qcow2reader`,
and the guest UUID scan finds `/dev/vdb` without requiring a `/dev/vdb1` partition.
Conventional `/mnt/lima-<name>` second mounts, `growpart`, and `resize2fs` from that script never run
against the live upper; their absence is by design, not a suppressed provisioning step.
Before first start and every resume, verify the effective/persisted
`additionalDisks` configuration names the exact owned upper and still has
`format:false`, alongside the physical/in-use binding. A post-boot sealing check
or correct rendered YAML alone is insufficient.

Host binding includes canonical run UUID, backend disk name, exact private
directory/file device+inode, raw format/size, selected Lima attachment and
in-use owner. Guest binding combines exactly one expected filesystem UUID with
a prelaid run/root-plan/chain-bound nonce record. Disk order/label/serial alone
is insufficient. The selected source attaches primary disk, optional installer,
named additional disks, then read-only cidata; actual guest presentation still
requires live corroboration, not a guessed `/dev/vdb` assumption.

Initialize upper/work on that same filesystem only with one-shot first-use
authorization and an unambiguous empty/prepared state. Persist/fsync the matching
initialization binding; resume requires it and never formats, clears populated
data, or repairs partial/unknown initialization automatically. Replaced host
identity, duplicate/missing guest UUID, wrong nonce/chain or uncertain previous
initialization fails closed. Verify aggregate allocation of every VM-attached
disk/image, including carrier, upper, user data, installer and cidata, against the
current per-VM bound. Physical accounting also includes preparation temporaries/
copies and retained platform/export assets; the disk bound is not independently
granted to each attachment.


### Requirement: Native root and platform access are independently proven

Root assembly SHALL transition PID1's actual root and retain needed pseudo-filesystem, artifact and state mounts. Main workload and public SSH/console sessions SHALL use that root. Verification SHALL compare mountinfo, intended lower order and `/` versus `/proc/1/root` filesystem/device/inode identity. `/opt/layers/merged`, app-only chroot, a container in a carrier, an open port or idle PID1 SHALL NOT substitute for this evidence.

Platform init/supervision, networking and authenticated access SHALL be provided and qualified when absent from the image. Their binaries, identities, commit exclusions and storage accounting SHALL have explicit boundaries. Arbitrary OCI userspace SHALL NOT be assumed to include systemd, bash, sudo, SSH or Lima guestagent. Platform libraries SHALL NOT silently replace unrelated image userspace. The selected ABI2 Ubuntu24 ARM profile uses the private non-root bootstrap/real privileged-helper boundary below; source100 component102 and actual native-root/access105–116 qualify that profile, not arbitrary OCI userspace or M3 capture.

#### Scenario: Image lacks normal Lima dependencies
- **WHEN** an OCI image lacks the services and commands required by Lima's readiness protocol
- **THEN** the implementation SHALL use an explicitly designed and qualified platform-access boundary or reject execution before claiming readiness
- **AND** no no-op readiness response or chroot-only application path is accepted.

#### Retained stop and generic-host boundaries

After authenticated quiescence and accepted poweroff, the original bounded
observation window MAY include Lima's intermediate `Broken` state. Only exact
owned `Stopped`, zero/absent provider PIDs, complete related-process/PID/socket/
autostart absence, exclusive canonical upper and unchanged source/boot authority
SHALL establish stopped state. No second stop, signal or force fallback is granted.

An original ordinary stop-incomplete HOLD MAY settle by observation only under
persisted authenticated stop-ready/session-zero/output-complete evidence and
exact run/plan/start-attempt/marker bindings. The final resource reload SHALL
reject its own contrary provider observation; complete stopped/actor/upper proof
SHALL be repeated after it before durable adoption or marker release.
Persist verified settlement before exact marker release, retain interrupted
recovery, and reject replacement or unknown journals. Start SHALL validate the
closed journal and reject unfinished recovery before any mutation, never erase
unknown evidence to admit a boot.

A clean retained dev exit whose exact provider has naturally stopped MAY
reconcile to stopped before a new boot under the same full authority. This is
disk-state persistence, not RAM/process restoration. Unrelated reuse of an old
SSH port SHALL NOT manufacture stop/removal custody; an occupied port SHALL
reject start admission before a new attempt/provider effect, without alternate
ports or a false start HOLD.

Generic host observation SHALL classify native or ambiguous native-looking Lima
identities conservatively without granting ownership. They SHALL NOT gain
generic force removal, lifecycle controls, shell/exec, metrics or raw serial
access, even through a stale cloud-image row. Recheck physical classification at
every generic transport/removal boundary. Managed native access SHALL use its
canonical dispatcher/custody and pinned main console; a genuinely owned
cloud-image ledger retains its original exact-identity admission, including a
reserved-looking name. Names alone never prove ownership.

Live VM inventory SHALL preserve canonical `runtime_status` independently from
physical VM `status`; main process exit is not evidence that a native dev guest
has stopped. Fresh successful pinned OCI-root/Lima-VZ reconciliation is the
physical baseline, requiring the current full UUID, committed plan, derived
native instance, recognized main state, pinned home identity and no HOLD.
Missing ambient Lima tools or a different ambient `LIMA_HOME` SHALL NOT revoke
that independently validated canonical access.

Coalesce a supplementary provider alias only with the same home device/inode,
full UUID, backend, native kind and derived provider name. Copy physical status
and resources only, never SSH credentials/endpoints, guest addresses or publication
data. Ambient discovery SHALL NOT upgrade pinned stopped state; same-home
collisions SHALL revoke eligibility. Missing/stale/ambiguous/HOLD/Broken canonical
reconciliation SHALL NOT enable guest access. Runtime inspect retains original
main-exit truth; transport-time ownership/custody revalidation remains required.

Native reconciliation SHALL project inventory and its physical alias from one
validated backend observation. Do not reread a concurrently replaced status file
to reconstruct that same observation or place private snapshots/paths in public
summary DTOs. This remains point-in-time discovery, not reusable transport
authority; subsequent access SHALL revalidate its complete expected identity.

Live native observation SHALL use one request-local allowance of at most five
seconds of actual contended mutex acquisition across run-name locks, preflight,
resolution and refresh. Ordinary provider/file work SHALL NOT consume it, and
the allowance SHALL NOT grow or reset between stages of one pass. It is not a
total request deadline. Every authoritative mutable native ledger read SHALL
cooperate with its existing run-name lock. Internal offline discovery and
missing-root inspection SHALL remain nonmutating and SHALL NOT initialize state.

Concurrent calls in one process MAY join only an ongoing selected-root native
reconciliation registered before mutable-ledger/name-lock waits. A completed
flight SHALL be removed immediately; no TTL, recent-result cache, cross-process
sharing or reusable capability/transport authority is introduced. A joiner SHALL
charge its wait to its own remaining allowance, securely reread the full record
and lifecycle/state generation, and freshly verify host configuration, native
custody/HOLD and root bindings before publishing a private observer snapshot.
Shared snapshots SHALL be deeply immutable. Coordination/run/custody locks
SHALL NOT surround observers, waits or provider callbacks.

Leader failure/cancellation, join timeout and superseded observations SHALL NOT
cause provider or metadata retries. Safe known native rows remain stale and
issue no observer/alias grant; if neither caller obtained secure discovery,
named errors need not invent rows. Legacy/KVM observations remain independent,
including on a failed join. A same-identity advance only in status/revision or
captured native status/logs MAY report busy; changed identity/plan/native custody
or removal SHALL NOT be downgraded to a pure mutex exception. Concurrent run
directory membership changes can invalidate native sharing, not already
independent legacy results. This remains point-in-time discovery.

Pure namespace acquisition timeout SHALL raise `RuntimeBusyError`; the named
lock timeout SHALL retain the more specific `RunLockTimeoutError`. Only the
named timeout permits enumeration's first strict FD/ctime/UID/private-mode/
single-link degraded read. A busy row SHALL remain stale/unobserved, report
`runtime-busy`, issue no capability report and lend no observer/alias/guest or
lifecycle authority. Never reread failed bytes or retry a provider command.
Active actors, HOLDs, unsafe bindings and uncertain operations remain distinct
fail-closed failures. After acquisition, revalidate visible/held directory and
lock identity and all prior guards without pinning legitimate mutable ctime.

Native log admission SHALL resolve, refresh and pin under separate bounded
lock windows, never hold a mutex across refresh callbacks, yielded events or
sleep. A routing-only immutable metadata hint MAY preserve the legacy console
path, whose fresh descriptor reader SHALL require full record equality; it SHALL
NOT authorize native access. A failed hint SHALL never supply bytes or authority,
and a proven-absent name SHALL not create lock metadata. Native follow EOF reads
retain strict private expected-directory guards; legacy per-file guards remain.
An admitted follower MAY skip only a typed pure mutex-busy observation, keep its
original pinned FD and continue existing EOF polling with one new shared bounded
allowance per poll. Snapshot/initial admission remain fail-closed. Integrity,
actor/resource HOLD and uncertain provider failures SHALL terminate the stream,
not be replayed. Cancellation can wait at most the current mutex allowance;
already-admitted provider/actor work retains its independent limits.

Existing-run observations and sessions acquire run-name before namespace custody.
Creation and native source intake SHALL preserve preflight/source guards under
namespace custody but acquire any run-name lock try-once, before run-directory
effects; a busy committed-deletion quarantine remains for later normal cleanup.
Do not move initialization before preflight or weaken source revalidation.
The existing registry initialization flock is unchanged and can still block.
Default lifecycle/preflight namespace acquisition remains fail-fast.

Guest `exec --timeout` SHALL govern only guest execution, never elapsed host
preflight/load/status work or a shortened forwarded guest timer. Native status
actors retain their independent fixed 40-second bound and real host grant/
configuration expiry remains enforced. No timeout or contention grants force,
uncertain request replay, or authority over another runtime.

Dashboard native exec SHALL retain the same non-daemon drain and registry
admission/shutdown owner as KVM exec. Foreground close, timeout and drawer
disconnect detach only; underlying session cleanup stays on the retained worker.
Completion protocol selection SHALL use the canonical OCI-root dispatch record:
KVM requires an original mailbox ACK by default; native Lima-VZ explicitly does
not have that ACK. Native process completion SHALL NOT fabricate an ACK or count
as unconfirmed. Missing/failed required ACK observation SHALL count unconfirmed,
while always removing/notifying the completed registry entry. Normal dashboard
shutdown waits for actual underlying drain/cleanup, including native custody.

The dashboard SHALL start VM metrics samples on an independent five-second
cadence, not five seconds after inventory/log completion. At most one sample
per inspector is in flight; overlapping requests join that promise without
queuing. Auto VM log reads occur only on the Logs tab. Closing, hiding or
pausing the inspector/page stops automatic sampling. Source/run/boot identity,
positive uptime/wall-clock intervals, monotonic counters and the existing
15-second valid-pair bound remain enforced; gaps are not fabricated as zero.
Mutations SHALL await already-started client inventory/metrics requests before
rechecking eligibility. Client abort settlement is not proof of server-side
drain; server authority remains final. Ordinary console use SHALL NOT pause
metrics solely to turn a contention failure into an apparent acceptance.



#### M1-15 selected platform component boundary

Use a first-party POSIX-C ARM64 init/supervisor/session helper, statically linked
with a pinned Linux ARM64 musl toolchain. Reuse canonical image/process, root-
transition, bounded control/output and ownership contracts; do not relabel or
numerically patch the existing x86 raw-syscall/seccomp/ELF as an ARM implementation.
This is a privileged VM development profile, not a claim of identical KVM
stage-1 sandboxing, hostile-guest-root isolation or attestation.

| Component | Selected role and provenance gate |
|---|---|
| Boot assets | Pin the Ubuntu ARM64 kernel, required matching modules and custom platform initramfs plus build recipe/toolchain/config/source hashes. Existing builder6.8.0-147 configuration has virtiofs/OverlayFS/vsock as modules; include verified dependency closure before boot. A generic Ubuntu initrd is not the native-root assembler. |
| Platform executables | Pinned static BusyBox, static Dropbear and first-party ARM64 helpers; the selected Lima2.2.1 guestagent is a separately pinned platform asset. Validate ELF machine183, static linkage, exact bytes/recipe and licenses; never borrow the packaged x86 stage1 or mix platform libraries into user image library paths. Missing assets/toolchain fail before VM effects. |
| PID1/root | Early init authenticates the run/boot plan, source qualification, exact share/layers and bound private filesystem, assembles the ordered root and transitions PID1 itself. Preserve private platform/pseudo/input/lower/state mounts at stable guest paths. Main and user access verify the same `/` and `/proc/1/root` filesystem identity before executing. |
| Main execution | Use canonical `oci_process` argv/env/User/workdir/StopSignal/override and size/account bounds, direct exec rather than shell reconstruction, bounded output and explicit exit/signal/OOM state. Resolve main/CLI-exec accounts only against the verified workload root, never control-root NSS. Root readiness and main state are different facts. Later dev/prod/restart/session requirements remain separately exercised acceptance. |
| Platform state | `/.palimpsest` is a reserved private mount namespace, collision-checked before effects. Binaries/control/access state remain outside image lowers and user upper payload. Mutable host keys and identities live in per-VM private disk state outside upper/work; volatile runtime/readiness/session files live in private tmpfs. |

Initially the dedicated builder had actual ARM64 BusyBox-static1.36.1 and Lima
guestagent2.2.1 but no gcc/musl-gcc/Dropbear/qemu-img. Approved guest-only
preparation subsequently downloaded/hashed35 signed-index APT packages
(50.5MB), installed them offline without upgrades/removals/service restarts,
and observed gcc13.3/musl1.2.4/qemu8.2.2 with clean dpkg audit. No first-party
helper/native initramfs is built by that installation. Pin the complete final
asset recipe before VM effects. The selected SSH source is Dropbear2026.94,
commit `28216cd9af822732a1549b78621c2ea9a76a0fe5`; the primary release manifest
lists tarball SHA256
`e098034a843699200c8c977a991fff73159735bf795d5f72ef672c41a6b1ae81`.
[Static build instructions](https://github.com/mkj/dropbear/blob/28216cd9af822732a1549b78621c2ea9a76a0fe5/INSTALL.md)
and [server/key-hook semantics](https://github.com/mkj/dropbear/blob/28216cd9af822732a1549b78621c2ea9a76a0fe5/manpages/dropbear.8)
are source evidence, not an authenticated native service. The primary guest
tarball GET returned403; the upstream-listed public mirror supplied identical
pinned bytes without credentials/protection bypass. A bounded offline musl build
produced a506576byte ARM64 static binary, SHA256
`7ecd073d57f4fb68bcbbbab1f75ea4a1b4cd860318df9b3508d95f4515c0b866`,
machine183/no interpreter/no dynamic segment, and actual version2026.94 passed.
No key, SSH service, broker/handoff or VZ root proof ran. The HTTPS source
manifest was observed; no local signature or broader binary attestation is claimed.

The selected build disables server password authentication, PAM, plugins and
syslog at compile time. Its inetd argv therefore omits `-E` and `-s`; retain
key-class/forwarding/host-key restrictions. Builder AND loader MUST qualify
the actual Dropbear bytes against the exact506576byte digest above, not merely
matching version strings or a self-consistent receipt. An alternative build
requires explicit component qualification before acceptance. This keeps omitted
runtime switches from enabling password authentication in an alternate binary;
it does not itself prove an authenticated service.


#### M1-15 authenticated access and genuine Lima readiness

Run static SSH services in a private platform control root with its own minimal
account/shell/key files, proc/dev access and bound seed. This root isolates
platform dependencies; it is not the workload root or a hostile-root sandbox.
Never overwrite user `/etc/passwd`, `/etc/ssh`, shell binaries or libraries to
satisfy Lima. Password/keyboard-interactive login, inherited daemon environment,
agent/X11 forwarding and unrequested forwarding are disabled. Use explicit
per-VM host keys and separate owned control/user public-key classes; never load
host `~/.ssh` keys or forward its agent.

Generate a unique host key during authorized offline preparation of the new
private disk, store it only in that VM's private platform-state sibling and pin
its public key/fingerprint in the canonical run binding before first start.
Resume requires the same key binding; missing/changed state is not permission
to regenerate it. Shared lowers/public initramfs never contain private host or
client keys. Native user SSH uses a private known-hosts file and explicit
identities/host-key validation; do not trust an unauthenticated first connection.
Control and user client private keys stay outside the guest and image.

Authorized-key forced-command hooks run a first-party session helper. User SSH
and interactive CLI console enter the verified PID1 root before a real shell;
structured CLI exec preserves the canonical argv/context without shell joining.
Administrative SSH/console identity is explicitly VM-owner root, distinct from
the main process's preserved OCI User. CLI exec retains its canonical run
execution context. No public user access path may return the platform jail.
All native CLI access uses the user transport, not conventional `limactl shell`
into the control root.
Derive control/user class from the fixed trusted authorized-key hook argument,
not client-controlled `SSH_ORIGINAL_COMMAND` or a relay address. Do not override
per-key hooks with daemon `-c`. Control keys may disable PTYs, but preserve real
user PTY support; indiscriminate `restrict` would disable it.

Select strict SSH-over-vsock for the first profile. The selected Lima2.2.1
[usernet source](https://github.com/lima-vm/lima/blob/27bc4c4b101e27994e01e36566a0f2091bbca507/pkg/networks/usernet/client.go)
first resolves DHCP and waits for genuine TCP22 before attempting its vsock22
forwarder. Provide a real authenticated bootstrap TCP service and an ARM
vsock22 broker. Stock Dropbear `-i` is not an AF_VSOCK/AF_UNIX address adapter:
its session code obtains numeric socket peer names and the selected musl
resolver supports AF_INET/AF_INET6. For each accepted authorized host-vsock
connection, select a bounded private AF_INET loopback connected-pair adapter:
bind loopback port0, connect/accept the expected local pair, close the temporary
listener, and pass only the connected AF_INET descriptor to Dropbear `-i`.
Relay opaque SSH bytes bidirectionally with bounded connections/buffers and
lifetime-fenced EOF/cleanup. Do not clip valid quiet user sessions or production
following with an unrelated daemon idle timeout or one-hour limit.
End-to-end SSH authentication and pinned host keys
remain in Dropbear. No permanent alternate TCP listener is reserved. Keep actual
vsock peer context separate from Dropbear's loopback transport address; do not
fabricate `SSH_CONNECTION` or authorize by that address.

After the host independently verifies the exact owned vsock-forwarder event and
authenticated root status, switch to vsock-only and confirm bootstrap TCP22 is
closed before admitting the original main process. Platform SSH does not
permanently steal the workload's TCP22. No fake port, unqualified NAT fallback or
main launch before handoff is accepted; missing vsock/DHCP/relay/transport support
is an explicit qualification failure.

The control root supplies genuine execution of the selected Lima requirement
scripts, its bound `param.env` and current instance ID. Lima2.2.1's actual
`ValidateTemplateArgs` rejects bootstrap username `root` and UID0; YAML validation
alone is not a Create proof. Use the real private `plmctl` UID/GID1000 account,
`/home/plmctl` and `/bin/bash` shim. Ordinary private shell commands execute with
all real/effective/saved IDs1000. Its root-owned non-writable home/`.ssh` directories
are traversable and its public authorized-key file is readable to1000 because
Dropbear reads it after temporarily assuming the authenticating identity.

The distinct root platform-control and root user-admin keys keep their forced
classes. User admin enters the actual image root; canonical main/structured exec
keeps the image User/env/cwd/argv. Never fake UID1000 metadata over a UID0 account.
The private first-party sudo helper performs actual root elevation and trusted
literal argv execution with clean environment, not canned outputs. Its setuid bit
is effective only on the explicitly suid-enabled private jail self-bind; the
outer platform and host transports remain nosuid/nodev. Verify real caller IDs,
actual PID1-bound jail root device/inode, root-owned immutable executable/ancestors,
NNP0 and active guest Yama `ptrace_scope>=1` before admission. Require
`CONFIG_SECURITY_YAMA=y`; writing/readback of the guest policy proves activation,
not that the build config alone does. Both the jail-root and Yama guards are
needed against ordinary same-UID image processes; no hostile-root/CAP_SYS_PTRACE
security parity is claimed. Do not change host kernel security policy.

The selected requirements execute genuinely, including nonroot readable current-IID
markers and actual sudo access to `param.env`. Unsupported forms fail explicitly.
Platform binaries and all these account/privilege files remain outside the user root.
Readiness markers are emitted only after verified PID1 root, exact lower/private-disk binding,
network/access services and current seed/session predicate; also run a real
native-root readiness probe. SSH availability, Lima running state and main
readiness are never equivalent. Host-key validation uses the pre-start owned
boot/control/key binding; user SSH pins that exact expected public host key.

Control/guestagent/readiness connections are not user activity. Track actual
user SSH and CLI console/exec sessions through lifetime-fenced hooks; unknown
observation is not a proven last-session close. The later five-minute dev grace,
reconnect races, prod continuation and explicit-stop precedence must use this
same session/supervisor authority, not host-side TCP counts.

#### M1-15 provider preparation and pinned file authority

The platform semantic ABI is `palimpsest.vz-platform.v2`. Builder, loader and
authenticated status checks use that exact ABI and reject older root0/seconds-only
bundles before workload VM effects; no compatibility fallback is provided.

Boot-identity readonly injections MUST retain the validated original target FD.
On the supported Linux6.8 profile, clone a source-pinned detached bind, add
readonly/nosuid/nodev without clearing inherited noexec or other restrictions,
then attach directly to that target FD. Do not remount through its pre-bind
procfs magic link: it still identifies the covered inode/mount, not the attached
bind root. Reject source/target symlinks at their validation boundaries and keep
covered-target inode provenance. Writable binds and the separate intentional
control-jail setuid self-remount remain independent.

Preparation HOLD diagnostics MAY be persisted only as private evidence. Keep
stderr out of public exception strings; retain at most64KiB plus observed byte
count, truncation/EOF and observed returncode. Drain immediately available stderr
before premature stdout EOF while preserving the1MiB total-output cap, custody
markers, original locks and no-force/no-replay behavior. A root-run isolated
interpreter import check must use explicit `-B`; `-I` ignores the bytecode
environment variable. Never relax bytecode ownership to accommodate staging.


The actual `limactl create` CLI calls `instance.Create` **and** `instance.Prepare`.
It prepares the primary disk, kernel, initrd and command line but does not start
the Linux VZ machine. Cloud config exists at creation; the host agent generates
CIDATA during actual start before driver VM start. Do not infer the CLI behavior
from `instance.Create` alone. After a successful authorized new create whose name
and directory were absent, verify the exact owned Stopped instance and the actual
provider-created raw disk/boot-copy bytes, modes, identities and configured sizes
against the pinned carrier/assets/plan before first start. Pin kernel/initrd0400
and command-line0644; the public boot-only primary may be provider-produced0600
or0644 under the owned0700 instance directory. Record its exact initial mode and
require it unchanged later; the private upper policy is unchanged. Bind producer
Lima CLI and read-only qemu comparison tools separately instead of labelling it a
first-party conversion. Missing, partial, changed or failed outputs remain held;
neither overwrite nor adopt them through a retry. Resume reopens retained boot
authority and never regenerates it.

Before create/start/resume, pin the installed stock guestagent selected by the
resolved Lima prefix, bounded-decode it and require exact digest/size equality
with the verified initramfs guestagent. Missing stock resources fail without
download. The closed Linux/VZ provider profile permits a conservative32MiB
non-agent CIDATA envelope; select a64/128MiB reservation that contains this plus
the actual bounded agent. Include it with primary/upper/all attachments in the
same32GiB total, not an independent per-disk grant. Clear all eight lower-/uppercase
proxy fields explicitly; `propagateProxyEnv:false` alone does not suppress macOS
system proxy import. The original OCI environment remains separate.

Keep actual provider configurations as private pinned files. Ledger metadata
stores paths/digests/full stamps, never embedded rendered/filled dictionaries
containing policy-forbidden keys such as `passwordlessSudo`. Verify the generated
file immediately before Create and persisted `lima.yaml` identity on later use.
Do not weaken the canonical state secret guard or disguise credential material.

Unknown actors/creation/deletion remain a HOLD. Exact-owned failed-prestart
cleanup is a separate, fail-closed reconciliation: prove the same resource,
no unsettled actor and authoritative Stopped/never-started state; perform ordinary
delete once, then prove absence before releasing root/export/lease authority and
removing only that resource's HOLD. It never authorizes adoption, resume or a
new consumer while cleanup is uncertain.
An orphan actor journal blocks ordinary mutation, probing and cleanup. A healthy
live SSH session retains its kernel lock and does not by itself block a pinned
read-only version probe; failed-prestart cleanup admits no session uncertainty.
Before invoking ordinary Lima delete, freshly validate the exact canonical root
and require its owned no-follow directory to contain only `datadisk`. The pinned
provider's normal delete may unlock configured disks even without a force flag;
a post-delete check alone cannot prove that a foreign consumer lock was absent.
Do not remove that lock, normalize unknown entries or invoke provider cleanup
when any actor/consumer ownership remains uncertain.
Runtime start/resume/admission keeps full carrier/config/source continuity.
Canonical RM may separate destruction eligibility from historical carrier ctime
only after the complete failed-prestart checks above. Require an eight-integer
original carrier stamp, identical digest/dev/inode/UID/full mode/link count/
size/mtime and complete current-read stability, including ctime. Never repin
the original carrier. Admit only the original `launch-incomplete` resource marker
paired with ledger `launch-incomplete` or `retained-authority-changed`; no other
reason pair, partial deletion, actor or changed authority qualifies. All other
bindings remain exact, the next consumer stays blocked until absence/release,
and this is neither a benign-history claim nor operational permission.

Failed-start cleanup is a different scope, never the failed-prestart classifier.
Require original failed/launch HOLD, recorded bound start attempt, ready boot
copies and no authenticated handoff/access publication. A private exact
run/actor/network/deadline context admits only canonical STOP/RM; normal runtime
entrypoints do not inherit it. Retain all eight historical carrier stamp fields.
Concrete same-UID Darwin PID birth/parent/group/executable/argv observations
must match the original actor; inaccessible or changing reads are not absence.
Require exact provider inventory/no named-network or autostart effect, no related
actors and exact owned upper-link custody. Persist each stop/delete attempt
before its effect; no replay, timeout signal or force fallback. Normal stop is
not guaranteed to settle. Delete requires original actor/related processes,
PID/active endpoints and published port absent, provider Stopped, then fresh
provider/directory absence before releases and original HOLD removal last.
The closed inactive default-gvisor filesystem-entry exception below is not
authority for active, configured/shared-network or unknown endpoints.
Missing zero-valued provider PID fields count as zero only after concrete
process absence; explicit null or unknown values never qualify. Filesystem
absence requires ENOENT, not suppressed permission/I/O errors. This cooperative
custody policy is not protection against malicious independent same-UID races.
Historical ctime failure remains a blocker until a separately explicit owner
amendment for the particular operation; no boot/adoption authority is restored.

A successful ordinary stop recorded as `returned`, with no termination history,
MAY settle through fresh complete observation without replaying the provider
stop. The supported Lima2.2.1 unconfigured per-instance network may retain only
`default_ep.sock`/`default_fd.sock`: caller-owned, single-link0755 sockets, both
STREAM and DGRAM connections refused, unchanged directory/leaf/deadline checks
and timestamps bounded by original start→before-stop observation. Record exact
current-after-normal-stop pins honestly; do not invent earlier inode witnesses.
Persist stronger absence and reject replacement/reappearance. All original
input/actor/upper/namespace/port guards still apply. Only ordinary provider delete
removes these entries; no manual unlink, signal fallback or cleanup replay is
introduced. The separately scoped forced ha/vz endpoint policy is unchanged.


#### M1-15 capture exclusions and capacity accounting

Mandatory capture excludes the complete platform/control/access namespace,
private disk siblings, injected keys/authorized identities/tokens, per-VM machine
ID/hostname, seed/cloud metadata, resolver/network/DHCP state and ephemeral pseudo
mounts. Runtime identity/resolver files use explicit private mounts, not writes
to immutable lowers. Capture operates on a consistent upper copy and never
traverses those mounts or modifies the live upper; user ignore re-inclusion
cannot override mandatory separation. Lower-baked identity hiding/regeneration
still requires the later M3 proof, not merely removing an upper file.

For the supported Ubuntu24 profile, use this explicit injection map:

| Generated state | Runtime placement / capture treatment |
|---|---|
| Tools, control jail, keys, seed/plan, readiness, DHCP leases | Reserved `/.palimpsest` mounts; persistent private-state siblings outside upper/work and volatile private tmpfs. Exclude actual generated mounts/siblings and their tracked upper placeholders from the capture copy. Shared payload projections/public initramfs never carry per-VM secrets or private plan environments. |
| Machine identity | Private per-VM bind files at `/etc/machine-id` and applicable `/var/lib/dbus/machine-id`; record actual canonical target and generated placeholder. Do not reuse another VM's identity; lower masking/regeneration remains M3-proofed. |
| Hostname | Kernel hostname plus a private `/etc/hostname` bind when the supported image uses it; exclude only generated instance state/placeholder, not arbitrary user configuration directories. |
| Resolver | Private `/etc/resolv.conf` bind with explicit DNS/lease policy. Preserve original image/user bytes underneath; keep generated lease/runtime-network files in the platform namespace rather than blanket-excluding `/etc/netplan`, `/etc/network` or user application configuration. |
| Pseudo/runtime mounts | Record actual proc/sys/dev and any explicitly selected ephemeral mounts. Exclude mounted runtime content, not inferred broad path trees; `/tmp` is not implicitly disposable merely because of its name. |

Resolve bind targets by bounded descriptor-rooted lookup within the verified
workload root, never the host/control root. Reject escapes, magic links,
ambiguous/type-changed targets or unsupported symlink forms; explicitly admitted
in-root symlinks must resolve to the verified canonical target. Create a missing
mountpoint only as a recorded generated upper placeholder and filter only that
placeholder in a capture copy. Exclusions are not blanket deletion of `/etc`,
`/root`, user network settings, package databases or other persistent user data.

Report separately original compressed/derived chain bytes and deduplicated
physical bytes, user-upper allocated/logical usage, private platform persistent
state, volatile RAM, kernel/initrd/modules/tools, carrier/upper/seed disks and
export/preparation copy overhead. Excluding platform data from a commit or user
upper accounting never excludes its real physical storage from the host budget.
Bind actual tmpfs/RAM and private-state growth limits to the existing resource
envelope, measure temporary overlap at peak, and report platform siblings as a
breakdown of their raw-disk consumption rather than adding that disk twice.
Preserve stopped upper and platform identity for the same VM; a new VM generates
new identities and never inherits another VM's injected keys.

Shutdown uses authenticated supervisor requests, child quiescence and filesystem
sync before normal guest poweroff. Missing acknowledgement, failed cleanup or
ownership uncertainty holds resources; no forced-stop/SIGKILL workaround.
These are selected design requirements only. Assets, root transition, genuine
SSH/console, strict transport handoff, capture and lifecycle behavior require
their own M1-16 onward implementation and live evidence.

### Requirement: Commit captures a stopped, read-only upper as an OCI layer

M3 selects the following contract; it is design plus CPU-level behavior until a separately approved native run proves it. A commit SHALL be admitted only for a run that is stopped (graceful, supervisor-acknowledged shutdown) and whose upper, platform-state sibling and lower export are all still owned by that run. The guest SHALL boot a platform-only capture mode: the upper is mounted read-only, no overlay, workload, control jail with user keys, or network access to the workload is started, and the guest streams the upper through the authenticated control session into host staging. A running run SHALL NOT be captured by default. Running capture is an explicit opt-in that requires a bounded filesystem freeze with a guaranteed thaw on every exit path, and SHALL be rejected when freeze or thaw cannot be acknowledged.

The live upper SHALL NOT be modified, cleaned, renamed or truncated by capture. Every capture failure (admission, streaming, conversion, exclusion, digest, storage, cancellation) SHALL leave the source upper, its platform state, the lower export and the run record exactly as before capture and SHALL leave no catalog entry. Staging output is created exclusively, verified, then registered atomically; partial staging is removed only by the capture that created it.

Capture converts overlay semantics to an OCI layer: a character device 0/0 becomes a `.wh.<name>` entry, an opaque directory (`trusted.overlay.opaque=y`) becomes a `.wh..wh..opq` marker, regular files, directories, symlinks, hardlinks, ownership, modes and non-overlay xattrs are preserved. Overlay `redirect`, `metacopy` and other `trusted.overlay.*` metadata other than `opaque` SHALL be rejected, as SHALL sockets and device nodes other than whiteouts. Entry paths SHALL be normalized, relative, free of `..` and bounded; ordering and mtimes are deterministic. The layer's uncompressed digest is its DiffID.

Selection applies in this order: (1) the mandatory exclusion set, which always wins; (2) `.plmignore` patterns evaluated relative to the guest root `/` with Docker-ignore syntax, including `!` re-inclusion, applied only to what mandatory exclusion left; (3) default include. A `!` rule SHALL NOT re-include a mandatory exclusion. `.plmignore` for commit is read from the guest root `/.plmignore` inside the captured upper or lower view; it differs from the build `.plmignore`, which is relative to the build context. `.plmignore` affects only files first created or modified in this upper; it SHALL NOT remove, and SHALL NOT be reported as removing, files already present in a parent layer. Mounted external volumes, virtiofs shares and tmpfs are not part of the upper and SHALL NOT be captured.

The mandatory exclusion set consists of the reserved `/.palimpsest` namespace; generated placeholders for the per-VM `/etc/machine-id`, `/var/lib/dbus/machine-id`, `/etc/hostname` and `/etc/resolv.conf` (and only a placeholder recorded as generated, not user content); `/etc/ssh/ssh_host_*`; `/var/lib/cloud`; and DHCP lease state. Identity paths that exist in a parent layer and would reappear in a new VM (SSH host keys, cloud-init state) SHALL be hidden by an explicit whiteout in the committed layer; files the platform binds over at runtime need no whiteout. Logs, caches and package databases are included by default. A policy table fixes this explicitly: `/var/log`, `/var/cache` and `/tmp` content is included only when written to the upper outside a tmpfs mount, and package databases (`/var/lib/dpkg`) are always included so installed packages stay consistent.

The committed result SHALL be a normal catalog image: a new OCI manifest whose layers are the parent chain followed by the new layer, an image config that inherits the parent's runtime configuration (Entrypoint, Cmd, Env, User, WorkingDir) and appends the new DiffID and history, and a parent link to the source image and run. It SHALL be runnable and publishable through the existing materialize, run and `published push`/`pull` paths without a new Hub schema.

#### Scenario: Stopped run is captured without touching its upper
- **WHEN** a stopped run's upper contains a modified file, a deleted lower file, an opaque directory and a generated machine-id placeholder
- **THEN** the committed layer contains the modified file, a `.wh.` entry for the deleted file, an opaque marker, and no machine-id placeholder
- **AND** the source upper is byte-identical before and after

#### Scenario: Re-inclusion cannot defeat mandatory exclusion
- **WHEN** `.plmignore` contains `!etc/ssh/ssh_host_ed25519_key`
- **THEN** that key is not captured and, if present in a parent layer, is hidden by a whiteout

#### Scenario: Failure preserves the source
- **WHEN** conversion rejects a `trusted.overlay.redirect` entry
- **THEN** no staging output or catalog entry remains and the upper is unchanged

### Requirement: Evidence and operating authority remain scoped

The initial proof profile is Ubuntu24.04/aarch64 on Lima VZ, not universal image support. Source-backed LinuxBootLoader and share APIs SHALL remain distinct from installed behavior and native proof. ARM assembly, kernel/filesystem support, projection fidelity, persistent resume, real SSH and two-consumer upper isolation SHALL be verified separately. Existing x86 stage1 and fresh-upper Nova helpers SHALL NOT be presented as ready-made ARM persistent-root implementations.

Every operation SHALL obey the exact local approval's expiry, cumulative attempts/downloads, resource bounds, namespace ownership and graceful cleanup. Ambiguous creation/deletion, changed ownership or failed cleanup SHALL stop new cases. Review approval SHALL NOT widen operational authority.

### Requirement: One canonical manifest has explicit root-binding intent

M1-02 retains `palimpsest.image-manifest.v1` with an optional strictly typed `base_root` field. Unbound records SHALL omit it and its conditional compatibility key entirely; explicit null SHALL be rejected. Existing OCI-root and descriptive legacy-application manifest digests, node IDs and read/public output SHALL remain unchanged. Normalization's keyword-only `require_root_binding` SHALL have no default. Stored reads, reuse policies, digest and public projection choose false; constructors, new-write/worker admission, native projection and local planning choose true. Hub stored-read and admission wrappers SHALL be distinct. Known reused-validator failures SHALL become ValueError, not uncaught service errors.

#### Scenario: Historical unqualified application is listed
- **WHEN** a stored application descriptor lacks the new root binding
- **THEN** descriptive reads preserve its existing content identity without qualifying it for a new write or native run
- **AND** it neither breaks a list page nor enters an execution fallback.

### Requirement: Local graph support does not widen publication or native authority

Shared graph/manifest support may admit linux/arm64 locally only through a complete consumer cutover. Throughout M1 the common SDK/Hub publication profile check SHALL require linux/amd64, OCI-root and absent `base_root`, before SDK uploads and Hub registration. Existing worker, native and standalone carrier gates SHALL remain explicit. M2 may enable application/arm64 publication only together with source qualification, correct physical-payload architecture/accounting and complete consumer migration. Any changed package still requires separate native package/resource approval; historical grants are not rewritten or carried to it.

#### Scenario: Local arm64 image is sent to current publication
- **WHEN** a local arm64 or bound-application descriptor is passed to SDK publish/push/extension or Hub admission during M1
- **THEN** the shared publication profile check rejects it before upload or registration
- **AND** no x86_64 physical payload label or native grant is reused to claim support.

### Requirement: Cloud-root projection identity is typed and complete

The `base_root` profile SHALL contain exactly the following normalized fields. Unknown fields, invalid strings, boolean integers and contradictory identities SHALL be rejected under existing manifest byte bounds.

| Field | Binding |
|---|---|
| `schema` | `palimpsest.cloud-root-projection.v1` |
| `platform` | Selected linux/amd64 or linux/arm64, matching the image and decoded config |
| `source_disk` | SHA256 digest, positive file size, separate positive virtual size, raw/qcow2 format with no external backing |
| `source_filesystem` | GPT partition number/UUID, ext4 UUID/type and bounded byte offset/length inside virtual disk; no mutable device/host path as identity |
| `policy` | `cloud-root-ext4-v1`: offline single ext4 root at `/`, supported file metadata preserved, no live pseudo-filesystems, EFI partition explicitly outside userspace; other separate userspace filesystems/encryption/unsupported forms rejected |
| `toolchain_digest` | Qualified projection/conversion recipe and toolchain content identity |
| `oci` | Strict manifest/config descriptors, one canonical root tar occurrence and matching DiffID for the selected platform; not an ambiguous index |
| `squashfs` | Positive strict SquashFS descriptor derived from the same canonical root recipe |
| `id` | Derived domain-separated canonical SHA256 over every other normalized field; supplied contradictions rejected |

Actual source qualification SHALL reject ext4 requiring journal recovery, recorded errors, unsupported features or inconsistent state. It SHALL NOT replay a journal, repair or change the source to qualify it. Structural metadata or a JSON clean flag SHALL NOT establish this condition.

### Requirement: Qualification is independent of self-declared metadata

When application publication is enabled, its shared authoritative admission and local run planning SHALL require independently established qualification covering every normalized projection field and its ID. Unchanged SourceCAS/materialization does not already qualify a cloud-disk projection. Actual source/producer evidence SHALL bind disk bytes/format/virtual size, real root selection/state, recipe/toolchain, decoded OCI/config, tar DiffID and same-root SquashFS derivation. Possession of a SquashFS file or caller-supplied receipt JSON SHALL NOT self-promote qualification. Private evidence/custody SHALL remain outside public descriptive metadata.

#### Scenario: Internally consistent asserted projection
- **WHEN** a caller supplies matching descriptor hashes but lacks independently established cloud-root qualification
- **THEN** shape normalization alone SHALL NOT admit the image as a publishable/executable application
- **AND** existing membership, visibility and source-byte checks remain required.

### Requirement: Complete lower graph and one compatibility authority

A new application graph SHALL contain the real projected cloud-root node first and each verified suffix occurrence afterward. Base-only and metadata-only results retain that real base node and actual final configuration, never a dummy layer. The first payload SHALL match `base_root.squashfs`; `base_image_digest` SHALL equal the source disk digest. Conditional application compatibility SHALL include `base_root.id`. The shared manifest authority SHALL export the compatibility used by Hub storage, reuse comparison and graph output. OCI-root hashing and the standalone carrier formula remain unchanged; the carrier rejects application, `base_root` and non-amd64.

SDK extension SHALL fetch the authorized public base and apply the shared profile/binding check before any upload. Public projection includes normalized `base_root` only when present and does not reconstruct or rehash private runtime from a redacted view; the Hub rechecks authoritative state.

### Requirement: Manifest cutover is atomic and legacy physical lanes are not a fallback

The cutover SHALL cover the shared normalizer/digest/compatibility/constructors/projections; SDK push/publish/extension; Hub stored-read/admission/API/graph/registration and compatibility rows; worker result/validation/completion; native grant/snapshot/delivery and carrier copy; local planner; verbatim database-copy preservation; affected docs and behavioral tests. Fixed0.3.11 source-baseline vectors, not newly recomputed expected values, SHALL prove OCI-root and legacy application identity/read stability and actual carrier acceptance/tamper rejection. This does not claim a formal0.3.11 release or deployment.

M1-02 removes unbound application manifest writes, not an imaginary manifest executor. Existing physical-layer Hub `/instances`, Lima/KVM `/opt/layers/merged` and CloudEnv lanes remain unchanged by this contract, never consume `base_root`, and SHALL NOT serve as a fallback for the new Goal root path. Their later replacement/removal needs its own source review. No deployed SQL, historical data or grant is silently rewritten.


### Requirement: Actual OS qualification is distinct from execution admission

The implemented source qualifier SHALL read the effective OS file from the
complete pinned original graph through existing SourceCAS leases and staged
changesets, with bounded metadata-only overlay/link resolution and origin-payload
replay. It SHALL NOT execute image contents, infer identity from a tag, merge
lower-priority release data or treat descriptive JSON as qualification authority.
Its initial source-profile selection is exactly Ubuntu24.04/linux-arm64/lima-vz/
direct-linux, not cloud-root projection equivalence or verified vendor identity.

Acquisition SHALL bound the complete plain-tar carrier before staging and logical
layout metadata/distinct selected blobs before promotion, including warm CAS.
Reader and shared-normalizer entry/metadata/decompression/path/link/deadline
bounds SHALL apply; accounting SHALL NOT be advertised as an RSS/PAX total.
Source-only CLI initialization SHALL preserve unrelated runtime/quarantine state,
avoid run-name locks and permission repair, and leave missing config trees absent.

The qualifier SHALL explicitly describe source-only scope. This component alone
SHALL NOT complete M1-04 or grant execution. Its native launch consumer SHALL
independently revalidate and bind actual graph/OS/profile evidence to the exact
prepared root and boot inputs before materialization, run reservation and VM
launch, with no force/risk bypass, legacy-runtime fallback or publication/native-
grant widening. Source CAS/config filesystem initialization is not VM admission.

### Requirement: Generic filesystem conversion does not widen native authority

The existing hard materializer and occurrence store MAY accept validated
acquisition snapshots for exactly Linux amd64/arm64. They SHALL preserve every
original ordered descriptor/DiffID and process/config identity, verify the same
source/CAS/recipe and retain native amd64 canonical identities and worker wire.
Repeated occurrences SHALL NOT be silently collapsed into a new root image.
The explicit ARM `oci materialize` path SHALL produce filesystem receipts only,
not an execution capability or durable runtime lease.

Legacy KVM intent admission and persisted-root recovery SHALL reject ARM before
lease, root-volume or workload-VM effects. Actual VZ preparation SHALL still
independently revalidate OS/profile/source and bind exact root/boot inputs;
neither a warm conversion nor matching OS alone completes M1-04.


### Requirement: Contextual immutable layer fidelity

SquashFS SHALL store filesystem payload/metadata/control entries; the separate
manifest SHALL preserve the complete original graph, ordered source descriptors,
DiffIDs, process configuration and occurrence receipts. Neither artifact alone
SHALL be treated as native execution admission or a flattened-chain fallback.

Canonical lower-only marker effects SHALL precede ordinary members regardless
of marker archive order. Same-layer content SHALL survive those effects.
Omitted directory/root attributes SHALL inherit only from surviving effective
preceding directories; explicit attributes SHALL win, and deletion/type barriers
SHALL prevent lower-child resurrection and obsolete metadata inheritance.
Generated opacity SHALL NOT propagate into later implicit directories; admitted
ordinary xattrs, including `user.overlay.*`, SHALL remain intact.

New derived recipes and root-specific cache views SHALL bind exact ordered
preceding compressed descriptors and DiffIDs, not unrelated future/config data.
Historical parentless recipes/leases SHALL remain readable but SHALL NOT be
reused for new contextual materialization or mint graph-inconsistent occurrences.
Warm workers SHALL authenticate source graph/prefix and current semantic
contracts before lookup. Cold replay SHALL bound resources/deadlines and retain
no payload capability from a closed preceding layer. Source/staging entry and
unknown teardown attribution SHALL remain distinct.

### Requirement: One local relational catalog behind state and inventory

M1 selects Python's standard-library SQLite, one private
`StatePaths.state / "catalog-v1" / "metadata.sqlite3"` per selected local root.
The same private directory SHALL contain the owner-bound writer lock; existing
state/shared-traversal root permissions SHALL NOT be repaired or tightened.
`state.py` SHALL own path/root selection and `inventory.py` SHALL own ingestion
and query entrypoints shared by CLI and web. A second application store, ORM,
daemon, provider connection or default-root scan SHALL NOT be introduced.

The catalog SHALL own durable local discovery registrations and a transactional
read model of existing metadata, not payload bytes or execution authority.
Existing owner/state journals, source/derived CAS records, tags, retained-source/
package records, leases and deletion guards SHALL retain their current authority.
Build progress/logs SHALL remain in existing build directories. The catalog
SHALL NOT authorize launch, rebase, deletion, lease recovery, publication, foreign
resource ownership or project attribution. Hub remains authoritative after
publication; copying this DB SHALL NOT publish or authorize another client.

The schema SHALL contain:

| Relation | Identity and facts |
|---|---|
| catalog metadata | schema version, root/catalog UUID, committed observation revision/time and collection warnings/completeness |
| images | verified source-image identity and original structured reference, root/manifest/config descriptors, platform and known base kind/digest; absent ancestry stays absent |
| image layer occurrences | `(image_id, ordinal)` primary key, compressed media type/digest/size and DiffID; repeated byte digests remain separate occurrences |
| derived occurrences | image/ordinal plus recipe digest, SquashFS digest/size, original source binding and store identity; a partial prefix never becomes a complete image |
| builds and build-image links | existing build ID, engine/status, known base/ordered parents, recipe/output identities and source-record digest; registration before final build success stays distinct |
| build provenance | existing recipe hash, pre-solve lookup key/context/Dockerfile hashes and explicitly typed canonical semantic source-record digest; absent historical fields remain null |
| build labels | build ID plus output label; not a durable tag/CAS binding |
| aliases | canonical existing artifact tag or retained package reference, target identity and source-record binding; no invented alias from an output label |
| run-image observations | canonical run UUID, name/backend, ledger revision/status and known image/artifact links; name reuse and best-effort project-name joins grant no authority |

Descriptors, ordered recipes and runtime/config identity SHALL reuse existing
typed validators. The DB SHALL store no injected keys/tokens or copied protected
runtime state; config descriptors reference original CAS bytes rather than
duplicating potentially secret process environments. Null ancestry SHALL NOT be
filled from OS similarity, an arbitrary cloud image or presentation metadata.

### Requirement: Explicit cross-authority consistency and migration

M1-12 SHALL extract the current inventory collectors for reuse, not establish a
second parser/matcher. Local inventory queries MAY refresh the private metadata
read model; they SHALL NOT initialize unrelated roots, retry run quarantines,
download sources or call live VM reconciliation for that refresh. Metadata-cache
writes SHALL be documented separately from runtime-read-only behavior. Queries
against an absent uninitialized state SHALL leave it absent.
An existing valid selected root MAY gain only this private catalog subtree;
refresh SHALL NOT call `init_resolved_roots()` or create a missing config tree.
Every observation SHALL identify capture scope/time and warnings rather than
claim an atomic snapshot of independently mutating authorities.
`store catalog` SHALL read the persisted frame without implicit refresh;
`store catalog --refresh` SHALL explicitly collect and commit a new frame.
Catalog/artifact envelopes SHALL expose this observation. Existing build list/
detail response shapes remain unchanged; consumers needing whole-frame
completeness SHALL use the catalog observation rather than infer it from a
successful list request or an empty list.


Collectors SHALL finish authoritative reads before a SQLite write transaction.
No SQLite transaction SHALL be held while acquiring run-name, artifact, lease,
digest or provider locks. Catalog writers SHALL be serialized with an owner-bound
no-follow private lock; normal JSON/CAS writers SHALL NOT call into the DB while
holding their existing locks. Completed public build/materialize flows SHALL
record metadata after their existing durable commits and released guards.
Readers SHALL see a complete committed observation, never half of a new ordered
chain. Invalid authoritative entries SHALL produce explicit warnings/incompleteness,
not a claim that an image is complete or an artifact is unreferenced.
Admission SHALL isolate invalid entries before SQL while retaining unrelated
valid facts. Incomplete frames SHALL conservatively disable all artifact-deletion
previews without replacing independent actual deletion guards. The known
`builds/outputs` transport namespace SHALL be validated as a directory, not
misclassified as a build ledger; malformed aliases/files and unknown entries
remain incomplete. Final artifact admission SHALL follow safety-flag updates so
an oversized rejected row cannot poison the entire observation.

A canonical failed reservation with missing or null `vz_native` SHALL remain
discoverable as having no committed native plan, preserving its immutable
identity/status/revision without inventing source or resource links. Non-null
malformed committed metadata SHALL remain invalid and fail-visible. Missing plan
metadata SHALL NOT prove provider absence, permit replay, relax a HOLD or grant
start/cleanup authority: reservation failure handling can replace an earlier
payload. Lifecycle recovery SHALL still independently prove exact ownership and
resource state through its existing authority.

Canonical v1 preparation metadata without a plan or committed plan digest MAY
remain discoverable. Wrong schema, lost committed plan/digest, explicit null plan
or malformed self-hashed plan SHALL NOT masquerade as unprepared metadata.
Committed native source links SHALL agree across canonical descriptors, source
CAS membership, the existing recomputed snapshot-binding digest, qualification,
top-level image/binding fields and boot plan. This is pure metadata validation,
not blob rehash/decompression, payload proof or execution authority. Network
inventory SHALL derive OCI run identities from canonical owner-bound ledgers;
an observed provider alias SHALL NOT overwrite that UUID's network projection.




Existing ledgers and payloads SHALL be preserved. First catalog creation SHALL
import validated selected-root facts only; later refreshes SHALL preserve verified
image registrations and atomically replace observational rows. JSON/CAS and SQL
are not one crash-atomic transaction: failure after a durable producer commit
SHALL preserve that commit and report metadata synchronization failure, without
replaying a build/create or rewriting it as a failed workload. A later explicit
refresh MAY reconcile those retained facts.

The SQLite file/parent/lock and journal handling SHALL reject unsafe owners,
permissions, symlinks, multiple links and replaced identities. Use bounded busy
waiting, foreign keys, a versioned application identity, full synchronization
and a rollback journal on the selected local filesystem; WAL/network-shared
multi-host writing is not part of M1. Corrupt or unknown-schema databases SHALL
fail visibly, never silently fall back to a competing JSON query path or delete
source data. Same-UID adversarial mutation is not a sandbox guarantee.
Parent-path aliases MAY be canonicalized without following the selected root
leaf; existing shared-traversal state modes SHALL NOT be tightened. A catchable
fresh-initialization failure SHALL remove only newly created pinned DB/journal
files after closing. An unknown existing empty file, process-death/power-loss
residue or pre-return file-creation failure SHALL NOT trigger automatic adoption
or rebuilding; crash-atomic first publication is not claimed.


State relocation SHALL include a closed, consistent DB under catalog-writer
quiescence, preserve existing no-runs/no-projects restrictions and revalidate
destination root/source bindings. `--keep-source` creates independent catalogs,
not a shared authority or transferable lease. Ordinary reads/initialization
SHALL NOT perform an operational migration of another root.
Immutable reference/descriptors/order SHALL survive relocation. Destination
catalog identity SHALL be independent, copied derivations invalidated, and
destination CAS metadata plus source bytes verified before source-only rebinding.
Unchanged source-only registration SHALL NOT downgrade an existing verified
partial/complete graph. These facts do not transfer leases or confer runnability.


### Requirement: Local catalog acceptance is separate from VZ execution

M1-11 completion requires the source/caller/authority map, this transition
contract and a real bounded SQLite persistence/transaction check. M1-12 requires
implemented integration and separate CLI processes observing actual build,
base/ordered chain, tag/image and exact run linkage through the shared inventory
contract. A synthetic schema check SHALL NOT complete M1-12 or invent a VZ run.

Source31 component evidence:1202 focused pass/11skips, actual default-output
Dockerfile/COPY/C-compile build, five ordered cold materializations, independent
persisted CLI reads, and real read-only web image/build detail/log/provenance
observations. The build/source graph has no actual VZ run link yet. Primary
`spec.md` therefore keeps M1-12 unchecked; older M1-11 engine-only evidence is not
substituted for product or runtime acceptance.

