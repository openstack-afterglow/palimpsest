"""Access preflight run under the caller's own token before any resource is allocated.

Every check is read-only. A failed preflight returns a structured 4xx that names
each failed check, the OpenStack service involved and the operator action, and
callers MUST stop before creating a server, volume, port or trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

REQUIRED_SERVICES = ("compute", "network", "image", "volumev3")
SHARED_FILE_SERVICE = "shared-file-system"

_STATUS_BY_CHECK = {
    "token_scope": 403,
    "policy": 403,
    "catalog": 424,
    "quota": 409,
}


@dataclass(frozen=True, slots=True)
class PreflightNeed:
    """What the pending request is about to consume."""

    action: str = "packages:write"
    instances: int = 1
    volumes: int = 1
    volume_gib: int = 0
    shared_files: bool = False


def _failure(check: str, service: str, detail: str, action: str) -> dict[str, str]:
    return {"check": check, "service": service, "detail": detail, "action": action}


def _endpoint(conn: Any, service_type: str, interface: str | None) -> bool:
    try:
        return bool(conn.session.get_endpoint(service_type=service_type, interface=interface))
    except Exception:
        return False


def _left(limit: int | None, used: int | None) -> int | None:
    """Remaining headroom; a negative limit means unlimited in Nova and Cinder."""
    if limit is None or used is None:
        return None
    if limit < 0:
        return 1 << 62
    return limit - used


def _quota_failures(conn: Any, need: PreflightNeed) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    try:
        compute = conn.compute.get_limits().absolute
        room = _left(getattr(compute, "instances", None), getattr(compute, "instances_used", None))
        if room is not None and room < need.instances:
            failures.append(
                _failure(
                    "quota",
                    "compute",
                    f"instance quota leaves {room}, need {need.instances}",
                    "free an instance or ask the project admin to raise the compute quota",
                )
            )
    except Exception as exc:
        failures.append(
            _failure("quota", "compute", f"compute limits unreadable: {type(exc).__name__}", "check compute API access")
        )
    if need.volumes or need.volume_gib:
        try:
            block = conn.block_storage.get_limits().absolute
            count = _left(getattr(block, "max_total_volumes", None), getattr(block, "total_volumes_used", None))
            size = _left(
                getattr(block, "max_total_volume_gigabytes", None), getattr(block, "total_gigabytes_used", None)
            )
            if count is not None and count < need.volumes:
                failures.append(
                    _failure(
                        "quota",
                        "volumev3",
                        f"volume count quota leaves {count}, need {need.volumes}",
                        "delete an unused volume or raise the block storage quota",
                    )
                )
            if size is not None and size < need.volume_gib:
                failures.append(
                    _failure(
                        "quota",
                        "volumev3",
                        f"volume size quota leaves {size} GiB, need {need.volume_gib} GiB",
                        "delete an unused volume or raise the block storage gigabytes quota",
                    )
                )
        except Exception as exc:
            failures.append(
                _failure(
                    "quota",
                    "volumev3",
                    f"block storage limits unreadable: {type(exc).__name__}",
                    "check block storage API access",
                )
            )
    return failures


def run(conn: Any, actor: dict, need: PreflightNeed, *, interface: str | None = None) -> list[dict[str, str]]:
    """Return every failed check; an empty list means every check passed."""
    failures: list[dict[str, str]] = []
    project = str(actor.get("project_id") or "")
    if not project or str(getattr(conn, "_afterglow_project_id", "")) != project:
        failures.append(
            _failure(
                "token_scope",
                "identity",
                "the token is not scoped to the project of this request",
                "authenticate with a project-scoped token (system tokens also send X-Target-Project-Id)",
            )
        )
    if need.action not in tuple(actor.get("package_actions", ())):
        failures.append(
            _failure(
                "policy",
                "palimpsest",
                f"the caller's roles do not grant {need.action}",
                "ask a project admin for a role that grants it",
            )
        )
    services = REQUIRED_SERVICES + ((SHARED_FILE_SERVICE,) if need.shared_files else ())
    for service in services:
        if not _endpoint(conn, service, interface):
            failures.append(
                _failure(
                    "catalog",
                    service,
                    f"no {interface or 'default'} endpoint for service type {service} in the token's catalog",
                    "ask the cloud operator to register the endpoint or grant the role that exposes it",
                )
            )
    # Quotas are only meaningful once the services answer.
    if not any(item["check"] == "catalog" for item in failures):
        failures.extend(_quota_failures(conn, need))
    return failures


def require(conn: Any, actor: dict, need: PreflightNeed, *, interface: str | None = None) -> None:
    """Raise a structured 4xx before allocation when any preflight check fails."""
    failures = run(conn, actor, need, interface=interface)
    if not failures:
        return
    status = max(_STATUS_BY_CHECK[item["check"]] for item in failures)
    raise HTTPException(status_code=status, detail={"code": "preflight_failed", "failures": failures})
