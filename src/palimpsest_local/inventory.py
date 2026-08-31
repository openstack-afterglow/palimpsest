"""Single read and mutation surface for Palimpsest local inventory and storage."""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import shutil
import stat as stat_module
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import runtime_dispatch
from .artifact_store import ArtifactStore, ArtifactStoreError
from .digest import digest_file, require_digest
from .errors import ArtifactValidationError, StateError
from .hub import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from .oci_layout import ContentStore
from .oci_store import OCIStore, OCIStoreError
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


def list_vms(roots: StatePaths) -> dict[str, Any]:
    proj_index = _build_project_index(roots)
    aggregation = runtime_dispatch.reconcile(roots=roots)
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
        guest_ip = details["guest_ip"] or ssh_info.get("host")

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


def _normalize_build_record(build_dir: Path, rec: dict[str, Any]) -> dict[str, Any]:
    schema_version = rec.get("schema_version", 1)
    engine = rec.get("engine")
    if not engine:
        engine = "buildkit" if schema_version == 2 else "palimpsestfile"
    if engine != "buildkit":
        engine = "palimpsestfile"

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
    log_available = (build_dir / "console.log").is_file()

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
    log_file = build_dir / "console.log"
    if not log_file.is_file():
        return ""
    text = log_file.read_text(encoding="utf-8", errors="replace")
    if tail <= 0:
        return ""
    lines = text.splitlines()
    if len(lines) > tail:
        return "\n".join(lines[-tail:]) + "\n"
    return text if text.endswith("\n") or not text else text + "\n"


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
