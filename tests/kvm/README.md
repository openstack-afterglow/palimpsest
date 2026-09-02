# Native stage-1 KVM proof

This suite is the release-qualified proof for the packaged x86_64 `/init` live
path. It direct-boots a Linux bzImage with the exact source-controlled
initramfs, raw stage-1 transport, writable root, and two read-only lower disks
under `-accel kvm -cpu host`. Device attachment order is intentionally
permuted. The positive boot must mount the authenticated ext4/SquashFS set,
assemble OverlayFS, perform the authenticated move-mount/chroot root
transition, reach the single root-transition marker with that OverlayFS as
actual `/`, and remain alive in the fail-closed PID 1 wait. Thirteen separate negative boots exercise writable
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
proof stops after the authenticated OverlayFS is moved onto `/` and PID 1
enters it with `chroot(2)`. Receipt v8 records exact root and moved
pseudo-filesystem identities, `switch_root=true`, and `pivot_root=false`; it
does not claim that the initial initramfs root was unmounted or reclaimed.
Workload supervision and production define/start remain disabled.

The v8 canonical receipt binds every path-free negative topology contract and
its console digest, records mutable-root seed/boot-one/boot-two digests, and
keeps immutable transport/lower equality. The two committed real SquashFS
fixtures use zstd level 3 and contain an ordinal-specific collision sentinel;
the authenticated proof-only merged-tree probe checks highest-ordinal
precedence. Three post-overlay probe controls fail only at assembly validation.
Three additional transition-target boots use distinct real zstd SquashFS
highest lowers containing a regular `dev`, `sys`, or `proc`; each must reach
only the root-transition rejection marker while its independent mutable root
seed/post digest and immutable lower/transport bytes remain receipt-bound.
Evidence contains positive and retained-boot consoles plus
`negative-<case>.bin` for every named control; all evidence files are
exclusively created owner-readable (`0400`).

The PR workflow also has an always-running `Required native KVM proof`
aggregator. It fails if the self-hosted job is disabled, skipped or
unsuccessful; setting the repository variable to false cannot turn this proof
into a green merge check.

The stage-1 plan/protocol remains v6 and its supervisor-required handoff is
unchanged. Pre-mount, filesystem, and assembly negative controls reject before
root transition and must contain no root-transition rejection marker. A
failure after an irreversible mount move emits the dedicated indeterminate
root-state marker and waits fail-closed; no rollback is claimed. This source
update is not itself native-KVM runtime evidence.
