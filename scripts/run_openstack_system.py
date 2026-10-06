#!/usr/bin/env python3
"""Member-only OpenStack SYSTEM direct-Nova native Afterglow build/run acceptance runner.

Phases (each resumable from the durable owner receipt ``<evidence>/system-run.json``):

* ``prepare``: owned keypair, SSH security group, builder port/FIP/boot volume/server, Manila CephFS
  artifact share with a cephx access rule, verified SSH host key, apt-only builder bootstrap, share mount.
* ``build``: SSH-transfer the verified source bundle, BuildKit ``native-cloud-vm`` rootfs tar streamed through
  gzip into the share, the bundle's own exporter creates the BIOS raw disk, authenticated byte upload to Glance.
* ``run``: RGW bucket checksum round trip, consumer boot volume from the derived image plus a blank data volume,
  direct Nova boot, systemd/supervisor/HTTP/schema/no-container-PID1 proof, CephFS and data-volume markers,
  member-account Afterglow login, reboot persistence of MariaDB/Redis/data volume/CephFS markers.
* ``cleanup``: delete only receipt-recorded, ownership-verified resources and verify absence.

The direct Nova ``native-cloud-vm`` boot is an additional conventional-cloud contract. It is not a proof of
protected stage-1 OCI-root execution, untouched OCI image execution, OCI security parity, or a public
Palimpsest OpenStack backend.

Every phase requires a parent-written ``--source-approval`` receipt (schema
``palimpsest.system-source-approval.v1``) naming the approved full Afterglow ref plus the exact bundle and
manifest digests. One process-wide lock and one durable active binding
(``~/.local/state/palimpsest/openstack-system/<project>/active.json``) allow a single SYSTEM experiment at a
time; the binding is released only by verified cleanup. Non-cleanup phases require the dedicated member
login account in the profile.

Run with ``uv run --with 'openstacksdk==3.3.0' python scripts/run_openstack_system.py ...``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import posixpath
import re
import shlex
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Exact approved configuration ────────────────────────────────────────────

RECEIPT_SCHEMA = 1
RECEIPT_NAME = "system-run.json"
PROFILE_SCHEMA = 1

APPROVED_PROJECT_ID = "944405f937c3410f9b4f082b20a22e10"
FORBIDDEN_PROJECT_HEX = frozenset({"e4f1487023f047b59901f28a91876e7b"})
APPROVED_AFTERGLOW_REF = "ff007edb6e4bf75f0c0a05107ca6340b593675ff"

APPROVED_FLAVOR_ID = "783c0eda-08f5-4233-8ddf-c142cfa0ff76"
APPROVED_FLAVOR_NAME = "cpu.2c_8g"
APPROVED_FLAVOR_VCPUS = 2
APPROVED_FLAVOR_RAM_MIB = 8192
APPROVED_NETWORK_ID = "64225535-e4ec-43b4-a94e-ff2c0eb4cd58"
APPROVED_EXTERNAL_NETWORK_ID = "02dd84ca-653d-4fd6-a425-05b870786c87"
BUILDER_IMAGE_ID = "2a57a904-fc77-46e6-851a-148e8cb0ca98"
BUILDER_IMAGE_SHA512 = (
    "66b04eb63a398a4ba0162de07d9a5398b2f93a7922e4ffecc96138f0829f48b4"
    "360184faae2e25e921b63041907faea56e3698d448cd06877e9329ebdec44c3f"
)
BUILDER_IMAGE_MAX_VIRTUAL_BYTES = 10 * 1024**3
APPROVED_VOLUME_TYPE = "ceph_hdd"
VOLUME_SIZES_GIB = {"builder-boot": 20, "consumer-boot": 20, "data": 8}
MAX_TOTAL_VOLUME_GIB = 48
SHARE_SIZE_GIB = 10
SHARE_TYPE = "cephfs"
SHARE_PROTO = "CEPHFS"
SSH_SOURCE_CIDR = "10.8.0.2/32"
# Exact existing SYSTEM security groups the user explicitly approved for owned ports (CLI); never mutated.
USER_PORT_SECURITY_GROUPS: tuple[str, ...] = ()
PROTECTED_FLOATING_IPS = frozenset({"172.30.102.169"})
EXPORT_DISK_GIB = 8
MAX_ARTIFACT_BYTES = 10 * 1024**3
ARTIFACT_MARGIN_BYTES = 64 * 1024**2
MAX_BUNDLE_BYTES = 200 * 1024**2
MAX_MANIFEST_BYTES = 16 * 1024**2
RGW_PROOF_PAYLOAD_BYTES = 8 * 1024**2
CEPHFS_MARKER_BYTES = 1024**2

RESOURCE_KINDS = (
    "servers",
    "volumes",
    "shares",
    "images",
    "ports",
    "security_groups",
    "floating_ips",
    "keypairs",
    "buckets",
)
RESOURCE_ROLES: dict[str, frozenset[str]] = {
    "servers": frozenset({"builder", "consumer"}),
    "volumes": frozenset(VOLUME_SIZES_GIB),
    "shares": frozenset({"artifacts"}),
    "images": frozenset({"derived"}),
    "ports": frozenset({"builder", "consumer"}),
    "security_groups": frozenset({"ssh"}),
    "floating_ips": frozenset({"builder", "consumer"}),
    "keypairs": frozenset({"key"}),
    "buckets": frozenset({"proof"}),
}
RESOURCE_CAPS = {
    "servers": 2,
    "volumes": 3,
    "shares": 1,
    "images": 1,
    "ports": 2,
    "security_groups": 1,
    "floating_ips": 2,
    "keypairs": 1,
    "buckets": 1,
}
ALLOWED_ROLES = frozenset({"member", "reader"})
REFUSED_ROLES = frozenset({"admin", "manager", "service"})
SERVICE_TYPES = ("compute", "volumev3", "network", "image", "sharev2")
MANILA_MICROVERSION = "2.51"

SSH_USERS = {"builder": "ubuntu", "consumer": "debian"}
CONSUMER_UNIT = "afterglow-native.service"
SHARE_MOUNT = "/mnt/pal-share"
DATA_MOUNT = "/mnt/pal-data"
BUILDER_WORK = "/var/tmp/pal-system"
ARTIFACT_DIR = f"{SHARE_MOUNT}/artifacts"
BUILDER_PACKAGES = (
    "ca-certificates",
    "coreutils",
    "docker.io",
    "docker-buildx",
    "e2fsprogs",
    "grub-pc-bin",
    "grub2-common",
    "pigz",
    "python3",
    "qemu-utils",
    "udev",
    "util-linux",
)
EXPECTED_CHILD_ROLES = ("backend", "frontend", "mariadb", "redis", "worker")
NATIVE_OVERLAY_FILES = frozenset({"Dockerfile", "backend/scripts/native_vm.py", "scripts/export_native_cloud.py"})
NATIVE_OVERLAY_DIRECTORY = "backend/scripts/native-cloud/"
REQUIRED_OVERLAYS = frozenset({"Dockerfile", "scripts/export_native_cloud.py"})

PHASE_DEADLINES = {"prepare": 2400, "build": 10800, "run": 5400, "cleanup": 1800}
MAX_DEADLINE_SECONDS = 14400
POLL_SECONDS = 5.0
INFLIGHT_GRACE_SECONDS = 300
RESULT_MARKER = "PALIMPSEST_RESULT "
NOT_CLAIMED = (
    "protected stage-1 OCI-root execution",
    "untouched upstream OCI image execution",
    "OCI security or isolation parity",
    "public Palimpsest OpenStack backend",
    "original OpenSpec native OCI-root tasks 6/7",
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_PREFIX_RE = re.compile(r"^[0-9a-f]{7,40}$")
_CEPHX_KEY_RE = re.compile(r"^[A-Za-z0-9+/=]{20,128}$")
_CEPH_EXPORT_RE = re.compile(r"^((?:[0-9.]+:[0-9]+,)*[0-9.]+:[0-9]+):(/[A-Za-z0-9/_.-]+)$")
_HOST_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN SSH HOST KEY KEYS-----(.*?)-----END SSH HOST KEY KEYS-----",
    re.DOTALL,
)
_ED25519_KEY_RE = re.compile(r"(?:^|\s)ssh-ed25519\s+([A-Za-z0-9+/]{40,}={0,2})(?=\s|$)", re.MULTILINE)
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(secret|password|passwd|token|access_key|secret_key|key)=([^\s,&;]+)",
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|x-auth-token:|x-subject-token:)\s*\S+")
_DOCKERFILE_TARGET_RE = re.compile(r"^FROM\s+\S+\s+AS\s+native-cloud-vm\s*$", re.IGNORECASE | re.MULTILINE)


# ── Errors and redaction ────────────────────────────────────────────────────


class SystemProofError(RuntimeError):
    """Fail-closed error. Messages are sanitized before they leave the process."""


class SystemProofTimeout(SystemProofError):
    """A bounded phase deadline expired."""


class SystemProofInterrupted(SystemProofError):
    """SIGINT/SIGTERM interrupted the phase; durable receipts remain authoritative."""


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, *values: str | None) -> None:
        for value in values:
            if isinstance(value, str) and len(value) >= 4:
                self._secrets.add(value)

    def scrub(self, text: str) -> str:
        cleaned = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED PRIVATE KEY]", text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            cleaned = cleaned.replace(secret, "[REDACTED]")
        cleaned = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", cleaned)
        return _BEARER_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", cleaned)


REDACTOR = Redactor()


def sanitize(text: str, limit: int = 4000) -> str:
    cleaned = REDACTOR.scrub(text)
    if len(cleaned) > limit:
        cleaned = "…" + cleaned[-limit:]
    return cleaned


def _status_of(exc: BaseException) -> int | None:
    for attribute in ("status_code", "http_status", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def cloud_error(action: str, exc: BaseException) -> SystemProofError:
    """Never include SDK/HTTP bodies: class name and status only."""
    status = _status_of(exc)
    suffix = f" status={status}" if status is not None else ""
    return SystemProofError(f"{action} failed: {type(exc).__name__}{suffix}")


def _is_not_found(exc: BaseException) -> bool:
    return _status_of(exc) == 404 or type(exc).__name__ in {"NotFoundException", "ResourceNotFound"}


# ── Generic helpers ─────────────────────────────────────────────────────────


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_project(value: object) -> str:
    if not isinstance(value, str):
        raise SystemProofError("project identifier must be a string")
    cleaned = value.strip().lower().replace("-", "")
    if not _HEX32_RE.fullmatch(cleaned):
        raise SystemProofError("project identifier is not a 32-digit hexadecimal ID")
    return cleaned


def attr(resource: Any, name: str, default: Any = None) -> Any:
    if isinstance(resource, Mapping):
        value = resource.get(name, default)
        return default if value is None else value
    value = getattr(resource, name, None)
    return default if value is None else value


def _project_of(resource: Any) -> str | None:
    for key in ("project_id", "tenant_id", "owner", "os-vol-tenant-attr:tenant_id"):
        value = attr(resource, key)
        if isinstance(value, str) and value.strip():
            return normalize_project(value)
    return None


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    parent = path.parent
    if parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise SystemProofError(f"refusing symlinked evidence path: {path.name}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=os.fspath(parent))
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        Path(temporary).unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(temporary, path)
    directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    atomic_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_private_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    """Read a regular, non-symlink, owner-only file without following links."""
    try:
        visible = path.lstat()
    except OSError as exc:
        raise SystemProofError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(visible.st_mode):
        raise SystemProofError(f"{label} must be a regular file")
    if visible.st_uid != os.getuid() or visible.st_mode & 0o077:
        raise SystemProofError(f"{label} must be owned by the current user with mode 0600")
    if not 0 < visible.st_size <= max_bytes:
        raise SystemProofError(f"{label} size is out of bounds")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
            raise SystemProofError(f"{label} changed while opening")
        chunks = []
        remaining = opened.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise SystemProofError(f"{label} was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def sha256_regular_file(path: Path, *, label: str, max_bytes: int) -> tuple[str, int]:
    try:
        visible = path.lstat()
    except OSError as exc:
        raise SystemProofError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(visible.st_mode):
        raise SystemProofError(f"{label} must be a regular file")
    if not 0 < visible.st_size <= max_bytes:
        raise SystemProofError(f"{label} size {visible.st_size} is out of bounds")
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino):
            raise SystemProofError(f"{label} changed while opening")
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if total != opened.st_size or after.st_mtime_ns != opened.st_mtime_ns:
            raise SystemProofError(f"{label} changed while hashing")
    finally:
        os.close(fd)
    return digest.hexdigest(), total


class Deadline:
    def __init__(self, seconds: int) -> None:
        if type(seconds) is not int or not 1 <= seconds <= MAX_DEADLINE_SECONDS:
            raise SystemProofError("deadline-seconds is out of bounds")
        self.end = time.monotonic() + seconds

    def remaining(self, action: str) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            raise SystemProofTimeout(f"phase deadline exceeded while {action}")
        return left

    def sleep(self, seconds: float, action: str) -> None:
        time.sleep(min(seconds, self.remaining(action)))


# ── Private profile ─────────────────────────────────────────────────────────


def validate_public_https_url(url: object, label: str) -> str:
    """Public, TLS, DNS-named endpoints only; refuse internal/admin interfaces and IP literals."""
    if not isinstance(url, str):
        raise SystemProofError(f"{label} must be an HTTPS URL")
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise SystemProofError(f"{label} must be an HTTPS URL without user info")
    if parts.query or parts.fragment:
        raise SystemProofError(f"{label} must not carry a query or fragment")
    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SystemProofError(f"{label} must use a public DNS name, not an IP literal")
    labels = host.split(".")
    if (
        len(labels) < 2
        or host in {"localhost"}
        or host.endswith((".local", ".internal", ".svc", ".cluster.local", ".localdomain"))
        or any(part in {"internal", "admin", "int", "mgmt"} or "internal" in part for part in labels)
    ):
        raise SystemProofError(f"{label} looks like an internal/admin endpoint")
    return url.strip()


@dataclass(frozen=True)
class Profile:
    auth_url: str
    application_credential_id: str = field(repr=False)
    application_credential_secret: str = field(repr=False)
    project_id: str
    project_name: str
    user_id: str
    region_name: str
    s3_endpoint: str
    s3_access_key: str = field(repr=False)
    s3_secret_key: str = field(repr=False)
    s3_region: str
    ceph_monitors: tuple[str, ...]
    login_username: str | None
    login_password: str | None = field(repr=False)
    login_domain_name: str | None = None


def load_profile(path: Path) -> Profile:
    raw = read_private_file(path, label="profile", max_bytes=64 * 1024)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SystemProofError("profile is not valid JSON") from exc
    if not isinstance(data, dict):
        raise SystemProofError("profile must be a JSON object")
    # Register every secret-shaped value before any further validation can echo them.
    REDACTOR.add(
        *(
            value
            for key, value in data.items()
            if isinstance(value, str)
            and any(part in key.lower() for part in ("secret", "password", "token", "key", "application_credential"))
        )
    )

    def text(key: str, *, required: bool = True) -> str | None:
        value = data.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip():
            raise SystemProofError(f"profile field {key} must be a non-empty string")
        return value.strip()

    if data.get("schema_version") != PROFILE_SCHEMA:
        raise SystemProofError("profile schema_version must be 1")
    if data.get("auth_type") != "v3applicationcredential":
        raise SystemProofError("profile must use a v3applicationcredential application credential")
    if data.get("interface") != "public":
        raise SystemProofError("profile interface must be public")
    if data.get("verify") is not True or data.get("insecure") not in (None, False) or data.get("cacert"):
        raise SystemProofError("profile must keep default TLS verification enabled")
    forbidden = {"password", "token", "username", "admin_password", "os_password", "os_token"} & set(data)
    if forbidden:
        raise SystemProofError("profile must not contain admin/password/token auth fields")
    project = normalize_project(text("project_id"))
    if project in FORBIDDEN_PROJECT_HEX or project != APPROVED_PROJECT_ID:
        raise SystemProofError("profile project_id is not the approved SYSTEM project")
    if data.get("network_id") != APPROVED_NETWORK_ID:
        raise SystemProofError("profile network_id does not match the approved SYSTEM network")
    if data.get("volume_type") != APPROVED_VOLUME_TYPE or data.get("manila_share_type") != SHARE_TYPE:
        raise SystemProofError("profile volume/share types do not match the approved configuration")
    expires = data.get("expires_at")
    if isinstance(expires, str):
        try:
            expiry = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise SystemProofError("profile expires_at is malformed") from exc
        if expiry <= datetime.now(UTC):
            raise SystemProofError("profile application credential has expired")
    monitors = tuple(item.strip() for item in (text("ceph_monitors") or "").split(",") if item.strip())
    for monitor in monitors:
        host, _, port = monitor.rpartition(":")
        try:
            ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise SystemProofError("profile ceph_monitors must be IPv4:port entries") from exc
        if not port.isdigit():
            raise SystemProofError("profile ceph_monitors must be IPv4:port entries")
    login_values = [data.get(key) for key in ("login_username", "login_password", "login_domain_name")]
    if any(value is not None for value in login_values) and not all(
        isinstance(value, str) and value for value in login_values
    ):
        raise SystemProofError("profile login_username/login_password/login_domain_name must be supplied together")
    if isinstance(login_values[0], str) and login_values[0].strip().lower() in {"admin", "root", "service"}:
        raise SystemProofError("profile login account must be the dedicated member test account")
    region = text("region_name") or "RegionOne"
    return Profile(
        auth_url=validate_public_https_url(text("auth_url"), "profile auth_url"),
        application_credential_id=text("application_credential_id") or "",
        application_credential_secret=text("application_credential_secret") or "",
        project_id=project,
        project_name=text("project_name") or "",
        user_id=normalize_project(text("user_id")),
        region_name=region,
        s3_endpoint=validate_public_https_url(text("s3_endpoint"), "profile s3_endpoint"),
        s3_access_key=text("s3_access_key") or "",
        s3_secret_key=text("s3_secret_key") or "",
        s3_region=text("s3_region", required=False) or "us-east-1",
        ceph_monitors=monitors,
        login_username=login_values[0],
        login_password=login_values[1],
        login_domain_name=login_values[2],
    )


# ── Source bundle ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceBundle:
    path: Path
    sha256: str
    size: int
    manifest: dict[str, Any]
    manifest_sha256: str
    approval_sha256: str = ""

    def manifest_json_bytes(self) -> bytes:
        return (json.dumps(self.manifest, indent=2, sort_keys=True) + "\n").encode()


def _excluded_source_path(name: str) -> bool:
    parts = Path(name).parts
    return (
        any(part == ".git" or part.startswith(".env") for part in parts)
        or name == "afterglow.conf"
        or name == "backend/tests/integration/credentials.toml"
    )


def _safe_member_name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise SystemProofError("source bundle contains an unsafe path")
    normalized = posixpath.normpath(name)
    if normalized in {".", ".."} or normalized.startswith("../") or normalized != name.rstrip("/"):
        raise SystemProofError("source bundle contains a non-normalized path")
    return normalized


def resolve_afterglow_ref(value: str) -> str:
    cleaned = value.strip().lower()
    if not _REF_PREFIX_RE.fullmatch(cleaned) or not APPROVED_AFTERGLOW_REF.startswith(cleaned):
        raise SystemProofError("afterglow-ref must identify the approved ff007ed baseline")
    return APPROVED_AFTERGLOW_REF


def verify_source_bundle(bundle: Path, afterglow_ref: str, *, cleanup_only: bool = False) -> SourceBundle:
    """Verify sidecar manifest, digest, size, file list/hashes and exclusions before any cloud call.

    ``cleanup_only`` (cleanup phase) still requires the manifest and approved ref, but tolerates a bundle
    that was removed after provisioning so owned resources can always be reclaimed; it never provisions.
    """
    ref = resolve_afterglow_ref(afterglow_ref)
    sidecar = Path(f"{bundle}.manifest.json")
    raw_manifest = read_private_file(sidecar, label="source bundle manifest", max_bytes=MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw_manifest)
    except ValueError as exc:
        raise SystemProofError("source bundle manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise SystemProofError("source bundle manifest schema_version must be 1")
    if manifest.get("afterglow_ref") != ref:
        raise SystemProofError("source bundle afterglow_ref is not the approved full baseline SHA")
    expected_sha = manifest.get("bundle_sha256")
    expected_size = manifest.get("bundle_bytes")
    if not isinstance(expected_sha, str) or not _SHA256_RE.fullmatch(expected_sha):
        raise SystemProofError("source bundle manifest bundle_sha256 is invalid")
    if type(expected_size) is not int or not 0 < expected_size <= MAX_BUNDLE_BYTES:
        raise SystemProofError("source bundle size is out of bounds")
    overlays = manifest.get("overlays")
    if not isinstance(overlays, list) or not all(isinstance(item, str) for item in overlays):
        raise SystemProofError("source bundle overlays must be a list of paths")
    for item in overlays:
        if item not in NATIVE_OVERLAY_FILES and not item.startswith(NATIVE_OVERLAY_DIRECTORY):
            raise SystemProofError(f"source bundle overlay outside the native allowlist: {item}")
        if _excluded_source_path(item) or _safe_member_name(item) != item:
            raise SystemProofError(f"source bundle overlay is not allowed: {item}")
    if not REQUIRED_OVERLAYS <= set(overlays):
        raise SystemProofError("source bundle must overlay Dockerfile and scripts/export_native_cloud.py")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SystemProofError("source bundle manifest files must be a list")
    listed: dict[str, dict[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise SystemProofError("source bundle manifest file entries are malformed")
        path = _safe_member_name(item["path"])
        if path in listed or _excluded_source_path(path):
            raise SystemProofError(f"source bundle manifest has a duplicate or excluded path: {path}")
        has_sha = isinstance(item.get("sha256"), str) and _SHA256_RE.fullmatch(item["sha256"]) is not None
        has_link = isinstance(item.get("symlink"), str)
        if has_sha == has_link:
            raise SystemProofError(f"source bundle manifest entry needs exactly one of sha256/symlink: {path}")
        listed[path] = item
    for overlay in overlays:
        if listed.get(overlay, {}).get("origin") != "native-overlay":
            raise SystemProofError(f"source bundle overlay is not recorded as native-overlay: {overlay}")

    if cleanup_only and not os.path.lexists(bundle):
        return SourceBundle(
            path=bundle,
            sha256=expected_sha,
            size=expected_size,
            manifest=manifest,
            manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
        )
    actual_sha, actual_size = sha256_regular_file(bundle, label="source bundle", max_bytes=MAX_BUNDLE_BYTES)
    if actual_sha != expected_sha or actual_size != expected_size:
        raise SystemProofError("source bundle digest/size does not match its manifest")

    seen: set[str] = set()
    dockerfile_has_target = False
    try:
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive:
                name = _safe_member_name(member.name)
                if _excluded_source_path(name):
                    raise SystemProofError(f"source bundle contains an excluded path: {name}")
                if member.isdir():
                    continue
                entry = listed.get(name)
                if entry is None:
                    raise SystemProofError(f"source bundle contains an unlisted path: {name}")
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None or "sha256" not in entry:
                        raise SystemProofError(f"source bundle file entry mismatch: {name}")
                    digest = hashlib.sha256()
                    content_head = b""
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        if name == "Dockerfile" and len(content_head) < 4 * 1024 * 1024:
                            content_head += chunk
                    if digest.hexdigest() != entry["sha256"]:
                        raise SystemProofError(f"source bundle file digest mismatch: {name}")
                    if name == "Dockerfile":
                        dockerfile_has_target = bool(
                            _DOCKERFILE_TARGET_RE.search(content_head.decode("utf-8", errors="replace"))
                        )
                elif member.issym():
                    target = member.linkname
                    if entry.get("symlink") != target or target.startswith("/") or "\\" in target:
                        raise SystemProofError(f"source bundle symlink mismatch: {name}")
                    _safe_member_name(posixpath.normpath(posixpath.join(posixpath.dirname(name), target)))
                else:
                    raise SystemProofError(f"source bundle contains an unsupported entry type: {name}")
                if name in seen:
                    raise SystemProofError(f"source bundle contains a duplicate path: {name}")
                seen.add(name)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SystemProofError(f"source bundle is not a readable gzip tar: {type(exc).__name__}") from exc
    if seen != set(listed):
        raise SystemProofError("source bundle members do not match the manifest file list")
    if not dockerfile_has_target:
        raise SystemProofError("source bundle Dockerfile lacks the native-cloud-vm target")
    return SourceBundle(
        path=bundle,
        sha256=actual_sha,
        size=actual_size,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(raw_manifest).hexdigest(),
    )


SOURCE_APPROVAL_SCHEMA = "palimpsest.system-source-approval.v1"


def verify_source_approval(path: Path, source: SourceBundle) -> SourceBundle:
    """Bind the bundle to a parent-written, owner-private approval receipt.

    The runner never derives approval from the manifest or an allowlist: a separate receipt written after
    source review must name the approved full ref plus the exact bundle and manifest digests.
    """
    resolved = path.absolute()
    if resolved in {source.path.absolute(), Path(f"{source.path}.manifest.json").absolute()}:
        raise SystemProofError("source approval must be a separate receipt, not the bundle or its manifest")
    raw = read_private_file(path, label="source approval receipt", max_bytes=64 * 1024)
    try:
        approval = json.loads(raw)
    except ValueError as exc:
        raise SystemProofError("source approval receipt is not valid JSON") from exc
    if not isinstance(approval, dict) or approval.get("schema") != SOURCE_APPROVAL_SCHEMA:
        raise SystemProofError(f"source approval receipt schema must be {SOURCE_APPROVAL_SCHEMA}")
    if approval.get("afterglow_ref") != APPROVED_AFTERGLOW_REF:
        raise SystemProofError("source approval does not name the approved full Afterglow baseline")
    expected = {
        "bundle_sha256": source.sha256,
        "bundle_bytes": source.size,
        "manifest_sha256": source.manifest_sha256,
    }
    for key, value in expected.items():
        if approval.get(key) != value:
            raise SystemProofError(f"source approval {key} does not match the verified bundle")
    if not isinstance(approval.get("approved_at"), str) or not approval["approved_at"].endswith("Z"):
        raise SystemProofError("source approval approved_at must be a UTC timestamp")
    return SourceBundle(
        path=source.path,
        sha256=source.sha256,
        size=source.size,
        manifest=source.manifest,
        manifest_sha256=source.manifest_sha256,
        approval_sha256=hashlib.sha256(raw).hexdigest(),
    )


# ── Process-wide lock and single active experiment binding ──────────────────


def binding_directory() -> Path:
    """Canonical per-user/project state path; independent of evidence dirs and of $HOME/XDG overrides."""
    import pwd

    return (
        Path(pwd.getpwuid(os.getuid()).pw_dir)
        / ".local"
        / "state"
        / "palimpsest"
        / "openstack-system"
        / (APPROVED_PROJECT_ID)
    )


class SystemBinding:
    """Exclusive process lock plus one durable active owner/evidence/source binding for the SYSTEM project.

    The binding is written before any cloud mutation and removed only after exact-owned cleanup is verified,
    so a second evidence directory can never provision while the first experiment's resources may be live.
    """

    def __init__(self, directory: Path) -> None:
        import fcntl

        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SystemProofError("SYSTEM binding directory must be an owner-only (0700) directory")
        self.directory = directory
        self.active_path = directory / "active.json"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        self._lock_fd = os.open(directory / "lock", flags, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise SystemProofError("another SYSTEM runner process holds the exclusive lock; refusing") from None

    def active(self) -> dict[str, Any] | None:
        if not self.active_path.exists() and not self.active_path.is_symlink():
            return None
        raw = read_private_file(self.active_path, label="active SYSTEM binding", max_bytes=64 * 1024)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SystemProofError("active SYSTEM binding is corrupt; refusing") from exc
        if not isinstance(data, dict) or data.get("schema") != "palimpsest.system-active-binding.v1":
            raise SystemProofError("active SYSTEM binding has an unknown schema; refusing")
        return data

    def check(self, *, evidence: Path, owner_id: str | None, source: SourceBundle) -> None:
        active = self.active()
        if active is None:
            return
        if active.get("evidence") != os.fspath(evidence.resolve()):
            raise SystemProofError(
                "another SYSTEM experiment is active (different evidence directory); clean it up first"
            )
        if owner_id is not None and active.get("owner_id") != owner_id:
            raise SystemProofError("active SYSTEM binding names a different owner_id; refusing")
        if active.get("source") != {"bundle_sha256": source.sha256, "manifest_sha256": source.manifest_sha256}:
            raise SystemProofError("active SYSTEM binding names a different source bundle; refusing")

    def bind(self, *, evidence: Path, owner_id: str, source: SourceBundle) -> None:
        self.check(evidence=evidence, owner_id=owner_id, source=source)
        if self.active() is not None:
            return
        atomic_write_json(
            self.active_path,
            {
                "schema": "palimpsest.system-active-binding.v1",
                "project_id": APPROVED_PROJECT_ID,
                "owner_id": owner_id,
                "evidence": os.fspath(evidence.resolve()),
                "source": {"bundle_sha256": source.sha256, "manifest_sha256": source.manifest_sha256},
                "bound_at": utc_now(),
            },
        )

    def release(self, *, evidence: Path, owner_id: str) -> None:
        active = self.active()
        if active is None:
            return
        if active.get("evidence") != os.fspath(evidence.resolve()) or active.get("owner_id") != owner_id:
            raise SystemProofError("refusing to release a SYSTEM binding owned by another experiment")
        self.active_path.unlink()
        directory = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def close(self) -> None:
        os.close(self._lock_fd)


# ── Owner receipt ───────────────────────────────────────────────────────────


def expected_resource_name(run_name: str, kind: str, role: str) -> str:
    if kind == "keypairs":
        return f"{run_name}-key"
    if kind == "security_groups":
        return f"{run_name}-ssh"
    if kind == "buckets":
        return run_name
    if kind == "floating_ips":
        return f"{run_name}-{role}-fip"
    if kind == "shares":
        return f"{run_name}-share"
    if kind == "images":
        return f"{run_name}-afterglow-native"
    return f"{run_name}-{role}"


def validate_receipt(
    data: object, *, source_ref: str, bundle_sha256: str, manifest_sha256: str | None = None
) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA:
        raise SystemProofError("owner receipt schema must be 1")
    owner = data.get("owner_id")
    if not isinstance(owner, str) or not _UUID_RE.fullmatch(owner):
        raise SystemProofError("owner receipt owner_id must be a canonical lowercase UUID")
    run_name = f"pal-system-{uuid.UUID(owner).hex[:8]}"
    if data.get("run_name") != run_name:
        raise SystemProofError("owner receipt run_name is not bound to owner_id")
    if normalize_project(data.get("project_id")) != APPROVED_PROJECT_ID:
        raise SystemProofError("owner receipt project_id is not the approved SYSTEM project")
    source = data.get("source")
    if not isinstance(source, dict) or source.get("ref") != source_ref or source.get("bundle_sha256") != bundle_sha256:
        raise SystemProofError("owner receipt source ref/bundle does not match; refusing to resume")
    if manifest_sha256 is not None:
        if source.get("manifest_sha256") not in (None, manifest_sha256):
            raise SystemProofError("owner receipt source manifest does not match; refusing to resume")
        source["manifest_sha256"] = manifest_sha256
    resources = data.get("resources")
    if not isinstance(resources, dict) or set(resources) - set(RESOURCE_KINDS):
        raise SystemProofError("owner receipt resources must be a dict of known resource kinds")
    total_gib = 0
    for kind in RESOURCE_KINDS:
        entries = resources.setdefault(kind, [])
        if not isinstance(entries, list) or len(entries) > RESOURCE_CAPS[kind]:
            raise SystemProofError(f"owner receipt {kind} exceeds the approved cap")
        roles: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise SystemProofError(f"owner receipt {kind} entry must be an object")
            role = entry.get("role")
            if role not in RESOURCE_ROLES[kind] or role in roles:
                raise SystemProofError(f"owner receipt {kind} entry has an invalid or duplicate role")
            roles.add(role)
            if not isinstance(entry.get("id"), str) or not entry["id"].strip():
                raise SystemProofError(f"owner receipt {kind} entry lacks an id")
            if entry.get("name") != expected_resource_name(run_name, kind, role):
                raise SystemProofError(f"owner receipt {kind}/{role} name is not the owner-bound name")
            if kind == "volumes":
                if entry.get("size_gib") != VOLUME_SIZES_GIB[role]:
                    raise SystemProofError(f"owner receipt volume {role} size is not the approved size")
                total_gib += entry["size_gib"]
    if total_gib > MAX_TOTAL_VOLUME_GIB:
        raise SystemProofError("owner receipt volumes exceed the 48 GiB cap")
    inflight = data.get("inflight")
    if inflight is not None and not (
        isinstance(inflight, dict) and all(isinstance(inflight.get(key), str) for key in ("kind", "role", "name"))
    ):
        raise SystemProofError("owner receipt inflight marker is malformed")
    if not isinstance(data.setdefault("phases", {}), dict):
        raise SystemProofError("owner receipt phases must be an object")
    data.setdefault("inflight", None)
    data.setdefault("cleanup", None)
    return data


class Receipt:
    """Durable owner receipt; every successful mutation is persisted before the next call."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        # Authenticated test-identity user; keypairs (no project) are owned by user_id.
        self.user_id: str | None = None

    @classmethod
    def create(cls, path: Path, source: SourceBundle) -> Receipt:
        if path.exists() or path.is_symlink():
            raise SystemProofError("owner receipt already exists")
        owner = uuid.uuid4()
        receipt = cls(
            path,
            {
                "schema": RECEIPT_SCHEMA,
                "owner_id": str(owner),
                "run_name": f"pal-system-{owner.hex[:8]}",
                "project_id": APPROVED_PROJECT_ID,
                "source": {
                    "ref": source.manifest["afterglow_ref"],
                    "bundle_sha256": source.sha256,
                    "manifest_sha256": source.manifest_sha256,
                    "approval_sha256": source.approval_sha256,
                },
                "created_at": utc_now(),
                "inflight": None,
                "resources": {kind: [] for kind in RESOURCE_KINDS},
                "phases": {},
                "cleanup": None,
            },
        )
        receipt.save()
        return receipt

    @classmethod
    def load(cls, path: Path, *, source_ref: str, bundle_sha256: str, manifest_sha256: str | None = None) -> Receipt:
        raw = read_private_file(path, label="owner receipt", max_bytes=4 * 1024 * 1024)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SystemProofError("owner receipt is not valid JSON") from exc
        return cls(
            path,
            validate_receipt(data, source_ref=source_ref, bundle_sha256=bundle_sha256, manifest_sha256=manifest_sha256),
        )

    @property
    def owner_id(self) -> str:
        return self.data["owner_id"]

    @property
    def run_name(self) -> str:
        return self.data["run_name"]

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write_json(self.path, self.data)

    def entries(self, kind: str) -> list[dict[str, Any]]:
        return self.data["resources"][kind]

    def find(self, kind: str, role: str) -> dict[str, Any] | None:
        return next((entry for entry in self.entries(kind) if entry["role"] == role), None)

    def require(self, kind: str, role: str) -> dict[str, Any]:
        entry = self.find(kind, role)
        if entry is None:
            raise SystemProofError(f"owner receipt has no {kind}/{role}; run the earlier phase first")
        return entry

    def name(self, kind: str, role: str) -> str:
        return expected_resource_name(self.run_name, kind, role)

    def check_cap(self, kind: str, role: str) -> None:
        if self.data.get("cleanup"):
            raise SystemProofError("owner receipt is closed by verified cleanup; start a new evidence directory")
        if role not in RESOURCE_ROLES[kind]:
            raise SystemProofError(f"role {role} is not approved for {kind}")
        if len(self.entries(kind)) >= RESOURCE_CAPS[kind]:
            raise SystemProofError(f"approved {kind} cap reached")
        if kind == "volumes":
            used = sum(entry["size_gib"] for entry in self.entries("volumes"))
            if used + VOLUME_SIZES_GIB[role] > MAX_TOTAL_VOLUME_GIB:
                raise SystemProofError("approved 48 GiB volume cap would be exceeded")

    def begin(self, kind: str, role: str) -> None:
        if self.data["inflight"] is not None:
            raise SystemProofError("an earlier creation outcome is unresolved; reconcile before creating")
        self.check_cap(kind, role)
        self.data["inflight"] = {"kind": kind, "role": role, "name": self.name(kind, role), "started_at": utc_now()}
        self.save()

    def finish(self, kind: str, entry: dict[str, Any]) -> dict[str, Any]:
        entry = {**entry, "created_at": entry.get("created_at") or utc_now()}
        self.entries(kind).append(entry)
        self.data["inflight"] = None
        self.save()
        return entry

    def clear_inflight(self) -> None:
        self.data["inflight"] = None
        self.save()

    def update(self, kind: str, role: str, **fields: Any) -> dict[str, Any]:
        entry = self.require(kind, role)
        entry.update(fields)
        self.save()
        return entry

    def mark_phase(self, phase: str, status: str, **info: Any) -> None:
        self.data["phases"][phase] = {"status": status, "at": utc_now(), **info}
        self.save()

    def metadata(self, role: str) -> dict[str, str]:
        return {"palimpsest_owner": self.owner_id, "palimpsest_run": self.run_name, "palimpsest_role": role}

    def description(self, role: str) -> str:
        return f"palimpsest-system owner={self.owner_id} run={self.run_name} role={role}"

    def tags(self) -> list[str]:
        return ["palimpsest-system", f"palimpsest-owner:{self.owner_id}"]


# ── Evidence directory ──────────────────────────────────────────────────────


class Evidence:
    def __init__(self, root: Path) -> None:
        root = root.absolute()
        if root.is_symlink():
            raise SystemProofError("evidence directory must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SystemProofError("evidence directory must be an owner-only (0700) directory")
        self.root = root
        self.private = root / "private"
        self.private.mkdir(exist_ok=True, mode=0o700)
        if self.private.is_symlink() or self.private.lstat().st_mode & 0o077:
            raise SystemProofError("evidence private directory must be owner-only")
        self.ssh_dir = self.private / "ssh"
        self.ssh_dir.mkdir(exist_ok=True, mode=0o700)
        self.receipt_path = root / RECEIPT_NAME
        self.ceph_access_path = self.private / "cephfs-access.json"

    def write(self, name: str, payload: Mapping[str, object]) -> None:
        atomic_write_json(self.root / name, payload)

    def write_text(self, name: str, text: str, *, private: bool = False) -> None:
        atomic_write_bytes((self.private if private else self.root) / name, sanitize(text, 2_000_000).encode())

    def known_hosts(self, role: str) -> Path:
        return self.ssh_dir / f"known_hosts_{role}"

    @property
    def private_key(self) -> Path:
        return self.ssh_dir / "id_ed25519"


# ── OpenStack session ───────────────────────────────────────────────────────


class Cloud:
    """Member-only, project-bound OpenStack access through the public interface."""

    def __init__(self, profile: Profile) -> None:
        try:
            from keystoneauth1 import session as ks_session
            from keystoneauth1.identity.v3 import ApplicationCredential
            from openstack.connection import Connection
        except ImportError as exc:
            raise SystemProofError(
                "openstacksdk is required: uv run --with 'openstacksdk==3.3.0' python scripts/run_openstack_system.py"
            ) from exc
        self.profile = profile
        auth = ApplicationCredential(
            auth_url=profile.auth_url,
            application_credential_id=profile.application_credential_id,
            application_credential_secret=profile.application_credential_secret,
        )
        self.session = ks_session.Session(auth=auth, verify=True, timeout=60)
        try:
            access = auth.get_access(self.session)
        except Exception as exc:
            raise cloud_error("Keystone application-credential authentication", exc) from None
        project = normalize_project(access.project_id or "")
        if project in FORBIDDEN_PROJECT_HEX or project != APPROVED_PROJECT_ID:
            raise SystemProofError("authenticated project is not the approved SYSTEM project")
        if normalize_project(access.user_id or "") != profile.user_id:
            raise SystemProofError("authenticated user does not match the dedicated test identity")
        roles = {str(role).lower() for role in (access.role_names or [])}
        if roles & REFUSED_ROLES:
            raise SystemProofError("credential carries admin/manager/service role; refusing (member-only runner)")
        if not roles or not roles <= ALLOWED_ROLES or "member" not in roles:
            raise SystemProofError("credential roles must be exactly member (+reader)")
        self.roles = sorted(roles)
        self.user_id = profile.user_id
        self.project_id = project
        self.endpoints: dict[str, str] = {}
        for service_type in SERVICE_TYPES:
            try:
                endpoint = self.session.get_endpoint(
                    service_type=service_type, interface="public", region_name=profile.region_name
                )
            except Exception as exc:
                raise cloud_error(f"resolving public {service_type} endpoint", exc) from None
            endpoint = validate_public_https_url(endpoint, f"{service_type} public endpoint")
            segments = [part.replace("-", "").lower() for part in urllib.parse.urlsplit(endpoint).path.split("/")]
            project_segments = [part for part in segments if _HEX32_RE.fullmatch(part)]
            if any(part != project for part in project_segments):
                raise SystemProofError(f"{service_type} endpoint is bound to a different project")
            if service_type == "sharev2" and not project_segments:
                # Manila may publish a project-less ``/v2`` catalog URL (optional since 2.60);
                # the explicit project path is accepted at every microversion and keeps calls bound.
                endpoint = endpoint.rstrip("/") + "/" + project
                project_segments = [project]
            if service_type in {"volumev3", "sharev2"} and project not in project_segments:
                raise SystemProofError(f"{service_type} endpoint is not project-scoped")
            self.endpoints[service_type] = endpoint.rstrip("/")
        self.conn = Connection(
            session=self.session,
            region_name=profile.region_name,
            interface="public",
            compute_api_version="2.10",
            connect_retries=0,
            status_code_retries=0,
        )

    def call(self, action: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except SystemProofError:
            raise
        except Exception as exc:
            raise cloud_error(action, exc) from None

    def get_or_none(self, action: str, function: Callable[..., Any], *args: Any) -> Any:
        try:
            return function(*args)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise cloud_error(action, exc) from None

    def listing(self, action: str, function: Callable[..., Any], **filters: Any) -> list[Any]:
        try:
            return list(function(**filters))
        except Exception as exc:
            raise cloud_error(action, exc) from None

    def tag(self, label: str, resource: Any, tags: Sequence[str]) -> bool:
        """Owner tags where Neutron supports them; the owner description remains the binding otherwise."""
        try:
            self.conn.network.set_tags(resource, list(tags))
        except Exception as exc:
            if _status_of(exc) in {400, 404}:
                return False
            raise cloud_error(f"tagging {label}", exc) from None
        return True

    def rest(
        self,
        method: str,
        url: str,
        *,
        action: str,
        expected: Sequence[int] = (200,),
        json_body: object | None = None,
        headers: Mapping[str, str] | None = None,
        data: Any = None,
        allow_404: bool = False,
    ) -> Any:
        try:
            response = self.session.request(
                url,
                method,
                json=json_body,
                data=data,
                headers=dict(headers or {}),
                raise_exc=False,
                connect_retries=0,
                status_code_retries=0,
            )
        except SystemProofError:
            raise
        except Exception as exc:
            raise cloud_error(action, exc) from None
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code not in expected:
            raise SystemProofError(f"{action} failed: HTTP {response.status_code}")
        return response

    # Manila (sharev2) through the authenticated session; the SDK lacks this service here.
    def manila(self, method: str, path: str, *, action: str, **kwargs: Any) -> Any:
        headers = {"X-OpenStack-Manila-API-Version": MANILA_MICROVERSION, "Accept": "application/json"}
        return self.rest(method, self.endpoints["sharev2"] + path, action=action, headers=headers, **kwargs)

    def glance_base(self) -> str:
        base = self.endpoints["image"]
        return base if base.endswith("/v2") else base + "/v2"

    def glance(self, method: str, path: str, *, action: str, **kwargs: Any) -> Any:
        return self.rest(method, self.glance_base() + path, action=action, **kwargs)


def _json(response: Any, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SystemProofError(f"{action} returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise SystemProofError(f"{action} returned a non-object")
    return payload


# ── RGW S3 (stdlib SigV4) ───────────────────────────────────────────────────


def _quote(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(value, safe=safe)


def sigv4_authorization(
    *,
    method: str,
    host: str,
    canonical_uri: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    payload_sha256: str,
    amz_date: str,
    region: str,
    access_key: str,
    secret_key: str,
) -> str:
    """AWS Signature Version 4 Authorization header for the S3 service."""
    all_headers = {key.lower(): " ".join(str(value).split()) for key, value in headers.items()}
    all_headers["host"] = host
    signed = sorted(all_headers)
    canonical_headers = "".join(f"{key}:{all_headers[key]}\n" for key in signed)
    canonical_query = "&".join(f"{_quote(key)}={_quote(value)}" for key, value in sorted(query.items()))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, ";".join(signed), payload_sha256]
    )
    date = amz_date[:8]
    scope = f"{date}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = ("AWS4" + secret_key).encode()
    for part in (date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(signed)}, Signature={signature}"


class S3:
    def __init__(self, profile: Profile) -> None:
        parts = urllib.parse.urlsplit(profile.s3_endpoint)
        self.scheme_host = f"https://{parts.netloc}"
        self.host = parts.netloc
        self.access_key = profile.s3_access_key
        self.secret_key = profile.s3_secret_key
        self.region = profile.s3_region
        self.context = ssl.create_default_context()

    def request(
        self,
        method: str,
        bucket: str,
        key: str = "",
        *,
        query: Mapping[str, str] | None = None,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        action: str,
        max_body: int = 64 * 1024**2,
    ) -> tuple[int, dict[str, str], bytes]:
        query = dict(query or {})
        uri = "/" + _quote(bucket) + ("/" + _quote(key, safe="-_.~/") if key else "")
        payload_sha = hashlib.sha256(body).hexdigest()
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        request_headers = {"x-amz-content-sha256": payload_sha, "x-amz-date": amz_date, **(headers or {})}
        request_headers["Authorization"] = sigv4_authorization(
            method=method,
            host=self.host,
            canonical_uri=uri,
            query=query,
            headers={k: v for k, v in request_headers.items() if k != "Authorization"},
            payload_sha256=payload_sha,
            amz_date=amz_date,
            region=self.region,
            access_key=self.access_key,
            secret_key=self.secret_key,
        )
        url = (
            self.scheme_host
            + uri
            + ("?" + "&".join(f"{_quote(k)}={_quote(v)}" for k, v in query.items()) if query else "")
        )
        request = urllib.request.Request(url, data=body if method in {"PUT", "POST"} else None, method=method)
        for header, value in request_headers.items():
            request.add_header(header, value)
        try:
            with urllib.request.urlopen(request, timeout=120, context=self.context) as response:
                content = response.read(max_body + 1)
                status = response.status
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            content = exc.read(64 * 1024) if method != "HEAD" else b""
        except (urllib.error.URLError, OSError) as exc:
            raise SystemProofError(f"{action} failed: {type(exc).__name__}") from None
        if len(content) > max_body:
            raise SystemProofError(f"{action} response exceeded {max_body} bytes")
        return status, response_headers, content

    def expect(self, expected: Sequence[int], *args: Any, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        status, headers, body = self.request(*args, **kwargs)
        if status not in expected:
            raise SystemProofError(f"{kwargs['action']} failed: HTTP {status}")
        return status, headers, body

    def bucket_tags(self, bucket: str) -> dict[str, str] | None:
        status, _, body = self.request("GET", bucket, query={"tagging": ""}, action="reading RGW bucket tags")
        if status == 404:
            return None
        if status != 200:
            raise SystemProofError(f"reading RGW bucket tags failed: HTTP {status}")
        tags: dict[str, str] = {}
        root = ElementTree.fromstring(body)
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "Tag":
                fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "") for child in element}
                tags[fields.get("Key", "")] = fields.get("Value", "")
        return tags

    def put_bucket_tags(self, bucket: str, tags: Mapping[str, str]) -> None:
        tag_xml = "".join(
            f"<Tag><Key>{_xml_escape(key)}</Key><Value>{_xml_escape(value)}</Value></Tag>"
            for key, value in tags.items()
        )
        body = f"<Tagging><TagSet>{tag_xml}</TagSet></Tagging>".encode()
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        self.expect(
            (200, 204),
            "PUT",
            bucket,
            query={"tagging": ""},
            body=body,
            headers={"Content-MD5": md5, "Content-Type": "application/xml"},
            action="tagging RGW bucket",
        )

    def list_keys(self, bucket: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        for _ in range(100):
            query = {"list-type": "2", "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            _, _, body = self.expect((200,), "GET", bucket, query=query, action="listing RGW bucket")
            root = ElementTree.fromstring(body)
            token = None
            truncated = False
            for element in root:
                tag = element.tag.rsplit("}", 1)[-1]
                if tag == "Contents":
                    for child in element:
                        if child.tag.rsplit("}", 1)[-1] == "Key":
                            keys.append(child.text or "")
                elif tag == "IsTruncated":
                    truncated = (element.text or "").lower() == "true"
                elif tag == "NextContinuationToken":
                    token = element.text
            if not truncated:
                return keys
        raise SystemProofError("RGW bucket listing exceeded the bounded page count")


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── SSH ─────────────────────────────────────────────────────────────────────


def _command_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}


def run_local(argv: Sequence[str], *, timeout: float, action: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(argv), input=stdin, capture_output=True, check=False, env=_command_env(), timeout=max(1.0, timeout)
        )
    except subprocess.TimeoutExpired:
        raise SystemProofTimeout(f"{action} timed out") from None
    except OSError as exc:
        raise SystemProofError(f"{action} could not start {argv[0]}: {type(exc).__name__}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise SystemProofError(f"{action} failed (exit {result.returncode}): {sanitize(detail, 3000)}")
    return result


def ed25519_fingerprint(key_b64: str) -> str:
    try:
        raw = base64.b64decode(key_b64.encode("ascii"), validate=True)
    except ValueError as exc:
        raise SystemProofError("invalid base64 ssh-ed25519 public key") from exc
    if not raw.startswith(b"\x00\x00\x00\x0bssh-ed25519"):
        raise SystemProofError("public key is not ssh-ed25519")
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def console_ed25519_host_key(console_text: str) -> str | None:
    """Return the single ED25519 host key printed by cloud-init between its host-key markers."""
    keys: set[str] = set()
    for block in _HOST_KEY_BLOCK_RE.findall(console_text):
        keys.update(_ED25519_KEY_RE.findall(block))
    if not keys:
        return None
    if len(keys) != 1:
        raise SystemProofError("Nova console reported conflicting ED25519 host keys")
    key = keys.pop()
    ed25519_fingerprint(key)
    return key


def verify_keyscan(address: str, scan_output: str, console_key: str) -> str:
    scanned: list[str] = []
    for line in scan_output.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and not parts[0].startswith("#") and parts[1] == "ssh-ed25519":
            if address not in parts[0].split(","):
                continue
            scanned.append(parts[2])
    if len(scanned) != 1:
        raise SystemProofError("ssh-keyscan did not return exactly one ED25519 key for the target")
    if scanned[0] != console_key:
        raise SystemProofError(
            f"scanned host key {ed25519_fingerprint(scanned[0])} does not match the Nova console key "
            f"{ed25519_fingerprint(console_key)}"
        )
    return f"{address} ssh-ed25519 {console_key}\n"


class Ssh:
    def __init__(self, evidence: Evidence, role: str, address: str, deadline: Deadline) -> None:
        self.evidence = evidence
        self.role = role
        self.address = str(ipaddress.IPv4Address(address))
        self.user = SSH_USERS[role]
        self.deadline = deadline

    def options(self) -> list[str]:
        return [
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.evidence.known_hosts(self.role)}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-i",
            os.fspath(self.evidence.private_key),
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "LogLevel=ERROR",
        ]

    @property
    def target(self) -> str:
        return f"{self.user}@{self.address}"

    def run(
        self, command: str, *, action: str, stdin: bytes = b"", timeout: float = 600, check: bool = True
    ) -> subprocess.CompletedProcess:
        limit = min(timeout, self.deadline.remaining(action))
        argv = ["ssh", *self.options(), self.target, command]
        if check:
            return run_local(argv, timeout=limit, action=action, stdin=stdin)
        try:
            return subprocess.run(
                argv, input=stdin, capture_output=True, check=False, env=_command_env(), timeout=max(1.0, limit)
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 255, b"", b"timeout")

    def python(
        self,
        script: str,
        config: Mapping[str, object],
        *,
        action: str,
        interpreter: str = "python3",
        timeout: float = 900,
    ) -> dict[str, Any]:
        """Run a root Python script; config (possibly secret) travels on stdin, never in argv."""
        command = shlex.join(["sudo", "-n", interpreter, "-c", script])
        result = self.run(command, action=action, stdin=(json.dumps(config) + "\n").encode(), timeout=timeout)
        return parse_result(result.stdout, action)

    def bash(self, script: str, *, action: str, timeout: float = 3600) -> dict[str, Any]:
        result = self.run(shlex.join(["sudo", "-n", "bash", "-c", script]), action=action, timeout=timeout)
        return parse_result(result.stdout, action)

    def upload(self, local: Path, remote: str, *, action: str) -> None:
        argv = ["scp", "-B", "-q", *self.options(), os.fspath(local), f"{self.target}:{remote}"]
        run_local(argv, timeout=min(1800, self.deadline.remaining(action)), action=action)

    def boot_id(self) -> str | None:
        result = self.run("cat /proc/sys/kernel/random/boot_id", action="reading boot id", timeout=30, check=False)
        value = result.stdout.decode(errors="replace").strip()
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f-]{36}", value) else None


def parse_result(stdout: bytes, action: str) -> dict[str, Any]:
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                payload = json.loads(line[len(RESULT_MARKER) :])
            except ValueError as exc:
                raise SystemProofError(f"{action} returned a malformed result") from exc
            if isinstance(payload, dict):
                return payload
    raise SystemProofError(f"{action} produced no result record")


def ensure_ssh_key(evidence: Evidence, receipt: Receipt) -> str:
    key = evidence.private_key
    public = key.with_name("id_ed25519.pub")
    if not key.exists():
        if receipt.find("keypairs", "key") is not None:
            raise SystemProofError("Nova keypair exists in the receipt but the private SSH key is missing")
        run_local(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", receipt.run_name, "-f", os.fspath(key)],
            timeout=30,
            action="generating owned SSH key",
        )
    read_private_file(key, label="SSH private key", max_bytes=16 * 1024)
    text = public.read_text(encoding="utf-8").strip()
    if not text.startswith("ssh-ed25519 "):
        raise SystemProofError("owned SSH public key is not ssh-ed25519")
    return text


def ssh_public_key_digest(public_key: object) -> str:
    if not isinstance(public_key, str):
        raise SystemProofError("SSH public key is missing")
    fields = public_key.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise SystemProofError("SSH public key is not ssh-ed25519")
    ed25519_fingerprint(fields[1])
    return hashlib.sha256(" ".join(fields[:2]).encode("ascii")).hexdigest()


# ── Ownership checks ────────────────────────────────────────────────────────


def _metadata_matches(resource: Any, receipt: Receipt, role: str) -> bool:
    metadata = attr(resource, "metadata", {}) or attr(resource, "properties", {}) or {}
    if not isinstance(metadata, Mapping):
        return False
    return all(metadata.get(key) == value for key, value in receipt.metadata(role).items())


def _project_ok(resource: Any, *, required: bool) -> bool:
    project = _project_of(resource)
    if project is None:
        return not required
    return project == APPROVED_PROJECT_ID


def owned(kind: str, entry: Mapping[str, Any], resource: Any, receipt: Receipt) -> str | None:
    """Return None when ``resource`` is exactly the receipt-owned object, else the refusal reason."""
    role = entry["role"]
    if kind != "keypairs" and str(attr(resource, "id", "")) != entry["id"]:
        return "id mismatch"
    if kind == "floating_ips":
        address = attr(resource, "floating_ip_address")
        if address in PROTECTED_FLOATING_IPS:
            return "protected production floating IP"
        if attr(resource, "description") != receipt.description(role):
            return "description owner mismatch"
        if attr(resource, "floating_network_id") != APPROVED_EXTERNAL_NETWORK_ID:
            return "floating network mismatch"
        return None if _project_ok(resource, required=True) else "project mismatch"
    if attr(resource, "name") != entry["name"]:
        return "name mismatch"
    if kind == "keypairs":
        user = attr(resource, "user_id")
        if receipt.user_id is None or not isinstance(user, str) or normalize_project(user) != receipt.user_id:
            return "keypair user mismatch"
        expected = receipt.data.get("ssh_public_key_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return "keypair public key ownership digest missing"
        try:
            actual = ssh_public_key_digest(attr(resource, "public_key"))
        except (SystemProofError, UnicodeError):
            return "keypair public key invalid"
        return None if actual == expected else "keypair public key mismatch"
    if kind in {"servers", "volumes", "shares"}:
        if not _metadata_matches(resource, receipt, role):
            return "metadata owner mismatch"
        return None if _project_ok(resource, required=kind != "volumes") else "project mismatch"
    if kind in {"ports", "security_groups"}:
        if attr(resource, "description") != receipt.description(role):
            return "description owner mismatch"
        if attr(resource, "name") == "default":
            return "default security group"
        return None if _project_ok(resource, required=True) else "project mismatch"
    if kind == "images":
        for key, value in receipt.metadata(role).items():
            if attr(resource, key) != value:
                return "image owner property mismatch"
        return (
            None
            if normalize_project(attr(resource, "owner", "") or "0" * 32) == APPROVED_PROJECT_ID
            else ("image owner project mismatch")
        )
    return "unsupported resource kind"


def ports_bound_to_security_group(ports: Sequence[Any], security_group_id: str) -> list[str]:
    """Client-side SG binding filter; server-side Neutron filters have returned foreign ports here.

    Any port carrying the SG blocks deletion, whatever project the API reports for it.
    """
    return [
        str(attr(port, "id", "")) for port in ports if security_group_id in (attr(port, "security_group_ids", []) or [])
    ]


def port_ipv4(port: Any) -> str:
    for item in attr(port, "fixed_ips", []) or []:
        address = item.get("ip_address") if isinstance(item, Mapping) else None
        try:
            parsed = ipaddress.ip_address(address or "")
        except ValueError:
            continue
        if isinstance(parsed, ipaddress.IPv4Address):
            return str(parsed)
    raise SystemProofError("owned port has no IPv4 address")


# ── Runner ──────────────────────────────────────────────────────────────────


class Runner:
    def __init__(
        self,
        profile: Profile,
        evidence: Evidence,
        source: SourceBundle,
        phase: str,
        deadline: Deadline,
        binding: SystemBinding,
    ):
        self.profile = profile
        self.evidence = evidence
        self.source = source
        self.phase = phase
        self.deadline = deadline
        self.binding = binding
        self.cloud: Cloud | None = None
        self.s3 = S3(profile)
        self.receipt = self._open_receipt()

    # ── setup ────────────────────────────────────────────────────────────

    def _open_receipt(self) -> Receipt:
        path = self.evidence.receipt_path
        ref = self.source.manifest["afterglow_ref"]
        # Refuse before touching this evidence directory if another experiment is still bound.
        self.binding.check(evidence=self.evidence.root, owner_id=None, source=self.source)
        if path.exists() or path.is_symlink():
            receipt = Receipt.load(
                path, source_ref=ref, bundle_sha256=self.source.sha256, manifest_sha256=self.source.manifest_sha256
            )
        elif self.phase in {"prepare", "all"}:
            receipt = Receipt.create(path, self.source)
        else:
            raise SystemProofError("owner receipt is absent; run prepare first")
        if receipt.data.get("cleanup"):
            if self.phase != "cleanup":
                raise SystemProofError("owner receipt is closed by verified cleanup; use a new evidence directory")
            self.binding.release(evidence=self.evidence.root, owner_id=receipt.owner_id)
        else:
            # Durable single-active binding precedes every cloud mutation of this experiment.
            self.binding.bind(evidence=self.evidence.root, owner_id=receipt.owner_id, source=self.source)
        receipt.data["source"].setdefault("approval_sha256", self.source.approval_sha256)
        receipt.user_id = self.profile.user_id
        self.evidence.write(
            "source-bundle-verification.json",
            {
                "afterglow_ref": ref,
                "bundle_sha256": self.source.sha256,
                "bundle_bytes": self.source.size,
                "manifest_sha256": self.source.manifest_sha256,
                "approval_sha256": self.source.approval_sha256,
                "overlays": self.source.manifest["overlays"],
                "excluded": self.source.manifest.get("excluded", []),
                "file_count": len(self.source.manifest["files"]),
                "verified_at": utc_now(),
            },
        )
        return receipt

    def connect(self, *, reconcile: bool = True) -> Cloud:
        if self.cloud is None:
            self.cloud = Cloud(self.profile)
            if reconcile:
                self.reconcile_inflight()
        return self.cloud

    @property
    def conn(self) -> Any:
        return self.connect().conn

    # ── inflight discovery ───────────────────────────────────────────────

    def _discover(self, kind: str, role: str, name: str) -> list[dict[str, Any]]:
        cloud = self.connect() if self.cloud is None else self.cloud
        conn = cloud.conn
        entry = {"id": "", "name": name, "role": role}
        found: list[dict[str, Any]] = []
        if kind == "servers":
            candidates = cloud.listing("listing servers", conn.compute.servers, name=f"^{re.escape(name)}$")
        elif kind == "volumes":
            candidates = cloud.listing("listing volumes", conn.block_storage.volumes, name=name)
        elif kind == "ports":
            candidates = cloud.listing("listing ports", conn.network.ports, name=name, project_id=APPROVED_PROJECT_ID)
        elif kind == "security_groups":
            candidates = cloud.listing(
                "listing security groups", conn.network.security_groups, name=name, project_id=APPROVED_PROJECT_ID
            )
        elif kind == "floating_ips":
            candidates = cloud.listing(
                "listing floating IPs",
                conn.network.ips,
                project_id=APPROVED_PROJECT_ID,
                floating_network_id=APPROVED_EXTERNAL_NETWORK_ID,
            )
        elif kind == "keypairs":
            keypair = cloud.get_or_none("reading keypair", conn.compute.get_keypair, name)
            candidates = [keypair] if keypair is not None else []
        elif kind == "images":
            response = cloud.glance(
                "GET",
                "/images?" + urllib.parse.urlencode({"name": name, "owner": APPROVED_PROJECT_ID}),
                action="listing images",
            )
            candidates = _json(response, "listing images").get("images", [])
        elif kind == "shares":
            response = cloud.manila(
                "GET", "/shares/detail?" + urllib.parse.urlencode({"name": name}), action="listing shares"
            )
            candidates = _json(response, "listing shares").get("shares", [])
        elif kind == "buckets":
            status, _, _ = self.s3.request("HEAD", name, action="probing RGW bucket")
            if status == 200:
                tags = self.s3.bucket_tags(name) or {}
                if tags.get("palimpsest-owner") != self.receipt.owner_id:
                    # Never adopt or retag: an untagged or foreign-tagged bucket needs manual review.
                    raise SystemProofError("RGW bucket with the owner-bound name lacks this owner's tag; refusing")
                return [{"id": name, "name": name, "role": role, "objects": []}]
            if status == 404:
                return []
            raise SystemProofError(f"RGW bucket presence could not be verified: HTTP {status}")
        else:
            raise SystemProofError(f"cannot discover {kind}")
        for candidate in candidates:
            identifier = name if kind == "keypairs" else str(attr(candidate, "id", ""))
            reason = owned(kind, {**entry, "id": identifier}, candidate, self.receipt)
            if kind != "floating_ips" and attr(candidate, "name") != name:
                continue
            if kind == "keypairs" and reason is not None:
                raise SystemProofError(f"owner-bound keypair discovery refused: {reason}")
            if reason is None:
                extra: dict[str, Any] = {}
                if kind == "volumes":
                    extra["size_gib"] = attr(candidate, "size")
                if kind == "floating_ips":
                    extra["address"] = attr(candidate, "floating_ip_address")
                found.append({"id": identifier, "name": name, "role": role, **extra})
        return found

    def _discover_any(self, kind: str, role: str, name: str) -> list[dict[str, Any]]:
        if kind == "share_access":
            if name != self.receipt.run_name or role != "artifacts":
                raise SystemProofError("share access marker is not owner-bound")
            share = self.receipt.require("shares", role)
            current = self._share(share["id"])
            if current is None:
                return []
            if reason := owned("shares", share, current, self.receipt):
                raise SystemProofError(f"share access discovery refused: {reason}")
            rules = self._share_access_rules(share["id"])
            matching = [rule for rule in rules if rule.get("access_to") == name]
            if any(
                rule.get("access_type") != "cephx" or rule.get("share_id") not in (None, share["id"])
                for rule in matching
            ):
                raise SystemProofError("share access discovery found mismatched type/share")
            return [{"id": rule["id"], "access_to": name} for rule in matching]
        if kind not in RESOURCE_KINDS or name != self.receipt.name(kind, role):
            raise SystemProofError("owner receipt inflight marker does not name an owner-bound resource")
        return self._discover(kind, role, name)

    def reconcile_inflight(self) -> None:
        """Resolve an ambiguous create outcome by exact owner discovery; never assume it did not happen."""
        inflight = self.receipt.data["inflight"]
        if inflight is None:
            return
        kind, role, name = inflight["kind"], inflight["role"], inflight["name"]
        matches = self._discover_any(kind, role, name)
        if not matches:
            # A timed-out request may still be applied server-side: wait out a grace window and look again.
            try:
                started = datetime.strptime(str(inflight.get("started_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                age = (datetime.now(UTC) - started).total_seconds()
            except ValueError:
                age = 0
            if age < INFLIGHT_GRACE_SECONDS:
                self.deadline.sleep(INFLIGHT_GRACE_SECONDS - age, f"waiting out ambiguous {kind} creation")
            matches = self._discover_any(kind, role, name)
        if len(matches) > 1:
            raise SystemProofError(f"multiple owner-bound {kind} match the unresolved creation; refusing")
        if matches and kind == "share_access":
            self.receipt.require("shares", role).setdefault("access", []).append(matches[0])
            self.receipt.clear_inflight()
        elif matches:
            self.receipt.finish(kind, matches[0])
        else:
            # Keep a durable record so cleanup re-checks for a late-applied creation.
            self.receipt.data.setdefault("cleared_inflight", []).append({**inflight, "cleared_at": utc_now()})
            self.receipt.clear_inflight()

    # ── tracked creation ─────────────────────────────────────────────────

    def create(self, kind: str, role: str, function: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        existing = self.receipt.find(kind, role)
        if existing is not None:
            return existing
        self.deadline.remaining(f"creating {kind}/{role}")
        name = self.receipt.name(kind, role)
        self.receipt.begin(kind, role)
        fields = function(name)
        if not isinstance(fields.get("id"), str) or not fields["id"]:
            raise SystemProofError(f"created {kind}/{role} returned no id")
        return self.receipt.finish(kind, {"name": name, "role": role, **fields})

    def wait_status(
        self,
        label: str,
        getter: Callable[[], Any],
        *,
        ready: set[str],
        failed: set[str],
        interval: float = POLL_SECONDS,
    ) -> Any:
        while True:
            current = getter()
            if current is None:
                raise SystemProofError(f"{label} disappeared while waiting")
            status = str(attr(current, "status", "")).lower()
            if status in ready:
                return current
            if status in failed:
                raise SystemProofError(f"{label} entered status {status}")
            self.deadline.sleep(interval, f"waiting for {label}")

    # ── preflight ────────────────────────────────────────────────────────

    def preflight(self) -> dict[str, Any]:
        cloud = self.connect()
        conn = cloud.conn
        flavor = cloud.get_or_none("reading flavor", conn.compute.get_flavor, APPROVED_FLAVOR_ID)
        if (
            flavor is None
            or attr(flavor, "name") != APPROVED_FLAVOR_NAME
            or attr(flavor, "vcpus") != APPROVED_FLAVOR_VCPUS
            or attr(flavor, "ram") != APPROVED_FLAVOR_RAM_MIB
        ):
            raise SystemProofError("approved cpu.2c_8g flavor pin does not match")
        image = cloud.glance("GET", f"/images/{BUILDER_IMAGE_ID}", action="reading builder image", allow_404=True)
        if image is None:
            raise SystemProofError("approved builder image is not visible")
        image = _json(image, "reading builder image")
        virtual = image.get("virtual_size") or image.get("size")
        if (
            image.get("status") != "active"
            or image.get("disk_format") != "raw"
            or image.get("container_format") != "bare"
            or image.get("os_hash_algo") != "sha512"
            or image.get("os_hash_value") != BUILDER_IMAGE_SHA512
            or type(virtual) is not int
            or virtual > BUILDER_IMAGE_MAX_VIRTUAL_BYTES
        ):
            raise SystemProofError("approved builder image status/format/SHA-512/size pin does not match")
        network = cloud.get_or_none("reading network", conn.network.get_network, APPROVED_NETWORK_ID)
        if network is None or _project_of(network) != APPROVED_PROJECT_ID or attr(network, "is_router_external"):
            raise SystemProofError("approved SYSTEM tenant network does not match")
        external = cloud.get_or_none("reading external network", conn.network.get_network, APPROVED_EXTERNAL_NETWORK_ID)
        if external is None or not attr(external, "is_router_external"):
            raise SystemProofError("approved external network is not router:external")
        types = {attr(item, "name") for item in cloud.listing("listing volume types", conn.block_storage.types)}
        if APPROVED_VOLUME_TYPE not in types:
            raise SystemProofError("approved ceph_hdd volume type is not available")
        share_types = _json(cloud.manila("GET", "/types", action="listing share types"), "listing share types")
        if SHARE_TYPE not in {item.get("name") for item in share_types.get("share_types", [])}:
            raise SystemProofError("approved cephfs share type is not available")
        return {
            "roles": cloud.roles,
            "endpoints": {key: urllib.parse.urlsplit(value).hostname for key, value in cloud.endpoints.items()},
            "builder_image": {"id": BUILDER_IMAGE_ID, "sha512": BUILDER_IMAGE_SHA512, "virtual_size": virtual},
            "flavor": APPROVED_FLAVOR_ID,
            "network": APPROVED_NETWORK_ID,
            "external_network": APPROVED_EXTERNAL_NETWORK_ID,
        }

    # ── shared resources ─────────────────────────────────────────────────

    def ensure_keypair(self, public_key: str) -> dict[str, Any]:
        cloud = self.connect()
        expected = ssh_public_key_digest(public_key)
        recorded = self.receipt.data.get("ssh_public_key_sha256")
        if recorded is not None and recorded != expected:
            raise SystemProofError("SSH public key differs from the durable ownership digest")
        if recorded is None and (
            self.receipt.find("keypairs", "key") is not None or self.receipt.data["inflight"] is not None
        ):
            raise SystemProofError("existing keypair outcome lacks a durable public-key ownership digest")
        self.receipt.data["ssh_public_key_sha256"] = expected
        self.receipt.save()

        def create(name: str) -> dict[str, Any]:
            keypair = cloud.call(
                "creating keypair", cloud.conn.compute.create_keypair, name=name, public_key=public_key
            )
            if attr(keypair, "name") != name:
                raise SystemProofError("created keypair name mismatch")
            return {"id": name, "fingerprint": attr(keypair, "fingerprint")}

        entry = self.create("keypairs", "key", create)
        keypair = cloud.get_or_none("reading keypair", cloud.conn.compute.get_keypair, entry["name"])
        if keypair is None or owned("keypairs", entry, keypair, self.receipt) is not None:
            raise SystemProofError("owned keypair is missing or not owned by the test identity")
        if str(attr(keypair, "public_key", "")).split()[:2] != public_key.split()[:2]:
            raise SystemProofError("owned keypair public key does not match the private SSH key")
        return entry

    def ensure_security_group(self) -> dict[str, Any]:
        cloud = self.connect()
        conn = cloud.conn

        def create(name: str) -> dict[str, Any]:
            group = cloud.call(
                "creating security group",
                conn.network.create_security_group,
                name=name,
                description=self.receipt.description("ssh"),
            )
            return {"id": str(attr(group, "id", ""))}

        entry = self.create("security_groups", "ssh", create)
        group = cloud.get_or_none("reading security group", conn.network.get_security_group, entry["id"])
        if group is None or (reason := owned("security_groups", entry, group, self.receipt)):
            raise SystemProofError(f"owned security group check failed: {reason if group else 'missing'}")
        cloud.tag("security group", group, self.receipt.tags())
        rules = attr(group, "security_group_rules", []) or []
        ingress = [rule for rule in rules if attr(rule, "direction") == "ingress"]
        wanted = {"protocol": "tcp", "port_range_min": 22, "port_range_max": 22, "remote_ip_prefix": SSH_SOURCE_CIDR}
        if any(any(attr(rule, key) != value for key, value in wanted.items()) for rule in ingress):
            raise SystemProofError("owned security group has unexpected ingress; refusing broad access")
        if not ingress:
            cloud.call(
                "creating SSH ingress rule",
                conn.network.create_security_group_rule,
                security_group_id=entry["id"],
                direction="ingress",
                ethertype="IPv4",
                description=self.receipt.description("ssh"),
                **wanted,
            )
        self.verify_security_group()
        return entry

    def verify_security_group(self) -> None:
        """The owned SG must carry exactly one ingress rule: IPv4 TCP/22 from the approved client /32."""
        cloud = self.connect()
        entry = self.receipt.require("security_groups", "ssh")
        group = cloud.get_or_none("reading security group", cloud.conn.network.get_security_group, entry["id"])
        if group is None or (reason := owned("security_groups", entry, group, self.receipt)):
            raise SystemProofError(f"owned security group check failed: {reason if group else 'missing'}")
        ingress = [
            rule for rule in attr(group, "security_group_rules", []) or [] if attr(rule, "direction") == "ingress"
        ]
        wanted = {
            "ethertype": "IPv4",
            "protocol": "tcp",
            "port_range_min": 22,
            "port_range_max": 22,
            "remote_ip_prefix": SSH_SOURCE_CIDR,
            "remote_group_id": None,
        }
        if len(ingress) != 1 or any(attr(ingress[0], key) != value for key, value in wanted.items()):
            raise SystemProofError("owned security group ingress is not exactly TCP/22 from the approved /32")

    def verify_ingress(self, role: str) -> None:
        """Revalidated before every SSH connection or transfer: SG rule set and the port's SG binding."""
        self.verify_security_group()
        cloud = self.connect()
        group = self.receipt.require("security_groups", "ssh")
        port_entry = self.receipt.require("ports", role)
        port = cloud.get_or_none("reading port", cloud.conn.network.get_port, port_entry["id"])
        if port is None or (reason := owned("ports", port_entry, port, self.receipt)):
            raise SystemProofError(f"owned {role} port check failed: {reason if port else 'missing'}")
        if sorted(attr(port, "security_group_ids", []) or []) != self.expected_port_groups(group["id"]):
            raise SystemProofError(f"owned {role} port security groups differ from the owned/user-approved set")
        if attr(port, "is_port_security_enabled") is not True:
            raise SystemProofError(f"owned {role} port security disabled or not verified enabled")

    def expected_port_groups(self, owned_group_id: str) -> list[str]:
        """Owned SSH group plus any exact project security group the user approved on the command line."""
        cloud = self.connect()
        for group_id in USER_PORT_SECURITY_GROUPS:
            extra = cloud.get_or_none(
                "reading user-approved security group", cloud.conn.network.get_security_group, group_id
            )
            if extra is None or _project_of(extra) != APPROVED_PROJECT_ID or group_id == owned_group_id:
                raise SystemProofError("user-approved port security group is missing, foreign or the owned group")
        return sorted({owned_group_id, *USER_PORT_SECURITY_GROUPS})

    def ensure_port(self, role: str) -> tuple[dict[str, Any], str]:
        cloud = self.connect()
        conn = cloud.conn
        group = self.receipt.require("security_groups", "ssh")

        def create(name: str) -> dict[str, Any]:
            port = cloud.call(
                f"creating {role} port",
                conn.network.create_port,
                name=name,
                network_id=APPROVED_NETWORK_ID,
                security_group_ids=self.expected_port_groups(group["id"]),
                description=self.receipt.description(role),
            )
            return {"id": str(attr(port, "id", ""))}

        entry = self.create("ports", role, create)
        port = cloud.get_or_none("reading port", conn.network.get_port, entry["id"])
        if port is None or (reason := owned("ports", entry, port, self.receipt)):
            raise SystemProofError(f"owned {role} port check failed: {reason if port else 'missing'}")
        if attr(port, "network_id") != APPROVED_NETWORK_ID or sorted(
            attr(port, "security_group_ids", []) or []
        ) != self.expected_port_groups(group["id"]):
            raise SystemProofError(f"owned {role} port network/security group binding mismatch")
        cloud.tag("port", port, self.receipt.tags())
        return entry, port_ipv4(port)

    def ensure_floating_ip(self, role: str, port_id: str) -> str:
        cloud = self.connect()
        conn = cloud.conn

        def create(_name: str) -> dict[str, Any]:
            address = cloud.call(
                f"creating {role} floating IP",
                conn.network.create_ip,
                floating_network_id=APPROVED_EXTERNAL_NETWORK_ID,
                port_id=port_id,
                description=self.receipt.description(role),
            )
            return {"id": str(attr(address, "id", "")), "address": attr(address, "floating_ip_address")}

        entry = self.create("floating_ips", role, create)
        address = cloud.get_or_none("reading floating IP", conn.network.get_ip, entry["id"])
        if address is None or (reason := owned("floating_ips", entry, address, self.receipt)):
            raise SystemProofError(f"owned {role} floating IP check failed: {reason if address else 'missing'}")
        if attr(address, "port_id") != port_id:
            raise SystemProofError(f"owned {role} floating IP is not associated with the owned port")
        cloud.tag("floating IP", address, self.receipt.tags())
        public = str(ipaddress.IPv4Address(attr(address, "floating_ip_address")))
        if public in PROTECTED_FLOATING_IPS:
            raise SystemProofError("refusing protected production floating IP")
        if entry.get("address") != public:
            self.receipt.update("floating_ips", role, address=public)
        return public

    def ensure_volume(self, role: str, *, image_id: str | None) -> dict[str, Any]:
        cloud = self.connect()
        conn = cloud.conn

        def create(name: str) -> dict[str, Any]:
            options: dict[str, Any] = {
                "name": name,
                "size": VOLUME_SIZES_GIB[role],
                "volume_type": APPROVED_VOLUME_TYPE,
                "metadata": self.receipt.metadata(role),
                "description": self.receipt.description(role),
            }
            if image_id is not None:
                options["image_id"] = image_id
            volume = cloud.call(f"creating {role} volume", conn.block_storage.create_volume, **options)
            return {"id": str(attr(volume, "id", "")), "size_gib": VOLUME_SIZES_GIB[role], "image_id": image_id}

        entry = self.create("volumes", role, create)
        volume = self.wait_status(
            f"{role} volume",
            lambda: cloud.get_or_none("reading volume", conn.block_storage.get_volume, entry["id"]),
            ready={"available", "in-use"},
            failed={"error", "error_restoring", "error_extending", "error_deleting"},
        )
        if reason := owned("volumes", entry, volume, self.receipt):
            raise SystemProofError(f"owned {role} volume check failed: {reason}")
        if attr(volume, "size") != VOLUME_SIZES_GIB[role] or attr(volume, "volume_type") != APPROVED_VOLUME_TYPE:
            raise SystemProofError(f"owned {role} volume size/type mismatch")
        return entry

    def ensure_server(
        self, role: str, *, port_id: str, volumes: Sequence[str], user_data: str | None = None
    ) -> dict[str, Any]:
        cloud = self.connect()
        conn = cloud.conn
        keypair = self.receipt.require("keypairs", "key")

        def create(name: str) -> dict[str, Any]:
            mappings = [
                {
                    "uuid": volume_id,
                    "source_type": "volume",
                    "destination_type": "volume",
                    "boot_index": 0 if index == 0 else -1,
                    "delete_on_termination": False,
                }
                for index, volume_id in enumerate(volumes)
            ]
            options: dict[str, Any] = {
                "name": name,
                "flavor_id": APPROVED_FLAVOR_ID,
                "key_name": keypair["name"],
                "networks": [{"port": port_id}],
                "block_device_mapping_v2": mappings,
                "metadata": self.receipt.metadata(role),
                "config_drive": True,
            }
            if user_data is not None:
                options["user_data"] = base64.b64encode(user_data.encode()).decode()
            server = cloud.call(f"creating {role} server", conn.compute.create_server, **options)
            return {"id": str(attr(server, "id", ""))}

        entry = self.create("servers", role, create)
        server = self.wait_status(
            f"{role} server",
            lambda: cloud.get_or_none("reading server", conn.compute.get_server, entry["id"]),
            ready={"active"},
            failed={"error", "deleted", "soft_deleted", "shelved_offloaded"},
        )
        if reason := owned("servers", entry, server, self.receipt):
            raise SystemProofError(f"owned {role} server check failed: {reason}")
        if attr(server, "key_name") != keypair["name"]:
            raise SystemProofError(f"owned {role} server does not use the owned keypair")
        flavor = attr(server, "flavor", {}) or {}
        flavor_id = attr(flavor, "id")
        flavor_name = attr(flavor, "original_name") or attr(flavor, "name")
        if flavor_id not in (None, APPROVED_FLAVOR_ID) or flavor_name not in (None, APPROVED_FLAVOR_NAME):
            raise SystemProofError(f"owned {role} server flavor mismatch")
        return entry

    def trust_host(self, role: str, server_id: str, address: str) -> Ssh:
        """Pin the host key from the owned server's Nova console and verify it with ssh-keyscan."""
        cloud = self.connect()
        self.verify_ingress(role)
        known = self.evidence.known_hosts(role)
        ssh = Ssh(self.evidence, role, address, self.deadline)
        if not known.exists():
            console_key: str | None = None
            while console_key is None:
                output = cloud.call(
                    "reading Nova console", cloud.conn.compute.get_server_console_output, server_id, length=4000
                )
                text = output.get("output", "") if isinstance(output, Mapping) else str(output or "")
                console_key = console_ed25519_host_key(text)
                if console_key is None:
                    self.deadline.sleep(POLL_SECONDS, f"waiting for {role} console host key")
            self.evidence.write_text(f"console-{role}-first-boot.log", text[-262144:], private=True)
            line: str | None = None
            while line is None:
                try:
                    scan = subprocess.run(
                        ["ssh-keyscan", "-t", "ed25519", "-T", "5", ssh.address],
                        capture_output=True,
                        check=False,
                        env=_command_env(),
                        timeout=20,
                    )
                    scanned = scan.stdout.decode(errors="replace")
                except subprocess.TimeoutExpired:
                    scanned = ""
                if "ssh-ed25519" in scanned:
                    line = verify_keyscan(ssh.address, scanned, console_key)
                else:
                    self.deadline.sleep(POLL_SECONDS, f"waiting for {role} sshd")
            atomic_write_bytes(known, line.encode())
            self.receipt.update("servers", role, ssh_host_ed25519=ed25519_fingerprint(console_key))
        while True:
            if ssh.run("true", action=f"connecting to {role}", timeout=40, check=False).returncode == 0:
                return ssh
            self.deadline.sleep(POLL_SECONDS, f"waiting for {role} SSH login")

    def wait_cloud_init(self, ssh: Ssh) -> None:
        result = ssh.run(
            "cloud-init status --wait >/dev/null 2>&1; cloud-init status --format json 2>/dev/null || true",
            action="waiting for cloud-init",
            timeout=1800,
            check=False,
        )
        text = result.stdout.decode(errors="replace").strip()
        try:
            status = json.loads(text).get("status") if text else None
        except ValueError:
            status = None
        if status not in {"done", None}:
            raise SystemProofError(f"cloud-init on {ssh.role} finished with status {status}")

    # ── Manila CephFS share ──────────────────────────────────────────────

    def _share(self, share_id: str) -> dict[str, Any] | None:
        response = self.connect().manila("GET", f"/shares/{share_id}", action="reading share", allow_404=True)
        return None if response is None else _json(response, "reading share").get("share")

    def _share_access_rules(self, share_id: str) -> list[dict[str, Any]]:
        response = self.connect().manila(
            "GET", "/share-access-rules?" + urllib.parse.urlencode({"share_id": share_id}), action="listing access"
        )
        return _json(response, "listing access").get("access_list", [])

    def ensure_share(self) -> dict[str, Any]:
        cloud = self.connect()

        def create(name: str) -> dict[str, Any]:
            body = {
                "share": {
                    "share_proto": SHARE_PROTO,
                    "size": SHARE_SIZE_GIB,
                    "name": name,
                    "description": self.receipt.description("artifacts"),
                    "share_type": SHARE_TYPE,
                    "metadata": self.receipt.metadata("artifacts"),
                    "is_public": False,
                }
            }
            response = cloud.manila(
                "POST", "/shares", action="creating share", json_body=body, expected=(200, 201, 202)
            )
            return {"id": str(_json(response, "creating share").get("share", {}).get("id", "")), "size_gib": 10}

        entry = self.create("shares", "artifacts", create)
        share = self.wait_status(
            "artifact share",
            lambda: self._share(entry["id"]),
            ready={"available"},
            failed={"error", "error_deleting", "deleting"},
        )
        if reason := owned("shares", entry, share, self.receipt):
            raise SystemProofError(f"owned share check failed: {reason}")
        if share.get("size") != SHARE_SIZE_GIB or str(share.get("share_proto", "")).upper() != SHARE_PROTO:
            raise SystemProofError("owned share size/protocol mismatch")
        access_to = self.receipt.run_name
        if not entry.get("access"):
            self.receipt.data["inflight"] = {
                "kind": "share_access",
                "role": "artifacts",
                "name": access_to,
                "started_at": utc_now(),
            }
            self.receipt.save()
            response = cloud.manila(
                "POST",
                f"/shares/{entry['id']}/action",
                action="allowing cephx access",
                json_body={"allow_access": {"access_type": "cephx", "access_to": access_to, "access_level": "rw"}},
            )
            access_id = str(_json(response, "allowing cephx access").get("access", {}).get("id", ""))
            if not access_id:
                raise SystemProofError("share access rule returned no id")
            entry.setdefault("access", []).append({"id": access_id, "access_to": access_to})
            self.receipt.data["inflight"] = None
            self.receipt.save()
        access_id = entry["access"][0]["id"]
        while True:
            response = cloud.manila("GET", f"/share-access-rules/{access_id}", action="reading access rule")
            rule = _json(response, "reading access rule").get("access", {})
            if (
                rule.get("access_to") != access_to
                or rule.get("access_type") != "cephx"
                or rule.get("share_id") not in (None, entry["id"])
            ):
                raise SystemProofError("share access rule ownership mismatch")
            if rule.get("state") == "active" and rule.get("access_key"):
                key = str(rule["access_key"])
                break
            if rule.get("state") in {"error", "denying"}:
                raise SystemProofError(f"share access rule entered state {rule.get('state')}")
            self.deadline.sleep(POLL_SECONDS, "waiting for cephx access rule")
        if not _CEPHX_KEY_RE.fullmatch(key):
            raise SystemProofError("cephx access key has an unexpected shape")
        REDACTOR.add(key)
        response = cloud.manila("GET", f"/shares/{entry['id']}/export_locations", action="reading export locations")
        locations = _json(response, "reading export locations").get("export_locations", [])
        paths = sorted(locations, key=lambda item: not item.get("preferred"))
        export = next(
            (item.get("path") for item in paths if _CEPH_EXPORT_RE.fullmatch(str(item.get("path", "")))), None
        )
        if export is None:
            raise SystemProofError("share has no CephFS export location")
        monitors = set(_CEPH_EXPORT_RE.fullmatch(export).group(1).split(","))
        if self.profile.ceph_monitors and not monitors <= set(self.profile.ceph_monitors):
            raise SystemProofError("share export monitors differ from the profile's Ceph monitors")
        atomic_write_json(
            self.evidence.ceph_access_path,
            {"share_id": entry["id"], "access_id": access_id, "access_to": access_to, "access_key": key},
        )
        if entry.get("export") != export:
            self.receipt.update("shares", "artifacts", export=export)
        return entry

    def ceph_access(self) -> dict[str, str]:
        raw = read_private_file(self.evidence.ceph_access_path, label="cephfs access file", max_bytes=8192)
        data = json.loads(raw)
        share = self.receipt.require("shares", "artifacts")
        if data.get("share_id") != share["id"] or data.get("access_to") != self.receipt.run_name:
            raise SystemProofError("cephfs access file does not belong to this receipt")
        if not _CEPHX_KEY_RE.fullmatch(str(data.get("access_key", ""))):
            raise SystemProofError("cephfs access key is malformed")
        REDACTOR.add(data["access_key"])
        return {"source": share["export"], "name": data["access_to"], "secret": data["access_key"]}

    def cephfs(self, ssh: Ssh, **operations: Any) -> dict[str, Any]:
        config = {**self.ceph_access(), "target": SHARE_MOUNT, **operations}
        return ssh.python(CEPHFS_SCRIPT, config, action=f"CephFS operation on {ssh.role}", timeout=900)

    # ── phases ───────────────────────────────────────────────────────────

    def builder(self) -> Ssh:
        server = self.receipt.require("servers", "builder")
        address = self.receipt.require("floating_ips", "builder").get("address")
        if not address:
            raise SystemProofError("builder floating IP address is not recorded")
        return self.trust_host("builder", server["id"], address)

    def phase_prepare(self) -> None:
        preflight = self.preflight()
        public_key = ensure_ssh_key(self.evidence, self.receipt)
        self.ensure_keypair(public_key)
        self.ensure_security_group()
        port, _ = self.ensure_port("builder")
        boot = self.ensure_volume("builder-boot", image_id=BUILDER_IMAGE_ID)
        server = self.ensure_server("builder", port_id=port["id"], volumes=[boot["id"]])
        address = self.ensure_floating_ip("builder", port["id"])
        share = self.ensure_share()
        ssh = self.trust_host("builder", server["id"], address)
        self.wait_cloud_init(ssh)
        bootstrap = ssh.bash(BUILDER_BOOTSTRAP_SCRIPT, action="bootstrapping builder from signed apt", timeout=2400)
        if bootstrap.get("root_size_bytes", 0) < 18 * 1024**3:
            raise SystemProofError("builder root filesystem did not grow to the 20 GiB boot volume")
        mount = self.cephfs(ssh, dirs=["artifacts", "proof"])
        if mount.get("size_bytes", 0) > (SHARE_SIZE_GIB + 1) * 1024**3:
            raise SystemProofError("mounted CephFS share reports more than its 10 GiB quota")
        self.evidence.write(
            "prepare.json",
            {
                "preflight": preflight,
                "builder": {"server_id": server["id"], "address": address, "bootstrap": bootstrap},
                "share": {"id": share["id"], "mount": mount},
                "ssh_source_cidr": SSH_SOURCE_CIDR,
                "completed_at": utc_now(),
            },
        )
        self.receipt.mark_phase("prepare", "done")

    def retire_derived_image(self, entry: dict[str, Any]) -> None:
        """A derived image from an incomplete build is deleted (exact ownership) and retired, then rebuilt."""
        cloud = self.connect()
        response = cloud.glance("GET", f"/images/{entry['id']}", action="reading image", allow_404=True)
        if response is not None:
            if reason := owned("images", entry, _json(response, "reading image"), self.receipt):
                raise SystemProofError(f"refusing to replace derived image: {reason}")
            cloud.glance("DELETE", f"/images/{entry['id']}", action="deleting incomplete image", expected=(204, 404))
            while cloud.glance("GET", f"/images/{entry['id']}", action="reading image", allow_404=True) is not None:
                self.deadline.sleep(POLL_SECONDS, "waiting for incomplete image deletion")
        self.receipt.entries("images").remove(entry)
        self.receipt.data.setdefault("retired", []).append(
            {"kind": "images", **entry, "retired_at": utc_now(), "verified_absent": True}
        )
        self.receipt.save()

    def phase_build(self) -> None:
        self.connect()
        if self.receipt.data["phases"].get("prepare", {}).get("status") != "done":
            raise SystemProofError("prepare phase has not completed")
        if self.receipt.data["phases"].get("build", {}).get("status") == "done":
            return
        existing = self.receipt.find("images", "derived")
        if existing is not None:
            self.retire_derived_image(existing)
        ssh = self.builder()
        mount = self.cephfs(ssh, dirs=["artifacts", "proof"])
        ssh.run(
            f"rm -rf {BUILDER_WORK}/upload && install -d -m 0700 {BUILDER_WORK}/upload",
            action="preparing builder upload directory",
            timeout=60,
        )
        ssh.upload(self.source.path, f"{BUILDER_WORK}/upload/source.tar.gz", action="uploading source bundle by SSH")
        extracted = ssh.python(
            EXTRACT_SCRIPT,
            {
                "bundle": f"{BUILDER_WORK}/upload/source.tar.gz",
                "dest": f"{BUILDER_WORK}/src",
                "sha256": self.source.sha256,
            },
            action="verifying and extracting source bundle",
        )
        build = ssh.bash(
            BUILD_SCRIPT_TEMPLATE.replace("@WORK@", shlex.quote(BUILDER_WORK)).replace(
                "@ART@", shlex.quote(ARTIFACT_DIR)
            ),
            action="BuildKit native-cloud-vm rootfs build",
            timeout=self.deadline.remaining("building"),
        )
        tar_bytes = int(build["rootfs_tar_gz_bytes"])
        disk_bytes = EXPORT_DISK_GIB * 1024**3
        if tar_bytes + disk_bytes + ARTIFACT_MARGIN_BYTES > MAX_ARTIFACT_BYTES:
            raise SystemProofError(
                f"artifact storage would exceed 10 GiB: rootfs.tar.gz {tar_bytes} + raw {disk_bytes} bytes"
            )
        if int(build["share_avail_bytes"]) < disk_bytes + ARTIFACT_MARGIN_BYTES:
            raise SystemProofError(
                f"share has {build['share_avail_bytes']} bytes available; raw disk needs {disk_bytes}"
            )
        export = ssh.bash(
            EXPORT_SCRIPT_TEMPLATE.replace("@WORK@", shlex.quote(BUILDER_WORK))
            .replace("@ART@", shlex.quote(ARTIFACT_DIR))
            .replace("@SIZE@", str(EXPORT_DISK_GIB))
            .replace("@SHA@", self.source.sha256),
            action="exporting BIOS raw disk with the bundle exporter",
            timeout=self.deadline.remaining("exporting"),
        )
        disk_sha = str(export.get("sidecar_sha256", ""))
        if not _SHA256_RE.fullmatch(disk_sha) or export.get("disk_bytes") != disk_bytes:
            raise SystemProofError("exporter sidecar/size is not the expected 8 GiB raw disk")
        if export.get("qemu_format") != "raw" or export.get("virtual_size") != disk_bytes:
            raise SystemProofError("qemu-img does not report an 8 GiB raw disk")
        metadata = export.get("metadata")
        if not isinstance(metadata, dict):
            raise SystemProofError("exporter metadata is missing")
        if self.source.sha256 not in json.dumps(metadata):
            raise SystemProofError("exporter metadata does not record the source bundle digest")
        image = self.upload_image(ssh, disk_sha, disk_bytes)
        self.evidence.write(
            "build.json",
            {
                "source": {
                    "afterglow_ref": self.source.manifest["afterglow_ref"],
                    "bundle_sha256": self.source.sha256,
                    "manifest_sha256": self.source.manifest_sha256,
                    "extracted": extracted,
                },
                "share_mount": mount,
                "buildkit": build,
                "export": export,
                "image": image,
                "completed_at": utc_now(),
            },
        )
        self.receipt.mark_phase(
            "build", "done", image_id=image["id"], disk_sha256=disk_sha, disk_sha512=image["sha512"]
        )

    def upload_image(self, ssh: Ssh, disk_sha256: str, disk_bytes: int) -> dict[str, Any]:
        cloud = self.connect()

        def create(name: str) -> dict[str, Any]:
            body = {
                "name": name,
                "disk_format": "raw",
                "container_format": "bare",
                "visibility": "private",
                "min_disk": EXPORT_DISK_GIB,
                **self.receipt.metadata("derived"),
                "palimpsest_afterglow_ref": self.source.manifest["afterglow_ref"],
                "palimpsest_source_sha256": self.source.sha256,
                "palimpsest_disk_sha256": disk_sha256,
                "palimpsest_target": "native-cloud-vm",
            }
            response = cloud.glance("POST", "/images", action="creating Glance image", json_body=body, expected=(201,))
            return {"id": str(_json(response, "creating Glance image").get("id", ""))}

        entry = self.create("images", "derived", create)
        record = _json(cloud.glance("GET", f"/images/{entry['id']}", action="reading image"), "reading image")
        if reason := owned("images", entry, record, self.receipt):
            raise SystemProofError(f"owned image check failed: {reason}")
        if record.get("status") != "queued":
            raise SystemProofError(f"owned image is {record.get('status')}, not queued; rerun build to retire it")
        process = subprocess.Popen(
            [
                "ssh",
                *ssh.options(),
                "-o",
                "Compression=yes",
                ssh.target,
                shlex.join(["sudo", "-n", "cat", f"{ARTIFACT_DIR}/disk.raw"]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_command_env(),
        )
        digests = {"sha256": hashlib.sha256(), "sha512": hashlib.sha512(), "md5": hashlib.md5()}
        counted = {"bytes": 0}

        def stream() -> Iterator[bytes]:
            assert process.stdout is not None
            while chunk := process.stdout.read(4 * 1024 * 1024):
                counted["bytes"] += len(chunk)
                if counted["bytes"] > disk_bytes:
                    raise SystemProofError("image stream exceeded the exported disk size")
                for digest in digests.values():
                    digest.update(chunk)
                self.deadline.remaining("uploading image bytes")
                yield chunk
            if process.wait(timeout=60) != 0 or counted["bytes"] != disk_bytes:
                raise SystemProofError("image byte stream from builder ended early or failed")
            if digests["sha256"].hexdigest() != disk_sha256:
                raise SystemProofError("streamed image SHA-256 does not match the exporter sidecar")

        try:
            cloud.glance(
                "PUT",
                f"/images/{entry['id']}/file",
                action="uploading authenticated image bytes",
                data=stream(),
                headers={"Content-Type": "application/octet-stream"},
                expected=(204,),
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if digests["sha256"].hexdigest() != disk_sha256 or counted["bytes"] != disk_bytes:
            raise SystemProofError("uploaded image bytes do not match the exporter sidecar")
        record = self.wait_status(
            "derived image",
            lambda: _json(cloud.glance("GET", f"/images/{entry['id']}", action="reading image"), "reading image"),
            ready={"active"},
            failed={"killed", "deleted", "deactivated"},
        )
        sha512 = digests["sha512"].hexdigest()
        if (
            record.get("os_hash_algo") != "sha512"
            or record.get("os_hash_value") != sha512
            or record.get("size") != disk_bytes
            or record.get("checksum") != digests["md5"].hexdigest()
        ):
            raise SystemProofError("Glance image hash/size does not match the uploaded bytes")
        self.receipt.update("images", "derived", sha256=disk_sha256, sha512=sha512, size=disk_bytes)
        return {"id": entry["id"], "sha256": disk_sha256, "sha512": sha512, "size": disk_bytes}

    def rgw_round_trip(self) -> dict[str, Any]:
        s3 = self.s3

        entry = self.receipt.find("buckets", "proof")
        if entry is None:
            name = self.receipt.name("buckets", "proof")
            status, _, _ = s3.request("HEAD", name, action="probing RGW bucket name")
            if status != 404:
                raise SystemProofError(f"RGW bucket name is not free (HTTP {status}); refusing")
            self.receipt.begin("buckets", "proof")
            s3.expect((200,), "PUT", name, action="creating RGW bucket")
            # Record a successful create before a distinct tagging request can fail.
            entry = self.receipt.finish("buckets", {"id": name, "name": name, "role": "proof", "objects": []})
            s3.put_bucket_tags(name, {"palimpsest-owner": self.receipt.owner_id, "palimpsest-run": name})
        tags = s3.bucket_tags(entry["id"]) or {}
        if tags.get("palimpsest-owner") != self.receipt.owner_id:
            raise SystemProofError("RGW proof bucket lacks this owner's tag; refusing to use or retag it")
        objects = {
            f"{self.receipt.run_name}/proof/payload.bin": os.urandom(RGW_PROOF_PAYLOAD_BYTES),
            f"{self.receipt.run_name}/proof/source-manifest.json": self.source.manifest_json_bytes(),
        }
        recorded = set(entry.get("objects", [])) | set(objects)
        unexpected = [key for key in s3.list_keys(entry["id"]) if key not in recorded]
        if unexpected:
            raise SystemProofError(f"RGW proof bucket holds {len(unexpected)} unrecorded objects; refusing")
        # Exact keys are recorded before upload and kept, so cleanup deletes only these keys.
        self.receipt.update("buckets", "proof", objects=sorted(recorded))
        results = {}
        for key, payload in objects.items():
            md5 = hashlib.md5(payload).hexdigest()
            _, headers, _ = s3.expect(
                (200,),
                "PUT",
                entry["id"],
                key,
                body=payload,
                headers={
                    "Content-MD5": base64.b64encode(bytes.fromhex(md5)).decode(),
                    "Content-Type": "application/octet-stream",
                },
                action="uploading RGW proof object",
            )
            _, _, downloaded = s3.expect((200,), "GET", entry["id"], key, action="downloading RGW proof object")
            expected_sha = hashlib.sha256(payload).hexdigest()
            actual_sha = hashlib.sha256(downloaded).hexdigest()
            if actual_sha != expected_sha or headers.get("etag", "").strip('"') != md5:
                raise SystemProofError("RGW object checksum round trip failed")
            results[key] = {"bytes": len(payload), "sha256": actual_sha, "etag_md5": md5}
        for key in objects:
            s3.expect((204, 200), "DELETE", entry["id"], key, action="deleting own RGW proof object")
        if s3.list_keys(entry["id"]):
            raise SystemProofError("RGW proof bucket is not empty after deleting own objects")
        # Keep the recorded key list: cleanup must still delete only exact recorded keys.
        return {"bucket": entry["id"], "objects": results, "deleted_own_objects": True}

    def consumer_user_data(self) -> str:
        conf = "\n".join(
            [
                "[openstack]",
                f"auth_url = {json.dumps(self.profile.auth_url)}",
                f"region_name = {json.dumps(self.profile.region_name)}",
                'interface = "public"',
                "insecure = false",
                'username = ""',
                'password = ""',
                "",
                "[security]",
                "ssl_verify = true",
                "",
                "[database]",
                "auto_create_tables = false",
                "",
            ]
        )
        dropin = "[Service]\nEnvironment=DEFAULT_NETWORK_ENABLED=false\n"
        indent = "      "
        return (
            "#cloud-config\n"
            "write_files:\n"
            "  - path: /etc/afterglow/afterglow.conf\n"
            "    owner: root:root\n"
            "    permissions: '0600'\n"
            "    content: |\n" + "".join(f"{indent}{line}\n" for line in conf.splitlines()) + ""
            f"  - path: /etc/systemd/system/{CONSUMER_UNIT}.d/50-palimpsest-system.conf\n"
            "    owner: root:root\n"
            "    permissions: '0644'\n"
            "    content: |\n" + "".join(f"{indent}{line}\n" for line in dropin.splitlines())
        )

    def consumer_probe(self, ssh: Ssh, mode: str) -> dict[str, Any]:
        return ssh.python(
            CONSUMER_PROBE_SCRIPT,
            {"mode": mode, "owner": self.receipt.owner_id, "marker": self.receipt.data.get("marker", "")},
            action=f"consumer {mode} probe",
            interpreter="/app/.venv/bin/python",
            timeout=300,
        )

    def wait_consumer_ready(self, ssh: Ssh, *, bound_seconds: float = 1800) -> dict[str, Any]:
        give_up = time.monotonic() + bound_seconds
        last_error = "no probe result"
        probe: dict[str, Any] = {}
        while True:
            try:
                probe = self.consumer_probe(ssh, "status")
                last_error = ""
            except SystemProofTimeout:
                raise
            except SystemProofError as exc:
                probe = {}
                last_error = sanitize(str(exc), 600)
            if (
                probe.get("health", {}).get("api_8000", {}).get("ok")
                and probe.get("health", {}).get("frontend_3080", {}).get("ok")
                and [child["role"] for child in probe.get("children", [])] == list(EXPECTED_CHILD_ROLES)
            ):
                return probe
            if time.monotonic() >= give_up:
                self.evidence.write("run-consumer-not-ready.json", {"probe": probe, "error": last_error})
                raise SystemProofError(
                    "native supervisor did not become ready: "
                    + (last_error or "; ".join(consumer_failures(probe)) or "children/health incomplete")
                )
            self.deadline.sleep(10, "waiting for native supervisor readiness")

    def record_consumer_installation(self, probe: Mapping[str, Any]) -> str | None:
        installed = probe.get("state", {}).get("installed_mtime")
        original = self.receipt.data.get("consumer_first_boot")
        if original is not None:
            if installed != original["installed_mtime"]:
                return "MariaDB data directory was reinitialized since the first consumer boot"
            return None
        if (installed or 0) < probe.get("boot_time", 0) - 1:
            return "MariaDB data directory predates the first consumer boot (not fresh)"
        self.receipt.data["consumer_first_boot"] = {
            "boot_id": probe["boot_id"],
            "boot_time": probe["boot_time"],
            "installed_mtime": installed,
        }
        self.receipt.save()
        return None

    def phase_run(self) -> None:
        cloud = self.connect()
        phases = self.receipt.data["phases"]
        if phases.get("build", {}).get("status") != "done":
            raise SystemProofError("build phase has not completed")
        image_entry = self.receipt.require("images", "derived")
        image = _json(cloud.glance("GET", f"/images/{image_entry['id']}", action="reading image"), "reading image")
        if owned("images", image_entry, image, self.receipt) or image.get("status") != "active":
            raise SystemProofError("derived image is not the active owned image")
        if image.get("os_hash_value") != phases["build"]["disk_sha512"]:
            raise SystemProofError("derived image hash changed since build")
        if "marker" not in self.receipt.data:
            self.receipt.data["marker"] = hashlib.sha256(os.urandom(32)).hexdigest()
            self.receipt.save()
        rgw = self.rgw_round_trip()

        port, _ = self.ensure_port("consumer")
        boot = self.ensure_volume("consumer-boot", image_id=image_entry["id"])
        data = self.ensure_volume("data", image_id=None)
        server = self.ensure_server(
            "consumer", port_id=port["id"], volumes=[boot["id"], data["id"]], user_data=self.consumer_user_data()
        )
        address = self.ensure_floating_ip("consumer", port["id"])
        ssh = self.trust_host("consumer", server["id"], address)
        self.wait_consumer_ready(ssh)
        status = self.consumer_probe(ssh, "status")
        if str(status.get("backend_env", {}).get("DEFAULT_NETWORK_ENABLED", "")).lower() != "false":
            # cloud-init wrote the drop-in after systemd loaded units; reload so the env takes effect.
            ssh.run(
                shlex.join(["sudo", "-n", "sh", "-c", f"systemctl daemon-reload && systemctl restart {CONSUMER_UNIT}"]),
                action="reloading consumer service drop-in",
                timeout=300,
            )
            self.wait_consumer_ready(ssh)
        first = self.consumer_probe(ssh, "write-markers")
        failures = consumer_failures(first)
        if not failures and (failure := self.record_consumer_installation(first)):
            failures.append(failure)
        if not first.get("markers", {}).get("mariadb") or not first.get("markers", {}).get("redis"):
            failures.append("MariaDB/Redis markers could not be written and read back")
        if failures:
            self.evidence.write("run-consumer-first.json", first)
            raise SystemProofError("consumer proof failed: " + "; ".join(failures))
        builder = self.builder()
        builder_marker = self.cephfs(builder, write={"proof/builder-marker.bin": CEPHFS_MARKER_BYTES})
        ceph_first = self.cephfs(
            ssh,
            verify={"proof/builder-marker.bin": builder_marker["written"]["proof/builder-marker.bin"]},
            write={"proof/consumer-marker.bin": CEPHFS_MARKER_BYTES},
        )
        if not all(ceph_first["verified"].values()):
            raise SystemProofError("consumer could not read the builder CephFS marker")
        data_entry = self.receipt.require("volumes", "data")
        if not data_entry.get("format_intent"):
            # Durable intent before mkfs: a crash after formatting can resume on this volume's own filesystem.
            data_entry = self.receipt.update("volumes", "data", format_intent=True)
        volume_first = ssh.python(
            DATA_VOLUME_SCRIPT,
            {
                "mode": "format",
                "volume_id": data_entry["id"],
                "size_bytes": VOLUME_SIZES_GIB["data"] * 1024**3,
                "mountpoint": DATA_MOUNT,
                "marker": self.receipt.data["marker"],
                "fs_uuid": data_entry.get("fs_uuid"),
                "format_intent": bool(data_entry.get("format_intent")),
            },
            action="formatting owned blank data volume",
        )
        self.receipt.update("volumes", "data", fs_uuid=volume_first["fs_uuid"])
        login = self.login_probe(ssh, server["id"])
        before_boot = first["boot_id"]
        ssh.run("sync; sleep 3", action="flushing before reboot", timeout=60)
        ssh.run(shlex.join(["sudo", "-n", "systemctl", "reboot"]), action="rebooting consumer", timeout=30, check=False)
        while True:
            self.deadline.sleep(10, "waiting for consumer reboot")
            current = ssh.boot_id()
            if current is not None and current != before_boot:
                break
        self.wait_consumer_ready(ssh)
        second = self.consumer_probe(ssh, "verify-markers")
        failures = consumer_failures(second)
        if not second.get("markers", {}).get("mariadb") or not second.get("markers", {}).get("redis"):
            failures.append("MariaDB/Redis markers did not persist across reboot")
        if second.get("state", {}).get("installed_mtime") != first.get("state", {}).get("installed_mtime"):
            failures.append("MariaDB data directory was reinitialized across reboot")
        volume_second = ssh.python(
            DATA_VOLUME_SCRIPT,
            {
                "mode": "verify",
                "volume_id": data_entry["id"],
                "size_bytes": VOLUME_SIZES_GIB["data"] * 1024**3,
                "mountpoint": DATA_MOUNT,
                "marker": self.receipt.data["marker"],
                "fs_uuid": volume_first["fs_uuid"],
            },
            action="verifying data volume after reboot",
        )
        ceph_second = self.cephfs(
            ssh, verify={"proof/consumer-marker.bin": ceph_first["written"]["proof/consumer-marker.bin"]}
        )
        if not volume_second.get("marker_ok"):
            failures.append("data volume marker did not persist across reboot")
        if not all(ceph_second["verified"].values()):
            failures.append("CephFS consumer marker did not survive consumer reboot/remount")
        report = {
            "scope": "direct Nova native-cloud-vm conventional cloud contract",
            "not_claimed": list(NOT_CLAIMED),
            "image": {"id": image_entry["id"], "sha512": image.get("os_hash_value")},
            "consumer": {"server_id": server["id"], "address": address},
            "rgw": rgw,
            "first_boot": first,
            "after_reboot": second,
            "cephfs": {"builder": builder_marker, "consumer_first": ceph_first, "consumer_after_reboot": ceph_second},
            "data_volume": {"first": volume_first, "after_reboot": volume_second},
            "login": login,
            "workqueue": {
                "exercised": False,
                "reason": "the only worker (app.notion_worker) needs external Notion/admin Keystone credentials; "
                "triggering it is unsafe for a member-only proof",
            },
            "failures": failures,
            "completed_at": utc_now(),
        }
        self.evidence.write("run.json", report)
        if failures:
            raise SystemProofError("consumer reboot proof failed: " + "; ".join(failures))
        self.receipt.mark_phase("run", "done", consumer_id=server["id"])

    def network_snapshot(self) -> dict[str, list[str]]:
        cloud = self.connect()
        conn = cloud.conn
        networks = cloud.listing("listing networks", conn.network.networks, project_id=APPROVED_PROJECT_ID)
        subnets = cloud.listing("listing subnets", conn.network.subnets, project_id=APPROVED_PROJECT_ID)
        routers = cloud.listing("listing routers", conn.network.routers, project_id=APPROVED_PROJECT_ID)
        ports = cloud.listing(
            "listing router ports",
            conn.network.ports,
            project_id=APPROVED_PROJECT_ID,
            device_owner="network:router_interface",
        )
        return {
            "networks": sorted(str(attr(item, "id")) for item in networks if _project_of(item) == APPROVED_PROJECT_ID),
            "subnets": sorted(str(attr(item, "id")) for item in subnets if _project_of(item) == APPROVED_PROJECT_ID),
            "routers": sorted(str(attr(item, "id")) for item in routers if _project_of(item) == APPROVED_PROJECT_ID),
            "router_interfaces": sorted(
                str(attr(item, "id"))
                for item in ports
                if attr(item, "device_owner") == "network:router_interface" and _project_of(item) == APPROVED_PROJECT_ID
            ),
        }

    def login_probe(self, ssh: Ssh, consumer_id: str) -> dict[str, Any]:
        """Real Afterglow member login through an SSH tunnel; tokens and password stay in memory only."""
        profile = self.profile
        require_login_account(profile)
        status = self.consumer_probe(ssh, "status")
        if (
            str(status.get("backend_env", {}).get("DEFAULT_NETWORK_ENABLED", "")).lower() != "false"
            or status.get("settings", {}).get("default_network_enabled") is not False
        ):
            raise SystemProofError("refusing login: DEFAULT_NETWORK_ENABLED=false is not effective in the backend")
        if consumer_failures(status):
            raise SystemProofError("refusing login: consumer service configuration is not member-only")
        before = self.network_snapshot()
        with socket.socket() as probe_socket:
            probe_socket.bind(("127.0.0.1", 0))
            local_port = probe_socket.getsockname()[1]
        tunnel = subprocess.Popen(
            [
                "ssh",
                *ssh.options(),
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"127.0.0.1:{local_port}:127.0.0.1:8000",
                ssh.target,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_command_env(),
        )
        base = f"http://127.0.0.1:{local_port}"
        result: dict[str, Any] = {"exercised": True}
        token: str | None = None
        login_error: BaseException | None = None
        try:
            while True:
                if tunnel.poll() is not None:
                    raise SystemProofError("SSH tunnel to the consumer API exited")
                try:
                    if _http(base + "/api/v1/health")[0] == 200:
                        break
                except OSError:
                    pass
                self.deadline.sleep(1, "opening consumer API tunnel")
            code, payload = _http(
                base + "/api/v1/auth/login",
                {
                    "username": profile.login_username,
                    "password": profile.login_password,
                    "project_name": profile.project_name,
                    "domain_name": profile.login_domain_name,
                },
            )
            result["login_status"] = code
            if code != 200 or not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
                raise SystemProofError(f"Afterglow member login failed: HTTP {code}")
            token = payload["token"]
            REDACTOR.add(token, payload.get("refresh_token"))
            # ff007ed TokenResponse: token, project_id, project_name, user_id, username, expires_at, roles, ...
            result["login"] = _principal_summary(payload, profile)
            if any(key not in payload for key in ("project_id", "project_name", "user_id", "username", "expires_at")):
                raise SystemProofError("Afterglow login response lacks the ff007ed TokenResponse fields")
            _require_member_principal(result["login"], "login token")
            code, me = _http(base + "/api/v1/auth/me", token=token)
            if code != 200 or not isinstance(me, dict):
                raise SystemProofError(f"Afterglow /auth/me failed: HTTP {code}")
            # ff007ed UserInfo: user_id, username, project_id, project_name, roles, is_system_admin, auth_method
            result["me"] = {"status": code, **_principal_summary(me, profile)}
            if me.get("auth_method") != "password" or not isinstance(me.get("roles"), list):
                raise SystemProofError("Afterglow /auth/me is not a password session with a role list")
            _require_member_principal(result["me"], "/auth/me")
            code, instances = _http(base + "/api/v1/instances", token=token)
            if code != 200 or not isinstance(instances, list):
                raise SystemProofError(f"authenticated instance listing failed: HTTP {code}")
            # ff007ed InstanceInfo requires id, name, status.
            if not all(isinstance(item, dict) and {"id", "name", "status"} <= set(item) for item in instances):
                raise SystemProofError("instance listing does not match the ff007ed InstanceInfo schema")
            listed = {item["id"]: item for item in instances}
            consumer = listed.get(consumer_id)
            result["instances"] = {
                "status": code,
                "count": len(listed),
                "consumer_listed": consumer is not None,
                "consumer_status": consumer.get("status") if consumer else None,
            }
            if consumer is None or str(consumer.get("status", "")).upper() != "ACTIVE":
                raise SystemProofError("authenticated instance listing does not show the ACTIVE consumer")
        except BaseException as exc:
            login_error = exc
        finally:
            if token is not None:
                try:
                    logout_code, logout_body = _http(base + "/api/v1/auth/logout", {}, token=token)
                except OSError:
                    logout_code, logout_body = None, None
                result["logout_status"] = logout_code
                result["logout_ok"] = logout_code == 200 and isinstance(logout_body, dict) and "message" in logout_body
                token = None
            tunnel.terminate()
            try:
                tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tunnel.kill()
                tunnel.wait()
        # Always compare SYSTEM networking, even when the login flow failed part-way.
        # Fixed settle (not the phase deadline) so the comparison runs even when the budget is exhausted.
        time.sleep(20)
        after = self.network_snapshot()
        result["system_network_unchanged"] = before == after
        result["network_snapshot_counts"] = {key: len(value) for key, value in after.items()}
        self.evidence.write("run-login.json", result)
        if before != after:
            raise SystemProofError("SYSTEM network/router inventory changed after Afterglow login")
        if login_error is not None:
            raise login_error
        if not result.get("logout_ok"):
            raise SystemProofError(f"Afterglow logout did not revoke the session: HTTP {result.get('logout_status')}")
        return result

    # ── cleanup ──────────────────────────────────────────────────────────

    def phase_cleanup(self) -> None:
        cloud = self.connect(reconcile=False)
        conn = cloud.conn
        outcome: dict[str, list[dict[str, Any]]] = {kind: [] for kind in RESOURCE_KINDS}
        refusals: list[str] = []
        try:
            self.reconcile_inflight()
        except SystemProofError as exc:
            refusals.append(f"unresolved inflight creation: {exc}")
        # Late-applied creations from cleared ambiguous markers: adopt into an empty slot, else refuse loudly.
        for marker in self.receipt.data.get("cleared_inflight", []):
            kind, role, name = marker["kind"], marker["role"], marker["name"]
            if kind == "share_access":
                continue  # access rules are removed with their owned share
            try:
                late = self._discover_any(kind, role, name)
            except SystemProofError as exc:
                refusals.append(f"{kind}/{role}: late-creation discovery refused: {exc}")
                continue
            known = {entry["id"] for entry in self.receipt.entries(kind)}
            for item in late:
                if item["id"] in known:
                    continue
                if self.receipt.find(kind, role) is None:
                    self.receipt.finish(kind, item)
                    known.add(item["id"])
                else:
                    refusals.append(f"{kind}/{role}: late-created owner-bound object {item['id']} outside the receipt")
        server_ids = {entry["id"] for entry in self.receipt.entries("servers")}
        port_ids = {entry["id"] for entry in self.receipt.entries("ports")}

        def record(kind: str, entry: Mapping[str, Any], result: str) -> None:
            outcome[kind].append({"id": entry["id"], "name": entry["name"], "role": entry["role"], "result": result})
            if result.startswith("refused"):
                refusals.append(f"{kind}/{entry['role']}: {result}")

        def delete_simple(kind: str, getter: Callable[[str], Any], deleter: Callable[..., Any], extra=None) -> list:
            pending = []
            for entry in self.receipt.entries(kind):
                key = entry["name"] if kind == "keypairs" else entry["id"]
                resource = cloud.get_or_none(f"reading {kind}", getter, key)
                if resource is None:
                    record(kind, entry, "absent")
                    continue
                reason = owned(kind, entry, resource, self.receipt) or (extra(entry, resource) if extra else None)
                if reason:
                    record(kind, entry, f"refused: {reason}")
                    continue
                cloud.call(f"deleting {kind}", deleter, key, ignore_missing=True)
                pending.append((kind, entry, getter, key))
            return pending

        def wait_absent(pending: list) -> None:
            for kind, entry, getter, key in pending:
                while cloud.get_or_none(f"reading {kind}", getter, key) is not None:
                    self.deadline.sleep(POLL_SECONDS, f"waiting for {kind}/{entry['role']} deletion")
                record(kind, entry, "deleted")

        wait_absent(delete_simple("servers", conn.compute.get_server, conn.compute.delete_server))

        def volume_extra(entry: Mapping[str, Any], volume: Any) -> str | None:
            for attachment in attr(volume, "attachments", []) or []:
                if attr(attachment, "server_id") not in server_ids:
                    return "attached to a foreign server"
            return None

        for entry in self.receipt.entries("volumes"):
            while True:
                volume = cloud.get_or_none("reading volume", conn.block_storage.get_volume, entry["id"])
                if volume is None or str(attr(volume, "status", "")).lower() not in {
                    "in-use",
                    "detaching",
                    "attaching",
                    "creating",
                    "downloading",
                }:
                    break
                if volume_extra(entry, volume):
                    break
                self.deadline.sleep(POLL_SECONDS, f"waiting for volume {entry['role']} detachment")
        wait_absent(
            delete_simple("volumes", conn.block_storage.get_volume, conn.block_storage.delete_volume, volume_extra)
        )

        def fip_extra(entry: Mapping[str, Any], address: Any) -> str | None:
            return None if attr(address, "port_id") in (None, "", *port_ids) else "associated with a foreign port"

        wait_absent(delete_simple("floating_ips", conn.network.get_ip, conn.network.delete_ip, fip_extra))

        def port_extra(entry: Mapping[str, Any], port: Any) -> str | None:
            if attr(port, "device_id") not in (None, "", *server_ids):
                return "bound to a foreign device"
            return None if attr(port, "network_id") == APPROVED_NETWORK_ID else "network mismatch"

        wait_absent(delete_simple("ports", conn.network.get_port, conn.network.delete_port, port_extra))

        def sg_extra(entry: Mapping[str, Any], group: Any) -> str | None:
            ports = cloud.listing("listing project ports", conn.network.ports, project_id=APPROVED_PROJECT_ID)
            bound = ports_bound_to_security_group(ports, entry["id"])
            return f"still bound to ports {bound}" if bound else None

        wait_absent(
            delete_simple(
                "security_groups", conn.network.get_security_group, conn.network.delete_security_group, sg_extra
            )
        )
        wait_absent(delete_simple("keypairs", conn.compute.get_keypair, conn.compute.delete_keypair))

        for entry in self.receipt.entries("images"):
            response = cloud.glance("GET", f"/images/{entry['id']}", action="reading image", allow_404=True)
            if response is None:
                record("images", entry, "absent")
                continue
            if reason := owned("images", entry, _json(response, "reading image"), self.receipt):
                record("images", entry, f"refused: {reason}")
                continue
            cloud.glance("DELETE", f"/images/{entry['id']}", action="deleting image", expected=(204, 404))
            while cloud.glance("GET", f"/images/{entry['id']}", action="reading image", allow_404=True) is not None:
                self.deadline.sleep(POLL_SECONDS, "waiting for image deletion")
            record("images", entry, "deleted")

        for entry in self.receipt.entries("shares"):
            share = self._share(entry["id"])
            if share is None:
                record("shares", entry, "absent")
                continue
            if reason := owned("shares", entry, share, self.receipt):
                record("shares", entry, f"refused: {reason}")
                continue
            rules = self._share_access_rules(entry["id"])
            for rule in rules:
                if (
                    rule.get("access_to") != self.receipt.run_name
                    or rule.get("access_type") != "cephx"
                    or rule.get("share_id") not in (None, entry["id"])
                ):
                    record("shares", entry, "refused: share carries a foreign access rule")
                    break
            else:
                # Include late-applied own rules after an ambiguous allow_access.
                # The live set is checked against the exact owned share above.
                entry["access"] = [{"id": rule["id"], "access_to": self.receipt.run_name} for rule in rules]
                self.receipt.save()
                for access in entry["access"]:
                    cloud.manila(
                        "POST",
                        f"/shares/{entry['id']}/action",
                        action="denying cephx access",
                        json_body={"deny_access": {"access_id": access["id"]}},
                        expected=(202, 404),
                    )
                while self._share_access_rules(entry["id"]):
                    self.deadline.sleep(POLL_SECONDS, "waiting for access rule removal")
                cloud.manila("DELETE", f"/shares/{entry['id']}", action="deleting share", expected=(202, 404))
                while self._share(entry["id"]) is not None:
                    self.deadline.sleep(POLL_SECONDS, "waiting for share deletion")
                record("shares", entry, "deleted")
                self.evidence.ceph_access_path.unlink(missing_ok=True)

        for entry in self.receipt.entries("buckets"):
            status, _, _ = self.s3.request("HEAD", entry["id"], action="probing RGW bucket")
            if status == 404:
                record("buckets", entry, "absent")
                continue
            if status != 200:
                record("buckets", entry, f"refused: presence unverifiable HTTP {status}")
                continue
            tags = self.s3.bucket_tags(entry["id"]) or {}
            if tags.get("palimpsest-owner") != self.receipt.owner_id:
                record("buckets", entry, "refused: bucket owner tag mismatch")
                continue
            keys = self.s3.list_keys(entry["id"])
            recorded = set(entry.get("objects", []))
            foreign = [key for key in keys if key not in recorded]
            if foreign:
                record("buckets", entry, f"refused: {len(foreign)} unrecorded objects present")
                continue
            for key in sorted(recorded):
                self.s3.expect((204, 200, 404), "DELETE", entry["id"], key, action="deleting own RGW object")
            self.s3.expect((204, 200, 404), "DELETE", entry["id"], action="deleting RGW bucket")
            if self.s3.request("HEAD", entry["id"], action="probing RGW bucket")[0] != 404:
                record("buckets", entry, "refused: bucket still present after delete")
                continue
            record("buckets", entry, "deleted")

        verified = not refusals and self.receipt.data["inflight"] is None
        self.evidence.write(
            "cleanup.json", {"outcome": outcome, "refusals": refusals, "verified": verified, "at": utc_now()}
        )
        if not verified:
            self.receipt.mark_phase("cleanup", "refused", refusals=refusals)
            raise SystemProofError("cleanup refused to delete some objects: " + "; ".join(refusals))
        self.receipt.data["cleanup"] = {"verified": True, "at": utc_now()}
        self.receipt.mark_phase("cleanup", "done")
        self.binding.release(evidence=self.evidence.root, owner_id=self.receipt.owner_id)


def require_login_account(profile: Profile) -> None:
    """Member API acceptance is mandatory for every non-cleanup phase; there is no skip path."""
    if not (profile.login_username and profile.login_password and profile.login_domain_name):
        raise SystemProofError(
            "profile lacks the dedicated member login_username/login_password/login_domain_name; "
            "refusing provisioning and proof phases (cleanup works with the application credential alone)"
        )


def _principal_summary(payload: Mapping[str, Any], profile: Profile) -> dict[str, Any]:
    roles = payload.get("roles")
    return {
        "project_id": str(payload.get("project_id", "")).replace("-", "").lower(),
        "project_name": payload.get("project_name"),
        "user_id_matches_appcred_user": str(payload.get("user_id", "")).replace("-", "").lower() == profile.user_id,
        "roles": sorted(str(role).lower() for role in roles) if isinstance(roles, list) else None,
        "is_system_admin": payload.get("is_system_admin"),
        "auth_method": payload.get("auth_method"),
    }


def _require_member_principal(summary: Mapping[str, Any], label: str) -> None:
    if summary["project_id"] != APPROVED_PROJECT_ID:
        raise SystemProofError(f"Afterglow {label} is not scoped to the SYSTEM project")
    if not summary["user_id_matches_appcred_user"]:
        raise SystemProofError(f"Afterglow {label} principal is not the provisioning application-credential user")
    roles = set(summary["roles"] or [])
    if not roles or roles & REFUSED_ROLES or not roles <= ALLOWED_ROLES or "member" not in roles:
        raise SystemProofError(f"Afterglow {label} roles are not exactly member (+reader)")
    if summary["is_system_admin"] is not False:
        raise SystemProofError(f"Afterglow {label} is not explicitly non-system-admin")


def _http(url: str, body: Mapping[str, Any] | None = None, *, token: str | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(4 * 1024 * 1024)
            code = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    try:
        return code, json.loads(raw) if raw else None
    except ValueError:
        return code, None


def consumer_failures(probe: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    pid1 = probe.get("pid1", {})
    if pid1.get("comm") != "systemd" or not str(pid1.get("exe", "")).endswith("/systemd"):
        failures.append("PID 1 is not systemd")
    if probe.get("container") != "none" or probe.get("dockerenv"):
        failures.append("guest reports a container environment")
    if probe.get("vm") != "kvm":
        failures.append("guest does not report a direct KVM virtual machine")
    if probe.get("engine_binaries") or probe.get("engine_processes"):
        failures.append("guest contains a container engine")
    unit = probe.get("unit", {})
    if unit.get("ActiveState") != "active" or unit.get("SubState") != "running":
        failures.append("afterglow-native.service is not active/running")
    supervisor = probe.get("supervisor", {})
    if supervisor.get("user") != "appuser" or not supervisor.get("script"):
        failures.append("supervisor is not native_vm.py running as appuser")
    children = probe.get("children", [])
    if [child.get("role") for child in children] != list(EXPECTED_CHILD_ROLES):
        failures.append("supervisor does not own exactly the five expected children")
    if any(child.get("user") != "appuser" for child in children):
        failures.append("a supervisor child is not running as appuser")
    health = probe.get("health", {})
    if not health.get("api_8000", {}).get("ok") or not health.get("frontend_3080", {}).get("ok"):
        failures.append("HTTP 8000/3080 health is not ok")
    listeners = probe.get("listeners", {})
    if not listeners.get("8000") or not listeners.get("3080"):
        failures.append("8000/3080 listeners missing")
    if not listeners.get("6379_loopback_only") or listeners.get("3306"):
        failures.append("datastores are exposed beyond loopback/unix socket")
    if str(probe.get("backend_env", {}).get("DEFAULT_NETWORK_ENABLED", "")).lower() != "false":
        failures.append("backend environment lacks DEFAULT_NETWORK_ENABLED=false")
    settings = probe.get("settings", {})
    expected = {
        "default_network_enabled": False,
        "database_auto_create_tables": False,
        "os_interface": "public",
        "os_insecure": False,
        "ssl_verify": True,
        "os_auth_url_https": True,
        "admin_username_configured": False,
        "admin_password_configured": False,
    }
    for key, value in expected.items():
        if settings.get(key) != value:
            failures.append(f"effective setting {key} is {settings.get(key)!r}, expected {value!r}")
    schema = probe.get("schema", {})
    if not isinstance(schema.get("tables"), int) or schema["tables"] < 1:
        failures.append("Afterglow schema has no tables")
    state = probe.get("state", {})
    if not state.get("account_ready") or state.get("mode") != "0o700" or state.get("owner") != "appuser":
        failures.append("persistent state directory is not initialized/owner-only")
    return failures


# ── Remote scripts (no secrets in text; secrets only arrive on stdin) ───────

BUILDER_BOOTSTRAP_SCRIPT = (
    r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
test "$ID" = ubuntu && test "$VERSION_ID" = 24.04
test "$(uname -m)" = x86_64
if grep -rqsiE 'trusted(=|:[[:space:]]*)yes|allow-(insecure|weak)(=|:[[:space:]]*)yes' \
    /etc/apt/sources.list /etc/apt/sources.list.d; then
  echo "unsigned/insecure apt source configured; refusing" >&2
  exit 5
fi
if apt-config dump | grep -qiE \
    'AllowUnauthenticated "(1|true|yes)"|AllowInsecureRepositories "(1|true|yes)"|AllowDowngradeToInsecureRepositories "(1|true|yes)"'; then
  echo "apt allows unauthenticated packages; refusing" >&2
  exit 5
fi
python3 - <<'PY'
import glob, re, sys
for path in glob.glob("/etc/apt/sources.list.d/*.sources"):
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    for stanza in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in stanza.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not any(line.lower().startswith("types:") for line in lines):
            continue
        if any(re.match(r"(?i)enabled:\s*no", line) for line in lines):
            continue
        if not any(line.lower().startswith("signed-by:") for line in lines):
            sys.exit("deb822 apt source without Signed-By: " + path)
PY
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends """
    + " ".join(BUILDER_PACKAGES)
    + r""" >/dev/null
if ! modprobe ceph 2>/dev/null; then
  apt-get install -y -q --no-install-recommends "linux-modules-extra-$(uname -r)" >/dev/null
  modprobe ceph
fi
systemctl enable --now docker >/dev/null 2>&1
docker buildx version >/dev/null
python3 - <<'PY'
import json, os, subprocess
packages = """
    + repr(list(BUILDER_PACKAGES))
    + r"""
versions = {}
for name in packages:
    out = subprocess.run(["dpkg-query", "-W", "-f=${Version}", name], capture_output=True, text=True)
    versions[name] = out.stdout.strip() if out.returncode == 0 else None
docker = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
buildx = subprocess.run(["docker", "buildx", "version"], capture_output=True, text=True)
st = os.statvfs("/")
print("PALIMPSEST_RESULT " + json.dumps({
    "packages": versions,
    "apt_unsigned_sources_absent": True,
    "docker_server": docker.stdout.strip(),
    "buildx": buildx.stdout.strip(),
    "kernel": os.uname().release,
    "root_size_bytes": st.f_blocks * st.f_frsize,
    "root_avail_bytes": st.f_bavail * st.f_frsize,
}))
PY
"""
)

EXTRACT_SCRIPT = r"""
import hashlib, json, os, shutil, sys, tarfile
cfg = json.loads(sys.stdin.readline())
digest = hashlib.sha256()
with open(cfg["bundle"], "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)
if digest.hexdigest() != cfg["sha256"]:
    sys.exit("uploaded source bundle digest mismatch")
dest = cfg["dest"]
if os.path.lexists(dest):
    shutil.rmtree(dest)
os.makedirs(dest, mode=0o755)
count = 0
with tarfile.open(cfg["bundle"], "r:gz") as archive:
    archive.extractall(dest, filter="data")
    count = sum(1 for member in archive.getmembers() if member.isfile())
for required in ("Dockerfile", "scripts/export_native_cloud.py"):
    if not os.path.isfile(os.path.join(dest, required)):
        sys.exit("extracted bundle lacks " + required)
print("PALIMPSEST_RESULT " + json.dumps({"sha256": cfg["sha256"], "files": count}))
"""

BUILD_SCRIPT_TEMPLATE = r"""
set -euo pipefail
work=@WORK@
art=@ART@
test "$(stat -f -c %T /mnt/pal-share)" = ceph
rm -rf "$art"
install -d -m 0755 "$art"
started=$(date -u +%s)
set +e
docker buildx build --progress=plain --platform linux/amd64 --target native-cloud-vm \
  --metadata-file "$work/build-metadata.json" --output type=tar,dest=- "$work/src" 2>"$work/build.log" \
  | pigz -6 > "$art/rootfs.tar.gz.partial"
codes=("${PIPESTATUS[@]}")
set -e
if [ "${codes[0]}" != 0 ] || [ "${codes[1]}" != 0 ]; then
  echo "build pipeline failed: buildx=${codes[0]} gzip=${codes[1]}" >&2
  tail -c 20000 "$work/build.log" >&2
  exit 3
fi
pigz -t "$art/rootfs.tar.gz.partial"
sync
mv "$art/rootfs.tar.gz.partial" "$art/rootfs.tar.gz"
finished=$(date -u +%s)
docker buildx prune -af >/dev/null 2>&1 || true
docker system prune -af --volumes >/dev/null 2>&1
python3 - "$work" "$art" "$started" "$finished" <<'PY'
import hashlib, json, os, re, sys
work, art, started, finished = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
digest = hashlib.sha256()
path = os.path.join(art, "rootfs.tar.gz")
with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        digest.update(chunk)
with open(os.path.join(work, "build.log"), encoding="utf-8", errors="replace") as handle:
    log = handle.read()
resolved = sorted(set(re.findall(r"resolve ([^\s]+@sha256:[0-9a-f]{64})", log)))[:64]
try:
    with open(os.path.join(work, "build-metadata.json"), encoding="utf-8") as handle:
        metadata = json.load(handle)
except (OSError, ValueError):
    metadata = None
share = os.statvfs("/mnt/pal-share")
root = os.statvfs("/")
print("PALIMPSEST_RESULT " + json.dumps({
    "target": "native-cloud-vm",
    "platform": "linux/amd64",
    "output": "type=tar streamed through pigz into CephFS",
    "rootfs_tar_gz_sha256": digest.hexdigest(),
    "rootfs_tar_gz_bytes": os.path.getsize(path),
    "base_image_digests": resolved,
    "buildx_metadata": metadata,
    "build_seconds": finished - started,
    "share_avail_bytes": share.f_bavail * share.f_frsize,
    "share_size_bytes": share.f_blocks * share.f_frsize,
    "root_avail_bytes_after_prune": root.f_bavail * root.f_frsize,
    "build_log_tail": log[-6000:],
}))
PY
"""

EXPORT_SCRIPT_TEMPLATE = r"""
set -euo pipefail
work=@WORK@
art=@ART@
rm -f "$art/disk.raw" "$art/disk.raw.sha256" "$art/disk.raw.metadata.json"
cd "$work/src"
if ! python3 scripts/export_native_cloud.py --rootfs "$art/rootfs.tar.gz" --output "$art/disk.raw" \
    --size-gib @SIZE@ --source-sha256 @SHA@ >"$work/export.log" 2>&1; then
  tail -c 20000 "$work/export.log" >&2
  exit 4
fi
sync
qemu-img info --output=json "$art/disk.raw" >"$work/qemu-img-info.json"
python3 - "$work" "$art" <<'PY'
import json, os, re, sys
work, art = sys.argv[1], sys.argv[2]
with open(os.path.join(art, "disk.raw.sha256"), encoding="utf-8") as handle:
    sidecar = handle.read(4096).strip()
match = re.fullmatch(r"([0-9a-f]{64})\s+\*?disk\.raw", sidecar)
if match is None:
    sys.exit("exporter sha256 sidecar is malformed")
with open(os.path.join(art, "disk.raw.metadata.json"), encoding="utf-8") as handle:
    metadata = json.loads(handle.read(1 << 20))
with open(os.path.join(work, "qemu-img-info.json"), encoding="utf-8") as handle:
    info = json.load(handle)
with open(os.path.join(work, "export.log"), encoding="utf-8", errors="replace") as handle:
    log_tail = handle.read()[-4000:]
share = os.statvfs("/mnt/pal-share")
print("PALIMPSEST_RESULT " + json.dumps({
    "sidecar_sha256": match.group(1),
    "disk_bytes": os.path.getsize(os.path.join(art, "disk.raw")),
    "disk_allocated_bytes": os.stat(os.path.join(art, "disk.raw")).st_blocks * 512,
    "qemu_format": info.get("format"),
    "virtual_size": info.get("virtual-size"),
    "metadata": metadata,
    "share_avail_bytes": share.f_bavail * share.f_frsize,
    "export_log_tail": log_tail,
}))
PY
"""

CEPHFS_SCRIPT = r"""
import ctypes, hashlib, json, os, re, subprocess, sys
cfg = json.loads(sys.stdin.readline())
target = cfg["target"]
def mount_entry(path):
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if fields[4] == path:
                tail = fields[fields.index("-") + 1:]
                return {"root": fields[3], "fstype": tail[0], "source": tail[1], "options": tail[2].split(",")}
    return None
export_path = cfg["source"].rsplit(":", 1)[1]
def identity_ok(entry):
    # Only the owned share's exact export, mounted at its root as the owned cephx identity, is accepted.
    return (
        entry["fstype"] == "ceph"
        and entry["root"] == "/"
        and (entry["source"].endswith(":" + export_path) or entry["source"].endswith("=" + export_path))
        and "name=" + cfg["name"] in entry["options"]
    )
def safe(rel):
    if not re.fullmatch(r"[a-z0-9][a-z0-9/_.-]*", rel) or ".." in rel.split("/"):
        sys.exit("unsafe share path")
    return os.path.join(target, rel)
result = {}
current = mount_entry(target)
if current is None:
    if not re.fullmatch(r"[A-Za-z0-9+/=]{20,128}", cfg["secret"]) or not re.fullmatch(r"[a-z0-9-]{1,64}", cfg["name"]):
        sys.exit("invalid cephx credential shape")
    if not re.fullmatch(r"(?:[0-9.]+:[0-9]+,)*[0-9.]+:[0-9]+:/[A-Za-z0-9/_.-]+", cfg["source"]):
        sys.exit("invalid CephFS export path")
    os.makedirs(target, mode=0o755, exist_ok=True)
    subprocess.run(["modprobe", "ceph"], check=False, capture_output=True)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    options = ("name=" + cfg["name"] + ",secret=" + cfg["secret"]).encode()
    if libc.mount(cfg["source"].encode(), target.encode(), b"ceph", 0, options) != 0:
        error = ctypes.get_errno()
        sys.exit("CephFS mount failed: errno %d %s" % (error, os.strerror(error)))
    result["mounted"] = "new"
    current = mount_entry(target)
    if current is None or not identity_ok(current):
        subprocess.run(["umount", target], check=False, capture_output=True)
        sys.exit("new CephFS mount does not show the owned export/identity")
elif not identity_ok(current):
    sys.exit("share mount point holds a mount other than the owned CephFS export; refusing")
else:
    result["mounted"] = "existing"
result["mount_source_matches_export"] = True
for rel in cfg.get("dirs", []):
    os.makedirs(safe(rel), mode=0o755, exist_ok=True)
written = {}
for rel, size in cfg.get("write", {}).items():
    path = safe(rel)
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    payload = os.urandom(int(size))
    with open(path + ".tmp", "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(path + ".tmp", path)
    written[rel] = hashlib.sha256(payload).hexdigest()
verified = {}
for rel, expected in cfg.get("verify", {}).items():
    digest = hashlib.sha256()
    with open(safe(rel), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    verified[rel] = digest.hexdigest() == expected
stats = os.statvfs(target)
result.update({
    "fstype": "ceph",
    "size_bytes": stats.f_blocks * stats.f_frsize,
    "avail_bytes": stats.f_bavail * stats.f_frsize,
    "written": written,
    "verified": verified,
})
print("PALIMPSEST_RESULT " + json.dumps(result))
"""

DATA_VOLUME_SCRIPT = r"""
import glob, json, os, subprocess, sys
cfg = json.loads(sys.stdin.readline())
volume = cfg["volume_id"]
mountpoint = cfg["mountpoint"]
marker_path = os.path.join(mountpoint, "palimpsest-system-marker")
def run(*argv, check=True):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if check and done.returncode != 0:
        sys.exit("%s failed with exit %d" % (argv[0], done.returncode))
    return done
links = [path for path in glob.glob("/dev/disk/by-id/*") if volume[:20] in os.path.basename(path) and "-part" not in path]
devices = sorted({os.path.realpath(path) for path in links})
if len(devices) != 1:
    sys.exit("expected exactly one block device for the owned data volume, found %d" % len(devices))
device = devices[0]
name = os.path.basename(device)
if os.path.exists("/sys/class/block/%s/partition" % name):
    sys.exit("data volume resolved to a partition")
with open("/sys/class/block/%s/size" % name) as handle:
    size = int(handle.read()) * 512
if size != cfg["size_bytes"]:
    sys.exit("data volume size mismatch")
def mounted_source(path):
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if fields[4] == path:
                return os.path.realpath(fields[fields.index("-") + 2])
    return None
result = {"device_name": name, "size_bytes": size}
uuid_now = run("blkid", "-s", "UUID", "-o", "value", device, check=False).stdout.strip()
if cfg["mode"] == "format":
    children = [entry for entry in os.listdir("/sys/class/block/%s" % name) if entry.startswith(name)]
    if children or os.listdir("/sys/class/block/%s/holders" % name):
        sys.exit("data volume has partitions or holders; refusing")
    probe = run("blkid", "-p", device, check=False)
    if probe.returncode == 2:
        with open(device, "rb") as handle:
            head = handle.read(1 << 20)
            handle.seek(size - (1 << 20))
            tail = handle.read(1 << 20)
        if head.strip(b"\0") or tail.strip(b"\0"):
            sys.exit("data volume is not blank; refusing to format")
        run("mkfs.ext4", "-q", "-L", "palsys-data", device)
        result["formatted"] = True
    elif cfg.get("fs_uuid") and uuid_now == cfg["fs_uuid"]:
        result["formatted"] = False
    elif (
        cfg.get("format_intent")
        and not cfg.get("fs_uuid")
        and run("blkid", "-s", "TYPE", "-o", "value", device, check=False).stdout.strip() == "ext4"
        and run("blkid", "-s", "LABEL", "-o", "value", device, check=False).stdout.strip() == "palsys-data"
    ):
        # Our own interrupted format of this newly created, owned volume (intent recorded before mkfs).
        result["formatted"] = False
        result["resumed_partial_format"] = True
    else:
        sys.exit("data volume carries a signature not recorded for this run; refusing to format")
    fs_uuid = run("blkid", "-s", "UUID", "-o", "value", device).stdout.strip()
    line = "UUID=%s %s ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n" % (fs_uuid, mountpoint)
    with open("/etc/fstab", encoding="utf-8") as handle:
        fstab = handle.read()
    if line not in fstab:
        if (" %s " % mountpoint) in fstab:
            sys.exit("fstab already has a different entry for the data mount point")
        with open("/etc/fstab", "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    os.makedirs(mountpoint, mode=0o755, exist_ok=True)
    if mounted_source(mountpoint) is None:
        run("systemctl", "daemon-reload")
        run("mount", mountpoint)
    if mounted_source(mountpoint) != device:
        sys.exit("data mount point is not backed by the owned volume")
    with open(marker_path + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(cfg["marker"] + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(marker_path + ".tmp", marker_path)
    run("sync")
    result["fs_uuid"] = fs_uuid
    result["marker_written"] = True
else:
    if uuid_now != cfg["fs_uuid"]:
        sys.exit("data volume filesystem UUID changed")
    result["mounted_from_fstab"] = mounted_source(mountpoint) == device
    with open(marker_path, encoding="utf-8") as handle:
        result["marker_ok"] = result["mounted_from_fstab"] and handle.read().strip() == cfg["marker"]
    result["fs_uuid"] = uuid_now
print("PALIMPSEST_RESULT " + json.dumps(result))
"""

CONSUMER_PROBE_SCRIPT = r"""
import json, os, pwd, shutil, socket, subprocess, sys, urllib.request
cfg = json.loads(sys.stdin.readline())
UNIT = "afterglow-native.service"
STATE = "/var/lib/afterglow"
TABLE = "palimpsest_system_proof"
ENGINES = ("docker", "dockerd", "containerd", "containerd-shim", "podman", "runc", "crun", "ctr", "nerdctl", "buildkitd")
def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read().strip()
def user_of(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)
result = {"probe": cfg["mode"], "boot_id": read("/proc/sys/kernel/random/boot_id")}
with open("/proc/stat") as handle:
    result["boot_time"] = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
result["pid1"] = {"exe": os.readlink("/proc/1/exe"), "comm": read("/proc/1/comm")}
detect = subprocess.run(["systemd-detect-virt", "--container"], capture_output=True, text=True)
result["container"] = detect.stdout.strip() or "none"
vm = subprocess.run(["systemd-detect-virt", "--vm"], capture_output=True, text=True)
result["vm"] = vm.stdout.strip() or "none"
result["engine_binaries"] = sorted(name for name in ENGINES if shutil.which(name))
result["dockerenv"] = os.path.exists("/.dockerenv")
processes = {}
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        comm = read("/proc/%s/comm" % entry)
        ppid = int(read("/proc/%s/stat" % entry).rsplit(")", 1)[1].split()[1])
        uid = os.stat("/proc/%s" % entry).st_uid
        with open("/proc/%s/cmdline" % entry, "rb") as handle:
            argv = [part.decode(errors="replace") for part in handle.read().split(b"\0") if part]
    except (OSError, ValueError, IndexError):
        continue
    processes[int(entry)] = (comm, ppid, uid, argv)
result["engine_processes"] = sorted({comm for comm, _, _, _ in processes.values() if comm in ENGINES})
show = subprocess.run(
    ["systemctl", "show", UNIT, "--property=ActiveState,SubState,MainPID,NRestarts,User"], capture_output=True, text=True
)
props = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
result["unit"] = {key: props.get(key) for key in ("ActiveState", "SubState", "MainPID", "NRestarts", "User")}
main = int(props.get("MainPID") or 0)
info = processes.get(main)
result["supervisor"] = {
    "user": user_of(info[2]) if info else None,
    "script": bool(info) and any(arg.endswith("native_vm.py") for arg in info[3]),
}
children = []
backend = None
for pid, (comm, ppid, uid, argv) in processes.items():
    if not main or ppid != main:
        continue
    joined = " ".join(argv[:8])
    if comm.startswith("mariadbd"):
        role = "mariadb"
    elif comm.startswith("redis-server"):
        role = "redis"
    elif "uvicorn" in joined and "app.main:app" in joined:
        role = "backend"
        backend = pid
    elif "app.notion_worker" in joined:
        role = "worker"
    elif comm == "node":
        role = "frontend"
    else:
        role = "other:" + comm
    children.append({"role": role, "user": user_of(uid)})
result["children"] = sorted(children, key=lambda item: item["role"])
def health(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read(8192)
            return {"status": response.status, "ok": response.status == 200 and json.loads(body).get("status") == "ok"}
    except Exception as exc:
        return {"status": getattr(exc, "code", None), "ok": False}
result["health"] = {
    "api_8000": health("http://127.0.0.1:8000/api/v1/health"),
    "frontend_3080": health("http://127.0.0.1:3080/health"),
}
listening = {}
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(table) as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if fields[3] != "0A":
                    continue
                address, port = fields[1].rsplit(":", 1)
                listening.setdefault(int(port, 16), set()).add(address)
    except OSError:
        pass
loopback = {"0100007F", "00000000000000000000000001000000"}
result["listeners"] = {
    "8000": 8000 in listening,
    "3080": 3080 in listening,
    "6379_loopback_only": 6379 in listening and listening[6379] <= loopback,
    "3306": 3306 in listening,
}
environment = {}
if backend:
    with open("/proc/%d/environ" % backend, "rb") as handle:
        for item in handle.read().split(b"\0"):
            if b"=" in item:
                key, value = item.decode(errors="replace").split("=", 1)
                environment[key] = value
result["backend_env"] = {"DEFAULT_NETWORK_ENABLED": environment.get("DEFAULT_NETWORK_ENABLED")}
SNIPPET = (
    "import json, sys\n"
    "sys.path.insert(0, '/app')\n"
    "from app.config import get_settings, load_raw_toml\n"
    "s = get_settings()\n"
    "raw = load_raw_toml()\n"
    "print(json.dumps({'default_network_enabled': s.default_network_enabled,"
    " 'database_auto_create_tables': s.database_auto_create_tables,"
    " 'raw_auto_create_tables': raw.get('database', {}).get('auto_create_tables'),"
    " 'os_interface': s.os_interface, 'os_insecure': s.os_insecure, 'ssl_verify': s.ssl_verify is True,"
    " 'os_auth_url_https': s.os_auth_url.startswith('https://'),"
    " 'admin_username_configured': bool(s.os_username), 'admin_password_configured': bool(s.os_password)}))\n"
)
if environment:
    done = subprocess.run(
        ["/app/.venv/bin/python", "-c", SNIPPET], env=environment, cwd="/app", user="appuser",
        capture_output=True, text=True, timeout=120,
    )
    try:
        result["settings"] = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else {}
    except (ValueError, IndexError):
        result["settings"] = {}
else:
    result["settings"] = {}
state = {}
try:
    st = os.stat(STATE)
    state["mode"] = oct(st.st_mode & 0o777)
    state["owner"] = user_of(st.st_uid)
    state["account_ready"] = os.path.isfile(STATE + "/mariadb/.account-ready")
    installed = STATE + "/mariadb/.installed"
    state["installed_mtime"] = int(os.stat(installed).st_mtime) if os.path.isfile(installed) else None
    cred = os.stat(STATE + "/credentials.json")
    state["credentials_mode"] = oct(cred.st_mode & 0o777)
except OSError:
    pass
result["state"] = state
schema = {}
markers = {}
try:
    with open(STATE + "/credentials.json") as handle:
        credentials = json.load(handle)
    import pymysql
    connection = pymysql.connect(
        unix_socket=STATE + "/run/mysql.sock", user="afterglow", password=credentials["sql_app"],
        database="afterglow", connect_timeout=5, autocommit=True,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='afterglow' AND table_name <> %s",
            (TABLE,),
        )
        schema["tables"] = int(cursor.fetchone()[0])
        if cfg["mode"] == "write-markers":
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS " + TABLE + " (owner_id CHAR(36) PRIMARY KEY, marker CHAR(64) NOT NULL)"
            )
            cursor.execute("REPLACE INTO " + TABLE + " (owner_id, marker) VALUES (%s, %s)", (cfg["owner"], cfg["marker"]))
        if cfg["mode"] in ("write-markers", "verify-markers"):
            cursor.execute("SELECT marker FROM " + TABLE + " WHERE owner_id=%s", (cfg["owner"],))
            row = cursor.fetchone()
            markers["mariadb"] = bool(row) and row[0] == cfg["marker"]
    connection.close()
    def resp(sock, *parts):
        payload = b"*%d\r\n" % len(parts) + b"".join(b"$%d\r\n%s\r\n" % (len(p), p) for p in parts)
        sock.sendall(payload)
        reader = sock.makefile("rb")
        head = reader.readline()
        if head.startswith(b"$"):
            length = int(head[1:])
            return None if length < 0 else reader.read(length + 2)[:-2]
        if head.startswith(b"-"):
            raise RuntimeError("redis error")
        return head.strip()
    if cfg["mode"] in ("write-markers", "verify-markers"):
        key = ("palimpsest:system-proof:" + cfg["owner"]).encode()
        with socket.create_connection(("127.0.0.1", 6379), timeout=5) as sock:
            resp(sock, b"AUTH", credentials["redis"].encode())
            if cfg["mode"] == "write-markers":
                resp(sock, b"SET", key, cfg["marker"].encode())
            markers["redis"] = resp(sock, b"GET", key) == cfg["marker"].encode()
except Exception as exc:
    schema["error"] = type(exc).__name__
result["schema"] = schema
result["markers"] = markers
print("PALIMPSEST_RESULT " + json.dumps(result))
"""


# ── CLI ─────────────────────────────────────────────────────────────────────


_FORBIDDEN_ARGV_PREFIXES = (
    "--os-",
    "--password",
    "--token",
    "--secret",
    "--application-credential",
    "--insecure",
    "--admin",
    "--allow",
    "--no-verify",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--profile", type=Path, required=True, help="private member/reader profile JSON (0600)")
    parser.add_argument("--evidence", type=Path, required=True, help="private evidence directory (0700)")
    parser.add_argument("--source-bundle", type=Path, required=True, help="SOURCE.tar.gz with .manifest.json sidecar")
    parser.add_argument("--afterglow-ref", required=True, help="approved Afterglow baseline (ff007ed)")
    parser.add_argument(
        "--source-approval",
        type=Path,
        required=True,
        help=f"parent-written owner-private approval receipt ({SOURCE_APPROVAL_SCHEMA})",
    )
    parser.add_argument("--phase", choices=("prepare", "build", "run", "cleanup", "all"), required=True)
    parser.add_argument("--deadline-seconds", type=int, default=None, help="per-phase bound (default per phase)")
    parser.add_argument(
        "--user-port-security-group",
        action="append",
        default=[],
        metavar="UUID",
        help="existing SYSTEM security group the user approved for owned ports (exact id; never modified)",
    )
    return parser


def run_phase(
    args: argparse.Namespace,
    phase: str,
    profile: Profile,
    evidence: Evidence,
    source: SourceBundle,
    binding: SystemBinding,
) -> None:
    deadline = Deadline(args.deadline_seconds or PHASE_DEADLINES[phase])
    runner = Runner(profile, evidence, source, phase, deadline, binding)
    try:
        getattr(runner, f"phase_{phase}")()
    except BaseException as exc:
        message = sanitize(str(exc)) if isinstance(exc, SystemProofError) else f"{type(exc).__name__}"
        try:
            runner.receipt.mark_phase(phase, "failed", error=message)
        except Exception:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    def interrupted(signum: int, _frame: Any) -> None:
        raise SystemProofInterrupted(f"interrupted by signal {signum}")

    try:
        for argument in arguments:
            if argument.lower().startswith(_FORBIDDEN_ARGV_PREFIXES):
                raise SystemProofError("secrets and safety overrides are never accepted on the command line")
        args = build_parser().parse_args(arguments)
        global USER_PORT_SECURITY_GROUPS
        if not all(_UUID_RE.fullmatch(item) for item in args.user_port_security_group):
            raise SystemProofError("--user-port-security-group must be an exact security group UUID")
        USER_PORT_SECURITY_GROUPS = tuple(sorted(set(args.user_port_security_group)))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        profile = load_profile(args.profile)
        if args.phase != "cleanup":
            require_login_account(profile)
        source = verify_source_bundle(args.source_bundle, args.afterglow_ref, cleanup_only=args.phase == "cleanup")
        source = verify_source_approval(args.source_approval, source)
        evidence = Evidence(args.evidence)
        binding = SystemBinding(binding_directory())
        try:
            if args.phase != "all":
                run_phase(args, args.phase, profile, evidence, source, binding)
                return 0
            primary: BaseException | None = None
            try:
                for phase in ("prepare", "build", "run"):
                    run_phase(args, phase, profile, evidence, source, binding)
            except BaseException as exc:
                primary = exc
            try:
                run_phase(args, "cleanup", profile, evidence, source, binding)
            except BaseException as exc:
                if primary is None:
                    raise
                raise SystemProofError(f"{sanitize(str(primary))}; cleanup also failed: {sanitize(str(exc))}") from exc
            if primary is not None:
                raise primary
            return 0
        finally:
            binding.close()
    except SystemProofError as exc:
        sys.stderr.write(f"run_openstack_system: {sanitize(str(exc))}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"run_openstack_system: unexpected {type(exc).__name__}: {sanitize(str(exc), 1000)}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
