# Palimpsest guest stage-1 consumer

`init.c` is a freestanding Linux x86_64 PID 1. It uses raw syscalls and embeds
its own SHA-256 and canonical JSON validation; it has no libc, dynamic loader,
or system-header dependency. It authenticates the stage-1 transport and then
waits permanently. It does not mount OCI root/lower filesystems, assemble
OverlayFS, pivot root, or execute the image workload.

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

The same ELF supports `--fixture-v1 ROOT` only when it is not PID 1. A
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

`driver`, `ro` and `dev` contain `virtio_blk\n`, `1\n` and `0:0\n` in the
portable fixture. The transport file must be a single-link regular file with
no group/world write bits. Exit codes are `0` verified, `64` fixture usage,
`65` cmdline, `66` discovery, `67` envelope/artifact, and `68` semantic plan
rejection. Live PID 1 never exits: both success and failure wait fail-closed.
The root-volume generation is bounded consistently in Python and C to 4096
canonical decimal digits.
