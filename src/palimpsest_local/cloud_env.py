"""Local orchestration of Nova build workers and Manila (CephFS) layer stores.

The local CLI drives OpenStack directly through openstacksdk and a named
``clouds.yaml`` cloud, the same SDK path the Hub uses for API requests:

* ``env up``: one Manila CephFS share, one worker Nova VM (boot-from-volume,
  floating IP, SSH host key pinned from the console) with a read-write cephx
  identity mounted at ``/mnt/palimpsest-layers``.
* ``dockerfile-build``: upload a Dockerfile and context to the worker, which
  builds with BuildKit, converts to ordered SquashFS layers and publishes them
  write-once into the share (``cloud_layer_worker``).
* ``layer-run``: a fresh consumer VM gets its own **read-only** cephx access
  rule, mounts the share, loop-mounts every layer read-only, assembles an
  overlay at ``/opt/layers/merged`` and runs a command inside it. No
  Palimpsest code runs in the consumer.

Every created resource is recorded in a private per-environment state file
before use; ``env down`` and failure cleanup delete only recorded IDs.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import inspect
import io
import ipaddress
import json
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from contextlib import suppress
from functools import wraps
from pathlib import Path
from typing import Any

from .buildkit import BuildKitSpec, validate_online_dockerfile
from .errors import PalimpsestError
from .image_policy_channel import run_policy_process
from .image_source_policy import ImageSourcePolicy, ImageSourcePolicyError, load_image_source_policy
from .state import resolve_roots

SCHEMA = "palimpsest.cloud-env.v1"
STORE_MOUNT = "/mnt/palimpsest-layers"
WORKER_SCRATCH = "/var/lib/palimpsest-cloud"
BUILDKIT_URL = "https://github.com/moby/buildkit/releases/download/v0.34.1/buildkit-v0.34.1.linux-amd64.tar.gz"
BUILDKIT_SHA256 = "a59539daf547fe0958efe2d07e41c0a6944e1112a66d7e519ec1cf5b673ef15b"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
CEPHX_KEY_RE = re.compile(r"[A-Za-z0-9+/=]{20,128}")
HOST_KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]+")
SSH_USER = "ubuntu"
OUTPUT_TAIL_BYTES = 64 * 1024

WORKER_PACKAGES = ["ceph-common", "squashfs-tools", "skopeo", "e2fsprogs", "util-linux", "python3"]
CONSUMER_PACKAGES = ["ceph-common", "squashfs-tools", "util-linux", "python3"]


def _operation(method):
    """Serialize the complete local transaction, including fresh credential scope."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._reload()
            if method.__name__ not in {"status", "down", "native_status", "native_cleanup"} and any(
                item.get("status") == "deleting"
                for group in self.state.get("resources", {}).values()
                for item in group.values()
            ):
                raise CloudEnvError("environment has pending deletion; HOLD until down completes")
            self._connection = None
            if self.state and method.__name__ not in {"status", "up"}:
                self._authenticate(self.state["cloud"])
            return method(self, *args, **kwargs)

    return locked


def _retain_output(output: dict) -> dict:
    """Retain UTF-8-safe stream tails without changing command status or other evidence."""
    retained = dict(output)
    metadata = {}
    for stream in ("stdout", "stderr"):
        value = retained.get(stream)
        if not isinstance(value, str):
            continue
        encoded = value.encode("utf-8")
        tail = encoded[-OUTPUT_TAIL_BYTES:].decode("utf-8", errors="ignore")
        retained[stream] = tail
        metadata[stream] = {
            "original_bytes": len(encoded),
            "retained_bytes": len(tail.encode("utf-8")),
            "truncated": len(encoded) > OUTPUT_TAIL_BYTES,
        }
    retained["output_retention"] = metadata
    return retained


def _validate_image(image: dict, *, require_root: bool = True) -> None:
    if not isinstance(image, dict) or not isinstance(image.get("layers"), list) or not image["layers"]:
        raise CloudEnvError("image requires a nonempty ordered layer list")
    for layer in image["layers"]:
        if (
            not isinstance(layer, dict)
            or not isinstance(layer.get("digest"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", layer["digest"]) is None
            or type(layer.get("size")) is not int
            or layer["size"] <= 0
        ):
            raise CloudEnvError("image layer digest/size is invalid")
    runtime = image.get("runtime")
    if not isinstance(runtime, dict):
        raise CloudEnvError("image runtime must be an object")
    if require_root and runtime.get("user") not in (None, "", "0", "root", "0:0", "root:root"):
        raise CloudEnvError("nonroot image User is not supported; refusing to run it as root")
    workdir = runtime.get("working_dir")
    if workdir is not None and (
        not isinstance(workdir, str) or "\x00" in workdir or (workdir and not workdir.startswith("/"))
    ):
        raise CloudEnvError("image WorkingDir must be an absolute path")
    env = runtime.get("env")
    if env is not None and (
        not isinstance(env, list)
        or any(
            not isinstance(item, str) or "\x00" in item or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", item) is None
            for item in env
        )
    ):
        raise CloudEnvError("image Env must contain KEY=value strings")
    for key in ("entrypoint", "cmd"):
        value = runtime.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) or "\x00" in item for item in value)
        ):
            raise CloudEnvError(f"image {key} must contain argument strings")


def _validate_registry_image(record: dict) -> None:
    """Check the ordered worker projection before retaining it for a consumer."""
    _validate_image(record, require_root=False)
    source = record["source"]
    manifest = source["manifest"]
    config = source["config"]
    originals = manifest["layers"]
    diff_ids = config["rootfs"]["diff_ids"]
    if (
        config.get("architecture") != "amd64"
        or config.get("os") != "linux"
        or config["rootfs"].get("type") != "layers"
        or len(record["layers"]) != len(originals)
        or len(originals) != len(diff_ids)
    ):
        raise CloudEnvError("worker registry receipt has inconsistent platform or layer count")
    fields = ("mediaType", "digest", "size")
    if {field: manifest["config"][field] for field in fields} != source["config_descriptor"]:
        raise CloudEnvError("worker config descriptor differs from its manifest")
    for descriptor, field in (
        ("root_descriptor", "root_digest"),
        ("manifest_descriptor", "manifest_digest"),
        ("config_descriptor", "config_digest"),
    ):
        if source[descriptor]["digest"] != source[field]:
            raise CloudEnvError("worker source descriptor binding differs")
    for ordinal, layer in enumerate(record["layers"]):
        if (
            type(layer.get("ordinal")) is not int
            or layer["ordinal"] != ordinal
            or layer.get("compressed") != {field: originals[ordinal][field] for field in fields}
            or layer.get("diff_id") != diff_ids[ordinal]
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(layer.get("diff_id"))) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(layer.get("conversion_key"))) is None
            or re.fullmatch(r"receipts/sha256/[0-9a-f]{64}\.json", str(layer.get("receipt"))) is None
        ):
            raise CloudEnvError("worker layer provenance contradicts its original occurrence")
    process = config.get("config") or {}
    expected = {
        "entrypoint": process.get("Entrypoint") or [],
        "env": process.get("Env") or [],
        "working_dir": process.get("WorkingDir") or "/",
        "user": process.get("User") or "0",
    }
    if "Cmd" in process:
        expected["cmd"] = process["Cmd"]
    if record["runtime"] != expected:
        raise CloudEnvError("worker runtime projection differs from the original image config")


def _validate_network_inputs(dns: list[str], cidrs: list[str]) -> None:
    try:
        for address in dns:
            if not isinstance(address, str) or re.fullmatch(r"[0-9A-Fa-f:.]+", address) is None:
                raise ValueError("DNS scope IDs and non-IP characters are unsupported")
            ipaddress.ip_address(address)
        for cidr in cidrs:
            if not isinstance(cidr, str) or re.fullmatch(r"[0-9A-Fa-f:./]+", cidr) is None:
                raise ValueError("CIDR contains unsupported characters")
            ipaddress.ip_network(cidr, strict=True)
    except ValueError as error:
        raise CloudEnvError("DNS must be IP addresses and SSH sources must be canonical CIDRs") from error


class CloudEnvError(PalimpsestError):
    pass


def _ceph_source_identity(source: str) -> tuple:
    """Bind literal monitor addresses/ports and share path, without DNS adoption."""
    import ipaddress
    import re

    if not isinstance(source, str) or ":/" not in source:
        raise ValueError("unsupported Ceph source syntax; require literal monitors:/path")
    monitors, path = source.rsplit(":/", 1)
    if (
        not monitors
        or re.fullmatch(r"[A-Za-z0-9_./-]*", path) is None
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError("unsafe Ceph source path")
    endpoints = []
    for value in monitors.split(","):
        port = None
        if value.startswith("["):
            match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", value)
            if match is None:
                raise ValueError("invalid bracketed Ceph monitor")
            host, port = match.groups()
        else:
            host = value
            try:
                ipaddress.ip_address(host)
            except ValueError:
                host, separator, port = value.rpartition(":")
                if not separator or ":" in host:
                    raise ValueError("hostname Ceph exports are unsupported; literal IP addresses required") from None
        address = ipaddress.ip_address(host)
        if getattr(address, "scope_id", None):
            raise ValueError("scoped Ceph monitor addresses are unsupported")
        if port is not None and (not port.isdecimal() or not 1 <= int(port) <= 65535):
            raise ValueError("invalid Ceph monitor port")
        endpoints.append((str(address), int(port) if port is not None else 0))
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("duplicate Ceph monitor endpoints")
    return tuple(sorted(endpoints)), "/" + path.rstrip("/")


def _cloud_config() -> str:
    # Packages are installed over SSH after boot so failures are visible and retryable.
    return "#cloud-config\npackage_update: false\n"


def _provision_script(packages: list[str], dns: list[str]) -> str:
    _validate_network_inputs(dns, [])
    resolver = ""
    if dns:
        servers = " ".join(dns)
        resolver = (
            "install -d /etc/systemd/resolved.conf.d; "
            f"printf '[Resolve]\\nDNS={servers}\\n' > /etc/systemd/resolved.conf.d/palimpsest.conf; "
            "systemctl restart systemd-resolved; "
        )
    return (
        "set -euo pipefail; export DEBIAN_FRONTEND=noninteractive; "
        + resolver
        + "apt-get update -q >/dev/null; "
        + "apt-get install -y -q --no-install-recommends "
        + " ".join(packages)
        + " >/dev/null; "
        + 'modprobe ceph 2>/dev/null || { apt-get install -y -q "linux-modules-extra-$(uname -r)" >/dev/null && modprobe ceph; }; '
        + "echo provisioned"
    )


def _now() -> float:
    return round(time.time(), 3)


def package_archive() -> tuple[bytes, str]:
    """Deterministic tar of the installed palimpsest_local package (stdlib-only) for the worker."""
    package = Path(__file__).resolve().parent
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(package.rglob("*")):
            if (
                "__pycache__" in path.parts
                or path.is_dir()
                or path.suffix in {".pyc", ".pyo"}
                or path.name == ".DS_Store"
            ):
                continue
            info = archive.gettarinfo(str(path), arcname="palimpsest_local/" + path.relative_to(package).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            info.mode = 0o644
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def context_archive(context: Path) -> bytes:
    """Fail closed on unsupported ignore semantics before any context upload."""
    context = context.resolve(strict=True)
    if not context.is_dir():
        raise CloudEnvError(f"build context is not a directory: {context}")
    try:
        (context / ".plmignore").lstat()
    except FileNotFoundError:
        pass
    else:
        raise CloudEnvError(
            ".plmignore is unsupported for cloud env remote build contexts; "
            "use the local BuildKit build route to apply .plmignore"
        )
    ignored = {".git"}
    ignore_file = context / ".dockerignore"
    if ignore_file.is_symlink():
        raise CloudEnvError(".dockerignore must not be a symlink")
    if ignore_file.is_file():
        for line in ignore_file.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            entry = line.strip().strip("/")
            if (
                not entry
                or any(ch in entry for ch in "*?![]\\")
                or any(part in {".", "..", ""} for part in entry.split("/"))
            ):
                raise CloudEnvError(
                    "unsupported .dockerignore pattern; supported subset is literal root-relative file/directory paths, comments and blank lines (no globs, negation or escapes)"
                )
            ignored.add(entry)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(context.rglob("*")):
            relative = path.relative_to(context).as_posix()
            if any(relative == entry or relative.startswith(entry + "/") for entry in ignored):
                continue
            if path.is_file() and not path.is_symlink():
                info = archive.gettarinfo(str(path), arcname=relative)
                info.type = tarfile.REGTYPE
                info.linkname = ""
                info.size = path.stat().st_size
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            elif path.is_symlink() or path.is_dir():
                archive.add(str(path), arcname=relative, recursive=False)
    return buffer.getvalue()


class CloudEnv:
    def __init__(self, name: str, *, root: Path | None = None):
        if not NAME_RE.fullmatch(name):
            raise CloudEnvError("environment name must match [a-z0-9][a-z0-9-]{0,30}")
        self.name = name
        self.directory = (root or resolve_roots().state / "cloud-envs") / name
        self.state_path = self.directory / "state.json"
        self.key_path = self.directory / "id_ed25519"
        self.known_hosts = self.directory / "known_hosts"
        self._connection = None
        self.state: dict[str, Any] = {}
        self._reload()

    def _reload(self) -> None:
        self.state = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if self.state.get("schema") != SCHEMA or self.state.get("name") != self.name:
                raise CloudEnvError("environment state file does not belong to this environment")

    def _authenticate(self, cloud: str) -> None:
        from .cloud_cli import _connect

        connection = _connect(cloud)
        project = connection.current_project_id
        if not project or (self.state.get("project_id") and project != self.state["project_id"]):
            raise CloudEnvError(
                "clouds.yaml credentials do not match the stored environment project; no effects allowed"
            )
        if self.state.get("user_id") and connection.current_user_id != self.state["user_id"]:
            raise CloudEnvError("clouds.yaml user differs from the keypair-owning environment user; HOLD")
        self._connection = connection

    # ── state and connection ─────────────────────────────────────────────
    def save(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".state.{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.state, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def record(self, kind: str, role: str, value: dict) -> dict:
        self.state.setdefault("resources", {}).setdefault(kind, {})[role] = value
        self.save()
        return value

    def resource(self, kind: str, role: str) -> dict | None:
        return self.state.get("resources", {}).get(kind, {}).get(role)

    def forget(self, kind: str, role: str, *, absent: bool = True) -> None:
        value = self.resource(kind, role)
        if value is not None:
            self.state.setdefault("history", []).append(
                {
                    "kind": kind,
                    "role": role,
                    "resource": dict(value),
                    "absent_at" if absent else "reference_released_at": _now(),
                }
            )
            self.state["resources"][kind].pop(role)
        self.save()

    def _intent(self, kind: str, role: str, **values) -> dict:
        if not self.state.get("marker"):
            raise CloudEnvError("legacy environment has no unique ownership marker; HOLD for manual review")
        return self.record(
            kind,
            role,
            {
                "id": None,
                "status": "creating",
                "project_id": self.state["project_id"],
                "requested_at": _now(),
                **values,
            },
        )

    def _confirmed(self, kind: str, role: str) -> dict | None:
        value = self.resource(kind, role)
        if value is not None and (value.get("status") not in {"confirmed", "deleting"} or not value.get("id")):
            raise CloudEnvError(f"{kind}:{role} outcome is unknown; HOLD, never replay or adopt by name")
        return value

    def _deleting(self, kind: str, role: str) -> None:
        value = self._confirmed(kind, role)
        if value["status"] != "deleting":
            self.state.setdefault("history", []).append(
                {"kind": kind, "role": role, "delete_intent_at": _now(), "resource": dict(value)}
            )
            self.record(kind, role, {**value, "status": "deleting"})

    def _get(self, getter, identity):
        from openstack.exceptions import NotFoundException

        try:
            result = getter(identity)
        except NotFoundException as error:
            if error.status_code == 404:
                return None
            raise
        if result is None:
            raise CloudEnvError("provider returned no object without an exact HTTP 404")
        return result

    def _wait_absent(self, getter, identity: str, *, seconds: int = 600) -> None:
        deadline = time.monotonic() + seconds
        while self._get(getter, identity) is not None:
            if time.monotonic() >= deadline:
                raise CloudEnvError(f"resource {identity} is still present after deletion")
            time.sleep(3)

    def _project(self, resource) -> None:
        if resource.project_id != self.state["project_id"]:
            raise CloudEnvError("fresh resource project differs from the environment; HOLD")

    def _owned(self, resource, *, role: str | None = None) -> None:
        self._project(resource)
        metadata = resource.metadata or {}
        if (
            not self.state.get("marker")
            or metadata.get("palimpsest_env_id") != self.state["marker"]
            or metadata.get("palimpsest_cloud_env") != self.name
        ):
            raise CloudEnvError("fresh resource ownership marker differs; HOLD")
        if role is not None and metadata.get("palimpsest_role") != role:
            raise CloudEnvError("fresh server role differs; HOLD")

    @property
    def conn(self):
        if self._connection is None:
            raise CloudEnvError("operation requires fresh project-bound authentication")
        return self._connection

    def _secret_path(self, access_to: str) -> Path:
        return self.directory / f"cephx-{access_to}.key"

    def _write_private(self, path: Path, data: str) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data)

    # ── SSH ──────────────────────────────────────────────────────────────
    def ssh_argv(self, address: str) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.key_path),
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=30",
            f"{SSH_USER}@{address}",
        ]

    def ssh(
        self, address: str, command: str, *, input: bytes | None = None, timeout: float = 600, check: bool = True
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [*self.ssh_argv(address), command], input=input, capture_output=True, timeout=timeout, check=False
        )
        if check and result.returncode != 0:
            tail = result.stderr.decode(errors="replace")[-2000:]
            raise CloudEnvError(f"remote command failed rc={result.returncode}: {tail}")
        return result

    def upload(self, address: str, data: bytes, remote: str, *, timeout: float = 1800) -> None:
        self.ssh(address, f"cat > {shlex.quote(remote)}", input=data, timeout=timeout)

    def _pin_host_key(self, server_id: str, address: str, deadline: float) -> None:
        """Pin the guest's host keys from the cloud-init console block; never trust-on-first-use."""
        keys: list[str] = []
        while time.monotonic() < deadline:
            with suppress(Exception):
                output = self.conn.compute.get_server_console_output(server_id)["output"]
                if "-----END SSH HOST KEY KEYS-----" in output:
                    block = output.split("-----BEGIN SSH HOST KEY KEYS-----", 1)[1].split(
                        "-----END SSH HOST KEY KEYS-----", 1
                    )[0]
                    keys = [m.group(0) for line in block.splitlines() if (m := HOST_KEY_RE.match(line.strip()))]
                    if keys:
                        break
            time.sleep(5)
        if not keys:
            raise CloudEnvError("guest SSH host keys did not appear on the console before the deadline")
        existing = self.known_hosts.read_text().splitlines() if self.known_hosts.exists() else []
        kept = [line for line in existing if not line.startswith(address + " ")]
        self._write_private(self.known_hosts, "\n".join([*kept, *(f"{address} {key}" for key in keys)]) + "\n")

    def _wait_ssh(self, address: str, deadline: float) -> dict:
        while time.monotonic() < deadline:
            result = subprocess.run(
                [*self.ssh_argv(address), "cloud-init status --wait --format json"],
                capture_output=True,
                timeout=1200,
                check=False,
            )
            if result.returncode == 255:
                time.sleep(5)
                continue
            try:
                status = json.loads(result.stdout)
            except (ValueError, UnicodeError):
                return {"status": "invalid-report", "returncode": result.returncode, "ready": False}
            if not isinstance(status, dict):
                return {"status": "invalid-report", "returncode": result.returncode, "ready": False}
            ready = (
                result.returncode == 0
                and status.get("status") == "done"
                and not status.get("errors")
                and not status.get("recoverable_errors")
            )
            return {
                "status": status.get("status"),
                "returncode": result.returncode,
                "ready": bool(ready),
                "has_errors": bool(status.get("errors")),
                "has_recoverable_errors": bool(status.get("recoverable_errors")),
            }
        raise CloudEnvError("guest SSH/cloud-init did not become ready before the deadline")

    # ── OpenStack resources ──────────────────────────────────────────────
    def _ensure_keypair(self) -> str:
        existing = self._confirmed("keypairs", "env")
        if existing:
            keypair = self._get(self.conn.compute.get_keypair, existing["id"])
            if (
                keypair is None
                or keypair.public_key != existing["public_key"]
                or keypair.user_id != existing["user_id"]
            ):
                raise CloudEnvError("fresh keypair public key/user identity differs; HOLD")
            return existing["id"]
        name = f"pal-{self.name}-{self.state['marker'][:12]}"
        if not self.key_path.exists():
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.key_path)], check=True
            )
        public = self.key_path.with_suffix(".pub").read_text().strip()
        pending = self._intent("keypairs", "env", name=name, public_key=public)
        keypair = self.conn.compute.create_keypair(name=name, public_key=public)
        fresh = self.conn.compute.get_keypair(name)
        if keypair.name != name or fresh.public_key != public or fresh.user_id != self.conn.current_user_id:
            raise CloudEnvError("created keypair identity was not confirmed; HOLD")
        self.record("keypairs", "env", {**pending, "id": name, "status": "confirmed", "user_id": fresh.user_id})
        return name

    def _ensure_security_group(self, cidrs: list[str], reuse: str | None) -> str:
        existing = self._confirmed("security_groups", "env")
        if existing:
            group = self.conn.network.get_security_group(existing["id"])
            self._project(group)
            if existing.get("owned") and group.description != existing["description"]:
                raise CloudEnvError("security group ownership marker differs; HOLD")
        elif reuse:
            # Reuse accepts an exact ID only: no ambiguous name adoption.
            group = self.conn.network.get_security_group(reuse)
            self._project(group)
            self.record(
                "security_groups", "env", {"id": group.id, "name": group.name, "owned": False, "status": "confirmed"}
            )
            return group.id
        else:
            if not cidrs:
                raise CloudEnvError("--ssh-cidr is required unless --security-group supplies an exact ID")
            description = f"palimpsest:{self.state['marker']}:security-group"
            pending = self._intent("security_groups", "env", description=description, owned=True)
            group = self.conn.network.create_security_group(name=f"pal-cloud-{self.name}", description=description)
            existing = self.record("security_groups", "env", {**pending, "id": group.id, "status": "confirmed"})
            group = self.conn.network.get_security_group(existing["id"])
            self._project(group)
            if group.description != description:
                raise CloudEnvError("new security group marker was not confirmed; HOLD")
        if not existing.get("owned"):
            return group.id
        for index, cidr in enumerate(cidrs):
            role = f"ssh-{index}"
            rule = self._confirmed("security_group_rules", role)
            if rule:
                fresh = self.conn.network.get_security_group_rule(rule["id"])
                self._project(fresh)
                if fresh.security_group_id != group.id or fresh.remote_ip_prefix != cidr:
                    raise CloudEnvError("SSH rule association differs; HOLD")
                continue
            pending = self._intent("security_group_rules", role, group_id=group.id, cidr=cidr)
            created = self.conn.network.create_security_group_rule(
                security_group_id=group.id,
                direction="ingress",
                ethertype=f"IPv{ipaddress.ip_network(cidr).version}",
                protocol="tcp",
                port_range_min=22,
                port_range_max=22,
                remote_ip_prefix=cidr,
            )
            self.record("security_group_rules", role, {**pending, "id": created.id, "status": "confirmed"})
        return group.id

    def _ensure_share(self) -> dict:
        existing = self._confirmed("shares", "layers")
        sfs = self.conn.shared_file_system
        if existing is None:
            pending = self._intent("shares", "layers", size_gib=self.state["share_gib"])
            share = sfs.create_share(
                name=f"pal-cloud-{self.name}-layers",
                share_proto="CEPHFS",
                size=self.state["share_gib"],
                share_type=self.state["share_type"],
                description=f"palimpsest cloud env {self.name} layer store",
                metadata={"palimpsest_cloud_env": self.name, "palimpsest_env_id": self.state["marker"]},
            )
            existing = self.record("shares", "layers", {**pending, "id": share.id, "status": "confirmed"})
        deadline = time.monotonic() + 600
        while True:
            share = sfs.get_share(existing["id"])
            self._owned(share)
            if share.status == "available":
                break
            if share.status in {"error", "error_deleting", "deleting"} or time.monotonic() > deadline:
                raise CloudEnvError(f"share {existing['id']} entered {share.status}")
            time.sleep(3)
        locations = sorted(sfs.export_locations(existing["id"]), key=lambda item: not item.is_preferred)
        if not locations:
            raise CloudEnvError("share has no export location")
        export = locations[0].path
        self._validate_export(export)
        return self.record("shares", "layers", {**existing, "export": export})

    def grant(self, role: str, level: str) -> dict:
        share = self._confirmed("shares", "layers")
        self._owned(self.conn.shared_file_system.get_share(share["id"]))
        access_to = f"pal-{self.state['marker'][:12]}-{role}"
        existing = self._confirmed("access_rules", role)
        sfs = self.conn.shared_file_system
        if existing is None:
            pending = self._intent("access_rules", role, share_id=share["id"], access_to=access_to, level=level)
            rule = sfs.create_access_rule(share["id"], access_type="cephx", access_to=access_to, access_level=level)
            existing = self.record("access_rules", role, {**pending, "id": rule.id, "status": "confirmed"})
        deadline = time.monotonic() + 300
        while True:
            rules = {rule.id: rule for rule in sfs.access_rules(share["id"])}
            rule = rules.get(existing["id"])
            if rule is None:
                raise CloudEnvError("recorded access rule disappeared")
            self._check_rule(existing, rule)
            if rule.state == "active" and rule.access_key:
                if not CEPHX_KEY_RE.fullmatch(rule.access_key):
                    raise CloudEnvError("cephx access key has an unexpected shape")
                self._write_private(self._secret_path(access_to), rule.access_key)
                return existing
            if rule.state in {"error", "denying"} or time.monotonic() > deadline:
                raise CloudEnvError(f"access rule {existing['id']} entered {rule.state}")
            time.sleep(3)

    @staticmethod
    def _check_rule(record: dict, rule) -> None:
        if rule.access_to != record["access_to"] or rule.access_level != record["level"] or rule.access_type != "cephx":
            raise CloudEnvError("access rule identity differs from the recorded role; HOLD")

    def _server_boot_ids(self, role: str, server) -> list[str]:
        self._owned(server, role=role)
        ids = [item["id"] for item in server.attached_volumes or []]
        if len(ids) != 1 or not isinstance(ids[0], str):
            raise CloudEnvError("cannot confirm exactly one boot volume; HOLD before server deletion")
        for identity in ids:
            volume = self.conn.block_storage.get_volume(identity)
            self._project(volume)
            if not volume.attachments or any(item.get("server_id") != server.id for item in volume.attachments):
                raise CloudEnvError("boot volume has an unexpected attachment; HOLD")
            record = self.resource("servers", role)
            if (
                volume.size != record["boot_gib"]
                or (volume.volume_image_metadata or {}).get("image_id") != self.state["image_id"]
            ):
                raise CloudEnvError("boot volume image/size differs from the recorded request; HOLD")
        return ids

    def _create_server(self, role: str, *, flavor: str, boot_gib: int, packages: list[str]) -> dict:
        existing = self._confirmed("servers", role)
        compute = self.conn.compute
        if existing is None:
            flavor_id = compute.find_flavor(flavor, ignore_missing=False).id
            pending = self._intent("servers", role, flavor=flavor, boot_gib=boot_gib)
            server = compute.create_server(
                name=f"pal-cloud-{self.name}-{role}",
                flavor_id=flavor_id,
                image_id=self.state["image_id"],
                networks=[{"uuid": self.state["network_id"]}],
                security_groups=[{"name": self.resource("security_groups", "env")["id"]}],
                key_name=self.resource("keypairs", "env")["name"],
                block_device_mapping_v2=[
                    {
                        "boot_index": 0,
                        "uuid": self.state["image_id"],
                        "source_type": "image",
                        "destination_type": "volume",
                        "volume_size": boot_gib,
                        "delete_on_termination": True,
                    }
                ],
                metadata={
                    "palimpsest_cloud_env": self.name,
                    "palimpsest_env_id": self.state["marker"],
                    "palimpsest_role": role,
                },
                user_data=base64.b64encode(_cloud_config().encode()).decode(),
            )
            existing = self.record("servers", role, {**pending, "id": server.id, "status": "confirmed"})
        fresh = compute.get_server(existing["id"])
        self._owned(fresh, role=role)
        server = compute.wait_for_server(fresh, status="ACTIVE", wait=1200)
        boot_ids = self._server_boot_ids(role, compute.get_server(existing["id"]))
        existing = self.record(
            "servers", role, {**existing, "boot_volumes": boot_ids, "active_at": existing.get("active_at") or _now()}
        )
        ports = list(self.conn.network.ports(device_id=server.id))
        if len(ports) != 1:
            raise CloudEnvError("server does not have exactly one port")
        port = self.conn.network.get_port(ports[0].id)
        self._project(port)
        if port.device_id != server.id or port.network_id != self.state["network_id"]:
            raise CloudEnvError("server port identity differs; HOLD")
        ip = self._confirmed("floating_ips", role)
        if ip is None:
            description = f"palimpsest:{self.state['marker']}:{role}"
            pending = self._intent("floating_ips", role, description=description, port_id=port.id, server_id=server.id)
            created = self.conn.network.create_ip(
                floating_network_id=self.state["external_network_id"], description=description
            )
            ip = self.record(
                "floating_ips",
                role,
                {**pending, "id": created.id, "status": "confirmed", "address": created.floating_ip_address},
            )
        fresh_ip = self.conn.network.get_ip(ip["id"])
        self._check_ip(ip, fresh_ip)
        if ip["port_id"] != port.id or ip["server_id"] != server.id:
            raise CloudEnvError("recorded floating IP server association differs; HOLD")
        if fresh_ip.port_id is None:
            # Retry only the association of a confirmed allocation, never the POST.
            self.record("floating_ips", role, {**ip, "association_intent_at": _now()})
            self.conn.network.update_ip(ip["id"], port_id=port.id)
            fresh_ip = self.conn.network.get_ip(ip["id"])
            self._check_ip(ip, fresh_ip)
        if fresh_ip.port_id != port.id:
            raise CloudEnvError("floating IP was not associated with the exact server port")
        address = ip["address"]
        ipaddress.ip_address(address)
        deadline = time.monotonic() + 1200
        self._pin_host_key(server.id, address, deadline)
        readiness = self._wait_ssh(address, deadline)
        existing = self.record("servers", role, {**existing, "cloud_init": readiness})
        if not readiness.get("ready"):
            raise CloudEnvError(
                "cloud-init reported error/degraded/incomplete status; guest is not ready (status persisted)"
            )
        if not existing.get("provisioned_at"):
            self.ssh(
                address,
                "sudo bash -c " + shlex.quote(_provision_script(packages, self.state.get("dns", []))),
                timeout=1800,
            )
            existing = self.record("servers", role, {**existing, "provisioned_at": _now()})
        return self.record("servers", role, {**existing, "address": address, "ssh_ready_at": _now()})

    def _check_ip(self, record: dict, ip) -> None:
        self._project(ip)
        if (
            ip.description != record["description"]
            or ip.floating_network_id != self.state["external_network_id"]
            or ip.floating_ip_address != record["address"]
            or ip.port_id not in (None, record["port_id"])
        ):
            raise CloudEnvError("floating IP identity/association differs; HOLD")

    def _delete_ip(self, role: str) -> None:
        record = self._confirmed("floating_ips", role)
        if not record:
            return
        fresh = self._get(self.conn.network.get_ip, record["id"])
        if fresh is not None:
            self._check_ip(record, fresh)
            if fresh.port_id is not None:
                port = self.conn.network.get_port(fresh.port_id)
                self._project(port)
                if port.device_id != record["server_id"] or port.network_id != self.state["network_id"]:
                    raise CloudEnvError("floating IP port no longer belongs to the recorded server; HOLD")
            if record["status"] != "deleting":
                self._deleting("floating_ips", role)
                self.conn.network.delete_ip(record["id"], ignore_missing=False)
            self._wait_absent(self.conn.network.get_ip, record["id"])
        self.forget("floating_ips", role)

    def _delete_server(self, role: str) -> dict:
        record = self._confirmed("servers", role)
        result: dict[str, Any] = {}
        if record:
            fresh = self._get(self.conn.compute.get_server, record["id"])
            if fresh is not None:
                if record["status"] == "deleting" and record.get("boot_volumes"):
                    self._owned(fresh, role=role)
                    # Nova may clear mappings while deleting. The already-bound boot
                    # IDs remain authoritative cleanup obligations until their 404s.
                    ids = [item["id"] for item in fresh.attached_volumes or []]
                    if ids and ids != record["boot_volumes"]:
                        raise CloudEnvError("boot volume IDs changed; HOLD")
                else:
                    ids = self._server_boot_ids(role, fresh)
                    if record.get("boot_volumes") and record["boot_volumes"] != ids:
                        raise CloudEnvError("boot volume IDs changed; HOLD")
                    record = self.record("servers", role, {**record, "boot_volumes": ids})
                if record["status"] != "deleting":
                    self._deleting("servers", role)
                    self.conn.compute.delete_server(record["id"], ignore_missing=False)
                self._wait_absent(self.conn.compute.get_server, record["id"])
            if not record.get("boot_volumes"):
                raise CloudEnvError("server absent but boot IDs were never confirmed; HOLD, cannot prove cleanup")
            # Keep the server record and persisted IDs through failures/restarts.
            for identity in record["boot_volumes"]:
                self._wait_absent(self.conn.block_storage.get_volume, identity, seconds=300)
            result.update(server="absent", boot_volumes_absent=record["boot_volumes"])
            self.forget("servers", role)
        self._delete_ip(role)
        return result

    def _revoke(self, role: str) -> None:
        record = self._confirmed("access_rules", role)
        if not record:
            return
        sfs = self.conn.shared_file_system
        share = self._get(sfs.get_share, record["share_id"])
        if share is not None:
            self._owned(share)
            rules = {rule.id: rule for rule in sfs.access_rules(record["share_id"])}
            if record["id"] in rules:
                self._check_rule(record, rules[record["id"]])
                if record["status"] != "deleting":
                    self._deleting("access_rules", role)
                    sfs.delete_access_rule(record["id"], record["share_id"], ignore_missing=False)
            deadline = time.monotonic() + 300
            while True:
                rules = {rule.id: rule for rule in sfs.access_rules(record["share_id"])}
                if record["id"] not in rules:
                    break
                self._check_rule(record, rules[record["id"]])
                if time.monotonic() >= deadline:
                    raise CloudEnvError("access rule is still present; revocation not confirmed")
                time.sleep(3)
        self._secret_path(record["access_to"]).unlink(missing_ok=True)
        self.forget("access_rules", role)

    # ── guest helpers ────────────────────────────────────────────────────
    @staticmethod
    def _validate_export(export: str) -> None:
        try:
            _ceph_source_identity(export)
        except (ValueError, TypeError) as error:
            raise CloudEnvError(
                "unsupported CephFS export; require validated literal monitor IPs and a safe share path: " + str(error)
            ) from None

    def _mount_script(self, role: str) -> str:
        rule = self.resource("access_rules", role)
        export = self.resource("shares", "layers")["export"]
        self._validate_export(export)
        name = rule["access_to"]
        if re.fullmatch(r"[a-z0-9-]{1,100}", name) is None:
            raise CloudEnvError("Ceph identity has an unsafe shape")
        secret = f"/etc/ceph/palimpsest-{name}.secret"
        check = (
            inspect.getsource(_ceph_source_identity)
            + "\n"
            + "import json,subprocess,sys; m=json.loads(subprocess.check_output(['findmnt','-J','-M',sys.argv[1],'-o','SOURCE,FSTYPE,OPTIONS']))['filesystems']; assert len(m)==1 and m[0]['fstype']=='ceph' and _ceph_source_identity(m[0]['source'])==_ceph_source_identity(sys.argv[2]) and [x for x in m[0]['options'].split(',') if x.startswith('name=')]==['name='+sys.argv[3]], 'Ceph export/identity differs or unsupported canonicalization; blocked'; print('ceph-export-identity-verified')"
        )
        return f"""set -euo pipefail
modprobe ceph
install -d -m 0700 /etc/ceph
umask 077; cat > {shlex.quote(secret)}
install -d -m 0755 {STORE_MOUNT}
if ! findmnt -n -M {STORE_MOUNT} >/dev/null; then
  mount -t ceph {shlex.quote(export)} {STORE_MOUNT} -o {shlex.quote("name=" + name + ",secretfile=" + secret)}
fi
python3 -c {shlex.quote(check)} {STORE_MOUNT} {shlex.quote(export)} {shlex.quote(name)}
"""

    def _send_mount(self, address: str, role: str) -> None:
        rule = self._confirmed("access_rules", role)
        share = self._confirmed("shares", "layers")
        self._owned(self.conn.shared_file_system.get_share(share["id"]))
        if share["export"] not in {item.path for item in self.conn.shared_file_system.export_locations(share["id"])}:
            raise CloudEnvError("recorded Ceph export is no longer present; HOLD")
        rules = {item.id: item for item in self.conn.shared_file_system.access_rules(share["id"])}
        if rule["id"] not in rules:
            raise CloudEnvError("Ceph access rule is no longer present; HOLD")
        self._check_rule(rule, rules[rule["id"]])
        if rules[rule["id"]].state != "active":
            raise CloudEnvError("Ceph access rule is not active")
        key = self._secret_path(rule["access_to"]).read_bytes()
        result = self.ssh(address, "sudo bash -c " + shlex.quote(self._mount_script(role)), input=key, timeout=900)
        lines = result.stdout.decode().strip().splitlines()
        if not lines or lines[-1] != "ceph-export-identity-verified":
            raise CloudEnvError(
                "store export/identity was not verified (this does not attest publication immutability)"
            )

    # ── public operations ────────────────────────────────────────────────
    @_operation
    def native_prepare(self, **kwargs) -> dict:
        from .nova_native_cloud import NativeCloud

        return NativeCloud(self).prepare(**kwargs)

    @_operation
    def native_run(self, **kwargs) -> dict:
        from .nova_native_cloud import NativeCloud

        return NativeCloud(self).run(**kwargs)

    @_operation
    def native_status(self, **kwargs) -> dict:
        from .nova_native_cloud import NativeCloud

        return NativeCloud(self).status(**kwargs)

    @_operation
    def native_cleanup(self, **kwargs) -> dict:
        from .nova_native_cloud import NativeCloud

        return NativeCloud(self).cleanup(**kwargs)

    @_operation
    def up(
        self,
        *,
        cloud: str,
        network: str,
        external_network: str,
        image: str,
        worker_flavor: str,
        share_gib: int,
        share_type: str,
        ssh_cidrs: list[str],
        security_group: str | None,
        worker_boot_gib: int,
        dns: list[str],
        buildkit_archive: Path,
    ) -> dict:
        recorded_source = self.state.get("worker_package_sha256")
        if recorded_source is not None and recorded_source != package_archive()[1]:
            raise CloudEnvError(
                "env up cannot replace recorded worker source; use an explicitly approved preserving upgrade"
            )
        _validate_network_inputs(dns, ssh_cidrs)
        if type(share_gib) is not int or share_gib <= 0 or type(worker_boot_gib) is not int or worker_boot_gib <= 0:
            raise CloudEnvError("share/boot sizes must be positive integers")
        if not ssh_cidrs and not security_group:
            raise CloudEnvError("--ssh-cidr or an exact --security-group ID is required")
        if security_group:
            try:
                uuid.UUID(security_group)
            except ValueError:
                raise CloudEnvError("--security-group must be an exact UUID") from None
        settings = dict(
            cloud=cloud,
            network=network,
            external_network=external_network,
            image=image,
            worker_flavor=worker_flavor,
            share_gib=share_gib,
            share_type=share_type,
            ssh_cidrs=ssh_cidrs,
            security_group=security_group,
            worker_boot_gib=worker_boot_gib,
            dns=dns,
        )
        if self.state and self.state.get("settings") != settings:
            raise CloudEnvError(
                "environment settings are immutable; changed arguments or legacy unbound state require a new environment"
            )
        archive = buildkit_archive.read_bytes()
        if hashlib.sha256(archive).hexdigest() != BUILDKIT_SHA256:
            raise CloudEnvError("BuildKit archive digest differs from the pinned release")
        if self._connection is None:
            self._authenticate(cloud)
        conn = self.conn
        if not self.state:
            tenant_network = conn.network.find_network(network, ignore_missing=False)
            self._project_for(tenant_network, conn.current_project_id)
            external = conn.network.find_network(external_network, ignore_missing=False)
            if not external.is_router_external:
                raise CloudEnvError("floating IP network is not an external network")
            selected_image = conn.image.find_image(image, ignore_missing=False)
            if selected_image.status != "active":
                raise CloudEnvError("boot image must be active before resource creation")
            # Publish complete initialization atomically: failed lookups leave no partial environment.
            self.state = {
                "schema": SCHEMA,
                "name": self.name,
                "cloud": cloud,
                "marker": uuid.uuid4().hex,
                "project_id": conn.current_project_id,
                "user_id": conn.current_user_id,
                "share_gib": share_gib,
                "share_type": share_type,
                "dns": dns,
                "settings": settings,
                "network_id": tenant_network.id,
                "external_network_id": external.id,
                "image_id": selected_image.id,
                "created_at": _now(),
                "resources": {},
                "images": {},
            }
        elif conn.image.get_image(self.state["image_id"]).status != "active":
            raise CloudEnvError("boot image must remain active before resource creation")
        self.state["worker_ready"] = False
        self.save()
        timings: dict[str, float] = {}
        started = time.monotonic()
        self._ensure_keypair()
        self._ensure_security_group(ssh_cidrs, security_group)
        self._ensure_share()
        self.grant("worker", "rw")
        timings["share_and_access"] = round(time.monotonic() - started, 3)
        mark = time.monotonic()
        worker = self._create_server("worker", flavor=worker_flavor, boot_gib=worker_boot_gib, packages=WORKER_PACKAGES)
        timings["worker_ready"] = round(time.monotonic() - mark, 3)
        mark = time.monotonic()
        address = worker["address"]
        self.upload(address, archive, "/tmp/buildkit.tar.gz")
        self.ssh(
            address,
            "sudo bash -c "
            + shlex.quote(
                f"set -euo pipefail; echo '{BUILDKIT_SHA256}  /tmp/buildkit.tar.gz' | sha256sum -c - >/dev/null; "
                "tar -xzf /tmp/buildkit.tar.gz -C /usr/local bin/buildctl bin/buildkitd bin/buildkit-runc; "
                "rm /tmp/buildkit.tar.gz; buildctl --version"
            ),
        )
        self._sync_worker_package(address)
        self._send_mount(address, "worker")
        timings["worker_provision"] = round(time.monotonic() - mark, 3)
        self.state["worker_ready"] = True
        self.save()
        return {
            "environment": self.name,
            "worker": worker,
            "share": self.resource("shares", "layers"),
            "timings": timings,
        }

    @staticmethod
    def _project_for(resource, project_id: str) -> None:
        if resource.project_id != project_id:
            raise CloudEnvError("tenant network does not belong to the authenticated project")

    def _sync_worker_package(self, address: str) -> None:
        """Install palimpsest_local into the worker's system site-packages.

        The SquashFS materializer re-executes ``python -I -m ...`` (isolated mode ignores
        PYTHONPATH), so the package must be importable from site-packages itself.
        """
        data, digest = package_archive()
        if self.state.get("worker_package_sha256") == digest:
            return
        if self.state.get("worker_package_sha256") is not None:
            raise CloudEnvError("worker source replacement requires an explicitly approved preserving upgrade")
        self.upload(address, data, "/tmp/palimpsest-package.tar")
        self.ssh(
            address,
            "sudo bash -c "
            + shlex.quote(
                f"set -euo pipefail; echo '{digest}  /tmp/palimpsest-package.tar' | sha256sum -c - >/dev/null; "
                "site=$(python3 -I -c 'import site; print(site.getsitepackages()[0])'); "
                'test ! -e "$site/palimpsest_local" && test ! -L "$site/palimpsest_local"; '
                'test ! -e "$site/palimpsest_local.new" && test ! -L "$site/palimpsest_local.new"; '
                'install -d -m 0755 "$site/palimpsest_local.new"; '
                'tar -xf /tmp/palimpsest-package.tar -C "$site/palimpsest_local.new" --strip-components=1; '
                "rm /tmp/palimpsest-package.tar; "
                'mv "$site/palimpsest_local.new" "$site/palimpsest_local"; '
                "python3 -I -B -c 'import palimpsest_local.cloud_layer_worker'"
            ),
        )
        self.state["worker_package_sha256"] = digest
        self.save()

    def _send_source_policy(self, address: str, remote: str, source_policy: ImageSourcePolicy) -> None:
        # This is controller-selected authority, never a field supplied by the image request.
        self.ssh(
            address,
            f"sudo install -d -m 0700 -o {SSH_USER} {remote} && "
            f"sudo sh -c {shlex.quote('umask 077; cat > ' + remote + '/source-policy.json')}",
            input=json.dumps(source_policy.to_dict(), sort_keys=True).encode(),
        )

    def _policy_ssh(
        self,
        address: str,
        command: str,
        *,
        source_policy: ImageSourcePolicy,
        timeout: float,
    ) -> subprocess.CompletedProcess:
        # Same authenticated transport/key/known-hosts path; only its private
        # stdin/stdout carry fresh authorization. Never use the copied snapshot.
        return run_policy_process(
            [*self.ssh_argv(address), command],
            initial=source_policy,
            current_policy=lambda: load_image_source_policy(resolve_roots()),
            policy_timeout=timeout,
            process_timeout=timeout + 300,
        )

    @_operation
    def build(self, *, dockerfile: Path, context: Path, tag: str, timeout: float) -> dict:
        if not isinstance(tag, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", tag) is None:
            raise CloudEnvError("image name must be lowercase [a-z0-9._-], at most 128 characters")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout < 604800:
            raise CloudEnvError("build timeout must be finite and positive (less than one week)")
        roots = resolve_roots()
        source_policy = load_image_source_policy(roots)
        if not source_policy.rules:
            raise ImageSourcePolicyError("image_sources policy does not allow any upstream image")
        validate_online_dockerfile(
            BuildKitSpec(
                context=context,
                dockerfile=dockerfile,
                tag=tag,
                output=context.expanduser().resolve().parent / "cloud-image.oci.tar",
            ),
            roots,
            source_policy=source_policy,
            require_pins=False,
        )
        data = dockerfile.read_bytes()
        context_data = context_archive(context)
        _, digest = package_archive()
        if self.state.get("worker_package_sha256") != digest:
            raise CloudEnvError(
                "worker package differs; an explicitly approved preserving upgrade is required, build never upgrades it"
            )
        worker = self.resource("servers", "worker")
        if not self.state.get("worker_ready") or not worker:
            raise CloudEnvError("worker is not provisioned; run `palimpsest cloud env up` first")
        address = worker["address"]
        # Remount after a worker reboot; idempotent when already mounted.
        self._send_mount(address, "worker")
        job = uuid.uuid4().hex
        remote = f"{WORKER_SCRATCH}/uploads/{job}"
        started = time.monotonic()
        self._send_source_policy(address, remote, source_policy)
        self.upload(address, data, f"{remote}/Dockerfile")
        self.upload(address, context_data, f"{remote}/context.tar")
        uploaded = time.monotonic()
        command = (
            "sudo env PYTHONDONTWRITEBYTECODE=1 python3 -I -m "
            f"palimpsest_local.cloud_layer_worker build --dockerfile {remote}/Dockerfile "
            f"--context-tar {remote}/context.tar --store {STORE_MOUNT} --scratch {WORKER_SCRATCH} "
            f"--name {shlex.quote(tag)} --timeout {str(timeout)} --source-policy {remote}/source-policy.json "
            "--policy-channel"
        )
        result = self._policy_ssh(address, command, source_policy=source_policy, timeout=timeout)
        self.ssh(address, f"sudo rm -rf {remote}", check=False)
        if result.returncode != 0:
            log = self.ssh(
                address,
                f"sudo bash -c 'tail -n 60 $(ls -td {WORKER_SCRATCH}/jobs/*/ | head -1)buildctl.log'",
                check=False,
            )
            raise CloudEnvError(
                "worker build failed\n--- buildctl.log tail ---\n"
                + log.stdout.decode(errors="replace")[-3000:]
                + "\n--- worker error ---\n"
                + result.stderr.decode(errors="replace")[-1500:]
            )
        record = json.loads(result.stdout.decode().strip().splitlines()[-1])
        if (
            record.get("source_policy") != source_policy.to_dict()
            or record.get("source_policy_digest") != source_policy.digest
        ):
            raise CloudEnvError("worker build receipt differs from the configured source policy")
        _validate_image(record, require_root=False)
        record["timings_seconds"]["upload"] = round(uploaded - started, 3)
        record["timings_seconds"]["total"] = round(time.monotonic() - started, 3)
        self.state.setdefault("images", {})[tag] = {
            key: record[key]
            for key in ("name", "source", "layers", "runtime", "built_at_unix", "source_policy", "source_policy_digest")
        }
        self.save()
        return record

    @_operation
    def pull(self, *, reference: str, tag: str, timeout: float) -> dict:
        """Acquire on the worker only; an interrupted publication never auto-replays."""
        from .registry_intake import resolve_anonymous_reference

        if not isinstance(tag, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", tag) is None:
            raise CloudEnvError("image name must be lowercase [a-z0-9._-], at most 128 characters")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout < 604800:
            raise CloudEnvError("pull timeout must be finite and positive (less than one week)")
        resolved = resolve_anonymous_reference(reference)
        canonical = resolved.canonical
        pin = canonical.rsplit("@", 1)[-1]
        if "@" not in canonical or re.fullmatch(r"sha256:[0-9a-f]{64}", pin) is None:
            raise CloudEnvError("registry acquisition requires an explicit sha256 digest pin")
        source_policy = load_image_source_policy(resolve_roots())
        source_policy.admit(resolved)
        prior = self.state.get("image_intakes", {}).get(tag)
        if prior and prior.get("status") != "success":
            raise CloudEnvError("prior image intake outcome requires reconciliation; HOLD, no replay")
        if prior and prior["reference"] != canonical:
            raise CloudEnvError("image tag is already bound to another immutable reference")
        worker = self._confirmed("servers", "worker")
        if not self.state.get("worker_ready") or not worker:
            raise CloudEnvError("worker is not provisioned")
        _, digest = package_archive()
        if self.state.get("worker_package_sha256") != digest:
            raise CloudEnvError(
                "worker package differs; an explicitly approved preserving upgrade is required, pull never upgrades it"
            )
        self._owned(self.conn.compute.get_server(worker["id"]), role="worker")
        address = worker["address"]
        started = time.monotonic()
        intake = {
            "id": uuid.uuid4().hex,
            "reference": canonical,
            "tag": tag,
            "status": "running",
            "started_at": _now(),
            "worker_id": worker["id"],
            "worker_package_sha256": digest,
            "source_policy": source_policy.to_dict(),
            "source_policy_digest": source_policy.digest,
        }
        if prior:
            self.state.setdefault("image_intake_history", []).append(dict(prior))
        self.state.setdefault("image_intakes", {})[tag] = intake
        self.save()
        try:
            self._send_mount(address, "worker")
            remote = f"{WORKER_SCRATCH}/uploads/{intake['id']}"
            self._send_source_policy(address, remote, source_policy)
            argv = [
                "sudo",
                "env",
                "PYTHONDONTWRITEBYTECODE=1",
                "python3",
                "-I",
                "-m",
                "palimpsest_local.cloud_layer_worker",
                "pull",
                "--reference",
                canonical,
                "--store",
                STORE_MOUNT,
                "--scratch",
                WORKER_SCRATCH,
                "--name",
                tag,
                "--timeout",
                str(timeout),
                "--job-id",
                intake["id"],
                "--source-policy",
                remote + "/source-policy.json",
                "--policy-channel",
            ]
            response = self._policy_ssh(
                address,
                shlex.join(argv),
                source_policy=source_policy,
                timeout=timeout,
            )
            if response.returncode != 0:
                intake["worker_returncode"] = response.returncode
                raise CloudEnvError("worker registry acquisition failed; preserve its journal, no automatic replay")
            record = json.loads(response.stdout.decode().strip().splitlines()[-1])
            if (
                record.get("status") != "success"
                or record.get("name") != tag
                or record.get("source", {}).get("root_digest") != pin
                or record.get("source", {}).get("reference") != canonical
                or record.get("job_id") != intake["id"]
                or record.get("source_policy") != source_policy.to_dict()
                or record.get("source_policy_digest") != source_policy.digest
            ):
                raise CloudEnvError("worker image receipt differs from requested job/tag/reference/digest")
            _validate_registry_image(record)
            record.setdefault("timings_seconds", {})["total"] = round(time.monotonic() - started, 3)
            # Retain full source/per-layer provenance, not only the final blob list.
            self.state.setdefault("images", {})[tag] = record
            intake.update(status="success", completed_at=_now(), result=record)
            self.save()
            return record
        except BaseException as error:
            intake.update(status="held", error_type=type(error).__name__, failed_at=_now())
            self.save()
            raise

    @_operation
    def run(self, *, tag: str, command: list[str], flavor: str, boot_gib: int, keep: bool, verify_blobs: bool) -> dict:
        if not self.state or not self.state.get("project_id") or not self.state.get("marker"):
            raise CloudEnvError(
                "environment is not initialized with project-bound ownership; run env up with a new name"
            )
        role = "consumer-" + uuid.uuid4().hex[:8]
        report: dict[str, Any] = {
            "role": role,
            "image": tag,
            "command": command,
            "status": "running",
            "exit_code": 1,
            "timings": {},
        }
        started = time.monotonic()
        self.state.setdefault("runs", {})[role] = report
        self.save()
        attempted = False
        try:
            image = self.state.get("images", {}).get(tag)
            if image is None:
                raise CloudEnvError(f"no built image {tag!r} in environment {self.name}")
            _validate_image(image)
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
                or type(boot_gib) is not int
                or boot_gib <= 0
                or not flavor
            ):
                raise CloudEnvError("consumer command/flavor/boot size is invalid")
            report["layers"] = image["layers"]
            attempted = True
            self.grant(role, "ro")
            report["timings"]["ro_access"] = round(time.monotonic() - started, 3)
            mark = time.monotonic()
            server = self._create_server(role, flavor=flavor, boot_gib=boot_gib, packages=CONSUMER_PACKAGES)
            report["server"] = {key: server[key] for key in ("id", "flavor", "address")}
            report["timings"]["consumer_ready"] = round(time.monotonic() - mark, 3)
            mark = time.monotonic()
            address = server["address"]
            self._send_mount(address, role)
            report["timings"]["store_mount"] = round(time.monotonic() - mark, 3)
            mark = time.monotonic()
            config = {
                "layers": image["layers"],
                "runtime": image["runtime"],
                "command": command,
                "verify": verify_blobs,
            }
            result = self.ssh(
                address,
                "sudo python3 -c " + shlex.quote(CONSUMER_SCRIPT),
                input=json.dumps(config).encode(),
                timeout=1800,
                check=False,
            )
            report["timings"]["assemble_and_run"] = round(time.monotonic() - mark, 3)
            report["guest_process"] = _retain_output(
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout.decode(errors="replace"),
                    "stderr": result.stderr.decode(errors="replace"),
                }
            )
            if result.returncode != 0:
                report["guest_process_returncode"] = result.returncode
                raise CloudEnvError("consumer assembly process failed; guest did not produce a successful report")
            report["guest"] = json.loads(result.stdout.decode().strip().splitlines()[-1])
            if isinstance(report["guest"].get("command"), dict):
                report["guest"]["command"] = _retain_output(report["guest"]["command"])
            code = report["guest"]["command"]["returncode"]
            if type(code) is not int:
                raise CloudEnvError("consumer report has no command return code")
            report["exit_code"] = 0 if code == 0 else 1
            report["status"] = "success" if code == 0 else "command_failed"
        except Exception as error:  # noqa: BLE001 - preserve the complete per-run record, including cleanup
            report["status"] = "failed"
            report["error"] = str(error) if isinstance(error, CloudEnvError) else type(error).__name__
        except BaseException:
            report["status"] = "interrupted"
            raise
        finally:
            if attempted and not keep:
                mark = time.monotonic()
                uncertain = any(
                    self.resource(kind, role) and self.resource(kind, role).get("status") == "creating"
                    for kind in ("servers", "access_rules", "floating_ips")
                )
                if uncertain:
                    report["cleanup_error"] = "HOLD: creation outcome unknown; no automatic cleanup or replay"
                    report["exit_code"] = 1
                else:
                    try:
                        report["cleanup"] = self._delete_server(role)
                        self._revoke(role)
                        report["cleanup"]["ro_access"] = "revoked"
                    except Exception as error:  # noqa: BLE001 - records remain for explicit env down
                        report["cleanup_error"] = (
                            str(error) if isinstance(error, CloudEnvError) else type(error).__name__
                        )
                        report["exit_code"] = 1
                        report["status"] = "cleanup_failed"
                report["timings"]["cleanup"] = round(time.monotonic() - mark, 3)
            report["timings"]["total"] = round(time.monotonic() - started, 3)
            self.save()
        return report

    @_operation
    def status(self) -> dict:
        return {
            key: self.state.get(key)
            for key in ("name", "cloud", "project_id", "resources", "images", "runs", "history")
        }

    @_operation
    def down(self) -> dict:
        if not self.state:
            raise CloudEnvError(f"environment {self.name} has no state")
        native = self.state.get("native", {})
        if (
            native.get("preparation", {}).get("service_started")
            or native.get("preparation", {}).get("service_start_intent")
            or any(records for kind, records in self.state.get("resources", {}).items() if kind.startswith("native_"))
            or any(run.get("status") != "cleaned" for run in native.get("runs", {}).values())
        ):
            raise CloudEnvError("native consumers/bootstrap service remain; use cloud native cleanup before env down")
        # An uncertain POST is never resolved by lookup/adoption or automatic teardown.
        for kind, records in self.state.get("resources", {}).items():
            for role in records:
                self._confirmed(kind, role)
        result: dict[str, Any] = {}
        for role in list(self.state.get("resources", {}).get("servers", {})):
            result[f"server:{role}"] = self._delete_server(role)
        for role in list(self.state.get("resources", {}).get("floating_ips", {})):
            self._delete_ip(role)
        for role in list(self.state.get("resources", {}).get("access_rules", {})):
            self._revoke(role)
            result[f"access:{role}"] = "revoked"
        share = self._confirmed("shares", "layers")
        if share:
            sfs = self.conn.shared_file_system
            fresh = self._get(sfs.get_share, share["id"])
            if fresh is not None:
                self._owned(fresh)
                if share["status"] != "deleting":
                    self._deleting("shares", "layers")
                    sfs.delete_share(share["id"], ignore_missing=False)
                self._wait_absent(sfs.get_share, share["id"])
            self.state.setdefault("retired_images", []).append(self.state.pop("images", {}))
            self.state["images"] = {}
            # One atomic journal save retires runnable tags and the deleted share together.
            self.forget("shares", "layers")
            result["share"] = "absent"
        group = self._confirmed("security_groups", "env")
        if group:
            if group.get("owned"):
                fresh = self._get(self.conn.network.get_security_group, group["id"])
                if fresh is not None:
                    self._project(fresh)
                    if fresh.description != group["description"]:
                        raise CloudEnvError("security group marker differs; HOLD")
                    if group["status"] != "deleting":
                        if any(group["id"] in (port.security_group_ids or []) for port in self.conn.network.ports()):
                            raise CloudEnvError("security group still has attached ports; HOLD")
                        for role, rule in list(self.state.get("resources", {}).get("security_group_rules", {}).items()):
                            current = self._get(self.conn.network.get_security_group_rule, rule["id"])
                            if current is not None:
                                self._project(current)
                                if (
                                    rule["group_id"] != group["id"]
                                    or current.security_group_id != group["id"]
                                    or current.remote_ip_prefix != rule["cidr"]
                                ):
                                    raise CloudEnvError("security group rule association differs; HOLD")
                                if rule["status"] != "deleting":
                                    self._deleting("security_group_rules", role)
                                    self.conn.network.delete_security_group_rule(rule["id"], ignore_missing=False)
                                self._wait_absent(self.conn.network.get_security_group_rule, rule["id"])
                            self.forget("security_group_rules", role)
                        self._deleting("security_groups", "env")
                        self.conn.network.delete_security_group(group["id"], ignore_missing=False)
                    self._wait_absent(self.conn.network.get_security_group, group["id"])
                # Once group deletion has started, never revisit rule DELETEs.
                # Even a parent 404 does not replace exact child absence evidence.
                for role in list(self.state.get("resources", {}).get("security_group_rules", {})):
                    rule = self.resource("security_group_rules", role)
                    current = self._get(self.conn.network.get_security_group_rule, rule["id"])
                    if current is not None:
                        self._project(current)
                        if (
                            rule["group_id"] != group["id"]
                            or current.security_group_id != group["id"]
                            or current.remote_ip_prefix != rule["cidr"]
                        ):
                            raise CloudEnvError("security group rule association differs; HOLD")
                        self._wait_absent(self.conn.network.get_security_group_rule, rule["id"])
                    self.forget("security_group_rules", role)
                result["security_group"] = "absent"
            self.forget("security_groups", "env", absent=bool(group.get("owned")))
        keypair = self._confirmed("keypairs", "env")
        if keypair:
            fresh = self._get(self.conn.compute.get_keypair, keypair["id"])
            if fresh is not None:
                if fresh.public_key != keypair["public_key"] or fresh.user_id != keypair["user_id"]:
                    raise CloudEnvError("keypair public key/user differs; HOLD")
                if keypair["status"] != "deleting":
                    self._deleting("keypairs", "env")
                    self.conn.compute.delete_keypair(keypair["id"], ignore_missing=False)
                self._wait_absent(self.conn.compute.get_keypair, keypair["id"])
            self.forget("keypairs", "env")
            result["keypair"] = "absent"
        self.state["worker_ready"] = False
        self.state.pop("worker_package_sha256", None)
        self.save()
        return result


# Runs as root inside the consumer VM with stock python3. Reads JSON config on
# stdin; prints one JSON line. Contains no credentials and no Palimpsest code.
CONSUMER_SCRIPT = r"""
import errno, hashlib, json, os, subprocess, sys, time
cfg = json.load(sys.stdin)
STORE = "/mnt/palimpsest-layers"
out = {"layers": [], "timings": {}}
def run(*argv, **kw):
    return subprocess.run(list(argv), check=True, capture_output=True, text=True, **kw).stdout.strip()
probe = os.path.join(STORE, ".palimpsest-ro-probe-%d" % os.getpid())
try:
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except OSError as error:
    if error.errno not in (errno.EACCES, errno.EROFS):
        sys.exit("read-only probe failed unexpectedly errno=%d" % error.errno)
    out["store_write"] = "denied errno=%d" % error.errno
else:
    os.close(fd)
    os.unlink(probe)
    out["store_write"] = "allowed"
    print(json.dumps(out)); sys.exit(3)
t = time.monotonic()
lowers = []
for ordinal, layer in enumerate(cfg["layers"]):
    path = os.path.join(STORE, "blobs", "sha256", layer["digest"].split(":", 1)[1])
    size = os.stat(path).st_size
    if size != layer["size"]:
        sys.exit("layer %d size differs" % ordinal)
    if cfg["verify"]:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(4 << 20), b""):
                digest.update(chunk)
        if "sha256:" + digest.hexdigest() != layer["digest"]:
            sys.exit("layer %d digest differs" % ordinal)
    target = "/run/palimpsest/lower/%d" % ordinal
    os.makedirs(target, exist_ok=True)
    if subprocess.run(["mountpoint", "-q", target]).returncode != 0:
        loop = run("losetup", "--find", "--show", "--read-only", path)
        run("mount", "-t", "squashfs", "-o", "ro,nodev", loop, target)
    out["layers"].append({"ordinal": ordinal, "digest": layer["digest"], "size": size, "mount": target})
    lowers.append(target)
out["timings"]["layer_mounts"] = round(time.monotonic() - t, 3)
merged = "/opt/layers/merged"
for directory in ("/var/lib/palimpsest-run/upper", "/var/lib/palimpsest-run/work", merged):
    os.makedirs(directory, exist_ok=True)
if subprocess.run(["mountpoint", "-q", merged]).returncode != 0:
    run("mount", "-t", "overlay", "overlay", "-o",
        "lowerdir=%s,upperdir=/var/lib/palimpsest-run/upper,workdir=/var/lib/palimpsest-run/work" % ":".join(reversed(lowers)),
        merged)
    for source in ("/proc", "/sys", "/dev"):
        os.makedirs(merged + source, exist_ok=True)
        run("mount", "--rbind", source, merged + source)
    os.makedirs(merged + "/etc", exist_ok=True)
    with open("/etc/resolv.conf", "rb") as src, open(merged + "/etc/resolv.conf", "wb") as dst:
        dst.write(src.read())
# Never emit raw Ceph mount options: some kernels expose authentication options.
out["lower_mount_options"] = ",".join(x for x in run("findmnt", "-n", "-o", "OPTIONS", lowers[0]).split(",") if x in ("ro", "rw", "nodev", "nosuid", "noexec"))
out["store_mount_options"] = ",".join(x for x in run("findmnt", "-n", "-o", "OPTIONS", STORE).split(",") if x in ("ro", "rw", "nodev", "nosuid", "noexec"))
rt = cfg["runtime"]
env = [item for item in rt.get("env") or [] if "=" in item] or ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
workdir = rt.get("working_dir") or "/"
t = time.monotonic()
try:
    proc = subprocess.run(["chroot", merged, "/usr/bin/env", "-i", *env, "/bin/sh", "-c",
                           'cd -- "$1" && shift && exec "$@"', "palimpsest-run", workdir, *cfg["command"]],
                          capture_output=True, text=True, errors="replace", timeout=1500)
except subprocess.TimeoutExpired as error:
    def text(value):
        return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
    proc = subprocess.CompletedProcess(cfg["command"], 124, text(error.stdout), text(error.stderr))
    out["command_timeout"] = True
out["timings"]["command"] = round(time.monotonic() - t, 3)
out["command"] = {"argv": cfg["command"], "returncode": proc.returncode,
                  "stdout": proc.stdout, "stderr": proc.stderr}
print(json.dumps(out))
"""


def fetch_buildkit(cache: Path) -> Path:
    """Download the pinned BuildKit release once into a private local cache."""
    target = cache / Path(BUILDKIT_URL).name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == BUILDKIT_SHA256:
        return target
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    import urllib.request

    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
        with urllib.request.urlopen(BUILDKIT_URL, timeout=120) as response:  # noqa: S310 - fixed https URL
            for chunk in iter(lambda: response.read(1 << 20), b""):
                handle.write(chunk)
    data = Path(handle.name).read_bytes()
    if hashlib.sha256(data).hexdigest() != BUILDKIT_SHA256:
        Path(handle.name).unlink()
        raise CloudEnvError("downloaded BuildKit archive digest differs from the pinned release")
    os.replace(handle.name, target)
    return target


def add_parser(operations) -> None:
    env = operations.add_parser("env", help="manage a local-driven Nova worker + Manila CephFS layer store")
    env_ops = env.add_subparsers(dest="env_operation", required=True)
    up = env_ops.add_parser("up", help="create the CephFS share and provision the build worker VM")
    up.add_argument("name", help="environment name ([a-z0-9-], at most 31 characters)")
    up.add_argument("--cloud", required=True, help="clouds.yaml cloud name")
    up.add_argument("--network", required=True, help="tenant network name or ID for the VMs")
    up.add_argument("--external-network", required=True, help="network providing floating IPs")
    up.add_argument("--image", required=True, help="Ubuntu 24.04 cloud image name or ID")
    up.add_argument("--worker-flavor", required=True, help="Nova flavor for the build worker")
    up.add_argument("--worker-boot-gib", type=int, default=80, help="worker boot volume size in GiB")
    up.add_argument("--share-gib", type=int, default=50, help="Manila CephFS share size in GiB")
    up.add_argument("--share-type", default="cephfs", help="Manila share type with the CEPHFS protocol")
    up.add_argument("--ssh-cidr", action="append", default=[], help="allowed SSH source CIDR for an owned group")
    up.add_argument("--security-group", help="reuse an existing project security group (not modified or deleted)")
    up.add_argument("--dns", action="append", default=[], help="guest DNS server override (repeatable)")
    for operation in ("status", "down"):
        env_ops.add_parser(operation).add_argument("name", help="environment name")
    build = operations.add_parser("dockerfile-build", help="build a Dockerfile on the env worker into CephFS layers")
    build.add_argument("name", help="environment name")
    build.add_argument("context", type=Path, help="build context directory")
    build.add_argument("-f", "--file", type=Path, help="Dockerfile (default CONTEXT/Dockerfile)")
    build.add_argument("-t", "--tag", required=True, help="image name recorded in the share and environment")
    build.add_argument("--timeout", type=float, default=3600, help="worker build deadline in seconds")
    pull = operations.add_parser(
        "image-pull", help="pull a digest-pinned OCI image on the worker into sequential CephFS layers"
    )
    pull.add_argument("name", help="environment name")
    pull.add_argument("reference", help="fully qualified registry/repository@sha256:digest")
    pull.add_argument("-t", "--tag", required=True, help="immutable image name in the share")
    pull.add_argument(
        "--timeout", type=float, default=3600, help="worker acquisition and conversion deadline in seconds"
    )
    run = operations.add_parser("layer-run", help="boot a consumer VM on read-only CephFS layers and run a command")
    run.add_argument("name", help="environment name")
    run.add_argument("tag", help="image name from a previous dockerfile-build")
    run.add_argument("--flavor", required=True, help="Nova flavor for the consumer VM")
    run.add_argument("--boot-gib", type=int, default=20, help="consumer boot volume size in GiB")
    run.add_argument("--keep", action="store_true", help="retain the consumer VM and its read-only access")
    run.add_argument("--no-verify", action="store_true", help="skip in-guest SHA-256 verification of each layer")
    run.add_argument("command", nargs="+", help="command run inside the assembled layer root")


def dispatch(args) -> dict:
    operation = args.cloud_operation
    if operation == "env":
        environment = CloudEnv(args.name)
        if args.env_operation == "up":
            return environment.up(
                cloud=args.cloud,
                network=args.network,
                external_network=args.external_network,
                image=args.image,
                worker_flavor=args.worker_flavor,
                share_gib=args.share_gib,
                share_type=args.share_type,
                ssh_cidrs=args.ssh_cidr,
                security_group=args.security_group,
                dns=args.dns,
                worker_boot_gib=args.worker_boot_gib,
                buildkit_archive=fetch_buildkit(resolve_roots().state / "cache" / "buildkit"),
            )
        if args.env_operation == "status":
            return environment.status()
        return environment.down()
    environment = CloudEnv(args.name)
    if operation == "image-pull":
        return environment.pull(reference=args.reference, tag=args.tag, timeout=args.timeout)
    if operation == "dockerfile-build":
        dockerfile = args.file or args.context / "Dockerfile"
        return environment.build(dockerfile=dockerfile, context=args.context, tag=args.tag, timeout=args.timeout)
    return environment.run(
        tag=args.tag,
        command=args.command,
        flavor=args.flavor,
        boot_gib=args.boot_gib,
        keep=args.keep,
        verify_blobs=not args.no_verify,
    )
