"""Durable Hub build claim, verified input handoff, and private layer publication.

Only the separate KVM-capable build worker calls this module. The Hub API never
executes a recipe; neither a caller token nor service credentials reach the VM.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select

from palimpsest_hub.api.hub import (
    HubLayerMeta,
    _grant_layer_access,
    _locked_blob,
    _registration_conflicts,
    _visible_filter,
)
from palimpsest_hub.config import get_build_worker_settings
from palimpsest_hub.database import get_session_factory
from palimpsest_hub.models import PalimpsestHubBuild, PalimpsestHubLayer
from palimpsest_hub.services.digest import compute_config_digest, normalize_digest
from palimpsest_hub.services.hub_store import MEDIA_TYPE_LAYER_SQUASHFS, get_blob_store


class BuildCleanupError(RuntimeError):
    """Keep the job tree and stop the worker if a guest cannot be reclaimed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _cleanup_guest(python: str, job_dir: Path) -> None:
    try:
        with (job_dir / "cleanup.stderr").open("ab") as stderr:
            result = subprocess.run(
                [python, "-I", "-m", "palimpsest_local.hub_builder", "cleanup", str(job_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env={
                    "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
                    "HOME": os.environ.get("HOME", "/nonexistent"),
                },
                cwd=job_dir,
                timeout=60,
                check=False,
            )
        if result.returncode:
            raise BuildCleanupError("builder guest cleanup did not complete")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildCleanupError("builder guest cleanup could not be verified") from exc


def _stop_interrupted_builder(job_dir: Path) -> None:
    """Never reclaim guest state while a recorded builder group may still run."""
    marker = job_dir / "builder.pid"
    if not marker.exists():
        return  # The child writes this before any guest operation.
    try:
        identity = json.loads(marker.read_text(encoding="ascii"))
        pid, start_ticks, boot_id = identity["pid"], identity["start_ticks"], identity["boot_id"]
        if type(pid) is not int or pid <= 1 or not isinstance(start_ticks, str) or not isinstance(boot_id, str):
            raise ValueError("invalid builder identity")
        if boot_id != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip():
            return  # A reboot cannot leave an old process alive.

        def recorded_group_alive() -> bool:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return False
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists():
                raise BuildCleanupError("builder group survived without its recorded leader")
            fields = stat.read_text(encoding="ascii").rsplit(") ", 1)[1].split()
            if fields[19] != start_ticks or int(fields[2]) != pid or int(fields[3]) != pid:
                raise BuildCleanupError("builder pid was reused or process identity changed")
            return True

        if not recorded_group_alive():
            return
        for signal_value in (signal.SIGTERM, signal.SIGKILL):
            os.killpg(pid, signal_value)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not recorded_group_alive():
                    return
                time.sleep(0.05)
        raise BuildCleanupError("builder process group could not be reaped")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise BuildCleanupError("builder process identity could not be verified") from exc


async def fail_interrupted_builds() -> int:
    """Reconcile exact owned job trees before accepting another queued build."""
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("build worker database is unavailable")
    store = get_blob_store(get_build_worker_settings())
    jobs_root = store.root / "builds"
    if jobs_root.is_symlink() or (jobs_root.exists() and not jobs_root.is_dir()):
        raise BuildCleanupError("private build root is not a directory")
    async with factory() as session:
        jobs = (
            (
                await session.execute(
                    select(PalimpsestHubBuild).where(
                        or_(PalimpsestHubBuild.status == "building", PalimpsestHubBuild.error_code == "cleanup_failed")
                    )
                )
            )
            .scalars()
            .all()
        )
        indexed = {job.id: job for job in jobs}
        if jobs_root.is_dir():
            for entry in jobs_root.iterdir():
                try:
                    if entry.is_symlink() or not entry.is_dir() or str(UUID(entry.name)) != entry.name:
                        raise ValueError("unexpected build tree")
                except ValueError as exc:
                    raise BuildCleanupError("unowned private build tree") from exc
                if entry.name not in indexed:
                    job = await session.get(PalimpsestHubBuild, entry.name)
                    if job is None or job.status not in ("complete", "error"):
                        raise BuildCleanupError("private build tree has no terminal owner")
                    jobs.append(job)
                    indexed[job.id] = job

        for job in jobs:
            try:
                if str(UUID(job.id)) != job.id:
                    raise ValueError("invalid job id")
            except ValueError as exc:
                raise BuildCleanupError("build job has an invalid identity") from exc
            job_dir = jobs_root / job.id
            if job_dir.is_symlink():
                raise BuildCleanupError("private build tree is a symlink")
            if not job_dir.is_dir() and job.error_code == "cleanup_failed":
                raise BuildCleanupError("failed cleanup lost its private build tree")
            if job_dir.is_dir():
                try:
                    await asyncio.to_thread(_stop_interrupted_builder, job_dir)
                    await asyncio.to_thread(
                        _cleanup_guest, get_build_worker_settings().palimpsest_hub_builder_python, job_dir
                    )
                    shutil.rmtree(job_dir)
                except (BuildCleanupError, OSError) as exc:
                    job.error_code = "cleanup_failed"
                    if job.status != "complete":
                        job.status = "error"
                    job.lease_owner = None
                    job.completed_at = job.completed_at or _now()
                    await session.commit()
                    raise BuildCleanupError("private build cleanup could not be verified") from exc
            if job.status == "complete":
                job.error_code = None
            elif job.status == "building" or job.error_code == "cleanup_failed":
                job.status = "error"
                job.error_code = "worker_interrupted"
                job.lease_owner = None
                job.completed_at = _now()
        await session.commit()
        return len(jobs)


async def _claim(owner: str) -> str | None:
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("build worker database is unavailable")
    async with factory() as session:
        row = (
            await session.execute(
                select(PalimpsestHubBuild)
                .where(PalimpsestHubBuild.status == "queued")
                .order_by(PalimpsestHubBuild.created_at, PalimpsestHubBuild.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.status = "building"
        row.lease_owner = owner
        row.started_at = _now()
        await session.commit()
        return row.id


async def _input_manifest(build_id: str, job_dir: Path) -> dict:
    factory = get_session_factory()
    store = get_blob_store(get_build_worker_settings())
    if factory is None:
        raise RuntimeError("build worker database is unavailable")
    async with factory() as session:
        job = await session.get(PalimpsestHubBuild, build_id)
        if job is None or job.status != "building":
            raise RuntimeError("build job authority is unavailable")
        project = {"project_id": job.project_id}
        inputs = []
        for digest in (job.base_digest, *job.layer_digests):
            row = (
                await session.execute(
                    _visible_filter(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == digest), project)
                )
            ).scalar_one_or_none()
            if row is None or not store.exists(digest):
                raise RuntimeError("a build source is no longer visible")
            inputs.append(row)
        base, *layers = inputs
        if base.kind != "cloud-image" or base.arch != "x86_64" or base.disk_format not in ("raw", "qcow2"):
            raise RuntimeError("invalid build base")
        previous = None
        for layer in layers:
            if layer.kind != "squashfs" or layer.media_type != MEDIA_TYPE_LAYER_SQUASHFS:
                raise RuntimeError("invalid build layer")
            if layer.parent_digest != previous:
                raise RuntimeError("invalid build parent chain")
            if previous is None and (layer.config_json or {}).get("base_image_digest") != base.blob_digest:
                raise RuntimeError("invalid build base chain")
            previous = layer.blob_digest
        if "sha256:" + hashlib.sha256(job.recipe.encode("utf-8")).hexdigest() != job.recipe_digest:
            raise RuntimeError("build recipe changed")
        return {
            "name": job.name,
            "recipe": job.recipe,
            "base": {
                "digest": base.blob_digest,
                "path": str(store.blob_path(base.blob_digest)),
                "arch": base.arch,
                "disk_format": base.disk_format,
            },
            "layers": [
                {
                    "digest": row.blob_digest,
                    "path": str(store.blob_path(row.blob_digest)),
                    "media_type": row.media_type,
                    "parent_digest": row.parent_digest,
                    "base_image_digest": (row.config_json or {}).get("base_image_digest"),
                }
                for row in layers
            ],
        }


def _run_guest_build(python: str, manifest: Path, job_dir: Path, timeout: int) -> dict:
    if not Path(python).is_absolute() or not Path(python).is_file():
        raise RuntimeError("configured build interpreter is not an absolute regular file")
    env = {
        "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": str(job_dir / "config"),
        "PALIMPSEST_STATE_HOME": str(job_dir / "state"),
        "PALIMPSEST_LOG_HOME": str(job_dir / "logs"),
    }
    with (job_dir / "stdout").open("wb") as stdout, (job_dir / "stderr").open("wb") as stderr:
        child = subprocess.Popen(
            [python, "-I", "-m", "palimpsest_local.hub_builder", str(manifest), str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            cwd=job_dir,
            start_new_session=True,
        )
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            # Never reclaim guest state here: the leader's exit does not prove the
            # recorded group is gone. The caller fences cleanup behind _stop_interrupted_builder.
            raise RuntimeError("build timed out") from exc
    if child.returncode:
        raise RuntimeError("isolated builder failed")
    if (job_dir / "stdout").stat().st_size > 1024:
        raise RuntimeError("builder output exceeds protocol limit")
    raw = (job_dir / "stdout").read_bytes()
    result = json.loads(raw)
    if not isinstance(result, dict) or normalize_digest(result.get("digest", "")) is None:
        raise RuntimeError("builder returned an invalid digest")
    if type(result.get("size_bytes")) is not int or result["size_bytes"] <= 0:
        raise RuntimeError("builder returned an invalid size")
    return result


async def _publish_output(build_id: str, owner: str, job_dir: Path, result: dict) -> None:
    factory = get_session_factory()
    store = get_blob_store(get_build_worker_settings())
    if factory is None:
        raise RuntimeError("build worker database is unavailable")
    staged = job_dir / "result.sqsh"
    expected = result["digest"]
    if staged.is_symlink() or not staged.is_file() or staged.stat().st_size != result["size_bytes"]:
        raise RuntimeError("build output is not a stable regular file")
    if staged.stat().st_size > get_build_worker_settings().palimpsest_hub_max_blob_bytes:
        raise RuntimeError("build output exceeds blob limit")
    with staged.open("rb") as handle:
        staged_digest = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    if staged_digest != expected:
        raise RuntimeError("build output digest mismatch")
    finalized = store.promote_file(staged, max_bytes=get_build_worker_settings().palimpsest_hub_max_blob_bytes)
    if finalized.blob_digest != expected:
        raise RuntimeError("build output digest mismatch")
    # The store is content-addressed; verify a reused target before granting access.
    with store.open_read(expected) as handle:
        actual = "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != expected or finalized.size_bytes != result["size_bytes"]:
        raise RuntimeError("build output target is not the verified result")
    async with _locked_blob(store, expected), factory() as session:
        job = await session.get(PalimpsestHubBuild, build_id, with_for_update=True)
        if job is None or job.status != "building" or job.lease_owner != owner:
            raise RuntimeError("build claim was lost")
        parent = job.layer_digests[-1] if job.layer_digests else None
        meta = HubLayerMeta(
            name=job.name,
            kind="squashfs",
            parent_digest=parent,
            base_image_digest=job.base_digest,
            arch="x86_64",
        )
        existing = await session.scalar(select(PalimpsestHubLayer).where(PalimpsestHubLayer.blob_digest == expected))
        if existing is not None:
            if _registration_conflicts(existing, meta):
                raise RuntimeError("output conflicts with an existing layer descriptor")
            await _grant_layer_access(session, expected, {"project_id": job.project_id, "user_id": job.created_by})
        else:
            config = {
                "name": meta.name,
                "kind": meta.kind,
                "ubuntu_base": None,
                "python_version": None,
                "parent_digest": parent,
                "chain_id": None,
                "blob_digest": expected,
                "disk_format": None,
                "arch": "x86_64",
                "os_variant": None,
                "base_image_digest": job.base_digest,
            }
            session.add(
                PalimpsestHubLayer(
                    blob_digest=expected,
                    blob_md5=finalized.blob_md5,
                    size_bytes=finalized.size_bytes,
                    media_type=MEDIA_TYPE_LAYER_SQUASHFS,
                    arch="x86_64",
                    config_digest=compute_config_digest(config),
                    parent_digest=parent,
                    name=job.name,
                    kind="squashfs",
                    config_json=config,
                    project_id=job.project_id,
                    is_published=False,
                    created_by=job.created_by,
                )
            )
        job.output_digest = expected
        job.output_size_bytes = finalized.size_bytes
        job.status = "complete"
        job.completed_at = _now()
        job.lease_owner = None
        await session.commit()


async def process_one_hub_build(owner: str) -> bool:
    build_id = await _claim(owner)
    if build_id is None:
        return False
    job_dir: Path | None = None
    created = False
    cleanup_verified = False
    try:
        store = get_blob_store(get_build_worker_settings())
        job_dir = store.root / "builds" / build_id
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        created = True
        payload = await _input_manifest(build_id, job_dir)
        manifest = job_dir / "input.json"
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        result = await asyncio.to_thread(
            _run_guest_build,
            get_build_worker_settings().palimpsest_hub_builder_python,
            manifest,
            job_dir,
            get_build_worker_settings().palimpsest_hub_build_timeout_seconds,
        )
        await _publish_output(build_id, owner, job_dir, result)
        await asyncio.to_thread(_stop_interrupted_builder, job_dir)
        await asyncio.to_thread(_cleanup_guest, get_build_worker_settings().palimpsest_hub_builder_python, job_dir)
        cleanup_verified = True
    except Exception as exc:
        cleanup_failed = isinstance(exc, BuildCleanupError)
        if created and job_dir is not None and not cleanup_failed:
            try:
                await asyncio.to_thread(_stop_interrupted_builder, job_dir)
                await asyncio.to_thread(
                    _cleanup_guest, get_build_worker_settings().palimpsest_hub_builder_python, job_dir
                )
            except BuildCleanupError:
                cleanup_failed = True
        factory = get_session_factory()
        if factory is None:
            raise
        async with factory() as session:
            job = await session.get(PalimpsestHubBuild, build_id, with_for_update=True)
            if job is not None and (
                (job.status == "building" and job.lease_owner == owner) or (job.status == "complete" and cleanup_failed)
            ):
                if job.status != "complete":
                    job.status = "error"
                    job.lease_owner = None
                    job.completed_at = _now()
                job.error_code = "cleanup_failed" if cleanup_failed else "build_failed"
                await session.commit()
        if cleanup_failed:
            raise BuildCleanupError("builder cleanup could not be verified") from exc
        cleanup_verified = True
    finally:
        if created and job_dir is not None and cleanup_verified:
            try:
                shutil.rmtree(job_dir)
            except OSError as exc:
                factory = get_session_factory()
                if factory is not None:
                    async with factory() as session:
                        job = await session.get(PalimpsestHubBuild, build_id, with_for_update=True)
                        if job is not None:
                            job.error_code = "cleanup_failed"
                            if job.status != "complete":
                                job.status = "error"
                            await session.commit()
                raise BuildCleanupError("private build scratch cleanup failed") from exc
    return True
