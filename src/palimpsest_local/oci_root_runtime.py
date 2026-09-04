"""Production-inert libvirt definition boundary for committed OCI-root runs.

This module intentionally defines but never starts a domain.  Public runtime
dispatch remains disabled until the privileged lifecycle handshake is ready.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import kvm
from .errors import StateError
from .oci_control_protocol_v2 import (
    OCI_CONTROL_CHANNEL_NAME,
    HostOCIControlV2Session,
    OCIControlV2Binding,
)
from .oci_lifecycle_transport import (
    DEFAULT_HANDOFF_TIMEOUT_SECONDS,
    OCI_ROOT_HANDOFF_SCHEMA,
    OCILifecycleHandoffReceipt,
    complete_initial_lifecycle_handoff,
)
from .oci_root_kvm import (
    ResolvedOCIRootDomainPlan,
    VerifiedHostBootArtifacts,
    resolve_committed_oci_root_domain_plan,
)
from .oci_store import OCIStore
from .platforms import DomainProfile
from .project_volumes import CommandRunner, _default_runner
from .runtime_types import ProcessExit
from .state import StatePaths, locked_existing_run

OCI_ROOT_DEFINITION_SCHEMA = "palimpsest.oci-root-definition.v1"
_MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


@dataclass(frozen=True, slots=True)
class DefinedOCIRootDomain:
    """Path-free receipt for one durable, inactive domain definition."""

    run_id: str
    run_name: str
    plan_digest: str
    domain_uuid: str
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class CompletedOCIRootHandoff:
    """Private result for one domain boot observed through TERMINAL."""

    run_id: str
    run_name: str
    plan_digest: str
    domain_uuid: str
    domain_id: int
    libvirt_uri: str
    terminal: ProcessExit
    lifecycle: OCILifecycleHandoffReceipt


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
        backing_stores = disk.findall("./backingStore")
        if any(
            child.tag
            not in {"address", "alias", "backingStore", "driver", "readonly", "serial", "shareable", "source", "target"}
            for child in disk
        ):
            raise StateError("defined OCI-root disk contains an unapproved child")
        if len(backing_stores) > 1 or any(
            backing.attrib or list(backing) or (backing.text or "").strip() for backing in backing_stores
        ):
            raise StateError("defined OCI-root disk backing store is invalid")
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
            or list(source)
            or list(target)
            or list(driver)
            or serials[0].attrib
            or list(serials[0])
            or len(disk.findall("./alias")) > 1
            or len(disk.findall("./address")) > 1
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


def _validate_devices_surface(devices: ET.Element) -> tuple[tuple[str, int], ...]:
    """Reject host-resource devices; admit only bounded inert normalization.

    Libvirt may synthesize bus controllers, a PTY serial peer, legacy input
    devices, a panic notifier, or a virtio balloon in inactive XML.  Those
    classes have no host path/source selector and are bounded here.  Every
    authored OCI-root device remains part of the exact projection below.
    """

    authored = {"channel", "console", "disk", "emulator", "interface"}
    safe_generated = {"input", "memballoon", "panic", "serial"}
    counts: dict[str, int] = {}
    generated_counts: dict[str, int] = {}
    safe_controller_ids: set[tuple[str, str]] = set()
    for child in list(devices):
        if not isinstance(child.tag, str) or "}" in child.tag:
            raise StateError("defined OCI-root device class is invalid")
        tag = child.tag
        if tag in authored:
            counts[tag] = counts.get(tag, 0) + 1
            continue
        if tag == "controller":
            controller_type = child.get("type")
            if controller_type == "virtio-serial":
                counts[tag] = counts.get(tag, 0) + 1
                continue
            if (
                controller_type not in {"pci", "sata", "usb"}
                or set(child.attrib) - {"index", "model", "ports", "type"}
                or any(grandchild.tag not in {"address", "alias", "driver", "model", "target"} for grandchild in child)
                or child.find(".//source") is not None
            ):
                raise StateError("defined OCI-root generated controller is invalid")
            identity = (controller_type, child.get("index", ""))
            if identity in safe_controller_ids or len(safe_controller_ids) >= 32:
                raise StateError("defined OCI-root generated controller set is invalid")
            safe_controller_ids.add(identity)
            continue
        if tag not in safe_generated:
            raise StateError(f"defined OCI-root device class is forbidden: {tag}")
        generated_counts[tag] = generated_counts.get(tag, 0) + 1
        limits = {"input": 2, "memballoon": 1, "panic": 1, "serial": 1}
        if generated_counts[tag] > limits[tag] or child.find(".//source") is not None:
            raise StateError("defined OCI-root generated device set is invalid")
        if tag == "input" and (
            child.get("type") not in {"keyboard", "mouse", "tablet"}
            or child.get("bus") not in {"ps2", "usb"}
            or any(grandchild.tag not in {"address", "alias"} for grandchild in child)
        ):
            raise StateError("defined OCI-root generated input device is invalid")
        if tag == "memballoon" and (
            child.get("model") not in {"none", "virtio"}
            or any(grandchild.tag not in {"address", "alias", "driver", "stats"} for grandchild in child)
        ):
            raise StateError("defined OCI-root generated balloon device is invalid")
        if tag == "panic" and (
            child.get("model") not in {"hyperv", "isa", "pseries", "s390"}
            or any(grandchild.tag not in {"address", "alias"} for grandchild in child)
        ):
            raise StateError("defined OCI-root generated panic device is invalid")
        if tag == "serial":
            targets = child.findall("./target")
            if (
                child.attrib != {"type": "pty"}
                or len(targets) != 1
                or targets[0].get("port") != "0"
                or targets[0].get("type") not in {"isa-serial", "serial"}
                or any(grandchild.tag not in {"address", "alias", "target"} for grandchild in child)
            ):
                raise StateError("defined OCI-root generated serial device is invalid")
    required = {"channel": 1, "console": 1, "emulator": 1}
    if any(counts.get(tag, 0) != count for tag, count in required.items()):
        raise StateError("defined OCI-root authored device multiplicity is invalid")
    if counts.get("disk", 0) < 3 or counts.get("controller", 0) != 1 or counts.get("interface", 0) > 1:
        raise StateError("defined OCI-root authored device multiplicity is invalid")
    return tuple(sorted(counts.items()))


def _validate_top_level_surface(root: ET.Element) -> None:
    authored = {"cpu", "devices", "features", "memory", "metadata", "name", "os", "vcpu"}
    safe_defaults = {"clock", "currentMemory", "on_crash", "on_poweroff", "on_reboot", "pm", "uuid"}
    counts: dict[str, int] = {}
    for child in list(root):
        if not isinstance(child.tag, str) or "}" in child.tag:
            raise StateError("defined OCI-root top-level extension is forbidden")
        if child.tag in authored:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            continue
        if child.tag not in safe_defaults:
            raise StateError(f"defined OCI-root top-level element is forbidden: {child.tag}")
        counts[child.tag] = counts.get(child.tag, 0) + 1
        if counts[child.tag] > 1:
            raise StateError("defined OCI-root generated top-level defaults are invalid")
        if child.tag == "uuid":
            try:
                value = str(uuid.UUID(child.text or ""))
            except ValueError:
                raise StateError("defined OCI-root generated UUID element is invalid") from None
            if value != child.text or child.attrib or list(child):
                raise StateError("defined OCI-root generated UUID element is invalid")
        elif child.tag == "clock":
            if child.attrib != {"offset": "utc"} or any(grandchild.tag != "timer" for grandchild in child):
                raise StateError("defined OCI-root generated clock is invalid")
        elif child.tag == "currentMemory":
            # libvirt may materialize this redundant field in inactive XML.  Its
            # value is checked against memory after authored multiplicity has
            # been established below.
            pass
        elif child.tag in {"on_crash", "on_poweroff", "on_reboot"}:
            expected = {"on_crash": "destroy", "on_poweroff": "destroy", "on_reboot": "restart"}[child.tag]
            if child.text != expected or child.attrib or list(child):
                raise StateError("defined OCI-root generated lifecycle default is invalid")
        elif child.tag == "pm":
            expected_pm = {"suspend-to-disk": "no", "suspend-to-mem": "no"}
            found_pm: dict[str, str | None] = {}
            for setting in child:
                if setting.tag not in expected_pm or set(setting.attrib) != {"enabled"} or list(setting):
                    raise StateError("defined OCI-root generated power-management default is invalid")
                found_pm[setting.tag] = setting.get("enabled")
            if child.attrib or found_pm != expected_pm:
                raise StateError("defined OCI-root generated power-management default is invalid")
    if any(counts.get(tag, 0) != 1 for tag in authored):
        raise StateError("defined OCI-root authored top-level multiplicity is invalid")
    _memory_projection(root)


def _memory_projection(root: ET.Element) -> tuple[str, int]:
    memory = _single(root, "./memory", "defined OCI-root memory contract is invalid")
    if (
        set(memory.attrib) != {"unit"}
        or memory.get("unit") not in {"KiB", "MiB"}
        or re.fullmatch(r"[1-9][0-9]*", memory.text or "") is None
        or list(memory)
    ):
        raise StateError("defined OCI-root memory contract is invalid")
    current = root.findall("./currentMemory")
    if current and (
        len(current) != 1 or current[0].attrib != memory.attrib or current[0].text != memory.text or list(current[0])
    ):
        raise StateError("defined OCI-root generated current memory is invalid")
    multiplier = 1 if memory.get("unit") == "KiB" else 1024
    return ("KiB", int(memory.text or "0") * multiplier)


def _domain_projection(xml: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError, ValueError):
        raise StateError("defined OCI-root domain XML is invalid") from None
    if root.tag != "domain" or root.attrib != {"type": "kvm"}:
        raise StateError("defined OCI-root domain root is invalid")
    _validate_top_level_surface(root)
    metadata = _single(root, "./metadata", "defined OCI-root metadata contract is invalid")
    if (
        metadata.attrib
        or (metadata.text is not None and metadata.text.strip())
        or (metadata.tail is not None and metadata.tail.strip())
        or any(child.tail is not None and child.tail.strip() for child in metadata)
    ):
        raise StateError("defined OCI-root metadata contract is invalid")
    marker_tag = f"{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run"
    lifecycle_tag = f"{{{kvm.DOMAIN_MARKER_NAMESPACE}}}lifecycle"
    metadata_tags = [child.tag for child in metadata]
    if (
        any(not isinstance(tag, str) or tag not in {lifecycle_tag, marker_tag} for tag in metadata_tags)
        or metadata_tags.count(marker_tag) != 1
        or metadata_tags.count(lifecycle_tag) > 1
    ):
        raise StateError("defined OCI-root metadata contract is invalid")
    marker = _single(
        root,
        f"./metadata/{marker_tag}",
        "defined OCI-root ownership marker is invalid",
    )
    if set(marker.attrib) != {"contract", "id", "schema", "version"} or marker.text is not None or list(marker):
        raise StateError("defined OCI-root ownership marker is invalid")
    lifecycle_nodes = metadata.findall(f"./{lifecycle_tag}")
    lifecycle_projection = {
        "channel": kvm.OCI_CONTROL_CHANNEL_NAME,
        "protocol": kvm.OCI_CONTROL_PROTOCOL_V2,
    }
    # libvirt retains only the ownership child when two application metadata
    # children share this namespace.  Absence is normalized to the fixed
    # lifecycle contract; the run contract digest and exact channel projection
    # below still bind the per-run source path and guest target.
    if lifecycle_nodes:
        lifecycle = lifecycle_nodes[0]
        if lifecycle.attrib != lifecycle_projection or lifecycle.text is not None or list(lifecycle):
            raise StateError("defined OCI-root lifecycle contract is invalid")
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
        macs = interface.findall("./mac")
        if (
            any(child.tag not in {"address", "alias", "mac", "model", "source"} for child in interface)
            or interface.attrib != {"type": "network"}
            or set(source.attrib) != {"network"}
            or set(model.attrib) != {"type"}
            or list(source)
            or list(model)
            or len(macs) > 1
            or len(interface.findall("./alias")) > 1
            or len(interface.findall("./address")) > 1
            or (macs and (set(macs[0].attrib) != {"address"} or _MAC_RE.fullmatch(macs[0].get("address", "")) is None))
        ):
            raise StateError("defined OCI-root network projection is invalid")
        network_projection.append((interface.get("type"), source.get("network"), model.get("type")))
    channel_projection: list[tuple[str | None, ...]] = []
    for channel in channels:
        source = _single(channel, "./source", "defined OCI-root lifecycle channel source is invalid")
        target = _single(channel, "./target", "defined OCI-root lifecycle channel is invalid")
        if (
            any(child.tag not in {"address", "alias", "source", "target"} for child in channel)
            or channel.attrib != {"type": "unix"}
            or set(source.attrib) != {"mode", "path"}
            or set(target.attrib) != {"type", "name"}
            or list(source)
            or list(target)
            or len(channel.findall("./alias")) > 1
            or len(channel.findall("./address")) > 1
        ):
            raise StateError("defined OCI-root lifecycle channel source is invalid")
        channel_projection.append(
            (
                channel.get("type"),
                source.get("mode"),
                source.get("path"),
                target.get("type"),
                target.get("name"),
            )
        )
    os_type = _single(root, "./os/type", "defined OCI-root machine contract is invalid")
    os_element = _single(root, "./os", "defined OCI-root direct-boot contract is invalid")
    expected_os_children = {"cmdline", "initrd", "kernel", "type"}
    if (
        os_element.attrib
        or len(os_element) != 4
        or {child.tag for child in os_element} != expected_os_children
        or any(len(os_element.findall(f"./{tag}")) != 1 for tag in expected_os_children)
    ):
        raise StateError("defined OCI-root direct-boot contract is invalid")
    memory = _memory_projection(root)
    cpu = _single(root, "./cpu", "defined OCI-root CPU contract is invalid")
    features = _single(root, "./features", "defined OCI-root feature contract is invalid")
    consoles = root.findall("./devices/console")
    if len(consoles) != 1:
        raise StateError("defined OCI-root console contract is invalid")
    console = consoles[0]
    console_targets = console.findall("./target")
    console_sources = console.findall("./source")
    if (
        len(console_targets) != 1
        or len(console_sources) > 1
        or any(child.tag not in {"address", "alias", "source", "target"} for child in console)
        or list(console_targets[0])
        or any(list(source) for source in console_sources)
        or len(console.findall("./alias")) > 1
        or len(console.findall("./address")) > 1
    ):
        raise StateError("defined OCI-root console contract is invalid")
    emulator = _single(root, "./devices/emulator", "defined OCI-root emulator is invalid")
    if emulator.attrib or list(emulator):
        raise StateError("defined OCI-root emulator is invalid")
    return {
        "channels": tuple(channel_projection),
        "controllers": tuple(controllers),
        "disks": _disk_projection(root),
        "device_counts": _validate_devices_surface(_single(root, "./devices", "defined OCI-root devices are invalid")),
        "domain_type": root.get("type"),
        "emulator": emulator.text,
        "features": tuple((child.tag, tuple(sorted(child.attrib.items())), child.text) for child in list(features)),
        "initramfs": _text(root, "./os/initrd", "defined OCI-root initramfs is invalid"),
        "interfaces": tuple(network_projection),
        "kernel": _text(root, "./os/kernel", "defined OCI-root kernel is invalid"),
        "kernel_cmdline": _text(root, "./os/cmdline", "defined OCI-root kernel command line is invalid"),
        "lifecycle": lifecycle_projection,
        "machine": (os_type.get("arch"), os_type.get("machine"), os_type.text),
        "marker": dict(marker.attrib),
        "current_memory": memory,
        "memory": memory,
        "name": _text(root, "./name", "defined OCI-root domain name is invalid"),
        "cpu": (
            tuple(sorted(cpu.attrib.items())),
            tuple((child.tag, tuple(sorted(child.attrib.items())), child.text) for child in list(cpu)),
        ),
        "console": (
            tuple(sorted(console.attrib.items())),
            None if not console_sources else tuple(sorted(console_sources[0].attrib.items())),
            tuple(sorted(console_targets[0].attrib.items())),
        ),
        "vcpus": _text(root, "./vcpu", "defined OCI-root vCPU contract is invalid"),
    }


def _domain_uuid(domain: Any) -> str:
    try:
        value = domain.UUIDString()
        parsed = str(uuid.UUID(value))
    except Exception as exc:
        raise StateError("defined OCI-root domain UUID is invalid") from exc
    if parsed != value:
        raise StateError("defined OCI-root domain UUID is not canonical")
    return value


def _domain_id(domain: Any) -> int:
    try:
        value = domain.ID()
    except Exception as exc:
        raise StateError("OCI-root domain boot instance ID cannot be inspected") from exc
    if type(value) is not int or value < -1:
        raise StateError("OCI-root domain boot instance ID is invalid")
    return value


def _validate_defined_domain(
    domain: Any,
    resolved: ResolvedOCIRootDomainPlan,
    expected_uuid: str,
) -> None:
    if _domain_uuid(domain) != expected_uuid:
        raise StateError("defined OCI-root domain UUID changed after definition")
    try:
        actual_xml = domain.XMLDesc()
    except Exception as exc:
        raise StateError("defined OCI-root domain cannot be inspected") from exc
    if _domain_projection(actual_xml) != _domain_projection(resolved.xml):
        raise StateError("defined OCI-root domain does not match the committed contract")
    actual_root = ET.fromstring(actual_xml)
    xml_uuids = actual_root.findall("./uuid")
    if xml_uuids and xml_uuids[0].text != expected_uuid:
        raise StateError("defined OCI-root XML UUID does not match the libvirt domain UUID")
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


def _cleanup_exact_new_domain(
    conn: Any,
    resolved: ResolvedOCIRootDomainPlan,
    expected_uuid: str | None,
) -> None:
    domain = _lookup(conn, resolved.plan.run_name)
    if domain is None:
        return
    if expected_uuid is None:
        raise StateError("partially defined OCI-root domain has no captured UUID")
    if _domain_uuid(domain) != expected_uuid or not _has_exact_owner(domain, resolved):
        raise StateError("defined OCI-root domain identity changed before cleanup")
    try:
        active = domain.isActive()
    except Exception as exc:
        raise StateError("defined OCI-root domain activity is ambiguous during cleanup") from exc
    if active != 0:
        raise StateError("defined OCI-root domain is unexpectedly active during cleanup")
    try:
        domain.undefine()
    except Exception as exc:
        raise StateError("defined OCI-root domain cleanup failed") from exc
    if _lookup(conn, resolved.plan.run_name) is not None:
        raise StateError("defined OCI-root domain remains after cleanup")


def _connection_uri(conn: Any, profile: DomainProfile) -> str:
    try:
        uri = conn.getURI()
    except Exception as exc:
        raise StateError("OCI-root libvirt connection URI cannot be inspected") from exc
    if not isinstance(uri, str) or uri != profile.uri:
        raise StateError("OCI-root libvirt connection URI does not match the qualified profile")
    return uri


def _record_cleanup_required(
    mutation: Any,
    resolved: ResolvedOCIRootDomainPlan,
    domain_uuid: str | None,
    libvirt_uri: str,
) -> None:
    data = mutation.mutable_state()
    data["error"] = "OCI-root domain definition failed and cleanup is required"
    data["oci_root_definition"] = {
        "domain_uuid": domain_uuid,
        "libvirt_uri": libvirt_uri,
        "phase": "cleanup-required",
        "plan_digest": resolved.plan.digest,
        "schema": OCI_ROOT_DEFINITION_SCHEMA,
    }
    mutation.write_state("failed", data)


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
        libvirt_uri = _connection_uri(conn, profile)
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
        domain_uuid: str | None = None
        try:
            mutation.verify_binding()
            attempted = True
            domain = conn.defineXML(resolved.xml)
            if domain is None:
                raise StateError("OCI-root domain definition failed")
            domain_uuid = _domain_uuid(domain)
            current = _lookup(conn, name)
            if current is None:
                raise StateError("defined OCI-root domain is missing")
            _validate_defined_domain(current, resolved, domain_uuid)
            mutation.verify_binding()
            data = mutation.mutable_state()
            data["oci_root_definition"] = {
                "domain_uuid": domain_uuid,
                "libvirt_uri": libvirt_uri,
                "phase": "defined",
                "plan_digest": resolved.plan.digest,
                "schema": OCI_ROOT_DEFINITION_SCHEMA,
            }
            result = mutation.write_state("defined", data)
            if result.get("status") != "defined":
                raise StateError("OCI-root domain definition was not durably recorded")
        except BaseException:
            if attempted:
                try:
                    _cleanup_exact_new_domain(conn, resolved, domain_uuid)
                except Exception as cleanup_exc:
                    try:
                        _record_cleanup_required(mutation, resolved, domain_uuid, libvirt_uri)
                    except Exception as ledger_exc:
                        raise StateError(
                            "OCI-root domain cleanup failed and cleanup-required state could not be recorded"
                        ) from ledger_exc
                    raise StateError("OCI-root domain cleanup failed; cleanup is required") from cleanup_exc
            raise
        return DefinedOCIRootDomain(
            resolved.plan.run_id,
            resolved.plan.run_name,
            resolved.plan.digest,
            domain_uuid,
            libvirt_uri,
        )


def _definition_ledger(state: Mapping[str, Any], profile: DomainProfile) -> tuple[str, str]:
    value = state.get("oci_root_definition")
    if not isinstance(value, Mapping) or set(value) != {
        "domain_uuid",
        "libvirt_uri",
        "phase",
        "plan_digest",
        "schema",
    }:
        raise StateError("OCI-root durable definition ledger is invalid")
    if value.get("schema") != OCI_ROOT_DEFINITION_SCHEMA or value.get("phase") != "defined":
        raise StateError("OCI-root durable definition ledger is invalid")
    domain_uuid = value.get("domain_uuid")
    plan_digest = value.get("plan_digest")
    if not isinstance(domain_uuid, str) or not isinstance(plan_digest, str):
        raise StateError("OCI-root durable definition ledger is invalid")
    try:
        if str(uuid.UUID(domain_uuid)) != domain_uuid:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise StateError("OCI-root durable definition ledger is invalid") from None
    if value.get("libvirt_uri") != profile.uri:
        raise StateError("OCI-root durable definition URI is invalid")
    return domain_uuid, plan_digest


def _lookup_uuid(conn: Any, domain_uuid: str) -> Any | None:
    libvirt = kvm._libvirt()
    try:
        domain = conn.lookupByUUIDString(domain_uuid)
    except libvirt.libvirtError as exc:
        code = exc.get_error_code() if hasattr(exc, "get_error_code") else None
        if code == libvirt.VIR_ERR_NO_DOMAIN:
            return None
        raise StateError("cannot determine whether the OCI-root domain UUID exists") from exc
    except Exception as exc:
        raise StateError("cannot determine whether the OCI-root domain UUID exists") from exc
    if domain is None:
        raise StateError("libvirt returned an ambiguous domain UUID lookup result")
    return domain


def _exact_domain(conn: Any, resolved: ResolvedOCIRootDomainPlan, expected_uuid: str) -> Any:
    by_name = _lookup(conn, resolved.plan.run_name)
    by_uuid = _lookup_uuid(conn, expected_uuid)
    if by_name is None or by_uuid is None:
        raise StateError("defined OCI-root domain identity is missing")
    if _domain_uuid(by_name) != expected_uuid or _domain_uuid(by_uuid) != expected_uuid:
        raise StateError("defined OCI-root domain name and UUID do not agree")
    if not _has_exact_owner(by_name, resolved) or not _has_exact_owner(by_uuid, resolved):
        raise StateError("defined OCI-root domain ownership is invalid")
    return by_name


def _validate_active_domain(
    domain: Any,
    resolved: ResolvedOCIRootDomainPlan,
    expected_uuid: str,
    expected_domain_id: int,
) -> None:
    if _domain_uuid(domain) != expected_uuid:
        raise StateError("active OCI-root domain UUID changed")
    try:
        inactive_flag = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
        if type(inactive_flag) is not int:
            raise StateError("libvirt inactive XML inspection support is unavailable")
        actual_xml = domain.XMLDesc(inactive_flag)
    except Exception as exc:
        raise StateError("active OCI-root domain cannot be inspected") from exc
    if _domain_projection(actual_xml) != _domain_projection(resolved.xml):
        raise StateError("active OCI-root domain does not match the committed contract")
    try:
        active = domain.isActive()
    except Exception as exc:
        raise StateError("active OCI-root domain activity cannot be inspected") from exc
    if active != 1:
        raise StateError("OCI-root domain did not become active")
    if _domain_id(domain) != expected_domain_id:
        raise StateError("active OCI-root domain boot instance changed")


def _handoff_ledger(
    resolved: ResolvedOCIRootDomainPlan,
    domain_uuid: str,
    domain_id: int | None,
    libvirt_uri: str,
    boot_attempt_id: str,
    phase: str,
    lifecycle: OCILifecycleHandoffReceipt | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "domain_uuid": domain_uuid,
        "boot_attempt_id": boot_attempt_id,
        "libvirt_uri": libvirt_uri,
        "phase": phase,
        "plan_digest": resolved.plan.digest,
        "schema": OCI_ROOT_HANDOFF_SCHEMA,
    }
    if phase == "activating":
        if domain_id is not None or lifecycle is not None:
            raise StateError("OCI-root activation ledger inputs are invalid")
    elif type(domain_id) is int and domain_id > 0:
        value["domain_id"] = domain_id
    else:
        raise StateError("OCI-root handoff domain ID is invalid")
    if lifecycle is not None:
        value["lifecycle"] = lifecycle.to_dict()
    return value


def _plain_wire_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_wire_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_wire_value(item) for item in value]
    return value


def _require_expected_handoff(
    state: Mapping[str, Any],
    resolved: ResolvedOCIRootDomainPlan,
    domain_uuid: str,
    domain_id: int | None,
    libvirt_uri: str,
    boot_attempt_id: str,
    phase: str,
    lifecycle: OCILifecycleHandoffReceipt | None = None,
) -> None:
    expected_status = {"activating": "starting", "starting": "starting", "ready": "running"}.get(phase)
    if expected_status is None:
        raise StateError("OCI-root expected handoff phase is invalid")
    expected = _handoff_ledger(
        resolved,
        domain_uuid,
        domain_id,
        libvirt_uri,
        boot_attempt_id,
        phase,
        lifecycle,
    )
    current = _plain_wire_value(state.get("oci_root_handoff"))
    if state.get("status") != expected_status or current != expected:
        raise StateError(f"OCI-root {phase} handoff ledger changed")


class _LaunchControlAmbiguity(StateError):
    """The captured boot instance can no longer be proven safe to mutate."""


def _handle_launch_failure(
    roots: StatePaths,
    name: str,
    conn: Any,
    resolved: ResolvedOCIRootDomainPlan,
    domain_uuid: str,
    domain_id: int | None,
    libvirt_uri: str,
    boot_attempt_id: str,
    ledger_phase: str,
    ready_lifecycle: OCILifecycleHandoffReceipt | None,
) -> str:
    with locked_existing_run(roots, name) as mutation:
        ledger_domain_id = None if ledger_phase == "activating" else domain_id
        _require_expected_handoff(
            mutation.snapshot.state,
            resolved,
            domain_uuid,
            ledger_domain_id,
            libvirt_uri,
            boot_attempt_id,
            ledger_phase,
            ready_lifecycle,
        )
        cleanup_phase = "failed"
        try:
            _cleanup_exact_launch_domain(conn, resolved, domain_uuid, domain_id)
        except _LaunchControlAmbiguity:
            cleanup_phase = "cleanup-not-attempted"
        except Exception:
            cleanup_phase = "cleanup-required"
        data = mutation.mutable_state()
        messages = {
            "cleanup-not-attempted": "OCI-root launch failed; control ambiguity prevented cleanup",
            "cleanup-required": "OCI-root launch failed and cleanup is required",
            "failed": "OCI-root launch failed",
        }
        data["error"] = messages[cleanup_phase]
        handoff = data.get("oci_root_handoff")
        if not isinstance(handoff, dict):
            raise StateError("OCI-root launch ledger changed before failure publication")
        handoff["phase"] = cleanup_phase
        mutation.write_state("failed", data)
        return cleanup_phase


def _exact_launch_instance(
    conn: Any,
    resolved: ResolvedOCIRootDomainPlan,
    expected_uuid: str,
    expected_domain_id: int,
    *,
    active: bool,
) -> Any:
    try:
        domain = _exact_domain(conn, resolved, expected_uuid)
        inactive_flag = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
        if type(inactive_flag) is not int:
            raise StateError("libvirt inactive XML inspection support is unavailable")
        actual_xml = domain.XMLDesc(inactive_flag)
        if _domain_projection(actual_xml) != _domain_projection(resolved.xml):
            raise StateError("OCI-root persistent domain XML changed")
        actual_root = ET.fromstring(actual_xml)
        xml_uuids = actual_root.findall("./uuid")
        if xml_uuids and xml_uuids[0].text != expected_uuid:
            raise StateError("OCI-root persistent XML UUID changed")
        actual_active = domain.isActive()
        actual_domain_id = _domain_id(domain)
    except Exception as exc:
        raise _LaunchControlAmbiguity("OCI-root boot instance identity is ambiguous") from exc
    expected_active = 1 if active else 0
    expected_id = expected_domain_id if active else -1
    if actual_active != expected_active or actual_domain_id != expected_id:
        raise _LaunchControlAmbiguity("OCI-root boot instance changed before cleanup")
    return domain


def _cleanup_exact_launch_domain(
    conn: Any,
    resolved: ResolvedOCIRootDomainPlan,
    expected_uuid: str,
    expected_domain_id: int | None,
) -> None:
    try:
        domain = _exact_domain(conn, resolved, expected_uuid)
        active = domain.isActive()
    except Exception as exc:
        raise _LaunchControlAmbiguity("OCI-root domain activity is ambiguous during launch cleanup") from exc
    if active not in {0, 1}:
        raise _LaunchControlAmbiguity("OCI-root domain activity is ambiguous during launch cleanup")
    if active == 1:
        if expected_domain_id is None:
            raise _LaunchControlAmbiguity("OCI-root active boot instance ID was not captured")
        domain = _exact_launch_instance(
            conn,
            resolved,
            expected_uuid,
            expected_domain_id,
            active=True,
        )
        try:
            domain.destroy()
        except Exception as exc:
            raise StateError("OCI-root active domain cleanup failed") from exc
    domain = _exact_launch_instance(
        conn,
        resolved,
        expected_uuid,
        0 if expected_domain_id is None else expected_domain_id,
        active=False,
    )
    try:
        domain.undefine()
    except Exception as exc:
        raise StateError("OCI-root domain definition cleanup failed") from exc
    if _lookup(conn, resolved.plan.run_name) is not None or _lookup_uuid(conn, expected_uuid) is not None:
        raise StateError("OCI-root domain remains after launch cleanup")


def _close_unowned_stream(stream: Any) -> None:
    for operation in ("abort", "free"):
        try:
            getattr(stream, operation)()
        except Exception:
            pass


def launch_defined_oci_root_domain(
    roots: StatePaths,
    name: str,
    store: OCIStore,
    boot_artifacts: VerifiedHostBootArtifacts,
    profile: DomainProfile,
    *,
    conn: Any,
    runner: CommandRunner = _default_runner,
    timeout_seconds: float = DEFAULT_HANDOFF_TIMEOUT_SECONDS,
    terminal_timeout_seconds: float | None = None,
) -> CompletedOCIRootHandoff:
    """Privately launch an exact defined domain and synchronously observe exit.

    This function remains disconnected from public runtime dispatch.  It does
    not implement detached operation, reconnect, stop, exec, or log delivery.
    """

    if conn is None:
        raise StateError("OCI-root launch requires an explicit libvirt connection")
    started_intent = False
    resolved: ResolvedOCIRootDomainPlan | None = None
    domain_uuid: str | None = None
    domain_id: int | None = None
    libvirt_uri = profile.uri
    boot_attempt_id: str | None = None
    ledger_phase = "activating"
    stream: Any | None = None
    stream_handed_off = False
    ready_lifecycle: OCILifecycleHandoffReceipt | None = None
    try:
        with locked_existing_run(roots, name) as mutation:
            libvirt_uri = _connection_uri(conn, profile)
            domain_uuid, definition_digest = _definition_ledger(mutation.snapshot.state, profile)
            resolved = resolve_committed_oci_root_domain_plan(
                roots,
                mutation.snapshot,
                store,
                boot_artifacts,
                profile,
                runner=runner,
                expected_status="defined",
            )
            if definition_digest != resolved.plan.digest:
                raise StateError("OCI-root durable definition plan binding is invalid")
            domain = _exact_domain(conn, resolved, domain_uuid)
            _validate_defined_domain(domain, resolved, domain_uuid)
            libvirt = kvm._libvirt()
            flags = getattr(libvirt, "VIR_STREAM_NONBLOCK", None)
            inactive_flag = getattr(libvirt, "VIR_DOMAIN_XML_INACTIVE", None)
            if type(flags) is not int or type(inactive_flag) is not int:
                raise StateError("required libvirt stream or inactive XML support is unavailable")
            if not callable(getattr(conn, "newStream", None)) or any(
                not callable(getattr(domain, operation, None))
                for operation in ("create", "destroy", "ID", "openChannel", "undefine")
            ):
                raise StateError("required libvirt OCI-root lifecycle operations are unavailable")
            lifecycle_contract = resolved.plan.to_dict().get("lifecycle_control")
            if (
                not isinstance(lifecycle_contract, Mapping)
                or lifecycle_contract.get("channel_name") != OCI_CONTROL_CHANNEL_NAME
            ):
                raise StateError("OCI-root durable lifecycle channel contract is invalid")
            channel_name = str(lifecycle_contract["channel_name"])
            binding = OCIControlV2Binding(
                resolved.plan.run_id,
                resolved.plan.domain_core_digest,
                str(resolved.plan.stage1_transport["artifact_digest"]),
            )
            lifecycle_session = HostOCIControlV2Session(binding)
            boot_attempt_id = lifecycle_session.boot_attempt_id
            try:
                stream = conn.newStream(flags)
            except Exception:
                raise StateError("OCI-root lifecycle stream allocation failed") from None
            if stream is None or any(
                not callable(getattr(stream, operation, None)) for operation in ("send", "recv", "abort", "free")
            ):
                raise StateError("OCI-root lifecycle stream surface is invalid")
            mutation.verify_binding()
            data = mutation.mutable_state()
            data["oci_root_handoff"] = _handoff_ledger(
                resolved,
                domain_uuid,
                None,
                libvirt_uri,
                boot_attempt_id,
                "activating",
            )
            result = mutation.write_state("starting", data)
            if result.get("status") != "starting":
                raise StateError("OCI-root activation intent was not durably recorded")
            started_intent = True
            try:
                domain.create()
            except Exception:
                raise StateError("OCI-root domain activation failed") from None
            captured_domain_id = _domain_id(domain)
            if captured_domain_id <= 0:
                raise StateError("OCI-root domain activation did not yield an active boot instance")
            domain_id = captured_domain_id
            domain = _exact_domain(conn, resolved, domain_uuid)
            _validate_active_domain(domain, resolved, domain_uuid, domain_id)
            data = mutation.mutable_state()
            data["oci_root_handoff"] = _handoff_ledger(
                resolved,
                domain_uuid,
                domain_id,
                libvirt_uri,
                boot_attempt_id,
                "starting",
            )
            result = mutation.write_state("starting", data)
            if result.get("status") != "starting":
                raise StateError("OCI-root starting intent was not durably recorded")
            ledger_phase = "starting"

        try:
            domain = _exact_domain(conn, resolved, domain_uuid)
            _validate_active_domain(domain, resolved, domain_uuid, domain_id)
            try:
                opened = domain.openChannel(channel_name, stream, 0)
            except Exception:
                raise StateError("OCI-root lifecycle channel open failed") from None
            if opened != 0:
                raise StateError("OCI-root lifecycle channel open result is invalid")
        except BaseException:
            _close_unowned_stream(stream)
            stream = None
            raise

        def publish_ready(lifecycle: OCILifecycleHandoffReceipt) -> None:
            nonlocal ledger_phase, ready_lifecycle
            with locked_existing_run(roots, name) as mutation:
                current_uuid, current_digest = _definition_ledger(mutation.snapshot.state, profile)
                if current_uuid != domain_uuid or current_digest != resolved.plan.digest:
                    raise StateError("OCI-root durable definition changed before READY")
                if (
                    not isinstance(lifecycle, OCILifecycleHandoffReceipt)
                    or lifecycle.phase != "ready"
                    or lifecycle.terminal is not None
                    or lifecycle.boot_attempt_id != boot_attempt_id
                ):
                    raise StateError("OCI-root READY boot attempt is stale")
                _require_expected_handoff(
                    mutation.snapshot.state,
                    resolved,
                    domain_uuid,
                    domain_id,
                    libvirt_uri,
                    boot_attempt_id,
                    "starting",
                )
                active_domain = _exact_domain(conn, resolved, domain_uuid)
                _validate_active_domain(active_domain, resolved, domain_uuid, domain_id)
                data = mutation.mutable_state()
                data.pop("error", None)
                data["oci_root_handoff"] = _handoff_ledger(
                    resolved,
                    domain_uuid,
                    domain_id,
                    libvirt_uri,
                    boot_attempt_id,
                    "ready",
                    lifecycle,
                )
                result = mutation.write_state("running", data)
                if result.get("status") != "running":
                    raise StateError("OCI-root READY was not durably recorded")
                ready_lifecycle = lifecycle
                ledger_phase = "ready"

        stream_handed_off = True
        lifecycle = complete_initial_lifecycle_handoff(
            stream,
            binding,
            on_ready=publish_ready,
            timeout_seconds=timeout_seconds,
            terminal_timeout_seconds=terminal_timeout_seconds,
            session=lifecycle_session,
        )
        stream = None
        terminal = lifecycle.terminal if isinstance(lifecycle, OCILifecycleHandoffReceipt) else None
        if terminal is None or lifecycle.phase != "terminal" or lifecycle.boot_attempt_id != boot_attempt_id:
            raise StateError("OCI-root lifecycle terminal result is missing")

        with locked_existing_run(roots, name) as mutation:
            current_uuid, current_digest = _definition_ledger(mutation.snapshot.state, profile)
            if current_uuid != domain_uuid or current_digest != resolved.plan.digest:
                raise StateError("OCI-root durable definition changed before TERMINAL")
            if ready_lifecycle is None:
                raise StateError("OCI-root durable READY receipt is missing")
            _require_expected_handoff(
                mutation.snapshot.state,
                resolved,
                domain_uuid,
                domain_id,
                libvirt_uri,
                boot_attempt_id,
                "ready",
                ready_lifecycle,
            )
            if (
                lifecycle.boot_generation != ready_lifecycle.boot_generation
                or lifecycle.key_id != ready_lifecycle.key_id
            ):
                raise StateError("OCI-root TERMINAL lifecycle identity changed")
            domain = _exact_domain(conn, resolved, domain_uuid)
            try:
                active = domain.isActive()
            except Exception as exc:
                raise StateError("OCI-root domain activity is ambiguous at TERMINAL") from exc
            if active not in {0, 1}:
                raise StateError("OCI-root domain activity is ambiguous at TERMINAL")
            if active == 1:
                domain = _exact_launch_instance(
                    conn,
                    resolved,
                    domain_uuid,
                    domain_id,
                    active=True,
                )
                try:
                    domain.destroy()
                except Exception:
                    raise StateError("OCI-root domain could not be stopped after TERMINAL") from None
            _exact_launch_instance(
                conn,
                resolved,
                domain_uuid,
                domain_id,
                active=False,
            )
            data = mutation.mutable_state()
            data.pop("error", None)
            data["oci_root_handoff"] = _handoff_ledger(
                resolved,
                domain_uuid,
                domain_id,
                libvirt_uri,
                boot_attempt_id,
                "terminal",
                lifecycle,
            )
            result = mutation.write_state("exited", data)
            if result.get("status") != "exited":
                raise StateError("OCI-root TERMINAL was not durably recorded")
        return CompletedOCIRootHandoff(
            resolved.plan.run_id,
            resolved.plan.run_name,
            resolved.plan.digest,
            domain_uuid,
            domain_id,
            libvirt_uri,
            terminal,
            lifecycle,
        )
    except BaseException:
        if stream is not None and not stream_handed_off:
            _close_unowned_stream(stream)
        if started_intent and resolved is not None and domain_uuid is not None and boot_attempt_id is not None:
            try:
                cleanup_phase = _handle_launch_failure(
                    roots,
                    name,
                    conn,
                    resolved,
                    domain_uuid,
                    domain_id,
                    libvirt_uri,
                    boot_attempt_id,
                    ledger_phase,
                    ready_lifecycle,
                )
            except Exception:
                raise StateError("OCI-root launch state changed; cleanup was not attempted") from None
            if cleanup_phase == "cleanup-not-attempted":
                raise StateError("OCI-root launch control is ambiguous; cleanup was not attempted") from None
            if cleanup_phase == "cleanup-required":
                raise StateError("OCI-root launch cleanup failed; cleanup is required") from None
            raise StateError("OCI-root launch failed") from None
        raise


__all__ = [
    "OCI_ROOT_DEFINITION_SCHEMA",
    "CompletedOCIRootHandoff",
    "DefinedOCIRootDomain",
    "define_committed_oci_root_domain",
    "launch_defined_oci_root_domain",
]
