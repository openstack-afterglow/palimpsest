"""Single read and mutation surface for Palimpsest local inventory and storage."""

from __future__ import annotations

import dataclasses
import datetime
import ipaddress
import os
import re
import shutil
import stat as stat_module
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import oci_network_status, oci_root_volume_inventory, project_runtime, runtime_dispatch, state
from .artifact_store import ArtifactStore, ArtifactStoreError
from .digest import digest_file, require_digest
from .errors import ArtifactValidationError, StateError
from .hub import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from .oci_layout import ContentStore
from .oci_store import OCIStore, OCIStoreError
from .runtime_types import RuntimeKind
from .state import (
    StatePaths,
    artifact_reference_guard,
    fsync_directory,
    init_roots,
    pinned_owner_directory,
    read_json,
    read_tag_record,
    read_tag_record_snapshot_at,
    state_root_source,
    write_state_root,
)

BUILD_ID_RE = re.compile(r"^(?:b|bk)-[0-9a-f]{12}$")
# Each build engine writes its console to one fixed file in the build directory.
_BUILD_LOG_FILENAMES = {"buildkit": "buildkit.log", "palimpsestfile": "console.log"}
_MAX_BUILD_LOG_TAIL_BYTES = 4 * 1024 * 1024


def _fsync_index_directory(directory_fd: int) -> None:
    os.fsync(directory_fd)


def _unlink_index_entry(filename: str, *, directory_fd: int) -> None:
    os.unlink(filename, dir_fd=directory_fd)


def _run_artifact_digests(record: Any) -> frozenset[str]:
    """Strictly project artifact references from one run ledger."""
    if not isinstance(record, dict):
        raise StateError("run ledger must be an object")
    found: set[str] = set()
    if "base" in record:
        base = record["base"]
        if not isinstance(base, dict):
            raise StateError("run ledger base reference is invalid")
        if "digest" in base:
            if not isinstance(base["digest"], str):
                raise StateError("run ledger base digest is invalid")
            found.add(require_digest(base["digest"]))
    if "base_digest" in record and record["base_digest"] is not None:
        if not isinstance(record["base_digest"], str):
            raise StateError("run ledger base digest is invalid")
        found.add(require_digest(record["base_digest"]))
    if "layers" in record:
        layers = record["layers"]
        if not isinstance(layers, list):
            raise StateError("run ledger layers must be a list")
        for layer in layers:
            if isinstance(layer, str):
                found.add(require_digest(layer))
            elif isinstance(layer, dict) and isinstance(layer.get("digest"), str):
                found.add(require_digest(layer["digest"]))
            else:
                raise StateError("run ledger layer reference is invalid")
    return frozenset(found)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


def storage_report(roots: StatePaths) -> dict[str, Any]:
    disk_total = 0
    disk_free = 0
    if roots.state.exists():
        try:
            usage = shutil.disk_usage(roots.state)
            disk_total = usage.total
            disk_free = usage.free
        except OSError:
            pass

    return {
        "state_root": str(roots.state),
        "source": state_root_source(),
        "directories": {
            "store": _dir_size(roots.store),
            "runs": _dir_size(roots.runs),
            "projects": _dir_size(roots.projects),
            "builds": _dir_size(roots.builds),
            "tags": _dir_size(roots.tags),
            "transfers": _dir_size(roots.transfers),
            "volumes": _dir_size(roots.volumes),
            "oci_root_volumes": _dir_size(roots.oci_root_volumes),
            "build_cache": _dir_size(roots.build_cache),
        },
        "total_state_bytes": _dir_size(roots.state),
        "free_bytes": disk_free,
        "total_bytes": disk_total,
    }


def _build_project_index(roots: StatePaths) -> dict[str, str]:
    proj_index: dict[str, str] = {}
    if not roots.projects.exists():
        return proj_index
    for proj_dir in roots.projects.iterdir():
        if not proj_dir.is_dir():
            continue
        state_file = proj_dir / "state.json"
        if not state_file.is_file():
            continue
        try:
            pdata = read_json(state_file)
            proj_name = pdata.get("project") or proj_dir.name
            services = pdata.get("services", [])
            if isinstance(services, list):
                for s in services:
                    if isinstance(s, dict) and s.get("run_name"):
                        proj_index[s["run_name"]] = proj_name
            elif isinstance(services, dict):
                for _, s in services.items():
                    if isinstance(s, dict) and s.get("run_name"):
                        proj_index[s["run_name"]] = proj_name
        except Exception:
            pass
    return proj_index


def _json_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_projection(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_projection(item) for item in value]
    return value


def list_vms(roots: StatePaths, *, live: bool = True) -> dict[str, Any]:
    """Project managed runs; ``live=False`` reads durable ledgers without backend calls."""
    proj_index = _build_project_index(roots)
    aggregation = runtime_dispatch.reconcile(roots=roots) if live else runtime_dispatch.ps(roots=roots)
    warnings = [
        f"{error.entry_token}: {error.message}" if error.name is None else f"run '{error.name}': {error.message}"
        for error in aggregation.errors
    ]
    vms: list[dict[str, Any]] = []
    for summary in aggregation.summaries:
        name = summary.name
        details = summary.details
        layers = _json_projection(details["layers"])
        ports = _json_projection(details["ports"])
        volumes = _json_projection(details["volumes"])
        ssh_info = _json_projection(details["ssh"])
        # Never substitute the SSH endpoint: a host-loopback forward is not a guest address.
        guest_ip = details["guest_ip"]

        vms.append(
            {
                "name": name,
                "run_id": summary.run_id,
                "runtime_kind": summary.runtime_kind.value,
                "backend": summary.backend.value,
                "status": summary.status,
                "stale": summary.stale,
                "base_digest": details["base_digest"],
                "base_arch": details["base_arch"],
                "layers": layers,
                "layer_count": len(layers),
                "memory_mib": details["memory_mib"],
                "vcpus": details["vcpus"],
                "network": details["network"],
                "ports": ports,
                "volumes": volumes,
                "ssh": ssh_info,
                "guest_ip": guest_ip,
                "project": proj_index.get(name),
                "created_at": details["created_at"],
                "updated_at": details["updated_at"],
            }
        )

    vms.sort(key=lambda x: x["name"])

    unique_warnings: list[str] = []
    seen = set()
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    return {"vms": vms, "warnings": unique_warnings}


def get_vm(roots: StatePaths, name: str) -> dict[str, Any]:
    res = list_vms(roots)
    for vm in res["vms"]:
        if vm["name"] == name:
            return vm
    raise StateError(f"VM '{name}' not found")


def list_artifacts(roots: StatePaths) -> dict[str, Any]:
    proj_index = _build_project_index(roots)
    referenced_by: dict[str, dict[str, set[str]]] = {}

    if roots.runs.exists():
        for run_dir in roots.runs.iterdir():
            if not run_dir.is_dir():
                continue
            name = run_dir.name
            state_file = run_dir / "state.json"
            if not state_file.is_file():
                continue
            try:
                st = read_json(state_file)
                base_d = st.get("base", {}).get("digest") or st.get("base_digest")
                if base_d:
                    ref_entry = referenced_by.setdefault(base_d, {"runs": set(), "projects": set()})
                    ref_entry["runs"].add(name)
                    if name in proj_index:
                        ref_entry["projects"].add(proj_index[name])
                for layer in st.get("layers", []):
                    layer_d = layer.get("digest") if isinstance(layer, dict) else str(layer)
                    if layer_d:
                        ref_entry = referenced_by.setdefault(layer_d, {"runs": set(), "projects": set()})
                        ref_entry["runs"].add(name)
                        if name in proj_index:
                            ref_entry["projects"].add(proj_index[name])
            except Exception:
                pass

    tags_by_digest: dict[str, list[dict[str, Any]]] = {}
    if roots.tags.exists():
        for tag_file in roots.tags.glob("*.json"):
            try:
                tag_rec = read_tag_record(roots, tag_file.stem)
                tags_by_digest.setdefault(tag_rec.digest, []).append(dataclasses.asdict(tag_rec))
            except Exception:
                pass

    store = ContentStore(roots.store)
    artifacts: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    if (roots.store / "metadata").exists():
        for meta_file in sorted((roots.store / "metadata").glob("*.json")):
            digest = f"sha256:{meta_file.stem}"
            try:
                meta = store.read_metadata(digest)
            except Exception:
                continue

            kind = meta.get("kind")
            size_bytes = meta.get("size")
            if size_bytes is None:
                size_bytes = store.size(digest) if store.exists(digest) else 0

            tags = [t for t in tags_by_digest.get(digest, []) if isinstance(t, dict)]
            ref_dict = {
                "runs": sorted(referenced_by.get(digest, {}).get("runs", set())),
                "projects": sorted(referenced_by.get(digest, {}).get("projects", set())),
            }

            art_record = {
                **meta,
                "digest": digest,
                "kind": kind or "unknown",
                "size_bytes": size_bytes,
                "tags": tags,
                "referenced_by": ref_dict,
            }

            artifacts.append(art_record)

            if kind == KIND_CLOUD_IMAGE:
                images.append(art_record)
            elif kind == "squashfs" or meta.get("media_type") == MEDIA_TYPE_LAYER_SQUASHFS:
                layers.append(art_record)
            else:
                unknown.append(art_record)

    return {
        "artifacts": artifacts,
        "images": images,
        "layers": layers,
        "unknown": unknown,
    }


def _build_engine(rec: dict[str, Any]) -> str:
    engine = rec.get("engine") or ("buildkit" if rec.get("schema_version", 1) == 2 else "palimpsestfile")
    return "buildkit" if engine == "buildkit" else "palimpsestfile"


def _normalize_build_record(build_dir: Path, rec: dict[str, Any]) -> dict[str, Any]:
    schema_version = rec.get("schema_version", 1)
    engine = _build_engine(rec)

    build_id = rec.get("build_id") or build_dir.name
    status = rec.get("status", "unknown")
    finished_at = rec.get("finished_at")
    created_at = rec.get("created_at")
    started_at = rec.get("started_at")

    duration_ms = None
    timings_ms = rec.get("timings_ms") if isinstance(rec.get("timings_ms"), dict) else {}

    if schema_version == 1:
        if not started_at:
            started_at = created_at
        if finished_at and created_at:
            try:
                dt_fin = datetime.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                dt_cre = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                duration_ms = int((dt_fin - dt_cre).total_seconds() * 1000)
            except Exception:
                pass
    else:
        if "total" in timings_ms and isinstance(timings_ms["total"], (int, float)):
            duration_ms = int(timings_ms["total"])
        elif finished_at and started_at:
            try:
                dt_fin = datetime.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                dt_sta = datetime.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                duration_ms = int((dt_fin - dt_sta).total_seconds() * 1000)
            except Exception:
                pass

        if not started_at and finished_at and duration_ms is not None:
            try:
                dt_fin = datetime.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                dt_sta = dt_fin - datetime.timedelta(milliseconds=duration_ms)
                started_at = dt_sta.isoformat().replace("+00:00", "Z")
            except Exception:
                pass

    output_tags = rec.get("output_tags")
    if output_tags is None:
        out_tag = rec.get("output_tag")
        output_tags = [out_tag] if out_tag else []

    output_digest = rec.get("output_digest") or rec.get("runtime_block_digest") or rec.get("output_oci_archive_digest")
    base_digest = rec.get("base_digest") or rec.get("runtime_base_digest")
    parent_digests = rec.get("parent_digests", [])
    platform = rec.get("platform")
    cache_source = rec.get("cache_source")
    log_available = (build_dir / _BUILD_LOG_FILENAMES[engine]).is_file()

    return {
        "build_id": build_id,
        "engine": engine,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "output_tags": output_tags,
        "output_digest": output_digest,
        "base_digest": base_digest,
        "parent_digests": parent_digests,
        "platform": platform,
        "cache_source": cache_source,
        "timings_ms": timings_ms,
        "log_available": log_available,
    }


def list_builds(roots: StatePaths, *, limit: int = 100) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not roots.builds.exists():
        return items

    for build_dir in roots.builds.iterdir():
        if not build_dir.is_dir():
            continue
        if BUILD_ID_RE.fullmatch(build_dir.name) is None:
            continue
        rec_file = build_dir / "record.json"
        if not rec_file.is_file():
            continue
        try:
            rec = read_json(rec_file)
            items.append(_normalize_build_record(build_dir, rec))
        except Exception:
            pass

    items.sort(key=lambda b: b.get("started_at") or b.get("finished_at") or "", reverse=True)
    return items[:limit]


def get_build(roots: StatePaths, build_id: str) -> dict[str, Any]:
    if not isinstance(build_id, str) or BUILD_ID_RE.fullmatch(build_id) is None:
        raise StateError(f"invalid build id: {build_id!r}")
    build_dir = (roots.builds / build_id).resolve()
    if not build_dir.is_relative_to(roots.builds.resolve()):
        raise StateError(f"invalid build id: {build_id!r}")
    rec_file = build_dir / "record.json"
    if not rec_file.is_file():
        raise StateError(f"build '{build_id}' not found")
    rec = read_json(rec_file)
    return _normalize_build_record(build_dir, rec)


def build_log(roots: StatePaths, build_id: str, *, tail: int = 400) -> str:
    if not isinstance(build_id, str) or BUILD_ID_RE.fullmatch(build_id) is None:
        raise StateError(f"invalid build id: {build_id!r}")
    build_dir = (roots.builds / build_id).resolve()
    if not build_dir.is_relative_to(roots.builds.resolve()):
        raise StateError(f"invalid build id: {build_id!r}")
    rec_file = build_dir / "record.json"
    if not rec_file.is_file():
        raise StateError(f"build '{build_id}' not found")
    log_file = build_dir / _BUILD_LOG_FILENAMES[_build_engine(read_json(rec_file))]
    if tail <= 0 or not log_file.is_file():
        return ""
    with log_file.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - _MAX_BUILD_LOG_TAIL_BYTES)
        stream.seek(start)
        data = stream.read(size - start)
    if start:
        # Drop the partial first line of a bounded window.
        data = data[data.find(b"\n") + 1 :]
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "".join(line + "\n" for line in lines[-tail:])


def remove_artifact(roots: StatePaths, digest: str, *, force: bool = False) -> dict[str, Any]:
    canonical_roots = StatePaths(config=roots.config.resolve(), state=roots.state.resolve())
    norm_digest = require_digest(digest)
    with artifact_reference_guard(canonical_roots):
        return _remove_artifact_locked(canonical_roots, norm_digest, force=force)


def _remove_artifact_locked(roots: StatePaths, norm_digest: str, *, force: bool) -> dict[str, Any]:
    proj_index = _build_project_index(roots)
    referencing_runs: set[str] = set()
    referencing_projects: set[str] = set()

    if roots.runs.exists():
        for run_dir in roots.runs.iterdir():
            if not run_dir.is_dir():
                continue
            name = run_dir.name
            state_file = run_dir / "state.json"
            if not state_file.is_file():
                continue
            try:
                st = read_json(state_file)
                if norm_digest in _run_artifact_digests(st):
                    referencing_runs.add(name)
                    if name in proj_index:
                        referencing_projects.add(proj_index[name])
            except Exception:
                raise StateError("cannot prove artifact is unreferenced because a run ledger is invalid") from None

    names = sorted(referencing_runs | referencing_projects)
    if names:
        raise StateError(f"{norm_digest} is still used by: " + ", ".join(names))

    removed_tags: list[tuple[str, str, tuple[int, int, int, int, int]]] = []
    with pinned_owner_directory(roots.tags) as tags_fd, ExitStack() as index_authorities:
        assert tags_fd is not None
        for tag_filename in sorted(name for name in os.listdir(tags_fd) if name.endswith(".json")):
            try:
                tag_name = tag_filename.removesuffix(".json")
                tag_rec, tag_identity = read_tag_record_snapshot_at(tags_fd, tag_name)
                if tag_rec.digest == norm_digest:
                    removed_tags.append((tag_rec.tag, tag_filename, tag_identity))
            except Exception:
                raise StateError("cannot prove artifact is unreferenced because a tag record is invalid") from None

        physical = ArtifactStore(roots.store)
        derived = OCIStore(roots)
        metadata_fd: int | None = None
        metadata_present = False
        metadata_identity: tuple[int, int] | None = None
        metadata_filename = f"{norm_digest.split(':', 1)[1]}.json"
        deleted_tags: list[str] = []

        def verify_tag_entry(tag_filename: str, tag_identity: tuple[int, int, int, int, int]) -> None:
            current = os.stat(tag_filename, dir_fd=tags_fd, follow_symlinks=False)
            current_identity = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
            )
            if (
                not stat_module.S_ISREG(current.st_mode)
                or current.st_uid != os.geteuid()
                or current_identity != tag_identity
            ):
                raise StateError("tag record changed before removal")

        def verify_tag_entries() -> None:
            for _, tag_filename, tag_identity in removed_tags:
                verify_tag_entry(tag_filename, tag_identity)

        def guard_retention_and_indexes() -> None:
            nonlocal metadata_fd, metadata_identity, metadata_present
            derived.assert_artifact_unleased(norm_digest)
            verify_tag_entries()
            metadata_fd = index_authorities.enter_context(
                pinned_owner_directory(roots.store / "metadata", missing_ok=True)
            )
            if metadata_fd is None:
                return
            try:
                entry = os.stat(metadata_filename, dir_fd=metadata_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat_module.S_ISREG(entry.st_mode) or entry.st_uid != os.geteuid():
                raise StateError("artifact metadata entry is unsafe")
            metadata_present = True
            metadata_identity = (entry.st_dev, entry.st_ino)

        def finalize_indexes() -> None:
            if metadata_present:
                assert metadata_fd is not None
                current = os.stat(metadata_filename, dir_fd=metadata_fd, follow_symlinks=False)
                if (
                    not stat_module.S_ISREG(current.st_mode)
                    or current.st_uid != os.geteuid()
                    or (current.st_dev, current.st_ino) != metadata_identity
                ):
                    raise StateError("artifact metadata entry changed before removal")
                os.unlink(metadata_filename, dir_fd=metadata_fd)
                _fsync_index_directory(metadata_fd)
            for tag, tag_filename, tag_identity in removed_tags:
                try:
                    verify_tag_entry(tag_filename, tag_identity)
                except (OSError, StateError):
                    try:
                        current_record, current_identity = read_tag_record_snapshot_at(tags_fd, tag)
                    except StateError:
                        try:
                            os.stat(tag_filename, dir_fd=tags_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        raise
                    if current_record.digest != norm_digest:
                        continue
                    verify_tag_entry(tag_filename, current_identity)
                _unlink_index_entry(tag_filename, directory_fd=tags_fd)
                deleted_tags.append(tag)
            if deleted_tags:
                _fsync_index_directory(tags_fd)

        try:
            freed_bytes = physical.delete_blob(
                norm_digest,
                retention_guard=guard_retention_and_indexes,
                finalize=finalize_indexes,
            )
        except OCIStoreError as exc:
            if exc.code == "oci-store-in-use":
                raise StateError(f"{norm_digest} is retained by a durable OCI lease") from None
            raise StateError("cannot prove artifact is unleased because OCI retention metadata is invalid") from None
        except ArtifactStoreError:
            raise StateError("artifact physical deletion failed descriptor verification") from None

    return {
        "digest": norm_digest,
        "removed_tags": sorted(deleted_tags),
        "freed_bytes": freed_bytes,
    }


def import_cloud_image(
    roots: StatePaths,
    path: Path,
    *,
    disk_format: str,
    arch: str,
    os_variant: str | None = None,
) -> dict[str, Any]:
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise ArtifactValidationError(f"image path not found: {file_path}")
    image_digest = digest_file(file_path)
    store = ContentStore(roots.store)
    store.ingest_file(file_path, expected_digest=image_digest)
    metadata = {
        "kind": KIND_CLOUD_IMAGE,
        "disk_format": disk_format,
        "arch": arch,
        "os_variant": os_variant,
        "name": file_path.name,
    }
    store.write_metadata(image_digest, metadata)
    return {
        "digest": image_digest,
        "path": str(file_path),
        "metadata": metadata,
    }


def set_state_root(roots: StatePaths, destination: Path) -> dict[str, Any]:
    if state_root_source() == "env":
        raise StateError(
            "cannot set state root while PALIMPSEST_STATE_HOME is set; unset PALIMPSEST_STATE_HOME to allow storage configuration"
        )
    dest = Path(destination)
    if not dest.is_absolute():
        raise StateError(f"state root destination must be an absolute path: {destination}")

    if not dest.exists():
        raise StateError(f"state root destination does not exist: {destination}")
    if not dest.is_dir():
        raise StateError(f"state root destination must be a directory: {destination}")

    entries = list(dest.iterdir())
    has_store = (dest / "store").is_dir()
    if len(entries) > 0 and not has_store:
        raise StateError(f"state root destination is not empty and lacks a store directory: {destination}")

    write_state_root(roots, dest)
    init_roots({"XDG_CONFIG_HOME": str(roots.config.parent)})
    return {
        "previous_root": str(roots.state),
        "new_root": str(dest.resolve()),
        "source": state_root_source(),
    }


def move_state_root(roots: StatePaths, destination: Path, *, keep_source: bool = False) -> dict[str, Any]:
    if state_root_source() == "env":
        raise StateError(
            "cannot move state root while PALIMPSEST_STATE_HOME is set; unset PALIMPSEST_STATE_HOME to allow storage relocation"
        )
    dest = Path(destination)
    if not dest.is_absolute():
        raise StateError(f"state root destination must be an absolute path: {destination}")

    if dest.exists():
        if not dest.is_dir():
            raise StateError(f"destination must be a directory: {dest}")
        if len(list(dest.iterdir())) > 0:
            raise StateError(f"destination directory is not empty: {dest}")

    runs: list[str] = []
    if roots.runs.exists():
        runs = [d.name for d in roots.runs.iterdir() if d.is_dir()]
    projects: list[str] = []
    if roots.projects.exists():
        projects = [d.name for d in roots.projects.iterdir() if d.is_dir()]

    names = sorted(set(runs) | set(projects))
    if names:
        raise StateError(
            "relocating the state root requires no runs and no projects; remove them first: " + ", ".join(names)
        )

    incoming = dest.parent / f"{dest.name}.incoming-{os.getpid()}"
    if incoming.exists():
        shutil.rmtree(incoming)

    old_root = roots.state
    try:
        shutil.copytree(old_root, incoming, symlinks=True)
        fsync_directory(incoming.parent)

        if dest.exists():
            dest.rmdir()

        os.replace(incoming, dest)
    except Exception:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise

    write_state_root(roots, dest)
    init_roots({"XDG_CONFIG_HOME": str(roots.config.parent)})

    if not keep_source and old_root.exists() and old_root.resolve() != dest.resolve():
        shutil.rmtree(old_root)

    return {
        "previous_root": str(old_root),
        "new_root": str(dest.resolve()),
        "source": state_root_source(),
    }


# Volume and network projections. Statuses describe ledger declarations, retention, or one fixed-file
# observation, never a mounted filesystem or an active network; Lima system disks are outside this
# state authority. Run attribution uses the durable ledger projection, so no backend is contacted.
_PROJECT_WARNING = "Project volume metadata is unavailable or inconsistent"
_RUN_WARNING = "Some VM resource metadata is unavailable or inconsistent"
_DISK_WARNING = "Some managed VM disk observations are unavailable or inconsistent"
_NETWORK_WARNING = "Some configured network metadata is unavailable or inconsistent"
_OCI_NETWORK_WARNING = (
    "OCI network status is unavailable; the exact run has no committed domain plan or its plan is invalid"
)
_METADATA_ERRORS = (StateError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError)


def _vms(roots: StatePaths, result: dict | None, warnings: set[str]) -> list[dict[str, Any]]:
    try:
        result = list_vms(roots, live=False) if result is None else result
        if not isinstance(result, dict) or not isinstance(result.get("vms"), list):
            raise StateError("invalid VM inventory")
        if result.get("warnings"):
            warnings.add(_RUN_WARNING)
        vms = []
        identities = set()
        for vm in result["vms"]:
            try:
                if not isinstance(vm, dict):
                    raise StateError("invalid VM projection")
                name = project_runtime._valid_name(vm.get("name"), "run", run=True)
                project_runtime._valid_run_id(vm.get("run_id"), "run")
                project_runtime._valid_backend(vm.get("backend"), "backend")
                if vm.get("runtime_kind") not in {"cloud-image", "oci-root"} or name in identities:
                    raise StateError("invalid VM projection")
                identities.add(name)
                vms.append(vm)
            except _METADATA_ERRORS:
                warnings.add(_RUN_WARNING)
        return sorted(vms, key=lambda vm: (vm["name"], vm["run_id"]))
    except _METADATA_ERRORS:
        warnings.add(_RUN_WARNING)
        return []


def _projects(roots: StatePaths, warnings: set[str]) -> list[project_runtime.ProjectState]:
    """Use the existing decoder through bounded, descriptor-pinned JSON reads."""
    projects = []
    try:
        with state.pinned_owner_directory(roots.state.resolve()) as root_fd:
            assert root_fd is not None
            try:
                before = os.stat("projects", dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return []
            if not stat_module.S_ISDIR(before.st_mode):
                raise StateError("invalid project namespace")
            projects_fd = os.open(
                "projects", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=root_fd
            )
            try:
                opened = os.fstat(projects_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise StateError("project namespace changed")
                if opened.st_uid != os.geteuid() or opened.st_mode & 0o022:
                    raise StateError("invalid project namespace")
                for name in sorted(os.listdir(projects_fd)):
                    child_fd = None
                    try:
                        project_runtime._valid_name(name, "project")
                        child_before = os.stat(name, dir_fd=projects_fd, follow_symlinks=False)
                        if not stat_module.S_ISDIR(child_before.st_mode):
                            raise StateError("invalid project entry")
                        child_fd = os.open(
                            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=projects_fd
                        )
                        child = os.fstat(child_fd)
                        if (child.st_dev, child.st_ino) != (child_before.st_dev, child_before.st_ino):
                            raise StateError("project changed")
                        if child.st_uid != os.geteuid() or child.st_mode & 0o022:
                            raise StateError("invalid project entry")
                        file_stat = os.stat("state.json", dir_fd=child_fd, follow_symlinks=False)
                        if file_stat.st_uid != os.geteuid() or file_stat.st_mode & 0o022 or file_stat.st_nlink != 1:
                            raise StateError("invalid project ledger")
                        payload = state._read_pinned_json_object(child_fd, "state.json")
                        record = project_runtime._decode_state(payload, name)
                        current = os.stat(name, dir_fd=projects_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != (child.st_dev, child.st_ino):
                            raise StateError("project changed")
                        projects.append(record)
                    except _METADATA_ERRORS:
                        warnings.add(_PROJECT_WARNING)
                    finally:
                        if child_fd is not None:
                            os.close(child_fd)
                current = os.stat("projects", dir_fd=root_fd, follow_symlinks=False)
                after = os.fstat(projects_fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or (
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ) != (opened.st_mtime_ns, opened.st_ctime_ns):
                    raise StateError("project namespace changed")
            finally:
                os.close(projects_fd)
    except _METADATA_ERRORS:
        warnings.add(_PROJECT_WARNING)
        return []
    return projects


def _owners(projects: list[project_runtime.ProjectState], warnings: set[str]) -> dict[tuple[str, str, str], str]:
    owners: dict[tuple[str, str, str], str] = {}
    ambiguous = set()
    for project in projects:
        for service in project.services.values():
            key = (service.run_name, service.run_id, service.backend)
            if key in owners:
                ambiguous.add(key)
                warnings.add(_PROJECT_WARNING)
            else:
                owners[key] = project.project
    return {key: owner for key, owner in owners.items() if key not in ambiguous}


def _volume(
    volume_id: str,
    name: str,
    kind: str,
    *,
    project: str | None = None,
    backend: str | None = None,
    status: str,
    size_bytes: int | None = None,
    source: str,
    retention_policy: str | None = None,
) -> dict[str, Any]:
    return {
        "id": volume_id,
        "name": name,
        "kind": kind,
        "project": project,
        "backend": backend,
        "status": status,
        "size_bytes": size_bytes,
        "attachments": [],
        "source": source,
        "retention_policy": retention_policy,
    }


def _overlay_size(roots: StatePaths, vm: dict[str, Any]) -> int:
    """Stat only the fixed managed overlay, with the run identity pinned."""
    with state.pinned_owner_directory(roots.state.resolve() / "runs") as runs_fd:
        assert runs_fd is not None
        before = os.stat(vm["name"], dir_fd=runs_fd, follow_symlinks=False)
        run_fd = os.open(vm["name"], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=runs_fd)
        try:
            opened = os.fstat(run_fd)
            if opened.st_uid != os.geteuid() or opened.st_mode & 0o022:
                raise StateError("invalid run directory")
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise StateError("run changed")
            info = os.stat("overlay.qcow2", dir_fd=run_fd, follow_symlinks=False)
            if not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise StateError("invalid managed overlay")
            owner, payload = state._read_pinned_run_payloads(runs_fd, vm["name"], expected_directory=opened)
            record = state._normalize_run_dispatch_record(vm["name"], owner, payload)
            if (
                record.run_id != vm["run_id"]
                or record.dispatch_key.backend.value != vm["backend"]
                or (record.dispatch_key.runtime_kind is not RuntimeKind.CLOUD_IMAGE)
            ):
                raise StateError("run identity changed")
            current = os.stat("overlay.qcow2", dir_fd=run_fd, follow_symlinks=False)
            # The overlay is the live guest disk: pin its identity, not its size or timestamps.
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise StateError("overlay changed")
            return current.st_size
        finally:
            os.close(run_fd)


def list_volumes(roots: StatePaths, *, vms_result: dict | None = None) -> dict[str, Any]:
    """Enumerate ledger volumes, including preserved volumes without any VM."""
    warnings: set[str] = set()
    projects = _projects(roots, warnings)
    owners = _owners(projects, warnings)
    volumes: dict[str, dict[str, Any]] = {}
    for project in projects:
        for volume in project.volumes.values():
            key = f"project:{project.project}:{volume.backend}:{volume.name}"
            volumes[key] = _volume(
                key,
                volume.name,
                "project",
                project=project.project,
                backend=volume.backend,
                status="declared",
                size_bytes=volume.size_bytes,
                source="project-ledger",
            )
    try:
        for root in oci_root_volume_inventory.root_volumes(roots)["volumes"]:
            key = f"oci-root:{root['volume_id']}"
            row = _volume(
                key,
                root["volume_id"],
                "oci-root",
                backend="kvm",
                status=root["status"],
                size_bytes=root["size_bytes"],
                source="oci-root-volume-ledger",
                retention_policy=root["retention_policy"],
            )
            if root["attachment"] is not None:
                row["attachments"].append({**root["attachment"], "mount_path": "/", "read_only": False})
            volumes[key] = row
    except _METADATA_ERRORS:
        warnings.add("OCI root-volume metadata is unavailable or inconsistent")

    for vm in _vms(roots, vms_result, warnings):
        project = owners.get((vm["name"], vm["run_id"], vm["backend"]))
        raw_volumes = vm.get("volumes", [])
        if not isinstance(raw_volumes, list):
            warnings.add(_RUN_WARNING)
            raw_volumes = []
        for attached in raw_volumes:
            try:
                if not isinstance(attached, dict):
                    raise StateError("invalid volume")
                name = project_runtime._valid_name(attached.get("name"), "volume")
                mount_path = attached.get("mount_path")
                if mount_path is not None and (
                    not isinstance(mount_path, str) or not mount_path.startswith("/") or "\x00" in mount_path
                ):
                    raise StateError("invalid guest mount path")
                read_only = attached.get("read_only", False)
                if type(read_only) is not bool:
                    raise StateError("invalid attachment")
                project_key = f"project:{project}:{vm['backend']}:{name}"
                key = (
                    project_key
                    if project is not None and project_key in volumes
                    else f"run-volume:{vm['run_id']}:{name}"
                )
                if key not in volumes:
                    volumes[key] = _volume(
                        key,
                        name,
                        "project",
                        project=project,
                        backend=vm["backend"],
                        status="declared-attached",
                        source="run-ledger",
                    )
                row = volumes[key]
                row["status"] = "declared-attached"
                attachment = {
                    "name": vm["name"],
                    "run_id": vm["run_id"],
                    "mount_path": mount_path,
                    "read_only": read_only,
                }
                if attachment not in row["attachments"]:
                    row["attachments"].append(attachment)
            except _METADATA_ERRORS:
                warnings.add(_RUN_WARNING)
        if vm["runtime_kind"] != "cloud-image":
            continue
        key = f"vm-disk:{vm['run_id']}"
        row = _volume(
            key,
            vm["name"],
            "vm-disk",
            project=project,
            backend=vm["backend"],
            status="unobservable",
            source="run-ledger",
        )
        row["file_size_bytes"] = None
        row["attachments"] = [{"name": vm["name"], "run_id": vm["run_id"], "mount_path": "/", "read_only": False}]
        if vm["backend"] in {"kvm", "libvirt-hvf"}:
            try:
                row["file_size_bytes"] = _overlay_size(roots, vm)
                row["source"] = "managed-overlay-stat"
                row["status"] = "observed-file"
            except _METADATA_ERRORS:
                row["status"] = "unavailable"
                warnings.add(_DISK_WARNING)
        volumes[key] = row
    for row in volumes.values():
        row["attachments"].sort(
            key=lambda item: (item["name"], item["run_id"] or "", item["mount_path"] or "", item["read_only"])
        )
    return {
        "volumes": [volumes[key] for key in sorted(volumes)],
        "warnings": sorted(warnings),
        "classification": "local-metadata-observation",
    }


def _address(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "%" in value:
        raise StateError("invalid network address")
    return str(ipaddress.ip_address(value))


def _ports(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise StateError("invalid port metadata")
    ports = []
    for port in value:
        if not isinstance(port, dict) or not {"host_ip", "host_port", "guest_port", "protocol"}.issubset(port):
            raise StateError("invalid port metadata")
        if any(type(port[field]) is not int or not 1 <= port[field] <= 65535 for field in ("host_port", "guest_port")):
            raise StateError("invalid port metadata")
        if port["protocol"] not in {"tcp", "udp"}:
            raise StateError("invalid port metadata")
        host_ip = _address(port["host_ip"])
        if host_ip is None:
            raise StateError("invalid port metadata")
        ports.append(
            {
                "host_ip": host_ip,
                "host_port": port["host_port"],
                "guest_port": port["guest_port"],
                "protocol": port["protocol"],
            }
        )
    return sorted(ports, key=lambda port: (port["host_ip"], port["host_port"], port["guest_port"], port["protocol"]))


def _network(
    key: str, name: str, kind: str, backend: str, *, mode: str | None, status: str, source: str
) -> dict[str, Any]:
    return {
        "id": key,
        "name": name,
        "kind": kind,
        "backend": backend,
        "mode": mode,
        "subnet": None,
        "gateway": None,
        "status": status,
        "source": source,
        "external": None,
        "attachments": [],
    }


def list_networks(roots: StatePaths, *, vms_result: dict | None = None) -> dict[str, Any]:
    """Configured networks, never evidence of a running listener or bridge."""
    warnings: set[str] = set()
    vms = _vms(roots, vms_result, warnings)
    # ps may omit any VM whose public fields or committed plan are malformed.
    # Retain validated identities, never raw network values or port metadata,
    # so a projection refusal cannot become an empty network inventory.
    projected = {(vm["name"], vm["run_id"], vm["backend"], vm["runtime_kind"]) for vm in vms}
    unavailable_runs = []
    oci_runs = {vm["name"]: (vm["run_id"], vm["backend"]) for vm in vms if vm["runtime_kind"] == "oci-root"}
    try:
        snapshots, errors = state.enumerate_run_snapshots(roots)
        if errors:
            warnings.add(_RUN_WARNING)
        for snapshot in snapshots:
            record = snapshot.record
            if record.dispatch_key.runtime_kind is RuntimeKind.OCI_ROOT:
                oci_runs[record.name] = (record.run_id, record.dispatch_key.backend.value)
            elif (
                record.name,
                record.run_id,
                record.dispatch_key.backend.value,
                record.dispatch_key.runtime_kind.value,
            ) not in projected:
                unavailable_runs.append(record)
    except _METADATA_ERRORS:
        warnings.add(_RUN_WARNING)
    networks: dict[str, dict[str, Any]] = {}
    for record in unavailable_runs:
        warnings.add(_NETWORK_WARNING)
        key = f"network-unavailable:{record.run_id}"
        networks[key] = _network(
            key,
            record.name,
            "unknown",
            record.dispatch_key.backend.value,
            mode=None,
            status="unavailable",
            source="run-ledger",
        )
    for name, (run_id, backend) in sorted(oci_runs.items()):
        key = f"oci-network:{run_id}"
        row = _network(key, name, "oci-root", backend, mode=None, status="unavailable", source="committed-domain-plan")
        try:
            observation = oci_network_status.network_status(roots, name)
            if observation["run"] != {"name": name, "run_id": run_id}:
                raise StateError("OCI identity changed")
            guest = observation["guest"]
            row.update(
                mode=observation["mode"],
                subnet=observation["subnet"],
                gateway=guest["gateway"],
                external=observation["exposure"]["external"],
                status="isolated" if observation["mode"] == "none" else "configured",
            )
            row["attachments"] = [
                {"name": name, "run_id": run_id, "guest_ip": guest["address"], "ports": observation["published_ports"]}
            ]
        except _METADATA_ERRORS:
            warnings.add(_OCI_NETWORK_WARNING)
        networks[key] = row
    for vm in vms:
        if vm["runtime_kind"] == "oci-root":
            continue
        backend = vm["backend"]
        try:
            network = vm.get("network")
            if network is None:
                raise StateError("network is unobservable")
            if not isinstance(network, str):
                raise StateError("invalid network")
            isolated = network == "none"
            per_vm = isolated or backend == "libvirt-hvf"
            if backend == "lima-vz":
                if network in {"default", "vzNAT"}:
                    network = "vzNAT"
                elif network.startswith("lima:"):
                    name = network.removeprefix("lima:")
                    project_runtime._valid_name(name, "Lima network", run=True)
                elif not isolated:
                    raise StateError("invalid Lima network")
                kind = "lima"
            else:
                project_runtime._valid_name(network, "libvirt network")
                kind = "user-hostfwd" if backend == "libvirt-hvf" else "libvirt"
            key = f"network:{backend}:{vm['run_id']}" if per_vm else f"network:{backend}:{network}"
            row = _network(
                key,
                network,
                kind,
                backend,
                mode="none" if isolated else "configured",
                status="isolated" if isolated else "configured",
                source="run-ledger",
            )
            attachment = {
                "name": vm["name"],
                "run_id": vm["run_id"],
                # Lima records the guest's first global address (user-mode eth0), not one on this network.
                "guest_ip": None if isolated or backend == "lima-vz" else _address(vm.get("guest_ip")),
                "ports": _ports(vm.get("ports", [])),
            }
            if isolated and attachment["ports"]:
                raise StateError("isolated network has inconsistent exposure")
            if key not in networks:
                networks[key] = row
            networks[key]["attachments"].append(attachment)
        except _METADATA_ERRORS:
            warnings.add(_NETWORK_WARNING)
            key = f"network-unavailable:{vm['run_id']}"
            networks[key] = _network(
                key, vm["name"], "unknown", backend, mode=None, status="unavailable", source="run-ledger"
            )
    for row in networks.values():
        row["attachments"].sort(key=lambda item: (item["name"], item["run_id"] or ""))
    return {
        "networks": [networks[key] for key in sorted(networks)],
        "warnings": sorted(warnings),
        "classification": "configured-network-observation",
    }
