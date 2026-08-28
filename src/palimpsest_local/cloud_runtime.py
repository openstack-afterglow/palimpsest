"""Local KVM domain lifecycle, state ledger management, and guest control."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import cloudinit, guest, kvm, platforms, state
from .digest import digest_file, require_file_digest
from .errors import (
    ArtifactValidationError,
    DigestMismatchError,
    LifecycleError,
    StateError,
)
from .oci_layout import MEDIA_TYPE_LAYER_SQUASHFS, ContentStore
from .refs import ImageRef, RunSpec
from .runtime_types import ExistingRunRecord
from .state import OwnerRecord, RunPaths, StatePaths, TagRecord

_logger = logging.getLogger(__name__)


def _get_conn(conn: Any | None, kvm_uri: str) -> Any:
    if conn is not None:
        return conn
    try:
        return kvm.connect(kvm_uri)
    except kvm.KvmError as exc:
        raise LifecycleError(str(exc)) from exc


_LIBVIRT_BACKENDS = (platforms.BACKEND_KVM, platforms.BACKEND_HVF)


def _resolve_ledger_profile(ledger: dict[str, Any]) -> platforms.DomainProfile:
    """Rebuild the domain profile an existing run's ledger was created under."""
    backend = ledger.get("backend", platforms.BACKEND_KVM)
    arch = ledger.get("base", {}).get("arch", "x86_64")
    return platforms.resolve_domain_profile(backend, arch)


def _resolve_new_run_profile(
    arch: str,
    *,
    kvm_uri: str | None,
    profile: platforms.DomainProfile | None,
    conn: Any | None,
) -> tuple[platforms.DomainProfile, str]:
    """Resolve ``(profile, kvm_uri)`` for defining a brand-new libvirt domain.

    A caller that already supplies ``conn`` or ``kvm_uri`` has taken
    responsibility for connecting, so the domain shape is derived from
    ``arch`` alone with no host detection or tool preflight -- this keeps
    legacy direct and test callers working unchanged. A bare call (no
    ``conn``, ``kvm_uri``, or ``profile``) goes through full host-capability
    auto-selection, matching what ``palimpsest run`` does by default.
    """
    if profile is not None:
        return profile, kvm_uri if kvm_uri is not None else profile.uri
    if kvm_uri is not None or conn is not None:
        resolved = platforms.resolve_domain_profile(platforms.BACKEND_KVM, arch)
        return resolved, kvm_uri if kvm_uri is not None else resolved.uri
    backend = platforms.select_backend(arch)
    platforms.preflight(backend)
    resolved = platforms.resolve_domain_profile(backend, arch)
    return resolved, resolved.uri


def _ssh_endpoint(name: str, st: dict[str, Any]) -> tuple[str, int]:
    """The reachable SSH ``(host, port)`` for a run, tolerating pre-migration ledgers."""
    ssh = st.get("ssh")
    if isinstance(ssh, dict) and ssh.get("host"):
        return ssh["host"], ssh.get("port", 22)
    guest_ip = st.get("guest_ip")
    if guest_ip:
        return guest_ip, 22
    raise LifecycleError(f"run '{name}' has no reachable SSH endpoint")


def _is_missing_domain_error(exc: Exception) -> bool:
    # In production libvirt reports an explicit error code.  ``KeyError`` is
    # retained for the small in-memory connection used by contract tests.
    if isinstance(exc, KeyError):
        return True
    try:
        libvirt = kvm._libvirt()
        return isinstance(exc, libvirt.libvirtError) and exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN
    except Exception:
        return False


def _destroy_and_undefine_domain(domain: Any) -> None:
    """Safely destroy and undefine a domain object without importing libvirt."""
    try:
        is_act = domain.isActive() if hasattr(domain, "isActive") else False
        if is_act and hasattr(domain, "destroy"):
            domain.destroy()
    except Exception:
        _logger.warning("failed to destroy domain", exc_info=True)
    try:
        if hasattr(domain, "undefine"):
            domain.undefine()
    except Exception:
        _logger.warning("failed to undefine domain", exc_info=True)


def _validate_run_ledger(rpaths: RunPaths) -> tuple[OwnerRecord, dict[str, Any]]:
    """Validate owner/state integrity boundaries and return (owner_record, state_dict)."""
    if not rpaths.owner.exists():
        raise StateError(f"run '{rpaths.name}' does not exist")

    try:
        owner_rec = state.read_owner_record(rpaths)
    except Exception as exc:
        raise StateError(f"corrupt owner record for run '{rpaths.name}': {exc}") from exc

    if owner_rec.schema_version != 1:
        raise StateError(f"unsupported owner schema version: {owner_rec.schema_version}")

    if owner_rec.name != rpaths.name:
        raise StateError(f"owner record name mismatch: '{owner_rec.name}' != '{rpaths.name}'")

    try:
        uuid.UUID(owner_rec.run_id)
    except ValueError as exc:
        raise StateError(f"invalid owner run_id UUID '{owner_rec.run_id}' for run '{rpaths.name}'") from exc

    st_data: dict[str, Any] = {}
    if rpaths.state.exists():
        try:
            st_data = state.read_run_state(rpaths)
        except Exception as exc:
            raise StateError(f"corrupt mutable state for run '{rpaths.name}': {exc}") from exc

        status = st_data.get("status")
        if status not in state._STATUSES:
            raise StateError(f"invalid or unknown run status '{status}' for run '{rpaths.name}'")

        st_run_id = st_data.get("run_id")
        if st_run_id is not None and st_run_id != owner_rec.run_id:
            raise StateError(
                f"state run_id mismatch for run '{rpaths.name}': state has '{st_run_id}', owner has '{owner_rec.run_id}'"
            )

    return owner_rec, st_data


def _run_cmd(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise LifecycleError(f"required tool not found: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        raise LifecycleError(f"command {' '.join(argv)} failed: {stderr}") from exc


def create_and_validate_overlay(base: ImageRef, overlay_path: Path) -> None:
    """Create a qcow2 overlay backed by base.local_path and validate it with qemu-img."""
    try:
        require_file_digest(base.local_path, base.digest)
    except Exception as exc:
        raise DigestMismatchError(f"base image digest mismatch: {exc}") from exc

    if base.disk_format == "raw":
        try:
            with open(base.local_path, "rb") as f:
                header = f.read(4)
                if header in (b"hsqs", b"sqsh"):
                    raise ArtifactValidationError("SquashFS image cannot be used as a boot base")
        except OSError as exc:
            raise LifecycleError(f"failed to read base image header: {exc}") from exc
    elif base.disk_format == "qcow2":
        res = _run_cmd(["qemu-img", "info", "--output=json", str(base.local_path)])
        try:
            info = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise LifecycleError(f"invalid qemu-img info JSON for base image: {exc}") from exc
        if "backing-filename" in info or "full-backing-filename" in info:
            raise ArtifactValidationError("base qcow2 image must not contain an external backing file")

    overlay_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    base_abs = str(base.local_path.resolve())
    create_argv = [
        "qemu-img",
        "create",
        "-f",
        "qcow2",
        "-F",
        base.disk_format,
        "-b",
        base_abs,
        str(overlay_path),
    ]
    _run_cmd(create_argv)

    res = _run_cmd(["qemu-img", "info", "--output=json", str(overlay_path)])
    try:
        info = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        overlay_path.unlink(missing_ok=True)
        raise LifecycleError(f"invalid qemu-img info JSON for overlay: {exc}") from exc

    if info.get("format") != "qcow2":
        overlay_path.unlink(missing_ok=True)
        raise LifecycleError(f"created overlay format is not qcow2: {info.get('format')}")

    backing_file = info.get("backing-filename") or info.get("full-backing-filename")
    if not backing_file or Path(backing_file).resolve() != base.local_path.resolve():
        overlay_path.unlink(missing_ok=True)
        raise LifecycleError("created overlay backing file path mismatch")

    backing_fmt = info.get("backing-filename-format")
    if not backing_fmt and "format-specific" in info:
        backing_fmt = info["format-specific"].get("data", {}).get("backing-filename-format")
    if backing_fmt and backing_fmt != base.disk_format:
        overlay_path.unlink(missing_ok=True)
        raise LifecycleError(f"created overlay backing format mismatch: {backing_fmt} != {base.disk_format}")


def _generate_ssh_keys(rpaths: RunPaths) -> Path:
    rpaths.ssh.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not rpaths.identity.exists():
        _run_cmd(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(rpaths.identity)])
        rpaths.identity.chmod(0o600)

    guest_host_key = rpaths.root / "ssh_host_ed25519_key"
    if not guest_host_key.exists():
        _run_cmd(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(guest_host_key)])
        guest_host_key.chmod(0o600)

    return guest_host_key


def _write_seed_iso(rpaths: RunPaths, profile: platforms.DomainProfile, meta_data: str, user_data: str) -> None:
    """Write NoCloud seed content and build ``rpaths.seed`` with the profile's seed tool."""
    if profile.seed_tool == "hdiutil":
        seed_dir = rpaths.root / "seed.d"
        seed_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta_file = seed_dir / "meta-data"
        user_file = seed_dir / "user-data"
        meta_file.write_text(meta_data, encoding="utf-8")
        meta_file.chmod(0o600)
        user_file.write_text(user_data, encoding="utf-8")
        user_file.chmod(0o600)
        try:
            kvm.run_hdiutil_seed_iso(rpaths.seed, seed_dir)
        except kvm.KvmError as exc:
            raise LifecycleError(f"seed ISO creation failed: {exc}") from exc
        return

    meta_file = rpaths.root / "meta-data"
    user_file = rpaths.root / "user-data"
    meta_file.write_text(meta_data, encoding="utf-8")
    meta_file.chmod(0o600)
    user_file.write_text(user_data, encoding="utf-8")
    user_file.chmod(0o600)
    try:
        kvm.run_seed_iso(rpaths.seed, user_file, meta_file)
    except kvm.KvmError as exc:
        raise LifecycleError(f"seed ISO creation failed: {exc}") from exc


def _generate_seed_iso(
    rpaths: RunPaths,
    run_id: str,
    spec: RunSpec,
    layer_disks: list[kvm.LayerDisk],
    guest_host_key: Path,
    profile: platforms.DomainProfile,
) -> None:
    volume_disks = _build_kvm_volume_disks(spec, layer_disks)
    activation_script = kvm.build_layer_activation_script(layer_disks, volumes=volume_disks)
    meta_data = cloudinit.build_meta_data(run_id, hostname=spec.name)
    user_data = cloudinit.build_user_data(
        client_public_key=rpaths.identity_public,
        host_private_key=guest_host_key,
        host_public_key=guest_host_key.with_name(guest_host_key.name + ".pub"),
        activation_script=activation_script,
        environment=spec.environment,
        cloud_init=spec.cloud_init,
    )
    _write_seed_iso(rpaths, profile, meta_data, user_data)


def _prepare_user_hostfwd(rpaths: RunPaths, profile: platforms.DomainProfile) -> tuple[int, Path]:
    """Allocate a fresh loopback SSH port and stage this boot's NVRAM copy."""
    if profile.firmware is None:
        raise LifecycleError("user-hostfwd networking requires firmware in the domain profile")
    port = platforms.allocate_local_port()
    nvram_path = rpaths.root / "nvram.fd"
    shutil.copyfile(profile.firmware.nvram_template, nvram_path)
    nvram_path.chmod(0o600)
    return port, nvram_path


def _build_kvm_volume_disks(spec: RunSpec, layer_disks: list[kvm.LayerDisk]) -> list[kvm.VolumeDisk]:
    if len(layer_disks) + len(spec.volumes) > kvm.MAX_LAYER_DISKS:
        raise ArtifactValidationError(f"combined layer and volume count exceeds limit {kvm.MAX_LAYER_DISKS}")
    occupied_serials = {disk.serial for disk in layer_disks}
    disks: list[kvm.VolumeDisk] = []
    for index, volume in enumerate(spec.volumes, start=len(layer_disks)):
        if volume.host_path is None:
            raise ArtifactValidationError("KVM runs require local block paths for every project volume")
        serial = hashlib.sha256(f"palimpsest-volume-v1:{volume.name}:{volume.host_path}".encode()).hexdigest()[:20]
        if serial in occupied_serials:
            raise ArtifactValidationError(f"volume serial collision for {volume.name}")
        occupied_serials.add(serial)
        disks.append(
            kvm.VolumeDisk(
                name=volume.name,
                host_path=volume.host_path,
                target_dev=f"vd{kvm._DISK_LETTERS[index]}",
                serial=serial,
                mount_path=volume.mount_path,
                filesystem=volume.filesystem,
                read_only=volume.read_only,
            )
        )
    return disks


def _discover_guest_ip(domain: Any, timeout_seconds: float = 300.0) -> str:
    libvirt = None
    try:
        libvirt = kvm._libvirt()
    except Exception:
        pass

    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout_seconds:
        if hasattr(domain, "interfaceAddresses"):
            sources = [1, 2]
            if libvirt:
                sources = [
                    getattr(libvirt, "VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE", 1),
                    getattr(libvirt, "VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT", 2),
                ]
            for source in sources:
                try:
                    ifaces = domain.interfaceAddresses(source)
                    if ifaces:
                        for iface_data in ifaces.values():
                            addrs = iface_data.get("addrs", [])
                            for addr in addrs:
                                ip = addr.get("addr")
                                if ip and not ip.startswith("127.") and not ip.startswith("fe80:"):
                                    return ip
                except Exception:
                    pass
        time.sleep(0.5)
    raise LifecycleError("timed out waiting for guest IP address discovery")


def _wait_for_readiness(rpaths: RunPaths, domain: Any, timeout_seconds: float, require_ip: bool = True) -> str | None:
    start_time = time.monotonic()
    ready = False
    while time.monotonic() - start_time < timeout_seconds:
        if rpaths.console.exists():
            try:
                content = rpaths.console.read_text(encoding="utf-8", errors="replace")
                if cloudinit.READY_SENTINEL in content:
                    ready = True
                    break
            except Exception:
                pass
        time.sleep(0.5)

    if not ready:
        raise LifecycleError(f"timed out waiting for readiness sentinel ({cloudinit.READY_SENTINEL}) in console.log")

    if not require_ip:
        return None

    remaining = max(5.0, timeout_seconds - (time.monotonic() - start_time))
    return _discover_guest_ip(domain, timeout_seconds=remaining)


def _write_state(rpaths: RunPaths, status: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = {**data, "status": status}
    return state.write_run_state(rpaths, status=status, data=payload)


def run(
    spec: RunSpec,
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    profile, kvm_uri = _resolve_new_run_profile(spec.stack.base.arch, kvm_uri=kvm_uri, profile=profile, conn=conn)
    if spec.ports:
        raise ArtifactValidationError(
            "KVM project port forwarding is unavailable for libvirt network interfaces; "
            "use a routed libvirt network or run this project with the Lima backend"
        )
    roots = roots or state.init_roots()
    rpaths = state.run_paths(roots, spec.name)

    if rpaths.owner.exists() or rpaths.root.exists():
        status = "unknown"
        try:
            st = state.read_run_state(rpaths)
            status = st.get("status", "unknown")
        except Exception:
            pass
        if status == "removed" or rpaths.root.exists():
            raise StateError(
                f"run name '{spec.name}' is held by a removed run; free it with: palimpsest rm {spec.name} --volumes"
            )
        raise StateError(f"run name '{spec.name}' already exists")

    for layer in spec.stack.layers:
        try:
            require_file_digest(layer.local_path, layer.digest)
        except Exception as exc:
            raise DigestMismatchError(f"layer image digest mismatch: {exc}") from exc

    if len(spec.stack.layers) + len(spec.volumes) > kvm.MAX_LAYER_DISKS:
        raise ArtifactValidationError(f"combined layer and volume count exceeds limit {kvm.MAX_LAYER_DISKS}")

    serials: set[str] = set()
    layer_disks: list[kvm.LayerDisk] = []
    for index, layer in enumerate(spec.stack.layers):
        serial = layer.digest.split(":", 1)[1][:20]
        if serial in serials:
            raise ArtifactValidationError(f"layer serial collision for digest {layer.digest}")
        serials.add(serial)
        layer_disks.append(
            kvm.LayerDisk(
                blob_digest=layer.digest,
                host_path=layer.local_path,
                target_dev=f"vd{kvm._DISK_LETTERS[index]}",
                serial=serial,
            )
        )
    volume_disks = _build_kvm_volume_disks(spec, layer_disks)

    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    with state.locked(rpaths):
        owner_rec = state.write_owner_record(rpaths)
        run_id = owner_rec.run_id

        init_data: dict[str, Any] = {
            "name": spec.name,
            "run_id": run_id,
            "created_at": state.utc_now_iso(),
            "updated_at": state.utc_now_iso(),
            "backend": profile.backend,
            "memory_mib": spec.memory_mib,
            "vcpus": spec.vcpus,
            "base": {
                "digest": spec.stack.base.digest,
                "local_path": str(spec.stack.base.local_path),
                "disk_format": spec.stack.base.disk_format,
                "arch": spec.stack.base.arch,
            },
            "layers": [
                {
                    "digest": layer.digest,
                    "local_path": str(layer.local_path),
                    "serial": disk.serial,
                    "target_dev": disk.target_dev,
                }
                for layer, disk in zip(spec.stack.layers, layer_disks, strict=True)
            ],
            "layer_attachment": {
                "delivery": "direct-block",
                "device": "virtio-blk",
                "mount": "squashfs-ro",
            },
            "network": spec.network,
            "volumes": [
                {
                    "name": volume.name,
                    "host_path": str(volume.host_path),
                    "serial": volume.serial,
                    "target_dev": volume.target_dev,
                    "mount_path": volume.mount_path,
                    "filesystem": volume.filesystem,
                    "read_only": volume.read_only,
                }
                for volume in volume_disks
            ],
            "environment_names": [name for name, _value in spec.environment],
            "cloud_init": spec.cloud_init is not None,
            "domain_uuid": None,
            "guest_ip": None,
            "ssh": {"host": None, "port": 22},
            "cleanup_flags": {},
        }

        _write_state(rpaths, status="creating", data=init_data)
        conn_obj = None

        try:
            # Preflight check: connect to libvirt and check if domain name is already taken
            conn_obj = _get_conn(conn, kvm_uri)
            if hasattr(conn_obj, "lookupByName"):
                existing_dom = None
                try:
                    existing_dom = conn_obj.lookupByName(spec.name)
                except Exception:
                    existing_dom = None
                if existing_dom is not None:
                    raise LifecycleError(f"a domain named '{spec.name}' already exists in libvirt")

            rpaths.console.touch(mode=0o600)
            create_and_validate_overlay(spec.stack.base, rpaths.overlay)
            guest_host_key = _generate_ssh_keys(rpaths)
            _generate_seed_iso(rpaths, run_id, spec, layer_disks, guest_host_key, profile)

            net_name = None if spec.network == "none" else spec.network
            ssh_host_port: int | None = None
            nvram_path: Path | None = None
            if profile.network_mode == "user-hostfwd":
                ssh_host_port, nvram_path = _prepare_user_hostfwd(rpaths, profile)

            domain_spec = kvm.DomainSpec(
                name=spec.name,
                memory_mib=spec.memory_mib,
                vcpus=spec.vcpus,
                root_disk=rpaths.overlay,
                seed_iso=rpaths.seed,
                layers=layer_disks,
                volumes=volume_disks,
                network=net_name,
                console_log=rpaths.console,
                run_id=run_id,
                ssh_host_port=ssh_host_port,
                nvram=nvram_path,
            )
            domain_xml = kvm.build_domain_xml(domain_spec, profile)

            domain = None
            if hasattr(conn_obj, "defineXML"):
                domain = conn_obj.defineXML(domain_xml)
            if domain is None:
                raise LifecycleError("domain definition failed")

            domain_uuid = str(domain.UUIDString()) if hasattr(domain, "UUIDString") else str(uuid.uuid4())
            init_data["domain_uuid"] = domain_uuid
            _write_state(rpaths, status="defined", data={**init_data, "updated_at": state.utc_now_iso()})

            _write_state(rpaths, status="starting", data={**init_data, "updated_at": state.utc_now_iso()})
            if hasattr(domain, "create"):
                domain.create()

            require_ip = spec.network != "none" and profile.network_mode != "user-hostfwd"
            guest_ip = _wait_for_readiness(rpaths, domain, timeout_seconds=timeout_seconds, require_ip=require_ip)

            if profile.network_mode == "user-hostfwd":
                ssh_endpoint: dict[str, Any] = {"host": "127.0.0.1", "port": ssh_host_port}
            else:
                ssh_endpoint = {"host": guest_ip, "port": 22}

            if ssh_endpoint["host"] is not None:
                guest_host_pub = guest_host_key.with_name(guest_host_key.name + ".pub")
                known_hosts_entry = guest.build_known_hosts_entry(
                    ssh_endpoint["host"], guest_host_pub, port=ssh_endpoint["port"]
                )
                rpaths.known_hosts.write_text(known_hosts_entry, encoding="utf-8")
                rpaths.known_hosts.chmod(0o600)

            init_data["guest_ip"] = guest_ip
            init_data["ssh"] = ssh_endpoint
            final_state = _write_state(rpaths, status="running", data={**init_data, "updated_at": state.utc_now_iso()})
            return final_state

        except Exception as exc:
            try:
                init_data["updated_at"] = state.utc_now_iso()
                init_data["error"] = str(exc)
                _write_state(rpaths, status="failed", data=init_data)
            except Exception:
                pass

            if conn_obj is not None:
                try:
                    libvirt_domain = conn_obj.lookupByName(spec.name)
                    if libvirt_domain is not None:
                        domain_run_id = kvm.get_domain_run_id(libvirt_domain)
                        if domain_run_id == run_id:
                            _destroy_and_undefine_domain(libvirt_domain)
                except Exception:
                    pass

            if isinstance(exc, (LifecycleError, StateError, ArtifactValidationError, DigestMismatchError)):
                raise
            raise LifecycleError(f"run failed: {exc}") from exc


def start(
    name: str,
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
    timeout_seconds: float = 300.0,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    """Start an owned stopped KVM run without recreating its root or named volumes."""

    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    if _expected_record is not None:
        state.require_bound_run_dispatch_record(roots, _expected_record)
    rpaths = state.run_paths(roots, name)
    owner_rec, current = _validate_run_ledger(rpaths)
    with state.locked(rpaths):
        if current.get("status") == "running":
            return current
        if current.get("status") != "stopped":
            raise LifecycleError(f"run '{name}' cannot be started from status {current.get('status')!r}")
        resolved_profile = profile if profile is not None else _resolve_ledger_profile(current)
        resolved_uri = kvm_uri if kvm_uri is not None else resolved_profile.uri
        conn_obj = _get_conn(conn, resolved_uri)
        try:
            domain = conn_obj.lookupByName(name)
        except Exception as exc:
            raise LifecycleError(f"owned libvirt domain '{name}' is missing") from exc
        if domain is None or kvm.get_domain_run_id(domain) != owner_rec.run_id:
            raise LifecycleError(f"domain '{name}' is missing or is not owned by this run")
        _write_state(rpaths, status="starting", data={**current, "updated_at": state.utc_now_iso()})
        try:
            # A stopped domain reuses its serial log.  Remove the previous boot's
            # sentinel so readiness can only be satisfied by this boot's
            # palimpsest-ready.service.
            rpaths.console.write_text("", encoding="utf-8")
            rpaths.console.chmod(0o600)
            if resolved_profile.network_mode == "user-hostfwd":
                ssh_port = platforms.allocate_local_port()
                xml_str = domain.XMLDesc()
                updated_xml, replacements = re.subn(
                    r"hostfwd=tcp:127\.0\.0\.1:\d+-:22",
                    f"hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
                    xml_str,
                    count=1,
                )
                if replacements != 1:
                    raise LifecycleError(f"domain '{name}' has no Palimpsest SSH host-forward rule")
                try:
                    domain = conn_obj.defineXML(updated_xml)
                except Exception as exc:
                    raise LifecycleError(f"failed to update SSH host-forward rule for domain '{name}'") from exc
                if domain is None or kvm.get_domain_run_id(domain) != owner_rec.run_id:
                    raise LifecycleError(f"domain '{name}' is missing or is not owned by this run")
                ssh_endpoint: dict[str, Any] = {"host": "127.0.0.1", "port": ssh_port}
            domain.create()
            require_ip = current.get("network") != "none" and resolved_profile.network_mode != "user-hostfwd"
            guest_ip = _wait_for_readiness(rpaths, domain, timeout_seconds=timeout_seconds, require_ip=require_ip)
            if resolved_profile.network_mode != "user-hostfwd":
                ssh_endpoint = {"host": guest_ip, "port": 22}
            if ssh_endpoint["host"] is not None:
                guest_host_pub = rpaths.root / "ssh_host_ed25519_key.pub"
                known_hosts_entry = guest.build_known_hosts_entry(
                    ssh_endpoint["host"], guest_host_pub, port=ssh_endpoint["port"]
                )
                rpaths.known_hosts.write_text(known_hosts_entry, encoding="utf-8")
                rpaths.known_hosts.chmod(0o600)
            return _write_state(
                rpaths,
                status="running",
                data={**current, "guest_ip": guest_ip, "ssh": ssh_endpoint, "updated_at": state.utc_now_iso()},
            )
        except Exception as exc:
            try:
                if hasattr(domain, "isActive") and domain.isActive() and hasattr(domain, "destroy"):
                    domain.destroy()
            except Exception:
                pass
            _write_state(
                rpaths,
                status="failed",
                data={**current, "error": str(exc), "updated_at": state.utc_now_iso()},
            )
            if isinstance(exc, (LifecycleError, StateError, ArtifactValidationError)):
                raise
            raise LifecycleError(f"start failed: {exc}") from exc


def start_serial_builder(
    spec: RunSpec,
    *,
    user_data: str,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
) -> dict[str, Any]:
    """Start a credential-free builder whose only host channel is output streaming."""
    profile, kvm_uri = _resolve_new_run_profile(spec.stack.base.arch, kvm_uri=kvm_uri, profile=profile, conn=conn)
    if spec.network not in {"none", "default"}:
        raise ArtifactValidationError("serial builder network must be 'none' or 'default'")
    roots = roots or state.init_roots()
    rpaths = state.run_paths(roots, spec.name)
    if rpaths.owner.exists() or rpaths.root.exists():
        raise StateError(f"run name '{spec.name}' already exists")
    for layer in spec.stack.layers:
        try:
            require_file_digest(layer.local_path, layer.digest)
        except Exception as exc:
            raise DigestMismatchError(f"layer image digest mismatch: {exc}") from exc
    if len(spec.stack.layers) > kvm.MAX_LAYER_DISKS:
        raise ArtifactValidationError(f"layer count exceeds limit {kvm.MAX_LAYER_DISKS}")

    layer_disks: list[kvm.LayerDisk] = []
    serials: set[str] = set()
    for index, layer in enumerate(spec.stack.layers):
        serial = layer.digest.split(":", 1)[1][:20]
        if serial in serials:
            raise ArtifactValidationError(f"layer serial collision for digest {layer.digest}")
        serials.add(serial)
        layer_disks.append(kvm.LayerDisk(layer.digest, layer.local_path, f"vd{kvm._DISK_LETTERS[index]}", serial))

    rpaths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state.locked(rpaths):
        try:
            owner_rec = state.write_owner_record(rpaths)
        except Exception:
            shutil.rmtree(rpaths.root, ignore_errors=True)
            raise
        run_id = owner_rec.run_id
        initial: dict[str, Any] = {
            "name": spec.name,
            "run_id": run_id,
            "created_at": state.utc_now_iso(),
            "updated_at": state.utc_now_iso(),
            "backend": profile.backend,
            "base": {
                "digest": spec.stack.base.digest,
                "local_path": str(spec.stack.base.local_path),
                "disk_format": spec.stack.base.disk_format,
                "arch": spec.stack.base.arch,
            },
            "layers": [
                {
                    "digest": layer.digest,
                    "local_path": str(layer.local_path),
                    "serial": disk.serial,
                    "target_dev": disk.target_dev,
                }
                for layer, disk in zip(spec.stack.layers, layer_disks, strict=True)
            ],
            "domain_uuid": None,
            "guest_ip": None,
            "cleanup_flags": {},
            "builder_transport": "serial-output-v1",
        }
        try:
            _write_state(rpaths, status="creating", data=initial)
        except Exception:
            shutil.rmtree(rpaths.root, ignore_errors=True)
            raise
        conn_obj = None
        try:
            conn_obj = _get_conn(conn, kvm_uri)
            if hasattr(conn_obj, "lookupByName"):
                try:
                    if conn_obj.lookupByName(spec.name) is not None:
                        raise LifecycleError(f"a domain named '{spec.name}' already exists in libvirt")
                except LifecycleError:
                    raise
                except Exception:
                    pass
            rpaths.console.touch(mode=0o600)
            create_and_validate_overlay(spec.stack.base, rpaths.overlay)
            meta_data = cloudinit.build_meta_data(run_id, hostname=spec.name)
            _write_seed_iso(rpaths, profile, meta_data, user_data)

            ssh_host_port: int | None = None
            nvram_path: Path | None = None
            if profile.network_mode == "user-hostfwd":
                ssh_host_port, nvram_path = _prepare_user_hostfwd(rpaths, profile)

            control_socket = rpaths.root / "builder.sock"
            domain_xml = kvm.build_domain_xml(
                kvm.DomainSpec(
                    name=spec.name,
                    memory_mib=spec.memory_mib,
                    vcpus=spec.vcpus,
                    root_disk=rpaths.overlay,
                    seed_iso=rpaths.seed,
                    layers=layer_disks,
                    network=None if spec.network == "none" else spec.network,
                    console_log=rpaths.console,
                    run_id=run_id,
                    guest_agent=False,
                    control_socket=control_socket,
                    ssh_host_port=ssh_host_port,
                    nvram=nvram_path,
                ),
                profile,
            )
            domain = conn_obj.defineXML(domain_xml) if hasattr(conn_obj, "defineXML") else None
            if domain is None:
                raise LifecycleError("domain definition failed")
            initial["domain_uuid"] = str(domain.UUIDString()) if hasattr(domain, "UUIDString") else str(uuid.uuid4())
            _write_state(rpaths, status="defined", data={**initial, "updated_at": state.utc_now_iso()})
            _write_state(rpaths, status="starting", data={**initial, "updated_at": state.utc_now_iso()})
            if hasattr(domain, "create"):
                domain.create()
            _wait_for_readiness(rpaths, domain, timeout_seconds=300.0, require_ip=False)
            return _write_state(rpaths, status="running", data={**initial, "updated_at": state.utc_now_iso()})
        except Exception as exc:
            try:
                _write_state(
                    rpaths,
                    status="failed",
                    data={**initial, "error": str(exc), "updated_at": state.utc_now_iso()},
                )
            except Exception:
                pass
            if conn_obj is not None:
                try:
                    domain = conn_obj.lookupByName(spec.name)
                    if domain is not None and kvm.get_domain_run_id(domain) == run_id:
                        _destroy_and_undefine_domain(domain)
                except Exception:
                    pass
            if isinstance(exc, (LifecycleError, StateError, ArtifactValidationError, DigestMismatchError)):
                raise
            raise LifecycleError(f"serial builder start failed: {exc}") from exc


def receive_serial_builder_output(
    socket_path: Path,
    destination: Path,
    *,
    connect_timeout_seconds: float = 300.0,
    build_timeout_seconds: float = 3600.0,
    transfer_timeout_seconds: float = 1800.0,
) -> str:
    """Receive one fixed framed SquashFS output from a serial-only builder."""
    if not socket_path.is_absolute():
        raise LifecycleError("serial builder socket path must be absolute")
    if min(connect_timeout_seconds, build_timeout_seconds, transfer_timeout_seconds) <= 0:
        raise LifecycleError("serial builder timeouts must be positive")
    deadline = time.monotonic() + connect_timeout_seconds
    sock: socket.socket | None = None
    while time.monotonic() < deadline:
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            candidate.connect(str(socket_path))
            sock = candidate
            break
        except OSError:
            candidate.close()
            time.sleep(0.1)
    if sock is None:
        raise LifecycleError("timed out connecting to serial builder output channel")
    deadline = time.monotonic() + build_timeout_seconds

    def read_exact(size: int) -> bytes:
        received = bytearray()
        while len(received) < size:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise LifecycleError("timed out receiving serial builder output")
            sock.settimeout(remaining_seconds)
            try:
                chunk = sock.recv(size - len(received))
            except TimeoutError as exc:
                raise LifecycleError("timed out receiving serial builder output") from exc
            if not chunk:
                raise LifecycleError("serial builder output was truncated")
            received.extend(chunk)
        return bytes(received)

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.unlink(missing_ok=True)
    try:
        header = bytearray()
        while len(header) <= 16_384:
            header.extend(read_exact(1))
            if header[-1:] == b"\n":
                header.pop()
                break
        else:
            raise LifecycleError("serial builder result header is too large")
        try:
            result = json.loads(header.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LifecycleError("serial builder sent an invalid result header") from exc
        if result.get("version") != 1:
            raise LifecycleError("serial builder sent an unsupported result version")
        if result.get("status") != "ok":
            stage = result.get("stage")
            line = result.get("line")
            detail = f" during {stage}" if isinstance(stage, str) else ""
            if isinstance(line, int) and line > 0:
                detail += f" at Palimpsestfile line {line}"
            raise LifecycleError(f"serial builder reported a build failure{detail}; inspect its console log")
        size = result.get("size")
        digest_hex = result.get("sha256")
        if not isinstance(size, int) or not 4 <= size <= 16 * 1024 * 1024 * 1024:
            raise LifecycleError("serial builder reported an invalid output size")
        if not isinstance(digest_hex, str) or len(digest_hex) != 64:
            raise LifecycleError("serial builder reported an invalid output digest")
        expected = "sha256:" + digest_hex.lower()
        deadline = time.monotonic() + transfer_timeout_seconds
        hasher = hashlib.sha256()
        with destination.open("xb") as fp:
            remaining = size
            while remaining:
                chunk = read_exact(min(1024 * 1024, remaining))
                fp.write(chunk)
                hasher.update(chunk)
                remaining -= len(chunk)
            fp.flush()
            os.fsync(fp.fileno())
        with destination.open("rb") as fp:
            if fp.read(4) != b"hsqs":
                raise LifecycleError("serial builder output is not a SquashFS archive")
        actual = "sha256:" + hasher.hexdigest()
        if actual != expected:
            raise LifecycleError("serial builder output digest mismatch")
        return actual
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        sock.close()


def stop(
    name: str,
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
    timeout_seconds: float = 30.0,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    if _expected_record is not None:
        state.require_bound_run_dispatch_record(roots, _expected_record)
    rpaths = state.run_paths(roots, name)
    owner_rec, curr_state = _validate_run_ledger(rpaths)

    with state.locked(rpaths):
        if curr_state.get("status") in ("stopped", "removed"):
            return curr_state

        resolved_profile = profile if profile is not None else _resolve_ledger_profile(curr_state)
        resolved_uri = kvm_uri if kvm_uri is not None else resolved_profile.uri
        conn_obj = _get_conn(conn, resolved_uri)

        domain = None
        if conn_obj is not None:
            try:
                domain = conn_obj.lookupByName(name)
            except Exception:
                domain = None

        if domain is not None:
            domain_run_id = kvm.get_domain_run_id(domain)
            if domain_run_id != owner_rec.run_id:
                raise LifecycleError(
                    f"domain '{name}' is foreign (domain run_id {domain_run_id!r} != owner run_id {owner_rec.run_id!r})"
                )

            is_active = True
            if hasattr(domain, "isActive"):
                is_active = domain.isActive()

            if is_active:
                _write_state(rpaths, status="stopping", data={**curr_state, "updated_at": state.utc_now_iso()})
                if hasattr(domain, "shutdown"):
                    try:
                        domain.shutdown()
                    except Exception:
                        pass

                start_t = time.monotonic()
                while time.monotonic() - start_t < timeout_seconds:
                    if hasattr(domain, "isActive") and not domain.isActive():
                        break
                    time.sleep(0.5)

                if hasattr(domain, "isActive") and domain.isActive():
                    if hasattr(domain, "destroy"):
                        domain.destroy()

        updated = _write_state(rpaths, status="stopped", data={**curr_state, "updated_at": state.utc_now_iso()})
        return updated


def rm(
    name: str,
    *,
    volumes: bool = False,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    if _expected_record is not None:
        state.require_bound_run_dispatch_record(roots, _expected_record)
    rpaths = state.run_paths(roots, name)
    owner_rec, curr_state = _validate_run_ledger(rpaths)

    with state.locked(rpaths):
        resolved_profile = profile if profile is not None else _resolve_ledger_profile(curr_state)
        resolved_uri = kvm_uri if kvm_uri is not None else resolved_profile.uri
        conn_obj = _get_conn(conn, resolved_uri)

        domain = None
        if conn_obj is not None:
            try:
                domain = conn_obj.lookupByName(name)
            except Exception:
                domain = None

        if domain is not None:
            domain_run_id = kvm.get_domain_run_id(domain)
            if domain_run_id != owner_rec.run_id:
                raise LifecycleError(
                    f"domain '{name}' is foreign (domain run_id {domain_run_id!r} != owner run_id {owner_rec.run_id!r})"
                )

            _destroy_and_undefine_domain(domain)

        if not volumes:
            updated = _write_state(rpaths, status="removed", data={**curr_state, "updated_at": state.utc_now_iso()})
            return updated
        updated = {**curr_state, "status": "removed", "updated_at": state.utc_now_iso()}
        if rpaths.root.exists():
            shutil.rmtree(rpaths.root)
        if rpaths.root.exists():
            raise LifecycleError(f"failed to remove run volume directory for '{name}'")
        if rpaths.lock.exists():
            rpaths.lock.unlink(missing_ok=True)
        return updated


def reconcile(
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    roots = roots or state.init_roots()
    resolved_uri = kvm_uri if kvm_uri is not None else (profile.uri if profile is not None else "qemu:///system")
    warnings: list[str] = []
    runs: list[dict[str, Any]] = []

    if not roots.runs.exists():
        return [], []

    conn_obj = None
    try:
        conn_obj = _get_conn(conn, resolved_uri)
    except Exception as exc:
        if profile is not None:
            raise LifecycleError(
                f"failed to connect to libvirt URI {resolved_uri!r} for backend {profile.backend!r}: {exc}"
            ) from exc

    for run_dir in sorted(roots.runs.iterdir()):
        if not run_dir.is_dir():
            continue
        name = run_dir.name
        rpaths = state.run_paths(roots, name)

        try:
            owner_rec, st_data = _validate_run_ledger(rpaths)
        except Exception as exc:
            warnings.append(f"run '{name}': invalid ledger ({exc})")
            continue
        run_backend = st_data.get("backend") or platforms.BACKEND_KVM
        if profile is not None and run_backend != profile.backend:
            continue

        status = st_data.get("status", "unknown")

        if run_backend in _LIBVIRT_BACKENDS and conn_obj is not None:
            try:
                domain = conn_obj.lookupByName(name)
            except Exception as exc:
                if not _is_missing_domain_error(exc):
                    raise LifecycleError(f"cannot inspect libvirt domain {name!r} during reconciliation") from exc
                if status in ("running", "starting"):
                    status = "stopped"
                    st_data["status"] = status
                    st_data["updated_at"] = state.utc_now_iso()
                    warnings.append(f"run '{name}': domain missing from libvirt")
                    try:
                        _write_state(rpaths, status=status, data=st_data)
                    except Exception:
                        pass
            else:
                domain_run_id = kvm.get_domain_run_id(domain)
                if domain_run_id != owner_rec.run_id:
                    raise StateError(
                        f"run '{name}' is shadowed by a foreign libvirt domain "
                        f"(domain run_id {domain_run_id!r} != owner run_id {owner_rec.run_id!r})"
                    )
                is_active = domain.isActive() if hasattr(domain, "isActive") else False
                if is_active and status in ("stopped", "defined"):
                    status = "running"
                    st_data["status"] = status
                    st_data["updated_at"] = state.utc_now_iso()
                    try:
                        _write_state(rpaths, status=status, data=st_data)
                    except Exception:
                        pass
                elif not is_active and status in ("running", "starting"):
                    status = "stopped"
                    st_data["status"] = status
                    st_data["updated_at"] = state.utc_now_iso()
                    try:
                        _write_state(rpaths, status=status, data=st_data)
                    except Exception:
                        pass

        runs.append(
            {
                "name": name,
                "status": status,
                "owner": asdict(owner_rec),
                "state": st_data,
            }
        )

    return runs, warnings


def ps(
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
) -> list[dict[str, Any]]:
    roots = roots or state.init_roots()
    runs, _ = reconcile(roots=roots, conn=conn, kvm_uri=kvm_uri, profile=profile)
    result = []
    for r in runs:
        st = r.get("state", {})
        status = st.get("status", "unknown")
        if status == "removed":
            continue
        base_digest = st.get("base", {}).get("digest", "")
        short_digest = base_digest[:19] if base_digest else "-"
        layers = st.get("layers", [])
        guest_ip = st.get("guest_ip") or "-"
        result.append(
            {
                "name": r["name"],
                "status": status,
                "backend": st.get("backend", platforms.BACKEND_KVM),
                "base_digest": short_digest,
                "layers_count": len(layers),
                "guest_ip": guest_ip,
                "created_at": st.get("created_at", "-"),
            }
        )
    return result


def inspect_run(
    name: str,
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
    _expected_record: ExistingRunRecord | None = None,
) -> dict[str, Any]:
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    if _expected_record is not None:
        state.require_bound_run_dispatch_record(roots, _expected_record)
    rpaths = state.run_paths(roots, name)
    owner_rec, _st_data = _validate_run_ledger(rpaths)

    _, warnings = reconcile(roots=roots, conn=conn, kvm_uri=kvm_uri, profile=profile)
    run_warnings = [w for w in warnings if f"run '{name}'" in w]
    # Reconciliation may have observed an external stop/start and durably
    # updated state.  Return the post-reconcile record, not the stale snapshot
    # captured above.
    owner_rec, st_data = _validate_run_ledger(rpaths)

    return {
        "schema_version": 1,
        "owner": asdict(owner_rec),
        "state": st_data,
        "warnings": run_warnings,
    }


def logs(
    name: str,
    *,
    roots: StatePaths | None = None,
    follow: bool = False,
    poll_interval: float = 0.5,
    _expected_record: ExistingRunRecord | None = None,
) -> Iterator[str]:
    roots = roots or (state.resolve_roots() if _expected_record is not None else state.init_roots())
    if _expected_record is not None:
        state.require_bound_run_dispatch_record(roots, _expected_record)
    rpaths = state.run_paths(roots, name)
    _validate_run_ledger(rpaths)

    if not rpaths.console.exists():
        raise LifecycleError(f"console log not found for run '{name}'")

    if not follow:
        content = rpaths.console.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines(keepends=True):
            yield line
        return

    with open(rpaths.console, encoding="utf-8", errors="replace") as f:
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                try:
                    st = state.read_run_state(rpaths)
                    if st.get("status") in ("removed", "failed", "stopped"):
                        break
                except Exception:
                    pass
                time.sleep(poll_interval)


def shell_command(
    name: str,
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
) -> list[str]:
    roots = roots or state.init_roots()
    rpaths = state.run_paths(roots, name)
    _, st = _validate_run_ledger(rpaths)

    if st.get("status") != "running":
        raise LifecycleError(f"run '{name}' is not running (status: {st.get('status')})")
    host, port = _ssh_endpoint(name, st)
    return guest.build_shell_command(
        host,
        identity=rpaths.identity,
        known_hosts=rpaths.known_hosts,
        port=port,
    )


def exec_command(
    name: str,
    argv: Sequence[str],
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
) -> list[str]:
    roots = roots or state.init_roots()
    rpaths = state.run_paths(roots, name)
    _, st = _validate_run_ledger(rpaths)

    if st.get("status") != "running":
        raise LifecycleError(f"run '{name}' is not running (status: {st.get('status')})")
    host, port = _ssh_endpoint(name, st)
    return guest.build_exec_command(
        host,
        argv,
        identity=rpaths.identity,
        known_hosts=rpaths.known_hosts,
        port=port,
    )


def commit(
    name: str,
    tag: str,
    *,
    roots: StatePaths | None = None,
    conn: Any | None = None,
    kvm_uri: str | None = None,
    profile: platforms.DomainProfile | None = None,
    runner: Callable[[list[str]], Any] | None = None,
) -> dict[str, Any]:
    """Commit upper layer changes of a running run into a local content-store tag."""
    roots = roots or state.init_roots()
    state.validate_tag(tag)

    tag_path = state.tag_path(roots, tag)
    existing_tag: TagRecord | None = None
    if tag_path.is_file():
        existing_tag = state.read_tag_record(roots, tag)

    rpaths = state.run_paths(roots, name)
    owner_rec, st_data = _validate_run_ledger(rpaths)

    if st_data.get("status") != "running":
        raise LifecycleError(f"run '{name}' is not running (status: {st_data.get('status')})")

    resolved_profile = profile if profile is not None else _resolve_ledger_profile(st_data)
    resolved_uri = kvm_uri if kvm_uri is not None else resolved_profile.uri
    conn_obj = _get_conn(conn, resolved_uri)
    try:
        dom = conn_obj.lookupByName(name)
        dom_run_id = kvm.get_domain_run_id(dom)
        if dom_run_id != owner_rec.run_id:
            raise LifecycleError(f"domain '{name}' is not owned by run ID '{owner_rec.run_id}' (found '{dom_run_id}')")
    except Exception as exc:
        if isinstance(exc, LifecycleError):
            raise
        raise LifecycleError(f"domain '{name}' lookup failed: {exc}") from exc

    host, port = _ssh_endpoint(name, st_data)

    base_info = st_data.get("base")
    if isinstance(base_info, dict) and "local_path" in base_info and "digest" in base_info:
        base_path = Path(base_info["local_path"])
        base_digest = base_info["digest"]
        try:
            require_file_digest(base_path, base_digest)
        except Exception as exc:
            raise LifecycleError(f"recorded base image digest mismatch: {exc}") from exc
    else:
        base_digest = st_data.get("base_digest")
        if not base_digest:
            raise LifecycleError(f"run '{name}' state record is missing base_digest")

    layers_data = st_data.get("layers", [])
    for layer in layers_data:
        if isinstance(layer, dict) and "local_path" in layer and "digest" in layer:
            try:
                require_file_digest(Path(layer["local_path"]), layer["digest"])
            except Exception as exc:
                raise LifecycleError(f"recorded layer digest mismatch: {exc}") from exc
    parent_digest = layers_data[-1]["digest"] if layers_data else None

    identity = rpaths.identity
    known_hosts = rpaths.known_hosts

    def exec_guest(argv: list[str]) -> subprocess.CompletedProcess[str]:
        ssh_cmd = guest.build_exec_command(host, argv, identity=identity, known_hosts=known_hosts, port=port)
        if runner is not None:
            res = runner(ssh_cmd)
            if isinstance(res, subprocess.CompletedProcess):
                return res
            return subprocess.CompletedProcess(ssh_cmd, 0 if res is None else res, "", "")
        return subprocess.run(ssh_cmd, capture_output=True, text=True, check=False)

    res_fuser = exec_guest(["sudo", "-n", "fuser", "-m", "/opt/layers/merged"])
    if res_fuser.returncode == 0:
        if not res_fuser.stdout.strip():
            raise LifecycleError("merged tree usage check returned no process information")
        raise LifecycleError(f"cannot commit run '{name}': processes are using /opt/layers/merged")
    if res_fuser.returncode != 1:
        raise LifecycleError(f"failed to check merged tree usage: {res_fuser.stderr}")

    res_upper = exec_guest(["sudo", "-n", "findmnt", "-no", "FSTYPE", "-T", "/opt/layers/upper"])
    res_work = exec_guest(["sudo", "-n", "findmnt", "-no", "FSTYPE", "-T", "/opt/layers/work"])
    if res_upper.returncode != 0 or res_work.returncode != 0:
        raise LifecycleError(f"failed to inspect commit capture filesystem: {res_upper.stderr or res_work.stderr}")
    upper_fstype = res_upper.stdout.strip()
    work_fstype = res_work.stdout.strip()
    if not upper_fstype or not work_fstype:
        raise LifecycleError("commit capture filesystem type is missing")
    for fstype in (upper_fstype, work_fstype):
        if fstype in {"nfs", "nfs4", "ceph", "virtiofs"} or fstype.startswith("fuse."):
            raise LifecycleError(f"commit upper/work filesystem type {fstype!r} is unsupported")

    res_dev_upper = exec_guest(["sudo", "-n", "stat", "-c", "%d", "/opt/layers/upper"])
    res_dev_work = exec_guest(["sudo", "-n", "stat", "-c", "%d", "/opt/layers/work"])
    if res_dev_upper.returncode != 0 or res_dev_work.returncode != 0:
        raise LifecycleError(f"failed to inspect commit capture devices: {res_dev_upper.stderr or res_dev_work.stderr}")
    if not res_dev_upper.stdout.strip() or not res_dev_work.stdout.strip():
        raise LifecycleError("commit capture device identifier is missing")
    if res_dev_upper.stdout.strip() != res_dev_work.stdout.strip():
        raise LifecycleError("upper and work directories must reside on the same filesystem device")

    res_sync = exec_guest(["sudo", "-n", "sync"])
    if res_sync.returncode != 0:
        raise LifecycleError(f"failed to sync run before commit: {res_sync.stderr}")

    from .build import generate_cleaning_command

    clean_cmd = generate_cleaning_command("/opt/layers/upper")
    res_clean = exec_guest(["sudo", "-n", "/bin/bash", "-c", clean_cmd])
    if res_clean.returncode != 0:
        raise LifecycleError(f"failed to clean run upperdir: {res_clean.stderr}")

    res_pack = exec_guest(
        [
            "sudo",
            "-n",
            "mksquashfs",
            "/opt/layers/upper",
            "/tmp/commit_output.squashfs",
            "-comp",
            "zstd",
            "-Xcompression-level",
            "3",
            "-noappend",
            "-no-exports",
        ]
    )
    if res_pack.returncode != 0:
        raise LifecycleError(f"mksquashfs in guest failed during commit: {res_pack.stderr}")

    tmp_host_out = rpaths.root / f"commit-{tag}.squashfs"
    scp_cmd = guest.build_scp_download_command(
        host, "/tmp/commit_output.squashfs", tmp_host_out, identity=identity, known_hosts=known_hosts, port=port
    )
    if runner is not None:
        res_scp = runner(scp_cmd)
    else:
        res_scp = subprocess.run(scp_cmd, capture_output=True, text=True, check=False)
    if isinstance(res_scp, subprocess.CompletedProcess) and res_scp.returncode != 0:
        raise LifecycleError(f"failed to scp commit output from guest: {res_scp.stderr}")

    try:
        if not tmp_host_out.is_file() or tmp_host_out.stat().st_size < 4:
            raise LifecycleError("committed output.squashfs is missing or truncated")
        with open(tmp_host_out, "rb") as f:
            header = f.read(4)
        if header != b"hsqs":
            raise LifecycleError("committed output file is not a valid SquashFS archive (invalid magic)")

        output_digest = digest_file(tmp_host_out)
        output_size = tmp_host_out.stat().st_size

        if existing_tag is not None and existing_tag.digest != output_digest:
            raise LifecycleError(
                f"tag '{tag}' already exists with digest {existing_tag.digest}, conflicting with committed digest {output_digest}"
            )

        store = ContentStore(roots.store)
        store.ingest_file(tmp_host_out, expected_digest=output_digest)
        store.write_metadata(
            output_digest,
            {
                "kind": "squashfs",
                "media_type": MEDIA_TYPE_LAYER_SQUASHFS,
                "parent_digest": parent_digest,
                "base_image_digest": base_digest,
            },
        )

        tag_record = TagRecord(
            schema_version=1,
            tag=tag,
            digest=output_digest,
            media_type=MEDIA_TYPE_LAYER_SQUASHFS,
            size_bytes=output_size,
            parent_digest=parent_digest,
            base_image_digest=base_digest,
            source="commit",
            created_at=state.utc_now_iso(),
        )
        state.write_tag_record(roots, tag_record)

        exec_guest(["sudo", "-n", "rm", "-f", "/tmp/commit_output.squashfs"])

        return {
            "tag": tag,
            "digest": output_digest,
            "size_bytes": output_size,
            "parent_digest": parent_digest,
            "base_image_digest": base_digest,
            "source": "commit",
        }
    finally:
        tmp_host_out.unlink(missing_ok=True)


commit_run = commit
