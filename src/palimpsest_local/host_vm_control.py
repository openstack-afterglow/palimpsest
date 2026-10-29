"""Explicit, identity-bound removal of observed VMs, without ledger adoption."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import cloud_runtime, kvm, lima, platforms, retained_instances, state
from .errors import PalimpsestError, StateError
from .runtime_types import RuntimeBackend


@contextmanager
def _physical_directory(path: str):
    descriptors = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        descriptors.append(fd)
        for component in path.split("/")[1:]:
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            descriptors.append(fd)
        yield fd
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _configuration_identity(directory_fd: int) -> tuple[Any, ...]:
    fd = os.open("lima.yaml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or not 0 < before.st_size <= 1024 * 1024
        ):
            raise StateError("selected Lima VM configuration is unsafe")
        content = bytearray()
        while len(content) <= before.st_size:
            chunk = os.read(fd, min(65536, before.st_size + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(fd)
        linked = os.stat("lima.yaml", dir_fd=directory_fd, follow_symlinks=False)

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if (
            len(content) != before.st_size
            or identity(before) != identity(after)
            or identity(before) != identity(linked)
        ):
            raise StateError("selected Lima VM configuration changed")
        content.decode("utf-8")
        return (*identity(before), hashlib.sha256(content).hexdigest())
    finally:
        os.close(fd)


def _directory_removed(fd: int) -> bool:
    held = os.fstat(fd)
    if held.st_nlink == 0:
        return True
    # APFS retains st_nlink on an unlinked open directory. F_GETPATH also follows
    # renames, preventing a renamed surviving VM from becoming a success receipt.
    command = getattr(fcntl, "F_GETPATH", None)
    if command is None:
        return False
    try:
        path = fcntl.fcntl(fd, command, b"\0" * 1024).split(b"\0", 1)[0]
        if not path.startswith(b"/"):
            return False
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return True
        raise
    return (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)


def lima_removal_identity(instance: dict[str, Any]) -> str | None:
    """Fingerprint a physical native instance, including unmarked Lima VMs."""
    try:
        name = instance.get("name")
        path = instance.get("dir")
        config = instance.get("config")
        vm_type = lima._instance_vm_type(instance)
        home = os.path.realpath(os.environ.get("LIMA_HOME", str(Path.home() / ".lima")))
        if (
            not isinstance(name, str)
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or not isinstance(config, dict)
            or instance.get("protected", False) is not False
            or config.get("protected", False) is not False
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
        environment = config.get("env", {})
        if not isinstance(environment, dict) or (
            "PALIMPSEST_RUN_ID" in environment and environment["PALIMPSEST_RUN_ID"] is None
        ):
            return None
        with _physical_directory(path) as fd:
            info = os.fstat(fd)
            linked = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                return None
            identity = (
                "lima-" + vm_type,
                name,
                path,
                info.st_dev,
                info.st_ino,
                lima._instance_run_id(instance),
                _configuration_identity(fd),
                config,
            )
            return hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
    except (OSError, TypeError, ValueError, PalimpsestError):
        return None


def libvirt_removal_identity(domain: Any, *, definition: ET.Element | None, backend: str | None = None) -> str | None:
    """Require positively classified full XML before exposing native removal."""
    if definition is None or definition.tag != "domain":
        return None
    native_type = definition.get("type")
    if native_type not in {"kvm", "hvf"} or (
        backend is not None and native_type != {"kvm": "kvm", "libvirt-hvf": "hvf"}.get(backend)
    ):
        return None
    if definition.find(f"./metadata/{{{kvm.DOMAIN_MARKER_NAMESPACE}}}lifecycle") is not None:
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
    inactive_flag = getattr(libvirt, "VIR_DOMAIN_XML_INACTIVE", None)
    if type(inactive_flag) is not int or inactive_flag <= 0:
        raise StateError("libvirt inactive TPM configuration is unavailable; VM was not stopped")
    inactive = ET.fromstring(domain.XMLDesc(inactive_flag))
    emulators += inactive.findall("./devices/tpm/backend[@type='emulator']")
    keep_nvram = getattr(libvirt, "VIR_DOMAIN_UNDEFINE_KEEP_NVRAM", None)
    if type(keep_nvram) is not int or keep_nvram <= 0:
        raise StateError("libvirt cannot explicitly retain NVRAM; VM was not stopped")
    flags = keep_nvram | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
    if emulators:
        keep_tpm = getattr(libvirt, "VIR_DOMAIN_UNDEFINE_KEEP_TPM", None)
        if type(keep_tpm) is not int or keep_tpm <= 0:
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
    path = current["dir"]
    try:
        with _physical_directory(path) as fd:
            pinned = os.fstat(fd)

            def check() -> dict[str, Any]:
                selected = lima._instance_info_or_none(name)
                if selected is None or lima_removal_identity(selected) != expected_identity:
                    raise StateError("selected Lima VM identity changed; refresh before removing")
                linked = os.stat(selected["dir"], follow_symlinks=False)
                if (pinned.st_dev, pinned.st_ino) != (linked.st_dev, linked.st_ino):
                    raise StateError("selected Lima VM directory changed; refresh before removing")
                return selected

            current = check()
            archive = None
            if not volumes:
                if str(current.get("status", "")).lower() != "stopped":
                    result = lima._run_command(["limactl", "stop", "--force", "--", name], timeout_seconds=60)
                    if result.returncode != 0:
                        raise StateError("cannot stop Lima VM for root retention")
                current = check()
                if str(current.get("status", "")).lower() != "stopped":
                    raise StateError("Lima VM is not stopped; refusing inconsistent root snapshot")
                archive = retained_instances.retain_lima(roots, current, identity=expected_identity, ledger=ledger)
            check()
            result = lima._run_command(["limactl", "delete", "--force", "--", name], timeout_seconds=60)
            if result.returncode != 0:
                raise StateError("Lima VM removal failed; retained root was not discarded")
            if lima._instance_info_or_none(name) is not None or os.path.lexists(path) or not _directory_removed(fd):
                raise StateError("Lima VM removal absence could not be verified; refresh before any further action")
            if archive is not None:
                retained_instances.mark_native_removed(roots, archive)
            return archive
    except OSError:
        raise StateError("selected Lima VM directory is unavailable") from None


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
                or libvirt_removal_identity(domain, definition=definition, backend=backend.value) != expected_identity
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
            if domain.destroy() not in {None, 0}:
                raise StateError("selected libvirt VM did not stop; native undefinition refused")
            if flags is not None and domain.isActive():
                raise StateError("selected libvirt VM did not stop; native undefinition refused")
        if flags is not None:
            flags = check()
            # Retain disk, NVRAM and TPM state; delete only domain-owned metadata.
            if flags is not None:
                if domain.undefineFlags(flags) not in {None, 0}:
                    raise StateError("selected libvirt VM undefinition failed")
        try:
            conn.lookupByUUIDString(expected_identity)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise StateError("cannot verify libvirt VM removal") from None
        else:
            raise StateError("selected libvirt VM is still present after removal")
        try:
            conn.lookupByName(row["name"])
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise StateError("cannot verify libvirt VM name absence") from None
        else:
            raise StateError("selected libvirt VM name is still present after removal")
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
        not isinstance(row, dict)
        or not isinstance(expected_identity, str)
        or not expected_identity
        or row.get("removal_identity") != expected_identity
    ):
        raise StateError("selected host VM identity changed; refresh before removing")
    if row.get("runtime_kind") == "oci-root":
        raise StateError("OCI-root removal requires its original managed runtime")
    if (
        type(volumes) is not bool
        or row.get("managed") is not False
        or row.get("source") != "host-backend"
        or row.get("backend") not in {"lima-vz", "lima-qemu", "lima-krunkit", "kvm", "libvirt-hvf"}
        or not isinstance(row.get("name"), str)
        or not row["name"]
        or len(f"host:{row.get('backend')}:{row['name']}") > 512
        or row["name"] in {".", ".."}
        or "/" in row["name"]
        or "\\" in row["name"]
        or any(ord(char) < 32 or ord(char) == 127 for char in row["name"])
        or row.get("id") != f"host:{row.get('backend')}:{row.get('name')}"
        or row.get("runtime_kind") not in {"external", "cloud-image"}
    ):
        raise StateError("invalid observed VM removal selection")
    if row.get("run_id") is not None:
        try:
            if str(uuid.UUID(row["run_id"])) != row["run_id"]:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise StateError("invalid observed VM run marker") from None
    if row.get("backend") in {"lima-vz", "lima-qemu", "lima-krunkit"}:
        host = platforms.detect_host()
        if (
            host.system != "Darwin"
            or host.machine not in {"aarch64", "x86_64"}
            or (row["backend"] != "lima-qemu" and host.machine != "aarch64")
        ):
            raise StateError("selected Lima backend is not available on this host")
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
