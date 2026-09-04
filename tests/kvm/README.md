# Native stage-1 KVM proof

This suite is the release-qualified proof for the packaged x86_64 `/init` live
path. It direct-boots a Linux bzImage with the exact source-controlled
initramfs, raw stage-1 transport, writable root, and two read-only lower disks
under `-accel kvm -cpu host`. Device attachment order is intentionally
permuted. The positive boot must mount the authenticated ext4/SquashFS set,
assemble OverlayFS, perform the authenticated move-mount/chroot root
transition, execute the proof workload as numeric `65534:65534` from
`/proof/workdir`, and require PID 1 to supervise its process group. A separate
positive boot executes the same proof as capabilityless numeric `0:0`. The
parent-authored terminal marker must report main status 42, cooperative status
43, forced status 137, three reaped children, forwarded SIGTERM 15, root PID 1
credentials, and completed cgroup cleanup; workload output is never
qualification authority. PID 1 and QEMU must remain alive after that marker.
Thirteen separate negative boots exercise writable
transport, missing/wrong/read-only/wrong-size root, missing/wrong/writable/wrong-size
lower, duplicate serial, and extra-disk controls. Each must reach exactly one
rejection marker, no success/preparation marker, and remain alive as PID 1.

The kernel and its config are selected together from `PALIMPSEST_KVM_KERNEL`
and `PALIMPSEST_KVM_KERNEL_CONFIG`, or together from the running Linux release's standard
boot paths. All required cgroup, initrd, devtmpfs, proc/sysfs, PCI, serial console,
virtio block/console/random, hardware random, ext4, SquashFS xattr plus
gzip/zstd codecs, and OverlayFS options
must be built in (`=y`); modules do not qualify because this initramfs has no
module loader. `PALIMPSEST_KVM_QEMU` can select an explicit
QEMU binary. The runner must be native Linux x86_64 with read/write access to a
KVM character device reporting API version 12.

Positive boots also use one private owner-bound QEMU Unix socket with
`server=on,wait=off`, one named virtio-serial port, and virtio RNG. The host
drives canonical lifecycle frames with partial nonblocking I/O and records
path-free frame digests plus nonce/generation/request/sequence correlation.
Receipt v15 covers a single connection and an exact six-connection retained-
root session. The latter proves lost initial READY recovery; ready, stopping,
and terminal SNAPSHOTs; a connection-local partial STOP; complete same-ID
retry; and an already-committed same-ID duplicate accepted without a second
signal dispatch. Linux connects pin socket dev/inode/uid/type and require
`SO_PEERCRED` to identify the spawned QEMU PID and current UID.
Every intended close-to-reconnect transition waits for the exact admitted-peer
EOF marker, and the partial STOP is closed only after its frame-minus-one
buffer marker. Both are proof-only console coordination with the known
workload, not production authority. A production host needs a privileged
in-band boundary acknowledgement or equivalent barrier; this receipt records
`rapid_reconnect_proven=false`.

Ten lifecycle-negative guest boots cover missing/wrong named ports,
zero/oversized lengths, a non-canonical duplicate JSON key, wrong binding,
nonce reuse, stale generation, request-ID collision, and a distinct second
STOP after the first was dispatched. A separate QEMU invocation proves a
duplicate lifecycle port name is rejected before any guest stage-1 marker.
The receipt therefore records `reconnect_proven=true` and
`negative_input_proven=true`; natural terminal behavior is implemented but is
not exercised by another native boot and remains
`natural_terminal_proven=false`.

Run on the qualified host:

```sh
install -d -m 0700 "$RUNNER_TEMP/stage1-kvm-evidence"
PALIMPSEST_REQUIRE_STAGE1_KVM=1 \
PALIMPSEST_KVM_EVIDENCE_DIR="$RUNNER_TEMP/stage1-kvm-evidence" \
uv run pytest -m stage1_kvm tests/kvm -vv
```

Missing prerequisites fail once qualified mode is enabled. TCG results are
development smoke evidence only and are never accepted by this harness. This
receipt v15 records the authenticated OverlayFS moved onto `/`, exact root and moved
pseudo-filesystem identities, `switch_root=true`, and `pivot_root=false`; it
does not claim that the initial initramfs root was unmounted or reclaimed.
It also binds the exact base and UID 0 mode argv, two-entry environment, cwd,
and their respective numeric uid/gid,
empty supplementary groups, root PID 1's fd-pinned cgroup-v2 broker, child
isolation and credential drop before the parent-verifiable attach/release gate,
stop signal, process-group
supervision, signal forwarding, all reaped statuses, and verified empty-cgroup
removal. The workload also proves UID 65534 cannot write-open either the parent
or its own `cgroup.procs`. Production define/start remains
disabled.

The v15 canonical receipt binds all 41 native-KVM guest boots: two positive boots
using the same retained mutable root, 13 topology controls, six filesystem
controls, three assembly controls, three root-transition controls, and three
workload-launch controls, ten lifecycle controls, and one UID 0 isolation
positive boot. One additional QEMU invocation is the duplicate-name preboot
rejection, for 42 invocations total.
It binds every path-free negative topology contract and
its console digest, records mutable-root seed/boot-one/boot-two digests, and
keeps immutable transport/lower equality. The two committed real SquashFS
fixtures use zstd level 3 and contain an ordinal-specific collision sentinel;
the authenticated proof-only merged-tree probe checks highest-ordinal
precedence. Three post-overlay probe controls fail only at assembly validation.
Three additional transition-target boots use distinct real zstd SquashFS
highest lowers containing a regular `dev`, `sys`, or `proc`; each must reach
only the root-transition rejection marker while its independent mutable root
seed/post digest and immutable lower/transport bytes remain receipt-bound.
The workload controls use separate plan, transport and mutable-root files.
They reach the root-transition marker, then reject a missing executable at
stage 7/errno 2, non-executable `/layer.txt` at stage 7/errno 13, or missing
cwd at stage 6/errno 2. They must emit neither workload-started nor terminal
markers and must remain alive fail-closed.

The highest lower and every transition fixture include the exact `0755`
`/.__palimpsest_workload_proof_v1`, the root sentinel, and
`/proof/workdir`. Fixture policy v9 binds the recursive source entry types and
modes, proof source/build-script/ELF hashes, digest-pinned GCC image, and the
pinned mksquashfs 4.7.5 zstd-level-3 build policy.
Evidence contains positive and retained-boot consoles plus every named
negative console and the raw QEMU duplicate-name rejection output. All
evidence files are
exclusively created owner-readable (`0400`).

The PR workflow also has an always-running `Required native KVM proof`
aggregator. It fails if the self-hosted job is disabled, skipped or
unsuccessful; setting the repository variable to false cannot turn this proof
into a green merge check.

The stage-1 plan/protocol is v11, OCI-root domain plan is v10, and the init
contract is v13. Initramfs manifest/ABI are v15, supervisor is v6, lifecycle
broker is v2, and filesystem fixture policy/schema are v9. Pre-mount,
filesystem, and assembly negative controls reject before
root transition and must contain no root-transition rejection marker. A
failure after an irreversible mount move emits the dedicated indeterminate
root-state marker and waits fail-closed; no rollback is claimed. Native KVM
must still produce a v15 receipt on the qualified runner; this source update and
local macOS tests are not native-KVM runtime evidence.

This qualification PID 1 remains a root, narrow broker while the admitted
workload child enters a private mount namespace, closes the lifecycle fd,
drops every capability, locks securebits, enables `no_new_privs` and the narrow
authority seccomp filter, and then assumes its numeric identity. The UID 0
proof checks the same boundary with `Cap*=0`, `NoNewPrivs=1`, `Seccomp=2`, an
exact private safe-device set, read-only/masked control paths, inaccessible PID
1 fd/memory, denied authority syscalls, and a working ordinary fork. This is a
lifecycle-authority boundary, not a complete hostile-root availability
sandbox: no PID or user namespace is introduced. The boundary remains
single-workload: future detached stop, multi-user exec, and agent lifecycle
require a separate production broker and authenticated host lifecycle channel.
The cgroup is a containment and cleanup mechanism; production policy must
separately constrain denial-of-service behavior.
