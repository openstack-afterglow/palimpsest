#!/usr/bin/env python3
"""Provision an isolated disposable OpenStack Nova VM, run the native KVM proof, and reclaim owned resources."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MANIFEST_SCHEMA = "palimpsest.native-openstack.v1"
PROOF_RECEIPT_SCHEMA = "palimpsest.native-openstack-proof-receipt.v1"

APPROVED_REPOSITORY = "openstack-afterglow/palimpsest"
APPROVED_EVENTS = frozenset({"push"})
APPROVED_BRANCH_REFS = frozenset({"refs/heads/dev", "refs/heads/main"})
APPROVED_FLAVOR_ID = "783c0eda-08f5-4233-8ddf-c142cfa0ff76"
APPROVED_FLAVOR_NAME = "cpu.2c_8g"
APPROVED_FLAVOR_VCPUS = 2
APPROVED_FLAVOR_RAM_MIB = 8192
APPROVED_NETWORK_ID = "8e1fb59c-aad8-4dad-8640-fd2e9d313c5f"
APPROVED_VOLUME_SIZE_GIB = 20
APPROVED_VOLUME_TYPE = "ceph_hdd"
APPROVED_IMAGE_SHA512 = (
    "c8b16a216ee0b40fd7ad6d730a1a39cee12cdd30d841c915c859f6c98d351020a021df89189af40b16ca871d845b2bc"
    "c7731f315e5ed811924751172886856de"
)
MAX_IMAGE_BYTES = 4 * 1024 * 1024 * 1024
MAX_VIRTUAL_IMAGE_BYTES = 20 * 1024 * 1024 * 1024
FORBIDDEN_PROJECT_HEX = frozenset({"e4f1487023f047b59901f28a91876e7b"})
APPROVED_PROJECT_ID = "53ec2dd9a1f7471fb6a2b174595fa232"

APPROVED_KERNEL_SHA256 = "89f7d4f31f6ef77d0f8d45810de9e19e3f8dededf3dd6ebf89bb540d26d8c0fd"
APPROVED_KERNEL_CONFIG_SHA256 = "4d4aaaed367bd2fb6ed1238b94ea9bb095a5e5a0d0cc67d1cb70595643cc795a"

KERNEL_ENV = "PALIMPSEST_KVM_KERNEL"
KERNEL_CONFIG_ENV = "PALIMPSEST_KVM_KERNEL_CONFIG"
RUN_ID_ENV = "GITHUB_RUN_ID"
RUN_ATTEMPT_ENV = "GITHUB_RUN_ATTEMPT"

DEFAULT_DEADLINE_SECONDS = 2700
MAX_DEADLINE_SECONDS = 2700
DEFAULT_CLEANUP_DEADLINE_SECONDS = 600
POLL_INTERVAL_SECONDS = 2.0

EGRESS_IP_URL = "https://checkip.amazonaws.com"

EXPECTED_OCI_FS_EVIDENCE_FILES = (
    "squashfs.json",
    "squashfs-replay.json",
    "erofs.json",
)

EXPECTED_STAGE1_KVM_EVIDENCE_FILES = (
    "console.bin",
    "retained-console.bin",
    "uid0-isolation-console.bin",
    "receipt.json",
    "negative-writable_transport.bin",
    "negative-missing_root.bin",
    "negative-wrong_root_serial.bin",
    "negative-readonly_root.bin",
    "negative-root_size_smaller.bin",
    "negative-root_size_larger.bin",
    "negative-missing_lower.bin",
    "negative-wrong_lower_serial.bin",
    "negative-writable_lower.bin",
    "negative-lower_size_smaller.bin",
    "negative-lower_size_larger.bin",
    "negative-duplicate_serial.bin",
    "negative-extra_disk.bin",
    "filesystem-negative-root_bad_magic.bin",
    "filesystem-negative-root_wrong_label.bin",
    "filesystem-negative-root_geometry.bin",
    "filesystem-negative-lower_bad_magic.bin",
    "filesystem-negative-lower_bad_structure.bin",
    "filesystem-negative-lower_digest_mismatch.bin",
    "assembly-negative-probe_missing.bin",
    "assembly-negative-probe_size_mismatch.bin",
    "assembly-negative-probe_digest_mismatch.bin",
    "root-transition-negative-transition_dev_not_directory.bin",
    "root-transition-negative-transition_sys_not_directory.bin",
    "root-transition-negative-transition_proc_not_directory.bin",
    "workload-negative-workload_missing_executable.bin",
    "workload-negative-workload_non_executable.bin",
    "workload-negative-workload_missing_cwd.bin",
    "workload-negative-workload_missing_user.bin",
    "workload-negative-workload_missing_group.bin",
    "lifecycle-negative-lifecycle_missing_port.bin",
    "lifecycle-negative-lifecycle_wrong_name_only.bin",
    "lifecycle-negative-hello_zero_length.bin",
    "lifecycle-negative-hello_oversized_length.bin",
    "lifecycle-negative-hello_duplicate_key_noncanonical.bin",
    "lifecycle-negative-hello_wrong_domain_core_binding.bin",
    "lifecycle-negative-hello_reused_nonce.bin",
    "lifecycle-negative-stop_stale_generation.bin",
    "lifecycle-negative-stop_request_id_collides_with_hello.bin",
    "lifecycle-negative-second_distinct_stop.bin",
    "qemu-duplicate-lifecycle-name.bin",
)

_TAG_REF_RE = re.compile(r"^refs/tags/v[0-9A-Za-z._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_UUID_HEX_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SSH_ED25519_LINE_RE = re.compile(r"(?:^|\s)ssh-ed25519\s+([A-Za-z0-9+/=]{40,})(?:\s|$)", re.MULTILINE)
_SSH_ED25519_FP_RE = re.compile(
    r"SHA256:([A-Za-z0-9+/]{43})(?:=)?[^\r\n]*(?:ED25519|ed25519)",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_ENV_KEYS = (
    "OS_APPLICATION_CREDENTIAL_SECRET",
    "OS_APPLICATION_CREDENTIAL_ID",
    "OS_PASSWORD",
    "OS_TOKEN",
    "OS_ACCESS_TOKEN",
    "OS_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "PALIMPSEST_TOKEN",
)
_FORBIDDEN_ARGV_PREFIXES = (
    "--os-",
    "--password",
    "--token",
    "--secret",
    "--application-credential",
    "--private-key",
)

CommandResult = subprocess.CompletedProcess[bytes]
CommandRunner = Callable[[Sequence[str], float], CommandResult]
CloudFactory = Callable[[Mapping[str, str]], Any]
EgressIPResolver = Callable[[float], str]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


class NativeOpenStackError(RuntimeError):
    """Fail-closed error for native OpenStack KVM proof and resource cleanup."""


class NativeProofTimeoutError(NativeOpenStackError):
    """An operation or proof step exceeded the remaining deadline."""


class NativeProofInterrupted(NativeOpenStackError):
    """A termination signal interrupted the native OpenStack KVM proof."""


@dataclass(frozen=True, slots=True)
class ProveRequest:
    repository: str
    event: str
    ref: str
    sha: str
    source_tar: Path
    source_sha256: str
    project_id: str
    image_id: str
    flavor_id: str
    network_id: str
    evidence_dir: Path
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    repository: str
    run_id: str
    run_attempt: str
    source_sha: str
    source_sha256: str
    project_id: str

    @property
    def owner_id(self) -> str:
        return f"{self.repository}/{self.run_id}/{self.run_attempt}"

    @property
    def token(self) -> str:
        digest = hashlib.sha256(
            f"{self.owner_id}:{self.source_sha}:{self.source_sha256}:{_normalize_uuid_hex(self.project_id)}".encode(
                "ascii"
            )
        ).hexdigest()
        return digest[:10]

    @property
    def prefix(self) -> str:
        return f"palimpsest-ci-{self.run_id}-{self.run_attempt}-{self.token}"

    @property
    def keypair_name(self) -> str:
        return f"{self.prefix}-key"

    @property
    def security_group_name(self) -> str:
        return f"{self.prefix}-sg"

    @property
    def port_name(self) -> str:
        return f"{self.prefix}-port"

    @property
    def volume_name(self) -> str:
        return f"{self.prefix}-vol"

    @property
    def server_name(self) -> str:
        return f"{self.prefix}-vm"

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "palimpsest_schema": MANIFEST_SCHEMA,
            "palimpsest_owner": self.owner_id,
            "palimpsest_repository": self.repository,
            "palimpsest_run_id": self.run_id,
            "palimpsest_run_attempt": self.run_attempt,
            "palimpsest_source_sha": self.source_sha,
            "palimpsest_source_sha256": self.source_sha256,
            "palimpsest_project_id": _normalize_uuid_hex(self.project_id),
        }

    @property
    def description(self) -> str:
        # Complete operation identity, within Neutron/Cinder's 255-byte limit.
        return f"p-ci-v1:{self.owner_id}:{_normalize_uuid_hex(self.project_id)}:{self.source_sha}:{self.source_sha256}"

    @property
    def tags(self) -> tuple[str, ...]:
        # Bind the full public identity without exceeding Neutron's 60-byte tags.
        digest = hashlib.sha256(self.description.encode("ascii")).hexdigest()
        return (f"p-ci-v1-a:{digest[:32]}", f"p-ci-v1-b:{digest[32:]}")


@dataclass(frozen=True, slots=True)
class ResourceManifest:
    schema: str
    repository: str
    run_id: str
    run_attempt: str
    source_sha: str
    source_sha256: str
    project_id: str
    server_id: str | None
    volume_id: str | None
    port_id: str | None
    security_group_id: str | None
    keypair_name: str | None
    created_at: str
    cleanup_verified: bool
    inflight: str | None = None

    @property
    def owner(self) -> OwnerIdentity:
        return OwnerIdentity(
            repository=self.repository,
            run_id=self.run_id,
            run_attempt=self.run_attempt,
            source_sha=self.source_sha,
            source_sha256=self.source_sha256,
            project_id=self.project_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repository": self.repository,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "source_sha": self.source_sha,
            "source_sha256": self.source_sha256,
            "project_id": self.project_id,
            "server_id": self.server_id,
            "volume_id": self.volume_id,
            "port_id": self.port_id,
            "security_group_id": self.security_group_id,
            "keypair_name": self.keypair_name,
            "created_at": self.created_at,
            "cleanup_verified": self.cleanup_verified,
            "inflight": self.inflight,
        }


MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "repository",
        "run_id",
        "run_attempt",
        "source_sha",
        "source_sha256",
        "project_id",
        "server_id",
        "volume_id",
        "port_id",
        "security_group_id",
        "keypair_name",
        "created_at",
        "cleanup_verified",
        "inflight",
    }
)


def _bounded_operation(function: Callable) -> Callable:
    """Wall-clock bound also interrupts blocked SDK calls, not just polling sleeps."""

    @wraps(function)
    def bounded(*args: Any, **kwargs: Any) -> Any:
        first = args[0] if args else kwargs.get("request")
        seconds = (
            first.deadline_seconds
            if isinstance(first, ProveRequest)
            else kwargs.get("deadline_seconds", DEFAULT_CLEANUP_DEADLINE_SECONDS)
        )
        if type(seconds) is not int or not 1 <= seconds <= MAX_DEADLINE_SECONDS:
            raise NativeOpenStackError("deadline-seconds is out of bounds")
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

        def expired(_signum: int, _frame: Any) -> None:
            raise NativeProofTimeoutError("operation deadline exceeded")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return function(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    return bounded


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_uuid_hex(value: str) -> str:
    cleaned = value.strip().lower()
    if _UUID_HEX_RE.fullmatch(cleaned) is None:
        raise NativeOpenStackError("identifier must be a canonical UUID or 32-digit hexadecimal ID")
    return cleaned.replace("-", "")


def _sanitize_text(text: str, env: Mapping[str, str] | None = None) -> str:
    sanitized = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED PRIVATE KEY]", text)
    if env is not None:
        for key in _SECRET_ENV_KEYS:
            secret = env.get(key)
            if secret and len(secret) >= 4:
                sanitized = sanitized.replace(secret, "[REDACTED]")
        for key, value in env.items():
            if key.upper().startswith("OS_") and any(part in key.upper() for part in ("SECRET", "PASSWORD", "TOKEN")):
                if value and len(value) >= 4:
                    sanitized = sanitized.replace(value, "[REDACTED]")
    return sanitized


def _remaining_seconds(deadline: float, clock: Clock, action: str) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise NativeProofTimeoutError(f"operation deadline exceeded while {action}")
    return remaining


def _default_run_command(command: Sequence[str], timeout_seconds: float) -> CommandResult:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            env={key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ},
            timeout=max(0.1, timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise NativeProofTimeoutError(f"command timed out: {command[0]}") from exc
    except OSError as exc:
        raise NativeOpenStackError(f"command failed to start: {command[0]}: {exc}") from exc


def _expect_command(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    deadline: float,
    clock: Clock,
    action: str,
    env: Mapping[str, str] | None = None,
    step_timeout: float | None = None,
) -> CommandResult:
    remaining = _remaining_seconds(deadline, clock, action)
    timeout = min(remaining, step_timeout) if step_timeout is not None else remaining
    try:
        result = runner(tuple(command), timeout)
    except subprocess.TimeoutExpired as exc:
        raise NativeProofTimeoutError(f"operation deadline exceeded while {action}") from exc
    if result.returncode != 0:
        detail = (
            "\n".join(
                stream.decode("utf-8", errors="replace").strip()
                for stream in (result.stdout, result.stderr)
                if stream.strip()
            )
            or f"exit status {result.returncode}"
        )
        raise NativeOpenStackError(f"{action} failed: {_sanitize_text(detail, env)}")
    return result


def _read_regular_file_sha256(path: Path, *, label: str, max_bytes: int = 512 * 1024 * 1024) -> tuple[str, int]:
    try:
        visible = path.lstat()
    except OSError as exc:
        raise NativeOpenStackError(f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(visible.st_mode):
        raise NativeOpenStackError(f"{label} must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise NativeOpenStackError(f"{label} cannot be opened safely: {path}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
            raise NativeOpenStackError(f"{label} changed while opening: {path}")
        if opened.st_size < 1 or opened.st_size > max_bytes:
            raise NativeOpenStackError(f"{label} size {opened.st_size} is out of bounds")
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise NativeOpenStackError(f"{label} was truncated while reading")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise NativeOpenStackError(f"{label} grew while reading")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise NativeOpenStackError(f"{label} changed while reading")
        return digest.hexdigest(), opened.st_size
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    if path.exists() and path.is_symlink():
        raise NativeOpenStackError(f"refusing to write symlinked path: {path}")
    parent = path.parent
    if parent.exists() and parent.is_symlink():
        raise NativeOpenStackError(f"refusing to use symlinked directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=os.fspath(parent))
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(temp_path, path)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_manifest(path: Path, manifest: ResourceManifest) -> ResourceManifest:
    if manifest.inflight is not None:
        intent_path = path.with_name(f"{path.name}.creation-intent.json")
        if not intent_path.exists() and not intent_path.is_symlink():
            # Retain an independent durable indication that create may have run.
            # Losing the mutable manifest must not become a pre-create no-op.
            _atomic_write_json(intent_path, manifest.to_dict())
    _atomic_write_json(path, manifest.to_dict())
    return manifest


def _load_manifest(path: Path, env: Mapping[str, str] | None = None) -> ResourceManifest:
    try:
        visible = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise NativeOpenStackError(f"manifest cannot be inspected: {path}") from exc
    if not stat.S_ISREG(visible.st_mode):
        raise NativeOpenStackError(f"manifest must be a regular file: {path}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (visible.st_dev, visible.st_ino)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or opened.st_mode & 0o077
                or not 1 <= opened.st_size <= 16384
            ):
                raise NativeOpenStackError("manifest changed, is not private, or exceeds size limit")
            payload = stream.read(16385)
            after = os.fstat(stream.fileno())
            if len(payload) != opened.st_size or (opened.st_mtime_ns, opened.st_size) != (
                after.st_mtime_ns,
                after.st_size,
            ):
                raise NativeOpenStackError("manifest changed while reading")
            raw = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeOpenStackError(f"manifest is not valid JSON: {path}") from exc
    if not isinstance(raw, dict) or set(raw) != MANIFEST_FIELDS:
        raise NativeOpenStackError("manifest fields do not match palimpsest.native-openstack.v1")
    if raw.get("schema") != MANIFEST_SCHEMA:
        raise NativeOpenStackError("manifest schema is invalid")
    repository = raw.get("repository")
    run_id = raw.get("run_id")
    run_attempt = raw.get("run_attempt")
    source_sha = raw.get("source_sha")
    source_sha256 = raw.get("source_sha256")
    project_id = raw.get("project_id")
    created_at = raw.get("created_at")
    cleanup_verified = raw.get("cleanup_verified")
    if repository != APPROVED_REPOSITORY:
        raise NativeOpenStackError("manifest repository is not approved")
    if not isinstance(run_id, str) or _RUN_NUMBER_RE.fullmatch(run_id) is None:
        raise NativeOpenStackError("manifest run_id is invalid")
    if not isinstance(run_attempt, str) or _RUN_NUMBER_RE.fullmatch(run_attempt) is None:
        raise NativeOpenStackError("manifest run_attempt is invalid")
    if env is not None:
        env_run_id = env.get(RUN_ID_ENV)
        env_run_attempt = env.get(RUN_ATTEMPT_ENV)
        if env_run_id is not None and env_run_id != run_id:
            raise NativeOpenStackError("manifest run_id does not match GITHUB_RUN_ID")
        if env_run_attempt is not None and env_run_attempt != run_attempt:
            raise NativeOpenStackError("manifest run_attempt does not match GITHUB_RUN_ATTEMPT")
    if not isinstance(source_sha, str) or _SHA_RE.fullmatch(source_sha) is None:
        raise NativeOpenStackError("manifest source_sha is invalid")
    if not isinstance(source_sha256, str) or _SHA256_RE.fullmatch(source_sha256) is None:
        raise NativeOpenStackError("manifest source_sha256 is invalid")
    if not isinstance(project_id, str):
        raise NativeOpenStackError("manifest project_id is invalid")
    normalized_project = _normalize_uuid_hex(project_id)
    if normalized_project in FORBIDDEN_PROJECT_HEX:
        raise NativeOpenStackError("manifest project_id is forbidden")
    if normalized_project != APPROVED_PROJECT_ID:
        raise NativeOpenStackError("manifest project_id is not the approved CI project")
    if not isinstance(created_at, str) or not created_at:
        raise NativeOpenStackError("manifest created_at is invalid")
    if type(cleanup_verified) is not bool:
        raise NativeOpenStackError("manifest cleanup_verified must be a boolean")

    def _optional_str(field_name: str) -> str | None:
        value = raw.get(field_name)
        if value is None:
            return None
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
            raise NativeOpenStackError(f"manifest {field_name} is invalid")
        return value

    manifest = ResourceManifest(
        schema=MANIFEST_SCHEMA,
        repository=repository,
        run_id=run_id,
        run_attempt=run_attempt,
        source_sha=source_sha,
        source_sha256=source_sha256,
        project_id=project_id,
        server_id=_optional_str("server_id"),
        volume_id=_optional_str("volume_id"),
        port_id=_optional_str("port_id"),
        security_group_id=_optional_str("security_group_id"),
        keypair_name=_optional_str("keypair_name"),
        created_at=created_at,
        cleanup_verified=cleanup_verified,
        inflight=_optional_str("inflight"),
    )
    if manifest.keypair_name is not None and manifest.keypair_name != manifest.owner.keypair_name:
        raise NativeOpenStackError("manifest keypair_name does not match derived owner keypair name")
    if manifest.inflight not in {None, "server_id", "volume_id", "port_id", "security_group_id", "keypair_name"}:
        raise NativeOpenStackError("invalid inflight operation")
    return manifest


def validate_prove_inputs(
    request: ProveRequest,
    env: Mapping[str, str],
    *,
    expected_kernel_sha256: str | None = None,
    expected_kernel_config_sha256: str | None = None,
) -> tuple[OwnerIdentity, Path, str, Path, str]:
    """Validate repository/event/ref allowlist, resource pins, and local file hashes before cloud access."""

    if request.repository != APPROVED_REPOSITORY:
        raise NativeOpenStackError(f"unapproved repository: {request.repository}")
    if request.event not in APPROVED_EVENTS:
        raise NativeOpenStackError(f"unapproved workflow event: {request.event}")
    if request.ref not in APPROVED_BRANCH_REFS and (".." in request.ref or _TAG_REF_RE.fullmatch(request.ref) is None):
        raise NativeOpenStackError(f"unapproved ref: {request.ref}")
    sha = request.sha.strip().lower()
    if _SHA_RE.fullmatch(sha) is None:
        raise NativeOpenStackError("sha must be a 40-character hexadecimal commit SHA")
    source_sha256 = request.source_sha256.strip().lower()
    if _SHA256_RE.fullmatch(source_sha256) is None:
        raise NativeOpenStackError("source-sha256 must be a 64-character hexadecimal SHA-256 digest")
    if type(request.deadline_seconds) is not int or not 1 <= request.deadline_seconds <= MAX_DEADLINE_SECONDS:
        raise NativeOpenStackError(f"deadline-seconds must be between 1 and {MAX_DEADLINE_SECONDS}")

    normalized_project = _normalize_uuid_hex(request.project_id)
    if normalized_project in FORBIDDEN_PROJECT_HEX:
        raise NativeOpenStackError("refusing to use admin or candidate Hub project for CI native proof")
    if normalized_project != APPROVED_PROJECT_ID:
        raise NativeOpenStackError("project-id is not the approved CI project")
    _normalize_uuid_hex(request.image_id)
    if _normalize_uuid_hex(request.flavor_id) != _normalize_uuid_hex(APPROVED_FLAVOR_ID):
        raise NativeOpenStackError(f"flavor-id must be pinned cpu.2c_8g ({APPROVED_FLAVOR_ID})")
    if _normalize_uuid_hex(request.network_id) != _normalize_uuid_hex(APPROVED_NETWORK_ID):
        raise NativeOpenStackError(f"network-id must be pinned public_provider ({APPROVED_NETWORK_ID})")

    run_id = env.get(RUN_ID_ENV, "").strip()
    run_attempt = env.get(RUN_ATTEMPT_ENV, "").strip()
    if _RUN_NUMBER_RE.fullmatch(run_id) is None or _RUN_NUMBER_RE.fullmatch(run_attempt) is None:
        raise NativeOpenStackError("GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT must be positive integers")

    actual_source_sha256, _ = _read_regular_file_sha256(request.source_tar, label="source archive")
    if actual_source_sha256 != source_sha256:
        raise NativeOpenStackError("source archive SHA-256 does not match --source-sha256")

    kernel_raw = env.get(KERNEL_ENV, "").strip()
    config_raw = env.get(KERNEL_CONFIG_ENV, "").strip()
    if not kernel_raw or not config_raw or "\0" in kernel_raw or "\0" in config_raw:
        raise NativeOpenStackError(
            f"{KERNEL_ENV} and {KERNEL_CONFIG_ENV} must be set to local file paths before cloud resource creation"
        )
    kernel_path = Path(kernel_raw).resolve()
    config_path = Path(config_raw).resolve()
    if Path(kernel_raw).is_symlink() or Path(config_raw).is_symlink():
        raise NativeOpenStackError("kernel and kernel config paths must not be symlinks")

    pinned_kernel_sha256 = (expected_kernel_sha256 or APPROVED_KERNEL_SHA256).lower()
    pinned_config_sha256 = (expected_kernel_config_sha256 or APPROVED_KERNEL_CONFIG_SHA256).lower()
    actual_kernel_sha256, _ = _read_regular_file_sha256(kernel_path, label="KVM kernel", max_bytes=128 * 1024 * 1024)
    if actual_kernel_sha256 != pinned_kernel_sha256:
        raise NativeOpenStackError("KVM kernel SHA-256 does not match approved pin")
    actual_config_sha256, _ = _read_regular_file_sha256(
        config_path, label="KVM kernel config", max_bytes=4 * 1024 * 1024
    )
    if actual_config_sha256 != pinned_config_sha256:
        raise NativeOpenStackError("KVM kernel config SHA-256 does not match approved pin")

    owner = OwnerIdentity(
        repository=request.repository,
        run_id=run_id,
        run_attempt=run_attempt,
        source_sha=sha,
        source_sha256=source_sha256,
        project_id=request.project_id,
    )
    return owner, kernel_path, actual_kernel_sha256, config_path, actual_config_sha256


def _validate_openstack_env(env: Mapping[str, str]) -> dict[str, str]:
    os_env = {key: value for key, value in env.items() if key.startswith("OS_") and value}
    auth_url = os_env.get("OS_AUTH_URL", "").strip()
    endpoint = urlsplit(auth_url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:
        raise NativeOpenStackError("OS_AUTH_URL must be set to a verified HTTPS endpoint")
    insecure = os_env.get("OS_INSECURE", "").strip().lower()
    if insecure not in {"", "0", "false", "no"} or os_env.get("OS_VERIFY", "true").lower() in {"0", "false", "no"}:
        raise NativeOpenStackError("TLS verification cannot be disabled")
    has_app_cred = bool(os_env.get("OS_APPLICATION_CREDENTIAL_ID") and os_env.get("OS_APPLICATION_CREDENTIAL_SECRET"))
    if not has_app_cred or os_env.get("OS_AUTH_TYPE") != "v3applicationcredential":
        raise NativeOpenStackError("project-scoped v3applicationcredential OS_* credentials are required")
    if any(os_env.get(key) for key in ("OS_PASSWORD", "OS_TOKEN", "OS_ACCESS_TOKEN")):
        raise NativeOpenStackError("mixed privileged credentials are not permitted")
    return os_env


def _default_cloud_factory(env: Mapping[str, str]) -> Any:
    validated = _validate_openstack_env(env)
    try:
        from keystoneauth1 import session
        from keystoneauth1.identity.v3 import ApplicationCredential
        from openstack.connection import Connection
    except ImportError as exc:
        raise NativeOpenStackError("openstacksdk is required to connect to OpenStack") from exc
    auth = ApplicationCredential(
        auth_url=validated["OS_AUTH_URL"],
        application_credential_id=validated["OS_APPLICATION_CREDENTIAL_ID"],
        application_credential_secret=validated["OS_APPLICATION_CREDENTIAL_SECRET"],
    )
    bound_session = session.Session(auth=auth, verify=validated.get("OS_CACERT") or True, timeout=30)
    return Connection(
        session=bound_session,
        region_name=validated.get("OS_REGION_NAME", "RegionOne"),
        interface="public",
        compute_api_version="2.10",
        connect_retries=0,
        status_code_retries=0,
    )


def _authenticate_project_bound(
    cloud_factory: CloudFactory,
    env: Mapping[str, str],
    expected_project_id: str,
) -> tuple[Any, str, str]:
    _validate_openstack_env(env)
    expected_hex = _normalize_uuid_hex(expected_project_id)
    if expected_hex in FORBIDDEN_PROJECT_HEX:
        raise NativeOpenStackError("refusing to authenticate against forbidden project")
    if expected_hex != APPROVED_PROJECT_ID:
        raise NativeOpenStackError("project_id is not the approved CI project")
    try:
        conn = cloud_factory(env)
        actual_project_id = getattr(conn, "current_project_id", None)
        actual_user_id = getattr(conn, "current_user_id", None)
    except NativeOpenStackError:
        raise
    except Exception as exc:
        raise NativeOpenStackError(f"OpenStack authentication failed: {_sanitize_text(str(exc), env)}") from exc
    if not isinstance(actual_project_id, str) or not actual_project_id.strip():
        raise NativeOpenStackError("OpenStack connection did not report a project-scoped token")
    actual_hex = _normalize_uuid_hex(actual_project_id)
    if actual_hex in FORBIDDEN_PROJECT_HEX:
        raise NativeOpenStackError("authenticated OpenStack project is forbidden for CI")
    if actual_hex != expected_hex:
        raise NativeOpenStackError("authenticated OpenStack project does not match requested project_id")
    if not isinstance(actual_user_id, str) or not actual_user_id.strip():
        raise NativeOpenStackError("OpenStack connection did not report current_user_id")
    session = getattr(conn, "session", None)
    get_endpoint = getattr(session, "get_endpoint", None) if session is not None else None
    if not callable(get_endpoint):
        raise NativeOpenStackError("project-scoped volumev3 catalog endpoint is unavailable")
    if callable(get_endpoint):
        region_name = env.get("OS_REGION_NAME", "RegionOne").strip() or "RegionOne"
        try:
            cinder_endpoint = get_endpoint(
                service_type="volumev3",
                interface="public",
                region_name=region_name,
            )
        except Exception as exc:
            raise NativeOpenStackError(
                f"failed to resolve volumev3 catalog endpoint: {_sanitize_text(str(exc), env)}"
            ) from exc
        if not isinstance(cinder_endpoint, str) or not cinder_endpoint.startswith("https://"):
            raise NativeOpenStackError("volumev3 public catalog endpoint must be an HTTPS URL")
        endpoint = urlsplit(cinder_endpoint)
        project_segment = endpoint.path.rstrip("/").rsplit("/", 1)[-1].replace("-", "").lower()
        if project_segment in FORBIDDEN_PROJECT_HEX:
            raise NativeOpenStackError("volumev3 catalog endpoint points to forbidden project")
        if project_segment != expected_hex or endpoint.query or endpoint.fragment or endpoint.username:
            raise NativeOpenStackError("volumev3 catalog endpoint does not match requested project_id")
    return conn, actual_project_id, actual_user_id


def _is_not_found_exception(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status_code == 404:
        return True
    return exc.__class__.__name__ in {"NotFoundException", "ResourceNotFound"}


def _get_or_none(getter: Callable[[str], Any], identifier: str, env: Mapping[str, str] | None = None) -> Any:
    try:
        return getter(identifier)
    except Exception as exc:
        if _is_not_found_exception(exc):
            return None
        raise NativeOpenStackError(f"resource lookup failed: {_sanitize_text(str(exc), env)}") from exc


def _attr_or_key(resource: Any, name: str, default: Any = None) -> Any:
    if hasattr(resource, name):
        value = getattr(resource, name)
        if value is not None:
            return value
    if isinstance(resource, Mapping) and name in resource:
        return resource[name]
    return default


def _resource_project_hex(resource: Any) -> str | None:
    for key in ("project_id", "tenant_id", "os-vol-tenant-attr:tenant_id"):
        value = _attr_or_key(resource, key)
        if isinstance(value, str) and value.strip():
            return _normalize_uuid_hex(value)
    return None


def _verify_cloud_prerequisites(conn: Any, request: ProveRequest, env: Mapping[str, str]) -> str:
    for listing in (conn.compute.servers, conn.block_storage.volumes):
        if any(
            _resource_project_hex(resource) in {None, APPROVED_PROJECT_ID} for resource in _list_resources(listing, env)
        ):
            raise NativeOpenStackError(
                "CI project already has a server or volume; refusing concurrent native resources"
            )
    image = _get_or_none(conn.image.get_image, request.image_id, env)
    if image is None:
        raise NativeOpenStackError(f"pinned Glance image {request.image_id} was not found")
    image_owner = _attr_or_key(image, "owner_id") or _attr_or_key(image, "owner")
    if image_owner != APPROVED_PROJECT_ID or _attr_or_key(image, "visibility") != "private":
        raise NativeOpenStackError("Glance image must be the CI project's private copy")
    if _normalize_uuid_hex(str(_attr_or_key(image, "id", ""))) != _normalize_uuid_hex(request.image_id):
        raise NativeOpenStackError("Glance image ID does not match requested --image-id")
    if _attr_or_key(image, "status") != "active":
        raise NativeOpenStackError("Glance image is not active")
    hash_algo = (_attr_or_key(image, "hash_algo") or _attr_or_key(image, "os_hash_algo") or "").lower()
    hash_value = (_attr_or_key(image, "hash_value") or _attr_or_key(image, "os_hash_value") or "").lower()
    if hash_algo != "sha512" or hash_value != APPROVED_IMAGE_SHA512:
        raise NativeOpenStackError("Glance image SHA-512 does not match approved pin")
    image_size = _attr_or_key(image, "size")
    if type(image_size) is not int or not 1 <= image_size <= MAX_IMAGE_BYTES:
        raise NativeOpenStackError("Glance image size exceeds 4 GiB CI copy limit")
    virtual_size = _attr_or_key(image, "virtual_size")
    if virtual_size is not None and (type(virtual_size) is not int or virtual_size > MAX_VIRTUAL_IMAGE_BYTES):
        raise NativeOpenStackError("Glance image virtual_size exceeds 20 GiB boot volume limit")

    flavor = _get_or_none(conn.compute.get_flavor, request.flavor_id, env)
    if flavor is None:
        raise NativeOpenStackError(f"pinned flavor {request.flavor_id} was not found")
    if _normalize_uuid_hex(str(_attr_or_key(flavor, "id", ""))) != _normalize_uuid_hex(APPROVED_FLAVOR_ID):
        raise NativeOpenStackError("flavor ID does not match approved cpu.2c_8g pin")
    flavor_name = _attr_or_key(flavor, "name")
    if flavor_name is not None and flavor_name != APPROVED_FLAVOR_NAME:
        raise NativeOpenStackError(f"flavor name {flavor_name!r} does not match {APPROVED_FLAVOR_NAME!r}")
    if _attr_or_key(flavor, "vcpus") != APPROVED_FLAVOR_VCPUS or _attr_or_key(flavor, "ram") != APPROVED_FLAVOR_RAM_MIB:
        raise NativeOpenStackError("flavor resources do not match 2 vCPU / 8192 MiB constraint")

    network = _get_or_none(conn.network.get_network, request.network_id, env)
    if network is None:
        raise NativeOpenStackError(f"pinned network {request.network_id} was not found")
    if _normalize_uuid_hex(str(_attr_or_key(network, "id", ""))) != _normalize_uuid_hex(APPROVED_NETWORK_ID):
        raise NativeOpenStackError("network ID does not match approved public_provider pin")
    network_status = _attr_or_key(network, "status")
    if network_status is not None and network_status != "ACTIVE":
        raise NativeOpenStackError("pinned network is not ACTIVE")

    return hash_value


def _validate_public_egress_ipv4(raw_ip: str) -> str:
    try:
        parsed = ipaddress.IPv4Address(raw_ip.strip())
    except ipaddress.AddressValueError as exc:
        raise NativeOpenStackError(f"egress address is not a valid IPv4 address: {raw_ip!r}") from exc
    if (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_unspecified
        or parsed.is_reserved
    ):
        raise NativeOpenStackError(f"egress IPv4 {parsed} is not a routable public IPv4 address")
    return str(parsed)


def _default_resolve_egress_ipv4(timeout_seconds: float) -> str:
    context = ssl.create_default_context()
    request = urllib.request.Request(EGRESS_IP_URL, headers={"User-Agent": "palimpsest-native-kvm/1.0"})
    with urllib.request.urlopen(request, timeout=min(10.0, timeout_seconds), context=context) as response:
        if urlsplit(response.url).scheme != "https":
            raise NativeOpenStackError("egress lookup redirected outside HTTPS")
        payload = response.read(128).decode("ascii", errors="strict").strip()
    return _validate_public_egress_ipv4(payload)


def _extract_port_ipv4(port: Any) -> str:
    fixed_ips = _attr_or_key(port, "fixed_ips")
    if not isinstance(fixed_ips, Sequence):
        raise NativeOpenStackError("created Neutron port has no fixed_ips")
    for entry in fixed_ips:
        if isinstance(entry, Mapping):
            ip_value = entry.get("ip_address")
            if isinstance(ip_value, str):
                try:
                    addr = ipaddress.ip_address(ip_value)
                except ValueError:
                    continue
                if isinstance(addr, ipaddress.IPv4Address) and not addr.is_unspecified:
                    return str(addr)
    raise NativeOpenStackError("created Neutron port did not receive an IPv4 address")


def _ed25519_fingerprint_from_key_base64(key_b64: str) -> str:
    try:
        raw = base64.b64decode(key_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise NativeOpenStackError("invalid base64 SSH ED25519 public key") from exc
    if not raw.startswith(b"\x00\x00\x00\x0bssh-ed25519"):
        raise NativeOpenStackError("decoded SSH public key is not ssh-ed25519")
    digest_b64 = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest_b64}"


def extract_console_ed25519_identity(console_text: str) -> tuple[str, str | None]:
    """Extract the first ED25519 host key fingerprint (and optional public key) from Nova console output."""

    key_match = _SSH_ED25519_LINE_RE.search(console_text)
    fp_match = _SSH_ED25519_FP_RE.search(console_text)
    derived_fp: str | None = None
    key_b64: str | None = None
    if key_match is not None:
        key_b64 = key_match.group(1)
        derived_fp = _ed25519_fingerprint_from_key_base64(key_b64)
    explicit_fp = f"SHA256:{fp_match.group(1)}" if fp_match is not None else None
    if derived_fp is not None and explicit_fp is not None and derived_fp != explicit_fp:
        raise NativeOpenStackError("Nova console output reported conflicting ED25519 public key and SHA256 fingerprint")
    fingerprint = derived_fp or explicit_fp
    if fingerprint is None:
        raise NativeOpenStackError("Nova console output does not yet contain an ED25519 host key fingerprint")
    return fingerprint, key_b64


def verify_scanned_ssh_host_key(
    server_ip: str,
    scan_output: str,
    expected_fingerprint: str,
    expected_key_b64: str | None = None,
) -> str:
    """Verify ssh-keyscan output against the Nova console ED25519 fingerprint and return a known_hosts line."""

    matched_keys: list[str] = []
    for raw_line in scan_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3 or parts[1] != "ssh-ed25519":
            continue
        hosts = parts[0].split(",")
        if server_ip not in hosts:
            continue
        matched_keys.append(parts[2])
    if len(matched_keys) != 1:
        raise NativeOpenStackError("ssh-keyscan did not return a single ssh-ed25519 host key for target IP")
    scanned_b64 = matched_keys[0]
    actual_fingerprint = _ed25519_fingerprint_from_key_base64(scanned_b64)
    if actual_fingerprint != expected_fingerprint:
        raise NativeOpenStackError(
            f"scanned SSH host key fingerprint {actual_fingerprint} does not match Nova console {expected_fingerprint}"
        )
    if expected_key_b64 is not None and scanned_b64 != expected_key_b64:
        raise NativeOpenStackError("scanned SSH host key payload does not match Nova console host key")
    return f"{server_ip} ssh-ed25519 {scanned_b64}\n"


def _apply_neutron_tags_if_supported(conn: Any, resource: Any, tags: Sequence[str]) -> None:
    set_tags = getattr(conn.network, "set_tags", None)
    if callable(set_tags):
        set_tags(resource, list(tags))


def _list_resources(listing_fn: Callable[..., Iterable[Any]], env: Mapping[str, str] | None = None) -> list[Any]:
    try:
        return list(listing_fn())
    except Exception as exc:
        raise NativeOpenStackError(f"resource enumeration failed: {_sanitize_text(str(exc), env)}") from exc


def _matches_owner_metadata(resource: Any, owner: OwnerIdentity) -> bool:
    project_hex = _resource_project_hex(resource)
    if project_hex is not None and project_hex != _normalize_uuid_hex(owner.project_id):
        return False
    metadata = _attr_or_key(resource, "metadata")
    if not isinstance(metadata, Mapping):
        return False
    expected = owner.metadata
    return all(metadata.get(key) == value for key, value in expected.items())


def _matches_owner_description_and_tags(resource: Any, owner: OwnerIdentity) -> bool:
    project_hex = _resource_project_hex(resource)
    if project_hex != _normalize_uuid_hex(owner.project_id):
        return False
    description = _attr_or_key(resource, "description")
    if description != owner.description:
        return False
    tags = _attr_or_key(resource, "tags")
    if isinstance(tags, Sequence) and not isinstance(tags, (str, bytes)) and len(tags) > 0:
        if not set(owner.tags).issubset(set(tags)):
            return False
    return True


def _discover_inflight_resources(
    conn: Any,
    manifest: ResourceManifest,
    manifest_path: Path,
    current_user_id: str,
    env: Mapping[str, str] | None = None,
    *,
    require_absent: bool = False,
) -> ResourceManifest:
    owner = manifest.owner
    for field, name, listing, matches in (
        ("server_id", owner.server_name, conn.compute.servers, _matches_owner_metadata),
        ("volume_id", owner.volume_name, conn.block_storage.volumes, _matches_owner_metadata),
        ("port_id", owner.port_name, conn.network.ports, _matches_owner_description_and_tags),
        (
            "security_group_id",
            owner.security_group_name,
            conn.network.security_groups,
            _matches_owner_description_and_tags,
        ),
    ):
        candidates = []
        for resource in _list_resources(listing, env):
            named = _attr_or_key(resource, "name") == name
            owned = matches(resource, owner)
            if named and not owned:
                raise NativeOpenStackError(f"{field}: ownership mismatch for operation name")
            if named or owned:
                candidates.append(resource)
        if len(candidates) > 1:
            raise NativeOpenStackError(f"ambiguous inflight {field}")
        if candidates:
            if require_absent:
                raise NativeOpenStackError(f"owned {field} remains after cleanup")
            identifier = str(_attr_or_key(candidates[0], "id", ""))
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identifier) is None:
                raise NativeOpenStackError(f"invalid discovered {field}")
            recorded = getattr(manifest, field)
            if recorded is not None and recorded != identifier:
                raise NativeOpenStackError(f"{field}: recorded and discovered identity mismatch")
            if recorded is None:
                manifest = _write_manifest(manifest_path, replace(manifest, **{field: identifier}))
    keypair = _get_or_none(conn.compute.get_keypair, owner.keypair_name, env)
    if keypair is not None:
        if _attr_or_key(keypair, "user_id") != current_user_id:
            raise NativeOpenStackError("keypair user_id or name mismatch")
        if require_absent:
            raise NativeOpenStackError("owned keypair remains after cleanup")
        if manifest.keypair_name is None:
            manifest = _write_manifest(manifest_path, replace(manifest, keypair_name=owner.keypair_name))
    return manifest


def _wait_until_absent(
    getter: Callable[[str], Any],
    identifier: str,
    *,
    label: str,
    deadline: float,
    clock: Clock,
    sleeper: Sleeper,
    env: Mapping[str, str] | None = None,
) -> None:
    while True:
        resource = _get_or_none(getter, identifier, env)
        if resource is None:
            return
        if str(_attr_or_key(resource, "status", "")).lower() == "error_deleting":
            raise NativeOpenStackError(f"{label} entered error_deleting")
        remaining = _remaining_seconds(deadline, clock, f"waiting for {label} {identifier} deletion")
        sleeper(min(POLL_INTERVAL_SECONDS, remaining))


@_bounded_operation
def cleanup_manifest(
    manifest_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    cloud_factory: CloudFactory = _default_cloud_factory,
    deadline_seconds: int = DEFAULT_CLEANUP_DEADLINE_SECONDS,
    clock: Clock = time.monotonic,
    sleeper: Sleeper = time.sleep,
) -> ResourceManifest | None:
    """Reclaim exact-owned cloud resources recorded in or recoverable from the manifest."""
    if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= MAX_DEADLINE_SECONDS:
        raise NativeOpenStackError("cleanup deadline-seconds is out of bounds")

    effective_env = dict(os.environ if env is None else env)
    try:
        manifest_path.lstat()
    except FileNotFoundError:
        intent_path = manifest_path.with_name(f"{manifest_path.name}.creation-intent.json")
        if intent_path.exists() or intent_path.is_symlink():
            raise NativeOpenStackError(
                "resource manifest is missing after creation intent; cleanup outcome is uncertain"
            ) from None
        return None

    manifest = _load_manifest(manifest_path, effective_env)
    if manifest.cleanup_verified:
        manifest = _write_manifest(manifest_path, replace(manifest, cleanup_verified=False))

    deadline = clock() + max(1, deadline_seconds)
    conn, _, current_user_id = _authenticate_project_bound(cloud_factory, effective_env, manifest.project_id)
    manifest = _discover_inflight_resources(conn, manifest, manifest_path, current_user_id, effective_env)
    if manifest.inflight is not None and getattr(manifest, manifest.inflight) is not None:
        manifest = _write_manifest(manifest_path, replace(manifest, inflight=None))
    owner = manifest.owner

    if manifest.server_id is not None:
        server = _get_or_none(conn.compute.get_server, manifest.server_id, effective_env)
        if server is not None:
            if (
                str(_attr_or_key(server, "id", "")) != manifest.server_id
                or _attr_or_key(server, "name") != owner.server_name
                or _resource_project_hex(server) != _normalize_uuid_hex(owner.project_id)
                or not _matches_owner_metadata(server, owner)
            ):
                raise NativeOpenStackError(f"refusing to delete server {manifest.server_id}: ownership mismatch")
            try:
                conn.compute.delete_server(manifest.server_id, ignore_missing=False)
            except Exception as exc:
                raise NativeOpenStackError(
                    f"server deletion failed for {manifest.server_id}: {_sanitize_text(str(exc), effective_env)}"
                ) from exc
        _wait_until_absent(
            conn.compute.get_server,
            manifest.server_id,
            label="server",
            deadline=deadline,
            clock=clock,
            sleeper=sleeper,
            env=effective_env,
        )

    if manifest.volume_id is not None:
        while True:
            volume = _get_or_none(conn.block_storage.get_volume, manifest.volume_id, effective_env)
            if volume is None:
                break
            if (
                str(_attr_or_key(volume, "id", "")) != manifest.volume_id
                or _attr_or_key(volume, "name") != owner.volume_name
                or not _matches_owner_metadata(volume, owner)
            ):
                raise NativeOpenStackError(f"refusing to delete volume {manifest.volume_id}: ownership mismatch")
            attachments = _attr_or_key(volume, "attachments") or ()
            if not isinstance(attachments, Sequence):
                raise NativeOpenStackError(f"volume {manifest.volume_id} has invalid attachments metadata")
            for attachment in attachments:
                attached_server = _attr_or_key(attachment, "server_id")
                if attached_server not in (None, "", manifest.server_id):
                    raise NativeOpenStackError(
                        f"refusing to delete volume {manifest.volume_id}: attached to foreign server {attached_server}"
                    )
            status = str(_attr_or_key(volume, "status", "")).lower()
            if status in {"creating", "downloading", "in-use", "detaching", "attaching"} or len(attachments) > 0:
                remaining = _remaining_seconds(deadline, clock, f"waiting for volume {manifest.volume_id} detachment")
                sleeper(min(POLL_INTERVAL_SECONDS, remaining))
                continue
            try:
                conn.block_storage.delete_volume(manifest.volume_id, ignore_missing=False)
            except Exception as exc:
                raise NativeOpenStackError(
                    f"volume deletion failed for {manifest.volume_id}: {_sanitize_text(str(exc), effective_env)}"
                ) from exc
            break
        _wait_until_absent(
            conn.block_storage.get_volume,
            manifest.volume_id,
            label="volume",
            deadline=deadline,
            clock=clock,
            sleeper=sleeper,
            env=effective_env,
        )

    if manifest.port_id is not None:
        port = _get_or_none(conn.network.get_port, manifest.port_id, effective_env)
        if port is not None:
            device_id = _attr_or_key(port, "device_id")
            if (
                str(_attr_or_key(port, "id", "")) != manifest.port_id
                or _attr_or_key(port, "name") != owner.port_name
                or not _matches_owner_description_and_tags(port, owner)
                or device_id not in (None, "", manifest.server_id)
            ):
                raise NativeOpenStackError(f"refusing to delete port {manifest.port_id}: ownership mismatch")
            try:
                conn.network.delete_port(manifest.port_id, ignore_missing=False)
            except Exception as exc:
                raise NativeOpenStackError(
                    f"port deletion failed for {manifest.port_id}: {_sanitize_text(str(exc), effective_env)}"
                ) from exc
        _wait_until_absent(
            conn.network.get_port,
            manifest.port_id,
            label="port",
            deadline=deadline,
            clock=clock,
            sleeper=sleeper,
            env=effective_env,
        )

    if manifest.security_group_id is not None:
        sg = _get_or_none(conn.network.get_security_group, manifest.security_group_id, effective_env)
        if sg is not None:
            sg_name = _attr_or_key(sg, "name")
            if (
                str(_attr_or_key(sg, "id", "")) != manifest.security_group_id
                or sg_name == "default"
                or sg_name != owner.security_group_name
                or not _matches_owner_description_and_tags(sg, owner)
            ):
                raise NativeOpenStackError(
                    f"refusing to delete security group {manifest.security_group_id}: ownership mismatch"
                )
            all_ports = _list_resources(conn.network.ports, effective_env)
            bound_ports = [
                str(_attr_or_key(port_item, "id", ""))
                for port_item in all_ports
                if manifest.security_group_id in (_attr_or_key(port_item, "security_group_ids") or ())
            ]
            if bound_ports:
                raise NativeOpenStackError(
                    f"refusing to delete security group {manifest.security_group_id}: still bound to ports {bound_ports}"
                )
            try:
                conn.network.delete_security_group(manifest.security_group_id, ignore_missing=False)
            except Exception as exc:
                raise NativeOpenStackError(
                    f"security group deletion failed for {manifest.security_group_id}: "
                    f"{_sanitize_text(str(exc), effective_env)}"
                ) from exc
        _wait_until_absent(
            conn.network.get_security_group,
            manifest.security_group_id,
            label="security group",
            deadline=deadline,
            clock=clock,
            sleeper=sleeper,
            env=effective_env,
        )

    if manifest.keypair_name is not None:
        if manifest.keypair_name != owner.keypair_name:
            raise NativeOpenStackError("refusing to delete keypair with non-owner name")
        kp = _get_or_none(conn.compute.get_keypair, manifest.keypair_name, effective_env)
        if kp is not None:
            if _attr_or_key(kp, "name") != owner.keypair_name or _attr_or_key(kp, "user_id") != current_user_id:
                raise NativeOpenStackError(
                    f"refusing to delete keypair {manifest.keypair_name}: user_id or name mismatch"
                )
            try:
                conn.compute.delete_keypair(manifest.keypair_name, ignore_missing=False)
            except Exception as exc:
                raise NativeOpenStackError(
                    f"keypair deletion failed for {manifest.keypair_name}: {_sanitize_text(str(exc), effective_env)}"
                ) from exc
        _wait_until_absent(
            conn.compute.get_keypair,
            manifest.keypair_name,
            label="keypair",
            deadline=deadline,
            clock=clock,
            sleeper=sleeper,
            env=effective_env,
        )

    _discover_inflight_resources(conn, manifest, manifest_path, current_user_id, effective_env, require_absent=True)
    if manifest.inflight is not None:
        raise NativeOpenStackError("creation outcome uncertain; exact owner discovery must be repeated before success")
    verified_manifest = _write_manifest(manifest_path, replace(manifest, cleanup_verified=True))
    return verified_manifest


def _build_remote_proof_script(
    *,
    source_sha256: str,
    kernel_sha256: str,
    kernel_config_sha256: str,
) -> str:
    return "\n".join(
        (
            "set -euo pipefail",
            "umask 0022",
            'test "$(uname -s)" = "Linux"',
            'test "$(uname -m)" = "x86_64"',
            "test -c /dev/kvm",
            'REMOTE_ROOT="/tmp/palimpsest-native-proof"',
            'UPLOAD_DIR="/tmp/palimpsest-native-proof-upload"',
            'rm -rf "$REMOTE_ROOT"',
            'install -d -m 0700 "$REMOTE_ROOT" "$REMOTE_ROOT/pinned"',
            f'printf "%s  %s\\n" {shlex.quote(source_sha256)} "$UPLOAD_DIR/source.tar" | sha256sum --check --strict',
            f'printf "%s  %s\\n" {shlex.quote(kernel_sha256)} "$UPLOAD_DIR/vmlinuz" | sha256sum --check --strict',
            (
                f'printf "%s  %s\\n" {shlex.quote(kernel_config_sha256)} "$UPLOAD_DIR/kernel.config" '
                "| sha256sum --check --strict"
            ),
            'install -m 0400 "$UPLOAD_DIR/vmlinuz" "$REMOTE_ROOT/pinned/vmlinuz"',
            'install -m 0400 "$UPLOAD_DIR/kernel.config" "$REMOTE_ROOT/pinned/kernel.config"',
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update",
            (
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends "
                "acl ca-certificates curl erofs-utils python3 python3-venv qemu-system-x86 squashfs-tools"
            ),
            "if ! command -v uv >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi",
            'export PATH="$HOME/.local/bin:$PATH"',
            'sudo usermod --append --groups kvm "$(id -un)"',
            (
                'sudo -u "$(id -un)" python3 -c \'import fcntl, os, stat; fd = os.open("/dev/kvm", os.O_RDWR); '
                "assert stat.S_ISCHR(os.fstat(fd).st_mode); "
                "assert fcntl.ioctl(fd, 0xAE00, 0) == 12; os.close(fd)'"
            ),
            'QEMU_BIN="$(realpath "$(command -v qemu-system-x86_64)")"',
            'install -d -m 0755 "$REMOTE_ROOT/repo"',
            'tar --no-same-owner --no-same-permissions -xf "$UPLOAD_DIR/source.tar" -C "$REMOTE_ROOT/repo"',
            'chmod -R go-w "$REMOTE_ROOT/repo"',
            'cd "$REMOTE_ROOT/repo"',
            "uv sync --frozen --extra dev",
            'mkdir -m 0700 "$REMOTE_ROOT/oci-fs-evidence"',
            'mkdir -m 0700 "$REMOTE_ROOT/oci-fs-replay"',
            (
                "sudo --preserve-env=PALIMPSEST_OCI_FS_EVIDENCE_DIR,PALIMPSEST_REQUIRE_OCI_FS "
                'PALIMPSEST_OCI_FS_EVIDENCE_DIR="$REMOTE_ROOT/oci-fs-evidence" '
                "PALIMPSEST_REQUIRE_OCI_FS=1 "
                '"$(command -v uv)" run python -m pytest -m oci_fs tests/oci_fs -vv'
            ),
            (
                "sudo --preserve-env=PALIMPSEST_OCI_FS_EVIDENCE_DIR,PALIMPSEST_REQUIRE_OCI_FS "
                'PALIMPSEST_OCI_FS_EVIDENCE_DIR="$REMOTE_ROOT/oci-fs-replay" '
                "PALIMPSEST_REQUIRE_OCI_FS=1 "
                '"$(command -v uv)" run python -m pytest -m oci_fs -k layer_filesystem_semantics tests/oci_fs -vv'
            ),
            'sudo cmp "$REMOTE_ROOT/oci-fs-evidence/squashfs.json" "$REMOTE_ROOT/oci-fs-replay/squashfs.json"',
            'sudo cp "$REMOTE_ROOT/oci-fs-replay/squashfs.json" "$REMOTE_ROOT/oci-fs-evidence/squashfs-replay.json"',
            (
                'test -s "$REMOTE_ROOT/oci-fs-evidence/squashfs.json" && '
                'test -s "$REMOTE_ROOT/oci-fs-evidence/squashfs-replay.json" && '
                'test -s "$REMOTE_ROOT/oci-fs-evidence/erofs.json"'
            ),
            'sudo chown -R "$(id -u):$(id -g)" "$REMOTE_ROOT/oci-fs-evidence"',
            'chmod 0700 "$REMOTE_ROOT/oci-fs-evidence"',
            'chmod 0400 "$REMOTE_ROOT/oci-fs-evidence"/*',
            "test -c /dev/kvm",
            "uv sync --frozen --extra dev",
            'install -d -m 0700 "$REMOTE_ROOT/stage1-kvm-evidence"',
            (
                'sudo -u "$(id -un)" env '
                "PALIMPSEST_REQUIRE_STAGE1_KVM=1 "
                'PALIMPSEST_KVM_KERNEL="$REMOTE_ROOT/pinned/vmlinuz" '
                'PALIMPSEST_KVM_KERNEL_CONFIG="$REMOTE_ROOT/pinned/kernel.config" '
                'PALIMPSEST_KVM_QEMU="$QEMU_BIN" '
                'PALIMPSEST_KVM_EVIDENCE_DIR="$REMOTE_ROOT/stage1-kvm-evidence" '
                '"$(command -v uv)" run python -m pytest -m stage1_kvm tests/kvm -vv'
            ),
            'tar -cf "$REMOTE_ROOT/evidence-bundle.tar" -C "$REMOTE_ROOT" oci-fs-evidence stage1-kvm-evidence',
        )
    )


def _extract_and_verify_evidence_bundle(
    bundle_path: Path,
    evidence_dir: Path,
    *,
    expected_kernel_sha256: str,
    expected_kernel_config_sha256: str,
    require_complete: bool = True,
) -> tuple[dict[str, str], dict[str, str]]:
    allowed_dirs = {"oci-fs-evidence", "stage1-kvm-evidence"}
    allowed_files = {
        *(f"oci-fs-evidence/{name}" for name in EXPECTED_OCI_FS_EVIDENCE_FILES),
        *(f"stage1-kvm-evidence/{name}" for name in EXPECTED_STAGE1_KVM_EVIDENCE_FILES),
    }

    oci_fs_dir = evidence_dir / "oci-fs-evidence"
    stage1_dir = evidence_dir / "stage1-kvm-evidence"
    if oci_fs_dir.exists() or stage1_dir.exists():
        raise NativeOpenStackError("refusing to overwrite existing extracted evidence")
    oci_fs_dir.mkdir(mode=0o700)
    stage1_dir.mkdir(mode=0o700)

    seen_files: set[str] = set()
    digests: dict[str, dict[str, str]] = {directory: {} for directory in allowed_dirs}
    try:
        with tarfile.open(bundle_path, mode="r:*") as archive:
            members: list[tarfile.TarInfo] = []
            seen_names: set[str] = set()
            expanded_bytes = 0
            for member in archive:
                normalized = member.name.rstrip("/")
                if normalized in seen_names or len(seen_names) >= len(allowed_files) + len(allowed_dirs):
                    raise NativeOpenStackError("duplicate or excessive evidence archive members")
                seen_names.add(normalized)
                if normalized in allowed_dirs:
                    if not member.isdir() or member.size != 0:
                        raise NativeOpenStackError(f"evidence archive entry {normalized!r} must be a directory")
                    continue
                if normalized not in allowed_files or not member.isfile() or member.sparse is not None:
                    raise NativeOpenStackError(f"unexpected or unsafe evidence archive member: {member.name!r}")
                if member.size < (1 if require_complete else 0) or member.size > 16 * 1024 * 1024:
                    raise NativeOpenStackError(f"evidence archive member {normalized!r} has invalid size")
                expanded_bytes += member.size
                if expanded_bytes > 256 * 1024 * 1024:
                    raise NativeOpenStackError("evidence archive exceeds expanded size limit")
                members.append(member)
            # Validate every member before publishing any capture, including on failure.
            for member in members:
                normalized = member.name.rstrip("/")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise NativeOpenStackError(f"unable to read evidence archive member: {normalized!r}")
                payload = extracted.read(member.size + 1)
                if len(payload) != member.size:
                    raise NativeOpenStackError(f"evidence archive member size mismatch: {normalized!r}")
                destination = evidence_dir / normalized
                fd = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o400,
                )
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                seen_files.add(normalized)
                directory, name = normalized.split("/", 1)
                digests[directory][name] = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    except NativeOpenStackError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise NativeOpenStackError(f"evidence bundle extraction failed: {exc}") from exc
    if not require_complete:
        return digests["oci-fs-evidence"], digests["stage1-kvm-evidence"]

    if seen_files != allowed_files:
        missing = sorted(allowed_files - seen_files)
        raise NativeOpenStackError(f"evidence bundle is missing required files: {missing}")

    squashfs_bytes = (oci_fs_dir / "squashfs.json").read_bytes()
    replay_bytes = (oci_fs_dir / "squashfs-replay.json").read_bytes()
    erofs_bytes = (oci_fs_dir / "erofs.json").read_bytes()
    if squashfs_bytes != replay_bytes:
        raise NativeOpenStackError("oci-fs-evidence squashfs.json and squashfs-replay.json differ")
    if not erofs_bytes:
        raise NativeOpenStackError("oci-fs-evidence erofs.json is empty")

    try:
        stage1_receipt = json.loads((stage1_dir / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeOpenStackError("stage1-kvm-evidence/receipt.json is not valid JSON") from exc
    if not isinstance(stage1_receipt, dict):
        raise NativeOpenStackError("stage1-kvm-evidence/receipt.json must be a JSON object")
    if stage1_receipt.get("executed_boots") != 43 or stage1_receipt.get("qemu_invocations") != 44:
        raise NativeOpenStackError("stage1-kvm-evidence/receipt.json did not record 43 boots and 44 QEMU invocations")
    expected_qualification = {
        "accelerator": "kvm",
        "architecture": "x86_64",
        "cpu": "host",
        "kvm_api_version": 12,
        "live_pid1": True,
    }
    if stage1_receipt.get("qualification") != expected_qualification:
        raise NativeOpenStackError("stage1-kvm-evidence/receipt.json qualification block is invalid")

    kernel_binding = stage1_receipt.get("kernel", {})
    receipt_kernel_digest = kernel_binding.get("artifact_digest") if isinstance(kernel_binding, Mapping) else None
    if receipt_kernel_digest not in {expected_kernel_sha256, f"sha256:{expected_kernel_sha256}"}:
        raise NativeOpenStackError("stage1-kvm-evidence/receipt.json kernel digest does not match pinned kernel")

    receipt_config_digest = kernel_binding.get("config_digest") if isinstance(kernel_binding, Mapping) else None
    if receipt_config_digest not in {expected_kernel_config_sha256, f"sha256:{expected_kernel_config_sha256}"}:
        raise NativeOpenStackError(
            "stage1-kvm-evidence/receipt.json kernel config digest does not match pinned kernel config"
        )

    return digests["oci-fs-evidence"], digests["stage1-kvm-evidence"]


@_bounded_operation
def prove_native_kvm(
    request: ProveRequest,
    *,
    env: Mapping[str, str] | None = None,
    cloud_factory: CloudFactory = _default_cloud_factory,
    command_runner: CommandRunner = _default_run_command,
    egress_ip_resolver: EgressIPResolver = _default_resolve_egress_ipv4,
    clock: Clock = time.monotonic,
    sleeper: Sleeper = time.sleep,
    expected_kernel_sha256: str | None = None,
    expected_kernel_config_sha256: str | None = None,
) -> dict[str, object]:
    """Run the isolated disposable OpenStack Nova native KVM proof and verify resource cleanup."""

    effective_env = dict(os.environ if env is None else env)
    deadline = clock() + request.deadline_seconds

    owner, kernel_path, kernel_sha256, config_path, config_sha256 = validate_prove_inputs(
        request,
        effective_env,
        expected_kernel_sha256=expected_kernel_sha256,
        expected_kernel_config_sha256=expected_kernel_config_sha256,
    )

    if request.evidence_dir.exists() and request.evidence_dir.is_symlink():
        raise NativeOpenStackError(f"evidence-dir must not be a symlink: {request.evidence_dir}")

    conn, _, _ = _authenticate_project_bound(cloud_factory, effective_env, request.project_id)
    image_sha512 = _verify_cloud_prerequisites(conn, request, effective_env)
    egress_ipv4 = _validate_public_egress_ipv4(
        egress_ip_resolver(_remaining_seconds(deadline, clock, "resolving runner egress IPv4"))
    )

    request.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = request.evidence_dir / "resource-manifest.json"
    intent_path = manifest_path.with_name(f"{manifest_path.name}.creation-intent.json")
    if any(path.exists() or path.is_symlink() for path in (manifest_path, intent_path)):
        raise NativeOpenStackError(f"refusing to overwrite existing resource ownership state: {manifest_path}")

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_signal(signum: int, _frame: Any) -> None:
        raise NativeProofInterrupted(f"native KVM proof interrupted by signal {signum}")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    temp_dir_path = Path(tempfile.mkdtemp(prefix="palimpsest-native-ssh-", dir=request.evidence_dir.resolve().parent))
    temp_dir_path.chmod(0o700)
    manifest: ResourceManifest | None = None
    primary_error: BaseException | None = None
    receipt_payload: dict[str, object] | None = None

    try:
        private_key_path = temp_dir_path / "id_ed25519"
        public_key_path = temp_dir_path / "id_ed25519.pub"
        known_hosts_path = temp_dir_path / "known_hosts"
        bundle_path = temp_dir_path / "evidence-bundle.tar"

        _expect_command(
            command_runner,
            (
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                owner.owner_id,
                "-f",
                os.fspath(private_key_path),
            ),
            deadline=deadline,
            clock=clock,
            action="generating ephemeral SSH keypair",
            env=effective_env,
            step_timeout=30.0,
        )
        if not private_key_path.is_file() or not public_key_path.is_file():
            raise NativeOpenStackError("ssh-keygen did not produce expected keypair files")
        private_key_path.chmod(0o600)
        public_key = public_key_path.read_text(encoding="utf-8").strip()
        if not public_key.startswith("ssh-ed25519 "):
            raise NativeOpenStackError("generated SSH public key is not ssh-ed25519")

        manifest = _write_manifest(
            manifest_path,
            ResourceManifest(
                schema=MANIFEST_SCHEMA,
                repository=owner.repository,
                run_id=owner.run_id,
                run_attempt=owner.run_attempt,
                source_sha=owner.source_sha,
                source_sha256=owner.source_sha256,
                project_id=request.project_id,
                server_id=None,
                volume_id=None,
                port_id=None,
                security_group_id=None,
                keypair_name=None,
                created_at=_utc_now_iso(),
                cleanup_verified=False,
            ),
        )

        _remaining_seconds(deadline, clock, "creating Nova keypair")
        manifest = _write_manifest(manifest_path, replace(manifest, inflight="keypair_name"))
        keypair = conn.compute.create_keypair(name=owner.keypair_name, public_key=public_key)
        created_keypair_name = str(_attr_or_key(keypair, "name", owner.keypair_name)).strip()
        if created_keypair_name != owner.keypair_name:
            raise NativeOpenStackError("created keypair name did not match expected owner keypair name")
        manifest = _write_manifest(manifest_path, replace(manifest, keypair_name=created_keypair_name, inflight=None))

        _remaining_seconds(deadline, clock, "creating Neutron security group")
        manifest = _write_manifest(manifest_path, replace(manifest, inflight="security_group_id"))
        sg = conn.network.create_security_group(
            name=owner.security_group_name,
            description=owner.description,
        )
        sg_id = str(_attr_or_key(sg, "id", "")).strip()
        if not sg_id:
            raise NativeOpenStackError("created security group did not return an id")
        manifest = _write_manifest(manifest_path, replace(manifest, security_group_id=sg_id, inflight=None))
        _apply_neutron_tags_if_supported(conn, sg, owner.tags)

        conn.network.create_security_group_rule(
            security_group_id=sg_id,
            direction="ingress",
            ethertype="IPv4",
            protocol="tcp",
            port_range_min=22,
            port_range_max=22,
            remote_ip_prefix=f"{egress_ipv4}/32",
        )

        _remaining_seconds(deadline, clock, "creating public_provider port")
        manifest = _write_manifest(manifest_path, replace(manifest, inflight="port_id"))
        port = conn.network.create_port(
            name=owner.port_name,
            network_id=request.network_id,
            security_group_ids=[sg_id],
            description=owner.description,
        )
        port_id = str(_attr_or_key(port, "id", "")).strip()
        if not port_id:
            raise NativeOpenStackError("created port did not return an id")
        manifest = _write_manifest(manifest_path, replace(manifest, port_id=port_id, inflight=None))
        _apply_neutron_tags_if_supported(conn, port, owner.tags)
        server_ip = _extract_port_ipv4(port)

        _remaining_seconds(deadline, clock, "creating Cinder boot volume")
        manifest = _write_manifest(manifest_path, replace(manifest, inflight="volume_id"))
        volume = conn.block_storage.create_volume(
            name=owner.volume_name,
            size=APPROVED_VOLUME_SIZE_GIB,
            image_id=request.image_id,
            volume_type=APPROVED_VOLUME_TYPE,
            metadata=owner.metadata,
            description=owner.description,
        )
        volume_id = str(_attr_or_key(volume, "id", "")).strip()
        if not volume_id:
            raise NativeOpenStackError("created volume did not return an id")
        manifest = _write_manifest(manifest_path, replace(manifest, volume_id=volume_id, inflight=None))

        while True:
            current_volume = _get_or_none(conn.block_storage.get_volume, volume_id, effective_env)
            if current_volume is None:
                raise NativeOpenStackError(f"volume {volume_id} disappeared while waiting for available status")
            volume_status = str(_attr_or_key(current_volume, "status", "")).lower()
            if volume_status == "available":
                break
            if volume_status in {"error", "error_restoring", "error_extending"}:
                raise NativeOpenStackError(f"volume {volume_id} entered status {volume_status!r}")
            remaining = _remaining_seconds(deadline, clock, f"waiting for volume {volume_id} to become available")
            sleeper(min(POLL_INTERVAL_SECONDS, remaining))

        _remaining_seconds(deadline, clock, "creating Nova server")
        manifest = _write_manifest(manifest_path, replace(manifest, inflight="server_id"))
        server = conn.compute.create_server(
            name=owner.server_name,
            flavor_id=request.flavor_id,
            key_name=owner.keypair_name,
            networks=[{"port": port_id}],
            block_device_mapping_v2=[
                {
                    "uuid": volume_id,
                    "source_type": "volume",
                    "destination_type": "volume",
                    "boot_index": 0,
                    "delete_on_termination": False,
                }
            ],
            metadata=owner.metadata,
            config_drive=True,
        )
        server_id = str(_attr_or_key(server, "id", "")).strip()
        if not server_id:
            raise NativeOpenStackError("created server did not return an id")
        manifest = _write_manifest(manifest_path, replace(manifest, server_id=server_id, inflight=None))

        while True:
            current_server = _get_or_none(conn.compute.get_server, server_id, effective_env)
            if current_server is None:
                raise NativeOpenStackError(f"server {server_id} disappeared while waiting for ACTIVE status")
            server_status = str(_attr_or_key(current_server, "status", "")).upper()
            if server_status == "ACTIVE":
                break
            if server_status in {"ERROR", "DELETED", "SOFT_DELETED"}:
                raise NativeOpenStackError(f"server {server_id} entered status {server_status!r}")
            remaining = _remaining_seconds(deadline, clock, f"waiting for server {server_id} to become ACTIVE")
            sleeper(min(POLL_INTERVAL_SECONDS, remaining))

        console_fingerprint: str | None = None
        console_key_b64: str | None = None
        while console_fingerprint is None:
            _remaining_seconds(deadline, clock, "waiting for Nova console SSH host key fingerprint")
            raw_console = conn.compute.get_server_console_output(server_id)
            console_text = (
                raw_console.get("output", "")
                if isinstance(raw_console, Mapping)
                else (raw_console if isinstance(raw_console, str) else "")
            )
            if console_text:
                try:
                    console_fingerprint, console_key_b64 = extract_console_ed25519_identity(console_text)
                    break
                except NativeOpenStackError as exc:
                    if "conflicting" in str(exc):
                        raise
            remaining = _remaining_seconds(deadline, clock, "polling Nova console for SSH host key fingerprint")
            sleeper(min(POLL_INTERVAL_SECONDS, remaining))

        scan_output: str | None = None
        while scan_output is None:
            remaining = _remaining_seconds(deadline, clock, "scanning VM SSH host key")
            scan_result = command_runner(
                ("ssh-keyscan", "-t", "ed25519", "-T", "5", server_ip),
                min(10.0, remaining),
            )
            text = scan_result.stdout.decode("utf-8", errors="replace").strip()
            if scan_result.returncode == 0 and "ssh-ed25519" in text:
                scan_output = text
                break
            remaining = _remaining_seconds(deadline, clock, "retrying ssh-keyscan until sshd is listening")
            sleeper(min(POLL_INTERVAL_SECONDS, remaining))

        known_hosts_line = verify_scanned_ssh_host_key(
            server_ip,
            scan_output,
            console_fingerprint,
            console_key_b64,
        )
        known_hosts_path.write_text(known_hosts_line, encoding="utf-8")
        known_hosts_path.chmod(0o600)

        ssh_common_opts = (
            "-F",
            "/dev/null",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={os.fspath(known_hosts_path)}",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            os.fspath(private_key_path),
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        )
        remote_target = f"ubuntu@{server_ip}"

        _expect_command(
            command_runner,
            (
                "ssh",
                *ssh_common_opts,
                remote_target,
                "rm -rf /tmp/palimpsest-native-proof-upload && install -d -m 0700 /tmp/palimpsest-native-proof-upload",
            ),
            deadline=deadline,
            clock=clock,
            action="preparing remote upload directory",
            env=effective_env,
            step_timeout=60.0,
        )

        for local_file, remote_name in (
            (request.source_tar, "source.tar"),
            (kernel_path, "vmlinuz"),
            (config_path, "kernel.config"),
        ):
            _expect_command(
                command_runner,
                (
                    "scp",
                    "-B",
                    *ssh_common_opts,
                    os.fspath(local_file),
                    f"{remote_target}:/tmp/palimpsest-native-proof-upload/{remote_name}",
                ),
                deadline=deadline,
                clock=clock,
                action=f"uploading {remote_name} to disposable VM",
                env=effective_env,
            )

        remote_script = _build_remote_proof_script(
            source_sha256=owner.source_sha256,
            kernel_sha256=kernel_sha256,
            kernel_config_sha256=config_sha256,
        )
        proof_error: BaseException | None = None
        try:
            remote_seconds = max(1, int(_remaining_seconds(deadline, clock, "running remote proof")) - 15)
            _expect_command(
                command_runner,
                (
                    "ssh",
                    *ssh_common_opts,
                    remote_target,
                    f"timeout --signal=TERM --kill-after=10 {remote_seconds} bash -c {shlex.quote(remote_script)}",
                ),
                deadline=deadline,
                clock=clock,
                action="executing remote mounted-FS and stage-1 native KVM proof",
                env=effective_env,
            )
        except BaseException as exc:
            proof_error = exc
        finally:
            # A separate bounded recovery interval retrieves partial evidence even on
            # deadline/signal failure. Never turn a failed proof into a success.
            signal.setitimer(signal.ITIMER_REAL, 90)
            recovery_deadline = clock() + 90
            try:
                pack = (
                    "set -e; root=/tmp/palimpsest-native-proof; "
                    'sudo install -d -m 0700 "$root/oci-fs-evidence" "$root/stage1-kvm-evidence"; '
                    'sudo tar -cf "$root/evidence-bundle.tar" -C "$root" oci-fs-evidence stage1-kvm-evidence; '
                    'sudo chown "$(id -u):$(id -g)" "$root/evidence-bundle.tar"'
                )
                _expect_command(
                    command_runner,
                    ("ssh", *ssh_common_opts, remote_target, pack),
                    deadline=recovery_deadline,
                    clock=clock,
                    action="packing remote evidence",
                    env=effective_env,
                )
                _expect_command(
                    command_runner,
                    (
                        "scp",
                        "-B",
                        *ssh_common_opts,
                        f"{remote_target}:/tmp/palimpsest-native-proof/evidence-bundle.tar",
                        os.fspath(bundle_path),
                    ),
                    deadline=recovery_deadline,
                    clock=clock,
                    action="retrieving remote evidence",
                    env=effective_env,
                )
                oci_fs_digests, stage1_digests = _extract_and_verify_evidence_bundle(
                    bundle_path,
                    request.evidence_dir,
                    expected_kernel_sha256=kernel_sha256,
                    expected_kernel_config_sha256=config_sha256,
                    require_complete=proof_error is None,
                )
                shutil.copyfile(bundle_path, request.evidence_dir / "evidence-bundle.tar")
            except BaseException as retrieval_error:
                raise NativeOpenStackError(
                    "evidence retrieval failed"
                    + (f" after {_sanitize_text(str(proof_error), effective_env)}" if proof_error else "")
                ) from retrieval_error
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        if proof_error is not None:
            raise proof_error

        receipt_payload = {
            "schema": PROOF_RECEIPT_SCHEMA,
            "repository": owner.repository,
            "event": request.event,
            "ref": request.ref,
            "source_sha": owner.source_sha,
            "source_sha256": owner.source_sha256,
            "run_id": owner.run_id,
            "run_attempt": owner.run_attempt,
            "owner_id": owner.owner_id,
            "project_id": request.project_id,
            "image_id": request.image_id,
            "image_sha512": image_sha512,
            "flavor_id": request.flavor_id,
            "network_id": request.network_id,
            "server_id": manifest.server_id,
            "volume_id": manifest.volume_id,
            "port_id": manifest.port_id,
            "security_group_id": manifest.security_group_id,
            "keypair_name": manifest.keypair_name,
            "ssh_host_key_fingerprint": console_fingerprint,
            "kernel_sha256": kernel_sha256,
            "kernel_config_sha256": config_sha256,
            "executed_boots": 43,
            "qemu_invocations": 44,
            "oci_fs_evidence_digests": oci_fs_digests,
            "stage1_kvm_evidence_digests": stage1_digests,
        }
        _atomic_write_json(request.evidence_dir / "native-proof-receipt.json", receipt_payload)
    except BaseException as exc:
        primary_error = exc
        if isinstance(exc, NativeOpenStackError):
            raise
        raise NativeOpenStackError(_sanitize_text(str(exc), effective_env)) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        cleanup_signals: list[int] = []

        def record_cleanup_signal(signum: int, _frame: Any) -> None:
            cleanup_signals.append(signum)

        signal.signal(signal.SIGINT, record_cleanup_signal)
        signal.signal(signal.SIGTERM, record_cleanup_signal)
        shutil.rmtree(temp_dir_path, ignore_errors=True)
        cleanup_error: BaseException | None = None
        if manifest is not None or manifest_path.exists():
            cleanup_deadline = DEFAULT_CLEANUP_DEADLINE_SECONDS
            try:
                cleaned = cleanup_manifest(
                    manifest_path,
                    env=effective_env,
                    cloud_factory=cloud_factory,
                    deadline_seconds=cleanup_deadline,
                    clock=clock,
                    sleeper=sleeper,
                )
                if cleaned is None or not cleaned.cleanup_verified:
                    cleanup_error = NativeOpenStackError("resource cleanup did not complete verification")
            except BaseException as exc:
                cleanup_error = exc
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if cleanup_error is not None:
            if primary_error is not None:
                raise NativeOpenStackError(
                    f"{_sanitize_text(str(primary_error), effective_env)}; "
                    f"cleanup also failed: {_sanitize_text(str(cleanup_error), effective_env)}"
                ) from cleanup_error
            raise cleanup_error
        if cleanup_signals:
            raise NativeProofInterrupted(f"native KVM cleanup interrupted by signal {cleanup_signals[0]}")

    assert receipt_payload is not None
    return receipt_payload


def _check_forbidden_argv(argv: Sequence[str]) -> None:
    for arg in argv:
        lower = arg.lower()
        if any(lower.startswith(prefix) for prefix in _FORBIDDEN_ARGV_PREFIXES):
            raise NativeOpenStackError(
                "cloud credentials and secrets must be supplied only via OS_* environment variables, never argv"
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision an isolated OpenStack Nova VM for native KVM stage-1 proof and clean up owned resources."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prove_parser = subparsers.add_parser("prove", help="Run isolated native KVM proof in a disposable Nova VM.")
    prove_parser.add_argument("--repository", required=True)
    prove_parser.add_argument("--event", required=True)
    prove_parser.add_argument("--ref", required=True)
    prove_parser.add_argument("--sha", required=True)
    prove_parser.add_argument("--source-tar", type=Path, required=True)
    prove_parser.add_argument("--source-sha256", required=True)
    prove_parser.add_argument("--project-id", required=True)
    prove_parser.add_argument("--image-id", required=True)
    prove_parser.add_argument("--flavor-id", required=True)
    prove_parser.add_argument("--network-id", required=True)
    prove_parser.add_argument("--evidence-dir", type=Path, required=True)
    prove_parser.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)

    cleanup_parser = subparsers.add_parser("cleanup", help="Verify ownership and delete manifest-recorded resources.")
    cleanup_parser.add_argument("--manifest", type=Path, required=True)
    cleanup_parser.add_argument("--deadline-seconds", type=int, default=DEFAULT_CLEANUP_DEADLINE_SECONDS)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        _check_forbidden_argv(effective_argv)
        args = _build_parser().parse_args(effective_argv)
        if args.command == "prove":
            prove_native_kvm(
                ProveRequest(
                    repository=args.repository,
                    event=args.event,
                    ref=args.ref,
                    sha=args.sha,
                    source_tar=args.source_tar,
                    source_sha256=args.source_sha256,
                    project_id=args.project_id,
                    image_id=args.image_id,
                    flavor_id=args.flavor_id,
                    network_id=args.network_id,
                    evidence_dir=args.evidence_dir,
                    deadline_seconds=args.deadline_seconds,
                )
            )
            return 0
        if args.command == "cleanup":
            cleanup_manifest(args.manifest, deadline_seconds=args.deadline_seconds)
            return 0
        raise NativeOpenStackError(f"unknown command: {args.command}")
    except NativeOpenStackError as exc:
        sys.stderr.write(f"{_sanitize_text(str(exc), os.environ)}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
