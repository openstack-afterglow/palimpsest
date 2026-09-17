"""Path-free public projections of bounded local OCI root-volume metadata."""

from __future__ import annotations

import uuid
from typing import Any

from .errors import StateError
from .oci_root_volume import OCIRootVolumeRecord, list_oci_root_volume_records
from .state import StatePaths

LIST_SCHEMA = "palimpsest.oci-root-volumes.v1"
INSPECT_SCHEMA = "palimpsest.oci-root-volume-inspect.v1"
CLASSIFICATION = "local-metadata-observation"
GUIDANCE = "Stored metadata does not verify ext4 contents or current VM attachment and does not authorize root reuse or deletion."


class OCIRootVolumeInventoryError(StateError):
    """Fixed public refusal for unavailable or inconsistent local metadata."""


def _item(record: OCIRootVolumeRecord) -> dict[str, Any]:
    attachment = (
        None if record.attached_run_id is None else {"run_id": record.attached_run_id, "name": record.attached_run_name}
    )
    return {
        "volume_id": record.volume_id,
        "status": record.status,
        "retention_policy": record.retention_policy,
        "size_bytes": record.size_bytes,
        "lower_graph_digest": record.lower_graph_digest,
        "generation": record.generation,
        "attachment": attachment,
    }


def _records(roots: StatePaths) -> tuple[OCIRootVolumeRecord, ...]:
    try:
        records = list_oci_root_volume_records(roots)
        return tuple(sorted(records, key=lambda record: record.volume_id))
    except (StateError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise OCIRootVolumeInventoryError("OCI root-volume metadata is unavailable or inconsistent") from None


def root_volumes(roots: StatePaths) -> dict[str, Any]:
    if type(roots) is not StatePaths:
        raise OCIRootVolumeInventoryError("OCI root-volume inventory request is invalid")
    return {
        "schema": LIST_SCHEMA,
        "classification": CLASSIFICATION,
        "guidance": GUIDANCE,
        "volumes": [_item(record) for record in _records(roots)],
    }


def root_volume(roots: StatePaths, volume_id: str) -> dict[str, Any]:
    try:
        parsed = uuid.UUID(volume_id)
    except (AttributeError, TypeError, ValueError):
        raise OCIRootVolumeInventoryError("OCI root-volume ID must be a canonical UUID") from None
    if str(parsed) != volume_id:
        raise OCIRootVolumeInventoryError("OCI root-volume ID must be a canonical UUID")
    if type(roots) is not StatePaths:
        raise OCIRootVolumeInventoryError("OCI root-volume inventory request is invalid")
    for record in _records(roots):
        if record.volume_id == volume_id:
            return {
                "schema": INSPECT_SCHEMA,
                "classification": CLASSIFICATION,
                "guidance": GUIDANCE,
                "volume": _item(record),
            }
    raise OCIRootVolumeInventoryError("OCI root volume is unknown")


__all__ = ["OCIRootVolumeInventoryError", "root_volume", "root_volumes"]
