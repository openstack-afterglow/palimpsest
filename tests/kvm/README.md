# Native stage-1 KVM proof

This suite is the release-qualified proof for the packaged x86_64 `/init` live
path. It direct-boots a Linux bzImage with the exact source-controlled
initramfs, raw stage-1 transport, writable root, and two read-only lower disks
under `-accel kvm -cpu host`. Device attachment order is intentionally
permuted. The positive boot must reach the single pre-mount post-ioctl marker and remain alive in
the fail-closed PID 1 wait. A second boot exposes the same bytes through a
writable virtio-blk mapping and must reach only the rejection marker.

The kernel and its config are selected together from `PALIMPSEST_KVM_KERNEL`
and `PALIMPSEST_KVM_KERNEL_CONFIG`, or together from the running Linux release's standard
boot paths. All required initrd, devtmpfs, proc/sysfs, PCI, serial console and
virtio options must be built in (`=y`); modules do not qualify because this
initramfs has no module loader. `PALIMPSEST_KVM_QEMU` can select an explicit
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
proof stops after authenticated root/lower block identity. Filesystem magic
and content verification, mounting, OverlayFS, pivot, workload supervision and
production define/start remain disabled.

The PR workflow also has an always-running `Required native KVM proof`
aggregator. It fails if the self-hosted job is disabled, skipped or
unsuccessful; setting the repository variable to false cannot turn this proof
into a green merge check.
