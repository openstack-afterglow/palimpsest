"""Explicit, experimental OCI containers inside disposable Lima/QEMU Linux VMs.

This is NOT native OCI-root isolation. No host container engine is used. Supply a
local cloud-init/Lima-compatible Linux carrier with working passwordless sudo,
containerd, nerdctl, runc, tar, sha256sum and the Lima guest agent dependencies
already installed. Carrier and OCI platform must match. Carrier bytes are pinned
by --vm-image-digest; no image, package or container downloads are performed.

Foreground execution owns the complete VM lifetime. SIGINT, SIGTERM and web-job
cancellation unwind the owned VM. A killed/crashed client leaves a durable record
and isolated LIMA_HOME for `palimpsest inspect/stop/rm NAME` recovery. Native run
ledgers, existing Lima homes, SSH agents and registry credentials are untouched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import lima, platforms, state
from .digest import require_digest
from .errors import ArtifactValidationError, LifecycleError, StateError
from .oci_process import OCIProcessSpec, image_process_vectors
from .oci_run_request import LocalOCIRunRequest
from .package_source import PackageSnapshot, snapshot_package
from .runtime_types import ExperimentalOCIRunResult
from .state import StatePaths

_BACKEND = platforms.BACKEND_LIMA_QEMU_OCI
_SCHEMA = "palimpsest.experimental-oci-vm.v1"
_INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_TIMEOUT = 900


class _Cancelled(BaseException):
    def __init__(self, number: int):
        self.number = number


@contextmanager
def _cancellable():
    previous = {}
    if threading.current_thread() is threading.main_thread():

        def cancel(number, _frame):
            for event in previous:
                signal.signal(event, signal.SIG_IGN)
            raise _Cancelled(number)

        for event in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
            previous[event] = signal.getsignal(event)
            signal.signal(event, cancel)
    try:
        yield
    finally:
        for event, handler in previous.items():
            signal.signal(event, handler)


def _directory(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise StateError("experimental OCI state directory must be owner-only and not a symlink")
    return path


def _run_directory(roots: StatePaths, name: str) -> Path:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise StateError("invalid experimental OCI run name")
    return roots.state / "experimental-oci-vms" / name


def exists(roots: StatePaths, name: str) -> bool:
    return _run_directory(roots, name).exists()


def _short_home(run_id: str) -> Path:
    # macOS AF_UNIX paths are bounded; proof/state roots may be arbitrarily long.
    return Path("/tmp").resolve() / ("plm-" + uuid.UUID(run_id).hex)


def _lima_home(directory: Path) -> Path:
    record = json.loads((directory / "state.json").read_text())
    expected = _short_home(record["run_id"])
    if record.get("lima_home") != str(expected):
        raise StateError("experimental Lima home does not match its run identity")
    if not expected.exists():
        raise StateError("experimental Lima home is missing; ownership cannot be recovered automatically")
    _directory(expected)
    marker = expected / ".palimpsest-owner"
    if marker.is_symlink() or marker.read_text() != record["run_id"]:
        raise StateError("experimental Lima home owner marker differs")
    return expected


def inspect(roots: StatePaths, name: str) -> dict[str, Any]:
    _directory(roots.state / "experimental-oci-vms")
    directory = _run_directory(roots, name)
    if not directory.exists():
        raise StateError("experimental OCI run not found")
    _directory(directory)
    path = directory / "state.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024 or info.st_uid != os.geteuid():
            raise StateError("invalid experimental OCI ledger")
        record = json.load(stream)
    try:
        valid = (
            isinstance(record, dict)
            and record.get("schema") == _SCHEMA
            and record.get("name") == name
            and record.get("backend") == _BACKEND
            and record.get("runtime_kind") == "oci-container"
            and str(uuid.UUID(record["run_id"])) == record["run_id"]
            and record.get("instance") == "oci"
            and record.get("lima_home") == str(_short_home(record["run_id"]))
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise StateError("invalid experimental OCI owner identity")
    return record


def _environment(directory: Path) -> dict[str, str]:
    # Allowlist, not a blacklist: no native/package credentials, Docker config,
    # cloud tokens, SSH sockets, proxy credentials or inherited Lima defaults.
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(directory / "home"),
        "LIMA_HOME": str(_lima_home(directory)),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }


def _command(directory: Path, argv: list[str], *, timeout: float | None = _TIMEOUT, attached: bool = False):
    try:
        process = subprocess.Popen(
            argv,
            env=_environment(directory),
            start_new_session=True,
            stdin=None if attached else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if attached else subprocess.PIPE,
            text=not attached,
        )
    except OSError as exc:
        raise LifecycleError(f"experimental OCI command unavailable: {argv[0]}; {exc.strerror}") from None
    try:
        if attached:
            assert process.stdout is not None
            with (directory / "console.log").open("ab") as log:
                while block := process.stdout.read1(65536):
                    log.write(block)
                    log.flush()
                    sys.stdout.buffer.write(block)
                    sys.stdout.buffer.flush()
            process.wait()
            output, error = None, None
        else:
            output, error = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    if not attached:
        with (directory / "console.log").open("a") as log:
            log.write((output or "") + (error or ""))
    return subprocess.CompletedProcess(argv, process.returncode, output, error)


def _checked(directory: Path, argv: list[str], *, timeout: float = _TIMEOUT):
    try:
        result = _command(directory, argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LifecycleError(
            f"experimental OCI {argv[0]} command timed out; inspect retained state and retry rm"
        ) from None
    if result.returncode:
        raise LifecycleError(
            f"experimental OCI {argv[0]} failed: {(result.stderr or result.stdout or '').strip()[-2000:]}"
        )
    return result


def _instances(directory: Path) -> list[dict[str, Any]]:
    result = _checked(directory, ["limactl", "list", "--format", "json"], timeout=30)
    return lima._decode_json_objects(result.stdout, "experimental OCI instance")


def _owned_instance(directory: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    instances = _instances(directory)
    matches = [item for item in instances if item.get("name") == record["instance"]]
    if len(matches) > 1:
        raise StateError("ambiguous experimental OCI instance identity")
    if not matches:
        return None
    lima._require_owned_instance(matches[0], record["run_id"], record["instance"])
    return matches[0]


def _write(directory: Path, record: dict[str, Any], status: str, **fields) -> None:
    with state.file_lock(directory / "ledger.lock"):
        path = directory / "state.json"
        if path.exists():
            current = json.loads(path.read_text())
            if current.get("run_id") != record["run_id"]:
                raise StateError("experimental OCI owner changed during mutation")
            if current.get("cleanup_complete"):
                record.update(current)
                if status != "removed":
                    fields["outcome"] = status
                status = "removed"
        record.update(status=status, **fields)
        state.atomic_write_json(path, record)


def _cleanup(directory: Path, record: dict[str, Any]) -> None:
    current = json.loads((directory / "state.json").read_text())
    if current.get("run_id") != record["run_id"]:
        raise StateError("experimental OCI owner changed before cleanup")
    record.update(current)
    if record.get("cleanup_complete"):
        return
    instance = _owned_instance(directory, record) if record.get("create_attempted") else None
    if instance is not None:
        _checked(directory, ["limactl", "delete", "--force", record["instance"]], timeout=120)
        if _owned_instance(directory, record) is not None:
            raise LifecycleError("experimental OCI VM still exists after deletion")
    for filename in ("carrier.img", "image.tar"):
        (directory / filename).unlink(missing_ok=True)
    home = _lima_home(directory)
    shutil.rmtree(home)
    outcome = record.get("outcome", record["status"])
    _write(directory, record, "removed", cleanup_complete=True, outcome=outcome)


def stop(roots: StatePaths, name: str) -> None:
    record = inspect(roots, name)
    directory = _run_directory(roots, name)
    if record.get("cleanup_complete"):
        return
    (directory / "stop.request").touch(mode=0o600, exist_ok=True)
    with state.file_lock(directory / "lifecycle.lock"):
        record = inspect(roots, name)
        if record.get("cleanup_complete"):
            return
        instance = _owned_instance(directory, record)
        if instance is not None and instance.get("status") == "Running":
            _checked(directory, ["limactl", "stop", "--force", record["instance"]], timeout=120)
        _write(directory, record, "stopped")


def remove(roots: StatePaths, name: str) -> None:
    record = inspect(roots, name)
    directory = _run_directory(roots, name)
    (directory / "stop.request").touch(mode=0o600, exist_ok=True)
    with state.file_lock(directory / "lifecycle.lock"):
        record = inspect(roots, name)
        _cleanup(directory, record)
    # Retain metadata and logs for provenance; removal is idempotent. A new name
    # is required for a new attempt rather than overwriting old evidence.


def logs(roots: StatePaths, name: str, *, follow: bool = False):
    record = inspect(roots, name)
    path = _run_directory(roots, name) / "console.log"
    offset = 0
    while True:
        current = inspect(roots, name)
        if current["run_id"] != record["run_id"]:
            raise StateError("experimental OCI log owner changed")
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                    raise StateError("invalid experimental OCI console log")
                stream.seek(offset)
                while block := stream.read(65536):
                    offset += len(block)
                    yield block
        if not follow or current.get("cleanup_complete") or current["status"] == "cleanup-failed":
            return
        time.sleep(0.1)


def _blob_document(archive: tarfile.TarFile, digest: str, snapshot: PackageSnapshot) -> dict[str, Any]:
    name = "blobs/sha256/" + digest.split(":", 1)[1]
    try:
        stream = archive.extractfile(name)
    except KeyError:
        if digest != snapshot.root_digest:
            raise ArtifactValidationError("verified OCI graph metadata is missing") from None
        stream = archive.extractfile("index.json")
    if stream is None:
        raise ArtifactValidationError("verified OCI metadata is not a file")
    with stream:
        data = stream.read(4 * 1024**2 + 1)
    if len(data) > 4 * 1024**2 or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ArtifactValidationError("verified OCI metadata binding changed")
    return json.loads(data)


def _selected_manifest(archive: tarfile.TarFile, snapshot: PackageSnapshot, platform: str):
    matches = []

    def visit(digest: str, media_type: str):
        document = _blob_document(archive, digest, snapshot)
        if media_type in _INDEX_TYPES:
            for child in document["manifests"]:
                if child.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest":
                    visit(child["digest"], child["mediaType"])
            return
        config = _blob_document(archive, document["config"]["digest"], snapshot)
        if f"{config['os']}/{config['architecture']}" == platform:
            matches.append((digest, media_type, document, config))

    visit(snapshot.root_digest, snapshot.root_media_type)
    if len(matches) != 1:
        raise ArtifactValidationError(
            f"verified OCI graph must contain exactly one {platform} image; found {len(matches)}"
        )
    return matches[0]


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size, member.mode = len(content), 0o400
    archive.addfile(member, io.BytesIO(content))


def _prepare_archive(request: LocalOCIRunRequest, destination: Path, image_ref: str) -> dict[str, Any]:
    # The existing package verifier freezes source bytes and verifies the full
    # pinned reachable graph, layer checksums AND uncompressed DiffIDs on macOS.
    with snapshot_package(request.source, manifest=request.manifest_digest) as snapshot:
        if snapshot.package_type != "oci-image":
            raise ArtifactValidationError("experimental execution requires an OCI image, not a runtime bundle")
        with tarfile.open(snapshot.archive, "r:") as source:
            digest, media, manifest, config = _selected_manifest(source, snapshot, request.platform)
            process = OCIProcessSpec.from_config(config.get("config"))
            if request.command_override is not None:
                entrypoint, _command = image_process_vectors(config.get("config"))
                process = process.with_command(entrypoint, request.command_override)
            process.require_bootable()
            descriptors = [
                {"digest": digest, "size": snapshot.graph[digest]["size_bytes"], "mediaType": media},
                manifest["config"],
                *manifest["layers"],
            ]
            root = {**descriptors[0], "annotations": {"org.opencontainers.image.ref.name": image_ref}}
            with (
                destination.open("xb") as output,
                tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as target,
            ):
                _add_bytes(target, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
                _add_bytes(target, "index.json", json.dumps({"schemaVersion": 2, "manifests": [root]}).encode())
                seen = set()
                for descriptor in descriptors:
                    if descriptor["digest"] in seen:
                        continue
                    seen.add(descriptor["digest"])
                    name = "blobs/sha256/" + descriptor["digest"].split(":", 1)[1]
                    stream = source.extractfile(name)
                    if stream is None:
                        raise ArtifactValidationError("verified OCI blob is unavailable")
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = descriptor["size"], 0o400
                    with stream:
                        target.addfile(member, stream)
            destination.chmod(0o400)
            return {
                "root_digest": snapshot.root_digest,
                "manifest_digest": digest,
                "config_digest": manifest["config"]["digest"],
                "source_archive_digest": snapshot.archive_digest,
            }


def _snapshot_carrier(source: Path, destination: Path, expected: str) -> None:
    require_digest(expected)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as input_file, destination.open("xb") as output:
        before = os.fstat(input_file.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactValidationError("VM carrier must be a regular local disk")
        while block := input_file.read(1024 * 1024):
            digest.update(block)
            output.write(block)
        after = os.fstat(input_file.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or "sha256:" + digest.hexdigest() != expected:
        destination.unlink()
        raise ArtifactValidationError("VM carrier checksum mismatch or source changed during snapshot")
    destination.chmod(0o400)


def _check_stopped(directory: Path) -> None:
    if (directory / "stop.request").exists():
        raise _Cancelled(signal.SIGTERM)


def _guest(directory: Path, record: dict[str, Any], *argv: str):
    _check_stopped(directory)
    owned = _owned_instance(directory, record)
    if owned is None:
        raise LifecycleError("owned experimental OCI VM is missing")
    return _checked(directory, ["limactl", "shell", record["instance"], "--", *argv])


def run(roots: StatePaths, request: LocalOCIRunRequest) -> ExperimentalOCIRunResult:
    if type(request) is not LocalOCIRunRequest:
        raise ArtifactValidationError("experimental OCI launch requires a typed local request")
    request.__post_init__()
    selected = platforms.select_backend(
        request.platform.removeprefix("linux/"),
        runtime_kind="oci-root",
        requested=request.backend,
        force_architecture=request.force_architecture,
    )
    if selected != _BACKEND:
        raise ArtifactValidationError("experimental OCI adapter requires its explicit backend")
    arch = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}[request.platform]
    missing = [tool for tool in ("limactl", "qemu-img", f"qemu-system-{arch}") if shutil.which(tool) is None]
    if missing:
        raise LifecycleError(f"experimental OCI runtime needs {', '.join(missing)}; install Lima 2.1.x and QEMU")
    if request.vm_image is None or request.vm_image_digest is None:
        raise LifecycleError(
            "experimental lima-qemu-oci requires --vm-image /absolute/local/carrier.qcow2 "
            "--vm-image-digest sha256:<digest>; provide a matching Linux cloud-init/Lima carrier "
            "with passwordless sudo, containerd, nerdctl, runc, tar and sha256sum preinstalled. "
            "No carrier or guest packages are downloaded automatically."
        )
    if (roots.runs / request.name).exists():
        raise StateError("run name is already owned by a native runtime; choose a different name")
    namespace = _directory(roots.state / "experimental-oci-vms")
    directory = namespace / request.name
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise StateError("experimental OCI name already has evidence; inspect/rm it and choose a new name") from None
    _directory(directory / "home")
    run_id = str(uuid.uuid4())
    home = _short_home(run_id)
    home.mkdir(mode=0o700)
    (home / ".palimpsest-owner").write_text(run_id)
    record = {
        "schema": _SCHEMA,
        "name": request.name,
        "run_id": run_id,
        "instance": "oci",
        "lima_home": str(home),
        "backend": _BACKEND,
        "runtime_kind": "oci-container",
        "platform": request.platform,
        "isolation_contract": "experimental-container-in-qemu-vm-not-native-oci-root",
        "cleanup_complete": False,
        "create_attempted": False,
        "network_mode": request.network.mode,
        "network_scope": "workload-only; carrier uses Lima user-mode networking",
        "ports": [p.to_dict() for p in request.network.published_ports],
    }
    _write(directory, record, "preparing")
    exit_code = 1
    with _cancellable():
        try:
            version = _checked(directory, ["limactl", "--version"], timeout=30)
            match = lima._LIMA_VERSION_RE.search(version.stdout)
            if match is None or not (2, 1, 0) <= tuple(map(int, match.groups())) < (3, 0, 0):
                raise LifecycleError("experimental OCI execution requires Lima >=2.1.0,<3")
            image_ref = "palimpsest.local/oci:" + uuid.UUID(run_id).hex
            binding = _prepare_archive(request, directory / "image.tar", image_ref)
            _snapshot_carrier(request.vm_image, directory / "carrier.img", request.vm_image_digest)
            disk_info = json.loads(
                _checked(directory, ["qemu-img", "info", "--output=json", str(directory / "carrier.img")]).stdout
            )
            format_data = disk_info.get("format-specific", {}).get("data", {})
            if (
                disk_info.get("format") not in {"qcow2", "raw"}
                or disk_info.get("backing-filename")
                or format_data.get("data-file")
            ):
                raise ArtifactValidationError(
                    "VM carrier must be a self-contained raw or qcow2 disk without backing or external data files"
                )
            disk_bytes = max(30, (int(disk_info["virtual-size"]) + 1024**3 - 1) // 1024**3) * 1024**3
            config = directory / "lima.yaml"
            config.write_text(
                lima.experimental_oci_config(
                    request,
                    carrier=directory / "carrier.img",
                    carrier_digest=request.vm_image_digest,
                    run_id=run_id,
                    disk_bytes=disk_bytes,
                )
            )
            _write(
                directory,
                record,
                "starting",
                **binding,
                vm_disk_bytes=disk_bytes,
                carrier_digest=request.vm_image_digest,
                image_reference=image_ref,
            )
            with state.file_lock(directory / "lifecycle.lock"):
                _check_stopped(directory)
                _write(directory, record, "starting", create_attempted=True)
                _checked(directory, ["limactl", "start", "--tty=false", "--name=" + record["instance"], str(config)])
            observed = _guest(directory, record, "uname", "-m").stdout.strip()
            if platforms.normalize_machine(observed) != arch:
                raise ArtifactValidationError(
                    f"VM carrier reported {observed}, expected {arch}; refusing image execution"
                )
            _guest(directory, record, "sudo", "-n", "nerdctl", "--namespace", "palimpsest", "info")
            guest_archive = "/tmp/palimpsest-" + uuid.UUID(run_id).hex + ".tar"
            _checked(
                directory, ["limactl", "copy", str(directory / "image.tar"), record["instance"] + ":" + guest_archive]
            )
            expected = hashlib.sha256()
            with (directory / "image.tar").open("rb") as image:
                while chunk := image.read(1024 * 1024):
                    expected.update(chunk)
            observed = _guest(directory, record, "sha256sum", guest_archive).stdout.split()[0]
            if observed != expected.hexdigest():
                raise ArtifactValidationError("guest OCI archive transfer checksum mismatch")
            _guest(
                directory,
                record,
                "sudo",
                "-n",
                "nerdctl",
                "--namespace",
                "palimpsest",
                "load",
                "--input",
                guest_archive,
            )
            loaded = json.loads(
                _guest(
                    directory,
                    record,
                    "sudo",
                    "-n",
                    "nerdctl",
                    "--namespace",
                    "palimpsest",
                    "image",
                    "inspect",
                    image_ref,
                ).stdout
            )
            if (
                not isinstance(loaded, list)
                or len(loaded) != 1
                or loaded[0].get("Id") != binding["config_digest"]
                or loaded[0].get("Os") != "linux"
                or loaded[0].get("Architecture") != request.platform.split("/")[1]
            ):
                raise ArtifactValidationError("guest loaded image identity/platform differs from verified OCI config")
            argv = [
                "sudo",
                "-n",
                "nerdctl",
                "--namespace",
                "palimpsest",
                "run",
                "--rm",
                "--pull=never",
                "--name",
                "palimpsest-workload",
                "--network",
                "host" if request.network.enabled else "none",
            ]
            if request.user_override is not None:
                user = request.user_override
                argv += ["--user", user.user + (":" + user.group if user.group is not None else "")]
            argv.append(image_ref)
            argv.extend(request.command_override or ())
            _write(directory, record, "running")
            result = _command(
                directory, ["limactl", "shell", record["instance"], "--", *argv], timeout=None, attached=True
            )
            exit_code = result.returncode if result.returncode >= 0 else 128 - result.returncode
            _write(directory, record, "exited", exit_code=exit_code)
        except _Cancelled as exc:
            exit_code = 128 + exc.number
            _write(directory, record, "cancelled", exit_code=exit_code)
        except BaseException as exc:
            _write(directory, record, "failed", failure_category=type(exc).__name__)
            raise
        finally:
            if threading.current_thread() is threading.main_thread():
                for event in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
                    signal.signal(event, signal.SIG_IGN)
            try:
                with state.file_lock(directory / "lifecycle.lock"):
                    _cleanup(directory, record)
            except Exception as exc:
                _write(directory, record, "cleanup-failed", cleanup_complete=False)
                raise LifecycleError(
                    f"experimental VM cleanup failed; run palimpsest rm {request.name}; "
                    f"owner record: {directory / 'state.json'}; {exc}"
                ) from exc
    return ExperimentalOCIRunResult(request.name, run_id, str(directory / "state.json"), exit_code)
