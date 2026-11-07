"""Experimental trusted-workload Nova PID1, NOT the protected local stage-1.

The carrier supplies Python, mount helpers and a hash-pinned driver inventory.
The initramfs (not an Ubuntu guest) obtains a per-instance provider-read-only
Ceph credential over enrolled mTLS, assembles the original image's ordered
SquashFS layers, and moves that overlay to the actual root before executing the
original entrypoint. This is a functional proof, not hostile-tenant attestation.
No cloud APIs, driver installation, GPU rebinding or host driver loading occurs.
Wiping temporary key files removes direct pathname exposure, not confidentiality
against a uid0 workload: raw guest disks and process memory remain accessible.
The observation must not be presented as credential isolation or attestation.

Carrier builder API: ``dhcp_script()``, ``init_script()``,
``plan_policy_digest(plan)`` and ``validate_artifacts_manifest(manifest)``.
All imports are stdlib so this file can also be copied to /bootstrap and run
standalone; the package entrypoint is ``python3 -m palimpsest_local.nova_ceph_boot``.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import http.client
import ipaddress
import json
import os
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

ARTIFACT_SCHEMA = "palimpsest.nova-boot-artifacts.v1"
BOOTSTRAP_SCHEMA = "palimpsest.nova-bootstrap.v1"
RESULT_SCHEMA = "palimpsest.nova-ceph-boot-result.v1"
MAX_JSON = 1024 * 1024
MS_RDONLY, MS_NOSUID, MS_NODEV = 1, 2, 4
MS_NOEXEC = 8
INTERNAL = "/.palimpsest-internal"
MOVED_MOUNTS = ("/dev", "/sys", "/proc", "/run")
_RETAINED_PROCESSES = []
_RETAINED_READERS = []
MS_REMOUNT, MS_BIND, MS_MOVE, MS_PRIVATE, MS_REC = 32, 4096, 8192, 1 << 18, 16384
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
REQUIRED_EXECUTABLES = (
    "/bin/busybox",
    "/sbin/blkid",
    "/sbin/losetup",
    "/sbin/mount.ceph",
    "/sbin/modprobe",
    "/sbin/modinfo",
    "/usr/bin/ssh",
    "/usr/bin/python3",
    "/bootstrap/nova_ceph_boot.py",
)


class BootError(RuntimeError):
    """Only fixed, non-secret diagnostic codes may reach the serial console."""

    code = "native_boot_failed"


class HoldError(BootError):
    """Unsettled owned work is retained; no forced termination or success."""


class BootstrapPending(BootError):
    """Authenticated service has not published this enrollment yet."""

    code = "bootstrap_pending"

    def __init__(self, status: int):
        super().__init__("bootstrap_pending")
        self.status = status


def fail(code: str) -> None:
    error = BootError(code)
    error.code = code
    raise error


def hold(code: str) -> None:
    error = HoldError(code)
    error.code = code
    raise error


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def strict_json(data: bytes, *, maximum: int = MAX_JSON) -> dict:
    if len(data) > maximum:
        fail("json_size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("json_duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: fail("json_constant"))
    except (ValueError, UnicodeError):
        fail("json_invalid")
    if not isinstance(value, dict):
        fail("json_object")
    return value


def _text(value, code: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        fail(code)
    return value


def _digest(value, code="digest") -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or not _HEX.fullmatch(value[7:]):
        fail(code)
    return value


def _uuid(value) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            fail("instance_uuid")
    except (ValueError, TypeError, AttributeError):
        fail("instance_uuid")
    return value


def _path(value, *, bootstrap=False) -> str:
    value = _text(value, "path")
    p = PurePosixPath(value)
    if not value.startswith("/") or str(p) != value or ".." in p.parts or any(c in value for c in "\n\r,:\\"):
        fail("path")
    if bootstrap and (p.parent != PurePosixPath("/bootstrap") or len(p.parts) != 3):
        fail("bootstrap_path")
    return value


def validate_seed(seed: dict, *, now: float | None = None) -> dict:
    required = {
        "run_id",
        "nonce",
        "worker_address",
        "worker_known_hosts",
        "ssh_private_key",
        "client_cert",
        "client_key",
        "ca_file",
        "tls_server_name",
        "bootstrap_url",
        "deadline",
        "artifacts_manifest",
        "expected_plan_digest",
        "carrier_digest",
        "project_id",
        "expected_image_digest",
    }
    if not isinstance(seed, dict) or set(seed) != required:
        fail("seed_fields")
    if not isinstance(seed["run_id"], str) or not _RUN.fullmatch(seed["run_id"]):
        fail("run_id")
    if not isinstance(seed["nonce"], str) or not _NONCE.fullmatch(seed["nonce"]):
        fail("nonce")
    try:
        ipaddress.IPv4Address(seed["worker_address"])
    except (ValueError, TypeError):
        fail("worker_address")
    for field in (
        "worker_known_hosts",
        "ssh_private_key",
        "client_cert",
        "client_key",
        "ca_file",
        "artifacts_manifest",
    ):
        _path(seed[field], bootstrap=True)
    if not _SAFE_NAME.fullmatch(_text(seed["tls_server_name"], "tls_name", 253)):
        fail("tls_name")
    if seed["bootstrap_url"] != "https://127.0.0.1:9443":
        fail("bootstrap_url")
    if type(seed["deadline"]) is not int or not (now if now is not None else time.time()) < seed["deadline"]:
        fail("deadline")
    _digest(seed["expected_plan_digest"])
    _digest(seed["carrier_digest"])
    _digest(seed["expected_image_digest"])
    if not isinstance(seed["project_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", seed["project_id"]):
        fail("seed_project")
    return seed


def plan_policy_digest(plan: dict) -> str:
    """Bind immutable policy while allowing freshly enrolled UUID and DHCP IP."""
    if not isinstance(plan, dict) or not isinstance(plan.get("network"), dict):
        fail("plan_fields")
    stable = {key: value for key, value in plan.items() if key != "server_id"}
    stable["network"] = {key: value for key, value in plan["network"].items() if key != "address"}
    return "sha256:" + hashlib.sha256(canonical(stable)).hexdigest()


def runtime_vector(runtime: dict, command: list[str]) -> tuple[list[str], dict[str, str], str, str]:
    if not isinstance(runtime, dict) or set(runtime) - {"entrypoint", "cmd", "env", "working_dir", "user"}:
        fail("runtime_fields")
    if not {"entrypoint", "env", "working_dir", "user"} <= runtime.keys():
        fail("runtime_fields")
    original_cmd = [] if runtime.get("cmd") is None else runtime["cmd"]
    for vector in (runtime["entrypoint"], original_cmd, command):
        if not isinstance(vector, list) or any(not isinstance(s, str) or "\x00" in s for s in vector):
            fail("runtime_vector")
    # An explicit proof command replaces OCI Cmd, never Entrypoint. Cmd is used
    # only when it existed in the original config and no override was supplied.
    args = runtime["entrypoint"] + (command if command else original_cmd)
    if not args or not args[0]:
        fail("runtime_command")
    env = {}
    if not isinstance(runtime["env"], list):
        fail("runtime_env")
    for item in runtime["env"]:
        item = _text(item, "runtime_env", 65536)
        name, sep, value = item.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            fail("runtime_env")
        env[name] = value
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    cwd = runtime["working_dir"] or "/"
    _path(cwd)
    user = _text(runtime["user"], "runtime_user", 256)
    if "\n" in user or "\r" in user or user.count(":") > 1:
        fail("runtime_user")
    return args, env, cwd, user


def ordered_lower_paths(layers: list[dict]) -> list[str]:
    if not isinstance(layers, list) or not 1 <= len(layers) <= 128:
        fail("layers")
    result = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or not {"digest", "size", "ordinal"} <= layer.keys():
            fail("layer_fields")
        digest = _digest(layer["digest"])
        if type(layer["size"]) is not int or layer["size"] < 96 or layer["ordinal"] != index:
            fail("layer_order_size")
        if "path" in layer:
            fail("layer_path")
        result.append("blobs/sha256/" + digest[7:])
    return result


def overlay_lowers(mounts: list[str]) -> str:
    """OCI order is base→top; overlayfs lowerdir order is top→base."""
    if not mounts or len(set(mounts)) != len(mounts):
        fail("lower_mounts")
    return ":".join(_path(p) for p in reversed(mounts))


def validate_image_projection(image: dict, expected_digest: str) -> None:
    # Published local builds have no OCI descriptors or DiffIDs. Their exact
    # normalized manifest is the immutable authority instead of invented OCI
    # source documents; this module must remain standalone in the carrier.
    if isinstance(image.get("source"), dict) and image["source"].get("kind") == "published":
        source = image["source"]
        manifest = source.get("manifest")
        if (
            set(source) != {"kind", "manifest_digest", "manifest"}
            or not isinstance(manifest, dict)
            or manifest.get("schema") != "palimpsest.image-manifest.v1"
            or manifest.get("mode") != "oci-root"
            or manifest.get("platform") != "linux/amd64"
            or source["manifest_digest"] != _digest(expected_digest)
            or "sha256:"
            + hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            != expected_digest
            or manifest.get("runtime") != image.get("runtime")
            or not isinstance(manifest.get("source"), dict)
            or manifest["source"].get("kind") not in {"local", "registry"}
            or not all(isinstance(manifest.get(key), str) and manifest[key] for key in ("os_family", "os_version"))
        ):
            fail("published_manifest")
        layers = manifest.get("layers")
        if not isinstance(layers, list):
            fail("published_layers")
        projected = [
            {"digest": layer.get("digest"), "size": layer.get("size_bytes"), "ordinal": index}
            for index, layer in enumerate(layers)
            if isinstance(layer, dict)
        ]
        if image.get("layers") != projected or len(projected) != len(layers):
            fail("published_layers")
        ordered_lower_paths(projected)
        parent = None
        for layer in layers:
            if layer.get("parent_id") != parent:
                fail("published_parent")
            compatibility = {
                key: manifest.get(key) for key in ("mode", "platform", "os_family", "os_version", "base_image_digest")
            }
            identity = {
                "schema": "palimpsest.layer-node.v1",
                "parent_id": parent,
                "digest": layer["digest"],
                "size_bytes": layer["size_bytes"],
                **compatibility,
            }
            expected_id = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                ).hexdigest()
            )
            if layer.get("id") != expected_id:
                fail("published_layer_identity")
            parent = _digest(layer["id"])
        return
    source = image["source"]
    if (
        not isinstance(source.get("reference"), str)
        or "://" in source["reference"]
        or not source["reference"].endswith("@" + expected_digest)
    ):
        fail("plan_source")
    for descriptor, field in (
        ("root_descriptor", "root_digest"),
        ("manifest_descriptor", "manifest_digest"),
        ("config_descriptor", "config_digest"),
    ):
        value = source.get(descriptor)
        if (
            not isinstance(value, dict)
            or not {"digest", "size", "mediaType"} <= value.keys()
            or value["digest"] != _digest(source.get(field))
            or type(value["size"]) is not int
            or value["size"] <= 0
        ):
            fail("source_descriptor")
    if source["root_digest"] != expected_digest:
        fail("source_root_pin")
    manifest, config, root = source.get("manifest"), source.get("config"), source.get("root_document")
    if not isinstance(manifest, dict) or not isinstance(config, dict) or not isinstance(root, dict):
        fail("source_documents")
    if source["root_digest"] == source["manifest_digest"]:
        if root != manifest:
            fail("source_manifest")
    else:
        candidates = root.get("manifests")
        if not isinstance(candidates, list):
            fail("source_index")
        matches = [
            item
            for item in candidates
            if isinstance(item, dict)
            and {key: item.get(key) for key in ("mediaType", "digest", "size")} == source["manifest_descriptor"]
        ]
        if (
            len(matches) != 1
            or not isinstance(matches[0].get("platform"), dict)
            or {key: matches[0]["platform"].get(key) for key in ("os", "architecture")}
            != {"os": "linux", "architecture": "amd64"}
        ):
            fail("source_index")
    fields = ("mediaType", "digest", "size")
    if (
        not isinstance(manifest.get("config"), dict)
        or {field: manifest["config"].get(field) for field in fields} != source["config_descriptor"]
    ):
        fail("source_config_descriptor")
    rootfs = config.get("rootfs")
    originals = manifest.get("layers")
    layers = image["layers"]
    ordered_lower_paths(layers)
    if (
        config.get("architecture") != "amd64"
        or config.get("os") != "linux"
        or not isinstance(rootfs, dict)
        or rootfs.get("type") != "layers"
        or not isinstance(originals, list)
        or len(originals) != len(layers)
        or not isinstance(rootfs.get("diff_ids"), list)
        or len(rootfs["diff_ids"]) != len(layers)
    ):
        fail("source_platform_layers")
    for index, layer in enumerate(layers):
        if (
            not isinstance(originals[index], dict)
            or layer.get("compressed") != {field: originals[index].get(field) for field in fields}
            or _digest(layer.get("diff_id")) != rootfs["diff_ids"][index]
        ):
            fail("source_layer_projection")
    process = config.get("config") or {}
    if not isinstance(process, dict):
        fail("source_process")
    expected = {
        "entrypoint": process.get("Entrypoint") or [],
        "env": process.get("Env") or [],
        "working_dir": process.get("WorkingDir") or "/",
        "user": process.get("User") or "0",
    }
    if "Cmd" in process:
        expected["cmd"] = process["Cmd"]
    if image["runtime"] != expected:
        fail("source_runtime_projection")


def validate_plan(plan: dict, seed: dict, instance_id: str, address: str) -> dict:
    keys = {
        "run_id",
        "server_id",
        "project_id",
        "carrier_digest",
        "image",
        "network",
        "upper",
        "gpu",
        "command",
    }
    if not isinstance(plan, dict) or set(plan) not in (keys, keys | {"storage"}):
        fail("plan_fields")
    if "storage" in plan:
        storage = plan["storage"]
        if not isinstance(storage, dict) or set(storage) != {"export", "fs_name", "data_pool", "namespace"}:
            fail("plan_storage")
        _text(storage["export"], "plan_storage")
        for field, maximum in (("fs_name", 64), ("data_pool", 128), ("namespace", 128)):
            if (
                not isinstance(storage[field], str)
                or len(storage[field]) > maximum
                or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", storage[field]) is None
            ):
                fail("plan_storage")
    if plan["run_id"] != seed["run_id"] or _uuid(plan["server_id"]) != _uuid(instance_id):
        fail("plan_instance")
    if not isinstance(plan["project_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", plan["project_id"]):
        fail("plan_project")
    if plan["carrier_digest"] != seed["carrier_digest"] or plan_policy_digest(plan) != seed["expected_plan_digest"]:
        fail("plan_policy")
    if plan["project_id"] != seed["project_id"]:
        fail("plan_project")
    network = plan["network"]
    if not isinstance(network, dict) or set(network) != {"address", "cidr", "gateway", "dns"}:
        fail("plan_network")
    try:
        subnet = ipaddress.IPv4Network(network["cidr"], strict=False)
        if str(ipaddress.IPv4Address(network["address"])) != address or ipaddress.IPv4Address(address) not in subnet:
            fail("plan_address")
        if ipaddress.IPv4Address(network["gateway"]) not in subnet:
            fail("plan_gateway")
        if not isinstance(network["dns"], list) or not network["dns"]:
            fail("plan_dns")
        for dns in network["dns"]:
            ipaddress.IPv4Address(dns)
    except (ValueError, TypeError):
        fail("plan_network")
    image = plan["image"]
    if (
        not isinstance(image, dict)
        or set(image) != {"source", "runtime", "layers"}
        or not isinstance(image["source"], dict)
    ):
        fail("plan_image")
    validate_image_projection(image, seed["expected_image_digest"])
    if not isinstance(plan["command"], list) or not plan["command"]:
        fail("proof_command")
    runtime_vector(image["runtime"], plan["command"])
    if plan["upper"] != {"label": "PAL-UPPER"}:
        fail("plan_upper")
    gpu = plan["gpu"]
    if (
        not isinstance(gpu, dict)
        or set(gpu) != {"enabled", "kernel_release", "driver_version", "expected_pci_alias"}
        or type(gpu["enabled"]) is not bool
    ):
        fail("plan_gpu")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", _text(gpu["kernel_release"], "kernel_release")):
        fail("kernel_release")
    if gpu["enabled"] and (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", _text(gpu["driver_version"], "driver_version"))
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]+:[1-9][0-9]*(?:,[A-Za-z0-9_.-]+:[1-9][0-9]*)*",
            _text(gpu["expected_pci_alias"], "pci_alias", 512),
        )
    ):
        fail("gpu_pin")
    return plan


def validate_artifacts_manifest(manifest: dict) -> dict:
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "kernel_release", "driver_version", "files"}
        or manifest["schema"] != ARTIFACT_SCHEMA
    ):
        fail("artifact_fields")
    if not isinstance(manifest["kernel_release"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.+-]{1,128}", manifest["kernel_release"]
    ):
        fail("artifact_kernel")
    if not isinstance(manifest["driver_version"], str):
        fail("artifact_driver")
    if not isinstance(manifest["files"], dict) or not manifest["files"]:
        fail("artifact_files")
    for path, record in manifest["files"].items():
        if not isinstance(path, str) or not path or path.startswith("/"):
            fail("artifact_path")
        _path("/" + path)
        if not isinstance(record, dict) or set(record) != {"sha256", "size", "mode"}:
            fail("artifact_record")
        if not isinstance(record["sha256"], str) or not _HEX.fullmatch(record["sha256"]):
            fail("artifact_hash")
        if (
            type(record["size"]) is not int
            or record["size"] < 0
            or type(record["mode"]) is not int
            or record["mode"] & ~0o777
            or record["mode"] & 0o022
        ):
            fail("artifact_metadata")
    return manifest


def _open_regular(path: str, *, root: str = "/") -> int:
    """No-follow every component, not just the terminal payload."""
    parts = PurePosixPath(_path(path)).parts[1:]
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            fail("regular_file")
        return fd
    finally:
        os.close(directory)


def read_regular(path: str, *, private=False, maximum: int = MAX_JSON) -> bytes:
    fd = _open_regular(path)
    try:
        info = os.fstat(fd)
        if info.st_size > maximum or (private and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600)):
            fail("private_file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(maximum + 1)
    finally:
        os.close(fd)


def wipe_private_file(path: str) -> None:
    content = read_regular(path, private=True)
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        remaining = len(content)
        while remaining:
            written = os.write(fd, b"\x00" * min(remaining, 4096))
            if written <= 0:
                hold("private_file_wipe")
            remaining -= written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(path)


def verify_file(path: str, digest: str, size: int, *, mode: int | None = None) -> dict:
    fd = _open_regular(path)
    try:
        before = os.fstat(fd)
        if before.st_size != size or (mode is not None and stat.S_IMODE(before.st_mode) != mode):
            fail("file_metadata")
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or h.hexdigest() != digest.removeprefix("sha256:"):
            fail("file_hash")
        return {"sha256": h.hexdigest(), "size": size}
    finally:
        os.close(fd)


def readonly_denial(error: OSError | None) -> bool:
    return error is not None and error.errno in (errno.EACCES, errno.EROFS)


def assert_root_identity(
    root: tuple[int, int], proc1: tuple[int, int], expected: tuple[int, int], filesystem: str
) -> None:
    if root != expected or proc1 != expected or filesystem != "overlay":
        fail("actual_root_identity")


def resolve_user(user: str, passwd: str, group: str) -> tuple[int, int, list[int]]:
    """Resolve exclusively against the mounted image, never carrier NSS."""
    name, sep, group_name = user.partition(":")
    if not name:
        if sep:
            fail("runtime_user")
        return 0, 0, []
    accounts = []
    for line in passwd.splitlines():
        fields = line.split(":")
        if len(fields) == 7 and fields[2].isdigit() and fields[3].isdigit():
            accounts.append((fields[0], int(fields[2]), int(fields[3])))
    matches = [a for a in accounts if (a[1] == int(name) if name.isdigit() else a[0] == name)]
    if len(matches) > 1 or (not matches and not name.isdigit()):
        fail("user_unresolved")
    uid = int(name) if name.isdigit() else matches[0][1]
    gid = matches[0][2] if matches else 0
    groups = []
    group_records = []
    for line in group.splitlines():
        fields = line.split(":")
        if len(fields) == 4 and fields[2].isdigit():
            group_records.append((fields[0], int(fields[2]), fields[3].split(",")))
    if sep:
        if not group_name:
            fail("group_unresolved")
        found = [g for g in group_records if g[0] == group_name]
        if group_name.isdigit():
            gid = int(group_name)
        elif len(found) == 1:
            gid = found[0][1]
        else:
            fail("group_unresolved")
    elif matches:
        groups = sorted({g[1] for g in group_records if matches[0][0] in g[2] and g[1] != gid})
    if max([uid, gid] + groups) >= 2**32 - 1:
        fail("user_range")
    return uid, gid, groups


def dhcp_script() -> str:
    """udhcpc callback, explicit execs; no eval of DHCP-provided strings."""
    return """#!/bin/sh
set -eu
case "$1" in
 deconfig) /bin/busybox ip addr flush dev "$interface" ;;
 bound|renew)
  /bin/busybox ip addr flush dev "$interface"
  /bin/busybox ifconfig "$interface" "$ip" netmask "$subnet" up
  # BusyBox's unspecified-family default selector also matches connected routes.
  /bin/busybox ip -4 route flush default
  for gateway in ${router:-}; do
   /bin/busybox ip route add default via "$gateway" dev "$interface"
   break
  done
  set -- ${staticroutes:-${msstaticroutes:-}}
  while [ "$#" -ge 2 ]; do
   /bin/busybox ip route replace "$1" via "$2" dev "$interface"
   shift 2
  done
  [ "$#" -eq 0 ]
  : > /run/resolv.conf
  for server in ${dns:-}; do printf 'nameserver %s\\n' "$server" >> /run/resolv.conf; done
  ;;
 *) exit 1 ;;
esac
"""


def _process_id() -> int:
    return os.getpid()


def _runtime_identity() -> tuple[str, int, int]:
    return sys.platform, os.geteuid(), _process_id()


def init_script() -> str:
    return "#!/bin/sh\nexec /usr/bin/python3 -I -B /bootstrap/nova_ceph_boot.py --seed /bootstrap/seed.json\n"


class Syscalls:
    """Tiny injected syscall seam; failures are never converted into success."""

    def __init__(self, *, library=None):
        self.libc = library if library is not None else ctypes.CDLL(None, use_errno=True)
        self.libc.mount.argtypes = [ctypes.c_char_p] * 3 + [ctypes.c_ulong, ctypes.c_void_p]
        self.libc.mount.restype = ctypes.c_int

    def mount(
        self, source: str | None, target: str, filesystem: str | None = None, flags: int = 0, data: str | None = None
    ):
        args = [s.encode() if s is not None else None for s in (source, target, filesystem)]
        options = data.encode() if data is not None else None
        if self.libc.mount(*args, flags, options) != 0:
            fail("mount_failed")

    def chroot(self, path: str):
        os.chroot(path)

    def chdir(self, path: str):
        os.chdir(path)

    def identity(self, path: str) -> tuple[int, int]:
        info = os.stat(path)
        return info.st_dev, info.st_ino


def _mkdir(path: str):
    Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        fail("mountpoint")


def _run(argv: list[str], *, timeout: float = 30, env: dict | None = None) -> str:
    # No Popen context manager: its exit can wait indefinitely for a retained
    # uncertain process. Helpers get TERM plus bounded grace, never KILL.
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        data, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _RETAINED_PROCESSES.append(process)
            hold("helper_term_unsettled")
        fail("command_timeout")
    if process.returncode != 0 or len(data) > MAX_JSON:
        fail("command_failed")
    return data.decode("utf-8", errors="strict")


def mount_table() -> list[dict]:
    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, tail = left.split(), right.split()

        # Mountpoints generated here never contain whitespace; decode kernel
        # mountinfo escapes for reporting and exact identity checks.
        def unescape(value):
            return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

        result.append(
            {
                "id": fields[0],
                "device": fields[2],
                "root": unescape(fields[3]),
                "target": unescape(fields[4]),
                "options": fields[5].split(","),
                "fstype": tail[0],
                "source": unescape(tail[1]),
                "super": tail[2].split(","),
            }
        )
    return result


def _mount_entry(path: str, filesystem: str | None = None, *, ro=False) -> dict:
    entries = [m for m in mount_table() if m["target"] == path]
    if (
        len(entries) != 1
        or (filesystem and entries[0]["fstype"] != filesystem)
        or (ro and "ro" not in entries[0]["options"])
    ):
        fail("mount_identity")
    return entries[0]


def _unique_label(label: str, filesystem: str) -> str:
    devices = _run(["/sbin/blkid", "-t", "LABEL=" + label, "-o", "device"]).splitlines()
    if len(devices) != 1 or not devices[0].startswith("/dev/"):
        fail("label_not_exclusive")
    device = devices[0]
    if not stat.S_ISBLK(os.stat(device).st_mode):
        fail("label_device")
    if _run(["/sbin/blkid", "-s", "TYPE", "-o", "value", device]).strip() != filesystem:
        fail("label_filesystem")
    rdev = os.stat(device).st_rdev
    identity = f"{os.major(rdev)}:{os.minor(rdev)}"
    if any(m["device"] == identity for m in mount_table()):
        fail("label_already_mounted")
    return device


class TLSClient:
    def __init__(self, seed: dict):
        self.seed = seed
        self.context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=seed["ca_file"])
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(seed["client_cert"], seed["client_key"])
        self.peer_sha256 = None

    def post(self, path: str, payload: dict) -> dict:
        remaining = self.seed["deadline"] - time.time()
        if remaining <= 0:
            fail("deadline")
        # Connect only to the loopback SSH forward, verify the enrolled worker
        # identity with explicit SNI/hostname, never disable hostname checking.
        raw = socket.create_connection(("127.0.0.1", 9443), timeout=min(15, remaining))
        try:
            with self.context.wrap_socket(raw, server_hostname=self.seed["tls_server_name"]) as tls:
                peer = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
                if self.peer_sha256 is not None and self.peer_sha256 != peer:
                    fail("tls_peer_changed")
                self.peer_sha256 = peer
                body = canonical(payload)
                request = (
                    f"POST {path} HTTP/1.1\r\nHost: {self.seed['tls_server_name']}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode() + body
                tls.sendall(request)
                response = http.client.HTTPResponse(tls)
                response.begin()
                if path == "/v1/bootstrap/" + self.seed["run_id"] and response.status == 404:
                    raise BootstrapPending(response.status)
                if (
                    response.status != 200
                    or response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json"
                ):
                    fail("service_response")
                content = response.read(MAX_JSON + 1)
                return strict_json(content)
        finally:
            raw.close()


def wait_for_bootstrap(client, seed: dict, tunnel) -> dict:
    """Retry only authenticated pending enrollment 404, preserving identity.

    The service's same-nonce journal makes these repeated reads idempotent;
    authorization, TLS verification, peer changes and malformed plans do not
    enter this loop's retry class. Wall and monotonic limits both apply.
    """
    until = time.monotonic() + min(300, max(0, seed["deadline"] - time.time()))
    delay = 1.0
    while True:
        remaining = min(seed["deadline"] - time.time(), until - time.monotonic())
        if remaining <= 0:
            fail("bootstrap_wait_expired")
        if tunnel.poll() is not None:
            fail("ssh_tunnel")
        try:
            reply = client.post("/v1/bootstrap/" + seed["run_id"], {"nonce": seed["nonce"]})
            if time.monotonic() >= until or time.time() >= seed["deadline"]:
                fail("bootstrap_wait_expired")
            return reply
        except BootstrapPending as pending:
            if pending.status != 404:
                fail("service_response")
        remaining = min(seed["deadline"] - time.time(), until - time.monotonic())
        if remaining <= 0:
            fail("bootstrap_wait_expired")
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 5.0)


def validate_bootstrap(reply: dict, seed: dict, *, now: float | None = None) -> tuple[dict, dict]:
    if (
        set(reply) != {"schema", "run_id", "nonce", "expires_unix", "plan", "ceph_access"}
        or reply["schema"] != BOOTSTRAP_SCHEMA
    ):
        fail("bootstrap_fields")
    if reply["run_id"] != seed["run_id"] or reply["nonce"] != seed["nonce"]:
        fail("bootstrap_binding")
    if (
        type(reply["expires_unix"]) is not int
        or not (now if now is not None else time.time()) < reply["expires_unix"] <= seed["deadline"]
    ):
        fail("bootstrap_expired")
    access = reply["ceph_access"]
    if not isinstance(access, dict) or set(access) not in (
        {"export", "name", "secret"},
        {"export", "name", "secret", "fs_name"},
    ):
        fail("ceph_access")
    export = _text(access["export"], "ceph_export")
    # Manila kernel Ceph exports: monitor[,monitor...]:/exact/subvolume/path.
    monitors, sep, subvolume = export.partition(":/")
    if (
        not sep
        or not monitors
        or not subvolume
        or any(c in export for c in "\n\r \t\\")
        or ".." in subvolume.split("/")
    ):
        fail("ceph_export")
    for monitor in monitors.split(","):
        host, colon, port = monitor.partition(":")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            fail("ceph_monitor")
        if colon and (not port.isdigit() or not 0 < int(port) < 65536):
            fail("ceph_monitor")
    if not isinstance(access["name"], str) or not _SAFE_NAME.fullmatch(access["name"]):
        fail("ceph_name")
    if not isinstance(access["secret"], str) or not re.fullmatch(r"[A-Za-z0-9+/]{16,256}={0,2}", access["secret"]):
        fail("ceph_secret")
    if "fs_name" in access and (
        not isinstance(access["fs_name"], str)
        or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", access["fs_name"]) is None
    ):
        fail("ceph_filesystem")
    storage = reply.get("plan", {}).get("storage")
    if storage is not None and (
        not isinstance(storage, dict)
        or access.get("export") != storage.get("export")
        or access.get("fs_name") != storage.get("fs_name")
    ):
        fail("ceph_storage_binding")
    return reply["plan"], access


def metadata_instance() -> str:
    connection = http.client.HTTPConnection("169.254.169.254", timeout=5)
    try:
        connection.request("GET", "/openstack/latest/meta_data.json", headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            fail("metadata_status")
        content = response.read(65537)
        if len(content) > 65536:
            fail("metadata_size")
        return _uuid(strict_json(content)["uuid"])
    except (KeyError, ValueError):
        fail("metadata_identity")
    finally:
        connection.close()


def switch_actual_root(syscalls, newroot: str, expected: tuple[int, int]) -> None:
    """PID1 initramfs transition, not a child chroot in a conventional VM."""
    if _process_id() != 1:
        fail("pid1_required")
    _mount_entry(newroot, "overlay")
    for source in MOVED_MOUNTS:
        destination = newroot + source
        _mkdir(destination)
        syscalls.mount(source, destination, flags=MS_MOVE)
    syscalls.chdir(newroot)
    syscalls.mount(newroot, "/", flags=MS_MOVE)
    syscalls.chroot(".")
    syscalls.chdir("/")
    root, proc1 = syscalls.identity("/"), syscalls.identity("/proc/1/root")
    assert_root_identity(root, proc1, expected, _mount_entry("/", "overlay")["fstype"])


class Runtime:
    def __init__(self, seed: dict, *, syscalls=None, seed_path="/bootstrap/seed.json"):
        self.seed = seed
        self.seed_path = seed_path
        self.syscalls = syscalls if syscalls is not None else Syscalls()
        self.client = None
        self.tunnel = None
        self.plan = None
        self.artifacts = None
        self.checks = {}
        self.failure_stage = "prepare_mounts"
        self.oldroot = "/.palimpsest-bootstrap"

    def prepare(self):
        for target, filesystem, source, flags, options in (
            ("/proc", "proc", "proc", MS_NOSUID | MS_NODEV, None),
            ("/sys", "sysfs", "sysfs", MS_NOSUID | MS_NODEV, None),
            ("/dev", "devtmpfs", "devtmpfs", MS_NOSUID, "mode=0755"),
            ("/run", "tmpfs", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0700"),
            (INTERNAL, "tmpfs", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0700"),
        ):
            _mkdir(target)
            self.syscalls.mount(source, target, filesystem, flags, options)
        self.syscalls.mount(None, "/", flags=MS_PRIVATE | MS_REC)
        # A conventional guest cannot invoke this module to masquerade as a
        # native-root proof; only an initramfs PID1 is admitted.
        root_fs = _mount_entry("/")["fstype"]
        if root_fs not in {"rootfs", "ramfs", "tmpfs"}:
            fail("initramfs_required")
        # Verify the initramfs inventory before executing helpers or loading
        # even the ext4/virtio modules needed to discover the carrier disk.
        self.failure_stage = "prepare_manifest"
        manifest_data = read_regular(self.seed["artifacts_manifest"], maximum=32 * MAX_JSON)
        self.artifacts = validate_artifacts_manifest(strict_json(manifest_data, maximum=32 * MAX_JSON))
        if "sha256:" + hashlib.sha256(canonical(self.artifacts)).hexdigest() != self.seed["carrier_digest"]:
            fail("carrier_digest")
        if os.uname().release != self.artifacts["kernel_release"]:
            fail("kernel_abi")
        for required in REQUIRED_EXECUTABLES:
            if required.lstrip("/") not in self.artifacts["files"]:
                fail("required_artifact_missing")
        for index in ("modules.builtin", "modules.builtin.modinfo", "modules.dep", "modules.dep.bin"):
            if "lib/modules/" + self.artifacts["kernel_release"] + "/" + index not in self.artifacts["files"]:
                fail("module_index_inventory")
        for path, record in self.artifacts["files"].items():
            if not path.startswith(("carrier/", "driver/")):
                verify_file("/" + path, record["sha256"], record["size"], mode=record["mode"])
        self.failure_stage = "prepare_modules"
        for module in (
            "virtio_pci",
            "virtio_blk",
            "virtio_scsi",
            "sd_mod",
            "ext4",
            "loop",
            "squashfs",
            "overlay",
            "ceph",
            "virtio_net",
        ):
            _run(["/sbin/modprobe", module])
        _mkdir("/carrier")
        self.failure_stage = "prepare_carrier_mount"
        carrier = _unique_label("PAL-CARRIER", "ext4")
        self.syscalls.mount(carrier, "/carrier", "ext4", MS_RDONLY | MS_NOSUID | MS_NODEV, "noload")
        _mount_entry("/carrier", "ext4", ro=True)
        _mkdir("/driver")
        self.syscalls.mount("/carrier/driver", "/driver", flags=MS_BIND)
        self.syscalls.mount(None, "/driver", flags=MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV)
        self.failure_stage = "prepare_driver_inventory"
        for path, record in self.artifacts["files"].items():
            if path.startswith(("carrier/", "driver/")):
                verify_file("/" + path, record["sha256"], record["size"], mode=record["mode"])
        self.failure_stage = "prepare_identity_files"
        for path in (self.seed["ssh_private_key"], self.seed["client_key"]):
            read_regular(path, private=True)
        for path in (self.seed["worker_known_hosts"], self.seed["client_cert"], self.seed["ca_file"]):
            read_regular(path)
        # All Python extensions used after switch-root are imported now. No
        # dependency is acquired from the upstream image or the network.
        hashlib.sha256(b"").digest()
        self.failure_stage = "prepare_tls"
        self.client = TLSClient(self.seed)
        self.checks["carrier_inventory"] = True

    def network(self) -> str:
        self.failure_stage = "network_interface"
        interfaces = [p.name for p in Path("/sys/class/net").iterdir() if p.name != "lo"]
        if len(interfaces) != 1 or not _SAFE_NAME.fullmatch(interfaces[0]):
            fail("network_interface")
        interface = interfaces[0]
        script = "/run/udhcpc-script"
        Path(script).write_text(dhcp_script())
        os.chmod(script, 0o700)
        _run(["/bin/busybox", "ip", "link", "set", "lo", "up"])
        _run(["/bin/busybox", "ip", "link", "set", interface, "up"])
        self.failure_stage = "network_dhcp"
        _run(
            [
                "/bin/busybox",
                "udhcpc",
                "-n",
                "-q",
                "-t",
                "5",
                "-T",
                "3",
                "-O",
                "121",
                "-O",
                "249",
                "-i",
                interface,
                "-s",
                script,
            ],
            timeout=25,
        )
        self.failure_stage = "network_observation"
        output = _run(["/bin/busybox", "ip", "-4", "-o", "addr", "show", "dev", interface])
        addresses = re.findall(r"\binet ([0-9.]+)/[0-9]+", output)
        if len(addresses) != 1:
            fail("dhcp_address")
        interfaces_with_prefix = re.findall(r"\binet ([0-9.]+/[0-9]+)", output)
        routes = _run(["/bin/busybox", "ip", "-4", "route", "show", "default"]).splitlines()
        gateways = [
            m.group(1)
            for line in routes
            if (m := re.search(r"^default via ([0-9.]+) dev " + re.escape(interface) + r"(?: |$)", line))
        ]
        dns = [
            line.split()[1]
            for line in Path("/run/resolv.conf").read_text().splitlines()
            if line.startswith("nameserver ") and len(line.split()) == 2
        ]
        if len(interfaces_with_prefix) != 1 or len(gateways) != 1:
            fail("dhcp_network")
        self.network_observed = {
            "cidr": str(ipaddress.IPv4Interface(interfaces_with_prefix[0]).network),
            "gateway": gateways[0],
            "dns": dns,
        }
        self.failure_stage = "network_tunnel"
        self.tunnel = subprocess.Popen(
            [
                "/usr/bin/ssh",
                "-N",
                "-T",
                "-F",
                "/dev/null",
                "-i",
                self.seed["ssh_private_key"],
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + self.seed["worker_known_hosts"],
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=10",
                "-o",
                "ServerAliveCountMax=2",
                "-L",
                "127.0.0.1:9443:127.0.0.1:9443",
                "ubuntu@" + self.seed["worker_address"],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        until = min(self.seed["deadline"], time.time() + 15)
        while time.time() < until:
            if self.tunnel.poll() is not None:
                fail("ssh_tunnel")
            try:
                with socket.create_connection(("127.0.0.1", 9443), timeout=1):
                    return addresses[0]
            except OSError:
                time.sleep(0.1)
        fail("ssh_tunnel_timeout")

    def bootstrap(self, address: str) -> dict:
        reply = wait_for_bootstrap(self.client, self.seed, self.tunnel)
        plan, access = validate_bootstrap(reply, self.seed)
        # The carrier seed grants only an outer preparation/key lifetime. The
        # authenticated enrollment starts the narrower post-Nova boot window.
        self.seed = {**self.seed, "deadline": reply["expires_unix"]}
        self.client.seed = {**self.client.seed, "deadline": reply["expires_unix"]}
        self.plan = validate_plan(plan, self.seed, metadata_instance(), address)
        if self.plan["gpu"]["kernel_release"] != self.artifacts["kernel_release"]:
            fail("plan_kernel_abi")
        network = self.plan["network"]
        observed = self.network_observed
        if (
            str(ipaddress.IPv4Network(network["cidr"], strict=False)) != observed["cidr"]
            or network["gateway"] != observed["gateway"]
        ):
            fail("dhcp_plan_binding")
        # DNS comes from the seed-bound authenticated plan, not DHCP identity.
        # Preserve DHCP CIDR/gateway binding while replacing advertised resolvers.
        Path("/run/resolv.conf").write_text("".join("nameserver " + dns + "\n" for dns in network["dns"]))
        self.checks["instance_metadata"] = True
        self.checks["plan_policy"] = True
        return access

    def ceph(self, access: dict):
        _mkdir(INTERNAL + "/ceph")
        secret_path = INTERNAL + "/ceph-key"
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            secret = access["secret"].encode()
            os.write(fd, secret)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            # Mount RW initially solely to test the *provider* RO capability;
            # a local -o ro alone cannot establish storage-enforced denial.
            _run(
                [
                    "/sbin/mount.ceph",
                    access["export"],
                    INTERNAL + "/ceph",
                    "-o",
                    "name="
                    + access["name"]
                    + ",secretfile="
                    + secret_path
                    + ",nosuid,nodev,noexec"
                    + (",fs=" + access["fs_name"] if "fs_name" in access else ""),
                ],
                timeout=30,
            )
        finally:
            with open(secret_path, "r+b", buffering=0) as stream:
                stream.write(b"\x00" * len(secret))
                os.fsync(stream.fileno())
            os.unlink(secret_path)
            access["secret"] = ""
            del secret
        mounted = _mount_entry(INTERNAL + "/ceph", "ceph")
        if mounted["source"] != access["export"] or "name=" + access["name"] not in mounted["super"]:
            fail("ceph_mount_binding")
        if "rw" not in mounted["options"] or "ro" in mounted["options"]:
            fail("provider_probe_mount_not_rw")
        probe = INTERNAL + "/ceph/.palimpsest-ro-probe-" + self.seed["nonce"]
        denial = None
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            denial = error
        else:
            os.close(fd)
            os.unlink(probe)
        if not readonly_denial(denial):
            fail("provider_readonly_not_proven")
        self.syscalls.mount(
            None, INTERNAL + "/ceph", flags=MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC
        )
        _mount_entry(INTERNAL + "/ceph", "ceph", ro=True)
        self.checks["provider_readonly_errno"] = denial.errno

    def assemble(self) -> tuple[int, int]:
        mounts = []
        records = []
        for index, (layer, relative) in enumerate(
            zip(self.plan["image"]["layers"], ordered_lower_paths(self.plan["image"]["layers"]), strict=True)
        ):
            path = INTERNAL + "/ceph/" + relative
            records.append(verify_file(path, layer["digest"], layer["size"]))
            target = INTERNAL + f"/lower/{index}"
            _mkdir(target)
            loop = _run(["/sbin/losetup", "--find", "--show", "--read-only", "--", path]).strip()
            if not re.fullmatch(r"/dev/loop[0-9]+", loop):
                fail("loop_device")
            self.syscalls.mount(loop, target, "squashfs", MS_RDONLY | MS_NOSUID | MS_NODEV)
            _mount_entry(target, "squashfs", ro=True)
            mounts.append(target)
        self.checks["layers"] = records
        upper_device = _unique_label("PAL-UPPER", "ext4")
        _mkdir(INTERNAL + "/private")
        self.syscalls.mount(upper_device, INTERNAL + "/private", "ext4", MS_NOSUID | MS_NODEV, "errors=remount-ro")
        _mount_entry(INTERNAL + "/private", "ext4")
        # A retained or pre-populated writable volume is not a fresh private
        # overlay. Never reuse its prior upper/work state.
        if set(os.listdir(INTERNAL + "/private")) - {"lost+found"}:
            fail("upper_not_empty")
        for path in (INTERNAL + "/private/upper", INTERNAL + "/private/work", "/newroot"):
            _mkdir(path)
        self.syscalls.mount(
            "overlay",
            "/newroot",
            "overlay",
            MS_NOSUID | MS_NODEV,
            "lowerdir="
            + overlay_lowers(mounts)
            + ",upperdir="
            + INTERNAL
            + "/private/upper,workdir="
            + INTERNAL
            + "/private/work",
        )
        _mount_entry("/newroot", "overlay")
        root = os.stat("/newroot")
        self.checks["upper_private"] = True
        return root.st_dev, root.st_ino

    def readonly_bind(self, source: str, target: str, *, directory=True):
        # Reject symlinked image mountpoints rather than following an upstream
        # symlink outside the intended destination. Creating mountpoints only
        # touches the private upper, never the original SquashFS blobs.
        components = Path(target).parts
        current = Path("/")
        for component in components[1:-1]:
            current /= component
            if current.exists() and (current.is_symlink() or not current.is_dir()):
                fail("bind_mountpoint")
            current.mkdir(mode=0o755, exist_ok=True)
        if os.path.lexists(target):
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) != directory:
                fail("bind_mountpoint")
        elif directory:
            os.mkdir(target, 0o755)
        else:
            Path(target).touch(mode=0o644)
        self.syscalls.mount(source, target, flags=MS_BIND)
        self.syscalls.mount(None, target, flags=MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
        _mount_entry(target, ro=True)

    def gpu(self) -> dict:
        pin = self.plan["gpu"]
        if not pin["enabled"]:
            return {"enabled": False}
        if pin["driver_version"] != self.artifacts["driver_version"] or os.uname().release != pin["kernel_release"]:
            fail("gpu_inventory_pin")
        if (
            self.plan["command"] != ["/run/palimpsest-probe/cuda-probe"]
            or "carrier/probe/cuda-probe" not in self.artifacts["files"]
        ):
            fail("gpu_probe_inventory")
        self.readonly_bind("/carrier/probe", "/run/palimpsest-probe")
        devices = []
        for path in Path("/sys/bus/pci/devices").iterdir():
            if (path / "vendor").read_text().strip() == "0x10de" and (path / "class").read_text().strip().startswith(
                "0x03"
            ):
                if (path / "driver").exists():
                    fail("gpu_prebound")
                devices.append(path.name)
        if len(devices) != 1:
            fail("gpu_assignment")
        module_root = "/lib/modules/" + pin["kernel_release"] + "/"
        for module in ("nvidia", "nvidia_uvm"):
            resolved = _run(["/sbin/modinfo", "-n", module]).strip()
            if not resolved.startswith(module_root) or resolved.lstrip("/") not in self.artifacts["files"]:
                fail("gpu_module_inventory")
            if _run(["/sbin/modinfo", "-F", "version", resolved]).strip() != pin["driver_version"]:
                fail("gpu_module_version")
            if not _run(["/sbin/modinfo", "-F", "vermagic", resolved]).startswith(pin["kernel_release"] + " "):
                fail("gpu_module_abi")
        self.readonly_bind("/driver/lib/firmware", "/lib/firmware")
        for module in ("nvidia", "nvidia_uvm"):
            _run(["/sbin/modprobe", module], timeout=30)
        if Path("/sys/module/nvidia/version").read_text().strip() != pin["driver_version"]:
            fail("gpu_loaded_version")
        # Only the exact assigned compute device and control/UVM nodes are
        # admitted. No arbitrary major/minor grants and no foreign GPU nodes.
        proc = Path("/proc/driver/nvidia/gpus")
        infos = list(proc.glob("*/information"))
        if len(infos) != 1:
            fail("gpu_driver_assignment")
        info = dict(line.split(":", 1) for line in infos[0].read_text().splitlines() if ":" in line)
        if (
            info.get("Bus Location", "").strip().lower() != devices[0].lower()
            or info.get("Device Minor", "").strip() != "0"
        ):
            fail("gpu_driver_pci")
        gpu_uuid = info.get("GPU UUID", "").strip()
        if not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", gpu_uuid):
            fail("gpu_driver_uuid")
        major = None
        for line in Path("/proc/devices").read_text().splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == "nvidia-uvm":
                major = int(fields[0])
        if major is None:
            fail("gpu_uvm_major")
        allowed = {
            "nvidia0": (195, 0),
            "nvidiactl": (195, 255),
            "nvidia-uvm": (major, 0),
            "nvidia-uvm-tools": (major, 1),
        }
        if {p.name for p in Path("/dev").glob("nvidia*")} - allowed.keys():
            fail("gpu_foreign_devices")
        for name, (device_major, minor) in allowed.items():
            path = "/dev/" + name
            if not os.path.lexists(path):
                os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(device_major, minor))
            node = os.lstat(path)
            if not stat.S_ISCHR(node.st_mode) or (os.major(node.st_rdev), os.minor(node.st_rdev)) != (
                device_major,
                minor,
            ):
                fail("gpu_device_identity")
            # mknod is umask-sensitive; the original image's non-root User
            # needs the same compute-node access as the root preflight.
            os.chmod(path, 0o666)
            if stat.S_IMODE(os.lstat(path).st_mode) != 0o666:
                fail("gpu_device_permissions")
        env = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/driver/usr/lib/x86_64-linux-gnu"}
        output = _run(
            [
                "/driver/usr/bin/nvidia-smi",
                "--query-gpu=uuid,pci.bus_id,driver_version",
                "--format=csv,noheader,nounits",
            ],
            env=env,
        )
        rows = [row.strip().split(", ") for row in output.splitlines() if row.strip()]
        if (
            len(rows) != 1
            or len(rows[0]) != 3
            or rows[0][0] != gpu_uuid
            or rows[0][2] != pin["driver_version"]
            or normalize_pci(rows[0][1]) != normalize_pci(devices[0])
        ):
            fail("gpu_smi_binding")
        self.readonly_bind("/driver/usr/bin", "/newroot/usr/local/nvidia/bin")
        self.readonly_bind("/driver/usr/lib/x86_64-linux-gnu", "/newroot/usr/local/nvidia/lib64")
        return {
            "enabled": True,
            "uuid": gpu_uuid,
            "pci": devices[0],
            "driver_version": pin["driver_version"],
            "kernel_release": os.uname().release,
            "smi_uuid": rows[0][0],
            "expected_pci_alias": pin["expected_pci_alias"],
        }

    def preserve(self):
        # Retain only first-party execution dependencies, not the whole root or
        # carrier: either would expose seed/key files or the original initrd.
        self.readonly_bind("/run/resolv.conf", "/newroot/etc/resolv.conf", directory=False)
        staging = "/newroot" + self.oldroot
        _mkdir(staging)
        self.syscalls.mount("tmpfs", staging, "tmpfs", MS_NOSUID | MS_NODEV, "mode=0700")
        for source in ("/usr", "/lib"):
            self.readonly_bind(source, staging + source)
        self.readonly_bind("/bootstrap/nova_ceph_boot.py", staging + "/bootstrap/nova_ceph_boot.py", directory=False)
        self.syscalls.mount(None, staging, flags=MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
        for path in {self.seed_path, self.seed["ssh_private_key"], self.seed["client_key"]}:
            wipe_private_file(path)
        # SSLContext and SSH already hold the enrolled identities in memory.
        sys.path[:] = [self.oldroot + p if p.startswith("/") else p for p in sys.path]

    def post_result(self, result: dict):
        if self.client is not None and self.plan is not None:
            self.client.post("/v1/results/" + self.seed["run_id"], {"nonce": self.seed["nonce"], "result": result})

    def execute(self) -> dict:
        self.prepare()
        address = self.network()
        self.failure_stage = "bootstrap"
        access = self.bootstrap(address)
        self.failure_stage = "ceph"
        self.ceph(access)
        self.failure_stage = "assemble"
        expected = self.assemble()
        self.failure_stage = "gpu"
        gpu = self.gpu()
        self.failure_stage = "preserve"
        self.preserve()
        self.failure_stage = "switch_root"
        switch_actual_root(self.syscalls, "/newroot", expected)
        self.failure_stage = "workload"
        # Avoid image NSS plugins or on-demand imports after transition.
        argv, env, cwd, user = runtime_vector(self.plan["image"]["runtime"], self.plan["command"])
        passwd = Path("/etc/passwd").read_text() if Path("/etc/passwd").exists() else ""
        group = Path("/etc/group").read_text() if Path("/etc/group").exists() else ""
        uid, gid, groups = resolve_user(user, passwd, group)
        if gpu["enabled"]:
            env["PATH"] = "/usr/local/nvidia/bin:" + env.get(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            )
            env["LD_LIBRARY_PATH"] = "/usr/local/nvidia/lib64" + (
                ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
            )
        status, stdout = supervise(
            argv,
            env,
            cwd,
            uid,
            gid,
            groups,
            self.seed["deadline"] - 10,
            auxiliary_pids={self.tunnel.pid},
            capture=gpu["enabled"],
        )
        if gpu["enabled"]:
            gpu["computation"] = validate_cuda_result(stdout, gpu["uuid"], status)
        observed_root = self.syscalls.identity("/")
        observed_proc1 = self.syscalls.identity("/proc/1/root")
        root_entry = _mount_entry("/", "overlay")
        assert_root_identity(observed_root, observed_proc1, expected, root_entry["fstype"])
        result = {
            "schema": RESULT_SCHEMA,
            "run_id": self.seed["run_id"],
            "server_id": self.plan["server_id"],
            "success": status == 0,
            "root": {
                "pid1": _process_id(),
                "filesystem": "overlay",
                "device": observed_root[0],
                "inode": observed_root[1],
                "mount_id": root_entry["id"],
                "mountinfo": root_entry,
                "proc1_same_root": observed_proc1 == observed_root,
                "proc1_device": observed_proc1[0],
                "proc1_inode": observed_proc1[1],
            },
            "checks": self.checks,
            "gpu": gpu,
            "command": {
                "exit_code": status,
                "uid": uid,
                "gid": gid,
                "runtime_user": user,
                "argv": argv,
                "entrypoint": self.plan["image"]["runtime"]["entrypoint"],
                "cmd_present": "cmd" in self.plan["image"]["runtime"],
            },
            "trust": "trusted-workload-observation-not-attestation",
        }
        self.post_result(result)
        return result


def normalize_pci(address: str) -> str:
    match = re.fullmatch(r"([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])", address)
    if not match:
        fail("pci_address")
    return f"{int(match[1], 16):04x}:{match[2].lower()}:{match[3].lower()}.{match[4]}"


def validate_cuda_result(stdout: bytes, gpu_uuid: str, status: int) -> dict:
    if status != 0 or not stdout or len(stdout) > 65536:
        fail("cuda_computation")
    observation = strict_json(stdout.splitlines()[-1])
    if (
        set(observation)
        != {
            "success",
            "device_index",
            "device_uuid",
            "device_name",
            "results",
            "checksum",
            "runtime_version",
            "cublas_results",
            "cublas_checksum",
        }
        or observation["success"] is not True
        or type(observation["device_index"]) is not int
        or observation["device_index"] != 0
        or observation["device_uuid"] != gpu_uuid
        or observation["results"] != [2, 6, 12, 20, 30, 42, 56, 72, 90]
        or type(observation["checksum"]) is not int
        or observation["checksum"] != 330
    ):
        fail("cuda_computation")
    if any(type(value) is not int for value in observation["results"]):
        fail("cuda_computation")
    if (
        type(observation["runtime_version"]) is not int
        or observation["runtime_version"] != 12020
        or observation["cublas_results"] != [90, 114, 138, 54, 69, 84, 18, 24, 30]
        or type(observation["cublas_checksum"]) not in (int, float)
        or observation["cublas_checksum"] != 621
        or any(type(value) not in (int, float) for value in observation["cublas_results"])
    ):
        fail("cuda_image_libraries")
    _text(observation["device_name"], "cuda_device_name", 256)
    return observation


def process_record(text: str) -> dict:
    # /proc stat comm may itself contain spaces and parentheses.
    _, separator, tail = text.rpartition(") ")
    fields = tail.split()
    if not separator or len(fields) < 20:
        hold("process_census_invalid")
    try:
        return {
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
            "kernel": bool(int(fields[6]) & 0x00200000),
            "start": int(fields[19]),
        }
    except ValueError:
        hold("process_census_invalid")


def process_snapshot() -> dict[int, dict]:
    snapshot = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            snapshot[int(path.name)] = process_record((path / "stat").read_text())
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            hold("process_census_unavailable")
    return snapshot


def classify_processes(snapshot: dict, owned: dict, auxiliary: dict, child: int) -> tuple[dict, set[int]]:
    """Pure ownership census; unknown or PID-reused processes are never owned."""
    owned = dict(owned)
    unknown = set()
    for pid, started in auxiliary.items():
        if pid not in snapshot or snapshot[pid]["start"] != started:
            hold("auxiliary_process_changed")
    for pid, started in owned.items():
        if pid in snapshot and started is not None and snapshot[pid]["start"] != started:
            hold("workload_pid_reused")
    changed = True
    while changed:
        changed = False
        for pid, record in snapshot.items():
            if pid == 1 or pid in auxiliary or record["kernel"]:
                continue
            if pid in owned or record["pgrp"] == child or record["session"] == child or record["ppid"] in owned:
                if owned.get(pid) != record["start"]:
                    owned[pid] = record["start"]
                    changed = True
    for pid, record in snapshot.items():
        if pid != 1 and not record["kernel"] and pid not in auxiliary and pid not in owned:
            unknown.add(pid)
    return owned, unknown


def supervise(
    argv: list[str],
    env: dict,
    cwd: str,
    uid: int,
    gid: int,
    groups: list[int],
    deadline: int,
    *,
    auxiliary_pids: set[int],
    capture: bool = False,
) -> tuple[int, bytes]:
    """PID1 with explicit SSH exclusion, TERM-only grace and conservative HOLD.

    Success needs the main child reaped, every observed descendant settled,
    the workload process group absent and (for GPU) stdout EOF. An escaped or
    late unclassified process is uncertainty, not evidence of a clean exit.
    """
    if _process_id() != 1:
        fail("pid1_required")
    before = process_snapshot()
    if not auxiliary_pids or any(pid not in before or pid == 1 for pid in auxiliary_pids):
        hold("auxiliary_process_missing")
    auxiliary = {pid: before[pid]["start"] for pid in auxiliary_pids}
    reader, writer = os.pipe() if capture else (None, None)
    if reader is not None:
        os.set_blocking(reader, False)
    child = os.fork()
    if child == 0:
        try:
            if reader is not None:
                os.close(reader)
                os.dup2(writer, 1)
                os.close(writer)
            os.setsid()
            for signum in {signal.SIGPIPE} | {
                getattr(signal, name) for name in ("SIGXFZ", "SIGXFSZ") if hasattr(signal, name)
            }:
                signal.signal(signum, signal.SIG_DFL)
            os.chdir(cwd)
            os.setgroups(groups)
            os.setgid(gid)
            os.setuid(uid)
            os.execvpe(argv[0], argv, env)
        except BaseException:
            os._exit(126)
    if writer is not None:
        os.close(writer)
    output = bytearray()
    owned = {child: None}
    eof = not capture
    overflow = False

    def forward(signum, _frame):
        snapshot = process_snapshot()
        # The group belongs to the fresh setsid child, never to PID1/SSH.
        grouped = any(pid in owned and record["pgrp"] == child for pid, record in snapshot.items())
        if grouped:
            try:
                os.killpg(child, signum)
            except ProcessLookupError:
                pass
        for pid, started in owned.items():
            record = snapshot.get(pid)
            if record is not None and record["start"] == started and record["pgrp"] != child:
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass

    def drain():
        nonlocal eof, overflow
        if reader is None:
            return
        while True:
            try:
                chunk = os.read(reader, 4096)
            except BlockingIOError:
                return
            if not chunk:
                eof = True
                return
            remaining = 65536 - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow = True

    previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)}
    timed_out = False
    term_deadline = None
    main_status = None
    stable = 0
    settled = False
    try:
        while True:
            drain()
            snapshot = process_snapshot()
            owned, unknown = classify_processes(snapshot, owned, auxiliary, child)
            if unknown:
                hold("untracked_workload_process")
            while True:
                try:
                    reaped, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    reaped = 0
                if not reaped:
                    break
                if reaped in auxiliary:
                    hold("auxiliary_process_exited")
                if reaped not in owned:
                    hold("untracked_workload_child")
                if reaped == child:
                    main_status = os.waitstatus_to_exitcode(status)
            timed_out = timed_out or time.time() >= deadline
            if (main_status is not None or timed_out or overflow) and term_deadline is None:
                forward(signal.SIGTERM, None)
                term_deadline = time.monotonic() + 5
            live_owned = any(pid in snapshot for pid in owned)
            try:
                os.killpg(child, 0)
                group_present = True
            except ProcessLookupError:
                group_present = False
            except PermissionError:
                hold("workload_group_unknown")
            if main_status is not None and not live_owned and not group_present and eof:
                stable += 1
                if stable >= 2:
                    settled = True
                    if overflow:
                        fail("command_output_limit")
                    return (124 if timed_out else main_status), bytes(output)
            else:
                stable = 0
            if term_deadline is not None and time.monotonic() >= term_deadline:
                hold("workload_term_unsettled")
            time.sleep(0.05)
    finally:
        if reader is not None:
            if settled:
                os.close(reader)
            else:
                # Closing an unsettled child's pipe would induce SIGPIPE.
                # Retain and drain it in HOLD instead of indirectly killing it.
                _RETAINED_READERS.append(reader)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def retain_pid1() -> None:
    """Retain failed or completed guest resources; reap without force signals."""
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, lambda _signum, _frame: None)
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        for reader in tuple(_RETAINED_READERS):
            for _ in range(16):
                try:
                    if not os.read(reader, 4096):
                        _RETAINED_READERS.remove(reader)
                        os.close(reader)
                        break
                except BlockingIOError:
                    break
        time.sleep(1)


def failure_result(run_id: str, error: BaseException) -> dict:
    # Arbitrary exception messages are never reflected. Only codes originating
    # at internal fail()/hold() callsites are retained as bounded diagnostics.
    code = error.code if isinstance(error, BootError) else "native_boot_failed"
    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
        code = "native_boot_failed"
    return {
        "schema": RESULT_SCHEMA,
        "run_id": run_id,
        "success": False,
        "failure": code,
        "disposition": "HOLD",
        "trust": "trusted-workload-observation-not-attestation",
    }


def _failure_diagnostic(stage: str, error: BaseException) -> dict:
    # Only fixed local labels and known types reach serial. Never messages,
    # filenames, traceback locals, certificate data or arbitrary class names.
    stages = {
        "seed",
        "prepare_mounts",
        "prepare_manifest",
        "prepare_modules",
        "prepare_carrier_mount",
        "prepare_driver_inventory",
        "prepare_identity_files",
        "prepare_tls",
        "network_interface",
        "network_dhcp",
        "network_observation",
        "network_tunnel",
        "bootstrap",
        "ceph",
        "assemble",
        "gpu",
        "preserve",
        "switch_root",
        "workload",
    }
    types = (
        BootError,
        HoldError,
        OSError,
        FileNotFoundError,
        PermissionError,
        NotADirectoryError,
        IsADirectoryError,
        TimeoutError,
        ConnectionError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
        ssl.SSLError,
        ssl.SSLCertVerificationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
        ImportError,
        ModuleNotFoundError,
    )
    known = type(error) in types
    number = error.errno if known and isinstance(error, OSError) else None
    return {
        "stage": stage if isinstance(stage, str) and stage in stages else "unknown",
        "error_type": type(error).__name__ if known else "Exception",
        "errno": number if type(number) is int and 0 <= number <= 4096 else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args(argv)
    if _runtime_identity() != ("linux", 0, 1):
        print("palimpsest native boot refused: Linux root initramfs PID1 required", file=sys.stderr)
        return 1
    runtime = None
    try:
        seed = validate_seed(strict_json(read_regular(_path(args.seed, bootstrap=True), private=True)))
        runtime = Runtime(seed, seed_path=args.seed)
        runtime.execute()
        retain_pid1()
    except BaseException as error:
        result = failure_result(runtime.seed["run_id"] if runtime is not None else "unbound", error)
        print("palimpsest native boot HOLD: " + result["failure"], file=sys.stderr)
        diagnostic = _failure_diagnostic(runtime.failure_stage if runtime is not None else "seed", error)
        print("palimpsest native boot diagnostic: " + json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
        if runtime is not None:
            try:
                runtime.post_result(result)
            except BaseException:
                pass
        retain_pid1()


if __name__ == "__main__":
    raise SystemExit(main())
