# Native stage-1 KVM proof

This suite is the release-qualified proof for the packaged x86_64 `/init` live
path. It direct-boots a Linux bzImage with the exact source-controlled
initramfs, raw stage-1 transport, writable root, and two read-only lower disks
under `-accel kvm -cpu host`. Device attachment order is intentionally
permuted. The positive boot must mount the authenticated ext4/SquashFS set,
assemble OverlayFS, perform the authenticated move-mount/chroot root
transition, execute the proof workload as numeric `65534:65534` from
`/proof/workdir`, and require PID 1 to supervise its process group. The
parent-authored terminal marker must report main status 42, descendant status
43, two reaped children, and forwarded SIGTERM 15; workload output is never
qualification authority. PID 1 and QEMU must remain alive after that marker.
Thirteen separate negative boots exercise writable
transport, missing/wrong/read-only/wrong-size root, missing/wrong/writable/wrong-size
lower, duplicate serial, and extra-disk controls. Each must reach exactly one
rejection marker, no success/preparation marker, and remain alive as PID 1.

The kernel and its config are selected together from `PALIMPSEST_KVM_KERNEL`
and `PALIMPSEST_KVM_KERNEL_CONFIG`, or together from the running Linux release's standard
boot paths. All required initrd, devtmpfs, proc/sysfs, PCI, serial console,
virtio block, ext4, SquashFS xattr plus gzip/zstd codecs, and OverlayFS options
must be built in (`=y`); modules do not qualify because this initramfs has no
module loader. `PALIMPSEST_KVM_QEMU` can select an explicit
QEMU binary. The runner must be native Linux x86_64 with read/write access to a
KVM character device reporting API version 12.

Run on the qualified host:

```sh
install -d -m 0700 "$RUNNER_TEMP/stage1-kvm-evidence"
PALIMPSEST_REQUIRE_STAGE1_KVM=1 \
PALIMPSEST_KVM_EVIDENCE_DIR="$RUNNER_TEMP/stage1-kvm-evidence" \
uv run pytest -m stage1_kvm tests/kvm -vv
```

Missing prerequisites fail once qualified mode is enabled. TCG results are
development smoke evidence only and are never accepted by this harness. This
Receipt v9 records the authenticated OverlayFS moved onto `/`, exact root and moved
pseudo-filesystem identities, `switch_root=true`, and `pivot_root=false`; it
does not claim that the initial initramfs root was unmounted or reclaimed.
It also binds the exact argv, two-entry environment, cwd, numeric uid/gid,
empty supplementary groups, stop signal, process-group supervision, signal
forwarding, and both reaped statuses. Production define/start remains
disabled.

The v9 canonical receipt binds all 30 native-KVM boots: two positive boots
using the same retained mutable root, 13 topology controls, six filesystem
controls, three assembly controls, three root-transition controls, and three
workload-launch controls. It binds every path-free negative topology contract and
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
`/proof/workdir`. Fixture policy v4 binds the recursive source entry types and
modes, proof source/build-script/ELF hashes, digest-pinned GCC image, and the
pinned mksquashfs 4.7.5 zstd-level-3 build policy.
Evidence contains positive and retained-boot consoles plus
`negative-<case>.bin` for every named control; all evidence files are
exclusively created owner-readable (`0400`).

The PR workflow also has an always-running `Required native KVM proof`
aggregator. It fails if the self-hosted job is disabled, skipped or
unsuccessful; setting the repository variable to false cannot turn this proof
into a green merge check.

The stage-1 plan/protocol is v7 and the init contract is v8. Pre-mount,
filesystem, and assembly negative controls reject before
root transition and must contain no root-transition rejection marker. A
failure after an irreversible mount move emits the dedicated indeterminate
root-state marker and waits fail-closed; no rollback is claimed. Native KVM
must still produce a v9 receipt on the qualified runner; this source update and
local macOS tests are not native-KVM runtime evidence.
