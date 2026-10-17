"""Private root snapshots and ordered connection topology, never native adoption.

Lock order: existing run lock (when applicable), artifact reference guard, archive
lock. Readers deliberately do not take run locks. Publication is complete before
native deletion; failed native removal leaves a protected, non-deleteable record.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import state
from .errors import StateError

_SCHEMA = "palimpsest-retained-instance-v1"
_PREFIX = "retained-instance:"


def _directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or path.resolve() != path:
        raise StateError("retained instance directory is not canonical and owner-controlled")
    return info


def _file(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or path.resolve() != path
    ):
        raise StateError("retained instance file is not an owned regular file")
    return info


def _stamp(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def _root(roots: state.StatePaths, *, create: bool = False) -> Path:
    path = roots.state.resolve() / "retained-instances"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        _directory(path)
    return path


def _lock(roots: state.StatePaths):
    return state.file_lock(roots.locks / "retained-instances-v1.lock")


def _topology(ledger: dict[str, Any] | None) -> dict[str, Any]:
    ledger = ledger or {}
    # Do not archive guest credentials/environment in the inspectable manifest.
    result = {
        key: ledger[key]
        for key in ("base", "base_digest", "layers", "source", "runtime_kind", "volumes", "layer_attachment")
        if key in ledger
    }
    result.setdefault("layers", [])
    return result


def _references(value: Any) -> list[str]:
    result: list[str] = []

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            for child_key, child in item.items():
                visit(child, child_key)
        elif isinstance(item, list):
            for child in item:
                visit(child, key)
        elif key.endswith("digest") and item is not None:
            if not isinstance(item, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item):
                raise StateError("invalid retained instance artifact digest")
            if item not in result:
                result.append(item)

    visit(value)
    return result


def _copy(source: Path, target: Path) -> dict[str, Any]:
    before = _file(source)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if _stamp(os.fstat(fd)) != _stamp(before):
            raise StateError("root changed before retention snapshot")
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(out, "wb") as destination, os.fdopen(os.dup(fd), "rb") as origin:
            shutil.copyfileobj(origin, destination, 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        after = os.fstat(fd)
        if (
            _stamp(_file(source)) != _stamp(before)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise StateError("root changed during retention snapshot")
    finally:
        os.close(fd)
    copied = _file(target)
    return {"file": target.name, "source": str(source), "inode": _stamp(copied), "size_bytes": copied.st_size}


def _read(directory: Path) -> dict[str, Any]:
    try:
        info = _directory(directory)
        manifest = directory / "record.json"
        _file(manifest)
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            record["schema"] != _SCHEMA
            or record["identity"] != directory.name
            or record["directory_inode"] != _stamp(info)
            or not re.fullmatch(r"[0-9a-f]{64}", record["identity"])
            or not isinstance(record["name"], str)
            or not record["name"]
            or type(record["native_removed"]) is not bool
            or not isinstance(record["topology"], dict)
            or not isinstance(record["files"], list)
            or type(record["reference_only"]) is not bool
            or not isinstance(record["native_identity"], str)
            or record["backend"] not in {"lima-vz", "lima-qemu", "lima-krunkit", "kvm", "libvirt-hvf"}
        ):
            raise StateError("invalid retained instance record")
        _validate_topology(record["topology"])
        expected = {"record.json"}
        for entry in record["files"]:
            name = entry["file"]
            if not isinstance(name, str) or Path(name).name != name or name in expected:
                raise StateError("invalid retained instance snapshot filename")
            expected.add(name)
            file_info = _file(directory / name)
            if entry["inode"] != _stamp(file_info) or entry["size_bytes"] != file_info.st_size:
                raise StateError("retained instance snapshot identity changed")
        if {child.name for child in directory.iterdir()} != expected:
            raise StateError("retained instance archive has unknown files")
        return record
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError("cannot validate retained instance record; artifact references are unknown") from exc


def _validate_topology(topology: dict[str, Any]) -> None:
    layers = topology.get("layers")
    if not isinstance(layers, list) or any(
        not isinstance(layer, dict)
        or not isinstance(layer.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", layer["digest"])
        for layer in layers
    ):
        raise StateError("invalid retained instance ordered layers")
    if "base" in topology and not isinstance(topology["base"], dict):
        raise StateError("invalid retained instance base topology")
    _references(topology)


def _disk_closure(sources: list[Path], aliases: dict[Path, str]) -> tuple[list[Path], list[dict[str, str]]]:
    """Copy every qcow backing file, then reconnect only the private copies."""
    result = list(sources)
    links: list[dict[str, str]] = []
    for source in sources:
        _file(source)
        with source.open("rb") as stream:
            qcow = stream.read(4) == b"QFI\xfb"
        if not qcow:
            continue
        try:
            response = subprocess.run(
                ["qemu-img", "info", "--output=json", "--backing-chain", str(source)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            chain = json.loads(response.stdout)
            if response.returncode or not isinstance(chain, list) or not chain:
                raise ValueError("invalid backing chain")
            for index, item in enumerate(chain):
                path = Path(item["filename"]).resolve(strict=True)
                if item["format"] not in {"qcow2", "raw"}:
                    raise ValueError("unsupported backing format")
                _file(path)
                if path not in result:
                    result.append(path)
                if index + 1 < len(chain):
                    backing = Path(chain[index + 1]["filename"]).resolve(strict=True)
                    links.append(
                        {
                            "file": aliases.get(path, path.name),
                            "backing": aliases.get(backing, backing.name),
                            "backing_format": chain[index + 1]["format"],
                        }
                    )
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise StateError("cannot preserve root backing chain; native deletion refused") from exc
    if len({aliases.get(path, path.name) for path in result}) != len(result):
        raise StateError("root backing filenames collide; native deletion refused")
    return result, links


def _connect_copies(directory: Path, links: list[dict[str, str]]) -> None:
    for link in links:
        try:
            response = subprocess.run(
                [
                    "qemu-img",
                    "rebase",
                    "-u",
                    "-f",
                    "qcow2",
                    "-F",
                    link["backing_format"],
                    "-b",
                    link["backing"],
                    str(directory / link["file"]),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if response.returncode:
                raise StateError("cannot reconnect retained backing chain; native deletion refused")
        except (OSError, subprocess.SubprocessError) as exc:
            raise StateError("cannot reconnect retained backing chain; native deletion refused") from exc


def _copy_tree(source: Path, target: Path) -> dict[str, Any]:
    before = _directory(source)
    target.touch(mode=0o600, exist_ok=False)
    with tarfile.open(target, "w") as archive:
        root = tarfile.TarInfo(source.name)
        root.type = tarfile.DIRTYPE
        root.mode = stat.S_IMODE(before.st_mode)
        archive.addfile(root)
        for path in sorted(source.rglob("*")):
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                _directory(path)
                member = tarfile.TarInfo(str(path.relative_to(source.parent)))
                member.type = tarfile.DIRTYPE
                member.mode = stat.S_IMODE(info.st_mode)
                archive.addfile(member)
                continue
            _file(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as content:
                opened = os.fstat(content.fileno())
                if _stamp(opened) != _stamp(info):
                    raise StateError("native firmware changed before retention")
                member = tarfile.TarInfo(str(path.relative_to(source.parent)))
                member.size = opened.st_size
                member.mode = stat.S_IMODE(opened.st_mode)
                archive.addfile(member, content)
                after = _file(path)
                if (
                    _stamp(after) != _stamp(opened)
                    or after.st_size != opened.st_size
                    or after.st_mtime_ns != opened.st_mtime_ns
                ):
                    raise StateError("native firmware changed during retention")
    after = _directory(source)
    if _stamp(after) != _stamp(before) or after.st_mtime_ns != before.st_mtime_ns:
        raise StateError("native firmware directory changed during retention")
    with target.open("rb") as content:
        os.fsync(content.fileno())
    info = _file(target)
    return {
        "file": target.name,
        "source": str(source),
        "archive_format": "tar",
        "inode": _stamp(info),
        "size_bytes": info.st_size,
    }


def _publish(
    roots: state.StatePaths,
    *,
    name: str,
    backend: str,
    identity: str,
    run_id: str | None,
    topology: dict[str, Any],
    sources: list[Path],
    native: bytes,
    native_filename: str,
    reference_only: bool,
    directories: list[Path] | None = None,
) -> str:
    # Pin references before inspecting any source or backing chain: an
    # observed native VM may not have a selected-store run ledger to pin them.
    with state.artifact_reference_guard(roots), _lock(roots):
        artifact_sources = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
            elif isinstance(value, str):
                parsed = urlparse(value)
                path = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(value)
                if path.parent.resolve() == roots.store.resolve() / "blobs" / "sha256" and re.fullmatch(
                    r"[0-9a-f]{64}", path.name
                ):
                    artifact_sources.append({"digest": "sha256:" + path.name})

        collect(topology)
        topology = {**topology, "path_artifact_sources": artifact_sources}
        _validate_topology(topology)
        aliases = {source.resolve(strict=True): source.name for source in sources}
        if len(aliases) != len(sources):
            raise StateError("native root files alias each other; native deletion refused")
        topology["native_symlinks"] = [
            {"file": source.name, "target": str(source.resolve(strict=True))}
            for source in sources
            if source.is_symlink()
        ]
        sources, links = _disk_closure(list(aliases), aliases)
        topology = {**topology, "archived_backing_chain": links}
        token = hashlib.sha256(f"{backend}:{identity}:{uuid.uuid4()}".encode()).hexdigest()
        directory = _root(roots, create=True) / token
        directory.mkdir(mode=0o700)
        try:
            files = [_copy(source, directory / aliases.get(source, source.name)) for source in sources]
            for source in directories or []:
                files.append(_copy_tree(source, directory / (source.name + ".tar")))
            _connect_copies(directory, links)
            for entry in files:
                info = _file(directory / entry["file"])
                entry.update(inode=_stamp(info), size_bytes=info.st_size)
                with (directory / entry["file"]).open("rb") as stream:
                    os.fsync(stream.fileno())
            native_path = directory / native_filename
            fd = os.open(native_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(native)
                output.flush()
                os.fsync(output.fileno())
            info = _file(native_path)
            files.append({"file": native_filename, "inode": _stamp(info), "size_bytes": info.st_size})
            record = {
                "schema": _SCHEMA,
                "identity": token,
                "name": name,
                "backend": backend,
                "native_identity": identity,
                "run_id": run_id,
                "topology": topology,
                "files": files,
                "reference_only": reference_only,
                "native_removed": False,
                "directory_inode": _stamp(_directory(directory)),
                "created_at": state.utc_now_iso(),
            }
            state.atomic_write_json(directory / "record.json", record)
            state.fsync_directory(directory)
            state.fsync_directory(directory.parent)
        except Exception:
            # Native removal has not started; the original root is still authoritative.
            shutil.rmtree(directory)
            raise
    return _PREFIX + token


def retain_lima(
    roots: state.StatePaths, instance: dict[str, Any], *, identity: str, ledger: dict[str, Any] | None = None
) -> str:
    from .lima import _instance_run_id

    directory = Path(instance["dir"])
    _directory(directory)
    sources = [
        directory / name
        for name in (
            "basedisk",
            "diffdisk",
            "kernel",
            "initrd",
            "vz-identifier",
            "lima.yaml",
            "vz-efi",
            "nvram",
            "tpm",
            "swtpm",
        )
        if (directory / name).exists() or (directory / name).is_symlink()
    ]
    directories = [source for source in sources if source.is_dir()]
    sources = [source for source in sources if source not in directories]
    if not any(path.name in {"basedisk", "diffdisk"} for path in sources):
        raise StateError("Lima root disk is unavailable; refusing native deletion")
    topology = _topology(ledger)
    config = instance.get("config", {})
    topology["native_sources"] = {key: config[key] for key in ("images", "mounts", "additionalDisks") if key in config}
    topology["native_directories"] = [{"path": str(path), "archive": path.name + ".tar"} for path in directories]
    return _publish(
        roots,
        name=instance["name"],
        backend="lima-" + (instance.get("vmType") or config.get("vmType", "vz")),
        identity=identity,
        run_id=_instance_run_id(instance),
        topology=topology,
        sources=sources,
        native=json.dumps(instance).encode(),
        native_filename="native.json",
        reference_only=False,
        directories=directories,
    )


def retain_libvirt(
    roots: state.StatePaths,
    *,
    name: str,
    backend: str,
    identity: str,
    definition: ET.Element,
    ledger: dict[str, Any] | None = None,
    owned_sources: list[Path] | None = None,
) -> str:
    topology = _topology(ledger)
    topology["native_disks"] = [ET.tostring(disk, encoding="unicode") for disk in definition.findall("./devices/disk")]
    topology["nvram"] = definition.findtext("./os/nvram")
    topology["tpm"] = [ET.tostring(tpm, encoding="unicode") for tpm in definition.findall("./devices/tpm")]
    topology["artifact_sources"] = []
    for source in definition.findall(".//disk//source"):
        path = source.get("file")
        if path:
            candidate = Path(path)
            blob_root = roots.store / "blobs" / "sha256"
            if candidate.parent == blob_root and re.fullmatch(r"[0-9a-f]{64}", candidate.name):
                topology["artifact_sources"].append({"digest": "sha256:" + candidate.name})
    return _publish(
        roots,
        name=name,
        backend=backend,
        identity=identity,
        run_id=ledger.get("run_id") if ledger else None,
        topology=topology,
        sources=owned_sources or [],
        native=ET.tostring(definition),
        native_filename="domain.xml",
        reference_only=not bool(owned_sources),
    )


def mark_native_removed(roots: state.StatePaths, identifier: str) -> None:
    with state.artifact_reference_guard(roots), _lock(roots):
        directory = _archive(roots, identifier)
        record = _read(directory)
        state.atomic_write_json(directory / "record.json", {**record, "native_removed": True})


def _archive(roots: state.StatePaths, identifier: str) -> Path:
    if not isinstance(identifier, str):
        raise StateError("invalid retained instance identifier")
    token = identifier.removeprefix(_PREFIX)
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise StateError("invalid retained instance identifier")
    return _root(roots) / token


def _records(roots: state.StatePaths):
    root = _root(roots)
    if root.exists():
        for directory in sorted(root.iterdir()):
            yield _read(directory)


def list_retained_instances(roots: state.StatePaths) -> list[dict[str, Any]]:
    with _lock(roots):
        return [
            {
                "id": _PREFIX + rec["identity"],
                "name": rec["name"],
                "kind": "retained-instance",
                "backend": rec["backend"],
                "status": "available" if rec["native_removed"] and not rec["reference_only"] else "unavailable",
                "attachments": [] if rec["native_removed"] else [{"name": rec["name"], "run_id": rec["run_id"]}],
                "retention_policy": "retain",
                "source": "retained-instance-ledger",
                "size_bytes": sum(file["size_bytes"] for file in rec["files"]),
                "instance_identity": rec["native_identity"],
                "run_id": rec["run_id"],
                "base_digest": rec["topology"].get("base_digest", rec["topology"].get("base", {}).get("digest")),
                "layers": rec["topology"]["layers"],
                "topology": rec["topology"],
                "reference_only": rec["reference_only"],
                "deletable": not rec["reference_only"],
                "native_removal_confirmed": rec["native_removed"],
                "deletion_requires_native_check": True,
            }
            for rec in _records(roots)
        ]


def artifact_references(roots: state.StatePaths) -> dict[str, list[str]]:
    with _lock(roots):
        result: dict[str, list[str]] = {}
        for rec in _records(roots):
            for digest in _references(rec["topology"]):
                result.setdefault(digest, []).append(f"retained-instance:{rec['identity']} ({rec['name']})")
        return result


def _references_archive(value: Any, directory: Path) -> bool:
    if isinstance(value, dict):
        return any(_references_archive(child, directory) for child in value.values())
    if isinstance(value, list):
        return any(_references_archive(child, directory) for child in value)
    if not isinstance(value, str):
        return False
    decoded = unquote(html.unescape(value))
    if str(directory) in decoded:
        return True
    parsed = urlparse(decoded)
    candidate = Path(parsed.path if parsed.scheme == "file" else decoded)
    return candidate.is_absolute() and candidate.resolve().is_relative_to(directory)


def _assert_detached(directory: Path, record: dict[str, Any], connection: Any = None) -> None:
    from . import cloud_runtime, host_vm_control, kvm, lima
    from .runtime_types import RuntimeBackend

    try:
        if record["backend"].startswith("lima-"):
            for instance in lima._list_instances():
                if (
                    not record["native_removed"]
                    and instance.get("name") == record["name"]
                    or host_vm_control.lima_removal_identity(instance) == record["native_identity"]
                ):
                    raise StateError("native VM removal is unconfirmed; retained root may still be attached")
                if _references_archive(instance, directory):
                    raise StateError("retained root is referenced by a native Lima VM")
        else:
            backend = RuntimeBackend(record["backend"])
            conn = connection if connection is not None else kvm.connect(cloud_runtime._RECONCILE_URIS[backend])
            try:
                domains = conn.listAllDomains(0)
                if not isinstance(domains, (list, tuple)):
                    raise StateError("cannot establish native domain inventory")
                for domain in domains:
                    if domain.UUIDString() == record["native_identity"]:
                        raise StateError("retained root is referenced by a native libvirt VM")
                    definitions = [domain.XMLDesc(0)]
                    persistent = domain.isPersistent()
                    if type(persistent) is not int or persistent not in (0, 1):
                        raise StateError("native domain persistence is uncertain")
                    if persistent:
                        inactive = getattr(kvm._libvirt(), "VIR_DOMAIN_XML_INACTIVE", None)
                        if type(inactive) is not int:
                            raise StateError("native inactive XML inspection is unavailable")
                        definitions.append(domain.XMLDesc(inactive))
                    for definition in definitions:
                        parsed = ET.fromstring(definition)
                        values = [value for node in parsed.iter() for value in [*node.attrib.values(), node.text or ""]]
                        if _references_archive(values, directory):
                            raise StateError("retained root is referenced by a native libvirt VM")
            finally:
                if connection is None:
                    conn.close()
    except StateError:
        raise
    except Exception as exc:
        raise StateError("cannot establish native detachment of retained root") from exc


def delete_retained_instance(
    roots: state.StatePaths, identifier: str, *, confirmation: str, _connection: Any = None
) -> dict[str, Any]:
    # Discover without holding the archive lock while acquiring a run lock.
    # Re-read the archive under the final locks before changing either ledger.
    with _lock(roots):
        observed = _read(_archive(roots, identifier))
    if observed.get("run_id") is not None and state.run_entry_present_or_ambiguous(roots, observed["name"]):
        snapshot = state.read_run_ledger_snapshot(roots, observed["name"])
        if (
            snapshot.record.run_id == observed["run_id"]
            and snapshot.record.dispatch_key.backend.value == observed["backend"]
            and snapshot.state.get("retained_instance") == identifier
        ):
            with state.locked_existing_run(roots, observed["name"], expected_snapshot=snapshot) as mutation:
                return _delete_archive(
                    roots,
                    identifier,
                    confirmation,
                    _connection,
                    observed=observed,
                    mutation=mutation,
                    clear_reference=True,
                )
    return _delete_archive(roots, identifier, confirmation, _connection, observed=observed)


def delete_retained_instance_for_run(
    roots: state.StatePaths,
    identifier: str,
    *,
    mutation: state.ExistingRunMutation,
    _connection: Any = None,
) -> dict[str, Any]:
    """Delete for a caller already holding the exact run lock, before run purge."""
    return _delete_archive(roots, identifier, mutation.record.name, _connection, mutation=mutation)


def _delete_archive(
    roots: state.StatePaths,
    identifier: str,
    confirmation: str,
    connection: Any,
    *,
    observed: dict[str, Any] | None = None,
    mutation: state.ExistingRunMutation | None = None,
    clear_reference: bool = False,
) -> dict[str, Any]:
    with state.artifact_reference_guard(roots), _lock(roots):
        directory = _archive(roots, identifier)
        record = _read(directory)
        if observed is not None and record != observed:
            raise StateError("retained instance changed before deletion")
        if confirmation != record["name"]:
            raise StateError("confirmation must exactly match the retained instance name")
        if record["reference_only"]:
            raise StateError("retained libvirt disks are not owned archives; refusing arbitrary disk deletion")
        if mutation is not None:
            mutation.verify_binding()
            payload = mutation.mutable_state()
            if (
                mutation.paths.root.parent != roots.runs.resolve()
                or mutation.record.name != record["name"]
                or mutation.record.run_id != record["run_id"]
                or mutation.record.dispatch_key.backend.value != record["backend"]
                or payload.get("status") != "removed"
                or payload.get("retained_instance") != identifier
            ):
                raise StateError("retained instance does not match the exact removed run")
        _assert_detached(directory, record, connection)
        if mutation is not None and clear_reference:
            # This primitive requires the artifact guard already held here.
            # Calling mutation.write_state would nest the nonreentrant guard.
            # Publish first: an interrupted archive deletion still pins topology,
            # but can never leave a removed archive referenced by the run.
            state._write_existing_run_mutation_state(
                mutation,
                "removed",
                {**payload, "retained_instance": None, "updated_at": state.utc_now_iso()},
            )
        # Keep record until every owned file is removed. Any interrupted cleanup
        # fails reference queries closed rather than releasing a partial topology.
        for entry in record["files"]:
            path = directory / entry["file"]
            if _stamp(_file(path)) != entry["inode"]:
                raise StateError("retained root identity changed")
            path.unlink()
        (directory / "record.json").unlink()
        directory.rmdir()
        state.fsync_directory(directory.parent)
        return {
            "id": _PREFIX + record["identity"],
            "name": record["name"],
            "status": "removed",
            "storage_policy": "instance-root-and-topology-purged",
        }
