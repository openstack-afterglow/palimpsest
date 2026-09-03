"""Production-inert libvirt definition boundary for committed OCI-root runs.

This module intentionally defines but never starts a domain.  Public runtime
dispatch remains disabled until the privileged lifecycle handshake is ready.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from . import kvm
from .errors import StateError
from .oci_root_kvm import (
    ResolvedOCIRootDomainPlan,
    VerifiedHostBootArtifacts,
    resolve_committed_oci_root_domain_plan,
)
from .oci_store import OCIStore
from .platforms import DomainProfile
from .project_volumes import CommandRunner, _default_runner
from .state import StatePaths, locked_existing_run


@dataclass(frozen=True, slots=True)
class DefinedOCIRootDomain:
    """Path-free receipt for one durable, inactive domain definition."""

    run_id: str
    run_name: str
    plan_digest: str


def _single(parent: ET.Element, path: str, message: str) -> ET.Element:
    found = parent.findall(path)
    if len(found) != 1:
        raise StateError(message)
    return found[0]


def _text(parent: ET.Element, path: str, message: str) -> str:
    value = _single(parent, path, message).text
    if not isinstance(value, str):
        raise StateError(message)
    return value


def _disk_projection(root: ET.Element) -> tuple[tuple[Any, ...], ...]:
    projected: list[tuple[Any, ...]] = []
    seen_targets: set[str] = set()
    for disk in root.findall("./devices/disk"):
        source = _single(disk, "./source", "defined OCI-root disk source is invalid")
        target = _single(disk, "./target", "defined OCI-root disk target is invalid")
        driver = _single(disk, "./driver", "defined OCI-root disk driver is invalid")
        serials = disk.findall("./serial")
        readonly_count = len(disk.findall("./readonly"))
        shareable_count = len(disk.findall("./shareable"))
        target_name = target.get("dev")
        if (
            disk.attrib != {"type": "file", "device": "disk"}
            or set(source.attrib) != {"file"}
            or target.attrib.get("bus") != "virtio"
            or set(target.attrib) != {"dev", "bus"}
            or driver.get("name") != "qemu"
            or driver.get("type") != "raw"
            or not isinstance(target_name, str)
            or target_name in seen_targets
            or len(serials) != 1
            or not isinstance(serials[0].text, str)
            or readonly_count > 1
            or shareable_count > 1
        ):
            raise StateError("defined OCI-root disk projection is invalid")
        seen_targets.add(target_name)
        projected.append(
            (
                target_name,
                source.get("file"),
                serials[0].text,
                tuple(sorted(driver.attrib.items())),
                readonly_count == 1,
                shareable_count == 1,
            )
        )
    return tuple(sorted(projected))


def _domain_projection(xml: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError, ValueError):
        raise StateError("defined OCI-root domain XML is invalid") from None
    marker = _single(
        root,
        f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run",
        "defined OCI-root ownership marker is invalid",
    )
    lifecycle = _single(
        root,
        f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}lifecycle",
        "defined OCI-root lifecycle contract is invalid",
    )
    channels = root.findall("./devices/channel")
    controllers = [
        (controller.get("type"), controller.get("index"))
        for controller in root.findall("./devices/controller")
        if controller.get("type") == "virtio-serial"
    ]
    interfaces = root.findall("./devices/interface")
    network_projection: list[tuple[str | None, str | None, str | None]] = []
    for interface in interfaces:
        source = _single(interface, "./source", "defined OCI-root network source is invalid")
        model = _single(interface, "./model", "defined OCI-root network model is invalid")
        network_projection.append((interface.get("type"), source.get("network"), model.get("type")))
    channel_projection: list[tuple[str | None, str | None, str | None]] = []
    for channel in channels:
        target = _single(channel, "./target", "defined OCI-root lifecycle channel is invalid")
        channel_projection.append((channel.get("type"), target.get("type"), target.get("name")))
    os_type = _single(root, "./os/type", "defined OCI-root machine contract is invalid")
    memory = _single(root, "./memory", "defined OCI-root memory contract is invalid")
    return {
        "channels": tuple(channel_projection),
        "controllers": tuple(controllers),
        "disks": _disk_projection(root),
        "domain_type": root.get("type"),
        "emulator": _text(root, "./devices/emulator", "defined OCI-root emulator is invalid"),
        "initramfs": _text(root, "./os/initrd", "defined OCI-root initramfs is invalid"),
        "interfaces": tuple(network_projection),
        "kernel": _text(root, "./os/kernel", "defined OCI-root kernel is invalid"),
        "kernel_cmdline": _text(root, "./os/cmdline", "defined OCI-root kernel command line is invalid"),
        "lifecycle": dict(lifecycle.attrib),
        "machine": (os_type.get("arch"), os_type.get("machine"), os_type.text),
        "marker": dict(marker.attrib),
        "memory": (memory.get("unit"), memory.text),
        "name": _text(root, "./name", "defined OCI-root domain name is invalid"),
        "vcpus": _text(root, "./vcpu", "defined OCI-root vCPU contract is invalid"),
    }


def _validate_defined_domain(domain: Any, resolved: ResolvedOCIRootDomainPlan) -> None:
    try:
        actual_xml = domain.XMLDesc()
    except Exception as exc:
        raise StateError("defined OCI-root domain cannot be inspected") from exc
    if _domain_projection(actual_xml) != _domain_projection(resolved.xml):
        raise StateError("defined OCI-root domain does not match the committed contract")
    try:
        active = domain.isActive()
    except Exception as exc:
        raise StateError("defined OCI-root domain activity cannot be inspected") from exc
    if active != 0:
        raise StateError("defined OCI-root domain became active before start authorization")


def _lookup(conn: Any, name: str) -> Any | None:
    libvirt = kvm._libvirt()
    try:
        domain = conn.lookupByName(name)
    except libvirt.libvirtError as exc:
        code = exc.get_error_code() if hasattr(exc, "get_error_code") else None
        if code == libvirt.VIR_ERR_NO_DOMAIN:
            return None
        raise StateError(f"cannot determine whether OCI-root domain name is available: {name}") from exc
    except Exception as exc:
        raise StateError(f"cannot determine whether OCI-root domain name is available: {name}") from exc
    if domain is None:
        raise StateError(f"libvirt returned an ambiguous domain lookup result: {name}")
    return domain


def _has_exact_owner(domain: Any, resolved: ResolvedOCIRootDomainPlan) -> bool:
    try:
        root = ET.fromstring(domain.XMLDesc())
        marker = _single(
            root,
            f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run",
            "defined OCI-root ownership marker is invalid",
        )
    except Exception:
        return False
    return marker.attrib == {
        "contract": resolved.plan.digest,
        "id": resolved.plan.run_id,
        "schema": "1",
        "version": kvm.DOMAIN_MARKER_VERSION,
    }


def _cleanup_exact_new_domain(conn: Any, resolved: ResolvedOCIRootDomainPlan) -> None:
    try:
        domain = _lookup(conn, resolved.plan.run_name)
        if domain is None or not _has_exact_owner(domain, resolved):
            return
        if domain.isActive() != 0:
            return
        domain.undefine()
    except Exception:
        return


def define_committed_oci_root_domain(
    roots: StatePaths,
    name: str,
    store: OCIStore,
    boot_artifacts: VerifiedHostBootArtifacts,
    profile: DomainProfile,
    *,
    conn: Any,
    runner: CommandRunner = _default_runner,
) -> DefinedOCIRootDomain:
    """Define, validate, and durably record one inactive OCI-root domain.

    This is intentionally not registered with runtime dispatch and never calls
    ``create``.  Every path-bearing authority is reconstructed while the pinned
    run lock is held, immediately before ``defineXML``.
    """

    if conn is None:
        raise StateError("OCI-root domain definition requires an explicit libvirt connection")
    with locked_existing_run(roots, name) as mutation:
        resolved = resolve_committed_oci_root_domain_plan(
            roots,
            mutation.snapshot,
            store,
            boot_artifacts,
            profile,
            runner=runner,
        )
        if _lookup(conn, name) is not None:
            raise StateError(f"libvirt domain name is already reserved: {name}")
        attempted = False
        try:
            mutation.verify_binding()
            attempted = True
            domain = conn.defineXML(resolved.xml)
            if domain is None:
                raise StateError("OCI-root domain definition failed")
            current = _lookup(conn, name)
            if current is None:
                raise StateError("defined OCI-root domain is missing")
            _validate_defined_domain(current, resolved)
            mutation.verify_binding()
            data = mutation.mutable_state()
            result = mutation.write_state("defined", data)
            if result.get("status") != "defined":
                raise StateError("OCI-root domain definition was not durably recorded")
        except BaseException:
            if attempted:
                _cleanup_exact_new_domain(conn, resolved)
            raise
        return DefinedOCIRootDomain(resolved.plan.run_id, resolved.plan.run_name, resolved.plan.digest)


__all__ = ["DefinedOCIRootDomain", "define_committed_oci_root_domain"]
