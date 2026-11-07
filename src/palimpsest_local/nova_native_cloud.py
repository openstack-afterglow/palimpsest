"""Explicit, grant-bound private Nova native-root proof transactions.

No acquisition, source installation or consumer launch happens implicitly. An
uncertain provider POST stays in the environment ledger and is never replayed.
The bootstrap service is an authenticated delivery channel, not attestation.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import io
import ipaddress
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import tarfile
import time
import uuid
from datetime import UTC
from pathlib import Path

from .cloud_env import WORKER_SCRATCH, CloudEnvError, _validate_registry_image, package_archive

GRANT_SCHEMA = "palimpsest.nova-native-grant.v1"
RUN_SCHEMA = "palimpsest.nova-native-run.v1"
GPU_COMMAND = ["/run/palimpsest-probe/cuda-probe"]
KINDS = ("ports", "images", "volumes", "access_rules")


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def validate_probe_glibc(symbol_versions: bytes) -> str:
    """The pinned Ubuntu22.04 image supplies glibc2.35, not the build host's ABI."""
    if not isinstance(symbol_versions, bytes) or len(symbol_versions) > 1024 * 1024:
        raise CloudEnvError("probe symbol-version evidence is invalid")
    try:
        labels = set(re.findall(r"\bGLIBC_([A-Za-z0-9_.]+)", symbol_versions.decode("ascii")))
    except UnicodeDecodeError:
        raise CloudEnvError("probe symbol-version evidence is invalid") from None
    if not labels or any(re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", label) is None for label in labels):
        raise CloudEnvError("probe requires unsupported or unverified glibc symbol versions")
    versions = [tuple(int(part) for part in label.split(".")) for label in labels]
    highest = max(version + (0,) * (3 - len(version)) for version in versions)
    if highest > (2, 35, 0):
        raise CloudEnvError("compiled probe requires newer glibc than the original Ubuntu22.04 image")
    return ".".join(str(part) for part in (highest if highest[2] else highest[:2]))


def _read_private_json(path: Path, *, maximum: int | None) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or (maximum is not None and before.st_size > maximum)
        ):
            raise CloudEnvError("native grant/config/state must be an owned private regular file")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise CloudEnvError("duplicate native private JSON key")
                result[key] = value
            return result

        result = json.load(stream, object_pairs_hook=unique)
        after, current = os.fstat(stream.fileno()), path.stat(follow_symlinks=False)

        def identity(info):
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise CloudEnvError("native private JSON changed while reading; HOLD")
    if not isinstance(result, dict):
        raise CloudEnvError("native private JSON must be an object; HOLD")
    return result


def _private_json(path: Path) -> dict:
    return _read_private_json(path, maximum=1024 * 1024)


def _private_state_json(path: Path) -> dict:
    # The owned append-preserving journal grows with successful runs and their
    # absence evidence. Its source fields are bounded at admission, not by a
    # grant/config-sized cap that can strand an already-created consumer.
    return _read_private_json(path, maximum=None)


def _native_plan(image, command, phase, project_id, run_id, network, storage=None):
    plan = {
        "run_id": run_id,
        "server_id": "00000000-0000-0000-0000-000000000000",
        "project_id": project_id,
        "carrier_digest": "sha256:" + "0" * 64,
        "image": {key: image[key] for key in ("source", "runtime", "layers")},
        "network": network,
        "upper": {"label": "PAL-UPPER"},
        "gpu": {
            "enabled": phase == "gpu",
            "kernel_release": "6.8.0-147-generic",
            "driver_version": "580.178.04",
            "expected_pci_alias": "RTX-2000Ada:1,AD107-audio:1",
        },
        "command": command,
    }
    if storage is not None:
        plan["storage"] = {key: storage[key] for key in ("export", "fs_name", "data_pool", "namespace")}
    return plan


def _native_seed(run_id, nonce, project_id, source_digest, worker_address, deadline):
    return {
        "run_id": run_id,
        "nonce": nonce,
        "worker_address": worker_address,
        **{
            key: "/bootstrap/" + key
            for key in ("worker_known_hosts", "ssh_private_key", "client_cert", "client_key", "ca_file")
        },
        "tls_server_name": "palimpsest-bootstrap",
        "bootstrap_url": "https://127.0.0.1:9443",
        "deadline": deadline,
        "artifacts_manifest": "/bootstrap/artifacts.json",
        "expected_plan_digest": "sha256:" + "0" * 64,
        "carrier_digest": "sha256:" + "0" * 64,
        "project_id": project_id,
        "expected_image_digest": source_digest,
    }


def native_prepared_dns(state: dict) -> list[str]:
    dns = state.get("native", {}).get("preparation", {}).get("config", {}).get("dns") or state.get("dns")
    if not isinstance(dns, list) or not dns:
        raise CloudEnvError("native delivery requires the actual prepared DNS policy before admission")
    return dns


def native_delivery_preflight(
    image: dict,
    command: list[str],
    phase: str,
    project_id: str,
    *,
    dns: list[str] | None = None,
    storage: dict | None = None,
) -> None:
    """Apply the real enrollment/guest consumers before SQL intent or effects.

    Keep the complete canonical manifest, including metadata. Reserve the full
    consumer-allowed encoded size of later Ceph credentials, rather than drop
    metadata or enlarge the bootstrap service's fixed admission bounds.
    """
    from . import nova_bootstrap_service as service
    from . import nova_ceph_boot as boot

    try:
        role = "native-" + "0" * 32
        source = image["source"]
        source_digest = source["manifest_digest"] if source.get("kind") == "published" else source["root_digest"]
        if source.get("kind") == "published":
            boot.validate_image_projection(image, source_digest)
        network = {
            "address": "255.255.255.255",
            "cidr": "255.255.255.255/32",
            "gateway": "255.255.255.255",
            "dns": ["255.255.255.255"] * 8 if dns is None else dns,
        }
        plan = _native_plan(image, command, phase, project_id, role, network, storage)
        seed = _native_seed(role, "0" * 32, project_id, source_digest, network["address"], 2**53 - 1)
        seed["expected_plan_digest"] = boot.plan_policy_digest(plan)
        boot.validate_seed(boot.strict_json(_canonical(seed), maximum=65536))
        boot.runtime_vector(image["runtime"], command)
        enrollment = {
            "schema": service.ENROLLMENT_SCHEMA,
            "run_id": role,
            "client_cert_sha256": "0" * 64,
            "expires_unix": seed["deadline"],
            "revoked": False,
            "plan": plan,
            "ceph_access": {key: "\U00010000" * 4096 for key in ("export", "name", "secret")},
        }
        if storage is not None:
            enrollment["ceph_access"].update(export=storage["export"], fs_name=storage["fs_name"])
        _validate_native_enrollment(enrollment)
    except (service.BootstrapError, boot.BootError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise CloudEnvError("native delivery exceeds bootstrap enrollment/plan/seed admission bounds") from exc


def _validate_native_enrollment(enrollment: dict) -> None:
    from . import nova_bootstrap_service as service
    from . import nova_ceph_boot as boot

    encoded = _canonical(enrollment)
    if len(encoded) > service.MAX_ENROLLMENT_BYTES:
        raise CloudEnvError("native delivery exceeds bootstrap enrollment admission bounds")
    service._validate_enrollment(service._decode(encoded), enrollment["run_id"])
    response = {
        "schema": service.BOOTSTRAP_SCHEMA,
        "run_id": enrollment["run_id"],
        "nonce": "0" * 32,
        "expires_unix": enrollment["expires_unix"],
        "plan": enrollment["plan"],
        "ceph_access": enrollment["ceph_access"],
    }
    boot.strict_json(service._encode(response), maximum=min(service.MAX_JSON_BYTES, boot.MAX_JSON))


def _uuid(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise CloudEnvError("native provider identity must be an exact canonical UUID") from None
    return value


def _remove_authorized_key(path: str, line: bytes):
    """Standalone worker transaction: never replace an incomplete or changed key file."""
    import os
    import stat
    import uuid

    def identity(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
        )

    parent, name = os.path.split(path)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source = staging = None
    temporary = None
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(source)
        limit = 1024 * 1024
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise RuntimeError("authorized_keys is unsafe or exceeds the complete-read bound; HOLD")
        chunks = []
        count = 0
        while True:
            chunk = os.read(source, min(65536, limit + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > limit:
                raise RuntimeError("authorized_keys exceeds the complete-read bound; HOLD")
        if count != before.st_size or identity(os.fstat(source)) != identity(before):
            raise RuntimeError("authorized_keys changed or was truncated during read; HOLD")
        data = b"".join(chunks)
        kept = b"".join(item for item in data.splitlines(keepends=True) if item.rstrip(b"\r\n") != line)
        if kept == data:
            return
        candidate = name + ".native-remove-" + uuid.uuid4().hex
        staging = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        temporary = candidate
        os.fchown(staging, before.st_uid, before.st_gid)
        os.fchmod(staging, stat.S_IMODE(before.st_mode))
        view = memoryview(kept)
        while view:
            written = os.write(staging, view)
            if written <= 0:
                raise RuntimeError("authorized_keys staging write was incomplete; HOLD")
            view = view[written:]
        os.fsync(staging)
        if identity(os.fstat(source)) != identity(before) or identity(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ) != identity(before):
            raise RuntimeError("authorized_keys changed before replacement; HOLD")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
    finally:
        if staging is not None:
            os.close(staging)
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
        if source is not None:
            os.close(source)
        os.close(directory)


def _preserve_and_replace_source(package_path: str, root_path: str, data: bytes, old_sha: str, new_sha: str) -> dict:
    """Standalone stdlib-only worker transaction; never import predecessor code."""
    import hashlib
    import io
    import json
    import os
    import re
    import shutil
    import stat
    import tarfile
    import uuid
    from pathlib import Path

    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in (old_sha, new_sha)):
        raise RuntimeError("source identity missing; HOLD")
    if len(data) > 256 * 1024**2 or hashlib.sha256(data).hexdigest() != new_sha:
        raise RuntimeError("new source archive identity differs; HOLD")
    package, root = Path(package_path), Path(root_path)

    def directory(path):
        if not path.is_absolute() or ".." in path.parts:
            raise RuntimeError("source directory is not canonical; HOLD")
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise RuntimeError("source directory ownership/mode differs; HOLD")
        finally:
            os.close(descriptor)

    def snapshot(path):
        directory(path)
        files, directories, contents = {}, {}, {}

        def walk(current, relative="", cache=False):
            before = current.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o022
            ):
                raise RuntimeError("source directory drift or link; HOLD")
            directories[relative] = {
                "mode": stat.S_IMODE(before.st_mode),
                "uid": before.st_uid,
                "gid": before.st_gid,
                "mtime_ns": before.st_mtime_ns,
            }
            names = sorted(os.listdir(current))
            for name in names:
                child = current / name
                label = relative + "/" + name if relative else name
                info = child.stat(follow_symlinks=False)
                child_cache = cache or name == "__pycache__"
                if stat.S_ISDIR(info.st_mode):
                    walk(child, label, child_cache)
                    if not child_cache and not any(key.startswith(label + "/") for key in contents):
                        raise RuntimeError("unaccounted empty source directory; HOLD")
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022
                    or info.st_size > 64 * 1024**2
                    or (not child_cache and child.suffix in {".pyc", ".pyo"})
                ):
                    raise RuntimeError("source extra, link, ownership or file type differs; HOLD")
                fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    payload = stream.read(64 * 1024**2 + 1)
                    after = os.fstat(stream.fileno())

                def identity(item):
                    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns

                if (
                    identity(info) != identity(opened)
                    or identity(opened) != identity(after)
                    or len(payload) != info.st_size
                ):
                    raise RuntimeError("source changed during read; HOLD")
                files[label] = {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": info.st_size,
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "mtime_ns": info.st_mtime_ns,
                    "atime_ns": info.st_atime_ns,
                    "ctime_ns": info.st_ctime_ns,
                    "inode": info.st_ino,
                    "device": info.st_dev,
                    "ignored_owned_cache": child_cache,
                }
                if not child_cache:
                    contents[label] = payload
            after = current.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or names != sorted(os.listdir(current)):
                raise RuntimeError("source directory changed during read; HOLD")

        walk(path)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, payload in sorted(contents.items(), key=lambda item: Path(item[0]).parts):
                item = tarfile.TarInfo("palimpsest_local/" + name)
                item.uid = item.gid = 0
                item.uname = item.gname = "root"
                item.mtime = 0
                item.mode = 0o644
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
        canonical = buffer.getvalue()
        return canonical, {"files": files, "directories": directories}

    directory(root)
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise RuntimeError("source evidence directory must be private; HOLD")
    old_data, old_manifest = snapshot(package)
    if hashlib.sha256(old_data).hexdigest() != old_sha:
        raise RuntimeError("installed predecessor source differs from its recorded canonical hash; HOLD")
    prior = root / ("source-before-" + old_sha + ".prior")
    staging = package.with_name("." + package.name + ".after-" + new_sha + ".stage")
    displaced = staging / "predecessor"
    journal = root / ("source-install-" + new_sha + ".json")
    record = {
        "schema": "palimpsest.native-source-install.v1",
        "status": "intent",
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "prior": str(prior),
        "staging": str(staging),
        "displaced": str(displaced),
        "package": str(package),
    }

    def write_private(path, payload):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def sync_path(path, directory_only=False):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory_only:
            flags |= os.O_DIRECTORY
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def durable_tree(path):
        for child in sorted(path.rglob("*"), reverse=True):
            sync_path(child, child.is_dir())
        sync_path(path, True)

    def persist(status, initial=False):
        record["status"] = status
        destination = journal if initial else journal.with_name(journal.name + "." + uuid.uuid4().hex)
        write_private(destination, json.dumps(record, sort_keys=True).encode())
        if not initial:
            os.replace(destination, journal)
        sync_path(root, True)

    persist("intent", initial=True)
    try:
        prior.mkdir(mode=0o700)
        shutil.copytree(package, prior / "package", copy_function=shutil.copy2, symlinks=True)
        copied_data, copied_manifest = snapshot(prior / "package")
        if copied_data != old_data or set(copied_manifest["files"]) != set(old_manifest["files"]):
            raise RuntimeError("predecessor backup content differs; HOLD")
        fields = ("sha256", "size", "mode", "uid", "gid", "mtime_ns", "ignored_owned_cache")
        if (
            any(
                any(copied_manifest["files"][name][key] != values[key] for key in fields)
                for name, values in old_manifest["files"].items()
            )
            or copied_manifest["directories"] != old_manifest["directories"]
        ):
            raise RuntimeError("predecessor backup metadata differs; HOLD")
        write_private(prior / "source.tar", old_data)
        write_private(prior / "metadata.json", json.dumps(old_manifest, sort_keys=True).encode())
        durable_tree(prior)
        staging.mkdir(mode=0o700)
        fresh = staging / "palimpsest_local"
        fresh.mkdir(mode=0o755)
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                parts = member.name.split("/")
                if (
                    not member.isfile()
                    or len(parts) < 2
                    or parts[0] != "palimpsest_local"
                    or any(part in ("", ".", "..", "__pycache__") for part in parts)
                    or member.name in seen
                    or member.size > 64 * 1024**2
                    or Path(member.name).suffix in {".pyc", ".pyo"}
                ):
                    raise RuntimeError("new source archive contains extra or unsafe entries; HOLD")
                seen.add(member.name)
                target = fresh.joinpath(*parts[1:])
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                incoming = archive.extractfile(member)
                if incoming is None:
                    raise RuntimeError("new source member unreadable; HOLD")
                write_private(target, incoming.read())
                os.chmod(target, 0o644)
        staged_data, _ = snapshot(fresh)
        current_data, current_manifest = snapshot(package)
        # Reading may update atime; every content/ownership/change identity remains pinned.
        stable_fields = (
            "sha256",
            "size",
            "mode",
            "uid",
            "gid",
            "mtime_ns",
            "ctime_ns",
            "inode",
            "device",
            "ignored_owned_cache",
        )
        same_old = (
            current_data == old_data
            and current_manifest["directories"] == old_manifest["directories"]
            and set(current_manifest["files"]) == set(old_manifest["files"])
            and all(
                all(current_manifest["files"][name][key] == values[key] for key in stable_fields)
                for name, values in old_manifest["files"].items()
            )
        )
        if hashlib.sha256(staged_data).hexdigest() != new_sha or not same_old:
            raise RuntimeError("new stage or installed predecessor drifted; HOLD")
        durable_tree(fresh)
        sync_path(staging, True)
        sync_path(package.parent, True)
        persist("verified_backup_and_stage")
        os.rename(package, displaced)
        sync_path(staging, True)
        sync_path(package.parent, True)
        persist("predecessor_renamed")
        os.rename(fresh, package)
        sync_path(staging, True)
        sync_path(package.parent, True)
        activated_data, _ = snapshot(package)
        if hashlib.sha256(activated_data).hexdigest() != new_sha:
            raise RuntimeError("activated source identity differs; HOLD without automatic rollback")
        persist("activated")
        return {**record, "journal": str(journal), "installed_sha256": new_sha}
    except BaseException:
        persist("held")
        raise


class _HashingStream(io.RawIOBase):
    """One-pass SDK upload; never retain the carrier in controller memory."""

    def __init__(self, source, deadline: float | None = None):
        self.source = source
        self.sha256 = hashlib.sha256()
        self.count = 0
        self.deadline = deadline

    def readable(self):
        return True

    def read(self, size=-1):
        # Requests may request all data: keep the stream bounded even then.
        size = 1024 * 1024 if size is None or size < 0 else min(size, 1024 * 1024)
        if self.deadline is not None and size:
            import select

            remaining = self.deadline - time.time()
            if remaining <= 0 or not select.select([self.source.fileno()], [], [], remaining)[0]:
                raise CloudEnvError("carrier upload stream deadline expired; exact image retained")
            data = os.read(self.source.fileno(), size)
        else:
            data = self.source.read(size)
        self.sha256.update(data)
        self.count += len(data)
        return data


class NativeCloud:
    """Uses a CloudEnv under its existing operation flock and fresh authentication."""

    def __init__(self, env):
        self.env = env
        self._native_ceph_secrets = {}
        self.base_directory = env.directory / "native"
        self.base_remote = f"{WORKER_SCRATCH}/native-{env.state.get('marker', '')}"
        prepared = self.state.get("preparation", {})
        self.directory = Path(prepared.get("directory", self.base_directory))
        self.remote = prepared.get("remote", self.base_remote)
        generation = "generation-" + prepared.get("package_sha256", "")
        if (self.directory, self.remote) not in (
            (self.base_directory, self.base_remote),
            (self.base_directory / generation, self.base_remote + "/" + generation),
        ):
            raise CloudEnvError("native preparation paths are outside their exact environment generation; HOLD")

    @property
    def state(self):
        return self.env.state.setdefault("native", {"grants": {}, "runs": {}})

    def _save(self):
        self.env.save()

    def _remember_certificate_hold(self, error: BaseException, record: dict):
        from .nova_bootstrap_service import CertificateProcessHold

        if not isinstance(error, CertificateProcessHold):
            return
        holds = getattr(self, "certificate_holds", None)
        if holds is None:
            self.certificate_holds = holds = {}
        holds[error.process.pid] = error
        record["certificate_hold"] = {
            "pid": error.process.pid,
            "status": "unreaped",
            "staging_directory": str(error.staging_directory),
        }

    def _certificate_terminal(self, record: dict):
        hold = record.get("certificate_hold")
        if not hold or hold["status"] == "reaped":
            return
        actor = getattr(self, "certificate_holds", {}).get(hold["pid"])
        if actor is None:
            raise CloudEnvError("certificate actor ownership/reap is unconfirmed; preserve private artifacts and HOLD")
        try:
            actor.process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            raise CloudEnvError("certificate actor remains unreaped; preserve private artifacts and HOLD") from None
        for field in ("staging_directory_fd", "directory_fd"):
            descriptor = getattr(actor, field)
            if descriptor is not None:
                os.close(descriptor)
                setattr(actor, field, None)
        hold["status"] = "reaped"
        self._save()

    def _worker(self) -> str:
        worker = self.env._confirmed("servers", "worker")
        if not worker or not self.env.state.get("worker_ready"):
            raise CloudEnvError("native preparation requires the existing ready worker")
        fresh = self.env.conn.compute.get_server(worker["id"])
        self.env._owned(fresh, role="worker")
        return worker["address"]

    def _ssh(
        self, address: str, command: str, *, input: bytes | None = None, timeout: float = 60, check: bool = True
    ) -> subprocess.CompletedProcess:
        if getattr(self, "effect_grant", None) is not None and getattr(self, "transport_deadline", None) is None:
            self._check_time(self.effect_grant)
        deadline = getattr(self, "transport_deadline", None) or getattr(self, "active_deadline", None)
        if deadline is not None:
            timeout = min(timeout, max(0, deadline - time.time()))
        if timeout <= 0:
            raise CloudEnvError("native transport deadline expired; outcome held")
        argv = [*self.env.ssh_argv(address), command]
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            process.terminate()
            retained = False
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                retained = True
            self.state.setdefault("transport_holds", []).append(
                {
                    "pid": process.pid,
                    "retained_client": retained,
                    "at_unix": int(time.time()),
                    "remote_outcome": "unknown",
                }
            )
            self._save()
            raise CloudEnvError(
                "native SSH interrupted; TERM sent without forced kill; remote outcome unknown"
            ) from None
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if check and result.returncode:
            raise CloudEnvError("native worker command failed; remote journal/resources retained")
        return result

    def _worker_port(self):
        worker = self.env._confirmed("servers", "worker")
        if not worker or worker["status"] != "confirmed":
            raise CloudEnvError("native tunnel requires the exact confirmed worker")
        self.env._owned(self.env.conn.compute.get_server(worker["id"]), role="worker")
        candidates = [
            port
            for port in self.env.conn.network.ports(device_id=worker["id"])
            if port.device_id == worker["id"] and port.network_id == self.env.state["network_id"]
        ]
        if len(candidates) != 1:
            raise CloudEnvError("worker requires one exact project-network port for the private bootstrap tunnel")
        port = self.env.conn.network.get_port(candidates[0].id)
        if port is None or getattr(port, "project_id", None) != self.env.state["project_id"]:
            raise CloudEnvError("native tunnel worker port project is missing or differs; HOLD")
        self.env._project(port)
        if (
            port.device_id != worker["id"]
            or port.network_id != self.env.state["network_id"]
            or len(port.fixed_ips or []) != 1
        ):
            raise CloudEnvError("fresh worker port binding differs; no guest tunnel enrollment")
        return port

    def _worker_fixed_address(self, address: str) -> tuple[str, list[str]]:
        port = self._worker_port()
        fixed = str(ipaddress.IPv4Address(port.fixed_ips[0]["ip_address"]))
        pins = [
            fixed + line[len(address) :]
            for line in self.env.known_hosts.read_text().splitlines()
            if line.startswith(address + " ")
        ]
        if not pins:
            raise CloudEnvError("worker console-pinned management host key is missing; no trust-on-first-use")
        return fixed, pins

    def _install_source(self, address: str):
        data, digest = package_archive()
        predecessor = self.env.state.get("worker_package_sha256")
        preparation = self.state["preparation"]
        if preparation.get("source_install"):
            raise CloudEnvError(
                "source install already has an intent; HOLD, never replay or discard predecessor evidence"
            )
        if not isinstance(predecessor, str) or not re.fullmatch(r"[0-9a-f]{64}", predecessor):
            raise CloudEnvError("installed predecessor has no canonical source receipt; HOLD")
        preparation["source_install"] = {
            "status": "intent",
            "old_sha256": predecessor,
            "new_sha256": digest,
            "prior": self.remote + "/source-before-" + predecessor + ".prior",
        }
        self._save()
        try:
            self._ssh(address, "sudo install -d -m 0700 " + shlex.quote(self.remote))
            script = (
                "import json,sys,site\n"
                + inspect.getsource(_preserve_and_replace_source)
                + "\nresult=_preserve_and_replace_source(site.getsitepackages()[0]+'/palimpsest_local',"
                + repr(self.remote)
                + ",sys.stdin.buffer.read(),"
                + repr(predecessor)
                + ","
                + repr(digest)
                + "); print(json.dumps(result,sort_keys=True))\n"
            )
            response = self._ssh(address, "sudo python3 -I -S -B -c " + shlex.quote(script), input=data, timeout=300)
            result = json.loads(response.stdout)
            if result.get("status") != "activated" or result.get("installed_sha256") != digest:
                raise CloudEnvError("source replacement was not confirmed; preserve all journals and HOLD")
            expected_origin = str(Path(result["package"]) / "__init__.py")
            origin_script = (
                "import importlib.util,json,sys; spec=importlib.util.find_spec('palimpsest_local'); "
                "origin=None if spec is None else spec.origin; print(json.dumps({'origin':origin})); "
                "sys.exit(0 if origin==sys.argv[1] else 44)"
            )
            origin_response = self._ssh(
                address,
                "sudo python3 -I -B -c " + shlex.quote(origin_script) + " " + shlex.quote(expected_origin),
                check=False,
            )
            if origin_response.returncode or json.loads(origin_response.stdout).get("origin") != expected_origin:
                raise CloudEnvError("isolated interpreter does not select the verified activated package; HOLD")
            result["import_origin"] = expected_origin
            preparation["source_install"].update(status="activated", result=result)
            self.env.state["worker_package_sha256"] = digest
            self._save()
        except BaseException:
            preparation["source_install"]["status"] = "held"
            self._save()
            raise

    def _carrier_terminal(self, address: str, run: dict):
        if not run.get("carrier_build_intent"):
            return
        unit = run["carrier_build_unit"]
        output = self._ssh(
            address,
            "sudo systemctl show " + shlex.quote(unit) + " --property=ActiveState --property=LoadState",
            check=False,
        )
        facts = dict(line.split("=", 1) for line in output.stdout.decode().splitlines() if "=" in line)
        if facts.get("ActiveState") not in ("inactive", "failed") and facts.get("LoadState") != "not-found":
            raise CloudEnvError("carrier builder may still be running; no forced kill or completed-cleanup claim")
        path = run["remote"] + "/carrier/build-state.json"
        script = (
            "import json,os,sys; p=sys.argv[1]; sys.exit(44) if not os.path.lexists(p) else None; "
            "f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); s=os.fdopen(f); v=json.load(s); s.close(); "
            "print(v['state'])"
        )
        result = self._ssh(address, "sudo python3 -I -c " + shlex.quote(script) + " " + shlex.quote(path), check=False)
        if result.returncode != 44 and (
            result.returncode or result.stdout.strip() not in (b"BUILT_NOT_BOOT_PROVEN", b"FAILED_PRESERVED")
        ):
            raise CloudEnvError("carrier resource journal is not terminal/released; HOLD")
        run["carrier_build_terminal"] = True
        self._save()

    def _grant(
        self, path: Path, action: str, tag: str | None = None, image: dict | None = None, *, phase: str | None = None
    ) -> tuple[dict, dict]:
        grant = _private_json(path)
        required = {
            "schema",
            "grant_id",
            "project_id",
            "environment",
            "source_digest",
            "source_package_sha256",
            "expires_unix",
            "max_seconds",
            "actions",
            "limits",
            "flavors",
            "boot_gib",
            "max_gpu_seconds",
            "probe_source_sha256",
            "cleanup_expires_unix",
        }
        if (
            not required
            <= set(grant)
            <= required
            | {
                "cleanup_of_grant_id",
                "recovery",
                "cleanup_reserve_seconds",
                "allow_retained",
                "source_project_id",
                "allow_path_scoped_ceph",
            }
            or grant["schema"] != GRANT_SCHEMA
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(grant["grant_id"]))
            or grant["project_id"] != self.env.state["project_id"]
            or grant["environment"] != self.env.name
            or type(grant["expires_unix"]) is not int
            or (action != "cleanup" and grant["expires_unix"] <= time.time())
            or type(grant["cleanup_expires_unix"]) is not int
            or not grant["expires_unix"] < grant["cleanup_expires_unix"] <= grant["expires_unix"] + 3600
            or (action == "cleanup" and grant["cleanup_expires_unix"] <= time.time())
            or not isinstance(grant["actions"], list)
            or action not in grant["actions"]
            or (action == "run" and "cleanup" not in grant["actions"])
            or any(value not in {"prepare", "run", "cleanup"} for value in grant["actions"])
            or type(grant["max_seconds"]) is not int
            or not 1 <= grant["max_seconds"] <= 3600
            or type(grant["boot_gib"]) is not int
            or not 4 <= grant["boot_gib"] <= 80
            or type(grant["max_gpu_seconds"]) is not int
            or grant["max_gpu_seconds"] < 0
            or (
                grant["source_digest"] is not None
                if "source_project_id" in grant
                else not re.fullmatch(r"sha256:[0-9a-f]{64}", str(grant["source_digest"]))
            )
            or not re.fullmatch(r"[0-9a-f]{64}", str(grant["source_package_sha256"]))
            or not re.fullmatch(r"[0-9a-f]{64}", str(grant["probe_source_sha256"]))
        ):
            raise CloudEnvError("native grant scope, expiry or limits are invalid")
        if "cleanup_of_grant_id" in grant and (
            grant["actions"] != ["cleanup"] or grant["cleanup_of_grant_id"] not in self.state["grants"]
        ):
            raise CloudEnvError("recovery approval must be cleanup-only and target a recorded original grant")
        reserve = grant.get("cleanup_reserve_seconds", 0)
        if "allow_retained" in grant and type(grant["allow_retained"]) is not bool:
            raise CloudEnvError("native retained-consumer approval must be an explicit boolean")
        if type(reserve) is not int or not 0 <= reserve < grant["max_seconds"]:
            raise CloudEnvError("native cleanup reserve must fit inside the allocation window")
        recovery = grant.get("recovery")
        if "recovery" in grant and (
            not isinstance(recovery, dict)
            or set(recovery) != {"preparation_sha256", "history_sha256", "runs"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(recovery["preparation_sha256"]))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(recovery["history_sha256"]))
            or not isinstance(recovery["runs"], dict)
            or not recovery["runs"]
            or any(
                not re.fullmatch(r"native-[a-z0-9-]{1,64}", str(role))
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest))
                for role, digest in recovery["runs"].items()
            )
            or "cleanup_of_grant_id" in grant
        ):
            raise CloudEnvError("native recovery must bind exact prior preparation and failed-run digests")
        if (
            not isinstance(grant["limits"], dict)
            or set(grant["limits"]) != {*KINDS, "cpu_servers", "gpu_servers"}
            or any(type(value) is not int or value < 0 for value in grant["limits"].values())
            or not isinstance(grant["flavors"], dict)
            or set(grant["flavors"]) != {"cpu", "gpu"}
        ):
            raise CloudEnvError("native grant requires exact per-kind cumulative limits and flavor IDs")
        project_scope = "source_project_id" in grant
        if project_scope and (
            grant["source_project_id"] != grant["project_id"]
            or grant["limits"]["gpu_servers"] != 0
            or grant["flavors"]["gpu"] is not None
            or grant["max_gpu_seconds"] != 0
            or tag is not None
            or (phase is not None and phase != "cpu")
        ):
            raise CloudEnvError("native project-image authority requires the exact project and a CPU-only policy")
        if "allow_path_scoped_ceph" in grant and type(grant["allow_path_scoped_ceph"]) is not bool:
            raise CloudEnvError("native path-scoped storage approval must be an explicit boolean")
        for identity in grant["flavors"].values():
            if identity is not None:
                _uuid(identity)
        if image is not None:
            from .nova_ceph_boot import validate_image_projection

            if tag is not None or image.get("source", {}).get("kind") != "published":
                raise CloudEnvError("server native source must be one exact Hub published manifest")
            actual_digest = image["source"].get("manifest_digest")
            if project_scope:
                authority = getattr(self.env, "_hub_published_image", None)
                if (
                    not isinstance(authority, dict)
                    or authority.get("project_id") != grant["source_project_id"]
                    or not authority.get("image_id")
                    or authority.get("manifest_digest") != actual_digest
                ):
                    raise CloudEnvError("native project-image selection requires exact server Hub authority")
            validate_image_projection(image, actual_digest if project_scope else grant["source_digest"])
            if action == "run" and (
                grant.get("allow_path_scoped_ceph") is not True
                or not callable(getattr(self.env, "native_ceph_access", None))
            ):
                raise CloudEnvError("published native images require approved storage-enforced per-image access")
        if tag is not None:
            image = self.env.state.get("images", {}).get(tag)
            if image is None:
                raise CloudEnvError("native source must already have a registry acquisition receipt")
            _validate_registry_image(image)
            if image["source"]["root_digest"] != grant["source_digest"]:
                raise CloudEnvError("native grant is bound to another original registry image")
        _, package_sha = package_archive()
        if action != "cleanup" and package_sha != grant["source_package_sha256"]:
            raise CloudEnvError("native grant source package hash differs from reviewed source")
        fingerprint = _digest(grant)
        ledger = self.state["grants"].get(grant["grant_id"])
        if ledger and ledger["digest"] != fingerprint:
            raise CloudEnvError("native grant ID was already bound to different approval bytes")
        if ledger is None:
            ledger = {"digest": fingerprint, "used": {key: 0 for key in grant["limits"]}, "gpu_seconds": 0}
            self.state["grants"][grant["grant_id"]] = ledger
            self._save()
        if action != "cleanup":
            self.grant_path = path
            self.effect_grant = grant
            self.transport_deadline = None
        return grant, ledger

    def _check_time(self, grant: dict):
        if time.time() >= grant["expires_unix"]:
            # Allocation expiry prohibits new effects but never releases exact cleanup authority.
            raise CloudEnvError("native grant expired; HOLD, no further effects")
        if _digest(_private_json(self.grant_path)) != _digest(grant):
            raise CloudEnvError("native approval file changed during transaction; cleanup only")
        _, digest = package_archive()
        if digest != grant["source_package_sha256"]:
            raise CloudEnvError("native source changed during transaction; cleanup only")
        self.env._authenticate(self.env.state["cloud"])
        if getattr(self, "active_deadline", grant["expires_unix"]) <= time.time():
            raise CloudEnvError("native run deadline expired; cleanup only")

    def _cleanup_boundary(self, grant: dict, path: Path, run: dict | None = None):
        if time.time() >= grant["cleanup_expires_unix"] or _digest(_private_json(path)) != _digest(grant):
            raise CloudEnvError("native cleanup approval changed or its bounded tail expired; HOLD")
        if run is not None and (
            run["grant_id"] != grant.get("cleanup_of_grant_id", grant["grant_id"])
            or not self._run_source_matches(grant, run)
            or run["package_sha256"] != grant["source_package_sha256"]
        ):
            raise CloudEnvError("native cleanup approval does not bind the exact original run")
        self.env._authenticate(self.env.state["cloud"])

    @staticmethod
    def _run_source_matches(grant: dict, run: dict) -> bool:
        if "source_project_id" in grant:
            return run.get("source_project_id") == grant["source_project_id"] and bool(
                re.fullmatch(r"sha256:[0-9a-f]{64}", str(run.get("source_digest")))
            )
        return run.get("source_digest") == grant["source_digest"] and "source_project_id" not in run

    def _bind_ceph_mutation_boundary(self, grant, path, run_id):
        def boundary(operation, role):
            if role != run_id or operation not in {"add", "del"}:
                raise CloudEnvError("Ceph mutation does not bind the active native transaction")
            if operation == "add":
                self._check_time(grant)
                self.env.native_authorize_image()
                deadline = min(grant["expires_unix"], getattr(self, "active_deadline", grant["expires_unix"]))
            else:
                run = self.state["runs"].get(role)
                if run is None:
                    raise CloudEnvError("Ceph cleanup has no exact native run")
                self._cleanup_boundary(grant, path, run)
                deadline = grant["cleanup_expires_unix"]
            # Authentication/SQL reads above may consume time or change approval.
            # This last local fence is immediately adjacent to the Ceph spawn.
            if time.time() >= deadline or _digest(_private_json(path)) != _digest(grant):
                raise CloudEnvError("Ceph mutation approval changed or expired before command dispatch")

        self.env.native_ceph_mutation_boundary = boundary

    def _prior_runs(self, grant: dict, *, check_absence: bool = False):
        """A new approval may acknowledge failure, never rewrite/refund it."""
        runs = self.state["runs"]
        if any(run.get("status") != "cleaned" for run in runs.values()):
            raise CloudEnvError("prior native run is pending/failed/unknown; exact cleanup is required")
        failed = {
            role: run for role, run in runs.items() if run.get("outcome") != "success" or run.get("budget_exceeded")
        }
        recovery = grant.get("recovery", {})
        if failed and (
            recovery.get("runs") != {role: _digest(run) for role, run in failed.items()}
            or any(run.get("grant_id") == grant["grant_id"] for run in failed.values())
        ):
            raise CloudEnvError(
                "prior native run failed; a new exact recovery approval is required, never automatic retry"
            )
        if recovery and set(recovery["runs"]) != set(failed):
            raise CloudEnvError("native recovery contains unknown, changed or successful prior runs")
        if any(
            records for kind, records in self.env.state.get("resources", {}).items() if kind.startswith("native_")
        ) or any(role.startswith("native-") for role in self.env.state.get("resources", {}).get("access_rules", {})):
            raise CloudEnvError("orphan native resources remain; exact cleanup is required before allocation")
        if recovery and recovery["history_sha256"] != _digest(
            {"history": [item for item in self.env.state.get("history", []) if item.get("role") in failed]}
        ):
            raise CloudEnvError("native recovery absence history changed; HOLD, no skipped resource reads")
        if not check_absence:
            return
        getters = {
            "native_servers": self.env.conn.compute.get_server,
            "native_volumes": self.env.conn.block_storage.get_volume,
            "native_ports": self.env.conn.network.get_port,
            "native_images": self.env.conn.image.get_image,
        }
        for role, run in failed.items():
            if (
                run.get("enrolled")
                or run.get("enrollment_intent")
                or run.get("cleanup_errors")
                or (run.get("carrier_build_intent") and not run.get("carrier_build_terminal"))
                or run.get("certificate_hold", {}).get("status", "reaped") != "reaped"
                or run.get("upload_transport_hold", {}).get("retained_client")
            ):
                raise CloudEnvError("native recovery requires revoked enrollment and terminal cleanup evidence")
            history = {
                item["kind"]: item["resource"]
                for item in self.env.state.get("history", [])
                if item.get("role") == role and "absent_at" in item
            }
            if run.get("nova_post_unix") is not None and (
                not run.get("server_id") or history.get("native_servers", {}).get("id") != run["server_id"]
            ):
                raise CloudEnvError("native recovery lacks exact server absence history; HOLD")
            for kind, getter in getters.items():
                if kind in history and self.env._get(getter, _uuid(history[kind]["id"])) is not None:
                    raise CloudEnvError("native recovery resource still exists; HOLD, read-only reconciliation only")
            if "access_rules" in history:
                rule = history["access_rules"]
                if rule.get("provider") == "ceph-namespace-v1":
                    check = getattr(self.env, "native_ceph_absent", None)
                    if not callable(check):
                        raise CloudEnvError("native namespace recovery requires its exact capability issuer; HOLD")
                    check(rule)
                else:
                    share = self.env._confirmed("shares", "layers")
                    if not share or rule.get("share_id") != share["id"]:
                        raise CloudEnvError("native recovery share differs from the original access rule; HOLD")
                    self.env._owned(self.env.conn.shared_file_system.get_share(share["id"]))
                    if any(
                        item.id == rule["id"] or item.access_to == rule["access_to"]
                        for item in self.env.conn.shared_file_system.access_rules(share["id"])
                    ):
                        raise CloudEnvError("native recovery access rule remains; HOLD")

    def _reserve(self, grant: dict, ledger: dict, phase: str, timeout: int):
        if type(timeout) is not int or not 1 <= timeout <= grant["max_seconds"]:
            raise CloudEnvError("native run timeout exceeds explicit grant")
        if timeout <= grant.get("cleanup_reserve_seconds", 0):
            raise CloudEnvError("native execution window must leave the explicit cleanup reserve")
        requested = {key: 1 for key in KINDS}
        requested[phase + "_servers"] = 1
        if any(ledger["used"][key] + count > grant["limits"][key] for key, count in requested.items()):
            raise CloudEnvError("native cumulative resource budget is exhausted; no retry allocation")
        if phase == "gpu" and ledger["gpu_seconds"] + timeout > grant["max_gpu_seconds"]:
            raise CloudEnvError("native cumulative GPU time budget is exhausted")
        for key, count in requested.items():
            ledger["used"][key] += count
        if phase == "gpu":
            ledger["gpu_seconds"] += timeout
        self._save()

    def _remote_private(self, address: str, path: str, data: bytes):
        script = (
            "import os,sys,uuid; p=sys.argv[1]; q=p+'.'+uuid.uuid4().hex+'.tmp'; "
            "f=os.open(q,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); "
            "s=os.fdopen(f,'wb'); s.write(sys.stdin.buffer.read()); s.flush(); os.fsync(s.fileno()); s.close(); "
            "assert not os.path.lexists(p); os.rename(q,p); "
            "d=os.open(os.path.dirname(p),os.O_RDONLY|os.O_DIRECTORY); os.fsync(d); os.close(d)"
        )
        self._ssh(address, "sudo python3 -I -c " + shlex.quote(script) + " " + shlex.quote(path), input=data)

    def _remote_json(self, address: str, path: str, value: dict):
        self._remote_private(address, path, _canonical(value))

    def _remote_sha256(self, address: str, path: str) -> str:
        script = (
            "import hashlib,os,stat,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW); "
            "s=os.fdopen(f,'rb'); a=os.fstat(s.fileno()); "
            "assert stat.S_ISREG(a.st_mode) and a.st_uid==0 and a.st_nlink==1 and not a.st_mode&0o022 and a.st_size<=67108864; "
            "h=hashlib.file_digest(s,'sha256'); b=os.fstat(s.fileno()); "
            "assert (a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns)==(b.st_dev,b.st_ino,b.st_size,b.st_mtime_ns,b.st_ctime_ns); "
            "s.close(); print(h.hexdigest())"
        )
        response = self._ssh(address, "sudo python3 -I -S -B -c " + shlex.quote(script) + " " + shlex.quote(path))
        digest = response.stdout.decode().strip()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise CloudEnvError("worker artifact hash response is invalid; HOLD")
        return digest

    def _remote_probe_glibc(self, address: str, path: str) -> str:
        response = self._ssh(address, shlex.join(["sudo", "readelf", "--version-info", "--wide", path]))
        return validate_probe_glibc(response.stdout)

    def _verify_prepared_artifacts(self, address: str, preparation: dict):
        runtime_sha = preparation.get("runtime_sha256")
        probe_sha = preparation.get("probe_sha256")
        if (
            not isinstance(runtime_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", runtime_sha) is None
            or not isinstance(probe_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", probe_sha) is None
            or hashlib.sha256(Path(__file__).with_name("nova_ceph_boot.py").read_bytes()).hexdigest() != runtime_sha
        ):
            raise CloudEnvError("prepared artifacts lack an exact current reviewed runtime/probe binding; HOLD")
        if (
            self._remote_sha256(address, preparation["config"]["runtime_script_path"]) != runtime_sha
            or self._remote_sha256(address, preparation["probe_path"]) != probe_sha
        ):
            raise CloudEnvError("prepared worker runtime/probe bytes changed; HOLD, never rewrite during run/restart")
        if (
            not isinstance(preparation.get("probe_glibc_max"), str)
            or self._remote_probe_glibc(address, preparation["probe_path"]) != preparation["probe_glibc_max"]
        ):
            raise CloudEnvError("compiled probe ABI was not verified against the original image; HOLD")

    def _verify_carrier_manifest(self, run: dict, result: dict, manifest: dict):
        preparation = self.state["preparation"]
        files = manifest.get("files")
        if not isinstance(files, dict) or _digest(manifest) != result.get("carrier_digest"):
            raise CloudEnvError("carrier artifact manifest differs from its cryptographic pin; HOLD")
        expected = {"bootstrap/nova_ceph_boot.py": preparation["runtime_sha256"]}
        if run["phase"] == "gpu":
            expected["carrier/probe/cuda-probe"] = preparation["probe_sha256"]
        if any(
            not isinstance(files.get(path), dict) or files[path].get("sha256") != digest
            for path, digest in expected.items()
        ):
            raise CloudEnvError("carrier did not contain the exact reviewed runtime/prepared probe bytes; HOLD")

    def _recover_preparation(self, grant: dict, config: dict) -> bool:
        prepared = self.state.get("preparation")
        if not prepared or not config["install_source"]:
            return False
        recovery = grant.get("recovery", {})
        if (
            recovery.get("preparation_sha256") != _digest(prepared)
            or prepared.get("status") != "prepared"
            or prepared.get("service_started")
            or prepared.get("service_start_intent")
            or prepared.get("grant_id") == grant["grant_id"]
            or prepared.get("package_sha256") == grant["source_package_sha256"]
            or prepared.get("package_sha256") != self.env.state.get("worker_package_sha256")
            or prepared.get("certificate_hold", {}).get("status", "reaped") != "reaped"
        ):
            raise CloudEnvError(
                "source replacement requires a new exact recovery approval and stopped prepared service"
            )
        self._check_time(grant)
        self._prior_runs(grant, check_absence=True)
        address = self._worker()
        response = self._ssh(
            address,
            "sudo systemctl show " + shlex.quote(prepared["service_unit"]) + " --property=ActiveState,SubState,MainPID",
        )
        facts = dict(line.split("=", 1) for line in response.stdout.decode().splitlines() if "=" in line)
        if facts != {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0"}:
            raise CloudEnvError("predecessor bootstrap service is not freshly confirmed stopped; HOLD")
        for run in self.state["runs"].values():
            self._carrier_terminal(address, run)
        self.remote = self.base_remote + "/generation-" + grant["source_package_sha256"]
        self.directory = self.base_directory / ("generation-" + grant["source_package_sha256"])
        if config["runtime_script_path"] != self.remote + "/nova_ceph_boot.py":
            raise CloudEnvError("recovery runtime must use its new immutable preparation generation")
        if any(config[key] != prepared["config"].get(key) for key in ("bundle_path", "dns")):
            raise CloudEnvError("recovery must reuse the exact prepared kernel bundle and DNS; HOLD")
        if _digest(prepared) in self.state.get("preparation_history", {}):
            raise CloudEnvError("preparation generation was already superseded; HOLD, no replay")
        return True

    def prepare(
        self, *, grant_path: Path, tag: str | None = None, config_path: Path, image: dict | None = None
    ) -> dict:
        if image is not None and tag is not None:
            raise CloudEnvError("select exactly one native image authority")
        if image is None and tag is None and "source_project_id" not in _private_json(grant_path):
            raise CloudEnvError("select exactly one native image authority")
        grant, _ = self._grant(grant_path, "prepare", tag, image)
        config = _private_json(config_path)
        if set(config) != {
            "install_source",
            "start_service",
            "bundle_path",
            "runtime_script_path",
            "probe_source_file",
            "dns",
        }:
            raise CloudEnvError("prepare requires explicit source/service choices, artifact/probe paths and DNS")
        if type(config["install_source"]) is not bool or type(config["start_service"]) is not bool:
            raise CloudEnvError("prepare source/service choices must be booleans")
        for key in ("bundle_path", "runtime_script_path"):
            path = Path(config[key])
            if not path.is_absolute() or ".." in path.parts:
                raise CloudEnvError("prepared worker artifact paths must be absolute")
        if not isinstance(config["dns"], list) or not 1 <= len(config["dns"]) <= 8:
            raise CloudEnvError("prepare requires one to eight explicit DNS addresses")
        for address in config["dns"]:
            if not isinstance(address, str) or str(ipaddress.IPv4Address(address)) != address:
                raise CloudEnvError("native proof DNS must contain canonical IPv4 addresses")
        probe_source = Path(config["probe_source_file"]).read_bytes()
        if hashlib.sha256(probe_source).hexdigest() != grant["probe_source_sha256"]:
            raise CloudEnvError("CUDA probe source differs from the explicit reviewed grant")
        recovering = self._recover_preparation(grant, config)
        if config["runtime_script_path"] != self.remote + "/nova_ceph_boot.py":
            raise CloudEnvError("runtime script must use the environment-owned native artifact path")
        if self.state.get("preparation") and not recovering:
            prepared = self.state["preparation"]
            previous = prepared["config"]
            if (
                prepared.get("status") != "prepared"
                or prepared.get("service_started")
                or config["install_source"]
                or not config["start_service"]
                or any(config[key] != previous[key] for key in config if key not in {"start_service", "install_source"})
                or any(run.get("status") != "cleaned" for run in self.state["runs"].values())
            ):
                raise CloudEnvError("preparation exists; only explicit source-unchanged service start is permitted")
            if (
                prepared["package_sha256"] != grant["source_package_sha256"]
                or prepared["probe_source_sha256"] != grant["probe_source_sha256"]
            ):
                raise CloudEnvError("prepared service source differs from new approval")
            self._prior_runs(grant, check_absence=bool(grant.get("recovery")))
            if grant.get("recovery") and grant["recovery"]["preparation_sha256"] != _digest(prepared):
                raise CloudEnvError("service restart recovery differs from the exact stopped preparation")
            self._check_time(grant)
            self._verify_prepared_artifacts(self._worker(), prepared)
            prepared.update(service_start_intent=True, grant_id=grant["grant_id"])
            if grant.get("recovery"):
                prepared["recovery"] = grant["recovery"]
            self._save()
            self._ssh(self._worker(), "sudo systemctl start " + shlex.quote(prepared["service_unit"]))
            self._ssh(self._worker(), "sudo systemctl is-active --quiet " + shlex.quote(prepared["service_unit"]))
            prepared.update(service_started=True)
            self._save()
            return {"status": "prepared", "service_started": True, "exit_code": 0}
        address = self._worker()
        preparation = {
            "status": "preparing",
            "grant_id": grant["grant_id"],
            "config": config,
            "package_sha256": grant["source_package_sha256"],
            "source_digest": grant["source_digest"],
            "remote": self.remote,
            "directory": str(self.directory),
        }
        if grant.get("recovery"):
            preparation["recovery"] = grant["recovery"]
        if recovering:
            previous = self.state["preparation"]
            self.state.setdefault("preparation_history", {})[_digest(previous)] = previous
        self.state["preparation"] = preparation
        self._save()
        try:
            self._check_time(grant)
            if config["install_source"]:
                self._install_source(address)
            elif self.env.state.get("worker_package_sha256") != grant["source_package_sha256"]:
                raise CloudEnvError("worker source differs; explicit install_source approval is required")
            from .nova_bootstrap_service import create_ca, issue_server_certificate

            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            ca_dir = self.directory / "ca"
            ca_dir.mkdir(mode=0o700)
            ca = create_ca(ca_dir)
            server_dir = self.directory / "server"
            server_dir.mkdir(mode=0o700)
            server = issue_server_certificate(server_dir, ca["cert_file"], ca["key_file"])
            # The CA signing key never leaves the controller.
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for name, path in (
                    ("ca.pem", ca["cert_file"]),
                    ("server.pem", server["cert_file"]),
                    ("server.key", server["key_file"]),
                ):
                    content = Path(path).read_bytes()
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o600
                    tar.addfile(info, io.BytesIO(content))
            self._ssh(
                address,
                "sudo install -d -m 0700 "
                + shlex.quote(self.remote)
                + " "
                + shlex.quote(self.remote + "/enrollments")
                + " "
                + shlex.quote(self.remote + "/results"),
            )
            self._ssh(address, "sudo tar -xf - -C " + shlex.quote(self.remote), input=archive.getvalue())
            runtime_source = Path(__file__).with_name("nova_ceph_boot.py").read_bytes()
            preparation["runtime_sha256"] = hashlib.sha256(runtime_source).hexdigest()
            self._save()
            self._remote_private(address, config["runtime_script_path"], runtime_source)
            self._remote_private(address, self.remote + "/cuda_probe.c", probe_source)
            if (
                self._remote_sha256(address, config["runtime_script_path"]) != preparation["runtime_sha256"]
                or self._remote_sha256(address, self.remote + "/cuda_probe.c") != grant["probe_source_sha256"]
            ):
                raise CloudEnvError("worker runtime/probe source read-back hash differs; HOLD")
            compile_command = shlex.join(
                [
                    "gcc",
                    "-std=c11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-pedantic",
                    self.remote + "/cuda_probe.c",
                    "-o",
                    self.remote + "/cuda-probe",
                    "-ldl",
                ]
            )
            self._ssh(address, "sudo sh -c " + shlex.quote("umask 077; exec " + compile_command), timeout=120)
            preparation["probe_path"] = self.remote + "/cuda-probe"
            preparation["probe_source_sha256"] = grant["probe_source_sha256"]
            preparation["probe_sha256"] = self._remote_sha256(address, preparation["probe_path"])
            preparation["probe_glibc_max"] = self._remote_probe_glibc(address, preparation["probe_path"])
            self._save()
            self._verify_prepared_artifacts(address, preparation)
            service_config = {
                "schema": "palimpsest.nova-bootstrap-service.v1",
                "bind": "127.0.0.1",
                "port": 9443,
                "cert_file": self.remote + "/server.pem",
                "key_file": self.remote + "/server.key",
                "client_ca_file": self.remote + "/ca.pem",
                "enrollments_dir": self.remote + "/enrollments",
                "results_dir": self.remote + "/results",
            }
            self._remote_json(address, self.remote + "/service.json", service_config)
            suffix = "-" + grant["source_package_sha256"][:16] if recovering else ""
            unit_name = "palimpsest-native-" + self.env.state["marker"] + suffix + ".service"
            unit = (
                "[Unit]\nDescription=Palimpsest private native bootstrap\nAfter=network.target\n[Service]\n"
                "Type=simple\nUMask=0077\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
                "ExecStart=/usr/bin/python3 -I -m palimpsest_local.nova_bootstrap_service serve --config "
                + self.remote
                + "/service.json\nKillSignal=SIGTERM\nSendSIGKILL=no\nTimeoutStopSec=30\nRestart=no\n"
            )
            preparation["service_unit"] = unit_name
            preparation["ca_cert_file"] = ca["cert_file"]
            preparation["ca_key_file"] = ca["key_file"]
            self._save()
            self._check_time(grant)
            self._ssh(address, "sudo tee /etc/systemd/system/" + unit_name + " >/dev/null", input=unit.encode())
            self._ssh(address, "sudo systemctl daemon-reload")
            if config["start_service"]:
                preparation["service_start_intent"] = True
                self._save()
                self._ssh(address, "sudo systemctl start " + unit_name)
                self._ssh(address, "sudo systemctl is-active --quiet " + unit_name)
            preparation.update(status="prepared", service_started=config["start_service"])
            self._save()
            return {"status": "prepared", "service_started": config["start_service"], "exit_code": 0}
        except BaseException as error:
            self._remember_certificate_hold(error, preparation)
            preparation.update(status="held", error_type=type(error).__name__)
            self._save()
            raise

    def _metadata(self, role: str, run: dict) -> dict:
        return {
            "palimpsest_cloud_env": self.env.name,
            "palimpsest_env_id": self.env.state["marker"],
            "palimpsest_role": role,
            "palimpsest_native_run": run["run_id"],
            "palimpsest_source_digest": run["source_digest"],
            "palimpsest_grant": run["grant_id"],
        }

    def _confirmed_id(self, kind: str, role: str, pending: dict, identity: str):
        _uuid(identity)
        return self.env.record(kind, role, {**pending, "id": identity, "status": "confirmed"})

    def _network(self, port) -> dict:
        self.env._project(port)
        if port.network_id != self.env.state["network_id"] or len(port.fixed_ips or []) != 1:
            raise CloudEnvError("native port requires exactly one project-owned fixed IPv4 address")
        fixed = port.fixed_ips[0]
        subnet = self.env.conn.network.get_subnet(fixed["subnet_id"])
        self.env._project(subnet)
        if subnet.network_id != port.network_id or not subnet.is_dhcp_enabled:
            raise CloudEnvError("native runtime requires the exact DHCP-enabled subnet")
        cidr = ipaddress.ip_network(subnet.cidr, strict=True)
        address = ipaddress.ip_address(fixed["ip_address"])
        if cidr.version != 4 or address not in cidr or not subnet.gateway_ip:
            raise CloudEnvError("native runtime requires an IPv4 subnet and gateway")
        dns = (
            self.state.get("preparation", {}).get("config", {}).get("dns")
            or self.env.state.get("dns")
            or list(subnet.dns_nameservers or [])
        )
        if not dns:
            raise CloudEnvError("native runtime requires explicit subnet/environment DNS")
        return {"address": str(address), "cidr": str(cidr), "gateway": subnet.gateway_ip, "dns": dns}

    def _tunnel_policy(self) -> str:
        """Prove only the native guest's fixed-IP SSH path; never repair provider ingress."""
        group = self.env._confirmed("security_groups", "env")
        if not group or group["status"] != "confirmed":
            raise CloudEnvError("native tunnel requires a confirmed environment security group")
        fresh = self.env.conn.network.get_security_group(group["id"])
        if fresh is None or getattr(fresh, "project_id", None) != self.env.state["project_id"]:
            raise CloudEnvError("native tunnel security group project is missing or differs; HOLD")
        self.env._project(fresh)
        if fresh.id != group["id"] or getattr(fresh, "stateful", None) is not True:
            raise CloudEnvError("native tunnel requires the exact stateful environment security group")
        worker = self._worker_port()
        if (
            getattr(worker, "security_group_ids", None) != [group["id"]]
            or getattr(worker, "is_port_security_enabled", None) is not True
        ):
            raise CloudEnvError("worker tunnel port requires security enabled and only the environment security group")
        worker_network = self._network(worker)
        network = self.env.conn.network.get_network(self.env.state["network_id"])
        if network is None or getattr(network, "project_id", None) != self.env.state["project_id"]:
            raise CloudEnvError("native tunnel network project is missing or differs; HOLD")
        self.env._project(network)
        if getattr(network, "is_port_security_enabled", None) is not True:
            raise CloudEnvError("native tunnel requires the selected network's port security enabled")
        if (
            network.id != worker.network_id
            or not isinstance(getattr(network, "subnet_ids", None), list)
            or not network.subnet_ids
        ):
            raise CloudEnvError("native tunnel requires the exact network's complete subnet inventory")
        subnets = []
        for subnet_id in network.subnet_ids:
            subnet = self.env.conn.network.get_subnet(subnet_id)
            if subnet is None or getattr(subnet, "project_id", None) != self.env.state["project_id"]:
                raise CloudEnvError("native tunnel subnet project is missing or differs; HOLD")
            self.env._project(subnet)
            if subnet.id != subnet_id or subnet.network_id != network.id:
                raise CloudEnvError("native tunnel subnet binding differs; HOLD")
            try:
                cidr = ipaddress.ip_network(subnet.cidr, strict=True)
            except (TypeError, ValueError):
                raise CloudEnvError("native tunnel subnet CIDR is unknown; HOLD") from None
            if cidr.version == 4:
                if not subnet.is_dhcp_enabled or not subnet.gateway_ip:
                    raise CloudEnvError("native tunnel requires DHCP-enabled IPv4 guest subnets")
                subnets.append(cidr)
        if not subnets or worker.fixed_ips[0]["subnet_id"] not in network.subnet_ids:
            raise CloudEnvError("native tunnel worker subnet is absent from the selected network")
        # Use rule resources, not the group's embedded raw Neutron dictionaries.
        rules = list(self.env.conn.network.security_group_rules(security_group_id=group["id"]))
        required = (
            "project_id",
            "security_group_id",
            "direction",
            "ether_type",
            "protocol",
            "port_range_min",
            "port_range_max",
            "remote_group_id",
            "remote_ip_prefix",
            "remote_address_group_id",
        )
        for rule in rules:
            if any(not hasattr(rule, field) for field in required):
                raise CloudEnvError("native tunnel security group rule policy is incomplete; HOLD")
            # SDK descriptors return None for omitted fields too: null/all is
            # authoritative only when the Neutron body actually supplied it.
            supplied = getattr(getattr(rule, "_body", None), "attributes", {})
            known = {
                "security_group_id",
                "direction",
                "ethertype",
                "protocol",
                "port_range_min",
                "port_range_max",
                "remote_group_id",
                "remote_ip_prefix",
            }
            if not known.issubset(supplied) or not ({"project_id", "tenant_id"} & supplied.keys()):
                raise CloudEnvError("native tunnel security group rule policy is incomplete; HOLD")
            self.env._project(rule)
            if rule.security_group_id != group["id"]:
                raise CloudEnvError("native tunnel rule belongs to a different security group; HOLD")

        def permits(rule, direction, targets):
            if rule.direction != direction or rule.ether_type != "IPv4" or rule.remote_address_group_id:
                return False
            if rule.protocol not in (None, "tcp", "6", 6):
                return False
            low, high = rule.port_range_min, rule.port_range_max
            if low is not None or high is not None:
                if (
                    rule.protocol is None
                    or type(low) is not int
                    or type(high) is not int
                    or not 0 <= low <= 22 <= high <= 65535
                ):
                    return False
            if rule.remote_group_id:
                return rule.remote_group_id == group["id"] and not rule.remote_ip_prefix
            if rule.remote_ip_prefix is None:
                return True
            try:
                prefix = ipaddress.ip_network(rule.remote_ip_prefix, strict=True)
            except (TypeError, ValueError):
                return False
            return prefix.version == 4 and all(target.subnet_of(prefix) for target in targets)

        ingress = all(any(permits(rule, "ingress", [subnet]) for rule in rules) for subnet in subnets)
        destination = ipaddress.ip_network(worker_network["address"] + "/32")
        if not ingress or not any(permits(rule, "egress", [destination]) for rule in rules):
            raise CloudEnvError("native tunnel policy does not permit guest subnet TCP22 to the worker; no reservation")
        return group["id"]

    def _tunnel_key(self, address: str, role: str, local: Path, deadline: int) -> dict:
        key = local / "ssh_private_key"
        process = subprocess.Popen(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", role, "-f", str(key)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            process.communicate(timeout=max(1, min(30, deadline - time.time())))
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.state.setdefault("transport_holds", []).append(
                    {"pid": process.pid, "retained_client": True, "kind": "ssh-keygen"}
                )
                self._save()
            raise CloudEnvError("per-run SSH key creation timed out; TERM only, HOLD") from None
        if process.returncode:
            raise CloudEnvError("per-run SSH key creation failed")
        return {"key": key}

    def _install_tunnel_key(self, address: str, role: str, local: Path, deadline: int) -> dict:
        key = local / "ssh_private_key"
        public = key.with_suffix(".pub").read_text().strip()
        from datetime import datetime

        expiry = datetime.fromtimestamp(deadline, UTC).strftime("%Y%m%d%H%M%SZ")
        source_address = str(ipaddress.IPv4Address(self.state["runs"][role]["plan"]["network"]["address"]))
        line = (
            'from="'
            + source_address
            + '",restrict,port-forwarding,command="/usr/bin/false",permitopen="127.0.0.1:9443",expiry-time="'
            + expiry
            + '" '
            + public
            + " palimpsest-native-"
            + role
        )
        pending = self.env._intent(
            "native_ssh_keys", role, line=line, worker_id=self.env.resource("servers", "worker")["id"]
        )
        script = (
            "import os,sys; p='/home/ubuntu/.ssh/authorized_keys'; line=sys.stdin.buffer.read(); "
            "fd=os.open(p,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW); "
            "os.write(fd,b'\\n'+line+b'\\n'); os.fsync(fd); os.close(fd)"
        )
        self._ssh(address, "python3 -I -c " + shlex.quote(script), input=line.encode())
        self.env.record("native_ssh_keys", role, {**pending, "id": role, "status": "confirmed", "deadline": deadline})
        return {"key": key, "line": line}

    def _build(self, address: str, role: str, run: dict, plan: dict, deadline: int) -> dict:
        from .nova_bootstrap_service import issue_client_certificate

        preparation = self.state["preparation"]
        self._verify_prepared_artifacts(address, preparation)
        local = self.directory / role
        local.mkdir(mode=0o700)
        cert = issue_client_certificate(local, preparation["ca_cert_file"], preparation["ca_key_file"], run_id=role)
        self._tunnel_key(address, role, local, deadline)
        fixed_address, pins = self._worker_fixed_address(address)
        seed = _native_seed(
            role, run["nonce"], self.env.state["project_id"], run["source_digest"], fixed_address, deadline
        )
        data = {
            "seed.json": _canonical(seed),
            "worker_known_hosts": ("\n".join(pins) + "\n").encode(),
            "ssh_private_key": (local / "ssh_private_key").read_bytes(),
            "client_cert": Path(cert["cert_file"]).read_bytes(),
            "client_key": Path(cert["key_file"]).read_bytes(),
            "ca_file": Path(preparation["ca_cert_file"]).read_bytes(),
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, content in data.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(content))
        remote = self.remote + "/" + role
        run.update(remote=remote, client_cert_sha256=cert["cert_sha256"])
        self._save()
        self._ssh(address, "sudo install -d -m 0700 " + shlex.quote(remote) + " " + shlex.quote(remote + "/seed"))
        self._ssh(address, "sudo tar -xf - -C " + shlex.quote(remote + "/seed"), input=buffer.getvalue())
        self._remote_json(address, remote + "/plan.json", plan)
        config = preparation["config"]
        command = [
            "sudo",
            "env",
            "PYTHONDONTWRITEBYTECODE=1",
            "python3",
            "-I",
            "-m",
            "palimpsest_local.nova_boot_carrier",
            "--bundle",
            config["bundle_path"],
            "--runtime-script",
            config["runtime_script_path"],
            "--seed-files",
            remote + "/seed",
            "--plan-template",
            remote + "/plan.json",
            "--output-dir",
            remote + "/carrier",
        ]
        if run["phase"] == "gpu":
            command.extend(["--probe", preparation["probe_path"]])
        unit = "palimpsest-carrier-" + role + ".service"
        qcow = remote + "/carrier/carrier.qcow2"
        build_command = (
            shlex.join(command[1:])
            + " && "
            + shlex.join(
                ["/usr/bin/qemu-img", "convert", "-f", "raw", "-O", "qcow2", remote + "/carrier/carrier.raw", qcow]
            )
        )
        seconds = max(1, min(900, deadline - int(time.time())))
        run.update(carrier_build_intent=True, carrier_build_unit=unit)
        self._save()
        response = self._ssh(
            address,
            shlex.join(
                [
                    "sudo",
                    "systemd-run",
                    "--unit=" + unit,
                    "--wait",
                    "--pipe",
                    "--property=Type=exec",
                    "--property=RuntimeMaxSec=" + str(seconds),
                    "--property=KillSignal=SIGTERM",
                    "--property=SendSIGKILL=no",
                    "--property=TimeoutStopSec=30",
                    "/bin/sh",
                    "-c",
                    build_command,
                ]
            ),
            timeout=seconds + 30,
            check=False,
        )
        if response.returncode:
            run["build_failure_capture"] = self._capture_build_failure(run, response, data)
            self._save()
            raise CloudEnvError("native carrier builder failed; private diagnostic status retained") from None
        self._carrier_terminal(address, run)
        result = json.loads(self._ssh(address, "sudo cat " + shlex.quote(remote + "/carrier/result.json")).stdout)
        if result.get("schema") != "palimpsest.nova-boot-carrier.v1" or result.get("state") != "BUILT_NOT_BOOT_PROVEN":
            raise CloudEnvError("worker did not return a complete native carrier")
        built = result["plan"]
        expected = {**plan, "carrier_digest": result["carrier_digest"]}
        if built != expected or not re.fullmatch(r"sha256:[0-9a-f]{64}", result["carrier_digest"]):
            raise CloudEnvError("carrier builder changed the approved plan")
        from .nova_ceph_boot import plan_policy_digest

        if (
            result.get("expected_plan_digest") != plan_policy_digest(built)
            or result.get("manifest_digest") != result["carrier_digest"]
        ):
            raise CloudEnvError("carrier policy/manifest digest differs from controller calculation")
        manifest = json.loads(self._ssh(address, "sudo cat " + shlex.quote(remote + "/carrier/artifacts.json")).stdout)
        self._verify_carrier_manifest(run, result, manifest)
        raw = result["artifacts"]["raw_disk"]
        if raw["path"] != remote + "/carrier/carrier.raw" or raw["size"] != 4 * 1024**3:
            raise CloudEnvError("carrier raw disk escaped its per-run output or differs from four GiB")
        qcow = remote + "/carrier/carrier.qcow2"
        facts = (
            self._ssh(
                address,
                "sudo sha256sum "
                + shlex.quote(qcow)
                + " && sudo sha512sum "
                + shlex.quote(qcow)
                + " && sudo stat -c %s "
                + shlex.quote(qcow),
            )
            .stdout.decode()
            .splitlines()
        )
        sha = facts[0].split()[0]
        sha512 = facts[1].split()[0]
        size = int(facts[2])
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not re.fullmatch(r"[0-9a-f]{128}", sha512) or size <= 0:
            raise CloudEnvError("carrier qcow2 identity is invalid")
        run.update(carrier=result, qcow2={"path": qcow, "sha256": sha, "sha512": sha512, "size": size})
        self._save()
        return built

    def _upload_carrier(self, address: str, role: str, run: dict, grant: dict):
        self._check_time(grant)
        upload_deadline = min(grant["expires_unix"], run["deadline"])
        if upload_deadline <= time.time():
            raise CloudEnvError("carrier upload deadline expired; no image created")
        metadata = self._metadata(role, run)
        pending = self.env._intent("native_images", role, metadata=metadata, sha256=run["qcow2"]["sha256"])
        image = self.env.conn.image.create_image(
            name="pal-native-" + role,
            disk_format="qcow2",
            container_format="bare",
            visibility="private",
            allow_duplicates=True,
            architecture="x86_64",
            hw_disk_bus="virtio",
            hw_vif_model="virtio",
            properties={**metadata, "hw_firmware_type": "bios"},
        )
        self._confirmed_id("native_images", role, pending, image.id)
        # Separate metadata POST from upload: the exact image ID is durable even if PUT fails.
        self._check_time(grant)
        if upload_deadline <= time.time():
            raise CloudEnvError("carrier upload deadline expired; exact image retained")
        process = subprocess.Popen(
            [*self.env.ssh_argv(address), "sudo cat " + shlex.quote(run["qcow2"]["path"])],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stream = None
        response = None
        try:
            stream = _HashingStream(process.stdout, upload_deadline)
            remaining = upload_deadline - time.time()
            if remaining <= 0:
                raise CloudEnvError("carrier upload deadline expired; exact image retained")
            # Match Image.upload's streaming PUT/size header, but set transport
            # options per call: never mutate the shared metadata/auth session or
            # replay this one-pass body for retries, reauthentication or redirects.
            # Requests uses the connect timeout for body writes too. This scalar
            # bounds each socket wait, not total PUT time; source reads and
            # completion also reject an expired grant.
            response = self.env.conn.image.put(
                "images/" + image.id + "/file",
                data=stream,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Accept": "",
                    "X-OpenStack-Image-Size": str(run["qcow2"]["size"]),
                },
                timeout=min(600, remaining),
                connect_retries=0,
                status_code_retries=0,
                allow_reauth=False,
                redirect=False,
                raise_exc=False,
            )
            if time.time() >= upload_deadline:
                raise CloudEnvError("carrier upload deadline expired; exact image retained")
            if response.status_code not in (200, 201, 204):
                raise CloudEnvError("carrier image upload failed")
            if (
                process.wait(timeout=min(30, max(0, upload_deadline - time.time()))) != 0
                or stream.count != run["qcow2"]["size"]
                or stream.sha256.hexdigest() != run["qcow2"]["sha256"]
            ):
                raise CloudEnvError("streamed carrier bytes differ from worker qcow2 identity")
        except BaseException as error:
            pending, seen, causes = [error], set(), []
            while pending and len(causes) < 8:
                cause = pending.pop()
                if id(cause) in seen:
                    continue
                seen.add(id(cause))
                name = type(cause).__name__
                number = getattr(cause, "errno", None)
                causes.append(
                    {
                        "type": name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception",
                        "errno": number if type(number) is int and 0 <= number <= 4096 else None,
                    }
                )
                pending.extend(
                    value
                    for value in (
                        cause.__cause__,
                        cause.__context__,
                        getattr(cause, "reason", None),
                        *cause.args,
                    )
                    if isinstance(value, BaseException)
                )
            run["upload_failure"] = {
                "causes": causes,
                "source_bytes_read": getattr(stream, "count", 0),
                "expected_bytes": run["qcow2"]["size"],
                "http_status": None if response is None else response.status_code,
                "remote_outcome": "unconfirmed",
            }
            raise
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    run["upload_transport_hold"] = {"pid": process.pid, "retained_client": True}
                    self._save()
        if time.time() >= upload_deadline:
            raise CloudEnvError("carrier upload deadline expired; exact image retained")
        deadline = time.monotonic() + 600
        while True:
            self._check_time(grant)
            fresh = self.env.conn.image.get_image(image.id)
            self._check_image(role, run, fresh)
            if fresh.status == "active":
                return fresh
            if fresh.status not in ("queued", "saving", "uploading", "importing") or time.monotonic() > deadline:
                raise CloudEnvError("carrier image did not become active")
            time.sleep(3)

    def _check_image(self, role: str, run: dict, image):
        properties = image.properties or {}
        if (
            image.owner_id != self.env.state["project_id"]
            or image.visibility != "private"
            or image.disk_format != "qcow2"
            or image.container_format != "bare"
            or image.architecture != "x86_64"
            or image.hw_disk_bus != "virtio"
            or image.hw_vif_model != "virtio"
            or properties.get("hw_firmware_type") != "bios"
            or any(properties.get(k) != v for k, v in self._metadata(role, run).items())
        ):
            raise CloudEnvError("private carrier image ownership/metadata differs; HOLD")
        if image.status == "active" and (
            image.size != run["qcow2"]["size"]
            or image.hash_algo not in ("sha256", "sha512")
            or image.hash_value != run["qcow2"].get(image.hash_algo)
        ):
            raise CloudEnvError("Glance carrier content hash/size differs; HOLD")

    def _check_volume(self, role: str, run: dict, volume, *, available=False):
        self.env._owned(volume, role=role)
        image_id = self.env.resource("native_volumes", role)["image_id"]
        if (
            volume.size != run["boot_gib"]
            or any((volume.metadata or {}).get(k) != v for k, v in self._metadata(role, run).items())
            or (volume.volume_image_metadata or {}).get("image_id") != image_id
        ):
            raise CloudEnvError("native boot volume metadata, size or carrier image differs; HOLD")
        if available and any(
            (volume.volume_image_metadata or {}).get(key) != value
            for key, value in {
                "architecture": "x86_64",
                "hw_firmware_type": "bios",
                "hw_disk_bus": "virtio",
                "hw_vif_model": "virtio",
            }.items()
        ):
            raise CloudEnvError(
                "Cinder boot volume did not preserve the carrier BIOS/virtio image profile; no Nova POST"
            )
        if available and (volume.status != "available" or volume.attachments):
            raise CloudEnvError("native boot volume must be available and unattached before Nova POST")
        if volume.attachments and any(item.get("server_id") != run.get("server_id") for item in volume.attachments):
            raise CloudEnvError("native boot volume has a foreign attachment; HOLD")

    def _check_port(self, role: str, run: dict, port, *, attached=False):
        self.env._project(port)
        record = self.env.resource("native_ports", role)
        if (
            port.network_id != self.env.state["network_id"]
            or port.description != record["description"]
            or port.fixed_ips != record["fixed_ips"]
            or sorted(port.security_group_ids or []) != sorted(record["security_group_ids"])
        ):
            raise CloudEnvError("native port ownership/address/security groups differ; HOLD")
        if port.device_id not in (None, "", run.get("server_id")) or (attached and port.device_id != run["server_id"]):
            raise CloudEnvError("native port is not attached to the exact consumer; HOLD")

    def _bind_server(self, role: str, run: dict, *, deleting: bool = False) -> dict:
        server = self.env.conn.compute.get_server(run["server_id"])
        self.env._owned(server, role=role)
        if any((server.metadata or {}).get(k) != v for k, v in self._metadata(role, run).items()):
            raise CloudEnvError("native server metadata differs; HOLD")
        flavor = server.flavor or {}
        if flavor.get("id", flavor.get("original_name")) not in (run["flavor_id"], run.get("flavor_name")):
            raise CloudEnvError("native server flavor differs; HOLD")
        boot_id = self.env.resource("native_volumes", role)["id"]
        attachments = [item["id"] for item in server.attached_volumes or []]
        if attachments != [boot_id] and (not deleting or attachments):
            raise CloudEnvError("native server does not have the exact prepared boot volume; HOLD")
        volume = self.env.conn.block_storage.get_volume(boot_id)
        self._check_volume(role, run, volume)
        if (not deleting and not volume.attachments) or any(
            item.get("server_id") != server.id for item in volume.attachments or []
        ):
            raise CloudEnvError("Cinder did not confirm the exact consumer attachment")
        port = self.env.conn.network.get_port(self.env.resource("native_ports", role)["id"])
        self._check_port(role, run, port, attached=not deleting)
        network = self._network(port)
        if network != run["plan"]["network"]:
            raise CloudEnvError("fresh consumer network differs from the carrier policy")
        return {**run["plan"], "server_id": server.id, "network": network}

    def run(
        self,
        *,
        grant_path: Path,
        tag: str | None = None,
        phase: str,
        timeout: int,
        command: list[str],
        image: dict | None = None,
        keep: bool = False,
        run_id: str | None = None,
    ) -> dict:
        if phase not in ("cpu", "gpu"):
            raise CloudEnvError("native phase must be cpu or gpu")
        if (image is None) == (tag is None):
            raise CloudEnvError("select exactly one native image authority")
        selected_image = image if image is not None else self.env.state.get("images", {}).get(tag)
        if selected_image is None:
            raise CloudEnvError("native source must already have a registry acquisition receipt")
        authority = getattr(self.env, "_hub_published_image", None)
        storage = authority.get("storage_binding") if isinstance(authority, dict) and image is not None else None
        native_delivery_preflight(
            selected_image,
            command,
            phase,
            self.env.state["project_id"],
            dns=native_prepared_dns(self.env.state),
            storage=storage,
        )
        if image is not None and (not isinstance(storage, dict) or storage.get("kind") != "manila-namespace-v1"):
            raise CloudEnvError("published native image requires its authorized namespace storage binding")
        grant, ledger = self._grant(grant_path, "run", tag, image, phase=phase)
        if image is not None:
            recheck = getattr(self.env, "native_authorize_image", None)
            if not callable(recheck):
                raise CloudEnvError("published native images require fresh Hub image authorization")
            recheck()
        from .cloud_env import _validate_image

        if type(keep) is not bool or (keep and grant.get("allow_retained") is not True):
            raise CloudEnvError("retained native consumers require explicit operator grant approval")
        _validate_image(selected_image, require_root=image is None)
        prep = self.state.get("preparation", {})
        if prep.get("status") != "prepared" or not prep.get("service_started"):
            raise CloudEnvError("explicit native source/artifact/service preparation is required")
        if (
            prep["package_sha256"] != grant["source_package_sha256"]
            or self.env.state.get("worker_package_sha256") != grant["source_package_sha256"]
        ):
            raise CloudEnvError("prepared and worker source hashes must equal reviewed grant; run never upgrades")
        if prep.get("probe_source_sha256") != grant["probe_source_sha256"]:
            raise CloudEnvError("prepared CUDA probe differs from the reviewed grant")
        if image is not None and prep.get("source_digest") != grant["source_digest"]:
            raise CloudEnvError("prepared native profile is bound to another published manifest")
        self._prior_runs(grant, check_absence=bool(grant.get("recovery")))
        if grant.get("recovery") and prep.get("recovery") != grant["recovery"]:
            raise CloudEnvError("prepared service is not bound to this exact recovery approval")
        if phase == "gpu":
            gates = [
                item
                for item in self.state["runs"].values()
                if item["phase"] == "cpu"
                and item["source_digest"] == grant["source_digest"]
                and item.get("package_sha256") == grant["source_package_sha256"]
                and item.get("outcome") == "success"
                and not item.get("budget_exceeded")
                and item.get("verified_result")
            ]
            if not gates or not any(self._successful(item, item["verified_result"]) for item in gates):
                raise CloudEnvError("GPU phase requires a verified native-root CPU service result on the same source")
            if command != GPU_COMMAND:
                raise CloudEnvError("GPU proof must execute the carrier CUDA Driver API probe")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        ):
            raise CloudEnvError("native command must be a nonempty explicit argv")
        flavor_id = grant["flavors"][phase]
        if flavor_id is None:
            raise CloudEnvError("grant does not authorize this phase's flavor")
        flavor = self.env.conn.compute.get_flavor(flavor_id)
        extra = self.env.conn.compute.fetch_flavor_extra_specs(flavor).extra_specs or {}
        alias = extra.get("pci_passthrough:alias", "")
        if phase == "gpu" and alias != "RTX-2000Ada:1,AD107-audio:1":
            raise CloudEnvError("GPU flavor PCI alias differs from approved RTX-2000Ada/audio binding")
        if phase == "cpu" and alias:
            raise CloudEnvError("CPU gate must not allocate a PCI passthrough flavor")
        group_id = self._tunnel_policy()
        self._check_time(grant)
        role = run_id if run_id is not None else "native-" + uuid.uuid4().hex
        if not re.fullmatch(r"native-[0-9a-f]{32}", role) or role in self.state["runs"]:
            raise CloudEnvError("native run identity invalid or already spent; HOLD, never replay")
        self._reserve(grant, ledger, phase, timeout)
        deadline = grant["expires_unix"]
        self.active_deadline = deadline
        run = {
            "schema": RUN_SCHEMA,
            "run_id": role,
            "phase": phase,
            "status": "creating",
            "exit_code": 1,
            "grant_id": grant["grant_id"],
            "source_digest": selected_image["source"]["manifest_digest"]
            if image is not None
            else grant["source_digest"],
            "package_sha256": grant["source_package_sha256"],
            "nonce": secrets.token_hex(16),
            "deadline": deadline,
            "seed_deadline": deadline,
            "allocation_seconds": timeout,
            "cleanup_reserve_seconds": grant.get("cleanup_reserve_seconds", 0),
            "flavor_id": flavor_id,
            "flavor_name": flavor.name,
            "boot_gib": grant["boot_gib"],
            "command": command,
            "keep": keep,
        }
        if "source_project_id" in grant:
            run["source_project_id"] = grant["source_project_id"]
        self.state["runs"][role] = run
        self._save()
        try:
            address = self._worker()
            self._check_time(grant)
            description = "palimpsest:" + self.env.state["marker"] + ":" + role
            if self._tunnel_policy() != group_id:
                raise CloudEnvError("native tunnel security group changed before port allocation; HOLD")
            self._check_time(grant)
            pending = self.env._intent("native_ports", role, description=description)
            port = self.env.conn.network.create_port(
                name="pal-" + role,
                network_id=self.env.state["network_id"],
                description=description,
                security_group_ids=[group_id],
            )
            record = self._confirmed_id("native_ports", role, pending, port.id)
            port = self.env.conn.network.get_port(port.id)
            self.env.record(
                "native_ports",
                role,
                {**record, "fixed_ips": port.fixed_ips, "security_group_ids": port.security_group_ids},
            )
            self._check_port(role, run, port)
            plan = _native_plan(
                selected_image, command, phase, self.env.state["project_id"], role, self._network(port), storage
            )
            run["plan"] = self._build(address, role, run, plan, deadline)
            image = self._upload_carrier(address, role, run, grant)
            self._check_time(grant)
            pending = self.env._intent(
                "native_volumes", role, boot_gib=run["boot_gib"], image_id=image.id, metadata=self._metadata(role, run)
            )
            volume = self.env.conn.block_storage.create_volume(
                size=run["boot_gib"], image_id=image.id, name="pal-" + role, metadata=self._metadata(role, run)
            )
            self._confirmed_id("native_volumes", role, pending, volume.id)
            # Exact-ID GET is authoritative; never project_id-filter Cinder collections.
            while True:
                volume = self.env.conn.block_storage.get_volume(volume.id)
                self.env._owned(volume, role=role)
                if volume.status == "available":
                    self._check_volume(role, run, volume, available=True)
                    break
                if volume.status not in ("creating", "downloading") or time.time() >= deadline:
                    raise CloudEnvError("native boot volume did not become available before Nova POST")
                time.sleep(3)
            self._check_time(grant)
            if image is not None:
                self._bind_ceph_mutation_boundary(grant, grant_path, role)
            # Activate storage access before compute allocation; no secret is delivered before fresh UUID binding.
            self.env.grant(role, "ro")
            self._check_time(grant)
            self._check_port(role, run, self.env.conn.network.get_port(port.id))
            self._check_image(role, run, self.env.conn.image.get_image(image.id))
            self._check_volume(role, run, self.env.conn.block_storage.get_volume(volume.id), available=True)
            if self._tunnel_policy() != group_id:
                raise CloudEnvError("native tunnel security group changed before Nova allocation; HOLD")
            if grant["expires_unix"] - time.time() < timeout:
                raise CloudEnvError("grant lacks the full post-Nova allocation window; cleanup only, no Nova POST")
            run["nova_post_unix"] = time.time()
            run["allocation_deadline"] = run["nova_post_unix"] + timeout
            deadline = int(run["allocation_deadline"]) - run["cleanup_reserve_seconds"]
            run["deadline"] = deadline
            self.active_deadline = deadline
            self._install_tunnel_key(address, role, self.directory / role, deadline)
            pending = self.env._intent(
                "native_servers", role, boot_volumes=[volume.id], metadata=self._metadata(role, run)
            )
            server = self.env.conn.compute.create_server(
                name="pal-" + role,
                flavor_id=flavor_id,
                networks=[{"port": port.id}],
                metadata=self._metadata(role, run),
                block_device_mapping_v2=[
                    {
                        "boot_index": 0,
                        "uuid": volume.id,
                        "source_type": "volume",
                        "destination_type": "volume",
                        "delete_on_termination": False,
                    }
                ],
            )
            run["server_id"] = _uuid(server.id)
            self._confirmed_id("native_servers", role, pending, server.id)
            self._save()
            while True:
                server = self.env.conn.compute.get_server(server.id)
                self.env._owned(server, role=role)
                if server.status == "ACTIVE":
                    run["nova_active_unix"] = time.time()
                    break
                if server.status not in ("BUILD", "REBUILD") or time.time() >= deadline:
                    raise CloudEnvError("Nova did not reach ACTIVE; native readiness is not established")
                time.sleep(3)
            plan = self._bind_server(role, run)
            self._check_time(grant)
            if selected_image["source"].get("kind") == "published":
                self.env.native_authorize_image()
                access = self.env.native_ceph_access(run)
                self._native_ceph_secrets[role] = access["secret"]
            else:
                rule = self.env._confirmed("access_rules", role)
                share = self.env._confirmed("shares", "layers")
                self.env._owned(self.env.conn.shared_file_system.get_share(share["id"]))
                if share["export"] not in {
                    item.path for item in self.env.conn.shared_file_system.export_locations(share["id"])
                }:
                    raise CloudEnvError("native Ceph export no longer belongs to the exact share")
                rules = {item.id: item for item in self.env.conn.shared_file_system.access_rules(share["id"])}
                fresh_rule = rules.get(rule["id"])
                if fresh_rule is None:
                    raise CloudEnvError("native read-only access rule disappeared before enrollment")
                self.env._check_rule(rule, fresh_rule)
                if (
                    fresh_rule.state != "active"
                    or fresh_rule.access_key != self.env._secret_path(rule["access_to"]).read_text()
                ):
                    raise CloudEnvError("native read-only credential state changed before enrollment")
                access = {
                    "export": share["export"],
                    "name": rule["access_to"],
                    "secret": self.env._secret_path(rule["access_to"]).read_text(),
                }
            # Recheck all bindings after storage authorization; no enrollment before UUID exists.
            plan = self._bind_server(role, run)
            enrollment = {
                "schema": "palimpsest.nova-enrollment.v1",
                "run_id": role,
                "client_cert_sha256": run["client_cert_sha256"],
                "expires_unix": deadline,
                "revoked": False,
                "plan": plan,
                "ceph_access": access,
            }
            _validate_native_enrollment(enrollment)
            run["enrollment_intent"] = True
            self._save()
            self._check_time(grant)
            self._remote_json(address, self.remote + "/enrollments/" + role + ".json", enrollment)
            run.update(status="bootstrapping", plan=plan, enrolled=True, enrollment_published_unix=time.time())
            self._save()
            while time.time() < deadline:
                result = self._read_result(address, run)
                if result is not None:
                    run["verified_result"] = result
                    run["status"] = "success" if self._successful(run, result) else "failed"
                    run["exit_code"] = 0 if run["status"] == "success" else 1
                    self._save()
                    break
                time.sleep(3)
            else:
                raise CloudEnvError("native bootstrap/result deadline expired; retain resources for exact cleanup")
        except Exception as error:
            self._remember_certificate_hold(error, run)
            run.update(status="held", failure_phase=run["status"], error_type=type(error).__name__)
            self._save()
        finally:
            try:
                if run["status"] != "success" and run.get("server_id"):
                    run["console_capture"] = self._capture_failure_console(run)
            finally:
                run["outcome"] = run["status"]
                self._save()
                if keep and run["status"] == "success":
                    run["status"] = "retained"
                    self._save()
                else:
                    try:
                        self.cleanup(grant_path=grant_path, run_id=role)
                    except Exception as error:
                        run.update(
                            status="cleanup_failed",
                            exit_code=1,
                            cleanup_errors=[{"resource": "cleanup", "error_type": type(error).__name__}],
                        )
                        self._save()
        return self._public(run)

    @staticmethod
    def _successful(run: dict, result: dict) -> bool:
        if not isinstance(result, dict) or any(
            not isinstance(result.get(key), dict) for key in ("checks", "root", "command", "gpu")
        ):
            return False
        checks = result.get("checks", {})
        root = result.get("root", {})
        command = result.get("command", {})
        expected_layers = [
            {"sha256": item["digest"].split(":")[-1], "size": item["size"]} for item in run["plan"]["image"]["layers"]
        ]
        runtime = run["plan"]["image"]["runtime"]
        if run["plan"]["image"]["source"].get("kind") == "published":
            identity_ok = (
                type(command.get("uid")) is int
                and command["uid"] >= 0
                and type(command.get("gid")) is int
                and command["gid"] >= 0
                and command.get("runtime_user") == runtime["user"]
            )
            numeric_user, separator, numeric_group = runtime["user"].partition(":")
            if numeric_user.isdigit():
                identity_ok = identity_ok and command["uid"] == int(numeric_user)
            if separator and numeric_group.isdigit():
                identity_ok = identity_ok and command["gid"] == int(numeric_group)
        else:
            identity_ok = command.get("uid") == 0 and command.get("gid") == 0
        good = (
            result.get("schema") == "palimpsest.nova-ceph-boot-result.v1"
            and result.get("run_id") == run["run_id"]
            and result.get("server_id") == run.get("server_id")
            and result.get("success") is True
            and root.get("filesystem") == "overlay"
            and root.get("pid1") == 1
            and root.get("proc1_same_root") is True
            and type(root.get("device")) is int
            and type(root.get("inode")) is int
            and root.get("proc1_device") == root["device"]
            and root.get("proc1_inode") == root["inode"]
            and isinstance(root.get("mount_id"), str)
            and root["mount_id"].isdigit()
            and root.get("mountinfo", {}).get("id") == root["mount_id"]
            and root.get("mountinfo", {}).get("target") == "/"
            and root.get("mountinfo", {}).get("fstype") == "overlay"
            and all(
                checks.get(key) is True
                for key in ("carrier_inventory", "instance_metadata", "plan_policy", "upper_private")
            )
            and checks.get("provider_readonly_errno") in (errno.EACCES, errno.EROFS)
            and checks.get("layers") == expected_layers
            and type(command.get("exit_code")) is int
            and command["exit_code"] == 0
            and identity_ok
            and command.get("entrypoint") == run["plan"]["image"]["runtime"]["entrypoint"]
            and command.get("argv") == run["plan"]["image"]["runtime"]["entrypoint"] + run["command"]
            and command.get("cmd_present") == ("cmd" in run["plan"]["image"]["runtime"])
        )
        gpu = result.get("gpu", {})
        if run["phase"] == "cpu":
            return bool(good and gpu.get("enabled") is False)
        compute = gpu.get("computation", {})
        return bool(
            good
            and gpu.get("enabled") is True
            and gpu.get("driver_version") == "580.178.04"
            and gpu.get("expected_pci_alias") == "RTX-2000Ada:1,AD107-audio:1"
            and gpu.get("kernel_release") == "6.8.0-147-generic"
            and isinstance(gpu.get("uuid"), str)
            and gpu["uuid"].startswith("GPU-")
            and gpu.get("smi_uuid") == gpu["uuid"]
            and gpu.get("pci")
            and compute.get("success") is True
            and compute.get("device_index") == 0
            and compute.get("device_uuid") == gpu["uuid"]
            and compute.get("checksum") == 330
            and compute.get("results") == [2, 6, 12, 20, 30, 42, 56, 72, 90]
            and type(compute.get("runtime_version")) is int
            and compute["runtime_version"] == 12020
            and compute.get("cublas_results") == [90, 114, 138, 54, 69, 84, 18, 24, 30]
            and type(compute.get("cublas_checksum")) in (int, float)
            and compute["cublas_checksum"] == 621
        )

    def _diagnostic_access_secret(self, run, rule):
        if rule.get("provider") == "ceph-namespace-v1":
            secret = self._native_ceph_secrets.get(run["run_id"])
            if secret is None:
                secret = self.env.native_ceph_access(run)["secret"]
            return secret
        from .nova_bootstrap_service import _read_private

        return _read_private(self.env._secret_path(rule["access_to"])).decode().strip()

    def _capture_build_failure(self, run: dict, response: subprocess.CompletedProcess, seed_files: dict) -> dict:
        """Retain bounded, private pipe output; SSH exit is not remote custody proof."""
        try:
            sensitive = [run["nonce"]]
            for name in ("ssh_private_key", "client_key"):
                key = seed_files[name].decode(errors="replace")
                sensitive.append(key)
                sensitive.extend(line for line in key.splitlines() if line and not line.startswith("-----"))
            rule = self.env.resource("access_rules", run["run_id"])
            if rule:
                sensitive.append(self._diagnostic_access_secret(run, rule))

            def sanitized(output: bytes) -> str:
                text = output.decode(errors="replace")
                for value in sensitive:
                    if value:
                        text = text.replace(value, "[REDACTED]")
                text = re.sub(
                    r"-----BEGIN [^\n]*PRIVATE KEY-----.*?(?:-----END [^\n]*PRIVATE KEY-----|\Z)",
                    "[REDACTED PRIVATE KEY]",
                    text,
                    flags=re.DOTALL,
                )
                text = "\n".join(
                    "[REDACTED CREDENTIAL LINE]"
                    if re.search(
                        r"password|passwd|secret|token|authorization|credential|cephx|private.?key|"
                        r"(?:api|access|signing)[_-]?key|bearer",
                        line,
                        re.I,
                    )
                    or re.search(r"://[^/\s:@]+:[^/\s@]+@", line)
                    else line
                    for line in text.splitlines()
                )
                # Redact before truncation so neither key blocks nor labelled lines
                # can lose their identifying prefix at the retained tail boundary.
                return text.encode()[-64 * 1024 :].decode(errors="ignore")

            payload = _canonical(
                {
                    "run_id": run["run_id"],
                    "unit": run["carrier_build_unit"],
                    "captured_unix": time.time(),
                    "transport_client_exit_code": response.returncode,
                    "remote_outcome": "unconfirmed",
                    "stdout": sanitized(response.stdout),
                    "stderr": sanitized(response.stderr),
                    "redaction": "known carried secrets, private-key blocks and credential-labelled lines",
                    "stream_tail_bytes": 64 * 1024,
                    "qualification": False,
                }
            )
            if len(payload) > 1024 * 1024:
                raise CloudEnvError("builder diagnostic exceeds the private artifact bound")
            path = self.directory / (run["run_id"] + ".build-failure.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            return {"status": "captured", "path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}
        except Exception:
            return {"status": "unavailable"}

    def _capture_failure_console(self, run: dict) -> dict:
        """Best-effort private diagnostics, before deletion; never qualification."""
        settings = []
        try:
            compute = self.env.conn.compute
            for target, name, value in (
                (compute.session, "timeout", 5),
                (compute, "connect_retries", 0),
                (compute, "status_code_retries", 0),
            ):
                settings.append((target, name, getattr(target, name)))
                setattr(target, name, value)
            server = compute.get_server(run["server_id"])
            self.env._owned(server, role=run["run_id"])
            if server.id != run["server_id"]:
                raise CloudEnvError("console server identity differs")
            response = compute.get_server_console_output(server.id, length=2000)
            output = response["output"]
            if not isinstance(output, str) or len(output.encode()) > 1024 * 1024:
                raise CloudEnvError("console response exceeds the diagnostic bound")
            original_sha256 = hashlib.sha256(output.encode()).hexdigest()
            sensitive = [run["nonce"]]
            rule = self.env.resource("access_rules", run["run_id"])
            from .nova_bootstrap_service import _read_private

            if rule:
                sensitive.append(self._diagnostic_access_secret(run, rule))
            if run.get("client_cert_sha256"):
                for name in ("ssh_private_key", "client-" + run["run_id"] + ".key"):
                    key = _read_private(self.directory / run["run_id"] / name).decode()
                    sensitive.extend(line for line in key.splitlines() if line and not line.startswith("-----"))
            for value in sensitive:
                if value:
                    output = output.replace(value, "[REDACTED]")
            output = re.sub(
                r"-----BEGIN [^\n]*PRIVATE KEY-----.*?(?:-----END [^\n]*PRIVATE KEY-----|\Z)",
                "[REDACTED PRIVATE KEY]",
                output,
                flags=re.DOTALL,
            )
            output = "\n".join(
                "[REDACTED CREDENTIAL LINE]"
                if re.search(r"password|passwd|secret|token|authorization|credential|cephx|private.?key", line, re.I)
                or re.search(r"://[^/\s:@]+:[^/\s@]+@", line)
                else line
                for line in output.splitlines()
            )
            text = output.encode()[-256 * 1024 :].decode(errors="ignore")
            payload = _canonical(
                {
                    "server_id": server.id,
                    "captured_unix": time.time(),
                    "original_sha256": original_sha256,
                    "redaction": "known carried secrets, private-key blocks and credential-labelled lines",
                    "console": text,
                    "qualification": False,
                }
            )
            path = self.directory / (run["run_id"] + ".console.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            return {"status": "captured", "path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}
        except Exception as error:
            return {"status": "unavailable", "error_type": type(error).__name__}
        finally:
            for target, name, value in reversed(settings):
                setattr(target, name, value)

    def _read_result(self, address: str, run: dict) -> dict | None:
        path = self.remote + "/results/" + run["run_id"] + ".result.json"
        script = (
            "import os,sys,stat; p=sys.argv[1]; "
            "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW) if os.path.lexists(p) else None; "
            "sys.exit(44) if fd is None else None; s=os.fstat(fd); "
            "assert stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==0 and s.st_nlink==1 and s.st_size<=1048576; "
            "sys.stdout.buffer.write(os.read(fd,1048577)); os.close(fd)"
        )
        response = self._ssh(
            address, "sudo python3 -I -c " + shlex.quote(script) + " " + shlex.quote(path), check=False
        )
        if response.returncode == 44:
            return None
        if response.returncode:
            raise CloudEnvError("bootstrap result read failed; absence is not established")
        wrapper = json.loads(response.stdout)
        if (
            wrapper.get("schema") != "palimpsest.nova-bootstrap-result.v1"
            or wrapper.get("run_id") != run["run_id"]
            or wrapper.get("nonce") != run["nonce"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(wrapper.get("request_sha256")))
        ):
            raise CloudEnvError("bootstrap result wrapper differs from the enrolled run/nonce")
        return wrapper["result"]

    def _public(self, run: dict) -> dict:
        report = {
            key: run[key]
            for key in (
                "schema",
                "run_id",
                "phase",
                "status",
                "exit_code",
                "server_id",
                "source_digest",
                "deadline",
                "seed_deadline",
                "allocation_seconds",
                "allocation_deadline",
                "cleanup_reserve_seconds",
                "keep",
                "nova_post_unix",
                "server_absent_unix",
                "nova_post_to_absence_seconds",
                "budget_exceeded",
                "outcome",
                "verified_result",
                "error_type",
                "failure_phase",
                "nova_active_unix",
                "enrollment_published_unix",
                "console_capture",
                "build_failure_capture",
                "upload_failure",
                "cleanup_errors",
                "certificate_hold",
            )
            if key in run
        }
        report["retained_resources"] = {
            kind: {"id": records[run["run_id"]].get("id"), "status": records[run["run_id"]]["status"]}
            for kind, records in self.env.state.get("resources", {}).items()
            if run["run_id"] in records and (kind.startswith("native_") or kind == "access_rules")
        }
        return report

    def status(self, *, run_id: str | None = None) -> dict:
        if run_id is None:
            return {
                "runs": [self._public(run) for run in self.state["runs"].values()],
                "preparation": self.state.get("preparation", {}).get("status"),
            }
        run = self.state["runs"].get(run_id)
        if run is None:
            raise CloudEnvError("unknown native run ID")
        if run.get("enrolled") and run["status"] not in ("success", "retained", "cleaned"):
            result = self._read_result(self._worker(), run)
            if result is not None:
                run["verified_result"] = result
                if run["status"] in ("creating", "bootstrapping"):
                    run["status"] = "success" if self._successful(run, result) else "failed"
                    run["exit_code"] = 0 if run["status"] == "success" else 1
                self._save()
        return self._public(run)

    def _revoke_enrollment(self, address: str, run: dict):
        if not run.get("enrollment_intent"):
            return
        path = self.remote + "/enrollments/" + run["run_id"] + ".json"
        script = (
            "import json,os,sys; p,r,c=sys.argv[1:]; sys.exit(44) if not os.path.lexists(p) else None; "
            "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); "
            "s=os.fdopen(fd); v=json.load(s); s.close(); "
            "assert v['run_id']==r and v['client_cert_sha256']==c; v['revoked']=True; "
            "q=p+'.revoke'; f=os.open(q,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); "
            "s=os.fdopen(f,'w'); json.dump(v,s); s.flush(); os.fsync(s.fileno()); s.close(); os.replace(q,p)"
        )
        response = self._ssh(
            address,
            "sudo python3 -I -c "
            + shlex.quote(script)
            + " "
            + shlex.join([path, run["run_id"], run["client_cert_sha256"]]),
            check=False,
        )
        if response.returncode not in (0, 44):
            raise CloudEnvError("enrollment revocation outcome unknown; HOLD")
        run.update(enrolled=False, enrollment_intent=False)
        self._save()

    def _remove_tunnel_key(self, address: str, role: str):
        record = self.env.resource("native_ssh_keys", role)
        if not record:
            return
        if record["worker_id"] != self.env.resource("servers", "worker")["id"]:
            raise CloudEnvError("tunnel key worker binding differs; HOLD")
        # The exact line is known before append: uncertain append can safely remove only this line.
        script = (
            inspect.getsource(_remove_authorized_key)
            + "\nimport sys\n_remove_authorized_key('/home/ubuntu/.ssh/authorized_keys', sys.stdin.buffer.read())\n"
        )
        self._ssh(address, "python3 -I -c " + shlex.quote(script), input=record["line"].encode())
        self.env.forget("native_ssh_keys", role)

    def cleanup(self, *, grant_path: Path, run_id: str | None = None, stop_service: bool = False) -> dict:
        grant, _ = self._grant(grant_path, "cleanup")
        self.transport_deadline = grant["cleanup_expires_unix"]
        if run_id is None:
            if not stop_service or any(run.get("status") != "cleaned" for run in self.state["runs"].values()):
                raise CloudEnvError("service-only cleanup requires --stop-service and no retained consumer runs")
            if any(
                records for kind, records in self.env.state.get("resources", {}).items() if kind.startswith("native_")
            ):
                raise CloudEnvError("native resources remain; clean their exact run before stopping service")
            preparation = self.state.get("preparation", {})
            if (
                preparation.get("grant_id") != grant.get("cleanup_of_grant_id", grant["grant_id"])
                or preparation.get("source_digest") != grant["source_digest"]
                or preparation.get("package_sha256") != grant["source_package_sha256"]
            ):
                raise CloudEnvError("cleanup grant does not own the prepared bootstrap service")
            unit = preparation.get("service_unit")
            if not unit:
                raise CloudEnvError("native service identity was not recorded; HOLD")
            self._cleanup_boundary(grant, grant_path)
            self._ssh(self._worker(), "sudo systemctl stop " + shlex.quote(unit))
            preparation.update(service_started=False, service_start_intent=False)
            self._save()
            return {"status": "service_stopped", "exit_code": 0}
        run = self.state["runs"].get(run_id)
        if run is None or not self._run_source_matches(grant, run):
            raise CloudEnvError("cleanup grant does not bind this native source/run")
        self._cleanup_boundary(grant, grant_path, run)
        if (self.env.resource("access_rules", run_id) or {}).get("provider") == "ceph-namespace-v1":
            self._bind_ceph_mutation_boundary(grant, grant_path, run_id)
        address = self._worker()
        errors = []
        # Revoke credential delivery and storage access even when a provider create is uncertain.
        for name, operation in (
            ("enrollment", lambda: self._revoke_enrollment(address, run)),
            ("access_rule", lambda: self.env._revoke(run_id)),
            ("tunnel_key", lambda: self._remove_tunnel_key(address, run_id)),
        ):
            try:
                self._cleanup_boundary(grant, grant_path, run)
                operation()
            except Exception as error:
                errors.append({"resource": name, "error_type": type(error).__name__})
        server_record = self.env.resource("native_servers", run_id)
        try:
            if server_record:
                self._cleanup_boundary(grant, grant_path, run)
                record = self.env._confirmed("native_servers", run_id)
                server = self.env._get(self.env.conn.compute.get_server, record["id"])
                if server is not None:
                    if record["status"] == "deleting":
                        self.env._owned(server, role=run_id)
                        if any((server.metadata or {}).get(k) != v for k, v in self._metadata(run_id, run).items()):
                            raise CloudEnvError("deleting native server metadata differs; HOLD")
                    else:
                        self._bind_server(run_id, run, deleting=True)
                    self._cleanup_boundary(grant, grant_path, run)
                    if record["status"] != "deleting":
                        self.env._deleting("native_servers", run_id)
                        self.env.conn.compute.delete_server(record["id"], ignore_missing=False)
                    self.env._wait_absent(self.env.conn.compute.get_server, record["id"])
                run["server_absent_unix"] = time.time()
                if run.get("nova_post_unix") is not None:
                    elapsed = run["server_absent_unix"] - run["nova_post_unix"]
                    run["nova_post_to_absence_seconds"] = round(elapsed, 3)
                    run["budget_exceeded"] = elapsed > run["allocation_seconds"]
                    if run["budget_exceeded"]:
                        run["outcome"] = "budget_exceeded"
                self.env.forget("native_servers", run_id)
        except Exception as error:
            errors.append({"resource": "server", "error_type": type(error).__name__})
        # Do not delete a port/volume/image behind an unresolved server allocation.
        server_clear = self.env.resource("native_servers", run_id) is None
        for kind, getter, deleter in (
            ("native_volumes", self.env.conn.block_storage.get_volume, self.env.conn.block_storage.delete_volume),
            ("native_ports", self.env.conn.network.get_port, self.env.conn.network.delete_port),
            ("native_images", self.env.conn.image.get_image, self.env.conn.image.delete_image),
        ):
            try:
                record = self.env._confirmed(kind, run_id)
                if not record:
                    continue
                if not server_clear:
                    raise CloudEnvError("server outcome unresolved; dependent cleanup held")
                self._cleanup_boundary(grant, grant_path, run)
                fresh = self.env._get(getter, record["id"])
                if fresh is not None:
                    if kind == "native_volumes":
                        self._check_volume(run_id, run, fresh)
                        allowed = ("available", "deleting") if record["status"] == "deleting" else ("available",)
                        if fresh.attachments or fresh.status not in allowed:
                            raise CloudEnvError("boot volume is not detached/available; HOLD")
                    elif kind == "native_ports":
                        self._check_port(run_id, run, fresh)
                        if fresh.device_id:
                            raise CloudEnvError("native port is still attached; HOLD")
                    else:
                        if self.env.resource("native_volumes", run_id):
                            raise CloudEnvError("boot volume cleanup unresolved; retain private carrier image")
                        self._check_image(run_id, run, fresh)
                    self._cleanup_boundary(grant, grant_path, run)
                    if record["status"] != "deleting":
                        self.env._deleting(kind, run_id)
                        deleter(record["id"], ignore_missing=False)
                    self.env._wait_absent(getter, record["id"])
                self.env.forget(kind, run_id)
            except Exception as error:
                errors.append({"resource": kind, "error_type": type(error).__name__})
        try:
            self._cleanup_boundary(grant, grant_path, run)
            self._carrier_terminal(address, run)
        except Exception as error:
            errors.append({"resource": "carrier_builder", "error_type": type(error).__name__})
        try:
            self._certificate_terminal(run)
        except Exception as error:
            errors.append({"resource": "certificate_process", "error_type": type(error).__name__})
        if stop_service:
            try:
                if (
                    any(
                        item["run_id"] != run_id and item["status"] != "cleaned" for item in self.state["runs"].values()
                    )
                    or errors
                ):
                    raise CloudEnvError("native service still has retained runs; refuse stop")
                unit = self.state["preparation"]["service_unit"]
                self._cleanup_boundary(grant, grant_path, run)
                self._ssh(address, "sudo systemctl stop " + shlex.quote(unit))
                self.state["preparation"]["service_started"] = False
                self.state["preparation"]["service_start_intent"] = False
            except Exception as error:
                errors.append({"resource": "service", "error_type": type(error).__name__})
        run.update(
            status="cleanup_failed" if errors else "cleaned",
            exit_code=1 if errors or run.get("outcome", run["status"]) != "success" else 0,
            cleanup_errors=errors,
        )
        self._save()
        return self._public(run)


def add_parser(operations):
    warning = (
        "Controller death does not trigger autonomous cloud resource deletion. "
        "SSH/enrollment expiry only revokes delivery; exact-owned cleanup needs a live controller "
        "and an unexpired cleanup approval. Results are trusted workload observations, not remote "
        "attestation. Carrier preparation is not boot/GPU proof."
    )
    native = operations.add_parser(
        "native", help="explicit grant-bound native OCI-root Nova proof", description=warning
    )
    commands = native.add_subparsers(dest="native_operation", required=True)
    prepare = commands.add_parser(
        "prepare", help="explicitly install reviewed source and prepare authenticated service", epilog=warning
    )
    prepare.add_argument("name", help="existing project-bound cloud environment name")
    prepare.add_argument(
        "tag",
        nargs="?",
        default=None,
        help="existing registry receipt for exact-image grants; omitted only for explicit project-scope preparation",
    )
    prepare.add_argument(
        "--grant", type=Path, required=True, help="owned mode-0600 approval JSON permitting preparation"
    )
    prepare.add_argument(
        "--config",
        type=Path,
        required=True,
        help="owned mode-0600 JSON with explicit source/service choices, bundle/runtime/probe paths and DNS",
    )
    run = commands.add_parser("run", help="one budget-reserved consumer; never automatically retry", epilog=warning)
    run.add_argument("name", help="existing prepared cloud environment name")
    run.add_argument("tag", help="digest-bound original registry image receipt name")
    run.add_argument(
        "--grant",
        type=Path,
        required=True,
        help="owned mode-0600 run+cleanup approval with cumulative budgets and exact flavor IDs",
    )
    run.add_argument(
        "--phase",
        choices=("cpu", "gpu"),
        required=True,
        help="CPU native-root gate or subsequent GPU computation proof",
    )
    run.add_argument(
        "--timeout",
        type=int,
        required=True,
        help="maximum allocation/result window in seconds from Nova POST; preparation uses the outer grant expiry",
    )
    run.add_argument(
        "--keep",
        action="store_true",
        help="retain successful idle PID1 consumer only with allow_retained approval; exact cleanup remains required",
    )
    run.add_argument(
        "command",
        nargs="+",
        help="explicit OCI Cmd override, preserving original Entrypoint; use -- before workload options",
    )
    status = commands.add_parser(
        "status", help="read exact authenticated service result; ACTIVE is not readiness", epilog=warning
    )
    status.add_argument("name", help="existing cloud environment name")
    status.add_argument("--run-id", help="exact native run ID; omit to list all retained run summaries")
    cleanup = commands.add_parser(
        "cleanup", help="revoke access and delete only exact ledger-owned consumer resources", epilog=warning
    )
    cleanup.add_argument("name", help="existing cloud environment name; worker and share are preserved")
    cleanup.add_argument("run_id", nargs="?", help="exact recorded run ID; omit only for service-only --stop-service")
    cleanup.add_argument(
        "--grant",
        type=Path,
        required=True,
        help="original bounded cleanup-tail grant or separately approved cleanup-only recovery grant",
    )
    cleanup.add_argument(
        "--stop-service",
        action="store_true",
        help="also stop the exact owned bootstrap unit with TERM only, after all consumers are cleaned",
    )


def dispatch(args):
    from .cloud_env import CloudEnv

    env = CloudEnv(args.name)
    if args.native_operation == "prepare":
        return env.native_prepare(grant_path=args.grant, tag=args.tag, config_path=args.config)
    if args.native_operation == "run":
        return env.native_run(
            grant_path=args.grant,
            tag=args.tag,
            phase=args.phase,
            timeout=args.timeout,
            command=args.command,
            keep=args.keep,
        )
    if args.native_operation == "status":
        return env.native_status(run_id=args.run_id)
    return env.native_cleanup(grant_path=args.grant, run_id=args.run_id, stop_service=args.stop_service)
