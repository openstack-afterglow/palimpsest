"""Single read and mutation surface for Palimpsest local inventory and storage."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import ipaddress
import math
import os
import re
import shutil
import stat as stat_module
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import (
    host_inventory,
    host_vm_control,
    named_volumes,
    oci_network_status,
    oci_root_volume_inventory,
    platforms,
    project_runtime,
    retained_instances,
    runtime_dispatch,
    state,
    vm_access,
)
from .artifact_store import ArtifactStore, ArtifactStoreError
from .digest import digest_file, require_digest
from .errors import ArtifactValidationError, InvalidDigestError, StateError
from .hub import KIND_CLOUD_IMAGE, MEDIA_TYPE_LAYER_SQUASHFS
from .oci_layout import DISK_FORMAT_MEDIA_TYPES, ContentStore
from .oci_store import OCIStore, OCIStoreError
from .remote_artifacts import HubConnection, RemoteArtifacts
from .run_source import list_retained_oci_sources
from .runtime_types import (
    ExpectedRunIdentity,
    LifecycleResult,
    ProcessCapabilityError,
    RuntimeCapabilityError,
    RuntimeKind,
)
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
from .web_operations import OperationManager

BUILD_ID_RE = re.compile(r"^(?:b|bk)-[0-9a-f]{12}$")
# Each build engine writes its console to one fixed file in the build directory.
_BUILD_LOG_FILENAMES = {"buildkit": "buildkit.log", "palimpsestfile": "console.log"}
_MAX_BUILD_LOG_TAIL_BYTES = 4 * 1024 * 1024


def _web_string(body: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\0\r\n"):
        raise StateError(f"missing or invalid field '{key}'")
    return value


def _web_strings(body: dict[str, Any], key: str) -> list[str]:
    values = body.get(key, [])
    if (
        not isinstance(values, list)
        or len(values) > 128
        or any(not isinstance(value, str) or "\0" in value for value in values)
    ):
        raise StateError(f"invalid field '{key}'")
    return values


def build_image(roots: StatePaths, operations: OperationManager, body: dict[str, Any]) -> dict[str, Any]:
    """Start the canonical local-cache Dockerfile pipeline, never a publication."""
    if set(body) - {"context", "dockerfile", "dockerfile_text", "tag", "platform", "target", "build_args", "offline"}:
        raise StateError("unsupported build field")
    context = Path(_web_string(body, "context", required=True)).expanduser()
    if not context.is_absolute() or not context.is_dir():
        raise StateError("context must be an existing absolute host directory")
    tag = _web_string(body, "tag", required=True)
    platform = body.get("platform", "linux/amd64")
    if not isinstance(platform, str) or platform not in {"linux/amd64", "linux/arm64"}:
        raise StateError("platform must be linux/amd64 or linux/arm64")
    offline = body.get("offline", False)
    if type(offline) is not bool:
        raise StateError("offline must be a boolean")
    recipe = _web_string(body, "dockerfile")
    inline = body.get("dockerfile_text")
    if inline is not None and (not isinstance(inline, str) or not inline.strip() or "\0" in inline):
        raise StateError("dockerfile_text must be nonempty Dockerfile text")
    if recipe is not None and inline is not None:
        raise StateError("choose dockerfile or dockerfile_text, not both")
    args = [
        "build",
        str(context.resolve()),
        "--frontend=dockerfile",
        "--cache-mode=local",
        f"--tag={tag}",
        f"--platform={platform}",
        f"--network={'none' if offline else 'default'}",
    ]
    if offline:
        args.append("--offline")
    else:
        args.append("--registry=docker")
    if recipe is not None:
        recipe_path = Path(recipe).expanduser()
        if not recipe_path.is_absolute() or not recipe_path.is_file():
            raise StateError("dockerfile must be an existing absolute host file")
        args.append(f"--file={recipe_path.resolve()}")
    elif inline is None and not (context / "Dockerfile").is_file():
        raise StateError("context has no Dockerfile; provide a host file or inline recipe")
    target = _web_string(body, "target")
    if target is not None:
        args.append(f"--target={target}")
    args.extend(f"--build-arg={value}" for value in _web_strings(body, "build_args"))
    return operations.start(roots, "build", args, dockerfile_text=inline)


def run_image(roots: StatePaths, operations: OperationManager, body: dict[str, Any]) -> dict[str, Any]:
    """Start image resolution/runtime, detached except for foreground-only force mode."""
    if set(body) - {
        "image",
        "name",
        "backend",
        "runtime_kind",
        "manifest",
        "memory",
        "vcpus",
        "network",
        "publish",
        "command",
        "registry",
        "layers",
        "volumes",
        "platform",
        "force_architecture",
        "vm_image",
        "vm_image_digest",
        "root_retention",
        "root_volume",
    }:
        raise StateError("unsupported run field")
    image = _web_string(body, "image", required=True)
    if image.startswith("-"):
        raise StateError("image cannot begin with an option prefix")
    name = _web_string(body, "name") or f"web-{uuid.uuid4().hex[:12]}"
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) is None:
        raise StateError("invalid run name")
    force_architecture = body.get("force_architecture") is True
    args = ["run", f"--name={name}"]
    if not force_architecture:
        args.append("--detach")
    backend_val = _web_string(body, "backend")
    if backend_val is not None:
        args.append(f"--backend={backend_val}")
    root_retention_val = _web_string(body, "root_retention")
    if root_retention_val is not None:
        if root_retention_val not in {"delete", "retain"}:
            raise StateError("root_retention must be delete or retain")
        if force_architecture and root_retention_val == "retain":
            raise StateError("force_architecture requires disposable root_retention delete, not retain")
        args.append(f"--root-retention={root_retention_val}")
    root_volume_val = _web_string(body, "root_volume")
    if root_volume_val is not None:
        args.append(f"--root-volume={root_volume_val}")
    for key, option in (
        ("runtime_kind", "runtime-kind"),
        ("manifest", "manifest"),
        ("network", "network"),
        ("registry", "registry"),
    ):
        value = _web_string(body, key)
        if key == "runtime_kind" and value is not None and value not in {"cloud-image", "oci-root"}:
            raise StateError("runtime_kind must be cloud-image or oci-root")
        if value is not None:
            args.append(f"--{option}={value}")
    platform_val = _web_string(body, "platform")
    if platform_val is not None:
        if platform_val not in {"linux/amd64", "linux/arm64"}:
            raise StateError("platform must be linux/amd64 or linux/arm64")
        args.append(f"--platform={platform_val}")
    if force_architecture:
        args.append("--force-architecture")
    vm_image = _web_string(body, "vm_image")
    if vm_image is not None:
        args.append(f"--vm-image={vm_image}")
    vm_image_digest = _web_string(body, "vm_image_digest")
    if vm_image_digest is not None:
        args.append(f"--vm-image-digest={vm_image_digest}")
    for key, low, high in (("memory", 256, 1048576), ("vcpus", 1, 256)):
        value = body.get(key)
        if value is not None:
            if type(value) is not int or not low <= value <= high:
                raise StateError(f"{key} must be an integer between {low} and {high}")
            args.append(f"--{key}={value}")
    args.extend(f"--publish={value}" for value in _web_strings(body, "publish"))
    raw_volumes = body.get("volumes", [])
    if not isinstance(raw_volumes, list) or len(raw_volumes) > 16:
        raise StateError("volumes must be a list of at most 16 attachments")
    for vol in raw_volumes:
        if not isinstance(vol, dict) or set(vol) - {"name", "mount_path", "read_only"}:
            raise StateError("volume attachment entry must contain name, mount_path and optional read_only")
        v_name = _web_string(vol, "name", required=True)
        v_mount = _web_string(vol, "mount_path", required=True)
        v_ro = vol.get("read_only", False)
        if type(v_ro) is not bool:
            raise StateError("volume read_only must be a boolean")
        if ":" in v_name or ":" in v_mount:
            raise StateError("volume name and mount_path cannot contain ':'")
        entry_str = f"{v_name}:{v_mount}:ro" if v_ro else f"{v_name}:{v_mount}"
        args.append(f"--volume={entry_str}")
    for layer in _web_strings(body, "layers"):
        args.append(f"--layer={require_digest(layer)}")
    args.append(image)
    command = _web_strings(body, "command")
    if command:
        args.extend(["--", *command])
    return operations.start(roots, "run", args, resource_name=name)


def remote_connections(roots: StatePaths, *, hub_connection: HubConnection | None = None) -> dict[str, Any]:
    """Describe protocols without resolving remote credentials or contacting a Hub."""
    return {"connections": RemoteArtifacts(roots, hub_connection=hub_connection).connections()}


def remote_hub_identity(roots: StatePaths, *, hub_connection: HubConnection | None = None) -> dict[str, Any]:
    return RemoteArtifacts(roots, hub_connection=hub_connection).hub_identity()


def inspect_remote_artifact(
    roots: StatePaths,
    *,
    connection_id: str,
    reference: str,
    manifest: str | None = None,
    hub_connection: HubConnection | None = None,
) -> dict[str, Any]:
    return RemoteArtifacts(roots, hub_connection=hub_connection).inspect(connection_id, reference, manifest=manifest)


def browse_remote_artifacts(
    roots: StatePaths,
    *,
    connection_id: str,
    kind: str = "all",
    query: str = "",
    repository: str = "",
    limit: int = 30,
    cursor: str | None = None,
    arch: str = "",
    disk_format: str = "",
    parent: str = "",
    hub_connection: HubConnection | None = None,
) -> dict[str, Any]:
    return RemoteArtifacts(roots, hub_connection=hub_connection).browse(
        connection_id,
        kind=kind,
        query=query,
        repository=repository,
        limit=limit,
        cursor=cursor,
        arch=arch,
        disk_format=disk_format,
        parent=parent,
    )


def transfer_artifact(
    roots: StatePaths,
    operations: OperationManager,
    body: dict[str, Any],
    *,
    hub_connection: HubConnection | None = None,
) -> dict[str, Any]:
    action = _web_string(body, "action", required=True)
    connection = _web_string(body, "connection_id", required=True)
    backend = RemoteArtifacts(roots, hub_connection=hub_connection)
    if action == "download":
        if set(body) - {"action", "connection_id", "reference", "platform"}:
            raise StateError("unsupported download field")
        platform = _web_string(body, "platform") or "linux/amd64"
        return backend.start_download(
            operations, connection, _web_string(body, "reference", required=True), platform=platform
        )
    if action == "upload":
        if set(body) - {"action", "connection_id", "source", "target"}:
            raise StateError("unsupported upload field")
        return backend.start_upload(
            operations, connection, body.get("source"), _web_string(body, "target", required=True)
        )
    raise StateError("transfer action must be download or upload")


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
    """Project selected runs and live host resources; ``live=False`` is ledger-only."""
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
                "id": name,
                "managed": not live or not summary.stale,
                "source": "run-ledger",
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

    if live:
        observed = host_inventory.observe()
        warnings.extend(observed["warnings"])
        selected = {(vm["backend"], vm["name"], vm["run_id"]): vm for vm in vms}
        for host_vm in observed["vms"]:
            identity = (host_vm["backend"], host_vm["name"], host_vm.get("run_id"))
            owned = selected.get(identity) if identity[2] else None
            if owned is not None and owned["status"] != "removed":
                # Libvirt domain enumeration cannot recover Palimpsest's published endpoints.
                fields = ("status", "memory_mib", "vcpus")
                if host_vm["backend"] == "lima-vz":
                    fields += ("ssh", "guest_ip", "ports")
                for field in fields:
                    if host_vm.get(field) is not None:
                        owned[field] = host_vm[field]
                owned["source"] = "run-ledger+host-backend"
            else:
                for vm in vms:
                    if vm["backend"] == host_vm["backend"] and vm["name"] == host_vm["name"]:
                        vm["managed"] = False
                        vm["stale"] = True
                vms.append(host_vm)

    vms.sort(key=lambda x: (x["name"], x["backend"], x["id"]))
    for vm in vms:
        collision = any(
            other is not vm and other["backend"] == vm["backend"] and other["name"] == vm["name"] for other in vms
        )
        vm["access"] = vm_access.capabilities(vm, live=live, collision=collision)
        if vm.get("managed") is False and vm.get("source") == "host-backend":
            # For host VMs, do not let stale managed run ledgers with the same name disable native removal.
            # Only collide with distinct host-backend records of the same ID or conflicting active host names.
            host_collision = any(
                other is not vm
                and other.get("source") == "host-backend"
                and (other["id"] == vm["id"] or (other["backend"] == vm["backend"] and other["name"] == vm["name"]))
                for other in vms
            )
            vm["lifecycle"] = _vm_lifecycle_capabilities(vm, live=live, collision=host_collision)
        else:
            identity_collision = collision or any(
                other is not vm and (other["id"] == vm["id"] or other["name"] == vm["id"]) for other in vms
            )
            vm["lifecycle"] = _vm_lifecycle_capabilities(vm, live=live, collision=identity_collision)
        vm["remove_reason"] = vm["lifecycle"].pop("remove_reason")
    unique_warnings: list[str] = []
    seen = set()
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    return {"vms": vms, "warnings": unique_warnings}


def get_vm(roots: StatePaths, name: str) -> dict[str, Any]:
    res = list_vms(roots)
    matches = [vm for vm in res["vms"] if vm["id"] == name or vm["name"] == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise StateError(f"VM '{name}' is ambiguous across backend ownership identities")
    raise StateError(f"VM '{name}' not found")


def _vm_lifecycle_capabilities(row: dict[str, Any], *, live: bool = True, collision: bool = False) -> dict[str, Any]:
    """Project managed lifecycle and explicit native-identity host removal.

    External removal never adopts a run or permits another state's file cleanup.
    OCI-root retains its separate runtime removal/retention policy.
    """
    eligible = (
        live
        and row.get("managed") is True
        and not row.get("stale")
        and not collision
        and row.get("id") == row.get("name")
        and bool(row.get("run_id"))
        and row.get("runtime_kind") == "cloud-image"
        and row.get("status") != "removed"
    )
    host_removal = (
        live
        and row.get("managed") is False
        and row.get("source") == "host-backend"
        and not row.get("stale")
        and not collision
        and row.get("id") == f"host:{row.get('backend')}:{row.get('name')}"
        and isinstance(row.get("removal_identity"), str)
        and bool(row["removal_identity"])
        and row.get("runtime_kind") != "oci-root"
        and row.get("status") in {"running", "stopped", "paused", "failed"}
    )
    can_remove = eligible or host_removal
    remove_reason = None
    if not can_remove:
        if not live:
            remove_reason = "오프라인/기록 전용 조회에서는 제거할 수 없습니다."
        elif row.get("runtime_kind") == "oci-root":
            remove_reason = "OCI-root 제거는 원래 상태의 CLI rm을 사용하세요."
        elif collision:
            remove_reason = "동일 백엔드 내 VM 이름 또는 식별자가 충돌합니다."
        elif row.get("managed") is False and not row.get("removal_identity"):
            remove_reason = "호스트 백엔드의 고유 인스턴스 식별자를 확인할 수 없습니다."
        elif row.get("stale"):
            remove_reason = "상태가 변경되었거나 오래된 관찰입니다. 지금 갱신하세요."
        else:
            remove_reason = "이 VM 상태에서는 제거할 수 없습니다."

    return {
        "start": eligible and row.get("status") == "stopped",
        "stop": eligible and row.get("status") == "running",
        "remove": can_remove,
        "remove_reason": remove_reason,
        "remove_volumes": eligible or host_removal and row.get("backend") in {"lima-vz", "lima-qemu", "lima-krunkit"},
    }


def _selected_vm_identity(
    roots: StatePaths, identifier: str, *, expected_run_id: str, action: str
) -> ExpectedRunIdentity:
    if not isinstance(identifier, str) or not identifier:
        raise StateError("invalid VM inventory identifier")
    if not isinstance(expected_run_id, str) or not expected_run_id:
        raise StateError("expected_run_id must be the selected VM UUID")
    row = get_vm(roots, identifier)
    if identifier != row["id"] or row.get("managed") is not True:
        raise StateError("VM lifecycle is forbidden for unmanaged inventory identities")
    if row.get("run_id") != expected_run_id:
        raise StateError("selected VM identity changed")
    if not row["lifecycle"][action]:
        raise StateError("VM lifecycle action is unavailable for this selected identity")
    snapshot = state.read_run_ledger_snapshot(roots, row["name"])
    record = snapshot.record
    if (
        record.name != identifier
        or record.run_id != expected_run_id
        or record.dispatch_key.backend.value != row["backend"]
        or record.dispatch_key.runtime_kind.value != row["runtime_kind"]
        or snapshot.state.get("status") == "removed"
    ):
        raise StateError("selected VM identity changed")
    # Carry the original selection into dispatch; never resolve a fresh identity
    # at mutation time or wrap the adapter's own lock in an inventory lock.
    return ExpectedRunIdentity(record.name, expected_run_id, record.dispatch_key)


def _vm_lifecycle_projection(result: LifecycleResult) -> dict[str, Any]:
    """Return only public receipt fields, never a fresh same-name VM lookup."""
    if not isinstance(result, LifecycleResult):
        raise StateError("runtime returned an invalid VM lifecycle receipt")
    return {
        "name": result.record.name,
        "run_id": result.record.run_id,
        "runtime_kind": result.record.dispatch_key.runtime_kind.value,
        "backend": result.record.dispatch_key.backend.value,
        "operation": result.operation.value,
        "previous_status": result.previous_status,
        "status": result.current_status,
        "lifecycle_revision": result.cursor.revision,
        "warning_category": None if result.warning_category is None else result.warning_category.value,
        "fallback_used": result.fallback_used,
        **(
            {"storage_policy": result.storage_policy, "retained_instance": result.retained_instance}
            if result.storage_policy is not None
            else {}
        ),
    }


def vm_lifecycle(roots: StatePaths, identifier: str, action: str, *, expected_run_id: str) -> dict[str, Any]:
    """Start/stop an exact managed selection with backend-lock UUID fencing."""
    if not isinstance(action, str) or action not in {"start", "stop"}:
        raise StateError("VM lifecycle action must be start or stop")
    identity = _selected_vm_identity(roots, identifier, expected_run_id=expected_run_id, action=action)
    operation = runtime_dispatch.start if action == "start" else runtime_dispatch.stop
    return _vm_lifecycle_projection(operation(identity.name, roots=roots, expected_identity=identity))


def remove_vm(
    roots: StatePaths,
    identifier: str,
    *,
    expected_run_id: str | None = None,
    expected_removal_identity: str | None = None,
    confirmation: str,
    volumes: bool = False,
) -> dict[str, Any]:
    """Remove an exact selection after typed-name confirmation.

    Managed instance roots and ordered topology are retained by default.
    ``volumes=True`` purges only safely owned instance state, never independent
    named data volumes. External libvirt disks are reference-only, not adopted.
    """
    if type(volumes) is not bool:
        raise StateError("volumes must be a boolean")
    if isinstance(identifier, str) and identifier.startswith("host:"):
        row = get_vm(roots, identifier)
        if (
            row["id"] != identifier
            or row.get("managed") is not False
            or not row["lifecycle"]["remove"]
            or not isinstance(expected_removal_identity, str)
            or not expected_removal_identity
        ):
            raise StateError("host VM removal is forbidden without a current native identity")
        if row.get("run_id") != expected_run_id or row.get("removal_identity") != expected_removal_identity:
            raise StateError("selected host VM identity changed; refresh before removing")
        if not isinstance(confirmation, str) or confirmation != row["name"]:
            raise StateError("confirmation must exactly match the selected VM name")
        return host_vm_control.remove_vm(
            row,
            expected_identity=expected_removal_identity,
            roots=roots,
            volumes=volumes,
        )
    if expected_removal_identity is not None:
        raise StateError("native removal identity requires a stable host VM identifier")
    identity = _selected_vm_identity(
        roots, identifier, expected_run_id=expected_run_id, action="remove_volumes" if volumes else "remove"
    )
    if not isinstance(confirmation, str) or confirmation != identity.name:
        raise StateError("confirmation must exactly match the selected VM name")
    return _vm_lifecycle_projection(
        runtime_dispatch.rm(
            identity.name,
            roots=roots,
            expected_identity=identity,
            volumes=volumes,
        )
    )


def vm_metrics(
    roots: StatePaths, identifier: str, *, expected_run_id: str | None = None, exec_owners=None
) -> dict[str, Any]:
    """Collect one bounded Linux sample, only for this detail request."""
    try:
        access = vm_access.Access.resolve(roots, identifier, expected_run_id=expected_run_id)
        access.exec_owners = exec_owners
        return vm_access.metrics(access)
    except vm_access.VMAccessError as exc:
        reason, message = exc.category, str(exc)
    except (RuntimeCapabilityError, ProcessCapabilityError):
        reason, message = "capability", "Guest measurement transport is unsupported by this runtime"
    except Exception:
        reason, message = "transport", "Guest measurements are unavailable"
    return {
        "id": identifier,
        "status": "unavailable",
        "source": "linux-guest",
        "reason": reason,
        "message": message,
        "measured_at": None,
        "run_id": expected_run_id,
        "cpu": None,
        "load": None,
        "memory": None,
        "uptime_seconds": None,
        "boot_id": None,
        "filesystems": None,
        "interfaces": None,
        "block_devices": None,
        "unavailable": [
            "stat",
            "loadavg",
            "meminfo",
            "uptime",
            "df",
            "net/dev",
            "diskstats",
            "sys/kernel/random/boot_id",
        ],
    }


def vm_logs(
    roots: StatePaths, identifier: str, *, tail: int = 200, expected_run_id: str | None = None, exec_owners=None
) -> dict[str, Any]:
    """Read bounded guest journal or identity-bound native Lima serial console."""
    if type(tail) is not int or not 1 <= tail <= 5000:
        raise StateError("VM log tail must be between 1 and 5000 lines")
    try:
        access = vm_access.Access.resolve(roots, identifier, expected_run_id=expected_run_id)
        access.exec_owners = exec_owners
        return {"status": "available", **access.logs(tail)}
    except vm_access.VMAccessError as exc:
        reason, message = exc.category, str(exc)
    except (RuntimeCapabilityError, ProcessCapabilityError):
        reason, message = "capability", "Guest log transport is unsupported by this runtime"
    except Exception:
        reason, message = "transport", "Guest journal or native console is unavailable"
    return {
        "status": "unavailable",
        "log": "",
        "source": None,
        "truncated": False,
        "reason": reason,
        "message": message,
    }


def list_artifacts(roots: StatePaths) -> dict[str, Any]:
    proj_index = _build_project_index(roots)
    referenced_by: dict[str, dict[str, set[str]]] = {}
    retained_metadata_valid = True
    try:
        retained_refs = retained_instances.artifact_references(roots)
    except _METADATA_ERRORS:
        retained_metadata_valid = False
        retained_refs = {}

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
            runs_refs = sorted(referenced_by.get(digest, {}).get("runs", set()))
            projs_refs = sorted(referenced_by.get(digest, {}).get("projects", set()))
            vols_refs = sorted(retained_refs.get(digest, []))
            ref_dict: dict[str, list[str]] = {
                "runs": runs_refs,
                "projects": projs_refs,
            }
            if vols_refs:
                ref_dict["volumes"] = vols_refs
            is_referenced = bool(runs_refs or projs_refs or vols_refs)
            blockers = {
                "runs": runs_refs,
                "projects": projs_refs,
                "volumes": vols_refs,
            }

            art_record = {
                **meta,
                "digest": digest,
                "kind": kind or "unknown",
                "size_bytes": size_bytes,
                "tags": tags,
                "source": "content-store",
                "availability": "available" if store.exists(digest) else "missing",
                "editable": store.exists(digest)
                and (
                    kind == KIND_CLOUD_IMAGE
                    or kind == "squashfs"
                    or meta.get("media_type") == MEDIA_TYPE_LAYER_SQUASHFS
                ),
                "referenced_by": ref_dict,
                "delete_supported": retained_metadata_valid and not is_referenced,
                "delete_reason": "보존된 인스턴스 메타데이터를 확인할 수 없어 삭제가 차단되었습니다."
                if not retained_metadata_valid
                else "실행 중이거나 보존된 볼륨/인스턴스가 참조 중인 레이어/이미지는 삭제할 수 없습니다."
                if is_referenced
                else None,
                "deletion_blockers": blockers,
            }

            artifacts.append(art_record)

            if kind == KIND_CLOUD_IMAGE:
                images.append(art_record)
            elif kind == "squashfs" or meta.get("media_type") == MEDIA_TYPE_LAYER_SQUASHFS:
                layers.append(art_record)
            else:
                unknown.append(art_record)

    # BuildKit's OCI export is not a runtime block and need not be in the CAS.
    # Output tags are build provenance, not proof of a durable local CAS binding.
    artifacts_by_digest = {artifact["digest"]: artifact for artifact in artifacts}
    oci_images: dict[tuple[str, str], dict[str, Any]] = {}
    export_observations: dict[tuple[str | None, int | None], tuple[str, int | None]] = {}
    for build_dir, rec in _build_records(roots):
        build = _normalize_build_record(build_dir, rec)
        if build["engine"] != "buildkit" or build["status"] != "success":
            continue
        runtime_digest = build["runtime_block_digest"]
        runtime_artifact = artifacts_by_digest.get(runtime_digest)
        if runtime_artifact is not None and (
            runtime_artifact.get("kind") == "squashfs"
            or runtime_artifact.get("media_type") == MEDIA_TYPE_LAYER_SQUASHFS
        ):
            runtime_artifact.setdefault("builds", []).append(build)
            runtime_artifact["runtime_block_digest"] = runtime_digest
        archive_digest = build["output_oci_archive_digest"]
        manifest_digest = build["output_oci_manifest_digest"]
        if archive_digest is None and manifest_digest is None:
            continue
        output_path = rec.get("output_path")
        output_path = output_path if isinstance(output_path, str) else None
        recorded_bytes = rec.get("bytes")
        recorded_size = recorded_bytes.get("oci_archive") if isinstance(recorded_bytes, dict) else None
        recorded_size = recorded_size if type(recorded_size) is int and recorded_size >= 0 else None
        export_key = (output_path, recorded_size)
        if export_key not in export_observations:
            export_observations[export_key] = _local_export_observation(output_path, recorded_size)
        availability, observed_size = export_observations[export_key]
        size = recorded_size if recorded_size is not None else observed_size
        digest_type = "oci-manifest" if manifest_digest else "oci-archive"
        digest = manifest_digest or archive_digest
        key = (digest_type, digest)
        if key not in oci_images:
            art_record = {
                "digest": digest,
                "digest_type": digest_type,
                "kind": "oci-image",
                "source": "build-record",
                "editable": False,
                "availability": availability,
                "name": ", ".join(build["output_tags"]) or build["build_id"],
                "output_tags": list(build["output_tags"]),
                "build_ids": [],
                "arch": build["platform"].split("/")[1] if build["platform"] and "/" in build["platform"] else None,
                "created_at": build["finished_at"] or build["started_at"],
                "size_bytes": size,
                "output_oci_archive_size_bytes": size,
                "platform": build["platform"],
                "output_oci_manifest_digest": manifest_digest,
                "output_oci_archive_digest": archive_digest,
                "tags": [],
                "referenced_by": {"runs": [], "projects": []},
                "builds": [],
            }
            oci_images[key] = art_record
            artifacts.append(art_record)
            images.append(art_record)
        oci_images[key]["builds"].append(build)
        image = oci_images[key]
        image["build_ids"].append(build["build_id"])
        image["output_tags"] = sorted(set(image["output_tags"] + build["output_tags"]))
        image["name"] = ", ".join(image["output_tags"]) or image["build_ids"][0]
        if availability == "available" and image["availability"] != "available":
            image["availability"] = availability
            image["size_bytes"] = image["output_oci_archive_size_bytes"] = size
            image["output_oci_archive_digest"] = archive_digest
        elif availability == "unavailable" and image["availability"] == "missing":
            image["availability"] = availability
    for retained in list_retained_oci_sources(roots):
        key = (retained["digest_type"], retained["digest"])
        if key in oci_images:
            continue
        oci_images[key] = retained
        artifacts.append(retained)
        images.append(retained)
    for retained in _retained_remote_artifacts(roots):
        key = (retained["digest_type"], retained["digest"])
        if key in oci_images:
            oci_images[key].setdefault("upload_source", retained["upload_source"])
            continue
        oci_images[key] = retained
        artifacts.append(retained)
        images.append(retained)
    return {
        "artifacts": artifacts,
        "images": images,
        "layers": layers,
        "unknown": unknown,
    }


def _retained_remote_artifacts(roots: StatePaths) -> Iterator[dict[str, Any]]:
    """Project owner-bound receipts; observe files without rehashing archives."""
    try:
        with pinned_owner_directory(roots.state.resolve() / "remote-references", missing_ok=True) as directory_fd:
            if directory_fd is None:
                return
            for filename in sorted(os.listdir(directory_fd)):
                if re.fullmatch(r"[0-9a-f]{64}\.json", filename) is None:
                    continue
                try:
                    info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                        continue
                    record = state._read_pinned_json_object(directory_fd, filename)
                    if record.get("schema") != "palimpsest-remote-reference-v1" or record.get("kind") not in {
                        "oci-image",
                        "runtime-bundle",
                    }:
                        continue
                    connection, reference = record.get("connection_id"), record.get("requested_reference")
                    if not isinstance(connection, str) or not isinstance(reference, str):
                        continue
                    if hashlib.sha256((connection + "\0" + reference).encode()).hexdigest() + ".json" != filename:
                        continue
                    archive_digest = require_digest(record.get("archive_digest"))
                    root_digest = require_digest(record.get("package_manifest"))
                    archive = record.get("archive")
                    if not isinstance(archive, str) or not Path(archive).is_absolute():
                        continue
                    archive_path = Path(archive).parent.resolve() / Path(archive).name
                    if archive_path.parent not in {
                        roots.state.resolve() / "registry-artifacts",
                        roots.state.resolve() / "package-artifacts",
                    }:
                        continue
                    availability, size = _local_export_observation(archive, None)
                    yield {
                        "digest": root_digest,
                        "digest_type": "oci-manifest",
                        "kind": record["kind"],
                        "source": "remote-transfer",
                        "name": reference,
                        "editable": False,
                        "availability": availability,
                        "size_bytes": size,
                        "output_oci_archive_digest": archive_digest,
                        "output_oci_manifest_digest": record.get("manifest"),
                        "image": record.get("image"),
                        "manifest": record.get("manifest"),
                        "runtime_kind": record.get("runtime_kind"),
                        "layers": record.get("layers", []),
                        "run_supported": record.get("run_supported") is True,
                        "run_limitation": record.get("run_limitation"),
                        "architectures": record.get("architectures", []),
                        "tags": [],
                        "referenced_by": {"runs": [], "projects": []},
                        "upload_source": {"kind": "remote", "connection_id": connection, "reference": reference},
                    }
                except (OSError, ValueError, TypeError, StateError, ArtifactValidationError):
                    continue
    except (OSError, StateError):
        return


def _optional_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return require_digest(value)
    except InvalidDigestError:
        return None


def _local_export_observation(value: str | None, recorded_size: int | None) -> tuple[str, int | None]:
    """Observe a regular archive's presence/size, not its digest or runnability."""
    if not value or not Path(value).is_absolute():
        return "unavailable", None
    path = Path(value)
    try:
        # Allow parent aliases such as macOS /tmp, but never follow the leaf.
        path = path.parent.resolve(strict=True) / path.name
        entry = path.stat(follow_symlinks=False)
        if not stat_module.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            return "unavailable", None
        size = entry.st_size
        if size <= 0 or recorded_size is not None and size != recorded_size:
            return "unavailable", size
        return "available", size
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, ArtifactValidationError):
        return "unavailable", None


def _read_build_record(builds_fd: int, name: str) -> dict[str, Any]:
    before = os.stat(name, dir_fd=builds_fd, follow_symlinks=False)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    build_fd = os.open(name, flags, dir_fd=builds_fd)
    try:
        opened = os.fstat(build_fd)
        if (
            not stat_module.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat_module.S_IMODE(opened.st_mode) & 0o022
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise StateError("invalid build directory")
        rec = state._read_pinned_json_object(build_fd, "record.json")
        current = os.stat(name, dir_fd=builds_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise StateError("build directory changed during read")
        if rec.get("build_id", name) != name:
            raise StateError("build record does not match its directory")
        return rec
    finally:
        os.close(build_fd)


def _build_records(roots: StatePaths) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Read editable ledgers only through owner-bound directories and files."""
    try:
        with pinned_owner_directory(roots.state.resolve() / "builds", missing_ok=True) as builds_fd:
            if builds_fd is None:
                return
            for name in sorted(os.listdir(builds_fd)):
                if BUILD_ID_RE.fullmatch(name) is None:
                    continue
                build_dir = roots.state.resolve() / "builds" / name
                try:
                    yield build_dir, _read_build_record(builds_fd, name)
                except (OSError, StateError, ArtifactValidationError):
                    continue
    except (OSError, StateError):
        return


def _build_engine(rec: dict[str, Any]) -> str:
    engine = rec.get("engine") or ("buildkit" if rec.get("schema_version", 1) == 2 else "palimpsestfile")
    return "buildkit" if engine == "buildkit" else "palimpsestfile"


def _normalize_build_record(build_dir: Path, rec: dict[str, Any]) -> dict[str, Any]:
    schema_version = rec.get("schema_version", 1)
    engine = _build_engine(rec)

    build_id = rec.get("build_id") or build_dir.name
    status = rec.get("status") if isinstance(rec.get("status"), str) else "unknown"
    finished_at = rec.get("finished_at") if isinstance(rec.get("finished_at"), str) else None
    created_at = rec.get("created_at") if isinstance(rec.get("created_at"), str) else None
    started_at = rec.get("started_at") if isinstance(rec.get("started_at"), str) else None

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
        total = timings_ms.get("total")
        if type(total) in (int, float) and math.isfinite(total):
            duration_ms = int(total)
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
        output_tags = [out_tag] if isinstance(out_tag, str) and out_tag else []
    if not isinstance(output_tags, list):
        output_tags = []
    output_tags = [tag for tag in output_tags if isinstance(tag, str) and tag]

    manifest_digest = _optional_digest(rec.get("output_oci_manifest_digest"))
    archive_digest = _optional_digest(rec.get("output_oci_archive_digest"))
    runtime_digest = _optional_digest(rec.get("runtime_block_digest"))
    output_digest = _optional_digest(rec.get("output_digest")) or runtime_digest or manifest_digest or archive_digest
    base_digest = _optional_digest(rec.get("base_digest")) or _optional_digest(rec.get("runtime_base_digest"))
    parent_digests = rec.get("parent_digests", [])
    parent_digests = (
        [digest for value in parent_digests if (digest := _optional_digest(value)) is not None]
        if isinstance(parent_digests, list)
        else []
    )
    platform = rec.get("platform") if isinstance(rec.get("platform"), str) else None
    cache_source = rec.get("cache_source") if isinstance(rec.get("cache_source"), str) else None
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
        "output_oci_manifest_digest": manifest_digest,
        "output_oci_archive_digest": archive_digest,
        "runtime_block_digest": runtime_digest,
        "base_digest": base_digest,
        "parent_digests": parent_digests,
        "platform": platform,
        "cache_source": cache_source,
        "timings_ms": timings_ms,
        "log_available": log_available,
    }


def list_builds(roots: StatePaths, *, limit: int = 100) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for build_dir, rec in _build_records(roots):
        items.append(_normalize_build_record(build_dir, rec))

    items.sort(key=lambda b: b.get("started_at") or b.get("finished_at") or "", reverse=True)
    return items[:limit]


def get_build(roots: StatePaths, build_id: str) -> dict[str, Any]:
    if not isinstance(build_id, str) or BUILD_ID_RE.fullmatch(build_id) is None:
        raise StateError(f"invalid build id: {build_id!r}")
    build_dir = roots.state.resolve() / "builds" / build_id
    try:
        with pinned_owner_directory(build_dir.parent) as builds_fd:
            assert builds_fd is not None
            rec = _read_build_record(builds_fd, build_id)
    except OSError:
        raise StateError(f"build '{build_id}' not found or unsafe") from None
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


def tag_artifact(roots: StatePaths, digest: str, tag: str) -> dict[str, Any]:
    """Verify and bind one supported CAS artifact in a single guarded transaction."""
    roots = StatePaths(config=roots.config.resolve(), state=roots.state.resolve())
    digest = require_digest(digest)
    tag = state.validate_tag(tag)
    with artifact_reference_guard(roots):
        physical = ArtifactStore(roots.store)
        with (
            physical.digest_guard(digest),
            pinned_owner_directory(roots.store / "metadata") as metadata_fd,
            pinned_owner_directory(roots.tags) as tags_fd,
        ):
            assert metadata_fd is not None and tags_fd is not None
            blob = roots.store / "blobs" / "sha256" / digest.split(":", 1)[1]
            try:
                blob_snapshot = blob.stat(follow_symlinks=False)
                verified = physical.verify_blob(digest)
            except (OSError, ArtifactStoreError):
                raise StateError("tag references a missing or unsafe CAS artifact") from None
            meta, metadata_snapshot = state._read_pinned_json_object_snapshot(
                metadata_fd, f"{digest.split(':', 1)[1]}.json"
            )
            if meta.get("digest") != digest:
                raise StateError("artifact metadata digest mismatch")
            if meta.get("kind") == KIND_CLOUD_IMAGE:
                disk_format = meta.get("disk_format")
                media_type = DISK_FORMAT_MEDIA_TYPES.get(disk_format) if isinstance(disk_format, str) else None
                if media_type is None:
                    raise StateError("unsupported cloud image disk format")
            elif meta.get("kind") == "squashfs" or meta.get("media_type") == MEDIA_TYPE_LAYER_SQUASHFS:
                media_type = MEDIA_TYPE_LAYER_SQUASHFS
            else:
                raise StateError("unsupported artifact type for local tagging")
            for field in ("parent_digest", "base_image_digest"):
                value = meta.get(field)
                if value is not None and _optional_digest(value) is None:
                    raise StateError("artifact metadata has an invalid digest reference")
            record = state.TagRecord(
                schema_version=1,
                tag=tag,
                digest=digest,
                media_type=media_type,
                size_bytes=verified.size,
                parent_digest=meta.get("parent_digest"),
                base_image_digest=meta.get("base_image_digest"),
                source="inventory",
                created_at=state.utc_now_iso(),
            )
            committed = state._write_tag_record_locked(
                roots,
                record,
                tags_fd=tags_fd,
                blob_snapshot=blob_snapshot,
                metadata_snapshot=metadata_snapshot,
            )
            return dataclasses.asdict(committed)


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

    try:
        retained_refs = retained_instances.artifact_references(roots)
    except Exception:
        raise StateError(
            "cannot prove artifact is unreferenced because retained instance metadata is invalid"
        ) from None
    retained_blockers = retained_refs.get(norm_digest, [])

    names = sorted(referencing_runs | referencing_projects | set(retained_blockers))
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


def create_volume(roots: StatePaths, body: dict[str, Any]) -> dict[str, Any]:
    if set(body) != {"name", "backend", "size_bytes"}:
        raise StateError("volume creation requires name, backend and size_bytes")
    name = _web_string(body, "name", required=True)
    backend = _web_string(body, "backend", required=True)
    size = body["size_bytes"]
    if type(size) is not int or size <= 0:
        raise StateError("size_bytes must be a positive integer")
    return named_volumes.create_named_volume(roots, name=name, backend=backend, size_bytes=size)


def delete_volume(roots: StatePaths, identifier: str, *, confirmation: str) -> dict[str, Any]:
    if not isinstance(confirmation, str) or not confirmation:
        raise StateError("confirmation must exactly match the selected volume name")
    if identifier.startswith("named:"):
        parts = identifier.split(":", 2)
        if len(parts) != 3:
            raise StateError("invalid named volume identifier")
        _, backend, name = parts
        if confirmation != name:
            raise StateError("confirmation must exactly match the selected volume name")
        return named_volumes.delete_named_volume(roots, name=name, backend=backend)
    if identifier.startswith("retained-instance:"):
        return retained_instances.delete_retained_instance(roots, identifier, confirmation=confirmation)
    raise StateError("only named volumes or retained instances can be deleted")


def list_volumes(roots: StatePaths, *, vms_result: dict | None = None) -> dict[str, Any]:
    """Enumerate retained ledger volumes and read-only host disks, including unattached disks."""
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
    try:
        for named in named_volumes.list_named_volumes(roots):
            volumes[named["id"]] = named
    except _METADATA_ERRORS:
        warnings.add("Named volume metadata is unavailable or inconsistent")
    try:
        for retained in retained_instances.list_retained_instances(roots):
            volumes[retained["id"]] = retained
    except _METADATA_ERRORS:
        warnings.add("Retained instance metadata is unavailable or inconsistent")
    observed = host_inventory.observe()
    warnings.update(observed["warnings"])
    for host_volume in observed["volumes"]:
        target = None
        if host_volume["kind"] == "vm-disk":
            identities = {
                (item["name"], item.get("run_id")) for item in host_volume["attachments"] if item.get("run_id")
            }
            target = next(
                (
                    row
                    for row in volumes.values()
                    if row["kind"] == "vm-disk"
                    and row["backend"] == host_volume["backend"]
                    and any((item["name"], item.get("run_id")) in identities for item in row["attachments"])
                ),
                None,
            )
        if target is None:
            volumes[host_volume["id"]] = host_volume
        else:
            for field in ("size_bytes", "file_size_bytes", "allocated_bytes", "status", "source"):
                if field in host_volume:
                    target[field] = host_volume[field]
    host_info = platforms.detect_host()
    create_backends = []
    for backend in ("lima-vz", "kvm", "libvirt-hvf"):
        try:
            if platforms.backend_capability_report(backend, host=host_info).successful:
                create_backends.append(backend)
        except (ArtifactValidationError, RuntimeCapabilityError):
            continue
    for row in volumes.values():
        row["attachments"].sort(
            key=lambda item: (
                item["name"],
                item.get("run_id") or "",
                item.get("mount_path") or "",
                item.get("read_only", False),
            )
        )
        kind = row.get("kind")
        if kind == "named":
            has_att = bool(row.get("attachments"))
            row["delete_supported"] = not has_att
            row["delete_reason"] = (
                "VM에 연결된 볼륨은 제거할 수 없습니다. VM을 먼저 종료·제거하세요." if has_att else None
            )
        elif kind == "retained-instance":
            is_ref = row.get("reference_only") is True
            row["delete_supported"] = not is_ref and row.get("deletable") is True
            row["delete_reason"] = "참조 전용 libvirt 디스크는 원본 파일 삭제를 지원하지 않습니다." if is_ref else None
        else:
            row["delete_supported"] = False
            row["delete_reason"] = "호스트 디스크 또는 프로젝트 관리 디스크는 웹에서 직접 삭제할 수 없습니다."
    return {
        "volumes": [volumes[key] for key in sorted(volumes)],
        "warnings": sorted(warnings),
        "create_backends": create_backends,
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
    """Configured and host-observed networks; configuration alone is not listener proof."""
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
    observed = host_inventory.observe()
    warnings.update(observed["warnings"])
    for host_network in observed["networks"]:
        target = next(
            (
                row
                for row in networks.values()
                if row["backend"] == host_network["backend"]
                and (row["name"].removeprefix("lima:") if row["backend"] == "lima-vz" else row["name"])
                == host_network["name"]
                and row["kind"] in {"lima", "libvirt"}
                and row["status"] != "isolated"
            ),
            None,
        )
        if target is None:
            networks[host_network["id"]] = host_network
        else:
            attached = {(item["name"], item.get("run_id")): item for item in target["attachments"]}
            attached.update({(item["name"], item.get("run_id")): item for item in host_network["attachments"]})
            target["attachments"] = list(attached.values())
            for field in ("mode", "subnet", "gateway", "status", "source"):
                if host_network.get(field) is not None:
                    target[field] = host_network[field]
    for row in networks.values():
        row["attachments"].sort(key=lambda item: (item["name"], item["run_id"] or ""))
    return {
        "networks": [networks[key] for key in sorted(networks)],
        "warnings": sorted(warnings),
        "classification": "configured-network-observation",
    }
