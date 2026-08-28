"""Fail-closed routing for lifecycle operations on existing run ledgers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

from . import cloud_runtime, lima, state
from .errors import StateError
from .runtime_types import (
    ExistingRunRecord,
    ExpectedRunIdentity,
    RunAggregationError,
    RunAggregationResult,
    RunSummary,
    RuntimeBackend,
    RuntimeCapabilityError,
    RuntimeKind,
    RuntimeOperation,
)
from .state import StatePaths


def resolve_existing_run(name: str, *, roots: StatePaths | None = None) -> ExistingRunRecord:
    """Resolve a run exclusively from its validated durable owner and state ledgers."""
    resolved_roots = roots or state.resolve_roots()
    return state.read_run_dispatch_record(resolved_roots, name)


def _adapter_for(record: ExistingRunRecord, operation: RuntimeOperation) -> Any:
    if record.dispatch_key.runtime_kind is RuntimeKind.OCI_ROOT:
        raise RuntimeCapabilityError(operation, record.dispatch_key)
    if record.dispatch_key.backend is RuntimeBackend.LIMA_VZ:
        return lima
    return cloud_runtime


def _revalidate_bound_record(record: ExistingRunRecord, roots: StatePaths) -> None:
    current = state.read_run_dispatch_record(roots, record.name)
    if current != record:
        raise StateError("run ledger changed during dispatch")


def _require_expected_identity(record: ExistingRunRecord, expected_identity: ExpectedRunIdentity | None) -> None:
    if expected_identity is None:
        return
    if not isinstance(expected_identity, ExpectedRunIdentity):
        raise StateError("invalid expected run identity")
    if (
        record.name != expected_identity.name
        or record.run_id != expected_identity.run_id
        or record.dispatch_key != expected_identity.dispatch_key
    ):
        raise StateError("run identity changed before lifecycle operation")


def start(
    name: str,
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _adapter_for(record, RuntimeOperation.START)
    _revalidate_bound_record(record, resolved_roots)
    return adapter.start(name, roots=resolved_roots, _expected_record=record)


def stop(
    name: str,
    *,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _adapter_for(record, RuntimeOperation.STOP)
    _revalidate_bound_record(record, resolved_roots)
    return adapter.stop(name, roots=resolved_roots, _expected_record=record)


def rm(
    name: str,
    *,
    volumes: bool = False,
    roots: StatePaths | None = None,
    expected_identity: ExpectedRunIdentity | None = None,
) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _adapter_for(record, RuntimeOperation.RM)
    _revalidate_bound_record(record, resolved_roots)
    return adapter.rm(
        name,
        roots=resolved_roots,
        volumes=volumes,
        _expected_record=record,
    )


def inspect_run(name: str, *, roots: StatePaths | None = None) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    adapter = _adapter_for(record, RuntimeOperation.INSPECT)
    _revalidate_bound_record(record, resolved_roots)
    return adapter.inspect_run(name, roots=resolved_roots, _expected_record=record)


def logs(
    name: str,
    *,
    roots: StatePaths | None = None,
    follow: bool = False,
    expected_identity: ExpectedRunIdentity | None = None,
) -> Iterator[str]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    _require_expected_identity(record, expected_identity)
    adapter = _adapter_for(record, RuntimeOperation.LOGS)

    def validated_stream() -> Iterator[str]:
        _revalidate_bound_record(record, resolved_roots)
        yield from adapter.logs(
            name,
            roots=resolved_roots,
            follow=follow,
            _expected_record=record,
        )

    return validated_stream()


def _optional_string(raw: Any, field: str) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError("run ledger contains an invalid public field")
    return value


def _optional_integer(raw: Any, field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if type(value) is not int:
        raise StateError("run ledger contains an invalid public field")
    return value


def _project_mapping_items(
    raw_items: Any,
    *,
    fields: Mapping[str, type],
    required: frozenset[str] = frozenset(),
) -> tuple[MappingProxyType[str, Any], ...]:
    if raw_items is None:
        return ()
    if not isinstance(raw_items, tuple):
        raise StateError("run ledger contains an invalid public collection")
    projected: list[MappingProxyType[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping) or not required.issubset(raw_item):
            raise StateError("run ledger contains an invalid public collection")
        item: dict[str, Any] = {}
        for field, expected_type in fields.items():
            if field not in raw_item:
                continue
            value = raw_item[field]
            if expected_type in {int, bool}:
                valid = type(value) is expected_type
            else:
                valid = isinstance(value, expected_type)
            if not valid:
                raise StateError("run ledger contains an invalid public collection")
            item[field] = value
        projected.append(MappingProxyType(item))
    return tuple(projected)


def _project_summary(snapshot: state.RunLedgerSnapshot, *, stale: bool) -> RunSummary:
    """Build a deeply immutable public projection without host paths or raw ledger data."""
    raw = snapshot.state
    base = raw.get("base")
    if base is not None and not isinstance(base, Mapping):
        raise StateError("run ledger contains an invalid public base")
    base_digest = _optional_string(raw, "base_digest")
    base_arch = _optional_string(raw, "base_arch")
    if base_digest is None and base is not None:
        base_digest = _optional_string(base, "digest")
    if base_arch is None and base is not None:
        base_arch = _optional_string(base, "arch")

    layers = _project_mapping_items(
        raw.get("layers", ()),
        fields={"digest": str, "target_dev": str},
        required=frozenset({"digest"}),
    )
    volumes = _project_mapping_items(
        raw.get("volumes", ()),
        fields={
            "name": str,
            "mount_path": str,
            "filesystem": str,
            "read_only": bool,
            "target_dev": str,
        },
        required=frozenset({"name"}),
    )
    ports = _project_mapping_items(
        raw.get("ports", ()),
        fields={
            "host_ip": str,
            "host_port": int,
            "guest_port": int,
            "protocol": str,
        },
        required=frozenset({"host_ip", "host_port", "guest_port", "protocol"}),
    )

    guest_ip = _optional_string(raw, "guest_ip")
    raw_ssh = raw.get("ssh")
    ssh: MappingProxyType[str, Any]
    if raw_ssh is not None:
        if not isinstance(raw_ssh, Mapping):
            raise StateError("run ledger contains an invalid public SSH endpoint")
        host = _optional_string(raw_ssh, "host")
        port = _optional_integer(raw_ssh, "port")
    else:
        host = _optional_string(raw, "ssh_host")
        port = _optional_integer(raw, "ssh_local_port")
    if host is None:
        host = guest_ip
    ssh = MappingProxyType({"host": host, "port": 22 if port is None else port})

    details = MappingProxyType(
        {
            "base_digest": "" if base_digest is None else base_digest,
            "base_arch": "" if base_arch is None else base_arch,
            "layers": layers,
            "memory_mib": _optional_integer(raw, "memory_mib"),
            "vcpus": _optional_integer(raw, "vcpus"),
            "network": _optional_string(raw, "network"),
            "ports": ports,
            "volumes": volumes,
            "ssh": ssh,
            "guest_ip": guest_ip,
            "created_at": _optional_string(raw, "created_at"),
            "updated_at": _optional_string(raw, "updated_at"),
        }
    )
    status = raw.get("status")
    if not isinstance(status, str):
        raise StateError("run ledger contains an invalid status")
    return RunSummary(snapshot.record, status, details, stale=stale)


def _aggregation_error(
    *,
    operation: RuntimeOperation,
    code: str,
    message: str,
    record: ExistingRunRecord | None = None,
    name: str | None = None,
    entry_token: str | None = None,
) -> RunAggregationError:
    return RunAggregationError(
        name=record.name if record is not None else name,
        entry_token=entry_token,
        operation=operation,
        dispatch_key=record.dispatch_key if record is not None else None,
        code=code,
        message=message,
    )


def _sorted_unique_errors(errors: list[RunAggregationError]) -> tuple[RunAggregationError, ...]:
    keyed: dict[tuple[str, str, str], RunAggregationError] = {}
    for error in errors:
        key = (
            error.name or error.entry_token or "",
            error.code,
            error.operation.value,
        )
        keyed.setdefault(key, error)
    return tuple(keyed[key] for key in sorted(keyed))


def _project_or_error(
    snapshot: state.RunLedgerSnapshot,
    *,
    operation: RuntimeOperation,
    stale: bool,
) -> tuple[RunSummary | None, RunAggregationError | None]:
    try:
        return _project_summary(snapshot, stale=stale), None
    except (RecursionError, StateError, TypeError, ValueError):
        return None, _aggregation_error(
            operation=operation,
            record=snapshot.record,
            code="invalid-ledger",
            message="invalid run ledger",
        )


def ps(*, roots: StatePaths | None = None) -> RunAggregationResult:
    """Return deterministic durable summaries without backend calls or writes."""
    resolved_roots = roots or state.resolve_roots()
    snapshots, snapshot_errors = state.enumerate_run_snapshots(resolved_roots)
    summaries: list[RunSummary] = []
    errors = list(snapshot_errors)
    for snapshot in snapshots:
        summary, error = _project_or_error(snapshot, operation=RuntimeOperation.PS, stale=True)
        if summary is not None:
            summaries.append(summary)
        if error is not None:
            errors.append(error)
    return RunAggregationResult(
        tuple(sorted(summaries, key=lambda item: item.name)),
        _sorted_unique_errors(errors),
    )


def reconcile(*, roots: StatePaths | None = None) -> RunAggregationResult:
    """Live-reconcile each valid run through its exact durable dispatch record."""
    resolved_roots = roots or state.resolve_roots()
    snapshots, snapshot_errors = state.enumerate_run_snapshots(resolved_roots)
    summaries: list[RunSummary] = []
    errors = [
        RunAggregationError(
            name=error.name,
            entry_token=error.entry_token,
            operation=RuntimeOperation.RECONCILE,
            dispatch_key=error.dispatch_key,
            code=error.code,
            message=error.message,
        )
        for error in snapshot_errors
    ]
    for snapshot in snapshots:
        record = snapshot.record
        try:
            adapter = _adapter_for(record, RuntimeOperation.RECONCILE)
            _revalidate_bound_record(record, resolved_roots)
            if adapter is cloud_runtime:
                adapter_result = cloud_runtime.reconcile_run(
                    record.name,
                    roots=resolved_roots,
                    _expected_record=record,
                )
            else:
                adapter_result = lima.reconcile_run(
                    record.name,
                    roots=resolved_roots,
                    _expected_record=record,
                )
            refreshed = state.read_run_ledger_snapshot(resolved_roots, record.name)
            if refreshed.record != record:
                raise StateError("run ledger changed during reconciliation")
            summary, error = _project_or_error(
                refreshed,
                operation=RuntimeOperation.RECONCILE,
                stale=False,
            )
            if summary is None or error is not None:
                raise StateError("invalid reconciled run ledger")
            summaries.append(summary)
            adapter_warnings = adapter_result.get("warnings") if isinstance(adapter_result, Mapping) else None
            if isinstance(adapter_warnings, (list, tuple)) and adapter_warnings:
                errors.append(
                    _aggregation_error(
                        operation=RuntimeOperation.RECONCILE,
                        record=record,
                        code="runtime-warning",
                        message="runtime status changed during reconciliation",
                    )
                )
        except RuntimeCapabilityError:
            summary, projection_error = _project_or_error(
                snapshot,
                operation=RuntimeOperation.RECONCILE,
                stale=True,
            )
            if summary is not None:
                summaries.append(summary)
            if projection_error is not None:
                errors.append(projection_error)
            errors.append(
                _aggregation_error(
                    operation=RuntimeOperation.RECONCILE,
                    record=record,
                    code="runtime-capability",
                    message="runtime reconciliation is unavailable",
                )
            )
        except Exception:
            summary, projection_error = _project_or_error(
                snapshot,
                operation=RuntimeOperation.RECONCILE,
                stale=True,
            )
            if summary is not None:
                summaries.append(summary)
            if projection_error is not None:
                errors.append(projection_error)
            errors.append(
                _aggregation_error(
                    operation=RuntimeOperation.RECONCILE,
                    record=record,
                    code="runtime-failure",
                    message="runtime reconciliation failed",
                )
            )
    return RunAggregationResult(
        tuple(sorted(summaries, key=lambda item: item.name)),
        _sorted_unique_errors(errors),
    )


__all__ = (
    "inspect_run",
    "logs",
    "ps",
    "reconcile",
    "resolve_existing_run",
    "rm",
    "start",
    "stop",
)
