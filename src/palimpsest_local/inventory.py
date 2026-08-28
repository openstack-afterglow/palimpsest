"""Single read and mutation surface for Palimpsest local inventory and storage."""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import platforms
from .digest import digest_file, require_digest
from .errors import ArtifactValidationError, StateError
from .hub import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from .lima import inspect_instance_status
from .oci_layout import ContentStore
from .platforms import preflight
from .runtime import reconcile
from .state import (
    StatePaths,
    fsync_directory,
    init_roots,
    read_json,
    read_tag_record,
    state_root_source,
    write_state_root,
)

BUILD_ID_RE = re.compile(r"^(?:b|bk)-[0-9a-f]{12}$")


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


def list_vms(roots: StatePaths) -> dict[str, Any]:
    proj_index = _build_project_index(roots)
    warnings: list[str] = []
    vms: list[dict[str, Any]] = []

    if not roots.runs.exists():
        return {"vms": [], "warnings": []}

    runs_by_backend: dict[str, list[tuple[str, Any, dict[str, Any]]]] = {}
    for run_dir in sorted(roots.runs.iterdir()):
        if not run_dir.is_dir():
            continue
        name = run_dir.name
        try:
            owner_data = read_json(run_dir / "owner.json") if (run_dir / "owner.json").is_file() else {}
            st_data = read_json(run_dir / "state.json") if (run_dir / "state.json").is_file() else {}
        except Exception as exc:
            warnings.append(f"run '{name}': invalid ledger ({exc})")
            continue
        backend = st_data.get("backend", "kvm")
        runs_by_backend.setdefault(backend, []).append((name, owner_data, st_data))

    preflight_status: dict[str, tuple[bool, str]] = {}
    for backend in runs_by_backend:
        try:
            preflight(backend)
            preflight_status[backend] = (True, "")
        except Exception as exc:
            preflight_status[backend] = (False, str(exc))

    reconciled_map: dict[str, dict[str, Any]] = {}
    successfully_reconciled: set[str] = set()

    for backend in (platforms.BACKEND_KVM, platforms.BACKEND_HVF):
        if backend in runs_by_backend and preflight_status.get(backend, (False, ""))[0]:
            resolved_profile = None
            for _name, _owner, st_data in runs_by_backend[backend]:
                base_info = st_data.get("base") if isinstance(st_data.get("base"), dict) else {}
                arch = st_data.get("base_arch") or base_info.get("arch") or ""
                if arch:
                    try:
                        resolved_profile = platforms.resolve_domain_profile(backend, arch)
                        break
                    except Exception:
                        pass
            if resolved_profile is None:
                if backend == platforms.BACKEND_KVM:
                    fallback_arch = platforms.detect_host().machine
                else:
                    fallback_arch = "aarch64"
                resolved_profile = platforms.resolve_domain_profile(backend, fallback_arch)

            try:
                reconciled_runs, rec_warnings = reconcile(roots=roots, profile=resolved_profile)
                warnings.extend(rec_warnings)
                returned_names = set()
                for r in reconciled_runs:
                    r_name = r["name"]
                    reconciled_map[r_name] = r["state"]
                    successfully_reconciled.add(r_name)
                    returned_names.add(r_name)

                for r_name, _, _ in runs_by_backend[backend]:
                    if r_name not in returned_names:
                        warnings.append(f"run '{r_name}': omitted during reconciliation for backend '{backend}'")
            except Exception as exc:
                warnings.append(f"reconcile failed for backend '{backend}': {exc}")

    if preflight_status.get("lima-vz", (False, ""))[0] and "lima-vz" in runs_by_backend:
        for name, _, st_data in runs_by_backend["lima-vz"]:
            try:
                live_st = inspect_instance_status(name)
                if live_st is not None:
                    st_data["status"] = live_st
                    successfully_reconciled.add(name)
                else:
                    warnings.append(f"run '{name}': Lima status inspection returned None")
            except Exception as exc:
                warnings.append(f"run '{name}': Lima status inspection failed ({exc})")

    for backend, runs_list in runs_by_backend.items():
        is_ok, err_msg = preflight_status.get(backend, (False, "preflight not run"))
        for name, owner_data, st_data in runs_list:
            if not is_ok:
                stale = True
                warnings.append(f"backend '{backend}' for run '{name}' is unavailable on this host ({err_msg})")
            else:
                if name in reconciled_map:
                    st_data = reconciled_map[name]
                stale = name not in successfully_reconciled
            run_id = owner_data.get("run_id") if isinstance(owner_data, dict) else ""
            if not run_id and isinstance(st_data, dict):
                run_id = st_data.get("run_id", "")

            status = st_data.get("status", "unknown")
            base_info = st_data.get("base") if isinstance(st_data.get("base"), dict) else {}
            base_digest = st_data.get("base_digest") or base_info.get("digest") or ""
            base_arch = st_data.get("base_arch") or base_info.get("arch") or ""

            layers_raw = st_data.get("layers", [])
            layers = [
                {"digest": layer.get("digest", ""), "target_dev": layer.get("target_dev", "")}
                for layer in layers_raw
                if isinstance(layer, dict) and "digest" in layer
            ]
            layer_count = len(layers)

            memory_mib = st_data.get("memory_mib")
            vcpus = st_data.get("vcpus")
            network = st_data.get("network")
            ports = st_data.get("ports")
            volumes = st_data.get("volumes")
            ssh_info = st_data.get("ssh")
            if not isinstance(ssh_info, dict):
                ssh_info = {"host": st_data.get("guest_ip"), "port": 22}
            guest_ip = st_data.get("guest_ip") or ssh_info.get("host")
            project_name = proj_index.get(name)
            created_at = st_data.get("created_at")
            updated_at = st_data.get("updated_at")

            vms.append(
                {
                    "name": name,
                    "run_id": run_id,
                    "backend": backend,
                    "status": status,
                    "stale": stale,
                    "base_digest": base_digest,
                    "base_arch": base_arch,
                    "layers": layers,
                    "layer_count": layer_count,
                    "memory_mib": memory_mib,
                    "vcpus": vcpus,
                    "network": network,
                    "ports": ports,
                    "volumes": volumes,
                    "ssh": ssh_info,
                    "guest_ip": guest_ip,
                    "project": project_name,
                    "created_at": created_at,
                    "updated_at": updated_at,
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
    norm_digest = require_digest(digest)
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
                base_d = st.get("base", {}).get("digest") or st.get("base_digest")
                if base_d == norm_digest:
                    referencing_runs.add(name)
                    if name in proj_index:
                        referencing_projects.add(proj_index[name])
                for layer in st.get("layers", []):
                    layer_d = layer.get("digest") if isinstance(layer, dict) else str(layer)
                    if layer_d == norm_digest:
                        referencing_runs.add(name)
                        if name in proj_index:
                            referencing_projects.add(proj_index[name])
            except Exception:
                pass

    names = sorted(referencing_runs | referencing_projects)
    if names:
        raise StateError(f"{norm_digest} is still used by: " + ", ".join(names))

    removed_tags: list[str] = []
    if roots.tags.exists():
        for tag_file in roots.tags.glob("*.json"):
            try:
                tag_rec = read_tag_record(roots, tag_file.stem)
                if tag_rec.digest == norm_digest:
                    removed_tags.append(tag_rec.tag)
            except Exception:
                pass

    store = ContentStore(roots.store)
    freed_bytes = store.size(norm_digest) if store.exists(norm_digest) else 0

    store.blob_path(norm_digest).unlink(missing_ok=True)
    store.metadata_path(norm_digest).unlink(missing_ok=True)
    for tag in removed_tags:
        (roots.tags / f"{tag}.json").unlink(missing_ok=True)

    return {
        "digest": norm_digest,
        "removed_tags": sorted(removed_tags),
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
