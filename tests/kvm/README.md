# Native stage-1 KVM proof

This suite is the release-qualified proof for the packaged x86_64 `/init` live
path. It direct-boots a Linux bzImage with the exact source-controlled
initramfs, raw stage-1 transport, writable root, and two read-only lower disks
under `-accel kvm -cpu host`. Device attachment order is intentionally
permuted. The positive boot must mount the authenticated ext4/SquashFS set,
assemble `/run/palimpsest/merged`, reach the single staging-overlay marker, and
remain alive in the fail-closed PID 1 wait. Thirteen separate negative boots exercise writable
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
proof stops after authenticated filesystem mounts and staging OverlayFS
assembly. The assembled tree is not `/`; pivot, workload supervision, and
production define/start remain disabled.

The v5 canonical receipt binds every path-free negative topology contract and
its console digest, records separate mutable-root seed/post-run digests, and
keeps immutable transport/lower equality. Evidence contains
`negative-<case>.bin` for every named control; all evidence files are
exclusively created owner-readable (`0400`).

The PR workflow also has an always-running `Required native KVM proof`
aggregator. It fails if the self-hosted job is disabled, skipped or
unsuccessful; setting the repository variable to false cannot turn this proof
into a green merge check.
