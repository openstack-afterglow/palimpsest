"""Nova-owned GPU allocation and cloud-init builds on retained Cinder storage.

No nested libvirt, host PCI management or caller credentials in a guest. Install
commands are explicit operator policy. ACTIVE is only a provider state: a separate
boot-bound guest result must attest successful capture and actual checks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .cloudinit import _BUILD_WORKER_SOURCE
from .errors import ArtifactValidationError, BuildError
from .layer_cache import _directory, _private_directory, _read_file
from .oci_image import strict_json_object
from .state import atomic_write_json

SCHEMA = "palimpsest.nova-build.v1"
RESULT_SCHEMA = "palimpsest.nova-build-result.v1"
ROOT = "/srv/palimpsest"
_HELPER = "/usr/libexec/palimpsest-nova-build"
_CAPTURE = "/usr/libexec/palimpsest-capture"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _fail(message: str) -> None:
    raise ArtifactValidationError("Nova build: " + message)


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail(label + " must be an explicit bounded identifier")
    return value


def _uuid(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            _fail("storage identity must be a canonical UUID")
    except (ValueError, AttributeError):
        _fail("storage identity must be a canonical UUID")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def spec_digest(spec: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(spec)).hexdigest()


def _commands(value: Any, label: str) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        _fail(label + " requires at least one explicit argv")
    for argv in value:
        if (
            not isinstance(argv, list)
            or not argv
            or not argv[0]
            or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
        ):
            _fail(label + " requires NUL-free argv arrays")
    return value


def validate_build_spec(spec: Any) -> dict:
    required = {"schema", "job_id", "project_id", "arch", "base_image_id", "storage", "commands", "checks"}
    optional = {"mode", "parent_layers", "packages"}
    if not isinstance(spec, dict) or not required <= spec.keys() or set(spec) - required - optional:
        _fail("invalid build spec fields")
    if spec["schema"] != SCHEMA:
        _fail("unsupported build spec schema")
    for key in ("job_id", "project_id", "base_image_id"):
        _id(spec[key], key)
    if spec["arch"] not in {"x86_64", "aarch64"}:
        _fail("target architecture is required")
    mode = spec.get("mode", "capture")
    if mode not in {"capture", "install"}:
        _fail("mode must be capture or install")
    storage = spec["storage"]
    if not isinstance(storage, dict) or set(storage) != {"volume_id", "filesystem_uuid", "size_gib", "initialize"}:
        _fail("storage requires exact volume, filesystem, size and initialization policy")
    for key in ("volume_id", "filesystem_uuid"):
        _uuid(storage[key])
    if type(storage["size_gib"]) is not int or not 1 <= storage["size_gib"] <= 16384:
        _fail("invalid layer volume size")
    if type(storage["initialize"]) is not bool:
        _fail("storage initialization must be boolean")
    commands = _commands(spec["commands"], "install commands")
    checks = _commands(spec["checks"], "GPU/runtime checks")
    parents = spec.get("parent_layers", [])
    if not isinstance(parents, list) or len(parents) > 25:
        _fail("at most 25 ordered parent layers are supported")
    for parent in parents:
        if not isinstance(parent, dict) or set(parent) != {"digest", "size"}:
            _fail("parent layer fields are invalid")
        if not isinstance(parent["digest"], str) or _DIGEST.fullmatch(parent["digest"]) is None:
            _fail("parent layer requires a canonical digest")
        if type(parent["size"]) is not int or parent["size"] < 96:
            _fail("parent layer requires its verified size")
    if len({p["digest"] for p in parents}) != len(parents):
        _fail("parent layers must be unique and base-to-leaf ordered")
    packages = spec.get("packages", [])
    if not isinstance(packages, list) or any(
        not isinstance(p, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:=~-]*", p) is None for p in packages
    ):
        _fail("packages must be explicit apt package names or versions")
    normalized = {
        **spec,
        "mode": mode,
        "commands": commands,
        "checks": checks,
        "parent_layers": parents,
        "packages": packages,
    }
    if len(_canonical(normalized)) > 128 * 1024:
        _fail("build spec exceeds its bound")
    return normalized


_GUEST = r"""#!/usr/bin/python3
import ctypes
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path('/srv/palimpsest')
SPEC = json.loads(SPEC_JSON)
RESULT = ROOT / 'builds' / SPEC['job_id'] / 'result.json'
START = time.monotonic()

def run(argv, **kwargs):
    return subprocess.run(argv, check=True, stdin=subprocess.DEVNULL, **kwargs)

def output(argv):
    return subprocess.check_output(argv, text=True).strip()

def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return 'sha256:' + result.hexdigest()

def safe_dir(path, create=False):
    for parent in reversed((path, *path.parents)):
        if create and not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('unsafe runtime directory')
    return path

def mount_storage():
    policy = SPEC['storage']
    # Serial belongs to the newly recorded Cinder resource, never a positional /dev/vdb guess.
    serials = {policy['volume_id'], policy['volume_id'][:20]}
    disks = []
    for entry in Path('/sys/class/block').iterdir():
        serial = entry / 'serial'
        if not serial.exists():
            serial = entry / 'device/serial'
        if serial.exists() and serial.read_text().strip() in serials and not (entry / 'partition').exists():
            disks.append(Path('/dev') / entry.name)
    if len(disks) != 1:
        raise RuntimeError('attached Cinder serial is absent or ambiguous')
    device = disks[0]
    if int(output(['blockdev', '--getsize64', str(device)])) != policy['size_gib'] * 1024**3:
        raise RuntimeError('Cinder device size mismatch')
    mounted = json.loads(output(['findmnt', '--json', '--output', 'TARGET,SOURCE', '--source', str(device)])) if subprocess.run(['findmnt', '--source', str(device)], stdout=subprocess.DEVNULL).returncode == 0 else {}
    if mounted:
        raise RuntimeError('layer device was already mounted before build activation')
    current = subprocess.run(['blkid', '-p', '-s', 'UUID', '-o', 'value', str(device)], capture_output=True, text=True)
    if current.returncode != 0:
        if not policy['initialize'] or output(['wipefs', '--noheadings', '--output', 'TYPE', str(device)]):
            raise RuntimeError('refusing to format a nonblank or unapproved device')
        run(['mkfs.ext4', '-F', '-U', policy['filesystem_uuid'], str(device)])
    elif current.stdout.strip() != policy['filesystem_uuid']:
        raise RuntimeError('existing filesystem UUID does not match build policy')
    safe_dir(ROOT, create=True)
    run(['mount', '-t', 'ext4', '-o', 'nosuid,nodev', str(device), str(ROOT)])
    if output(['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(ROOT)]) != policy['filesystem_uuid']:
        raise RuntimeError('mounted layer filesystem identity mismatch')
    safe_dir(ROOT)

def main():
    if os.geteuid() != 0 or platform.machine() != SPEC['arch']:
        raise RuntimeError('builder root or target architecture mismatch')
    mount_storage()
    # Keep parent SquashFS mounts out of service peer namespaces as well as
    # the capture child's namespace. Process exit releases these read-only mounts.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x00020000) != 0:  # CLONE_NEWNS
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), 'unshare builder mount namespace')
    run(['mount', '--make-rprivate', '/'])
    build = ROOT / 'builds' / SPEC['job_id']
    if build.exists():
        raise RuntimeError('build identity already exists; ambiguous work is retained, not replayed')
    safe_dir(build, create=True)
    (build / 'spec.json').write_text(json.dumps(SPEC, sort_keys=True))
    parent_mounts = []
    for index, parent in enumerate(SPEC['parent_layers']):
        blob = ROOT / 'blobs/sha256' / parent['digest'].split(':')[1]
        info = blob.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size != parent['size'] or digest(blob) != parent['digest']:
            raise RuntimeError('parent payload receipt mismatch')
        target = safe_dir(build / ('parent-' + str(index)), create=True)
        run(['mount', '-t', 'squashfs', '-o', 'loop,ro,nosuid,nodev', str(blob), str(target)])
        parent_mounts.append(str(target))
    logs = build / 'commands.log'
    with logs.open('xb') as log:
        if SPEC['mode'] == 'capture':
            import shlex
            job = {'network': 'default', 'capture_tmpfs': False, 'parent_mounts': parent_mounts,
                   'runs': [{'line': index+1, 'command': shlex.join(argv), 'env': {}, 'workdir': '/'}
                            for index, argv in enumerate(SPEC['commands'] + SPEC['checks'])]}
            Path('/etc/palimpsest').mkdir(mode=0o755, exist_ok=True)
            Path('/etc/palimpsest/build-job.json').write_text(json.dumps(job))
            env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
                   'PALIMPSEST_BUILD_CAPTURE': str(build / 'capture'),
                   'PALIMPSEST_BUILD_OUTPUT': str(build / 'output.squashfs'),
                   'PALIMPSEST_BUILD_RESULT': str(build / 'capture-result.json')}
            run(['/usr/bin/python3', '/usr/libexec/palimpsest-capture'], env=env, stdout=log, stderr=subprocess.STDOUT)
            captured = json.loads((build / 'capture-result.json').read_text())
            artifact = build / 'output.squashfs'
            if captured.get('status') != 'ok' or artifact.stat().st_size != captured['size'] or digest(artifact) != 'sha256:' + captured['sha256']:
                raise RuntimeError('capture result does not match produced bytes')
            blob_root = safe_dir(ROOT / 'blobs/sha256', create=True)
            final = blob_root / captured['sha256']
            artifact.chmod(0o400)
            try:
                # Atomic no-overwrite publication: a second successful build
                # may reuse identical bytes, never replace their retained inode.
                os.link(artifact, final, follow_symlinks=False)
            except FileExistsError:
                if final.is_symlink() or final.stat().st_size != captured['size'] or digest(final) != digest(artifact):
                    raise RuntimeError('retained output digest is corrupt')
            artifact.unlink()
            directory_fd = os.open(blob_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            payload = {'digest': 'sha256:' + captured['sha256'], 'size': captured['size'], 'path': str(final)}
        else:
            for argv in SPEC['commands'] + SPEC['checks']:
                run(argv, stdout=log, stderr=subprocess.STDOUT)
            payload = None
    report = {'schema': 'palimpsest.nova-build-result.v1', 'status': 'success',
              'job_id': SPEC['job_id'], 'project_id': SPEC['project_id'], 'arch': SPEC['arch'],
              'base_image_id': SPEC['base_image_id'], 'recipe_digest': SPEC_DIGEST,
              'mode': SPEC['mode'], 'storage': SPEC['storage'], 'parent_layers': SPEC['parent_layers'],
              'output': payload, 'checks_succeeded': len(SPEC['checks']),
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'started_seconds_after_boot': START, 'completed_seconds_after_boot': time.monotonic()}
    temporary = RESULT.with_suffix('.tmp')
    with temporary.open('x') as stream:
        json.dump(report, stream, sort_keys=True)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(RESULT)
    run(['sync'])
    print('PALIMPSEST_NOVA_BUILD_SUCCESS=' + json.dumps(report, sort_keys=True), flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('PALIMPSEST_NOVA_BUILD_FAILURE=' + type(error).__name__, flush=True)
        raise
"""


def build_nova_layer_user_data(spec: dict) -> str:
    """Pure account-free exporter; compose user/SSH policy separately, never cloud credentials."""
    spec = validate_build_spec(spec)
    helper = (
        "SPEC_JSON = "
        + repr(json.dumps(spec, sort_keys=True))
        + "\nSPEC_DIGEST = "
        + repr(spec_digest(spec))
        + "\n"
        + _GUEST
    )
    unit = (
        "[Unit]\nDescription=Nova Palimpsest attached-storage build\nAfter=network-online.target\n"
        "Wants=network-online.target\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/usr/bin/python3 {_HELPER}\nTimeoutStartSec=14400\n"
        "StandardOutput=journal+console\nStandardError=journal+console\n[Install]\nWantedBy=multi-user.target\n"
    )
    lines = ["#cloud-config", "package_update: true", "packages:"]
    for package in dict.fromkeys(["squashfs-tools", "e2fsprogs", *spec["packages"]]):
        lines.append("  - " + json.dumps(package))
    lines.append("write_files:")
    for path, mode, content in (
        (_HELPER, "0755", helper),
        (_CAPTURE, "0755", _BUILD_WORKER_SOURCE),
        ("/etc/systemd/system/palimpsest-nova-build.service", "0644", unit),
    ):
        lines.extend(["  - path: " + path, "    permissions: '" + mode + "'", "    owner: root:root", "    content: |"])
        lines.extend("      " + line for line in content.rstrip("\n").split("\n"))
    lines.extend(
        [
            "runcmd:",
            "  - "
            + json.dumps(
                [
                    "/bin/sh",
                    "-ec",
                    "systemctl daemon-reload && systemctl enable palimpsest-nova-build.service && systemctl start --no-block palimpsest-nova-build.service",
                ]
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _field(resource: Any, name: str, default=None):
    return resource.get(name, default) if isinstance(resource, dict) else getattr(resource, name, default)


class NovaController:
    """Durable exact-owned Nova lifecycle for operator jobs, with retained data volumes."""

    def __init__(self, connection: Any, project_id: str, receipts: Path):
        self.connection = connection
        self.project_id = _id(project_id, "project")
        self.receipts = receipts.expanduser().absolute()
        with _directory(self.receipts) as fd:
            _private_directory(fd)
        self._scope()

    def _scope(self):
        if self.connection.current_project_id != self.project_id:
            _fail("authenticated project differs from the explicit target")

    def _path(self, job_id: str) -> Path:
        return self.receipts / (_id(job_id, "job") + ".json")

    def _read(self, job_id: str) -> dict:
        with _directory(self.receipts) as fd:
            _private_directory(fd)
            result = strict_json_object(
                _read_file(fd, self._path(job_id).name, mode=0o600, maximum=1024 * 1024), "Nova receipt"
            )
        if result.get("project_id") != self.project_id or result.get("job_id") != job_id:
            _fail("receipt ownership mismatch")
        return result

    def _save(self, record: dict):
        atomic_write_json(self._path(record["job_id"]), record)

    def _owned(self, record: dict):
        self._scope()
        server = self.connection.compute.get_server(record["server_id"])
        if (
            _field(server, "project_id") != self.project_id
            or _field(server, "name") != record["name"]
            or _field(server, "metadata", {}).get("palimpsest_job") != record["job_id"]
            or _field(server, "metadata", {}).get("palimpsest_spec") != record["spec_digest"]
        ):
            _fail("server project, original identity or job marker changed")
        return server

    def create(
        self,
        *,
        job_id: str,
        name: str,
        image_id: str,
        flavor_id: str,
        network_id: str,
        security_group_ids: list[str],
        user_data: str,
        boot_gib: int = 40,
        boot_volume_type: str | None = None,
        data_volumes: list[dict] | None = None,
        require_gpu: bool = True,
    ) -> dict:
        self._scope()
        for label, value in (
            ("job", job_id),
            ("name", name),
            ("image", image_id),
            ("flavor", flavor_id),
            ("network", network_id),
        ):
            _id(value, label)
        if type(boot_gib) is not int or not 10 <= boot_gib <= 16384:
            _fail("boot volume size is invalid")
        if boot_volume_type is not None:
            _id(boot_volume_type, "boot volume type")
        if (
            not isinstance(user_data, str)
            or not user_data.startswith("#cloud-config")
            or len(user_data.encode()) > 65535
        ):
            _fail("cloud-init must be complete and within Nova's user-data limit")
        if not isinstance(security_group_ids, list) or not security_group_ids:
            _fail("explicit project-owned security groups are required")
        path = self._path(job_id)
        if path.exists() or path.is_symlink():
            _fail("job intent already exists; inspect instead of replaying a possibly accepted create")
        image = self.connection.image.get_image(image_id)
        if _field(image, "status") != "active":
            _fail("selected base image is not active")
        network = self.connection.network.get_network(network_id)
        if _field(network, "project_id") != self.project_id:
            _fail("network must be explicitly owned by the target project")
        for identity in security_group_ids:
            _id(identity, "security group")
            group = self.connection.network.get_security_group(identity)
            if _field(group, "project_id") != self.project_id:
                _fail("security group belongs to another project")
        flavor = self.connection.compute.get_flavor(flavor_id)
        # Flavor GET omits extra_specs before microversion 2.61. The dedicated
        # endpoint is authoritative on supported older Nova deployments as well.
        flavor = self.connection.compute.fetch_flavor_extra_specs(flavor)
        extra = _field(flavor, "extra_specs", {}) or {}
        flavor_at_request = {
            key: _field(flavor, key) for key in ("id", "name", "vcpus", "ram", "disk", "ephemeral", "swap")
        }
        flavor_at_request["extra_specs"] = dict(extra)
        if (
            require_gpu
            and not extra.get("pci_passthrough:alias")
            and not any(k.startswith("resources:") and ("VGPU" in k or "GPU" in k) for k in extra)
        ):
            _fail("selected flavor declares no Nova GPU allocation")
        mapping = [
            {
                "source_type": "image",
                "destination_type": "volume",
                "uuid": image_id,
                "boot_index": 0,
                "volume_size": boot_gib,
                "delete_on_termination": False,
            }
        ]
        if boot_volume_type is not None:
            mapping[0]["volume_type"] = boot_volume_type
        data_volumes = data_volumes or []
        if len({v.get("id") for v in data_volumes}) != len(data_volumes):
            _fail("data volume IDs must be unique")
        for item in data_volumes:
            if not isinstance(item, dict) or set(item) != {"id", "read_only"} or type(item["read_only"]) is not bool:
                _fail("volume binding requires ID and explicit read-only boolean")
            volume = self.connection.block_storage.get_volume(_uuid(item["id"]))
            if _field(volume, "project_id") != self.project_id:
                _fail("data volume belongs to another project")
            if item["read_only"]:
                metadata = _field(volume, "metadata", {}) or {}
                if str(metadata.get("readonly", "")).lower() != "true":
                    _fail("shared volume is not provider-marked read-only")
                if _field(volume, "attachments", []) and not _field(volume, "is_multiattach", False):
                    _fail("in-use shared volume lacks multiattach capability")
            elif _field(volume, "attachments", []) or _field(volume, "status") != "available":
                _fail("writable build volume must be exclusively unattached and available")
            mapping.append(
                {
                    "source_type": "volume",
                    "destination_type": "volume",
                    "uuid": item["id"],
                    "boot_index": -1,
                    "delete_on_termination": False,
                }
            )
        descriptor = {
            "image_id": image_id,
            "flavor_id": flavor_id,
            "network_id": network_id,
            "security_groups": security_group_ids,
            "boot_gib": boot_gib,
            "volumes": data_volumes,
            "user_data_sha256": hashlib.sha256(user_data.encode()).hexdigest(),
        }
        if boot_volume_type is not None:
            descriptor["boot_volume_type"] = boot_volume_type
        record = {
            "schema": "palimpsest.nova-job.v1",
            "job_id": job_id,
            "project_id": self.project_id,
            "name": name,
            "spec_digest": spec_digest(descriptor),
            "descriptor": descriptor,
            "flavor_at_request": flavor_at_request,
            "phase": "create-intent",
            "request_monotonic_seconds": time.monotonic(),
            "request_unix_seconds": time.time(),
        }
        # Exclusive durable intent is the create fence, including across CLI
        # processes. An uncertain accepted request is never automatically replayed.
        with _directory(self.receipts) as receipt_fd:
            _private_directory(receipt_fd)
            intent_fd = os.open(
                path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=receipt_fd
            )
            with os.fdopen(intent_fd, "wb") as stream:
                stream.write(_canonical(record) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(receipt_fd)
        server = self.connection.compute.create_server(
            name=name,
            flavor_id=flavor_id,
            networks=[{"uuid": network_id}],
            security_groups=[{"name": identity} for identity in security_group_ids],
            user_data=base64.b64encode(user_data.encode()).decode(),
            block_device_mapping_v2=mapping,
            metadata={
                "palimpsest_job": job_id,
                "palimpsest_spec": record["spec_digest"],
                "palimpsest_purpose": "nova-layer-workflow",
            },
        )
        record.update(server_id=_field(server, "id"), phase="created")
        self._save(record)
        return record

    def inspect(self, job_id: str) -> dict:
        record = self._read(job_id)
        if "server_id" not in record:
            return {**record, "uncertain_create": True}
        server = self._owned(record)
        return {
            **record,
            "status": _field(server, "status"),
            "addresses": _field(server, "addresses", {}),
            "attached_volumes": _field(server, "attached_volumes", []),
            "flavor_at_allocation": _field(server, "flavor", None),
        }

    def wait_active(self, job_id: str, *, timeout_seconds: float = 900) -> dict:
        if not 1 <= timeout_seconds <= 14400:
            _fail("invalid Nova wait deadline")
        record = self._read(job_id)
        deadline = time.monotonic() + timeout_seconds
        while True:
            server = self._owned(record)
            status = _field(server, "status")
            if status == "ACTIVE":
                record.update(
                    phase="active-not-ready",
                    active_monotonic_seconds=time.monotonic(),
                    attached_volumes=_field(server, "attached_volumes", []),
                    flavor_at_allocation=_field(server, "flavor", None),
                )
                self._save(record)
                return self.inspect(job_id)
            if status == "ERROR":
                record["phase"] = "provider-error"
                self._save(record)
                raise BuildError("Nova server entered ERROR; exact-owned resources and evidence retained")
            if time.monotonic() >= deadline:
                raise BuildError("Nova ACTIVE deadline expired; resources retained, no readiness claimed")
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def record_guest_result(self, job_id: str, result: dict) -> dict:
        """Transport must be host-key/instance bound. An arbitrary console substring is not authority."""
        record = self._read(job_id)
        server = self._owned(record)
        if _field(server, "status") != "ACTIVE":
            _fail("guest result requires the original active Nova instance")
        if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA or result.get("status") != "success":
            _fail("guest did not return a successful build result")
        if result.get("job_id") != job_id or result.get("project_id") != self.project_id or not result.get("boot_id"):
            _fail("guest result does not match the requested job and boot")
        if type(result.get("checks_succeeded")) is not int or result["checks_succeeded"] <= 0:
            _fail("guest runtime checks are absent")
        record.update(phase="guest-verified", guest_result=result, ready_monotonic_seconds=time.monotonic())
        self._save(record)
        return record

    def stop(self, job_id: str) -> dict:
        record = self._read(job_id)
        self._owned(record)
        self.connection.compute.stop_server(record["server_id"])
        record["phase"] = "stop-requested"
        self._save(record)
        return record

    def delete(self, job_id: str) -> dict:
        """Explicit exact-owned server deletion; all Cinder data and boot volumes are retained."""
        record = self._read(job_id)
        self._owned(record)
        response = self.connection.compute.get(
            "/servers/" + record["server_id"] + "/os-volume_attachments", microversion="2.79"
        )
        response.raise_for_status()
        attachments = response.json().get("volumeAttachments")
        if not isinstance(attachments, list) or any(
            item.get("delete_on_termination") is not False for item in attachments
        ):
            _fail("volume retention changed or could not be attested; refusing deletion")
        self.connection.compute.delete_server(record["server_id"], ignore_missing=False)
        record["phase"] = "delete-requested-volumes-retained"
        self._save(record)
        return record
