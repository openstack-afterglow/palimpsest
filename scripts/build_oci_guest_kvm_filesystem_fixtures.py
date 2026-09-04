#!/usr/bin/env python3
"""Rebuild the deterministic native-KVM SquashFS workload fixtures."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
ASSETS = REPOSITORY / "tests" / "kvm" / "assets"
INPUTS = ASSETS / "inputs"
MANIFEST = ASSETS / "filesystem-fixtures.json"
HELPER = ASSETS / "workload-proof.x86_64"
HELPER_NAME = ".__palimpsest_workload_proof_v1"
SOURCES = {
    "lower0": ("lower0", "lower0.squashfs.b64"),
    "lower1": ("lower1", "lower1.squashfs.b64"),
    "transition_dev": ("transition-dev", "transition-dev.squashfs.b64"),
    "transition_proc": ("transition-proc", "transition-proc.squashfs.b64"),
    "transition_sys": ("transition-sys", "transition-sys.squashfs.b64"),
}
MKSQUASHFS_ARGS = (
    "-comp",
    "zstd",
    "-Xcompression-level",
    "3",
    "-b",
    "131072",
    "-noappend",
    "-no-exports",
    "-no-xattrs",
    "-reproducible",
    "-all-time",
    "0",
    "-mkfs-time",
    "0",
    "-all-root",
    "-processors",
    "1",
    "-no-progress",
)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_entries(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            entries.append({"mode": mode, "path": relative, "type": "directory"})
        elif stat.S_ISREG(info.st_mode):
            payload = path.read_bytes()
            entries.append(
                {
                    "mode": mode,
                    "path": relative,
                    "sha256": digest(payload),
                    "size_bytes": len(payload),
                    "type": "file",
                }
            )
        else:
            raise SystemExit(f"unsupported fixture entry: {path}")
    return entries


def build(*, check: bool) -> None:
    packer_name = shutil.which("mksquashfs")
    if packer_name is None:
        raise SystemExit("mksquashfs is required")
    packer = Path(packer_name).resolve()
    version_output = subprocess.run(
        [str(packer), "-version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ).stdout.splitlines()[0]
    version = version_output.removeprefix("mksquashfs version ").split(" ", 1)[0]
    if version != "4.7.5":
        raise SystemExit(f"mksquashfs 4.7.5 is required, got {version}")

    helper = HELPER.read_bytes()
    generated: dict[Path, bytes] = {}
    metadata = json.loads(MANIFEST.read_text(encoding="ascii"))
    for key, (source_name, output_name) in SOURCES.items():
        source = INPUTS / source_name
        if key != "lower0":
            helper_path = source / HELPER_NAME
            if not check:
                helper_path.write_bytes(helper)
                helper_path.chmod(0o755)
            elif helper_path.read_bytes() != helper or stat.S_IMODE(helper_path.stat().st_mode) != 0o755:
                raise SystemExit(f"stale workload helper: {helper_path}")
        with tempfile.TemporaryDirectory(prefix="palimpsest-kvm-fixture-") as temporary:
            raw_path = Path(temporary) / "fixture.squashfs"
            subprocess.run([str(packer), str(source), str(raw_path), *MKSQUASHFS_ARGS], check=True)
            raw = raw_path.read_bytes()
        generated[ASSETS / output_name] = base64.b64encode(raw) + b"\n"
        artifact = metadata["artifacts"][key]
        artifact["raw_sha256"] = digest(raw)
        artifact["raw_size_bytes"] = len(raw)
        artifact["source_entries"] = source_entries(source)

    metadata["policy"] = "palimpsest.kvm-actual-filesystem-fixtures.v11"
    metadata["schema"] = "palimpsest.kvm-filesystem-fixtures.v11"
    metadata["provenance"]["lower_builder"].update(
        {
            "argv_policy": "mksquashfs source output " + " ".join(MKSQUASHFS_ARGS),
            "executable_sha256": digest(packer.read_bytes()),
            "version": f"mksquashfs {version}",
        }
    )
    proof = metadata["provenance"]["workload_proof"]
    proof.update(
        {
            "elf_sha256": digest(helper),
            "elf_size_bytes": len(helper),
            "source_sha256": digest((REPOSITORY / proof["source"]).read_bytes()),
        }
    )
    generated[MANIFEST] = (json.dumps(metadata, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    if check:
        stale = [
            str(path.relative_to(REPOSITORY)) for path, payload in generated.items() if path.read_bytes() != payload
        ]
        if stale:
            raise SystemExit("stale KVM fixtures: " + ", ".join(stale))
        return
    for path, payload in generated.items():
        path.write_bytes(payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    build(check=args.check)


if __name__ == "__main__":
    main()
