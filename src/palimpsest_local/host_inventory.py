"""Bounded read-only host resource observation for virtual machines, disks, and networks.

This module provides host-level discovery of virtualization resources across
supported hypervisor backends (Lima VZ on macOS Apple Silicon, libvirt KVM on Linux,
and libvirt HVF on macOS).

Subprocess queries against command-line daemons (such as ``limactl``) are strictly
bounded by a 5-second deadline per query invocation to avoid blocking API polling loops.
Native in-process libvirt C-binding calls execute read-only queries synchronously over
local hypervisor sockets and handle errors safely through exception isolation.

This module never mutates runtime ledgers, never initiates state transitions or
directory adoptions, and never persists state records.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from . import cloud_runtime, host_vm_control, kvm, lima, platforms
from .errors import StateError
from .runtime_types import RuntimeBackend

_BACKEND_LIMA = platforms.BACKEND_LIMA
_BACKEND_KVM = platforms.BACKEND_KVM
_BACKEND_HVF = platforms.BACKEND_HVF

_COMMAND_TIMEOUT_SECONDS = 5.0
_RUN_ID_ENV = "PALIMPSEST_RUN_ID"

_WARN_LIMA_QUERY_FAILED = "Lima backend observation failed"
_WARN_LIMA_NETWORK_FAILED = "Lima network observation failed"
_WARN_LIMA_DISK_FAILED = "Lima disk observation failed"
_WARN_LIBVIRT_QUERY_FAILED = "libvirt backend observation failed"
_WARN_LIBVIRT_NETWORK_FAILED = "libvirt network observation failed"
_WARN_LIBVIRT_DISK_FAILED = "libvirt disk observation failed"

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]+)?$")
_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1000,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1000 * 1000,
    "mib": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1000 * 1000 * 1000,
    "gib": 1024 * 1024 * 1024,
    "t": 1024 * 1024 * 1024 * 1024,
    "tb": 1000 * 1000 * 1000 * 1000,
    "tib": 1024 * 1024 * 1024 * 1024,
}


def _parse_bytes(value: Any) -> int | None:
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    raw = value.strip()
    match = _SIZE_RE.match(raw)
    if not match:
        return None
    num_str, unit_str = match.groups()
    unit = unit_str.lower() if unit_str else "b"
    multiplier = _UNITS.get(unit)
    if multiplier is None:
        return None
    try:
        return int(float(num_str) * multiplier)
    except (ValueError, OverflowError):
        return None


def _validate_port(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    host_ip_raw = raw.get("hostIP") or raw.get("host_ip") or "127.0.0.1"
    if not isinstance(host_ip_raw, str):
        return None
    try:
        host_ip = str(ipaddress.ip_address(host_ip_raw))
    except ValueError:
        return None
    host_port = raw.get("hostPort") or raw.get("host_port")
    guest_port = raw.get("guestPort") or raw.get("guest_port")
    if not isinstance(host_port, int) or not (1 <= host_port <= 65535):
        return None
    if not isinstance(guest_port, int) or not (1 <= guest_port <= 65535):
        return None
    proto = raw.get("proto") or raw.get("protocol") or "tcp"
    if not isinstance(proto, str) or proto.lower() not in {"tcp", "udp"}:
        return None
    return {
        "host_ip": host_ip,
        "host_port": host_port,
        "guest_port": guest_port,
        "protocol": proto.lower(),
    }


def _safe_instance_run_id(instance: dict[str, Any]) -> str | None:
    config = instance.get("config")
    env = config.get("env") if isinstance(config, dict) else None
    val = env.get(_RUN_ID_ENV) if isinstance(env, dict) else None
    if not isinstance(val, str):
        return None
    try:
        parsed = uuid.UUID(val)
        canonical = str(parsed)
        if canonical == val:
            return val
    except Exception:
        pass
    return None


def _safe_instance_status(instance: dict[str, Any]) -> str:
    val = instance.get("status")
    if not isinstance(val, str):
        return "unknown"
    statuses = {
        "Running": "running",
        "Stopped": "stopped",
        "Broken": "failed",
        "Starting": "starting",
        "Stopping": "stopping",
    }
    return statuses.get(val, val.lower())


def _safe_lima_disk_stat(dir_path: str | None) -> tuple[int | None, int | None, str]:
    if not isinstance(dir_path, str) or not dir_path or not os.path.isabs(dir_path):
        return None, None, "unavailable"
    # Never follow symlinks; inspect directory entries with os.lstat
    for candidate_name in ("disk", "diffdisk", "basedisk"):
        candidate_path = os.path.join(dir_path, candidate_name)
        try:
            st = os.lstat(candidate_path)
            # S_ISREG on lstat returns False if candidate_path is a symlink
            if stat_module.S_ISREG(st.st_mode):
                allocated = st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
                return st.st_size, allocated, "attached"
        except (OSError, ValueError):
            continue
    return None, None, "unavailable"


def _decode_json_objects(output: str, context: str) -> list[dict[str, Any]]:
    raw = output.strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, list):
            items = [x for x in decoded if isinstance(x, dict)]
            if len(items) != len(decoded):
                raise ValueError(f"Lima {context} list contains non-object items")
            return items
        if isinstance(decoded, dict):
            return [decoded]
        raise ValueError(f"Lima {context} JSON is not an object or list")
    except json.JSONDecodeError:
        pass
    results = []
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in lines:
        try:
            val = json.loads(line)
            if isinstance(val, dict):
                results.append(val)
            else:
                raise ValueError(f"Lima {context} JSON line is not an object")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Lima {context} returned invalid JSON: {exc}") from exc
    if not results and lines:
        raise ValueError(f"Lima {context} returned non-empty unparseable JSON")
    return results


def _observe_lima() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    vms: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    networks_by_id: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    if shutil.which("limactl") is None:
        return vms, volumes, list(networks_by_id.values()), warnings

    # 1. Query Lima instances
    try:
        proc = subprocess.run(
            ["limactl", "list", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError):
        warnings.append(_WARN_LIMA_QUERY_FAILED)
        return vms, volumes, list(networks_by_id.values()), warnings

    if proc.returncode != 0:
        warnings.append(_WARN_LIMA_QUERY_FAILED)
        return vms, volumes, list(networks_by_id.values()), warnings

    try:
        instances = _decode_json_objects(proc.stdout, "instance")
    except ValueError:
        warnings.append(_WARN_LIMA_QUERY_FAILED)
        return vms, volumes, list(networks_by_id.values()), warnings

    # 2. Query Lima networks
    try:
        net_proc = subprocess.run(
            ["limactl", "network", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        if net_proc.returncode == 0:
            try:
                nets = _decode_json_objects(net_proc.stdout, "network")
                for net in nets:
                    net_name = net.get("name")
                    if not isinstance(net_name, str) or not net_name:
                        continue
                    net_id = f"network:lima:{net_name}"
                    mode = net.get("mode") if isinstance(net.get("mode"), str) else None
                    gateway = net.get("gateway") if isinstance(net.get("gateway"), str) else None
                    netmask = net.get("netmask") if isinstance(net.get("netmask"), str) else None
                    subnet = None
                    if gateway and netmask:
                        try:
                            subnet = str(ipaddress.IPv4Network(f"{gateway}/{netmask}", strict=False))
                        except ValueError:
                            pass
                    networks_by_id[net_id] = {
                        "id": net_id,
                        "name": net_name,
                        "kind": "lima",
                        "backend": "lima",
                        "mode": mode,
                        "subnet": subnet,
                        "gateway": gateway,
                        "status": "configured",
                        "source": "host-backend",
                        "external": None,
                        "attachments": [],
                    }
            except ValueError:
                warnings.append(_WARN_LIMA_NETWORK_FAILED)
        else:
            warnings.append(_WARN_LIMA_NETWORK_FAILED)
    except (subprocess.SubprocessError, OSError):
        warnings.append(_WARN_LIMA_NETWORK_FAILED)

    # 3. Query Lima additional disks
    additional_disks_by_name: dict[str, dict[str, Any]] = {}
    try:
        disk_proc = subprocess.run(
            ["limactl", "disk", "list", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        if disk_proc.returncode == 0:
            try:
                disks = _decode_json_objects(disk_proc.stdout, "disk")
                for disk in disks:
                    d_name = disk.get("name")
                    if isinstance(d_name, str) and d_name:
                        additional_disks_by_name[d_name] = disk
            except ValueError:
                warnings.append(_WARN_LIMA_DISK_FAILED)
        else:
            warnings.append(_WARN_LIMA_DISK_FAILED)
    except (subprocess.SubprocessError, OSError):
        warnings.append(_WARN_LIMA_DISK_FAILED)

    # 4. Map instances, primary disks, and attachments
    for instance in instances:
        name = instance.get("name")
        if not isinstance(name, str) or not name:
            continue
        config = instance.get("config") if isinstance(instance.get("config"), dict) else {}
        try:
            native_backend = lima.instance_backend(instance)
        except StateError:
            warnings.append(_WARN_LIMA_QUERY_FAILED)
            continue
        vm_type = instance.get("vmType") or config.get("vmType")
        if native_backend is None and vm_type != "qemu":
            warnings.append(_WARN_LIMA_QUERY_FAILED)
            continue
        backend = native_backend.value if native_backend is not None else "lima-qemu"
        instance_backend = backend
        run_id = _safe_instance_run_id(instance)
        runtime_kind = "cloud-image" if run_id is not None else "external"
        status = _safe_instance_status(instance)

        mem_raw = instance.get("memory") if instance.get("memory") is not None else config.get("memory")
        mem_bytes = _parse_bytes(mem_raw)
        memory_mib = (mem_bytes // (1024 * 1024)) if mem_bytes is not None else None

        cpus_raw = instance.get("cpus") if instance.get("cpus") is not None else config.get("cpus")
        vcpus = cpus_raw if isinstance(cpus_raw, int) else None

        arch_raw = instance.get("arch") if instance.get("arch") is not None else config.get("arch")
        base_arch = str(arch_raw) if isinstance(arch_raw, str) else None

        ssh_port = instance.get("sshLocalPort")
        ssh_addr = instance.get("sshAddress") if isinstance(instance.get("sshAddress"), str) else "127.0.0.1"
        ssh = (
            {"host": ssh_addr, "port": ssh_port}
            if isinstance(ssh_port, int) and ssh_port > 0
            else {"host": None, "port": 22}
        )

        raw_ports = config.get("portForwards") if isinstance(config.get("portForwards"), list) else []
        ports_list = []
        for p in raw_ports:
            vp = _validate_port(p)
            if vp is not None and vp not in ports_list:
                ports_list.append(vp)
        ports_list.sort(key=lambda p: (p["host_ip"], p["host_port"], p["guest_port"], p["protocol"]))

        config_nets = config.get("networks") if isinstance(config.get("networks"), list) else []
        inst_nets = instance.get("network") if isinstance(instance.get("network"), list) else []
        all_nets = config_nets or inst_nets
        has_vznat = any(isinstance(n, dict) and (n.get("vzNAT") is True or n.get("vzNAT") == "true") for n in all_nets)
        lima_named_nets = [n.get("lima") for n in all_nets if isinstance(n, dict) and isinstance(n.get("lima"), str)]

        if has_vznat:
            primary_net = "vzNAT" if backend == _BACKEND_LIMA else "unknown"
        elif lima_named_nets:
            primary_net = f"lima:{lima_named_nets[0]}"
        elif backend == platforms.BACKEND_LIMA_KRUNKIT:
            primary_net = "default"
        else:
            primary_net = "none"

        raw_add_disks = config.get("additionalDisks") if isinstance(config.get("additionalDisks"), list) else []
        vm_volumes = []
        for disk_spec in raw_add_disks:
            if isinstance(disk_spec, dict) and isinstance(disk_spec.get("name"), str):
                dname = disk_spec["name"]
                vm_volumes.append(
                    {
                        "name": dname,
                        "backend_name": dname,
                        "mount_path": "",
                        "read_only": False,
                    }
                )

        vm_row = {
            "id": f"host:{instance_backend}:{name}",
            "name": name,
            "run_id": run_id,
            "removal_identity": host_vm_control.lima_removal_identity(instance),
            "backend": instance_backend,
            "runtime_kind": runtime_kind,
            "status": status,
            "stale": False,
            "base_digest": None,
            "base_arch": base_arch,
            "layers": [],
            "layer_count": 0,
            "memory_mib": memory_mib,
            "vcpus": vcpus,
            "network": primary_net,
            "ports": ports_list,
            "volumes": vm_volumes,
            "ssh": ssh,
            "guest_ip": None,
            "project": None,
            "created_at": None,
            "updated_at": None,
            "managed": False,
            "source": "host-backend",
        }
        vms.append(vm_row)
        if backend == platforms.BACKEND_LIMA_KRUNKIT and primary_net == "default":
            net_id = f"network:{backend}:{run_id or 'instance'}:{name}"
            networks_by_id[net_id] = {
                "id": net_id,
                "name": "default",
                "kind": "user-mode",
                "backend": backend,
                "mode": "gvisor",
                "subnet": None,
                "gateway": None,
                "status": "configured",
                "source": "host-backend",
                "external": None,
                "attachments": [{"name": name, "run_id": run_id, "guest_ip": None, "ports": ports_list}],
            }

        # Synthesize vzNAT network only when an attached VM actually configures vzNAT
        if has_vznat and backend == _BACKEND_LIMA:
            vznat_id = f"network:{_BACKEND_LIMA}:vzNAT"
            if vznat_id not in networks_by_id:
                networks_by_id[vznat_id] = {
                    "id": vznat_id,
                    "name": "vzNAT",
                    "kind": "lima",
                    "backend": _BACKEND_LIMA,
                    "mode": "vzNAT",
                    "subnet": None,
                    "gateway": None,
                    "status": "configured",
                    "source": "host-backend",
                    "external": None,
                    "attachments": [],
                }
            networks_by_id[vznat_id]["attachments"].append(
                {
                    "name": name,
                    "run_id": run_id,
                    "guest_ip": None,
                    "ports": ports_list,
                }
            )

        for lnet in lima_named_nets:
            lid = f"network:lima:{lnet}"
            if lid not in networks_by_id:
                networks_by_id[lid] = {
                    "id": lid,
                    "name": lnet,
                    "kind": "lima",
                    "backend": "lima",
                    "mode": None,
                    "subnet": None,
                    "gateway": None,
                    "status": "configured",
                    "source": "host-backend",
                    "external": None,
                    "attachments": [],
                }
            networks_by_id[lid]["attachments"].append(
                {
                    "name": name,
                    "run_id": run_id,
                    "guest_ip": None,
                    "ports": ports_list,
                }
            )

        # Primary disk for VM: distinct :primary suffix prevents collision with additional volumes
        disk_raw = instance.get("disk") if instance.get("disk") is not None else config.get("disk")
        capacity = _parse_bytes(disk_raw)
        file_size, allocated, disk_status = _safe_lima_disk_stat(instance.get("dir"))
        # Never fallback capacity = file_size: file_size is apparent byte length, not virtual capacity

        primary_disk = {
            "id": f"disk:{backend}:{name}:primary",
            "name": name,
            "kind": "vm-disk",
            "backend": backend,
            "project": None,
            "status": disk_status,
            "size_bytes": capacity,
            "file_size_bytes": file_size,
            "allocated_bytes": allocated,
            "source": "host-backend",
            "attachments": [
                {
                    "name": name,
                    "run_id": run_id,
                    "mount_path": "/",
                    "read_only": False,
                }
            ],
            "retention_policy": None,
        }
        volumes.append(primary_disk)

    # 5. Process additional Lima disks in the volume: namespace
    for d_name, d_info in additional_disks_by_name.items():
        disk_id = f"volume:lima:{d_name}"
        capacity = _parse_bytes(d_info.get("size"))
        file_size = None
        allocated = None
        disk_status = "detached"

        dir_path = d_info.get("dir")
        if isinstance(dir_path, str) and dir_path and os.path.isabs(dir_path):
            datadisk_path = os.path.join(dir_path, "datadisk")
            try:
                st = os.lstat(datadisk_path)
                if stat_module.S_ISREG(st.st_mode):
                    file_size = st.st_size
                    allocated = st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
            except (OSError, ValueError):
                pass

        disk_attachments = []
        for vm in vms:
            for vol in vm["volumes"]:
                if vol["name"] == d_name:
                    disk_attachments.append(
                        {
                            "name": vm["name"],
                            "run_id": vm["run_id"],
                            "mount_path": vol["mount_path"],
                            "read_only": vol["read_only"],
                        }
                    )
        if disk_attachments:
            disk_status = "attached"

        volumes.append(
            {
                "id": disk_id,
                "name": d_name,
                "kind": "volume",
                "backend": "lima",
                "project": None,
                "status": disk_status,
                "size_bytes": capacity,
                "file_size_bytes": file_size,
                "allocated_bytes": allocated,
                "source": "host-backend",
                "attachments": disk_attachments,
                "retention_policy": None,
            }
        )

    return vms, volumes, list(networks_by_id.values()), warnings


def _observe_libvirt() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    vms: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    networks_by_id: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    attached_paths: set[str] = set()

    try:
        import libvirt

        try:
            libvirt.registerErrorHandler(lambda ctx, err: None, None)
        except Exception:
            pass
    except ImportError:
        # libvirt is not installed: clean return without warning
        return vms, volumes, list(networks_by_id.values()), warnings
    host = platforms.detect_host()
    if host.system == "Linux":
        backend = _BACKEND_KVM
        uri = cloud_runtime._RECONCILE_URIS[RuntimeBackend.KVM]
    elif host.system == "Darwin":
        backend = _BACKEND_HVF
        uri = cloud_runtime._RECONCILE_URIS[RuntimeBackend.LIBVIRT_HVF]
    else:
        return vms, volumes, list(networks_by_id.values()), warnings

    # libvirt is installed; connection failure must record a safe warning
    try:
        conn = libvirt.openReadOnly(uri)
    except Exception:
        warnings.append(_WARN_LIBVIRT_QUERY_FAILED)
        return vms, volumes, list(networks_by_id.values()), warnings

    if conn is None:
        warnings.append(_WARN_LIBVIRT_QUERY_FAILED)
        return vms, volumes, list(networks_by_id.values()), warnings

    try:
        # 1. Networks
        try:
            raw_networks = conn.listAllNetworks()
            for net in raw_networks:
                try:
                    net_name = net.name()
                    if not isinstance(net_name, str) or not net_name:
                        continue
                    net_id = f"network:{backend}:{net_name}"
                    is_active = bool(net.isActive())
                    xml_desc = net.XMLDesc(0)
                    mode = None
                    gateway = None
                    subnet = None
                    try:
                        net_tree = ET.fromstring(xml_desc)
                        fwd = net_tree.find("forward")
                        if fwd is not None:
                            mode = fwd.get("mode")
                        ip_elem = net_tree.find("ip")
                        if ip_elem is not None:
                            gateway = ip_elem.get("address")
                            netmask = ip_elem.get("netmask")
                            if gateway and netmask:
                                try:
                                    subnet = str(ipaddress.IPv4Network(f"{gateway}/{netmask}", strict=False))
                                except ValueError:
                                    pass
                    except Exception:
                        pass
                    networks_by_id[net_id] = {
                        "id": net_id,
                        "name": net_name,
                        "kind": "libvirt",
                        "backend": backend,
                        "mode": mode,
                        "subnet": subnet,
                        "gateway": gateway,
                        "status": "configured",
                        "source": "host-backend",
                        "external": None,
                        "attachments": [],
                    }
                except Exception:
                    warnings.append(_WARN_LIBVIRT_NETWORK_FAILED)
                    continue
        except Exception:
            warnings.append(_WARN_LIBVIRT_NETWORK_FAILED)

        # 2. Domains
        try:
            raw_domains = conn.listAllDomains()
            for dom in raw_domains:
                try:
                    dom_name = dom.name()
                    if not isinstance(dom_name, str) or not dom_name:
                        continue
                    is_active = bool(dom.isActive())
                    try:
                        state_code, _ = dom.state()
                        state_map = {
                            1: "running",
                            2: "paused",
                            3: "paused",
                            4: "stopping",
                            5: "stopped",
                            6: "failed",
                            7: "paused",
                        }
                        status = state_map.get(state_code, "running" if is_active else "stopped")
                    except Exception:
                        status = "running" if is_active else "stopped"

                    run_id = None
                    try:
                        marker_id = kvm.get_domain_run_id(dom)
                        if isinstance(marker_id, str):
                            parsed = uuid.UUID(marker_id)
                            if str(parsed) == marker_id:
                                run_id = marker_id
                    except Exception:
                        pass

                    runtime_kind = "cloud-image" if run_id is not None else "external"

                    info = None
                    try:
                        info = dom.info()
                    except Exception:
                        pass
                    memory_mib = int(info[2] // 1024) if info and len(info) > 2 else None
                    vcpus = int(info[3]) if info and len(info) > 3 else None

                    base_arch = None
                    xml_desc = None
                    try:
                        xml_desc = dom.XMLDesc(0)
                    except Exception:
                        pass

                    dom_tree = None
                    if xml_desc:
                        try:
                            dom_tree = ET.fromstring(xml_desc)
                            markers = dom_tree.findall(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
                            run_id = kvm._validated_domain_marker_run_id(markers[0]) if len(markers) == 1 else None
                            runtime_kind = "cloud-image" if run_id is not None else "external"
                            if run_id and len(markers) == 1 and markers[0].get("id") == run_id:
                                contract = markers[0].get("contract", "")
                                if backend == _BACKEND_KVM and re.fullmatch(r"sha256:[0-9a-f]{64}", contract):
                                    runtime_kind = "oci-root"
                            type_elem = dom_tree.find("./os/type")
                            if type_elem is not None:
                                base_arch = type_elem.get("arch")
                        except Exception:
                            pass

                    dom_primary_net = "none"
                    dom_disks: list[dict[str, Any]] = []
                    if dom_tree is not None:
                        devices = dom_tree.find("devices")
                        if devices is not None:
                            for disk_elem in devices.findall("disk"):
                                disk_device = disk_elem.get("device", "disk")
                                if disk_device != "disk":
                                    continue
                                source_elem = disk_elem.find("source")
                                target_elem = disk_elem.find("target")
                                readonly_elem = disk_elem.find("readonly")
                                is_readonly = readonly_elem is not None
                                file_path = source_elem.get("file") if source_elem is not None else None
                                if isinstance(file_path, str) and os.path.isabs(file_path):
                                    attached_paths.add(os.path.normpath(file_path))
                                target_dev = target_elem.get("dev") if target_elem is not None else None

                                # Virtual capacity from blockInfo, never file length
                                disk_capacity = None
                                disk_allocated = None
                                missing_local_disk = False
                                if not is_active and file_path and os.path.isabs(file_path):
                                    try:
                                        os.lstat(file_path)
                                    except FileNotFoundError:
                                        missing_local_disk = True
                                    except (OSError, ValueError):
                                        pass  # The libvirt daemon may still have access.
                                if target_dev and hasattr(dom, "blockInfo") and not missing_local_disk:
                                    try:
                                        binfo = dom.blockInfo(target_dev, 0)
                                        if isinstance(binfo, (tuple, list)) and len(binfo) >= 2:
                                            if isinstance(binfo[0], int) and binfo[0] > 0:
                                                disk_capacity = binfo[0]
                                            if isinstance(binfo[1], int) and binfo[1] >= 0:
                                                disk_allocated = binfo[1]
                                    except Exception:
                                        pass
                                file_size = None
                                disk_status = "unavailable"
                                if file_path and os.path.isabs(file_path):
                                    try:
                                        st = os.lstat(file_path)
                                        if stat_module.S_ISREG(st.st_mode):
                                            file_size = st.st_size
                                            if hasattr(st, "st_blocks"):
                                                disk_allocated = st.st_blocks * 512
                                            disk_status = "attached"
                                    except (OSError, ValueError):
                                        pass

                                dom_disks.append(
                                    {
                                        "target_dev": target_dev,
                                        "file_path": file_path,
                                        "capacity": disk_capacity,
                                        "file_size": file_size,
                                        "allocated": disk_allocated,
                                        "is_readonly": is_readonly,
                                        "status": disk_status,
                                    }
                                )

                            for iface_elem in devices.findall("interface"):
                                iface_type = iface_elem.get("type")
                                if iface_type == "network":
                                    source = iface_elem.find("source")
                                    if source is not None and source.get("network"):
                                        net_name = source.get("network")
                                        dom_primary_net = net_name
                                        net_id = f"network:{backend}:{net_name}"
                                        if net_id not in networks_by_id:
                                            networks_by_id[net_id] = {
                                                "id": net_id,
                                                "name": net_name,
                                                "kind": "libvirt",
                                                "backend": backend,
                                                "mode": None,
                                                "subnet": None,
                                                "gateway": None,
                                                "status": "configured",
                                                "source": "host-backend",
                                                "external": None,
                                                "attachments": [],
                                            }
                                        networks_by_id[net_id]["attachments"].append(
                                            {
                                                "name": dom_name,
                                                "run_id": run_id,
                                                "guest_ip": None,
                                                "ports": [],
                                            }
                                        )
                                elif iface_type == "user":
                                    dom_primary_net = "user"

                    # Identify exactly ONE primary disk (known boot/vda/sda if present, else index 0)
                    primary_idx = 0
                    for idx, d_cand in enumerate(dom_disks):
                        if d_cand["target_dev"] in {"vda", "sda"}:
                            primary_idx = idx
                            break

                    vm_row = {
                        "id": f"host:{backend}:{dom_name}",
                        "name": dom_name,
                        "run_id": run_id,
                        "backend": backend,
                        "runtime_kind": runtime_kind,
                        "status": status,
                        "stale": False,
                        "base_digest": None,
                        "base_arch": base_arch,
                        "layers": [],
                        "layer_count": 0,
                        "memory_mib": memory_mib,
                        "vcpus": vcpus,
                        "network": dom_primary_net,
                        "ports": [],
                        "volumes": [],
                        "ssh": {"host": None, "port": 22},
                        "guest_ip": None,
                        "project": None,
                        "created_at": None,
                        "updated_at": None,
                        "managed": False,
                        "source": "host-backend",
                        "removal_identity": host_vm_control.libvirt_removal_identity(dom, definition=dom_tree),
                    }
                    vms.append(vm_row)

                    for idx, d in enumerate(dom_disks):
                        is_primary = idx == primary_idx
                        if is_primary:
                            disk_id = f"disk:{backend}:{dom_name}:primary"
                            disk_name = dom_name
                            disk_kind = "vm-disk"
                            mount_path = "/"
                        else:
                            # Use internal normalized path digest to avoid path leaks while sharing identity
                            if d["file_path"]:
                                norm_p = os.path.normpath(d["file_path"])
                                p_digest = hashlib.sha256(norm_p.encode()).hexdigest()[:12]
                                disk_id = f"volume:{backend}:{p_digest}"
                            else:
                                disk_id = f"volume:{backend}:{dom_name}:{d['target_dev'] or f'disk-{idx}'}"
                            disk_name = f"{dom_name}-{d['target_dev'] or f'disk-{idx}'}"
                            disk_kind = "volume"
                            mount_path = ""

                        volumes.append(
                            {
                                "id": disk_id,
                                "name": disk_name,
                                "kind": disk_kind,
                                "backend": backend,
                                "project": None,
                                "status": d["status"],
                                "size_bytes": d["capacity"],
                                "file_size_bytes": d["file_size"],
                                "allocated_bytes": d["allocated"],
                                "source": "host-backend",
                                "attachments": [
                                    {
                                        "name": dom_name,
                                        "run_id": run_id,
                                        "mount_path": mount_path,
                                        "read_only": d["is_readonly"],
                                    }
                                ],
                                "retention_policy": None,
                            }
                        )

                except Exception:
                    warnings.append(_WARN_LIBVIRT_QUERY_FAILED)
                    continue
        except Exception:
            warnings.append(_WARN_LIBVIRT_QUERY_FAILED)

        # 3. Storage pools (support unattached pool volumes where available)
        try:
            if hasattr(conn, "listAllStoragePools"):
                pools = conn.listAllStoragePools()
                for pool in pools:
                    try:
                        if not pool.isActive():
                            continue
                        for vol in pool.listAllVolumes():
                            try:
                                vol_name = vol.name()
                                vol_path = vol.path() if hasattr(vol, "path") else None
                                if vol_path:
                                    norm_vp = os.path.normpath(vol_path)
                                    if norm_vp in attached_paths:
                                        continue
                                    v_digest = hashlib.sha256(norm_vp.encode()).hexdigest()[:12]
                                    vol_id = f"volume:{backend}:{v_digest}"
                                else:
                                    vol_id = f"volume:{backend}:{vol_name}"

                                if any(v["id"] == vol_id for v in volumes):
                                    continue

                                vol_cap = None
                                vol_alloc = None
                                try:
                                    vinfo = vol.info()
                                    if isinstance(vinfo, (tuple, list)) and len(vinfo) >= 2:
                                        vol_cap = vinfo[1]
                                        vol_alloc = vinfo[2] if len(vinfo) > 2 else None
                                except Exception:
                                    pass

                                v_file_size = None
                                if vol_path and os.path.isabs(vol_path):
                                    try:
                                        st = os.lstat(vol_path)
                                        if stat_module.S_ISREG(st.st_mode):
                                            v_file_size = st.st_size
                                            if hasattr(st, "st_blocks"):
                                                vol_alloc = st.st_blocks * 512
                                    except Exception:
                                        pass

                                volumes.append(
                                    {
                                        "id": vol_id,
                                        "name": vol_name,
                                        "kind": "volume",
                                        "backend": backend,
                                        "project": None,
                                        "status": "detached",
                                        "size_bytes": vol_cap,
                                        "file_size_bytes": v_file_size,
                                        "allocated_bytes": vol_alloc,
                                        "source": "host-backend",
                                        "attachments": [],
                                        "retention_policy": None,
                                    }
                                )
                            except Exception:
                                warnings.append(_WARN_LIBVIRT_DISK_FAILED)
                                continue
                    except Exception:
                        warnings.append(_WARN_LIBVIRT_DISK_FAILED)
                        continue
        except Exception:
            warnings.append(_WARN_LIBVIRT_DISK_FAILED)

    finally:
        try:
            conn.close()
        except Exception:
            pass

    return vms, volumes, list(networks_by_id.values()), warnings


def observe() -> dict[str, Any]:
    """Observe live host virtual machines, disks, and networks.

    Returns:
        dict containing:
            - "vms": list of canonical VM rows with stable host IDs and managed: False.
            - "volumes": list of host disks and volumes.
            - "networks": list of host backend networks and VM attachments.
            - "warnings": list of safe warning summaries on backend query failure.
    """
    vms: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    networks: list[dict[str, Any]] = []
    warnings: list[str] = []

    # 1. Lima observation
    lima_vms, lima_vols, lima_nets, lima_warns = _observe_lima()
    vms.extend(lima_vms)
    volumes.extend(lima_vols)
    networks.extend(lima_nets)
    warnings.extend(lima_warns)

    # 2. Libvirt observation
    lv_vms, lv_vols, lv_nets, lv_warns = _observe_libvirt()
    vms.extend(lv_vms)
    volumes.extend(lv_vols)
    networks.extend(lv_nets)
    warnings.extend(lv_warns)

    # Deduplicate volumes by ID and merge attachments
    vols_by_id: dict[str, dict[str, Any]] = {}
    for vol in volumes:
        vid = vol["id"]
        if vid not in vols_by_id:
            vols_by_id[vid] = vol
        else:
            existing = vols_by_id[vid]
            existing["attachments"].extend(vol["attachments"])
            if existing["size_bytes"] is None and vol["size_bytes"] is not None:
                existing["size_bytes"] = vol["size_bytes"]
            if existing["file_size_bytes"] is None and vol["file_size_bytes"] is not None:
                existing["file_size_bytes"] = vol["file_size_bytes"]
            if existing["allocated_bytes"] is None and vol["allocated_bytes"] is not None:
                existing["allocated_bytes"] = vol["allocated_bytes"]
            if existing["status"] == "detached" and vol["status"] == "attached":
                existing["status"] = "attached"

    for vol in vols_by_id.values():
        deduped_att = {}
        for att in vol["attachments"]:
            key = (att["name"], att["run_id"] or "", att["mount_path"] or "", att["read_only"])
            deduped_att[key] = att
        vol["attachments"] = [deduped_att[k] for k in sorted(deduped_att)]

    # Deduplicate networks by ID and merge attachments
    nets_by_id: dict[str, dict[str, Any]] = {}
    for net in networks:
        nid = net["id"]
        if nid not in nets_by_id:
            nets_by_id[nid] = net
        else:
            existing = nets_by_id[nid]
            existing["attachments"].extend(net["attachments"])
            if existing["subnet"] is None and net["subnet"] is not None:
                existing["subnet"] = net["subnet"]
            if existing["gateway"] is None and net["gateway"] is not None:
                existing["gateway"] = net["gateway"]
            if existing["mode"] is None and net["mode"] is not None:
                existing["mode"] = net["mode"]

    for net in nets_by_id.values():
        deduped_att = {}
        for att in net["attachments"]:
            key = (att["name"], att["run_id"] or "")
            deduped_att[key] = att
        net["attachments"] = [deduped_att[k] for k in sorted(deduped_att)]

    vms.sort(key=lambda x: x["name"])
    final_volumes = [vols_by_id[k] for k in sorted(vols_by_id)]
    final_networks = [nets_by_id[k] for k in sorted(nets_by_id)]

    unique_warnings: list[str] = []
    seen_w: set[str] = set()
    for w in warnings:
        if w not in seen_w:
            seen_w.add(w)
            unique_warnings.append(w)

    return {
        "vms": vms,
        "volumes": final_volumes,
        "networks": final_networks,
        "warnings": unique_warnings,
    }
