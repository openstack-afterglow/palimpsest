"""Libvirt XML and lifecycle primitives with a delayed optional dependency."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cloudinit import BUILD_CHANNEL_NAME
from .digest import normalize_digest

_logger = logging.getLogger(__name__)
_DOMAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_DISK_LETTERS = "bcdefghijklmnopqrstuvwxyz"
MAX_LAYER_DISKS = len(_DISK_LETTERS)
DOMAIN_MARKER_VERSION = "0.1.0"
DOMAIN_MARKER_NAMESPACE = "https://afterglow.dev/palimpsest-local/domain/v1"
ET.register_namespace("palimpsest", DOMAIN_MARKER_NAMESPACE)


class KvmError(RuntimeError):
    """An expected local KVM operational failure."""


class KvmUnavailable(KvmError):
    """The optional libvirt runtime is unavailable."""


@dataclass(frozen=True)
class LayerDisk:
    blob_digest: str
    host_path: Path
    target_dev: str
    serial: str


@dataclass(frozen=True)
class DomainSpec:
    name: str
    memory_mib: int
    vcpus: int
    root_disk: Path
    seed_iso: Path
    layers: list[LayerDisk]
    network: str | None = "default"
    console_log: Path | None = None
    run_id: str | None = None
    guest_agent: bool = True
    control_socket: Path | None = None


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
) -> ET.Element:
    disk = ET.SubElement(devices, "disk", {"type": "file", "device": device})
    driver = {"name": "qemu", "type": disk_format}
    if target == "vda":
        driver["discard"] = "unmap"
    ET.SubElement(disk, "driver", driver)
    ET.SubElement(disk, "source", {"file": str(path)})
    ET.SubElement(disk, "target", {"dev": target, "bus": bus})
    if readonly:
        ET.SubElement(disk, "readonly")
    return disk


def build_domain_xml(spec: DomainSpec) -> str:
    if not _valid_name(spec.name):
        raise KvmError("domain name must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if not 256 <= spec.memory_mib <= 1_048_576:
        raise KvmError("memory_mib is outside the supported range")
    if not 1 <= spec.vcpus <= 256:
        raise KvmError("vcpus is outside the supported range")
    if len(spec.layers) > MAX_LAYER_DISKS:
        raise KvmError("layer count exceeds disk limit")

    domain = ET.Element("domain", {"type": "kvm"})
    ET.SubElement(domain, "name").text = spec.name
    ET.SubElement(domain, "memory", {"unit": "MiB"}).text = str(spec.memory_mib)
    ET.SubElement(domain, "vcpu").text = str(spec.vcpus)
    os_element = ET.SubElement(domain, "os")
    ET.SubElement(os_element, "type", {"arch": "x86_64", "machine": "q35"}).text = "hvm"
    ET.SubElement(os_element, "boot", {"dev": "hd"})
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
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
    ET.SubElement(devices, "emulator").text = "/usr/bin/qemu-system-x86_64"
    _disk(devices, spec.root_disk, "vda", "qcow2", readonly=False)
    seen_serials: set[str] = set()
    for layer in spec.layers:
        if layer.target_dev == "vda" or layer.serial in seen_serials or len(layer.serial) != 20:
            raise KvmError("invalid or colliding layer disk")
        seen_serials.add(layer.serial)
        disk = _disk(devices, layer.host_path, layer.target_dev, "raw", readonly=True)
        ET.SubElement(disk, "serial").text = layer.serial
    _disk(devices, spec.seed_iso, "sda", "raw", readonly=True, device="cdrom", bus="sata")
    if spec.network is not None:
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
    return ET.tostring(domain, encoding="unicode")


def build_seed_iso_command(seed_iso: Path, user_data: Path, meta_data: Path) -> list[str]:
    for label, path in (("seed_iso", seed_iso), ("user_data", user_data), ("meta_data", meta_data)):
        if not path.is_absolute():
            raise KvmError(f"{label} must be an absolute path: {path!s}")
    return ["cloud-localds", str(seed_iso), str(user_data), str(meta_data)]


def build_layer_activation_script(disks: list[LayerDisk], *, merged_dir: str = "/opt/layers/merged") -> str:
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


def _libvirt() -> Any:
    try:
        import libvirt
    except ImportError as exc:
        raise KvmUnavailable("libvirt-python is not installed; install palimpsest-local[kvm]") from exc
    return libvirt


def connect(uri: str):
    if not uri.strip():
        raise KvmUnavailable("kvm_uri is not configured")
    connection = _libvirt().open(uri)
    if connection is None:
        raise KvmError(f"libvirt connection failed: {uri}")
    return connection


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
