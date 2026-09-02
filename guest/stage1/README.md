# Palimpsest guest stage-1 consumer

`init.c` is a freestanding Linux x86_64 PID 1. It uses raw syscalls and embeds
its own SHA-256 and canonical JSON validation; it has no libc, dynamic loader,
or system-header dependency. It authenticates the stage-1 transport, then
opens and authenticates the complete root/lower block-device set by role,
serial, read-only state, exact size and stable identity before waiting
permanently. It verifies the root ext4 identity and geometry, then every
lower's SquashFS v4 structure and whole-device image digest. Live PID 1 mounts
the authenticated block FDs at deterministic staging paths and assembles an
OverlayFS root at `/run/palimpsest/merged`. It does not pivot/chroot into that
tree or execute or supervise the image workload.

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
rejection, `69` filesystem rejection, and live-only `70` mount/assembly
rejection. Live PID 1 never exits: both success and failure wait fail-closed.
The root-volume generation is bounded consistently in Python and C to 4096
canonical decimal digits.

## Native KVM qualification

`tests/kvm/test_oci_guest_stage1_live.py` is the only release-qualified live
consumer proof. It direct-boots this exact packaged initramfs on native Linux
x86_64 with KVM API 12 and QEMU `-accel kvm -cpu host`. The selected kernel
configuration must provide the initrd, devtmpfs, proc/sysfs, PCI, serial
console, virtio block, ext4, SquashFS xattr plus gzip/zstd codecs, and
OverlayFS requirements as built-ins (`=y`). A successful boot
attaches an actual ext4 writable root and two actual SquashFS read-only lowers
in deliberately permuted QEMU order. A successful boot must emit the
staging-overlay marker exactly once and remain alive in the PID 1 fail-closed wait. Thirteen
separate negative boots cover writable transport, root/lower absence, wrong
serial, read-only-mode mismatch, capacity mismatch, duplicate serial, and an
extra disk. Six separate same-topology filesystem negatives cover root magic,
label and geometry plus lower magic, structure and whole-image digest. Each
must emit only its exact rejection marker and remain in the PID 1 fail-closed wait.
Root controls carry a recalculated valid superblock checksum. Lower magic and
structure controls carry their mutated image digest through a distinct
plan/transport/cmdline, while only the digest control intentionally keeps the
original digest.

The v5 proof retains owner-only positive and per-control consoles plus a
canonical receipt binding each exact path-free topology. Missing
KVM prerequisites fail when `PALIMPSEST_REQUIRE_STAGE1_KVM=1`; they are not
converted into skips. TCG can be useful for development but is never accepted
as qualified evidence. This boundary proves transport, block identity,
filesystem structure/content policy and staging OverlayFS assembly. Mutable
root content is not authenticated; the merged tree is not `/`, and pivot,
workload supervision and production VM launch remain disabled.
