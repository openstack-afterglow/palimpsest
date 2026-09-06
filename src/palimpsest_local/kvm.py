"""Libvirt XML and lifecycle primitives with a delayed optional dependency."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cloudinit import BUILD_CHANNEL_NAME
from .digest import normalize_digest
from .oci_control_protocol import OCI_CONTROL_CHANNEL_NAME
from .oci_control_protocol_v2 import OCI_CONTROL_PROTOCOL_V2
from .platforms import DomainProfile

_logger = logging.getLogger(__name__)
_DOMAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NETWORK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_DISK_LETTERS = "bcdefghijklmnopqrstuvwxyz"
MAX_LAYER_DISKS = len(_DISK_LETTERS)
MAX_OCI_ROOT_LAYER_DISKS = MAX_LAYER_DISKS - 1
LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES = 107
DOMAIN_MARKER_VERSION = "0.1.0"
DOMAIN_MARKER_NAMESPACE = "https://afterglow.dev/palimpsest-local/domain/v1"
ET.register_namespace("palimpsest", DOMAIN_MARKER_NAMESPACE)


class KvmError(RuntimeError):
    """An expected local KVM operational failure."""


class KvmUnavailable(KvmError):
    """The optional libvirt runtime is unavailable."""


def _validate_oci_lifecycle_socket_path(path: Path | None) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or path.name != "lifecycle.sock":
        raise KvmError("OCI-root lifecycle socket path is invalid")
    try:
        encoded = os.fsencode(path)
    except (TypeError, UnicodeError, ValueError):
        raise KvmError("OCI-root lifecycle socket path is invalid") from None
    if b"\0" in encoded:
        raise KvmError("OCI-root lifecycle socket path is invalid")
    if len(encoded) > LIBVIRT_UNIX_SOCKET_PATH_MAX_BYTES:
        raise KvmError("OCI-root lifecycle socket path exceeds the Linux AF_UNIX pathname limit")


@dataclass(frozen=True)
class LayerDisk:
    blob_digest: str
    host_path: Path
    target_dev: str
    serial: str


@dataclass(frozen=True)
class Stage1TransportDisk:
    artifact_digest: str
    host_path: Path
    target_dev: str
    serial: str


@dataclass(frozen=True)
class VolumeDisk:
    name: str
    host_path: Path
    target_dev: str
    serial: str
    mount_path: str
    filesystem: str = "ext4"
    read_only: bool = False


@dataclass(frozen=True)
class DomainSpec:
    name: str
    memory_mib: int
    vcpus: int
    root_disk: Path
    seed_iso: Path
    layers: list[LayerDisk]
    volumes: list[VolumeDisk] = field(default_factory=list)
    network: str | None = "default"
    console_log: Path | None = None
    run_id: str | None = None
    guest_agent: bool = True
    control_socket: Path | None = None
    ssh_host_port: int | None = None
    nvram: Path | None = None


@dataclass(frozen=True)
class OCIRootDomainSpec:
    """Local-only libvirt inputs for a direct-kernel OCI-root guest."""

    name: str
    memory_mib: int
    vcpus: int
    kernel: Path
    initramfs: Path
    kernel_cmdline: str
    root_disk: Path
    root_serial: str
    layers: tuple[LayerDisk, ...]
    stage1_transport: Stage1TransportDisk
    network: str | None = "default"
    console_log: Path | None = None
    run_id: str | None = None
    boot_contract_digest: str | None = None
    lifecycle_socket: Path | None = None
    lifecycle_channel_name: str = OCI_CONTROL_CHANNEL_NAME
    lifecycle_protocol: str = OCI_CONTROL_PROTOCOL_V2
    dac_label: str | None = None


def _valid_name(name: str) -> bool:
    return _DOMAIN_NAME_RE.fullmatch(name) is not None


def layer_blob_path(layer_root: Path, digest: str) -> Path:
    try:
        digest_hex = normalize_digest(digest).split(":", 1)[1]
    except Exception as exc:
        raise KvmError(f"invalid digest: {digest!r}") from exc
    return layer_root / "blobs" / "sha256" / digest_hex


def build_layer_disks(layer_root: Path, digests: list[str]) -> list[LayerDisk]:
    if not digests:
        raise KvmError("at least one layer is required")
    if len(digests) > MAX_LAYER_DISKS:
        raise KvmError(f"layer count exceeds limit {MAX_LAYER_DISKS}")
    disks: list[LayerDisk] = []
    serials: set[str] = set()
    for index, digest in enumerate(digests):
        try:
            normalized = normalize_digest(digest)
        except Exception as exc:
            raise KvmError(f"invalid digest: {digest!r}") from exc
        serial = normalized.split(":", 1)[1][:20]
        if serial in serials:
            raise KvmError("layer serial collision")
        serials.add(serial)
        disks.append(
            LayerDisk(normalized, layer_blob_path(layer_root, normalized), f"vd{_DISK_LETTERS[index]}", serial)
        )
    return disks


def _disk(
    devices: ET.Element,
    path: Path,
    target: str,
    disk_format: str,
    *,
    readonly: bool,
    device: str = "disk",
    bus: str = "virtio",
    shareable: bool = False,
    dac_no_relabel: bool = False,
) -> ET.Element:
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": device})
    driver = {"name": "qemu", "type": disk_format}
    if target == "vda":
        driver["discard"] = "unmap"
    ET.SubElement(disk, "driver", driver)
    source = ET.SubElement(disk, "source", {"file": str(path)})
    if dac_no_relabel:
        ET.SubElement(source, "seclabel", {"model": "dac", "relabel": "no"})
    ET.SubElement(disk, "target", {"dev": target, "bus": bus})
    if readonly:
        ET.SubElement(disk, "readonly")
    if shareable:
        ET.SubElement(disk, "shareable")
    return disk


def build_domain_xml(spec: DomainSpec, profile: DomainProfile) -> str:
    if not _valid_name(spec.name):
        raise KvmError("domain name must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if not 256 <= spec.memory_mib <= 1_048_576:
        raise KvmError("memory_mib is outside the supported range")
    if not 1 <= spec.vcpus <= 256:
        raise KvmError("vcpus is outside the supported range")
    if len(spec.layers) + len(spec.volumes) > MAX_LAYER_DISKS:
        raise KvmError("layer and volume count exceeds disk limit")

    domain_attrib = {"type": profile.domain_type}
    if profile.network_mode == "user-hostfwd":
        domain_attrib["xmlns:qemu"] = "http://libvirt.org/schemas/domain/qemu/1.0"
    domain = ET.Element("domain", domain_attrib)
    ET.SubElement(domain, "name").text = spec.name
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(spec.memory_mib)
    ET.SubElement(domain, "vcpu").text = str(spec.vcpus)
    os_attrib = {"firmware": "efi"} if profile.autoselect_firmware else {}
    os_element = ET.SubElement(domain, "os", os_attrib)
    ET.SubElement(os_element, "type", {"arch": profile.arch, "machine": profile.machine}).text = "hvm"
    ET.SubElement(os_element, "boot", {"dev": "hd"})
    if profile.firmware is not None:
        if spec.nvram is None or not spec.nvram.is_absolute():
            raise KvmError("nvram path is required for pflash firmware")
        ET.SubElement(os_element, "loader", {"readonly": "yes", "type": "pflash"}).text = str(profile.firmware.loader)
        ET.SubElement(os_element, "nvram").text = str(spec.nvram)
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    if profile.arch == "x86_64":
        ET.SubElement(features, "apic")
    ET.SubElement(domain, "cpu", {"mode": "host-passthrough"})
    if spec.run_id is not None:
        metadata = ET.SubElement(domain, "metadata")
        ET.SubElement(
            metadata,
            "{https://afterglow.dev/palimpsest-local/domain/v1}run",
            {"id": spec.run_id, "schema": "1", "version": DOMAIN_MARKER_VERSION},
        )
    devices = ET.SubElement(domain, "devices")
    ET.SubElement(devices, "emulator").text = str(profile.emulator)
    _disk(devices, spec.root_disk, "vda", "qcow2", readonly=False)
    seen_serials: set[str] = set()
    for layer in spec.layers:
        if layer.target_dev == "vda" or layer.serial in seen_serials or len(layer.serial) != 20:
            raise KvmError("invalid or colliding layer disk")
        seen_serials.add(layer.serial)
        disk = _disk(devices, layer.host_path, layer.target_dev, "raw", readonly=True)
        ET.SubElement(disk, "serial").text = layer.serial
    for volume in spec.volumes:
        if volume.target_dev == "vda" or volume.serial in seen_serials or len(volume.serial) != 20:
            raise KvmError("invalid or colliding volume disk")
        if volume.filesystem != "ext4":
            raise KvmError("only ext4 volume disks are supported")
        if not volume.host_path.is_absolute():
            raise KvmError("volume disk host paths must be absolute")
        seen_serials.add(volume.serial)
        disk = _disk(devices, volume.host_path, volume.target_dev, "raw", readonly=volume.read_only)
        ET.SubElement(disk, "serial").text = volume.serial
    if profile.seed_bus == "scsi":
        ET.SubElement(devices, "controller", {"type": "scsi", "index": "0", "model": "virtio-scsi"})
    _disk(devices, spec.seed_iso, "sda", "raw", readonly=True, device="cdrom", bus=profile.seed_bus)
    if profile.network_mode == "libvirt-network" and spec.network is not None:
        interface = ET.SubElement(devices, "interface", {"type": "network"})
        ET.SubElement(interface, "source", {"network": spec.network})
        ET.SubElement(interface, "model", {"type": "virtio"})
    if spec.console_log is None:
        console = ET.SubElement(devices, "console", {"type": "pty"})
    else:
        console = ET.SubElement(devices, "console", {"type": "file"})
        ET.SubElement(console, "source", {"path": str(spec.console_log), "append": "on"})
    ET.SubElement(console, "target", {"type": "serial", "port": "0"})
    if spec.guest_agent:
        agent_channel = ET.SubElement(devices, "channel", {"type": "unix"})
        ET.SubElement(agent_channel, "target", {"type": "virtio", "name": "org.qemu.guest_agent.0"})
    if spec.control_socket is not None:
        if not spec.control_socket.is_absolute():
            raise KvmError("control_socket must be an absolute path")
        channel = ET.SubElement(devices, "channel", {"type": "unix"})
        ET.SubElement(channel, "source", {"mode": "bind", "path": str(spec.control_socket)})
        ET.SubElement(channel, "target", {"type": "virtio", "name": BUILD_CHANNEL_NAME})
    if profile.network_mode == "user-hostfwd":
        if spec.ssh_host_port is None or not 1 <= spec.ssh_host_port <= 65535:
            raise KvmError("user-mode networking requires an ssh host port")
        commandline = ET.SubElement(domain, "qemu:commandline")
        ET.SubElement(commandline, "qemu:arg", {"value": "-netdev"})
        ET.SubElement(
            commandline,
            "qemu:arg",
            {"value": f"user,id=palimpsest0,hostfwd=tcp:127.0.0.1:{spec.ssh_host_port}-:22"},
        )
        ET.SubElement(commandline, "qemu:arg", {"value": "-device"})
        ET.SubElement(commandline, "qemu:arg", {"value": "virtio-net-pci,netdev=palimpsest0"})
    return ET.tostring(domain, encoding="unicode")


def build_oci_root_domain_xml(spec: OCIRootDomainSpec, profile: DomainProfile) -> str:
    """Build the fail-closed direct-kernel domain shape used by OCI-root.

    This deliberately does not share the cloud-image builder: OCI-root has a
    raw writable root, no firmware boot disk, and no NoCloud seed device.
    """

    if profile.backend != "kvm" or profile.domain_type != "kvm" or profile.arch != "x86_64":
        raise KvmError("OCI-root direct boot currently requires x86_64 KVM")
    if profile.firmware is not None or profile.autoselect_firmware:
        raise KvmError("OCI-root direct boot does not accept a firmware profile")
    if not _valid_name(spec.name):
        raise KvmError("domain name must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if not 256 <= spec.memory_mib <= 1_048_576:
        raise KvmError("memory_mib is outside the supported range")
    if not 1 <= spec.vcpus <= 256:
        raise KvmError("vcpus is outside the supported range")
    if not spec.layers or len(spec.layers) > MAX_OCI_ROOT_LAYER_DISKS:
        raise KvmError("OCI-root layer count is outside the supported range")
    for label, path in (
        ("kernel", spec.kernel),
        ("initramfs", spec.initramfs),
        ("root disk", spec.root_disk),
    ):
        if not isinstance(path, Path) or not path.is_absolute():
            raise KvmError(f"OCI-root {label} path must be absolute")
    if spec.console_log is not None and not spec.console_log.is_absolute():
        raise KvmError("OCI-root console path must be absolute")
    if not isinstance(spec.kernel_cmdline, str) or not spec.kernel_cmdline or len(spec.kernel_cmdline) > 4096:
        raise KvmError("OCI-root kernel command line is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in spec.kernel_cmdline):
        raise KvmError("OCI-root kernel command line contains control characters")
    if re.fullmatch(r"[0-9a-f]{20}", spec.root_serial or "") is None:
        raise KvmError("OCI-root root disk serial is invalid")
    if spec.network is not None and _NETWORK_NAME_RE.fullmatch(spec.network) is None:
        raise KvmError("OCI-root network name is invalid")
    try:
        run_id = str(uuid.UUID(spec.run_id or ""))
    except (AttributeError, TypeError, ValueError):
        raise KvmError("OCI-root run ID is invalid") from None
    if run_id != spec.run_id:
        raise KvmError("OCI-root run ID is not canonical")
    try:
        contract_digest = normalize_digest(spec.boot_contract_digest or "")
    except Exception:
        raise KvmError("OCI-root boot contract digest is invalid") from None
    if contract_digest != spec.boot_contract_digest:
        raise KvmError("OCI-root boot contract digest is not canonical")
    if spec.lifecycle_channel_name != OCI_CONTROL_CHANNEL_NAME or spec.lifecycle_protocol != OCI_CONTROL_PROTOCOL_V2:
        raise KvmError("OCI-root lifecycle channel contract is invalid")
    _validate_oci_lifecycle_socket_path(spec.lifecycle_socket)

    seen_serials = {spec.root_serial}
    for index, layer in enumerate(spec.layers):
        try:
            layer_digest = normalize_digest(layer.blob_digest)
        except Exception:
            raise KvmError("OCI-root layer digest is invalid") from None
        if (
            layer.target_dev != f"vd{_DISK_LETTERS[index + 1]}"
            or not layer.host_path.is_absolute()
            or layer.host_path == spec.root_disk
            or layer_digest != layer.blob_digest
            or re.fullmatch(r"[0-9a-f]{20}", layer.serial or "") is None
            or layer.serial in seen_serials
        ):
            raise KvmError("OCI-root layer disk order or identity is invalid")
        seen_serials.add(layer.serial)
    transport = spec.stage1_transport
    try:
        transport_digest = normalize_digest(transport.artifact_digest)
    except Exception:
        raise KvmError("OCI-root stage-1 transport digest is invalid") from None
    if (
        transport_digest != transport.artifact_digest
        or not transport.host_path.is_absolute()
        or transport.host_path == spec.root_disk
        or transport.host_path in {layer.host_path for layer in spec.layers}
        or transport.target_dev != "vdb"
        or re.fullmatch(r"[0-9a-f]{20}", transport.serial or "") is None
        or transport.serial in seen_serials
    ):
        raise KvmError("OCI-root stage-1 transport identity is invalid")

    domain = ET.Element("domain", {"type": "kvm"})
    if spec.dac_label is not None:
        if (
            not isinstance(spec.dac_label, str)
            or re.fullmatch(r"\+[1-9][0-9]{0,9}:\+[1-9][0-9]{0,9}", spec.dac_label) is None
        ):
            raise KvmError("OCI-root fixed DAC principal is invalid")
        if any(int(value) >= 2**32 - 1 for value in spec.dac_label.split(":")):
            raise KvmError("OCI-root fixed DAC principal is invalid")
        label = ET.SubElement(domain, "seclabel", {"type": "static", "model": "dac", "relabel": "no"})
        ET.SubElement(label, "label").text = spec.dac_label
    ET.SubElement(domain, "name").text = spec.name
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(spec.memory_mib)
    ET.SubElement(domain, "vcpu").text = str(spec.vcpus)
    os_element = ET.SubElement(domain, "os")
    ET.SubElement(os_element, "type", {"arch": profile.arch, "machine": profile.machine}).text = "hvm"
    ET.SubElement(os_element, "kernel").text = str(spec.kernel)
    ET.SubElement(os_element, "initrd").text = str(spec.initramfs)
    ET.SubElement(os_element, "cmdline").text = spec.kernel_cmdline
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    ET.SubElement(features, "apic")
    ET.SubElement(domain, "cpu", {"mode": "host-passthrough"})
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(
        metadata,
        "{https://afterglow.dev/palimpsest-local/domain/v1}run",
        {
            "id": run_id,
            "schema": "1",
            "version": DOMAIN_MARKER_VERSION,
            "contract": contract_digest,
        },
    )
    ET.SubElement(
        metadata,
        "{https://afterglow.dev/palimpsest-local/domain/v1}lifecycle",
        {"channel": spec.lifecycle_channel_name, "protocol": spec.lifecycle_protocol},
    )
    devices = ET.SubElement(domain, "devices")
    ET.SubElement(devices, "emulator").text = str(profile.emulator)
    # libvirt forbids per-source label overrides when domain relabeling is off.
    source_dac_override = spec.dac_label is None
    root = _disk(devices, spec.root_disk, "vda", "raw", readonly=False, dac_no_relabel=source_dac_override)
    ET.SubElement(root, "serial").text = spec.root_serial
    transport_disk = _disk(
        devices,
        transport.host_path,
        transport.target_dev,
        "raw",
        readonly=True,
        dac_no_relabel=source_dac_override,
    )
    ET.SubElement(transport_disk, "serial").text = transport.serial
    for layer in spec.layers:
        disk = _disk(
            devices,
            layer.host_path,
            layer.target_dev,
            "raw",
            readonly=True,
            shareable=True,
            dac_no_relabel=source_dac_override,
        )
        ET.SubElement(disk, "serial").text = layer.serial
    ET.SubElement(devices, "controller", {"type": "virtio-serial", "index": "0"})
    lifecycle_channel = ET.SubElement(devices, "channel", {"type": "unix"})
    ET.SubElement(
        lifecycle_channel,
        "source",
        {"mode": "bind", "path": str(spec.lifecycle_socket)},
    )
    ET.SubElement(
        lifecycle_channel,
        "target",
        {"type": "virtio", "name": spec.lifecycle_channel_name},
    )
    if profile.network_mode == "libvirt-network" and spec.network is not None:
        interface = ET.SubElement(devices, "interface", {"type": "network"})
        ET.SubElement(interface, "source", {"network": spec.network})
        ET.SubElement(interface, "model", {"type": "virtio"})
    if spec.console_log is None:
        console = ET.SubElement(devices, "console", {"type": "pty"})
    else:
        console = ET.SubElement(devices, "console", {"type": "file"})
        console_source = ET.SubElement(console, "source", {"path": str(spec.console_log), "append": "on"})
        if source_dac_override:
            ET.SubElement(console_source, "seclabel", {"model": "dac", "relabel": "no"})
    ET.SubElement(console, "target", {"type": "serial", "port": "0"})
    return ET.tostring(domain, encoding="unicode")


def build_seed_iso_command(seed_iso: Path, user_data: Path, meta_data: Path) -> list[str]:
    for label, path in (("seed_iso", seed_iso), ("user_data", user_data), ("meta_data", meta_data)):
        if not path.is_absolute():
            raise KvmError(f"{label} must be an absolute path: {path!s}")
    return ["cloud-localds", str(seed_iso), str(user_data), str(meta_data)]


def build_layer_activation_script(
    disks: list[LayerDisk],
    *,
    merged_dir: str = "/opt/layers/merged",
    volumes: list[VolumeDisk] | tuple[VolumeDisk, ...] = (),
) -> str:
    lines = ["set -euo pipefail", f"mkdir -p /opt/layers/upper /opt/layers/work {shlex.quote(merged_dir)}"]
    mounts: list[str] = []
    for index, layer in enumerate(disks):
        mount_point = f"/mnt/palimpsest/lower{index}"
        by_id = f"/dev/disk/by-id/virtio-{layer.serial}"
        lines.extend(
            [
                f"mkdir -p {shlex.quote(mount_point)}",
                f"DEV={shlex.quote(by_id)}",
                'for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done',
                '[ -e "$DEV" ] || { echo "layer disk missing: $DEV" >&2; exit 1; }',
                f'mount -t squashfs -o ro "$DEV" {shlex.quote(mount_point)}',
            ]
        )
        mounts.append(mount_point)
    if mounts:
        lowerdir = ":".join(shlex.quote(path) for path in reversed(mounts))
        lines.append(
            f"mount -t overlay overlay -o lowerdir={lowerdir},upperdir=/opt/layers/upper,workdir=/opt/layers/work {shlex.quote(merged_dir)}"
        )
    for volume in volumes:
        by_id = f"/dev/disk/by-id/virtio-{volume.serial}"
        options = "ro,noload" if volume.read_only else "rw,noatime"
        lines.extend(
            [
                f"install -d -m 0755 {shlex.quote(volume.mount_path)}",
                f"DEV={shlex.quote(by_id)}",
                'for _ in $(seq 1 30); do [ -e "$DEV" ] && break; sleep 1; done',
                '[ -e "$DEV" ] || { echo "volume disk missing: $DEV" >&2; exit 1; }',
                f'mount -t {shlex.quote(volume.filesystem)} -o {options} "$DEV" {shlex.quote(volume.mount_path)}',
            ]
        )
    return "\n".join(lines) + "\n"


def run_seed_iso(seed_iso: Path, user_data: Path, meta_data: Path) -> None:
    try:
        subprocess.run(
            build_seed_iso_command(seed_iso, user_data, meta_data), check=True, capture_output=True, timeout=120
        )
    except FileNotFoundError as exc:
        raise KvmError("cloud-localds is unavailable; install cloud-image-utils") from exc
    except subprocess.CalledProcessError as exc:
        raise KvmError(f"seed ISO creation failed: {exc.stderr.decode(errors='replace')[:200]}") from exc


def build_hdiutil_seed_command(seed_iso: Path, seed_dir: Path) -> list[str]:
    for label, path in (("seed_iso", seed_iso), ("seed_dir", seed_dir)):
        if not path.is_absolute():
            raise KvmError(f"{label} must be an absolute path: {path!s}")
    return [
        "hdiutil",
        "makehybrid",
        "-iso",
        "-joliet",
        "-default-volume-name",
        "CIDATA",
        "-o",
        str(seed_iso),
        str(seed_dir),
    ]


def run_hdiutil_seed_iso(seed_iso: Path, seed_dir: Path) -> None:
    try:
        subprocess.run(build_hdiutil_seed_command(seed_iso, seed_dir), check=True, capture_output=True, timeout=120)
    except FileNotFoundError as exc:
        raise KvmError("hdiutil is unavailable; macOS libvirt runs require hdiutil") from exc
    except subprocess.CalledProcessError as exc:
        raise KvmError(f"seed ISO creation failed: {exc.stderr.decode(errors='replace')[:200]}") from exc


def _libvirt() -> Any:
    try:
        import libvirt
    except ImportError as exc:
        raise KvmUnavailable("libvirt-python is not installed; install palimpsest-local[kvm]") from exc
    return libvirt


def _validated_domain_marker_run_id(element: ET.Element) -> str | None:
    if (
        element.tag != f"{{{DOMAIN_MARKER_NAMESPACE}}}run"
        or element.get("schema") != "1"
        or element.get("version") != DOMAIN_MARKER_VERSION
    ):
        return None
    run_id = element.get("id")
    if not isinstance(run_id, str):
        return None
    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        return None
    return run_id if str(parsed) == run_id else None


def get_domain_run_id(domain: Any) -> str | None:
    """Extract the Palimpsest run ID from a libvirt domain."""
    try:
        libvirt = _libvirt()
        metadata_xml = domain.metadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            DOMAIN_MARKER_NAMESPACE,
        )
        if metadata_xml:
            root = ET.fromstring(metadata_xml)
            run_id = _validated_domain_marker_run_id(root)
            if run_id is not None:
                return run_id
    except Exception:
        pass

    try:
        root = ET.fromstring(domain.XMLDesc())
        element = root.find(f"./metadata/{{{DOMAIN_MARKER_NAMESPACE}}}run")
        if element is not None:
            return _validated_domain_marker_run_id(element)
    except Exception:
        pass
    return None


def connect(uri: str):
    if not uri.strip():
        raise KvmUnavailable("kvm_uri is not configured")
    connection = _libvirt().open(uri)
    if connection is None:
        raise KvmError(f"libvirt connection failed: {uri}")
    return connection


def validate_network(
    name: str,
    *,
    conn: Any | None = None,
    uri: str = "qemu:///system",
    profile: DomainProfile | None = None,
) -> None:
    """Fail closed unless an exact active libvirt network exists.

    Returns immediately for ``user-hostfwd`` profiles: libvirt's network
    driver is not implemented on macOS, so slirp networking has nothing to
    validate.
    """

    if profile is not None and profile.network_mode == "user-hostfwd":
        return
    if not isinstance(name, str) or _NETWORK_NAME_RE.fullmatch(name) is None:
        raise KvmError("libvirt network name must match ^[a-z0-9][a-z0-9_.-]{0,62}$")
    owned_connection = conn is None
    connection = connect(uri) if conn is None else conn
    try:
        try:
            network = connection.networkLookupByName(name)
        except Exception as exc:
            raise KvmError(f"libvirt network does not exist or cannot be inspected: {name}") from exc
        if network is None:
            raise KvmError(f"libvirt network does not exist: {name}")
        try:
            active = network.isActive()
        except Exception as exc:
            raise KvmError(f"cannot determine whether libvirt network is active: {name}") from exc
        if active != 1:
            raise KvmError(f"libvirt network is not active: {name}")
    finally:
        if owned_connection and hasattr(connection, "close"):
            try:
                connection.close()
            except Exception:
                pass


def validate_domain_name_available(
    name: str,
    *,
    conn: Any | None = None,
    uri: str = "qemu:///system",
) -> None:
    """Fail closed unless no libvirt domain currently reserves ``name``."""

    if not _valid_name(name):
        raise KvmError("invalid domain name")
    libvirt = _libvirt()
    owned_connection = conn is None
    connection = connect(uri) if conn is None else conn
    try:
        try:
            domain = connection.lookupByName(name)
        except libvirt.libvirtError as exc:
            error_code = exc.get_error_code() if hasattr(exc, "get_error_code") else None
            if error_code == libvirt.VIR_ERR_NO_DOMAIN:
                return
            raise KvmError(f"cannot determine whether libvirt domain name is available: {name}") from exc
        except Exception as exc:
            raise KvmError(f"cannot determine whether libvirt domain name is available: {name}") from exc
        if domain is not None:
            raise KvmError(f"libvirt domain name is already reserved: {name}")
    finally:
        if owned_connection and hasattr(connection, "close"):
            try:
                connection.close()
            except Exception:
                pass


def define_and_start(conn: Any, name: str, domain_xml: str):
    if not _valid_name(name):
        raise KvmError("invalid domain name")
    libvirt = _libvirt()
    try:
        existing = conn.lookupByName(name)
    except libvirt.libvirtError:
        existing = None
    if existing is not None:
        raise KvmError(f"domain already exists: {name}")
    domain = conn.defineXML(domain_xml)
    if domain is None:
        raise KvmError("domain definition failed")
    domain.create()
    return domain


def destroy_and_undefine(conn: Any, name: str) -> None:
    libvirt = _libvirt()
    try:
        domain = conn.lookupByName(name)
    except libvirt.libvirtError:
        return
    try:
        if domain.isActive():
            domain.destroy()
    except libvirt.libvirtError:
        _logger.warning("failed to destroy domain %s", name, exc_info=True)
    try:
        domain.undefine()
    except libvirt.libvirtError:
        _logger.warning("failed to undefine domain %s", name, exc_info=True)
