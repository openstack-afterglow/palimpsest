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
    "resource": 422,
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
    flavor_id: str | None = None
    ports: int = 0


def _failure(check: str, service: str, detail: str, action: str) -> dict[str, str]:
    return {"check": check, "service": service, "detail": detail, "action": action}


def _endpoint(conn: Any, service_type: str, interface: str | None, region_name: str | None) -> bool:
    try:
        return bool(
            conn.session.get_endpoint(
                service_type=service_type,
                interface=interface,
                region_name=region_name,
            )
        )
    except Exception:
        return False


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _left(limit: Any, used: Any) -> int | None:
    """Remaining headroom; negative integer limits mean unlimited."""
    if type(limit) is not int or type(used) is not int or used < 0:
        return None
    if limit < 0:
        return 1 << 62
    return limit - used


def _check_quota(
    failures: list[dict[str, str]],
    *,
    service: str,
    label: str,
    limit: Any,
    used: Any,
    required: int,
    action: str,
) -> None:
    if required == 0:
        return
    room = _left(limit, used)
    if room is None:
        failures.append(_failure("quota", service, f"{label} quota unreadable", action))
    elif room < required:
        failures.append(_failure("quota", service, f"{label} quota leaves {room}, need {required}", action))


def _quota_failures(
    conn: Any,
    need: PreflightNeed,
    project: str,
    flavor_size: tuple[int, int] | None,
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    if need.instances or flavor_size is not None:
        try:
            compute = conn.compute.get_limits().absolute
            _check_quota(
                failures,
                service="compute",
                label="instance",
                limit=_field(compute, "instances"),
                used=_field(compute, "instances_used"),
                required=need.instances,
                action="free an instance or ask the project admin to raise the compute quota",
            )
            if flavor_size is not None:
                vcpus, ram = flavor_size
                _check_quota(
                    failures,
                    service="compute",
                    label="core",
                    limit=_field(compute, "total_cores"),
                    used=_field(compute, "total_cores_used"),
                    required=vcpus,
                    action="free compute cores or ask the project admin to raise the core quota",
                )
                _check_quota(
                    failures,
                    service="compute",
                    label="RAM",
                    limit=_field(compute, "total_ram"),
                    used=_field(compute, "total_ram_used"),
                    required=ram,
                    action="free compute RAM or ask the project admin to raise the RAM quota",
                )
        except Exception as exc:
            failures.append(
                _failure(
                    "quota", "compute", f"compute limits unreadable: {type(exc).__name__}", "check compute API access"
                )
            )
    if need.volumes or need.volume_gib:
        try:
            block = conn.block_storage.get_limits().absolute
            _check_quota(
                failures,
                service="volumev3",
                label="volume count",
                limit=_field(block, "max_total_volumes"),
                used=_field(block, "total_volumes_used"),
                required=need.volumes,
                action="delete an unused volume or raise the block storage quota",
            )
            _check_quota(
                failures,
                service="volumev3",
                label="volume size",
                limit=_field(block, "max_total_volume_gigabytes"),
                used=_field(block, "total_gigabytes_used"),
                required=need.volume_gib,
                action="delete an unused volume or raise the block storage gigabytes quota",
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
    if need.ports:
        try:
            quota = conn.network.get_quota(project, details=True)
            ports = quota.get("ports") if isinstance(quota, dict) else None
            if not isinstance(ports, dict):
                raise ValueError("ports quota missing")
            used = ports.get("used")
            reserved = ports.get("reserved")
            if type(used) is not int or type(reserved) is not int or used < 0 or reserved < 0:
                raise ValueError("ports usage unreadable")
            _check_quota(
                failures,
                service="network",
                label="port",
                limit=ports.get("limit"),
                used=used + reserved,
                required=need.ports,
                action="delete an unused port or ask the project admin to raise the network quota",
            )
        except Exception as exc:
            failures.append(
                _failure("quota", "network", f"port quota unreadable: {type(exc).__name__}", "check network API access")
            )
    return failures


def run(
    conn: Any,
    actor: dict,
    need: PreflightNeed,
    *,
    interface: str | None = None,
    region_name: str | None = None,
) -> list[dict[str, str]]:
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
        if not _endpoint(conn, service, interface, region_name):
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
        flavor_size = None
        if need.flavor_id is not None:
            try:
                flavor = conn.compute.get_flavor(need.flavor_id)
                vcpus, ram = _field(flavor, "vcpus"), _field(flavor, "ram")
                if type(vcpus) is not int or vcpus <= 0 or type(ram) is not int or ram <= 0:
                    raise ValueError("flavor sizing missing")
                flavor_size = (vcpus, ram)
            except Exception as exc:
                failures.append(
                    _failure(
                        "resource",
                        "compute",
                        f"requested flavor unavailable or unreadable: {type(exc).__name__}",
                        "select an available flavor with readable vCPU and RAM sizing",
                    )
                )
        failures.extend(_quota_failures(conn, need, project, flavor_size))
    return failures


def require(
    conn: Any,
    actor: dict,
    need: PreflightNeed,
    *,
    interface: str | None = None,
    region_name: str | None = None,
) -> None:
    """Raise a structured 4xx before allocation when any preflight check fails."""
    failures = run(conn, actor, need, interface=interface, region_name=region_name)
    if not failures:
        return
    status = max(_STATUS_BY_CHECK[item["check"]] for item in failures)
    raise HTTPException(status_code=status, detail={"code": "preflight_failed", "failures": failures})
