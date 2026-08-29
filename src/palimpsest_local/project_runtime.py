"""Transactional lifecycle orchestration for declarative Palimpsest projects.

This module deliberately does not know how a stack is resolved or how a KVM/Lima
VM is created.  Those operations are supplied through :class:`ProjectCallbacks`.
That boundary keeps project planning read-only until *all* selected services have
been resolved and preflighted, and makes the same lifecycle usable by both local
backends.

Only names, timestamps, and configuration digests are written to project state.
Resolved environment values, cloud-init documents, and callback return values are
never serialized.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from . import state
from .digest import InvalidDigestError, require_digest
from .errors import LifecycleError, StateError
from .project import (
    Project,
    ServiceSpec,
    canonical_project_payload,
    deterministic_service_name,
    project_config_digest,
    service_start_order,
)
from .runtime_types import (
    DispatchKey,
    ExpectedRunIdentity,
    LifecycleResult,
    RunResult,
    RuntimeBackend,
    RuntimeKind,
)

PROJECT_STATE_SCHEMA_VERSION = 2
_MIB = 1024 * 1024

_LOGICAL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_RUN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_KNOWN_RUNTIME_STATUSES = {
    "creating",
    "defined",
    "starting",
    "running",
    "stopping",
    "stopped",
    "removed",
    "failed",
}
_KNOWN_BACKENDS = {"kvm", "lima-vz", "libvirt-hvf"}

UpAction = Literal["noop", "create", "start", "recreate"]


class ProjectLifecycleError(LifecycleError):
    """A project cannot be planned or reconciled without unsafe ambiguity."""


class ProjectPrepareError(ProjectLifecycleError):
    """Volume preparation failed after creating one or more durable volumes.

    Named project volumes deliberately survive a failed ``up``.  Adapters use
    this exception to return the safe ownership records that were committed
    before a later preparation step failed, so ``down --volumes`` can still
    remove them explicitly.
    """

    def __init__(self, message: str, volumes: Sequence[ManagedVolume] = ()) -> None:
        super().__init__(message)
        self.volumes = tuple(volumes)


@dataclass(frozen=True)
class ManagedService:
    """State-safe ownership record for one service VM."""

    service: str
    run_name: str
    config_digest: str
    run_id: str
    backend: str


@dataclass(frozen=True)
class ManagedVolume:
    """State-safe backend binding for one project-owned named volume."""

    name: str
    backend: str
    size_bytes: int


@dataclass(frozen=True)
class ProjectState:
    """Schema-versioned owner ledger for one declarative project."""

    schema_version: int
    project: str
    config_digest: str
    services: Mapping[str, ManagedService]
    order: tuple[str, ...]
    volumes: Mapping[str, ManagedVolume]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PublishedPort:
    """A host port and its deterministic service/run ownership."""

    service: str
    run_name: str
    host_ip: str
    host_port: int
    guest_port: int
    protocol: str


@dataclass(frozen=True)
class ServicePlan:
    """One read-only action selected by project reconciliation."""

    service: str
    run_name: str
    action: UpAction
    config_digest: str
    previous_status: str | None
    preserve_config: bool = False


@dataclass(frozen=True)
class PreparedService:
    """A service whose backend inputs have been resolved but not started.

    ``resolved`` may contain short-lived credentials or rendered cloud-init, so it
    is excluded from repr/equality and is never included in project state.
    """

    project: Project = field(repr=False, compare=False)
    service: ServiceSpec = field(repr=False, compare=False)
    plan: ServicePlan
    resolved: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class DownTarget:
    """One owned service considered during a project down operation."""

    service: str
    run_name: str
    status: str | None


@dataclass(frozen=True)
class ProjectServiceStatus:
    """Sanitized project-oriented view of a runtime status."""

    service: str
    run_name: str
    status: str


@dataclass(frozen=True)
class RuntimeIdentity:
    """Immutable owner identity extracted from a runtime inspection."""

    run_id: str
    backend: str


class StartServiceCallback(Protocol):
    def __call__(
        self,
        item: PreparedService,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ) -> object: ...


class ExistingRunMutationCallback(Protocol):
    def __call__(
        self,
        name: str,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ) -> object: ...


class ExistingRunLogsCallback(Protocol):
    def __call__(
        self,
        name: str,
        follow: bool,
        *,
        expected_identity: ExpectedRunIdentity | None = None,
    ) -> Iterable[str]: ...


def _expected_mutation_identity(managed: ManagedService) -> ExpectedRunIdentity:
    try:
        backend = RuntimeBackend(managed.backend)
    except (TypeError, ValueError):
        raise ProjectLifecycleError("managed run has an invalid backend identity") from None
    return ExpectedRunIdentity(
        managed.run_name,
        managed.run_id,
        DispatchKey(RuntimeKind.CLOUD_IMAGE, backend),
    )


def _noop_preflight(_services: tuple[PreparedService, ...]) -> None:
    return None


def _noop_prepare(_services: tuple[PreparedService, ...]) -> Sequence[ManagedVolume]:
    return ()


def _noop_down_preflight(_services: tuple[DownTarget, ...], _volumes: tuple[ManagedVolume, ...]) -> None:
    return None


def _port_available(_port: PublishedPort, _replacing_run: str | None) -> bool:
    return True


def _remove_volume(_project: str, _volume: str, _backend: str, _size_bytes: int) -> object:
    return None


@dataclass(frozen=True)
class ProjectCallbacks:
    """Backend callbacks used by project lifecycle operations.

    ``inspect`` and ``resolve`` must not create, stop, or remove VMs.  ``preflight``
    is called exactly once with every mutating service action before the first
    mutation.  Adapters can use it to validate backend capabilities, disks,
    networks, image availability, and ownership as one atomic planning phase.

    ``prepare`` is then called exactly once, still before any VM is stopped or
    removed.  It may create durable named volumes and must return their sanitized
    backend/size ownership records.  Those volumes are recorded immediately and
    are intentionally preserved if a later service operation fails.

    ``start`` handles both a new VM and a stopped VM; ``plan.action`` tells the
    adapter which case applies. Existing-run start/stop/remove calls carry the
    immutable project-owned identity so adapters can reject name reuse immediately
    before backend entry. New creation and best-effort rollback remain unbound when
    no durable identity is available. For ``recreate`` the orchestrator calls
    ``stop`` and ``remove`` before ``start``. ``remove`` should release the per-run
    writable overlay/tombstone, but project named volumes are managed only by
    ``remove_volume``.

    ``desired_digest`` can extend the structural service digest with a one-way
    fingerprint of execution-only inputs such as resolved environment and
    cloud-init values.  The returned canonical SHA-256 digest is the only value
    retained by the project ledger.
    """

    inspect: Callable[[str], object | None]
    resolve: Callable[[Project, ServiceSpec, str], object]
    start: StartServiceCallback
    stop: ExistingRunMutationCallback
    remove: ExistingRunMutationCallback
    preflight: Callable[[tuple[PreparedService, ...]], None] = _noop_preflight
    prepare: Callable[[tuple[PreparedService, ...]], Sequence[ManagedVolume]] = _noop_prepare
    preflight_down: Callable[[tuple[DownTarget, ...], tuple[ManagedVolume, ...]], None] = _noop_down_preflight
    port_available: Callable[[PublishedPort, str | None], bool] = _port_available
    remove_volume: Callable[[str, str, str, int], object] = _remove_volume
    logs: ExistingRunLogsCallback | None = None
    desired_digest: Callable[[Project, ServiceSpec], str] | None = None


@dataclass(frozen=True)
class UpResult:
    project: str
    actions: tuple[ServicePlan, ...]
    created: tuple[str, ...]
    state_path: Path

    @property
    def changed(self) -> bool:
        return any(item.action != "noop" for item in self.actions)


@dataclass(frozen=True)
class DownResult:
    project: str
    removed_services: tuple[str, ...]
    removed_volumes: tuple[str, ...]
    state_path: Path

    @property
    def changed(self) -> bool:
        return bool(self.removed_services or self.removed_volumes)


def service_run_name(project: Project, service: str) -> str:
    """Return the stable VM name used for a project service."""

    if service not in project.services:
        raise ProjectLifecycleError(f"unknown project service: {service!r}")
    return deterministic_service_name(project.name, service)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def service_config_digest(project: Project, service_name: str) -> str:
    """Hash one service plus the network/volume definitions it references."""

    service = project.services.get(service_name)
    if service is None:
        raise ProjectLifecycleError(f"unknown project service: {service_name!r}")
    payload = canonical_project_payload(project)
    service_payload = payload["services"][service_name]
    network_payload = {name: payload["networks"][name] for name in sorted(service.networks)}
    volume_names = sorted({mount.source for mount in service.volumes if mount.type == "volume"})
    volume_payload = {name: payload["volumes"][name] for name in volume_names}
    return _sha256_payload(
        {
            "project_version": project.version,
            "service": service_payload,
            "networks": network_payload,
            "volumes": volume_payload,
        }
    )


def published_ports(project: Project, services: Sequence[str] | None = None) -> tuple[PublishedPort, ...]:
    """Return deterministic, query-friendly published port records."""

    selected = sorted(project.services) if services is None else list(dict.fromkeys(services))
    unknown = sorted(set(selected) - set(project.services))
    if unknown:
        raise ProjectLifecycleError(f"unknown selected service(s): {', '.join(unknown)}")
    result: list[PublishedPort] = []
    for service_name in selected:
        run_name = service_run_name(project, service_name)
        for port in project.services[service_name].ports:
            result.append(
                PublishedPort(
                    service=service_name,
                    run_name=run_name,
                    host_ip=port.host_ip,
                    host_port=port.host_port,
                    guest_port=port.guest_port,
                    protocol=port.protocol,
                )
            )
    return tuple(sorted(result, key=lambda item: (item.host_ip, item.host_port, item.protocol, item.service)))


def runtime_status(value: object | None) -> str | None:
    """Normalize supported runtime inspect shapes to a lifecycle status."""

    if value is None:
        return None
    if isinstance(value, (RunResult, LifecycleResult)):
        status = value.status if isinstance(value, RunResult) else value.current_status
    elif isinstance(value, str):
        status = value
    elif isinstance(value, Mapping):
        candidate = value.get("status")
        if candidate is None and isinstance(value.get("state"), Mapping):
            candidate = value["state"].get("status")
        if not isinstance(candidate, str):
            raise ProjectLifecycleError("runtime inspection did not contain a string status")
        status = candidate
    else:
        raise ProjectLifecycleError("runtime inspection must return None, a status string, or a mapping")
    if status not in _KNOWN_RUNTIME_STATUSES:
        raise ProjectLifecycleError(f"runtime inspection returned unsupported status: {status!r}")
    return status


def runtime_identity(value: object | None) -> RuntimeIdentity | None:
    """Extract the immutable run UUID and backend from an inspect/start result."""

    if value is None:
        return None
    if isinstance(value, (RunResult, LifecycleResult)):
        return RuntimeIdentity(
            run_id=value.record.run_id,
            backend=value.record.dispatch_key.backend.value,
        )
    if not isinstance(value, Mapping):
        raise ProjectLifecycleError("runtime identity requires a mapping inspection result")
    nested_state = value.get("state")
    runtime_state = nested_state if isinstance(nested_state, Mapping) else value
    owner = value.get("owner")
    owner_mapping = owner if isinstance(owner, Mapping) else value
    run_id = owner_mapping.get("run_id")
    if not isinstance(run_id, str):
        raise ProjectLifecycleError("runtime inspection is missing owner.run_id")
    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except ValueError as exc:
        raise ProjectLifecycleError("runtime inspection contains an invalid owner.run_id") from exc
    if run_id != canonical_run_id:
        raise ProjectLifecycleError("runtime inspection owner.run_id must be a canonical UUID")
    backend = runtime_state.get("backend")
    if backend is None:
        # KVM ledgers predate the explicit backend field; Lima always records one.
        backend = "kvm"
    if not isinstance(backend, str) or backend not in _KNOWN_BACKENDS:
        raise ProjectLifecycleError(f"runtime inspection contains an unsupported backend: {backend!r}")
    return RuntimeIdentity(run_id=run_id, backend=backend)


def _assert_managed_identity(managed: ManagedService, inspected: object) -> RuntimeIdentity:
    identity = runtime_identity(inspected)
    if identity is None:  # pragma: no cover - caller only invokes for present runs
        raise ProjectLifecycleError(f"managed run {managed.run_name!r} disappeared during identity validation")
    if identity.run_id != managed.run_id or identity.backend != managed.backend:
        raise ProjectLifecycleError(
            f"managed run {managed.run_name!r} identity changed; expected {managed.run_id}/{managed.backend}, "
            f"found {identity.run_id}/{identity.backend}"
        )
    return identity


def _assert_local_managed_identity(
    roots: state.StatePaths,
    managed: ManagedService,
) -> None:
    rpaths = state.run_paths(roots, managed.run_name)
    try:
        owner = state.read_owner_record(rpaths)
        runtime_state = state.read_run_state(rpaths)
    except StateError as exc:
        raise ProjectLifecycleError(f"managed run {managed.run_name!r} is missing or has invalid local state") from exc
    _assert_managed_identity(managed, {"owner": {"run_id": owner.run_id}, "state": runtime_state})


def _state_payload(record: ProjectState) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "project": record.project,
        "config_digest": record.config_digest,
        # A sequence avoids treating user-chosen service names as JSON object
        # fields.  The shared state writer intentionally rejects secret-shaped
        # field names (for example ``token``), while such words are valid service
        # names and are harmless as values.
        "services": [
            {
                "service": managed.service,
                "run_name": managed.run_name,
                "config_digest": managed.config_digest,
                "run_id": managed.run_id,
                "backend": managed.backend,
            }
            for _name, managed in sorted(record.services.items())
        ],
        "order": list(record.order),
        "volumes": [
            {"name": volume.name, "backend": volume.backend, "size_bytes": volume.size_bytes}
            for _name, volume in sorted(record.volumes.items())
        ],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _valid_digest(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise StateError(f"{context} must be a digest string")
    try:
        return require_digest(value)
    except InvalidDigestError as exc:
        raise StateError(f"{context} is not a canonical SHA-256 digest") from exc


def _valid_name(value: object, context: str, *, run: bool = False) -> str:
    pattern = _RUN_NAME_RE if run else _LOGICAL_NAME_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise StateError(f"{context} contains an invalid name")
    return value


def _valid_run_id(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise StateError(f"{context} must be a UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise StateError(f"{context} is not a valid UUID") from exc
    if value != canonical:
        raise StateError(f"{context} must be a canonical UUID")
    return value


def _valid_backend(value: object, context: str) -> str:
    if not isinstance(value, str) or value not in _KNOWN_BACKENDS:
        raise StateError(f"{context} must be one of: {', '.join(sorted(_KNOWN_BACKENDS))}")
    return value


def _valid_volume_size(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < _MIB or value % _MIB:
        raise StateError(f"{context} must be a positive MiB-aligned byte count")
    return value


def _decode_state(payload: Mapping[str, Any], expected_project: str) -> ProjectState:
    expected_keys = {
        "schema_version",
        "project",
        "config_digest",
        "services",
        "order",
        "volumes",
        "created_at",
        "updated_at",
    }
    if set(payload) != expected_keys:
        raise StateError("project state has unknown or missing fields")
    if payload["schema_version"] != PROJECT_STATE_SCHEMA_VERSION:
        raise StateError(f"unsupported project state schema version: {payload['schema_version']!r}")
    project_name = _valid_name(payload["project"], "project state project")
    if project_name != expected_project:
        raise StateError(f"project state owner mismatch: {project_name!r} != {expected_project!r}")
    config_digest = _valid_digest(payload["config_digest"], "project state config_digest")
    raw_services = payload["services"]
    if not isinstance(raw_services, list):
        raise StateError("project state services must be a sequence")
    services: dict[str, ManagedService] = {}
    for raw in raw_services:
        if not isinstance(raw, Mapping) or set(raw) != {
            "service",
            "run_name",
            "config_digest",
            "run_id",
            "backend",
        }:
            raise StateError("project state contains a malformed service record")
        name = _valid_name(raw["service"], "project state service")
        if name in services:
            raise StateError(f"project state contains duplicate service {name!r}")
        run_name = _valid_name(raw["run_name"], f"project state run for {name!r}", run=True)
        expected_run_name = deterministic_service_name(project_name, name)
        if run_name != expected_run_name:
            raise StateError(f"project state run mapping for {name!r} is {run_name!r}, expected {expected_run_name!r}")
        services[name] = ManagedService(
            service=name,
            run_name=run_name,
            config_digest=_valid_digest(raw["config_digest"], f"project state digest for {name!r}"),
            run_id=_valid_run_id(raw["run_id"], f"project state run_id for {name!r}"),
            backend=_valid_backend(raw["backend"], f"project state backend for {name!r}"),
        )
    run_names = [managed.run_name for managed in services.values()]
    if len(set(run_names)) != len(run_names):
        raise StateError("project state cannot map multiple services to one run")
    raw_order = payload["order"]
    if not isinstance(raw_order, list) or any(not isinstance(item, str) for item in raw_order):
        raise StateError("project state order must be a string sequence")
    order = tuple(_valid_name(item, "project state order") for item in raw_order)
    if len(set(order)) != len(order) or set(order) != set(services):
        raise StateError("project state order must contain every managed service exactly once")
    raw_volumes = payload["volumes"]
    if not isinstance(raw_volumes, list):
        raise StateError("project state volumes must be a sequence")
    volumes: dict[str, ManagedVolume] = {}
    for raw_volume in raw_volumes:
        if not isinstance(raw_volume, Mapping) or set(raw_volume) != {"name", "backend", "size_bytes"}:
            raise StateError("project state contains a malformed volume record")
        volume_name = _valid_name(raw_volume["name"], "project state volume")
        if volume_name in volumes:
            raise StateError(f"project state contains duplicate volume {volume_name!r}")
        volumes[volume_name] = ManagedVolume(
            name=volume_name,
            backend=_valid_backend(raw_volume["backend"], f"project state volume backend for {volume_name!r}"),
            size_bytes=_valid_volume_size(raw_volume["size_bytes"], f"project state volume size for {volume_name!r}"),
        )
    created_at = payload["created_at"]
    updated_at = payload["updated_at"]
    if not isinstance(created_at, str) or not created_at or not isinstance(updated_at, str) or not updated_at:
        raise StateError("project state timestamps must be nonempty strings")
    return ProjectState(
        PROJECT_STATE_SCHEMA_VERSION,
        project_name,
        config_digest,
        MappingProxyType(dict(sorted(services.items()))),
        order,
        MappingProxyType(dict(sorted(volumes.items()))),
        created_at,
        updated_at,
    )


def read_project_state(project: Project, roots: state.StatePaths) -> ProjectState | None:
    """Read and strictly validate a project ledger, returning ``None`` if absent."""

    ppaths = state.project_paths(roots, project.name)
    if not ppaths.state.exists():
        return None
    return _decode_state(state.read_json(ppaths.state), project.name)


def _write_project_state(ppaths: state.ProjectPaths, record: ProjectState) -> None:
    state.atomic_write_json(ppaths.state, _state_payload(record))


def _new_state(project: Project) -> ProjectState:
    now = state.utc_now_iso()
    return ProjectState(
        schema_version=PROJECT_STATE_SCHEMA_VERSION,
        project=project.name,
        config_digest=project_config_digest(project),
        services=MappingProxyType({}),
        order=(),
        volumes=MappingProxyType({}),
        created_at=now,
        updated_at=now,
    )


def _replace_state(
    record: ProjectState,
    *,
    project_digest: str | None = None,
    services: Mapping[str, ManagedService] | None = None,
    order: Sequence[str] | None = None,
    volumes: Mapping[str, ManagedVolume] | None = None,
) -> ProjectState:
    return ProjectState(
        schema_version=record.schema_version,
        project=record.project,
        config_digest=project_digest or record.config_digest,
        services=MappingProxyType(dict(sorted((record.services if services is None else services).items()))),
        order=tuple(record.order if order is None else order),
        volumes=MappingProxyType(dict(sorted((record.volumes if volumes is None else volumes).items()))),
        created_at=record.created_at,
        updated_at=state.utc_now_iso(),
    )


def _referenced_managed_volumes(project: Project, service_names: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for service_name in service_names:
        for mount in project.services[service_name].volumes:
            if mount.type == "volume" and not project.volumes[mount.source].external:
                result.add(mount.source)
    return result


def _merge_prepared_volumes(
    project: Project,
    prepared: Sequence[PreparedService],
    existing: Mapping[str, ManagedVolume],
    supplied: Sequence[ManagedVolume],
) -> dict[str, ManagedVolume]:
    """Validate adapter volume receipts before copying them into the ledger."""

    expected_names = _referenced_managed_volumes(project, [item.plan.service for item in prepared])
    merged = dict(existing)
    seen: set[str] = set()
    for volume in supplied:
        if not isinstance(volume, ManagedVolume):
            raise ProjectLifecycleError("project prepare returned an invalid managed-volume record")
        if volume.name in seen:
            raise ProjectLifecycleError(f"project prepare returned duplicate volume {volume.name!r}")
        seen.add(volume.name)
        if volume.name not in expected_names:
            raise ProjectLifecycleError(f"project prepare returned unrequested volume {volume.name!r}")
        _valid_name(volume.name, "prepared volume name")
        _valid_backend(volume.backend, f"prepared volume backend for {volume.name!r}")
        _valid_volume_size(volume.size_bytes, f"prepared volume size for {volume.name!r}")
        expected_size = project.volumes[volume.name].size_mib * _MIB
        if volume.size_bytes != expected_size:
            raise ProjectLifecycleError(
                f"prepared volume {volume.name!r} has size {volume.size_bytes}, expected {expected_size} bytes"
            )
        prior = merged.get(volume.name)
        if prior is not None and prior != volume:
            raise ProjectLifecycleError(
                f"managed volume {volume.name!r} is bound to {prior.backend}/{prior.size_bytes} bytes, "
                f"but preparation returned {volume.backend}/{volume.size_bytes} bytes"
            )
        merged[volume.name] = volume
    return merged


def _successful_service_order(
    project: Project,
    services: Mapping[str, ManagedService],
    previous_order: Sequence[str],
) -> tuple[str, ...]:
    """Refresh current DAG order while retaining removed-from-YAML services stably."""

    current = [name for name in service_start_order(project) if name in services]
    orphans = [name for name in previous_order if name in services and name not in project.services]
    known = set(current) | set(orphans)
    # Defensive support for a valid future ledger whose old order was repaired
    # independently.  Unknown entries have no dependency data, so lexical order
    # is the only stable fail-closed choice.
    orphans.extend(sorted(set(services) - known))
    return tuple(current + orphans)


def _build_up_plan(
    project: Project,
    ledger: ProjectState,
    callbacks: ProjectCallbacks,
    selected: Sequence[str] | None,
    *,
    no_recreate: bool,
    force_recreate: bool,
) -> tuple[ServicePlan, ...]:
    if no_recreate and force_recreate:
        raise ProjectLifecycleError("no_recreate and force_recreate are mutually exclusive")
    order = service_start_order(project, selected)
    plans: list[ServicePlan] = []
    for service_name in order:
        run_name = service_run_name(project, service_name)
        managed = ledger.services.get(service_name)
        if managed is not None and managed.run_name != run_name:
            raise StateError(
                f"project service {service_name!r} maps to {managed.run_name!r}, expected deterministic run {run_name!r}"
            )
        inspected = callbacks.inspect(run_name)
        status = runtime_status(inspected)
        if managed is None and status is not None:
            raise ProjectLifecycleError(
                f"run name {run_name!r} already exists but is not owned by project service {service_name!r}"
            )
        if managed is not None and status is not None:
            _assert_managed_identity(managed, inspected)
        if status in {"creating", "starting", "stopping"}:
            raise ProjectLifecycleError(
                f"service {service_name!r} is in transitional status {status!r}; wait for it to settle before project up"
            )
        if managed is not None and no_recreate:
            if status == "running":
                plans.append(ServicePlan(service_name, run_name, "noop", managed.config_digest, status, True))
                continue
            if status == "stopped":
                raise ProjectLifecycleError(
                    f"service {service_name!r} is stopped; --no-recreate restart is disabled in v1 because "
                    "backend disk/network/port configuration cannot yet be proven unchanged"
                )
            raise ProjectLifecycleError(
                f"service {service_name!r} is {status or 'missing'!r} and cannot recover without recreation; "
                "remove --no-recreate or repair the existing runtime"
            )
        if callbacks.desired_digest is None:
            desired_digest = service_config_digest(project, service_name)
        else:
            desired_digest = callbacks.desired_digest(project, project.services[service_name])
            try:
                desired_digest = require_digest(desired_digest)
            except (InvalidDigestError, TypeError) as exc:
                raise ProjectLifecycleError(
                    f"desired digest callback returned an invalid digest for service {service_name!r}"
                ) from exc
        drifted = managed is not None and managed.config_digest != desired_digest
        if managed is None or status is None:
            action: UpAction = "create"
        elif force_recreate:
            action = "recreate"
        elif drifted and no_recreate:
            if status == "running":
                action = "noop"
            elif status == "stopped":
                action = "start"
            else:
                raise ProjectLifecycleError(
                    f"service {service_name!r} is {status!r} and cannot recover without recreation; "
                    "remove --no-recreate or use --force-recreate"
                )
        elif drifted or status in {"defined", "failed", "removed"}:
            action = "recreate"
        elif status == "running":
            action = "noop"
        elif status == "stopped":
            action = "start"
        else:  # pragma: no cover - every supported status is handled above
            raise ProjectLifecycleError(f"service {service_name!r} cannot be planned from status {status!r}")
        plans.append(ServicePlan(service_name, run_name, action, desired_digest, status))
    return tuple(plans)


def _prepare_actions(
    project: Project,
    plans: tuple[ServicePlan, ...],
    callbacks: ProjectCallbacks,
) -> tuple[PreparedService, ...]:
    prepared: list[PreparedService] = []
    for plan in plans:
        if plan.action == "noop":
            continue
        service = project.services[plan.service]
        resolved = None if plan.preserve_config else callbacks.resolve(project, service, plan.run_name)
        prepared.append(PreparedService(project, service, plan, resolved))
    if not prepared:
        return ()
    current_inputs = [item for item in prepared if not item.plan.preserve_config]
    by_service = {item.plan.service: item for item in current_inputs}
    for port in published_ports(project, [item.plan.service for item in current_inputs]):
        plan = by_service[port.service].plan
        replacing = (
            port.run_name
            if plan.action == "recreate" and plan.previous_status in {"running", "starting", "creating"}
            else None
        )
        if not callbacks.port_available(port, replacing):
            raise ProjectLifecycleError(
                f"host port {port.host_ip}:{port.host_port}/{port.protocol} required by service "
                f"{port.service!r} is unavailable"
            )
    callbacks.preflight(tuple(prepared))
    return tuple(prepared)


def up_project(
    project: Project,
    callbacks: ProjectCallbacks,
    *,
    roots: state.StatePaths | None = None,
    services: Sequence[str] | None = None,
    no_recreate: bool = False,
    force_recreate: bool = False,
) -> UpResult:
    """Reconcile selected services after a complete read-only preflight.

    Dependencies are always included and started first.  On failure only VMs that
    were newly created by this invocation are stopped and removed; pre-existing
    services (including ones merely restarted) are not rolled back.
    """

    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots) or _new_state(project)
        plans = _build_up_plan(
            project,
            ledger,
            callbacks,
            services,
            no_recreate=no_recreate,
            force_recreate=force_recreate,
        )
        prepared = _prepare_actions(project, plans, callbacks)
        if not prepared:
            refreshed_order = _successful_service_order(project, ledger.services, ledger.order)
            if refreshed_order != ledger.order:
                refreshed = _replace_state(
                    ledger,
                    project_digest=project_config_digest(project),
                    order=refreshed_order,
                )
                _write_project_state(ppaths, refreshed)
            return UpResult(project.name, plans, (), ppaths.state)

        mutable_services = dict(ledger.services)
        mutable_order = list(ledger.order)
        mutable_volumes = dict(ledger.volumes)
        current = ledger
        created: list[PreparedService] = []
        created_identities: dict[str, ExpectedRunIdentity] = {}
        prepared_by_service = {item.plan.service: item for item in prepared}
        try:
            current_input_prepared = tuple(item for item in prepared if not item.plan.preserve_config)
            try:
                supplied_volumes = tuple(callbacks.prepare(current_input_prepared)) if current_input_prepared else ()
            except ProjectPrepareError as prepare_exc:
                mutable_volumes = _merge_prepared_volumes(
                    project,
                    current_input_prepared,
                    mutable_volumes,
                    prepare_exc.volumes,
                )
                if mutable_volumes != dict(current.volumes):
                    current = _replace_state(
                        current,
                        project_digest=project_config_digest(project),
                        volumes=mutable_volumes,
                    )
                    _write_project_state(ppaths, current)
                raise
            mutable_volumes = _merge_prepared_volumes(
                project,
                current_input_prepared,
                mutable_volumes,
                supplied_volumes,
            )
            if mutable_volumes != dict(current.volumes):
                current = _replace_state(
                    current,
                    project_digest=project_config_digest(project),
                    volumes=mutable_volumes,
                )
                _write_project_state(ppaths, current)
            for plan in plans:
                if plan.action == "noop":
                    continue
                item = prepared_by_service[plan.service]
                prior_managed = mutable_services.get(plan.service)
                expected_identity = _expected_mutation_identity(prior_managed) if prior_managed is not None else None
                if prior_managed is not None and plan.previous_status is not None:
                    before_mutation = callbacks.inspect(plan.run_name)
                    if before_mutation is None:
                        raise ProjectLifecycleError(
                            f"managed run {plan.run_name!r} disappeared after project preflight"
                        )
                    _assert_managed_identity(prior_managed, before_mutation)
                if plan.action == "recreate":
                    if plan.previous_status not in {None, "removed"}:
                        callbacks.stop(plan.run_name, expected_identity=expected_identity)
                    callbacks.remove(plan.run_name, expected_identity=expected_identity)
                if plan.action in {"create", "recreate"}:
                    created.append(item)
                if plan.action == "start":
                    callbacks.start(item, expected_identity=expected_identity)
                else:
                    callbacks.start(item)
                started = callbacks.inspect(plan.run_name)
                if runtime_status(started) != "running":
                    raise ProjectLifecycleError(
                        f"service {plan.service!r} did not report running after its start callback"
                    )
                identity = runtime_identity(started)
                if identity is None:  # pragma: no cover - running inspection cannot be None
                    raise ProjectLifecycleError(f"service {plan.service!r} did not report a runtime identity")
                previous = mutable_services.get(plan.service)
                if plan.action == "start" and previous is not None:
                    _assert_managed_identity(previous, started)
                applied_digest = (
                    previous.config_digest
                    if no_recreate and previous is not None and previous.config_digest != plan.config_digest
                    else plan.config_digest
                )
                mutable_services[plan.service] = ManagedService(
                    plan.service,
                    plan.run_name,
                    applied_digest,
                    identity.run_id,
                    identity.backend,
                )
                if plan.action in {"create", "recreate"}:
                    created_identities[plan.service] = _expected_mutation_identity(mutable_services[plan.service])
                if plan.service not in mutable_order:
                    mutable_order.append(plan.service)
                referenced_volumes = (
                    set() if plan.preserve_config else _referenced_managed_volumes(project, [plan.service])
                )
                for volume_name in referenced_volumes:
                    existing_volume = mutable_volumes.get(volume_name)
                    size_bytes = project.volumes[volume_name].size_mib * _MIB
                    if existing_volume is not None and (
                        existing_volume.backend != identity.backend or existing_volume.size_bytes != size_bytes
                    ):
                        raise ProjectLifecycleError(
                            f"managed volume {volume_name!r} is bound to "
                            f"{existing_volume.backend}/{existing_volume.size_bytes} bytes, but service "
                            f"{plan.service!r} requests {identity.backend}/{size_bytes} bytes"
                        )
                    mutable_volumes[volume_name] = ManagedVolume(volume_name, identity.backend, size_bytes)
                current = _replace_state(
                    current,
                    project_digest=project_config_digest(project),
                    services=mutable_services,
                    order=mutable_order,
                    volumes=mutable_volumes,
                )
                _write_project_state(ppaths, current)

            refreshed_order = _successful_service_order(project, mutable_services, mutable_order)
            if refreshed_order != current.order:
                mutable_order = list(refreshed_order)
                current = _replace_state(
                    current,
                    project_digest=project_config_digest(project),
                    order=mutable_order,
                )
                _write_project_state(ppaths, current)
        except Exception as exc:
            rollback_errors: list[str] = []
            for item in reversed(created):
                expected_identity = created_identities.get(item.plan.service)
                cleanup_failed = False
                try:
                    if expected_identity is None:
                        callbacks.stop(item.plan.run_name)
                    else:
                        callbacks.stop(item.plan.run_name, expected_identity=expected_identity)
                except Exception as rollback_exc:  # cleanup continues best-effort
                    cleanup_failed = True
                    rollback_errors.append(f"stop {item.plan.run_name}: {rollback_exc}")
                try:
                    if expected_identity is None:
                        callbacks.remove(item.plan.run_name)
                    else:
                        callbacks.remove(item.plan.run_name, expected_identity=expected_identity)
                except Exception as rollback_exc:  # cleanup continues best-effort
                    cleanup_failed = True
                    rollback_errors.append(f"remove {item.plan.run_name}: {rollback_exc}")
                if not cleanup_failed:
                    mutable_services.pop(item.plan.service, None)
                    if item.plan.service in mutable_order:
                        mutable_order.remove(item.plan.service)
            rollback_changed = (
                dict(current.services) != mutable_services
                or current.order != tuple(mutable_order)
                or dict(current.volumes) != mutable_volumes
            )
            if rollback_changed:
                current = _replace_state(
                    current,
                    services=mutable_services,
                    order=mutable_order,
                    volumes=mutable_volumes,
                )
                try:
                    _write_project_state(ppaths, current)
                except Exception as rollback_exc:
                    rollback_errors.append(f"write project ledger: {rollback_exc}")
            detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
            raise ProjectLifecycleError(f"project up failed: {exc}{detail}") from exc

        return UpResult(project.name, plans, tuple(item.plan.service for item in created), ppaths.state)


def down_project(
    project: Project,
    callbacks: ProjectCallbacks,
    *,
    roots: state.StatePaths | None = None,
    volumes: bool = False,
) -> DownResult:
    """Stop and remove owned service VMs in reverse dependency/start order."""

    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots)
        if ledger is None:
            return DownResult(project.name, (), (), ppaths.state)
        targets: list[DownTarget] = []
        for service_name in reversed(ledger.order):
            managed = ledger.services[service_name]
            inspected = callbacks.inspect(managed.run_name)
            status = runtime_status(inspected)
            if status is not None:
                _assert_managed_identity(managed, inspected)
            targets.append(DownTarget(service_name, managed.run_name, status))
        volume_targets = tuple(volume for _name, volume in sorted(ledger.volumes.items())) if volumes else ()
        callbacks.preflight_down(tuple(targets), volume_targets)

        current = ledger
        mutable_services = dict(ledger.services)
        mutable_order = list(ledger.order)
        removed_services: list[str] = []
        for target in targets:
            managed = mutable_services[target.service]
            expected_identity = _expected_mutation_identity(managed)
            before_mutation = callbacks.inspect(target.run_name)
            if before_mutation is not None:
                _assert_managed_identity(managed, before_mutation)
            if target.status not in {None, "removed"}:
                callbacks.stop(target.run_name, expected_identity=expected_identity)
            before_remove = callbacks.inspect(target.run_name)
            if before_remove is not None:
                _assert_managed_identity(managed, before_remove)
            # Removal also clears an owned stopped/removed/missing run ledger or
            # tombstone.  The adapter must verify ownership before doing so.
            callbacks.remove(target.run_name, expected_identity=expected_identity)
            mutable_services.pop(target.service, None)
            mutable_order.remove(target.service)
            removed_services.append(target.service)
            current = _replace_state(current, services=mutable_services, order=mutable_order)
            _write_project_state(ppaths, current)

        mutable_volumes = dict(current.volumes)
        removed_volumes: list[str] = []
        for managed_volume in volume_targets:
            callbacks.remove_volume(
                project.name,
                managed_volume.name,
                managed_volume.backend,
                managed_volume.size_bytes,
            )
            mutable_volumes.pop(managed_volume.name)
            removed_volumes.append(managed_volume.name)
            current = _replace_state(current, volumes=mutable_volumes)
            _write_project_state(ppaths, current)
        return DownResult(project.name, tuple(removed_services), tuple(removed_volumes), ppaths.state)


def project_ps(
    project: Project,
    inspect: Callable[[str], object | None],
    *,
    roots: state.StatePaths | None = None,
) -> tuple[ProjectServiceStatus, ...]:
    """Return service names mapped to their owned runtime status."""

    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots)
        if ledger is None:
            return ()
        result = []
        for service_name in ledger.order:
            managed = ledger.services[service_name]
            inspected = inspect(managed.run_name)
            status = runtime_status(inspected) or "missing"
            if inspected is not None:
                _assert_managed_identity(managed, inspected)
            result.append(ProjectServiceStatus(service_name, managed.run_name, status))
        return tuple(result)


def project_log_targets(
    project: Project,
    services: Sequence[str] | None = None,
    *,
    roots: state.StatePaths | None = None,
    inspect: Callable[[str], object | None] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Map selected logical service names to owned run names for logs/exec."""

    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots)
        if ledger is None:
            raise ProjectLifecycleError(f"project {project.name!r} has not been started")
        selected = list(ledger.order) if services is None else list(dict.fromkeys(services))
        unknown = sorted(set(selected) - set(ledger.services))
        if unknown:
            raise ProjectLifecycleError(f"service(s) are not managed by this project: {', '.join(unknown)}")
        targets = []
        for name in selected:
            managed = ledger.services[name]
            if inspect is None:
                _assert_local_managed_identity(roots, managed)
            else:
                inspected = inspect(managed.run_name)
                if inspected is None:
                    raise ProjectLifecycleError(f"managed run {managed.run_name!r} is missing")
                _assert_managed_identity(managed, inspected)
            targets.append((name, managed.run_name))
        return tuple(targets)


def managed_run_name(
    project: Project,
    service: str,
    *,
    roots: state.StatePaths | None = None,
    inspect: Callable[[str], object | None] | None = None,
) -> str:
    """Resolve one managed logical service for ``exec``, ``stop``, or inspection."""

    return project_log_targets(project, [service], roots=roots, inspect=inspect)[0][1]


def project_service_operation(
    project: Project,
    service: str,
    inspect: Callable[[str], object | None],
    operation: ExistingRunMutationCallback,
    *,
    roots: state.StatePaths | None = None,
) -> object:
    """Run one service operation while its project owner identity stays locked."""

    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots)
        if ledger is None:
            raise ProjectLifecycleError(f"project {project.name!r} has not been started")
        managed = ledger.services.get(service)
        if managed is None:
            raise ProjectLifecycleError(f"service {service!r} is not managed by this project")
        inspected = inspect(managed.run_name)
        if inspected is None:
            raise ProjectLifecycleError(f"managed run {managed.run_name!r} is missing")
        _assert_managed_identity(managed, inspected)
        return operation(
            managed.run_name,
            expected_identity=_expected_mutation_identity(managed),
        )


def stop_project_services(
    project: Project,
    callbacks: ProjectCallbacks,
    services: Sequence[str] | None = None,
    *,
    roots: state.StatePaths | None = None,
) -> tuple[str, ...]:
    """Stop selected services under the project lock with live identity checks."""

    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots)
        if ledger is None:
            raise ProjectLifecycleError(f"project {project.name!r} has not been started")
        selected = list(ledger.order) if services is None else list(dict.fromkeys(services))
        unknown = sorted(set(selected) - set(ledger.services))
        if unknown:
            raise ProjectLifecycleError(f"service(s) are not managed by this project: {', '.join(unknown)}")
        stopped: list[str] = []
        for service_name in reversed(selected):
            managed = ledger.services[service_name]
            inspected = callbacks.inspect(managed.run_name)
            if inspected is None:
                raise ProjectLifecycleError(f"managed run {managed.run_name!r} is missing")
            _assert_managed_identity(managed, inspected)
            callbacks.stop(
                managed.run_name,
                expected_identity=_expected_mutation_identity(managed),
            )
            after = callbacks.inspect(managed.run_name)
            if after is None:
                raise ProjectLifecycleError(f"managed run {managed.run_name!r} disappeared while stopping")
            _assert_managed_identity(managed, after)
            if runtime_status(after) != "stopped":
                raise ProjectLifecycleError(f"managed run {managed.run_name!r} did not report stopped")
            stopped.append(service_name)
        return tuple(stopped)


def project_logs(
    project: Project,
    callbacks: ProjectCallbacks,
    services: Sequence[str] | None = None,
    *,
    roots: state.StatePaths | None = None,
    follow: bool = False,
) -> Iterable[tuple[str, str]]:
    """Yield ``(service, line)`` pairs without exposing backend run-name details."""

    if callbacks.logs is None:
        raise ProjectLifecycleError("the selected runtime does not provide a logs callback")
    roots = roots or state.init_roots()
    ppaths = state.project_paths(roots, project.name)
    with state.file_lock(ppaths.lock):
        ledger = read_project_state(project, roots)
        if ledger is None:
            raise ProjectLifecycleError(f"project {project.name!r} has not been started")
        selected = list(ledger.order) if services is None else list(dict.fromkeys(services))
        unknown = sorted(set(selected) - set(ledger.services))
        if unknown:
            raise ProjectLifecycleError(f"service(s) are not managed by this project: {', '.join(unknown)}")
        for service_name in selected:
            managed = ledger.services[service_name]
            inspected = callbacks.inspect(managed.run_name)
            if inspected is None:
                raise ProjectLifecycleError(f"managed run {managed.run_name!r} is missing")
            _assert_managed_identity(managed, inspected)
            for line in callbacks.logs(
                managed.run_name,
                follow,
                expected_identity=_expected_mutation_identity(managed),
            ):
                yield service_name, line
