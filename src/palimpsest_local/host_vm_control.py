"""Explicit, identity-bound removal of observed VMs, without ledger adoption."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import cloud_runtime, kvm, lima, platforms, retained_instances, state
from .errors import PalimpsestError, StateError
from .runtime_types import RuntimeBackend


def lima_removal_identity(instance: dict[str, Any]) -> str | None:
    """Fingerprint a physical native instance, including unmarked Lima VMs."""
    try:
        name = instance.get("name")
        path = instance.get("dir")
        config = instance.get("config")
        vm_type = instance.get("vmType") or (config.get("vmType") if isinstance(config, dict) else None)
        home = os.path.realpath(os.environ.get("LIMA_HOME", str(Path.home() / ".lima")))
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(vm_type, str)
            or vm_type not in {"vz", "qemu", "krunkit"}
            or not isinstance(path, str)
            or not os.path.isabs(path)
            or os.path.realpath(path) != path
            or os.path.dirname(path) != home
            or os.path.basename(path) != name
        ):
            return None
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            return None
        identity = ("lima-" + vm_type, name, path, info.st_dev, info.st_ino, lima._instance_run_id(instance))
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    except (OSError, ValueError, PalimpsestError):
        return None


def libvirt_removal_identity(domain: Any, *, definition: ET.Element | None) -> str | None:
    """Require positively classified full XML before exposing native removal."""
    if definition is None or definition.tag != "domain":
        return None
    try:
        value = domain.UUIDString()
        if (
            not isinstance(value, str)
            or str(uuid.UUID(value)) != value
            or definition.findtext("uuid") != value
            or definition.findtext("name") != domain.name()
        ):
            return None
        markers = definition.findall(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
        if len(markers) > 1 or any(
            kvm._validated_domain_marker_run_id(marker) is None or "contract" in marker.attrib for marker in markers
        ):
            return None
        return value
    except (AttributeError, TypeError, ValueError):
        return None


def _libvirt_retention_flags(domain: Any, definition: ET.Element, libvirt: Any) -> int | None:
    emulators = definition.findall("./devices/tpm/backend[@type='emulator']")
    if not domain.isPersistent():
        if any(tpm.get("persistent_state") != "yes" for tpm in emulators):
            raise StateError("cannot retain transient emulator TPM state; VM was not stopped")
        return None
    flags = (
        libvirt.VIR_DOMAIN_UNDEFINE_KEEP_NVRAM
        | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
        | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
    )
    if emulators:
        keep_tpm = getattr(libvirt, "VIR_DOMAIN_UNDEFINE_KEEP_TPM", None)
        if not isinstance(keep_tpm, int) or keep_tpm <= 0:
            raise StateError("libvirt cannot explicitly retain TPM state; VM was not stopped")
        flags |= keep_tpm
    return flags


def _remove_lima(
    row: dict[str, Any],
    expected_identity: str,
    *,
    roots: state.StatePaths,
    volumes: bool,
    ledger: dict[str, Any] | None = None,
) -> str | None:
    name = row["name"]
    current = lima._instance_info_or_none(name)
    if current is None or lima_removal_identity(current) != expected_identity:
        raise StateError("selected Lima VM identity changed; refresh before removing")
    try:
        fd = os.open(current["dir"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise StateError("selected Lima VM directory is unavailable") from None
    try:
        pinned = os.fstat(fd)
        current = lima._instance_info_or_none(name)
        if current is None or lima_removal_identity(current) != expected_identity:
            raise StateError("selected Lima VM identity changed; refresh before removing")
        linked = os.stat(current["dir"], follow_symlinks=False)
        if (pinned.st_dev, pinned.st_ino) != (linked.st_dev, linked.st_ino):
            raise StateError("selected Lima VM directory changed; refresh before removing")
        archive = None
        if not volumes:
            if str(current.get("status", "")).lower() != "stopped":
                result = lima._run_command(["limactl", "stop", "--force", "--", name], timeout_seconds=60)
                if result.returncode != 0:
                    raise StateError("cannot stop Lima VM for root retention")
            current = lima._instance_info_or_none(name)
            if current is None or lima_removal_identity(current) != expected_identity:
                raise StateError("selected Lima VM changed before retention")
            if str(current.get("status", "")).lower() != "stopped":
                raise StateError("Lima VM is not stopped; refusing inconsistent root snapshot")
            archive = retained_instances.retain_lima(roots, current, identity=expected_identity, ledger=ledger)
        current = lima._instance_info_or_none(name)
        if current is None or lima_removal_identity(current) != expected_identity:
            raise StateError("selected Lima VM changed before native deletion")
        linked = os.stat(current["dir"], follow_symlinks=False)
        if (pinned.st_dev, pinned.st_ino) != (linked.st_dev, linked.st_ino):
            raise StateError("selected Lima VM directory changed before native deletion")
        result = lima._run_command(["limactl", "delete", "--force", "--", name], timeout_seconds=60)
        if result.returncode != 0:
            raise StateError("Lima VM removal failed; retained root was not discarded")
        if lima._instance_info_or_none(name) is not None:
            raise StateError("Lima VM name is still present after removal; refresh before any further action")
        if archive is not None:
            retained_instances.mark_native_removed(roots, archive)
        return archive
    except OSError:
        raise StateError("selected Lima VM directory is unavailable") from None
    finally:
        os.close(fd)


def _remove_libvirt(row: dict[str, Any], expected_identity: str, *, roots: state.StatePaths, volumes: bool) -> str:
    host = platforms.detect_host()
    backend = RuntimeBackend.KVM if host.system == "Linux" else RuntimeBackend.LIBVIRT_HVF
    if host.system not in {"Linux", "Darwin"} or row["backend"] != backend.value:
        raise StateError("selected libvirt backend is not available on this host")
    libvirt = kvm._libvirt()
    conn = kvm.connect(cloud_runtime._RECONCILE_URIS[backend])
    try:
        domain = conn.lookupByUUIDString(expected_identity)

        def check() -> int | None:
            definition = ET.fromstring(domain.XMLDesc(0))
            if (
                domain.name() != row["name"]
                or libvirt_removal_identity(domain, definition=definition) != expected_identity
            ):
                raise StateError("selected libvirt VM identity or classification changed; refresh before removing")
            marker = definition.find(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}run")
            run_id = None if marker is None else marker.get("id")
            if run_id != row.get("run_id"):
                raise StateError("selected libvirt VM run marker changed; refresh before removing")
            return _libvirt_retention_flags(domain, definition, libvirt)

        flags = check()
        if volumes:
            raise StateError("observed libvirt root ownership is unknown; unsafe purge refused before stopping")
        archive = retained_instances.retain_libvirt(
            roots,
            name=row["name"],
            backend=row["backend"],
            identity=expected_identity,
            definition=ET.fromstring(domain.XMLDesc(0)),
            ledger={"run_id": row.get("run_id"), "layers": row.get("layers", [])},
        )
        if domain.isActive():
            flags = check()
            domain.destroy()
            if domain.isActive():
                raise StateError("selected libvirt VM did not stop; native undefinition refused")
        if flags is not None:
            flags = check()
            # Retain disk, NVRAM and TPM state; delete only domain-owned metadata.
            if flags is not None:
                domain.undefineFlags(flags)
        try:
            conn.lookupByUUIDString(expected_identity)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise StateError("cannot verify libvirt VM removal") from None
        else:
            raise StateError("selected libvirt VM is still present after removal")
        retained_instances.mark_native_removed(roots, archive)
        return archive
    except StateError:
        raise
    except (libvirt.libvirtError, ET.ParseError, AttributeError, TypeError, ValueError):
        raise StateError("libvirt VM removal failed; refresh to inspect its current state") from None
    finally:
        conn.close()


def remove_vm(
    row: dict[str, Any], *, expected_identity: str, roots: state.StatePaths | None = None, volumes: bool = False
) -> dict[str, Any]:
    """Remove only the confirmed native identity; never edit another state root."""
    if (
        not isinstance(expected_identity, str)
        or not expected_identity
        or row.get("removal_identity") != expected_identity
    ):
        raise StateError("selected host VM identity changed; refresh before removing")
    if row.get("runtime_kind") == "oci-root":
        raise StateError("OCI-root removal requires its original managed runtime")
    roots = roots or state.init_roots()
    if row["backend"] in {"lima-vz", "lima-qemu", "lima-krunkit"}:
        archive = _remove_lima(row, expected_identity, roots=roots, volumes=volumes)
        storage_policy = "instance-root-and-topology-purged" if volumes else "instance-root-and-topology-retained"
    elif row["backend"] in {platforms.BACKEND_KVM, platforms.BACKEND_HVF}:
        archive = _remove_libvirt(row, expected_identity, roots=roots, volumes=volumes)
        storage_policy = "libvirt-disks-retained"
    else:
        raise StateError("host VM removal is unavailable for this backend")
    return {
        "id": row["id"],
        "name": row["name"],
        "run_id": row.get("run_id"),
        "backend": row["backend"],
        "runtime_kind": row["runtime_kind"],
        "managed": False,
        "operation": "rm",
        "previous_status": row["status"],
        "status": "removed",
        "storage_policy": storage_policy,
        "retained_instance": archive,
        "retained_volumes": [] if archive is None else [archive],
    }
