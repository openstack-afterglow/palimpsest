"""Read-only public observation of one exact OCI-root run's network exposure.

The committed domain plan is the only authority: this module reports what
libvirt and QEMU were actually given, never a separately writable copy. A
missing, malformed, or non-matching plan is reported as a refusal rather than
as "no exposure".
"""

from __future__ import annotations

from typing import Any

from .errors import StateError
from .oci_root_kvm import load_oci_root_domain_plan
from .runtime_types import RuntimeBackend, RuntimeKind
from .state import StatePaths, read_run_dispatch_record

OCI_NETWORK_STATUS_SCHEMA = "palimpsest.oci-network-status.v1"


class OCINetworkStatusError(StateError):
    """Path-free refusal when exact network exposure cannot be proven."""


def network_status(roots: StatePaths, name: str) -> dict[str, Any]:
    """Project the durable network contract of one existing OCI-root run."""

    try:
        if type(roots) is not StatePaths:
            raise StateError("invalid state roots")
        record = read_run_dispatch_record(roots, name)
        if (
            record.dispatch_key.runtime_kind is not RuntimeKind.OCI_ROOT
            or record.dispatch_key.backend is not RuntimeBackend.KVM
        ):
            raise StateError("wrong runtime")
        plan = load_oci_root_domain_plan(roots, name)
        if plan.run_name != record.name or plan.run_id != record.run_id:
            raise StateError("run identity changed")
        network = plan.network
        mac = network.guest_mac_address(plan.run_id) if network.enabled else None
    except Exception:
        raise OCINetworkStatusError(
            "OCI network status is unavailable; the exact run has no committed domain plan or its plan is invalid"
        ) from None
    guest = network.guest_contract(plan.run_id)
    return {
        "schema": OCI_NETWORK_STATUS_SCHEMA,
        "run": {"name": plan.run_name, "run_id": plan.run_id},
        "plan_digest": plan.digest,
        "mode": network.mode,
        "backend": network.to_dict()["backend"],
        "subnet": network.to_dict()["subnet"],
        "egress": network.egress,
        "guest": {
            "address": guest["address"] or None,
            "gateway": guest["gateway"] or None,
            "interface": guest["interface"],
            "mac": mac,
            "nameservers": list(guest["nameservers"]),
            "netmask": guest["netmask"] or None,
        },
        "published_ports": [port.to_dict() for port in network.published_ports],
        "exposure": {
            "external": bool(network.external_ports),
            "external_ports": [port.to_dict() for port in network.external_ports],
            "endpoints": list(network.endpoints()),
        },
    }


__all__ = ["OCI_NETWORK_STATUS_SCHEMA", "OCINetworkStatusError", "network_status"]
