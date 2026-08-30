# OCI root layer filesystem decision

Palimpsest Phase 1 uses **SquashFS** for immutable OCI layer artifacts. This is a filesystem-semantics decision only: the OCI runtime, VM boot path, and CLI remain inactive until their later delivery gates land.

## Decision

SquashFS 4.6.1 passed the two-layer fixture through its real `mksquashfs -tar` path on a privileged Linux kernel. The probe demonstrated:

- ordinary OCI whiteouts stored as character devices `0:0` in the leaf and honored by OverlayFS;
- opaque directory markers stored as `trusted.overlay.opaque=y` in the leaf and honored by OverlayFS;
- file/directory replacement and archive-order behavior;
- regular files, directories, symlinks, forward/backward hardlinks, FIFO and character/block device metadata;
- exact root and non-root uid, gid, mode, integer-second mtime, user xattrs, and a valid binary `security.capability` value;
- byte-identical image rebuilds with fixed filesystem creation/root times, including two independent privileged container processes;
- lower mounts requested with `loop,ro,nodev,nosuid,noexec` and the merged OverlayFS mount requested with `nodev,nosuid,noexec`; effective filesystem type plus `ro,nodev,nosuid,noexec` on lowers and `nodev,nosuid,noexec` on the merged mount are verified from `/proc/self/mountinfo` (loop setup itself is not a security assertion);
- denial when opening the controlled character-device fixture on both lower and merged mounts. The probe never opens the block-device or FIFO fixtures.

The exact SquashFS command contract is:

```text
mksquashfs - <output> -tar -noappend -xattrs -mkfs-time 0 -processors 1 \
  -root-mode <mode> -root-uid <uid> -root-gid <gid> -root-time <mtime> \
  [-p "/ x <root-xattr>=0s<base64-value>" ...]
```

The probe consumes an already decompressed, diff-id-verified tar stream on standard input. Registry media-type decompression and diff-id verification belong to the later ingestion stage; this boundary explicitly rejects gzip/zstd blobs. It does not extract archive paths or create archive device/FIFO entries in a host directory.

## Retained EROFS comparison

EROFS 1.7.1 is not selected. With the tested tar path, `-T 0` makes the image deterministic but overwrites member mtimes with zero. Omitting the fixed timestamp preserves member metadata but produces different image digests on repeated builds. The privileged regression test retains this timestamp failure so a future toolchain improvement is visible rather than silently changing the backend.

The evaluated EROFS command is:

```text
mkfs.erofs --tar=f --ovlfs-strip=0 -T 0 -U 00000000-0000-0000-0000-000000000000 --preserve-mtime <output> <translated-tar>
```

No flattening fallback or weakened deletion semantics is permitted.

## Fixture and oracle

`tests/fixtures/oci-root/fixture-manifest.json` pins the exact size and SHA-256 of both tar layers and the merged receipt. The generator's `--check` mode regenerates all payloads in memory and fails on a missing file, symlink, size/hash mismatch, byte drift, or manifest drift without writing files.

The receipt compares the exact merged path set, type, ownership, permission bits, nanosecond timestamp value, content digest, symlink target, hardlink device/inode/link count, device major/minor values, and base64-encoded xattr byte sets. Separate lower-layer assertions prove whiteout and opaque control metadata before inspecting the merged view.

## Privileged proof contract

The `OCI filesystem proof (privileged Linux)` workflow job is the stable required status for this gate. Repository branch protection must require that exact job name. It installs `squashfs-tools` and `erofs-utils`, sets `PALIMPSEST_REQUIRE_OCI_FS=1`, and therefore fails rather than skips when Linux, root, `CAP_SYS_ADMIN`, mount namespace support, mountinfo, a required tool, or a required packer feature is absent. It then starts a second pytest process, rebuilds the selected SquashFS artifacts, and byte-compares the complete evidence JSON before retaining both receipts. Ordinary developer runs skip the two privileged cases with an explicit reason.

The probe creates a private mount namespace, marks `/` recursively private, uses an owner-only temporary directory, copies the preflight-hashed packer into that directory and executes only that pinned copy, applies subprocess timeouts and a fixed locale, validates output magic and deterministic digests, checks effective mount flags, and requires successful reverse-order unmount before publishing evidence. Translation is bounded to 10,000 members, a 512 MiB input/total regular payload, a 256 MiB individual file, and 64 KiB PAX metadata per member; production-scale streaming is still an activation prerequisite for the later registry converter.

Run the local pure gate:

```text
uv run python tests/fixtures/oci-root/generate_fixtures.py --check
uv run python -m pytest tests/unit/test_oci_convert_security.py tests/unit/test_oci_fs_fixtures.py tests/oci_fs -q
```

Run the strict Linux gate as root on a disposable privileged Linux host:

```text
PALIMPSEST_REQUIRE_OCI_FS=1 \
PALIMPSEST_OCI_FS_EVIDENCE_DIR=/tmp/oci-fs-evidence \
uv run python -m pytest -m oci_fs tests/oci_fs -vv
```

## Retained development evidence

The two checked-in matching development receipts came from separate Ubuntu 24.04 privileged container processes in a Docker Desktop Linux VM on 2026-08-30:

- kernel: `7.0.12-linuxkit`, architecture `aarch64`;
- `squashfs-tools 4.6.1`: pass;
- `erofs-utils 1.7.1`: expected timestamp-semantics failure;
- strict privileged tests: `2 passed` (selected SquashFS pass plus retained EROFS rejection);
- SquashFS base image SHA-256: `ae84880f28360df6acc0564675bcbeb968530a891ad7b26c93f21f6c75f3ede9`;
- SquashFS leaf image SHA-256: `f135e575e2aff48f01cf3dca5115022a849e82f798211f352eddd06017cab6cc`;
- packer executable SHA-256: `4bc5c66a81a1d86a5743e1c1ced12b6362e1b56c088d8241ef9ed5a3b8659317`.

The GitHub job repeats the proof on the required x86_64 Linux runner and uploads its candidate evidence JSON. That required job, rather than the development receipt alone, is the merge authority.
