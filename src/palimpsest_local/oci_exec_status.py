"""Read-only public observation of one exact OCI exec mailbox."""

from __future__ import annotations

import os
import sys

from .errors import StateError
from .oci_exec_session import validate_exec_status
from .oci_monitor_client import MonitorClient
from .oci_run_cleanup import _read_run_journal
from .runtime_types import RuntimeBackend, RuntimeKind
from .state import StatePaths, locked_existing_run, read_run_dispatch_record

OCI_EXEC_STATUS_SCHEMA = "palimpsest.oci-exec-status.v1"
_RUN_LOCK_TIMEOUT = 5.0

_GUIDANCE = {
    "not-ready": "Wait for authenticated READY.",
    "ready": "Ready and unoccupied is a point-in-time observation, not a guarantee the next exec will succeed.",
    "stopping": "Wait for shutdown and preserve existing client results.",
    "terminal": "Inspect the existing VM result; do not invent an exec result.",
    "control-lost": (
        "Preserve the run evidence and original-client output; do not rerun a command whose outcome is unknown."
    ),
}
_OCCUPIED_GUIDANCE = (
    "The original client may finish; active work and a retained unacknowledged result cannot be "
    "distinguished, takeover is unsupported, and occupancy does not prove abandonment. "
    "Do not rerun a command whose outcome is unknown."
)


class OCIExecStatusError(StateError):
    """Path-free refusal when an exact read-only status cannot be proven."""


def exec_status(roots: StatePaths, name: str) -> dict[str, object]:
    """Request only STATUS from the pinned monitor for one existing OCI run."""

    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise OCIExecStatusError("OCI exec status requires a POSIX Linux host")
    try:
        if type(roots) is not StatePaths:
            raise StateError("invalid state roots")
        record = read_run_dispatch_record(roots, name)
        if (
            record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
            or record.dispatch_key.backend is not RuntimeBackend.KVM
        ):
            raise StateError("wrong runtime")
        with locked_existing_run(roots, name, expected=record, lock_timeout=_RUN_LOCK_TIMEOUT) as mutation:
            snapshot = _read_run_journal(mutation)
            binding = snapshot.identity.binding
            if binding.record != record:
                raise StateError("run identity changed")
            endpoint = snapshot.endpoint
        with MonitorClient(roots, binding, endpoint) as client:
            status = client.exec_request("status", {})
    except OCIExecStatusError:
        raise
    except Exception:
        raise OCIExecStatusError("OCI exec status is unavailable; preserve the existing run evidence") from None

    try:
        validate_exec_status(status)
    except StateError:
        raise OCIExecStatusError("OCI exec status response is invalid; preserve the existing run evidence") from None

    guidance = _GUIDANCE[status["state"]]
    if status["occupied"] and status["state"] == "ready":
        guidance = _OCCUPIED_GUIDANCE
    elif status["occupied"]:
        guidance = f"{guidance} {_OCCUPIED_GUIDANCE}"
    return {
        "schema": OCI_EXEC_STATUS_SCHEMA,
        "state": status["state"],
        "occupied": status["occupied"],
        "guidance": guidance,
    }


__all__ = ["OCI_EXEC_STATUS_SCHEMA", "OCIExecStatusError", "exec_status"]
