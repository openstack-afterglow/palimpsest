"""Fail-closed routing for lifecycle operations on existing run ledgers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from . import cloud_runtime, lima, state
from .errors import StateError
from .runtime_types import (
    ExistingRunRecord,
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


def start(name: str, *, roots: StatePaths | None = None) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    adapter = _adapter_for(record, RuntimeOperation.START)
    _revalidate_bound_record(record, resolved_roots)
    return adapter.start(name, roots=resolved_roots, _expected_record=record)


def stop(name: str, *, roots: StatePaths | None = None) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
    adapter = _adapter_for(record, RuntimeOperation.STOP)
    _revalidate_bound_record(record, resolved_roots)
    return adapter.stop(name, roots=resolved_roots, _expected_record=record)


def rm(name: str, *, volumes: bool = False, roots: StatePaths | None = None) -> dict[str, Any]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
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


def logs(name: str, *, roots: StatePaths | None = None, follow: bool = False) -> Iterator[str]:
    resolved_roots = roots or state.resolve_roots()
    record = resolve_existing_run(name, roots=resolved_roots)
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


__all__ = (
    "inspect_run",
    "logs",
    "resolve_existing_run",
    "rm",
    "start",
    "stop",
)
