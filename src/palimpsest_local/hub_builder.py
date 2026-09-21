"""One-shot, worker-owned Palimpsestfile VM build from pinned Hub blob inputs.

This executable is invoked by a separate KVM-capable Hub build worker, never by the
Hub HTTP process. Its JSON input is written by that worker in a private job tree.
The caller's Keystone token and Hub service credentials are not forwarded.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import stat
import sys
from pathlib import Path
from typing import Any

from . import platforms, state
from .build import build_layer, parse_palimpsestfile_text, verify_build_integrity
from .digest import require_digest, require_file_digest
from .oci_layout import MEDIA_TYPE_LAYER_SQUASHFS, ContentStore
from .refs import BuildSpec, ImageRef, LayerRef


def run_build(manifest: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    """Verify Hub inputs, then execute RUN only in the disposable builder guest."""
    roots = state.init_roots()
    store = ContentStore(roots.store)
    recipe_text = manifest["recipe"]
    recipe = parse_palimpsestfile_text(recipe_text)
    base_data = manifest["base"]
    layer_data = manifest["layers"]
    base_digest = require_digest(base_data["digest"])
    layer_digests = tuple(require_digest(item["digest"]) for item in layer_data)
    verify_build_integrity(recipe, cli_base=base_digest, cli_layers=layer_digests)

    base_source = Path(base_data["path"])
    require_file_digest(base_source, base_digest)
    base_path = store.ingest_file(base_source, expected_digest=base_digest)
    store.write_metadata(
        base_digest,
        {"kind": "cloud-image", "disk_format": base_data["disk_format"], "arch": base_data["arch"]},
    )
    base = ImageRef(
        digest=base_digest,
        disk_format=base_data["disk_format"],
        arch=base_data["arch"],
        os_variant=None,
        local_path=base_path,
    )
    layers: list[LayerRef] = []
    previous: str | None = None
    for item, digest in zip(layer_data, layer_digests, strict=True):
        if item["media_type"] != MEDIA_TYPE_LAYER_SQUASHFS or item["parent_digest"] != previous:
            raise ValueError("Hub layer media type or parent chain mismatch")
        if previous is None and item["base_image_digest"] != base_digest:
            raise ValueError("Hub layer base image mismatch")
        source = Path(item["path"])
        require_file_digest(source, digest)
        local_path = store.ingest_file(source, expected_digest=digest)
        store.write_metadata(
            digest,
            {
                "kind": "squashfs",
                "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                "parent_digest": previous,
                "base_image_digest": base_digest,
            },
        )
        layers.append(LayerRef(digest=digest, media_type=MEDIA_TYPE_LAYER_SQUASHFS, local_path=local_path))
        previous = digest

    recipe_path = job_dir / "Palimpsestfile"
    recipe_path.write_text(recipe_text, encoding="utf-8")
    record = build_layer(
        BuildSpec(
            base=base, parent_layers=tuple(layers), recipe=recipe_path, network="none", output_name=manifest["name"]
        ),
        roots=roots,
    )
    digest = require_digest(record["output_digest"])
    output_path = store.blob_path(digest)
    require_file_digest(output_path, digest)
    staging = job_dir / "result.sqsh"
    shutil.copyfile(output_path, staging)
    require_file_digest(staging, digest)
    return {"digest": digest, "size_bytes": staging.stat().st_size}


def cleanup_build(job_dir: Path) -> None:
    """Reclaim only builder guests recorded inside this private Hub job tree."""
    from . import cloud_runtime

    roots = state.resolve_roots(
        {
            "PALIMPSEST_STATE_HOME": str(job_dir / "state"),
            "PALIMPSEST_LOG_HOME": str(job_dir / "logs"),
            "XDG_CONFIG_HOME": str(job_dir / "config"),
        }
    )
    if not roots.builds.exists():
        return
    for record in roots.builds.iterdir():
        if not record.is_dir() or not re.fullmatch(r"b-[0-9a-f]{12}", record.name):
            continue
        name = f"builder-{record.name}"
        run_tree = state.run_paths(roots, name).root
        if run_tree.exists():
            cloud_runtime.rm(name, roots=roots, volumes=True)
        else:
            # A guest can outlive its writer if killed between libvirt creation and
            # the local ledger write. Never silently claim cleanup in that case.
            import libvirt

            conn = libvirt.openReadOnly("qemu:///system")
            if conn is None:
                raise RuntimeError("cannot verify interrupted builder guest")
            try:
                try:
                    conn.lookupByName(name)
                except libvirt.libvirtError as exc:
                    if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                        raise
                else:
                    raise RuntimeError("builder guest exists without an ownership ledger")
            finally:
                conn.close()


def _mark_builder(job_dir: Path, expected_parent: int) -> None:
    """Persist process identity before guest creation; die with the worker."""
    if sys.platform != "linux" or expected_parent <= 1:
        raise RuntimeError("Hub builder requires a Linux worker parent")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "cannot fence builder to worker")
    if os.getppid() != expected_parent or os.getsid(0) != os.getpid():
        raise RuntimeError("builder lost its worker or process-group isolation")
    stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
    start_ticks = stat.rsplit(") ", 1)[1].split()[19]
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    with (job_dir / "builder.pid").open("x", encoding="ascii") as marker:
        json.dump({"pid": os.getpid(), "start_ticks": start_ticks, "boot_id": boot_id}, marker)
        marker.flush()
        os.fsync(marker.fileno())
    directory_fd = os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def preflight_build_host() -> None:
    """Reject missing or mutable host tools and an unavailable system libvirt URI."""
    if sys.platform != "linux" or not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("Linux KVM access is required")
    platforms.select_backend("x86_64", requested=platforms.BACKEND_KVM)
    platforms.preflight(platforms.BACKEND_KVM)
    profile = platforms.resolve_domain_profile(platforms.BACKEND_KVM, "x86_64")
    for tool in ("qemu-img", "cloud-localds", "ssh", "ssh-keygen", "scp", profile.emulator.name):
        executable = shutil.which(tool)
        if executable is None:
            raise RuntimeError("a required build tool is unavailable")
        resolved = Path(executable).resolve(strict=True)
        for entry in (resolved, *resolved.parents):
            info = entry.stat()
            if info.st_uid != 0 or info.st_mode & 0o022 or (entry == resolved and not stat.S_ISREG(info.st_mode)):
                raise RuntimeError("a required build tool is not root-owned and immutable")
    import libvirt

    conn = libvirt.openReadOnly("qemu:///system")
    if conn is None:
        raise RuntimeError("system libvirt is unavailable")
    conn.close()


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "preflight":
        try:
            preflight_build_host()
        except Exception:
            raise SystemExit("builder host preflight failed") from None
        return
    if len(sys.argv) == 3 and sys.argv[1] == "cleanup":
        try:
            cleanup_build(Path(sys.argv[2]))
        except Exception:
            raise SystemExit("build cleanup failed") from None
        return
    if len(sys.argv) != 3:
        raise SystemExit("expected private job manifest and worker pid")
    manifest_path = Path(sys.argv[1])
    try:
        _mark_builder(manifest_path.parent, int(sys.argv[2]))
    except Exception:
        raise SystemExit("builder worker identity unavailable") from None
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        result = run_build(manifest, manifest_path.parent)
    except Exception:
        # Paths, guest output and recipe commands can contain secrets; only the
        # worker's private state and host journal may retain diagnostic detail.
        raise SystemExit("build failed") from None
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
