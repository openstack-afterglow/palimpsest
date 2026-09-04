# Palimpsest guest stage-1 consumer

`init.c` is a freestanding Linux x86_64 PID 1. It uses raw syscalls and embeds
its own SHA-256 and canonical JSON validation; it has no libc, dynamic loader,
or system-header dependency. It authenticates the stage-1 transport, then
opens and authenticates the complete root/lower block-device set by role,
serial, read-only state, exact size and stable identity. It verifies the root
ext4 identity and geometry, then every
lower's SquashFS v4 structure and whole-device image digest. Live PID 1 mounts
the authenticated block FDs at deterministic staging paths, assembles
OverlayFS, moves devtmpfs, sysfs, and proc into that tree, then moves the
OverlayFS mount onto `/` and enters it with `chroot(2)`. This is an
initramfs-safe switch-root choreography, not a `pivot_root(2)` call. It then
decodes the authenticated process contract, forks the admitted image process
into its own process group, confirms `execve(2)` through a close-on-exec error
pipe, forwards an allow-listed signal set through `signalfd`, and reaps all
children with `wait4(2)`.

The executable subset accepts canonical numeric or image account names. PID 1
reads only bounded, root-owned, not group/other-writable, no-follow regular
`/etc/passwd` and `/etc/group` files; named matches must be unique. An omitted
group uses the matching passwd primary GID, while a numeric UID absent from
passwd uses Docker's GID 0 fallback. Explicit numeric groups bypass group-file
lookup. Supplementary groups intentionally remain empty: this is a restricted
security subset, not Docker's image group-membership expansion. The child gets
the authenticated image environment plus the fixed container default `PATH`
when absent. After credential drop and `chdir`, PID 1 performs shell-free
`execve` candidate search; argv containing `/` is direct, and an empty PATH
element explicitly means the workload cwd.

New OCI-root volumes are formatted with a closed ext4 feature allow-list and
fixed geometry rather than host `mke2fs.conf` defaults, then verified before
publication. Retained legacy volumes may omit `metadata_csum`; when it is
present, checksum type 1 (CRC32C) and the primary-superblock checksum are exact.

The reproducible build is:

```sh
scripts/build_oci_guest_init.sh
```

The build runs offline and read-only as the invoking UID/GID with fixed locale,
timezone, home and `SOURCE_DATE_EPOCH`. Its compiler is the linux/amd64 manifest
of GCC 14.3.0 Bookworm, pinned as:

```text
docker.io/library/gcc@sha256:a689e29bc3adf4663ef9a141d23081252764d1319c63f591a027bd6fd676f4c1
```

The linker output is sealed by `scripts/seal_static_elf.py`: the seal rejects
dynamic, interpreter, writable-executable, malformed and executable-stack
segments, truncates to the complete program-header/load extent, and removes
the section-header table. Normal package and initramfs construction reads the
already packaged `assets/oci-stage1-init.x86_64`; Docker and a compiler are not
runtime dependencies. Exact source, recipe, seal, toolchain and ELF digests are
bound by the initramfs manifest.

## Fixture ABI

The same ELF supports `--fixture-v1 ROOT` and `--fixture-v2 ROOT` only when it is not PID 1. A
non-PID1 invocation without that exact fixture ABI exits with usage status and
cannot enter the live mount path. `ROOT` contains regular files:

```text
proc/cmdline
sys/class/block/vdX/serial
sys/class/block/vdX/ro
sys/class/block/vdX/driver
sys/class/block/vdX/dev
dev/vdX
```

Fixture v2 additionally requires `root.raw` and ordered `lower-<ordinal>.raw`
regular files. It applies the live filesystem parsers and whole-lower digest,
but intentionally does not pretend regular files exercise block ioctls.

`driver`, `ro` and `dev` contain `virtio_blk\n`, `1\n` and `0:0\n` in the
portable fixture. The transport and filesystem files must be single-link
regular files with no group/world write bits; lowers must have no write bits.
Exit codes are `0` verified, `64` fixture usage,
`65` cmdline, `66` discovery, `67` envelope/artifact, and `68` semantic plan
rejection, `69` filesystem rejection, live-only `70` mount/assembly rejection,
and live-only `71` root-transition rejection. A partial transition is reported
as indeterminate rather than rolled back. Live PID 1 never exits: both success
and failure wait fail-closed.
The root-volume generation is bounded consistently in Python and C to 4096
canonical decimal digits.

## Native KVM qualification

`tests/kvm/test_oci_guest_stage1_live.py` is the only release-qualified live
consumer proof. It direct-boots this exact packaged initramfs on native Linux
x86_64 with KVM API 12 and QEMU `-accel kvm -cpu host`. The selected kernel
configuration must provide cgroup support, initrd, devtmpfs, proc/sysfs, PCI, serial
console, virtio block, ext4, SquashFS xattr plus gzip/zstd codecs, and
OverlayFS requirements as built-ins (`=y`). A successful boot
attaches an actual ext4 writable root and two actual SquashFS read-only lowers
in deliberately permuted QEMU order. A successful boot must emit the
root-transition and workload-started markers, then a PID-1-authored terminal
marker binding main status 42, cooperative status 43, forced status 137, three
reaps, forwarded signal 15, and root PID 1 credentials with no supplementary groups. It
remains alive in the terminal fail-closed wait. Before
the root-transition marker, PID 1 proves `/` is the same authenticated OverlayFS
inode previously mounted at staging, `/proc/self/root` matches `/`, the moved
pseudo-filesystems retain their pre-transition identities, probes still pass
at `/`, and every device passes a final recheck. Thirteen
separate negative boots cover writable transport, root/lower absence, wrong
serial, read-only-mode mismatch, capacity mismatch, duplicate serial, and an
extra disk. Six separate same-topology filesystem negatives cover root magic,
label and geometry plus lower magic, structure and whole-image digest. Each
must emit only its exact rejection marker and remain in the PID 1 fail-closed wait.
Root controls carry a recalculated valid superblock checksum. Lower magic and
structure controls carry their mutated image digest through a distinct
plan/transport/cmdline, while only the digest control intentionally keeps the
original digest.

The v19 receipt retains owner-only positive and per-control consoles plus a
canonical receipt binding each exact path-free topology. Missing
KVM prerequisites fail when `PALIMPSEST_REQUIRE_STAGE1_KVM=1`; they are not
converted into skips. TCG can be useful for development but is never accepted
as qualified evidence. This boundary proves transport, block identity,
filesystem structure/content policy, OverlayFS assembly, and an actual `/`
through `palimpsest.stage1-root-transition.v1` method `move-mount-chroot`, then
the `palimpsest.guest-pid1-supervisor.v10` execution checkpoint, the
`palimpsest.workload-lifecycle-authority-isolation.v3` boundary, and the
`palimpsest.guest-lifecycle-broker.v3` exchange.
Literal `pivot_root` remains false, the initial initramfs root is covered rather
than claimed unmounted or reclaimed, mutable root content is not authenticated,
and production VM launch remains disabled. Stage-1 plan/protocol v15 admits
the bounded image-root account and shell-free PATH process subset.

Before release, PID 1 verifies that the workload child has closed the
lifecycle descriptor, entered a private mount namespace, installed an exact
private safe `/dev`, made or masked sysfs/cgroup control paths read-only,
emptied every capability set, locked securebits, enabled `no_new_privs`, and
installed the narrow authority seccomp filter. The exact isolation marker is
emitted only after this child-ready handshake and cgroup attachment, before
`WORKLOAD_STARTED` and lifecycle READY. The UID 0 native positive boot proves
that numeric root receives no capabilities or lifecycle authority while normal
argv/env/cwd/root/stdout, safe-device I/O, ordinary fork, stop, and cleanup
remain usable. No PID or user namespace is claimed; this does not make the
workload availability-safe against all same-PID-namespace denial of service.

The positive highest lower contains the separately reproducible proof workload
and its OCI-root sentinel. That workload validates argv, environment, cwd,
credentials, parent PID, process group, its own `/proc/self/root` and exact
`/palimpsest.agent/exec-00000001` cgroup membership, and PID 1's
four UID/GID values and empty supplementary-group list through bounded
`/proc/1/status` parsing. It creates cooperative and stubborn descendants and
then exits 42 naturally. PID 1 sends the configured stop signal after main
exit; one descendant exits 43 and the other is killed through cgroup v2 with
status 137. Five additional launch controls independently bind a missing
executable, non-executable target, missing cwd, absent named user, and absent
named group to exact child setup stage/errno rejection markers.

After root transition, PID 1 mounts and verifies cgroup v2, creates and pins
the empty `/palimpsest.agent` parent and monotonic session leaf
`exec-00000001` plus both nodes' `cgroup.procs`, `cgroup.kill`, and
`cgroup.events`, and forks a child held behind a release gate. Existing names
are rejected rather than adopted. The child first closes the lifecycle fd,
installs and verifies its isolation boundary, and reports readiness. Only then
does root PID 1 move it into the dedicated cgroup, generate the per-boot
lifecycle key, complete authenticated BOOTSTRAP/KEY_ACK, and send the release byte;
the child may subsequently change cwd and exec. Cleanup uses
only the pinned leaf `cgroup.kill`; it requires `wait4` to `ECHILD`, an empty
leaf and successful leaf removal, then an empty direct-process-free parent and
successful parent removal before the terminal marker. Session IDs are
guest-internal monotonic u32 values, but this qualification permits at most one
active session and does not prove parallel exec. Future detached stop, runtime
exec, and agent lifecycle still need the production broker and dispatch path.
The cgroup provides workload containment and deterministic cleanup; it is not
a complete hostile-root availability sandbox. Every admitted resolved identity,
including UID 0, executes without capabilities behind the same boundary.

The native proof opens the uniquely named lifecycle virtio port and runs the
bounded v2 HELLO/BOOTSTRAP/KEY_ACK/READY/STOP/TERMINAL exchange for both the
base and distinct UID 0 plans. It also qualifies signed console BOUNDARY_ACK,
retained-root reconnect/SNAPSHOT/same-ID retry and deduplication, plus the
malformed, stale, replayed, and conflicting input matrix. TERMINAL is sent only
after cgroup cleanup certainty, an identity-stable no-follow reopen of OverlayFS
`/`, successful `syncfs`, and successful descriptor close, and before the console
terminal marker. Receipt v19 records `reconnect_proven=true` and
`negative_input_proven=true`; production
runtime dispatch and host-daemon recovery remain future boundaries.

Proof v7 uses two real zstd SquashFS images built from the committed
`tests/kvm/assets/inputs` trees. Both contain the same reserved root-level
sentinel with different bytes; an optional authenticated plan probe (empty for
normal production plans) verifies that the highest ordinal is visible through
the merged tree. The positive path boots the same ext4 backing twice and binds
seed, boot-one/post=boot-two/pre, and boot-two/post digests. This is a
synchronized retained-root reassembly checkpoint after `syncfs` on the mounted
ext4 filesystem. QEMU is terminated after the marker, so it is neither a
graceful guest-shutdown nor a crash-recovery claim. Three additional boots
isolate missing, wrong-sized, and wrong-digest post-overlay probe rejection.
Three v3-fixture-backed transition controls independently replace the highest
lower with a real zstd SquashFS containing a regular `dev`, `sys`, or `proc`.
Each reaches valid assembly and then proves that the named target must be an
exact root-owned 0755 empty directory before any mount move.
