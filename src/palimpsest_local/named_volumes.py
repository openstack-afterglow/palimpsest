"""Independent, format-once data disks with durable exclusive run reservations.

The ledger is independent of run trees. A stopped run keeps its reservation;
only verified native removal (or a proven pre-commit failure) releases it.
All mutations share one process lock and publish the complete ledger atomically.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import kvm, platforms, state
from . import project_volumes as physical
from .errors import LifecycleError, StateError
from .refs import VolumeAttachment
from .runtime_types import RunVolumeIntent
from .state import StatePaths

_SCHEMA = "palimpsest.named-volumes.v1"
_BACKENDS = frozenset(platforms.BACKENDS)


def _directory(roots: StatePaths, *, create: bool = False) -> Path:
    directory = roots.state / "named-volumes"
    if create:
        if directory.is_symlink():
            raise StateError("named volume directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        physical._verify_private_directory(directory, "named volume directory")
    return directory


def _key(name: str, backend: str) -> str:
    physical._validate_name(name, "named volume name")
    if not isinstance(backend, str) or backend not in _BACKENDS:
        raise StateError(f"unsupported named volume backend: {backend}")
    return f"named:{backend}:{name}"


def _physical_roots(roots: StatePaths, backend: str) -> StatePaths:
    result = StatePaths(roots.config, _directory(roots) / backend)
    for directory in (result.state, result.volumes):
        if directory.exists() or directory.is_symlink():
            physical._verify_private_directory(directory, "named physical volume directory")
    return result


def _project(roots: StatePaths) -> str:
    # Separate Lima's global disk namespace from project-managed disks and stores.
    return "named-" + hashlib.sha256(str(roots.state.resolve()).encode()).hexdigest()[:24]


def _read(roots: StatePaths) -> dict[str, Any]:
    directory = _directory(roots)
    if not directory.exists():
        return {"schema": _SCHEMA, "volumes": {}}
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            metadata = os.stat("ledger.json", dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            # Physical disks without their authority are never rediscovered/adopted.
            if any(directory.iterdir()):
                raise StateError("named volume ledger is missing from a nonempty store") from None
            return {"schema": _SCHEMA, "volumes": {}}
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise StateError("named volume ledger ownership is invalid")
        ledger = state._read_pinned_json_object(descriptor, "ledger.json")
    finally:
        os.close(descriptor)
    if set(ledger) != {"schema", "volumes"} or ledger["schema"] != _SCHEMA or not isinstance(ledger["volumes"], dict):
        raise StateError("invalid named volume ledger")
    for key, entry in ledger["volumes"].items():
        if not isinstance(entry, dict):
            raise StateError("invalid named volume entry")
        if key != _key(entry.get("name"), entry.get("backend")):
            raise StateError("named volume identity mismatch")
        physical._validate_size(entry.get("size_bytes"))
        try:
            if str(uuid.UUID(entry["filesystem_uuid"])) != entry["filesystem_uuid"]:
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise StateError("invalid named volume filesystem identity") from None
        if entry.get("phase") not in {"creating", "ready", "deleting"}:
            raise StateError("invalid named volume lifecycle phase")
        attachment = entry.get("attachment")
        if attachment is not None:
            if not isinstance(attachment, dict) or set(attachment) != {"name", "run_id", "mount_path", "read_only"}:
                raise StateError("invalid named volume reservation")
            state.run_paths(roots, attachment["name"])
            if not isinstance(attachment["run_id"], str) or not attachment["run_id"]:
                raise StateError("invalid named volume run identity")
            try:
                RunVolumeIntent(entry["name"], attachment["mount_path"], read_only=attachment["read_only"])
            except (TypeError, ValueError):
                raise StateError("invalid named volume attachment policy") from None
        if entry["phase"] == "ready":
            identity = entry.get("identity")
            if not isinstance(identity, list) or len(identity) != 2 or any(type(v) is not int for v in identity):
                raise StateError("invalid named volume physical identity")
    return ledger


def _write(roots: StatePaths, ledger: dict[str, Any]) -> None:
    state.atomic_write_json(_directory(roots, create=True) / "ledger.json", ledger)


def _lock(roots: StatePaths):
    return state.file_lock(roots.locks / "named-volumes-v1.lock")


def _lima_disk(roots: StatePaths, entry: dict[str, Any], runner: physical.CommandRunner):
    backend_name = physical.lima_backend_name(_project(roots), entry["name"])
    disks = {disk.name: disk for disk in physical.list_lima_disks(runner=runner)}
    disk = disks.get(backend_name)
    if disk is None:
        raise StateError("named Lima disk is missing")
    physical._verify_lima_disk(disk, backend_name, entry["size_bytes"])
    home = Path(os.environ.get("LIMA_HOME", str(Path.home() / ".lima"))).expanduser()
    expected = home / "_disks" / backend_name
    if disk.directory is None or Path(disk.directory) != expected or expected.is_symlink():
        raise StateError("named Lima disk directory identity is uncertain")
    physical._verify_private_directory(expected, "named Lima disk directory")
    return disk, expected / "datadisk"


def _path(roots: StatePaths, entry: dict[str, Any], runner: physical.CommandRunner) -> Path:
    if entry["backend"] in platforms.LIMA_BACKENDS:
        return _lima_disk(roots, entry, runner)[1]
    return physical.kvm_volume_path(_physical_roots(roots, entry["backend"]), _project(roots), entry["name"])


def _label(roots: StatePaths, entry: dict[str, Any]) -> str:
    if entry["backend"] in platforms.LIMA_BACKENDS:
        return "lima-" + physical.lima_backend_name(_project(roots), entry["name"])
    return physical.kvm_volume_label(_project(roots) + "-" + entry["backend"], entry["name"])


def _identity(path: Path) -> list[int]:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise StateError("named volume physical file ownership is invalid")
    return [metadata.st_dev, metadata.st_ino]


def _verify(roots: StatePaths, entry: dict[str, Any], runner: physical.CommandRunner) -> Path:
    if entry["phase"] != "ready":
        raise StateError("named volume has an incomplete or uncertain lifecycle")
    path = _path(roots, entry, runner)
    if _identity(path) != entry["identity"]:
        raise StateError("named volume physical file identity changed")
    physical._verify_kvm_path(path, entry["size_bytes"], _label(roots, entry), runner)
    # Mutable ext4 superblock checksums change during normal use; only the
    # immutable filesystem UUID is compared after creation.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        actual = os.pread(descriptor, 16, 1024 + 104)
    finally:
        os.close(descriptor)
    if actual != uuid.UUID(entry["filesystem_uuid"]).bytes:
        raise StateError("named volume filesystem UUID changed")
    return path


def _unreferenced(roots: StatePaths, entry: dict[str, Any], path: Path, runner: physical.CommandRunner) -> None:
    if entry["backend"] in platforms.LIMA_BACKENDS:
        disk, _ = _lima_disk(roots, entry, runner)
        physical._allowed_attachment(disk, None)
        return
    uri = "qemu:///session" if entry["backend"] == "libvirt-hvf" else "qemu:///system"
    connection = kvm.connect(uri)
    try:
        domains = connection.listAllDomains(0)
        if not isinstance(domains, (list, tuple)):
            raise StateError("cannot prove named disk has no native references")
        expected = path.resolve()
        for domain in domains:
            try:
                persistent = domain.isPersistent()
                if type(persistent) is not int or persistent not in {0, 1}:
                    raise StateError("native domain persistence is uncertain")
                definitions = [ET.fromstring(domain.XMLDesc(0))]
                if persistent:
                    inactive = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
                    if type(inactive) is not int:
                        raise StateError("native inactive XML inspection is unavailable")
                    definitions.append(ET.fromstring(domain.XMLDesc(inactive)))
            except Exception as exc:
                raise StateError("cannot inspect live and persistent native disk references") from exc
            for definition in definitions:
                # Include every nested backingStore/dataStore, not only the
                # top-level guest disk. Persistent-only hot-unplugged disks
                # remain references even while the live domain omits them.
                for source in definition.findall("./devices/disk//source"):
                    if source.get("pool") is not None or source.get("volume") is not None:
                        raise StateError("cannot resolve native storage-pool disk references")
                    for attribute in ("file", "dev"):
                        candidate = source.get(attribute)
                        if candidate:
                            native_path = Path(candidate)
                            if not native_path.is_absolute():
                                raise StateError("relative native disk reference is uncertain")
                            if native_path.resolve() == expected:
                                raise StateError(
                                    "named volume is referenced by a native domain (including stopped domains)"
                                )
    except StateError:
        raise
    except Exception as exc:
        raise StateError("cannot prove named disk has no native references") from exc
    finally:
        connection.close()


def _row(entry: dict[str, Any], *, unavailable: bool = False) -> dict[str, Any]:
    attachment = entry["attachment"]
    return {
        "id": _key(entry["name"], entry["backend"]),
        "name": entry["name"],
        "kind": "named",
        "backend": entry["backend"],
        "status": "unavailable"
        if unavailable or entry["phase"] != "ready"
        else ("attached" if attachment else "available"),
        "size_bytes": entry["size_bytes"],
        "attachments": [dict(attachment)] if attachment else [],
        "source": "named-volume-ledger",
        "retention_policy": "retain",
        "filesystem": "ext4",
        "filesystem_uuid": entry["filesystem_uuid"],
    }


def list_named_volumes(roots: StatePaths) -> list[dict[str, Any]]:
    """Read-only independent inventory; never create directories or acquire locks."""
    ledger = _read(roots)
    rows = []
    for _, entry in sorted(ledger["volumes"].items()):
        unavailable = False
        try:
            path = _path(roots, entry, physical._default_runner)
            unavailable = entry.get("identity") != _identity(path) or path.stat().st_size != entry["size_bytes"]
        except (LifecycleError, StateError, OSError):
            unavailable = True
        rows.append(_row(entry, unavailable=unavailable))
    return rows


def create_named_volume(
    roots: StatePaths,
    *,
    name: str,
    backend: str,
    size_bytes: int,
    runner: physical.CommandRunner = physical._default_runner,
) -> dict[str, Any]:
    key = _key(name, backend)
    physical._validate_size(size_bytes)
    with _lock(roots):
        ledger = _read(roots)
        if key in ledger["volumes"]:
            raise StateError("named volume already exists")
        if backend in platforms.LIMA_BACKENDS and any(
            entry["backend"] in platforms.LIMA_BACKENDS and entry["name"] == name
            for entry in ledger["volumes"].values()
        ):
            raise StateError("named Lima volume already exists in the shared backend namespace")
        physical.preflight_kvm_volume_support(runner=runner)
        if backend in platforms.LIMA_BACKENDS:
            physical.preflight_lima_volume_support(runner=runner)
        entry = {
            "name": name,
            "backend": backend,
            "size_bytes": size_bytes,
            "filesystem_uuid": str(uuid.uuid4()),
            "phase": "creating",
            "attachment": None,
        }
        _directory(roots, create=True)
        ledger["volumes"][key] = entry
        _write(roots, ledger)
        physical_roots = _physical_roots(roots, backend)
        for directory in (physical_roots.state, physical_roots.volumes):
            directory.mkdir(mode=0o700, exist_ok=True)
            physical._verify_private_directory(directory, "named physical volume directory")
        # A failed native creation leaves its explicit uncertain owner row. It
        # is never adopted or silently retried under the same name.
        if backend in platforms.LIMA_BACKENDS:
            created = physical.ensure_lima_volume(
                _physical_roots(roots, backend), _project(roots), name, size_bytes, runner=runner
            )
            if not created.created:
                raise StateError("refusing to adopt an existing Lima disk")
            disk, path = _lima_disk(roots, entry, runner)
            physical._allowed_attachment(disk, None)
            original = _identity(path)
            formatted = path.parent / ".palimpsest-named-formatted.raw"
            if formatted.exists() or formatted.is_symlink():
                raise StateError("unexpected named disk creation artifact")
            physical._ensure_ext4_raw_file_locked(
                formatted, size_bytes, _label(roots, entry), name, runner, filesystem_uuid=entry["filesystem_uuid"]
            )
            disk, current = _lima_disk(roots, entry, runner)
            physical._allowed_attachment(disk, None)
            if current != path or _identity(path) != original:
                raise StateError("Lima disk changed during format-once publication")
            os.replace(formatted, path)
            state.fsync_directory(path.parent)
        else:
            path = _path(roots, entry, runner)
            if path.exists() or path.is_symlink():
                raise StateError("refusing to adopt an existing raw disk")
            physical._ensure_ext4_raw_file_locked(
                path, size_bytes, _label(roots, entry), name, runner, filesystem_uuid=entry["filesystem_uuid"]
            )
        entry.update(phase="ready", identity=_identity(path))
        _verify(roots, entry, runner)
        _write(roots, ledger)
        return _row(entry)


def reserve_named_volumes(
    roots: StatePaths,
    *,
    name: str,
    run_id: str,
    backend: str,
    intents: tuple[RunVolumeIntent, ...],
    runner: physical.CommandRunner = physical._default_runner,
) -> tuple[VolumeAttachment, ...]:
    """Atomically claim precreated disks; run_id is a fresh pending token."""
    state.run_paths(roots, name)
    if not isinstance(run_id, str) or not run_id:
        raise StateError("named volume reservation requires an exact identity")
    if not isinstance(intents, tuple) or not all(isinstance(item, RunVolumeIntent) for item in intents):
        raise StateError("invalid named volume intents")
    if len({item.name for item in intents}) != len(intents) or len({item.target for item in intents}) != len(intents):
        raise StateError("duplicate named volume attachment")
    with _lock(roots):
        ledger = _read(roots)
        attachments = []
        for intent in intents:
            entry = ledger["volumes"].get(_key(intent.name, backend))
            if entry is None:
                raise StateError(f"named volume {intent.name!r} must be created before attachment")
            if entry["attachment"] is not None:
                raise StateError(f"named volume {intent.name!r} is already reserved; stopped runs retain their disks")
            path = _verify(roots, entry, runner)
            _unreferenced(roots, entry, path, runner)
            attachment = VolumeAttachment(
                intent.name,
                intent.target,
                read_only=intent.read_only,
                **(
                    {"backend_name": physical.lima_backend_name(_project(roots), intent.name)}
                    if backend in platforms.LIMA_BACKENDS
                    else {"host_path": path}
                ),
            )
            attachments.append(attachment)
        for intent in intents:
            ledger["volumes"][_key(intent.name, backend)]["attachment"] = {
                "name": name,
                "run_id": run_id,
                "mount_path": intent.target,
                "read_only": intent.read_only,
            }
        if intents:
            _write(roots, ledger)
        return tuple(attachments)


def _declared_volumes(snapshot: Any) -> tuple[Mapping[str, Any], ...]:
    values = snapshot.state.get("volumes", ())
    if not isinstance(values, (tuple, list)) or any(not isinstance(item, Mapping) for item in values):
        raise StateError("run has uncertain declared data disk references")
    return tuple(values)


def _source_matches(roots: StatePaths, entry: dict[str, Any], volume: Mapping[str, Any]) -> bool:
    if entry["backend"] in platforms.LIMA_BACKENDS:
        return volume.get("backend_name") == physical.lima_backend_name(_project(roots), entry["name"])
    expected = physical.kvm_volume_path(_physical_roots(roots, entry["backend"]), _project(roots), entry["name"])
    return volume.get("host_path") == str(expected)


def bind_named_volume_reservations(roots: StatePaths, *, name: str, reservation_id: str, run_id: str) -> None:
    """Bind a pending claim to the actual committed run, never a guessed UUID."""
    snapshot = state.read_run_ledger_snapshot(roots, name)
    if snapshot.record.run_id != run_id or snapshot.state.get("status") == "removed":
        raise StateError("named volume binding does not match committed run")
    with _lock(roots):
        ledger = _read(roots)
        declared = _declared_volumes(snapshot)
        changed = False
        for entry in ledger["volumes"].values():
            attachment = entry["attachment"]
            if attachment and attachment["name"] == name and attachment["run_id"] == reservation_id:
                if entry["backend"] != snapshot.record.dispatch_key.backend.value or not any(
                    _source_matches(roots, entry, volume)
                    and volume.get("name") == entry["name"]
                    and volume.get("mount_path") == attachment["mount_path"]
                    and volume.get("read_only") is attachment["read_only"]
                    for volume in declared
                ):
                    raise StateError("committed run does not declare the reserved physical data disk")
                attachment["run_id"] = run_id
                changed = True
        if changed:
            _write(roots, ledger)


def validate_named_volume_reservations(
    roots: StatePaths, *, name: str, run_id: str, runner: physical.CommandRunner = physical._default_runner
) -> None:
    """Recheck immutable physical identities before restarting a reserved VM."""
    if not any(
        entry["attachment"] and entry["attachment"]["name"] == name for entry in _read(roots)["volumes"].values()
    ):
        return
    with _lock(roots):
        ledger = _read(roots)
        for entry in ledger["volumes"].values():
            attachment = entry["attachment"]
            if attachment is None or attachment["name"] != name:
                continue
            if attachment["run_id"] != run_id:
                raise StateError("named volume reservation belongs to another run identity")
            _verify(roots, entry, runner)


def release_named_volume_reservations(
    roots: StatePaths, *, name: str, run_id: str, runner: physical.CommandRunner = physical._default_runner
) -> None:
    """Release only after run removal/precommit absence AND native detach proof."""
    if not any(
        entry["attachment"] and entry["attachment"]["name"] == name and entry["attachment"]["run_id"] == run_id
        for entry in _read(roots)["volumes"].values()
    ):
        return
    with _lock(roots):
        ledger = _read(roots)
        entries = [
            entry
            for entry in ledger["volumes"].values()
            if entry["attachment"] and entry["attachment"]["name"] == name and entry["attachment"]["run_id"] == run_id
        ]
        if not entries:
            return
        if state.run_entry_present_or_ambiguous(roots, name):
            snapshot = state.read_run_ledger_snapshot(roots, name)
            removed = snapshot.state.get("status") == "removed"
            pending_without_commit = run_id.startswith("pending-") and not any(
                _source_matches(roots, entry, volume) for entry in entries for volume in _declared_volumes(snapshot)
            )
            if not (
                removed
                and (snapshot.record.run_id == run_id or run_id.startswith("pending-"))
                or pending_without_commit
            ):
                raise StateError("named volume reservation remains until exact run removal is proven")
        for entry in entries:
            path = _verify(roots, entry, runner)
            _unreferenced(roots, entry, path, runner)
        for entry in entries:
            entry["attachment"] = None
        _write(roots, ledger)


def delete_named_volume(
    roots: StatePaths, *, name: str, backend: str, runner: physical.CommandRunner = physical._default_runner
) -> dict[str, Any]:
    key = _key(name, backend)
    with _lock(roots):
        ledger = _read(roots)
        entry = ledger["volumes"].get(key)
        if entry is None:
            raise StateError("named volume does not exist")
        if entry["attachment"] is not None:
            raise StateError("cannot delete a reserved named volume")
        path = _verify(roots, entry, runner)
        _unreferenced(roots, entry, path, runner)
        entry["phase"] = "deleting"
        _write(roots, ledger)
        if backend in platforms.LIMA_BACKENDS:
            physical.delete_lima_volume(
                _physical_roots(roots, backend), _project(roots), name, entry["size_bytes"], runner=runner
            )
        else:

            def validate_identity(checked: Path) -> None:
                if _identity(checked) != entry["identity"]:
                    raise StateError("named volume physical identity changed during deletion")

            def validate_quarantine(original: Path, quarantined: Path) -> None:
                validate_identity(quarantined)
                _unreferenced(roots, entry, original, runner)
                _unreferenced(roots, entry, quarantined, runner)

            deleted = physical._delete_ext4_raw_file_locked(
                path,
                entry["size_bytes"],
                _label(roots, entry),
                name,
                runner,
                quarantine_validator=validate_quarantine,
                access_validator=validate_identity,
            )
            if not deleted:
                raise StateError("named volume disappeared during deletion; authority retained")
        del ledger["volumes"][key]
        _write(roots, ledger)
        return {"id": key, "name": name, "backend": backend, "status": "removed", "kind": "named"}
