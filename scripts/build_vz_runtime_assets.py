#!/usr/bin/env python3
"""Build native VZ boot assets from an offline Linux installation.

Example (all inputs must already exist; no downloads or installation occur)::

    python3 scripts/build_vz_runtime_assets.py --output-dir ./vz-assets \
        --platform-binary ./plm-vz --kernel /boot/vmlinuz-6.8.0-147-generic \
        --modules-dir /usr/lib/modules/6.8.0-147-generic \
        --busybox /usr/bin/busybox --dropbear ./dropbearmulti \
        --guestagent ./lima-guestagent.Linux-aarch64 \
        --kernel-release 6.8.0-147-generic

Required binaries are static ARM64: first-party palimpsest.vz-platform.v2,
BusyBox 1.36.1, Dropbear multi 2026.94 and Lima guestagent 2.2.1. The kernel
must be the original matched Ubuntu ARM64 Image (optionally gzip encoded),
with its config-<release> beside it or config in the exact modules directory.
Supply original modules.dep, modules.alias, modules.softdep, modules.builtin and
modules.builtin.modinfo. Only the required dependency closure is packed, as
uncompressed .ko files with text indexes and an explicit /platform/modules.load
order (including soft dependencies). Original compressed module/index hashes,
sizes and encodings remain in the manifest; aliases retain their original order.
The original config must enable the required ARM64, virtio, filesystem, cgroup
memory/pids, /proc children, socket diagnostics and SquashFS/
ext4 xattr/ACL facilities. Config-selected socket diagnostic modules are preloaded
for the real Lima guestagent. BusyBox must include the selected control applets.
No input executable is run to manufacture version markers.
Canonical input paths must not traverse symlinks (use /usr/lib rather than a
merged-usr /lib symlink). Original .ko.zst modules use the already-installed,
root-owned system libzstd.so.1; no decompressor installation is attempted, and
the hashed library inode is checked against the actual loaded symbol mapping.

Optional <binary>.provenance.json and <kernel>.provenance.json sidecars accept
public source, recipe, toolchain and licenses metadata. Never include keys,
credentials, private host paths, workload bytes or dynamic platform libraries.
Output is a new private immutable directory; existing output is not overwritten.
The builder reopens and structurally validates all published bytes before
returning. Copy/transfer must retain caller ownership, directory mode 0500 and
file mode 0400 with no links or extra members. Missing provenance stays unknown:
neither structural assembly nor declared sidecars constitute runtime qualification,
source-signature verification or binary/toolchain attestation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Also usable directly from an offline source checkout, without installing it.
_SOURCE = Path(__file__).resolve().parents[1] / "src"
if _SOURCE.is_dir():
    sys.path.insert(0, str(_SOURCE))


def main() -> int:
    from palimpsest_local.errors import PalimpsestError
    from palimpsest_local.vz_runtime_assets import build_vz_runtime_assets

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, help_text in (
        ("output-dir", "NEW private output directory (must not exist)"),
        ("platform-binary", "static first-party ARM64 platform executable"),
        ("kernel", "original Ubuntu ARM64 Image or single gzip stream"),
        ("modules-dir", "canonical original /usr/lib/modules/<release> directory"),
        ("busybox", "static ARM64 BusyBox 1.36.1 executable"),
        ("dropbear", "static ARM64 Dropbear multi 2026.94 executable"),
        ("guestagent", "static ARM64 Lima 2.2.1 guestagent executable"),
    ):
        parser.add_argument("--" + name, required=True, type=Path, help=help_text)
    parser.add_argument("--kernel-release", required=True, help="exact original Ubuntu kernel release")
    parser.add_argument("--lima-version", default="2.2.1", choices=("2.2.1",))
    args = parser.parse_args()
    try:
        result = build_vz_runtime_assets(**vars(args))
    except (PalimpsestError, OSError, ValueError) as exc:
        parser.exit(1, f"offline VZ asset assembly failed: {exc}\n")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
